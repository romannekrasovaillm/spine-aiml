//! Архитектурный контроль: fitness functions, линтер spine, сенсоры спек,
//! маршрутизация по Architecture Significance Score.
//!
//! КОНТРАКТ (владелец: агент `control`) — детерминированный механический слой
//! (идеи из `docs/SOURCE_BRIEF.md`: линтер spine, сенсоры required-sections и
//! upstream-coverage, 15 триггеров значимости, маршруты Fast/Standard/Critical):
//! - [`lint_spine`] — проверки ARCHITECTURE-SPINE.md: дубли AD-id, пустые
//!   Binds/Prevents/Rule, заглушки (TODO/TBD), непиннутые версии, ссылки на
//!   несуществующие AD;
//! - [`sensors_check`] — сенсоры спецификаций: наличие обязательных секций,
//!   upstream-coverage (артефакт ссылается на входы из consumes);
//! - [`check`] — fitness functions из `CONSTRAINTS.yaml`: `must_contain` /
//!   `must_not_contain` (regex по glob-набору файлов),
//!   `each_file_must_contain` (regex обязателен в КАЖДОМ файле набора),
//!   `file_exists`, `dir_must_have_file` (обязательный файл в каждом
//!   каталоге набора), `max_age` (свежесть файла: mtime не старше N дней),
//!   `command_succeeds` (с таймаутом),
//!   `dependency_direction` (структурная проверка направления зависимостей:
//!   импорты файлов набора против `forbid`/`allow`-списков модулей,
//!   ADR-029) и `context_boundary` (границы контекстов: импорты не
//!   пересекают `code_roots` CMP-сущностей модели без `depends_on`,
//!   ADR-030), `deny_dependency` (запрещённые пакеты в манифестах
//!   Cargo.toml/pom.xml/requirements.txt — детектор тех-радара,
//!   `docs/corp-spine.md`); итог PASS/FAIL + находки + длительность каждого
//!   правила (per-rule timing, [`RuleDuration`]);
//! - наследование корпоративного контекста (`docs/corp-spine.md`): поле
//!   верхнего уровня `extends: [<ref>@<version>]` подмешивает правила
//!   родительских constraint-файлов с меткой источника и проверкой пина
//!   версии (расхождение — error-находка «родитель обновился», а не
//!   молчаливая поломка); `overrides:` — исключение правила только через
//!   ADR (rule + adr + until, неполный — error, просроченный — warn и
//!   правило снова действует); `severity: block|warn` — warn-правила не
//!   ломают гейт; [`control_report`] — отчёт вверх (`--level corp`);
//! - [`rules_report`] — отчёт по реестру правил `CONSTRAINTS.yaml` (сводка,
//!   таблица карточек, находки: без owner/expiry, просроченные, `exclude_glob`,
//!   git-прокси стоимости сопровождения, суммарный `effort_hours`);
//! - [`significance_score`] — по ответам на 15 триггеров → Score + Route
//!   (пороги — [`significance_score_with_limits`], дефолты
//!   [`DEFAULT_FAST_MAX`]/[`DEFAULT_STANDARD_MAX`], ADR-034);
//! - [`detect_diff_triggers`] — механический anti-bypass floor (ADR-034):
//!   вывод триггеров значимости из git-диффа; [`score_with_sources`] —
//!   fail-safe объединение заявленных и найденных триггеров.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, Instant, SystemTime};

use async_trait::async_trait;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use walkdir::WalkDir;

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Маршрут изменения по значимости.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Route {
    /// Низкий риск: дельта-спека + авто-валидация.
    Fast,
    /// Средний: контракт Spec→Plan→Tasks + Architecture Fit автоматически.
    Standard,
    /// Архитектурно/регуляторно значимое: Solutioning + human decision (A3).
    Critical,
}

impl std::str::FromStr for Route {
    type Err = String;

    /// Парсит маршрут из строки (fast/standard/critical, без учёта регистра).
    fn from_str(s: &str) -> std::result::Result<Self, Self::Err> {
        match s.trim().to_ascii_lowercase().as_str() {
            "fast" => Ok(Self::Fast),
            "standard" => Ok(Self::Standard),
            "critical" => Ok(Self::Critical),
            other => Err(format!(
                "неизвестный маршрут '{other}' (допустимы: fast, standard, critical)"
            )),
        }
    }
}

impl std::fmt::Display for Route {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            Self::Fast => "Fast",
            Self::Standard => "Standard",
            Self::Critical => "Critical",
        };
        f.write_str(s)
    }
}

/// Результат оценки значимости.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Significance {
    /// Число сработавших триггеров.
    pub score: usize,
    /// Сработавшие триггеры.
    pub fired: Vec<String>,
    /// Маршрут.
    pub route: Route,
}

/// Канонический список из 15 триггеров архитектурной значимости
/// (из обзора AI-Disrupt PDLC, см. `docs/SOURCE_BRIEF.md` §C.3).
pub const SIGNIFICANCE_TRIGGERS: [&str; 15] = [
    "new_component",
    "new_datastore",
    "new_vendor",
    "domain_ownership_change",
    "cross_domain_integration",
    "api_contract_change",
    "data_contract_change",
    "security_boundary_change",
    "trust_zone_change",
    "consistency_model_change",
    "significant_nfr",
    "rto_rpo_targets",
    "irreversible_migration",
    "financial_impact",
    "criticality_or_exception",
];

/// Обязательные заголовки спецификации (сенсор `required_sections`).
pub const REQUIRED_SECTIONS: [&str; 3] = ["## Проблема", "## Критерии приёмки", "## Риски"];

/// Дефолтный верхний порог маршрута Fast: score ≤ `fast_max` → Fast (ADR-034).
pub const DEFAULT_FAST_MAX: usize = 1;
/// Дефолтный верхний порог маршрута Standard: выше — Critical (ADR-034).
pub const DEFAULT_STANDARD_MAX: usize = 4;

/// Триггеры, форсирующие маршрут Critical независимо от счёта.
/// НЕ конфигурируются (fail-safe, ADR-034): границу безопасности,
/// необратимую миграцию и критичность/exception нельзя «откалибровать вниз».
const FORCING_CRITICAL_TRIGGERS: [&str; 3] = [
    "security_boundary_change",
    "irreversible_migration",
    "criticality_or_exception",
];

/// Оценивает значимость по карте «триггер → сработал» с дефолтными порогами
/// ([`DEFAULT_FAST_MAX`]/[`DEFAULT_STANDARD_MAX`]) — обратная совместимость.
#[must_use]
pub fn significance_score(answers: &BTreeMap<String, bool>) -> Significance {
    significance_score_with_limits(answers, DEFAULT_FAST_MAX, DEFAULT_STANDARD_MAX)
}

/// Оценивает значимость с явными порогами маршрутизации (ADR-034):
/// score ≤ `fast_max` → Fast, ≤ `standard_max` → Standard, выше → Critical;
/// любой из [`FORCING_CRITICAL_TRIGGERS`] → Critical независимо от счёта.
///
/// Пороги ожидаются валидированными (`fast_max < standard_max`,
/// см. `crate::config::SignificanceConfig::limits`); при невалидных граница
/// Fast пуста (fail-safe в сторону более строгого маршрута).
#[must_use]
pub fn significance_score_with_limits(
    answers: &BTreeMap<String, bool>,
    fast_max: usize,
    standard_max: usize,
) -> Significance {
    let fired: Vec<String> = answers
        .iter()
        .filter(|(_, v)| **v)
        .map(|(k, _)| k.clone())
        .collect();
    let critical = FORCING_CRITICAL_TRIGGERS
        .iter()
        .any(|t| fired.iter().any(|f| f == t));
    let score = fired.len();
    let route = if critical || score > standard_max {
        Route::Critical
    } else if score > fast_max {
        Route::Standard
    } else {
        Route::Fast
    };
    Significance {
        score,
        fired,
        route,
    }
}

// --- S-1: механический anti-bypass floor (триггеры из git-диффа, ADR-034) ---

/// Манифесты зависимостей, отслеживаемые детекторами
/// `new_component`/`new_vendor`.
const DEP_MANIFESTS: [&str; 4] = ["Cargo.toml", "pom.xml", "package.json", "go.mod"];

/// Расширения конфиг-файлов для детектора `new_datastore` (S-1).
const CONFIG_EXTS: [&str; 9] = [
    "yaml",
    "yml",
    "toml",
    "json",
    "ini",
    "conf",
    "config",
    "properties",
    "env",
];

/// Результат механического сканирования git-диффа (S-1).
#[derive(Debug, Clone, Default)]
pub struct DiffTriggers {
    /// Сработавшие триггеры (канонические имена из [`SIGNIFICANCE_TRIGGERS`]).
    pub triggers: BTreeSet<String>,
    /// Основания срабатываний («триггер: файл») — для отчёта и аудита.
    pub evidence: Vec<String>,
}

impl DiffTriggers {
    /// Фиксирует срабатывание триггера с основанием.
    fn fire(&mut self, trigger: &str, evidence: &str) {
        if self.triggers.insert(trigger.to_string()) {
            self.evidence.push(format!("{trigger}: {evidence}"));
        }
    }
}

/// stdout `git diff` в репозитории; ошибка — не git-репозиторий, git
/// недоступен или некорректный диапазон (понятный текст для CLI).
fn git_diff_out(repo: &Path, diff_args: &[String], extra: &[&str]) -> Result<String> {
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .arg("diff")
        .args(diff_args)
        .args(extra)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| {
            HarnessError::Control(format!(
                "anti-bypass: не удалось запустить git ({e}) — дифф-сканирование невозможно"
            ))
        })?;
    if !out.status.success() {
        let stderr = String::from_utf8_lossy(&out.stderr);
        let detail = stderr.trim().chars().take(200).collect::<String>();
        return Err(HarnessError::Control(format!(
            "anti-bypass: {} не git-репозиторий или некорректный GIT_REF ({detail})",
            repo.display()
        )));
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Похожа ли добавленная строка манифеста на новую зависимость
/// (эвристика по типу манифеста; fail-safe — ложное срабатывание лишь
/// расширяет множество триггеров).
fn looks_like_dependency_line(manifest: &str, line: &str) -> bool {
    let l = line.trim();
    match manifest {
        // serde = "1.0" / serde = { version = "1.0" }
        "Cargo.toml" => {
            let name_ok = l.split(['=', ' ']).next().is_some_and(|n| {
                !n.is_empty()
                    && n.chars()
                        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
            });
            name_ok && l.contains('=') && l.contains('"')
        }
        // "left-pad": "^1.3.0"
        "package.json" => l.starts_with('"') && l.contains("\":"),
        // <dependency> / <groupId>… / <artifactId>…
        "pom.xml" => {
            l.contains("<dependency>") || l.contains("<groupId>") || l.contains("<artifactId>")
        }
        // require github.com/foo/bar v1.2.3 (и строки в блоке require)
        "go.mod" => {
            l.starts_with("require ")
                || l.split_whitespace().nth(1).is_some_and(|v| {
                    v.starts_with('v') && v[1..].chars().next().is_some_and(|c| c.is_ascii_digit())
                })
        }
        _ => false,
    }
}

/// Файл — конфиг по эвристике: расширение из списка или «config»/«application»/
/// «settings» в имени (для детектора `new_datastore`).
fn looks_like_config(path: &str) -> bool {
    let name = Path::new(path)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let ext = Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    CONFIG_EXTS.contains(&ext.as_str())
        || name.contains("config")
        || name.contains("application")
        || name.contains("settings")
}

/// Компилирует статический regex детекторов (константные паттерны —
/// ошибка компиляции означает внутренний дефект).
fn diff_regex(pattern: &str) -> Result<Regex> {
    Regex::new(pattern)
        .map_err(|e| HarnessError::Control(format!("внутренний regex anti-bypass: {e}")))
}

/// Механический вывод триггеров значимости из git-диффа (S-1, ADR-034).
///
/// Диапазон: `git_ref = None` — рабочее дерево против `HEAD` (staged +
/// unstaged + untracked); `Some(r)` — `git diff r...HEAD`. Детекторы
/// (эвристики, fail-safe — только расширяют множество):
///
/// - `new_component` — добавлен каталог верхнего/второго уровня с манифестом
///   (Cargo.toml/pom.xml/package.json/go.mod) или `src/`;
/// - `new_vendor` — в диффе манифеста зависимостей добавлена строка
///   зависимости;
/// - `api_contract_change` — изменён/добавлен файл с `openapi`/`asyncapi`
///   в имени (без учёта регистра);
/// - `irreversible_migration` — в диффе файла миграций (каталог `migrations/`
///   или `*.sql`) есть `DROP TABLE`/`TRUNCATE`/`DROP COLUMN`;
/// - `new_datastore` — в конфигах добавлены строки подключения
///   `postgres://`/`mysql://`/`kafka`/`mongodb`/`redis://`;
/// - `hypothesis_hit` — появление в диффе файла по маске `kind: files` из
///   таблицы роутинга гипотез `$HYPOTHESES_DIR/routing.json` (продуктовая
///   фича «карточки гипотез + роутинг по фактам проекта»). Переменная не
///   задана, файла нет или JSON битый — сенсор молча выключен. Триггер НЕ
///   форсирует Critical: даёт ровно +1 к счёту значимости.
///
/// # Errors
/// Не git-репозиторий, git недоступен, некорректный `GIT_REF`.
pub fn detect_diff_triggers(repo: &Path, git_ref: Option<&str>) -> Result<DiffTriggers> {
    detect_diff_triggers_with_routing(repo, git_ref, hypothesis_routing_path_from_env().as_deref())
}

/// Ядро [`detect_diff_triggers`] с явным путём к таблице роутинга гипотез
/// (`None` — сенсор выключен). Выделено для тестов: путь передаётся
/// напрямую, без мутации переменной окружения (`std::env::set_var`
/// недоступен в edition 2024).
fn detect_diff_triggers_with_routing(
    repo: &Path,
    git_ref: Option<&str>,
    routing: Option<&Path>,
) -> Result<DiffTriggers> {
    let range: Vec<String> = match git_ref {
        None => vec!["HEAD".to_string()],
        Some(r) => vec![format!("{r}...HEAD")],
    };
    let name_status = git_diff_out(repo, &range, &["--name-status"])?;
    let patch_text = git_diff_out(repo, &range, &[])?;

    // Изменённые файлы: (статус, путь). Rename/copy — берём новый путь.
    let mut files: Vec<(char, String)> = Vec::new();
    for line in name_status.lines() {
        let mut parts = line.split('\t');
        let Some(status) = parts.next() else {
            continue;
        };
        let code = status.chars().next().unwrap_or(' ');
        let path = match code {
            'R' | 'C' => parts.nth(1),
            _ => parts.next(),
        };
        if let Some(p) = path {
            files.push((code, p.to_string()));
        }
    }

    // Добавленные строки по файлам (разбор unified diff).
    let mut added: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut current: Option<String> = None;
    for line in patch_text.lines() {
        if let Some(rest) = line.strip_prefix("+++ b/") {
            current = Some(rest.to_string());
        } else if line.starts_with("+++") {
            current = None; // +++ /dev/null — удалённый файл
        } else if let Some(cur) = &current {
            if let Some(l) = line.strip_prefix('+') {
                added.entry(cur.clone()).or_default().push(l.to_string());
            }
        }
    }

    // В режиме рабочего дерева untracked-файлы git-diff не видит — а новый
    // компонент почти всегда начинается с untracked. Дочитываем их как
    // добавленные (весь файл — «добавленные строки»).
    if git_ref.is_none() {
        let out = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["ls-files", "--others", "--exclude-standard"])
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output();
        if let Ok(out) = out {
            if out.status.success() {
                for path in String::from_utf8_lossy(&out.stdout).lines() {
                    let path = path.trim();
                    if path.is_empty() {
                        continue;
                    }
                    files.push(('A', path.to_string()));
                    // Нечитаемый/бинарный файл — без контентных детекторов.
                    if let Ok(content) = std::fs::read_to_string(repo.join(path)) {
                        added
                            .entry(path.to_string())
                            .or_default()
                            .extend(content.lines().map(str::to_string));
                    }
                }
            }
        }
    }

    let re_migration = diff_regex(r"(?i)\b(?:drop\s+table|truncate|drop\s+column)\b")?;
    let re_datastore = diff_regex(r"(?i)(?:postgres://|mysql://|mongodb|redis://|kafka)")?;

    let mut found = DiffTriggers::default();
    for (code, path) in &files {
        let segs: Vec<&str> = path.split('/').collect();
        let file_name = segs.last().copied().unwrap_or_default();
        let lower_name = file_name.to_ascii_lowercase();
        let lower_path = path.to_ascii_lowercase();

        // new_component: добавлен каталог 1-го/2-го уровня с манифестом или src/.
        if *code == 'A' {
            if (2..=3).contains(&segs.len()) && DEP_MANIFESTS.contains(&file_name) {
                found.fire("new_component", &format!("добавлен манифест {path}"));
            }
            if segs.len() >= 2 && (segs[0] == "src" || (segs.len() >= 3 && segs[1] == "src")) {
                found.fire("new_component", &format!("добавлены исходники {path}"));
            }
        }

        // new_vendor: строка зависимости в диффе манифеста.
        if *code != 'D' && DEP_MANIFESTS.contains(&file_name) {
            if let Some(lines) = added.get(path.as_str()) {
                if let Some(l) = lines
                    .iter()
                    .find(|l| looks_like_dependency_line(file_name, l))
                {
                    found.fire(
                        "new_vendor",
                        &format!(
                            "зависимость в {path}: {}",
                            l.trim().chars().take(80).collect::<String>()
                        ),
                    );
                }
            }
        }

        // api_contract_change: openapi/asyncapi в имени файла.
        if *code != 'D' && (lower_name.contains("openapi") || lower_name.contains("asyncapi")) {
            found.fire("api_contract_change", &format!("изменён контракт {path}"));
        }

        // irreversible_migration: DDL разрушения в файлах миграций.
        let is_migration = lower_path.split('/').any(|s| s == "migrations")
            || Path::new(path)
                .extension()
                .is_some_and(|e| e.eq_ignore_ascii_case("sql"));
        if *code != 'D' && is_migration {
            if let Some(lines) = added.get(path.as_str()) {
                if let Some(l) = lines.iter().find(|l| re_migration.is_match(l)) {
                    found.fire(
                        "irreversible_migration",
                        &format!(
                            "разрушающий DDL в {path}: {}",
                            l.trim().chars().take(80).collect::<String>()
                        ),
                    );
                }
            }
        }

        // new_datastore: строки подключения в конфигах.
        if *code != 'D' && looks_like_config(path) {
            if let Some(lines) = added.get(path.as_str()) {
                if let Some(l) = lines.iter().find(|l| re_datastore.is_match(l)) {
                    found.fire(
                        "new_datastore",
                        &format!(
                            "строка подключения в {path}: {}",
                            l.trim().chars().take(80).collect::<String>()
                        ),
                    );
                }
            }
        }
    }

    // hypothesis_hit: появление/изменение файла по маске `kind: files` из
    // таблицы роутинга гипотез (карточки HYPOTHESIS.md → routing.json).
    // Уникальный триггер: сколько бы строк ни совпало, `fire` даёт +1 к
    // счёту. Не форсирует Critical (нет в FORCING_CRITICAL_TRIGGERS).
    if let Some(routing) = routing {
        for trigger in hypothesis_file_triggers(routing) {
            let hit = files
                .iter()
                .find(|(code, path)| *code != 'D' && file_matches_glob(path, &trigger));
            if let Some((_, path)) = hit {
                found.fire(
                    HYPOTHESIS_HIT_TRIGGER,
                    &format!("гипотеза: {path} по маске '{trigger}'"),
                );
                break; // уникальный триггер — ровно +1 к счёту
            }
        }
    }

    Ok(found)
}

// --- Сенсор гипотез: роутинг по фактам проекта (карточки гипотез) ---

/// Имя триггера значимости, зажигаемого сенсором гипотез при совпадении
/// файла диффа с маской карточки. Намеренно НЕ входит в
/// [`FORCING_CRITICAL_TRIGGERS`]: влияет на выбор Fast/Standard/Critical
/// через +1 к счёту, но не переводит маршрут в Critical сам по себе.
const HYPOTHESIS_HIT_TRIGGER: &str = "hypothesis_hit";

/// Таблица роутинга гипотез (`$HYPOTHESES_DIR/routing.json`): хранится
/// только то, что читает сенсор — строки триггеров. Лишние поля
/// (`min_score`, `card`, `state`, `skills`) serde игнорирует.
#[derive(Deserialize)]
struct HypothesisRoutingTable {
    /// Плоские триггеры всех карточек.
    #[serde(default)]
    rows: Vec<HypothesisRoutingRow>,
}

/// Одна строка таблицы роутинга.
#[derive(Deserialize)]
struct HypothesisRoutingRow {
    /// Маска/литерал триггера (glob по basename и относительному пути).
    trigger: String,
    /// Вид факта: `files`/`keys`/`deps`/`words`. Сенсор значимости читает
    /// только `files` (остальное — матчинг намерения/контента).
    kind: String,
}

/// Глоб-маски триггеров `kind: files` из таблицы роутинга.
///
/// Сенсор fail-safe: файл не читается, JSON битый или нет строк `files` —
/// пустой список (сенсор молча выключен, без ошибок и предупреждений).
fn hypothesis_file_triggers(routing: &Path) -> Vec<String> {
    let Ok(text) = std::fs::read_to_string(routing) else {
        return Vec::new();
    };
    let Ok(table) = serde_json::from_str::<HypothesisRoutingTable>(&text) else {
        return Vec::new();
    };
    table
        .rows
        .into_iter()
        .filter(|r| r.kind == "files" && !r.trigger.is_empty())
        .map(|r| r.trigger)
        .collect()
}

/// Совпадение файла диффа с маской триггера карточки. Как в
/// `card_match.py`, маска проверяется и по basename, и по относительному
/// пути файла (glob-движок ядра — [`glob_matches`]).
fn file_matches_glob(path: &str, pattern: &str) -> bool {
    let base = path.rsplit('/').next().unwrap_or(path);
    glob_matches(pattern, path) || glob_matches(pattern, base)
}

/// Путь к таблице роутинга гипотез по значению `HYPOTHESES_DIR`: `None`
/// (переменная не задана) или пустое значение — `None`; ведущий `~/`
/// разворачивается от `$HOME` (значение переменной окружения шелл не
/// раскрывает). Значение передаётся вызывающим — env читает тонкая обёртка
/// [`hypothesis_routing_path_from_env`], тесты зовут эту функцию напрямую.
fn hypothesis_routing_path(dir: Option<&str>) -> Option<PathBuf> {
    let dir = dir?.trim();
    if dir.is_empty() {
        return None;
    }
    let base = match dir.strip_prefix("~/") {
        Some(rest) => std::env::var_os("HOME")
            .map_or_else(|| PathBuf::from(dir), |home| PathBuf::from(home).join(rest)),
        None => PathBuf::from(dir),
    };
    Some(base.join("routing.json"))
}

/// Обёртка с чтением env: вся логика — в [`hypothesis_routing_path`].
fn hypothesis_routing_path_from_env() -> Option<PathBuf> {
    hypothesis_routing_path(std::env::var("HYPOTHESES_DIR").ok().as_deref())
}

/// Источник срабатывания триггера значимости (anti-bypass отчёт, S-1).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TriggerSource {
    /// Заявлен флагом `--trigger`.
    Declared,
    /// Найден механически по git-диффу.
    Diff,
    /// Заявлен флагом и подтверждён диффом.
    Both,
}

impl TriggerSource {
    /// Метка для вывода: `(declared)` / `(diff)` / `(declared+diff)`.
    #[must_use]
    pub fn label(&self) -> &'static str {
        match self {
            Self::Declared => "declared",
            Self::Diff => "diff",
            Self::Both => "declared+diff",
        }
    }
}

/// Результат fail-safe объединения заявленных и найденных по диффу триггеров.
#[derive(Debug, Clone)]
pub struct ScoredTriggers {
    /// Оценка по объединённому множеству.
    pub significance: Significance,
    /// Сработавший триггер → источник.
    pub sources: BTreeMap<String, TriggerSource>,
    /// Найдены диффом, но не заявлены флагами (расхождение «заявлено vs
    /// видно по диффу» — anti-bypass сигнал; на маршрут влияет через
    /// объединённое множество, на exit code не влияет).
    pub undeclared: Vec<String>,
}

/// Fail-safe объединение (S-1, ADR-034): детектор ТОЛЬКО добавляет триггеры
/// к заявленным `--trigger` флагами (объединение множеств) — маршрут
/// считается по объединённому множеству с порогами `fast_max`/`standard_max`.
#[must_use]
pub fn score_with_sources(
    answers: &BTreeMap<String, bool>,
    diff: &DiffTriggers,
    fast_max: usize,
    standard_max: usize,
) -> ScoredTriggers {
    let mut merged = answers.clone();
    let mut sources = BTreeMap::new();
    for (name, fired) in answers {
        if *fired {
            sources.insert(name.clone(), TriggerSource::Declared);
        }
    }
    let mut undeclared = Vec::new();
    for t in &diff.triggers {
        if let Some(s) = sources.get_mut(t) {
            *s = TriggerSource::Both;
        } else {
            sources.insert(t.clone(), TriggerSource::Diff);
            undeclared.push(t.clone());
        }
        merged.insert(t.clone(), true);
    }
    ScoredTriggers {
        significance: significance_score_with_limits(&merged, fast_max, standard_max),
        sources,
        undeclared,
    }
}

/// Находка линтера/сенсора.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LintIssue {
    /// Файл.
    pub file: PathBuf,
    /// Строка (0 — файл целиком).
    pub line: usize,
    /// Код правила (`dup_ad_id`, `empty_field`, `stub_marker`, `unpinned_version`,
    /// `broken_ad_ref` либо имя fitness-правила из `CONSTRAINTS.yaml`).
    pub rule: String,
    /// Сообщение.
    pub message: String,
    /// Критичность: error|warn.
    pub severity: String,
}

/// Номера инвариантов `AD-<n>`, определённых в файле spine.
///
/// Семантика определения — как у [`lint_spine`]: заголовок `### AD-<n> …`
/// либо строка `AD-<n>:` / `AD-<n>.` в начале строки. Используется
/// трассировкой (`crate::trace`) для сверки модели со spine.
///
/// # Errors
/// Файл не читается, некорректный идентификатор AD.
pub fn spine_ad_ids(path: &Path) -> Result<BTreeSet<u64>> {
    let content = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let re_def_heading = spine_regex(r"^\s{0,3}#{1,6}\s+AD-(\d+)\b")?;
    let re_def_bare = spine_regex(r"^\s*AD-(\d+)\s*[:.]")?;
    let mut ids = BTreeSet::new();
    for (idx, line) in content.lines().enumerate() {
        if let Some(caps) = re_def_heading
            .captures(line)
            .or_else(|| re_def_bare.captures(line))
        {
            let id: u64 = caps[1].parse().map_err(|_| {
                HarnessError::Control(format!(
                    "{}:{}: некорректный идентификатор AD",
                    path.display(),
                    idx + 1
                ))
            })?;
            ids.insert(id);
        }
    }
    Ok(ids)
}

/// Линтер ARCHITECTURE-SPINE.md.
///
/// Определением AD-блока считается заголовок `### AD-<n> …` либо строка вида
/// `AD-<n>:` / `AD-<n>.` в начале строки; остальные вхождения `AD-<n>` —
/// ссылки. Блок простирается до следующего определения AD (или конца файла).
///
/// # Errors
/// Файл не читается.
pub fn lint_spine(path: &Path) -> Result<Vec<LintIssue>> {
    let content = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let lines: Vec<&str> = content.lines().collect();

    let re_def_heading = spine_regex(r"^\s{0,3}#{1,6}\s+AD-(\d+)\b")?;
    let re_def_bare = spine_regex(r"^\s*AD-(\d+)\s*[:.]")?;
    let re_ad_ref = spine_regex(r"\bAD-(\d+)\b")?;
    let re_stub = spine_regex(r"\b(?:TODO|TBD|FIXME|XXX)\b|\?\?\?")?;
    let re_unpinned = spine_regex(r#"\blatest\b|[:=]\s*["']?\*["']?(?:\s|$)"#)?;
    let re_field = spine_regex(r"\b(Binds|Prevents|Rule)\*{0,2}\s*:\s*\*{0,2}\s*(.*)$")?;

    // Первый проход: определения AD-блоков.
    let mut def_lines: Vec<Option<u64>> = vec![None; lines.len()];
    let mut definitions: Vec<(u64, usize)> = Vec::new(); // (id, строка 1-based)
    for (idx, line) in lines.iter().enumerate() {
        let caps = re_def_heading
            .captures(line)
            .or_else(|| re_def_bare.captures(line));
        if let Some(caps) = caps {
            let id: u64 = caps[1].parse().map_err(|_| {
                HarnessError::Control(format!(
                    "{}:{}: некорректный идентификатор AD",
                    path.display(),
                    idx + 1
                ))
            })?;
            def_lines[idx] = Some(id);
            definitions.push((id, idx + 1));
        }
    }
    let defined: BTreeSet<u64> = definitions.iter().map(|(id, _)| *id).collect();

    let mut issues = Vec::new();
    let mut push = |line: usize, rule: &str, severity: &str, message: String| {
        issues.push(LintIssue {
            file: path.to_path_buf(),
            line,
            rule: rule.into(),
            message,
            severity: severity.into(),
        });
    };

    // dup_ad_id: повторное определение того же идентификатора.
    let mut first_seen: BTreeMap<u64, usize> = BTreeMap::new();
    for (id, line_no) in &definitions {
        if let Some(first) = first_seen.get(id) {
            push(
                *line_no,
                "dup_ad_id",
                "error",
                format!("повторное определение AD-{id} (первое — строка {first})"),
            );
        } else {
            first_seen.insert(*id, *line_no);
        }
    }

    // empty_field: у каждого AD-блока должны быть непустые Binds/Prevents/Rule.
    for (pos, (id, def_line)) in definitions.iter().enumerate() {
        let start = def_line - 1;
        let end = definitions
            .get(pos + 1)
            .map_or(lines.len(), |(_, next)| next - 1);
        let block = &lines[start..end];
        for field in ["Binds", "Prevents", "Rule"] {
            let mut found = false;
            for (off, line) in block.iter().enumerate() {
                let Some(caps) = re_field.captures(line) else {
                    continue;
                };
                if &caps[1] != field {
                    continue;
                }
                found = true;
                let value = caps[2].trim().trim_matches('*').trim();
                if value.is_empty() {
                    push(
                        start + off + 1,
                        "empty_field",
                        "error",
                        format!("AD-{id}: пустое обязательное поле '{field}'"),
                    );
                }
            }
            if !found {
                push(
                    *def_line,
                    "empty_field",
                    "error",
                    format!("AD-{id}: отсутствует обязательное поле '{field}'"),
                );
            }
        }
    }

    // Построчные правила: заглушки и непиннутые версии.
    for (idx, line) in lines.iter().enumerate() {
        if let Some(m) = re_stub.find(line) {
            push(
                idx + 1,
                "stub_marker",
                "warn",
                format!("заглушка '{}' — заполнить до гейта", m.as_str()),
            );
        }
        if re_unpinned.is_match(line) {
            let token = if line.contains("latest") {
                "'latest'"
            } else {
                "'*'"
            };
            push(
                idx + 1,
                "unpinned_version",
                "warn",
                format!("непиннутая версия зависимости: {token}"),
            );
        }
    }

    // broken_ad_ref: ссылки на несуществующие в файле AD.
    for (idx, line) in lines.iter().enumerate() {
        let mut reported: BTreeSet<u64> = BTreeSet::new();
        for caps in re_ad_ref.captures_iter(line) {
            let Ok(id) = caps[1].parse::<u64>() else {
                continue;
            };
            // Собственный идентификатор на строке определения — не ссылка.
            if def_lines[idx] == Some(id) || defined.contains(&id) || !reported.insert(id) {
                continue;
            }
            push(
                idx + 1,
                "broken_ad_ref",
                "warn",
                format!("ссылка на несуществующий AD-{id}"),
            );
        }
    }

    issues.sort_by(|a, b| (a.line, &a.rule).cmp(&(b.line, &b.rule)));
    Ok(issues)
}

/// Компилирует статический regex линтера (ошибка компиляции — внутренний дефект).
fn spine_regex(pattern: &str) -> Result<Regex> {
    Regex::new(pattern).map_err(|e| HarnessError::Control(format!("внутренний regex линтера: {e}")))
}

/// Результат сенсора.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SensorResult {
    /// Имя сенсора (`required_sections`, `upstream_coverage`).
    pub sensor: String,
    /// Проверенный файл.
    pub file: PathBuf,
    /// Прошёл ли.
    pub passed: bool,
    /// Детали.
    pub details: String,
}

