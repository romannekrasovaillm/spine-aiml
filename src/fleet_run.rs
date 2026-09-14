//! Оркестратор флота: детерминированный роутинг + живой стрим + управление.
//!
//! Handoff-пакет декомпозируется в work-items (это делает архитектор/модель),
//! [`run_fleet`] назначает их агентам через [`crate::router::Router`] и гонит
//! конкурентно, пиша [`FleetEvent`]'ы в append-only JSONL. Стрим — проекция
//! единого журнала (инвариант «model-visible ⟺ logged», H1.4): событие =
//! строка лога; полный stdout агента — в per-agent лог на диске, в стрим идёт
//! компакт (статус/тайминги/код возврата). Флот в N агентов наблюдаем так же
//! дёшево, как 1 — контроллер читает проекцию, а не сырой вывод.
//!
//! Управление — control-канал: [`ControlCommand`] дописывается в
//! `control.jsonl`, раннер читает его между items. Субпроцессы не делят
//! память — файл как общий артефакт.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::Config;
use crate::error::{HarnessError, Result};
use crate::router::{AgentKind, AgentProfile, CostTier, RoutePlan, Router, WorkItem};

/// Тип-тег события (для подсветки/проекции).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FleetEventKind {
    RunStarted,
    ItemAssigned,
    AgentStarted,
    AgentHeartbeat,
    AgentDone,
    AgentError,
    Control,
    RunFinished,
    /// Событие плана флота (движок паттернов, ADR-042).
    PlanStarted,
    /// Начало волны плана.
    WaveStarted,
    /// Узел поставлен в очередь пула (потолок параллелизма занят).
    NodeQueued,
    /// Узел завершён (все гейты пройдены).
    NodeCompleted,
    /// Узел пропущен (возобновление прогона, блокировка зависимостью).
    NodeSkipped,
    /// Повтор попытки узла.
    NodeRetry,
    /// Вердикт одного гейта узла.
    NodeGated,
    /// Прогон продолжен из журнала.
    RunResumed,
    /// Прогон остановлен по гейту.
    RunHalted,
}

/// Событие флота — одна строка append-only JSONL.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum FleetEvent {
    RunStarted {
        run_id: String,
        package: String,
        n_items: usize,
        n_agents: usize,
        at: String,
    },
    ItemAssigned {
        run_id: String,
        item_id: String,
        agent_id: String,
        score: f64,
        #[serde(default)]
        reason: Vec<String>,
        at: String,
    },
    AgentStarted {
        run_id: String,
        agent_id: String,
        item_id: String,
        kind: String,
        at: String,
    },
    AgentHeartbeat {
        run_id: String,
        agent_id: String,
        item_id: String,
        at: String,
    },
    AgentDone {
        run_id: String,
        agent_id: String,
        item_id: String,
        exit_code: Option<i32>,
        duration_secs: f64,
        contract_status: String,
        at: String,
    },
    AgentError {
        run_id: String,
        agent_id: String,
        item_id: String,
        error: String,
        at: String,
    },
    Control {
        run_id: String,
        agent_id: String,
        cmd: String,
        at: String,
    },
    RunFinished {
        run_id: String,
        n_assigned: usize,
        n_done: usize,
        n_failed: usize,
        n_unassigned: usize,
        at: String,
    },
    /// План флота принят к исполнению (паттерн, число узлов и волн, хэш плана).
    ///
    /// `judge_path`/`judge_sha256` — baseline-судья гейтов, зафиксированный на
    /// старте прогона (ADR-046, п. 1): аудит уровня прогона отвечает на вопрос
    /// «кто судил», не собирая отчёты узлов. Оба поля `None`, если судья не
    /// задан; `judge_sha256` — `None` и тогда, когда файл судьи не прочитан.
    PlanStarted {
        run_id: String,
        plan_id: String,
        pattern: String,
        plan_path: String,
        plan_sha256: String,
        /// Путь baseline-судьи (сериализуется как `null`, если судьи нет).
        #[serde(default)]
        judge_path: Option<String>,
        /// `sha256` файла baseline-судьи (сериализуется как `null`, если нет).
        #[serde(default)]
        judge_sha256: Option<String>,
        n_nodes: usize,
        n_waves: usize,
        at: String,
    },
    /// Начало волны топологического уровня.
    WaveStarted {
        run_id: String,
        wave: usize,
        nodes: Vec<String>,
        at: String,
    },
    /// Узел ждёт слота пула (потолок `max_parallel` занят).
    NodeQueued {
        run_id: String,
        node_id: String,
        at: String,
    },
    /// Узел завершён: все гейты пройдены.
    NodeCompleted {
        run_id: String,
        node_id: String,
        at: String,
    },
    /// Узел пропущен (возобновление или блокировка зависимостью).
    NodeSkipped {
        run_id: String,
        node_id: String,
        reason: String,
        at: String,
    },
    /// Повтор попытки узла после отказа гейта.
    NodeRetry {
        run_id: String,
        node_id: String,
        attempt: usize,
        max_attempts: usize,
        at: String,
    },
    /// Вердикт одного гейта узла.
    ///
    /// `branch` — структурный признак связи «вердикт ↔ ветка» (ADR-046, п. 2):
    /// ветка `arch/…`, влитая в дерево узла событием вливания зависимости.
    /// Гейт приёмки читает ТОЛЬКО это поле, а не текст `detail` (два источника
    /// истины разошлись бы при правке формулировки). `None` — событие ветку не
    /// вливает; журналы старого формата поля не несут и трактуются как
    /// «вердиктов по ветке не найдено» (честное «не проверено»).
    NodeGated {
        run_id: String,
        node_id: String,
        gate: String,
        verdict: String,
        detail: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        branch: Option<String>,
        at: String,
    },
    /// Прогон продолжен из журнала.
    RunResumed {
        run_id: String,
        completed: Vec<String>,
        from_wave: usize,
        at: String,
    },
    /// Прогон остановлен по отказу гейта (`stop_on_gate_fail`).
    RunHalted {
        run_id: String,
        reason: String,
        at: String,
    },
}

impl FleetEvent {
    /// Тип события для проекции.
    #[must_use]
    pub fn kind(&self) -> FleetEventKind {
        match self {
            Self::RunStarted { .. } => FleetEventKind::RunStarted,
            Self::ItemAssigned { .. } => FleetEventKind::ItemAssigned,
            Self::AgentStarted { .. } => FleetEventKind::AgentStarted,
            Self::AgentHeartbeat { .. } => FleetEventKind::AgentHeartbeat,
            Self::AgentDone { .. } => FleetEventKind::AgentDone,
            Self::AgentError { .. } => FleetEventKind::AgentError,
            Self::Control { .. } => FleetEventKind::Control,
            Self::RunFinished { .. } => FleetEventKind::RunFinished,
            Self::PlanStarted { .. } => FleetEventKind::PlanStarted,
            Self::WaveStarted { .. } => FleetEventKind::WaveStarted,
            Self::NodeQueued { .. } => FleetEventKind::NodeQueued,
            Self::NodeCompleted { .. } => FleetEventKind::NodeCompleted,
            Self::NodeSkipped { .. } => FleetEventKind::NodeSkipped,
            Self::NodeRetry { .. } => FleetEventKind::NodeRetry,
            Self::NodeGated { .. } => FleetEventKind::NodeGated,
            Self::RunResumed { .. } => FleetEventKind::RunResumed,
            Self::RunHalted { .. } => FleetEventKind::RunHalted,
        }
    }
}

/// Известные типы событий журнала (для толерантного чтения).
const KNOWN_EVENT_TYPES: [&str; 17] = [
    "run_started",
    "item_assigned",
    "agent_started",
    "agent_heartbeat",
    "agent_done",
    "agent_error",
    "control",
    "run_finished",
    "plan_started",
    "wave_started",
    "node_queued",
    "node_completed",
    "node_skipped",
    "node_retry",
    "node_gated",
    "run_resumed",
    "run_halted",
];

/// Итог толерантного чтения журнала.
#[derive(Debug, Clone)]
pub struct JournalRead {
    /// Разобранные события.
    pub events: Vec<FleetEvent>,
    /// Пропущенные строки (неизвестный `type` или битая последняя строка) —
    /// причина пропуска.
    pub skipped: Vec<String>,
}

/// Журнал флота: append-only JSONL `state/fleet/<run-id>.jsonl`.
///
/// Append открывает файл в режиме `O_APPEND` и пишет строку ОДНИМ `write_all`
/// (payload вместе с переводом строки): дописи конкурентных задач не
/// склеиваются в одну битую строку. Это важно на флоте в сотни узлов, где в
/// журнал пишут одновременно десятки задач (найдено живым прогоном:
/// `writeln!` мог отправить payload и `\n` двумя системными вызовами).
#[derive(Debug, Clone)]
pub struct FleetLog {
    /// Путь к файлу журнала.
    path: PathBuf,
}

