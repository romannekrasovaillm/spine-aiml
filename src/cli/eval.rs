//! Оценочный контур: якорные и динамические рубрики архитектурного контроля
//! с LLM-судьёй (`rubric`, evidence-bound, ADR-004), архитектурные бенчмарки
//! и golden-гейт судьи (`bench`, метрика согласия MAE + evidence-журнал),
//! регрессионные eval-сьюты конфигурации харнесса (`eval`, continuous evals,
//! docs/evals.md).

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::Subcommand;

use arch_harness::config::Config;
use arch_harness::llm::LlmRegistry;

#[derive(Subcommand)]
pub(crate) enum RubricCmd {
    /// Список якорных рубрик.
    List,
    /// Оценить файл по рубрике (LLM-судья).
    Run {
        /// Рубрика (имя файла в assets/rubrics или путь).
        rubric: String,
        /// Целевой документ (md/txt).
        target: PathBuf,
        /// Модель-судья.
        #[arg(long)]
        model: Option<String>,
        /// Сначала сгенерировать динамическую рубрику под предмет.
        #[arg(long)]
        dynamic_subject: Option<String>,
    },
}

#[derive(Subcommand)]
pub(crate) enum BenchCmd {
    /// Список бенчмарков.
    List,
    /// Прогнать бенчмарк.
    Run {
        /// Имя файла бенчмарка в assets/benchmarks (или путь).
        name: Option<String>,
        /// Испытуемая модель (для --golden — модель-судья).
        #[arg(long)]
        model: Option<String>,
        /// Прогон судьи по golden-set (assets/benchmarks/golden): метрика
        /// согласия с эталоном MAE; выше порога `judge.golden_max_mae` — exit 1.
        #[arg(long)]
        golden: bool,
        /// Дописать результат golden-прогона строкой JSON в evidence-журнал
        /// (M-2, история — `bench golden-history`).
        #[arg(long)]
        record: Option<PathBuf>,
    },
    /// История golden-прогонов из evidence-журнала (JSONL) — markdown-таблица.
    GoldenHistory {
        /// Путь к журналу, записанному `bench run --golden --record`.
        path: PathBuf,
    },
    /// Согласие golden-эталонов с оценками живых архитекторов (J-3,
    /// протокол — docs/judge-human-agreement.md).
    HumanAgreement {
        /// Каталог golden-set (`<имя>.md` + `<имя>.expected.yaml`).
        #[arg(long)]
        golden_dir: PathBuf,
        /// Каталог человеческих анкет (`<документ>.<участник>.expected.yaml`).
        #[arg(long)]
        humans: PathBuf,
    },
}

/// Подкоманды `arch eval` (continuous evals, docs/evals.md).
#[derive(Subcommand)]
pub(crate) enum EvalCmd {
    /// Прогнать eval-сьют: детерминированные проверки (офлайн) + опциональный
    /// LLM-судья. Pass-rate ниже гейта — exit code 1 (регрессионный гейт).
    Run {
        /// Каталог сьюта (YAML-задачи). Без флага — встроенный сьют
        /// agent-config, прогоняемый герметично (ассеты разворачиваются во
        /// временный каталог; живой конфиг не трогается).
        #[arg(long)]
        suite: Option<PathBuf>,
        /// Гейт pass-rate в процентах (дефолт 100): ниже — exit code 1.
        #[arg(long)]
        gate: Option<f64>,
        /// Включить слой LLM-судьи (prompt-задачи и рубрики; нужен API-ключ).
        #[arg(long)]
        judge: bool,
        /// Модель для слоя судьи (имя из [models]; иначе — default).
        #[arg(long)]
        model: Option<String>,
    },
}

