//! Ralph-цикл: итеративная работа к неизменной цели свежими агентами.
//!
//! Идея — `DeepSeek` Harness (`docs/glossary.md`, «Ralph loop»): раунд —
//! СВЕЖАЯ дочерняя сессия без родительского и предшественного контекста;
//! междураундное состояние несут общий workspace (файлы рабочего каталога)
//! и один ограниченный структурированный handoff (status/summary/evidence/
//! `next_steps/blockers`). Свежесть контекста — главное свойство: цикл не
//! деградирует от накопленной истории и дёшев по токенам.
//!
//! КОНТРАКТ (владелец: агент `subagent`):
//! - инструмент `ralph_run` запускает цикл в фоне; цикл занимает один слот
//!   общего реестра ([`crate::subagent::SubagentRegistry`], лимит
//!   [`crate::subagent::MAX_RUNNING`]), статусы — `subagent_list`, итог —
//!   `subagent_result(id="ralph-…")`;
//! - каждый раунд — свежая [`crate::agent::AgentSession`] с реестром
//!   инструментов минус оркестрация (`subagent_*`, `ralph_run`); whitelist
//!   спеки (`plugins/*/agents/*.md`) сужает набор дальше;
//! - раунд обязан завершиться блоком ```` ```handoff ```` с JSON
//!   [`RalphHandoff`]; блока нет или он битый — синтезируется fallback
//!   (status=continue, summary из начала отчёта);
//! - стоп-условия: `status=done` (цель достигнута), `status=blocked`
//!   (нужно решение архитектора), исчерпание раундов (дефолт 3, максимум
//!   [`MAX_ROUNDS`]), ошибка модели;
//! - артефакты: `reports/ralph/<id>/round-NN.md` (полный ответ раунда),
//!   `handoff-NN.json`, `FINAL.md` (сводка цикла) — аудит цикла вне сессии.

use std::fmt::Write as _;
use std::sync::Arc;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::config::Config;
use crate::error::Result;
use crate::llm::{LlmProvider, ToolSpec};
use crate::subagent::{
    SUBAGENT_TOOLS, SubagentRegistry, SubagentSpec, SubagentTask, TaskStatus, available_specs,
    general_spec, now_iso,
};
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Жёсткий максимум раундов одного цикла (бюджет токенов и времени).
const MAX_ROUNDS: usize = 6;
/// Дефолтное число раундов.
const DEFAULT_ROUNDS: usize = 3;
/// Лимит summary из handoff в сводке реестра (символов).
const SUMMARY_IN_LOG_CHARS: usize = 300;

/// Системный промпт рабочего агента ralph-цикла.
const RALPH_SYSTEM: &str = "Ты — рабочий агент ralph-цикла solution-архитектора AI/ML. \
Отрабатываешь ОДИН раунд над неизменной целью. Памяти прошлых раундов у тебя нет: \
общее состояние — в файлах рабочего каталога и в handoff предшественника. \
Правила раунда: (1) сверься с тем, что уже сделано в файлах, — не переделывай; \
(2) сделай один измеримый шаг к цели; (3) фиксируй результаты в файлах — \
следующий раунд увидит только их. \
Ответ заверши блоком ```handoff с JSON: \
{\"status\":\"continue|done|blocked\",\"summary\":\"что сделано\",\"evidence\":[\"файл:строка или команда → результат\"],\"next_steps\":[\"шаг\"],\"blockers\":[\"препятствие\"]}. \
status=done — цель достигнута полностью; blocked — дальше нельзя без решения архитектора \
или внешнего условия; continue — нужен следующий раунд. До блока handoff — краткий отчёт о раунде.";

/// Ограниченный структурированный handoff между раундами ralph-цикла.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RalphHandoff {
    /// Нормализованный статус: `continue` | `done` | `blocked`.
    pub status: String,
    /// Что сделано за раунд (кратко).
    #[serde(default)]
    pub summary: String,
    /// Свидетельства: файл:строка, команда → результат.
    #[serde(default)]
    pub evidence: Vec<String>,
    /// Шаги для следующего раунда.
    #[serde(default)]
    pub next_steps: Vec<String>,
    /// Препятствия (обязательны при `blocked`).
    #[serde(default)]
    pub blockers: Vec<String>,
}

/// Нормализует статус из модели к `continue`/`done`/`blocked`.
fn normalize_status(raw: &str) -> &'static str {
    let s = raw.trim().to_lowercase();
    if s.contains("done") || s.contains("complete") || s.contains("готов") {
        "done"
    } else if s.contains("block") || s.contains("стоп") {
        "blocked"
    } else {
        "continue"
    }
}

