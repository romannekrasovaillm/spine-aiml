//! Типизированная модель архитектуры с трассируемостью ссылок (ADR-003).
//!
//! КОНТРАКТ (владелец: агент `model`):
//! - Хранение: каталог `model/` целевого проекта, одна сущность = один
//!   `.md`-файл с YAML-frontmatter (`id`, `type`, `title`, `status` +
//!   опциональные `date`, `depends_on`, `implements`, `affects`,
//!   `verified_by`, `verification`, количественные поля ADR-007 —
//!   `latency_budget_ms`, `p99_target_ms`, `availability_target`,
//!   `rto_minutes`, `rpo_seconds`, `rps_target`, `currency`,
//!   `availability`, `replicas`, `rps_per_instance`, `instances`,
//!   `cost_per_instance_month`, `exit_cost` и поля QAS `source`,
//!   `stimulus`, `artifact`, `response`, `measure`) и прозаическим телом;
//! - Стабильные типы ID: `CAP-`, `SYS-`, `CMP-`, `INT-`, `NFR-`, `REQ-`,
//!   `AD-`, `ADR-`, `RISK-`, `OWNER-`, `QAS-` (сценарий атрибута качества,
//!   ADR-007); разбор ID — один regex
//!   ([`ID_PATTERN`]/[`id_re`]), переиспользуется в `control::adr_new`;
//! - [`parse`] — разбор файлов в сущности, [`validate`] — ссылочная
//!   целостность (битая ссылка/дубль/цикл — `error`; `ADR` без `CMP`,
//!   `NFR` без проверки — `warn`), [`graph`] — текстовый/mermaid-граф,
//!   [`project`] — проекция `ADR-*` в `.arch-handoff/adr/`,
//!   [`exchange`] — обмен с отраслевыми форматами (экспорт Structurizr
//!   DSL/PlantUML/drawio, импорт Structurizr DSL; ADR-009);
//! - инструмент агента: `model_query` ([`tools`]).

pub mod exchange;
pub mod graph;
pub mod parse;
pub mod project;
pub mod validate;

use std::fmt::Write as _;
use std::sync::Arc;

use async_trait::async_trait;
use regex::Regex;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

pub use exchange::{ExportFormat, ImportReport, export_model, import_structurizr};
pub use graph::{find_cycle, graph_mermaid, graph_text};
pub use parse::{Entity, LinkKind, Model, load_model, parse_entity, split_frontmatter};
pub use project::{ProjectReport, project_adr, render_adr};
pub use validate::{ModelIssue, Severity, ValidationReport, validate};

/// Канонический паттерн идентификатора сущности модели (ADR-003).
///
/// Якорь `^`, без якоря конца: подходит и для строгой проверки
/// ([`parse_id`] сверяет длину совпадения), и для префиксного разбора имён
/// файлов (`ADR-001-saga.md` в `control::adr_new`). Группы: 1 — префикс,
/// 2 — номер.
pub const ID_PATTERN: &str = r"^(CAP|SYS|CMP|INT|NFR|REQ|AD|ADR|RISK|OWNER|QAS)-([0-9]+)";

/// Компилирует [`ID_PATTERN`]. Единственное место сборки regex идентификатора.
///
/// # Errors
/// Невозможна при корректном [`ID_PATTERN`]; ошибка компиляции пробрасывается,
/// чтобы не паниковать на статике.
pub fn id_re() -> Result<Regex> {
    Regex::new(ID_PATTERN).map_err(|e| HarnessError::Model(format!("внутренний regex ID: {e}")))
}

/// Строгий разбор идентификатора сущности (`PREFIX-NNN`, вся строка).
///
/// `None` — строка не является валидным ID.
#[must_use]
pub fn parse_id(s: &str) -> Option<(EntityKind, u64)> {
    let re = id_re().ok()?;
    let caps = re.captures(s)?;
    let whole = caps.get(0)?;
    if whole.end() != s.len() {
        return None;
    }
    let kind = EntityKind::from_prefix(caps.get(1)?.as_str())?;
    let n: u64 = caps.get(2)?.as_str().parse().ok()?;
    Some((kind, n))
}

