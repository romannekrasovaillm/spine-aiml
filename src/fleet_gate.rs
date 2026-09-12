//! Гейты узла плана флота: механические проверки ПОСЛЕ прогона агента.
//!
//! Судья в движке оркестрации всегда детерминированный (ADR-042): гейт —
//! это вызов уже существующей проверки, а не суждение модели. Каждый вариант
//! [`GateSpec`] — тонкая обёртка:
//! [`Contract`](GateSpec::Contract) — JSON-контракт результата
//! ([`crate::harness::parse_result_contract`]); [`Fitness`](GateSpec::Fitness) —
//! `control check` ([`crate::control::check`]); [`SpineLint`](GateSpec::SpineLint) —
//! линтер спайна ([`crate::control::lint_spine`]); [`DeltaGuard`](GateSpec::DeltaGuard) —
//! запрет правок спайна мимо дельты ([`crate::delta::guard`]);
//! [`Outputs`](GateSpec::Outputs) — объявленные артефакты существуют;
//! [`Command`](GateSpec::Command) — детерминированная команда (тесты);
//! [`Review`](GateSpec::Review) — вердикт состязательного ревьювера
//! READY/NOT-READY.
//!
//! LLM-вердикт ([`ReviewVerdict`]) принимается только для БЛОКИРОВКИ и никогда
//! для выбора «лучшего кода» — выбор в турнире делает детерминированный
//! порядок гейтов.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{HarnessError, Result};
use crate::harness::ContractParse;
use crate::preflight::Verdict;

/// Вердикт состязательного ревьювера.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ReviewVerdict {
    /// Готово к интеграции.
    Ready,
    /// Не готово: находки обязывают блокировать зависимые узлы и мерж.
    NotReady,
}

impl ReviewVerdict {
    /// Строковый ключ вердикта (для журнала и отчётов).
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ready => "ready",
            Self::NotReady => "not-ready",
        }
    }
}

/// Гейт узла плана.
///
/// В плане записывается либо коротким ключом (`"fitness"`, `"contract"`), либо
/// таблицей с одним ключом-именем гейта (`{ command = { cmd = "pytest -q" } }`,
/// `{ review = { min = "ready" } }`) — разбор см. [`GateSpec::from_value`].
#[derive(Debug, Clone, PartialEq)]
pub enum GateSpec {
    /// Контракт результата: статус должен быть в `allow` (дефолт `complete`).
    Contract {
        /// Разрешённые статусы (`complete | partial | blocked`).
        allow: Vec<String>,
    },
    /// Fitness-функции репозитория (`control check`).
    Fitness {
        /// Файл правил (дефолт: `CONSTRAINTS.yaml`, затем
        /// `.arch-handoff/CONSTRAINTS.yaml`).
        constraints: Option<PathBuf>,
    },
    /// Линтер спайна.
    SpineLint {
        /// Файл спайна (дефолт `ARCHITECTURE-SPINE.md`).
        file: Option<PathBuf>,
    },
    /// Delta guard: правки защищённых путей только через активную дельту.
    DeltaGuard {
        /// База diff (дефолт `origin/main...HEAD` на стороне `delta::guard`).
        base: Option<String>,
        /// Защищённые префиксы путей.
        protect: Vec<String>,
    },
    /// Сенсор «produces»: объявленные артефакты существуют.
    Outputs {
        /// Пути относительно репозитория узла.
        paths: Vec<String>,
    },
    /// Детерминированная команда; гейт пройден при exit 0.
    Command {
        /// Команда (исполняется shell'ом в каталоге узла).
        cmd: String,
        /// Таймаут, секунд.
        timeout_secs: u64,
    },
    /// Вердикт ревьювера из stdout узла.
    Review {
        /// Минимальный вердикт, при котором гейт пройден.
        min: ReviewVerdict,
    },
}

