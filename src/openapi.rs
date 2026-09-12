//! Линтер контрактов `OpenAPI` 3.x — агентный инструмент `openapi_lint`.
//!
//! Walking skeleton контрактного контура (транш T1, ADR-015): пять правил
//! скелета OA-001..OA-005 — версионирование контракта (`info.version`) и
//! путей (`/v<число>/`), идемпотентность mutating-операций
//! (header `Idempotency-Key`), ошибки RFC 7807 (`application/problem+json`),
//! `operationId`. Вывод находок — в стиле `spine_lint`: сводка, строки
//! `[severity] локация rule — message`, «Итог: PASS/FAIL».
//!
//! Парсинг — без новых зависимостей: `serde_json` (JSON) и `serde_yaml_ng`
//! (YAML) уже в Cargo.toml; документ представляется как `serde_json::Value`
//! (формат определяется по первому непробельному символу: `{` — JSON, иначе
//! YAML), навигация — по JSON Pointer. Generic-инструмент MIT-ядра (AD-BE1):
//! банковской зоны не касается.
//!
//! Резолюция `$ref` (закрытый Deferred скелета): локальные ссылки вида
//! `#/components/...` разрешаются по месту при чтении параметров и ответов —
//! цели components-карт parameters/responses/schemas/headers; узел за ссылкой
//! проверяется по цели (возвращается ссылка на узел-цель того же Value-дерева,
//! документ не модифицируется и не материализуется заново). Внешние ссылки
//! (`http://`, `https://`, `file://`, относительные пути) и прочие локальные
//! цели не разрешаются: линтер не ходит в сеть и на диск, узел за такой
//! ссылкой трактуется как отсутствующий. Цепочки `$ref` проходятся до глубины
//! [`MAX_REF_DEPTH`] (32); циклы и переполнение обрываются срезом по глубине —
//! ссылка считается неразрешённой, без паники. Локализация находок — JSON
//! Pointer (`#/paths/~1pets/post`), а не номер строки (Value-парсинг исходные
//! строки не сохраняет).

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
    /// Код правила (`OA-001`..`OA-005`).
    pub rule: String,
    /// JSON Pointer (`#/paths/~1pets/post`).
    pub location: String,
    /// Сообщение.
    pub message: String,
}

/// Методы операций `OpenAPI` (остальные ключи path item — служебные).
const OPERATION_METHODS: [&str; 8] = [
    "get", "put", "post", "delete", "options", "head", "patch", "trace",
];

