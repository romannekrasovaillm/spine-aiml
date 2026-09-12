//! Трассируемость как fitness-функция (ADR-006): позвенное покрытие цепочки
//! `REQ → NFR → AD/ADR → CMP → fitness-правило` поверх модели P0-1.
//!
//! КОНТРАКТ (владелец: агент `trace`):
//! - [`trace_check`] читает `<case>/model/`, `<case>/CONSTRAINTS.yaml` (ID
//!   правил — разделяемый загрузчик из `model::validate`) и опциональный
//!   `<case>/ARCHITECTURE-SPINE.md` (перечень инвариантов — сверка модели
//!   со spine);
//! - обязательное звено одно: `AD → fitness-правило` (`verified_by` на
//!   существующее правило `C-NNN` либо непустое `unverifiable`) — нарушение
//!   даёт `error` и exit code 1; сироты остальных звеньев — `warn`;
//! - звено `INT → контракт` (ADR-035): `INT` с полем `contract` обязан
//!   указывать на существующий файл (относительно корня кейса) — битый
//!   путь даёт `error`; `INT` без поля `contract` — `warn` (внешняя
//!   интеграция без артефакта в репо легитимна, но видима);
//! - [`render_markdown`] — отчёт таблицей, пригодный для evidence bundle;
//! - инструмент агента: `trace_check` ([`tools`]).

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_trait::async_trait;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::Result;
use crate::llm::ToolSpec;
use crate::model::validate::{Constraints, load_constraint_ids};
use crate::model::{EntityKind, LinkKind, Model, Severity, load_model};
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Звено цепочки трассировки.
#[derive(Debug)]
pub struct LevelReport {
    /// Название звена (`REQ → дизайн`, …).
    pub name: &'static str,
    /// Сущностей в звене.
    pub total: usize,
    /// Покрытых.
    pub covered: usize,
    /// Из них покрыто через `unverifiable` (только звено AD).
    pub unverifiable: usize,
    /// ID сирот (непокрытых), поимённо.
    pub orphans: Vec<String>,
}

impl LevelReport {
    /// Доля покрытия в процентах (`None` — звено пусто).
    fn percent(&self) -> Option<usize> {
        (self.covered * 100).checked_div(self.total)
    }
}

/// Находка трассировки.
#[derive(Debug)]
pub struct TraceIssue {
    /// Критичность.
    pub severity: Severity,
    /// Код правила (`ad-not-verified`, `orphan-req`, …).
    pub rule: &'static str,
    /// Сообщение.
    pub message: String,
}

/// Отчёт трассировки.
#[derive(Debug)]
pub struct TraceReport {
    /// Корень кейса.
    pub case: PathBuf,
    /// Звенья в порядке цепочки.
    pub levels: Vec<LevelReport>,
    /// Сущностей в модели.
    pub entities: usize,
    /// Правил в CONSTRAINTS.yaml (`None` — файла нет/не читается).
    pub constraint_rules: Option<usize>,
    /// Инвариантов в spine (`None` — файла нет).
    pub spine_ads: Option<usize>,
    /// Находки.
    pub issues: Vec<TraceIssue>,
}

impl TraceReport {
    /// Есть ли блокирующие находки (exit code 1).
    #[must_use]
    pub fn has_errors(&self) -> bool {
        self.issues.iter().any(|i| i.severity == Severity::Error)
    }

    /// Число находок вида (errors, warns).
    fn counts(&self) -> (usize, usize) {
        let errors = self
            .issues
            .iter()
            .filter(|i| i.severity == Severity::Error)
            .count();
        (errors, self.issues.len() - errors)
    }
}

/// Множества ссылочной активности модели для позвенного анализа.
struct LinkIndex<'m> {
    /// ID сущности → сущности, ссылающиеся на неё.
    inbound: BTreeMap<&'m str, Vec<&'m crate::model::Entity>>,
}

