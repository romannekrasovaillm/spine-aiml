//! Независимая проверка лечения дефекта относительных путей в mcp.json.
//!
//! Дефект: у `McpServerConfig` не было поля `cwd`, `connect_server` делал
//! `Command::new(cmd).args(args)` без `current_dir`, поэтому относительный
//! аргумент (`./servers/x/server.py`) резолвился от cwd харнесса, а не от
//! каталога объявившего манифеста. Симптом — «таймаут initialize», потому что
//! stderr дочернего процесса выбрасывался.
//!
//! Эти тесты ходят ТОЛЬКО через публичный API библиотеки
//! (`plugin::discover` / `plugin::mcp_servers` / `mcp::load_servers` /
//! `McpManager::connect`), а не через приватные функции.

mod common;

use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use arch_harness::mcp::{McpManager, McpServerConfig, load_servers};
use arch_harness::plugin;
use common::arch_cmd;

/// Фейковый stdio MCP-сервер: отвечает на initialize и tools/list.
const FAKE_SERVER_PY: &str = r#"#!/usr/bin/env python3
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "fake", "version": "0"}}}), flush=True)
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": "echo", "description": "d",
                       "inputSchema": {"type": "object"}}]}}), flush=True)
"#;

/// Тот же сервер, но сначала заливает stderr (проверка отсутствия дедлока
/// и ограничения буфера стока).
const FLOOD_SERVER_PY: &str = r#"#!/usr/bin/env python3
import sys, json
for i in range(40000):
    sys.stderr.write("noise-line-%d-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n" % i)
sys.stderr.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "flood", "version": "0"}}}), flush=True)
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": "echo", "description": "d",
                       "inputSchema": {"type": "object"}}]}}), flush=True)
"#;

fn write_script(dir: &Path, name: &str, body: &str) -> PathBuf {
    let path = dir.join(name);
    std::fs::write(&path, body).expect("write script");
    let mut perms = std::fs::metadata(&path).expect("metadata").permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&path, perms).expect("chmod");
    path
}

/// Сценарий дефекта: плагин в tempdir, в корневом `mcp.json` ОТНОСИТЕЛЬНЫЙ
/// путь `./server.py`. Харнесс запущен из другого cwd, поэтому без `cwd`
/// от каталога плагина сервер не поднимается.
#[tokio::test]
async fn plugin_relative_manifest_connects_from_foreign_cwd() {
    let root = tempfile::tempdir().expect("tempdir");
    let plug = root.path().join("myplug");
    std::fs::create_dir_all(&plug).expect("mkdir");
    std::fs::write(
        plug.join("plugin.json"),
        r#"{"name":"myplug","version":"0.1.0"}"#,
    )
    .expect("plugin.json");
    std::fs::write(
        plug.join("mcp.json"),
        r#"{"mcpServers":{"srv":{"command":"python3","args":["-u","./server.py"]}}}"#,
    )
    .expect("mcp.json");
    write_script(&plug, "server.py", FAKE_SERVER_PY);

    let plugins = plugin::discover(&[root.path().to_path_buf()]);
    assert!(
        plugins.iter().any(|p| p.manifest.name == "myplug"),
        "плагин не обнаружен: {plugins:?}"
    );
    let servers = plugin::mcp_servers(&plugins);
    let srv = servers
        .iter()
        .find(|s| s.name == "myplug.srv")
        .expect("myplug.srv");
    // Ключевое утверждение лечения: cwd — каталог объявившего манифеста.
    assert_eq!(
        srv.cwd.as_deref(),
        Some(plug.as_path()),
        "cwd не от плагина"
    );

    let manager = McpManager::connect(&servers, 10)
        .await
        .expect("относительный манифест должен подключаться");
    let tools = manager.tools().await;
    assert!(
        tools.iter().any(|t| t.name.contains("echo")),
        "нет tools: {:?}",
        tools.iter().map(|t| &t.name).collect::<Vec<_>>()
    );
    manager.shutdown().await;
}

/// Пользовательский `mcp.json` с относительным путём: cwd = каталог файла.
#[tokio::test]
async fn user_config_relative_path_resolves_from_file_dir() {
    let dir = tempfile::tempdir().expect("tempdir");
    write_script(dir.path(), "server.py", FAKE_SERVER_PY);
    let cfg_path = dir.path().join("mcp.json");
    std::fs::write(
        &cfg_path,
        r#"{"mcpServers":{"u":{"command":"python3","args":["-u","./server.py"]}}}"#,
    )
    .expect("mcp.json");

    let servers = load_servers(&cfg_path).expect("load_servers");
    assert_eq!(servers[0].cwd.as_deref(), Some(dir.path()));
    let manager = McpManager::connect(&servers, 10)
        .await
        .expect("пользовательский относительный mcp.json должен подключаться");
    assert!(
        manager
            .tools()
            .await
            .iter()
            .any(|t| t.name.contains("echo"))
    );
    manager.shutdown().await;
}