/// `arch-ml rubric`: якорные и динамические рубрики с LLM-судьёй.
pub(crate) async fn cmd_rubric(cfg: &Arc<Config>, cmd: RubricCmd) -> Result<()> {
    match cmd {
        RubricCmd::List => {
            let list = arch_harness::rubric::list(&cfg.paths.rubrics_dir())?;
            for r in &list {
                outln!(
                    "  {:<32} {} ({} критериев)",
                    r.name,
                    r.description,
                    r.criteria_count
                );
            }
        }
        RubricCmd::Run {
            rubric,
            target,
            model,
            dynamic_subject,
        } => {
            let registry = Arc::new(LlmRegistry::from_config(cfg)?);
            let judge = match &model {
                Some(name) => registry.get(name)?,
                None => registry.default(),
            };
            let text = std::fs::read_to_string(&target)
                .with_context(|| format!("чтение {}", target.display()))?;
            let rub = if let Some(subject) = dynamic_subject {
                let anchor_path = resolve_asset(&cfg.paths.rubrics_dir(), &rubric, "yaml");
                let anchor = arch_harness::rubric::load(&anchor_path).ok();
                arch_harness::rubric::generate_dynamic(&subject, anchor.as_ref(), judge.as_ref())
                    .await?
            } else {
                let path = resolve_asset(&cfg.paths.rubrics_dir(), &rubric, "yaml");
                arch_harness::rubric::load(&path)?
            };
            let report = arch_harness::rubric::evaluate(&rub, &text, judge.as_ref()).await?;
            outln!("{}", report.to_markdown());
            let out = cfg
                .paths
                .reports_dir
                .join(format!("rubric-{}-{}.md", rub.name, timestamp()));
            if let Some(parent) = out.parent() {
                std::fs::create_dir_all(parent).ok();
            }
            std::fs::write(&out, report.to_markdown())?;
            eprintln!("Отчёт: {}", out.display());
        }
    }
    Ok(())
}

/// `arch-ml bench`: архитектурные бенчмарки и golden-гейт качества судьи.
pub(crate) async fn cmd_bench(cfg: &Arc<Config>, cmd: BenchCmd) -> Result<()> {
    match cmd {
        BenchCmd::List => {
            for b in arch_harness::bench::list(&cfg.paths.benchmarks_dir())? {
                outln!("  {:<32} {} [{}]", b.name, b.description, b.tags.join(", "));
            }
        }
        BenchCmd::Run {
            name,
            model,
            golden,
            record,
        } => {
            if record.is_some() && !golden {
                anyhow::bail!("`bench run --record` применим только с --golden");
            }
            let registry = LlmRegistry::from_config(cfg)?;
            let provider = match &model {
                Some(m) => registry.get(m)?,
                None => registry.default(),
            };
            if golden {
                if name.is_some() {
                    anyhow::bail!("`bench run --golden` не совместим с именем бенчмарка");
                }
                // Регрессионный гейт качества судьи (ADR-004): согласие с
                // эталоном ниже порога — exit 1, как у `control check`.
                let report = arch_harness::bench::run_golden(
                    provider.as_ref(),
                    &cfg.paths.rubrics_dir(),
                    &cfg.paths.benchmarks_dir().join("golden"),
                    &cfg.judge,
                )
                .await?;
                outln!(
                    "Golden-прогон судьи '{}' (сэмплов на критерий: {}):",
                    report.judge_model,
                    cfg.judge.samples
                );
                for case in &report.cases {
                    outln!(
                        "  {:<32} MAE {:.2} ({} критериев)",
                        case.doc,
                        case.mae,
                        case.compared
                    );
                }
                // Механическая диагностика: MAE по критериям и length bias.
                outp!("{}", report.diagnostics_text());
                let passed = report.mae <= cfg.judge.golden_max_mae;
                outln!(
                    "Итог MAE: {:.2} по {} парам (порог {:.2}) — {}",
                    report.mae,
                    report.compared,
                    cfg.judge.golden_max_mae,
                    if passed { "PASS" } else { "FAIL" }
                );
                // Evidence-журнал (M-2): запись не зависит от исхода гейта —
                // история хранит и регрессии.
                if let Some(path) = &record {
                    let entry = arch_harness::bench::GoldenRecord::from_report(
                        &report,
                        chrono::Local::now().format("%Y-%m-%d").to_string(),
                    );
                    arch_harness::bench::record_golden(path, &entry)?;
                    outln!("Записано в журнал: {}", path.display());
                }
                if !passed {
                    std::process::exit(1);
                }
                return Ok(());
            }
            let Some(name) = name else {
                anyhow::bail!("укажите имя бенчмарка или флаг --golden");
            };
            let path = resolve_asset(&cfg.paths.benchmarks_dir(), &name, "yaml");
            let bench = arch_harness::bench::load(&path)?;
            let report = arch_harness::bench::run(
                &bench,
                provider.as_ref(),
                &cfg.paths.rubrics_dir(),
                &cfg.paths.reports_dir,
                &cfg.judge,
            )
            .await?;
            outln!(
                "Бенчмарк '{}': {:.2} (порог {:.2}) — {}",
                report.bench_name,
                report.rubric_report.weighted_total,
                bench.pass_threshold,
                if report.passed { "PASS" } else { "FAIL" }
            );
        }
        BenchCmd::GoldenHistory { path } => {
            let (records, broken) = arch_harness::bench::load_golden_history(&path)?;
            if broken > 0 {
                eprintln!("пропущено битых строк: {broken}");
            }
            if records.is_empty() {
                outln!("Журнал {} пуст.", path.display());
            } else {
                outp!("{}", arch_harness::bench::golden_history_markdown(&records));
            }
        }
        BenchCmd::HumanAgreement { golden_dir, humans } => {
            let report = arch_harness::bench::human_agreement(&golden_dir, &humans)?;
            for doc in &report.skipped {
                eprintln!("пропущен {doc}: нет человеческих анкет");
            }
            outp!("{}", report.to_markdown());
        }
    }
    Ok(())
}

