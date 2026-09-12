//! Репетиция отката (rollback rehearsal) handoff-пакета — механическая часть
//! гейта A4 (conformance evidence) по мотивам AI-native SDLC: rollback должен
//! быть «самым отрепетированным путём», а не импровизацией на инциденте.
//!
//! КОНТРАКТ (владелец: агент `control`):
//! - план отката — машиночитаемый `ROLLBACK.yaml` в пакете `.arch-handoff/`:
//!   `baseline_commit` (якорь) + шаги `steps[].run` (shell) + опциональная
//!   финальная верификация `verify`; генерируется `generate_handoff`
//!   (заготовка, не затирается), переписывается архитектором под эпик;
//! - [`rehearse`] прогоняет шаги во ВРЕМЕННОМ git-worktree на `baseline_commit`
//!   (detached): рабочее дерево и история основного репозитория не трогаются.
//!   Шаги с внешними/деструктивными/недетерминированными эффектами
//!   (denylist [`DENIED_PATTERNS`]) отклоняются с диагностикой — их место не
//!   в репетируемом плане; шаги выполняются fail-fast, как в боевом откате;
//! - результат (PASS/FAIL + лог шагов) пишется в evidence пакета —
//!   `REHEARSAL.json`; [`gate_a4`] оценивает гейт: маршрутам не ниже порога
//!   [`RehearsalRequirement`] (дефолт — только Critical) нужна свежая успешная
//!   репетиция, иначе гейт FAIL (exit 1 в CLI).
//!
//! ОГРАНИЧЕНИЕ: репетиция проверяет ПРИМЕНИМОСТЬ шагов на baseline (якорь
//! резолвится, команды завершаются нулём, дерево возвращается чистым), а не
//! фактический откат коммита исполнителя — его в момент гейта ещё не
//! существует. Отказано/пропущено — только декларативная диагностика.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::control::Route;
use crate::error::{HarnessError, Result};

/// Имя файла машиночитаемого плана отката в пакете.
pub const ROLLBACK_FILE: &str = "ROLLBACK.yaml";

/// Имя файла evidence репетиции в пакете.
pub const REHEARSAL_FILE: &str = "REHEARSAL.json";

/// Таймаут одного шага репетиции, секунд: шаги отката — короткие
/// верификационные команды; зависший шаг — сам по себе находка.
const STEP_TIMEOUT_SECS: u64 = 60;

/// Хвост вывода шага, попадающий в лог (символов): начало вывода важно редко,
/// диагностика — в конце.
const STEP_LOG_TAIL: usize = 2000;

/// Запрещённые в репетиции паттерны шагов: (regex, причина отказа).
/// Репетиция — проверка применимости в одноразовом worktree, поэтому
/// `git reset --hard <baseline>` внутри него безопасен и разрешён; отклоняются
/// шаги, чей эффект уходит ЗА пределы worktree (сеть, remote, инфраструктура,
/// процессы хоста) или недетерминирован.
const DENIED_PATTERNS: &[(&str, &str)] = &[
    (r"\bgit\s+push\b", "push на remote — внешний эффект"),
    (
        r"\bgit\s+clean\b",
        "git clean — необратимое удаление untracked-файлов",
    ),
    // Флаг — отдельный токен из букв (`rm -rf x`, `rm -r -f x`); дефис ВНУТРИ
    // имени файла (`rm obsolete-marker.txt`) флагом не считается.
    (
        r"\brm(\s+-[a-zA-Z]+)+(\s|$)",
        "rm с флагами (-r/-f/…) — потенциально массовое удаление",
    ),
    (
        r"\b(curl|wget|ssh|scp|sftp|rsync|nc|telnet)\b",
        "сетевая команда — недетерминизм и внешний эффект",
    ),
    (r"\b(sudo|doas)\b", "эскалация прав на хосте"),
    (
        r"\b(kill|pkill|killall|shutdown|reboot|systemctl)\b",
        "управление процессами/сервисами хоста",
    ),
    (
        r"\b(docker|podman|kubectl|helm|terraform)\b",
        "изменение внешней инфраструктуры",
    ),
    (
        r"\b(npm|yarn|pnpm|cargo|pip|twine|gem)\s+(publish|push|upload)\b",
        "публикация артефакта наружу",
    ),
    (r"(?i)\bdrop\s+(database|table|schema)\b", "удаление данных"),
];

/// Шаг плана отката.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RollbackStep {
    /// Человеко-читаемое имя шага.
    pub name: String,
    /// Shell-команда (`bash -c`), выполняемая в репетиционном worktree.
    pub run: String,
}

