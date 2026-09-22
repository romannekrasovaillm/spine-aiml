//! Отрисовка TUI: сплэш с градиентным баннером, чат (диалог | вкладки),
//! строка ввода, статус-бар. Все блоки — со скруглёнными рамками.
//!
//! Правило ASCII-арта: строки mermaid-рендеров (box-drawing U+2500–257F,
//! геометрия U+25A0–25FF) НИКОГДА не переносятся — разрыв box-линий убивает
//! диаграмму; длинное клипается по ширине панели.

use std::path::Path;

use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, Borders, Clear, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState, Wrap,
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::assets;

use super::app::{App, ChatBlock, Panels, RightTab, Screen, ToastLevel, ToolLive, ToolState};
use super::text::{markdown_lines, wrap_line};
use super::theme::Theme;

/// Базовая ширина правой колонки (вкладки). 42 = внутренние 40 ячеек: чат —
/// главный экран (при канонических 80×24 ему остаётся 38 клеток против 25 при
/// 55), а бар вкладок честно деградирует в компактный режим «иконки + подпись
/// активной». Полный бар из пяти подписей требует 53 ячейки (юникод) / 54
/// (ASCII: `< >` — две клетки) и в 40 не влезает ни на какой ширине терминала,
/// поэтому деградация здесь не зависит от `term_w` — это цена за широкий чат.
/// Раньше база была 55 «под полный бар», но это отнимало у чата треть ширины
/// на 80×24 ради подписей, которые и так доступны как иконки с подсказкой.
/// Число 42, а не произвольное: при 43 внутренняя ширина диалога падает до 41
/// и первая строка баннера (43 клетки) начинает обрезаться.
const RIGHT_WIDTH: u16 = 42;
/// Минимум, который обязан получить диалог (tui-layout-components: главная
/// область не сжимается ниже своего минимума ради боковой панели).
const DIALOG_MIN_WIDTH: u16 = 24;
/// Минимум, при котором правая панель ещё читаема: рамка (2) + заголовок
/// вкладки. Ниже — панель прячется, диалог забирает экран целиком.
const RIGHT_MIN_WIDTH: u16 = 28;
/// Минимум клеток под подсказки клавиш в статус-баре. В 48 помещаются
/// `Enter отправить · Esc прервать · ? помощь · q выход` — первичные действия;
/// половины строки на 60 колонках (30) хватало только на `?`/`q`, и Enter/Esc
/// исчезали (обнаруживаемость не должна зависеть от длины соседних сегментов).
const STATUS_HINTS_MIN: usize = 48;
/// Сколько клеток статус-бара обязано остаться под модель и контекст при
/// сколь угодно длинных подсказках.
const STATUS_LEFT_MIN: usize = 12;
/// Максимум строк блока «мысли» в диалоге (компактность; хвост — счётчиком).
const MAX_THINKING_LINES: usize = 6;
/// Замедление пульса «модель думает»: кадр раз в N тиков тикера (120 мс).
const PULSE_TICK_DIVISOR: usize = 4;
/// Хвост живого вывода в блоке инструмента: строк максимум.
const TOOL_TAIL_LINES: usize = 3;
/// Хвост живого вывода: символов в строке максимум (дальше — «…»).
const TOOL_TAIL_LINE_CHARS: usize = 110;

/// Длительность как `M:SS` (до часа) или `H:MM:SS` — для живых таймеров
/// («модель думает · 1:23», «выполняется: bash · 0:42»).
pub(crate) fn fmt_elapsed(secs: u64) -> String {
    if secs >= 3600 {
        format!("{}:{:02}:{:02}", secs / 3600, secs % 3600 / 60, secs % 60)
    } else {
        format!("{}:{:02}", secs / 60, secs % 60)
    }
}

/// Обрезает строку до `max` символов, добавляя «…» при усечении.
fn truncate_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let cut: String = s.chars().take(max.saturating_sub(1)).collect();
    format!("{cut}…")
}

/// Обрезает строку метки до `max` ЯЧЕЕК (не символов: кириллица и глифы
/// шириной 2 не должны разъезжаться), добавляя многоточие темы (`…`/`...`).
///
/// Зачем отдельно от [`truncate_chars`]: метка ask-варианта делит строку со
/// звёздочкой рекомендации, и усечение обязано оставить место ИМЕННО под неё.
/// Раньше длинная метка вытесняла `★` за край панели — рекомендация терялась
/// первой, хотя это самый важный знак строки. Считаем в ячейках, чтобы
/// обрезка совпала с тем, что реально нарисует терминал.
fn fit_label_cells(s: &str, max: usize, dots: &str) -> String {
    if UnicodeWidthStr::width(s) <= max {
        return s.to_string();
    }
    let dots_w = UnicodeWidthStr::width(dots);
    if max <= dots_w {
        // Даже под многоточие места нет — отдаём его целиком: лучше знак
        // усечения, чем молча срезанный хвост без намёка на продолжение.
        return dots.to_string();
    }
    let budget = max - dots_w;
    let mut out = String::new();
    let mut used = 0usize;
    for ch in s.chars() {
        let w = UnicodeWidthChar::width(ch).unwrap_or(0);
        if used + w > budget {
            break;
        }
        out.push(ch);
        used += w;
    }
    out.push_str(dots);
    out
}

/// Грубая оценка токенов и скорости стрима, (токены, ток/с): 4 байта ≈
/// 1 токен — та же конвенция, что `ChatMessage::rough_tokens`; скорость
/// считается с 0,5 с стрима (раньше — шум деления на миллисекунды).
fn stream_tok_rate(bytes: usize, secs: f64) -> (usize, usize) {
    #[allow(clippy::cast_sign_loss, clippy::cast_possible_truncation)]
    let rate = if secs >= 0.5 {
        (bytes as f64 / secs / 4.0) as usize
    } else {
        0
    };
    (bytes / 4, rate)
}

/// Ширина правой колонки: под широкий mermaid-арт панель растёт (до 60%
/// терминала), чтобы схема помещалась целиком, без горизонтального клипа.
/// На остальных вкладках — фиксированная [`RIGHT_WIDTH`].
///
/// Возвращается ФАКТИЧЕСКИ выделяемая ширина, а не номинал: на узком
/// терминале `Length(42)` в паре с `Min(24)` выигрывал и сжимал диалог до
/// 18 клеток — `Min` не защищает от превышения суммы. Поэтому здесь панель
/// уступает диалогу его минимум, сжимается до [`RIGHT_MIN_WIDTH`] и, если
/// и этого не хватает, скрывается совсем (0) — диалог забирает весь экран
/// (tui-design-principles, адаптивность: минимум размера + брейкпоинты).
fn right_panel_width(app: &App, term_w: u16) -> u16 {
    let room = term_w.saturating_sub(DIALOG_MIN_WIDTH);
    if room < RIGHT_MIN_WIDTH {
        return 0;
    }
    nominal_right_width(app, term_w).min(room)
}

/// Номинал ширины панели без учёта тесноты терминала.
fn nominal_right_width(app: &App, term_w: u16) -> u16 {
    if app.right_tab() != RightTab::Mermaid {
        return RIGHT_WIDTH;
    }
    let art_w = app
        .panels
        .mermaid
        .lines()
        .map(UnicodeWidthStr::width)
        .max()
        .unwrap_or(0);
    // +2 — рамка панели; диалог остаётся главным экраном (кап 60%).
    let want = u16_sat(art_w.saturating_add(2));
    let cap = u16_sat(usize::from(term_w) * 3 / 5).max(RIGHT_WIDTH);
    want.clamp(RIGHT_WIDTH, cap)
}

/// usize → u16 с насыщением (без паник на гигантских терминалах).
fn u16_sat(v: usize) -> u16 {
    u16::try_from(v).unwrap_or(u16::MAX)
}

/// Текст похож на ASCII-арт (box-линии/геометрические фигуры)?
/// Гуттеры чата (▎ U+258E) и галки (✓ U+2713) вне диапазонов — не арт.
fn is_art(text: &str) -> bool {
    text.chars()
        .any(|c| ('\u{2500}'..='\u{257f}').contains(&c) || ('\u{25a0}'..='\u{25ff}').contains(&c))
}

/// Строка (со спанами) — часть диаграммы?
fn line_is_art(line: &Line<'_>) -> bool {
    line.spans.iter().any(|s| is_art(&s.content))
}

/// Рисует текущий экран приложения.
pub(crate) fn draw(f: &mut Frame, app: &mut App) {
    let theme = app.theme;
    let area = f.area();
    f.render_widget(Block::default().style(theme.base()), area);
    // Чат ниже минимума не рисуем: панели схлопываются в кашу, а «Диалог»
    // с Min(24) и панель Length(34) спорят за одну строку (AP2). Показываем
    // заглушку с размерами — она честнее мусора. Fatal рисуем всегда: это
    // последний экран, на котором видно причину сбоя.
    if matches!(app.screen, Screen::Chat)
        && (area.width < app.caps.min_width || area.height < app.caps.min_height)
    {
        draw_too_small(f, area, app, &theme);
        return;
    }
    match &app.screen {
        Screen::Fatal(_) => draw_fatal(f, area, app, &theme),
        Screen::Chat if app.intro.is_some() => super::intro::draw(f, app),
        Screen::Chat => draw_chat(f, app),
    }
}

/// Заглушка «терминал мал»: текущий и требуемый размер, способ выйти.
/// Рисуется без рамок — рамка на таком экране съела бы остаток места.
fn draw_too_small(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    let lines = vec![
        Line::from(Span::styled("Терминал мал", theme.heading())),
        Line::default(),
        Line::from(Span::styled(
            format!(
                "сейчас {}×{} · нужно {}×{}",
                area.width, area.height, app.caps.min_width, app.caps.min_height
            ),
            theme.base(),
        )),
        Line::from(Span::styled(
            "Разверните окно — или q для выхода.",
            theme.muted(),
        )),
    ];
    let rows = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(u16_sat(lines.len())),
        Constraint::Min(0),
    ])
    .split(area);
    f.render_widget(Paragraph::new(lines).alignment(Alignment::Center), rows[1]);
}

/// Строки логотипа `ArchSpine` для блока диалога: вордмарк — акцентом,
/// редакция и подпись — приглушённо. Один акцент вместо посимвольного
/// градиента: градиент — декор, который спорит с содержанием и хуже
/// переносится между терминалами (принцип сдержанности, AP10).
fn logo_lines(theme: &Theme) -> Vec<Line<'static>> {
    assets::BANNER
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            // Вордмарк и строка редакции — акцентом, остальное приглушённо.
            if l.contains('█') || l.contains("EDITION") {
                Line::from(Span::styled(
                    l.to_string(),
                    Style::default()
                        .fg(theme.cyan)
                        .bg(theme.bg)
                        .add_modifier(Modifier::BOLD),
                ))
            } else {
                Line::from(Span::styled(l.to_string(), theme.muted()))
            }
        })
        .collect()
}

/// Экран фатальной ошибки инициализации (модель/конфиг).
fn draw_fatal(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    let Screen::Fatal(error) = &app.screen else {
        return;
    };
    let width = area.width.saturating_sub(4).clamp(20, 72);
    let cols = Layout::horizontal([
        Constraint::Min(0),
        Constraint::Length(width),
        Constraint::Min(0),
    ])
    .split(area);
    let rows = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(9),
        Constraint::Min(0),
    ])
    .split(cols[1]);

    let block = Block::default()
        .borders(Borders::ALL)
        .border_set(theme.glyphs.border_set())
        .border_style(theme.error())
        .title(Span::styled(
            " Ошибка инициализации ",
            Style::default()
                .fg(theme.red)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD),
        ));
    let text = vec![
        Line::from(Span::styled(error.clone(), theme.error())),
        Line::default(),
        Line::from(Span::styled(
            "Проверьте config.toml (команда `arch-ml init`) и переменные окружения \
             с API-ключами (api_key_env у моделей).",
            theme.muted(),
        )),
        Line::default(),
        Line::from(Span::styled(
            match app.status_extra() {
                Some(note) => format!("{note} · q — выход"),
                // Ошибку часто нужно отнести в тикет: даём скопировать.
                None => format!("{} — скопировать ошибку · q — выход", theme.glyphs.cursor()),
            },
            theme.muted(),
        )),
    ];
    f.render_widget(
        Paragraph::new(text).block(block).wrap(Wrap { trim: false }),
        rows[1],
    );
}

/// Основной экран: диалог | вкладки; снизу ввод и статус-бар.
/// Поверх — модалка выбора вариантов (`propose_options`), если она открыта.
fn draw_chat(f: &mut Frame, app: &mut App) {
    let theme = app.theme;
    // Поле ввода многострочное: высота растёт с текстом (перенос по ширине
    // и явные '\n'), максимум MAX_INPUT_ROWS видимых строк. Рамки у поля нет:
    // она была четвёртой рамкой экрана и спорила с диалогом за акцент (AP1);
    // состояние («думает», очередь) переехало в строку над вводом.
    let input_w = usize::from(f.area().width).max(1);
    let input_h = {
        let (lines, _, _) = wrap_input(app.input.text(), app.input.cursor(), input_w);
        u16_sat(lines.len().min(MAX_INPUT_ROWS)).max(1)
    };
    let state_h = u16::from(app.has_input_state());
    let rows = Layout::vertical([
        Constraint::Min(6),
        Constraint::Length(state_h),
        Constraint::Length(input_h),
        Constraint::Length(1),
    ])
    .split(f.area());
    // Блокирующая ask-модалка забирает место у правой панели: пока она
    // открыта, панель не рисуется, а колонка диалога разворачивается на всю
    // ширину. Так рамка модалки гарантированно остаётся ВНУТРИ рамки панели
    // диалога — ни одну чужую рамку она не перечёркивает, и при этом не
    // приходится ужимать саму модалку до ширины колонки диалога (на 60–110
    // колонках это стоило бы сегментов подсказки: реестр клавиш не влезал).
    // Панель — хром, модалка — главное содержимое момента (AP1: главное не
    // приносится в жертву хрому). Взаимодействовать с панелью всё равно
    // нельзя: модалка блокирующая, ввод перехвачен.
    let modal_open = app.ask.is_some();
    let right_w = if app.right_visible && !modal_open {
        right_panel_width(app, f.area().width)
    } else {
        0
    };
    let cols = Layout::horizontal([
        Constraint::Min(DIALOG_MIN_WIDTH),
        Constraint::Length(right_w),
    ])
    .split(rows[0]);

    draw_dialog(f, cols[0], app, &theme);
    // Очередь сообщений — плавающей карточкой внизу окна логов (над вводом).
    if !app.queue.is_empty() {
        draw_queue_overlay(f, cols[0], app, &theme);
    }
    if right_w > 0 {
        draw_right(f, cols[1], app, &theme);
    }
    if state_h > 0 {
        draw_input_state(f, rows[1], app, &theme);
    }
    draw_input(f, rows[2], app, &theme);
    draw_status(f, rows[3], app, &theme);
    // Справка — поверх всего: она забирает фокус (закрывается `?`/Esc/q).
    if app.help {
        draw_help(f, f.area(), app, &theme);
        return;
    }
    // Просмотрщик вкладки — под модалкой выбора (она блокирующая).
    if app.viewer.is_some() {
        draw_viewer(f, f.area(), app, &theme);
    }
    if modal_open {
        // Область модалки — полоса диалога `rows[0]`. Когда модалка открыта,
        // она совпадает с рамкой панели диалога (правая панель не рисуется,
        // см. `right_w` выше), поэтому модалка центрируется внутри неё и не
        // пересекает ничьих рамок. Высота ужимается под `rows[0]`, так что
        // строка ввода и статус-бар остаются чистыми.
        draw_ask(f, rows[0], app, &theme);
    }
}

/// Оверлей справки (`?`): клавиши текущего экрана, сгруппированные по задаче
/// (реестр `keymap` — единый источник со строкой подсказок). Фон под окном
/// не затемняем: справка не блокирует сценарий, а подсказывает.
fn draw_help(f: &mut Frame, area: Rect, app: &mut App, theme: &Theme) {
    let sections = super::keymap::sections(app.help_ctx());
    // Ширина колонки описания: остальное — под клавиши и отступы.
    let width = area.width.saturating_sub(8).clamp(40, 84).min(area.width);
    let inner_w = usize::from(width.saturating_sub(4)).max(1);
    let keys_w = 16usize.min(inner_w.saturating_sub(12));

    let mut lines: Vec<Line<'static>> = Vec::new();
    for (i, (group, rows)) in sections.iter().enumerate() {
        if i > 0 {
            lines.push(Line::default());
        }
        lines.push(Line::from(Span::styled(
            group.title().to_string(),
            theme.heading(),
        )));
        for (keys, desc) in rows {
            // Длинное описание переносится с отступом под колонку описания.
            let wrapped = wrap_line(
                &Line::from(Span::styled((*desc).to_string(), theme.base())),
                inner_w.saturating_sub(keys_w + 3).max(8),
            );
            for (j, l) in wrapped.iter().enumerate() {
                let mut spans = Vec::new();
                if j == 0 {
                    spans.push(Span::styled(format!("  {keys:<keys_w$}  "), theme.accent()));
                } else {
                    spans.push(Span::styled(" ".repeat(2 + keys_w + 2), theme.base()));
                }
                spans.extend(l.spans.clone());
                lines.push(Line::from(spans));
            }
        }
    }

    let height = u16_sat(lines.len() + 4).min(area.height.saturating_sub(2));
    let max_scroll = lines
        .len()
        .saturating_sub(usize::from(height.saturating_sub(3)).max(1));
    app.set_help_scroll(app.help_scroll().min(max_scroll));
    let scroll = u16_sat(app.help_scroll());

    let cols = Layout::horizontal([
        Constraint::Min(0),
        Constraint::Length(width),
        Constraint::Min(0),
    ])
    .split(area);
    let rows = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(height),
        Constraint::Min(0),
    ])
    .split(cols[1]);
    let panel = rows[1];

    let block = Block::default()
        .borders(Borders::ALL)
        .border_set(theme.glyphs.border_set())
        .border_style(Style::default().fg(theme.cyan).bg(theme.bg))
        .style(theme.base())
        .title(Span::styled(
            format!(" {} Помощь · клавиши ", theme.glyphs.context()),
            Style::default()
                .fg(theme.cyan)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD),
        ));
    f.render_widget(Clear, panel);
    f.render_widget(
        Paragraph::new(lines).block(block).scroll((scroll, 0)),
        panel,
    );
}

/// Полноэкранный просмотрщик активной вкладки (F4): вся ширина терминала,
/// вертикальный скролл и ГОРИЗОНТАЛЬНАЯ панорама для широкого mermaid-арта.
/// Основной экран не перестраивается — viewer накрывает его слоем.
fn draw_viewer(f: &mut Frame, area: Rect, app: &mut App, theme: &Theme) {
    let tab = app.right_tab();
    let inner_w = usize::from(area.width.saturating_sub(2)).max(1);
    let view_h = usize::from(area.height.saturating_sub(2)).max(1);
    let content = app.panels.content(tab).to_string();

    let mut lines: Vec<Line<'static>> = Vec::new();
    if content.is_empty() {
        lines.push(Line::from(Span::styled(
            Panels::placeholder(tab),
            theme.muted(),
        )));
    } else if tab == RightTab::Mermaid {
        // Арт не переносим: клип по ширине окна (дальше — горизонтальный сдвиг).
        for l in content.lines() {
            let style = if is_art(l) { theme.art() } else { theme.base() };
            lines.push(Line::from(Span::styled(l.to_string(), style)));
        }
    } else {
        for l in content.lines() {
            lines.extend(wrap_line(
                &Line::from(Span::styled(l.to_string(), theme.base())),
                inner_w,
            ));
        }
    }

    // Клампы скролла по фактическим размерам содержимого.
    let mut v = app.viewer.unwrap_or_default();
    v.scroll_y = v.scroll_y.min(lines.len().saturating_sub(view_h));
    let max_line_w = lines
        .iter()
        .map(|l| {
            l.spans
                .iter()
                .map(|s| UnicodeWidthStr::width(s.content.as_ref()))
                .sum::<usize>()
        })
        .max()
        .unwrap_or(0);
    v.scroll_x = v.scroll_x.min(max_line_w.saturating_sub(inner_w));
    app.viewer = Some(v);

    let visible: Vec<Line> = lines
        .into_iter()
        .skip(v.scroll_y)
        .take(view_h)
        .map(|l| {
            if v.scroll_x == 0 {
                l
            } else {
                hclip_line(&l, v.scroll_x)
            }
        })
        .collect();

    let icon = theme.glyphs.tab_icons()[RightTab::ALL.iter().position(|t| *t == tab).unwrap_or(0)];
    let pos = if v.scroll_y > 0 || v.scroll_x > 0 {
        format!(" · +{} строк, →{} кол.", v.scroll_y, v.scroll_x)
    } else {
        String::new()
    };
    let title = format!(
        " {icon} {} — на весь экран{pos} · {} · PgUp/PgDn · {} · F4/Esc — назад ",
        tab.title(),
        theme.glyphs.up_down(),
        theme.glyphs.left_right()
    );
    let block = Block::default()
        .borders(Borders::ALL)
        .border_set(theme.glyphs.border_set())
        .border_style(Style::default().fg(theme.cyan).bg(theme.bg))
        .title(Span::styled(
            title,
            Style::default()
                .fg(theme.cyan)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD),
        ))
        .style(theme.base());
    f.render_widget(ratatui::widgets::Clear, area);
    f.render_widget(Paragraph::new(visible).block(block), area);
}