impl FleetLog {
    /// Журнал по пути файла.
    #[must_use]
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    /// Путь к файлу журнала.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Дописывает событие строкой JSONL.
    ///
    /// # Errors
    /// Не удалось создать каталог или записать файл.
    pub fn append(&self, event: &FleetEvent) -> Result<()> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
        }
        let mut line = serde_json::to_string(event).map_err(HarnessError::Json)?;
        line.push('\n');
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|e| HarnessError::io(&self.path, e))?;
        use std::io::Write as _;
        f.write_all(line.as_bytes())
            .map_err(|e| HarnessError::io(&self.path, e))?;
        Ok(())
    }

    /// Читает все события (хронологически); отсутствие файла — пусто.
    ///
    /// # Errors
    /// Файл не читается.
    pub fn read_all(path: &Path) -> Result<Vec<FleetEvent>> {
        if !path.is_file() {
            return Ok(Vec::new());
        }
        let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
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

    /// Толерантное чтение: неизвестный `type` (журнал более новой версии) или
    /// битая строка (процесс убит посреди дописки) не роняют разбор — строка
    /// попадает в [`JournalRead::skipped`]. Нужно возобновлению прогона
    /// (ADR-042): инвариант «журнал пишет и читает одна версия» ослаблен
    /// ровно настолько, чтобы resume пережил обрыв записи.
    ///
    /// # Errors
    /// Файл не читается целиком (I/O).
    pub fn read_tolerant(path: &Path) -> Result<JournalRead> {
        let mut events = Vec::new();
        let mut skipped = Vec::new();
        if !path.is_file() {
            return Ok(JournalRead { events, skipped });
        }
        let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
        for (n, line) in text.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let kind = serde_json::from_str::<Value>(line)
                .ok()
                .and_then(|v| v.get("type").and_then(Value::as_str).map(str::to_owned));
            let Some(kind) = kind else {
                skipped.push(format!("строка {}: не JSON-объект с полем type", n + 1));
                continue;
            };
            if !KNOWN_EVENT_TYPES.contains(&kind.as_str()) {
                skipped.push(format!("строка {}: неизвестный тип '{kind}'", n + 1));
                continue;
            }
            match serde_json::from_str::<FleetEvent>(line) {
                Ok(e) => events.push(e),
                Err(e) => skipped.push(format!("строка {}: {}", n + 1, e)),
            }
        }
        Ok(JournalRead { events, skipped })
    }
}

/// Команда управления флотом (control-канал).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControlCommand {
    /// Пауза агента (до Resume).
    Pause,
    /// Снять паузу.
    Resume,
    /// Перекинуть item другому агенту (payload — id нового агента).
    Reroute,
    /// Убить агента.
    Kill,
    /// Поднять приоритет (payload — новый приоритет).
    Priority,
}

/// Запись control-канала.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ControlRecord {
    /// Идентификатор агента (или `*` — все).
    pub agent_id: String,
    /// Команда.
    pub cmd: ControlCommand,
    /// Payload (для Reroute — новый агент; для Priority — число).
    #[serde(default)]
    pub payload: Option<String>,
    /// Штамп.
    pub at: String,
}

/// Control-канал: append-only `state/fleet/<run-id>/control.jsonl`.
#[derive(Debug, Clone)]
pub struct ControlChannel {
    path: PathBuf,
}

impl ControlChannel {
    /// Канал по пути файла.
    #[must_use]
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    /// Дописывает команду.
    ///
    /// # Errors
    /// Ошибка записи.
    pub fn send(&self, agent_id: &str, cmd: ControlCommand, payload: Option<&str>) -> Result<()> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
        }
        let rec = ControlRecord {
            agent_id: agent_id.to_string(),
            cmd,
            payload: payload.map(str::to_owned),
            at: now(),
        };
        // Один `write_all` на команду: дописи не склеиваются (см. `FleetLog`).
        let mut line = serde_json::to_string(&rec).map_err(HarnessError::Json)?;
        line.push('\n');
        let mut f = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|e| HarnessError::io(&self.path, e))?;
        use std::io::Write as _;
        f.write_all(line.as_bytes())
            .map_err(|e| HarnessError::io(&self.path, e))?;
        Ok(())
    }

    /// Читает команды (хронологически); отсутствие файла — пусто.
    ///
    /// # Errors
    /// Файл не читается.
    pub fn read(path: &Path) -> Result<Vec<ControlRecord>> {
        if !path.is_file() {
            return Ok(Vec::new());
        }
        let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
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
}

/// Итог прогона флота.
#[derive(Debug, Clone)]
pub struct FleetRunOutcome {
    /// Идентификатор прогона.
    pub run_id: String,
    /// План маршрутизации (назначения + нераспределённые).
    pub plan: RoutePlan,
    /// Сколько агентов завершились успешно.
    pub n_done: usize,
    /// Сколько агентов упали.
    pub n_failed: usize,
    /// Путь к журналу событий.
    pub log_path: PathBuf,
}

/// Штамп времени для события.
fn now() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// Тир стоимости харнесса (эвристика): Claude/Kimi дороже, qwen/open-source дешевле.
fn harness_cost(name: &str) -> CostTier {
    match name {
        "claude-code" => CostTier::Expensive,
        "kimi-code" | "codewhale" => CostTier::Standard,
        _ => CostTier::Cheap,
    }
}

/// Строит профили агентов флота из известных харнессов конфига.
///
/// Каждый харнесс — `AgentKind::Harness` с общим скиллом `code`, тегом `code`
/// и ёмкостью из `[fleet] capacity_per_agent`. Стоимость — эвристика
/// [`harness_cost`]. Скиллы/теги — минимальная заглушка: тонкая настройка
/// компетенций харнессов — в конфиге (future).
#[must_use]
pub fn harness_profiles(cfg: &Config) -> Vec<AgentProfile> {
    let capacity = cfg.fleet.capacity_per_agent.max(1);
    // Все настроенные адаптеры, а не только встроенные имена: собственный
    // харнесс в config.toml обязан быть доступен флоту (порядок — BTreeMap,
    // то есть детерминированный).
    cfg.harnesses
        .keys()
        .map(|name| AgentProfile {
            id: name.clone(),
            kind: AgentKind::Harness,
            skills: vec!["code".to_string(), name.clone()],
            tools: Vec::new(),
            tags: vec!["code".to_string()],
            load: 0,
            capacity,
            cost_tier: harness_cost(name),
        })
        .collect()
}

/// Запускает субагента в флоте и ждёт завершения поллингом реестра.
#[allow(clippy::too_many_arguments)]
async fn run_subagent_item(
    registry: &crate::subagent::SubagentRegistry,
    spec: &crate::subagent::SubagentSpec,
    provider: std::sync::Arc<dyn crate::llm::LlmProvider>,
    tool_ctx: &crate::tool::ToolContext,
    item_spec: &str,
    log: &FleetLog,
    run_id: &str,
    agent_id: &str,
    item_id: &str,
    control: Option<std::sync::Arc<crate::harness::HarnessControl>>,
) -> std::result::Result<(), ()> {
    log.append(&FleetEvent::AgentStarted {
        run_id: run_id.to_string(),
        agent_id: agent_id.to_string(),
        item_id: item_id.to_string(),
        kind: "subagent".into(),
        at: now(),
    })
    .ok();
    let id = match registry.launch(spec, item_spec, None, provider, tool_ctx.clone()) {
        Ok(id) => id,
        Err(e) => {
            log.append(&FleetEvent::AgentError {
                run_id: run_id.to_string(),
                agent_id: agent_id.to_string(),
                item_id: item_id.to_string(),
                error: e.to_string(),
                at: now(),
            })
            .ok();
            return Err(());
        }
    };
    let started = std::time::Instant::now();
    loop {
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        if control
            .as_ref()
            .is_some_and(|c| c.cancel.load(std::sync::atomic::Ordering::Relaxed))
        {
            log.append(&FleetEvent::AgentError {
                run_id: run_id.to_string(),
                agent_id: agent_id.to_string(),
                item_id: item_id.to_string(),
                error: "killed by control".into(),
                at: now(),
            })
            .ok();
            return Err(());
        }
        // Пауза: субагент продолжает работать, но флот не считает его готовым.
        if control
            .as_ref()
            .is_some_and(|c| c.paused.load(std::sync::atomic::Ordering::Relaxed))
        {
            continue;
        }
        match registry.get(&id) {
            Some(t) if t.status == crate::subagent::TaskStatus::Done => {
                log.append(&FleetEvent::AgentDone {
                    run_id: run_id.to_string(),
                    agent_id: agent_id.to_string(),
                    item_id: item_id.to_string(),
                    exit_code: None,
                    duration_secs: started.elapsed().as_secs_f64(),
                    contract_status: "done".into(),
                    at: now(),
                })
                .ok();
                return Ok(());
            }
            Some(t) if t.status == crate::subagent::TaskStatus::Failed => {
                log.append(&FleetEvent::AgentError {
                    run_id: run_id.to_string(),
                    agent_id: agent_id.to_string(),
                    item_id: item_id.to_string(),
                    error: t.report,
                    at: now(),
                })
                .ok();
                return Err(());
            }
            _ => {}
        }
        if started.elapsed() > std::time::Duration::from_secs(1800) {
            log.append(&FleetEvent::AgentError {
                run_id: run_id.to_string(),
                agent_id: agent_id.to_string(),
                item_id: item_id.to_string(),
                error: "субагент не завершился за 1800 с".into(),
                at: now(),
            })
            .ok();
            return Err(());
        }
    }
}

