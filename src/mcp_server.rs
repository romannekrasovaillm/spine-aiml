//! MCP-серверный режим: `arch-ml mcp serve` отдаёт архитектурный контроль
//! наружу кодовым агентам (Claude Code и др.) — verdict в момент написания
//! кода, а не на приёмке пакета (ADR-008, находка F-5 / задача P1-2).
//!
//! КОНТРАКТ (владелец: агент `mcp-serve`):
//! - транспорт stdio, NDJSON: одно сообщение JSON-RPC 2.0 — одна строка
//!   (как у клиента [`crate::mcp`], без `Content-Length`-фрейминга);
//!   stdout — только протокол, логи — stderr (tracing в `main`);
//! - методы: `initialize` (echo известной версии протокола, иначе наша),
//!   `tools/list`, `tools/call`, `ping`; `notifications/*` — игнор без
//!   ответа; неизвестный метод → `-32601`, битый JSON → `-32700`,
//!   отсутствует `method` → `-32600`, битые аргументы/инструмент → `-32602`;
//! - инструменты (все `readOnlyHint`): контрольные `spine_lint`,
//!   `fitness_check`, `significance_score`, `trace_check`, `model_query`,
//!   `rubric_run` и чтение знаний (T4, ADR-015): `kb_search`,
//!   `skill_search`, `skill_load`, `mermaid_render`; наружу НЕ отдаются
//!   write/exec-инструменты агента (`bash`, `write_file`, `edit_file`,
//!   `harness_run`, `subagent_run`, …) — сервер строго read-only
//!   (проверяется тестом реестра); пути — только аргументами вызова,
//!   глобального «текущего кейса» нет;
//! - успешный вызов: `structuredContent` (машиночитаемый verdict) + тот же
//!   объект pretty-JSON в `content[0].text`; контрольные verdict'ы несут
//!   `passed: bool` — `false` означает блокирующую находку, клиентский агент
//!   обязан отказать изменению, нарушающему `AD-*`;
//! - доменный сбой выполнения (файл не читается, сущность не найдена) —
//!   `result` с `isError: true`, не protocol error; сервер не падает ни на
//!   каком вводе, цикл живёт до EOF stdin;
//! - `rubric_run` требует LLM-ключ из конфига: предпроверка доступности
//!   ключа (env задана / файл ключа существует; содержимое не печатается)
//!   → без ключа понятная JSON-RPC ошибка `-32603`.

use std::collections::BTreeMap;
use std::fmt;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Deserialize;
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};

use crate::config::{Config, ModelConfig};
use crate::error::Result;
use crate::{control, kb, mcp, mermaid, model, plugin, rubric, trace};

/// Код JSON-RPC «разбор запроса не удался» (невалидный JSON).
const PARSE_ERROR: i64 = -32700;
/// Код JSON-RPC «некорректный запрос» (не объект, нет `method`).
const INVALID_REQUEST: i64 = -32600;
/// Код JSON-RPC «метод не найден».
const METHOD_NOT_FOUND: i64 = -32601;
/// Код JSON-RPC «некорректные параметры» (аргументы, неизвестный инструмент).
const INVALID_PARAMS: i64 = -32602;
/// Код JSON-RPC «внутренняя ошибка» (недоступная capability: нет LLM-ключа).
const INTERNAL_ERROR: i64 = -32603;

/// Максимальная длина одной входной строки (`16 МиБ`). Защита от безграничного
/// роста буфера построчного чтения; самый тяжёлый легальный вход — текст
/// документа для `rubric_run` (лимит рубрики 24k символов), запас ~600×.
const MAX_LINE_BYTES: usize = 16 * 1024 * 1024;

/// Дефолтный лимит хитов `kb_search` (контракт инструмента kb.rs).
const KB_SEARCH_DEFAULT_LIMIT: usize = 10;
/// Дефолтный лимит хитов `skill_search` (контракт инструмента plugin.rs).
const SKILL_SEARCH_DEFAULT_LIMIT: usize = 8;
/// Потолок хитов поисков знаний (общий у kb.rs и plugin.rs).
const KNOWLEDGE_MAX_HITS: usize = 20;
/// Потолок символов тела скилла в ответе `skill_load` (как у агентного
/// инструмента: `ToolOutput::truncated(16_000)` — защита контекста клиента).
const SKILL_TEXT_MAX_CHARS: usize = 16_000;

/// Ошибка вызова инструмента: на каком уровне протокола отвечать.
#[derive(Debug)]
enum CallError {
    /// Ошибка протокола: ответ error-объектом (`-32602`/`-32603`).
    Protocol {
        /// Код ошибки JSON-RPC.
        code: i64,
        /// Сообщение (рус.).
        message: String,
    },
    /// Доменная ошибка выполнения: `result` с `isError: true` (MCP-стиль,
    /// как у агентских инструментов) — клиент видит причину, сервер жив.
    Execution(String),
}

impl CallError {
    /// Доменная ошибка выполнения с префиксом инструмента.
    fn execution(tool: &str, e: impl fmt::Display) -> Self {
        Self::Execution(format!("{tool}: {e}"))
    }

    /// Ошибка параметров (`-32602`).
    fn invalid_params(message: String) -> Self {
        Self::Protocol {
            code: INVALID_PARAMS,
            message,
        }
    }
}

/// Состояние сервера: только конфигурация (нужна `rubric_run` для резолва
/// рубрик и LLM-судьи). Путей/«текущего кейса» сервер не хранит — все цели
/// приходят аргументами вызова (read-only по отношению к репозиторию клиента).
pub struct McpServe {
    cfg: Arc<Config>,
}

/// Ответ-успех JSON-RPC.
fn ok_response(id: &Value, result: &Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

/// Ответ-ошибка JSON-RPC.
fn error_response(id: &Value, code: i64, message: impl Into<String>) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message.into()},
    })
}

/// Разбирает аргументы инструмента в типизированную структуру;
/// ошибка десериализации → `-32602`.
fn parse_args<T: serde::de::DeserializeOwned>(
    args: Value,
    tool: &str,
) -> std::result::Result<T, CallError> {
    serde_json::from_value(args)
        .map_err(|e| CallError::invalid_params(format!("{tool}: невалидные аргументы: {e}")))
}

/// Прогоняет синхронную доменную функцию на blocking-пуле (fitness-правила
/// `command_succeeds` и обходы fs не должны держать worker runtime).
async fn blocking<T, F>(tool: &str, f: F) -> std::result::Result<T, CallError>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T> + Send + 'static,
{
    match tokio::task::spawn_blocking(f).await {
        Ok(Ok(value)) => Ok(value),
        Ok(Err(e)) => Err(CallError::execution(tool, e)),
        Err(e) => Err(CallError::Execution(format!(
            "{tool}: задача прервана: {e}"
        ))),
    }
}

impl McpServe {
    /// Сервер поверх конфигурации харнесса.
    #[must_use]
    pub fn new(cfg: Arc<Config>) -> Self {
        Self { cfg }
    }