/// Прогон сенсоров по каталогу спецификаций (нерекурсивно, только `*.md`).
///
/// Сенсоры:
/// - `required_sections` — наличие заголовков из [`REQUIRED_SECTIONS`];
/// - `upstream_coverage` — относительные ссылки `[..](path.md)` существуют
///   относительно каталога спецификаций.
///
/// # Errors
/// Каталог не читается.
pub fn sensors_check(spec_dir: &Path) -> Result<Vec<SensorResult>> {
    let mut files: Vec<PathBuf> = Vec::new();
    let rd = std::fs::read_dir(spec_dir).map_err(|e| HarnessError::io(spec_dir, e))?;
    for entry in rd {
        let entry = entry.map_err(|e| HarnessError::io(spec_dir, e))?;
        let p = entry.path();
        if p.is_file() && p.extension().is_some_and(|ext| ext == "md") {
            files.push(p);
        }
    }
    files.sort();

    let re_link = Regex::new(r"\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)")
        .map_err(|e| HarnessError::Control(format!("внутренний regex сенсоров: {e}")))?;

    let mut results = Vec::new();
    for file in files {
        let content = std::fs::read_to_string(&file).map_err(|e| HarnessError::io(&file, e))?;

        let missing: Vec<&str> = REQUIRED_SECTIONS
            .iter()
            .copied()
            .filter(|sec| !content.lines().any(|l| l.trim_start().starts_with(sec)))
            .collect();
        results.push(SensorResult {
            sensor: "required_sections".into(),
            file: file.clone(),
            passed: missing.is_empty(),
            details: if missing.is_empty() {
                "все обязательные секции на месте".into()
            } else {
                format!("нет секций: {}", missing.join(", "))
            },
        });

        let mut total = 0usize;
        let mut broken: Vec<String> = Vec::new();
        for caps in re_link.captures_iter(&content) {
            let raw = &caps[1];
            if raw.starts_with("http://")
                || raw.starts_with("https://")
                || raw.starts_with("mailto:")
                || raw.starts_with('#')
            {
                continue;
            }
            let target = raw.split('#').next().unwrap_or(raw);
            if target.is_empty() {
                continue;
            }
            total += 1;
            let p = Path::new(target);
            let full = if p.is_absolute() {
                p.to_path_buf()
            } else {
                spec_dir.join(p)
            };
            if !full.exists() {
                broken.push(target.to_string());
            }
        }
        results.push(SensorResult {
            sensor: "upstream_coverage".into(),
            file,
            passed: broken.is_empty(),
            details: if broken.is_empty() {
                format!("все ссылки валидны ({total})")
            } else {
                format!("битые ссылки: {}", broken.join(", "))
            },
        });
    }
    Ok(results)
}

/// Длительность выполнения одного fitness-правила (per-rule timing).
///
/// Аддитивное поле отчёта (SDK-контракт v1: новые поля не ломают клиентов).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RuleDuration {
    /// Имя правила.
    pub rule: String,
    /// Длительность, миллисекунды.
    pub ms: u64,
}

/// Отчёт fitness-контроля.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FitnessReport {
    /// Репозиторий.
    pub repo: PathBuf,
    /// Все правила пройдены.
    pub passed: bool,
    /// Находки по правилам.
    pub issues: Vec<LintIssue>,
    /// Сводка (для отчёта).
    pub summary: String,
    /// Длительность каждого правила (порядок — как в `CONSTRAINTS.yaml`).
    #[serde(default)]
    pub durations: Vec<RuleDuration>,
    /// Источники правил при наследовании `extends` (метка → число правил;
    /// пусто, если наследования нет). Аддитивное поле SDK-контракта v1.
    #[serde(default)]
    pub inherited: Vec<SourceCount>,
    /// Overrides из `CONSTRAINTS.yaml` со статусами (active/expired/invalid;
    /// `docs/corp-spine.md`). Аддитивное поле SDK-контракта v1.
    #[serde(default)]
    pub overrides: Vec<OverrideInfo>,
}

/// Тип fitness-правила из `CONSTRAINTS.yaml`.
#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RuleKind {
    /// Regex должен найтись хотя бы в одном файле по glob.
    MustContain,
    /// Regex не должен встречаться ни в одном файле по glob (issue на каждое вхождение).
    MustNotContain,
    /// Regex обязан найтись в КАЖДОМ файле по glob (issue на каждый файл без
    /// совпадения). Пустой набор файлов — находка (glob, скорее всего, ошибочен).
    EachFileMustContain,
    /// Файл существует относительно корня репозитория.
    FileExists,
    /// В каждом каталоге по glob существует файл `path` (например, у каждого
    /// сервиса `services/*` есть `openapi.yaml`). Пустой набор каталогов —
    /// находка.
    DirMustHaveFile,
    /// Файл существует и свеж: его mtime не старше `max_age_days` дней
    /// (свежесть evidence: дата последних учений, ежегодный pentest).
    MaxAge,
    /// Команда (`bash -c`, в корне репозитория) завершается кодом 0 до таймаута.
    CommandSucceeds,
    /// Структурная проверка направления зависимостей (ADR-029): импорты
    /// каждого файла набора сопоставляются с `forbid`/`allow`-списками
    /// модулей (префикс модульного пути по сегментам). Пустой набор файлов —
    /// находка.
    DependencyDirection,
    /// Границы контекстов (ADR-030): импорты файлов не пересекают
    /// `code_roots` чужих CMP-сущностей модели (`model_dir`), если целевой
    /// CMP не объявлен в `depends_on` исходного.
    ContextBoundary,
    /// `ArchUnit`-гейт (ADR-039): java-правила этого же `CONSTRAINTS.yaml`
    /// (`dependency_direction`/`context_boundary` с glob `**/*.java`)
    /// исполняются настоящим `ArchUnit` на скомпилированных классах
    /// (standalone-раннер, общий код с `arch-ml archunit check`).
    #[serde(rename = "archunit")]
    ArchUnit,
    /// Запрещённые пакеты в манифестах зависимостей (детектор тех-радара,
    /// `docs/corp-spine.md`): построчный разбор `Cargo.toml` (секции
    /// *dependencies), `pom.xml` (`<artifactId>`), `requirements.txt`;
    /// пакет из `deny` — находка со ссылкой `reference` на решение
    /// техкомитета.
    DenyDependency,
    /// Контракт Agent Skill (гейт «md vs skill», `docs/corp-spine.md`):
    /// каждый `SKILL.md` по glob обязан иметь frontmatter (`name` в kebab-case,
    /// непустой `description` с триггер-маркером), раздел-триггер («когда
    /// применять») и структурный признак процедуры (раздел методики с ≥3
    /// шагами/пунктами). Пустой набор файлов — находка.
    SkillContract,
    /// Целостность градации playbook (площадка вызревания процедуры,
    /// `docs/corp-spine.md`): playbook с `graduated_to: X` обязан ссылаться
    /// на существующий файл репозитория и иметь `uses ≥ PLAYBOOK_USES_MIN`
    /// («правило трёх повторений»). Неградированный playbook без ссылки —
    /// не ошибка (кандидат ещё вызревает). Пустой набор файлов по glob —
    /// находка.
    PlaybookGraduation,
    /// Целостность managed-блоков (якорные диффы, `docs/corp-spine.md`):
    /// каждый файл по glob проходит `managed::lint_file` — находки
    /// `hash_mismatch` и структурные нарушения (`unclosed_block/orphan_end`/
    /// `duplicate_id`). Пустой набор файлов — находка.
    ManagedHashCurrent,
    /// Адресный след дистиллята (H0.3): каждый адрес инварианта (`spine:AD-*`,
    /// `ADR-*`, `rule:*`) обязан резолвиться; при `require` — обязательное
    /// покрытие. Резолюция детерминирована, LLM не участвует.
    AddressTrace,
}

impl RuleKind {
    /// Строковое имя типа (как в YAML) — для таблиц отчётов.
    fn as_str(self) -> &'static str {
        match self {
            Self::MustContain => "must_contain",
            Self::MustNotContain => "must_not_contain",
            Self::EachFileMustContain => "each_file_must_contain",
            Self::FileExists => "file_exists",
            Self::DirMustHaveFile => "dir_must_have_file",
            Self::MaxAge => "max_age",
            Self::CommandSucceeds => "command_succeeds",
            Self::DependencyDirection => "dependency_direction",
            Self::ContextBoundary => "context_boundary",
            Self::ArchUnit => "archunit",
            Self::DenyDependency => "deny_dependency",
            Self::SkillContract => "skill_contract",
            Self::PlaybookGraduation => "playbook_graduation",
            Self::ManagedHashCurrent => "managed_hash_current",
            Self::AddressTrace => "address_trace",
        }
    }
}

// ─── Адресный след дистиллята (H0.3) ─────────────────────────────────────

/// Разновидность адреса инварианта/правила в дистилляте.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AddressKind {
    /// `spine:AD-<n>` / `spine:AD-BE<n>`.
    Spine,
    /// `ADR-<nnn>` / `ADR-<nnn>:rule-<m>`.
    Adr,
    /// `rule:<id>`.
    Rule,
}

/// Адрес инварианта/правила, извлечённый из дистиллята.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Address {
    /// Сырой токен (как написан).
    pub raw: String,
    /// Разновидность.
    pub kind: AddressKind,
    /// Канонический ключ для резолюции: `AD-3`, `AD-BE1`, `ADR-014`, `ML-SKILL-1`.
    pub key: String,
    /// Фрагмент `rule-<m>` (для `ADR-<nnn>:rule-<m>`).
    pub frag: Option<String>,
}

/// Индекс резолвируемых узлов репозитория (строится один раз на прогон).
#[derive(Debug, Default)]
pub(crate) struct AddressIndex {
    /// Spine-идентификаторы (`"AD-3"`, `"AD-BE1"`).
    pub spine: BTreeSet<String>,
    /// ADR-записи: номер → путь файла.
    pub adr: BTreeMap<u64, String>,
    /// Fitness-правила (`"C-04"`, `"ML-SKILL-1"`).
    pub rules: BTreeSet<String>,
}

/// Заголовок инварианта в spine: `### AD-<n>` / `### AD-BE<n>` (константный
/// паттерн — ленивая компиляция один раз; `.expect` — внутренний дефект
/// кода, ловится тестами на старте).
static AD_HEADING_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?m)^\s{0,3}#{1,6}\s+AD-(BE)?(\d+)\b").expect("константный regex заголовков AD")
});

/// Строит индекс резолвируемых узлов репозитория (spine + ADR + правила).
pub(crate) fn build_address_index(root: &Path) -> Result<AddressIndex> {
    let mut idx = AddressIndex::default();
    for name in [
        "ARCHITECTURE-SPINE.md",
        "ARCHITECTURE-SPINE-BE.md",
        "docs/ARCHITECTURE-SPINE.md",
        "docs/ARCHITECTURE-SPINE-BE.md",
    ] {
        let p = root.join(name);
        if !p.is_file() {
            continue;
        }
        let text = std::fs::read_to_string(&p).map_err(|e| HarnessError::io(&p, e))?;
        for caps in AD_HEADING_RE.captures_iter(&text) {
            let be = caps.get(1).map_or("", |m| m.as_str());
            let n = caps.get(2).map_or("", |m| m.as_str());
            idx.spine.insert(format!("AD-{be}{n}"));
        }
    }
    let report = crate::adr_registry::build_registry(root)?;
    for e in &report.entries {
        idx.adr.entry(e.number).or_insert_with(|| e.file.clone());
    }
    let c = root.join("CONSTRAINTS.yaml");
    if c.is_file() {
        let text = std::fs::read_to_string(&c).map_err(|e| HarnessError::io(&c, e))?;
        if let Ok(doc) = serde_yaml_ng::from_str::<serde_yaml_ng::Value>(&text) {
            if let Some(rules) = doc.get("constraints").and_then(|v| v.as_sequence()) {
                for r in rules {
                    if let Some(id) = r.get("id").and_then(|v| v.as_str()) {
                        idx.rules.insert(id.to_string());
                    }
                }
            }
        }
    }
    Ok(idx)
}

/// Классифицирует сырой адрес в `(kind, key, frag)`.
fn classify_address(raw: &str) -> (AddressKind, String, Option<String>) {
    if let Some(rest) = raw.strip_prefix("spine:AD-BE") {
        return (AddressKind::Spine, format!("AD-BE{}", rest.trim()), None);
    }
    if let Some(rest) = raw.strip_prefix("spine:AD-") {
        return (AddressKind::Spine, format!("AD-{}", rest.trim()), None);
    }
    if let Some(rest) = raw.strip_prefix("ADR-") {
        let (num, frag) = match rest.split_once(':') {
            Some((n, f)) => (n, Some(f.to_string())),
            None => (rest, None),
        };
        let n: u64 = num.trim().parse().unwrap_or(0);
        return (AddressKind::Adr, format!("ADR-{n:03}"), frag);
    }
    if let Some(id) = raw.strip_prefix("rule:") {
        return (AddressKind::Rule, id.to_string(), None);
    }
    // Голый `AD-<n>` (для require) — spine.
    if let Some(rest) = raw.strip_prefix("AD-") {
        return (AddressKind::Spine, format!("AD-{}", rest.trim()), None);
    }
    (AddressKind::Rule, raw.to_string(), None)
}

/// Адрес инварианта в тексте дистиллята: `spine:AD-(BE)<n>`, `ADR-<n>[:frag]`
/// или `rule:<id>` (константный паттерн — ленивая компиляция один раз;
/// `.expect` — внутренний дефект кода, ловится тестами на старте).
static ADDRESS_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?m)(?:spine:AD-(?:BE)?\d+|ADR-\d{1,4}(?::[A-Za-z0-9][A-Za-z0-9_-]*)?|rule:[A-Za-z][A-Za-z0-9_-]*)",
    )
    .expect("константный regex адресов инвариантов")
});

/// Извлекает адреса из текста (обе формы: `trace:` и inline `[…]`).
pub(crate) fn extract_addresses(text: &str) -> Vec<Address> {
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut out = Vec::new();
    for m in ADDRESS_RE.find_iter(text) {
        let raw = m.as_str().to_string();
        if !seen.insert(raw.clone()) {
            continue;
        }
        let (kind, key, frag) = classify_address(&raw);
        out.push(Address {
            raw,
            kind,
            key,
            frag,
        });
    }
    out
}

/// Проверяет след дистиллята: резолюция адресов + обязательное покрытие.
pub(crate) fn address_trace_violations(
    text: &str,
    index: &AddressIndex,
    require: &[String],
    pattern: Option<&str>,
) -> Vec<String> {
    let pat = pattern.and_then(|p| Regex::new(p).ok());
    let addresses = extract_addresses(text);
    let filtered: Vec<&Address> = addresses
        .iter()
        .filter(|a| pat.as_ref().is_none_or(|re| re.is_match(&a.raw)))
        .collect();
    let mut out = Vec::new();
    if filtered.is_empty() {
        out.push("пустой след: нет ни одного адреса инварианта".to_string());
        return out;
    }
    for a in &filtered {
        let resolved = match a.kind {
            AddressKind::Spine => index.spine.contains(&a.key),
            AddressKind::Rule => index.rules.contains(&a.key),
            AddressKind::Adr => {
                let n: u64 = a.key.trim_start_matches("ADR-").parse().unwrap_or(0);
                match index.adr.get(&n) {
                    None => false,
                    Some(file) => match &a.frag {
                        None => true,
                        Some(frag) => crate::adr_registry::anchor_present(Path::new(file), frag)
                            .unwrap_or(false),
                    },
                }
            }
        };
        if !resolved {
            out.push(format!("адрес {} не резолвится", a.raw));
        }
    }
    for req in require {
        let (_, key, _) = classify_address(req);
        if !filtered.iter().any(|a| a.key == key) {
            out.push(format!("обязательный адрес {req} не покрыт"));
        }
    }
    out
}

/// Одно правило из `CONSTRAINTS.yaml`.
///
/// Публичная структура (нужна `ArchUnit`-мосту на CLI-краю, ADR-039); поля
/// крейт-видимые — внешние потребители работают через [`check`] и JSON-отчёт.
#[derive(Debug, Deserialize)]
pub struct FitnessRule {
    /// Имя правила (становится кодом находки).
    pub(crate) name: String,
    /// Идентификатор правила (например, `C-12`; dogfood-набор и библиотека
    /// правил). Используется `ArchUnit`-мостом (ADR-039) как id правила в
    /// сообщениях; движок находок по-прежнему оперирует `name`.
    #[serde(default)]
    pub(crate) id: Option<String>,
    /// Тип проверки. Может опускаться у записей `unverifiable: true`
    /// (ручной контроль, `docs/corp-spine.md`) — движок их не исполняет.
    #[serde(rename = "type", default = "default_rule_kind")]
    pub(crate) kind: RuleKind,
    /// Glob'ы набора файлов (для content-правил, `dependency_direction` и
    /// `context_boundary`) и каталогов (для `dir_must_have_file`); строка
    /// или список строк, дефолт `**/*`. Наборы по нескольким glob'ам
    /// объединяются (ADR-029: слоевые правила покрывают `src/x.rs` +
    /// `src/x/**` одним правилом).
    #[serde(default, deserialize_with = "de_string_or_list")]
    pub(crate) glob: Vec<String>,
    /// Regex (для `must_contain/must_not_contain/each_file_must_contain`).
    pattern: Option<String>,
    /// Путь относительно репозитория (для `file_exists` и `max_age`) или
    /// относительно каждого каталога набора (для `dir_must_have_file`).
    path: Option<String>,
    /// Обязательные адреса покрытия (для `address_trace`, H0.3): каждый адрес
    /// обязан присутствовать в следе файла (покрытие-надмножество).
    #[serde(default)]
    pub(crate) require: Vec<String>,
    /// Glob'ы исключений из набора (строка или список строк; для
    /// content-правил и `dir_must_have_file`). Файлы/каталоги, попавшие под
    /// исключение, вычитаются из набора ДО проверки — легитимные точечные
    /// отступления (например, сборщик витрины, которому JOIN разрешён).
    #[serde(default, deserialize_with = "de_string_or_list")]
    exclude_glob: Vec<String>,
    /// Максимальный возраст файла в днях (для `max_age`; mtime не старше
    /// `now − max_age_days`).
    max_age_days: Option<u64>,
    /// Команда (для `command_succeeds`).
    command: Option<String>,
    /// Запрещённые модули (для `dependency_direction`, ADR-029): префиксы
    /// модульного пути в координатах импортов (`agent`, `com/bank/legacy`).
    /// Ровно одно из `forbid`/`allow` обязательно.
    pub(crate) forbid: Option<Vec<String>>,
    /// Разрешённые модули (для `dependency_direction`): импорт обязан
    /// префиксно совпадать с одним из них; пустой список — запрет любых
    /// внутрикрейтовых зависимостей (листовые модули).
    pub(crate) allow: Option<Vec<String>>,
    /// Каталог модели архитектуры относительно корня репозитория (для
    /// `context_boundary`, ADR-030; дефолт `model`).
    pub(crate) model_dir: Option<String>,
    /// Каталог скомпилированных классов JVM-проекта относительно корня
    /// репозитория (для `archunit`, ADR-039; без него — авто-детект
    /// `target/classes`, `build/classes/java/main`, `out/production`,
    /// `classes`).
    #[serde(default)]
    pub(crate) classes_dir: Option<String>,
    /// Каталог с jar'ами `ArchUnit` относительно корня репозитория (для
    /// `archunit`; без него — `--jar-dir` / `ARCHUNIT_HOME` /
    /// `~/.arch-ml/archunit/lib`).
    #[serde(default)]
    pub(crate) jar_dir: Option<String>,
    /// Карточка правила (метаданные из шаблона дистилляции источников,
    /// необязательны): признак применимости.
    /// Поля схемы — читаются внешними потребителями YAML; движок
    /// использует пока только owner/expiry (expiry-находка).
    #[serde(default)]
    #[allow(dead_code)]
    trigger: Option<String>,
    /// Карточка правила: какой отказ предотвращается (одно предложение).
    #[serde(default)]
    #[allow(dead_code)]
    rationale: Option<String>,
    /// Карточка правила: артефакт, остающийся после проверки.
    #[serde(default)]
    #[allow(dead_code)]
    evidence: Option<String>,
    /// Карточка правила: стоимость отмены (обратимо / дорого / необратимо).
    #[serde(default)]
    #[allow(dead_code)]
    reversibility: Option<String>,
    /// Карточка правила: владелец.
    #[serde(default)]
    owner: Option<String>,
    /// Карточка правила: дата пересмотра (YYYY-MM-DD). Просроченное правило —
    /// находка уровня warn (антипаттерн «правило без срока жизни» — теперь
    /// механически видно).
    #[serde(default)]
    expiry: Option<String>,
    /// Карточка правила: оценка стоимости сопровождения в человеко-часах
    /// (метаданные; движок не enforce'ит — суммируется в `rules_report`).
    #[serde(default)]
    effort_hours: Option<f64>,
    /// Связь «правило ← требование» (адаптер `OpenSpec`, `src/openspec.rs`):
    /// идентификаторы требований вида `openspec:<capability>#<hash8>`,
    /// которые покрывает это правило. Движок находок поле не использует;
    /// читает отчёт покрытия `arch-ml openspec coverage`.
    #[serde(default)]
    #[allow(dead_code)]
    covers: Vec<String>,
    /// Glob'ы манифестов зависимостей (для `deny_dependency`; строка или
    /// список). Пустой — auto: `**/Cargo.toml`, `**/pom.xml`,
    /// `**/requirements.txt`.
    #[serde(default, deserialize_with = "de_string_or_list")]
    manifests: Vec<String>,
    /// Запрещённые имена пакетов (для `deny_dependency`; строка или список).
    #[serde(default, deserialize_with = "de_string_or_list")]
    deny: Vec<String>,
    /// Ссылка на основание запрета — решение техкомитета/ADR (для
    /// `deny_dependency`; попадает в текст находки).
    #[serde(default)]
    reference: Option<String>,
    /// Метка источника правила при наследовании (`extends`,
    /// `docs/corp-spine.md`): `<ref>@<version>` родительского файла;
    /// `None` — собственное правило этого файла. Из YAML не читается,
    /// проставляется резолвером [`load_constraints_resolved`].
    #[serde(skip)]
    pub(crate) source: Option<String>,
    /// Признак ручного контроля (`docs/corp-spine.md`): требование/стандарт
    /// не механизуется — движок правило НЕ исполняет, но оно видно в
    /// отчётах (`control report`, поле `unverifiable_rules`) как осознанный
    /// долг с owner. Поле `type` у таких записей можно опускать.
    #[serde(default)]
    pub(crate) unverifiable: bool,
    /// Критичность находок правила: error|warn (дефолт error).
    #[serde(default = "default_severity")]
    pub(crate) severity: String,
    /// Таймаут исполнения, секунды. Дефолт по типу правила: 60 для
    /// `command_succeeds`, 300 для `archunit` (ADR-039).
    #[serde(default)]
    pub(crate) timeout_secs: Option<u64>,
}

fn default_severity() -> String {
    "error".into()
}

/// Дефолтный тип правила: используется только у записей `unverifiable: true`
/// (без `type`; движок их не исполняет, значение не достигает исполнения).
fn default_rule_kind() -> RuleKind {
    RuleKind::MustContain
}

/// Десериализация поля-исключения: принимает и одиночную строку, и список
/// строк (агенты и люди пишут оба варианта; `|` внутри glob НЕ
/// поддерживается — это отдельные glob'ы, а не regex-альтернатива).
fn de_string_or_list<'de, D>(deserializer: D) -> std::result::Result<Vec<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum StringOrList {
        /// Одиночный glob.
        One(String),
        /// Список glob'ов.
        Many(Vec<String>),
    }
    Ok(match StringOrList::deserialize(deserializer)? {
        StringOrList::One(s) => vec![s],
        StringOrList::Many(v) => v,
    })
}

/// Вычитает `exclude_glob`'ы правила из набора относительных путей.
fn apply_excludes<T: AsRef<str>>(paths: &mut Vec<T>, excludes: &[String]) {
    if excludes.is_empty() {
        return;
    }
    paths.retain(|p| {
        let s = p.as_ref();
        !excludes.iter().any(|ex| glob_matches(ex, s))
    });
}

/// Дефолтный таймаут `command_succeeds`, секунды.
const COMMAND_TIMEOUT_DEFAULT_SECS: u64 = 60;

/// Auto-набор манифестов для `deny_dependency` (поле `manifests` не задано).
const DEFAULT_MANIFEST_GLOBS: [&str; 3] = ["**/Cargo.toml", "**/pom.xml", "**/requirements.txt"];

/// Построчный разбор `Cargo.toml`: имена пакетов из секций `[dependencies]`,
/// `[dev-dependencies]`, `[build-dependencies]` (и `*.dependencies` —
/// target-специфичных). Имя — ключ до `=`; значения не разбираются.
/// Возвращает (пакет, строка 1-based).
fn parse_cargo_manifest_deps(content: &str) -> Vec<(String, usize)> {
    let mut out = Vec::new();
    let mut in_deps = false;
    for (idx, raw) in content.lines().enumerate() {
        let line = raw.trim();
        if line.starts_with('[') {
            let section = line.trim_matches(['[', ']']).trim();
            in_deps = section == "dependencies"
                || section == "dev-dependencies"
                || section == "build-dependencies"
                || section.ends_with(".dependencies");
            continue;
        }
        if !in_deps || line.is_empty() || line.starts_with('#') {
            continue;
        }
        let key = line.split('=').next().unwrap_or_default().trim();
        let name = key
            .split_whitespace()
            .next()
            .unwrap_or_default()
            .trim_matches('"')
            .trim_matches('\'');
        if !name.is_empty()
            && name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
        {
            out.push((name.to_string(), idx + 1));
        }
    }
    out
}

/// Построчный разбор `pom.xml`: все `<artifactId>` (эвристика MVP: собственный
/// artifactId проекта и parent тоже попадают в список — задокументировано;
/// ложное срабатывание возможно только при совпадении имени проекта с
/// deny-пакетом). Возвращает (пакет, строка 1-based).
fn parse_pom_manifest_deps(content: &str) -> Result<Vec<(String, usize)>> {
    let re = diff_regex(r"<artifactId>\s*([^<]+?)\s*</artifactId>")?;
    let mut out = Vec::new();
    for (idx, line) in content.lines().enumerate() {
        for caps in re.captures_iter(line) {
            out.push((caps[1].to_string(), idx + 1));
        }
    }
    Ok(out)
}

/// Построчный разбор `requirements.txt`: имя пакета — до спецификатора
/// версии (`==`/`>=`/`~=`/`[` и т.п.); комментарии `#`, опции (`-r`, `-e`)
/// и пустые строки пропускаются. Возвращает (пакет, строка 1-based).
fn parse_requirements_manifest_deps(content: &str) -> Vec<(String, usize)> {
    let mut out = Vec::new();
    for (idx, raw) in content.lines().enumerate() {
        let line = raw.split(" #").next().unwrap_or_default().trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with('-') {
            continue;
        }
        let name = line
            .split(['=', '<', '>', '!', '~', '[', ';', ' '])
            .next()
            .unwrap_or_default()
            .trim();
        if !name.is_empty() {
            out.push((name.to_string(), idx + 1));
        }
    }
    out
}

/// Зависимости манифеста по имени файла (для `deny_dependency`).
fn manifest_deps(rel: &str, content: &str) -> Result<Vec<(String, usize)>> {
    let name = Path::new(rel)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or_default();
    if name == "Cargo.toml" {
        Ok(parse_cargo_manifest_deps(content))
    } else if name == "pom.xml" {
        parse_pom_manifest_deps(content)
    } else {
        // requirements.txt и любые иные *.txt по glob — формат pip.
        Ok(parse_requirements_manifest_deps(content))
    }
}

/// Исключение правила через ADR (поле верхнего уровня `overrides:`,
/// `docs/corp-spine.md`). Все три поля обязательны к заполнению: неполный
/// override — error-находка и игнорируется (гейт «только через ADR»).
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct OverrideEntry {
    /// Идентификатор (`id`) или имя (`name`) отключаемого правила.
    #[serde(default)]
    pub rule: Option<String>,
    /// Номер ADR, разрешающего отступление.
    #[serde(default)]
    pub adr: Option<String>,
    /// Срок действия: `YYYY-MM` (по месяцу включительно) или `YYYY-MM-DD`.
    #[serde(default)]
    pub until: Option<String>,
}

/// Статус override после оценки (отчёт `check` и `control report`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OverrideInfo {
    /// Правило (как записано).
    pub rule: String,
    /// ADR (как записан, может быть пустым при неполном override).
    pub adr: String,
    /// Срок (как записан).
    pub until: String,
    /// `active` (правило отключено) / `expired` (просрочен, правило снова
    /// действует) / `invalid` (неполный, некорректная дата или правило не
    /// найдено — игнорируется).
    pub status: String,
    /// Пояснение (текст находки).
    pub note: String,
}

/// Корень `CONSTRAINTS.yaml`.
#[derive(Debug, Deserialize)]
struct ConstraintsFile {
    /// Список правил (канонический корень `rules:`).
    #[serde(default)]
    rules: Vec<FitnessRule>,
    /// Альтернативный корень `constraints:` (кейсы и handoff-пакеты).
    #[serde(default)]
    constraints: Vec<FitnessRule>,
    /// Наследование (`docs/corp-spine.md`): родительские constraint-файлы
    /// в форме `<ref>@<version>` — правила подмешиваются с меткой
    /// источника, пин версии проверяется против поля `version` родителя.
    #[serde(default)]
    extends: Vec<String>,
    /// Версия этого файла (обязательна у родительских constraint-файлов —
    /// по ней дочерние проверяют свой пин в `extends`).
    #[serde(default)]
    version: Option<String>,
    /// Исключения правил через ADR (см. [`OverrideEntry`]).
    #[serde(default)]
    overrides: Vec<OverrideEntry>,
}

impl ConstraintsFile {
    /// Все правила из обоих допустимых корней.
    fn all_rules(&self) -> impl Iterator<Item = &FitnessRule> {
        self.rules.iter().chain(self.constraints.iter())
    }
}

/// Читает и разбирает `CONSTRAINTS.yaml` в список правил (оба корня —
/// `rules:` и `constraints:`). Общий парсер для `check`, `ArchUnit`-моста
/// (ADR-039) и CLI.
///
/// Наследование (`extends`) здесь НЕ разрешается — только плоский файл;
/// полный резолв с метками источника и проверкой пинов — в
/// [`load_constraints_resolved`].
///
/// # Errors
/// Файл не читается, YAML невалиден.
pub fn load_fitness_rules(constraints: &Path) -> Result<Vec<FitnessRule>> {
    let yaml =
        std::fs::read_to_string(constraints).map_err(|e| HarnessError::io(constraints, e))?;
    let parsed: ConstraintsFile = serde_yaml_ng::from_str(&yaml)?;
    let ConstraintsFile {
        rules, constraints, ..
    } = parsed;
    Ok(rules.into_iter().chain(constraints).collect())
}

/// Что за файл правил найден дефолтным поиском ([`resolve_ruleset`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RulesetKind {
    /// `<repo>/CONSTRAINTS.yaml` — рабочий ruleset репозитория/кейса.
    Working,
    /// `<repo>/.arch-handoff/CONSTRAINTS.yaml` — файл handoff-пакета
    /// (заготовка генератора либо копия рабочего ruleset).
    PacketStub,
}

impl RulesetKind {
    /// Человекочитаемая метка источника для вывода.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Working => "рабочий ruleset",
            Self::PacketStub => "пакетная заготовка",
        }
    }
}

/// Файл правил, найденный дефолтным поиском.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedRuleset {
    /// Путь к `CONSTRAINTS.yaml`.
    pub path: PathBuf,
    /// Что это за файл (см. [`RulesetKind`]).
    pub kind: RulesetKind,
}

impl ResolvedRuleset {
    /// Предупреждение для вывода, если найден только пакетный файл:
    /// проверяется заготовка, а не рабочий ruleset кейса. PASS по ней —
    /// не полноценный контроль. `None` — найден рабочий ruleset.
    #[must_use]
    pub fn warning(&self, repo: &Path) -> Option<String> {
        match self.kind {
            RulesetKind::Working => None,
            RulesetKind::PacketStub => Some(format!(
                "ruleset: проверяется пакетная заготовка {}, а не рабочий ruleset — {} \
                 не найден. PASS по заготовке не является полноценным контролем; \
                 передайте --constraints или создайте рабочий CONSTRAINTS.yaml",
                self.path.display(),
                repo.join("CONSTRAINTS.yaml").display()
            )),
        }
    }
}

