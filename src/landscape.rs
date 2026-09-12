//! Ландшафт систем (EA-3, ADR-037): агрегация моделей `model/` набора проектов.
//!
//! Ответ на замечание ревью «спайн живёт внутри проекта; нет связи с
//! ландшафтом и реестром систем» — в той же механике, что реестр ADR
//! (ADR-036): read-only агрегация файлов по ROOT + непосредственным
//! подкаталогам (ADR-033: реестры — агрегация файлов, не сервис).
//! Дедупликация — по НОРМАЛИЗОВАННОМУ ИМЕНИ системы, БЕЗ глобального
//! пространства ID (глобальные ID инвазивны: потребовали бы переписывания
//! существующих `model/` всех проектов).
//!
//! Команда `arch-ml model landscape <ROOT> [--mermaid]`:
//! - реестр систем: каноническое имя (lowercase, схлопывание пробелов и
//!   дефисов) → проекты/id/статусы всех вхождений (дедуп-витрина);
//! - находки: `id-divergence` (одна система — разные id в разных проектах),
//!   `status-conflict` (расхождение статусов), `dangling-ref` (связь
//!   `depends_on`/`implements`/`affects` на id, отсутствующий во всём
//!   наборе), `cross-project-link` (связь на систему другого проекта —
//!   показываемый положительный факт, не ошибка);
//! - топ-5 систем по связности (вход+исход); `--mermaid` — `graph TD`
//!   канонических систем.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use crate::error::{HarnessError, Result};
use crate::model::{EntityKind, LinkKind};

/// Каталоги, которые не считаются проектами при сканировании `ROOT`.
const SKIP_DIRS: [&str; 3] = [".git", "target", "node_modules"];

/// Виды связей, учитываемых ландшафтом (`verified_by` — про правила, не про
/// ландшафт систем).
const LINK_KINDS: [LinkKind; 3] = [LinkKind::DependsOn, LinkKind::Implements, LinkKind::Affects];

/// Вхождение системы: сущность конкретного проекта.
#[derive(Debug, Clone)]
pub struct Occurrence {
    /// Проект (имя подкаталога ROOT; сам ROOT — имя его каталога).
    pub project: String,
    /// Идентификатор сущности (`SYS-001`, `INT-007`).
    pub id: String,
    /// Заголовок сущности (как записан в проекте).
    pub title: String,
    /// Статус сущности.
    pub status: String,
    /// Тип сущности (`SYS` или `INT`).
    pub kind: EntityKind,
}

/// Каноническая система ландшафта (дедуп по нормализованному имени).
#[derive(Debug, Clone)]
pub struct LandscapeSystem {
    /// Каноническая форма имени (ключ дедупликации).
    pub canonical: String,
    /// Представительное имя (title первого вхождения).
    pub display: String,
    /// Вхождения (сортировка: проект, id).
    pub occurrences: Vec<Occurrence>,
}

/// Находка ландшафта.
#[derive(Debug, Clone)]
pub struct LandscapeFinding {
    /// Тип: `id-divergence` | `status-conflict` | `dangling-ref` |
    /// `cross-project-link`.
    pub kind: String,
    /// Описание.
    pub message: String,
}

/// Отчёт ландшафта систем.
#[derive(Debug)]
pub struct LandscapeReport {
    /// Сканированный корень.
    pub root: PathBuf,
    /// Реестр систем (сортировка по каноническому имени).
    pub systems: Vec<LandscapeSystem>,
    /// Находки.
    pub findings: Vec<LandscapeFinding>,
    /// Рёбра ландшафта: (канон. источник, канон. цель), отсортированные и
    /// дедуплицированные (для mermaid и топа связности).
    pub edges: Vec<(String, String)>,
    /// Топ связности: (представительное имя, число связей вход+исход).
    pub top: Vec<(String, usize)>,
}