    /// Обрабатывает одну строку транспорта; `None` — отвечать не нужно
    /// (уведомления по JSON-RPC ответа не имеют).
    async fn handle_line(&self, line: &str) -> Option<Value> {
        let message: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                return Some(error_response(
                    &Value::Null,
                    PARSE_ERROR,
                    format!("невалидный JSON: {e}"),
                ));
            }
        };
        let Some(obj) = message.as_object() else {
            return Some(error_response(
                &Value::Null,
                INVALID_REQUEST,
                "сообщение не является JSON-объектом",
            ));
        };
        let id = obj.get("id").cloned();
        let method = obj.get("method").and_then(Value::as_str);
        match (id, method) {
            // Уведомления (без id) не получают ответа — ни на notifications/*,
            // ни на неизвестные методы-уведомления.
            (None, Some(_)) => None,
            (None, None) => Some(error_response(
                &Value::Null,
                INVALID_REQUEST,
                "нет поля 'method'",
            )),
            (Some(id), None) => Some(error_response(&id, INVALID_REQUEST, "нет поля 'method'")),
            (Some(id), Some(method)) => {
                let params = obj.get("params").cloned().unwrap_or(Value::Null);
                Some(self.handle_request(&id, method, params).await)
            }
        }
    }

    /// Диспетчер запросов (методы с `id`, требующие ответа).
    async fn handle_request(&self, id: &Value, method: &str, params: Value) -> Value {
        match method {
            "initialize" => {
                // Echo известной версии из запроса, иначе — наша текущая:
                // клиент сам решит, устраивает ли его ответная версия.
                let requested = params.get("protocolVersion").and_then(Value::as_str);
                let version = match requested {
                    Some(v)
                        if v == mcp::PROTOCOL_VERSION || v == mcp::PROTOCOL_VERSION_FALLBACK =>
                    {
                        v
                    }
                    _ => mcp::PROTOCOL_VERSION,
                };
                ok_response(
                    id,
                    &json!({
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": false}},
                        "serverInfo": {
                            "name": "arch-ml",
                            "version": env!("CARGO_PKG_VERSION"),
                        },
                        "instructions": "Архитектурный контроль Spine для кодового агента: \
                                         перед коммитом изменения вызывайте fitness_check \
                                         (repo + CONSTRAINTS.yaml), trace_check (case) и \
                                         spine_lint (path); passed=false с находками error — \
                                         основание ОТКАЗАТЬ изменению, нарушающему AD-*, \
                                         перечислив находки. significance_score — маршрут \
                                         значимости (fast/standard/critical). model_query — \
                                         карточки и связи сущностей модели. rubric_run — \
                                         LLM-оценка документа рубрикой (нужен API-ключ). \
                                         Чтение знаний (read-only): kb_search — поиск по \
                                         базе знаний архитектора; skill_search/skill_load — \
                                         библиотека скиллов; mermaid_render — диаграмма \
                                         mermaid (code/path) в ASCII-арт.",
                    }),
                )
            }
            "ping" => ok_response(id, &json!({})),
            "tools/list" => ok_response(id, &json!({"tools": tool_specs()})),
            "tools/call" => self.handle_tool_call(id, &params).await,
            other => error_response(id, METHOD_NOT_FOUND, format!("неизвестный метод '{other}'")),
        }
    }

    /// `tools/call`: разбор `name`/`arguments`, диспетчер инструментов,
    /// упаковка verdict'а в MCP-ответ.
    async fn handle_tool_call(&self, id: &Value, params: &Value) -> Value {
        let Some(name) = params.get("name").and_then(Value::as_str) else {
            return error_response(id, INVALID_PARAMS, "tools/call: нет строкового поля 'name'");
        };
        let args = params
            .get("arguments")
            .cloned()
            .unwrap_or_else(|| json!({}));
        if !args.is_object() {
            return error_response(
                id,
                INVALID_PARAMS,
                format!("tools/call: 'arguments' должен быть объектом, получено: {args}"),
            );
        }
        match self.dispatch_tool(name, args).await {
            Ok(structured) => {
                // Клиент нашего же mcp.rs читает только text-части — дублируем
                // verdict pretty-JSON; structuredContent — для MCP-клиентов.
                let text = serde_json::to_string_pretty(&structured)
                    .unwrap_or_else(|_| structured.to_string());
                ok_response(
                    id,
                    &json!({
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": structured,
                        "isError": false,
                    }),
                )
            }
            Err(CallError::Execution(message)) => ok_response(
                id,
                &json!({
                    "content": [{"type": "text", "text": message}],
                    "isError": true,
                }),
            ),
            Err(CallError::Protocol { code, message }) => error_response(id, code, message),
        }
    }

    /// Маршрутизация вызова по имени инструмента.
    async fn dispatch_tool(
        &self,
        name: &str,
        args: Value,
    ) -> std::result::Result<Value, CallError> {
        match name {
            "spine_lint" => self.tool_spine_lint(args).await,
            "fitness_check" => self.tool_fitness_check(args).await,
            "significance_score" => Self::tool_significance_score(args),
            "trace_check" => self.tool_trace_check(args).await,
            "model_query" => self.tool_model_query(args).await,
            "rubric_run" => self.tool_rubric_run(args).await,
            // T4 (ADR-015): чтение знаний наружу; тонкие адаптеры к ядру
            // агентных инструментов kb.rs/plugin.rs/mermaid.rs (общая логика
            // живёт там — MCP-слой только парсит аргументы и формирует JSON).
            "kb_search" => self.tool_kb_search(args).await,
            "skill_search" => self.tool_skill_search(args).await,
            "skill_load" => self.tool_skill_load(args).await,
            "mermaid_render" => self.tool_mermaid_render(args).await,
            other => Err(CallError::invalid_params(format!(
                "неизвестный инструмент '{other}' (список — tools/list)"
            ))),
        }
    }

    /// `spine_lint`: линтер ARCHITECTURE-SPINE.md → verdict
    /// (`passed` = нет находок severity error).
    async fn tool_spine_lint(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Путь к ARCHITECTURE-SPINE.md.
            path: String,
        }
        let args: Args = parse_args(args, "spine_lint")?;
        let path = PathBuf::from(args.path);
        let issues = blocking("spine_lint", move || control::lint_spine(&path)).await?;
        let errors = issues.iter().filter(|i| i.severity == "error").count();
        let warns = issues.len() - errors;
        let summary = if issues.is_empty() {
            "spine: нарушений нет".to_string()
        } else {
            format!(
                "spine: {} находок (error: {errors}, warn: {warns})",
                issues.len()
            )
        };
        Ok(json!({
            "passed": errors == 0,
            "issue_count": issues.len(),
            "error_count": errors,
            "warn_count": warns,
            "issues": issues,
            "summary": summary,
        }))
    }

    /// `fitness_check`: прогон CONSTRAINTS.yaml по репозиторию → verdict
    /// (семантика [`control::check`]: `passed` = нет находок severity error).
    async fn tool_fitness_check(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Корень репозитория клиента.
            repo: String,
            /// Файл ограничений (дефолт — рабочий `<repo>/CONSTRAINTS.yaml`,
            /// затем пакетный `<repo>/.arch-handoff/CONSTRAINTS.yaml`).
            constraints: Option<String>,
        }
        let args: Args = parse_args(args, "fitness_check")?;
        let repo = PathBuf::from(args.repo);
        // Дефолт — рабочий ruleset (C-037, ADR-011): пакетная заготовка
        // только как fallback, о ней клиент предупреждается в ответе.
        let (constraints, resolved) = if let Some(path) = args.constraints {
            (PathBuf::from(path), None)
        } else {
            let resolved = control::resolve_ruleset_required(&repo)
                .map_err(|e| CallError::execution("fitness_check", e))?;
            (resolved.path.clone(), Some(resolved))
        };
        let warning = resolved
            .as_ref()
            .and_then(|r| r.warning(&repo))
            .unwrap_or_default();
        let ruleset = constraints.display().to_string();
        let ruleset_kind = resolved.as_ref().map_or("явный путь", |r| r.kind.label());
        let report = blocking("fitness_check", move || control::check(&repo, &constraints)).await?;
        Ok(json!({
            "passed": report.passed,
            "repo": report.repo,
            "issue_count": report.issues.len(),
            "issues": report.issues,
            "summary": report.summary,
            "ruleset": ruleset,
            "ruleset_kind": ruleset_kind,
            "warning": warning,
        }))
    }

    /// `significance_score`: маршрут значимости по 15 триггерам
    /// (информационный инструмент, verdict `passed` не применим).
    /// Не метод: конфиг не нужен (clippy `unused_self` — `&self` осознанно
    /// отсутствует, в отличие от соседних инструментов).
    fn tool_significance_score(args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Карта «триггер → сработал».
            triggers: BTreeMap<String, bool>,
        }
        let args: Args = parse_args(args, "significance_score")?;
        let s = control::significance_score(&args.triggers);
        let unknown: Vec<&str> = s
            .fired
            .iter()
            .map(String::as_str)
            .filter(|f| !control::SIGNIFICANCE_TRIGGERS.contains(f))
            .collect();
        Ok(json!({
            "score": s.score,
            "fired": s.fired,
            "route": s.route,
            "unknown_triggers": unknown,
            "summary": format!("Score: {} → маршрут {}", s.score, s.route),
        }))
    }

    /// `trace_check`: позвенная трассируемость кейса → verdict
    /// (`passed` = нет находок severity error) + markdown-отчёт.
    async fn tool_trace_check(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Корень кейса (каталог с `model/`).
            case: String,
        }
        let args: Args = parse_args(args, "trace_check")?;
        let case = PathBuf::from(args.case);
        let report = blocking("trace_check", move || trace::trace_check(&case)).await?;
        let levels: Vec<Value> = report
            .levels
            .iter()
            .map(|l| {
                json!({
                    "name": l.name,
                    "total": l.total,
                    "covered": l.covered,
                    "unverifiable": l.unverifiable,
                    "orphans": l.orphans,
                    "percent": (l.covered * 100).checked_div(l.total),
                })
            })
            .collect();
        let issues: Vec<Value> = report
            .issues
            .iter()
            .map(|i| {
                json!({
                    "severity": i.severity.to_string(),
                    "rule": i.rule,
                    "message": i.message,
                })
            })
            .collect();
        let errors = report
            .issues
            .iter()
            .filter(|i| i.severity == model::Severity::Error)
            .count();
        let passed = !report.has_errors();
        Ok(json!({
            "passed": passed,
            "entities": report.entities,
            "constraint_rules": report.constraint_rules,
            "spine_ads": report.spine_ads,
            "levels": levels,
            "issue_count": report.issues.len(),
            "error_count": errors,
            "warn_count": report.issues.len() - errors,
            "issues": issues,
            "report_markdown": trace::render_markdown(&report),
            "summary": format!(
                "Итог: {} (error: {errors}, warn: {})",
                if passed { "PASS" } else { "FAIL" },
                report.issues.len() - errors
            ),
        }))
    }

    /// `model_query`: список сущностей модели (с фильтром по типу) либо
    /// карточка сущности по `id` со связями и обратными ссылками.
    async fn tool_model_query(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Каталог модели (дефолт `model` от cwd процесса сервера).
            dir: Option<String>,
            /// ID сущности — карточка (без `id` — список).
            id: Option<String>,
            /// Фильтр списка по типу (`cmp`, `adr`, … или префикс `CMP`).
            #[serde(rename = "type")]
            kind: Option<String>,
        }
        let args: Args = parse_args(args, "model_query")?;
        let kind = match &args.kind {
            Some(raw) => {
                let norm = raw.trim().to_ascii_lowercase();
                model::EntityKind::from_type_str(&norm)
                    .or_else(|| model::EntityKind::from_prefix(&raw.trim().to_ascii_uppercase()))
                    .ok_or_else(|| {
                        CallError::invalid_params(format!(
                            "model_query: неизвестный тип '{raw}' (допустимы: {})",
                            model::EntityKind::type_names().join(", ")
                        ))
                    })
                    .map(Some)?
            }
            None => None,
        };
        let dir = PathBuf::from(args.dir.unwrap_or_else(|| "model".into()));
        let id = args.id;
        blocking("model_query", move || {
            let m = model::load_model(&dir)?;
            model_query_value(&m, id.as_deref(), kind, &dir)
        })
        .await
    }

    /// `rubric_run`: оценка документа рубрикой LLM-судьёй (ADR-004).
    ///
    /// Без доступного API-ключа провайдера — JSON-RPC `-32603` с подсказкой,
    /// какой env/файл настроить (содержимое ключа не читается в ответ).
    async fn tool_rubric_run(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Рубрика: имя в каталоге рубрик (`paths.rubrics_dir`) или путь к YAML.
            rubric: String,
            /// Путь к оцениваемому документу (md/txt).
            target: Option<String>,
            /// Текст документа inline (альтернатива `target`).
            target_text: Option<String>,
            /// Модель-судья (имя из `[models]`, дефолт — `default_model`).
            model: Option<String>,
        }
        let args: Args = parse_args(args, "rubric_run")?;
        let text = match (args.target, args.target_text) {
            (Some(path), None) => {
                let path = PathBuf::from(path);
                blocking("rubric_run", move || {
                    std::fs::read_to_string(&path)
                        .map_err(|e| crate::error::HarnessError::io(&path, e))
                })
                .await?
            }
            (None, Some(text)) => text,
            _ => {
                return Err(CallError::invalid_params(
                    "rubric_run: укажите ровно один из аргументов 'target' / 'target_text'".into(),
                ));
            }
        };
        let model_name = args.model.unwrap_or_else(|| self.cfg.default_model.clone());
        let model_cfg = self.cfg.models.get(&model_name).ok_or_else(|| {
            CallError::invalid_params(format!(
                "rubric_run: модель '{model_name}' не настроена в [models] конфига"
            ))
        })?;
        if !api_key_available(model_cfg) {
            return Err(CallError::Protocol {
                code: INTERNAL_ERROR,
                message: format!(
                    "rubric_run: нет API-ключа провайдера '{model_name}' — установите переменную \
                     окружения '{}' или положите ключ в файл {:?} (см. README «API keys»)",
                    model_cfg.api_key_env, model_cfg.api_key_file
                ),
            });
        }
        let rubric_path = resolve_rubric(&self.cfg.paths.rubrics_dir(), &args.rubric);
        let rub = blocking("rubric_run", move || rubric::load(&rubric_path)).await?;
        let registry = crate::llm::LlmRegistry::from_config(&self.cfg)
            .map_err(|e| CallError::execution("rubric_run", e))?;
        let judge = registry
            .get(&model_name)
            .map_err(|e| CallError::execution("rubric_run", e))?;
        let report = rubric::evaluate_with_options(&rub, &text, judge.as_ref(), &self.cfg.judge)
            .await
            .map_err(|e| CallError::execution("rubric_run", e))?;
        // Отчёт НЕ пишется на диск (read-only-семантика сервера, ADR-008) —
        // markdown возвращается в verdict'е.
        Ok(json!({
            "rubric": report.rubric_name,
            "judge_model": report.judge_model,
            "judge_samples": report.judge_samples,
            "weighted_total": report.weighted_total,
            "verdict": report.verdict,
            "scores": report.scores,
            "report_markdown": report.to_markdown(),
            "summary": format!(
                "Рубрика '{}': {:.2}/5 (судья {})",
                report.rubric_name, report.weighted_total, report.judge_model
            ),
        }))
    }

    /// `kb_search`: поиск по локальной базе знаний харнесса
    /// (`knowledge.dirs` из конфига arch, а не каталоги репозитория клиента).
    /// Ядро — [`kb::search`] (то же, что у агентного инструмента);
    /// MCP-слой только формирует JSON из хитов.
    async fn tool_kb_search(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Поисковый запрос: термины через пробел.
            query: String,
            /// Максимум хитов (по умолчанию 10, не больше 20).
            limit: Option<usize>,
        }
        let args: Args = parse_args(args, "kb_search")?;
        let limit = args
            .limit
            .unwrap_or(KB_SEARCH_DEFAULT_LIMIT)
            .min(KNOWLEDGE_MAX_HITS);
        let hits = kb::search(
            &self.cfg.knowledge.dirs,
            &self.cfg.knowledge.extensions,
            &args.query,
            limit,
        )
        .await
        .map_err(|e| CallError::execution("kb_search", e))?;
        let summary = if hits.is_empty() {
            format!(
                "По запросу «{}» в базе знаний ничего не найдено.",
                args.query
            )
        } else {
            format!("По запросу «{}» найдено хитов: {}", args.query, hits.len())
        };
        Ok(json!({
            "query": args.query,
            "count": hits.len(),
            "hits": hits,
            "summary": summary,
        }))
    }

    /// `skill_search`: поиск по библиотеке скиллов (`plugins.dirs` из конфига
    /// arch). Ядро — [`plugin::discover`] + [`plugin::search`] (агентный
    /// инструмент `skill_search` использует те же функции).
    async fn tool_skill_search(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Поисковый запрос.
            query: String,
            /// Максимум результатов (по умолчанию 8, не больше 20).
            limit: Option<usize>,
        }
        let args: Args = parse_args(args, "skill_search")?;
        let limit = args
            .limit
            .unwrap_or(SKILL_SEARCH_DEFAULT_LIMIT)
            .min(KNOWLEDGE_MAX_HITS);
        let dirs = self.cfg.plugins.dirs.clone();
        let query = args.query;
        blocking("skill_search", move || -> Result<Value> {
            let plugins = plugin::discover(&dirs);
            let total: usize = plugins.iter().map(|p| p.skills.len()).sum();
            let hits = plugin::search(&plugins, &query, limit);
            let summary = if hits.is_empty() {
                format!("по запросу '{query}' ничего не найдено (скиллов в индексе: {total})")
            } else {
                format!("по запросу '{query}' найдено скиллов: {}", hits.len())
            };
            Ok(json!({
                "query": query,
                "count": hits.len(),
                "hits": hits.iter().map(|h| json!({
                    "name": h.meta.name,
                    "plugin": h.meta.plugin,
                    "score": h.score,
                    "description": h.meta.description,
                    "snippet": h.snippet,
                })).collect::<Vec<_>>(),
                "summary": summary,
            }))
        })
        .await
    }

    /// `skill_load`: полный текст скилла по точному имени. Ядро —
    /// [`plugin::skill_by_name`] + [`plugin::load_skill`]; лимит тела —
    /// как у агентного инструмента (`SKILL_TEXT_MAX_CHARS`).
    async fn tool_skill_load(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Точное имя скилла.
            name: String,
        }
        let args: Args = parse_args(args, "skill_load")?;
        let dirs = self.cfg.plugins.dirs.clone();
        let name = args.name;
        blocking("skill_load", move || -> Result<Value> {
            let plugins = plugin::discover(&dirs);
            let Some(meta) = plugin::skill_by_name(&plugins, &name) else {
                return Err(crate::error::HarnessError::Tool(format!(
                    "скилл '{name}' не найден; сначала skill_search"
                )));
            };
            let mut text = plugin::load_skill(meta)?;
            if text.chars().count() > SKILL_TEXT_MAX_CHARS {
                let cut: String = text.chars().take(SKILL_TEXT_MAX_CHARS).collect();
                text = format!("{cut}\n… [усечено: {SKILL_TEXT_MAX_CHARS} символов]");
            }
            Ok(json!({
                "name": meta.name,
                "plugin": meta.plugin,
                "description": meta.description,
                "path": meta.path,
                "body": text,
                "summary": format!("Скилл '{}' загружен (плагин '{}').", meta.name, meta.plugin),
            }))
        })
        .await
    }

    /// `mermaid_render`: диаграмма mermaid → ASCII-арт. Вход — `code`
    /// (исходник) или `path` (файл относительно cwd сервера, как и пути
    /// контрольных инструментов). Ядро — [`mermaid::render`] /
    /// [`mermaid::read_diagram_source`] (агентный `mermaid_render`).
    async fn tool_mermaid_render(&self, args: Value) -> std::result::Result<Value, CallError> {
        #[derive(Deserialize)]
        struct Args {
            /// Исходный код mermaid-диаграммы.
            code: Option<String>,
            /// Путь к .mmd-файлу (резолвится от cwd сервера).
            path: Option<String>,
        }
        let args: Args = parse_args(args, "mermaid_render")?;
        match args.code {
            Some(code) if !code.trim().is_empty() => {
                blocking("mermaid_render", move || mermaid_render_value(&code)).await
            }
            _ => match args.path {
                Some(path) => {
                    let path = PathBuf::from(path);
                    blocking("mermaid_render", move || {
                        let code = mermaid::read_diagram_source(&path)?;
                        mermaid_render_value(&code)
                    })
                    .await
                }
                None => Err(CallError::Execution(
                    "mermaid_render: нужен аргумент 'code' (исходник) или 'path' (файл)".into(),
                )),
            },
        }
    }
}