/// Дефолтный поиск файла правил репозитория.
///
/// Приоритет: рабочий `<repo>/CONSTRAINTS.yaml`, затем — как fallback —
/// пакетный `<repo>/.arch-handoff/CONSTRAINTS.yaml`. Обратный приоритет
/// (пакетный первым) давал «зелёный по пустоте»: в каталоге кейса дефолтный
/// `control check` проверял 1 правило заготовки вместо рабочего ruleset'а
/// (C-037, ADR-011). Вызывающий обязан предупредить о fallback —
/// [`ResolvedRuleset::warning`].
///
/// `None` — ни одного файла нет.
#[must_use]
pub fn resolve_ruleset(repo: &Path) -> Option<ResolvedRuleset> {
    let working = repo.join("CONSTRAINTS.yaml");
    if working.is_file() {
        return Some(ResolvedRuleset {
            path: working,
            kind: RulesetKind::Working,
        });
    }
    let packet = repo.join(".arch-handoff/CONSTRAINTS.yaml");
    packet.is_file().then_some(ResolvedRuleset {
        path: packet,
        kind: RulesetKind::PacketStub,
    })
}

/// Как [`resolve_ruleset`], но отсутствие обоих файлов — явная ошибка
/// с перечислением ожидаемых путей (вместо невнятного io-сбоя).
///
/// # Errors
/// Ни `<repo>/CONSTRAINTS.yaml`, ни `<repo>/.arch-handoff/CONSTRAINTS.yaml`
/// не найдены.
pub fn resolve_ruleset_required(repo: &Path) -> Result<ResolvedRuleset> {
    resolve_ruleset(repo).ok_or_else(|| {
        HarnessError::Control(format!(
            "ruleset не найден: ни {} (рабочий), ни {} (пакетная заготовка)",
            repo.join("CONSTRAINTS.yaml").display(),
            repo.join(".arch-handoff/CONSTRAINTS.yaml").display()
        ))
    })
}

/// Переменная окружения с каталогом-реестром родительских
/// constraint-файлов (резолв непутевых ref в `extends`; без сети,
/// `docs/corp-spine.md`).
pub const CONSTRAINTS_REGISTRY_ENV: &str = "ARCH_CONSTRAINTS_REGISTRY";

/// Разрешённый родитель из `extends` (метка источника для вывода).
#[derive(Debug, Clone, Serialize)]
pub struct ResolvedParent {
    /// Запись как в `extends` (`<ref>@<pin>`).
    pub reference: String,
    /// Разрешённый путь к файлу.
    pub path: PathBuf,
    /// Запиненная версия из записи.
    pub pinned: String,
    /// Фактическая версия из поля `version` родителя (`None` — не задана).
    pub actual: Option<String>,
    /// Число унаследованных из этого файла правил (включая транзитивные).
    pub rules: usize,
}

/// Счётчик правил по источнику (вывод `check` и `report`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SourceCount {
    /// Метка источника (`<ref>@<version>` либо `own` — собственные).
    pub source: String,
    /// Число правил.
    pub rules: usize,
}

/// Результат полного резолва constraint-файла: правила всех уровней с
/// метками источника, родители, overrides и находки резолва (расхождение
/// пина версии — error-находка, `docs/corp-spine.md`).
#[derive(Debug)]
pub struct ResolvedConstraints {
    /// Слитые правила: родительские (по порядку `extends`, транзитивно)
    /// первыми, затем собственные; собственное правило с именем
    /// унаследованного замещает его (shadowing).
    pub rules: Vec<FitnessRule>,
    /// Непосредственные родители (для отчётности).
    pub parents: Vec<ResolvedParent>,
    /// Overrides всех уровней (родительские + собственные).
    pub overrides: Vec<OverrideEntry>,
    /// Находки резолва (несовпадение пина версии, родитель без `version`).
    pub findings: Vec<LintIssue>,
}

/// Разбирает запись `extends` на (ref, пин версии): разделитель — последний
/// `@` (в путях `@` практически не встречается, в версиях — тем более).
fn parse_extends_ref(entry: &str) -> Result<(&str, &str)> {
    entry
        .rsplit_once('@')
        .filter(|(r, p)| !r.is_empty() && !p.is_empty())
        .ok_or_else(|| {
            HarnessError::Control(format!(
                "extends: запись '{entry}' не вида <ref>@<version> — пин версии обязателен"
            ))
        })
}

/// Признак «ref похож на путь» для `extends`: содержит `/`, начинается с
/// `.` или имеет yaml-расширение (регистр не важен).
fn extends_ref_looks_like_path(reference: &str) -> bool {
    reference.contains('/')
        || reference.starts_with('.')
        || Path::new(reference)
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("yaml") || ext.eq_ignore_ascii_case("yml"))
}

/// Разрешает ref из `extends` в путь к файлу (без сети,
/// `docs/corp-spine.md`): ref с `/`, ведущей `.` или расширением
/// `.yaml`/`.yml` — путь относительно каталога текущего файла; «голое» имя
/// — поиск `<ref>.yaml`/`<ref>.yml` в реестре: каталог из
/// [`CONSTRAINTS_REGISTRY_ENV`], иначе `constraints.d/` рядом с текущим
/// файлом.
fn resolve_extends_path(reference: &str, current_file: &Path) -> Result<PathBuf> {
    let base_dir = current_file.parent().unwrap_or_else(|| Path::new("."));
    if extends_ref_looks_like_path(reference) {
        let path = base_dir.join(reference);
        return path.is_file().then_some(path).ok_or_else(|| {
            HarnessError::Control(format!(
                "extends: родительский файл не найден: {reference} (от {})",
                current_file.display()
            ))
        });
    }
    let mut roots = Vec::new();
    if let Ok(registry) = std::env::var(CONSTRAINTS_REGISTRY_ENV) {
        roots.push(PathBuf::from(registry));
    }
    roots.push(base_dir.join("constraints.d"));
    for root in &roots {
        for ext in ["yaml", "yml"] {
            let candidate = root.join(format!("{reference}.{ext}"));
            if candidate.is_file() {
                return Ok(candidate);
            }
        }
    }
    Err(HarnessError::Control(format!(
        "extends: '{reference}' не найден в реестре ({} или {}/constraints.d)",
        CONSTRAINTS_REGISTRY_ENV,
        base_dir.display()
    )))
}

/// Метка источника для правил родителя: `<имя>@<версия>`, где имя — ref как
/// записан (для путевого ref — имя файла без расширения), версия —
/// фактическая из родителя (иначе пин, иначе `?`).
fn source_label(reference: &str, path: &Path, pinned: &str, actual: Option<&str>) -> String {
    let name = if extends_ref_looks_like_path(reference) {
        path.file_stem().map_or_else(
            || reference.to_string(),
            |s| s.to_string_lossy().to_string(),
        )
    } else {
        reference.to_string()
    };
    format!("{name}@{}", actual.unwrap_or(pinned))
}

/// Рекурсивный резолв constraint-файла; `stack` — цепочка канонических
/// путей от корневого файла (детектор циклов `extends`).
fn resolve_constraints(path: &Path, stack: &mut Vec<PathBuf>) -> Result<ResolvedConstraints> {
    let canonical = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
    if stack.contains(&canonical) {
        let mut cycle: Vec<String> = stack.iter().map(|p| p.display().to_string()).collect();
        cycle.push(canonical.display().to_string());
        return Err(HarnessError::Control(format!(
            "extends: цикл наследования: {}",
            cycle.join(" → ")
        )));
    }
    stack.push(canonical);

    let yaml = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let parsed: ConstraintsFile = serde_yaml_ng::from_str(&yaml)?;

    let mut rules = Vec::new();
    let mut parents = Vec::new();
    let mut overrides = Vec::new();
    let mut findings = Vec::new();

    for entry in &parsed.extends {
        let (reference, pinned) = parse_extends_ref(entry)?;
        let parent_path = resolve_extends_path(reference, path)?;
        let parent = resolve_constraints(&parent_path, stack)?;
        // Пин версии — «PR с diff»: расхождение — error-находка, а не
        // молчаливая поломка и не тихий пропуск обновления родителя.
        let actual = parent_actual_version(&parent_path)?;
        match &actual {
            Some(actual) if actual != pinned => {
                findings.push(LintIssue {
                    file: path.to_path_buf(),
                    line: 0,
                    rule: "extends".to_string(),
                    message: format!(
                        "extends: родитель обновился: {reference}@{pinned} → {actual} — перепиновать осознанно"
                    ),
                    severity: "error".to_string(),
                });
            }
            Some(_) => {}
            None => {
                findings.push(LintIssue {
                    file: path.to_path_buf(),
                    line: 0,
                    rule: "extends".to_string(),
                    message: format!(
                        "extends: у родителя {reference} нет поля version — пин {pinned} проверить нельзя"
                    ),
                    severity: "error".to_string(),
                });
            }
        }
        let label = source_label(reference, &parent_path, pinned, actual.as_deref());
        let mut inherited = parent.rules;
        let inherited_count = inherited.len();
        for rule in &mut inherited {
            if rule.source.is_none() {
                rule.source = Some(label.clone());
            }
        }
        parents.push(ResolvedParent {
            reference: entry.clone(),
            path: parent_path,
            pinned: pinned.to_string(),
            actual,
            rules: inherited_count,
        });
        rules.extend(inherited);
        overrides.extend(parent.overrides);
        findings.extend(parent.findings);
    }

    let own_rules: Vec<FitnessRule> = parsed.rules.into_iter().chain(parsed.constraints).collect();
    // Shadowing: собственное правило с тем же именем замещает унаследованное.
    let own_names: BTreeSet<&str> = own_rules.iter().map(|r| r.name.as_str()).collect();
    rules.retain(|r| !own_names.contains(r.name.as_str()));
    rules.extend(own_rules);
    overrides.extend(parsed.overrides);

    stack.pop();
    Ok(ResolvedConstraints {
        rules,
        parents,
        overrides,
        findings,
    })
}

/// Версия constraint-файла (поле верхнего уровня `version`) — читается
/// родителем дочернего файла при проверке пина.
fn parent_actual_version(path: &Path) -> Result<Option<String>> {
    let yaml = std::fs::read_to_string(path).map_err(|e| HarnessError::io(path, e))?;
    let parsed: ConstraintsFile = serde_yaml_ng::from_str(&yaml)?;
    Ok(parsed.version)
}

/// Полный резолв `CONSTRAINTS.yaml`: наследование `extends` (транзитивно,
/// с метками источника и проверкой пинов версий), слияние overrides.
///
/// # Errors
/// Файл не читается/невалиден, родитель из `extends` не найден, цикл
/// наследования, запись `extends` без пина версии.
pub fn load_constraints_resolved(constraints: &Path) -> Result<ResolvedConstraints> {
    resolve_constraints(constraints, &mut Vec::new())
}

/// Разбирает срок override: `YYYY-MM-DD` либо `YYYY-MM` (год-месяц; срок
/// действует по указанный месяц включительно). Возвращает (год, месяц,
/// день); день 0 — форма YYYY-MM.
fn parse_until(raw: &str) -> Option<(i32, u32, u32)> {
    use chrono::Datelike as _;
    let raw = raw.trim();
    if let Ok(date) = chrono::NaiveDate::parse_from_str(raw, "%Y-%m-%d") {
        return Some((date.year(), date.month(), date.day()));
    }
    let (y, m) = raw.split_once('-')?;
    if m.len() != 2 || y.len() != 4 {
        return None;
    }
    Some((y.parse().ok()?, m.parse().ok()?, 0)).filter(|(y, m, _)| {
        *m >= 1 && *m <= 12 && chrono::NaiveDate::from_ymd_opt(*y, *m, 1).is_some()
    })
}

/// Истёк ли срок override на сегодня (для YYYY-MM — по месяцу: истёк, когда
/// текущий месяц ПОЗЖЕ указанного).
fn until_expired(y: i32, m: u32, d: u32) -> bool {
    use chrono::Datelike as _;
    let today = chrono::Local::now().date_naive();
    if d > 0 {
        chrono::NaiveDate::from_ymd_opt(y, m, d).is_some_and(|date| date < today)
    } else {
        (i64::from(y) * 12 + i64::from(m))
            < (i64::from(today.year()) * 12 + i64::from(today.month()))
    }
}

/// Оценивает overrides против итогового набора правил: статусы для отчёта,
/// находки (неполный override — error; просроченный или на несуществующее
/// правило — warn) и множество отключённых активными overrides имён правил.
fn evaluate_overrides(
    overrides: &[OverrideEntry],
    rules: &[FitnessRule],
    file: &Path,
) -> (Vec<OverrideInfo>, Vec<LintIssue>, BTreeSet<String>) {
    let mut infos = Vec::new();
    let mut findings = Vec::new();
    let mut disabled = BTreeSet::new();
    let finding = |severity: &str, message: String| LintIssue {
        file: file.to_path_buf(),
        line: 0,
        rule: "override".to_string(),
        message,
        severity: severity.to_string(),
    };
    for entry in overrides {
        let rule = entry.rule.as_deref().unwrap_or("").trim();
        let adr = entry.adr.as_deref().unwrap_or("").trim();
        let until = entry.until.as_deref().unwrap_or("").trim();
        let mut info = OverrideInfo {
            rule: rule.to_string(),
            adr: adr.to_string(),
            until: until.to_string(),
            status: "invalid".to_string(),
            note: String::new(),
        };
        // Гейт «override только через ADR»: все три поля обязательны.
        let missing: Vec<&str> = [
            rule.is_empty().then_some("rule"),
            adr.is_empty().then_some("adr"),
            until.is_empty().then_some("until"),
        ]
        .into_iter()
        .flatten()
        .collect();
        if !missing.is_empty() {
            info.note = format!("неполный override (нет {})", missing.join(", "));
            findings.push(finding(
                "error",
                format!(
                    "override: неполная запись для '{rule}' — требуются rule+adr+until ({})",
                    info.note
                ),
            ));
            infos.push(info);
            continue;
        }
        let Some((y, m, d)) = parse_until(until) else {
            info.note = format!("некорректный until '{until}' (ожидается YYYY-MM или YYYY-MM-DD)");
            findings.push(finding("error", format!("override {rule}: {}", info.note)));
            infos.push(info);
            continue;
        };
        let targets: Vec<&FitnessRule> = rules
            .iter()
            .filter(|r| r.name == rule || r.id.as_deref() == Some(rule))
            .collect();
        if targets.is_empty() {
            info.note = format!("правило '{rule}' не найдено (ни id, ни name)");
            findings.push(finding(
                "warn",
                format!("override на несуществующее правило '{rule}' (adr {adr}) — игнорируется"),
            ));
            infos.push(info);
            continue;
        }
        if until_expired(y, m, d) {
            info.status = "expired".to_string();
            info.note = format!("override истёк {until} — правило снова действует");
            findings.push(finding(
                "warn",
                format!("override '{rule}' (adr {adr}) истёк {until} — правило снова действует"),
            ));
            infos.push(info);
            continue;
        }
        info.status = "active".to_string();
        info.note = format!("правило отключено до {until} (adr {adr})");
        for target in targets {
            disabled.insert(target.name.clone());
        }
        infos.push(info);
    }
    (infos, findings, disabled)
}

/// Прогон fitness functions из `CONSTRAINTS.yaml` по репозиторию.
///
/// `passed = true`, если нет находок с severity `error`. Обход репозитория
/// пропускает каталоги `.git` и `target`; файлы в не-UTF8 кодировке читаются
/// с потерями (content-правила по ним приблизительны). Наследование
/// `extends` резолвится ([`load_constraints_resolved`]): унаследованные
/// правила несут метку источника (отчёт `inherited`), расхождение пина
/// версии — error-находка; активные overrides отключают свои правила
/// (отчёт `overrides`, `docs/corp-spine.md`).
///
/// # Errors
/// `CONSTRAINTS.yaml` не читается/не валиден, репозиторий недоступен,
/// правило некорректно (нет pattern/path/command, невалидный regex/severity),
/// родитель из `extends` не найден, цикл наследования.
pub fn check(repo: &Path, constraints: &Path) -> Result<FitnessReport> {
    if !repo.is_dir() {
        return Err(HarnessError::Control(format!(
            "репозиторий недоступен: {}",
            repo.display()
        )));
    }
    let resolved = load_constraints_resolved(constraints)?;
    if resolved.rules.is_empty() {
        return Err(HarnessError::Control(format!(
            "{}: файл не содержит правил — ожидается непустой корень `rules:`/`constraints:` или `extends:`",
            constraints.display()
        )));
    }
    let (override_infos, override_findings, disabled) =
        evaluate_overrides(&resolved.overrides, &resolved.rules, constraints);

    let rules = resolved.rules;
    let rule_refs: Vec<&FitnessRule> = rules
        .iter()
        .filter(|r| !disabled.contains(&r.name) && !r.unverifiable)
        .collect();

    let mut issues = resolved.findings;
    issues.extend(override_findings);
    let mut durations = Vec::new();
    for rule in &rule_refs {
        let started = Instant::now();
        run_rule(rule, repo, &rule_refs, &mut issues)?;
        // u128 → u64 с насыщением: переполнение недостижимо практически
        // (584 млн лет), насыщение — страховка вместо паники.
        let ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
        durations.push(RuleDuration {
            rule: rule.name.clone(),
            ms,
        });
    }
    issues.sort_by(|a, b| {
        a.file
            .cmp(&b.file)
            .then(a.line.cmp(&b.line))
            .then(a.rule.cmp(&b.rule))
    });

    // Счётчики правил по источникам (наследование видно в выводе).
    let mut by_source: BTreeMap<String, usize> = BTreeMap::new();
    for rule in &rules {
        if let Some(source) = &rule.source {
            *by_source.entry(source.clone()).or_default() += 1;
        }
    }
    let inherited: Vec<SourceCount> = by_source
        .into_iter()
        .map(|(source, rules)| SourceCount { source, rules })
        .collect();

    let errors = issues.iter().filter(|i| i.severity == "error").count();
    let warns = issues.len() - errors;
    let summary = format!(
        "Правил: {}, нарушений: {} (error: {errors}, warn: {warns})",
        rule_refs.len(),
        issues.len()
    );
    Ok(FitnessReport {
        repo: repo.to_path_buf(),
        passed: errors == 0,
        issues,
        summary,
        durations,
        inherited,
        overrides: override_infos,
    })
}

/// Число коммитов за последние 90 дней, трогавших файл ограничений —
/// git-прокси стоимости сопровождения реестра правил. `None` — не
/// git-репозиторий или git недоступен: отчёт показывает «недоступно»,
/// это не ошибка.
fn constraint_churn_90d(repo: &Path, constraints: &Path) -> Option<usize> {
    let abs = constraints.canonicalize().ok()?;
    let out = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["log", "--since=90 days ago", "--oneline", "--"])
        .arg(&abs)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    Some(
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter(|l| !l.trim().is_empty())
            .count(),
    )
}

/// Отчёт по реестру правил `CONSTRAINTS.yaml` (markdown в stdout).
///
/// Секции: сводка (всего / по типам / по severity), таблица карточек
/// (name, type, severity, owner, expiry, число `exclude_glob`, `effort_hours`),
/// находки (правила без owner/expiry; просроченные expiry — та же логика
/// даты, что в [`run_rule`]; правила с `exclude_glob` — прокси отступлений/FP),
/// git-прокси стоимости сопровождения (коммиты за 90 дней по файлу
/// ограничений; «недоступно» вне git-репозитория) и итоговая строка
/// «правил N, суммарный `effort_hours` X (покрыто Y правил)».
///
/// # Errors
/// Те же, что у [`check`]: репозиторий/файл недоступны, YAML невалиден,
/// правил нет.
pub fn rules_report(repo: &Path, constraints: &Path) -> Result<String> {
    if !repo.is_dir() {
        return Err(HarnessError::Control(format!(
            "репозиторий недоступен: {}",
            repo.display()
        )));
    }
    let yaml =
        std::fs::read_to_string(constraints).map_err(|e| HarnessError::io(constraints, e))?;
    let parsed: ConstraintsFile = serde_yaml_ng::from_str(&yaml)?;
    let rules: Vec<&FitnessRule> = parsed.all_rules().collect();
    if rules.is_empty() {
        return Err(HarnessError::Control(format!(
            "{}: файл не содержит правил — ожидается непустой корень `rules:` или `constraints:`",
            constraints.display()
        )));
    }

    let mut out = String::new();
    let _ = writeln!(out, "# Отчёт по правилам: {}", constraints.display());
    let _ = writeln!(out, "\nВсего правил: {}", rules.len());

    let mut by_kind: BTreeMap<&str, usize> = BTreeMap::new();
    let mut by_severity: BTreeMap<&str, usize> = BTreeMap::new();
    for r in &rules {
        *by_kind.entry(r.kind.as_str()).or_default() += 1;
        *by_severity.entry(r.severity.as_str()).or_default() += 1;
    }
    let join_counts = |m: &BTreeMap<&str, usize>| {
        m.iter()
            .map(|(k, n)| format!("{k} {n}"))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let _ = writeln!(out, "По типам: {}", join_counts(&by_kind));
    let _ = writeln!(out, "По severity: {}", join_counts(&by_severity));

    let _ = writeln!(
        out,
        "\n| Правило | Тип | Severity | Owner | Expiry | exclude_glob | effort_hours |"
    );
    let _ = writeln!(out, "|---|---|---|---|---|---|---|");
    let dash = "—";
    for r in &rules {
        let _ = writeln!(
            out,
            "| {} | {} | {} | {} | {} | {} | {} |",
            r.name,
            r.kind.as_str(),
            r.severity,
            r.owner.as_deref().unwrap_or(dash),
            r.expiry.as_deref().unwrap_or(dash),
            r.exclude_glob.len(),
            r.effort_hours
                .map_or_else(|| dash.to_string(), |h| h.to_string()),
        );
    }

    let _ = writeln!(out, "\n## Находки\n");

    let _ = writeln!(out, "### Правила без owner/expiry");
    let mut any = false;
    for r in &rules {
        let missing: Vec<&str> = [
            (r.owner.is_none()).then_some("owner"),
            (r.expiry.is_none()).then_some("expiry"),
        ]
        .into_iter()
        .flatten()
        .collect();
        if !missing.is_empty() {
            let _ = writeln!(out, "- {} (нет {})", r.name, missing.join(", "));
            any = true;
        }
    }
    if !any {
        let _ = writeln!(out, "- нет");
    }

    let _ = writeln!(out, "\n### Просроченные правила (expiry в прошлом)");
    let today = chrono::Local::now().date_naive();
    let mut any = false;
    for r in &rules {
        let Some(expiry) = &r.expiry else {
            continue;
        };
        // Невалидная дата — не ошибка: поле метаданное (как в run_rule).
        if let Ok(date) = chrono::NaiveDate::parse_from_str(expiry.trim(), "%Y-%m-%d") {
            if date < today {
                let owner = r
                    .owner
                    .as_deref()
                    .map(|o| format!(" (владелец: {o})"))
                    .unwrap_or_default();
                let _ = writeln!(out, "- {} — expiry {expiry}{owner}", r.name);
                any = true;
            }
        }
    }
    if !any {
        let _ = writeln!(out, "- нет");
    }

    let _ = writeln!(
        out,
        "\n### Правила с exclude_glob (прокси отступлений/ложных срабатываний)"
    );
    let mut any = false;
    for r in &rules {
        if !r.exclude_glob.is_empty() {
            let _ = writeln!(out, "- {}: {}", r.name, r.exclude_glob.join(", "));
            any = true;
        }
    }
    if !any {
        let _ = writeln!(out, "- нет");
    }

    let _ = writeln!(out, "\n### Стоимость сопровождения (git-прокси)");
    match constraint_churn_90d(repo, constraints) {
        Some(n) => {
            let _ = writeln!(
                out,
                "Коммитов за последние 90 дней, трогавших файл ограничений: {n}"
            );
        }
        None => {
            let _ = writeln!(
                out,
                "Коммитов за последние 90 дней, трогавших файл ограничений: недоступно \
                 (не git-репозиторий или git не найден)"
            );
        }
    }

    let covered = rules.iter().filter(|r| r.effort_hours.is_some()).count();
    // Sum для f64 на пустом множестве даёт -0.0 (особенность std) — +0.0
    // нормализует к «0» в выводе.
    let total: f64 = rules.iter().filter_map(|r| r.effort_hours).sum::<f64>() + 0.0;
    let _ = writeln!(
        out,
        "\nправил {}, суммарный effort_hours {total} (покрыто {covered} правил)",
        rules.len()
    );
    Ok(out)
}

/// Просрочено ли правило (expiry в прошлом; невалидная дата — не просрочка,
/// поле метаданное). Общая логика даты с `run_rule`/`rules_report`.
fn rule_expiry_in_past(expiry: &str) -> bool {
    chrono::NaiveDate::parse_from_str(expiry.trim(), "%Y-%m-%d")
        .is_ok_and(|date| date < chrono::Local::now().date_naive())
}

/// Запись overrides в отчёте `control report` (SDK-контракт, аддитивные поля).
#[derive(Debug, Clone, Serialize)]
pub struct OverrideReportEntry {
    /// Правило.
    pub rule: String,
    /// ADR.
    pub adr: String,
    /// Срок.
    pub until: String,
    /// active|expired|invalid.
    pub status: String,
    /// Пояснение.
    pub note: String,
}

/// Отчёт вверх по корпоративному контуру (`arch-ml control report`,
/// `docs/corp-spine.md`): агрегат по текущему репо — покрытие корп-правил,
/// overrides со статусами, просроченные правила.
#[derive(Debug, Clone, Serialize)]
pub struct ControlReport {
    /// Репозиторий.
    pub repo: PathBuf,
    /// Файл ограничений.
    pub constraints: PathBuf,
    /// Уровень отчёта: `corp` (только унаследованные правила) | `all`.
    pub level: String,
    /// Правил в скоупе уровня.
    pub rules_total: usize,
    /// Собственных правил в скоупе.
    pub own: usize,
    /// Унаследованные правила по источникам (`<ref>@<version>` → число).
    pub inherited: BTreeMap<String, usize>,
    /// Правил без находок.
    pub pass: usize,
    /// Правил с error-находками.
    pub fail: usize,
    /// Правил только с warn-находками.
    pub warn: usize,
    /// Итог гейта (`control check`).
    pub passed: bool,
    /// Overrides со статусами.
    pub overrides: Vec<OverrideReportEntry>,
    /// Просроченные правила (expiry в прошлом).
    pub expired_rules: Vec<String>,
    /// Правила ручного контроля (`unverifiable: true` — не исполняются,
    /// осознанный долг с owner).
    pub unverifiable_rules: Vec<String>,
    /// Расхождения пинов версий `extends` (текст находок).
    pub version_mismatches: Vec<String>,
}

/// Отчёт `control report --level corp|all` по текущему репо: прогоняет
/// [`check`] и группирует находки по правилам в скоупе уровня (`corp` —
/// только унаследованные через `extends` правила, `all` — все).
///
/// # Errors
/// Неизвестный уровень; те же, что у [`check`].
pub fn control_report(repo: &Path, constraints: &Path, level: &str) -> Result<ControlReport> {
    if level != "corp" && level != "all" {
        return Err(HarnessError::Control(format!(
            "неизвестный уровень отчёта '{level}' (допустимы: corp, all)"
        )));
    }
    let resolved = load_constraints_resolved(constraints)?;
    let fitness = check(repo, constraints)?;

    let in_scope = |rule: &&FitnessRule| level == "all" || rule.source.is_some();
    let scoped: Vec<&FitnessRule> = resolved.rules.iter().filter(in_scope).collect();

    // Худший исход правила по его находкам (error > warn > pass); находки
    // механики (extends/override) правилам не принадлежат и не группируются.
    // Правила ручного контроля (unverifiable) не исполняются — в исходы не
    // входят, перечисляются отдельно.
    let mut pass = 0usize;
    let mut fail = 0usize;
    let mut warn = 0usize;
    let mut inherited: BTreeMap<String, usize> = BTreeMap::new();
    let mut own = 0usize;
    let mut unverifiable_rules = Vec::new();
    for rule in &scoped {
        match &rule.source {
            Some(source) => *inherited.entry(source.clone()).or_default() += 1,
            None => own += 1,
        }
        if rule.unverifiable {
            unverifiable_rules.push(rule.name.clone());
            continue;
        }
        let has_error = fitness
            .issues
            .iter()
            .any(|i| i.rule == rule.name && i.severity == "error");
        let has_warn = fitness
            .issues
            .iter()
            .any(|i| i.rule == rule.name && i.severity == "warn");
        if has_error {
            fail += 1;
        } else if has_warn {
            warn += 1;
        } else {
            pass += 1;
        }
    }

    let expired_rules: Vec<String> = scoped
        .iter()
        .filter(|r| r.expiry.as_deref().is_some_and(rule_expiry_in_past))
        .map(|r| r.name.clone())
        .collect();
    let version_mismatches: Vec<String> = fitness
        .issues
        .iter()
        .filter(|i| i.rule == "extends")
        .map(|i| i.message.clone())
        .collect();
    let overrides: Vec<OverrideReportEntry> = fitness
        .overrides
        .iter()
        .map(|o| OverrideReportEntry {
            rule: o.rule.clone(),
            adr: o.adr.clone(),
            until: o.until.clone(),
            status: o.status.clone(),
            note: o.note.clone(),
        })
        .collect();

    Ok(ControlReport {
        repo: repo.to_path_buf(),
        constraints: constraints.to_path_buf(),
        level: level.to_string(),
        rules_total: scoped.len(),
        own,
        inherited,
        pass,
        fail,
        warn,
        passed: fitness.passed,
        overrides,
        expired_rules,
        unverifiable_rules,
        version_mismatches,
    })
}

/// Рендерит [`ControlReport`] в markdown (человекочитаемый режим
/// `arch-ml control report`).
#[must_use]
pub fn render_control_report(report: &ControlReport) -> String {
    let mut out = String::new();
    let _ = writeln!(
        out,
        "# Отчёт контроля (уровень {}): {}",
        report.level,
        report.repo.display()
    );
    let _ = writeln!(out, "Файл ограничений: {}", report.constraints.display());
    let _ = writeln!(
        out,
        "\nПравил в скоупе: {} (собственных: {}, унаследованных: {})",
        report.rules_total,
        report.own,
        report.inherited.values().sum::<usize>()
    );
    for (source, count) in &report.inherited {
        let _ = writeln!(out, "- {source}: {count} правил");
    }
    let _ = writeln!(
        out,
        "\nИсходы правил: pass {}, fail {}, warn {}",
        report.pass, report.fail, report.warn
    );
    let _ = writeln!(
        out,
        "Итог гейта: {}",
        if report.passed { "PASS" } else { "FAIL" }
    );

    let _ = writeln!(out, "\n## Overrides");
    if report.overrides.is_empty() {
        let _ = writeln!(out, "- нет");
    }
    for o in &report.overrides {
        let _ = writeln!(
            out,
            "- {} (adr {}, until {}): {} — {}",
            o.rule, o.adr, o.until, o.status, o.note
        );
    }

    let _ = writeln!(out, "\n## Просроченные правила (expiry)");
    if report.expired_rules.is_empty() {
        let _ = writeln!(out, "- нет");
    }
    for name in &report.expired_rules {
        let _ = writeln!(out, "- {name}");
    }

    let _ = writeln!(out, "\n## Ручной контроль (unverifiable)");
    if report.unverifiable_rules.is_empty() {
        let _ = writeln!(out, "- нет");
    }
    for name in &report.unverifiable_rules {
        let _ = writeln!(out, "- {name}");
    }

    let _ = writeln!(out, "\n## Расхождения версий extends");
    if report.version_mismatches.is_empty() {
        let _ = writeln!(out, "- нет");
    }
    for m in &report.version_mismatches {
        let _ = writeln!(out, "- {m}");
    }
    out
}

/// Нормализует severity правила: канонические `error`/`warn` и `block`/`warn`
/// корп-спайна (`docs/corp-spine.md`: block останавливает мерж, warn — только
/// в отчёт) проходят как есть; шкала кейсов и handoff-пакетов маппится:
/// `critical`/`high` → error (блокирующие), `medium` → warn (advisory).
pub(crate) fn normalize_severity(raw: &str, rule_name: &str) -> Result<&'static str> {
    match raw {
        "error" | "block" | "critical" | "high" => Ok("error"),
        "warn" | "medium" => Ok("warn"),
        other => Err(HarnessError::Control(format!(
            "правило '{rule_name}': severity должно быть error|warn|block|critical|high|medium, получено '{other}'"
        ))),
    }
}

