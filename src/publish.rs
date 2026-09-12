//! Файловые адаптеры процессной обвязки (ADR-033: «git и файлы — транспорт»,
//! живых коннекторов нет): публикация артефактов в корпоративные системы
//! через их импортируемые форматы.
//!
//! - [`markdown_to_confluence`] — подмножество markdown → Confluence
//!   storage format (XHTML): заголовки h1–h4, таблицы, fenced-код
//!   (ac:structured-macro code), списки, инлайн-разметка (bold/italic/
//!   code/ссылки). Spine, ADR и evidence-индексы уходят в wiki без
//!   ручной перерисовки;
//! - [`handoff_json_to_jira_csv`] — JSON результата handoff
//!   (`status`/`assumptions`/`open_questions`/`conflicts_with_prior_decisions`)
//!   → Jira-CSV импорта (Summary/Type/Description/Labels): открытые
//!   вопросы и конфликты становятся трекаемыми задачами.

use std::fmt::Write as _;
use std::path::Path;

use serde::Deserialize;

use crate::error::{HarnessError, Result};

/// HTML-экранирование текстового содержимого storage format.
fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

/// Индлайн-разметка storage format: `**bold**`, `` `code` ``,
/// ссылки `[текст](url)`. Применяется ПОСЛЕ `html_escape` — спецсимволы уже
/// безопасны; разметка маркируется по исходным звёздочкам/бэктикам.
/// Курсив одинарными `*` намеренно не поддержан (частый ложный разбор
/// в технических текстах) — задокументированное подмножество.
fn inline_markup(text: &str) -> String {
    let escaped = html_escape(text);
    // Бэктики — первыми: их содержимое не размечается дальше.
    let mut out = String::with_capacity(escaped.len() + 16);
    let mut rest = escaped.as_str();
    while let Some(start) = rest.find('`') {
        let (before, after) = rest.split_at(start);
        out.push_str(before);
        if let Some(end) = after[1..].find('`') {
            out.push_str("<code>");
            out.push_str(&after[1..=end]);
            out.push_str("</code>");
            rest = &after[1 + end + 1..];
        } else {
            out.push('`');
            rest = &after[1..];
        }
    }
    out.push_str(rest);
    // Пары `**` — в <strong>…</strong>; непарная — обратно в литерал.
    let out = balance_strong(&out.replace("**", "<strong>"));
    // Ссылки [текст](url) — после emphasis, чтобы скобки url не размечались.
    links(&out)
}

/// Превращает маркеры `<strong>` в пары `<strong>…</strong>`; нечётный
/// маркер возвращается как литеральные `**`.
fn balance_strong(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut open = false;
    let mut rest = input;
    while let Some(pos) = rest.find("<strong>") {
        out.push_str(&rest[..pos]);
        if open {
            out.push_str("</strong>");
        } else {
            out.push_str("<strong>");
        }
        open = !open;
        rest = &rest[pos + "<strong>".len()..];
    }
    out.push_str(rest);
    if open {
        // Незакрытая пара: последний <strong> был лишним — вернуть `**`.
        if let Some(pos) = out.rfind("<strong>") {
            out.replace_range(pos..pos + "<strong>".len(), "**");
        }
    }
    out
}

/// `[текст](url)` → `<a href="url">текст</a>` (url без скобок и пробелов).
fn links(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut rest = input;
    while let Some(start) = rest.find('[') {
        let (before, after) = rest.split_at(start);
        out.push_str(before);
        let Some(close) = after.find("](") else {
            out.push('[');
            rest = &after[1..];
            continue;
        };
        let Some(end) = after[close + 2..].find(')') else {
            out.push('[');
            rest = &after[1..];
            continue;
        };
        let text = &after[1..close];
        let url = &after[close + 2..close + 2 + end];
        if url.contains(char::is_whitespace) {
            // Не ссылка (например, сноска с пробелом) — оставить как есть.
            out.push('[');
            out.push_str(&after[1..close + 2]);
            rest = &after[close + 2..];
            continue;
        }
        let _ = write!(out, "<a href=\"{url}\">{text}</a>");
        rest = &after[close + 2 + end + 1..];
    }
    out.push_str(rest);
    out
}

