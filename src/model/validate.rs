//! Валидация модели: ссылочная целостность, дубли, циклы, полнота связей.
//!
//! Правила (ADR-003): битая ссылка — `error`; дубль ID — `error`; цикл
//! `depends_on` — `error`; `ADR` без затронутых `CMP` — `warn`; `NFR` без
//! способа проверки — `warn`; `unverifiable` без обоснования — `error`
//! (ADR-006); `QAS` с незаполненными полями сценария — `warn` (ADR-007).
//! Цели `verified_by` могут ссылаться на правила
//! `C-NNN` файла `CONSTRAINTS.yaml`, лежащего рядом с каталогом модели
//! (файл отсутствует — такие ссылки не проверяются).

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use regex::Regex;

use crate::model::graph::find_cycle;
use crate::model::parse::{LinkKind, Model};
use crate::model::{EntityKind, parse_id};

/// Критичность находки.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    /// Блокирующая (exit code 1).
    Error,
    /// Предупреждение.
    Warn,
}

impl std::fmt::Display for Severity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Severity::Error => "error",
            Severity::Warn => "warn",
        })
    }
}

/// Находка валидации.
#[derive(Debug)]
pub struct ModelIssue {
    /// Критичность.
    pub severity: Severity,
    /// Файл сущности (для находок уровня каталога — сам каталог).
    pub file: PathBuf,
    /// Код правила (`broken-link`, `duplicate-id`, …).
    pub rule: &'static str,
    /// Сообщение.
    pub message: String,
}

/// Отчёт валидации.
#[derive(Debug)]
pub struct ValidationReport {
    /// Каталог модели.
    pub dir: PathBuf,
    /// Число сущностей.
    pub entities: usize,
    /// Находки (отсортированы: file, rule).
    pub issues: Vec<ModelIssue>,
}

impl ValidationReport {
    /// Есть ли блокирующие находки.
    #[must_use]
    pub fn has_errors(&self) -> bool {
        self.issues.iter().any(|i| i.severity == Severity::Error)
    }

    /// Строка сводки для CLI.
    #[must_use]
    pub fn summary(&self) -> String {
        let errors = self
            .issues
            .iter()
            .filter(|i| i.severity == Severity::Error)
            .count();
        let warns = self.issues.len() - errors;
        format!(
            "Сущностей: {}, находок: {} (error: {errors}, warn: {warns})",
            self.entities,
            self.issues.len()
        )
    }
}

/// Состояние соседнего CONSTRAINTS.yaml для проверки ссылок `C-NNN`.
pub(crate) enum Constraints {
    /// Файла нет — ссылки на правила не проверяются.
    Absent,
    /// Набор ID правил (`id:` либо `name:`).
    Loaded(BTreeSet<String>),
    /// Файл есть, но не читается/не парсится.
    Broken(String),
}

/// Читает ID правил из `<model_dir>/../CONSTRAINTS.yaml`
/// (корни `constraints:` или `rules:`, элементы с `id`/`name`).
pub(crate) fn load_constraint_ids(model_dir: &Path) -> Constraints {
    let Some(parent) = model_dir.parent() else {
        return Constraints::Absent;
    };
    let path = parent.join("CONSTRAINTS.yaml");
    let text = match std::fs::read_to_string(&path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Constraints::Absent,
        Err(e) => return Constraints::Broken(format!("{}: {e}", path.display())),
    };
    let yaml: serde_yaml_ng::Value = match serde_yaml_ng::from_str(&text) {
        Ok(v) => v,
        Err(e) => return Constraints::Broken(format!("{}: {e}", path.display())),
    };
    let mut ids = BTreeSet::new();
    for root in ["constraints", "rules"] {
        if let Some(seq) = yaml.get(root).and_then(|v| v.as_sequence()) {
            for item in seq {
                // ID правила — `id`, для харнесc-формата (rules:) — `name`.
                if let Some(v) = item
                    .get("id")
                    .and_then(|v| v.as_str())
                    .or_else(|| item.get("name").and_then(|v| v.as_str()))
                {
                    ids.insert(v.to_string());
                }
            }
        }
    }
    Constraints::Loaded(ids)
}

