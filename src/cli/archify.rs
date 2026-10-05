//! Диаграммы Archify (`arch-ml archify`): проверка окружения, рекомендация
//! типа/сценария, валидация IR (9 artifact checks + composition-профиль),
//! атомарная доставка HTML с SHA-256 receipt, сравнение architecture-снапшотов.

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Subcommand;

use arch_harness::config::Config;

/// Подкоманды `arch-ml archify`.
#[derive(Subcommand)]
pub(crate) enum ArchifyCmd {
    /// Проверка окружения Archify (node, CLI, рендеры пяти типов).
    Doctor,
    /// Рекомендация типа диаграммы и сценария под запрос.
    Guide {
        /// Вопрос/сценарий на естественном языке.
        query: String,
    },
    /// Валидация IR: 9 artifact checks + composition-профиль.
    Validate {
        /// Тип диаграммы: architecture|workflow|sequence|dataflow|lifecycle.
        r#type: String,
        /// Путь к JSON IR.
        path: PathBuf,
        /// Composition-профиль приёмки: standard|showcase.
        #[arg(long, default_value = "showcase")]
        quality: String,
        /// Машиночитаемый вывод: сырой JSON-receipt Archify CLI (SDK-контракт v1).
        #[arg(long)]
        json: bool,
    },
    /// Финальная приёмка: атомарная доставка HTML + SHA-256 receipt.
    Deliver {
        /// Тип диаграммы: architecture|workflow|sequence|dataflow|lifecycle.
        r#type: String,
        /// Путь к JSON IR.
        path: PathBuf,
        /// Путь к выходному HTML.
        output: PathBuf,
        /// Composition-профиль приёмки: standard|showcase.
        #[arg(long, default_value = "showcase")]
        quality: String,
        /// Машиночитаемый вывод: сырой JSON-receipt Archify CLI (SDK-контракт v1).
        #[arg(long)]
        json: bool,
    },
    /// Дельта двух architecture-снапшотов (Before/Delta/After + receipt).
    Compare {
        /// Путь к базовому architecture JSON IR.
        base: PathBuf,
        /// Путь к целевому architecture JSON IR.
        head: PathBuf,
        /// Путь к выходному delta HTML.
        output: PathBuf,
        /// Composition-профиль приёмки: standard|showcase.
        #[arg(long, default_value = "showcase")]
        quality: String,
        /// Машиночитаемый вывод: сырой JSON-receipt Archify CLI (SDK-контракт v1).
        #[arg(long)]
        json: bool,
    },
}

/// Прокси к Archify CLI (`node <archify.mjs> …`): печатает вывод, код
/// возврата CLI становится кодом возврата `arch-ml` (гейт для CI/скриптов).
pub(crate) async fn cmd_archify(cfg: &Config, cmd: ArchifyCmd) -> Result<()> {
    let cwd = std::env::current_dir().context("archify: не удалось определить рабочий каталог")?;
    let args: Vec<String> = match &cmd {
        ArchifyCmd::Doctor => vec!["doctor".into()],
        ArchifyCmd::Guide { query } => vec!["guide".into(), query.clone(), "--json".into()],
        ArchifyCmd::Validate {
            r#type,
            path,
            quality,
            json: _,
        } => vec![
            "validate".into(),
            r#type.clone(),
            path.to_string_lossy().into_owned(),
            "--quality".into(),
            quality.clone(),
            "--json".into(),
        ],
        ArchifyCmd::Deliver {
            r#type,
            path,
            output,
            quality,
            json: _,
        } => vec![
            "deliver".into(),
            r#type.clone(),
            path.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
            "--quality".into(),
            quality.clone(),
            "--json".into(),
        ],
        ArchifyCmd::Compare {
            base,
            head,
            output,
            quality,
            json: _,
        } => vec![
            "compare".into(),
            "architecture".into(),
            base.to_string_lossy().into_owned(),
            head.to_string_lossy().into_owned(),
            output.to_string_lossy().into_owned(),
            "--quality".into(),
            quality.clone(),
            "--json".into(),
        ],
    };
    let run = arch_harness::archify::run(cfg, &cwd, &args, cfg.archify.timeout_secs).await?;
    // Для validate/deliver/compare отдаём компактную сводку receipt;
    // doctor/guide — сырой вывод CLI (текст/JSON рекомендации).
    // Флаг --json: сырой JSON-receipt Archify CLI (SDK-контракт v1).
    let json_mode = matches!(
        cmd,
        ArchifyCmd::Validate { json: true, .. }
            | ArchifyCmd::Deliver { json: true, .. }
            | ArchifyCmd::Compare { json: true, .. }
    );
    let summarize = !matches!(cmd, ArchifyCmd::Doctor | ArchifyCmd::Guide { .. });
    if json_mode {
        outp!("{}", run.stdout);
    } else if summarize {
        outp!(
            "{}",
            arch_harness::archify::summarize_receipt(&args[0], &run.stdout)
        );
    } else {
        outp!("{}", run.stdout);
    }
    if !run.stderr.trim().is_empty() {
        eprint!("{}", run.stderr);
    }
    if run.timed_out {
        anyhow::bail!(
            "archify {}: таймаут {} сек",
            args[0],
            cfg.archify.timeout_secs
        );
    }
    if !run.ok() {
        anyhow::bail!(
            "archify {}: провал (код выхода {})",
            args[0],
            run.status.map_or("?".to_string(), |c| c.to_string())
        );
    }
    Ok(())
}
