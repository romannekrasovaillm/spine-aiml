//! Общие команды харнесса: `init` (конфиг + ассеты в ~/.arch-ml), `models`,
//! `prompts` (библиотека промптов), `memory` (глобальная md-память),
//! `mermaid` (рендер в Unicode/ASCII-арт), `doctor` (диагностика окружения),
//! `metrics` (операционные метрики из журналов), `preflight` (гейт
//! ML-эксперимента до аренды GPU), `export` (журнал сессии → Word/Excel),
//! `policy` (уровни автономии R0–R5), `cron` (планировщик md-задач).

use std::io::Read as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Subcommand;

use arch_harness::config::Config;
use arch_harness::llm::LlmRegistry;

/// Подкоманды `arch-ml memory`.
#[derive(Subcommand)]
pub(crate) enum MemoryCmd {
    /// Дописать заметку в конец файла памяти.
    Add {
        /// Текст заметки.
        text: String,
    },
}

#[derive(Subcommand)]
pub(crate) enum CronCmd {
    /// Список задач расписания.
    List,
    /// Запустить задачу по имени сейчас.
    Run {
        /// Имя задачи.
        name: String,
    },
    /// Проверить и запустить дюжные задачи (для системного cron).
    Tick,
}

/// `arch-ml init`: конфиг + ассеты в ~/.arch-ml.
pub(crate) fn cmd_init(cfg: &Config) -> Result<()> {
    let home = Config::home_dir();
    std::fs::create_dir_all(&home).context("создание домашнего каталога")?;
    let written = arch_harness::assets::write_defaults(&home)?;
    let cfg_path = cfg.save_default()?;
    outln!("Инициализация завершена:");
    outln!("  конфиг:  {}", cfg_path.display());
    outln!("  домашний каталог: {}", home.display());
    for f in &written {
        outln!("  ассет:   {}", f.display());
    }
    Ok(())
}

/// `arch-ml models`: список настроенных моделей.
pub(crate) fn cmd_models(cfg: &Config) -> Result<()> {
    let registry = LlmRegistry::from_config(cfg)?;
    outln!("Модели (по умолчанию: {}):", registry.default_name());
    for name in registry.names() {
        let p = registry.get(&name)?;
        outln!("  {name:<20} {} ({})", p.model(), p.name());
    }
    Ok(())
}

/// `arch-ml prompts`.
pub(crate) fn cmd_prompts(cfg: &Config, name: Option<String>) -> Result<()> {
    let lib = arch_harness::agent::prompts::load_library(&cfg.paths.prompts_dir())?;
    match name {
        None => {
            outln!(
                "Библиотека промптов ({}):",
                cfg.paths.prompts_dir().display()
            );
            for tpl in &lib {
                outln!("  {:<24} {}", tpl.name, tpl.description);
            }
        }
        Some(n) => {
            let tpl = lib
                .iter()
                .find(|t| t.name == n)
                .with_context(|| format!("шаблон '{n}' не найден"))?;
            outln!("{}", tpl.body);
        }
    }
    Ok(())
}

/// `arch-ml memory [add <текст>]`: путь и содержимое глобальной md-памяти
/// либо дописка заметки в конец файла.
pub(crate) fn cmd_memory(cfg: &Config, cmd: Option<MemoryCmd>) -> Result<()> {
    let path = &cfg.paths.memory_file;
    match cmd {
        None => match arch_harness::memory::load(path)? {
            Some(content) => outln!("Память ({}):\n{content}", path.display()),
            None => outln!(
                "память пустая, файл: {} (дописать — arch-ml memory add <текст>)",
                path.display()
            ),
        },
        Some(MemoryCmd::Add { text }) => {
            arch_harness::memory::append(path, &text)?;
            outln!("заметка дописана в память: {}", path.display());
        }
    }
    Ok(())
}