/// Машиночитаемый план отката (`ROLLBACK.yaml` пакета).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RollbackPlan {
    /// Якорь отката: коммит, на который откатываемся (обязателен для Critical).
    #[serde(default)]
    pub baseline_commit: String,
    /// Шаги отката (обязателен хотя бы один для Critical).
    #[serde(default)]
    pub steps: Vec<RollbackStep>,
    /// Опциональная финальная верификация состояния после всех шагов
    /// (должна завершиться кодом 0).
    #[serde(default)]
    pub verify: Option<String>,
}

/// Итог одного шага репетиции.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StepStatus {
    /// Команда завершилась кодом 0 до таймаута.
    Pass,
    /// Команда завершилась ненулевым кодом или по таймауту.
    Fail,
    /// Шаг отклонён до выполнения (denylist: внешний/деструктивный эффект).
    Refused,
}

/// Отчёт о шаге.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StepReport {
    /// Имя шага.
    pub name: String,
    /// Итог.
    pub status: StepStatus,
    /// Код возврата (None — таймаут/отказ до запуска).
    pub exit_code: Option<i32>,
    /// Диагностика: причина отказа либо хвост вывода команды.
    pub detail: String,
}

/// Отчёт репетиции (`REHEARSAL.json` в пакете — evidence гейта A4).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RehearsalReport {
    /// Вид evidence (`rollback_rehearsal`).
    pub kind: String,
    /// Гейт (`A4`).
    pub gate: String,
    /// Все шаги и verify прошли.
    pub passed: bool,
    /// Якорь отката, на котором репетировали.
    pub baseline_commit: String,
    /// Метка времени репетиции (UTC, RFC 3339).
    pub rehearsed_at: String,
    /// Длительность, секунды.
    pub duration_secs: f64,
    /// Отчёты шагов (fail-fast: после первого FAIL/REFUSED шаги не запускались).
    pub steps: Vec<StepReport>,
    /// Отчёт финальной верификации (None — verify не задана или не дошли).
    pub verify: Option<StepReport>,
    /// Человеко-читаемый лог (по строке на событие).
    pub log: Vec<String>,
}

/// Порог обязательности репетиции по маршруту значимости.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RehearsalRequirement {
    /// Репетиция нигде не обязательна (advisory).
    Never,
    /// Обязательна для маршрутов не ниже заданного.
    AtLeast(Route),
}

impl std::str::FromStr for RehearsalRequirement {
    type Err = String;

    /// Парсит порог: `never` | `fast` | `standard` | `critical`.
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "never" => Ok(Self::Never),
            other => Ok(Self::AtLeast(other.parse::<Route>()?)),
        }
    }
}

impl std::fmt::Display for RehearsalRequirement {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Never => f.write_str("never"),
            Self::AtLeast(route) => write!(f, "{route}"),
        }
    }
}

impl RehearsalRequirement {
    /// Требуется ли репетиция для маршрута: `never` — никому, иначе маршрутам
    /// не ниже порога (Fast=0 < Standard=1 < Critical=2).
    #[must_use]
    pub fn requires(&self, route: Route) -> bool {
        fn level(route: Route) -> u8 {
            match route {
                Route::Fast => 0,
                Route::Standard => 1,
                Route::Critical => 2,
            }
        }
        match self {
            Self::Never => false,
            Self::AtLeast(threshold) => level(route) >= level(*threshold),
        }
    }
}

/// Вердикт гейта A4.
#[derive(Debug, Clone)]
pub struct GateVerdict {
    /// Гейт пройден.
    pub passed: bool,
    /// Сводка для отчёта/CLI.
    pub summary: String,
}

/// Локализует handoff-пакет по пути: сам каталог пакета (есть MANIFEST.json)
/// или корень репозитория (ищем `.arch-handoff/`). Возвращает (репозиторий,
/// каталог пакета).
///
/// # Errors
/// Пакет не найден ни в одном из видов.
pub fn locate_packet(path: &Path) -> Result<(PathBuf, PathBuf)> {
    if path.join("MANIFEST.json").is_file() {
        let repo = path.parent().map(Path::to_path_buf).ok_or_else(|| {
            HarnessError::Control(format!("у пакета {} нет родителя", path.display()))
        })?;
        return Ok((repo, path.to_path_buf()));
    }
    let packet = path.join(".arch-handoff");
    if packet.is_dir() {
        return Ok((path.to_path_buf(), packet));
    }
    Err(HarnessError::Control(format!(
        "{}: ни handoff-пакет (нет MANIFEST.json), ни репозиторий с .arch-handoff/ — \
         соберите пакет: arch handoff <harness> --repo <path> --task <text>",
        path.display()
    )))
}