/// Рендерит код диаграммы в JSON-ответ `mermaid_render` (общий для inline-кода
/// и файла): арт + вид диаграммы. Ошибки парсера — [`HarnessError::Mermaid`]
/// с номером строки, как у агентного инструмента.
fn mermaid_render_value(code: &str) -> Result<Value> {
    let art = mermaid::render(code)?;
    let kind = match mermaid::diagram_kind(code) {
        mermaid::DiagramKind::Flowchart => "flowchart",
        mermaid::DiagramKind::Sequence => "sequenceDiagram",
        mermaid::DiagramKind::Er => "erDiagram",
        mermaid::DiagramKind::C4 => "c4",
        mermaid::DiagramKind::C4Unsupported | mermaid::DiagramKind::Unknown => {
            // `render` выше уже отклонил эти виды — ветка недостижима, но
            // DiagramKind не знает об успехе; держимся консервативно.
            "unknown"
        }
    };
    Ok(json!({
        "kind": kind,
        "art": art,
        "summary": format!("Диаграмма ({kind}) отрендерена в ASCII-арт."),
    }))
}

/// Строит JSON-ответ `model_query`: список сущностей либо карточка по `id`.
fn model_query_value(
    m: &model::Model,
    id: Option<&str>,
    kind: Option<model::EntityKind>,
    dir: &Path,
) -> Result<Value> {
    if let Some(id) = id {
        let e = m.get(id).ok_or_else(|| {
            crate::error::HarnessError::Model(format!(
                "model_query: сущность '{id}' не найдена (всего сущностей: {})",
                m.entities.len()
            ))
        })?;
        let mut links = serde_json::Map::new();
        for lk in model::LinkKind::ALL {
            let targets = e.link_targets(lk);
            if !targets.is_empty() {
                links.insert(lk.field_name().to_string(), json!(targets));
            }
        }
        let referents: Vec<Value> = m
            .referents(&e.id)
            .iter()
            .map(|(src, lk)| json!({"id": src.id, "title": src.title, "via": lk.field_name()}))
            .collect();
        return Ok(json!({
            "entity": {
                "id": e.id,
                "kind": e.kind.type_str(),
                "kind_ru": e.kind.title_ru(),
                "status": e.status,
                "title": e.title,
                "date": e.date,
                "verification": e.verification,
                "file": e.file,
                "links": links,
                "referents": referents,
                "body": e.body,
            },
            "card": model::card(m, e),
        }));
    }
    let entities: Vec<Value> = m
        .entities
        .iter()
        .filter(|e| kind.is_none_or(|k| e.kind == k))
        .map(|e| {
            let links: usize = model::LinkKind::ALL
                .iter()
                .map(|k| e.link_targets(*k).len())
                .sum();
            json!({
                "id": e.id,
                "kind": e.kind.type_str(),
                "status": e.status,
                "title": e.title,
                "links": links,
            })
        })
        .collect();
    Ok(json!({
        "dir": dir,
        "total": entities.len(),
        "entities": entities,
    }))
}