/// `arch eval`: регрессионные eval-сьюты конфигурации харнесса (docs/evals.md).
///
/// Встроенный сьют (без `--suite`) герметичен: ассеты и конфиг разворачиваются
/// во временный каталог, живой `~/.arch-ml` не трогается — прогон зелёный
/// и в CI без `arch init`. Пользовательский `--suite` бежит против живой
/// установки. Гейт: pass-rate ниже `--gate` (дефолт 100%) — exit code 1.
pub(crate) async fn cmd_eval(cfg: &Arc<Config>, cmd: EvalCmd) -> Result<()> {
    match cmd {
        EvalCmd::Run {
            suite,
            gate,
            judge,
            model,
        } => {
            let gate_pct = gate.unwrap_or(100.0);
            if !(0.0..=100.0).contains(&gate_pct) {
                anyhow::bail!("--gate: ожидается процент 0..=100, получено {gate_pct}");
            }
            // tempdir держим живым до конца прогона (встроенный сьют).
            let mut _tmp = None;
            let (suite_dir, ctx) = if let Some(dir) = &suite {
                (dir.clone(), arch_harness::eval::SuiteContext::for_live()?)
            } else {
                let tmp = tempfile::tempdir().context("временный каталог встроенного сьюта")?;
                let home = arch_harness::eval::prepare_builtin_home(tmp.path())?;
                let ctx = arch_harness::eval::SuiteContext::for_builtin(tmp.path(), &home.config)?;
                _tmp = Some(tmp);
                (home.suite_dir, ctx)
            };
            // Слой судьи: реестр моделей строится только при --judge —
            // офлайн-прогон не требует ни ключей, ни сети.
            let provider = if judge {
                let registry = LlmRegistry::from_config(cfg)?;
                Some(match &model {
                    Some(name) => registry.get(name)?,
                    None => registry.default(),
                })
            } else {
                None
            };
            let rubrics_dir = cfg.paths.rubrics_dir();
            let judge_ctx = provider.as_ref().map(|p| arch_harness::eval::JudgeCtx {
                provider: p.as_ref(),
                cfg: &cfg.judge,
                rubrics_dir: &rubrics_dir,
            });
            let report =
                arch_harness::eval::run_suite(&suite_dir, &ctx, judge_ctx.as_ref(), gate_pct)
                    .await?;
            print!("{}", arch_harness::eval::render_text(&report));
            let out = arch_harness::eval::write_report(&report, &cfg.paths.evals_dir())?;
            eprintln!("Отчёт: {}", out.display());
            if !report.gate_passed {
                std::process::exit(1);
            }
        }
    }
    Ok(())
}

/// Резолвит имя ассета: точный путь, либо `<dir>/<name>`, либо `<dir>/<name>.<ext>`.
fn resolve_asset(dir: &std::path::Path, name: &str, ext: &str) -> PathBuf {
    let as_path = PathBuf::from(name);
    if as_path.is_file() {
        return as_path;
    }
    let in_dir = dir.join(name);
    if in_dir.is_file() {
        return in_dir;
    }
    dir.join(format!("{name}.{ext}"))
}

/// Метка времени для имён отчётов.
fn timestamp() -> String {
    chrono::Local::now().format("%Y%m%d-%H%M%S").to_string()
}
