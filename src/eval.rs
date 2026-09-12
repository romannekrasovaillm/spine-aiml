//! Регрессионные eval-сьюты конфигурации харнесса (continuous evals).
//!
//! КОНТРАКТ (владелец: агент `eval`):
//! - сьют — каталог YAML-задач ([`EvalTask`]): `{id, title, command|prompt,
//!   checks, rubric?}`. Задача-команда исполняется через `bash -c`
//!   (stdout+stderr — предмет проверок), задача-промпт уходит в модель
//!   (только со включённым слоем `--judge`, иначе — пропуск);
//! - два слоя проверок: детерминированные [`EvalCheck`] (всегда, офлайн:
//!   `must_contain` / `must_not_contain` / `command_succeeds` / `json_field`)
//!   и LLM-судья по якорной рубрике (опционально, поле `rubric` + `--judge`,
//!   через [`crate::rubric::evaluate_with_options`]);
//! - [`run_suite`] считает pass-rate по исполненным задачам и применяет гейт
//!   (проценты; ниже порога — `gate_passed = false`, exit code 1 назначает CLI);
//! - встроенный сьют `assets/evals/agent-config` прогоняется герметично:
//!   [`prepare_builtin_home`] разворачивает ассеты и конфиг во временный
//!   каталог, плейсхолдеры команд — `{arch}` (бинарь + `--config`) и `{home}`.
//!   Пользовательский сьют (`--suite`) бежит против ЖИВОЙ установки харнесса.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tokio::io::AsyncReadExt as _;

use crate::config::{Config, JudgeConfig};
use crate::error::{HarnessError, Result};
use crate::llm::{ChatMessage, ChatRequest, LlmProvider};

/// Дефолтный таймаут команды задачи, секунды.
const DEFAULT_TIMEOUT_SECS: u64 = 120;
/// Дефолтный проходной порог взвешенного итога рубрики (шкала 1..=5).
const DEFAULT_PASS_THRESHOLD: f64 = 3.0;
/// Сколько символов вывода задачи сохраняется в отчёте (excerpt).
const MAX_OUTPUT_EXCERPT: usize = 4000;
/// Сколько символов значения JSON-поля показывается в деталях проверки.
const MAX_JSON_DETAIL: usize = 120;

/// Задача eval-сьюта (YAML-файл каталога сьюта).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvalTask {
    /// Уникальный идентификатор задачи (kebab-case).
    pub id: String,
    /// Человекочитаемое название: что проверяет.
    pub title: String,
    /// Shell-команда (`bash -c`); вывод stdout+stderr — предмет проверок.
    /// Плейсхолдеры: `{arch}` — вызов бинаря харнесса, `{home}` — домашний
    /// каталог прогона (в кавычках).
    #[serde(default)]
    pub command: Option<String>,
    /// Промпт модели (задача слоя `--judge`; без судьи — пропускается).
    #[serde(default)]
    pub prompt: Option<String>,
    /// Рубрика LLM-судьи (имя файла в assets/rubrics или путь; слой `--judge`).
    #[serde(default)]
    pub rubric: Option<String>,
    /// Проходной порог взвешенного итога рубрики (дефолт 3.0).
    #[serde(default)]
    pub pass_threshold: Option<f64>,
    /// Детерминированные проверки вывода (слой 1, офлайн).
    #[serde(default)]
    pub checks: Vec<EvalCheck>,
    /// Таймаут команды, секунды (дефолт 120).
    #[serde(default = "default_timeout_secs")]
    pub timeout_secs: u64,
}

fn default_timeout_secs() -> u64 {
    DEFAULT_TIMEOUT_SECS
}

/// Детерминированная проверка вывода задачи (слой 1).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum EvalCheck {
    /// Regex должен найтись в выводе задачи.
    MustContain {
        /// Regex (синтаксис crate `regex`).
        pattern: String,
    },
    /// Regex не должен встречаться в выводе задачи.
    MustNotContain {
        /// Regex (синтаксис crate `regex`).
        pattern: String,
    },
    /// Команда завершилась кодом 0 до таймаута (только command-задачи).
    CommandSucceeds,
    /// JSON-указатель (RFC 6901) на поле вывода: поле существует
    /// (и равно `equals`, если задано). Вывод обязан быть валидным JSON.
    JsonField {
        /// Указатель вида `/status/code`.
        pointer: String,
        /// Ожидаемое значение (опционально; без него — проверка наличия).
        #[serde(default)]
        equals: Option<serde_json::Value>,
    },
}

/// Контекст исполнения команд сьюта.
#[derive(Debug, Clone)]
pub struct SuiteContext {
    /// Рабочий каталог команд.
    pub cwd: PathBuf,
    /// Префикс вызова бинаря `arch` (в кавычках; для встроенного сьюта —
    /// с явным `--config` герметичного дома).
    pub arch: String,
    /// Домашний каталог харнесса прогона (подстановка `{home}` и `ARCH_ML_HOME`).
    pub home: PathBuf,
}

impl SuiteContext {
    /// Контекст герметичного прогона встроенного сьюта: команды вызывают
    /// текущий бинарь с явным `--config` внутри подготовленного дома —
    /// прогон не зависит от пользовательского конфига и `arch init`.
    ///
    /// # Errors
    /// Не удалось определить путь текущего бинаря.
    pub fn for_builtin(home: &Path, config: &Path) -> Result<Self> {
        let exe = std::env::current_exe()
            .map_err(|e| HarnessError::Eval(format!("путь текущего бинаря: {e}")))?;
        Ok(Self {
            cwd: home.to_path_buf(),
            arch: format!("\"{}\" --config \"{}\"", exe.display(), config.display()),
            home: home.to_path_buf(),
        })
    }

    /// Контекст живого окружения (пользовательский `--suite`): бинарь без
    /// подмены конфига — прогон проверяет реальную установку харнесса
    /// (реальный конфиг, реальные `paths.*` и библиотеки плагинов).
    ///
    /// # Errors
    /// Не удалось определить путь текущего бинаря или рабочий каталог.
    pub fn for_live() -> Result<Self> {
        let exe = std::env::current_exe()
            .map_err(|e| HarnessError::Eval(format!("путь текущего бинаря: {e}")))?;
        let cwd = std::env::current_dir()
            .map_err(|e| HarnessError::Eval(format!("рабочий каталог: {e}")))?;
        Ok(Self {
            cwd,
            arch: format!("\"{}\"", exe.display()),
            home: Config::home_dir(),
        })
    }
}

