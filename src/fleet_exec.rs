//! Исполнитель плана флота: волны по топологическим уровням, пул процессов,
//! гейты узла, ретраи, возобновление из журнала (ADR-042).
//!
//! Отличие от [`crate::fleet_run::run_fleet`] (плоский веер одной волной):
//! здесь исполняется ГРАФ. Узлы идут волнами по топологическим уровням
//! ([`crate::fleet_plan::CompiledPlan::levels`]); внутри волны конкурентность
//! ограничена `tokio::sync::Semaphore` — потолок `policy.max_parallel`, а не
//! «спавн на каждый узел». Это и делает исполнимым флот из сотен узлов:
//! сто узлов в плане — сто worktree, но одновременно работающих процессов
//! ровно столько, сколько разрешил владелец.
//!
//! Каждый узел: worktree с детерминированным именем (`<run-id>-<node-id>`,
//! идемпотентно для resume) → прогон харнесса с таймаутом из плана / MANIFEST /
//! маршрута → механические гейты ([`crate::fleet_gate::run_gates`]) →
//! `NodeCompleted` либо политика отказа (`retry | block | skip | continue`).
//! Узел-судья (`tournament`) процесса не запускает: победитель выбирается
//! детерминированно — первый по id узел группы, прошедший все гейты.
//!
//! Управление на лету — тот же [`ControlChannel`](crate::fleet_run::ControlChannel),
//! что и у веера: `pause`/`resume`/`kill` адресуются узлу или `*`.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::config::{CodingHarnessConfig, Config};
use crate::error::{HarnessError, Result};
use crate::fleet_gate::{GateContext, NodeOutcome, run_gates};
use crate::fleet_plan::{
    CompiledPlan, FailPolicy, FleetPlan, NodeRole, PlanNode, compile, parse_plan,
};
use crate::fleet_run::{ControlChannel, ControlCommand, FleetEvent, FleetLog};
use crate::router::{AgentProfile, WorkItem};

/// Режим прогона плана.
#[derive(Debug, Clone)]
pub enum ResumeMode {
    /// Новый прогон.
    Fresh,
    /// Продолжение: завершённые узлы берутся из журнала.
    Resume {
        /// Идентификатор прогона.
        run_id: String,
        /// Явно разрешить повтор узлов без worktree-изоляции (небезопасно).
        force_rerun: bool,
    },
}

/// Итог прогона плана.
#[derive(Debug, Clone)]
pub struct PlanRunOutcome {
    /// Идентификатор прогона.
    pub run_id: String,
    /// Идентификатор плана.
    pub plan_id: String,
    /// Паттерн оркестрации.
    pub pattern: String,
    /// Число волн.
    pub n_waves: usize,
    /// Число узлов.
    pub n_nodes: usize,
    /// Успешно завершённые узлы.
    pub n_done: usize,
    /// Провалившиеся узлы.
    pub n_failed: usize,
    /// Пропущенные узлы (возобновление, блокировка зависимостью).
    pub n_skipped: usize,
    /// Причина остановки по гейту (`None` — прогон дошёл до конца).
    pub halted: Option<String>,
    /// Путь к журналу событий.
    pub log_path: PathBuf,
    /// Worktree-каталоги узлов (для последующего `fleet merge`).
    pub worktrees: Vec<(String, PathBuf)>,
}

/// Текстовый отчёт прогона плана.
#[must_use]
pub fn render_plan_outcome(outcome: &PlanRunOutcome) -> String {
    use std::fmt::Write as _;
    let mut s = String::new();
    let _ = writeln!(
        s,
        "Прогон плана {} ({}): run-id {}",
        outcome.plan_id, outcome.pattern, outcome.run_id
    );
    let _ = writeln!(
        s,
        "Узлов: {} в {} волнах; завершено {}, сбой {}, пропущено {}",
        outcome.n_nodes, outcome.n_waves, outcome.n_done, outcome.n_failed, outcome.n_skipped
    );
    if let Some(reason) = &outcome.halted {
        let _ = writeln!(s, "Остановлен по гейту: {reason}");
    }
    let _ = writeln!(s, "Журнал: {}", outcome.log_path.display());
    if !outcome.worktrees.is_empty() {
        let _ = writeln!(s, "Worktree узлов: {}", outcome.worktrees.len());
        for (node, path) in &outcome.worktrees {
            let _ = writeln!(s, "  {node:<24} {}", path.display());
        }
        let _ = writeln!(
            s,
            "Интеграция — только гейтом владельца: fleet merge <run-id> --owner-approve"
        );
    }
    s
}

/// Состояние узла в прогоне.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NodeState {
    Completed,
    Failed,
    Skipped,
}

/// Продолжает прогон плана из снимка и журнала.
///
/// # Errors
/// Снимок плана/журнал не найдены или нечитаемы; см. [`run_plan`].
pub async fn resume_plan(
    cfg: &Config,
    state_dir: &Path,
    repo: &Path,
    run_id: &str,
    force_rerun: bool,
    agents: &[AgentProfile],
) -> Result<PlanRunOutcome> {
    let fleet_dir = state_dir.join("fleet");
    let snapshot = fleet_dir.join(format!("{run_id}.plan.toml"));
    if !snapshot.is_file() {
        return Err(HarnessError::Fleet(format!(
            "снимок плана прогона не найден: {} (resume — только для прогонов плана)",
            snapshot.display()
        )));
    }
    let plan = parse_plan(&snapshot)?;
    run_plan(
        cfg,
        state_dir,
        repo,
        plan,
        agents,
        ResumeMode::Resume {
            run_id: run_id.to_string(),
            force_rerun,
        },
    )
    .await
}

