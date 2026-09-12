//! Граф связей модели: обнаружение циклов, текстовый и mermaid-вывод.

use std::collections::HashMap;
use std::fmt::Write as _;

use crate::model::parse::{Entity, LinkKind, Model};

/// Максимальная длина заголовка в метке mermaid-узла (дальше — `…`).
const MAX_MERMAID_TITLE_CHARS: usize = 48;

/// Ищет цикл по рёбрам `depends_on` (только между существующими сущностями).
///
/// Возвращает первый найденный цикл как цепочку ID (`A → B → A`).
/// Детерминирован: обход в порядке сущностей модели.
#[must_use]
pub fn find_cycle(model: &Model) -> Option<Vec<String>> {
    // 0 — не посещён, 1 — в стеке, 2 — завершён.
    let mut color: HashMap<&str, u8> = HashMap::with_capacity(model.entities.len());
    let mut stack: Vec<&str> = Vec::new();
    for e in &model.entities {
        if color.get(e.id.as_str()) != Some(&2) {
            if let Some(cycle) = dfs(model, &e.id, &mut color, &mut stack) {
                return Some(cycle);
            }
        }
    }
    None
}

/// Шаг DFS с отсечением по текущему пути (классические три цвета).
fn dfs<'m>(
    model: &'m Model,
    id: &'m str,
    color: &mut HashMap<&'m str, u8>,
    stack: &mut Vec<&'m str>,
) -> Option<Vec<String>> {
    color.insert(id, 1);
    stack.push(id);
    let e = model.get(id)?;
    for dep in &e.depends_on {
        let dep = dep.as_str();
        if model.get(dep).is_none() {
            continue; // битые ссылки — забота validate, не обхода
        }
        match color.get(dep) {
            Some(1) => {
                // Нашли заднее ребро: цикл от позиции dep в стеке.
                let start = stack.iter().position(|s| *s == dep).unwrap_or(0);
                let mut cycle: Vec<String> =
                    stack[start..].iter().map(|s| (*s).to_string()).collect();
                cycle.push(dep.to_string());
                return Some(cycle);
            }
            Some(2) => {}
            _ => {
                if let Some(cycle) = dfs(model, dep, color, stack) {
                    return Some(cycle);
                }
            }
        }
    }
    stack.pop();
    color.insert(id, 2);
    None
}

/// Текстовый граф: сводка + для каждой сущности исходящие связи.
#[must_use]
pub fn graph_text(model: &Model) -> String {
    let mut edges = 0usize;
    let mut out = String::new();
    for e in &model.entities {
        for kind in LinkKind::ALL {
            edges += e.link_targets(kind).len();
        }
    }
    let _ = writeln!(
        out,
        "Граф модели {}: {} сущностей, {} связей",
        model.dir.display(),
        model.entities.len(),
        edges
    );
    for e in &model.entities {
        let _ = writeln!(
            out,
            "{} [{} · {}] {}",
            e.id,
            e.kind.type_str(),
            e.status,
            e.title
        );
        for kind in LinkKind::ALL {
            let targets = e.link_targets(kind);
            if !targets.is_empty() {
                let _ = writeln!(out, "  {} → {}", kind.field_name(), targets.join(", "));
            }
        }
    }
    out
}

/// Mermaid-идентификатор узла: `-` недопустим в id flowchart — замена на `_`.
fn mermaid_id(id: &str) -> String {
    id.replace('-', "_")
}

/// Метка узла: `ID · заголовок` с усечением и безопасными скобками/кавычками.
fn mermaid_label(e: &Entity) -> String {
    let mut title: String = e.title.chars().take(MAX_MERMAID_TITLE_CHARS).collect();
    if e.title.chars().count() > MAX_MERMAID_TITLE_CHARS {
        title.push('…');
    }
    let mut safe = String::with_capacity(e.id.len() + 3 + title.len());
    safe.push_str(&e.id);
    safe.push_str(" · ");
    for ch in title.chars() {
        safe.push(match ch {
            '"' => '\'',
            '[' | '{' => '(',
            ']' | '}' => ')',
            other => other,
        });
    }
    safe
}

