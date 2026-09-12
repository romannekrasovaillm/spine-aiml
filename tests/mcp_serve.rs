//! Процессные тесты MCP-серверного режима `arch-ml mcp serve` (P1-2, ADR-008):
//! дочернему процессу скармливаются JSON-RPC запросы через stdin, NDJSON-
//! ответы читаются из stdout. Всё детерминированно и офлайн: дом изолирован
//! в tempdir (см. [`common::arch_cmd`]), живой LLM не вызывается (тест
//! `rubric_run` с ключом — `#[ignore]`).

mod common;

use std::path::Path;

use serde_json::{Value, json};

use common::arch_cmd;

/// Прогоняет пачку NDJSON-запросов через дочерний `arch-ml mcp serve`
/// (stdin закрывается после записи — сервер завершается по EOF) и
/// возвращает разобранные ответы в порядке выдачи.
fn mcp_serve(home: &Path, requests: &str) -> Vec<Value> {
    let output = arch_cmd(home)
        .args(["mcp", "serve"])
        .write_stdin(requests)
        .output()
        .expect("запуск arch-ml mcp serve");
    assert!(
        output.status.success(),
        "сервер завершился сбоем: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).expect("stdout — utf8");
    stdout
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).expect("каждая строка stdout — валидный JSON-RPC"))
        .collect()
}

/// Одна строка запроса `tools/call`.
fn call(id: u64, name: &str, args: &Value) -> String {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    .to_string()
}

/// Склеивает запросы в NDJSON-пачку.
fn batch(requests: &[String]) -> String {
    let mut out = requests.join("\n");
    out.push('\n');
    out
}

/// Структурированный verdict успешного вызова инструмента.
fn structured(response: &Value, idx: u64) -> &Value {
    assert_eq!(response["id"], idx, "id ответа");
    assert_eq!(
        response["result"]["isError"], false,
        "вызов не должен быть isError: {response}"
    );
    &response["result"]["structuredContent"]
}

/// Фикстура spine-файла: чистый либо с дублем AD-1 и пустым полем.
/// Каждый вариант — свой подкаталог `name` (пути не должны пересекаться).
fn spine_fixture(home: &Path, name: &str, broken: bool) -> String {
    let dir = home.join(name);
    std::fs::create_dir_all(&dir).expect("mkdir spine");
    let path = dir.join("ARCHITECTURE-SPINE.md");
    let body = if broken {
        "### AD-1. Брокер\n- Binds: контур\n- Prevents: хаос\n- Rule: только брокер\n\n\
         ### AD-1. Дубль\n- Binds:\n- Prevents: y\n- Rule: z\n"
    } else {
        "### AD-1. Брокер\n- Binds: контур\n- Prevents: хаос\n- Rule: только брокер\n"
    };
    std::fs::write(&path, body).expect("запись spine");
    path.display().to_string()
}

/// Фикстура репозитория `name` с CONSTRAINTS.yaml: правило `file_exists`
/// уровня error на файл, который есть либо нет.
fn repo_fixture(home: &Path, name: &str, create_required_file: bool) -> String {
    let repo = home.join(name);
    let handoff = repo.join(".arch-handoff");
    std::fs::create_dir_all(&handoff).expect("mkdir");
    std::fs::write(
        handoff.join("CONSTRAINTS.yaml"),
        "rules:\n  - name: spine_present\n    type: file_exists\n    path: \"ARCHITECTURE-SPINE.md\"\n    severity: error\n",
    )
    .expect("constraints");
    if create_required_file {
        std::fs::write(repo.join("ARCHITECTURE-SPINE.md"), "# spine\n").expect("spine");
    }
    repo.display().to_string()
}

