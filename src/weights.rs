//! Реестр артефактов ML-контура: веса, датасеты, токенизаторы.
//!
//! Манифест (`artifacts.yaml`) объявляет артефакты, которыми живёт контур
//! обучения и инференса. Проверка механическая: артефакт на диске должен
//! лежать симлинком, а не копией в рабочем дереве; `sha256` сверяется через
//! subprocess `sha256sum` (в стеке нет `sha2` и он не заводится).
//!
//! ЗАФИКСИРОВАННАЯ СЕМАНТИКА [`verify`]:
//! - артефакт не найден → [`CheckStatus::Warn`] «объявлен, не найден»
//!   (артефакты контура на диске пока отсутствуют — это НЕ провал);
//! - найден симлинком → размещение в порядке ([`CheckStatus::Ok`]); если цель
//!   симлинка лежит внутри `repo_root`, в detail добавляется предупреждение
//!   (копия в рабочем дереве недопустима по C-032);
//! - найден регулярным файлом при `kind = weights | dataset` →
//!   [`CheckStatus::Fail`] «копия в рабочем дереве»;
//! - `sha256` объявлен → сверить через subprocess `sha256sum`; несовпадение →
//!   [`CheckStatus::Fail`]; невозможность сверки (нет `sha256sum`, битый
//!   симлинк) → [`CheckStatus::Warn`], а не выдуманный провал;
//! - `sha256` не объявлен → [`CheckStatus::Warn`] «хеш не объявлен».
//!
//! Статус одной проверки — наиболее серьёзная находка (`Fail` > `Warn` >
//! `Ok`), детали находок накапливаются в [`Check::detail`] через `;`.

use std::fmt::Write as _;
use std::path::{Component, Path, PathBuf};
use std::process::Command;

use anyhow::Context;

/// Имя манифеста артефактов по умолчанию (в текущем каталоге).
pub const DEFAULT_MANIFEST: &str = "artifacts.yaml";

/// Роль артефакта в контуре.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ArtifactKind {
    /// Веса модели (чекпоинт/адаптер).
    Weights,
    /// Датасет (обучающий/оценочный).
    Dataset,
    /// Токенизатор.
    Tokenizer,
    /// Прочее (конфиг, словарь, индекс).
    Other,
}

/// Объявление одного артефакта в манифесте.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct Artifact {
    /// Идентификатор артефакта (уникален в манифесте).
    pub id: String,
    /// Роль артефакта; `None` — не объявлена.
    #[serde(default)]
    pub kind: Option<ArtifactKind>,
    /// Путь к артефакту (относительный — от корня репозитория).
    pub path: std::path::PathBuf,
    /// Ожидаемый `sha256` (hex), если объявлен.
    #[serde(default)]
    pub sha256: Option<String>,
    /// Лицензия артефакта, если объявлена.
    #[serde(default)]
    pub license: Option<String>,
    /// Источник (URL/имя ревизии), если объявлен.
    #[serde(default)]
    pub source: Option<String>,
    /// Размер в байтах, если объявлен.
    #[serde(default)]
    pub bytes: Option<u64>,
}

/// Манифест артефактов (`artifacts.yaml`).
#[derive(Debug, Clone, Default, serde::Deserialize)]
pub struct Manifest {
    /// Объявленные артефакты.
    #[serde(default)]
    pub artifacts: Vec<Artifact>,
}

/// Итог одной проверки артефакта.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CheckStatus {
    /// Проверка пройдена.
    Ok,
    /// Замечание: не провал, но требует внимания.
    Warn,
    /// Провал: механическое нарушение контракта артефактов.
    Fail,
}

/// Одна запись отчёта проверки.
#[derive(Debug, Clone)]
pub struct Check {
    /// Идентификатор артефакта (или синтетическая метка).
    pub id: String,
    /// Статус проверки.
    pub status: CheckStatus,
    /// Человекочитаемая деталь.
    pub detail: String,
}

/// Отчёт проверки манифеста.
#[derive(Debug, Clone, Default)]
pub struct VerifyReport {
    /// Проверки (по одной на артефакт и на общие условия).
    pub checks: Vec<Check>,
}