/// Читает план отката `ROLLBACK.yaml` из пакета.
///
/// # Errors
/// Файл отсутствует или невалиден (синтаксис/схема — ошибка оператора, не FAIL).
pub fn load_plan(packet_dir: &Path) -> Result<RollbackPlan> {
    let path = packet_dir.join(ROLLBACK_FILE);
    let text = std::fs::read_to_string(&path).map_err(|e| {
        HarnessError::Control(format!(
            "{}: план отката не читается ({e}); пакеты старых версий без {ROLLBACK_FILE} \
             обновите повторной генерацией handoff",
            path.display()
        ))
    })?;
    Ok(serde_yaml_ng::from_str(&text)?)
}

/// Читает маршрут значимости из `MANIFEST.json` пакета.
///
/// # Errors
/// Манифест отсутствует/невалиден, поле `route` неизвестно.
pub fn packet_route(packet_dir: &Path) -> Result<Route> {
    #[derive(Deserialize)]
    struct ManifestMeta {
        route: String,
    }
    let path = packet_dir.join("MANIFEST.json");
    let text = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
    let meta: ManifestMeta = serde_json::from_str(&text).map_err(|e| {
        HarnessError::Control(format!("{}: невалидный MANIFEST.json: {e}", path.display()))
    })?;
    meta.route
        .parse()
        .map_err(|e: String| HarnessError::Control(format!("{}: {e}", path.display())))
}

/// Читает evidence репетиции (`REHEARSAL.json`): None — репетиции ещё не было.
///
/// # Errors
/// Файл есть, но не разбирается (подмена/дрейф evidence не замалчивается).
pub fn load_report(packet_dir: &Path) -> Result<Option<RehearsalReport>> {
    let path = packet_dir.join(REHEARSAL_FILE);
    if !path.is_file() {
        return Ok(None);
    }
    let text = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
    let report = serde_json::from_str(&text).map_err(|e| {
        HarnessError::Control(format!(
            "{}: невалидный {REHEARSAL_FILE}: {e}",
            path.display()
        ))
    })?;
    Ok(Some(report))
}

/// Оценивает гейт A4: маршруту, покрытому порогом `requirement`, нужна
/// успешная и СВЕЖАЯ репетиция (baseline в evidence совпадает с текущим
/// `plan_baseline` — план, изменённый после репетиции, обесценивает её).
#[must_use]
pub fn gate_a4(
    route: Route,
    requirement: RehearsalRequirement,
    plan_baseline: Option<&str>,
    evidence: Option<&RehearsalReport>,
) -> GateVerdict {
    if !requirement.requires(route) {
        let note = match evidence {
            Some(r) if r.passed => "репетиция пройдена (advisory)",
            Some(_) => "репетиция FAILED (advisory — не блокирует маршрут)",
            None => "репетиции не было (advisory)",
        };
        return GateVerdict {
            passed: true,
            summary: format!(
                "A4: маршрут {route} ниже порога {requirement} — репетиция не обязательна; {note}"
            ),
        };
    }
    let Some(report) = evidence else {
        return GateVerdict {
            passed: false,
            summary: format!(
                "A4: маршрут {route} требует репетиции отката (порог {requirement}), \
                 а evidence нет — прогоните: arch control gate A4 <repo> --rehearse"
            ),
        };
    };
    if !report.passed {
        return GateVerdict {
            passed: false,
            summary: format!(
                "A4: последняя репетиция отката FAILED ({}) — план неприменим, \
                 исправьте {ROLLBACK_FILE} и прогоните заново",
                report.rehearsed_at
            ),
        };
    }
    if let Some(baseline) = plan_baseline {
        if !baseline.is_empty() && baseline != report.baseline_commit {
            return GateVerdict {
                passed: false,
                summary: format!(
                    "A4: evidence устарело — план отката изменился после репетиции \
                     (baseline {baseline} ≠ {}); прогоните репетицию заново",
                    report.baseline_commit
                ),
            };
        }
    }
    GateVerdict {
        passed: true,
        summary: format!(
            "A4: маршрут {route} — откат отрепетирован на {} ({})",
            report.baseline_commit, report.rehearsed_at
        ),
    }
}