/// Резолвит рубрику: существующий путь → как есть; имя в каталоге рубрик →
/// `<dir>/<name>` или `<dir>/<name>.yaml` (семантика `resolve_asset` из CLI).
fn resolve_rubric(dir: &Path, name: &str) -> PathBuf {
    let as_path = PathBuf::from(name);
    if as_path.is_file() {
        return as_path;
    }
    let in_dir = dir.join(name);
    if in_dir.is_file() {
        return in_dir;
    }
    dir.join(format!("{name}.yaml"))
}

/// Доступен ли API-ключ провайдера (та же семантика, что у резолва ключа
/// в `llm::openai_compat`: env непустая после trim; файл с `~`-раскрытием
/// читается и непуст). Содержимое ключа в ответы/логи не попадает.
fn api_key_available(mc: &ModelConfig) -> bool {
    if let Ok(raw) = std::env::var(&mc.api_key_env) {
        if !raw.trim().is_empty() {
            return true;
        }
    }
    if let Some(path) = &mc.api_key_file {
        let expanded = match path.strip_prefix("~/") {
            Some(rest) => dirs::home_dir().map_or_else(|| PathBuf::from(path), |h| h.join(rest)),
            None => PathBuf::from(path),
        };
        if let Ok(raw) = std::fs::read_to_string(&expanded) {
            if !raw.trim().is_empty() {
                return true;
            }
        }
    }
    false
}

