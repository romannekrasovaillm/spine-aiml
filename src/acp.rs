//! Клиент ACP (Agent Client Protocol) v1 — протокольный транспорт внешних
//! кодовых харнессов (ADR-049).
//!
//! Зачем: процессный транспорт (`run_harness`) видит харнесс как чёрный ящик
//! (stdout по итогу), а контекст между прогонами теряется. ACP поверх stdio
//! (newline-delimited JSON-RPC 2.0) даёт живые `session/update` (видимость
//! хода: текст, tool-вызовы, план, usage), `session/resume` (удержание
//! контекста между прогонами) и структурные разрешения
//! (`session/request_permission`).
//!
//! Отличие от MCP-транспорта (`crate::mcp`, чей каркас здесь повторён):
//! агент шлёт КЛИЕНТУ запросы (`session/request_permission`, `fs/*`,
//! `terminal/*`) — на них ОБЯЗАТЕЛЬНО отвечать, иначе агент дедлочится.
//! Клиент объявляет fs/terminal неподдерживаемыми; неизвестные входящие
//! запросы получают JSON-RPC ошибку `-32601` (fail-closed, без зависаний).
//!
//! Новых зависимостей нет (fitness-правило `no-new-dependencies`): tokio
//! process + `serde_json`.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::error::{HarnessError, Result};

/// Версия протокола ACP (пара initialize.protocolVersion).
const PROTOCOL_VERSION: u64 = 1;

/// JSON-RPC код «метод не найден»: ответ на входящие запросы агента,
/// которые клиент не поддерживает (fs/*, terminal/* и неизвестные).
const RPC_METHOD_NOT_FOUND: i64 = -32601;

/// Берёт std-мьютекс, восстанавливая guard после poisoning (критические
/// секции модуля не паникуют, poison маловероятен).
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Способности агента из ответа `initialize` (разбирается минимум нужного,
/// остальное доступно в `raw`).
#[derive(Debug, Clone, Default)]
pub struct AcpCapabilities {
    /// `agentCapabilities.loadSession` — `session/load` с реплеем истории.
    pub load_session: bool,
    /// `agentCapabilities.sessionCapabilities.resume` — возобновление сессии
    /// без реплея (контекст сохраняется между прогонами).
    pub resume: bool,
    /// `agentCapabilities.sessionCapabilities.close` — явное закрытие сессии.
    pub close: bool,
    /// Сырой результат initialize (диагностика и будущие способности).
    pub raw: Value,
}

impl AcpCapabilities {
    /// Разбор результата `initialize`.
    fn parse(result: &Value) -> Self {
        let caps = result.get("agentCapabilities").cloned().unwrap_or_default();
        let flag = |path: &[&str]| {
            let mut cur = &caps;
            for key in path {
                cur = cur.get(*key).unwrap_or(&Value::Null);
            }
            // Спека ACP допускает ДВЕ формы флага-способности: bool
            // (`"loadSession": true`) или объект опций (`"resume": {}` —
            // так шлёт kimi; пустой объект = «поддерживается»).
            cur.as_bool().unwrap_or_else(|| cur.is_object())
        };
        Self {
            load_session: flag(&["loadSession"]),
            resume: flag(&["sessionCapabilities", "resume"]),
            close: flag(&["sessionCapabilities", "close"]),
            raw: result.clone(),
        }
    }
}

/// Проекция `session/update`-уведомления для потребителя (лог прогона,
/// живой прогресс TUI). Сырой JSON не тащим — потребителю нужен смысл.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AcpUpdate {
    /// Кусок видимого текста ответа (`agent_message_chunk`).
    Text(String),
    /// Кусок «мыслей» агента (`agent_thought_chunk`).
    Thought(String),
    /// Tool-вызов агента (`tool_call` / `tool_call_update`).
    ToolCall {
        /// Идентификатор вызова (`toolCallId`).
        id: String,
        /// Заголовок (человекочитаемый).
        title: String,
        /// Вид (`read`/`edit`/`execute`/…; пусто — агент не прислал).
        kind: String,
        /// Статус (`pending`/`in_progress`/`completed`/`failed`).
        status: String,
    },
    /// План агента (`plan`): пункты вида «[status] content».
    Plan(Vec<String>),
    /// Метрики контекста/стоимости (`usage_update`).
    Usage {
        /// Использовано токенов контекста.
        used: Option<u64>,
        /// Размер окна контекста.
        size: Option<u64>,
        /// Стоимость, если агент её сообщил (amount+currency, как строка).
        cost: Option<String>,
        /// Токены, попавшие в кэш (нестандартное расширение: `_meta.cached_tokens`
        /// / `_meta.prompt_cache_hit_tokens`; для своих агентов, ADR-049).
        cached: Option<u64>,
    },
    /// Прочие варианты (`session_info_update` и будущие) — имя варианта.
    Other(String),
}