/// Прогоняет репетицию отката: шаги `ROLLBACK.yaml` выполняются во временном
/// git-worktree на `baseline_commit` (detached), результат пишется в
/// `REHEARSAL.json` пакета (evidence гейта A4).
///
/// Проблемы СОДЕРЖАНИЯ плана (нет baseline/шагов, битый якорь, отказ denylist,
/// ненулевой выход шага) — это FAIL-отчёт (фиксируется в evidence), а не
/// ошибка; `Err` — только операторские сбои (план/манифест не читаются,
/// git/worktree недоступны, запись evidence не удалась).
///
/// # Errors
/// См. выше + сбой записи REHEARSAL.json.
pub fn rehearse(repo: &Path, packet_dir: &Path) -> Result<RehearsalReport> {
    let started = Instant::now();
    let plan = load_plan(packet_dir)?;
    let baseline = plan.baseline_commit.trim().to_string();
    let mut steps: Vec<StepReport> = Vec::new();
    let mut log: Vec<String> = Vec::new();

    // Макрос-финализатор: собирает отчёт, пишет evidence и возвращает его.
    macro_rules! finish {
        ($passed:expr, $verify:expr) => {{
            let report = RehearsalReport {
                kind: "rollback_rehearsal".into(),
                gate: "A4".into(),
                passed: $passed,
                baseline_commit: baseline.clone(),
                rehearsed_at: chrono::Utc::now().to_rfc3339(),
                duration_secs: started.elapsed().as_secs_f64(),
                steps,
                verify: $verify,
                log,
            };
            write_report(packet_dir, &report)?;
            return Ok(report);
        }};
    }

    if baseline.is_empty() {
        log.push("FAIL: план без baseline_commit — откату не за что зацепиться".into());
        finish!(false, None);
    }
    if plan.steps.is_empty() {
        log.push("FAIL: план без шагов — репетировать нечего".into());
        finish!(false, None);
    }

    // Якорь обязан резолвиться в коммит репозитория.
    match git_out(repo, &["cat-file", "-t", &baseline]) {
        Some(kind) if kind.trim() == "commit" => {}
        _ => {
            log.push(format!(
                "FAIL: baseline_commit '{baseline}' не резолвится в коммит репозитория {}",
                repo.display()
            ));
            finish!(false, None);
        }
    }

    // Одноразовый worktree на baseline: все шаги — внутри него, основное
    // дерево и история репозитория не трогаются.
    let wt_dir = std::env::temp_dir().join(format!(
        "arch-rehearsal-{}-{}",
        std::process::id(),
        started.elapsed().as_nanos()
    ));
    if let Some(err) = git_err(
        repo,
        &[
            "worktree",
            "add",
            "--detach",
            &wt_dir.to_string_lossy(),
            &baseline,
        ],
    ) {
        return Err(HarnessError::Control(format!(
            "не удалось создать репетиционный worktree: {err}"
        )));
    }
    let _guard = WorktreeGuard {
        repo: repo.to_path_buf(),
        dir: wt_dir.clone(),
    };
    log.push(format!(
        "репетиционный worktree: {} (detached на {baseline})",
        wt_dir.display()
    ));

    // Fail-fast: после первого FAIL/REFUSED шаги не запускаются — как в
    // боевом откате, где продолжать процедуру после сбоя шага нельзя.
    let mut all_pass = true;
    for step in &plan.steps {
        let report = match screen_step(&step.run)? {
            Some(reason) => StepReport {
                name: step.name.clone(),
                status: StepStatus::Refused,
                exit_code: None,
                detail: format!(
                    "шаг не репетируется: {reason}. Перепишите шаг верификационной \
                     командой (dry-run/проверка) или вынесите его из плана"
                ),
            },
            None => run_step(&wt_dir, &step.name, &step.run)?,
        };
        log.push(format!(
            "[{}] {} — {}",
            match report.status {
                StepStatus::Pass => "PASS",
                StepStatus::Fail => "FAIL",
                StepStatus::Refused => "REFUSED",
            },
            report.name,
            report.detail.lines().next().unwrap_or_default()
        ));
        if report.status != StepStatus::Pass {
            all_pass = false;
            let remaining = plan.steps.len() - steps.len() - 1;
            if remaining > 0 {
                log.push(format!(
                    "оставшиеся шаги не запускались (fail-fast, как в боевом откате): {remaining}"
                ));
            }
            steps.push(report);
            break;
        }
        steps.push(report);
    }

    // Финальная верификация — только когда все шаги прошли.
    let mut verify_report = None;
    if all_pass {
        if let Some(verify) = plan
            .verify
            .as_deref()
            .map(str::trim)
            .filter(|v| !v.is_empty())
        {
            let report = match screen_step(verify)? {
                Some(reason) => StepReport {
                    name: "verify".into(),
                    status: StepStatus::Refused,
                    exit_code: None,
                    detail: format!("verify не репетируется: {reason}"),
                },
                None => run_step(&wt_dir, "verify", verify)?,
            };
            log.push(format!(
                "[{}] verify — {}",
                if report.status == StepStatus::Pass {
                    "PASS"
                } else {
                    "FAIL"
                },
                report.detail.lines().next().unwrap_or_default()
            ));
            if report.status != StepStatus::Pass {
                all_pass = false;
            }
            verify_report = Some(report);
        }
    }

    finish!(all_pass, verify_report);
}

