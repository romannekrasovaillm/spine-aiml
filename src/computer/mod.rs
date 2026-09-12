//! Управление компьютером и браузером через vision (ADR-041).
//!
//! КОНТРАКТ (владелец: агент `computer`):
//! - [`screen`] — координатный контракт (нормализованные 0–1000) и разбор
//!   размеров кадра/экрана; чистые функции без X11;
//! - [`input`] — инструменты воздействия `computer_*` (мышь, клавиатура,
//!   фокус окна): необратимые внешние действия, класс `Destructive`;
//! - [`browser`] — браузерный контур: запуск Chrome и CDP-инструменты
//!   `browser_*` (Node-хелпер, ноль новых Rust-крейтов);
//! - [`tools`] — реестр этого контура, подключается в `tools::domain_tools`
//!   ТОЛЬКО при `[computer].enabled = true`.
//!
//! Разделение контуров: наблюдение (`screenshot`, `read_image`, `screen_size`,
//! `window_list`) живёт в ядре и включено всегда — видеть экран безопасно.
//! Воздействие гейтится конфигом (по умолчанию выключено) И классом риска
//! policy: клик и ввод текста необратимы и видны за пределами харнесса.
//!
//! Граница честности: гейт policy останавливает промахи модели на
//! blessed-пути и делает решение аудируемым, но НЕ является изоляцией —
//! `bash` остаётся общим люком (см. ADR-041, «Чего гейт не даёт»).

use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use crate::config::Config;
use crate::error::{HarnessError, Result};
use crate::tool::Tool;

pub mod browser;
pub mod input;
pub mod screen;

/// Таймаут ожидания завершения команды ввода по умолчанию, секунды.
const DEFAULT_TIMEOUT_SECS: u64 = 10;

/// Инструменты контура воздействия.
///
/// `[computer].enabled = false` (дефолт) — пустой список: инструменты не
/// попадают в реестр вовсе (гейт на уровне регистрации, как у `[web]` и
/// `[archify]`). Браузерный подконтур включается отдельно —
/// `[computer.browser].enabled`, чтобы управление столом и браузером
/// можно было включать независимо.
#[must_use]
pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>> {
    if !cfg.computer.enabled {
        return Vec::new();
    }
    let mut out = input::tools(cfg);
    if cfg.computer.browser.enabled {
        out.extend(browser::tools(cfg));
    }
    out
}

/// Проверить, что сессия вообще может получать ввод: есть дисплей.
///
/// `xdotool` работает только через X11. Wayland-бэкенды (`ydotool`/`wtype`)
/// в этой ревизии не поддержаны — честная ошибка лучше, чем выдуманный argv
/// (см. ADR-041). Отсутствие `DISPLAY` — не паника, а внятный отказ.
pub(crate) fn ensure_display() -> Result<String> {
    if let Ok(display) = std::env::var("DISPLAY") {
        if !display.trim().is_empty() {
            return Ok(display);
        }
    }
    if std::env::var("WAYLAND_DISPLAY").is_ok_and(|v| !v.trim().is_empty()) {
        return Err(HarnessError::Tool(
            "сессия под Wayland: управление вводом в этой ревизии работает только через X11 \
             (xdotool). Запустите харнесс в X11-сессии либо отключите [computer].enabled"
                .into(),
        ));
    }
    Err(HarnessError::Tool(
        "DISPLAY не задан — управление компьютером недоступно (нужна X11-сессия с xdotool)".into(),
    ))
}

/// Есть ли бинарь в `PATH`.
pub(crate) fn binary_in_path(name: &str) -> bool {
    crate::doctor::binary_in_path(name)
}

/// Имя бэкенда ввода: явная настройка `[computer].input_backend`, иначе
/// автодетект (`xdotool`).
pub(crate) fn input_backend(cfg: &Config) -> Result<String> {
    let configured = cfg.computer.input_backend.trim();
    if !configured.is_empty() {
        if configured != "xdotool" {
            return Err(HarnessError::Tool(format!(
                "[computer].input_backend = \"{configured}\": поддерживается только \"xdotool\" \
                 (Wayland-бэкенды не реализованы — ADR-041)"
            )));
        }
        return Ok(configured.to_string());
    }
    if binary_in_path("xdotool") {
        return Ok("xdotool".to_string());
    }
    Err(HarnessError::Tool(
        "не найден xdotool — управление вводом недоступно. Установите: \
         sudo apt install xdotool (X11), либо выключите [computer].enabled"
            .into(),
    ))
}

