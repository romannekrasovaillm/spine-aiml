//! Evidence Bundle — аудиторский след как условие выпуска (AI-Disrupt PDLC):
//! не «отчёт после», а гейт релиза. `pack` собирает манифест с хэшами
//! артефактов, `verify` проверяет полноту по профилю маршрута
//! (Fast/Standard/Critical) и целостность хэшей.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::control::Route;
use crate::error::{HarnessError, Result};

/// Запись манифеста об одном артефакте.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvidenceItem {
    /// Ключ артефакта (spec, adr, spine, `decision_a3`…).
    pub key: String,
    /// Путь относительно каталога изменения.
    pub path: String,
    /// FNV-1a хэш содержимого на момент упаковки.
    pub hash: String,
    /// Размер в байтах.
    pub size: u64,
}

/// Манифест Evidence Bundle (`EVIDENCE.yaml`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvidenceBundle {
    /// Маршрут значимости (профиль полноты).
    pub route: String,
    /// Метка времени упаковки.
    pub packed_at: String,
    /// Артефакты.
    pub items: Vec<EvidenceItem>,
}

/// Результат проверки.
#[derive(Debug, Clone)]
pub struct EvidenceVerdict {
    /// Полнота и целостность подтверждены.
    pub passed: bool,
    /// Отсутствующие обязательные артефакты.
    pub missing: Vec<String>,
    /// Артефакты с изменённым хэшем (подмена/дрейф после упаковки).
    pub tampered: Vec<String>,
    /// Сводка для отчёта.
    pub summary: String,
}

/// Обязательные артефакты по маршруту (из обзора AI-Disrupt: объектный минимум
/// разделяют все режимы; Standard/Critical добавляют evidence-проверки).
fn required_artifacts(route: Route) -> Vec<(&'static str, &'static str)> {
    // (ключ, человеко-читаемое описание)
    let mut base = vec![
        ("problem", "формулировка проблемы/гипотезы результата"),
        ("spec_or_delta", "спецификация или дельта"),
        ("risk_level", "уровень риска (significance score)"),
        ("acceptance", "критерии приёмки"),
        ("rollback", "план отката"),
    ];
    match route {
        Route::Fast => {}
        Route::Standard => base.extend([
            ("adr_or_pattern", "ссылка на паттерн или ADR"),
            ("validation", "доказательства валидации (тесты/отчёт)"),
            ("fitness_report", "прогон fitness functions"),
        ]),
        Route::Critical => base.extend([
            ("adr_or_pattern", "ADR с оценкой обратимости"),
            ("spine", "ARCHITECTURE-SPINE с затронутыми инвариантами"),
            (
                "decision_a3",
                "запись человеческого решения A3 (choice/rationale/rejected/expiry)",
            ),
            ("walking_skeleton", "отчёт walking skeleton"),
            (
                "adversarial_review",
                "вердикт состязательного ревью READY/NOT-READY",
            ),
            (
                "rollback_rehearsal",
                "репетиция отката на гейте A4 (.arch-handoff/REHEARSAL.json, PASS)",
            ),
            ("validation", "доказательства валидации (тесты/отчёт)"),
            ("fitness_report", "прогон fitness functions"),
        ]),
    }
    base
}

/// Канонические расположения артефактов в каталоге изменения.
fn candidate_paths(key: &str) -> Vec<&'static str> {
    match key {
        "problem" => vec!["PROBLEM.md", "SPEC.md", "docs/PROBLEM.md", "DELTA.md"],
        "spec_or_delta" => vec!["SPEC.md", "DELTA.md", "docs/SPEC.md", "docs/specs/SPEC.md"],
        "risk_level" => vec!["RISK.md", "SCORE.md", "docs/RISK.md"],
        "acceptance" => vec!["ACCEPTANCE.md", "SPEC.md", "docs/ACCEPTANCE.md"],
        "rollback" => vec!["ROLLBACK.md", "docs/ROLLBACK.md", "PLAN.md"],
        "rollback_rehearsal" => vec![".arch-handoff/REHEARSAL.json", "REHEARSAL.json"],
        "adr_or_pattern" => vec!["docs/adr", "adr", "ADR.md"],
        "spine" => vec!["docs/ARCHITECTURE-SPINE.md", "ARCHITECTURE-SPINE.md"],
        "decision_a3" => vec!["DECISION.md", "docs/DECISION.md", "A3.md"],
        "walking_skeleton" => vec!["WALKING-SKELETON.md", "docs/WALKING-SKELETON.md"],
        "adversarial_review" => vec!["REVIEW.md", "docs/REVIEW.md", "reports/review.md"],
        "validation" => vec!["VALIDATION.md", "reports/tests.md", "docs/VALIDATION.md"],
        "fitness_report" => vec!["reports/fitness.md", "FITNESS.md", "docs/FITNESS.md"],
        _ => vec![],
    }
}

