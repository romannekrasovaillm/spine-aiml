//! Возможности терминала: ярус цвета, Unicode-глифы, мышь, анимация.
//!
//! Приложение запустят не в нашем терминале: в conhost на Windows, в tmux
//! поверх ssh с задержкой, в `LANG=C` без UTF-8, в пайпе без TTY. Поэтому
//! оформление не хардкодится, а выбирается по признакам окружения
//! (см. скилл `tui-accessibility-compat`, §1) с явным переопределением из CLI.
//!
//! Приоритет: флаги CLI → переменные окружения → `[tui]` конфига → дефолт.
//! Значения из конфига приходят сюда уже слитыми в [`Overrides`], поэтому
//! функция [`detect`] детерминирована и тестируется без реального окружения.

/// Ярус цвета терминала (по убыванию возможностей).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ColorLevel {
    /// 24-битный цвет (`COLORTERM=truecolor|24bit`, kitty/WezTerm/…).
    TrueColor,
    /// 256 индексированных цветов (`TERM=*-256color`).
    Ansi256,
    /// Базовые 16 цветов ANSI — тема пользователя задаёт их сама.
    Ansi16,
    /// Без цвета (`NO_COLOR`, `TERM=dumb`): смысл несут символ и атрибуты.
    Mono,
}

/// Выбор палитры — re-export из `config` (нижний слой): тип темы — понятие
/// конфигурации; `ui` ссылается на него снизу вверх (`no_tui_below_ui`).
pub use crate::config::ThemeChoice;

/// Явные переопределения оформления (флаги CLI, значения `[tui]` конфига).
#[derive(Debug, Clone, Copy, Default)]
pub struct Overrides {
    /// `--no-color`: отключить цвет, оставить символы и атрибуты.
    pub no_color: bool,
    /// `--ascii`: рамки и глифы — только ASCII.
    pub ascii: bool,
    /// `--no-mouse`: не захватывать мышь (выделение текста терминалом).
    pub no_mouse: bool,
    /// `--no-animation`: статичные индикаторы вместо спиннера.
    pub no_animation: bool,
    /// `--theme dark|light|auto`.
    pub theme: Option<ThemeChoice>,
}

/// Значения `[tui]` конфига — «мягкие» дефолты, которые перебивает явный
/// флаг CLI, но которые сами перебивают эвристику окружения.
#[derive(Debug, Clone, Copy)]
pub struct ConfigDefaults {
    /// Тема из конфига (`None` — не задана/не распознана).
    pub theme: Option<ThemeChoice>,
    /// Захватывать мышь.
    pub mouse: bool,
    /// Крутить анимации.
    pub animation: bool,
    /// Unicode-глифы.
    pub unicode: bool,
}

impl Default for ConfigDefaults {
    fn default() -> Self {
        Self {
            theme: None,
            mouse: true,
            animation: true,
            unicode: true,
        }
    }
}

impl Overrides {
    /// Дополняет переопределения значениями конфига: явный флаг CLI важнее.
    #[must_use]
    pub fn with_defaults(mut self, d: ConfigDefaults) -> Self {
        if self.theme.is_none() {
            self.theme = d.theme;
        }
        self.no_mouse |= !d.mouse;
        self.no_animation |= !d.animation;
        self.ascii |= !d.unicode;
        self
    }
}

/// Возможности терминала и вытекающие ограничения интерфейса.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Caps {
    /// Ярус цвета.
    pub color: ColorLevel,
    /// Доступны ли Unicode-рамки и символы (иначе — ASCII-фолбэк).
    pub unicode: bool,
    /// Захватывать ли мышь (колесо, выделение, кнопка «▼»).
    pub mouse: bool,
    /// Крутить ли анимации (спиннер, пульс).
    pub animation: bool,
    /// Светлый фон терминала → светлая палитра.
    pub light: bool,
    /// Контрастная тема (`--theme high-contrast`): без цвета и dim,
    /// смысл несут bold/reverse (для слабовидящих).
    pub high_contrast: bool,
    /// Минимальная ширина окна: меньше — заглушка «терминал мал».
    pub min_width: u16,
    /// Минимальная высота окна.
    pub min_height: u16,
}

