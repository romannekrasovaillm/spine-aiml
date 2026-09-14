//! MCP-клиент: интеграция MCP-серверов для архитектора.
//!
//! КОНТРАКТ (владелец: агент `mcp`):
//! - формат серверов — как у Claude Code: `{"mcpServers": {name: {command,
//!   args, env}}}` ([`load_servers`]);
//! - транспорт stdio: запуск процесса (`tokio::process`), JSON-RPC 2.0 по
//!   stdin/stdout, Content-Length-less NDJSON-стиль MCP (строки JSON);
//! - handshake `initialize` (protocolVersion "2025-06-18", clientInfo
//!   arch-ml) + `notifications/initialized`;
//! - [`McpManager::tools`] — `tools/list` всех серверов, имена в формате
//!   `server__tool`; [`McpManager::call`] — `tools/call` с разбором
//!   content[] (text); таймауты из `McpSettings`;
//! - [`McpToolAdapter`] — мост MCP-инструмента в [`crate::tool::Tool`];
//! - graceful shutdown: `kill` дочерних процессов в [`McpManager::shutdown`].

use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::ExitStatus;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use async_trait::async_trait;
use futures::{StreamExt, stream};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Версия протокола MCP, запрашиваемая при handshake.
/// `pub(crate)`: её же анонсирует серверный режим (`crate::mcp_server`).
pub(crate) const PROTOCOL_VERSION: &str = "2025-06-18";
/// Запасная версия протокола (один fallback при протокольной ошибке).
pub(crate) const PROTOCOL_VERSION_FALLBACK: &str = "2024-11-05";
/// Лимит параллельных подключений/опросов серверов.
const CONNECT_CONCURRENCY: usize = 4;

/// Максимальный хвост stderr дочернего процесса, сохраняемый для
/// диагностики (ограничение на память: сервер может флудить в stderr).
const STDERR_TAIL_LIMIT: usize = 8 * 1024;

/// Отсрочка, даваемая процессу на завершение и дописывание stderr после
/// провала handshake (дальше столько же ждём сток stderr).
const STDERR_GRACE: Duration = Duration::from_millis(200);

/// Описание MCP-сервера из конфигурационного файла.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct McpServerConfig {
    /// Имя сервера (ключ в mcpServers).
    pub name: String,
    /// Команда запуска.
    pub command: String,
    /// Аргументы.
    #[serde(default)]
    pub args: Vec<String>,
    /// Окружение.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Рабочий каталог запуска. `Some(dir)` — процесс получает `chdir(dir)`
    /// до `exec`, поэтому и относительная КОМАНДА (`./bin/server`), и
    /// относительные аргументы (`./servers/x/server.py`) резолвятся от
    /// каталога объявившего манифеста, а не от cwd харнесса.
    /// `None` — наследуется cwd процесса (совместимость, in-memory тесты).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<PathBuf>,
}

/// Загружает описания серверов из `mcp.json` (формат Claude Code).
///
/// Каждому серверу выставляется `cwd = path.parent()`: относительные команды
/// и аргументы толкуются от каталога манифеста, который их объявил (тот же
/// принцип, что и у плагинов). Это ОСОЗНАННОЕ ИЗМЕНЕНИЕ ПОВЕДЕНИЯ: раньше
/// относительный путь резолвился от cwd процесса-харнесса и падал, если
/// харнесс запущен из другого каталога (симптом — таймаут initialize вместо
/// понятной ошибки, stderr дочернего процесса выбрасывался).
///
/// # Errors
/// Файл не существует (с подсказкой, как его создать), не читается
/// или содержит невалидный JSON.
pub fn load_servers(path: &Path) -> Result<Vec<McpServerConfig>> {
    let text = std::fs::read_to_string(path).map_err(|e| match e.kind() {
        std::io::ErrorKind::NotFound => HarnessError::Mcp(format!(
            "файл MCP-серверов не найден: {} — создайте ~/.arch-ml/mcp.json (см. examples/mcp.example.json)",
            path.display()
        )),
        _ => HarnessError::io(path, e),
    })?;
    let file: ServersFile = serde_json::from_str(&text)?;
    // Пустой parent (файл задан именем без каталога) — cwd не трогаем:
    // current_dir("") уронил бы spawn.
    let cwd = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(Path::to_path_buf);
    Ok(file
        .mcp_servers
        .into_iter()
        .map(|(name, entry)| McpServerConfig {
            name,
            command: entry.command,
            args: entry.args,
            env: entry.env,
            cwd: cwd.clone(),
        })
        .collect())
}

/// Корневая структура файла `mcp.json`.
#[derive(Debug, Deserialize)]
struct ServersFile {
    /// Карта «имя → описание сервера» (ключ — как у Claude Code: `mcpServers`).
    #[serde(default, rename = "mcpServers")]
    mcp_servers: BTreeMap<String, ServerEntry>,
}

/// Описание одного сервера в файле (имя задаётся ключом карты).
#[derive(Debug, Deserialize)]
struct ServerEntry {
    /// Команда запуска.
    command: String,
    /// Аргументы.
    #[serde(default)]
    args: Vec<String>,
    /// Дополнительные переменные окружения.
    #[serde(default)]
    env: BTreeMap<String, String>,
}

/// ЧИСТЫЙ рендер инвентаря MCP-серверов: НЕ запускает процессы, НЕ async.
///
/// Печатает по каждому серверу имя, команду, аргументы, РАЗРЕШЁННЫЙ рабочий
/// каталог (cwd, от которого толкуются относительные пути) и источник.
/// `source_of` — колбэк источника: пользовательский файл (`mcp.json`),
/// `плагин <имя>` и т. п.; конфиг намеренно не хранит источник, чтобы не
/// тянуть в него слои. Даёт починить `mcp list` без инициализации серверов
/// (сейчас `cmd_mcp` подключается ко всем и падает по таймауту).
#[must_use]
pub fn render_inventory(
    servers: &[McpServerConfig],
    source_of: &dyn Fn(&McpServerConfig) -> String,
) -> String {
    if servers.is_empty() {
        return "MCP-серверы не объявлены\n".to_string();
    }
    let mut out = String::new();
    let _ = writeln!(out, "MCP-серверы ({}):", servers.len());
    for server in servers {
        let args = if server.args.is_empty() {
            String::new()
        } else {
            format!(" {}", server.args.join(" "))
        };
        let _ = writeln!(out, "  {}", server.name);
        let _ = writeln!(out, "    команда:  {}{args}", server.command);
        match &server.cwd {
            Some(dir) => {
                let _ = writeln!(out, "    cwd:      {}", dir.display());
            }
            None => {
                let _ = writeln!(out, "    cwd:      — (наследуется от процесса)");
            }
        }
        let _ = writeln!(out, "    источник: {}", source_of(server));
    }
    out
}

