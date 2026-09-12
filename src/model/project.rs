//! Проекция модели в производные артефакты: `ADR-*` → `.arch-handoff/adr/`.
//!
//! Проекция зеркальная (ADR-003): имена файлов совпадают с именами файлов
//! сущностей в `model/`; файлы `ADR-*.md` в целевом каталоге без
//! соответствующей сущности удаляются. Каталог `.arch-handoff/adr/` —
//! полностью генерируемый, ручных правок в нём быть не должно.

use std::path::{Path, PathBuf};

use crate::error::{HarnessError, Result};
use crate::model::EntityKind;
use crate::model::parse::{Entity, Model, load_model};

/// Отчёт проекции.
#[derive(Debug)]
pub struct ProjectReport {
    /// Каталог, куда записаны файлы.
    pub out_dir: PathBuf,
    /// Записанные файлы (в порядке сущностей модели).
    pub written: Vec<PathBuf>,
    /// Удалённые устаревшие файлы (`ADR-*.md` без сущности).
    pub removed: Vec<PathBuf>,
}

/// Рендерит сущность `ADR-*` в markdown формата `docs/adr`
/// (`# ADR-NNN. <title>`, `- Date:`, `- Status:`, тело).
#[must_use]
pub fn render_adr(e: &Entity) -> String {
    use std::fmt::Write as _;
    let mut out = format!("# {}. {}\n\n", e.id, e.title);
    if let Some(date) = &e.date {
        // игнорируется: запись в String не падает
        let _ = writeln!(out, "- Date: {date}");
    }
    let _ = writeln!(out, "- Status: {}", e.status);
    if !e.body.is_empty() {
        let _ = write!(out, "\n{}\n", e.body);
    }
    out
}

