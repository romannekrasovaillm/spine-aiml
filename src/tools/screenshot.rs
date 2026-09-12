//! Инструменты работы с изображениями и наблюдения за экраном (нативная
//! мультимодальность `deepseek-flash`, формат `OpenAI` `image_url` data-URL):
//! - [`screenshot`] — снять скриншот экрана и вернуть изображение модели;
//! - [`read_image`] — прочитать файл изображения и вернуть его модели;
//! - [`screen_size`] — размер корневого окна X11 и подсказка координат;
//! - [`window_list`] — видимые окна (id, заголовок) для адресации.
//!
//! Изображение возвращается в [`ToolOutput::images`]; агентный цикл
//! прикрепляет его к user-сообщению (см. `agent.rs`), после чего модель
//! распознаёт/извлекает данные по скриншоту.
//!
//! Наблюдение (эти инструменты) включено всегда и живёт в ядре: видеть экран
//! безопасно. Воздействие — инструменты `computer_*`/`browser_*` — вынесено в
//! доменный модуль [`crate::computer`], гейтится конфигом и классом риска
//! (ADR-041). Единственное, что связывает контуры, — координатный контракт:
//! модель получает размер кадра здесь и называет координаты в нормализованной
//! шкале 0–1000, которую [`crate::computer`] переводит в пиксели.

use std::path::Path;
use std::sync::Arc;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Кандидаты команд скриншота (первый успешный — рабочий).
/// `{path}` — целевой файл, `{display}` — DISPLAY (для `ffmpeg` x11grab).
/// `ffmpeg` — надёжный fallback (уже стоит в ML-контурах); `scrot`/`import` —
/// легче, но требуют установки.
const SCREENSHOT_CANDIDATES: &[&[&str]] = &[
    &["scrot", "{path}"],
    &["import", "-window", "root", "{path}"],
    &["gnome-screenshot", "-f", "{path}"],
    &[
        "ffmpeg",
        "-y",
        "-f",
        "x11grab",
        "-i",
        "{display}",
        "-frames:v",
        "1",
        "{path}",
    ],
];

/// Инструмент `screenshot`: снять скриншот и вернуть его модели.
struct ScreenshotTool;

#[derive(Deserialize)]
struct ScreenshotArgs {
    /// Куда сохранить файл (без него — `state/screenshots/shot-<ts>.png`).
    #[serde(default)]
    path: Option<String>,
}

/// Инструмент `read_image`: прочитать изображение и вернуть его модели.
struct ReadImageTool;

#[derive(Deserialize)]
struct ReadImageArgs {
    /// Путь к файлу изображения (png/jpg/webp/gif).
    path: String,
}

/// Инструмент `screen_size`: размер корневого окна X11.
struct ScreenSizeTool;

/// Инструмент `window_list`: видимые окна (id + заголовок).
struct WindowListTool;

/// Инструменты домена изображений (регистрируются в ядре).
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(ScreenshotTool),
        Arc::new(ReadImageTool),
        Arc::new(ScreenSizeTool),
        Arc::new(WindowListTool),
    ]
}

/// Подсказка координатного контракта — дословно одинаковая у всех
/// инструментов наблюдения и у описаний `computer_*` (ADR-041): модель
/// узнаёт контракт только из текстов, системный промпт о нём не знает.
pub(crate) const COORDINATE_HINT: &str = "Координаты задаются НОРМАЛИЗОВАННЫМИ 0–1000 \
     по каждой оси (не пиксели), начало — левый верхний угол кадра: провайдер ужимает кадр \
     перед инференсом, поэтому пиксельная шкала к экрану не привязана.";

