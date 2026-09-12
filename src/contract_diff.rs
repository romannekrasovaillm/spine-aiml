//! Сравнение двух версий контракта `OpenAPI` 3.x — агентный инструмент `contract_diff`.
//!
//! Третий инструмент контрактного контура (транш T1, ADR-015): сравнение двух
//! версий контракта `OpenAPI` 3.x на breaking changes. Правила CD-001..CD-006 —
//! удалённые пути (CD-001), операции (CD-002), обязательные параметры и
//! параметры, ставшие required (CD-003), коды ответов (CD-004) — breaking
//! (error); добавленные пути/операции/необязательные параметры/коды ответов
//! (CD-005) — non-breaking (warn, информирование); смена типа поля схемы
//! (CD-006) — breaking (error). Вывод — в стиле `spine_lint`: сводка, строки
//! `[severity] локация rule — message`, «Итог: PASS/FAIL» (PASS при отсутствии
//! breaking-изменений).
//!
//! Парсинг — без новых зависимостей: `serde_json` (JSON) и `serde_yaml_ng`
//! (YAML) уже в Cargo.toml; документ представляется как `serde_json::Value`
//! (формат определяется по первому непробельному символу: `{` — JSON, иначе
//! YAML), навигация — по JSON Pointer. Generic-инструмент MIT-ядра:
//! доменной зоны не касается.
//!
//! Известные ограничения скелета (Deferred — резолюция `$ref` и глубокая
//! рекурсия следующей итерацией T1): CD-006 сравнивает только прямое поле
//! `type` у `components.schemas.*.properties.*`; обязательность параметра — по
//! полю `required` (неявная обязательность path-параметров не учитывается);
//! удаление необязательного параметра и добавление обязательного параметра
//! вне CD-001..CD-006 и не флагаются. Локализация находок — JSON Pointer,
//! параметры адресуются по имени (а не по индексу массива).

use std::collections::HashMap;
use std::fmt::Write as _;
use std::path::Path;
use std::sync::Arc;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Находка диффа контракта.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    /// Критичность: `error` (breaking) | `warn` (non-breaking).
    pub severity: String,
    /// Код правила (`CD-001`..`CD-006`).
    pub rule: String,
    /// JSON Pointer (`#/paths/~1v1~1pets/post`).
    pub location: String,
    /// Сообщение.
    pub message: String,
}

/// Методы операций `OpenAPI` (остальные ключи path item — служебные).
const OPERATION_METHODS: [&str; 8] = [
    "get", "put", "post", "delete", "options", "head", "patch", "trace",
];

/// Сравнивает два контракта `OpenAPI` 3.x на breaking changes.
///
/// Читает оба файла, определяет формат (JSON, если первый непробельный символ —
/// `{`, иначе YAML), парсит в [`Value`] и проверяет признак `OpenAPI` 3.x.
/// Находки — данные отчёта, не ошибки выполнения: диф отработал, если оба
/// документа прочитаны и распознаны.
///
/// # Errors
/// Файл не читается, JSON/YAML не парсится, документ не `OpenAPI` 3.x.
pub fn diff_contracts(old: &Path, new: &Path) -> Result<Vec<Finding>> {
    let old_doc = read_contract(old)?;
    let new_doc = read_contract(new)?;
    Ok(diff_documents(&old_doc, &new_doc))
}

