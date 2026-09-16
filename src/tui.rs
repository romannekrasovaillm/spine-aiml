//! TUI харнесса: ratatui + crossterm, акторная архитектура.
//!
//! КОНТРАКТ (владелец: агент `tui-cron`):
//! - [`run`] — полноэкранный TUI: левая колонка (команды/сессия), центр —
//!   чат с агентом (стриминг дельт, markdown-lite подсветка, статус вызовов
//!   инструментов), правая колонка с вкладками (Mermaid / Рубрика / Знания),
//!   статус-бар (модель, токены, cwd); ASCII-баннер при старте
//!   (`assets::BANNER`);
//! - палитра Tokyo Night: bg #1a1b26, fg #c0caf5, cyan #7dcfff,
//!   purple #bb9af7, green #9ece6a, orange #ff9e64, red #f7768e, muted #565f89;
//! - событийная модель: crossterm `EventStream` + mpsc-каналы (агентные
//!   события `AgentEvent`), bounded-каналы, graceful shutdown по Ctrl-C/q/Esc,
//!   восстановление терминала при панике (RAII-гард + panic hook);
//! - ввод: история команд (Up/Down), автодополнение слэш-команд по Tab.
//!
//! Реализация: [`app::App`] владеет всем состоянием в event loop (без
//! `Arc<Mutex>`); ход агента и слэш-команды выполняются в `tokio::spawn`,
//! сессия возвращается сообщением [`app::AppMessage`]; события
//! [`crate::agent::AgentEvent`] форвардятся через bounded mpsc(64).

use std::io;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crossterm::event::{DisableMouseCapture, EnableMouseCapture};
use crossterm::event::{Event, EventStream, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use futures::StreamExt;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use tokio::sync::mpsc;

use crate::config::Config;
use crate::error::{HarnessError, Result};

pub(crate) mod app;
pub mod caps;
mod intro;
mod keymap;
mod render;
#[cfg(test)]
pub(crate) mod shot;
mod text;
mod theme;

use app::{App, AppMessage};

/// Ёмкость канала сообщений приложения (bounded — backpressure).
const APP_CHANNEL_CAP: usize = 256;
/// Период перерисовки спиннера ожидания модели.
const SPINNER_INTERVAL: Duration = Duration::from_millis(120);
/// Минимальный интервал между полными очистками экрана на ресайзе:
/// чаще — мерцание при перетаскивании окна, реже — «призраки» кадра.
const RESIZE_CLEAR_DEBOUNCE: Duration = Duration::from_millis(50);

/// Запускает TUI. Блокируется до выхода пользователя (`q`, Ctrl-C, Esc).
///
/// # Errors
/// Терминал недоступен (нет TTY), ошибка отрисовки кадра.
pub async fn run(cfg: Arc<Config>) -> Result<()> {
    run_with(cfg, caps::Overrides::default()).await
}

/// Запускает TUI с явными переопределениями оформления из CLI
/// (`--theme`, `--no-color`, `--ascii`, `--no-mouse`, `--no-animation`).
///
/// # Errors
/// Терминал недоступен (нет TTY), ошибка отрисовки кадра.
pub async fn run_with(cfg: Arc<Config>, overrides: caps::Overrides) -> Result<()> {
    let defaults = caps::ConfigDefaults {
        theme: cfg.tui.theme_choice(),
        mouse: cfg.tui.mouse,
        animation: cfg.tui.animation,
        unicode: cfg.tui.unicode,
    };
    let mut caps = caps::Caps::detect(|k| std::env::var(k).ok(), overrides.with_defaults(defaults));
    caps.min_width = cfg.tui.min_width;
    caps.min_height = cfg.tui.min_height;
    let _guard = TerminalGuard::enter(caps.mouse)?;
    install_panic_hook();
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))
        .map_err(|e| HarnessError::Tui(format!("инициализация терминала: {e}")))?;
    terminal
        .clear()
        .map_err(|e| HarnessError::Tui(format!("очистка экрана: {e}")))?;

    let mut app = App::build(cfg, caps).await;
    // Стартовая заставка-интро (уважает `--no-animation` / `[tui] animation`).
    if app.caps.animation {
        app.start_intro();
    }
    let (msg_tx, mut msg_rx) = mpsc::channel::<AppMessage>(APP_CHANNEL_CAP);
    app.attach(msg_tx);

    let mut keys = EventStream::new();
    let mut ticker = tokio::time::interval(SPINNER_INTERVAL);
    // Время последней очистки экрана на ресайзе (дебаунс, см. ниже).
    let mut last_resize_clear: Option<Instant> = None;
    let ctrl_c = tokio::signal::ctrl_c();
    tokio::pin!(ctrl_c);

    loop {
        if app.should_quit {
            break;
        }
        terminal
            .draw(|f| app.render(f))
            .map_err(|e| HarnessError::Tui(format!("отрисовка: {e}")))?;
        tokio::select! {
            maybe_event = keys.next() => {
                match maybe_event {
                    Some(Ok(Event::Key(key))) => {
                        if key.kind != KeyEventKind::Release {
                            app.handle_key(key);
                        }
                    }
                    // Колесо мыши — прокрутка диалога.
                    Some(Ok(Event::Mouse(mouse))) => app.handle_mouse(mouse),
                    // Resize: терминал сам рефлоуит содержимое, и его буфер
                    // расходится с кадровым буфером ratatui — без полной
                    // очистки на экране остаются «призрачные» артефакты
                    // (размазанные остатки шапки/текста, кейс 2026-09-02).
                    // Полный clear → следующий кадр перерисуется с нуля.
                    Some(Ok(Event::Resize(..))) => {
                        // Перетаскивание окна шлёт десятки событий в секунду;
                        // полный clear на каждое даёт мерцание. Чистим не чаще
                        // раза в RESIZE_CLEAR_DEBOUNCE — этого хватает, чтобы
                        // не осталось «призраков», но кадр не мигает.
                        let now = Instant::now();
                        if last_resize_clear
                            .is_none_or(|t| now.duration_since(t) >= RESIZE_CLEAR_DEBOUNCE)
                        {
                            last_resize_clear = Some(now);
                            let _ = terminal.clear();
                        }
                    }
                    // Фокус и прочие события — просто перерисовываемся.
                    Some(Ok(_)) => {}
                    // Поток ввода закрылся или сломался — выходим.
                    Some(Err(_)) | None => app.should_quit = true,
                }
            }
            msg = msg_rx.recv() => {
                if let Some(m) = msg {
                    app.handle_message(m);
                }
                // None: отправителей нет и не будет — ждём только клавиши.
            }
            _ = &mut ctrl_c => app.should_quit = true,
            _ = ticker.tick(), if app.needs_tick() => app.tick(),
        }
    }

    app.shutdown().await;
    Ok(())
}