/// Исполняет план: компилирует паттерн в граф и прогоняет волнами.
///
/// # Errors
/// План не компилируется; нет настроенных харнессов; каталог состояния не
/// создаётся. Отказы отдельных узлов — НЕ ошибка вызова (события журнала).
pub async fn run_plan(
    cfg: &Config,
    state_dir: &Path,
    repo: &Path,
    plan: FleetPlan,
    agents: &[AgentProfile],
    resume: ResumeMode,
) -> Result<PlanRunOutcome> {
    let compiled = compile(plan)?;
    if agents.is_empty() {
        return Err(HarnessError::Fleet(
            "нет настроенных кодовых харнессов ([harnesses.*] в config.toml)".to_string(),
        ));
    }

    let fleet_dir = state_dir.join("fleet");
    std::fs::create_dir_all(&fleet_dir).map_err(|e| HarnessError::io(&fleet_dir, e))?;

    let (run_id, already_done, resumed) = match &resume {
        ResumeMode::Fresh => (
            format!("fpl-{}", chrono::Local::now().timestamp_millis()),
            HashSet::new(),
            false,
        ),
        ResumeMode::Resume {
            run_id,
            force_rerun,
        } => {
            if !compiled.plan.policy.require_worktree && !*force_rerun {
                return Err(HarnessError::Fleet(
                    "resume без worktree-изоляции неидемпотентен: повторный прогон узла \
                     ложится поверх уже сделанной работы. Включите require_worktree в плане \
                     или передайте --force-rerun осознанно"
                        .to_string(),
                ));
            }
            let log_path = fleet_dir.join(format!("{run_id}.jsonl"));
            let read = FleetLog::read_tolerant(&log_path)?;
            let done: HashSet<String> = read
                .events
                .iter()
                .filter_map(|e| match e {
                    FleetEvent::NodeCompleted { node_id, .. } => Some(node_id.clone()),
                    _ => None,
                })
                .collect();
            (run_id.clone(), done, true)
        }
    };

    // Политика worktree плана перекрывает конфиг: план описывает прогон целиком.
    let mut cfg_owned = cfg.clone();
    cfg_owned.fleet.require_worktree = compiled.plan.policy.require_worktree;
    let cfg_arc = Arc::new(cfg_owned);
    let cfg = cfg_arc.as_ref();

    let policy = crate::policy::Policy::parse(&compiled.plan.policy.autonomy)
        .or_else(|_| crate::policy::Policy::parse(&cfg.policy.autonomy))?;

    let log = FleetLog::new(fleet_dir.join(format!("{run_id}.jsonl")));
    let snapshot = snapshot_plan(&compiled.plan, &fleet_dir, &run_id)?;
    let sha = crate::managed::Sha256::of_text(&snapshot);
    log.append(&FleetEvent::PlanStarted {
        run_id: run_id.clone(),
        plan_id: compiled.plan.id.clone(),
        pattern: compiled.plan.pattern.as_str().to_string(),
        plan_path: compiled.plan.source.to_string_lossy().into_owned(),
        plan_sha256: sha.as_hex().to_string(),
        n_nodes: compiled.nodes().len(),
        n_waves: compiled.levels.len(),
        at: now(),
    })?;
    if resumed {
        let mut done: Vec<String> = already_done.iter().cloned().collect();
        done.sort();
        log.append(&FleetEvent::RunResumed {
            run_id: run_id.clone(),
            completed: done,
            from_wave: 0,
            at: now(),
        })?;
    }

    let agent_map = assign_agents(cfg, compiled.nodes(), agents);

    let controls: Arc<std::sync::Mutex<HashMap<String, Arc<crate::harness::HarnessControl>>>> =
        Arc::new(std::sync::Mutex::new(HashMap::new()));
    let ctrl_path = crate::fleet_run::control_path(&fleet_dir, &run_id);
    let watcher = spawn_control_watcher(controls.clone(), ctrl_path.clone());

    let semaphore = Arc::new(tokio::sync::Semaphore::new(
        compiled.plan.policy.max_parallel.max(1),
    ));
    let mut states: HashMap<String, NodeState> = HashMap::new();
    let mut outcomes: HashMap<String, NodeOutcome> = HashMap::new();
    let mut worktrees: Vec<(String, PathBuf)> = Vec::new();
    let mut n_done = 0usize;
    let mut n_failed = 0usize;
    let mut n_skipped = 0usize;
    let mut halted: Option<String> = None;

    for (wave, level) in compiled.levels.iter().enumerate() {
        let mut wave_nodes: Vec<String> = Vec::new();
        let mut handles = Vec::new();

        for idx in level {
            let Some(node) = compiled.nodes().get(*idx) else {
                continue;
            };
            let deps = compiled.incoming.get(&node.id).cloned().unwrap_or_default();

            if already_done.contains(&node.id) {
                states.insert(node.id.clone(), NodeState::Completed);
                n_skipped += 1;
                log.append(&FleetEvent::NodeSkipped {
                    run_id: run_id.clone(),
                    node_id: node.id.clone(),
                    reason: "resumed".to_string(),
                    at: now(),
                })?;
                continue;
            }
            if let Some(reason) = blocked_by(&deps, &compiled, &states) {
                states.insert(node.id.clone(), NodeState::Skipped);
                n_skipped += 1;
                log.append(&FleetEvent::NodeSkipped {
                    run_id: run_id.clone(),
                    node_id: node.id.clone(),
                    reason,
                    at: now(),
                })?;
                continue;
            }

            // Судья турнира процесса не запускает: выбор детерминированный.
            if node.role == NodeRole::Judge {
                if let Some(winner) = judge_decision(&deps, &states) {
                    log.append(&FleetEvent::NodeGated {
                        run_id: run_id.clone(),
                        node_id: node.id.clone(),
                        gate: "judge".to_string(),
                        verdict: "ok".to_string(),
                        detail: format!("победитель группы: {winner}"),
                        at: now(),
                    })?;
                    states.insert(node.id.clone(), NodeState::Completed);
                    n_done += 1;
                    log.append(&FleetEvent::NodeCompleted {
                        run_id: run_id.clone(),
                        node_id: node.id.clone(),
                        at: now(),
                    })?;
                } else {
                    states.insert(node.id.clone(), NodeState::Failed);
                    n_failed += 1;
                    log.append(&FleetEvent::NodeGated {
                        run_id: run_id.clone(),
                        node_id: node.id.clone(),
                        gate: "judge".to_string(),
                        verdict: "fail".to_string(),
                        detail: "ни один узел группы не прошёл гейты".to_string(),
                        at: now(),
                    })?;
                }
                continue;
            }

            let agent = agent_map
                .get(&node.id)
                .cloned()
                .unwrap_or_else(|| node.id.clone());
            let Some(hcfg) = cfg.harnesses.get(&agent).cloned() else {
                states.insert(node.id.clone(), NodeState::Failed);
                n_failed += 1;
                log.append(&FleetEvent::AgentError {
                    run_id: run_id.clone(),
                    agent_id: agent,
                    item_id: node.id.clone(),
                    error: "харнесс узла не настроен в config.toml".to_string(),
                    at: now(),
                })?;
                continue;
            };
            wave_nodes.push(node.id.clone());

            let node_owned = node.clone();
            let plan_owned = compiled.plan.clone();
            let compiled_owned = compiled.clone();
            let dep_summaries = dep_summaries_for(&deps, &compiled, &outcomes);
            // Работа зависимостей, уже завершившихся в прошлых волнах: её
            // ветки вливаются в worktree этого узла, иначе ревьювер и
            // интегратор видели бы пустое дерево (review_pair/map_reduce).
            let dep_worktrees: Vec<(String, PathBuf)> = deps
                .iter()
                .filter_map(|dep| {
                    worktrees
                        .iter()
                        .find(|(id, _)| id == dep)
                        .map(|(id, path)| (id.clone(), path.clone()))
                })
                .collect();
            let sem = semaphore.clone();
            let cfg_task = cfg_arc.clone();
            let log_task = log.clone();
            let run_id_task = run_id.clone();
            let fleet_dir_task = fleet_dir.clone();
            let ctrl_path_task = ctrl_path.clone();
            let repo_task = repo.to_path_buf();
            let controls_task = controls.clone();
            let handle = tokio::spawn(async move {
                if sem.available_permits() == 0 {
                    log_task
                        .append(&FleetEvent::NodeQueued {
                            run_id: run_id_task.clone(),
                            node_id: node_owned.id.clone(),
                            at: now(),
                        })
                        .ok();
                }
                let Ok(_permit) = sem.acquire_owned().await else {
                    return (node_owned.id.clone(), NodeState::Failed, None, None);
                };
                let control = Arc::new(crate::harness::HarnessControl::default());
                controls_task
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .insert(node_owned.id.clone(), control.clone());
                let ctx = NodeRun {
                    cfg: cfg_task.as_ref(),
                    plan: &plan_owned,
                    compiled: &compiled_owned,
                    node: &node_owned,
                    agent: &agent,
                    repo: &repo_task,
                    run_id: &run_id_task,
                    fleet_dir: &fleet_dir_task,
                    log: &log_task,
                    control,
                    policy,
                    dep_summaries,
                    dep_worktrees,
                    harness: hcfg,
                    ctrl_path: ctrl_path_task,
                };
                let (state, outcome, worktree) = run_node(&ctx).await;
                controls_task
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .remove(&node_owned.id);
                (node_owned.id.clone(), state, outcome, worktree)
            });
            handles.push(handle);
        }

        log.append(&FleetEvent::WaveStarted {
            run_id: run_id.clone(),
            wave,
            nodes: wave_nodes,
            at: now(),
        })?;

        for handle in handles {
            match handle.await {
                Ok((node_id, state, outcome, worktree)) => {
                    states.insert(node_id.clone(), state);
                    match state {
                        NodeState::Completed => n_done += 1,
                        NodeState::Failed => n_failed += 1,
                        NodeState::Skipped => n_skipped += 1,
                    }
                    if let Some(path) = worktree {
                        worktrees.push((node_id.clone(), path));
                    }
                    if let Some(o) = outcome {
                        outcomes.insert(node_id, o);
                    }
                }
                Err(e) => {
                    n_failed += 1;
                    if halted.is_none() {
                        halted = Some(format!("задача узла упала: {e}"));
                    }
                }
            }
        }

        if compiled.plan.policy.stop_on_gate_fail {
            if let Some(node_id) = first_failure(&states, level, &compiled) {
                let reason =
                    format!("узел {node_id}: гейты не пройдены — масштабирование остановлено");
                log.append(&FleetEvent::RunHalted {
                    run_id: run_id.clone(),
                    reason: reason.clone(),
                    at: now(),
                })?;
                halted = Some(reason);
                break;
            }
        }
    }

    watcher.abort();

    // Останов по гейту: узлы, до которых волны не дошли, помечаем пропущенными —
    // иначе журнал и счётчики расхоятся (учёт важнее умолчания).
    if halted.is_some() {
        for node in compiled.nodes() {
            if states.contains_key(&node.id) {
                continue;
            }
            states.insert(node.id.clone(), NodeState::Skipped);
            n_skipped += 1;
            log.append(&FleetEvent::NodeSkipped {
                run_id: run_id.clone(),
                node_id: node.id.clone(),
                reason: "прогон остановлен по гейту (stop_on_gate_fail)".to_string(),
                at: now(),
            })?;
        }
    }

    log.append(&FleetEvent::RunFinished {
        run_id: run_id.clone(),
        n_assigned: compiled.nodes().len(),
        n_done,
        n_failed,
        n_unassigned: 0,
        at: now(),
    })?;

    Ok(PlanRunOutcome {
        run_id,
        plan_id: compiled.plan.id.clone(),
        pattern: compiled.plan.pattern.as_str().to_string(),
        n_waves: compiled.levels.len(),
        n_nodes: compiled.nodes().len(),
        n_done,
        n_failed,
        n_skipped,
        halted,
        log_path: log.path().to_path_buf(),
        worktrees,
    })
}

