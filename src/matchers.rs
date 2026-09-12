//! Каскад матчеров для `edit_file` (по опыту Claude Code ~9 уровней и
//! Theseus `matchers.rs`): модель часто воспроизводит фрагмент файла
//! с дрейфом пробелов — лишний хвостовой пробел, другой отступ, схлопнутый
//! двойной пробел. Точное совпадение в таких случаях падает, хотя намерение
//! однозначно. Каскад пробует уровни от строгого к терпимому; первый уровень,
//! давший вхождения, выигрывает.
//!
//! КОНТРАКТ (владелец: агент `tools`):
//! - уровни: [`MatchLevel::Exact`] (подстрока байт-в-байт) →
//!   [`MatchLevel::TrimEnd`] → [`MatchLevel::TrimBoth`] →
//!   [`MatchLevel::WhitespaceCollapsed`] (последние три — построчные блоки);
//! - [`cascade_find`] возвращает байтовые диапазоны вхождений и уровень;
//! - [`cascade_replace`] — замена с контрактом `edit_file`: 0 вхождений —
//!   подсказка, >1 без `replace_all` — ошибка неоднозначности;
//! - нечёткие уровни оперируют ЦЕЛЫМИ строками: замена никогда не рвёт
//!   строку посередине и не портит соседний текст.

use std::fmt::Write as _;

/// Уровень каскада, на котором найдено совпадение.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MatchLevel {
    /// Точная подстрока (байт-в-байт).
    Exact,
    /// Построчно, без хвостовых пробелов.
    TrimEnd,
    /// Построчно, без краевых пробелов (индифферентно к отступам).
    TrimBoth,
    /// Построчно, все пробельные серии схлопнуты в один пробел.
    WhitespaceCollapsed,
}

impl MatchLevel {
    /// Человекочитаемое имя уровня (для сообщений инструмента).
    #[must_use]
    pub const fn title(self) -> &'static str {
        match self {
            Self::Exact => "точное",
            Self::TrimEnd => "без хвостовых пробелов",
            Self::TrimBoth => "без краевых пробелов",
            Self::WhitespaceCollapsed => "схлопнутые пробелы",
        }
    }
}

/// Одно вхождение: байтовый диапазон в исходном тексте и уровень находки.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Found {
    /// Начало вхождения (байт).
    pub start: usize,
    /// Конец вхождения (байт, exclusive).
    pub end: usize,
    /// Уровень каскада, сработавший для этого вхождения.
    pub level: MatchLevel,
}

/// Нормализация строки по уровню каскада (Exact не нормализует).
fn normalize(line: &str, level: MatchLevel) -> String {
    match level {
        MatchLevel::Exact => line.to_string(),
        MatchLevel::TrimEnd => line.trim_end().to_string(),
        MatchLevel::TrimBoth => line.trim().to_string(),
        MatchLevel::WhitespaceCollapsed => line.split_whitespace().collect::<Vec<_>>().join(" "),
    }
}

/// Строки текста с байтовыми диапазонами (start, end включая `\n`, текст
/// строки без `\n`). Последняя строка без перевода — end = `text.len()`.
fn line_spans(text: &str) -> Vec<(usize, usize, &str)> {
    let mut out = Vec::new();
    let mut start = 0usize;
    for (i, b) in text.bytes().enumerate() {
        if b == b'\n' {
            out.push((start, i + 1, &text[start..i]));
            start = i + 1;
        }
    }
    if start < text.len() {
        out.push((start, text.len(), &text[start..]));
    }
    out
}

/// Построчный поиск блока `needle_lines` в `spans` с нормализацией `level`.
fn find_block(
    spans: &[(usize, usize, &str)],
    needle_lines: &[&str],
    level: MatchLevel,
) -> Vec<Found> {
    let n = needle_lines.len();
    if n == 0 || spans.len() < n {
        return Vec::new();
    }
    let normalized_needle: Vec<String> = needle_lines.iter().map(|l| normalize(l, level)).collect();
    let mut out = Vec::new();
    for w in 0..=spans.len() - n {
        let hit = spans[w..w + n]
            .iter()
            .zip(&normalized_needle)
            .all(|((_, _, line), want)| normalize(line, level) == *want);
        if hit {
            out.push(Found {
                start: spans[w].0,
                end: spans[w + n - 1].1,
                level,
            });
        }
    }
    out
}

/// Ищет `needle` в `haystack` по каскаду уровней. Первый уровень с
/// вхождениями выигрывает; пусто — совпадений нет ни на одном уровне.
#[must_use]
pub fn cascade_find(haystack: &str, needle: &str) -> Vec<Found> {
    if needle.is_empty() {
        return Vec::new();
    }
    // Уровень 0: точная подстрока (обратная совместимость edit_file).
    let exact: Vec<Found> = haystack
        .match_indices(needle)
        .map(|(start, _)| Found {
            start,
            end: start + needle.len(),
            level: MatchLevel::Exact,
        })
        .collect();
    if !exact.is_empty() {
        return exact;
    }
    // Нечёткие уровни — построчные блоки.
    let spans = line_spans(haystack);
    let needle_lines: Vec<&str> = needle.lines().collect();
    for level in [
        MatchLevel::TrimEnd,
        MatchLevel::TrimBoth,
        MatchLevel::WhitespaceCollapsed,
    ] {
        let found = find_block(&spans, &needle_lines, level);
        if !found.is_empty() {
            return found;
        }
    }
    Vec::new()
}