impl VerifyReport {
    /// Число проверок по статусам: `(ok, warn, fail)`.
    #[must_use]
    pub fn counts(&self) -> (usize, usize, usize) {
        let mut ok = 0;
        let mut warn = 0;
        let mut fail = 0;
        for c in &self.checks {
            match c.status {
                CheckStatus::Ok => ok += 1,
                CheckStatus::Warn => warn += 1,
                CheckStatus::Fail => fail += 1,
            }
        }
        (ok, warn, fail)
    }

    /// Есть ли хотя бы один провал ([`CheckStatus::Fail`]).
    #[must_use]
    pub fn has_failures(&self) -> bool {
        self.checks.iter().any(|c| c.status == CheckStatus::Fail)
    }
}

/// Разобрать манифест из текста YAML.
///
/// # Errors
/// Возвращает ошибку, если YAML не разбирается в [`Manifest`].
pub fn parse_manifest_yaml(text: &str) -> anyhow::Result<Manifest> {
    if text.trim().is_empty() {
        return Ok(Manifest::default());
    }
    serde_yaml_ng::from_str(text).context("разбор манифеста артефактов (YAML)")
}

/// Прочитать и разобрать манифест с диска.
///
/// # Errors
/// Ошибка чтения файла или разбора YAML.
pub fn load_manifest(path: &std::path::Path) -> anyhow::Result<Manifest> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("чтение манифеста {}", path.display()))?;
    parse_manifest_yaml(&text)
}

/// Проверить манифест против файловой системы.
///
/// Семантика зафиксирована в doc-комментарии модуля: сначала размещение
/// (симлинк против копии в рабочем дереве), затем сверка `sha256` через
/// `sha256sum`.
#[must_use]
pub fn verify(manifest: &Manifest, repo_root: &std::path::Path) -> VerifyReport {
    let root = canonical_or_lexical(repo_root);
    let checks = manifest
        .artifacts
        .iter()
        .map(|a| check_artifact(a, &root))
        .collect();
    VerifyReport { checks }
}

/// Отрисовать отчёт проверки в текст.
#[must_use]
pub fn render_report(r: &VerifyReport) -> String {
    let (ok, warn, fail) = r.counts();
    let mut out = String::new();
    let _ = writeln!(out, "Артефакты: ok {ok}, warn {warn}, fail {fail}");
    for c in &r.checks {
        let _ = writeln!(out, "  [{:?}] {} — {}", c.status, c.id, c.detail);
    }
    out
}

/// Проверить один артефакт: размещение и (при объявлении) `sha256`.
fn check_artifact(a: &Artifact, repo_root: &Path) -> Check {
    let path = resolve_path(repo_root, &a.path);
    let Ok(meta) = std::fs::symlink_metadata(&path) else {
        return Check {
            id: a.id.clone(),
            status: CheckStatus::Warn,
            detail: format!("объявлен, не найден: {}", path.display()),
        };
    };

    let mut status = CheckStatus::Ok;
    let mut notes: Vec<String> = Vec::new();
    let is_link = meta.file_type().is_symlink();

    if is_link {
        match std::fs::read_link(&path) {
            Ok(target) => {
                notes.push(format!("симлинк → {}", target.display()));
                let resolved = resolve_link_target(&path, &target);
                if resolved.starts_with(repo_root) {
                    notes.push(format!(
                        "цель симлинка внутри рабочего дерева ({}): копия недопустима по C-032",
                        resolved.display()
                    ));
                } else if !resolved.exists() {
                    notes.push(format!(
                        "цель симлинка не существует: {}",
                        resolved.display()
                    ));
                }
            }
            Err(e) => notes.push(format!("симлинк не читается: {e}")),
        }
    } else if meta.is_file() {
        if matches!(a.kind, Some(ArtifactKind::Weights | ArtifactKind::Dataset)) {
            status = CheckStatus::Fail;
            notes.push(format!("копия в рабочем дереве: {}", path.display()));
        } else {
            notes.push(format!("регулярный файл: {}", path.display()));
        }
    } else {
        status = CheckStatus::Warn;
        notes.push(format!("не файл и не симлинк: {}", path.display()));
    }

    let declared = a.sha256.as_deref().map(str::trim).filter(|s| !s.is_empty());
    if let Some(expected) = declared {
        if meta.is_file() || is_link {
            match sha256_of(&path) {
                Ok(actual) if actual.eq_ignore_ascii_case(expected) => {
                    notes.push(format!("sha256 совпал: {actual}"));
                }
                Ok(actual) => {
                    status = CheckStatus::Fail;
                    notes.push(format!(
                        "sha256 не совпал: ожидался {expected}, получен {actual}"
                    ));
                }
                Err(e) => {
                    if status != CheckStatus::Fail {
                        status = CheckStatus::Warn;
                    }
                    notes.push(format!("sha256 не сверен: {e}"));
                }
            }
        } else if status != CheckStatus::Fail {
            status = CheckStatus::Warn;
            notes.push("sha256 не сверен: путь не является файлом".to_string());
        }
    } else {
        if status == CheckStatus::Ok {
            status = CheckStatus::Warn;
        }
        notes.push("хеш не объявлен".to_string());
    }

    Check {
        id: a.id.clone(),
        status,
        detail: notes.join("; "),
    }
}