/// Выполнить команду с таймаутом, `kill_on_drop` и очищенным окружением.
///
/// Окружение чистится тем же правилом, что у bash (`[bash].env_scrub`):
/// процесс ввода не должен видеть ключи провайдеров. Таймаут защищает ход
/// агента от зависшего `xdotool --sync`.
///
/// # Errors
/// Бинарь не найден; ненулевой код возврата; превышен таймаут.
pub(crate) async fn run_argv(bin: &str, args: &[String], timeout_secs: u64) -> Result<String> {
    let mut command = tokio::process::Command::new(bin);
    command.args(args).kill_on_drop(true);
    command.env_clear();
    // Скраб тот же, что у bash: `Config` тут недоступен, поэтому правило
    // берётся из дефолта конфигурации (env_scrub = true, без исключений).
    let (kept, _dropped) = crate::tools::bash::scrub_env(std::env::vars(), true, &[]);
    command.envs(kept);
    command.stdout(Stdio::piped()).stderr(Stdio::piped());

    let waited = tokio::time::timeout(Duration::from_secs(timeout_secs), command.output()).await;
    let out = match waited {
        Ok(Ok(out)) => out,
        Ok(Err(e)) => {
            return Err(HarnessError::Tool(format!("{bin}: не запустился: {e}")));
        }
        Err(_) => {
            return Err(HarnessError::Tool(format!(
                "{bin}: таймаут {timeout_secs} с — команда не завершилась"
            )));
        }
    };
    let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
        return Err(HarnessError::Tool(format!(
            "{bin}: код {:?}: {}",
            out.status.code(),
            if stderr.is_empty() { stdout } else { stderr }
        )));
    }
    Ok(stdout)
}

/// Таймаут вызова инструмента ввода из конфига (0 — дефолт).
pub(crate) fn timeout_secs(cfg: &Config) -> u64 {
    if cfg.computer.timeout_secs == 0 {
        DEFAULT_TIMEOUT_SECS
    } else {
        cfg.computer.timeout_secs
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tools_empty_when_computer_disabled() {
        let cfg = Config::default();
        assert!(!cfg.computer.enabled, "воздействие выключено по умолчанию");
        assert!(
            tools(&cfg).is_empty(),
            "выключенный контур не отдаёт инструментов"
        );
    }

    #[test]
    fn tools_include_input_but_not_browser_when_browser_disabled() {
        let mut cfg = Config::default();
        cfg.computer.enabled = true;
        cfg.computer.browser.enabled = false;
        let names: Vec<String> = tools(&cfg).iter().map(|t| t.spec().name).collect();
        assert!(names.iter().any(|n| n == "computer_click"), "{names:?}");
        assert!(
            !names.iter().any(|n| n.starts_with("browser_")),
            "браузерный подконтур включается отдельно: {names:?}"
        );
    }

    #[test]
    fn tools_include_browser_when_both_enabled() {
        let mut cfg = Config::default();
        cfg.computer.enabled = true;
        cfg.computer.browser.enabled = true;
        let names: Vec<String> = tools(&cfg).iter().map(|t| t.spec().name).collect();
        assert!(names.iter().any(|n| n == "browser_navigate"), "{names:?}");
    }

    #[test]
    fn input_backend_rejects_unknown_setting() {
        let mut cfg = Config::default();
        cfg.computer.input_backend = "ydotool".into();
        let err = input_backend(&cfg).expect_err("ydotool не поддержан в v1");
        assert!(err.to_string().contains("xdotool"), "{err}");
    }

    #[test]
    fn timeout_falls_back_to_default_on_zero() {
        let mut cfg = Config::default();
        cfg.computer.timeout_secs = 0;
        assert_eq!(timeout_secs(&cfg), DEFAULT_TIMEOUT_SECS);
        cfg.computer.timeout_secs = 42;
        assert_eq!(timeout_secs(&cfg), 42);
    }

    #[test]
    fn config_round_trip_parses_computer_section() {
        let toml = r"
            [computer]
            enabled = true
            key_delay_ms = 25
            max_clicks = 2

            [computer.browser]
            enabled = true
            headless = true
            debug_port = 9333
        ";
        let cfg: Config = toml::from_str(toml).expect("разбор секции [computer]");
        assert!(cfg.computer.enabled);
        assert_eq!(cfg.computer.key_delay_ms, 25);
        assert_eq!(cfg.computer.max_clicks, 2);
        assert!(cfg.computer.browser.enabled);
        assert!(cfg.computer.browser.headless);
        assert_eq!(cfg.computer.browser.debug_port, 9333);
        // Незаданные поля — дефолты.
        assert_eq!(
            cfg.computer.browser.node_bin,
            crate::config::default_node_bin()
        );
        assert_eq!(cfg.agent.images_keep_last, 1);
    }
}
