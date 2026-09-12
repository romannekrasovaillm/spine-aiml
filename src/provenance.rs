//! Provenance эксперимента — несжимаемый след «что именно гоняли».
//!
//! Инвариант «experiment-visible ⟺ logged» (расширение dsh-инварианта H1.4):
//! всё, что влияет на воспроизводимость прогона — модель, стек, образ, ресурс,
//! seed, git-коммит, драйвер — фиксируется в манифесте **до** запуска. Код
//! вендора этого не делает: у него «запустил и забыл». Здесь `experiment record`
//! пишет манифест, `experiment reproduce` перечитывает его и сверяет git-коммит
//! с текущим (дрейф → явное предупреждение, а не молчание).
//!
//! КОНТРАКТ (владелец: модуль `provenance`):
//! - [`capture`] — собрать манифест из [`ExperimentSpec`] + окружения (чистая сборка);
//! - [`record`] / [`load`] / [`list`] — файловое хранение в `state/experiments/`;
//! - [`render`] — рецепт воспроизведения;
//! - [`reproduce`] — рецепт + сверка git-дрейфа;
//! - [`parse_nvidia_header`] — парсер заголовка `nvidia-smi` (драйвер + CUDA).

use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::preflight::ExperimentSpec;

/// Манифест provenance одного эксперимента.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Provenance {
    /// Стабильный id (slug имени эксперимента).
    pub id: String,
    /// Имя эксперимента.
    pub experiment: String,
    /// Метка записи (RFC 3339).
    pub recorded_at: String,
    /// Модель: имя, параметры (млрд), dtype.
    pub model_name: Option<String>,
    pub params_b: Option<f64>,
    pub dtype: Option<String>,
    /// Стек.
    pub framework: Option<String>,
    pub verl_version: Option<String>,
    pub vllm_version: Option<String>,
    pub torch_version: Option<String>,
    pub cuda_version: Option<String>,
    pub image: Option<String>,
    /// Ресурс: local/cloud + устройство.
    pub resource: Option<String>,
    pub device: Option<String>,
    /// Seed (воспроизводимость).
    pub seed: Option<u64>,
    /// Git-состояние на момент записи.
    pub git_commit: Option<String>,
    pub git_dirty: bool,
    /// Драйвер/CUDA хоста (best-effort).
    pub nvidia_driver: Option<String>,
    pub nvidia_cuda: Option<String>,
    /// Драйвер/CUDA удалённого GPU (по SSH-алиасу, если ресурс — remote).
    pub remote_driver: Option<String>,
    pub remote_cuda: Option<String>,
}

/// Каталог манифестов под `state/experiments/`.
#[must_use]
pub fn experiments_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("experiments")
}

/// Слаг id из имени эксперимента (нижний регистр, `-` вместо прочего).
#[must_use]
pub fn slug(name: &str) -> String {
    let mut s = String::new();
    for c in name.chars() {
        if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
            s.push(c.to_ascii_lowercase());
        } else {
            s.push('-');
        }
    }
    let trimmed = s.trim_matches('-').to_string();
    if trimmed.is_empty() {
        "unnamed".into()
    } else {
        trimmed
    }
}

/// Собрать манифест из спецификации + окружения (git + nvidia). `remote_ssh` —
/// SSH-алиас удалённого GPU (для захвата его драйвера), `None` — только локальный.
#[must_use]
pub fn capture(spec: &ExperimentSpec, repo: &Path, remote_ssh: Option<&str>) -> Provenance {
    let (driver, cuda) = nvidia_info();
    let (remote_driver, remote_cuda) = match remote_ssh {
        Some(alias) => remote_nvidia_info(alias),
        None => (None, None),
    };
    Provenance {
        id: slug(&spec.name),
        experiment: spec.name.clone(),
        recorded_at: chrono::Utc::now().to_rfc3339(),
        model_name: spec.model_name.clone(),
        params_b: spec.params_b,
        dtype: spec.dtype.clone(),
        framework: spec.framework.clone(),
        verl_version: spec.verl_version.clone(),
        vllm_version: spec.vllm_version.clone(),
        torch_version: spec.torch_version.clone(),
        cuda_version: spec.cuda_version.clone(),
        image: spec.image.clone(),
        resource: spec.resource.clone(),
        device: spec.device.clone(),
        seed: spec.seed,
        git_commit: git_commit(repo),
        git_dirty: git_dirty(repo),
        nvidia_driver: driver,
        nvidia_cuda: cuda,
        remote_driver,
        remote_cuda,
    }
}

/// Записать манифест в `state/experiments/<id>.json` и вернуть путь.
///
/// # Errors
/// Ошибка, если каталог не создаётся или файл не пишется.
pub fn record(p: &Provenance, state_dir: &Path) -> Result<PathBuf> {
    let dir = experiments_dir(state_dir);
    std::fs::create_dir_all(&dir).context("создание каталога experiments")?;
    let path = dir.join(format!("{}.json", p.id));
    let json = serde_json::to_string_pretty(p).context("сериализация provenance")?;
    std::fs::write(&path, json).context("запись provenance")?;
    Ok(path)
}

