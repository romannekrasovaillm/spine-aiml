//! Глобальный реестр ADR (PR-2, ADR-036): агрегация решений по набору проектов.
//!
//! Ответ на замечание ревью «нет глобального реестра ADR» в рамках позиции
//! single-user (ADR-033): реестр — АГРЕГАЦИЯ ФАЙЛОВ, а не сервис. Команда
//! `arch-ml adr registry <ROOT>` сканирует сам `ROOT` и его непосредственные
//! подкаталоги (каждый — проект/репозиторий) и собирает единый индекс из
//! двух источников:
//!
//! - `docs/adr/*.md` — прозаические ADR: номер из заголовка `# ADR-NNN.` /
//!   `# ADR-NNN:`, заголовок — остаток заголовка, `- Date:` и `- Status:`
//!   из шапки;
//! - `model/ADR-*.md` — типизированные сущности (ADR-003): frontmatter
//!   `id`/`title`/`status`/`date` (разбор — [`crate::model::parse_entity`];
//!   файлы сущностей других типов игнорируются — фильтр по префиксу `ADR-`).
//!
//! Находки: коллизия номеров (один `ADR-NNN` в разных проектах с разными
//! заголовками — глобального пространства номеров нет), дубль номера ВНУТРИ
//! проекта (два решения под одним номером — ссылка «ADR-NNN» неоднозначна;
//! намеренное зеркало «проза + типизированная сущность» дублем не считается,
//! см. [`find_issues`]), дубль заголовка (одинаковый заголовок у разных
//! номеров — возможный дубль решения), запись без даты/статуса. Проект без
//! ADR — не находка: просто не попадает в индекс.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{HarnessError, Result};

/// Каталоги, которые не считаются проектами при сканировании `ROOT`.
const SKIP_DIRS: [&str; 3] = [".git", "target", "node_modules"];

/// Одна запись реестра ADR.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegistryEntry {
    /// Проект (имя непосредственного подкаталога `ROOT`; сам `ROOT` — его
    /// имя каталога).
    pub project: String,
    /// Номер ADR (`ADR-007` → 7).
    pub number: u64,
    /// Заголовок решения.
    pub title: String,
    /// Статус (`None` — пропуск обязательного поля, находка).
    pub status: Option<String>,
    /// Дата (`None` — пропуск обязательного поля, находка).
    pub date: Option<String>,
    /// Источник записи: `docs/adr` (проза) или `model` (типизированная
    /// сущность ADR-003).
    pub source: String,
    /// Файл записи относительно каталога проекта (для диагностики).
    pub file: String,
}

impl RegistryEntry {
    /// Идентификатор вида `ADR-007` — для таблиц и находок.
    #[must_use]
    pub fn id(&self) -> String {
        format!("ADR-{:03}", self.number)
    }
}

/// Находка реестра.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegistryFinding {
    /// Тип находки: `number_collision` | `number_duplicate` |
    /// `title_duplicate` | `missing_fields`.
    pub kind: String,
    /// Описание (проекты, номера, заголовки).
    pub message: String,
}

/// Отчёт реестра ADR (сериализуется в `--json` как `{root, entries, findings}`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RegistryReport {
    /// Сканированный корень.
    pub root: PathBuf,
    /// Записи индекса (детерминированная сортировка: проект, номер,
    /// источник, файл).
    pub entries: Vec<RegistryEntry>,
    /// Находки (коллизии номеров, дубли заголовков, пропуски полей).
    pub findings: Vec<RegistryFinding>,
}

/// Exit-код команды: `--strict` + любая находка → 1 (гейт для CI);
/// по умолчанию — 0 (реестр — отчёт, а не гейт).
#[must_use]
pub fn exit_code(report: &RegistryReport, strict: bool) -> i32 {
    i32::from(strict && !report.findings.is_empty())
}

/// Разобранный прозаический ADR (заголовок + шапка).
#[derive(Debug)]
struct ProseAdr {
    /// Номер (`ADR-007` → 7).
    number: u64,
    /// Заголовок решения.
    title: String,
    /// Дата из `- Date:` (может отсутствовать).
    date: Option<String>,
    /// Статус из `- Status:` (может отсутствовать).
    status: Option<String>,
}