/// Дефолт разрешённых статусов контракта.
fn default_allow() -> Vec<String> {
    vec!["complete".to_string()]
}

/// Дефолт таймаута команды-гейта, секунд.
fn default_cmd_timeout() -> u64 {
    600
}

/// Дефолтный вердикт review-гейта.
fn default_min_ready() -> ReviewVerdict {
    ReviewVerdict::Ready
}

impl GateSpec {
    /// Разбирает гейт из «сырого» значения (строка-ключ или таблица).
    ///
    /// Формы: `"contract" | "fitness" | "spine_lint" | "delta_guard" | "outputs"
    /// | "review"`, либо таблица `{ command = { cmd = "…", timeout_secs = 60 } }`,
    /// `{ outputs = { paths = ["a.py"] } }`, `{ fitness = { constraints = "…" } }`.
    ///
    /// # Errors
    /// Неизвестный ключ гейта или несовместимая форма значения.
    pub fn from_value(raw: &serde_json::Value) -> std::result::Result<Self, String> {
        if let Some(keyword) = raw.as_str() {
            return Self::from_keyword(keyword);
        }
        let obj = raw
            .as_object()
            .ok_or_else(|| format!("гейт: ожидалась строка или таблица, получено {raw}"))?;
        let (name, body) = obj
            .iter()
            .next()
            .ok_or_else(|| "гейт: пустая таблица".to_string())?;
        let empty = serde_json::json!({});
        let body = if body.is_null() { &empty } else { body };
        match name.as_str() {
            "contract" => Ok(Self::Contract {
                allow: str_list(body.get("allow")).unwrap_or_else(default_allow),
            }),
            "fitness" => Ok(Self::Fitness {
                constraints: body
                    .get("constraints")
                    .and_then(serde_json::Value::as_str)
                    .map(PathBuf::from),
            }),
            "spine_lint" => Ok(Self::SpineLint {
                file: body
                    .get("file")
                    .and_then(serde_json::Value::as_str)
                    .map(PathBuf::from),
            }),
            "delta_guard" => Ok(Self::DeltaGuard {
                base: body
                    .get("base")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_owned),
                protect: str_list(body.get("protect")).unwrap_or_default(),
            }),
            "outputs" => Ok(Self::Outputs {
                paths: str_list(body.get("paths"))
                    .ok_or_else(|| "гейт outputs: нужно поле paths (массив строк)".to_string())?,
            }),
            "command" => Ok(Self::Command {
                cmd: body
                    .get("cmd")
                    .and_then(serde_json::Value::as_str)
                    .ok_or_else(|| "гейт command: нужно поле cmd (строка)".to_string())?
                    .to_string(),
                timeout_secs: body
                    .get("timeout_secs")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or_else(default_cmd_timeout),
            }),
            "review" => {
                let min = match body.get("min").and_then(serde_json::Value::as_str) {
                    Some("ready") => ReviewVerdict::Ready,
                    Some("not-ready") => ReviewVerdict::NotReady,
                    Some(other) => {
                        return Err(format!("гейт review: неизвестный min '{other}'"));
                    }
                    None => default_min_ready(),
                };
                Ok(Self::Review { min })
            }
            other => Err(format!("неизвестный гейт '{other}'")),
        }
    }

    /// Короткая форма гейта (`"fitness"` и т.п.).
    ///
    /// # Errors
    /// Неизвестный ключ гейта.
    pub fn from_keyword(keyword: &str) -> std::result::Result<Self, String> {
        match keyword.trim().to_ascii_lowercase().as_str() {
            "contract" => Ok(Self::Contract {
                allow: default_allow(),
            }),
            "fitness" => Ok(Self::Fitness { constraints: None }),
            "spine_lint" => Ok(Self::SpineLint { file: None }),
            "delta_guard" => Ok(Self::DeltaGuard {
                base: None,
                protect: Vec::new(),
            }),
            "outputs" => Ok(Self::Outputs { paths: Vec::new() }),
            "review" => Ok(Self::Review {
                min: default_min_ready(),
            }),
            other => Err(format!("неизвестный гейт '{other}'")),
        }
    }

    /// Имя гейта для журнала и отчётов.
    #[must_use]
    pub const fn name(&self) -> &'static str {
        match self {
            Self::Contract { .. } => "contract",
            Self::Fitness { .. } => "fitness",
            Self::SpineLint { .. } => "spine_lint",
            Self::DeltaGuard { .. } => "delta_guard",
            Self::Outputs { .. } => "outputs",
            Self::Command { .. } => "command",
            Self::Review { .. } => "review",
        }
    }
}