/// Герметичный дом встроенного сьюта: развёрнутые ассеты + конфиг.
#[derive(Debug, Clone)]
pub struct BuiltinHome {
    /// Каталог развёрнутого сьюта (`assets/evals/agent-config`).
    pub suite_dir: PathBuf,
    /// Сгенерированный конфиг, смотрящий только внутрь дома.
    pub config: PathBuf,
}

/// Разворачивает герметичный дом для встроенного сьюта: дефолтные ассеты
/// ([`crate::assets::write_defaults`], включая сам сьют) + конфиг со всеми
/// путями внутри `home`. Существующие файлы не затираются.
///
/// # Errors
/// Ошибка записи ассетов/конфига.
pub fn prepare_builtin_home(home: &Path) -> Result<BuiltinHome> {
    crate::assets::write_defaults(home)?;
    let mut cfg = Config::default();
    cfg.paths.assets_dir = home.join("assets");
    cfg.paths.reports_dir = home.join("reports");
    cfg.paths.sessions_dir = home.join("sessions");
    cfg.paths.memory_file = home.join("MEMORY.md");
    cfg.plugins.dirs = vec![home.join("plugins")];
    cfg.mcp.servers_file = home.join("mcp.json");
    cfg.cron.file = home.join("cron.toml");
    let config = home.join("eval-config.toml");
    let text = toml::to_string_pretty(&cfg)
        .map_err(|e| HarnessError::Eval(format!("сериализация конфига сьюта: {e}")))?;
    std::fs::write(&config, text).map_err(|e| HarnessError::io(&config, e))?;
    Ok(BuiltinHome {
        suite_dir: home.join("assets/evals/agent-config"),
        config,
    })
}

/// Подставляет плейсхолдеры `{arch}` и `{home}` в шаблон команды.
#[must_use]
pub fn expand_command(template: &str, ctx: &SuiteContext) -> String {
    template
        .replace("{arch}", &ctx.arch)
        .replace("{home}", &format!("\"{}\"", ctx.home.display()))
}

/// Валидирует задачу (вызывается при загрузке сьюта: битая задача —
/// ошибка сьюта, а не молчаливый пропуск — иначе гейт завышал бы pass-rate).
fn validate_task(task: &EvalTask, source: &Path) -> Result<()> {
    let bad = |msg: String| HarnessError::Eval(format!("{}: {msg}", source.display()));
    if task.id.trim().is_empty() {
        return Err(bad("пустой id".into()));
    }
    if task.title.trim().is_empty() {
        return Err(bad(format!("задача '{}': пустой title", task.id)));
    }
    let has_command = task
        .command
        .as_deref()
        .is_some_and(|c| !c.trim().is_empty());
    let has_prompt = task.prompt.as_deref().is_some_and(|p| !p.trim().is_empty());
    if !has_command && !has_prompt {
        return Err(bad(format!(
            "задача '{}': нужно хотя бы одно из command/prompt",
            task.id
        )));
    }
    if task.checks.is_empty() && task.rubric.is_none() {
        return Err(bad(format!(
            "задача '{}': нет ни детерминированных проверок, ни рубрики судьи",
            task.id
        )));
    }
    for check in &task.checks {
        match check {
            EvalCheck::MustContain { pattern } | EvalCheck::MustNotContain { pattern } => {
                regex::Regex::new(pattern).map_err(|e| {
                    bad(format!(
                        "задача '{}': невалидный regex '{pattern}': {e}",
                        task.id
                    ))
                })?;
            }
            EvalCheck::CommandSucceeds => {
                if !has_command {
                    return Err(bad(format!(
                        "задача '{}': проверка command_succeeds применима только к command-задачам",
                        task.id
                    )));
                }
            }
            EvalCheck::JsonField { pointer, .. } => {
                if !pointer.starts_with('/') {
                    return Err(bad(format!(
                        "задача '{}': JSON-указатель должен начинаться с '/': '{pointer}'",
                        task.id
                    )));
                }
            }
        }
    }
    Ok(())
}

/// Загружает сьют из каталога (`*.yaml`/`*.yml`), сортирует по id.
///
/// В отличие от [`crate::bench::list`], битый файл — ошибка, а не пропуск:
/// молчаливо потерянная задача завышала бы измеренный pass-rate гейта
/// (та же мотивация, что у [`crate::bench::load_golden`]).
///
/// # Errors
/// Каталог не читается, файл не парсится/не валиден, дубль id, сьют пуст.
pub fn load_suite(dir: &Path) -> Result<Vec<EvalTask>> {
    let entries = std::fs::read_dir(dir).map_err(|e| HarnessError::io(dir, e))?;
    let mut out = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let is_yaml = path
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| e.eq_ignore_ascii_case("yaml") || e.eq_ignore_ascii_case("yml"));
        if !is_yaml {
            continue;
        }
        let text = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
        let task: EvalTask = serde_yaml_ng::from_str(&text)
            .map_err(|e| HarnessError::Eval(format!("{}: разбор задачи: {e}", path.display())))?;
        validate_task(&task, &path)?;
        out.push(task);
    }
    out.sort_by(|a, b| a.id.cmp(&b.id));
    let mut dup: Option<&str> = None;
    for w in out.windows(2) {
        if w[0].id == w[1].id {
            dup = Some(&w[0].id);
        }
    }
    if let Some(id) = dup {
        return Err(HarnessError::Eval(format!(
            "{}: дублирующийся id задачи '{id}'",
            dir.display()
        )));
    }
    if out.is_empty() {
        return Err(HarnessError::Eval(format!(
            "{}: сьют пуст — ожидается хотя бы один *.yaml с задачей",
            dir.display()
        )));
    }
    Ok(out)
}