/// Записывает отчёт репетиции в evidence пакета (`REHEARSAL.json`).
fn write_report(packet_dir: &Path, report: &RehearsalReport) -> Result<()> {
    let path = packet_dir.join(REHEARSAL_FILE);
    let text = serde_json::to_string_pretty(report)?;
    std::fs::write(&path, format!("{text}\n")).map_err(|e| HarnessError::io(&path, e))
}

/// Проверяет шаг по denylist: `Some(причина)` — шаг отклоняется до запуска.
///
/// # Errors
/// Внутренний regex не скомпилировался (дефект констант, не ввода).
fn screen_step(command: &str) -> Result<Option<String>> {
    for (pattern, reason) in DENIED_PATTERNS {
        let re = regex::Regex::new(pattern).map_err(|e| {
            HarnessError::Control(format!("внутренний regex репетиции '{pattern}': {e}"))
        })?;
        if re.is_match(command) {
            return Ok(Some((*reason).to_string()));
        }
    }
    Ok(None)
}

/// Выполняет шаг в репетиционном worktree: `bash -c` с таймаутом
/// [`STEP_TIMEOUT_SECS`]; вывод (stdout+stderr) уходит в лог-файл внутри
/// одноразового worktree (не в pipe — без риска дедлока на буфере), в отчёт
/// попадает хвост [`STEP_LOG_TAIL`] символов.
fn run_step(dir: &Path, name: &str, command: &str) -> Result<StepReport> {
    let log_file = dir.join(".arch-rehearsal-step.log");
    let out = std::fs::File::create(&log_file).map_err(|e| HarnessError::io(&log_file, e))?;
    let err = out
        .try_clone()
        .map_err(|e| HarnessError::io(&log_file, e))?;
    let mut child = Command::new("bash")
        .arg("-c")
        .arg(command)
        .current_dir(dir)
        .stdin(Stdio::null())
        .stdout(Stdio::from(out))
        .stderr(Stdio::from(err))
        .spawn()
        .map_err(|e| HarnessError::Control(format!("не удалось запустить bash: {e}")))?;
    let started = Instant::now();
    let timeout = Duration::from_secs(STEP_TIMEOUT_SECS);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {
                if started.elapsed() >= timeout {
                    let _ = child.kill();
                    let _ = child.wait(); // забрать зомби
                    break None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(HarnessError::Control(format!(
                    "ошибка ожидания шага '{name}': {e}"
                )));
            }
        }
    };
    let output = std::fs::read_to_string(&log_file).unwrap_or_default();
    let tail: String = output
        .chars()
        .rev()
        .take(STEP_LOG_TAIL)
        .collect::<Vec<char>>()
        .into_iter()
        .rev()
        .collect();
    let detail = if tail.trim().is_empty() {
        "команда завершилась без вывода".to_string()
    } else {
        tail.trim().to_string()
    };
    match status {
        Some(s) if s.success() => Ok(StepReport {
            name: name.into(),
            status: StepStatus::Pass,
            exit_code: s.code(),
            detail,
        }),
        Some(s) => Ok(StepReport {
            name: name.into(),
            status: StepStatus::Fail,
            exit_code: s.code(),
            detail: format!(
                "команда завершилась неуспешно ({}): {detail}",
                s.code()
                    .map_or_else(|| "сигнал".to_string(), |c| format!("код {c}"))
            ),
        }),
        None => Ok(StepReport {
            name: name.into(),
            status: StepStatus::Fail,
            exit_code: None,
            detail: format!("шаг превысил таймаут {STEP_TIMEOUT_SECS}s и был убит: {detail}"),
        }),
    }
}

/// stdout git-команды в репозитории; None — ненулевой выход.
fn git_out(repo: &Path, args: &[&str]) -> Option<String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// stderr git-команды при ненулевом выходе; None — команда успешна.
fn git_err(repo: &Path, args: &[&str]) -> Option<String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .ok()?;
    if out.status.success() {
        return None;
    }
    Some(
        String::from_utf8_lossy(&out.stderr)
            .trim()
            .chars()
            .take(300)
            .collect(),
    )
}