/// Абсолютный путь в mcp.json продолжает работать.
#[tokio::test]
async fn absolute_path_in_manifest_still_connects() {
    let dir = tempfile::tempdir().expect("tempdir");
    let script = write_script(dir.path(), "server.py", FAKE_SERVER_PY);
    let cfg_path = dir.path().join("mcp.json");
    std::fs::write(
        &cfg_path,
        format!(
            r#"{{"mcpServers":{{"a":{{"command":"python3","args":["-u","{}"]}}}}}}"#,
            script.display()
        ),
    )
    .expect("mcp.json");
    let servers = load_servers(&cfg_path).expect("load_servers");
    let manager = McpManager::connect(&servers, 10)
        .await
        .expect("абсолютный путь должен подключаться");
    manager.shutdown().await;
}

/// cwd = None — прежнее поведение, наследующее cwd процесса: не падает.
#[tokio::test]
async fn cwd_none_keeps_previous_behavior() {
    let dir = tempfile::tempdir().expect("tempdir");
    let script = write_script(dir.path(), "server.py", FAKE_SERVER_PY);
    let servers = vec![McpServerConfig {
        name: "none".into(),
        command: "python3".into(),
        args: vec!["-u".into(), script.to_string_lossy().into_owned()],
        env: BTreeMap::new(),
        cwd: None,
    }];
    let manager = McpManager::connect(&servers, 10)
        .await
        .expect("cwd=None не должен ломать запуск по абсолютному пути");
    manager.shutdown().await;
}

/// Контроль: ТОТ ЖЕ относительный манифест, но cwd принудительно None —
/// подключение обязано сорваться (доказывает, что именно cwd несёт лечение).
#[tokio::test]
async fn same_relative_manifest_without_cwd_fails() {
    let dir = tempfile::tempdir().expect("tempdir");
    write_script(dir.path(), "server.py", FAKE_SERVER_PY);
    let servers = vec![McpServerConfig {
        name: "nocwd".into(),
        command: "python3".into(),
        args: vec!["-u".into(), "./server.py".into()],
        env: BTreeMap::new(),
        cwd: None,
    }];
    let err = match McpManager::connect(&servers, 3).await {
        Ok(m) => {
            m.shutdown().await;
            panic!("без cwd относительный путь не должен резолвиться из чужого каталога");
        }
        Err(e) => e,
    };
    eprintln!("[control] ошибка без cwd: {err}");
}

/// Диагностика правдива на уровне CLI: битый сервер печатает в stderr
/// РЕАЛЬНУЮ причину (Errno/No such file), а не только «таймаут initialize».
///
/// Примечание независимого проверяющего: возвращаемая `McpManager::connect`
/// ошибка осталась агрегатной («подробности — в логе»); причина видна в
/// stderr-логе (tracing, уровень warn по умолчанию). Этот тест фиксирует
/// именно поведение, которое видит пользователь `arch-ml mcp list`.
#[test]
fn broken_server_cli_reports_real_cause_on_stderr() {
    let home = tempfile::tempdir().expect("tempdir");
    let arch_home = home.path().join(".arch-ml");
    std::fs::create_dir_all(&arch_home).expect("mkdir");
    std::fs::write(
        arch_home.join("mcp.json"),
        r#"{"mcpServers":{"broken":{"command":"python3","args":["-u","./missing-server.py"]}}}"#,
    )
    .expect("mcp.json");

    let out = arch_cmd(home.path())
        .args(["mcp", "list"])
        .output()
        .expect("запуск arch-ml");
    assert!(!out.status.success(), "битый сервер → ненулевой код");
    let stderr = String::from_utf8_lossy(&out.stderr);
    eprintln!("[cli stderr]\n{stderr}");
    assert!(
        stderr.contains("No such file") || stderr.contains("Errno 2"),
        "в stderr нет реальной причины: {stderr}"
    );
    assert!(
        stderr.contains("missing-server.py"),
        "в stderr нет имени недостающего файла: {stderr}"
    );
    assert!(
        stderr.contains("процесс завершился"),
        "в stderr нет кода выхода процесса: {stderr}"
    );
}

/// Сервер, заливающий stderr, не вызывает дедлок и не течёт бесконечно.
#[tokio::test]
async fn stderr_flood_does_not_deadlock() {
    let root = tempfile::tempdir().expect("tempdir");
    let plug = root.path().join("floodplug");
    std::fs::create_dir_all(&plug).expect("mkdir");
    std::fs::write(
        plug.join("plugin.json"),
        r#"{"name":"floodplug","version":"0.1.0"}"#,
    )
    .expect("plugin.json");
    std::fs::write(
        plug.join("mcp.json"),
        r#"{"mcpServers":{"srv":{"command":"python3","args":["-u","./flood.py"]}}}"#,
    )
    .expect("mcp.json");
    write_script(&plug, "flood.py", FLOOD_SERVER_PY);

    let plugins = plugin::discover(&[root.path().to_path_buf()]);
    let servers = plugin::mcp_servers(&plugins);
    let manager = tokio::time::timeout(Duration::from_secs(15), McpManager::connect(&servers, 10))
        .await
        .expect("дедлок: connect не завершился за 15с")
        .expect("сервер с флудом в stderr должен подключаться");
    assert!(
        manager
            .tools()
            .await
            .iter()
            .any(|t| t.name.contains("echo"))
    );
    manager.shutdown().await;
}