/// Внутренняя ошибка JSON-RPC вызова; наружу конвертируется в [`HarnessError::Mcp`].
#[derive(Debug)]
enum RpcError {
    /// Сервер ответил error-объектом (код и сообщение протокола).
    Rpc {
        /// Код ошибки JSON-RPC.
        code: i64,
        /// Сообщение сервера.
        message: String,
    },
    /// Ответ не получен за отведённый таймаут.
    Timeout,
    /// Транспортная ошибка: запись в stdin или обрыв соединения.
    Transport(String),
}

impl RpcError {
    /// Конвертирует в доменную ошибку с контекстом сервера и метода.
    fn into_harness(self, server: &str, method: &str) -> HarnessError {
        match self {
            Self::Rpc { code, message } => HarnessError::Mcp(format!(
                "{server}: {method}: ошибка JSON-RPC {code}: {message}"
            )),
            Self::Timeout => HarnessError::Mcp(format!("{server}: таймаут вызова {method}")),
            Self::Transport(msg) => HarnessError::Mcp(format!("{server}: {method}: {msg}")),
        }
    }
}

/// Берёт std-мьютекс, восстанавливая guard после poisoning
/// (критические секции модуля не паникуют, poison маловероятен).
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// JSON-RPC соединение с одним MCP-сервером поверх NDJSON-транспорта
/// (одно сообщение — одна строка, без заголовков Content-Length).
struct McpConnection {
    /// Имя сервера (для диагностики).
    server: String,
    /// Таймаут одного RPC-вызова.
    timeout: Duration,
    /// Атомарный счётчик id запросов.
    next_id: AtomicU64,
    /// Ожидающие ответа запросы: id → канал результата.
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>,
    /// Писатель в транспорт (tokio-мьютекс: guard живёт через точку await записи).
    writer: tokio::sync::Mutex<Box<dyn AsyncWrite + Unpin + Send>>,
    /// Задача-читатель, раскладывающая ответы по `pending`.
    reader: JoinHandle<()>,
}

impl McpConnection {
    /// Создаёт соединение поверх произвольных AsyncRead/AsyncWrite
    /// (в бою — stdout/stdin дочернего процесса, в тестах — in-memory duplex).
    fn new<R, W>(server: String, reader: R, writer: W, timeout: Duration) -> Self
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let reader = tokio::spawn(read_loop(
            server.clone(),
            BufReader::new(reader).lines(),
            Arc::clone(&pending),
        ));
        Self {
            server,
            timeout,
            next_id: AtomicU64::new(1),
            pending,
            writer: tokio::sync::Mutex::new(Box::new(writer)),
            reader,
        }
    }

    /// Запрос-ответ: регистрирует oneshot, пишет строку, ждёт ответ с таймаутом.
    async fn request(&self, method: &str, params: Value) -> std::result::Result<Value, RpcError> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        lock(&self.pending).insert(id, tx);
        let request = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        if let Err(e) = self.write_line(&request.to_string()).await {
            lock(&self.pending).remove(&id);
            return Err(RpcError::Transport(format!("запись в транспорт: {e}")));
        }
        match tokio::time::timeout(self.timeout, rx).await {
            Ok(Ok(response)) => Self::parse_response(&response),
            Ok(Err(_closed)) => Err(RpcError::Transport("соединение закрыто сервером".into())),
            Err(_elapsed) => {
                lock(&self.pending).remove(&id);
                Err(RpcError::Timeout)
            }
        }
    }

    /// Уведомление (без id и без ожидания ответа).
    async fn notify(&self, method: &str, params: Value) -> std::result::Result<(), RpcError> {
        let notification = json!({"jsonrpc": "2.0", "method": method, "params": params});
        self.write_line(&notification.to_string())
            .await
            .map_err(|e| RpcError::Transport(format!("запись в транспорт: {e}")))
    }

    /// MCP-handshake: `initialize` с текущей версией протокола и одним
    /// fallback на старую при протокольной ошибке, затем `notifications/initialized`.
    ///
    /// # Errors
    /// Handshake отклонён обеими версиями протокола, таймаут, обрыв соединения.
    async fn handshake(&self) -> Result<()> {
        let params = |version: &str| {
            json!({
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "arch-ml", "version": "0.1.0"},
            })
        };
        if let Err(first) = self.request("initialize", params(PROTOCOL_VERSION)).await {
            // Fallback только на протокольную ошибку: таймаут/обрыв означают
            // мёртвый сервер, и повторная попытка лишь удвоит ожидание.
            if !matches!(first, RpcError::Rpc { .. }) {
                return Err(first.into_harness(&self.server, "initialize"));
            }
            tracing::debug!(
                server = %self.server,
                "mcp: протокол {PROTOCOL_VERSION} отклонён, пробуем {PROTOCOL_VERSION_FALLBACK}"
            );
            self.request("initialize", params(PROTOCOL_VERSION_FALLBACK))
                .await
                .map_err(|second| second.into_harness(&self.server, "initialize"))?;
        }
        self.notify("notifications/initialized", json!({}))
            .await
            .map_err(|e| e.into_harness(&self.server, "notifications/initialized"))
    }

    /// Пишет одно сообщение строкой (NDJSON) и сбрасывает буфер.
    async fn write_line(&self, line: &str) -> std::io::Result<()> {
        let mut writer = self.writer.lock().await;
        writer.write_all(line.as_bytes()).await?;
        writer.write_all(b"\n").await?;
        writer.flush().await
    }

    /// Разбирает ответ: error-объект → [`RpcError::Rpc`], иначе result.
    fn parse_response(response: &Value) -> std::result::Result<Value, RpcError> {
        if let Some(error) = response.get("error") {
            let code = error.get("code").and_then(Value::as_i64).unwrap_or(0);
            let message = error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("без сообщения");
            return Err(RpcError::Rpc {
                code,
                message: message.to_string(),
            });
        }
        Ok(response.get("result").cloned().unwrap_or(Value::Null))
    }
}

