//! Текстовые утилиты TUI: markdown-lite, перенос строк по unicode-ширине,
//! кандидаты автодополнения слэш-команд.

use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::agent::slash;

use super::theme::Theme;

/// Разбирает текст в markdown-lite линии: заголовки `#`..`####`, код-блоки
/// `` ``` ``, буллеты `- `/`* `, инлайн `**жирный**`, таблицы `|…|`.
/// `width` — целевая ширина строки: таблицы, не влезающие в неё,
/// переносятся внутри ячеек (см. `table_lines`).
pub(crate) fn markdown_lines(text: &str, theme: &Theme, width: usize) -> Vec<Line<'static>> {
    let base = theme.base();
    let mut out = Vec::new();
    let mut in_code = false;
    let raw_lines: Vec<&str> = text.lines().collect();
    let mut i = 0;
    while i < raw_lines.len() {
        let raw = raw_lines[i];
        let line = raw.strip_suffix('\r').unwrap_or(raw);
        if line.trim_start().starts_with("```") {
            in_code = !in_code;
            i += 1;
            continue;
        }
        if in_code {
            out.push(Line::from(Span::styled(format!(" {line}"), theme.code())));
            i += 1;
            continue;
        }
        let trimmed = line.trim_start();
        if trimmed.starts_with('|') && trimmed.ends_with('|') {
            // Таблица целиком: собираем ряды (разделители пропускаем) и
            // рендерим с учётом ширины — иначе длинные строки клипались бы
            // детектором арта (│ — box-drawing) и обрезались справа.
            let mut rows: Vec<Vec<String>> = Vec::new();
            while i < raw_lines.len() {
                let t = raw_lines[i]
                    .strip_suffix('\r')
                    .unwrap_or(raw_lines[i])
                    .trim_start();
                if !t.starts_with('|') || !t.ends_with('|') {
                    break;
                }
                if !is_table_separator(t) {
                    rows.push(
                        t.trim_matches('|')
                            .split('|')
                            .map(|c| c.trim().to_string())
                            .collect(),
                    );
                }
                i += 1;
            }
            // Воздух вокруг таблицы: отделяет её от прозы, легче читается.
            if out.last().is_some_and(|l| !line_is_blank(l)) {
                out.push(Line::default());
            }
            out.extend(table_lines(&rows, theme, width));
            out.push(Line::default());
            continue;
        }
        let indent = &line[..line.len() - trimmed.len()];
        if let Some(heading) = strip_heading(trimmed) {
            out.push(Line::from(Span::styled(
                heading.to_string(),
                theme.heading(),
            )));
        } else if let Some(rest) = trimmed
            .strip_prefix("- ")
            .or_else(|| trimmed.strip_prefix("* "))
        {
            let mut spans = vec![Span::styled(format!("{indent}• "), theme.purple())];
            spans.extend(inline_spans(rest, base));
            out.push(Line::from(spans));
        } else if let Some(rest) = trimmed.strip_prefix("> ") {
            // Цитата: фиолетовый гуттер + приглушённый курсив.
            let mut spans = vec![Span::styled(format!("{indent}▌ "), theme.purple())];
            spans.extend(inline_spans(
                rest,
                theme.muted().add_modifier(Modifier::ITALIC),
            ));
            out.push(Line::from(spans));
        } else {
            out.push(Line::from(inline_spans(line, base)));
        }
        i += 1;
    }
    out
}

/// Разделитель ячеек таблицы (тонкий, приглушённый при отрисовке).
const TABLE_SEP: &str = " │ ";

/// Минимальная ширина колонки при сжатии таблицы.
const MIN_TABLE_COL: usize = 8;

/// Строка без видимого текста?
fn line_is_blank(line: &Line<'_>) -> bool {
    line.spans
        .iter()
        .all(|s| s.content.as_ref().trim().is_empty())
}

/// Plain-текст линии (конкатенация спанов).
fn line_to_plain(line: &Line<'_>) -> String {
    line.spans.iter().map(|s| s.content.as_ref()).collect()
}

/// Снимает markdown-маркеры `**` и `*` (для измерения и сжатых ячеек).
fn strip_md(text: &str) -> String {
    text.replace(['*'], "")
}

