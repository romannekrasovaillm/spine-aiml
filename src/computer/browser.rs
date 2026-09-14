//! Браузерный контур: запуск Chrome и управление через CDP (ADR-041).
//!
//! Два пути управления браузером:
//! - **vision** — `screenshot` + `computer_click`/`computer_type`: работает с
//!   любым приложением, точность ограничена разрешением, которое провайдер
//!   отдаёт модели;
//! - **CDP** (этот модуль) — структурный доступ к странице: точные клики по
//!   селектору, чтение текста, ввод через `Input.insertText` (надёжная
//!   кириллица, в отличие от `xdotool type`). Требует Node.
//!
//! Транспорт CDP вынесен в Node-хелпер (`assets/computer/cdp.mjs`): так
//! харнесс не тянет websocket-крейты в контур с SBOM и `cargo audit`
//! (docs/supply-chain.md). Тот же приём уже принят для Archify.
//!
//! Безопасность: харнесс поднимает браузер с ВЫДЕЛЕННЫМ `--user-data-dir`
//! внутри `paths.state_dir` — профиль пользователя с его логинами не
//! читается и не модифицируется. Отладочный порт — только loopback.
//! `browser_eval` исполняет произвольный JS в контексте страницы, поэтому
//! классифицируется как `Destructive` наравне с вводом.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};

use super::{binary_in_path, run_argv};
use crate::config::{Config, DEFAULT_BROWSER_CANDIDATES};
use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Встроенный CDP-хелпер (разворачивается в `state_dir/computer/cdp.mjs`).
pub const CDP_SCRIPT: &str = include_str!("../../assets/computer/cdp.mjs");

/// Относительный путь хелпера внутри `state_dir`.
const CDP_SCRIPT_REL: &str = "computer/cdp.mjs";

/// Проверить URL: только http/https.
///
/// Браузер охотно откроет `file://`, `mailto:` или строку с ведущим `-`
/// (которую бинарь прочитает как флаг), поэтому схема проверяется явно, а
/// аргумент передаётся как argv — shell не участвует.
///
/// # Errors
/// Пустой URL, не-http(s) схема, ведущий дефис.
pub(crate) fn validate_http_url(url: &str) -> std::result::Result<(), String> {
    let url = url.trim();
    if url.is_empty() {
        return Err("пустой URL".into());
    }
    if url.starts_with('-') {
        return Err(format!("URL не может начинаться с «-»: {url}"));
    }
    let lower = url.to_ascii_lowercase();
    if lower.starts_with("http://") || lower.starts_with("https://") {
        return Ok(());
    }
    let scheme = url.split(':').next().unwrap_or("");
    Err(format!(
        "схема «{scheme}» не поддержана: браузерный контур открывает только http и https"
    ))
}

/// Бинарь браузера: настройка `[computer.browser].browser_bin`, иначе
/// автодетект по списку кандидатов.
///
/// # Errors
/// Ни один кандидат не найден.
pub(crate) fn browser_bin(cfg: &Config) -> Result<String> {
    let configured = cfg.computer.browser.browser_bin.trim();
    if !configured.is_empty() {
        return Ok(configured.to_string());
    }
    for candidate in DEFAULT_BROWSER_CANDIDATES {
        if binary_in_path(candidate) {
            return Ok((*candidate).to_string());
        }
    }
    Err(HarnessError::Tool(format!(
        "не найден браузер: испробованы {}. Укажите [computer.browser].browser_bin",
        DEFAULT_BROWSER_CANDIDATES.join(", ")
    )))
}

/// Каталог выделенного профиля браузера (никогда — профиль пользователя).
fn profile_dir(cfg: &Config) -> PathBuf {
    let configured = &cfg.computer.browser.user_data_dir;
    if configured.as_os_str().is_empty() {
        cfg.paths.state_dir.join("computer/chrome-profile")
    } else {
        configured.clone()
    }
}

/// Слушает ли кто-нибудь отладочный порт CDP (только loopback).
fn cdp_port_listening(port: u16) -> bool {
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_ok()
}

