//! Типизированная модель архитектуры кейса (`model`, ADR-003): ссылочная
//! целостность, карточки сущностей, граф связей, проекция ADR, экспорт
//! (Structurizr/`PlantUML`/drawio/`ArchiMate`, ADR-009/ADR-032) и импорт,
//! ландшафт систем (EA-3, ADR-037); позвенная трассируемость (`trace`,
//! ADR-006); количественные NFR (`nfr`, ADR-007); глобальный реестр ADR
//! (`adr registry`, ADR-036).

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Subcommand;

/// Подкоманды `arch-ml adr` (реестр ADR, ADR-036).
#[derive(Subcommand)]
pub(crate) enum AdrCmd {
    /// Глобальный реестр ADR по набору проектов: сам ROOT + непосредственные
    /// подкаталоги; источники — docs/adr/*.md (проза) и model/ADR-*.md
    /// (типизированные сущности ADR-003).
    Registry {
        /// Корневой каталог набора проектов.
        root: PathBuf,
        /// Машиночитаемый вывод: единый JSON {entries, findings}.
        #[arg(long)]
        json: bool,
        /// Exit 1 при любой находке (коллизия номеров, дубль заголовка,
        /// пропуск даты/статуса) — гейт для CI; по умолчанию exit 0.
        #[arg(long)]
        strict: bool,
    },
}

/// Подкоманды `arch-ml model` (ADR-003).
#[derive(Subcommand)]
pub(crate) enum ModelCmd {
    /// Ссылочная целостность модели: битая ссылка/дубль ID/цикл `depends_on` —
    /// error (exit code 1, как у `control check`); ADR без CMP, NFR без
    /// способа проверки — warn.
    Validate {
        /// Каталог модели.
        dir: PathBuf,
    },
    /// Карточка сущности: шапка, связи, обратные ссылки, тело.
    Show {
        /// ID сущности (ADR-001, CMP-002, …).
        id: String,
        /// Каталог модели (по умолчанию ./model).
        #[arg(long, default_value = "model")]
        dir: PathBuf,
    },
    /// Граф связей модели.
    Graph {
        /// Каталог модели (по умолчанию ./model).
        #[arg(long, default_value = "model")]
        dir: PathBuf,
        /// Формат: text (список) или mermaid (flowchart, совместим с `arch-ml mermaid`).
        #[arg(long, default_value = "text")]
        format: String,
    },
    /// Проекция: рендер ADR-файлов из модели в <кейс>/.arch-handoff/adr/
    /// (зеркально; устаревшие ADR-*.md удаляются).
    Project {
        /// Каталог модели.
        dir: PathBuf,
    },
    /// Экспорт модели в отраслевой формат (ADR-009, ADR-032): Structurizr
    /// DSL, `PlantUML`, drawio (SYS/CMP/INT + связи) или `ArchiMate` Open
    /// Exchange 3.2 (SYS/CMP/INT/CAP/REQ/NFR/AD + связи) — на stdout.
    Export {
        /// Каталог модели.
        dir: PathBuf,
        /// Формат: structurizr, plantuml, drawio или archimate.
        #[arg(long)]
        format: String,
    },
    /// Импорт Structurizr DSL в модель: сущности SYS/CMP/INT + связи
    /// (по одному .md на элемент; существующие файлы не затираются).
    Import {
        /// Файл Structurizr DSL.
        file: PathBuf,
        /// Формат (пока только structurizr).
        #[arg(long)]
        format: String,
        /// Каталог модели-получателя (создаётся при отсутствии).
        #[arg(long, default_value = "model")]
        dir: PathBuf,
    },
    /// Ландшафт систем набора проектов (EA-3, ADR-036/ADR-037): агрегация
    /// `model/` самого ROOT и непосредственных подкаталогов в единый
    /// реестр систем SYS/INT с дедупликацией по имени (без глобальных ID),
    /// находки (id-divergence, status-conflict, dangling-ref,
    /// cross-project-link) и топ связности.
    Landscape {
        /// Корневой каталог набора проектов.
        root: PathBuf,
        /// Дополнительно вывести mermaid `graph TD` ландшафта.
        #[arg(long)]
        mermaid: bool,
    },
}