/// Парсит прозаический ADR из `docs/adr/*.md`: первый заголовок
/// `# ADR-NNN.` / `# ADR-NNN:`, шапочные `- Date:` / `- Status:`.
/// `Ok(None)` — файл не является ADR (нет заголовка с номером).
fn parse_prose_adr(text: &str) -> Result<Option<ProseAdr>> {
    let heading_re = regex::Regex::new(r"^#{1,3}\s+(ADR-[0-9]+)\s*[:.]\s*(.*?)\s*$")
        .map_err(|e| HarnessError::Model(format!("внутренний regex реестра: {e}")))?;
    let field_re = regex::Regex::new(r"^-\s*(Date|Status)\s*:\s*(.*?)\s*$")
        .map_err(|e| HarnessError::Model(format!("внутренний regex реестра: {e}")))?;
    let id_re = crate::model::id_re()?;

    let mut parsed: Option<ProseAdr> = None;
    for line in text.lines() {
        if parsed.is_none() {
            if let Some(caps) = heading_re.captures(line) {
                // Номер — через общий regex идентификатора модели (ADR-003).
                let id_token = &caps[1];
                let Some(num_caps) = id_re.captures(id_token) else {
                    continue;
                };
                let Ok(number) = num_caps[2].parse::<u64>() else {
                    continue;
                };
                parsed = Some(ProseAdr {
                    number,
                    title: caps[2].to_string(),
                    date: None,
                    status: None,
                });
            }
            continue;
        }
        if let Some(caps) = field_re.captures(line) {
            let value = caps[2].trim().trim_matches('*').trim().to_string();
            let value = (!value.is_empty()).then_some(value);
            if let Some(p) = &mut parsed {
                match &caps[1] {
                    "Date" => p.date = value,
                    _ => p.status = value,
                }
            }
        }
    }
    Ok(parsed)
}

/// Собирает записи проекта `project` из каталога `dir` (два источника).
fn collect_project(project: &str, dir: &Path, entries: &mut Vec<RegistryEntry>) -> Result<()> {
    // Источник 1: прозаические ADR из docs/adr/*.md.
    let docs_dir = dir.join("docs/adr");
    if docs_dir.is_dir() {
        let mut files: Vec<PathBuf> = Vec::new();
        let rd = std::fs::read_dir(&docs_dir).map_err(|e| HarnessError::io(&docs_dir, e))?;
        for entry in rd {
            let entry = entry.map_err(|e| HarnessError::io(&docs_dir, e))?;
            let p = entry.path();
            if p.is_file() && p.extension().is_some_and(|ext| ext == "md") {
                files.push(p);
            }
        }
        files.sort();
        for file in files {
            let text = std::fs::read_to_string(&file).map_err(|e| HarnessError::io(&file, e))?;
            let Some(adr) = parse_prose_adr(&text)? else {
                continue; // не ADR-файл (без заголовка # ADR-NNN.)
            };
            let rel = file
                .strip_prefix(dir)
                .map_or_else(|_| file.clone(), PathBuf::from);
            entries.push(RegistryEntry {
                project: project.to_string(),
                number: adr.number,
                title: adr.title,
                status: adr.status,
                date: adr.date,
                source: "docs/adr".to_string(),
                file: rel.to_string_lossy().replace('\\', "/"),
            });
        }
    }

    // Источник 2: типизированные сущности model/ADR-*.md (ADR-003).
    let model_dir = dir.join("model");
    if model_dir.is_dir() {
        let mut files: Vec<PathBuf> = Vec::new();
        let rd = std::fs::read_dir(&model_dir).map_err(|e| HarnessError::io(&model_dir, e))?;
        for entry in rd {
            let entry = entry.map_err(|e| HarnessError::io(&model_dir, e))?;
            let p = entry.path();
            let is_adr = p
                .file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with("ADR-"))
                && p.extension().is_some_and(|e| e.eq_ignore_ascii_case("md"));
            if p.is_file() && is_adr {
                files.push(p);
            }
        }
        files.sort();
        for file in files {
            let text = std::fs::read_to_string(&file).map_err(|e| HarnessError::io(&file, e))?;
            // Неразбираемый файл (битый frontmatter) — не сущность модели,
            // пропускаем: её проблемы — зона `arch-ml model validate`.
            let Ok(entity) = crate::model::parse_entity(&file, &text) else {
                continue;
            };
            // В model/ живут и другие типы сущностей — берём только ADR-.
            if entity.kind != crate::model::EntityKind::Adr {
                continue;
            }
            let Some((_, number)) = crate::model::parse_id(&entity.id) else {
                continue;
            };
            let rel = file
                .strip_prefix(dir)
                .map_or_else(|_| file.clone(), PathBuf::from);
            entries.push(RegistryEntry {
                project: project.to_string(),
                number,
                title: entity.title,
                status: Some(entity.status),
                date: entity.date,
                source: "model".to_string(),
                file: rel.to_string_lossy().replace('\\', "/"),
            });
        }
    }
    Ok(())
}

