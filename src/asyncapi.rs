//! Линтер контрактов `AsyncAPI` 2.x/3.x — агентный инструмент `asyncapi_lint`.
//!
//! Walking skeleton контрактного контура (транш T1, ADR-015): пять правил
//! скелета AA-001..AA-005 — версионирование контракта (`info.version`),
//! наличие message/payload-схемы у операций и сообщений, стабильный id
//! события (`messageId`/`x-message-id`/`key`) на publish/send-операциях для
//! идемпотентности потребителя, наличие `servers` и каналов/операций. Вывод
//! находок — в стиле `spine_lint`: сводка, строки
//! `[severity] локация rule — message`, «Итог: PASS/FAIL».
//!
//! Парсинг — без новых зависимостей: `serde_json` (JSON) и `serde_yaml_ng`
//! (YAML) уже в Cargo.toml; документ представляется как `serde_json::Value`
//! (формат определяется по первому непробельному символу: `{` — JSON, иначе
//! YAML), навигация — по JSON Pointer. Generic-инструмент MIT-ядра:
//! доменной зоны не касается.
//!
//! Известные ограничения скелета (Deferred — резолюция `$ref` следующей
//! итерацией T1): message за `$ref` (и trait-поля 2.x, message через
//! `traits`) не обходятся — трактуются как отсутствующие; локализация
//! находок — JSON Pointer (`#/channels/paymentCreated/publish`), а не номер
//! строки (Value-парсинг исходные строки не сохраняет).

use std::fmt::Write as _;
use std::path::Path;
use std::sync::Arc;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Находка линтера контракта.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    /// Критичность: `error`|`warn`.
    pub severity: String,
    /// Код правила (`AA-001`..`AA-005`).
    pub rule: String,
    /// JSON Pointer (`#/channels/paymentCreated/publish`).
    pub location: String,
    /// Сообщение.
    pub message: String,
}

/// Прогоняет скелет-правила AA-001..AA-005 по контракту `AsyncAPI` 2.x/3.x.
///
/// Читает файл, определяет формат (JSON, если первый непробельный символ —
/// `{`, иначе YAML), парсит в [`Value`] и проверяет признак `AsyncAPI` 2.x/3.x
/// (поле `asyncapi` вида `2.x.y` или `3.x.y`). Находки — данные отчёта, не
/// ошибки выполнения: линтер отработал, если документ прочитан и распознан.
///
/// # Errors
/// Файл не читается, JSON/YAML не парсится, документ не `AsyncAPI` 2.x/3.x.
pub fn lint_asyncapi(path: &Path) -> Result<Vec<Finding>> {
    let content = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let doc = parse_contract(&content, path)?;
    let Some(major) = asyncapi_major(&doc) else {
        return Err(HarnessError::Tool(format!(
            "{}: не AsyncAPI 2.x/3.x: ожидается поле asyncapi вида «2.x.y» или «3.x.y»",
            path.display()
        )));
    };
    Ok(lint_document(&doc, major))
}

/// Разбирает текст контракта: `{` в начале — JSON, иначе YAML.
///
/// # Errors
/// Содержимое не парсится выбранным форматом.
fn parse_contract(content: &str, path: &Path) -> Result<Value> {
    let trimmed = content.trim_start();
    if trimmed.starts_with('{') {
        serde_json::from_str(trimmed)
            .map_err(|e| HarnessError::Tool(format!("{}: невалидный JSON: {e}", path.display())))
    } else {
        serde_yaml_ng::from_str(trimmed)
            .map_err(|e| HarnessError::Tool(format!("{}: невалидный YAML: {e}", path.display())))
    }
}

/// Старшая версия `AsyncAPI`: поле `asyncapi` вида `2.x.y` или `3.x.y`.
fn asyncapi_major(doc: &Value) -> Option<u32> {
    let version = doc.get("asyncapi").and_then(Value::as_str)?;
    let parts: Vec<&str> = version.split('.').collect();
    if parts.len() < 2 {
        return None;
    }
    let major = parts[0];
    if major != "2" && major != "3" {
        return None;
    }
    let minor_ok = parts[1..]
        .iter()
        .all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()));
    if !minor_ok {
        return None;
    }
    major.parse().ok()
}