/// Горизонтальный срез строки (по спанам) с display-колонки `offset`:
/// unicode-width безопасно; box-линии и кириллица арта — width 1.
/// Широкий глиф, разрезанный границей оффсета, пропускается целиком.
fn hclip_line(line: &Line<'_>, offset: usize) -> Line<'static> {
    use unicode_width::UnicodeWidthChar;
    let mut col = 0usize;
    let mut spans: Vec<Span<'static>> = Vec::new();
    for span in &line.spans {
        let mut buf = String::new();
        for ch in span.content.chars() {
            let w = UnicodeWidthChar::width(ch).unwrap_or(0);
            let next = col + w;
            if next <= offset || col < offset {
                col = next;
                continue;
            }
            buf.push(ch);
            col = next;
        }
        if !buf.is_empty() {
            spans.push(Span::styled(buf, span.style));
        }
    }
    Line::from(spans)
}

/// Заголовок ask-модалки и хвост клавиши `Esc` для её подсказки — по виду
/// вопроса.
///
/// Вынесено из [`draw_ask`], потому что тот же хвост печатает строка состояния,
/// пока модалка открыта (статус-бар обязан повторять клавиши верхнего слоя, а
/// не чата). Один источник — один текст: иначе заголовок говорил бы «решение за
/// вами», а футер — «отмена».
fn ask_chrome(kind: crate::tui::app::AskKind, theme: &Theme) -> (String, &'static str) {
    let mark = theme.glyphs.context();
    match kind {
        crate::tui::app::AskKind::Tool => (
            format!(" {mark} решение за вами "),
            // Коротко: «Esc решить» честно называет отказ (инструмент решит
            // сам), а длинный хвост «решить агенту» вытеснял из подсказки
            // страничные клавиши на 80 колонках (обрезка по сегментам).
            " решить",
        ),
        crate::tui::app::AskKind::ModelPicker => (format!(" {mark} выбор модели "), " отмена"),
        crate::tui::app::AskKind::SessionPicker => (format!(" {mark} выбор сессии "), " отмена"),
    }
}

/// Модальная панель выбора вариантов (инструмент `propose_options)`:
/// центрированное окно поверх чата — вопрос, варианты, курсор, подсказки.
fn draw_ask(f: &mut Frame, area: Rect, app: &mut App, theme: &Theme) {
    let Some(ask) = &app.ask else {
        return;
    };
    let width = area.width.saturating_sub(6).clamp(40, 76).min(area.width);
    let inner_w = usize::from(width.saturating_sub(4)).max(1);
    // Высота: вопрос (с переносом) + варианты (по 2 строки: label + описание)
    // + разделители/подсказка + рамка.
    let question_h = wrap_line(
        &Line::from(Span::styled(ask.question.clone(), theme.base())),
        inner_w,
    )
    .len();
    let height = u16_sat(question_h + ask.options.len() * 2 + 5)
        .min(area.height.saturating_sub(2))
        .max(6);
    let cols = Layout::horizontal([
        Constraint::Min(0),
        Constraint::Length(width),
        Constraint::Min(0),
    ])
    .split(area);
    let rows = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(height),
        Constraint::Min(0),
    ])
    .split(cols[1]);
    let panel = rows[1];

    let (title, esc_hint) = ask_chrome(ask.kind, theme);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_set(theme.glyphs.border_set())
        .border_style(Style::default().fg(theme.cyan).bg(theme.bg))
        .title(Span::styled(
            title,
            Style::default()
                .fg(theme.cyan)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD),
        ))
        .style(theme.base());
    f.render_widget(ratatui::widgets::Clear, panel);
    f.render_widget(block, panel);
    let inner = Rect {
        x: panel.x + 2,
        y: panel.y + 1,
        width: panel.width.saturating_sub(4),
        height: panel.height.saturating_sub(2),
    };

    let question_lines = wrap_line(
        &Line::from(Span::styled(
            ask.question.clone(),
            Style::default()
                .fg(theme.fg)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD),
        )),
        inner_w,
    );
    // Опции — отдельный скроллируемый слой между фиксированными вопросом
    // (сверху) и подсказками клавиш (снизу): без скролла Paragraph клипал
    // хвост списка, и пункты ниже видимой области были недостижимы
    // (баг 05.09: пикер /resume не давал долистать до сессий 11+).
    let mut opt_lines: Vec<Line<'static>> = Vec::new();
    // Диапазон строк выбранного варианта — для автоскролла за курсором.
    let mut sel_start = 0usize;
    let mut sel_end = 0usize;
    // Ширина номера — по числу вариантов, ФИКСИРОВАННАЯ на весь список:
    // иначе «10. » сдвигал метку на клетку вправо (пункт с двузначным номером
    // читался иначе, чем соседи), а вместе с меткой ехала и звёздочка.
    let num_w = ask.options.len().max(1).to_string().len();
    for (i, opt) in ask.options.iter().enumerate() {
        let current = i == ask.selected;
        if current {
            sel_start = opt_lines.len();
        }
        let recommended = ask.recommended.as_deref() == Some(opt.label.as_str());
        // Маркер выбора — ТОЛЬКО ASCII `"> "` (не выбран — `"  "`), ровно
        // две ячейки. Здесь стоял Unicode-глиф: сначала U+276F «❯», затем
        // U+203A «›». Оба — структурно хрупкие: если глифа нет в шрифте,
        // fontconfig-фолбэк рисует его ВНЕ моноширинной сетки и затирает
        // соседние ячейки. Инцидент 07.09: «›» съел цифру «1.» первого
        // пункта ask-модалки — цифры вариантов пропали. Причина не в
        // конкретном глифе, а в том, что перед критичной цифрой вообще
        // стоял не-ASCII символ. ASCII-маркер закрывает класс инцидента
        // навсегда: `">"` и пробел есть в любом моноширинном шрифте.
        // НЕ возвращать сюда глиф и не переносить эту логику на
        // `theme.glyphs.cursor()` — он остаётся для курсора ввода.
        let (mark, row_style, num_style) = if current {
            let sel = Style::default()
                .fg(theme.cyan)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD);
            ("> ", sel, sel)
        } else {
            // Иерархия выправлена: МЕТКА (то, что выбирают) — основной текст
            // `base()` (контраст ≥ 4.5:1, в монохроме без DIM), а НОМЕР —
            // акцент формы `number_key()`. Раньше было наоборот: метка шла
            // `muted()` (3:1, в монохроме ещё и DIM), а номер — ярче метки,
            // то есть подпись читалась хуже собственного порядкового номера.
            ("  ", theme.base(), theme.number_key())
        };
        let star = if recommended {
            format!(" {}", theme.glyphs.star())
        } else {
            String::new()
        };
        // Метке — остаток строки после маркера (2), номера (`num_w + 2`) и
        // РЕЗЕРВА под звёздочку (2). Резерв считаем до усечения: звезда
        // рекомендации не должна уезжать за край, даже если метка длинная.
        let star_w = if recommended {
            UnicodeWidthStr::width(star.as_str())
        } else {
            0
        };
        let label_budget = inner_w.saturating_sub(2 + num_w + 2).saturating_sub(star_w);
        let label = fit_label_cells(&opt.label, label_budget, theme.glyphs.ellipsis());
        opt_lines.push(Line::from(vec![
            Span::styled(mark, row_style),
            Span::styled(format!("{:>num_w$}. ", i + 1), num_style),
            Span::styled(label, row_style),
            Span::styled(star, Style::default().fg(theme.orange).bg(theme.bg)),
        ]));
        if !opt.description.is_empty() {
            for extra in wrap_line(
                &Line::from(Span::styled(
                    format!("    {}", opt.description),
                    theme.muted(),
                )),
                inner_w,
            ) {
                opt_lines.push(extra);
            }
        }
        if current {
            sel_end = opt_lines.len();
        }
    }
    let n = ask.options.len();
    // Счётчик позиции (`N/M`) — ОТДЕЛЬНАЯ правоприжатая зона фиксированной
    // ширины, а не хвост подсказки. Раньше он дописывался в конец одной
    // строки, и при узкой панели Paragraph (без wrap) клипал справа первым
    // именно его — «часть контекста», ради которой модалка существует,
    // пропадала раньше, чем подсказка о стрелках. Теперь место под счётчик
    // резервируется ДО подсказки, и обрезаться может только подсказка.
    let counter = if n > 0 {
        format!("{}/{}", ask.selected + 1, n)
    } else {
        String::new()
    };
    let counter_w = u16_sat(UnicodeWidthStr::width(counter.as_str()));
    // Раскладка: вопрос (+пустая строка) сверху, подсказки — последняя
    // строка панели, опции — прокручиваемая середина.
    let header_h = u16::try_from(question_lines.len() + 1).unwrap_or(u16::MAX);
    let header = Rect {
        height: header_h.min(inner.height),
        ..inner
    };
    let footer = Rect {
        y: inner.y + inner.height.saturating_sub(1),
        height: 1,
        ..inner
    };
    let (hint_area, counter_area) = if counter_w == 0 {
        (
            footer,
            Rect {
                x: footer.x + footer.width,
                width: 0,
                ..footer
            },
        )
    } else {
        let cols = Layout::horizontal([
            Constraint::Min(0),
            Constraint::Length(counter_w.saturating_add(1)),
        ])
        .split(footer);
        (cols[0], cols[1])
    };
    let body = Rect {
        y: inner.y + header.height,
        height: inner.height.saturating_sub(header.height).saturating_sub(1),
        ..inner
    };
    // Окно прокрутки за курсором (минимальное): вниз — ровно до показа
    // описания выбранного, вверх — пункт в топ.
    let visible = usize::from(body.height);
    let total = opt_lines.len();
    // Запоминаем реальную высоту тела: app.rs берёт от неё шаг `PgUp`/`PgDn`
    // (был константой 10, не связанной с окном терминала).
    app.ask_viewport = visible;
    // Подсказка собирается ЗДЕСЬ, когда окно уже известно: страничные клавиши
    // обещаем только если список длиннее окна (обещать листать нечего листать
    // — ложь). Честная подсказка обещает только то, что клавиши реально делают
    // (app.rs, `handle_ask_key`).
    let hint_line = ask_hint_line(
        theme,
        n,
        esc_hint,
        total > visible,
        usize::from(hint_area.width),
    );
    let mut offset = 0usize;
    if visible > 0 && total > visible {
        offset = sel_start.min(total - visible);
        if sel_end > offset + visible {
            offset = sel_end - visible;
        }
    }
    let mut head_lines = question_lines;
    head_lines.push(Line::default());
    f.render_widget(Paragraph::new(head_lines), header);
    f.render_widget(Paragraph::new(vec![hint_line]), hint_area);
    if counter_w > 0 {
        f.render_widget(
            Paragraph::new(counter)
                .alignment(Alignment::Right)
                .style(theme.number_key()),
            counter_area,
        );
    }
    f.render_widget(
        Paragraph::new(opt_lines).scroll((u16::try_from(offset).unwrap_or(u16::MAX), 0)),
        body,
    );
}

/// Подсказка клавиш ask-модалки.
///
/// Порядок сегментов — по значимости слева направо, потому что `Paragraph`
/// без переноса обрезает строку СПРАВА: то, что обязано быть видно (навигация,
/// страничные клавиши при переполнении), стоит левее декоративного «быстрого»
/// диапазона и хвоста `Esc`. `paging` включает `PgUp`/`PgDn`/`Home`/`End`
/// только когда список длиннее окна: когда листать нечего, обещание листания —
/// такая же ложь, как молчание о нём, когда листать нужно. Текст подсказки
/// обязан совпадать с тем, что реально делает `handle_ask_key` (app.rs).
fn ask_hint_line(
    theme: &Theme,
    n: usize,
    esc_hint: &str,
    paging: bool,
    width: usize,
) -> Line<'static> {
    struct Seg {
        keys: String,
        label: String,
        /// Порядок отображения слева направо.
        display: u8,
        /// Порядок удержания: меньше — нужнее, уходит последним.
        keep: u8,
    }

    let key = |k: &str| Span::styled(k.to_string(), theme.heading());
    let sep = |t: &str| Span::styled(t.to_string(), theme.muted());

    // Подписи и стабильные клавиши берём из реестра клавиш: он объявляет
    // действие один раз, и подсказка не может обещать то, чего обработчик
    // не делает (см. `App::handle_ask_key`). Исключение — стрелки: их вид
    // зависит от набора глифов (↑/↓ или ^/v), а реестр статичен.
    let from_registry = |code: &str, fallback: &str, labels: bool| -> String {
        super::keymap::action(super::keymap::Ctx::Ask, code)
            .map_or(fallback, |b| if labels { b.label } else { b.keys })
            .to_string()
    };
    let label = |code: &str, fallback: &str| from_registry(code, fallback, true);
    let rkey = |code: &str, fallback: &str| from_registry(code, fallback, false);

    let mut segs: Vec<Seg> = Vec::new();
    if n == 0 {
        // Выбирать не из чего — единственное действие «ответить».
        segs.push(Seg {
            keys: rkey("enter", "Enter"),
            label: "ответить".to_string(),
            display: 2,
            keep: 0,
        });
    } else {
        // Стрелки — то, чем модалку листают; оставляем до последнего.
        segs.push(Seg {
            keys: theme.glyphs.up_down().to_string(),
            label: label("up", "выбор"),
            display: 0,
            keep: 4,
        });
        if paging {
            // Список длиннее окна: без этих клавиш хвост достаётся только по
            // одной строке. При переполнении диапазон цифр не показываем —
            // страница важнее.
            segs.push(Seg {
                keys: rkey("pageup", "PgUp/PgDn/Home/End"),
                label: label("pageup", "листать"),
                display: 1,
                keep: 3,
            });
        } else {
            let keys = if n == 1 {
                // «1-1» — диапазон из одного числа, читателю он ничего не
                // сообщает.
                "1".to_string()
            } else if n <= 9 {
                format!("1-{n}")
            } else {
                // Компактно и честно: «1-9 + ↑/↓» говорит, что клавиши есть
                // только у первых девяти, остальное — стрелками.
                format!("1-9 + {}", theme.glyphs.up_down())
            };
            segs.push(Seg {
                keys,
                label: label("1", "быстро"),
                display: 3,
                keep: 2,
            });
        }
        segs.push(Seg {
            keys: rkey("enter", "Enter"),
            label: label("enter", "подтвердить"),
            display: 2,
            keep: 0,
        });
    }
    segs.push(Seg {
        keys: rkey("esc", "Esc"),
        label: esc_hint.trim_start().to_string(),
        display: 4,
        keep: 1,
    });

    let seg_w = |s: &Seg| {
        UnicodeWidthStr::width(s.keys.as_str()) + 1 + UnicodeWidthStr::width(s.label.as_str())
    };
    let joined = |v: &[&Seg]| -> usize {
        v.iter().map(|s| seg_w(s)).sum::<usize>() + v.len().saturating_sub(1) * 3
    };

    // Обрезка — по сегментам, с многоточием: `Paragraph` без переноса резал
    // строку по клетке и рвал слово пополам («Enter подтвердить» → «Ent»).
    // Уходят первыми наименее нужные: сначала стрелки, потом листание, потом
    // цифры; Enter и Esc остаются всегда (без них модалка непонятна).
    let all: Vec<&Seg> = segs.iter().collect();
    let mut chosen: Vec<&Seg> = Vec::new();
    let mut truncated = false;
    if joined(&all) > width {
        truncated = true;
        let budget = width.saturating_sub(4); // « · …»
        let mut by_keep: Vec<&Seg> = segs.iter().collect();
        by_keep.sort_by_key(|s| s.keep);
        for s in by_keep {
            let mut cand = chosen.clone();
            cand.push(s);
            if joined(&cand) <= budget {
                chosen = cand;
            }
        }
        chosen.sort_by_key(|s| s.display);
    } else {
        chosen = all;
    }

    let mut spans: Vec<Span<'static>> = Vec::new();
    if chosen.is_empty() {
        spans.push(Span::styled(
            theme.glyphs.ellipsis().to_string(),
            theme.muted(),
        ));
    } else {
        for (i, s) in chosen.iter().enumerate() {
            if i > 0 {
                spans.push(sep(" · "));
            }
            spans.push(key(&s.keys));
            spans.push(sep(&format!(" {}", s.label)));
        }
        if truncated {
            // Многоточие — из набора глифов: в ASCII-режиме «…» — чужой
            // символ (см. theme::Glyphs::ellipsis).
            spans.push(sep(&format!(" · {}", theme.glyphs.ellipsis())));
        }
    }
    Line::from(spans)
}

/// Центральная колонка: блоки диалога с переносом и прокруткой.
/// Арт-строки (mermaid) не переносятся — клипаются.
fn draw_dialog(f: &mut Frame, area: Rect, app: &mut App, theme: &Theme) {
    let inner_w = usize::from(area.width.saturating_sub(2)).max(1);
    let mut lines: Vec<Line<'static>> = Vec::new();
    // Лог пуст, если в нём нет ничего, кроме логотипа: подсказка «что делать»
    // обязана быть видна на первом кадре (A4). Считаем её здесь, а ставим
    // ниже логотипа — он первый блок диалога и открывает экран.
    let empty_log = app.blocks.iter().all(|b| matches!(b, ChatBlock::Logo));
    // Обход с группировкой: серия ПОДРЯД идущих tool-вызовов рендерится как
    // один компактный «журнал активности» (не раздувает диалог на блок на вызов).
    let mut idx = 0;
    while idx < app.blocks.len() {
        let block = &app.blocks[idx];
        if matches!(block, ChatBlock::Tool { .. }) {
            let mut end = idx + 1;
            while end < app.blocks.len() && matches!(app.blocks[end], ChatBlock::Tool { .. }) {
                end += 1;
            }
            for line in tool_run_lines(&app.blocks[idx..end], idx, &app.tool_live, theme) {
                lines.extend(wrap_line(&line, inner_w));
            }
            lines.push(Line::default());
            idx = end;
            continue;
        }
        for line in block_lines(block, theme, inner_w) {
            if line_is_art(&line) {
                lines.push(line);
            } else {
                lines.extend(wrap_line(&line, inner_w));
            }
        }
        lines.push(Line::default());
        idx += 1;
    }
    if empty_log {
        // Обязательно через `wrap_line`: раньше подсказка была одной длинной
        // строкой и в узком диалоге молча обрезалась вместе с путём к справке.
        lines.extend(wrap_line(
            &Line::from(Span::styled(
                "Пусто. Напишите сообщение — или ? — список клавиш, /help — команды.",
                theme.muted(),
            )),
            inner_w,
        ));
        lines.push(Line::default());
    }

    let view_h = usize::from(area.height.saturating_sub(2));
    app.viewport = view_h;
    let total = lines.len();
    let max_scroll = total.saturating_sub(view_h);
    if app.scroll > max_scroll {
        app.scroll = max_scroll;
    }
    // Поиск по диалогу (Ctrl+F): совпадения пересчитываются каждый кадр —
    // дёшево и актуально даже при стриме; флаг dirty центрирует текущее.
    if let Some(s) = app.search.as_mut() {
        if s.query.is_empty() {
            s.matches.clear();
            s.current = 0;
            s.dirty = false;
        } else {
            let needle = s.query.to_lowercase();
            s.matches = lines
                .iter()
                .enumerate()
                .filter(|(_, l)| line_to_plain(l).to_lowercase().contains(&needle))
                .map(|(i, _)| i)
                .collect();
            if !s.matches.is_empty() && s.current >= s.matches.len() {
                s.current = s.matches.len() - 1;
            }
            if s.dirty {
                if let Some(&idx) = s.matches.get(s.current) {
                    let target_skip = idx.saturating_sub(view_h / 2).min(max_scroll);
                    app.scroll = total.saturating_sub(view_h + target_skip);
                }
                s.dirty = false;
            }
        }
    }
    let skip = total.saturating_sub(view_h + app.scroll);
    // Состояние для выделения мышью: внутренняя область, plain-строки, сдвиг.
    app.dialog_inner = Some(Rect {
        x: area.x + 1,
        y: area.y + 1,
        width: area.width.saturating_sub(2),
        height: area.height.saturating_sub(2),
    });
    app.dialog_lines = lines.iter().map(line_to_plain).collect();
    app.dialog_skip = skip;
    let visible: Vec<Line> = lines.into_iter().skip(skip).take(view_h).collect();
    // Подсветка совпадений поиска: текущее — акцентной плашкой, остальные —
    // приглушённой; игла и счётчик — в строке над вводом (draw_input_state).
    let visible = match app.search.as_ref().filter(|s| !s.query.is_empty()) {
        Some(s) => {
            let needle = s.query.to_lowercase();
            let current_line = s.matches.get(s.current).copied();
            visible
                .into_iter()
                .enumerate()
                .map(|(vi, l)| {
                    highlight_line(
                        &l,
                        &needle,
                        Style::default().fg(theme.bg).bg(theme.muted),
                        Style::default()
                            .fg(theme.bg)
                            .bg(theme.orange)
                            .add_modifier(Modifier::BOLD),
                        Some(skip + vi) == current_line,
                    )
                })
                .collect()
        }
        None => visible,
    };

    let title = if app.scroll > 0 {
        format!(" Диалог · прокрутка +{} (PgDn — вниз) ", app.scroll)
    } else {
        " Диалог ".to_string()
    };
    let border_style = if app.thinking() {
        Style::default().fg(theme.purple).bg(theme.bg)
    } else {
        theme.border()
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_set(theme.glyphs.border_set())
        .border_style(border_style)
        .title(Span::styled(title, theme.heading()));
    // Счётчик позиции справа: «первая видимая/всего» — где я в истории
    // (B6: полоса скролла есть, числовой ориентир — нет).
    let block = if max_scroll > 0 {
        let pos = format!(" {}/{} ", skip + 1, total);
        block.title_top(Line::from(Span::styled(pos, theme.muted())).right_aligned())
    } else {
        block
    };
    f.render_widget(Paragraph::new(visible).block(block), area);

    // Скроллбар на правой кромке рамки: видно, где мы в истории диалога.
    // Трек повторяет глиф рамки (│), бегунок █ — акцентный.
    if max_scroll > 0 && area.height > 3 {
        let sb_area = Rect {
            x: area.x + area.width.saturating_sub(1),
            y: area.y + 1,
            width: 1,
            height: area.height.saturating_sub(2),
        };
        let mut sb_state = ScrollbarState::new(max_scroll)
            .position(skip)
            .viewport_content_length(view_h);
        let scrollbar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(None)
            .end_symbol(None)
            .track_symbol(Some(theme.glyphs.scroll_track()))
            .thumb_symbol(theme.glyphs.scroll_thumb())
            .track_style(theme.muted())
            .thumb_style(Style::default().fg(theme.cyan).bg(theme.bg));
        f.render_stateful_widget(scrollbar, sb_area, &mut sb_state);
    }

    // Кнопка «▼» в правом нижнем углу: прыжок к свежему ответу.
    // Показывается, только когда пользователь поднялся выше дна.
    if app.scroll > 0 && area.width > 6 && area.height > 3 {
        let btn = Rect {
            x: area.x + area.width.saturating_sub(4),
            y: area.y + area.height.saturating_sub(2),
            width: 3,
            height: 1,
        };
        app.jump_btn = Some(btn);
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!(" {} ", theme.glyphs.down()),
                theme.badge(),
            ))),
            btn,
        );
    } else {
        app.jump_btn = None;
    }

    // Выделение мышью: пост-пасс поверх виджета — инверсия фона ячеек.
    // Строки считает app.selection_rows() в координатах контента.
    if app.selection.is_some() {
        if let Some(inner) = app.dialog_inner {
            let buf = f.buffer_mut();
            for (idx, c0, c1) in app.selection_rows() {
                let row = inner.y + u16_sat(idx.saturating_sub(app.dialog_skip));
                for col in c0..c1 {
                    let x = inner.x + u16_sat(col);
                    if x < inner.x + inner.width {
                        buf[(x, row)].set_style(theme.selection());
                    }
                }
            }
        }
    }
}

