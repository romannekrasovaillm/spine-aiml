//! Планировщик md-задач: крон + LLM + баш-пайпы.
//!
//! КОНТРАКТ (владелец: агент `tui-cron`):
//! - расписание `cron.toml`: `[[job]] name, schedule (5-полевой cron),
//!   task_md (файл с инструкцией), model (опц.), out (каталог отчётов)`;
//! - [`due_jobs`] — какие задачи пора запустить (cron-parser, сравнение с
//!   «последний тик»);
//! - [`run_job`] — md-инструкция → LLM (с ядерными инструментами: баш-пайпы!)
//!   → отчёт markdown в out/<job>-<timestamp>.md; headless-контракт результата:
//!   JSON-статус complete/partial/blocked в конце отчёта;
//! - принцип: задача — это md; исполнитель — LLM с bash; тайминг — cron.
//!
//! [`crate::agent::AgentSession`] здесь намеренно не используется: ему нужен
//! `Arc<Config>`, которого нет в замороженной сигнатуре [`run_job`]. Вместо
//! этого — локальный лёгкий цикл «модель ↔ инструменты» ([`agent_loop`]).

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use chrono::{DateTime, Local};
use serde::{Deserialize, Serialize};

use crate::config::Config;
use crate::error::{HarnessError, Result};
use crate::llm::{ChatMessage, ChatRequest, LlmProvider};
use crate::tool::{ToolContext, ToolRegistry};

/// Системный промпт исполнителя плановой задачи. Финальная строка ответа —
/// JSON-статус (headless-контракт результата).
const SYSTEM_PROMPT: &str = "Ты — исполнитель плановой архитектурной задачи. \
Выполни инструкцию из задания. Можешь пользоваться инструментами (bash, файлы). \
Ответь отчётом в markdown. Последней строкой ОБЯЗАТЕЛЬНО JSON: \
{\"status\": \"complete|partial|blocked\", \"summary\": \"…\"}";

/// Лимит итераций «модель ↔ инструменты» внутри одной задачи.
const MAX_TURNS: usize = 16;

/// Одна задача расписания.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CronJob {
    /// Имя задачи.
    pub name: String,
    /// 5-полевое cron-выражение (минута час день месяц день-недели).
    pub schedule: String,
    /// Файл с md-инструкцией.
    pub task_md: PathBuf,
    /// Модель (None — дефолтная).
    pub model: Option<String>,
    /// Каталог отчётов (None — `reports_dir/cron`).
    pub out: Option<PathBuf>,
}

/// Расписание (cron.toml).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CronTab {
    /// Задачи.
    #[serde(default, rename = "job")]
    pub jobs: Vec<CronJob>,
}

/// Загружает расписание из toml-файла (`[[job]]` → [`CronTab::jobs`]).
///
/// # Errors
/// - файл не существует → [`HarnessError::Cron`] с подсказкой про `cron.example.toml`;
/// - файл не читается → [`HarnessError::Io`];
/// - битый toml → [`HarnessError::Toml`].
pub fn load(path: &Path) -> Result<CronTab> {
    let text = std::fs::read_to_string(path).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            HarnessError::Cron(format!(
                "файл расписания '{}' не найден; создайте его по образцу cron.example.toml",
                path.display()
            ))
        } else {
            HarnessError::io(path, e)
        }
    })?;
    Ok(toml::from_str(&text)?)
}

/// Задачи, которые должны сработать между `last` (последний тик) и `now`.
///
/// Чистая детерминированная функция: ближайшее срабатывание после `last`
/// считается через `cron-parser`; если оно `<= now`, задача дюжная.
/// Задачи с битым cron-выражением пропускаются (предупреждение в лог,
/// без паники).
#[must_use]
pub fn due_jobs(tab: &CronTab, last: DateTime<Local>, now: DateTime<Local>) -> Vec<CronJob> {
    tab.jobs
        .iter()
        .filter(|job| is_due(job, last, now))
        .cloned()
        .collect()
}