/// Исход исполнения команды задачи.
#[derive(Debug, Clone)]
pub struct CommandOutcome {
    /// Собранный вывод (stdout, затем stderr).
    pub output: String,
    /// Код выхода (None — завершена сигналом или убита по таймауту).
    pub code: Option<i32>,
    /// Превышен ли таймаут (процесс убит).
    pub timed_out: bool,
}

/// Исполняет `bash -c <команда>` в контексте сьюта с захватом вывода и
/// таймаутом (читатели stdout/stderr — отдельные задачи tokio, по таймауту
/// процесс убивается). Команды сьюта — доверенная конфигурация (как правила
/// CONSTRAINTS.yaml), окружение наследуется; `ARCH_ML_HOME` переопределяется
/// домом прогона.
///
/// # Errors
/// Не удалось запустить/дождаться процесс или прочитать его вывод.
pub async fn run_command(
    command: &str,
    ctx: &SuiteContext,
    timeout: Duration,
) -> Result<CommandOutcome> {
    let mut child = tokio::process::Command::new("bash")
        .arg("-c")
        .arg(command)
        .current_dir(&ctx.cwd)
        .env("ARCH_ML_HOME", &ctx.home)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| HarnessError::Eval(format!("не удалось запустить bash: {e}")))?;
    // stdout/stderr piped выше — take() гарантированно Some.
    let mut out_pipe = child
        .stdout
        .take()
        .ok_or_else(|| HarnessError::Eval("внутренняя: нет pipe stdout".into()))?;
    let mut err_pipe = child
        .stderr
        .take()
        .ok_or_else(|| HarnessError::Eval("внутренняя: нет pipe stderr".into()))?;
    let reader_out = tokio::spawn(async move {
        let mut buf = Vec::new();
        out_pipe.read_to_end(&mut buf).await.map(|_| buf)
    });
    let reader_err = tokio::spawn(async move {
        let mut buf = Vec::new();
        err_pipe.read_to_end(&mut buf).await.map(|_| buf)
    });
    let (status, timed_out) = if let Ok(res) = tokio::time::timeout(timeout, child.wait()).await {
        (
            Some(res.map_err(|e| HarnessError::Eval(format!("ожидание команды: {e}")))?),
            false,
        )
    } else {
        // Игнорируем ошибку kill: процесс мог завершиться в гонке с таймаутом.
        let _ = child.start_kill();
        let _ = child.wait().await; // забрать зомби
        (None, true)
    };
    let read = |r: tokio::task::JoinHandle<std::io::Result<Vec<u8>>>| async move {
        r.await
            .map_err(|e| HarnessError::Eval(format!("задача чтения вывода: {e}")))?
            .map_err(|e| HarnessError::Eval(format!("чтение вывода команды: {e}")))
    };
    let out_bytes = read(reader_out).await?;
    let err_bytes = read(reader_err).await?;
    let mut output = String::from_utf8_lossy(&out_bytes).into_owned();
    let err = String::from_utf8_lossy(&err_bytes);
    if !err.trim().is_empty() {
        let _ = write!(output, "\n[stderr]\n{err}");
    }
    Ok(CommandOutcome {
        output,
        code: status.and_then(|s| s.code()),
        timed_out,
    })
}

/// Результат одной детерминированной проверки.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckResult {
    /// Компактное описание проверки (`must_contain '…'`).
    pub check: String,
    /// Прошла ли.
    pub passed: bool,
    /// Детали (что нашлось/не нашлось).
    pub detail: String,
}

/// Оценивает одну проверку против вывода задачи.
///
/// `outcome` — исход команды (None для prompt-задач; `command_succeeds`
/// для них отклоняется ещё при загрузке сьюта).
fn eval_check(
    check: &EvalCheck,
    subject: &str,
    outcome: Option<&CommandOutcome>,
) -> Result<CheckResult> {
    match check {
        EvalCheck::MustContain { pattern } => {
            let re = regex::Regex::new(pattern)
                .map_err(|e| HarnessError::Eval(format!("внутренний regex '{pattern}': {e}")))?;
            let passed = re.is_match(subject);
            Ok(CheckResult {
                check: format!("must_contain '{pattern}'"),
                passed,
                detail: if passed {
                    "паттерн найден".into()
                } else {
                    "паттерн НЕ найден в выводе".into()
                },
            })
        }
        EvalCheck::MustNotContain { pattern } => {
            let re = regex::Regex::new(pattern)
                .map_err(|e| HarnessError::Eval(format!("внутренний regex '{pattern}': {e}")))?;
            let hit = subject
                .lines()
                .find(|l| re.is_match(l))
                .map(|l| l.trim().chars().take(MAX_JSON_DETAIL).collect::<String>());
            Ok(CheckResult {
                check: format!("must_not_contain '{pattern}'"),
                passed: hit.is_none(),
                detail: match hit {
                    None => "запрещённый паттерн не встретился".into(),
                    Some(line) => format!("найдено запрещённое: {line}"),
                },
            })
        }
        EvalCheck::CommandSucceeds => {
            let Some(outcome) = outcome else {
                return Err(HarnessError::Eval(
                    "command_succeeds без команды (должно отсекаться при загрузке)".into(),
                ));
            };
            let (passed, detail) = if outcome.timed_out {
                (false, "команда превысила таймаут и была убита".to_string())
            } else {
                match outcome.code {
                    Some(0) => (true, "код выхода 0".to_string()),
                    Some(code) => (false, format!("код выхода {code}")),
                    None => (false, "команда завершена сигналом".to_string()),
                }
            };
            Ok(CheckResult {
                check: "command_succeeds".into(),
                passed,
                detail,
            })
        }
        EvalCheck::JsonField { pointer, equals } => {
            let json: serde_json::Value = serde_json::from_str(subject.trim()).map_err(|e| {
                HarnessError::Eval(format!(
                    "json_field '{pointer}': вывод задачи не валидный JSON: {e}"
                ))
            })?;
            let found = json.pointer(pointer);
            let short = |v: &serde_json::Value| {
                let s = v.to_string();
                s.chars().take(MAX_JSON_DETAIL).collect::<String>()
            };
            let (passed, detail) = match (found, equals) {
                (Some(v), Some(want)) => (
                    v == want,
                    format!("{pointer} = {} (ожидалось {})", short(v), short(want)),
                ),
                (Some(v), None) => (true, format!("{pointer} найден: {}", short(v))),
                (None, _) => (false, format!("{pointer} отсутствует в выводе")),
            };
            Ok(CheckResult {
                check: format!("json_field '{pointer}'"),
                passed,
                detail,
            })
        }
    }
}

