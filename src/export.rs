//! Экспорт экрана диалога в Word (.docx) и Excel (.xlsx) — без тяжёлых
//! зависимостей: оба формата — ZIP с XML, пишутся напрямую через `zip`.
//! Корпоративный кейс: результат сессии (обсуждение ADR, диаграмма,
//! вердикт рубрики) одной командой уходит в отчёт для правления.
//!
//! КОНТРАКТ (владелец: агент `tui`):
//! - [`rows_of`] разворачивает блоки чата в строки `(роль, текст)` —
//!   по строке на линию блока, роль только у первой линии;
//! - [`export_docx`] / [`export_xlsx`] пишут валидные минимальные пакеты
//!   (document.xml / sheet1.xml + rels + content types); арт (mermaid)
//!   в Word — моноширинным Courier New, чтобы схема не «плыла»;
//! - XML-экранирование централизовано ([`xml_escape`]).

use std::fmt::Write as _;
use std::io::Write as _;
use std::path::Path;

use crate::error::{HarnessError, Result};
use crate::tui::app::ChatBlock;

/// Формат экспорта экрана.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExportFormat {
    /// Microsoft Word (.docx).
    Word,
    /// Microsoft Excel (.xlsx).
    Excel,
}

impl ExportFormat {
    /// Разбор из аргумента слэш-команды (`word`/`docx`/`excel`/`xlsx`).
    #[must_use]
    pub fn parse(s: &str) -> Option<Self> {
        match s.to_ascii_lowercase().as_str() {
            "word" | "docx" | "doc" => Some(Self::Word),
            "excel" | "xlsx" | "xls" => Some(Self::Excel),
            _ => None,
        }
    }

    /// Расширение файла формата.
    #[must_use]
    pub const fn extension(self) -> &'static str {
        match self {
            Self::Word => "docx",
            Self::Excel => "xlsx",
        }
    }
}

/// Строка экспорта: роль блока (только у первой линии) + текст линии.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportRow {
    /// Роль («вы», «арх», «✓ tool», …); пусто у продолжений блока.
    pub role: String,
    /// Текст линии.
    pub text: String,
}

/// Блоки чата → плоские строки для экспорта.
#[must_use]
pub(crate) fn rows_of(blocks: &[ChatBlock]) -> Vec<ExportRow> {
    let mut rows = Vec::new();
    let mut push = |role: String, text: &str| {
        let mut first = true;
        for line in text.lines() {
            rows.push(ExportRow {
                role: if first { role.clone() } else { String::new() },
                text: line.to_string(),
            });
            first = false;
        }
        if first {
            // Пустой блок — строка с одной ролью.
            rows.push(ExportRow {
                role,
                text: String::new(),
            });
        }
    };
    for block in blocks {
        match block {
            ChatBlock::Logo => push("» ArchSpine".into(), crate::assets::BANNER),
            ChatBlock::User(text) => push("вы".into(), text),
            ChatBlock::Assistant(text) => push("арх".into(), text),
            ChatBlock::Thinking(text) => push("» мысли".into(), text),
            ChatBlock::Tool {
                name,
                state,
                action,
                summary,
            } => {
                let mark = match state {
                    crate::tui::app::ToolState::Running => "◌",
                    crate::tui::app::ToolState::Ok => "✓",
                    crate::tui::app::ToolState::Error => "✗",
                };
                push(
                    if action.is_empty() {
                        format!("{mark} {name}")
                    } else {
                        format!("{mark} {name} {action}")
                    },
                    summary,
                );
            }
            ChatBlock::System { command, text } => push(format!("» {command}"), text),
            ChatBlock::Error(text) => push("✗ ошибка".into(), text),
        }
    }
    rows
}

/// XML-экранирование текста ячейки/параграфа.
fn xml_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '&' => out.push_str("&amp;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&apos;"),
            _ => out.push(c),
        }
    }
    out
}

/// Линия — часть ASCII-арта (box-линии/геометрия)? Копия эвристики рендера:
/// арт в Word печатается моноширинным, чтобы не развалиться.
fn is_art(text: &str) -> bool {
    text.chars()
        .any(|c| ('\u{2500}'..='\u{257f}').contains(&c) || ('\u{25a0}'..='\u{25ff}').contains(&c))
}

/// Человекочитаемая ошибка харнесса (zip-упаковка экспорта и т.п.).
fn plain(msg: String) -> HarnessError {
    HarnessError::IoBare(std::io::Error::other(msg))
}