/// Фикстура кейса трассировки: полный (AD-1 с правилом C-001) либо
/// рвущий обязательное звено (AD-1 без `verified_by`/`unverifiable`).
fn case_fixture(home: &Path, full: bool) -> String {
    let case = home.join("case");
    let model = case.join("model");
    std::fs::create_dir_all(&model).expect("mkdir model");
    let ad_links = if full { "\nverified_by: [C-001]" } else { "" };
    std::fs::write(
        model.join("AD-1.md"),
        format!("---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED{ad_links}\n---\n\nПравило.\n"),
    )
    .expect("AD");
    std::fs::write(
        model.join("CMP-001.md"),
        "---\nid: CMP-001\ntype: cmp\ntitle: Компонент\nstatus: designed\nimplements: [AD-1]\n---\n\nТело.\n",
    )
    .expect("CMP");
    std::fs::write(
        case.join("CONSTRAINTS.yaml"),
        "constraints:\n  - id: C-001\n    name: правило\n",
    )
    .expect("constraints");
    case.display().to_string()
}

/// Фикстура модели для `model_query`.
fn model_fixture(home: &Path) -> String {
    let model = home.join("model");
    std::fs::create_dir_all(&model).expect("mkdir model");
    std::fs::write(
        model.join("AD-1.md"),
        "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\n---\n\nПравило.\n",
    )
    .expect("AD");
    std::fs::write(
        model.join("ADR-001-x.md"),
        "---\nid: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted\nimplements: [AD-1]\n---\n\nКонтекст.\n",
    )
    .expect("ADR");
    model.display().to_string()
}

#[test]
fn handshake_then_tools_list_over_stdio() {
    let home = tempfile::tempdir().expect("tmp");
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{
                "protocolVersion":"2025-06-18","capabilities":{},
                "clientInfo":{"name":"claude-code","version":"1.0"}}})
            .to_string(),
            json!({"jsonrpc":"2.0","method":"notifications/initialized"}).to_string(),
            json!({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}).to_string(),
        ]),
    );
    assert_eq!(
        responses.len(),
        2,
        "уведомление не получает ответа: {responses:?}"
    );
    let init = &responses[0]["result"];
    assert_eq!(init["protocolVersion"], "2025-06-18");
    assert_eq!(init["serverInfo"]["name"], "arch-ml");
    assert!(init["capabilities"]["tools"].is_object());
    let tools = responses[1]["result"]["tools"].as_array().expect("tools");
    let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
    for want in [
        "spine_lint",
        "fitness_check",
        "significance_score",
        "trace_check",
        "model_query",
        "rubric_run",
        "kb_search",
        "skill_search",
        "skill_load",
        "mermaid_render",
    ] {
        assert!(names.contains(&want), "нет инструмента {want}: {names:?}");
    }
    assert_eq!(
        tools.len(),
        10,
        "ровно 10 инструментов (6 контрольных + 4 чтения знаний, T4)"
    );
}

#[test]
fn ping_and_unknown_method_and_broken_json() {
    let home = tempfile::tempdir().expect("tmp");
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            json!({"jsonrpc":"2.0","id":1,"method":"ping"}).to_string(),
            json!({"jsonrpc":"2.0","id":2,"method":"resources/list"}).to_string(),
            "{битый json".to_string(),
            json!({"jsonrpc":"2.0","id":3,"method":"ping"}).to_string(),
        ]),
    );
    assert_eq!(responses.len(), 4, "сервер ответил всем и остался жив");
    assert_eq!(responses[0]["result"], json!({}));
    assert_eq!(responses[1]["error"]["code"], -32601, "unknown method");
    assert_eq!(responses[2]["error"]["code"], -32700, "parse error");
    assert_eq!(responses[2]["id"], Value::Null);
    assert_eq!(responses[3]["result"], json!({}), "ping после мусора");
}

#[test]
fn unknown_tool_and_bad_arguments_are_32602() {
    let home = tempfile::tempdir().expect("tmp");
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            call(1, "ghost_tool", &json!({})),
            call(2, "spine_lint", &json!({})), // нет path
            call(3, "fitness_check", &json!({"repo": 42})), // repo не строка
            call(4, "trace_check", &json!({"case": null})), // case не строка
        ]),
    );
    for (i, r) in responses.iter().enumerate() {
        assert_eq!(r["error"]["code"], -32602, "ответ {}: {r}", i + 1);
    }
}

