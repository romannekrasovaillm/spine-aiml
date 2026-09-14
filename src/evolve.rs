//! Guarded harness evolution (H2.2): самоулучшение через propose/commit с
//! детерминированным стражем.
//!
//! Доменные плагины/скиллы/маршруты эволюционируют из failure-траекторий, но
//! изменение не попадает в активный слой без гейта: именной аппрувер (govern),
//! `managed`-блок со сверкой хэша (managed) и подтверждение, что
//! детерминированный слой (fitness) не регрессировал (контрольный прогон
//! отдельным шагом CI).
//!
//! ADR-046 п.3 расширяет протокол на файлы ядра (`src/**`): кроме
//! managed-блоков появляется режим [`MODE_FILE`] — замена файла целиком со
//! сверкой базового SHA-256 и **обязательным** зелёным прогоном гейтов
//! baseline-судьёй (судья не принадлежит проверяемому дереву). Режим
//! [`MODE_BLOCK`] сохраняет прежнее поведение.
//!
//! Порядок коммита для [`MODE_FILE`] — «применить → судить → зафиксировать или
//! откатить»: судится **внесённый** контент, а не дерево до правки (дерево
//! зелено и без дельты, поэтому прогон гейта до применения пропускал бы
//! регрессию). Красный гейт, сбой запуска судьи или таймаут возвращают цель к
//! исходному состоянию; статус предложения остаётся `proposed`. Инвариант
//! исхода: дерево содержит либо ровно предложенный контент (успех), либо ровно
//! исходное состояние (отказ) — частично применённого состояния не остаётся.
//!
//! Журнал — append-only JSONL в `state/evolve/`.

use std::io::Write as _;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::error::{HarnessError, Result};
use crate::managed::Sha256;

/// Режим managed-блока: тело блока внутри артефакта (дефолт, обратная
/// совместимость предложений без поля `mode`).
pub const MODE_BLOCK: &str = "block";
/// Режим замены файла целиком (правка ядра `src/**`, ADR-046 п.3).
pub const MODE_FILE: &str = "file";

/// Переменная окружения с путём baseline-судьи (ADR-046 п.1).
pub const JUDGE_ENV: &str = "ARCH_ML_JUDGE";

/// Файлы-цели, закрытые для `evolve`: зона архитектора, только рука человека.
const PROTECTED_FILES: &[&str] = &["CONSTRAINTS.yaml"];
/// Каталоги-цели, закрытые для `evolve` (ADR, spine — зона архитектора).
const PROTECTED_PREFIXES: &[&str] = &["docs/adr/"];
/// Префикс имени spine-документов, закрытых для `evolve`.
const PROTECTED_SPINE_PREFIX: &str = "ARCHITECTURE-SPINE";
/// Потолок текста находок гейта в отказе коммита, символов.
const GATE_FINDINGS_MAX_CHARS: usize = 2000;
/// Таймаут одной команды гейта по умолчанию, секунды. Зависший судья — красный
/// гейт (и откат), а не вечно применённая правка.
const DEFAULT_GATE_TIMEOUT_SECS: u64 = 600;
/// Шаг опроса завершения процесса гейта, миллисекунды.
const GATE_POLL_STEP_MS: u64 = 50;

fn default_mode() -> String {
    MODE_BLOCK.to_string()
}

/// Предложение изменения харнесса.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvolutionProposal {
    /// Идентификатор предложения (kebab-case).
    pub id: String,
    /// Целевой файл (относительно репо).
    pub target: String,
    /// Managed-блок (id) внутри файла — только для [`MODE_BLOCK`].
    pub block_id: String,
    /// Режим применения: [`MODE_BLOCK`] | [`MODE_FILE`].
    #[serde(default = "default_mode")]
    pub mode: String,
    /// Новое содержимое: тело блока ([`MODE_BLOCK`]) либо файл целиком
    /// ([`MODE_FILE`]).
    pub content: String,
    /// SHA-256 целевого файла на момент предложения ([`MODE_FILE`];
    /// голый hex). Несовпадение при применении — отказ.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub base_sha256: Option<String>,
    /// Именной аппрувер (как в govern).
    pub approver: String,
    /// Статус: proposed|committed|rejected.
    pub status: String,
    /// Штамп.
    pub at: String,
}

/// Запрос на запись предложения (H2.2).
#[derive(Debug, Clone)]
pub struct ProposeRequest<'a> {
    /// Идентификатор предложения (kebab-case).
    pub id: &'a str,
    /// Целевой файл (относительно репо).
    pub target: &'a str,
    /// Режим: [`MODE_BLOCK`] | [`MODE_FILE`].
    pub mode: &'a str,
    /// Managed-блок (id) — только для [`MODE_BLOCK`].
    pub block_id: &'a str,
    /// Новое содержимое (тело блока либо файл целиком).
    pub content: &'a str,
    /// Именной аппрувер.
    pub approver: &'a str,
    /// Базовый SHA-256 файла ([`MODE_FILE`]); `None` — снять с диска сейчас.
    pub base_sha256: Option<&'a str>,
}

/// Одна механическая проверка гейта: программа и аргументы (без shell).
#[derive(Debug, Clone)]
pub struct GateCommand {
    /// Программа.
    pub program: PathBuf,
    /// Аргументы.
    pub args: Vec<String>,
}

impl GateCommand {
    /// Человекочитаемая командная строка (для отчёта об отказе).
    #[must_use]
    pub fn display(&self) -> String {
        let mut text = self.program.display().to_string();
        for arg in &self.args {
            text.push(' ');
            text.push_str(arg);
        }
        text
    }
}

/// Политика гейта перед коммитом file-предложения (ADR-046 п.3).
///
/// Судья фиксируется ДО прогона и не принадлежит проверяемому дереву;
/// команды исполняются синхронно, без shell, в каталоге репозитория, каждая —
/// с таймаутом [`DEFAULT_GATE_TIMEOUT_SECS`] (переопределяется
/// [`GatePlan::with_timeout`]).
#[derive(Debug, Clone)]
pub struct GatePlan {
    judge: PathBuf,
    commands: Vec<GateCommand>,
    timeout: Duration,
}

impl GatePlan {
    /// План по умолчанию: fitness-контроль baseline-судьёй и проверка формата.
    ///
    /// Команды: `<судья> control check . --constraints CONSTRAINTS.yaml` и
    /// `cargo fmt --all -- --check`.
    #[must_use]
    pub fn baseline(judge: PathBuf) -> Self {
        Self {
            commands: vec![
                GateCommand {
                    program: judge.clone(),
                    args: vec![
                        "control".into(),
                        "check".into(),
                        ".".into(),
                        "--constraints".into(),
                        "CONSTRAINTS.yaml".into(),
                    ],
                },
                GateCommand {
                    program: PathBuf::from("cargo"),
                    args: vec!["fmt".into(), "--all".into(), "--".into(), "--check".into()],
                },
            ],
            judge,
            timeout: Duration::from_secs(DEFAULT_GATE_TIMEOUT_SECS),
        }
    }