/// Тип сущности модели (префикс ID).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum EntityKind {
    /// `CAP-` — бизнес-возможность.
    Cap,
    /// `SYS-` — система целиком.
    Sys,
    /// `CMP-` — компонент / bounded context.
    Cmp,
    /// `INT-` — интерфейс / взаимодействие с внешней системой.
    Int,
    /// `NFR-` — нефункциональное требование.
    Nfr,
    /// `REQ-` — функциональное требование.
    Req,
    /// `AD-` — архитектурный инвариант (spine).
    Ad,
    /// `ADR-` — запись архитектурного решения.
    Adr,
    /// `RISK-` — риск.
    Risk,
    /// `OWNER-` — владелец (команда/роль).
    Owner,
    /// `QAS-` — сценарий атрибута качества (source/stimulus/artifact/
    /// response/measure, ADR-007).
    Qas,
}

impl EntityKind {
    /// Все типы в стабильном порядке.
    pub const ALL: [EntityKind; 11] = [
        EntityKind::Cap,
        EntityKind::Sys,
        EntityKind::Cmp,
        EntityKind::Int,
        EntityKind::Nfr,
        EntityKind::Req,
        EntityKind::Ad,
        EntityKind::Adr,
        EntityKind::Risk,
        EntityKind::Owner,
        EntityKind::Qas,
    ];

    /// Префикс идентификатора (`CAP`, `ADR`, …).
    #[must_use]
    pub fn prefix(self) -> &'static str {
        match self {
            EntityKind::Cap => "CAP",
            EntityKind::Sys => "SYS",
            EntityKind::Cmp => "CMP",
            EntityKind::Int => "INT",
            EntityKind::Nfr => "NFR",
            EntityKind::Req => "REQ",
            EntityKind::Ad => "AD",
            EntityKind::Adr => "ADR",
            EntityKind::Risk => "RISK",
            EntityKind::Owner => "OWNER",
            EntityKind::Qas => "QAS",
        }
    }

    /// Тип по префиксу ID.
    #[must_use]
    pub fn from_prefix(prefix: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|k| k.prefix() == prefix)
    }

    /// Имя типа в frontmatter (`cap`, `adr`, …).
    #[must_use]
    pub fn type_str(self) -> &'static str {
        match self {
            EntityKind::Cap => "cap",
            EntityKind::Sys => "sys",
            EntityKind::Cmp => "cmp",
            EntityKind::Int => "int",
            EntityKind::Nfr => "nfr",
            EntityKind::Req => "req",
            EntityKind::Ad => "ad",
            EntityKind::Adr => "adr",
            EntityKind::Risk => "risk",
            EntityKind::Owner => "owner",
            EntityKind::Qas => "qas",
        }
    }

    /// Тип по имени из frontmatter.
    #[must_use]
    pub fn from_type_str(s: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|k| k.type_str() == s)
    }

    /// Название типа для пользователя (рус.).
    #[must_use]
    pub fn title_ru(self) -> &'static str {
        match self {
            EntityKind::Cap => "возможность",
            EntityKind::Sys => "система",
            EntityKind::Cmp => "компонент",
            EntityKind::Int => "интерфейс",
            EntityKind::Nfr => "нефункциональное требование",
            EntityKind::Req => "требование",
            EntityKind::Ad => "архитектурный инвариант",
            EntityKind::Adr => "архитектурное решение",
            EntityKind::Risk => "риск",
            EntityKind::Owner => "владелец",
            EntityKind::Qas => "сценарий атрибута качества",
        }
    }

    /// Все префиксы (для сообщений об ошибках).
    #[must_use]
    pub fn prefixes() -> Vec<&'static str> {
        Self::ALL.iter().map(|k| k.prefix()).collect()
    }

    /// Все имена типов (для сообщений об ошибках).
    #[must_use]
    pub fn type_names() -> Vec<&'static str> {
        Self::ALL.iter().map(|k| k.type_str()).collect()
    }
}

/// Текстовая карточка сущности: шапка, связи, обратные ссылки, тело.
#[must_use]
pub fn card(model: &Model, e: &Entity) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "{} · {}", e.id, e.title); // игнорируется: записи в String не падают
    let _ = writeln!(
        out,
        "Тип: {} ({})   Статус: {}",
        e.kind.type_str(),
        e.kind.title_ru(),
        e.status
    );
    if let Some(date) = &e.date {
        let _ = writeln!(out, "Дата: {date}");
    }
    if let Some(verification) = &e.verification {
        let _ = writeln!(out, "Проверка: {verification}");
    }
    let _ = writeln!(out, "Файл: {}", e.file.display());
    for kind in LinkKind::ALL {
        let targets = e.link_targets(kind);
        if !targets.is_empty() {
            let _ = writeln!(out, "  {} → {}", kind.field_name(), targets.join(", "));
        }
    }
    let refs = model.referents(&e.id);
    if !refs.is_empty() {
        let _ = writeln!(out, "Обратные ссылки:");
        for (src, kind) in refs {
            let _ = writeln!(
                out,
                "  ← {} от {} ({})",
                kind.field_name(),
                src.id,
                src.title
            );
        }
    }
    if !e.body.is_empty() {
        let _ = writeln!(out, "\n{}", e.body);
    }
    out
}

