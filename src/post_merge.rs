//! Приёмка дельты: сверки ДО мержа и гейт ПОСЛЕ мержа (ADR-024 кейса
//! `laguna-compact`, шаги 2/3/5; платформенные решения и коды возврата —
//! ADR-048). Ниже «ADR-024» без уточнения — это ADR-024 кейса.
//!
//! Мерж — единственная операция приёмки, меняющая основную ветку, и её
//! результат обязан проверяться инструментом, а не памятью архитектора
//! (кейс `laguna-compact`: два конфликта при мерже разрешены вручную и гейт
//! после них не прогонялся; приёмка отклонялась из-за перезаписи
//! исторического evidence). Отсюда три проверки, добавляемые к `worktree
//! accept`:
//!
//! 1. **Сверка редакции правил (шаг 2).** `sha256` рабочего
//!    `CONSTRAINTS.yaml` ветки против основной ветки. Расхождение значит,
//!    что гейт в ветке проверял СВОЮ редакцию правил — предупреждение с
//!    обоими хешами и советом сначала влить основную ветку в ветку.
//!    Не блокирует: приёмка остаётся решением человека ([`PreMergeWarnings`]).
//! 2. **Исторические evidence (шаг 3).** Изменённые (не добавленные) файлы
//!    `evidence/**` — правка доказательств завершённых стадий задним числом;
//!    список выводится, решение — за владельцем.
//! 3. **Post-merge гейт (шаг 5).** Прогон fitness-правил основной рабочей
//!    копии после мержа: сколько правил, сколько нарушений, поимённо
//!    красные ([`post_merge_gate`]).
//!
//! Модуль ЧИТАЕТ и докладывает: он ничего не блокирует и не откатывает —
//! откат мержа решение владельца (`git reset --hard <pre-merge>`), потому
//! что красное состояние могло быть и до мержа (ADR-024, «Negative»).
//!
//! Сверки не выдумывают связь и не читают формулировки: хеши считаются по
//! байтам blob'а из git (`cat-file blob`), статусы изменений берутся из
//! `git diff --name-status`, вердикт — из `FitnessReport` (тот же прогон,
//! что у `arch-ml control check`).

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::Path;

use crate::control::{FitnessReport, LintIssue};
use crate::error::{HarnessError, Result};

/// Рабочий файл правил репозитория (относительно корня) — та же константа
/// приоритета, что у [`crate::control::resolve_ruleset`].
const RULES_FILE: &str = "CONSTRAINTS.yaml";

/// Каталоги доказательств: `evidence/**` на любом уровне (кейс держит
/// `evidence/` в корне, платформа — `banking/compliance/evidence/`).
const EVIDENCE_GLOB: &str = ":(glob)**/evidence/**";

/// Сколько строк находок печатает компактный вердикт гейта (остальное —
/// счётчиком): вывод приёмки читает человек, а не парсер.
const MAX_ISSUE_LINES: usize = 10;

/// Предупреждения, собранные ДО мержа (ADR-024, шаги 2–3). Приёмку не
/// блокируют — см. модульную документацию.
#[derive(Debug, Default, Clone)]
pub struct PreMergeWarnings {
    /// Готовые строки предупреждений (пусто — расхождений не найдено).
    pub lines: Vec<String>,
}

impl PreMergeWarnings {
    /// Есть ли предупреждения.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.lines.is_empty()
    }

    /// Блок вывода для отчёта приёмки; пустая строка — предупреждений нет.
    #[must_use]
    pub fn render(&self) -> String {
        if self.lines.is_empty() {
            return String::new();
        }
        let mut out = String::new();
        let _ = writeln!(out, "── сверки до мержа (ADR-024, шаги 2–3) ──");
        for line in &self.lines {
            let _ = writeln!(out, "{line}");
        }
        out
    }
}

/// Вердикт post-merge гейта (ADR-024, шаг 5).
#[derive(Debug, Clone)]
pub struct GateVerdict {
    /// Текст вердикта: ruleset, число правил и нарушений, поимённо красные.
    pub text: String,
    /// Гейт нашёл нарушения — либо ruleset есть, но не отработал (правила
    /// невалидны): непроверенное состояние основной ветки не есть успех.
    pub failed: bool,
    /// Гейт не выполнялся: файла правил в дереве нет — проверять нечего.
    pub not_run: bool,
}

impl GateVerdict {
    /// Статус приёмки для вывода и кода возврата.
    #[must_use]
    pub fn status_label(&self) -> &'static str {
        if self.failed {
            "CONSTRAINTS-FAILED"
        } else {
            "OK"
        }
    }
}