/// Размер окна браузера по умолчанию: headless без него отдаёт viewport
/// 780×493, и `browser_screenshot` приносит модели бесполезно мелкий кадр.
/// Значение — компромисс между детализацией интерфейса и весом base64.
const DEFAULT_WINDOW_SIZE: &str = "1280,900";

/// argv запуска браузера (чистая функция — тестируется без запуска).
#[must_use]
pub(crate) fn build_launch_argv(
    bin: &str,
    port: u16,
    profile: &Path,
    headless: bool,
    url: Option<&str>,
) -> Vec<String> {
    let mut argv = vec![
        bin.to_string(),
        format!("--remote-debugging-port={port}"),
        "--remote-debugging-address=127.0.0.1".to_string(),
        format!("--user-data-dir={}", profile.display()),
        format!("--window-size={DEFAULT_WINDOW_SIZE}"),
        "--no-first-run".to_string(),
        "--no-default-browser-check".to_string(),
    ];
    if headless {
        argv.push("--headless=new".to_string());
    }
    argv.push(url.unwrap_or("about:blank").to_string());
    argv
}

/// Развернуть CDP-хелпер из встроенной константы в `state_dir`.
///
/// Self-heal: содержимое сверяется с встроенным, поэтому обновление харнесса
/// подтягивает новую версию хелпера без `arch-ml init`.
///
/// # Errors
/// Каталог не создаётся или файл не записывается.
fn materialize_cdp_script(cfg: &Config) -> Result<PathBuf> {
    let path = cfg.paths.state_dir.join(CDP_SCRIPT_REL);
    if std::fs::read_to_string(&path).is_ok_and(|current| current == CDP_SCRIPT) {
        return Ok(path);
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| HarnessError::io(parent, e))?;
    }
    std::fs::write(&path, CDP_SCRIPT).map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

/// Убедиться, что браузер слушает CDP-порт, иначе поднять его.
///
/// Браузер запускается отсоединённым (без ожидания) — он живёт дольше вызова
/// инструмента, пользователь может за ним наблюдать. `kill_on_drop` не
/// ставится сознательно: закрывать браузер пользователя харнесс не вправе.
async fn ensure_browser(cfg: &Config, url: Option<&str>) -> Result<()> {
    let port = cfg.computer.browser.debug_port;
    if cdp_port_listening(port) {
        return Ok(());
    }
    let bin = browser_bin(cfg)?;
    let profile = profile_dir(cfg);
    std::fs::create_dir_all(&profile).map_err(|e| HarnessError::io(&profile, e))?;
    let argv = build_launch_argv(&bin, port, &profile, cfg.computer.browser.headless, url);
    let (program, args) = argv
        .split_first()
        .ok_or_else(|| HarnessError::Tool("browser: пустой argv запуска".into()))?;
    let mut command = tokio::process::Command::new(program);
    command
        .args(args)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    command.env_clear();
    let (kept, _dropped) = crate::tools::bash::scrub_env(std::env::vars(), true, &[]);
    command.envs(kept);
    command
        .spawn()
        .map_err(|e| HarnessError::Tool(format!("{bin}: не запустился: {e}")))?;
    // Ждём готовности порта: браузер поднимает CDP не мгновенно, а первый же
    // CDP-вызов после запуска иначе упал бы на «нет соединения».
    for _ in 0..40 {
        if cdp_port_listening(port) {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    Err(HarnessError::Tool(format!(
        "{bin} запущен, но CDP-порт {port} не открылся за 10 с"
    )))
}

/// Вызвать CDP-хелпер и разобрать его JSON-ответ.
async fn run_cdp(cfg: &Config, payload: Value) -> Result<Value> {
    if cfg.computer.browser.node_bin.trim().is_empty() {
        return Err(HarnessError::Tool(
            "[computer.browser].node_bin пуст — укажите исполняемый файл Node.js (>=18)".into(),
        ));
    }
    let node = cfg.computer.browser.node_bin.trim();
    if !binary_in_path(node) {
        return Err(HarnessError::Tool(format!(
            "CDP-хелперу нужен Node.js (>=18) — «{node}» не найден в PATH"
        )));
    }
    let script = materialize_cdp_script(cfg)?;
    let args = vec![
        script.to_string_lossy().to_string(),
        payload.to_string(),
        format!("--port={}", cfg.computer.browser.debug_port),
        format!(
            "--timeout-ms={}",
            cfg.computer.browser.timeout_secs.max(1) * 1000
        ),
    ];
    // Таймаут внешнего процесса — из браузерной секции, а не из общей
    // `[computer].timeout_secs` (10 с): навигация и загрузка страницы законно
    // занимают дольше, чем клик, и внешний kill убил бы её раньше, чем
    // собственный бюджет хелпера.
    let stdout = run_argv(node, &args, cfg.computer.browser.timeout_secs.max(1)).await?;
    let line = stdout.lines().last().unwrap_or("").trim();
    let parsed: Value = serde_json::from_str(line).map_err(|e| {
        HarnessError::Tool(format!("CDP-хелпер вернул не JSON: {e}; вывод: {line}"))
    })?;
    if parsed.get("ok").and_then(Value::as_bool) != Some(true) {
        let msg = parsed
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("неизвестная ошибка CDP");
        return Err(HarnessError::Tool(format!("CDP: {msg}")));
    }
    Ok(parsed)
}

/// Инструменты браузерного контура (подключаются при
/// `[computer.browser].enabled = true`).
#[must_use]
pub fn tools(_cfg: &Config) -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(OpenTool),
        Arc::new(NavigateTool),
        Arc::new(PageTool),
        Arc::new(ScreenshotTool),
        Arc::new(ClickTool),
        Arc::new(TypeTool),
        Arc::new(EvalTool),
    ]
}