/// Прогоняет флот: маршрутизирует items и гонит конкурентно через харнессы.
///
/// Стрим пишется в `state/fleet/<run-id>.jsonl`, control-канал —
/// `state/fleet/<run-id>/control.jsonl` (раннер читает его между items).
/// Полный stdout каждого агента — per-agent лог рядом с журналом.
///
/// Ограничение MVP: items гонятся в ОДНОМ репозитории (без worktree-изоляции
/// на item) — для непересекающихся items (разные ADR/файлы) это безопасно;
/// worktree-per-item — следующий шаг (уже есть фабрика [`crate::worktree`]).
///
/// # Errors
/// Не удалось создать каталог журнала; ошибки прогона отдельных агентов —
/// НЕ ошибка вызова (фиксируются событиями `AgentError`).
pub async fn run_fleet(
    cfg: &Config,
    state_dir: &Path,
    repo: &Path,
    package: &str,
    items: &[WorkItem],
    agents: &[AgentProfile],
    tool_ctx: Option<crate::tool::ToolContext>,
) -> Result<FleetRunOutcome> {
    let run_id = format!("flt-{}", chrono::Local::now().timestamp_millis());
    let fleet_dir = state_dir.join("fleet");
    std::fs::create_dir_all(&fleet_dir).map_err(|e| HarnessError::io(&fleet_dir, e))?;

    let log = FleetLog::new(fleet_dir.join(format!("{run_id}.jsonl")));
    log.append(&FleetEvent::RunStarted {
        run_id: run_id.clone(),
        package: package.to_string(),
        n_items: items.len(),
        n_agents: agents.len(),
        at: now(),
    })?;

    // Маршрутизация (детерминированная).
    let plan = Router::new().assign(items, agents);
    for a in &plan.assignments {
        log.append(&FleetEvent::ItemAssigned {
            run_id: run_id.clone(),
            item_id: a.item_id.clone(),
            agent_id: a.agent_id.clone(),
            score: a.score,
            reason: a.reason.clone(),
            at: now(),
        })?;
    }

    // Конкурентный прогон назначений. Каждый item → run_harness; ошибки
    // отдельного прогона фиксируются событием, не валят весь флот.
    let ctrl_path = control_path(&fleet_dir, &run_id);
    // Control-состояние агентов (cancel/pause) + control-watcher: Kill/Pause/
    // Resume/Reroute/Priority применяются к живым прогонам через HarnessControl.
    let controls: std::sync::Arc<
        std::sync::Mutex<
            std::collections::HashMap<String, std::sync::Arc<crate::harness::HarnessControl>>,
        >,
    > = std::sync::Arc::new(std::sync::Mutex::new(std::collections::HashMap::new()));
    {
        let mut m = controls
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        for a in &plan.assignments {
            m.entry(a.agent_id.clone())
                .or_insert_with(|| std::sync::Arc::new(crate::harness::HarnessControl::default()));
        }
    }
    // agent → item (для Reroute; корректен при capacity 1 — один item на агента).
    let agent_to_item: std::collections::HashMap<String, String> = plan
        .assignments
        .iter()
        .map(|a| (a.agent_id.clone(), a.item_id.clone()))
        .collect();
    // Перекидки: (item_id, новый_агент), зафиксированные control-каналом.
    let reroutes: std::sync::Arc<std::sync::Mutex<Vec<(String, String)>>> =
        std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));

    let watcher_controls = controls.clone();
    let watcher_reroutes = reroutes.clone();
    let watcher_ctrl = ctrl_path.clone();
    let watcher_map = agent_to_item;
    let watcher = tokio::spawn(async move {
        loop {
            if let Ok(recs) = ControlChannel::read(&watcher_ctrl) {
                let m = watcher_controls
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                for rec in recs {
                    let all = rec.agent_id == "*";
                    let matches = |aid: &str| all || aid == rec.agent_id;
                    match rec.cmd {
                        ControlCommand::Kill => {
                            for (aid, c) in m.iter() {
                                if matches(aid) {
                                    c.cancel.store(true, std::sync::atomic::Ordering::Relaxed);
                                }
                            }
                        }
                        ControlCommand::Pause => {
                            for (aid, c) in m.iter() {
                                if matches(aid) {
                                    c.paused.store(true, std::sync::atomic::Ordering::Relaxed);
                                }
                            }
                        }
                        ControlCommand::Resume => {
                            for (aid, c) in m.iter() {
                                if matches(aid) {
                                    c.paused.store(false, std::sync::atomic::Ordering::Relaxed);
                                }
                            }
                        }
                        ControlCommand::Reroute => {
                            // Kill источник + записать перекидку item → target (payload).
                            for (aid, c) in m.iter() {
                                if matches(aid) {
                                    c.cancel.store(true, std::sync::atomic::Ordering::Relaxed);
                                }
                            }
                            if let (Some(item), Some(target)) =
                                (watcher_map.get(&rec.agent_id), rec.payload.as_deref())
                            {
                                watcher_reroutes
                                    .lock()
                                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                                    .push((item.clone(), target.to_string()));
                            }
                        }
                        ControlCommand::Priority => {
                            // В конкурентной модели (без очереди) приоритет — no-op:
                            // команда уже зафиксирована в control.jsonl (аудит).
                        }
                    }
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }
    });
    let mut handles = Vec::new();
    for a in &plan.assignments {
        let Some(agent) = agents.iter().find(|ag| ag.id == a.agent_id) else {
            continue;
        };
        if agent.kind != AgentKind::Harness {
            // Субагент: запуск через реестр + поллинг до завершения. Требует
            // provider + registry из ToolContext (в headless-CLI их нет).
            let Some(tctx) = tool_ctx.clone() else {
                log.append(&FleetEvent::AgentError {
                    run_id: run_id.clone(),
                    agent_id: agent.id.clone(),
                    item_id: a.item_id.clone(),
                    error: "флот: субагенты недоступны (headless-режим без TUI/раннера)".into(),
                    at: now(),
                })?;
                continue;
            };
            let Some(registry) = tctx.subagents.clone() else {
                log.append(&FleetEvent::AgentError {
                    run_id: run_id.clone(),
                    agent_id: agent.id.clone(),
                    item_id: a.item_id.clone(),
                    error: "флот: реестр субагентов не подключён".into(),
                    at: now(),
                })?;
                continue;
            };
            let Some(provider) = tctx
                .provider
                .clone()
                .or_else(|| tctx.llm.as_ref().map(|r| r.default()))
            else {
                log.append(&FleetEvent::AgentError {
                    run_id: run_id.clone(),
                    agent_id: agent.id.clone(),
                    item_id: a.item_id.clone(),
                    error: "флот: нет модели для субагента".into(),
                    at: now(),
                })?;
                continue;
            };
            let spec = if agent.id == "general" {
                crate::subagent::general_spec()
            } else if let Some(s) = crate::subagent::available_specs(&cfg.plugins.dirs)
                .into_iter()
                .find(|s| s.name == agent.id)
            {
                s
            } else {
                log.append(&FleetEvent::AgentError {
                    run_id: run_id.clone(),
                    agent_id: agent.id.clone(),
                    item_id: a.item_id.clone(),
                    error: format!("флот: субагент '{}' не найден", agent.id),
                    at: now(),
                })?;
                continue;
            };
            let name = agent.id.clone();
            let item_id = a.item_id.clone();
            let item_spec = items
                .iter()
                .find(|i| i.id == item_id)
                .map_or_else(|| item_id.clone(), |i| i.spec.clone());
            let control = controls
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .get(&name)
                .cloned();
            let log2 = log.clone();
            let run_id2 = run_id.clone();
            let handle = tokio::spawn(async move {
                run_subagent_item(
                    &registry, &spec, provider, &tctx, &item_spec, &log2, &run_id2, &name,
                    &item_id, control,
                )
                .await
            });
            handles.push(handle);
            continue;
        }
        let Some(hcfg) = cfg.harnesses.get(&agent.id) else {
            log.append(&FleetEvent::AgentError {
                run_id: run_id.clone(),
                agent_id: agent.id.clone(),
                item_id: a.item_id.clone(),
                error: format!("флот: харнесс '{}' не настроен", agent.id),
                at: now(),
            })?;
            continue;
        };
        let name = agent.id.clone();
        let item_id = a.item_id.clone();
        let spec = items
            .iter()
            .find(|i| i.id == item_id)
            .map_or_else(|| item_id.clone(), |i| i.spec.clone());
        let hcfg = hcfg.clone();
        let log = log.clone();
        let run_id = run_id.clone();
        let fleet_dir = fleet_dir.clone();
        let ctrl_path = ctrl_path.clone();
        // Worktree-изоляция на item ([fleet] require_worktree): каждый item —
        // в своём worktree, чтобы параллельные харнессы не портили одно дерево.
        let repo = if cfg.fleet.require_worktree {
            match crate::harness::enforce_run_worktree(cfg, repo, &format!("{name}-{item_id}"))
                .await
            {
                Ok(Some((dir, _))) => dir,
                _ => repo.to_path_buf(), // не git-репо → прогон в дереве как есть
            }
        } else {
            repo.to_path_buf()
        };
        let control = controls
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(&name)
            .cloned();
        let handle = tokio::spawn(async move {
            // Control-канал: Kill перед стартом — item пропускается.
            if control_kills(&ctrl_path, &name).unwrap_or(false) {
                log.append(&FleetEvent::AgentError {
                    run_id: run_id.clone(),
                    agent_id: name.clone(),
                    item_id: item_id.clone(),
                    error: "killed by control".into(),
                    at: now(),
                })
                .ok();
                return Err(());
            }
            log.append(&FleetEvent::AgentStarted {
                run_id: run_id.clone(),
                agent_id: name.clone(),
                item_id: item_id.clone(),
                kind: "harness".into(),
                at: now(),
            })
            .ok();
            let started = std::time::Instant::now();
            // Heartbeat-стрим: сигнал живости не чаще раза в 5 с (сырой stdout —
            // в per-agent лог ниже; в стрим идёт проекция «агент жив»).
            let last_hb = std::sync::Arc::new(std::sync::Mutex::new(std::time::Instant::now()));
            let (hb_log, hb_run, hb_name, hb_item) =
                (log.clone(), run_id.clone(), name.clone(), item_id.clone());
            let on_activity: std::sync::Arc<dyn Fn() + Send + Sync> =
                std::sync::Arc::new(move || {
                    let mut last = last_hb
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner);
                    if last.elapsed() >= std::time::Duration::from_secs(5) {
                        *last = std::time::Instant::now();
                        hb_log
                            .append(&FleetEvent::AgentHeartbeat {
                                run_id: hb_run.clone(),
                                agent_id: hb_name.clone(),
                                item_id: hb_item.clone(),
                                at: now(),
                            })
                            .ok();
                    }
                });
            let result = crate::harness::run_harness_streaming(
                &name,
                &hcfg,
                &repo,
                &spec,
                on_activity,
                control,
            )
            .await;
            let duration = started.elapsed().as_secs_f64();
            // Полный вывод — per-agent лог на диске (стрим несёт проекцию).
            if let Ok(run) = &result {
                let agent_log = fleet_dir.join(format!("{run_id}-{name}.log"));
                if let Ok(mut f) = std::fs::File::create(&agent_log) {
                    use std::io::Write as _;
                    let _ = write!(f, "{}\n{}", run.stdout, run.stderr);
                }
            }
            match result {
                Ok(run) => {
                    log.append(&FleetEvent::AgentDone {
                        run_id,
                        agent_id: name,
                        item_id,
                        exit_code: run.exit_code,
                        duration_secs: duration,
                        contract_status: match &run.contract {
                            crate::harness::ContractParse::Valid(c) => {
                                c.status.as_str().to_string()
                            }
                            crate::harness::ContractParse::Invalid(_) => "invalid".to_string(),
                            crate::harness::ContractParse::Missing => "missing".to_string(),
                        },
                        at: now(),
                    })
                    .ok();
                    Ok::<(), ()>(())
                }
                Err(e) => {
                    log.append(&FleetEvent::AgentError {
                        run_id,
                        agent_id: name,
                        item_id,
                        error: e.to_string(),
                        at: now(),
                    })
                    .ok();
                    Err(())
                }
            }
        });
        handles.push(handle);
    }

    // Считаем исходы.
    let mut n_done = 0usize;
    let mut n_failed = 0usize;
    for h in handles {
        match h.await {
            Ok(Ok(())) => n_done += 1,
            _ => n_failed += 1,
        }
    }
    // Прогон завершён — глушим control-watcher.
    watcher.abort();

    // Second-wave: Reroute — перезапуск перекинутых items на целевом агенте.
    let rerouted = reroutes
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .clone();
    for (item_id, target) in rerouted {
        let Some(target_agent) = agents.iter().find(|ag| ag.id == target) else {
            log.append(&FleetEvent::AgentError {
                run_id: run_id.clone(),
                agent_id: target.clone(),
                item_id: item_id.clone(),
                error: format!("reroute: целевой агент '{target}' отсутствует во флоте"),
                at: now(),
            })?;
            continue;
        };
        let Some(hcfg) = cfg.harnesses.get(&target) else {
            log.append(&FleetEvent::AgentError {
                run_id: run_id.clone(),
                agent_id: target.clone(),
                item_id: item_id.clone(),
                error: format!("reroute: харнесс '{target}' не настроен"),
                at: now(),
            })?;
            continue;
        };
        let _ = target_agent; // kind проверен: перекидываем только на харнесс
        let spec = items
            .iter()
            .find(|i| i.id == item_id)
            .map_or_else(|| item_id.clone(), |i| i.spec.clone());
        let (name, run_id2, log2, hcfg2, repo2, item_id2) = (
            target.clone(),
            run_id.clone(),
            log.clone(),
            hcfg.clone(),
            repo.to_path_buf(),
            item_id.clone(),
        );
        tokio::spawn(async move {
            log2.append(&FleetEvent::AgentStarted {
                run_id: run_id2.clone(),
                agent_id: name.clone(),
                item_id: item_id2.clone(),
                kind: "harness".into(),
                at: now(),
            })
            .ok();
            let started = std::time::Instant::now();
            match crate::harness::run_harness(&name, &hcfg2, &repo2, &spec).await {
                Ok(run) => {
                    log2.append(&FleetEvent::AgentDone {
                        run_id: run_id2,
                        agent_id: name,
                        item_id: item_id2,
                        exit_code: run.exit_code,
                        duration_secs: started.elapsed().as_secs_f64(),
                        contract_status: match &run.contract {
                            crate::harness::ContractParse::Valid(c) => {
                                c.status.as_str().to_string()
                            }
                            crate::harness::ContractParse::Invalid(_) => "invalid".to_string(),
                            crate::harness::ContractParse::Missing => "missing".to_string(),
                        },
                        at: now(),
                    })
                    .ok();
                }
                Err(e) => {
                    log2.append(&FleetEvent::AgentError {
                        run_id: run_id2,
                        agent_id: name,
                        item_id: item_id2,
                        error: e.to_string(),
                        at: now(),
                    })
                    .ok();
                }
            }
        });
    }

    log.append(&FleetEvent::RunFinished {
        run_id: run_id.clone(),
        n_assigned: plan.assignments.len(),
        n_done,
        n_failed,
        n_unassigned: plan.unassigned.len(),
        at: now(),
    })?;

    Ok(FleetRunOutcome {
        run_id,
        plan,
        n_done,
        n_failed,
        log_path: log.path().to_path_buf(),
    })
}

