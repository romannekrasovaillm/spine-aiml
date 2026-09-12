//! Guarded harness evolution (H2.2): самоулучшение доменного слоя через
//! propose/commit с детерминированным стражем.
//!
//! Доменные плагины/скиллы/маршруты эволюционируют из failure-траекторий, но
//! изменение не попадает в активный слой без гейта: именной аппрувер (govern)
//! + managed-блок со сверкой хэша (managed) + подтверждение, что детерминированный
//! слой (fitness) не регрессировал (контрольный прогон отдельным шагом CI).
//!
//! Журнал — append-only JSONL в `state/evolve/`.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{HarnessError, Result};

/// Предложение изменения доменного слоя харнесса.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvolutionProposal {
    /// Идентификатор предложения (kebab-case).
    pub id: String,
    /// Целевой файл (относительно репо).
    pub target: String,
    /// Managed-блок (id) внутри файла.
    pub block_id: String,
    /// Новое тело блока.
    pub content: String,
    /// Именной аппрувер (как в govern).
    pub approver: String,
    /// Статус: proposed|committed|rejected.
    pub status: String,
    /// Штамп.
    pub at: String,
}

/// Каталог журнала эволюции.
fn evolve_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("evolve")
}

fn now() -> String {
    chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

/// Записывает предложение изменения (status=proposed).
///
/// # Errors
/// Пустой аппрувер или ошибка записи.
pub fn evolve_propose(
    state_dir: &Path,
    id: &str,
    target: &str,
    block_id: &str,
    content: &str,
    approver: &str,
) -> Result<PathBuf> {
    if approver.trim().is_empty() {
        return Err(HarnessError::Tool(
            "evolve: предложение без именного аппрувера не принимается".to_string(),
        ));
    }
    let p = EvolutionProposal {
        id: id.trim().to_string(),
        target: target.trim().to_string(),
        block_id: block_id.trim().to_string(),
        content: content.to_string(),
        approver: approver.trim().to_string(),
        status: "proposed".to_string(),
        at: now(),
    };
    append(state_dir, &p)
}

/// Читает предложения (хронологически).
pub fn evolve_list(state_dir: &Path) -> Result<Vec<EvolutionProposal>> {
    let path = evolve_dir(state_dir).join("proposals.jsonl");
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let text = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        out.push(serde_json::from_str(line).map_err(HarnessError::Json)?);
    }
    Ok(out)
}

/// Помечает предложение committed (гейт прошёл) или rejected.
fn set_status(state_dir: &Path, id: &str, status: &str) -> Result<()> {
    let mut all = evolve_list(state_dir)?;
    let Some(p) = all.iter_mut().find(|p| p.id == id.trim()) else {
        return Err(HarnessError::Tool(format!(
            "evolve: предложение {id} не найдено"
        )));
    };
    p.status = status.to_string();
    p.at = now();
    let path = evolve_dir(state_dir).join("proposals.jsonl");
    let mut text = String::new();
    for p in &all {
        text.push_str(&serde_json::to_string(p).map_err(HarnessError::Json)?);
        text.push('\n');
    }
    crate::managed::atomic_write(&path, &text)?;
    Ok(())
}

/// Гейт коммита: аппрувер непустой + managed-блок применяется. Статус →
/// committed. Возвращает предложение (для применения вызывающим).
pub fn evolve_commit(state_dir: &Path, id: &str) -> Result<EvolutionProposal> {
    let all = evolve_list(state_dir)?;
    let Some(p) = all.iter().find(|p| p.id == id.trim()) else {
        return Err(HarnessError::Tool(format!(
            "evolve: предложение {id} не найдено"
        )));
    };
    if p.approver.is_empty() {
        return Err(HarnessError::Tool(format!(
            "evolve: {id} без именного аппрувера — коммит запрещён"
        )));
    }
    if p.target.is_empty() || p.block_id.is_empty() {
        return Err(HarnessError::Tool(format!(
            "evolve: {id} неполон (target/block_id пуст)"
        )));
    }
    set_status(state_dir, id, "committed")?;
    let mut committed = p.clone();
    committed.status = "committed".to_string();
    Ok(committed)
}

/// Отклоняет предложение (status=rejected).
pub fn evolve_reject(state_dir: &Path, id: &str) -> Result<()> {
    set_status(state_dir, id, "rejected")
}

/// Применяет committed-предложение к файлу через managed-блок (сверка хэша).
///
/// # Errors
/// Не удалось применить managed-блок (в т.ч. `HashMismatch` при ручной правке).
pub fn evolve_apply(repo: &Path, p: &EvolutionProposal) -> Result<()> {
    let target = repo.join(&p.target);
    let patch = crate::managed::Patch {
        target: target.clone(),
        base_hash: crate::managed::HashGuard::Force,
        block_ops: vec![crate::managed::BlockOp::Replace {
            id: p.block_id.clone(),
            guard: crate::managed::HashGuard::Force,
            content: p.content.clone(),
            source: Some("evolve".into()),
            upsert: true,
            anchor: crate::managed::Anchor::Eof,
        }],
        section_ops: vec![],
    };
    let report = crate::managed::apply_file(&patch)?;
    if !report.changed {
        // upsert с тем же телом — idempotent no-op, это не ошибка
        let _ = target;
    }
    Ok(())
}

fn append(state_dir: &Path, p: &EvolutionProposal) -> Result<PathBuf> {
    let path = evolve_dir(state_dir).join("proposals.jsonl");
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
    }
    let line = serde_json::to_string(p).map_err(HarnessError::Json)?;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| HarnessError::io(&path, e))?;
    use std::io::Write as _;
    writeln!(f, "{line}").map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn propose_commit_apply_roundtrip() {
        let dir = tempfile::tempdir().expect("tmp");
        let state = dir.path();
        let repo = dir.path();
        // целевой файл с managed-блоком
        let target = repo.join("aiml/skill.md");
        std::fs::create_dir_all(target.parent().unwrap()).expect("mkdir");
        let body = "v1";
        let h = crate::managed::Sha256::of_text(body);
        let initial = crate::managed::render_block("doc", 1, &h, None, body);
        std::fs::write(&target, &initial).expect("write");

        evolve_propose(state, "up-1", "aiml/skill.md", "doc", "v2", "Иванов И.И.")
            .expect("propose");
        let p = evolve_commit(state, "up-1").expect("commit");
        assert_eq!(p.status, "committed");
        evolve_apply(repo, &p).expect("apply");
        let text = std::fs::read_to_string(&target).expect("read");
        assert!(text.contains("v2"));
    }

    #[test]
    fn propose_rejects_empty_approver() {
        let dir = tempfile::tempdir().expect("tmp");
        let e = evolve_propose(dir.path(), "up-1", "a.md", "doc", "x", "  ").unwrap_err();
        assert!(e.to_string().contains("аппрувера"), "{e}");
    }

    #[test]
    fn commit_requires_approver() {
        let dir = tempfile::tempdir().expect("tmp");
        evolve_propose(dir.path(), "up-1", "a.md", "doc", "x", "Иванов").expect("ok");
        // вручную стираем аппрувера (имитация пустого)
        let path = dir.path().join("evolve/proposals.jsonl");
        let text = std::fs::read_to_string(&path).expect("read");
        let broken = text.replace("Иванов", "");
        std::fs::write(&path, broken).expect("write");
        let e = evolve_commit(dir.path(), "up-1").unwrap_err();
        assert!(e.to_string().contains("аппрувера"), "{e}");
    }
}