/// Plain-текст строки ratatui (конкатенация спанов) — для выделения мышью.
fn line_to_plain(line: &Line<'_>) -> String {
    line.spans.iter().map(|s| s.content.as_ref()).collect()
}

/// Сколько вызовов серии показываем полностью, прежде чем схлопнуть середину.
const TOOL_RUN_MAX_VISIBLE: usize = 6;

/// Одна строка вызова инструмента: маркер состояния + имя + краткое действие
/// (путь/команда/запрос — приглушённо). Пользователь видит, ЧТО делает агент,
/// ценой одной строки на вызов. У выполняющегося вызова — живой таймер
/// (`· N:SS`): долгая команда перестаёт выглядеть зависанием.
fn tool_item_line(
    name: &str,
    action: &str,
    state: ToolState,
    running_secs: Option<u64>,
    theme: &Theme,
) -> Line<'static> {
    let (mark, color) = match state {
        ToolState::Running => (theme.glyphs.running(), theme.orange),
        ToolState::Ok => (theme.glyphs.ok(), theme.green),
        ToolState::Error => (theme.glyphs.err(), theme.red),
    };
    let mark_style = Style::default().fg(color).bg(theme.bg);
    let mut spans = vec![
        Span::styled(format!("{mark} "), mark_style),
        Span::styled(name.to_string(), mark_style.add_modifier(Modifier::BOLD)),
    ];
    if !action.is_empty() {
        spans.push(Span::styled(format!(" {action}"), theme.muted()));
    }
    if let (ToolState::Running, Some(secs)) = (state, running_secs) {
        spans.push(Span::styled(
            format!(" · {}", fmt_elapsed(secs)),
            theme.muted(),
        ));
    }
    Line::from(spans)
}

/// Компактный «журнал активности» для серии ПОДРЯД идущих tool-вызовов:
/// до [`TOOL_RUN_MAX_VISIBLE`] строк; при превышении — первые два, счётчик
/// скрытых и последние три. Итог показываем только у последнего завершённого
/// (одна строка) и у ошибок (по одной строке) — диалог не перегружается,
/// полный вывод доступен на вкладках правой панели. У выполняющегося вызова —
/// живой таймер и хвост вывода (что команда делает прямо сейчас).
/// `base` — индекс первого блока среза в `app.blocks` (ключ карты `live`).
fn tool_run_lines(
    run: &[ChatBlock],
    base: usize,
    live: &std::collections::HashMap<usize, ToolLive>,
    theme: &Theme,
) -> Vec<Line<'static>> {
    /// Строка итога одного вызова (последняя строка summary, приглушённо).
    fn summary_line(summary: &str, theme: &Theme) -> Option<Line<'static>> {
        summary
            .lines()
            .last()
            .map(|l| Line::from(Span::styled(format!("  {}", l.trim_end()), theme.muted())))
    }
    let items: Vec<(&str, &str, &ToolState, &str)> = run
        .iter()
        .filter_map(|b| match b {
            ChatBlock::Tool {
                name,
                state,
                action,
                summary,
            } => Some((name.as_str(), action.as_str(), state, summary.as_str())),
            _ => None,
        })
        .collect();
    let mut out = Vec::new();
    let visible: Vec<usize> = if items.len() <= TOOL_RUN_MAX_VISIBLE {
        (0..items.len()).collect()
    } else {
        let mut v: Vec<usize> = vec![0, 1];
        v.extend(items.len() - 3..items.len());
        v
    };
    for (pos, &i) in visible.iter().enumerate() {
        if pos == 2 && items.len() > TOOL_RUN_MAX_VISIBLE {
            out.push(Line::from(Span::styled(
                format!(
                    "  {} +{} вызовов",
                    theme.glyphs.ellipsis(),
                    items.len() - TOOL_RUN_MAX_VISIBLE + 1
                ),
                theme.muted(),
            )));
        }
        let (name, action, state, summary) = items[i];
        let item_live = live.get(&(base + i));
        out.push(tool_item_line(
            name,
            action,
            *state,
            item_live.map(|l| l.started.elapsed().as_secs()),
            theme,
        ));
        // Живой хвост вывода выполняющегося вызова: «что происходит сейчас»
        // вместо немого спиннера на долгих командах (сборка, тесты).
        if matches!(state, ToolState::Running) {
            if let Some(l) = item_live {
                if !l.tail.is_empty() {
                    let tail: Vec<&str> = l.tail.lines().collect();
                    for t in &tail[tail.len().saturating_sub(TOOL_TAIL_LINES)..] {
                        out.push(Line::from(Span::styled(
                            format!("  {}", truncate_chars(t.trim_end(), TOOL_TAIL_LINE_CHARS)),
                            theme.muted().add_modifier(Modifier::ITALIC),
                        )));
                    }
                }
            }
        }
        let is_last = i == items.len() - 1;
        match state {
            ToolState::Error => out.extend(summary_line(summary, theme)),
            ToolState::Ok if is_last => out.extend(summary_line(summary, theme)),
            _ => {}
        }
    }
    out
}

/// Блок чата → стилизованные строки (до переноса по ширине).
/// `width` нужен таблицам: они переносятся внутри ячеек уже здесь,
/// т.к. их разделитель │ ловится детектором арта и общий перенос их
/// пропускает (иначе широкие таблицы обрезались справа).
fn block_lines(block: &ChatBlock, theme: &Theme, width: usize) -> Vec<Line<'static>> {
    match block {
        ChatBlock::Logo => logo_lines(theme),
        ChatBlock::User(text) => {
            let mut out = vec![Line::from(vec![
                Span::styled(format!("{} ", theme.glyphs.role_user()), theme.heading()),
                Span::styled("вы", theme.heading()),
            ])];
            for l in text.lines() {
                out.push(Line::from(vec![
                    Span::styled(format!("{} ", theme.glyphs.gutter()), theme.accent()),
                    Span::styled(l.to_string(), theme.base()),
                ]));
            }
            out
        }
        ChatBlock::Thinking(text) => {
            // «Мысли» — компактно и приглушённо: максимум
            // MAX_THINKING_LINES строк, хвост — счётчиком.
            let mut out = vec![Line::from(vec![
                Span::styled(format!("{} ", theme.glyphs.note()), theme.muted()),
                Span::styled("мысли", theme.muted().add_modifier(Modifier::ITALIC)),
            ])];
            let lines: Vec<&str> = text.lines().collect();
            let show = lines.len().min(MAX_THINKING_LINES);
            for l in &lines[..show] {
                out.push(Line::from(Span::styled(
                    format!("  {l}"),
                    theme.muted().add_modifier(Modifier::ITALIC),
                )));
            }
            if lines.len() > show {
                out.push(Line::from(Span::styled(
                    format!(
                        "  {} (+{} строк)",
                        theme.glyphs.ellipsis(),
                        lines.len() - show
                    ),
                    theme.muted(),
                )));
            }
            out
        }
        ChatBlock::Assistant(text) => {
            let mut out = vec![Line::from(vec![
                Span::styled(
                    format!("{} ", theme.glyphs.role_assistant()),
                    theme.purple().add_modifier(Modifier::BOLD),
                ),
                Span::styled("арх", theme.purple().add_modifier(Modifier::BOLD)),
            ])];
            out.extend(markdown_lines(text, theme, width));
            out
        }
        ChatBlock::Tool {
            name,
            state,
            action,
            summary,
        } => {
            let mut out = vec![tool_item_line(name, action, *state, None, theme)];
            if !matches!(state, ToolState::Running) && !summary.is_empty() {
                // Одна последняя строка итога, приглушённо (не раздуваем диалог).
                if let Some(last) = summary.lines().last() {
                    out.push(Line::from(Span::styled(
                        format!("  {}", last.trim_end()),
                        theme.muted(),
                    )));
                }
            }
            out
        }
        ChatBlock::System { command, text } => {
            let mut out = vec![Line::from(vec![
                Span::styled(format!("{} ", theme.glyphs.note()), theme.purple()),
                Span::styled(command.clone(), theme.purple().add_modifier(Modifier::BOLD)),
            ])];
            for l in text.lines() {
                out.push(Line::from(Span::styled(l.to_string(), theme.base())));
            }
            out
        }
        ChatBlock::Error(text) => {
            let mut out = vec![Line::from(Span::styled(
                format!("{} ошибка", theme.glyphs.err()),
                theme.error().add_modifier(Modifier::BOLD),
            ))];
            for l in text.lines() {
                out.push(Line::from(Span::styled(l.to_string(), theme.error())));
            }
            out
        }
    }
}

/// Ступень деградации бара вкладок (см. [`draw_right`]).
#[derive(Clone, Copy, PartialEq, Eq)]
enum TabLabels {
    /// Полный бар: подпись у каждой вкладки.
    Full,
    /// Компактный: подпись только у активной, остальные — иконками.
    Compact,
    /// Только иконки (крайний случай узкой панели).
    Icons,
}

/// Подпись вкладки для заголовка панели. Базовая — [`RightTab::title`];
/// у «Субагентов» при непустом реестре — счётчик `(бегут/всего)`
/// («Субагенты (2/7)»). Пара чисел, а не одно: сессия, где все задачи уже
/// завершены, всё равно показывает `(0/7)` — панель не пуста, и заголовок
/// обязан это отражать (tui-design-principles #5, «проектируй все состояния»:
/// состояние не должно исчезать вместе с процессом; у списка — счётчик).
/// Ноль записей — чистая подпись, чтобы пустой реестр не рябил «(0/0)».
/// Длину массива [`Theme::glyphs.tab_icons`] это не ломает: иконки берутся
/// отдельно, по индексу вкладки в [`RightTab::ALL`]. `Флот` счётчик не
/// получает сознательно — он показывает завершённые прогоны с диска, а не
/// число задач реестра.
fn tab_label(app: &App, tab: RightTab) -> String {
    if tab == RightTab::Subagents {
        let total = app.subagents_total();
        if total > 0 {
            return format!(
                "{} {}",
                tab.title(),
                subagents_ratio(app.subagents_running(), total)
            );
        }
    }
    tab.title().to_string()
}

/// Пара чисел счётчика субагентов — `(бегут/всего)`, в скобках. Один факт —
/// один формат: заголовок вкладки [`tab_label`] («Субагенты (2/2)») и сегмент
/// статус-бара («субагенты (2/2)») берут её отсюда, поэтому не могут разойтись
/// ни в порядке чисел, ни в разделителе (п.6 — согласование форматов).
fn subagents_ratio(running: usize, total: usize) -> String {
    format!("({running}/{total})")
}

/// Правая колонка: вкладки Mermaid / Рубрика / Знания / Флот / Субагенты.
/// Mermaid — без переносов (арт клипается), остальные — с переносом.
/// Содержимое прокручивается: панель узкая, список задач/лог легко длиннее
/// её высоты, поэтому рендер клампит сдвиг и показывает полосу прокрутки и
/// счётчик.
fn draw_right(f: &mut Frame, area: Rect, app: &mut App, theme: &Theme) {
    let inner_w = usize::from(area.width.saturating_sub(2)).max(1);
    let active = app.right_tab();
    // Арт шире даже расширенной панели (кап 60%)? Подскажем путь: F4.
    let mermaid_clipped = app
        .panels
        .mermaid
        .lines()
        .map(UnicodeWidthStr::width)
        .max()
        .unwrap_or(0)
        > inner_w;
    // `active` захвачен по значению: замыкание не держит ссылку на `app`, и
    // ниже можно писать клампнутый скролл обратно в `app`.
    let tab_style = move |tab: &RightTab, theme: &Theme| {
        if *tab == active {
            Style::default()
                .fg(theme.bg)
                .bg(theme.cyan)
                .add_modifier(Modifier::BOLD)
        } else {
            theme.muted()
        }
    };
    // Подписи вкладок — всегда короткие: длинный хинт в заголовке обрезал
    // соседние вкладки у правого края (кейс 2026-09-02).
    let make_tabs = |labels: TabLabels| -> Vec<Span<'static>> {
        RightTab::ALL
            .iter()
            .enumerate()
            .map(|(i, tab)| {
                let icon = theme.glyphs.tab_icons()[i];
                let show = labels == TabLabels::Full || *tab == active;
                let text = if labels != TabLabels::Icons && show {
                    format!(" {icon} {} ", tab_label(app, *tab))
                } else {
                    format!(" {icon} ")
                };
                Span::styled(text, tab_style(tab, theme))
            })
            .collect()
    };
    // Деградация ступенями, НЕЗАВИСИМО от ширины терминала: при базовой
    // ширине панели 42 (внутренних 40) полный бар из пяти подписей (53 ячейки
    // в юникоде, 54 в ASCII — у `< >` две клетки) не влезает никогда. Значит,
    // на любой ширине активная подпись обязана остаться видимой: раньше
    // порог зависел от `term_w`, и ASCII-пользователь на широком терминале
    // видел только глифы `< > + # > @`, где два `>` неразличимы.
    let full = make_tabs(TabLabels::Full);
    let title_spans = if spans_width(&full) <= inner_w {
        full
    } else {
        let compact = make_tabs(TabLabels::Compact);
        if spans_width(&compact) <= inner_w {
            compact
        } else {
            make_tabs(TabLabels::Icons)
        }
    };

    let tab = active;
    let content = app.panels.content(tab).to_string();
    let mut lines: Vec<Line<'static>> = Vec::new();
    if content.is_empty() {
        lines.extend(wrap_line(
            &Line::from(Span::styled(Panels::placeholder(tab), theme.muted())),
            inner_w,
        ));
    } else if tab == RightTab::Mermaid {
        // Арт не переносим: клип по ширине, зелёный оттенок для схемы.
        for l in content.lines() {
            let style = if is_art(l) { theme.art() } else { theme.base() };
            lines.push(Line::from(Span::styled(l.to_string(), style)));
        }
    } else {
        for l in content.lines() {
            lines.extend(wrap_line(
                &Line::from(Span::styled(l.to_string(), theme.base())),
                inner_w,
            ));
        }
    }

    // Прокрутка: вьюпорт — внутренняя высота панели; сдвиг клампится по
    // содержимому и пишется обратно (тот же приём, что у help-скролла), иначе
    // после смены вкладки остался бы «мёртвый» запас и панель казалась бы
    // пустой. `set_right_viewport` даёт шаг страничной прокрутки клавишам.
    let viewport = usize::from(area.height.saturating_sub(2)).max(1);
    app.set_right_viewport(viewport);
    let max_scroll = lines.len().saturating_sub(viewport);
    // Границу знает только рендер: `G` (прыжок к концу) берёт её отсюда.
    app.set_right_max_scroll(max_scroll);
    if app.right_scroll() > max_scroll {
        app.set_right_scroll(max_scroll);
    }
    let scroll = app.right_scroll();

    let mut block = Block::default()
        .borders(Borders::ALL)
        .border_set(theme.glyphs.border_set())
        .border_style(theme.border())
        .title(Line::from(title_spans));
    if mermaid_clipped {
        // Хинт F4 — в НИЖНИЙ заголовок (слева), а не в бар вкладок.
        block = block.title_bottom(Line::from(Span::styled(
            " F4 — вся схема ".to_string(),
            theme.muted(),
        )));
    }
    // Переполнение обязано быть видимым: счётчик «первая-последняя/всего» и,
    // если позволяет высота, полоса прокрутки на правой кромке. Раньше хвост
    // длинного списка молча срезался и был недостижим.
    if max_scroll > 0 {
        let end = (scroll + viewport).min(lines.len());
        let pos = format!(" PgUp/PgDn · g/G {}-{}/{} ", scroll + 1, end, lines.len());
        block = block
            .title_bottom(Line::from(Span::styled(pos, theme.muted())).alignment(Alignment::Right));
    }
    f.render_widget(
        Paragraph::new(lines)
            .block(block)
            .scroll((u16_sat(scroll), 0)),
        area,
    );

    if max_scroll > 0 && area.height > 3 {
        let sb_area = Rect {
            x: area.x + area.width.saturating_sub(1),
            y: area.y + 1,
            width: 1,
            height: area.height.saturating_sub(2),
        };
        let mut sb_state = ScrollbarState::new(max_scroll)
            .position(scroll)
            .viewport_content_length(viewport);
        let scrollbar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
            .begin_symbol(None)
            .end_symbol(None)
            .track_symbol(Some(theme.glyphs.scroll_track()))
            .thumb_symbol(theme.glyphs.scroll_thumb())
            .track_style(theme.muted())
            .thumb_style(Style::default().fg(theme.cyan).bg(theme.bg));
        f.render_stateful_widget(scrollbar, sb_area, &mut sb_state);
    }
}

/// Максимум видимых строк поля ввода (дальше окно следует за курсором).
const MAX_INPUT_ROWS: usize = 8;

/// Раскладывает текст поля ввода на визуальные строки ширины `width`:
/// перенос по ширине + жёсткие '\n'. Возвращает (строки, строка курсора,
/// x курсора); x включает prompt/отступ (2 ячейки).
fn wrap_input(text: &str, cursor: usize, width: usize) -> (Vec<String>, usize, usize) {
    let avail = width.saturating_sub(2).max(1);
    let mut rows: Vec<String> = Vec::new();
    let mut row = String::new();
    let mut row_w = 0usize;
    let mut cur: Option<(usize, usize)> = None;
    for (i, c) in text.char_indices() {
        if i == cursor {
            cur = Some((rows.len(), 2 + row_w));
        }
        if c == '\n' {
            rows.push(std::mem::take(&mut row));
            row_w = 0;
            continue;
        }
        let cw = UnicodeWidthChar::width(c).unwrap_or(0);
        if row_w + cw > avail {
            rows.push(std::mem::take(&mut row));
            row_w = 0;
        }
        row.push(c);
        row_w += cw;
    }
    rows.push(row);
    let (cur_row, cur_x) = cur.unwrap_or((rows.len() - 1, 2 + row_w));
    (rows, cur_row, cur_x)
}

/// Плавающая карточка очереди сообщений внизу окна логов: следующее на
/// запуск сообщение подсвечено (▶), остальные — приглушены (•); не более
/// трёх строк + «ещё N». Нижний ряд диалога оставлен кнопке «▼».
fn draw_queue_overlay(f: &mut Frame, dialog: Rect, app: &App, theme: &Theme) {
    if dialog.width < 24 || dialog.height < 8 {
        return;
    }
    let shown = app.queue.len().min(3);
    let more = usize::from(app.queue.len() > 3);
    let h = u16_sat(shown + more + 2);
    let area = Rect {
        x: dialog.x + 1,
        y: dialog.y + dialog.height.saturating_sub(h + 2),
        width: dialog.width - 2,
        height: h,
    };
    f.render_widget(Clear, area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_set(theme.glyphs.border_set())
        .border_style(Style::default().fg(theme.purple).bg(theme.bg))
        .style(Style::default().bg(theme.bg))
        .title(Span::styled(
            format!(" {} очередь · {} ", theme.glyphs.queue(), app.queue.len()),
            theme.purple(),
        ));
    let inner_w = usize::from(area.width.saturating_sub(6)).max(1);
    let mut lines: Vec<Line<'static>> = Vec::new();
    for (i, q) in app.queue.iter().take(3).enumerate() {
        // Многострочное сообщение показываем первой строкой с маркером ↵.
        let first = q.lines().next().unwrap_or("");
        let fold = if q.contains('\n') {
            format!(" {}", theme.glyphs.fold())
        } else {
            String::new()
        };
        let text: String = first.chars().take(inner_w).collect();
        let style = if i == 0 {
            Style::default()
                .fg(theme.green)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD)
        } else {
            theme.muted()
        };
        let marker = if i == 0 {
            theme.glyphs.next_marker()
        } else {
            theme.glyphs.bullet()
        };
        // Порядковый номер — ВПЕРЁД маркера: глиф `▶`/`•` вне ASCII и его
        // ширина в чужом моноширинном шрифте не гарантирована (инцидент
        // 07.09 — fontconfig-фолбэк ломал сетку). Начало строки всегда
        // «N. » из ASCII-цифр и точки, номер не съезжает вместе с маркером.
        lines.push(Line::from(Span::styled(
            format!("{}. {marker} {text}{fold}", i + 1),
            style,
        )));
    }
    if more == 1 {
        lines.push(Line::from(Span::styled(
            format!("{} ещё {}", theme.glyphs.ellipsis(), app.queue.len() - 3),
            theme.muted(),
        )));
    }
    f.render_widget(Paragraph::new(lines).block(block), area);
}

/// Нижнее поле ввода: многострочное (перенос по ширине; перевод строки —
/// Shift+Enter / Alt+Enter / Ctrl+J), растёт до `MAX_INPUT_ROWS` строк, дальше
/// видимое окно следует за курсором. Плюс ghost-подсказка автодополнения.
fn draw_input(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    let prompt_w = 2;
    let inner_w = usize::from(area.width).max(1);
    let (rows, cur_row, cur_x) = wrap_input(app.input.text(), app.input.cursor(), inner_w);
    let visible = usize::from(area.height).max(1);
    let skip = cur_row.saturating_sub(visible.saturating_sub(1));
    let multiline = app.input.text().contains('\n');

    let mut lines: Vec<Line<'static>> = Vec::new();
    for (i, row) in rows.iter().enumerate().skip(skip).take(visible) {
        let mut spans = vec![
            Span::styled(
                if i == 0 {
                    format!("{} ", theme.glyphs.cursor())
                } else {
                    " ".repeat(prompt_w)
                },
                theme.heading(),
            ),
            Span::styled(row.clone(), theme.base()),
        ];
        // Ghost-подсказка — только у однострочного ввода, на последней строке.
        if !multiline && i == rows.len() - 1 {
            if let Some(hint) = app.input.ghost_hint() {
                spans.push(Span::styled(hint, theme.muted()));
            }
        }
        // Пустое поле, но есть отложенный черновик: напоминаем, как вернуть.
        // Тост гаснет через 4 с — без этого напоминания текст «пропадал бы».
        if rows.len() == 1 && app.input.text().is_empty() && app.draft().is_some() {
            spans.push(Span::styled(
                "черновик отложен · Esc — вернуть",
                theme.muted(),
            ));
        }
        lines.push(Line::from(spans));
    }
    f.render_widget(Paragraph::new(lines), area);

    // Курсор — по фактической позиции ввода (поле без рамки: без сдвигов).
    let x = area
        .x
        .saturating_add(u16_sat(cur_x))
        .min(area.x.saturating_add(area.width.saturating_sub(1)));
    let y = area
        .y
        .saturating_add(u16_sat(cur_row.saturating_sub(skip)))
        .min(area.y.saturating_add(area.height.saturating_sub(1)));
    f.set_cursor_position((x, y));
}