impl<'m> LinkIndex<'m> {
    fn build(model: &'m Model) -> Self {
        let mut inbound: BTreeMap<&str, Vec<&crate::model::Entity>> = BTreeMap::new();
        for e in &model.entities {
            for kind in LinkKind::ALL {
                for target in e.link_targets(kind) {
                    if model.get(target).is_some() {
                        inbound.entry(target.as_str()).or_default().push(e);
                    }
                }
            }
        }
        Self { inbound }
    }

    /// Ссылается ли на `id` хотя бы одна сущность.
    fn has_inbound(&self, id: &str) -> bool {
        self.inbound.contains_key(id)
    }

    /// Ссылается ли на `id` сущность одного из типов `kinds`.
    fn has_inbound_of(&self, id: &str, kinds: &[EntityKind]) -> bool {
        self.inbound
            .get(id)
            .is_some_and(|srcs| srcs.iter().any(|s| kinds.contains(&s.kind)))
    }
}

/// Есть ли у сущности исходящая связь на сущность одного из типов `kinds`.
fn has_outbound_of(model: &Model, e: &crate::model::Entity, kinds: &[EntityKind]) -> bool {
    LinkKind::ALL.iter().any(|kind| {
        e.link_targets(*kind)
            .iter()
            .any(|t| model.get(t).is_some_and(|x| kinds.contains(&x.kind)))
    })
}