impl<'de> Deserialize<'de> for GateSpec {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let raw = serde_json::Value::deserialize(deserializer)?;
        Self::from_value(&raw).map_err(serde::de::Error::custom)
    }
}

impl Serialize for GateSpec {
    fn serialize<S>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        use serde::ser::SerializeMap as _;
        // Поля `None` в тело гейта НЕ пишутся: TOML не умеет `null`
        // (`unsupported unit type`), а JSON/YAML от этого только чище.
        let mut body = serde_json::Map::new();
        let name = match self {
            Self::Contract { allow } => {
                body.insert("allow".into(), serde_json::json!(allow));
                "contract"
            }
            Self::Fitness { constraints } => {
                if let Some(c) = constraints {
                    body.insert("constraints".into(), serde_json::json!(c));
                }
                "fitness"
            }
            Self::SpineLint { file } => {
                if let Some(f) = file {
                    body.insert("file".into(), serde_json::json!(f));
                }
                "spine_lint"
            }
            Self::DeltaGuard { base, protect } => {
                if let Some(b) = base {
                    body.insert("base".into(), serde_json::json!(b));
                }
                if !protect.is_empty() {
                    body.insert("protect".into(), serde_json::json!(protect));
                }
                "delta_guard"
            }
            Self::Outputs { paths } => {
                body.insert("paths".into(), serde_json::json!(paths));
                "outputs"
            }
            Self::Command { cmd, timeout_secs } => {
                body.insert("cmd".into(), serde_json::json!(cmd));
                body.insert("timeout_secs".into(), serde_json::json!(timeout_secs));
                "command"
            }
            Self::Review { min } => {
                body.insert("min".into(), serde_json::json!(min.as_str()));
                "review"
            }
        };
        let mut map = serializer.serialize_map(Some(1))?;
        map.serialize_entry(name, &serde_json::Value::Object(body))?;
        map.end()
    }
}

/// Список строк из JSON-значения (не-массив или не-строки — `None`).
fn str_list(value: Option<&serde_json::Value>) -> Option<Vec<String>> {
    let arr = value?.as_array()?;
    let mut out = Vec::with_capacity(arr.len());
    for v in arr {
        out.push(v.as_str()?.to_string());
    }
    Some(out)
}

/// Итог одного гейта.
#[derive(Debug, Clone, Serialize)]
pub struct GateReport {
    /// Имя гейта (`fitness`, `command:pytest -q`, …).
    pub gate: String,
    /// Вердикт по шкале [`Verdict`].
    #[serde(skip)]
    pub verdict: Verdict,
    /// Ключ вердикта (`ok|warn|fail`) — сериализуемая форма.
    pub result: String,
    /// Подробность (сводка проверки или причина отказа).
    pub detail: String,
}

impl GateReport {
    fn new(gate: impl Into<String>, verdict: Verdict, detail: impl Into<String>) -> Self {
        Self {
            gate: gate.into(),
            verdict,
            result: verdict.as_str().to_string(),
            detail: detail.into(),
        }
    }

    /// Гейт провален.
    #[must_use]
    pub fn failed(&self) -> bool {
        self.verdict == Verdict::Fail
    }
}