/// Разбирает `session/update`-уведомление в [`AcpUpdate`].
/// Не-`session/update` уведомления возвращают `None` (пропускаются).
fn map_update(method: &str, params: &Value) -> Option<AcpUpdate> {
    if method != "session/update" {
        return None;
    }
    let update = params.get("update").cloned().unwrap_or(Value::Null);
    let variant = update
        .get("sessionUpdate")
        .and_then(Value::as_str)
        .unwrap_or("");
    let content_text = |u: &Value| {
        u.pointer("/content/text")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    };
    Some(match variant {
        "agent_message_chunk" => AcpUpdate::Text(content_text(&update)),
        "agent_thought_chunk" => AcpUpdate::Thought(content_text(&update)),
        "tool_call" | "tool_call_update" => AcpUpdate::ToolCall {
            id: update
                .get("toolCallId")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            title: update
                .get("title")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            kind: update
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            status: update
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
        },
        "plan" => {
            let entries = update
                .get("entries")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .map(|e| {
                            let content =
                                e.get("content").and_then(Value::as_str).unwrap_or_default();
                            let status =
                                e.get("status").and_then(Value::as_str).unwrap_or_default();
                            format!("[{status}] {content}")
                        })
                        .collect()
                })
                .unwrap_or_default();
            AcpUpdate::Plan(entries)
        }
        "usage_update" => {
            let cost = update.get("cost").map(|c| {
                let amount = c.get("amount").and_then(Value::as_f64).unwrap_or(0.0);
                let currency = c.get("currency").and_then(Value::as_str).unwrap_or("");
                format!("{amount} {currency}")
            });
            // Кэш — нестандартное поле (свои агенты): `_meta.cached_tokens` /
            // `_meta.prompt_cache_hit_tokens`; терпим и плоскую форму.
            let meta = update.get("_meta").cloned().unwrap_or(Value::Null);
            let pick = |v: &Value| {
                ["cached_tokens", "prompt_cache_hit_tokens"]
                    .iter()
                    .find_map(|k| v.get(k).and_then(Value::as_u64))
            };
            AcpUpdate::Usage {
                used: update.get("used").and_then(Value::as_u64),
                size: update.get("size").and_then(Value::as_u64),
                cost,
                cached: pick(&meta).or_else(|| pick(&update)),
            }
        }
        other => AcpUpdate::Other(other.to_string()),
    })
}

/// Политика ответов на `session/request_permission`: агент спрашивает
/// разрешение на tool-вызов, и без ответа ждёт вечно. Политика задаётся
/// конфигом адаптера (`acp_permission`).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum PermissionPolicy {
    /// Дефолт (`readonly_auto`): читающие виды вызовов (`read`, `search`,
    /// `think`, `fetch`) разрешаются автоматически (`allow_once`), остальные
    /// (и неизвестные) отклоняются (`reject_once`).
    #[default]
    ReadonlyAuto,
    /// Всё отклонять (`reject_once`; нет reject-опций — cancelled).
    Deny,
    /// Всё разрешать (`allow_once`; нет allow-опций — cancelled).
    AllowAll,
}

impl PermissionPolicy {
    /// Разбор из конфига (`acp_permission`). Неизвестное значение — ошибка:
    /// опечатка в политике разрешений не должна молча стать чем-то другим.
    ///
    /// # Errors
    /// Незнакомое имя политики (перечень допустимых — в тексте ошибки).
    pub fn parse(raw: Option<&str>) -> Result<Self> {
        match raw {
            None | Some("readonly_auto") => Ok(Self::ReadonlyAuto),
            Some("deny") => Ok(Self::Deny),
            Some("allow_all") => Ok(Self::AllowAll),
            Some(other) => Err(HarnessError::Harness(format!(
                "acp_permission '{other}' неизвестен: допустимы readonly_auto | deny | allow_all"
            ))),
        }
    }

    /// Ответ на `params` запроса `session/request_permission`: outcome-объект
    /// (`selected` с `optionId` либо `cancelled` — если подходящей опции нет).
    pub fn answer(&self, params: &Value) -> Value {
        let allow: &[&str] = &["allow_once", "allow_always"];
        let reject: &[&str] = &["reject_once", "reject_always"];
        let kind = params
            .pointer("/toolCall/kind")
            .and_then(Value::as_str)
            .unwrap_or("");
        let want: &[&str] = match self {
            Self::AllowAll => allow,
            Self::Deny => reject,
            // Неизвестный/пустой kind НЕ автопропускаем (fail-closed → reject).
            Self::ReadonlyAuto => {
                if matches!(kind, "read" | "search" | "think" | "fetch") {
                    allow
                } else {
                    reject
                }
            }
        };
        if let Some(options) = params.get("options").and_then(Value::as_array) {
            for wanted in want {
                let hit = options.iter().find_map(|o| {
                    let matches_kind = o.get("kind").and_then(Value::as_str) == Some(*wanted);
                    let id = o.get("optionId").and_then(Value::as_str);
                    (matches_kind).then_some(id).flatten()
                });
                if let Some(id) = hit {
                    return json!({"outcome": {"outcome": "selected", "optionId": id}});
                }
            }
        }
        json!({"outcome": {"outcome": "cancelled"}})
    }
}

/// Кадр, разобранный из строки NDJSON.
enum Frame {
    /// Ответ на наш запрос (есть id, нет method).
    Response(Value),
    /// Запрос агента клиенту (есть id и method) — требует ответа.
    Request(Value),
    /// Уведомление агента (method без id).
    Notification(Value),
    /// Мусор/не-JSON — пропускаем (баннеры агента до handshake и т.п.).
    Garbage,
}

/// Классифицирует строку NDJSON.
fn classify(line: &str) -> Frame {
    let Ok(message) = serde_json::from_str::<Value>(line) else {
        return Frame::Garbage;
    };
    let has_id = message.get("id").is_some();
    let has_method = message.get("method").is_some();
    match (has_id, has_method) {
        (true, false) => Frame::Response(message),
        (true, true) => Frame::Request(message),
        (false, true) => Frame::Notification(message),
        (false, false) => Frame::Garbage,
    }
}

/// JSON-RPC соединение с ACP-агентом поверх NDJSON (одно сообщение — одна
/// строка). В бою — stdio дочернего процесса, в тестах — in-memory duplex.
pub struct AcpConnection {
    /// Атомарный счётчик id исходящих запросов.
    next_id: AtomicU64,
    /// Ожидающие ответа запросы: id → канал результата.
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>,
    /// Писатель в транспорт (общий с читателем: ответы на входящие запросы
    /// агента пишутся из задачи-читателя).
    writer: Arc<tokio::sync::Mutex<Box<dyn AsyncWrite + Unpin + Send>>>,
    /// Задача-читатель (диспетчер кадров).
    reader: JoinHandle<()>,
    /// Момент последнего ВХОДЯЩЕГО кадра — сигнал активности для
    /// idle-таймаута вызывающего.
    last_frame: Arc<Mutex<Instant>>,
}

