//! Копирование в системный буфер обмена.
//!
//! КОНТРАКТ (владелец: агент `tui`):
//! - основной механизм — `arboard`: прямая работа с X11-селекцией CLIPBOARD
//!   (или Wayland) без внешних утилит; владелец селекции — фоновый поток,
//!   поэтому [`Clipboard`] должен ЖИТЬ, пока пользователь не вставил текст
//!   (на X11 буфер — это «владелец отдаёт данные по запросу», дроп объекта =
//!   потеря буфера, стандартное поведение X11);
//! - дальше фолбэки: внешние утилиты `wl-copy`/`xclip`/`xsel`, затем OSC 52
//!   (escape-последовательность в /dev/tty; в GNOME Terminal/VTE запись
//!   буфера через OSC 52 отключена — это последний шанс, не гарантия);
//! - возвращается имя сработавшего механизма для статус-сообщения; ошибка —
//!   только если недоступны ВСЕ механизмы (с перечнем попыток).

use std::io::Write;
use std::process::{Command, Stdio};

use crate::error::{HarnessError, Result};

/// Держатель системного буфера обмена (ленивая инициализация `arboard`).
///
/// Хранится в `App` на всю жизнь TUI: на X11 данные буфера живут, пока жив
/// владелец селекции, — создавать `Clipboard` на каждое копирование нельзя.
pub struct Clipboard {
    inner: Option<arboard::Clipboard>,
}

impl Clipboard {
    /// Пустой держатель; соединение с сервером откроется при первом `copy`.
    #[must_use]
    pub fn new() -> Self {
        Self { inner: None }
    }

    /// Копирует текст в буфер обмена; возвращает имя механизма.
    ///
    /// # Errors
    /// Ни один механизм не сработал (перечень попыток в тексте ошибки).
    pub fn copy(&mut self, text: &str) -> Result<&'static str> {
        let mut tried = Vec::new();
        // 1. Нативный путь: arboard сам становится владельцем селекции.
        if self.inner.is_none() {
            match arboard::Clipboard::new() {
                Ok(cb) => self.inner = Some(cb),
                Err(e) => tried.push(format!("arboard ({e})")),
            }
        }
        if let Some(cb) = &mut self.inner {
            match cb.set_text(text) {
                Ok(()) => return Ok("системный буфер"),
                Err(e) => {
                    // Соединение могло умереть — сбросим, чтобы в следующий
                    // раз переоткрыть; сейчас идём по фолбэкам.
                    tried.push(format!("arboard ({e})"));
                    self.inner = None;
                }
            }
        }
        // 2. Внешние утилиты: stdin → буфер.
        for (prog, argv, name) in [
            ("wl-copy", &[][..], "wl-copy"),
            ("xclip", &["-selection", "clipboard"][..], "xclip"),
            ("xsel", &["--clipboard", "--input"][..], "xsel"),
        ] {
            match pipe_to(prog, argv, text) {
                Ok(()) => return Ok(name),
                Err(_) => tried.push(name.to_string()),
            }
        }
        // 3. OSC 52: последовательность в /dev/tty (не stdout — TUI владеет
        // экраном). В VTE (GNOME Terminal) запись отключена, но kitty/
        // alacritty/wezterm/foot и некоторые tmux-конфиги примут.
        if osc52(text).is_ok() {
            return Ok("OSC 52");
        }
        tried.push("OSC 52".into());
        Err(HarnessError::Tui(format!(
            "буфер обмена недоступен: не сработали {}. \
             Установите xclip (X11) или wl-clipboard (Wayland)",
            tried.join(", ")
        )))
    }
}

impl Default for Clipboard {
    fn default() -> Self {
        Self::new()
    }
}

/// Пишет текст в stdin утилиты буфера; Ok при коде возврата 0.
fn pipe_to(prog: &str, args: &[&str], text: &str) -> std::io::Result<()> {
    let mut child = Command::new(prog)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    if let Some(mut stdin) = child.stdin.take() {
        // EPIPE не страшен: утилита вправе закрыть stdin раньше.
        let _ = stdin.write_all(text.as_bytes());
    }
    let status = child.wait()?;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other(format!("{prog}: код {status}")))
    }
}

/// OSC 52: `\x1b]52;c;<base64>\x07` в /dev/tty (clipboard-селекция `c`).
fn osc52(text: &str) -> std::io::Result<()> {
    let mut tty = std::fs::OpenOptions::new().write(true).open("/dev/tty")?;
    let seq = format!("\x1b]52;c;{}\x07", base64_encode(text.as_bytes()));
    tty.write_all(seq.as_bytes())?;
    tty.flush()
}

/// Минимальный base64 (RFC 4648, с паддингом) — без внешней зависимости.
/// Переиспользуется мультимодальными инструментами (скриншот/чтение изображения).
pub(crate) fn base64_encode(data: &[u8]) -> String {
    const ABC: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = u32::from(chunk[0]);
        let b1 = u32::from(*chunk.get(1).unwrap_or(&0));
        let b2 = u32::from(*chunk.get(2).unwrap_or(&0));
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ABC[(n >> 18) as usize & 63] as char);
        out.push(ABC[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            ABC[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ABC[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_vectors_rfc4648() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foob"), "Zm9vYg==");
        assert_eq!(base64_encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
        // UTF-8 (кириллица) кодируется побайтово.
        assert_eq!(base64_encode("привет".as_bytes()), "0L/RgNC40LLQtdGC");
    }

    #[test]
    fn copy_reports_missing_mechanisms_gracefully() {
        // В тестовой среде хоть один механизм может и сработать — важно,
        // что вызов не паникует и завершается (Ok или понятная ошибка).
        let mut cb = Clipboard::new();
        match cb.copy("тест буфера") {
            Ok(mech) => assert!(
                ["системный буфер", "wl-copy", "xclip", "xsel", "OSC 52"].contains(&mech),
                "неизвестный механизм {mech}"
            ),
            Err(e) => assert!(e.to_string().contains("буфер обмена недоступен")),
        }
    }

    #[test]
    fn second_copy_reuses_handle() {
        // Повторное копирование по живому хендлу не должно падать
        // (регрессия: «второй Ctrl+C молча теряет буфер»).
        let mut cb = Clipboard::new();
        let _ = cb.copy("первый");
        let _ = cb.copy("второй");
    }
}