/// Цель ссылки — правило CONSTRAINTS.yaml (`C-` + номер)?
fn is_constraint_ref(raw: &str) -> bool {
    let rest = raw.strip_prefix("C-");
    rest.is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

/// Трассировка кейса: покрытие звеньев + сверка со spine.
///
/// # Errors
/// Каталог модели не читается/не разбирается.
pub fn trace_check(case_dir: &Path) -> Result<TraceReport> {
    let model_dir = case_dir.join("model");
    let model = load_model(&model_dir)?;
    let constraints = load_constraint_ids(&model_dir);
    let mut issues = Vec::new();
    let constraint_rules = constraints_status(case_dir, &constraints, &mut issues);
    let index = LinkIndex::build(&model);
    let levels = vec![
        req_level(&model, &index, &mut issues),
        nfr_level(&model, &index, &mut issues),
        ad_level(&model, &constraints, &mut issues),
        adr_level(&model, &mut issues),
        cmp_level(&model, &mut issues),
        int_level(case_dir, &model, &mut issues),
    ];
    let spine_ads = spine_crosscheck(case_dir, &model, &mut issues)?;
    Ok(TraceReport {
        case: case_dir.to_path_buf(),
        levels,
        entities: model.entities.len(),
        constraint_rules,
        spine_ads,
        issues,
    })
}

/// Добавляет находку в отчёт.
fn issue(issues: &mut Vec<TraceIssue>, severity: Severity, rule: &'static str, message: String) {
    issues.push(TraceIssue {
        severity,
        rule,
        message,
    });
}

/// Статус CONSTRAINTS.yaml: число правил либо error-находка.
fn constraints_status(
    case_dir: &Path,
    constraints: &Constraints,
    issues: &mut Vec<TraceIssue>,
) -> Option<usize> {
    match constraints {
        Constraints::Loaded(ids) => Some(ids.len()),
        Constraints::Absent => {
            issue(
                issues,
                Severity::Error,
                "constraints-missing",
                format!(
                    "CONSTRAINTS.yaml не найден рядом с моделью ({}) — звено fitness не проверить",
                    case_dir.display()
                ),
            );
            None
        }
        Constraints::Broken(why) => {
            issue(
                issues,
                Severity::Error,
                "constraints-unreadable",
                format!("CONSTRAINTS.yaml не читается: {why}"),
            );
            None
        }
    }
}

/// Звено `REQ → дизайн`: есть входящая ссылка.
fn req_level(model: &Model, index: &LinkIndex, issues: &mut Vec<TraceIssue>) -> LevelReport {
    level_report(
        "REQ → дизайн",
        model,
        EntityKind::Req,
        |e| index.has_inbound(&e.id),
        |e| {
            issue(
                issues,
                Severity::Warn,
                "orphan-req",
                format!("{}: на требование не ссылается ни одна сущность", e.id),
            );
        },
    )
}

/// Звено `NFR → дизайн`: способ проверки + связь с AD/ADR/CMP.
fn nfr_level(model: &Model, index: &LinkIndex, issues: &mut Vec<TraceIssue>) -> LevelReport {
    let design = [EntityKind::Ad, EntityKind::Adr, EntityKind::Cmp];
    level_report(
        "NFR → дизайн",
        model,
        EntityKind::Nfr,
        |e| {
            let checkable = e
                .verification
                .as_deref()
                .is_some_and(|v| !v.trim().is_empty())
                || !e.verified_by.is_empty();
            let linked = has_outbound_of(model, e, &design) || index.has_inbound_of(&e.id, &design);
            checkable && linked
        },
        |e| {
            issue(
                issues,
                Severity::Warn,
                "orphan-nfr",
                format!(
                    "{}: нет способа проверки (verification/verified_by) или связи с AD/ADR/CMP",
                    e.id
                ),
            );
        },
    )
}

/// Звено `AD → fitness-правило` (обязательное): существующее правило C-NNN
/// либо непустое `unverifiable`.
fn ad_level(model: &Model, constraints: &Constraints, issues: &mut Vec<TraceIssue>) -> LevelReport {
    let rule_exists = |raw: &str| match constraints {
        Constraints::Loaded(ids) => ids.contains(raw),
        _ => false,
    };
    let mut level = LevelReport {
        name: "AD → fitness-правило",
        total: 0,
        covered: 0,
        unverifiable: 0,
        orphans: Vec::new(),
    };
    for e in model.entities.iter().filter(|e| e.kind == EntityKind::Ad) {
        level.total += 1;
        let rules: Vec<&str> = e
            .verified_by
            .iter()
            .filter(|t| is_constraint_ref(t) && rule_exists(t))
            .map(String::as_str)
            .collect();
        let has_rule = !rules.is_empty();
        let has_unverifiable = e
            .unverifiable
            .as_deref()
            .is_some_and(|u| !u.trim().is_empty());
        if has_rule && has_unverifiable {
            issue(
                issues,
                Severity::Warn,
                "ad-rule-and-unverifiable",
                format!(
                    "{}: есть и правило ({}), и unverifiable — оставьте что-то одно",
                    e.id,
                    rules.join(", ")
                ),
            );
        }
        if has_rule || has_unverifiable {
            level.covered += 1;
            if !has_rule && has_unverifiable {
                level.unverifiable += 1;
            }
        } else {
            level.orphans.push(e.id.clone());
            issue(
                issues,
                Severity::Error,
                "ad-not-verified",
                format!(
                    "{}: ни одного правила CONSTRAINTS.yaml (verified_by: C-NNN) и нет unverifiable с обоснованием",
                    e.id
                ),
            );
        }
    }
    level
}

/// Звено `ADR → CMP`: affects содержит существующий CMP.
fn adr_level(model: &Model, issues: &mut Vec<TraceIssue>) -> LevelReport {
    level_report(
        "ADR → CMP",
        model,
        EntityKind::Adr,
        |e| {
            e.affects
                .iter()
                .any(|t| model.get(t).is_some_and(|x| x.kind == EntityKind::Cmp))
        },
        |e| {
            issue(
                issues,
                Severity::Warn,
                "orphan-adr",
                format!(
                    "{}: не затрагивает ни одного компонента (affects → CMP-*)",
                    e.id
                ),
            );
        },
    )
}

/// Звено `CMP → fitness`: implements содержит AD (компонент привязан
/// к проверяемому инварианту).
fn cmp_level(model: &Model, issues: &mut Vec<TraceIssue>) -> LevelReport {
    level_report(
        "CMP → fitness",
        model,
        EntityKind::Cmp,
        |e| {
            e.implements
                .iter()
                .any(|t| model.get(t).is_some_and(|x| x.kind == EntityKind::Ad))
        },
        |e| {
            issue(
                issues,
                Severity::Warn,
                "orphan-cmp",
                format!(
                    "{}: не реализует ни одного инварианта (implements → AD-*)",
                    e.id
                ),
            );
        },
    )
}

/// Непустое значение поля `contract` сущности (пустая строка = отсутствует).
fn contract_path(e: &crate::model::Entity) -> Option<&str> {
    e.contract
        .as_deref()
        .map(str::trim)
        .filter(|c| !c.is_empty())
}

/// Звено `INT → контракт` (ADR-035): поле `contract` указывает на
/// существующий файл относительно корня кейса. Битый путь — `error`
/// (заявленный контракт потерян), отсутствие поля — `warn` (внешняя
/// интеграция без артефакта в репо легитимна, но должна быть видна).
fn int_level(case_dir: &Path, model: &Model, issues: &mut Vec<TraceIssue>) -> LevelReport {
    level_report(
        "INT → контракт",
        model,
        EntityKind::Int,
        |e| contract_path(e).is_some_and(|c| case_dir.join(c).is_file()),
        |e| match contract_path(e) {
            Some(c) => issue(
                issues,
                Severity::Error,
                "int-contract-missing",
                format!(
                    "{}: контрактный артефакт '{c}' не найден (путь относительно корня кейса)",
                    e.id
                ),
            ),
            None => issue(
                issues,
                Severity::Warn,
                "int-without-contract",
                format!(
                    "{}: интеграция без контрактного артефакта (поле contract не задано)",
                    e.id
                ),
            ),
        },
    )
}

/// Сверка со spine (если файл есть): каждый AD из ARCHITECTURE-SPINE.md
/// обязан быть в модели (error); AD в модели вне spine — warn.
/// `Ok(None)` — spine-файла нет.
fn spine_crosscheck(
    case_dir: &Path,
    model: &Model,
    issues: &mut Vec<TraceIssue>,
) -> Result<Option<usize>> {
    let spine_path = case_dir.join("ARCHITECTURE-SPINE.md");
    if !spine_path.is_file() {
        return Ok(None);
    }
    let spine_ids = crate::control::spine_ad_ids(&spine_path)?;
    let model_ads: BTreeSet<u64> = model
        .entities
        .iter()
        .filter(|e| e.kind == EntityKind::Ad)
        .filter_map(|e| crate::model::parse_id(&e.id).map(|(_, n)| n))
        .collect();
    for n in spine_ids.difference(&model_ads) {
        issue(
            issues,
            Severity::Error,
            "spine-ad-missing-in-model",
            format!("AD-{n}: есть в ARCHITECTURE-SPINE.md, но нет в модели"),
        );
    }
    for n in model_ads.difference(&spine_ids) {
        issue(
            issues,
            Severity::Warn,
            "ad-not-in-spine",
            format!("AD-{n}: есть в модели, но нет в ARCHITECTURE-SPINE.md"),
        );
    }
    Ok(Some(spine_ids.len()))
}

/// Считает покрытие одного звена по предикату и регистрирует сирот.
fn level_report(
    name: &'static str,
    model: &Model,
    kind: EntityKind,
    covered: impl Fn(&crate::model::Entity) -> bool,
    mut on_orphan: impl FnMut(&crate::model::Entity),
) -> LevelReport {
    let mut level = LevelReport {
        name,
        total: 0,
        covered: 0,
        unverifiable: 0,
        orphans: Vec::new(),
    };
    for e in model.entities.iter().filter(|e| e.kind == kind) {
        level.total += 1;
        if covered(e) {
            level.covered += 1;
        } else {
            level.orphans.push(e.id.clone());
            on_orphan(e);
        }
    }
    level
}

/// Отчёт трассировки в markdown (пригоден для evidence bundle).
#[must_use]
pub fn render_markdown(report: &TraceReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# Трассируемость: {}", report.case.display());
    let _ = writeln!(out);
    let rules = report
        .constraint_rules
        .map_or("нет".into(), |n| n.to_string());
    let spine = report.spine_ads.map_or("нет".into(), |n| n.to_string());
    let _ = writeln!(
        out,
        "Сущностей: {}; правил CONSTRAINTS.yaml: {}; инвариантов spine: {}.",
        report.entities, rules, spine
    );
    let _ = writeln!(out);
    let _ = writeln!(out, "| Звено | Покрыто | Доля | Сироты |");
    let _ = writeln!(out, "|---|---|---|---|");
    for level in &report.levels {
        let percent = level.percent().map_or("—".into(), |p| format!("{p}%"));
        let mut covered = format!("{}/{}", level.covered, level.total);
        if level.unverifiable > 0 {
            let _ = write!(covered, " (unverifiable: {})", level.unverifiable);
        }
        let orphans = if level.orphans.is_empty() {
            "—".into()
        } else {
            level.orphans.join(", ")
        };
        let _ = writeln!(
            out,
            "| {} | {} | {} | {} |",
            level.name, covered, percent, orphans
        );
    }
    if !report.issues.is_empty() {
        let _ = writeln!(out, "\n## Находки");
        let _ = writeln!(out);
        for i in &report.issues {
            let _ = writeln!(out, "- [{}] {}: {}", i.severity, i.rule, i.message);
        }
    }
    let (errors, warns) = report.counts();
    let _ = writeln!(
        out,
        "\nИтог: {} (error: {errors}, warn: {warns})",
        if report.has_errors() { "FAIL" } else { "PASS" }
    );
    out
}

/// Инструменты домена: `trace_check`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(TraceCheckTool)]
}

