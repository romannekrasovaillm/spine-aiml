//! Mermaid → Unicode/ASCII-арт рендер.
//!
//! КОНТРАКТ (владелец: агент `mermaid`): подмножество mermaid —
//! `graph|flowchart TD|TB|BT|LR|RL` (узлы `A[label]`, `B(label)`, `C{label}`,
//! `D((label))`, рёбра `-->`, `---`, `-.->`, `-- label -->`, цепочки
//! `A --> B --> C`, quoted-метки `A["текст с --> внутри"]`), `sequenceDiagram`
//! (участники `participant X as Label`, `->>`/`-->>`, `Note left/right of`),
//! `erDiagram` (сущности с блоками атрибутов `{ тип имя [PK] }`, связи
//! `A ||--o{ B : label` с кардинальностями; ADR-009) и C4-подмножество
//! (`C4Context`/`C4Container`/`C4Component`: `Person`/`System`/`Container`/
//! `Component` + суффиксы `_Ext`/`Db`/`Queue`, связи `Rel*`/`BiRel`;
//! boundaries/стили пропускаются; ADR-009).
//! Рендер — на символьную сетку box-drawing символами (┌─┐│└┘, ▼, ─▶),
//! layered layout (Sugiyama-lite: слои по longest path, barycenter-сортировка);
//! ER и C4 понижаются к flowchart-AST (узлы с многострочными метками).
//! Неподдерживаемые конструкции (`subgraph`, `classDef`, `click`, `style`,
//! `loop`, `*_Boundary`, …) пропускаются с предупреждением `%% пропущено: …`
//! в конце вывода. `C4Deployment`/`C4Dynamic` отклоняются с подсказкой-рецептом.
//! Без внешних зависимостей-рендеров; чистые функции, покрытые тестами.

mod draw;
mod layout;
mod model;
mod parse;

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value;

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Вид диаграммы (детект по первой значащей строке).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiagramKind {
    /// `graph`/`flowchart` с направлением.
    Flowchart,
    /// `sequenceDiagram`.
    Sequence,
    /// `erDiagram` (ADR-009).
    Er,
    /// `C4Context`/`C4Container`/`C4Component` (ADR-009).
    C4,
    /// `C4Deployment`/`C4Dynamic` — осознанно не поддерживаются (ADR-009):
    /// deployment выражается рецептом flowchart+subgraph, динамика — sequence.
    C4Unsupported,
    /// Не удалось определить.
    Unknown,
}

/// Определяет вид диаграммы по заголовку.
///
/// Пустые строки и строки-комментарии `%%` пропускаются; заголовком считается
/// первая содержательная строка.
#[must_use]
pub fn diagram_kind(input: &str) -> DiagramKind {
    for line in input.lines() {
        let t = line.trim();
        if t.is_empty() || t.starts_with("%%") {
            continue;
        }
        let Some(first) = t.split_whitespace().next() else {
            continue;
        };
        if first == "graph" || first == "flowchart" {
            return DiagramKind::Flowchart;
        }
        if first == "sequenceDiagram" {
            return DiagramKind::Sequence;
        }
        if first == "erDiagram" {
            return DiagramKind::Er;
        }
        if matches!(first, "C4Context" | "C4Container" | "C4Component") {
            return DiagramKind::C4;
        }
        if matches!(first, "C4Deployment" | "C4Dynamic") {
            return DiagramKind::C4Unsupported;
        }
        return DiagramKind::Unknown;
    }
    DiagramKind::Unknown
}

/// Рендерит mermaid-диаграмму в Unicode/ASCII-арт.
///
/// # Errors
/// Неподдерживаемый вид диаграммы, синтаксические ошибки подмножества
/// (в тексте ошибки — номер строки и фрагмент), диаграмма без узлов/участников.
pub fn render(input: &str) -> Result<String> {
    match diagram_kind(input) {
        DiagramKind::Flowchart => {
            let ast = parse::parse_flowchart(input)?;
            Ok(draw::render_flowchart(&ast))
        }
        DiagramKind::Sequence => {
            let ast = parse::parse_sequence(input)?;
            Ok(draw::render_sequence(&ast))
        }
        DiagramKind::Er => {
            let ast = parse::parse_er(input)?;
            Ok(draw::render_flowchart(&ast.to_flow()))
        }
        DiagramKind::C4 => {
            let ast = parse::parse_c4(input)?;
            Ok(draw::render_flowchart(&ast.to_flow()))
        }
        DiagramKind::C4Unsupported => Err(HarnessError::Mermaid(
            "C4Deployment/C4Dynamic не поддерживаются (ADR-009): deployment-вид \
             соберите через 'flowchart TD' + subgraph (окружение → subgraph, \
             артефакты → узлы), динамику вызовов — через sequenceDiagram"
                .into(),
        )),
        DiagramKind::Unknown => Err(HarnessError::Mermaid(
            "не удалось определить вид диаграммы: первая строка должна быть \
             'graph TD|TB|BT|LR|RL', 'flowchart …', 'sequenceDiagram', \
             'erDiagram' или 'C4Context'/'C4Container'/'C4Component'"
                .into(),
        )),
    }
}