/// Выполняет одно fitness-правило, добавляя находки в `issues`.
///
/// `all_rules` — все правила того же файла: нужны типу `archunit`
/// (ADR-039), который исполняет java-правила всего `CONSTRAINTS.yaml`.
fn run_rule(
    rule: &FitnessRule,
    repo: &Path,
    all_rules: &[&FitnessRule],
    issues: &mut Vec<LintIssue>,
) -> Result<()> {
    let severity = normalize_severity(&rule.severity, &rule.name)?;
    // Просроченное правило (expiry в прошлом) — warn-находка независимо от
    // исхода проверки: «правило без срока жизни» (антипаттерн библиотеки №5)
    // становится механически видимым. Невалидная дата — не ошибка, поле
    // метаданное.
    if let Some(expiry) = &rule.expiry {
        if let Ok(date) = chrono::NaiveDate::parse_from_str(expiry.trim(), "%Y-%m-%d") {
            if date < chrono::Local::now().date_naive() {
                let owner = rule
                    .owner
                    .as_deref()
                    .map(|o| format!(" (владелец: {o})"))
                    .unwrap_or_default();
                issues.push(LintIssue {
                    file: PathBuf::from("CONSTRAINTS.yaml"),
                    line: 0,
                    rule: rule.name.clone(),
                    message: format!(
                        "expiry: правило просрочено {expiry}{owner} — пересмотреть, продлить с владельцем или удалить"
                    ),
                    severity: "warn".into(),
                });
            }
        }
    }
    let mut issue = |file: PathBuf, line: usize, message: String| {
        issues.push(LintIssue {
            file,
            line,
            rule: rule.name.clone(),
            message,
            severity: severity.to_string(),
        });
    };
    match rule.kind {
        RuleKind::MustContain => {
            let (re, glob, files) = prep_content_rule(rule, repo)?;
            let pattern = rule.pattern.as_deref().unwrap_or_default();
            let mut found = false;
            for (_, abs) in &files {
                let bytes = std::fs::read(abs).map_err(|e| HarnessError::io(abs, e))?;
                if re.is_match(&String::from_utf8_lossy(&bytes)) {
                    found = true;
                    break;
                }
            }
            if !found {
                issue(
                    PathBuf::from(&glob),
                    0,
                    format!(
                        "must_contain: паттерн '{pattern}' не найден ни в одном файле по glob '{glob}'"
                    ),
                );
            }
        }
        RuleKind::MustNotContain => {
            let (re, _, files) = prep_content_rule(rule, repo)?;
            let pattern = rule.pattern.as_deref().unwrap_or_default();
            for (rel, abs) in &files {
                let bytes = std::fs::read(abs).map_err(|e| HarnessError::io(abs, e))?;
                let content = String::from_utf8_lossy(&bytes);
                for (idx, line) in content.lines().enumerate() {
                    if re.is_match(line) {
                        let snippet: String = line.trim().chars().take(120).collect();
                        issue(
                            PathBuf::from(rel),
                            idx + 1,
                            format!("must_not_contain: запрещённый паттерн '{pattern}': {snippet}"),
                        );
                    }
                }
            }
        }
        RuleKind::EachFileMustContain => {
            let (re, glob, files) = prep_content_rule(rule, repo)?;
            let pattern = rule.pattern.as_deref().unwrap_or_default();
            if files.is_empty() {
                issue(
                    PathBuf::from(&glob),
                    0,
                    format!(
                        "each_file_must_contain: по glob '{glob}' не найдено ни одного файла — правилу нечего проверять"
                    ),
                );
            }
            for (rel, abs) in &files {
                let bytes = std::fs::read(abs).map_err(|e| HarnessError::io(abs, e))?;
                if !re.is_match(&String::from_utf8_lossy(&bytes)) {
                    issue(
                        PathBuf::from(rel),
                        0,
                        format!(
                            "each_file_must_contain: паттерн '{pattern}' не найден в файле {rel}"
                        ),
                    );
                }
            }
        }
        RuleKind::FileExists => {
            let rel = rule.path.as_deref().ok_or_else(|| {
                HarnessError::Control(format!(
                    "правило '{}': для file_exists нужен path",
                    rule.name
                ))
            })?;
            if !repo.join(rel).exists() {
                issue(
                    PathBuf::from(rel),
                    0,
                    format!("file_exists: файл не найден: {rel}"),
                );
            }
        }
        RuleKind::DirMustHaveFile => {
            let rel_path = rule.path.as_deref().ok_or_else(|| {
                HarnessError::Control(format!(
                    "правило '{}': для dir_must_have_file нужен path",
                    rule.name
                ))
            })?;
            let globs = rule_globs(rule);
            let mut dirs = Vec::new();
            for glob in &globs {
                dirs.extend(collect_dirs(repo, glob)?);
            }
            dirs.sort();
            dirs.dedup();
            apply_excludes(&mut dirs, &rule.exclude_glob);
            if dirs.is_empty() {
                issue(
                    PathBuf::from(globs.join(", ")),
                    0,
                    format!(
                        "dir_must_have_file: по glob '{}' не найдено ни одного каталога — правилу нечего проверять",
                        globs.join(", ")
                    ),
                );
            }
            for dir in &dirs {
                if !repo.join(dir).join(rel_path).exists() {
                    issue(
                        PathBuf::from(dir),
                        0,
                        format!(
                            "dir_must_have_file: в каталоге {dir} не найден обязательный файл {rel_path}"
                        ),
                    );
                }
            }
        }
        RuleKind::MaxAge => {
            let rel = rule.path.as_deref().ok_or_else(|| {
                HarnessError::Control(format!("правило '{}': для max_age нужен path", rule.name))
            })?;
            let max_age_days = rule.max_age_days.ok_or_else(|| {
                HarnessError::Control(format!(
                    "правило '{}': для max_age нужен max_age_days",
                    rule.name
                ))
            })?;
            if let Some(message) = check_max_age(repo, rel, max_age_days, SystemTime::now())? {
                issue(PathBuf::from(rel), 0, message);
            }
        }
        RuleKind::CommandSucceeds => {
            let cmd = rule.command.as_deref().ok_or_else(|| {
                HarnessError::Control(format!(
                    "правило '{}': для command_succeeds нужен command",
                    rule.name
                ))
            })?;
            let timeout_secs = rule.timeout_secs.unwrap_or(COMMAND_TIMEOUT_DEFAULT_SECS);
            match run_with_timeout(repo, cmd, Duration::from_secs(timeout_secs))? {
                outcome if outcome.status.is_some_and(|s| s.success()) => {}
                outcome if outcome.status.is_some() => {
                    let code = outcome
                        .status
                        .and_then(|s| s.code())
                        .map_or_else(|| "завершена сигналом".to_string(), |c| format!("код {c}"));
                    let tail = report_tail(&outcome.tail);
                    let detail = if tail.is_empty() {
                        String::new()
                    } else {
                        format!("; хвост вывода:\n{tail}")
                    };
                    issue(
                        repo.to_path_buf(),
                        0,
                        format!(
                            "command_succeeds: команда '{cmd}' завершилась неуспешно ({code}){detail}"
                        ),
                    );
                }
                _ => {
                    issue(
                        repo.to_path_buf(),
                        0,
                        format!(
                            "command_succeeds: команда '{cmd}' превысила таймаут {timeout_secs}s и была убита"
                        ),
                    );
                }
            }
        }
        RuleKind::DependencyDirection => {
            let mode = deps_mode(rule)?;
            let globs = rule_globs(rule);
            let mut files = Vec::new();
            for glob in &globs {
                files.extend(collect_files(repo, glob)?);
            }
            files.sort_by(|a, b| a.0.cmp(&b.0));
            files.dedup_by(|a, b| a.0 == b.0);
            if !rule.exclude_glob.is_empty() {
                files.retain(|(rel, _)| !rule.exclude_glob.iter().any(|ex| glob_matches(ex, rel)));
            }
            if files.is_empty() {
                issue(
                    PathBuf::from(globs.join(", ")),
                    0,
                    format!(
                        "dependency_direction: по glob '{}' не найдено ни одного файла — правилу нечего проверять",
                        globs.join(", ")
                    ),
                );
            }
            for (rel, abs) in &files {
                let bytes = std::fs::read(abs).map_err(|e| HarnessError::io(abs, e))?;
                let content = String::from_utf8_lossy(&bytes);
                for (module, line) in extract_imports(rel, &content) {
                    // Относительные импорты TS/JS (`./…`) не выражаются в
                    // координатах модулей — их разрешает `context_boundary`.
                    if module.starts_with('.') {
                        continue;
                    }
                    match &mode {
                        DepsMode::Forbid(forbid) => {
                            if let Some(entry) =
                                forbid.iter().find(|e| module_prefix_match(&module, e))
                            {
                                issue(
                                    PathBuf::from(rel),
                                    line,
                                    format!(
                                        "dependency_direction: запрещённая зависимость '{module}' (forbid: '{entry}')"
                                    ),
                                );
                            }
                        }
                        DepsMode::Allow(allow) => {
                            if !allow.iter().any(|e| module_prefix_match(&module, e)) {
                                issue(
                                    PathBuf::from(rel),
                                    line,
                                    format!(
                                        "dependency_direction: зависимость '{module}' вне allow-списка ({})",
                                        allow.join(", ")
                                    ),
                                );
                            }
                        }
                    }
                }
            }
        }
        RuleKind::ContextBoundary => {
            let model_dir = rule.model_dir.as_deref().unwrap_or("model");
            if !repo.join(model_dir).is_dir() {
                issue(
                    PathBuf::from(model_dir),
                    0,
                    format!("context_boundary: каталог модели не найден: {model_dir}"),
                );
                return Ok(());
            }
            let model = crate::model::load_model(&repo.join(model_dir))?;
            check_context_boundary(rule, repo, &model, model_dir, &mut issue)?;
        }
        RuleKind::ArchUnit => {
            // Общий код с `arch-ml archunit check` (ADR-039): спек строится
            // из java-правил этого же CONSTRAINTS.yaml. Инфраструктурный
            // сбой (нет java/jar'ов/классов, таймаут) — error-находка
            // (fail-closed), а не молчаливый PASS.
            match crate::archunit::run_control_rule(rule, all_rules, repo) {
                Ok(found) => issues.extend(found),
                Err(e) => issue(
                    repo.to_path_buf(),
                    0,
                    format!("archunit: гейт не исполнен (fail-closed): {e}"),
                ),
            }
        }
        RuleKind::DenyDependency => {
            if rule.deny.is_empty() {
                return Err(HarnessError::Control(format!(
                    "правило '{}': для deny_dependency нужен непустой список deny",
                    rule.name
                )));
            }
            let globs = if rule.manifests.is_empty() {
                DEFAULT_MANIFEST_GLOBS
                    .iter()
                    .map(ToString::to_string)
                    .collect::<Vec<_>>()
            } else {
                rule.manifests.clone()
            };
            let mut files = Vec::new();
            for glob in &globs {
                files.extend(collect_files(repo, glob)?);
            }
            files.sort_by(|a, b| a.0.cmp(&b.0));
            files.dedup_by(|a, b| a.0 == b.0);
            if !rule.exclude_glob.is_empty() {
                files.retain(|(rel, _)| !rule.exclude_glob.iter().any(|ex| glob_matches(ex, rel)));
            }
            let reference = rule
                .reference
                .as_deref()
                .map(|r| format!(" — основание: {r}"))
                .unwrap_or_default();
            for (rel, abs) in &files {
                let bytes = std::fs::read(abs).map_err(|e| HarnessError::io(abs, e))?;
                let content = String::from_utf8_lossy(&bytes);
                for (pkg, line) in manifest_deps(rel, &content)? {
                    if rule.deny.iter().any(|d| d == &pkg) {
                        issue(
                            PathBuf::from(rel),
                            line,
                            format!("deny_dependency: пакет '{pkg}' из deny-списка{reference}"),
                        );
                    }
                }
            }
        }
        RuleKind::SkillContract => {
            // Набор glob'ов обязателен (спека гейта «md vs skill»); пустой
            // список вырождается в дефолт `**/SKILL.md`, чтобы правило без
            // настройки не сканировало весь репозиторий как скиллы.
            let globs = if rule.glob.is_empty() {
                vec!["**/SKILL.md".to_string()]
            } else {
                rule.glob.clone()
            };
            let mut files = Vec::new();
            for glob in &globs {
                files.extend(collect_files(repo, glob)?);
            }
            files.sort_by(|a, b| a.0.cmp(&b.0));
            files.dedup_by(|a, b| a.0 == b.0);
            if !rule.exclude_glob.is_empty() {
                files.retain(|(rel, _)| !rule.exclude_glob.iter().any(|ex| glob_matches(ex, rel)));
            }
            if files.is_empty() {
                issue(
                    PathBuf::from(globs.join(", ")),
                    0,
                    format!(
                        "skill_contract: по glob-ам '{}' не найдено ни одного SKILL.md — правилу нечего проверять",
                        globs.join(", ")
                    ),
                );
            }
            for (rel, abs) in &files {
                let bytes = std::fs::read(abs).map_err(|e| HarnessError::io(abs, e))?;
                let text = String::from_utf8_lossy(&bytes);
                for msg in skill_contract_violations(&text) {
                    issue(PathBuf::from(rel), 0, format!("skill_contract: {msg}"));
                }
            }
        }
        RuleKind::PlaybookGraduation => {
            // Механический гейт градации (предикат `playbook_graduation_violations`)
            // применяется в `graduate_playbook` до записи в `skills/`; правило
            // репозитория проверяет целостность АРХИВА: градированный playbook
            // обязан ссылаться на реальный скилл и не обойти порог повторений.
            // Неградированный playbook (нет `graduated_to`) — кандидат, не ошибка.
            let globs = if rule.glob.is_empty() {
                vec!["**/distillate/playbooks/*.md".to_string()]
            } else {
                rule.glob.clone()
            };
            let mut files = Vec::new();
            for glob in &globs {
                files.extend(collect_files(repo, glob)?);
            }
            files.sort_by(|a, b| a.0.cmp(&b.0));
            files.dedup_by(|a, b| a.0 == b.0);
            if !rule.exclude_glob.is_empty() {
                files.retain(|(rel, _)| !rule.exclude_glob.iter().any(|ex| glob_matches(ex, rel)));
            }
            if files.is_empty() {
                issue(
                    PathBuf::from(globs.join(", ")),
                    0,
                    format!(
                        "playbook_graduation: по glob-ам '{}' не найдено ни одного playbook — правилу нечего проверять",
                        globs.join(", ")
                    ),
                );
            }
            for (rel, abs) in &files {
                let bytes = std::fs::read(abs).map_err(|e| HarnessError::io(abs, e))?;
                let text = String::from_utf8_lossy(&bytes);
                let Some((frontmatter, _)) = split_frontmatter(&text) else {
                    continue;
                };
                let Some(to) = frontmatter_field(frontmatter, "graduated_to") else {
                    continue;
                };
                let to = to.trim();
                if to.is_empty() {
                    continue;
                }
                // #1: graduated playbook указывает на существующий скилл.
                if !repo.join(to).is_file() {
                    issue(
                        PathBuf::from(rel),
                        0,
                        format!(
                            "playbook_graduation: graduated playbook указывает на несуществующий скилл '{to}'"
                        ),
                    );
                }
                // #2: архивировать можно лишь после трёх применений.
                let uses = frontmatter_field(frontmatter, "uses")
                    .and_then(|v| v.trim().parse::<u32>().ok())
                    .unwrap_or(0);
                if uses < PLAYBOOK_USES_MIN {
                    issue(
                        PathBuf::from(rel),
                        0,
                        format!(
                            "playbook_graduation: playbook градирован без трёх повторений (uses = {uses} < {PLAYBOOK_USES_MIN})"
                        ),
                    );
                }
            }
        }
        RuleKind::ManagedHashCurrent => {
            let globs = if rule.glob.is_empty() {
                vec!["**/*.md".to_string()]
            } else {
                rule.glob.clone()
            };
            let mut files = Vec::new();
            for glob in &globs {
                files.extend(collect_files(repo, glob)?);
            }
            files.sort_by(|a, b| a.0.cmp(&b.0));
            files.dedup_by(|a, b| a.0 == b.0);
            if files.is_empty() {
                issue(
                    PathBuf::from(globs.join(", ")),
                    0,
                    format!(
                        "managed_hash_current: по glob-ам '{}' не найдено ни одного файла",
                        globs.join(", ")
                    ),
                );
            }
            for (rel, abs) in &files {
                for m in crate::managed::lint_file(abs)? {
                    issue(
                        PathBuf::from(rel),
                        m.line,
                        format!("managed_hash_current [{}]: {}", m.rule, m.message),
                    );
                }
            }
        }
        RuleKind::AddressTrace => {
            let globs = if rule.glob.is_empty() {
                vec![
                    "**/distillate/**/*.md".to_string(),
                    "**/skills/*/SKILL.md".to_string(),
                ]
            } else {
                rule.glob.clone()
            };
            let mut files = Vec::new();
            for glob in &globs {
                files.extend(collect_files(repo, glob)?);
            }
            files.sort_by(|a, b| a.0.cmp(&b.0));
            files.dedup_by(|a, b| a.0 == b.0);
            if !rule.exclude_glob.is_empty() {
                files.retain(|(rel, _)| !rule.exclude_glob.iter().any(|ex| glob_matches(ex, rel)));
            }
            let index = build_address_index(repo)?;
            for (rel, abs) in &files {
                let text = std::fs::read_to_string(abs).map_err(|e| HarnessError::io(abs, e))?;
                for msg in
                    address_trace_violations(&text, &index, &rule.require, rule.pattern.as_deref())
                {
                    issue(PathBuf::from(rel), 0, format!("address_trace: {msg}"));
                }
            }
        }
    }
    Ok(())
}

/// Проверяет контракт Agent Skill в тексте `SKILL.md` (гейт «md vs skill»,
/// `docs/corp-spine.md`). Возвращает список нарушений на русском; пустой
/// список — документ структурно похож на скилл.
///
/// Проверки (все — структурные признаки, а не оценка содержания):
/// 1. frontmatter: файл начинается с `---`;
/// 2. `name:` есть и является kebab-case (`[a-z0-9-]+`);
/// 3. `description:` есть и не длиннее 1024 символов;
/// 4. в `description` есть триггер-маркер («используй этот навык», «когда
///    пользователь», …) — сигнал активации для агента;
/// 5. есть непустой раздел-триггер («Когда применять» и синонимы);
/// 6. есть раздел методики с ≥3 шагами (нумерованными или пунктами) —
///    структурный признак процедуры;
/// 7. тело (без frontmatter) не длиннее 500 строк.
#[must_use]
pub(crate) fn skill_contract_violations(text: &str) -> Vec<String> {
    use std::sync::OnceLock;
    static TRIGGER: OnceLock<Option<Regex>> = OnceLock::new();
    static WHEN_HEADING: OnceLock<Option<Regex>> = OnceLock::new();
    static HOW_HEADING: OnceLock<Option<Regex>> = OnceLock::new();
    static KEBAB: OnceLock<Option<Regex>> = OnceLock::new();
    let mut out = Vec::new();

    let trimmed = text.trim_start_matches('\u{feff}').trim_start();
    if !trimmed.starts_with("---") {
        out.push("нет frontmatter: файл не начинается со строки '---'".to_string());
        return out;
    }
    // Тело между первой и второй строкой `---`.
    let after = trimmed.strip_prefix("---").unwrap_or_default();
    let (frontmatter, body) = after.split_once("\n---").map_or(("", after), |(fm, b)| {
        let fm = fm.trim_start_matches(['\r', '\n']);
        let body = b.trim_start_matches(['\r', '\n']);
        (fm, body)
    });

    match frontmatter_field(frontmatter, "name") {
        None => out.push("frontmatter: нет поля name".to_string()),
        Some(name) => {
            let kebab = KEBAB.get_or_init(|| Regex::new(r"^[a-z0-9]+(?:-[a-z0-9]+)*$").ok());
            if !kebab.as_ref().is_some_and(|re| re.is_match(name.trim())) {
                out.push(format!(
                    "frontmatter: name '{}' не в kebab-case (ожидается [a-z0-9-]+)",
                    name.trim()
                ));
            }
        }
    }
    match frontmatter_field(frontmatter, "description") {
        None => out.push("frontmatter: нет поля description".to_string()),
        Some(desc) => {
            let desc = desc.trim();
            if desc.chars().count() > 1024 {
                out.push(format!(
                    "frontmatter: description длиннее 1024 символов ({} символов)",
                    desc.chars().count()
                ));
            }
            let trigger = TRIGGER.get_or_init(|| {
                Regex::new(
                    r"(?i)(используй\s+этот\s+навык|используй\s+навык|когда\s+пользователь|когда\s+нужно|когда:|триггер|применяй|use\s+this\s+skill|when\s+the\s+user)",
                )
                .ok()
            });
            if !trigger.as_ref().is_some_and(|re| re.is_match(desc)) {
                out.push(
                    "frontmatter: description без триггер-маркера (когда применять навык) — \
                     агенту не за что зацепиться при активации"
                        .to_string(),
                );
            }
        }
    }

    let when = WHEN_HEADING.get_or_init(|| {
        Regex::new(r"(?im)^#{1,3}\s*(когда\s+применять|когда\s+использовать|триггер)").ok()
    });
    match when.as_ref().and_then(|re| re.find(body)) {
        None => out.push(
            "нет раздела-триггера: нужен заголовок «## Когда применять» (или «Когда использовать», «Триггер»)"
                .to_string(),
        ),
        Some(m) => {
            let rest = &body[m.end()..];
            if section_body(rest).is_empty() {
                out.push("раздел-триггер пуст: нет ни одной строки условий применения".to_string());
            }
        }
    }

    let how = HOW_HEADING.get_or_init(|| {
        Regex::new(r"(?im)^#{1,3}\s*(методика|метод|процедура|порядок|шаги|how\s+to)").ok()
    });
    match how.as_ref().and_then(|re| re.find(body)) {
        None => out.push(
            "нет структурного признака процедуры: нужен раздел методики («## Методика», «## Шаги», …) с шагами"
                .to_string(),
        ),
        Some(m) => {
            let section = section_body(&body[m.end()..]);
            let steps = section
                .lines()
                .filter(|l| {
                    let t = l.trim_start();
                    t.starts_with("- ") || t.starts_with("* ")
                })
                .count();
            let numbered = section
                .lines()
                .filter(|l| {
                    let t = l.trim_start();
                    let rest = t.trim_start_matches(|c: char| c.is_ascii_digit());
                    let digits = t.len() - rest.len();
                    digits > 0 && matches!(rest.chars().next(), Some('.' | ')'))
                })
                .count();
            if numbered < 3 && steps < 3 {
                out.push(format!(
                    "раздел методики без процедуры: менее 3 шагов (нумерованных {numbered}, пунктов {steps}) — \
                     структурный признак процедуры отсутствует"
                ));
            }
        }
    }

    let body_lines = body.lines().count();
    if body_lines > 500 {
        out.push(format!(
            "тело скилла длиннее 500 строк ({body_lines}) — вынести детали в справочные документы"
        ));
    }
    out
}

/// Тело раздела до следующего заголовка `#` (обрезанное по краям), с
/// исключением вложенных подзаголовков глубже 3-го уровня.
fn section_body(rest: &str) -> String {
    let mut lines = Vec::new();
    for line in rest.lines() {
        let t = line.trim_start();
        if t.starts_with('#') {
            let hashes = t.chars().take_while(|c| *c == '#').count();
            if hashes <= 3 {
                break;
            }
        }
        if t.starts_with("---") {
            break;
        }
        lines.push(line);
    }
    lines.join("\n").trim().to_string()
}

/// Значение поля frontmatter: `key: value`, блочный скаляр (`key: |` / `>-`)
/// либо пустая строка. Возвращает `None`, если поля нет.
pub(crate) fn frontmatter_field(frontmatter: &str, key: &str) -> Option<String> {
    let mut lines = frontmatter.lines().peekable();
    while let Some(line) = lines.next() {
        let Some(rest) = line.strip_prefix(key) else {
            continue;
        };
        let Some(value) = rest.strip_prefix(':') else {
            continue;
        };
        // Многострочный скаляр: `|`, `>`, `|-`, `>-`, `|2` и т.п.
        let value = value.trim();
        let block = matches!(value.chars().next(), Some('|' | '>'));
        if !block {
            return Some(value.to_string());
        }
        let mut buf = Vec::new();
        for next in lines.by_ref() {
            if next.trim().is_empty() {
                buf.push(String::new());
                continue;
            }
            if next.starts_with(char::is_whitespace) {
                buf.push(next.trim().to_string());
            } else {
                break;
            }
        }
        return Some(buf.join(" ").trim().to_string());
    }
    None
}

/// Порог «правила трёх повторений» (в данных — счётчик `uses` playbook).
/// Фиксирован методологией: процедура становится скиллом, когда применена не
/// менее трёх раз и перестала меняться.
pub(crate) const PLAYBOOK_USES_MIN: u32 = 3;

/// Разделяет текст на frontmatter и тело. `None`, если файл не начинается со
/// строки `---`. Хвостовые `\r`/`\n` по краям обеих частей срезаются.
fn split_frontmatter(text: &str) -> Option<(&str, &str)> {
    let trimmed = text.trim_start_matches('\u{feff}').trim_start();
    let after = trimmed.strip_prefix("---")?;
    Some(after.split_once("\n---").map_or(("", after), |(fm, b)| {
        (
            fm.trim_start_matches(['\r', '\n']),
            b.trim_start_matches(['\r', '\n']),
        )
    }))
}

/// Тело раздела методики: среди заголовков-синонимов (методика/метод/
/// процедура/порядок/шаги/how to) выбирается тот, чей раздел содержит больше
/// шагов (нумерованных + пунктов); при равенстве — первый. Так заголовок
/// документа («# Порядок эскалации») не перебивает сам раздел процедуры.
/// Хвостовые пробелы строк нормализуются — косметика не должна менять хэш
/// стабильности. `None`, если заголовка-синонима нет.
#[must_use]
pub(crate) fn procedure_section(body: &str) -> Option<String> {
    use std::sync::OnceLock;
    static HOW_HEADING: OnceLock<Option<Regex>> = OnceLock::new();
    let re = HOW_HEADING.get_or_init(|| {
        Regex::new(r"(?im)^#{1,3}\s*(методика|метод|процедура|порядок|шаги|how\s+to)").ok()
    });
    let re = re.as_ref()?;
    let mut best: Option<(usize, String)> = None;
    for m in re.find_iter(body) {
        let section = normalize_section(&section_body(&body[m.end()..]));
        let (numbered, bullets) = count_procedure_steps(&section);
        let score = numbered + bullets;
        let better = match &best {
            None => true,
            Some((top, _)) => score > *top,
        };
        if better {
            best = Some((score, section));
        }
    }
    best.map(|(_, section)| section)
}

/// Нормализация тела раздела перед сверкой/хэшированием: срез хвостовых
/// пробелов строк и краёв.
fn normalize_section(section: &str) -> String {
    section
        .lines()
        .map(str::trim_end)
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

/// Считает шаги процедуры в теле раздела: `(нумерованные, пункты)`.
fn count_procedure_steps(section: &str) -> (usize, usize) {
    let bullets = section
        .lines()
        .filter(|l| {
            let t = l.trim_start();
            t.starts_with("- ") || t.starts_with("* ")
        })
        .count();
    let numbered = section
        .lines()
        .filter(|l| {
            let t = l.trim_start();
            let rest = t.trim_start_matches(|c: char| c.is_ascii_digit());
            let digits = t.len() - rest.len();
            digits > 0 && matches!(rest.chars().next(), Some('.' | ')'))
        })
        .count();
    (numbered, bullets)
}

/// Нарушения допуска playbook к градации в скилл. Пустой список — playbook
/// готов по механическим признакам (семантику держат судейская рубрика и
/// человек).
///
/// Проверки — структурные прокси «правила трёх повторений»:
/// 1. frontmatter есть, `type: playbook`;
/// 2. `uses` есть, целое, ≥ [`PLAYBOOK_USES_MIN`];
/// 3. `skill_candidate` не выставлен в `false`;
/// 4. `steps_sha256` присутствует (журнал стабильности вёлся) — иначе
///    стабильность механически недоказуема;
/// 5. раздел методики есть и содержит ≥ 3 шага;
/// 6. `graduated_to` ещё не проставлен (повторная градация — не молчаливая).
///
/// Хэш-равенство `steps_sha256` проверяет вызывающий код (ему доступно тело).
#[must_use]
pub(crate) fn playbook_graduation_violations(text: &str) -> Vec<String> {
    let mut out = Vec::new();

    let Some((frontmatter, body)) = split_frontmatter(text) else {
        out.push("нет frontmatter: файл не начинается со строки '---'".to_string());
        return out;
    };

    match frontmatter_field(frontmatter, "type").map(|v| v.trim().to_string()) {
        Some(t) if t == "playbook" => {}
        Some(t) => out.push(format!("frontmatter: type '{t}' вместо 'playbook'")),
        None => out.push("frontmatter: нет поля type (ожидается type: playbook)".to_string()),
    }

    match frontmatter_field(frontmatter, "uses").and_then(|v| v.trim().parse::<u32>().ok()) {
        Some(n) if n >= PLAYBOOK_USES_MIN => {}
        Some(n) => out.push(format!(
            "uses = {n} < {PLAYBOOK_USES_MIN}: процедура не повторена трижды (правило трёх повторений)"
        )),
        None => out.push(format!(
            "frontmatter: нет целого поля uses (порог градации — {PLAYBOOK_USES_MIN})"
        )),
    }

    if frontmatter_field(frontmatter, "skill_candidate")
        .is_some_and(|v| v.trim().eq_ignore_ascii_case("false"))
    {
        out.push("skill_candidate: false — playbook явно не кандидат в скиллы".to_string());
    }

    let has_hash =
        frontmatter_field(frontmatter, "steps_sha256").is_some_and(|v| !v.trim().is_empty());
    if !has_hash {
        out.push(
            "frontmatter: нет поля steps_sha256 — журнала стабильности шагов не было".to_string(),
        );
    }

    if frontmatter_field(frontmatter, "graduated_to").is_some_and(|v| !v.trim().is_empty()) {
        out.push(
            "playbook уже градирован (graduated_to проставлен) — повторная градация не молчалива"
                .to_string(),
        );
    }

    match procedure_section(body) {
        None => out.push(
            "нет структурного признака процедуры: нужен раздел методики («## Методика», «## Шаги», …) с шагами"
                .to_string(),
        ),
        Some(section) => {
            let (numbered, bullets) = count_procedure_steps(&section);
            if numbered < 3 && bullets < 3 {
                out.push(format!(
                    "раздел методики без процедуры: менее 3 шагов (нумерованных {numbered}, пунктов {bullets})"
                ));
            }
        }
    }

    out
}

/// Проверяет свежесть файла для правила `max_age`:
/// PASS ⇔ файл существует И его mtime не старше `now − max_age_days`.
///
/// `now` инъецируется параметром ради тестируемости: std не умеет выставлять
/// mtime, `unsafe` запрещён, новых зависимостей не добавляем — юнит-тесты
/// сдвигают `now`, а не время файла. Публичный вызов передаёт
/// [`SystemTime::now`].
///
/// Возвращает `Ok(None)` при прохождении; `Ok(Some(message))` при нарушении:
/// файл отсутствует (текст как у `file_exists`) либо устарел (возраст в днях
/// против лимита).
fn check_max_age(
    repo: &Path,
    rel: &str,
    max_age_days: u64,
    now: SystemTime,
) -> Result<Option<String>> {
    /// Секунд в сутках (перевод возраста файла в дни).
    const DAY_SECS: u64 = 86_400;
    let abs = repo.join(rel);
    if !abs.exists() {
        return Ok(Some(format!("file_exists: файл не найден: {rel}")));
    }
    let meta = std::fs::metadata(&abs).map_err(|e| HarnessError::io(&abs, e))?;
    let mtime = meta.modified().map_err(|e| HarnessError::io(&abs, e))?;
    // mtime в будущем (дрейф часов) — файл считаем свежим, возраст 0.
    let age = now.duration_since(mtime).unwrap_or(Duration::ZERO);
    // saturating_mul: абсурдно большой max_age_days не паникует, а просто
    // делает лимит практически бесконечным.
    let limit = Duration::from_secs(max_age_days.saturating_mul(DAY_SECS));
    if age > limit {
        let age_days = age.as_secs() / DAY_SECS;
        return Ok(Some(format!(
            "max_age: файл {rel} устарел: возраст {age_days} дн. при лимите {max_age_days} дн."
        )));
    }
    Ok(None)
}

/// Режим правила `dependency_direction`: чёрный или белый список модулей.
enum DepsMode<'a> {
    /// Запрещённые префиксы модульных путей.
    Forbid(&'a [String]),
    /// Разрешённые префиксы (пустой список — запрет всех внутренних
    /// зависимостей: листовые модули).
    Allow(&'a [String]),
}

/// Валидирует и возвращает режим `dependency_direction`: ровно одно из
/// `forbid`/`allow`.
///
/// # Errors
/// Оба списка заданы или оба отсутствуют.
fn deps_mode(rule: &FitnessRule) -> Result<DepsMode<'_>> {
    match (&rule.forbid, &rule.allow) {
        (Some(forbid), None) => Ok(DepsMode::Forbid(forbid)),
        (None, Some(allow)) => Ok(DepsMode::Allow(allow)),
        _ => Err(HarnessError::Control(format!(
            "правило '{}': для dependency_direction нужно ровно одно из forbid/allow",
            rule.name
        ))),
    }
}

/// Совпадение модуля с записью списка по префиксу пути с границей сегмента:
/// `agent/slash` совпадает с `agent`, `agentworld` — нет.
fn module_prefix_match(module: &str, entry: &str) -> bool {
    module == entry
        || module
            .strip_prefix(entry)
            .is_some_and(|rest| rest.starts_with('/'))
}

/// Извлечённый импорт: модуль в координатах `/` и номер строки (1-based).
type ImportEdge = (String, usize);

// Паттерны импортов по языкам: константные — компилируются лениво один раз.
// `.expect` допустим: ошибка компиляции константного паттерна — внутренний
// дефект кода, ловится тестами на старте.

/// Rust: `use crate::…` и инлайн-пути `crate::…::`.
static IMPORT_RS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\bcrate::([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)")
        .expect("константный regex импортов Rust")
});
/// Python: `import a.b`.
static IMPORT_PY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\s*import\s+([A-Za-z_][\w.]*)").expect("константный regex импортов Python")
});
/// Python: `from a.b import …`.
static IMPORT_PY_FROM: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b")
        .expect("константный regex from-импортов Python")
});
/// Java/Kotlin: `import a.b.C;` (включая `import static …`).
static IMPORT_JAVA: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;")
        .expect("константный regex импортов Java/Kotlin")
});
/// TS/JS: `from '…'`.
static IMPORT_JS_FROM: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"\bfrom\s+['"]([^'"]+)['"]"#).expect("константный regex from-импортов TS/JS")
});
/// TS/JS: `import '…'` (side-effect import).
static IMPORT_JS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"^\s*import\s+['"]([^'"]+)['"]"#).expect("константный regex импортов TS/JS")
});
/// TS/JS: `require('…')`.
static IMPORT_JS_REQUIRE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"\brequire\(\s*['"]([^'"]+)['"]\s*\)"#).expect("константный regex require TS/JS")
});