/// Контекст прогона одного узла.
struct NodeRun<'a> {
    cfg: &'a Config,
    plan: &'a FleetPlan,
    compiled: &'a CompiledPlan,
    node: &'a PlanNode,
    agent: &'a str,
    repo: &'a Path,
    run_id: &'a str,
    fleet_dir: &'a Path,
    log: &'a FleetLog,
    control: Arc<crate::harness::HarnessControl>,
    policy: crate::policy::Policy,
    dep_summaries: Vec<String>,
    /// Worktree'ы зависимостей (их ветки вливаются в дерево узла).
    dep_worktrees: Vec<(String, PathBuf)>,
    harness: CodingHarnessConfig,
    ctrl_path: PathBuf,
}

impl NodeRun<'_> {
    /// Пишет событие узла в журнал.
    ///
    /// Сбой дописи намеренно не роняет узел: журнал — аудит прогона, а не
    /// канал управления; потеря одной строки не должна убивать работу агента
    /// (тот же приём, что в задачах [`crate::fleet_run::run_fleet`]).
    fn record(&self, event: &FleetEvent) {
        let _ = self.log.append(event);
    }
}

/// Прогоняет узел с ретраями; возвращает состояние, итог и worktree.
async fn run_node(ctx: &NodeRun<'_>) -> (NodeState, Option<NodeOutcome>, Option<PathBuf>) {
    let policy_cap = ctx.node.on_fail.max_attempts().unwrap_or(1).max(1);
    let budget_cap = if ctx.plan.policy.budget.max_attempts == 0 {
        usize::MAX
    } else {
        ctx.plan.policy.budget.max_attempts
    };
    let max_attempts = policy_cap.min(budget_cap).max(1);
    let mut attempt = 0usize;
    loop {
        attempt += 1;
        match run_node_once(ctx).await {
            Ok(attempt_result) if attempt_result.state == NodeState::Completed => {
                return (
                    NodeState::Completed,
                    attempt_result.outcome,
                    attempt_result.worktree,
                );
            }
            Ok(attempt_result) => {
                if attempt < max_attempts {
                    ctx.record(&FleetEvent::NodeRetry {
                        run_id: ctx.run_id.to_string(),
                        node_id: ctx.node.id.clone(),
                        attempt,
                        max_attempts,
                        at: now(),
                    });
                    continue;
                }
                return (
                    NodeState::Failed,
                    attempt_result.outcome,
                    attempt_result.worktree,
                );
            }
            Err(e) => {
                if attempt < max_attempts {
                    ctx.record(&FleetEvent::NodeRetry {
                        run_id: ctx.run_id.to_string(),
                        node_id: ctx.node.id.clone(),
                        attempt,
                        max_attempts,
                        at: now(),
                    });
                    continue;
                }
                ctx.record(&FleetEvent::AgentError {
                    run_id: ctx.run_id.to_string(),
                    agent_id: ctx.agent.to_string(),
                    item_id: ctx.node.id.clone(),
                    error: e.to_string(),
                    at: now(),
                });
                return (NodeState::Failed, None, None);
            }
        }
    }
}