/// Прогоняет все правила по разобранному документу.
fn lint_document(doc: &Value, major: u32) -> Vec<Finding> {
    let mut findings = Vec::new();
    lint_info(doc, &mut findings);
    lint_structure(doc, major, &mut findings);
    if major == 2 {
        lint_channels_v2(doc, &mut findings);
    } else {
        lint_operations_v3(doc, &mut findings);
    }
    findings
}

/// AA-001 (error): `info.version` отсутствует или не MAJOR.MINOR.PATCH.
fn lint_info(doc: &Value, out: &mut Vec<Finding>) {
    let Some(info) = doc.get("info") else {
        out.push(Finding {
            severity: "error".into(),
            rule: "AA-001".into(),
            location: "#/info".into(),
            message: "нет блока info — info.version отсутствует".into(),
        });
        return;
    };
    let version_field = info.get("version");
    let Some(version) = version_field.and_then(Value::as_str) else {
        let message = if version_field.is_some() {
            "info.version не строка".to_string()
        } else {
            "info.version отсутствует".to_string()
        };
        out.push(Finding {
            severity: "error".into(),
            rule: "AA-001".into(),
            location: "#/info".into(),
            message,
        });
        return;
    };
    if !is_semver(version) {
        out.push(Finding {
            severity: "error".into(),
            rule: "AA-001".into(),
            location: "#/info/version".into(),
            message: format!(
                "info.version «{version}» не в semver-форме MAJOR.MINOR.PATCH (например 1.2.3)"
            ),
        });
    }
}

/// Semver-форма MAJOR.MINOR.PATCH: три непустых десятичных компонента.
fn is_semver(version: &str) -> bool {
    let parts: Vec<&str> = version.split('.').collect();
    parts.len() == 3
        && parts
            .iter()
            .all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()))
}

/// AA-005 (warn): нет `servers` либо пуст контейнер событий (в 2.x —
/// `channels`, в 3.x — `operations`).
fn lint_structure(doc: &Value, major: u32, out: &mut Vec<Finding>) {
    if doc.get("servers").is_none() {
        out.push(Finding {
            severity: "warn".into(),
            rule: "AA-005".into(),
            location: "#/servers".into(),
            message: "нет servers — не заданы точки подключения".into(),
        });
    }
    let (key, label) = if major == 2 {
        ("channels", "каналов")
    } else {
        ("operations", "операций")
    };
    let has_events = doc
        .get(key)
        .and_then(Value::as_object)
        .is_some_and(|obj| !obj.is_empty());
    if !has_events {
        out.push(Finding {
            severity: "warn".into(),
            rule: "AA-005".into(),
            location: location(&[key]),
            message: format!("нет {label} — контракт без событий"),
        });
    }
}

/// Правила каналов 2.x: AA-002/AA-003/AA-004 на операциях `publish`/`subscribe`.
fn lint_channels_v2(doc: &Value, out: &mut Vec<Finding>) {
    let Some(channels) = doc.get("channels").and_then(Value::as_object) else {
        return;
    };
    for (channel, item) in channels {
        if item.get("$ref").is_some() {
            continue;
        }
        for op in ["publish", "subscribe"] {
            let Some(operation) = item.get(op) else {
                continue;
            };
            if operation.is_object() {
                lint_channel_operation_v2(channel, op, operation, out);
            }
        }
    }
}

/// Правила одной операции канала 2.x.
fn lint_channel_operation_v2(channel: &str, op: &str, operation: &Value, out: &mut Vec<Finding>) {
    let op_location = location(&["channels", channel, op]);

    // AA-002: операция без message — контракт события не задан.
    let Some(message) = operation.get("message") else {
        out.push(Finding {
            severity: "warn".into(),
            rule: "AA-002".into(),
            location: op_location,
            message: format!("{op} без message — контракт события не задан"),
        });
        return;
    };
    // message за `$ref` не разрешается (Deferred) — считается не заданным.
    if message.get("$ref").is_some() || !message.is_object() {
        return;
    }

    // AA-003: message без payload-схемы — нечего валидировать на границе.
    if message.get("payload").is_none() {
        out.push(Finding {
            severity: "warn".into(),
            rule: "AA-003".into(),
            location: format!("{op_location}/message"),
            message: "message без payload-схемы — нечего валидировать на границе".into(),
        });
    }

    // AA-004: publish-операция (событие владельца канала) без messageId —
    // потребитель не может дедуплицировать событие (аналог OA-003).
    if op == "publish" && !has_message_id_v2(message) {
        out.push(Finding {
            severity: "error".into(),
            rule: "AA-004".into(),
            location: format!("{op_location}/message"),
            message: "publish без messageId — потребитель не может дедуплицировать событие".into(),
        });
    }
}

