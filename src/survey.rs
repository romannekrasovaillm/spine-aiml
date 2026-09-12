//! Обратное обследование legacy (reverse discovery): детерминированный
//! сканер репозитория → каркас карты обследования `docs/reverse/survey.md`.
//!
//! Методология — скилл `reverse-discovery` (9 артефактов обследования со
//! сроком годности, метки уверенности). Без LLM (AD-2): только glob/regex/
//! git — повторный прогон по неизменному репозиторию даёт тот же каркас.
//!
//! КОНТРАКТ (владелец: агент `control`):
//! - всё найденное — метка `[confirmed]` со ссылкой `файл:строка`;
//! - обязательная секция без находок — `[gap]` с вопросом владельцу домена;
//! - `survey.md` ПОЛНОСТЬЮ генерируемый: повторный прогон перезаписывает его
//!   целиком (merge не нужен); человеческие/LLM-дополнения `[inferred]` — в
//!   соседний `survey-notes.md`, который survey НИКОГДА не перезаписывает
//!   (при отсутствии создаёт заготовку со структурой 9 артефактов);
//! - интеграции выводятся как `host:port` без пути и БЕЗ userinfo
//!   (`user:pass@` срезается), localhost/127.0.0.1 пропускаются;
//! - frontmatter: `created_at`/`expires_at` (created + [`SURVEY_TTL_DAYS`]
//!   дней) — по истечении карту пересобирают (living-spec, не разовый аудит).

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_trait::async_trait;
use chrono::{Duration, NaiveDate};
use regex::Regex;
use serde_json::{Value, json};
use walkdir::WalkDir;

use crate::error::{HarnessError, Result};
use crate::llm::ToolSpec;
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Срок годности карты обследования, дней (`created_at` → `expires_at`).
const SURVEY_TTL_DAYS: i64 = 90;

/// Потолок размера файла для чтения содержимого (большие — пропускаем,
/// сканер regex-ориентирован, гигабайтные дампы не нужны).
const MAX_FILE_BYTES: u64 = 1024 * 1024;

/// Потолок находок одного сканера (секция не превращается в простыню).
const MAX_FINDINGS_PER_SCANNER: usize = 40;

/// Потолок ссылок-доказательств у одной находки.
const MAX_REFS_PER_FINDING: usize = 5;

/// Сколько первых строк CODEOWNERS показывать.
const MAX_CODEOWNERS_LINES: usize = 20;

/// Сколько каталогов верхнего уровня профилировать git shortlog.
const TOP_DIRS_FOR_OWNERS: usize = 5;

/// Сколько коммитеров показывать на каталог (git shortlog -sn).
const TOP_COMMITERS_PER_DIR: usize = 3;

/// Глубина git-истории для оценки «горячих» файлов (риски изменения).
const CHURN_COMMITS: usize = 500;

/// Сколько самых горячих файлов показывать в рисках.
const TOP_CHURN_FILES: usize = 5;

/// Служебные/производные каталоги, исключаемые из сканирования.
const SKIP_DIRS: [&str; 9] = [
    ".git",
    "target",
    "node_modules",
    ".arch-handoff",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".venv",
];

/// Расширения файлов, чьё содержимое сканируется regex-сканерами.
const SCANNABLE_EXTENSIONS: [&str; 22] = [
    "rs",
    "py",
    "java",
    "kt",
    "js",
    "ts",
    "jsx",
    "tsx",
    "go",
    "cs",
    "rb",
    "php",
    "sql",
    "sh",
    "bash",
    "yaml",
    "yml",
    "toml",
    "properties",
    "json",
    "xml",
    "gradle",
];

/// Расширения конфигов, из которых извлекаются интеграции (URL/host'ы).
const CONFIG_EXTENSIONS: [&str; 5] = ["yaml", "yml", "toml", "properties", "json"];

/// Имена каталогов миграций (включая «db/migrate» — по суффиксу пути).
const MIGRATION_DIR_NAMES: [&str; 4] = ["migrations", "flyway", "liquibase", "alembic"];

/// Имена каталогов тестов.
const TEST_DIR_NAMES: [&str; 4] = ["tests", "test", "__tests__", "spec"];

/// Имена каталогов файлового обмена (скрытые связи через drop-каталоги).
const EXCHANGE_DIR_NAMES: [&str; 7] = [
    "drop", "dropbox", "inbox", "outbox", "exchange", "incoming", "outgoing",
];

/// Хосты-петлевые: не интеграции, из карты исключаются.
const LOOPBACK_HOSTS: [&str; 4] = ["localhost", "127.0.0.1", "0.0.0.0", "::1"];

/// Одна находка сканера: текст + доказательства `файл:строка`.
#[derive(Debug, Clone)]
struct Finding {
    /// Текст находки (без метки — метку добавляет рендер).
    text: String,
    /// Ссылки-доказательства (`путь:строка` или `путь/` для каталогов).
    refs: Vec<String>,
}

impl Finding {
    /// Находка с одной ссылкой.
    fn one(text: impl Into<String>, reference: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            refs: vec![reference.into()],
        }
    }
}

/// Секция карты обследования (один из 9 артефактов скилла reverse-discovery).
#[derive(Debug, Clone)]
struct Section {
    /// Заголовок секции (как в скилле).
    title: &'static str,
    /// Находки сканеров ([confirmed]).
    findings: Vec<Finding>,
    /// Вопрос владельцу домена, если находок нет ([gap]).
    gap_question: &'static str,
}

/// Отчёт обследования: каркас карты с метками и сроком годности.
#[derive(Debug, Clone)]
pub struct SurveyReport {
    /// Имя каталога репозитория (для заголовка).
    pub repo_name: String,
    /// Дата сборки карты.
    pub created_at: NaiveDate,
    /// Дата истечения срока годности (`created_at` + [`SURVEY_TTL_DAYS`]).
    pub expires_at: NaiveDate,
    /// Секции по 9 артефактам скилла.
    sections: Vec<Section>,
}

impl SurveyReport {
    /// Число находок [confirmed] по всем секциям.
    #[must_use]
    pub fn confirmed_count(&self) -> usize {
        self.sections.iter().map(|s| s.findings.len()).sum()
    }

    /// Число секций без находок ([gap]).
    #[must_use]
    pub fn gap_count(&self) -> usize {
        self.sections
            .iter()
            .filter(|s| s.findings.is_empty())
            .count()
    }
}

/// Итог прогона: пути артефактов и счётчики.
#[derive(Debug, Clone)]
pub struct SurveyOutcome {
    /// Путь записанного `survey.md`.
    pub survey_path: PathBuf,
    /// Путь `survey-notes.md` (существующего или созданной заготовки).
    pub notes_path: PathBuf,
    /// Заготовка заметок создана этим прогоном (false — файл уже был и не тронут).
    pub notes_created: bool,
    /// Отчёт обследования.
    pub report: SurveyReport,
}