/// Строка состояния над полем ввода: что происходит прямо сейчас (ход
/// модели, очередь). Появляется только когда есть что сказать — иначе
/// строка не занимает место (раскладка не дёргается без причины).
fn draw_input_state(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    // Активный поиск (Ctrl+F): строка запроса, счётчик совпадений, клавиши;
    // курсор — в конец запроса (состояние хода при этом видно в рамке
    // «Диалог» и статус-баре — дублировать не нужно).
    if let Some(s) = &app.search {
        let counter = if s.query.is_empty() {
            "введите запрос".to_string()
        } else if s.matches.is_empty() {
            "совпадений нет".to_string()
        } else {
            format!("{}/{}", s.current + 1, s.matches.len())
        };
        let prompt = format!("поиск: {}", s.query);
        f.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(prompt.clone(), Style::default().fg(theme.fg).bg(theme.bg)),
                Span::styled(
                    format!("  ·  {counter}  ·  Enter — далее · Alt+Enter — назад · Esc — закрыть"),
                    theme.muted(),
                ),
            ])),
            area,
        );
        let x = area
            .x
            .saturating_add(u16_sat(UnicodeWidthStr::width(prompt.as_str())))
            .min(area.x.saturating_add(area.width.saturating_sub(1)));
        f.set_cursor_position((x, area.y));
        return;
    }
    if app.thinking() {
        // «Дышащая» точка — спокойный живой индикатор мыслей: кадр меняется
        // раз в PULSE_TICK_DIVISOR тиков (120 мс × 4 ≈ 0.5 с/кадр, полный
        // вдох-выдох ~2.9 с — без мерцания).
        let frames = theme.glyphs.pulse();
        let pulse = frames[app.anim_frame(frames.len() * PULSE_TICK_DIVISOR) / PULSE_TICK_DIVISOR];
        // Что происходит прямо сейчас: если выполняется инструмент — говорим
        // какой и сколько уже (долгая команда не выглядит «офлайном» модели),
        // иначе — таймер разгона самой модели.
        let label = if let Some((name, action, secs)) = app.running_tool() {
            // action начинается с «: » (конвенция action_desc) — в строке
            // состояния свой разделитель, дубль-двоеточие не нужно.
            let what = match action.trim_start_matches(':').trim_start() {
                "" => name.to_string(),
                a => format!("{name}: {a}"),
            };
            format!(
                "{pulse} выполняется: {} · {}",
                truncate_chars(&what, 60),
                fmt_elapsed(secs)
            )
        } else {
            let elapsed = app
                .thinking_elapsed()
                .map(|s| format!(" · {}", fmt_elapsed(s)))
                .unwrap_or_default();
            match app.stream_stats() {
                // Видимый ответ уже стримится: сколько и с какой скоростью.
                Some((answer, _, secs)) if answer > 0 => {
                    let (tok, rate) = stream_tok_rate(answer, secs);
                    format!("{pulse} отвечает{elapsed} · ~{tok} ток · ~{rate} т/с")
                }
                // Пока только «мысли»: это тоже живой стрим — объём виден.
                Some((_, think, secs)) if think > 0 => {
                    let (tok, rate) = stream_tok_rate(think, secs);
                    format!("{pulse} модель думает{elapsed} · ~{tok} ток · ~{rate} т/с")
                }
                _ => format!("{pulse} модель думает{elapsed}"),
            }
        };
        let mut spans = vec![Span::styled(
            label,
            Style::default()
                .fg(theme.purple)
                .bg(theme.bg)
                .add_modifier(Modifier::BOLD),
        )];
        // Открытая ask-модалка — верхний слой: клавиши уходят ей, и обещание
        // «Esc — прервать» (или «Enter — в очередь») стало бы ложью: Esc
        // отклоняет выбор, Enter выбирает пункт. Клавиши модалки печатает её
        // собственный футер, а строка состояния честно сообщает только факт.
        if app.ask.is_some() {
            f.render_widget(
                Paragraph::new(Line::from(spans)).alignment(Alignment::Right),
                area,
            );
            return;
        }
        spans.push(Span::styled("  ·  ", theme.muted()));
        if app.queue.is_empty() {
            spans.push(Span::styled("Esc — прервать", theme.muted()));
        } else {
            spans.push(Span::styled(
                format!("очередь: {}  ·  Esc — прервать", app.queue.len()),
                theme.muted(),
            ));
        }
        f.render_widget(
            Paragraph::new(Line::from(spans)).alignment(Alignment::Right),
            area,
        );
        return;
    }
    if !app.queue.is_empty() {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                format!("очередь: {}", app.queue.len()),
                theme.muted(),
            )))
            .alignment(Alignment::Right),
            area,
        );
    } else if let Some(hint) = app
        .intent_hint()
        // Под ask-модалкой строки теплицы нет: модалка — верхний слой, её
        // футер печатает свои клавиши, а хвост строки хука («… — поднять?»)
        // читался бы как живой вопрос, ответить на который нечем. Та же
        // причина, по которой строка состояния под модалкой не обещает
        // «Esc — прервать» (см. ветку `app.thinking()` выше).
        .filter(|_| app.ask.is_none())
    {
        // Факт, а не действие: строка теплицы гипотез, пересекающихся с
        // намерением цели (первая строка доменного хука `intent`). Считана
        // один раз при постановке цели — рендер процессов не запускает.
        // Во время хода строка уступает живому статусу терна (ветка выше
        // вернулась), после хода возвращается; в диалоге она всё это время
        // видна в подтверждении цели.
        let label = fit_label_cells(hint, usize::from(area.width), theme.glyphs.ellipsis());
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(label, theme.muted())))
                .alignment(Alignment::Right),
            area,
        );
    }
}

/// Статус-бар: бейдж модели | индикатор контекста | счётчик субагентов | toast |
/// заметка | cwd | подсказки клавиш.
///
/// Правая колонка — подсказки клавиш ([`super::keymap::hints`]): они важнее
/// всего (tui-design-principles #6, обнаруживаемость). Левая — атомарные
/// сегменты по убыванию важности: бейдж модели → шкала контекста → счётчик
/// субагентов → toast → заметка (усекается с `…`) → cwd. Счётчик не режется
/// посередине: он либо влезает целиком в одной из своих форм, либо уступает
/// соседу (п.5, «никогда не рвать число и не исчезать молча»). Раскладка
/// предвычисляется целиком: выбор формы счётчика зависит от того, сколько
/// клеток осталось после бейджа и шкалы.
fn draw_status(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    let width = usize::from(area.width);
    let hint_budget = (width / 2)
        .max(STATUS_HINTS_MIN)
        .min(width.saturating_sub(STATUS_LEFT_MIN));
    // Под открытой ask-модалкой подсказку собирает тот же построитель, что
    // рисует футер модалки: статическая таблица реестра обещала бы «1-9» и
    // стрелки и при пустом списке, и при одном варианте, а клавиши `Esc`
    // (приоритетнее цифр) вылетали бы первыми по правилам общей обрезки.
    // Число вариантов — состояние кадра, поэтому фильтрует рендер, а подписи
    // по-прежнему берутся из реестра.
    let hint_spans = match app.ask.as_ref() {
        Some(ask) if matches!(app.hint_ctx(), super::keymap::Ctx::Ask) => {
            let (_, esc_tail) = ask_chrome(ask.kind, theme);
            ask_hint_line(theme, ask.options.len(), esc_tail, false, hint_budget).spans
        }
        _ => super::keymap::hints(app.hint_ctx(), hint_budget, theme),
    };
    let hint_w = spans_width(&hint_spans);
    let left_budget = width.saturating_sub(hint_w + 2);

    let badge = Span::styled(format!(" {} ", app.model_name), theme.badge());
    let badge_w = UnicodeWidthStr::width(app.model_name.as_str()) + 2; // « %s »
    let ctx = context_spans(app, theme);
    let ctx_w = spans_width(&ctx);
    // Счётчик фоновых субагентов: атомарный сегмент той же формы, что заголовок
    // вкладки. Список имён раньше не имел границы длины, обрезался рендером
    // жёстко (без `…`) и вытеснял подсказки — на 60×16 пропадали Enter/Esc;
    // сам же счётчик на 60 колонках исчезал целиком, а на 80 рвался посередине
    // числа (дефект №5). Теперь у него две формы и явный приоритет уступок.
    let counter = subagent_counter(app, theme);
    let (full_w, compact_w) = match &counter {
        Some((full, compact, _)) => (
            UnicodeWidthStr::width(full.as_str()),
            UnicodeWidthStr::width(compact.as_str()),
        ),
        None => (0, 0),
    };
    let has_counter = counter.is_some();
    // Порядок уступок: (шкала + полная) → (шкала + компактная) → (полная без
    // шкалы) → (компактная без шкалы) → счётчика нет вовсе. Ни в одной из форм
    // число не рвётся: сегмент вставляется целиком по свободному месту.
    let with_ctx_full = has_counter && badge_w + ctx_w + full_w <= left_budget;
    let with_ctx_compact =
        !with_ctx_full && has_counter && badge_w + ctx_w + compact_w <= left_budget;
    let bare_full =
        !with_ctx_full && !with_ctx_compact && has_counter && badge_w + full_w <= left_budget;
    let bare_compact = !with_ctx_full
        && !with_ctx_compact
        && !bare_full
        && has_counter
        && badge_w + compact_w <= left_budget;
    let keep_ctx = if has_counter {
        with_ctx_full || with_ctx_compact
    } else {
        // Счётчика нет (реестр пуст) — шкала контекста никому не уступает,
        // кроме собственного невмещения в остаток строки.
        badge_w + ctx_w <= left_budget
    };

    let mut spans = vec![badge];
    if keep_ctx {
        spans.extend(ctx);
    }
    if let Some((full, compact, style)) = counter {
        if with_ctx_full || bare_full {
            spans.push(Span::styled(full, style));
        } else if with_ctx_compact || bare_compact {
            spans.push(Span::styled(compact, style));
        }
    }

    // Тост последнего действия — важнее постоянных заметок: он про то,
    // что только что произошло. Успех несёт символ `ok`, ошибка — `err`:
    // цвет здесь дублируется знаком (правило «цвет не в одиночку»).
    if let Some(toast) = app.toast() {
        let (mark, style) = match toast.level {
            ToastLevel::Ok => (
                theme.glyphs.ok(),
                Style::default().fg(theme.green).bg(theme.bg),
            ),
            ToastLevel::Err => (
                theme.glyphs.err(),
                Style::default().fg(theme.red).bg(theme.bg),
            ),
        };
        spans.push(Span::styled(format!("  · {mark} {}", toast.text), style));
    }

    // Заметка уступает подсказкам (они важнее, E6): что не влезло в
    // left_budget, урезаем с `…`, а не рвём на полуслове у границы подсказок;
    // совсем узко — заметка пропускается целиком.
    if let Some(extra) = app.status_extra() {
        let style = Style::default().fg(theme.orange).bg(theme.bg);
        let used = spans_width(&spans);
        let tail = format!("  · {extra}");
        if used + UnicodeWidthStr::width(tail.as_str()) <= left_budget {
            spans.push(Span::styled(tail, style));
        } else {
            let room = left_budget.saturating_sub(used + 4); // «  · » + «…»
            if room >= 8 {
                spans.push(Span::styled(
                    format!("  · {}…", clip_display_width(extra, room)),
                    style,
                ));
            }
        }
    }

    // cwd добавляется последним и уходит первым: «где я» пользователь знает из
    // оболочки, а «что нажать» — только отсюда.
    let cwd = Span::styled(
        format!("  {}", shorten_path(&app.tool_ctx.cwd)),
        theme.muted(),
    );
    if spans_width(&spans) + UnicodeWidthStr::width(cwd.content.as_ref()) <= left_budget {
        spans.push(cwd);
    }

    let cols =
        Layout::horizontal([Constraint::Min(0), Constraint::Length(u16_sat(hint_w))]).split(area);

    f.render_widget(
        Paragraph::new(Line::from(clip_spans(spans, left_budget, theme))),
        cols[0],
    );
    f.render_widget(Paragraph::new(Line::from(hint_spans)), cols[1]);
}

/// Формы счётчика субагентов для статус-бара: (полная, компактная, стиль).
/// `None` — реестр пуст, показывать нечего.
///
/// Полная форма — та же пара чисел и скобки, что у заголовка вкладки
/// [`tab_label`] (п.6: один факт — один формат); пока задачи бегут, она несёт
/// спиннер и зелёный цвет (движение видно без чтения числа), у завершённого
/// реестра приглушена. Компактная форма заменяет слово иконкой вкладки
/// «Субагенты»: на 60 колонках статус-бар терял счётчик целиком, а на 80 рвал
/// его посередине числа, потому что сегмент вытеснялся рендером. Компактная
/// форма влезает на обеих ширинах, не разрывая числа (п.5).
fn subagent_counter(app: &App, theme: &Theme) -> Option<(String, String, Style)> {
    let total = app.subagents_total();
    if total == 0 {
        return None;
    }
    let running = app.subagents_running();
    let ratio = subagents_ratio(running, total);
    // Иконка вкладки «Субагенты» — тот же знак, что в ряду вкладок справа,
    // поэтому компактная форма однозначно читается (и ASCII-безопасна:
    // в ASCII-режиме иконки заменяются на `@`/`<>`/…).
    let icon = theme.glyphs.tab_icons()[RightTab::ALL
        .iter()
        .position(|t| *t == RightTab::Subagents)
        .unwrap_or(0)];
    if running > 0 {
        let spin = theme.glyphs.spinner()[app.anim_frame(theme.glyphs.spinner().len())];
        Some((
            format!("  · {spin} субагенты {ratio} "),
            format!(" {icon}{running}/{total}"),
            Style::default().fg(theme.green).bg(theme.bg),
        ))
    } else {
        Some((
            format!("  · субагенты {ratio} "),
            format!(" {icon}{running}/{total}"),
            theme.muted(),
        ))
    }
}

/// Обрезка строки спанов до `max` клеток с многоточием на конце.
///
/// Стиль усечённого спана сохраняется (акцент/цвет не теряются), `…` —
/// приглушённый. Ширина считается по отображаемым клеткам, а не по байтам:
/// кириллица и гуттеры — по 1, бокс-арт — по 1, а `Span.content` хранит
/// байты (tui-accessibility-compat: границы режутся по клеткам).
fn clip_spans(spans: Vec<Span<'static>>, max: usize, theme: &Theme) -> Vec<Span<'static>> {
    if spans_width(&spans) <= max {
        return spans;
    }
    if max == 0 {
        return Vec::new();
    }
    let budget = max - 1; // одна клетка — под `…`
    let mut out: Vec<Span<'static>> = Vec::new();
    let mut used = 0usize;
    for s in spans {
        if used >= budget {
            break;
        }
        let w = UnicodeWidthStr::width(s.content.as_ref());
        if used + w <= budget {
            used += w;
            out.push(s);
        } else {
            let clipped = clip_display_width(s.content.as_ref(), budget - used);
            if !clipped.is_empty() {
                out.push(Span::styled(clipped, s.style));
            }
            break;
        }
    }
    out.push(Span::styled("…", theme.muted()));
    out
}

/// Подсветка совпадений `needle` (нижний регистр) в строке: спаны режутся
/// по границам совпадений, совпавшие сегменты получают `hl`-стиль
/// (текущее совпадение — `cur_hl`). Игла пуста — строка без изменений.
fn highlight_line(
    line: &Line<'static>,
    needle: &str,
    hl: Style,
    cur_hl: Style,
    is_current: bool,
) -> Line<'static> {
    if needle.is_empty() {
        return line.clone();
    }
    let match_style = if is_current { cur_hl } else { hl };
    let mut out = Vec::new();
    for span in &line.spans {
        let text = span.content.as_ref();
        // Карта «байт lowercased-текста → байт исходного»: регистронезависимый
        // поиск без разрыва UTF-8 при подстановке сегментов обратно.
        let mut lower = String::with_capacity(text.len());
        let mut map = Vec::with_capacity(text.len() + 1);
        for (i, ch) in text.char_indices() {
            let buf = ch.to_lowercase().to_string();
            lower.push_str(&buf);
            for _ in 0..buf.len() {
                map.push(i);
            }
        }
        map.push(text.len());
        let mut lp = 0usize;
        let mut prev_end = 0usize;
        while let Some(rel) = lower[lp..].find(needle) {
            let ls = lp + rel;
            let le = ls + needle.len();
            let (os, oe) = (map[ls], map[le]);
            if os > prev_end {
                out.push(Span::styled(text[prev_end..os].to_string(), span.style));
            }
            out.push(Span::styled(text[os..oe].to_string(), match_style));
            prev_end = oe;
            lp = le;
        }
        if prev_end < text.len() {
            out.push(Span::styled(text[prev_end..].to_string(), span.style));
        }
    }
    Line::from(out)
}

/// Сколько ячеек занимает набор спанов (display-ширина, не байты).
fn spans_width(spans: &[Span<'_>]) -> usize {
    spans
        .iter()
        .map(|s| UnicodeWidthStr::width(s.content.as_ref()))
        .sum()
}

/// Урезать строку до `max` ячеек по display-ширине (без разрыва
/// двухклеточного символа; `…` добавляет вызывающий).
fn clip_display_width(s: &str, max: usize) -> String {
    let mut out = String::new();
    let mut w = 0usize;
    for ch in s.chars() {
        let cw = UnicodeWidthStr::width(ch.to_string().as_str());
        if w + cw > max {
            break;
        }
        w += cw;
        out.push(ch);
    }
    out.trim_end().to_string()
}

/// Индикатор заполнения контекста: «◈ 12.3k/1.0M ▰▰▱▱▱▱▱▱ 1%».
/// Цвет шкалы — по порогам компактификации из конфига: зелёный до L1,
/// оранжевый до L3, дальше красный (авто-компактификация уже близко/идёт).
fn context_spans(app: &App, theme: &Theme) -> Vec<Span<'static>> {
    const WIDTH: usize = 8;
    let used = app.history_tokens();
    let budget = app.context_budget();
    if budget == 0 {
        return vec![Span::styled(
            format!(" {} ~{used} ток.", theme.glyphs.context()),
            theme.muted(),
        )];
    }
    let pct = used.saturating_mul(100) / budget;
    let agent_cfg = &app.tool_ctx.config.agent;
    let color = if pct >= agent_cfg.compact_l3_pct {
        theme.red
    } else if pct >= agent_cfg.compact_l1_pct {
        theme.orange
    } else {
        theme.green
    };
    let filled = (used.saturating_mul(WIDTH) / budget).min(WIDTH);
    let bar = format!(
        "{}{}",
        theme.glyphs.gauge_full().repeat(filled),
        theme.glyphs.gauge_empty().repeat(WIDTH - filled)
    );
    vec![
        Span::styled(
            format!(
                " {} {}/{} ",
                theme.glyphs.context(),
                fmt_tokens(used),
                fmt_tokens(budget)
            ),
            theme.muted(),
        ),
        Span::styled(
            format!("{bar} {pct}%"),
            Style::default().fg(color).bg(theme.bg),
        ),
    ]
}

/// Человекочитаемый размер токенов: 999 → «999», `12_345` → «12.3k»,
/// `1_000_000` → «1.0M».
#[allow(clippy::cast_precision_loss)]
fn fmt_tokens(n: usize) -> String {
    if n >= 1_000_000 {
        format!("{:.1}M", n as f64 / 1_000_000.0)
    } else if n >= 1_000 {
        format!("{:.1}k", n as f64 / 1_000.0)
    } else {
        n.to_string()
    }
}