/// Итог узла — вход интегратора (fan-in) и review-гейтов.
#[derive(Debug, Clone, Serialize)]
pub struct NodeOutcome {
    /// Идентификатор узла.
    pub node_id: String,
    /// Агент (харнесс), исполнявший узел.
    pub agent_id: String,
    /// Статус контракта результата (`complete|partial|blocked|invalid|missing`).
    pub contract_status: String,
    /// Изменённые файлы (из коммита узла, если он есть).
    pub changed_files: Vec<String>,
    /// Короткая сводка (из контракта или вывода).
    pub summary: String,
}

/// Контекст прогона гейтов узла.
pub struct GateContext<'a> {
    /// Каталог узла (worktree или репозиторий) — проверяется именно он.
    pub repo: &'a Path,
    /// stdout прогона агента.
    pub stdout: &'a str,
    /// Разобранный контракт результата.
    pub contract: &'a ContractParse,
    /// Уровень автономии (для классификации `Command`-гейта).
    pub policy: crate::policy::Policy,
}

/// Прогоняет гейты последовательно; первый `Fail` не отменяет остальные —
/// журналу нужна полная картина.
///
/// # Errors
/// Ошибка чтения файла правил/спайна или сбой запуска команды.
pub async fn run_gates(gates: &[GateSpec], ctx: &GateContext<'_>) -> Result<Vec<GateReport>> {
    let mut out = Vec::with_capacity(gates.len());
    for gate in gates {
        out.push(run_gate(gate, ctx).await?);
    }
    Ok(out)
}

/// Прогоняет один гейт.
///
/// # Errors
/// Ошибка чтения файла правил/спайна или сбой запуска команды.
pub async fn run_gate(gate: &GateSpec, ctx: &GateContext<'_>) -> Result<GateReport> {
    match gate {
        GateSpec::Contract { allow } => Ok(run_contract(allow, ctx)),
        GateSpec::Fitness { constraints } => run_fitness(constraints.as_deref(), ctx),
        GateSpec::SpineLint { file } => run_spine_lint(file.as_deref(), ctx),
        GateSpec::DeltaGuard { base, protect } => run_delta_guard(base.as_deref(), protect, ctx),
        GateSpec::Outputs { paths } => Ok(run_outputs(paths, ctx)),
        GateSpec::Command { cmd, timeout_secs } => run_command(cmd, *timeout_secs, ctx).await,
        GateSpec::Review { min } => Ok(run_review(*min, ctx)),
    }
}

/// Гейт контракта результата.
fn run_contract(allow: &[String], ctx: &GateContext<'_>) -> GateReport {
    let (status, detail) = match ctx.contract {
        ContractParse::Valid(c) => (
            c.status.as_str().to_string(),
            format!(
                "статус {}; допущено: {}",
                c.status.as_str(),
                allow.join(", ")
            ),
        ),
        ContractParse::Invalid(e) => ("invalid".to_string(), format!("контракт невалиден: {e}")),
        ContractParse::Missing => (
            "missing".to_string(),
            "в выводе нет JSON-контракта результата".to_string(),
        ),
    };
    if allow.iter().any(|a| a == &status) {
        GateReport::new("contract", Verdict::Ok, detail)
    } else {
        GateReport::new(
            "contract",
            Verdict::Fail,
            format!("{detail}; требуется один из: {}", allow.join(", ")),
        )
    }
}

/// Гейт fitness-функций (`control check`).
fn run_fitness(constraints: Option<&Path>, ctx: &GateContext<'_>) -> Result<GateReport> {
    let path = match constraints {
        Some(p) => {
            if p.is_absolute() {
                p.to_path_buf()
            } else {
                ctx.repo.join(p)
            }
        }
        None => default_constraints(ctx.repo),
    };
    if !path.is_file() {
        return Ok(GateReport::new(
            "fitness",
            Verdict::Warn,
            format!("файл правил не найден: {}", path.display()),
        ));
    }
    let report = crate::control::check(ctx.repo, &path)?;
    let verdict = if report.passed {
        Verdict::Ok
    } else {
        Verdict::Fail
    };
    Ok(GateReport::new("fitness", verdict, report.summary))
}

