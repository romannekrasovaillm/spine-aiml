//! Инструменты воздействия `computer_*`: мышь, клавиатура, фокус окна.
//!
//! Все они необратимы и видны за пределами харнесса, поэтому классифицируются
//! как `Destructive` (`src/policy.rs`): при дефолтном R2 отклоняются, на R4
//! требуют подтверждения человека, на R5 идут автоматически (ADR-041).
//!
//! Команды только X11 (`xdotool`). Wayland-бэкенды (`ydotool`, `wtype`) в
//! этой ревизии не реализованы сознательно: их argv (битовые маски кнопок
//! ydotool, keysym-таблицы) нельзя проверить на этой машине, а выдуманный
//! argv необратимой команды хуже честного отказа. См. [`super::input_backend`].
//!
//! Аргументы собираются чистыми билдерами (`build_*_argv`) и передаются
//! процессу как argv — shell не участвует нигде, поэтому инъекция через текст
//! или заголовок окна невозможна по построению (тесты это фиксируют).

use std::sync::Arc;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};

use super::screen::{map_normalized, validate_normalized};
use super::{ensure_display, input_backend, run_argv, timeout_secs};
use crate::config::Config;
use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Бинарь бэкенда ввода в этой ревизии.
const XDOTOOL: &str = "xdotool";

/// Номер кнопки X11 по имени.
#[must_use]
pub(crate) fn button_number(name: &str) -> Option<&'static str> {
    match name {
        "left" => Some("1"),
        "middle" => Some("2"),
        "right" => Some("3"),
        _ => None,
    }
}

/// Кнопка колеса по направлению (X11: 4/5 — вертикаль, 6/7 — горизонталь).
#[must_use]
pub(crate) fn scroll_button(direction: &str) -> Option<&'static str> {
    match direction {
        "up" => Some("4"),
        "down" => Some("5"),
        "left" => Some("6"),
        "right" => Some("7"),
        _ => None,
    }
}

/// `xdotool mousemove X Y`.
#[must_use]
pub(crate) fn build_move_argv(x: u32, y: u32) -> Vec<String> {
    vec![
        XDOTOOL.into(),
        "mousemove".into(),
        x.to_string(),
        y.to_string(),
    ]
}

/// `xdotool mousemove X Y click --repeat N BUTTON` — наведение и клик одним
/// процессом: иначе между наведением и кликом курсор может уехать.
#[must_use]
pub(crate) fn build_click_argv(x: u32, y: u32, button: &str, clicks: u32) -> Vec<String> {
    vec![
        XDOTOOL.into(),
        "mousemove".into(),
        x.to_string(),
        y.to_string(),
        "click".into(),
        "--repeat".into(),
        clicks.to_string(),
        button.into(),
    ]
}

/// `xdotool [mousemove X Y] click --repeat N BUTTON` — колесо.
#[must_use]
pub(crate) fn build_scroll_argv(at: Option<(u32, u32)>, button: &str, amount: u32) -> Vec<String> {
    let mut argv = vec![XDOTOOL.to_string()];
    if let Some((x, y)) = at {
        argv.extend(["mousemove".into(), x.to_string(), y.to_string()]);
    }
    argv.extend([
        "click".into(),
        "--repeat".into(),
        amount.to_string(),
        button.into(),
    ]);
    argv
}

/// Перетаскивание ОДНИМ процессом `xdotool`: `mousemove → mousedown →
/// mousemove → mouseup`. Разбивать на отдельные вызовы нельзя — между ними
/// курсор успевает уехать и перетаскивание срывается.
#[must_use]
pub(crate) fn build_drag_argv(from: (u32, u32), to: (u32, u32), button: &str) -> Vec<String> {
    vec![
        XDOTOOL.into(),
        "mousemove".into(),
        from.0.to_string(),
        from.1.to_string(),
        "mousedown".into(),
        button.into(),
        "mousemove".into(),
        to.0.to_string(),
        to.1.to_string(),
        "mouseup".into(),
        button.into(),
    ]
}

