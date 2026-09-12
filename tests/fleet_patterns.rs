//! Интеграционные тесты движка оркестрации флотов по паттернам (ADR-042).
//!
//! Всё детерминировано и офлайн: дом изолирован в tempdir
//! ([`common::arch_cmd`]), кодовый харнесс — shell-скрипт, печатающий
//! JSON-контракт результата. Живые LLM и реальные харнессы не вызываются:
//! движок проверяется механически (паттерны, гейты, resume, owner-гейт).

mod common;

use std::path::{Path, PathBuf};

use predicates::str::contains;

use common::arch_cmd;

/// Путь к фикстуре плана.
fn fixture(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/fleet")
        .join(name)
}

/// Пишет исполняемый shell-скрипт фейкового харнесса.
///
/// Скрипт читает задание со stdin (как настоящий харнесс) и печатает
/// JSON-контракт `status`. Вариант `not_ready` дополнительно отвечает
/// вердиктом ревьювера на состязательную формулировку (роль reviewer).
fn write_harness(dir: &Path, not_ready: bool) -> PathBuf {
    let script = dir.join(if not_ready {
        "fake-smart.sh"
    } else {
        "fake-harness.sh"
    });
    let body = if not_ready {
        "#!/bin/sh\nprompt=$(cat)\n\
         if printf '%s' \"$prompt\" | grep -q 'Ты НЕ проектировал'; then\n\
         echo '{\"verdict\":\"NOT-READY\",\"findings\":[{\"severity\":\"high\"}]}'\n\
         exit 0\nfi\n\
         echo '```json'\n\
         echo '{\"status\":\"complete\",\"assumptions\":[\"готово\"],\"open_questions\":[],\"conflicts_with_prior_decisions\":[]}'\n\
         echo '```'\n"
    } else {
        "#!/bin/sh\ncat > /dev/null\necho 'работа сделана'\n\
         echo '```json'\n\
         echo '{\"status\":\"complete\",\"assumptions\":[\"готово\"],\"open_questions\":[],\"conflicts_with_prior_decisions\":[]}'\n\
         echo '```'\n"
    };
    std::fs::write(&script, body).expect("запись fake-харнесса");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        let mut perms = std::fs::metadata(&script)
            .expect("stat скрипта")
            .permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&script, perms).expect("chmod +x");
    }
    script
}

/// Пишет харнесс, который создаёт артефакт и коммитит его (как настоящий
/// исполнитель): нужен, чтобы проверить, что работа зависимости реально
/// вливается в дерево зависимого узла.
fn write_marker_harness(dir: &Path) -> PathBuf {
    let script = dir.join("fake-marker.sh");
    let body = "#!/bin/sh\ncat > /dev/null\n\
         echo 'работа сделана' > dep_marker.txt\n\
         git add dep_marker.txt >/dev/null 2>&1\n\
         git -c user.email=t@t -c user.name=t commit -qm 'feat: маркер зависимости' >/dev/null 2>&1\n\
         echo '```json'\n\
         echo '{\"status\":\"complete\",\"assumptions\":[\"готово\"],\"open_questions\":[],\"conflicts_with_prior_decisions\":[]}'\n\
         echo '```'\n";
    std::fs::write(&script, body).expect("запись marker-харнесса");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        let mut perms = std::fs::metadata(&script)
            .expect("stat скрипта")
            .permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&script, perms).expect("chmod +x");
    }
    script
}