/// Загрузить манифест по id.
///
/// # Errors
/// Ошибка, если файл не читается или не JSON.
pub fn load(id: &str, state_dir: &Path) -> Result<Provenance> {
    let path = experiments_dir(state_dir).join(format!("{id}.json"));
    let text =
        std::fs::read_to_string(&path).with_context(|| format!("чтение {}", path.display()))?;
    serde_json::from_str(&text).with_context(|| format!("разбор {}", path.display()))
}

/// Список записанных id экспериментов (по алфавиту).
///
/// # Errors
/// Ошибка, если каталог не читается.
pub fn list(state_dir: &Path) -> Result<Vec<String>> {
    let dir = experiments_dir(state_dir);
    if !dir.is_dir() {
        return Ok(Vec::new());
    }
    let mut ids = Vec::new();
    for entry in std::fs::read_dir(&dir).context("чтение каталога experiments")? {
        let entry = entry.context("чтение записи experiments")?;
        let name = entry.file_name().to_string_lossy().to_string();
        if let Some(stem) = name.strip_suffix(".json") {
            ids.push(stem.to_string());
        }
    }
    ids.sort();
    Ok(ids)
}

/// Рецепт воспроизведения (текст).
#[must_use]
pub fn render(p: &Provenance) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "Эксперимент: {}", p.experiment);
    let _ = writeln!(out, "Записан: {}", p.recorded_at);
    let _ = writeln!(out);
    if let Some(m) = &p.model_name {
        let mut line = format!("Модель: {m}");
        let mut parts = Vec::new();
        if let Some(b) = p.params_b {
            parts.push(format!("{b}B"));
        }
        if let Some(d) = &p.dtype {
            parts.push(d.clone());
        }
        if !parts.is_empty() {
            let _ = write!(line, " ({})", parts.join(", "));
        }
        let _ = writeln!(out, "{line}");
    }
    let versions: Vec<String> = [
        ("framework", p.framework.as_ref()),
        ("veRL", p.verl_version.as_ref()),
        ("vLLM", p.vllm_version.as_ref()),
        ("torch", p.torch_version.as_ref()),
        ("CUDA (стек)", p.cuda_version.as_ref()),
    ]
    .into_iter()
    .filter_map(|(k, v)| v.map(|v| format!("{k} {v}")))
    .collect();
    if !versions.is_empty() {
        let _ = writeln!(out, "Стек: {}", versions.join(", "));
    }
    if let Some(img) = &p.image {
        let _ = writeln!(out, "Образ: {img}");
    }
    if let (Some(r), Some(d)) = (&p.resource, &p.device) {
        let _ = writeln!(out, "Ресурс: {r} (device {d})");
    } else if let Some(r) = &p.resource {
        let _ = writeln!(out, "Ресурс: {r}");
    }
    if let Some(seed) = p.seed {
        let _ = writeln!(out, "Seed: {seed}");
    }
    let _ = writeln!(out);
    match &p.git_commit {
        Some(c) => {
            let _ = writeln!(
                out,
                "Git: {c} (дерево {}грязное)",
                if p.git_dirty { "" } else { "не " }
            );
        }
        None => {
            let _ = writeln!(out, "Git: не записан (репозиторий недоступен при записи)");
        }
    }
    if let (Some(d), Some(c)) = (&p.nvidia_driver, &p.nvidia_cuda) {
        let _ = writeln!(out, "Драйвер: {d} (CUDA {c})");
    }
    if let (Some(d), Some(c)) = (&p.remote_driver, &p.remote_cuda) {
        let _ = writeln!(out, "Драйвер (remote): {d} (CUDA {c})");
    }
    let _ = writeln!(
        out,
        "\nВоспроизведение: вернуть git-коммит, повторить стек/образ/seed выше."
    );
    out
}

/// Рецепт + сверка git-дрейфа с текущим репозиторием.
///
/// # Errors
/// Ошибка, если манифест с таким id не найден/не читается.
pub fn reproduce(id: &str, state_dir: &Path, repo: &Path) -> Result<String> {
    let p = load(id, state_dir)?;
    let mut out = render(&p);
    match (&p.git_commit, &git_commit(repo)) {
        (Some(rec), Some(cur)) if rec != cur => {
            let _ = writeln!(
                out,
                "\n⚠ ДРЕЙФ: коммит изменился\n  записан: {rec}\n  сейчас:  {cur}\n  вернуться: git checkout {rec}"
            );
        }
        (Some(rec), None) => {
            let _ = writeln!(
                out,
                "\n⚠ git недоступен сейчас (записан {rec}) — сверку не выполнить"
            );
        }
        (None, _) => {
            let _ = writeln!(
                out,
                "\n⚠ git-коммит не был записан — воспроизводимость неполная"
            );
        }
        _ => {}
    }
    if p.git_dirty {
        let _ = writeln!(
            out,
            "⚠ дерево было грязным при записи — чистый checkout не воспроизведёт результат"
        );
    }
    Ok(out)
}