/// Нормализация имени системы: lowercase, дефисы → пробелы, схлопывание
/// пробелов. Ключ дедупликации («Процессинг» == «процессинг» ==
/// «Процессинг  СБОЛ» по пробелам).
fn canonical_name(title: &str) -> String {
    title
        .trim()
        .to_lowercase()
        .replace('-', " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Имя проекта по каталогу (для ROOT — имя самого каталога).
fn project_name(dir: &Path) -> String {
    dir.canonicalize()
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_else(|| ".".to_string())
}

/// Собирает сущности проекта с меткой проекта; `None` — нет `model/`
/// (проект молча пропускается).
fn load_project(dir: &Path) -> Result<Option<(String, crate::model::Model)>> {
    let model_dir = dir.join("model");
    if !model_dir.is_dir() {
        return Ok(None);
    }
    let model = crate::model::load_model(&model_dir)?;
    Ok(Some((project_name(dir), model)))
}

/// Строит ландшафт систем по `ROOT`: сам каталог + непосредственные
/// подкаталоги с `model/`.
///
/// # Errors
/// `ROOT` не каталог; ни в самом `ROOT`, ни в подкаталогах нет ни одного
/// `model/` — понятная ошибка; модель проекта не разбирается.
pub fn build_landscape(root: &Path) -> Result<LandscapeReport> {
    if !root.is_dir() {
        return Err(HarnessError::Model(format!(
            "ландшафт: корень недоступен: {}",
            root.display()
        )));
    }

    let mut projects: Vec<(String, crate::model::Model)> = Vec::new();
    if let Some(p) = load_project(root)? {
        projects.push(p);
    }
    let mut dirs: Vec<PathBuf> = Vec::new();
    let rd = std::fs::read_dir(root).map_err(|e| HarnessError::io(root, e))?;
    for entry in rd {
        let entry = entry.map_err(|e| HarnessError::io(root, e))?;
        let p = entry.path();
        if !p.is_dir() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if SKIP_DIRS.contains(&name.as_str()) {
            continue;
        }
        dirs.push(p);
    }
    dirs.sort();
    for d in &dirs {
        if let Some(p) = load_project(d)? {
            projects.push(p);
        }
    }

    if projects.is_empty() {
        return Err(HarnessError::Model(format!(
            "ландшафт: ни в {}, ни в его непосредственных подкаталогах не найдено \
             каталога model/ — нечего агрегировать",
            root.display()
        )));
    }

    // Глобальный индекс id → (проект, сущность) по всем проектам набора
    // (разрешение связей — сквозное: dangling/cross-project определяются
    // против ВСЕГО набора, а не локальной модели).
    let mut global: BTreeMap<&str, Vec<(&str, &crate::model::Entity)>> = BTreeMap::new();
    for (project, model) in &projects {
        for e in &model.entities {
            global
                .entry(e.id.as_str())
                .or_default()
                .push((project.as_str(), e));
        }
    }

    // Реестр систем: только SYS/INT, дедуп по каноническому имени.
    let mut by_canonical: BTreeMap<String, Vec<Occurrence>> = BTreeMap::new();
    for (project, model) in &projects {
        for e in &model.entities {
            if !matches!(e.kind, EntityKind::Sys | EntityKind::Int) {
                continue;
            }
            let canonical = canonical_name(&e.title);
            if canonical.is_empty() {
                continue;
            }
            by_canonical.entry(canonical).or_default().push(Occurrence {
                project: project.clone(),
                id: e.id.clone(),
                title: e.title.trim().to_string(),
                status: e.status.clone(),
                kind: e.kind,
            });
        }
    }
    let mut systems: Vec<LandscapeSystem> = by_canonical
        .into_iter()
        .map(|(canonical, mut occurrences)| {
            occurrences.sort_by(|a, b| (&a.project, &a.id).cmp(&(&b.project, &b.id)));
            // Представительное имя — заголовок первого вхождения.
            let display = occurrences
                .first()
                .map_or_else(|| canonical.clone(), |o| o.title.clone());
            LandscapeSystem {
                canonical,
                display,
                occurrences,
            }
        })
        .collect();
    systems.sort_by(|a, b| a.canonical.cmp(&b.canonical));
    let canonical_of: BTreeMap<&str, &str> = systems
        .iter()
        .flat_map(|s| {
            s.occurrences
                .iter()
                .map(move |o| (o.id.as_str(), s.canonical.as_str()))
        })
        .collect();

    let mut findings = Vec::new();

    // id-divergence / status-conflict по реестру.
    for s in &systems {
        let projects: BTreeSet<&str> = s.occurrences.iter().map(|o| o.project.as_str()).collect();
        let ids: BTreeSet<&str> = s.occurrences.iter().map(|o| o.id.as_str()).collect();
        let whereabouts = s
            .occurrences
            .iter()
            .map(|o| format!("{} {} ({})", o.project, o.id, o.status))
            .collect::<Vec<_>>()
            .join(", ");
        if projects.len() > 1 && ids.len() > 1 {
            findings.push(LandscapeFinding {
                kind: "id-divergence".to_string(),
                message: format!(
                    "«{}» — разные id в разных проектах: {whereabouts}",
                    s.display
                ),
            });
        }
        let statuses: BTreeSet<String> = s
            .occurrences
            .iter()
            .map(|o| o.status.trim().to_lowercase())
            .collect();
        if statuses.len() > 1 {
            findings.push(LandscapeFinding {
                kind: "status-conflict".to_string(),
                message: format!("«{}» — статусы расходятся: {whereabouts}", s.display),
            });
        }
    }

    // Связи: dangling-ref / cross-project-link + рёбра ландшафта.
    let mut edges: BTreeSet<(String, String)> = BTreeSet::new();
    let mut seen_dangling: BTreeSet<(String, String, String)> = BTreeSet::new();
    let mut seen_cross: BTreeSet<(String, String, String)> = BTreeSet::new();
    for (project, model) in &projects {
        for e in &model.entities {
            for kind in LINK_KINDS {
                for target in e.link_targets(kind) {
                    match global.get(target.as_str()) {
                        None => {
                            // dangling-ref: цели нет ни в одном проекте набора.
                            if seen_dangling.insert((
                                e.id.clone(),
                                kind.field_name().to_string(),
                                target.clone(),
                            )) {
                                findings.push(LandscapeFinding {
                                    kind: "dangling-ref".to_string(),
                                    message: format!(
                                        "{project} {} -[{}]-> {target}: цель отсутствует \
                                         во всех моделях набора",
                                        e.id,
                                        kind.field_name()
                                    ),
                                });
                            }
                        }
                        Some(targets) => {
                            for (t_project, t_entity) in targets {
                                // cross-project-link: цель — система (SYS/INT)
                                // ДРУГОГО проекта. Положительный факт, не ошибка.
                                if *t_project == project.as_str()
                                    || !matches!(t_entity.kind, EntityKind::Sys | EntityKind::Int)
                                {
                                    continue;
                                }
                                if seen_cross.insert((
                                    e.id.clone(),
                                    target.clone(),
                                    (*t_project).to_string(),
                                )) {
                                    findings.push(LandscapeFinding {
                                        kind: "cross-project-link".to_string(),
                                        message: format!(
                                            "{project} {} -[{}]-> {target} (проект {t_project}, «{}»)",
                                            e.id,
                                            kind.field_name(),
                                            t_entity.title.trim()
                                        ),
                                    });
                                }
                            }
                        }
                    }
                    // Ребро ландшафта — только между системами (SYS/INT),
                    // разрешёнными в канонические имена.
                    let (Some(src_canonical), Some(dst_canonical)) = (
                        canonical_of
                            .get(e.id.as_str())
                            .filter(|_| matches!(e.kind, EntityKind::Sys | EntityKind::Int)),
                        canonical_of.get(target.as_str()),
                    ) else {
                        continue;
                    };
                    if src_canonical != dst_canonical {
                        edges.insert(((*src_canonical).to_string(), (*dst_canonical).to_string()));
                    }
                }
            }
        }
    }

    // Топ связности: вход+исход по рёбрам ландшафта.
    let mut degree: BTreeMap<&str, usize> = BTreeMap::new();
    for (src, dst) in &edges {
        *degree.entry(src.as_str()).or_default() += 1;
        *degree.entry(dst.as_str()).or_default() += 1;
    }
    let mut ranked: Vec<(String, String, usize)> = systems
        .iter()
        .map(|s| {
            (
                s.canonical.clone(),
                s.display.clone(),
                degree.get(s.canonical.as_str()).copied().unwrap_or(0),
            )
        })
        .collect();
    ranked.sort_by(|a, b| b.2.cmp(&a.2).then(a.0.cmp(&b.0)));
    let top: Vec<(String, usize)> = ranked
        .into_iter()
        .take(5)
        .map(|(_, display, n)| (display, n))
        .collect();

    Ok(LandscapeReport {
        root: root.to_path_buf(),
        systems,
        findings,
        edges: edges.into_iter().collect(),
        top,
    })
}

/// Рендер отчёта в markdown: реестр систем, находки, топ связности.
#[must_use]
pub fn render_markdown(report: &LandscapeReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# Ландшафт систем: {}\n", report.root.display());
    let _ = writeln!(out, "## Реестр систем\n");
    let _ = writeln!(out, "| Система | Проекты и ID | Статусы |");
    let _ = writeln!(out, "|---|---|---|");
    for s in &report.systems {
        let ids = s
            .occurrences
            .iter()
            .map(|o| format!("{}: {}", o.project, o.id))
            .collect::<Vec<_>>()
            .join("; ");
        let statuses: BTreeSet<&str> = s.occurrences.iter().map(|o| o.status.as_str()).collect();
        let _ = writeln!(
            out,
            "| {} | {} | {} |",
            s.display,
            ids,
            statuses.into_iter().collect::<Vec<_>>().join(", ")
        );
    }
    let _ = writeln!(out, "\n## Находки\n");
    if report.findings.is_empty() {
        let _ = writeln!(out, "- нет");
    } else {
        for f in &report.findings {
            let _ = writeln!(out, "- [{}] {}", f.kind, f.message);
        }
    }
    let _ = writeln!(out, "\n## Топ связности\n");
    for (i, (name, n)) in report.top.iter().enumerate() {
        let _ = writeln!(out, "{}. {name} — {n} связей", i + 1);
    }
    let _ = write!(
        out,
        "\nИтого: {} систем, {} связей, {} находок",
        report.systems.len(),
        report.edges.len(),
        report.findings.len()
    );
    out
}

/// Санитизация метки узла mermaid: кавычки и скобки — в безопасные символы.
fn mermaid_label(text: &str) -> String {
    text.replace(['"', '[', ']', '|'], "'")
}

/// Рендер ландшафта в mermaid `graph TD`: узлы — канонические системы
/// (при id-divergence — с перечнем проектов), рёбра — связи. Идентификаторы
/// узлов — санитизированные `s1..sN` в порядке канонических имён.
#[must_use]
pub fn render_mermaid(report: &LandscapeReport) -> String {
    let mut out = String::from("graph TD\n");
    let mut node_ids: BTreeMap<&str, String> = BTreeMap::new();
    for (i, s) in report.systems.iter().enumerate() {
        let node = format!("s{}", i + 1);
        node_ids.insert(s.canonical.as_str(), node.clone());
        let projects: BTreeSet<&str> = s.occurrences.iter().map(|o| o.project.as_str()).collect();
        let ids: BTreeSet<&str> = s.occurrences.iter().map(|o| o.id.as_str()).collect();
        // Метка проекта — только при id-divergence (разные id у одной системы).
        let label = if projects.len() > 1 && ids.len() > 1 {
            format!(
                "{} ({})",
                s.display,
                projects.into_iter().collect::<Vec<_>>().join(", ")
            )
        } else {
            s.display.clone()
        };
        let _ = writeln!(out, "    {node}[\"{}\"]", mermaid_label(&label));
    }
    for (src, dst) in &report.edges {
        let (Some(a), Some(b)) = (node_ids.get(src.as_str()), node_ids.get(dst.as_str())) else {
            continue;
        };
        let _ = writeln!(out, "    {a} --> {b}");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Пишет файл в каталог и возвращает его путь.
    fn write_file(dir: &Path, name: &str, content: &str) -> PathBuf {
        let p = dir.join(name);
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(&p, content).unwrap();
        p
    }

    /// Фикстура: p1 и p2 с model/ (общая система «Процессинг» под разными
    /// id и статусами, ссылка на несуществующий SYS-099, кросс-проектная
    /// связь p2 → SYS-001 из p1), p3 без model/ (молча пропускается).
    fn fixture(dir: &Path) -> PathBuf {
        let root = dir.join("root");
        write_file(
            &root,
            "p1/model/SYS-001-processing.md",
            "---\nid: SYS-001\ntype: sys\ntitle: Процессинг\nstatus: adopted\n---\nСистема.\n",
        );
        write_file(
            &root,
            "p1/model/INT-002-gateway.md",
            "---\nid: INT-002\ntype: int\ntitle: Шлюз\nstatus: adopted\ndepends_on: [SYS-001, SYS-099]\n---\nИнтеграция.\n",
        );
        write_file(
            &root,
            "p2/model/INT-007-processing.md",
            "---\nid: INT-007\ntype: int\ntitle: процессинг\nstatus: proposed\naffects: [SYS-001]\n---\nТа же система в другом проекте.\n",
        );
        write_file(
            &root,
            "p2/model/SYS-003-fraud.md",
            "---\nid: SYS-003\ntype: sys\ntitle: Фрод-монитор\nstatus: adopted\ndepends_on: [INT-007]\n---\nСистема.\n",
        );
        // CMP в model/ — не система ландшафта, в реестр не попадает.
        write_file(
            &root,
            "p2/model/CMP-001-core.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: Ядро\nstatus: adopted\n---\nКомпонент.\n",
        );
        write_file(&root, "p3/README.md", "проект без модели\n");
        root
    }

    #[test]
    fn landscape_builds_deduped_registry_and_findings() {
        let dir = tempfile::tempdir().unwrap();
        let root = fixture(dir.path());
        let report = build_landscape(&root).unwrap();

        // Реестр: 3 системы; «Процессинг» — ОДНА строка с двумя вхождениями.
        assert_eq!(report.systems.len(), 3, "{:?}", report.systems);
        let proc = report
            .systems
            .iter()
            .find(|s| s.canonical == "процессинг")
            .unwrap();
        assert_eq!(proc.occurrences.len(), 2, "{proc:?}");
        let ids: BTreeSet<&str> = proc.occurrences.iter().map(|o| o.id.as_str()).collect();
        assert_eq!(ids, BTreeSet::from(["SYS-001", "INT-007"]));
        // CMP не система; p3 без model/ молча пропущен.
        assert!(
            report.systems.iter().all(|s| s.canonical != "ядро"),
            "{:?}",
            report.systems
        );
        assert!(
            report
                .systems
                .iter()
                .flat_map(|s| &s.occurrences)
                .all(|o| o.project != "p3")
        );

        // Находки: все четыре типа.
        let kinds = |k: &str| {
            report
                .findings
                .iter()
                .filter(|f| f.kind == k)
                .collect::<Vec<_>>()
        };
        let divergence = kinds("id-divergence");
        assert_eq!(divergence.len(), 1, "{divergence:?}");
        assert!(
            divergence[0].message.contains("Процессинг"),
            "{divergence:?}"
        );
        let conflicts = kinds("status-conflict");
        assert_eq!(conflicts.len(), 1, "{conflicts:?}");
        assert!(conflicts[0].message.contains("adopted"), "{conflicts:?}");
        let dangling = kinds("dangling-ref");
        assert_eq!(dangling.len(), 1, "{dangling:?}");
        assert!(dangling[0].message.contains("SYS-099"), "{dangling:?}");
        let cross = kinds("cross-project-link");
        assert_eq!(cross.len(), 1, "{cross:?}");
        assert!(
            cross[0].message.contains("INT-007") && cross[0].message.contains("p1"),
            "{cross:?}"
        );

        // Топ связности: процессинг (2 вход: от шлюза и фрод-монитора).
        assert_eq!(report.top[0].0, "Процессинг", "{:?}", report.top);
        assert_eq!(report.top[0].1, 2, "{:?}", report.top);
        assert_eq!(report.top[1].0, "Фрод-монитор", "{:?}", report.top);
        assert_eq!(report.top[1].1, 1, "{:?}", report.top);
        // Рёбра: шлюз → процессинг, фрод-монитор → процессинг (саморебро
        // INT-007→SYS-001 схлопнуто дедупликацией).
        assert_eq!(
            report.edges,
            vec![
                ("фрод монитор".to_string(), "процессинг".to_string()),
                ("шлюз".to_string(), "процессинг".to_string())
            ],
            "{:?}",
            report.edges
        );
    }

    #[test]
    fn landscape_mermaid_shape() {
        let dir = tempfile::tempdir().unwrap();
        let root = fixture(dir.path());
        let report = build_landscape(&root).unwrap();
        let mm = render_mermaid(&report);
        assert!(mm.starts_with("graph TD\n"), "{mm}");
        // Узлы санитизированы (sN) в порядке канонических имён:
        // s1 — процессинг, s2 — фрод монитор, s3 — шлюз.
        assert!(mm.contains("s2[\"Фрод-монитор\"]"), "{mm}");
        assert!(
            mm.contains("s1[\"Процессинг (p1, p2)\"]"),
            "метка проектов при id-divergence: {mm}"
        );
        // Рёбра: фрод-монитор → процессинг, шлюз → процессинг.
        assert!(mm.contains("s2 --> s1"), "{mm}");
        assert!(mm.contains("s3 --> s1"), "{mm}");
        // Невалидных символов mermaid в метках нет.
        assert!(!mm.contains("[\"Шлюз ["), "{mm}");
    }

    #[test]
    fn landscape_markdown_sections() {
        let dir = tempfile::tempdir().unwrap();
        let root = fixture(dir.path());
        let report = build_landscape(&root).unwrap();
        let md = render_markdown(&report);
        assert!(md.contains("## Реестр систем"), "{md}");
        assert!(
            md.contains("| Процессинг | p1: SYS-001; p2: INT-007 | adopted, proposed |"),
            "{md}"
        );
        assert!(md.contains("[id-divergence]"), "{md}");
        assert!(md.contains("[dangling-ref]"), "{md}");
        assert!(md.contains("[cross-project-link]"), "{md}");
        assert!(md.contains("## Топ связности"), "{md}");
        assert!(md.contains("1. Процессинг — 2 связей"), "{md}");
    }

    #[test]
    fn landscape_without_any_model_is_clear_error() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("root");
        write_file(&root, "p3/README.md", "без моделей\n");
        let err = build_landscape(&root).unwrap_err();
        assert!(err.to_string().contains("model/"), "{err}");
        assert!(build_landscape(&root.join("missing")).is_err());
    }
}