/// Спецификации инструментов для `tools/list` (имена и аргументы — ADR-008).
// Декларативная таблица: дробление на fn-по-инструменту ухудшит обзорность.
#[expect(clippy::too_many_lines, reason = "декларативная таблица спецификаций")]
fn tool_specs() -> Vec<Value> {
    let read_only = json!({
        "readOnlyHint": true,
        "destructiveHint": false,
        "idempotentHint": true,
        "openWorldHint": false,
    });
    vec![
        json!({
            "name": "spine_lint",
            "description": "Линтер ARCHITECTURE-SPINE.md: дубли AD-id, пустые/отсутствующие \
                            Binds/Prevents/Rule, заглушки (TODO/TBD), непиннутые версии, ссылки \
                            на несуществующие AD. Verdict: passed=false (есть находки error) — \
                            spine нарушен, отказать изменению с перечнем находок",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к ARCHITECTURE-SPINE.md"}
                },
                "required": ["path"],
            },
            "annotations": read_only,
        }),
        json!({
            "name": "fitness_check",
            "description": "Fitness-контроль репозитория по CONSTRAINTS.yaml: must_contain / \
                            must_not_contain (regex по glob), file_exists, command_succeeds \
                            (с таймаутом). Вызывать ПЕРЕД коммитом: verdict passed=false — \
                            изменение нарушает архитектурные правила (AD-*), отказать и \
                            перечислить находки",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Корень репозитория"},
                    "constraints": {
                        "type": "string",
                        "description": "Путь к CONSTRAINTS.yaml (по умолчанию рабочий <repo>/CONSTRAINTS.yaml, затем пакетный <repo>/.arch-handoff/CONSTRAINTS.yaml)",
                    },
                },
                "required": ["repo"],
            },
            "annotations": read_only,
        }),
        json!({
            "name": "significance_score",
            "description": "Architecture Significance Score по 15 триггерам → маршрут изменения: \
                            Fast (0–1), Standard (2–4), Critical (5+ или критические триггеры \
                            security_boundary_change / irreversible_migration / \
                            criticality_or_exception)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "triggers": {
                        "type": "object",
                        "description": "Карта «триггер → true/false», ключи — из 15 канонических триггеров",
                        "additionalProperties": {"type": "boolean"},
                    }
                },
                "required": ["triggers"],
            },
            "annotations": read_only,
        }),
        json!({
            "name": "trace_check",
            "description": "Трассируемость как fitness-функция: покрытие звеньев REQ → NFR → \
                            AD/ADR → CMP → правило CONSTRAINTS.yaml, поимённые сироты, сверка \
                            модели с ARCHITECTURE-SPINE.md. AD без правила и без unverifiable — \
                            error. Verdict: passed=false — отказать изменению; report_markdown \
                            пригоден для evidence bundle",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "case": {"type": "string", "description": "Корень кейса (каталог с model/)"}
                },
                "required": ["case"],
            },
            "annotations": read_only,
        }),
        json!({
            "name": "model_query",
            "description": "Запрос к типизированной модели архитектуры (каталог model/): \
                            карточка сущности по id со связями и обратными ссылками, либо \
                            список сущностей (фильтр по типу: cap, sys, cmp, int, nfr, req, \
                            ad, adr, risk, owner)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "dir": {"type": "string", "description": "Каталог модели (по умолчанию model от cwd сервера)"},
                    "id": {"type": "string", "description": "ID сущности (ADR-001, CMP-002, …): карточка со связями"},
                    "type": {"type": "string", "description": "Фильтр списка по типу (cmp, adr, …)"},
                },
            },
            "annotations": read_only,
        }),
        json!({
            "name": "rubric_run",
            "description": "Оценка документа рубрикой архитектурного контроля через LLM-судью \
                            (evidence-bound, ADR-004; требует API-ключ провайдера из конфига \
                            arch — без ключа понятная JSON-RPC ошибка). Укажите ровно один из \
                            target / target_text",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "rubric": {"type": "string", "description": "Рубрика: имя в каталоге рубрик arch или путь к YAML"},
                    "target": {"type": "string", "description": "Путь к оцениваемому документу (md/txt)"},
                    "target_text": {"type": "string", "description": "Текст документа inline (альтернатива target)"},
                    "model": {"type": "string", "description": "Модель-судья (имя из [models]; по умолчанию — дефолтная)"},
                },
                "required": ["rubric"],
            },
            "annotations": {
                "readOnlyHint": true,
                "destructiveHint": false,
                "idempotentHint": false,
                "openWorldHint": true,
            },
        }),
        // T4 (ADR-015): чтение знаний наружу. Только чтение — write/exec
        // инструменты агенту НЕ отдаются (см. тест write_tools_are_not_exposed).
        json!({
            "name": "kb_search",
            "description": "Поиск по локальной базе знаний архитектора (каталоги \
                            knowledge.dirs из конфига arch): ранжированные хиты — \
                            путь, строка, сниппет с контекстом. Вызывай, когда \
                            нужен доменный материал (ADRs, заметки, статьи)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос: термины через пробел"},
                    "limit": {"type": "integer", "description": "Максимум хитов (по умолчанию 10, не больше 20)"},
                },
                "required": ["query"],
            },
            "annotations": read_only,
        }),
        json!({
            "name": "skill_search",
            "description": "Поиск по библиотеке архитектурных скиллов (плагины \
                            arch: навыки, MCP, субагенты). Вызывай, когда нужна \
                            методика по теме (ADR, saga, NFR, рубрики…)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "limit": {"type": "integer", "description": "Максимум результатов (по умолчанию 8, не больше 20)"},
                },
                "required": ["query"],
            },
            "annotations": read_only,
        }),
        json!({
            "name": "skill_load",
            "description": "Загрузить полный текст архитектурного скилла по точному \
                            имени (после skill_search). Скилл — методика: приёмы, \
                            чек-листы, антипаттерны",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Точное имя скилла"},
                },
                "required": ["name"],
            },
            "annotations": read_only,
        }),
        json!({
            "name": "mermaid_render",
            "description": "Рендерит mermaid-диаграмму (flowchart, sequenceDiagram, \
                            erDiagram или C4Context/C4Container/C4Component) в \
                            ASCII-арт. Вход: 'code' (исходник) или 'path' (путь к \
                            .mmd-файлу относительно cwd сервера)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Исходный код mermaid-диаграммы"},
                    "path": {"type": "string", "description": "Путь к .mmd-файлу (резолвится от cwd сервера)"},
                },
            },
            "annotations": read_only,
        }),
    ]
}