impl Drop for McpConnection {
    fn drop(&mut self) {
        self.reader.abort();
    }
}

/// Цикл чтения: раскладывает ответы по pending-мапе, уведомления игнорирует.
/// При обрыве чтения отправители просто дропаются — ожидающие запросы
/// завершаются ошибкой «соединение закрыто».
async fn read_loop<R: AsyncRead + Unpin>(
    server: String,
    mut lines: tokio::io::Lines<BufReader<R>>,
    pending: Arc<Mutex<HashMap<u64, oneshot::Sender<Value>>>>,
) {
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => dispatch_line(&server, &line, &pending),
            Ok(None) => {
                tracing::debug!(server = %server, "mcp: сервер закрыл stdout");
                break;
            }
            Err(e) => {
                tracing::warn!(server = %server, error = %e, "mcp: ошибка чтения, соединение завершено");
                break;
            }
        }
    }
}

/// Разбирает строку и маршрутизирует ответ по id из pending-мапы.
fn dispatch_line(server: &str, line: &str, pending: &Mutex<HashMap<u64, oneshot::Sender<Value>>>) {
    let Ok(message) = serde_json::from_str::<Value>(line) else {
        tracing::warn!(server = %server, "mcp: невалидная JSON-строка, пропущена");
        return;
    };
    // Запросы и уведомления от сервера (есть method) не поддерживаем.
    if message.get("method").is_some() {
        return;
    }
    let Some(id) = message.get("id").and_then(Value::as_u64) else {
        return;
    };
    let sender = lock(pending).remove(&id);
    if let Some(tx) = sender {
        let _ = tx.send(message);
    } else {
        tracing::debug!(server = %server, id, "mcp: ответ с неизвестным id (вероятно, после таймаута)");
    }
}

/// Подключённый MCP-сервер: соединение + дочерний процесс.
struct McpServer {
    /// Конфигурация запуска (имя, команда, аргументы).
    config: McpServerConfig,
    /// JSON-RPC соединение.
    conn: McpConnection,
    /// Дочерний процесс; изымается при shutdown/drop для kill.
    child: Mutex<Option<tokio::process::Child>>,
}

impl McpServer {
    /// Имя сервера.
    fn name(&self) -> &str {
        &self.config.name
    }

    /// Убивает дочерний процесс (идемпотентно, без ожидания выхода:
    /// zombie добирает reaper tokio).
    fn kill(&self) {
        if let Some(mut child) = lock(&self.child).take() {
            if let Err(e) = child.start_kill() {
                tracing::warn!(server = %self.config.name, error = %e, "mcp: не удалось завершить процесс");
            }
        }
    }
}

/// Запускает процесс сервера и выполняет handshake.
///
/// Относительные команда/аргументы резолвятся от `config.cwd` (каталог
/// объявившего манифеста). stderr не выбрасывается: он читается конкурентной
/// задачей в ограниченный буфер и попадает в текст ошибки при провале
/// handshake — иначе смерть дочернего процесса выглядит как «таймаут».
async fn connect_server(config: &McpServerConfig, timeout: Duration) -> Result<McpServer> {
    let mut command = tokio::process::Command::new(&config.command);
    command
        .args(&config.args)
        .envs(&config.env)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        // Подстраховка: процесс гибнет при drop Child внутри runtime.
        .kill_on_drop(true);
    if let Some(dir) = &config.cwd {
        command.current_dir(dir);
    }
    let mut child = command.spawn().map_err(|e| {
        HarnessError::Mcp(format!(
            "{}: не удалось запустить '{}': {e}",
            config.name, config.command
        ))
    })?;
    let stdout = child.stdout.take().ok_or_else(|| {
        HarnessError::Mcp(format!("{}: stdout процесса не захвачен", config.name))
    })?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| HarnessError::Mcp(format!("{}: stdin процесса не захвачен", config.name)))?;
    // stderr читаем конкурентно и сразу: если его не вычитывать, сервер
    // заблокируется на заполненной трубе. Задача живёт до EOF (гибели
    // процесса) и держит буфер через Arc.
    let stderr_tail: Arc<Mutex<String>> = Arc::new(Mutex::new(String::new()));
    let stderr_task = child.stderr.take().map(|stderr| {
        let tail = Arc::clone(&stderr_tail);
        let name = config.name.clone();
        tokio::spawn(drain_stderr(name, stderr, tail))
    });
    let conn = McpConnection::new(config.name.clone(), stdout, stdin, timeout);
    if let Err(e) = conn.handshake().await {
        return Err(handshake_failure(config, &mut child, stderr_task, &stderr_tail, e).await);
    }
    // Успех: задача-сток отцепляется (JoinHandle дропнут) и дочитывает stderr
    // до EOF; буфер больше не нужен.
    Ok(McpServer {
        config: config.clone(),
        conn,
        child: Mutex::new(Some(child)),
    })
}

/// Конкурентный сток stderr дочернего процесса в ограниченный хвост-буфер.
async fn drain_stderr<R>(server: String, stderr: R, tail: Arc<Mutex<String>>)
where
    R: AsyncRead + Unpin,
{
    let mut lines = BufReader::new(stderr).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                let mut buf = lock(&tail);
                buf.push_str(&line);
                buf.push('\n');
                trim_tail(&mut buf, STDERR_TAIL_LIMIT);
            }
            Ok(None) => break,
            Err(e) => {
                tracing::debug!(server = %server, error = %e, "mcp: ошибка чтения stderr, сток завершён");
                break;
            }
        }
    }
}

