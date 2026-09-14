//! Интеграционные тесты CLI-контракта `arch-ml` (ревью `SPINE-REVIEW.md`,
//! находка F-7 / задача P0-5; решения — `docs/adr/ADR-005-ci-and-cli-tests.md`).
//!
//! Все тесты детерминированы и офлайн: дом изолирован в tempdir
//! (см. [`common::arch_cmd`]), живые LLM и кодовые харнессы не вызываются.

mod common;

use std::path::{Path, PathBuf};

use predicates::prelude::*;
use predicates::str::contains;

use common::arch_cmd;

/// Пишет исполняемый shell-скрипт фейкового кодового харнесса: печатает
/// в stdout headless JSON-контракт результата (fenced json-блок со
/// `status`) и завершается нулём. Возвращает путь к скрипту.
fn write_fake_harness(dir: &Path, contract_json: &str) -> PathBuf {
    let script = dir.join("fake-harness.sh");
    let body = format!("#!/bin/sh\necho '```json'\necho '{contract_json}'\necho '```'\n");
    std::fs::write(&script, body).expect("запись fake-харнесса");
    // +x: без права на исполнение spawn вернёт PermissionDenied.
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

/// Готовая команда `arch-ml harness-run fake`: контракт `contract_json`
/// печатает фейковый харнесс, прописанный в тестовом config.toml.
fn harness_run_cmd(home: &Path, contract_json: &str) -> assert_cmd::Command {
    let script = write_fake_harness(home, contract_json);
    // Харнесс-адаптер собирается из конфига: бинарь — наш скрипт, задача
    // уходит в stdin (скрипт её игнорирует), авто-коммит выключен (репо —
    // не git), таймауты малые, чтобы зависший прогон падал быстро.
    let config = home.join("config.toml");
    let text = format!(
        "[harnesses.fake]\nbinary = '{}'\nprompt_mode = 'stdin'\n\
         timeout_secs = 30\nidle_timeout_secs = 0\nauto_commit = false\n",
        script.display()
    );
    std::fs::write(&config, text).expect("запись config.toml");
    let repo = home.join("repo");
    std::fs::create_dir_all(&repo).expect("mkdir repo");
    let mut cmd = arch_cmd(home);
    cmd.arg("--config")
        .arg(config.as_os_str())
        .arg("harness-run")
        .arg("fake")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--task")
        .arg("тестовая задача");
    cmd
}

/// CONSTRAINTS.yaml с одним правилом `file_exists` уровня error.
fn constraints_yaml(path: &str) -> String {
    format!(
        "rules:\n  - name: spine_present\n    type: file_exists\n    path: \"{path}\"\n    severity: error\n"
    )
}

/// Читатель пайпа, закрывший его раньше (`| head`, `| less -F`): бинарь
/// обязан тихо выйти с кодом 0, а не паниковать «Broken pipe» (регрессия:
/// `arch-ml models | head` падал кодом 101 — std паникует на EPIPE).
#[test]
fn stdout_closed_early_exits_zero_not_panic() {
    use std::io::Read as _;
    let home = tempfile::tempdir().expect("tempdir");
    let bin = assert_cmd::cargo::cargo_bin("arch-ml");
    let mut child = std::process::Command::new(bin)
        .arg("--help")
        .env("HOME", home.path())
        .env("XDG_CONFIG_HOME", home.path().join(".config"))
        .current_dir(home.path())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("spawn arch-ml --help");
    let mut stdout = child.stdout.take().expect("stdout pipe");
    let mut buf = [0u8; 64];
    let _ = stdout.read(&mut buf);
    drop(stdout); // читатель ушёл — следующая запись даст EPIPE
    let out = child.wait_with_output().expect("wait");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        out.status.success(),
        "код {:?}: EPIPE не должен паниковать; stderr: {stderr}",
        out.status.code()
    );
    assert!(!stderr.contains("panicked"), "паника broken pipe: {stderr}");
}

/// Репозиторий-фикстура с `.arch-handoff/CONSTRAINTS.yaml`.
fn repo_with_constraints(home: &Path, constraints: &str) -> PathBuf {
    let repo = home.join("repo");
    let handoff = repo.join(".arch-handoff");
    std::fs::create_dir_all(&handoff).expect("mkdir .arch-handoff");
    std::fs::write(handoff.join("CONSTRAINTS.yaml"), constraints).expect("запись CONSTRAINTS.yaml");
    repo
}

