//! Аудит флота worktree: детектор дублей и дрейфа копий архитектурного спайна.
//!
//! Источник модели — разбор реального кейса агентной разработки: флот из
//! 15 git-worktree, каждый несёт ПОЛНУЮ копию архитектурного спайна; 90%
//! файлов документации — точные дубли, копии измеримо дрейфуют (87/88/89
//! файлов на worktree). Целевая модель «5.2 + дельта-протокол»: спайн живёт
//! в одной копии в корне (SSOT), компонент несёт lean-дельту (~3 файла),
//! изменения спайна идут только дельтами `changes/<id>` (см. `crate::delta`).
//!
//! КОНТРАКТ (владелец: агент `control`):
//! - вход аудита — набор каталогов-worktree (позиционные пути CLI) либо
//!   репозиторий, чьи worktree перечисляются из `git worktree list --porcelain`;
//! - сканируется документация (`**/*.md|yaml|yml|json`) без служебных
//!   каталогов (`.git`, `target`, `node_modules`, `.arch-handoff`);
//! - метрики: всего файлов, точные дубли (есть идентичная копия того же пути
//!   в другом worktree), доля дублей, файлы-«ядро» (во ВСЕХ worktree), дрейф —
//!   пути с разными хэшами содержимого (канон = majority-хэш, при равенстве —
//!   численно меньший); сводка per-worktree «N/M файлов отличаются от канона»;
//! - семантика гейта: дрейф (хотя бы один путь с разными хэшами среди
//!   владельцев) — `has_drift`, CLI выходит кодом 1; опциональный порог
//!   доли дублей — второй независимый триггер exit 1.

use std::collections::hash_map::DefaultHasher;
use std::collections::{BTreeMap, HashMap};
use std::fmt::Write as _;
use std::hash::Hasher;
use std::path::{Path, PathBuf};

use async_trait::async_trait;
use serde::Serialize;
use serde_json::{Value, json};
use walkdir::WalkDir;

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Расширения сканируемой документации.
const DOC_EXTENSIONS: [&str; 4] = ["md", "yaml", "yml", "json"];

/// Служебные/производные каталоги, исключаемые из сканирования.
const SKIP_DIRS: [&str; 4] = [".git", "target", "node_modules", ".arch-handoff"];

/// Сколько самых расходящихся файлов показывать в текстовом отчёте.
const TOP_DRIFT: usize = 20;

/// Возраст последнего коммита (в полных днях), после которого аудит
/// рекомендует prune worktree.
const PRUNE_AGE_DAYS: u64 = 30;

/// Дефолтный порог «target пережил узел» в часах: непустой каталог сборки, в
/// котором дольше порога не менялся ни один файл/подкаталог, при отсутствии
/// свежей активности считается брошенным — кандидат на уборку.
///
/// Почему 24 ч: в живом прогоне сборка пишет в `target/` непрерывно (свежий
/// mtime), поэтому активный узел под правило не попадает; сутки простоя уже
/// означают, что дерево не строят, а суточная задержка не трогает worktree,
/// которые ещё могут понадобиться для ревью или добивания задачи. Существующий
/// `PRUNE_AGE_DAYS = 30` относится к удалению всего worktree — это тяжелее и
/// необратимее, чем уборка каталога сборки, поэтому порог для target намеренно
/// заметно короче. Настраивается через [`audit_with`] и параметр инструмента
/// `fleet_audit.stale_target_hours`.
pub const DEFAULT_STALE_TARGET_HOURS: u64 = 24;

/// Расхождение по одному относительному пути.
#[derive(Debug, Clone, Serialize)]
pub struct DriftEntry {
    /// Относительный путь файла.
    pub path: String,
    /// Метки worktree, чьё содержимое отличается от канона (majority).
    pub deviants: Vec<String>,
    /// Сколько worktree владеют этим файлом.
    pub owners: usize,
    /// Сколько различных версий содержимого.
    pub versions: usize,
}

/// Сводка по одному worktree: сколько его файлов отличаются от канона.
#[derive(Debug, Clone, Serialize)]
pub struct WorktreeSummary {
    /// Метка worktree (имя каталога).
    pub label: String,
    /// Каталог worktree (как передан).
    pub path: String,
    /// Всего отсканированных файлов.
    pub files: usize,
    /// Сравнимых файлов (путь встречается минимум в двух worktree).
    pub comparable: usize,
    /// Из них отличаются от канона пути.
    pub drifted: usize,
    /// Размер worktree на диске (`du -sh`, человекочитаемый); None — `du`
    /// недоступен или завершился ошибкой (в отчёте — «—»).
    pub disk_size: Option<String>,
    /// Возраст последнего коммита (`git log -1 --format=%cr`); None — не
    /// git-репозиторий, нет коммитов или git недоступен («—»).
    pub last_commit_age: Option<String>,
    /// Полных дней с последнего коммита (для рекомендации prune); None —
    /// возраст неизвестен (см. `last_commit_age`).
    pub age_days: Option<u64>,
    /// В worktree есть НЕПУСТОЙ каталог сборки `target/` (признак «target
    /// пережил узел»).
    pub target_nonempty: bool,
    /// Часов с последнего изменения внутри `target/` (максимум mtime файлов и
    /// подкаталогов); None — каталога нет или он пуст.
    pub target_age_hours: Option<u64>,
    /// `target/` непуст и не менялся дольше порога аудита — кандидат на
    /// уборку. Это WARN: [`FleetReport::has_drift`] и exit code аудита от него
    /// НЕ зависят (деградация гейта только сломала бы CI).
    pub stale_target: bool,
}