/// Сверки до мержа: редакция правил (шаг 2) и исторические evidence (шаг 3).
///
/// Сверка, которая не смогла отработать (сбой git, нет общего предка у
/// ветки), НЕ отменяет приёмку: она становится явной строкой «не проверено» —
/// предупреждения по решению владельца не блокируют (ADR-024). Молчаливого
/// пропуска нет: неотработавшая сверка видна в выводе.
pub async fn pre_merge(repo: &Path, branch: &str) -> PreMergeWarnings {
    let mut lines = Vec::new();
    match rules_edition_warning(repo, branch).await {
        Ok(Some(warning)) => lines.push(warning),
        Ok(None) => {}
        Err(e) => lines.push(format!(
            "⚑ сверка редакции правил не выполнена ({e}) — проверить расхождение редакций не удалось"
        )),
    }
    match evidence_warning(repo, branch).await {
        Ok(Some(warning)) => lines.push(warning),
        Ok(None) => {}
        Err(e) => lines.push(format!(
            "⚑ сверка исторических evidence не выполнена ({e}) — проверить правку доказательств не удалось"
        )),
    }
    PreMergeWarnings { lines }
}

/// `sha256` файла правил в ревизии (`None` — файла в ревизии нет).
///
/// Хеш считается по байтам blob'а (`cat-file blob`), а не по тексту рабочего
/// дерева: сравнивать надо редакции ПРАВИЛ, а не то, что лежит на диске.
async fn rules_sha(repo: &Path, rev: &str) -> Result<Option<String>> {
    let spec = format!("{rev}:{RULES_FILE}");
    // Путь в ревизии отсутствует (git: `fatal: path ... does not exist`)
    // либо ревизия не читается — обе причины одинаково значат «редакции
    // правил из этой ревизии взять не удалось».
    let Ok(blob) = crate::worktree::git_bytes(repo, &["cat-file", "blob", &spec]).await else {
        return Ok(None);
    };
    Ok(Some(crate::managed::Sha256::of_bytes(&blob).as_marker()))
}

/// Предупреждение о расхождении редакций правил (ADR-024, шаг 2, пробел П3).
async fn rules_edition_warning(repo: &Path, branch: &str) -> Result<Option<String>> {
    let branch_sha = rules_sha(repo, branch).await?;
    let head_sha = rules_sha(repo, "HEAD").await?;
    let warning = match (&branch_sha, &head_sha) {
        (Some(b), Some(h)) if b != h => format!(
            "⚑ редакция правил расходится с основной веткой: {RULES_FILE} в {branch} — {b}, \
             в основной ветке (HEAD) — {h}. Гейт в ветке проверял СВОЮ редакцию; сначала влейте \
             основную ветку в ветку (`git merge main`) и прогоните гейт заново — иначе проверка \
             идёт по устаревшим правилам"
        ),
        (Some(b), None) => format!(
            "⚑ в основной ветке (HEAD) нет {RULES_FILE}, а ветка {branch} его приносит ({b}): \
             правила появляются в основной ветке вместе с этим мержем — сверять редакцию не с чем"
        ),
        (None, Some(h)) => format!(
            "⚑ ветка {branch} не содержит {RULES_FILE}, в основной ветке (HEAD) он есть ({h}): \
             мерж не меняет редакцию правил (рабочий ruleset остаётся от основной ветки)"
        ),
        _ => return Ok(None),
    };
    Ok(Some(warning))
}

/// Предупреждение об изменённых исторических evidence (ADR-024, шаг 3,
/// пробел П5). Добавленные файлы доказательств — нормальная работа стадии;
/// предупреждаются только ИЗМЕНЕНИЕ, УДАЛЕНИЕ и ПЕРЕИМЕНОВАНИЕ уже
/// зафиксированных доказательств.
async fn evidence_warning(repo: &Path, branch: &str) -> Result<Option<String>> {
    let changed = changed_evidence(repo, branch).await?;
    if changed.is_empty() {
        return Ok(None);
    }
    let mut warning = format!(
        "⚑ правка исторических evidence: в ветке {branch} изменены (не добавлены) файлы \
         доказательств — перезапись задним числом требует решения владельца (ADR-024, шаг 3):"
    );
    for line in &changed {
        let _ = write!(warning, "\n  {line}");
    }
    Ok(Some(warning))
}