/// Итог одной попытки узла.
struct NodeAttempt {
    state: NodeState,
    outcome: Option<NodeOutcome>,
    worktree: Option<PathBuf>,
}

/// Одна попытка: worktree → прогон харнесса → гейты.
async fn run_node_once(ctx: &NodeRun<'_>) -> Result<NodeAttempt> {
    let run_id = ctx.run_id.to_string();
    let node_id = ctx.node.id.clone();

    let mut worktree: Option<PathBuf> = None;
    let node_repo = if ctx.plan.policy.require_worktree {
        match crate::harness::enforce_node_worktree(ctx.cfg, ctx.repo, ctx.run_id, &node_id).await {
            Ok(Some((dir, _))) => {
                worktree = Some(dir.clone());
                dir
            }
            Ok(None) => ctx.repo.to_path_buf(),
            Err(e) => {
                ctx.record(&FleetEvent::AgentError {
                    run_id: run_id.clone(),
                    agent_id: ctx.agent.to_string(),
                    item_id: node_id.clone(),
                    error: format!("worktree узла: {e}"),
                    at: now(),
                });
                return Ok(NodeAttempt {
                    state: NodeState::Failed,
                    outcome: None,
                    worktree: None,
                });
            }
        }
    } else {
        ctx.repo.to_path_buf()
    };

    // Вливаем результаты зависимостей: узел видит не только их сводку, но и
    // код (иначе ревьювер ревьюит пустое дерево, а интегратор склеивает
    // нечего). Конфликт вливания — честный отказ узла, а не тихая потеря.
    for (dep_id, dep_wt) in &ctx.dep_worktrees {
        if worktree.is_none() {
            // Без изоляции узлы делят одно дерево — вливать нечего.
            break;
        }
        let branch = branch_of(dep_wt);
        match git_merge(&node_repo, &branch).await {
            Ok(()) => {
                ctx.record(&FleetEvent::NodeGated {
                    run_id: run_id.clone(),
                    node_id: node_id.clone(),
                    gate: format!("dependency:{dep_id}"),
                    verdict: "ok".to_string(),
                    detail: format!("ветка {branch} влита в дерево узла"),
                    at: now(),
                });
            }
            Err(e) => {
                ctx.record(&FleetEvent::AgentError {
                    run_id: run_id.clone(),
                    agent_id: ctx.agent.to_string(),
                    item_id: node_id.clone(),
                    error: format!(
                        "не удалось влить результат зависимости {dep_id} ({branch}): {e}"
                    ),
                    at: now(),
                });
                return Ok(NodeAttempt {
                    state: NodeState::Failed,
                    outcome: None,
                    worktree,
                });
            }
        }
    }

    if crate::fleet_run::control_kills(&ctx.ctrl_path, &node_id).unwrap_or(false) {
        ctx.record(&FleetEvent::AgentError {
            run_id: run_id.clone(),
            agent_id: ctx.agent.to_string(),
            item_id: node_id.clone(),
            error: "killed by control".to_string(),
            at: now(),
        });
        return Ok(NodeAttempt {
            state: NodeState::Failed,
            outcome: None,
            worktree,
        });
    }

    let spec = enrich_spec(ctx.node, &ctx.dep_summaries);
    let mut hcfg = ctx.harness.clone();
    hcfg.timeout_secs = node_timeout(ctx);

    ctx.record(&FleetEvent::AgentStarted {
        run_id: run_id.clone(),
        agent_id: ctx.agent.to_string(),
        item_id: node_id.clone(),
        kind: format!("harness:{}", ctx.node.role.as_str()),
        at: now(),
    });

    let on_activity = heartbeat_callback(ctx.log.clone(), &run_id, ctx.agent, &node_id);
    let run = match crate::harness::run_harness_streaming(
        ctx.agent,
        &hcfg,
        &node_repo,
        &spec,
        on_activity,
        Some(ctx.control.clone()),
    )
    .await
    {
        Ok(run) => run,
        Err(e) => {
            ctx.record(&FleetEvent::AgentError {
                run_id: run_id.clone(),
                agent_id: ctx.agent.to_string(),
                item_id: node_id.clone(),
                error: e.to_string(),
                at: now(),
            });
            return Ok(NodeAttempt {
                state: NodeState::Failed,
                outcome: None,
                worktree,
            });
        }
    };

    let node_log = ctx.fleet_dir.join(format!("{run_id}-{node_id}.log"));
    if let Ok(mut f) = std::fs::File::create(&node_log) {
        use std::io::Write as _;
        let _ = write!(f, "{}\n{}", run.stdout, run.stderr);
    }
    let contract_status = contract_status(&run.contract);
    ctx.record(&FleetEvent::AgentDone {
        run_id: run_id.clone(),
        agent_id: ctx.agent.to_string(),
        item_id: node_id.clone(),
        exit_code: run.exit_code,
        duration_secs: run.duration_secs,
        contract_status: contract_status.clone(),
        at: now(),
    });

    let gate_ctx = GateContext {
        repo: &node_repo,
        stdout: &run.stdout,
        contract: &run.contract,
        policy: ctx.policy,
    };
    let reports = run_gates(ctx.compiled.gates_of(&node_id), &gate_ctx).await?;
    let mut failure: Option<String> = None;
    for report in &reports {
        ctx.record(&FleetEvent::NodeGated {
            run_id: run_id.clone(),
            node_id: node_id.clone(),
            gate: report.gate.clone(),
            verdict: report.result.clone(),
            detail: report.detail.clone(),
            at: now(),
        });
        if report.failed() && failure.is_none() {
            failure = Some(format!("гейт {}: {}", report.gate, report.detail));
        }
    }

    let outcome = NodeOutcome {
        node_id: node_id.clone(),
        agent_id: ctx.agent.to_string(),
        contract_status,
        changed_files: changed_files(&node_repo).await,
        summary: summary_of(&run.stdout, &run.contract),
    };

    if failure.is_some() {
        return Ok(NodeAttempt {
            state: NodeState::Failed,
            outcome: Some(outcome),
            worktree,
        });
    }
    ctx.record(&FleetEvent::NodeCompleted {
        run_id,
        node_id,
        at: now(),
    });
    Ok(NodeAttempt {
        state: NodeState::Completed,
        outcome: Some(outcome),
        worktree,
    })
}

