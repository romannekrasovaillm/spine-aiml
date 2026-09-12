//! Демо «CLI-агент, который проверяет себя сам» (без LLM, детерминированное).
//!
//! Сюжет для показа архитекторам: кодовый агент пишет черновик платёжного
//! модуля и ПЕРЕД показом человеку прогоняет себя через гейт Spine-BE:
//! control check находит нарушение (PAN в коде), агент чинит, повторный
//! check зелёный; финал — валидация и доставка диаграммы через archify.
//! Человек видит только зелёный результат.
//!
//! Запуск: `cd sdk/rust && cargo run --offline --example agent_loop`
//! Бинарь: `SPINE_BE_BIN` → `target/release/arch-ml` репозитория → PATH.
//! Exit code: 0 — финал зелёный; 1 — любая стадия красная/сломана.

use spine_be_sdk::Client;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

/// Черновик агента (итерация 1): PAN прямо в коде, нет Idempotency-Key —
/// нарушает BANK-01 (и BANK-02) учебного набора CONSTRAINTS.yaml.
const DRAFT_PAYMENTS: &str = r#"# (c) Банк, внутренний контур
"""Черновик платёжного модуля (кодовый агент, итерация 1)."""


def charge_card(amount: int) -> dict:
    """Списание с карты. FIXME: тестовая карта из тикета PAY-118."""
    pan = "4276550012345678"  # TODO: убрать до код-ревью
    return {"pan": pan, "amount": amount, "status": "charged"}
"#;

/// Исправленная версия (итерация 2): PAN живёт в токенизаторе,
/// ключ идемпотентности — в заголовках запроса.
const FIXED_PAYMENTS: &str = r#"# (c) Банк, внутренний контур
"""Платёжный модуль (кодовый агент, итерация 2 — после гейта Spine-BE)."""

import uuid


def charge_card(pan_token: str, amount: int) -> dict:
    """Списание по токену карты. Ключ идемпотентности обязателен."""
    return {
        "pan_token": pan_token,  # PAN живёт только в токенизаторе
        "amount": amount,
        "headers": {"Idempotency-Key": str(uuid.uuid4())},
    }
"#;

/// Корень репозитория: sdk/rust → ../.. (без хардкода машинных путей).
fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from("../.."))
}

/// Бинарь arch-ml: env → сборка репозитория → имя для поиска в PATH.
fn resolve_binary() -> PathBuf {
    if let Some(path) = std::env::var_os("SPINE_BE_BIN") {
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    let built = repo_root().join("target/release/arch-ml");
    if built.is_file() {
        return built;
    }
    PathBuf::from("arch-ml")
}

fn main() -> ExitCode {
    match agent_loop() {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("[ДЕМО] сбой: {err}");
            ExitCode::FAILURE
        }
    }
}

/// Тело демо: мини-цикл агента RED → GREEN + archify. Ошибка = exit 1.
fn agent_loop() -> Result<(), String> {
    let root = repo_root();
    let constraints_src =
        root.join("banking/demos/cli-from-claude-code/scenario3-gate/fixtures/CONSTRAINTS.yaml");
    let ir = root.join("banking/demos/cli-from-claude-code/scenario2-archify-cli/sbp-v1.architecture.json");
    if !constraints_src.is_file() || !ir.is_file() {
        return Err(format!("фикстуры демо не найдены под {}", root.display()));
    }

    let client = Client {
        binary: resolve_binary(),
        default_timeout: Duration::from_secs(120),
        cwd: None,
    };

    // Воркдир прогона: «репозиторий», который видит агент и гейт.
    let work = std::env::temp_dir().join(format!("spine-be-agent-loop-{}", std::process::id()));
    let src = work.join("src");
    std::fs::create_dir_all(&src).map_err(|e| format!("создать воркдир: {e}"))?;
    let constraints = work.join("CONSTRAINTS.yaml");
    std::fs::copy(&constraints_src, &constraints)
        .map_err(|e| format!("скопировать CONSTRAINTS.yaml: {e}"))?;
    println!("[AGENT] воркдир прогона: {}", work.display());

    // --- Итерация 1: черновик с нарушением -------------------------------
    let payments = src.join("payments.py");
    std::fs::write(&payments, DRAFT_PAYMENTS).map_err(|e| format!("записать черновик: {e}"))?;
    println!("[AGENT] итерация 1: черновик src/payments.py (тикет PAY-118)");

    let red = client
        .control_check(&work, Some(&constraints))
        .map_err(|e| format!("control check (черновик): {e}"))?;
    if red.passed {
        return Err("черновик неожиданно прошёл гейт — демо сломано".to_string());
    }
    println!("[SPINE-BE] control check → КРАСНЫЙ гейт (exit 1 у CLI — данные, не сбой):");
    for issue in &red.issues {
        println!(
            "[SPINE-BE]   {}:{} [{}] {}",
            issue.file, issue.line, issue.rule, issue.message
        );
    }

    // --- Итерация 2: фикс -------------------------------------------------
    std::fs::write(&payments, FIXED_PAYMENTS).map_err(|e| format!("записать фикс: {e}"))?;
    println!("[AGENT] итерация 2: PAN — в токенизатор, Idempotency-Key — в заголовки");

    let green = client
        .control_check(&work, Some(&constraints))
        .map_err(|e| format!("control check (фикс): {e}"))?;
    if !green.passed {
        return Err(format!("фикс не прошёл гейт: {}", green.summary));
    }
    println!("[SPINE-BE] control check → ЗЕЛЁНЫЙ гейт ({})", green.summary);

    // --- Финал: диаграмма модуля через archify ----------------------------
    let receipt = client
        .archify_validate("architecture", &ir)
        .map_err(|e| format!("archify validate: {e}"))?;
    if !receipt.ok() {
        return Err("archify validate: receipt ok=false".to_string());
    }
    let checks = receipt.checks().map_or(0, <[_]>::len);
    println!("[SPINE-BE] archify validate architecture → ok ({checks} проверок)");

    let html = work.join("payments-architecture.html");
    let receipt = client
        .archify_deliver("architecture", &ir, &html)
        .map_err(|e| format!("archify deliver: {e}"))?;
    if !receipt.ok() {
        return Err("archify deliver: receipt ok=false".to_string());
    }
    let bytes = receipt
        .artifact()
        .and_then(|a| a.get("bytes"))
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    println!(
        "[SPINE-BE] archify deliver → {} ({bytes} байт)",
        html.display()
    );

    println!("[AGENT] человеку уходит только зелёный результат: код + диаграмма");
    Ok(())
}