/// Прогоняет скелет-правила OA-001..OA-005 по контракту `OpenAPI` 3.x.
///
/// Читает файл, определяет формат (JSON, если первый непробельный символ —
/// `{`, иначе YAML), парсит в [`Value`] и проверяет признак `OpenAPI` 3.x
/// (поле `openapi` вида `3.x.y`). Находки — данные отчёта, не ошибки
/// выполнения: линтер отработал, если документ прочитан и распознан.
///
/// # Errors
/// Файл не читается, JSON/YAML не парсится, документ не `OpenAPI` 3.x.
pub fn lint_openapi(path: &Path) -> Result<Vec<Finding>> {
    let content = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let doc = parse_contract(&content, path)?;
    if !is_openapi3(&doc) {
        return Err(HarnessError::Tool(format!(
            "{}: не OpenAPI 3.x: ожидается поле openapi вида «3.x.y»",
            path.display()
        )));
    }
    Ok(lint_document(&doc))
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

/// Прогоняет все правила по разобранному документу.
fn lint_document(doc: &Value) -> Vec<Finding> {
    let mut findings = Vec::new();
    lint_info(doc, &mut findings);
    if let Some(paths) = doc.get("paths").and_then(Value::as_object) {
        for (path, item) in paths {
            lint_path(doc, path, item, &mut findings);
        }
    }
    findings
}

/// OA-001 (error): `info.version` отсутствует или не MAJOR.MINOR.PATCH.
fn lint_info(doc: &Value, out: &mut Vec<Finding>) {
    let Some(info) = doc.get("info") else {
        out.push(Finding {
            severity: "error".into(),
            rule: "OA-001".into(),
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
            rule: "OA-001".into(),
            location: "#/info".into(),
            message,
        });
        return;
    };
    if !is_semver(version) {
        out.push(Finding {
            severity: "error".into(),
            rule: "OA-001".into(),
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

/// Правила по одному path item: OA-002 + операции (OA-003..OA-005).
fn lint_path(doc: &Value, path: &str, item: &Value, out: &mut Vec<Finding>) {
    if !has_version_prefix(path) {
        out.push(Finding {
            severity: "warn".into(),
            rule: "OA-002".into(),
            location: location(&["paths", path]),
            message: "путь не начинается с версионного префикса /v<число>/ (например /v1/pets)"
                .into(),
        });
    }
    for method in OPERATION_METHODS {
        let Some(op) = item.get(method) else {
            continue;
        };
        if op.is_object() {
            lint_operation(doc, path, method, op, item, out);
        }
    }
}

/// Версионный префикс `/v<число>/`: после `/v` идут цифры, затем `/`.
fn has_version_prefix(path: &str) -> bool {
    let Some(rest) = path.strip_prefix("/v") else {
        return false;
    };
    let digits = rest.bytes().take_while(u8::is_ascii_digit).count();
    digits > 0 && rest.as_bytes().get(digits) == Some(&b'/')
}

/// Правила одной операции: OA-003 (идемпотентность), OA-004 (RFC 7807),
/// OA-005 (`operationId`).
fn lint_operation(
    doc: &Value,
    path: &str,
    method: &str,
    op: &Value,
    path_item: &Value,
    out: &mut Vec<Finding>,
) {
    let op_location = location(&["paths", path, method]);

    // OA-003: mutating-операция без header-параметра Idempotency-Key.
    if let Some(severity) = mutating_severity(method) {
        if !has_idempotency_key(doc, op, path_item) {
            out.push(Finding {
                severity: severity.into(),
                rule: "OA-003".into(),
                location: op_location.clone(),
                message: format!("{method} без header-параметра Idempotency-Key"),
            });
        }
    }

    // OA-004: ответы 4xx/5xx/default без content application/problem+json.
    if let Some(responses) = op.get("responses").and_then(Value::as_object) {
        for (code, response) in responses {
            if !is_error_response(code) {
                continue;
            }
            if !has_problem_json(doc, response) {
                out.push(Finding {
                    severity: "warn".into(),
                    rule: "OA-004".into(),
                    location: format!("{op_location}/responses/{}", escape_segment(code)),
                    message: format!(
                        "ответ {code} без content application/problem+json (RFC 7807)"
                    ),
                });
            }
        }
    }

    // OA-005: операция без operationId.
    let has_operation_id = op
        .get("operationId")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.trim().is_empty());
    if !has_operation_id {
        out.push(Finding {
            severity: "warn".into(),
            rule: "OA-005".into(),
            location: op_location,
            message: "операция без operationId".into(),
        });
    }
}

/// Критичность OA-003 по методу: `post` — error, `put`/`patch`/`delete` — warn.
fn mutating_severity(method: &str) -> Option<&'static str> {
    match method {
        "post" => Some("error"),
        "put" | "patch" | "delete" => Some("warn"),
        _ => None,
    }
}

/// Есть ли header-параметр `Idempotency-Key` (регистронезависимо) на уровне
/// операции или path item. Параметр за локальным `$ref` разрешается и
/// проверяется по цели; за внешней/битой/циклической ссылкой параметр
/// считается отсутствующим.
fn has_idempotency_key(doc: &Value, op: &Value, path_item: &Value) -> bool {
    for level in [op.get("parameters"), path_item.get("parameters")]
        .into_iter()
        .flatten()
    {
        let Some(params) = level.as_array() else {
            continue;
        };
        for parameter in params {
            // Узел за неразрешимой ссылкой неразличим — мимо (не ключ).
            let Some(parameter) = resolve_ref(doc, parameter, MAX_REF_DEPTH) else {
                continue;
            };
            let in_header = parameter.get("in").and_then(Value::as_str) == Some("header");
            let is_key = parameter
                .get("name")
                .and_then(Value::as_str)
                .is_some_and(|name| name.eq_ignore_ascii_case("idempotency-key"));
            if in_header && is_key {
                return true;
            }
        }
    }
    false
}

/// Код ответа ошибки: `default` или начинается с `4`/`5` (включая `4XX`/`5XX`).
fn is_error_response(code: &str) -> bool {
    code == "default" || code.starts_with('4') || code.starts_with('5')
}

/// Есть ли в ответе media type `application/problem+json` (RFC 7807).
/// Ответ за локальным `$ref` разрешается и проверяется по цели; за внешней/
/// битой/циклической ссылкой ответ считается без problem+json.
fn has_problem_json(doc: &Value, response: &Value) -> bool {
    let Some(response) = resolve_ref(doc, response, MAX_REF_DEPTH) else {
        return false;
    };
    response
        .get("content")
        .and_then(Value::as_object)
        .is_some_and(|content| {
            content
                .keys()
                .any(|media| media.starts_with("application/problem+json"))
        })
}

/// Максимальная глубина прохода цепочки локальных `$ref`; превышение (циклы
/// вида a→b→a или длинные цепочки) — ссылка считается неразрешённой, мягкая
/// деградация без паники.
const MAX_REF_DEPTH: usize = 32;

/// Резолвит узел за `$ref` для правил OA-003/OA-004 — выбор скелета.
///
/// Узел без `$ref` возвращается как есть; узел с `$ref`-строкой вида
/// `#/components/...` заменяется целью по JSON Pointer (components-карты
/// parameters/responses/schemas/headers), цепочки ссылок проходятся
/// рекурсивно до глубины [`MAX_REF_DEPTH`]. Резолюция по месту: возвращается
/// ссылка на узел-цель того же Value-дерева, документ не модифицируется.
///
/// Внешние ссылки (`http://`, `https://`, `file://`, относительные пути)
/// и прочие локальные цели не разрешаются — линтер не ходит в сеть и на диск.
/// `None`: ссылка внешняя/не-components, цель не существует, `$ref` не строка
/// или глубина исчерпана; вызывающий трактует узел как отсутствующий —
/// правило срабатывает (обратная совместимость со скелетом).
fn resolve_ref<'a>(doc: &'a Value, node: &'a Value, depth: usize) -> Option<&'a Value> {
    let reference = match node.get("$ref") {
        // Не ссылка — узел проверяется как есть.
        None => return Some(node),
        // `$ref` не строка (битый контракт) — ссылка неразрешима.
        Some(reference) => reference.as_str()?,
    };
    if depth == 0 || !reference.starts_with("#/components/") {
        // Внешняя ссылка, чужая локальная цель или срез по глубине.
        return None;
    }
    // `$ref` — JSON Reference: `#` + JSON Pointer от корня документа.
    let target = resolve_pointer(doc, &reference[1..])?;
    resolve_ref(doc, target, depth - 1)
}

/// Идёт по JSON Pointer (фрагмент без `#`, RFC 6901) от корня документа.
///
/// Сегменты раскодируются (`~1` → `/`, `~0` → `~`); цели правил скелета —
/// именованные карты под `#/components`, поэтому узлы пути ожидаются
/// объектами: иной узел или отсутствующий ключ — `None` (цель не существует).
fn resolve_pointer<'a>(doc: &'a Value, fragment: &str) -> Option<&'a Value> {
    if fragment.is_empty() {
        return Some(doc);
    }
    let mut current = doc;
    // Фрагмент после `#` начинается с `/`-разделителя — он не сегмент;
    // без среза первый split-токен был бы пустым ключом.
    for segment in fragment.strip_prefix('/').unwrap_or(fragment).split('/') {
        // RFC 6901: сначала `~1` — иначе `~01` раскодируется неверно.
        let key = segment.replace("~1", "/").replace("~0", "~");
        current = current.as_object()?.get(&key)?;
    }
    Some(current)
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

/// Инструменты модуля: `openapi_lint`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(OpenapiLintTool)]
}

/// Инструмент `openapi_lint`: проверка контракта `OpenAPI` 3.x правилами
/// скелета OA-001..OA-005 (транш T1 контрактного контура, ADR-015).
pub struct OpenapiLintTool;

#[derive(Debug, Deserialize)]
struct OpenapiLintArgs {
    /// Путь к файлу контракта `OpenAPI` (yaml/yml/json).
    path: String,
}

#[async_trait]
impl Tool for OpenapiLintTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "openapi_lint".into(),
            description: "Проверить контракт OpenAPI 3.x: версионирование, идемпотентность \
                          mutating-endpoint'ов, ошибки RFC 7807 (транш T1, ADR-015)"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Путь к файлу контракта OpenAPI (yaml/yml/json)"
                    }
                },
                "required": ["path"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: OpenapiLintArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "openapi_lint: невалидные аргументы: {e}"
                )));
            }
        };
        let path = ctx.resolve(&args.path);
        let findings = match lint_openapi(&path) {
            Ok(f) => f,
            Err(e) => return Ok(ToolOutput::err(format!("openapi_lint: {e}"))),
        };
        Ok(ToolOutput::ok(render_report(&findings)))
    }
}