/// Готовит дом теста: git-репозиторий, config.toml с фейковым харнессом
/// и путями внутри tempdir. Возвращает (home, repo, config).
fn home_with_fleet(not_ready: bool) -> (tempfile::TempDir, PathBuf, PathBuf) {
    let home = tempfile::tempdir().expect("tempdir");
    let repo = home.path().join("repo");
    std::fs::create_dir_all(&repo).expect("mkdir repo");
    git(&repo, &["init", "-q", "-b", "main"]);
    git(&repo, &["config", "user.email", "test@example.com"]);
    git(&repo, &["config", "user.name", "test"]);
    std::fs::write(repo.join("README.md"), "база\n").expect("write");
    git(&repo, &["add", "-A"]);
    git(&repo, &["commit", "-qm", "base"]);

    let script = write_harness(home.path(), not_ready);
    let config = home.path().join("config.toml");
    let text = format!(
        "[paths]\nstate_dir = '{}'\nreports_dir = '{}'\nsessions_dir = '{}'\nassets_dir = '{}'\n\n\
         [harnesses.fake]\nbinary = '{}'\nprompt_mode = 'stdin'\ntimeout_secs = 30\n\
         idle_timeout_secs = 0\nauto_commit = true\n",
        home.path().join("state").display(),
        home.path().join("reports").display(),
        home.path().join("sessions").display(),
        home.path().join("assets").display(),
        script.display(),
    );
    std::fs::write(&config, text).expect("config.toml");
    (home, repo, config)
}

/// Запускает git в каталоге (тестовая обвязка).
fn git(repo: &Path, args: &[&str]) {
    let out = std::process::Command::new("git")
        .args(args)
        .current_dir(repo)
        .output()
        .expect("git запускается");
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
}

/// Команда `arch-ml fleet …` в изолированном доме.
fn fleet_cmd(home: &Path, config: &Path) -> assert_cmd::Command {
    let mut cmd = arch_cmd(home);
    cmd.arg("--config").arg(config.as_os_str()).arg("fleet");
    cmd
}

/// Идентификатор прогона из stdout команды `fleet run --plan`.
fn run_id_of(stdout: &str) -> String {
    stdout
        .lines()
        .find_map(|l| l.split("run-id ").nth(1))
        .map(|s| s.trim().to_string())
        .expect("в выводе есть run-id")
}

/// Пишет план в дом теста и возвращает путь.
fn write_plan(home: &Path, name: &str, body: &str) -> PathBuf {
    let path = home.join(name);
    std::fs::write(&path, body).expect("план записан");
    path
}

#[test]
fn all_pattern_fixtures_validate() {
    for name in [
        "fanout.plan.toml",
        "pipeline.plan.toml",
        "map_reduce.plan.toml",
        "tournament.plan.toml",
        "review_pair.plan.toml",
        "ralph.plan.toml",
        "walking_skeleton.plan.toml",
        "dag.plan.toml",
    ] {
        let home = tempfile::tempdir().expect("tempdir");
        let mut cmd = arch_cmd(home.path());
        cmd.arg("fleet")
            .arg("plan")
            .arg("validate")
            .arg(fixture(name))
            .assert()
            .success();
    }
}

#[test]
fn invalid_plan_fails_validation_with_exit_1() {
    let home = tempfile::tempdir().expect("tempdir");
    let plan = write_plan(
        home.path(),
        "cycle.plan.toml",
        "id = \"cycle\"\npattern = \"dag\"\n\n[[nodes]]\nid = \"a\"\nspec = \"s\"\ndepends_on = [\"b\"]\n\n[[nodes]]\nid = \"b\"\nspec = \"s\"\ndepends_on = [\"a\"]\n",
    );
    arch_cmd(home.path())
        .arg("fleet")
        .arg("plan")
        .arg("validate")
        .arg(&plan)
        .assert()
        .failure()
        .code(1)
        .stdout(contains("цикл"));
}

#[test]
fn show_reports_waves_and_mermaid() {
    let home = tempfile::tempdir().expect("tempdir");
    arch_cmd(home.path())
        .arg("fleet")
        .arg("plan")
        .arg("show")
        .arg(fixture("pipeline.plan.toml"))
        .assert()
        .success()
        .stdout(contains("Волна 1"))
        .stdout(contains("Волна 3"))
        .stdout(contains("verify"));

    arch_cmd(home.path())
        .arg("fleet")
        .arg("plan")
        .arg("show")
        .arg(fixture("map_reduce.plan.toml"))
        .arg("--mermaid")
        .assert()
        .success()
        .stdout(contains("reduce"));
}

