//! Обмен моделью архитектуры с отраслевыми форматами (ADR-009).
//!
//! Экспортируемое подмножество модели: сущности `SYS`/`CMP`/`INT` и связи
//! между ними (оба конца — экспортируемые сущности; вид связи сохраняется).
//! Прочие типы (`CAP`/`NFR`/`REQ`/`AD`/`ADR`/`RISK`/`OWNER`) аналога в
//! C4-форматах не имеют и не экспортируются.
//!
//! - Экспорт: Structurizr DSL (`workspace { model { … } }`), `PlantUML`
//!   (`@startuml`, component), drawio (минимально валидный mxfile XML).
//!   Каждый элемент Structurizr-экспорта несёт `properties` со `spine.id`,
//!   `spine.type`, `spine.status`, `spine.date` — носитель точного
//!   round-trip.
//! - Экспорт `ArchiMate` Open Exchange Format 3.2 (ADR-032, только экспорт):
//!   подмножество шире C4 — `SYS`/`CMP` → `ApplicationComponent`,
//!   `INT` → `ApplicationInterface`, `CAP` → `Capability`,
//!   `REQ`/`NFR` → `Requirement`, `AD` → `Principle`; `ADR`/`RISK`/`OWNER`
//!   аналога не имеют и не экспортируются (связи на них пропускаются).
//!   Связи: `depends_on` → `Serving` (источник — цель зависимости: B
//!   обслуживает A при «A `depends_on` B»), `implements` → `Realization`
//!   (источник — реализатор), `affects` → `Influence`, `verified_by` →
//!   `Realization` (только если цель — сущность модели; правило `C-NNN`
//!   пропускается как битая ссылка). Round-trip-носитель — секция
//!   `propertyDefinitions` (`spine.id`/`spine.type`/`spine.status`/
//!   `spine.date`) + per-element `properties` (по аналогии со `spine.*`
//!   в Structurizr-экспорте). XML пишется руками с экранированием —
//!   новых зависимостей нет (прецедент drawio).
//! - Импорт: толерантный парсер подмножества Structurizr DSL → сущности
//!   модели (frontmatter + тело из `description`). Элемент со `spine.id`
//!   восстанавливается точно; чужие элементы получают синтезированные ID
//!   (`SYS-NNN`/`CMP-NNN`/`INT-NNN` в порядке документа); `person`
//!   пропускается с предупреждением. Связь с описанием, совпадающим с именем
//!   вида связи (`depends_on`/`implements`/`affects`/`verified_by`),
//!   восстанавливает вид; прочие — `depends_on`.
//!
//! Ограничения round-trip (ADR-009): кавычки в строках DSL заменяются на
//! `'`, переводы строк тел — на пробелы; связи на неэкспортируемые типы не
//! переживают круг.

use std::collections::{BTreeMap, HashMap};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::control::kebab_slug;
use crate::error::{HarnessError, Result};
use crate::model::parse::{Entity, LinkKind, Model};
use crate::model::{EntityKind, parse_id};

/// Формат экспорта модели.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExportFormat {
    /// Structurizr DSL (`workspace { model { … } }`).
    Structurizr,
    /// `PlantUML` component-диаграмма.
    Plantuml,
    /// drawio mxfile XML.
    Drawio,
    /// `ArchiMate` Open Exchange Format 3.2 (только экспорт, ADR-032).
    Archimate,
}

impl ExportFormat {
    /// Формат по имени из CLI (`structurizr`/`plantuml`/`drawio`/`archimate`).
    #[must_use]
    pub fn from_name(name: &str) -> Option<Self> {
        match name.trim().to_ascii_lowercase().as_str() {
            "structurizr" => Some(Self::Structurizr),
            "plantuml" => Some(Self::Plantuml),
            "drawio" => Some(Self::Drawio),
            "archimate" => Some(Self::Archimate),
            _ => None,
        }
    }

    /// Имена всех форматов (для сообщений об ошибках).
    #[must_use]
    pub fn names() -> &'static str {
        "structurizr, plantuml, drawio, archimate"
    }
}

/// Отчёт импорта Structurizr DSL.
#[derive(Debug)]
pub struct ImportReport {
    /// Каталог модели, куда записаны сущности.
    pub dir: PathBuf,
    /// Записанные файлы (в порядке сущностей документа).
    pub written: Vec<PathBuf>,
    /// Предупреждения (пропущенные элементы/связи чужого DSL).
    pub warnings: Vec<String>,
}

/// Экспортируемый ли тип сущности для C4-форматов (SYS/CMP/INT, ADR-009).
fn is_exportable(kind: EntityKind) -> bool {
    matches!(kind, EntityKind::Sys | EntityKind::Cmp | EntityKind::Int)
}

/// Экспортируемый ли тип сущности для `ArchiMate` (ADR-032): подмножество
/// шире C4 — к SYS/CMP/INT добавляются CAP/REQ/NFR/AD; ADR/RISK/OWNER
/// (и прочие типы) аналога в `ArchiMate`-маппинге не имеют.
fn is_exportable_archimate(kind: EntityKind) -> bool {
    matches!(
        kind,
        EntityKind::Sys
            | EntityKind::Cmp
            | EntityKind::Int
            | EntityKind::Cap
            | EntityKind::Req
            | EntityKind::Nfr
            | EntityKind::Ad
    )
}

/// Экспортируемые сущности модели по предикату формата, без дублей ID.
fn exportable(model: &Model, pred: fn(EntityKind) -> bool) -> Vec<&Entity> {
    let mut seen = Vec::new();
    let mut out = Vec::new();
    for e in &model.entities {
        if pred(e.kind) && !seen.contains(&e.id.as_str()) {
            seen.push(e.id.as_str());
            out.push(e);
        }
    }
    out
}

/// Связи между экспортируемыми сущностями: (источник, вид, цель).
/// Битые ссылки и связи на неэкспортируемые типы не попадают в вывод
/// (для `ArchiMate` это же правило отсекает `verified_by` на правила `C-NNN` —
/// их нет в модели).
fn export_links<'m>(
    model: &'m Model,
    entities: &[&'m Entity],
    pred: fn(EntityKind) -> bool,
) -> Vec<(&'m str, LinkKind, &'m str)> {
    let mut out = Vec::new();
    for e in entities {
        for kind in LinkKind::ALL {
            for target in e.link_targets(kind) {
                let Some(t) = model.get(target) else {
                    continue; // битая ссылка — забота validate
                };
                if pred(t.kind) {
                    out.push((e.id.as_str(), kind, target.as_str()));
                }
            }
        }
    }
    out
}

/// Алиас элемента по ID сущности: `SYS-001` → `sys_001`.
fn alias_of(id: &str) -> String {
    id.to_ascii_lowercase().replace('-', "_")
}

/// Строка для Structurizr DSL: кавычки → `'`, переводы строк → пробелы
/// (у DSL-строк нет escape-спецификации, ADR-009).
fn dsl_str(s: &str) -> String {
    s.replace('"', "'").replace(['\n', '\r'], " ")
}

/// Метка для `PlantUML`: `[`/`]` ломают синтаксис компонента — заменяются.
fn puml_str(s: &str) -> String {
    s.replace('[', "(")
        .replace(']', ")")
        .replace(['\n', '\r'], " ")
}

/// Экранирование значения для XML-атрибута drawio.
fn xml_str(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace(['\n', '\r'], " ")
}