/// `xdotool key --clearmodifiers <keys>` — комбинация клавиш.
#[must_use]
pub(crate) fn build_key_argv(keys: &str) -> Vec<String> {
    vec![
        XDOTOOL.into(),
        "key".into(),
        "--clearmodifiers".into(),
        keys.into(),
    ]
}

/// `xdotool type --delay N -- <text>` — ввод текста.
///
/// `--` завершает разбор опций: текст, начинающийся с `-`, не будет прочитан
/// как флаг. Текст остаётся ОДНИМ элементом argv — shell не участвует.
#[must_use]
pub(crate) fn build_type_argv(text: &str, delay_ms: u64) -> Vec<String> {
    vec![
        XDOTOOL.into(),
        "type".into(),
        "--delay".into(),
        delay_ms.to_string(),
        "--".into(),
        text.into(),
    ]
}

/// Вставка из буфера (`ctrl+v`) — надёжный путь для кириллицы.
#[must_use]
pub(crate) fn build_paste_argv() -> Vec<String> {
    vec![
        XDOTOOL.into(),
        "key".into(),
        "--clearmodifiers".into(),
        "ctrl+v".into(),
    ]
}

/// Фокус окна по подстроке заголовка.
#[must_use]
pub(crate) fn build_focus_argv(title: &str) -> Vec<String> {
    vec![
        XDOTOOL.into(),
        "search".into(),
        "--name".into(),
        title.into(),
        "windowactivate".into(),
    ]
}

/// Инструменты воздействия (подключаются при `[computer].enabled = true`).
#[must_use]
pub fn tools(_cfg: &Config) -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(MoveTool),
        Arc::new(ClickTool),
        Arc::new(ScrollTool),
        Arc::new(DragTool),
        Arc::new(KeyTool),
        Arc::new(TypeTool),
        Arc::new(FocusTool),
    ]
}

/// Тело вызова инструмента ввода: проверки → argv → запуск → отчёт.
async fn dispatch_input(
    cfg: &Config,
    argv: Vec<String>,
    report: String,
) -> std::result::Result<ToolOutput, ToolOutput> {
    ensure_display().map_err(|e| ToolOutput::err(e.to_string()))?;
    input_backend(cfg).map_err(|e| ToolOutput::err(e.to_string()))?;
    let (bin, rest) = argv.split_first().ok_or_else(|| {
        ToolOutput::err("внутренняя ошибка: пустой argv инструмента ввода".to_string())
    })?;
    match run_argv(bin, rest, timeout_secs(cfg)).await {
        Ok(_) => Ok(ToolOutput::ok(report)),
        Err(e) => Err(ToolOutput::err(e.to_string())),
    }
}

/// Точка в пикселях экрана и сам размер экрана.
type PixelsAndScreen = ((u32, u32), (u32, u32));

/// Проверить нормализованные координаты и перевести их в пиксели экрана.
///
/// Кадр скриншота снимается в натуральную величину всего экрана, поэтому
/// `frame == screen` и промежуточный масштаб тождественен; функция
/// [`map_normalized`] сохраняет двухразмерную форму на случай будущего
/// масштабированного захвата (ADR-041).
fn to_pixels(x: u32, y: u32) -> std::result::Result<PixelsAndScreen, ToolOutput> {
    if let Err(msg) = validate_normalized("x", x) {
        return Err(ToolOutput::err(msg));
    }
    if let Err(msg) = validate_normalized("y", y) {
        return Err(ToolOutput::err(msg));
    }
    let size = crate::tools::screenshot::screen_size()
        .map_err(|e| ToolOutput::err(format!("не удалось определить размер экрана: {e}")))?;
    Ok((map_normalized(x, y, size, size), size))
}

/// Общая часть отчёта: пиксели + исходные нормализованные единицы + экран.
fn point_report(action: &str, pt: (u32, u32), norm: (u32, u32), size: (u32, u32)) -> String {
    format!(
        "{action}: пиксели ({}, {}) [норм. {},{}]; экран {}×{}",
        pt.0, pt.1, norm.0, norm.1, size.0, size.1
    )
}

/// Потолок повторов: защита от runaway-цикла (ADR-041, v1).
fn clamp_amount(value: u32, max: u32) -> (u32, bool) {
    let max = max.max(1);
    if value > max {
        (max, true)
    } else {
        (value, false)
    }
}