/// Оставляет в строке последние `max` байт, не разрезая UTF-8-символ.
fn trim_tail(buf: &mut String, max: usize) {
    if buf.len() <= max {
        return;
    }
    let mut cut = buf.len() - max;
    while cut < buf.len() && !buf.is_char_boundary(cut) {
        cut += 1;
    }
    buf.drain(..cut);
}

/// Формирует ошибку провала handshake с диагностикой: если дочерний процесс
/// уже завершился — код выхода и хвост stderr (это и даёт «can't open file»
/// вместо «таймаут»); если жив — прежняя ошибка плюс непустой хвост stderr.
async fn handshake_failure(
    config: &McpServerConfig,
    child: &mut tokio::process::Child,
    stderr_task: Option<JoinHandle<()>>,
    stderr_tail: &Arc<Mutex<String>>,
    err: HarnessError,
) -> HarnessError {
    let status = wait_exit(child, STDERR_GRACE).await;
    // Процесс мог умереть: даём стоку дочитать трубу до EOF.
    if let Some(task) = stderr_task {
        let _ = tokio::time::timeout(STDERR_GRACE, task).await;
    }
    let tail = lock(stderr_tail);
    let tail = tail.trim_end();
    let tail_part = if tail.is_empty() {
        String::new()
    } else {
        format!("; stderr: {tail}")
    };
    match status {
        Some(status) => HarnessError::Mcp(format!(
            "{}: процесс завершился ({status}) до handshake: {err}{tail_part}",
            config.name
        )),
        None => {
            if tail_part.is_empty() {
                err
            } else {
                HarnessError::Mcp(format!("{err}{tail_part}"))
            }
        }
    }
}

/// Ждёт завершения процесса: сначала неблокирующая проверка, затем `grace`.
/// `Child::wait` cancel-safe, поэтому брошенный по таймауту future безопасен.
async fn wait_exit(child: &mut tokio::process::Child, grace: Duration) -> Option<ExitStatus> {
    match child.try_wait() {
        Ok(Some(status)) => return Some(status),
        Ok(None) => {}
        Err(_) => return None,
    }
    match tokio::time::timeout(grace, child.wait()).await {
        Ok(Ok(status)) => Some(status),
        Ok(Err(_)) | Err(_) => None,
    }
}

/// Шаг `connect` для одного сервера: подключение + лог, сбой → `None`.
///
/// Вынесено в функцию (а не замыкание в `stream::map`): у fn-item'ов
/// лайфтаймы позднесвязанные, поэтому они «general enough» для HRTB-проверок
/// `Send` в местах вроде `tokio::spawn` — замыкание, возвращающее future,
/// захватывающий `&T`, такой проверки не проходит.
async fn connect_indexed(
    item: ((usize, &McpServerConfig), Duration),
) -> Option<(usize, McpServer)> {
    let ((idx, config), timeout) = item;
    match connect_server(config, timeout).await {
        Ok(server) => {
            tracing::info!(server = %config.name, "mcp: сервер подключён");
            Some((idx, server))
        }
        Err(e) => {
            tracing::warn!(server = %config.name, error = %e, "mcp: сервер пропущен");
            None
        }
    }
}

/// Шаг `tools` для одного сервера: `tools/list` + лог, сбой → `None`.
/// Вынесено в функцию по той же причине, что и [`connect_indexed`].
async fn list_indexed(item: (usize, &McpServer)) -> Option<(usize, Vec<ToolSpec>)> {
    let (idx, server) = item;
    match list_tools(server).await {
        Ok(specs) => Some((idx, specs)),
        Err(e) => {
            tracing::warn!(server = %server.name(), error = %e, "mcp: tools/list не удался");
            None
        }
    }
}

/// Менеджер MCP-подключений (stdio JSON-RPC).
pub struct McpManager {
    /// Подключённые серверы (в порядке конфигурации).
    servers: Vec<McpServer>,
}

impl fmt::Debug for McpManager {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("McpManager")
            .field("servers", &self.server_names())
            .finish()
    }
}

impl McpManager {
    /// Подключается ко всем серверам (initialize + initialized), не более
    /// четырёх параллельно. Сбой одного сервера не роняет остальные —
    /// пишется предупреждение в лог.
    ///
    /// # Errors
    /// Все серверы из непустого списка недоступны (пустой список — не ошибка).
    pub async fn connect(servers: &[McpServerConfig], timeout_secs: u64) -> Result<Self> {
        let timeout = Duration::from_secs(timeout_secs);
        // Таймаут пристраивается через zip/repeat, а не замыканием: любое
        // замыкание, чей результат содержит `&`-параметр, не проходит
        // HRTB-проверку «FnOnce general enough» при проверке Send в spawn.
        let items = servers.iter().enumerate().zip(std::iter::repeat(timeout));
        let mut indexed: Vec<(usize, McpServer)> = stream::iter(items)
            .map(connect_indexed)
            .buffer_unordered(CONNECT_CONCURRENCY)
            .filter_map(std::future::ready)
            .collect()
            .await;
        // buffer_unordered меняет порядок — восстанавливаем порядок конфигурации.
        indexed.sort_by_key(|(idx, _)| *idx);
        let connected: Vec<McpServer> = indexed.into_iter().map(|(_, server)| server).collect();
        if connected.is_empty() && !servers.is_empty() {
            return Err(HarnessError::Mcp(format!(
                "ни один из {} MCP-серверов не подключился (подробности — в логе)",
                servers.len()
            )));
        }
        Ok(Self { servers: connected })
    }

    /// Имена подключённых серверов.
    #[must_use]
    pub fn server_names(&self) -> Vec<String> {
        self.servers.iter().map(|s| s.config.name.clone()).collect()
    }

    /// Спецификации инструментов всех серверов (`server__tool`).
    /// Сбой опроса одного сервера — предупреждение в лог и пропуск.
    pub async fn tools(&self) -> Vec<ToolSpec> {
        let mut indexed: Vec<(usize, Vec<ToolSpec>)> =
            stream::iter(self.servers.iter().enumerate())
                .map(list_indexed)
                .buffer_unordered(CONNECT_CONCURRENCY)
                .filter_map(std::future::ready)
                .collect()
                .await;
        indexed.sort_by_key(|(idx, _)| *idx);
        indexed.into_iter().flat_map(|(_, specs)| specs).collect()
    }

