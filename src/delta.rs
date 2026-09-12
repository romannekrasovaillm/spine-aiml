//! Дельта-спецификации как state machine (по OpenSpec): change-центричные
//! изменения `propose → (apply) → archive`; дельта описывает только изменение
//! относительно текущей истины (ADDED/MODIFIED/REMOVED).
//!
//! Каталог изменений — `changes/` в репозитории: предложенные на верхнем
//! уровне, заархивированные — в `changes/archive/`.
//!
//! [`guard`] — CI-гейт прямых правок спайна мимо дельты: изменённые файлы под
//! защищёнными путями (по умолчанию `model/`, `ARCHITECTURE-SPINE.md`,
//! `CONSTRAINTS.yaml`) обязаны упоминаться в активной дельте, иначе FAIL.

use std::path::{Path, PathBuf};

use crate::control::LintIssue;
use crate::error::{HarnessError, Result};

/// Шаблон дельты.
const DELTA_TEMPLATE: &str = "# Дельта: {name}
\
- Route: Fast|Standard (Critical — полный Solutioning, дельты недостаточно)
- Created: {date}

## Проблема

<что и зачем меняем — 2-3 предложения>

## ADDED

- <новые требования с критериями EARS: When <событие>, the <система> shall <реакция>>

## MODIFIED

- <изменяемые требования: было → стало, причина>

## REMOVED

- <удаляемое: что, замена, план миграции потребителей>

## План отката

<как откатываем изменение>

## Критерии приёмки

- [ ] <проверяемый критерий>
";

/// Создаёт каркас дельты `changes/<name>/DELTA.md`.
///
/// # Errors
/// Каталог существует, ошибка записи.
pub fn new(repo: &Path, name: &str) -> Result<PathBuf> {
    let dir = repo.join("changes").join(name);
    if dir.exists() || repo.join("changes/archive").join(name).exists() {
        return Err(HarnessError::Control(format!(
            "дельта '{name}' уже существует (активная или в архиве): {}",
            dir.display()
        )));
    }
    std::fs::create_dir_all(&dir).map_err(|e| HarnessError::io(&dir, e))?;
    let path = dir.join("DELTA.md");
    let content = DELTA_TEMPLATE.replace("{name}", name).replace(
        "{date}",
        &chrono::Local::now().format("%Y-%m-%d").to_string(),
    );
    std::fs::write(&path, content).map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

/// Статус дельты.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeltaStatus {
    /// Предложена (changes/<name>).
    Proposed,
    /// В архиве (changes/archive/<name>).
    Archived,
}

/// Сводка дельты.
#[derive(Debug, Clone)]
pub struct DeltaInfo {
    /// Имя.
    pub name: String,
    /// Статус.
    pub status: DeltaStatus,
    /// Путь к DELTA.md.
    pub path: PathBuf,
}