    /// План с явным набором команд (тесты, нестандартный гейт).
    #[must_use]
    pub fn with_commands(judge: PathBuf, commands: Vec<GateCommand>) -> Self {
        Self {
            judge,
            commands,
            timeout: Duration::from_secs(DEFAULT_GATE_TIMEOUT_SECS),
        }
    }

    /// Копия плана с другим таймаутом одной команды (тесты, нестандартный гейт).
    #[must_use]
    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// Путь baseline-судьи.
    #[must_use]
    pub fn judge(&self) -> &Path {
        &self.judge
    }

    /// Команды гейта.
    #[must_use]
    pub fn commands(&self) -> &[GateCommand] {
        &self.commands
    }

    /// Таймаут одной команды гейта.
    #[must_use]
    pub fn timeout(&self) -> Duration {
        self.timeout
    }
}

/// Каталог журнала эволюции.
fn evolve_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("evolve")
}

fn now() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// Нормализует цель: относительный путь без `..`/корня (защита от записи
/// за пределы репозитория). Возвращает путь в виде `a/b`.
fn normalize_target(raw: &str) -> Result<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(HarnessError::Tool(
            "evolve: цель пуста (нужен относительный путь)".to_string(),
        ));
    }
    let path = Path::new(trimmed);
    if path.is_absolute() {
        return Err(HarnessError::Tool(format!(
            "evolve: цель должна быть относительным путём: {trimmed}"
        )));
    }
    let mut parts: Vec<String> = Vec::new();
    for c in path.components() {
        match c {
            Component::Normal(s) => parts.push(s.to_string_lossy().into_owned()),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(HarnessError::Tool(format!(
                    "evolve: недопустимый сегмент в цели {trimmed}: выход за пределы репозитория"
                )));
            }
        }
    }
    if parts.is_empty() {
        return Err(HarnessError::Tool(format!(
            "evolve: цель не указывает на файл: {trimmed}"
        )));
    }
    Ok(parts.join("/"))
}

/// Цель закрыта для `evolve` (зона архитектора: ADR, constraints, spine).
fn is_protected_target(target: &str) -> bool {
    let is_spine = target.starts_with(PROTECTED_SPINE_PREFIX)
        && Path::new(target)
            .extension()
            .is_some_and(|e| e.eq_ignore_ascii_case("md"));
    PROTECTED_FILES.contains(&target)
        || PROTECTED_PREFIXES.iter().any(|p| target.starts_with(p))
        || is_spine
}

/// Запрещает правку защищённых целей (ADR-046 п.3: зона архитектора).
fn ensure_writable_target(target: &str) -> Result<()> {
    if is_protected_target(target) {
        return Err(HarnessError::Tool(format!(
            "evolve: {target} — зона архитектора (ADR-046): правка только рукой человека"
        )));
    }
    Ok(())
}

/// Режим предложения с проверкой допустимых значений.
fn normalize_mode(raw: &str) -> Result<&str> {
    let mode = raw.trim();
    if mode.is_empty() {
        return Ok(MODE_BLOCK);
    }
    match mode {
        MODE_BLOCK | MODE_FILE => Ok(mode),
        other => Err(HarnessError::Tool(format!(
            "evolve: неизвестный режим {other} (допустимо: {MODE_BLOCK}|{MODE_FILE})"
        ))),
    }
}

/// SHA-256 файла; отсутствующий файл — хеш пустого содержимого (создание).
fn hash_target(path: &Path) -> Result<Sha256> {
    match std::fs::read(path) {
        Ok(bytes) => Ok(Sha256::of_bytes(&bytes)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Sha256::of_bytes(b"")),
        Err(e) => Err(HarnessError::io(path, e)),
    }
}

/// Записывает предложение изменения (status=proposed).
///
/// # Errors
/// Пустой id/аппрувер, неизвестный режим, неполное block-предложение,
/// защищённая цель, ошибка чтения базового файла или записи журнала.
pub fn evolve_propose(state_dir: &Path, repo: &Path, req: &ProposeRequest<'_>) -> Result<PathBuf> {
    let approver = req.approver.trim();
    if approver.is_empty() {
        return Err(HarnessError::Tool(
            "evolve: предложение без именного аппрувера не принимается".to_string(),
        ));
    }
    let id = req.id.trim();
    if id.is_empty() {
        return Err(HarnessError::Tool(
            "evolve: предложение без id не принимается".to_string(),
        ));
    }
    let mode = normalize_mode(req.mode)?.to_string();
    let target = normalize_target(req.target)?;
    ensure_writable_target(&target)?;

    let block_id = req.block_id.trim().to_string();
    let base_sha256 = if mode == MODE_FILE {
        Some(
            match req.base_sha256.map(str::trim).filter(|s| !s.is_empty()) {
                Some(raw) => Sha256::parse(raw)?.as_hex().to_string(),
                None => hash_target(&repo.join(&target))?.as_hex().to_string(),
            },
        )
    } else {
        if block_id.is_empty() {
            return Err(HarnessError::Tool(format!(
                "evolve: block-предложение {id} неполно (пуст block_id)"
            )));
        }
        None
    };

    let p = EvolutionProposal {
        id: id.to_string(),
        target,
        block_id,
        mode,
        content: req.content.to_string(),
        base_sha256,
        approver: approver.to_string(),
        status: "proposed".to_string(),
        at: now(),
    };
    append(state_dir, &p)
}

/// Читает предложения (хронологически).
///
/// # Errors
/// Ошибка чтения журнала или разбора JSON-строки.
pub fn evolve_list(state_dir: &Path) -> Result<Vec<EvolutionProposal>> {
    let path = evolve_dir(state_dir).join("proposals.jsonl");
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let text = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        out.push(serde_json::from_str(line).map_err(HarnessError::Json)?);
    }
    Ok(out)
}

/// Находит предложение по id.
///
/// # Errors
/// Предложение не найдено или журнал не читается.
pub fn evolve_find(state_dir: &Path, id: &str) -> Result<EvolutionProposal> {
    let id = id.trim();
    evolve_list(state_dir)?
        .into_iter()
        .find(|p| p.id == id)
        .ok_or_else(|| HarnessError::Tool(format!("evolve: предложение {id} не найдено")))
}

/// Помечает предложение committed (гейт прошёл) или rejected.
fn set_status(state_dir: &Path, id: &str, status: &str) -> Result<()> {
    let mut all = evolve_list(state_dir)?;
    let Some(p) = all.iter_mut().find(|p| p.id == id.trim()) else {
        return Err(HarnessError::Tool(format!(
            "evolve: предложение {id} не найдено"
        )));
    };
    p.status = status.to_string();
    p.at = now();
    let path = evolve_dir(state_dir).join("proposals.jsonl");
    let mut text = String::new();
    for p in &all {
        text.push_str(&serde_json::to_string(p).map_err(HarnessError::Json)?);
        text.push('\n');
    }
    crate::managed::atomic_write(&path, &text)?;
    Ok(())
}