/// Замена по каскаду с контрактом `edit_file`.
///
/// Без `replace_all` вхождение обязано быть единственным. Возвращает
/// новый текст, уровень совпадения и число замен.
///
/// # Errors
/// Строка-ошибка с подсказкой: «не найден ни на одном уровне каскада»
/// или «неоднозначно (N вхождений)».
pub fn cascade_replace(
    haystack: &str,
    needle: &str,
    replacement: &str,
    replace_all: bool,
) -> std::result::Result<(String, MatchLevel, usize), String> {
    let found = cascade_find(haystack, needle);
    if found.is_empty() {
        return Err(
            "не найден ни на одном уровне каскада (точный, без хвостовых/краевых \
             пробелов, схлопнутые пробелы). Посмотрите фрагмент через read_file"
                .to_string(),
        );
    }
    let level = found[0].level;
    if found.len() > 1 && !replace_all {
        return Err(format!(
            "встречается {} раз (уровень «{}»); уточните контекст или replace_all=true",
            found.len(),
            level.title()
        ));
    }
    let count = if replace_all { found.len() } else { 1 };
    // Заменяем с конца, чтобы байтовые диапазоны не «поплыли».
    let mut out = haystack.to_string();
    for f in found.iter().take(count).rev() {
        // Блочный уровень: span включает перевод последней строки — сохраняем
        // его, иначе замена склеит следующую строку с нашим хвостом.
        let span_had_nl = haystack[f.start..f.end].ends_with('\n');
        let rep = if f.level != MatchLevel::Exact && span_had_nl && !replacement.ends_with('\n') {
            format!("{replacement}\n")
        } else {
            replacement.to_string()
        };
        out.replace_range(f.start..f.end, &rep);
    }
    Ok((out, level, count))
}

/// Подсказка для журнала/событий: как сработал каскад.
#[must_use]
pub fn level_note(level: MatchLevel, count: usize) -> String {
    let mut s = String::new();
    let _ = write!(s, "совпадение: {} ({} шт.)", level.title(), count);
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_match_wins_first() {
        let text = "fn main() {}\nlet x = 1;\n";
        let found = cascade_find(text, "let x = 1;");
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].level, MatchLevel::Exact);
        assert_eq!(&text[found[0].start..found[0].end], "let x = 1;");
    }

    #[test]
    fn trim_end_level_handles_trailing_spaces() {
        // В файле строка с хвостовыми пробелами, needle — без.
        let text = "a\nb   \nc\n";
        let found = cascade_find(text, "b\nc");
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].level, MatchLevel::TrimEnd);
        let (out, level, n) = cascade_replace(text, "b\nc", "B\nC", false).expect("replace");
        assert_eq!(level, MatchLevel::TrimEnd);
        assert_eq!(n, 1);
        assert_eq!(out, "a\nB\nC\n");
    }

    #[test]
    fn trim_both_handles_indent_drift() {
        let text = "if x {\n    foo();\n}\n";
        // Модель прислала блок с ДРУГИМИ отступами (2 пробела вместо 4):
        // целиком подстрокой не найти, TrimBoth выравнивает построчно.
        let (out, level, _) =
            cascade_replace(text, "  if x {\n  foo();", "if x {\n    bar();", false).expect("r");
        assert_eq!(level, MatchLevel::TrimBoth);
        assert_eq!(out, "if x {\n    bar();\n}\n");
    }

    #[test]
    fn collapsed_handles_double_spaces() {
        let text = "let  x   =  1;\n";
        let found = cascade_find(text, "let x = 1;");
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].level, MatchLevel::WhitespaceCollapsed);
    }

    #[test]
    fn multi_occurrence_requires_replace_all() {
        let text = "dup\ndup\n";
        let err = cascade_replace(text, "dup", "x", false).expect_err("ambiguous");
        assert!(err.contains("2 раз"), "{err}");
        let (out, _, n) = cascade_replace(text, "dup", "x", true).expect("all");
        assert_eq!(n, 2);
        assert_eq!(out, "x\nx\n");
    }

    #[test]
    fn not_found_on_any_level() {
        let err = cascade_replace("a\nb\n", "zzz", "x", false).expect_err("none");
        assert!(err.contains("каскада"), "{err}");
    }

    #[test]
    fn replacement_never_splits_lines() {
        // Блок из двух строк заменяется целиком, соседние строки не тронуты,
        // перевод строки сохранён (needle без отступов — уровень TrimBoth).
        let text = "head\n  one  \n  two  \ntail\n";
        let (out, level, _) = cascade_replace(text, "one\ntwo", "1\n2", false).expect("r");
        assert_eq!(level, MatchLevel::TrimBoth);
        assert_eq!(out, "head\n1\n2\ntail\n");
    }

    #[test]
    fn line_spans_cover_tail_without_newline() {
        let spans = line_spans("a\nb");
        assert_eq!(spans.len(), 2);
        assert_eq!(spans[1].1, 3, "конец текста без \\n");
        assert_eq!(spans[1].2, "b");
    }
}