/// Находки по собранному индексу.
fn find_issues(entries: &[RegistryEntry]) -> Vec<RegistryFinding> {
    let mut findings = Vec::new();

    // Коллизия номеров: один ADR-NNN в РАЗНЫХ проектах с РАЗНЫМИ заголовками.
    let mut by_number: BTreeMap<u64, Vec<&RegistryEntry>> = BTreeMap::new();
    for e in entries {
        by_number.entry(e.number).or_default().push(e);
    }
    for (number, group) in &by_number {
        let mut projects: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
        for e in group {
            projects
                .entry(e.project.as_str())
                .or_default()
                .push(e.title.as_str());
        }
        let distinct_titles: std::collections::BTreeSet<&str> =
            group.iter().map(|e| e.title.as_str()).collect();
        // Коллизия — только если разные проекты И разные заголовки.
        if projects.len() > 1 && distinct_titles.len() > 1 {
            let detail = projects
                .iter()
                .map(|(p, titles)| {
                    let mut t = titles.clone();
                    t.sort_unstable();
                    t.dedup();
                    format!("{p}: «{}»", t.join("», «"))
                })
                .collect::<Vec<_>>()
                .join("; ");
            findings.push(RegistryFinding {
                kind: "number_collision".to_string(),
                message: format!(
                    "ADR-{number:03} в разных проектах с разными заголовками: {detail} \
                     (глобального пространства номеров нет — ссылки на номер неоднозначны)"
                ),
            });
        }
    }

    // Дубль номера ВНУТРИ проекта: один ADR-NNN у двух разных решений в
    // одном проекте. Выше — про глобальную агрегацию (ADR-036: номера
    // проектные, кросс-проектное совпадение само по себе не дефект); здесь —
    // про однозначность ссылки «ADR-NNN» внутри проекта, где номер обязан
    // быть уникален. Живой случай: два файла в одном `docs/adr/` под одним
    // номером (нашлись ADR-041 «vision» и ADR-041 «язык дизайна TUI»).
    //
    // Намеренное зеркало «проза `docs/adr` + типизированная сущность `model`»
    // (ADR-003) дублем НЕ считается: у него совпадает заголовок, а источник
    // записей разный — поэтому файловый дубль ищется только внутри ОДНОГО
    // источника.
    for (number, group) in &by_number {
        let mut by_project: BTreeMap<&str, Vec<&RegistryEntry>> = BTreeMap::new();
        for e in group.iter().copied() {
            by_project.entry(e.project.as_str()).or_default().push(e);
        }
        for (project, same_project) in by_project {
            if same_project.len() < 2 {
                continue;
            }
            let titles: std::collections::BTreeSet<&str> =
                same_project.iter().map(|e| e.title.trim()).collect();
            let mut files_by_source: BTreeMap<&str, std::collections::BTreeSet<&str>> =
                BTreeMap::new();
            for e in &same_project {
                files_by_source
                    .entry(e.source.as_str())
                    .or_default()
                    .insert(e.file.as_str());
            }
            let same_source_dup = files_by_source.values().any(|files| files.len() > 1);
            if titles.len() <= 1 && !same_source_dup {
                continue;
            }
            let detail = same_project
                .iter()
                .map(|e| format!("«{}» ({})", e.title.trim(), e.file))
                .collect::<Vec<_>>()
                .join("; ");
            findings.push(RegistryFinding {
                kind: "number_duplicate".to_string(),
                message: format!(
                    "ADR-{number:03} встречается несколько раз в проекте {project}: {detail} \
                     (внутри проекта номер обязан быть уникален — иначе ссылка на \
                     ADR-{number:03} неоднозначна)"
                ),
            });
        }
    }

    // Дубль заголовка: одинаковый заголовок у разных номеров.
    let mut by_title: BTreeMap<String, Vec<&RegistryEntry>> = BTreeMap::new();
    for e in entries {
        let key = e.title.trim().to_lowercase();
        if !key.is_empty() {
            by_title.entry(key).or_default().push(e);
        }
    }
    for (title, group) in &by_title {
        let mut ids: Vec<String> = group
            .iter()
            .map(|e| format!("{} ({})", e.id(), e.project))
            .collect();
        ids.sort();
        ids.dedup();
        if ids.len() > 1 {
            // В сообщении — заголовок в исходном регистре (первой записи группы).
            let original = group.first().map_or(title.as_str(), |e| e.title.trim());
            findings.push(RegistryFinding {
                kind: "title_duplicate".to_string(),
                message: format!(
                    "заголовок «{original}» у разных номеров: {}",
                    ids.join(", ")
                ),
            });
        }
    }

    // Пропуск обязательных полей: нет даты или статуса.
    for e in entries {
        let missing: Vec<&str> = [
            e.date.is_none().then_some("дата"),
            e.status.is_none().then_some("статус"),
        ]
        .into_iter()
        .flatten()
        .collect();
        if !missing.is_empty() {
            findings.push(RegistryFinding {
                kind: "missing_fields".to_string(),
                message: format!(
                    "{} {} ({}): нет поля «{}»",
                    e.project,
                    e.id(),
                    e.file,
                    missing.join("», «")
                ),
            });
        }
    }
    findings
}

