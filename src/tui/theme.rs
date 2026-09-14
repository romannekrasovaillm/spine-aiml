//! Палитры и стили TUI: тёмная (Tokyo Night) и светлая (Tokyo Night Day),
//! с деградацией по ярусу цвета терминала, плюс набор глифов (Unicode/ASCII).
//!
//! Компоненты не знают литеральных цветов — только семантические роли
//! (`accent`, `muted`, `error`, …). Какой конкретно цвет получит роль, решают
//! [`Caps`]: truecolor → Tokyo Night, 256 → ближайший индекс xterm,
//! 16 → базовые ANSI-имена (фон не переопределяем — контраст обеспечивает
//! тема пользователя), без цвета → `Reset` и атрибуты.

use ratatui::style::{Color, Modifier, Style};
use ratatui::symbols::border::{self, Set};

use super::caps::{Caps, ColorLevel};

/// Роли палитры в truecolor-виде (до деградации по ярусу).
#[derive(Debug, Clone, Copy)]
struct Palette {
    bg: (u8, u8, u8),
    fg: (u8, u8, u8),
    accent: (u8, u8, u8),
    purple: (u8, u8, u8),
    green: (u8, u8, u8),
    orange: (u8, u8, u8),
    red: (u8, u8, u8),
    muted: (u8, u8, u8),
    /// Подсказка клавиши (номер варианта в ask-модалке).
    num_key: (u8, u8, u8),
    code_bg: (u8, u8, u8),
    code_fg: (u8, u8, u8),
    sel_bg: (u8, u8, u8),
}

/// Тёмная палитра Tokyo Night. `muted` светлее канонического `#565f89`
/// (контраст 2.8:1): подсказки и метки должны читаться, а не угадываться.
const DARK: Palette = Palette {
    bg: (0x1a, 0x1b, 0x26),
    fg: (0xc0, 0xca, 0xf5),
    accent: (0x7d, 0xcf, 0xff),
    purple: (0xbb, 0x9a, 0xf7),
    green: (0x9e, 0xce, 0x6a),
    orange: (0xff, 0x9e, 0x64),
    red: (0xf7, 0x76, 0x8e),
    muted: (0x6b, 0x73, 0x9e),
    num_key: (0x7a, 0xa2, 0xf7),
    code_bg: (0x24, 0x28, 0x3b),
    code_fg: (0xa9, 0xb1, 0xd6),
    sel_bg: (0x28, 0x34, 0x57),
};

/// Светлая палитра Tokyo Night Day. Значения затемнены относительно
/// канонических: цветной текст в TUI мелкий, поэтому каждая роль держит
/// контраст ≥ 4.5:1 к фону (проверяется тестом).
const LIGHT: Palette = Palette {
    bg: (0xe1, 0xe2, 0xe7),
    // `fg` затемнён относительно канонического Day-синего #3760bf (контраст
    // 4.5:1): основной текст обязан быть САМОЙ контрастной ролью палитры.
    // Иначе в ask-модалке метка (`base()`) читалась хуже своего номера
    // (`number_key()`, 5.5:1) — подпись проигрывала служебной цифре.
    fg: (0x2d, 0x4a, 0x9e),
    accent: (0x0d, 0x6c, 0x8c),
    purple: (0x7a, 0x45, 0xc9),
    green: (0x4d, 0x6a, 0x30),
    orange: (0x8f, 0x54, 0x00),
    red: (0xc0, 0x1e, 0x52),
    muted: (0x5f, 0x67, 0x97),
    num_key: (0x2e, 0x4f, 0xb8),
    code_bg: (0xd2, 0xd4, 0xde),
    code_fg: (0x2f, 0x56, 0xab),
    sel_bg: (0xb9, 0xc6, 0xec),
};