/// Инструменты домена: `mermaid_render` (code → ascii-арт).
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(MermaidRenderTool)]
}

/// Инструмент `mermaid_render`: `code` (исходник) или `path` (файл) → арт.
struct MermaidRenderTool;

#[async_trait]
impl Tool for MermaidRenderTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "mermaid_render".to_owned(),
            description: "Рендерит mermaid-диаграмму (flowchart, sequenceDiagram, erDiagram \
                          или C4Context/C4Container/C4Component) в Unicode/ASCII-арт. \
                          Вход: 'code' (исходник) или 'path' (путь к .mmd-файлу относительно \
                          рабочего каталога). Только ad-hoc mermaid-черновики: Archify IR \
                          НЕ конвертировать в mermaid ни в каком виде — ни для показа, ни для \
                          «структуры в чат»; такая конвертация теряет оригинал и запрещена. \
                          Показ Archify-диаграммы — инструмент archify_show (HTML в браузере)."
                .to_owned(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Исходный код mermaid-диаграммы"
                    },
                    "path": {
                        "type": "string",
                        "description": "Путь к .mmd-файлу (резолвится от рабочего каталога)"
                    }
                }
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let code = match args.get("code").and_then(Value::as_str) {
            Some(code) if !code.trim().is_empty() => code.to_owned(),
            _ => match args.get("path").and_then(Value::as_str) {
                Some(path) => {
                    let full = ctx.resolve(path);
                    // Каталог — подсказка модели (ToolOutput::err), не сбой вызова.
                    match read_diagram_source(&full) {
                        Ok(code) => code,
                        Err(e @ HarnessError::Mermaid(_)) => {
                            return Ok(ToolOutput::err(e.to_string()));
                        }
                        Err(e) => return Err(e),
                    }
                }
                None => {
                    return Ok(ToolOutput::err(
                        "mermaid_render: нужен аргумент 'code' (исходник) или 'path' (файл)",
                    ));
                }
            },
        };
        // Ошибки парсера (с номером строки) — сигнал модели, а не сбой вызова.
        match render(&code) {
            Ok(art) => Ok(ToolOutput::ok(art)),
            Err(e) => Ok(ToolOutput::err(e.to_string())),
        }
    }
}