/// Изменённые (не добавленные) файлы доказательств в ветке: строки вида
/// `<что> <путь>` для `severity`-независимого вывода.
///
/// # Errors
/// Сбой git (репозиторий недоступен, ревизия не читается).
async fn changed_evidence(repo: &Path, branch: &str) -> Result<Vec<String>> {
    let range = format!("HEAD...{branch}");
    let diff = crate::worktree::git(
        repo,
        &["diff", "--name-status", &range, "--", EVIDENCE_GLOB],
    )
    .await?;
    let mut out = Vec::new();
    for line in diff.lines() {
        let mut fields = line.split('\t');
        let Some(status) = fields.next() else {
            continue;
        };
        let label = match status.chars().next() {
            Some('M') => "изменён",
            Some('D') => "удалён",
            Some('R') => "переименован",
            Some('T') => "изменён тип",
            // A (добавлен) и C (скопирован) — не правка истории: новые
            // доказательства стадия добавляет свободно.
            _ => continue,
        };
        let paths: Vec<&str> = fields.filter(|p| !p.trim().is_empty()).collect();
        if paths.is_empty() {
            continue;
        }
        out.push(format!("{label} {}", paths.join(" → ")));
    }
    Ok(out)
}

/// Post-merge гейт: прогон fitness-правил основной рабочей копии после мержа
/// (ADR-024, шаг 5, пробелы П1/П4). Правила берутся из самой копии
/// ([`crate::control::resolve_ruleset`] — рабочий `CONSTRAINTS.yaml`, при
/// отсутствии пакетная заготовка с предупреждением), прогон — тот же
/// [`crate::control::check`], что у `arch-ml control check`.
///
/// Файла правил нет вовсе — гейт не выполнялся ([`GateVerdict::not_run`],
/// явная пометка «проверять нечего»): проверять нечего, но и молчать об этом
/// нельзя. Правила есть, а прогон не удался (невалидный YAML, пустой
/// ruleset) — fail-closed: непроверенная основная ветка не есть успех.
///
/// # Errors
/// Blocking-задача прогона правил прервана рантаймом (join-ошибка). Ошибка
/// самого прогона — не `Err`, а вердикт `failed` (fail-closed).
pub async fn post_merge_gate(repo: &Path) -> Result<GateVerdict> {
    let Some(ruleset) = crate::control::resolve_ruleset(repo) else {
        return Ok(GateVerdict {
            text: format!(
                "⚑ гейт НЕ выполнен: файла правил нет ({}/{} и пакетной заготовки) — проверять \
                 нечего (отсутствие проверки ≠ успех)",
                repo.display(),
                RULES_FILE
            ),
            failed: false,
            not_run: true,
        });
    };
    let mut text = String::new();
    let _ = writeln!(
        text,
        "ruleset: {} ({})",
        ruleset.path.display(),
        ruleset.kind.label()
    );
    if let Some(warning) = ruleset.warning(repo) {
        let _ = writeln!(text, "{warning}");
    }
    // Прогон CPU-bound (у репозитория платформы в правилах есть `cargo test`)
    // — уводим с потока рантайма, чтобы CLI/TUI не замирали на «тишине».
    let repo_owned = repo.to_path_buf();
    let rules_path = ruleset.path.clone();
    let report =
        tokio::task::spawn_blocking(move || crate::control::check(&repo_owned, &rules_path))
            .await
            .map_err(|e| {
                HarnessError::Tool(format!(
                    "post-merge гейт: прогон правил не завершился ({e}) — состояние основной ветки \
             не проверено"
                ))
            })?;
    match report {
        Ok(report) => {
            render_report(&report, &mut text);
            Ok(GateVerdict {
                failed: !report.passed,
                text,
                not_run: false,
            })
        }
        Err(e) => {
            let _ = writeln!(
                text,
                "⚑ гейт не отработал: {e} — состояние основной ветки не проверено"
            );
            Ok(GateVerdict {
                failed: true,
                text,
                not_run: false,
            })
        }
    }
}