/// Пишет ZIP-файл с заданными записями (имя → содержимое).
fn write_zip(path: &Path, entries: &[(&str, String)]) -> Result<()> {
    let file = std::fs::File::create(path).map_err(|e| HarnessError::io(path, e))?;
    let mut zip = zip::ZipWriter::new(file);
    let options = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated);
    for (name, body) in entries {
        zip.start_file(name, options)
            .map_err(|e| plain(format!("{}: zip {name}: {e}", path.display())))?;
        zip.write_all(body.as_bytes())
            .map_err(|e| HarnessError::io(path, e))?;
    }
    zip.finish()
        .map_err(|e| plain(format!("{}: zip finish: {e}", path.display())))?;
    Ok(())
}

/// Экспортирует строки в Word (.docx): роль — жирным cyan-ish, арт —
/// моноширинным Courier New 9pt.
///
/// # Errors
/// Файл не создаётся/не пишется.
pub fn export_docx(rows: &[ExportRow], path: &Path) -> Result<()> {
    let mut body = String::from(
        r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>"#,
    );
    for row in rows {
        let text_rpr = if is_art(&row.text) {
            r#"<w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/><w:sz w:val="18"/></w:rPr>"#
        } else {
            ""
        };
        body.push_str("<w:p>");
        if !row.role.is_empty() {
            let _ = write!(
                body,
                r#"<w:r><w:rPr><w:b/><w:color w:val="7DCFFF"/></w:rPr><w:t xml:space="preserve">{}: </w:t></w:r>"#,
                xml_escape(&row.role)
            );
        }
        let _ = write!(
            body,
            r#"<w:r>{runs}<w:t xml:space="preserve">{}</w:t></w:r>"#,
            xml_escape(&row.text),
            runs = text_rpr
        );
        body.push_str("</w:p>");
    }
    body.push_str(r"</w:body></w:document>");

    write_zip(
        path,
        &[
            (
                "[Content_Types].xml",
                r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"#
                    .to_string(),
            ),
            (
                "_rels/.rels",
                r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>"#
                    .to_string(),
            ),
            ("word/document.xml", body),
        ],
    )
}

/// Экспортирует строки в Excel (.xlsx): колонка A — роль, B — текст
/// (inline-строки, без sharedStrings).
///
/// # Errors
/// Файл не создаётся/не пишется.
pub fn export_xlsx(rows: &[ExportRow], path: &Path) -> Result<()> {
    let mut sheet = String::from(
        r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cols><col min="1" max="1" width="18" customWidth="1"/><col min="2" max="2" width="120" customWidth="1"/></cols><sheetData>"#,
    );
    for (i, row) in rows.iter().enumerate() {
        let r = i + 1;
        let _ = write!(
            sheet,
            r#"<row r="{r}"><c r="A{r}" t="inlineStr"><is><t xml:space="preserve">{}</t></is></c><c r="B{r}" t="inlineStr"><is><t xml:space="preserve">{}</t></is></c></row>"#,
            xml_escape(&row.role),
            xml_escape(&row.text)
        );
    }
    sheet.push_str("</sheetData></worksheet>");

    write_zip(
        path,
        &[
            (
                "[Content_Types].xml",
                r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>"#
                    .to_string(),
            ),
            (
                "_rels/.rels",
                r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>"#
                    .to_string(),
            ),
            (
                "xl/workbook.xml",
                r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Экран arch-ml" sheetId="1" r:id="rId1"/></sheets></workbook>"#
                    .to_string(),
            ),
            (
                "xl/_rels/workbook.xml.rels",
                r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>"#
                    .to_string(),
            ),
            ("xl/worksheets/sheet1.xml", sheet),
        ],
    )
}

/// Экспорт экрана в выбранном формате.
///
/// # Errors
/// См. [`export_docx`]/[`export_xlsx`].
pub(crate) fn export_blocks(
    blocks: &[ChatBlock],
    format: ExportFormat,
    path: &Path,
) -> Result<usize> {
    let rows = rows_of(blocks);
    match format {
        ExportFormat::Word => export_docx(&rows, path)?,
        ExportFormat::Excel => export_xlsx(&rows, path)?,
    }
    Ok(rows.len())
}

/// Журнал сессии (JSONL) → строки экспорта: user/assistant с содержимым,
/// tool — «имя ✓/✗ + первая строка вывода». Для CLI `arch-ml export` —
/// выгрузка прошлой сессии без TUI.
///
/// # Errors
/// Файл не читается.
pub fn rows_of_journal(path: &Path) -> Result<Vec<ExportRow>> {
    let text = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let mut rows = Vec::new();
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        let kind = v.get("kind").and_then(|k| k.as_str()).unwrap_or("");
        let content = v.get("content").and_then(|c| c.as_str()).unwrap_or("");
        let role = match kind {
            "user" => "вы",
            "assistant" => "арх",
            "tool" => {
                let name = v.get("name").and_then(|n| n.as_str()).unwrap_or("tool");
                let is_err = v
                    .get("is_error")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false);
                let head = content.lines().next().unwrap_or("");
                rows.push(ExportRow {
                    role: format!("{} {name}", if is_err { "✗" } else { "✓" }),
                    text: head.to_string(),
                });
                continue;
            }
            _ => continue,
        };
        if content.is_empty() {
            continue;
        }
        let mut first = true;
        for l in content.lines() {
            rows.push(ExportRow {
                role: if first { role.into() } else { String::new() },
                text: l.to_string(),
            });
            first = false;
        }
    }
    Ok(rows)
}