/// Рендерит таблицу. Строки, влезающие в `width`, — одной линией
/// (разделители приглушены, заголовок жирный). Слишком широкая таблица
/// сжимается: колонки уменьшаются пропорционально (не ниже `MIN_TABLE_COL`),
/// текст внутри ячеек переносится по словам — так ничего не обрезается
/// справа и колонки остаются выровненными.
fn table_lines(rows: &[Vec<String>], theme: &Theme, width: usize) -> Vec<Line<'static>> {
    let base = theme.base();
    let n = rows.iter().map(Vec::len).max().unwrap_or(0);
    if n == 0 {
        return Vec::new();
    }
    // Нормализуем ряды до одинакового числа колонок.
    let rows: Vec<Vec<String>> = rows
        .iter()
        .map(|r| {
            let mut r = r.clone();
            r.resize(n, String::new());
            r
        })
        .collect();
    let sep_w = UnicodeWidthStr::width(TABLE_SEP) * (n - 1);
    // Естественные ширины колонок — по самой длинной ячейке (без `**`).
    let natural: Vec<usize> = (0..n)
        .map(|c| {
            rows.iter()
                .map(|r| UnicodeWidthStr::width(strip_md(&r[c]).as_str()))
                .max()
                .unwrap_or(0)
                .max(1)
        })
        .collect();
    let fits = natural.iter().sum::<usize>() + sep_w <= width;
    let cols = if fits {
        natural.clone()
    } else {
        shrink_columns(&natural, width.saturating_sub(sep_w))
    };
    let mut out = Vec::new();
    for (ri, row) in rows.iter().enumerate() {
        let header = ri == 0;
        let cell_style = |base: Style| {
            if header {
                base.add_modifier(Modifier::BOLD)
            } else {
                base
            }
        };
        if fits {
            let mut spans = Vec::new();
            for (ci, cell) in row.iter().enumerate() {
                if ci > 0 {
                    spans.push(Span::styled(TABLE_SEP.to_string(), theme.muted()));
                }
                spans.extend(inline_spans(cell, cell_style(base)));
            }
            out.push(Line::from(spans));
        } else {
            // Перенос внутри ячеек. Markdown-маркеры снимаем: разрыв строки
            // мог бы попасть внутрь `**…**` и сломать инлайн-парсер.
            let wrapped: Vec<Vec<String>> = row
                .iter()
                .enumerate()
                .map(|(ci, cell)| {
                    wrap_line(&Line::from(strip_md(cell)), cols[ci])
                        .iter()
                        .map(|l| line_to_plain(l).trim_end().to_string())
                        .collect()
                })
                .collect();
            let h = wrapped.iter().map(Vec::len).max().unwrap_or(1);
            for vi in 0..h {
                let mut spans = Vec::new();
                for (ci, cell_lines) in wrapped.iter().enumerate() {
                    if ci > 0 {
                        spans.push(Span::styled(TABLE_SEP.to_string(), theme.muted()));
                    }
                    let text = cell_lines.get(vi).cloned().unwrap_or_default();
                    let pad = cols[ci].saturating_sub(UnicodeWidthStr::width(text.as_str()));
                    spans.push(Span::styled(
                        format!("{text}{}", " ".repeat(pad)),
                        cell_style(base),
                    ));
                }
                out.push(Line::from(spans));
            }
        }
    }
    out
}

/// Сжимает колонки до суммарного бюджета `budget` методом water-filling:
/// самые широкие режутся до потолка, узкие сохраняют естественную ширину
/// (так короткая колонка имён не расплющивается, пока длинные «дышат»).
/// Пол — `MIN_TABLE_COL`; если и минималки не влезают (очень узкий
/// терминал) — равные доли бюджета.
fn shrink_columns(natural: &[usize], budget: usize) -> Vec<usize> {
    let mut cols = natural.to_vec();
    while cols.iter().sum::<usize>() > budget {
        let Some(max_w) = cols.iter().max().copied() else {
            break;
        };
        if max_w <= MIN_TABLE_COL {
            break;
        }
        // Потолок — следующая по ширине колонка (не ниже пола); режем
        // самые широкие на недостачу, распределённую между ними.
        let second = cols
            .iter()
            .filter(|w| **w < max_w)
            .max()
            .copied()
            .unwrap_or(MIN_TABLE_COL)
            .max(MIN_TABLE_COL);
        let at_max = cols.iter().filter(|w| **w == max_w).count();
        let excess = cols.iter().sum::<usize>() - budget;
        let step = (excess / at_max).max(1);
        let target = max_w.saturating_sub(step).max(second);
        for w in &mut cols {
            if *w == max_w {
                *w = target;
            }
        }
    }
    if cols.iter().sum::<usize>() > budget {
        cols = vec![(budget / cols.len()).max(2); cols.len()];
    }
    cols
}

/// Строка-разделитель markdown-таблицы (`|---|---|`, `|:-|:-:|`)?
fn is_table_separator(line: &str) -> bool {
    line.chars().all(|c| matches!(c, '|' | '-' | ':' | ' ')) && line.contains('-')
}