impl Default for Caps {
    fn default() -> Self {
        Self {
            color: ColorLevel::TrueColor,
            unicode: true,
            mouse: true,
            animation: true,
            light: false,
            high_contrast: false,
            min_width: 60,
            min_height: 16,
        }
    }
}

impl Caps {
    /// Детектирует возможности по окружению `env` и переопределениям `o`.
    ///
    /// `env` инжектируется функцией-лукапом (в проде — `std::env::var`),
    /// чтобы тесты не зависели от реального окружения машины.
    pub(crate) fn detect(env: impl Fn(&str) -> Option<String>, o: Overrides) -> Self {
        let term = env("TERM").unwrap_or_default();
        let dumb = term.eq_ignore_ascii_case("dumb");
        let truecolor = env("COLORTERM").is_some_and(|v| {
            let v = v.to_ascii_lowercase();
            v == "truecolor" || v == "24bit"
        });
        let color = if o.no_color || env("NO_COLOR").is_some() || dumb {
            ColorLevel::Mono
        } else if truecolor {
            ColorLevel::TrueColor
        } else if term.contains("256color") {
            ColorLevel::Ansi256
        } else if [
            "kitty",
            "wezterm",
            "ghostty",
            "alacritty",
            "foot",
            "rio",
            "contour",
        ]
        .iter()
        .any(|t| term.contains(t))
        {
            ColorLevel::TrueColor
        } else {
            ColorLevel::Ansi16
        };

        let unicode = if o.ascii { false } else { unicode_locale(&env) };

        // Медленные соединения: анимация дёргает перерисовку каждый кадр.
        let remote = env("SSH_CONNECTION").is_some() || env("SSH_TTY").is_some();

        Self {
            color,
            unicode,
            mouse: !o.no_mouse,
            animation: !o.no_animation && !remote,
            light: match o.theme.unwrap_or_default() {
                ThemeChoice::Light => true,
                ThemeChoice::Dark | ThemeChoice::HighContrast => false,
                ThemeChoice::Auto => light_background(&env),
            },
            high_contrast: o.theme == Some(ThemeChoice::HighContrast),
            ..Self::default()
        }
    }
}

/// Локаль обещает UTF-8? Пустая локаль — считаем, что да (кроме Windows
/// без Windows Terminal: там кодовая страница консоли ненадёжна).
fn unicode_locale(env: &impl Fn(&str) -> Option<String>) -> bool {
    let locale = env("LC_ALL")
        .or_else(|| env("LC_CTYPE"))
        .or_else(|| env("LANG"))
        .unwrap_or_default();
    if locale.trim().is_empty() {
        return !cfg!(windows) || env("WT_SESSION").is_some();
    }
    let up = locale.to_ascii_uppercase();
    up.contains("UTF-8") || up.contains("UTF8")
}

/// Светлый ли фон терминала. Надёжного способа без OSC 11 нет; пользуемся
/// наследием rxvt — `COLORFGBG="fg;bg"`, где bg ≥ 7 в 16-цветной палитре
/// означает светлый фон. Неизвестно — тёмный (палитра по умолчанию).
fn light_background(env: &impl Fn(&str) -> Option<String>) -> bool {
    let Some(v) = env("COLORFGBG") else {
        return false;
    };
    v.rsplit(';')
        .next()
        .and_then(|bg| bg.trim().parse::<u8>().ok())
        .is_some_and(|bg| bg >= 7)
}

