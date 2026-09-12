//! Библиотека специализированных архитектурных промптов.
//!
//! КОНТРАКТ (владелец: агент `agent`): каталог `assets/prompts/*.md`;
//! front-matter не нужен — имя файла = имя шаблона, первая строка
//! `# Описание` — краткое описание; плейсхолдеры `{{var}}` подставляются
//! через [`render`].

use std::collections::HashMap;
use std::path::Path;

use crate::error::{HarnessError, Result};

/// Шаблон промпта.
#[derive(Debug, Clone)]
pub struct PromptTemplate {
    /// Имя (имя файла без .md).
    pub name: String,
    /// Краткое описание (первая строка файла).
    pub description: String,
    /// Тело шаблона.
    pub body: String,
}

/// Загружает все шаблоны каталога (`*.md`), отсортированные по имени.
/// Отсутствующий каталог — не ошибка: возвращается пустая библиотека
/// (харнесс должен работать и без ассетов).
///
/// # Errors
/// Каталог существует, но не читается; файл шаблона не читается.
pub fn load_library(dir: &Path) -> Result<Vec<PromptTemplate>> {
    if !dir.is_dir() {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    let entries = std::fs::read_dir(dir).map_err(|e| HarnessError::io(dir, e))?;
    for entry in entries {
        let entry = entry.map_err(|e| HarnessError::io(dir, e))?;
        let path = entry.path();
        if !path.is_file() || path.extension().and_then(|e| e.to_str()) != Some("md") {
            continue;
        }
        let Some(name) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        let body = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
        let description = body
            .lines()
            .map(str::trim)
            .find(|l| l.starts_with('#'))
            .map(|l| l.trim_start_matches('#').trim().to_string())
            .unwrap_or_default();
        out.push(PromptTemplate {
            name: name.to_string(),
            description,
            body,
        });
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}

/// Подставляет `{{var}}` в тело шаблона. Неизвестные плейсхолдеры остаются.
#[must_use]
pub fn render(template: &PromptTemplate, vars: &HashMap<String, String>) -> String {
    let mut out = template.body.clone();
    for (k, v) in vars {
        out = out.replace(&format!("{{{{{k}}}}}"), v);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn load_missing_dir_returns_empty() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let lib = load_library(&tmp.path().join("нет-такого")).expect("Ok");
        assert!(lib.is_empty());
    }

    #[test]
    fn load_reads_md_with_description() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let dir = tmp.path();
        std::fs::write(
            dir.join("architect.md"),
            "# Архитектор\n\nТело {{subject}}.\n",
        )
        .expect("write");
        std::fs::write(dir.join("plain.md"), "Без заголовка\nпросто текст").expect("write");
        std::fs::write(dir.join("ignore.txt"), "# Не md").expect("write");
        std::fs::create_dir(dir.join("subdir.md")).expect("mkdir");

        let lib = load_library(dir).expect("load");
        assert_eq!(lib.len(), 2, "только .md-файлы");
        assert_eq!(lib[0].name, "architect");
        assert_eq!(lib[0].description, "Архитектор");
        assert!(lib[0].body.contains("Тело"));
        assert_eq!(lib[1].name, "plain");
        assert_eq!(lib[1].description, "", "нет #-строки — пустое описание");
    }

    #[test]
    fn render_substitutes_known_keeps_unknown() {
        let tpl = PromptTemplate {
            name: "t".into(),
            description: String::new(),
            body: "Предмет: {{subject}}. Ещё: {{unknown}}. Повтор: {{subject}}.".into(),
        };
        let mut vars = HashMap::new();
        vars.insert("subject".to_string(), "интеграция".to_string());
        let out = render(&tpl, &vars);
        assert_eq!(
            out,
            "Предмет: интеграция. Ещё: {{unknown}}. Повтор: интеграция."
        );
    }
}