/// `arch-ml mermaid`: рендер mermaid-файла в Unicode/ASCII-арт.
pub(crate) fn cmd_mermaid(file: &str) -> Result<()> {
    // Каталог — понятная подсказка со списком *.mmd, а не «os error 21».
    let input = if file != "-" && std::path::Path::new(file).is_dir() {
        arch_harness::mermaid::read_diagram_source(std::path::Path::new(file))?
    } else {
        read_file_or_stdin(file)?
    };
    let art = arch_harness::mermaid::render(&input)?;
    outln!("{art}");
    Ok(())
}

/// `arch-ml doctor`: диагностика окружения (ключи, каталоги, плагины, MCP).
pub(crate) fn cmd_doctor(cfg: &Config) {
    let checks = arch_harness::doctor::run_checks(cfg);
    outp!("{}", arch_harness::doctor::render(&checks));
    if arch_harness::doctor::exit_code(&checks) != 0 {
        std::process::exit(1);
    }
}

/// `arch-ml metrics`: операционные метрики харнесса (из журналов сессий
/// и отчётов); `--cost-report` — смета по реальному usage.
pub(crate) fn cmd_metrics(cfg: &Config, cost_report: bool) -> Result<()> {
    if cost_report {
        // Смета по реальным записям usage журналов (тарифы — из конфига).
        let report = arch_harness::metrics::cost_report(&cfg.paths.sessions_dir, &cfg.models);
        outp!("{}", arch_harness::metrics::render_cost_report(&report));
        return Ok(());
    }
    let mut m = arch_harness::metrics::collect(&cfg.paths.sessions_dir, &cfg.paths.reports_dir)?;
    // Денежная стоимость — только по тарифам моделей из конфига
    // (None — тарифы не заданы, выдуманного курса нет).
    m.total_cost =
        arch_harness::metrics::cost_report(&cfg.paths.sessions_dir, &cfg.models).total_cost;
    // Architecture drift по реестру AGENTS.md (repos.txt), если он ведётся.
    let registry = arch_harness::config::Config::home_dir().join("repos.txt");
    if registry.is_file() {
        if let Ok(report) = arch_harness::agentsmd::lint_registry(&registry) {
            m.agentsmd_total = report.len();
            m.agentsmd_stale = report
                .iter()
                .filter(|(_, issues)| {
                    issues
                        .iter()
                        .any(|i| i.rule.contains("stale") || i.severity == "error")
                })
                .count();
        }
    }
    outln!("{}", m.to_markdown());
    Ok(())
}

/// `arch-ml preflight`: pre-flight гейт ML-эксперимента (TOML-спецификация).
pub(crate) fn cmd_preflight(spec: Option<PathBuf>, example: bool, json: bool) -> Result<()> {
    if example {
        outp!("{}", arch_harness::preflight::example_spec());
        return Ok(());
    }
    let Some(spec_path) = spec else {
        anyhow::bail!("нужен SPEC.toml (или --example для образца)");
    };
    let spec = arch_harness::preflight::parse_spec(&spec_path)?;
    if json {
        outln!("{}", arch_harness::preflight::render_json(&spec));
    } else {
        let gates = arch_harness::preflight::run_preflight(&spec);
        outp!("{}", arch_harness::preflight::render(&gates));
        if arch_harness::preflight::exit_code(&gates) != 0 {
            std::process::exit(1);
        }
    }
    Ok(())
}

/// `arch-ml export`: экспорт журнала сессии в Word/Excel.
pub(crate) fn cmd_export(format: &str, session: &Path, out: &Path) -> Result<()> {
    let Some(fmt) = arch_harness::export::ExportFormat::parse(format) else {
        return Err(anyhow::anyhow!(
            "неизвестный формат «{format}» (ожидалось word|excel)"
        ));
    };
    let n = arch_harness::export::export_journal(session, fmt, out)?;
    outln!("экспортировано {n} строк → {}", out.display());
    Ok(())
}