/// `arch-ml init` в изолированном доме создаёт конфиг и ассеты; повторный
/// запуск не затирает пользовательские правки (F-7: идемпотентность init).
#[test]
fn init_creates_config_and_assets_and_preserves_user_edits() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let home = tmp.path();

    arch_cmd(home)
        .arg("init")
        .assert()
        .success()
        .stdout(contains("Инициализация завершена"));

    let config = home.join(".config/arch-ml/config.toml");
    let asset = home.join(".arch-ml/assets/prompts/architect.md");
    assert!(config.is_file(), "конфиг создан: {}", config.display());
    assert!(asset.is_file(), "ассет развёрнут: {}", asset.display());

    // Пользовательские правки: маркер в ассете и смена модели по умолчанию.
    let mut edited = std::fs::read_to_string(&asset).expect("read asset");
    edited.push_str("\n<!-- правка пользователя -->\n");
    std::fs::write(&asset, edited).expect("write asset");
    let cfg_text = std::fs::read_to_string(&config).expect("read config");
    let cfg_text = cfg_text.replace("default_model = \"deepseek\"", "default_model = \"glm\"");
    assert!(
        cfg_text.contains("default_model = \"glm\""),
        "правка конфига применилась до повторного init"
    );
    std::fs::write(&config, cfg_text).expect("write config");

    arch_cmd(home).arg("init").assert().success();

    let asset_after = std::fs::read_to_string(&asset).expect("re-read asset");
    assert!(
        asset_after.contains("<!-- правка пользователя -->"),
        "повторный init затёр правку ассета"
    );
    let cfg_after = std::fs::read_to_string(&config).expect("re-read config");
    assert!(
        cfg_after.contains("default_model = \"glm\""),
        "повторный init потерял пользовательскую модель:\n{cfg_after}"
    );
}

/// `arch-ml control check` на падающем CONSTRAINTS.yaml (обязательный файл
/// отсутствует, severity error) → отчёт FAIL и exit 1 (скриптовый гейт
/// fitness-функций).
#[test]
fn control_check_failing_constraints_exits_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = repo_with_constraints(tmp.path(), &constraints_yaml("docs/ARCHITECTURE-SPINE.md"));

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control").arg("check").arg(repo.as_os_str());
    cmd.assert()
        .code(1)
        .stdout(contains("Итог: FAIL"))
        .stdout(contains("spine_present"));
}

/// Проходящий CONSTRAINTS.yaml (обязательный файл на месте) → PASS, exit 0.
#[test]
fn control_check_passing_constraints_exits_0() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = repo_with_constraints(tmp.path(), &constraints_yaml("docs/ARCHITECTURE-SPINE.md"));
    let docs = repo.join("docs");
    std::fs::create_dir_all(&docs).expect("mkdir docs");
    std::fs::write(docs.join("ARCHITECTURE-SPINE.md"), "# Spine\n").expect("write spine");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control").arg("check").arg(repo.as_os_str());
    cmd.assert().success().stdout(contains("Итог: PASS"));
}

/// Дефолтный поиск ruleset'а: рабочий `<repo>/CONSTRAINTS.yaml` приоритетнее
/// пакетной заготовки `.arch-handoff/CONSTRAINTS.yaml` (C-037, ADR-011 п. 5).
#[test]
fn control_check_defaults_to_working_ruleset_over_packet() {
    let tmp = tempfile::tempdir().expect("tempdir");
    // Пакетная заготовка сломала бы прогон: требует отсутствующий файл.
    let repo = repo_with_constraints(tmp.path(), &constraints_yaml("PACKET-ONLY.md"));
    // Рабочий ruleset в корне: требование выполнено.
    std::fs::write(
        repo.join("CONSTRAINTS.yaml"),
        constraints_yaml("WORKING.md"),
    )
    .expect("рабочий ruleset");
    std::fs::write(repo.join("WORKING.md"), "# ok\n").expect("working file");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control").arg("check").arg(repo.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("Итог: PASS"))
        .stdout(contains("(рабочий ruleset)"));
}

/// Только пакетный файл — fallback: источник объявлен как пакетная заготовка,
/// а в stderr уходит предупреждение (PASS по заготовке — не полный контроль).
#[test]
fn control_check_packet_fallback_warns_about_stub() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = repo_with_constraints(tmp.path(), &constraints_yaml("docs/SPINE.md"));
    let docs = repo.join("docs");
    std::fs::create_dir_all(&docs).expect("mkdir docs");
    std::fs::write(docs.join("SPINE.md"), "# spine\n").expect("write spine");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control").arg("check").arg(repo.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("(пакетная заготовка)"))
        .stderr(contains("проверяется пакетная заготовка"));
}

/// `arch-ml control spine` на чистом spine-файле → exit 0.
#[test]
fn control_spine_clean_exits_0() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let spine = tmp.path().join("ARCHITECTURE-SPINE.md");
    std::fs::write(
        &spine,
        "# Spine\n\n## AD-1: Первый инвариант\n\n- **Binds**: a ↔ b\n\
         - **Prevents**: дрейф\n- **Rule**: правило. Страж: C-01.\n\
         - **Статус**: [ADOPTED]\n",
    )
    .expect("write spine");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control").arg("spine").arg(spine.as_os_str());
    cmd.assert().success().stdout(contains("нарушений нет"));
}