/// Текстовый отчёт прогона: карта назначений + сводка.
#[must_use]
pub fn render_outcome(outcome: &FleetRunOutcome) -> String {
    let mut s = format!(
        "Флот: run-id {}\nназначено {} / успешно {} / сбой {} / нераспределено {}\nжурнал: {}\n",
        outcome.run_id,
        outcome.plan.assignments.len(),
        outcome.n_done,
        outcome.n_failed,
        outcome.plan.unassigned.len(),
        outcome.log_path.display()
    );
    s.push_str("\nНазначения:\n");
    for a in &outcome.plan.assignments {
        s.push_str(&format!(
            "  {} → {} (score {:.3})\n",
            a.item_id, a.agent_id, a.score
        ));
    }
    if !outcome.plan.unassigned.is_empty() {
        s.push_str(&format!(
            "Нераспределено: {}\n",
            outcome.plan.unassigned.join(", ")
        ));
    }
    s
}

/// Путь к control-каналу прогона.
#[must_use]
pub fn control_path(fleet_dir: &Path, run_id: &str) -> PathBuf {
    fleet_dir.join(format!("{run_id}.control.jsonl"))
}

/// Последний журнал прогона в каталоге флота (по mtime).
///
/// # Errors
/// Каталог пуст или не читается.
pub fn latest_log(fleet_dir: &Path) -> Result<PathBuf> {
    let mut best: Option<(std::time::SystemTime, PathBuf)> = None;
    for entry in std::fs::read_dir(fleet_dir).map_err(|e| HarnessError::io(fleet_dir, e))? {
        let entry = entry.map_err(|e| HarnessError::io(fleet_dir, e))?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("jsonl") {
            continue;
        }
        if path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| n.contains(".control."))
        {
            continue;
        }
        let mtime = entry
            .metadata()
            .and_then(|m| m.modified())
            .unwrap_or(std::time::UNIX_EPOCH);
        let newer = match &best {
            None => true,
            Some((t, _)) => mtime > *t,
        };
        if newer {
            best = Some((mtime, path));
        }
    }
    best.map(|(_, p)| p)
        .ok_or_else(|| HarnessError::Harness("нет журналов прогона в каталоге флота".into()))
}

