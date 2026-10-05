//! Кодовые харнессы и MCP: handoff-пакеты (AD-8), прогон `harness-run`
//! (изоляция в worktree при `[fleet] require_worktree`, контракт результата,
//! скриптовые коды выхода 2/3), реестр известных харнессов, MCP-клиент
//! (`mcp list|call`) и MCP-сервер `mcp serve` (stdio JSON-RPC, ADR-008).

use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Subcommand;

use arch_harness::config::Config;

#[derive(Subcommand)]
pub(crate) enum McpCmd {
    /// Список серверов и их инструментов.
    List,
    /// Вызвать MCP-инструмент.
    Call {
        /// Составное имя `server__tool`.
        name: String,
        /// Аргументы JSON.
        #[arg(default_value = "{}")]
        args: String,
    },
    /// MCP-сервер (stdio JSON-RPC, NDJSON): архитектурный контроль кодовым
    /// агентам (Claude Code и др.) — `spine_lint`, `fitness_check`,
    /// `significance_score`, `trace_check`, `model_query`, `rubric_run` (ADR-008).
    Serve,
}

/// `arch-ml handoff`: сформировать handoff-пакет для кодового харнесса.
pub(crate) fn cmd_handoff(
    cfg: &Config,
    harness: &str,
    repo: &Path,
    task: &str,
    spec: &[PathBuf],
    rollback: Option<&str>,
    route: &str,
) -> Result<()> {
    if !cfg.harnesses.contains_key(harness) {
        anyhow::bail!(
            "неизвестный харнесс '{harness}'. Известные: {:?}",
            arch_harness::harness::known()
        );
    }
    let route: arch_harness::control::Route =
        route.parse().map_err(|e: String| anyhow::anyhow!(e))?;
    let packet = arch_harness::harness::generate_handoff(repo, task, spec, cfg, rollback, route)?;
    outln!("Handoff-пакет: {}", packet.dir.display());
    for f in &packet.files {
        outln!("  {}", f.display());
    }
    outln!("epic-context ≈ {} токенов", packet.epic_context_tokens);
    match &packet.baseline {
        Some(h) => outln!(
            "git: {}baseline {h} (якорь отката)",
            if packet.git_initialized {
                "инициализирован, "
            } else {
                ""
            }
        ),
        None => outln!("⚠ git недоступен — якоря отката нет"),
    }
    outln!(
        "маршрут {route} → рекомендованный timeout_secs={}",
        packet.recommended_timeout_secs
    );
    if packet.git_dirty_tracked {
        outln!("⚠ незакоммиченные изменения отслеживаемых файлов: откат на baseline их потеряет");
    }
    Ok(())
}

/// `arch-ml harness-run`: прогнать кодовый харнесс по handoff-пакету.
pub(crate) async fn cmd_harness_run(
    cfg: &Arc<Config>,
    harness: &str,
    repo: &Path,
    task: Option<String>,
) -> Result<()> {
    use arch_harness::harness::Termination;
    let hcfg = cfg
        .harnesses
        .get(harness)
        .with_context(|| format!("харнесс '{harness}' не настроен"))?;
    // [fleet] require_worktree: прогон изолируется в git worktree
    // (ветка arch/<run-id>), основное дерево не трогается; мерж —
    // только гейтом владельца (`arch fleet merge <run-id>`).
    let (repo, run_id) =
        match arch_harness::harness::enforce_run_worktree(cfg, repo, harness).await? {
            Some((dir, run_id)) => {
                outln!(
                    "⚑ [fleet] require_worktree: прогон изолирован в worktree \
                 arch/{run_id} ({}); основное дерево не изменяется",
                    dir.display()
                );
                outln!(
                    "  интеграция — гейт владельца: arch fleet merge {run_id} \
                 [--owner-approve]; отклонение: arch worktree drop {run_id}"
                );
                (dir, Some(run_id))
            }
            None => (repo.to_path_buf(), None),
        };
    let task_text = match task {
        Some(t) => t,
        None => std::fs::read_to_string(repo.join(".arch-handoff/TASK.md"))
            .context("нет --task и не найден .arch-handoff/TASK.md")?,
    };
    let mut hcfg_owned = hcfg.clone();
    if let Some(t) = arch_harness::harness::recommended_timeout_secs(&repo) {
        // Пакет несёт рекомендацию по маршруту значимости (Fast/Standard/Critical).
        hcfg_owned.timeout_secs = t.clamp(600, 7200);
    }
    let run = arch_harness::harness::run_harness(harness, &hcfg_owned, &repo, &task_text).await?;
    if let Some(ac) = &run.auto_commit {
        outln!(
            "⚑ авто-коммит: исполнитель не зафиксировал результат — {} путей → {} «{}»",
            ac.files,
            ac.hash,
            ac.message
        );
    }
    match &run.contract {
        arch_harness::harness::ContractParse::Valid(c) => {
            outln!(
                "контракт: status={} assumptions={} open_questions={} conflicts={}",
                c.status.as_str(),
                c.assumptions.len(),
                c.open_questions.len(),
                c.conflicts.len()
            );
        }
        arch_harness::harness::ContractParse::Invalid(r) => {
            eprintln!("⚠ контракт найден, но невалиден по схеме: {r}");
        }
        arch_harness::harness::ContractParse::Missing => {
            eprintln!("⚠ контракт результата (```json со status) в stdout не найден");
        }
    }
    if let Some(id) = &run_id {
        outln!(
            "прогон изолирован в worktree arch/{id}: мерж — arch fleet merge {id} \
             --owner-approve, отклонение — arch worktree drop {id}"
        );
    }
    if run.termination != Termination::Completed {
        eprintln!(
            "⚠ прогон ПРЕРВАН ({}{}); процессная группа завершена, \
             репозиторий может быть в промежуточном состоянии — проверьте git status",
            run.termination,
            if run.termination == Termination::IdleTimeout {
                format!(" {} с", hcfg.idle_timeout_secs)
            } else {
                format!(" {} с", hcfg.timeout_secs)
            }
        );
    }
    outln!(
        "── stdout (exit {:?}, {:.1}s) ──",
        run.exit_code,
        run.duration_secs
    );
    outln!("{}", run.stdout);
    if !run.stderr.is_empty() {
        eprintln!("── stderr ──\n{}", run.stderr);
    }
    // Скриптовый гейт: status=blocked — код 2; непустые
    // conflicts_with_prior_decisions — код 3 (конфликт со spine
    // останавливает интеграцию по контракту). Полная схема кодов —
    // docs/harness_integrations.md.
    if let arch_harness::harness::ContractParse::Valid(c) = &run.contract {
        if c.status == arch_harness::harness::ContractStatus::Blocked {
            std::process::exit(2);
        }
        if !c.conflicts.is_empty() {
            std::process::exit(3);
        }
    }
    Ok(())
}