/// Общая обёртка: поднять браузер (если нужно) и выполнить CDP-команду.
async fn dispatch_cdp(cfg: &Config, payload: Value) -> Result<Value> {
    ensure_browser(cfg, None).await?;
    run_cdp(cfg, payload).await
}

struct OpenTool;

#[derive(Deserialize)]
struct OpenArgs {
    #[serde(default)]
    url: Option<String>,
}

#[async_trait]
impl Tool for OpenTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "browser_open".into(),
            description: "Открыть браузер (Chrome, выделенный профиль) с отладочным протоколом и \
                          при необходимости перейти по URL. Дальше — browser_page / \
                          browser_screenshot для наблюдения и browser_click / browser_type для \
                          действий."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http(s)-адрес (необязательно)"}
                }
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: OpenArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("browser_open: аргументы: {e}")))?;
        if let Some(url) = args.url.as_deref() {
            if let Err(msg) = validate_http_url(url) {
                return Ok(ToolOutput::err(format!("browser_open: {msg}")));
            }
        }
        ensure_browser(&ctx.config, args.url.as_deref()).await?;
        let port = ctx.config.computer.browser.debug_port;
        Ok(ToolOutput::ok(match args.url {
            Some(url) => format!("браузер открыт на {url} (CDP-порт {port}, выделенный профиль)"),
            None => format!("браузер поднят, CDP-порт {port} (выделенный профиль)"),
        }))
    }
}

struct NavigateTool;

#[derive(Deserialize)]
struct NavigateArgs {
    url: String,
}

#[async_trait]
impl Tool for NavigateTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "browser_navigate".into(),
            description: "Перейти по http(s)-адресу в активной вкладке. После перехода читай \
                          browser_page (текст) или browser_screenshot (кадр)."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http(s)-адрес"}},
                "required": ["url"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: NavigateArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("browser_navigate: аргументы: {e}")))?;
        if let Err(msg) = validate_http_url(&args.url) {
            return Ok(ToolOutput::err(format!("browser_navigate: {msg}")));
        }
        let out = dispatch_cdp(&ctx.config, json!({"cmd": "nav", "url": args.url})).await?;
        let title = out.get("title").and_then(Value::as_str).unwrap_or("");
        let url = out.get("url").and_then(Value::as_str).unwrap_or(&args.url);
        Ok(ToolOutput::ok(format!(
            "переход выполнен: {url} — «{title}»"
        )))
    }
}