/// Проверяет, что baseline-судья существует и лежит вне проверяемого дерева
/// (ADR-046 п.1: самооценка запрещена — судья не принадлежит дереву дельты).
///
/// # Errors
/// Судья недоступен или находится внутри `repo`.
pub fn ensure_judge_outside(repo: &Path, judge: &Path) -> Result<PathBuf> {
    let root = repo.canonicalize().map_err(|e| HarnessError::io(repo, e))?;
    let resolved = judge.canonicalize().map_err(|e| {
        HarnessError::Tool(format!(
            "evolve: baseline-судья {} недоступен: {e}",
            judge.display()
        ))
    })?;
    if !resolved.is_file() {
        return Err(HarnessError::Tool(format!(
            "evolve: baseline-судья {} не файл",
            resolved.display()
        )));
    }
    if resolved.starts_with(&root) {
        return Err(HarnessError::Tool(format!(
            "evolve: baseline-судья {} внутри проверяемого дерева {} — самооценка запрещена (ADR-046)",
            resolved.display(),
            root.display()
        )));
    }
    Ok(resolved)
}

/// Разрешает baseline-судью: явный путь → `ARCH_ML_JUDGE` → конфиг
/// (`[fleet] judge_binary`). Не задан — отказ (правка ядра без судьи
/// запрещена, ADR-046 п.3).
///
/// # Errors
/// Судья не задан, недоступен или лежит внутри проверяемого дерева.
pub fn resolve_judge(
    explicit: Option<&Path>,
    configured: Option<&Path>,
    repo: &Path,
) -> Result<PathBuf> {
    let from_env = std::env::var_os(JUDGE_ENV).map(PathBuf::from);
    let candidate = explicit
        .or(from_env.as_deref())
        .or(configured)
        .ok_or_else(|| {
            HarnessError::Tool(format!(
                "evolve: baseline-судья не задан (--judge / {JUDGE_ENV} / [fleet] judge_binary) — \
                 правка ядра без судьи запрещена (ADR-046)"
            ))
        })?;
    ensure_judge_outside(repo, candidate)
}

/// Хвост вывода гейта (для текста отказа) — не длиннее 2000 символов.
fn gate_findings(raw: &[u8]) -> String {
    let text = String::from_utf8_lossy(raw).trim().to_string();
    if text.chars().count() <= GATE_FINDINGS_MAX_CHARS {
        return text;
    }
    let skip = text.chars().count() - GATE_FINDINGS_MAX_CHARS;
    let tail: String = text.chars().skip(skip).collect();
    format!("…{tail}")
}

/// Итог одной команды гейта.
enum GateOutcome {
    /// Команда завершилась успешно (exit 0).
    Pass,
    /// Команда вернула ненулевой код.
    Fail {
        /// Код возврата (`None` — процесс убит сигналом).
        code: Option<i32>,
        /// Хвост вывода для отчёта.
        findings: String,
    },
    /// Команда не уложилась в таймаут и была убита — это красный гейт.
    Timeout {
        /// Хвост вывода, накопленный до убийства процесса.
        findings: String,
    },
}

/// Запускает одну команду гейта в `repo` с таймаутом.
///
/// Вывод пишется в одноразовый файл (не в pipe — процесс не блокируется на
/// заполненном буфере), поэтому хвост находок доступен и после убийства по
/// таймауту; файл удаляется вместе с [`tempfile::NamedTempFile`].
///
/// # Errors
/// Команда не запустилась (нет программы, нет прав на исполнение) или сбой
/// ожидания завершения. Зависание — не инфраструктурная ошибка, а красный
/// гейт: [`GateOutcome::Timeout`].
fn run_gate_command(repo: &Path, cmd: &GateCommand, timeout: Duration) -> Result<GateOutcome> {
    let log = tempfile::NamedTempFile::new().map_err(|e| {
        HarnessError::Tool(format!(
            "evolve: гейт `{}`: не создан файл вывода: {e}",
            cmd.display()
        ))
    })?;
    let stdout = log
        .as_file()
        .try_clone()
        .map_err(|e| HarnessError::io(log.path(), e))?;
    let stderr = log
        .as_file()
        .try_clone()
        .map_err(|e| HarnessError::io(log.path(), e))?;
    let mut child = Command::new(&cmd.program)
        .args(&cmd.args)
        .current_dir(repo)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .spawn()
        .map_err(|e| {
            HarnessError::Tool(format!(
                "evolve: гейт `{}` не запустился: {e} — коммит отклонён, статус proposed",
                cmd.display()
            ))
        })?;
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {
                if started.elapsed() >= timeout {
                    let _ = child.kill();
                    let _ = child.wait(); // забрать зомби
                    break None;
                }
                std::thread::sleep(Duration::from_millis(GATE_POLL_STEP_MS));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(HarnessError::Tool(format!(
                    "evolve: гейт `{}`: сбой ожидания завершения: {e}",
                    cmd.display()
                )));
            }
        }
    };
    let findings = gate_findings(&std::fs::read(log.path()).unwrap_or_default());
    match status {
        None => Ok(GateOutcome::Timeout { findings }),
        Some(s) if s.success() => Ok(GateOutcome::Pass),
        Some(s) => Ok(GateOutcome::Fail {
            code: s.code(),
            findings,
        }),
    }
}

/// Прогоняет механические проверки гейта; первая красная — отказ.
///
/// # Errors
/// Гейт без команд, команда не запустилась или вернула ненулевой код
/// (включая таймаут).
pub fn run_gate(repo: &Path, gate: &GatePlan) -> Result<()> {
    if gate.commands.is_empty() {
        return Err(HarnessError::Tool(
            "evolve: гейт без команд — коммит запрещён (ADR-046)".to_string(),
        ));
    }
    for cmd in &gate.commands {
        let display = cmd.display();
        match run_gate_command(repo, cmd, gate.timeout)? {
            GateOutcome::Pass => {}
            GateOutcome::Fail { code, findings } => {
                let reason = code.map_or_else(|| "сигнал".to_string(), |c| format!("exit {c}"));
                return Err(HarnessError::Tool(format!(
                    "evolve: гейт не пройден `{display}` ({reason}) — коммит отклонён, статус proposed\n{findings}"
                )));
            }
            GateOutcome::Timeout { findings } => {
                return Err(HarnessError::Tool(format!(
                    "evolve: гейт не пройден `{display}` (таймаут {:?}) — коммит отклонён, статус proposed\n{findings}",
                    gate.timeout
                )));
            }
        }
    }
    Ok(())
}