#[async_trait]
impl Tool for ScreenshotTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "screenshot".into(),
            description: "Снять скриншот экрана и передать изображение модели для распознавания/извлечения данных. Наблюдение за интерфейсом: смотри кадр, затем действуй (computer_*) и снимай НОВЫЙ скриншот для проверки результата — не предполагай результат действия. Результат сообщает размер кадра; координаты для computer_* нормализованные 0–1000.".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Куда сохранить файл (необязательно; по умолчанию во временный каталог скриншотов)"}
                }
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: ScreenshotArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("screenshot: невалидные аргументы: {e}")))?;
        let path = if let Some(p) = args.path {
            ctx.resolve(p)
        } else {
            let dir = ctx.config.paths.state_dir.join("screenshots");
            std::fs::create_dir_all(&dir).map_err(|e| {
                HarnessError::Tool(format!("screenshot: каталог {}: {e}", dir.display()))
            })?;
            dir.join(format!("shot-{}.png", chrono::Utc::now().timestamp()))
        };
        capture_screenshot(&path)?;
        let bytes = std::fs::read(&path)
            .map_err(|e| HarnessError::Tool(format!("чтение скриншота {}: {e}", path.display())))?;
        let image = data_url_from_bytes(mime_from_ext(&path), &bytes);
        // Размер кадра — часть координатного контракта: без него модель не
        // знает, к чему относятся её нормализованные координаты (ADR-041).
        let frame = match png_dimensions(&bytes) {
            Some((w, h)) => format!("Кадр {w}×{h}. "),
            None => String::new(),
        };
        Ok(ToolOutput::ok(format!(
            "Скриншот сохранён: {}. {frame}{COORDINATE_HINT}",
            path.display()
        ))
        .with_images(vec![image]))
    }
}

#[async_trait]
impl Tool for ReadImageTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "read_image".into(),
            description: "Прочитать файл изображения (png/jpg/webp/gif) и передать его модели для распознавания текста/структуры/данных.".into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу изображения"}
                },
                "required": ["path"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: ReadImageArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("read_image: невалидные аргументы: {e}")))?;
        let path = ctx.resolve(&args.path);
        let image = image_data_url(&path)?;
        Ok(
            ToolOutput::ok(format!("Изображение прочитано: {}", path.display()))
                .with_images(vec![image]),
        )
    }
}

#[async_trait]
impl Tool for ScreenSizeTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "screen_size".into(),
            description: "Размер корневого окна X11 в пикселях. Зови перед первым computer_*, \
                          если нужно перевести нормализованные координаты в пиксели вручную."
                .into(),
            parameters: json!({"type": "object", "properties": {}}),
        }
    }

    async fn call(&self, _args: Value, _ctx: &ToolContext) -> Result<ToolOutput> {
        let (w, h) = screen_size()?;
        Ok(ToolOutput::ok(format!(
            "Экран {w}×{h} пикселей. {COORDINATE_HINT}"
        )))
    }
}

#[async_trait]
impl Tool for WindowListTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "window_list".into(),
            description: "Список видимых окон X11 (id и заголовок). Нужен, чтобы понять, какое \
                          окно активно и к какому адресованы действия."
                .into(),
            parameters: json!({"type": "object", "properties": {}}),
        }
    }

    async fn call(&self, _args: Value, _ctx: &ToolContext) -> Result<ToolOutput> {
        Ok(ToolOutput::ok(window_list()?))
    }
}

/// Размер PNG из заголовка `IHDR` (без внешних крейтов).
///
/// Раскладка: 0..8 — сигнатура, 8..12 — длина IHDR, 12..16 — `IHDR`,
/// 16..20 — ширина, 20..24 — высота (big-endian). Формат проверяется по
/// сигнатуре и имени чанка — иначе `None`.
#[must_use]
pub(crate) fn png_dimensions(bytes: &[u8]) -> Option<(u32, u32)> {
    const SIGNATURE: &[u8; 8] = b"\x89PNG\r\n\x1a\n";
    if bytes.len() < 24 || &bytes[..8] != SIGNATURE || &bytes[12..16] != b"IHDR" {
        return None;
    }
    let width = u32::from_be_bytes(bytes[16..20].try_into().ok()?);
    let height = u32::from_be_bytes(bytes[20..24].try_into().ok()?);
    if width == 0 || height == 0 {
        return None;
    }
    Some((width, height))
}

