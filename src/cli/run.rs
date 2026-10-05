//! Headless-прогон агента (`arch-ml run`): стрим/тихий режимы, бюджеты
//! (общий таймаут, лимит итераций), goal-режим; системный промпт по
//! умолчанию (библиотека промптов или встроенный fallback + md-память).

use std::io::{IsTerminal, Read, Write as _};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};

use arch_harness::agent::AgentSession;
use arch_harness::config::Config;
use arch_harness::llm::LlmRegistry;
use arch_harness::tool::ToolContext;

/// Опции headless-прогона `arch-ml run` (бюджеты).
pub(crate) struct RunOptions {
    /// Общий таймаут прогона, секунды.
    pub(crate) timeout_secs: Option<u64>,
    /// Переопределение `agent.max_tool_turns` на прогон.
    pub(crate) max_turns: Option<u64>,
    /// Спека goal-режима (`objective || criterion || check`); `None` —
    /// обычный одиночный прогон.
    pub(crate) goal: Option<String>,
}

/// `arch-ml run`: headless агент.
///
/// Строгий режим (`--quiet`, как `dsh --profile headless` у `DeepSeek`
/// Harness) = без стриминга: stdout несёт ТОЛЬКО финальный ответ ассистента
/// (пригоден для пайпов), события хода молчат; пустая задача отклоняется
/// до запуска; сбой — причина в stderr и ненулевой код выхода.
///
/// Бюджеты: `--timeout SECS` — общий потолок прогона (превышение — причина
/// в stderr и exit 1), `--max-turns N` — лимит итераций инструментов
/// (перекрывает `agent.max_tool_turns` на клоне конфига).
///
/// В стрим-режиме stdout несёт только текст ответа (дельты); прогресс
/// (вызовы инструментов, заметки) уходит в stderr — пайп остаётся чистым.
pub(crate) async fn cmd_run(
    cfg: &Arc<Config>,
    prompt: Option<String>,
    model: Option<String>,
    stream: bool,
    think: Option<String>,
    opts: RunOptions,
) -> Result<i32> {
    // Гол-режим: цель несёт задачу сама — промпт не обязателен.
    let goal_spec = match opts.goal.as_deref() {
        Some(raw) => {
            Some(arch_harness::goal::GoalSpec::parse_set(raw).context("--goal: разбор цели")?)
        }
        None => None,
    };
    let input = match prompt.as_deref() {
        Some("-") | None if !std::io::stdin().is_terminal() => {
            let mut buf = String::new();
            std::io::stdin()
                .read_to_string(&mut buf)
                .context("чтение stdin")?;
            buf
        }
        Some(p) => p.to_string(),
        None if goal_spec.is_some() => String::new(),
        None => anyhow::bail!("нет промпта: передайте аргумент или пайп в stdin"),
    };
    if input.trim().is_empty() && goal_spec.is_none() {
        anyhow::bail!("пустая задача: передайте непустой промпт аргументом или пайпом в stdin");
    }
    let thinking = match think.as_deref() {
        Some("on") => Some(true),
        Some("off") => Some(false),
        Some(other) => anyhow::bail!("--think: ожидается on|off, получено '{other}'"),
        None => None,
    };

    // Переопределение бюджета итераций — на клоне конфига, глобальный не трогаем.
    let cfg = match opts.max_turns {
        Some(n) => {
            let mut owned = (**cfg).clone();
            owned.agent.max_tool_turns =
                usize::try_from(n).context("--max-turns: значение не помещается в usize")?;
            Arc::new(owned)
        }
        None => cfg.clone(),
    };

    let registry = Arc::new(LlmRegistry::from_config(&cfg)?);
    let provider = match &model {
        Some(name) => registry.get(name)?,
        None => registry.default(),
    };
    let tools = arch_harness::tools::full_registry(&cfg);
    let cwd = std::env::current_dir().context("cwd")?;
    // Путь проекта строкой — до того, как `cwd` уедет в `ToolContext`
    // (владелец один): нужен как аргумент доменного хука `intent`.
    let repo = cwd.to_string_lossy().into_owned();
    let tool_ctx = ToolContext::new(cwd, cfg.clone())
        .with_llm(registry.clone())
        .with_provider(provider.clone())
        .with_subagents(arch_harness::subagent::SubagentRegistry::new());
    let system = default_system_prompt(&cfg);
    let mut session = AgentSession::new(cfg.clone(), provider, tools, tool_ctx, system);
    session.set_thinking(thinking);
    // Гол-режим: цель ставится до первого хода, а сам ход запускает
    // continuation — цель материализуется в контексте как `user`-сообщение
    // (спека `aiml/notes/goal-mode.md` §2). Дальше петля продолжает сама.
    let input = if let Some(spec) = goal_spec {
        // Роутинг по фактам проекта: текст намерения уходит доменным хуком
        // `intent` (плагин hypothesis-router) сразу после приёма цели, до
        // первого хода, — «after text received, before REQ/NFR». Строка
        // теплицы идёт в stderr (прогресс-канал): в stream/quiet-режиме
        // stdout пайпа несёт только ответ модели. Хук не установлен или
        // промолчал — строки нет, и выдумывать гипотезы нечем.
        let intent_hint = arch_harness::agent::slash::intent_line(
            &cfg.plugins.dirs,
            Path::new(&repo),
            &spec.objective,
        );
        session.goal_set(spec);
        if let Some(hint) = intent_hint {
            let _ = writeln!(std::io::stderr().lock(), "\x1b[2m» {hint}\x1b[0m");
        }
        session.goal_kickoff_message().unwrap_or(input)
    } else {
        input
    };

    let send = async {
        if stream {
            let (tx, mut rx) = tokio::sync::mpsc::channel(64);
            let printer = tokio::spawn(async move {
                use arch_harness::agent::AgentEvent;
                while let Some(ev) = rx.recv().await {
                    // StdoutLock/StderrLock не Send — лочим на каждое событие,
                    // не через await.
                    match ev {
                        AgentEvent::Delta(text) => {
                            let mut out = std::io::stdout().lock();
                            let _ = out.write_all(text.as_bytes());
                            let _ = out.flush();
                        }
                        // «Мысли» — приглушённо в stderr (прогресс-канал).
                        AgentEvent::ReasoningDelta(text) => {
                            let mut err = std::io::stderr().lock();
                            let _ = write!(err, "\x1b[2m{text}\x1b[0m");
                            let _ = err.flush();
                        }
                        // Прогресс — в stderr: stdout пайпа несёт только ответ.
                        AgentEvent::ToolStart { name, .. } => {
                            let _ =
                                writeln!(std::io::stderr().lock(), "\x1b[2m▶ tool: {name}\x1b[0m");
                        }
                        AgentEvent::ToolEnd {
                            name,
                            is_error,
                            summary,
                            ..
                        } => {
                            let mark = if is_error { "✗" } else { "✓" };
                            let _ = writeln!(
                                std::io::stderr().lock(),
                                "\x1b[2m{mark} {name}: {summary}\x1b[0m"
                            );
                        }
                        AgentEvent::Note(text) => {
                            let _ = writeln!(std::io::stderr().lock(), "\x1b[2m» {text}\x1b[0m");
                        }
                        AgentEvent::TurnDone => {
                            let _ = writeln!(std::io::stdout().lock());
                        }
                        // Телеметрия индикатора контекста и usage последнего
                        // ответа (сегмент кэша) — только для TUI.
                        AgentEvent::ContextUsage(_) | AgentEvent::Usage(_) => {}
                    }
                }
            });
            let r = session.send(&input, Some(tx)).await;
            let _ = printer.await;
            r.map_err(anyhow::Error::from)
        } else {
            session
                .send(&input, None)
                .await
                .map_err(anyhow::Error::from)
        }
    };
    let reply = match opts.timeout_secs {
        Some(secs) => match tokio::time::timeout(Duration::from_secs(secs), send).await {
            Ok(r) => r,
            Err(_) => Err(anyhow::anyhow!(
                "таймаут прогона ({secs}с): провайдер или инструмент не ответил вовремя"
            )),
        },
        None => send.await,
    };
    // Гол-режим: сбой хода — автопауза цели (спека §8), а не потеря прогона.
    // Наружу — код 6 «заморожен»; причина и состояние цели в журнале сессии,
    // продолжить — `/goal resume` или повторный запуск.
    let reply = match reply {
        Ok(reply) => reply,
        Err(e) if session.goal().is_some() => {
            let _ = writeln!(
                std::io::stderr().lock(),
                "\x1b[2m» цель приостановлена: {e}\x1b[0m"
            );
            return Ok(session.goal_exit_code().unwrap_or(6));
        }
        Err(e) => return Err(e),
    };
    if !stream {
        // Печать через writeln с игнорированием BrokenPipe: `arch-ml run -q … | head`
        // обрывает stdout — для пайпа это норма, а не повод для паники outln!.
        let mut out = std::io::stdout().lock();
        let _ = writeln!(out, "{reply}");
        let _ = out.flush();
    }
    // Цель без гол-режима не ставилась — код 0 (поведение прежних версий).
    Ok(session.goal_exit_code().unwrap_or(0))
}