/// Колбэк heartbeat: не чаще раза в 5 с, чтобы сотни узлов не залили журнал.
fn heartbeat_callback(
    log: FleetLog,
    run_id: &str,
    agent: &str,
    node_id: &str,
) -> Arc<dyn Fn() + Send + Sync> {
    let last = Arc::new(std::sync::Mutex::new(std::time::Instant::now()));
    let (run_id, agent, node_id) = (run_id.to_string(), agent.to_string(), node_id.to_string());
    Arc::new(move || {
        let mut last = last
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if last.elapsed() >= std::time::Duration::from_secs(5) {
            *last = std::time::Instant::now();
            log.append(&FleetEvent::AgentHeartbeat {
                run_id: run_id.clone(),
                agent_id: agent.clone(),
                item_id: node_id.clone(),
                at: now(),
            })
            .ok();
        }
    })
}

/// Имя ветки worktree узла (фабрика создаёт `arch/<имя-каталога>`).
fn branch_of(worktree: &Path) -> String {
    let name = worktree
        .file_name()
        .map_or_else(|| "node".to_string(), |n| n.to_string_lossy().into_owned());
    format!("arch/{name}")
}

/// Вливает ветку зависимости в дерево узла (fast-forward по построению:
/// обе ветки растут от одного baseline, узел ещё ничего не менял).
async fn git_merge(repo: &Path, branch: &str) -> Result<()> {
    let out = tokio::process::Command::new("git")
        .args(["merge", "--no-edit", branch])
        .current_dir(repo)
        .output()
        .await
        .map_err(|e| HarnessError::Fleet(format!("git merge {branch}: {e}")))?;
    if out.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&out.stderr);
    Err(HarnessError::Fleet(
        stderr.trim().chars().take(300).collect::<String>(),
    ))
}

/// Таймаут узла: план → MANIFEST → дефолт маршрута; сверху — бюджет прогона.
fn node_timeout(ctx: &NodeRun<'_>) -> u64 {
    let manifest = if ctx.plan.defaults.timeout_from_manifest {
        crate::harness::recommended_timeout_secs(ctx.repo)
    } else {
        None
    };
    let base = ctx
        .node
        .timeout_secs
        .or(manifest)
        .unwrap_or_else(|| crate::harness::recommended_timeout_for_route(ctx.node.route));
    let cap = ctx.plan.policy.budget.max_node_secs;
    if cap == 0 { base } else { base.min(cap) }
}

