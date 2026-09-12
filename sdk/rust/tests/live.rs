//! Живые интеграционные тесты SDK против настоящего бинаря `arch-ml`
//! и эталонных фикстур репозитория (§5 контракта).
//!
//! Тесты пропускаются (skip), если бинарь не найден: искать через
//! `SPINE_BE_BIN`, затем `arch-ml` в PATH. LLM-прогон `run` здесь
//! намеренно не выполняется (стоимость/время, §5) — его покрывают
//! юнит-тесты на скрипте-заглушке в `src/client.rs`.

use spine_be_sdk::{ArchifyReceipt, Client};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

/// Счётчик уникальных имён временных каталогов тестов.
static TEMP_SEQ: AtomicUsize = AtomicUsize::new(0);

/// Корень репозитория: sdk/rust → ../.. .
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .expect("корень репозитория spine-bank")
}

/// Ищет бинарь `arch-ml`; `None` — живые тесты пропускаются.
fn find_binary() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("SPINE_BE_BIN") {
        let path = PathBuf::from(path);
        return path.is_file().then_some(path);
    }
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths).find_map(|dir| {
            let candidate = dir.join("arch-ml");
            candidate.is_file().then_some(candidate)
        })
    })
}

/// Клиент против живого бинаря или пропуск теста с пояснением в лог.
fn live_client() -> Option<Client> {
    let binary = find_binary()?;
    eprintln!("живой тест: arch-ml = {}", binary.display());
    Some(Client {
        binary,
        default_timeout: Duration::from_secs(120),
        cwd: None,
    })
}

/// Фикстуры сценариев банковского слоя (§5 контракта).
fn fixtures() -> (PathBuf, PathBuf, PathBuf) {
    let root = repo_root();
    let demos = root.join("banking/demos/cli-from-claude-code");
    (
        demos.join("scenario2-archify-cli/sbp-v1.architecture.json"),
        demos.join("scenario2-archify-cli/sbp-v2.architecture.json"),
        demos.join("scenario3-gate/fixtures"),
    )
}

/// Уникальный временный каталог теста.
fn temp_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "spine-be-sdk-live-{}-{}-{tag}",
        std::process::id(),
        TEMP_SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&dir).expect("создать временный каталог теста");
    dir
}

/// Рекурсивная копия каталога (без внешних зависимостей).
fn copy_dir(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).expect("создать каталог-копию");
    for entry in std::fs::read_dir(src).expect("прочитать исходный каталог") {
        let entry = entry.expect("элемент каталога");
        let target = dst.join(entry.file_name());
        let file_type = entry.file_type().expect("тип элемента каталога");
        if file_type.is_dir() {
            copy_dir(&entry.path(), &target);
        } else {
            std::fs::copy(entry.path(), &target).expect("скопировать файл");
        }
    }
}

/// Сводка compare-receipt'а как JSON (иначе — паника с диагностикой).
fn compare_summary(receipt: &ArchifyReceipt) -> &serde_json::Value {
    receipt
        .summary()
        .expect("compare-receipt обязан нести summary по §3.3 контракта")
}

#[test]
fn control_check_green_fixture_passes() {
    let Some(client) = live_client() else {
        eprintln!("skip: arch-ml не найден (SPINE_BE_BIN / PATH)");
        return;
    };
    let (_, _, gate) = fixtures();
    let report = client
        .control_check(&gate, Some(&gate.join("CONSTRAINTS.yaml")))
        .expect("зелёный гейт: валидный отчёт");
    assert!(report.passed, "зелёная фикстура обязана проходить гейт");
    assert!(report.issues.is_empty(), "нарушений быть не должно");
}

#[test]
fn control_check_red_fixture_is_data() {
    let Some(client) = live_client() else {
        eprintln!("skip: arch-ml не найден (SPINE_BE_BIN / PATH)");
        return;
    };
    let (_, _, gate) = fixtures();
    // Красный кейс по §5: временная копия фикстуры + файл с 16-значным числом.
    let red = temp_dir("red").join("fixtures");
    copy_dir(&gate, &red);
    std::fs::write(
        red.join("src/bad.py"),
        "# (c) Банк, внутренний контур\n\
         PAN = \"4276550012345678\"  # тестовая находка\n\
         IDEMPOTENCY = \"Idempotency-Key\"\n",
    )
    .expect("записать bad.py");

    // exit 1 + passed=false — данные, а не исключение (§4 контракта).
    let report = client
        .control_check(&red, Some(&red.join("CONSTRAINTS.yaml")))
        .expect("красный гейт: валидный отчёт-данные");
    assert!(!report.passed, "файл с PAN обязан ронять гейт");
    let pan_issue = report
        .issues
        .iter()
        .find(|issue| issue.rule == "no_pan_in_code")
        .expect("ожидалась находка по правилу no_pan_in_code");
    assert_eq!(pan_issue.file, "src/bad.py");
    assert_eq!(pan_issue.severity, "error");
}

#[test]
fn archify_validate_green_ir() {
    let Some(client) = live_client() else {
        eprintln!("skip: arch-ml не найден (SPINE_BE_BIN / PATH)");
        return;
    };
    let (v1, _, _) = fixtures();
    let receipt = client
        .archify_validate("architecture", &v1)
        .expect("validate: receipt по контракту");
    assert_eq!(receipt.schema_version(), Some(1));
    assert!(receipt.ok(), "эталонный IR обязан валидироваться");
    assert_eq!(receipt.command(), Some("validate"));
    assert_eq!(receipt.diagram_type(), Some("architecture"));
    let checks = receipt.checks().expect("validate несёт список проверок");
    assert_eq!(checks.len(), 9, "контрактные 9 проверок валидации");
    assert!(
        checks.iter().all(|c| c.get("ok").and_then(|v| v.as_bool()) == Some(true)),
        "все проверки эталонного IR зелёные"
    );
}

#[test]
fn archify_deliver_builds_html_in_tmp() {
    let Some(client) = live_client() else {
        eprintln!("skip: arch-ml не найден (SPINE_BE_BIN / PATH)");
        return;
    };
    let (v1, _, _) = fixtures();
    let out_html = temp_dir("deliver").join("sbp-v1.html");
    let receipt = client
        .archify_deliver("architecture", &v1, &out_html)
        .expect("deliver: receipt по контракту");
    assert!(receipt.ok(), "deliver эталонного IR обязан быть ok");
    assert_eq!(receipt.command(), Some("deliver"));
    let artifact = receipt.artifact().expect("deliver несёт сведения об артефакте");
    assert!(
        artifact.get("bytes").and_then(|b| b.as_u64()).unwrap_or(0) > 0,
        "артефакт непустой: {artifact}"
    );
    assert!(
        out_html.is_file() && out_html.metadata().map(|m| m.len() > 0).unwrap_or(false),
        "HTML-артефакт записан во временный каталог"
    );
}

#[test]
fn archify_compare_counts_delta() {
    let Some(client) = live_client() else {
        eprintln!("skip: arch-ml не найден (SPINE_BE_BIN / PATH)");
        return;
    };
    let (v1, v2, _) = fixtures();
    let out_html = temp_dir("compare").join("sbp-delta.html");
    let receipt = client
        .archify_compare(&v1, &v2, &out_html)
        .expect("compare: receipt по контракту");
    assert!(receipt.ok(), "compare эталонной пары обязан быть ok");
    assert_eq!(receipt.command(), Some("compare"));
    let summary = compare_summary(&receipt);
    assert_eq!(summary["components"]["added"], 1, "components.added");
    assert_eq!(summary["connections"]["added"], 2, "connections.added");
    assert!(out_html.is_file(), "delta HTML записан");
}
