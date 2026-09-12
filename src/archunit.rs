//! `ArchUnit`-мост: JVM-гейты из единого источника (ADR-039).
//!
//! Архитектор описывает структурные правила один раз — в `CONSTRAINTS.yaml`
//! (`dependency_direction`, `context_boundary`, чей glob целится в
//! `**/*.java`) и/или в типизированной модели (`model/`, CMP с `code_roots`).
//! Этот модуль проецирует их в стек-нейтральный спек [`ArchUnitSpec`] и
//! дальше в два исполняемых артефакта:
//!
//! 1. `ArchFitnessTest.java` — идиоматичный `JUnit` 5 + `ArchUnit` тест для
//!    встраивания в JVM-репо командой (`arch-ml archunit gen`);
//! 2. standalone-гейт (`arch-ml archunit check`): генерирует
//!    `ArchGateRunner.java` (main без `JUnit`, правила захардкожены из спека —
//!    без JSON-парсинга на стороне Java), компилирует его javac'ом в кэш
//!    `~/.arch-ml/archunit/runner` и запускает на скомпилированных
//!    классах проекта. Вывод раннера парсится в находки общего отчёта.
//!
//! Маппинг YAML → `ArchUnit` (эвристики задокументированы, ADR-039):
//!
//! - file-glob → пакетный паттерн `ArchUnit`: из glob'а вида
//!   `src/**/domain/**/*.java` берутся «конкретные» сегменты после
//!   известных исходных префиксов (`src/`, `src/main/java/`,
//!   `src/main/kotlin/`, `app/src/main/java/`); wildcard-сегменты (`**`,
//!   `*`) отбрасываются; если до конкретных сегментов был `**` — паттерн
//!   свободный (`..domain..`), иначе якорный (`com.acme.domain..`).
//!   Glob `**/*.java` без конкретных сегментов → `..` (любой пакет);
//! - запись forbid/allow (`модуль/в/координатах/слешей`) → пакетный паттерн:
//!   слеши → точки; последний сегмент с заглавной буквы считается именем
//!   класса и отбрасывается; многосегментная запись → якорный паттерн
//!   (`com.acme.infrastructure..`), одиночный сегмент → свободный
//!   (`..infrastructure..`);
//! - `dependency_direction` forbid →
//!   `noClasses().that().resideInAPackage(from).should().dependOnClassesThat().resideInAnyPackage(to)`;
//! - `dependency_direction` allow →
//!   `classes().that().resideInAPackage(from).should().onlyDependOnClassesThat().resideInAnyPackage(allow + from + JDK-белый-список)` —
//!   allow-список CONSTRAINTS описывает ВНУТРЕННИЕ модули, поэтому JDK/
//!   стандартные пакеты (`java..`, `javax..`, `jakarta..`, …) добавляются
//!   автоматически ([`JDK_ALLOWLIST`]);
//! - `context_boundary` (ADR-030) → для каждой пары CMP без `depends_on`
//!   правило `ForbiddenDeps` из пакета источника в пакет цели; пакет CMP
//!   выводится из `code_roots` тем же отрезанием исходных префиксов.
//!
//! Правила, которые не маппятся (не-java glob, невыводимый пакет,
//! некорректный forbid/allow), НЕ роняют генерацию — попадают в секцию
//! `unsupported` спека с причиной и печатаются предупреждением.
//!
//! Fail-closed диагностика `check`: нет `java`/`javac`, нет jar'ов `ArchUnit`
//! (подсказка `arch-ml archunit fetch`), нет скомпилированных классов
//! (подсказка `mvn compile`/`javac`), пустой спек, таймаут — всё это
//! ошибки с понятным текстом, а не молчаливый PASS.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::control::{FitnessRule, LintIssue, RuleKind, normalize_severity};
use crate::error::{HarnessError, Result};

/// Пин версий jar'ов `ArchUnit`-рантайма (supply-chain, ADR-039): версия и
/// SHA-256 зафиксированы в коде и в `docs/archunit.md`; `archunit fetch`
/// проверяет хэш после скачивания и отказывается писать файл при расхождении.
pub struct PinnedJar {
    /// Имя файла в каталоге lib.
    pub file: &'static str,
    /// URL на Maven Central.
    pub url: &'static str,
    /// Ожидаемый SHA-256 (hex, lowercase).
    pub sha256: &'static str,
}

/// Пинnutый набор: `ArchUnit` core + slf4j-api (обязателен в runtime —
/// `ClassFileImporter` логирует через slf4j) + slf4j-nop (тихий биндинг,
/// чтобы гейт не шумел логами). Версии сняты с maven-metadata 2026-09-04:
/// archunit 1.5.0 — текущий <release>; slf4j 2.0.19 — последний стабильный
/// 2.0.x (latest 2.1.0-alpha1 — предрелиз, не берём).
pub const PINNED_JARS: &[PinnedJar] = &[
    PinnedJar {
        file: "archunit-1.5.0.jar",
        url: "https://repo1.maven.org/maven2/com/tngtech/archunit/archunit/1.5.0/archunit-1.5.0.jar",
        sha256: "5ab139643fa5090af181ff8dc9eab48cb38ca306d3e03e66acacc75831a08fe9",
    },
    PinnedJar {
        file: "slf4j-api-2.0.19.jar",
        url: "https://repo1.maven.org/maven2/org/slf4j/slf4j-api/2.0.19/slf4j-api-2.0.19.jar",
        sha256: "e91ff6d720609e7a194ffe758c3ed5c84e798617ae07b0a0f6a4fe229741b4bb",
    },
    PinnedJar {
        file: "slf4j-nop-2.0.19.jar",
        url: "https://repo1.maven.org/maven2/org/slf4j/slf4j-nop/2.0.19/slf4j-nop-2.0.19.jar",
        sha256: "d0226062f9b3a2793f62002f892a8e624eb07f396130fc949e01b1d5f4888cba",
    },
];

/// Белый список JDK/стандартных пакетов для allow-правил (см. модульный
/// комментарий): allow в CONSTRAINTS описывает внутренние модули, а не
/// стандартную библиотеку.
const JDK_ALLOWLIST: &[&str] = &[
    "java..",
    "javax..",
    "jakarta..",
    "jdk..",
    "sun..",
    "com.sun..",
    "org.w3c..",
    "org.xml..",
    "kotlin..",
    "scala..",
    "groovy..",
    "org.jetbrains..",
];

/// Дефолтный таймаут standalone-гейта, секунд (импорт классов `ArchUnit`'ом
/// на больших репозиториях заметно дольше command-правил).
pub const DEFAULT_GATE_TIMEOUT_SECS: u64 = 300;

/// Таймаут компиляции раннера javac'ом, секунд.
const COMPILE_TIMEOUT_SECS: u64 = 120;

/// Известные исходные префиксы JVM-репозиториев (отрезаются при выводе
/// пакетного паттерна из file-glob'а или `code_roots`).
const SOURCE_PREFIXES: &[&[&str]] = &[
    &["app", "src", "main", "java"],
    &["src", "main", "java"],
    &["src", "main", "kotlin"],
    &["src"],
];

/// Кандидаты авто-детекта скомпилированных классов (Maven, Gradle, `IntelliJ`,
/// голый javac). Первый существующий каталог с хотя бы одним `.class` побеждает.
const CLASSES_CANDIDATES: &[&str] = &[
    "target/classes",
    "build/classes/java/main",
    "out/production",
    "out/production/classes",
    "classes",
];