/// Подмножество markdown → Confluence storage format.
///
/// Поддерживается: заголовки `#`–`####`, таблицы `| … |`, fenced-блоки
/// кода (тело — в CDATA без разметки), списки `-` и `1.` (без вложенности),
/// инлайн `**`/`*`/`` ` ``/ссылки, параграфы. Горизонтальные линии,
/// цитаты и вложенные списки — out of scope (документировано).
#[must_use]
pub fn markdown_to_confluence(markdown: &str) -> String {
    let mut out = String::with_capacity(markdown.len() * 2);
    let mut para = String::new();
    let mut in_code = false;
    let mut code_buf = String::new();
    let mut in_table = false;
    let mut list: Option<char> = None; // 'u' | 'o'
    let mut cur_item = String::new(); // текущий <li> (собирается с переносами)

    let flush_para = |out: &mut String, para: &mut String| {
        if !para.trim().is_empty() {
            let _ = write!(out, "<p>{}</p>", inline_markup(para.trim()));
            para.clear();
        }
    };
    let flush_item = |out: &mut String, cur_item: &mut String| {
        if !cur_item.trim().is_empty() {
            let _ = write!(out, "<li>{}</li>", inline_markup(cur_item.trim()));
            cur_item.clear();
        }
    };
    let close_list = |out: &mut String, list: &mut Option<char>, cur_item: &mut String| {
        flush_item(out, cur_item);
        if let Some(kind) = list.take() {
            out.push_str(if kind == 'u' { "</ul>" } else { "</ol>" });
        }
    };
    let close_table = |out: &mut String, in_table: &mut bool| {
        if *in_table {
            out.push_str("</tbody></table>");
            *in_table = false;
        }
    };

    for line in markdown.lines() {
        let trimmed = line.trim_end();
        if let Some(stripped) = trimmed.trim_start().strip_prefix("```") {
            if in_code {
                let _ = write!(
                    out,
                    "<ac:structured-macro ac:name=\"code\"><ac:plain-text-body><![CDATA[{code_buf}]]></ac:plain-text-body></ac:structured-macro>"
                );
                code_buf.clear();
                in_code = false;
            } else {
                flush_para(&mut out, &mut para);
                close_list(&mut out, &mut list, &mut cur_item);
                close_table(&mut out, &mut in_table);
                in_code = true;
                let _ = stripped; // язык блока игнорируем (подмножество)
            }
            continue;
        }
        if in_code {
            code_buf.push_str(trimmed);
            code_buf.push('\n');
            continue;
        }
        let line = trimmed.trim();
        if line.is_empty() {
            flush_para(&mut out, &mut para);
            close_list(&mut out, &mut list, &mut cur_item);
            close_table(&mut out, &mut in_table);
            continue;
        }
        // Таблица: строка вида | … |; строка-разделитель |---| пропускается.
        if line.starts_with('|') && line.ends_with('|') {
            let cells: Vec<String> = line
                .trim_matches('|')
                .split('|')
                .map(|c| inline_markup(c.trim()))
                .collect();
            if cells
                .iter()
                .all(|c| c.chars().all(|ch| ch == '-' || ch == ':' || ch == ' '))
            {
                continue;
            }
            if !in_table {
                flush_para(&mut out, &mut para);
                close_list(&mut out, &mut list, &mut cur_item);
                out.push_str("<table><tbody>");
                in_table = true;
                out.push_str("<tr>");
                for c in &cells {
                    let _ = write!(out, "<th>{c}</th>");
                }
                out.push_str("</tr>");
                continue;
            }
            out.push_str("<tr>");
            for c in &cells {
                let _ = write!(out, "<td>{c}</td>");
            }
            out.push_str("</tr>");
            continue;
        }
        close_table(&mut out, &mut in_table);
        let hashes = line.chars().take_while(|c| *c == '#').count();
        if (1..=4).contains(&hashes) && line[hashes..].starts_with(' ') {
            flush_para(&mut out, &mut para);
            close_list(&mut out, &mut list, &mut cur_item);
            let _ = write!(
                out,
                "<h{hashes}>{}</h{hashes}>",
                inline_markup(line[hashes + 1..].trim())
            );
            continue;
        }
        if let Some(item) = line.strip_prefix("- ") {
            flush_para(&mut out, &mut para);
            if list == Some('u') {
                flush_item(&mut out, &mut cur_item);
            } else {
                close_list(&mut out, &mut list, &mut cur_item);
                out.push_str("<ul>");
                list = Some('u');
            }
            cur_item.push_str(item.trim());
            continue;
        }
        if let Some(dot) = line.find(". ") {
            if line[..dot].chars().all(|c| c.is_ascii_digit()) && !line[..dot].is_empty() {
                flush_para(&mut out, &mut para);
                if list == Some('o') {
                    flush_item(&mut out, &mut cur_item);
                } else {
                    close_list(&mut out, &mut list, &mut cur_item);
                    out.push_str("<ol>");
                    list = Some('o');
                }
                cur_item.push_str(line[dot + 2..].trim());
                continue;
            }
        }
        // Перенос строки пункта списка — продолжение текущего <li>.
        if list.is_some() && !cur_item.is_empty() {
            cur_item.push(' ');
            cur_item.push_str(line);
            continue;
        }
        close_list(&mut out, &mut list, &mut cur_item);
        if !para.is_empty() {
            para.push(' ');
        }
        para.push_str(line);
    }
    flush_para(&mut out, &mut para);
    close_list(&mut out, &mut list, &mut cur_item);
    close_table(&mut out, &mut in_table);
    out
}