/// Цветовая тема TUI: роли уже деградированы по ярусу [`ColorLevel`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Theme {
    /// Фон (в 16-цветном и монохромном режиме — `Reset`: фон задаёт терминал).
    pub(crate) bg: Color,
    /// Основной текст.
    pub(crate) fg: Color,
    /// Акцент: cyan.
    pub(crate) cyan: Color,
    /// Акцент: purple.
    pub(crate) purple: Color,
    /// Успех: green.
    pub(crate) green: Color,
    /// Предупреждение: orange.
    pub(crate) orange: Color,
    /// Ошибка: red.
    pub(crate) red: Color,
    /// Приглушённый текст: muted.
    pub(crate) muted: Color,
    /// Подсказка клавиши-номера (номер варианта в ask-модалке). Отдельно от
    /// `muted`: цифра — это не украшение строки, а сама команда, и обязана
    /// читаться (контраст к фону ≥ 4.5:1, мелкий текст).
    num_key: Color,
    /// Фон код-панели markdown.
    code_bg: Color,
    /// Текст код-панели markdown.
    code_fg: Color,
    /// Фон выделения мышью.
    sel_bg: Color,
    /// Набор глифов (Unicode либо ASCII-фолбэк).
    pub(crate) glyphs: Glyphs,
    /// Ярус цвета (для стилей, которым мало цвета: badge, selection).
    level: ColorLevel,
    /// Контрастная тема (`--theme high-contrast`): вместо DIM — BOLD
    /// (dim плохо читается слабовидящими, `tui-accessibility-compat` §7).
    high_contrast: bool,
}

impl Default for Theme {
    fn default() -> Self {
        Self::dark(ColorLevel::TrueColor)
    }
}

impl Theme {
    /// Тёмная тема (Tokyo Night) на заданном ярусе цвета.
    pub(crate) fn dark(level: ColorLevel) -> Self {
        Self::build(&DARK, level)
    }

    /// Светлая тема (Tokyo Night Day) на заданном ярусе цвета.
    pub(crate) fn light(level: ColorLevel) -> Self {
        Self::build(&LIGHT, level)
    }

    /// Контрастная тема для слабовидящих: цвета нет совсем (все роли
    /// `Reset`), вторичность и акценты несут BOLD/REVERSED, а не DIM.
    pub(crate) fn high_contrast() -> Self {
        let mut t = Self::build(&DARK, ColorLevel::Mono);
        t.high_contrast = true;
        t
    }

    /// Тема по возможностям терминала: палитра — по светлому фону, ярус
    /// цвета и набор глифов — из [`Caps`] (монохром и ASCII — разные оси:
    /// `NO_COLOR` не означает `LANG=C`).
    pub(crate) fn for_caps(caps: &Caps) -> Self {
        let mut theme = if caps.high_contrast {
            Self::high_contrast()
        } else if caps.light {
            Self::light(caps.color)
        } else {
            Self::dark(caps.color)
        };
        theme.glyphs = Glyphs {
            unicode: caps.unicode,
        };
        theme
    }

    /// Сборка темы: роли палитры деградируются по ярусу цвета,
    /// глифы — Unicode (ASCII-фолбэк ставит [`Self::for_caps`]).
    fn build(p: &Palette, level: ColorLevel) -> Self {
        let paint = |rgb: (u8, u8, u8), ansi: Color| -> Color {
            match level {
                ColorLevel::TrueColor => Color::Rgb(rgb.0, rgb.1, rgb.2),
                ColorLevel::Ansi256 => {
                    Color::Indexed(super::caps::rgb_to_ansi256(rgb.0, rgb.1, rgb.2))
                }
                ColorLevel::Ansi16 => ansi,
                ColorLevel::Mono => Color::Reset,
            }
        };
        Self {
            bg: paint(p.bg, Color::Reset),
            fg: paint(p.fg, Color::Reset),
            cyan: paint(p.accent, Color::Cyan),
            purple: paint(p.purple, Color::Magenta),
            green: paint(p.green, Color::Green),
            orange: paint(p.orange, Color::Yellow),
            red: paint(p.red, Color::Red),
            muted: paint(p.muted, Color::DarkGray),
            num_key: paint(p.num_key, Color::LightBlue),
            code_bg: paint(p.code_bg, Color::Reset),
            code_fg: paint(p.code_fg, Color::DarkGray),
            sel_bg: paint(p.sel_bg, Color::Reset),
            glyphs: Glyphs { unicode: true },
            level,
            high_contrast: false,
        }
    }

