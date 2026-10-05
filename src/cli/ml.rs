//! ML-контур: реестр артефактов (`weights`, манифест `artifacts.yaml`),
//! датасет-карточки (`data-card`), метрики eval-траекторий (`trajectory`:
//! `success_rate`, `ci95`, `pass@k`), локальные GPU (`resources`: инвентарь, живая
//! детекция, «куда гнать»), provenance экспериментов (`experiment`, ось 3),
//! детерминированный роутер экспертных моделей Ariadna (`ariadna`, ось 4).

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use clap::Subcommand;

use arch_harness::config::Config;

/// Подкоманды `arch-ml weights` (реестр артефактов, `artifacts.yaml`).
#[derive(Subcommand)]
pub(crate) enum WeightsCmd {
    /// Объявленные артефакты манифеста (без обращения к диску).
    List {
        /// Манифест артефактов (по умолчанию `artifacts.yaml`).
        #[arg(long)]
        manifest: Option<PathBuf>,
    },
    /// Механическая проверка манифеста против файловой системы.
    Verify {
        /// Манифест артефактов (по умолчанию `artifacts.yaml`).
        #[arg(long)]
        manifest: Option<PathBuf>,
        /// Вывести отчёт в JSON вместо текста.
        #[arg(long)]
        json: bool,
    },
}

/// Подкоманды `arch-ml data-card` (датасет-карточки).
#[derive(Subcommand)]
pub(crate) enum DataCardCmd {
    /// Проверить карточки: объявленные поля, пробелы, противоречия.
    Check {
        /// Файл или каталог карточек (по умолчанию текущий каталог).
        #[arg(long)]
        cards: Option<PathBuf>,
        /// Путь к датасету, к которому относятся проверяемые карточки.
        /// Без флага противоречия не проверяются: существование файла по
        /// неизвестному пути не доказать, ложно-красный недопустим.
        #[arg(long)]
        dataset: Option<PathBuf>,
        /// Считать пробелы и противоречия провалом (ненулевой код возврата).
        #[arg(long)]
        strict: bool,
    },
}

/// Подкоманды `arch-ml trajectory` (eval траекторий).
#[derive(Subcommand)]
pub(crate) enum TrajectoryCmd {
    /// Посчитать метрики эпизодов из файла траекторий.
    Metrics {
        /// Файл траекторий (JSONL).
        #[arg(long)]
        input: PathBuf,
        /// Формат входа: `auto` | `episode-jsonl` | `session-journal` | `selfplay`.
        #[arg(long, default_value = "auto")]
        format: String,
        /// `k` для несмещённой оценки pass@k.
        #[arg(long, default_value_t = 1)]
        k: usize,
        /// Вывести метрики в JSON вместо текста.
        #[arg(long)]
        json: bool,
    },
}

/// Подкоманды `arch-ml resources` (локальные GPU, `[[gpus.local]]`).
#[derive(Subcommand)]
pub(crate) enum ResourcesCmd {
    /// Объявленные локальные GPU из конфига + реестр известных устройств.
    List,
    /// Живая детекция `nvidia-smi` против объявленного инвентаря.
    Check,
    /// Куда гнать модель: локальное железо ($0) первым, облако — замыкающим.
    Recommend {
        /// Параметры модели, млрд.
        #[arg(long)]
        params_b: f64,
        /// dtype весов (bf16/fp16/fp8/fp32).
        #[arg(long, default_value = "bf16")]
        dtype: String,
        /// VRAM облачного GPU для сравнения, ГБ.
        #[arg(long, default_value_t = 40.0)]
        cloud_vram: f64,
        /// Учитывать живое свободное место (локальный nvidia-smi / SSH), а не
        /// только паспортный VRAM из реестра.
        #[arg(long)]
        live: bool,
    },
}