struct PageTool;

#[async_trait]
impl Tool for PageTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "browser_page".into(),
            description: "Текущий адрес, заголовок и видимый текст страницы. Дешевле скриншота и \
                          точнее для чтения; для визуальной проверки используй browser_screenshot."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "max_chars": {"type": "integer", "description": "Потолок символов текста (по умолчанию 8000)"}
                }
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let max_chars = args
            .get("max_chars")
            .and_then(Value::as_u64)
            .unwrap_or(8000);
        let out = dispatch_cdp(&ctx.config, json!({"cmd": "page", "maxChars": max_chars})).await?;
        let url = out.get("url").and_then(Value::as_str).unwrap_or("");
        let title = out.get("title").and_then(Value::as_str).unwrap_or("");
        let text = out.get("text").and_then(Value::as_str).unwrap_or("");
        Ok(ToolOutput::ok(format!("{url} — «{title}»\n\n{text}")))
    }
}

struct ScreenshotTool;

#[async_trait]
impl Tool for ScreenshotTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "browser_screenshot".into(),
            description: format!(
                "Кадр видимой области страницы — передаётся модели. Точнее полноэкранного \
                 screenshot: без обрамления окна. {}",
                crate::tools::screenshot::COORDINATE_HINT
            ),
            parameters: json!({"type": "object", "properties": {}}),
        }
    }

    async fn call(&self, _args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let dir = ctx.config.paths.state_dir.join("computer");
        std::fs::create_dir_all(&dir).map_err(|e| HarnessError::io(&dir, e))?;
        let path = dir.join(format!(
            "page-{}.png",
            chrono::Utc::now().timestamp_millis()
        ));
        let out = dispatch_cdp(
            &ctx.config,
            json!({"cmd": "shot", "path": path.to_string_lossy()}),
        )
        .await?;
        let bytes = std::fs::read(&path)
            .map_err(|e| HarnessError::Tool(format!("чтение кадра {}: {e}", path.display())))?;
        let image = crate::tools::screenshot::data_url_from_bytes("image/png", &bytes);
        let frame = match crate::tools::screenshot::png_dimensions(&bytes) {
            Some((w, h)) => format!("Кадр {w}×{h}. "),
            None => String::new(),
        };
        let target = out.get("target").and_then(Value::as_str).unwrap_or("");
        Ok(ToolOutput::ok(format!(
            "Кадр страницы сохранён: {}. {frame}{}",
            path.display(),
            if target.is_empty() {
                String::new()
            } else {
                format!("Браузер: {target}. ")
            }
        ))
        .with_images(vec![image]))
    }
}

struct ClickTool;

#[derive(Deserialize)]
struct ClickArgs {
    selector: String,
}

#[async_trait]
impl Tool for ClickTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "browser_click".into(),
            description:
                "Клик по CSS-селектору (точнее координатного computer_click: не зависит от \
                          разрешения). Элемент должен быть видим; после клика проверь результат \
                          через browser_page или browser_screenshot."
                    .into(),
            parameters: json!({
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS-селектор, напр. #submit или button.primary"}},
                "required": ["selector"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: ClickArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("browser_click: аргументы: {e}")))?;
        let out = dispatch_cdp(
            &ctx.config,
            json!({"cmd": "click", "selector": args.selector}),
        )
        .await?;
        let x = out.get("x").and_then(Value::as_i64).unwrap_or(0);
        let y = out.get("y").and_then(Value::as_i64).unwrap_or(0);
        Ok(ToolOutput::ok(format!(
            "клик по «{}» в точке ({x}, {y})",
            args.selector
        )))
    }
}

struct TypeTool;

#[derive(Deserialize)]
struct TypeArgs {
    text: String,
    #[serde(default)]
    selector: Option<String>,
}