/// Читает исходник диаграммы из файла. Каталог — не сырой «Is a directory
/// (os error 21)», а понятная подсказка: список *.mmd/*.mermaid в нём
/// (или первые файлы каталога, если диаграмм нет) + как вызвать правильно.
///
/// # Errors
/// [`HarnessError::Mermaid`] с подсказкой — путь является каталогом;
/// [`HarnessError::Io`] — файл не читается.
pub fn read_diagram_source(path: &std::path::Path) -> Result<String> {
    if path.is_dir() {
        let mut diagrams: Vec<String> = Vec::new();
        let mut others: Vec<String> = Vec::new();
        if let Ok(rd) = std::fs::read_dir(path) {
            for entry in rd.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                let is_diagram = entry
                    .path()
                    .extension()
                    .is_some_and(|x| x == "mmd" || x == "mermaid");
                if is_diagram {
                    diagrams.push(name);
                } else {
                    others.push(name);
                }
            }
        }
        diagrams.sort();
        others.sort();
        let listing = if diagrams.is_empty() {
            let head: Vec<String> = others.into_iter().take(10).collect();
            if head.is_empty() {
                "каталог пуст".to_string()
            } else {
                format!(
                    "файлов *.mmd/*.mermaid в нём нет; содержимое (первые 10):\n  {}",
                    head.join("\n  ")
                )
            }
        } else {
            let head: Vec<String> = diagrams.into_iter().take(10).collect();
            format!("файлы диаграмм в нём:\n  {}", head.join("\n  "))
        };
        return Err(HarnessError::Mermaid(format!(
            "{} — каталог, а не файл диаграммы: {}\n\
             Укажите файл (/mermaid <путь> или path=… в mermaid_render) \
             или передайте inline-код (flowchart …, sequenceDiagram …).",
            path.display(),
            listing
        )));
    }
    std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_diagram_kind() {
        assert_eq!(diagram_kind("graph TD\nA-->B"), DiagramKind::Flowchart);
        assert_eq!(
            diagram_kind("%% шапка\n\nflowchart LR\nA-->B"),
            DiagramKind::Flowchart
        );
        assert_eq!(
            diagram_kind("sequenceDiagram\nA->>B: x"),
            DiagramKind::Sequence
        );
        assert_eq!(diagram_kind("graph\tLR\nA-->B"), DiagramKind::Flowchart);
        assert_eq!(diagram_kind("just text"), DiagramKind::Unknown);
        assert_eq!(diagram_kind(""), DiagramKind::Unknown);
        assert_eq!(diagram_kind("erDiagram\nA ||--o{ B : x"), DiagramKind::Er);
        assert_eq!(
            diagram_kind("%% комментарий\nC4Context\nSystem(a, \"A\")"),
            DiagramKind::C4
        );
        assert_eq!(
            diagram_kind("C4Component\nComponent(a, \"A\")"),
            DiagramKind::C4
        );
        assert_eq!(
            diagram_kind("C4Deployment\nSystem(a, \"A\")"),
            DiagramKind::C4Unsupported
        );
    }

    #[test]
    fn renders_lr_chain_of_three() {
        let art = render("graph LR\nA --> B --> C").unwrap();
        let want = "┌───┐  ┌───┐  ┌───┐\n\
                    │ A │─▶│ B │─▶│ C │\n\
                    └───┘  └───┘  └───┘";
        assert_eq!(art, want);
    }

    #[test]
    fn renders_td_branching() {
        let art = render("graph TD\nA --> B\nA --> C").unwrap();
        let want = "    ┌───┐\n\
                    ␣   │ A │\n\
                    ␣   └───┘\n\
                    ␣ ┌───┴───┐\n\
                    ␣ ▼       ▼\n\
                    ┌───┐   ┌───┐\n\
                    │ B │   │ C │\n\
                    └───┘   └───┘";
        assert_eq!(art, want.replace('␣', " "));
    }

    #[test]
    fn renders_edge_labels_on_lines() {
        let art = render("graph TD\nA[Начало] -- да --> B{ОК?}\nA -- нет --> C[Стоп]").unwrap();
        assert!(art.contains('▼'), "нет стрелок:\n{art}");
        assert!(art.contains("да"), "метка 'да' потерялась:\n{art}");
        assert!(art.contains("нет"), "метка 'нет' потерялась:\n{art}");
        // метки лежат на шине между слоями (4-я строка арта)
        let bus_row = art.lines().nth(3).unwrap_or("");
        assert!(
            bus_row.contains("да") && bus_row.contains("нет"),
            "метки не на шине:\n{art}"
        );
    }

    #[test]
    fn renders_lr_edge_label() {
        let art = render("graph LR\nA -- вызов --> B").unwrap();
        let want = "┌───┐           ┌───┐\n\
                    │ A │──вызов───▶│ B │\n\
                    └───┘           └───┘";
        assert_eq!(art, want);
    }

    #[test]
    fn renders_quoted_label() {
        let art = render("graph LR\nA[\"текст с --> внутри\"] --> B").unwrap();
        assert!(
            art.contains("текст с --> внутри"),
            "quoted-метка потерялась:\n{art}"
        );
        assert!(art.contains('▶'), "ребро не отрисовано:\n{art}");
    }

    #[test]
    fn renders_plain_and_dotted_edges() {
        let art = render("graph LR\nA --- B\nB -.-> C").unwrap();
        assert!(
            art.contains("─│") || art.contains("──"),
            "нет линий:\n{art}"
        );
        assert!(
            art.contains('▶'),
            "пунктир должен рендериться как стрелка:\n{art}"
        );
        assert!(!art.contains('◀'), "направление перепутано:\n{art}");
    }

    #[test]
    fn renders_sequence_with_note() {
        let art = render(
            "sequenceDiagram\nparticipant A as Клиент\nparticipant B as Банк\nA->>B: Запрос\nB-->>A: Ответ\nNote right of B: Проверка",
        )
        .unwrap();
        assert!(art.contains("│ Клиент │"), "нет бокса участника:\n{art}");
        assert!(art.contains("│ Банк │"), "нет бокса участника:\n{art}");
        assert!(art.contains("Запрос"), "нет метки сообщения:\n{art}");
        assert!(art.contains('▶'), "нет стрелки ->>:\n{art}");
        assert!(art.contains('◀'), "нет стрелки -->> назад:\n{art}");
        assert!(art.contains("│ Проверка │"), "нет заметки:\n{art}");
    }

    #[test]
    fn skips_unsupported_constructs_with_warning() {
        let art = render("flowchart LR\nsubgraph x\nA --> B\nend\nstyle A fill:#f9f").unwrap();
        assert!(art.contains("%% пропущено"), "нет предупреждения:\n{art}");
        assert!(art.contains('▶'), "граф не отрисован:\n{art}");
    }

    #[test]
    fn rejects_garbage_without_panic() {
        assert!(render("lorem ipsum dolor").is_err());
        assert!(render("graph TD\nA -->").is_err());
        assert!(render("graph TD\nA --> --> B").is_err());
        assert!(render("graph XX\nA --> B").is_err());
        assert!(render("sequenceDiagram\nA=>B: не та стрелка").is_err());
    }

    #[test]
    fn rejects_empty_input() {
        assert!(render("").is_err());
        assert!(render("%% только комментарий\n\n").is_err());
        let err = render("").unwrap_err();
        assert!(matches!(err, HarnessError::Mermaid(_)));
    }

    #[test]
    fn no_trailing_spaces() {
        let art = render("graph TD\nA --> B\nA --> C\nB --> D(да)\nC --> D").unwrap();
        for line in art.lines() {
            assert_eq!(line, line.trim_end(), "хвостовые пробелы: «{line}»");
        }
    }

    #[test]
    fn tools_not_empty() {
        let ts = tools();
        assert_eq!(ts.len(), 1);
        assert_eq!(ts[0].spec().name, "mermaid_render");
        assert!(ts[0].spec().description.contains("mermaid"));
    }

    #[test]
    fn spec_routes_archify_ir_away_from_mermaid() {
        // Регрессия: просьба «покажи архифай» не должна уводить модель
        // в mermaid — описание обязано маршрутизировать Archify IR
        // в archify_show (показ = HTML в браузере, не ASCII-арт), причём
        // с явным запретом лазейки «структура в чат».
        let d = tools()[0].spec().description;
        assert!(d.contains("archify_show"), "{d}");
        assert!(d.contains("структуры в чат"), "{d}");
        assert!(d.contains("запрещена"), "{d}");
    }

    #[tokio::test]
    async fn tool_renders_code_arg() {
        let ctx = ToolContext::new(
            std::path::PathBuf::from("."),
            Arc::new(crate::config::Config::default()),
        );
        let tool = &tools()[0];
        let out = tool
            .call(serde_json::json!({"code": "graph LR\nA --> B"}), &ctx)
            .await
            .unwrap();
        assert!(!out.is_error, "вызов завершился ошибкой: {}", out.content);
        assert!(out.content.contains('▶'), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_reads_path_arg() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("d.mmd"), "graph TD\nA --> B").unwrap();
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let tool = &tools()[0];
        let out = tool
            .call(serde_json::json!({"path": "d.mmd"}), &ctx)
            .await
            .unwrap();
        assert!(!out.is_error, "вызов завершился ошибкой: {}", out.content);
        assert!(out.content.contains('▼'), "{}", out.content);
    }

    #[tokio::test]
    async fn tool_reports_parser_error_with_line() {
        let ctx = ToolContext::new(
            std::path::PathBuf::from("."),
            Arc::new(crate::config::Config::default()),
        );
        let tool = &tools()[0];
        let out = tool
            .call(serde_json::json!({"code": "graph TD\nA -->\n"}), &ctx)
            .await
            .unwrap();
        assert!(out.is_error);
        assert!(out.content.contains("строка 2"), "{}", out.content);
        // без аргументов — тоже вежливая ошибка
        let out = tool.call(serde_json::json!({}), &ctx).await.unwrap();
        assert!(out.is_error);
    }

    #[test]
    fn read_source_from_directory_lists_diagram_files() {
        let tmp = tempfile::tempdir().expect("tmp");
        std::fs::write(tmp.path().join("flow.mmd"), "flowchart LR\nA-->B\n").expect("mmd");
        std::fs::write(tmp.path().join("notes.txt"), "текст").expect("txt");
        let err = read_diagram_source(tmp.path()).expect_err("каталог — подсказка");
        let msg = err.to_string();
        assert!(msg.contains("каталог"), "{msg}");
        assert!(msg.contains("flow.mmd"), "файл диаграммы подсказан: {msg}");
        assert!(
            !msg.contains("notes.txt"),
            "посторонние файлы скрыты, когда есть диаграммы: {msg}"
        );
        // Обычный файл читается как раньше.
        let code = read_diagram_source(&tmp.path().join("flow.mmd")).expect("файл");
        assert!(code.contains("flowchart"));
    }

    #[test]
    fn read_source_from_directory_without_diagrams_shows_contents() {
        let tmp = tempfile::tempdir().expect("tmp");
        std::fs::write(tmp.path().join("run.sh"), "#!/bin/sh\n").expect("sh");
        let err = read_diagram_source(tmp.path()).expect_err("каталог");
        let msg = err.to_string();
        assert!(msg.contains("нет"), "нет диаграмм: {msg}");
        assert!(msg.contains("run.sh"), "содержимое показано: {msg}");
    }

    #[test]
    fn renders_er_diagram_with_attributes_and_cards() {
        let art = render(
            "erDiagram\n\
             CUSTOMER ||--o{ ORDER : places\n\
             CUSTOMER {\n\
             \x20   string name\n\
             \x20   int id PK\n\
             }\n",
        )
        .unwrap();
        // Рамка сущности: имя, разделитель, атрибуты.
        assert!(art.contains("CUSTOMER"), "{art}");
        assert!(art.contains("├"), "нет разделителя атрибутов:\n{art}");
        assert!(art.contains("string name"), "{art}");
        assert!(art.contains("int id PK"), "{art}");
        assert!(art.contains("ORDER"), "{art}");
        // Кардинальности — текстом в метке ребра.
        assert!(art.contains("places (1:0..*)"), "метка связи:\n{art}");
        for line in art.lines() {
            assert_eq!(line, line.trim_end(), "хвостовые пробелы: «{line}»");
        }
    }

    #[test]
    fn renders_er_without_attributes_and_relations() {
        // Сущности без блоков и связей — не паника, рамки рядом.
        let art = render("erDiagram\nA ||--|| B : one\nC {\n  int x\n}\n").unwrap();
        assert!(art.contains("│ A │"), "{art}");
        assert!(art.contains("│ B │"), "{art}");
        assert!(art.contains("one (1:1)"), "{art}");
        let single = render("erDiagram\nSOLO {\n  int id\n}\n").unwrap();
        assert!(single.contains("SOLO"), "{single}");
        assert!(single.contains("int id"), "{single}");
    }

    #[test]
    fn renders_c4_container_diagram() {
        let art = render(
            "C4Container\n\
             Person(user, \"Пользователь\")\n\
             Container(api, \"API\", \"Rust\")\n\
             Rel(user, api, \"HTTPS\")\n",
        )
        .unwrap();
        assert!(art.contains("«person»"), "{art}");
        assert!(art.contains("Пользователь"), "{art}");
        assert!(art.contains("«container»"), "{art}");
        assert!(art.contains("[Rust]"), "технология в рамке:\n{art}");
        assert!(art.contains('▼'), "стрелка Rel:\n{art}");
        assert!(art.contains("HTTPS"), "метка связи:\n{art}");
        for line in art.lines() {
            assert_eq!(line, line.trim_end(), "хвостовые пробелы: «{line}»");
        }
    }

    #[test]
    fn c4deployment_rejected_with_recipe_hint() {
        let err = render("C4Deployment\nSystem(a, \"A\")\n").unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("не поддерживаются"), "{msg}");
        assert!(msg.contains("flowchart"), "подсказка-рецепт: {msg}");
    }

    #[tokio::test]
    async fn tool_directory_path_returns_hint_to_model() {
        let tmp = tempfile::tempdir().expect("tmp");
        std::fs::write(tmp.path().join("seq.mmd"), "sequenceDiagram\nA->>B: ping\n").expect("mmd");
        let ctx = ToolContext::new(
            tmp.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let out = tools()[0]
            .call(serde_json::json!({"path": "."}), &ctx)
            .await
            .expect("call");
        assert!(out.is_error, "каталог — ошибка-подсказка для модели");
        assert!(out.content.contains("seq.mmd"), "{}", out.content);
    }
}