/// `arch-ml control spine` с error-находкой (дубль AD-id) → exit 1
/// (скриптовый гейт spine-линтера в CI).
#[test]
fn control_spine_error_exits_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let spine = tmp.path().join("ARCHITECTURE-SPINE.md");
    std::fs::write(
        &spine,
        "# Spine\n\n## AD-1: Первый\n\n- **Binds**: a\n- **Prevents**: b\n- **Rule**: c.\n\n\
         ## AD-1: Дубль\n\n- **Binds**: a\n- **Prevents**: b\n- **Rule**: c.\n",
    )
    .expect("write spine");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control").arg("spine").arg(spine.as_os_str());
    cmd.assert().code(1).stdout(contains("dup_ad_id"));
}

/// `harness-run` со `status=blocked` в контракте → exit 2 (скриптовый гейт
/// в пайпах, см. `docs/harness_integrations.md`).
#[test]
fn harness_run_blocked_contract_exits_2() {
    let tmp = tempfile::tempdir().expect("tempdir");
    harness_run_cmd(
        tmp.path(),
        r#"{"status": "blocked", "assumptions": [], "open_questions": ["нужен доступ к КШД"], "conflicts_with_prior_decisions": []}"#,
    )
    .assert()
    .code(2)
    .stdout(contains("status=blocked"));
}

/// `harness-run` с непустыми `conflicts_with_prior_decisions` → exit 3
/// (конфликт со spine останавливает интеграцию по контракту).
#[test]
fn harness_run_conflicts_exit_3() {
    let tmp = tempfile::tempdir().expect("tempdir");
    harness_run_cmd(
        tmp.path(),
        r#"{"status": "complete", "conflicts_with_prior_decisions": ["AD-2 запрещает vendor lock-in"]}"#,
    )
    .assert()
    .code(3)
    .stdout(contains("conflicts=1"));
}

/// `harness-run` с чистым `complete` (списки пусты) → exit 0.
#[test]
fn harness_run_complete_exits_0() {
    let tmp = tempfile::tempdir().expect("tempdir");
    harness_run_cmd(tmp.path(), r#"{"status": "complete"}"#)
        .assert()
        .success()
        .stdout(contains("status=complete"));
}

/// `arch-ml fleet run` маршрутизирует items на известный харнесс и пишет
/// стрим-журнал; `fleet status latest` читает его в dashboard. Офлайн:
/// известное имя `claude-code` переопределено на shell-скрипт, LLM не нужен.
#[test]
fn fleet_run_routes_and_status_renders_dashboard() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let script = write_fake_harness(tmp.path(), r#"{"status": "complete"}"#);
    let config = tmp.path().join("config.toml");
    let text = format!(
        "[harnesses.claude-code]\nbinary = '{}'\nprompt_mode = 'stdin'\n\
         timeout_secs = 30\nidle_timeout_secs = 0\nauto_commit = false\n",
        script.display()
    );
    std::fs::write(&config, text).expect("config");
    let repo = tmp.path().join("repo");
    std::fs::create_dir_all(&repo).expect("repo");
    let items = tmp.path().join("items.json");
    std::fs::write(
        &items,
        r#"[{"id": "ADR-1", "spec": "инвариант периметра", "route": "fast"}]"#,
    )
    .expect("items");

    let mut run = arch_cmd(tmp.path());
    run.arg("--config")
        .arg(&config)
        .arg("fleet")
        .arg("run")
        .arg("--repo")
        .arg(&repo)
        .arg("--items-file")
        .arg(&items)
        .arg("--package")
        .arg("cli-test");
    run.assert()
        .success()
        .stdout(contains("назначено 1 / успешно 1"))
        .stdout(contains("ADR-1 → claude-code"));

    let mut status = arch_cmd(tmp.path());
    status
        .arg("--config")
        .arg(&config)
        .arg("fleet")
        .arg("status")
        .arg("latest");
    status
        .assert()
        .success()
        .stdout(contains("cli-test"))
        .stdout(contains("done"));
}

/// `arch-ml mermaid` рендерит пример из репозитория без единого ключа
/// (no-LLM смоук из AGENTS.md).
#[test]
fn mermaid_renders_example_without_keys() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let diagram = Path::new(env!("CARGO_MANIFEST_DIR")).join("examples/mermaid/flow.mmd");
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("mermaid").arg(diagram.as_os_str());
    cmd.assert().success().stdout(contains("API Gateway"));
}

/// `arch-ml doctor` без единого API-ключа в окружении не падает: отчёт
/// рендерится полностью, без паники и трейса ошибки в stderr. Код 1 —
/// задокументированный контракт (Fail «нет ключа модели по умолчанию»,
/// `src/doctor.rs`; отступление от буквы `DoD` ревью — ADR-005 §7).
#[test]
fn doctor_without_keys_reports_problems_and_exits_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    arch_cmd(tmp.path())
        .arg("doctor")
        .assert()
        .code(1)
        .stdout(contains("arch-ml doctor"))
        .stdout(contains("нет ключа модели по умолчанию"))
        .stdout(contains("Итог:"))
        .stderr(contains("Error:").not());
}