/// Читает и распознаёт один контракт `OpenAPI` 3.x.
///
/// # Errors
/// Файл не читается, JSON/YAML не парсится, документ не `OpenAPI` 3.x.
fn read_contract(path: &Path) -> Result<Value> {
    let content = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let doc = parse_contract(&content, path)?;
    if !is_openapi3(&doc) {
        return Err(HarnessError::Tool(format!(
            "{}: не OpenAPI 3.x: ожидается поле openapi вида «3.x.y»",
            path.display()
        )));
    }
    Ok(doc)
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

/// Признак `OpenAPI` 3.x: поле `openapi` со значением вида `3.x.y`.
fn is_openapi3(doc: &Value) -> bool {
    match doc.get("openapi").and_then(Value::as_str) {
        Some(version) => {
            let parts: Vec<&str> = version.split('.').collect();
            parts.len() >= 2
                && parts[0] == "3"
                && parts[1..]
                    .iter()
                    .all(|p| !p.is_empty() && p.chars().all(|c| c.is_ascii_digit()))
        }
        None => false,
    }
}

/// Прогоняет все правила по разобранным документам.
fn diff_documents(old: &Value, new: &Value) -> Vec<Finding> {
    let mut findings = Vec::new();
    diff_paths(old, new, &mut findings);
    diff_schemas(old, new, &mut findings);
    findings
}

/// Правила по `paths`: CD-001/CD-002/CD-003/CD-004/CD-005.
fn diff_paths(old: &Value, new: &Value, out: &mut Vec<Finding>) {
    let old_paths = old.get("paths").and_then(Value::as_object);
    let new_paths = new.get("paths").and_then(Value::as_object);

    // CD-001 (error): удалённый путь; операции/параметры/ответы — общий путь.
    if let Some(old_paths) = old_paths {
        for (path, old_item) in old_paths {
            let path = path.as_str();
            match new_paths.and_then(|np| np.get(path)) {
                None => out.push(Finding {
                    severity: "error".into(),
                    rule: "CD-001".into(),
                    location: location(&["paths", path]),
                    message: format!("удалён путь «{path}»"),
                }),
                Some(new_item) => diff_path_item(path, old_item, new_item, out),
            }
        }
    }

    // CD-005 (warn): добавленный путь.
    if let Some(new_paths) = new_paths {
        for (path, _) in new_paths {
            let path = path.as_str();
            if !old_paths.is_some_and(|op| op.contains_key(path)) {
                out.push(Finding {
                    severity: "warn".into(),
                    rule: "CD-005".into(),
                    location: location(&["paths", path]),
                    message: format!("добавлен путь «{path}»"),
                });
            }
        }
    }
}

/// Правила одного path item: CD-002 (операции) и CD-003/CD-004 (общие операции).
fn diff_path_item(path: &str, old: &Value, new: &Value, out: &mut Vec<Finding>) {
    for method in OPERATION_METHODS {
        let old_op = old.get(method).filter(|v| v.is_object());
        let new_op = new.get(method).filter(|v| v.is_object());
        match (old_op, new_op) {
            // CD-002 (error): удалённая операция.
            (Some(_), None) => out.push(Finding {
                severity: "error".into(),
                rule: "CD-002".into(),
                location: location(&["paths", path, method]),
                message: format!("удалена операция {method}"),
            }),
            // CD-005 (warn): добавленная операция.
            (None, Some(_)) => out.push(Finding {
                severity: "warn".into(),
                rule: "CD-005".into(),
                location: location(&["paths", path, method]),
                message: format!("добавлена операция {method}"),
            }),
            (Some(old_op), Some(new_op)) => {
                diff_operation(path, method, old, new, old_op, new_op, out);
            }
            (None, None) => {}
        }
    }
}

/// Правила общей операции: CD-003 (параметры) и CD-004 (ответы).
fn diff_operation(
    path: &str,
    method: &str,
    old_item: &Value,
    new_item: &Value,
    old_op: &Value,
    new_op: &Value,
    out: &mut Vec<Finding>,
) {
    diff_parameters(path, method, old_item, new_item, old_op, new_op, out);
    diff_responses(path, method, old_op, new_op, out);
}

/// Эффективный набор параметров операции: `path_item.parameters` +
/// `operation.parameters` (операция перекрывает path item по ключу `name`+`in`).
/// Параметр за `$ref` не резолвится (Deferred) — пропускается.
fn effective_parameters(path_item: &Value, op: &Value) -> HashMap<(String, String), Value> {
    let mut map = HashMap::new();
    for level in [path_item.get("parameters"), op.get("parameters")]
        .into_iter()
        .flatten()
    {
        let Some(params) = level.as_array() else {
            continue;
        };
        for param in params {
            if param.get("$ref").is_some() || !param.is_object() {
                continue;
            }
            let (Some(name), Some(in_)) = (
                param.get("name").and_then(Value::as_str),
                param.get("in").and_then(Value::as_str),
            ) else {
                continue;
            };
            map.insert((name.to_string(), in_.to_string()), param.clone());
        }
    }
    map
}

/// Обязательность параметра — по полю `required`.
fn is_required(param: &Value) -> bool {
    param
        .get("required")
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

/// CD-003/CD-005: удаление обязательного параметра, добавление необязательного,
/// переход необязательного в required.
fn diff_parameters(
    path: &str,
    method: &str,
    old_item: &Value,
    new_item: &Value,
    old_op: &Value,
    new_op: &Value,
    out: &mut Vec<Finding>,
) {
    let op_location = location(&["paths", path, method]);
    let old_params = effective_parameters(old_item, old_op);
    let new_params = effective_parameters(new_item, new_op);

    // CD-003 (error): удалён обязательный параметр.
    for ((name, in_), param) in &old_params {
        if !new_params.contains_key(&(name.clone(), in_.clone())) && is_required(param) {
            out.push(Finding {
                severity: "error".into(),
                rule: "CD-003".into(),
                location: format!("{op_location}/parameters/{}", escape_segment(name)),
                message: format!("удалён обязательный параметр «{name}» (in: {in_})"),
            });
        }
    }

    // CD-005 (warn): добавлен необязательный параметр (обязательный — вне скелета).
    for ((name, in_), param) in &new_params {
        if !old_params.contains_key(&(name.clone(), in_.clone())) && !is_required(param) {
            out.push(Finding {
                severity: "warn".into(),
                rule: "CD-005".into(),
                location: format!("{op_location}/parameters/{}", escape_segment(name)),
                message: format!("добавлен необязательный параметр «{name}» (in: {in_})"),
            });
        }
    }

    // CD-003 (error): параметр стал required (был необязательным).
    for ((name, in_), old_param) in &old_params {
        let became_required = new_params
            .get(&(name.clone(), in_.clone()))
            .is_some_and(|new_param| !is_required(old_param) && is_required(new_param));
        if became_required {
            out.push(Finding {
                severity: "error".into(),
                rule: "CD-003".into(),
                location: format!("{op_location}/parameters/{}", escape_segment(name)),
                message: format!("параметр «{name}» стал required (in: {in_})"),
            });
        }
    }
}

/// CD-004/CD-005: удалённый/добавленный код ответа.
fn diff_responses(
    path: &str,
    method: &str,
    old_op: &Value,
    new_op: &Value,
    out: &mut Vec<Finding>,
) {
    let op_location = location(&["paths", path, method]);
    let old_responses = old_op.get("responses").and_then(Value::as_object);
    let new_responses = new_op.get("responses").and_then(Value::as_object);

    // CD-004 (error): удалённый код ответа — потребитель мог на него опираться.
    if let Some(old_responses) = old_responses {
        for (code, _) in old_responses {
            if !new_responses.is_some_and(|nr| nr.contains_key(code)) {
                out.push(Finding {
                    severity: "error".into(),
                    rule: "CD-004".into(),
                    location: format!("{op_location}/responses/{}", escape_segment(code)),
                    message: format!("удалён код ответа {code}"),
                });
            }
        }
    }

    // CD-005 (warn): добавленный код ответа.
    if let Some(new_responses) = new_responses {
        for (code, _) in new_responses {
            if !old_responses.is_some_and(|or| or.contains_key(code)) {
                out.push(Finding {
                    severity: "warn".into(),
                    rule: "CD-005".into(),
                    location: format!("{op_location}/responses/{}", escape_segment(code)),
                    message: format!("добавлен код ответа {code}"),
                });
            }
        }
    }
}

/// CD-006: изменение типа поля схемы (поверхностно, без `$ref`-резолюции).
fn diff_schemas(old: &Value, new: &Value, out: &mut Vec<Finding>) {
    let old_schemas = old
        .get("components")
        .and_then(|c| c.get("schemas"))
        .and_then(Value::as_object);
    let new_schemas = new
        .get("components")
        .and_then(|c| c.get("schemas"))
        .and_then(Value::as_object);
    let (Some(old_schemas), Some(new_schemas)) = (old_schemas, new_schemas) else {
        return;
    };
    for (name, old_schema) in old_schemas {
        let Some(new_schema) = new_schemas.get(name) else {
            continue;
        };
        diff_schema_properties(name, old_schema, new_schema, out);
    }
}

/// Сравнивает типы верхнеуровневых `properties` одной схемы.
fn diff_schema_properties(
    name: &str,
    old_schema: &Value,
    new_schema: &Value,
    out: &mut Vec<Finding>,
) {
    let old_props = old_schema.get("properties").and_then(Value::as_object);
    let new_props = new_schema.get("properties").and_then(Value::as_object);
    let (Some(old_props), Some(new_props)) = (old_props, new_props) else {
        return;
    };
    for (prop, old_prop) in old_props {
        let prop = prop.as_str();
        let Some(new_prop) = new_props.get(prop) else {
            continue;
        };
        // Поверхностно: только прямое поле type, без $ref-резолюции и рекурсии.
        let old_type = old_prop.get("type").and_then(Value::as_str);
        let new_type = new_prop.get("type").and_then(Value::as_str);
        match (old_type, new_type) {
            (Some(old_type), Some(new_type)) if old_type != new_type => {
                out.push(Finding {
                    severity: "error".into(),
                    rule: "CD-006".into(),
                    location: location(&["components", "schemas", name, "properties", prop]),
                    message: format!(
                        "изменился тип поля «{prop}» схемы «{name}»: {old_type} → {new_type}"
                    ),
                });
            }
            _ => {}
        }
    }
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

/// Инструменты модуля: `contract_diff`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(ContractDiffTool)]
}

/// Инструмент `contract_diff`: сравнение двух версий контракта `OpenAPI` 3.x
/// на breaking changes правилами CD-001..CD-006 (транш T1 контрактного контура,
/// ADR-015).
pub struct ContractDiffTool;

#[derive(Debug, Deserialize)]
struct ContractDiffArgs {
    /// Путь к старой версии контракта `OpenAPI` (yaml/yml/json).
    old: String,
    /// Путь к новой версии контракта `OpenAPI` (yaml/yml/json).
    new: String,
}

#[async_trait]
impl Tool for ContractDiffTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "contract_diff".into(),
            description: "Сравнить две версии контракта OpenAPI 3.x: breaking changes (удалённые \
                          пути/операции/параметры/ответы, смена типов) — третий инструмент T1 \
                          (ADR-015)"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "old": {
                        "type": "string",
                        "description": "Путь к старой версии контракта OpenAPI (yaml/yml/json)"
                    },
                    "new": {
                        "type": "string",
                        "description": "Путь к новой версии контракта OpenAPI (yaml/yml/json)"
                    }
                },
                "required": ["old", "new"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: ContractDiffArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "contract_diff: невалидные аргументы: {e}"
                )));
            }
        };
        let old = ctx.resolve(&args.old);
        let new = ctx.resolve(&args.new);
        let findings = match diff_contracts(&old, &new) {
            Ok(f) => f,
            Err(e) => return Ok(ToolOutput::err(format!("contract_diff: {e}"))),
        };
        Ok(ToolOutput::ok(render_report(&findings)))
    }
}