/// Строит реестр ADR по `ROOT`: сам каталог + непосредственные подкаталоги
/// (каждый — проект). Каталоги из [`SKIP_DIRS`] пропускаются; проект без
/// ADR молча не попадает в индекс.
///
/// # Errors
/// `ROOT` не каталог; нечего регистрировать — ни в самом `ROOT`, ни в
/// непосредственных подкаталогах не найдено ни одного ADR.
pub fn build_registry(root: &Path) -> Result<RegistryReport> {
    if !root.is_dir() {
        return Err(HarnessError::Model(format!(
            "реестр ADR: корень недоступен: {}",
            root.display()
        )));
    }
    let root_name = root
        .canonicalize()
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_else(|| ".".to_string());

    let mut entries = Vec::new();
    collect_project(&root_name, root, &mut entries)?;

    let mut projects: Vec<PathBuf> = Vec::new();
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
        projects.push(p);
    }
    projects.sort();
    for p in &projects {
        let name = p
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        collect_project(&name, p, &mut entries)?;
    }

    if entries.is_empty() {
        return Err(HarnessError::Model(format!(
            "реестр ADR: нечего регистрировать — ни в {}, ни в его непосредственных \
             подкаталогах не найдено ADR (источники: docs/adr/*.md, model/ADR-*.md)",
            root.display()
        )));
    }

    entries.sort_by(|a, b| {
        (&a.project, a.number, &a.source, &a.file).cmp(&(&b.project, b.number, &b.source, &b.file))
    });
    let findings = find_issues(&entries);
    Ok(RegistryReport {
        root: root.to_path_buf(),
        entries,
        findings,
    })
}