// ---------------------------------------------------------------------------
// Спек правил (стек-нейтральная модель)
// ---------------------------------------------------------------------------

/// Стек-нейтральный спек `ArchUnit`-правил: результат проекции `CONSTRAINTS.yaml`
/// (+ модели) перед рендером в Java-артефакты.
#[derive(Debug, Clone, Serialize)]
pub struct ArchUnitSpec {
    /// Версия схемы `archunit-rules.json`.
    pub version: u32,
    /// Базовый пакет для `@AnalyzeClasses` (выводится из якорных паттернов;
    /// `None` — сканировать всё, `..`).
    pub base_package: Option<String>,
    /// Смапленные правила.
    pub rules: Vec<SpecRule>,
    /// Правила, которые не удалось смаппить (с причиной).
    pub unsupported: Vec<UnsupportedRule>,
}

/// Одно правило спека.
#[derive(Debug, Clone, Serialize)]
pub struct SpecRule {
    /// Идентификатор правила CONSTRAINTS (`id`, иначе `name`) — трассировка
    /// нарушения обратно в источник.
    pub id: String,
    /// Человекочитаемое описание (id + имя + смысл) — в сообщение `ArchUnit`.
    pub description: String,
    /// Нормализованная критичность находок: error|warn.
    pub severity: String,
    /// Вид проверки.
    pub kind: SpecRuleKind,
}

/// Вид правила спека.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SpecRuleKind {
    /// Запрет зависимостей: классы пакета `from` не зависят от пакетов `to`.
    ForbiddenDeps {
        /// Пакетный паттерн источника (например, `..domain..`).
        from: String,
        /// Запрещённые целевые пакетные паттерны.
        to: Vec<String>,
    },
    /// Белый список: классы пакета `from` зависят только от `allowed`.
    AllowedDepsOnly {
        /// Пакетный паттерн источника.
        from: String,
        /// Разрешённые пакетные паттерны (включая `from` и JDK-белый список).
        allowed: Vec<String>,
    },
}

/// Правило, не смаплившееся в спек (не роняет генерацию).
#[derive(Debug, Clone, Serialize)]
pub struct UnsupportedRule {
    /// Имя правила CONSTRAINTS.
    pub rule: String,
    /// Причина.
    pub reason: String,
}

/// Идентификатор правила для спека и сообщений: `id` (C-12) при наличии,
/// иначе `name`.
fn rule_id(rule: &FitnessRule) -> String {
    rule.id.clone().unwrap_or_else(|| rule.name.clone())
}

/// Строит спек `ArchUnit` из правил `CONSTRAINTS.yaml`.
///
/// Берутся `dependency_direction` и `context_boundary`, чей glob целится в
/// `**/*.java`; для `context_boundary` дополнительно нужна модель
/// (`model_dir_override` → `rule.model_dir` → `model`). Остальные типы
/// правил `ArchUnit`-мосту не относятся и пропускаются молча.
pub fn spec_from_constraints(
    repo: &Path,
    rules: &[&FitnessRule],
    model_dir_override: Option<&Path>,
) -> ArchUnitSpec {
    let mut spec = ArchUnitSpec {
        version: 1,
        base_package: None,
        rules: Vec::new(),
        unsupported: Vec::new(),
    };
    // Кэш загруженных моделей по каталогу (несколько context_boundary-правил
    // с одним model_dir не перечитывают диск).
    let mut models: std::collections::BTreeMap<PathBuf, Option<crate::model::Model>> =
        std::collections::BTreeMap::new();

    for rule in rules {
        match rule.kind {
            RuleKind::DependencyDirection => map_dependency_direction(rule, &mut spec),
            RuleKind::ContextBoundary => {
                let dir = model_dir_override.map_or_else(
                    || repo.join(rule.model_dir.as_deref().unwrap_or("model")),
                    Path::to_path_buf,
                );
                let model = models
                    .entry(dir.clone())
                    .or_insert_with(|| crate::model::load_model(&dir).ok());
                map_context_boundary(rule, model.as_ref(), &mut spec);
            }
            _ => {}
        }
    }
    spec.rules
        .sort_by(|a, b| a.id.cmp(&b.id).then(a.description.cmp(&b.description)));
    spec.rules
        .dedup_by(|a, b| a.id == b.id && a.description == b.description);
    spec.base_package = derive_base_package(&spec.rules);
    spec
}

/// Маппинг `dependency_direction` (ADR-029) в правила спека.
fn map_dependency_direction(rule: &FitnessRule, spec: &mut ArchUnitSpec) {
    let id = rule_id(rule);
    let Some(from) = rule.glob.iter().find_map(|g| glob_to_package_pattern(g)) else {
        spec.unsupported.push(UnsupportedRule {
            rule: rule.name.clone(),
            reason: "glob не целится в **/*.java — не JVM-правило".into(),
        });
        return;
    };
    let severity = normalize_severity(&rule.severity, &rule.name).unwrap_or("error");
    match (&rule.forbid, &rule.allow) {
        (Some(forbid), None) => {
            let mut to = Vec::new();
            let mut bad = Vec::new();
            for entry in forbid {
                match entry_to_package_pattern(entry) {
                    Some(p) => to.push(p),
                    None => bad.push(entry.clone()),
                }
            }
            if !bad.is_empty() {
                spec.unsupported.push(UnsupportedRule {
                    rule: rule.name.clone(),
                    reason: format!(
                        "не удалось вывести пакет из forbid-записей: {}",
                        bad.join(", ")
                    ),
                });
                return;
            }
            spec.rules.push(SpecRule {
                id: id.clone(),
                description: format!(
                    "[{id}] {}: {from} не зависит от {}",
                    rule.name,
                    to.join(", ")
                ),
                severity: severity.into(),
                kind: SpecRuleKind::ForbiddenDeps { from, to },
            });
        }
        (None, Some(allow)) => {
            let mut allowed = Vec::new();
            let mut bad = Vec::new();
            for entry in allow {
                match entry_to_package_pattern(entry) {
                    Some(p) => allowed.push(p),
                    None => bad.push(entry.clone()),
                }
            }
            if !bad.is_empty() {
                spec.unsupported.push(UnsupportedRule {
                    rule: rule.name.clone(),
                    reason: format!(
                        "не удалось вывести пакет из allow-записей: {}",
                        bad.join(", ")
                    ),
                });
                return;
            }
            // allow в CONSTRAINTS описывает внутренние модули: свой пакет и
            // JDK добавляются автоматически (см. модульный комментарий).
            allowed.push(from.clone());
            allowed.extend(JDK_ALLOWLIST.iter().map(|s| (*s).to_string()));
            spec.rules.push(SpecRule {
                id: id.clone(),
                description: format!(
                    "[{id}] {}: {from} зависит только от белого списка",
                    rule.name
                ),
                severity: severity.into(),
                kind: SpecRuleKind::AllowedDepsOnly { from, allowed },
            });
        }
        _ => {
            spec.unsupported.push(UnsupportedRule {
                rule: rule.name.clone(),
                reason: "для dependency_direction нужно ровно одно из forbid/allow".into(),
            });
        }
    }
}

/// Контекст CMP для маппинга `context_boundary`: id + `depends_on` + пакет.
struct CtxInfo<'m> {
    /// Идентификатор CMP.
    id: &'m str,
    /// `depends_on` из модели.
    depends_on: &'m [String],
    /// Выведенный пакетный паттерн.
    package: String,
}