/// Файл правил по умолчанию: `CONSTRAINTS.yaml`, иначе пакетный.
fn default_constraints(repo: &Path) -> PathBuf {
    let root = repo.join("CONSTRAINTS.yaml");
    if root.is_file() {
        return root;
    }
    repo.join(".arch-handoff/CONSTRAINTS.yaml")
}

/// Гейт линтера спайна.
fn run_spine_lint(file: Option<&Path>, ctx: &GateContext<'_>) -> Result<GateReport> {
    let path = match file {
        Some(p) if p.is_absolute() => p.to_path_buf(),
        Some(p) => ctx.repo.join(p),
        None => ctx.repo.join("ARCHITECTURE-SPINE.md"),
    };
    if !path.is_file() {
        return Ok(GateReport::new(
            "spine_lint",
            Verdict::Warn,
            format!("спайн не найден: {}", path.display()),
        ));
    }
    let issues = crate::control::lint_spine(&path)?;
    let errors: Vec<&crate::control::LintIssue> =
        issues.iter().filter(|i| i.severity == "error").collect();
    if errors.is_empty() {
        return Ok(GateReport::new(
            "spine_lint",
            Verdict::Ok,
            format!("находок-error нет (всего находок {})", issues.len()),
        ));
    }
    Ok(GateReport::new(
        "spine_lint",
        Verdict::Fail,
        format!(
            "нарушений-ошибок {}; первое: {} ({})",
            errors.len(),
            errors[0].message,
            errors[0].rule
        ),
    ))
}

/// Гейт delta guard.
fn run_delta_guard(
    base: Option<&str>,
    protect: &[String],
    ctx: &GateContext<'_>,
) -> Result<GateReport> {
    let report = crate::delta::guard(ctx.repo, base, protect)?;
    let verdict = if report.passed {
        Verdict::Ok
    } else {
        Verdict::Fail
    };
    let detail = if report.passed {
        format!(
            "правки защищённых путей покрыты дельтой (изменено {})",
            report.changed
        )
    } else {
        format!(
            "непокрытых правок {}: {}",
            report.violations.len(),
            report.violations.join(", ")
        )
    };
    Ok(GateReport::new("delta_guard", verdict, detail))
}

/// Гейт объявленных артефактов-выходов.
fn run_outputs(paths: &[String], ctx: &GateContext<'_>) -> GateReport {
    if paths.is_empty() {
        return GateReport::new(
            "outputs",
            Verdict::Warn,
            "у гейта outputs пустой список путей",
        );
    }
    let missing: Vec<&str> = paths
        .iter()
        .filter(|p| !ctx.repo.join(p.as_str()).exists())
        .map(String::as_str)
        .collect();
    if missing.is_empty() {
        return GateReport::new(
            "outputs",
            Verdict::Ok,
            format!("артефакты на месте ({})", paths.join(", ")),
        );
    }
    GateReport::new(
        "outputs",
        Verdict::Fail,
        format!("нет объявленных артефактов: {}", missing.join(", ")),
    )
}