/// Проецирует все сущности `ADR-*` модели из `model_dir` в
/// `<model_dir>/../.arch-handoff/adr/`.
///
/// Запись идёт по одному файлу на сущность, имя — как у файла сущности.
/// Устаревшие `ADR-*.md` в целевом каталоге удаляются (зеркало). Если в
/// модели нет ни одной сущности `ADR-*` — ошибка (страховка от затирания
/// каталога пустой проекцией).
///
/// # Errors
/// Модель не загружается, нет сущностей `ADR-*`, целевой каталог
/// не создаётся, файл не записывается.
pub fn project_adr(model_dir: &Path) -> Result<ProjectReport> {
    let model: Model = load_model(model_dir)?;
    let adrs: Vec<&Entity> = model
        .entities
        .iter()
        .filter(|e| e.kind == EntityKind::Adr)
        .collect();
    if adrs.is_empty() {
        return Err(HarnessError::Model(format!(
            "в модели {} нет сущностей ADR-* — проекция отменена",
            model_dir.display()
        )));
    }
    let case_root = model_dir.parent().ok_or_else(|| {
        HarnessError::Model(format!(
            "у {} нет родительского каталога",
            model_dir.display()
        ))
    })?;
    let out_dir = case_root.join(".arch-handoff").join("adr");
    std::fs::create_dir_all(&out_dir).map_err(|e| HarnessError::io(&out_dir, e))?;

    let mut written = Vec::with_capacity(adrs.len());
    let mut produced: Vec<String> = Vec::with_capacity(adrs.len());
    for e in adrs {
        let name = e
            .file
            .file_name()
            .ok_or_else(|| HarnessError::Model(format!("у сущности {} нет имени файла", e.id)))?;
        let name = name.to_string_lossy().to_string();
        let target = out_dir.join(&name);
        std::fs::write(&target, render_adr(e)).map_err(|e| HarnessError::io(&target, e))?;
        produced.push(name);
        written.push(target);
    }

    // Зеркало: убрать устаревшие ADR-*.md, которых нет среди сгенерированных.
    let mut removed = Vec::new();
    let rd = std::fs::read_dir(&out_dir).map_err(|e| HarnessError::io(&out_dir, e))?;
    for entry in rd {
        let entry = entry.map_err(|e| HarnessError::io(&out_dir, e))?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().to_string();
        let is_adr = name.starts_with("ADR-")
            && path
                .extension()
                .is_some_and(|ext| ext.eq_ignore_ascii_case("md"));
        if is_adr && !produced.contains(&name) {
            std::fs::remove_file(&path).map_err(|e| HarnessError::io(&path, e))?;
            removed.push(path);
        }
    }
    removed.sort();
    Ok(ProjectReport {
        out_dir,
        written,
        removed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn adr_entity(dir: &Path, name: &str, id: &str, title: &str, body: &str) {
        std::fs::write(
            dir.join(name),
            format!(
                "---\nid: {id}\ntype: adr\ntitle: {title}\nstatus: Accepted\ndate: 2026-08-15\n---\n\n{body}\n"
            ),
        )
        .expect("фикстура");
    }

    #[test]
    fn render_adr_layout() {
        let dir = tempfile::tempdir().expect("tmp");
        adr_entity(
            dir.path(),
            "ADR-001-x.md",
            "ADR-001",
            "Заголовок",
            "Тело решения.",
        );
        let m = load_model(dir.path()).expect("модель");
        let e = m.get("ADR-001").expect("сущность");
        assert_eq!(
            render_adr(e),
            "# ADR-001. Заголовок\n\n- Date: 2026-08-15\n- Status: Accepted\n\nТело решения.\n"
        );
    }

    #[test]
    fn project_writes_files_and_removes_stale() {
        let dir = tempfile::tempdir().expect("tmp");
        let model_dir = dir.path().join("case/model");
        std::fs::create_dir_all(&model_dir).expect("dirs");
        adr_entity(&model_dir, "ADR-001-a.md", "ADR-001", "A", "Тело A.");
        adr_entity(&model_dir, "ADR-002-b.md", "ADR-002", "B", "Тело B.");
        // Устаревший файл без сущности + посторонний файл (не трогаем).
        let out = dir.path().join("case/.arch-handoff/adr");
        std::fs::create_dir_all(&out).expect("out");
        std::fs::write(out.join("ADR-099-stale.md"), "старый").expect("stale");
        std::fs::write(out.join("NOTES.md"), "посторонний").expect("other");

        let report = project_adr(&model_dir).expect("проекция");
        assert_eq!(report.written.len(), 2);
        assert_eq!(report.removed, vec![out.join("ADR-099-stale.md")]);
        assert!(out.join("NOTES.md").exists(), "не-ADR файл не трогаем");
        let text = std::fs::read_to_string(out.join("ADR-001-a.md")).expect("чтение");
        assert!(
            text.starts_with("# ADR-001. A\n\n- Date: 2026-08-15\n"),
            "{text}"
        );
    }

    #[test]
    fn project_without_adr_entities_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let model_dir = dir.path().join("case/model");
        std::fs::create_dir_all(&model_dir).expect("dirs");
        std::fs::write(
            model_dir.join("CMP-001.md"),
            "---\nid: CMP-001\ntype: cmp\ntitle: X\nstatus: s\n---\n",
        )
        .expect("фикстура");
        let err = project_adr(&model_dir).expect_err("нет ADR — отказ");
        assert!(err.to_string().contains("нет сущностей ADR"), "{err}");
    }

    #[test]
    fn project_creates_out_dir() {
        let dir = tempfile::tempdir().expect("tmp");
        let model_dir = dir.path().join("case/model");
        std::fs::create_dir_all(&model_dir).expect("dirs");
        adr_entity(&model_dir, "ADR-001-a.md", "ADR-001", "A", "Тело.");
        let report = project_adr(&model_dir).expect("проекция");
        assert!(report.out_dir.is_dir(), "каталог создан");
        assert!(report.out_dir.join("ADR-001-a.md").is_file());
    }
}