/// Маппинг `context_boundary` (ADR-030) в правила `ForbiddenDeps`.
fn map_context_boundary(
    rule: &FitnessRule,
    model: Option<&crate::model::Model>,
    spec: &mut ArchUnitSpec,
) {
    if !rule
        .glob
        .iter()
        .any(|g| glob_to_package_pattern(g).is_some())
    {
        spec.unsupported.push(UnsupportedRule {
            rule: rule.name.clone(),
            reason: "glob не целится в **/*.java — не JVM-правило".into(),
        });
        return;
    }
    let Some(model) = model else {
        spec.unsupported.push(UnsupportedRule {
            rule: rule.name.clone(),
            reason: "модель недоступна (каталог model/ не найден или не читается)".into(),
        });
        return;
    };
    let id = rule_id(rule);
    let severity = normalize_severity(&rule.severity, &rule.name).unwrap_or("error");
    let mut contexts: Vec<CtxInfo<'_>> = Vec::new();
    let mut skipped: Vec<String> = Vec::new();
    for e in &model.entities {
        if e.kind != crate::model::EntityKind::Cmp || e.code_roots.is_empty() {
            continue;
        }
        match e.code_roots.iter().find_map(|r| root_to_package(r)) {
            Some(package) => contexts.push(CtxInfo {
                id: &e.id,
                depends_on: &e.depends_on,
                package,
            }),
            None => skipped.push(e.id.clone()),
        }
    }
    if !skipped.is_empty() {
        spec.unsupported.push(UnsupportedRule {
            rule: rule.name.clone(),
            reason: format!(
                "не удалось вывести java-пакет из code_roots CMP: {}",
                skipped.join(", ")
            ),
        });
    }
    spec.rules
        .extend(context_boundary_rules(&id, &rule.name, severity, &contexts));
}

/// Чистое ядро маппинга `context_boundary`: для каждой упорядоченной пары
/// CMP (источник → цель) без `depends_on` — запрет зависимости.
fn context_boundary_rules(
    id: &str,
    name: &str,
    severity: &str,
    contexts: &[CtxInfo<'_>],
) -> Vec<SpecRule> {
    let mut out = Vec::new();
    for source in contexts {
        for target in contexts {
            if source.id == target.id
                || source.depends_on.iter().any(|d| d == target.id)
                || source.package == target.package
            {
                continue;
            }
            out.push(SpecRule {
                id: id.to_string(),
                description: format!(
                    "[{id}] {name}: {} → {} без depends_on в модели",
                    source.id, target.id
                ),
                severity: severity.into(),
                kind: SpecRuleKind::ForbiddenDeps {
                    from: source.package.clone(),
                    to: vec![target.package.clone()],
                },
            });
        }
    }
    out
}

/// File-glob → пакетный паттерн `ArchUnit` (эвристика, см. модульный
/// комментарий). `None` — glob не про java-файлы.
fn glob_to_package_pattern(glob: &str) -> Option<String> {
    if !glob.contains(".java") {
        return None;
    }
    let mut segments: Vec<&str> = glob.split('/').collect();
    // Отбрасываем файловый сегмент (`*.java`) и хвостовые wildcard'ы.
    while segments
        .last()
        .is_some_and(|s| s.contains(".java") || *s == "**" || *s == "*")
    {
        segments.pop();
    }
    for prefix in SOURCE_PREFIXES {
        if segments.len() >= prefix.len() && segments[..prefix.len()] == **prefix {
            segments.drain(..prefix.len());
            break;
        }
    }
    let anchored = !segments.first().is_some_and(|s| *s == "**" || *s == "*");
    let concrete: Vec<&str> = segments
        .into_iter()
        .filter(|s| *s != "**" && *s != "*")
        .collect();
    if concrete.is_empty() {
        return Some("..".to_string());
    }
    let joined = concrete.join(".");
    Some(if anchored {
        format!("{joined}..")
    } else {
        format!("..{joined}..")
    })
}

/// Запись forbid/allow (`com/acme/infrastructure` или `infrastructure`) →
/// пакетный паттерн `ArchUnit` (эвристика, см. модульный комментарий).
fn entry_to_package_pattern(entry: &str) -> Option<String> {
    let normalized = entry.trim().replace('.', "/");
    let mut segments: Vec<&str> = normalized.split('/').filter(|s| !s.is_empty()).collect();
    // Последний сегмент с заглавной буквы — имя класса, не пакета.
    if segments
        .last()
        .is_some_and(|s| s.chars().next().is_some_and(char::is_uppercase))
    {
        segments.pop();
    }
    if segments.is_empty() {
        return None;
    }
    if segments.iter().any(|s| !is_java_ident(s)) {
        return None;
    }
    Some(if segments.len() == 1 {
        format!("..{}..", segments[0])
    } else {
        format!("{}..", segments.join("."))
    })
}

/// Сегмент — валидный Java-идентификатор пакета (lowercase-эвристику не
/// enforce'им: пакеты со смешанным регистром встречаются в legacy).
fn is_java_ident(segment: &str) -> bool {
    let mut chars = segment.chars();
    chars
        .next()
        .is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// `code_roots` CMP → якорный пакетный паттерн (отрезание исходных
/// префиксов; `None` — пакет не выводится).
fn root_to_package(root: &str) -> Option<String> {
    let trimmed = root.trim().trim_start_matches("./");
    let mut segments: Vec<&str> = trimmed
        .split('/')
        .filter(|s| !s.is_empty() && *s != "**" && *s != "*")
        .collect();
    for prefix in SOURCE_PREFIXES {
        if segments.len() >= prefix.len() && segments[..prefix.len()] == **prefix {
            segments.drain(..prefix.len());
            break;
        }
    }
    if segments.is_empty() || !segments.iter().all(|s| is_java_ident(s)) {
        return None;
    }
    Some(format!("{}..", segments.join(".")))
}

/// Вывод базового пакета для `@AnalyzeClasses`: общий префикс якорных
/// (не начинающихся с `..`) пакетных паттернов спека. Свободные паттерны
/// (`..domain..`) базу не сужают. `None` — якорных нет (сканировать всё).
fn derive_base_package(rules: &[SpecRule]) -> Option<String> {
    fn anchored(pattern: &str) -> Option<String> {
        let p = pattern.trim_end_matches("..");
        // JDK-белый список allow-правил базу не сужает.
        (!p.starts_with("..") && !p.is_empty() && !JDK_ALLOWLIST.contains(&pattern))
            .then(|| p.to_string())
    }
    let mut anchors: Vec<String> = Vec::new();
    for rule in rules {
        match &rule.kind {
            SpecRuleKind::ForbiddenDeps { from, to } => {
                anchors.extend(anchored(from));
                anchors.extend(to.iter().filter_map(|p| anchored(p)));
            }
            SpecRuleKind::AllowedDepsOnly { from, allowed } => {
                anchors.extend(anchored(from));
                anchors.extend(allowed.iter().filter_map(|p| anchored(p)));
            }
        }
    }
    anchors.sort();
    anchors.dedup();
    let first = anchors.first()?.clone();
    if anchors.len() == 1 {
        // Единственный якорь — обычно пакет слоя (`com.acme.infrastructure`):
        // сканировать только его нельзя (классы других слоёв не попадут в
        // анализ), поэтому базой становится родительский пакет.
        let mut segs: Vec<&str> = first.split('.').collect();
        segs.pop();
        return (!segs.is_empty()).then(|| segs.join("."));
    }
    let mut prefix: Vec<&str> = first.split('.').collect();
    for other in &anchors[1..] {
        let segs: Vec<&str> = other.split('.').collect();
        let common = prefix
            .iter()
            .zip(segs.iter())
            .take_while(|(a, b)| a == b)
            .count();
        prefix.truncate(common);
    }
    (!prefix.is_empty()).then(|| prefix.join("."))
}

// ---------------------------------------------------------------------------
// Рендер артефактов
// ---------------------------------------------------------------------------

/// Экранирование строкового литерала Java.
fn java_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            _ => out.push(c),
        }
    }
    out
}