/// Цель ссылки: сущность модели или правило CONSTRAINTS.yaml.
enum LinkTarget<'a> {
    /// Валидный ID сущности (`PREFIX-NNN`).
    Entity(&'a str),
    /// Ссылка на правило (`C-NNN`).
    Constraint(&'a str),
    /// Ни то, ни другое.
    Invalid(&'a str),
}

fn classify_target(raw: &str) -> LinkTarget<'_> {
    if parse_id(raw).is_some() {
        return LinkTarget::Entity(raw);
    }
    // Правило CONSTRAINTS.yaml: `C-` + номер.
    let re = Regex::new(r"^C-([0-9]+)$");
    match re {
        Ok(re) if re.is_match(raw) => LinkTarget::Constraint(raw),
        _ => LinkTarget::Invalid(raw),
    }
}

/// Проверяет модель, возвращает отчёт с находками.
#[must_use]
pub fn validate(model: &Model) -> ValidationReport {
    let mut issues = Vec::new();
    check_not_empty(model, &mut issues);
    check_ids(model, &mut issues);
    check_links(model, &mut issues);
    check_cycle(model, &mut issues);
    check_completeness(model, &mut issues);
    issues.sort_by(|a, b| {
        a.file
            .cmp(&b.file)
            .then(a.rule.cmp(b.rule))
            .then(a.message.cmp(&b.message))
    });
    ValidationReport {
        dir: model.dir.clone(),
        entities: model.entities.len(),
        issues,
    }
}

/// Добавляет находку в отчёт.
fn issue(
    issues: &mut Vec<ModelIssue>,
    severity: Severity,
    file: &Path,
    rule: &'static str,
    message: String,
) {
    issues.push(ModelIssue {
        severity,
        file: file.to_path_buf(),
        rule,
        message,
    });
}

/// Правило: модель не пуста.
fn check_not_empty(model: &Model, issues: &mut Vec<ModelIssue>) {
    if model.entities.is_empty() {
        issue(
            issues,
            Severity::Error,
            &model.dir,
            "empty-model",
            format!(
                "в каталоге {} нет ни одной сущности (*.md)",
                model.dir.display()
            ),
        );
    }
}

/// Правила: дубли ID, соответствие префикса ID типу, `unverifiable` обязан
/// нести непустое обоснование (ADR-006).
fn check_ids(model: &Model, issues: &mut Vec<ModelIssue>) {
    let mut seen: BTreeMap<&str, &Path> = BTreeMap::new();
    for e in &model.entities {
        if let Some(first) = seen.get(e.id.as_str()) {
            issue(
                issues,
                Severity::Error,
                &e.file,
                "duplicate-id",
                format!(
                    "дубль ID '{}' (первое вхождение: {})",
                    e.id,
                    first.display()
                ),
            );
        } else {
            seen.insert(e.id.as_str(), &e.file);
        }
        if e.unverifiable
            .as_deref()
            .is_some_and(|s| s.trim().is_empty())
        {
            issue(
                issues,
                Severity::Error,
                &e.file,
                "empty-unverifiable",
                format!("{}: `unverifiable` без обоснования", e.id),
            );
        }
        if let Some((kind, _)) = parse_id(&e.id) {
            if kind != e.kind {
                issue(
                    issues,
                    Severity::Error,
                    &e.file,
                    "id-type-mismatch",
                    format!(
                        "префикс ID '{}' ({}) не соответствует type '{}'",
                        e.id,
                        kind.prefix(),
                        e.kind.type_str()
                    ),
                );
            }
        }
    }
}

/// Правила: существование целей ссылок, допустимость `C-NNN` только
/// в `verified_by`, существование правила в соседнем CONSTRAINTS.yaml.
fn check_links(model: &Model, issues: &mut Vec<ModelIssue>) {
    let constraints = load_constraint_ids(&model.dir);
    if let Constraints::Broken(why) = &constraints {
        issue(
            issues,
            Severity::Error,
            &model.dir,
            "constraints-unreadable",
            format!("CONSTRAINTS.yaml рядом с моделью не читается: {why}"),
        );
    }
    for e in &model.entities {
        for kind in LinkKind::ALL {
            for raw in e.link_targets(kind) {
                check_link_target(model, &constraints, e, kind, raw, issues);
            }
        }
    }
}

/// Одна цель ссылки.
fn check_link_target(
    model: &Model,
    constraints: &Constraints,
    e: &crate::model::parse::Entity,
    kind: LinkKind,
    raw: &str,
    issues: &mut Vec<ModelIssue>,
) {
    match classify_target(raw) {
        LinkTarget::Entity(id) => {
            if model.get(id).is_none() {
                issue(
                    issues,
                    Severity::Error,
                    &e.file,
                    "broken-link",
                    format!("{}: '{}' — сущности нет в модели", kind.field_name(), id),
                );
            }
        }
        LinkTarget::Constraint(cid) => {
            if kind != LinkKind::VerifiedBy {
                issue(
                    issues,
                    Severity::Error,
                    &e.file,
                    "bad-link-target",
                    format!(
                        "{}: '{}' — ссылки на правила CONSTRAINTS.yaml допустимы только в verified_by",
                        kind.field_name(),
                        cid
                    ),
                );
            } else if let Constraints::Loaded(ids) = constraints {
                if !ids.contains(cid) {
                    issue(
                        issues,
                        Severity::Error,
                        &e.file,
                        "broken-link",
                        format!("verified_by: '{cid}' — нет такого правила в CONSTRAINTS.yaml"),
                    );
                }
            }
        }
        LinkTarget::Invalid(raw) => {
            issue(
                issues,
                Severity::Error,
                &e.file,
                "bad-link-target",
                format!(
                    "{}: '{raw}' — не ID сущности ({}) и не правило C-NNN",
                    kind.field_name(),
                    EntityKind::prefixes().join(", ")
                ),
            );
        }
    }
}

/// Правило: ацикличность `depends_on`.
fn check_cycle(model: &Model, issues: &mut Vec<ModelIssue>) {
    if let Some(cycle) = find_cycle(model) {
        let file = model
            .get(&cycle[0])
            .map_or_else(|| model.dir.clone(), |e| e.file.clone());
        issue(
            issues,
            Severity::Error,
            &file,
            "dependency-cycle",
            format!("цикл depends_on: {}", cycle.join(" → ")),
        );
    }
}

/// Правила полноты (предупреждения): `ADR` затрагивает `CMP`, у `NFR`
/// есть способ проверки, у `QAS` заполнены все поля сценария (ADR-007).
fn check_completeness(model: &Model, issues: &mut Vec<ModelIssue>) {
    for e in &model.entities {
        if e.kind == EntityKind::Adr {
            let touches_cmp = e
                .affects
                .iter()
                .any(|t| model.get(t).is_some_and(|c| c.kind == EntityKind::Cmp));
            if !touches_cmp {
                issue(
                    issues,
                    Severity::Warn,
                    &e.file,
                    "adr-without-component",
                    format!(
                        "{} не затрагивает ни одного компонента (affects → CMP-*)",
                        e.id
                    ),
                );
            }
        }
        if e.kind == EntityKind::Nfr && e.verification.as_deref().is_none_or(str::is_empty) {
            issue(
                issues,
                Severity::Warn,
                &e.file,
                "nfr-without-verification",
                format!("{} без способа проверки (поле verification)", e.id),
            );
        }
        if e.kind == EntityKind::Qas {
            let fields: [(&str, &Option<String>); 5] = [
                ("source", &e.source),
                ("stimulus", &e.stimulus),
                ("artifact", &e.artifact),
                ("response", &e.response),
                ("measure", &e.measure),
            ];
            let missing: Vec<&str> = fields
                .iter()
                .filter(|(_, v)| v.as_deref().is_none_or(|s| s.trim().is_empty()))
                .map(|(name, _)| *name)
                .collect();
            if !missing.is_empty() {
                issue(
                    issues,
                    Severity::Warn,
                    &e.file,
                    "qas-incomplete",
                    format!(
                        "{}: сценарий атрибута качества без полей: {}",
                        e.id,
                        missing.join(", ")
                    ),
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::parse::load_model;

    fn entity(dir: &Path, name: &str, frontmatter: &str) {
        std::fs::write(dir.join(name), format!("{frontmatter}\n---\n\nТело.\n")).expect("фикстура");
    }

    fn base(dir: &Path) {
        entity(
            dir,
            "CMP-001-gw.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: Gateway\nstatus: adopted",
        );
        entity(
            dir,
            "ADR-001-x.md",
            "---\nid: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted\naffects: [CMP-001]",
        );
        entity(
            dir,
            "NFR-001-lat.md",
            "---\nid: NFR-001\ntype: nfr\ntitle: Latency\nstatus: accepted\nverification: histogram p99",
        );
    }

    fn validate_dir(dir: &Path) -> ValidationReport {
        let m = load_model(dir).expect("модель");
        validate(&m)
    }

    #[test]
    fn valid_model_has_no_issues() {
        let dir = tempfile::tempdir().expect("tmp");
        base(dir.path());
        let report = validate_dir(dir.path());
        assert!(report.issues.is_empty(), "{:?}", report.summary());
        assert!(!report.has_errors());
        assert_eq!(report.entities, 3);
    }

    #[test]
    fn broken_link_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        base(dir.path());
        entity(
            dir.path(),
            "CMP-002-orch.md",
            "---\nid: CMP-002\ntype: cmp\ntitle: Orch\nstatus: adopted\ndepends_on: [CMP-999]",
        );
        let report = validate_dir(dir.path());
        let found = report
            .issues
            .iter()
            .any(|i| i.rule == "broken-link" && i.message.contains("CMP-999"));
        assert!(found, "{}", report.summary());
        assert!(report.has_errors());
    }

    #[test]
    fn duplicate_id_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        base(dir.path());
        entity(
            dir.path(),
            "CMP-900-copy.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: Копия\nstatus: adopted",
        );
        let report = validate_dir(dir.path());
        assert!(
            report.issues.iter().any(|i| i.rule == "duplicate-id"),
            "{}",
            report.summary()
        );
    }

    #[test]
    fn depends_on_cycle_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "CMP-001-a.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: A\nstatus: s\ndepends_on: [CMP-002]",
        );
        entity(
            dir.path(),
            "CMP-002-b.md",
            "---\nid: CMP-002\ntype: cmp\ntitle: B\nstatus: s\ndepends_on: [CMP-001]",
        );
        let report = validate_dir(dir.path());
        let cycle = report
            .issues
            .iter()
            .find(|i| i.rule == "dependency-cycle")
            .expect("цикл");
        assert!(cycle.message.contains("CMP-001"), "{}", cycle.message);
        assert!(cycle.message.contains('→'), "{}", cycle.message);
    }

    #[test]
    fn adr_without_cmp_is_warn() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "ADR-001-x.md",
            "---\nid: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted",
        );
        let report = validate_dir(dir.path());
        let issue = report
            .issues
            .iter()
            .find(|i| i.rule == "adr-without-component")
            .expect("warn");
        assert_eq!(issue.severity, Severity::Warn);
        assert!(!report.has_errors(), "warn не ломает сборку");
    }

    #[test]
    fn adr_with_only_non_cmp_affects_is_warn() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "AD-1-x.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED",
        );
        entity(
            dir.path(),
            "ADR-001-x.md",
            "---\nid: ADR-001\ntype: adr\ntitle: Решение\nstatus: Accepted\naffects: [AD-1]",
        );
        let report = validate_dir(dir.path());
        assert!(
            report
                .issues
                .iter()
                .any(|i| i.rule == "adr-without-component"),
            "affects на AD не считается затронутым CMP: {}",
            report.summary()
        );
    }

    #[test]
    fn nfr_without_verification_is_warn() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "NFR-001-x.md",
            "---\nid: NFR-001\ntype: nfr\ntitle: Latency\nstatus: accepted",
        );
        let report = validate_dir(dir.path());
        let issue = report
            .issues
            .iter()
            .find(|i| i.rule == "nfr-without-verification")
            .expect("warn");
        assert_eq!(issue.severity, Severity::Warn);
    }

    #[test]
    fn qas_incomplete_is_warn_complete_is_clean() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "QAS-001-x.md",
            "---\nid: QAS-001\ntype: qas\ntitle: Сценарий\nstatus: accepted\n\
             source: клиент\nstimulus: пик\nartifact: CMP-001\nresponse: ответ",
        );
        let report = validate_dir(dir.path());
        let issue = report
            .issues
            .iter()
            .find(|i| i.rule == "qas-incomplete")
            .expect("warn о неполном QAS");
        assert_eq!(issue.severity, Severity::Warn);
        assert!(issue.message.contains("measure"), "{}", issue.message);
        assert!(!report.has_errors(), "warn не ломает сборку");

        // Полный сценарий — без находок.
        let dir2 = tempfile::tempdir().expect("tmp");
        entity(
            dir2.path(),
            "QAS-001-x.md",
            "---\nid: QAS-001\ntype: qas\ntitle: Сценарий\nstatus: accepted\n\
             source: клиент\nstimulus: пик\nartifact: CMP-001\nresponse: ответ\n\
             measure: p99 < 2s",
        );
        let report2 = validate_dir(dir2.path());
        assert!(report2.issues.is_empty(), "{}", report2.summary());
    }

    #[test]
    fn empty_dir_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let report = validate_dir(dir.path());
        let issue = report
            .issues
            .iter()
            .find(|i| i.rule == "empty-model")
            .expect("error");
        assert_eq!(issue.severity, Severity::Error);
        assert!(report.has_errors());
    }

    #[test]
    fn id_type_mismatch_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "CMP-001-x.md",
            "---\nid: CMP-001\ntype: nfr\ntitle: Не тот тип\nstatus: s\nverification: v",
        );
        let report = validate_dir(dir.path());
        assert!(
            report.issues.iter().any(|i| i.rule == "id-type-mismatch"),
            "{}",
            report.summary()
        );
    }

    #[test]
    fn invalid_link_target_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        base(dir.path());
        entity(
            dir.path(),
            "CMP-002-x.md",
            "---\nid: CMP-002\ntype: cmp\ntitle: X\nstatus: s\ndepends_on: [куда-то]",
        );
        let report = validate_dir(dir.path());
        assert!(
            report.issues.iter().any(|i| i.rule == "bad-link-target"),
            "{}",
            report.summary()
        );
    }

    #[test]
    fn constraint_refs_checked_against_constraints_yaml() {
        let dir = tempfile::tempdir().expect("tmp");
        let case = dir.path().join("case");
        let model_dir = case.join("model");
        std::fs::create_dir_all(&model_dir).expect("dirs");
        std::fs::write(
            case.join("CONSTRAINTS.yaml"),
            "constraints:\n  - id: C-001\n    name: правило\n",
        )
        .expect("constraints");
        entity(
            &model_dir,
            "AD-1-x.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\nverified_by: [C-001]",
        );
        let report = validate_dir(&model_dir);
        assert!(report.issues.is_empty(), "{}", report.summary());

        // Несуществующее правило — error.
        std::fs::write(
            model_dir.join("AD-2-y.md"),
            "---\nid: AD-2\ntype: ad\ntitle: Другой\nstatus: ADOPTED\nverified_by: [C-099]\n---\n",
        )
        .expect("фикстура");
        let report = validate_dir(&model_dir);
        let found = report
            .issues
            .iter()
            .any(|i| i.rule == "broken-link" && i.message.contains("C-099"));
        assert!(found, "{}", report.summary());
    }

    #[test]
    fn constraint_refs_skipped_without_constraints_file() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "AD-1-x.md",
            "---\nid: AD-1\ntype: ad\ntitle: Инвариант\nstatus: ADOPTED\nverified_by: [C-777]",
        );
        let report = validate_dir(dir.path());
        assert!(
            report.issues.is_empty(),
            "без CONSTRAINTS.yaml ссылки C-NNN не проверяются: {}",
            report.summary()
        );
    }

    #[test]
    fn constraint_ref_outside_verified_by_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "CMP-001-x.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: X\nstatus: s\ndepends_on: [C-001]",
        );
        let report = validate_dir(dir.path());
        assert!(
            report.issues.iter().any(|i| i.rule == "bad-link-target"),
            "{}",
            report.summary()
        );
    }

    #[test]
    fn unverifiable_with_justification_is_valid() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "AD-1-x.md",
            "---\nid: AD-1\ntype: ad\ntitle: Практика\nstatus: ADOPTED\nunverifiable: проверяется регламентом, не кодом",
        );
        let report = validate_dir(dir.path());
        assert!(report.issues.is_empty(), "{}", report.summary());
    }

    #[test]
    fn unverifiable_without_justification_is_error() {
        let dir = tempfile::tempdir().expect("tmp");
        entity(
            dir.path(),
            "AD-1-x.md",
            "---\nid: AD-1\ntype: ad\ntitle: Практика\nstatus: ADOPTED\nunverifiable: \"  \"",
        );
        let report = validate_dir(dir.path());
        let issue = report
            .issues
            .iter()
            .find(|i| i.rule == "empty-unverifiable")
            .expect("error");
        assert_eq!(issue.severity, Severity::Error);
    }
}