/// Уборка репетиционного worktree при выходе из области (в т.ч. при `?`):
/// `worktree remove --force` (в worktree могли остаться изменения от шагов —
/// это одноразовая копия); при сбое — каталог вручную + `worktree prune`.
struct WorktreeGuard {
    repo: PathBuf,
    dir: PathBuf,
}

impl Drop for WorktreeGuard {
    fn drop(&mut self) {
        let dir = self.dir.to_string_lossy().into_owned();
        if git_err(&self.repo, &["worktree", "remove", "--force", &dir]).is_some() {
            // remove не удался (каталог уже убран/повреждён) — убираем каталог
            // сами и чистим админ-запись git, чтобы реестр worktree не гнил.
            let _ = std::fs::remove_dir_all(&self.dir);
            let _ = git_out(&self.repo, &["worktree", "prune"]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// git в каталоге с тестовой идентичностью коммиттера.
    fn git_in(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(args)
            .env("GIT_AUTHOR_NAME", "t")
            .env("GIT_AUTHOR_EMAIL", "t@t")
            .env("GIT_COMMITTER_NAME", "t")
            .env("GIT_COMMITTER_EMAIL", "t@t")
            .output()
            .expect("git");
        assert!(
            out.status.success(),
            "git {}: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    /// Репозиторий-фикстура: git init + один коммит; возвращает хеш baseline.
    fn make_repo(dir: &Path) -> String {
        std::fs::create_dir_all(dir).expect("mkdir");
        git_in(dir, &["init", "-q", "-b", "main"]);
        std::fs::write(dir.join("README.md"), "base\n").expect("write");
        git_in(dir, &["add", "."]);
        git_in(dir, &["commit", "-q", "-m", "baseline"]);
        git_in(dir, &["rev-parse", "HEAD"]).trim().to_string()
    }

    /// Пакет-фикстура: `.arch-handoff/` с MANIFEST.json и ROLLBACK.yaml.
    fn write_packet(repo: &Path, route: &str, plan_yaml: &str) -> PathBuf {
        let packet = repo.join(".arch-handoff");
        std::fs::create_dir_all(&packet).expect("mkdir packet");
        std::fs::write(
            packet.join("MANIFEST.json"),
            format!("{{\"route\": \"{route}\"}}\n"),
        )
        .expect("manifest");
        std::fs::write(packet.join(ROLLBACK_FILE), plan_yaml).expect("plan");
        packet
    }

    /// План-фикстура: проверка якоря + откат на baseline + verify чистоты.
    fn safe_plan(baseline: &str) -> String {
        format!(
            "baseline_commit: \"{baseline}\"\n\
             steps:\n\
             \x20 - name: якорь-доступен\n\
             \x20   run: git cat-file -t {baseline}\n\
             \x20 - name: откат-на-baseline\n\
             \x20   run: git reset --hard {baseline}\n\
             verify: test -z \"$(git status --porcelain --untracked-files=no)\"\n"
        )
    }

    #[test]
    fn rehearsal_passes_on_safe_plan_and_leaves_repo_untouched() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        let baseline = make_repo(&repo);
        let packet = write_packet(&repo, "Critical", &safe_plan(&baseline));

        let report = rehearse(&repo, &packet).expect("rehearse");
        assert!(report.passed, "лог: {:?}", report.log);
        assert_eq!(report.steps.len(), 2);
        assert!(
            report
                .steps
                .iter()
                .all(|s| s.status == StepStatus::Pass && s.exit_code == Some(0))
        );
        assert_eq!(
            report.verify.as_ref().map(|v| v.status),
            Some(StepStatus::Pass)
        );
        // Evidence записан и перечитывается.
        let loaded = load_report(&packet).expect("load").expect("evidence");
        assert!(loaded.passed);
        assert_eq!(loaded.baseline_commit, baseline);
        // Основной репозиторий не тронут: HEAD тот же; в status — только
        // untracked-пакет `.arch-handoff/` (он никогда не коммитится).
        assert_eq!(git_in(&repo, &["rev-parse", "HEAD"]).trim(), baseline);
        let status = git_in(&repo, &["status", "--porcelain"]);
        assert!(
            status
                .lines()
                .all(|l| l.contains(".arch-handoff/") && l.starts_with("??")),
            "посторонний след репетиции: {status}"
        );
        let worktrees = git_in(&repo, &["worktree", "list", "--porcelain"]);
        assert_eq!(
            worktrees.matches("worktree ").count(),
            1,
            "остался лишний worktree: {worktrees}"
        );
    }

    #[test]
    fn rehearsal_refuses_destructive_step_with_diagnostics() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        let baseline = make_repo(&repo);
        let plan = format!(
            "baseline_commit: \"{baseline}\"\n\
             steps:\n\
             \x20 - name: снести-всё\n\
             \x20   run: rm -rf README.md\n"
        );
        let packet = write_packet(&repo, "Critical", &plan);
        let report = rehearse(&repo, &packet).expect("rehearse");
        assert!(!report.passed);
        assert_eq!(report.steps.len(), 1);
        let step = &report.steps[0];
        assert_eq!(step.status, StepStatus::Refused);
        assert!(step.detail.contains("массовое удаление"), "{step:?}");
        assert!(step.detail.contains("не репетируется"), "{step:?}");
        // Файл в основном репозитории цел (шаг не выполнялся нигде).
        assert!(repo.join("README.md").is_file());
        // FAIL зафиксирован в evidence.
        assert!(
            !load_report(&packet)
                .expect("load")
                .expect("evidence")
                .passed
        );
    }

    #[test]
    fn rehearsal_fails_on_failing_step_and_stops() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        let baseline = make_repo(&repo);
        let plan = format!(
            "baseline_commit: \"{baseline}\"\n\
             steps:\n\
             \x20 - name: нет-такой-команды\n\
             \x20   run: definitely-missing-cmd-xyz\n\
             \x20 - name: не-должен-запуститься\n\
             \x20   run: touch MARKER\n"
        );
        let packet = write_packet(&repo, "Critical", &plan);
        let report = rehearse(&repo, &packet).expect("rehearse");
        assert!(!report.passed);
        assert_eq!(report.steps.len(), 1, "fail-fast: второй шаг не запускался");
        assert_eq!(report.steps[0].status, StepStatus::Fail);
        assert!(
            report.steps[0].detail.contains("неуспешно"),
            "{:?}",
            report.steps[0]
        );
        assert!(report.verify.is_none(), "verify при провале шага не идёт");
        // Побочек на основном репо нет (MARKER не появился).
        assert!(!repo.join("MARKER").exists());
    }

    #[test]
    fn rehearsal_fails_on_unknown_baseline_and_empty_plan() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        make_repo(&repo);
        // Якорь не резолвится в коммит.
        let plan = "baseline_commit: \"deadbeefdeadbeef\"\nsteps:\n  - name: x\n    run: 'true'\n";
        let packet = write_packet(&repo, "Critical", plan);
        let report = rehearse(&repo, &packet).expect("rehearse");
        assert!(!report.passed);
        assert!(report.steps.is_empty(), "до шагов не дошли");
        assert!(
            report.log.iter().any(|l| l.contains("не резолвится")),
            "{:?}",
            report.log
        );
        // План без baseline и без шагов.
        let packet2 = write_packet(&repo, "Critical", "baseline_commit: \"\"\nsteps: []\n");
        let report = rehearse(&repo, &packet2).expect("rehearse");
        assert!(!report.passed);
        assert!(report.log.iter().any(|l| l.contains("baseline_commit")));
    }

    #[test]
    fn rehearsal_runs_verify_and_catches_dirty_state() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        let baseline = make_repo(&repo);
        // Шаг оставляет грязное дерево — verify это ловит.
        let plan = format!(
            "baseline_commit: \"{baseline}\"\n\
             steps:\n\
             \x20 - name: правка-без-отката\n\
             \x20   run: echo changed >> README.md\n\
             verify: test -z \"$(git status --porcelain --untracked-files=no)\"\n"
        );
        let packet = write_packet(&repo, "Critical", &plan);
        let report = rehearse(&repo, &packet).expect("rehearse");
        assert!(!report.passed, "verify обязан поймать грязное дерево");
        assert_eq!(
            report.verify.as_ref().map(|v| v.status),
            Some(StepStatus::Fail)
        );
        // Основное дерево чистое (кроме untracked-пакета) — правка была
        // только в репетиционном worktree.
        let status = git_in(&repo, &["status", "--porcelain"]);
        assert!(
            status
                .lines()
                .all(|l| l.contains(".arch-handoff/") && l.starts_with("??")),
            "посторонний след репетиции: {status}"
        );
    }