/// Список дельт (предложенные и архивные).
#[must_use]
pub fn list(repo: &Path) -> Vec<DeltaInfo> {
    let mut out = Vec::new();
    for (dir, status) in [
        (repo.join("changes"), DeltaStatus::Proposed),
        (repo.join("changes/archive"), DeltaStatus::Archived),
    ] {
        if let Ok(rd) = std::fs::read_dir(&dir) {
            for e in rd.flatten() {
                let p = e.path();
                if !p.is_dir() || p.file_name().is_some_and(|n| n == "archive") {
                    continue;
                }
                let delta = p.join("DELTA.md");
                if delta.is_file() {
                    out.push(DeltaInfo {
                        name: e.file_name().to_string_lossy().to_string(),
                        status,
                        path: delta,
                    });
                }
            }
        }
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out
}

/// Валидация структуры дельты: обязательные секции, заглушки, пустые блоки.
///
/// # Errors
/// DELTA.md не читается.
pub fn validate(repo: &Path, name: &str) -> Result<Vec<LintIssue>> {
    let path = find_delta(repo, name)?;
    let text = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
    let mut issues = Vec::new();
    for section in [
        "## Проблема",
        "## ADDED",
        "## MODIFIED",
        "## REMOVED",
        "## План отката",
        "## Критерии приёмки",
    ] {
        if !text.contains(section) {
            issues.push(LintIssue {
                file: path.clone(),
                line: 0,
                rule: "missing_section".into(),
                message: format!("нет секции «{section}»"),
                severity: "error".into(),
            });
        }
    }
    // Пустые секции-заглушки из шаблона.
    for (n, line) in text.lines().enumerate() {
        let t = line.trim();
        if t.starts_with('<') && t.ends_with('>') || t.contains("TODO") || t.contains("TBD") {
            issues.push(LintIssue {
                file: path.clone(),
                line: n + 1,
                rule: "stub_marker".into(),
                message: format!(
                    "незаполненное место: {}",
                    t.chars().take(60).collect::<String>()
                ),
                severity: "warn".into(),
            });
        }
    }
    // Все три блока пустые — дельта ни о чём.
    let has_content = ["## ADDED", "## MODIFIED", "## REMOVED"].iter().any(|s| {
        section_body(&text, s)
            .lines()
            .any(|l| l.trim().starts_with('-') && !l.contains('<'))
    });
    if !has_content {
        issues.push(LintIssue {
            file: path.clone(),
            line: 0,
            rule: "empty_delta".into(),
            message: "ADDED/MODIFIED/REMOVED пусты — дельта без содержания".into(),
            severity: "error".into(),
        });
    }
    Ok(issues)
}

/// Тело секции между заголовком `## X` и следующим `## `.
fn section_body<'a>(text: &'a str, section: &str) -> &'a str {
    let Some(start) = text.find(section) else {
        return "";
    };
    let rest = &text[start + section.len()..];
    let end = rest.find("\n## ").unwrap_or(rest.len());
    &rest[..end]
}

/// Архивирует дельту (после apply): валидация → перенос в `changes/archive/`.
///
/// # Errors
/// Дельта не найдена, не прошла валидацию (error-находки), ошибка переноса.
pub fn archive(repo: &Path, name: &str) -> Result<PathBuf> {
    let issues = validate(repo, name)?;
    let errors: Vec<&LintIssue> = issues.iter().filter(|i| i.severity == "error").collect();
    if !errors.is_empty() {
        return Err(HarnessError::Control(format!(
            "дельта '{name}' не прошла валидацию: {} error-находок",
            errors.len()
        )));
    }
    let src = repo.join("changes").join(name);
    let dst_dir = repo.join("changes/archive");
    std::fs::create_dir_all(&dst_dir).map_err(|e| HarnessError::io(&dst_dir, e))?;
    let dst = dst_dir.join(name);
    if dst.exists() {
        return Err(HarnessError::Control(format!(
            "в архиве уже есть '{name}' — номера/имена не переиспользуются"
        )));
    }
    std::fs::rename(&src, &dst).map_err(|e| HarnessError::io(&dst, e))?;
    Ok(dst.join("DELTA.md"))
}

/// Находит DELTA.md по имени (предложенная или архивная).
fn find_delta(repo: &Path, name: &str) -> Result<PathBuf> {
    for base in [
        repo.join("changes").join(name),
        repo.join("changes/archive").join(name),
    ] {
        let p = base.join("DELTA.md");
        if p.is_file() {
            return Ok(p);
        }
    }
    Err(HarnessError::Control(format!(
        "дельта '{name}' не найдена в {}/changes",
        repo.display()
    )))
}

/// Защищаемые пути по умолчанию гейта прямых правок спайна (`delta guard`):
/// модель архитектуры и корневые spine-артефакты. Любой явный `--protect`
/// заменяет этот список целиком.
pub const DEFAULT_PROTECTED: [&str; 3] = ["model/", "ARCHITECTURE-SPINE.md", "CONSTRAINTS.yaml"];