/// Размер корневого окна из вывода `xdpyinfo` (`dimensions: 1920x1080 pixels`).
#[must_use]
pub(crate) fn parse_xdpyinfo_dimensions(text: &str) -> Option<(u32, u32)> {
    let line = text.lines().find(|l| l.contains("dimensions:"))?;
    let rest = line.split("dimensions:").nth(1)?;
    parse_wxh(rest.split_whitespace().next()?)
}

/// Размер из вывода `xdotool getdisplaygeometry` (`1920 1080`).
#[must_use]
pub(crate) fn parse_xdotool_geometry(text: &str) -> Option<(u32, u32)> {
    let mut nums = text
        .split_whitespace()
        .filter_map(|t| t.parse::<u32>().ok());
    let w = nums.next()?;
    let h = nums.next()?;
    if w == 0 || h == 0 {
        return None;
    }
    Some((w, h))
}

/// Разбор пары `ШИРИНАxВЫСОТА` (разделитель — латинская `x` или `X`).
fn parse_wxh(token: &str) -> Option<(u32, u32)> {
    let (w, h) = token.split_once(['x', 'X'])?;
    let w: u32 = w.trim().parse().ok()?;
    let h: u32 = h.trim().parse().ok()?;
    if w == 0 || h == 0 {
        return None;
    }
    Some((w, h))
}

/// Размер корневого окна X11 — система координат мыши.
///
/// Кандидаты: `xdotool getdisplaygeometry` (точнее), затем `xdpyinfo`.
/// Обе команды быстрые и не блокируют X-сервер, поэтому синхронный вызов
/// согласован с остальным модулем.
///
/// # Errors
/// Ни один кандидат не дал размера.
pub(crate) fn screen_size() -> Result<(u32, u32)> {
    if crate::doctor::binary_in_path("xdotool") {
        if let Ok(out) = std::process::Command::new("xdotool")
            .arg("getdisplaygeometry")
            .output()
        {
            if out.status.success() {
                if let Some(size) = parse_xdotool_geometry(&String::from_utf8_lossy(&out.stdout)) {
                    return Ok(size);
                }
            }
        }
    }
    let out = std::process::Command::new("xdpyinfo")
        .output()
        .map_err(|e| HarnessError::Tool(format!("xdpyinfo: не запустился: {e}")))?;
    parse_xdpyinfo_dimensions(&String::from_utf8_lossy(&out.stdout)).ok_or_else(|| {
        HarnessError::Tool(
            "не удалось определить размер экрана: нужен xdotool или xdpyinfo с X11-сессией".into(),
        )
    })
}