/// Цикл сервера поверх произвольных AsyncRead/AsyncWrite: строка → ответ
/// (или молчание на уведомление), flush на каждый ответ, выход по EOF
/// либо по ошибке чтения (транспорт мёртв — сервер завершается чисто).
async fn run_loop<R, W>(server: &McpServe, reader: R, mut writer: W) -> Result<()>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let mut lines = BufReader::new(reader).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                if line.trim().is_empty() {
                    continue; // пустые строки — не сообщения, пропускаем молча
                }
                let response = if line.len() > MAX_LINE_BYTES {
                    Some(error_response(
                        &Value::Null,
                        INVALID_REQUEST,
                        format!("строка длиннее лимита {MAX_LINE_BYTES} байт"),
                    ))
                } else {
                    server.handle_line(&line).await
                };
                if let Some(response) = response {
                    let mut payload = response.to_string();
                    payload.push('\n');
                    writer.write_all(payload.as_bytes()).await?;
                    writer.flush().await?;
                }
            }
            Ok(None) => break,
            Err(e) => {
                tracing::warn!(error = %e, "mcp-serve: ошибка чтения stdin, завершение");
                break;
            }
        }
    }
    writer.flush().await?;
    Ok(())
}

/// Точка входа `arch-ml mcp serve`: цикл на stdin/stdout процесса.
///
/// # Errors
/// Запись в stdout оборвалась (клиент умер) — сервер завершается с ошибкой
/// транспорта; входной мусор ошибкой не является (ответ `-32700` и дальше).
pub async fn serve(cfg: Arc<Config>) -> Result<()> {
    let server = McpServe::new(cfg);
    run_loop(&server, tokio::io::stdin(), tokio::io::stdout()).await
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Сервер на дефолтном конфиге (без ключей и реального дома).
    fn server() -> McpServe {
        McpServe::new(Arc::new(Config::default()))
    }

    /// Прогоняет пачку входных строк через in-memory цикл и возвращает
    /// разобранные ответы (по одному на строку вывода). Сервер — дефолтный.
    async fn run_lines(input: &[&str]) -> Vec<Value> {
        run_lines_on(server(), input).await
    }

    /// Вариант [`run_lines`] на сервере с заданным конфигом (например,
    /// с временными knowledge/plugins-каталогами).
    async fn run_lines_on(server: McpServe, input: &[&str]) -> Vec<Value> {
        // spawn требует 'static: пачка клонируется в owned-строки заранее.
        let owned: Vec<String> = input.iter().map(|s| (*s).to_string()).collect();
        let (read_end, mut write_end) = tokio::io::duplex(64 * 1024);
        let (out_read, out_write) = tokio::io::duplex(64 * 1024);
        let writer_task = tokio::spawn(async move {
            for line in owned {
                write_end.write_all(line.as_bytes()).await.expect("запись");
                write_end.write_all(b"\n").await.expect("запись nl");
            }
            // Закрытие write_end → EOF на read_end → цикл завершается.
        });
        run_loop(&server, read_end, out_write).await.expect("цикл");
        writer_task.await.expect("писатель");
        let mut responses = Vec::new();
        let mut lines = BufReader::new(out_read).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            responses.push(serde_json::from_str(&line).expect("валидный JSON ответа"));
        }
        responses
    }

    #[tokio::test]
    async fn initialize_echoes_known_protocol_and_advertises_tools() {
        for (asked, want) in [
            ("2025-06-18", "2025-06-18"),
            ("2024-11-05", "2024-11-05"),
            ("1999-01-01", "2025-06-18"),
        ] {
            let responses = run_lines(&[&format!(
                r#"{{"jsonrpc":"2.0","id":1,"method":"initialize","params":{{"protocolVersion":"{asked}","capabilities":{{}},"clientInfo":{{"name":"t","version":"0"}}}}}}"#
            )])
            .await;
            assert_eq!(responses.len(), 1);
            let result = &responses[0]["result"];
            assert_eq!(result["protocolVersion"], want, "версия для {asked}");
            assert_eq!(result["serverInfo"]["name"], "arch-ml");
            assert!(result["capabilities"]["tools"].is_object());
        }
    }

    #[tokio::test]
    async fn notifications_and_empty_lines_get_no_response() {
        let responses = run_lines(&[
            r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
            r#"{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}"#,
            r#"{"jsonrpc":"2.0","method":"unknown_notification"}"#,
            "",
            "   ",
            r#"{"jsonrpc":"2.0","id":7,"method":"ping"}"#,
        ])
        .await;
        assert_eq!(responses.len(), 1, "ответ только на ping: {responses:?}");
        assert_eq!(responses[0]["id"], 7);
        assert_eq!(responses[0]["result"], json!({}));
    }

    #[tokio::test]
    async fn broken_json_gives_32700_and_loop_continues() {
        let responses = run_lines(&[
            "{это не json",
            r#"{"jsonrpc":"2.0","id":2,"method":"ping"}"#,
        ])
        .await;
        assert_eq!(responses.len(), 2, "сервер пережил битую строку");
        assert_eq!(responses[0]["error"]["code"], PARSE_ERROR);
        assert_eq!(responses[0]["id"], Value::Null);
        assert_eq!(responses[1]["result"], json!({}));
    }

    #[tokio::test]
    async fn unknown_method_gives_32601_with_any_id_type() {
        let responses = run_lines(&[
            r#"{"jsonrpc":"2.0","id":"abc-1","method":"resources/list"}"#,
            "[1,2,3]",
            r#"{"jsonrpc":"2.0","id":3}"#,
        ])
        .await;
        assert_eq!(responses[0]["error"]["code"], METHOD_NOT_FOUND);
        assert_eq!(responses[0]["id"], "abc-1", "строковый id эхом");
        assert_eq!(responses[1]["error"]["code"], INVALID_REQUEST);
        assert_eq!(responses[2]["error"]["code"], INVALID_REQUEST);
        assert_eq!(responses[2]["id"], 3);
    }

    #[tokio::test]
    async fn tools_list_has_ten_read_only_tools() {
        let responses =
            run_lines(&[r#"{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}"#]).await;
        let tools = responses[0]["result"]["tools"].as_array().expect("tools");
        let names: Vec<&str> = tools
            .iter()
            .map(|t| t["name"].as_str().expect("имя"))
            .collect();
        assert_eq!(
            names,
            vec![
                "spine_lint",
                "fitness_check",
                "significance_score",
                "trace_check",
                "model_query",
                "rubric_run",
                "kb_search",
                "skill_search",
                "skill_load",
                "mermaid_render"
            ]
        );
        for t in tools {
            assert_eq!(t["annotations"]["readOnlyHint"], true, "{}", t["name"]);
            assert!(t["inputSchema"].is_object(), "{}", t["name"]);
        }
    }

    #[tokio::test]
    async fn write_and_exec_tools_are_not_exposed() {
        // Read-only дисциплина (T4, ADR-015): сервер отдаёт только чтение —
        // ни одного write/exec-инструмента агентного реестра наружу.
        let responses =
            run_lines(&[r#"{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}"#]).await;
        let names: Vec<&str> = responses[0]["result"]["tools"]
            .as_array()
            .expect("tools")
            .iter()
            .map(|t| t["name"].as_str().expect("имя"))
            .collect();
        for forbidden in [
            "bash",
            "write_file",
            "edit_file",
            "harness_run",
            "ralph_run",
            "subagent_run",
            "subagent_list",
            "worktree_new",
            "handoff_create",
            "agentsmd_generate",
            "web_fetch",
            "web_search",
        ] {
            assert!(
                !names.contains(&forbidden),
                "write/exec-инструмент '{forbidden}' не должен отдаваться наружу: {names:?}"
            );
        }
    }

    #[tokio::test]
    async fn unknown_tool_and_bad_arguments_give_32602() {
        let responses = run_lines(&[
            r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ghost","arguments":{}}}"#,
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"spine_lint","arguments":{}}}"#,
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fitness_check","arguments":{"repo":42}}}"#,
            r#"{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"ping"}}"#,
            r#"{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"arguments":{}}}"#,
        ])
        .await;
        for (i, r) in responses.iter().enumerate() {
            assert_eq!(r["error"]["code"], INVALID_PARAMS, "ответ {}: {r}", i + 1);
        }
        assert!(
            responses[0]["error"]["message"]
                .as_str()
                .expect("сообщение")
                .contains("ghost")
        );
    }

    #[tokio::test]
    async fn significance_score_routes_and_flags_unknown() {
        let responses = run_lines(&[
            r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"significance_score","arguments":{"triggers":{"new_component":true,"security_boundary_change":true}}}}"#,
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"significance_score","arguments":{"triggers":{}}}}"#,
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"significance_score","arguments":{"triggers":{"alien_trigger":true}}}}"#,
        ])
        .await;
        let sc = &responses[0]["result"]["structuredContent"];
        assert_eq!(sc["route"], "Critical");
        assert_eq!(sc["score"], 2);
        assert_eq!(responses[1]["result"]["structuredContent"]["route"], "Fast");
        let alien = &responses[2]["result"]["structuredContent"];
        assert_eq!(alien["unknown_triggers"], json!(["alien_trigger"]));
        // text-дубль verdict'а — валидный JSON (его разбирает клиент mcp.rs).
        let text = responses[0]["result"]["content"][0]["text"]
            .as_str()
            .expect("text");
        let parsed: Value = serde_json::from_str(text).expect("text — JSON");
        assert_eq!(parsed["route"], "Critical");
    }

    #[tokio::test]
    async fn spine_lint_verdict_marks_violations_and_clean() {
        let dir = tempfile::tempdir().expect("tmp");
        let bad = dir.path().join("BAD-SPINE.md");
        std::fs::write(
            &bad,
            "### AD-1. Брокер\n- Binds: контур\n- Prevents: хаос\n- Rule: только брокер\n\n\
             ### AD-1. Дубль\n- Binds: x\n- Prevents: y\n- Rule: z\n",
        )
        .expect("spine");
        let good = dir.path().join("GOOD-SPINE.md");
        std::fs::write(
            &good,
            "### AD-1. Брокер\n- Binds: контур\n- Prevents: хаос\n- Rule: только брокер\n",
        )
        .expect("spine");
        let call = |id: u64, path: &Path| {
            format!(
                r#"{{"jsonrpc":"2.0","id":{id},"method":"tools/call","params":{{"name":"spine_lint","arguments":{{"path":"{}"}}}}}}"#,
                path.display()
            )
        };
        let owned = [call(1, &bad), call(2, &good)];
        let refs: Vec<&str> = owned.iter().map(String::as_str).collect();
        let responses = run_lines(&refs).await;
        let bad_v = &responses[0]["result"]["structuredContent"];
        assert_eq!(bad_v["passed"], false, "{bad_v}");
        assert!(bad_v["issue_count"].as_u64().expect("число") >= 1);
        assert!(
            bad_v["issues"]
                .as_array()
                .expect("issues")
                .iter()
                .any(|i| i["rule"] == "dup_ad_id")
        );
        let good_v = &responses[1]["result"]["structuredContent"];
        assert_eq!(good_v["passed"], true, "{good_v}");
        assert_eq!(good_v["issue_count"], 0);
    }

    #[tokio::test]
    async fn domain_failure_is_is_error_result_not_crash() {
        let responses = run_lines(&[
            r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"spine_lint","arguments":{"path":"/нет/такого/spine.md"}}}"#,
            r#"{"jsonrpc":"2.0","id":2,"method":"ping"}"#,
        ])
        .await;
        assert_eq!(responses[0]["result"]["isError"], true);
        let text = responses[0]["result"]["content"][0]["text"]
            .as_str()
            .expect("text");
        assert!(text.contains("spine_lint"), "{text}");
        assert_eq!(responses[1]["result"], json!({}), "сервер жив после сбоя");
    }

    #[tokio::test]
    async fn rubric_run_validates_target_pair_and_key() {
        // Конфиг с моделью, чей ключ гарантированно отсутствует в окружении.
        let mut cfg = Config::default();
        cfg.models.insert(
            "nokey".into(),
            ModelConfig {
                base_url: "http://127.0.0.1:9".into(),
                model: "stub".into(),
                api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
                api_key_file: None,
                ..ModelConfig::default()
            },
        );
        cfg.default_model = "nokey".into();
        let server = McpServe::new(Arc::new(cfg));
        // Оба target сразу → -32602.
        let both = server
            .handle_line(
                r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"rubric_run","arguments":{"rubric":"x","target":"a.md","target_text":"текст"}}}"#,
            )
            .await
            .expect("ответ");
        assert_eq!(both["error"]["code"], INVALID_PARAMS);
        // Ни одного target → -32602.
        let none_ = server
            .handle_line(
                r#"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"rubric_run","arguments":{"rubric":"x"}}}"#,
            )
            .await
            .expect("ответ");
        assert_eq!(none_["error"]["code"], INVALID_PARAMS);
        // Ключ недоступен → понятная -32603 (ДО обращения к рубрике/LLM).
        let no_key = server
            .handle_line(
                r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"rubric_run","arguments":{"rubric":"x","target_text":"текст"}}}"#,
            )
            .await
            .expect("ответ");
        assert_eq!(no_key["error"]["code"], INTERNAL_ERROR, "{no_key}");
        let msg = no_key["error"]["message"].as_str().expect("сообщение");
        assert!(msg.contains("ARCH_HARNESS_TEST_MISSING_KEY_XYZ"), "{msg}");
        assert!(!msg.contains("sk-"), "секретов в сообщении нет: {msg}");
        // Неизвестная модель → -32602.
        let bad_model = server
            .handle_line(
                r#"{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"rubric_run","arguments":{"rubric":"x","target_text":"т","model":"ghost"}}}"#,
            )
            .await
            .expect("ответ");
        assert_eq!(bad_model["error"]["code"], INVALID_PARAMS);
    }

    /// Сервер на конфиге с временными каталогами знаний и плагинов
    /// (реального дома тест не касается).
    fn server_with_dirs(kb_dir: &Path, plugins_dir: &Path) -> McpServe {
        let mut cfg = Config::default();
        cfg.knowledge.dirs = vec![kb_dir.to_path_buf()];
        cfg.knowledge.extensions = vec!["md".into(), "txt".into()];
        cfg.plugins.dirs = vec![plugins_dir.to_path_buf()];
        McpServe::new(Arc::new(cfg))
    }

    /// Файлы тестовой базы знаний: документ про Kafka и нейтральный readme.
    fn kb_fixture(dir: &Path) {
        std::fs::write(
            dir.join("adr-042-kafka.md"),
            "# ADR-042\n\nБрокер сообщений.\nKafka выбран как шина событий.\nИтог: kafka в проде.\n",
        )
        .expect("adr");
        std::fs::write(dir.join("readme.md"), "# Общее\n\nНичего про брокеров.\n").expect("readme");
    }

    /// Плагин `mine` со скиллом `arch-core` (каталог плагина — носитель
    /// `skills/`, манифест синтезируется из имени каталога).
    fn plugin_fixture(dir: &Path) {
        let skill_md = dir.join("mine/skills/arch-core/SKILL.md");
        std::fs::create_dir_all(skill_md.parent().expect("parent")).expect("dirs");
        std::fs::write(
            &skill_md,
            "---\nname: arch-core\ndescription: Архитектурное ядро ML-платформы: каркас решений по ADR.\n---\n\
             # Arch Core\n\nМетодика принятия решений: фиксируйте ADR в каталоге model/adr.\n",
        )
        .expect("SKILL.md");
    }

    #[tokio::test]
    async fn kb_search_returns_hits_from_test_knowledge_catalog() {
        let tmp = tempfile::tempdir().expect("tmp");
        kb_fixture(tmp.path());
        let kb_dir = tmp.path().join("kb");
        std::fs::create_dir(&kb_dir).expect("kb dir");
        std::fs::write(
            kb_dir.join("notes-kafka.md"),
            "# Заметки\n\nKafka как шина событий в проде.\n",
        )
        .expect("notes");
        // Каталог знаний — только kb_dir (в tmp лежат и файлы фикстуры).
        let mut cfg = Config::default();
        cfg.knowledge.dirs = vec![kb_dir.clone()];
        cfg.knowledge.extensions = vec!["md".into()];
        let server = McpServe::new(Arc::new(cfg));
        let call = |id: u64, query: &str| {
            format!(
                r#"{{"jsonrpc":"2.0","id":{id},"method":"tools/call","params":{{"name":"kb_search","arguments":{{"query":"{query}"}}}}}}"#
            )
        };
        let responses = run_lines_on(server, &[&call(1, "kafka")]).await;
        let result = &responses[0]["result"];
        assert_eq!(result["isError"], false, "{result}");
        let sc = &result["structuredContent"];
        assert!(sc["count"].as_u64().expect("count") >= 1, "{sc}");
        let hits = sc["hits"].as_array().expect("hits");
        let top = &hits[0];
        assert!(
            top["path"]
                .as_str()
                .expect("path")
                .ends_with("notes-kafka.md"),
            "верхний хит — файл с kafka в имени: {top}"
        );
        assert!(top["score"].as_f64().expect("score") > 0.0);
        assert!(
            top["snippet"].as_str().expect("snippet").contains("Kafka"),
            "сниппет с матчем: {top}"
        );
        // Текст-дубль — валидный JSON (его разбирает клиент mcp.rs).
        let text = result["content"][0]["text"].as_str().expect("text");
        let parsed: Value = serde_json::from_str(text).expect("text — JSON");
        assert_eq!(parsed["count"], sc["count"]);
    }

    #[tokio::test]
    async fn kb_search_empty_result_is_not_an_error() {
        let tmp = tempfile::tempdir().expect("tmp");
        kb_fixture(tmp.path());
        let server = server_with_dirs(tmp.path(), tmp.path());
        let call = r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"kb_search","arguments":{"query":"никогданесуществующийтермин"}}}"#;
        let responses = run_lines_on(server, &[call]).await;
        let result = &responses[0]["result"];
        assert_eq!(result["isError"], false, "{result}");
        let sc = &result["structuredContent"];
        assert_eq!(sc["count"], 0);
        assert!(
            sc["summary"]
                .as_str()
                .expect("summary")
                .contains("ничего не найдено")
        );
    }

    #[tokio::test]
    async fn skill_search_finds_arch_core_and_load_returns_body() {
        let tmp = tempfile::tempdir().expect("tmp");
        plugin_fixture(tmp.path());
        let server = server_with_dirs(tmp.path(), tmp.path());
        let responses = run_lines_on(
            server,
            &[
                r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"skill_search","arguments":{"query":"arch"}}}"#,
                r#"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"skill_load","arguments":{"name":"arch-core"}}}"#,
                r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"skill_load","arguments":{"name":"нет-такого"}}}"#,
            ],
        )
        .await;
        // skill_search находит скилл arch-core.
        let search = &responses[0]["result"]["structuredContent"];
        let hits = search["hits"].as_array().expect("hits");
        assert!(
            hits.iter().any(|h| {
                h["name"] == "arch-core"
                    && h["plugin"] == "mine"
                    && h["score"].as_f64().expect("score") > 0.0
            }),
            "arch-core в хитах: {search}"
        );
        // skill_load отдаёт полный текст скилла.
        let loaded = &responses[1]["result"];
        assert_eq!(loaded["isError"], false, "{loaded}");
        let sc = &loaded["structuredContent"];
        assert_eq!(sc["name"], "arch-core");
        assert_eq!(sc["plugin"], "mine");
        assert!(
            sc["body"]
                .as_str()
                .expect("body")
                .contains("Методика принятия решений"),
            "тело скилла: {sc}"
        );
        // Неизвестное имя — доменная ошибка isError, а не protocol error.
        let missing = &responses[2]["result"];
        assert_eq!(missing["isError"], true, "{missing}");
        let text = missing["content"][0]["text"].as_str().expect("text");
        assert!(
            text.contains("не найден") && text.contains("нет-такого"),
            "{text}"
        );
    }

    #[tokio::test]
    async fn mermaid_render_returns_ascii_art_for_flowchart() {
        let server = server();
        let responses = run_lines_on(
            server,
            &[
                r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"mermaid_render","arguments":{"code":"graph LR\nA --> B"}}}"#,
                r#"{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mermaid_render","arguments":{}}}"#,
            ],
        )
        .await;
        let ok = &responses[0]["result"];
        assert_eq!(ok["isError"], false, "{ok}");
        let sc = &ok["structuredContent"];
        assert_eq!(sc["kind"], "flowchart");
        let art = sc["art"].as_str().expect("art");
        assert!(
            art.contains("│ A │") && art.contains("│ B │") && art.contains('▶'),
            "LR-цепочка из двух узлов:\n{art}"
        );
        assert!(
            sc["summary"]
                .as_str()
                .expect("summary")
                .contains("flowchart"),
            "{}",
            sc["summary"]
        );
        // Без code/path — вежливая доменная ошибка, не protocol error.
        let no_args = &responses[1]["result"];
        assert_eq!(no_args["isError"], true, "{no_args}");
        assert!(
            no_args["content"][0]["text"]
                .as_str()
                .expect("text")
                .contains("нужен аргумент"),
            "{no_args}"
        );
    }

    #[tokio::test]
    async fn mermaid_render_reads_mmd_file_by_path() {
        let tmp = tempfile::tempdir().expect("tmp");
        std::fs::write(tmp.path().join("flow.mmd"), "graph TD\nA --> B\n").expect("mmd");
        let server = server();
        let call = format!(
            r#"{{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{{"name":"mermaid_render","arguments":{{"path":"{}"}}}}}}"#,
            tmp.path().join("flow.mmd").display()
        );
        let responses = run_lines_on(server, &[&call]).await;
        let result = &responses[0]["result"];
        assert_eq!(result["isError"], false, "{result}");
        let sc = &result["structuredContent"];
        assert_eq!(sc["kind"], "flowchart");
        let art = sc["art"].as_str().expect("art");
        assert!(
            art.contains("│ A │") && art.contains("│ B │") && art.contains('▼'),
            "TD-цепочка из файла:\n{art}"
        );
    }
}