/// JSON результата handoff (headless-контракт, ADR-020).
#[derive(Debug, Deserialize)]
struct HandoffResult {
    /// Итоговый статус исполнения.
    #[allow(dead_code)] // поле — часть контракта; в CSV не попадает
    status: Option<String>,
    /// Допущения исполнителя.
    #[serde(default)]
    assumptions: Vec<String>,
    /// Открытые вопросы к архитектору.
    #[serde(default)]
    open_questions: Vec<String>,
    /// Конфликты с прежними решениями.
    #[serde(default)]
    conflicts_with_prior_decisions: Vec<String>,
}

/// CSV-экранирование поля (Jira импортирует стандартный CSV).
fn csv_field(value: &str) -> String {
    if value.contains([',', '"', '\n']) {
        format!("\"{}\"", value.replace('"', "\"\""))
    } else {
        value.to_string()
    }
}

/// Усечение summary для Jira-строки: не более `max` символов + многоточие.
fn truncate_summary(value: &str, max: usize) -> String {
    let mut chars = value.chars();
    let taken: String = chars.by_ref().take(max).collect();
    if chars.next().is_some() {
        format!("{taken}…")
    } else {
        taken
    }
}

/// JSON результата handoff → Jira-CSV (Summary,Type,Description,Labels).
///
/// Каждый открытый вопрос — задача `Question`, конфликт — `Task` с меткой
/// `conflict`, допущение — `Task` с меткой `assumption`. `project_key`
/// (если задан) попадает в метку `spine-<ключ>` для фильтрации в Jira.
///
/// # Errors
/// Файл не читается или не разбирается как handoff-результат.
pub fn handoff_json_to_jira_csv(path: &Path, project_key: Option<&str>) -> Result<String> {
    let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let result: HandoffResult = serde_json::from_str(&text).map_err(|e| {
        HarnessError::Model(format!(
            "{}: не handoff-результат (status/assumptions/open_questions/…): {e}",
            path.display()
        ))
    })?;
    let mut out = String::from("Summary,Type,Description,Labels\n");
    let label = project_key
        .map(|k| format!(" spine-{k}"))
        .unwrap_or_default();
    for q in &result.open_questions {
        let _ = writeln!(
            out,
            "{},Question,\"{}\",\"spine-handoff{label}\"",
            csv_field(&format!("Вопрос архитектору: {}", truncate_summary(q, 80))),
            q.replace('"', "\"\"").replace('\n', " ")
        );
    }
    for c in &result.conflicts_with_prior_decisions {
        let _ = writeln!(
            out,
            "{},Task,\"{}\",\"spine-handoff conflict{label}\"",
            csv_field(&format!("Конфликт с решением: {}", truncate_summary(c, 80))),
            c.replace('"', "\"\"").replace('\n', " ")
        );
    }
    for a in &result.assumptions {
        let _ = writeln!(
            out,
            "{},Task,\"{}\",\"spine-handoff assumption{label}\"",
            csv_field(&format!("Допущение: {}", truncate_summary(a, 80))),
            a.replace('"', "\"\"").replace('\n', " ")
        );
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn confluence_headings_lists_and_inline() {
        let md = "# Заголовок\n\nТекст с **жирным** и `кодом` и [ссылкой](https://x.y).\n\n- один\n- два\n\n1. первый\n2. второй\n";
        let out = markdown_to_confluence(md);
        assert!(out.contains("<h1>Заголовок</h1>"), "{out}");
        assert!(out.contains("<strong>жирным</strong>"), "{out}");
        assert!(out.contains("<code>кодом</code>"), "{out}");
        assert!(out.contains("<a href=\"https://x.y\">ссылкой</a>"), "{out}");
        assert!(out.contains("<ul><li>один</li><li>два</li></ul>"), "{out}");
        assert!(
            out.contains("<ol><li>первый</li><li>второй</li></ol>"),
            "{out}"
        );
    }

    #[test]
    fn confluence_multiline_list_item_stays_in_one_li() {
        let md = "- первый пункт с переносом\n  продолжение строки\n- второй\n\nТекст после.\n";
        let out = markdown_to_confluence(md);
        assert!(
            out.contains(
                "<ul><li>первый пункт с переносом продолжение строки</li><li>второй</li></ul>"
            ),
            "{out}"
        );
        assert!(out.contains("<p>Текст после.</p>"), "{out}");
    }

    #[test]
    fn confluence_table_and_code_block() {
        let md = "| A | B |\n|---|---|\n| 1 | 2 |\n\n```rust\nfn main() { let a = 1 < 2; }\n```\n";
        let out = markdown_to_confluence(md);
        assert!(
            out.contains("<table><tbody><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></tbody></table>"),
            "{out}"
        );
        assert!(
            out.contains("<![CDATA[fn main() { let a = 1 < 2; }\n]]>"),
            "{out}"
        );
    }

    #[test]
    fn confluence_escapes_html_and_handles_unbalanced_bold() {
        let out = markdown_to_confluence("a < b & **непара");
        assert!(out.contains("a &lt; b &amp;"), "{out}");
        assert!(out.contains("**непара"), "{out}");
        assert!(!out.matches("<strong>").count().eq(&1), "{out}");
    }

    #[test]
    fn jira_csv_from_handoff_result() {
        let dir = tempfile::tempdir().expect("tmp");
        let file = dir.path().join("result.json");
        std::fs::write(
            &file,
            r#"{"status": "blocked", "open_questions": ["Кто владеет, \"смешной\" вопрос?"],
               "conflicts_with_prior_decisions": ["AD-2 против выбора"],
               "assumptions": ["доступ к стенду будет"]}"#,
        )
        .expect("write");
        let csv = handoff_json_to_jira_csv(&file, Some("PAY")).expect("csv");
        assert!(
            csv.starts_with("Summary,Type,Description,Labels\n"),
            "{csv}"
        );
        assert!(csv.contains("Question"), "{csv}");
        assert!(csv.contains("\"\"смешной\"\" вопрос"), "{csv}");
        assert!(csv.contains("spine-PAY"), "{csv}");
        assert!(csv.contains("conflict"), "{csv}");
        assert!(csv.contains("assumption"), "{csv}");
    }

    #[test]
    fn jira_csv_rejects_non_handoff_json() {
        let dir = tempfile::tempdir().expect("tmp");
        let file = dir.path().join("x.json");
        std::fs::write(&file, "[1,2,3]").expect("write");
        let err = handoff_json_to_jira_csv(&file, None).expect_err("не handoff");
        assert!(err.to_string().contains("не handoff-результат"), "{err}");
    }
}