#[test]
fn fanout_plan_runs_offline_and_completes() {
    let (home, repo, config) = home_with_fleet(false);
    let plan = write_plan(
        home.path(),
        "run-fanout.plan.toml",
        "id = \"run-fanout\"\npattern = \"fanout\"\n\n[policy]\nmax_parallel = 2\n\
         require_worktree = true\n\n[defaults]\nroute = \"fast\"\ngates = [\"contract\"]\n\n\
         [[nodes]]\nid = \"n1\"\nspec = \"модуль 1\"\n\n[[nodes]]\nid = \"n2\"\nspec = \"модуль 2\"\n",
    );
    let assert = fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--plan")
        .arg(&plan)
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    assert!(stdout.contains("завершено 2"), "{stdout}");
    assert!(stdout.contains("Worktree узлов: 2"), "{stdout}");

    // Журнал: каждый узел дошёл до NodeCompleted и ни одна строка не битая.
    let run_id = run_id_of(&stdout);
    let journal = home
        .path()
        .join("state/fleet")
        .join(format!("{run_id}.jsonl"));
    let text = std::fs::read_to_string(&journal).expect("журнал");
    let completed = text
        .lines()
        .filter(|l| l.contains("\"type\":\"node_completed\""))
        .count();
    assert_eq!(completed, 2, "{text}");
    for line in text.lines() {
        serde_json::from_str::<serde_json::Value>(line).expect("строка журнала — валидный JSON");
    }

    // Снимок плана рядом с журналом — resume самодостаточен.
    assert!(
        home.path()
            .join("state/fleet")
            .join(format!("{run_id}.plan.toml"))
            .is_file()
    );
}

#[test]
fn pipeline_gate_failure_halts_and_skips_dependents() {
    let (home, repo, config) = home_with_fleet(false);
    let plan = write_plan(
        home.path(),
        "run-pipeline-fail.plan.toml",
        "id = \"pipeline-fail\"\npattern = \"pipeline\"\norder = [\"a\", \"b\"]\n\n\
         [policy]\nmax_parallel = 2\nrequire_worktree = true\nstop_on_gate_fail = true\n\n\
         [[nodes]]\nid = \"a\"\nspec = \"стадия A\"\n\
         gates = [{ command = { cmd = \"false\", timeout_secs = 10 } }]\non_fail = \"block\"\n\n\
         [[nodes]]\nid = \"b\"\nspec = \"стадия B\"\ngates = [\"contract\"]\n",
    );
    fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--plan")
        .arg(&plan)
        .assert()
        .failure()
        .code(1)
        .stdout(contains("Остановлен по гейту"))
        .stdout(contains("пропущено 1"));
}

#[test]
fn review_pair_not_ready_blocks_integration() {
    let (home, repo, config) = home_with_fleet(true);
    let plan = write_plan(
        home.path(),
        "run-review.plan.toml",
        "id = \"run-review\"\npattern = \"review_pair\"\n\n[policy]\nmax_parallel = 2\n\
         require_worktree = true\nmerge_gate = \"owner\"\n\n[defaults]\nroute = \"fast\"\ngates = [\"contract\"]\n\n\
         [[nodes]]\nid = \"impl\"\nspec = \"реализовать модуль\"\n",
    );
    let assert = fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--plan")
        .arg(&plan)
        .assert()
        .failure()
        .code(1);
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    assert!(stdout.contains("review-impl"), "{stdout}");
    // Вердикт ревьювера зафиксирован в журнале как fail.
    let run_id = run_id_of(&stdout);
    let journal = std::fs::read_to_string(
        home.path()
            .join("state/fleet")
            .join(format!("{run_id}.jsonl")),
    )
    .expect("журнал");
    assert!(
        journal.contains("\"gate\":\"review\",\"verdict\":\"fail\""),
        "{journal}"
    );
}