/// Подкоманды `arch-ml experiment` (provenance, ось 3).
#[derive(Subcommand)]
pub(crate) enum ExperimentCmd {
    /// Записать provenance эксперимента из TOML-спеки (+ git-коммит и драйвер).
    Record {
        /// TOML-файл спецификации эксперимента.
        spec: PathBuf,
        /// Репозиторий для git-коммита (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
    /// Список записанных экспериментов.
    List,
    /// Рецепт воспроизведения + сверка git-дрейфа.
    Reproduce {
        /// Id эксперимента (из `experiment list`).
        id: String,
        /// Репозиторий для сверки git-коммита (по умолчанию — текущий каталог).
        #[arg(long)]
        repo: Option<PathBuf>,
    },
}

/// `arch-ml resources`: инвентарь, живая детекция и выбор ресурса локальных GPU.
pub(crate) fn cmd_resources(cfg: &Config, cmd: ResourcesCmd) -> Result<()> {
    match cmd {
        ResourcesCmd::List => {
            outln!(
                "Локальные GPU ([[gpus.local]]) — объявлено {}:",
                cfg.gpus.local.len()
            );
            if cfg.gpus.local.is_empty() {
                outln!("  (пусто — добавьте [[gpus.local]] в config.toml)");
            }
            for g in &cfg.gpus.local {
                let spec = arch_harness::gpu::resolve_device(&g.device);
                let vram = g.vram_gb.or(spec.map(|d| d.vram_gb)).unwrap_or(0.0);
                let known = spec.map_or("неизвестно", |d| d.key);
                let ssh = g
                    .ssh
                    .as_deref()
                    .map_or(String::new(), |a| format!(", ssh {a}"));
                outln!(
                    "  {:<16} {:<12} {vram:>5.0} ГБ  (device = \"{}\"{ssh})",
                    g.name,
                    known,
                    g.device
                );
            }
            outln!();
            outln!(
                "Реестр устройств ({}):",
                arch_harness::gpu::known_devices().len()
            );
            for d in arch_harness::gpu::known_devices() {
                let kind = if d.unified { "unified" } else { "VRAM" };
                outln!(
                    "  {:<14} {:>5.0} ГБ ({kind})  [{}]",
                    d.key,
                    d.vram_gb,
                    d.source
                );
            }
        }
        ResourcesCmd::Check => {
            outln!("Объявлено локальных GPU: {}", cfg.gpus.local.len());
            // Локальная детекция (GPU на этой машине — без ssh-алиаса).
            let has_local = cfg.gpus.local.iter().any(|g| g.ssh.is_none());
            if has_local {
                match arch_harness::gpu::detect() {
                    Ok(gpus) => {
                        outln!("nvidia-smi (эта машина) увидел {} GPU:", gpus.len());
                        outp!("{}", arch_harness::gpu::render_detected(&gpus));
                    }
                    Err(e) => outln!("локальная детекция недоступна: {e:#}"),
                }
            }
            // Удалённые GPU по SSH-алиасу (read-only, не грузит GPU): unified-память
            // (GB10) читается из /proc/meminfo, дискретная — из nvidia-smi memory.*.
            for g in cfg.gpus.local.iter().filter(|g| g.ssh.is_some()) {
                let alias = g.ssh.as_deref().unwrap_or("?");
                let unified =
                    arch_harness::gpu::resolve_device(&g.device).is_some_and(|d| d.unified);
                outln!("{} (ssh {alias}):", g.name);
                let result = if unified {
                    arch_harness::gpu::detect_remote_unified(alias)
                        .map(|m| arch_harness::gpu::render_unified(&m))
                } else {
                    arch_harness::gpu::detect_remote(alias)
                        .map(|gpus| arch_harness::gpu::render_detected(&gpus))
                };
                match result {
                    Ok(text) => outp!("{text}"),
                    Err(e) => outln!("  недоступна: {e:#}"),
                }
            }
        }
        ResourcesCmd::Recommend {
            params_b,
            dtype,
            cloud_vram,
            live,
        } => {
            if params_b <= 0.0 {
                anyhow::bail!("params_b должен быть > 0");
            }
            let needed = arch_harness::gpu::required_vram_gb(params_b, &dtype);
            outln!("Куда гнать модель {params_b}B ({dtype}) — нужно ≈{needed:.1} ГБ:");
            for g in &cfg.gpus.local {
                let spec = arch_harness::gpu::resolve_device(&g.device);
                let total = g.vram_gb.or(spec.map(|d| d.vram_gb)).unwrap_or(0.0);
                let ssh = g
                    .ssh
                    .as_deref()
                    .map_or_else(|| "локально".to_string(), |a| format!("ssh {a}"));
                let (icon, note) = if live {
                    match arch_harness::gpu::live_free_gb(g) {
                        Some(f) if f >= needed => {
                            ("✓", format!("свободно {f:.0} ГБ — влезает сейчас"))
                        }
                        Some(f) => ("✗", format!("свободно лишь {f:.0} ГБ — не влезает сейчас")),
                        None => ("?", "недоступна — свободно неизвестно".to_string()),
                    }
                } else if total >= needed {
                    ("✓", "влезает ($0)".to_string())
                } else {
                    ("✗", "не влезает".to_string())
                };
                outln!(
                    "  {icon} local:{:<14} {total:>5.0} ГБ всего ({ssh}) — {note}",
                    g.name
                );
            }
            let cicon = if cloud_vram >= needed { "✓" } else { "✗" };
            outln!("  {cicon} cloud           {cloud_vram:>5.0} ГБ — платно, $/час");
        }
    }
    Ok(())
}

/// `arch-ml experiment`: provenance эксперимента (record/list/reproduce).
pub(crate) fn cmd_experiment(cfg: &Config, cmd: ExperimentCmd) -> Result<()> {
    let state_dir = &cfg.paths.state_dir;
    match cmd {
        ExperimentCmd::Record { spec, repo } => {
            let spec = arch_harness::preflight::parse_spec(&spec)?;
            if spec.name.trim().is_empty() {
                anyhow::bail!("в спеке нужно имя (name = \"...\")");
            }
            let repo = repo
                .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
            // SSH-алиас удалённого GPU: по device из спеки находим объявленный
            // локальный ресурс в [gpus.local] — его драйвер пишем как remote.
            let remote_ssh = spec.device.as_deref().and_then(|dev| {
                cfg.gpus
                    .local
                    .iter()
                    .find(|g| g.device.eq_ignore_ascii_case(dev))
                    .and_then(|g| g.ssh.clone())
            });
            let p = arch_harness::provenance::capture(&spec, &repo, remote_ssh.as_deref());
            let path = arch_harness::provenance::record(&p, state_dir)?;
            outln!("Provenance записан: {}", path.display());
            outp!("{}", arch_harness::provenance::render(&p));
        }
        ExperimentCmd::List => {
            let ids = arch_harness::provenance::list(state_dir)?;
            if ids.is_empty() {
                outln!("нет записей (запишите: arch-ml experiment record SPEC.toml)");
            } else {
                outln!("Эксперименты ({}):", ids.len());
                for id in ids {
                    outln!("  {id}");
                }
            }
        }
        ExperimentCmd::Reproduce { id, repo } => {
            let repo = repo
                .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
            outp!(
                "{}",
                arch_harness::provenance::reproduce(&id, state_dir, &repo)?
            );
        }
    }
    Ok(())
}

/// `arch-ml ariadna`: детерминированный роутинг вопроса к модели Ariadna.
pub(crate) fn cmd_ariadna(cfg: &Config, question: &str) -> Result<()> {
    let index = &cfg.concept.index;
    let db = if index.as_os_str().is_empty() {
        None
    } else {
        Some(arch_harness::concept::ConceptDb::open_cached(index)?)
    };
    let r = arch_harness::ariadna::route(question, db.as_deref());
    outln!("{} — {}", r.expert.as_str(), r.reason);
    Ok(())
}

/// `arch-ml weights`: реестр артефактов ML-контура (`artifacts.yaml`).
pub(crate) fn cmd_weights(_cfg: &Config, cmd: WeightsCmd) -> Result<()> {
    match cmd {
        WeightsCmd::List { manifest } => {
            let path =
                manifest.unwrap_or_else(|| PathBuf::from(arch_harness::weights::DEFAULT_MANIFEST));
            let m = arch_harness::weights::load_manifest(&path)?;
            outln!("Артефактов: {} ({})", m.artifacts.len(), path.display());
            for a in &m.artifacts {
                outln!(
                    "  {:<24} {:<10} {}",
                    a.id,
                    artifact_kind_str(a.kind),
                    a.path.display()
                );
            }
        }
        WeightsCmd::Verify { manifest, json } => {
            let path =
                manifest.unwrap_or_else(|| PathBuf::from(arch_harness::weights::DEFAULT_MANIFEST));
            let m = arch_harness::weights::load_manifest(&path)?;
            let root = std::env::current_dir().context("текущий каталог")?;
            let report = arch_harness::weights::verify(&m, &root);
            if json {
                let (ok, warn, fail) = report.counts();
                let v = serde_json::json!({
                    "ok": ok,
                    "warn": warn,
                    "fail": fail,
                    "checks": report.checks.iter().map(|c| serde_json::json!({
                        "id": c.id,
                        "status": check_status_str(c.status),
                        "detail": c.detail,
                    })).collect::<Vec<_>>(),
                });
                outln!("{}", serde_json::to_string_pretty(&v)?);
            } else {
                outp!("{}", arch_harness::weights::render_report(&report));
            }
            if report.has_failures() {
                return Err(anyhow::anyhow!("проверка артефактов: есть провалы"));
            }
        }
    }
    Ok(())
}

/// Строковая метка роли артефакта.
fn artifact_kind_str(kind: Option<arch_harness::weights::ArtifactKind>) -> &'static str {
    match kind {
        Some(arch_harness::weights::ArtifactKind::Weights) => "weights",
        Some(arch_harness::weights::ArtifactKind::Dataset) => "dataset",
        Some(arch_harness::weights::ArtifactKind::Tokenizer) => "tokenizer",
        Some(arch_harness::weights::ArtifactKind::Other) => "other",
        None => "-",
    }
}