/// `arch-ml policy`: уровень автономии и классификация команды.
pub(crate) fn cmd_policy(cfg: &Config, check: Option<String>) -> Result<()> {
    let policy = arch_harness::policy::Policy::parse(&cfg.policy.autonomy)?;
    match check {
        None => {
            outln!(
                "Уровень автономии: R{} (из config [policy] autonomy)",
                policy.level
            );
            outln!(
                "  R0 — только чтения авто; R2 — + изменения (дефолт); R4 — деструктив с подтверждением; R5 — полная (красный флаг аудита)"
            );
        }
        Some(cmd) => {
            use arch_harness::policy::{PolicyDecision, classify_bash};
            let class = classify_bash(&cmd);
            let decision = policy.check("bash", &serde_json::json!({"command": cmd}));
            let verdict = match &decision {
                PolicyDecision::Allow => "ALLOW",
                PolicyDecision::RequireConfirm(_) => "REQUIRE-CONFIRM",
                PolicyDecision::Deny(_) => "DENY",
            };
            outln!(
                "команда: {cmd}\nкласс риска: {class:?}\nрешение (R{}): {verdict}",
                policy.level
            );
            match &decision {
                PolicyDecision::RequireConfirm(m) | PolicyDecision::Deny(m) => {
                    outln!("причина: {m}");
                }
                PolicyDecision::Allow => {}
            }
        }
    }
    Ok(())
}

/// `arch-ml cron`: планировщик md-задач (list/run/tick).
pub(crate) async fn cmd_cron(cfg: &Arc<Config>, cmd: CronCmd) -> Result<()> {
    let tab = arch_harness::cron::load(&cfg.cron.file)?;
    match cmd {
        CronCmd::List => {
            for j in &tab.jobs {
                outln!(
                    "  {:<24} {:<16} {}",
                    j.name,
                    j.schedule,
                    j.task_md.display()
                );
            }
        }
        CronCmd::Run { name } => {
            let job = tab
                .jobs
                .iter()
                .find(|j| j.name == name)
                .with_context(|| format!("задача '{name}' не найдена"))?;
            let registry = LlmRegistry::from_config(cfg)?;
            let provider = match &job.model {
                Some(m) => registry.get(m)?,
                None => registry.default(),
            };
            let tools = arch_harness::tools::full_registry(cfg);
            let out_dir = job
                .out
                .clone()
                .unwrap_or_else(|| cfg.paths.reports_dir.join("cron"));
            let path =
                arch_harness::cron::run_job(job, provider.as_ref(), &tools, &out_dir).await?;
            outln!("Отчёт: {}", path.display());
        }
        CronCmd::Tick => {
            // «Дюжные» задачи между прошлым тиком и сейчас; метка — в state-файле.
            let state_file = Config::home_dir().join("cron-last-tick");
            let now = chrono::Local::now();
            let last = std::fs::read_to_string(&state_file)
                .ok()
                .and_then(|s| {
                    chrono::DateTime::parse_from_rfc3339(s.trim())
                        .ok()
                        .map(|dt| dt.with_timezone(&chrono::Local))
                })
                .unwrap_or_else(|| now - chrono::Duration::hours(24));
            let registry = LlmRegistry::from_config(cfg)?;
            let provider = registry.default();
            let tools = arch_harness::tools::full_registry(cfg);
            let reports_dir = cfg
                .cron
                .out_dir
                .clone()
                .unwrap_or_else(|| cfg.paths.reports_dir.join("cron"));
            let reports = arch_harness::cron::run_due(
                &tab,
                last,
                now,
                provider.as_ref(),
                &tools,
                &reports_dir,
            )
            .await?;
            std::fs::write(&state_file, now.to_rfc3339()).context("запись метки тика")?;
            if reports.is_empty() {
                println!("Дюжных задач нет.");
            }
            for path in &reports {
                println!("Отчёт: {}", path.display());
            }
        }
    }
    Ok(())
}

/// Читает файл или stdin (`-`).
fn read_file_or_stdin(file: &str) -> Result<String> {
    if file == "-" {
        let mut buf = String::new();
        std::io::stdin()
            .read_to_string(&mut buf)
            .context("чтение stdin")?;
        Ok(buf)
    } else {
        std::fs::read_to_string(file).with_context(|| format!("чтение {file}"))
    }
}