/// Дополняет задание узла результатами зависимостей (fan-in и вход ревьювера).
fn enrich_spec(node: &PlanNode, dep_summaries: &[String]) -> String {
    if dep_summaries.is_empty() {
        return node.spec.clone();
    }
    let mut s = node.spec.clone();
    s.push_str("\n\nРезультаты зависимостей (для сведения):\n");
    for d in dep_summaries {
        s.push_str("- ");
        s.push_str(d);
        s.push('\n');
    }
    s
}

/// Компактные сводки зависимостей узла.
fn dep_summaries_for(
    deps: &[String],
    compiled: &CompiledPlan,
    outcomes: &HashMap<String, NodeOutcome>,
) -> Vec<String> {
    let mut out = Vec::new();
    for dep in deps {
        if let Some(o) = outcomes.get(dep) {
            let files = if o.changed_files.is_empty() {
                "файлов нет".to_string()
            } else {
                o.changed_files.join(", ")
            };
            out.push(format!(
                "{dep}: контракт {}, файлы: {files}",
                o.contract_status
            ));
        } else if let Some(node) = compiled.node(dep) {
            out.push(format!("{dep}: узел {}", node.role.as_str()));
        }
    }
    out
}

/// Победитель группы турнира: первый по id узел, прошедший все гейты.
fn judge_decision(deps: &[String], states: &HashMap<String, NodeState>) -> Option<String> {
    let mut sorted: Vec<&String> = deps.iter().collect();
    sorted.sort();
    sorted
        .into_iter()
        .find(|d| states.get(*d) == Some(&NodeState::Completed))
        .cloned()
}

/// Первый провалившийся узел волны (для `stop_on_gate_fail`).
fn first_failure(
    states: &HashMap<String, NodeState>,
    level: &[usize],
    compiled: &CompiledPlan,
) -> Option<String> {
    for idx in level {
        let Some(node) = compiled.nodes().get(*idx) else {
            continue;
        };
        if states.get(&node.id) == Some(&NodeState::Failed) {
            return Some(node.id.clone());
        }
    }
    None
}

/// Причина блокировки узла зависимостью.
fn blocked_by(
    deps: &[String],
    compiled: &CompiledPlan,
    states: &HashMap<String, NodeState>,
) -> Option<String> {
    for dep in deps {
        match states.get(dep) {
            Some(NodeState::Failed) => {
                let policy = compiled.node(dep).map_or(FailPolicy::Block, |n| n.on_fail);
                if policy == FailPolicy::Block {
                    return Some(format!("зависимость {dep} не прошла гейты (block)"));
                }
            }
            Some(NodeState::Skipped) => {
                return Some(format!("зависимость {dep} пропущена"));
            }
            _ => {}
        }
    }
    None
}

/// Назначает узлам агентов детерминированным роутером.
fn assign_agents(
    cfg: &Config,
    nodes: &[PlanNode],
    agents: &[AgentProfile],
) -> HashMap<String, String> {
    let items: Vec<WorkItem> = nodes
        .iter()
        .map(|n| WorkItem {
            id: n.id.clone(),
            spec: n.spec.clone(),
            required_skills: n.required_skills.clone(),
            tags: n.tags.clone(),
            route: n.route,
            domain: n.domain.clone(),
            effort: n.effort.max(1),
        })
        .collect();
    let plan = crate::router::Router::new().assign(&items, agents);
    let mut map: HashMap<String, String> = plan
        .assignments
        .into_iter()
        .map(|a| (a.item_id, a.agent_id))
        .collect();
    let fallback = cfg
        .harnesses
        .keys()
        .next()
        .cloned()
        .unwrap_or_else(|| "claude-code".to_string());
    for node in nodes {
        if let Some(explicit) = &node.agent {
            if cfg.harnesses.contains_key(explicit) {
                map.insert(node.id.clone(), explicit.clone());
            }
        }
        map.entry(node.id.clone())
            .or_insert_with(|| fallback.clone());
    }
    map
}

/// Статус контракта результата строкой.
fn contract_status(contract: &crate::harness::ContractParse) -> String {
    match contract {
        crate::harness::ContractParse::Valid(c) => c.status.as_str().to_string(),
        crate::harness::ContractParse::Invalid(_) => "invalid".to_string(),
        crate::harness::ContractParse::Missing => "missing".to_string(),
    }
}

/// Изменённые файлы узла (`git status --porcelain` в каталоге узла).
async fn changed_files(repo: &Path) -> Vec<String> {
    let out = tokio::process::Command::new("git")
        .args(["status", "--porcelain"])
        .current_dir(repo)
        .output()
        .await;
    let Ok(out) = out else {
        return Vec::new();
    };
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|l| l.get(3..).map(str::trim))
        .filter(|s| !s.is_empty())
        .take(50)
        .map(str::to_owned)
        .collect()
}

/// Короткая сводка результата узла (допущения контракта или хвост вывода).
fn summary_of(stdout: &str, contract: &crate::harness::ContractParse) -> String {
    if let crate::harness::ContractParse::Valid(c) = contract {
        if !c.assumptions.is_empty() {
            return c.assumptions.join("; ");
        }
    }
    let text = stdout.trim();
    let chars: Vec<char> = text.chars().collect();
    let take = chars.len().min(300);
    chars[chars.len() - take..].iter().collect()
}

/// Снимок плана рядом с журналом (resume самодостаточен).
fn snapshot_plan(plan: &FleetPlan, fleet_dir: &Path, run_id: &str) -> Result<String> {
    let text = toml::to_string(plan)
        .map_err(|e| HarnessError::Fleet(format!("сериализация плана в снимок: {e}")))?;
    let path = fleet_dir.join(format!("{run_id}.plan.toml"));
    std::fs::write(&path, &text).map_err(|e| HarnessError::io(&path, e))?;
    Ok(text)
}

