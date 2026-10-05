//! Флот харнессов и worktree-фабрика: SSOT-аудит флота (дубли/дрейф копий
//! спайна, модель 5.2), планы и прогоны флота (паттерны ADR-042, план из ADR
//! ADR-045, resume из журнала, control-канал), гейт мерджа владельцем
//! (ADR-046), изоляция агентной работы в git worktree (new/list/diff/accept/
//! drop, ADR-024/ADR-048).

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Subcommand;

use arch_harness::config::Config;
use arch_harness::fleet_run::ControlCommand;

/// Подкоманды `arch-ml fleet`.
#[derive(Subcommand)]
pub(crate) enum FleetCmd {
    /// SSOT-аудит флота: точные дубли документации и дрейф копий спайна.
    /// Дрейф хотя бы одного файла (разное содержимое у владельцев одного
    /// пути; канон — majority-версия) — exit code 1 (гейт для CI).
    Audit {
        /// Каталоги-worktree (каждый с копией архитектурных файлов).
        paths: Vec<PathBuf>,
        /// Репозиторий: worktree перечисляются из `git worktree list`
        /// (добавляются к позиционным путям).
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Сузить сканирование glob'ом (повторяемый), напр. --include 'model/**'.
        /// По умолчанию — **/*.md|yaml|yml|json без .git/target/node_modules/.arch-handoff.
        #[arg(long)]
        include: Vec<String>,
        /// Формат вывода: text (таблица + топ расхождений) или json.
        #[arg(long, default_value = "text")]
        format: String,
        /// Воспроизводимый text-вывод для золотых файлов/CI: без волатильных
        /// колонок (размер на диске, возраст последнего коммита).
        #[arg(long)]
        stable: bool,
        /// Exit 1, если доля точных дублей выше порога (проценты, напр. 50).
        #[arg(long)]
        fail_on_dupes: Option<f64>,
    },
    /// Гейт мерджа результата прогона флота (worktree `arch/<run-id>`) в
    /// основную ветку — «агент не имеет пути в main», интеграцию подтверждает
    /// владелец. Без --owner-approve и без --approver печатает сводку прогона
    /// (diff stat, коммиты ветки, статус контракта из лога-evidence) и
    /// ОТКАЗЫВАЕТ мержить (exit 1). Режим гейта — [fleet] `merge_gate`
    /// ("owner" по умолчанию, "none" — без гейта).
    Merge {
        /// Run-id прогона (имя worktree без префикса arch/, напр.
        /// claude-code-20260825103000 — его сообщает `harness_run` при
        /// [fleet] `require_worktree` = true).
        run_id: String,
        /// Явное подтверждение владельца: выполнить merge в основную ветку.
        /// Это НЕ именной обход: без --approver вердикт ревьювера `NOT-READY`
        /// блокирует и этот путь (ADR-046, п. 2).
        #[arg(long)]
        owner_approve: bool,
        /// Именной аппрувер: обход блокирующего вердикта ревьювера (NOT-READY)
        /// по ветке прогона. Решение пишется в журнал приёмки
        /// (`state_dir/accept/decisions.jsonl`) — той же записью, что у
        /// `worktree accept --approver`; подтверждает интеграцию вместо
        /// --owner-approve.
        #[arg(long)]
        approver: Option<String>,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Прогнать флот харнессов: план (паттерн, ADR-042) ИЛИ legacy
    /// items-файл (одна волна). Items — JSON-файл: массив
    /// {id, spec, `required_skills`[], tags[], route, domain, effort}.
    Run {
        /// Корень репозитория (с handoff-пакетом).
        #[arg(long)]
        repo: PathBuf,
        /// JSON-файл с work-items (массив объектов) — legacy-веер.
        #[arg(long, conflicts_with = "plan")]
        items_file: Option<PathBuf>,
        /// Файл плана флота (.arch-fleet/<id>.plan.toml|json|yaml).
        #[arg(long)]
        plan: Option<PathBuf>,
        /// Метка пакета/эпика (для журнала).
        #[arg(long, default_value = "fleet")]
        package: String,
        /// Baseline-судья гейтов узлов (ADR-046): путь к бинарю, собранному
        /// ДО прогона. Приоритет над `[fleet] judge_binary`. Без флага и
        /// конфига берётся бинарь, запустивший флот; команда гейта, зовущая
        /// судью внутри ворктри узла, без baseline падает (самооценка).
        #[arg(long)]
        judge: Option<PathBuf>,
    },
    /// Управление планами флота: предложить, проверить, показать.
    Plan {
        #[command(subcommand)]
        cmd: FleetPlanCmd,
    },
    /// Продолжить прогон плана из журнала событий (ADR-042): завершённые
    /// узлы пропускаются, незавершённые догоняются.
    Resume {
        /// Run-id прогона плана (fpl-…).
        run_id: String,
        /// Корень репозитория.
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Разрешить повтор узлов без worktree-изоляции (неидемпотентно).
        #[arg(long)]
        force_rerun: bool,
    },
    /// Показать dashboard прогона флота: карта назначений + статусы агентов
    /// из журнала state/fleet/<run-id>.jsonl.
    Status {
        /// Run-id прогона (или "latest" — последний журнал в state/fleet/).
        run_id: String,
        /// Машиночитаемая сводка (JSON) вместо текстового dashboard.
        #[arg(long)]
        json: bool,
    },
    /// Отправить команду управления в control-канал прогона.
    Control {
        /// Run-id прогона.
        run_id: String,
        /// Агент (или "*" — все).
        #[arg(long)]
        agent: String,
        /// Убить агента (Kill).
        #[arg(long)]
        kill: bool,
        /// Поставить на паузу (SIGSTOP).
        #[arg(long)]
        pause: bool,
        /// Снять паузу (SIGCONT).
        #[arg(long)]
        resume: bool,
        /// Перекинуть item другому агенту (payload — новый агент).
        #[arg(long)]
        reroute: Option<String>,
        /// Поднять приоритет (payload — число; no-op в конкурентной модели).
        #[arg(long)]
        priority: Option<usize>,
    },
}

/// Подкоманды `arch-ml fleet plan`.
#[derive(Subcommand)]
pub(crate) enum FleetPlanCmd {
    /// Предложить черновик плана по handoff-пакету репозитория (паттерн по
    /// маршруту значимости). Пишет `<repo>/.arch-fleet/<id>.plan.toml`;
    /// автозапуска нет — план утверждает владелец.
    Propose {
        /// Корень репозитория с `.arch-handoff/MANIFEST.json`.
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Форсировать паттерн (иначе — по маршруту пакета).
        #[arg(long)]
        pattern: Option<String>,
        /// Каталог планов (дефолт `[fleet] plan_dir` = .arch-fleet).
        #[arg(long)]
        out: Option<PathBuf>,
        /// Собрать план из набора решений (ADR-045) вместо handoff-пакета:
        /// один узел на ADR, рёбра — из `depends_on`.
        #[arg(long)]
        from_adrs: bool,
        /// Каталог ADR для `--from-adrs` (дефолт `docs/adr` от `--repo`).
        #[arg(long)]
        adr_dir: Option<PathBuf>,
        /// Отбор ADR по статусу: proposed|accepted|all (дефолт proposed).
        #[arg(long)]
        status: Option<String>,
    },
    /// Механическая проверка плана (без LLM, без прогона): exit 1 при ошибке.
    Validate {
        /// Файл плана (.toml|.json|.yaml).
        plan: PathBuf,
        /// Машиночитаемый вывод (JSON).
        #[arg(long)]
        json: bool,
    },
    /// Показать скомпилированный план: волны, роли, гейты, зависимости.
    Show {
        /// Файл плана (.toml|.json|.yaml).
        plan: PathBuf,
        /// Диаграмма Mermaid вместо таблицы волн.
        #[arg(long)]
        mermaid: bool,
        /// Машиночитаемый вывод (JSON).
        #[arg(long)]
        json: bool,
    },
}

/// Подкоманды `arch-ml worktree`.
#[derive(Subcommand)]
pub(crate) enum WorktreeCmd {
    /// Создать изолированный worktree (ветка arch/<name>).
    New {
        /// Имя (kebab-case [a-z0-9-]).
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Базовая ветка/коммит (по умолчанию HEAD).
        #[arg(long)]
        base: Option<String>,
    },
    /// Список worktree фабрики.
    List {
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Diff ветки worktree против HEAD (review).
    Diff {
        /// Имя worktree.
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Принять: merge в текущую ветку + post-merge гейт правил + уборка
    /// worktree. Коды возврата: 0 — приёмка чистая; 4 — мерж состоялся, но
    /// правила основной ветки красные (CONSTRAINTS-FAILED, решение
    /// владельца); 1 — приёмка отклонена, основная ветка не тронута.
    Accept {
        /// Имя worktree.
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Явный именной аппрувер: обход блокирующего вердикта ревьювера
        /// (NOT-READY) по этой ветке. Решение пишется в журнал приёмки
        /// (`state_dir/accept/decisions.jsonl`).
        #[arg(long)]
        approver: Option<String>,
    },
    /// Удалить worktree без merge (только чистое).
    Drop {
        /// Имя worktree.
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
}

/// `arch-ml fleet`: аудит флота worktree (дубли/дрейф) и гейт мерджа прогонов.
pub(crate) async fn cmd_fleet(cfg: &Arc<Config>, cmd: FleetCmd) -> Result<()> {
    match cmd {
        FleetCmd::Audit {
            paths,
            repo,
            include,
            format,
            stable,
            fail_on_dupes,
        } => {
            let mut roots = paths;
            if let Some(repo) = repo {
                roots.extend(arch_harness::fleet::worktrees_from_git(&repo)?);
            }
            let report = arch_harness::fleet::audit(&roots, &include)?;
            match format.as_str() {
                "json" => outln!("{}", serde_json::to_string_pretty(&report)?),
                "text" => outp!("{}", arch_harness::fleet::render_text_opts(&report, stable)),
                other => anyhow::bail!("неизвестный формат '{other}' (допустимы: text, json)"),
            }
            // Независимые триггеры гейта: дрейф копий и порог доли дублей.
            let dupes_failed = fail_on_dupes.is_some_and(|pct| report.dup_pct > pct);
            if let Some(pct) = fail_on_dupes {
                if dupes_failed {
                    outln!(
                        "Порог дублей превышен: {:.1}% > {pct:.1}% — exit 1",
                        report.dup_pct
                    );
                }
            }
            if report.has_drift || dupes_failed {
                std::process::exit(1);
            }
        }
        FleetCmd::Merge {
            run_id,
            owner_approve,
            approver,
            repo,
        } => {
            let repo = match repo {
                Some(r) => r,
                None => std::env::current_dir().context("cwd")?,
            };
            match arch_harness::worktree::gated_merge(
                cfg,
                &repo,
                &run_id,
                owner_approve,
                approver.as_deref(),
            )
            .await?
            {
                arch_harness::worktree::MergeGateOutcome::Refused(summary) => {
                    outln!("{summary}");
                    std::process::exit(1);
                }
                arch_harness::worktree::MergeGateOutcome::Merged(outcome) => {
                    outln!("{}", outcome.text);
                    // Как у `worktree accept`: красный post-merge гейт —
                    // код 4, а не 0 (мерж состоялся, ADR-024, шаг 5).
                    if outcome.exit_code() != 0 {
                        std::process::exit(outcome.exit_code());
                    }
                }
            }
        }
        FleetCmd::Run {
            repo,
            items_file,
            plan,
            package,
            judge,
        } => {
            let state_dir = cfg.paths.state_dir.clone();
            let agents = arch_harness::fleet_run::harness_profiles(cfg);
            match (plan, items_file) {
                (Some(plan_path), _) => {
                    let plan = arch_harness::fleet_plan::parse_plan(&plan_path)?;
                    let outcome = arch_harness::fleet_exec::run_plan(
                        cfg,
                        &state_dir,
                        &repo,
                        plan,
                        &agents,
                        arch_harness::fleet_exec::ResumeMode::Fresh,
                        judge.as_deref(),
                    )
                    .await?;
                    outp!(
                        "{}",
                        arch_harness::fleet_exec::render_plan_outcome(&outcome)
                    );
                    if outcome.halted.is_some() || outcome.n_failed > 0 {
                        std::process::exit(1);
                    }
                }
                (None, Some(items_file)) => {
                    let text = std::fs::read_to_string(&items_file)
                        .with_context(|| format!("не читается {}", items_file.display()))?;
                    let value: serde_json::Value = serde_json::from_str(&text)?;
                    let items = arch_harness::fleet_run::parse_items(&value)?;
                    let outcome = arch_harness::fleet_run::run_fleet(
                        cfg, &state_dir, &repo, &package, &items, &agents, None,
                    )
                    .await?;
                    outp!("{}", arch_harness::fleet_run::render_outcome(&outcome));
                }
                (None, None) => anyhow::bail!(
                    "fleet run: укажите --plan <файл плана> (паттерны) или \
                     --items-file <json> (legacy-веер)"
                ),
            }
        }
        FleetCmd::Plan { cmd } => cmd_fleet_plan(cfg, cmd)?,
        FleetCmd::Resume {
            run_id,
            repo,
            force_rerun,
        } => {
            let repo = match repo {
                Some(r) => r,
                None => std::env::current_dir().context("cwd")?,
            };
            let agents = arch_harness::fleet_run::harness_profiles(cfg);
            let state_dir = cfg.paths.state_dir.clone();
            let outcome = arch_harness::fleet_exec::resume_plan(
                cfg,
                &state_dir,
                &repo,
                &run_id,
                force_rerun,
                &agents,
                None,
            )
            .await?;
            outp!(
                "{}",
                arch_harness::fleet_exec::render_plan_outcome(&outcome)
            );
            if outcome.halted.is_some() || outcome.n_failed > 0 {
                std::process::exit(1);
            }
        }
        FleetCmd::Status { run_id, json } => {
            let fleet_dir = cfg.paths.state_dir.join("fleet");
            let path = if run_id == "latest" {
                arch_harness::fleet_run::latest_log(&fleet_dir)?
            } else {
                fleet_dir.join(format!("{run_id}.jsonl"))
            };
            if json {
                let read = arch_harness::fleet_run::FleetLog::read_tolerant(&path)?;
                let nodes = arch_harness::fleet_exec::completed_nodes(&path)?;
                let summary = serde_json::json!({
                    "run_id": run_id,
                    "log_path": path,
                    "events": read.events.len(),
                    "skipped_lines": read.skipped,
                    "node_gates": nodes,
                });
                outln!("{}", serde_json::to_string_pretty(&summary)?);
            } else {
                outp!("{}", arch_harness::fleet_run::render_log(&path)?);
            }
        }
        FleetCmd::Control {
            run_id,
            agent,
            kill,
            pause,
            resume,
            reroute,
            priority,
        } => {
            let (cmd, payload, label) = if kill {
                (ControlCommand::Kill, None, "Kill")
            } else if pause {
                (ControlCommand::Pause, None, "Pause")
            } else if resume {
                (ControlCommand::Resume, None, "Resume")
            } else if let Some(target) = reroute {
                (ControlCommand::Reroute, Some(target), "Reroute")
            } else if let Some(n) = priority {
                (ControlCommand::Priority, Some(n.to_string()), "Priority")
            } else {
                anyhow::bail!(
                    "fleet control: укажите действие (--kill | --pause | --resume | \
                     --reroute <target> | --priority <n>)"
                );
            };
            let fleet_dir = cfg.paths.state_dir.join("fleet");
            let ch = arch_harness::fleet_run::ControlChannel::new(
                arch_harness::fleet_run::control_path(&fleet_dir, &run_id),
            );
            ch.send(&agent, cmd, payload.as_deref())?;
            outln!("{label} отправлен агенту '{agent}' в прогоне {run_id}");
        }
    }
    Ok(())
}

/// `arch-ml fleet plan`: предложение, проверка и просмотр планов флота (ADR-042).
fn cmd_fleet_plan(cfg: &Arc<Config>, cmd: FleetPlanCmd) -> Result<()> {
    use arch_harness::fleet_plan;
    match cmd {
        FleetPlanCmd::Propose {
            repo,
            pattern,
            out,
            from_adrs,
            adr_dir,
            status,
        } => {
            let repo = match repo {
                Some(r) => r,
                None => std::env::current_dir().context("cwd")?,
            };
            let forced = match pattern {
                Some(p) => Some(fleet_plan::PatternKind::parse(&p)?),
                None => None,
            };
            let target = out.unwrap_or_else(|| repo.join(&cfg.fleet.plan_dir));
            if from_adrs {
                let dir = match adr_dir {
                    Some(d) if d.is_absolute() => d,
                    Some(d) => repo.join(d),
                    None => repo.join(fleet_plan::ADR_DIR),
                };
                let filter = match status {
                    Some(s) => fleet_plan::AdrStatusFilter::parse(&s)?,
                    None => fleet_plan::AdrStatusFilter::Proposed,
                };
                let proposal = fleet_plan::propose_from_adrs(&dir, filter, forced)
                    .with_context(|| format!("план из ADR каталога '{}'", dir.display()))?;
                let path = fleet_plan::write_plan(&proposal.plan, &target)
                    .with_context(|| format!("запись плана в '{}'", target.display()))?;
                outln!("План записан: {}", path.display());
                outp!("{}", proposal.rationale);
                for w in &proposal.warnings {
                    outln!("  ⚠ {w}");
                }
                let issues = fleet_plan::validate_plan(&proposal.plan);
                if !issues.is_empty() {
                    outln!("\nПроверка черновика:");
                    for issue in &issues {
                        outln!("  {:?}: {}", issue.severity, issue.message);
                    }
                }
                // Гейт независимости — отказ, а не совет: пара без пути с общим
                // путём записи не может стартовать (ADR-045). План-черновик уже
                // записан — владелец правит рёбра, а не генерирует заново.
                if issues
                    .iter()
                    .any(|i| i.severity == fleet_plan::PlanSeverity::Error)
                {
                    std::process::exit(1);
                }
            } else {
                if adr_dir.is_some() || status.is_some() {
                    anyhow::bail!("--adr-dir/--status имеют смысл только с --from-adrs");
                }
                let (plan, rationale) = fleet_plan::propose(&repo, forced)?;
                let path = fleet_plan::write_plan(&plan, &target)?;
                outln!("План записан: {}", path.display());
                outp!("{rationale}");
                let issues = fleet_plan::validate_plan(&plan);
                if !issues.is_empty() {
                    outln!("\nПроверка черновика:");
                    for issue in &issues {
                        outln!("  {:?}: {}", issue.severity, issue.message);
                    }
                }
            }
        }
        FleetPlanCmd::Validate { plan, json } => {
            let parsed = fleet_plan::parse_plan(&plan)?;
            let issues = fleet_plan::validate_plan(&parsed);
            let has_error = issues
                .iter()
                .any(|i| i.severity == fleet_plan::PlanSeverity::Error);
            if json {
                outln!("{}", serde_json::to_string_pretty(&issues)?);
            } else if issues.is_empty() {
                outln!("План {} валиден: замечаний нет", parsed.id);
            } else {
                for issue in &issues {
                    outln!("  {:?}: {}", issue.severity, issue.message);
                }
                outln!(
                    "Итог: {}",
                    if has_error {
                        "FAIL (exit 1)"
                    } else {
                        "PASS с замечаниями"
                    }
                );
            }
            if has_error {
                std::process::exit(1);
            }
        }
        FleetPlanCmd::Show {
            plan,
            mermaid,
            json,
        } => {
            let parsed = fleet_plan::parse_plan(&plan)?;
            let compiled = fleet_plan::compile(parsed)?;
            if mermaid {
                outp!("{}", fleet_plan::render_mermaid(&compiled));
            } else if json {
                let waves: Vec<Vec<String>> = compiled
                    .levels
                    .iter()
                    .map(|level| {
                        level
                            .iter()
                            .filter_map(|i| compiled.nodes().get(*i).map(|n| n.id.clone()))
                            .collect()
                    })
                    .collect();
                let summary = serde_json::json!({
                    "id": compiled.plan.id,
                    "pattern": compiled.plan.pattern.as_str(),
                    "max_parallel": compiled.plan.policy.max_parallel,
                    "require_worktree": compiled.plan.policy.require_worktree,
                    "merge_gate": compiled.plan.policy.merge_gate,
                    "nodes": compiled.nodes().len(),
                    "waves": waves,
                    "warnings": compiled.warnings,
                });
                outln!("{}", serde_json::to_string_pretty(&summary)?);
            } else {
                outp!("{}", fleet_plan::render_plan(&compiled));
            }
        }
    }
    Ok(())
}

/// `arch-ml worktree`: изоляция агентной работы (создание, review, accept, drop).
pub(crate) async fn cmd_worktree(cfg: &Arc<Config>, cmd: WorktreeCmd) -> Result<()> {
    let cwd = std::env::current_dir().context("cwd")?;
    let repo_of = |repo: Option<PathBuf>| repo.unwrap_or_else(|| cwd.clone());
    match cmd {
        WorktreeCmd::New { name, repo, base } => {
            let path =
                arch_harness::worktree::create(cfg, &repo_of(repo), &name, base.as_deref()).await?;
            outln!("worktree создан: {}", path.display());
            outln!(
                "review: arch-ml worktree diff {name} · accept: arch-ml worktree accept {name} · drop: arch-ml worktree drop {name}"
            );
        }
        WorktreeCmd::List { repo } => {
            let infos = arch_harness::worktree::list(&repo_of(repo)).await?;
            outp!("{}", arch_harness::worktree::render_list(&infos));
        }
        WorktreeCmd::Diff { name, repo } => {
            outln!(
                "{}",
                arch_harness::worktree::diff(&repo_of(repo), &name).await?
            );
        }
        WorktreeCmd::Accept {
            name,
            repo,
            approver,
        } => {
            let outcome =
                arch_harness::worktree::accept(cfg, &repo_of(repo), &name, approver.as_deref())
                    .await?;
            outln!("{}", outcome.text);
            // Код 4 — «мерж состоялся, но post-merge гейт красный»: отличим
            // от 1 («приёмка отклонена, основная ветка не тронута»), ADR-024.
            if outcome.exit_code() != 0 {
                std::process::exit(outcome.exit_code());
            }
        }
        WorktreeCmd::Drop { name, repo } => {
            outln!(
                "{}",
                arch_harness::worktree::drop(cfg, &repo_of(repo), &name).await?
            );
        }
    }
    Ok(())
}