/// Отчёт гейта прямых правок спайна.
#[derive(Debug, Clone)]
pub struct GuardReport {
    /// База diff, как передана в git.
    pub base: String,
    /// Всего изменённых файлов по diff.
    pub changed: usize,
    /// Изменённые защищённые файлы.
    pub protected_changed: Vec<String>,
    /// Покрытые правки: (файл, имя активной дельты).
    pub covered: Vec<(String, String)>,
    /// Нарушения: защищённые файлы без упоминания в активных дельтах.
    pub violations: Vec<String>,
    /// Гейт пройден (нет непокрытых правок защищённых путей).
    pub passed: bool,
}

/// Защищён ли путь: совпадение с записью-файлом или вхождение в каталог-префикс
/// (`model/` матчит `model/adr/ADR-003.md`; запись без слэша трактуется и как
/// каталог: `model` тоже матчит).
fn is_protected(path: &str, protected: &[String]) -> bool {
    protected.iter().any(|entry| {
        let e = entry.trim_end_matches('/');
        path == e || path.starts_with(&format!("{e}/"))
    })
}

/// Упоминает ли текст дельты файл: полным относительным путём, именем файла
/// или стемом (от 4 символов — короткие стемы вроде `a` не матчим).
fn delta_mentions(body: &str, path: &str) -> bool {
    if body.contains(path) {
        return true;
    }
    let name = Path::new(path)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    if !name.is_empty() && body.contains(&name) {
        return true;
    }
    let stem = Path::new(path)
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    stem.chars().count() >= 4 && body.contains(&stem)
}

/// Гейт прямых правок спайна мимо дельты (CI-запрет «прямых коммитов в model/
/// мимо changes/»): каждый изменённый защищённый файл обязан упоминаться
/// (путём или именем) в теле хотя бы одной АКТИВНОЙ дельты
/// `changes/<name>/DELTA.md`; архивные дельты не засчитываются.
///
/// Изменённые файлы — `git diff --name-only <base>` (дефолт `HEAD`: staged +
/// unstaged рабочего дерева; untracked-файлы git-diff не показывает — для CI
/// передавайте базу вида `origin/main...HEAD`).
///
/// # Errors
/// `git` недоступен или вернул ненулевой код (не репозиторий, плохая база).
pub fn guard(repo: &Path, base: Option<&str>, protect: &[String]) -> Result<GuardReport> {
    let protected: Vec<String> = if protect.is_empty() {
        DEFAULT_PROTECTED.iter().map(|s| (*s).to_string()).collect()
    } else {
        protect.to_vec()
    };
    let base = base.unwrap_or("HEAD").to_string();
    let out = std::process::Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["diff", "--name-only", &base])
        .output()
        .map_err(|e| HarnessError::Control(format!("git не запустился: {e}")))?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(HarnessError::Control(format!(
            "git diff --name-only {base}: {}",
            stderr.trim().chars().take(300).collect::<String>()
        )));
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let changed: Vec<String> = stdout
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(str::to_owned)
        .collect();

    // Тела активных дельт (архив — уже влитая истина, покрытием не считается).
    let mut active: Vec<(String, String)> = Vec::new();
    for d in list(repo) {
        if d.status != DeltaStatus::Proposed {
            continue;
        }
        let body = std::fs::read_to_string(&d.path).map_err(|e| HarnessError::io(&d.path, e))?;
        active.push((d.name, body));
    }

    let mut protected_changed = Vec::new();
    let mut covered = Vec::new();
    let mut violations = Vec::new();
    for file in changed.iter().filter(|f| is_protected(f, &protected)) {
        protected_changed.push(file.clone());
        match active.iter().find(|(_, body)| delta_mentions(body, file)) {
            Some((name, _)) => covered.push((file.clone(), name.clone())),
            None => violations.push(file.clone()),
        }
    }
    let passed = violations.is_empty();
    Ok(GuardReport {
        base,
        changed: changed.len(),
        protected_changed,
        covered,
        violations,
        passed,
    })
}