/// Выполняет задачу: читает md-инструкцию, гоняет локальный цикл
/// «LLM ↔ инструменты» и пишет markdown-отчёт
/// `reports_dir/<name>-<yyyymmdd-HHMMSS>.md`. Возвращает путь к отчёту.
///
/// Отчёт: шапка (задача, расписание, модель, дата, длительность, статус),
/// тело ответа модели. Статус извлекается из последней строки ответа
/// (`{"status": …, "summary": …}`); не распарсилась — `unknown`.
///
/// # Errors
/// - `job.task_md` не читается → [`HarnessError::Io`];
/// - ошибка модели → [`HarnessError::Llm`];
/// - модель не выдала финальный текст за [`MAX_TURNS`] итераций → [`HarnessError::Cron`];
/// - ошибка создания каталога / записи отчёта → [`HarnessError::Io`].
pub async fn run_job(
    job: &CronJob,
    llm: &dyn LlmProvider,
    tools: &ToolRegistry,
    reports_dir: &Path,
) -> Result<PathBuf> {
    let started = Instant::now();
    let at = Local::now();
    let task_md = tokio::fs::read_to_string(&job.task_md)
        .await
        .map_err(|e| HarnessError::io(&job.task_md, e))?;

    // Конфиг инструментам нужен только как контекст; сигнатура заморожена,
    // поэтому берём дефолтный. cwd — текущий каталог процесса.
    let ctx = ToolContext::new(std::env::current_dir()?, Arc::new(Config::default()));
    let answer = agent_loop(llm, tools, &ctx, &task_md).await?;

    let (status, summary) = extract_status(&answer);
    let report = render_report(job, llm, at, started.elapsed(), &answer, &status, &summary);

    tokio::fs::create_dir_all(reports_dir)
        .await
        .map_err(|e| HarnessError::io(reports_dir, e))?;
    let file = reports_dir.join(format!(
        "{}-{}.md",
        sanitize_file_name(&job.name),
        at.format("%Y%m%d-%H%M%S")
    ));
    tokio::fs::write(&file, report)
        .await
        .map_err(|e| HarnessError::io(&file, e))?;
    Ok(file)
}

/// Тик планировщика (для `arch-ml cron tick`): выполняет все дюжные между
/// `last` и `now` задачи последовательно, возвращает пути к отчётам.
///
/// # Errors
/// Первая ошибка [`run_job`] прерывает тик; уже записанные отчёты остаются.
pub async fn run_due(
    tab: &CronTab,
    last: DateTime<Local>,
    now: DateTime<Local>,
    llm: &dyn LlmProvider,
    tools: &ToolRegistry,
    reports_dir: &Path,
) -> Result<Vec<PathBuf>> {
    let mut reports = Vec::new();
    for job in due_jobs(tab, last, now) {
        reports.push(run_job(&job, llm, tools, reports_dir).await?);
    }
    Ok(reports)
}

/// Признак дюжности одной задачи: срабатывание после `last` не позже `now`.
fn is_due(job: &CronJob, last: DateTime<Local>, now: DateTime<Local>) -> bool {
    // cron-parser 0.8 индексирует поля без проверки длины и паникует на
    // выражениях короче 5 полей — отсекаем их до вызова.
    if job.schedule.split_whitespace().count() != 5 {
        tracing::warn!(job = %job.name, schedule = %job.schedule, "cron: задача пропущена — выражение не из 5 полей");
        return false;
    }
    match cron_parser::parse(&job.schedule, &last) {
        Ok(next) => next <= now,
        Err(e) => {
            tracing::warn!(job = %job.name, error = %e, "cron: задача пропущена — битое расписание");
            false
        }
    }
}

/// Локальный агентный цикл: system-промпт + md-задание, затем итерации
/// «модель → вызовы инструментов → результаты», пока модель не ответит
/// текстом без `tool_calls` (лимит — [`MAX_TURNS`]).
async fn agent_loop(
    llm: &dyn LlmProvider,
    tools: &ToolRegistry,
    ctx: &ToolContext,
    task_md: &str,
) -> Result<String> {
    let mut messages = vec![
        ChatMessage::system(SYSTEM_PROMPT),
        ChatMessage::user(task_md),
    ];
    let specs = tools.specs();
    for _ in 0..MAX_TURNS {
        let req = ChatRequest::chat(messages.clone()).with_tools(specs.clone());
        let msg = llm.complete(req).await?;
        if msg.tool_calls.is_empty() {
            return Ok(msg.content);
        }
        messages.push(ChatMessage::assistant(msg.content, msg.tool_calls.clone()));
        for call in &msg.tool_calls {
            let out = tools
                .dispatch(&call.name, call.arguments.clone(), ctx)
                .await;
            let content = if out.is_error {
                format!("ОШИБКА: {}", out.content)
            } else {
                out.content
            };
            messages.push(ChatMessage::tool_result(call.id.clone(), content));
        }
    }
    Err(HarnessError::Cron(format!(
        "модель не завершила задачу за {MAX_TURNS} итераций инструментов"
    )))
}

/// Извлекает headless-статус из последней непустой строки ответа.
/// Возвращает `(status, summary)`; если строка — не JSON с полем `status`,
/// то `("unknown", "")`.
fn extract_status(answer: &str) -> (String, String) {
    let unknown = || ("unknown".to_string(), String::new());
    let Some(line) = answer.lines().rev().find(|l| !l.trim().is_empty()) else {
        return unknown();
    };
    let line = line.trim();
    if !line.starts_with('{') {
        return unknown();
    }
    let Ok(json) = serde_json::from_str::<serde_json::Value>(line) else {
        return unknown();
    };
    let Some(status) = json.get("status").and_then(|v| v.as_str()) else {
        return unknown();
    };
    let summary = json
        .get("summary")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    (status.to_string(), summary)
}