impl AcpConnection {
    /// Создаёт соединение и запускает задачу-читатель. `updates` — канал
    /// проекций `session/update`; `policy` — ответы на запросы разрешений.
    pub fn new<R, W>(
        reader: R,
        writer: W,
        updates: mpsc::UnboundedSender<AcpUpdate>,
        policy: PermissionPolicy,
    ) -> Self
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let writer: Arc<tokio::sync::Mutex<Box<dyn AsyncWrite + Unpin + Send>>> =
            Arc::new(tokio::sync::Mutex::new(Box::new(writer)));
        let last_frame = Arc::new(Mutex::new(Instant::now()));
        let reader = tokio::spawn(read_loop(
            BufReader::new(reader).lines(),
            Arc::clone(&pending),
            Arc::clone(&writer),
            updates,
            policy,
            Arc::clone(&last_frame),
        ));
        Self {
            next_id: AtomicU64::new(1),
            pending,
            writer,
            reader,
            last_frame,
        }
    }

    /// Момент последнего входящего кадра (для idle-таймаута вызывающего).
    pub fn last_frame(&self) -> Arc<Mutex<Instant>> {
        Arc::clone(&self.last_frame)
    }

    /// Пишет одно сообщение строкой (NDJSON) и сбрасывает буфер.
    async fn write_line_to(
        writer: &tokio::sync::Mutex<Box<dyn AsyncWrite + Unpin + Send>>,
        line: &str,
    ) -> std::io::Result<()> {
        let mut w = writer.lock().await;
        w.write_all(line.as_bytes()).await?;
        w.write_all(b"\n").await?;
        w.flush().await
    }

    async fn write_line(&self, line: &str) -> std::io::Result<()> {
        Self::write_line_to(&self.writer, line).await
    }

    /// Регистрирует запрос и отправляет его; ответ придёт в возвращённый
    /// oneshot. Без таймаута: ход (`session/prompt`) длится минутами —
    /// таймауты прогона реализует вызывающий (harness).
    async fn request_start(&self, method: &str, params: Value) -> Result<oneshot::Receiver<Value>> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        lock(&self.pending).insert(id, tx);
        let request = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        if let Err(e) = self.write_line(&request.to_string()).await {
            lock(&self.pending).remove(&id);
            return Err(HarnessError::Harness(format!(
                "acp: запись запроса {method} в транспорт: {e}"
            )));
        }
        Ok(rx)
    }

    /// Запрос-ответ с таймаутом (handshake-вызовы: initialize, session/*).
    ///
    /// # Errors
    /// Таймаут, обрыв соединения, ошибка записи, JSON-RPC error от агента.
    async fn request(&self, method: &str, params: Value, timeout: Duration) -> Result<Value> {
        let rx = self.request_start(method, params).await?;
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(response)) => parse_response(method, &response),
            Ok(Err(_closed)) => Err(HarnessError::Harness(format!(
                "acp: соединение закрыто агентом до ответа на {method}"
            ))),
            Err(_elapsed) => Err(HarnessError::Harness(format!(
                "acp: таймаут {} с ожидания ответа на {method}",
                timeout.as_secs()
            ))),
        }
    }

    /// Уведомление (без id и без ожидания ответа).
    ///
    /// # Errors
    /// Ошибка записи в транспорт.
    pub async fn notify(&self, method: &str, params: Value) -> Result<()> {
        let notification = json!({"jsonrpc": "2.0", "method": method, "params": params});
        self.write_line(&notification.to_string())
            .await
            .map_err(|e| HarnessError::Harness(format!("acp: запись {method}: {e}")))
    }

    /// Handshake: `initialize` с версией протокола и возможностями клиента
    /// (fs/terminal НЕ поддерживаются — агент не должен их вызывать; если
    /// всё же вызовет, получит JSON-RPC ошибку, а не дедлок).
    ///
    /// # Errors
    /// Таймаут, обрыв соединения, протокольная ошибка.
    pub async fn initialize(&self, timeout: Duration) -> Result<AcpCapabilities> {
        let result = self
            .request(
                "initialize",
                json!({
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": false, "writeTextFile": false},
                        "terminal": false,
                    },
                    "clientInfo": {"name": "arch-ml", "version": env!("CARGO_PKG_VERSION")},
                }),
                timeout,
            )
            .await?;
        Ok(AcpCapabilities::parse(&result))
    }

    /// `session/new` — новая сессия в каталоге `cwd`; возвращает sessionId.
    ///
    /// # Errors
    /// Как у [`Self::request`].
    pub async fn session_new(&self, cwd: &str, timeout: Duration) -> Result<String> {
        let result = self
            .request(
                "session/new",
                json!({"cwd": cwd, "mcpServers": []}),
                timeout,
            )
            .await?;
        let id = result
            .get("sessionId")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if id.is_empty() {
            return Err(HarnessError::Harness(
                "acp: session/new вернул ответ без sessionId".into(),
            ));
        }
        Ok(id)
    }

    /// `session/resume` — возобновление сессии без реплея истории (контекст
    /// прошлых прогонов сохраняется). Требует `sessionCapabilities.resume` —
    /// проверка способности ДО вызова лежит на вызывающем.
    ///
    /// # Errors
    /// Как у [`Self::request`].
    pub async fn session_resume(
        &self,
        session_id: &str,
        cwd: &str,
        timeout: Duration,
    ) -> Result<()> {
        self.request(
            "session/resume",
            json!({"sessionId": session_id, "cwd": cwd, "mcpServers": []}),
            timeout,
        )
        .await?;
        Ok(())
    }

    /// `session/close` — явное закрытие сессии (best effort: ошибки закрытия
    /// вызывающий вправе проигнорировать).
    ///
    /// # Errors
    /// Как у [`Self::request`].
    pub async fn session_close(&self, session_id: &str, timeout: Duration) -> Result<()> {
        self.request("session/close", json!({"sessionId": session_id}), timeout)
            .await?;
        Ok(())
    }

    /// Старт хода: `session/prompt{sessionId, prompt:[text]}`. Ответ придёт
    /// позже — вызывающий опрашивает receiver (`try_recv`) в своём цикле
    /// таймаутов; отмена хода — [`Self::session_cancel`].
    ///
    /// # Errors
    /// Ошибка записи в транспорт.
    pub async fn prompt_start(
        &self,
        session_id: &str,
        text: &str,
    ) -> Result<oneshot::Receiver<Value>> {
        self.request_start(
            "session/prompt",
            json!({
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            }),
        )
        .await
    }

    /// `session/cancel` — уведомление об отмене текущего хода сессии.
    ///
    /// # Errors
    /// Ошибка записи в транспорт.
    pub async fn session_cancel(&self, session_id: &str) -> Result<()> {
        self.notify("session/cancel", json!({"sessionId": session_id}))
            .await
    }
}

