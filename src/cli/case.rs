//! Изменение кейса: дельта-спецификации (`delta`: propose → apply → archive,
//! гейт прямых правок спайна, модель 5.2), Evidence Bundle (`evidence`:
//! аудиторский след как условие выпуска), обратное обследование
//! legacy-репозитория (`survey`, reverse discovery → docs/reverse/survey.md).

use std::path::{Path, PathBuf};

use anyhow::Result;
use clap::Subcommand;

#[derive(Subcommand)]
pub(crate) enum EvidenceCmd {
    /// Собрать bundle (EVIDENCE.yaml) по каталогу изменения.
    Pack {
        /// Каталог изменения.
        dir: PathBuf,
        /// Маршрут: fast|standard|critical.
        #[arg(long, default_value = "standard")]
        route: String,
    },
    /// Проверить bundle: полнота + целостность хэшей.
    Verify {
        /// Каталог изменения.
        dir: PathBuf,
    },
}

#[derive(Subcommand)]
pub(crate) enum DeltaCmd {
    /// Новая дельта (каркас changes/<name>/DELTA.md).
    New {
        /// Имя изменения (kebab-case).
        name: String,
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Список дельт (предложенные/архивные).
    List {
        /// Репозиторий.
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Валидация структуры дельты.
    Validate {
        /// Имя дельты.
        name: String,
        /// Репозиторий.
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Архивировать дельту после apply (вливание в живую истину).
    Archive {
        /// Имя дельты.
        name: String,
        /// Репозиторий.
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Гейт прямых правок спайна: изменённые защищённые файлы обязаны
    /// упоминаться в активной дельте changes/<name>/DELTA.md, иначе exit 1.
    /// Новые untracked-файлы git-diff не видит — для CI используйте --base.
    Guard {
        /// Репозиторий (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
        /// База diff (по умолчанию HEAD — staged+unstaged рабочего дерева;
        /// для CI — напр. origin/main...HEAD: трёхточечную форму разбирает
        /// сам git).
        #[arg(long)]
        base: Option<String>,
        /// Защищаемый путь/префикс (повторяемый). Если задан хотя бы один —
        /// заменяет дефолт: model/, ARCHITECTURE-SPINE.md, `CONSTRAINTS.yaml`.
        #[arg(long)]
        protect: Vec<String>,
    },
}

/// `arch-ml evidence`: Evidence Bundle.
pub(crate) fn cmd_evidence(cmd: EvidenceCmd) -> Result<()> {
    match cmd {
        EvidenceCmd::Pack { dir, route } => {
            let route = match route.to_lowercase().as_str() {
                "fast" => arch_harness::control::Route::Fast,
                "critical" => arch_harness::control::Route::Critical,
                _ => arch_harness::control::Route::Standard,
            };
            let (bundle, verdict) = arch_harness::evidence::pack(&dir, route)?;
            outln!("{}", verdict.summary);
            for item in &bundle.items {
                outln!("  + {:<20} {} ({} б)", item.key, item.path, item.size);
            }
            for miss in &verdict.missing {
                outln!("  ✗ ОТСУТСТВУЕТ: {miss}");
            }
            outln!("Манифест: {}", dir.join("EVIDENCE.yaml").display());
            if !verdict.passed {
                std::process::exit(1);
            }
        }
        EvidenceCmd::Verify { dir } => {
            let v = arch_harness::evidence::verify(&dir)?;
            outln!("{}", v.summary);
            for m in &v.missing {
                outln!("  ✗ ОТСУТСТВУЕТ: {m}");
            }
            for t in &v.tampered {
                outln!("  ✗ ИЗМЕНЁН: {t}");
            }
            outln!(
                "Итог: {}",
                if v.passed {
                    "PASS — выпуск разрешён"
                } else {
                    "FAIL — выпуск заблокирован"
                }
            );
            if !v.passed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml delta`: дельта-спецификации.
pub(crate) fn cmd_delta(cmd: DeltaCmd) -> Result<()> {
    let cwd = || std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    match cmd {
        DeltaCmd::New { name, repo } => {
            let path = arch_harness::delta::new(&repo.unwrap_or_else(cwd), &name)?;
            outln!("Дельта создана: {}", path.display());
        }
        DeltaCmd::List { repo } => {
            let list = arch_harness::delta::list(&repo.unwrap_or_else(cwd));
            if list.is_empty() {
                outln!("Дельт нет (changes/ пуст или отсутствует).");
            }
            for d in &list {
                outln!("  {:<30} {:?}", d.name, d.status);
            }
        }
        DeltaCmd::Validate { name, repo } => {
            let issues = arch_harness::delta::validate(&repo.unwrap_or_else(cwd), &name)?;
            if issues.is_empty() {
                outln!("дельта '{name}': нарушений нет");
            }
            let mut failed = false;
            for i in &issues {
                outln!(
                    "[{}] {}:{} {} — {}",
                    i.severity,
                    i.file.display(),
                    i.line,
                    i.rule,
                    i.message
                );
                failed |= i.severity == "error";
            }
            if failed {
                std::process::exit(1);
            }
        }
        DeltaCmd::Archive { name, repo } => {
            let path = arch_harness::delta::archive(&repo.unwrap_or_else(cwd), &name)?;
            outln!("Дельта заархивирована: {}", path.display());
        }
        DeltaCmd::Guard {
            repo,
            base,
            protect,
        } => {
            let report =
                arch_harness::delta::guard(&repo.unwrap_or_else(cwd), base.as_deref(), &protect)?;
            outp!("{}", arch_harness::delta::render_guard(&report));
            if !report.passed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// `arch-ml survey <repo>`: обратное обследование legacy → docs/reverse/survey.md.
pub(crate) fn cmd_survey(repo: &Path, out: Option<&Path>) -> Result<()> {
    let outcome = arch_harness::survey::run(repo, out)?;
    outln!(
        "обследование `{}`: {} находок [confirmed], {} секций [gap]",
        outcome.report.repo_name,
        outcome.report.confirmed_count(),
        outcome.report.gap_count()
    );
    outln!("карта: {}", outcome.survey_path.display());
    if outcome.notes_created {
        outln!(
            "создана заготовка заметок [inferred]: {}",
            outcome.notes_path.display()
        );
    }
    Ok(())
}