/// Схлопывает домашний каталог в `~` для компактного статус-бара.
fn shorten_path(path: &Path) -> String {
    let s = path.to_string_lossy();
    if let Some(home) = dirs::home_dir() {
        let h = home.to_string_lossy();
        if let Some(rest) = s.strip_prefix(h.as_ref()) {
            return format!("~{rest}");
        }
    }
    s.into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tui::app::testing::{self, test_app};
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    #[test]
    fn clip_display_width_respects_cells() {
        assert_eq!(clip_display_width("abcdef", 4), "abcd");
        assert_eq!(clip_display_width("абвгде", 4), "абвг");
        // Двухклеточный символ не рвём: не влез — отбрасывается целиком.
        assert_eq!(clip_display_width("ab界cd", 3), "ab");
        assert_eq!(clip_display_width("ab界cd", 4), "ab界");
        assert_eq!(clip_display_width("короткий", 100), "короткий");
    }

    #[test]
    fn highlight_line_splits_spans_at_matches_case_insensitive() {
        use ratatui::style::Color;
        let line = Line::from(vec![
            Span::styled("abc GRPO def", Style::default().fg(Color::Green)),
            Span::styled(" grpo!", Style::default().fg(Color::Red)),
        ]);
        let hl = Style::default().bg(Color::Yellow);
        let cur = Style::default().bg(Color::Blue);
        // Текущая строка: оба совпадения — акцентным стилем (навигация
        // строчная: Enter прыгает по строкам, не по вхождениям внутри строки).
        let out = highlight_line(&line, "grpo", hl, cur, true);
        let cur_hits: Vec<_> = out
            .spans
            .iter()
            .filter(|s| s.style.bg == Some(Color::Blue))
            .collect();
        assert_eq!(cur_hits.len(), 2);
        assert_eq!(cur_hits[0].content.as_ref(), "GRPO");
        assert_eq!(cur_hits[1].content.as_ref(), "grpo");
        // Нетекущая строка: оба — базовой плашкой.
        let out = highlight_line(&line, "grpo", hl, cur, false);
        let plain_hits: Vec<_> = out
            .spans
            .iter()
            .filter(|s| s.style.bg == Some(Color::Yellow))
            .collect();
        assert_eq!(plain_hits.len(), 2);
        // Содержимое строки не изменилось (только разбивка спанов).
        let joined: String = out.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(joined, "abc GRPO def grpo!");
        // Пустая игла — строка без изменений.
        let same = highlight_line(&line, "", hl, cur, true);
        assert_eq!(same.spans.len(), 2);
    }

    #[test]
    fn search_renders_prompt_counter_and_highlight() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.push_block(ChatBlock::User("расскажи про GRPO и entropy".into()));
        app.push_block(ChatBlock::Assistant("GRPO держит entropy бонусом".into()));
        app.search = Some(crate::tui::app::DialogSearch {
            query: "grpo".into(),
            matches: Vec::new(),
            current: 0,
            dirty: true,
        });
        let mut term = Terminal::new(TestBackend::new(80, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(text.contains("поиск: grpo"), "нет строки поиска:\n{text}");
        assert!(text.contains("1/2"), "нет счётчика 1/2:\n{text}");
        assert!(text.contains("Esc — закрыть"), "нет подсказки:\n{text}");
        // Текущее совпадение выделено акцентной плашкой (bg оранжевый).
        let buf = term.backend().buffer();
        let highlighted = (0..buf.area.height).any(|y| {
            (0..buf.area.width).any(|x| {
                let c = &buf[(x, y)];
                c.bg == ratatui::style::Color::Rgb(0xff, 0x9e, 0x64) && c.symbol() == "G"
            })
        });
        assert!(highlighted, "текущее «GRPO» не подсвечено плашкой:\n{text}");
        // Поиск занял строку состояния ввода: has_input_state истина.
        assert!(app.has_input_state());
    }

    #[test]
    fn intent_hint_renders_in_input_status_row_and_yields_to_modal() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let hint =
            "гипотезы: laguna-gb10-ladder — поднять? · балл 3 · порог 2 · источник routing.json";
        testing::set_intent_hint(&mut app, Some(hint));
        let mut term = Terminal::new(TestBackend::new(100, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(text.contains(hint), "строки теплицы нет на экране:\n{text}");

        // Под блокирующей ask-модалкой строки нет: это факт о намерении, а не
        // живой вопрос (модалка — верхний слой, отвечать на него некуда).
        app.open_model_picker();
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(
            !text.contains("поднять?"),
            "строка теплицы просочилась под модалку:\n{text}"
        );
        app.ask = None;

        // Узкий терминал: строка режется по клеткам с многоточием, а не
        // ломается на середине слова и не уезжает на второй ряд.
        let mut narrow = Terminal::new(TestBackend::new(60, 24)).expect("term");
        narrow.draw(|f| app.render(f)).expect("draw");
        let rows: Vec<String> = buffer_rows(&narrow).iter().map(|r| r.concat()).collect();
        let dots = app.theme.glyphs.ellipsis();
        let row = rows
            .iter()
            .find(|r| r.contains("гипотезы"))
            .unwrap_or_else(|| panic!("строки теплицы нет на узком экране: {rows:?}"));
        assert!(row.contains(dots), "длинная строка не обрезана: {row:?}");
        assert!(
            !row.contains("routing.json"),
            "хвост строки обязан быть отрезан, а не перенесён: {row:?}"
        );
        assert_eq!(
            rows.iter().filter(|r| r.contains("гипотезы")).count(),
            1,
            "обрезка не должна оставлять вторую строку: {rows:?}"
        );
    }

    /// Текстовое содержимое буфера `TestBackend` (построчно).
    fn buffer_text(term: &Terminal<TestBackend>) -> String {
        let buf = term.backend().buffer();
        let area = buf.area;
        let mut s = String::new();
        for y in 0..area.height {
            for x in 0..area.width {
                s.push_str(buf[(x, y)].symbol());
            }
            s.push('\n');
        }
        s
    }

    /// Ширина диалога в клетках по верхней рамке: индекс первой `╮` + 1.
    fn dialog_width(term: &Terminal<TestBackend>) -> usize {
        buffer_rows(term)[0]
            .iter()
            .position(|c| c == "╮")
            .map(|i| i + 1)
            .expect("верхняя рамка диалога с углом ╮")
    }

    /// Схлопнутый текст левой колонки (диалог шириной `width`): подстроки,
    /// которые могут переноситься в узком диалоге, не должны ломаться о
    /// перенос, а соседняя правая панель — попадать в склейку. Вертикальные
    /// рамки выкидываем: `│` между словами разорвала бы поиск подстроки.
    fn flat_dialog(term: &Terminal<TestBackend>, width: u16) -> String {
        let buf = term.backend().buffer();
        let mut s = String::new();
        for y in 0..buf.area.height {
            for x in 0..width.min(buf.area.width) {
                let sym = buf[(x, y)].symbol();
                s.push_str(if sym == "│" { " " } else { sym });
            }
            s.push(' ');
        }
        s.split_whitespace().collect::<Vec<_>>().join(" ")
    }

    /// Строки буфера как ячейки (символ на колонку) — для проверок колонок
    /// и ASCII-префикса перед номером варианта.
    fn buffer_rows(term: &Terminal<TestBackend>) -> Vec<Vec<String>> {
        let buf = term.backend().buffer();
        let area = buf.area;
        (0..area.height)
            .map(|y| {
                (0..area.width)
                    .map(|x| buf[(x, y)].symbol().to_string())
                    .collect()
            })
            .collect()
    }

    /// Колонка начала последовательности `"{n}. "` в строке буфера, но
    /// только если перед ней стоит маркер варианта (ровно две ячейки ASCII:
    /// `"> "` у выбранного, `"  "` у остальных). Ограничение отсекает
    /// ложные совпадения вроде `"1. "` внутри `"11. "` или подстроки метки.
    fn option_number_col(row: &[String], n: usize) -> Option<usize> {
        let needle: Vec<String> = format!("{n}. ").chars().map(|c| c.to_string()).collect();
        if row.len() < needle.len() + 2 {
            return None;
        }
        (2..=row.len() - needle.len()).find(|&c| {
            row[c..c + needle.len()] == needle[..] && (row[c - 2] == " " || row[c - 2] == ">")
        })
    }

    /// Первая строка с номером `n` и колонкой его начала.
    fn find_option_number(rows: &[Vec<String>], n: usize) -> (usize, usize) {
        rows.iter()
            .enumerate()
            .find_map(|(y, row)| option_number_col(row, n).map(|c| (y, c)))
            .unwrap_or_else(|| panic!("номер «{n}. » не найден в буфере"))
    }

    /// Ask-модалка с заданными пунктами (label, description) для тестов.
    fn ask_app(options: Vec<(&str, &str)>) -> App {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.ask = Some(crate::tui::app::AskState {
            question: "Выберите вариант".into(),
            options: options
                .into_iter()
                .map(|(label, description)| crate::tool::AskOption {
                    label: label.into(),
                    description: description.into(),
                })
                .collect(),
            recommended: None,
            selected: 0,
            reply: None,
            kind: crate::tui::app::AskKind::Tool,
        });
        app
    }

    #[test]
    fn first_frame_is_the_working_chat_not_a_splash() {
        // Первый же кадр — рабочий экран: логотип как первый блок диалога и
        // пустое состояние, объясняющее, что делать (A1, A4).
        let mut app = test_app();
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        let banner_line = assets::BANNER
            .lines()
            .find(|l| !l.trim().is_empty())
            .expect("в баннере есть непустая строка");
        assert!(
            text.contains(banner_line.trim_end()),
            "логотипа нет на первом кадре:\n{text}"
        );
        assert!(text.contains("Диалог"), "нет рабочей области:\n{text}");
        assert!(
            text.contains("Напишите сообщение"),
            "пустое состояние не подсказывает, что делать:\n{text}"
        );
        // Подсказка переносится в узком диалоге — сверяем по схлопнутому тексту.
        let flat = flat_dialog(&terminal, 100 - RIGHT_WIDTH);
        assert!(
            flat.contains("? — список клавиш"),
            "нет пути к справке:\n{flat}"
        );
    }

    #[test]
    fn chat_shows_panels_statusbar_and_input() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "test:model".into();
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        for needle in [
            "Диалог",
            // Бар вкладок на базовой ширине деградирует до компактного: подпись
            // активной вкладки + иконки всех пяти (полный бар из подписей
            // требует 53 клетки и в 40 не влезает). Иконки — отдельно ниже.
            "Mermaid",
            "◇",
            "✓",
            "◈",
            "▶",
            "◉",
            "›",
            "test:model",
            // Подсказки клавиш приходят из реестра `keymap`: пара «клавиша подпись».
            "q выход",
            "? помощь",
        ] {
            assert!(text.contains(needle), "не найдено «{needle}»:\n{text}");
        }
        // Левая колонка с каталогом команд убрана: заголовка быть не должно.
        assert!(
            !text.contains("Команды"),
            "панель команд не скрыта:\n{text}"
        );
    }

    #[test]
    fn logo_block_renders_banner_as_first_dialog_lines() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.push_block(ChatBlock::Logo);
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("A I / M L   E D I T I O N"),
            "логотип не найден в диалоге:\n{text}"
        );
        assert!(
            text.contains("███████╗ ██████╗"),
            "арт SPINE не найден:\n{text}"
        );
    }

    #[test]
    fn chat_renders_user_assistant_and_tool_blocks() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.push_block(ChatBlock::User("привет, арх".into()));
        app.push_block(ChatBlock::Assistant("# Ответ\nтекст ответа".into()));
        app.push_block(ChatBlock::Tool {
            name: "kb_search".into(),
            action: "«архитектурные инварианты»".into(),
            state: ToolState::Ok,
            summary: "найдено 3 фрагмента".into(),
        });
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        for needle in [
            "● вы",
            "привет, арх",
            "◆ арх",
            "Ответ",
            "✓ kb_search",
            "найдено 3 фрагмента",
        ] {
            assert!(text.contains(needle), "не найдено «{needle}»:\n{text}");
        }
    }

    #[test]
    fn thinking_block_renders_compact_and_dimmed() {
        // Блок «мысли»: компактно (кап строк + счётчик хвоста).
        let mut app = test_app();
        app.screen = Screen::Chat;
        let long = (1..=20)
            .map(|i| format!("шаг рассуждения {i}"))
            .collect::<Vec<_>>()
            .join("\n");
        app.push_block(ChatBlock::Thinking(long));
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("мысли"), "заголовок блока:\n{text}");
        assert!(text.contains("шаг рассуждения 1"), "{text}");
        assert!(!text.contains("шаг рассуждения 20"), "хвост скрыт:\n{text}");
        assert!(text.contains("… (+"), "счётчик хвоста:\n{text}");
    }

    #[test]
    fn ascii_art_is_not_wrapped_in_dialog() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        // Арт шире диалога: при терминале 60 диалог ≈ 24 колонки внутри.
        let art = "┌──────────┐     ┌──────────┐     ┌──────────┐xxxx";
        app.push_block(ChatBlock::System {
            command: "mermaid".into(),
            text: art.to_string(),
        });
        let mut terminal = Terminal::new(TestBackend::new(60, 16)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        // Клип: начало строки арта сохраняет форму без разрыва переносом.
        assert!(
            text.contains("┌──────────┐     ┌───"),
            "арт не порван переносом:\n{text}"
        );
    }

    #[test]
    fn mermaid_tab_widens_to_fit_wide_art() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        // Арт 70 колонок: при терминале 150 панель растёт до 72 (кап 90).
        let wide = format!("┌{}┐", "─".repeat(68));
        app.panels.mermaid = format!("{wide}\n│{:^68}│\n└{}┘", "схема", "─".repeat(68));
        let mut terminal = Terminal::new(TestBackend::new(150, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains(&wide),
            "широкая схема показана целиком (панель расширилась):\n{text}"
        );
        assert!(
            !text.contains("F4 — вся схема"),
            "клипа нет — хинт не нужен"
        );
    }

    #[test]
    fn mermaid_tab_width_capped_with_f4_hint() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        // Арт 120 колонок при терминале 100: панель упирается в кап 60% (60),
        // остаток клипается, но вкладка подсказывает F4.
        let wide = format!("┌{}┐", "─".repeat(118));
        app.panels.mermaid = format!("{wide}\n└{}┘", "─".repeat(118));
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(!text.contains(&wide), "за капом — клип:\n{text}");
        assert!(
            text.contains("F4 — вся схема"),
            "хинт про полноэкранный просмотр:\n{text}"
        );
        // Бар вкладок цел: длинный хинт ушёл из него в нижний заголовок.
        assert!(text.contains("Mermaid"), "{text}");
        assert!(text.contains("Рубрика"), "{text}");
        assert!(text.contains("Знания"), "{text}");
    }

    #[test]
    fn mermaid_tab_clips_instead_of_wrapping() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        // Строка арта 41 колонка — шире вкладки даже после авто-расширения
        // (терминал 60 → кап 36, внутренняя ширина 34).
        app.panels.mermaid =
            "┌──────────┐     ┌──────────┐     ┌─────┐\n│    A     │─────▶│    B     │     │  C  │"
                .into();
        let mut terminal = Terminal::new(TestBackend::new(60, 16)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        // Перенос порвал бы строку и вынес бы третий блок «┌─────┐»
        // на отдельную строку; при клипе он не появляется никогда.
        assert!(
            text.contains("┌──────────┐     ┌──────────┐"),
            "начало строки арта целое:\n{text}"
        );
        assert!(
            !text.contains("┌─────┐"),
            "третий блок клипнут, а не перенесён:\n{text}"
        );
        assert!(text.contains("─▶"), "стрелка цела:\n{text}");
    }

    #[test]
    fn tab_strip_keeps_active_label_and_all_icons_at_normal_width() {
        use crate::tui::app::RightTab;
        // Полный бар из пяти подписей требует 53 клетки (юникод) / 54 (ASCII)
        // и в базовые внутренние 40 не влезает ни на одной ширине терминала —
        // это цена за широкий чат (RIGHT_WIDTH=42). Значит, бар обязан честно
        // деградировать: подпись активной вкладки + иконки всех пяти, ни одна
        // вкладка не срезана у правого края.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_tab = RightTab::Subagents;
        let mut term = Terminal::new(TestBackend::new(100, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(
            text.contains("Субагенты"),
            "подпись активной вкладки:\n{text}"
        );
        for icon in ["◇", "✓", "◈", "▶", "◉"] {
            assert!(text.contains(icon), "нет иконки {icon}:\n{text}");
        }
        for other in ["Mermaid", "Рубрика", "Знания", "Флот"] {
            assert!(!text.contains(other), "неактивные — без подписей:\n{text}");
        }
    }

    #[test]
    fn compact_tab_strip_keeps_all_five_icons() {
        use crate::tui::app::RightTab;
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_tab = RightTab::Subagents;
        let mut term = Terminal::new(TestBackend::new(64, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        for icon in ["◇", "✓", "◈", "▶", "◉"] {
            assert!(text.contains(icon), "нет иконки {icon}:\n{text}");
        }
        assert!(
            text.contains("Субагенты"),
            "активная вкладка с подписью в компактном баре:\n{text}"
        );
        assert!(
            !text.contains("Знания"),
            "неактивные вкладки — только иконками:\n{text}"
        );
    }

    #[test]
    fn ascii_tab_strip_shows_active_label_and_all_icons_at_any_width() {
        use crate::tui::app::RightTab;
        // Дефект: в ASCII полный бар — 54 клетки (у `< >` две клетки), поэтому
        // на широком терминале бар «деградировал» даже когда места вдоволь, и
        // подписи не показывались никогда, а два `>` были неразличимы. Теперь
        // деградация не зависит от `term_w`: активную подпись видно всегда.
        for w in [60u16, 80, 100, 200] {
            for active in [RightTab::Mermaid, RightTab::Subagents] {
                let mut app = ascii_app();
                app.right_tab = active;
                let mut term = Terminal::new(TestBackend::new(w, 24)).expect("term");
                term.draw(|f| app.render(f)).expect("draw");
                let text = buffer_text(&term);
                assert!(
                    text.contains(active.title()),
                    "ASCII: подпись активной вкладки «{}» пропала при ширине {w}:\n{text}",
                    active.title()
                );
                for icon in ["<>", "+", "#", ">", "@"] {
                    assert!(
                        text.contains(icon),
                        "ASCII: иконка {icon} срезана при ширине {w}:\n{text}"
                    );
                }
            }
        }
    }

    #[test]
    fn hidden_right_panel_gives_dialog_full_width() {
        // F5: панель скрыта — вкладок не видно, диалог занимает всю ширину.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_visible = false;
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(!text.contains("Mermaid"), "вкладка скрыта:\n{text}");
        assert!(text.contains("Диалог"), "{text}");
    }

    #[test]
    fn fatal_screen_shows_error_and_hint() {
        let mut app = test_app();
        app.screen = Screen::Fatal("default_model отсутствует".into());
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("Ошибка инициализации"));
        assert!(text.contains("default_model отсутствует"));
        assert!(text.contains("q — выход"));
    }

    #[test]
    fn narrow_terminal_does_not_panic() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut terminal = Terminal::new(TestBackend::new(20, 6)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
    }

    #[test]
    fn art_detection_ranges() {
        assert!(is_art("┌──┐"));
        assert!(is_art("─▶"));
        assert!(!is_art("обычный текст"));
        assert!(!is_art("▎ гуттер пользователя"));
        assert!(!is_art("✓ галка инструмента"));
    }

    #[test]
    fn status_bar_shows_running_subagents_count() {
        use crate::subagent::{SubagentRegistry, SubagentTask, TaskStatus};
        let registry = SubagentRegistry::new();
        registry.insert(SubagentTask {
            id: "sa-t-00".into(),
            agent: "general".into(),
            task: "разведка".into(),
            status: TaskStatus::Running,
            report: String::new(),
            started_at: "2026-08-15".into(),
            finished_at: None,
        });
        let ctx = test_app().tool_ctx.clone().with_subagents(registry);
        let mut app = test_app();
        app.tool_ctx = ctx;
        app.screen = Screen::Chat;
        let mut terminal = Terminal::new(TestBackend::new(110, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        // Дефект №5: сегмент показывал СПИСОК имён, который рос без границы и
        // выдавливал подсказки. Теперь — счётчик «работает/всего», имена живут
        // только на вкладке «Субагенты».
        assert!(
            text.contains("субагенты (1/1)"),
            "сегмент статус-бара — счётчик, а не список имён:\n{text}"
        );
        assert!(
            !text.contains("general"),
            "имена субагентов в статус-бар больше не протекают:\n{text}"
        );
        assert!(app.needs_tick(), "тики идут, пока крутятся субагенты");
    }

    /// Приложение с `n` работающими фоновыми задачами (реестр в памяти).
    fn app_with_running_tasks(n: usize) -> App {
        use crate::subagent::{SubagentRegistry, SubagentTask, TaskStatus};
        let registry = SubagentRegistry::new();
        for i in 0..n {
            registry.insert(SubagentTask {
                id: format!("sa-t-{i:02}"),
                agent: "general".into(),
                task: "разведка".into(),
                status: TaskStatus::Running,
                report: String::new(),
                started_at: "2026-08-15".into(),
                finished_at: None,
            });
        }
        let mut app = test_app();
        app.tool_ctx = app.tool_ctx.clone().with_subagents(registry);
        app.screen = Screen::Chat;
        app
    }

    #[test]
    fn subagents_tab_title_reflects_running_count() {
        // Заголовок — часть индикации хода исполнения: «Субагенты (2/3)» видно,
        // не открывая вкладку. Счётчик говорит и о работающих, и о всех
        // строках реестра (правило #5: состояние списка видно снаружи).
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_tab = RightTab::Subagents;
        let mut term = Terminal::new(TestBackend::new(100, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(text.contains("Субагенты"), "подпись вкладки:\n{text}");
        assert!(
            !text.contains("Субагенты ("),
            "при пустом реестре счётчика нет — панель пуста, шум не нужен:\n{text}"
        );

        let mut app = app_with_running_tasks(2);
        app.right_tab = RightTab::Subagents;
        let mut term = Terminal::new(TestBackend::new(100, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(
            text.contains("Субагенты (2/2)"),
            "заголовок отражает работающие задачи:\n{text}"
        );
    }

    /// Дефект №7: сессия, где ВСЕ задачи уже завершены (`running == 0`), но
    /// реестр не пуст, раньше теряла и счётчик в заголовке, и сегмент статуса:
    /// панель с содержимым выглядела как пустая. Правило tui-design-principles
    /// #5 («проектируй все состояния», список обязан показывать счётчик):
    /// счётчик присутствует всегда, когда есть строки, а не только пока идёт ход.
    #[test]
    fn subagents_counter_present_when_all_tasks_finished() {
        use crate::subagent::{SubagentRegistry, SubagentTask, TaskStatus};
        let registry = SubagentRegistry::new();
        for i in 0..7 {
            registry.insert(SubagentTask {
                id: format!("sa-d-{i:02}"),
                agent: "general".into(),
                task: "разведка".into(),
                status: TaskStatus::Done,
                report: String::new(),
                started_at: "2026-08-15".into(),
                finished_at: Some("2026-08-15".into()),
            });
        }
        let mut app = test_app();
        app.tool_ctx = app.tool_ctx.clone().with_subagents(registry);
        app.screen = Screen::Chat;
        app.right_tab = RightTab::Subagents;
        assert_eq!(app.subagents_running(), 0);
        assert_eq!(app.subagents_total(), 7);

        let mut term = Terminal::new(TestBackend::new(110, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(
            text.contains("Субагенты (0/7)"),
            "все завершены — заголовок всё равно считает строки:\n{text}"
        );
        assert!(
            text.contains("субагенты (0/7)"),
            "сегмент статус-бара виден и без работающих задач:\n{text}"
        );
    }

    #[test]
    fn right_panel_scrolls_to_the_end_of_long_content() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.panels.mermaid = (1..=40)
            .map(|i| format!("строка {i:02}"))
            .collect::<Vec<_>>()
            .join("\n");
        let mut term = Terminal::new(TestBackend::new(100, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(text.contains("строка 01"), "начало списка:\n{text}");
        assert!(!text.contains("строка 40"), "хвост за вьюпортом:\n{text}");
        assert!(
            text.contains("PgUp/PgDn · g/G 1-"),
            "счётчик показанного с РАБОТАЮЩИМИ клавишами (панель переполнена):\n{text}"
        );

        // Хвост достижим: сдвиг вниз до конца, рендер клампит его по
        // содержимому и пишет обратно — панель не «залипает».
        app.set_right_scroll(usize::MAX);
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(text.contains("строка 40"), "хвост достижим:\n{text}");
        assert!(!text.contains("строка 01"), "начало уехало:\n{text}");
        assert_eq!(
            app.right_scroll(),
            40 - app.right_viewport,
            "сдвиг клампится по содержимому"
        );
        assert!(text.contains("40/40"), "счётчик конца:\n{text}");
    }

    /// Дефект №4: `Length(42)` справа в паре с `Min(24)` выигрывал спор за
    /// место, и на узком терминале диалог сжимался ниже объявленного минимума
    /// (при 60 — до 18 клеток). Теперь панель уступает диалогу его минимум;
    /// на широких терминалах раскладка не меняется.
    #[test]
    fn dialog_keeps_its_minimum_width_on_narrow_terminals() {
        // (ширина терминала, ожидаемая ширина диалога): 60 и 80 — панель
        // сжата/закрыта в пользу диалога, 100 и 200 — прежние 42 у панели.
        let cases = [(60u16, 24usize), (80, 38), (100, 58), (200, 158)];
        for (w, want) in cases {
            let mut app = test_app();
            app.screen = Screen::Chat;
            let mut term = Terminal::new(TestBackend::new(w, 24)).expect("term");
            term.draw(|f| app.render(f)).expect("draw");
            let dw = dialog_width(&term);
            assert!(
                dw >= DIALOG_MIN_WIDTH as usize,
                "ширина {w}: диалог {dw} меньше минимума {DIALOG_MIN_WIDTH}"
            );
            assert_eq!(dw, want, "ширина {w}: диалог получил не свою долю");
        }
    }

    /// Дефект №5: список имён субагентов в статус-баре рос без границы и
    /// выдавливал основные подсказки — при 60×16 «Enter»/«Esc» исчезали.
    /// Теперь имена живут на вкладке, а статус несёт счётчик; бюджет подсказок
    /// защищён (правило #6: подсказки всегда на виду).
    #[test]
    fn narrow_status_bar_keeps_primary_hints() {
        let mut app = app_with_running_tasks(3);
        let mut term = Terminal::new(TestBackend::new(60, 16)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(
            text.contains("Enter"),
            "основная подсказка Enter уцелела:\n{text}"
        );
        assert!(
            text.contains("Esc"),
            "основная подсказка Esc уцелела:\n{text}"
        );
        assert!(
            !text.contains("general"),
            "имена субагентов в статус-бар не протекают:\n{text}"
        );
    }

    /// П.5: счётчик субагентов не рвётся посередине числа и не исчезает молча.
    /// На 60 колонках он раньше не выводился вовсе, на 80 — обрезался рендером
    /// посередине («⠋…»). Проверяем все три ширины: пара чисел целиком, в полной
    /// форме (`субагенты (3/3)`) или компактной (иконка вкладки `◉3/3`), и
    /// отсутствие обрубков вроде `3/ ` или `3/…`.
    #[test]
    fn subagents_counter_stays_whole_on_narrow_widths() {
        for w in [60u16, 80, 100] {
            for unicode in [true, false] {
                let mut app = app_with_running_tasks(3);
                if !unicode {
                    app.caps.unicode = false;
                    app.theme = Theme::for_caps(&app.caps);
                }
                let mut term = Terminal::new(TestBackend::new(w, 16)).expect("term");
                term.draw(|f| app.render(f)).expect("draw");
                let rows = buffer_rows(&term);
                let status = rows.last().cloned().unwrap_or_default().join("");
                assert!(
                    status.contains("3/3"),
                    "ширина {w} (unicode={unicode}): счётчик исчез из статуса — \
                     молчаливое выпадение, которого п.5 не допускает:\n{status}"
                );
                assert!(
                    !status.contains("3/ ") && !status.contains("3/…") && !status.contains("(3/ "),
                    "ширина {w} (unicode={unicode}): число счётчика обрублено посередине:\n{status}"
                );
            }
        }
    }

    /// П.6: заголовок вкладки и статус-бар показывают один факт одним форматом
    /// («Субагенты (3/3)» / «субагенты (3/3)»). Регистр различается только
    /// потому, что заголовок — часть подписи вкладки; скобки, порядок чисел и
    /// разделитель совпадают.
    #[test]
    fn subagents_counter_format_matches_tab_title() {
        let mut app = app_with_running_tasks(3);
        app.right_tab = RightTab::Subagents;
        let mut term = Terminal::new(TestBackend::new(110, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(
            text.contains("Субагенты (3/3)"),
            "заголовок вкладки несёт счётчик:\n{text}"
        );
        assert!(
            text.contains("субагенты (3/3)"),
            "статус-бар несёт тот же счётчик тем же форматом:\n{text}"
        );
        assert!(
            !text.contains("субагенты: 3/3"),
            "старый формат с двоеточием не должен остаться (п.6):\n{text}"
        );
    }

    /// Дефект №6: индикатор прокрутки обязан называть клавиши, которые
    /// реально работают. Раньше панель слушала только `Ctrl+U/D` (и ненадёжные
    /// `Alt+PgUp/PgDn`), а обычные `PgUp/PgDn` уходили в диалог — индикатор
    /// врал. Проверяем каждую клавишу из строки `PgUp/PgDn · g/G`.
    #[test]
    fn every_key_named_in_scroll_indicator_actually_scrolls() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.panels.mermaid = (1..=60)
            .map(|i| format!("строка {i:02}"))
            .collect::<Vec<_>>()
            .join("\n");
        let mut term = Terminal::new(TestBackend::new(100, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(
            text.contains("PgUp/PgDn"),
            "индикатор называет PgUp/PgDn:\n{text}"
        );
        assert!(text.contains("g/G"), "индикатор называет g/G:\n{text}");

        let key = |c, m| KeyEvent::new(c, m);
        // PgDn — вниз, PgUp — к началу.
        app.handle_key(key(KeyCode::PageDown, KeyModifiers::NONE));
        assert!(app.right_scroll() > 0, "PgDn обязан прокручивать вниз");
        app.handle_key(key(KeyCode::PageUp, KeyModifiers::NONE));
        assert_eq!(app.right_scroll(), 0, "PgUp обязан вернуть к началу");

        // Синонимы Ctrl+D/Ctrl+U ведут себя так же.
        app.handle_key(key(KeyCode::Char('d'), KeyModifiers::CONTROL));
        assert!(app.right_scroll() > 0, "Ctrl+D — синоним вниз");
        app.handle_key(key(KeyCode::Char('u'), KeyModifiers::CONTROL));
        assert_eq!(app.right_scroll(), 0, "Ctrl+U — синоним к началу");

        // G — в самый низ (клампится рендером), g — в самое начало.
        app.handle_key(key(KeyCode::Char('G'), KeyModifiers::SHIFT));
        assert_eq!(app.right_scroll(), app.right_max, "G — конец списка");
        term.draw(|f| app.render(f)).expect("draw");
        assert!(
            buffer_text(&term).contains("строка 60"),
            "G показал хвост:\n{}",
            buffer_text(&term)
        );
        app.handle_key(key(KeyCode::Char('g'), KeyModifiers::NONE));
        assert_eq!(app.right_scroll(), 0, "g — начало списка");
        term.draw(|f| app.render(f)).expect("draw");
        assert!(
            buffer_text(&term).contains("строка 01"),
            "g показал начало:\n{}",
            buffer_text(&term)
        );
    }

    #[test]
    fn tab_switch_refreshes_panel_content_immediately() {
        // Дефект: Tab/Shift+Tab ждали тикового опроса и до ~0.5 с показывали
        // заглушку, хотя реестр в памяти. Теперь переключение само
        // перечитывает «живую» вкладку — старый кадр не мелькает.
        let mut app = app_with_running_tasks(1);
        app.right_tab = RightTab::Mermaid;
        app.panels.subagents = "устаревший кадр".into();
        app.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT));
        assert_eq!(app.right_tab, RightTab::Subagents);
        let mut term = Terminal::new(TestBackend::new(100, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        assert!(
            text.contains("general"),
            "содержимое обновилось на этом же кадре:\n{text}"
        );
        assert!(
            !text.contains("устаревший кадр"),
            "старый кадр не мелькает:\n{text}"
        );
    }

    #[test]
    fn right_panel_scroll_resets_on_tab_switch() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.set_right_scroll(7);
        // Shift+Tab: Mermaid → Субагенты, прокрутка обязана обнулиться, иначе
        // новая вкладка открывалась бы с середины.
        app.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT));
        assert_eq!(app.right_tab, RightTab::Subagents);
        assert_eq!(
            app.right_scroll(),
            0,
            "прокрутка не залипает на новой вкладке"
        );
    }

    /// Цвет заливки первой ячейки шкалы контекста в буфере (None — не найдена).
    fn gauge_color(term: &Terminal<TestBackend>) -> Option<ratatui::style::Color> {
        let buf = term.backend().buffer();
        let area = buf.area;
        for y in 0..area.height {
            for x in 0..area.width {
                let cell = &buf[(x, y)];
                if cell.symbol() == "▰" {
                    return Some(cell.fg);
                }
            }
        }
        None
    }

    #[test]
    fn status_bar_shows_context_gauge() {
        use crate::tui::app::testing::set_context_usage;
        let mut app = test_app();
        app.screen = Screen::Chat;
        // Половина окна 1M: зелёная шкала, подпись «500.0k/1.0M … 50%».
        set_context_usage(&mut app, 500_000, 1_000_000);
        let mut terminal = Terminal::new(TestBackend::new(120, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("500.0k/1.0M"), "токены окна:\n{text}");
        assert!(text.contains("▰▰▰▰▱▱▱▱ 50%"), "шкала 50%:\n{text}");
        assert_eq!(
            gauge_color(&terminal),
            Some(ratatui::style::Color::Rgb(0x9e, 0xce, 0x6a)),
            "до L1 шкала зелёная"
        );
    }

    #[test]
    fn status_bar_gauge_turns_red_near_l3_threshold() {
        use crate::tui::app::testing::set_context_usage;
        let mut app = test_app();
        app.screen = Screen::Chat;
        // 96% окна: за порогом L3 (95%) — шкала красная.
        set_context_usage(&mut app, 960_000, 1_000_000);
        let mut terminal = Terminal::new(TestBackend::new(120, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("96%"), "процент заполнения:\n{text}");
        assert_eq!(
            gauge_color(&terminal),
            Some(ratatui::style::Color::Rgb(0xf7, 0x76, 0x8e)),
            "за L3 шкала красная"
        );
    }

    #[test]
    fn status_bar_gauge_orange_between_l1_and_l3() {
        use crate::tui::app::testing::set_context_usage;
        let mut app = test_app();
        app.screen = Screen::Chat;
        // 80% окна: между L1 (70%) и L3 (95%) — шкала оранжевая.
        set_context_usage(&mut app, 800_000, 1_000_000);
        let mut terminal = Terminal::new(TestBackend::new(120, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        assert_eq!(
            gauge_color(&terminal),
            Some(ratatui::style::Color::Rgb(0xff, 0x9e, 0x64)),
            "между L1 и L3 шкала оранжевая"
        );
    }

    #[test]
    fn queue_overlay_in_dialog_area_above_input() {
        use crate::tui::app::testing::set_thinking;
        let mut app = test_app();
        app.screen = Screen::Chat;
        set_thinking(&mut app, true);
        app.queue.push_back("первое в очереди".into());
        app.queue.push_back("второе\nмногострочное".into());
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("очередь · 2"), "заголовок карточки:\n{text}");
        assert!(
            text.contains("1. ▶ первое в очереди"),
            "первое — следующее на запуск:\n{text}"
        );
        // Многострочное сообщение — первой строкой с маркером ↵.
        assert!(
            text.contains("2. • второе ↵"),
            "вторая строка с маркером переноса:\n{text}"
        );
        // Карточка очереди — В окне логов: выше поля ввода (строки с «›»).
        let q_row = text.lines().position(|l| l.contains("1. ▶")).expect("▶");
        let i_row = text.lines().position(|l| l.contains('›')).expect("›");
        assert!(
            q_row < i_row,
            "очередь ({q_row}) над вводом ({i_row}):\n{text}"
        );
        // В поле ввода строк очереди больше нет — оно однострочное по дефолту.
        let input_rows = text
            .lines()
            .skip(i_row)
            .take_while(|l| !l.contains("F1"))
            .count();
        assert!(
            input_rows <= 4,
            "поле ввода не раздувается очередью:\n{text}"
        );
    }

    /// Инвариант класса 07.09: номер строки очереди — из ASCII и стоит ПЕРЕД
    /// маркером. Иначе ширина не-ASCII глифа в чужом шрифте сдвигает номер.
    #[test]
    fn queue_ordinal_is_ascii_and_precedes_marker() {
        use crate::tui::app::testing::set_thinking;
        let mut app = test_app();
        app.screen = Screen::Chat;
        set_thinking(&mut app, true);
        app.queue.push_back("первое".into());
        app.queue.push_back("второе".into());
        let mut term = Terminal::new(TestBackend::new(100, 30)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let rows = buffer_rows(&term);
        for n in ["1. ▶", "2. •"] {
            let row = rows
                .iter()
                .find(|r| r.join("").contains(n))
                .unwrap_or_else(|| panic!("нет строки очереди {n:?}"));
            let row = row.join("");
            let idx = row.find(n).expect("номер");
            let before = &row[..idx];
            assert!(
                !before.contains('▶') && !before.contains('•'),
                "маркер стоит перед номером: {before:?}"
            );
            assert!(
                row[idx..].starts_with(n),
                "строка должна начинаться с ASCII-номера: {row:?}"
            );
            assert!(
                n.chars().take(3).all(|c| c.is_ascii()),
                "номер не ASCII: {n:?}"
            );
        }
    }

    #[test]
    fn wrap_input_cases() {
        // Пустой ввод: одна строка, курсор за prompt'ом (ширина 2).
        let (rows, cr, cx) = wrap_input("", 0, 20);
        assert_eq!(rows, vec![String::new()]);
        assert_eq!((cr, cx), (0, 2));
        // Жёсткий перенос: две строки, курсор в конце второй.
        let (rows, cr, cx) = wrap_input("ab\ncd", 5, 20);
        assert_eq!(rows, vec!["ab".to_string(), "cd".to_string()]);
        assert_eq!((cr, cx), (1, 4));
        // Курсор в середине первой строки.
        let (_, cr, cx) = wrap_input("ab\ncd", 1, 20);
        assert_eq!((cr, cx), (0, 3));
        // Перенос по ширине: avail = 10-2 = 8, «0123456789» → 8 + 2.
        let (rows, cr, cx) = wrap_input("0123456789", 10, 10);
        assert_eq!(rows, vec!["01234567".to_string(), "89".to_string()]);
        assert_eq!((cr, cx), (1, 4));
    }

    #[test]
    fn input_grows_with_multiline_text() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.input.set_text("первая строка\nвторая строка".into());
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("первая строка"), "первая:\n{text}");
        assert!(text.contains("вторая строка"), "вторая:\n{text}");
        // Обе строки — внутри поля ввода: над статус-баром не более 4 строк.
        let first = text
            .lines()
            .position(|l| l.contains("первая строка"))
            .unwrap();
        let second = text
            .lines()
            .position(|l| l.contains("вторая строка"))
            .unwrap();
        assert_eq!(second, first + 1, "строки ввода идут подряд:\n{text}");
    }

    #[test]
    fn selection_highlights_cells_with_selection_style() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.push_block(ChatBlock::User("скопируй меня мышкой".into()));
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let inner = app.dialog_inner.expect("inner после рендера");
        // Выделяем 5 ячеек на строке сообщения (вторая строка контента —
        // после заголовка «вы»): колонки 2..6 от начала области.
        let row = inner.y + 1;
        app.selection = Some(((inner.x + 2, row), (inner.x + 6, row)));
        terminal.draw(|f| app.render(f)).expect("draw");
        let buf = terminal.backend().buffer();
        let sel_bg = ratatui::style::Color::Rgb(0x28, 0x34, 0x57);
        assert_eq!(
            buf[(inner.x + 2, row)].bg,
            sel_bg,
            "первая ячейка подсвечена"
        );
        assert_eq!(buf[(inner.x + 6, row)].bg, sel_bg, "пятая подсвечена");
        assert_ne!(
            buf[(inner.x + 7, row)].bg,
            sel_bg,
            "шестая — уже вне выделения"
        );
    }

    #[test]
    fn fmt_tokens_human_readable() {
        assert_eq!(fmt_tokens(0), "0");
        assert_eq!(fmt_tokens(999), "999");
        assert_eq!(fmt_tokens(1_500), "1.5k");
        assert_eq!(fmt_tokens(12_345), "12.3k");
        assert_eq!(fmt_tokens(1_000_000), "1.0M");
        assert_eq!(fmt_tokens(6_000_000), "6.0M");
    }

    /// Длинный диалог: 20 блоков × ~3 строки — прокрутка гарантирована.
    fn long_dialog_app() -> App {
        let mut app = test_app();
        app.screen = Screen::Chat;
        for i in 0..20 {
            app.push_block(ChatBlock::User(format!("сообщение {i}")));
        }
        app
    }

    #[test]
    fn dialog_shows_scrollbar_and_jump_button_when_scrolled() {
        let mut app = long_dialog_app();
        app.scroll_by(10);
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains('█'), "бегунок скроллбара:\n{text}");
        assert!(text.contains(" ▼ "), "кнопка к свежему ответу:\n{text}");
        assert!(app.jump_btn.is_some(), "область кнопки выставлена рендером");
        // Кнопка — в правом нижнем углу диалога. Ширину диалога не хардкодим:
        // она = терминал − правая панель, а [`RIGHT_WIDTH`] может меняться.
        let btn = app.jump_btn.expect("кнопка");
        let dialog_w = 100u16 - RIGHT_WIDTH;
        assert_eq!(btn.width, 3);
        assert_eq!(
            btn.x + btn.width,
            dialog_w - 1,
            "кнопка прижата к правому краю диалога: {btn:?}"
        );
        assert!(btn.y >= 20, "кнопка у нижнего края: {btn:?}");
    }

    #[test]
    fn no_jump_button_at_bottom_and_no_scrollbar_when_short() {
        // Диалог короче экрана: ни скроллбара, ни кнопки. Логотип стартового
        // экрана убираем — он сам по себе выше вьюпорта и здесь не про то.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.blocks.clear();
        app.push_block(ChatBlock::User("привет".into()));
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(!text.contains('█'), "бегунка быть не должно:\n{text}");
        assert!(!text.contains(" ▼ "), "кнопки быть не должно:\n{text}");
        assert!(app.jump_btn.is_none());

        // Длинный диалог, но у дна: скроллбар есть, кнопки нет.
        let mut app = long_dialog_app();
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains('█'), "бегунок виден у дна:\n{text}");
        assert!(!text.contains(" ▼ "), "у дна кнопка скрыта:\n{text}");
        assert!(app.jump_btn.is_none());
    }

    #[test]
    fn ask_modal_shows_question_options_and_hints() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.ask = Some(crate::tui::app::AskState {
            question: "Какой брокер сообщений выбрать?".into(),
            options: vec![
                crate::tool::AskOption {
                    label: "Kafka".into(),
                    description: "масштаб, но тяжёлый".into(),
                },
                crate::tool::AskOption {
                    label: "NATS".into(),
                    description: "лёгкий, без хранения".into(),
                },
            ],
            recommended: Some("Kafka".into()),
            selected: 0,
            reply: None,
            kind: crate::tui::app::AskKind::Tool,
        });
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        for needle in [
            "решение за вами",
            "Какой брокер сообщений выбрать?",
            "1. Kafka",
            "★",
            "2. NATS",
            "лёгкий, без хранения",
            "Enter",
            "Esc",
        ] {
            assert!(text.contains(needle), "не найдено «{needle}»:\n{text}");
        }
    }

    #[test]
    fn ask_modal_long_option_keeps_number() {
        // Регрессия по инциденту 07.09: в ask-модалке с длинным первым пунктом
        // (выбран, с ASCII-маркером «> ») номер «1.» обязан оставаться в
        // буфере — инвариант рендера опций при усечении длинной строки.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.ask = Some(crate::tui::app::AskState {
            question: "Где исполняются агентные прогоны (data plane) платформы Enterprise Harness? Это определит security boundary, сегментацию и дизайн control plane — менять позже дороже всего.".into(),
            options: vec![
                crate::tool::AskOption {
                    label: "Все прогоны — в выделенном буферном контуре (K8s/VM-пул), код приезжает через git-мост, агенты коммитят в worktree, наружу — только PR.".into(),
                    description: "Максимальный контроль: единая песочница, секреты не покидают контур, весь evidence в одном месте (CMP-07, PAY-11..13 закрываются естественно). Минусы: нужна доставка сред сборки (образы под стеки команд), латентность на clone/build, кап по вычислениям, буферный контур — новый объект ИБ-реестра.".into(),
                },
                crate::tool::AskOption { label: "Прогоны в CI-раннерах продуктовых команд".into(), description: "децентрализованно".into() },
                crate::tool::AskOption { label: "Локальные прогоны у архитекторов".into(), description: "минимум инфраструктуры".into() },
            ],
            recommended: None,
            selected: 0,
            reply: None,
            kind: crate::tui::app::AskKind::Tool,
        });
        // Размер терминала близкий к живому скриншоту (модалка ~78 колонок).
        let mut terminal = Terminal::new(TestBackend::new(120, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("1. Все прогоны"),
            "номер первого пункта потерян:\n{text}"
        );
        assert!(text.contains("2. Прогоны"), "второй пункт:\n{text}");
        // Третий пункт за пределами окна — скролл следует за выбором.
        if let Some(ask) = app.ask.as_mut() {
            ask.selected = 2;
        }
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("3. Локальные"),
            "третий пункт после скролла:\n{text}"
        );
    }

    #[test]
    fn ask_option_number_is_preceded_only_by_ascii_marker() {
        // Инвариант, закрывающий класс инцидента 07.09 навсегда: перед
        // номером варианта не должно быть НИ ОДНОГО не-ASCII символа —
        // иначе fontconfig при отсутствии глифа рисует фолбэк вне
        // моноширинной сетки и затирает цифру. Проверяем оба набора глифов
        // (Unicode и ASCII-фолбэк): класс дефекта не зависит от шрифта.
        for unicode in [true, false] {
            let mut app = if unicode {
                test_app()
            } else {
                let mut a = test_app();
                a.caps.unicode = false;
                a.theme = Theme::for_caps(&a.caps);
                a
            };
            app.screen = Screen::Chat;
            // 12 пунктов: номера 10-12 двузначные — маркер не должен
            // «разъезжаться» на смене ширины номера.
            app.ask = Some(crate::tui::app::AskState {
                question: "Выберите вариант".into(),
                options: (0..12)
                    .map(|i| crate::tool::AskOption {
                        label: format!("ВАРИАНТ-{i}"),
                        description: String::new(),
                    })
                    .collect(),
                recommended: None,
                selected: 3,
                reply: None,
                kind: crate::tui::app::AskKind::Tool,
            });
            let mut terminal = Terminal::new(TestBackend::new(100, 40)).expect("terminal");
            terminal.draw(|f| app.render(f)).expect("draw");
            let rows = buffer_rows(&terminal);
            for n in 1..=12usize {
                let (y, col) = find_option_number(&rows, n);
                assert!(col >= 2, "номер «{n}. » без маркера (unicode={unicode})");
                let (mark0, mark1) = (&rows[y][col - 2], &rows[y][col - 1]);
                assert!(
                    mark0.is_ascii() && mark1.is_ascii(),
                    "перед номером «{n}. » не-ASCII маркер {mark0:?}{mark1:?} (unicode={unicode})"
                );
                assert!(
                    mark0 == ">" || mark0 == " ",
                    "маркер «{n}. » не '>' и не пробел: {mark0:?} (unicode={unicode})"
                );
                assert_eq!(mark1, " ", "второй байт маркера «{n}. » не пробел");
                // Сам номер с точкой заканчивается ASCII-пробелом.
                let dot = format!("{n}. ");
                let cells: String = rows[y][col..col + dot.len()].concat();
                assert!(cells.is_ascii(), "номер «{n}. » не ASCII: {cells:?}");
                assert_eq!(cells, dot, "номер «{n}. » повреждён");
            }
        }
    }

    #[test]
    fn ask_option_number_sits_in_constant_column() {
        // Цифры должны стоять в фиксированной колонке независимо от числа
        // пунктов и длины меток: иначе колонка «пляшет» и список читается
        // хуже (моноширинная сетка, `tui-layout-components`).
        let cases: Vec<Vec<(&str, &str)>> = vec![
            vec![("Kafka", "масштаб")],
            vec![("Kafka", "масштаб"), ("NATS", "лёгкий")],
            vec![
                ("Короткий", ""),
                ("Вариант подлиннее, чем все остальные вместе", ""),
                ("Средний вариант", ""),
                ("Ещё", ""),
                ("Пятый", ""),
            ],
        ];
        let mut cols = Vec::new();
        for options in cases {
            let mut app = ask_app(options);
            let mut terminal = Terminal::new(TestBackend::new(100, 40)).expect("terminal");
            terminal.draw(|f| app.render(f)).expect("draw");
            let rows = buffer_rows(&terminal);
            cols.push(find_option_number(&rows, 1).1);
        }
        assert!(
            cols.windows(2).all(|w| w[0] == w[1]),
            "колонка номера не постоянна: {cols:?}"
        );
    }

    #[test]
    fn ask_hint_range_is_honest() {
        // Клавиши быстрого выбора — только '1'..='9' (app.rs). Подсказка не
        // имеет права обещать больше, чем есть: 2 пункта → «1-2»,
        // 12 пунктов → «1-9 + ↑/↓» (клавиши только у первых девяти, дальше
        // стрелками), 0 пунктов → никакого диапазона. Счётчик позиции
        // (`{selected+1}/{len}`) при этом обязан остаться виден — ради него
        // подсказка и укорочена.
        let render = |options: Vec<(&str, &str)>| -> String {
            let mut app = ask_app(options);
            let mut terminal = Terminal::new(TestBackend::new(100, 40)).expect("terminal");
            terminal.draw(|f| app.render(f)).expect("draw");
            buffer_text(&terminal)
        };

        let two = render(vec![("Kafka", ""), ("NATS", "")]);
        assert!(two.contains("1-2"), "для двух пунктов ждали «1-2»:\n{two}");
        assert!(!two.contains("дальше"), "2 пункта — оговорка не нужна");
        assert!(!two.contains("1-4"), "жёсткий «1-4» больше не показываем");

        let many = render((0..12).map(|_| ("Вариант", "")).collect());
        assert!(
            many.contains("1-9 +"),
            "для 12 пунктов ждали «1-9 + ↑/↓»:\n{many}"
        );
        assert!(
            !many.contains("1-12"),
            "обещать 1-12 нельзя — клавиш только девять:\n{many}"
        );
        assert!(
            many.contains("/12"),
            "счётчик позиции вытеснен подсказкой (потерян контекст):\n{many}"
        );

        let zero = render(vec![]);
        assert!(
            !zero.contains("быстро"),
            "без пунктов нечего предлагать:\n{zero}"
        );
        assert!(!zero.contains("1-"), "нет пунктов — нет диапазона:\n{zero}");

        // У одного пункта диапазона «1-1» быть не должно: это диапазон из
        // одного числа, читателю он ничего не сообщает — в подсказке просто «1».
        let one = render(vec![("Единственный", "")]);
        assert!(
            !one.contains("1-1"),
            "для одного пункта ждали «1», а не «1-1»:\n{one}"
        );
        assert!(
            one.contains("1 быстро"),
            "нет честной подсказки «1»:\n{one}"
        );

        // У нуля пунктов не обещаем ни стрелок, ни цифр: выбирать не из чего,
        // единственное осмысленное действие — «ответить».
        assert!(
            !zero.contains("выбор"),
            "без пунктов нечего выбирать стрелками:\n{zero}"
        );
        assert!(
            zero.contains("ответить"),
            "нулевой список обязан оставить путь «ответить»:\n{zero}"
        );
    }

    /// Счётчик позиции `N/M` обязан выживать при узкой модалке в обоих
    /// наборах глифов и при любом числе пунктов: это самый ценный элемент
    /// подсказки (сколько всего вариантов и где курсор). Раньше он был
    /// хвостом одной строки и клипался первым; теперь под него резервируется
    /// отдельная правоприжатая зона.
    #[test]
    fn ask_counter_survives_narrow_width_in_unicode_and_ascii() {
        for unicode in [true, false] {
            for n in [2usize, 4, 12] {
                for width in [60u16, 80] {
                    let mut app = if unicode { test_app() } else { ascii_app() };
                    app.screen = Screen::Chat;
                    app.ask = Some(crate::tui::app::AskState {
                        question: "Выберите вариант съёмки прототипа".into(),
                        options: (0..n)
                            .map(|i| crate::tool::AskOption {
                                label: format!("Вариант-{i}"),
                                description: String::new(),
                            })
                            .collect(),
                        recommended: None,
                        selected: 1,
                        reply: None,
                        kind: crate::tui::app::AskKind::Tool,
                    });
                    let mut terminal =
                        Terminal::new(TestBackend::new(width, 40)).expect("terminal");
                    terminal.draw(|f| app.render(f)).expect("draw");
                    let text = buffer_text(&terminal);
                    let counter = format!("2/{n}");
                    assert!(
                        text.contains(&counter),
                        "счётчик «{counter}» потерян (width={width}, n={n}, unicode={unicode}):\n{text}"
                    );
                }
            }
        }
    }

    /// Подсказка обязана называть страничные клавиши ровно тогда, когда список
    /// длиннее окна: `PgUp`/`PgDn`/`Home`/`End` упомянуты при переполнении и
    /// молчат, когда листать нечего (обещание несуществующего листания — такая
    /// же ложь, как умолчание о нём). Раньше подсказка была статичной и эти
    /// клавиши не называла вовсе, хотя хендлер их обрабатывал.
    #[test]
    fn ask_hint_names_page_keys_only_when_list_overflows_window() {
        for unicode in [true, false] {
            let mut terminal = Terminal::new(TestBackend::new(80, 24)).expect("terminal");
            // Переполнение: 12 вариантов с описанием (24 строки) не влезают в
            // тело модалки — подсказка обязана назвать страничные клавиши.
            let mut over = if unicode { test_app() } else { ascii_app() };
            over.screen = Screen::Chat;
            over.ask = Some(crate::tui::app::AskState {
                question: "Выберите вариант".into(),
                options: (0..12)
                    .map(|i| crate::tool::AskOption {
                        label: format!("Вариант-{i}"),
                        description: "описание".into(),
                    })
                    .collect(),
                recommended: None,
                selected: 0,
                reply: None,
                kind: crate::tui::app::AskKind::Tool,
            });
            terminal.draw(|f| over.render(f)).expect("draw");
            let text = buffer_text(&terminal);
            assert!(
                over.ask_viewport > 0 && over.ask_viewport < 24,
                "рендер обязан записать высоту тела модалки (unicode={unicode}): \
                 ask_viewport={}",
                over.ask_viewport
            );
            assert!(
                text.contains("PgUp/PgDn"),
                "при переполнении подсказка обязана назвать PgUp/PgDn \
                 (unicode={unicode}):\n{text}"
            );
            assert!(
                text.contains("Home/End"),
                "при переполнении подсказка обязана назвать Home/End \
                 (unicode={unicode}):\n{text}"
            );
            assert!(
                text.contains("/12"),
                "счётчик позиции обязан остаться и при переполнении \
                 (unicode={unicode}):\n{text}"
            );

            // Список короче окна: страничные клавиши не обещаем.
            let mut fits = if unicode { test_app() } else { ascii_app() };
            fits.screen = Screen::Chat;
            fits.ask = Some(crate::tui::app::AskState {
                question: "Выберите вариант".into(),
                options: (0..3)
                    .map(|i| crate::tool::AskOption {
                        label: format!("Вариант-{i}"),
                        description: String::new(),
                    })
                    .collect(),
                recommended: None,
                selected: 0,
                reply: None,
                kind: crate::tui::app::AskKind::Tool,
            });
            terminal.draw(|f| fits.render(f)).expect("draw");
            let text = buffer_text(&terminal);
            assert!(
                !text.contains("PgUp"),
                "без переполнения подсказка не смеет обещать листание \
                 (unicode={unicode}):\n{text}"
            );
            assert!(
                !text.contains("Home/End"),
                "без переполнения подсказка не смеет обещать Home/End \
                 (unicode={unicode}):\n{text}"
            );
            assert!(
                text.contains("1-3"),
                "когда листать нечего, остаётся честный быстрый диапазон \
                 (unicode={unicode}):\n{text}"
            );
        }
    }

    /// Длинная рекомендованная метка обязана: (1) получить многоточие-индикатор
    /// усечения и (2) не вытеснить звёздочку рекомендации за край панели.
    /// Регрессия: раньше звезда резервировалась хвостом и пропадала первой —
    /// ровно тот знак, ради которого строка помечена.
    #[test]
    fn ask_long_recommended_label_keeps_star_and_ellipsis() {
        let long = "Очень длинная метка варианта, которая заведомо не помещается целиком \
                    в узкой модалке и должна быть усечена с явным индикатором обрезки";
        for unicode in [true, false] {
            let mut app = if unicode { test_app() } else { ascii_app() };
            app.screen = Screen::Chat;
            app.ask = Some(crate::tui::app::AskState {
                question: "Выберите вариант".into(),
                options: vec![crate::tool::AskOption {
                    label: long.into(),
                    description: String::new(),
                }],
                recommended: Some(long.into()),
                selected: 0,
                reply: None,
                kind: crate::tui::app::AskKind::Tool,
            });
            let dots = app.theme.glyphs.ellipsis().to_string();
            let star = app.theme.glyphs.star().to_string();
            let mut terminal = Terminal::new(TestBackend::new(60, 30)).expect("terminal");
            terminal.draw(|f| app.render(f)).expect("draw");
            let text = buffer_text(&terminal);
            assert!(
                text.contains(&dots),
                "усечение без индикатора «{dots}» (unicode={unicode}):\n{text}"
            );
            assert!(
                text.contains(&star),
                "звёздочка «{star}» вытеснена длинной меткой (unicode={unicode}):\n{text}"
            );
        }
    }

    /// Двузначный номер не должен сдвигать метку: «10. » и « 1. » обязаны
    /// начинать подпись в одной колонке (фиксированная ширина поля номера).
    #[test]
    fn ask_two_digit_number_keeps_label_column() {
        let labels: Vec<String> = (1..=12).map(|i| format!("L{i:02}")).collect();
        let mut app = ask_app(labels.iter().map(|l| (l.as_str(), "")).collect::<Vec<_>>());
        app.ask.as_mut().expect("ask").selected = 0;
        let mut terminal = Terminal::new(TestBackend::new(100, 40)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let rows = buffer_rows(&terminal);
        let label_col = |lab: &str| -> usize {
            let cells: Vec<String> = lab.chars().map(|c| c.to_string()).collect();
            for row in &rows {
                if row.len() >= cells.len() {
                    if let Some(c) = (0..=row.len() - cells.len())
                        .find(|&c| row[c..c + cells.len()] == cells[..])
                    {
                        return c;
                    }
                }
            }
            panic!("метка «{lab}» не найдена в буфере");
        };
        let cols: Vec<usize> = labels.iter().map(|l| label_col(l)).collect();
        assert!(
            cols.windows(2).all(|w| w[0] == w[1]),
            "колонка метки пляшет на двузначных номерах: {cols:?}"
        );
    }

    /// Рендер-замок к theme-тесту `mono_label_is_not_dimmer_than_number`:
    /// в буфере подпись невыбранного варианта обязана идти основным текстом
    /// (`base()`), а номер — служебным акцентом (`number_key()`), а не наоборот.
    #[test]
    fn ask_option_label_uses_body_style_not_number_style() {
        let mut app = ask_app(vec![("Kafka", ""), ("NATS", "")]);
        app.ask.as_mut().expect("ask").selected = 0;
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let rows = buffer_rows(&terminal);
        let buf = terminal.backend().buffer();
        // Невыбранный пункт «NATS» — метка базовым стилем.
        let needle: Vec<String> = "NATS".chars().map(|c| c.to_string()).collect();
        let (y, x) = rows
            .iter()
            .enumerate()
            .find_map(|(y, row)| {
                (0..=row.len().saturating_sub(needle.len()))
                    .find(|&x| row[x..x + needle.len()] == needle[..])
                    .map(|x| (y, x))
            })
            .expect("метка NATS не найдена");
        assert_eq!(
            buf[(x as u16, y as u16)].fg,
            app.theme.base().fg.unwrap(),
            "метка невыбранного варианта не основным текстом"
        );
        // Номер «2. » этой же строки — служебным акцентом.
        let dcol = option_number_col(&rows[y], 2).expect("номер «2. »");
        assert_eq!(
            buf[(dcol as u16, y as u16)].fg,
            app.theme.number_key().fg.unwrap(),
            "номер варианта не акцентом формы"
        );
    }

    /// Приложение с ASCII-набором глифов (как при `--ascii` или `LANG=C`).
    fn ascii_app() -> App {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.caps.unicode = false;
        app.theme = Theme::for_caps(&app.caps);
        app
    }

    #[test]
    fn help_overlay_lists_grouped_keys() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "test:model".into();
        app.help = true;
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        for needle in [
            "Помощь",
            "ВВОД",
            "НАВИГАЦИЯ",
            "Enter",
            "PgUp/PgDn",
            "закрыть",
        ] {
            assert!(text.contains(needle), "справка без «{needle}»:\n{text}");
        }
    }

    #[test]
    fn help_overlay_survives_narrow_and_short_windows() {
        for (w, h) in [(80u16, 24u16), (60, 16)] {
            let mut app = test_app();
            app.screen = Screen::Chat;
            app.help = true;
            let mut terminal = Terminal::new(TestBackend::new(w, h)).expect("terminal");
            terminal.draw(|f| app.render(f)).expect("draw");
            assert!(
                buffer_text(&terminal).contains("Помощь"),
                "справка не отрисовалась в {w}×{h}"
            );
        }
        // Ниже минимума показываем заглушку, а не справку в каше.
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.help = true;
        let mut terminal = Terminal::new(TestBackend::new(40, 12)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        assert!(buffer_text(&terminal).contains("Терминал мал"));
    }

    #[test]
    fn ascii_mode_gives_plain_borders_and_no_box_drawing() {
        let mut app = ascii_app();
        app.model_name = "test:model".into();
        let mut terminal = Terminal::new(TestBackend::new(90, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        for box_glyph in ["─", "│", "╭", "╮", "▰", "▱"] {
            assert!(
                !text.contains(box_glyph),
                "ASCII-режим всё ещё рисует «{box_glyph}»:\n{text}"
            );
        }
        // Шкала контекста и рамки — из ASCII-набора.
        assert!(
            text.contains('#') || text.contains('|'),
            "нет ASCII-рамки:\n{text}"
        );
    }

    #[test]
    fn nerd_of_no_color_still_shows_marks_by_symbol() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.caps.color = crate::tui::caps::ColorLevel::Mono;
        app.theme = Theme::for_caps(&app.caps);
        app.push_block(ChatBlock::Tool {
            name: "kb_search".into(),
            action: "«outbox»".into(),
            state: ToolState::Error,
            summary: "ошибка сети".into(),
        });
        let mut terminal = Terminal::new(TestBackend::new(90, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        // Смысл ошибки несёт символ, а не цвет: монохром ничего не теряет.
        assert!(buffer_text(&terminal).contains('✗'));
    }

    #[test]
    fn too_small_terminal_shows_a_stub_with_sizes() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        let mut terminal = Terminal::new(TestBackend::new(59, 15)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("Терминал мал"), "{text}");
        assert!(text.contains("59×15"), "нужен текущий размер: {text}");
        assert!(text.contains("60×16"), "нужен требуемый размер: {text}");
        assert!(text.contains('q'), "нужен способ выйти: {text}");
    }

    #[test]
    fn minimum_size_boundary_renders_the_normal_screen() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.model_name = "test:model".into();
        let mut terminal = Terminal::new(TestBackend::new(60, 16)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(!text.contains("Терминал мал"), "60×16 — уже рабочий размер");
        assert!(text.contains("Диалог"), "{text}");
    }

    #[test]
    fn fatal_screen_hints_how_to_copy_the_error() {
        let mut app = test_app();
        app.screen = Screen::Fatal("нет ключа GIGACHAT_API_KEY".into());
        let mut terminal = Terminal::new(TestBackend::new(90, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("нет ключа"), "{text}");
        assert!(
            text.contains("скопировать"),
            "нет подсказки про копирование: {text}"
        );
        // Fatal показывается и в маленьком окне: причина важнее размера.
        let mut terminal = Terminal::new(TestBackend::new(40, 12)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        assert!(buffer_text(&terminal).contains("нет ключа"));
    }

    #[test]
    fn chrome_is_two_frames_not_three() {
        // Рамок на экране две: диалог и панель вкладок. Поле ввода — без
        // рамки (AP1: рамка только там, где объясняет структуру).
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.blocks.clear();
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert_eq!(
            text.matches('╭').count(),
            2,
            "лишние рамки на экране:\n{text}"
        );
        assert!(text.contains("›"), "prompt ввода на месте:\n{text}");
    }

    #[test]
    fn state_line_appears_only_while_busy() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.blocks.clear();
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        assert!(
            !buffer_text(&terminal).contains("модель думает"),
            "в простое строки состояния нет — она не занимает место"
        );

        testing::set_thinking(&mut app, true);
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("модель думает"),
            "ход виден над вводом:\n{text}"
        );
        assert!(
            text.contains("Esc — прервать"),
            "есть способ отменить:\n{text}"
        );
    }

    #[test]
    fn state_line_shows_running_tool_instead_of_generic_thinking() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.blocks.clear();
        testing::set_thinking(&mut app, true);
        // Выполняется bash: строка состояния говорит, ЧТО именно происходит,
        // а не общее «модель думает» (кейс «модель ушла в офлайн»).
        app.push_block(ChatBlock::Tool {
            name: "bash".into(),
            state: ToolState::Running,
            action: ": cargo test".into(),
            summary: String::new(),
        });
        let idx = app.blocks.len() - 1;
        app.tool_live.insert(
            idx,
            ToolLive {
                started: std::time::Instant::now(),
                tail: String::new(),
            },
        );
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("выполняется: bash: cargo test · 0:0"),
            "видно, что происходит и сколько уже:\n{text}"
        );
        assert!(
            !text.contains("модель думает"),
            "общий спиннер уступил место конкретике:\n{text}"
        );
    }

    #[test]
    fn stream_tok_rate_rough_estimate() {
        assert_eq!(stream_tok_rate(400, 2.0), (100, 50));
        assert_eq!(
            stream_tok_rate(10, 0.1),
            (2, 0),
            "до 0,5 с скорость не считаем"
        );
        assert_eq!(stream_tok_rate(0, 10.0), (0, 0));
    }

    #[test]
    fn state_line_shows_stream_progress_while_answering() {
        use crate::agent::AgentEvent;
        use crate::tui::app::AppMessage;
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.blocks.clear();
        testing::set_thinking(&mut app, true);
        // 40 дельт по 4 байта = 160 байт ≈ 40 токенов (грубая оценка 4 ≈ 1).
        for _ in 0..40 {
            app.handle_message(AppMessage::AgentEvent(AgentEvent::Delta("abc ".into())));
        }
        let mut terminal = Terminal::new(TestBackend::new(110, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("отвечает · 0:0"),
            "видно, что модель стримит ответ:\n{text}"
        );
        assert!(text.contains("~40 ток"), "есть оценка объёма:\n{text}");
        assert!(text.contains("т/с"), "есть скорость стрима:\n{text}");
    }

    #[test]
    fn queue_is_visible_in_the_state_line() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.blocks.clear();
        app.queue.push_back("второе".into());
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("очередь: 1"),
            "очередь видна в строке состояния:\n{text}"
        );
    }

    #[test]
    fn viewer_fullscreen_pans_wide_art_horizontally() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        // Арт 5+100+5 колонок — заведомо шире терминала.
        let art = format!("LEFT{}RIGHT", "─".repeat(100));
        app.panels.mermaid = art;
        let mut terminal = Terminal::new(TestBackend::new(80, 20)).expect("terminal");

        // Без панорамы: левый край виден, правый — клипнут.
        app.viewer = Some(crate::tui::app::ViewerState {
            scroll_x: 0,
            scroll_y: 0,
        });
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("на весь экран"),
            "заголовок просмотрщика:\n{text}"
        );
        assert!(text.contains("LEFT"), "левый край при scroll_x=0:\n{text}");
        assert!(!text.contains("RIGHT"), "правый край клипнут:\n{text}");

        // Панорама вправо: левый край ушёл, правый показался
        // (scroll_x=60 клампится к max 109-78=31 — проверяем индикатор без числа).
        app.viewer = Some(crate::tui::app::ViewerState {
            scroll_x: 60,
            scroll_y: 0,
        });
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(!text.contains("LEFT"), "левый край за окном:\n{text}");
        assert!(
            text.contains("RIGHT"),
            "правый край показан панорамой:\n{text}"
        );
        assert!(text.contains("→"), "индикатор позиции:\n{text}");
    }

    #[test]
    fn hclip_drops_columns_unicode_safely() {
        let line = Line::from(Span::styled("┌──┐abcdef".to_string(), Style::default()));
        let clipped = hclip_line(&line, 4);
        let text: String = clipped.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(text, "abcdef", "срезаны ровно 4 display-колонки");
        let clipped = hclip_line(&line, 0);
        let text: String = clipped.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(text, "┌──┐abcdef", "нулевой оффсет — строка цела");
    }

    fn tool(name: &str, state: ToolState, action: &str, summary: &str) -> ChatBlock {
        ChatBlock::Tool {
            name: name.into(),
            state,
            action: action.into(),
            summary: summary.into(),
        }
    }

    fn plain(lines: &[Line<'static>]) -> String {
        lines
            .iter()
            .map(|l| {
                l.spans
                    .iter()
                    .map(|s| s.content.as_ref())
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn tool_run_short_series_shows_every_call_with_action() {
        let theme = Theme::default();
        let run = vec![
            tool(
                "read_file",
                ToolState::Ok,
                "src/control.rs",
                "прочитано 2 КБ",
            ),
            tool("grep", ToolState::Ok, "'fitness'", "12 совпадений"),
            tool("bash", ToolState::Running, ": cargo test", ""),
        ];
        let text = plain(&tool_run_lines(
            &run,
            0,
            &std::collections::HashMap::new(),
            &theme,
        ));
        assert!(text.contains("✓ read_file src/control.rs"), "{text}");
        assert!(text.contains("✓ grep 'fitness'"), "{text}");
        assert!(text.contains("◌ bash : cargo test"), "{text}");
        // Итоги скрыты, пока серия не завершилась (последний — Running).
        assert!(!text.contains("12 совпадений"), "{text}");
        assert!(!text.contains("прочитано 2 КБ"), "{text}");
        assert!(!text.contains("… +"), "{text}");
        // Серия завершена успехом — одна строка итога у последнего.
        let done = vec![
            tool("read_file", ToolState::Ok, "src/a.rs", "прочитано"),
            tool("bash", ToolState::Ok, ": cargo test", "900 passed"),
        ];
        let text = plain(&tool_run_lines(
            &done,
            0,
            &std::collections::HashMap::new(),
            &theme,
        ));
        assert!(text.contains("900 passed"), "{text}");
        assert!(!text.contains("прочитано\n"), "{text}");
    }

    #[test]
    fn tool_run_long_series_collapses_middle() {
        let theme = Theme::default();
        let run: Vec<ChatBlock> = (0..10)
            .map(|i| tool("bash", ToolState::Ok, &format!(": cmd{i}"), "ok"))
            .collect();
        let text = plain(&tool_run_lines(
            &run,
            0,
            &std::collections::HashMap::new(),
            &theme,
        ));
        assert!(text.contains("✓ bash : cmd0"), "{text}");
        assert!(text.contains("✓ bash : cmd1"), "{text}");
        assert!(text.contains("… +5 вызовов"), "{text}");
        assert!(text.contains("✓ bash : cmd7"), "{text}");
        assert!(text.contains("✓ bash : cmd9"), "{text}");
        assert!(!text.contains(": cmd4\n"), "{text}");
    }

    #[test]
    fn tool_run_error_shows_summary_line() {
        let theme = Theme::default();
        let run = vec![
            tool("bash", ToolState::Ok, ": cargo build", "собрано"),
            tool("bash", ToolState::Error, ": cargo test", "FAILED: 2 теста"),
        ];
        let text = plain(&tool_run_lines(
            &run,
            0,
            &std::collections::HashMap::new(),
            &theme,
        ));
        assert!(text.contains("✗ bash : cargo test"), "{text}");
        assert!(text.contains("FAILED: 2 теста"), "{text}");
    }

    #[test]
    fn fmt_elapsed_minutes_and_hours() {
        assert_eq!(fmt_elapsed(0), "0:00");
        assert_eq!(fmt_elapsed(7), "0:07");
        assert_eq!(fmt_elapsed(65), "1:05");
        assert_eq!(fmt_elapsed(3599), "59:59");
        assert_eq!(fmt_elapsed(3600), "1:00:00");
        assert_eq!(fmt_elapsed(9000), "2:30:00");
    }

    #[test]
    fn truncate_chars_adds_ellipsis() {
        assert_eq!(truncate_chars("короткий", 60), "короткий");
        let long = "а".repeat(100);
        let out = truncate_chars(&long, 10);
        assert_eq!(out.chars().count(), 10, "{out}");
        assert!(out.ends_with('…'), "{out}");
    }

    #[test]
    fn tool_run_running_shows_live_tail_and_timer() {
        let theme = Theme::default();
        let run = vec![
            tool("read_file", ToolState::Ok, "src/a.rs", "прочитано"),
            tool("bash", ToolState::Running, ": cargo test", ""),
        ];
        let mut live = std::collections::HashMap::new();
        live.insert(
            1usize,
            ToolLive {
                started: std::time::Instant::now(),
                tail: "running 900 tests\nline two\n  … test fitness_rules ... ok\ncompiling arch"
                    .into(),
            },
        );
        let text = plain(&tool_run_lines(&run, 0, &live, &theme));
        assert!(
            text.contains("◌ bash : cargo test · 0:0"),
            "живой таймер: {text}"
        );
        assert!(
            text.contains("test fitness_rules ... ok"),
            "хвост вывода виден: {text}"
        );
        assert!(
            text.contains("compiling arch"),
            "последняя строка хвоста видна: {text}"
        );
        assert!(
            !text.contains("running 900 tests"),
            "старые строки хвоста уходят за предел TOOL_TAIL_LINES: {text}"
        );
    }

    #[test]
    fn tool_run_without_live_entry_renders_as_before() {
        // Обратная совместимость: Running-блок без live-записи (снимки,
        // экспорт) — ни таймера, ни хвоста, как раньше.
        let theme = Theme::default();
        let run = vec![tool("bash", ToolState::Running, ": make", "")];
        let text = plain(&tool_run_lines(
            &run,
            0,
            &std::collections::HashMap::new(),
            &theme,
        ));
        assert!(text.contains("◌ bash : make"), "{text}");
        assert!(!text.contains("· 0:0"), "{text}");
    }

    #[test]
    fn dialog_collapses_consecutive_tool_blocks() {
        let mut app = test_app();
        app.screen = Screen::Chat;
        for i in 0..8 {
            app.push_block(tool("bash", ToolState::Ok, &format!(": cmd{i}"), "ok"));
        }
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(text.contains("… +3 вызовов"), "схлопывание:\n{text}");
        assert!(text.contains("✓ bash : cmd0"), "{text}");
        assert!(text.contains("✓ bash : cmd7"), "{text}");
    }

    #[test]
    fn ask_modal_scrolls_to_follow_selection() {
        // Регрессия (баг 05.09): пикер /resume с 12 сессиями клипал список —
        // пункты ниже видимой области были недостижимы. Окно обязано ехать
        // за курсором: при выборе последнего пункта он виден.
        let mut app = test_app();
        app.screen = Screen::Chat;
        let options = (1..=12)
            .map(|i| crate::tool::AskOption {
                label: format!("session-{i:02}.jsonl"),
                description: format!("2026-09-05 19:0{i} · сообщений: {i}"),
            })
            .collect();
        app.ask = Some(crate::tui::app::AskState {
            question: "Сессия для восстановления:".into(),
            options,
            recommended: None,
            selected: 0,
            reply: None,
            kind: crate::tui::app::AskKind::SessionPicker,
        });
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            !text.contains("session-12.jsonl"),
            "хвост списка должен быть за окном:\n{text}"
        );
        assert!(text.contains("1/12"), "индикатор позиции:\n{text}");
        // Курсор на последний пункт — окно доезжает, пункт и индикатор видны.
        if let Some(ask) = app.ask.as_mut() {
            ask.selected = 11;
        }
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        assert!(
            text.contains("session-12.jsonl"),
            "выбранный последний пункт обязан быть виден:\n{text}"
        );
        assert!(text.contains("12/12"), "индикатор позиции:\n{text}");
    }

    /// Текст подсказки модалки: срез строки буфера между её левой рамкой и
    /// правой зоной со счётчиком. Нужен, чтобы проверять усечение подсказки
    /// по границам сегментов, а не по клеткам.
    fn ask_footer_hint(term: &Terminal<TestBackend>, counter: &str) -> String {
        let rows = buffer_rows(term);
        let cells: Vec<String> = counter.chars().map(|c| c.to_string()).collect();
        for row in &rows {
            let start = (0..row.len().saturating_sub(cells.len()))
                .find(|&i| row[i..i + cells.len()] == cells[..]);
            let Some(start) = start else { continue };
            let border = row[..start].iter().rposition(|c| c == "│" || c == "|");
            let Some(border) = border else { continue };
            return row[border + 2..start].concat().trim_end().to_string();
        }
        panic!("строка подсказки со счётчиком «{counter}» не найдена");
    }

    /// Открытая ask-модалка — верхний слой: статус-бар обязан показывать
    /// клавиши модалки, а не чата, даже когда чат занят и есть очередь.
    /// Раньше `hint_ctx()` отдавал `ChatBusy`, и под модалкой висели
    /// «Enter — в очередь», «Esc — прервать» — обещания, которые модалка
    /// перехватывала (Enter выбирает пункт, Esc отклоняет).
    #[test]
    fn open_modal_replaces_chat_keys_in_status_bar() {
        for unicode in [true, false] {
            for width in [60u16, 80, 100] {
                let mut app = if unicode { test_app() } else { ascii_app() };
                app.screen = Screen::Chat;
                testing::set_thinking(&mut app, true);
                app.queue.push_back("следующее сообщение".into());
                app.ask = Some(crate::tui::app::AskState {
                    question: "Выберите вариант".into(),
                    options: vec![
                        crate::tool::AskOption {
                            label: "Kafka".into(),
                            description: String::new(),
                        },
                        crate::tool::AskOption {
                            label: "NATS".into(),
                            description: String::new(),
                        },
                    ],
                    recommended: None,
                    selected: 0,
                    reply: None,
                    kind: crate::tui::app::AskKind::Tool,
                });
                let mut terminal = Terminal::new(TestBackend::new(width, 24)).expect("terminal");
                terminal.draw(|f| app.render(f)).expect("draw");
                assert!(
                    matches!(app.hint_ctx(), crate::tui::keymap::Ctx::Ask),
                    "контекст подсказок под модалкой не Ask (width={width}, \
                     unicode={unicode})"
                );
                let rows = buffer_rows(&terminal);
                let status = rows.last().expect("статус-бар").concat();
                for needle in ["Enter", "Esc", "подтвердить"] {
                    assert!(
                        status.contains(needle),
                        "статус-бар под модалкой без клавиши «{needle}» \
                         (width={width}, unicode={unicode}):\n{status}"
                    );
                }
                for banned in ["в очередь", "прервать", "очередь", "отправить"]
                {
                    assert!(
                        !status.contains(banned),
                        "статус-бар под модалкой обещает клавишу чата «{banned}» \
                         (width={width}, unicode={unicode}):\n{status}"
                    );
                }
                // Строка состояния занятого чата сообщает только факт «думает» —
                // без «Esc — прервать» и без «очередь: N».
                let state = rows
                    .iter()
                    .find(|r| r.concat().contains("думает"))
                    .expect("строка состояния занятого чата")
                    .concat();
                assert!(
                    !state.contains("прервать") && !state.contains("очередь:"),
                    "строка состояния под модалкой обещает прерывание/очередь:\n{state}"
                );
                // Нигде на экране не должно остаться обещания прервать ход.
                let text = buffer_text(&terminal);
                assert!(
                    !text.contains("прервать"),
                    "под модалкой остался текст «прервать» (width={width}, \
                     unicode={unicode}):\n{text}"
                );
            }
        }
    }

    /// Справка под модалкой описывает клавиши МОДАЛКИ, а не чата: `help_ctx`
    /// обязан подчиняться `hint_ctx`. Раньше `?` под модалкой показывал раздел
    /// чата («Enter — отправить», «Esc — прервать») — справка врала о том,
    /// куда уйдут клавиши.
    #[test]
    fn help_overlay_under_modal_describes_modal_keys() {
        let mut app = ask_app(vec![("Kafka", ""), ("NATS", "")]);
        app.help = true;
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).expect("terminal");
        terminal.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&terminal);
        for needle in [
            "Перемещение по вариантам",
            "Выбрать текущий вариант",
            "Быстрый выбор варианта по номеру",
            "Отказ от выбора",
        ] {
            assert!(
                text.contains(needle),
                "справка под модалкой не описывает клавишу модалки «{needle}»:\n{text}"
            );
        }
        for banned in ["Поставить набранное в очередь", "Прервать ход", "прервать"]
        {
            assert!(
                !text.contains(banned),
                "справка под модалкой описывает клавишу чата «{banned}»:\n{text}"
            );
        }
    }

    /// Подсказка модалки усекается по границам сегментов с многоточием, а не
    /// рвёт слово по клетке: при ширине 60 «Enter подтвердить» не смеет
    /// превратиться в «Ent». Проверяем 2/4/12 пунктов и переполнение
    /// (страничные клавиши) в обоих наборах глифов и на ширинах 60/80/100.
    #[test]
    fn ask_hint_truncates_on_segment_boundaries_not_mid_word() {
        for unicode in [true, false] {
            let up_down = if unicode { "↑/↓" } else { "Up/Down" };
            let dots = if unicode { "…" } else { "..." };
            for width in [60u16, 80, 100] {
                for (n, desc) in [(2usize, false), (4, false), (12, false), (12, true)] {
                    let mut app = if unicode { test_app() } else { ascii_app() };
                    app.screen = Screen::Chat;
                    app.ask = Some(crate::tui::app::AskState {
                        question: "Выберите вариант".into(),
                        options: (0..n)
                            .map(|i| crate::tool::AskOption {
                                label: format!("Вариант-{i}"),
                                description: if desc {
                                    "описание".into()
                                } else {
                                    String::new()
                                },
                            })
                            .collect(),
                        recommended: None,
                        selected: 0,
                        reply: None,
                        kind: crate::tui::app::AskKind::Tool,
                    });
                    let mut terminal =
                        Terminal::new(TestBackend::new(width, 24)).expect("terminal");
                    terminal.draw(|f| app.render(f)).expect("draw");
                    let hint = ask_footer_hint(&terminal, &format!("1/{n}"));

                    let body = hint
                        .strip_suffix(&format!(" · {dots}"))
                        .unwrap_or(hint.as_str());
                    let truncated = body.len() != hint.len();
                    let segs: Vec<&str> = if body.is_empty() {
                        Vec::new()
                    } else {
                        body.split(" · ").collect()
                    };

                    // Полный сегмент — единственная допустимая форма: разрыв
                    // внутри слова дал бы обрывок вроде «Ent».
                    let mut full: Vec<String> = vec![
                        format!("{up_down} выбор"),
                        "Enter подтвердить".into(),
                        "Esc решить".into(),
                    ];
                    if desc {
                        full.push("PgUp/PgDn/Home/End листать".into());
                    } else if n <= 9 {
                        full.push(format!("1-{n} быстро"));
                    } else {
                        full.push(format!("1-9 + {up_down} быстро"));
                    }
                    for seg in &segs {
                        assert!(
                            full.iter().any(|f| f == seg),
                            "сегмент «{seg}» — не целый (разрыв слова?) \
                             (width={width}, n={n}, desc={desc}, unicode={unicode}); \
                             подсказка: {hint:?}"
                        );
                    }
                    // Многоточие — только хвост усечения, не внутри текста.
                    assert!(
                        hint.matches(dots).count() <= 1,
                        "многоточие появилось в середине подсказки \
                         (width={width}, n={n}, unicode={unicode}): {hint:?}"
                    );

                    // Тест ровно про усечение на ширине 60: там не влезает
                    // ни один полный набор сегментов.
                    if width == 60 {
                        assert!(
                            truncated,
                            "на ширине 60 подсказка обязана усечься \
                             (n={n}, desc={desc}, unicode={unicode}): {hint:?}"
                        );
                    }
                    if truncated {
                        // Без Enter и Esc модалка непонятна — они остаются
                        // последними (приоритет удержания).
                        assert!(
                            segs.iter().any(|s| s.starts_with("Enter")),
                            "при усечении потерян Enter (width={width}, n={n}, \
                             unicode={unicode}): {hint:?}"
                        );
                        assert!(
                            segs.iter().any(|s| s.starts_with("Esc")),
                            "при усечении потерян Esc (width={width}, n={n}, \
                             unicode={unicode}): {hint:?}"
                        );
                        assert!(
                            !segs.is_empty(),
                            "усечение до пустого тела (width={width}, n={n}): {hint:?}"
                        );
                    } else {
                        assert_eq!(
                            segs.len(),
                            full.len(),
                            "без усечения обязаны быть все сегменты \
                             (width={width}, n={n}, desc={desc}, unicode={unicode}): {hint:?}"
                        );
                    }
                    // Страничные клавиши — когда список листается; на ширинах
                    // 80/100 для этого хватает места.
                    if desc && width >= 80 {
                        assert!(
                            segs.contains(&"PgUp/PgDn/Home/End листать"),
                            "переполнение: на ширине {width} подсказка обязана \
                             назвать листание (unicode={unicode}): {hint:?}"
                        );
                    }
                    if !desc && width >= 80 {
                        let digits = if n <= 9 {
                            format!("1-{n} быстро")
                        } else {
                            format!("1-9 + {up_down} быстро")
                        };
                        assert!(
                            segs.contains(&digits.as_str()),
                            "на ширине {width} обязан влезть быстрый диапазон \
                             «{digits}» (n={n}, unicode={unicode}): {hint:?}"
                        );
                    }
                }
            }
        }
    }

    /// 60×16 с 6 пунктами: рамка модалки не пересекает рамку панели диалога
    /// и не наезжает на строку ввода/статуса — при нехватке высоты список
    /// ужимается (с прокруткой), а не рисуется поверх чужого хрома.
    #[test]
    fn ask_modal_at_sixty_by_sixteen_does_not_cover_chrome() {
        for unicode in [true, false] {
            let mut app = if unicode { test_app() } else { ascii_app() };
            app.screen = Screen::Chat;
            app.ask = Some(crate::tui::app::AskState {
                question: "Выберите вариант".into(),
                options: (0..6)
                    .map(|i| crate::tool::AskOption {
                        label: format!("Вариант-{i}"),
                        description: "описание варианта".into(),
                    })
                    .collect(),
                recommended: None,
                selected: 0,
                reply: None,
                kind: crate::tui::app::AskKind::Tool,
            });
            let mut terminal = Terminal::new(TestBackend::new(60, 16)).expect("terminal");
            terminal.draw(|f| app.render(f)).expect("draw");
            let rows = buffer_rows(&terminal);
            let (top_corner, bottom_corner) = if unicode { ("╭", "╰") } else { ("+", "+") };
            let top_right = if unicode { "╮" } else { "+" };
            let cursor = if unicode { "›" } else { ">" };

            let dialog_top = rows
                .iter()
                .position(|r| r.concat().contains("Диалог"))
                .expect("шапка панели диалога");
            let dialog_bottom = (dialog_top + 1..rows.len())
                .find(|&y| rows[y][0] == bottom_corner)
                .expect("нижняя рамка панели диалога");
            let modal_top = rows
                .iter()
                .position(|r| r.concat().contains("решение за вами"))
                .expect("шапка модалки");
            let modal_left = rows[modal_top]
                .iter()
                .position(|c| c == top_corner)
                .expect("левый верхний угол модалки");
            let modal_bottom = (modal_top + 1..rows.len())
                .find(|&y| rows[y][modal_left] == bottom_corner)
                .expect("нижняя рамка модалки");
            let input_y = rows
                .iter()
                .position(|r| r[0] == cursor)
                .expect("строка ввода с курсором");

            assert!(
                modal_top > dialog_top,
                "рамка модалки наехала на шапку панели (unicode={unicode}): \
                 modal_top={modal_top}, dialog_top={dialog_top}"
            );
            assert!(
                modal_bottom < dialog_bottom,
                "рамка модалки пересекает нижнюю рамку панели \
                 (unicode={unicode}): modal_bottom={modal_bottom}, \
                 dialog_bottom={dialog_bottom}"
            );
            assert!(
                modal_bottom < input_y,
                "рамка модалки наехала на строку ввода (unicode={unicode}): \
                 modal_bottom={modal_bottom}, input_y={input_y}"
            );
            // Горизонталь и главное правило: под блокирующей модалкой правая
            // панель не рисуется вовсе, поэтому перечёркивать её рамку
            // нечему, а колонка диалога занимает всю ширину — рамка модалки
            // строго внутри рамки панели диалога. Это и есть проверяемая
            // инварианта: «пересечений рамок нет» при любой трактовке.
            let text = buffer_text(&terminal);
            assert!(
                !text.contains("Mermaid"),
                "под блокирующей модалкой правая панель осталась нарисованной \
                 и её рамка перечёркнута модалкой (unicode={unicode}):\n{text}"
            );
            let dialog_right = rows[0]
                .iter()
                .rposition(|c| *c == top_right)
                .expect("правый верхний угол панели диалога");
            let horizontal = if unicode { "─" } else { "-" };
            let mut modal_right = modal_left + 1;
            while modal_right + 1 < rows[modal_top].len()
                && rows[modal_top][modal_right] == horizontal
            {
                modal_right += 1;
            }
            assert!(
                modal_right < dialog_right,
                "рамка модалки вышла за рамку панели диалога \
                 (unicode={unicode}): правый край модалки={modal_right}, \
                 правый край панели={dialog_right}"
            );
            // Строка ввода читаема: курсор на месте и никаких рамок модалки.
            let input = rows[input_y].concat();
            assert!(
                input.starts_with(cursor),
                "строка ввода без курсора (unicode={unicode}): {input:?}"
            );
            for glyph in ["│", "|", "╭", "╰", "─"] {
                assert!(
                    !input.contains(glyph),
                    "строка ввода залеплена рамкой «{glyph}» (unicode={unicode}): {input:?}"
                );
            }
            // Список ужат: последний пункт за окном, а не нарисован поверх.
            let text = buffer_text(&terminal);
            assert!(
                text.contains("Вариант-0"),
                "первый пункт обязан быть виден (unicode={unicode}):\n{text}"
            );
            assert!(
                !text.contains("Вариант-5"),
                "шестой пункт обязан уйти за окно прокрутки, а не рисоваться \
                 поверх хрома (unicode={unicode}):\n{text}"
            );
            assert!(
                app.ask_viewport > 0 && app.ask_viewport < 6 * 3,
                "модалка обязана ужать список под высоту диалога \
                 (unicode={unicode}): ask_viewport={}",
                app.ask_viewport
            );
        }
    }
}