impl Drop for AcpConnection {
    fn drop(&mut self) {
        self.reader.abort();
    }
}

/// Разбирает ответ на наш запрос: error-объект → ошибка с методом и кодом,
/// иначе — `result` (может отсутствовать → Null).
fn parse_response(method: &str, response: &Value) -> Result<Value> {
    if let Some(error) = response.get("error") {
        let code = error.get("code").and_then(Value::as_i64).unwrap_or(0);
        let message = error
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("без сообщения");
        return Err(HarnessError::Harness(format!(
            "acp: {method} отклонён агентом (код {code}): {message}"
        )));
    }
    Ok(response.get("result").cloned().unwrap_or(Value::Null))
}

/// Цикл чтения: обновляет активность, раскладывает ответы по pending-мапе,
/// отвечает на входящие запросы агента, уведомления проецирует в `updates`.
/// При обрыве чтения отправители просто дропаются — ожидающие запросы
/// завершаются ошибкой «соединение закрыто».
async fn read_loop<R: AsyncRead + Unpin>(
    mut lines: tokio::io::Lines<BufReader<R>>,
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>,
    writer: Arc<tokio::sync::Mutex<Box<dyn AsyncWrite + Unpin + Send>>>,
    updates: mpsc::UnboundedSender<AcpUpdate>,
    policy: PermissionPolicy,
    last_frame: Arc<Mutex<Instant>>,
) {
    loop {
        let line = match lines.next_line().await {
            Ok(Some(line)) => line,
            Ok(None) => {
                tracing::debug!("acp: агент закрыл stdout");
                break;
            }
            Err(e) => {
                tracing::warn!("acp: ошибка чтения, соединение завершено: {e}");
                break;
            }
        };
        *lock(&last_frame) = Instant::now();
        match classify(&line) {
            Frame::Response(message) => {
                let Some(id) = message.get("id").and_then(Value::as_u64) else {
                    continue;
                };
                if let Some(tx) = lock(&pending).remove(&id) {
                    let _ = tx.send(message);
                } else {
                    tracing::debug!(id, "acp: ответ с неизвестным id (после таймаута?)");
                }
            }
            Frame::Request(message) => {
                let id = message.get("id").cloned().unwrap_or(Value::Null);
                let method = message
                    .get("method")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                let params = message.get("params").cloned().unwrap_or(Value::Null);
                let response = if method == "session/request_permission" {
                    json!({"jsonrpc": "2.0", "id": id, "result": policy.answer(&params)})
                } else {
                    // fs/*, terminal/* и неизвестное: fail-closed ошибкой.
                    json!({"jsonrpc": "2.0", "id": id, "error": {
                        "code": RPC_METHOD_NOT_FOUND,
                        "message": format!("метод не поддерживается клиентом: {method}"),
                    }})
                };
                // Ошибка записи осознанно игнорируется: транспорт умер —
                // читатель выйдет на следующей итерации.
                let _ = AcpConnection::write_line_to(&writer, &response.to_string()).await;
            }
            Frame::Notification(message) => {
                let method = message
                    .get("method")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                let params = message.get("params").cloned().unwrap_or(Value::Null);
                if let Some(update) = map_update(method, &params) {
                    // Полный канал невозможен (unbounded); закрытый — значит
                    // потребитель ушёл, проекции больше никому не нужны.
                    let _ = updates.send(update);
                }
            }
            Frame::Garbage => {
                tracing::debug!("acp: не-JSON строка от агента, пропущена");
            }
        }
    }
}

/// Запущенный ACP-агент: дочерний процесс + соединение + stderr-диагностика.
///
/// Процесс стартует в СОБСТВЕННОЙ процессной группе (`process_group(0)`) с
/// `kill_on_drop` — как процессный транспорт `crate::harness`: обёртки плодят
/// дочерние процессы, при прерывании убивается вся группа (сирот нет).
pub struct AcpAgent {
    /// Дочерний процесс агента.
    pub child: tokio::process::Child,
    /// pid (для групповых сигналов вызывающего).
    pub pid: u32,
    /// Соединение с агентом.
    pub conn: AcpConnection,
    /// Накопленный stderr агента (диагностика — в лог прогона).
    pub stderr: Arc<Mutex<Vec<u8>>>,
    /// Задача-читатель stderr.
    stderr_reader: JoinHandle<()>,
}