/// Коммит предложения: именной аппрувер + (для [`MODE_FILE`]) суд над
/// **внесённым** контентом. Для file-режима статус → `committed` означает
/// «контент применён И гейт зелёный»: правка уже на диске, повторное
/// [`evolve_apply`] не требуется (и не применяет контент дважды).
///
/// Порядок для [`MODE_FILE`] — «применить → судить → зафиксировать или
/// откатить»: сверка `base_sha256` (до любой записи) → сохранение исходного
/// состояния цели → атомарная запись предложенного контента → прогон гейтов
/// baseline-судьёй на применённом контенте. Красный гейт, сбой запуска судьи
/// или таймаут — **полный откат**, который выполняет эта функция (не
/// [`evolve_apply`]): цель возвращается к исходным байтам (или удаляется, если
/// её не было), статус остаётся `proposed`, в отчёте — хвост находок.
///
/// Для [`MODE_BLOCK`] поведение прежнее: гейт не исполняется, `gate`/`repo`
/// не влияют на результат, применение делает [`evolve_apply`].
///
/// # Errors
/// Предложение не найдено, без аппрувера, неполно, неизвестный режим,
/// защищённая цель, несовпадение `base_sha256`, отсутствует гейт/судья,
/// гейт красный или сбой отката.
pub fn evolve_commit(
    state_dir: &Path,
    id: &str,
    repo: &Path,
    gate: Option<&GatePlan>,
) -> Result<EvolutionProposal> {
    let p = evolve_find(state_dir, id)?;
    if p.approver.trim().is_empty() {
        return Err(HarnessError::Tool(format!(
            "evolve: {id} без именного аппрувера — коммит запрещён"
        )));
    }
    let mode = normalize_mode(&p.mode)?.to_string();
    let target = normalize_target(&p.target)?;
    ensure_writable_target(&target)?;

    // normalize_mode гарантирует ровно два режима: file (суд над внесённым
    // контентом) и block (managed-блок доменного слоя, гейт не исполняется).
    if mode == MODE_FILE {
        if p.base_sha256.as_deref().is_none_or(str::is_empty) {
            return Err(HarnessError::Tool(format!(
                "evolve: file-предложение {id} без base_sha256 — коммит запрещён"
            )));
        }
        if p.status == "committed" {
            // Идемпотентность финала: контент внесён и судим при первом
            // коммите; повторный вызов ничего не пишет и не судит заново.
            return Ok(p);
        }
        let gate = gate.ok_or_else(|| {
            HarnessError::Tool(format!(
                "evolve: {id} (mode=file) без гейта baseline-судьи — коммит запрещён (ADR-046)"
            ))
        })?;
        ensure_judge_outside(repo, gate.judge())?;
        return commit_file_with_gate(state_dir, repo, &p, &target, gate);
    }

    if p.block_id.trim().is_empty() {
        return Err(HarnessError::Tool(format!(
            "evolve: {id} неполон (target/block_id пуст)"
        )));
    }
    set_status(state_dir, id, "committed")?;
    let mut committed = p;
    committed.status = "committed".to_string();
    Ok(committed)
}

/// Исходное состояние цели до правки — материал полного отката.
#[derive(Debug, Clone)]
struct OriginalState {
    /// Байты файла; `None` — цели не было (откат удаляет созданный файл).
    bytes: Option<Vec<u8>>,
    /// Права файла до правки.
    perms: Option<std::fs::Permissions>,
}

/// Снимает исходное состояние цели ДО записи.
///
/// Не-UTF-8 цель отклоняется здесь же — до любой записи: откат пишет исходный
/// текст через [`crate::managed::atomic_write`], и побайтовое восстановление
/// произвольных байт протоколом не предусмотрено.
///
/// # Errors
/// Цель не читается или не является UTF-8.
fn capture_original(path: &Path) -> Result<OriginalState> {
    match std::fs::read(path) {
        Ok(bytes) => {
            if std::str::from_utf8(&bytes).is_err() {
                return Err(HarnessError::Tool(format!(
                    "evolve: цель {} не является UTF-8 — побайтовый откат невозможен, коммит запрещён",
                    path.display()
                )));
            }
            let perms = std::fs::metadata(path).ok().map(|m| m.permissions());
            Ok(OriginalState {
                bytes: Some(bytes),
                perms,
            })
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(OriginalState {
            bytes: None,
            perms: None,
        }),
        Err(e) => Err(HarnessError::io(path, e)),
    }
}

/// Восстанавливает исходное состояние цели атомарно: исходные байты (и права)
/// либо отсутствие файла.
///
/// # Errors
/// Ошибка атомарной записи/удаления или выставления прав.
fn restore_original(path: &Path, original: &OriginalState) -> Result<()> {
    match &original.bytes {
        Some(bytes) => {
            let text = std::str::from_utf8(bytes).map_err(|_| {
                HarnessError::Tool(format!(
                    "evolve: исходное содержимое {} не UTF-8 — откат невозможен",
                    path.display()
                ))
            })?;
            crate::managed::atomic_write(path, text)?;
            if let Some(perms) = &original.perms {
                std::fs::set_permissions(path, perms.clone())
                    .map_err(|e| HarnessError::io(path, e))?;
            }
            Ok(())
        }
        None => match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            // Файла нет — цель уже в исходном состоянии (отсутствует).
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(HarnessError::io(path, e)),
        },
    }
}

/// Отказ отката: дерево могло остаться с предложенным контентом.
fn rollback_failed(
    id: &str,
    target: &str,
    rollback: &HarnessError,
    cause: &HarnessError,
) -> HarnessError {
    HarnessError::Tool(format!(
        "evolve: {id}: отказ «{cause}», но откат {target} не удался ({rollback}) — \
         дерево может содержать предложенный контент, нужна ручная проверка"
    ))
}