/// Гейт детерминированной команды (тесты). Деструктивная команда на текущем
/// R-уровне получает `Warn` с объяснением — молча не исполняется.
async fn run_command(cmd: &str, timeout_secs: u64, ctx: &GateContext<'_>) -> Result<GateReport> {
    let name = format!("command:{cmd}");
    let args = serde_json::json!({ "command": cmd });
    match ctx.policy.check("bash", &args) {
        crate::policy::PolicyDecision::Allow => {}
        crate::policy::PolicyDecision::RequireConfirm(why) => {
            return Ok(GateReport::new(
                name,
                Verdict::Warn,
                format!("команда требует подтверждения человека (headless): {why}"),
            ));
        }
        crate::policy::PolicyDecision::Deny(why) => {
            return Ok(GateReport::new(
                name,
                Verdict::Warn,
                format!("команда запрещена политикой: {why}"),
            ));
        }
    }
    let mut command = tokio::process::Command::new("sh");
    command
        .arg("-c")
        .arg(cmd)
        .current_dir(ctx.repo)
        .kill_on_drop(true)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let fut = command.output();
    let output = match tokio::time::timeout(std::time::Duration::from_secs(timeout_secs), fut).await
    {
        Ok(Ok(o)) => o,
        Ok(Err(e)) => {
            return Err(HarnessError::Harness(format!(
                "гейт command '{cmd}': не удалось запустить: {e}"
            )));
        }
        Err(_) => {
            return Ok(GateReport::new(
                name,
                Verdict::Fail,
                format!("таймаут {timeout_secs} с"),
            ));
        }
    };
    let code = output.status.code();
    let tail = tail_text(&String::from_utf8_lossy(&output.stderr), 400);
    if output.status.success() {
        Ok(GateReport::new(
            name,
            Verdict::Ok,
            format!("exit 0 ({})", output.status),
        ))
    } else {
        Ok(GateReport::new(
            name,
            Verdict::Fail,
            format!("exit {code:?}; stderr: {tail}"),
        ))
    }
}

/// Хвост текста (для компактной подробности гейта).
fn tail_text(text: &str, max_chars: usize) -> String {
    let trimmed = text.trim();
    if trimmed.chars().count() <= max_chars {
        return trimmed.to_string();
    }
    let skip = trimmed.chars().count() - max_chars;
    trimmed.chars().skip(skip).collect()
}

/// Гейт вердикта ревьювера: `min = ready` требует READY, `min = not-ready`
/// пропускает любой распознанный вердикт (но не его отсутствие).
fn run_review(min: ReviewVerdict, ctx: &GateContext<'_>) -> GateReport {
    match parse_review_verdict(ctx.stdout) {
        Some(ReviewVerdict::Ready) => GateReport::new("review", Verdict::Ok, "вердикт ready"),
        Some(ReviewVerdict::NotReady) if min == ReviewVerdict::NotReady => GateReport::new(
            "review",
            Verdict::Ok,
            "вердикт not-ready (допущен политикой)",
        ),
        Some(ReviewVerdict::NotReady) => GateReport::new(
            "review",
            Verdict::Fail,
            "вердикт not-ready: находки блокируют интеграцию",
        ),
        None => GateReport::new(
            "review",
            Verdict::Fail,
            "в выводе ревьювера нет распознанного вердикта READY/NOT-READY",
        ),
    }
}