impl AcpAgent {
    /// Спавнит агента из подготовленной команды (окружение/cwd собирает
    /// вызывающий — правила изоляции env едины с процессным транспортом).
    /// `label` — имя бинаря для текста ошибки «не найден».
    ///
    /// # Errors
    /// Бинарь не найден (с подсказкой по установке/конфигу), сбой запуска.
    pub fn spawn(
        mut cmd: tokio::process::Command,
        label: &str,
        harness_name: &str,
        updates: mpsc::UnboundedSender<AcpUpdate>,
        policy: PermissionPolicy,
    ) -> Result<Self> {
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0)
            .kill_on_drop(true);
        let mut child = cmd.spawn().map_err(|e| {
            if e.kind() == std::io::ErrorKind::NotFound {
                HarnessError::Harness(format!(
                    "бинарь '{label}' не найден: установите {label} или поправьте config.toml [harnesses.{harness_name}]"
                ))
            } else {
                HarnessError::Harness(format!("не удалось запустить '{label}': {e}"))
            }
        })?;
        let pid = child.id().unwrap_or(0);
        // stdin/stdout гарантированно есть: выше выставлены piped().
        let (Some(stdin), Some(stdout)) = (child.stdin.take(), child.stdout.take()) else {
            return Err(HarnessError::Harness(format!(
                "acp: не удалось открыть stdio процесса '{label}'"
            )));
        };
        let conn = AcpConnection::new(stdout, stdin, updates, policy);
        let stderr_buf: Arc<Mutex<Vec<u8>>> = Arc::new(Mutex::new(Vec::new()));
        let stderr_reader = child.stderr.take().map(|mut pipe| {
            let buf = Arc::clone(&stderr_buf);
            tokio::spawn(async move {
                let mut chunk = [0u8; 8192];
                loop {
                    match tokio::io::AsyncReadExt::read(&mut pipe, &mut chunk).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            let mut b = lock(&buf);
                            b.extend_from_slice(&chunk[..n]);
                            // Тот же лимит, что у процессного транспорта:
                            // храним хвост (диагностика в конце важнее).
                            if b.len() > 256 * 1024 {
                                let excess = b.len() - 256 * 1024;
                                b.drain(..excess);
                            }
                        }
                    }
                }
            })
        });
        Ok(Self {
            child,
            pid,
            conn,
            stderr: stderr_buf,
            // stderr без pipe быть не может (piped выше); None недостижим,
            // но не паникуем: пустая задача.
            stderr_reader: stderr_reader.unwrap_or_else(|| tokio::spawn(async {})),
        })
    }

    /// Содержимое stderr агента на текущий момент (lossy).
    pub fn stderr_text(&self) -> String {
        String::from_utf8_lossy(&lock(&self.stderr)).into_owned()
    }
}

impl Drop for AcpAgent {
    fn drop(&mut self) {
        self.stderr_reader.abort();
    }
}

/// Запись карты закреплённых сессий: sessionId + номер прогона в контексте.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct AcpSessionEntry {
    /// Идентификатор сессии, выданный агентом при `session/new`.
    pub session_id: String,
    /// Номер прогона в этом контексте (1 — сессия только что создана).
    pub runs: u64,
}

/// Сериализуемая форма файла карты (обёртка с запасом на версионирование).
#[derive(Debug, Default, serde::Serialize, serde::Deserialize)]
struct AcpSessionFile {
    /// Записи `харнесс/алиас → sessionId`.
    #[serde(default)]
    aliases: std::collections::BTreeMap<String, AcpSessionEntry>,
}

/// Карта закреплённых ACP-сессий (алиас → sessionId): продолжение контекста
/// между прогонами по ЧЕЛОВЕКОЧИТАЕМОМУ имени (ADR-049). Файл —
/// `acp-sessions.json` в state-каталоге arch-ml. Ключ записи —
/// `харнесс/алиас`: sessionId осмыслен только для выдавшего его агента,
/// одинаковые алиасы у разных харнессов не пересекаются.
///
/// Запись атомарна (tmp-файл + rename): флот гоняет прогоны параллельно,
/// полузаписанный файл не должен ломать читателей. Битый или отсутствующий
/// файл читается как пустая карта: потерять закрепление (свежая сессия)
/// безопаснее, чем отказать прогону.
#[derive(Debug, Default)]
pub struct AcpSessionStore {
    /// Путь к файлу карты.
    path: PathBuf,
    /// Записи: ключ `харнесс/алиас`.
    entries: std::collections::BTreeMap<String, AcpSessionEntry>,
}

impl AcpSessionStore {
    /// Путь карты по умолчанию: `<ARCH_ML_HOME>/state/acp-sessions.json`
    /// (рядом с `failure_memory.json` и прочим состоянием харнесса).
    #[must_use]
    pub fn default_path() -> PathBuf {
        crate::config::Config::home_dir()
            .join("state")
            .join("acp-sessions.json")
    }

    /// Загружает карту из `path`; отсутствующий или битый файл — пустая
    /// карта (см. документацию типа).
    #[must_use]
    pub fn load(path: &Path) -> Self {
        let entries = std::fs::read_to_string(path)
            .ok()
            .and_then(|text| serde_json::from_str::<AcpSessionFile>(&text).ok())
            .map(|file| file.aliases)
            .unwrap_or_default();
        Self {
            path: path.to_path_buf(),
            entries,
        }
    }

    /// Запись по харнессу и алиасу.
    #[must_use]
    pub fn get(&self, harness: &str, alias: &str) -> Option<&AcpSessionEntry> {
        self.entries.get(&format!("{harness}/{alias}"))
    }

    /// Закрепляет `session_id` за алиасом (runs — номер прогона).
    pub fn record(&mut self, harness: &str, alias: &str, session_id: &str, runs: u64) {
        self.entries.insert(
            format!("{harness}/{alias}"),
            AcpSessionEntry {
                session_id: session_id.to_string(),
                runs,
            },
        );
    }