    /// Без цвета? (`NO_COLOR`, `TERM=dumb`) — тогда смысл несут символы и атрибуты.
    pub(crate) fn is_mono(&self) -> bool {
        self.level == ColorLevel::Mono
    }

    /// Базовый стиль: основной текст на фоне.
    pub(crate) fn base(&self) -> Style {
        Style::default().fg(self.fg).bg(self.bg)
    }

    /// Рамки блоков.
    pub(crate) fn border(&self) -> Style {
        Style::default().fg(self.muted).bg(self.bg)
    }

    /// Приглушённый текст (подсказки, сводки инструментов). В монохроме
    /// единственный доступный носитель вторичности — атрибут DIM; в
    /// контрастной теме — BOLD (dim плохо читается слабовидящими).
    pub(crate) fn muted(&self) -> Style {
        let s = Style::default().fg(self.muted).bg(self.bg);
        if self.high_contrast {
            s.add_modifier(Modifier::BOLD)
        } else if self.is_mono() {
            s.add_modifier(Modifier::DIM)
        } else {
            s
        }
    }

    /// Подсказка клавиши-номера (цифра варианта в ask-модалке).
    ///
    /// Отдельный токен, а не `muted()`: цифра в строке варианта — это не
    /// приглушённая метка, а сама команда выбора, поэтому она держит
    /// контраст к фону ≥ 4.5:1 (мелкий текст, правило
    /// `tui-color-theming`). В монохроме носитель вторичности — DIM, и
    /// номер намеренно рисуется БЕЗ него: `muted()` там тускнеет, а номер
    /// обязан остаться ярче метки. В контрастной теме — BOLD.
    pub(crate) fn number_key(&self) -> Style {
        let s = Style::default().fg(self.num_key).bg(self.bg);
        if self.high_contrast {
            s.add_modifier(Modifier::BOLD)
        } else {
            s
        }
    }

    /// Акцент cyan (команды, prompt).
    pub(crate) fn accent(&self) -> Style {
        Style::default().fg(self.cyan).bg(self.bg)
    }

    /// Заголовок (markdown `#`, имена блоков): жирный cyan.
    pub(crate) fn heading(&self) -> Style {
        self.accent().add_modifier(Modifier::BOLD)
    }

    /// Акцент purple (команды system-блоков, буллеты).
    pub(crate) fn purple(&self) -> Style {
        Style::default().fg(self.purple).bg(self.bg)
    }

    /// Код-блок markdown: мягкая панель (`bg_highlight` #24283b, текст
    /// #a9b1d6) — читается как редактор, без жёсткой инверсии. В монохроме
    /// панель задаётся атрибутом DIM: цвет фона недоступен; в контрастной
    /// теме — BOLD.
    pub(crate) fn code(&self) -> Style {
        let s = Style::default().fg(self.code_fg).bg(self.code_bg);
        if self.high_contrast {
            s.add_modifier(Modifier::BOLD)
        } else if self.is_mono() {
            s.add_modifier(Modifier::DIM)
        } else {
            s
        }
    }

    /// Текст ошибки: в монохроме — жирный (плюс символ `✗`/`x` на месте вызова).
    pub(crate) fn error(&self) -> Style {
        let s = Style::default().fg(self.red).bg(self.bg);
        if self.is_mono() {
            s.add_modifier(Modifier::BOLD)
        } else {
            s
        }
    }

    /// Бейдж (инверсия на акценте): имя модели в статус-баре. В монохроме
    /// инверсия — единственный способ отделить бейдж от строки.
    pub(crate) fn badge(&self) -> Style {
        if self.is_mono() {
            return Style::default()
                .fg(Color::Reset)
                .bg(Color::Reset)
                .add_modifier(Modifier::REVERSED | Modifier::BOLD);
        }
        Style::default()
            .fg(self.bg)
            .bg(self.cyan)
            .add_modifier(Modifier::BOLD)
    }