/// Инструмент `trace_check`: позвенная трассируемость модели кейса.
pub struct TraceCheckTool;

#[derive(Debug, Deserialize)]
struct TraceCheckArgs {
    /// Корень кейса (каталог с model/, дефолт — текущий).
    dir: Option<String>,
}

#[async_trait]
impl Tool for TraceCheckTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "trace_check".into(),
            description: "Трассируемость архитектуры как fitness-функция: покрытие звеньев \
                          REQ → NFR → AD/ADR → CMP → правило CONSTRAINTS.yaml, INT → контракт \
                          (поле contract, ADR-035), поимённые сироты, \
                          сверка модели с ARCHITECTURE-SPINE.md. AD без правила и без unverifiable — \
                          error. Отчёт markdown (пригоден для evidence bundle)"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "dir": {"type": "string", "description": "Корень кейса (по умолчанию текущий каталог)"}
                }
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: TraceCheckArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "trace_check: невалидные аргументы: {e}"
                )));
            }
        };
        let dir = ctx.resolve(args.dir.as_deref().unwrap_or("."));
        match trace_check(&dir) {
            Ok(report) => Ok(ToolOutput::ok(render_markdown(&report))),
            Err(e) => Ok(ToolOutput::err(format!("trace_check: {e}"))),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Пишет синтетический кейс: `model/*.md`, опционально CONSTRAINTS.yaml
    /// и ARCHITECTURE-SPINE.md, плюс контрактный артефакт
    /// `contracts/rail.yaml` (для INT-001 из [`full_entities`], ADR-035).
    /// Возвращает корень кейса.
    fn write_case(
        root: &Path,
        entities: &[(&str, &str)],
        constraints: Option<&str>,
        spine: Option<&str>,
    ) -> PathBuf {
        let case = root.join("case");
        let model_dir = case.join("model");
        std::fs::create_dir_all(&model_dir).expect("dirs");
        for (name, fm) in entities {
            std::fs::write(model_dir.join(name), format!("{fm}\n---\n\nТело.\n"))
                .expect("сущность");
        }
        let contracts = case.join("contracts");
        std::fs::create_dir_all(&contracts).expect("contracts dir");
        std::fs::write(contracts.join("rail.yaml"), "asyncapi: 3.0.0\n").expect("контракт");
        if let Some(c) = constraints {
            std::fs::write(case.join("CONSTRAINTS.yaml"), c).expect("constraints");
        }
        if let Some(s) = spine {
            std::fs::write(case.join("ARCHITECTURE-SPINE.md"), s).expect("spine");
        }
        case
    }

    /// Полный минимальный кейс без сирот: REQ ← CMP, NFR → CMP + verification,
    /// AD с правилом, ADR → CMP, CMP implements AD, INT с контрактом;
    /// spine согласован.
    fn full_entities() -> Vec<(&'static str, &'static str)> {
        vec![
            (
                "REQ-001.md",
                "---\nid: REQ-001\ntype: req\ntitle: Требование\nstatus: accepted",
            ),
            (
                "NFR-001.md",
                "---\nid: NFR-001\ntype: nfr\ntitle: Латентность\nstatus: accepted\nverification: histogram\naffects: [CMP-001]",
            ),
            (
                "AD-1.md",
                "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\nverified_by: [C-001]",
            ),
            (
                "ADR-001.md",
                "---\nid: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted\naffects: [CMP-001]\nimplements: [AD-1]",
            ),
            (
                "CMP-001.md",
                "---\nid: CMP-001\ntype: cmp\ntitle: Компонент\nstatus: designed\nimplements: [AD-1, REQ-001]",
            ),
            (
                "INT-001.md",
                "---\nid: INT-001\ntype: int\ntitle: Карточный рельс\nstatus: accepted\ncontract: contracts/rail.yaml",
            ),
        ]
    }

    const CONSTRAINTS: &str = "constraints:\n  - id: C-001\n    name: правило\n";
    const SPINE: &str = "# Spine\n\n## AD-1: Инвариант\n\n- **Rule**: …\n";

    #[test]
    fn full_chain_covered_passes() {
        let dir = tempfile::tempdir().expect("tmp");
        let case = write_case(dir.path(), &full_entities(), Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(!report.has_errors(), "{:?}", report.issues.len());
        assert!(
            report.issues.is_empty(),
            "ни warn: {:?}",
            report.issues.iter().map(|i| i.rule).collect::<Vec<_>>()
        );
        for level in &report.levels {
            assert_eq!(level.percent(), Some(100), "{}", level.name);
        }
        assert_eq!(report.constraint_rules, Some(1));
        assert_eq!(report.spine_ads, Some(1));
    }

    #[test]
    fn ad_without_rule_and_unverifiable_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities[2] = (
            "AD-1.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(report.has_errors());
        assert!(
            report.issues.iter().any(|i| i.rule == "ad-not-verified"),
            "{:?}",
            report.issues.iter().map(|i| i.rule).collect::<Vec<_>>()
        );
        let ad = report
            .levels
            .iter()
            .find(|l| l.name == "AD → fitness-правило")
            .expect("уровень");
        assert_eq!(ad.orphans, vec!["AD-1"]);
    }

    #[test]
    fn ad_with_unverifiable_justification_is_covered() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities[2] = (
            "AD-1.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\nunverifiable: практика game days, проверяется регламентом",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(!report.has_errors());
        let ad = report
            .levels
            .iter()
            .find(|l| l.name == "AD → fitness-правило")
            .expect("уровень");
        assert_eq!(ad.percent(), Some(100));
        assert_eq!(ad.unverifiable, 1);
    }

    #[test]
    fn ad_with_empty_unverifiable_is_not_covered() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities[2] = (
            "AD-1.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\nunverifiable: \"  \"",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(report.has_errors(), "пустое обоснование не покрывает");
    }

    #[test]
    fn ad_with_nonexistent_constraint_rule_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities[2] = (
            "AD-1.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\nverified_by: [C-999]",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(report.has_errors());
        assert!(report.issues.iter().any(|i| i.rule == "ad-not-verified"));
    }

    #[test]
    fn ad_with_both_rule_and_unverifiable_warns() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities[2] = (
            "AD-1.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\nverified_by: [C-001]\nunverifiable: на всякий случай",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(!report.has_errors(), "warn не ломает");
        assert!(
            report
                .issues
                .iter()
                .any(|i| i.rule == "ad-rule-and-unverifiable")
        );
    }

    #[test]
    fn req_orphan_warns_but_passes() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        // CMP перестаёт ссылаться на REQ-001.
        entities[4] = (
            "CMP-001.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: Компонент\nstatus: designed\nimplements: [AD-1]",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(!report.has_errors());
        assert!(report.issues.iter().any(|i| i.rule == "orphan-req"));
        let req = report
            .levels
            .iter()
            .find(|l| l.name == "REQ → дизайн")
            .expect("уровень");
        assert_eq!(req.orphans, vec!["REQ-001"]);
        assert_eq!(req.percent(), Some(0));
    }

    #[test]
    fn nfr_orphans_warn() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        // NFR без связи с дизайном (но с verification).
        entities[1] = (
            "NFR-001.md",
            "---\nid: NFR-001\ntype: nfr\ntitle: Латентность\nstatus: accepted\nverification: histogram",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(report.issues.iter().any(|i| i.rule == "orphan-nfr"));
        // NFR без verification — тоже сирота.
        entities[1] = (
            "NFR-001.md",
            "---\nid: NFR-001\ntype: nfr\ntitle: Латентность\nstatus: accepted\naffects: [CMP-001]",
        );
        let case = write_case(
            dir.path().join("x").as_path(),
            &entities,
            Some(CONSTRAINTS),
            Some(SPINE),
        );
        let report = trace_check(&case).expect("trace");
        assert!(report.issues.iter().any(|i| i.rule == "orphan-nfr"));
    }

    #[test]
    fn adr_and_cmp_orphans_warn() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities[3] = (
            "ADR-001.md",
            "---\nid: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted\nimplements: [AD-1]",
        );
        entities[4] = (
            "CMP-001.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: Компонент\nstatus: designed\nimplements: [REQ-001]",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        let rules: Vec<_> = report.issues.iter().map(|i| i.rule).collect();
        assert!(rules.contains(&"orphan-adr"), "{rules:?}");
        assert!(rules.contains(&"orphan-cmp"), "{rules:?}");
        assert!(!report.has_errors());
    }

    #[test]
    fn int_contract_broken_path_is_error() {
        // Зелёное звено (INT-001 + contracts/rail.yaml) проверяет
        // full_chain_covered_passes: все уровни на 100%.
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities[5] = (
            "INT-001.md",
            "---\nid: INT-001\ntype: int\ntitle: Карточный рельс\nstatus: accepted\ncontract: contracts/ghost.yaml",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(report.has_errors(), "битый путь контракта — error");
        assert!(
            report
                .issues
                .iter()
                .any(|i| i.rule == "int-contract-missing"
                    && i.severity == Severity::Error
                    && i.message.contains("INT-001"))
        );
        let lvl = report
            .levels
            .iter()
            .find(|l| l.name == "INT → контракт")
            .expect("уровень");
        assert_eq!(lvl.orphans, vec!["INT-001"]);
        assert_eq!(lvl.percent(), Some(0));
    }

    #[test]
    fn int_without_contract_is_warn_not_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities[5] = (
            "INT-001.md",
            "---\nid: INT-001\ntype: int\ntitle: Карточный рельс\nstatus: accepted",
        );
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(
            !report.has_errors(),
            "внешняя интеграция без контракта легитимна"
        );
        assert!(
            report
                .issues
                .iter()
                .any(|i| i.rule == "int-without-contract"
                    && i.severity == Severity::Warn
                    && i.message.contains("INT-001"))
        );
        // Пустое значение contract эквивалентно отсутствию поля.
        entities[5] = (
            "INT-001.md",
            "---\nid: INT-001\ntype: int\ntitle: Карточный рельс\nstatus: accepted\ncontract: \"  \"",
        );
        let case = write_case(
            dir.path().join("y").as_path(),
            &entities,
            Some(CONSTRAINTS),
            Some(SPINE),
        );
        let report = trace_check(&case).expect("trace");
        assert!(
            report
                .issues
                .iter()
                .any(|i| i.rule == "int-without-contract")
        );
        assert!(!report.has_errors());
    }

    #[test]
    fn render_markdown_includes_int_contract_row() {
        let dir = tempfile::tempdir().expect("tmp");
        let case = write_case(dir.path(), &full_entities(), Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        let md = render_markdown(&report);
        assert!(md.contains("| INT → контракт | 1/1 | 100% | — |"), "{md}");
    }

    #[test]
    fn spine_ad_missing_in_model_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let spine =
            "# Spine\n\n## AD-1: Инвариант\n\n- **Rule**: …\n\n## AD-2: Второй\n\n- **Rule**: …\n";
        let case = write_case(dir.path(), &full_entities(), Some(CONSTRAINTS), Some(spine));
        let report = trace_check(&case).expect("trace");
        assert!(report.has_errors());
        assert!(
            report
                .issues
                .iter()
                .any(|i| i.rule == "spine-ad-missing-in-model" && i.message.contains("AD-2"))
        );
    }

    #[test]
    fn model_ad_not_in_spine_warns() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities.push((
            "AD-9.md",
            "---\nid: AD-9\ntype: ad\ntitle: Лишний\nstatus: ADOPTED\nverified_by: [C-001]",
        ));
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(!report.has_errors());
        assert!(
            report
                .issues
                .iter()
                .any(|i| i.rule == "ad-not-in-spine" && i.message.contains("AD-9"))
        );
    }

    #[test]
    fn missing_constraints_file_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let case = write_case(dir.path(), &full_entities(), None, Some(SPINE));
        let report = trace_check(&case).expect("trace");
        assert!(report.has_errors());
        let rules: Vec<_> = report.issues.iter().map(|i| i.rule).collect();
        assert!(rules.contains(&"constraints-missing"), "{rules:?}");
        assert!(rules.contains(&"ad-not-verified"), "{rules:?}");
        assert_eq!(report.constraint_rules, None);
    }

    #[test]
    fn missing_spine_file_skips_crosscheck() {
        let dir = tempfile::tempdir().expect("tmp");
        let case = write_case(dir.path(), &full_entities(), Some(CONSTRAINTS), None);
        let report = trace_check(&case).expect("trace");
        assert_eq!(report.spine_ads, None);
        assert!(!report.has_errors());
    }

    #[test]
    fn render_markdown_has_table_orphans_and_verdict() {
        let dir = tempfile::tempdir().expect("tmp");
        let mut entities = full_entities();
        entities.push((
            "REQ-009.md",
            "---\nid: REQ-009\ntype: req\ntitle: Сирота\nstatus: accepted",
        ));
        let case = write_case(dir.path(), &entities, Some(CONSTRAINTS), Some(SPINE));
        let report = trace_check(&case).expect("trace");
        let md = render_markdown(&report);
        assert!(md.contains("| Звено | Покрыто | Доля | Сироты |"), "{md}");
        assert!(
            md.contains("| REQ → дизайн | 1/2 | 50% | REQ-009 |"),
            "{md}"
        );
        assert!(md.contains("- [warn] orphan-req: REQ-009"), "{md}");
        assert!(md.contains("Итог: PASS"), "{md}");
    }

    #[tokio::test]
    async fn tool_ok_report_and_err_on_missing_dir() {
        let dir = tempfile::tempdir().expect("tmp");
        let case = write_case(dir.path(), &full_entities(), Some(CONSTRAINTS), Some(SPINE));
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let tool = TraceCheckTool;
        let out = tool
            .call(json!({"dir": "case"}), &ctx)
            .await
            .expect("вызов");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("Итог: PASS"), "{}", out.content);
        assert_eq!(case.file_name().expect("имя").to_string_lossy(), "case");
        let out = tool
            .call(json!({"dir": "ghost"}), &ctx)
            .await
            .expect("вызов");
        assert!(out.is_error, "несуществующий каталог — мягкая ошибка");
    }
}