/// Следит за control-каналом и применяет Kill/Pause/Resume к узлам.
fn spawn_control_watcher(
    controls: Arc<std::sync::Mutex<HashMap<String, Arc<crate::harness::HarnessControl>>>>,
    path: PathBuf,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            if let Ok(recs) = ControlChannel::read(&path) {
                let map = controls
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                for rec in recs {
                    let all = rec.agent_id == "*";
                    let affected = map
                        .iter()
                        .filter(|(id, _)| all || **id == rec.agent_id)
                        .map(|(_, c)| c.clone())
                        .collect::<Vec<_>>();
                    for c in affected {
                        match rec.cmd {
                            ControlCommand::Kill => {
                                c.cancel.store(true, std::sync::atomic::Ordering::Relaxed);
                            }
                            ControlCommand::Pause => {
                                c.paused.store(true, std::sync::atomic::Ordering::Relaxed);
                            }
                            ControlCommand::Resume => {
                                c.paused.store(false, std::sync::atomic::Ordering::Relaxed);
                            }
                            // Маршрут узла задан графом: перекидка и приоритет
                            // в плановом режиме смысла не имеют.
                            ControlCommand::Reroute | ControlCommand::Priority => {}
                        }
                    }
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }
    })
}

/// Штамп времени события.
fn now() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// Завершённые узлы прогона: `узел → последний вердикт гейта`.
///
/// # Errors
/// Журнал не читается.
pub fn completed_nodes(log_path: &Path) -> Result<std::collections::BTreeMap<String, String>> {
    let read = FleetLog::read_tolerant(log_path)?;
    let mut out = std::collections::BTreeMap::new();
    for e in read.events {
        if let FleetEvent::NodeGated {
            node_id,
            gate,
            verdict,
            ..
        } = e
        {
            out.insert(node_id, format!("{gate}: {verdict}"));
        }
    }
    Ok(out)
}

/// Инструмент агента: план флота (предложить / проверить / показать /
/// прогнать / продолжить).
pub struct FleetPlanTool;