/// Экранирование XML-спецсимволов для текстового содержимого элемента
/// (`ArchiMate` `name`/`documentation`/`value`): переводы строк — допустимый
/// XML-текст, сохраняются (ADR-032).
fn xml_text(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

/// Метка элемента: `ID · заголовок`.
fn label_of(e: &Entity) -> String {
    format!("{} · {}", e.id, e.title)
}

/// Проверяет, что в модели есть экспортируемые сущности.
fn ensure_exportable<'m>(model: &'m Model, entities: &[&'m Entity], kinds: &str) -> Result<()> {
    if entities.is_empty() {
        return Err(HarnessError::Model(format!(
            "в модели {} нет сущностей {kinds} — нечего экспортировать",
            model.dir.display()
        )));
    }
    Ok(())
}

/// Экспортирует модель в заданный формат.
///
/// # Errors
/// В модели нет экспортируемых сущностей (SYS/CMP/INT для C4-форматов;
/// SYS/CMP/INT/CAP/REQ/NFR/AD для `ArchiMate`, ADR-032).
pub fn export_model(model: &Model, format: ExportFormat) -> Result<String> {
    let (pred, kinds): (fn(EntityKind) -> bool, &str) = match format {
        ExportFormat::Archimate => (is_exportable_archimate, "SYS/CMP/INT/CAP/REQ/NFR/AD"),
        _ => (is_exportable, "SYS/CMP/INT"),
    };
    let entities = exportable(model, pred);
    ensure_exportable(model, &entities, kinds)?;
    let links = export_links(model, &entities, pred);
    Ok(match format {
        ExportFormat::Structurizr => export_structurizr(&entities, &links),
        ExportFormat::Plantuml => export_plantuml(&entities, &links),
        ExportFormat::Drawio => export_drawio(&entities, &links),
        ExportFormat::Archimate => export_archimate(&entities, &links),
    })
}

/// Пишет `properties`-блок Structurizr с идентичностью сущности spine.
fn write_spine_properties(out: &mut String, indent: &str, e: &Entity) {
    let _ = writeln!(out, "{indent}properties {{");
    let _ = writeln!(out, "{indent}    \"spine.id\" \"{}\"", e.id);
    let _ = writeln!(out, "{indent}    \"spine.type\" \"{}\"", e.kind.type_str());
    let _ = writeln!(
        out,
        "{indent}    \"spine.status\" \"{}\"",
        dsl_str(&e.status)
    );
    if let Some(date) = &e.date {
        let _ = writeln!(out, "{indent}    \"spine.date\" \"{}\"", dsl_str(date));
    }
    let _ = writeln!(out, "{indent}}}");
}

/// Пишет `description`, если тело сущности непустое.
fn write_description(out: &mut String, indent: &str, e: &Entity) {
    if !e.body.is_empty() {
        let _ = writeln!(out, "{indent}description \"{}\"", dsl_str(&e.body));
    }
}

/// Экспорт в Structurizr DSL (ADR-009).
///
/// `SYS` → `softwareSystem`; `INT` → `softwareSystem` с тегом `External`;
/// `CMP` → `container` внутри единственной системы, если `SYS` ровно одна,
/// иначе — плоский `softwareSystem` (тип сохраняется в `spine.type`).
fn export_structurizr(entities: &[&Entity], links: &[(&str, LinkKind, &str)]) -> String {
    let systems: Vec<&Entity> = entities
        .iter()
        .copied()
        .filter(|e| e.kind == EntityKind::Sys)
        .collect();
    let nest = systems.len() == 1;
    let workspace_name = systems
        .first()
        .map_or("Модель архитектуры", |s| s.title.as_str());
    let mut out = String::new();
    let _ = writeln!(
        out,
        "workspace \"{}\" \"Экспорт из модели arch-ml (SYS/CMP/INT)\" {{",
        dsl_str(workspace_name)
    );
    let _ = writeln!(out);
    let _ = writeln!(out, "    model {{");
    for e in entities {
        let alias = alias_of(&e.id);
        match e.kind {
            EntityKind::Sys => {
                let _ = writeln!(
                    out,
                    "        {alias} = softwareSystem \"{}\" {{",
                    dsl_str(&e.title)
                );
                write_description(&mut out, "            ", e);
                write_spine_properties(&mut out, "            ", e);
                if nest {
                    for c in entities
                        .iter()
                        .filter(|c| c.kind == EntityKind::Cmp)
                        .copied()
                    {
                        let _ = writeln!(out);
                        let _ = writeln!(
                            out,
                            "            {} = container \"{}\" {{",
                            alias_of(&c.id),
                            dsl_str(&c.title)
                        );
                        write_description(&mut out, "                ", c);
                        write_spine_properties(&mut out, "                ", c);
                        let _ = writeln!(out, "            }}");
                    }
                }
                let _ = writeln!(out, "        }}");
            }
            EntityKind::Int => {
                let _ = writeln!(
                    out,
                    "        {alias} = softwareSystem \"{}\" {{",
                    dsl_str(&e.title)
                );
                write_description(&mut out, "            ", e);
                let _ = writeln!(out, "            tags \"External\"");
                write_spine_properties(&mut out, "            ", e);
                let _ = writeln!(out, "        }}");
            }
            EntityKind::Cmp if !nest => {
                let _ = writeln!(
                    out,
                    "        {alias} = softwareSystem \"{}\" {{",
                    dsl_str(&e.title)
                );
                write_description(&mut out, "            ", e);
                write_spine_properties(&mut out, "            ", e);
                let _ = writeln!(out, "        }}");
            }
            _ => {} // CMP при nest — внутри блока системы
        }
    }
    if !links.is_empty() {
        let _ = writeln!(out);
        for (from, kind, to) in links {
            let _ = writeln!(
                out,
                "        {} -> {} \"{}\"",
                alias_of(from),
                alias_of(to),
                kind.field_name()
            );
        }
    }
    let _ = writeln!(out, "    }}");
    let _ = writeln!(out, "}}");
    out
}

/// Экспорт в `PlantUML` component-диаграмму (ADR-009).
fn export_plantuml(entities: &[&Entity], links: &[(&str, LinkKind, &str)]) -> String {
    let systems: Vec<&Entity> = entities
        .iter()
        .copied()
        .filter(|e| e.kind == EntityKind::Sys)
        .collect();
    let nest = systems.len() == 1;
    let mut out = String::from("@startuml\n");
    let _ = writeln!(out, "' Экспорт модели arch-ml (SYS/CMP/INT), ADR-009");
    for e in entities {
        match e.kind {
            EntityKind::Sys if nest => {
                let _ = writeln!(
                    out,
                    "package \"{}\" as {} {{",
                    puml_str(&label_of(e)),
                    alias_of(&e.id)
                );
                for c in entities
                    .iter()
                    .filter(|c| c.kind == EntityKind::Cmp)
                    .copied()
                {
                    let _ = writeln!(out, "  [{}] as {}", puml_str(&label_of(c)), alias_of(&c.id));
                }
                let _ = writeln!(out, "}}");
            }
            EntityKind::Sys => {
                let _ = writeln!(out, "[{}] as {}", puml_str(&label_of(e)), alias_of(&e.id));
            }
            EntityKind::Int => {
                let _ = writeln!(
                    out,
                    "[{}] as {} << External >>",
                    puml_str(&label_of(e)),
                    alias_of(&e.id)
                );
            }
            EntityKind::Cmp if !nest => {
                let _ = writeln!(out, "[{}] as {}", puml_str(&label_of(e)), alias_of(&e.id));
            }
            _ => {}
        }
    }
    for (from, kind, to) in links {
        let _ = writeln!(
            out,
            "{} --> {} : {}",
            alias_of(from),
            alias_of(to),
            kind.field_name()
        );
    }
    out.push_str("@enduml\n");
    out
}

/// Экспорт в drawio mxfile (минимально валидный XML, ADR-009).
///
/// Вершины раскладываются детерминированной сеткой (3 колонки), рёбра —
/// `source`/`target` по алиасам, метка — вид связи.
fn export_drawio(entities: &[&Entity], links: &[(&str, LinkKind, &str)]) -> String {
    /// Число колонок сетки вершин.
    const COLS: usize = 3;
    /// Шаг сетки по X/Y (ширина/высота ячейки + зазор).
    const STEP_X: usize = 300;
    const STEP_Y: usize = 120;
    let mut out = String::new();
    let _ = writeln!(out, "<mxfile host=\"arch-ml\" type=\"device\">");
    let _ = writeln!(out, "  <diagram id=\"model\" name=\"Model\">");
    let _ = writeln!(
        out,
        "    <mxGraphModel dx=\"0\" dy=\"0\" grid=\"1\" gridSize=\"10\" guides=\"1\" \
         tooltips=\"1\" connect=\"1\" arrows=\"1\" fold=\"1\" page=\"1\" pageScale=\"1\" \
         pageWidth=\"1169\" pageHeight=\"827\" math=\"0\" shadow=\"0\">"
    );
    let _ = writeln!(out, "      <root>");
    let _ = writeln!(out, "        <mxCell id=\"0\" />");
    let _ = writeln!(out, "        <mxCell id=\"1\" parent=\"0\" />");
    for (i, e) in entities.iter().enumerate() {
        let style = match e.kind {
            EntityKind::Sys => "rounded=0;whiteSpace=wrap;html=1;",
            EntityKind::Int => "rounded=0;whiteSpace=wrap;html=1;dashed=1;",
            _ => "rounded=1;whiteSpace=wrap;html=1;",
        };
        let x = 40 + (i % COLS) * STEP_X;
        let y = 40 + (i / COLS) * STEP_Y;
        let _ = writeln!(
            out,
            "        <mxCell id=\"{}\" value=\"{}\" style=\"{style}\" vertex=\"1\" parent=\"1\">",
            alias_of(&e.id),
            xml_str(&label_of(e))
        );
        let _ = writeln!(
            out,
            "          <mxGeometry x=\"{x}\" y=\"{y}\" width=\"240\" height=\"80\" as=\"geometry\" />"
        );
        let _ = writeln!(out, "        </mxCell>");
    }
    for (i, (from, kind, to)) in links.iter().enumerate() {
        let _ = writeln!(
            out,
            "        <mxCell id=\"e_{i}\" value=\"{}\" \
             style=\"edgeStyle=orthogonalEdgeStyle;html=1;\" edge=\"1\" parent=\"1\" \
             source=\"{}\" target=\"{}\">",
            kind.field_name(),
            alias_of(from),
            alias_of(to)
        );
        let _ = writeln!(
            out,
            "          <mxGeometry relative=\"1\" as=\"geometry\" />"
        );
        let _ = writeln!(out, "        </mxCell>");
    }
    let _ = writeln!(out, "      </root>");
    let _ = writeln!(out, "    </mxGraphModel>");
    let _ = writeln!(out, "  </diagram>");
    let _ = writeln!(out, "</mxfile>");
    out
}

/// Тип элемента `ArchiMate` (`xsi:type`) по типу сущности (ADR-032):
/// `SYS`/`CMP` → `ApplicationComponent`, `INT` → `ApplicationInterface`,
/// `CAP` → `Capability`, `REQ`/`NFR` → `Requirement`, `AD` → `Principle`.
/// Неэкспортируемые типы сюда не доходят (отсечены предикатом формата).
fn archimate_type(kind: EntityKind) -> &'static str {
    match kind {
        EntityKind::Sys | EntityKind::Cmp => "ApplicationComponent",
        EntityKind::Int => "ApplicationInterface",
        EntityKind::Cap => "Capability",
        EntityKind::Req | EntityKind::Nfr => "Requirement",
        EntityKind::Ad => "Principle",
        _ => unreachable!("неэкспортируемый тип сущности в `ArchiMate`-экспорте"),
    }
}