/// Экспорт журнала сессии в файл (CLI).
///
/// # Errors
/// Журнал не читается, файл не пишется.
pub fn export_journal(session: &Path, format: ExportFormat, out: &Path) -> Result<usize> {
    let rows = rows_of_journal(session)?;
    match format {
        ExportFormat::Word => export_docx(&rows, out)?,
        ExportFormat::Excel => export_xlsx(&rows, out)?,
    }
    Ok(rows.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_blocks() -> Vec<ChatBlock> {
        vec![
            ChatBlock::User("сделай диаграмму".into()),
            ChatBlock::Assistant("Вот схема:\nпоток A → B".into()),
            ChatBlock::System {
                command: "mermaid".into(),
                text: "┌───┐\n│ A │\n└───┘".into(),
            },
            ChatBlock::Error("мелочь & <прочее>".into()),
        ]
    }

    #[test]
    fn rows_flatten_blocks_with_roles() {
        let rows = rows_of(&sample_blocks());
        assert_eq!(rows[0].role, "вы");
        assert_eq!(rows[1].role, "арх");
        assert_eq!(rows[2].role, "", "вторая линия блока — без роли");
        assert!(rows.iter().any(|r| r.role == "» mermaid"));
        assert_eq!(rows.len(), 7);
    }

    #[test]
    fn format_parse_aliases() {
        assert_eq!(ExportFormat::parse("word"), Some(ExportFormat::Word));
        assert_eq!(ExportFormat::parse("DOCX"), Some(ExportFormat::Word));
        assert_eq!(ExportFormat::parse("excel"), Some(ExportFormat::Excel));
        assert_eq!(ExportFormat::parse("xlsx"), Some(ExportFormat::Excel));
        assert_eq!(ExportFormat::parse("pdf"), None);
    }

    #[test]
    fn xml_escape_covers_specials() {
        assert_eq!(
            xml_escape("a<b>&\"'\""),
            "a&lt;b&gt;&amp;&quot;&apos;&quot;"
        );
    }

    /// Читает запись из готового ZIP-пакета.
    fn zip_entry(path: &Path, name: &str) -> String {
        let file = std::fs::File::open(path).expect("open zip");
        let mut zip = zip::ZipArchive::new(file).expect("valid zip");
        let mut entry = zip.by_name(name).expect("entry exists");
        let mut s = String::new();
        std::io::Read::read_to_string(&mut entry, &mut s).expect("read entry");
        s
    }

    #[test]
    fn docx_is_valid_zip_with_document() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = tmp.path().join("screen.docx");
        let rows = rows_of(&sample_blocks());
        export_docx(&rows, &path).expect("export");
        let doc = zip_entry(&path, "word/document.xml");
        assert!(doc.contains("сделай диаграмму"), "{doc}");
        assert!(doc.contains("Courier New"), "арт моноширинным: {doc}");
        assert!(
            doc.contains("мелочь &amp; &lt;прочее&gt;"),
            "экранирование: {doc}"
        );
        let types = zip_entry(&path, "[Content_Types].xml");
        assert!(types.contains("wordprocessingml.document.main"));
    }

    #[test]
    fn xlsx_is_valid_zip_with_sheet() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = tmp.path().join("screen.xlsx");
        let rows = rows_of(&sample_blocks());
        export_xlsx(&rows, &path).expect("export");
        let sheet = zip_entry(&path, "xl/worksheets/sheet1.xml");
        assert!(sheet.contains(r#"<row r="1">"#), "{sheet}");
        assert!(sheet.contains("поток A → B"), "{sheet}");
        assert!(sheet.contains("┌───┐"), "арт в ячейке: {sheet}");
        let wb = zip_entry(&path, "xl/workbook.xml");
        assert!(wb.contains("Экран arch-ml"));
    }

    #[test]
    fn export_blocks_counts_rows() {
        let tmp = tempfile::tempdir().expect("tmp");
        let path = tmp.path().join("e.docx");
        let n = export_blocks(&sample_blocks(), ExportFormat::Word, &path).expect("ok");
        assert_eq!(n, 7);
        assert!(path.is_file());
    }
}