/// Есть ли Kill-команда для агента (или `*`) в control-канале.
///
/// # Errors
/// Файл control не читается (битый JSON) — возвращает ошибку.
pub fn control_kills(path: &Path, agent_id: &str) -> Result<bool> {
    for rec in ControlChannel::read(path)? {
        if rec.cmd == ControlCommand::Kill && (rec.agent_id == agent_id || rec.agent_id == "*") {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Проекция журнала прогона в текстовый dashboard (агент → статус/тайминги).
///
/// # Errors
/// Журнал не читается.
pub fn render_log(path: &Path) -> Result<String> {
    let events = FleetLog::read_all(path)?;
    let mut s = String::new();
    if events.is_empty() {
        return Ok("журнал пуст или не найден".to_string());
    }
    // Порядок агентов: как встретились в событиях (детерминированно).
    let mut agents: Vec<String> = Vec::new();
    let mut started: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    let mut status: std::collections::HashMap<String, String> = std::collections::HashMap::new();
    // Статусы узлов плана (аддитивные события ADR-042): BTreeMap — порядок
    // вывода детерминирован.
    let mut nodes_status: std::collections::BTreeMap<String, String> =
        std::collections::BTreeMap::new();
    let mut n_heartbeat = 0usize;
    for e in &events {
        match e {
            FleetEvent::RunStarted { package, .. } => {
                s.push_str(&format!("Пакет: {package}\n"));
            }
            FleetEvent::ItemAssigned {
                item_id,
                agent_id,
                score,
                ..
            } => {
                s.push_str(&format!("  {item_id} → {agent_id} (score {score:.3})\n"));
            }
            FleetEvent::AgentStarted { agent_id, at, .. } => {
                if !agents.contains(agent_id) {
                    agents.push(agent_id.clone());
                }
                started.insert(agent_id.clone(), at.clone());
                status.insert(agent_id.clone(), "running".to_string());
            }
            FleetEvent::AgentHeartbeat { .. } => n_heartbeat += 1,
            FleetEvent::AgentDone {
                agent_id,
                exit_code,
                duration_secs,
                contract_status,
                ..
            } => {
                status.insert(
                    agent_id.clone(),
                    format!("done (exit {exit_code:?}, {duration_secs:.1}s, {contract_status})"),
                );
            }
            FleetEvent::AgentError {
                agent_id, error, ..
            } => {
                status.insert(agent_id.clone(), format!("error: {error}"));
            }
            FleetEvent::RunFinished {
                n_assigned,
                n_done,
                n_failed,
                n_unassigned,
                ..
            } => {
                s.push_str(&format!(
                    "\nИтог: назначено {n_assigned} / успешно {n_done} / сбой {n_failed} / нераспределено {n_unassigned}\n"
                ));
            }
            FleetEvent::Control { .. } => {}
            FleetEvent::PlanStarted {
                plan_id,
                pattern,
                n_nodes,
                n_waves,
                ..
            } => {
                s.push_str(&format!(
                    "План: {plan_id} (паттерн {pattern}, узлов {n_nodes}, волн {n_waves})\n"
                ));
            }
            FleetEvent::WaveStarted { wave, nodes, .. } => {
                s.push_str(&format!(
                    "\nВолна {}: {} узлов ({})\n",
                    wave + 1,
                    nodes.len(),
                    nodes.join(", ")
                ));
            }
            FleetEvent::NodeQueued { node_id, .. } => {
                nodes_status.insert(node_id.clone(), "в очереди пула".to_string());
            }
            FleetEvent::NodeCompleted { node_id, .. } => {
                nodes_status.insert(node_id.clone(), "completed".to_string());
            }
            FleetEvent::NodeSkipped {
                node_id, reason, ..
            } => {
                nodes_status.insert(node_id.clone(), format!("skipped ({reason})"));
            }
            FleetEvent::NodeRetry {
                node_id,
                attempt,
                max_attempts,
                ..
            } => {
                nodes_status.insert(node_id.clone(), format!("повтор {attempt}/{max_attempts}"));
            }
            FleetEvent::NodeGated {
                node_id,
                gate,
                verdict,
                detail,
                ..
            } => {
                nodes_status.insert(
                    node_id.clone(),
                    format!("гейт {gate}: {verdict} ({detail})"),
                );
            }
            FleetEvent::RunResumed { completed, .. } => {
                s.push_str(&format!(
                    "Возобновление: уже завершено узлов {}\n",
                    completed.len()
                ));
            }
            FleetEvent::RunHalted { reason, .. } => {
                s.push_str(&format!("\nОстановлен по гейту: {reason}\n"));
            }
        }
    }
    if n_heartbeat > 0 {
        s.push_str(&format!("heartbeats: {n_heartbeat}\n"));
    }
    s.push_str("\nАгенты:\n");
    for id in agents {
        let st = status.get(&id).map_or("—", String::as_str);
        let at = started.get(&id).map_or("", String::as_str);
        s.push_str(&format!("  {id:<16} {st}  (старт {at})\n"));
    }
    if !nodes_status.is_empty() {
        s.push_str("\nУзлы плана:\n");
        for (id, st) in &nodes_status {
            s.push_str(&format!("  {id:<24} {st}\n"));
        }
    }
    Ok(s)
}

/// Сводка прогресса прогона (для живого заголовка вкладки «Флот»):
/// считается из журнала целиком — дешево перечитывать по тику.
#[derive(Debug, Default, Clone)]
pub struct FleetProgress {
    /// Узлов всего (план; без плана — назначенные агенты).
    pub total_nodes: usize,
    /// Узлов завершено (NodeCompleted).
    pub done_nodes: usize,
    /// Агентов всего видели в журнале.
    pub n_agents: usize,
    /// Агентов сейчас в работе (started без done/error).
    pub running_agents: usize,
    /// Всего heartbeat-событий.
    pub n_heartbeat: usize,
    /// Прогон завершён штатно (RunFinished).
    pub finished: bool,
    /// Прогон остановлен по гейту (RunHalted).
    pub halted: bool,
    /// Время старта (первое событие с `at`, формат `%Y-%m-%d %H:%M:%S`).
    pub started_at: Option<chrono::NaiveDateTime>,
}

/// Прочитать прогресс из журнала прогона (JSONL).
///
/// # Errors
/// Те же, что у [`FleetLog::read_all`].
pub fn read_progress(path: &Path) -> Result<FleetProgress> {
    let events = FleetLog::read_all(path)?;
    let mut p = FleetProgress::default();
    let mut agents: Vec<String> = Vec::new();
    let mut done_agents: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut stamp = |at: &String| {
        if p.started_at.is_none() {
            p.started_at = chrono::NaiveDateTime::parse_from_str(at, "%Y-%m-%d %H:%M:%S").ok();
        }
    };
    for e in &events {
        match e {
            FleetEvent::PlanStarted { n_nodes, at, .. } => {
                p.total_nodes = *n_nodes;
                stamp(at);
            }
            FleetEvent::RunStarted { n_items, at, .. } => {
                if p.total_nodes == 0 {
                    p.total_nodes = *n_items;
                }
                stamp(at);
            }
            FleetEvent::NodeCompleted { at, .. } => {
                p.done_nodes += 1;
                stamp(at);
            }
            FleetEvent::AgentStarted { agent_id, at, .. } => {
                if !agents.contains(agent_id) {
                    agents.push(agent_id.clone());
                }
                stamp(at);
            }
            FleetEvent::AgentHeartbeat { .. } => p.n_heartbeat += 1,
            FleetEvent::AgentDone { agent_id, .. } | FleetEvent::AgentError { agent_id, .. } => {
                done_agents.insert(agent_id.clone());
            }
            FleetEvent::RunFinished { .. } => p.finished = true,
            FleetEvent::RunHalted { .. } => p.halted = true,
            _ => {}
        }
    }
    p.n_agents = agents.len();
    p.running_agents = agents.iter().filter(|a| !done_agents.contains(*a)).count();
    Ok(p)
}

/// Пороги «пульс-правила»: heartbeat моложе — прогон жив; старше — похоже на зависание.
pub const HEARTBEAT_ALIVE_SECS: u64 = 30;
/// За этим возрастом heartbeat — прогон почти наверняка мёртв/завис.
pub const HEARTBEAT_STALE_SECS: u64 = 90;

/// Живой заголовок прогресса вкладки «Флот» (одна-две строки, плейн-текст,
/// монохром-безопасен): шкала узлов, таймер, возраст heartbeat.
/// `gauge` — пара глифов (полный, пустой) из темы; `age_secs` — возраст
/// журнала (mtime) в секундах; `now` — текущее локальное время.
#[must_use]
pub fn progress_header(
    p: &FleetProgress,
    age_secs: Option<u64>,
    now: chrono::NaiveDateTime,
    gauge: (&str, &str),
) -> String {
    const CELLS: usize = 10;
    let mut parts = Vec::new();
    if p.total_nodes > 0 {
        let filled = p.done_nodes.saturating_mul(CELLS) / p.total_nodes;
        let pct = p.done_nodes.saturating_mul(100) / p.total_nodes;
        parts.push(format!(
            "{}{} {}/{} узлов ({}%)",
            gauge.0.repeat(filled),
            gauge.1.repeat(CELLS - filled),
            p.done_nodes,
            p.total_nodes,
            pct
        ));
    }
    if p.running_agents > 0 {
        parts.push(format!("в работе агентов: {}", p.running_agents));
    }
    // Таймер прогона: от первого события до сейчас (или до финиша — тогда
    // таймер не движется: финиш — точка остановки часов).
    if let Some(start) = p.started_at {
        let span = now.signed_duration_since(start).num_seconds().max(0) as u64;
        parts.push(format!("идёт {}", fmt_duration(span)));
    }
    // Пульс: возраст журнала против порогов (живой/стареющий/завис/финиш).
    let pulse = if p.halted {
        "✗ остановлен по гейту".to_string()
    } else if p.finished {
        "✓ завершён".to_string()
    } else {
        match age_secs {
            Some(s) if s <= HEARTBEAT_ALIVE_SECS => format!("● живой · heartbeat {s} с назад"),
            Some(s) if s <= HEARTBEAT_STALE_SECS => format!("◌ heartbeat {s} с назад"),
            Some(s) => format!(
                "✗ heartbeat {} назад — похоже на зависание",
                fmt_duration(s)
            ),
            None => "● живой".to_string(),
        }
    };
    parts.push(pulse);
    parts.join("  ·  ")
}

/// Длительность человекочитаемо: `0:47`, `12:47`, `1:02:47`.
fn fmt_duration(secs: u64) -> String {
    let (h, m, s) = (secs / 3600, (secs % 3600) / 60, secs % 60);
    if h > 0 {
        format!("{h}:{m:02}:{s:02}")
    } else {
        format!("{m}:{s:02}")
    }
}

/// Парсит маршрут из строки (для JSON-аргументов инструмента).
fn parse_route(s: &str) -> Option<crate::control::Route> {
    match s.to_ascii_lowercase().as_str() {
        "fast" => Some(crate::control::Route::Fast),
        "standard" => Some(crate::control::Route::Standard),
        "critical" => Some(crate::control::Route::Critical),
        _ => None,
    }
}

/// Парсит work-items из JSON-массива аргумента `items`.
pub fn parse_items(value: &Value) -> Result<Vec<WorkItem>> {
    let arr = value
        .as_array()
        .ok_or_else(|| HarnessError::Tool("fleet_run: 'items' должен быть массивом".into()))?;
    let mut out = Vec::new();
    for (i, v) in arr.iter().enumerate() {
        let id = v
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or(&format!("item-{i}"))
            .to_string();
        let spec = v
            .get("spec")
            .and_then(Value::as_str)
            .unwrap_or(&id)
            .to_string();
        let required_skills: Vec<String> = v
            .get("required_skills")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let tags: Vec<String> = v
            .get("tags")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let route = match v.get("route").and_then(Value::as_str) {
            Some(s) => parse_route(s)
                .ok_or_else(|| HarnessError::Tool(format!("fleet_run: неизвестный route '{s}'")))?,
            None => crate::control::Route::Standard,
        };
        let domain = v.get("domain").and_then(Value::as_str).map(str::to_owned);
        let effort = v.get("effort").and_then(Value::as_u64).unwrap_or(1) as usize;
        out.push(WorkItem {
            id,
            spec,
            required_skills,
            tags,
            route,
            domain,
            effort,
        });
    }
    Ok(out)
}

/// Инструмент агента: `fleet_run` — прогнать флот по декомпозированному пакету.
/// Фоновый прогон флота: задача `flt-*` в общем реестре фоновых задач,
/// исполнение — в отдельной tokio-задаче; инструмент возвращается немедленно.
/// Живой прогон — вкладка F6 (панель «Флот» читает `latest_log` из
/// `state/fleet/`), по завершении — `BackgroundNotice` в TUI, результат —
/// через `subagent_result`. Прерывание хода фоновый флот НЕ затрагивает.
fn launch_background(
    cfg: std::sync::Arc<Config>,
    state_dir: PathBuf,
    repo: PathBuf,
    package: String,
    items: Vec<WorkItem>,
    agents: Vec<AgentProfile>,
    ctx: &crate::tool::ToolContext,
) -> crate::tool::ToolOutput {
    let Some(registry) = &ctx.subagents else {
        return crate::tool::ToolOutput::err(
            "fleet_run background: реестр фоновых задач не подключён \
             (headless-режим без TUI/раннера) — запустите без background",
        );
    };
    if registry.running() >= registry.capacity() {
        return crate::tool::ToolOutput::err(format!(
            "все слоты фоновых задач заняты ({}); дождитесь завершения — subagent_list",
            registry.capacity()
        ));
    }
    let id = registry.next_id("flt");
    registry.insert(crate::subagent::SubagentTask {
        id: id.clone(),
        agent: "fleet".into(),
        task: package.chars().take(2000).collect(),
        status: crate::subagent::TaskStatus::Running,
        report: String::new(),
        started_at: crate::subagent::now_iso(),
        finished_at: None,
    });
    let registry = registry.clone();
    let run_id = id.clone();
    let tctx = ctx.clone();
    tokio::spawn(async move {
        let outcome = run_fleet(
            &cfg,
            &state_dir,
            &repo,
            &package,
            &items,
            &agents,
            Some(tctx),
        )
        .await;
        let (status, report) = match outcome {
            Ok(o) => (crate::subagent::TaskStatus::Done, render_outcome(&o)),
            Err(e) => (
                crate::subagent::TaskStatus::Failed,
                format!("флот упал: {e}"),
            ),
        };
        registry.finish(&run_id, status, report);
    });
    crate::tool::ToolOutput::ok(format!(
        "Флот запущен в ФОНЕ: {id}. Ход агента НЕ ждёт завершения. Живой прогон — \
         вкладка F6 (панель «Флот»), журнал — state/fleet/. Статус — subagent_list, \
         результат — subagent_result(id=\"{id}\"). Сообщи пользователю, что флот идёт \
         в фоне, и продолжай диалог."
    ))
}

pub struct FleetRunTool;

#[async_trait::async_trait]
impl crate::tool::Tool for FleetRunTool {
    fn spec(&self) -> crate::llm::ToolSpec {
        crate::llm::ToolSpec {
            name: "fleet_run".into(),
            description: "Прогнать флот кодовых харнессов по декомпозированному пакету: \
                назначает work-items агентам детерминированным роутером (capability × load × \
                route/risk × cost) и гонит конкурентно, пиша живой стрим событий в \
                state/fleet/<run-id>.jsonl. Каждый item — {id, spec, required_skills, tags, \
                route(fast|standard|critical), domain, effort}. Вызывай после декомпозиции \
                handoff-пакета на независимые items."
                .into(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Корень репозитория (относительно cwd или абсолютный)"},
                    "package": {"type": "string", "description": "Метка пакета/эпика (для журнала)"},
                    "items": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Work-items: {id, spec, required_skills[], tags[], route, domain, effort}"
                    },
                    "background": {
                        "type": "boolean",
                        "description": "true — прогон флота в фоне: инструмент возвращается сразу (задача flt-*), агент остаётся доступен; живой прогон — вкладка F6, статус — subagent_list, результат — subagent_result(id). false/отсутствует — синхронно: ход агента ждёт завершения флота"
                    }
                },
                "required": ["repo", "items"]
            }),
        }
    }

    async fn call(
        &self,
        args: Value,
        ctx: &crate::tool::ToolContext,
    ) -> Result<crate::tool::ToolOutput> {
        let Some(repo) = args.get("repo").and_then(Value::as_str) else {
            return Ok(crate::tool::ToolOutput::err(
                "fleet_run: обязательный аргумент 'repo' (string) отсутствует",
            ));
        };
        let package = args
            .get("package")
            .and_then(Value::as_str)
            .unwrap_or("fleet");
        let items = match args.get("items") {
            Some(v) => match parse_items(v) {
                Ok(i) => i,
                Err(e) => return Ok(crate::tool::ToolOutput::err(format!("fleet_run: {e}"))),
            },
            None => {
                return Ok(crate::tool::ToolOutput::err(
                    "fleet_run: обязательный аргумент 'items' (массив work-items) отсутствует",
                ));
            }
        };
        let cfg = ctx.config.clone();
        let state_dir = cfg.paths.state_dir.clone();
        let agents = harness_profiles(&cfg);
        if agents.is_empty() {
            return Ok(crate::tool::ToolOutput::err(
                "fleet_run: нет настроенных харнессов ([harnesses.*] в config.toml)",
            ));
        }
        let repo = ctx.resolve(repo);
        // Фоновый прогон: вернуть сразу, исполнение — отдельная tokio-задача.
        if args.get("background").and_then(Value::as_bool) == Some(true) {
            return Ok(launch_background(
                cfg,
                state_dir,
                repo,
                package.to_string(),
                items,
                agents,
                ctx,
            ));
        }
        match run_fleet(
            &cfg,
            &state_dir,
            &repo,
            package,
            &items,
            &agents,
            Some(ctx.clone()),
        )
        .await
        {
            Ok(outcome) => Ok(crate::tool::ToolOutput::ok(render_outcome(&outcome))),
            Err(e) => Ok(crate::tool::ToolOutput::err(format!("fleet_run: {e}"))),
        }
    }

    fn timeout_secs(&self) -> u64 {
        7200 + 120
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn progress_counts_nodes_agents_and_renders_header() {
        let dir = tempfile::tempdir().expect("tmp");
        let path = dir.path().join("fpl-p.jsonl");
        std::fs::write(
            &path,
            concat!(
                "{\"type\":\"plan_started\",\"run_id\":\"fpl-p\",\"plan_id\":\"demo\",\"pattern\":\"fanout\",\"plan_path\":\"p.toml\",\"plan_sha256\":\"h\",\"n_nodes\":2,\"n_waves\":1,\"at\":\"2026-09-12 16:00:00\"}\n",
                "{\"type\":\"wave_started\",\"run_id\":\"fpl-p\",\"wave\":0,\"nodes\":[\"n0\"],\"at\":\"2026-09-12 16:00:00\"}\n",
                "{\"type\":\"agent_started\",\"run_id\":\"fpl-p\",\"agent_id\":\"a0\",\"item_id\":\"n0\",\"kind\":\"harness:worker\",\"at\":\"2026-09-12 16:00:01\"}\n",
                "{\"type\":\"agent_started\",\"run_id\":\"fpl-p\",\"agent_id\":\"a1\",\"item_id\":\"n1\",\"kind\":\"harness:worker\",\"at\":\"2026-09-12 16:00:02\"}\n",
                "{\"type\":\"agent_heartbeat\",\"run_id\":\"fpl-p\",\"agent_id\":\"a0\",\"item_id\":\"n0\",\"at\":\"2026-09-12 16:00:05\"}\n",
                "{\"type\":\"node_completed\",\"run_id\":\"fpl-p\",\"node_id\":\"n0\",\"at\":\"2026-09-12 16:00:07\"}\n",
            ),
        )
        .expect("log");
        let p = read_progress(&path).expect("progress");
        assert_eq!(p.total_nodes, 2);
        assert_eq!(p.done_nodes, 1);
        assert_eq!(p.n_agents, 2);
        assert_eq!(p.running_agents, 2, "оба агента в работе (done/error нет)");
        assert_eq!(p.n_heartbeat, 1);
        assert!(!p.finished && !p.halted);
        assert!(p.started_at.is_some());
        let now = chrono::NaiveDate::from_ymd_opt(2026, 9, 12)
            .unwrap()
            .and_hms_opt(16, 0, 20)
            .unwrap();
        let h = progress_header(&p, Some(4), now, ("▰", "▱"));
        assert!(h.contains("1/2 узлов (50%)"), "{h}");
        assert!(h.contains("▰▰▰▰▰▱▱▱▱▱"), "{h}");
        assert!(h.contains("● живой"), "{h}");
        assert!(h.contains("идёт 0:20"), "{h}");
        // Пороги пульса.
        let stale = progress_header(&p, Some(120), now, ("▰", "▱"));
        assert!(stale.contains("зависание"), "{stale}");
        // Финиш: часы остановлены, маркер ✓.
        let p2 = FleetProgress {
            finished: true,
            ..p.clone()
        };
        let h2 = progress_header(&p2, Some(300), now, ("▰", "▱"));
        assert!(h2.contains("✓ завершён"), "{h2}");
    }

    #[test]
    fn event_serde_roundtrip() {
        let e = FleetEvent::ItemAssigned {
            run_id: "flt-1".into(),
            item_id: "i1".into(),
            agent_id: "claude-code".into(),
            score: 1.5,
            reason: vec!["capability 1/1 skills".into()],
            at: "2026-09-11 00:00:00".into(),
        };
        let line = serde_json::to_string(&e).expect("ser");
        assert!(line.contains("\"type\":\"item_assigned\""), "{line}");
        let back: FleetEvent = serde_json::from_str(&line).expect("de");
        assert_eq!(back.kind(), FleetEventKind::ItemAssigned);
    }

    #[test]
    fn concurrent_appends_do_not_interleave() {
        // Регрессия на гонку, найденную живым прогоном: `writeln!` мог
        // отправить payload и `\n` двумя системными вызовами, и дописи
        // конкурентных задач склеивались в одну битую строку.
        let dir = tempfile::tempdir().expect("tmp");
        let log = std::sync::Arc::new(FleetLog::new(dir.path().join("fleet/flt-c.jsonl")));
        let mut handles = Vec::new();
        for i in 0..32 {
            let log = log.clone();
            handles.push(std::thread::spawn(move || {
                for j in 0..25 {
                    log.append(&FleetEvent::NodeQueued {
                        run_id: "flt-c".into(),
                        node_id: format!("n{i}-{j}"),
                        at: "t".into(),
                    })
                    .expect("append");
                }
            }));
        }
        for h in handles {
            h.join().expect("join");
        }
        let events = FleetLog::read_all(log.path()).expect("read");
        assert_eq!(events.len(), 32 * 25);
    }

    #[test]
    fn tolerant_read_skips_unknown_and_broken_lines() {
        let dir = tempfile::tempdir().expect("tmp");
        std::fs::create_dir_all(dir.path().join("fleet")).expect("dir");
        let path = dir.path().join("fleet/flt-t.jsonl");
        let mut text = String::new();
        text.push_str(
            "{\"type\":\"node_completed\",\"run_id\":\"r\",\"node_id\":\"a\",\"at\":\"t\"}\n",
        );
        text.push_str("{\"type\":\"from_the_future\",\"x\":1}\n");
        text.push_str("{\"type\":\"node_que\n");
        std::fs::write(&path, text).expect("write");
        let read = FleetLog::read_tolerant(&path).expect("read");
        assert_eq!(read.events.len(), 1);
        assert_eq!(read.skipped.len(), 2, "{:?}", read.skipped);
    }

    #[test]
    fn log_append_and_read_roundtrip() {
        let dir = tempfile::tempdir().expect("tmp");
        let log = FleetLog::new(dir.path().join("fleet/flt-1.jsonl"));
        log.append(&FleetEvent::RunStarted {
            run_id: "flt-1".into(),
            package: "pkg".into(),
            n_items: 2,
            n_agents: 2,
            at: "t".into(),
        })
        .expect("append");
        let events = FleetLog::read_all(log.path()).expect("read");
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].kind(), FleetEventKind::RunStarted);
    }

    #[test]
    fn control_channel_roundtrip() {
        let dir = tempfile::tempdir().expect("tmp");
        let ch = ControlChannel::new(dir.path().join("control.jsonl"));
        ch.send("claude-code", ControlCommand::Kill, None)
            .expect("send");
        let recs = ControlChannel::read(&dir.path().join("control.jsonl")).expect("read");
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].cmd, ControlCommand::Kill);
        assert_eq!(recs[0].agent_id, "claude-code");
    }

    #[test]
    fn control_all_commands_roundtrip() {
        let dir = tempfile::tempdir().expect("tmp");
        let ch = ControlChannel::new(dir.path().join("control.jsonl"));
        ch.send("a", ControlCommand::Pause, None).unwrap();
        ch.send("a", ControlCommand::Resume, None).unwrap();
        ch.send("a", ControlCommand::Reroute, Some("b")).unwrap();
        ch.send("a", ControlCommand::Priority, Some("3")).unwrap();
        let recs = ControlChannel::read(&dir.path().join("control.jsonl")).unwrap();
        assert_eq!(recs.len(), 4);
        assert_eq!(recs[0].cmd, ControlCommand::Pause);
        assert_eq!(recs[1].cmd, ControlCommand::Resume);
        assert_eq!(recs[2].cmd, ControlCommand::Reroute);
        assert_eq!(recs[2].payload.as_deref(), Some("b"));
        assert_eq!(recs[3].cmd, ControlCommand::Priority);
        assert_eq!(recs[3].payload.as_deref(), Some("3"));
    }

    #[test]
    fn harness_profiles_cover_configured_harnesses() {
        let cfg = Config::default();
        let profiles = harness_profiles(&cfg);
        assert!(!profiles.is_empty());
        for p in &profiles {
            assert_eq!(p.kind, AgentKind::Harness);
            assert!(p.skills.contains(&"code".to_string()));
        }
        assert!(profiles.iter().any(|p| p.id == "claude-code"));
    }

    #[test]
    fn parse_items_builds_work_items() {
        let v: Value = serde_json::json!([
            {"id": "ADR-1", "spec": "сделать", "required_skills": ["rust"],
             "route": "critical", "domain": "code", "effort": 3}
        ]);
        let items = parse_items(&v).expect("parse");
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].id, "ADR-1");
        assert_eq!(items[0].route, crate::control::Route::Critical);
        assert_eq!(items[0].required_skills, vec!["rust".to_string()]);
    }

    #[test]
    fn render_log_shows_assignments_and_agent_status() {
        let dir = tempfile::tempdir().expect("tmp");
        let log = FleetLog::new(dir.path().join("flt-1.jsonl"));
        log.append(&FleetEvent::RunStarted {
            run_id: "flt-1".into(),
            package: "pkg".into(),
            n_items: 1,
            n_agents: 1,
            at: "t".into(),
        })
        .unwrap();
        log.append(&FleetEvent::ItemAssigned {
            run_id: "flt-1".into(),
            item_id: "i1".into(),
            agent_id: "claude-code".into(),
            score: 1.5,
            reason: vec!["x".into()],
            at: "t".into(),
        })
        .unwrap();
        log.append(&FleetEvent::AgentStarted {
            run_id: "flt-1".into(),
            agent_id: "claude-code".into(),
            item_id: "i1".into(),
            kind: "harness".into(),
            at: "t".into(),
        })
        .unwrap();
        log.append(&FleetEvent::AgentDone {
            run_id: "flt-1".into(),
            agent_id: "claude-code".into(),
            item_id: "i1".into(),
            exit_code: Some(0),
            duration_secs: 1.0,
            contract_status: "complete".into(),
            at: "t".into(),
        })
        .unwrap();
        log.append(&FleetEvent::RunFinished {
            run_id: "flt-1".into(),
            n_assigned: 1,
            n_done: 1,
            n_failed: 0,
            n_unassigned: 0,
            at: "t".into(),
        })
        .unwrap();
        let s = render_log(log.path()).expect("render");
        assert!(s.contains("i1 → claude-code"), "{s}");
        assert!(s.contains("done"), "{s}");
        assert!(s.contains("Итог: назначено 1"), "{s}");
    }

    #[test]
    fn control_kills_detects_target_and_wildcard() {
        let dir = tempfile::tempdir().expect("tmp");
        let path = dir.path().join("c.jsonl");
        let ch = ControlChannel::new(path.clone());
        ch.send("claude-code", ControlCommand::Kill, None).unwrap();
        assert!(control_kills(&path, "claude-code").unwrap());
        assert!(!control_kills(&path, "qwen-code").unwrap());
        ch.send("*", ControlCommand::Kill, None).unwrap();
        assert!(control_kills(&path, "qwen-code").unwrap());
    }

    #[tokio::test]
    async fn run_fleet_routes_and_streams_with_fake_harness() {
        let dir = tempfile::tempdir().expect("tmp");
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).expect("repo");
        let state = dir.path().join("state");

        // Фейковый харнесс: `cat` читает задачу из stdin и отдаёт в stdout.
        let mut cfg = Config::default();
        cfg.harnesses.insert(
            "fake".into(),
            crate::config::CodingHarnessConfig {
                binary: "cat".into(),
                prompt_mode: crate::config::PromptMode::Stdin,
                timeout_secs: 30,
                ..crate::config::CodingHarnessConfig::default()
            },
        );

        let items = vec![WorkItem {
            id: "i1".into(),
            spec: "echo me".into(),
            required_skills: vec![],
            tags: vec![],
            route: crate::control::Route::Fast,
            domain: None,
            effort: 1,
        }];
        let agents = vec![AgentProfile {
            id: "fake".into(),
            kind: AgentKind::Harness,
            skills: vec!["code".into()],
            tools: vec![],
            tags: vec!["code".into()],
            load: 0,
            capacity: 1,
            cost_tier: CostTier::Cheap,
        }];

        let outcome = run_fleet(&cfg, &state, &repo, "demo", &items, &agents, None)
            .await
            .expect("run");
        assert_eq!(outcome.n_done, 1);
        assert_eq!(outcome.plan.assignments.len(), 1);

        // Журнал несёт полный жизненный цикл прогона.
        let events = FleetLog::read_all(&outcome.log_path).expect("read");
        let kinds: Vec<_> = events.iter().map(FleetEvent::kind).collect();
        for expected in [
            FleetEventKind::RunStarted,
            FleetEventKind::ItemAssigned,
            FleetEventKind::AgentStarted,
            FleetEventKind::AgentDone,
            FleetEventKind::RunFinished,
        ] {
            assert!(kinds.contains(&expected), "нет {expected:?}");
        }
    }

    #[tokio::test]
    async fn fleet_run_background_registers_and_completes() {
        use crate::tool::Tool;
        let dir = tempfile::tempdir().expect("tmp");
        let mut cfg = Config::default();
        // Только один харнесс (cat) — иначе роутер угонит item на дефолтный
        // бинарь (qwen и т.п.), которого нет/который зависнет.
        cfg.harnesses.clear();
        cfg.harnesses.insert(
            "claude-code".into(),
            crate::config::CodingHarnessConfig {
                binary: "cat".into(),
                prompt_mode: crate::config::PromptMode::Stdin,
                timeout_secs: 30,
                ..crate::config::CodingHarnessConfig::default()
            },
        );
        // Изолируем журнал флота в tempdir (не пишем в ~/.arch-ml/state).
        cfg.paths.state_dir = dir.path().join("state");
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).expect("repo");
        let registry = crate::subagent::SubagentRegistry::new();
        let ctx = crate::tool::ToolContext::new(repo.clone(), std::sync::Arc::new(cfg))
            .with_subagents(registry.clone());

        let tool = FleetRunTool;
        let out = tool
            .call(
                serde_json::json!({
                    "repo": repo.to_string_lossy(),
                    "package": "bg",
                    "items": [{"id": "ADR-1", "spec": "инвариант", "route": "fast"}],
                    "background": true
                }),
                &ctx,
            )
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("flt-"), "id задачи: {}", out.content);
        assert_eq!(registry.running(), 1, "флот числится запущенным");

        // cat завершается мгновенно — дожидаемся финиша задачи флота.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        loop {
            let task = registry
                .list()
                .into_iter()
                .find(|t| t.id.starts_with("flt-"))
                .expect("задача flt-* в реестре");
            if task.status != crate::subagent::TaskStatus::Running {
                assert_eq!(
                    task.status,
                    crate::subagent::TaskStatus::Done,
                    "отчёт: {}",
                    task.report
                );
                assert!(task.report.contains("назначено"), "отчёт: {}", task.report);
                assert!(task.finished_at.is_some());
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "фоновый флот не завершился за 10 с"
            );
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    }
}