/// Свойство элемента `ArchiMate` (`spine.*` — носитель round-trip, ADR-032).
fn write_archimate_property(out: &mut String, indent: &str, def_ref: &str, value: &str) {
    let _ = writeln!(
        out,
        "{indent}<property propertyDefinitionRef=\"{def_ref}\">\
         <value>{}</value></property>",
        xml_text(value)
    );
}

/// Экспорт в `ArchiMate` Open Exchange Format 3.2 (только экспорт, ADR-032).
///
/// Идентификаторы элементов — spine-id как есть (детерминированные;
/// XML-спецсимволы экранируются). Тело сущности → `documentation`.
/// Направления связей: `depends_on` A→B → `Serving`(source=B, target=A)
/// (B обслуживает A), `implements`/`verified_by` A→B → `Realization`
/// (source=A, target=B — источник реализует/подтверждается целью),
/// `affects` A→B → `Influence`. Идентификаторы связей — `rel-NNN`
/// в порядке обхода (детерминированные).
fn export_archimate(entities: &[&Entity], links: &[(&str, LinkKind, &str)]) -> String {
    let model_name = entities
        .iter()
        .find(|e| e.kind == EntityKind::Sys)
        .map_or("Модель архитектуры", |e| e.title.as_str());
    let mut out = String::new();
    let _ = writeln!(out, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
    let _ = writeln!(
        out,
        "<model xmlns=\"http://www.opengroup.org/xsd/archimate/3.0/\" \
         xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" \
         identifier=\"spine-be-export\">"
    );
    let _ = writeln!(out, "  <name>{}</name>", xml_text(model_name));
    let _ = writeln!(out, "  <elements>");
    for e in entities {
        let _ = writeln!(
            out,
            "    <element identifier=\"{}\" xsi:type=\"{}\">",
            xml_str(&e.id),
            archimate_type(e.kind)
        );
        let _ = writeln!(out, "      <name>{}</name>", xml_text(&e.title));
        if !e.body.is_empty() {
            let _ = writeln!(
                out,
                "      <documentation>{}</documentation>",
                xml_text(&e.body)
            );
        }
        let _ = writeln!(out, "      <properties>");
        write_archimate_property(&mut out, "        ", "pd-spine-id", &e.id);
        write_archimate_property(&mut out, "        ", "pd-spine-type", e.kind.type_str());
        write_archimate_property(&mut out, "        ", "pd-spine-status", &e.status);
        if let Some(date) = &e.date {
            write_archimate_property(&mut out, "        ", "pd-spine-date", date);
        }
        let _ = writeln!(out, "      </properties>");
        let _ = writeln!(out, "    </element>");
    }
    let _ = writeln!(out, "  </elements>");
    let _ = writeln!(out, "  <relationships>");
    for (i, (from, kind, to)) in links.iter().enumerate() {
        let (xsi_type, source, target) = match kind {
            // «A depends_on B» — B обслуживает A.
            LinkKind::DependsOn => ("Serving", to, from),
            LinkKind::Implements | LinkKind::VerifiedBy => ("Realization", from, to),
            LinkKind::Affects => ("Influence", from, to),
        };
        let _ = writeln!(
            out,
            "    <relationship identifier=\"rel-{i:03}\" source=\"{}\" target=\"{}\" \
             xsi:type=\"{xsi_type}\"/>",
            xml_str(source),
            xml_str(target)
        );
    }
    let _ = writeln!(out, "  </relationships>");
    let _ = writeln!(out, "  <propertyDefinitions>");
    for (ident, name) in [
        ("pd-spine-id", "spine.id"),
        ("pd-spine-type", "spine.type"),
        ("pd-spine-status", "spine.status"),
        ("pd-spine-date", "spine.date"),
    ] {
        let _ = writeln!(
            out,
            "    <propertyDefinition identifier=\"{ident}\" type=\"string\">\
             <name>{name}</name></propertyDefinition>"
        );
    }
    let _ = writeln!(out, "  </propertyDefinitions>");
    let _ = writeln!(out, "</model>");
    out
}

// ===== Импорт Structurizr DSL =====

/// Лексема DSL (номер строки — у идентификаторов и строк).
#[derive(Debug, Clone, PartialEq)]
enum Tok {
    /// Неп quoted-слово (`softwareSystem`, `a`, `->`, `=`).
    Ident(String, usize),
    /// Строка в двойных кавычках (кавычки сняты).
    Str(String, usize),
    /// `{`.
    LBrace,
    /// `}`.
    RBrace,
    /// Перевод строки (конец утверждения).
    Newline,
}

/// Токенизатор DSL: комментарии `//`/`#`, строки в кавычках, скобки,
/// переводы строк как разделители утверждений.
fn tokenize_dsl(text: &str) -> Vec<Tok> {
    let mut toks = Vec::new();
    let mut line = 1usize;
    let mut chars = text.chars().peekable();
    while let Some(&c) = chars.peek() {
        match c {
            '\n' => {
                toks.push(Tok::Newline);
                line += 1;
                chars.next();
            }
            ' ' | '\t' | '\r' => {
                chars.next();
            }
            '#' => {
                for c in chars.by_ref() {
                    if c == '\n' {
                        line += 1;
                        toks.push(Tok::Newline);
                        break;
                    }
                }
            }
            '/' => {
                chars.next();
                if chars.peek() == Some(&'/') {
                    for c in chars.by_ref() {
                        if c == '\n' {
                            line += 1;
                            toks.push(Tok::Newline);
                            break;
                        }
                    }
                } else {
                    toks.push(Tok::Ident("/".to_owned(), line));
                }
            }
            '"' => {
                chars.next();
                let mut s = String::new();
                for c in chars.by_ref() {
                    if c == '"' {
                        break;
                    }
                    if c == '\n' {
                        line += 1;
                    }
                    s.push(c);
                }
                toks.push(Tok::Str(s, line));
            }
            '{' => {
                toks.push(Tok::LBrace);
                chars.next();
            }
            '}' => {
                toks.push(Tok::RBrace);
                chars.next();
            }
            _ => {
                let mut word = String::new();
                while let Some(&c) = chars.peek() {
                    if c.is_whitespace() || matches!(c, '{' | '}' | '"' | '#') {
                        break;
                    }
                    word.push(c);
                    chars.next();
                }
                toks.push(Tok::Ident(word, line));
            }
        }
    }
    toks
}

/// Элемент модели Structurizr (подмножество).
#[derive(Debug)]
struct StElement {
    /// Алиас (`a = softwareSystem …`), если задан.
    alias: Option<String>,
    /// Ключевое слово (`softwareSystem`, `container`, `component`, `person`).
    keyword: String,
    /// Имя (первый позиционный аргумент).
    name: String,
    /// Описание (второй позиционный аргумент или `description` в блоке).
    description: Option<String>,
    /// Теги (позиционные или `tags "…"` в блоке).
    tags: Vec<String>,
    /// Свойства (`properties { "k" "v" }`).
    properties: BTreeMap<String, String>,
    /// Номер строки объявления (для предупреждений).
    line: usize,
}

/// Связь `a -> b "описание"` (алиасы — как в документе).
#[derive(Debug)]
struct StRelation {
    from: String,
    to: String,
    description: String,
    line: usize,
}

/// Разобранная модель Structurizr + предупреждения о пропущенном.
#[derive(Debug, Default)]
struct StModel {
    elements: Vec<StElement>,
    relations: Vec<StRelation>,
    warnings: Vec<String>,
}

/// Курсор по лексемам.
struct Tokens {
    toks: Vec<Tok>,
    pos: usize,
}

impl Tokens {
    fn peek(&self) -> Option<&Tok> {
        self.toks.get(self.pos)
    }

    fn next(&mut self) -> Option<Tok> {
        let t = self.toks.get(self.pos).cloned();
        if t.is_some() {
            self.pos += 1;
        }
        t
    }

    fn skip_newlines(&mut self) {
        while matches!(self.peek(), Some(Tok::Newline)) {
            self.pos += 1;
        }
    }

    /// Пропускает лексемы до конца строки (включая Newline).
    fn skip_to_eol(&mut self) {
        while let Some(t) = self.next() {
            if matches!(t, Tok::Newline) {
                break;
            }
        }
    }

    /// Пропускает утверждение до конца строки; блок `{ … }`, начатый на той
    /// же строке, пропускается по балансу скобок.
    fn skip_stmt(&mut self) -> Result<()> {
        loop {
            match self.next() {
                Some(Tok::LBrace) => self.skip_block()?,
                Some(Tok::Newline) | None => return Ok(()),
                Some(_) => {}
            }
        }
    }

    /// Пропускает сбалансированный блок `{ … }` (открывающая скобка уже съедена).
    fn skip_block(&mut self) -> Result<()> {
        let mut depth = 1usize;
        while let Some(t) = self.next() {
            match t {
                Tok::LBrace => depth += 1,
                Tok::RBrace => {
                    depth -= 1;
                    if depth == 0 {
                        return Ok(());
                    }
                }
                _ => {}
            }
        }
        Err(HarnessError::Model(
            "structurizr: несбалансированные фигурные скобки (не хватает '}')".into(),
        ))
    }
}

/// Ключевые слова элементов, разбираемые в модель (ADR-009).
fn is_element_keyword(kw: &str) -> bool {
    matches!(kw, "softwareSystem" | "container" | "component" | "person")
}

/// Разбирает позиционные строки вызова элемента до конца строки или `{`.
fn read_positional_strings(ts: &mut Tokens) -> Vec<String> {
    let mut strings = Vec::new();
    while let Some(Tok::Str(_, _)) = ts.peek() {
        if let Some(Tok::Str(s, _)) = ts.next() {
            strings.push(s);
        }
    }
    strings
}

/// Разбирает блок `properties { "k" "v" … }` (открывающая скобка съедена).
fn read_properties(ts: &mut Tokens) -> Result<BTreeMap<String, String>> {
    let mut props = BTreeMap::new();
    loop {
        ts.skip_newlines();
        match ts.next() {
            Some(Tok::RBrace) => return Ok(props),
            Some(Tok::Str(k, _)) => {
                if let Some(Tok::Str(v, _)) = ts.next() {
                    props.insert(k, v);
                }
            }
            Some(_) => {}
            None => {
                return Err(HarnessError::Model(
                    "structurizr: незакрытый блок properties".into(),
                ));
            }
        }
    }
}

/// Разбирает утверждение элемента (`[alias =] keyword "name" … [{ … }]`).
///
/// `group` прозрачен: содержимое блока разбирается в той же области.
/// Элемент без имени пропускается с предупреждением.
fn parse_element_stmt(
    ts: &mut Tokens,
    alias: Option<String>,
    keyword: &str,
    line: usize,
    out: &mut StModel,
) -> Result<()> {
    let strings = read_positional_strings(ts);
    let has_block = if matches!(ts.peek(), Some(Tok::LBrace)) {
        ts.next();
        true
    } else {
        ts.skip_to_eol();
        false
    };
    if keyword == "group" {
        if has_block {
            parse_statements(ts, None, true, out)?;
        }
        return Ok(());
    }
    let Some(name) = strings.first().cloned() else {
        out.warnings.push(format!(
            "строка {line}: элемент '{keyword}' без имени — пропущен"
        ));
        if has_block {
            ts.skip_block()?;
        }
        return Ok(());
    };
    // Позиционные теги: softwareSystem/person — с 3-й строки, у контейнеров
    // 3-я — технология (не нужна), теги — с 4-й.
    let tags_from = if matches!(keyword, "container" | "component") {
        3
    } else {
        2
    };
    let mut tags: Vec<String> = Vec::new();
    for raw in strings.get(tags_from..).unwrap_or(&[]) {
        for tag in raw.split(',') {
            let tag = tag.trim();
            if !tag.is_empty() {
                tags.push(tag.to_owned());
            }
        }
    }
    out.elements.push(StElement {
        alias,
        keyword: keyword.to_owned(),
        name,
        description: strings.get(1).cloned(),
        tags,
        properties: BTreeMap::new(),
        line,
    });
    if has_block {
        parse_statements(ts, Some(out.elements.len() - 1), true, out)?;
    }
    Ok(())
}

/// Разбирает утверждения до `}` (внутри блока) или EOF (верхний уровень).
///
/// `current` — индекс элемента, чей блок разбирается (приёмник
/// `properties`/`tags`/`description`); `in_block` — ожидать ли закрывающую
/// скобку (ложь только на верхнем уровне файла).
fn parse_statements(
    ts: &mut Tokens,
    current: Option<usize>,
    in_block: bool,
    out: &mut StModel,
) -> Result<()> {
    loop {
        ts.skip_newlines();
        let Some(tok) = ts.next() else {
            if in_block {
                return Err(HarnessError::Model(
                    "structurizr: несбалансированные фигурные скобки (не хватает '}')".into(),
                ));
            }
            return Ok(());
        };
        match tok {
            Tok::RBrace => return Ok(()),
            Tok::LBrace => ts.skip_block()?,
            Tok::Newline => {} // пропуски уже сняты skip_newlines; страховка
            Tok::Str(_, line) => {
                out.warnings
                    .push(format!("строка {line}: пропущена строка вне утверждения"));
            }
            Tok::Ident(word, line) => {
                // Присвоение алиаса: `a = keyword …`.
                if matches!(ts.peek(), Some(Tok::Ident(w, _)) if w == "=") {
                    ts.next();
                    ts.skip_newlines();
                    let Some(Tok::Ident(keyword, kw_line)) = ts.next() else {
                        out.warnings.push(format!(
                            "строка {line}: после '{word} =' ожидалось ключевое слово"
                        ));
                        ts.skip_to_eol();
                        continue;
                    };
                    if is_element_keyword(&keyword) || keyword == "group" {
                        parse_element_stmt(ts, Some(word), &keyword, kw_line, out)?;
                    } else {
                        // `views { item = systemContext … }` и т.п. — пропускаем.
                        ts.skip_stmt()?;
                    }
                    continue;
                }
                // Связь: `a -> b ["описание"] [{ … }]`.
                if matches!(ts.peek(), Some(Tok::Ident(w, _)) if w == "->") {
                    ts.next();
                    let Some(Tok::Ident(target, _)) = ts.next() else {
                        out.warnings.push(format!(
                            "строка {line}: связь '{word} ->' без цели — пропущена"
                        ));
                        ts.skip_to_eol();
                        continue;
                    };
                    let strings = read_positional_strings(ts);
                    if matches!(ts.peek(), Some(Tok::LBrace)) {
                        ts.next();
                        ts.skip_block()?;
                    } else {
                        ts.skip_to_eol();
                    }
                    out.relations.push(StRelation {
                        from: word,
                        to: target,
                        description: strings.first().cloned().unwrap_or_default(),
                        line,
                    });
                    continue;
                }
                // Ключевые слова.
                match word.as_str() {
                    "model" | "workspace" => {
                        // Позиционные строки workspace (имя/описание) пропускаем.
                        let _ = read_positional_strings(ts);
                        if matches!(ts.peek(), Some(Tok::LBrace)) {
                            ts.next();
                            parse_statements(ts, current, true, out)?;
                        } else {
                            ts.skip_to_eol();
                        }
                    }
                    kw if is_element_keyword(kw) || kw == "group" => {
                        parse_element_stmt(ts, None, kw, line, out)?;
                    }
                    "properties" => {
                        if matches!(ts.peek(), Some(Tok::LBrace)) {
                            ts.next();
                            let props = read_properties(ts)?;
                            if let Some(i) = current {
                                if let Some(el) = out.elements.get_mut(i) {
                                    el.properties.extend(props);
                                }
                            }
                        } else {
                            ts.skip_to_eol();
                        }
                    }
                    "tags" => {
                        let mut tags: Vec<String> = Vec::new();
                        loop {
                            match ts.peek() {
                                Some(Tok::Str(_, _)) => {
                                    if let Some(Tok::Str(s, _)) = ts.next() {
                                        tags.push(s);
                                    }
                                }
                                Some(Tok::Newline) | None => break,
                                Some(_) => {
                                    ts.next();
                                }
                            }
                        }
                        if let Some(i) = current {
                            if let Some(el) = out.elements.get_mut(i) {
                                el.tags.extend(tags);
                            }
                        }
                    }
                    "description" => {
                        if let Some(Tok::Str(s, _)) = ts.next() {
                            if let Some(i) = current {
                                if let Some(el) = out.elements.get_mut(i) {
                                    el.description.get_or_insert(s);
                                }
                            }
                        }
                        ts.skip_to_eol();
                    }
                    // Заведомо ненужное: метаданные элемента, views, стили.
                    "technology" | "url" | "docs" | "documentation" | "perspectives"
                    | "instances" | "healthCheck" => {
                        ts.skip_stmt()?;
                    }
                    _ => {
                        // Прочее (views, configuration, styles, !identifiers, …):
                        // пропускаем строку; блок — по балансу скобок.
                        ts.skip_stmt()?;
                    }
                }
            }
        }
    }
}

/// Разбирает текст Structurizr DSL в [`StModel`].
///
/// # Errors
/// Несбалансированные фигурные скобки. Битые отдельные утверждения —
/// не ошибка, а предупреждение в `warnings`.
fn parse_structurizr(text: &str) -> Result<StModel> {
    let mut ts = Tokens {
        toks: tokenize_dsl(text),
        pos: 0,
    };
    let mut out = StModel::default();
    parse_statements(&mut ts, None, false, &mut out)?;
    Ok(out)
}

/// Черновик сущности модели, собранной из Structurizr-элемента.
struct Draft {
    id: String,
    kind: EntityKind,
    title: String,
    status: String,
    date: Option<String>,
    body: String,
    depends_on: Vec<String>,
    implements: Vec<String>,
    affects: Vec<String>,
    verified_by: Vec<String>,
}

/// Тип сущности по элементу: свойства `spine.*` в приоритете, иначе —
/// ключевое слово и тег `External` (ADR-009). `None` — элемент вне
/// подмножества обмена (`person`).
fn draft_kind(el: &StElement) -> Option<EntityKind> {
    if let Some(t) = el.properties.get("spine.type") {
        return EntityKind::from_type_str(t);
    }
    match el.keyword.as_str() {
        "container" | "component" => Some(EntityKind::Cmp),
        "softwareSystem" => {
            if el.tags.iter().any(|t| t.eq_ignore_ascii_case("external")) {
                Some(EntityKind::Int)
            } else {
                Some(EntityKind::Sys)
            }
        }
        _ => None,
    }
}

/// Собирает черновики сущностей из разобранной модели Structurizr.
///
/// Возвращает (черновики, отображение алиас → индекс черновика, предупреждения
/// об элементах вне подмножества обмена).
fn drafts_from(st: &StModel) -> (Vec<Draft>, HashMap<&str, usize>, Vec<String>) {
    let mut drafts: Vec<Draft> = Vec::new();
    let mut by_alias: HashMap<&str, usize> = HashMap::new();
    let mut counters: BTreeMap<EntityKind, u64> = BTreeMap::new();
    let mut warnings = Vec::new();
    for el in &st.elements {
        let Some(kind) = draft_kind(el) else {
            warnings.push(format!(
                "строка {}: элемент '{}' ({}) — вне подмножества обмена SYS/CMP/INT, пропущен",
                el.line, el.name, el.keyword
            ));
            continue;
        };
        // ID: spine.id в приоритете; невалидный/отсутствующий — синтез.
        let mut id = el.properties.get("spine.id").cloned().unwrap_or_default();
        if parse_id(&id).is_none() || drafts.iter().any(|d| d.id == id) {
            loop {
                let n = counters.entry(kind).or_insert(0);
                *n += 1;
                id = format!("{}-{n:03}", kind.prefix());
                if !drafts.iter().any(|d| d.id == id) {
                    break;
                }
            }
        }
        let idx = drafts.len();
        if let Some(alias) = &el.alias {
            by_alias.insert(alias.as_str(), idx);
        }
        drafts.push(Draft {
            id,
            kind,
            title: el.name.clone(),
            status: el
                .properties
                .get("spine.status")
                .cloned()
                .unwrap_or_else(|| "imported".to_owned()),
            date: el.properties.get("spine.date").cloned(),
            body: el.description.clone().unwrap_or_default(),
            depends_on: Vec::new(),
            implements: Vec::new(),
            affects: Vec::new(),
            verified_by: Vec::new(),
        });
    }
    (drafts, by_alias, warnings)
}

/// Накладывает связи Structurizr на черновики (с предупреждениями о битых).
fn apply_relations(
    st: &StModel,
    drafts: &mut [Draft],
    by_alias: &HashMap<&str, usize>,
) -> Vec<String> {
    let mut warnings = Vec::new();
    for rel in &st.relations {
        let (Some(&from), Some(&to)) = (
            by_alias.get(rel.from.as_str()),
            by_alias.get(rel.to.as_str()),
        ) else {
            warnings.push(format!(
                "строка {}: связь '{}' -> '{}' — неизвестный алиас, пропущена",
                rel.line, rel.from, rel.to
            ));
            continue;
        };
        let kind = LinkKind::ALL
            .into_iter()
            .find(|k| k.field_name() == rel.description)
            .unwrap_or(LinkKind::DependsOn);
        let target = drafts[to].id.clone();
        let links = match kind {
            LinkKind::DependsOn => &mut drafts[from].depends_on,
            LinkKind::Implements => &mut drafts[from].implements,
            LinkKind::Affects => &mut drafts[from].affects,
            LinkKind::VerifiedBy => &mut drafts[from].verified_by,
        };
        if !links.contains(&target) {
            links.push(target);
        }
    }
    warnings
}

/// Frontmatter записываемой сущности (сериализация в YAML, порядок полей —
/// как в объявлении структуры).
#[derive(Serialize)]
struct OutFrontmatter<'a> {
    id: &'a str,
    #[serde(rename = "type")]
    kind: &'a str,
    title: &'a str,
    status: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    date: Option<&'a str>,
    #[serde(skip_serializing_if = "<[String]>::is_empty")]
    depends_on: &'a [String],
    #[serde(skip_serializing_if = "<[String]>::is_empty")]
    implements: &'a [String],
    #[serde(skip_serializing_if = "<[String]>::is_empty")]
    affects: &'a [String],
    #[serde(skip_serializing_if = "<[String]>::is_empty")]
    verified_by: &'a [String],
}