/// Извлекает handoff из отчёта раунда: ПОСЛЕДНИЙ блок ```` ```handoff ````
/// (модель может привести пример формата раньше — берём финальный).
/// Битый или отсутствующий JSON → `None` (синтезирует [`fallback_handoff`]).
fn parse_handoff(report: &str) -> Option<RalphHandoff> {
    let start = report.rfind("```handoff")?;
    let after = &report[start + "```handoff".len()..];
    let end = after.find("```")?;
    let body = after[..end].trim();
    let mut handoff: RalphHandoff = serde_json::from_str(body).ok()?;
    handoff.status = normalize_status(&handoff.status).to_string();
    Some(handoff)
}

/// Handoff по умолчанию для раунда без валидного блока: цикл продолжается,
/// summary — начало отчёта (модель увидела задачу, но формат проигнорировала).
fn fallback_handoff(report: &str) -> RalphHandoff {
    RalphHandoff {
        status: "continue".into(),
        summary: report.trim().chars().take(500).collect(),
        evidence: Vec::new(),
        next_steps: Vec::new(),
        blockers: Vec::new(),
    }
}

/// Промпт раунда: цель (неизменна) + контекст заказчика (1-й раунд)
/// + handoff предшественника (2+ раунды).
fn round_prompt(
    objective: &str,
    round: usize,
    max_rounds: usize,
    prev: Option<&RalphHandoff>,
    context: Option<&str>,
) -> String {
    let mut p =
        format!("ЦЕЛЬ (неизменна весь цикл): {objective}\n\nРаунд {round} из {max_rounds}.");
    if let Some(ctx) = context.map(str::trim).filter(|s| !s.is_empty()) {
        let _ = write!(
            p,
            "\n\nКонтекст от заказчика:\n{}",
            ctx.chars().take(8000).collect::<String>()
        );
    }
    if let Some(hb) = prev {
        let json = serde_json::to_string_pretty(hb).unwrap_or_default();
        let _ = write!(
            p,
            "\n\nHandoff предыдущего раунда:\n```handoff\n{json}\n```"
        );
    }
    p
}

/// Сводка цикла для реестра задач и `FINAL.md`.
fn cycle_report(
    id: &str,
    objective: &str,
    stop_reason: &str,
    rounds_log: &[String],
    out_dir: &std::path::Path,
) -> String {
    let mut r = format!(
        "Ralph-цикл {id} — {stop_reason}.\nЦель: {}\n",
        objective.chars().take(300).collect::<String>()
    );
    for line in rounds_log {
        let _ = write!(r, "\n{line}");
    }
    let _ = write!(r, "\n\nАртефакты раундов: {}", out_dir.display());
    r
}

