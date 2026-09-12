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
use ratatui::widgets::block::Title;
use ratatui::widgets::{
    Block, Borders, Clear, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState, Wrap,
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::assets;

use super::app::{App, ChatBlock, Panels, RightTab, Screen, ToastLevel, ToolLive, ToolState};
use super::text::{markdown_lines, wrap_line};
use super::theme::Theme;

/// Ширина правой колонки (вкладки).
const RIGHT_WIDTH: u16 = 42;
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
fn right_panel_width(app: &App, term_w: u16) -> u16 {
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
    let right_w = if app.right_visible {
        right_panel_width(app, f.area().width)
    } else {
        0
    };
    let cols =
        Layout::horizontal([Constraint::Min(24), Constraint::Length(right_w)]).split(rows[0]);

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
    if app.ask.is_some() {
        draw_ask(f, f.area(), app, &theme);
    }
}

/// Оверлей справки (`?`): клавиши текущего экрана, сгруппированные по задаче
/// (реестр `keymap` — единый источник со строкой подсказок). Фон под окном
/// не затемняем: справка не блокирует сценарий, а подсказывает.
fn draw_help(f: &mut Frame, area: Rect, app: &mut App, theme: &Theme) {
    let sections = super::keymap::sections(app.chat_ctx());
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

/// Модальная панель выбора вариантов (инструмент `propose_options)`:
/// центрированное окно поверх чата — вопрос, варианты, курсор, подсказки.
fn draw_ask(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
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

    let (title, esc_hint) = match ask.kind {
        crate::tui::app::AskKind::Tool => (
            format!(" {} решение за вами ", theme.glyphs.context()),
            " решить агенту",
        ),
        crate::tui::app::AskKind::ModelPicker => (
            format!(" {} выбор модели ", theme.glyphs.context()),
            " отмена",
        ),
        crate::tui::app::AskKind::SessionPicker => (
            format!(" {} выбор сессии ", theme.glyphs.context()),
            " отмена",
        ),
    };
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
    for (i, opt) in ask.options.iter().enumerate() {
        let current = i == ask.selected;
        if current {
            sel_start = opt_lines.len();
        }
        let recommended = ask.recommended.as_deref() == Some(opt.label.as_str());
        // Маркер выбора — U+203A, а не U+276F «❯»: последнего нет в
        // Ubuntu Sans Mono, и fontconfig-фолбэк рисовал глиф вне сетки,
        // затирая следующие ячейки (инцидент 07.09: съедалась цифра «1.»
        // первого пункта ask-модалки). U+203A есть в шрифте — без фолбэка.
        let (mark, num_style) = if current {
            (
                format!("{} ", theme.glyphs.cursor()),
                Style::default()
                    .fg(theme.cyan)
                    .bg(theme.bg)
                    .add_modifier(Modifier::BOLD),
            )
        } else {
            ("  ".to_string(), theme.muted())
        };
        let star = if recommended {
            format!(" {}", theme.glyphs.star())
        } else {
            String::new()
        };
        opt_lines.push(Line::from(vec![
            Span::styled(mark, num_style),
            Span::styled(format!("{}. ", i + 1), num_style),
            Span::styled(opt.label.clone(), num_style),
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
    let key = |k: &str| Span::styled(k.to_string(), theme.heading());
    let sep = |t: String| Span::styled(t, theme.muted());
    let hint_line = Line::from(vec![
        key(theme.glyphs.up_down()),
        sep(" выбор · ".into()),
        key("Enter"),
        sep(" подтвердить · ".into()),
        key("1-4"),
        sep(" быстро · ".into()),
        key("Esc"),
        sep(esc_hint.into()),
        sep(format!(" · {}/{}", ask.selected + 1, ask.options.len())),
    ]);
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
    let body = Rect {
        y: inner.y + header.height,
        height: inner.height.saturating_sub(header.height).saturating_sub(1),
        ..inner
    };
    let mut head_lines = question_lines;
    head_lines.push(Line::default());
    f.render_widget(Paragraph::new(head_lines), header);
    f.render_widget(Paragraph::new(vec![hint_line]), footer);
    // Окно прокрутки за курсором (минимальное): вниз — ровно до показа
    // описания выбранного, вверх — пункт в топ.
    let visible = usize::from(body.height);
    let total = opt_lines.len();
    let mut offset = 0usize;
    if visible > 0 && total > visible {
        offset = sel_start.min(total - visible);
        if sel_end > offset + visible {
            offset = sel_end - visible;
        }
    }
    f.render_widget(
        Paragraph::new(opt_lines).scroll((u16::try_from(offset).unwrap_or(u16::MAX), 0)),
        body,
    );
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
        lines.push(Line::from(Span::styled(
            "Пусто. Напишите сообщение — или ? — список клавиш, /help — команды.",
            theme.muted(),
        )));
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
        block.title(Title::from(Span::styled(pos, theme.muted())).alignment(Alignment::Right))
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
        if matches!(state, ToolState::Running)
            && let Some(l) = item_live
            && !l.tail.is_empty()
        {
            let tail: Vec<&str> = l.tail.lines().collect();
            for t in &tail[tail.len().saturating_sub(TOOL_TAIL_LINES)..] {
                out.push(Line::from(Span::styled(
                    format!("  {}", truncate_chars(t.trim_end(), TOOL_TAIL_LINE_CHARS)),
                    theme.muted().add_modifier(Modifier::ITALIC),
                )));
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

/// Правая колонка: вкладки Mermaid / Рубрика / Знания.
/// Mermaid — без переносов (арт клипается), остальные — с переносом.
fn draw_right(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    let inner_w = usize::from(area.width.saturating_sub(2)).max(1);
    // Арт шире даже расширенной панели (кап 60%)? Подскажем путь: F4.
    let mermaid_clipped = app
        .panels
        .mermaid
        .lines()
        .map(UnicodeWidthStr::width)
        .max()
        .unwrap_or(0)
        > inner_w;
    let mut title_spans = Vec::new();
    let tab_style = |tab: &RightTab, theme: &Theme| {
        if *tab == app.right_tab() {
            Style::default()
                .fg(theme.bg)
                .bg(theme.cyan)
                .add_modifier(Modifier::BOLD)
        } else {
            theme.muted()
        }
    };
    for (i, tab) in RightTab::ALL.iter().enumerate() {
        let icon = theme.glyphs.tab_icons()[i];
        // Подписи вкладок — всегда короткие: длинный хинт в заголовке
        // обрезал соседние вкладки у правого края (кейс 2026-09-02).
        title_spans.push(Span::styled(
            format!(" {icon} {} ", tab.title()),
            tab_style(tab, theme),
        ));
    }
    if spans_width(&title_spans) > inner_w {
        // Не влезает полный бар (4 вкладки × подписи): деградация ступенями —
        // сначала активная с подписью + иконки остальных (ориентир виден),
        // иначе — только иконки (все 4 вкладки всё равно видны; раньше
        // «Флот» просто срезался у правого края панели).
        let active = app.right_tab();
        let mut compact: Vec<Span> = RightTab::ALL
            .iter()
            .enumerate()
            .map(|(i, tab)| {
                if *tab == active {
                    Span::styled(
                        format!(" {} {} ", theme.glyphs.tab_icons()[i], tab.title()),
                        tab_style(tab, theme),
                    )
                } else {
                    Span::styled(
                        format!(" {} ", theme.glyphs.tab_icons()[i]),
                        tab_style(tab, theme),
                    )
                }
            })
            .collect();
        title_spans = compact;
    }
    let mut block = Block::default()
        .borders(Borders::ALL)
        .border_set(theme.glyphs.border_set())
        .border_style(theme.border())
        .title(Line::from(title_spans));
    if mermaid_clipped {
        // Хинт F4 — в НИЖНИЙ заголовок (справа), а не в бар вкладок.
        block = block.title_bottom(Line::from(Span::styled(
            " F4 — вся схема ".to_string(),
            theme.muted(),
        )));
    }

    let tab = app.right_tab();
    let content = app.panels.content(tab);
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
    f.render_widget(Paragraph::new(lines).block(block), area);
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
        lines.push(Line::from(Span::styled(
            format!("{marker} {}. {text}{fold}", i + 1),
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
        let label = match app.running_tool() {
            Some((name, action, secs)) => {
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
            }
            None => {
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
            }
        };
        let mut spans = vec![
            Span::styled(
                label,
                Style::default()
                    .fg(theme.purple)
                    .bg(theme.bg)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled("  ·  ", theme.muted()),
        ];
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
    }
}

/// Статус-бар: бейдж модели | индикатор контекста | cwd | заметки | подсказки.
fn draw_status(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    let mut spans = vec![Span::styled(format!(" {} ", app.model_name), theme.badge())];
    // Индикатор заполнения контекста — сразу после бейджа модели.
    spans.extend(context_spans(app, theme));
    // Фоновые субагенты/ralph-циклы: имена (видимость запуска), не только счётчик.
    let running = app.subagents_running();
    if running > 0 {
        let names = app.running_subagent_names();
        let label = if names.is_empty() {
            format!("субагенты: {running}")
        } else if names.len() <= 3 {
            format!("субагенты: {}", names.join(", "))
        } else {
            format!(
                "субагенты: {} (+{})",
                names[..3].join(", "),
                names.len() - 3
            )
        };
        spans.push(Span::styled(
            format!(
                "  · {} {label} ",
                theme.glyphs.spinner()[app.anim_frame(theme.glyphs.spinner().len())]
            ),
            Style::default().fg(theme.green).bg(theme.bg),
        ));
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

    // Порядок важности в узком окне: подсказки клавиш (обнаруживаемость,
    // E6) → индикаторы модели/контекста → заметка (урезается) → cwd.
    // Поэтому подсказкам отдаётся до половины строки, а cwd добавляется
    // последним и уходит первым: «где я» пользователь знает из оболочки,
    // а «что нажать» — только отсюда.
    let width = usize::from(area.width);
    let hint_spans = super::keymap::hints(app.hint_ctx(), width / 2, theme);
    let hint_w = spans_width(&hint_spans);
    let left_budget = width.saturating_sub(hint_w + 2);

    if let Some(extra) = app.status_extra() {
        // Заметка уступает подсказкам (они важнее, E6): что не влезло в
        // left_budget, урезаем с `…`, а не рвём на полуслове у границы
        // подсказок; совсем узко — заметка пропускается целиком.
        let style = Style::default().fg(theme.orange).bg(theme.bg);
        let used = spans_width(&spans);
        let tail = format!("  · {extra}");
        if used + UnicodeWidthStr::width(tail.as_str()) <= left_budget {
            spans.push(Span::styled(tail, style));
        } else {
            let room = left_budget.saturating_sub(used + 4); // «  · » + «…»
            if room >= 8 {
                spans.push(Span::styled(
                    format!("  · {}…", clip_display_width(&extra, room)),
                    style,
                ));
            }
        }
    }

    let cwd = Span::styled(
        format!("  {}", shorten_path(&app.tool_ctx.cwd)),
        theme.muted(),
    );
    if spans_width(&spans) + UnicodeWidthStr::width(cwd.content.as_ref()) <= left_budget {
        spans.push(cwd);
    }

    let cols =
        Layout::horizontal([Constraint::Min(0), Constraint::Length(u16_sat(hint_w))]).split(area);

    f.render_widget(Paragraph::new(Line::from(spans)), cols[0]);
    f.render_widget(Paragraph::new(Line::from(hint_spans)), cols[1]);
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
        assert!(
            text.contains("? — список клавиш"),
            "нет пути к справке:\n{text}"
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
            "Mermaid",
            "Рубрика",
            "Знания",
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
    fn narrow_panel_degrades_tab_strip_to_icons_with_all_tabs() {
        use crate::tui::app::RightTab;
        let mut app = test_app();
        app.screen = Screen::Chat;
        app.right_tab = RightTab::Fleet;
        // Широкий терминал: полный бар — все 4 вкладки текстом (панель 42,
        // бар из 40 ячеек влезает ровно).
        let mut wide = Terminal::new(TestBackend::new(150, 24)).expect("term");
        wide.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&wide);
        for t in ["Mermaid", "Рубрика", "Знания", "Флот"] {
            assert!(text.contains(t), "нет вкладки {t}:\n{text}");
        }
        // 64 колонки (диалог сжат до минимума): полный бар не влезает —
        // деградация: активная с подписью + иконки остальных; все 4 видны.
        let mut term = Terminal::new(TestBackend::new(64, 24)).expect("term");
        term.draw(|f| app.render(f)).expect("draw");
        let text = buffer_text(&term);
        for icon in ["◇", "✓", "◈", "▶"] {
            assert!(text.contains(icon), "нет иконки {icon}:\n{text}");
        }
        assert!(
            text.contains("Флот"),
            "активная вкладка с подписью в компактном баре:\n{text}"
        );
        assert!(
            !text.contains("Знания"),
            "неактивные вкладки — только иконками:\n{text}"
        );
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
        let _tmp = tempfile::tempdir().expect("tmp");
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
        assert!(
            text.contains("субагенты: general"),
            "индикатор в статус-баре показывает имена:\n{text}"
        );
        assert!(app.needs_tick(), "тики идут, пока крутятся субагенты");
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
            text.contains("▶ 1. первое в очереди"),
            "первое — следующее на запуск:\n{text}"
        );
        // Многострочное сообщение — первой строкой с маркером ↵.
        assert!(
            text.contains("• 2. второе ↵"),
            "вторая строка с маркером переноса:\n{text}"
        );
        // Карточка очереди — В окне логов: выше поля ввода (строки с «›»).
        let q_row = text.lines().position(|l| l.contains("▶ 1.")).expect("▶");
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
        // Кнопка — в правом нижнем углу диалога (диалог 58 колонок при 100).
        let btn = app.jump_btn.expect("кнопка");
        assert_eq!(btn.width, 3);
        assert!(btn.x >= 50 && btn.y >= 20, "позиция кнопки: {btn:?}");
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
        // (выбран, с маркером «›») номер «1.» обязан оставаться в буфере —
        // инвариант рендера опций при усечении длинной строки.
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
}