/// Системный промпт по умолчанию: из библиотеки промптов или встроенный,
/// дополненный глобальной md-памятью (`paths.memory_file`, см. `memory`).
fn default_system_prompt(cfg: &Config) -> String {
    let dir = cfg.paths.prompts_dir();
    let base = match arch_harness::agent::prompts::load_library(&dir) {
        Ok(lib) => match lib.iter().find(|t| t.name == "architect") {
            Some(tpl) => tpl.body.clone(),
            None => fallback_system_prompt(),
        },
        Err(_) => fallback_system_prompt(),
    };
    // Ошибка чтения памяти не фатальна: сессия работает без неё.
    let memory = arch_harness::memory::load(&cfg.paths.memory_file)
        .ok()
        .flatten();
    arch_harness::memory::augment_system_prompt(&base, memory.as_deref(), &cfg.paths.memory_file)
}

/// Встроенный системный промпт (fallback, когда библиотека недоступна).
fn fallback_system_prompt() -> String {
    "Ты — solution-архитектор в контуре AI/ML-исследователя. Помогаешь проектировать \
     решения, ведёшь ADR и architecture-spine, оцениваешь архитектуру по рубрикам, \
     готовишь handoff-пакеты кодовым агентам. Отвечай по-русски, точно и по делу."
        .into()
}