/// Пометка об обрезке повторов — модель должна знать, что её ввод изменили.
fn clamped_note(was_clamped: bool, max: u32) -> String {
    if was_clamped {
        format!(" [число повторов обрезано до {max}]")
    } else {
        String::new()
    }
}

struct MoveTool;

#[derive(Deserialize)]
struct MoveArgs {
    x: u32,
    y: u32,
}

#[async_trait]
impl Tool for MoveTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "computer_move".into(),
            description: format!(
                "Навести курсор (без клика) — нужно для всплывающих подсказок и подменю. {}",
                crate::tools::screenshot::COORDINATE_HINT
            ),
            parameters: json!({
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Нормализованная координата 0–1000"},
                    "y": {"type": "integer", "description": "Нормализованная координата 0–1000"}
                },
                "required": ["x", "y"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: MoveArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("computer_move: аргументы: {e}")))?;
        let (pt, size) = match to_pixels(args.x, args.y) {
            Ok(v) => v,
            Err(out) => return Ok(out),
        };
        let report = point_report("курсор наведён", pt, (args.x, args.y), size);
        match dispatch_input(&ctx.config, build_move_argv(pt.0, pt.1), report).await {
            Ok(out) | Err(out) => Ok(out),
        }
    }
}

struct ClickTool;

#[derive(Deserialize)]
struct ClickArgs {
    x: u32,
    y: u32,
    #[serde(default)]
    button: Option<String>,
    #[serde(default)]
    clicks: Option<u32>,
}

#[async_trait]
impl Tool for ClickTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "computer_click".into(),
            description: format!(
                "Клик мышью по координатам. Сначала screenshot, затем клик, затем НОВЫЙ screenshot \
                 для проверки результата. {}",
                crate::tools::screenshot::COORDINATE_HINT
            ),
            parameters: json!({
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Нормализованная координата 0–1000"},
                    "y": {"type": "integer", "description": "Нормализованная координата 0–1000"},
                    "button": {"type": "string", "enum": ["left", "middle", "right"], "description": "Кнопка (по умолчанию left)"},
                    "clicks": {"type": "integer", "description": "Число кликов, 1–3 (по умолчанию 1; двойной клик — 2)"}
                },
                "required": ["x", "y"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: ClickArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("computer_click: аргументы: {e}")))?;
        let button_name = args.button.as_deref().unwrap_or("left");
        let Some(button) = button_number(button_name) else {
            return Ok(ToolOutput::err(format!(
                "computer_click: неизвестная кнопка «{button_name}» (left | middle | right)"
            )));
        };
        let (clicks, clamped) =
            clamp_amount(args.clicks.unwrap_or(1), ctx.config.computer.max_clicks);
        let (pt, size) = match to_pixels(args.x, args.y) {
            Ok(v) => v,
            Err(out) => return Ok(out),
        };
        let report = format!(
            "{}{}",
            point_report(
                &format!("клик {button_name} ×{clicks}"),
                pt,
                (args.x, args.y),
                size
            ),
            clamped_note(clamped, ctx.config.computer.max_clicks)
        );
        match dispatch_input(
            &ctx.config,
            build_click_argv(pt.0, pt.1, button, clicks),
            report,
        )
        .await
        {
            Ok(out) | Err(out) => Ok(out),
        }
    }
}

struct ScrollTool;

#[derive(Deserialize)]
struct ScrollArgs {
    #[serde(default)]
    x: Option<u32>,
    #[serde(default)]
    y: Option<u32>,
    direction: String,
    #[serde(default)]
    amount: Option<u32>,
}