/// Запускает ralph-цикл фоновой задачей; возвращает id (`ralph-…`).
///
/// # Errors
/// Все слоты фоновых задач заняты ([`MAX_RUNNING`]).
#[allow(clippy::too_many_arguments)]
pub(crate) fn launch_ralph(
    registry: &SubagentRegistry,
    spec: &SubagentSpec,
    objective: &str,
    max_rounds: usize,
    context: Option<&str>,
    provider: Arc<dyn LlmProvider>,
    tool_ctx: ToolContext,
) -> crate::error::Result<String> {
    if registry.running() >= registry.capacity() {
        return Err(crate::error::HarnessError::Agent(format!(
            "все слоты фоновых задач заняты ({}); дождитесь завершения — subagent_list",
            registry.capacity()
        )));
    }
    let id = registry.next_id("ralph");
    registry.insert(SubagentTask {
        id: id.clone(),
        agent: format!("ralph({})", spec.name),
        task: objective.chars().take(2000).collect(),
        status: TaskStatus::Running,
        report: String::new(),
        started_at: now_iso(),
        finished_at: None,
    });

    // Инструменты раунда: полный реестр минус оркестрация; whitelist спеки — поверх.
    let mut tools = crate::tools::full_registry(&tool_ctx.config).excluding(&SUBAGENT_TOOLS);
    if !spec.tools.is_empty() {
        tools = tools.subset(&spec.tools);
    }
    let objective = objective.to_string();
    let context = context.map(str::to_string);
    let registry2 = registry.clone();
    let task_id = id.clone();
    tokio::spawn(async move {
        let out_dir = tool_ctx
            .config
            .paths
            .reports_dir
            .join("ralph")
            .join(&task_id);
        if let Err(e) = std::fs::create_dir_all(&out_dir) {
            tracing::warn!(
                "ralph: каталог отчётов не создан {}: {e}",
                out_dir.display()
            );
        }
        let mut prev: Option<RalphHandoff> = None;
        let mut rounds_log: Vec<String> = Vec::new();
        let mut status = TaskStatus::Done;
        let mut stop_reason = format!("раунды исчерпаны ({max_rounds})");
        for round in 1..=max_rounds {
            let prompt = round_prompt(
                &objective,
                round,
                max_rounds,
                prev.as_ref(),
                context.as_deref(),
            );
            let mut session = crate::agent::AgentSession::new(
                tool_ctx.config.clone(),
                provider.clone(),
                tools.clone(),
                tool_ctx.clone(),
                RALPH_SYSTEM.to_string(),
            );
            match session.send(&prompt, None).await {
                Ok(text) => {
                    let handoff = parse_handoff(&text).unwrap_or_else(|| fallback_handoff(&text));
                    // Артефакты раунда (best effort: аудит не должен ронять цикл).
                    let round_file = out_dir.join(format!("round-{round:02}.md"));
                    if let Err(e) = std::fs::write(
                        &round_file,
                        format!("# Ralph-цикл {task_id}, раунд {round}\n\n{text}\n"),
                    ) {
                        tracing::warn!(
                            "ralph: отчёт раунда не записан {}: {e}",
                            round_file.display()
                        );
                    }
                    let handoff_file = out_dir.join(format!("handoff-{round:02}.json"));
                    if let Ok(json) = serde_json::to_string_pretty(&handoff) {
                        if let Err(e) = std::fs::write(&handoff_file, json) {
                            tracing::warn!(
                                "ralph: handoff не записан {}: {e}",
                                handoff_file.display()
                            );
                        }
                    }
                    rounds_log.push(format!(
                        "Раунд {round} [{}]: {}",
                        handoff.status,
                        handoff
                            .summary
                            .chars()
                            .take(SUMMARY_IN_LOG_CHARS)
                            .collect::<String>()
                    ));
                    let round_status = handoff.status.clone();
                    prev = Some(handoff);
                    if round_status == "done" {
                        stop_reason = format!("цель достигнута за {round} раунд(а)");
                        break;
                    }
                    if round_status == "blocked" {
                        stop_reason = format!("остановлено блокерами на раунде {round}");
                        break;
                    }
                }
                Err(e) => {
                    status = TaskStatus::Failed;
                    stop_reason = format!("ошибка модели на раунде {round}");
                    rounds_log.push(format!("Раунд {round} [ошибка]: {e}"));
                    break;
                }
            }
        }
        let report = cycle_report(&task_id, &objective, &stop_reason, &rounds_log, &out_dir);
        if let Err(e) = std::fs::write(out_dir.join("FINAL.md"), format!("# {report}\n")) {
            tracing::warn!("ralph: FINAL.md не записан: {e}");
        }
        registry2.finish(&task_id, status, report);
    });
    Ok(id)
}

/// Инструменты домена: `ralph_run`.
#[must_use]
pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(RalphRunTool {
        dirs: cfg.plugins.dirs.clone(),
    })]
}

/// Инструмент `ralph_run`: запуск ralph-цикла в фоне.
struct RalphRunTool {
    dirs: Vec<std::path::PathBuf>,
}