    /// Вызов MCP-инструмента по составному имени `server__tool`.
    ///
    /// # Errors
    /// Сервер/инструмент не найден, ошибка RPC, таймаут.
    pub async fn call(&self, name: &str, args: Value) -> Result<ToolOutput> {
        let (server_name, tool_name) = name.split_once("__").ok_or_else(|| {
            HarnessError::Mcp(format!(
                "некорректное имя MCP-инструмента '{name}' (ожидается 'server__tool')"
            ))
        })?;
        let server = self
            .servers
            .iter()
            .find(|s| s.config.name == server_name)
            .ok_or_else(|| HarnessError::Mcp(format!("MCP-сервер '{server_name}' не подключён")))?;
        let result = server
            .conn
            .request("tools/call", json!({"name": tool_name, "arguments": args}))
            .await
            .map_err(|e| e.into_harness(server.name(), "tools/call"))?;
        Ok(parse_tool_result(&result))
    }

    /// Адаптеры MCP-инструментов как доменных [`Tool`] для регистрации
    /// в общем реестре. Спецификации кэшируются из [`McpManager::tools`].
    pub async fn tool_adapters(self: &Arc<Self>) -> Vec<Arc<dyn Tool>> {
        self.tools()
            .await
            .into_iter()
            .map(|spec| {
                Arc::new(McpToolAdapter {
                    manager: Arc::clone(self),
                    spec,
                }) as Arc<dyn Tool>
            })
            .collect()
    }

    /// Завершает дочерние процессы серверов (идемпотентно).
    // Сигнатура заморожена контрактом (async), хотя ожидания внутри нет:
    // start_kill синхронен, reaping делает reaper tokio.
    #[expect(clippy::unused_async, reason = "сигнатура заморожена контрактом")]
    // Clippy 1.98: новый линт unused_async_trait_impl срабатывает и на
    // inherent-методы (false positive, см. rust-lang/rust-clippy#16244);
    // снять expect при починке линта апстримом.
    #[expect(
        clippy::unused_async_trait_impl,
        reason = "сигнатура заморожена контрактом; линт — false positive на inherent-методе"
    )]
    pub async fn shutdown(&self) {
        for server in &self.servers {
            server.kill();
        }
    }
}

impl Drop for McpManager {
    fn drop(&mut self) {
        // Пользователь мог забыть shutdown(): добиваем процессы синхронно.
        for server in &self.servers {
            server.kill();
        }
    }
}

/// Опрашивает `tools/list` одного сервера и преобразует в [`ToolSpec`].
async fn list_tools(server: &McpServer) -> Result<Vec<ToolSpec>> {
    let result = server
        .conn
        .request("tools/list", json!({}))
        .await
        .map_err(|e| e.into_harness(server.name(), "tools/list"))?;
    let tools = result
        .get("tools")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let specs = tools
        .iter()
        .filter_map(|tool| {
            let name = tool.get("name")?.as_str()?;
            Some(ToolSpec {
                name: format!("{}__{name}", server.name()),
                description: tool
                    .get("description")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
                parameters: tool
                    .get("inputSchema")
                    .cloned()
                    .unwrap_or_else(|| json!({"type": "object", "properties": {}})),
            })
        })
        .collect();
    Ok(specs)
}

/// Разбирает результат `tools/call`: склеивает text-элементы через '\n',
/// неподдерживаемые типы помечает заглушкой, `isError` → [`ToolOutput::err`].
fn parse_tool_result(result: &Value) -> ToolOutput {
    let is_error = result
        .get("isError")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let empty = Vec::new();
    let items = result
        .get("content")
        .and_then(Value::as_array)
        .unwrap_or(&empty);
    let content = items
        .iter()
        .map(|item| {
            let kind = item
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            if kind == "text" {
                item.get("text")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string()
            } else {
                format!("[неподдерживаемый content type: {kind}]")
            }
        })
        .collect::<Vec<_>>()
        .join("\n");
    if is_error {
        ToolOutput::err(content)
    } else {
        ToolOutput::ok(content)
    }
}

/// Мост MCP-инструмента в доменный трейт [`Tool`].
///
/// Замыкает [`Arc<McpManager>`] и составное имя `server__tool`;
/// [`ToolContext`] адаптеру не нужен — контекст свободен от MCP.
#[derive(Debug)]
pub struct McpToolAdapter {
    /// Менеджер, через который выполняется вызов.
    manager: Arc<McpManager>,
    /// Кэшированная спецификация (из [`McpManager::tools`]).
    spec: ToolSpec,
}

#[async_trait]
impl Tool for McpToolAdapter {
    fn spec(&self) -> ToolSpec {
        self.spec.clone()
    }

    async fn call(&self, args: Value, _ctx: &ToolContext) -> Result<ToolOutput> {
        self.manager.call(&self.spec.name, args).await
    }
}

