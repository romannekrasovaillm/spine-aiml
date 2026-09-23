//! Снятие ANSI ESC-последовательностей и опасных управляющих символов из
//! текста, пришедшего извне (stdout дочерних харнессов, отчёты субагентов,
//! ответы моделей).
//!
//! Два потребителя на разных слоях: TUI (ESC в кадре ratatui исполняется
//! терминалом — «мусор» цветных блоков, инцидент 2026-09-23) и разбор
//! контракта результата в `harness.rs` (ESC внутри JSON ломает serde).
//! Живёт в корне crate: TUI — верхний слой, зависеть от него нижним
//! запрещено (правило no-tui-below-ui).

use std::borrow::Cow;

/// Снимает ANSI ESC-последовательности (CSI/OSC/DCS/…) и опасные
/// управляющие символы из текста. Сохраняются `\n` и `\t`; чистый текст
/// возвращается без копирования.
#[must_use]
pub fn strip(text: &str) -> Cow<'_, str> {
    if text
        .chars()
        .any(|c| c == '\u{1b}' || (c.is_control() && !matches!(c, '\n' | '\t')))
    {
        Cow::Owned(strip_terminal_controls(text))
    } else {
        Cow::Borrowed(text)
    }
}

/// Пушит видимый символ; прочие управляющие, кроме `\n` и `\t`, глотаются
/// (C0, DEL и C1-диапазон — всё, что `char::is_control`).
fn push_visible(out: &mut String, c: char) {
    if !c.is_control() || matches!(c, '\n' | '\t') {
        out.push(c);
    }
}

/// Конечный автомат разбора ESC-последовательностей: CSI (`ESC [ … финал`
/// 0x40..=0x7E), строковые формы OSC/DCS/SOS/PM/APC (до BEL или ST =
/// `ESC \`), переключатели `ESC + промежуточные + один финальный`.
/// Незавершённая последовательность съедается до конца строки; символ,
/// не похожий на продолжение последовательности, остаётся в тексте.
fn strip_terminal_controls(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut it = text.chars().peekable();
    while let Some(c) = it.next() {
        if c != '\u{1b}' {
            push_visible(&mut out, c);
            continue;
        }
        match it.next() {
            Some('[') => {
                for c2 in it.by_ref() {
                    if ('\u{40}'..='\u{7e}').contains(&c2) {
                        break;
                    }
                }
            }
            Some(']' | 'P' | 'X' | '^' | '_') => {
                let mut prev_esc = false;
                for c2 in it.by_ref() {
                    if c2 == '\u{7}' || (prev_esc && c2 == '\\') {
                        break;
                    }
                    prev_esc = c2 == '\u{1b}';
                }
            }
            Some(c2) if ('\u{20}'..='\u{2f}').contains(&c2) => {
                while let Some(&c3) = it.peek() {
                    if ('\u{20}'..='\u{2f}').contains(&c3) {
                        it.next();
                    } else if ('\u{30}'..='\u{7e}').contains(&c3) {
                        it.next();
                        break;
                    } else {
                        // Битая форма: символ остаётся в тексте.
                        break;
                    }
                }
            }
            // ESC + один финальный (ESC 7, ESC M, ESC c, …).
            Some(c2) if ('\u{30}'..='\u{7e}').contains(&c2) => {}
            // Одинокий ESC: глотаем его, следующий символ возвращаем.
            Some(c2) => push_visible(&mut out, c2),
            None => {}
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strip_passes_clean_text_without_copy() {
        let clean = "обычный текст\nс переносом\tи табом";
        assert!(matches!(strip(clean), Cow::Borrowed(_)));
        assert_eq!(strip(clean), clean);
    }

    #[test]
    fn strip_removes_csi_colors() {
        // Формат из лога theseus: цветные маркеры вокруг текста.
        let dirty = "\u{1b}[32m❯ ответ\u{1b}[0m и \u{1b}[90m(мышление)\u{1b}[0m";
        assert_eq!(strip(dirty), "❯ ответ и (мышление)");
    }

    #[test]
    fn strip_removes_osc_with_bel_and_st() {
        // Гиперссылка OSC 8 с BEL-терминатором и заголовок с ST (ESC \).
        let dirty = "\u{1b}]8;;https://example.com\u{7}ссылка\u{1b}]8;;\u{7}";
        assert_eq!(strip(dirty), "ссылка");
        let dirty_st = "\u{1b}]0;заголовок\u{1b}\\текст";
        assert_eq!(strip(dirty_st), "текст");
    }

    #[test]
    fn strip_removes_charset_and_single_finals() {
        // ESC ( B — выбор кодировки; ESC 7 / ESC M — один финальный байт.
        assert_eq!(strip("\u{1b}(Babc"), "abc");
        assert_eq!(strip("a\u{1b}7b\u{1b}Mc"), "abc");
    }

    #[test]
    fn strip_drops_controls_but_keeps_layout() {
        // BEL, backspace, CR, C1 (NEL) — глотаются; \n и \t — выживают.
        let dirty = "a\u{7}\u{8}\u{85}\nb\tc";
        assert_eq!(strip(dirty), "a\nb\tc");
    }

    #[test]
    fn strip_eats_unterminated_sequence_at_eof() {
        assert_eq!(strip("хвост\u{1b}[31"), "хвост");
        assert_eq!(strip("хвост\u{1b}"), "хвост");
    }

    #[test]
    fn strip_keeps_char_after_lone_esc() {
        // ESC не перед последовательностью: глотается только сам ESC
        // ('я' вне диапазона финального байта 0x30..=0x7E, а вот 'y' в нём —
        // «ESC y» по ECMA-48 валидная двухбайтовая последовательность).
        assert_eq!(strip("x\u{1b}я"), "xя");
    }

    #[test]
    fn strip_real_world_harness_log_line() {
        // Строка из инцидента 2026-09-23 (отчёт hr-*, stdout theseus).
        let dirty =
            "\u{1b}[33m⚙ bash\u{1b}[90m {\"command\": \"ssh -o ConnectTimeout=5\"}\u{1b}[0m";
        assert_eq!(
            strip(dirty),
            "⚙ bash {\"command\": \"ssh -o ConnectTimeout=5\"}"
        );
    }
}