#[async_trait]
impl Tool for ScrollTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "computer_scroll".into(),
            description: format!(
                "Прокрутка колесом. Координаты необязательны: без них прокручивается область под \
                 текущим курсором. {}",
                crate::tools::screenshot::COORDINATE_HINT
            ),
            parameters: json!({
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Нормализованная координата 0–1000 (необязательно)"},
                    "y": {"type": "integer", "description": "Нормализованная координата 0–1000 (необязательно)"},
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "amount": {"type": "integer", "description": "Число шагов (по умолчанию 3; обрезается потолком [computer].max_clicks × 4)"}
                },
                "required": ["direction"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: ScrollArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("computer_scroll: аргументы: {e}")))?;
        let Some(button) = scroll_button(&args.direction) else {
            return Ok(ToolOutput::err(format!(
                "computer_scroll: неизвестное направление «{}» (up | down | left | right)",
                args.direction
            )));
        };
        let (amount, clamped) =
            clamp_amount(args.amount.unwrap_or(3), ctx.config.computer.max_clicks * 4);
        // Координаты — обе или ни одной: половинчатая пара неоднозначна.
        let at = match (args.x, args.y) {
            (Some(x), Some(y)) => match to_pixels(x, y) {
                Ok((pt, size)) => Some((pt, size)),
                Err(out) => return Ok(out),
            },
            (None, None) => None,
            _ => {
                return Ok(ToolOutput::err(
                    "computer_scroll: задайте обе координаты (x и y) или ни одной",
                ));
            }
        };
        let report = match at {
            Some((pt, size)) => format!(
                "{}{}",
                point_report(
                    &format!("прокрутка {} ×{amount}", args.direction),
                    pt,
                    (args.x.unwrap_or(0), args.y.unwrap_or(0)),
                    size
                ),
                clamped_note(clamped, ctx.config.computer.max_clicks * 4)
            ),
            None => format!("прокрутка {} ×{amount} под курсором", args.direction),
        };
        let argv = build_scroll_argv(at.map(|(pt, _)| pt), button, amount);
        match dispatch_input(&ctx.config, argv, report).await {
            Ok(out) | Err(out) => Ok(out),
        }
    }
}

struct DragTool;

#[derive(Deserialize)]
struct DragArgs {
    from_x: u32,
    from_y: u32,
    to_x: u32,
    to_y: u32,
    #[serde(default)]
    button: Option<String>,
}

#[async_trait]
impl Tool for DragTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "computer_drag".into(),
            description: format!(
                "Перетаскивание: зажать кнопку в точке А, довести до Б, отпустить (слайдеры, холсты, \
                 изменение размеров). {}",
                crate::tools::screenshot::COORDINATE_HINT
            ),
            parameters: json!({
                "type": "object",
                "properties": {
                    "from_x": {"type": "integer", "description": "Начало, нормализованная 0–1000"},
                    "from_y": {"type": "integer", "description": "Начало, нормализованная 0–1000"},
                    "to_x": {"type": "integer", "description": "Конец, нормализованная 0–1000"},
                    "to_y": {"type": "integer", "description": "Конец, нормализованная 0–1000"},
                    "button": {"type": "string", "enum": ["left", "middle", "right"]}
                },
                "required": ["from_x", "from_y", "to_x", "to_y"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: DragArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("computer_drag: аргументы: {e}")))?;
        let button_name = args.button.as_deref().unwrap_or("left");
        let Some(button) = button_number(button_name) else {
            return Ok(ToolOutput::err(format!(
                "computer_drag: неизвестная кнопка «{button_name}» (left | middle | right)"
            )));
        };
        let (from, from_size) = match to_pixels(args.from_x, args.from_y) {
            Ok(v) => v,
            Err(out) => return Ok(out),
        };
        let (to, _) = match to_pixels(args.to_x, args.to_y) {
            Ok(v) => v,
            Err(out) => return Ok(out),
        };
        let report = format!(
            "перетаскивание {button_name}: ({}, {}) → ({}, {}) [норм. {},{}) → {},{}]; экран {}×{}",
            from.0,
            from.1,
            to.0,
            to.1,
            args.from_x,
            args.from_y,
            args.to_x,
            args.to_y,
            from_size.0,
            from_size.1
        );
        match dispatch_input(&ctx.config, build_drag_argv(from, to, button), report).await {
            Ok(out) | Err(out) => Ok(out),
        }
    }
}

struct KeyTool;

#[derive(Deserialize)]
struct KeyArgs {
    keys: String,
}