/// Извлекает импорты из исходного файла по его расширению (ADR-029).
///
/// Поддерживаемые формы:
/// - Rust (`.rs`): `use crate::…` и инлайн-пути `crate::…::` (`::` → `/`);
///   внешние крейты (`use std::…`) не извлекаются;
/// - Python (`.py`): `import a.b`, `from a.b import …` (`.` → `/`);
/// - Java/Kotlin (`.java`, `.kt`): `import a.b.C;` (`.` → `/`);
/// - TS/JS (`.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`): `from '…'`, `import '…'`,
///   `require('…')`; относительные пути (`./…`) сохраняются как есть —
///   их разрешает `context_boundary`, а `dependency_direction` пропускает.
///
/// Строки-комментарии (`//`, `///`, `//!`, `#`) игнорируются; блочные
/// комментарии и строковые литералы не разбираются — эвристика
/// документированно приблизительна (ложное срабатывание возможно на
/// `crate::…` внутри строки). Для файлов неподдерживаемых расширений
/// возвращается пустой список.
fn extract_imports(rel: &str, content: &str) -> Vec<ImportEdge> {
    let ext = Path::new(rel)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default();
    let comment_prefix = match ext {
        "rs" | "java" | "kt" | "ts" | "tsx" | "js" | "jsx" | "mjs" => "//",
        "py" => "#",
        _ => return Vec::new(),
    };
    let mut out = Vec::new();
    for (idx, line) in content.lines().enumerate() {
        if line.trim_start().starts_with(comment_prefix) {
            continue;
        }
        let lineno = idx + 1;
        match ext {
            "rs" => {
                out.extend(
                    IMPORT_RS
                        .captures_iter(line)
                        .map(|c| (c[1].replace("::", "/"), lineno)),
                );
            }
            "py" => {
                out.extend(
                    IMPORT_PY
                        .captures(line)
                        .into_iter()
                        .chain(IMPORT_PY_FROM.captures(line))
                        .map(|c| (c[1].replace('.', "/"), lineno)),
                );
            }
            "java" | "kt" => {
                if let Some(c) = IMPORT_JAVA.captures(line) {
                    out.push((c[1].replace('.', "/"), lineno));
                }
            }
            _ => {
                // ts/tsx/js/jsx/mjs: путь сохраняется сырым (включая `./…`).
                out.extend(
                    IMPORT_JS_FROM
                        .captures_iter(line)
                        .chain(IMPORT_JS.captures_iter(line))
                        .chain(IMPORT_JS_REQUIRE.captures_iter(line))
                        .map(|c| (c[1].to_string(), lineno)),
                );
            }
        }
    }
    out
}

/// Проверка `context_boundary` (ADR-030): импорты файлов не пересекают
/// границы контекстов (CMP с `code_roots`) без объявленного `depends_on`.
///
/// Контекст файла определяется по префиксу пути среди `code_roots`;
/// контекст импорта — сначала прямым префиксным совпадением в координатах
/// импортов (Python/Java: путь пакета == путь файла), затем разрешением
/// модуля в существующий файл (Rust `crate::…` против `src/`, TS-относительные
/// против каталога файла). Неразрешённый импорт — внешняя зависимость,
/// пропускается. Пересекающиеся `code_roots` разных CMP — ошибка
/// конфигурации правила.
fn check_context_boundary(
    rule: &FitnessRule,
    repo: &Path,
    model: &crate::model::Model,
    model_dir: &str,
    issue: &mut impl FnMut(PathBuf, usize, String),
) -> Result<()> {
    let contexts: Vec<(&crate::model::Entity, Vec<String>)> = model
        .entities
        .iter()
        .filter(|e| e.kind == crate::model::EntityKind::Cmp && !e.code_roots.is_empty())
        .map(|e| (e, e.code_roots.iter().map(|r| normalize_root(r)).collect()))
        .collect();
    if contexts.is_empty() {
        issue(
            PathBuf::from(model_dir),
            0,
            "context_boundary: в модели нет CMP с code_roots — правилу нечего проверять"
                .to_string(),
        );
        return Ok(());
    }
    // Пересечение корней двух CMP делает владение файлом неоднозначным.
    for (i, (a, roots_a)) in contexts.iter().enumerate() {
        for (b, roots_b) in &contexts[i + 1..] {
            for ra in roots_a {
                for rb in roots_b {
                    if module_prefix_match(ra, rb) || module_prefix_match(rb, ra) {
                        return Err(HarnessError::Control(format!(
                            "правило '{}': code_roots пересекаются: {} ('{ra}') и {} ('{rb}')",
                            rule.name, a.id, b.id
                        )));
                    }
                }
            }
        }
    }
    let bases = candidate_bases(repo);
    let globs = rule_globs(rule);
    let mut files = Vec::new();
    for glob in &globs {
        files.extend(collect_files(repo, glob)?);
    }
    files.sort_by(|a, b| a.0.cmp(&b.0));
    files.dedup_by(|a, b| a.0 == b.0);
    if !rule.exclude_glob.is_empty() {
        files.retain(|(rel, _)| !rule.exclude_glob.iter().any(|ex| glob_matches(ex, rel)));
    }
    for (rel, abs) in &files {
        let Some(source) = owner_by_path(&contexts, rel) else {
            continue;
        };
        let bytes = std::fs::read(abs).map_err(|e| HarnessError::io(abs, e))?;
        let content = String::from_utf8_lossy(&bytes);
        for (module, line) in extract_imports(rel, &content) {
            let Some(target) = resolve_import_owner(repo, &contexts, &bases, rel, &module) else {
                continue;
            };
            if target.id == source.id || source.depends_on.contains(&target.id) {
                continue;
            }
            issue(
                PathBuf::from(rel),
                line,
                format!(
                    "context_boundary: импорт '{module}' пересекает границу контекста: {} → {} ({}) без depends_on в модели",
                    source.id, target.id, target.title
                ),
            );
        }
    }
    Ok(())
}

/// Контекст (CMP), которому принадлежит путь `path` по префиксу `code_roots`.
fn owner_by_path<'m>(
    contexts: &[(&'m crate::model::Entity, Vec<String>)],
    path: &str,
) -> Option<&'m crate::model::Entity> {
    contexts
        .iter()
        .find(|(_, roots)| roots.iter().any(|r| module_prefix_match(path, r)))
        .map(|(e, _)| *e)
}

/// Разрешает импорт `module` (координаты `/`) во владеющий им контекст.
///
/// Порядок: TS-относительный путь — от каталога файла; прямое префиксное
/// совпадение с `code_roots`; разрешение в существующий файл репозитория
/// (базы `candidate_bases` + модульные суффиксы). `None` — внешняя или
/// неразрешённая зависимость.
fn resolve_import_owner<'m>(
    repo: &Path,
    contexts: &[(&'m crate::model::Entity, Vec<String>)],
    bases: &[String],
    from_rel: &str,
    module: &str,
) -> Option<&'m crate::model::Entity> {
    if let Some(stripped) = module.strip_prefix('.') {
        // TS/JS-относительный импорт: `./foo`, `../bar` — от каталога файла.
        let dir = Path::new(from_rel)
            .parent()
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        let resolved = normalize_rel_path(&format!("{dir}/{stripped}"));
        return owner_by_path(contexts, &resolved);
    }
    // Прямое совпадение в координатах импортов (Python/Java-пакеты).
    if let Some(owner) = owner_by_path(contexts, module) {
        return Some(owner);
    }
    // Разрешение модуля в файл (Rust `crate::…`, смешанные монорепо).
    // Путь импорта включает имя элемента (`beta/core/Engine`), поэтому
    // пробуем префиксы от длинного к короткому: `beta/core/Engine` →
    // `beta/core` → `beta`.
    let segments: Vec<&str> = module.split('/').collect();
    for len in (1..=segments.len()).rev() {
        let prefix = segments[..len].join("/");
        for base in bases {
            for cand in candidate_paths(base, &prefix) {
                if repo.join(&cand).is_file() {
                    return owner_by_path(contexts, &cand);
                }
            }
        }
    }
    None
}

/// Базовые каталоги для разрешения модуля в файл: корень, типовые корни
/// исходников и крейты Rust-workspace (`crates/*`).
fn candidate_bases(repo: &Path) -> Vec<String> {
    let mut bases = vec![
        String::new(),
        "src/".to_string(),
        "src/main/java/".to_string(),
        "src/main/kotlin/".to_string(),
    ];
    // Отсутствующий `crates/` — не ошибка, просто нет дополнительных баз.
    if let Ok(rd) = std::fs::read_dir(repo.join("crates")) {
        for entry in rd.flatten() {
            if entry.path().is_dir() {
                bases.push(format!("crates/{}/", entry.file_name().to_string_lossy()));
            }
        }
    }
    bases
}

/// Кандидатные пути файла для модуля `module` под базой `base`.
fn candidate_paths(base: &str, module: &str) -> Vec<String> {
    [
        "{m}.rs",
        "{m}/mod.rs",
        "{m}.py",
        "{m}/__init__.py",
        "{m}.java",
        "{m}.kt",
        "{m}.ts",
        "{m}/index.ts",
        "{m}.js",
        "{m}/index.js",
    ]
    .iter()
    .map(|pat| format!("{base}{}", pat.replace("{m}", module)))
    .collect()
}

/// Нормализует `code_root`: срезает пробелы, ведущий `./` и хвостовые `/`.
fn normalize_root(root: &str) -> String {
    root.trim()
        .trim_start_matches("./")
        .trim_end_matches('/')
        .to_string()
}

/// Нормализует относительный путь: раскрывает `.` и `..` по сегментам.
fn normalize_rel_path(path: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    for seg in path.split('/') {
        match seg {
            "" | "." => {}
            ".." => {
                out.pop();
            }
            s => out.push(s),
        }
    }
    out.join("/")
}

/// Подготовленное content-правило: скомпилированный regex, glob-шаблон
/// и набор файлов (относительный путь, абсолютный путь).
type PreparedContentRule = (Regex, String, Vec<(String, PathBuf)>);

/// Общая подготовка content-правил: компилированный regex, glob, набор файлов.
fn prep_content_rule(rule: &FitnessRule, repo: &Path) -> Result<PreparedContentRule> {
    let pattern = rule.pattern.as_deref().ok_or_else(|| {
        HarnessError::Control(format!(
            "правило '{}': для {:?} нужен pattern",
            rule.name, rule.kind
        ))
    })?;
    let re = Regex::new(pattern).map_err(|e| {
        HarnessError::Control(format!(
            "правило '{}': невалидный regex '{pattern}': {e}",
            rule.name
        ))
    })?;
    let globs = rule_globs(rule);
    let mut files = Vec::new();
    for glob in &globs {
        files.extend(collect_files(repo, glob)?);
    }
    files.sort_by(|a, b| a.0.cmp(&b.0));
    files.dedup_by(|a, b| a.0 == b.0);
    if !rule.exclude_glob.is_empty() {
        files.retain(|(rel, _)| !rule.exclude_glob.iter().any(|ex| glob_matches(ex, rel)));
    }
    Ok((re, globs.join(", "), files))
}

/// Glob'ы правила с дефолтом `**/*`.
fn rule_globs(rule: &FitnessRule) -> Vec<String> {
    if rule.glob.is_empty() {
        vec!["**/*".to_string()]
    } else {
        rule.glob.clone()
    }
}

/// Собирает файлы репозитория по простому glob-шаблону (`**` — любая глубина,
/// `*` — внутри сегмента, `?` — один символ). Возвращает (относительный путь,
/// абсолютный путь), отсортированные по относительному пути.
///
/// Служебные и производные каталоги исключены всегда: `.git`, `target`,
/// `node_modules`, `dist`, `__pycache__`, `.next`, `.pytest_cache` и
/// `.arch-handoff` — fitness-правила целятся в АРТЕФАКТЫ РЕАЛИЗАЦИИ, а не в
/// документы решения: пакет handoff содержит текст spine/TASK.md, и правило
/// `must_not_contain` срабатывало на собственные цитаты контракта (кейс 1).
fn collect_files(repo: &Path, glob: &str) -> Result<Vec<(String, PathBuf)>> {
    const SKIP: [&str; 8] = [
        ".git",
        "target",
        "node_modules",
        "dist",
        "__pycache__",
        ".next",
        ".pytest_cache",
        ".arch-handoff",
    ];
    let mut out = Vec::new();
    let walker = WalkDir::new(repo).follow_links(false).into_iter();
    for entry in walker.filter_entry(|e| {
        let name = e.file_name().to_string_lossy();
        !(e.file_type().is_dir() && SKIP.contains(&name.as_ref()))
    }) {
        let entry = entry.map_err(|e| {
            HarnessError::Control(format!("обход репозитория {}: {e}", repo.display()))
        })?;
        if !entry.file_type().is_file() {
            continue;
        }
        let rel = entry.path().strip_prefix(repo).map_err(|e| {
            HarnessError::Control(format!(
                "относительный путь {}: {e}",
                entry.path().display()
            ))
        })?;
        let rel_str = rel.to_string_lossy().replace('\\', "/");
        if glob_matches(glob, &rel_str) {
            out.push((rel_str, entry.path().to_path_buf()));
        }
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(out)
}

/// Собирает КАТАЛОГИ репозитория по glob-шаблону (для `dir_must_have_file`).
/// Возвращает относительные пути каталогов (с `/`-разделителями),
/// отсортированные. Служебные каталоги исключены тем же списком, что и в
/// [`collect_files`]; корень репозитория в выборку не входит.
fn collect_dirs(repo: &Path, glob: &str) -> Result<Vec<String>> {
    const SKIP: [&str; 8] = [
        ".git",
        "target",
        "node_modules",
        "dist",
        "__pycache__",
        ".next",
        ".pytest_cache",
        ".arch-handoff",
    ];
    let mut out = Vec::new();
    let walker = WalkDir::new(repo).follow_links(false).into_iter();
    for entry in walker.filter_entry(|e| {
        let name = e.file_name().to_string_lossy();
        !(e.file_type().is_dir() && SKIP.contains(&name.as_ref()))
    }) {
        let entry = entry.map_err(|e| {
            HarnessError::Control(format!("обход репозитория {}: {e}", repo.display()))
        })?;
        if !entry.file_type().is_dir() || entry.path() == repo {
            continue;
        }
        let rel = entry.path().strip_prefix(repo).map_err(|e| {
            HarnessError::Control(format!(
                "относительный путь {}: {e}",
                entry.path().display()
            ))
        })?;
        let rel_str = rel.to_string_lossy().replace('\\', "/");
        if glob_matches(glob, &rel_str) {
            out.push(rel_str);
        }
    }
    out.sort();
    Ok(out)
}

/// Матч одного сегмента пути по glob-шаблону (`*` — любые символы, `?` — один).
fn segment_matches(pattern: &str, name: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let n: Vec<char> = name.chars().collect();
    let (mut pi, mut ni) = (0usize, 0usize);
    let mut star: Option<(usize, usize)> = None; // (позиция '*' в шаблоне, позиция в имени)
    while ni < n.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == n[ni]) {
            pi += 1;
            ni += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star = Some((pi, ni));
            pi += 1;
        } else if let Some((sp, sn)) = star {
            pi = sp + 1;
            ni = sn + 1;
            star = Some((sp, sn + 1));
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}

/// Матч относительного пути по glob-шаблону с поддержкой `**` (любая глубина,
/// включая ноль сегментов: `**/*.rs` матчит и `main.rs`) и brace-альтернатив
/// `{a,b,c}` (декартово произведение по нескольким группам).
///
/// `pub(crate)`: разделяется с аудитом флота (`crate::fleet`, флаг `--include`).
pub(crate) fn glob_matches(pattern: &str, path: &str) -> bool {
    let parts: Vec<&str> = path.split('/').collect();
    expand_braces(pattern).iter().any(|pat| {
        let pat: Vec<&str> = pat.split('/').collect();
        match_glob_segments(&pat, &parts)
    })
}

/// Раскрытие brace-альтернатив `{a,b,c}` в glob-шаблоне: `*.{yaml,yml}` →
/// `*.yaml`, `*.yml`. Несколько групп раскрываются декартово
/// (`{net,env}/**` → `net/**`, `env/**`). Непарная скобка остаётся литералом
/// (шаблон не ломается), вложенные группы не поддерживаются.
fn expand_braces(pattern: &str) -> Vec<String> {
    let Some(open) = pattern.find('{') else {
        return vec![pattern.to_string()];
    };
    let rest = &pattern[open + 1..];
    let Some(close_rel) = rest.find('}') else {
        return vec![pattern.to_string()];
    };
    let close = open + 1 + close_rel;
    let inner = &pattern[open + 1..close];
    if inner.contains('{') {
        return vec![pattern.to_string()];
    }
    let mut out = Vec::new();
    for alt in inner.split(',') {
        let expanded = format!("{}{}{}", &pattern[..open], alt, &pattern[close + 1..]);
        out.extend(expand_braces(&expanded));
    }
    out
}

fn match_glob_segments(pat: &[&str], parts: &[&str]) -> bool {
    if pat.is_empty() {
        return parts.is_empty();
    }
    if pat[0] == "**" {
        return (0..=parts.len()).any(|skip| match_glob_segments(&pat[1..], &parts[skip..]));
    }
    if parts.is_empty() {
        return false;
    }
    segment_matches(pat[0], parts[0]) && match_glob_segments(&pat[1..], &parts[1..])
}

/// Запускает `bash -c <command>` в `repo` с ручным таймаутом:
/// spawn + опрос `try_wait` каждые 50 мс + `kill` по истечении.
/// `Ok(None)` — команда превысила таймаут и была убита.
fn run_with_timeout(repo: &Path, command: &str, timeout: Duration) -> Result<CommandOutcome> {
    let mut child = Command::new("bash")
        .arg("-c")
        .arg(command)
        .current_dir(repo)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| HarnessError::Control(format!("не удалось запустить bash: {e}")))?;
    // Читатели живут отдельно от ожидания (как в bash-инструменте агента):
    // иначе полный pipe заблокирует дочерний процесс задолго до таймаута.
    let out_task = child
        .stdout
        .take()
        .map(|p| std::thread::spawn(move || drain_tail(p)));
    let err_task = child
        .stderr
        .take()
        .map(|p| std::thread::spawn(move || drain_tail(p)));
    let start = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {
                if start.elapsed() >= timeout {
                    let _ = child.kill();
                    let _ = child.wait(); // забрать зомби
                    break None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(HarnessError::Control(format!(
                    "ошибка ожидания команды '{command}': {e}"
                )));
            }
        }
    };
    let mut captured = Vec::new();
    if let Some(t) = out_task {
        if let Ok(mut bytes) = t.join() {
            captured.append(&mut bytes);
        }
    }
    if let Some(t) = err_task {
        if let Ok(mut bytes) = t.join() {
            captured.append(&mut bytes);
        }
    }
    Ok(CommandOutcome {
        status,
        tail: String::from_utf8_lossy(&captured).into_owned(),
    })
}

/// Итог прогона `command_succeeds`-команды: статус и хвост вывода для отчёта.
struct CommandOutcome {
    /// `Some`, если команда завершилась сама (иначе — убита по таймауту).
    status: Option<ExitStatus>,
    /// Последние байты stdout+stderr (обрезаны до [`MAX_CAPTURE_BYTES`]).
    tail: String,
}

/// Сколько байт вывода храним для отчёта об упавшей команде (хвост).
const MAX_CAPTURE_BYTES: usize = 16 * 1024;
/// Сколько последних строк хвоста включаем в отчёт об упавшей команде.
const REPORT_TAIL_LINES: usize = 15;

/// Читает pipe до конца, храня только хвост в [`MAX_CAPTURE_BYTES`]
/// (вывод упавшей команды может быть мегабайтным — в отчёт нужен конец).
fn drain_tail(mut pipe: impl std::io::Read) -> Vec<u8> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        match pipe.read(&mut chunk) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if buf.len() > MAX_CAPTURE_BYTES * 2 {
                    let keep = buf.split_off(buf.len() - MAX_CAPTURE_BYTES);
                    buf = keep;
                }
            }
        }
    }
    if buf.len() > MAX_CAPTURE_BYTES {
        buf.split_off(buf.len() - MAX_CAPTURE_BYTES)
    } else {
        buf
    }
}

/// Хвост вывода упавшей команды для отчёта: последние [`REPORT_TAIL_LINES`]
/// строк, пропущенные через редактор секретов (AD-3: вывод может содержать
/// значения переменных окружения и токены).
fn report_tail(raw: &str) -> String {
    let redacted = crate::secrets::Redactor::new(crate::secrets::builtin_rules()).redact(raw);
    let lines: Vec<&str> = redacted.lines().collect();
    let skip = lines.len().saturating_sub(REPORT_TAIL_LINES);
    lines[skip..].join("\n")
}

/// Создаёт новый ADR по шаблону AI-DLC (Status/Context/Decision/Alternatives/
/// Consequences/Reversibility/References) с очередным номером в каталоге.
///
/// Номер — max(существующие `ADR-NNN-*`) + 1; каталог создаётся при
/// отсутствии. Имя файла: `ADR-NNN-kebab-case-title.md` (кириллица
/// транслитерируется, ADR-002). Разбор ID — общий regex модели
/// (`crate::model::id_re`, ADR-003), файлы не-ADR сущностей игнорируются.
///
/// # Errors
/// Каталог недоступен/не создаётся, файл уже существует.
pub fn adr_new(dir: &Path, title: &str) -> Result<PathBuf> {
    std::fs::create_dir_all(dir).map_err(|e| HarnessError::io(dir, e))?;
    let re_id = crate::model::id_re()
        .map_err(|e| HarnessError::Control(format!("внутренний regex ID: {e}")))?;
    let mut max_n = 0u64;
    let rd = std::fs::read_dir(dir).map_err(|e| HarnessError::io(dir, e))?;
    for entry in rd {
        let entry = entry.map_err(|e| HarnessError::io(dir, e))?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if let Some(caps) = re_id.captures(&name) {
            // Нумеруются только ADR-файлы; прочие сущности (CMP-*, AD-*) мимо.
            if caps.get(1).is_none_or(|m| m.as_str() != "ADR") {
                continue;
            }
            let Some(num) = caps.get(2) else {
                continue; // группа номера гарантирована паттерном; страховка от паники
            };
            let n: u64 = num.as_str().parse().map_err(|_| {
                HarnessError::Control(format!("некорректный номер ADR в имени файла '{name}'"))
            })?;
            max_n = max_n.max(n);
        }
    }
    let next = max_n + 1;
    let file = dir.join(format!("ADR-{next:03}-{}.md", kebab_slug(title)));
    if file.exists() {
        return Err(HarnessError::Control(format!(
            "ADR уже существует: {}",
            file.display()
        )));
    }
    let date = chrono::Local::now().format("%Y-%m-%d").to_string();
    std::fs::write(&file, adr_template(next, title, &date))
        .map_err(|e| HarnessError::io(&file, e))?;
    Ok(file)
}

/// Практичная транслитерация кириллической буквы для слагов (ADR-002).
/// `Some("")` для ъ/ь (опускаются без дефиса), `None` для не-кириллицы.
fn translit_cyrillic(ch: char) -> Option<&'static str> {
    let lat = match ch {
        'а' => "a",
        'б' => "b",
        'в' => "v",
        'г' => "g",
        'д' => "d",
        'е' | 'э' => "e",
        'ё' => "yo",
        'ж' => "zh",
        'з' => "z",
        'и' => "i",
        'й' | 'ы' => "y",
        'к' => "k",
        'л' => "l",
        'м' => "m",
        'н' => "n",
        'о' => "o",
        'п' => "p",
        'р' => "r",
        'с' => "s",
        'т' => "t",
        'у' => "u",
        'ф' => "f",
        'х' => "h",
        'ц' => "c",
        'ч' => "ch",
        'ш' => "sh",
        'щ' => "sch",
        'ъ' | 'ь' => "",
        'ю' => "yu",
        'я' => "ya",
        _ => return None,
    };
    Some(lat)
}

/// kebab-case slug заголовка: ASCII-буквы/цифры в нижний регистр, кириллица
/// транслитерируется (`translit_cyrillic`), всё прочее — в `-`, повторы `-`
/// схлопываются. Пустой результат (пустой/символьный заголовок) → `"adr"`.
pub(crate) fn kebab_slug(title: &str) -> String {
    let mut out = String::with_capacity(title.len());
    let mut dash = true; // подавляет '-' в начале
    for ch in title.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
            dash = false;
        } else if let Some(lat) = translit_cyrillic(ch) {
            out.push_str(lat);
            if !lat.is_empty() {
                dash = false;
            }
        } else if !dash {
            out.push('-');
            dash = true;
        }
    }
    let trimmed = out.trim_end_matches('-');
    if trimmed.is_empty() {
        "adr".into()
    } else {
        trimmed.to_string()
    }
}

/// YAML-скаляр в двойных кавычках: экранируются `\` и `"` (ADR-045 §2 —
/// frontmatter обязан оставаться разбираемым при любом заголовке).
fn yaml_dquote(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            _ => out.push(ch),
        }
    }
    out.push('"');
    out
}

/// Шаблон ADR по AI-DLC с placeholder-комментариями. Первый блок —
/// YAML-frontmatter (ADR-045 §2): `depends_on`/`affects`/`spec_files` —
/// рёбра и артефакты плана флота; пустые списки заполняет автор решения.
fn adr_template(n: u64, title: &str, date: &str) -> String {
    let title_yaml = yaml_dquote(title);
    format!(
        "---\n\
        id: ADR-{n:03}\n\
        title: {title_yaml}\n\
        status: Proposed\n\
        date: \"{date}\"\n\
        depends_on: []\n\
        affects: []\n\
        spec_files: []\n\
        ---\n\
        \n\
        # ADR-{n:03}. {title}\n\
        \n\
        - Date: {date}\n\
        - Status: Proposed\n\
        \n\
        ## Context\n\
        \n\
        <!-- Что заставляет принять решение: контекст, силы, ограничения. -->\n\
        \n\
        ## Decision\n\
        \n\
        <!-- Принятое решение: одно, явно сформулированное. -->\n\
        \n\
        ## Alternatives Considered\n\
        \n\
        | Вариант | Плюсы | Минусы |\n\
        |---------|-------|--------|\n\
        | <!-- вариант --> | <!-- плюсы --> | <!-- минусы --> |\n\
        \n\
        ## Consequences\n\
        \n\
        ### Positive\n\
        \n\
        <!-- Что станет лучше. -->\n\
        \n\
        ### Negative\n\
        \n\
        <!-- Цена решения: что станет хуже, какие риски принимаем. Обязательно к заполнению. -->\n\
        \n\
        ## Reversibility\n\
        \n\
        <!-- Обратимость: reversible | costly | irreversible. Обоснование оценки. -->\n\
        \n\
        ## References\n\
        \n\
        <!-- Ссылки на spine (AD-n), спеки, обсуждения. -->\n"
    )
}

/// Инструменты домена: `adr_new`, `spine_lint`, `fitness_check`, `significance_score`.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(AdrNewTool),
        Arc::new(SpineLintTool),
        Arc::new(FitnessCheckTool),
        Arc::new(SignificanceScoreTool),
    ]
}

/// Инструмент `adr_new`: создать ADR по шаблону AI-DLC с очередным номером.
pub struct AdrNewTool;

#[derive(Debug, Deserialize)]
struct AdrNewArgs {
    /// Заголовок решения.
    title: String,
    /// Каталог ADR (дефолт `docs/adr`).
    dir: Option<String>,
}

#[async_trait]
impl Tool for AdrNewTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "adr_new".into(),
            description: "Создать новый ADR (Architecture Decision Record) по шаблону AI-DLC \
                          (Context/Decision/Alternatives/Consequences/Reversibility) с очередным \
                          номером ADR-NNN в каталоге"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Заголовок решения"},
                    "dir": {"type": "string", "description": "Каталог ADR (по умолчанию docs/adr)"}
                },
                "required": ["title"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: AdrNewArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "adr_new: невалидные аргументы: {e}"
                )));
            }
        };
        let dir = ctx.resolve(args.dir.as_deref().unwrap_or("docs/adr"));
        match adr_new(&dir, &args.title) {
            Ok(path) => Ok(ToolOutput::ok(format!("ADR создан: {}", path.display()))),
            Err(e) => Ok(ToolOutput::err(format!("adr_new: {e}"))),
        }
    }
}

/// Инструмент `spine_lint`: линтер ARCHITECTURE-SPINE.md.
pub struct SpineLintTool;

#[derive(Debug, Deserialize)]
struct SpineLintArgs {
    /// Путь к файлу spine.
    path: String,
}

#[async_trait]
impl Tool for SpineLintTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "spine_lint".into(),
            description: "Проверить ARCHITECTURE-SPINE.md: дубли AD-id, пустые/отсутствующие \
                          Binds/Prevents/Rule, заглушки (TODO/TBD), непиннутые версии, \
                          ссылки на несуществующие AD"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к ARCHITECTURE-SPINE.md"}
                },
                "required": ["path"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: SpineLintArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "spine_lint: невалидные аргументы: {e}"
                )));
            }
        };
        let path = ctx.resolve(&args.path);
        match lint_spine(&path) {
            Ok(issues) if issues.is_empty() => Ok(ToolOutput::ok("spine: нарушений нет")),
            Ok(issues) => {
                let mut out = format!("spine: {} находок\n", issues.len());
                for i in &issues {
                    let _ = writeln!(
                        out,
                        "[{}] {}:{} {} — {}",
                        i.severity,
                        i.file.display(),
                        i.line,
                        i.rule,
                        i.message
                    );
                }
                Ok(ToolOutput::ok(out))
            }
            Err(e) => Ok(ToolOutput::err(format!("spine_lint: {e}"))),
        }
    }
}

/// Инструмент `fitness_check`: прогон fitness functions из `CONSTRAINTS.yaml`.
pub struct FitnessCheckTool;