/// Контекст слоя LLM-судьи (включается флагом `--judge`).
pub struct JudgeCtx<'a> {
    /// Провайдер (испытуемая модель и судья в одном лице, как у `bench`).
    pub provider: &'a dyn LlmProvider,
    /// Настройки судьи (k сэмплов, верификация цитат — ADR-004).
    pub cfg: &'a JudgeConfig,
    /// Каталог якорных рубрик.
    pub rubrics_dir: &'a Path,
}

/// Отчёт по одной задаче сьюта.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskReport {
    /// Идентификатор задачи.
    pub id: String,
    /// Название.
    pub title: String,
    /// Пропущена ли (prompt-задача без `--judge`).
    pub skipped: bool,
    /// Причина пропуска.
    #[serde(default)]
    pub skip_reason: Option<String>,
    /// Прошла ли (все детерминированные проверки + вердикт судьи, если был).
    pub passed: bool,
    /// Результаты детерминированных проверок.
    pub checks: Vec<CheckResult>,
    /// Взвешенный итог судьи (если слой включён и рубрика задана).
    #[serde(default)]
    pub judge_score: Option<f64>,
    /// Порог судьи.
    #[serde(default)]
    pub judge_threshold: Option<f64>,
    /// Вердикт судьи (score >= threshold).
    #[serde(default)]
    pub judge_passed: Option<bool>,
    /// Начало вывода задачи (первые [`MAX_OUTPUT_EXCERPT`] символов).
    pub output_excerpt: String,
}

/// Отчёт прогона сьюта.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SuiteReport {
    /// Имя сьюта (каталог).
    pub suite: String,
    /// Модель-судья (если слой `--judge` включён).
    #[serde(default)]
    pub judge_model: Option<String>,
    /// Метка времени прогона.
    pub timestamp: String,
    /// Гейт pass-rate, проценты.
    pub gate_pct: f64,
    /// Исполнено задач (без пропущенных).
    pub total: usize,
    /// Прошло задач.
    pub passed: usize,
    /// Пропущено задач (вне знаменателя pass-rate).
    pub skipped: usize,
    /// Pass-rate, проценты (`passed / total × 100`).
    pub pass_rate: f64,
    /// Гейт пройден: `pass_rate >= gate_pct`.
    pub gate_passed: bool,
    /// Разбор по задачам.
    pub tasks: Vec<TaskReport>,
}

/// Прогоняет одну задачу сьюта.
async fn run_task(
    task: &EvalTask,
    ctx: &SuiteContext,
    judge: Option<&JudgeCtx<'_>>,
) -> Result<TaskReport> {
    // Предмет проверок: вывод команды либо ответ модели.
    let (subject, outcome) = if let Some(cmd) = &task.command {
        let expanded = expand_command(cmd, ctx);
        let outcome = run_command(&expanded, ctx, Duration::from_secs(task.timeout_secs)).await?;
        (outcome.output.clone(), Some(outcome))
    } else {
        // Наличие prompt гарантировано валидацией при загрузке.
        let prompt = task.prompt.clone().unwrap_or_default();
        let Some(judge) = judge else {
            return Ok(TaskReport {
                id: task.id.clone(),
                title: task.title.clone(),
                skipped: true,
                skip_reason: Some("prompt-задача требует слоя --judge".into()),
                passed: false,
                checks: Vec::new(),
                judge_score: None,
                judge_threshold: None,
                judge_passed: None,
                output_excerpt: String::new(),
            });
        };
        let request = ChatRequest::chat(vec![ChatMessage::user(prompt)]);
        let response = judge.provider.complete(request).await?.content;
        (response, None)
    };

    let mut checks = Vec::with_capacity(task.checks.len());
    for check in &task.checks {
        checks.push(eval_check(check, &subject, outcome.as_ref())?);
    }
    let checks_passed = checks.iter().all(|c| c.passed);

    // Слой 2: LLM-судья по рубрике. Без --judge молча пропускается:
    // детерминированные проверки остаются базовым (офлайн) гейтом.
    let mut judge_score = None;
    let mut judge_threshold = None;
    let mut judge_passed = None;
    if let (Some(rubric_name), Some(judge)) = (&task.rubric, judge) {
        let rubric_path = {
            let p = PathBuf::from(rubric_name);
            if p.is_absolute() {
                p
            } else {
                let direct = judge.rubrics_dir.join(p);
                if direct.is_file() {
                    direct
                } else {
                    judge.rubrics_dir.join(format!("{rubric_name}.yaml"))
                }
            }
        };
        let rubric = crate::rubric::load(&rubric_path)?;
        let report =
            crate::rubric::evaluate_with_options(&rubric, &subject, judge.provider, judge.cfg)
                .await?;
        let threshold = task.pass_threshold.unwrap_or(DEFAULT_PASS_THRESHOLD);
        judge_score = Some(report.weighted_total);
        judge_threshold = Some(threshold);
        judge_passed = Some(report.weighted_total >= threshold);
    }

    let excerpt: String = subject.chars().take(MAX_OUTPUT_EXCERPT).collect();
    Ok(TaskReport {
        id: task.id.clone(),
        title: task.title.clone(),
        skipped: false,
        skip_reason: None,
        passed: checks_passed && judge_passed.unwrap_or(true),
        checks,
        judge_score,
        judge_threshold,
        judge_passed,
        output_excerpt: excerpt,
    })
}