    #[test]
    fn denylist_covers_outward_and_host_effects() {
        for (cmd, why) in [
            ("git push origin main", "push"),
            ("git clean -fdx", "clean"),
            ("rm -rf build/", "rm -r"),
            ("rm -f tmp.txt", "rm -f"),
            ("curl -X DELETE https://api/internal", "сеть"),
            ("ssh deploy@host systemctl restart app", "ssh"),
            ("sudo systemctl restart app", "sudo"),
            ("kubectl rollout undo deploy/x", "инфраструктура"),
            ("cargo publish", "публикация"),
            ("psql -c 'DROP TABLE payments'", "DROP"),
        ] {
            assert!(
                screen_step(cmd).expect("screen").is_some(),
                "'{cmd}' обязан отклоняться ({why})"
            );
        }
        for cmd in [
            "git reset --hard abc1234",
            "git status --porcelain",
            "git revert --no-commit HEAD",
            "cargo check",
            "test -f README.md",
            "rm obsolete-marker.txt",
            "true",
        ] {
            assert!(
                screen_step(cmd).expect("screen").is_none(),
                "'{cmd}' обязан проходить"
            );
        }
    }

    #[test]
    fn gate_a4_requirement_matrix() {
        let ok_report = |baseline: &str| RehearsalReport {
            kind: "rollback_rehearsal".into(),
            gate: "A4".into(),
            passed: true,
            baseline_commit: baseline.into(),
            rehearsed_at: "2026-08-26T00:00:00Z".into(),
            duration_secs: 0.1,
            steps: Vec::new(),
            verify: None,
            log: Vec::new(),
        };
        let critical = RehearsalRequirement::AtLeast(Route::Critical);

        // Critical без evidence — FAIL; с PASS-evidence — пройден.
        let v = gate_a4(Route::Critical, critical, Some("abc"), None);
        assert!(!v.passed, "{:?}", v.summary);
        assert!(v.summary.contains("--rehearse"), "{}", v.summary);
        let v = gate_a4(
            Route::Critical,
            critical,
            Some("abc"),
            Some(&ok_report("abc")),
        );
        assert!(v.passed, "{}", v.summary);

        // Fast/Standard при дефолтном пороге — пройдены и без репетиции.
        for route in [Route::Fast, Route::Standard] {
            let v = gate_a4(route, critical, Some("abc"), None);
            assert!(v.passed, "{route}: {}", v.summary);
        }
        // Порог standard требует репетиции и для Standard; never — ни для кого.
        let standard = RehearsalRequirement::AtLeast(Route::Standard);
        assert!(!gate_a4(Route::Standard, standard, None, None).passed);
        assert!(gate_a4(Route::Fast, standard, None, None).passed);
        assert!(
            !gate_a4(Route::Fast, RehearsalRequirement::Never, None, None)
                .summary
                .is_empty()
        );
        assert!(gate_a4(Route::Critical, RehearsalRequirement::Never, None, None).passed);

        // FAILED-evidence не засчитывается.
        let mut failed = ok_report("abc");
        failed.passed = false;
        assert!(!gate_a4(Route::Critical, critical, Some("abc"), Some(&failed)).passed);

        // План изменился после репетиции (другой baseline) — evidence протухло.
        let v = gate_a4(
            Route::Critical,
            critical,
            Some("def"),
            Some(&ok_report("abc")),
        );
        assert!(!v.passed, "{:?}", v.summary);
        assert!(v.summary.contains("устарело"), "{}", v.summary);
    }