/// Компактный вердикт по отчёту: сводка, поимённо красные правила, первые
/// находки с файлом и строкой.
fn render_report(report: &FitnessReport, out: &mut String) {
    let _ = writeln!(out, "{}", report.summary);
    let errors: Vec<&LintIssue> = report
        .issues
        .iter()
        .filter(|i| i.severity == "error")
        .collect();
    if errors.is_empty() {
        return;
    }
    let mut by_rule: BTreeMap<&str, usize> = BTreeMap::new();
    for issue in &errors {
        *by_rule.entry(issue.rule.as_str()).or_default() += 1;
    }
    let names = by_rule
        .iter()
        .map(|(rule, n)| {
            if *n > 1 {
                format!("{rule} ({n})")
            } else {
                (*rule).to_string()
            }
        })
        .collect::<Vec<_>>()
        .join(", ");
    let _ = writeln!(out, "красные правила: {names}");
    for issue in errors.iter().take(MAX_ISSUE_LINES) {
        let _ = writeln!(
            out,
            "  [error] {}:{} {} — {}",
            issue.file.display(),
            issue.line,
            issue.rule,
            issue.message
        );
    }
    if errors.len() > MAX_ISSUE_LINES {
        let _ = writeln!(out, "  … и ещё {} находок", errors.len() - MAX_ISSUE_LINES);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// git в каталоге с тестовой идентичностью коммиттера.
    fn git_in(dir: &Path, args: &[&str]) {
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

    /// Репозиторий-фикстура: git init + базовый коммит, ветка `arch/x` с
    /// изменениями `files` (путь → содержимое; `None` — удалить файл).
    fn repo_with_branch(
        files: &[(&str, Option<&str>)],
        constraints: Option<&str>,
    ) -> (tempfile::TempDir, PathBuf) {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        std::fs::create_dir(&repo).expect("mkdir");
        git_in(&repo, &["init", "-q", "-b", "main"]);
        std::fs::write(repo.join("README.md"), "база\n").expect("write");
        // Доказательства: в корне и во вложенном каталоге (`evidence/` бывает
        // на любом уровне — кейс держит его в корне, платформа — в
        // `banking/compliance/`).
        std::fs::create_dir_all(repo.join("evidence")).expect("mkdir evidence");
        std::fs::write(repo.join("evidence/hist.json"), "{\"stage\":\"S1\"}\n").expect("write");
        std::fs::create_dir_all(repo.join("sub/evidence")).expect("mkdir sub/evidence");
        std::fs::write(repo.join("sub/evidence/deep.json"), "{\"stage\":\"S3\"}\n").expect("write");
        if let Some(c) = constraints {
            std::fs::write(repo.join(RULES_FILE), c).expect("write");
        }
        git_in(&repo, &["add", "-A"]);
        git_in(&repo, &["commit", "-qm", "base"]);
        git_in(&repo, &["checkout", "-q", "-b", "arch/x"]);
        for (path, body) in files {
            let full = repo.join(path);
            match body {
                Some(body) => {
                    if let Some(parent) = full.parent() {
                        std::fs::create_dir_all(parent).expect("mkdir");
                    }
                    std::fs::write(&full, body).expect("write");
                }
                None => {
                    std::fs::remove_file(&full).expect("remove");
                }
            }
        }
        git_in(&repo, &["add", "-A"]);
        git_in(&repo, &["commit", "-qm", "delta"]);
        git_in(&repo, &["checkout", "-q", "main"]);
        (tmp, repo)
    }

    #[tokio::test]
    async fn same_rules_edition_gives_no_warning() {
        let rules =
            "rules:\n  - id: C-1\n    name: readme\n    type: file_exists\n    path: README.md\n";
        let (_tmp, repo) = repo_with_branch(&[("feature.md", Some("фича\n"))], Some(rules));
        let warnings = pre_merge(&repo, "arch/x").await;
        assert!(warnings.is_empty(), "{}", warnings.render());
    }

    #[tokio::test]
    async fn divergent_rules_edition_warns_with_both_hashes() {
        let base_rules =
            "rules:\n  - id: C-1\n    name: readme\n    type: file_exists\n    path: README.md\n";
        let delta_rules =
            "rules:\n  - id: C-1\n    name: readme\n    type: file_exists\n    path: lib.rs\n";
        let (_tmp, repo) = repo_with_branch(&[(RULES_FILE, Some(delta_rules))], Some(base_rules));
        // Хеши: в ветке — редакция дельты, в HEAD — базовая.
        let branch_sha = rules_sha(&repo, "arch/x").await.expect("git").expect("sha");
        let head_sha = rules_sha(&repo, "HEAD").await.expect("git").expect("sha");
        assert_ne!(branch_sha, head_sha);
        let warnings = pre_merge(&repo, "arch/x").await;
        let text = warnings.render();
        assert!(text.contains("редакция правил расходится"), "{text}");
        assert!(
            text.contains(&branch_sha),
            "хеш ветки в предупреждении: {text}"
        );
        assert!(
            text.contains(&head_sha),
            "хеш main в предупреждении: {text}"
        );
        assert!(
            text.contains("git merge main"),
            "совет синхронизации: {text}"
        );
    }

    #[tokio::test]
    async fn modified_evidence_warns_but_added_is_silent() {
        let (_tmp, repo) = repo_with_branch(
            &[
                ("evidence/hist.json", Some("{\"stage\":\"S1\",\"rev\":2}\n")),
                ("evidence/new.json", Some("{\"stage\":\"S2\"}\n")),
                (
                    "sub/evidence/deep.json",
                    Some("{\"stage\":\"S3\",\"rev\":2}\n"),
                ),
            ],
            None,
        );
        let changed = changed_evidence(&repo, "arch/x").await.expect("diff");
        assert_eq!(
            changed,
            vec![
                "изменён evidence/hist.json".to_string(),
                "изменён sub/evidence/deep.json".to_string(),
            ],
            "{changed:?}"
        );
        assert!(
            !changed.iter().any(|l| l.contains("new.json")),
            "добавленное доказательство не предупреждается: {changed:?}"
        );
        let warnings = pre_merge(&repo, "arch/x").await;
        let text = warnings.render();
        assert!(text.contains("правка исторических evidence"), "{text}");
        assert!(text.contains("решения владельца"), "{text}");
    }

    #[tokio::test]
    async fn deleted_evidence_is_reported() {
        let (_tmp, repo) = repo_with_branch(&[("evidence/hist.json", None)], None);
        let changed = changed_evidence(&repo, "arch/x").await.expect("diff");
        assert_eq!(changed, vec!["удалён evidence/hist.json".to_string()]);
    }

    /// Сверка, которая не смогла отработать (у ветки нет общего предка с
    /// основной), не отменяет приёмку: строка «не проверено» вместо ошибки.
    #[tokio::test]
    async fn unworkable_check_is_reported_not_fatal() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        std::fs::create_dir(&repo).expect("mkdir");
        git_in(&repo, &["init", "-q", "-b", "main"]);
        std::fs::write(repo.join("README.md"), "база\n").expect("write");
        git_in(&repo, &["add", "-A"]);
        git_in(&repo, &["commit", "-qm", "base"]);
        // Сирота-ветка: общей истории с main нет — `HEAD...branch` не считается.
        git_in(&repo, &["checkout", "-q", "--orphan", "arch/orphan"]);
        std::fs::write(repo.join("feature.md"), "фича\n").expect("write");
        git_in(&repo, &["add", "-A"]);
        git_in(&repo, &["commit", "-qm", "orphan"]);
        git_in(&repo, &["checkout", "-q", "main"]);

        let warnings = pre_merge(&repo, "arch/orphan").await;
        let text = warnings.render();
        assert!(
            text.contains("сверка исторических evidence не выполнена"),
            "{text}"
        );
        assert!(
            text.contains("не блокирует") || text.contains("не выполнена"),
            "{text}"
        );
    }

    #[tokio::test]
    async fn gate_without_ruleset_is_not_run() {
        let (_tmp, repo) = repo_with_branch(&[("feature.md", Some("фича\n"))], None);
        let verdict = post_merge_gate(&repo).await.expect("gate");
        assert!(verdict.not_run, "{}", verdict.text);
        assert!(!verdict.failed);
        assert_eq!(verdict.status_label(), "OK");
        assert!(
            verdict.text.contains("проверять нечего"),
            "{}",
            verdict.text
        );
    }

    #[tokio::test]
    async fn gate_reports_green_and_red_rules() {
        let rules = "rules:\n  - id: C-1\n    name: readme_needed\n    type: file_exists\n    path: README.md\n";
        let (_tmp, repo) = repo_with_branch(&[("feature.md", Some("фича\n"))], Some(rules));
        let verdict = post_merge_gate(&repo).await.expect("gate");
        assert!(!verdict.failed, "{}", verdict.text);
        assert_eq!(verdict.status_label(), "OK");
        assert!(verdict.text.contains("Правил: 1"), "{}", verdict.text);

        // Красный: правило нарушено в основной ветке (мерж не откатываем —
        // гейт только докладывает).
        std::fs::remove_file(repo.join("README.md")).expect("rm");
        let verdict = post_merge_gate(&repo).await.expect("gate");
        assert!(verdict.failed, "{}", verdict.text);
        assert_eq!(verdict.status_label(), "CONSTRAINTS-FAILED");
        assert!(verdict.text.contains("readme_needed"), "{}", verdict.text);
    }

    #[tokio::test]
    async fn broken_ruleset_fails_closed() {
        let rules = "rules: []\n";
        let (_tmp, repo) = repo_with_branch(&[("feature.md", Some("фича\n"))], Some(rules));
        let verdict = post_merge_gate(&repo).await.expect("gate");
        assert!(verdict.failed, "пустой ruleset — не «зелёный по пустоте»");
        assert!(verdict.text.contains("не отработал"), "{}", verdict.text);
    }
}