/// Синтетический кейс для `arch-ml trace check`: `model/` с одним AD,
/// CONSTRAINTS.yaml, spine. `with_rule` — связывает AD с правилом C-001.
fn trace_case(home: &Path, with_rule: bool) -> PathBuf {
    let case = home.join("case");
    let model = case.join("model");
    std::fs::create_dir_all(&model).expect("mkdir model");
    let verified = if with_rule {
        "verified_by: [C-001]"
    } else {
        ""
    };
    std::fs::write(
        model.join("AD-1.md"),
        format!("---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\n{verified}\n---\n"),
    )
    .expect("write AD");
    std::fs::write(
        case.join("CONSTRAINTS.yaml"),
        "constraints:\n  - id: C-001\n    name: правило\n",
    )
    .expect("write constraints");
    std::fs::write(case.join("ARCHITECTURE-SPINE.md"), "## AD-1: Инвариант\n")
        .expect("write spine");
    case
}

/// `arch-ml trace check`: спайн покрыт правилом → PASS, exit 0 (ADR-006).
#[test]
fn trace_check_covered_spine_exits_0() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let case = trace_case(tmp.path(), true);
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("trace").arg("check").arg(case.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("AD → fitness-правило | 1/1 | 100%"))
        .stdout(contains("Итог: PASS"));
}

/// `arch-ml trace check`: AD без правила и без `unverifiable` → FAIL, exit 1
/// (скриптовый гейт CI флота, ADR-006).
#[test]
fn trace_check_uncovered_ad_exits_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let case = trace_case(tmp.path(), false);
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("trace").arg("check").arg(case.as_os_str());
    cmd.assert()
        .code(1)
        .stdout(contains("ad-not-verified"))
        .stdout(contains("Итог: FAIL"));
}

/// Синтетический кейс для `arch-ml nfr`: `model/` с INT-hop'ом и NFR с целью
/// p99. `hop_budget_ms` — бюджет hop'а (None — hop без бюджета).
fn nfr_budget_case(home: &Path, target_ms: u32, hop_budget_ms: Option<u32>) -> PathBuf {
    let case = home.join("nfr-case");
    let model = case.join("model");
    std::fs::create_dir_all(&model).expect("mkdir model");
    let budget = hop_budget_ms.map_or(String::new(), |b| format!("latency_budget_ms: {b}\n"));
    std::fs::write(
        model.join("INT-001-hop.md"),
        format!("---\nid: INT-001\ntype: int\ntitle: Hop\nstatus: accepted\n{budget}---\n"),
    )
    .expect("write INT");
    std::fs::write(
        model.join("NFR-001-lat.md"),
        format!(
            "---\nid: NFR-001\ntype: nfr\ntitle: Latency\nstatus: accepted\n\
             verification: histogram\np99_target_ms: {target_ms}\naffects: [INT-001]\n---\n"
        ),
    )
    .expect("write NFR");
    case
}

/// `arch-ml nfr budget`: сумма hop'ов в пределах цели p99 → PASS, exit 0 (ADR-007).
#[test]
fn nfr_budget_converging_exits_0() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let case = nfr_budget_case(tmp.path(), 2000, Some(800));
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("nfr").arg("budget").arg(case.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("резерв: 1200 мс"))
        .stdout(contains("Итог: PASS"));
}

/// `arch-ml nfr budget`: сумма hop'ов выше цели p99 → error с виновными hop'ами,
/// exit 1 (`DoD` P1-1, скриптовый гейт).
#[test]
fn nfr_budget_exceeded_exits_1_with_guilty_hops() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let case = nfr_budget_case(tmp.path(), 2000, Some(3000));
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("nfr").arg("budget").arg(case.as_os_str());
    cmd.assert()
        .code(1)
        .stdout(contains("budget-exceeded"))
        .stdout(contains("INT-001=3000"))
        .stdout(contains("Итог: FAIL"));
}

/// `arch-ml nfr budget`: hop без заявленного бюджета → error, exit 1.
#[test]
fn nfr_budget_missing_hop_budget_exits_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let case = nfr_budget_case(tmp.path(), 2000, None);
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("nfr").arg("budget").arg(case.as_os_str());
    cmd.assert()
        .code(1)
        .stdout(contains("budget-hop-missing"))
        .stdout(contains("INT-001"))
        .stdout(contains("Итог: FAIL"));
}

/// `arch-ml run --max-turns 0` — лимит итераций не бывает нулевым
/// (`value_parser` range `1..`, код 2, без обращения к LLM).
#[test]
fn run_max_turns_zero_rejected_exits_2() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("run").arg("--max-turns").arg("0").arg("пинг");
    cmd.assert().code(2).stderr(contains("--max-turns"));
}