/// Разбирает вердикт ревьювера из stdout: последняя строка с полем `verdict`
/// (`ready` | `not-ready`, регистр не важен).
#[must_use]
pub fn parse_review_verdict(stdout: &str) -> Option<ReviewVerdict> {
    let mut found = None;
    for line in stdout.lines() {
        let trimmed = line.trim();
        let lowered = trimmed.to_ascii_lowercase();
        // Терпимый разбор: строка вида `"verdict": "NOT-READY"` в JSON-блоке.
        if let Some(rest) = lowered.split("\"verdict\"").nth(1) {
            let rest = rest.trim_start_matches([':', ' ', '"']);
            if rest.starts_with("not-ready") || rest.starts_with("not_ready") {
                found = Some(ReviewVerdict::NotReady);
            } else if rest.starts_with("ready") {
                found = Some(ReviewVerdict::Ready);
            }
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::harness::{ContractParse, ContractStatus, ResultContract};

    fn ctx_for<'a>(
        repo: &'a Path,
        stdout: &'a str,
        contract: &'a ContractParse,
    ) -> GateContext<'a> {
        GateContext {
            repo,
            stdout,
            contract,
            policy: crate::policy::Policy::parse("R2").expect("policy"),
        }
    }

    #[test]
    fn gate_keyword_roundtrip() {
        let g: GateSpec = serde_json::from_str("\"fitness\"").expect("keyword");
        assert_eq!(g.name(), "fitness");
        let detailed: GateSpec =
            serde_json::from_str(r#"{"command":{"cmd":"pytest -q","timeout_secs":60}}"#)
                .expect("table");
        assert_eq!(
            detailed,
            GateSpec::Command {
                cmd: "pytest -q".into(),
                timeout_secs: 60
            }
        );
        let review: GateSpec =
            serde_json::from_str(r#"{"review":{"min":"not-ready"}}"#).expect("review");
        assert_eq!(
            review,
            GateSpec::Review {
                min: ReviewVerdict::NotReady
            }
        );
    }

    #[test]
    fn gate_spec_serialises_to_toml_without_nulls() {
        // Регрессия: поле `None` в теле гейта давало TOML «unsupported unit
        // type» — TOML не умеет null. Сериализатор обязан пропускать пустые поля.
        for gate in [
            GateSpec::Fitness { constraints: None },
            GateSpec::SpineLint { file: None },
            GateSpec::DeltaGuard {
                base: None,
                protect: Vec::new(),
            },
            GateSpec::Contract {
                allow: vec!["complete".into()],
            },
        ] {
            let text = toml::to_string(&gate).expect("TOML без null");
            let back: GateSpec = toml::from_str(&text).expect("обратный разбор");
            assert_eq!(back, gate, "round-trip: {text}");
        }
    }

    #[test]
    fn unknown_gate_is_error() {
        let err = GateSpec::from_keyword("nope").expect_err("must fail");
        assert!(err.contains("неизвестный гейт"), "{err}");
    }

    #[test]
    fn contract_gate_fails_on_blocked() {
        let dir = tempfile::tempdir().expect("tmp");
        let contract = ContractParse::Valid(ResultContract {
            status: ContractStatus::Blocked,
            assumptions: Vec::new(),
            open_questions: Vec::new(),
            conflicts: Vec::new(),
        });
        let ctx = ctx_for(dir.path(), "", &contract);
        let report = run_contract(&default_allow(), &ctx);
        assert!(report.failed(), "{report:?}");
        let ok = run_contract(&["blocked".into()], &ctx);
        assert!(!ok.failed(), "{ok:?}");
    }

    #[test]
    fn outputs_gate_detects_missing() {
        let dir = tempfile::tempdir().expect("tmp");
        std::fs::write(dir.path().join("present.py"), "x").expect("write");
        let contract = ContractParse::Missing;
        let ctx = ctx_for(dir.path(), "", &contract);
        let missing = run_outputs(&["present.py".into(), "absent.py".into()], &ctx);
        assert!(missing.failed(), "{missing:?}");
        let ok = run_outputs(&["present.py".into()], &ctx);
        assert!(!ok.failed(), "{ok:?}");
    }

    #[test]
    fn review_verdict_parsed_from_stdout() {
        let stdout = "текст\n{\"verdict\":\"NOT-READY\",\"findings\":[]}\n";
        assert_eq!(parse_review_verdict(stdout), Some(ReviewVerdict::NotReady));
        assert_eq!(
            parse_review_verdict("{\"verdict\": \"ready\"}"),
            Some(ReviewVerdict::Ready)
        );
        assert_eq!(parse_review_verdict("нет вердикта"), None);
    }

    #[test]
    fn review_gate_fails_without_verdict() {
        let dir = tempfile::tempdir().expect("tmp");
        let contract = ContractParse::Missing;
        let ctx = ctx_for(dir.path(), "болтовня без вердикта", &contract);
        let report = run_review(ReviewVerdict::Ready, &ctx);
        assert!(report.failed(), "{report:?}");
    }
}