#[async_trait]
impl Tool for KeyTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "computer_key".into(),
            description: "Нажать клавишу или комбинацию в X11-нотации: Return, Escape, Tab, \
                          ctrl+l, ctrl+shift+t, alt+F4, ctrl+a. Используется для навигации и \
                          горячих клавиш интерфейса."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "keys": {"type": "string", "description": "Клавиша или комбинация, напр. ctrl+l"}
                },
                "required": ["keys"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: KeyArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("computer_key: аргументы: {e}")))?;
        if args.keys.trim().is_empty() {
            return Ok(ToolOutput::err("computer_key: пустая комбинация"));
        }
        let report = format!("нажатие «{}»", args.keys);
        match dispatch_input(&ctx.config, build_key_argv(&args.keys), report).await {
            Ok(out) | Err(out) => Ok(out),
        }
    }
}

struct TypeTool;

#[derive(Deserialize)]
struct TypeArgs {
    text: String,
    #[serde(default)]
    method: Option<String>,
}

#[async_trait]
impl Tool for TypeTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "computer_type".into(),
            description:
                "Ввести текст в активное поле. method=\"type\" (по умолчанию) жмёт клавиши \
                          и надёжен только для ASCII; для кириллицы и не-ASCII используй \
                          method=\"clipboard\" (текст кладётся в буфер и вставляется через ctrl+v)."
                    .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст для ввода"},
                    "method": {"type": "string", "enum": ["type", "clipboard"], "description": "Способ ввода (по умолчанию type)"}
                },
                "required": ["text"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: TypeArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("computer_type: аргументы: {e}")))?;
        let method = args.method.as_deref().unwrap_or("type");
        match method {
            "type" => {
                let report = format!(
                    "введён текст ({} символов, метод type)",
                    args.text.chars().count()
                );
                match dispatch_input(
                    &ctx.config,
                    build_type_argv(&args.text, ctx.config.computer.key_delay_ms),
                    report,
                )
                .await
                {
                    Ok(out) | Err(out) => Ok(out),
                }
            }
            "clipboard" => {
                ensure_display().map_err(|e| HarnessError::Tool(e.to_string()))?;
                input_backend(&ctx.config).map_err(|e| HarnessError::Tool(e.to_string()))?;
                // Буфер живёт, пока живёт Clipboard: держим его до конца
                // вставки, иначе владение selection потеряется до ctrl+v.
                let mut clipboard = crate::clipboard::Clipboard::new();
                let how = clipboard
                    .copy(&args.text)
                    .map_err(|e| HarnessError::Tool(format!("computer_type: буфер: {e}")))?;
                let argv = build_paste_argv();
                let (bin, rest) = argv.split_first().ok_or_else(|| {
                    HarnessError::Tool("computer_type: пустой argv вставки".into())
                })?;
                let out = run_argv(bin, rest, timeout_secs(&ctx.config)).await;
                drop(clipboard);
                out.map_err(|e| HarnessError::Tool(e.to_string()))?;
                Ok(ToolOutput::ok(format!(
                    "вставлен текст ({} символов, метод clipboard через {how})",
                    args.text.chars().count()
                )))
            }
            other => Ok(ToolOutput::err(format!(
                "computer_type: неизвестный method «{other}» (type | clipboard)"
            ))),
        }
    }
}

struct FocusTool;

#[derive(Deserialize)]
struct FocusArgs {
    title: String,
}