/// Подкоманды `arch-ml trace` (ADR-006).
#[derive(Subcommand)]
pub(crate) enum TraceCmd {
    /// Позвенная трассируемость: REQ → NFR → AD/ADR → CMP → правило
    /// `CONSTRAINTS.yaml`; AD без правила и без `unverifiable` — error
    /// (exit code 1). Отчёт markdown, пригоден для evidence bundle.
    Check {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
}

/// Подкоманды `arch-ml nfr` (ADR-007).
#[derive(Subcommand)]
pub(crate) enum NfrCmd {
    /// Latency-бюджет: сумма бюджетов hop'ов INT-* против цели p99 из NFR-*;
    /// hop без бюджета или превышение — error (exit code 1).
    Budget {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
    /// Доступность: композиция последовательных/параллельных участков против
    /// SLA из NFR-* + цели RTO/RPO; ниже SLA — error (exit code 1).
    Availability {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
    /// Пропускная способность: RPS-цель против ёмкости компонентов
    /// (instances × `rps_per_instance`); дефицит — error (exit code 1).
    Capacity {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
    /// Стоимость: TCO (инстансы × тариф) и цена выхода (Σ `exit_cost`)
    /// по тарифным данным сущностей.
    Cost {
        /// Корень кейса (каталог с model/).
        dir: PathBuf,
    },
}

/// `arch-ml adr`: реестр ADR по набору проектов (ADR-036).
pub(crate) fn cmd_adr(cmd: AdrCmd) -> Result<()> {
    match cmd {
        AdrCmd::Registry { root, json, strict } => {
            let report = arch_harness::adr_registry::build_registry(&root)?;
            if json {
                outln!(
                    "{}",
                    serde_json::to_string(&report).expect("RegistryReport сериализуется")
                );
            } else {
                outln!("{}", arch_harness::adr_registry::render_markdown(&report));
            }
            let code = arch_harness::adr_registry::exit_code(&report, strict);
            if code != 0 {
                std::process::exit(code);
            }
        }
    }
    Ok(())
}

/// `arch-ml model`: типизированная модель архитектуры (ADR-003).
pub(crate) fn cmd_model(cmd: ModelCmd) -> Result<()> {
    match cmd {
        ModelCmd::Validate { dir } => {
            let model = arch_harness::model::load_model(&dir)
                .with_context(|| format!("загрузка модели {}", dir.display()))?;
            let report = arch_harness::model::validate(&model);
            for i in &report.issues {
                outln!(
                    "[{}] {}: {} — {}",
                    i.severity,
                    i.file.display(),
                    i.rule,
                    i.message
                );
            }
            outln!("{}", report.summary());
            outln!(
                "Итог: {}",
                if report.has_errors() { "FAIL" } else { "PASS" }
            );
            if report.has_errors() {
                std::process::exit(1);
            }
        }
        ModelCmd::Show { id, dir } => {
            let model = arch_harness::model::load_model(&dir)
                .with_context(|| format!("загрузка модели {}", dir.display()))?;
            let entity = model
                .get(&id)
                .with_context(|| format!("сущность '{id}' не найдена в {}", dir.display()))?;
            outp!("{}", arch_harness::model::card(&model, entity));
        }
        ModelCmd::Graph { dir, format } => {
            let model = arch_harness::model::load_model(&dir)
                .with_context(|| format!("загрузка модели {}", dir.display()))?;
            match format.as_str() {
                "text" => outp!("{}", arch_harness::model::graph_text(&model)),
                "mermaid" => outp!("{}", arch_harness::model::graph_mermaid(&model)),
                other => anyhow::bail!("неизвестный формат '{other}' (допустимы: text, mermaid)"),
            }
        }
        ModelCmd::Project { dir } => {
            let report = arch_harness::model::project_adr(&dir)
                .with_context(|| format!("проекция модели {}", dir.display()))?;
            for f in &report.written {
                outln!("записан: {}", f.display());
            }
            for f in &report.removed {
                outln!("удалён (нет сущности): {}", f.display());
            }
            outln!(
                "Проекция {}: {} ADR-файлов, удалено устаревших: {}",
                report.out_dir.display(),
                report.written.len(),
                report.removed.len()
            );
        }
        ModelCmd::Export { dir, format } => {
            let fmt = arch_harness::model::ExportFormat::from_name(&format).with_context(|| {
                format!(
                    "неизвестный формат '{format}' (допустимы: {})",
                    arch_harness::model::ExportFormat::names()
                )
            })?;
            let model = arch_harness::model::load_model(&dir)
                .with_context(|| format!("загрузка модели {}", dir.display()))?;
            let text = arch_harness::model::export_model(&model, fmt)
                .with_context(|| format!("экспорт модели {}", dir.display()))?;
            outp!("{text}");
        }
        ModelCmd::Import { file, format, dir } => {
            if !format.trim().eq_ignore_ascii_case("structurizr") {
                anyhow::bail!(
                    "импорт поддерживает только --format structurizr (получено: '{format}')"
                );
            }
            let report = arch_harness::model::import_structurizr(&file, &dir)
                .with_context(|| format!("импорт {} в {}", file.display(), dir.display()))?;
            for f in &report.written {
                outln!("записан: {}", f.display());
            }
            for w in &report.warnings {
                outln!("предупреждение: {w}");
            }
            outln!(
                "Импорт {}: {} сущностей, предупреждений: {}",
                report.dir.display(),
                report.written.len(),
                report.warnings.len()
            );
        }
        ModelCmd::Landscape { root, mermaid } => {
            let report = arch_harness::landscape::build_landscape(&root)?;
            outln!("{}", arch_harness::landscape::render_markdown(&report));
            if mermaid {
                outln!("\n```mermaid");
                outln!("{}", arch_harness::landscape::render_mermaid(&report));
                outln!("```");
            }
        }
    }
    Ok(())
}

/// `arch-ml trace`: трассируемость модели как fitness-функция (ADR-006).
pub(crate) fn cmd_trace(cmd: TraceCmd) -> Result<()> {
    match cmd {
        TraceCmd::Check { dir } => {
            let report = arch_harness::trace::trace_check(&dir)
                .with_context(|| format!("трассировка кейса {}", dir.display()))?;
            outp!("{}", arch_harness::trace::render_markdown(&report));
            if report.has_errors() {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml nfr`: количественные NFR поверх модели (ADR-007); error — exit code 1.
pub(crate) fn cmd_nfr(cmd: NfrCmd) -> Result<()> {
    match cmd {
        NfrCmd::Budget { dir } => {
            let report = arch_harness::nfr::budget_check(&dir)
                .with_context(|| format!("latency-бюджет кейса {}", dir.display()))?;
            outp!("{}", report.render());
            if report.has_errors() {
                std::process::exit(1);
            }
        }
        NfrCmd::Availability { dir } => {
            let report = arch_harness::nfr::availability_check(&dir)
                .with_context(|| format!("расчёт доступности кейса {}", dir.display()))?;
            outp!("{}", report.render());
            if report.has_errors() {
                std::process::exit(1);
            }
        }
        NfrCmd::Capacity { dir } => {
            let report = arch_harness::nfr::capacity_check(&dir)
                .with_context(|| format!("расчёт ёмкости кейса {}", dir.display()))?;
            outp!("{}", report.render());
            if report.has_errors() {
                std::process::exit(1);
            }
        }
        NfrCmd::Cost { dir } => {
            let report = arch_harness::nfr::cost_check(&dir)
                .with_context(|| format!("расчёт стоимости кейса {}", dir.display()))?;
            outp!("{}", report.render());
            if report.has_errors() {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}