#[async_trait]
impl Tool for TypeTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "browser_type".into(),
            description:
                "Ввести текст в поле (CSS-селектор необязателен — иначе в текущий фокус). \
                          Работает через CDP Input.insertText, поэтому кириллица и не-ASCII \
                          вводятся надёжно — в отличие от computer_type method=\"type\"."
                    .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст для ввода"},
                    "selector": {"type": "string", "description": "CSS-селектор поля (необязательно)"}
                },
                "required": ["text"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: TypeArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("browser_type: аргументы: {e}")))?;
        let mut payload = json!({"cmd": "type", "text": args.text});
        if let Some(selector) = &args.selector {
            payload["selector"] = json!(selector);
        }
        let out = dispatch_cdp(&ctx.config, payload).await?;
        let chars = out.get("chars").and_then(Value::as_u64).unwrap_or(0);
        Ok(ToolOutput::ok(format!(
            "введено символов: {chars}{}",
            args.selector
                .map(|s| format!(", в поле «{s}»"))
                .unwrap_or_default()
        )))
    }
}

struct EvalTool;

#[derive(Deserialize)]
struct EvalArgs {
    expression: String,
    #[serde(default)]
    wait: Option<bool>,
}

#[async_trait]
impl Tool for EvalTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "browser_eval".into(),
            description:
                "Выполнить JavaScript в контексте страницы и вернуть результат. Для чтения \
                          структурных данных, когда текста browser_page мало. Это исполнение \
                          произвольного кода на странице — инструмент деструктивного класса, \
                          применим только когда селекторов и текста не хватает."
                    .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "JS-выражение; результат сериализуется в JSON"},
                    "wait": {"type": "boolean", "description": "Дождаться Promise (по умолчанию false)"}
                },
                "required": ["expression"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: EvalArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("browser_eval: аргументы: {e}")))?;
        let out = dispatch_cdp(
            &ctx.config,
            json!({"cmd": "eval", "expression": args.expression, "await": args.wait.unwrap_or(false)}),
        )
        .await?;
        let result = out.get("result").and_then(Value::as_str).unwrap_or("null");
        Ok(ToolOutput::ok(format!("результат: {result}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_url_accepts_http_and_https_only() {
        assert!(validate_http_url("https://example.com/a?b=1").is_ok());
        assert!(validate_http_url("http://127.0.0.1:8080").is_ok());
        assert!(
            validate_http_url("  HTTPS://Example.COM  ").is_ok(),
            "регистр и пробелы"
        );
    }

    #[test]
    fn validate_url_rejects_dangerous_schemes_and_flags() {
        for bad in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "mailto:a@b.c",
            "data:text/html,<script>",
            "--headless=new",
            "-x",
            "",
            "   ",
            "example.com",
        ] {
            assert!(
                validate_http_url(bad).is_err(),
                "должен быть отвергнут: {bad:?}"
            );
        }
    }

    #[test]
    fn launch_argv_pins_loopback_port_and_dedicated_profile() {
        let argv = build_launch_argv(
            "google-chrome",
            9222,
            Path::new("/home/user/.arch-ml/state/computer/chrome-profile"),
            false,
            Some("https://example.com"),
        );
        assert_eq!(argv[0], "google-chrome");
        assert!(argv.contains(&"--remote-debugging-port=9222".to_string()));
        assert!(
            argv.contains(&"--remote-debugging-address=127.0.0.1".to_string()),
            "отладка не должна слушать внешние интерфейсы"
        );
        assert!(
            argv.iter()
                .any(|a| a.starts_with("--user-data-dir=") && a.contains("chrome-profile"))
        );
        assert_eq!(
            argv.iter()
                .find(|a| a.starts_with("--window-size="))
                .map(String::as_str),
            Some("--window-size=1280,900"),
            "headless без размера окна отдаёт бесполезно мелкий кадр"
        );
        assert_eq!(argv.last().map(String::as_str), Some("https://example.com"));
        assert!(
            !argv.iter().any(|a| a == "--headless=new"),
            "headed по умолчанию"
        );
    }

    #[test]
    fn launch_argv_headless_and_default_url() {
        let argv = build_launch_argv("chromium", 9333, Path::new("/tmp/p"), true, None);
        assert!(argv.contains(&"--headless=new".to_string()));
        assert_eq!(argv.last().map(String::as_str), Some("about:blank"));
        assert_eq!(
            argv.iter().filter(|a| a.contains("about:blank")).count(),
            1,
            "URL передаётся одним аргументом"
        );
    }

    #[test]
    fn profile_dir_defaults_under_state_dir() {
        let cfg = Config::default();
        let dir = profile_dir(&cfg);
        assert!(
            dir.starts_with(&cfg.paths.state_dir),
            "профиль обязан лежать в state_dir: {}",
            dir.display()
        );
        assert!(!dir.to_string_lossy().contains(".config/google-chrome"));
    }

    #[test]
    fn profile_dir_honours_explicit_setting() {
        let mut cfg = Config::default();
        cfg.computer.browser.user_data_dir = PathBuf::from("/tmp/explicit-profile");
        assert_eq!(profile_dir(&cfg), PathBuf::from("/tmp/explicit-profile"));
    }

    #[test]
    fn cdp_script_is_embedded_and_written_into_state_dir() {
        assert!(
            CDP_SCRIPT.contains("Input.insertText"),
            "хелпер про ввод текста"
        );
        assert!(
            CDP_SCRIPT.contains("Page.captureScreenshot"),
            "хелпер про кадр"
        );
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut cfg = Config::default();
        cfg.paths.state_dir = tmp.path().to_path_buf();
        let path = materialize_cdp_script(&cfg).expect("развёртывание хелпера");
        assert!(path.starts_with(tmp.path()), "{}", path.display());
        let written = std::fs::read_to_string(&path).expect("чтение хелпера");
        assert_eq!(written, CDP_SCRIPT);
        // Повторный вызов не перезаписывает и не падает.
        let again = materialize_cdp_script(&cfg).expect("повторное развёртывание");
        assert_eq!(again, path);
    }

    #[test]
    fn tools_expose_expected_names() {
        let names: Vec<String> = tools(&Config::default())
            .iter()
            .map(|t| t.spec().name)
            .collect();
        for expected in [
            "browser_open",
            "browser_navigate",
            "browser_page",
            "browser_screenshot",
            "browser_click",
            "browser_type",
            "browser_eval",
        ] {
            assert!(
                names.iter().any(|n| n == expected),
                "нет {expected}: {names:?}"
            );
        }
    }

    /// Живой прогон CDP: Chrome поднимает сам тест (свой PID), поэтому за
    /// собой убирает — харнесс в проде браузер не закрывает, тест обязан.
    #[tokio::test]
    #[ignore = "live: запускает headless Chrome с временным профилем и ходит по CDP"]
    async fn live_cdp_roundtrip_against_headless_chrome() {
        use std::process::Stdio;

        let bin = "google-chrome";
        if !binary_in_path(bin) {
            eprintln!("{bin} не найден — живой прогон пропущен");
            return;
        }
        let tmp = tempfile::tempdir().expect("tempdir");
        let port = 9333;
        let profile = tmp.path().join("profile");
        let argv = build_launch_argv(bin, port, &profile, true, Some("about:blank"));
        let (program, args) = argv.split_first().expect("argv непуст");
        let mut child = tokio::process::Command::new(program)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("запуск Chrome");

        let ready = {
            let mut ok = false;
            for _ in 0..40 {
                if cdp_port_listening(port) {
                    ok = true;
                    break;
                }
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
            ok
        };
        if !ready {
            let _ = child.kill().await;
            panic!("CDP-порт {port} не открылся за 10 с");
        }

        let mut cfg = Config::default();
        cfg.paths.state_dir = tmp.path().to_path_buf();
        cfg.computer.browser.debug_port = port;
        let result = run_cdp(
            &cfg,
            json!({"cmd": "eval", "expression": "document.title = 'spine-live'; document.title"}),
        )
        .await;
        let _ = child.kill().await;
        let out = result.expect("eval через CDP");
        assert_eq!(
            out.get("result").and_then(Value::as_str),
            Some("\"spine-live\""),
            "хелпер должен вернуть установленный заголовок: {out}"
        );
    }
}