/// git-репозиторий `home/<name>` с baseline-коммитом и handoff-пакетом
/// (MANIFEST.json + ROLLBACK.yaml; `{BASELINE}` в плане подменяется на хеш).
fn repo_with_rollback_plan(home: &Path, name: &str, route: &str, plan_yaml: &str) -> PathBuf {
    let repo = home.join(name);
    std::fs::create_dir_all(&repo).expect("mkdir repo");
    let git = |args: &[&str]| {
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args(args)
            .env("GIT_AUTHOR_NAME", "t")
            .env("GIT_AUTHOR_EMAIL", "t@t")
            .env("GIT_COMMITTER_NAME", "t")
            .env("GIT_COMMITTER_EMAIL", "t@t")
            .output()
            .expect("git");
        assert!(out.status.success(), "git {args:?}: {:?}", out.stderr);
        String::from_utf8_lossy(&out.stdout).into_owned()
    };
    git(&["init", "-q", "-b", "main"]);
    std::fs::write(repo.join("README.md"), "base\n").expect("readme");
    git(&["add", "."]);
    git(&["commit", "-q", "-m", "baseline"]);
    let baseline = git(&["rev-parse", "HEAD"]).trim().to_string();
    let packet = repo.join(".arch-handoff");
    std::fs::create_dir_all(&packet).expect("mkdir packet");
    std::fs::write(
        packet.join("MANIFEST.json"),
        format!("{{\"route\": \"{route}\"}}\n"),
    )
    .expect("manifest");
    std::fs::write(
        packet.join("ROLLBACK.yaml"),
        plan_yaml.replace("{BASELINE}", &baseline),
    )
    .expect("plan");
    repo
}

/// Безопасный план отката: проверка якоря + откат на baseline + verify.
const SAFE_PLAN: &str = "baseline_commit: \"{BASELINE}\"\n\
     steps:\n\
     \x20 - name: якорь-доступен\n\
     \x20   run: git cat-file -t {BASELINE}\n\
     \x20 - name: откат-на-baseline\n\
     \x20   run: git reset --hard {BASELINE}\n\
     verify: test -z \"$(git status --porcelain --untracked-files=no)\"\n";

/// `arch control gate A4 <repo> --rehearse`: безопасный план репетируется во
/// временном worktree → PASS, exit 0, evidence REHEARSAL.json записан.
#[test]
fn control_gate_a4_rehearse_pass_exits_0() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = repo_with_rollback_plan(tmp.path(), "repo-gate", "Critical", SAFE_PLAN);

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control")
        .arg("gate")
        .arg("A4")
        .arg(repo.as_os_str())
        .arg("--rehearse");
    cmd.assert()
        .success()
        .stdout(contains("Репетиция отката"))
        .stdout(contains("отрепетирован"))
        .stdout(contains("Итог: PASS"));
    assert!(
        repo.join(".arch-handoff/REHEARSAL.json").is_file(),
        "evidence репетиции записан в пакет"
    );
}

/// Шаг с деструктивной командой (`rm -rf`) отклоняется с диагностикой →
/// FAIL, exit 1; основной репозиторий не тронут.
#[test]
fn control_gate_a4_refuses_destructive_step_exits_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let plan = "baseline_commit: \"{BASELINE}\"\n\
                steps:\n\
                \x20 - name: снести-всё\n\
                \x20   run: rm -rf README.md\n";
    let repo = repo_with_rollback_plan(tmp.path(), "repo-gate", "Critical", plan);

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control")
        .arg("gate")
        .arg("A4")
        .arg(repo.as_os_str())
        .arg("--rehearse");
    cmd.assert()
        .code(1)
        .stdout(contains("REFUSED"))
        .stdout(contains("не репетируется"))
        .stdout(contains("Итог: FAIL"));
    assert!(repo.join("README.md").is_file(), "шаг не выполнялся");
}

/// Critical без успешной репетиции гейт A4 не проходит (exit 1); маршрут Fast
/// при дефолтном пороге — advisory (exit 0 без репетиции).
#[test]
fn control_gate_a4_requires_rehearsal_only_for_critical_by_default() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = repo_with_rollback_plan(tmp.path(), "repo-gate", "Critical", SAFE_PLAN);
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control")
        .arg("gate")
        .arg("A4")
        .arg(repo.as_os_str());
    cmd.assert()
        .code(1)
        .stdout(contains("--rehearse"))
        .stdout(contains("Итог: FAIL"));

    let fast2 = repo_with_rollback_plan(tmp.path(), "repo-gate-fast", "Fast", SAFE_PLAN);
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control")
        .arg("gate")
        .arg("A4")
        .arg(fast2.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("не обязательна"))
        .stdout(contains("Итог: PASS"));
}