#[test]
fn spine_lint_verdict_blocks_and_passes() {
    let home = tempfile::tempdir().expect("tmp");
    let bad = spine_fixture(home.path(), "bad", true);
    let good = spine_fixture(home.path(), "good", false);
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            call(1, "spine_lint", &json!({"path": bad})),
            call(2, "spine_lint", &json!({"path": good})),
            call(
                3,
                "spine_lint",
                &json!({"path": home.path().join("ghost.md")}),
            ),
        ]),
    );
    let verdict = structured(&responses[0], 1);
    assert_eq!(verdict["passed"], false, "{verdict}");
    assert!(verdict["error_count"].as_u64().expect("число") >= 1);
    let rules: Vec<&str> = verdict["issues"]
        .as_array()
        .expect("issues")
        .iter()
        .filter_map(|i| i["rule"].as_str())
        .collect();
    assert!(rules.contains(&"dup_ad_id"), "{rules:?}");
    assert!(rules.contains(&"empty_field"), "{rules:?}");

    let verdict = structured(&responses[1], 2);
    assert_eq!(verdict["passed"], true, "{verdict}");
    assert_eq!(verdict["issue_count"], 0);

    // Файл не читается — доменный сбой: isError-результат, не protocol error.
    assert_eq!(responses[2]["result"]["isError"], true);
    assert!(responses[2].get("error").is_none());
}

#[test]
fn fitness_check_verdict_blocks_and_passes() {
    let home = tempfile::tempdir().expect("tmp");
    let broken = repo_fixture(home.path(), "broken-repo", false);
    let clean = repo_fixture(home.path(), "clean-repo", true);
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            call(1, "fitness_check", &json!({"repo": broken})),
            call(2, "fitness_check", &json!({"repo": clean})),
        ]),
    );
    let verdict = structured(&responses[0], 1);
    assert_eq!(verdict["passed"], false, "{verdict}");
    let issue = &verdict["issues"][0];
    assert_eq!(issue["rule"], "spine_present");
    assert_eq!(issue["severity"], "error");

    let verdict = structured(&responses[1], 2);
    assert_eq!(verdict["passed"], true, "{verdict}");
    assert!(
        verdict["summary"]
            .as_str()
            .expect("summary")
            .contains("Правил: 1")
    );
}

#[test]
fn significance_score_routes_change() {
    let home = tempfile::tempdir().expect("tmp");
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            call(
                1,
                "significance_score",
                &json!({"triggers": {"security_boundary_change": true}}),
            ),
            call(2, "significance_score", &json!({"triggers": {}})),
        ]),
    );
    let critical = structured(&responses[0], 1);
    assert_eq!(critical["route"], "Critical");
    assert_eq!(critical["score"], 1);
    let fast = structured(&responses[1], 2);
    assert_eq!(fast["route"], "Fast");
    assert_eq!(fast["score"], 0);
}

#[test]
fn trace_check_verdict_blocks_broken_chain() {
    let home = tempfile::tempdir().expect("tmp");
    let full = case_fixture(home.path(), true);
    let broken = case_fixture(&home.path().join("other"), false);
    // case_fixture пишет от переданного корня: для broken корень другой.
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            call(1, "trace_check", &json!({"case": full})),
            call(2, "trace_check", &json!({"case": broken})),
        ]),
    );
    let verdict = structured(&responses[0], 1);
    assert_eq!(verdict["passed"], true, "{verdict}");
    assert!(
        verdict["report_markdown"]
            .as_str()
            .expect("markdown")
            .contains("| Звено | Покрыто |")
    );

    let verdict = structured(&responses[1], 2);
    assert_eq!(verdict["passed"], false, "{verdict}");
    let rules: Vec<&str> = verdict["issues"]
        .as_array()
        .expect("issues")
        .iter()
        .filter_map(|i| i["rule"].as_str())
        .collect();
    assert!(rules.contains(&"ad-not-verified"), "{rules:?}");
}