#[derive(Debug, Deserialize)]
struct FitnessCheckArgs {
    /// Корень репозитория.
    repo: String,
    /// Путь к `CONSTRAINTS.yaml` (дефолт `<repo>/.arch-handoff/CONSTRAINTS.yaml`).
    constraints: Option<String>,
}

#[async_trait]
impl Tool for FitnessCheckTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "fitness_check".into(),
            description: "Прогнать fitness functions из CONSTRAINTS.yaml по репозиторию: \
                          must_contain / must_not_contain (regex по glob-набору файлов), \
                          each_file_must_contain (regex в КАЖДОМ файле набора), \
                          file_exists, dir_must_have_file (обязательный файл в каждом каталоге набора), \
                          max_age (свежесть файла), command_succeeds (с таймаутом), \
                          dependency_direction (направление зависимостей: импорты против forbid/allow), \
                          context_boundary (границы контекстов CMP по code_roots модели), \
                          archunit (JVM-гейт: java-правила файла исполняются настоящим ArchUnit, ADR-039). \
                          Итог PASS/FAIL + находки"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Корень репозитория"},
                    "constraints": {
                        "type": "string",
                        "description": "Путь к CONSTRAINTS.yaml (по умолчанию <repo>/.arch-handoff/CONSTRAINTS.yaml)"
                    }
                },
                "required": ["repo"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: FitnessCheckArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "fitness_check: невалидные аргументы: {e}"
                )));
            }
        };
        let repo = ctx.resolve(&args.repo);
        let constraints = args.constraints.map_or_else(
            || repo.join(".arch-handoff/CONSTRAINTS.yaml"),
            |c| ctx.resolve(c),
        );
        // Прогон может занимать минуты (command_succeeds) — уводим с worker'а runtime.
        match tokio::task::spawn_blocking(move || check(&repo, &constraints)).await {
            Ok(Ok(report)) => {
                let mut out = String::new();
                let _ = writeln!(out, "{}", report.summary);
                for i in &report.issues {
                    let _ = writeln!(
                        out,
                        "  [{}] {}:{} {} — {}",
                        i.severity,
                        i.file.display(),
                        i.line,
                        i.rule,
                        i.message
                    );
                }
                let _ = writeln!(out, "Итог: {}", if report.passed { "PASS" } else { "FAIL" });
                Ok(ToolOutput::ok(out))
            }
            Ok(Err(e)) => Ok(ToolOutput::err(format!("fitness_check: {e}"))),
            Err(e) => Ok(ToolOutput::err(format!(
                "fitness_check: задача прервана: {e}"
            ))),
        }
    }
}

/// Инструмент `significance_score`: Architecture Significance Score → маршрут.
pub struct SignificanceScoreTool;

#[derive(Debug, Deserialize)]
struct SignificanceScoreArgs {
    /// Карта «триггер → сработал».
    triggers: BTreeMap<String, bool>,
}