/// Инструменты домена: `model_query`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(ModelQueryTool)]
}

/// Инструмент `model_query`: запрос сущностей и связей типизированной модели.
pub struct ModelQueryTool;

#[derive(Debug, Deserialize)]
struct ModelQueryArgs {
    /// Каталог модели (дефолт `model`).
    dir: Option<String>,
    /// ID сущности — карточка со связями (без `id` — список сущностей).
    id: Option<String>,
    /// Фильтр списка по типу (`cmp`/`CMP-…` стиль: `cmp` или префикс `CMP`).
    #[serde(rename = "type")]
    kind: Option<String>,
}

#[async_trait]
impl Tool for ModelQueryTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "model_query".into(),
            description: "Запрос к типизированной модели архитектуры (каталог model/): \
                          карточка сущности по id со связями и обратными ссылками, либо \
                          список сущностей (с фильтром по типу: cap, sys, cmp, int, nfr, \
                          req, ad, adr, risk, owner, qas)"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "dir": {"type": "string", "description": "Каталог модели (по умолчанию model)"},
                    "id": {"type": "string", "description": "ID сущности (ADR-001, CMP-002, …): карточка со связями"},
                    "type": {"type": "string", "description": "Фильтр списка по типу (cmp, adr, …)"}
                }
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: ModelQueryArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "model_query: невалидные аргументы: {e}"
                )));
            }
        };
        let dir = ctx.resolve(args.dir.as_deref().unwrap_or("model"));
        let model = match load_model(&dir) {
            Ok(m) => m,
            Err(e) => return Ok(ToolOutput::err(format!("model_query: {e}"))),
        };
        if let Some(id) = args.id {
            return match model.get(&id) {
                Some(e) => Ok(ToolOutput::ok(card(&model, e))),
                None => Ok(ToolOutput::err(format!(
                    "model_query: сущность '{id}' не найдена (всего сущностей: {})",
                    model.entities.len()
                ))),
            };
        }
        let kind = match &args.kind {
            Some(raw) => {
                let norm = raw.trim().to_ascii_lowercase();
                match EntityKind::from_type_str(&norm)
                    .or_else(|| EntityKind::from_prefix(&raw.trim().to_ascii_uppercase()))
                {
                    Some(k) => Some(k),
                    None => {
                        return Ok(ToolOutput::err(format!(
                            "model_query: неизвестный тип '{raw}' (допустимы: {})",
                            EntityKind::type_names().join(", ")
                        )));
                    }
                }
            }
            None => None,
        };
        let mut out = format!(
            "Модель {}: {} сущностей{}\n",
            dir.display(),
            model.entities.len(),
            kind.map_or_else(String::new, |k| format!(", тип {}", k.type_str()))
        );
        for e in &model.entities {
            if kind.is_some_and(|k| e.kind != k) {
                continue;
            }
            let links: usize = LinkKind::ALL.iter().map(|k| e.link_targets(*k).len()).sum();
            let _ = writeln!(
                out,
                "  {:<10} {:<5} {:<9} {:<3} {}",
                e.id,
                e.kind.type_str(),
                e.status,
                links,
                e.title
            );
        }
        Ok(ToolOutput::ok(out))
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;

    #[test]
    fn parse_id_accepts_all_prefixes() {
        for (prefix, kind) in [
            ("CAP", EntityKind::Cap),
            ("SYS", EntityKind::Sys),
            ("CMP", EntityKind::Cmp),
            ("INT", EntityKind::Int),
            ("NFR", EntityKind::Nfr),
            ("REQ", EntityKind::Req),
            ("AD", EntityKind::Ad),
            ("ADR", EntityKind::Adr),
            ("RISK", EntityKind::Risk),
            ("OWNER", EntityKind::Owner),
            ("QAS", EntityKind::Qas),
        ] {
            let got = parse_id(&format!("{prefix}-7")).expect("валидный ID");
            assert_eq!(got, (kind, 7), "префикс {prefix}");
        }
        // Непаддинг и паддинг эквивалентны по номеру.
        assert_eq!(parse_id("AD-1"), Some((EntityKind::Ad, 1)));
        assert_eq!(parse_id("ADR-001"), Some((EntityKind::Adr, 1)));
    }

    #[test]
    fn parse_id_rejects_garbage() {
        for bad in [
            "", "ADR", "ADR-", "FOO-1",
            "ad-1",     // регистр префикса значим
            "ADR-1x",   // номер не чисто числовой
            "ADR-1.md", // лишний хвост
            " ADR-1",   // ведущий пробел
            "ADR--1",
        ] {
            assert_eq!(parse_id(bad), None, "'{bad}' не должен разбираться");
        }
        // ADR не схлопывается в AD: префикс жадный и точный.
        assert_eq!(parse_id("ADR-5"), Some((EntityKind::Adr, 5)));
    }

    #[test]
    fn id_re_matches_filename_prefix() {
        // Тот же regex используется control::adr_new для имён файлов.
        let re = id_re().expect("regex");
        let caps = re.captures("ADR-012-api-versioning.md").expect("матч");
        assert_eq!(caps.get(1).expect("группа 1").as_str(), "ADR");
        assert_eq!(caps.get(2).expect("группа 2").as_str(), "012");
        assert!(re.captures("README.md").is_none());
    }

    #[test]
    fn kind_prefix_type_roundtrip() {
        for k in EntityKind::ALL {
            assert_eq!(EntityKind::from_prefix(k.prefix()), Some(k));
            assert_eq!(EntityKind::from_type_str(k.type_str()), Some(k));
        }
        assert_eq!(EntityKind::from_prefix("FOO"), None);
        assert_eq!(EntityKind::from_type_str("widget"), None);
    }

    fn fixture_model(dir: &Path) {
        std::fs::write(
            dir.join("AD-1.md"),
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\n---\n\nПравило.\n",
        )
        .expect("фикстура AD");
        std::fs::write(
            dir.join("ADR-001-x.md"),
            "---\nid: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted\ndate: 2026-08-17\nimplements: [AD-1]\n---\n\nКонтекст и решение.\n",
        )
        .expect("фикстура ADR");
    }

    #[tokio::test]
    async fn model_query_card_and_missing() {
        let dir = tempfile::tempdir().expect("tmp");
        fixture_model(dir.path());
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let tool = ModelQueryTool;
        let out = tool
            .call(json!({"dir": ".", "id": "ADR-001"}), &ctx)
            .await
            .expect("вызов");
        assert!(!out.is_error, "{}", out.content);
        for needle in [
            "ADR-001 · Решение",
            "implements → AD-1",
            "Контекст и решение.",
        ] {
            assert!(
                out.content.contains(needle),
                "нет '{needle}' в {}",
                out.content
            );
        }
        let out = tool
            .call(json!({"dir": ".", "id": "CMP-999"}), &ctx)
            .await
            .expect("вызов");
        assert!(out.is_error, "неизвестный id — мягкая ошибка");
        assert!(out.content.contains("не найдена"), "{}", out.content);
    }

    #[tokio::test]
    async fn model_query_list_with_type_filter_and_bad_dir() {
        let dir = tempfile::tempdir().expect("tmp");
        fixture_model(dir.path());
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let tool = ModelQueryTool;
        let out = tool
            .call(json!({"dir": ".", "type": "adr"}), &ctx)
            .await
            .expect("вызов");
        assert!(out.content.contains("ADR-001"), "{}", out.content);
        assert!(
            !out.content.contains("AD-1 "),
            "фильтр по adr: {}",
            out.content
        );
        // Неизвестный тип — мягкая ошибка со списком допустимых.
        let out = tool
            .call(json!({"dir": ".", "type": "widget"}), &ctx)
            .await
            .expect("вызов");
        assert!(
            out.is_error && out.content.contains("неизвестный тип"),
            "{}",
            out.content
        );
        // Несуществующий каталог — мягкая ошибка, не паника.
        let out = tool
            .call(json!({"dir": "ghost-model"}), &ctx)
            .await
            .expect("вызов");
        assert!(out.is_error, "{}", out.content);
    }
}