    /// Атомарно сохраняет карту: пишет во временный файл рядом с целевым и
    /// переименовывает поверх; каталог создаётся при необходимости.
    ///
    /// # Errors
    /// Сбой создания каталога, записи или переименования (диск, права).
    pub fn save(&self) -> Result<()> {
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| {
                HarnessError::Harness(format!(
                    "acp: не создать каталог карты сессий {}: {e}",
                    dir.display()
                ))
            })?;
        }
        let file = AcpSessionFile {
            aliases: self.entries.clone(),
        };
        let text = serde_json::to_string_pretty(&file)
            .map_err(|e| HarnessError::Harness(format!("acp: сериализация карты сессий: {e}")))?;
        let tmp = self.path.with_extension("json.tmp");
        std::fs::write(&tmp, text)
            .map_err(|e| HarnessError::Harness(format!("acp: запись {}: {e}", tmp.display())))?;
        std::fs::rename(&tmp, &self.path).map_err(|e| {
            HarnessError::Harness(format!("acp: замена {}: {e}", self.path.display()))
        })?;
        Ok(())
    }
}

/// План открытия сессии ACP-прогона — результат чистой функции
/// [`plan_session_start`] (проверяем без запуска агента).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AcpSessionPlan {
    /// `session/new` без закрепления (дефолт — свежий контекст).
    Fresh,
    /// `session/new` + закрепление за алиасом (первый прогон с этим алиасом).
    PinNew,
    /// `session/resume` закреплённого sessionId; при недоступности сессии
    /// раннер открывает новую и перезакрепляет алиас.
    Resume(String),
    /// Алиас закреплён, но агент не заявил `sessionCapabilities.resume`:
    /// `session/new` + перезакрепление (контекст честно НЕ продолжается).
    NewWithoutResume,
}