/// Регистрирует все MCP-инструменты менеджера в [`crate::tool::ToolRegistry`].
/// Контекст инструмента адаптеру не нужен: он замыкает [`Arc<McpManager>`].
pub async fn register_mcp_tools(
    registry: &mut crate::tool::ToolRegistry,
    manager: &Arc<McpManager>,
) {
    for tool in manager.tool_adapters().await {
        registry.register(tool);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Журнал запросов, полученных фейковым сервером.
    type RequestLog = Arc<Mutex<Vec<Value>>>;

    /// Собирает клиентское соединение на in-memory duplex-канале и запускает
    /// фейковый сервер, отвечающий по сценарию `handler`
    /// (`None` — молчать; у уведомлений нет id, ответа не ждут).
    fn conn_pair(
        handler: impl Fn(&Value) -> Option<Value> + Send + 'static,
    ) -> (McpConnection, RequestLog) {
        conn_pair_with_timeout(handler, Duration::from_secs(30))
    }

    /// Вариант [`conn_pair`] с нестандартным таймаутом соединения.
    fn conn_pair_with_timeout(
        handler: impl Fn(&Value) -> Option<Value> + Send + 'static,
        timeout: Duration,
    ) -> (McpConnection, RequestLog) {
        let (client_end, server_end) = tokio::io::duplex(16 * 1024);
        let log: RequestLog = Arc::new(Mutex::new(Vec::new()));
        let server_log = Arc::clone(&log);
        tokio::spawn(async move {
            let (reader, mut writer) = tokio::io::split(server_end);
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let Ok(request) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                lock(&server_log).push(request.clone());
                if let Some(response) = handler(&request) {
                    let mut payload = response.to_string();
                    payload.push('\n');
                    if writer.write_all(payload.as_bytes()).await.is_err() {
                        break;
                    }
                }
            }
        });
        let (reader, writer) = tokio::io::split(client_end);
        (
            McpConnection::new("test".into(), reader, writer, timeout),
            log,
        )
    }

    /// Стандартный сценарий: initialize ok, один инструмент echo,
    /// tools/call — два текстовых элемента.
    fn standard_handler(request: &Value) -> Option<Value> {
        let id = request.get("id")?.clone();
        let method = request.get("method")?.as_str()?;
        let result = match method {
            "initialize" => json!({
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "fake", "version": "0.0.1"},
            }),
            "tools/list" => json!({"tools": [{
                "name": "echo",
                "description": "эхо-инструмент",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            }]}),
            "tools/call" => json!({"content": [
                {"type": "text", "text": "привет"},
                {"type": "text", "text": "мир"},
            ]}),
            _ => return None,
        };
        Some(json!({"jsonrpc": "2.0", "id": id, "result": result}))
    }

    /// Менеджер с одним in-memory сервером `test` (без реального процесса).
    fn manager_for(
        handler: impl Fn(&Value) -> Option<Value> + Send + 'static,
    ) -> (McpManager, RequestLog) {
        manager_with_timeout(handler, Duration::from_secs(30))
    }

    /// Вариант [`manager_for`] с нестандартным таймаутом соединения.
    fn manager_with_timeout(
        handler: impl Fn(&Value) -> Option<Value> + Send + 'static,
        timeout: Duration,
    ) -> (McpManager, RequestLog) {
        let (conn, log) = conn_pair_with_timeout(handler, timeout);
        let server = McpServer {
            config: McpServerConfig {
                name: "test".into(),
                command: "unused".into(),
                args: Vec::new(),
                env: BTreeMap::new(),
                cwd: None,
            },
            conn,
            child: Mutex::new(None),
        };
        (
            McpManager {
                servers: vec![server],
            },
            log,
        )
    }

    #[tokio::test]
    async fn handshake_sends_initialize_then_initialized() {
        let (conn, log) = conn_pair(standard_handler);
        conn.handshake().await.expect("handshake");
        // Уведомление fire-and-forget: handshake завершается по записи в канал,
        // дожидаемся, пока фейковый сервер прочитает и залогирует его.
        for _ in 0..100 {
            if lock(&log).len() == 2 {
                break;
            }
            tokio::task::yield_now().await;
        }
        let log = lock(&log);
        assert_eq!(log.len(), 2);
        assert_eq!(log[0]["method"], "initialize");
        assert_eq!(log[0]["params"]["protocolVersion"], "2025-06-18");
        assert_eq!(log[0]["params"]["clientInfo"]["name"], "arch-ml");
        assert_eq!(log[1]["method"], "notifications/initialized");
        assert!(log[1].get("id").is_none(), "уведомление не должно иметь id");
    }

    #[tokio::test]
    async fn handshake_falls_back_to_older_protocol_once() {
        let handler = |request: &Value| -> Option<Value> {
            let id = request.get("id")?.clone();
            if request.get("method")?.as_str()? != "initialize" {
                return None;
            }
            let version = request["params"]["protocolVersion"].as_str()?;
            if version == "2025-06-18" {
                Some(json!({"jsonrpc": "2.0", "id": id,
                    "error": {"code": -32602, "message": "Unsupported protocol version"}}))
            } else {
                Some(json!({"jsonrpc": "2.0", "id": id, "result": {
                    "protocolVersion": version, "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "0"},
                }}))
            }
        };
        let (conn, log) = conn_pair(handler);
        conn.handshake().await.expect("handshake с fallback");
        let log = lock(&log);
        let inits: Vec<&Value> = log.iter().filter(|m| m["method"] == "initialize").collect();
        assert_eq!(inits.len(), 2, "ровно одна повторная попытка");
        assert_eq!(inits[0]["params"]["protocolVersion"], "2025-06-18");
        assert_eq!(inits[1]["params"]["protocolVersion"], "2024-11-05");
    }

    #[tokio::test]
    async fn handshake_fails_when_both_protocols_rejected() {
        let handler = |request: &Value| -> Option<Value> {
            let id = request.get("id")?.clone();
            Some(json!({"jsonrpc": "2.0", "id": id,
                "error": {"code": -32602, "message": "unsupported"}}))
        };
        let (conn, _log) = conn_pair(handler);
        let err = conn.handshake().await.expect_err("обе версии отклонены");
        assert!(matches!(err, HarnessError::Mcp(_)), "{err}");
    }

    #[tokio::test]
    async fn tools_lists_prefixed_specs() {
        let (manager, _log) = manager_for(standard_handler);
        let specs = manager.tools().await;
        assert_eq!(specs.len(), 1);
        assert_eq!(specs[0].name, "test__echo");
        assert_eq!(specs[0].description, "эхо-инструмент");
        assert_eq!(specs[0].parameters["properties"]["text"]["type"], "string");
    }

    #[tokio::test]
    async fn call_joins_text_content() {
        let (manager, _log) = manager_for(standard_handler);
        let out = manager
            .call("test__echo", json!({"text": "hi"}))
            .await
            .expect("call");
        assert!(!out.is_error);
        assert_eq!(out.content, "привет\nмир");
    }

    #[tokio::test]
    async fn call_maps_is_error_flag() {
        let handler = |request: &Value| -> Option<Value> {
            let id = request.get("id")?.clone();
            if request.get("method")?.as_str()? != "tools/call" {
                return None;
            }
            Some(json!({"jsonrpc": "2.0", "id": id, "result": {
                "isError": true,
                "content": [{"type": "text", "text": "бум"}],
            }}))
        };
        let (manager, _log) = manager_for(handler);
        let out = manager.call("test__echo", json!({})).await.expect("call");
        assert!(out.is_error);
        assert_eq!(out.content, "бум");
    }

    #[tokio::test]
    async fn call_marks_unsupported_content_types() {
        let handler = |request: &Value| -> Option<Value> {
            let id = request.get("id")?.clone();
            if request.get("method")?.as_str()? != "tools/call" {
                return None;
            }
            Some(json!({"jsonrpc": "2.0", "id": id, "result": {"content": [
                {"type": "image", "data": "aGVsbG8="},
                {"type": "text", "text": "готово"},
            ]}}))
        };
        let (manager, _log) = manager_for(handler);
        let out = manager.call("test__echo", json!({})).await.expect("call");
        assert_eq!(
            out.content,
            "[неподдерживаемый content type: image]\nготово"
        );
    }

    #[tokio::test]
    async fn call_unknown_server_and_malformed_name() {
        let (manager, _log) = manager_for(standard_handler);
        let err = manager
            .call("ghost__echo", json!({}))
            .await
            .expect_err("сервер не подключён");
        assert!(err.to_string().contains("не подключён"), "{err}");
        let err = manager
            .call("echo", json!({}))
            .await
            .expect_err("нет разделителя");
        assert!(err.to_string().contains("server__tool"), "{err}");
    }

    #[tokio::test]
    async fn call_propagates_json_rpc_error() {
        let handler = |request: &Value| -> Option<Value> {
            let id = request.get("id")?.clone();
            Some(json!({"jsonrpc": "2.0", "id": id,
                "error": {"code": -32601, "message": "Method not found"}}))
        };
        let (manager, _log) = manager_for(handler);
        let err = manager
            .call("test__echo", json!({}))
            .await
            .expect_err("ошибка протокола");
        let text = err.to_string();
        assert!(text.contains("-32601"), "{text}");
        assert!(text.contains("Method not found"), "{text}");
    }

    #[tokio::test]
    async fn call_times_out_when_server_is_silent() {
        // Сервер молчит на все запросы. Реальный таймаут 100 мс: исход
        // детерминирован — ответа не будет никогда, таймаут сработает всегда.
        // (start_paused недоступен: у tokio нет feature `test-util`.)
        let handler = |_request: &Value| -> Option<Value> { None };
        let (manager, _log) = manager_with_timeout(handler, Duration::from_millis(100));
        let err = manager
            .call("test__echo", json!({}))
            .await
            .expect_err("должен быть таймаут");
        assert!(err.to_string().contains("таймаут"), "{err}");
    }

    #[tokio::test]
    async fn adapter_delegates_call_to_manager() {
        let (manager, _log) = manager_for(standard_handler);
        let manager = Arc::new(manager);
        let adapters = manager.tool_adapters().await;
        assert_eq!(adapters.len(), 1);
        assert_eq!(adapters[0].spec().name, "test__echo");
        let ctx = ToolContext::new(
            std::path::PathBuf::from("."),
            Arc::new(crate::config::Config::default()),
        );
        let out = adapters[0]
            .call(json!({"text": "x"}), &ctx)
            .await
            .expect("adapter call");
        assert_eq!(out.content, "привет\nмир");
    }

    #[tokio::test]
    async fn manager_debug_lists_server_names() {
        let (manager, _log) = manager_for(standard_handler);
        let text = format!("{manager:?}");
        assert!(text.contains("McpManager"), "{text}");
        assert!(text.contains("test"), "{text}");
    }

    #[tokio::test]
    async fn shutdown_is_idempotent() {
        let (manager, _log) = manager_for(standard_handler);
        manager.shutdown().await;
        manager.shutdown().await;
    }

    #[tokio::test]
    async fn connect_fails_when_all_servers_fail() {
        let servers = vec![McpServerConfig {
            name: "bad".into(),
            command: "arch-ml-nonexistent-binary-xyz".into(),
            args: Vec::new(),
            env: BTreeMap::new(),
            cwd: None,
        }];
        let err = McpManager::connect(&servers, 1)
            .await
            .expect_err("все серверы упали");
        assert!(err.to_string().contains("ни один"), "{err}");
    }

    #[tokio::test]
    async fn connect_allows_empty_server_list() {
        let manager = McpManager::connect(&[], 1)
            .await
            .expect("пустой список — ок");
        assert!(manager.server_names().is_empty());
    }

    #[test]
    fn load_servers_parses_claude_code_format() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("mcp.json");
        std::fs::write(
            &path,
            r#"{
                "mcpServers": {
                    "kb": {"command": "kb-mcp", "args": ["--stdio"], "env": {"KB_DIR": "/data"}},
                    "plain": {"command": "plain-mcp"}
                }
            }"#,
        )
        .expect("write");
        let servers = load_servers(&path).expect("parse");
        assert_eq!(servers.len(), 2);
        assert_eq!(servers[0].name, "kb", "BTreeMap сортирует по имени");
        assert_eq!(servers[0].command, "kb-mcp");
        assert_eq!(servers[0].args, vec!["--stdio".to_string()]);
        assert_eq!(
            servers[0].env.get("KB_DIR").map(String::as_str),
            Some("/data")
        );
        assert_eq!(servers[1].name, "plain");
        assert!(servers[1].args.is_empty());
        assert!(servers[1].env.is_empty());
        // cwd каждого сервера — каталог объявившего манифеста, а не cwd процесса.
        assert_eq!(servers[0].cwd.as_deref(), Some(dir.path()));
        assert_eq!(servers[1].cwd.as_deref(), Some(dir.path()));
    }

    #[test]
    fn load_servers_missing_file_suggests_example() {
        let dir = tempfile::tempdir().expect("tempdir");
        let err = load_servers(&dir.path().join("нет-файла.json")).expect_err("файла нет");
        let text = err.to_string();
        assert!(matches!(err, HarnessError::Mcp(_)), "{text}");
        assert!(text.contains("~/.arch-ml/mcp.json"), "{text}");
        assert!(text.contains("examples/mcp.example.json"), "{text}");
    }

    #[test]
    fn load_servers_rejects_invalid_json() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("mcp.json");
        std::fs::write(&path, "{ это не json").expect("write");
        let err = load_servers(&path).expect_err("невалидный json");
        assert!(matches!(err, HarnessError::Json(_)), "{err}");
    }

    /// Исполняемый фейковый MCP-сервер: отвечает на initialize и tools/list.
    #[cfg(unix)]
    const FAKE_SERVER_SH: &str = r#"#!/bin/sh