/// Срезает маркер заголовка `# `…`#### `, возвращает текст заголовка.
fn strip_heading(line: &str) -> Option<&str> {
    let hashes = line.chars().take_while(|c| *c == '#').count();
    if (1..=4).contains(&hashes) && line[hashes..].starts_with(' ') {
        Some(line[hashes + 1..].trim_start())
    } else {
        None
    }
}

/// Инлайн-разбор `**жирный**` и `*курсив*` (простой парсер: чередование
/// по маркерам; сначала режем по `**`, внутри сегментов — по `*`).
fn inline_spans(text: &str, base: Style) -> Vec<Span<'static>> {
    let mut spans = Vec::new();
    let mut rest = text;
    let mut is_bold = false;
    while let Some(pos) = rest.find("**") {
        if pos > 0 {
            push_italic_spans(&mut spans, &rest[..pos], base, is_bold);
        }
        is_bold = !is_bold;
        rest = &rest[pos + 2..];
    }
    push_italic_spans(&mut spans, rest, base, is_bold);
    if spans.is_empty() {
        spans.push(Span::styled(String::new(), base));
    }
    spans
}

/// Добавляет спаны сегмента, разбирая одиночные `*курсив*`.
fn push_italic_spans(spans: &mut Vec<Span<'static>>, text: &str, base: Style, is_bold: bool) {
    let style = |italic: bool| {
        let mut s = base;
        if is_bold {
            s = s.add_modifier(Modifier::BOLD);
        }
        if italic {
            s = s.add_modifier(Modifier::ITALIC);
        }
        s
    };
    let mut rest = text;
    let mut italic = false;
    while let Some(pos) = rest.find('*') {
        if pos > 0 {
            spans.push(Span::styled(rest[..pos].to_string(), style(italic)));
        }
        italic = !italic;
        rest = &rest[pos + 1..];
    }
    if !rest.is_empty() {
        spans.push(Span::styled(rest.to_string(), style(italic)));
    }
}