/// Коммит file-предложения по порядку «применить → судить → зафиксировать или
/// откатить» (ADR-046 п.3).
///
/// Инвариант исхода: при `Ok` цель содержит ровно предложенный контент и статус
/// записан `committed`; при `Err` цель содержит ровно исходное состояние
/// (байты и права) либо отсутствует, а статус остаётся `proposed`.
fn commit_file_with_gate(
    state_dir: &Path,
    repo: &Path,
    p: &EvolutionProposal,
    target: &str,
    gate: &GatePlan,
) -> Result<EvolutionProposal> {
    let path = repo.join(target);
    let raw_base = p
        .base_sha256
        .as_deref()
        .ok_or_else(|| HarnessError::Tool(format!("evolve: {} без base_sha256", p.id)))?;
    let expected = Sha256::parse(raw_base)?;
    let actual = hash_target(&path)?;
    if actual != expected {
        return Err(HarnessError::Tool(format!(
            "evolve: {target} изменился после предложения (base_sha256: ожидался {}, на диске {}) — файл не тронут",
            expected.as_hex(),
            actual.as_hex()
        )));
    }

    let original = capture_original(&path)?;
    crate::managed::atomic_write(&path, &p.content)?;

    match run_gate(repo, gate) {
        Ok(()) => match set_status(state_dir, &p.id, "committed") {
            Ok(()) => {
                let mut committed = p.clone();
                committed.status = "committed".to_string();
                Ok(committed)
            }
            // Журнал не записан: возвращаем дерево к исходному состоянию,
            // иначе предложение осталось бы применённым при статусе proposed.
            Err(status_err) => match restore_original(&path, &original) {
                Ok(()) => Err(status_err),
                Err(rollback_err) => {
                    Err(rollback_failed(&p.id, target, &rollback_err, &status_err))
                }
            },
        },
        Err(gate_err) => match restore_original(&path, &original) {
            Ok(()) => Err(HarnessError::Tool(format!(
                "{gate_err}\nоткат: {target} восстановлен — дерево в исходном состоянии"
            ))),
            Err(rollback_err) => Err(rollback_failed(&p.id, target, &rollback_err, &gate_err)),
        },
    }
}

/// Отклоняет предложение (status=rejected).
///
/// # Errors
/// Предложение не найдено или журнал не перезаписывается.
pub fn evolve_reject(state_dir: &Path, id: &str) -> Result<()> {
    set_status(state_dir, id, "rejected")
}

/// Применяет committed-предложение к файлу.
///
/// [`MODE_BLOCK`] — managed-блок со сверкой хэша (прежнее поведение); это
/// единственный режим, где применение делает `evolve_apply`.
///
/// [`MODE_FILE`] — **явный отказ**: контент вносит и судит [`evolve_commit`]
/// (гейт судит внесённый контент), поэтому применять больше нечего. Молчаливый
/// no-op запрещён: вызывающий должен знать, что действие не выполнено, и не
/// ждать от `apply` записи. Ненулевой код возврата (`Err`) на CLI-поверхности
/// становится ненулевым кодом выхода.
///
/// # Errors
/// Предложение не committed (file), защищённая цель, ошибка применения
/// managed-блока; для file-режима — отказ с указанием шага `evolve commit`.
pub fn evolve_apply(repo: &Path, p: &EvolutionProposal) -> Result<()> {
    let mode = normalize_mode(&p.mode)?.to_string();
    let target = normalize_target(&p.target)?;
    ensure_writable_target(&target)?;
    match mode.as_str() {
        MODE_FILE => apply_file_mode(p),
        _ => apply_block_mode(repo, p, &target),
    }
}

/// Отказ `apply` для file-предложения: контент вносится на шаге коммита.
///
/// Проверка статуса идёт первой: непринятое предложение отклоняется отдельной
/// причиной («не committed»), а принятое — отсылкой к [`evolve_commit`].
fn apply_file_mode(p: &EvolutionProposal) -> Result<()> {
    if p.status != "committed" {
        return Err(HarnessError::Tool(format!(
            "evolve: {0} не committed — применение file-режима запрещено (гейт не пройден?)",
            p.id
        )));
    }
    Err(HarnessError::Tool(format!(
        "evolve: {} (mode=file): file-режим применяется на `evolve commit` \
         (гейт судит внесённый контент); повторное применение не требуется",
        p.id
    )))
}

/// Применение block-предложения через managed-блок (сверка хэша).
fn apply_block_mode(repo: &Path, p: &EvolutionProposal, target: &str) -> Result<()> {
    let target = repo.join(target);
    let patch = crate::managed::Patch {
        target,
        base_hash: crate::managed::HashGuard::Force,
        block_ops: vec![crate::managed::BlockOp::Replace {
            id: p.block_id.clone(),
            guard: crate::managed::HashGuard::Force,
            content: p.content.clone(),
            source: Some("evolve".into()),
            upsert: true,
            anchor: crate::managed::Anchor::Eof,
        }],
        section_ops: vec![],
    };
    // upsert с тем же телом — идемпотентный no-op, это не ошибка
    let _report = crate::managed::apply_file(&patch)?;
    Ok(())
}