/// Стабильный id события в 2.x: поле `messageId` (непустая строка).
fn has_message_id_v2(message: &Value) -> bool {
    message
        .get("messageId")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.trim().is_empty())
}

/// Правила операций 3.x: AA-002/AA-003/AA-004 на `action` send/receive.
fn lint_operations_v3(doc: &Value, out: &mut Vec<Finding>) {
    let Some(operations) = doc.get("operations").and_then(Value::as_object) else {
        return;
    };
    for (name, operation) in operations {
        if operation.get("$ref").is_some() || !operation.is_object() {
            continue;
        }
        lint_operation_v3(name, operation, out);
    }
}

/// Правила одной операции 3.x.
fn lint_operation_v3(name: &str, operation: &Value, out: &mut Vec<Finding>) {
    let op_location = location(&["operations", name]);

    // AA-002: операция без messages — контракт события не задан.
    let Some(messages) = operation.get("messages").and_then(Value::as_array) else {
        out.push(Finding {
            severity: "warn".into(),
            rule: "AA-002".into(),
            location: op_location,
            message: "операция без messages — контракт события не задан".into(),
        });
        return;
    };
    if messages.is_empty() {
        out.push(Finding {
            severity: "warn".into(),
            rule: "AA-002".into(),
            location: op_location,
            message: "операция без messages — контракт события не задан".into(),
        });
        return;
    }

    // `action: send` — операция владельца канала (аналог publish в 2.x).
    let is_send = operation.get("action").and_then(Value::as_str) == Some("send");
    for (i, message) in messages.iter().enumerate() {
        // message за `$ref` не разрешается (Deferred) — считается не заданным.
        if message.get("$ref").is_some() || !message.is_object() {
            continue;
        }
        let message_location = format!("{op_location}/messages/{i}");

        // AA-003: message без payload-схемы — нечего валидировать на границе.
        if message.get("payload").is_none() {
            out.push(Finding {
                severity: "warn".into(),
                rule: "AA-003".into(),
                location: message_location.clone(),
                message: "message без payload-схемы — нечего валидировать на границе".into(),
            });
        }

        // AA-004: send-операция без стабильного id события (x-message-id/key) —
        // потребитель не может дедуплицировать событие (аналог OA-003).
        if is_send && !has_message_id_v3(message) {
            out.push(Finding {
                severity: "error".into(),
                rule: "AA-004".into(),
                location: message_location,
                message: "send без x-message-id/key — потребитель не может дедуплицировать событие"
                    .into(),
            });
        }
    }
}

/// Стабильный id события в 3.x: расширение `x-message-id` или поле `key`
/// (непустая строка).
fn has_message_id_v3(message: &Value) -> bool {
    ["x-message-id", "key"].iter().any(|key| {
        message
            .get(*key)
            .and_then(Value::as_str)
            .is_some_and(|s| !s.trim().is_empty())
    })
}

/// JSON Pointer-экранирование сегмента: `~` → `~0`, `/` → `~1`.
fn escape_segment(segment: &str) -> String {
    segment.replace('~', "~0").replace('/', "~1")
}

/// Собирает JSON Pointer с якорем `#` из сегментов.
fn location(segments: &[&str]) -> String {
    let escaped: Vec<String> = segments.iter().map(|s| escape_segment(s)).collect();
    format!("#/{}", escaped.join("/"))
}

/// Инструменты модуля: `asyncapi_lint`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(AsyncapiLintTool)]
}

/// Инструмент `asyncapi_lint`: проверка контракта `AsyncAPI` 2.x/3.x правилами
/// скелета AA-001..AA-005 (транш T1 контрактного контура, ADR-015).
pub struct AsyncapiLintTool;

#[derive(Debug, Deserialize)]
struct AsyncapiLintArgs {
    /// Путь к файлу контракта `AsyncAPI` (yaml/yml/json).
    path: String,
}