/// Граф модели как mermaid flowchart (совместим с `arch-ml mermaid`).
#[must_use]
pub fn graph_mermaid(model: &Model) -> String {
    let mut out = String::from("flowchart LR\n");
    for e in &model.entities {
        let _ = writeln!(out, "  {}[\"{}\"]", mermaid_id(&e.id), mermaid_label(e));
    }
    for e in &model.entities {
        for kind in LinkKind::ALL {
            for target in e.link_targets(kind) {
                if model.get(target).is_none() {
                    continue; // битые ссылки не рисуем — их покажет validate
                }
                let _ = writeln!(
                    out,
                    "  {} -->|{}| {}",
                    mermaid_id(&e.id),
                    kind.field_name(),
                    mermaid_id(target)
                );
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;
    use crate::model::parse::load_model;

    fn entity(dir: &Path, id: &str, kind: &str, depends: &[&str]) {
        let deps = depends
            .iter()
            .map(|d| format!("  - {d}"))
            .collect::<Vec<_>>()
            .join("\n");
        let deps = if deps.is_empty() {
            String::new()
        } else {
            format!("depends_on:\n{deps}\n")
        };
        std::fs::write(
            dir.join(format!("{id}.md")),
            format!("---\nid: {id}\ntype: {kind}\ntitle: T {id}\nstatus: s\n{deps}---\n"),
        )
        .expect("фикстура");
    }

    #[test]
    fn find_cycle_none_for_acyclic() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(dir.path(), "CMP-001", "cmp", &["CMP-002"]);
        entity(dir.path(), "CMP-002", "cmp", &["CMP-003"]);
        entity(dir.path(), "CMP-003", "cmp", &[]);
        let m = load_model(dir.path()).expect("модель");
        assert_eq!(find_cycle(&m), None);
    }

    #[test]
    fn find_cycle_detects_triangle() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(dir.path(), "CMP-001", "cmp", &["CMP-002"]);
        entity(dir.path(), "CMP-002", "cmp", &["CMP-003"]);
        entity(dir.path(), "CMP-003", "cmp", &["CMP-001"]);
        let m = load_model(dir.path()).expect("модель");
        let cycle = find_cycle(&m).expect("цикл");
        assert_eq!(cycle.first(), cycle.last(), "цепочка замкнута: {cycle:?}");
        assert!(cycle.len() >= 3, "{cycle:?}");
    }

    #[test]
    fn find_cycle_detects_self_loop() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(dir.path(), "CMP-001", "cmp", &["CMP-001"]);
        let m = load_model(dir.path()).expect("модель");
        let cycle = find_cycle(&m).expect("петля");
        assert_eq!(cycle, vec!["CMP-001", "CMP-001"]);
    }

    #[test]
    fn find_cycle_ignores_dangling_edges() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(dir.path(), "CMP-001", "cmp", &["CMP-999"]);
        let m = load_model(dir.path()).expect("модель");
        assert_eq!(find_cycle(&m), None, "битая ссылка — не цикл");
    }

    #[test]
    fn graph_text_lists_entities_and_edges() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(dir.path(), "CMP-001", "cmp", &["CMP-002"]);
        entity(dir.path(), "CMP-002", "cmp", &[]);
        let m = load_model(dir.path()).expect("модель");
        let text = graph_text(&m);
        assert!(text.contains("2 сущностей, 1 связей"), "{text}");
        assert!(text.contains("CMP-001 [cmp · s] T CMP-001"), "{text}");
        assert!(text.contains("depends_on → CMP-002"), "{text}");
    }

    #[test]
    fn graph_mermaid_flowchart_sanitized() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(dir.path(), "ADR-001", "adr", &["AD-1"]);
        entity(dir.path(), "AD-1", "ad", &[]);
        let m = load_model(dir.path()).expect("модель");
        let mm = graph_mermaid(&m);
        assert!(mm.starts_with("flowchart LR\n"), "{mm}");
        assert!(mm.contains("ADR_001[\"ADR-001 · T ADR-001\"]"), "{mm}");
        assert!(mm.contains("ADR_001 -->|depends_on| AD_1"), "{mm}");
    }

    #[test]
    fn graph_mermaid_skips_broken_targets() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(dir.path(), "CMP-001", "cmp", &["CMP-999"]);
        let m = load_model(dir.path()).expect("модель");
        let mm = graph_mermaid(&m);
        assert!(
            !mm.contains("CMP_999"),
            "несуществующий узел не рисуется: {mm}"
        );
    }
}