/// Строковая метка статуса проверки артефакта.
fn check_status_str(status: arch_harness::weights::CheckStatus) -> &'static str {
    match status {
        arch_harness::weights::CheckStatus::Ok => "ok",
        arch_harness::weights::CheckStatus::Warn => "warn",
        arch_harness::weights::CheckStatus::Fail => "fail",
    }
}

/// `arch-ml data-card`: датасет-карточки.
///
/// Семантика `check` зафиксирована ядром `dataset_card`:
/// - пустое обязательное поле — декларация-пробел, НЕ провал (карточек на
///   диске может не быть, ложно-красный недопустим);
/// - противоречия (`sha256`/`records` объявлены, а файла датасета нет)
///   проверяются только при явном `--dataset`: без пути «файла нет» не
///   доказать, поэтому без флага проблема не выдумывается.
///
/// `--strict` сохраняет смысл: и пробелы, и противоречия дают ненулевой код
/// возврата с разбивкой в тексте ошибки.
pub(crate) fn cmd_data_card(_cfg: &Config, cmd: DataCardCmd) -> Result<()> {
    match cmd {
        DataCardCmd::Check {
            cards,
            dataset,
            strict,
        } => {
            let root = cards.unwrap_or_else(|| PathBuf::from("."));
            let files = collect_card_files(&root)?;
            if files.is_empty() {
                outln!("Карточек не найдено: {}", root.display());
            }
            let mut gaps = 0usize;
            let mut problems = 0usize;
            for p in &files {
                let card = arch_harness::dataset_card::load_card(p)?;
                let mut report = arch_harness::dataset_card::validate(&card, dataset.as_deref());
                // В отчёте CLI остаётся путь карточки (семантика ядра:
                // `report.path` — путь известного вызывающему файла).
                report.path.clone_from(p);
                gaps += report.missing.len();
                problems += report.problems.len();
                outp!("{}", arch_harness::dataset_card::render_report(&report));
            }
            outln!(
                "Карточек: {} (пробелов: {gaps}, проблем: {problems})",
                files.len()
            );
            if strict && (gaps > 0 || problems > 0) {
                return Err(anyhow::anyhow!(
                    "data-card --strict: пробелов {gaps}, проблем {problems}"
                ));
            }
        }
    }
    Ok(())
}