/// Отчёт аудита флота.
#[derive(Debug, Clone, Serialize)]
pub struct FleetReport {
    /// Метки worktree в порядке входа.
    pub worktrees: Vec<String>,
    /// Всего файлов-экземпляров (сумма по worktree).
    pub total_files: usize,
    /// Экземпляры, имеющие побайтово идентичную копию того же пути в другом
    /// worktree.
    pub duplicates: usize,
    /// Доля точных дублей, % (`duplicates / total_files` × 100).
    pub dup_pct: f64,
    /// Экземпляры без идентичной копии (уникальное содержимое).
    pub unique_content: usize,
    /// Различных относительных путей по флоту.
    pub unique_paths: usize,
    /// Путей-«ядро»: присутствуют во ВСЕХ worktree.
    pub core_files: usize,
    /// Расхождения содержимого (пути с разными хэшами среди владельцев),
    /// отсортированы: больше отступников → раньше, затем по пути.
    pub drift: Vec<DriftEntry>,
    /// Сводки по worktree.
    pub per_worktree: Vec<WorktreeSummary>,
    /// Порог (часы), по которому считался [`WorktreeSummary::stale_target`].
    pub stale_target_hours: u64,
    /// Метки worktree с брошенным `target/` — warn «кандидат на уборку».
    /// Не влияет на `has_drift` и exit code.
    pub stale_targets: Vec<String>,
    /// Обнаружен дрейф (гейт CI: exit 1).
    pub has_drift: bool,
}

/// Метка worktree: имя каталога, при коллизии/отсутствии — полный путь.
fn label_of(root: &Path) -> String {
    root.file_name().map_or_else(
        || root.display().to_string(),
        |n| n.to_string_lossy().into_owned(),
    )
}

/// Хэш содержимого файла (`DefaultHasher` — не криптография, только сравнение
/// версий внутри одного прогона).
fn hash_file(path: &Path) -> Result<u64> {
    let bytes = std::fs::read(path).map_err(|e| HarnessError::io(path, e))?;
    let mut h = DefaultHasher::new();
    h.write(&bytes);
    Ok(h.finish())
}

/// Сканирует один worktree: (относительный путь → хэш содержимого).
///
/// Непустой `include` сужает набор: путь обязан матчиться хотя бы одним
/// glob'ом (семантика `**`/`*`/`?` — как у fitness-правил, см.
/// `crate::control`).
fn scan_worktree(root: &Path, include: &[String]) -> Result<BTreeMap<String, u64>> {
    let mut out = BTreeMap::new();
    let walker = WalkDir::new(root).follow_links(false).into_iter();
    for entry in walker.filter_entry(|e| {
        let name = e.file_name().to_string_lossy();
        !(e.file_type().is_dir() && SKIP_DIRS.contains(&name.as_ref()))
    }) {
        let entry = entry.map_err(|e| {
            HarnessError::Control(format!("обход worktree {}: {e}", root.display()))
        })?;
        if !entry.file_type().is_file() {
            continue;
        }
        let ext_ok = entry
            .path()
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| DOC_EXTENSIONS.iter().any(|d| e.eq_ignore_ascii_case(d)));
        if !ext_ok {
            continue;
        }
        let rel = entry.path().strip_prefix(root).map_err(|e| {
            HarnessError::Control(format!(
                "относительный путь {}: {e}",
                entry.path().display()
            ))
        })?;
        let rel_str = rel.to_string_lossy().replace('\\', "/");
        if !include.is_empty()
            && !include
                .iter()
                .any(|g| crate::control::glob_matches(g, &rel_str))
        {
            continue;
        }
        out.insert(rel_str, hash_file(entry.path())?);
    }
    Ok(out)
}

/// Перечисляет worktree репозитория из `git worktree list --porcelain`
/// (все записи `worktree <path>`, включая основное дерево).
///
/// # Errors
/// `git` недоступен или вернул ненулевой код (не репозиторий).
pub fn worktrees_from_git(repo: &Path) -> Result<Vec<PathBuf>> {
    let out = std::process::Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["worktree", "list", "--porcelain"])
        .output()
        .map_err(|e| HarnessError::Control(format!("git не запустился: {e}")))?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(HarnessError::Control(format!(
            "git worktree list: {}",
            stderr.trim().chars().take(300).collect::<String>()
        )));
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let roots: Vec<PathBuf> = stdout
        .lines()
        .filter_map(|l| l.strip_prefix("worktree ").map(PathBuf::from))
        .collect();
    if roots.is_empty() {
        return Err(HarnessError::Control(format!(
            "в {} не найдено ни одного worktree",
            repo.display()
        )));
    }
    Ok(roots)
}

/// Размер каталога на диске (`du -sh`, человекочитаемая строка): None —
/// `du` недоступен или завершился ошибкой (в отчёте — «—»).
fn disk_size_human(root: &Path) -> Option<String> {
    let out = std::process::Command::new("du")
        .args(["-sh", "--"])
        .arg(root)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    stdout.split_whitespace().next().map(str::to_owned)
}