/// Список видимых окон: `id  заголовок` построчно.
///
/// # Errors
/// `xdotool` не установлен.
pub(crate) fn window_list() -> Result<String> {
    if !crate::doctor::binary_in_path("xdotool") {
        return Err(HarnessError::Tool(
            "window_list: не найден xdotool. Установите: sudo apt install xdotool".into(),
        ));
    }
    let out = std::process::Command::new("xdotool")
        .args(["search", "--onlyvisible", "--name", ""])
        .output()
        .map_err(|e| HarnessError::Tool(format!("xdotool search: {e}")))?;
    if !out.status.success() {
        return Err(HarnessError::Tool(format!(
            "xdotool search: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        )));
    }
    let ids: Vec<String> = String::from_utf8_lossy(&out.stdout)
        .split_whitespace()
        .map(str::to_string)
        .collect();
    if ids.is_empty() {
        return Ok("видимых окон не найдено".into());
    }
    // Имя окна запрашивается одним вызовом на id — окон на экране единицы,
    // стоимость пренебрежима, зато не нужен разбор пакетного вывода.
    let mut lines = Vec::with_capacity(ids.len());
    for id in ids {
        let name = std::process::Command::new("xdotool")
            .args(["getwindowname", &id])
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .filter(|n| !n.is_empty())
            .unwrap_or_else(|| "(без имени)".to_string());
        lines.push(format!("{id}  {name}"));
    }
    Ok(format!(
        "Видимые окна (id  заголовок):\n{}",
        lines.join("\n")
    ))
}

/// Снять скриншот первой доступной утилитой.
fn capture_screenshot(path: &Path) -> Result<()> {
    let display = std::env::var("DISPLAY").unwrap_or_else(|_| ":0".to_string());
    let mut last = String::new();
    for args in SCREENSHOT_CANDIDATES {
        let bin = args[0];
        let rest: Vec<String> = args[1..]
            .iter()
            .map(|a| {
                a.replace("{path}", &path.to_string_lossy())
                    .replace("{display}", &display)
            })
            .collect();
        match std::process::Command::new(bin).args(&rest).output() {
            Ok(out) if out.status.success() => return Ok(()),
            Ok(out) => {
                last = format!("{bin}: {}", String::from_utf8_lossy(&out.stderr).trim());
            }
            Err(e) => {
                last = format!("{bin}: {e}");
            }
        }
    }
    let display_hint = if std::env::var("DISPLAY").is_ok_and(|d| !d.trim().is_empty()) {
        String::new()
    } else {
        " DISPLAY не задан — нужна X11-сессия.".to_string()
    };
    Err(HarnessError::Tool(format!(
        "не удалось снять скриншот — нужна утилита (scrot/import/gnome-screenshot/ffmpeg).{display_hint} Последняя ошибка: {last}"
    )))
}

/// Прочитать изображение в base64 data-URL.
fn image_data_url(path: &Path) -> Result<String> {
    let bytes = std::fs::read(path)
        .map_err(|e| HarnessError::Tool(format!("чтение изображения {}: {e}", path.display())))?;
    Ok(data_url_from_bytes(mime_from_ext(path), &bytes))
}

/// Собрать base64 data-URL из уже прочитанных байт (формат `OpenAI`
/// `image_url`; `DeepSeek` детектирует формат по содержимому, MIME — лишь
/// префикс data-URL).
pub(crate) fn data_url_from_bytes(mime: &str, bytes: &[u8]) -> String {
    format!(
        "data:{mime};base64,{}",
        crate::clipboard::base64_encode(bytes)
    )
}

/// MIME-тип по расширению файла (`DeepSeek` детектирует по содержимому, MIME —
/// лишь префикс data-URL).
fn mime_from_ext(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("webp") => "image/webp",
        Some("gif") => "image/gif",
        _ => "image/png",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Минимальный PNG (1×1 пиксель, валидная сигнатура) — для теста чтения.
    const TINY_PNG: &[u8] = &[
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, // сигнатура
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52, // IHDR
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, // 1×1
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4, // bit depth
        0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41, // IDAT
        0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00, // данные
        0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, // IEND
        0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ];

    #[test]
    fn mime_from_extension() {
        assert_eq!(mime_from_ext(Path::new("a.png")), "image/png");
        assert_eq!(mime_from_ext(Path::new("a.jpg")), "image/jpeg");
        assert_eq!(mime_from_ext(Path::new("a.webp")), "image/webp");
        assert_eq!(mime_from_ext(Path::new("a.bin")), "image/png");
    }

    /// Заголовок PNG с заданными размерами (читается только IHDR).
    fn png_header(width: u32, height: u32) -> Vec<u8> {
        let mut out = Vec::from(*b"\x89PNG\r\n\x1a\n");
        out.extend_from_slice(&13u32.to_be_bytes());
        out.extend_from_slice(b"IHDR");
        out.extend_from_slice(&width.to_be_bytes());
        out.extend_from_slice(&height.to_be_bytes());
        out.extend_from_slice(&[0x08, 0x06, 0x00, 0x00, 0x00]);
        out
    }

    #[test]
    fn png_dimensions_reads_tiny_fixture() {
        assert_eq!(png_dimensions(TINY_PNG), Some((1, 1)));
    }

    #[test]
    fn png_dimensions_reads_full_hd_header() {
        assert_eq!(png_dimensions(&png_header(1920, 1080)), Some((1920, 1080)));
    }

    #[test]
    fn png_dimensions_rejects_non_png_truncated_and_zero_sized() {
        assert_eq!(png_dimensions(b"not a png at all, just text"), None);
        assert_eq!(png_dimensions(&TINY_PNG[..20]), None, "усечённый заголовок");
        assert_eq!(png_dimensions(&[]), None);
        assert_eq!(png_dimensions(&png_header(0, 1080)), None, "нулевой размер");
    }

    #[test]
    fn xdpyinfo_dimensions_parsed_from_real_output() {
        // Строка снята с рабочей машины (`xdpyinfo`, DISPLAY=:1).
        let out = "name of display:    :1\nscreen #0:\n  \
                   dimensions:    1920x1080 pixels (602x331 millimeters)\n  \
                   depth of root window:    24 planes\n";
        assert_eq!(parse_xdpyinfo_dimensions(out), Some((1920, 1080)));
        assert_eq!(parse_xdpyinfo_dimensions("нет тут размеров"), None);
        assert_eq!(parse_xdpyinfo_dimensions("dimensions:   мусор"), None);
    }

    #[test]
    fn xdotool_geometry_parsed() {
        assert_eq!(parse_xdotool_geometry("1920 1080\n"), Some((1920, 1080)));
        assert_eq!(parse_xdotool_geometry("  3840   2160 "), Some((3840, 2160)));
        assert_eq!(parse_xdotool_geometry("0 1080"), None, "нулевая ширина");
        assert_eq!(parse_xdotool_geometry("одно число"), None);
        assert_eq!(parse_xdotool_geometry(""), None);
    }

    #[test]
    fn data_url_from_bytes_prefixes_mime() {
        let url = data_url_from_bytes("image/png", TINY_PNG);
        assert!(url.starts_with("data:image/png;base64,"), "{url}");
        assert!(url.len() > "data:image/png;base64,".len());
    }

    #[test]
    fn image_data_url_embeds_base64() {
        let tmp = tempfile::tempdir().expect("tmp");
        let p = tmp.path().join("x.png");
        std::fs::write(&p, TINY_PNG).expect("write");
        let url = image_data_url(&p).expect("encode");
        assert!(url.starts_with("data:image/png;base64,"), "{url}");
        assert!(url.len() > "data:image/png;base64,".len());
    }

    /// Живой прогон: размер кадра из IHDR совпадает с размером экрана —
    /// это предусловие координатного контракта (ADR-041). Если разойдётся,
    /// значит захват масштабирует кадр и `computer_*` нужен промежуточный
    /// пересчёт кадр→экран.
    #[test]
    #[ignore = "live: требует DISPLAY + утилиту скриншота"]
    fn live_frame_size_matches_screen_size() {
        let tmp = tempfile::tempdir().expect("tmp");
        let p = tmp.path().join("frame.png");
        capture_screenshot(&p).expect("скриншот");
        let bytes = std::fs::read(&p).expect("read");
        let frame = png_dimensions(&bytes).expect("размер кадра из IHDR");
        let screen = screen_size().expect("размер экрана");
        assert_eq!(
            frame, screen,
            "кадр {frame:?} и экран {screen:?} разошлись — нужен пересчёт масштаба"
        );
    }

    #[test]
    #[ignore = "live: требует DISPLAY + утилиту скриншота (ffmpeg/scrot)"]
    fn live_capture_produces_png_data_url() {
        let tmp = tempfile::tempdir().expect("tmp");
        let p = tmp.path().join("live.png");
        capture_screenshot(&p).expect("скриншот");
        let bytes = std::fs::read(&p).expect("read");
        assert!(
            bytes.starts_with(&[0x89, 0x50, 0x4E, 0x47]),
            "PNG-сигнатура (первые байты: {:02X?})",
            &bytes[..8.min(bytes.len())]
        );
        let url = image_data_url(&p).expect("data-url");
        assert!(url.starts_with("data:image/png;base64,"), "{url}");
    }
}