/// Репозиторий, поднятый в память один раз (все сканеры работают по нему).
struct RepoSnapshot {
    /// Относительные пути всех файлов (без `SKIP_DIRS` и dot-каталогов).
    files: Vec<String>,
    /// Содержимое scannable-файлов: относительный путь → строки.
    contents: BTreeMap<String, Vec<String>>,
}

/// Читает репозиторий в память: обход без служебных и dot-каталогов,
/// содержимое — только scannable-расширения до [`MAX_FILE_BYTES`].
fn snapshot(repo: &Path) -> Result<RepoSnapshot> {
    let mut files = Vec::new();
    let mut contents = BTreeMap::new();
    let walker = WalkDir::new(repo).follow_links(false).into_iter();
    for entry in walker.filter_entry(|e| {
        if e.file_type().is_dir() {
            let name = e.file_name().to_string_lossy();
            // Dot-каталоги (.git, .github, .venv…) и служебные — вне обхода;
            // CI ищется точечно по известным путям, а не обходом.
            !(name.starts_with('.') || SKIP_DIRS.contains(&name.as_ref()))
        } else {
            true
        }
    }) {
        let entry =
            entry.map_err(|e| HarnessError::Control(format!("обход {}: {e}", repo.display())))?;
        if !entry.file_type().is_file() {
            continue;
        }
        let rel = entry
            .path()
            .strip_prefix(repo)
            .map_err(|e| HarnessError::Control(format!("относительный путь: {e}")))?
            .to_string_lossy()
            .replace('\\', "/");
        // Dot-файлы (.env, .gitlab-ci.yml) в общий скан не берём.
        if rel.starts_with('.') {
            files.push(rel);
            continue;
        }
        let scannable = entry
            .path()
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| {
                SCANNABLE_EXTENSIONS
                    .iter()
                    .any(|s| e.eq_ignore_ascii_case(s))
            });
        if scannable && entry.metadata().map_or(0, |m| m.len()) <= MAX_FILE_BYTES {
            if let Ok(text) = std::fs::read_to_string(entry.path()) {
                contents.insert(rel.clone(), text.lines().map(str::to_owned).collect());
            }
            // Бинарный/невалидный UTF-8 — игнорируем содержимое, файл остаётся
            // в списке (безопасно: regex-сканеры по нему просто не пройдут).
        }
        files.push(rel);
    }
    files.sort();
    Ok(RepoSnapshot { files, contents })
}

/// Ссылка `путь:строка` (нумерация с 1).
fn file_line(path: &str, line_idx: usize) -> String {
    format!("{path}:{}", line_idx + 1)
}

/// Усекает строку-доказательство для вывода (длинные строки кода).
fn short_evidence(line: &str) -> String {
    const MAX_LINE: usize = 120;
    let trimmed = line.trim();
    if trimmed.chars().count() > MAX_LINE {
        format!("{}…", trimmed.chars().take(MAX_LINE).collect::<String>())
    } else {
        trimmed.to_string()
    }
}

/// Расширение файла (после последней точки) входит в набор — сравнение
/// регистронезависимое (`report.SQL` тоже SQL).
fn ext_is(path: &str, exts: &[&str]) -> bool {
    path.rsplit('.')
        .next()
        .is_some_and(|ext| exts.iter().any(|e| ext.eq_ignore_ascii_case(e)))
}

/// Regex-сканер по строкам содержимого: первый capture (или всё совпадение)
/// становится текстом находки, ссылка — `файл:строка`. Дедупликация по
/// (метка, путь, строка).
fn scan_lines(
    snap: &RepoSnapshot,
    label: &str,
    pattern: &str,
    findings: &mut Vec<Finding>,
) -> Result<()> {
    let re =
        Regex::new(pattern).map_err(|e| HarnessError::Control(format!("шаблон `{label}`: {e}")))?;
    for (path, lines) in &snap.contents {
        for (idx, line) in lines.iter().enumerate() {
            if findings.len() >= MAX_FINDINGS_PER_SCANNER {
                return Ok(());
            }
            if let Some(caps) = re.captures(line) {
                let snippet = caps
                    .get(1)
                    .map_or_else(|| short_evidence(line), |m| m.as_str().to_string());
                let f = Finding::one(format!("{label}: {snippet}"), file_line(path, idx));
                if !findings
                    .iter()
                    .any(|x| x.text == f.text && x.refs == f.refs)
                {
                    findings.push(f);
                }
            }
        }
    }
    Ok(())
}

/// Сканер 1а: стек по манифестам сборки.
fn scan_stack(snap: &RepoSnapshot) -> Vec<Finding> {
    const MANIFESTS: [(&str, &str); 7] = [
        ("Cargo.toml", "Rust (Cargo)"),
        ("pom.xml", "Java (Maven)"),
        ("build.gradle", "JVM (Gradle)"),
        ("build.gradle.kts", "Kotlin/JVM (Gradle)"),
        ("package.json", "Node.js (npm)"),
        ("go.mod", "Go"),
        ("pyproject.toml", "Python (pyproject)"),
    ];
    let mut out: Vec<Finding> = Vec::new();
    for path in &snap.files {
        let name = path.rsplit('/').next().unwrap_or(path.as_str());
        for (manifest, stack) in MANIFESTS {
            if name == manifest {
                push_unique(
                    &mut out,
                    Finding::one(format!("стек: {stack}"), file_line(path, 0)),
                );
            }
        }
        if name == "requirements.txt" {
            push_unique(
                &mut out,
                Finding::one("стек: Python (requirements)", file_line(path, 0)),
            );
        }
        if name.ends_with(".csproj") {
            push_unique(
                &mut out,
                Finding::one("стек: .NET (csproj)", file_line(path, 0)),
            );
        }
    }
    out
}

/// Находка добавляется, если такой текст уже не встречался (ссылки дополняются).
fn push_unique(findings: &mut Vec<Finding>, f: Finding) {
    if let Some(existing) = findings.iter_mut().find(|x| x.text == f.text) {
        for r in f.refs {
            if !existing.refs.contains(&r) && existing.refs.len() < MAX_REFS_PER_FINDING {
                existing.refs.push(r);
            }
        }
    } else {
        findings.push(f);
    }
}