#[test]
fn model_query_lists_and_shows_card() {
    let home = tempfile::tempdir().expect("tmp");
    let dir = model_fixture(home.path());
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            call(1, "model_query", &json!({"dir": dir, "type": "adr"})),
            call(2, "model_query", &json!({"dir": dir, "id": "ADR-001"})),
            call(3, "model_query", &json!({"dir": dir, "id": "CMP-999"})),
            call(4, "model_query", &json!({"dir": dir, "type": "widget"})),
        ]),
    );
    let list = structured(&responses[0], 1);
    assert_eq!(list["total"], 1, "фильтр adr: {list}");
    assert_eq!(list["entities"][0]["id"], "ADR-001");

    let card = structured(&responses[1], 2);
    assert_eq!(card["entity"]["id"], "ADR-001");
    assert_eq!(card["entity"]["links"]["implements"], json!(["AD-1"]));
    assert!(card["card"].as_str().expect("карточка").contains("Решение"));

    // Несуществующий id — доменная ошибка (isError), не protocol error.
    assert_eq!(responses[2]["result"]["isError"], true);
    // Несуществующий тип — ошибка параметров -32602.
    assert_eq!(responses[3]["error"]["code"], -32602);
}

#[test]
fn rubric_run_without_api_key_is_clear_json_rpc_error() {
    let home = tempfile::tempdir().expect("tmp");
    // arch_cmd снимает ключи провайдеров и изолирует HOME (файл-ключ kimi
    // тоже недоступен): дефолтный deepseek без ключа → -32603 с подсказкой.
    let responses = mcp_serve(
        home.path(),
        &batch(&[
            call(
                1,
                "rubric_run",
                &json!({"rubric": "solution_architecture", "target_text": "текст"}),
            ),
            call(2, "ping_tool", &json!({})), // неизвестный инструмент после ошибки
        ]),
    );
    let error = &responses[0]["error"];
    assert_eq!(error["code"], -32603, "{error}");
    let message = error["message"].as_str().expect("сообщение");
    assert!(message.contains("API-ключ"), "{message}");
    assert!(message.contains("DEEPSEEK_API_KEY"), "{message}");
    // Сервер жив после ошибки rubric_run.
    assert_eq!(responses[1]["error"]["code"], -32602);
}

/// Живой прогон `rubric_run` с реальным ключом (сеть + LLM): в общем гейте
/// пропускается, запуск — `cargo test --test mcp_serve -- --ignored`.
#[test]
#[ignore = "нужен API-ключ LLM и сеть (живой смоук)"]
fn rubric_run_live_with_key() {
    let home = tempfile::tempdir().expect("tmp");
    let doc = home.path().join("doc.md");
    std::fs::write(&doc, "# Архитектура\n\nСистема из одного скрипта.\n").expect("doc");
    // Без изоляции окружения: ключи берутся из env разработчика.
    let output = assert_cmd::Command::cargo_bin("arch-ml")
        .expect("бинарь")
        .args(["mcp", "serve"])
        .write_stdin(
            call(
                1,
                "rubric_run",
                &json!({"rubric": "solution_architecture", "target": doc.display().to_string()}),
            ) + "\n",
        )
        .output()
        .expect("запуск");
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stdout = String::from_utf8(output.stdout).expect("utf8");
    let response: Value =
        serde_json::from_str(stdout.lines().next().expect("ответ")).expect("json");
    if let Some(error) = response.get("error") {
        panic!("живой вызов не должен давать protocol error: {error}");
    }
    let result = &response["result"];
    assert!(
        result["isError"] != true,
        "живой вызов не должен давать isError: {result}"
    );
    let verdict = &result["structuredContent"];
    assert!(verdict["weighted_total"].as_f64().expect("балл") > 0.0);
    assert!(!verdict["scores"].as_array().expect("оценки").is_empty());
}