while IFS= read -r line; do
  case "$line" in
    *'"method":"initialize"'*)
      id=$(printf '%s' "$line" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
      printf '{"jsonrpc":"2.0","id":%s,"result":{"protocolVersion":"2025-06-18","capabilities":{},"serverInfo":{"name":"fake","version":"0"}}}\n' "$id"
      ;;
    *'"method":"tools/list"'*)
      id=$(printf '%s' "$line" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
      printf '{"jsonrpc":"2.0","id":%s,"result":{"tools":[{"name":"echo","description":"d","inputSchema":{"type":"object"}}]}}\n' "$id"
      ;;
  esac
done
"#;

    /// Пишет фейковый сервер `name` в `dir` и делает его исполняемым.
    #[cfg(unix)]
    fn write_fake_server(dir: &Path, name: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.join(name);
        std::fs::write(&path, FAKE_SERVER_SH).expect("write fake server");
        let mut perms = std::fs::metadata(&path).expect("metadata").permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&path, perms).expect("chmod");
        path
    }

    /// ОТНОСИТЕЛЬНЫЙ АРГУМЕНТ (`./fake-server.sh`) резолвится от каталога
    /// объявившего манифеста (cwd), а не от cwd харнесса.
    #[cfg(unix)]
    #[tokio::test]
    async fn relative_arg_path_resolves_from_manifest_dir() {
        let dir = tempfile::tempdir().expect("tempdir");
        write_fake_server(dir.path(), "fake-server.sh");
        let cfg = McpServerConfig {
            name: "rel-arg".into(),
            command: "sh".into(),
            args: vec!["./fake-server.sh".into()],
            env: BTreeMap::new(),
            cwd: Some(dir.path().to_path_buf()),
        };
        let server = connect_server(&cfg, Duration::from_secs(10))
            .await
            .expect("относительный аргумент резолвится от cwd");
        let specs = list_tools(&server).await.expect("tools/list");
        assert_eq!(specs[0].name, "rel-arg__echo");
        server.kill();
    }

    /// ОТНОСИТЕЛЬНАЯ КОМАНДА (`./bin-server`) тоже резолвится от cwd.
    #[cfg(unix)]
    #[tokio::test]
    async fn relative_command_resolves_from_cwd() {
        let dir = tempfile::tempdir().expect("tempdir");
        write_fake_server(dir.path(), "bin-server");
        let cfg = McpServerConfig {
            name: "rel-cmd".into(),
            command: "./bin-server".into(),
            args: Vec::new(),
            env: BTreeMap::new(),
            cwd: Some(dir.path().to_path_buf()),
        };
        connect_server(&cfg, Duration::from_secs(10))
            .await
            .expect("относительная команда резолвится от cwd");
    }

    /// Диагностика: если процесс умирает до handshake (файла нет), ошибка
    /// несёт код выхода и хвост stderr, а не глухое «таймаут вызова initialize».
    #[cfg(unix)]
    #[tokio::test]
    async fn foreign_cwd_reports_exit_status_and_stderr_tail() {
        let dir = tempfile::tempdir().expect("tempdir");
        let cfg = McpServerConfig {
            name: "broken".into(),
            command: "sh".into(),
            args: vec!["./нет-такого-сервера.py".into()],
            env: BTreeMap::new(),
            cwd: Some(dir.path().to_path_buf()),
        };
        // `McpServer` не Debug, поэтому expect_err недоступен — разбираем вручную.
        let err = match connect_server(&cfg, Duration::from_secs(10)).await {
            Ok(server) => {
                server.kill();
                panic!("процесс должен умереть до handshake");
            }
            Err(e) => e,
        };
        let text = err.to_string();
        assert!(text.contains("завершился"), "нет кода выхода: {text}");
        assert!(text.contains("stderr"), "нет хвоста stderr: {text}");
        assert!(
            text.contains("нет-такого-сервера.py"),
            "хвост stderr не содержит имя файла: {text}"
        );
    }

    /// `render_inventory` — ЧИСТАЯ функция: не запускает процессов (даже
    /// заведомо несуществующую команду), мгновенна и печатает все поля.
    #[test]
    fn render_inventory_is_pure_and_lists_resolved_fields() {
        let servers = vec![
            McpServerConfig {
                name: "plug.fetch".into(),
                command: "arch-ml-nonexistent-binary-xyz".into(),
                args: vec!["-u".into(), "./servers/fetch/server.py".into()],
                env: BTreeMap::new(),
                cwd: Some(PathBuf::from("/opt/plugins/plug")),
            },
            McpServerConfig {
                name: "user.kb".into(),
                command: "kb-mcp".into(),
                args: Vec::new(),
                env: BTreeMap::new(),
                cwd: None,
            },
        ];
        let started = std::time::Instant::now();
        let text = render_inventory(&servers, &|s| {
            if s.name.starts_with("plug.") {
                "плагин plug".to_string()
            } else {
                "пользовательский mcp.json".to_string()
            }
        });
        assert!(
            started.elapsed() < Duration::from_secs(1),
            "рендер не должен запускать процессы"
        );
        assert!(text.contains("plug.fetch"), "{text}");
        assert!(
            text.contains("arch-ml-nonexistent-binary-xyz -u ./servers/fetch/server.py"),
            "{text}"
        );
        assert!(text.contains("/opt/plugins/plug"), "{text}");
        assert!(text.contains("наследуется"), "{text}");
        assert!(text.contains("источник: плагин plug"), "{text}");
        assert!(
            text.contains("источник: пользовательский mcp.json"),
            "{text}"
        );
    }

    #[test]
    fn render_inventory_empty_is_message() {
        let text = render_inventory(&[], &|_| "x".to_string());
        assert!(text.contains("не объявлены"), "{text}");
    }
}