/// Прогоняет сьют и применяет гейт pass-rate.
///
/// Пропущенные задачи (prompt без `--judge`) в знаменатель pass-rate не
/// входят. Сьют без единой исполненной задачи — ошибка: гейт по пустому
/// знаменателю был бы фикцией.
///
/// # Errors
/// Каталог сьюта не читается, команда/модель/рубрика дали ошибку,
/// все задачи пропущены.
pub async fn run_suite(
    suite_dir: &Path,
    ctx: &SuiteContext,
    judge: Option<&JudgeCtx<'_>>,
    gate_pct: f64,
) -> Result<SuiteReport> {
    let tasks = load_suite(suite_dir)?;
    let mut reports = Vec::with_capacity(tasks.len());
    for task in &tasks {
        reports.push(run_task(task, ctx, judge).await?);
    }
    let skipped = reports.iter().filter(|r| r.skipped).count();
    let total = reports.len() - skipped;
    if total == 0 {
        return Err(HarnessError::Eval(format!(
            "{}: ни одной исполненной задачи (все пропущены — prompt-задачи требуют --judge)",
            suite_dir.display()
        )));
    }
    let passed = reports.iter().filter(|r| !r.skipped && r.passed).count();
    #[allow(clippy::cast_precision_loss)] // счётчики задач малы, точность f64 избыточна
    let pass_rate = passed as f64 * 100.0 / total as f64;
    let suite = suite_dir.file_name().map_or_else(
        || suite_dir.display().to_string(),
        |n| n.to_string_lossy().into_owned(),
    );
    Ok(SuiteReport {
        suite,
        judge_model: judge.map(|j| j.provider.model().to_string()),
        timestamp: chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
        gate_pct,
        total,
        passed,
        skipped,
        pass_rate,
        gate_passed: pass_rate + f64::EPSILON >= gate_pct,
        tasks: reports,
    })
}

/// Текстовая сводка прогона для stdout (русская, как весь юзерфейс).
#[must_use]
pub fn render_text(report: &SuiteReport) -> String {
    let mut out = String::new();
    let _ = writeln!(
        out,
        "Eval-сьют '{}': задач {}, пропущено {}",
        report.suite, report.total, report.skipped
    );
    if let Some(model) = &report.judge_model {
        let _ = writeln!(out, "Судья: {model}");
    }
    for task in &report.tasks {
        if task.skipped {
            let _ = writeln!(
                out,
                "  ⊘ {} — пропущена ({})",
                task.id,
                task.skip_reason.as_deref().unwrap_or("без причины")
            );
            continue;
        }
        let mark = if task.passed { "✓" } else { "✗" };
        let _ = writeln!(out, "  {mark} {} — {}", task.id, task.title);
        for c in task.checks.iter().filter(|c| !c.passed) {
            let _ = writeln!(out, "      ✗ {}: {}", c.check, c.detail);
        }
        if let (Some(score), Some(threshold)) = (task.judge_score, task.judge_threshold) {
            let verdict = if task.judge_passed == Some(true) {
                ">="
            } else {
                "<"
            };
            let _ = writeln!(out, "      судья: {score:.2} {verdict} {threshold:.2}");
        }
    }
    let _ = writeln!(
        out,
        "Pass-rate: {:.1}% ({}/{}), гейт {:.1}% — {}",
        report.pass_rate,
        report.passed,
        report.total,
        report.gate_pct,
        if report.gate_passed { "PASS" } else { "FAIL" }
    );
    out
}