fn append(state_dir: &Path, p: &EvolutionProposal) -> Result<PathBuf> {
    let path = evolve_dir(state_dir).join("proposals.jsonl");
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
    }
    let line = serde_json::to_string(p).map_err(HarnessError::Json)?;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| HarnessError::io(&path, e))?;
    writeln!(f, "{line}").map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Создаёт фейковый baseline-судья-скрипт с заданным кодом возврата.
    #[cfg(unix)]
    fn fake_judge(dir: &Path, code: i32) -> PathBuf {
        use std::os::unix::fs::PermissionsExt as _;
        let path = dir.join(format!("judge-{code}.sh"));
        std::fs::write(
            &path,
            format!("#!/bin/sh\necho \"судья: находка (код {code})\" >&2\nexit {code}\n"),
        )
        .expect("скрипт судьи");
        let mut perms = std::fs::metadata(&path).expect("meta").permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&path, perms).expect("chmod");
        path
    }

    /// Фейковый гейт: одна команда — судья с кодом возврата `code`.
    #[cfg(unix)]
    fn fake_gate(judge: &Path) -> GatePlan {
        GatePlan::with_commands(
            judge.to_path_buf(),
            vec![GateCommand {
                program: judge.to_path_buf(),
                args: vec![],
            }],
        )
    }

    /// Делает файл исполняемым.
    #[cfg(unix)]
    fn make_executable(path: &Path) {
        use std::os::unix::fs::PermissionsExt as _;
        let mut perms = std::fs::metadata(path).expect("meta").permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(path, perms).expect("chmod");
    }

    /// Фейковый baseline-судья, судящий ФАКТИЧЕСКИЙ контент цели: зелёный ровно
    /// тогда, когда в `target` (относительно каталога репозитория) есть/нет
    /// `needle`. `green_when_present = false` — обратный судья: он ловит прогон
    /// гейта на дереве ДО правки (там `needle` ещё нет).
    #[cfg(unix)]
    fn fake_content_judge(
        dir: &Path,
        target: &str,
        needle: &str,
        green_when_present: bool,
    ) -> PathBuf {
        let path = dir.join("judge-content.sh");
        let (hit, miss) = if green_when_present {
            (
                "exit 0".to_string(),
                format!("echo \"судья: в {target} нет '{needle}'\" >&2\nexit 1\n"),
            )
        } else {
            (
                format!("echo \"судья: в {target} уже есть '{needle}'\" >&2\nexit 1\n"),
                "exit 0".to_string(),
            )
        };
        std::fs::write(
            &path,
            format!(
                "#!/bin/sh\nif grep -q -- '{needle}' '{target}' 2>/dev/null; then\n{hit}\nfi\n{miss}\n"
            ),
        )
        .expect("скрипт судьи");
        make_executable(&path);
        path
    }

    /// Фейковый судья, который висит заведомо дольше таймаута гейта.
    #[cfg(unix)]
    fn hanging_judge(dir: &Path) -> PathBuf {
        let path = dir.join("judge-hang.sh");
        std::fs::write(&path, "#!/bin/sh\nsleep 30\nexit 0\n").expect("скрипт судьи");
        make_executable(&path);
        path
    }

    /// Имена файлов в каталоге (для проверки, что не осталось tmp-мусора).
    fn names_in(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(dir)
            .expect("read_dir")
            .map(|e| e.expect("entry").file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    /// Репозиторий-песочница с файлом `src/foo.rs`.
    fn repo_with_src(dir: &Path) -> PathBuf {
        let repo = dir.join("repo");
        std::fs::create_dir_all(repo.join("src")).expect("mkdir src");
        std::fs::write(repo.join("src/foo.rs"), "fn foo() {}\n").expect("write foo");
        repo
    }

    fn file_request(base: Option<&str>) -> ProposeRequest<'_> {
        ProposeRequest {
            id: "up-file",
            target: "src/foo.rs",
            mode: MODE_FILE,
            block_id: "",
            content: "fn foo() { /* v2 */ }\n",
            approver: "Иванов И.И.",
            base_sha256: base,
        }
    }

    fn status_of(state: &Path, id: &str) -> String {
        evolve_find(state, id).expect("найти").status
    }

    #[test]
    fn propose_commit_apply_roundtrip() {
        let dir = tempfile::tempdir().expect("tmp");
        let state = dir.path();
        let repo = dir.path();
        // целевой файл с managed-блоком
        let target = repo.join("aiml/skill.md");
        std::fs::create_dir_all(target.parent().expect("parent")).expect("mkdir");
        let body = "v1";
        let h = crate::managed::Sha256::of_text(body);
        let initial = crate::managed::render_block("doc", 1, &h, None, body);
        std::fs::write(&target, &initial).expect("write");

        let req = ProposeRequest {
            id: "up-1",
            target: "aiml/skill.md",
            mode: MODE_BLOCK,
            block_id: "doc",
            content: "v2",
            approver: "Иванов И.И.",
            base_sha256: None,
        };
        evolve_propose(state, repo, &req).expect("propose");
        let p = evolve_commit(state, "up-1", repo, None).expect("commit");
        assert_eq!(p.status, "committed");
        evolve_apply(repo, &p).expect("apply");
        let text = std::fs::read_to_string(&target).expect("read");
        assert!(text.contains("v2"));
    }

    #[test]
    fn propose_rejects_empty_approver() {
        let dir = tempfile::tempdir().expect("tmp");
        let req = ProposeRequest {
            id: "up-1",
            target: "a.md",
            mode: MODE_BLOCK,
            block_id: "doc",
            content: "x",
            approver: "  ",
            base_sha256: None,
        };
        let e = evolve_propose(dir.path(), dir.path(), &req).unwrap_err();
        assert!(e.to_string().contains("аппрувера"), "{e}");
    }

    #[test]
    fn commit_requires_approver() {
        let dir = tempfile::tempdir().expect("tmp");
        let req = ProposeRequest {
            id: "up-1",
            target: "a.md",
            mode: MODE_BLOCK,
            block_id: "doc",
            content: "x",
            approver: "Иванов",
            base_sha256: None,
        };
        evolve_propose(dir.path(), dir.path(), &req).expect("ok");
        // вручную стираем аппрувера (имитация пустого)
        let path = dir.path().join("evolve/proposals.jsonl");
        let text = std::fs::read_to_string(&path).expect("read");
        let broken = text.replace("Иванов", "");
        std::fs::write(&path, broken).expect("write");
        let e = evolve_commit(dir.path(), "up-1", dir.path(), None).unwrap_err();
        assert!(e.to_string().contains("аппрувера"), "{e}");
    }

    #[test]
    fn old_proposal_without_mode_reads_as_block() {
        let dir = tempfile::tempdir().expect("tmp");
        // строка журнала до ADR-046: без mode/base_sha256
        let legacy = r#"{"id":"old","target":"a.md","block_id":"doc","content":"x","approver":"Иванов","status":"proposed","at":"2026-01-01 00:00:00"}"#;
        std::fs::create_dir_all(dir.path().join("evolve")).expect("mkdir");
        std::fs::write(
            dir.path().join("evolve/proposals.jsonl"),
            format!("{legacy}\n"),
        )
        .expect("write");
        let p = evolve_find(dir.path(), "old").expect("найти");
        assert_eq!(p.mode, MODE_BLOCK);
        assert!(p.base_sha256.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn file_mode_without_approver_rejects_commit() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        // стираем аппрувера в журнале
        let path = state.join("evolve/proposals.jsonl");
        let text = std::fs::read_to_string(&path).expect("read");
        std::fs::write(&path, text.replace("Иванов И.И.", "")).expect("write");
        let judge = fake_judge(dir.path(), 0);
        let gate = fake_gate(&judge);
        let e = evolve_commit(&state, "up-file", &repo, Some(&gate)).unwrap_err();
        assert!(e.to_string().contains("аппрувера"), "{e}");
        assert_eq!(status_of(&state, "up-file"), "proposed");
        assert_eq!(
            std::fs::read_to_string(repo.join("src/foo.rs")).expect("read"),
            "fn foo() {}\n"
        );
    }

    /// Красный гейт после применения: полный откат к исходным байтам.
    ///
    /// Судья — обратный: он зелёный на дереве БЕЗ правки и красный на внесённом
    /// контенте. Прогон гейта до применения (прежний порядок) дал бы зелёный
    /// вердикт и пропустил регрессию.
    #[cfg(unix)]
    #[test]
    fn file_mode_red_gate_rolls_back_to_original() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        let judge = fake_content_judge(dir.path(), "src/foo.rs", "v2", false);
        let gate = fake_gate(&judge);
        let e = evolve_commit(&state, "up-file", &repo, Some(&gate)).unwrap_err();
        let text = e.to_string();
        assert!(text.contains("гейт не пройден"), "{text}");
        assert!(text.contains("уже есть 'v2'"), "хвост находок: {text}");
        assert_eq!(status_of(&state, "up-file"), "proposed");
        assert_eq!(
            std::fs::read(repo.join("src/foo.rs")).expect("read"),
            b"fn foo() {}\n",
            "красный гейт откатывает цель к исходным байтам"
        );
        assert_eq!(
            names_in(&repo.join("src")),
            vec!["foo.rs"],
            "tmp-мусора нет"
        );
    }

    #[cfg(unix)]
    #[test]
    fn file_mode_green_gate_commits_applied_content() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        // Судья смотрит сам файл: зелёный только потому, что правка уже внесена.
        let judge = fake_content_judge(dir.path(), "src/foo.rs", "v2", true);
        let gate = fake_gate(&judge);
        let p = evolve_commit(&state, "up-file", &repo, Some(&gate)).expect("commit");
        assert_eq!(p.status, "committed");
        assert_eq!(status_of(&state, "up-file"), "committed");
        assert_eq!(
            std::fs::read_to_string(repo.join("src/foo.rs")).expect("read"),
            "fn foo() { /* v2 */ }\n",
            "commit сам применяет контент: правка уже на диске"
        );
        assert_eq!(evolve_list(&state).expect("журнал").len(), 1);
        // Повторное применение — явный отказ: контент внесён коммитом, а не
        // молчаливый no-op (дельта F8). Файл при этом не трогается.
        let e = evolve_apply(&repo, &p).unwrap_err();
        assert!(
            e.to_string().contains("повторное применение не требуется"),
            "{e}"
        );
        assert_eq!(
            std::fs::read_to_string(repo.join("src/foo.rs")).expect("read"),
            "fn foo() { /* v2 */ }\n"
        );
    }

    /// Повторный commit уже committed-предложения не применяет контент дважды.
    #[cfg(unix)]
    #[test]
    fn file_mode_repeat_commit_does_not_apply_twice() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        let judge = fake_content_judge(dir.path(), "src/foo.rs", "v2", true);
        let gate = fake_gate(&judge);
        let first = evolve_commit(&state, "up-file", &repo, Some(&gate)).expect("commit");
        let second =
            evolve_commit(&state, "up-file", &repo, Some(&gate)).expect("повторный commit");
        assert_eq!(second.status, "committed");
        assert_eq!(second.content, first.content);
        assert_eq!(
            std::fs::read_to_string(repo.join("src/foo.rs")).expect("read"),
            "fn foo() { /* v2 */ }\n",
            "повторный commit не пишет контент заново (иначе base_sha256 не совпал бы)"
        );
    }

    /// Красный гейт на НОВОМ файле: после отказа цели нет.
    #[cfg(unix)]
    #[test]
    fn file_mode_red_gate_on_new_file_removes_it() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        let req = ProposeRequest {
            id: "up-new",
            target: "src/new.rs",
            mode: MODE_FILE,
            block_id: "",
            content: "fn new() { /* v2 */ }\n",
            approver: "Иванов И.И.",
            base_sha256: None,
        };
        evolve_propose(&state, &repo, &req).expect("propose");
        let judge = fake_content_judge(dir.path(), "src/new.rs", "v2", false);
        let gate = fake_gate(&judge);
        let e = evolve_commit(&state, "up-new", &repo, Some(&gate)).unwrap_err();
        assert!(e.to_string().contains("гейт не пройден"), "{e}");
        assert_eq!(status_of(&state, "up-new"), "proposed");
        assert!(
            !repo.join("src/new.rs").exists(),
            "созданная цель удаляется при откате"
        );
        assert_eq!(names_in(&repo.join("src")), vec!["foo.rs"]);
    }

    /// Судья не запускается (нет прав на исполнение) — тот же полный откат.
    #[cfg(unix)]
    #[test]
    fn file_mode_gate_spawn_failure_rolls_back() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        // Файл судьи есть и лежит вне дерева, но не исполняем: spawn падает.
        let judge = dir.path().join("judge-noexec.sh");
        std::fs::write(&judge, "#!/bin/sh\nexit 0\n").expect("write");
        let gate = fake_gate(&judge);
        let e = evolve_commit(&state, "up-file", &repo, Some(&gate)).unwrap_err();
        let text = e.to_string();
        assert!(text.contains("не запустился"), "{text}");
        assert_eq!(status_of(&state, "up-file"), "proposed");
        assert_eq!(
            std::fs::read(repo.join("src/foo.rs")).expect("read"),
            b"fn foo() {}\n",
            "сбой запуска судьи откатывает цель"
        );
    }

    /// Зависший судья — красный гейт (таймаут) и тот же полный откат.
    #[cfg(unix)]
    #[test]
    fn file_mode_gate_timeout_rolls_back() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        let judge = hanging_judge(dir.path());
        let gate = fake_gate(&judge).with_timeout(Duration::from_millis(300));
        let e = evolve_commit(&state, "up-file", &repo, Some(&gate)).unwrap_err();
        let text = e.to_string();
        assert!(text.contains("таймаут"), "{text}");
        assert_eq!(status_of(&state, "up-file"), "proposed");
        assert_eq!(
            std::fs::read(repo.join("src/foo.rs")).expect("read"),
            b"fn foo() {}\n",
            "таймаут гейта откатывает цель"
        );
    }

    /// Несовпадение `base_sha256` — отказ ДО любой записи, файл не тронут.
    #[cfg(unix)]
    #[test]
    fn file_mode_base_mismatch_refuses_commit_before_write() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        // файл изменился после предложения
        std::fs::write(repo.join("src/foo.rs"), "fn foo() { /* чужая правка */ }\n")
            .expect("write");
        let judge = fake_judge(dir.path(), 0);
        let gate = fake_gate(&judge);
        let e = evolve_commit(&state, "up-file", &repo, Some(&gate)).unwrap_err();
        assert!(e.to_string().contains("изменился после предложения"), "{e}");
        assert_eq!(status_of(&state, "up-file"), "proposed");
        assert_eq!(
            std::fs::read_to_string(repo.join("src/foo.rs")).expect("read"),
            "fn foo() { /* чужая правка */ }\n",
            "при несовпадении базы файл не трогается"
        );
    }

    /// File-режим без гейта baseline-судьи — отказ до записи.
    #[cfg(unix)]
    #[test]
    fn file_mode_without_gate_rejects_commit() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        let e = evolve_commit(&state, "up-file", &repo, None).unwrap_err();
        assert!(e.to_string().contains("без гейта"), "{e}");
        assert_eq!(status_of(&state, "up-file"), "proposed");
        assert_eq!(
            std::fs::read(repo.join("src/foo.rs")).expect("read"),
            b"fn foo() {}\n"
        );
    }

    /// Apply не трогает цель: явный отказ вместо затирания чужой правки.
    #[cfg(unix)]
    #[test]
    fn file_mode_apply_refuses_and_keeps_foreign_edit() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        let judge = fake_content_judge(dir.path(), "src/foo.rs", "v2", true);
        let gate = fake_gate(&judge);
        let p = evolve_commit(&state, "up-file", &repo, Some(&gate)).expect("commit");
        // чужая правка уже применённого файла
        std::fs::write(repo.join("src/foo.rs"), "fn foo() { /* чужая */ }\n").expect("write");
        let e = evolve_apply(&repo, &p).unwrap_err();
        assert!(
            e.to_string().contains("повторное применение не требуется"),
            "{e}"
        );
        assert_eq!(
            std::fs::read_to_string(repo.join("src/foo.rs")).expect("read"),
            "fn foo() { /* чужая */ }\n",
            "чужая правка не затирается"
        );
    }

    /// Дельта F8: `apply` для file-режима — явный отказ, а не молчаливый no-op.
    ///
    /// Контент внесён и судим [`evolve_commit`]; повторное применение не
    /// требуется, и файл после отказа остаётся ровно тем, что внёс коммит.
    #[cfg(unix)]
    #[test]
    fn file_mode_apply_is_explicit_refusal() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        let judge = fake_content_judge(dir.path(), "src/foo.rs", "v2", true);
        let gate = fake_gate(&judge);
        let p = evolve_commit(&state, "up-file", &repo, Some(&gate)).expect("commit");
        assert_eq!(p.status, "committed");
        let before = std::fs::read(repo.join("src/foo.rs")).expect("read");
        let e = evolve_apply(&repo, &p).unwrap_err();
        let text = e.to_string();
        assert!(
            text.contains("file-режим применяется на `evolve commit`"),
            "{text}"
        );
        assert!(text.contains("повторное применение не требуется"), "{text}");
        assert_eq!(
            std::fs::read(repo.join("src/foo.rs")).expect("read"),
            before,
            "отказ apply не меняет файл"
        );
    }

    /// Дельта F8: для block-режима `apply` работает как прежде.
    #[test]
    fn block_mode_apply_still_applies() {
        let dir = tempfile::tempdir().expect("tmp");
        let state = dir.path();
        let repo = dir.path();
        let target = repo.join("aiml/skill.md");
        std::fs::create_dir_all(target.parent().expect("parent")).expect("mkdir");
        let body = "v1";
        let h = crate::managed::Sha256::of_text(body);
        std::fs::write(
            &target,
            crate::managed::render_block("doc", 1, &h, None, body),
        )
        .expect("write");
        let req = ProposeRequest {
            id: "up-block",
            target: "aiml/skill.md",
            mode: MODE_BLOCK,
            block_id: "doc",
            content: "v2",
            approver: "Иванов И.И.",
            base_sha256: None,
        };
        evolve_propose(state, repo, &req).expect("propose");
        let p = evolve_commit(state, "up-block", repo, None).expect("commit");
        evolve_apply(repo, &p).expect("block-режим применяется прежним путём");
        assert!(
            std::fs::read_to_string(&target)
                .expect("read")
                .contains("v2"),
            "block-apply вносит новое тело блока"
        );
    }

    /// Регресс: `mode=block` гейт не исполняет (managed-блоки доменного слоя).
    #[test]
    fn block_mode_commit_ignores_gate() {
        let dir = tempfile::tempdir().expect("tmp");
        let state = dir.path();
        let repo = dir.path();
        let target = repo.join("aiml/skill.md");
        std::fs::create_dir_all(target.parent().expect("parent")).expect("mkdir");
        let body = "v1";
        let h = crate::managed::Sha256::of_text(body);
        let initial = crate::managed::render_block("doc", 1, &h, None, body);
        std::fs::write(&target, &initial).expect("write");

        let req = ProposeRequest {
            id: "up-block",
            target: "aiml/skill.md",
            mode: MODE_BLOCK,
            block_id: "doc",
            content: "v2",
            approver: "Иванов И.И.",
            base_sha256: None,
        };
        evolve_propose(state, repo, &req).expect("propose");
        // Гейт заведомо нерабочий: для block-режима он не запускается вообще.
        let program = PathBuf::from("/nonexistent/judge");
        let gate = GatePlan::with_commands(
            program.clone(),
            vec![GateCommand {
                program,
                args: vec![],
            }],
        );
        let p = evolve_commit(state, "up-block", repo, Some(&gate)).expect("commit");
        assert_eq!(p.status, "committed");
        evolve_apply(repo, &p).expect("apply");
        assert!(
            std::fs::read_to_string(&target)
                .expect("read")
                .contains("v2")
        );
    }

    #[cfg(unix)]
    #[test]
    fn file_mode_apply_requires_committed() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let state = dir.path().join("state");
        evolve_propose(&state, &repo, &file_request(None)).expect("propose");
        let p = evolve_find(&state, "up-file").expect("найти");
        assert_eq!(p.status, "proposed");
        let e = evolve_apply(&repo, &p).unwrap_err();
        assert!(e.to_string().contains("не committed"), "{e}");
        assert_eq!(
            std::fs::read_to_string(repo.join("src/foo.rs")).expect("read"),
            "fn foo() {}\n"
        );
    }

    #[test]
    fn protected_targets_are_refused() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = dir.path();
        for target in [
            "CONSTRAINTS.yaml",
            "docs/adr/ADR-046.md",
            "ARCHITECTURE-SPINE.md",
            "ARCHITECTURE-SPINE-BE.md",
        ] {
            let req = ProposeRequest {
                id: "up-prot",
                target,
                mode: MODE_FILE,
                block_id: "",
                content: "x",
                approver: "Иванов",
                base_sha256: Some("00"),
            };
            let e = evolve_propose(dir.path(), repo, &req).unwrap_err();
            assert!(e.to_string().contains("зона архитектора"), "{target}: {e}");
        }
    }

    #[cfg(unix)]
    #[test]
    fn judge_inside_repo_is_refused() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = repo_with_src(dir.path());
        let judge_in_repo = repo.join("src/judge.sh");
        std::fs::write(&judge_in_repo, "#!/bin/sh\nexit 0\n").expect("write");
        let e = ensure_judge_outside(&repo, &judge_in_repo).unwrap_err();
        assert!(e.to_string().contains("внутри проверяемого дерева"), "{e}");
        // судья вне дерева — ок
        let outside = fake_judge(dir.path(), 0);
        let resolved = ensure_judge_outside(&repo, &outside).expect("вне дерева");
        assert_eq!(resolved, outside.canonicalize().expect("canon"));
    }

    #[cfg(unix)]
    #[test]
    fn resolve_judge_prefers_explicit_and_refuses_missing() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).expect("mkdir");
        let outside = fake_judge(dir.path(), 0);
        let resolved = resolve_judge(Some(&outside), None, &repo).expect("явный судья");
        assert_eq!(resolved, outside.canonicalize().expect("canon"));
        let e = resolve_judge(None, None, &repo).unwrap_err();
        assert!(e.to_string().contains("не задан"), "{e}");
    }

    #[test]
    fn gate_without_commands_is_refused() {
        let dir = tempfile::tempdir().expect("tmp");
        let gate = GatePlan::with_commands(PathBuf::from("/nonexistent"), vec![]);
        let e = run_gate(dir.path(), &gate).unwrap_err();
        assert!(e.to_string().contains("без команд"), "{e}");
    }
}
