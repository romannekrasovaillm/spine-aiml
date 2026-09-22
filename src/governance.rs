//! Губернанс AI/ML-редакции: формальная подотчётность (govern) и независимый
//! eval-бар (accept).
//!
//! Два белых пятна, которые не закрывает ни один вендор (H1.1/H1.2):
//! - **govern** — именной аппрувер + журнал решений, привязанный к версии
//!   спека. Подотчётность фиксируется человеком, а не декларируется моделью.
//! - **accept** — человеко-заданные приёмочные тесты записываются ДО
//!   генерации и становятся гейтом (независимый eval-бар).
//!
//! Журналы — append-only JSONL в `state/governance/` (путь — `paths.state_dir`).
//! EU AI Act — только фрейминг; комплаенс-чеклистов здесь нет.

use std::io::Write as _;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{HarnessError, Result};

/// Запись решения (govern).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DecisionRecord {
    /// Имя аппрувера (человек, ФИО/роль).
    pub approver: String,
    /// Версия спека (`spec_version` или хэш), к которой привязано решение.
    pub spec_version: String,
    /// Артефакт, к которому относится решение (путь относительно репо).
    pub artifact: Option<String>,
    /// Текст решения.
    pub decision: String,
    /// Штамп времени.
    pub at: String,
}

/// Запись приёмочных тестов (accept).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AcceptanceRecord {
    /// Версия спека, к которой относятся тесты.
    pub spec_version: String,
    /// Человеко-заданные приёмочные критерии/тесты.
    pub tests: Vec<String>,
    /// Штамп.
    pub at: String,
}

/// Каталог журналов губернанса.
fn gov_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("governance")
}

/// Штамп времени для записи.
fn now() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// Дописывает решение в журнал (append-only JSONL).
///
/// # Errors
/// Не удалось создать каталог или записать файл.
pub fn govern_record(
    state_dir: &Path,
    approver: &str,
    spec_version: &str,
    artifact: Option<&str>,
    decision: &str,
) -> Result<PathBuf> {
    if approver.trim().is_empty() {
        return Err(HarnessError::Tool(
            "govern: approver пуст — подотчётность требует именного аппрувера".to_string(),
        ));
    }
    let rec = DecisionRecord {
        approver: approver.trim().to_string(),
        spec_version: spec_version.trim().to_string(),
        artifact: artifact.map(|a| a.trim().to_string()),
        decision: decision.trim().to_string(),
        at: now(),
    };
    let path = gov_dir(state_dir).join("decisions.jsonl");
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
    }
    let line = serde_json::to_string(&rec).map_err(HarnessError::Json)?;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| HarnessError::io(&path, e))?;
    writeln!(f, "{line}").map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

/// Читает журнал решений (хронологически).
///
/// # Errors
/// Файл не читается (отсутствие — пустой список, не ошибка).
pub fn govern_show(state_dir: &Path) -> Result<Vec<DecisionRecord>> {
    read_jsonl::<DecisionRecord>(&gov_dir(state_dir).join("decisions.jsonl"))
}

/// Проверяет подотчётность артефакта: есть запись с именным аппрувером и
/// версией спека (по `artifact`, либо любая запись, если `artifact` не задан).
///
/// # Errors
/// Файл журнала не читается.
pub fn govern_verify(state_dir: &Path, artifact: Option<&str>) -> Result<bool> {
    let recs = govern_show(state_dir)?;
    Ok(recs.iter().any(|r| {
        !r.approver.is_empty()
            && !r.spec_version.is_empty()
            && (artifact.is_none()
                || r.artifact.as_deref() == artifact
                || artifact.is_some_and(|a| r.artifact.as_deref() == Some(a.trim())))
    }))
}

/// Записывает человеко-заданные приёмочные тесты ДО генерации.
///
/// # Errors
/// Пустой список тестов или ошибка записи.
pub fn accept_set(state_dir: &Path, spec_version: &str, tests: Vec<String>) -> Result<PathBuf> {
    let tests: Vec<String> = tests
        .into_iter()
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect();
    if tests.is_empty() {
        return Err(HarnessError::Tool(
            "accept: список приёмочных тестов пуст — eval-бар без тестов бессмыслен".to_string(),
        ));
    }
    let rec = AcceptanceRecord {
        spec_version: spec_version.trim().to_string(),
        tests,
        at: now(),
    };
    let path = gov_dir(state_dir).join("acceptance.jsonl");
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
    }
    let line = serde_json::to_string(&rec).map_err(HarnessError::Json)?;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| HarnessError::io(&path, e))?;
    writeln!(f, "{line}").map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

/// Читает приёмочные тесты (хронологически).
///
/// # Errors
/// Файл не читается.
pub fn accept_show(state_dir: &Path) -> Result<Vec<AcceptanceRecord>> {
    read_jsonl::<AcceptanceRecord>(&gov_dir(state_dir).join("acceptance.jsonl"))
}

/// Гейт accept: для версии спека есть человеко-заданные тесты.
///
/// # Errors
/// Файл не читается.
pub fn accept_check(state_dir: &Path, spec_version: &str) -> Result<bool> {
    let recs = accept_show(state_dir)?;
    Ok(recs
        .iter()
        .any(|r| r.spec_version == spec_version.trim() && !r.tests.is_empty()))
}

/// Читает JSONL-файл в список записей; отсутствие файла — пустой список.
fn read_jsonl<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<Vec<T>> {
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let rec: T = serde_json::from_str(line).map_err(HarnessError::Json)?;
        out.push(rec);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn govern_record_and_verify_roundtrip() {
        let dir = tempfile::tempdir().expect("tmp");
        let state = dir.path();
        let p = govern_record(state, "Иванов И.И.", "spec-v1", Some("ADR-014"), "принять")
            .expect("record");
        assert!(p.is_file());
        let recs = govern_show(state).expect("show");
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].approver, "Иванов И.И.");
        assert!(govern_verify(state, Some("ADR-014")).expect("verify"));
        assert!(!govern_verify(state, Some("ADR-999")).expect("verify"));
    }

    #[test]
    fn govern_rejects_empty_approver() {
        let dir = tempfile::tempdir().expect("tmp");
        let e = govern_record(dir.path(), "  ", "spec-v1", None, "x").unwrap_err();
        assert!(e.to_string().contains("именного"), "{e}");
    }

    #[test]
    fn accept_set_and_check_roundtrip() {
        let dir = tempfile::tempdir().expect("tmp");
        let tests = vec!["нет data race".to_string(), "идемпотентен".to_string()];
        accept_set(dir.path(), "spec-v1", tests).expect("set");
        assert!(accept_check(dir.path(), "spec-v1").expect("check"));
        assert!(!accept_check(dir.path(), "spec-v2").expect("check"));
    }

    #[test]
    fn accept_rejects_empty_tests() {
        let dir = tempfile::tempdir().expect("tmp");
        let e = accept_set(dir.path(), "spec-v1", vec!["  ".to_string()]).unwrap_err();
        assert!(e.to_string().contains("пуст"), "{e}");
    }
}