/// Пишет JSON-отчёт прогона в `out_dir` (`eval-<suite>-<yyyymmdd-hhmmss>.json`).
///
/// # Errors
/// Каталог не создаётся, файл не пишется.
pub fn write_report(report: &SuiteReport, out_dir: &Path) -> Result<PathBuf> {
    std::fs::create_dir_all(out_dir).map_err(|e| HarnessError::io(out_dir, e))?;
    let stamp = chrono::Local::now().format("%Y%m%d-%H%M%S");
    let path = out_dir.join(format!(
        "eval-{}-{stamp}.json",
        sanitize_file_part(&report.suite)
    ));
    let json = serde_json::to_string_pretty(report)?;
    std::fs::write(&path, json).map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

/// Заменяет символы, небезопасные в имени файла, на `-`.
fn sanitize_file_part(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '-'
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;

    /// Задача-команда для тестов раннера.
    fn command_task(id: &str, command: &str, checks: Vec<EvalCheck>) -> EvalTask {
        EvalTask {
            id: id.into(),
            title: format!("Тестовая задача {id}"),
            command: Some(command.into()),
            prompt: None,
            rubric: None,
            pass_threshold: None,
            checks,
            timeout_secs: 10,
        }
    }

    /// Контекст сьюта во временном каталоге (без плейсхолдера {arch}).
    fn test_ctx(home: &Path) -> SuiteContext {
        SuiteContext {
            cwd: home.to_path_buf(),
            arch: "arch".into(),
            home: home.to_path_buf(),
        }
    }

    #[test]
    fn task_yaml_parses_all_check_types() {
        let yaml = "\
id: demo
title: Демо-задача
command: \"{arch} models\"
checks:
  - type: command_succeeds
  - type: must_contain
    pattern: \"deepseek\"
  - type: must_not_contain
    pattern: \"panic\"
  - type: json_field
    pointer: \"/status\"
    equals: \"ok\"
timeout_secs: 30
";
        let task: EvalTask = serde_yaml_ng::from_str(yaml).expect("parse");
        assert_eq!(task.id, "demo");
        assert_eq!(task.checks.len(), 4);
        assert_eq!(task.timeout_secs, 30);
        assert!(matches!(task.checks[0], EvalCheck::CommandSucceeds));
        assert!(matches!(
            task.checks[3],
            EvalCheck::JsonField { ref pointer, .. } if pointer == "/status"
        ));
    }

    #[test]
    fn default_timeout_applies() {
        let task: EvalTask = serde_yaml_ng::from_str(
            "id: x\ntitle: t\ncommand: \"true\"\nchecks:\n  - type: command_succeeds\n",
        )
        .expect("parse");
        assert_eq!(task.timeout_secs, DEFAULT_TIMEOUT_SECS);
    }

    /// Валидирует yaml-задачу, ожидая ошибку с фрагментом сообщения.
    fn expect_invalid(yaml: &str, needle: &str) {
        let task: EvalTask = serde_yaml_ng::from_str(yaml).expect("yaml парсится");
        let err = validate_task(&task, Path::new("task.yaml")).expect_err("должно быть невалидно");
        assert!(err.to_string().contains(needle), "{err} ~ {needle}");
    }

    #[test]
    fn validate_rejects_bad_tasks() {
        expect_invalid(
            "id: \"\"\ntitle: t\ncommand: \"true\"\nchecks:\n  - type: command_succeeds\n",
            "пустой id",
        );
        expect_invalid(
            "id: x\ntitle: t\nchecks:\n  - type: command_succeeds\n",
            "command/prompt",
        );
        expect_invalid(
            "id: x\ntitle: t\ncommand: \"true\"\n",
            "нет ни детерминированных",
        );
        expect_invalid(
            "id: x\ntitle: t\ncommand: \"true\"\nchecks:\n  - type: must_contain\n    pattern: \"[unclosed\"\n",
            "невалидный regex",
        );
        expect_invalid(
            "id: x\ntitle: t\nprompt: \"привет\"\nchecks:\n  - type: command_succeeds\n",
            "только к command-задачам",
        );
        expect_invalid(
            "id: x\ntitle: t\ncommand: \"true\"\nchecks:\n  - type: json_field\n    pointer: \"status\"\n",
            "начинаться с '/'",
        );
    }

    #[test]
    fn load_suite_sorts_and_rejects_broken() {
        let dir = tempfile::tempdir().expect("tempdir");
        let task = |id: &str| {
            format!(
                "id: {id}\ntitle: Задача {id}\ncommand: \"true\"\nchecks:\n  - type: command_succeeds\n"
            )
        };
        std::fs::write(dir.path().join("b.yaml"), task("b-task")).expect("write");
        std::fs::write(dir.path().join("a.yaml"), task("a-task")).expect("write");
        std::fs::write(dir.path().join("NOTES.md"), "не задача").expect("write");
        let tasks = load_suite(dir.path()).expect("load");
        assert_eq!(tasks.len(), 2);
        assert_eq!(tasks[0].id, "a-task", "сортировка по id");
        assert_eq!(tasks[1].id, "b-task");

        // Битый файл — ошибка сьюта (гейт не должен молча терять задачи).
        std::fs::write(dir.path().join("broken.yaml"), "id: [unclosed").expect("write");
        let err = load_suite(dir.path()).expect_err("битый файл");
        assert!(err.to_string().contains("разбор задачи"), "{err}");
        std::fs::remove_file(dir.path().join("broken.yaml")).expect("rm");

        // Дубль id — ошибка.
        std::fs::write(dir.path().join("dup.yaml"), task("a-task")).expect("write");
        let err = load_suite(dir.path()).expect_err("дубль id");
        assert!(err.to_string().contains("дублирующийся id"), "{err}");

        // Пустой каталог — ошибка.
        let empty = tempfile::tempdir().expect("tempdir");
        let err = load_suite(empty.path()).expect_err("пустой сьют");
        assert!(err.to_string().contains("сьют пуст"), "{err}");
    }

    #[tokio::test]
    async fn run_command_captures_output_code_and_timeout() {
        let dir = tempfile::tempdir().expect("tempdir");
        let ctx = test_ctx(dir.path());
        let out = run_command(
            "echo привет; echo ошибка >&2; exit 3",
            &ctx,
            Duration::from_secs(5),
        )
        .await
        .expect("run");
        assert_eq!(out.code, Some(3));
        assert!(!out.timed_out);
        assert!(out.output.contains("привет"), "stdout: {}", out.output);
        assert!(out.output.contains("ошибка"), "stderr: {}", out.output);

        let out = run_command("sleep 5", &ctx, Duration::from_millis(300))
            .await
            .expect("run");
        assert!(out.timed_out, "sleep обязан быть убит по таймауту");
        assert_eq!(out.code, None);
    }

    #[test]
    fn expand_command_substitutes_placeholders() {
        let ctx = SuiteContext {
            cwd: PathBuf::from("/tmp"),
            arch: "\"/usr/bin/arch\" --config \"/tmp/c.toml\"".into(),
            home: PathBuf::from("/tmp/home"),
        };
        let cmd = expand_command("{arch} skills list > {home}/out.txt", &ctx);
        assert_eq!(
            cmd,
            "\"/usr/bin/arch\" --config \"/tmp/c.toml\" skills list > \"/tmp/home\"/out.txt"
        );
    }

    /// Прогоняет одну детерминированную проверку против заданного вывода.
    fn check_once(check: &EvalCheck, subject: &str, code: Option<i32>) -> CheckResult {
        let outcome = CommandOutcome {
            output: subject.into(),
            code,
            timed_out: false,
        };
        eval_check(check, subject, Some(&outcome)).expect("check")
    }

    #[test]
    fn check_must_contain_and_must_not_contain() {
        let c = check_once(
            &EvalCheck::MustContain {
                pattern: "deepseek".into(),
            },
            "модели: deepseek, glm",
            Some(0),
        );
        assert!(c.passed, "{}", c.detail);
        let c = check_once(
            &EvalCheck::MustContain {
                pattern: "kimi".into(),
            },
            "модели: deepseek, glm",
            Some(0),
        );
        assert!(!c.passed, "{}", c.detail);
        let c = check_once(
            &EvalCheck::MustNotContain {
                pattern: "panic".into(),
            },
            "всё спокойно",
            Some(0),
        );
        assert!(c.passed, "{}", c.detail);
        let c = check_once(
            &EvalCheck::MustNotContain {
                pattern: "panic".into(),
            },
            "тут panic в логе",
            Some(0),
        );
        assert!(!c.passed, "{}", c.detail);
        assert!(c.detail.contains("panic"), "{}", c.detail);
    }

    #[test]
    fn check_command_succeeds_and_json_field() {
        let c = check_once(&EvalCheck::CommandSucceeds, "ok", Some(0));
        assert!(c.passed, "{}", c.detail);
        let c = check_once(&EvalCheck::CommandSucceeds, "fail", Some(1));
        assert!(!c.passed, "{}", c.detail);

        let json = "{\"status\": \"ok\", \"stats\": {\"total\": 8}}";
        let c = check_once(
            &EvalCheck::JsonField {
                pointer: "/status".into(),
                equals: Some(serde_json::json!("ok")),
            },
            json,
            Some(0),
        );
        assert!(c.passed, "{}", c.detail);
        let c = check_once(
            &EvalCheck::JsonField {
                pointer: "/status".into(),
                equals: Some(serde_json::json!("fail")),
            },
            json,
            Some(0),
        );
        assert!(!c.passed, "{}", c.detail);
        let c = check_once(
            &EvalCheck::JsonField {
                pointer: "/stats/total".into(),
                equals: None,
            },
            json,
            Some(0),
        );
        assert!(c.passed, "{}", c.detail);
        let c = check_once(
            &EvalCheck::JsonField {
                pointer: "/missing".into(),
                equals: None,
            },
            json,
            Some(0),
        );
        assert!(!c.passed, "{}", c.detail);

        // Невалидный JSON — ошибка данных задачи, а не молчаливый FAIL.
        let outcome = CommandOutcome {
            output: "не json".into(),
            code: Some(0),
            timed_out: false,
        };
        let err = eval_check(
            &EvalCheck::JsonField {
                pointer: "/a".into(),
                equals: None,
            },
            "не json",
            Some(&outcome),
        )
        .expect_err("не-JSON вывод");
        assert!(err.to_string().contains("не валидный JSON"), "{err}");
    }

    #[tokio::test]
    async fn run_suite_counts_pass_rate_and_applies_gate() {
        let dir = tempfile::tempdir().expect("tempdir");
        let ctx = test_ctx(dir.path());
        let tasks = vec![
            command_task(
                "ok-task",
                "echo всё хорошо",
                vec![EvalCheck::MustContain {
                    pattern: "хорошо".into(),
                }],
            ),
            command_task(
                "bad-task",
                "echo всё хорошо",
                vec![EvalCheck::MustContain {
                    pattern: "отсутствует".into(),
                }],
            ),
        ];
        let report = run_suite_tasks(&tasks, &ctx, 100.0).await;
        assert_eq!(report.total, 2);
        assert_eq!(report.passed, 1);
        assert!((report.pass_rate - 50.0).abs() < 1e-9);
        assert!(!report.gate_passed, "50% < гейта 100%");
        let text = render_text(&report);
        assert!(text.contains("✗ bad-task"), "{text}");
        assert!(text.contains("FAIL"), "{text}");

        let report = run_suite_tasks(&tasks, &ctx, 50.0).await;
        assert!(report.gate_passed, "50% >= гейта 50%");

        let report = run_suite_tasks(&tasks[..1], &ctx, 100.0).await;
        assert!(report.gate_passed, "100% >= гейта 100%");
        assert!(render_text(&report).contains("PASS"));
    }

    /// Обертка прогона списка задач без чтения каталога (suite-имя фиктивное).
    async fn run_suite_tasks(tasks: &[EvalTask], ctx: &SuiteContext, gate: f64) -> SuiteReport {
        let mut reports = Vec::new();
        for task in tasks {
            reports.push(run_task(task, ctx, None).await.expect("task"));
        }
        let total = reports.len();
        let passed = reports.iter().filter(|r| r.passed).count();
        #[allow(clippy::cast_precision_loss)]
        let pass_rate = passed as f64 * 100.0 / total as f64;
        SuiteReport {
            suite: "test-suite".into(),
            judge_model: None,
            timestamp: "2026-08-26 00:00:00".into(),
            gate_pct: gate,
            total,
            passed,
            skipped: 0,
            pass_rate,
            gate_passed: pass_rate + f64::EPSILON >= gate,
            tasks: reports,
        }
    }

    #[tokio::test]
    async fn prompt_task_is_skipped_without_judge() {
        let dir = tempfile::tempdir().expect("tempdir");
        let ctx = test_ctx(dir.path());
        let task = EvalTask {
            id: "prompt-task".into(),
            title: "Задача с промптом".into(),
            command: None,
            prompt: Some("Расскажи о сагах".into()),
            rubric: None,
            pass_threshold: None,
            checks: vec![EvalCheck::MustContain {
                pattern: "сага".into(),
            }],
            timeout_secs: 10,
        };
        let report = run_task(&task, &ctx, None).await.expect("run");
        assert!(report.skipped);
        assert!(!report.passed, "пропуск не входит в знаменатель pass-rate");
    }

    /// Рубрика-пример для судьи (как в тестах bench).
    const RUBRIC_YAML: &str = "\
name: core
description: Базовая рубрика
scale_max: 5
origin: anchor
criteria:
  - id: context
    name: Контекст
    description: Описан контекст
    weight: 1.0
    anchors:
      1: нет
      3: частично
      5: полный
";

    /// Фейк-провайдер: на задачу отвечает `answer`, на судейский промпт — `judge`.
    /// Различение по числу сообщений: запрос задачи — одно user-сообщение,
    /// судейский запрос рубрики — system + user (ADR-004).
    #[derive(Debug)]
    struct FakeLlm {
        answer: String,
        judge: String,
    }

    #[async_trait]
    impl LlmProvider for FakeLlm {
        fn name(&self) -> &'static str {
            "fake"
        }
        fn model(&self) -> &'static str {
            "fake-model"
        }
        async fn complete(&self, req: ChatRequest) -> Result<ChatMessage> {
            let is_judge = req.messages.len() > 1;
            let content = if is_judge {
                self.judge.clone()
            } else {
                self.answer.clone()
            };
            Ok(ChatMessage::assistant(content, Vec::new()))
        }
    }

    #[tokio::test]
    async fn prompt_task_with_judge_passes_above_threshold() {
        let dir = tempfile::tempdir().expect("tempdir");
        let rubrics = dir.path().join("rubrics");
        std::fs::create_dir_all(&rubrics).expect("mkdir");
        std::fs::write(rubrics.join("core.yaml"), RUBRIC_YAML).expect("rubric");
        let ctx = test_ctx(dir.path());
        let llm = FakeLlm {
            answer: "Сага — это последовательность локальных транзакций.".into(),
            // Цитата — дословный фрагмент ответа (верификация свидетельств, ADR-004).
            judge: "{\"scores\": [{\"criterion_id\": \"context\", \"score\": 5, \
                    \"rationale\": \"Цитата: \\\"последовательность локальных транзакций\\\"\"}], \
                    \"verdict\": \"годно\"}"
                .into(),
        };
        let judge_cfg = JudgeConfig {
            samples: 1,
            ..JudgeConfig::default()
        };
        let judge = JudgeCtx {
            provider: &llm,
            cfg: &judge_cfg,
            rubrics_dir: &rubrics,
        };
        let task = EvalTask {
            id: "saga-prompt".into(),
            title: "Модель объясняет саги".into(),
            command: None,
            prompt: Some("Что такое сага?".into()),
            rubric: Some("core".into()),
            pass_threshold: Some(4.0),
            checks: vec![EvalCheck::MustContain {
                pattern: "Сага".into(),
            }],
            timeout_secs: 10,
        };
        let report = run_task(&task, &ctx, Some(&judge)).await.expect("run");
        assert!(!report.skipped);
        assert!(report.passed, "проверки + судья 5.0 >= 4.0");
        assert_eq!(report.judge_score, Some(5.0));
        assert_eq!(report.judge_passed, Some(true));

        // Судья ниже порога — задача не прошла, даже при зелёных проверках.
        let llm_low = FakeLlm {
            answer: "Сага — это последовательность локальных транзакций.".into(),
            judge: "{\"scores\": [{\"criterion_id\": \"context\", \"score\": 2, \
                    \"rationale\": \"Цитата: \\\"Сага — это\\\" — слабо\"}], \
                    \"verdict\": \"плохо\"}"
                .into(),
        };
        let judge_low = JudgeCtx {
            provider: &llm_low,
            cfg: &judge_cfg,
            rubrics_dir: &rubrics,
        };
        let report = run_task(&task, &ctx, Some(&judge_low)).await.expect("run");
        assert!(!report.passed, "судья 2.0 < порога 4.0");
        assert_eq!(report.judge_passed, Some(false));
    }

    #[test]
    fn prepare_builtin_home_deploys_suite_and_config() {
        let dir = tempfile::tempdir().expect("tempdir");
        let home = prepare_builtin_home(dir.path()).expect("prepare");
        assert!(home.suite_dir.is_dir(), "сьют развёрнут");
        assert!(home.config.is_file(), "конфиг записан");
        // Конфиг герметичен: все пути смотрят внутрь дома.
        let cfg: Config =
            toml::from_str(&std::fs::read_to_string(&home.config).expect("read")).expect("toml");
        assert_eq!(cfg.paths.assets_dir, dir.path().join("assets"));
        assert_eq!(cfg.plugins.dirs, vec![dir.path().join("plugins")]);
        // Развёрнутый сьют валиден как набор задач.
        let tasks = load_suite(&home.suite_dir).expect("suite loads");
        assert!(
            tasks.len() >= 5,
            "встроенный сьют: 5+ задач, фактически {}",
            tasks.len()
        );
    }

    #[test]
    fn builtin_suite_assets_are_valid_offline_commands() {
        // Встроенный сьют — только офлайн-команды через {arch}: судья и
        // prompt-задачи сделали бы прогон негерметичным (CI без ключей).
        let texts = [
            crate::assets::EVAL_AGENT_CONFIG_MODELS_REGISTRY,
            crate::assets::EVAL_AGENT_CONFIG_SKILLS_LIBRARY,
            crate::assets::EVAL_AGENT_CONFIG_RUBRICS_LIBRARY,
            crate::assets::EVAL_AGENT_CONFIG_BENCH_CATALOG,
            crate::assets::EVAL_AGENT_CONFIG_MERMAID_RENDER,
            crate::assets::EVAL_AGENT_CONFIG_CONTROL_SCORE_FAST,
            crate::assets::EVAL_AGENT_CONFIG_CONTROL_SCORE_CRITICAL,
            crate::assets::EVAL_AGENT_CONFIG_POLICY_DENIES_DESTRUCTIVE,
        ];
        for text in texts {
            let task: EvalTask = serde_yaml_ng::from_str(text).expect("задача парсится");
            validate_task(&task, Path::new("builtin")).expect("задача валидна");
            let cmd = task.command.as_deref().expect("command-задача");
            assert!(cmd.contains("{arch}"), "{}: команда без {{arch}}", task.id);
            assert!(
                task.prompt.is_none(),
                "{}: prompt во встроенном сьюте",
                task.id
            );
            assert!(
                task.rubric.is_none(),
                "{}: рубрика во встроенном сьюте",
                task.id
            );
            assert!(
                task.checks
                    .iter()
                    .any(|c| matches!(c, EvalCheck::CommandSucceeds)),
                "{}: нет command_succeeds",
                task.id
            );
        }
    }

    #[test]
    fn report_json_roundtrips() {
        let dir = tempfile::tempdir().expect("tempdir");
        let report = SuiteReport {
            suite: "agent-config".into(),
            judge_model: None,
            timestamp: "2026-08-26 00:00:00".into(),
            gate_pct: 100.0,
            total: 8,
            passed: 8,
            skipped: 0,
            pass_rate: 100.0,
            gate_passed: true,
            tasks: vec![TaskReport {
                id: "x".into(),
                title: "t".into(),
                skipped: false,
                skip_reason: None,
                passed: true,
                checks: vec![CheckResult {
                    check: "command_succeeds".into(),
                    passed: true,
                    detail: "код выхода 0".into(),
                }],
                judge_score: None,
                judge_threshold: None,
                judge_passed: None,
                output_excerpt: "ok".into(),
            }],
        };
        let path = write_report(&report, dir.path()).expect("write");
        assert!(
            path.file_name()
                .expect("имя")
                .to_string_lossy()
                .starts_with("eval-agent-config-")
        );
        let back: SuiteReport =
            serde_json::from_str(&std::fs::read_to_string(&path).expect("read")).expect("parse");
        assert!(back.gate_passed);
        assert_eq!(back.tasks.len(), 1);
        assert!((back.pass_rate - 100.0).abs() < 1e-9);
    }
}