/// FNV-1a 64 — детекция изменения артефакта после упаковки.
fn hash_file(path: &Path) -> Result<(String, u64)> {
    let bytes = std::fs::read(path).map_err(|e| HarnessError::io(path, e))?;
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for b in &bytes {
        hash ^= u64::from(*b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    Ok((format!("{hash:016x}"), bytes.len() as u64))
}

/// Ищет артефакт по каноническим путям (файл или каталог с ≥1 md).
fn find_artifact(change_dir: &Path, key: &str) -> Option<PathBuf> {
    for cand in candidate_paths(key) {
        let p = change_dir.join(cand);
        if p.is_file() {
            return Some(p);
        }
        if p.is_dir() {
            let has_md = std::fs::read_dir(&p).is_ok_and(|rd| {
                rd.flatten()
                    .any(|e| e.path().extension().is_some_and(|x| x == "md"))
            });
            if has_md {
                // Каталог (напр. docs/adr) — хэшируем сводку содержимого.
                return Some(p);
            }
        }
    }
    None
}

/// Хэш артефакта: файла — содержимого; каталога — имён+хэшей содержимого.
fn hash_artifact(path: &Path) -> Result<(String, u64)> {
    if path.is_file() {
        return hash_file(path);
    }
    let mut acc = String::new();
    let mut size = 0u64;
    let mut entries: Vec<PathBuf> = std::fs::read_dir(path)
        .map_err(|e| HarnessError::io(path, e))?
        .flatten()
        .map(|e| e.path())
        .collect();
    entries.sort();
    for e in entries {
        if e.is_file() {
            let (h, s) = hash_file(&e)?;
            let _ = write!(acc, "{}:{h};", e.display());
            size += s;
        }
    }
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for b in acc.as_bytes() {
        hash ^= u64::from(*b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    Ok((format!("{hash:016x}"), size))
}

/// Собирает Evidence Bundle: манифест `EVIDENCE.yaml` в каталоге изменения.
///
/// # Errors
/// Каталог недоступен, ошибка записи манифеста.
pub fn pack(change_dir: &Path, route: Route) -> Result<(EvidenceBundle, EvidenceVerdict)> {
    let mut items = Vec::new();
    let mut missing = Vec::new();
    for (key, _desc) in required_artifacts(route) {
        match find_artifact(change_dir, key) {
            Some(path) => {
                let (hash, size) = hash_artifact(&path)?;
                items.push(EvidenceItem {
                    key: key.into(),
                    path: path
                        .strip_prefix(change_dir)
                        .map_or_else(|_| path.display().to_string(), |p| p.display().to_string()),
                    hash,
                    size,
                });
            }
            None => missing.push(key.to_string()),
        }
    }
    let bundle = EvidenceBundle {
        route: format!("{route:?}"),
        packed_at: chrono::Local::now().to_rfc3339(),
        items,
    };
    let manifest = change_dir.join("EVIDENCE.yaml");
    let text = serde_yaml_ng::to_string(&bundle)
        .map_err(|e| HarnessError::Config(format!("сериализация EVIDENCE: {e}")))?;
    std::fs::write(&manifest, text).map_err(|e| HarnessError::io(&manifest, e))?;
    let verdict = EvidenceVerdict {
        passed: missing.is_empty(),
        summary: format!(
            "Evidence Bundle ({route:?}): артефактов {}, отсутствует {}",
            bundle.items.len(),
            missing.len()
        ),
        missing,
        tampered: Vec::new(),
    };
    Ok((bundle, verdict))
}

/// Проверяет bundle: обязательные артефакты на месте, хэши совпадают.
///
/// # Errors
/// Манифест отсутствует/не валиден.
pub fn verify(change_dir: &Path) -> Result<EvidenceVerdict> {
    let manifest = change_dir.join("EVIDENCE.yaml");
    let text = std::fs::read_to_string(&manifest).map_err(|e| HarnessError::io(&manifest, e))?;
    let bundle: EvidenceBundle = serde_yaml_ng::from_str(&text)?;
    let route = match bundle.route.as_str() {
        "Standard" => Route::Standard,
        "Critical" => Route::Critical,
        _ => Route::Fast,
    };
    let mut missing = Vec::new();
    let mut tampered = Vec::new();
    for (key, _desc) in required_artifacts(route) {
        if !bundle.items.iter().any(|i| i.key == key) {
            missing.push(key.to_string());
        }
    }
    for item in &bundle.items {
        let path = change_dir.join(&item.path);
        if !path.exists() {
            tampered.push(format!("{} (удалён: {})", item.key, item.path));
            continue;
        }
        let (hash, _) = hash_artifact(&path)?;
        if hash != item.hash {
            tampered.push(format!(
                "{} (изменён после упаковки: {})",
                item.key, item.path
            ));
        }
    }
    let passed = missing.is_empty() && tampered.is_empty();
    Ok(EvidenceVerdict {
        passed,
        summary: format!(
            "Проверка bundle ({}): артефактов {}, отсутствует {}, изменено {}",
            bundle.route,
            bundle.items.len(),
            missing.len(),
            tampered.len()
        ),
        missing,
        tampered,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn put(root: &Path, rel: &str, content: &str) {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().expect("parent")).expect("mkdir");
        std::fs::write(p, content).expect("write");
    }

    #[test]
    fn fast_route_packs_minimal_bundle() {
        let tmp = tempfile::tempdir().expect("tmp");
        let dir = tmp.path();
        put(dir, "PROBLEM.md", "# Проблема\n");
        put(
            dir,
            "SPEC.md",
            "## Проблема\n## Критерии приёмки\n## Риски\n",
        );
        put(dir, "RISK.md", "Fast: 0 триггеров\n");
        put(dir, "ROLLBACK.md", "git revert\n");
        let (bundle, verdict) = pack(dir, Route::Fast).expect("pack");
        assert!(verdict.passed, "missing: {:?}", verdict.missing);
        assert!(bundle.items.len() >= 5, "items: {}", bundle.items.len());
        assert!(dir.join("EVIDENCE.yaml").is_file());
        // Проверка чиста сразу после упаковки.
        let v = verify(dir).expect("verify");
        assert!(v.passed, "{:?} {:?}", v.missing, v.tampered);
    }

    #[test]
    fn critical_route_requires_a3_and_spine() {
        let tmp = tempfile::tempdir().expect("tmp");
        let dir = tmp.path();
        put(dir, "PROBLEM.md", "x");
        let (_bundle, verdict) = pack(dir, Route::Critical).expect("pack");
        assert!(!verdict.passed);
        assert!(verdict.missing.contains(&"decision_a3".to_string()));
        assert!(verdict.missing.contains(&"spine".to_string()));
        assert!(verdict.missing.contains(&"adversarial_review".to_string()));
        // Critical требует и evidence репетиции отката (гейт A4).
        assert!(verdict.missing.contains(&"rollback_rehearsal".to_string()));
    }

    #[test]
    fn verify_detects_tampering_after_pack() {
        let tmp = tempfile::tempdir().expect("tmp");
        let dir = tmp.path();
        put(dir, "PROBLEM.md", "исходная проблема");
        put(dir, "SPEC.md", "спека");
        put(dir, "RISK.md", "r");
        put(dir, "ROLLBACK.md", "rb");
        pack(dir, Route::Fast).expect("pack");
        // Подмена артефакта после упаковки.
        put(dir, "SPEC.md", "ТИХО ПЕРЕПИСАЛИ");
        let v = verify(dir).expect("verify");
        assert!(!v.passed);
        assert!(
            v.tampered.iter().any(|t| t.contains("spec_or_delta")),
            "{:?}",
            v.tampered
        );
    }
}