#[async_trait]
impl Tool for FocusTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "window_focus".into(),
            description: "Поднять и активировать окно по подстроке заголовка (window_list покажет \
                          доступные). Нужен, чтобы действия попали в нужное окно."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Подстрока заголовка окна"}
                },
                "required": ["title"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: FocusArgs = serde_json::from_value(args)
            .map_err(|e| HarnessError::Tool(format!("window_focus: аргументы: {e}")))?;
        if args.title.trim().is_empty() {
            return Ok(ToolOutput::err("window_focus: пустой заголовок"));
        }
        let report = format!("окно по подстроке «{}» активировано", args.title);
        match dispatch_input(&ctx.config, build_focus_argv(&args.title), report).await {
            Ok(out) | Err(out) => Ok(out),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn button_names_map_to_x11_numbers() {
        assert_eq!(button_number("left"), Some("1"));
        assert_eq!(button_number("middle"), Some("2"));
        assert_eq!(button_number("right"), Some("3"));
        assert_eq!(button_number("back"), None);
    }

    #[test]
    fn scroll_directions_map_to_wheel_buttons() {
        assert_eq!(scroll_button("up"), Some("4"));
        assert_eq!(scroll_button("down"), Some("5"));
        assert_eq!(scroll_button("left"), Some("6"));
        assert_eq!(scroll_button("right"), Some("7"));
        assert_eq!(scroll_button("вверх"), None);
    }

    #[test]
    fn move_argv_is_exact() {
        assert_eq!(
            build_move_argv(960, 540),
            vec!["xdotool", "mousemove", "960", "540"]
        );
    }

    #[test]
    fn click_argv_navigates_and_clicks_in_one_process() {
        assert_eq!(
            build_click_argv(960, 540, "1", 2),
            vec![
                "xdotool",
                "mousemove",
                "960",
                "540",
                "click",
                "--repeat",
                "2",
                "1"
            ]
        );
    }

    #[test]
    fn scroll_argv_optional_pointer_position() {
        assert_eq!(
            build_scroll_argv(None, "5", 3),
            vec!["xdotool", "click", "--repeat", "3", "5"]
        );
        assert_eq!(
            build_scroll_argv(Some((10, 20)), "4", 1),
            vec![
                "xdotool",
                "mousemove",
                "10",
                "20",
                "click",
                "--repeat",
                "1",
                "4"
            ]
        );
    }

    #[test]
    fn drag_argv_is_a_single_process_sequence() {
        assert_eq!(
            build_drag_argv((1, 2), (3, 4), "1"),
            vec![
                "xdotool",
                "mousemove",
                "1",
                "2",
                "mousedown",
                "1",
                "mousemove",
                "3",
                "4",
                "mouseup",
                "1"
            ]
        );
    }

    #[test]
    fn key_argv_clears_modifiers() {
        assert_eq!(
            build_key_argv("ctrl+l"),
            vec!["xdotool", "key", "--clearmodifiers", "ctrl+l"]
        );
    }

    #[test]
    fn type_argv_keeps_text_as_one_element_after_double_dash() {
        // Текст с ведущим дефисом не должен читаться как опция xdotool,
        // а пробелы и кавычки не должны расщепляться — shell не участвует.
        let argv = build_type_argv("-rf --dangerous ' text", 12);
        assert_eq!(
            argv,
            vec![
                "xdotool",
                "type",
                "--delay",
                "12",
                "--",
                "-rf --dangerous ' text"
            ]
        );
        assert_eq!(argv.len(), 6, "текст — ровно один элемент argv");
    }

    #[test]
    fn focus_argv_keeps_title_as_one_element() {
        assert_eq!(
            build_focus_argv("Chrome; rm -rf /"),
            vec![
                "xdotool",
                "search",
                "--name",
                "Chrome; rm -rf /",
                "windowactivate"
            ]
        );
    }

    #[test]
    fn clamp_amount_caps_and_reports() {
        assert_eq!(clamp_amount(2, 3), (2, false));
        assert_eq!(clamp_amount(9, 3), (3, true));
        assert_eq!(clamp_amount(9, 0), (1, true), "нулевой потолок — минимум 1");
        assert!(clamped_note(true, 3).contains("обрезано"));
        assert!(clamped_note(false, 3).is_empty());
    }

    /// Текущая позиция указателя (`xdotool getmouselocation --shell`).
    fn read_cursor() -> (i64, i64) {
        let out = std::process::Command::new("xdotool")
            .args(["getmouselocation", "--shell"])
            .output()
            .expect("getmouselocation");
        let text = String::from_utf8_lossy(&out.stdout).to_string();
        let get = |p: &str| -> i64 {
            text.lines()
                .find(|l| l.starts_with(p))
                .and_then(|l| l.split('=').nth(1))
                .and_then(|v| v.trim().parse().ok())
                .unwrap_or(-1)
        };
        (get("X="), get("Y="))
    }

    /// Умеет ли этот X-сервер двигать указатель.
    ///
    /// Виртуальные серверы (Xvfb) принимают `XWarpPointer` и XTEST с кодом 0,
    /// но позиция указателя не меняется. На них имеет смысл проверять только
    /// приемлемость argv (коды возврата), а позиционные утверждения — на
    /// настоящем X-сервере.
    fn pointer_is_warpable() -> bool {
        let before = read_cursor();
        let _ = std::process::Command::new("xdotool")
            .args(["mousemove", "1", "1"])
            .output();
        read_cursor() != before
    }

    /// Общий замок live-тестов ввода: оба теста ходят на один X-сервер и
    /// делят один курсор — параллельный прогон даёт гонку за позицию
    /// указателя (контур читает координаты, установленные соседним тестом).
    static LIVE_DISPLAY_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

    /// Живой прогон: нормализованные координаты реально двигают курсор.
    ///
    /// Только `computer_move`, без клика: проверяем координатный контракт, не
    /// трогая ничего за пределами позиции указателя.
    #[tokio::test]
    #[ignore = "live: требует X11 (DISPLAY) + xdotool; двигает курсор мыши"]
    async fn live_move_places_cursor_at_normalized_center() {
        // Оба live-теста ввода делят один X-сервер и один курсор: параллельный
        // прогон читает позицию, установленную соседним тестом (гонка за
        // указатель). Сериализуемся общим замком.
        let _display_guard = LIVE_DISPLAY_LOCK.lock().await;
        if !crate::doctor::binary_in_path("xdotool") {
            eprintln!("xdotool не найден — живой прогон пропущен");
            return;
        }
        // Проба ДО наведения: сама она двигает указатель в (1,1).
        if !pointer_is_warpable() {
            eprintln!("дисплей не двигает указатель (виртуальный X) — позиция не проверяется");
            return;
        }
        let ctx = ToolContext::new(std::env::temp_dir(), std::sync::Arc::new(Config::default()));
        let out = MoveTool
            .call(json!({"x": 500, "y": 500}), &ctx)
            .await
            .expect("вызов computer_move");
        assert!(!out.is_error, "computer_move: {}", out.content);
        let (screen_w, screen_h) = crate::tools::screenshot::screen_size().expect("размер экрана");
        let (gx, gy) = read_cursor();
        let expected_x = i64::from(screen_w / 2);
        let expected_y = i64::from(screen_h / 2);
        assert!(
            (gx - expected_x).abs() <= 2,
            "курсор X={gx} при ожидании ~{expected_x}"
        );
        assert!(
            (gy - expected_y).abs() <= 2,
            "курсор Y={gy} при ожидании ~{expected_y}"
        );
    }

    /// Полный контур ввода на изолированном X-дисплее.
    ///
    /// Клик, скролл, перетаскивание и ввод трогают активное окно, поэтому
    /// тест запускается ТОЛЬКО при явном `ARCH_ML_LIVE_XDOTOOL=1` — обычный
    /// `cargo test -- --ignored` на рабочем столе его не выполнит. Штатный
    /// сценарий: Xvfb (`Xvfb :99 & DISPLAY=:99 ARCH_ML_LIVE_XDOTOOL=1 cargo
    /// test -- --ignored live_input_contour`), где кликать не по чему.
    #[tokio::test]
    #[ignore = "live: требует Xvfb + ARCH_ML_LIVE_XDOTOOL=1 (иначе трогал бы рабочий стол)"]
    async fn live_input_contour_against_isolated_display() {
        // См. LIVE_DISPLAY_LOCK: сериализация с `live_move_…` на одном дисплее.
        let _display_guard = LIVE_DISPLAY_LOCK.lock().await;
        if std::env::var("ARCH_ML_LIVE_XDOTOOL").as_deref() != Ok("1") {
            eprintln!("ARCH_ML_LIVE_XDOTOOL != 1 — пропуск (защита рабочего стола)");
            return;
        }
        if !crate::doctor::binary_in_path("xdotool") {
            eprintln!("xdotool не найден — пропуск");
            return;
        }
        let ctx = ToolContext::new(std::env::temp_dir(), std::sync::Arc::new(Config::default()));
        let (w, h) = crate::tools::screenshot::screen_size().expect("размер экрана");
        let warpable = pointer_is_warpable();
        if !warpable {
            eprintln!(
                "дисплей не двигает указатель (виртуальный X) — проверяются только \
                 приемлемость argv и коды возврата"
            );
        }
        let expect = |nx: u32, ny: u32| {
            (
                i64::from(nx) * (i64::from(w) - 1) / 1000,
                i64::from(ny) * (i64::from(h) - 1) / 1000,
            )
        };

        // Наведение.
        let out = MoveTool
            .call(json!({"x": 250, "y": 250}), &ctx)
            .await
            .expect("move");
        assert!(!out.is_error, "{}", out.content);
        if warpable {
            let (ex, ey) = expect(250, 250);
            let (gx, gy) = read_cursor();
            assert!(
                (gx - ex).abs() <= 2 && (gy - ey).abs() <= 2,
                "move: ({gx},{gy}) vs ({ex},{ey})"
            );
        }

        // Клик (левый, двойной) — на пустом корневом окне безопасен.
        let out = ClickTool
            .call(json!({"x": 250, "y": 250, "clicks": 2}), &ctx)
            .await
            .expect("click");
        assert!(!out.is_error, "{}", out.content);

        // Скролл под курсором.
        let out = ScrollTool
            .call(json!({"direction": "down", "amount": 3}), &ctx)
            .await
            .expect("scroll");
        assert!(!out.is_error, "{}", out.content);

        // Перетаскивание: курсор обязан остаться в конечной точке.
        let out = DragTool
            .call(
                json!({"from_x": 200, "from_y": 200, "to_x": 600, "to_y": 600}),
                &ctx,
            )
            .await
            .expect("drag");
        assert!(!out.is_error, "{}", out.content);
        if warpable {
            let (ex, ey) = expect(600, 600);
            let (gx, gy) = read_cursor();
            assert!(
                (gx - ex).abs() <= 2 && (gy - ey).abs() <= 2,
                "drag: ({gx},{gy}) vs ({ex},{ey})"
            );
        }

        // Клавиша и текст — уходят в никуда (на корневом окне нет фокуса ввода).
        let out = KeyTool
            .call(json!({"keys": "Escape"}), &ctx)
            .await
            .expect("key");
        assert!(!out.is_error, "{}", out.content);
        let out = TypeTool
            .call(json!({"text": "spine live smoke"}), &ctx)
            .await
            .expect("type");
        assert!(!out.is_error, "{}", out.content);
        // Кириллица — через буфер: проверяем, что путь не падает и кладёт текст.
        let out = TypeTool
            .call(json!({"text": "Привет", "method": "clipboard"}), &ctx)
            .await
            .expect("type clipboard");
        assert!(!out.is_error, "{}", out.content);
    }

    #[test]
    fn tools_expose_expected_names() {
        let names: Vec<String> = tools(&Config::default())
            .iter()
            .map(|t| t.spec().name)
            .collect();
        for expected in [
            "computer_move",
            "computer_click",
            "computer_scroll",
            "computer_drag",
            "computer_key",
            "computer_type",
            "window_focus",
        ] {
            assert!(
                names.iter().any(|n| n == expected),
                "нет {expected}: {names:?}"
            );
        }
    }

    #[test]
    fn every_input_spec_states_the_coordinate_contract() {
        // Контракт доходит до модели только через описания инструментов
        // (системный промпт о computer_* не знает) — проверяем, что он там есть.
        for tool in tools(&Config::default()) {
            let spec = tool.spec();
            if spec.name == "window_focus"
                || spec.name == "computer_key"
                || spec.name == "computer_type"
            {
                continue; // эти инструменты работают без координат
            }
            assert!(
                spec.description.contains("НОРМАЛИЗОВАН") && spec.description.contains("0–1000"),
                "{}: описание не несёт координатный контракт: {}",
                spec.name,
                spec.description
            );
        }
    }
}