/// Возраст последнего коммита: (полных дней назад, относительная строка
/// `git log -1 --format=%cr`). None — не git-репозиторий, нет коммитов или
/// git недоступен (в отчёте — «—»).
fn last_commit_age(root: &Path) -> Option<(u64, String)> {
    let out = std::process::Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["log", "-1", "--format=%ct|%cr"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let (ts, rel) = stdout.trim().split_once('|')?;
    let ts: i64 = ts.trim().parse().ok()?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    // Коммит «из будущего» (часы сбиты) трактуем как нулевой возраст.
    #[allow(clippy::cast_sign_loss)]
    let days = now.saturating_sub(ts).max(0) as u64 / 86_400;
    Some((days, rel.trim().to_string()))
}

/// Unix-секунды самого свежего mtime среди ФАЙЛОВ каталога (подкаталоги и
/// симлинки не учитываются, за симлинками не идём). Каталоги исключены
/// намеренно: их mtime меняется при создании/удалении записей, а активность
/// сборки видна именно по времени записи файлов. None — файлов нет/нечитаемо.
fn newest_mtime_secs(root: &Path) -> Option<u64> {
    let mut newest: Option<u64> = None;
    for entry in WalkDir::new(root).follow_links(false).into_iter().flatten() {
        if !entry.file_type().is_file() {
            continue;
        }
        let Ok(md) = entry.metadata() else { continue };
        let Ok(modified) = md.modified() else {
            continue;
        };
        let Ok(since) = modified.duration_since(std::time::UNIX_EPOCH) else {
            continue; // mtime до эпохи — пропускаем
        };
        let secs = since.as_secs();
        newest = Some(newest.map_or(secs, |n| n.max(secs)));
    }
    newest
}

/// Полных часов с момента `unix_secs` (0 для метки «из будущего» — часы сбиты).
fn hours_since(unix_secs: u64) -> u64 {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    now.saturating_sub(unix_secs) / 3600
}

/// Метрики `target/` одного worktree: `(непуст, часов с последней правки,
/// брошен)`. Активность узла трактуется по свежести каталога сборки: пока
/// сборка пишет в `target/`, mtime свежий и правило молчит. Живой, но ничего
/// не пишущий узел (ревью без компиляции) от брошенного неотличим — поэтому
/// это warn с ручной проверкой, а не автоматическая уборка.
fn target_metrics(root: &Path, threshold_hours: u64) -> (bool, Option<u64>, bool) {
    let target = root.join("target");
    if !target.is_dir() {
        return (false, None, false);
    }
    let nonempty = std::fs::read_dir(&target).is_ok_and(|mut d| d.next().is_some());
    if !nonempty {
        return (false, None, false);
    }
    let age = newest_mtime_secs(&target).map(hours_since);
    let stale = age.is_some_and(|h| h > threshold_hours);
    (true, age, stale)
}

/// Аудит флота worktree: дубли, ядро, дрейф копий спайна.
///
/// # Errors
/// Пустой набор корней; корень не существует или не каталог; ошибка чтения.
pub fn audit(roots: &[PathBuf], include: &[String]) -> Result<FleetReport> {
    audit_with(roots, include, DEFAULT_STALE_TARGET_HOURS)
}

/// Аудит с настраиваемым порогом «target пережил узел» (часы). Правило
/// `stale_target` — WARN: оно попадает в [`FleetReport::stale_targets`] и
/// текстовый отчёт, но НЕ влияет на `has_drift` и exit code аудита. Порог N =
/// 0 помечает любой непустой target (кроме изменённого в текущий час).
///
/// # Errors
/// Пустой набор корней; корень не существует или не каталог; ошибка чтения.
pub fn audit_with(
    roots: &[PathBuf],
    include: &[String],
    stale_target_hours: u64,
) -> Result<FleetReport> {
    if roots.is_empty() {
        return Err(HarnessError::Control(
            "fleet audit: не заданы worktree — передайте пути позиционно или --repo <path>".into(),
        ));
    }
    for root in roots {
        if !root.is_dir() {
            return Err(HarnessError::Control(format!(
                "worktree недоступен или не каталог: {}",
                root.display()
            )));
        }
    }
    let labels: Vec<String> = roots.iter().map(|r| label_of(r)).collect();
    let scans: Vec<BTreeMap<String, u64>> = roots
        .iter()
        .map(|r| scan_worktree(r, include))
        .collect::<Result<_>>()?;

    // Путь → версии содержимого по worktree-владельцам.
    let mut by_path: BTreeMap<String, Vec<(usize, u64)>> = BTreeMap::new();
    for (idx, scan) in scans.iter().enumerate() {
        for (path, hash) in scan {
            by_path.entry(path.clone()).or_default().push((idx, *hash));
        }
    }

    let total_files: usize = scans.iter().map(BTreeMap::len).sum();
    // Дубль = экземпляр, у которого есть идентичная копия того же пути
    // в другом worktree (в синхронном флоте дубли — норма, их рост при
    // расползании копий — сигнал нарушения SSOT).
    let mut duplicates = 0usize;
    for owners in by_path.values() {
        let mut by_hash: HashMap<u64, usize> = HashMap::new();
        for (_, h) in owners {
            *by_hash.entry(*h).or_default() += 1;
        }
        for (_, h) in owners {
            if by_hash[h] > 1 {
                duplicates += 1;
            }
        }
    }
    let dup_pct = if total_files == 0 {
        0.0
    } else {
        duplicates as f64 / total_files as f64 * 100.0
    };
    let core_files = by_path
        .values()
        .filter(|owners| owners.len() == roots.len())
        .count();

    // Канон пути: majority-хэш; при равенстве голосов — численно меньший
    // (детерминированный выбор без привязки к порядку входа).
    let canonical_of = |owners: &[(usize, u64)]| -> u64 {
        let mut by_hash: BTreeMap<u64, usize> = BTreeMap::new();
        for (_, h) in owners {
            *by_hash.entry(*h).or_default() += 1;
        }
        by_hash
            .into_iter()
            .max_by(|a, b| a.1.cmp(&b.1).then(b.0.cmp(&a.0)))
            .map_or(0, |(h, _)| h)
    };

    let mut drift = Vec::new();
    let mut stale_targets: Vec<String> = Vec::new();
    let mut per_worktree: Vec<WorktreeSummary> = labels
        .iter()
        .enumerate()
        .map(|(idx, label)| {
            let (age_days, age_rel) =
                last_commit_age(&roots[idx]).map_or((None, None), |(d, r)| (Some(d), Some(r)));
            let (target_nonempty, target_age_hours, stale_target) =
                target_metrics(&roots[idx], stale_target_hours);
            if stale_target {
                stale_targets.push(format!(
                    "{}: непустой target старше {stale_target_hours} ч (возраст {} ч) — кандидат на уборку",
                    roots[idx].display(),
                    target_age_hours.map_or_else(|| "?".to_string(), |h| h.to_string()),
                ));
            }
            WorktreeSummary {
                label: label.clone(),
                path: roots[idx].display().to_string(),
                files: scans[idx].len(),
                comparable: 0,
                drifted: 0,
                disk_size: disk_size_human(&roots[idx]),
                last_commit_age: age_rel,
                age_days,
                target_nonempty,
                target_age_hours,
                stale_target,
            }
        })
        .collect();
    for (path, owners) in &by_path {
        if owners.len() < 2 {
            continue; // единоличный файл: канона нет, дрейфа нет
        }
        let canonical = canonical_of(owners);
        let versions: std::collections::BTreeSet<u64> = owners.iter().map(|(_, h)| *h).collect();
        let mut deviants = Vec::new();
        for (idx, h) in owners {
            per_worktree[*idx].comparable += 1;
            if *h != canonical {
                per_worktree[*idx].drifted += 1;
                deviants.push(labels[*idx].clone());
            }
        }
        if versions.len() > 1 {
            drift.push(DriftEntry {
                path: path.clone(),
                deviants,
                owners: owners.len(),
                versions: versions.len(),
            });
        }
    }
    drift.sort_by(|a, b| {
        b.deviants
            .len()
            .cmp(&a.deviants.len())
            .then_with(|| a.path.cmp(&b.path))
    });
    let has_drift = !drift.is_empty();

    Ok(FleetReport {
        worktrees: labels,
        total_files,
        duplicates,
        dup_pct,
        unique_content: total_files - duplicates,
        unique_paths: by_path.len(),
        core_files,
        drift,
        per_worktree,
        stale_target_hours,
        stale_targets,
        has_drift,
    })
}

/// Текстовый рендер отчёта: сводка, таблица per-worktree, топ расходящихся.
#[must_use]
pub fn render_text(report: &FleetReport) -> String {
    render_text_opts(report, false)
}

/// Рендер отчёта. `stable` — воспроизводимый вывод для золотых файлов и CI:
/// без волатильных колонок (размер на диске, возраст последнего коммита) —
/// остальное (дубли, ядро, дрейф, exit code) стабильно от прогона к прогону.
#[must_use]
pub fn render_text_opts(report: &FleetReport, stable: bool) -> String {
    let mut out = String::new();
    let _ = writeln!(
        out,
        "Аудит флота worktree ({} корн.):",
        report.worktrees.len()
    );
    let _ = writeln!(out, "  всего файлов:          {}", report.total_files);
    let _ = writeln!(
        out,
        "  точные дубли:          {} ({:.1}%)",
        report.duplicates, report.dup_pct
    );
    let _ = writeln!(out, "  без идентичной копии:  {}", report.unique_content);
    let _ = writeln!(out, "  различных путей:       {}", report.unique_paths);
    let _ = writeln!(out, "  ядро (во всех worktree): {}", report.core_files);
    let _ = writeln!(out, "  файлов с дрейфом:      {}", report.drift.len());
    out.push_str("\nПо worktree (отличаются от канона):\n");
    for w in &report.per_worktree {
        if stable {
            let _ = writeln!(
                out,
                "  {:<24} {}/{} файлов (всего {})",
                w.label, w.drifted, w.comparable, w.files,
            );
        } else {
            let _ = writeln!(
                out,
                "  {:<24} {}/{} файлов (всего {}) · на диске: {} · последний коммит: {}",
                w.label,
                w.drifted,
                w.comparable,
                w.files,
                w.disk_size.as_deref().unwrap_or("—"),
                w.last_commit_age.as_deref().unwrap_or("—"),
            );
        }
    }
    // Рекомендация prune: worktree с известным возрастом старше PRUNE_AGE_DAYS.
    let prune_candidates: Vec<&WorktreeSummary> = report
        .per_worktree
        .iter()
        .filter(|w| w.age_days.is_some_and(|d| d > PRUNE_AGE_DAYS))
        .collect();
    if !prune_candidates.is_empty() {
        let _ = writeln!(
            out,
            "\nРекомендация prune (worktree старше {PRUNE_AGE_DAYS} дней):"
        );
        for w in prune_candidates {
            let _ = writeln!(
                out,
                "  {} — {} дн., на диске {}: git worktree remove {}",
                w.label,
                w.age_days.unwrap_or(0),
                w.disk_size.as_deref().unwrap_or("—"),
                w.path,
            );
        }
    }
    // WARN (не fail): непустой target пережил узел. Возраст берётся по самому
    // свежему mtime внутри target/: живой билд постоянно его обновляет, поэтому
    // «старый» target означает, что узел не активен (или давно завершён и
    // забыт). Это рекомендация человеку, а не блокирующее условие: has_drift и
    // exit code аудита не меняются. Колонка волатильна, поэтому в `stable`
    // (золотые файлы/CI) блок не печатается.
    if !stable && !report.stale_targets.is_empty() {
        let _ = writeln!(
            out,
            "\nWARN: target пережил узел — непустой target старше {} ч и без активности \
             (кандидат на уборку; вердикт не меняется):",
            report.stale_target_hours
        );
        for note in &report.stale_targets {
            let _ = writeln!(out, "  {note}");
        }
        let _ = writeln!(
            out,
            "  убрать: rm -rf <worktree>/target — при повторном прогоне узла target пересоберётся \
             (accept/drop снимают target вместе с worktree)"
        );
    }
    if !report.drift.is_empty() {
        let _ = writeln!(out, "\nТоп расходящихся файлов (до {TOP_DRIFT}):");
        for d in report.drift.iter().take(TOP_DRIFT) {
            let _ = writeln!(
                out,
                "  {} — версий: {}, владельцев: {}; отступники: {}",
                d.path,
                d.versions,
                d.owners,
                d.deviants.join(", ")
            );
        }
        if report.drift.len() > TOP_DRIFT {
            let _ = writeln!(out, "  … и ещё {}", report.drift.len() - TOP_DRIFT);
        }
    }
    let _ = writeln!(
        out,
        "\nИтог: {}",
        if report.has_drift {
            "DRIFT — копии спайна разошлись (exit 1)"
        } else {
            "PASS — дрейфа нет"
        }
    );
    out
}

/// Инструмент агента: `fleet_audit` — SSOT-аудит флота worktree.
pub struct FleetAuditTool;

#[async_trait]
impl Tool for FleetAuditTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "fleet_audit".into(),
            description: "Аудит флота worktree (модель «5.2 + дельта-протокол»): находит точные \
                дубли документации и дрейф копий архитектурного спайна по набору worktree. \
                Для каждого расходящегося файла канон — majority-версия, отступники называются \
                поимённо. Сканируются **/*.md|yaml|yml|json (без .git/target/node_modules/.arch-handoff). \
                Дрейф хотя бы одного файла — сигнал нарушения SSOT: спайн обязан жить в одной \
                копии, изменения — только дельтами changes/<id> (arch-ml delta …)."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Каталоги-worktree (относительно cwd или абсолютные)"
                    },
                    "repo": {
                        "type": "string",
                        "description": "Git-репозиторий: worktree перечисляются из `git worktree list` (добавляются к paths)"
                    },
                    "include": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Сузить сканирование glob'ами, напр. [\"model/**\"]"
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "json"],
                        "description": "Формат сводки (по умолчанию text)"
                    },
                    "stale_target_hours": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Порог WARN «target пережил узел» в часах: непустой target, \
                            не обновлявшийся дольше порога, помечается кандидатом на уборку \
                            (по умолчанию 24; вердикт PASS/DRIFT не меняется)"
                    }
                }
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let mut roots: Vec<PathBuf> = args
            .get("paths")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(|s| ctx.resolve(s))
                    .collect()
            })
            .unwrap_or_default();
        if let Some(repo) = args.get("repo").and_then(Value::as_str) {
            match worktrees_from_git(&ctx.resolve(repo)) {
                Ok(list) => roots.extend(list),
                Err(e) => return Ok(ToolOutput::err(format!("fleet_audit: {e}"))),
            }
        }
        let include: Vec<String> = args
            .get("include")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let format = args.get("format").and_then(Value::as_str).unwrap_or("text");
        let stale_target_hours = args
            .get("stale_target_hours")
            .and_then(Value::as_u64)
            .unwrap_or(DEFAULT_STALE_TARGET_HOURS);
        let report = match audit_with(&roots, &include, stale_target_hours) {
            Ok(r) => r,
            Err(e) => return Ok(ToolOutput::err(format!("fleet_audit: {e}"))),
        };
        let body = match format {
            "json" => match serde_json::to_string_pretty(&report) {
                Ok(j) => j,
                Err(e) => return Ok(ToolOutput::err(format!("fleet_audit: {e}"))),
            },
            "text" => render_text(&report),
            other => {
                return Ok(ToolOutput::err(format!(
                    "fleet_audit: неизвестный формат '{other}' (допустимы: text, json)"
                )));
            }
        };
        // Дрейф — находка аудита, а не сбой инструмента: is_error не ставим,
        // но вердикт DRIFT в тексте модель обязана увидеть и отреагировать.
        Ok(ToolOutput::ok(body))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Пишет файл, создавая родительские каталоги.
    fn write_file(path: &Path, text: &str) {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("mkdir");
        }
        std::fs::write(path, text).expect("write");
    }

    /// Флот-фикстура из трёх worktree: общий спайн (3 файла), уникальный
    /// файл на worktree; в `wt-c` CONSTRAINTS.yaml намеренно изменён.
    fn make_fleet(base: &Path) -> [PathBuf; 3] {
        let spine = "# Spine\n\n## AD-1: Единый протокол\n\n**Rule:** дельты.\n";
        let adr = "# ADR-001\n\nРешение: gRPC.\n";
        let constraints = "rules:\n  - name: c1\n    type: file_exists\n    path: README.md\n";
        let drifted = "rules:\n  - name: c1\n    type: file_exists\n    path: OTHER.md\n";
        let mut roots = Vec::new();
        for (name, unique) in [("wt-a", "a"), ("wt-b", "b"), ("wt-c", "c")] {
            let root = base.join(name);
            write_file(&root.join("ARCHITECTURE-SPINE.md"), spine);
            write_file(&root.join("model/adr/ADR-001.md"), adr);
            let c = if name == "wt-c" { drifted } else { constraints };
            write_file(&root.join("CONSTRAINTS.yaml"), c);
            write_file(
                &root.join(format!("model/components/comp-{unique}.md")),
                &format!("компонент {unique}\n"),
            );
            // Служебные каталоги и недокументация вне сканирования.
            write_file(&root.join("target/junk.md"), "мусор\n");
            write_file(&root.join(".arch-handoff/TASK.md"), "задача\n");
            write_file(&root.join("src/main.rs"), "fn main() {}\n");
            roots.push(root);
        }
        [roots[0].clone(), roots[1].clone(), roots[2].clone()]
    }

    #[test]
    fn audit_counts_dupes_core_and_detects_drift() {
        let tmp = tempfile::tempdir().expect("tmp");
        let roots = make_fleet(tmp.path());
        let report = audit(&roots, &[]).expect("audit");
        // По 4 документа на worktree (target/.arch-handoff/rs пропущены).
        assert_eq!(report.total_files, 12, "{report:?}");
        // Дубли: SPINE ×3 + ADR ×3 + CONSTRAINTS (wt-a/wt-b) ×2 = 8.
        assert_eq!(report.duplicates, 8, "{report:?}");
        assert!(report.dup_pct > 60.0, "{}", report.dup_pct);
        assert_eq!(report.unique_content, 4);
        // Ядро: SPINE, ADR, CONSTRAINTS — во всех трёх.
        assert_eq!(report.core_files, 3);
        // Дрейф ровно одного файла; канон = majority (wt-a/wt-b), отступник wt-c.
        assert!(report.has_drift);
        assert_eq!(report.drift.len(), 1, "{:?}", report.drift);
        let d = &report.drift[0];
        assert_eq!(d.path, "CONSTRAINTS.yaml");
        assert_eq!(d.deviants, vec!["wt-c".to_string()]);
        assert_eq!(d.owners, 3);
        assert_eq!(d.versions, 2);
        // Сводка per-worktree.
        let wc = report
            .per_worktree
            .iter()
            .find(|w| w.label == "wt-c")
            .expect("wt-c");
        assert_eq!(wc.files, 4);
        assert_eq!(wc.comparable, 3, "SPINE/ADR/CONSTRAINTS сравнимы");
        assert_eq!(wc.drifted, 1);
        let wa = &report.per_worktree[0];
        assert_eq!(wa.drifted, 0);
        // Рендер называет отступника и вердикт.
        let text = render_text(&report);
        assert!(text.contains("CONSTRAINTS.yaml"), "{text}");
        assert!(text.contains("wt-c"), "{text}");
        assert!(text.contains("DRIFT"), "{text}");
    }

    #[test]
    fn clean_fleet_has_no_drift() {
        let tmp = tempfile::tempdir().expect("tmp");
        let a = tmp.path().join("a");
        let b = tmp.path().join("b");
        for root in [&a, &b] {
            write_file(&root.join("ARCHITECTURE-SPINE.md"), "# Spine\n");
        }
        let report = audit(&[a, b], &[]).expect("audit");
        assert!(!report.has_drift, "{report:?}");
        assert_eq!(report.duplicates, 2);
        assert_eq!(report.core_files, 1);
        assert!(render_text(&report).contains("PASS"));
    }

    #[test]
    fn include_narrows_scan() {
        let tmp = tempfile::tempdir().expect("tmp");
        let roots = make_fleet(tmp.path());
        // Только model/**: CONSTRAINTS.yaml (дрейф) вне выборки.
        let report = audit(&roots, &["model/**".to_string()]).expect("audit");
        assert_eq!(report.total_files, 6, "{report:?}");
        assert!(!report.has_drift, "дрейф вне include: {report:?}");
        assert_eq!(report.core_files, 1, "только ADR-001 общий");
    }

    #[test]
    fn audit_rejects_empty_and_missing_roots() {
        let err = audit(&[], &[]).expect_err("пустой набор");
        assert!(err.to_string().contains("--repo"), "{err}");
        let tmp = tempfile::tempdir().expect("tmp");
        let err = audit(&[tmp.path().join("ghost")], &[]).expect_err("нет каталога");
        assert!(err.to_string().contains("ghost"), "{err}");
        // Файл вместо каталога — тоже отказ.
        let f = tmp.path().join("file.md");
        write_file(&f, "x\n");
        assert!(audit(&[f], &[]).is_err());
    }

    #[test]
    fn worktrees_from_git_lists_all_worktrees() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        std::fs::create_dir(&repo).expect("mkdir");
        let git = |args: &[&str]| {
            let out = std::process::Command::new("git")
                .arg("-C")
                .arg(&repo)
                .args(args)
                .env("GIT_AUTHOR_NAME", "t")
                .env("GIT_AUTHOR_EMAIL", "t@t")
                .env("GIT_COMMITTER_NAME", "t")
                .env("GIT_COMMITTER_EMAIL", "t@t")
                .output()
                .expect("git");
            assert!(out.status.success(), "git {args:?}");
        };
        git(&["init", "-q"]);
        std::fs::write(repo.join("README.md"), "base\n").expect("write");
        git(&["add", "."]);
        git(&["commit", "-q", "-m", "init"]);
        let wt = tmp.path().join("wt-extra");
        git(&[
            "worktree",
            "add",
            "-q",
            "-b",
            "arch/extra",
            &wt.to_string_lossy(),
        ]);
        let roots = worktrees_from_git(&repo).expect("list");
        assert_eq!(roots.len(), 2, "{roots:?}");
        assert!(roots.contains(&wt));
        // Не репозиторий — внятная ошибка.
        let err = worktrees_from_git(tmp.path()).expect_err("не git");
        assert!(err.to_string().contains("git worktree list"), "{err}");
    }

    #[tokio::test]
    async fn fleet_audit_tool_text_and_errors() {
        let tmp = tempfile::tempdir().expect("tmp");
        make_fleet(tmp.path());
        let tool = FleetAuditTool;
        assert_eq!(tool.spec().name, "fleet_audit");
        let ctx = ToolContext::new(
            tmp.path().to_path_buf(),
            std::sync::Arc::new(crate::config::Config::default()),
        );
        // Относительные пути от cwd.
        let out = tool
            .call(json!({"paths": ["wt-a", "wt-b", "wt-c"]}), &ctx)
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("DRIFT"), "{}", out.content);
        assert!(out.content.contains("wt-c"), "{}", out.content);
        // JSON-формат.
        let out = tool
            .call(
                json!({"paths": ["wt-a", "wt-b", "wt-c"], "format": "json"}),
                &ctx,
            )
            .await
            .expect("call json");
        let v: Value = serde_json::from_str(&out.content).expect("json");
        assert_eq!(v["has_drift"], true);
        assert_eq!(v["total_files"], 12);
        assert_eq!(v["stale_target_hours"], DEFAULT_STALE_TARGET_HOURS);
        // Пустой вызов — ошибка с подсказкой, не паника.
        let out = tool.call(json!({}), &ctx).await.expect("call empty");
        assert!(out.is_error);
        assert!(out.content.contains("--repo"), "{}", out.content);
        // Неизвестный формат — ошибка.
        let out = tool
            .call(json!({"paths": ["wt-a"], "format": "xml"}), &ctx)
            .await
            .expect("call fmt");
        assert!(
            out.is_error && out.content.contains("xml"),
            "{}",
            out.content
        );
    }

    /// Коммит в git-репозиторий с явной датой (детерминированный возраст).
    fn git_commit_dated(repo: &Path, date: &str) {
        let envs = [
            ("GIT_AUTHOR_NAME", "t"),
            ("GIT_AUTHOR_EMAIL", "t@t"),
            ("GIT_COMMITTER_NAME", "t"),
            ("GIT_COMMITTER_EMAIL", "t@t"),
            ("GIT_AUTHOR_DATE", date),
            ("GIT_COMMITTER_DATE", date),
        ];
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["init", "-q"])
            .output()
            .expect("git init");
        assert!(out.status.success());
        std::fs::write(repo.join("README.md"), "base\n").expect("write");
        for args in [
            ["add", "."].as_slice(),
            ["commit", "-q", "-m", "init"].as_slice(),
        ] {
            let out = std::process::Command::new("git")
                .arg("-C")
                .arg(repo)
                .args(args)
                .envs(envs)
                .output()
                .expect("git");
            assert!(out.status.success(), "git {args:?}");
        }
    }

    #[test]
    fn disk_size_and_commit_age_degrade_gracefully() {
        let tmp = tempfile::tempdir().expect("tmp");
        // Не-git каталог: возраст «—», размер — строка от du (du есть в Linux CI).
        let plain = tmp.path().join("plain");
        write_file(&plain.join("doc.md"), "# Документ\n");
        assert!(last_commit_age(&plain).is_none());
        assert!(disk_size_human(&plain).is_some(), "du должен отработать");
        let report = audit(std::slice::from_ref(&plain), &[]).expect("audit");
        let w = &report.per_worktree[0];
        assert!(w.last_commit_age.is_none());
        assert!(w.age_days.is_none());
        assert!(w.disk_size.is_some());
        let text = render_text(&report);
        assert!(text.contains("последний коммит: —"), "{text}");
        assert!(!text.contains("Рекомендация prune"), "{text}");
        // stable-рендер: волатильные колонки отсутствуют, числовое ядро на месте.
        let stable = render_text_opts(&report, true);
        assert!(!stable.contains("на диске:"), "{stable}");
        assert!(!stable.contains("последний коммит:"), "{stable}");
        assert!(stable.contains("точные дубли:"), "{stable}");
        assert!(stable.contains("файлов (всего"), "{stable}");
        // Несуществующий путь: обе ветки деградации — None.
        let ghost = tmp.path().join("ghost");
        assert!(last_commit_age(&ghost).is_none());
        assert!(disk_size_human(&ghost).is_none());
    }

    #[test]
    fn fresh_repo_has_age_but_no_prune_recommendation() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        std::fs::create_dir(&repo).expect("mkdir");
        write_file(&repo.join("ARCHITECTURE-SPINE.md"), "# Spine\n");
        git_commit_dated(&repo, "2030-01-01T00:00:00 +0000");
        // Дата «из будущего» — возраст 0 дней (часы сбиты → не в минус).
        let (days, rel) = last_commit_age(&repo).expect("git-репозиторий");
        assert_eq!(days, 0);
        assert!(!rel.is_empty());
        let report = audit(&[repo], &[]).expect("audit");
        let text = render_text(&report);
        assert!(!text.contains("Рекомендация prune"), "{text}");
    }

    #[test]
    fn stale_worktree_gets_prune_recommendation() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("old-wt");
        std::fs::create_dir(&repo).expect("mkdir");
        write_file(&repo.join("ARCHITECTURE-SPINE.md"), "# Spine\n");
        // Коммит 60 дней назад — детерминированный возраст через дату коммита.
        let date = {
            let secs = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("часы")
                .as_secs()
                - 60 * 86_400;
            format!("@{secs} +0000")
        };
        git_commit_dated(&repo, &date);
        let (days, _) = last_commit_age(&repo).expect("git-репозиторий");
        assert!(days >= 59, "days: {days}");
        let report = audit(&[repo], &[]).expect("audit");
        assert_eq!(report.per_worktree[0].age_days, Some(days));
        let text = render_text(&report);
        assert!(text.contains("Рекомендация prune"), "{text}");
        assert!(text.contains("git worktree remove"), "{text}");
        assert!(text.contains("old-wt"), "{text}");
    }

    /// Непустой `<root>/target/` с mtime, откатанным на `age_hours` назад
    /// (0 — оставить «сейчас»). Через `File::set_modified`, без внешних утилит.
    fn write_target(root: &Path, age_hours: u64) {
        let f = root.join("target/debug/arch-ml");
        write_file(&f, "bin\n");
        if age_hours == 0 {
            return;
        }
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("часы")
            .as_secs()
            .saturating_sub(age_hours * 3600);
        let when = std::time::UNIX_EPOCH + std::time::Duration::from_secs(secs);
        std::fs::File::options()
            .write(true)
            .open(&f)
            .expect("open target")
            .set_modified(when)
            .expect("set_modified");
    }

    /// Похожий на рабочий worktree: спайн-файл, чтобы аудит не ругался на пустоту.
    fn make_wt(base: &Path, name: &str) -> PathBuf {
        let root = base.join(name);
        write_file(&root.join("ARCHITECTURE-SPINE.md"), "# Spine\n");
        root
    }

    #[test]
    fn stale_target_flagged_fresh_and_active_not() {
        let tmp = tempfile::tempdir().expect("tmp");
        let old = make_wt(tmp.path(), "old");
        let fresh = make_wt(tmp.path(), "fresh");
        let none = make_wt(tmp.path(), "none");
        write_target(&old, 100); // 100 ч без правок — узел мёртв
        write_target(&fresh, 0); // сборка «прямо сейчас» — узел активен
        // none — target/ вообще нет

        let report = audit(&[old.clone(), fresh.clone(), none.clone()], &[]).expect("audit");
        let by = |name: &str| {
            report
                .per_worktree
                .iter()
                .find(|w| w.label == name)
                .expect("worktree")
        };
        assert!(by("old").target_nonempty);
        assert!(by("old").stale_target, "{:?}", by("old"));
        assert!(by("old").target_age_hours.is_some_and(|h| h >= 99));
        assert!(by("fresh").target_nonempty);
        assert!(!by("fresh").stale_target, "{:?}", by("fresh"));
        assert!(!by("none").target_nonempty);
        assert!(!by("none").stale_target);

        assert_eq!(report.stale_targets.len(), 1, "{:?}", report.stale_targets);
        assert!(
            report.stale_targets[0].contains("old"),
            "{:?}",
            report.stale_targets
        );
        // WARN не превращается в FAIL: дрейфа нет, вердикт PASS.
        assert!(!report.has_drift, "{report:?}");

        let text = render_text(&report);
        assert!(text.contains("target пережил узел"), "{text}");
        assert!(
            text.contains(&DEFAULT_STALE_TARGET_HOURS.to_string()),
            "{text}"
        );
        assert!(text.contains("rm -rf"), "{text}");
        assert!(text.contains("PASS"), "{text}");
        assert!(!text.contains("DRIFT"), "{text}");
    }

    #[test]
    fn stale_target_threshold_is_configurable() {
        let tmp = tempfile::tempdir().expect("tmp");
        let wt = make_wt(tmp.path(), "wt");
        write_target(&wt, 10); // 10 ч

        // Дефолт 24 ч: 10 ч — ещё «свежий».
        let def = audit(std::slice::from_ref(&wt), &[]).expect("audit");
        assert_eq!(def.stale_target_hours, DEFAULT_STALE_TARGET_HOURS);
        assert!(def.stale_targets.is_empty(), "{:?}", def.stale_targets);
        assert!(!def.per_worktree[0].stale_target);

        // Порог 1 ч: тот же target — уже кандидат.
        let tight = audit_with(std::slice::from_ref(&wt), &[], 1).expect("audit_with");
        assert_eq!(tight.stale_target_hours, 1);
        assert_eq!(tight.stale_targets.len(), 1, "{:?}", tight.stale_targets);
        assert!(tight.per_worktree[0].stale_target);
        let text = render_text(&tight);
        assert!(text.contains("старше 1 ч"), "{text}");

        // Порог 0 ч по-прежнему не трогает вердикт.
        assert!(!tight.has_drift);
    }

    #[test]
    fn empty_or_missing_target_is_not_stale() {
        let tmp = tempfile::tempdir().expect("tmp");
        let missing = make_wt(tmp.path(), "missing");
        let empty = make_wt(tmp.path(), "empty");
        std::fs::create_dir_all(empty.join("target")).expect("mkdir target");
        // Пустой каталог target/ (без файлов) — не «пережил узел».
        let report = audit(&[missing.clone(), empty.clone()], &[]).expect("audit");
        assert!(
            report.stale_targets.is_empty(),
            "{:?}",
            report.stale_targets
        );
        for w in &report.per_worktree {
            assert!(!w.target_nonempty, "{w:?}");
            assert!(!w.stale_target, "{w:?}");
        }
        let text = render_text(&report);
        assert!(!text.contains("target пережил узел"), "{text}");
    }

    #[test]
    fn stable_render_omits_volatile_stale_target_section() {
        let tmp = tempfile::tempdir().expect("tmp");
        let wt = make_wt(tmp.path(), "wt");
        write_target(&wt, 100);
        let report = audit_with(std::slice::from_ref(&wt), &[], 1).expect("audit_with");
        assert_eq!(report.stale_targets.len(), 1);
        let stable = render_text_opts(&report, true);
        assert!(!stable.contains("target пережил узел"), "{stable}");
    }
}