/// RAII-гард терминала: при Drop покидает alternate screen и выключает
/// raw mode — терминал восстанавливается при любом выходе из [`run`].
struct TerminalGuard {
    /// Захватывали ли мышь (тогда её и отпускаем).
    mouse: bool,
}

impl TerminalGuard {
    /// Входит в raw mode + alternate screen + захват мыши (колесо — скролл).
    ///
    /// # Errors
    /// Терминал недоступен (нет TTY, запуск в пайпе/CI). Текст ошибки ведёт
    /// к headless-поверхности: интерактивный TUI в пайпе не имеет смысла,
    /// а молчаливое «нужен TTY» не подсказывает, что делать.
    fn enter(mouse: bool) -> Result<Self> {
        enable_raw_mode().map_err(|e| {
            HarnessError::Tui(format!(
                "интерактивный режим требует терминал (TTY): {e}\n\
                 Для скриптов и пайпов используйте headless-режим:\n\
                 `arch-ml run -q \"…\"` (stdout — только ответ; docs/headless.md)."
            ))
        })?;
        let entered = if mouse {
            execute!(io::stdout(), EnterAlternateScreen, EnableMouseCapture)
        } else {
            execute!(io::stdout(), EnterAlternateScreen)
        };
        if let Err(e) = entered {
            let _ = disable_raw_mode();
            return Err(HarnessError::Tui(format!("alternate screen: {e}")));
        }
        Ok(Self { mouse })
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        if self.mouse {
            let _ = execute!(io::stdout(), DisableMouseCapture, LeaveAlternateScreen);
        } else {
            let _ = execute!(io::stdout(), LeaveAlternateScreen);
        }
        let _ = disable_raw_mode();
    }
}

/// Panic hook: восстанавливает терминал перед печатью паники,
/// затем передаёт управление исходному обработчику.
fn install_panic_hook() {
    let original = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let _ = execute!(io::stdout(), DisableMouseCapture, LeaveAlternateScreen);
        let _ = disable_raw_mode();
        original(info);
    }));
}