/// Нереализованный гейт — понятная ошибка.
#[test]
fn control_gate_unknown_gate_errors() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = repo_with_rollback_plan(tmp.path(), "repo-gate", "Critical", SAFE_PLAN);
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("control")
        .arg("gate")
        .arg("A3")
        .arg(repo.as_os_str());
    cmd.assert().failure().stderr(contains("только A4"));
}

/// `arch eval run`: встроенный сьют agent-config прогоняется герметично
/// (временный дом, без ключей и сети), гейт 100% проходит, JSON-отчёт
/// пишется в `<дом>/evals/` (docs/evals.md).
#[test]
fn eval_builtin_suite_passes_offline_and_writes_report() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("eval").arg("run");
    cmd.assert()
        .success()
        .stdout(contains("Eval-сьют 'agent-config': задач 8, пропущено 0"))
        .stdout(contains("✓ mermaid-render"))
        .stdout(contains("✓ policy-denies-destructive"))
        .stdout(contains("Pass-rate: 100.0% (8/8), гейт 100.0% — PASS"));

    // JSON-отчёт: <tempdir>/.arch-ml/evals/eval-agent-config-*.json.
    let evals = tmp.path().join(".arch-ml/evals");
    let reports: Vec<PathBuf> = std::fs::read_dir(&evals)
        .expect("каталог evals создан")
        .flatten()
        .map(|e| e.path())
        .collect();
    assert_eq!(reports.len(), 1, "ровно один отчёт: {reports:?}");
    let text = std::fs::read_to_string(&reports[0]).expect("read json");
    assert!(text.contains("\"gate_passed\": true"), "{text}");
    assert!(text.contains("\"pass_rate\": 100.0"), "{text}");
}

/// Гейт ломает код выхода: пользовательский сьют с заведомо красной
/// задачей → exit 1 (регрессионный контракт для CI).
#[test]
fn eval_gate_below_pass_rate_exits_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let suite = tmp.path().join("suite");
    std::fs::create_dir_all(&suite).expect("mkdir");
    std::fs::write(
        suite.join("01-red.yaml"),
        "id: red-task\ntitle: Заведомо красная задача\ncommand: \"echo тихо\"\nchecks:\n  - type: must_contain\n    pattern: \"отсутствует\"\n",
    )
    .expect("write suite");
    std::fs::write(
        suite.join("02-green.yaml"),
        "id: green-task\ntitle: Зелёная задача\ncommand: \"echo тихо\"\nchecks:\n  - type: must_contain\n    pattern: \"тихо\"\n",
    )
    .expect("write suite");
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("eval")
        .arg("run")
        .arg("--suite")
        .arg(suite.as_os_str());
    cmd.assert()
        .code(1)
        .stdout(contains("✗ red-task"))
        .stdout(contains("Pass-rate: 50.0% (1/2), гейт 100.0% — FAIL"));

    // Пониженный гейт пропускает тот же сьют.
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("eval")
        .arg("run")
        .arg("--suite")
        .arg(suite.as_os_str())
        .arg("--gate")
        .arg("50");
    cmd.assert().success().stdout(contains("гейт 50.0% — PASS"));
}

/// `arch-ml preflight` на облачном спеке с чёрным хостом и перерасходом
/// бюджета: стоп-ошибки + exit 1.
#[test]
fn preflight_cloud_blacklisted_host_and_budget_exit_1() {
    let home = tempfile::tempdir().expect("tmp");
    let spec = home.path().join("spec.toml");
    std::fs::write(
        &spec,
        r#"
name = "demo"
resource = "cloud"
params_b = 3.0
num_gpus = 2
gpu_vram_gb = 40.0
host_id = "14804"
framework = "verl"
price_per_hour = 2.0
expected_hours = 8.0
"#,
    )
    .expect("write spec");
    arch_cmd(home.path())
        .arg("preflight")
        .arg(spec.as_os_str())
        .assert()
        .code(1)
        .stdout(contains("хост 14804 исключён"))
        .stdout(contains("согласие владельца"));
}

/// Локальный GB10 проходит pre-flight (бюджет/хост не применяются) — exit 0.
#[test]
fn preflight_local_gb10_exit_0() {
    let home = tempfile::tempdir().expect("tmp");
    let spec = home.path().join("spec.toml");
    std::fs::write(
        &spec,
        r#"
name = "laguna-1.5b"
resource = "local"
device = "gb10"
params_b = 1.5
model_name = "Qwen2.5-1.5B-Instruct"
framework = "verl"
kl_coef = 0.01
prompt_format = "chat"
seed = 7
gpu_memory_utilization = 0.32
"#,
    )
    .expect("write spec");
    arch_cmd(home.path())
        .arg("preflight")
        .arg(spec.as_os_str())
        .assert()
        .success()
        .stdout(contains("unified 128 ГБ"))
        .stdout(contains("аренда $0"));
}