/// Сканер 1б: дерево каталогов верхнего уровня с числом файлов.
/// Возвращает находки и пары (каталог, число файлов) для сканера владельцев.
fn scan_structure(snap: &RepoSnapshot) -> (Vec<Finding>, Vec<(String, usize)>) {
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut root_files = 0usize;
    for path in &snap.files {
        match path.split_once('/') {
            Some((top, _)) if !top.starts_with('.') => {
                *counts.entry(top.to_string()).or_insert(0) += 1;
            }
            _ => root_files += 1,
        }
    }
    let mut dirs: Vec<(String, usize)> = counts.into_iter().collect();
    dirs.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    let mut out: Vec<Finding> = dirs
        .iter()
        .map(|(dir, n)| Finding::one(format!("каталог `{dir}/` — {n} файлов"), format!("{dir}/")))
        .collect();
    if root_files > 0 {
        out.push(Finding::one(
            format!("файлов в корне репозитория: {root_files}"),
            "./".to_string(),
        ));
    }
    (out, dirs)
}

/// Сканер 2: точки входа — HTTP-роуты, консьюмеры очередей, джобы.
fn scan_entry_points(snap: &RepoSnapshot) -> Result<Vec<Finding>> {
    let mut out = Vec::new();
    // HTTP-роуты: axum, spring, express, fastapi/flask.
    scan_lines(
        snap,
        "HTTP route (axum)",
        r#"\.route\(\s*"([^"]+)""#,
        &mut out,
    )?;
    scan_lines(
        snap,
        "HTTP endpoint (spring)",
        r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)",
        &mut out,
    )?;
    scan_lines(
        snap,
        "HTTP route (express)",
        // `app.get("/…")` в начале оператора: декоратор `@app.get` (flask)
        // сюда не попадает — иначе одна строка давала бы две находки.
        r#"^\s*(app\.(?:get|post|put|delete|patch)\s*\(\s*["'][^"']+)"#,
        &mut out,
    )?;
    scan_lines(
        snap,
        "HTTP route (fastapi/flask)",
        r#"(@(?:app|router)\.(?:get|post|put|delete|patch|route)\s*\(\s*["'][^"']+)"#,
        &mut out,
    )?;
    // Консьюмеры очередей.
    scan_lines(
        snap,
        "консьюмер Kafka (spring)",
        r"@KafkaListener",
        &mut out,
    )?;
    scan_lines(
        snap,
        "консьюмер Kafka (cli)",
        r"\bkafka-console-consumer\b",
        &mut out,
    )?;
    scan_lines(
        snap,
        "консьюмер очереди (pika)",
        r"\bbasic_consume\s*\(",
        &mut out,
    )?;
    scan_lines(
        snap,
        "консьюмер очереди (amqp)",
        r"\bchannel\.basicConsume\s*\(",
        &mut out,
    )?;
    scan_lines(snap, "упоминание Rabbit", r"(?i)\brabbit", &mut out)?;
    // Джобы: spring @Scheduled + файлы планировщиков.
    scan_lines(snap, "джоба (spring @Scheduled)", r"@Scheduled", &mut out)?;
    for path in &snap.files {
        if out.len() >= MAX_FINDINGS_PER_SCANNER {
            break;
        }
        let name = path.rsplit('/').next().unwrap_or(path.as_str());
        if name == "crontab" || name == "cron.toml" || ext_is(name, &["timer"]) {
            out.push(Finding::one(
                format!("расписание планировщика: `{path}`"),
                file_line(path, 0),
            ));
        }
    }
    Ok(out)
}

/// Сканер 3: хранилища — каталоги миграций и *.sql вне миграций.
fn scan_storages(snap: &RepoSnapshot) -> Vec<Finding> {
    let mut out = Vec::new();
    // Каталоги миграций: по имени каталога или суффиксу «db/migrate».
    let mut migration_dirs: BTreeSet<String> = BTreeSet::new();
    for path in &snap.files {
        let parts: Vec<&str> = path.split('/').collect();
        for window in parts.windows(2) {
            let parent = window[0];
            if MIGRATION_DIR_NAMES.contains(&parent) {
                let idx = path.find(parent).unwrap_or(0);
                if let Some(dir) = path.get(..idx + parent.len()) {
                    migration_dirs.insert(dir.to_string());
                }
            }
        }
        if let Some(pos) = path.find("db/migrate/") {
            if let Some(dir) = path.get(..pos + "db/migrate".len()) {
                migration_dirs.insert(dir.to_string());
            }
        }
    }
    for dir in &migration_dirs {
        let prefix = format!("{dir}/");
        let n = snap.files.iter().filter(|f| f.starts_with(&prefix)).count();
        out.push(Finding::one(
            format!("каталог миграций `{dir}/` — {n} файлов"),
            format!("{dir}/"),
        ));
    }
    // *.sql вне каталогов миграций.
    let mut sql_outside = 0usize;
    for path in &snap.files {
        if !ext_is(path, &["sql"]) {
            continue;
        }
        let in_migrations = migration_dirs
            .iter()
            .any(|d| path.starts_with(&format!("{d}/")));
        if !in_migrations {
            sql_outside += 1;
            if sql_outside <= MAX_FINDINGS_PER_SCANNER {
                out.push(Finding::one(
                    format!("SQL вне миграций: `{path}`"),
                    file_line(path, 0),
                ));
            }
        }
    }
    out
}

/// Извлекает `host:port` из URL/строки подключения: схема и путь отбрасываются,
/// userinfo (`user:pass@`) срезается по ПОСЛЕДНЕМУ `@` в authority-части.
/// Петлевые хосты ([`LOOPBACK_HOSTS`]) → None.
fn extract_host_port(url: &str) -> Option<String> {
    let after_scheme = url.split_once("://").map_or(url, |(_, rest)| rest);
    // Authority — до первого разделителя пути/запроса/фрагмента.
    let end = after_scheme
        .find(['/', '?', '#'])
        .unwrap_or(after_scheme.len());
    let authority = &after_scheme[..end];
    // Userinfo — всё до последнего '@' в authority (в пароле может быть '@'
    // в percent-encoding, но и в сыром виде встречается — берём надёжную сторону).
    let host_port = authority.rsplit('@').next().unwrap_or(authority);
    let host_port = host_port.trim_end_matches([',', ';', ')', ']', '\'', '"']);
    let host = host_port.split(':').next().unwrap_or("");
    if host.is_empty() || LOOPBACK_HOSTS.contains(&host) {
        return None;
    }
    Some(host_port.to_string())
}