/// Идентификатор Java-поля/метода из id правила (`C-01` → `C_01`).
fn java_ident(id: &str) -> String {
    let mut out: String = id
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect();
    if out.chars().next().is_some_and(|c| c.is_ascii_digit()) {
        out.insert(0, '_');
    }
    if out.is_empty() {
        out.push_str("rule");
    }
    out
}

/// Тело `ArchUnit`-правила (общее для `JUnit`-теста и standalone-раннера).
fn rule_expr(kind: &SpecRuleKind) -> String {
    fn quoted(patterns: &[String]) -> String {
        patterns
            .iter()
            .map(|p| format!("\"{}\"", java_escape(p)))
            .collect::<Vec<_>>()
            .join(", ")
    }
    match kind {
        SpecRuleKind::ForbiddenDeps { from, to } => format!(
            "noClasses().that().resideInAPackage(\"{}\")\n            .should().dependOnClassesThat().resideInAnyPackage({})",
            java_escape(from),
            quoted(to)
        ),
        SpecRuleKind::AllowedDepsOnly { from, allowed } => format!(
            "classes().that().resideInAPackage(\"{}\")\n            .should().onlyDependOnClassesThat().resideInAnyPackage({})",
            java_escape(from),
            quoted(allowed)
        ),
    }
}

/// Рендер `ArchFitnessTest.java` — идиоматичный `JUnit` 5 + `ArchUnit` тест для
/// встраивания в JVM-репо (нужна зависимость `archunit-junit5`).
#[must_use]
pub fn render_junit_test(spec: &ArchUnitSpec) -> String {
    let base = spec.base_package.as_deref().unwrap_or("..");
    let mut out = String::new();
    let _ = writeln!(
        out,
        "// СГЕНЕРИРОВАНО: arch-ml archunit gen (ADR-039). Не редактировать вручную —\n\
         // источник правил: CONSTRAINTS.yaml; перегенерация перезапишет файл.\n\
         // Каждое правило помечено id из CONSTRAINTS.yaml — трассировка нарушения в источник.\n\
         import com.tngtech.archunit.core.importer.ImportOption;\n\
         import com.tngtech.archunit.junit.AnalyzeClasses;\n\
         import com.tngtech.archunit.junit.ArchTest;\n\
         import com.tngtech.archunit.lang.ArchRule;\n\
         \n\
         import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.classes;\n\
         import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;\n\
         \n\
         @AnalyzeClasses(packages = \"{}\", importOptions = ImportOption.DoNotIncludeTests.class)\n\
         class ArchFitnessTest {{",
        java_escape(base)
    );
    for rule in &spec.rules {
        let _ = writeln!(
            out,
            "\n    @ArchTest\n    static final ArchRule {} =\n            {}\n            .as(\"{}\");",
            java_ident(&rule.id),
            rule_expr(&rule.kind),
            java_escape(&rule.description)
        );
    }
    let _ = writeln!(out, "}}");
    out
}

/// Рендер `ArchGateRunner.java` — standalone main без `JUnit`: правила
/// захардкожены из спека (JSON на стороне Java не парсится — меньше движущихся
/// частей, воспроизводимый кэш по хэшу исходника).
///
/// Протокол вывода (парсится [`parse_runner_output`]):
/// `VIOLATION|<rule-id>|<detail>` на каждое нарушение, в конце
/// `SUMMARY|rules=<n>|violations=<m>`; exit 0 — чисто, 1 — есть нарушения,
/// 2 — ошибка запуска.
#[must_use]
pub fn render_runner(spec: &ArchUnitSpec) -> String {
    let mut out = String::new();
    let _ = writeln!(
        out,
        "// СГЕНЕРИРОВАНО: arch-ml archunit check (ADR-039). Standalone-гейт без JUnit.\n\
         import com.tngtech.archunit.core.domain.JavaClasses;\n\
         import com.tngtech.archunit.core.importer.ClassFileImporter;\n\
         import com.tngtech.archunit.lang.ArchRule;\n\
         import com.tngtech.archunit.lang.EvaluationResult;\n\
         \n\
         import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.classes;\n\
         import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;\n\
         \n\
         public final class ArchGateRunner {{\n\
         \x20   private ArchGateRunner() {{\n\
         \x20   }}\n\
         \n\
         \x20   public static void main(String[] args) {{\n\
         \x20       if (args.length < 1) {{\n\
         \x20           System.err.println(\"usage: ArchGateRunner <classes-dir>\");\n\
         \x20           System.exit(2);\n\
         \x20       }}\n\
         \x20       JavaClasses imported = new ClassFileImporter().importPaths(java.nio.file.Paths.get(args[0]));\n\
         \x20       int violations = 0;"
    );
    for rule in &spec.rules {
        let _ = writeln!(
            out,
            "        violations += check(imported, \"{}\",\n                {}\n                .as(\"{}\"));",
            java_escape(&rule.id),
            rule_expr(&rule.kind),
            java_escape(&rule.description)
        );
    }
    let _ = writeln!(
        out,
        "        System.out.println(\"SUMMARY|rules={}|violations=\" + violations);\n\
         \x20       System.exit(violations == 0 ? 0 : 1);\n\
         \x20   }}\n\
         \n\
         \x20   private static int check(JavaClasses imported, String ruleId, ArchRule rule) {{\n\
         \x20       EvaluationResult result = rule.evaluate(imported);\n\
         \x20       java.util.TreeSet<String> details = new java.util.TreeSet<>(result.getFailureReport().getDetails());\n\
         \x20       for (String detail : details) {{\n\
         \x20           System.out.println(\"VIOLATION|\" + ruleId + \"|\" + sanitize(detail));\n\
         \x20       }}\n\
         \x20       return details.size();\n\
         \x20   }}\n\
         \n\
         \x20   private static String sanitize(String text) {{\n\
         \x20       return text.replace('\\n', ' ').replace('\\r', ' ').replace('|', '/');\n\
         \x20   }}\n\
         }}",
        spec.rules.len()
    );
    out
}

/// Сериализация спека в `archunit-rules.json`.
///
/// # Errors
/// Сериализация не удалась (инвариант; практически недостижимо).
pub fn spec_to_json(spec: &ArchUnitSpec) -> Result<String> {
    Ok(serde_json::to_string_pretty(spec)?)
}

// ---------------------------------------------------------------------------
// Standalone-гейт
// ---------------------------------------------------------------------------

/// Нарушение, разобранное из вывода раннера.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateViolation {
    /// Идентификатор правила CONSTRAINTS (C-12 / имя).
    pub rule_id: String,
    /// Деталь `ArchUnit` (класс-нарушитель, зависимость, файл:строка).
    pub detail: String,
}