/// Рендер реестра в markdown: таблица индекса + секция находок.
#[must_use]
pub fn render_markdown(report: &RegistryReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# Реестр ADR: {}\n", report.root.display());
    let _ = writeln!(
        out,
        "| Проект | ADR | Заголовок | Статус | Дата | Источник |"
    );
    let _ = writeln!(out, "|---|---|---|---|---|---|");
    for e in &report.entries {
        let dash = "—";
        let _ = writeln!(
            out,
            "| {} | {} | {} | {} | {} | {} |",
            e.project,
            e.id(),
            e.title,
            e.status.as_deref().unwrap_or(dash),
            e.date.as_deref().unwrap_or(dash),
            e.source
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
    let _ = write!(
        out,
        "\nИтого: {} записей, {} находок",
        report.entries.len(),
        report.findings.len()
    );
    out
}

/// Проверяет наличие анкера `rule-<m>` в ADR-файле (H0.3 §2.1): заголовок
/// `### rule-<m> …` либо жирный ярлык пункта `- **rule-<m>** — …`.
pub(crate) fn anchor_present(file: &Path, frag: &str) -> Result<bool> {
    let text = std::fs::read_to_string(file).map_err(|e| HarnessError::io(file, e))?;
    Ok(text.lines().any(|l| {
        let t = l.trim();
        (t.starts_with('#') && t.contains(frag))
            || ((t.starts_with('-') || t.starts_with('*')) && t.contains(&format!("**{frag}**")))
    }))
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

    /// Фикстура: ROOT с собственным ADR и тремя «проектами»:
    /// p1 (docs/adr, один ADR без даты), p2 (model/: ADR + CMP, который
    /// обязан игнорироваться), p3 (без ADR — молча пропускается).
    fn fixture(dir: &Path) -> PathBuf {
        let root = dir.join("root");
        write_file(
            &root,
            "docs/adr/ADR-009-korn.md",
            "# ADR-009. Корневое решение\n\n- Date: 2026-01-01\n- Status: Accepted\n",
        );
        write_file(
            &root,
            "p1/docs/adr/ADR-001-outbox.md",
            "# ADR-001. Outbox pattern\n\n- Date: 2026-02-01\n- Status: Accepted\n",
        );
        write_file(
            &root,
            "p1/docs/adr/ADR-002-shina.md",
            "# ADR-002: Единая шина событий\n\n- Status: Proposed\n",
        );
        // Не ADR-файл в docs/adr — игнорируется.
        write_file(&root, "p1/docs/adr/README.md", "# О каталоге\n");
        write_file(
            &root,
            "p2/model/ADR-002-shina.md",
            "---\nid: ADR-002\ntype: adr\ntitle: Другая шина\nstatus: adopted\ndate: 2026-03-01\n---\nТело.\n",
        );
        write_file(
            &root,
            "p2/model/ADR-005-outbox.md",
            "---\nid: ADR-005\ntype: adr\ntitle: Outbox pattern\nstatus: proposed\n---\nТело.\n",
        );
        // Другой тип сущности в model/ — игнорируется.
        write_file(
            &root,
            "p2/model/CMP-001-core.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: Core\nstatus: adopted\n---\nТело.\n",
        );
        // Проект без ADR.
        write_file(&root, "p3/README.md", "пустой проект\n");
        // Служебные каталоги не считаются проектами.
        write_file(
            &root,
            ".git/docs/adr/ADR-099-sleep.md",
            "# ADR-099. Спящий\n\n- Date: 2020-01-01\n- Status: Draft\n",
        );
        root
    }

    #[test]
    fn registry_collects_index_and_findings() {
        let dir = tempfile::tempdir().unwrap();
        let root = fixture(dir.path());
        let report = build_registry(&root).unwrap();

        // Индекс: root (1) + p1 (2) + p2 (2) = 5 записей; p3 и .git — мимо.
        assert_eq!(report.entries.len(), 5, "{:?}", report.entries);
        assert!(
            report.entries.iter().all(|e| e.project != "p3"),
            "проект без ADR молча пропускается"
        );
        assert!(
            report.entries.iter().all(|e| e.number != 99),
            ".git не сканируется"
        );
        // Детерминированная сортировка: проект, затем номер.
        let order: Vec<(String, u64)> = report
            .entries
            .iter()
            .map(|e| (e.project.clone(), e.number))
            .collect();
        let mut sorted = order.clone();
        sorted.sort();
        assert_eq!(order, sorted, "{order:?}");
        // Источники распознаны.
        let p2 = |n: u64| {
            report
                .entries
                .iter()
                .find(|e| e.project == "p2" && e.number == n)
                .unwrap()
        };
        assert_eq!(p2(2).source, "model");
        assert_eq!(p2(2).status.as_deref(), Some("adopted"));
        assert_eq!(p2(2).date.as_deref(), Some("2026-03-01"));
        assert_eq!(p2(5).date, None, "дата опциональна в модели");
        let prose = report
            .entries
            .iter()
            .find(|e| e.project == "p1" && e.number == 2)
            .unwrap();
        assert_eq!(prose.source, "docs/adr");
        assert_eq!(prose.title, "Единая шина событий");
        assert_eq!(prose.status.as_deref(), Some("Proposed"));
        assert_eq!(prose.date, None, "в прозе даты нет");

        // Находки: коллизия номеров (ADR-002: p1 ≠ p2), дубль заголовка
        // (Outbox pattern: ADR-001/p1 и ADR-005/p2), пропуски полей.
        let kinds = |k: &str| {
            report
                .findings
                .iter()
                .filter(|f| f.kind == k)
                .collect::<Vec<_>>()
        };
        let collisions = kinds("number_collision");
        assert_eq!(collisions.len(), 1, "{collisions:?}");
        assert!(collisions[0].message.contains("ADR-002"), "{collisions:?}");
        let dupes = kinds("title_duplicate");
        assert_eq!(dupes.len(), 1, "{dupes:?}");
        assert!(dupes[0].message.contains("Outbox pattern"), "{dupes:?}");
        let missing = kinds("missing_fields");
        assert!(
            missing.iter().any(|f| f.message.contains("p1 ADR-002")),
            "{missing:?}"
        );
        assert!(
            missing.iter().any(|f| f.message.contains("p2 ADR-005")),
            "{missing:?}"
        );
    }

    #[test]
    fn registry_same_title_same_number_across_projects_is_not_collision() {
        // Одинаковый номер + одинаковый заголовок в разных проектах — это
        // скопированное решение, а не коллизия пространства номеров.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("root");
        write_file(
            &root,
            "a/docs/adr/ADR-001-x.md",
            "# ADR-001. Одно и то же\n\n- Date: 2026-01-01\n- Status: Accepted\n",
        );
        write_file(
            &root,
            "b/docs/adr/ADR-001-x.md",
            "# ADR-001. Одно и то же\n\n- Date: 2026-01-02\n- Status: Accepted\n",
        );
        let report = build_registry(&root).unwrap();
        assert!(
            report.findings.iter().all(|f| f.kind != "number_collision"),
            "{:?}",
            report.findings
        );
    }

    #[test]
    fn registry_same_number_twice_in_one_project_is_duplicate() {
        // Живой случай: два файла в одном `docs/adr/` под номером 041 —
        // ссылка «ADR-041» внутри проекта становится неоднозначной.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("root");
        write_file(
            &root,
            "docs/adr/ADR-041-upravlenie-brauzerom.md",
            "# ADR-041. Управление браузером\n\n- Date: 2026-09-12\n- Status: Accepted\n",
        );
        write_file(
            &root,
            "docs/adr/ADR-041-yazyk-dizayna-tui.md",
            "# ADR-041. Язык дизайна TUI\n\n- Date: 2026-09-12\n- Status: Accepted\n",
        );
        let report = build_registry(&root).unwrap();
        let dup: Vec<&RegistryFinding> = report
            .findings
            .iter()
            .filter(|f| f.kind == "number_duplicate")
            .collect();
        assert_eq!(dup.len(), 1, "ровно одна находка: {:?}", report.findings);
        assert!(dup[0].message.contains("ADR-041"), "{}", dup[0].message);
        assert!(
            dup[0].message.contains("Управление браузером")
                && dup[0].message.contains("Язык дизайна TUI"),
            "в сообщении оба заголовка: {}",
            dup[0].message
        );
        // Гейт CI: находка обязана ломать --strict.
        assert_eq!(exit_code(&report, true), 1);
    }

    #[test]
    fn registry_same_number_same_title_same_source_is_still_duplicate() {
        // Два файла одного источника с одним номером — дубль нумерации даже
        // при совпадающих заголовках (заголовок не различает файлы).
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("root");
        for name in ["ADR-002-shina.md", "ADR-002-shina-kopiya.md"] {
            write_file(
                &root,
                &format!("docs/adr/{name}"),
                "# ADR-002. Единая шина\n\n- Date: 2026-01-01\n- Status: Accepted\n",
            );
        }
        let report = build_registry(&root).unwrap();
        assert!(
            report.findings.iter().any(|f| f.kind == "number_duplicate"),
            "{:?}",
            report.findings
        );
    }

    #[test]
    fn registry_prose_and_typed_mirror_is_not_duplicate() {
        // Намеренное зеркало (ADR-003): та же сущность прозой в `docs/adr`
        // и типизированно в `model` — один ADR в двух представлениях, не дубль.
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("root");
        write_file(
            &root,
            "docs/adr/ADR-007-obshee.md",
            "# ADR-007. Общее решение\n\n- Date: 2026-01-01\n- Status: Accepted\n",
        );
        write_file(
            &root,
            "model/ADR-007-obshee.md",
            "---\nid: ADR-007\ntype: adr\ntitle: Общее решение\nstatus: adopted\ndate: 2026-01-01\n---\nТело.\n",
        );
        let report = build_registry(&root).unwrap();
        assert!(
            report.findings.is_empty(),
            "зеркало не должно давать находок: {:?}",
            report.findings
        );
        assert_eq!(report.entries.len(), 2, "обе записи проиндексированы");
    }

    #[test]
    fn registry_strict_exit_code_and_json() {
        let dir = tempfile::tempdir().unwrap();
        let root = fixture(dir.path());
        let report = build_registry(&root).unwrap();
        // --strict: любая находка → exit 1; без --strict — отчёт, exit 0.
        assert_eq!(exit_code(&report, true), 1);
        assert_eq!(exit_code(&report, false), 0);
        // Чистый индекс: --strict не ломает.
        let clean_root = dir.path().join("clean");
        write_file(
            &clean_root,
            "docs/adr/ADR-001-ok.md",
            "# ADR-001. Чистое\n\n- Date: 2026-01-01\n- Status: Accepted\n",
        );
        let clean = build_registry(&clean_root).unwrap();
        assert!(clean.findings.is_empty(), "{:?}", clean.findings);
        assert_eq!(exit_code(&clean, true), 0);
        // --json: единый объект {entries, findings}.
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&report).unwrap()).unwrap();
        assert!(v["entries"].is_array(), "{v}");
        assert!(v["findings"].is_array(), "{v}");
        assert_eq!(v["entries"][0]["project"], "p1");
        assert!(v["entries"][0]["number"].is_number());
        assert!(
            v["findings"]
                .as_array()
                .unwrap()
                .iter()
                .any(|f| f["kind"] == "number_collision")
        );
    }

    #[test]
    fn registry_empty_root_is_clear_error() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("empty");
        std::fs::create_dir_all(root.join("p3")).unwrap();
        let err = build_registry(&root).unwrap_err();
        assert!(err.to_string().contains("нечего регистрировать"), "{err}");
        assert!(
            build_registry(&root.join("missing")).is_err(),
            "несуществующий корень — ошибка"
        );
    }

    #[test]
    fn registry_markdown_renders_table_and_findings() {
        let dir = tempfile::tempdir().unwrap();
        let root = fixture(dir.path());
        let report = build_registry(&root).unwrap();
        let md = render_markdown(&report);
        assert!(
            md.contains("| Проект | ADR | Заголовок | Статус | Дата | Источник |"),
            "{md}"
        );
        assert!(
            md.contains("| p1 | ADR-001 | Outbox pattern | Accepted | 2026-02-01 | docs/adr |"),
            "{md}"
        );
        assert!(
            md.contains("| p2 | ADR-002 | Другая шина | adopted | 2026-03-01 | model |"),
            "{md}"
        );
        assert!(md.contains("[number_collision]"), "{md}");
        assert!(md.contains("## Находки"), "{md}");
        assert!(md.contains("Итого: 5 записей"), "{md}");
    }
}