#[async_trait::async_trait]
impl crate::tool::Tool for FleetPlanTool {
    fn spec(&self) -> crate::llm::ToolSpec {
        crate::llm::ToolSpec {
            name: "fleet_plan".into(),
            description: "План флота кодовых агентов (ADR-042): паттерн оркестрации \
                (fanout|pipeline|map_reduce|tournament|review_pair|ralph|\
                walking_skeleton|dag) разворачивается в граф узлов и исполняется волнами \
                с механическими гейтами (контракт, fitness, линт спайна, delta guard) \
                и owner-гейтом на мерж. Операции: propose (черновик плана по \
                .arch-handoff/MANIFEST.json), validate, show, run, resume. Мерж в main \
                инструмент НЕ делает — только `arch-ml fleet merge --owner-approve`."
                .into(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["propose", "validate", "show", "run", "resume"],
                        "description": "Операция над планом"
                    },
                    "plan_path": {"type": "string", "description": "Путь к файлу плана (.toml|.json|.yaml)"},
                    "repo": {"type": "string", "description": "Корень репозитория (по умолчанию cwd)"},
                    "pattern": {"type": "string", "description": "Для propose: форсировать паттерн"},
                    "run_id": {"type": "string", "description": "Для resume: run-id прогона плана (fpl-…)"},
                    "force_rerun": {"type": "boolean", "description": "Для resume: разрешить повтор без worktree-изоляции"},
                    "mermaid": {"type": "boolean", "description": "Для show: диаграмма Mermaid"}
                },
                "required": ["op"]
            }),
        }
    }

    async fn call(
        &self,
        args: serde_json::Value,
        ctx: &crate::tool::ToolContext,
    ) -> Result<crate::tool::ToolOutput> {
        use crate::tool::ToolOutput;
        let op = args
            .get("op")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("");
        let cfg = ctx.config.clone();
        let state_dir = cfg.paths.state_dir.clone();
        let repo = args
            .get("repo")
            .and_then(serde_json::Value::as_str)
            .map_or_else(|| ctx.cwd.clone(), |r| ctx.resolve(r));
        let agents = crate::fleet_run::harness_profiles(&cfg);
        match op {
            "propose" => {
                let forced = match args.get("pattern").and_then(serde_json::Value::as_str) {
                    Some(p) => Some(crate::fleet_plan::PatternKind::parse(p)?),
                    None => None,
                };
                let (plan, rationale) = crate::fleet_plan::propose(&repo, forced)?;
                let target = repo.join(&cfg.fleet.plan_dir);
                let path = crate::fleet_plan::write_plan(&plan, &target)?;
                Ok(ToolOutput::ok(format!(
                    "Черновик плана записан: {}\n{rationale}",
                    path.display()
                )))
            }
            "validate" | "show" => {
                let Some(path) = args.get("plan_path").and_then(serde_json::Value::as_str) else {
                    return Ok(ToolOutput::err(
                        "fleet_plan: для validate/show нужен plan_path",
                    ));
                };
                let path = ctx.resolve(path);
                let parsed = crate::fleet_plan::parse_plan(&path)?;
                if op == "validate" {
                    let issues = crate::fleet_plan::validate_plan(&parsed);
                    if issues.is_empty() {
                        return Ok(ToolOutput::ok(format!("План {} валиден", parsed.id)));
                    }
                    let text = issues
                        .iter()
                        .map(|i| format!("{:?}: {}", i.severity, i.message))
                        .collect::<Vec<_>>()
                        .join("\n");
                    let has_error = issues
                        .iter()
                        .any(|i| i.severity == crate::fleet_plan::PlanSeverity::Error);
                    return Ok(if has_error {
                        ToolOutput::err(format!("План невалиден:\n{text}"))
                    } else {
                        ToolOutput::ok(format!("План валиден с замечаниями:\n{text}"))
                    });
                }
                let compiled = crate::fleet_plan::compile(parsed)?;
                let mermaid = args
                    .get("mermaid")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false);
                Ok(ToolOutput::ok(if mermaid {
                    crate::fleet_plan::render_mermaid(&compiled)
                } else {
                    crate::fleet_plan::render_plan(&compiled)
                }))
            }
            "run" => {
                let Some(path) = args.get("plan_path").and_then(serde_json::Value::as_str) else {
                    return Ok(ToolOutput::err("fleet_plan: для run нужен plan_path"));
                };
                let plan = crate::fleet_plan::parse_plan(&ctx.resolve(path))?;
                let outcome =
                    run_plan(&cfg, &state_dir, &repo, plan, &agents, ResumeMode::Fresh).await?;
                Ok(ToolOutput::ok(render_plan_outcome(&outcome)))
            }
            "resume" => {
                let Some(run_id) = args.get("run_id").and_then(serde_json::Value::as_str) else {
                    return Ok(ToolOutput::err("fleet_plan: для resume нужен run_id"));
                };
                let force = args
                    .get("force_rerun")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false);
                let outcome = resume_plan(&cfg, &state_dir, &repo, run_id, force, &agents).await?;
                Ok(ToolOutput::ok(render_plan_outcome(&outcome)))
            }
            other => Ok(ToolOutput::err(format!(
                "fleet_plan: неизвестная операция '{other}' (propose|validate|show|run|resume)"
            ))),
        }
    }

    fn timeout_secs(&self) -> u64 {
        7200 + 120
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fleet_plan::{PlanFormat, parse_plan_str};

    fn fake_cfg() -> Config {
        let mut cfg = Config::default();
        let h = CodingHarnessConfig {
            binary: "true".to_string(),
            timeout_secs: 60,
            ..CodingHarnessConfig::default()
        };
        cfg.harnesses.insert("fake".to_string(), h);
        cfg
    }

    #[test]
    fn explicit_node_agent_wins_over_router() {
        let cfg = fake_cfg();
        let plan = parse_plan_str(
            "id = \"x\"\npattern = \"fanout\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\nagent = \"fake\"\n",
            PlanFormat::Toml,
        )
        .expect("plan");
        let agents = crate::fleet_run::harness_profiles(&cfg);
        let map = assign_agents(&cfg, &plan.nodes, &agents);
        assert_eq!(map.get("a").map(String::as_str), Some("fake"));
    }

    #[test]
    fn blocked_by_reports_block_policy() {
        let plan = parse_plan_str(
            "id = \"x\"\npattern = \"pipeline\"\norder = [\"a\", \"b\"]\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\non_fail = \"block\"\n\n[[nodes]]\nid = \"b\"\nspec = \"s\"\n",
            PlanFormat::Toml,
        )
        .expect("plan");
        let compiled = compile(plan).expect("compile");
        let mut states = HashMap::new();
        states.insert("a".to_string(), NodeState::Failed);
        let deps = compiled.incoming.get("b").cloned().unwrap_or_default();
        let reason = blocked_by(&deps, &compiled, &states).expect("blocked");
        assert!(reason.contains("block"), "{reason}");
    }

    #[test]
    fn judge_picks_first_completed_by_id() {
        let mut states = HashMap::new();
        states.insert("t1".to_string(), NodeState::Completed);
        states.insert("t2".to_string(), NodeState::Completed);
        let deps = vec!["t2".to_string(), "t1".to_string()];
        assert_eq!(judge_decision(&deps, &states).as_deref(), Some("t1"));
    }

    #[test]
    fn judge_returns_none_without_winners() {
        let mut states = HashMap::new();
        states.insert("t1".to_string(), NodeState::Failed);
        let deps = vec!["t1".to_string()];
        assert!(judge_decision(&deps, &states).is_none());
    }

    #[test]
    fn node_timeout_prefers_plan_then_budget() {
        let plan = parse_plan_str(
            "id = \"x\"\npattern = \"fanout\"\n[policy.budget]\nmax_node_secs = 120\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\ntimeout_secs = 900\n",
            PlanFormat::Toml,
        )
        .expect("plan");
        let compiled = compile(plan).expect("compile");
        let cfg = fake_cfg();
        let log = FleetLog::new(PathBuf::from("/tmp/x.jsonl"));
        let ctx = NodeRun {
            cfg: &cfg,
            plan: &compiled.plan,
            compiled: &compiled,
            node: compiled.node("a").expect("a"),
            agent: "fake",
            repo: Path::new("/tmp"),
            run_id: "fpl-1",
            fleet_dir: Path::new("/tmp"),
            log: &log,
            control: Arc::new(crate::harness::HarnessControl::default()),
            policy: crate::policy::Policy::default(),
            dep_summaries: Vec::new(),
            dep_worktrees: Vec::new(),
            harness: CodingHarnessConfig::default(),
            ctrl_path: PathBuf::from("/tmp/ctl.jsonl"),
        };
        assert_eq!(node_timeout(&ctx), 120);
    }

    #[test]
    fn outcome_render_mentions_halt_and_worktrees() {
        let outcome = PlanRunOutcome {
            run_id: "fpl-1".into(),
            plan_id: "p".into(),
            pattern: "fanout".into(),
            n_waves: 1,
            n_nodes: 2,
            n_done: 1,
            n_failed: 1,
            n_skipped: 0,
            halted: Some("узел a: гейты не пройдены".into()),
            log_path: PathBuf::from("/tmp/x.jsonl"),
            worktrees: vec![("a".into(), PathBuf::from("/tmp/wt"))],
        };
        let text = render_plan_outcome(&outcome);
        assert!(text.contains("Остановлен по гейту"), "{text}");
        assert!(text.contains("/tmp/wt"), "{text}");
        assert!(text.contains("--owner-approve"), "{text}");
    }
}