/// Имя задачи → безопасный фрагмент имени файла (буквы/цифры/`-`/`_`,
/// остальное заменяется на `-`).
fn sanitize_file_name(name: &str) -> String {
    name.chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect()
}

/// Текст markdown-отчёта: шапка с метаданными + тело ответа модели.
fn render_report(
    job: &CronJob,
    llm: &dyn LlmProvider,
    at: DateTime<Local>,
    elapsed: std::time::Duration,
    answer: &str,
    status: &str,
    summary: &str,
) -> String {
    let summary_line = if summary.is_empty() {
        String::new()
    } else {
        format!(" — {summary}")
    };
    format!(
        "# Отчёт cron-задачи: {name}\n\n\
         - Задача: `{task}`\n\
         - Расписание: `{schedule}`\n\
         - Модель: `{provider}/{model}`\n\
         - Дата: {date}\n\
         - Длительность: {elapsed:.1} с\n\
         - Статус: **{status}**{summary_line}\n\n\
         ---\n\n\
         {answer}\n",
        name = job.name,
        task = job.task_md.display(),
        schedule = job.schedule,
        provider = llm.name(),
        model = llm.model(),
        date = at.format("%Y-%m-%d %H:%M:%S %:z"),
        elapsed = elapsed.as_secs_f64(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    /// Провайдер-заглушка: сразу отвечает финальным отчётом с JSON-статусом.
    #[derive(Debug)]
    struct FakeLlm;

    #[async_trait::async_trait]
    impl LlmProvider for FakeLlm {
        fn name(&self) -> &'static str {
            "fake"
        }

        fn model(&self) -> &'static str {
            "fake-1"
        }

        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "отчёт\n{\"status\":\"complete\",\"summary\":\"ok\"}",
                Vec::new(),
            ))
        }
    }

    /// Момент 2026-08-14 HH:MM:00 локального времени (детерминированно:
    /// cron-parser сравнивает naive local time).
    fn dt(h: u32, m: u32) -> DateTime<Local> {
        Local.with_ymd_and_hms(2026, 8, 14, h, m, 0).unwrap()
    }

    /// Задача с минимальными полями.
    fn job(name: &str, schedule: &str) -> CronJob {
        CronJob {
            name: name.into(),
            schedule: schedule.into(),
            task_md: PathBuf::from("task.md"),
            model: None,
            out: None,
        }
    }

    #[test]
    fn load_parses_jobs_from_toml() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cron.toml");
        std::fs::write(
            &path,
            r#"
[[job]]
name = "daily-review"
schedule = "0 9 * * *"
task_md = "tasks/review.md"

[[job]]
name = "quarter"
schedule = "*/15 * * * *"
task_md = "tasks/q.md"
model = "deepseek-reasoner"
out = "out/quarter"
"#,
        )
        .unwrap();
        let tab = load(&path).unwrap();
        assert_eq!(tab.jobs.len(), 2);
        assert_eq!(tab.jobs[0].name, "daily-review");
        assert_eq!(tab.jobs[0].schedule, "0 9 * * *");
        assert_eq!(tab.jobs[0].task_md, PathBuf::from("tasks/review.md"));
        assert_eq!(tab.jobs[1].model.as_deref(), Some("deepseek-reasoner"));
        assert_eq!(tab.jobs[1].out, Some(PathBuf::from("out/quarter")));
    }

    #[test]
    fn load_missing_file_hints_cron_example() {
        let err = load(Path::new("/definitely/missing/cron.toml")).unwrap_err();
        match err {
            HarnessError::Cron(msg) => {
                assert!(msg.contains("cron.example.toml"), "подсказка: {msg}");
            }
            other => panic!("ожидался HarnessError::Cron, получено {other:?}"),
        }
    }

    #[test]
    fn load_invalid_toml_is_toml_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cron.toml");
        std::fs::write(&path, "[[[not toml").unwrap();
        assert!(matches!(load(&path), Err(HarnessError::Toml(_))));
    }

    #[test]
    fn due_jobs_marks_every_15_min_due_between_10_00_and_10_16() {
        let tab = CronTab {
            jobs: vec![job("a", "*/15 * * * *")],
        };
        let due = due_jobs(&tab, dt(10, 0), dt(10, 16));
        assert_eq!(due.len(), 1);
        assert_eq!(due[0].name, "a");
    }

    #[test]
    fn due_jobs_skips_daily_9am_at_10_16() {
        // «0 9 * * *» сработала в 9:00 (до последнего тика 10:00);
        // следующая — завтра в 9:00 > 10:16 → не дюжна.
        let tab = CronTab {
            jobs: vec![job("b", "0 9 * * *")],
        };
        assert!(due_jobs(&tab, dt(10, 0), dt(10, 16)).is_empty());
    }

    #[test]
    fn due_jobs_marks_daily_9am_due_when_window_covers_9am() {
        let tab = CronTab {
            jobs: vec![job("b", "0 9 * * *")],
        };
        assert_eq!(due_jobs(&tab, dt(8, 59), dt(10, 16)).len(), 1);
    }

    #[test]
    fn due_jobs_skips_broken_expressions_without_panic() {
        let tab = CronTab {
            jobs: vec![
                job("few-fields", "*/15 * *"), // паника в cron-parser без нашей предпроверки
                job("bad-fields", "x y z w v"), // Err(ParseError)
                job("empty", ""),
                job("ok", "*/15 * * * *"),
            ],
        };
        let due = due_jobs(&tab, dt(10, 0), dt(10, 16));
        assert_eq!(due.len(), 1);
        assert_eq!(due[0].name, "ok");
    }

    #[test]
    fn extract_status_parses_last_json_line() {
        let (status, summary) =
            extract_status("текст отчёта\n{\"status\":\"partial\",\"summary\":\"половина\"}\n");
        assert_eq!(status, "partial");
        assert_eq!(summary, "половина");
    }

    #[test]
    fn extract_status_unknown_when_no_json() {
        assert_eq!(extract_status("").0, "unknown");
        assert_eq!(extract_status("просто текст").0, "unknown");
        assert_eq!(extract_status("отчёт\n{битый json").0, "unknown");
        assert_eq!(extract_status("{\"summary\":\"без статуса\"}").0, "unknown");
    }

    #[test]
    fn sanitize_file_name_replaces_unsafe_chars() {
        assert_eq!(
            sanitize_file_name("обзор компонента/v2"),
            "обзор-компонента-v2"
        );
        assert_eq!(sanitize_file_name("a-b_c"), "a-b_c");
    }

    #[tokio::test]
    async fn run_job_writes_report_with_extracted_status() {
        let dir = tempfile::tempdir().unwrap();
        let task = dir.path().join("task.md");
        std::fs::write(&task, "Сделай обзор компонента.").unwrap();
        let reports = dir.path().join("reports");
        let j = CronJob {
            task_md: task,
            ..job("обзор компонента", "*/15 * * * *")
        };
        let path = run_job(&j, &FakeLlm, &ToolRegistry::new(), &reports)
            .await
            .unwrap();

        assert!(path.is_file());
        assert_eq!(path.parent(), Some(reports.as_path()));
        let name = path.file_name().unwrap().to_str().unwrap();
        assert!(name.starts_with("обзор-компонента-"), "имя файла: {name}");
        assert_eq!(path.extension().and_then(|e| e.to_str()), Some("md"));

        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.contains("# Отчёт cron-задачи: обзор компонента"));
        assert!(text.contains("- Расписание: `*/15 * * * *`"));
        assert!(text.contains("- Модель: `fake/fake-1`"));
        assert!(text.contains("- Статус: **complete** — ok"));
        assert!(text.contains("отчёт\n{\"status\":\"complete\",\"summary\":\"ok\"}"));
    }

    #[tokio::test]
    async fn run_job_missing_task_md_is_io_error() {
        let dir = tempfile::tempdir().unwrap();
        let j = CronJob {
            task_md: dir.path().join("no-such.md"),
            ..job("x", "*/15 * * * *")
        };
        let err = run_job(&j, &FakeLlm, &ToolRegistry::new(), dir.path())
            .await
            .unwrap_err();
        assert!(matches!(err, HarnessError::Io { .. }), "err: {err:?}");
    }

    #[tokio::test]
    async fn run_due_runs_only_due_jobs() {
        let dir = tempfile::tempdir().unwrap();
        let task = dir.path().join("task.md");
        std::fs::write(&task, "задание").unwrap();
        let tab = CronTab {
            jobs: vec![
                CronJob {
                    task_md: task.clone(),
                    ..job("due", "*/15 * * * *")
                },
                CronJob {
                    task_md: task,
                    ..job("not-due", "0 9 * * *")
                },
            ],
        };
        let reports = run_due(
            &tab,
            dt(10, 0),
            dt(10, 16),
            &FakeLlm,
            &ToolRegistry::new(),
            dir.path(),
        )
        .await
        .unwrap();
        assert_eq!(reports.len(), 1);
        assert!(reports[0].is_file());
        assert!(
            reports[0]
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .starts_with("due-")
        );
    }
}