    #[test]
    fn requirement_parses_thresholds() {
        assert_eq!(
            "never".parse::<RehearsalRequirement>(),
            Ok(RehearsalRequirement::Never)
        );
        assert_eq!(
            "Critical".parse::<RehearsalRequirement>(),
            Ok(RehearsalRequirement::AtLeast(Route::Critical))
        );
        assert!("sometimes".parse::<RehearsalRequirement>().is_err());
    }

    #[test]
    fn locate_packet_accepts_repo_or_packet_dir() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        let packet = write_packet(&repo, "Fast", "baseline_commit: \"\"\nsteps: []\n");
        let (r1, p1) = locate_packet(&repo).expect("repo");
        assert_eq!((r1, p1.clone()), (repo.clone(), packet.clone()));
        let (r2, p2) = locate_packet(&packet).expect("packet dir");
        assert_eq!((r2, p2), (repo, packet));
        let err = locate_packet(tmp.path()).expect_err("нет пакета");
        assert!(err.to_string().contains("arch handoff"), "{err}");
    }

    #[test]
    fn packet_route_reads_manifest() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        let packet = write_packet(&repo, "Critical", "baseline_commit: \"\"\nsteps: []\n");
        assert_eq!(packet_route(&packet).expect("route"), Route::Critical);
        std::fs::write(packet.join("MANIFEST.json"), "{\"route\": \"weird\"}\n").expect("write");
        assert!(
            packet_route(&packet).is_err(),
            "неизвестный маршрут — ошибка"
        );
    }
}