/// Итог standalone-гейта.
#[derive(Debug, Clone)]
pub struct GateOutcome {
    /// Нарушения (пусто — PASS).
    pub violations: Vec<GateViolation>,
    /// Сколько правил исполнено.
    pub rules_executed: usize,
}

/// Параметры запуска гейта.
#[derive(Debug, Clone)]
pub struct GateOptions {
    /// Каталог скомпилированных классов проекта.
    pub classes_dir: PathBuf,
    /// Каталог с jar'ами `ArchUnit` (archunit + slf4j).
    pub jar_dir: PathBuf,
    /// Кэш компиляции раннера (`~/.arch-ml/archunit/runner`).
    pub runner_cache: PathBuf,
    /// Таймаут запуска java.
    pub timeout: Duration,
}

/// Парсит stdout раннера: строки `VIOLATION|<id>|<detail>` → нарушения.
#[must_use]
pub fn parse_runner_output(stdout: &str) -> Vec<GateViolation> {
    stdout
        .lines()
        .filter_map(|line| {
            let rest = line.strip_prefix("VIOLATION|")?;
            let (id, detail) = rest.split_once('|')?;
            Some(GateViolation {
                rule_id: id.to_string(),
                detail: detail.to_string(),
            })
        })
        .collect()
}

/// Авто-детект каталога скомпилированных классов (`target/classes`,
/// `build/classes/java/main`, `out/production`, `classes`).
#[must_use]
pub fn find_classes_dir(repo: &Path) -> Option<PathBuf> {
    CLASSES_CANDIDATES
        .iter()
        .map(|c| repo.join(c))
        .find(|dir| dir.is_dir() && contains_class_file(dir))
}

/// В каталоге (рекурсивно, первый уровень вложенности достаточен для
/// детекта) есть хотя бы один `.class`.
fn contains_class_file(dir: &Path) -> bool {
    walkdir::WalkDir::new(dir)
        .max_depth(4)
        .into_iter()
        .filter_map(std::result::Result::ok)
        .any(|e| e.path().extension().is_some_and(|x| x == "class"))
}

/// Разрешает каталог jar'ов: явный аргумент → env `ARCHUNIT_HOME` (каталог
/// сам или его `lib/`) → кэш `~/.arch-ml/archunit/lib`.
#[must_use]
pub fn resolve_jar_dir(explicit: Option<&Path>) -> PathBuf {
    if let Some(dir) = explicit {
        return dir.to_path_buf();
    }
    if let Some(home) = std::env::var_os("ARCHUNIT_HOME") {
        let home = PathBuf::from(home);
        if home.join("lib").is_dir() {
            return home.join("lib");
        }
        return home;
    }
    crate::config::Config::home_dir()
        .join("archunit")
        .join("lib")
}

/// Кэш компиляции раннера по умолчанию.
#[must_use]
pub fn default_runner_cache() -> PathBuf {
    crate::config::Config::home_dir()
        .join("archunit")
        .join("runner")
}

/// Classpath из всех jar'ов каталога (детерминированный порядок).
fn classpath_of(jar_dir: &Path) -> Result<String> {
    let mut jars: Vec<PathBuf> = std::fs::read_dir(jar_dir)
        .map_err(|e| HarnessError::io(jar_dir, e))?
        .filter_map(std::result::Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "jar"))
        .collect();
    jars.sort();
    if jars.is_empty() {
        return Err(HarnessError::Control(format!(
            "archunit: в {} нет jar'ов ArchUnit — выполните `arch-ml archunit fetch` \
             (или укажите --jar-dir / ARCHUNIT_HOME)",
            jar_dir.display()
        )));
    }
    Ok(jars
        .iter()
        .map(|p| p.display().to_string())
        .collect::<Vec<_>>()
        .join(":"))
}

/// Запускает standalone-гейт: компилирует (при необходимости) раннер в кэш и
/// исполняет его на классах проекта. Fail-closed: любая инфраструктурная
/// проблема — `Err` с понятной диагностикой и подсказкой.
///
/// # Errors
/// Пустой спек; нет классов/jar'ов/`java`/`javac`; раннер не собрался;
/// таймаут; раннер завершился кодом 2+.
pub fn run_gate(spec: &ArchUnitSpec, opts: &GateOptions) -> Result<GateOutcome> {
    if spec.rules.is_empty() {
        return Err(HarnessError::Control(
            "archunit: спек не содержит правил — нечего исполнять \
             (нет dependency_direction/context_boundary с glob **/*.java; \
             см. секцию unsupported)"
                .into(),
        ));
    }
    if !opts.classes_dir.is_dir() {
        return Err(HarnessError::Control(format!(
            "archunit: каталог скомпилированных классов не найден: {} — \
             сначала соберите проект (`mvn compile`, `gradle classes` или \
             `javac -d classes ...`), либо укажите --classes",
            opts.classes_dir.display()
        )));
    }
    let jars_cp = classpath_of(&opts.jar_dir)?;

    // Кэш компиляции: пересобираем раннер, только если изменился исходник.
    let source = render_runner(spec);
    let hash = sha256_hex(source.as_bytes());
    std::fs::create_dir_all(&opts.runner_cache)
        .map_err(|e| HarnessError::io(&opts.runner_cache, e))?;
    let src_path = opts.runner_cache.join("ArchGateRunner.java");
    let hash_path = opts.runner_cache.join("ArchGateRunner.sha256");
    let class_path = opts.runner_cache.join("ArchGateRunner.class");
    let fresh =
        std::fs::read_to_string(&hash_path).is_ok_and(|h| h.trim() == hash) && class_path.is_file();
    if !fresh {
        std::fs::write(&src_path, &source).map_err(|e| HarnessError::io(&src_path, e))?;
        let compile = spawn_capture(
            Command::new("javac")
                .arg("-encoding")
                .arg("UTF-8")
                .arg("-cp")
                .arg(&jars_cp)
                .arg("-d")
                .arg(&opts.runner_cache)
                .arg(&src_path),
            Duration::from_secs(COMPILE_TIMEOUT_SECS),
        )
        .map_err(|e| {
            HarnessError::Control(format!(
                "archunit: не удалось запустить javac: {e} — нужен JDK (javac в PATH)"
            ))
        })?;
        match compile {
            None => {
                return Err(HarnessError::Control(format!(
                    "archunit: javac превысил таймаут {COMPILE_TIMEOUT_SECS}s и был убит"
                )));
            }
            Some(out) if !out.status.success() => {
                return Err(HarnessError::Control(format!(
                    "archunit: раннер не собрался (javac, код {}):\n{}",
                    out.status.code().unwrap_or(-1),
                    tail(&String::from_utf8_lossy(&out.stderr))
                )));
            }
            Some(_) => {
                std::fs::write(&hash_path, &hash).map_err(|e| HarnessError::io(&hash_path, e))?;
            }
        }
    }

    let run_cp = format!(
        "{}:{jars_cp}:{}",
        opts.classes_dir.display(),
        opts.runner_cache.display()
    );
    let run = spawn_capture(
        Command::new("java")
            .arg("-cp")
            .arg(&run_cp)
            .arg("ArchGateRunner")
            .arg(&opts.classes_dir),
        opts.timeout,
    )
    .map_err(|e| {
        HarnessError::Control(format!(
            "archunit: не удалось запустить java: {e} — нужна JRE/JDK (java в PATH)"
        ))
    })?;
    let Some(out) = run else {
        return Err(HarnessError::Control(format!(
            "archunit: гейт превысил таймаут {}s и был убит (настройте --timeout-secs)",
            opts.timeout.as_secs()
        )));
    };
    let stdout = String::from_utf8_lossy(&out.stdout);
    match out.status.code() {
        Some(0 | 1) => Ok(GateOutcome {
            violations: parse_runner_output(&stdout),
            rules_executed: spec.rules.len(),
        }),
        code => Err(HarnessError::Control(format!(
            "archunit: раннер завершился ошибкой (код {}):\n{}",
            code.map_or("сигнал".into(), |c| c.to_string()),
            tail(&String::from_utf8_lossy(&out.stderr))
        ))),
    }
}