/// Собирает отчёт в стиле `spine_lint`: сводка, строки находок, итог.
fn render_report(findings: &[Finding]) -> String {
    let errors = findings.iter().filter(|f| f.severity == "error").count();
    let warns = findings.len() - errors;
    let mut report = format!(
        "openapi: {} находок (error: {errors}, warn: {warns})",
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

    /// Валидный контракт: все правила скелета соблюдены.
    const VALID: &str = r#"openapi: 3.0.3
info:
  title: Pet Store API
  version: 1.0.0
paths:
  /v1/pets:
    get:
      operationId: listPets
      responses:
        '200':
          description: ok
        '404':
          description: not found
          content:
            "application/problem+json":
              schema:
                type: object
    post:
      operationId: createPet
      parameters:
        - in: header
          name: idempotency-key
          schema:
            type: string
      responses:
        '201':
          description: created
        '400':
          description: bad request
          content:
            "application/problem+json":
              schema: {}
"#;

    /// Блок operation-уровневого параметра Idempotency-Key в [`VALID`].
    const OP_PARAMS_BLOCK: &str = "      parameters:\n        - in: header\n          name: \
                                   idempotency-key\n          schema:\n            type: string\n";

    /// Контент 404-ответа в [`VALID`] (удаляется в тестах OA-004).
    const NOT_FOUND_CONTENT: &str = "          content:\n            \"application/problem+json\":\n              schema:\n                type: object";

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
            .expect("вызов openapi_lint")
    }

    /// [`VALID`] без operation-уровневых параметров (для OA-003-вариантов).
    fn without_op_params() -> String {
        VALID.replace(OP_PARAMS_BLOCK, "")
    }

    #[test]
    fn factory_exposes_single_openapi_lint_tool() {
        let ts = tools();
        assert_eq!(ts.len(), 1);
        let spec = ts[0].spec();
        assert_eq!(spec.name, "openapi_lint");
        assert!(
            spec.description.contains("OpenAPI 3.x"),
            "{}",
            spec.description
        );
        assert!(
            spec.description.contains("RFC 7807"),
            "{}",
            spec.description
        );
        assert_eq!(spec.parameters["required"], json!(["path"]));
    }

    #[tokio::test]
    async fn valid_contract_passes_clean() {
        let out = lint_text(VALID, "petstore.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("openapi: 0 находок (error: 0, warn: 0)"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
        assert!(!out.content.contains("OA-00"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa001_missing_info_version_is_error() {
        let contract = VALID.replace("  version: 1.0.0\n", "");
        let out = lint_text(&contract, "no-version.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/info OA-001 — info.version отсутствует"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa001_non_semver_version_is_error() {
        for bad in ["1.0", "v1.0.0", "1.0.0-rc1", "1.0.x", "abc"] {
            let contract = VALID.replace("version: 1.0.0", &format!("version: \"{bad}\""));
            let out = lint_text(&contract, "bad-version.yaml").await;
            assert!(!out.is_error, "{bad}: {}", out.content);
            assert!(
                out.content.contains("[error] #/info/version OA-001"),
                "{bad}: {}",
                out.content
            );
            assert!(out.content.contains("Итог: FAIL"), "{bad}: {}", out.content);
        }
    }

    #[tokio::test]
    async fn oa002_path_without_version_prefix_warns() {
        let contract = VALID.replace("/v1/pets", "/pets");
        let out = lint_text(&contract, "no-prefix.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("openapi: 1 находок (error: 0, warn: 1)"),
            "{}",
            out.content
        );
        assert!(
            out.content.contains("[warn] #/paths/~1pets OA-002"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa003_post_without_idempotency_key_is_error() {
        let contract = without_op_params();
        let out = lint_text(&contract, "post-no-key.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/post OA-003"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa003_path_level_idempotency_key_accepted_case_insensitive() {
        // Параметр перенесён на уровень path item и написан как Idempotency-Key.
        let contract = without_op_params().replace(
            "  /v1/pets:\n    get:",
            "  /v1/pets:\n    parameters:\n      - in: header\n        name: Idempotency-Key\n        schema:\n          type: string\n    get:",
        );
        let out = lint_text(&contract, "path-level-key.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(!out.content.contains("OA-003"), "{}", out.content);
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa003_get_without_key_is_clean() {
        // В VALID get не имеет Idempotency-Key — OA-003 его не требует.
        let out = lint_text(VALID, "get-no-key.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(!out.content.contains("OA-003"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa003_put_patch_delete_without_key_warn() {
        for method in ["put", "patch", "delete"] {
            let contract = without_op_params().replace("    post:", &format!("    {method}:"));
            let out = lint_text(&contract, &format!("{method}-no-key.yaml")).await;
            assert!(!out.is_error, "{method}: {}", out.content);
            assert!(
                out.content
                    .contains(&format!("[warn] #/paths/~1v1~1pets/{method} OA-003")),
                "{method}: {}",
                out.content
            );
            assert!(
                out.content.contains("Итог: PASS"),
                "{method}: {}",
                out.content
            );
        }
    }

    #[tokio::test]
    async fn oa004_error_responses_without_problem_json_warn() {
        // Убираем problem+json из 404-ответа и смотрим коды 404/500/default.
        let base = VALID.replace(NOT_FOUND_CONTENT, "");
        for (code, pointer) in [
            ("'404':", "/404"),
            ("'500':", "/500"),
            ("default:", "/default"),
        ] {
            // code несёт и двоеточие ключа ответа — иначе replace ломает YAML.
            let contract = base.replace("'404':", code);
            // Имя файла — из code (pointer содержит `/` и ушёл бы в подкаталог);
            // из code убираем кавычки и завершающее двоеточие.
            let file_name = format!(
                "no-problem-{}.yaml",
                code.trim_matches('\'').trim_end_matches(':')
            );
            let out = lint_text(&contract, &file_name).await;
            assert!(!out.is_error, "{code}: {}", out.content);
            assert!(
                out.content.contains(&format!(
                    "[warn] #/paths/~1v1~1pets/get/responses{pointer} OA-004"
                )),
                "{code}: {}",
                out.content
            );
            assert!(
                out.content.contains("Итог: PASS"),
                "{code}: {}",
                out.content
            );
        }
    }

    #[tokio::test]
    async fn oa004_error_response_with_problem_json_is_clean() {
        // В VALID у 404 и 400 есть problem+json — OA-004 не срабатывает.
        let out = lint_text(VALID, "with-problem.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(!out.content.contains("OA-004"), "{}", out.content);
    }

    /// Возвращает [`VALID`] без inline-параметров post'а, с `$ref`-параметрами
    /// вместо них (`with_parameters` — YAML-список параметров) и дописанным
    /// в конец блоком `components` верхнего уровня (`components` — YAML, может
    /// быть пустым для тестов внешних ссылок).
    fn ref_param_contract(with_parameters: &str, components: &str) -> String {
        without_op_params().replace(
            "    post:\n      operationId: createPet",
            &format!("    post:\n      parameters:{with_parameters}      operationId: createPet"),
        ) + components
    }

    #[tokio::test]
    async fn oa003_idempotency_key_via_local_ref_to_component_parameter_is_clean() {
        // Параметр post'а — $ref на #/components/parameters: локальная ссылка
        // разрешается, Idempotency-Key виден по цели, OA-003 не срабатывает.
        let contract = ref_param_contract(
            "\n        - $ref: '#/components/parameters/IdempotencyKey'\n",
            "components:\n  parameters:\n    IdempotencyKey:\n      in: header\n      name: \
             idempotency-key\n      schema:\n        type: string\n",
        );
        let out = lint_text(&contract, "ref-param-key.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("openapi: 0 находок (error: 0, warn: 0)"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa003_ref_chain_to_component_parameter_is_clean() {
        // Цепочка $ref (параметр → ComponentsA → ComponentsB): OA-003 видит
        // Idempotency-Key через оба звена локальных ссылок.
        let contract = ref_param_contract(
            "\n        - $ref: '#/components/parameters/ComponentsA'\n",
            "components:\n  parameters:\n    ComponentsA:\n      $ref: \
             '#/components/parameters/ComponentsB'\n    ComponentsB:\n      in: header\n      name: \
             idempotency-key\n      schema:\n        type: string\n",
        );
        let out = lint_text(&contract, "ref-chain-param-key.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("openapi: 0 находок (error: 0, warn: 0)"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa004_problem_json_via_local_ref_to_component_response_is_clean() {
        // 404-ответ get'а — $ref на #/components/responses: problem+json
        // виден по цели локальной ссылки, OA-004 не срабатывает.
        let contract = VALID.replace(NOT_FOUND_CONTENT, "").replace(
            "        '404':\n          description: not found",
            "        '404':\n          $ref: '#/components/responses/NotFound'",
        ) + "components:\n  responses:\n    NotFound:\n      description: not found\n      \
               content:\n        \"application/problem+json\":\n          schema:\n            \
               type: object\n";
        let out = lint_text(&contract, "ref-problem-response.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("openapi: 0 находок (error: 0, warn: 0)"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa003_external_ref_is_not_resolved_rule_fires() {
        // Внешний $ref (https://…) не разрешается — линтер не ходит в сеть,
        // параметр за ним считается отсутствующим: OA-003 срабатывает.
        let contract = ref_param_contract(
            "\n        - $ref: 'https://example.com/common.yaml#/components/parameters/\
             IdempotencyKey'\n",
            "",
        );
        let out = lint_text(&contract, "external-ref-param.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/post OA-003"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa004_external_ref_response_rule_fires() {
        // Внешний $ref в 404-ответе не разрешается: OA-004 срабатывает.
        let contract = VALID.replace(NOT_FOUND_CONTENT, "").replace(
            "        '404':\n          description: not found",
            "        '404':\n          $ref: 'https://example.com/common.yaml#/components/\
                 responses/NotFound'",
        );
        let out = lint_text(&contract, "external-ref-response.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[warn] #/paths/~1v1~1pets/get/responses/404 OA-004"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa003_ref_to_missing_component_pointer_rule_fires() {
        // $ref на несуществующий JSON Pointer — цель не найдена, параметр
        // считается отсутствующим: OA-003 срабатывает.
        let contract = ref_param_contract(
            "\n        - $ref: '#/components/parameters/NoSuchKey'\n",
            "components:\n  parameters:\n    Other:\n      in: header\n      name: other\n",
        );
        let out = lint_text(&contract, "missing-ref-param.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/post OA-003"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn cyclic_local_refs_terminate_via_depth_limit() {
        // Цикл $ref (A→B→A) обрывается срезом по глубине MAX_REF_DEPTH:
        // линтер завершается, параметр считается отсутствующим — OA-003.
        let contract = ref_param_contract(
            "\n        - $ref: '#/components/parameters/A'\n",
            "components:\n  parameters:\n    A:\n      $ref: '#/components/parameters/B'\n    \
             B:\n      $ref: '#/components/parameters/A'\n",
        );
        let out = lint_text(&contract, "cyclic-ref-param.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/post OA-003"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn oa005_missing_operation_id_warns() {
        let contract = VALID.replace("      operationId: listPets\n", "");
        let out = lint_text(&contract, "no-op-id.yaml").await;
        assert!(!out.is_error, "{}", out.content);
        assert!(
            out.content.contains("[warn] #/paths/~1v1~1pets/get OA-005"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
    }

    #[tokio::test]
    async fn json_format_is_accepted_on_par_with_yaml() {
        let contract = r#"{
  "openapi": "3.0.3",
  "info": {"title": "T", "version": "2.1.0"},
  "paths": {
    "/v1/pets": {
      "post": {"responses": {"400": {"description": "bad request"}}}
    }
  }
}"#;
        let out = lint_text(contract, "contract.json").await;
        assert!(!out.is_error, "{}", out.content);
        // Правила работают по Value-документу независимо от исходного формата.
        assert!(
            out.content
                .contains("[error] #/paths/~1v1~1pets/post OA-003"),
            "{}",
            out.content
        );
        assert!(
            out.content
                .contains("[warn] #/paths/~1v1~1pets/post/responses/400 OA-004"),
            "{}",
            out.content
        );
        assert!(
            out.content
                .contains("[warn] #/paths/~1v1~1pets/post OA-005"),
            "{}",
            out.content
        );
        assert!(out.content.contains("Итог: FAIL"), "{}", out.content);
    }

    #[tokio::test]
    async fn not_openapi3_file_errors() {
        let out = lint_text("title: hello\nversion: 1.0.0\n", "not-openapi.yaml").await;
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("не OpenAPI 3.x"), "{}", out.content);
    }

    #[tokio::test]
    async fn broken_json_and_yaml_errors() {
        let out = lint_text("{{{", "broken.json").await;
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("невалидный JSON"), "{}", out.content);

        let out = lint_text("openapi: [3.0.3\n", "broken.yaml").await;
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
            .expect("вызов openapi_lint");
        assert!(out.is_error, "{}", out.content);
        assert!(out.content.contains("openapi_lint"), "{}", out.content);
    }
}