#[test]
fn tournament_judge_is_deterministic() {
    let (home, repo, config) = home_with_fleet(false);
    let plan = write_plan(
        home.path(),
        "run-tournament.plan.toml",
        "id = \"run-tournament\"\npattern = \"tournament\"\n\n[policy]\nmax_parallel = 2\n\
         require_worktree = true\n\n[defaults]\nroute = \"fast\"\ngates = [\"contract\"]\n\n\
         [[nodes]]\nid = \"try-b\"\nspec = \"вариант B\"\ngroup = \"solve\"\n\n\
         [[nodes]]\nid = \"try-a\"\nspec = \"вариант A\"\ngroup = \"solve\"\n",
    );
    let mut winners = Vec::new();
    for _ in 0..2 {
        let assert = fleet_cmd(home.path(), &config)
            .arg("run")
            .arg("--repo")
            .arg(repo.as_os_str())
            .arg("--plan")
            .arg(&plan)
            .assert()
            .success();
        let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
        let run_id = run_id_of(&stdout);
        let journal = std::fs::read_to_string(
            home.path()
                .join("state/fleet")
                .join(format!("{run_id}.jsonl")),
        )
        .expect("журнал");
        let winner = journal
            .lines()
            .find_map(|l| {
                l.split("победитель группы: ")
                    .nth(1)
                    .and_then(|s| s.split('"').next())
            })
            .map(str::to_string)
            .expect("судья выбрал победителя");
        winners.push(winner);
    }
    // Детерминизм: два прогона — один и тот же победитель (первый по id).
    assert_eq!(winners[0], winners[1]);
    assert_eq!(winners[0], "try-a");
}

#[test]
fn resume_skips_completed_nodes() {
    let (home, repo, config) = home_with_fleet(false);
    let plan = write_plan(
        home.path(),
        "run-resume.plan.toml",
        "id = \"run-resume\"\npattern = \"fanout\"\n\n[policy]\nmax_parallel = 2\n\
         require_worktree = true\n\n[defaults]\nroute = \"fast\"\ngates = [\"contract\"]\n\n\
         [[nodes]]\nid = \"n1\"\nspec = \"модуль 1\"\n\n[[nodes]]\nid = \"n2\"\nspec = \"модуль 2\"\n",
    );
    let assert = fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--plan")
        .arg(&plan)
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    let run_id = run_id_of(&stdout);

    // Возобновление: оба узла уже завершены — повторно не запускаются.
    fleet_cmd(home.path(), &config)
        .arg("resume")
        .arg(&run_id)
        .arg("--repo")
        .arg(repo.as_os_str())
        .assert()
        .success()
        .stdout(contains("пропущено 2"))
        .stdout(contains("завершено 0"));
}

#[test]
fn resume_without_worktree_requires_force() {
    let (home, repo, config) = home_with_fleet(false);
    let plan = write_plan(
        home.path(),
        "run-noworktree.plan.toml",
        "id = \"run-noworktree\"\npattern = \"fanout\"\n\n[policy]\nmax_parallel = 1\n\
         require_worktree = false\n\n[defaults]\nroute = \"fast\"\ngates = [\"contract\"]\n\n\
         [[nodes]]\nid = \"n1\"\nspec = \"модуль\"\n",
    );
    let assert = fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--plan")
        .arg(&plan)
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    let run_id = run_id_of(&stdout);

    fleet_cmd(home.path(), &config)
        .arg("resume")
        .arg(&run_id)
        .arg("--repo")
        .arg(repo.as_os_str())
        .assert()
        .failure()
        .stderr(contains("force-rerun"));
}