#[async_trait]
impl Tool for SignificanceScoreTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "significance_score".into(),
            description: "Оценить Architecture Significance Score по 15 триггерам и вернуть \
                          маршрут изменения: Fast, Standard или Critical (пороги — секция \
                          [significance] конфига, дефолт Fast 0–1 / Standard 2–4 / Critical 5+; \
                          критические триггеры security_boundary_change / irreversible_migration / \
                          criticality_or_exception форсируют Critical и не конфигурируются)"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "triggers": {
                        "type": "object",
                        "description": "Карта «триггер → true/false», ключи — из 15 канонических триггеров",
                        "additionalProperties": {"type": "boolean"}
                    }
                },
                "required": ["triggers"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let args: SignificanceScoreArgs = match serde_json::from_value(args) {
            Ok(a) => a,
            Err(e) => {
                return Ok(ToolOutput::err(format!(
                    "significance_score: невалидные аргументы: {e}"
                )));
            }
        };
        // Пороги маршрутов — из конфига ([significance], ADR-034); невалидные
        // границы — понятная ошибка инструмента, не паника и не тихий дефолт.
        let (fast_max, standard_max) = match ctx.config.significance.limits() {
            Ok(l) => l,
            Err(e) => return Ok(ToolOutput::err(format!("significance_score: {e}"))),
        };
        let s = significance_score_with_limits(&args.triggers, fast_max, standard_max);
        let fired = if s.fired.is_empty() {
            "нет".to_string()
        } else {
            s.fired.join(", ")
        };
        let mut out = format!(
            "Score: {} → маршрут {:?}; сработали: {fired}",
            s.score, s.route
        );
        let unknown: Vec<&str> = s
            .fired
            .iter()
            .map(String::as_str)
            .filter(|f| !SIGNIFICANCE_TRIGGERS.contains(f))
            .collect();
        if !unknown.is_empty() {
            let _ = write!(
                out,
                "\nВнимание: триггеры вне канонических 15: {}",
                unknown.join(", ")
            );
        }
        Ok(ToolOutput::ok(out))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Доменная fitness-библиотека ML (`aiml/library/fitness/`) обязана парситься
    /// движком `control check`: id уникальны, severity каноничны. Это гейт
    /// «каталог правил не рассинхронизирован с движком».
    ///
    /// Дополнительно проверяется ожидаемый файл роутинга гипотез
    /// `aiml/library/fitness/hypothesis-routing.yaml` (правила плагина
    /// hypothesis-router в схеме движка). Файл делает смежный срез, поэтому
    /// проверка условна: пока файла нет — тест зелёный, но как только он
    /// появится, его правила обязаны парситься (иначе сломаются незаметно).
    #[test]
    fn ml_fitness_library_parses() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("aiml/library/fitness/wave1-ml-experiment.yaml");
        let rules = load_fitness_rules(&path).expect("ML fitness-библиотека парсится");
        assert_eq!(rules.len(), 11, "11 правил в волне 1");
        for r in &rules {
            assert!(
                matches!(r.severity.as_str(), "error" | "warn"),
                "severity {} не каноничен",
                r.severity
            );
        }
        let ids: std::collections::HashSet<_> = rules.iter().filter_map(|r| r.id.clone()).collect();
        assert_eq!(ids.len(), rules.len(), "id правил уникальны");

        // Ожидаемый файл: aiml/library/fitness/hypothesis-routing.yaml.
        let hyp = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("aiml/library/fitness/hypothesis-routing.yaml");
        if hyp.exists() {
            let hrules =
                load_fitness_rules(&hyp).expect("fitness роутинга гипотез парсится движком");
            assert!(!hrules.is_empty(), "файл роутинга гипотез без правил");
            for r in &hrules {
                assert!(
                    matches!(r.severity.as_str(), "error" | "warn"),
                    "severity {} не каноничен",
                    r.severity
                );
            }
            let hids: std::collections::HashSet<_> =
                hrules.iter().filter_map(|r| r.id.clone()).collect();
            assert_eq!(hids.len(), hrules.len(), "id правил роутинга уникальны");
        }
    }

    #[test]
    fn significance_routes_by_count() {
        let mut answers = BTreeMap::new();
        assert_eq!(significance_score(&answers).route, Route::Fast);
        answers.insert("new_component".into(), true);
        answers.insert("new_vendor".into(), true);
        assert_eq!(significance_score(&answers).route, Route::Standard);
        answers.insert("security_boundary_change".into(), true);
        assert_eq!(significance_score(&answers).route, Route::Critical);
    }

    /// Дефолтный поиск ruleset'а: рабочий `<repo>/CONSTRAINTS.yaml` имеет
    /// приоритет над пакетной заготовкой (C-037, ADR-011 п. 5).
    #[test]
    fn resolve_ruleset_prefers_working_over_packet() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let working = write_file(repo, "CONSTRAINTS.yaml", "rules:\n  - name: w\n");
        write_file(
            repo,
            ".arch-handoff/CONSTRAINTS.yaml",
            "rules:\n  - name: p\n",
        );

        let resolved = resolve_ruleset(repo).expect("ruleset найден");
        assert_eq!(resolved.path, working);
        assert_eq!(resolved.kind, RulesetKind::Working);
        assert!(
            resolved.warning(repo).is_none(),
            "рабочий файл — без warnings"
        );
    }

    /// Только пакетный файл — fallback с явным предупреждением, что это
    /// заготовка, а не рабочий ruleset (PASS по ней не полноценен).
    #[test]
    fn resolve_ruleset_falls_back_to_packet_with_warning() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        let packet = write_file(
            repo,
            ".arch-handoff/CONSTRAINTS.yaml",
            "rules:\n  - name: p\n",
        );

        let resolved = resolve_ruleset(repo).expect("ruleset найден");
        assert_eq!(resolved.path, packet);
        assert_eq!(resolved.kind, RulesetKind::PacketStub);
        let warning = resolved.warning(repo).expect("предупреждение о заготовке");
        assert!(warning.contains("заготовка"), "{warning}");
        assert!(warning.contains("CONSTRAINTS.yaml"), "{warning}");
    }

    /// Ни рабочего, ни пакетного файла — явная ошибка с обоими путями.
    #[test]
    fn resolve_ruleset_missing_reports_both_paths() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path();
        assert!(resolve_ruleset(repo).is_none());
        let err = resolve_ruleset_required(repo).expect_err("файлов нет — ошибка");
        let text = err.to_string();
        assert!(text.contains("CONSTRAINTS.yaml"), "{text}");
        assert!(text.contains(".arch-handoff"), "{text}");
    }

    /// Пишет файл в каталог и возвращает его путь.
    fn write_file(dir: &Path, name: &str, content: &str) -> PathBuf {
        let p = dir.join(name);
        if let Some(parent) = p.parent() {
            std::fs::create_dir_all(parent).unwrap();
        }
        std::fs::write(&p, content).unwrap();
        p
    }

    #[test]
    fn spine_detects_all_rule_kinds() {
        let dir = tempfile::tempdir().unwrap();
        let spine = write_file(
            dir.path(),
            "ARCHITECTURE-SPINE.md",
            "# ARCHITECTURE-SPINE\n\
             \n\
             ### AD-1. Единый брокер сообщений\n\
             - Binds: интеграционный контур\n\
             - Prevents: point-to-point связность\n\
             - Rule: все события — через брокер\n\
             \n\
             ### AD-2. Хранилище\n\
             - Binds:\n\
             - Rule: см. AD-99\n\
             \n\
             ### AD-1. Повторное определение\n\
             - Binds: x\n\
             - Prevents: y\n\
             - Rule: z\n\
             \n\
             ### AD-3. Версии зависимостей\n\
             - Binds: стек\n\
             - Prevents: дрейф версий\n\
             - Rule: все зависимости пинуются\n\
             - kafka-client = \"latest\"  # TODO запиновать\n",
        );
        let issues = lint_spine(&spine).unwrap();
        let count = |rule: &str| issues.iter().filter(|i| i.rule == rule).count();
        assert_eq!(
            count("dup_ad_id"),
            1,
            "должен найти повтор AD-1: {issues:?}"
        );
        assert_eq!(
            count("empty_field"),
            2,
            "AD-2: пустой Binds + нет Prevents: {issues:?}"
        );
        assert_eq!(count("stub_marker"), 1, "TODO: {issues:?}");
        assert_eq!(count("unpinned_version"), 1, "latest: {issues:?}");
        assert_eq!(count("broken_ad_ref"), 1, "AD-99 не определён: {issues:?}");
        assert!(
            issues
                .iter()
                .all(|i| i.severity == "error" || i.severity == "warn")
        );
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "broken_ad_ref" && i.message.contains("AD-99"))
        );
        assert!(issues.iter().all(|i| i.line > 0));
    }

    #[test]
    fn spine_clean_file_has_no_issues() {
        let dir = tempfile::tempdir().unwrap();
        let spine = write_file(
            dir.path(),
            "SPINE.md",
            "### AD-1. Брокер\n\
             - Binds: контур\n\
             - Prevents: хаос\n\
             - Rule: только через брокер\n\
             \n\
             ### AD-2. Хранилище\n\
             - Binds: данные\n\
             - Prevents: дубли\n\
             - Rule: одно хранилище, см. AD-1\n",
        );
        let issues = lint_spine(&spine).unwrap();
        assert!(issues.is_empty(), "чистый spine: {issues:?}");
    }

    #[test]
    fn sensors_pass_and_fail_per_file() {
        let dir = tempfile::tempdir().unwrap();
        write_file(
            dir.path(),
            "good.md",
            "# Спека\n\
             \n\
             ## Проблема\n\
             текст\n\
             \n\
             ## Критерии приёмки\n\
             текст\n\
             \n\
             ## Риски\n\
             текст\n\
             \n\
             Связано: [вторая спека](bad.md), [сайт](https://example.com), [якорь](#риски)\n",
        );
        write_file(
            dir.path(),
            "bad.md",
            "# Плохая спека\n\
             \n\
             ## Проблема\n\
             только проблема, ссылка [битая](missing.md)\n",
        );
        write_file(
            dir.path(),
            "ignored.txt",
            "## Проблема\nне md — игнорируем\n",
        );

        let results = sensors_check(dir.path()).unwrap();
        assert_eq!(results.len(), 4, "2 md-файла × 2 сенсора: {results:?}");
        let find = |file: &str, sensor: &str| {
            results
                .iter()
                .find(|r| r.file.ends_with(file) && r.sensor == sensor)
                .unwrap()
        };
        assert!(find("good.md", "required_sections").passed);
        assert!(find("good.md", "upstream_coverage").passed);
        let bad_sections = find("bad.md", "required_sections");
        assert!(!bad_sections.passed);
        assert!(bad_sections.details.contains("## Риски"));
        assert!(bad_sections.details.contains("## Критерии приёмки"));
        let bad_links = find("bad.md", "upstream_coverage");
        assert!(!bad_links.passed);
        assert!(bad_links.details.contains("missing.md"));
    }

    #[test]
    fn fitness_all_rule_types() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "src/main.rs", "fn main() { println!(\"hi\"); }\n");
        write_file(
            &repo,
            "src/lib.rs",
            "pub fn f() {}\n// unsafe тут запрещён\n",
        );
        write_file(&repo, "Cargo.toml", "[package]\nname = \"x\"\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: has_main\n\
             \x20   type: must_contain\n\
             \x20   glob: '**/*.rs'\n\
             \x20   pattern: 'fn main'\n\
             \x20 - name: no_such_token\n\
             \x20   type: must_contain\n\
             \x20   glob: '**/*.rs'\n\
             \x20   pattern: 'zzqwxv_never'\n\
             \x20 - name: no_unsafe\n\
             \x20   type: must_not_contain\n\
             \x20   glob: '**/*.rs'\n\
             \x20   pattern: '\\bunsafe\\b'\n\
             \x20   severity: warn\n\
             \x20 - name: cargo_toml_exists\n\
             \x20   type: file_exists\n\
             \x20   path: Cargo.toml\n\
             \x20 - name: readme_exists\n\
             \x20   type: file_exists\n\
             \x20   path: README.md\n\
             \x20 - name: cmd_true\n\
             \x20   type: command_succeeds\n\
             \x20   command: 'true'\n\
             \x20 - name: cmd_false\n\
             \x20   type: command_succeeds\n\
             \x20   command: 'false'\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert_eq!(
            report.summary,
            "Правил: 7, нарушений: 4 (error: 3, warn: 1)"
        );
        assert!(!report.passed);
        let by_rule = |name: &str| report.issues.iter().find(|i| i.rule == name).unwrap();
        assert_eq!(by_rule("no_such_token").line, 0);
        assert_eq!(by_rule("no_such_token").severity, "error");
        let unsafe_issue = by_rule("no_unsafe");
        assert_eq!(unsafe_issue.severity, "warn");
        assert_eq!(unsafe_issue.line, 2, "unsafe на второй строке lib.rs");
        assert!(unsafe_issue.file.ends_with("src/lib.rs"));
        assert_eq!(by_rule("readme_exists").severity, "error");
        assert!(by_rule("cmd_false").message.contains("неуспешно"));
        assert!(report.issues.iter().all(|i| i.rule != "has_main"
            && i.rule != "cargo_toml_exists"
            && i.rule != "cmd_true"));
    }

    #[test]
    fn fitness_report_json_matches_sdk_contract_v1() {
        // SDK-контракт v1 (sdk/CONTRACT.md §2): однострочный JSON FitnessReport
        // с ключами repo/passed/summary/issues и полями находки
        // file/line/rule/message/severity.
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "src/main.py", "pan = \"4276550012345678\"\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "constraints:\n\
             \x20 - id: BANK-01\n\
             \x20   name: no_pan\n\
             \x20   type: must_not_contain\n\
             \x20   glob: 'src/**/*.py'\n\
             \x20   pattern: '\\b\\d{16}\\b'\n\
             \x20   severity: critical\n",
        );
        let report = check(&repo, &constraints).unwrap();
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&report).unwrap()).unwrap();
        assert_eq!(v["passed"], false);
        assert!(v["repo"].is_string() && v["summary"].is_string());
        let issue = &v["issues"][0];
        for key in ["file", "line", "rule", "message", "severity"] {
            assert!(issue.get(key).is_some(), "нет ключа {key} в issue");
        }
        assert_eq!(issue["rule"], "no_pan");
        assert_eq!(issue["severity"], "error");
        assert_eq!(issue["line"], 1);
    }

    #[test]
    fn fitness_each_file_must_contain_per_file_issues() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "services/a/config.yaml", "timeout: 5s\n");
        write_file(&repo, "services/b/config.yaml", "retries: 3\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: every_client_has_timeout\n\
             \x20   type: each_file_must_contain\n\
             \x20   glob: 'services/*/config.yaml'\n\
             \x20   pattern: 'timeout'\n\
             \x20 - name: glob_miss\n\
             \x20   type: each_file_must_contain\n\
             \x20   glob: 'services/*/settings.yaml'\n\
             \x20   pattern: 'timeout'\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        let by_rule = |name: &str| {
            report
                .issues
                .iter()
                .filter(|i| i.rule == name)
                .collect::<Vec<_>>()
        };
        let timeout_issues = by_rule("every_client_has_timeout");
        assert_eq!(
            timeout_issues.len(),
            1,
            "только services/b: {timeout_issues:?}"
        );
        assert!(timeout_issues[0].file.ends_with("services/b/config.yaml"));
        assert_eq!(by_rule("glob_miss").len(), 1, "пустой набор — находка");
    }

    #[test]
    fn fitness_dir_must_have_file_per_dir_issues() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "services/a/openapi.yaml", "openapi: 3.0.0\n");
        write_file(&repo, "services/b/main.rs", "fn main() {}\n");
        // Служебный каталог не должен попадать в выборку даже при широком glob.
        write_file(&repo, "node_modules/pkg/package.json", "{}\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: every_service_has_contract\n\
             \x20   type: dir_must_have_file\n\
             \x20   glob: 'services/*'\n\
             \x20   path: openapi.yaml\n\
             \x20 - name: glob_miss\n\
             \x20   type: dir_must_have_file\n\
             \x20   glob: 'apps/*'\n\
             \x20   path: openapi.yaml\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        let by_rule = |name: &str| {
            report
                .issues
                .iter()
                .filter(|i| i.rule == name)
                .collect::<Vec<_>>()
        };
        let contract_issues = by_rule("every_service_has_contract");
        assert_eq!(
            contract_issues.len(),
            1,
            "только services/b: {contract_issues:?}"
        );
        assert!(contract_issues[0].file.ends_with("services/b"));
        assert!(contract_issues[0].message.contains("openapi.yaml"));
        assert_eq!(
            by_rule("glob_miss").len(),
            1,
            "пустой набор каталогов — находка"
        );
        assert!(
            report
                .issues
                .iter()
                .all(|i| !i.file.to_string_lossy().contains("node_modules"))
        );
    }

    #[test]
    fn fitness_dir_must_have_file_passes_when_all_dirs_have_it() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "services/a/openapi.yaml", "openapi: 3.0.0\n");
        write_file(&repo, "services/b/openapi.yaml", "openapi: 3.0.0\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: every_service_has_contract\n\
             \x20   type: dir_must_have_file\n\
             \x20   glob: 'services/*'\n\
             \x20   path: openapi.yaml\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(report.passed, "{:?}", report.issues);
    }

    #[test]
    fn fitness_max_age_fresh_file_passes() {
        // (а) свежий файл + реальное now → pass: возраст файла (микросекунды)
        // несопоставим с лимитом 3650 дней.
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "evidence/dr-report.md", "учения: 2026-08\n");
        assert_eq!(
            check_max_age(&repo, "evidence/dr-report.md", 3650, SystemTime::now()).unwrap(),
            None
        );
    }

    #[test]
    fn fitness_max_age_stale_file_fails_with_age() {
        // (б) тот же файл + now, сдвинутый на max_age_days + запас → fail
        // с возрастом в днях против лимита (mtime не трогаем — std не умеет).
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        let evidence = write_file(&repo, "evidence/dr-report.md", "учения: 2026-08\n");
        let mtime = std::fs::metadata(&evidence).unwrap().modified().unwrap();
        let stale_now = mtime + Duration::from_secs((3650 + 2) * 86_400);
        let message = check_max_age(&repo, "evidence/dr-report.md", 3650, stale_now)
            .unwrap()
            .expect("файл старше лимита — нарушение");
        assert!(message.contains("устарел"), "{message}");
        assert!(message.contains("3652 дн."), "{message}");
        assert!(message.contains("лимите 3650"), "{message}");
    }

    #[test]
    fn fitness_max_age_missing_file_fails() {
        // (в) отсутствующий файл → fail с сообщением как у file_exists.
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let message = check_max_age(&repo, "evidence/missing.md", 3650, SystemTime::now())
            .unwrap()
            .expect("отсутствующий файл — нарушение");
        assert_eq!(message, "file_exists: файл не найден: evidence/missing.md");
    }

    #[test]
    fn fitness_max_age_yaml_parses_and_backward_compat() {
        // (г) YAML: правило max_age (path + max_age_days) парсится и работает;
        // старые типы рядом парсятся без изменений (обратная совместимость).
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "evidence/dr-report.md", "учения: 2026-08\n");
        write_file(&repo, "src/main.rs", "fn main() {}\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: dr_report_fresh\n\
             \x20   type: max_age\n\
             \x20   path: evidence/dr-report.md\n\
             \x20   max_age_days: 365\n\
             \x20 - name: legacy_file_exists\n\
             \x20   type: file_exists\n\
             \x20   path: evidence/dr-report.md\n\
             \x20 - name: legacy_must_contain\n\
             \x20   type: must_contain\n\
             \x20   glob: 'src/**/*.rs'\n\
             \x20   pattern: 'fn main'\n\
             \x20 - name: legacy_command\n\
             \x20   type: command_succeeds\n\
             \x20   command: 'true'\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(
            report.passed,
            "max_age и старые типы парсятся и проходят: {:?}",
            report.issues
        );
        assert_eq!(
            report.summary,
            "Правил: 4, нарушений: 0 (error: 0, warn: 0)"
        );
        // max_age без обязательных полей — ошибка парсинга правила.
        let no_days = write_file(
            dir.path(),
            "no-days.yaml",
            "rules:\n\
             \x20 - name: x\n\
             \x20   type: max_age\n\
             \x20   path: a.txt\n",
        );
        assert!(
            check(&repo, &no_days).is_err(),
            "max_age без max_age_days — ошибка"
        );
        let no_path = write_file(
            dir.path(),
            "no-path.yaml",
            "rules:\n\
             \x20 - name: x\n\
             \x20   type: max_age\n\
             \x20   max_age_days: 365\n",
        );
        assert!(check(&repo, &no_path).is_err(), "max_age без path — ошибка");
    }

    #[test]
    fn fitness_max_age_medium_severity_warns() {
        // (д) severity medium наследуется общим правилом: medium → warn,
        // итог остаётся PASS. max_age_days: 0 — любой файл «старше» лимита
        // (mtime всегда раньше now), детерминированно без трюков с mtime.
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "evidence/stale.md", "отчёт прошлого года\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: stale_evidence\n\
             \x20   type: max_age\n\
             \x20   path: evidence/stale.md\n\
             \x20   max_age_days: 0\n\
             \x20   severity: medium\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(report.passed, "medium → warn не ломает итог");
        assert_eq!(report.issues.len(), 1);
        assert_eq!(report.issues[0].severity, "warn");
        assert!(report.issues[0].message.contains("устарел"));
        assert_eq!(
            report.summary,
            "Правил: 1, нарушений: 1 (error: 0, warn: 1)"
        );
    }

    #[test]
    fn fitness_exclude_glob_string_and_list_forms() {
        // Мотив — живой кейс флота 2026-09-01: архитектурный агент писал
        // exclude_glob в CONSTRAINTS.yaml, а движок поле игнорировал.
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        // mart.py — легитимное место JOIN (сборщик витрины), исключён из запрета.
        write_file(&repo, "poc/meta_agent/mart.py", "SELECT a JOIN b\n");
        write_file(&repo, "poc/meta_agent/api.py", "SELECT a JOIN b\n");
        write_file(&repo, "services/a/openapi.yaml", "openapi: 3.0.0\n");
        write_file(&repo, "services/b/openapi.yaml", "openapi: 3.0.0\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: no_runtime_join\n\
             \x20   type: must_not_contain\n\
             \x20   glob: 'poc/**/*.py'\n\
             \x20   pattern: 'JOIN'\n\
             \x20   exclude_glob: 'poc/meta_agent/mart.py'\n\
             \x20 - name: contracts_except_legacy\n\
             \x20   type: dir_must_have_file\n\
             \x20   glob: 'services/*'\n\
             \x20   path: asyncapi.yaml\n\
             \x20   exclude_glob: ['services/a', 'services/b']\n",
        );
        let report = check(&repo, &constraints).unwrap();
        let join_issues: Vec<_> = report
            .issues
            .iter()
            .filter(|i| i.rule == "no_runtime_join")
            .collect();
        assert_eq!(join_issues.len(), 1, "только api.py: {join_issues:?}");
        assert!(join_issues[0].file.ends_with("api.py"));
        // Оба каталога исключены списком — набор пуст → одна находка о пустом наборе.
        let dir_issues: Vec<_> = report
            .issues
            .iter()
            .filter(|i| i.rule == "contracts_except_legacy")
            .collect();
        assert_eq!(dir_issues.len(), 1, "{dir_issues:?}");
        assert!(
            dir_issues[0]
                .message
                .contains("не найдено ни одного каталога")
        );
    }

    #[test]
    fn fitness_expired_rule_warns_but_passes_and_metadata_accepted() {
        // Карточка правила (trigger/rationale/owner/expiry из шаблона
        // дистилляции) — опциональные метаданные; expiry в прошлом —
        // warn-находка, итог не ломает.
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "src/main.rs", "fn main() {}\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: old_rule\n\
             \x20   type: file_exists\n\
             \x20   path: src/main.rs\n\
             \x20   trigger: пока стек X\n\
             \x20   rationale: исторический запрет\n\
             \x20   owner: архитектор контура\n\
             \x20   expiry: '2020-01-01'\n\
             \x20 - name: fresh_rule\n\
             \x20   type: file_exists\n\
             \x20   path: src/main.rs\n\
             \x20   expiry: '2999-01-01'\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(report.passed, "warn не ломает итог: {:?}", report.issues);
        let expired: Vec<_> = report
            .issues
            .iter()
            .filter(|i| i.rule == "old_rule")
            .collect();
        assert_eq!(expired.len(), 1, "{expired:?}");
        assert_eq!(expired[0].severity, "warn");
        assert!(expired[0].message.contains("просрочено"));
        assert!(
            report.issues.iter().all(|i| i.rule != "fresh_rule"),
            "будущая дата — без находки: {:?}",
            report.issues
        );
    }

    #[test]
    fn fitness_skips_handoff_packet_and_junk_dirs() {
        // Разрыв P2 «fitness целится в документ решения»: правило с широким
        // glob срабатывало на текст spine внутри пакета. Служебные каталоги
        // (.arch-handoff, node_modules, __pycache__, …) исключены из обхода.
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "src/bad.py", "print('hi')\n");
        write_file(
            &repo,
            ".arch-handoff/TASK.md",
            "Контекст: print( запрещён в проде\n",
        );
        write_file(&repo, "node_modules/pkg/index.js", "print('junk')\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: no_print\n\
             \x20   type: must_not_contain\n\
             \x20   glob: '**/*'\n\
             \x20   pattern: 'print\\('\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert_eq!(
            report.issues.len(),
            1,
            "только код, не пакет: {:?}",
            report.issues
        );
        assert!(report.issues[0].file.ends_with("src/bad.py"));
    }

    #[test]
    fn fitness_passes_when_all_rules_hold() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "src/main.rs", "fn main() {}\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: has_main\n\
             \x20   type: must_contain\n\
             \x20   glob: '**/*.rs'\n\
             \x20   pattern: 'fn main'\n\
             \x20 - name: cmd_ok\n\
             \x20   type: command_succeeds\n\
             \x20   command: 'true'\n\
             \x20   timeout_secs: 5\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(report.passed);
        assert!(report.issues.is_empty());
        assert_eq!(
            report.summary,
            "Правил: 2, нарушений: 0 (error: 0, warn: 0)"
        );
    }

    #[test]
    fn fitness_command_timeout_fails_rule() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: slow\n\
             \x20   type: command_succeeds\n\
             \x20   command: 'sleep 5'\n\
             \x20   timeout_secs: 1\n",
        );
        let started = std::time::Instant::now();
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert_eq!(report.issues.len(), 1);
        assert!(report.issues[0].message.contains("таймаут"));
        assert!(
            started.elapsed() < Duration::from_secs(4),
            "таймаут должен убить команду раньше её завершения"
        );
    }

    #[test]
    fn fitness_rejects_invalid_constraints() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let bad_yaml = write_file(dir.path(), "bad.yaml", "rules: [\n");
        assert!(check(&repo, &bad_yaml).is_err(), "битый YAML — ошибка");
        let bad_severity = write_file(
            dir.path(),
            "sev.yaml",
            "rules:\n\
             \x20 - name: x\n\
             \x20   type: file_exists\n\
             \x20   path: a.txt\n\
             \x20   severity: fatal\n",
        );
        assert!(
            check(&repo, &bad_severity).is_err(),
            "неизвестный severity — ошибка"
        );
        assert!(
            check(&repo, &repo.join("missing.yaml")).is_err(),
            "несуществующий constraints — ошибка"
        );
    }

    #[test]
    fn fitness_accepts_constraints_root() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        std::fs::write(repo.join("ok.go"), "package main\n").unwrap();
        // Корень `constraints:` (стиль кейсов/handoff-пакетов) читается наравне
        // с каноническим `rules:`.
        let file = write_file(
            dir.path(),
            "constraints.yaml",
            "constraints:\n\
             \x20 - id: C-001\n\
             \x20   name: go-file\n\
             \x20   type: must_contain\n\
             \x20   glob: \"**/*.go\"\n\
             \x20   pattern: package\n",
        );
        let report = check(&repo, &file).unwrap();
        assert!(report.passed, "правило из корня constraints: выполняется");
        let failing = write_file(
            dir.path(),
            "constraints-fail.yaml",
            "constraints:\n\
             \x20 - id: C-001\n\
             \x20   name: no-todo\n\
             \x20   type: must_not_contain\n\
             \x20   glob: \"**/*.go\"\n\
             \x20   pattern: package\n\
             \x20   severity: critical\n",
        );
        let report = check(&repo, &failing).unwrap();
        assert!(!report.passed, "нарушение из корня constraints: ловится");
        assert!(
            report.issues.iter().all(|i| i.severity == "error"),
            "critical маппится в блокирующий error"
        );
    }

    #[test]
    fn fitness_rejects_constraints_file_without_rules() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        // Опечатка в корне (`rulez:`) не должна давать тихий PASS.
        let typo = write_file(dir.path(), "typo.yaml", "rulez:\n  - name: x\n");
        assert!(
            check(&repo, &typo).is_err(),
            "файл без правил rules:/constraints: — ошибка, а не тихий PASS"
        );
        let empty = write_file(dir.path(), "empty.yaml", "rules: []\n");
        assert!(
            check(&repo, &empty).is_err(),
            "пустой список правил — ошибка"
        );
    }

    #[test]
    fn adr_new_numbers_sequentially() {
        let dir = tempfile::tempdir().unwrap();
        let adr_dir = dir.path().join("docs/adr"); // каталога ещё нет — должен создаться
        let first = adr_new(&adr_dir, "API Contract Change").unwrap();
        assert_eq!(
            first.file_name().unwrap().to_string_lossy(),
            "ADR-001-api-contract-change.md"
        );
        let content = std::fs::read_to_string(&first).unwrap();
        for needle in [
            "# ADR-001. API Contract Change",
            "- Status: Proposed",
            "## Context",
            "## Decision",
            "## Alternatives Considered",
            "### Positive",
            "### Negative",
            "## Reversibility",
            "reversible | costly | irreversible",
            "## References",
            "<!--",
        ] {
            assert!(content.contains(needle), "в шаблоне нет '{needle}'");
        }
        let second = adr_new(&adr_dir, "Шина событий").unwrap();
        assert_eq!(
            second.file_name().unwrap().to_string_lossy(),
            "ADR-002-shina-sobytiy.md",
            "кириллица транслитерируется (ADR-002)"
        );
    }

    #[test]
    fn adr_new_writes_parseable_frontmatter() {
        // ADR-045 §2: ADR обязан нести YAML-frontmatter первым блоком —
        // иначе `depends_on`/`affects` объявить негде и граф работ из ADR
        // вырождается.
        let dir = tempfile::tempdir().unwrap();
        let adr_dir = dir.path().join("docs/adr");
        let path = adr_new(&adr_dir, "План из решений").unwrap();
        let content = std::fs::read_to_string(&path).unwrap();
        assert!(content.starts_with("---\n"), "{content}");
        let (yaml, body) = crate::model::split_frontmatter(&content).unwrap();
        let fm: serde_yaml_ng::Value = serde_yaml_ng::from_str(yaml).unwrap();
        assert_eq!(fm["id"].as_str(), Some("ADR-001"), "{yaml}");
        assert_eq!(fm["title"].as_str(), Some("План из решений"), "{yaml}");
        assert_eq!(fm["status"].as_str(), Some("Proposed"), "{yaml}");
        let date = fm["date"].as_str().expect("дата обязательна");
        assert_eq!(date.len(), 10, "дата в формате YYYY-MM-DD: {date}");
        for field in ["depends_on", "affects", "spec_files"] {
            assert_eq!(
                fm[field].as_sequence().map(Vec::len),
                Some(0),
                "поле {field} — пустой список: {yaml}"
            );
        }
        // Существующее тело (заголовок, шапка, разделы) сохранено.
        for needle in [
            "# ADR-001. План из решений",
            "- Date:",
            "- Status: Proposed",
            "## Context",
            "## Decision",
        ] {
            assert!(body.contains(needle), "в теле нет '{needle}'");
        }
    }

    #[test]
    fn adr_new_frontmatter_escapes_title_quotes() {
        // Кавычка в заголовке не должна ломать YAML-frontmatter.
        let dir = tempfile::tempdir().unwrap();
        let adr_dir = dir.path().join("docs/adr");
        let path = adr_new(&adr_dir, "Решение \"быстрое\"").unwrap();
        let content = std::fs::read_to_string(&path).unwrap();
        let (yaml, _) = crate::model::split_frontmatter(&content).unwrap();
        let fm: serde_yaml_ng::Value = serde_yaml_ng::from_str(yaml).unwrap();
        assert_eq!(fm["title"].as_str(), Some("Решение \"быстрое\""));
    }

    #[test]
    fn adr_new_ignores_non_adr_entity_files() {
        // Общий regex ID (model::id_re) матчит и CMP-*/AD-* имена —
        // нумерацию ADR они затрагивать не должны (ADR-003).
        let dir = tempfile::tempdir().unwrap();
        let adr_dir = dir.path().join("docs/adr");
        std::fs::create_dir_all(&adr_dir).unwrap();
        for name in [
            "CMP-999-payment-gateway.md",
            "AD-27-multi-currency.md",
            "README.md",
        ] {
            std::fs::write(adr_dir.join(name), "посторонний файл").unwrap();
        }
        let first = adr_new(&adr_dir, "Outbox").unwrap();
        assert_eq!(
            first.file_name().unwrap().to_string_lossy(),
            "ADR-001-outbox.md",
            "CMP-999/AD-27 не влияют на номер ADR"
        );
    }

    #[test]
    fn spine_ad_ids_heading_and_bare_forms() {
        // Заголовки `## AD-<n>:` и «голые» строки `AD-<n>.` — определения;
        // вхождения AD-<n> в тексте — ссылки, не определения (ADR-006).
        let dir = tempfile::tempdir().unwrap();
        let spine = dir.path().join("ARCHITECTURE-SPINE.md");
        std::fs::write(
            &spine,
            "# Spine\n\n## AD-1: Первый\n\nСсылаемся на AD-2 в тексте.\n\nAD-2. Второй (bare-форма)\n\n- **Rule**: AD-1 применяется.\n",
        )
        .unwrap();
        let ids = spine_ad_ids(&spine).unwrap();
        assert_eq!(ids, BTreeSet::from([1, 2]), "только определения");
    }

    #[test]
    fn kebab_slug_transliterates_cyrillic() {
        assert_eq!(
            kebab_slug("Сегментация доверенных зон (4 зоны)"),
            "segmentaciya-doverennyh-zon-4-zony"
        );
        assert_eq!(
            kebab_slug("Стратегия идемпотентности на точках входа"),
            "strategiya-idempotentnosti-na-tochkah-vhoda"
        );
        // ъ/ь опускаются без дефиса, ё → yo, щ → sch
        assert_eq!(kebab_slug("Подъём щёточный"), "podyom-schyotochnyy");
    }

    #[test]
    fn kebab_slug_mixed_latin_cyrillic() {
        assert_eq!(kebab_slug("Outbox паттерн"), "outbox-pattern");
        assert_eq!(kebab_slug("ADR для async рельсов"), "adr-dlya-async-relsov");
    }

    #[test]
    fn kebab_slug_empty_falls_back_to_adr() {
        assert_eq!(kebab_slug(""), "adr");
        assert_eq!(kebab_slug("!!! ..."), "adr");
    }

    #[test]
    fn glob_matcher_cases() {
        assert!(glob_matches("**/*.rs", "src/main.rs"));
        assert!(
            glob_matches("**/*.rs", "main.rs"),
            "** матчит ноль сегментов"
        );
        assert!(glob_matches("*.md", "a.md"));
        assert!(!glob_matches("*.md", "docs/a.md"));
        assert!(glob_matches("docs/**", "docs/a/b.txt"));
        assert!(glob_matches("src/*/mod.rs", "src/foo/mod.rs"));
        assert!(!glob_matches("src/*/mod.rs", "src/foo/bar/mod.rs"));
        assert!(glob_matches("plain/path.txt", "plain/path.txt"));
        assert!(!glob_matches("plain/path.txt", "plain/other.txt"));
        assert!(segment_matches("f?o.rs", "foo.rs"));
        assert!(!segment_matches("f?o.rs", "fo.rs"));
        assert!(segment_matches("*", "anything"));
    }

    #[test]
    fn glob_matcher_brace_alternatives() {
        // Классический кейс wave-файлов: `**/*.{yaml,yml,toml}`.
        assert!(glob_matches("**/*.{yaml,yml,toml}", "configs/grpo.yaml"));
        assert!(glob_matches("**/*.{yaml,yml,toml}", "a/b/c.toml"));
        assert!(glob_matches("**/*.{yaml,yml,toml}", "root.yml"));
        assert!(!glob_matches("**/*.{yaml,yml,toml}", "a/b/c.json"));
        // Альтернативы каталогов и несколько групп (декартово).
        assert!(glob_matches(
            "кейсы/kimi-killer/{net,env}/**/*.md",
            "кейсы/kimi-killer/env/x/y.md"
        ));
        assert!(glob_matches(
            "**/{openapi,asyncapi}.{yaml,yml,json}",
            "api/asyncapi.json"
        ));
        assert!(!glob_matches(
            "**/{openapi,asyncapi}.{yaml,yml,json}",
            "api/graphql.json"
        ));
        // Непарная/вложенная скобка — литерал, без паники.
        assert!(!glob_matches("**/*.{yaml,yml", "a.yaml"));
        assert!(!glob_matches("**/*.{a,{b,c}}", "a.c"));
        // Шаблоны без скобок неизменны.
        assert_eq!(expand_braces("**/*.rs"), vec!["**/*.rs".to_string()]);
        // Декартово произведение двух групп.
        assert_eq!(
            expand_braces("{a,b}{1,2}"),
            vec![
                "a1".to_string(),
                "a2".to_string(),
                "b1".to_string(),
                "b2".to_string()
            ]
        );
    }

    #[test]
    fn tools_expose_four_domain_specs() {
        let mut names: Vec<String> = tools().iter().map(|t| t.spec().name.clone()).collect();
        names.sort();
        assert_eq!(
            names,
            [
                "adr_new",
                "fitness_check",
                "significance_score",
                "spine_lint"
            ]
        );
    }

    #[tokio::test]
    async fn significance_tool_scores_via_call() {
        let dir = tempfile::tempdir().unwrap();
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let tool = SignificanceScoreTool;
        let out = tool
            .call(
                json!({"triggers": {"new_component": true, "new_vendor": true, "exotic": true}}),
                &ctx,
            )
            .await
            .unwrap();
        assert!(!out.is_error, "{:?}", out.content);
        assert!(out.content.contains("Standard"), "{}", out.content);
        assert!(
            out.content.contains("exotic"),
            "неизвестный триггер подсвечен: {}",
            out.content
        );
    }

    #[tokio::test]
    async fn spine_tool_reports_findings() {
        let dir = tempfile::tempdir().unwrap();
        write_file(dir.path(), "SPINE.md", "### AD-1. X\n- Binds:\n");
        let ctx = ToolContext::new(
            dir.path().to_path_buf(),
            Arc::new(crate::config::Config::default()),
        );
        let tool = SpineLintTool;
        let out = tool.call(json!({"path": "SPINE.md"}), &ctx).await.unwrap();
        assert!(!out.is_error);
        assert!(out.content.contains("empty_field"), "{}", out.content);
        let err = tool.call(json!({"path": "nope.md"}), &ctx).await.unwrap();
        assert!(err.is_error, "несуществующий файл — is_error");
    }

    // --- dependency_direction (ADR-029) -----------------------------------

    /// Мини-репозиторий для слоевых тестов: `src/llm/engine.rs` импортирует
    /// `crate::config` (use) и `crate::agent` (инлайн-путь), комментарий
    /// упоминает `crate::tui` (должен игнорироваться).
    fn deps_repo(dir: &Path) -> PathBuf {
        let repo = dir.join("repo");
        write_file(
            &repo,
            "src/llm/engine.rs",
            "use crate::config::Config;\n\
             \n\
             pub fn f() {\n\
             \x20   let _ = crate::agent::run();\n\
             }\n\
             // crate::tui::render — упоминание в комментарии\n",
        );
        write_file(&repo, "src/agent.rs", "pub fn run() {}\n");
        repo
    }

    #[test]
    fn fitness_dependency_direction_forbid_violation_and_pass() {
        let dir = tempfile::tempdir().unwrap();
        let repo = deps_repo(dir.path());
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: llm_no_agent\n\
             \x20   type: dependency_direction\n\
             \x20   glob: 'src/llm/**'\n\
             \x20   forbid: ['agent', 'tui']\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert_eq!(report.issues.len(), 1, "{:?}", report.issues);
        let i = &report.issues[0];
        assert_eq!(i.rule, "llm_no_agent");
        assert_eq!(i.line, 4, "инлайн-путь на 4-й строке: {i:?}");
        assert!(i.file.ends_with("src/llm/engine.rs"));
        assert!(i.message.contains("crate-путь 'agent'") || i.message.contains("'agent'"));
        assert!(
            !i.message.contains("tui"),
            "комментарий с упоминанием tui-модуля игнорируется: {i:?}"
        );

        // Чистый вариант: forbid только tui — нарушений нет.
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS2.yaml",
            "rules:\n\
             \x20 - name: llm_no_tui\n\
             \x20   type: dependency_direction\n\
             \x20   glob: 'src/llm/**'\n\
             \x20   forbid: ['tui']\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(report.passed, "{:?}", report.issues);
    }

    #[test]
    fn fitness_dependency_direction_allow_list() {
        let dir = tempfile::tempdir().unwrap();
        let repo = deps_repo(dir.path());
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: llm_allow\n\
             \x20   type: dependency_direction\n\
             \x20   glob: ['src/llm.rs', 'src/llm/**']\n\
             \x20   allow: ['config', 'error']\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert_eq!(report.issues.len(), 1, "{:?}", report.issues);
        assert!(
            report.issues[0].message.contains("вне allow-списка"),
            "{:?}",
            report.issues[0]
        );
    }

    #[test]
    fn fitness_dependency_direction_empty_allow_forbids_all() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "src/error.rs", "pub struct E;\n");
        write_file(&repo, "src/secrets.rs", "use crate::error::E;\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: leaves\n\
             \x20   type: dependency_direction\n\
             \x20   glob: ['src/error.rs', 'src/secrets.rs']\n\
             \x20   allow: []\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert_eq!(report.issues.len(), 1, "{:?}", report.issues);
        assert!(report.issues[0].file.ends_with("src/secrets.rs"));
    }

    #[test]
    fn fitness_dependency_direction_requires_exactly_one_mode() {
        let dir = tempfile::tempdir().unwrap();
        let repo = deps_repo(dir.path());
        for (name, extra) in [
            ("both", "forbid: ['agent']\n   allow: ['config']\n"),
            ("none", ""),
        ] {
            let constraints = write_file(
                dir.path(),
                &format!("C-{name}.yaml"),
                &format!(
                    "rules:\n - name: bad\n   type: dependency_direction\n   glob: 'src/**'\n   {extra}"
                ),
            );
            let err = check(&repo, &constraints).unwrap_err();
            assert!(
                err.to_string().contains("ровно одно из forbid/allow"),
                "{name}: {err}"
            );
        }
    }

    #[test]
    fn fitness_dependency_direction_empty_file_set_is_issue() {
        let dir = tempfile::tempdir().unwrap();
        let repo = deps_repo(dir.path());
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: dead_glob\n\
             \x20   type: dependency_direction\n\
             \x20   glob: 'src/ghost/**'\n\
             \x20   forbid: ['agent']\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert!(
            report.issues[0]
                .message
                .contains("правилу нечего проверять"),
            "{:?}",
            report.issues[0]
        );
    }

    #[test]
    fn fitness_dependency_direction_python_imports() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(
            &repo,
            "services/api/main.py",
            "import os\nfrom services.legacy.db import connect\n# from services.legacy.x import y\n",
        );
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: no_legacy\n\
             \x20   type: dependency_direction\n\
             \x20   glob: 'services/**/*.py'\n\
             \x20   forbid: ['services/legacy']\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert_eq!(report.issues.len(), 1, "{:?}", report.issues);
        assert_eq!(report.issues[0].line, 2);
        assert!(report.issues[0].message.contains("services/legacy/db"));
    }

    // --- context_boundary (ADR-030) ----------------------------------------

    /// Репозиторий с моделью из двух контекстов и python-кодом:
    /// `services/alpha` импортирует `services.beta` (через границу).
    fn contexts_repo(dir: &Path, alpha_depends_on_beta: bool) -> PathBuf {
        let repo = dir.join("repo");
        let depends = if alpha_depends_on_beta {
            "depends_on: [CMP-002]\n"
        } else {
            ""
        };
        write_file(
            &repo,
            "model/CMP-001-alpha.md",
            &format!(
                "---\nid: CMP-001\ntype: cmp\ntitle: Alpha\nstatus: adopted\n{depends}code_roots: [services/alpha]\n---\nКонтекст A.\n"
            ),
        );
        write_file(
            &repo,
            "model/CMP-002-beta.md",
            "---\nid: CMP-002\ntype: cmp\ntitle: Beta\nstatus: adopted\ncode_roots: [services/beta]\n---\nКонтекст B.\n",
        );
        write_file(
            &repo,
            "services/alpha/main.py",
            "from services.beta.core import run\nfrom services.alpha.util import helper\n",
        );
        write_file(&repo, "services/beta/core.py", "def run(): pass\n");
        write_file(&repo, "services/alpha/util.py", "def helper(): pass\n");
        write_file(
            &repo,
            "tools/free.py",
            "from services.beta.core import run\n",
        );
        repo
    }

    #[test]
    fn fitness_context_boundary_detects_crossing() {
        let dir = tempfile::tempdir().unwrap();
        let repo = contexts_repo(dir.path(), false);
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: contexts\n\
             \x20   type: context_boundary\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert_eq!(report.issues.len(), 1, "{:?}", report.issues);
        let i = &report.issues[0];
        assert_eq!(i.line, 1, "только импорт beta; свой alpha и tools/ — мимо");
        assert!(i.file.ends_with("services/alpha/main.py"));
        assert!(
            i.message.contains("CMP-001") && i.message.contains("CMP-002"),
            "{i:?}"
        );
    }

    #[test]
    fn fitness_context_boundary_depends_on_allows_crossing() {
        let dir = tempfile::tempdir().unwrap();
        let repo = contexts_repo(dir.path(), true);
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: contexts\n\
             \x20   type: context_boundary\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(report.passed, "{:?}", report.issues);
    }

    #[test]
    fn fitness_context_boundary_resolves_rust_crate_paths() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(
            &repo,
            "model/CMP-001-alpha.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: Alpha\nstatus: adopted\ncode_roots: [src/alpha]\n---\nA.\n",
        );
        write_file(
            &repo,
            "model/CMP-002-beta.md",
            "---\nid: CMP-002\ntype: cmp\ntitle: Beta\nstatus: adopted\ncode_roots: [src/beta]\n---\nB.\n",
        );
        write_file(
            &repo,
            "src/alpha/lib.rs",
            "use crate::beta::core::Engine;\n",
        );
        write_file(&repo, "src/beta/core.rs", "pub struct Engine;\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: contexts\n\
             \x20   type: context_boundary\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert_eq!(report.issues.len(), 1, "{:?}", report.issues);
        assert!(report.issues[0].file.ends_with("src/alpha/lib.rs"));
    }

    #[test]
    fn fitness_context_boundary_missing_model_dir_is_issue() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "src/main.rs", "fn main() {}\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: contexts\n\
             \x20   type: context_boundary\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert!(
            report.issues[0]
                .message
                .contains("каталог модели не найден"),
            "{:?}",
            report.issues[0]
        );
    }

    #[test]
    fn fitness_context_boundary_overlapping_roots_is_config_error() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(
            &repo,
            "model/CMP-001-a.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: A\nstatus: adopted\ncode_roots: [services/x]\n---\nA.\n",
        );
        write_file(
            &repo,
            "model/CMP-002-b.md",
            "---\nid: CMP-002\ntype: cmp\ntitle: B\nstatus: adopted\ncode_roots: [services/x/sub]\n---\nB.\n",
        );
        write_file(&repo, "services/x/sub/m.py", "pass\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: contexts\n\
             \x20   type: context_boundary\n",
        );
        let err = check(&repo, &constraints).unwrap_err();
        assert!(err.to_string().contains("code_roots пересекаются"), "{err}");
    }

    #[test]
    fn fitness_context_boundary_without_code_roots_is_issue() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(
            &repo,
            "model/CMP-001-a.md",
            "---\nid: CMP-001\ntype: cmp\ntitle: A\nstatus: adopted\n---\nA.\n",
        );
        write_file(&repo, "src/main.rs", "fn main() {}\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: contexts\n\
             \x20   type: context_boundary\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        assert!(
            report.issues[0].message.contains("нет CMP с code_roots"),
            "{:?}",
            report.issues[0]
        );
    }

    // --- M-1a: per-rule timing ------------------------------------------------

    #[test]
    fn fitness_report_carries_per_rule_durations() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(&repo, "src/main.rs", "fn main() {}\n");
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: has_main\n\
             \x20   type: must_contain\n\
             \x20   glob: '**/*.rs'\n\
             \x20   pattern: 'fn main'\n\
             \x20 - name: main_exists\n\
             \x20   type: file_exists\n\
             \x20   path: src/main.rs\n",
        );
        let report = check(&repo, &constraints).unwrap();
        // Длительность замеряется для КАЖДОГО правила (порядок — как в файле).
        let names: Vec<&str> = report.durations.iter().map(|d| d.rule.as_str()).collect();
        assert_eq!(names, ["has_main", "main_exists"], "{:?}", report.durations);
        // SDK-контракт v1: durations — аддитивный ключ рядом с каноническими.
        let v: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&report).unwrap()).unwrap();
        assert!(v.get("durations").is_some(), "аддитивный ключ durations");
        assert_eq!(v["durations"][0]["rule"], "has_main");
        assert!(v["durations"][0]["ms"].is_number());
        for key in ["repo", "passed", "summary", "issues"] {
            assert!(v.get(key).is_some(), "канонический ключ {key} на месте");
        }
        // Десериализация старого JSON без durations — serde default (обратная
        // совместимость для SDK-клиентов, читающих отчёт в структуру).
        let legacy: FitnessReport =
            serde_json::from_str(r#"{"repo":".","passed":true,"issues":[],"summary":"s"}"#)
                .unwrap();
        assert!(legacy.durations.is_empty());
    }

    // --- M-1b/C-3: rules_report ------------------------------------------------

    #[test]
    fn rules_report_sections_and_effort_hours() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: no-pan\n\
             \x20   type: must_not_contain\n\
             \x20   glob: 'src/**/*.py'\n\
             \x20   pattern: '\\b\\d{16}\\b'\n\
             \x20   owner: Иванов\n\
             \x20   expiry: '2999-01-01'\n\
             \x20   effort_hours: 4.5\n\
             \x20 - name: old-rule\n\
             \x20   type: file_exists\n\
             \x20   path: README.md\n\
             \x20   owner: Петров\n\
             \x20   expiry: '2020-01-01'\n\
             \x20   effort_hours: 2\n\
             \x20 - name: contracts-except-legacy\n\
             \x20   type: dir_must_have_file\n\
             \x20   glob: 'services/*'\n\
             \x20   path: openapi.yaml\n\
             \x20   exclude_glob: ['services/a', 'services/b']\n\
             \x20   severity: warn\n\
             \x20 - name: bare-rule\n\
             \x20   type: command_succeeds\n\
             \x20   command: 'true'\n",
        );
        let report = rules_report(&repo, &constraints).unwrap();
        // Сводка.
        assert!(report.contains("Всего правил: 4"), "{report}");
        assert!(report.contains("must_not_contain 1"), "{report}");
        assert!(report.contains("По severity: error 3, warn 1"), "{report}");
        // Таблица карточек.
        assert!(
            report
                .contains("| no-pan | must_not_contain | error | Иванов | 2999-01-01 | 0 | 4.5 |"),
            "{report}"
        );
        assert!(
            report.contains(
                "| contracts-except-legacy | dir_must_have_file | warn | — | — | 2 | — |"
            ),
            "{report}"
        );
        // Находки: без owner/expiry (нет обоих полей у двух правил).
        let no_meta = report.split("### Правила без owner/expiry").nth(1).unwrap();
        let no_meta = no_meta.split("###").next().unwrap();
        assert!(
            no_meta.contains("contracts-except-legacy (нет owner, expiry)"),
            "{no_meta}"
        );
        assert!(
            no_meta.contains("bare-rule (нет owner, expiry)"),
            "{no_meta}"
        );
        assert!(
            !no_meta.contains("no-pan"),
            "owner+expiry заданы: {no_meta}"
        );
        // Просроченные — та же логика даты, что в run_rule.
        let expired = report.split("### Просроченные правила").nth(1).unwrap();
        let expired = expired.split("###").next().unwrap();
        assert!(
            expired.contains("old-rule — expiry 2020-01-01 (владелец: Петров)"),
            "{expired}"
        );
        assert!(
            !expired.contains("no-pan"),
            "дата в будущем — не просрочено: {expired}"
        );
        // exclude_glob — прокси отступлений.
        assert!(
            report.contains("- contracts-except-legacy: services/a, services/b"),
            "{report}"
        );
        // Без git-репозитория — ветка «недоступно», не ошибка.
        assert!(report.contains("недоступно"), "{report}");
        // Итоговая строка: effort_hours парсится и суммируется.
        assert!(
            report.contains("правил 4, суммарный effort_hours 6.5 (покрыто 2 правил)"),
            "{report}"
        );
    }

    // --- S-1: anti-bypass floor (триггеры из диффа) -----------------------------

    /// git в каталоге с тестовой идентичностью коммиттера (изоляция AD-7).
    fn git_in(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(args)
            .env("GIT_AUTHOR_NAME", "t")
            .env("GIT_AUTHOR_EMAIL", "t@t")
            .env("GIT_COMMITTER_NAME", "t")
            .env("GIT_COMMITTER_EMAIL", "t@t")
            .output()
            .expect("git");
        assert!(
            out.status.success(),
            "git {}: {}",
            args.join(" "),
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).into_owned()
    }

    /// Git-репозиторий с baseline-коммитом.
    fn git_repo(dir: &Path) -> PathBuf {
        let repo = dir.join("repo");
        std::fs::create_dir_all(&repo).unwrap();
        git_in(&repo, &["init", "-q", "-b", "main"]);
        write_file(&repo, "README.md", "base\n");
        write_file(&repo, "Cargo.toml", "[package]\nname = \"x\"\n");
        git_in(&repo, &["add", "."]);
        git_in(&repo, &["commit", "-q", "-m", "baseline"]);
        repo
    }

    #[test]
    fn diff_detectors_fire_on_committed_changes() {
        let dir = tempfile::tempdir().unwrap();
        let repo = git_repo(dir.path());
        // Новый компонент (манифест + src/), новый контракт, миграция с
        // DROP TABLE, конфиг со строкой подключения, зависимость в манифесте.
        write_file(
            &repo,
            "billing/Cargo.toml",
            "[package]\nname = \"billing\"\n",
        );
        write_file(&repo, "billing/src/main.rs", "fn main() {}\n");
        write_file(&repo, "api/OpenAPI.yaml", "openapi: 3.0.0\n");
        write_file(
            &repo,
            "migrations/001_init.sql",
            "CREATE TABLE t (id int);\n",
        );
        write_file(
            &repo,
            "migrations/002_drop.sql",
            "ALTER TABLE t ADD COLUMN x int;\nDROP TABLE t;\n",
        );
        write_file(&repo, "config/app.yaml", "db: postgres://localhost/x\n");
        let cargo = std::fs::read_to_string(repo.join("Cargo.toml")).unwrap();
        write_file(
            &repo,
            "Cargo.toml",
            &format!("{cargo}\n[dependencies]\nserde = \"1.0\"\n"),
        );
        git_in(&repo, &["add", "."]);
        git_in(&repo, &["commit", "-q", "-m", "feature"]);

        let found = detect_diff_triggers(&repo, Some("HEAD~1")).unwrap();
        for t in [
            "new_component",
            "new_vendor",
            "api_contract_change",
            "irreversible_migration",
            "new_datastore",
        ] {
            assert!(found.triggers.contains(t), "нет {t}: {:?}", found.triggers);
        }
        assert_eq!(found.triggers.len(), 5, "{:?}", found.triggers);
        assert_eq!(found.evidence.len(), 5, "{:?}", found.evidence);

        // Чистый дифф (HEAD против самого себя) — триггеров нет.
        let clean = detect_diff_triggers(&repo, None).unwrap();
        assert!(clean.triggers.is_empty(), "{:?}", clean.triggers);
    }

    #[test]
    fn diff_detector_worktree_mode_sees_untracked_new_component() {
        let dir = tempfile::tempdir().unwrap();
        let repo = git_repo(dir.path());
        // Без коммита: новый компонент — untracked-файлы рабочего дерева.
        write_file(&repo, "fraud/package.json", "{\"name\": \"fraud\"}\n");
        write_file(&repo, "fraud/src/index.js", "console.log(1);\n");
        let found = detect_diff_triggers(&repo, None).unwrap();
        assert!(
            found.triggers.contains("new_component"),
            "{:?}",
            found.triggers
        );
    }

    #[test]
    fn diff_detector_non_git_repo_is_control_error() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("plain");
        std::fs::create_dir_all(&repo).unwrap();
        let err = detect_diff_triggers(&repo, None).unwrap_err();
        assert!(err.to_string().contains("anti-bypass"), "{err}");
    }

    #[test]
    fn score_with_sources_unions_and_flags_undeclared() {
        let mut answers = BTreeMap::new();
        answers.insert("new_component".to_string(), true);
        answers.insert("new_vendor".to_string(), false);
        let mut diff = DiffTriggers::default();
        diff.fire("new_vendor", "зависимость в Cargo.toml: serde");
        diff.fire("new_component", "подтверждение диффом");

        let scored = score_with_sources(&answers, &diff, 1, 4);
        // Fail-safe: детектор добавил new_vendor поверх declared=false.
        assert_eq!(scored.significance.score, 2);
        assert_eq!(scored.significance.route, Route::Standard);
        assert_eq!(
            scored.sources["new_component"],
            TriggerSource::Both,
            "заявлен и подтверждён"
        );
        assert_eq!(scored.sources["new_vendor"], TriggerSource::Diff);
        // Расхождение — только new_vendor (заявлен false, найден диффом).
        assert_eq!(scored.undeclared, ["new_vendor"]);
        // Пороги параметризуются: тем же множеством при standard_max=1 — Critical.
        let strict = score_with_sources(&answers, &diff, 0, 1);
        assert_eq!(strict.significance.route, Route::Critical);
    }

    // --- Сенсор гипотез: роутинг по фактам проекта (карточки → routing.json) -----

    /// Пишет таблицу роутинга гипотез в `hypotheses/routing.json` tempdir'а
    /// и возвращает путь к файлу (как `$HYPOTHESES_DIR/routing.json`).
    fn write_routing(dir: &Path, rows: &str) -> PathBuf {
        write_file(
            dir,
            "hypotheses/routing.json",
            &format!("{{\"min_score\": 2, \"rows\": [{rows}]}}"),
        )
    }

    #[test]
    fn hypothesis_routing_path_handles_env_value() {
        // Сенсор выключен без HYPOTHESES_DIR (None) и на пустом значении.
        assert_eq!(hypothesis_routing_path(None), None);
        assert_eq!(hypothesis_routing_path(Some("")), None);
        assert_eq!(hypothesis_routing_path(Some("   ")), None);
        assert_eq!(
            hypothesis_routing_path(Some("/home/user/hypotheses")),
            Some(PathBuf::from("/home/user/hypotheses/routing.json"))
        );
        // Ведущий `~/` разворачивается от $HOME (шелл значение env не
        // раскрывает), хвост пути сохраняется.
        let tilde = hypothesis_routing_path(Some("~/hyp")).expect("путь из ~/");
        assert!(tilde.ends_with("hyp/routing.json"), "{tilde:?}");
        // Тонкая обёртка читает ту же переменную и повторяет разбор ровно.
        match std::env::var("HYPOTHESES_DIR") {
            Err(_) => assert_eq!(hypothesis_routing_path_from_env(), None),
            Ok(v) => assert_eq!(
                hypothesis_routing_path_from_env(),
                hypothesis_routing_path(Some(&v))
            ),
        }
    }

    #[test]
    fn hypothesis_sensor_off_when_routing_file_absent() {
        let dir = tempfile::tempdir().unwrap();
        let repo = git_repo(dir.path());
        write_file(&repo, "rollouts_log.jsonl", "{}\n");
        // Ядро без таблицы (HYPOTHESES_DIR не задан) — сенсор выключен.
        let off = detect_diff_triggers_with_routing(&repo, None, None).unwrap();
        assert!(
            !off.triggers.contains(HYPOTHESIS_HIT_TRIGGER),
            "{:?}",
            off.triggers
        );
        // Путь передан, но файла нет — тоже молча выключен.
        let missing = dir.path().join("nope/routing.json");
        let gone = detect_diff_triggers_with_routing(&repo, None, Some(&missing)).unwrap();
        assert!(
            !gone.triggers.contains(HYPOTHESIS_HIT_TRIGGER),
            "{:?}",
            gone.triggers
        );
    }

    #[test]
    fn hypothesis_sensor_fires_on_files_trigger() {
        let dir = tempfile::tempdir().unwrap();
        let repo = git_repo(dir.path());
        write_file(&repo, "exp/rollouts_log.jsonl", "{}\n");
        // Родной формат строки routing.json (`card`/`state`/`skills` — как
        // их пишет card_match.py --emit-routing); триггер — литерал basename.
        let routing = write_routing(
            dir.path(),
            r#"{"trigger": "rollouts_log.jsonl", "kind": "files", "weight": 3,
                "card": "laguna-gb10-ladder", "state": "latent",
                "skills": ["laguna-ladder-run", "ood-stage-eval"]}"#,
        );
        let found = detect_diff_triggers_with_routing(&repo, None, Some(&routing)).unwrap();
        assert!(
            found.triggers.contains(HYPOTHESIS_HIT_TRIGGER),
            "{:?}",
            found.triggers
        );
        assert!(
            found
                .evidence
                .iter()
                .any(|e| e.contains(HYPOTHESIS_HIT_TRIGGER)),
            "триггер виден в отчёте: {:?}",
            found.evidence
        );
        // +1 к счёту; сам по себе маршрут не Critical (Fast при score 1).
        let scored = score_with_sources(
            &BTreeMap::new(),
            &found,
            DEFAULT_FAST_MAX,
            DEFAULT_STANDARD_MAX,
        );
        assert_eq!(scored.significance.fired, [HYPOTHESIS_HIT_TRIGGER]);
        assert_eq!(scored.significance.score, 1);
        assert_eq!(
            scored.significance.route,
            Route::Fast,
            "не форсирует Critical"
        );
        assert_eq!(scored.sources[HYPOTHESIS_HIT_TRIGGER], TriggerSource::Diff);
    }

    #[test]
    fn hypothesis_sensor_ignores_non_file_kinds() {
        let dir = tempfile::tempdir().unwrap();
        let repo = git_repo(dir.path());
        write_file(&repo, "rollouts_log.jsonl", "{}\n");
        // Тот же файл совпал бы, будь строки kind: words/keys/deps — сенсор
        // значимости читает только файловые факты.
        let routing = write_routing(
            dir.path(),
            r#"{"trigger": "rollouts*", "kind": "words", "weight": 1, "card": "c1", "state": "latent", "skills": []},
               {"trigger": "rollouts_log.jsonl", "kind": "keys", "weight": 3, "card": "c1", "state": "latent", "skills": []},
               {"trigger": "rollouts_log.jsonl", "kind": "deps", "weight": 2, "card": "c1", "state": "latent", "skills": []}"#,
        );
        let found = detect_diff_triggers_with_routing(&repo, None, Some(&routing)).unwrap();
        assert!(
            !found.triggers.contains(HYPOTHESIS_HIT_TRIGGER),
            "{:?}",
            found.triggers
        );
    }

    #[test]
    fn hypothesis_sensor_counts_once_for_multiple_matches() {
        let dir = tempfile::tempdir().unwrap();
        let repo = git_repo(dir.path());
        write_file(&repo, "runs/rollouts_log.jsonl", "{}\n");
        write_file(&repo, "archive/rollouts_log.jsonl", "{}\n");
        // Две маски (basename-glob и glob по относительному пути) совпали —
        // триггер уникален, счёт растёт ровно на 1.
        let routing = write_routing(
            dir.path(),
            r#"{"trigger": "*.jsonl", "kind": "files", "weight": 3, "card": "a", "state": "latent", "skills": []},
               {"trigger": "runs/*.jsonl", "kind": "files", "weight": 3, "card": "b", "state": "latent", "skills": []}"#,
        );
        let found = detect_diff_triggers_with_routing(&repo, None, Some(&routing)).unwrap();
        assert_eq!(
            found
                .triggers
                .iter()
                .filter(|t| *t == HYPOTHESIS_HIT_TRIGGER)
                .count(),
            1,
            "уникальный триггер: {:?}",
            found.triggers
        );
        let scored = score_with_sources(
            &BTreeMap::new(),
            &found,
            DEFAULT_FAST_MAX,
            DEFAULT_STANDARD_MAX,
        );
        assert_eq!(scored.significance.score, 1, "ровно +1 при N совпадениях");
        assert_eq!(scored.significance.route, Route::Fast);
    }

    // --- S-2: пороги значимости (ADR-034) ---------------------------------------

    #[test]
    fn significance_with_limits_defaults_match_legacy_behavior() {
        // Дефолты 1/4 воспроизводят исторические маршруты 0–1/2–4/5+.
        for (count, want) in [
            (0, Route::Fast),
            (1, Route::Fast),
            (2, Route::Standard),
            (4, Route::Standard),
            (5, Route::Critical),
        ] {
            let answers: BTreeMap<String, bool> =
                (0..count).map(|i| (format!("t{i}"), true)).collect();
            assert_eq!(
                significance_score(&answers).route,
                significance_score_with_limits(&answers, DEFAULT_FAST_MAX, DEFAULT_STANDARD_MAX)
                    .route,
                "дефолты = старое поведение при score={count}"
            );
            let legacy = match count {
                0 | 1 => Route::Fast,
                2..=4 => Route::Standard,
                _ => Route::Critical,
            };
            assert_eq!(want, legacy, "таблица теста самосогласована");
            assert_eq!(significance_score(&answers).route, want);
        }
    }

    #[test]
    fn significance_with_limits_custom_thresholds_change_route() {
        let answers: BTreeMap<String, bool> = (0..3).map(|i| (format!("t{i}"), true)).collect();
        // fast_max=2, standard_max=5: те же 3 триггера — Standard, не Fast.
        assert_eq!(
            significance_score_with_limits(&answers, 2, 5).route,
            Route::Standard
        );
        assert_eq!(
            significance_score_with_limits(&answers, 3, 5).route,
            Route::Fast
        );
        assert_eq!(
            significance_score_with_limits(&answers, 0, 2).route,
            Route::Critical
        );
    }

    #[test]
    fn significance_forcing_critical_triggers_not_configurable() {
        // security_boundary_change форсирует Critical при ЛЮБЫХ порогах
        // (fail-safe): «откалибровать вниз» его нельзя.
        let mut answers = BTreeMap::new();
        answers.insert("security_boundary_change".to_string(), true);
        assert_eq!(
            significance_score_with_limits(&answers, 10, 20).route,
            Route::Critical
        );
    }

    #[tokio::test]
    async fn significance_tool_uses_config_thresholds() {
        let dir = tempfile::tempdir().unwrap();
        let mut cfg = crate::config::Config::default();
        cfg.significance.fast_max = 2;
        cfg.significance.standard_max = 5;
        let ctx = ToolContext::new(dir.path().to_path_buf(), Arc::new(cfg));
        let tool = SignificanceScoreTool;
        // 2 триггера при fast_max=2 — Fast (при дефолте был бы Standard).
        let out = tool
            .call(
                json!({"triggers": {"new_component": true, "new_vendor": true}}),
                &ctx,
            )
            .await
            .unwrap();
        assert!(!out.is_error, "{:?}", out.content);
        assert!(out.content.contains("Fast"), "{}", out.content);

        // Невалидные пороги — ошибка инструмента с понятным текстом.
        let mut cfg = crate::config::Config::default();
        cfg.significance.fast_max = 5;
        cfg.significance.standard_max = 5;
        let ctx = ToolContext::new(dir.path().to_path_buf(), Arc::new(cfg));
        let out = tool
            .call(json!({"triggers": {"new_component": true}}), &ctx)
            .await
            .unwrap();
        assert!(out.is_error, "{:?}", out.content);
        assert!(out.content.contains("fast_max"), "{}", out.content);
    }

    // --- Корп-спайн: extends / deny_dependency / severity / overrides / report ---

    /// Корп-родитель для фикстур наследования (docs/corp-spine.md).
    const CORP_YAML: &str = "version: \"2026.3\"\n\
        rules:\n\
        \x20 - id: C-CORP-001\n\
        \x20   name: no_hold_crates\n\
        \x20   type: deny_dependency\n\
        \x20   deny: [left-pad, openssl-sys]\n\
        \x20   reference: \"Техкомитет 2026-03, протокол №12\"\n\
        \x20   severity: block\n\
        \x20 - id: C-CORP-002\n\
        \x20   name: corp_readme_present\n\
        \x20   type: file_exists\n\
        \x20   path: \"README.md\"\n\
        \x20   severity: warn\n";

    /// Пишет корп-родителя и продуктовый файл с extends в tempdir;
    /// возвращает (tempdir, путь к продуктовому CONSTRAINTS.yaml).
    fn extends_fixture(corp_version: &str, pin: &str) -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        write_file(
            dir.path(),
            "corp/CONSTRAINTS.corp.yaml",
            &CORP_YAML.replace("2026.3", corp_version),
        );
        let product = write_file(
            dir.path(),
            "product/CONSTRAINTS.yaml",
            &format!(
                "extends: [\"../corp/CONSTRAINTS.corp.yaml@{pin}\"]\n\
                 rules:\n\
                 \x20 - name: own_rule\n\
                 \x20   type: must_contain\n\
                 \x20   glob: \"marker.txt\"\n\
                 \x20   pattern: \"ok\"\n"
            ),
        );
        write_file(dir.path(), "product/marker.txt", "ok\n");
        write_file(dir.path(), "product/README.md", "# product\n");
        (dir, product)
    }

    #[test]
    fn extends_inherits_rules_with_source_label() {
        let (dir, product) = extends_fixture("2026.3", "2026.3");
        let resolved = load_constraints_resolved(&product).unwrap();
        assert_eq!(resolved.rules.len(), 3);
        assert_eq!(resolved.findings.len(), 0, "{:?}", resolved.findings);
        let inherited: Vec<&FitnessRule> = resolved
            .rules
            .iter()
            .filter(|r| r.source.is_some())
            .collect();
        assert_eq!(inherited.len(), 2);
        assert_eq!(
            inherited[0].source.as_deref(),
            Some("CONSTRAINTS.corp@2026.3")
        );
        assert!(resolved.rules.iter().any(|r| r.source.is_none()));

        let report = check(&dir.path().join("product"), &product).unwrap();
        assert!(report.passed, "{:?}", report.issues);
        assert_eq!(report.inherited.len(), 1);
        assert_eq!(report.inherited[0].source, "CONSTRAINTS.corp@2026.3");
        assert_eq!(report.inherited[0].rules, 2);
    }

    #[test]
    fn extends_version_mismatch_is_error_finding() {
        let (dir, product) = extends_fixture("2026.4", "2026.3");
        let report = check(&dir.path().join("product"), &product).unwrap();
        assert!(!report.passed, "расхождение пина ломает гейт");
        let finding = report
            .issues
            .iter()
            .find(|i| i.rule == "extends")
            .expect("находка extends");
        assert_eq!(finding.severity, "error");
        assert!(
            finding.message.contains("2026.3 → 2026.4"),
            "{}",
            finding.message
        );
    }

    #[test]
    fn extends_cycle_is_error() {
        let dir = tempfile::tempdir().unwrap();
        write_file(
            dir.path(),
            "a.yaml",
            "version: \"1\"\nextends: [\"b.yaml@1\"]\nrules: []\n",
        );
        write_file(
            dir.path(),
            "b.yaml",
            "version: \"1\"\nextends: [\"a.yaml@1\"]\nrules: []\n",
        );
        let err = load_constraints_resolved(&dir.path().join("a.yaml")).unwrap_err();
        assert!(err.to_string().contains("цикл"), "{err}");
    }

    #[test]
    fn extends_without_pin_is_error() {
        let dir = tempfile::tempdir().unwrap();
        let file = write_file(
            dir.path(),
            "p.yaml",
            "extends: [\"corp.yaml@\"]\nrules: []\n",
        );
        let err = load_constraints_resolved(&file).unwrap_err();
        assert!(err.to_string().contains("<ref>@<version>"), "{err}");
    }

    #[test]
    fn deny_dependency_hits_all_manifest_formats() {
        let dir = tempfile::tempdir().unwrap();
        write_file(
            dir.path(),
            "Cargo.toml",
            "[package]\nname = \"x\"\n\n[dependencies]\nserde = \"1\"\nleft-pad = \"0.1\"\n\n[dev-dependencies]\n# comment\ntempfile = \"3\"\n",
        );
        write_file(
            dir.path(),
            "service/pom.xml",
            "<project>\n  <artifactId>my-service</artifactId>\n  <dependency>\n    <artifactId>openssl-sys</artifactId>\n  </dependency>\n</project>\n",
        );
        write_file(
            dir.path(),
            "ml/requirements.txt",
            "# комментарий\nrequests>=2.0\nleft-pad == 1.0  # pinned\n-r base.txt\n",
        );
        let constraints = write_file(dir.path(), "CONSTRAINTS.yaml", CORP_YAML);
        let report = check(dir.path(), &constraints).unwrap();
        assert!(!report.passed);
        let deny: Vec<&LintIssue> = report
            .issues
            .iter()
            .filter(|i| i.rule == "no_hold_crates")
            .collect();
        // Cargo.toml left-pad, pom openssl-sys, requirements left-pad.
        assert_eq!(deny.len(), 3, "{deny:?}");
        assert!(
            deny.iter()
                .all(|i| i.message.contains("Техкомитет 2026-03"))
        );
        assert!(
            deny.iter()
                .any(|i| i.file == Path::new("Cargo.toml") && i.line == 6)
        );
        assert!(deny.iter().any(|i| i.file == Path::new("service/pom.xml")));
        assert!(
            deny.iter()
                .any(|i| i.file == Path::new("ml/requirements.txt") && i.line == 3)
        );
        // serde/requests не в deny — находок нет.
        assert!(!deny.iter().any(|i| i.message.contains("serde")));
    }

    #[test]
    fn deny_dependency_empty_deny_is_config_error() {
        let dir = tempfile::tempdir().unwrap();
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n  - name: bad\n    type: deny_dependency\n    deny: []\n",
        );
        let err = check(dir.path(), &constraints).unwrap_err();
        assert!(err.to_string().contains("deny"), "{err}");
    }

    #[test]
    fn severity_warn_rule_does_not_break_gate() {
        let dir = tempfile::tempdir().unwrap();
        // C-CORP-002 (file_exists README.md, severity warn) не находит README —
        // warn-находка, гейт PASS; deny-правилу нечего найти (манифестов нет).
        let constraints = write_file(dir.path(), "CONSTRAINTS.yaml", CORP_YAML);
        let report = check(dir.path(), &constraints).unwrap();
        assert!(report.passed, "{:?}", report.issues);
        let warn = report
            .issues
            .iter()
            .find(|i| i.rule == "corp_readme_present")
            .expect("warn-находка");
        assert_eq!(warn.severity, "warn");
        assert_eq!(report.summary.matches("error: 0").count(), 1);
    }

    /// Продуктовый файл с override-фикстурой (тело overrides — параметр).
    fn override_fixture(overrides_yaml: &str) -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        write_file(dir.path(), "corp/CONSTRAINTS.corp.yaml", CORP_YAML);
        write_file(dir.path(), "product/README.md", "# product\n");
        let product = write_file(
            dir.path(),
            "product/CONSTRAINTS.yaml",
            &format!(
                "extends: [\"../corp/CONSTRAINTS.corp.yaml@2026.3\"]\n\
                 overrides:\n{overrides_yaml}\n\
                 rules:\n\
                 \x20 - name: own_rule\n\
                 \x20   type: file_exists\n\
                 \x20   path: \"README.md\"\n"
            ),
        );
        (dir, product)
    }

    #[test]
    fn override_active_disables_rule() {
        // Cargo.toml содержит deny-пакет, но правило отключено активным override.
        let (dir, product) =
            override_fixture("  - rule: C-CORP-001\n    adr: ADR-041\n    until: \"2999-01\"\n");
        write_file(
            dir.path(),
            "product/Cargo.toml",
            "[dependencies]\nleft-pad = \"0.1\"\n",
        );
        let report = check(&dir.path().join("product"), &product).unwrap();
        assert!(report.passed, "{:?}", report.issues);
        assert!(!report.issues.iter().any(|i| i.rule == "no_hold_crates"));
        let info = &report.overrides[0];
        assert_eq!(info.status, "active");
        assert_eq!(info.rule, "C-CORP-001");
    }

    #[test]
    fn override_incomplete_is_error_and_rule_stays() {
        let (dir, product) = override_fixture("  - rule: C-CORP-001\n    until: \"2999-01\"\n");
        write_file(
            dir.path(),
            "product/Cargo.toml",
            "[dependencies]\nleft-pad = \"0.1\"\n",
        );
        let report = check(&dir.path().join("product"), &product).unwrap();
        assert!(!report.passed);
        // Неполный override — error-находка, правило НЕ отключено (deny-hit есть).
        assert!(
            report
                .issues
                .iter()
                .any(|i| i.rule == "override" && i.severity == "error")
        );
        assert!(report.issues.iter().any(|i| i.rule == "no_hold_crates"));
        assert_eq!(report.overrides[0].status, "invalid");
    }

    #[test]
    fn override_expired_warns_and_rule_applies_again() {
        let (dir, product) =
            override_fixture("  - rule: C-CORP-001\n    adr: ADR-041\n    until: \"2020-01\"\n");
        write_file(
            dir.path(),
            "product/Cargo.toml",
            "[dependencies]\nleft-pad = \"0.1\"\n",
        );
        let report = check(&dir.path().join("product"), &product).unwrap();
        assert!(!report.passed, "правило снова действует → deny-hit");
        assert!(report.issues.iter().any(|i| i.rule == "no_hold_crates"));
        let expired = report
            .issues
            .iter()
            .find(|i| i.rule == "override" && i.message.contains("истёк"))
            .expect("warn «override истёк»");
        assert_eq!(expired.severity, "warn");
        assert_eq!(report.overrides[0].status, "expired");
    }

    #[test]
    fn override_unknown_rule_is_warn() {
        let (dir, product) =
            override_fixture("  - rule: C-CORP-999\n    adr: ADR-041\n    until: \"2999-01\"\n");
        let report = check(&dir.path().join("product"), &product).unwrap();
        assert!(report.passed, "warn не ломает гейт: {:?}", report.issues);
        assert!(report.issues.iter().any(|i| i.rule == "override"
            && i.severity == "warn"
            && i.message.contains("несуществующее правило")));
        assert_eq!(report.overrides[0].status, "invalid");
    }

    #[test]
    fn control_report_json_shape_corp_level() {
        let (dir, product) =
            override_fixture("  - rule: C-CORP-001\n    adr: ADR-041\n    until: \"2999-01\"\n");
        let product_dir = dir.path().join("product");
        let report = control_report(&product_dir, &product, "corp").unwrap();
        assert_eq!(report.rules_total, 2, "только унаследованные");
        assert_eq!(report.own, 0);
        assert_eq!(report.inherited.get("CONSTRAINTS.corp@2026.3"), Some(&2));
        // C-CORP-001 отключён override'ом (находок нет → pass), C-CORP-002 —
        // README на месте → pass.
        assert_eq!(report.pass, 2);
        assert_eq!(report.fail, 0);
        assert_eq!(report.overrides.len(), 1);
        assert_eq!(report.overrides[0].status, "active");
        assert!(report.expired_rules.is_empty());
        assert!(report.version_mismatches.is_empty());
        assert!(report.passed);
        // JSON-контракт: ключевые поля сериализуются.
        let json = serde_json::to_value(&report).unwrap();
        for key in [
            "rules_total",
            "inherited",
            "pass",
            "fail",
            "warn",
            "overrides",
            "expired_rules",
        ] {
            assert!(json.get(key).is_some(), "нет поля {key}");
        }

        let all = control_report(&product_dir, &product, "all").unwrap();
        assert_eq!(all.rules_total, 3);
        assert_eq!(all.own, 1);
    }

    #[test]
    fn control_report_rejects_unknown_level() {
        let dir = tempfile::tempdir().unwrap();
        let constraints = write_file(dir.path(), "CONSTRAINTS.yaml", CORP_YAML);
        let err = control_report(dir.path(), &constraints, "domain").unwrap_err();
        assert!(err.to_string().contains("corp"), "{err}");
    }

    #[test]
    fn unverifiable_rule_parses_without_type_and_is_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - id: C-CORP-900\n\
             \x20   name: manual_observability\n\
             \x20   unverifiable: true\n\
             \x20   owner: \"@corp-arch\"\n\
             \x20 - name: own_rule\n\
             \x20   type: file_exists\n\
             \x20   path: \"README.md\"\n",
        );
        // Правило без type парсится, не исполняется; гейт — по own_rule (fail:
        // README нет), unverifiable не даёт находок.
        let report = check(dir.path(), &constraints).unwrap();
        assert!(!report.passed);
        assert_eq!(report.durations.len(), 1, "исполнено только own_rule");
        assert!(
            !report
                .issues
                .iter()
                .any(|i| i.rule == "manual_observability")
        );

        let corp = control_report(dir.path(), &constraints, "all").unwrap();
        assert_eq!(corp.unverifiable_rules, vec!["manual_observability"]);
        assert_eq!(corp.fail, 1, "только own_rule в исходах");
        assert_eq!(corp.pass, 0);
    }

    /// Валидный по контракту SKILL.md: frontmatter + триггер + методика.
    fn full_skill_text() -> String {
        "---\n\
         name: saga-staging\n\
         description: Поэтапное внедрение саги. Используй этот навык при миграции транзакций.\n\
         ---\n\n\
         # Поэтапное внедрение саги\n\n\
         ## Когда применять\n\
         - распределённая транзакция через несколько сервисов\n\n\
         ## Методика\n\
         1. Выделить шаги саги и компенсации.\n\
         2. Описать оркестрацию и идемпотентность.\n\
         3. Внедрить и проверить в staging.\n"
            .to_string()
    }

    #[test]
    fn skill_contract_accepts_full_skill() {
        assert_eq!(
            skill_contract_violations(&full_skill_text()),
            Vec::<String>::new()
        );
    }

    #[test]
    fn skill_contract_flags_missing_trigger() {
        let text = "---\n\
             name: saga-staging\n\
             description: Поэтапное внедрение саги в событийный контур.\n\
             ---\n\n\
             # Поэтапное внедрение саги\n\n\
             ## Когда применять\n\
             - миграция транзакций\n\n\
             ## Методика\n\
             1. Шаг.\n\
             2. Шаг.\n\
             3. Шаг.\n";
        let v = skill_contract_violations(text);
        assert!(v.iter().any(|m| m.contains("триггер-маркер")), "{v:?}");
    }

    #[test]
    fn skill_contract_flags_thin_procedure() {
        let text = "---\n\
             name: saga-staging\n\
             description: Внедрение саги. Используй этот навык при миграции.\n\
             ---\n\n\
             # Внедрение саги\n\n\
             ## Когда применять\n\
             - миграция транзакций\n\n\
             ## Методика\n\
             1. Сделать шаг.\n";
        let v = skill_contract_violations(text);
        assert!(v.iter().any(|m| m.contains("процедуры")), "{v:?}");
    }

    #[test]
    fn skill_contract_flags_no_frontmatter() {
        let v = skill_contract_violations("# Просто заметка\n\nбез frontmatter\n");
        assert!(v.iter().any(|m| m.contains("frontmatter")), "{v:?}");
    }

    #[test]
    fn skill_contract_rule_reports_on_repo() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(
            &repo,
            "aiml/plugins/ops/skills/routing/SKILL.md",
            &full_skill_text(),
        );
        write_file(
            &repo,
            "aiml/plugins/ops/skills/broken/SKILL.md",
            "# Без frontmatter\n",
        );
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: skill_has_trigger_and_procedure\n\
             \x20   type: skill_contract\n\
             \x20   glob: 'aiml/plugins/*/skills/*/SKILL.md'\n\
             \x20 - name: skill_glob_miss\n\
             \x20   type: skill_contract\n\
             \x20   glob: 'no-such/*/SKILL.md'\n",
        );
        let report = check(&repo, &constraints).unwrap();
        assert!(!report.passed);
        let by_rule = |name: &str| {
            report
                .issues
                .iter()
                .filter(|i| i.rule == name)
                .collect::<Vec<_>>()
        };
        let contract = by_rule("skill_has_trigger_and_procedure");
        assert_eq!(contract.len(), 1, "только broken: {contract:?}");
        assert!(contract[0].file.ends_with("skills/broken/SKILL.md"));
        assert_eq!(
            by_rule("skill_glob_miss").len(),
            1,
            "пустой набор — находка"
        );
    }

    /// Playbook-текст: паспорт задаётся строкой frontmatter, тело — `steps`
    /// шагов методики (общая обвязка триггера одинакова).
    fn playbook_with(frontmatter: &str, steps: usize) -> String {
        use std::fmt::Write as _;
        let mut steps_text = String::new();
        // Запись в String не может завершиться ошибкой — игнор безопасен.
        for i in 1..=steps {
            let _ = writeln!(steps_text, "{i}. Шаг {i}.");
        }
        format!(
            "---\n{frontmatter}---\n\n\
             # Saga staging\n\n\
             ## Когда применять\n\
             - распределённая транзакция\n\n\
             ## Методика\n{steps_text}"
        )
    }

    /// Зрелый паспорт: три повторения, кандидат, журнал стабильности.
    fn ripe_frontmatter() -> &'static str {
        "type: playbook\n\
         status: draft\n\
         name: saga-staging\n\
         description: Поэтапное внедрение саги.\n\
         uses: 3\n\
         skill_candidate: true\n\
         steps_sha256: deadbeef\n"
    }

    #[test]
    fn playbook_graduation_accepts_ripe() {
        let text = playbook_with(ripe_frontmatter(), 3);
        assert_eq!(playbook_graduation_violations(&text), Vec::<String>::new());
    }

    #[test]
    fn playbook_graduation_flags_thin_uses() {
        let fm = "type: playbook\nuses: 2\nskill_candidate: true\nsteps_sha256: deadbeef\n";
        let v = playbook_graduation_violations(&playbook_with(fm, 3));
        assert!(v.iter().any(|m| m.contains("трижды")), "{v:?}");
    }

    #[test]
    fn playbook_graduation_flags_no_candidate() {
        let fm = "type: playbook\nuses: 3\nskill_candidate: false\nsteps_sha256: deadbeef\n";
        let v = playbook_graduation_violations(&playbook_with(fm, 3));
        assert!(v.iter().any(|m| m.contains("skill_candidate")), "{v:?}");
    }

    #[test]
    fn playbook_graduation_flags_missing_hash() {
        let fm = "type: playbook\nuses: 3\nskill_candidate: true\n";
        let v = playbook_graduation_violations(&playbook_with(fm, 3));
        assert!(v.iter().any(|m| m.contains("журнала")), "{v:?}");
    }

    #[test]
    fn playbook_graduation_flags_thin_metodika() {
        let v = playbook_graduation_violations(&playbook_with(ripe_frontmatter(), 1));
        assert!(v.iter().any(|m| m.contains("менее 3 шагов")), "{v:?}");
    }

    #[test]
    fn playbook_graduation_flags_wrong_type() {
        let fm = "type: digest\nuses: 3\nskill_candidate: true\nsteps_sha256: deadbeef\n";
        let v = playbook_graduation_violations(&playbook_with(fm, 3));
        assert!(v.iter().any(|m| m.contains("вместо")), "{v:?}");
    }

    #[test]
    fn playbook_graduation_rule_reports_on_repo() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        write_file(
            &repo,
            "aiml/plugins/ops/skills/routing/SKILL.md",
            &full_skill_text(),
        );
        // Корректная градация: указатель ведёт на существующий скилл, uses ≥ 3.
        let good_fm = "type: playbook\nuses: 3\nskill_candidate: true\nsteps_sha256: deadbeef\n\
             graduated_to: aiml/plugins/ops/skills/routing/SKILL.md\n";
        write_file(
            &repo,
            "aiml/plugins/ops/distillate/playbooks/ripe.md",
            &playbook_with(good_fm, 3),
        );
        // Указатель в никуда.
        let void_fm = "type: playbook\nuses: 3\nskill_candidate: true\nsteps_sha256: deadbeef\n\
             graduated_to: aiml/plugins/ops/skills/ghost/SKILL.md\n";
        write_file(
            &repo,
            "aiml/plugins/ops/distillate/playbooks/hollow.md",
            &playbook_with(void_fm, 3),
        );
        // Градация ниже порога.
        let early_fm = "type: playbook\nuses: 2\nskill_candidate: true\nsteps_sha256: deadbeef\n\
             graduated_to: aiml/plugins/ops/skills/routing/SKILL.md\n";
        write_file(
            &repo,
            "aiml/plugins/ops/distillate/playbooks/early.md",
            &playbook_with(early_fm, 3),
        );
        let constraints = write_file(
            dir.path(),
            "CONSTRAINTS.yaml",
            "rules:\n\
             \x20 - name: playbook_graduates_into_existing_skill\n\
             \x20   type: playbook_graduation\n\
             \x20   glob: 'aiml/plugins/*/distillate/playbooks/*.md'\n\
             \x20 - name: playbook_glob_miss\n\
             \x20   type: playbook_graduation\n\
             \x20   glob: 'no-such/*/playbooks/*.md'\n",
        );
        let report = check(&repo, &constraints).unwrap();
        let by_rule = |name: &str| {
            report
                .issues
                .iter()
                .filter(|i| i.rule == name)
                .collect::<Vec<_>>()
        };
        let grad = by_rule("playbook_graduates_into_existing_skill");
        assert_eq!(grad.len(), 2, "hollow + early: {grad:?}");
        assert!(
            grad.iter().any(|i| i.message.contains("несуществующий")),
            "{grad:?}"
        );
        assert!(
            grad.iter()
                .any(|i| i.message.contains("без трёх повторений")),
            "{grad:?}"
        );
        assert_eq!(
            by_rule("playbook_glob_miss").len(),
            1,
            "пустой набор — находка"
        );
    }
}