/// Путь артефакта: абсолютный берётся как есть, относительный — от корня.
fn resolve_path(repo_root: &Path, declared: &Path) -> PathBuf {
    if declared.is_absolute() {
        normalize_lexical(declared)
    } else {
        normalize_lexical(&repo_root.join(declared))
    }
}

/// Разрешить цель симлинка в абсолютный путь.
///
/// Относительная цель отсчитывается от каталога ссылки; каталог ссылки
/// существует, поэтому канонизируется — так проверка «цель внутри корня
/// репозитория» не ломается на путях с симлинками (`/tmp` → `/private/tmp`
/// и подобных).
fn resolve_link_target(link: &Path, target: &Path) -> PathBuf {
    if target.is_absolute() {
        return canonical_or_lexical(target);
    }
    let base = link
        .parent()
        .map_or_else(PathBuf::new, canonical_or_lexical);
    normalize_lexical(&base.join(target))
}

/// Канонический путь, если он существует; иначе — лексически нормализованный.
fn canonical_or_lexical(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| normalize_lexical(path))
}

/// Лексическая нормализация пути: `.` и `..` без обращения к диску.
fn normalize_lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in path.components() {
        match c {
            Component::CurDir => {}
            Component::ParentDir => {
                if !out.pop() && !out.has_root() {
                    out.push("..");
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

/// `sha256` файла через subprocess `sha256sum` (в стеке нет `sha2`).
fn sha256_of(path: &Path) -> anyhow::Result<String> {
    let out = Command::new("sha256sum")
        .arg(path)
        .output()
        .with_context(|| format!("запуск sha256sum для {}", path.display()))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!("sha256sum завершился с ошибкой: {}", err.trim());
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let hash = stdout.split_whitespace().next().unwrap_or_default();
    if hash.is_empty() {
        anyhow::bail!("sha256sum не вернул хеш");
    }
    Ok(hash.to_ascii_lowercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `sha256` файла с содержимым `abc` (независимая константа: эталон
    /// сверки не должен вычисляться тем же вызовом, что и в реализации).
    const SHA256_ABC: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

    /// Временный «репозиторий» с каталогом `models/`.
    fn repo() -> tempfile::TempDir {
        let dir = tempfile::tempdir().expect("временный каталог");
        std::fs::create_dir_all(dir.path().join("models")).expect("каталог models");
        dir
    }

    /// Манифест с одним артефактом.
    fn manifest(
        id: &str,
        kind: Option<ArtifactKind>,
        path: &str,
        sha256: Option<&str>,
    ) -> Manifest {
        Manifest {
            artifacts: vec![Artifact {
                id: id.to_string(),
                kind,
                path: PathBuf::from(path),
                sha256: sha256.map(str::to_string),
                license: None,
                source: None,
                bytes: None,
            }],
        }
    }

    /// Файл-эталон «канонического хранилища» вне рабочего дерева.
    fn outside_target() -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().expect("внешний каталог");
        let target = dir.path().join("weights.bin");
        std::fs::write(&target, b"abc").expect("файл цели");
        (dir, target)
    }

    /// Симлинк в `repo/models/<name>` на внешний файл. Возвращает гард
    /// внешнего каталога: он должен жить, пока идёт проверка.
    fn link_to_outside(repo: &tempfile::TempDir, name: &str) -> tempfile::TempDir {
        let (outside, target) = outside_target();
        let link = repo.path().join("models").join(name);
        std::os::unix::fs::symlink(&target, &link).expect("симлинк");
        outside
    }

    #[test]
    fn empty_yaml_yields_no_artifacts() {
        let m = parse_manifest_yaml("").expect("пустой YAML");
        assert!(m.artifacts.is_empty());
        let r = verify(&m, Path::new("/nonexistent"));
        assert!(r.checks.is_empty());
        assert_eq!(r.counts(), (0, 0, 0));
    }

    #[test]
    fn manifest_is_parsed_from_yaml() {
        let m = parse_manifest_yaml(
            "artifacts:\n  - id: w\n    kind: weights\n    path: models/w.bin\n    sha256: ab\n",
        )
        .expect("манифест");
        assert_eq!(m.artifacts.len(), 1);
        assert_eq!(m.artifacts[0].kind, Some(ArtifactKind::Weights));
    }

    #[test]
    fn missing_artifact_is_warn_not_fail() {
        let dir = repo();
        let m = manifest("w", Some(ArtifactKind::Weights), "models/absent.bin", None);
        let r = verify(&m, dir.path());
        assert_eq!(r.counts(), (0, 1, 0));
        assert_eq!(r.checks[0].status, CheckStatus::Warn);
        assert!(
            r.checks[0].detail.contains("объявлен, не найден"),
            "{}",
            r.checks[0].detail
        );
        assert!(!r.has_failures());
    }

    #[test]
    fn regular_file_copy_of_weights_is_fail() {
        let dir = repo();
        std::fs::write(dir.path().join("models/w.bin"), b"abc").expect("копия весов");
        let m = manifest(
            "w",
            Some(ArtifactKind::Weights),
            "models/w.bin",
            Some(SHA256_ABC),
        );
        let r = verify(&m, dir.path());
        assert_eq!(r.checks[0].status, CheckStatus::Fail);
        assert!(
            r.checks[0].detail.contains("копия в рабочем дереве"),
            "{}",
            r.checks[0].detail
        );
        assert!(r.has_failures());
    }

    #[test]
    fn regular_file_copy_of_dataset_is_fail() {
        let dir = repo();
        std::fs::write(dir.path().join("models/d.jsonl"), b"abc").expect("копия датасета");
        let m = manifest(
            "d",
            Some(ArtifactKind::Dataset),
            "models/d.jsonl",
            Some(SHA256_ABC),
        );
        let r = verify(&m, dir.path());
        assert_eq!(r.checks[0].status, CheckStatus::Fail);
    }

    #[test]
    fn symlink_outside_repo_root_is_ok() {
        let dir = repo();
        let _outside = link_to_outside(&dir, "w.bin");
        let m = manifest(
            "w",
            Some(ArtifactKind::Weights),
            "models/w.bin",
            Some(SHA256_ABC),
        );
        let r = verify(&m, dir.path());
        assert_eq!(
            r.checks[0].status,
            CheckStatus::Ok,
            "{}",
            r.checks[0].detail
        );
        assert!(
            !r.checks[0].detail.contains("C-032"),
            "{}",
            r.checks[0].detail
        );
    }

    #[test]
    fn symlink_target_inside_repo_root_warns_in_detail() {
        let dir = repo();
        let target = dir.path().join("models/copy.bin");
        std::fs::write(&target, b"abc").expect("цель внутри репозитория");
        let link = dir.path().join("models/w.bin");
        std::os::unix::fs::symlink(&target, &link).expect("симлинк");
        let m = manifest(
            "w",
            Some(ArtifactKind::Weights),
            "models/w.bin",
            Some(SHA256_ABC),
        );
        let r = verify(&m, dir.path());
        assert_eq!(
            r.checks[0].status,
            CheckStatus::Ok,
            "{}",
            r.checks[0].detail
        );
        assert!(
            r.checks[0].detail.contains("внутри рабочего дерева"),
            "{}",
            r.checks[0].detail
        );
        assert!(
            r.checks[0].detail.contains("C-032"),
            "{}",
            r.checks[0].detail
        );
    }

    #[test]
    fn relative_symlink_target_inside_repo_root_is_detected() {
        let dir = repo();
        let target = dir.path().join("models/copy.bin");
        std::fs::write(&target, b"abc").expect("цель внутри репозитория");
        let link = dir.path().join("models/w.bin");
        std::os::unix::fs::symlink("copy.bin", &link).expect("симлинк");
        let m = manifest(
            "w",
            Some(ArtifactKind::Weights),
            "models/w.bin",
            Some(SHA256_ABC),
        );
        let r = verify(&m, dir.path());
        assert_eq!(
            r.checks[0].status,
            CheckStatus::Ok,
            "{}",
            r.checks[0].detail
        );
        assert!(
            r.checks[0].detail.contains("C-032"),
            "{}",
            r.checks[0].detail
        );
    }

    #[test]
    fn sha256_mismatch_is_fail() {
        let dir = repo();
        let _outside = link_to_outside(&dir, "w.bin");
        let wrong = "0".repeat(64);
        let m = manifest(
            "w",
            Some(ArtifactKind::Weights),
            "models/w.bin",
            Some(&wrong),
        );
        let r = verify(&m, dir.path());
        assert_eq!(r.checks[0].status, CheckStatus::Fail);
        assert!(
            r.checks[0].detail.contains("sha256 не совпал"),
            "{}",
            r.checks[0].detail
        );
    }

    #[test]
    fn sha256_match_is_ok() {
        let dir = repo();
        let _outside = link_to_outside(&dir, "w.bin");
        let m = manifest(
            "w",
            Some(ArtifactKind::Weights),
            "models/w.bin",
            Some(SHA256_ABC),
        );
        let r = verify(&m, dir.path());
        assert_eq!(
            r.checks[0].status,
            CheckStatus::Ok,
            "{}",
            r.checks[0].detail
        );
        assert!(
            r.checks[0].detail.contains("sha256 совпал"),
            "{}",
            r.checks[0].detail
        );
    }

    #[test]
    fn missing_sha256_is_warn() {
        let dir = repo();
        let _outside = link_to_outside(&dir, "w.bin");
        let m = manifest("w", Some(ArtifactKind::Weights), "models/w.bin", None);
        let r = verify(&m, dir.path());
        assert_eq!(r.checks[0].status, CheckStatus::Warn);
        assert!(
            r.checks[0].detail.contains("хеш не объявлен"),
            "{}",
            r.checks[0].detail
        );
    }

    #[test]
    fn absolute_path_is_used_as_is() {
        let (_outside, target) = outside_target();
        // Роль `tokenizer`: регулярный файл вне репозитория — не копия весов.
        let m = manifest(
            "t",
            Some(ArtifactKind::Tokenizer),
            &target.to_string_lossy(),
            Some(SHA256_ABC),
        );
        let r = verify(&m, Path::new("/nonexistent/root"));
        assert_eq!(
            r.checks[0].status,
            CheckStatus::Ok,
            "{}",
            r.checks[0].detail
        );
    }

    #[test]
    fn counts_and_failures() {
        let r = VerifyReport {
            checks: vec![
                Check {
                    id: "a".into(),
                    status: CheckStatus::Ok,
                    detail: String::new(),
                },
                Check {
                    id: "b".into(),
                    status: CheckStatus::Warn,
                    detail: String::new(),
                },
                Check {
                    id: "c".into(),
                    status: CheckStatus::Fail,
                    detail: String::new(),
                },
            ],
        };
        assert_eq!(r.counts(), (1, 1, 1));
        assert!(r.has_failures());
    }

    #[test]
    fn render_report_lists_statuses() {
        let dir = repo();
        let m = manifest("w", Some(ArtifactKind::Weights), "models/absent.bin", None);
        let text = render_report(&verify(&m, dir.path()));
        assert!(text.contains("Артефакты: ok 0, warn 1, fail 0"), "{text}");
        assert!(text.contains('w'), "{text}");
    }
}