/// Собирает отчёт в стиле `spine_lint`: сводка, строки находок, итог.
fn render_report(findings: &[Finding]) -> String {
    let breaking = findings.iter().filter(|f| f.severity == "error").count();
    let non_breaking = findings.len() - breaking;
    let mut report = format!(
        "contract_diff: {} изменений (breaking: {breaking}, non-breaking: {non_breaking})",
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
        if breaking == 0 { "PASS" } else { "FAIL" }
    );
    report
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use serde_json::json;

    use super::*;
    use crate::tool::ToolContext;

    /// Контракт-эталон: один путь с двумя операциями, параметрами, ответами и схемой.
    const BASE: &str = r"openapi: 3.0.3
info:
  title: Pet Store API
  version: 1.0.0
paths:
  /v1/pets:
    get:
      operationId: listPets
      parameters:
        - name: limit
          in: query
          required: false
          schema:
            type: integer
      responses:
        '200':
          description: ok
        '404':
          description: not found
    post:
      operationId: createPet
      parameters:
        - name: idempotency-key
          in: header
          required: true
          schema:
            type: string
      responses:
        '201':
          description: created
components:
  schemas:
    Pet:
      type: object
      properties:
        name:
          type: string
        age:
          type: integer
";

    /// Блок path item `/v1/pets` в [`BASE`] (удаляется в тестах CD-001/CD-005).
    const PETS_PATH: &str = "  /v1/pets:\n    get:\n      operationId: listPets\n      parameters:\n        - name: limit\n          in: query\n          required: false\n          schema:\n            type: integer\n      responses:\n        '200':\n          description: ok\n        '404':\n          description: not found\n    post:\n      operationId: createPet\n      parameters:\n        - name: idempotency-key\n          in: header\n          required: true\n          schema:\n            type: string\n      responses:\n        '201':\n          description: created\n";

    /// Блок post-операции в [`BASE`] (удаляется в тестах CD-002/CD-005).
    const POST_BLOCK: &str = "    post:\n      operationId: createPet\n      parameters:\n        - name: idempotency-key\n          in: header\n          required: true\n          schema:\n            type: string\n      responses:\n        '201':\n          description: created\n";

    /// Обязательный параметр post-операции в [`BASE`] (удаляется в тесте CD-003).
    const REQUIRED_PARAM: &str = "        - name: idempotency-key\n          in: header\n          required: true\n          schema:\n            type: string\n";

    /// Необязательный параметр get-операции в [`BASE`] (удаляется в тесте CD-005).
    const LIMIT_PARAM: &str = "        - name: limit\n          in: query\n          required: false\n          schema:\n            type: integer\n";

    /// Ответ 404 get-операции в [`BASE`] (удаляется в тестах CD-004/CD-005).
    const NOT_FOUND_RESPONSE: &str = "        '404':\n          description: not found\n";

    /// Запускает инструмент на паре контрактов, записанных во временные файлы.
    async fn diff_text(old: &str, new: &str, old_name: &str, new_name: &str) -> ToolOutput {
        let dir = tempfile::tempdir().expect("tmp");
        std::fs::write(dir.path().join(old_name), old).expect("запись старого контракта");
        std::fs::write(dir.path().join(new_name), new).expect("запись нового контракта");
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        tools()[0]
            .call(json!({"old": old_name, "new": new_name}), &ctx)
            .await
            .expect("вызов contract_diff")
    }

    #[test]
    fn factory_exposes_single_contract_diff_tool() {
        let ts = tools();
        assert_eq!(ts.len(), 1);
        let spec = ts[0].spec();
        assert_eq!(spec.name, "contract_diff");
        assert!(
            spec.description.contains("OpenAPI 3.x"),
            "{}",
            spec.description
        );
        assert!(
            spec.description.contains("breaking"),
            "{}",
            spec.description
        );
        assert_eq!(spec.parameters["required"], json!(["old", "new"]));
    }

    #[tokio::test]
    async fn identical_contracts_pass_clean() {
        let out = diff_text(BASE, BASE, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("contract_diff: 0 изменений (breaking: 0, non-breaking: 0)"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
        assert!(!out.content.contains("CD-00"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd001_removed_path_is_breaking() {
        let new = BASE.replace(PETS_PATH, "");
        let out = diff_text(BASE, &new, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content.contains("[error] #/paths/~1v1~1pets CD-001"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd005_added_path_is_warn_and_pass() {
        let old = BASE.replace(PETS_PATH, "");
        let out = diff_text(&old, BASE, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content.contains("[warn] #/paths/~1v1~1pets CD-005"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd002_removed_operation_is_breaking() {
        let new = BASE.replace(POST_BLOCK, "");
        let out = diff_text(BASE, &new, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/post CD-002"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd005_added_operation_is_warn_and_pass() {
        let old = BASE.replace(POST_BLOCK, "");
        let out = diff_text(&old, BASE, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[warn] #/paths/~1v1~1pets/post CD-005"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd003_removed_required_parameter_is_breaking() {
        let new = BASE.replace(REQUIRED_PARAM, "");
        let out = diff_text(BASE, &new, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/post/parameters/idempotency-key CD-003"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd003_parameter_became_required_is_breaking() {
        let new = BASE.replace("required: false", "required: true");
        let out = diff_text(BASE, &new, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/get/parameters/limit CD-003"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd005_added_optional_parameter_is_warn_and_pass() {
        let old = BASE.replace(LIMIT_PARAM, "");
        let out = diff_text(&old, BASE, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[warn] #/paths/~1v1~1pets/get/parameters/limit CD-005"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn removed_optional_parameter_is_not_flagged() {
        // Удаление необязательного параметра — вне скелета CD-001..CD-006.
        let new = BASE.replace(LIMIT_PARAM, "");
        let out = diff_text(BASE, &new, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(!out.content.contains("CD-003"), "{}", out.content);
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd004_removed_response_code_is_breaking() {
        let new = BASE.replace(NOT_FOUND_RESPONSE, "");
        let out = diff_text(BASE, &new, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/get/responses/404 CD-004"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd005_added_response_code_is_warn_and_pass() {
        let old = BASE.replace(NOT_FOUND_RESPONSE, "");
        let out = diff_text(&old, BASE, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[warn] #/paths/~1v1~1pets/get/responses/404 CD-005"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn cd006_changed_schema_type_is_breaking() {
        let new = BASE.replace(
            "        age:\n          type: integer",
            "        age:\n          type: string",
        );
        let out = diff_text(BASE, &new, "old.yaml", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/components/schemas/Pet/properties/age CD-006"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn mixed_json_and_yaml_are_compared() {
        let old = r#"{
  "openapi": "3.0.3",
  "info": {"title": "T", "version": "1.0.0"},
  "paths": {
    "/v1/pets": {
      "get": {
        "operationId": "listPets",
        "responses": {
          "200": {"description": "ok"},
          "404": {"description": "not found"}
        }
      }
    }
  }
}"#;
        let new = r"openapi: 3.0.3
info:
  title: T
  version: 1.0.0
paths:
  /v1/pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
";
        let out = diff_text(old, new, "old.json", "new.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/get/responses/404 CD-004"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn non_openapi_file_errors() {
        let out = diff_text(
            "title: hello\nversion: 1.0.0\n",
            BASE,
            "old.yaml",
            "new.yaml",
        )
        .await;
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("не OpenAPI 3.x"), "{}", out.content);
    }

    #[tokio::test]
    async fn missing_file_errors() {
        let dir = tempfile::tempdir().expect("tmp");
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let out = tools()[0]
            .call(
                json!({"old": "absent.yaml", "new": "also-absent.yaml"}),
                &ctx,
            )
            .await
            .expect("вызов contract_diff");
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("contract_diff"), "{}", out.content);
    }
}