#[async_trait]
impl Tool for AsyncapiLintTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "asyncapi_lint".into(),
            description: "Проверить контракт AsyncAPI 2.x/3.x: версионирование, схемы сообщений, \
                          идемпотентность событий (транш T1, ADR-015)"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу контракта AsyncAPI (yaml/yml/json)"
                    }
                },
                "required": ["path"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: AsyncapiLintArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "asyncapi_lint: невалидные аргументы: {e}"
                )));
            }
        };
        let path = ctx.resolve(&args.path);
        let findings = match lint_asyncapi(&path) {
            Ok(f) => f,
            Err(e) => return Ok(ToolOutput::err(format!("asyncapi_lint: {e}"))),
        };
        Ok(ToolOutput::ok(render_report(&findings)))
    }
}

/// Собирает отчёт в стиле `spine_lint`: сводка, строки находок, итог.
fn render_report(findings: &[Finding]) -> String {
    let errors = findings.iter().filter(|f| f.severity == "error").count();
    let warns = findings.len() - errors;
    let mut report = format!(
        "asyncapi: {} находок (error: {errors}, warn: {warns})",
        findings.len()
    );
    // Запись в String не может завершиться ошибкой — игнор безопасен.
    for f in findings {
        let _ = writeln!(
            report,
            "[{}] {} {} — {}",
            f.severity, f.location, f.rule, f.message
        );
    }
    // Запись в String не может завершиться ошибкой — игнор безопасен.
    let _ = writeln!(
        report,
        "Итог: {}",
        if errors == 0 { "PASS" } else { "FAIL" }
    );
    report
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use serde_json::json;

    use super::*;
    use crate::tool::ToolContext;

    /// Валидный контракт 2.x: все правила скелета соблюдены.
    const VALID_2X: &str = r"asyncapi: 2.6.0
info:
  title: Payment Events
  version: 1.0.0
servers:
  production:
    url: broker.example.com
    protocol: kafka
channels:
  paymentCreated:
    publish:
      message:
        messageId: PaymentCreated
        payload:
          type: object
    subscribe:
      message:
        messageId: PaymentCreated
        payload:
          type: object
";

    /// Блок publish-операции в [`VALID_2X`] (мутируется в тестах AA-002..AA-004).
    const PUBLISH_BLOCK: &str = "    publish:\n      message:\n        messageId: PaymentCreated\n        payload:\n          type: object";

    /// Валидный контракт 3.x: все правила скелета соблюдены.
    const VALID_3X: &str = r"asyncapi: 3.0.0
info:
  title: Payment Events
  version: 1.0.0
servers:
  production:
    host: broker.example.com
    protocol: kafka
channels:
  paymentCreated:
    address: payment/created
operations:
  sendPaymentCreated:
    action: send
    channel:
      $ref: '#/channels/paymentCreated'
    messages:
      - x-message-id: payment-created
        payload:
          type: object
  receivePaymentCreated:
    action: receive
    channel:
      $ref: '#/channels/paymentCreated'
    messages:
      - payload:
          type: object
";

    /// Запускает инструмент на тексте контракта, записанном во временный файл.
    async fn lint_text(content: &str, file_name: &str) -> ToolOutput {
        let dir = tempfile::tempdir().expect("tmp");
        std::fs::write(dir.path().join(file_name), content).expect("запись контракта");
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        tools()[0]
            .call(json!({"path": file_name}), &ctx)
            .await
            .expect("вызов asyncapi_lint")
    }

    #[test]
    fn factory_exposes_single_asyncapi_lint_tool() {
        let ts = tools();
        assert_eq!(ts.len(), 1);
        let spec = ts[0].spec();
        assert_eq!(spec.name, "asyncapi_lint");
        assert!(
            spec.description.contains("AsyncAPI 2.x/3.x"),
            "{}",
            spec.description
        );
        assert!(
            spec.description.contains("идемпотентность"),
            "{}",
            spec.description
        );
        assert_eq!(spec.parameters["required"], json!(["path"]));
    }

    #[tokio::test]
    async fn valid_2x_contract_passes_clean() {
        let out = lint_text(VALID_2X, "events-2x.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("asyncapi: 0 находок (error: 0, warn: 0)"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
        assert!(!out.content.contains("AA-00"), "{}", out.content);
    }

    #[tokio::test]
    async fn valid_3x_contract_passes_clean() {
        let out = lint_text(VALID_3X, "events-3x.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("asyncapi: 0 находок (error: 0, warn: 0)"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
        assert!(!out.content.contains("AA-00"), "{}", out.content);
    }

    #[tokio::test]
    async fn aa001_missing_info_version_is_error() {
        let contract = VALID_2X.replace("  version: 1.0.0\n", "");
        let out = lint_text(&contract, "no-version.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/info AA-001 — info.version отсутствует"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn aa001_non_semver_version_is_error() {
        for bad in ["1.0", "v1.0.0", "1.0.0-rc1", "1.0.x", "abc"] {
            let contract = VALID_2X.replace("version: 1.0.0", &format!("version: \"{bad}\""));
            let out = lint_text(&contract, "bad-version.yaml").await;
            assert!(!out.is_error, "{bad}: {}", out.content);
            assert!(
                out.content.contains("[error] #/info/version AA-001"),
                "{bad}: {}",
                out.content
            );
            assert!(out.content.contains("Итог: FAIL"), "{bad}: {}", out.content);
        }
    }

    #[tokio::test]
    async fn aa002_publish_without_message_warns() {
        let contract = VALID_2X.replace(
            PUBLISH_BLOCK,
            "    publish:\n      description: sends payment created",
        );
        let out = lint_text(&contract, "publish-no-message.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[warn] #/channels/paymentCreated/publish AA-002"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn aa003_message_without_payload_warns() {
        let contract = VALID_2X.replace(
            PUBLISH_BLOCK,
            "    publish:\n      message:\n        messageId: PaymentCreated",
        );
        let out = lint_text(&contract, "message-no-payload.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[warn] #/channels/paymentCreated/publish/message AA-003"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn aa004_publish_without_message_id_is_error() {
        let contract = VALID_2X.replace(
            PUBLISH_BLOCK,
            "    publish:\n      message:\n        payload:\n          type: object",
        );
        let out = lint_text(&contract, "publish-no-id.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/channels/paymentCreated/publish/message AA-004"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn aa004_3x_send_without_message_id_is_error() {
        // В 3.x publisher — операция `action: send`; id события ищем в
        // `x-message-id`/`key` (Deferred: `$ref`-сообщения не обходятся).
        let contract = VALID_3X.replace(
            "      - x-message-id: payment-created\n        payload:\n          type: object",
            "      - payload:\n          type: object",
        );
        let out = lint_text(&contract, "send-no-id.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/operations/sendPaymentCreated/messages/0 AA-004"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn aa005_missing_servers_warns() {
        let contract = VALID_2X.replace(
            "servers:\n  production:\n    url: broker.example.com\n    protocol: kafka\n",
            "",
        );
        let out = lint_text(&contract, "no-servers.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content.contains("[warn] #/servers AA-005"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn json_format_is_accepted_on_par_with_yaml() {
        let contract = r#"{
  "asyncapi": "2.6.0",
  "info": {"title": "T", "version": "2.1.0"},
  "servers": {"production": {"url": "broker.example.com", "protocol": "kafka"}},
  "channels": {
    "paymentCreated": {
      "publish": {"message": {"payload": {"type": "object"}}}
    }
  }
}"#;
        let out = lint_text(contract, "contract.json").await;
        assert!(!out.is_error, "{}", out.content);
        // Правила работают по Value-документу независимо от исходного формата.
        assert!(
            out.content
                .contains("[error] #/channels/paymentCreated/publish/message AA-004"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn not_asyncapi_file_errors() {
        let out = lint_text("title: hello\nversion: 1.0.0\n", "not-asyncapi.yaml").await;
        assert!(out.is_error, "{}", out.content);
        assert!(
            out.content.contains("не AsyncAPI 2.x/3.x"),
            "{}",
            out.content
        );
    }

    #[tokio::test]
    async fn broken_json_and_yaml_errors() {
        let out = lint_text("{{{", "broken.json").await;
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("невалидный JSON"), "{}", out.content);

        let out = lint_text("asyncapi: [2.6.0\n", "broken.yaml").await;
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("невалидный YAML"), "{}", out.content);
    }

    #[tokio::test]
    async fn missing_file_errors() {
        let dir = tempfile::tempdir().expect("tmp");
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let out = tools()[0]
            .call(json!({"path": "absent.yaml"}), &ctx)
            .await
            .expect("вызов asyncapi_lint");
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("asyncapi_lint"), "{}", out.content);
    }
}