    /// ASCII-арт (mermaid-рендеры): зелёный отделяет схему от прозы.
    pub(crate) fn art(&self) -> Style {
        Style::default().fg(self.green).bg(self.bg)
    }

    /// Выделение мышью. В монохроме и 16 цветах — инверсия: она контрастна
    /// на любом фоне терминала.
    pub(crate) fn selection(&self) -> Style {
        match self.level {
            ColorLevel::TrueColor | ColorLevel::Ansi256 => {
                Style::default().fg(self.fg).bg(self.sel_bg)
            }
            ColorLevel::Ansi16 | ColorLevel::Mono => Style::default()
                .fg(Color::Reset)
                .bg(Color::Reset)
                .add_modifier(Modifier::REVERSED),
        }
    }
}

/// Набор глифов интерфейса: Unicode (по умолчанию) либо ASCII-фолбэк
/// (`--ascii`, `LANG=C`, старый conhost). Правило: любой Unicode-глиф
/// интерфейса имеет ASCII-двойник — иначе в «плохом» терминале экран
/// превращается в мусор.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Glyphs {
    /// Unicode-набор доступен.
    pub(crate) unicode: bool,
}

impl Glyphs {
    /// ASCII-набор (нужен тестам и явному `--ascii`).
    #[cfg(test)]
    pub(crate) fn ascii() -> Self {
        Self { unicode: false }
    }

    /// Набор символов рамки блоков. Unicode — скруглённые линии (текущий вид),
    /// ASCII — `+-|`: в `LANG=C`, старом conhost и части ssh-клиентов
    /// box-drawing не рендерится, и рамка превращается в мусор.
    pub(crate) fn border_set(self) -> Set {
        if self.unicode {
            border::ROUNDED
        } else {
            border::Set {
                top_left: "+",
                top_right: "+",
                bottom_left: "+",
                bottom_right: "+",
                vertical_left: "|",
                vertical_right: "|",
                horizontal_top: "-",
                horizontal_bottom: "-",
            }
        }
    }