#[test]
fn status_json_summarises_run() {
    let (home, repo, config) = home_with_fleet(false);
    let plan = write_plan(
        home.path(),
        "run-status.plan.toml",
        "id = \"run-status\"\npattern = \"fanout\"\n\n[policy]\nmax_parallel = 1\n\
         require_worktree = true\n\n[defaults]\nroute = \"fast\"\ngates = [\"contract\"]\n\n\
         [[nodes]]\nid = \"n1\"\nspec = \"модуль\"\n",
    );
    let assert = fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--plan")
        .arg(&plan)
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    let run_id = run_id_of(&stdout);

    let assert = fleet_cmd(home.path(), &config)
        .arg("status")
        .arg(&run_id)
        .arg("--json")
        .assert()
        .success();
    let text = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    let value: serde_json::Value = serde_json::from_str(&text).expect("валидный JSON");
    assert_eq!(value["skipped_lines"].as_array().map(Vec::len), Some(0));
    assert!(
        value["node_gates"]["n1"]
            .as_str()
            .unwrap_or("")
            .contains("contract")
    );
}

#[test]
fn legacy_items_file_still_runs() {
    let (home, repo, config) = home_with_fleet(false);
    let items = write_plan(
        home.path(),
        "items.json",
        "[{\"id\":\"i1\",\"spec\":\"задача\",\"required_skills\":[],\"tags\":[],\"route\":\"fast\",\"effort\":1}]",
    );
    fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--items-file")
        .arg(&items)
        .arg("--package")
        .arg("legacy")
        .assert()
        .success()
        .stdout(contains("успешно 1"));
}

#[test]
fn run_requires_plan_or_items() {
    let (home, repo, config) = home_with_fleet(false);
    fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .assert()
        .failure()
        .stderr(contains("--plan"));
}

#[test]
fn dependent_node_sees_dependency_work_in_its_worktree() {
    // Регрессия: ревьювер и интегратор работают в СВОЁМ worktree — без
    // вливания ветки зависимости они видели бы пустое дерево.
    let home = tempfile::tempdir().expect("tempdir");
    let repo = home.path().join("repo");
    std::fs::create_dir_all(&repo).expect("mkdir repo");
    git(&repo, &["init", "-q", "-b", "main"]);
    git(&repo, &["config", "user.email", "test@example.com"]);
    git(&repo, &["config", "user.name", "test"]);
    std::fs::write(repo.join("README.md"), "база\n").expect("write");
    git(&repo, &["add", "-A"]);
    git(&repo, &["commit", "-qm", "base"]);

    let script = write_marker_harness(home.path());
    let config = home.path().join("config.toml");
    let text = format!(
        "[paths]\nstate_dir = '{}'\nreports_dir = '{}'\nsessions_dir = '{}'\nassets_dir = '{}'\n\n\
         [harnesses.fake]\nbinary = '{}'\nprompt_mode = 'stdin'\ntimeout_secs = 30\n\
         idle_timeout_secs = 0\nauto_commit = true\n",
        home.path().join("state").display(),
        home.path().join("reports").display(),
        home.path().join("sessions").display(),
        home.path().join("assets").display(),
        script.display(),
    );
    std::fs::write(&config, text).expect("config.toml");

    let plan = write_plan(
        home.path(),
        "run-dep.plan.toml",
        "id = \"run-dep\"\npattern = \"pipeline\"\norder = [\"a\", \"b\"]\n\n\
         [policy]\nmax_parallel = 1\nrequire_worktree = true\n\n\
         [[nodes]]\nid = \"a\"\nspec = \"создать артефакт\"\ngates = [\"contract\"]\n\n\
         [[nodes]]\nid = \"b\"\nspec = \"использовать артефакт\"\n\
         gates = [{ outputs = { paths = [\"dep_marker.txt\"] } }]\n",
    );
    let assert = fleet_cmd(home.path(), &config)
        .arg("run")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--plan")
        .arg(&plan)
        .assert()
        .success();
    let stdout = String::from_utf8_lossy(&assert.get_output().stdout).into_owned();
    assert!(stdout.contains("завершено 2"), "{stdout}");
    let run_id = run_id_of(&stdout);
    let journal = std::fs::read_to_string(
        home.path()
            .join("state/fleet")
            .join(format!("{run_id}.jsonl")),
    )
    .expect("журнал");
    assert!(
        journal.contains("\"gate\":\"dependency:a\",\"verdict\":\"ok\""),
        "{journal}"
    );
}