/// Собрать YAML-файлы карточек из каталога (рекурсивно) или один файл.
fn collect_card_files(root: &Path) -> Result<Vec<PathBuf>> {
    if root.is_file() {
        return Ok(vec![root.to_path_buf()]);
    }
    let mut out = Vec::new();
    for entry in walkdir::WalkDir::new(root) {
        let entry = entry.with_context(|| format!("обход {}", root.display()))?;
        if !entry.file_type().is_file() {
            continue;
        }
        let is_yaml = entry
            .path()
            .extension()
            .is_some_and(|e| e.eq_ignore_ascii_case("yaml") || e.eq_ignore_ascii_case("yml"));
        if is_yaml {
            out.push(entry.path().to_path_buf());
        }
    }
    Ok(out)
}

/// `arch-ml trajectory`: метрики eval-траекторий.
pub(crate) fn cmd_trajectory(_cfg: &Config, cmd: TrajectoryCmd) -> Result<()> {
    match cmd {
        TrajectoryCmd::Metrics {
            input,
            format,
            k,
            json,
        } => {
            let fmt = if format == "auto" {
                detect_input_format(&input)?
            } else {
                parse_format_name(&format).ok_or_else(|| {
                    anyhow::anyhow!(
                        "неизвестный формат «{format}» (ожидалось auto|episode-jsonl|session-journal|selfplay)"
                    )
                })?
            };
            let episodes = arch_harness::trajectory::parse_episodes(&input, fmt)?;
            let m = arch_harness::trajectory::compute(&episodes, k);
            if json {
                let v = serde_json::json!({
                    "episodes": m.episodes,
                    "tasks": m.tasks,
                    "labeled": m.labeled,
                    "successes": m.successes,
                    "success_rate": m.success_rate,
                    "ci95": m.ci95,
                    "pass_at_k": m.pass_at_k,
                    "k": m.k,
                    "mean_steps": m.mean_steps,
                    "mean_tokens": m.mean_tokens,
                    "invalid_tool_calls": m.invalid_tool_calls,
                });
                outln!("{}", serde_json::to_string_pretty(&v)?);
            } else {
                outp!("{}", arch_harness::trajectory::render_metrics(&m));
            }
        }
    }
    Ok(())
}

/// Определить формат траекторий по первой значимой строке файла.
fn detect_input_format(path: &Path) -> Result<arch_harness::trajectory::InputFormat> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("чтение {}", path.display()))?;
    for line in text.lines() {
        if line.trim().is_empty() || line.trim_start().starts_with('#') {
            continue;
        }
        return arch_harness::trajectory::detect_format(line)
            .ok_or_else(|| anyhow::anyhow!("не удалось определить формат {}", path.display()));
    }
    Err(anyhow::anyhow!("файл пуст: {}", path.display()))
}

/// Разобрать имя формата траекторий (кроме `auto`).
fn parse_format_name(name: &str) -> Option<arch_harness::trajectory::InputFormat> {
    match name {
        "episode-jsonl" => Some(arch_harness::trajectory::InputFormat::EpisodeJsonl),
        "session-journal" => Some(arch_harness::trajectory::InputFormat::SessionJournal),
        "selfplay" => Some(arch_harness::trajectory::InputFormat::Selfplay),
        _ => None,
    }
}