/// Сканер 4: интеграции — URL/строки подключения из конфигов → host:port.
/// Значения проходят через редактор секретов (`src/secrets.rs`) — в карту
/// не должны уехать ни userinfo, ни токены в query.
fn scan_integrations(snap: &RepoSnapshot) -> Result<Vec<Finding>> {
    let re = Regex::new(
        r#"(?i)\b(?:https?|postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqps?|jdbc:[a-z0-9]+)://[^\s"'<>)\]]+"#,
    )
    .map_err(|e| HarnessError::Control(format!("шаблон интеграций: {e}")))?;
    let redactor = crate::secrets::Redactor::with_builtin_rules();
    let mut by_host: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (path, lines) in &snap.contents {
        let is_config = path.rsplit('.').next().is_some_and(|ext| {
            CONFIG_EXTENSIONS
                .iter()
                .any(|c| ext.eq_ignore_ascii_case(c))
        });
        if !is_config {
            continue;
        }
        for (idx, line) in lines.iter().enumerate() {
            for m in re.find_iter(line) {
                if let Some(host) = extract_host_port(m.as_str()) {
                    let host = redactor.redact(&host);
                    let refs = by_host.entry(host).or_default();
                    let reference = file_line(path, idx);
                    if !refs.contains(&reference) && refs.len() < MAX_REFS_PER_FINDING {
                        refs.push(reference);
                    }
                }
            }
        }
    }
    Ok(by_host
        .into_iter()
        .take(MAX_FINDINGS_PER_SCANNER)
        .map(|(host, refs)| Finding {
            text: format!("внешний endpoint `{host}`"),
            refs,
        })
        .collect())
}

/// Каталог — КОРЕНЬ git-репозитория. Вложенный в чужой репозиторий каталог
/// не считается источником истории: `git log` в нём вернул бы churn/авторов
/// родительского репо — чужие данные в карте обследования хуже [gap].
fn git_root(repo: &Path) -> Option<PathBuf> {
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None; // не git-репозиторий (и не внутри чужого)
    }
    let top = PathBuf::from(String::from_utf8_lossy(&output.stdout).trim().to_string());
    let canon_top = std::fs::canonicalize(&top).ok()?;
    let canon_repo = std::fs::canonicalize(repo).ok()?;
    (canon_top == canon_repo).then_some(top)
}

/// Сканер 5: владельцы — CODEOWNERS (первые строки) + git shortlog по
/// топ-каталогам. Не-git репозиторий → пустой результат (секция уйдёт в
/// [gap], если и CODEOWNERS нет).
fn scan_owners(repo: &Path, snap: &RepoSnapshot, top_dirs: &[(String, usize)]) -> Vec<Finding> {
    let mut out = Vec::new();
    // CODEOWNERS: корень или .github/ (путь известный — dot-каталог вне обхода).
    for candidate in ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"] {
        let path = repo.join(candidate);
        if let Ok(text) = std::fs::read_to_string(&path) {
            for (idx, line) in text.lines().enumerate() {
                if idx >= MAX_CODEOWNERS_LINES {
                    break;
                }
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') {
                    continue;
                }
                out.push(Finding::one(
                    format!("CODEOWNERS: {line}"),
                    file_line(candidate, idx),
                ));
            }
            break; // первый найденный CODEOWNERS — единственный источник
        }
    }
    // git shortlog -sn по топ-каталогам (не-git или вложенный в чужой
    // репозиторий каталог → тихий пропуск, история была бы чужой).
    let _ = snap; // файлы уже посчитаны в scan_structure
    if git_root(repo).is_none() {
        return out;
    }
    for (dir, _) in top_dirs.iter().take(TOP_DIRS_FOR_OWNERS) {
        let Ok(output) = std::process::Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["shortlog", "-sn", "HEAD", "--", dir])
            .output()
        else {
            continue; // git недоступен — владельцы из истории неизвлекаемы
        };
        if !output.status.success() {
            continue; // не git-репозиторий — [gap] решит секция
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        let committers: Vec<String> = stdout
            .lines()
            .take(TOP_COMMITERS_PER_DIR)
            .map(|l| l.trim().to_string())
            .filter(|l| !l.is_empty())
            .collect();
        if !committers.is_empty() {
            out.push(Finding::one(
                format!("коммитеры `{dir}/`: {}", committers.join("; ")),
                format!("{dir}/"),
            ));
        }
    }
    out
}

/// Сканер 6: тесты и CI — каталоги тестов с числом файлов, наличие CI.
fn scan_tests_ci(repo: &Path, snap: &RepoSnapshot) -> Vec<Finding> {
    let mut out = Vec::new();
    let mut test_dirs: BTreeMap<String, usize> = BTreeMap::new();
    for path in &snap.files {
        let parts: Vec<&str> = path.split('/').collect();
        for (i, part) in parts.iter().enumerate().take(parts.len().saturating_sub(1)) {
            if TEST_DIR_NAMES.contains(part) {
                let dir = parts[..=i].join("/");
                *test_dirs.entry(dir).or_insert(0) += 1;
            }
        }
    }
    for (dir, n) in test_dirs {
        out.push(Finding::one(
            format!("каталог тестов `{dir}/` — {n} файлов"),
            format!("{dir}/"),
        ));
    }
    // CI: известные пути (dot-каталоги вне общего обхода — проверяем точечно).
    let workflows = repo.join(".github/workflows");
    if workflows.is_dir() {
        let n = std::fs::read_dir(&workflows).map_or(0, |rd| rd.flatten().count());
        out.push(Finding::one(
            format!("CI: GitHub Actions — {n} workflow-файлов"),
            ".github/workflows/".to_string(),
        ));
    }
    for candidate in [".gitlab-ci.yml", "Jenkinsfile"] {
        if repo.join(candidate).is_file() {
            out.push(Finding::one(
                format!("CI: `{candidate}`"),
                file_line(candidate, 0),
            ));
        }
    }
    out
}

/// Сканер 7: долг и трупы — маркеры в именах файлов, закомментированные
/// feature-флаги.
fn scan_debt(snap: &RepoSnapshot) -> Result<Vec<Finding>> {
    let mut out = Vec::new();
    let marker = Regex::new(r"(?i)deprecated|dead[_-]?code|legacy")
        .map_err(|e| HarnessError::Control(format!("шаблон долга: {e}")))?;
    for path in &snap.files {
        let name = path.rsplit('/').next().unwrap_or(path.as_str());
        if marker.is_match(name) {
            out.push(Finding::one(
                format!("маркер долга в имени файла: `{path}`"),
                file_line(path, 0),
            ));
        }
    }
    // Закомментированные feature-флаги: комментарий с присваиванием флага
    // (capture 1 — сам флаг со значением, маркер комментария вне группы).
    scan_lines(
        snap,
        "закомментированный feature-флаг",
        r"^\s*(?:#|//|--|;)\s*(?:export\s+)?(\w*FEATURE[A-Za-z0-9_]*\s*[=:]\s*\S+)",
        &mut out,
    )?;
    scan_lines(
        snap,
        "закомментированный feature-флаг",
        r"^\s*(?:#|//|--|;)\s*(?:export\s+)?([a-z_]*feature[_-][a-z0-9_]+\s*[=:]\s*\S+)",
        &mut out,
    )?;
    Ok(out)
}