/// Хвост сообщения об ошибке (stderr java/javac может быть длинным).
fn tail(text: &str) -> String {
    const MAX_ERR_CHARS: usize = 2000;
    text.chars().take(MAX_ERR_CHARS).collect()
}

/// Запуск процесса с захватом stdout/stderr и таймаутом.
/// `Ok(None)` — таймаут (процесс убит). Обёртка над тем же паттерном, что
/// `control::run_with_timeout`, но с захватом вывода.
fn spawn_capture(cmd: &mut Command, timeout: Duration) -> std::io::Result<Option<Output>> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                // Процесс завершился — читаем накопленный вывод.
                let mut stdout = Vec::new();
                let mut stderr = Vec::new();
                if let Some(mut pipe) = child.stdout.take() {
                    use std::io::Read as _;
                    let _ = pipe.read_to_end(&mut stdout); // чтение пайпа завершённого процесса не блокируется
                }
                if let Some(mut pipe) = child.stderr.take() {
                    use std::io::Read as _;
                    let _ = pipe.read_to_end(&mut stderr);
                }
                let status = child.wait()?;
                return Ok(Some(Output {
                    status,
                    stdout,
                    stderr,
                }));
            }
            Ok(None) => {
                if start.elapsed() >= timeout {
                    let _ = child.kill();
                    let _ = child.wait(); // забрать зомби
                    return Ok(None);
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(e);
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Интеграция с `control check` (тип правила `archunit`)
// ---------------------------------------------------------------------------

/// Исполняет правило типа `archunit` в контексте `arch-ml control check`:
/// строит спек из ВСЕХ правил того же `CONSTRAINTS.yaml` (общий код с
/// `archunit check`), прогоняет гейт и возвращает находки.
///
/// Инфраструктурные сбои (нет java/jar'ов/классов, таймаут) возвращаются
/// `Err` — вызывающий превращает их в error-находку (fail-closed).
///
/// # Errors
/// Инфраструктурный сбой гейта (см. [`run_gate`]); нет классов по явному
/// `classes_dir` или авто-детекту.
pub(crate) fn run_control_rule(
    rule: &FitnessRule,
    all_rules: &[&FitnessRule],
    repo: &Path,
) -> Result<Vec<LintIssue>> {
    let spec = spec_from_constraints(repo, all_rules, None);
    let classes_dir = rule.classes_dir.as_ref().map_or_else(
        || {
            find_classes_dir(repo).ok_or_else(|| {
                HarnessError::Control(
                    "archunit: скомпилированные классы не найдены (target/classes, \
                     build/classes/java/main, out/production, classes) — соберите \
                     проект или задайте classes_dir в правиле"
                        .into(),
                )
            })
        },
        |d| {
            let dir = repo.join(d);
            if dir.is_dir() {
                Ok(dir)
            } else {
                Err(HarnessError::Control(format!(
                    "archunit: classes_dir не найден: {d}"
                )))
            }
        },
    )?;
    let jar_dir = rule
        .jar_dir
        .as_ref()
        .map_or_else(|| resolve_jar_dir(None), |d| repo.join(d));
    let opts = GateOptions {
        classes_dir: classes_dir.clone(),
        jar_dir,
        runner_cache: default_runner_cache(),
        timeout: Duration::from_secs(rule.timeout_secs.unwrap_or(DEFAULT_GATE_TIMEOUT_SECS)),
    };
    let outcome = run_gate(&spec, &opts)?;
    // severity находки — из спека (нормализованная severity исходного
    // правила CONSTRAINTS, породившего ArchUnit-правило).
    let severity_of = |rule_id: &str| {
        spec.rules
            .iter()
            .find(|r| r.id == rule_id)
            .map_or("error", |r| r.severity.as_str())
    };
    let mut issues: Vec<LintIssue> = spec
        .unsupported
        .iter()
        .map(|u| LintIssue {
            file: PathBuf::from("CONSTRAINTS.yaml"),
            line: 0,
            rule: u.rule.clone(),
            message: format!("archunit: правило не исполняется JVM-гейтом: {}", u.reason),
            severity: "warn".into(),
        })
        .collect();
    issues.extend(outcome.violations.iter().map(|v| LintIssue {
        file: classes_dir.clone(),
        line: 0,
        rule: v.rule_id.clone(),
        message: format!("archunit: {}", v.detail),
        severity: severity_of(&v.rule_id).into(),
    }));
    Ok(issues)
}

// ---------------------------------------------------------------------------
// fetch: пинnutые jar'ы с Maven Central
// ---------------------------------------------------------------------------

/// Результат скачивания одного jar'а.
#[derive(Debug, Clone)]
pub struct FetchedJar {
    /// Имя файла.
    pub file: String,
    /// SHA-256 (проверенный против пина).
    pub sha256: String,
    /// true — файл уже был в кэше с совпадающим хэшем (скачивание не потребовалось).
    pub cached: bool,
}

/// Скачивает пинnutые jar'ы ([`PINNED_JARS`]) в `dest` с проверкой SHA-256.
/// Идемпотентно: файл с совпадающим хэшем не скачивается повторно.
///
/// # Errors
/// Сеть/HTTP, запись, расхождение SHA-256 с пином (supply-chain guard).
pub async fn fetch_jars(dest: &Path) -> Result<Vec<FetchedJar>> {
    std::fs::create_dir_all(dest).map_err(|e| HarnessError::io(dest, e))?;
    let client = reqwest::Client::new();
    let mut out = Vec::new();
    for jar in PINNED_JARS {
        let path = dest.join(jar.file);
        if path.is_file() {
            let existing = std::fs::read(&path).map_err(|e| HarnessError::io(&path, e))?;
            let hash = sha256_hex(&existing);
            if hash == jar.sha256 {
                out.push(FetchedJar {
                    file: jar.file.to_string(),
                    sha256: hash,
                    cached: true,
                });
                continue;
            }
        }
        let bytes = client
            .get(jar.url)
            .send()
            .await?
            .error_for_status()?
            .bytes()
            .await?;
        let hash = sha256_hex(&bytes);
        if hash != jar.sha256 {
            return Err(HarnessError::Control(format!(
                "archunit fetch: SHA-256 {} не совпал с пином: получено {hash}, \
                 ожидалось {} — файл НЕ записан (supply-chain guard)",
                jar.file, jar.sha256
            )));
        }
        std::fs::write(&path, &bytes).map_err(|e| HarnessError::io(&path, e))?;
        out.push(FetchedJar {
            file: jar.file.to_string(),
            sha256: hash,
            cached: false,
        });
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// SHA-256 (без новых зависимостей, AD-1: тонкое ядро; нужен для пинов fetch
// и ключа кэша раннера)
// ---------------------------------------------------------------------------

/// Первые 32 бита дробных частей кубических корней первых 64 простых чисел
/// (FIPS 180-4 §4.2.2; каноническая запись без разделителей — константы
/// сверяются со спецификацией дословно).
#[allow(clippy::unreadable_literal)]
const SHA256_K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

/// SHA-256 (hex, lowercase). Реализация по FIPS 180-4, safe Rust.
/// Рабочие переменные названы как в спецификации (a..h) — однобуквенные
/// имена здесь читаемость повышают, а не снижают.
#[allow(clippy::many_single_char_names, clippy::unreadable_literal)]
#[must_use]
pub fn sha256_hex(data: &[u8]) -> String {
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let bit_len = u64::try_from(data.len())
        .unwrap_or(u64::MAX / 8)
        .saturating_mul(8);
    let mut msg = data.to_vec();
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    for block in msg.chunks_exact(64) {
        let mut w = [0u32; 64];
        for (i, word) in w.iter_mut().take(16).enumerate() {
            let j = i * 4;
            *word = u32::from_be_bytes([block[j], block[j + 1], block[j + 2], block[j + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(SHA256_K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }
    let mut out = String::with_capacity(64);
    for x in h {
        let _ = write!(out, "{x:08x}");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule_yaml(name: &str, extra: &str) -> FitnessRule {
        let yaml = format!("name: {name}\ntype: dependency_direction\n{extra}");
        serde_yaml_ng::from_str(&yaml).expect("правило парсится")
    }

    // --- SHA-256: эталонные векторы FIPS 180-4 ----------------------------

    #[test]
    fn sha256_known_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            sha256_hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
    }

    // --- Эвристики маппинга -------------------------------------------------

    #[test]
    fn glob_to_package_patterns() {
        assert_eq!(
            glob_to_package_pattern("src/**/domain/**/*.java").as_deref(),
            Some("..domain..")
        );
        assert_eq!(
            glob_to_package_pattern("src/main/java/com/acme/domain/**/*.java").as_deref(),
            Some("com.acme.domain..")
        );
        assert_eq!(glob_to_package_pattern("**/*.java").as_deref(), Some(".."));
        assert_eq!(
            glob_to_package_pattern("app/src/main/java/ru/bank/pay/*.java").as_deref(),
            Some("ru.bank.pay..")
        );
        assert_eq!(glob_to_package_pattern("src/**/*.rs"), None);
        assert_eq!(glob_to_package_pattern("**/*.py"), None);
    }

    #[test]
    fn entry_to_package_patterns() {
        assert_eq!(
            entry_to_package_pattern("com/acme/infrastructure").as_deref(),
            Some("com.acme.infrastructure..")
        );
        assert_eq!(
            entry_to_package_pattern("infrastructure").as_deref(),
            Some("..infrastructure..")
        );
        // Имя класса (заглавная) отбрасывается.
        assert_eq!(
            entry_to_package_pattern("com/acme/infrastructure/OrderRepository").as_deref(),
            Some("com.acme.infrastructure..")
        );
        // Точечная запись тоже принимается.
        assert_eq!(
            entry_to_package_pattern("com.acme.infrastructure").as_deref(),
            Some("com.acme.infrastructure..")
        );
        assert_eq!(entry_to_package_pattern("OrderRepository"), None);
        assert_eq!(entry_to_package_pattern("com/acme/bad-name"), None);
    }

    // --- Маппинг YAML → спек -------------------------------------------------

    #[test]
    fn spec_maps_forbid_rule() {
        let rule = rule_yaml(
            "no_domain_infra",
            "glob: 'src/**/domain/**/*.java'\nforbid: ['com/acme/infrastructure']\nseverity: critical",
        );
        let spec = spec_from_constraints(Path::new("/tmp"), &[&rule], None);
        assert_eq!(spec.rules.len(), 1);
        assert_eq!(spec.unsupported.len(), 0);
        let r = &spec.rules[0];
        assert_eq!(r.id, "no_domain_infra");
        assert_eq!(r.severity, "error");
        match &r.kind {
            SpecRuleKind::ForbiddenDeps { from, to } => {
                assert_eq!(from, "..domain..");
                assert_eq!(to, &vec!["com.acme.infrastructure..".to_string()]);
            }
            SpecRuleKind::AllowedDepsOnly { .. } => panic!("ожидался forbid"),
        }
        // Якорный пакет в to даёт базу com.acme.
        assert_eq!(spec.base_package.as_deref(), Some("com.acme"));
    }

    #[test]
    fn spec_maps_allow_rule_with_jdk_allowlist() {
        let rule = rule_yaml(
            "layer_app",
            "glob: 'src/**/application/**/*.java'\nallow: ['com/acme/domain']",
        );
        let spec = spec_from_constraints(Path::new("/tmp"), &[&rule], None);
        assert_eq!(spec.rules.len(), 1);
        match &spec.rules[0].kind {
            SpecRuleKind::AllowedDepsOnly { from, allowed } => {
                assert_eq!(from, "..application..");
                assert!(allowed.contains(&"com.acme.domain..".to_string()));
                assert!(allowed.contains(&"..application..".to_string()));
                assert!(allowed.contains(&"java..".to_string()));
            }
            SpecRuleKind::ForbiddenDeps { .. } => panic!("ожидался allow"),
        }
    }

    #[test]
    fn spec_id_prefers_explicit_id_field() {
        let yaml = "id: C-12\nname: no_domain_infra\ntype: dependency_direction\nglob: '**/*.java'\nforbid: ['legacy']";
        let rule: FitnessRule = serde_yaml_ng::from_str(yaml).expect("правило парсится");
        let spec = spec_from_constraints(Path::new("/tmp"), &[&rule], None);
        assert_eq!(spec.rules[0].id, "C-12");
    }

    #[test]
    fn spec_non_java_and_broken_rules_go_unsupported() {
        let non_java = rule_yaml("rust_rule", "glob: 'src/**/*.rs'\nforbid: ['tui']");
        let broken = rule_yaml("bad_modes", "glob: '**/*.java'");
        let bad_pkg = rule_yaml("bad_pkg", "glob: '**/*.java'\nforbid: ['not-a-package!']");
        let spec = spec_from_constraints(Path::new("/tmp"), &[&non_java, &broken, &bad_pkg], None);
        assert_eq!(spec.rules.len(), 0);
        assert_eq!(spec.unsupported.len(), 3);
        assert!(spec.unsupported[0].reason.contains("**/*.java"));
        assert!(spec.unsupported[1].reason.contains("forbid/allow"));
        assert!(
            spec.unsupported[2]
                .reason
                .contains("не удалось вывести пакет")
        );
    }

    #[test]
    fn context_boundary_pairs_without_depends_on() {
        let depends = ["CMP-B".to_string()];
        let contexts = vec![
            CtxInfo {
                id: "CMP-A",
                depends_on: &depends,
                package: "com.acme.alpha..".into(),
            },
            CtxInfo {
                id: "CMP-B",
                depends_on: &[],
                package: "com.acme.beta..".into(),
            },
        ];
        let rules = context_boundary_rules("C-30", "boundaries", "error", &contexts);
        // CMP-A → CMP-B разрешён depends_on; остаётся только CMP-B → CMP-A.
        assert_eq!(rules.len(), 1);
        match &rules[0].kind {
            SpecRuleKind::ForbiddenDeps { from, to } => {
                assert_eq!(from, "com.acme.beta..");
                assert_eq!(to, &vec!["com.acme.alpha..".to_string()]);
            }
            SpecRuleKind::AllowedDepsOnly { .. } => panic!("ожидался forbid"),
        }
    }

    // --- Рендер артефактов (голден-фрагменты) --------------------------------

    fn sample_spec() -> ArchUnitSpec {
        let forbid = rule_yaml(
            "no_domain_infra",
            "glob: 'src/**/domain/**/*.java'\nforbid: ['com/acme/infrastructure']",
        );
        let allow = rule_yaml(
            "app_layer",
            "glob: 'src/**/application/**/*.java'\nallow: ['com/acme/domain']",
        );
        spec_from_constraints(Path::new("/tmp"), &[&forbid, &allow], None)
    }

    #[test]
    fn render_junit_test_is_idiomatic_and_traced() {
        let spec = sample_spec();
        let java = render_junit_test(&spec);
        assert!(java.contains("@AnalyzeClasses(packages = \"com.acme\""));
        assert!(java.contains("importOptions = ImportOption.DoNotIncludeTests.class"));
        assert!(java.contains("@ArchTest"));
        assert!(java.contains("static final ArchRule no_domain_infra ="));
        assert!(java.contains("noClasses().that().resideInAPackage(\"..domain..\")"));
        assert!(java.contains(
            ".should().dependOnClassesThat().resideInAnyPackage(\"com.acme.infrastructure..\")"
        ));
        assert!(java.contains("classes().that().resideInAPackage(\"..application..\")"));
        // id правила — в сообщении (трассировка).
        assert!(java.contains(".as(\"[no_domain_infra]"));
    }

    #[test]
    fn render_runner_embeds_rules_and_protocol() {
        let spec = sample_spec();
        let java = render_runner(&spec);
        assert!(java.contains("public final class ArchGateRunner"));
        assert!(java.contains("violations += check(imported, \"no_domain_infra\""));
        assert!(java.contains("\"VIOLATION|\" + ruleId + \"|\""));
        assert!(java.contains("\"SUMMARY|rules=2|violations=\""));
        assert!(java.contains("System.exit(violations == 0 ? 0 : 1)"));
    }

    #[test]
    fn render_escapes_java_literals() {
        let mut spec = sample_spec();
        spec.rules[0].description = "кавычки \" и \\ слэш".into();
        let java = render_runner(&spec);
        assert!(java.contains("кавычки \\\" и \\\\ слэш"));
    }

    #[test]
    fn spec_json_roundtrip_shape() {
        let spec = sample_spec();
        let json = spec_to_json(&spec).expect("спек сериализуется");
        assert!(json.contains("\"version\": 1"));
        assert!(json.contains("\"kind\": \"forbidden_deps\""));
        assert!(json.contains("\"kind\": \"allowed_deps_only\""));
        assert!(json.contains("\"base_package\": \"com.acme\""));
    }

    // --- Парсер вывода раннера ------------------------------------------------

    #[test]
    fn parse_runner_output_violations() {
        let stdout = "VIOLATION|C-01|Field <com.acme.domain.Order.repository> has type <com.acme.infrastructure.OrderRepository> in (Order.java:0)\n\
                      VIOLATION|C-01|Constructor <com.acme.domain.Order.<init>(...)> in (Order.java:0)\n\
                      SUMMARY|rules=2|violations=2\n";
        let v = parse_runner_output(stdout);
        assert_eq!(v.len(), 2);
        assert_eq!(v[0].rule_id, "C-01");
        assert!(v[0].detail.contains("OrderRepository"));
        assert!(v[1].detail.contains("<init>"));
    }

    #[test]
    fn parse_runner_output_pass_is_empty() {
        let v = parse_runner_output("SUMMARY|rules=2|violations=0\n");
        assert!(v.is_empty());
    }

    // --- Fail-closed пути (без живого java) -----------------------------------

    #[test]
    fn run_gate_fails_closed_on_empty_spec() {
        let spec = ArchUnitSpec {
            version: 1,
            base_package: None,
            rules: Vec::new(),
            unsupported: Vec::new(),
        };
        let tmp = tempfile::tempdir().expect("tempdir");
        let opts = GateOptions {
            classes_dir: tmp.path().to_path_buf(),
            jar_dir: tmp.path().to_path_buf(),
            runner_cache: tmp.path().to_path_buf(),
            timeout: Duration::from_secs(1),
        };
        let err = run_gate(&spec, &opts).expect_err("пустой спек — ошибка");
        assert!(err.to_string().contains("не содержит правил"));
    }

    #[test]
    fn run_gate_fails_closed_on_missing_classes() {
        let spec = sample_spec();
        let tmp = tempfile::tempdir().expect("tempdir");
        let opts = GateOptions {
            classes_dir: tmp.path().join("no-such-dir"),
            jar_dir: tmp.path().to_path_buf(),
            runner_cache: tmp.path().to_path_buf(),
            timeout: Duration::from_secs(1),
        };
        let err = run_gate(&spec, &opts).expect_err("нет классов — ошибка");
        assert!(err.to_string().contains("mvn compile"));
    }

    #[test]
    fn run_gate_fails_closed_on_missing_jars() {
        let spec = sample_spec();
        let tmp = tempfile::tempdir().expect("tempdir");
        let classes = tmp.path().join("classes");
        std::fs::create_dir_all(&classes).expect("classes dir");
        let empty_lib = tmp.path().join("lib");
        std::fs::create_dir_all(&empty_lib).expect("lib dir");
        let opts = GateOptions {
            classes_dir: classes,
            jar_dir: empty_lib,
            runner_cache: tmp.path().join("runner"),
            timeout: Duration::from_secs(1),
        };
        let err = run_gate(&spec, &opts).expect_err("нет jar'ов — ошибка");
        assert!(err.to_string().contains("arch-ml archunit fetch"));
    }

    #[test]
    fn find_classes_dir_autodetect() {
        let tmp = tempfile::tempdir().expect("tempdir");
        assert!(find_classes_dir(tmp.path()).is_none());
        let target = tmp.path().join("target/classes/com/acme");
        std::fs::create_dir_all(&target).expect("target dir");
        std::fs::write(target.join("Order.class"), b"\xca\xfe\xba\xbe").expect("class file");
        let found = find_classes_dir(tmp.path()).expect("детект target/classes");
        assert!(found.ends_with("target/classes"));
    }

    #[test]
    fn derive_base_package_common_prefix() {
        let a = rule_yaml("r1", "glob: '**/*.java'\nforbid: ['com/acme/infra']");
        let b = rule_yaml("r2", "glob: '**/*.java'\nforbid: ['com/acme/legacy']");
        let spec = spec_from_constraints(Path::new("/tmp"), &[&a, &b], None);
        assert_eq!(spec.base_package.as_deref(), Some("com.acme"));
        // Только свободные паттерны — базы нет.
        let c = rule_yaml("r3", "glob: 'src/**/domain/**/*.java'\nforbid: ['infra']");
        let spec = spec_from_constraints(Path::new("/tmp"), &[&c], None);
        assert_eq!(spec.base_package, None);
    }
}