/// Текстовый рендер отчёта гейта (в стиле остальных delta-команд).
#[must_use]
pub fn render_guard(report: &GuardReport) -> String {
    use std::fmt::Write as _;
    let mut out = String::new();
    let _ = writeln!(out, "Гейт прямых правок спайна (база: {})", report.base);
    let _ = writeln!(
        out,
        "Изменённых файлов: {}, защищённых среди них: {}",
        report.changed,
        report.protected_changed.len()
    );
    if !report.protected_changed.is_empty() {
        out.push('\n');
        for (file, delta) in &report.covered {
            let _ = writeln!(out, "[ok] {file} — покрыт активной дельтой '{delta}'");
        }
        for file in &report.violations {
            let _ = writeln!(
                out,
                "[error] {file} — не упоминается ни в одной активной дельте"
            );
            let _ = writeln!(
                out,
                "  → оформите правку дельтой: arch-ml delta new <name>, опишите изменение \
                 в changes/<name>/DELTA.md (архивные дельты не засчитываются)"
            );
        }
    }
    let _ = writeln!(
        out,
        "\nИтог: {}",
        if report.passed {
            if report.protected_changed.is_empty() {
                "PASS — защищённые пути не затронуты"
            } else {
                "PASS — все правки спайна покрыты активными дельтами"
            }
        } else {
            "FAIL — правки спайна мимо дельты (exit 1)"
        }
    );
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Заполняет дельту рабочим содержимым (все обязательные секции + буллеты).
    fn fill_delta(path: &Path) {
        std::fs::write(
            path,
            "# Дельта\n\n## Проблема\nТаймаут велик.\n\n## ADDED\n- Требование: таймаут авторизации 500 мс. Критерий: When запрос, the оркестратор shall ответить ≤ 500 мс\n\n## MODIFIED\n\n## REMOVED\n\n## План отката\nrevert флага\n\n## Критерии приёмки\n- [ ] тест таймаута\n",
        )
        .expect("fill");
    }

    #[test]
    fn delta_full_cycle_new_validate_archive_and_name_guard() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path();
        // new: шаблон создан, висит в Proposed.
        let path = new(repo, "saga-pilot").expect("new");
        assert!(path.is_file());
        let infos = list(repo);
        assert_eq!(infos.len(), 1);
        assert_eq!(infos[0].status, DeltaStatus::Proposed);
        // Дубликат активного имени — отказ.
        assert!(new(repo, "saga-pilot").is_err(), "активное имя занято");
        // Свежий шаблон не валиден (пустые блоки ADDED/MODIFIED/REMOVED).
        let issues = validate(repo, "saga-pilot").expect("validate");
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "empty_delta" && i.severity == "error"),
            "issues: {issues:?}"
        );
        // Архивация невалидной дельты — отказ.
        assert!(
            archive(repo, "saga-pilot").is_err(),
            "error-находки блокируют архив"
        );
        // Заполняем — валидация чиста (error-находок нет), архивируется.
        fill_delta(&path);
        let issues = validate(repo, "saga-pilot").expect("validate2");
        assert!(
            issues.iter().all(|i| i.severity != "error"),
            "остались error: {issues:?}"
        );
        let archived = archive(repo, "saga-pilot").expect("archive");
        assert!(archived.is_file());
        let infos = list(repo);
        assert_eq!(infos[0].status, DeltaStatus::Archived);
        // Имя в архиве занято навсегда: ни new, ни повторный archive.
        let err = new(repo, "saga-pilot").expect_err("имя в архиве защищено");
        assert!(err.to_string().contains("архиве"), "{err}");
        assert!(archive(repo, "saga-pilot").is_err());
        // Несуществующая дельта — внятная ошибка.
        assert!(validate(repo, "ghost").is_err());
    }

    /// git в каталоге с тестовой идентичностью коммиттера.
    fn git(dir: &Path, args: &[&str]) {
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(args)
            .env("GIT_AUTHOR_NAME", "t")
            .env("GIT_AUTHOR_EMAIL", "t@t")
            .env("GIT_COMMITTER_NAME", "t")
            .env("GIT_COMMITTER_EMAIL", "t@t")
            .output()
            .expect("git");
        assert!(
            out.status.success(),
            "git {}: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr)
        );
    }

    /// Репо-фикстура гейта: git init + защищённые файлы и код, один коммит.
    fn make_guard_repo(dir: &Path) {
        std::fs::create_dir_all(dir).expect("mkdir");
        git(dir, &["init", "-q"]);
        std::fs::create_dir_all(dir.join("model/adr")).expect("mkdir model");
        std::fs::write(dir.join("model/adr/ADR-003.md"), "# ADR-003\n").expect("adr");
        std::fs::write(dir.join("ARCHITECTURE-SPINE.md"), "# Spine\n").expect("spine");
        std::fs::create_dir_all(dir.join("src")).expect("mkdir src");
        std::fs::write(dir.join("src/main.rs"), "fn main() {}\n").expect("src");
        git(dir, &["add", "."]);
        git(dir, &["commit", "-q", "-m", "init"]);
    }

    #[test]
    fn guard_flags_protected_change_without_delta() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        make_guard_repo(&repo);
        // Прямая правка model/ без дельты (незакоммиченная — база HEAD ловит).
        std::fs::write(repo.join("model/adr/ADR-003.md"), "# ADR-003 v2\n").expect("edit");
        let report = guard(&repo, None, &[]).expect("guard");
        assert!(!report.passed);
        assert_eq!(report.violations, vec!["model/adr/ADR-003.md".to_string()]);
        assert!(report.covered.is_empty());
        let text = render_guard(&report);
        assert!(text.contains("arch-ml delta new"), "{text}");
        assert!(text.contains("FAIL"), "{text}");
    }

    #[test]
    fn guard_passes_when_active_delta_mentions_file() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        make_guard_repo(&repo);
        std::fs::write(repo.join("model/adr/ADR-003.md"), "# ADR-003 v2\n").expect("edit");
        // Активная дельта упоминает файл стемом (ADR-003) — засчитывается.
        let path = new(&repo, "update-adr-003").expect("new");
        let body = std::fs::read_to_string(&path).expect("read");
        std::fs::write(&path, format!("{body}\nЗатронут ADR-003 (таймауты).\n")).expect("mention");
        let report = guard(&repo, None, &[]).expect("guard");
        assert!(report.passed, "{report:?}");
        assert_eq!(
            report.covered,
            vec![(
                "model/adr/ADR-003.md".to_string(),
                "update-adr-003".to_string()
            )]
        );
        assert!(render_guard(&report).contains("PASS"));
    }

    #[test]
    fn guard_ignores_archived_delta() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        make_guard_repo(&repo);
        std::fs::write(repo.join("model/adr/ADR-003.md"), "# ADR-003 v2\n").expect("edit");
        let path = new(&repo, "update-adr-003").expect("new");
        fill_delta(&path);
        let body = std::fs::read_to_string(&path).expect("read");
        std::fs::write(&path, format!("{body}\nПравка model/adr/ADR-003.md.\n")).expect("mention");
        archive(&repo, "update-adr-003").expect("archive");
        // Архивная дельта — уже влитая истина, покрытием не считается.
        let report = guard(&repo, None, &[]).expect("guard");
        assert!(!report.passed, "архив не покрывает: {report:?}");
        assert_eq!(report.violations.len(), 1);
    }

    #[test]
    fn guard_passes_on_unprotected_change_and_protect_override() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        make_guard_repo(&repo);
        // Правка незащищённого файла — гейт молчит.
        std::fs::write(repo.join("src/main.rs"), "fn main() { println!(\"x\"); }\n").expect("edit");
        let report = guard(&repo, None, &[]).expect("guard");
        assert!(report.passed, "{report:?}");
        assert!(report.protected_changed.is_empty());
        assert!(render_guard(&report).contains("не затронуты"));
        // Явный --protect заменяет дефолт: теперь src/ под защитой → нарушение.
        let report = guard(&repo, None, &["src/".to_string()]).expect("guard override");
        assert!(!report.passed);
        assert_eq!(report.violations, vec!["src/main.rs".to_string()]);
    }

    #[test]
    fn guard_reports_git_errors() {
        let tmp = tempfile::tempdir().expect("tmp");
        // Не репозиторий — внятная ошибка, не паника.
        let err = guard(tmp.path(), None, &[]).expect_err("не git");
        assert!(err.to_string().contains("git diff"), "{err}");
    }
}