/// Разобрать заголовок `nvidia-smi`: `(Driver Version, CUDA Version)`.
#[must_use]
pub fn parse_nvidia_header(text: &str) -> Option<(String, String)> {
    let line = text.lines().find(|l| l.contains("Driver Version:"))?;
    Some((
        extract_after(line, "Driver Version:")?,
        extract_after(line, "CUDA Version:")?,
    ))
}

/// Вытащить первый токен после ключа (до пробела/`|`).
fn extract_after(s: &str, key: &str) -> Option<String> {
    let rest = &s[s.find(key)? + key.len()..];
    rest.split_whitespace()
        .next()
        .map(|t| t.trim_end_matches('|').to_string())
}

/// git-коммит репозитория (`HEAD`), `None` — недоступен.
fn git_commit(repo: &Path) -> Option<String> {
    git_output(repo, &["rev-parse", "HEAD"])
}

/// Есть ли неотслеживаемые/изменённые файлы (дерево «грязное»).
fn git_dirty(repo: &Path) -> bool {
    git_output(repo, &["status", "--porcelain"]).is_some_and(|s| !s.is_empty())
}

/// Выполнить `git -C <repo> <args>` и вернуть stdout (trim), `None` при сбое.
fn git_output(repo: &Path, args: &[&str]) -> Option<String> {
    let mut cmd = std::process::Command::new("git");
    cmd.arg("-C").arg(repo).args(args);
    let out = cmd.output().ok()?;
    out.status
        .success()
        .then(|| String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Драйвер + CUDA локального хоста (best-effort, `None` без nvidia-smi).
fn nvidia_info() -> (Option<String>, Option<String>) {
    let Ok(out) = std::process::Command::new("nvidia-smi").output() else {
        return (None, None);
    };
    if !out.status.success() {
        return (None, None);
    }
    match parse_nvidia_header(&String::from_utf8_lossy(&out.stdout)) {
        Some((d, c)) => (Some(d), Some(c)),
        None => (None, None),
    }
}

/// Драйвер + CUDA удалённого GPU по SSH-алиасу (best-effort, `None` при недоступности).
fn remote_nvidia_info(alias: &str) -> (Option<String>, Option<String>) {
    let Ok(out) = crate::gpu::ssh_out(alias, "nvidia-smi") else {
        return (None, None);
    };
    match parse_nvidia_header(&out) {
        Some((d, c)) => (Some(d), Some(c)),
        None => (None, None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slug_normalizes_names() {
        assert_eq!(slug("GRPO-ALFWorld 3B"), "grpo-alfworld-3b");
        assert_eq!(slug("  "), "unnamed");
        assert_eq!(slug("my_experiment"), "my_experiment");
    }

    #[test]
    fn parse_nvidia_header_extracts_versions() {
        let text = "+------------------------------------------------------------------------+\n\
            | NVIDIA-SMI 580.173.02  Driver Version: 580.173.02  CUDA Version: 13.0  |\n";
        let (driver, cuda) = parse_nvidia_header(text).expect("parses");
        assert_eq!(driver, "580.173.02");
        assert_eq!(cuda, "13.0");
    }

    #[test]
    fn render_includes_reproducible_keys() {
        let p = Provenance {
            id: "demo".into(),
            experiment: "grpo-alfworld-3b".into(),
            recorded_at: "2026-09-12T02:40:00Z".into(),
            model_name: Some("Qwen2.5-3B-Instruct".into()),
            params_b: Some(3.0),
            dtype: Some("bf16".into()),
            framework: Some("verl".into()),
            verl_version: Some("0.4.0".into()),
            vllm_version: Some("0.8.3".into()),
            torch_version: None,
            cuda_version: Some("12.4".into()),
            image: None,
            resource: Some("local".into()),
            device: Some("gb10".into()),
            seed: Some(42),
            git_commit: Some("abc123".into()),
            git_dirty: false,
            nvidia_driver: Some("580.173.02".into()),
            nvidia_cuda: Some("13.0".into()),
            remote_driver: Some("580.173.02".into()),
            remote_cuda: Some("13.0".into()),
        };
        let out = render(&p);
        assert!(out.contains("Qwen2.5-3B-Instruct (3B, bf16)"));
        assert!(out.contains("veRL 0.4.0"));
        assert!(out.contains("Seed: 42"));
        assert!(out.contains("abc123"));
        assert!(out.contains("Драйвер: 580.173.02"));
        assert!(out.contains("Драйвер (remote): 580.173.02"));
    }

    #[test]
    fn record_load_roundtrip() {
        let tmp = tempfile::tempdir().expect("tmp");
        let spec = ExperimentSpec {
            name: "demo-exp".into(),
            params_b: Some(0.5),
            seed: Some(7),
            ..ExperimentSpec::default()
        };
        let p = capture(&spec, tmp.path(), None);
        assert_eq!(p.id, "demo-exp");
        let path = record(&p, tmp.path()).expect("record");
        assert!(path.ends_with("demo-exp.json"));
        let loaded = load("demo-exp", tmp.path()).expect("load");
        assert_eq!(loaded.seed, Some(7));
        assert_eq!(loaded.params_b, Some(0.5));
        assert!(
            list(tmp.path())
                .expect("list")
                .contains(&"demo-exp".to_string())
        );
    }
}