/// Сканер 5 (скрытые связи): общие таблицы (одна таблица используется из
/// разных компонентов верхнего уровня) и каталоги файлового обмена.
fn scan_hidden_links(snap: &RepoSnapshot) -> Result<Vec<Finding>> {
    let mut out = Vec::new();
    let re = Regex::new(
        r"(?i)\b(?:create\s+table(?:\s+if\s+not\s+exists)?|insert\s+into|update|from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    )
    .map_err(|e| HarnessError::Control(format!("шаблон таблиц: {e}")))?;
    // Таблица → (компоненты верхнего уровня, ссылки).
    let mut by_table: BTreeMap<String, (BTreeSet<String>, Vec<String>)> = BTreeMap::new();
    for (path, lines) in &snap.contents {
        if !ext_is(path, &["sql", "py"]) {
            continue;
        }
        // Компонент = первый сегмент пути (корневые файлы — «.»).
        let component = path.split('/').next().unwrap_or(".").to_string();
        for (idx, line) in lines.iter().enumerate() {
            for caps in re.captures_iter(line) {
                let Some(m) = caps.get(1) else { continue };
                let table = m.as_str().to_lowercase();
                // Отсекаем SQL-ключевые слова, попавшие в capture у FROM/JOIN.
                if matches!(
                    table.as_str(),
                    "select" | "values" | "set" | "where" | "lateral"
                ) {
                    continue;
                }
                let entry = by_table.entry(table).or_default();
                entry.0.insert(component.clone());
                let reference = file_line(path, idx);
                if !entry.1.contains(&reference) && entry.1.len() < MAX_REFS_PER_FINDING {
                    entry.1.push(reference);
                }
            }
        }
    }
    for (table, (components, refs)) in by_table {
        if components.len() >= 2 {
            let mut comps: Vec<String> = components.into_iter().collect();
            comps.sort();
            out.push(Finding {
                text: format!(
                    "общая таблица `{table}` — используется из: {}",
                    comps.join(", ")
                ),
                refs,
            });
        }
    }
    // Каталоги файлового обмена (drop/inbox/outbox…).
    let mut exchange_dirs: BTreeSet<String> = BTreeSet::new();
    for path in &snap.files {
        let parts: Vec<&str> = path.split('/').collect();
        for (i, part) in parts.iter().enumerate().take(parts.len().saturating_sub(1)) {
            if EXCHANGE_DIR_NAMES.contains(part) {
                exchange_dirs.insert(parts[..=i].join("/"));
            }
        }
    }
    for dir in exchange_dirs {
        out.push(Finding::one(
            format!("каталог файлового обмена: `{dir}/`"),
            format!("{dir}/"),
        ));
    }
    out.truncate(MAX_FINDINGS_PER_SCANNER);
    Ok(out)
}

/// Сканер 8: ограничения платформы — объявленные версии из манифестов
/// (edition/rust-version, requires-python, engines, go-директива).
fn scan_platform(snap: &RepoSnapshot) -> Result<Vec<Finding>> {
    let mut out = Vec::new();
    let patterns: [(&str, &str); 5] = [
        ("rust edition", r#"^\s*edition\s*=\s*"[^"]+""#),
        ("rust-version", r#"^\s*rust-version\s*=\s*"[^"]+""#),
        ("requires-python", r#"^\s*requires-python\s*=\s*"[^"]+""#),
        ("node engines", r#""engines"\s*:"#),
        ("go-директива", r"^go\s+\d+\.\d+"),
    ];
    for (label, pattern) in patterns {
        scan_lines(snap, label, pattern, &mut out)?;
    }
    Ok(out)
}

/// Сканер 9: риски изменения — «горячие» файлы по git-истории (churn).
/// Не-git репозиторий (или вложенный в чужой) → пусто ([gap]).
fn scan_risks(repo: &Path) -> Vec<Finding> {
    let mut out = Vec::new();
    if git_root(repo).is_none() {
        return out; // не git-корень: churn был бы по чужой истории
    }
    let Ok(output) = std::process::Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(["log", "--pretty=format:", "--name-only", "-n"])
        .arg(CHURN_COMMITS.to_string())
        .output()
    else {
        return out; // git недоступен
    };
    if !output.status.success() {
        return out; // не git-репозиторий — [gap] решит секция
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for line in stdout.lines() {
        let path = line.trim();
        if !path.is_empty() {
            *counts.entry(path.to_string()).or_insert(0) += 1;
        }
    }
    let mut hot: Vec<(String, usize)> = counts.into_iter().collect();
    hot.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    for (path, n) in hot.into_iter().take(TOP_CHURN_FILES) {
        out.push(Finding::one(
            format!("горячий файл `{path}` — {n} изменений за последние ≤{CHURN_COMMITS} коммитов"),
            file_line(&path, 0),
        ));
    }
    out
}

/// Собирает карту обследования по репозиторию (дата — сегодня).
///
/// # Errors
/// Репозиторий не существует/не каталог; ошибки обхода и чтения.
pub fn scan(repo: &Path) -> Result<SurveyReport> {
    scan_at(repo, chrono::Local::now().date_naive())
}

/// Сборка карты с явной датой (детерминированные тесты frontmatter).
fn scan_at(repo: &Path, today: NaiveDate) -> Result<SurveyReport> {
    if !repo.is_dir() {
        return Err(HarnessError::Control(format!(
            "survey: репозиторий недоступен или не каталог: {}",
            repo.display()
        )));
    }
    let snap = snapshot(repo)?;
    let (structure, top_dirs) = scan_structure(&snap);
    let mut components = scan_stack(&snap);
    components.extend(structure);
    components.extend(scan_owners(repo, &snap, &top_dirs));

    let sections = vec![
        Section {
            title: "Карта компонентов и владельцев",
            findings: components,
            gap_question: "Стек/структура/владельцы не определены механически — кто владелец системы и из чего она собрана?",
        },
        Section {
            title: "Точки входа (API, джобы, очереди)",
            findings: scan_entry_points(&snap)?,
            gap_question: "Точки входа не найдены — как с системой взаимодействуют (API, джобы, очереди)?",
        },
        Section {
            title: "Модель данных (миграции, SQL)",
            findings: scan_storages(&snap),
            gap_question: "Миграции и SQL не найдены — где схема данных и кто владеет записями?",
        },
        Section {
            title: "Интеграции (host:port из конфигов)",
            findings: scan_integrations(&snap)?,
            gap_question: "Внешние интеграции не найдены в конфигах — с какими системами связь и по каким протоколам?",
        },
        Section {
            title: "Скрытые связи (общие таблицы, файловый обмен)",
            findings: scan_hidden_links(&snap)?,
            gap_question: "Скрытые связи не обнаружены — есть ли общие таблицы/файловые обмены/планировщики между компонентами?",
        },
        Section {
            title: "Тесты и наблюдаемость",
            findings: scan_tests_ci(repo, &snap),
            gap_question: "Тесты и CI не найдены — чем подтвердим безопасность изменения?",
        },
        Section {
            title: "Долг и трупы",
            findings: scan_debt(&snap)?,
            gap_question: "Эвристика долга ничего не нашла — есть ли мёртвый код и неиспользуемые флаги (подтвердить у владельца)?",
        },
        Section {
            title: "Ограничения платформы (версии из манифестов)",
            findings: scan_platform(&snap)?,
            gap_question: "Версии платформы не объявлены — какие рантаймы/зависимости, их EOL и лицензии?",
        },
        Section {
            title: "Риски изменения (churn по git-истории)",
            findings: scan_risks(repo),
            gap_question: "Git-история недоступна — где хрупкие места по истории инцидентов/коммитов?",
        },
    ];

    Ok(SurveyReport {
        repo_name: repo.file_name().map_or_else(
            || repo.display().to_string(),
            |n| n.to_string_lossy().into_owned(),
        ),
        created_at: today,
        expires_at: today + Duration::days(SURVEY_TTL_DAYS),
        sections,
    })
}

/// Рендер одной находки: `- [confirmed] текст (ref1, ref2)`.
fn render_finding(out: &mut String, f: &Finding) {
    let _ = write!(out, "- [confirmed] {}", f.text);
    if !f.refs.is_empty() {
        let _ = write!(out, " ({})", f.refs.join(", "));
    }
    out.push('\n');
}

/// Рендерит карту обследования в markdown (полностью генерируемый файл).
#[must_use]
pub fn render_markdown(report: &SurveyReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "---");
    let _ = writeln!(out, "title: Карта обследования (reverse discovery)");
    let _ = writeln!(out, "repo: {}", report.repo_name);
    let _ = writeln!(out, "created_at: {}", report.created_at);
    let _ = writeln!(out, "expires_at: {}", report.expires_at);
    let _ = writeln!(out, "generator: arch-ml survey");
    let _ = writeln!(out, "---\n");
    let _ = writeln!(out, "# Карта обследования: {}", report.repo_name);
    let _ = writeln!(
        out,
        "\n> Файл ПОЛНОСТЬЮ генерируется `arch-ml survey`: повторный прогон\n\
         > перезаписывает его целиком. Человеческие/LLM-дополнения `[inferred]` —\n\
         > в соседний `survey-notes.md` (survey его никогда не трогает).\n\
         > Метки: `[confirmed]` — доказано кодом/конфигом (ссылка `файл:строка`);\n\
         > `[gap]` — пробел, нужен владелец домена. Срок годности — до\n\
         > `expires_at`, затем карту пересобирают (living-spec, не разовый аудит).\n"
    );
    for (i, section) in report.sections.iter().enumerate() {
        let _ = writeln!(out, "## {}. {}", i + 1, section.title);
        if section.findings.is_empty() {
            let _ = writeln!(out, "- [gap] {}", section.gap_question);
        } else {
            for f in &section.findings {
                render_finding(&mut out, f);
            }
        }
        out.push('\n');
    }
    out
}

/// Заготовка `survey-notes.md`: структура 9 артефактов для [inferred]-заметок.
#[must_use]
pub fn render_notes_stub(report: &SurveyReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# Заметки к обследованию (human/LLM, [inferred])\n");
    let _ = writeln!(
        out,
        "> Этот файл НЕ перезаписывается `arch-ml survey`. Сюда — выводы,\n\
         > выведенные косвенно (`[inferred]`), с обязательной пометкой «проверить\n\
         > перед опорой» и именем проверяющего. Механические находки — в\n\
         > `survey.md`, он генерируемый.\n"
    );
    for (i, section) in report.sections.iter().enumerate() {
        let _ = writeln!(out, "## {}. {}", i + 1, section.title);
        let _ = writeln!(out, "- (пока пусто)\n");
    }
    out
}

/// Прогон обследования: пишет `survey.md` (перезапись), создаёт заготовку
/// `survey-notes.md` при отсутствии (существующий не трогает НИКОГДА).
///
/// # Errors
/// Репозиторий недоступен; ошибки сканирования и записи файлов.
pub fn run(repo: &Path, out: Option<&Path>) -> Result<SurveyOutcome> {
    if !repo.is_dir() {
        return Err(HarnessError::Control(format!(
            "survey: репозиторий недоступен или не каталог: {}",
            repo.display()
        )));
    }
    let out_dir = match out {
        Some(dir) => {
            if dir.is_absolute() {
                dir.to_path_buf()
            } else {
                repo.join(dir)
            }
        }
        None => repo.join("docs/reverse"),
    };
    std::fs::create_dir_all(&out_dir).map_err(|e| HarnessError::io(&out_dir, e))?;
    let report = scan(repo)?;
    let survey_path = out_dir.join("survey.md");
    std::fs::write(&survey_path, render_markdown(&report))
        .map_err(|e| HarnessError::io(&survey_path, e))?;
    let notes_path = out_dir.join("survey-notes.md");
    let mut notes_created = false;
    if !notes_path.exists() {
        std::fs::write(&notes_path, render_notes_stub(&report))
            .map_err(|e| HarnessError::io(&notes_path, e))?;
        notes_created = true;
    }
    Ok(SurveyOutcome {
        survey_path,
        notes_path,
        notes_created,
        report,
    })
}

/// Инструменты домена survey.
#[must_use]
pub fn tools() -> Vec<Arc<dyn Tool>> {
    vec![Arc::new(ReverseSurveyTool)]
}

/// Инструмент агента: `reverse_survey` — обратное обследование legacy.
pub struct ReverseSurveyTool;

#[async_trait]
impl Tool for ReverseSurveyTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "reverse_survey".into(),
            description: "Обратное обследование legacy-репозитория (reverse discovery): \
                детерминированный сканер (без LLM) собирает каркас карты обследования \
                docs/reverse/survey.md по 9 артефактам скилла reverse-discovery — компоненты \
                и владельцы, точки входа (HTTP/очереди/джобы), модель данных (миграции/SQL), \
                интеграции (host:port из конфигов, userinfo срезан, localhost пропущен), \
                скрытые связи (общие таблицы, drop-каталоги), тесты/CI, долг и трупы, \
                версии платформы, риски (git churn). Все находки — [confirmed] со ссылкой \
                файл:строка; секции без находок — [gap] с вопросом владельцу. survey.md \
                перезаписывается целиком; survey-notes.md (заметки [inferred]) не трогается \
                (при отсутствии создаётся заготовка). CLI-эквивалент: arch-ml survey <repo>."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "Каталог репозитория для обследования (относительно cwd или абсолютный)"
                    },
                    "out": {
                        "type": "string",
                        "description": "Каталог вывода относительно репозитория (по умолчанию docs/reverse)"
                    }
                },
                "required": ["repo"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let Some(repo) = args.get("repo").and_then(Value::as_str) else {
            return Ok(ToolOutput::err(
                "reverse_survey: нужен параметр `repo` (каталог репозитория)",
            ));
        };
        let repo = ctx.resolve(repo);
        let out = args.get("out").and_then(Value::as_str).map(PathBuf::from);
        let outcome = match run(&repo, out.as_deref()) {
            Ok(o) => o,
            Err(e) => return Ok(ToolOutput::err(format!("reverse_survey: {e}"))),
        };
        let mut text = format!(
            "Обследование `{}`: {} находок [confirmed], {} секций [gap].\nКарта: {}",
            outcome.report.repo_name,
            outcome.report.confirmed_count(),
            outcome.report.gap_count(),
            outcome.survey_path.display(),
        );
        if outcome.notes_created {
            let _ = write!(
                text,
                "\nСоздана заготовка заметок [inferred]: {}",
                outcome.notes_path.display()
            );
        }
        for (i, section) in outcome.report.sections.iter().enumerate() {
            let _ = writeln!(
                text,
                "  {}. {} — {} находок{}",
                i + 1,
                section.title,
                section.findings.len(),
                if section.findings.is_empty() {
                    " [gap]"
                } else {
                    ""
                }
            );
        }
        Ok(ToolOutput::ok(text))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Пишет файл, создавая родительские каталоги.
    fn write_file(path: &Path, text: &str) {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("mkdir");
        }
        std::fs::write(path, text).expect("write");
    }

    /// Фикстурный мультистек-репозиторий: rust + python + sql + конфиги,
    /// общая таблица между компонентами, drop-каталог, долг, CI, CODEOWNERS.
    fn make_repo(base: &Path) -> PathBuf {
        let repo = base.join("repo");
        write_file(&repo.join("Cargo.toml"), "[package]\nedition = \"2021\"\n");
        write_file(
            &repo.join("src/main.rs"),
            "fn main() {}\n// axum: .route(\"/api/pay\", post(pay))\n",
        );
        write_file(
            &repo.join("billing/worker.py"),
            "@app.get(\"/health\")\n@router.post(\"/charge\")\n\
             channel.basicConsume(\"payments.notify\")\n\
             cursor.execute(\"INSERT INTO payments (id, amount) VALUES (%s, %s)\")\n\
             # FEATURE_NEW_PRICING=false\n",
        );
        write_file(
            &repo.join("notifier/sender.py"),
            "import pika  # rabbit broker\nch.basic_consume(queue=\"payments.notify\")\n\
             rows = cur.execute(\"SELECT id FROM payments WHERE notified = 0\")\n",
        );
        write_file(
            &repo.join("billing/legacy_export.py"),
            "# не используется\n",
        );
        write_file(
            &repo.join("migrations/0001_init.sql"),
            "CREATE TABLE payments (id bigint, amount numeric);\n",
        );
        write_file(
            &repo.join("migrations/0002_idx.sql"),
            "CREATE INDEX ON payments (id);\n",
        );
        write_file(
            &repo.join("scripts/report.sql"),
            "SELECT count(*) FROM payments;\n",
        );
        write_file(
            &repo.join("config/app.yaml"),
            "services:\n  core: \"https://svc:secretpass@corebank.example:8443/api/v1\"\n\
             \x20 db: \"postgres://loader:pw123@db.internal.example:5432/ledger\"\n\
             \x20 stub: \"http://127.0.0.1:9099/mock\"\n  local: \"http://localhost:8080/x\"\n",
        );
        write_file(
            &repo.join("crontab"),
            "0 2 * * * /opt/repo/scripts/nightly.sh\n",
        );
        write_file(
            &repo.join("drop/rates_2026-09-01.csv"),
            "date,rate\n2026-09-01,1.0\n",
        );
        write_file(
            &repo.join("billing/tests/test_worker.py"),
            "def test_ok():\n    assert True\n",
        );
        write_file(
            &repo.join(".github/workflows/ci.yml"),
            "name: ci\non: [push]\n",
        );
        write_file(
            &repo.join("CODEOWNERS"),
            "* @team-core\nbilling/ @team-billing\n",
        );
        write_file(&repo.join("requirements.txt"), "flask\npika\npsycopg2\n");
        // Мусор вне сканирования.
        write_file(&repo.join("target/junk.rs"), "fn junk() {}\n");
        write_file(
            &repo.join("node_modules/pkg/index.js"),
            "app.get(\"/noise\")\n",
        );
        repo
    }

    #[test]
    fn survey_fixture_confirmed_sections_with_refs() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = make_repo(tmp.path());
        let report =
            scan_at(&repo, NaiveDate::from_ymd_opt(2026, 9, 4).expect("date")).expect("scan");
        let md = render_markdown(&report);
        // Стек: rust + python, со ссылкой на манифест.
        assert!(md.contains("стек: Rust (Cargo) (Cargo.toml:1)"), "{md}");
        assert!(md.contains("стек: Python (requirements)"), "{md}");
        // Точки входа: axum, fastapi, очереди, crontab — с файл:строка.
        assert!(
            md.contains("HTTP route (axum): /api/pay (src/main.rs:2)"),
            "{md}"
        );
        assert!(md.contains("HTTP route (fastapi/flask)"), "{md}");
        assert!(md.contains("billing/worker.py:1"), "{md}");
        assert!(md.contains("консьюмер очереди (amqp)"), "{md}");
        assert!(md.contains("расписание планировщика: `crontab`"), "{md}");
        // Хранилища: миграции с числом файлов + sql вне миграций.
        assert!(
            md.contains("каталог миграций `migrations/` — 2 файлов"),
            "{md}"
        );
        assert!(
            md.contains("SQL вне миграций: `scripts/report.sql`"),
            "{md}"
        );
        // Владельцы: CODEOWNERS (git-истории нет — shortlog тихо пропущен).
        assert!(
            md.contains("CODEOWNERS: * @team-core (CODEOWNERS:1)"),
            "{md}"
        );
        // Тесты и CI.
        assert!(
            md.contains("каталог тестов `billing/tests/` — 1 файлов"),
            "{md}"
        );
        assert!(
            md.contains("CI: GitHub Actions — 1 workflow-файлов"),
            "{md}"
        );
        // Долг: legacy в имени + закомментированный флаг.
        assert!(
            md.contains("маркер долга в имени файла: `billing/legacy_export.py`"),
            "{md}"
        );
        assert!(md.contains("FEATURE_NEW_PRICING"), "{md}");
        // Платформа: rust edition.
        assert!(
            md.contains("rust edition: edition = \"2021\" (Cargo.toml:2)"),
            "{md}"
        );
        // Служебные каталоги не сканируются.
        assert!(!md.contains("/noise"), "{md}");
        assert!(!md.contains("junk"), "{md}");
        // Frontmatter: срок годности created + 90 дней.
        assert!(md.contains("created_at: 2026-09-04"), "{md}");
        assert!(md.contains("expires_at: 2026-12-03"), "{md}");
    }

    #[test]
    fn survey_gap_sections_present_when_no_findings() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = make_repo(tmp.path());
        let report = scan(&repo).expect("scan");
        let md = render_markdown(&report);
        // Не-git каталог: риски (churn) — [gap].
        assert!(md.contains("## 9. Риски изменения"), "{md}");
        assert!(md.contains("- [gap] Git-история недоступна"), "{md}");
        assert!(report.gap_count() >= 1, "{}", report.gap_count());
    }

    #[test]
    fn survey_hidden_links_shared_table_and_drop_dir() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = make_repo(tmp.path());
        let report = scan(&repo).expect("scan");
        let md = render_markdown(&report);
        // payments пишется из billing/ и читается из notifier/ — скрытая связь
        // (миграции и ad-hoc SQL тоже ссылаются на неё и честно перечисляются).
        assert!(
            md.contains(
                "общая таблица `payments` — используется из: billing, migrations, notifier, scripts"
            ),
            "{md}"
        );
        assert!(md.contains("каталог файлового обмена: `drop/`"), "{md}");
    }

    #[test]
    fn git_root_only_for_repo_root_not_nested_dir() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = tmp.path().join("repo");
        std::fs::create_dir_all(repo.join("monolith")).expect("mkdir");
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args(["init", "-q"])
            .output()
            .expect("git init");
        assert!(out.status.success());
        // Корень репозитория — источник истории…
        assert!(git_root(&repo).is_some());
        // …а вложенный каталог — нет: его churn/shortlog были бы по чужой
        // истории родительского репо. Для survey он «не-git» → [gap].
        assert!(git_root(&repo.join("monolith")).is_none());
        assert!(scan_risks(&repo.join("monolith")).is_empty());
    }

    #[test]
    fn extract_host_port_strips_userinfo_path_and_loopback() {
        // userinfo обязан быть срезан — вместе с паролем.
        assert_eq!(
            extract_host_port("https://user:pass@host.example:8443/path?q=1"),
            Some("host.example:8443".to_string())
        );
        assert_eq!(
            extract_host_port("postgres://u:p@db.internal/ledger"),
            Some("db.internal".to_string())
        );
        // Без userinfo — хост как есть.
        assert_eq!(
            extract_host_port("http://rates.example.com:8080/feed"),
            Some("rates.example.com:8080".to_string())
        );
        // Петлевые — не интеграции.
        assert_eq!(extract_host_port("http://127.0.0.1:9099/mock"), None);
        assert_eq!(extract_host_port("http://localhost:8080/x"), None);
        assert_eq!(extract_host_port("amqp://0.0.0.0:5672"), None);
    }

    #[test]
    fn survey_integrations_host_only_no_userinfo_no_localhost() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = make_repo(tmp.path());
        let report = scan(&repo).expect("scan");
        let md = render_markdown(&report);
        assert!(
            md.contains("внешний endpoint `corebank.example:8443`"),
            "{md}"
        );
        assert!(
            md.contains("внешний endpoint `db.internal.example:5432`"),
            "{md}"
        );
        // userinfo срезан, петлевые пропущены.
        assert!(!md.contains("svc:secretpass"), "{md}");
        assert!(!md.contains("loader:pw123"), "{md}");
        assert!(!md.contains("secretpass@"), "{md}");
        assert!(!md.contains("127.0.0.1"), "{md}");
        assert!(!md.contains("localhost"), "{md}");
    }

    #[test]
    fn rerun_overwrites_survey_but_never_touches_notes() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = make_repo(tmp.path());
        // Первый прогон: создаёт survey.md и заготовку заметок.
        let first = run(&repo, None).expect("run");
        assert!(first.notes_created, "заготовка создаётся один раз");
        assert!(first.survey_path.ends_with("docs/reverse/survey.md"));
        // Человек дописывает заметки и «портит» survey.md — проверяем контракт.
        std::fs::write(&first.notes_path, "# Мои [inferred]-заметки\n").expect("notes");
        std::fs::write(&first.survey_path, "МУСОРНЫЙ МАРКЕР\n").expect("survey");
        let second = run(&repo, None).expect("rerun");
        assert!(!second.notes_created, "заготовка уже есть — не трогаем");
        let notes = std::fs::read_to_string(&second.notes_path).expect("notes");
        assert_eq!(notes, "# Мои [inferred]-заметки\n", "заметки человека целы");
        let survey = std::fs::read_to_string(&second.survey_path).expect("survey");
        assert!(
            !survey.contains("МУСОРНЫЙ МАРКЕР"),
            "survey перезаписан целиком"
        );
        assert!(survey.contains("# Карта обследования: repo"), "{survey}");
    }

    #[test]
    fn run_out_option_relative_and_absolute() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = make_repo(tmp.path());
        // Относительный out — от корня репозитория.
        let rel = run(&repo, Some(Path::new("evidence"))).expect("run rel");
        assert!(rel.survey_path.ends_with("repo/evidence/survey.md"));
        // Абсолютный out.
        let abs_dir = tmp.path().join("abs-out");
        let abs = run(&repo, Some(&abs_dir)).expect("run abs");
        assert_eq!(abs.survey_path, abs_dir.join("survey.md"));
        // Несуществующий репозиторий — внятная ошибка.
        let err = run(&tmp.path().join("ghost"), None).expect_err("ghost");
        assert!(err.to_string().contains("ghost"), "{err}");
    }

    #[tokio::test]
    async fn reverse_survey_tool_ok_and_errors() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = make_repo(tmp.path());
        let tool = ReverseSurveyTool;
        assert_eq!(tool.spec().name, "reverse_survey");
        let ctx = ToolContext::new(
            tmp.path().to_path_buf(),
            std::sync::Arc::new(crate::config::Config::default()),
        );
        // Относительный путь от cwd.
        let out = tool
            .call(json!({"repo": "repo", "out": "rev"}), &ctx)
            .await
            .expect("call");
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.contains("[confirmed]"), "{}", out.content);
        assert!(out.content.contains("rev/survey.md"), "{}", out.content);
        assert!(repo.join("rev/survey.md").is_file());
        // Без repo — мягкая ошибка, не паника.
        let out = tool.call(json!({}), &ctx).await.expect("call empty");
        assert!(out.is_error);
        assert!(out.content.contains("repo"), "{}", out.content);
        // Несуществующий репозиторий — мягкая ошибка.
        let out = tool
            .call(json!({"repo": "ghost"}), &ctx)
            .await
            .expect("call ghost");
        assert!(out.is_error);
        assert!(out.content.contains("ghost"), "{}", out.content);
    }
}