/// Ближайший индекс xterm-256 для truecolor-значения: стандартный куб 6×6×6
/// плюс серая рампа. Своя реализация — крейт `no-new-dependencies` запрещён.
pub(crate) fn rgb_to_ansi256(r: u8, g: u8, b: u8) -> u8 {
    if r == g && g == b {
        // Серая рампа: 232..=255 (8 + 10·n).
        return match r {
            0..=7 => 16,
            248..=255 => 231,
            _ => 232 + (r - 8) / 10,
        };
    }
    let axis = |c: u8| -> u16 {
        if c < 48 {
            0
        } else if c < 115 {
            1
        } else {
            (u16::from(c) - 35) / 40
        }
    };
    // 36·5 + 6·5 + 5 + 16 = 231 — старший индекс куба, в u8 помещается.
    (16 + 36 * axis(r) + 6 * axis(g) + axis(b)) as u8
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;

    /// Лукап по словарю — тесты не зависят от реального окружения машины.
    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> + use<> {
        let map: BTreeMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect();
        move |k: &str| map.get(k).cloned()
    }

    #[test]
    fn truecolor_detected_from_colorterm() {
        let c = Caps::detect(
            env(&[("TERM", "xterm-256color"), ("COLORTERM", "truecolor")]),
            Overrides::default(),
        );
        assert_eq!(c.color, ColorLevel::TrueColor);
    }

    #[test]
    fn ansi256_from_term_suffix() {
        let c = Caps::detect(env(&[("TERM", "xterm-256color")]), Overrides::default());
        assert_eq!(c.color, ColorLevel::Ansi256);
    }

    #[test]
    fn ansi16_is_the_floor_for_unknown_terms() {
        let c = Caps::detect(env(&[("TERM", "xterm")]), Overrides::default());
        assert_eq!(c.color, ColorLevel::Ansi16);
    }

    #[test]
    fn no_color_and_dumb_force_mono() {
        for pairs in [
            vec![("TERM", "xterm-256color"), ("NO_COLOR", "1")],
            vec![("TERM", "xterm-256color"), ("NO_COLOR", "")],
            vec![("TERM", "dumb")],
        ] {
            let c = Caps::detect(env(&pairs), Overrides::default());
            assert_eq!(c.color, ColorLevel::Mono, "набор: {pairs:?}");
        }
    }

    #[test]
    fn no_color_flag_beats_truecolor_env() {
        let o = Overrides {
            no_color: true,
            ..Overrides::default()
        };
        let c = Caps::detect(env(&[("COLORTERM", "truecolor")]), o);
        assert_eq!(c.color, ColorLevel::Mono);
    }

    #[test]
    fn modern_terminal_names_imply_truecolor() {
        for term in ["xterm-kitty", "wezterm", "ghostty", "alacritty"] {
            let c = Caps::detect(env(&[("TERM", term)]), Overrides::default());
            assert_eq!(c.color, ColorLevel::TrueColor, "TERM={term}");
        }
    }

    #[test]
    fn locale_without_utf8_turns_unicode_off() {
        let c = Caps::detect(env(&[("LANG", "C")]), Overrides::default());
        assert!(!c.unicode, "LANG=C — ASCII-фолбэк");
        let c = Caps::detect(env(&[("LANG", "ru_RU.UTF-8")]), Overrides::default());
        assert!(c.unicode);
        // LC_ALL перекрывает LANG.
        let c = Caps::detect(
            env(&[("LANG", "ru_RU.UTF-8"), ("LC_ALL", "C")]),
            Overrides::default(),
        );
        assert!(!c.unicode);
    }

    #[test]
    fn empty_locale_keeps_unicode_off_windows_only() {
        let c = Caps::detect(env(&[]), Overrides::default());
        assert_eq!(c.unicode, !cfg!(windows));
    }

    #[test]
    fn ascii_flag_forces_ascii_even_in_utf8_locale() {
        let o = Overrides {
            ascii: true,
            ..Overrides::default()
        };
        let c = Caps::detect(env(&[("LANG", "ru_RU.UTF-8")]), o);
        assert!(!c.unicode);
    }

    #[test]
    fn ssh_disables_animation_but_not_mouse() {
        let c = Caps::detect(
            env(&[("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 2")]),
            Overrides::default(),
        );
        assert!(!c.animation);
        assert!(c.mouse, "мышь по ssh работает");
    }

    #[test]
    fn no_mouse_and_no_animation_flags() {
        let o = Overrides {
            no_mouse: true,
            no_animation: true,
            ..Overrides::default()
        };
        let c = Caps::detect(env(&[]), o);
        assert!(!c.mouse && !c.animation);
    }

    #[test]
    fn theme_choice_and_colorfgbg() {
        let c = Caps::detect(env(&[("COLORFGBG", "15;0")]), Overrides::default());
        assert!(!c.light, "bg=0 — тёмный фон");
        let c = Caps::detect(env(&[("COLORFGBG", "0;15")]), Overrides::default());
        assert!(c.light, "bg=15 — светлый фон");
        let c = Caps::detect(env(&[("COLORFGBG", "0;15")]), Overrides::default());
        assert!(c.light);
        let o = Overrides {
            theme: Some(ThemeChoice::Dark),
            ..Overrides::default()
        };
        let c = Caps::detect(env(&[("COLORFGBG", "0;15")]), o);
        assert!(!c.light, "явный --theme dark перебивает COLORFGBG");
    }

    #[test]
    fn cli_flags_beat_config_defaults() {
        let cfg = ConfigDefaults {
            theme: Some(ThemeChoice::Light),
            mouse: false,
            animation: false,
            unicode: false,
        };
        // Конфиг применяется, когда CLI молчит.
        let o = Overrides::default().with_defaults(cfg);
        assert_eq!(o.theme, Some(ThemeChoice::Light));
        assert!(o.no_mouse && o.no_animation && o.ascii);
        // Явный флаг CLI важнее конфига.
        let o = Overrides {
            theme: Some(ThemeChoice::Dark),
            no_mouse: false,
            ..Overrides::default()
        }
        .with_defaults(cfg);
        assert_eq!(
            o.theme,
            Some(ThemeChoice::Dark),
            "--theme важнее [tui].theme"
        );
        assert!(
            o.no_mouse,
            "конфиг всё ещё выключает мышь: флаг её не включал"
        );
    }

    #[test]
    fn config_defaults_do_not_invent_restrictions() {
        // Дефолт конфига ничего не запрещает: поведение = эвристика окружения.
        let o = Overrides::default().with_defaults(ConfigDefaults::default());
        assert!(!o.no_mouse && !o.no_animation && !o.ascii && o.theme.is_none());
    }

    #[test]
    fn theme_choice_parse() {
        assert_eq!(ThemeChoice::parse("Light"), Some(ThemeChoice::Light));
        assert_eq!(ThemeChoice::parse(" auto "), Some(ThemeChoice::Auto));
        assert_eq!(ThemeChoice::parse("solarized"), None);
        assert_eq!(ThemeChoice::Light.name(), "light");
        assert_eq!(
            ThemeChoice::parse("high-contrast"),
            Some(ThemeChoice::HighContrast)
        );
        assert_eq!(ThemeChoice::parse("hc"), Some(ThemeChoice::HighContrast));
        assert_eq!(ThemeChoice::HighContrast.name(), "high-contrast");
    }

    #[test]
    fn high_contrast_choice_sets_flag_not_light() {
        let o = Overrides {
            theme: Some(ThemeChoice::HighContrast),
            ..Overrides::default()
        };
        let c = Caps::detect(env(&[("COLORFGBG", "0;15")]), o);
        assert!(c.high_contrast);
        assert!(!c.light, "HC не включает светлую палитру");
    }

    #[test]
    fn ansi256_mapping_is_stable() {
        assert_eq!(rgb_to_ansi256(0, 0, 0), 16);
        assert_eq!(rgb_to_ansi256(255, 255, 255), 231);
        assert_eq!(
            rgb_to_ansi256(0x1a, 0x1b, 0x26),
            16,
            "тёмно-серый — начало куба"
        );
        // Цветные значения дают индекс из куба 16..=231.
        for (r, g, b) in [(0x7d, 0xcf, 0xff), (0xf7, 0x76, 0x8e), (0x9e, 0xce, 0x6a)] {
            let i = rgb_to_ansi256(r, g, b);
            assert!((16..=231).contains(&i), "{r},{g},{b} → {i}");
        }
        // Серая рампа — монотонные индексы.
        let greys: Vec<u8> = [9u8, 60, 120, 200, 245]
            .iter()
            .map(|v| rgb_to_ansi256(*v, *v, *v))
            .collect();
        assert!(greys.windows(2).all(|w| w[0] < w[1]), "{greys:?}");
    }
}