#[async_trait]
impl Tool for RalphRunTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "ralph_run".into(),
            description: "Запустить ralph-цикл: до 6 раундов к НЕИЗМЕННОЙ цели, каждый раунд — \
                свежий агент без памяти прошлых раундов; состояние переносят файлы рабочего \
                каталога и компактный handoff (status/summary/evidence/next_steps/blockers). \
                Зови для длинных итеративных задач, которые один контекст не тянет: поэтапная \
                миграция, серия ADR, наращивание architecture-rules по модулям, ревизия \
                документации домена. Для одиночной параллельной проверки достаточно subagent_run."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "description": "неизменная цель цикла: что должно быть достигнуто и по каким признакам это проверить"
                    },
                    "rounds": {
                        "type": "integer",
                        "description": "число раундов (дефолт 3, максимум 6)",
                        "default": DEFAULT_ROUNDS,
                        "minimum": 1,
                        "maximum": MAX_ROUNDS
                    },
                    "agent": {
                        "type": "string",
                        "description": "спека рабочего агента из плагинов; пусто — general"
                    },
                    "context": {
                        "type": "string",
                        "description": "явный контекст первого раунда (пути, решения, ограничения)"
                    }
                },
                "required": ["objective"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let objective = args
            .get("objective")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if objective.is_empty() {
            return Ok(ToolOutput::err("ralph_run: пустой objective"));
        }
        let rounds = args
            .get("rounds")
            .and_then(Value::as_u64)
            .unwrap_or(DEFAULT_ROUNDS as u64)
            .clamp(1, MAX_ROUNDS as u64) as usize;
        let Some(registry) = &ctx.subagents else {
            return Ok(ToolOutput::err(
                "ralph-цикл недоступен: реестр фоновых задач не подключён (headless-режим)",
            ));
        };
        let agent = args
            .get("agent")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("general");
        let spec = if agent == "general" {
            general_spec()
        } else if let Some(s) = available_specs(&self.dirs)
            .into_iter()
            .find(|s| s.name == agent)
        {
            s
        } else {
            let available = available_specs(&self.dirs)
                .iter()
                .map(|s| format!("{} [{}]", s.name, s.plugin))
                .collect::<Vec<_>>()
                .join(", ");
            return Ok(ToolOutput::err(format!(
                "субагент '{agent}' не найден. Доступные: general, {available}"
            )));
        };
        let Some(provider) = ctx
            .provider
            .clone()
            .or_else(|| ctx.llm.as_ref().map(|r| r.default()))
        else {
            return Ok(ToolOutput::err(
                "нет модели для ralph-цикла: LLM не настроен в контексте",
            ));
        };
        let context = args.get("context").and_then(Value::as_str);
        match launch_ralph(
            registry,
            &spec,
            &objective,
            rounds,
            context,
            provider,
            ctx.clone(),
        ) {
            Ok(id) => Ok(ToolOutput::ok(format!(
                "ralph-цикл запущен в фоне: {id} (раундов: до {rounds}, агент: {}). \
                 Каждый раунд — свежий агент; состояние — файлы + handoff. \
                 Статус — subagent_list, сводка — subagent_result(id=\"{id}\"), \
                 артефакты — reports/ralph/{id}/.",
                spec.name
            ))),
            Err(e) => Ok(ToolOutput::err(format!("{e}"))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::{ChatMessage, ChatRequest};
    use crate::subagent::SubagentRegistry;
    use std::sync::Mutex;

    const GOOD_REPORT: &str = "Отчёт о раунде: создан ADR-скелет.\n\n```handoff\n\
        {\"status\":\"continue\",\"summary\":\"ADR-скелет создан\",\"evidence\":[\"docs/adr/001.md:1\"],\"next_steps\":[\"заполнить контекст\"],\"blockers\":[]}\n```";

    #[test]
    fn parses_last_handoff_block_and_normalizes_status() {
        let report = "пример формата:\n```handoff\n{\"status\":\"done\"}\n```\nотчёт…\n```handoff\n\
            {\"status\":\"Blocked by design\",\"summary\":\"упёрлись\",\"blockers\":[\"нет решения КА\"]}\n```";
        let hb = parse_handoff(report).expect("handoff распарсен");
        assert_eq!(hb.status, "blocked", "последний блок, статус нормализован");
        assert_eq!(hb.summary, "упёрлись");
        assert_eq!(hb.blockers, vec!["нет решения КА"]);
    }

    #[test]
    fn sloppy_status_maps_to_canonical() {
        assert_eq!(normalize_status("Done!"), "done");
        assert_eq!(normalize_status("COMPLETE"), "done");
        assert_eq!(normalize_status("готово"), "done");
        assert_eq!(normalize_status("blocked"), "blocked");
        assert_eq!(normalize_status("всё"), "continue");
    }

    #[test]
    fn missing_or_broken_handoff_gets_fallback() {
        assert!(parse_handoff("просто текст без блока").is_none());
        assert!(parse_handoff("```handoff\n{битый json\n```").is_none());
        let fb = fallback_handoff("просто текст без блока");
        assert_eq!(fb.status, "continue");
        assert_eq!(fb.summary, "просто текст без блока");
    }

    #[test]
    fn round_prompt_carries_objective_context_and_handoff() {
        let hb = parse_handoff(GOOD_REPORT).expect("handoff");
        let p = round_prompt("миграция на саги", 2, 3, Some(&hb), Some("контур: retail"));
        assert!(p.contains("миграция на саги"));
        assert!(p.contains("контур: retail"));
        assert!(p.contains("ADR-скелет создан"), "handoff встроен: {p}");
        assert!(p.contains("Раунд 2 из 3"));
        // Без контекста и предшественника — первый раунд минимален.
        let p1 = round_prompt("цель", 1, 3, None, None);
        assert!(!p1.contains("Контекст от заказчика"));
        assert!(!p1.contains("Handoff предыдущего"));
    }

    /// Провайдер, отвечающий по сценарию: i-й вызов → i-й текст.
    #[derive(Debug)]
    struct ScriptLlm {
        answers: Vec<String>,
        calls: Mutex<usize>,
    }

    #[async_trait]
    impl LlmProvider for ScriptLlm {
        fn name(&self) -> &'static str {
            "script"
        }
        fn model(&self) -> &'static str {
            "script-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            let mut calls = self.calls.lock().expect("calls");
            let i = *calls;
            *calls += 1;
            let text = self
                .answers
                .get(i)
                .cloned()
                .unwrap_or_else(|| "лишний вызов".to_string());
            Ok(ChatMessage::assistant(text, Vec::new()))
        }
    }

    fn test_ctx(dir: &std::path::Path) -> ToolContext {
        let mut cfg = Config::default();
        cfg.paths.sessions_dir = dir.join("sessions");
        cfg.paths.reports_dir = dir.join("reports");
        // Изоляция тестов от плагинных хуков реальной библиотеки.
        cfg.plugins.include_hooks = false;
        ToolContext::new(dir.to_path_buf(), Arc::new(cfg))
    }

    #[tokio::test]
    async fn ralph_cycle_stops_on_done_and_writes_artifacts() {
        let tmp = tempfile::tempdir().expect("tmp");
        let registry = SubagentRegistry::new();
        let ctx = test_ctx(tmp.path());
        let provider = Arc::new(ScriptLlm {
            answers: vec![
                GOOD_REPORT.to_string(),
                "всё готово.\n```handoff\n{\"status\":\"done\",\"summary\":\"саги описаны\"}\n```"
                    .to_string(),
                "этот вызов не должен случиться".to_string(),
            ],
            calls: Mutex::new(0),
        });
        let id = launch_ralph(
            &registry,
            &general_spec(),
            "описать саги",
            4,
            None,
            provider.clone(),
            ctx,
        )
        .expect("запуск");
        assert!(id.starts_with("ralph-"));
        // Ждём финиша фоновой задачи.
        let mut task = None;
        for _ in 0..200 {
            if let Some(t) = registry.get(&id) {
                if t.status != TaskStatus::Running {
                    task = Some(t);
                    break;
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        let task = task.expect("цикл завершился");
        assert_eq!(task.status, TaskStatus::Done, "report: {}", task.report);
        assert!(
            task.report.contains("цель достигнута за 2"),
            "report: {}",
            task.report
        );
        assert!(
            task.report.contains("Раунд 1 [continue]"),
            "report: {}",
            task.report
        );
        // Стоп на done: третий ответ не потребовался.
        assert_eq!(*provider.calls.lock().expect("calls"), 2);
        // Артефакты: round-01/02 + handoff-02 + FINAL.md.
        let dir = tmp.path().join("reports/ralph").join(&id);
        for f in [
            "round-01.md",
            "round-02.md",
            "handoff-01.json",
            "handoff-02.json",
            "FINAL.md",
        ] {
            assert!(dir.join(f).is_file(), "нет {f}");
        }
    }

    #[tokio::test]
    async fn ralph_cycle_stops_on_blocked_with_done_status_in_registry() {
        let tmp = tempfile::tempdir().expect("tmp");
        let registry = SubagentRegistry::new();
        let ctx = test_ctx(tmp.path());
        let provider = Arc::new(ScriptLlm {
            answers: vec!["упёрлись.\n```handoff\n{\"status\":\"blocked\",\"summary\":\"нет ТЗ\",\"blockers\":[\"ждём КА\"]}\n```".to_string()],
            calls: Mutex::new(0),
        });
        let id = launch_ralph(&registry, &general_spec(), "цель", 3, None, provider, ctx)
            .expect("запуск");
        let mut task = None;
        for _ in 0..200 {
            if let Some(t) = registry.get(&id) {
                if t.status != TaskStatus::Running {
                    task = Some(t);
                    break;
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        let task = task.expect("цикл завершился");
        assert_eq!(task.status, TaskStatus::Done);
        assert!(
            task.report.contains("блокерами на раунде 1"),
            "report: {}",
            task.report
        );
    }

    #[tokio::test]
    async fn ralph_tool_validates_args() {
        let tmp = tempfile::tempdir().expect("tmp");
        let registry = SubagentRegistry::new();
        let ctx = test_ctx(tmp.path()).with_subagents(registry);
        let tool = RalphRunTool { dirs: Vec::new() };
        let out = tool.call(json!({}), &ctx).await.expect("call");
        assert!(out.is_error, "пустой objective — ошибка: {}", out.content);
        // Нет провайдера в контексте — внятная ошибка, а не паника.
        let out = tool
            .call(json!({"objective": "что-то"}), &ctx)
            .await
            .expect("call");
        assert!(out.is_error, "нет LLM — ошибка: {}", out.content);
    }
}