/// `--example` печатает образец спецификации и выходит с кодом 0.
#[test]
fn preflight_example_prints_and_exit_0() {
    let home = tempfile::tempdir().expect("tmp");
    arch_cmd(home.path())
        .arg("preflight")
        .arg("--example")
        .assert()
        .success()
        .stdout(contains("resource = \"cloud\""));
}

/// `arch-ml resources list` показывает объявленный локальный инвентарь
/// (дефолты: gb10 + rtx4080super) и реестр устройств.
#[test]
fn resources_list_shows_local_inventory() {
    let home = tempfile::tempdir().expect("tmp");
    arch_cmd(home.path())
        .arg("resources")
        .arg("list")
        .assert()
        .success()
        .stdout(contains("gb10"))
        .stdout(contains("128 ГБ"))
        .stdout(contains("rtx4080super"))
        .stdout(contains("unified"))
        .stdout(contains("ssh gb10-fast"));
}

/// `arch-ml resources recommend` ставит локальное железо ($0) первым
/// (статический VRAM из реестра, без live-детекции — герметично).
#[test]
fn resources_recommend_local_first() {
    let home = tempfile::tempdir().expect("tmp");
    arch_cmd(home.path())
        .arg("resources")
        .arg("recommend")
        .arg("--params-b")
        .arg("1.5")
        .assert()
        .success()
        .stdout(contains("local:gb10"))
        .stdout(contains("влезает ($0)"))
        .stdout(contains("ssh gb10-fast"))
        .stdout(contains("cloud"));
}

/// Датасет-карточка без пробелов (все `REQUIRED_FIELDS` объявлены) с
/// заявленным `sha256`: годится для проверки ветки противоречий.
const FULL_CARD_WITH_SHA: &str = "dataset: demo\nsource: https://example.test/demo\n\
     license: MIT\nsplits: [train, test]\ntokenizer: qwen\ndedup: exact\n\
     contamination: none\nsha256: ba7816bf\n";

/// Записать карточку `name` в каталог `dir`; вернуть путь к файлу.
fn write_card(dir: &Path, name: &str, yaml: &str) -> PathBuf {
    std::fs::create_dir_all(dir).expect("mkdir cards");
    let path = dir.join(name);
    std::fs::write(&path, yaml).expect("write card");
    path
}

/// `--dataset` на несуществующий путь: объявленный `sha256` даёт проблему
/// (ядро `dataset_card`), с `--strict` — exit 1 (D4: ветка противоречий
/// достижима из CLI).
#[test]
fn data_card_absent_dataset_reports_problem_and_strict_exits_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let cards = tmp.path().join("cards");
    write_card(&cards, "demo.yaml", FULL_CARD_WITH_SHA);
    let absent = tmp.path().join("data/demo.jsonl");

    // Без --strict: проблема видна в тексте, пробелов нет — код 0.
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("data-card")
        .arg("check")
        .arg("--cards")
        .arg(cards.as_os_str())
        .arg("--dataset")
        .arg(absent.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("проблема:"))
        .stdout(contains("sha256"))
        .stdout(contains("отсутствующем датасете"))
        .stdout(contains("demo.jsonl"))
        .stdout(contains("проблем: 1"));

    // --strict: противоречие — провал с разбивкой в тексте ошибки.
    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("data-card")
        .arg("check")
        .arg("--cards")
        .arg(cards.as_os_str())
        .arg("--dataset")
        .arg(absent.as_os_str())
        .arg("--strict");
    cmd.assert()
        .code(1)
        .stderr(contains("проблем 1"))
        .stderr(contains("пробелов 0"));
}

/// `--dataset` на существующий файл: противоречия нет, exit 0 (пробелов в
/// карточке тоже нет).
#[test]
fn data_card_existing_dataset_has_no_problem_exits_0() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let cards = tmp.path().join("cards");
    write_card(&cards, "demo.yaml", FULL_CARD_WITH_SHA);
    let present = tmp.path().join("data/demo.jsonl");
    std::fs::create_dir_all(present.parent().expect("каталог датасета")).expect("mkdir data");
    std::fs::write(&present, b"{}").expect("write dataset");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("data-card")
        .arg("check")
        .arg("--cards")
        .arg(cards.as_os_str())
        .arg("--dataset")
        .arg(present.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("проблем: 0"))
        .stdout(contains("проблема:").not());
}

/// Без `--dataset` противоречие не выдумывается: «файла нет» по неизвестному
/// пути не доказано, ложный красный запрещён.
#[test]
fn data_card_without_dataset_hides_problems_exits_0() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let cards = tmp.path().join("cards");
    write_card(&cards, "demo.yaml", FULL_CARD_WITH_SHA);

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("data-card")
        .arg("check")
        .arg("--cards")
        .arg(cards.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("проблем: 0"))
        .stdout(contains("проблема:").not());
}