/// Текст файла сущности: frontmatter + тело.
fn render_entity_file(d: &Draft) -> Result<String> {
    let fm = OutFrontmatter {
        id: &d.id,
        kind: d.kind.type_str(),
        title: &d.title,
        status: &d.status,
        date: d.date.as_deref(),
        depends_on: &d.depends_on,
        implements: &d.implements,
        affects: &d.affects,
        verified_by: &d.verified_by,
    };
    let yaml = serde_yaml_ng::to_string(&fm)
        .map_err(|e| HarnessError::Model(format!("сериализация frontmatter {}: {e}", d.id)))?;
    let mut out = format!("---\n{yaml}---\n");
    if !d.body.is_empty() {
        let _ = write!(out, "\n{}\n", d.body);
    }
    Ok(out)
}

/// Импортирует Structurizr DSL из файла `file` в каталог модели `dir`:
/// пишет по одному `ID-slug.md` на сущность (ADR-009).
///
/// Существующие файлы не затираются: коллизия имени — ошибка. Предупреждения
/// о пропущенных элементах/связях чужого DSL — в [`ImportReport::warnings`].
///
/// # Errors
/// Файл не читается, DSL не разбирается (несбалансированные скобки),
/// каталог не создаётся, файл с таким именем уже существует.
pub fn import_structurizr(file: &Path, dir: &Path) -> Result<ImportReport> {
    let text = std::fs::read_to_string(file).map_err(|e| HarnessError::io(file, e))?;
    let st = parse_structurizr(&text)?;
    let (mut drafts, by_alias, mut warnings) = drafts_from(&st);
    warnings.extend(st.warnings.iter().cloned());
    warnings.extend(apply_relations(&st, &mut drafts, &by_alias));
    if drafts.is_empty() {
        return Err(HarnessError::Model(format!(
            "{}: не найдено ни одного элемента softwareSystem/container/component — \
             нечего импортировать",
            file.display()
        )));
    }
    std::fs::create_dir_all(dir).map_err(|e| HarnessError::io(dir, e))?;
    let mut written = Vec::with_capacity(drafts.len());
    for d in &drafts {
        let name = format!("{}-{}.md", d.id, kebab_slug(&d.title));
        let path = dir.join(name);
        if path.exists() {
            return Err(HarnessError::Model(format!(
                "{} уже существует — импорт не затирает файлы",
                path.display()
            )));
        }
        std::fs::write(&path, render_entity_file(d)?).map_err(|e| HarnessError::io(&path, e))?;
        written.push(path);
    }
    Ok(ImportReport {
        dir: dir.to_path_buf(),
        written,
        warnings,
    })
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;
    use std::path::Path;

    use super::*;
    use crate::model::parse::load_model;

    /// Пишет сущность модели с минимальным frontmatter.
    fn write_entity(dir: &Path, name: &str, frontmatter: &str, body: &str) {
        std::fs::write(dir.join(name), format!("---\n{frontmatter}---\n\n{body}\n"))
            .expect("фикстура");
    }

    /// Фикстурная модель: одна SYS, два CMP, один INT + связи двух видов.
    fn fixture_model(dir: &Path) {
        write_entity(
            dir,
            "SYS-001-platforma.md",
            "id: SYS-001\ntype: sys\ntitle: Платформа\nstatus: designed\ndate: 2026-08-17\n",
            "Центральная система.",
        );
        write_entity(
            dir,
            "CMP-001-gateway.md",
            "id: CMP-001\ntype: cmp\ntitle: Gateway\nstatus: designed\ndepends_on: [CMP-002]\nimplements: [AD-1]\n",
            "Точка входа.",
        );
        write_entity(
            dir,
            "CMP-002-ledger.md",
            "id: CMP-002\ntype: cmp\ntitle: Ledger\nstatus: designed\ndepends_on: [INT-001]\n",
            "Проводки.",
        );
        write_entity(
            dir,
            "INT-001-rail.md",
            "id: INT-001\ntype: int\ntitle: Карточный рельс\nstatus: accepted\n",
            "Внешний рельс.",
        );
        // Не экспортируется: тип вне SYS/CMP/INT.
        write_entity(
            dir,
            "AD-1-invariant.md",
            "id: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\n",
            "Правило.",
        );
    }

    /// Тройки связей (источник, вид, цель) — только между экспортируемыми.
    fn link_triples(model: &Model) -> BTreeSet<(String, String, String)> {
        let mut out = BTreeSet::new();
        for e in &model.entities {
            if !is_exportable(e.kind) {
                continue;
            }
            for kind in LinkKind::ALL {
                for t in e.link_targets(kind) {
                    if model.get(t).is_some_and(|x| is_exportable(x.kind)) {
                        out.insert((e.id.clone(), kind.field_name().to_owned(), t.clone()));
                    }
                }
            }
        }
        out
    }

    #[test]
    fn export_format_from_name() {
        assert_eq!(
            ExportFormat::from_name("structurizr"),
            Some(ExportFormat::Structurizr)
        );
        assert_eq!(
            ExportFormat::from_name("PlantUML"),
            Some(ExportFormat::Plantuml)
        );
        assert_eq!(
            ExportFormat::from_name("drawio"),
            Some(ExportFormat::Drawio)
        );
        assert_eq!(
            ExportFormat::from_name("archimate"),
            Some(ExportFormat::Archimate)
        );
        assert_eq!(
            ExportFormat::from_name("ArchiMate"),
            Some(ExportFormat::Archimate)
        );
        assert_eq!(ExportFormat::from_name("yaml"), None);
        assert!(ExportFormat::names().contains("archimate"));
    }

    #[test]
    fn export_structurizr_golden_substrings() {
        let dir = tempfile::tempdir().expect("tmp");
        fixture_model(dir.path());
        let model = load_model(dir.path()).expect("модель");
        let dsl = export_model(&model, ExportFormat::Structurizr).expect("экспорт");
        for needle in [
            "workspace \"Платформа\"",
            "sys_001 = softwareSystem \"Платформа\" {",
            "cmp_001 = container \"Gateway\" {",
            "int_001 = softwareSystem \"Карточный рельс\" {",
            "tags \"External\"",
            "\"spine.id\" \"CMP-001\"",
            "\"spine.type\" \"int\"",
            "\"spine.status\" \"designed\"",
            "\"spine.date\" \"2026-08-17\"",
            "description \"Точка входа.\"",
            "cmp_001 -> cmp_002 \"depends_on\"",
            "cmp_002 -> int_001 \"depends_on\"",
        ] {
            assert!(dsl.contains(needle), "нет '{needle}':\n{dsl}");
        }
        // AD-1 не экспортируется, связь на него — тоже.
        assert!(!dsl.contains("AD-1"), "{dsl}");
        assert!(!dsl.contains("implements"), "{dsl}");
    }

    #[test]
    fn export_structurizr_flat_when_no_single_system() {
        let dir = tempfile::tempdir().expect("tmp");
        write_entity(
            dir.path(),
            "CMP-001-a.md",
            "id: CMP-001\ntype: cmp\ntitle: Alpha\nstatus: s\n",
            "",
        );
        write_entity(
            dir.path(),
            "CMP-002-b.md",
            "id: CMP-002\ntype: cmp\ntitle: Beta\nstatus: s\n",
            "",
        );
        let model = load_model(dir.path()).expect("модель");
        let dsl = export_model(&model, ExportFormat::Structurizr).expect("экспорт");
        // Без единственной SYS — плоские softwareSystem с spine.type=cmp.
        assert!(dsl.contains("cmp_001 = softwareSystem \"Alpha\""), "{dsl}");
        assert!(dsl.contains("\"spine.type\" \"cmp\""), "{dsl}");
        assert!(!dsl.contains("container"), "{dsl}");
    }

    #[test]
    fn export_plantuml_golden_substrings() {
        let dir = tempfile::tempdir().expect("tmp");
        fixture_model(dir.path());
        let model = load_model(dir.path()).expect("модель");
        let puml = export_model(&model, ExportFormat::Plantuml).expect("экспорт");
        assert!(puml.starts_with("@startuml\n"), "{puml}");
        assert!(puml.ends_with("@enduml\n"), "{puml}");
        for needle in [
            "package \"SYS-001 · Платформа\" as sys_001 {",
            "[CMP-001 · Gateway] as cmp_001",
            "[INT-001 · Карточный рельс] as int_001 << External >>",
            "cmp_001 --> cmp_002 : depends_on",
            "cmp_002 --> int_001 : depends_on",
        ] {
            assert!(puml.contains(needle), "нет '{needle}':\n{puml}");
        }
        assert!(!puml.contains("AD-1"), "{puml}");
    }

    #[test]
    fn export_drawio_golden_substrings_and_escaping() {
        let dir = tempfile::tempdir().expect("tmp");
        write_entity(
            dir.path(),
            "SYS-001-s.md",
            "id: SYS-001\ntype: sys\ntitle: A & B <Core> \" quoted\"\nstatus: s\n",
            "",
        );
        write_entity(
            dir.path(),
            "CMP-001-c.md",
            "id: CMP-001\ntype: cmp\ntitle: Компонент\nstatus: s\ndepends_on: [SYS-001]\n",
            "",
        );
        let model = load_model(dir.path()).expect("модель");
        let xml = export_model(&model, ExportFormat::Drawio).expect("экспорт");
        for needle in [
            "<mxfile host=\"arch-ml\"",
            "<mxGraphModel",
            "<mxCell id=\"0\" />",
            "<mxCell id=\"1\" parent=\"0\" />",
            "id=\"sys_001\" value=\"SYS-001 · A &amp; B &lt;Core&gt; &quot; quoted&quot;\"",
            "vertex=\"1\" parent=\"1\"",
            "edge=\"1\" parent=\"1\" source=\"cmp_001\" target=\"sys_001\"",
            "value=\"depends_on\"",
            "</mxfile>",
        ] {
            assert!(xml.contains(needle), "нет '{needle}':\n{xml}");
        }
        assert!(!xml.contains("A & B"), "не экранировано:\n{xml}");
    }

    /// Фикстурная модель для `ArchiMate`: все экспортируемые типы + связи
    /// всех четырёх видов + неэкспортируемые ADR/RISK/OWNER и правило C-001.
    fn fixture_model_archimate(dir: &Path) {
        write_entity(
            dir,
            "SYS-001-platforma.md",
            "id: SYS-001\ntype: sys\ntitle: Платформа\nstatus: designed\ndate: 2026-09-04\n",
            "Центральная система.",
        );
        write_entity(
            dir,
            "CMP-001-gateway.md",
            "id: CMP-001\ntype: cmp\ntitle: Gateway\nstatus: designed\n\
             depends_on: [CMP-002]\nimplements: [REQ-001, AD-1]\n\
             affects: [CAP-001]\nverified_by: [NFR-001, C-001]\n",
            "Точка входа.",
        );
        write_entity(
            dir,
            "CMP-002-ledger.md",
            "id: CMP-002\ntype: cmp\ntitle: Ledger\nstatus: designed\ndepends_on: [INT-001, ADR-001]\n",
            "Проводки.",
        );
        write_entity(
            dir,
            "INT-001-rail.md",
            "id: INT-001\ntype: int\ntitle: Карточный рельс\nstatus: accepted\n",
            "Внешний рельс.",
        );
        write_entity(
            dir,
            "CAP-001-acquiring.md",
            "id: CAP-001\ntype: cap\ntitle: Эквайринг\nstatus: designed\n",
            "",
        );
        write_entity(
            dir,
            "REQ-001-report.md",
            "id: REQ-001\ntype: req\ntitle: Отчётность\nstatus: draft\n",
            "",
        );
        write_entity(
            dir,
            "NFR-001-availability.md",
            "id: NFR-001\ntype: nfr\ntitle: Доступность\nstatus: approved\n",
            "",
        );
        write_entity(
            dir,
            "AD-1-invariant.md",
            "id: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\n",
            "Правило.",
        );
        // Не экспортируются в `ArchiMate` (нет аналога в маппинге, ADR-032).
        write_entity(
            dir,
            "ADR-001-decision.md",
            "id: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted\n",
            "",
        );
        write_entity(
            dir,
            "RISK-001-risk.md",
            "id: RISK-001\ntype: risk\ntitle: Риск\nstatus: open\n",
            "",
        );
        write_entity(
            dir,
            "OWNER-001-team.md",
            "id: OWNER-001\ntype: owner\ntitle: Команда\nstatus: active\n",
            "",
        );
    }

    #[test]
    fn export_archimate_golden_substrings() {
        let dir = tempfile::tempdir().expect("tmp");
        fixture_model_archimate(dir.path());
        let model = load_model(dir.path()).expect("модель");
        let xml = export_model(&model, ExportFormat::Archimate).expect("экспорт");
        for needle in [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<model xmlns=\"http://www.opengroup.org/xsd/archimate/3.0/\"",
            "identifier=\"spine-be-export\"",
            "<name>Платформа</name>",
            // Маппинг типов (ADR-032).
            "<element identifier=\"SYS-001\" xsi:type=\"ApplicationComponent\">",
            "<element identifier=\"CMP-001\" xsi:type=\"ApplicationComponent\">",
            "<element identifier=\"INT-001\" xsi:type=\"ApplicationInterface\">",
            "<element identifier=\"CAP-001\" xsi:type=\"Capability\">",
            "<element identifier=\"REQ-001\" xsi:type=\"Requirement\">",
            "<element identifier=\"NFR-001\" xsi:type=\"Requirement\">",
            "<element identifier=\"AD-1\" xsi:type=\"Principle\">",
            "<documentation>Точка входа.</documentation>",
            // depends_on → Serving: источник — цель зависимости (B обслуживает A).
            "source=\"CMP-002\" target=\"CMP-001\" xsi:type=\"Serving\"",
            "source=\"INT-001\" target=\"CMP-002\" xsi:type=\"Serving\"",
            // implements/verified_by → Realization, affects → Influence.
            "source=\"CMP-001\" target=\"REQ-001\" xsi:type=\"Realization\"",
            "source=\"CMP-001\" target=\"AD-1\" xsi:type=\"Realization\"",
            "source=\"CMP-001\" target=\"NFR-001\" xsi:type=\"Realization\"",
            "source=\"CMP-001\" target=\"CAP-001\" xsi:type=\"Influence\"",
            // Round-trip-носитель: propertyDefinitions + per-element properties.
            "<propertyDefinition identifier=\"pd-spine-id\" type=\"string\">\
             <name>spine.id</name></propertyDefinition>",
            "<propertyDefinition identifier=\"pd-spine-date\" type=\"string\">\
             <name>spine.date</name></propertyDefinition>",
            "<property propertyDefinitionRef=\"pd-spine-id\"><value>CMP-001</value></property>",
            "<property propertyDefinitionRef=\"pd-spine-type\"><value>cmp</value></property>",
            "<property propertyDefinitionRef=\"pd-spine-status\">\
             <value>designed</value></property>",
            "<property propertyDefinitionRef=\"pd-spine-date\">\
             <value>2026-09-04</value></property>",
        ] {
            assert!(xml.contains(needle), "нет '{needle}':\n{xml}");
        }
        // ADR/RISK/OWNER не экспортируются; связи на них и на правило C-001 —
        // тоже (нет аналога / цель не сущность модели).
        for gone in ["ADR-001", "RISK-001", "OWNER-001", "C-001"] {
            assert!(
                !xml.contains(gone),
                "{gone} не должен попадать в вывод:\n{xml}"
            );
        }
    }

    #[test]
    fn export_archimate_escapes_xml_and_keeps_newlines() {
        let dir = tempfile::tempdir().expect("tmp");
        write_entity(
            dir.path(),
            "CMP-001-c.md",
            "id: CMP-001\ntype: cmp\ntitle: A & B <Core> \" quoted\" 'tick'\nstatus: s\n",
            "строка 1 & <тег>\nстрока 2",
        );
        let model = load_model(dir.path()).expect("модель");
        let xml = export_model(&model, ExportFormat::Archimate).expect("экспорт");
        for needle in [
            "<name>A &amp; B &lt;Core&gt; &quot; quoted&quot; &apos;tick&apos;</name>",
            "<documentation>строка 1 &amp; &lt;тег&gt;\nстрока 2</documentation>",
        ] {
            assert!(xml.contains(needle), "нет '{needle}':\n{xml}");
        }
        assert!(!xml.contains("A & B"), "не экранировано:\n{xml}");
    }

    #[test]
    fn export_empty_subset_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        write_entity(
            dir.path(),
            "AD-1.md",
            "id: AD-1\ntype: ad\ntitle: Инвариант\nstatus: s\n",
            "",
        );
        let model = load_model(dir.path()).expect("модель");
        for format in [
            ExportFormat::Structurizr,
            ExportFormat::Plantuml,
            ExportFormat::Drawio,
        ] {
            let err = export_model(&model, format).expect_err("пустое подмножество");
            assert!(err.to_string().contains("нечего экспортировать"), "{err}");
        }
        // AD экспортируется в `ArchiMate` (→ Principle) — это не пустое подмножество.
        export_model(&model, ExportFormat::Archimate).expect("AD экспортируется");
        // Для `ArchiMate` пустое подмножество — модель без SYS/CMP/INT/CAP/REQ/NFR/AD.
        let dir2 = tempfile::tempdir().expect("tmp");
        write_entity(
            dir2.path(),
            "ADR-001.md",
            "id: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted\n",
            "",
        );
        let model2 = load_model(dir2.path()).expect("модель");
        let err = export_model(&model2, ExportFormat::Archimate)
            .expect_err("пустое подмножество `ArchiMate`");
        assert!(err.to_string().contains("нечего экспортировать"), "{err}");
    }

    #[test]
    fn roundtrip_structurizr_preserves_entities_and_links() {
        // DoD P1-3: модель → Structurizr DSL → модель без потерь сущностей/связей.
        let src_dir = tempfile::tempdir().expect("tmp");
        fixture_model(src_dir.path());
        let model = load_model(src_dir.path()).expect("модель");
        let dsl = export_model(&model, ExportFormat::Structurizr).expect("экспорт");

        let out_dir = tempfile::tempdir().expect("tmp");
        let dsl_file = out_dir.path().join("model.dsl");
        std::fs::write(&dsl_file, &dsl).expect("запись dsl");
        let model_dir = out_dir.path().join("model");
        let report = import_structurizr(&dsl_file, &model_dir).expect("импорт");
        assert_eq!(report.written.len(), 4, "{:?}", report.warnings);

        let reloaded = load_model(&model_dir).expect("перечитанная модель");
        let ids: BTreeSet<String> = reloaded.entities.iter().map(|e| e.id.clone()).collect();
        let want_ids: BTreeSet<String> = ["SYS-001", "CMP-001", "CMP-002", "INT-001"]
            .iter()
            .map(|s| (*s).to_owned())
            .collect();
        assert_eq!(ids, want_ids, "множество id сохранено");
        assert_eq!(
            link_triples(&model),
            link_triples(&reloaded),
            "множество пар связей сохранено"
        );
        // Типы, заголовки, статусы, даты — тоже пережили круг.
        let sys = reloaded.get("SYS-001").expect("sys");
        assert_eq!(sys.kind, EntityKind::Sys);
        assert_eq!(sys.title, "Платформа");
        assert_eq!(sys.status, "designed");
        assert_eq!(sys.date.as_deref(), Some("2026-08-17"));
        assert_eq!(sys.body, "Центральная система.");
        let int = reloaded.get("INT-001").expect("int");
        assert_eq!(int.kind, EntityKind::Int, "тип из spine.type, не из тега");
    }

    #[test]
    fn import_foreign_dsl_synthesizes_ids_and_kinds() {
        let dir = tempfile::tempdir().expect("tmp");
        let dsl = r#"
workspace "Чужая система" {

    model {
        user = person "Клиент"
        shop = softwareSystem "Магазин" {
            web = container "Web UI" "фронт" "React"
        }
        pay = softwareSystem "Платёжный провайдер" {
            tags "External"
        }
        shop -> pay "charges card"
        web -> pay "redirect"
        shop -> ghost "broken"
    }

    views {
        systemContext shop {
            include *
        }
    }
}
"#;
        let file = dir.path().join("foreign.dsl");
        std::fs::write(&file, dsl).expect("запись");
        let model_dir = dir.path().join("model");
        let report = import_structurizr(&file, &model_dir).expect("импорт");
        let model = load_model(&model_dir).expect("модель");
        let by_title: BTreeMap<&str, &Entity> = model
            .entities
            .iter()
            .map(|e| (e.title.as_str(), e))
            .collect();
        assert_eq!(by_title.len(), 3, "person пропущен: {report:?}");
        assert_eq!(by_title["Магазин"].kind, EntityKind::Sys);
        assert_eq!(by_title["Web UI"].kind, EntityKind::Cmp);
        assert_eq!(by_title["Платёжный провайдер"].kind, EntityKind::Int);
        // ID синтезированы по типу в порядке документа.
        assert_eq!(by_title["Магазин"].id, "SYS-001");
        assert_eq!(by_title["Web UI"].id, "CMP-001");
        assert_eq!(by_title["Платёжный провайдер"].id, "INT-001");
        // Статус по умолчанию; описание — в тело.
        assert_eq!(by_title["Магазин"].status, "imported");
        // Чужие описания связей → depends_on; битая связь — предупреждение.
        assert_eq!(by_title["Магазин"].depends_on, vec!["INT-001"]);
        assert_eq!(by_title["Web UI"].depends_on, vec!["INT-001"]);
        assert!(
            report.warnings.iter().any(|w| w.contains("ghost")),
            "предупреждение о битом алиасе: {:?}",
            report.warnings
        );
        assert!(
            report.warnings.iter().any(|w| w.contains("Клиент")),
            "предупреждение о person: {:?}",
            report.warnings
        );
    }

    #[test]
    fn import_known_link_kinds_restored() {
        let dir = tempfile::tempdir().expect("tmp");
        let dsl = r#"
workspace {
    model {
        a = softwareSystem "A"
        b = container "B"
        a -> b "implements"
        a -> b "affects"
        a -> b "implements"
    }
}
"#;
        let file = dir.path().join("k.dsl");
        std::fs::write(&file, dsl).expect("запись");
        let model_dir = dir.path().join("model");
        import_structurizr(&file, &model_dir).expect("импорт");
        let model = load_model(&model_dir).expect("модель");
        let a = model.get("SYS-001").expect("a");
        assert_eq!(a.implements, vec!["CMP-001"], "дубль связи схлопнут");
        assert_eq!(a.affects, vec!["CMP-001"]);
    }

    #[test]
    fn import_unbalanced_braces_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let file = dir.path().join("broken.dsl");
        std::fs::write(
            &file,
            "workspace {\n  model {\n    a = softwareSystem \"A\" {\n",
        )
        .expect("запись");
        let err =
            import_structurizr(&file, &dir.path().join("model")).expect_err("незакрытые скобки");
        assert!(err.to_string().contains("скобки"), "{err}");
        // Пустой/чужой файл — внятная ошибка, не паника.
        let empty = dir.path().join("empty.dsl");
        std::fs::write(&empty, "lorem ipsum\n").expect("запись");
        let err = import_structurizr(&empty, &dir.path().join("m2")).expect_err("нет элементов");
        assert!(err.to_string().contains("нечего импортировать"), "{err}");
    }

    #[test]
    fn import_refuses_to_overwrite_existing_file() {
        let dir = tempfile::tempdir().expect("tmp");
        let model_dir = dir.path().join("model");
        std::fs::create_dir_all(&model_dir).expect("mkdir");
        std::fs::write(
            model_dir.join("SYS-001-magazin.md"),
            "---\nid: SYS-001\ntype: sys\ntitle: Магазин\nstatus: mine\n---\n",
        )
        .expect("существующий файл");
        let file = dir.path().join("x.dsl");
        std::fs::write(
            &file,
            "workspace {\n model {\n s = softwareSystem \"Магазин\"\n }\n}\n",
        )
        .expect("запись");
        let err = import_structurizr(&file, &model_dir).expect_err("коллизия имён");
        assert!(err.to_string().contains("не затирает"), "{err}");
        // Существующий файл не тронут.
        let text = std::fs::read_to_string(model_dir.join("SYS-001-magazin.md")).expect("чтение");
        assert!(text.contains("status: mine"), "{text}");
    }

    #[test]
    fn import_spine_id_without_properties_still_works() {
        // Элемент без properties: id синтезируется, тип — по ключевому слову/тегу.
        let dir = tempfile::tempdir().expect("tmp");
        let file = dir.path().join("y.dsl");
        std::fs::write(
            &file,
            "workspace {\n model {\n a = softwareSystem \"Solo\" \"Описание\" \"External\"\n }\n}\n",
        )
        .expect("запись");
        let model_dir = dir.path().join("model");
        import_structurizr(&file, &model_dir).expect("импорт");
        let model = load_model(&model_dir).expect("модель");
        let e = model.get("INT-001").expect("int по позиционному тегу");
        assert_eq!(e.body, "Описание");
    }
}
