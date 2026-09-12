//! Глобальная md-память харнесса (`MEMORY.md`, по аналогии с Kimi Code).
//!
//! Память — обычный markdown-файл в домашнем каталоге харнесса
//! (по умолчанию `~/.arch-ml/MEMORY.md`, переопределяется
//! `paths.memory_file`). Секция памяти дописывается в конец системного
//! промпта при старте каждой сессии (TUI и `arch-ml run`) — всегда, даже
//! когда файла ещё нет: агент знает путь и правило «дописывать факт по
//! явной просьбе пользователя» («запомни …»). Пополнение — fs-инструментами
//! агента или командой `/memory add` (`arch-ml memory add`).
//!
//! Отсутствие файла или пустая память — не ошибка: харнесс работает без неё.

use std::path::Path;

use crate::error::{HarnessError, Result};

/// Заголовок секции памяти в системном промпте.
const SECTION_HEADER: &str = "# Память пользователя";

/// Читает файл памяти. Нет файла или он пустой/из пробелов — `Ok(None)`
/// (отсутствие памяти — штатная ситуация, а не сбой).
///
/// # Errors
/// Ошибка чтения существующего файла (права, кодировка и т.п.).
pub fn load(path: &Path) -> Result<Option<String>> {
    match std::fs::read_to_string(path) {
        Ok(text) => {
            let trimmed = text.trim();
            Ok((!trimmed.is_empty()).then(|| trimmed.to_string()))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(HarnessError::io(path, e)),
    }
}

/// Дописывает секцию памяти в конец системного промпта. Секция инжектится
/// всегда, даже когда памяти ещё нет: агент должен знать путь к файлу и
/// правило — пополнять память по явной просьбе пользователя («запомни …»).
#[must_use]
pub fn augment_system_prompt(base: &str, memory: Option<&str>, path: &Path) -> String {
    let content = memory.unwrap_or("(пока пуста)");
    format!(
        "{base}\n\n{SECTION_HEADER}\n\n{content}\n\n\
         Это твоя персистентная память между сессиями (файл `{path}`). \
         Когда пользователь просит что-то запомнить («запомни …», «добавь в память …») — \
         сразу дописывай факт в конец этого файла fs-инструментами (режим append; \
         файла нет — создай) и подтверждай запись. \
         Без явной просьбы пользователя память не пополняй.",
        path = path.display()
    )
}

/// Дописывает заметку в конец файла памяти (файл и родительский каталог
/// создаются при отсутствии). Между существующим содержимым и заметкой —
/// пустая строка-разделитель.
///
/// # Errors
/// Ошибка создания каталога или записи файла.
pub fn append(path: &Path, note: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
        }
    }
    let mut content = load(path)?.unwrap_or_default();
    if !content.is_empty() {
        content.push_str("\n\n");
    }
    content.push_str(note.trim());
    content.push('\n');
    std::fs::write(path, content).map_err(|e| HarnessError::io(path, e))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn load_missing_file_is_none() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let path = tmp.path().join("MEMORY.md");
        assert_eq!(load(&path).expect("load"), None);
    }

    #[test]
    fn load_empty_or_whitespace_file_is_none() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let path = tmp.path().join("MEMORY.md");
        std::fs::write(&path, "  \n\t \n").expect("write");
        assert_eq!(load(&path).expect("load"), None);
    }

    #[test]
    fn load_non_empty_file_returns_trimmed_content() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let path = tmp.path().join("MEMORY.md");
        std::fs::write(&path, "\n# Заметки\n\nпользователь — архитектор\n\n").expect("write");
        let got = load(&path).expect("load").expect("память есть");
        assert_eq!(got, "# Заметки\n\nпользователь — архитектор");
    }

    #[test]
    fn augment_without_memory_appends_empty_marker_and_rule() {
        let base = "Ты — solution-архитектор.";
        let path = Path::new("/tmp/MEMORY.md");
        let got = augment_system_prompt(base, None, path);
        assert!(got.starts_with(base), "база сохранена в начале: {got}");
        assert!(got.contains(SECTION_HEADER), "заголовок секции: {got}");
        assert!(got.contains("(пока пуста)"), "маркер пустой памяти: {got}");
        assert!(got.contains("/tmp/MEMORY.md"), "путь к файлу: {got}");
        assert!(got.contains("запомни"), "правило дописки по просьбе: {got}");
        assert!(got.contains("не пополняй"), "запрет самопополнения: {got}");
    }

    #[test]
    fn augment_with_memory_appends_section_and_instruction() {
        let base = "Ты — solution-архитектор.";
        let path = Path::new("/tmp/MEMORY.md");
        let got = augment_system_prompt(base, Some("любит саги"), path);
        assert!(got.starts_with(base), "база сохранена в начале: {got}");
        assert!(got.contains(SECTION_HEADER), "заголовок секции: {got}");
        assert!(got.contains("любит саги"), "содержимое памяти: {got}");
        assert!(got.contains("/tmp/MEMORY.md"), "путь к файлу: {got}");
        assert!(got.contains("персистентная память"), "инструкция: {got}");
        assert!(got.contains("запомни"), "правило дописки по просьбе: {got}");
    }

    #[test]
    fn append_creates_file_and_parent_dirs() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let path = tmp.path().join("nested/dir/MEMORY.md");
        append(&path, "первая заметка").expect("append");
        let text = std::fs::read_to_string(&path).expect("read");
        assert_eq!(text, "первая заметка\n");
    }

    #[test]
    fn append_separates_notes_with_blank_line() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let path = tmp.path().join("MEMORY.md");
        append(&path, "первая").expect("append 1");
        append(&path, "вторая").expect("append 2");
        let text = std::fs::read_to_string(&path).expect("read");
        assert_eq!(text, "первая\n\nвторая\n");
    }
}