/// Пробелы обязательных полей + `--strict` → exit 1 (существующее поведение
/// не сломано, разбивка в тексте ошибки).
#[test]
fn data_card_strict_gaps_exit_1() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let cards = tmp.path().join("cards");
    write_card(&cards, "demo.yaml", "dataset: demo\n");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("data-card")
        .arg("check")
        .arg("--cards")
        .arg(cards.as_os_str())
        .arg("--strict");
    cmd.assert()
        .code(1)
        .stdout(contains("пробелы:"))
        .stderr(contains("пробелов 6"))
        .stderr(contains("проблем 0"));
}

/// Пишет фикстурный каталог решений ADR внутри `repo/docs/adr`.
fn write_adr_fixture(repo: &Path, name: &str, body: &str) {
    let dir = repo.join("docs/adr");
    std::fs::create_dir_all(&dir).expect("каталог docs/adr");
    std::fs::write(dir.join(name), body).expect("запись ADR");
}

/// Документ ADR с frontmatter (`extra` — дополнительные YAML-строки).
fn adr_fixture_doc(id: &str, title: &str, status: &str, extra: &str) -> String {
    format!("---\nid: {id}\ntitle: {title}\nstatus: {status}\n{extra}---\n\n# {id}. {title}\n")
}

/// `fleet plan propose --from-adrs` пишет черновик плана (exit 0), когда
/// независимые решения не пересекаются путями записи (ADR-045).
#[test]
fn fleet_plan_propose_from_adrs_writes_plan() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = tmp.path().join("repo");
    std::fs::create_dir_all(&repo).expect("repo");
    write_adr_fixture(
        &repo,
        "ADR-001-first.md",
        &adr_fixture_doc("ADR-001", "Первое решение", "proposed", ""),
    );
    write_adr_fixture(
        &repo,
        "ADR-002-second.md",
        &adr_fixture_doc(
            "ADR-002",
            "Второе решение",
            "proposed",
            "depends_on: [ADR-001]\naffects:\n  - src/b.rs\n",
        ),
    );
    let out = tmp.path().join("plans");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("fleet")
        .arg("plan")
        .arg("propose")
        .arg("--from-adrs")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--out")
        .arg(out.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("План записан"))
        .stdout(contains("docs-adr-from-adrs"))
        .stdout(contains("ADR-001"))
        .stdout(contains("ADR-002"));

    let plans: Vec<PathBuf> = std::fs::read_dir(&out)
        .expect("каталог планов")
        .filter_map(|entry| entry.ok().map(|e| e.path()))
        .filter(|path| path.extension().is_some_and(|e| e == "toml"))
        .collect();
    assert_eq!(plans.len(), 1, "{plans:?}");
    let text = std::fs::read_to_string(&plans[0]).expect("план читается");
    assert!(text.contains("id = \"adr-001\""), "{text}");
    assert!(text.contains("depends_on = [\"adr-001\"]"), "{text}");
}

/// Два независимых решения объявили запись в один путь — механический гейт
/// отказывает: exit 1, диагностика называет обе стороны и пересечение.
#[test]
fn fleet_plan_propose_from_adrs_refuses_shared_output_path() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = tmp.path().join("repo");
    std::fs::create_dir_all(&repo).expect("repo");
    for (name, id) in [("ADR-010-a.md", "ADR-010"), ("ADR-011-b.md", "ADR-011")] {
        write_adr_fixture(
            &repo,
            name,
            &adr_fixture_doc(
                id,
                "Правка TUI-слоя",
                "proposed",
                "affects:\n  - src/tui/app.rs\n",
            ),
        );
    }
    let out = tmp.path().join("plans");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("fleet")
        .arg("plan")
        .arg("propose")
        .arg("--from-adrs")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--out")
        .arg(out.as_os_str());
    cmd.assert()
        .code(1)
        .stdout(contains("adr-010"))
        .stdout(contains("adr-011"))
        .stdout(contains("src/tui/app.rs"));
}

/// Отбор по статусу: `--status accepted` берёт принятые решения (одно узлов);
/// без `--from-adrs` прежний режим MANIFEST не тронут.
#[test]
fn fleet_plan_propose_from_adrs_status_filter() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let repo = tmp.path().join("repo");
    std::fs::create_dir_all(&repo).expect("repo");
    write_adr_fixture(
        &repo,
        "ADR-020-p.md",
        &adr_fixture_doc("ADR-020", "Предложенное", "proposed", ""),
    );
    write_adr_fixture(
        &repo,
        "ADR-021-a.md",
        &adr_fixture_doc("ADR-021", "Принятое", "accepted", ""),
    );
    let out = tmp.path().join("plans");

    let mut cmd = arch_cmd(tmp.path());
    cmd.arg("fleet")
        .arg("plan")
        .arg("propose")
        .arg("--from-adrs")
        .arg("--status")
        .arg("accepted")
        .arg("--repo")
        .arg(repo.as_os_str())
        .arg("--out")
        .arg(out.as_os_str());
    cmd.assert()
        .success()
        .stdout(contains("ADR-021"))
        .stdout(contains("ADR-020").not());
}