/// Переносит стилизованную линию по ширине с разрывом на границах слов
/// (ширины — по unicode-width; широкие символы CJK/эмодзи учитываются).
/// Слово длиннее строки разрывается жёстко, по символам.
pub(crate) fn wrap_line(line: &Line<'static>, width: usize) -> Vec<Line<'static>> {
    let mut w = Wrapper {
        lines: vec![Vec::new()],
        width: width.max(1),
        cur: 0,
    };
    for span in &line.spans {
        for piece in span.content.split_inclusive(' ') {
            w.push_piece(piece, span.style);
        }
    }
    w.lines.into_iter().map(Line::from).collect()
}

/// Накопитель перенесённых строк.
struct Wrapper {
    /// Готовые строки (последняя — накапливаемая).
    lines: Vec<Vec<Span<'static>>>,
    /// Целевая ширина строки.
    width: usize,
    /// Текущая ширина последней строки.
    cur: usize,
}

impl Wrapper {
    /// Начинает новую строку.
    fn new_line(&mut self) {
        self.lines.push(Vec::new());
        self.cur = 0;
    }

    /// Добавляет кусок текста в текущую строку.
    fn push_span(&mut self, text: String, style: Style) {
        if text.is_empty() {
            return;
        }
        self.cur += UnicodeWidthStr::width(text.as_str());
        if let Some(last) = self.lines.last_mut() {
            last.push(Span::styled(text, style));
        }
    }

    /// Добавляет слово (с возможным хвостовым пробелом), перенося при нехватке места.
    fn push_piece(&mut self, piece: &str, style: Style) {
        let pw = UnicodeWidthStr::width(piece);
        if pw == 0 {
            return;
        }
        if self.cur > 0 && self.cur + pw > self.width {
            self.new_line();
            // Ведущий пробел новой строки не нужен.
            if piece.trim().is_empty() {
                return;
            }
        }
        if pw <= self.width {
            self.push_span(piece.to_string(), style);
        } else {
            self.push_long(piece, style);
        }
    }

    /// Жёсткий разрыв слова длиннее строки (по символам с учётом ширины).
    fn push_long(&mut self, piece: &str, style: Style) {
        debug_assert_eq!(self.cur, 0, "длинное слово начинается с новой строки");
        let mut buf = String::new();
        let mut bw = 0usize;
        for ch in piece.chars() {
            let cw = UnicodeWidthChar::width(ch).unwrap_or(0);
            if bw + cw > self.width && !buf.is_empty() {
                self.push_span(std::mem::take(&mut buf), style);
                self.new_line();
                bw = 0;
            }
            buf.push(ch);
            bw += cw;
        }
        self.push_span(buf, style);
    }
}

/// Кандидаты автодополнения слэш-команды по префиксу (`/me` → `/mermaid`).
/// Дополняется только первое слово ввода; не-слэш ввод не дополняется.
pub(crate) fn completion_candidates(input: &str) -> Vec<&'static str> {
    if !input.starts_with('/') || input.chars().any(char::is_whitespace) {
        return Vec::new();
    }
    slash::catalog()
        .into_iter()
        .filter_map(|(usage, _desc)| usage.split_whitespace().next())
        .filter(|cmd| cmd.starts_with(input) && *cmd != input)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::style::Color;

    /// Собирает текст линии из спанов (для сравнений).
    fn line_text(line: &Line<'_>) -> String {
        line.spans.iter().map(|s| s.content.as_ref()).collect()
    }

    #[test]
    fn wrap_breaks_at_word_boundaries() {
        let line = Line::from("привет мир это тест");
        let wrapped = wrap_line(&line, 11);
        let texts: Vec<String> = wrapped
            .iter()
            .map(|l| line_text(l).trim_end().to_string())
            .collect();
        assert_eq!(texts, vec!["привет мир", "это тест"]);
    }

    #[test]
    fn wrap_hard_splits_long_words() {
        let line = Line::from("абвгдежзикл");
        let wrapped = wrap_line(&line, 4);
        let texts: Vec<String> = wrapped.iter().map(line_text).collect();
        assert_eq!(texts, vec!["абвг", "дежз", "икл"]);
    }

    #[test]
    fn wrap_respects_wide_chars() {
        // Каждый иероглиф — ширина 2: «日本語» = 6 колонок.
        let line = Line::from("日本語");
        let wrapped = wrap_line(&line, 4);
        let texts: Vec<String> = wrapped.iter().map(line_text).collect();
        assert_eq!(texts, vec!["日本", "語"]);
    }

    #[test]
    fn wrap_preserves_span_styles() {
        let line = Line::from(Span::styled("раз два", Style::default().fg(Color::Red)));
        let wrapped = wrap_line(&line, 4);
        assert_eq!(wrapped.len(), 2);
        assert!(
            wrapped
                .iter()
                .all(|l| l.spans.iter().all(|s| s.style.fg == Some(Color::Red)))
        );
    }

    #[test]
    fn wrap_empty_line_stays_single() {
        let wrapped = wrap_line(&Line::from(""), 10);
        assert_eq!(wrapped.len(), 1);
    }

    #[test]
    fn markdown_heading_is_bold_cyan() {
        let theme = Theme::default();
        let lines = markdown_lines("# Заголовок\nтекст", &theme, 80);
        assert_eq!(lines.len(), 2);
        let head = &lines[0].spans[0];
        assert_eq!(head.content.as_ref(), "Заголовок");
        assert_eq!(head.style.fg, Some(theme.cyan));
        assert!(head.style.add_modifier.contains(Modifier::BOLD));
    }

    #[test]
    fn markdown_code_block_is_soft_panel() {
        let theme = Theme::default();
        let lines = markdown_lines("до\n```\nlet x = 1;\n```\nпосле", &theme, 80);
        assert_eq!(lines.len(), 3);
        let code = &lines[1].spans[0];
        // Мягкая панель Tokyo Night, не инверсия fg/bg.
        assert_eq!(code.style.bg, Some(Color::Rgb(0x24, 0x28, 0x3b)));
        assert_eq!(code.style.fg, Some(Color::Rgb(0xa9, 0xb1, 0xd6)));
        assert_eq!(line_text(&lines[0]), "до");
        assert_eq!(line_text(&lines[2]), "после");
    }

    #[test]
    fn markdown_table_renders_cells_and_skips_separator() {
        let lines = markdown_lines(
            "| Паттерн | Когда |\n|---|---|\n| сага | распределённая транзакция |",
            &Theme::default(),
            80,
        );
        // Разделитель пропущен, после таблицы — пустая строка-отбивка.
        assert_eq!(lines.len(), 3, "{lines:?}");
        assert_eq!(line_text(&lines[0]), "Паттерн │ Когда");
        assert_eq!(line_text(&lines[1]), "сага │ распределённая транзакция");
        assert!(
            lines[0]
                .spans
                .iter()
                .all(|s| s.style.add_modifier.contains(Modifier::BOLD)
                    || s.content.as_ref() == TABLE_SEP)
        );
        assert!(line_is_blank(&lines[2]));
    }

    #[test]
    fn markdown_table_is_padded_off_prose() {
        let lines = markdown_lines(
            "текст до\n| a | b |\n|---|---|\n| 1 | 2 |\nтекст после",
            &Theme::default(),
            80,
        );
        let texts: Vec<String> = lines.iter().map(line_text).collect();
        assert_eq!(
            texts,
            vec!["текст до", "", "a │ b", "1 │ 2", "", "текст после"]
        );
    }

    #[test]
    fn wide_table_wraps_inside_cells_not_off_screen() {
        let theme = Theme::default();
        let md = "| Термин | Определение |\n|---|---|\n\
                  | Инвариант | Что не имеет права разойтись между независимыми исполнителями |";
        let lines = markdown_lines(md, &theme, 40);
        // Ничего не вылезает за ширину: раньше такие строки клипались справа.
        for l in &lines {
            let w = UnicodeWidthStr::width(line_text(l).as_str());
            assert!(w <= 40, "строка шире 40 ({w}): {:?}", line_text(l));
        }
        // Перенос произошёл: данных больше, чем «заголовок + ряд + отбивка».
        assert!(lines.len() > 3, "{lines:?}");
        // Текст ячеек не потерян: и термин, и хвост определения на экране
        // («Инвариант» шире сжатой колонки — разорвано жёстко на «Инвариан»+«т»).
        let all: String = lines.iter().map(line_text).collect();
        assert!(all.contains("Инвариан"), "{all}");
        assert!(all.contains("исполнителями"), "{all}");
    }

    #[test]
    fn shrink_columns_hits_budget() {
        let cols = shrink_columns(&[30, 60, 10], 40);
        assert!(cols.iter().sum::<usize>() <= 40, "{cols:?}");
        assert!(cols.iter().all(|w| *w >= 2), "{cols:?}");
        // Узкий терминал: равные доли.
        let cols = shrink_columns(&[30, 60, 10], 9);
        assert_eq!(cols, vec![3, 3, 3]);
    }

    #[test]
    fn markdown_quote_gets_gutter() {
        let lines = markdown_lines("> важная мысль", &Theme::default(), 80);
        let text = line_text(&lines[0]);
        assert!(text.starts_with("▌ "), "{text}");
        assert!(text.contains("важная мысль"));
    }

    #[test]
    fn markdown_bullet_becomes_dot() {
        let lines = markdown_lines("- пункт списка\n* ещё пункт", &Theme::default(), 80);
        assert!(line_text(&lines[0]).starts_with("• "));
        assert!(line_text(&lines[1]).starts_with("• "));
    }

    #[test]
    fn markdown_bold_inline_splits_spans() {
        let lines = markdown_lines("это **жирный** текст", &Theme::default(), 80);
        let spans = &lines[0].spans;
        assert_eq!(spans.len(), 3);
        assert!(!spans[0].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(spans[1].content.as_ref(), "жирный");
        assert!(spans[1].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(spans[2].content.as_ref(), " текст");
        assert!(!spans[2].style.add_modifier.contains(Modifier::BOLD));
    }

    #[test]
    fn markdown_italic_inline_drops_single_stars() {
        let lines = markdown_lines("это *курсивный* текст", &Theme::default(), 80);
        let spans = &lines[0].spans;
        assert_eq!(spans.len(), 3);
        assert_eq!(spans[1].content.as_ref(), "курсивный");
        assert!(spans[1].style.add_modifier.contains(Modifier::ITALIC));
        assert!(!spans[1].style.add_modifier.contains(Modifier::BOLD));
        assert_eq!(line_text(&lines[0]), "это курсивный текст");
    }

    #[test]
    fn markdown_bold_italic_combines() {
        let lines = markdown_lines("***важно***", &Theme::default(), 80);
        let spans = &lines[0].spans;
        let all: String = spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(all, "важно");
        assert!(
            spans
                .iter()
                .any(|s| s.style.add_modifier.contains(Modifier::BOLD)
                    && s.style.add_modifier.contains(Modifier::ITALIC))
        );
    }

    #[test]
    fn completion_finds_mermaid() {
        assert_eq!(completion_candidates("/me"), vec!["/mermaid", "/memory"]);
    }

    #[test]
    fn completion_ignores_non_slash_and_args() {
        assert!(completion_candidates("привет").is_empty());
        assert!(completion_candidates("/rubric run").is_empty());
        assert!(completion_candidates("").is_empty());
    }

    #[test]
    fn completion_lists_all_on_bare_slash() {
        assert!(completion_candidates("/").len() >= 15);
    }
}