/// `arch-ml harnesses`: список известных кодовых харнессов и их статус.
pub(crate) fn cmd_harnesses(cfg: &Config) {
    outln!("Известные кодовые харнессы:");
    for name in arch_harness::harness::known() {
        let status = match cfg.harnesses.get(name) {
            Some(h) => format!("{} ({:?})", h.binary, h.prompt_mode),
            None => "не настроен".into(),
        };
        let installed = which(cfg.harnesses.get(name).map_or(name, |h| h.binary.as_str()));
        outln!("  {name:<14} {status:<40} {installed}");
    }
}

/// `arch-ml mcp`: MCP-серверы — список, вызовы, серверный режим.
pub(crate) async fn cmd_mcp(cfg: &Arc<Config>, cmd: McpCmd) -> Result<()> {
    // Серверный режим (P1-2, ADR-008) обслуживает клиентов и не подключается
    // к серверам: mcp.json для него не требуется, уходим до его загрузки.
    if matches!(cmd, McpCmd::Serve) {
        return arch_harness::mcp_server::serve(Arc::clone(cfg))
            .await
            .context("MCP-сервер (stdio)");
    }
    let mut servers = arch_harness::mcp::load_servers(&cfg.mcp.servers_file)
        .with_context(|| format!("чтение {}", cfg.mcp.servers_file.display()))?;
    // Плагины тоже несут MCP-серверы (стандарт: plugin.json mcpServers / .mcp.json).
    if cfg.plugins.include_mcp {
        let plugins = arch_harness::plugin::discover(&cfg.plugins.dirs);
        servers.extend(arch_harness::plugin::mcp_servers(&plugins));
    }
    let manager =
        Arc::new(arch_harness::mcp::McpManager::connect(&servers, cfg.mcp.timeout_secs).await?);
    match cmd {
        McpCmd::List => {
            outln!("Серверы: {}", manager.server_names().join(", "));
            for spec in manager.tools().await {
                outln!("  {:<40} {}", spec.name, spec.description);
            }
        }
        McpCmd::Call { name, args } => {
            let args: serde_json::Value =
                serde_json::from_str(&args).context("невалидный JSON аргументов")?;
            let out = manager.call(&name, args).await?;
            outln!("{}", out.content);
        }
        // Недостижимо: Serve обработан выше возвратом до подключения к серверам.
        McpCmd::Serve => {}
    }
    manager.shutdown().await;
    Ok(())
}

/// Есть ли бинарь в PATH.
fn which(binary: &str) -> String {
    std::process::Command::new("which")
        .arg(binary)
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map_or_else(
            || "MISSING".into(),
            |o| String::from_utf8_lossy(&o.stdout).trim().to_string(),
        )
}