/// Политика контекста ACP-прогона (ADR-049): свежая сессия — дефолт;
/// продолжение — только по имени-алиасу («возобновить-или-создать»):
/// мёртвая сессия или агент без resume — не отказ прогона, а новая сессия
/// с пометкой в логе.
pub(crate) fn plan_session_start(
    alias: Option<&str>,
    pinned: Option<&AcpSessionEntry>,
    resume_capable: bool,
) -> AcpSessionPlan {
    match (alias, pinned) {
        (None, _) => AcpSessionPlan::Fresh,
        (Some(_), None) => AcpSessionPlan::PinNew,
        (Some(_), Some(entry)) if resume_capable => {
            AcpSessionPlan::Resume(entry.session_id.clone())
        }
        (Some(_), Some(_)) => AcpSessionPlan::NewWithoutResume,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncReadExt as _;

    /// Пара «клиент ↔ тестовый сервер» на in-memory duplex: клиент — готовое
    /// соединение, сервер — единый поток (`DuplexStream` читает и пишет).
    /// Возвращает (клиент, сервер, приёмник проекций).
    fn duplex_pair(
        policy: PermissionPolicy,
    ) -> (
        AcpConnection,
        tokio::io::DuplexStream,
        mpsc::UnboundedReceiver<AcpUpdate>,
    ) {
        let (client_io, server_io) = tokio::io::duplex(64 * 1024);
        let (client_read, client_write) = tokio::io::split(client_io);
        let (tx, rx) = mpsc::unbounded_channel();
        let conn = AcpConnection::new(client_read, client_write, tx, policy);
        (conn, server_io, rx)
    }

    /// Читает одну NDJSON-строку с таймаутом (детерминизм тестов).
    async fn read_line(stream: &mut tokio::io::DuplexStream) -> Value {
        let mut buf = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            let n = tokio::time::timeout(Duration::from_secs(5), stream.read(&mut byte))
                .await
                .expect("таймаут чтения строки")
                .expect("read");
            assert!(n > 0, "соединение закрыто до конца строки");
            if byte[0] == b'\n' {
                break;
            }
            buf.push(byte[0]);
        }
        serde_json::from_slice(&buf).expect("json-строка")
    }

    /// Пишет NDJSON-строку.
    async fn write_line(stream: &mut tokio::io::DuplexStream, v: &Value) {
        let mut s = v.to_string();
        s.push('\n');
        stream.write_all(s.as_bytes()).await.expect("write");
        stream.flush().await.expect("flush");
    }

    #[tokio::test]
    async fn initialize_parses_agent_capabilities() {
        let (conn, mut server, _rx) = duplex_pair(PermissionPolicy::ReadonlyAuto);
        let server = tokio::spawn(async move {
            let req = read_line(&mut server).await;
            assert_eq!(req["method"], "initialize");
            assert_eq!(req["params"]["protocolVersion"], 1);
            // fs/terminal выключены у клиента.
            assert_eq!(req["params"]["clientCapabilities"]["terminal"], false);
            write_line(
                &mut server,
                &json!({
                    "jsonrpc": "2.0", "id": req["id"],
                    "result": {"protocolVersion": 1, "agentCapabilities": {
                        "loadSession": true,
                        // Формы спека: bool (loadSession) и объект опций
                        // (sessionCapabilities.*) — kimi шлёт `{}`.
                        "sessionCapabilities": {"resume": {}, "close": {}, "fork": false},
                    }, "authMethods": []}
                }),
            )
            .await;
        });
        let caps = conn
            .initialize(Duration::from_secs(5))
            .await
            .expect("initialize");
        assert!(caps.load_session);
        assert!(caps.resume);
        assert!(caps.close);
        server.await.expect("server");

        // Отрицательные формы: false и отсутствие — не поддерживается.
        let neg = AcpCapabilities::parse(&json!({"agentCapabilities": {
            "sessionCapabilities": {"resume": false}
        }}));
        assert!(!neg.load_session);
        assert!(!neg.resume);
        assert!(!neg.close);
    }

    #[tokio::test]
    async fn prompt_streams_updates_then_result() {
        let (conn, mut server, mut rx) = duplex_pair(PermissionPolicy::ReadonlyAuto);
        let server = tokio::spawn(async move {
            let req = read_line(&mut server).await;
            assert_eq!(req["method"], "session/prompt");
            assert_eq!(req["params"]["prompt"][0]["text"], "сделай");
            let sid = req["params"]["sessionId"].as_str().expect("sessionId");
            let notify = |u: Value| json!({"jsonrpc":"2.0","method":"session/update","params":{"sessionId": sid, "update": u}});
            write_line(&mut server, &notify(json!({"sessionUpdate":"tool_call","toolCallId":"t1","title":"Чтение","kind":"read","status":"completed"}))).await;
            write_line(&mut server, &notify(json!({"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"гото"}}))).await;
            write_line(&mut server, &notify(json!({"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"во"}}))).await;
            write_line(
                &mut server,
                &notify(json!({"sessionUpdate":"usage_update","used":1234,"size":200_000})),
            )
            .await;
            write_line(
                &mut server,
                &json!({"jsonrpc":"2.0","id":req["id"],"result":{"stopReason":"end_turn"}}),
            )
            .await;
        });
        let answer = conn.prompt_start("sess-1", "сделай").await.expect("prompt");
        let response = tokio::time::timeout(Duration::from_secs(5), answer)
            .await
            .expect("таймаут")
            .expect("канал ответа");
        assert_eq!(response["result"]["stopReason"], "end_turn");
        server.await.expect("server");

        let mut updates = Vec::new();
        while let Ok(u) = rx.try_recv() {
            updates.push(u);
        }
        assert_eq!(updates.len(), 4, "updates: {updates:?}");
        assert_eq!(
            updates[0],
            AcpUpdate::ToolCall {
                id: "t1".into(),
                title: "Чтение".into(),
                kind: "read".into(),
                status: "completed".into()
            }
        );
        assert_eq!(updates[1], AcpUpdate::Text("гото".into()));
        assert_eq!(updates[2], AcpUpdate::Text("во".into()));
        assert_eq!(
            updates[3],
            AcpUpdate::Usage {
                used: Some(1234),
                size: Some(200_000),
                cost: None,
                cached: None
            }
        );
    }

    #[tokio::test]
    async fn permission_request_is_answered_by_policy() {
        // readonly_auto: read — allow_once.
        let (conn, mut server, _rx) = duplex_pair(PermissionPolicy::ReadonlyAuto);
        let server = tokio::spawn(async move {
            let req = read_line(&mut server).await;
            assert_eq!(req["method"], "session/prompt");
            // Агент спрашивает разрешение (свой id, не из нашего пространства).
            write_line(
                &mut server,
                &json!({
                    "jsonrpc":"2.0","id":900,"method":"session/request_permission",
                    "params":{"sessionId":"s","toolCall":{"toolCallId":"t","kind":"read"},
                    "options":[
                        {"optionId":"allow","name":"Разрешить","kind":"allow_once"},
                        {"optionId":"deny","name":"Отклонить","kind":"reject_once"}]}
                }),
            )
            .await;
            let answer = read_line(&mut server).await;
            assert_eq!(answer["id"], 900);
            assert_eq!(answer["result"]["outcome"]["outcome"], "selected");
            assert_eq!(answer["result"]["outcome"]["optionId"], "allow");
            write_line(
                &mut server,
                &json!({"jsonrpc":"2.0","id":req["id"],"result":{"stopReason":"end_turn"}}),
            )
            .await;
        });
        let answer = conn.prompt_start("s", "задача").await.expect("prompt");
        let response = tokio::time::timeout(Duration::from_secs(5), answer)
            .await
            .expect("таймаут")
            .expect("канал");
        assert_eq!(response["result"]["stopReason"], "end_turn");
        server.await.expect("server");
    }

    #[tokio::test]
    async fn unknown_agent_request_gets_method_not_found() {
        // fs/read_text_file не объявлен в capabilities; если агент всё же
        // вызовет — JSON-RPC ошибка, а не дедлок.
        let (conn, mut server, _rx) = duplex_pair(PermissionPolicy::ReadonlyAuto);
        let server = tokio::spawn(async move {
            let req = read_line(&mut server).await;
            write_line(
                &mut server,
                &json!({
                    "jsonrpc":"2.0","id":901,"method":"fs/read_text_file",
                    "params":{"sessionId":"s","path":"/etc/passwd"}
                }),
            )
            .await;
            let answer = read_line(&mut server).await;
            assert_eq!(answer["id"], 901);
            assert_eq!(answer["error"]["code"], RPC_METHOD_NOT_FOUND);
            write_line(
                &mut server,
                &json!({"jsonrpc":"2.0","id":req["id"],"result":{"stopReason":"end_turn"}}),
            )
            .await;
        });
        let answer = conn.prompt_start("s", "задача").await.expect("prompt");
        tokio::time::timeout(Duration::from_secs(5), answer)
            .await
            .expect("таймаут — агент не должен висеть")
            .expect("канал");
        server.await.expect("server");
    }

    #[test]
    fn permission_policy_matrix() {
        let params = |kind: &str| {
            json!({"toolCall":{"kind":kind},"options":[
                {"optionId":"a1","kind":"allow_once"},
                {"optionId":"a2","kind":"allow_always"},
                {"optionId":"r1","kind":"reject_once"},
                {"optionId":"r2","kind":"reject_always"}]})
        };
        // readonly_auto: читающие — allow_once, прочие и неизвестные — reject.
        for kind in ["read", "search", "think", "fetch"] {
            assert_eq!(
                PermissionPolicy::ReadonlyAuto.answer(&params(kind))["outcome"]["optionId"],
                "a1",
                "kind={kind}"
            );
        }
        for kind in ["edit", "execute", "delete", "other", ""] {
            assert_eq!(
                PermissionPolicy::ReadonlyAuto.answer(&params(kind))["outcome"]["optionId"],
                "r1",
                "kind={kind}"
            );
        }
        assert_eq!(
            PermissionPolicy::AllowAll.answer(&params("edit"))["outcome"]["optionId"],
            "a1"
        );
        assert_eq!(
            PermissionPolicy::Deny.answer(&params("read"))["outcome"]["optionId"],
            "r1"
        );
        // Нет подходящих опций — cancelled.
        let no_options = json!({"toolCall":{"kind":"read"},"options":[]});
        assert_eq!(
            PermissionPolicy::AllowAll.answer(&no_options)["outcome"]["outcome"],
            "cancelled"
        );
        // Разбор из конфига: дефолт и ошибка на неизвестном значении.
        assert_eq!(
            PermissionPolicy::parse(None).expect("default"),
            PermissionPolicy::ReadonlyAuto
        );
        assert!(PermissionPolicy::parse(Some("всё-разрешить")).is_err());
    }

    #[test]
    fn update_projection_variants() {
        let p = |u: Value| json!({"sessionId":"s","update":u});
        assert_eq!(
            map_update(
                "session/update",
                &p(
                    json!({"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"думаю"}})
                )
            ),
            Some(AcpUpdate::Thought("думаю".into()))
        );
        assert_eq!(
            map_update(
                "session/update",
                &p(
                    json!({"sessionUpdate":"plan","entries":[{"content":"шаг 1","status":"in_progress"},{"content":"шаг 2","status":"pending"}]})
                )
            ),
            Some(AcpUpdate::Plan(vec![
                "[in_progress] шаг 1".into(),
                "[pending] шаг 2".into()
            ]))
        );
        assert_eq!(
            map_update(
                "session/update",
                &p(
                    json!({"sessionUpdate":"usage_update","used":10,"size":100,"cost":{"amount":0.5,"currency":"USD"}})
                )
            ),
            Some(AcpUpdate::Usage {
                used: Some(10),
                size: Some(100),
                cost: Some("0.5 USD".into()),
                cached: None
            })
        );
        // Кэш — нестандартное расширение своих агентов: `_meta.cached_tokens`
        // (и терпимые формы рядом).
        assert_eq!(
            map_update(
                "session/update",
                &p(
                    json!({"sessionUpdate":"usage_update","used":1000,"size":2000,"_meta":{"cached_tokens":800}})
                )
            ),
            Some(AcpUpdate::Usage {
                used: Some(1000),
                size: Some(2000),
                cost: None,
                cached: Some(800)
            })
        );
        assert_eq!(
            map_update(
                "session/update",
                &p(
                    json!({"sessionUpdate":"usage_update","used":1000,"prompt_cache_hit_tokens":500})
                )
            ),
            Some(AcpUpdate::Usage {
                used: Some(1000),
                size: None,
                cost: None,
                cached: Some(500)
            })
        );
        assert_eq!(
            map_update(
                "session/update",
                &p(json!({"sessionUpdate":"session_info_update"}))
            ),
            Some(AcpUpdate::Other("session_info_update".into()))
        );
        assert_eq!(map_update("notifications/initialized", &Value::Null), None);
    }

    #[test]
    fn classify_frames() {
        assert!(matches!(
            classify(r#"{"jsonrpc":"2.0","id":1,"result":{}}"#),
            Frame::Response(_)
        ));
        assert!(matches!(
            classify(r#"{"jsonrpc":"2.0","id":1,"method":"session/request_permission"}"#),
            Frame::Request(_)
        ));
        assert!(matches!(
            classify(r#"{"jsonrpc":"2.0","method":"session/update","params":{}}"#),
            Frame::Notification(_)
        ));
        assert!(matches!(classify("не json баннер"), Frame::Garbage));
        assert!(matches!(classify(r#"{"jsonrpc":"2.0"}"#), Frame::Garbage));
    }

    #[test]
    fn session_store_roundtrip_and_namespacing() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("state").join("acp-sessions.json");

        // Отсутствующий файл — пустая карта.
        let mut store = AcpSessionStore::load(&path);
        assert!(store.get("kimi", "reviewer").is_none());

        store.record("kimi", "reviewer", "sess-1", 1);
        store.record("claude", "reviewer", "sess-9", 3);
        store.save().expect("save");
        // Каталог создан, файл атомарно на месте, tmp не остался.
        assert!(path.exists());
        assert!(!path.with_extension("json.tmp").exists());

        let loaded = AcpSessionStore::load(&path);
        assert_eq!(
            loaded.get("kimi", "reviewer"),
            Some(&AcpSessionEntry {
                session_id: "sess-1".into(),
                runs: 1
            })
        );
        // Одинаковый алиас у другого харнесса — своя запись.
        assert_eq!(
            loaded.get("claude", "reviewer"),
            Some(&AcpSessionEntry {
                session_id: "sess-9".into(),
                runs: 3
            })
        );
        assert!(loaded.get("kimi", "другой").is_none());
    }

    #[test]
    fn session_store_corrupt_file_reads_empty() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("acp-sessions.json");
        std::fs::write(&path, "{не json").expect("write");
        let store = AcpSessionStore::load(&path);
        assert!(store.get("kimi", "reviewer").is_none());
    }

    #[test]
    fn session_start_policy() {
        let entry = AcpSessionEntry {
            session_id: "sess-1".into(),
            runs: 2,
        };
        // Без алиаса — всегда свежая сессия, даже если что-то закреплено.
        assert_eq!(
            plan_session_start(None, Some(&entry), true),
            AcpSessionPlan::Fresh
        );
        assert_eq!(plan_session_start(None, None, true), AcpSessionPlan::Fresh);
        // Алиас без закрепления — новая сессия + закрепление.
        assert_eq!(
            plan_session_start(Some("rev"), None, true),
            AcpSessionPlan::PinNew
        );
        // Алиас закреплён и агент умеет resume — возобновление.
        assert_eq!(
            plan_session_start(Some("rev"), Some(&entry), true),
            AcpSessionPlan::Resume("sess-1".into())
        );
        // Алиас закреплён, но агент без resume — новая сессия (честно,
        // без молчаливой потери ожидания: раннер помечает в логе).
        assert_eq!(
            plan_session_start(Some("rev"), Some(&entry), false),
            AcpSessionPlan::NewWithoutResume
        );
    }
}