#[cfg(test)]
mod address_trace_tests {
    use super::*;

    fn idx(spine: &[&str], adr: &[(u64, &str)], rules: &[&str]) -> AddressIndex {
        AddressIndex {
            spine: spine.iter().map(std::string::ToString::to_string).collect(),
            adr: adr.iter().map(|(n, f)| (*n, f.to_string())).collect(),
            rules: rules.iter().map(std::string::ToString::to_string).collect(),
        }
    }

    #[test]
    fn extracts_three_forms() {
        let text =
            "trace:\n  - ADR-014:rule-7\n  - spine:AD-3\n  - rule:ML-SKILL-1\nтело [ADR-014].";
        let addrs = extract_addresses(text);
        let keys: Vec<&str> = addrs.iter().map(|a| a.key.as_str()).collect();
        assert!(keys.contains(&"ADR-014"), "{keys:?}");
        assert!(keys.contains(&"AD-3"), "{keys:?}");
        assert!(keys.contains(&"ML-SKILL-1"), "{keys:?}");
    }

    #[test]
    fn resolves_and_reports_unresolved() {
        let index = idx(&["AD-3"], &[(14, "docs/adr/ADR-014.md")], &["ML-SKILL-1"]);
        assert!(
            address_trace_violations("ADR-014 [spine:AD-3] rule:ML-SKILL-1", &index, &[], None)
                .is_empty()
        );
        let v = address_trace_violations("ADR-014:rule-9", &index, &[], None);
        assert!(!v.is_empty(), "{v:?}");
        let v = address_trace_violations("нет адресов тут", &index, &[], None);
        assert!(v.iter().any(|s| s.contains("пустой след")), "{v:?}");
    }

    #[test]
    fn require_checks_coverage() {
        let index = idx(&["AD-3"], &[], &[]);
        assert!(
            address_trace_violations("spine:AD-3", &index, &["AD-3".to_string()], None).is_empty()
        );
        let v = address_trace_violations("spine:AD-3", &index, &["AD-9".to_string()], None);
        assert!(v.iter().any(|s| s.contains("AD-9 не покрыт")), "{v:?}");
    }

    #[test]
    fn pattern_filters_addresses() {
        let index = idx(&["AD-3"], &[], &["ML-SKILL-1"]);
        let v = address_trace_violations("rule:ML-SKILL-1", &index, &[], Some("^ADR-"));
        assert!(v.iter().any(|s| s.contains("пустой след")), "{v:?}");
    }
}

#[cfg(test)]
mod command_capture_tests {
    use super::*;

    #[test]
    fn run_with_timeout_captures_output_tail_on_failure() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let outcome = run_with_timeout(
            tmp.path(),
            "echo marker-строка-вывода; echo ошибка-в-стдошибку >&2; exit 3",
            Duration::from_secs(10),
        )
        .expect("прогон команды");
        assert_eq!(outcome.status.and_then(|s| s.code()), Some(3));
        assert!(outcome.tail.contains("marker-строка-вывода"));
        assert!(outcome.tail.contains("ошибка-в-стдошибку"));
    }

    #[test]
    fn report_tail_keeps_last_lines_and_redacts_secrets() {
        use std::fmt::Write as _;
        let mut raw = String::new();
        for i in 1..=20 {
            writeln!(raw, "строка {i}").expect("запись в String не падает");
        }
        raw.push_str("ключ DEEPSEEK_API_KEY=sk-0123456789abcdef0123456789 в тексте\n");
        let tail = report_tail(&raw);
        assert_eq!(tail.lines().count(), REPORT_TAIL_LINES);
        assert!(tail.contains("строка 20"));
        assert!(!tail.contains("строка 5"), "старые строки обрезаны: {tail}");
        assert!(
            !tail.contains("sk-0123456789abcdef0123456789"),
            "секрет обязан быть замаскирован: {tail}"
        );
    }

    #[test]
    fn drain_tail_bounds_memory_to_capture_limit() {
        let big = vec![b'x'; MAX_CAPTURE_BYTES * 3];
        let tail = drain_tail(&big[..]);
        assert_eq!(tail.len(), MAX_CAPTURE_BYTES);
    }
}