    /// Кадры спиннера ожидания (брайль либо `|/-\`).
    pub(crate) fn spinner(self) -> &'static [&'static str] {
        if self.unicode {
            &["⠋", "⠙", "⠚", "⠞", "⠖", "⠦", "⠴", "⠲", "⠳", "⠓"]
        } else {
            &["|", "/", "-", "\\"]
        }
    }

    /// Кадры «дышащей точки» мыслей модели.
    pub(crate) fn pulse(self) -> &'static [&'static str] {
        if self.unicode {
            &[" ", "·", "•", "●", "•", "·"]
        } else {
            &[" ", ".", "o", "O", "o", "."]
        }
    }

    /// Инструмент выполнен.
    pub(crate) fn ok(self) -> &'static str {
        if self.unicode { "✓" } else { "+" }
    }

    /// Инструмент упал.
    pub(crate) fn err(self) -> &'static str {
        if self.unicode { "✗" } else { "x" }
    }

    /// Инструмент выполняется.
    pub(crate) fn running(self) -> &'static str {
        if self.unicode { "◌" } else { "*" }
    }

    /// Маркер элемента списка.
    pub(crate) fn bullet(self) -> &'static str {
        if self.unicode { "•" } else { "-" }
    }

    /// Маркер следующего элемента (карточка очереди, полоса вкладок).
    pub(crate) fn next_marker(self) -> &'static str {
        if self.unicode { "▶" } else { ">" }
    }

    /// Роль «пользователь» в диалоге.
    pub(crate) fn role_user(self) -> &'static str {
        if self.unicode { "●" } else { "*" }
    }

    /// Роль «ассистент» в диалоге.
    pub(crate) fn role_assistant(self) -> &'static str {
        if self.unicode { "◆" } else { "*" }
    }

    /// Системная заметка/«мысли» модели.
    pub(crate) fn note(self) -> &'static str {
        if self.unicode { "»" } else { ">" }
    }

    /// Гуттер реплики пользователя.
    pub(crate) fn gutter(self) -> &'static str {
        if self.unicode { "▎" } else { "|" }
    }

    /// Имена стрелок в подсказках клавиш.
    pub(crate) fn up_down(self) -> &'static str {
        if self.unicode { "↑/↓" } else { "Up/Down" }
    }

    /// Имена горизонтальных стрелок в подсказках клавиш.
    pub(crate) fn left_right(self) -> &'static str {
        if self.unicode { "←→" } else { "Left/Right" }
    }

    /// Курсор активной строки списка / prompt ввода.
    pub(crate) fn cursor(self) -> &'static str {
        if self.unicode { "›" } else { ">" }
    }

    /// Текущий элемент (звёздочка в пикерах).
    pub(crate) fn star(self) -> &'static str {
        if self.unicode { "★" } else { "*" }
    }

    /// Полная ячейка шкалы.
    pub(crate) fn gauge_full(self) -> &'static str {
        if self.unicode { "▰" } else { "#" }
    }

    /// Пустая ячейка шкалы.
    pub(crate) fn gauge_empty(self) -> &'static str {
        if self.unicode { "▱" } else { "-" }
    }

    /// Бегунок полосы прокрутки.
    pub(crate) fn scroll_thumb(self) -> &'static str {
        if self.unicode { "█" } else { "#" }
    }

    /// Трек полосы прокрутки.
    pub(crate) fn scroll_track(self) -> &'static str {
        if self.unicode { "│" } else { "|" }
    }

    /// Кнопка «к свежему ответу» и иконка очереди.
    pub(crate) fn down(self) -> &'static str {
        if self.unicode { "▼" } else { "v" }
    }

    /// Иконка очереди сообщений.
    pub(crate) fn queue(self) -> &'static str {
        if self.unicode { "⏷" } else { "v" }
    }

    /// Метка «сообщение в несколько строк» в карточке очереди.
    pub(crate) fn fold(self) -> &'static str {
        if self.unicode { "↵" } else { "\\" }
    }

    /// Иконка индикатора контекста.
    pub(crate) fn context(self) -> &'static str {
        if self.unicode { "◈" } else { "#" }
    }

    /// Иконки вкладок правой панели (в порядке `RightTab::ALL`).
    ///
    /// Субагенты — `◉` (U+25C9, Geometric Shapes — тот же блок, что уже
    /// используемые `◇ ◈ ▶`). Требования к глифу вкладки (урок инцидента
    /// 07.09: редкий символ выпал в fontconfig-фолбэк и разъехался сеткой):
    /// одна ячейка, BMP и широкая моноширинная поддержка. Проверка на этой
    /// машине (`fc-list ":charset=<cp>" family | grep -ci mono`): `◉` = 8
    /// семейств против 2 у `✦`/`☰` — редкие глифы отвергнуты. ASCII-двойник
    /// `@` — намёк на адрес/хэндл агента, всегда одна ячейка.
    pub(crate) fn tab_icons(self) -> [&'static str; 5] {
        if self.unicode {
            ["◇", "✓", "◈", "▶", "◉"]
        } else {
            ["<>", "+", "#", ">", "@"]
        }
    }

    /// Включённый ризонинг в бейдже модели.
    pub(crate) fn think_on(self) -> &'static str {
        if self.unicode { "●" } else { "+" }
    }

    /// Явно выключенный ризонинг (суффикс `off` добавляет вызывающий).
    pub(crate) fn think_off(self) -> &'static str {
        if self.unicode { "○" } else { "-" }
    }

    /// Многоточие (обрезка «…», счётчики).
    pub(crate) fn ellipsis(self) -> &'static str {
        if self.unicode { "…" } else { "..." }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn high_contrast_uses_bold_instead_of_dim() {
        let t = Theme::high_contrast();
        assert_eq!(t.muted().fg, Some(Color::Reset));
        assert!(t.muted().add_modifier.contains(Modifier::BOLD));
        assert!(!t.muted().add_modifier.contains(Modifier::DIM));
        assert!(t.code().add_modifier.contains(Modifier::BOLD));
        assert!(!t.code().add_modifier.contains(Modifier::DIM));
        assert!(t.badge().add_modifier.contains(Modifier::REVERSED));
        assert!(t.selection().add_modifier.contains(Modifier::REVERSED));
        assert!(t.error().add_modifier.contains(Modifier::BOLD));
        // Палитра не задана: роли — фон/текст терминала.
        for c in [
            t.bg, t.fg, t.cyan, t.purple, t.green, t.orange, t.red, t.muted,
        ] {
            assert_eq!(c, Color::Reset);
        }
    }

    #[test]
    fn for_caps_picks_high_contrast_theme() {
        let mut caps = Caps::default();
        caps.high_contrast = true;
        let t = Theme::for_caps(&caps);
        assert!(t.muted().add_modifier.contains(Modifier::BOLD));
        assert!(!t.muted().add_modifier.contains(Modifier::DIM));
    }

    #[test]
    fn tokyo_night_palette_is_exact() {
        let t = Theme::dark(ColorLevel::TrueColor);
        assert_eq!(t.bg, Color::Rgb(0x1a, 0x1b, 0x26));
        assert_eq!(t.fg, Color::Rgb(0xc0, 0xca, 0xf5));
        assert_eq!(t.cyan, Color::Rgb(0x7d, 0xcf, 0xff));
        assert_eq!(t.purple, Color::Rgb(0xbb, 0x9a, 0xf7));
        assert_eq!(t.green, Color::Rgb(0x9e, 0xce, 0x6a));
        assert_eq!(t.orange, Color::Rgb(0xff, 0x9e, 0x64));
        assert_eq!(t.red, Color::Rgb(0xf7, 0x76, 0x8e));
        assert_eq!(t.muted, Color::Rgb(0x6b, 0x73, 0x9e));
        assert_eq!(Theme::default(), t, "дефолт — тёмная truecolor-тема");
    }

    #[test]
    fn light_palette_differs_from_dark() {
        let d = Theme::dark(ColorLevel::TrueColor);
        let l = Theme::light(ColorLevel::TrueColor);
        for (name, a, b) in [
            ("bg", d.bg, l.bg),
            ("fg", d.fg, l.fg),
            ("accent", d.cyan, l.cyan),
            ("muted", d.muted, l.muted),
        ] {
            assert_ne!(a, b, "роль {name} должна отличаться в светлой теме");
        }
    }

    #[test]
    fn for_caps_picks_palette_glyphs_and_level() {
        let mut caps = Caps::default();
        assert!(
            !Theme::for_caps(&caps).light_marker(),
            "тёмный фон по умолчанию"
        );
        assert!(Theme::for_caps(&caps).glyphs.unicode);
        caps.light = true;
        assert!(Theme::for_caps(&caps).light_marker());
        caps.unicode = false;
        caps.color = ColorLevel::Ansi16;
        let t = Theme::for_caps(&caps);
        assert!(!t.glyphs.unicode, "ASCII-фолбэк доходит до темы");
        assert_eq!(
            t.glyphs.border_set().top_left,
            "+",
            "ASCII-рамка дошла до темы"
        );
        assert_eq!(t.cyan, Color::Cyan, "ярус цвета тоже доходит");
    }

    #[test]
    fn ansi16_keeps_terminal_background() {
        let t = Theme::dark(ColorLevel::Ansi16);
        assert_eq!(t.fg, Color::Reset, "16 цветов: текст — цвет терминала");
        assert_eq!(
            t.bg,
            Color::Reset,
            "16 цветов: фон терминала не переопределяем"
        );
        assert_eq!(t.cyan, Color::Cyan);
        assert_eq!(t.purple, Color::Magenta);
        assert_eq!(t.green, Color::Green);
        assert_eq!(t.orange, Color::Yellow);
        assert_eq!(t.red, Color::Red);
        assert_eq!(t.muted, Color::DarkGray);
        assert_eq!(t.error().fg, Some(Color::Red));
        assert!(!t.error().add_modifier.contains(Modifier::BOLD));
    }

    #[test]
    fn mono_styles_carry_attributes() {
        let t = Theme::dark(ColorLevel::Mono);
        assert!(t.is_mono());
        for (name, s) in [
            ("base", t.base()),
            ("muted", t.muted()),
            ("error", t.error()),
            ("code", t.code()),
            ("selection", t.selection()),
        ] {
            assert_eq!(s.fg, Some(Color::Reset), "{name}: без цвета");
            assert_eq!(s.bg, Some(Color::Reset), "{name}: фон терминала");
        }
        // Вторичность и ошибка в монохроме несут атрибуты, а не цвет.
        assert!(t.muted().add_modifier.contains(Modifier::DIM));
        assert!(t.error().add_modifier.contains(Modifier::BOLD));
        assert!(t.selection().add_modifier.contains(Modifier::REVERSED));
        assert!(t.badge().add_modifier.contains(Modifier::REVERSED));
        assert!(t.heading().add_modifier.contains(Modifier::BOLD));
        // Монохром не меняет набор глифов: «нет цвета» ≠ «нет UTF-8».
        assert_eq!(t.glyphs.ok(), "✓");
    }

    #[test]
    fn ansi256_uses_indexed_colors() {
        let t = Theme::dark(ColorLevel::Ansi256);
        assert!(matches!(t.cyan, Color::Indexed(_)));
        assert!(matches!(t.bg, Color::Indexed(_)));
        assert!(
            t.selection().bg.is_some(),
            "выделение на 256 — заданный фон"
        );
        assert!(!t.selection().add_modifier.contains(Modifier::REVERSED));
    }

    #[test]
    fn ascii_glyphs_are_pure_ascii() {
        let g = Glyphs::ascii();
        let mut all: Vec<&str> = vec![
            g.ok(),
            g.err(),
            g.running(),
            g.bullet(),
            g.next_marker(),
            g.role_user(),
            g.role_assistant(),
            g.note(),
            g.gutter(),
            g.up_down(),
            g.left_right(),
            g.cursor(),
            g.star(),
            g.gauge_full(),
            g.gauge_empty(),
            g.scroll_thumb(),
            g.scroll_track(),
            g.down(),
            g.queue(),
            g.fold(),
            g.context(),
            g.think_on(),
            g.think_off(),
            g.ellipsis(),
        ];
        all.extend(g.spinner().iter().copied());
        all.extend(g.pulse().iter().copied());
        all.extend(g.tab_icons());
        for s in all {
            assert!(s.is_ascii(), "глиф {s:?} не ASCII — сломает LANG=C");
        }
        assert_eq!(g.border_set().top_left, "+");
        assert_eq!(g.border_set().vertical_left, "|");
    }

    #[test]
    fn unicode_glyphs_keep_previous_look() {
        let g = Glyphs { unicode: true };
        assert_eq!(g.border_set(), border::ROUNDED);
        assert_eq!(g.ok(), "✓");
        assert_eq!(g.err(), "✗");
        assert_eq!(g.running(), "◌");
        assert_eq!(g.cursor(), "›");
        assert_eq!(g.gauge_full(), "▰");
        assert_eq!(g.gauge_empty(), "▱");
        assert_eq!(g.spinner()[0], "⠋");
        assert_eq!(g.pulse()[3], "●");
        assert_eq!(g.tab_icons(), ["◇", "✓", "◈", "▶", "◉"]);
    }

    /// Число иконок обязано совпадать с числом вкладок `RightTab::ALL`:
    /// иначе `tab_icons()[i]` в баре панели паникует на новой вкладке.
    #[test]
    fn tab_icons_cover_every_right_tab() {
        use crate::tui::app::RightTab;
        let u = Glyphs { unicode: true };
        let a = Glyphs { unicode: false };
        assert_eq!(u.tab_icons().len(), RightTab::ALL.len());
        assert_eq!(a.tab_icons().len(), RightTab::ALL.len());
    }

    /// Контраст текста к фону по WCAG 2.x: обычный текст ≥ 4.5:1,
    /// вторичный (muted) ≥ 3:1.
    #[test]
    fn palette_meets_contrast_floor() {
        for (name, p) in [("dark", &DARK), ("light", &LIGHT)] {
            assert!(
                contrast(p.fg, p.bg) >= 4.5,
                "{name}: fg {:?}",
                contrast(p.fg, p.bg)
            );
            assert!(contrast(p.accent, p.bg) >= 4.5, "{name}: accent");
            assert!(contrast(p.purple, p.bg) >= 4.5, "{name}: purple");
            assert!(contrast(p.green, p.bg) >= 4.5, "{name}: green");
            assert!(contrast(p.orange, p.bg) >= 4.5, "{name}: orange");
            assert!(contrast(p.red, p.bg) >= 4.5, "{name}: red");
            assert!(contrast(p.muted, p.bg) >= 3.0, "{name}: muted");
            // Цифра варианта — команда, а не метка: держит порог мелкого
            // текста 4.5:1 (в отличие от muted).
            assert!(
                contrast(p.num_key, p.bg) >= 4.5,
                "{name}: num_key {:?}",
                contrast(p.num_key, p.bg)
            );
            // Иерархия ask-модалки: метка варианта (`base()` = fg) — контент,
            // номер (`number_key()` = num_key) — лишь подсказка клавиши.
            // Подпись обязана читаться НЕ хуже своего номера, иначе главный
            // текст строки проигрывает служебной цифре (исходный дефект:
            // метка `muted()` 3:1 против номера 4.5:1). Порядок зафиксирован
            // вот этим сравнением.
            assert!(
                contrast(p.fg, p.bg) >= contrast(p.num_key, p.bg),
                "{name}: метка {:?} бледнее номера {:?}",
                contrast(p.fg, p.bg),
                contrast(p.num_key, p.bg)
            );
            assert!(contrast(p.code_fg, p.code_bg) >= 4.5, "{name}: код-панель");
        }
    }

    /// В монохроме носитель вторичности — атрибут DIM. Метка ask-варианта
    /// (`base`) и номер (`number_key`) оба обязаны идти БЕЗ него: иначе
    /// подпись строки тусклее собственного номера — та же инверсия иерархии,
    /// что и по контрасту, только средствами атрибутов.
    #[test]
    fn mono_label_is_not_dimmer_than_number() {
        let t = Theme::dark(ColorLevel::Mono);
        assert!(!t.base().add_modifier.contains(Modifier::DIM));
        assert!(!t.number_key().add_modifier.contains(Modifier::DIM));
        // Контроль: приглушённый стиль DIM несёт — потому метке его и нельзя.
        assert!(t.muted().add_modifier.contains(Modifier::DIM));
    }

    /// Относительная яркость и коэффициент контраста WCAG.
    fn contrast(a: (u8, u8, u8), b: (u8, u8, u8)) -> f64 {
        let lum = |(r, g, b): (u8, u8, u8)| -> f64 {
            let f = |c: u8| -> f64 {
                let c = f64::from(c) / 255.0;
                if c <= 0.039_28 {
                    c / 12.92
                } else {
                    ((c + 0.055) / 1.055).powf(2.4)
                }
            };
            0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
        };
        let (la, lb) = (lum(a), lum(b));
        let (hi, lo) = if la > lb { (la, lb) } else { (lb, la) };
        (hi + 0.05) / (lo + 0.05)
    }

    impl Theme {
        /// Признак светлой палитры (для теста `for_caps`).
        fn light_marker(&self) -> bool {
            self.bg == Color::Rgb(LIGHT.bg.0, LIGHT.bg.1, LIGHT.bg.2)
        }
    }
}
