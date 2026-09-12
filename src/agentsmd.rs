//! Генерация и контроль `AGENTS.md` для репозиториев команд.
//!
//! AGENTS.md здесь — не ридми, а канал доставки архитектурного контроля:
//! файл **компилируется** из артефактов (spine-инварианты, CONSTRAINTS.yaml,
//! манифесты, структура репозитория), поэтому обновляем и проверяем на дрейф.
//!
//! Двухзонная схема: зона между `<!-- ARCH:GENERATED ... -->` и
//! `<!-- ARCH:END -->` перегенерируется; всё снаружи маркеров — рукописная
//! зона команды и никогда не затирается.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::config::Config;
use crate::control::LintIssue;
use crate::error::{HarnessError, Result};
use crate::tool::Tool;

/// Маркер начала сгенерированной зоны.
const BEGIN_MARKER: &str = "<!-- ARCH:GENERATED";
/// Маркер конца сгенерированной зоны.
const END_MARKER: &str = "<!-- ARCH:END -->";

/// Факты, обнаруженные в репозитории.
#[derive(Debug, Clone, Default)]
pub struct RepoFacts {
    /// Имя репозитория (имя каталога).
    pub name: String,
    /// Обнаруженные команды (напр. «Сборка» → «cargo build»).
    pub commands: Vec<(String, String)>,
    /// Стек (rust, node, java, go, python…).
    pub stack: Vec<String>,
    /// Каталоги верхнего уровня (карта).
    pub top_dirs: Vec<String>,
    /// CI-конфиги.
    pub ci: Vec<String>,
    /// Путь к spine (если есть).
    pub spine: Option<PathBuf>,
    /// Каталог ADR + число файлов.
    pub adr: Option<(PathBuf, usize)>,
    /// CONSTRAINTS.yaml (если есть — .arch-handoff или docs).
    pub constraints: Option<PathBuf>,
}

/// Инвариант из spine (блок AD-n).
#[derive(Debug, Clone)]
pub struct SpineInvariant {
    /// Идентификатор (AD-1).
    pub id: String,
    /// Заголовок.
    pub title: String,
    /// Проверяемое правило (поле Rule).
    pub rule: String,
}

/// Fitness-правило из CONSTRAINTS.yaml (минимальный разбор).
#[derive(Debug, Clone, Deserialize)]
struct ConstraintRule {
    name: String,
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    severity: Option<String>,
}

#[derive(Deserialize)]
struct ConstraintsDoc {
    #[serde(default)]
    rules: Vec<ConstraintRule>,
}

/// Собирает факты репозитория (манифесты, CI, карта, артефакты).
///
/// # Errors
/// `repo` не существует или не является каталогом.
pub fn scan_repo(repo: &Path) -> Result<RepoFacts> {
    if !repo.is_dir() {
        return Err(HarnessError::Agent(format!(
            "репозиторий {} не существует",
            repo.display()
        )));
    }
    let canonical = repo.canonicalize().unwrap_or_else(|_| repo.to_path_buf());
    let mut facts = RepoFacts {
        name: canonical
            .file_name()
            .map_or_else(|| "repo".into(), |n| n.to_string_lossy().to_string()),
        ..RepoFacts::default()
    };

    // Манифесты → стек и команды.
    if repo.join("Cargo.toml").is_file() {
        facts.stack.push("rust".into());
        facts.commands.extend([
            ("Сборка".into(), "cargo build".into()),
            ("Тесты".into(), "cargo test".into()),
            ("Линт".into(), "cargo clippy --all-targets".into()),
        ]);
    }
    if repo.join("package.json").is_file() {
        facts.stack.push("node".into());
        let scripts = std::fs::read_to_string(repo.join("package.json"))
            .ok()
            .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
            .and_then(|v| v.get("scripts")?.as_object().cloned())
            .unwrap_or_default();
        let pm = if repo.join("pnpm-lock.yaml").is_file() {
            "pnpm"
        } else {
            "npm"
        };
        for (label, key) in [("Сборка", "build"), ("Тесты", "test"), ("Линт", "lint")]
        {
            if scripts.contains_key(key) {
                facts
                    .commands
                    .push((label.into(), format!("{pm} run {key}")));
            }
        }
    }
    if repo.join("pom.xml").is_file() {
        facts.stack.push("java/maven".into());
        facts.commands.extend([
            ("Сборка".into(), "mvn -q compile".into()),
            ("Тесты".into(), "mvn -q test".into()),
        ]);
    }
    if repo.join("go.mod").is_file() {
        facts.stack.push("go".into());
        facts.commands.extend([
            ("Сборка".into(), "go build ./...".into()),
            ("Тесты".into(), "go test ./...".into()),
        ]);
    }
    if repo.join("pyproject.toml").is_file() {
        facts.stack.push("python".into());
        if repo.join("tests").is_dir() {
            facts.commands.push(("Тесты".into(), "pytest".into()));
        }
    }
    if repo.join("Makefile").is_file() {
        if let Ok(text) = std::fs::read_to_string(repo.join("Makefile")) {
            for line in text.lines() {
                if let Some(target) = line.strip_suffix(':') {
                    if matches!(target, "build" | "test" | "lint" | "check") {
                        facts
                            .commands
                            .push((format!("make {target}"), format!("make {target}")));
                    }
                }
            }
        }
    }

    // CI.
    for (path, name) in [
        (".github/workflows", "GitHub Actions"),
        (".gitlab-ci.yml", "GitLab CI"),
        (".ci", "CI"),
    ] {
        if repo.join(path).exists() {
            facts.ci.push(name.into());
        }
    }

    // Карта верхнего уровня.
    if let Ok(rd) = std::fs::read_dir(repo) {
        let mut dirs: Vec<String> = rd
            .flatten()
            .filter(|e| e.path().is_dir())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|n| !n.starts_with('.') && n != "target" && n != "node_modules")
            .collect();
        dirs.sort();
        dirs.truncate(12);
        facts.top_dirs = dirs;
    }

    // Архитектурные артефакты.
    for spine in ["docs/ARCHITECTURE-SPINE.md", "ARCHITECTURE-SPINE.md"] {
        if repo.join(spine).is_file() {
            facts.spine = Some(repo.join(spine));
            break;
        }
    }
    for adr in ["docs/adr", "adr"] {
        let dir = repo.join(adr);
        if dir.is_dir() {
            let count = std::fs::read_dir(&dir).map_or(0, |rd| {
                rd.flatten()
                    .filter(|e| e.path().extension().is_some_and(|x| x == "md"))
                    .count()
            });
            facts.adr = Some((dir, count));
            break;
        }
    }
    for c in [".arch-handoff/CONSTRAINTS.yaml", "docs/CONSTRAINTS.yaml"] {
        if repo.join(c).is_file() {
            facts.constraints = Some(repo.join(c));
            break;
        }
    }
    Ok(facts)
}

/// Извлекает инварианты из spine-файла (блоки `## AD-n. Заголовок` + поле Rule).
#[must_use]
pub fn parse_spine_invariants(spine: &Path) -> Vec<SpineInvariant> {
    let Ok(text) = std::fs::read_to_string(spine) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let mut current: Option<SpineInvariant> = None;
    let mut in_rule = false;
    for line in text.lines() {
        let t = line.trim();
        if let Some(rest) = t.strip_prefix("## AD-") {
            if let Some(cur) = current.take() {
                out.push(cur);
            }
            in_rule = false;
            // `AD-1: Заголовок` или `AD-1. Заголовок` — номер отсекаем по первому
            // разделителю (`.`/`:`/пробел).
            let sep = rest.find(['.', ':', ' ']).unwrap_or(rest.len());
            let (num, title) = rest.split_at(sep);
            current = Some(SpineInvariant {
                id: format!("AD-{}", num.trim()),
                title: title.trim_start_matches(['.', ':', ' ']).trim().to_string(),
                rule: String::new(),
            });
        } else if let Some(cur) = &mut current {
            let rule_marker = t
                .strip_prefix("Rule:")
                .or_else(|| t.strip_prefix("- **Rule:**"))
                .or_else(|| t.strip_prefix("**Rule:**"));
            if let Some(rule) = rule_marker {
                cur.rule = rule.trim().trim_end_matches("**").trim().to_string();
                in_rule = true;
            } else if in_rule && !t.is_empty() && !t.starts_with("- **") && !t.starts_with("## ") {
                // Продолжение многострочного Rule.
                if !cur.rule.is_empty() {
                    cur.rule.push(' ');
                }
                cur.rule.push_str(t.trim_end_matches("**").trim());
            } else {
                in_rule = false;
                if t.starts_with("## ") {
                    out.push(cur.clone());
                    current = None;
                }
            }
        }
    }
    if let Some(cur) = current {
        out.push(cur);
    }
    out
}

/// FNV-1a 64 — стабильный хэш входов для детекции дрейфа.
fn fnv1a(text: &str) -> u64 {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for b in text.as_bytes() {
        hash ^= u64::from(*b);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

/// Хэш входов генерации (spine + constraints + факты) — меняется вход → меняется хэш.
fn inputs_hash(repo: &Path, facts: &RepoFacts) -> Result<u64> {
    let mut buf = String::new();
    if let Some(spine) = &facts.spine {
        buf.push_str(&std::fs::read_to_string(spine).map_err(|e| HarnessError::io(spine, e))?);
    }
    if let Some(c) = &facts.constraints {
        buf.push_str(&std::fs::read_to_string(c).map_err(|e| HarnessError::io(c, e))?);
    }
    let _ = write!(
        buf,
        "{:?}|{:?}|{:?}",
        facts.commands, facts.top_dirs, facts.stack
    );
    let _ = repo;
    Ok(fnv1a(&buf))
}

/// Компилирует содержимое сгенерированной зоны AGENTS.md.
///
/// # Errors
/// Ошибка чтения файлов-источников (spine, CONSTRAINTS.yaml) при подсчёте хэша.
pub fn render_generated(repo: &Path, facts: &RepoFacts) -> Result<String> {
    let hash = inputs_hash(repo, facts)?;
    let mut out = String::new();
    let _ = writeln!(out, "<!-- inputs: {hash:016x} -->");
    out.push_str("> Сгенерировано харнессом `arch-ml` (`arch-ml agents-md refresh`). Не редактируйте\n> внутри маркеров — правьте источники (spine, CONSTRAINTS.yaml) или зону снаружи.\n\n");

    out.push_str("## Команды\n\n");
    if facts.commands.is_empty() {
        out.push_str("- (манифесты не обнаружены — заполните вручную вне маркеров)\n");
    } else {
        for (label, cmd) in &facts.commands {
            let _ = writeln!(out, "- {label}: `{cmd}`");
        }
    }
    if !facts.ci.is_empty() {
        let _ = writeln!(out, "- CI: {}", facts.ci.join(", "));
    }
    out.push('\n');

    out.push_str("## Инварианты архитектуры (нарушать нельзя)\n\n");
    match &facts.spine {
        Some(spine) => {
            let inv = parse_spine_invariants(spine);
            if inv.is_empty() {
                out.push_str("- spine есть, но блоков AD-n не найдено\n");
            }
            for i in &inv {
                let _ = write!(out, "- **{} {}**", i.id, i.title);
                if !i.rule.is_empty() {
                    let _ = write!(out, " — Rule: `{}`", i.rule);
                }
                out.push('\n');
            }
            let _ = write!(out, "\nПолный текст: `{}`\n", rel(repo, spine));
        }
        None => {
            out.push_str("- spine не обнаружен; для Standard/Critical маршрутов он обязателен\n");
        }
    }
    out.push('\n');

    out.push_str("## Запреты и fitness-правила\n\n");
    match &facts.constraints {
        Some(c) => {
            let text = std::fs::read_to_string(c).map_err(|e| HarnessError::io(c, e))?;
            let doc: ConstraintsDoc = serde_yaml_ng::from_str(&text).map_err(HarnessError::Yaml)?;
            for r in &doc.rules {
                let sev = r.severity.as_deref().unwrap_or("error");
                let _ = writeln!(out, "- `{}` ({}, {}) ", r.name, r.kind, sev);
            }
            let _ = write!(
                out,
                "\nПроверка: `arch-ml control check .` — источник `{}`\n",
                rel(repo, c)
            );
        }
        None => {
            out.push_str("- CONSTRAINTS.yaml не найден (`arch-ml handoff` создаёт стартовый)\n");
        }
    }
    out.push('\n');

    out.push_str("## Карта репозитория\n\n");
    let _ = writeln!(
        out,
        "- Стек: {}",
        if facts.stack.is_empty() {
            "не определён".into()
        } else {
            facts.stack.join(", ")
        }
    );
    if !facts.top_dirs.is_empty() {
        let _ = writeln!(out, "- Каталоги: {}", facts.top_dirs.join(", "));
    }
    if let Some((adr, n)) = &facts.adr {
        let _ = writeln!(
            out,
            "- ADR: `{}` ({n} шт.) — решения читаем ДО изменения затронутых мест",
            rel(repo, adr)
        );
    }
    out.push('\n');

    out.push_str("## Стоп-условия: когда остановиться и эскалировать архитектору\n\n");
    out.push_str(
        "Прекратите работу и запросите решение архитектора (A3), если изменение затрагивает:\n",
    );
    out.push_str("- API/data-контракт, схему данных, security boundary / trust zone;\n");
    out.push_str("- новый компонент/хранилище/вендора, cross-domain интеграцию;\n");
    out.push_str("- необратимую миграцию, RTO/RPO, финансово значимые потоки.\n");
    out.push_str(
        "Маршрут значимости: `arch-ml control score --trigger …` (Fast/Standard/Critical).\n",
    );
    let sha = crate::managed::Sha256::of_text(&out);
    Ok(crate::managed::render_block(
        "arch-generated",
        1,
        &sha,
        Some("agents-md"),
        &out,
    ))
}

/// Относительный путь (для ссылок внутри AGENTS.md).
fn rel(repo: &Path, path: &Path) -> String {
    path.strip_prefix(repo)
        .map_or_else(|_| path.display().to_string(), |p| p.display().to_string())
}

/// Итог генерации/обновления.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AgentsMdReport {
    /// Путь к AGENTS.md.
    pub path: PathBuf,
    /// Действие: created|refreshed|appended.
    pub action: String,
    /// Число инвариантов в сгенерированной зоне.
    pub invariants: usize,
    /// Есть ли fitness-правила.
    pub has_constraints: bool,
}

/// Генерирует или обновляет AGENTS.md в репозитории.
///
/// Существующая рукописная часть сохраняется: заменяется только зона между
/// маркерами; файла нет — создаётся с шапкой; маркеров нет — сгенерированная
/// зона дописывается в конец.
///
/// # Errors
/// Репозиторий недоступен, ошибки чтения источников/записи.
pub fn generate(repo: &Path) -> Result<AgentsMdReport> {
    let facts = scan_repo(repo)?;
    let zone = render_generated(repo, &facts)?;
    let invariants = facts
        .spine
        .as_ref()
        .map_or(0, |s| parse_spine_invariants(s).len());
    let path = repo.join("AGENTS.md");
    let (content, action) = if let Ok(existing) = std::fs::read_to_string(&path) {
        splice(&existing, &zone)?
    } else {
        let header = format!(
            "# AGENTS.md — {}\n\n> Инструкции для агентов и разработчиков. Рукописная зона — вне маркеров ARCH.\n\n",
            facts.name
        );
        (format!("{header}{zone}"), "created")
    };
    crate::managed::atomic_write(&path, &content)?;
    Ok(AgentsMdReport {
        path,
        action: action.into(),
        invariants,
        has_constraints: facts.constraints.is_some(),
    })
}

/// Сращивает существующий файл со свежей managed-зоной (блок `id=arch-generated`).
/// Ручная правка внутри блока → `HashMismatch` (не тихая потеря).
fn splice(existing: &str, zone: &str) -> Result<(String, &'static str)> {
    let new_blocks = crate::managed::parse_blocks(zone)?;
    let new_block = new_blocks
        .into_iter()
        .find(|b| b.id == "arch-generated")
        .ok_or_else(|| {
            HarnessError::Control("зона не содержит managed-блок arch-generated".into())
        })?;

    let old = crate::managed::parse_blocks(existing).unwrap_or_default();
    if let Some(ob) = old.iter().find(|b| b.id == "arch-generated") {
        let patch = crate::managed::Patch {
            target: std::path::PathBuf::new(),
            base_hash: crate::managed::HashGuard::Force,
            block_ops: vec![crate::managed::BlockOp::Replace {
                id: "arch-generated".into(),
                guard: crate::managed::HashGuard::Expect(ob.hash.clone()),
                content: new_block.body,
                source: Some("agents-md".into()),
                upsert: false,
                anchor: crate::managed::Anchor::Eof,
            }],
            section_ops: vec![],
        };
        let (out, _report) = crate::managed::apply(existing, &patch)?;
        return Ok((out, "refreshed"));
    }

    // Миграция: managed-блока нет. Старая ARCH-зона → заменить текстом;
    // маркеров нет — дописать в конец.
    let begin = existing.find(BEGIN_MARKER);
    let end = existing.find(END_MARKER);
    if let (Some(b), Some(e)) = (begin, end) {
        if b < e {
            let after = e + END_MARKER.len();
            let mut out = String::with_capacity(existing.len() + zone.len());
            out.push_str(&existing[..b]);
            out.push_str(zone.trim_end());
            out.push('\n');
            out.push_str(existing[after..].trim_start_matches('\n'));
            return Ok((out, "refreshed"));
        }
    }
    let mut out = existing.trim_end().to_string();
    out.push_str("\n\n");
    out.push_str(zone);
    Ok((out, "appended"))
}

/// Извлекает хэш входов из managed-зоны (комментарий `<!-- inputs: <hex> -->`).
fn embedded_hash(existing: &str) -> Option<u64> {
    existing.lines().find_map(|l| {
        let rest = l.trim().strip_prefix("<!-- inputs:")?;
        let hex = rest.trim_end_matches("-->").trim();
        u64::from_str_radix(hex, 16).ok()
    })
}

/// Линтер AGENTS.md: наличие зоны, свежесть (хэш входов), валидность ссылок,
/// заглушки.
///
/// # Errors
/// AGENTS.md отсутствует/не читается.
pub fn lint(repo: &Path) -> Result<Vec<LintIssue>> {
    let path = repo.join("AGENTS.md");
    let text = std::fs::read_to_string(&path).map_err(|e| HarnessError::io(&path, e))?;
    let mut issues = Vec::new();
    let push =
        |issues: &mut Vec<LintIssue>, line: usize, rule: &str, message: String, severity: &str| {
            issues.push(LintIssue {
                file: path.clone(),
                line,
                rule: rule.into(),
                message,
                severity: severity.into(),
            });
        };

    if !text.contains("<!-- inputs:") {
        push(
            &mut issues,
            0,
            "no_generated_zone",
            "нет сгенерированной зоны — `arch-ml agents-md refresh` добавит её, рукописное сохранится"
                .into(),
            "warn",
        );
        return Ok(issues);
    }

    // Свежесть: хэш входов.
    let facts = scan_repo(repo)?;
    let current = inputs_hash(repo, &facts)?;
    match embedded_hash(&text) {
        Some(h) if h == current => {}
        Some(_) => push(
            &mut issues,
            0,
            "stale",
            "источники изменились с последней генерации — запустите `arch-ml agents-md refresh`"
                .into(),
            "error",
        ),
        None => push(
            &mut issues,
            0,
            "no_hash",
            "нет хэша в маркере — refresh".into(),
            "warn",
        ),
    }

    // Ссылки на артефакты существуют.
    if let Some(spine) = &facts.spine {
        if !text.contains(&rel(repo, spine)) && text.contains("ARCHITECTURE-SPINE") {
            // ссылается по имени, но не по пути — не ошибка
        }
    }
    if text.contains("docs/ARCHITECTURE-SPINE.md") && facts.spine.is_none() {
        push(
            &mut issues,
            0,
            "broken_link",
            "ссылка на spine, но файл не найден".into(),
            "error",
        );
    }

    // Заглушки.
    for (n, line) in text.lines().enumerate() {
        if line.contains("TODO") || line.contains("TBD") || line.contains("FIXME") {
            push(
                &mut issues,
                n + 1,
                "stub_marker",
                format!("заглушка: {}", line.trim()),
                "warn",
            );
        }
    }
    Ok(issues)
}

/// Прогон линтера по реестру репозиториев (файл со списком путей, по одному на строку).
///
/// # Errors
/// Реестр не читается.
pub fn lint_registry(registry_file: &Path) -> Result<Vec<(PathBuf, Vec<LintIssue>)>> {
    let text =
        std::fs::read_to_string(registry_file).map_err(|e| HarnessError::io(registry_file, e))?;
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let repo = PathBuf::from(line);
        match lint(&repo) {
            Ok(issues) => out.push((repo, issues)),
            Err(e) => out.push((
                repo.clone(),
                vec![LintIssue {
                    file: repo,
                    line: 0,
                    rule: "lint_error".into(),
                    message: e.to_string(),
                    severity: "error".into(),
                }],
            )),
        }
    }
    Ok(out)
}

/// Инструменты домена: `agentsmd_generate`, `agentsmd_lint`.
#[must_use]
pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>> {
    let _ = cfg;
    vec![Arc::new(AgentsMdGenerateTool), Arc::new(AgentsMdLintTool)]
}

/// Инструмент `agentsmd_generate`.
struct AgentsMdGenerateTool;

#[async_trait::async_trait]
impl Tool for AgentsMdGenerateTool {
    fn spec(&self) -> crate::llm::ToolSpec {
        crate::llm::ToolSpec {
            name: "agentsmd_generate".into(),
            description:
                "Сгенерировать/обновить AGENTS.md репозитория из архитектурных артефактов \
                          (spine, CONSTRAINTS, манифесты). Рукописная зона сохраняется."
                    .into(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": {"repo": {"type": "string", "description": "путь к репозиторию"}},
                "required": ["repo"]
            }),
        }
    }

    async fn call(
        &self,
        args: serde_json::Value,
        ctx: &crate::tool::ToolContext,
    ) -> Result<crate::tool::ToolOutput> {
        let repo = args.get("repo").and_then(|r| r.as_str()).unwrap_or(".");
        let report = generate(&ctx.resolve(repo))?;
        Ok(crate::tool::ToolOutput::ok(format!(
            "AGENTS.md: {} ({}), инвариантов: {}, fitness: {}",
            report.path.display(),
            report.action,
            report.invariants,
            if report.has_constraints {
                "да"
            } else {
                "нет"
            }
        )))
    }
}

/// Инструмент `agentsmd_lint`.
struct AgentsMdLintTool;

#[async_trait::async_trait]
impl Tool for AgentsMdLintTool {
    fn spec(&self) -> crate::llm::ToolSpec {
        crate::llm::ToolSpec {
            name: "agentsmd_lint".into(),
            description:
                "Проверить AGENTS.md репозитория: свежесть (дрейф источников), ссылки, заглушки"
                    .into(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": {"repo": {"type": "string", "description": "путь к репозиторию"}},
                "required": ["repo"]
            }),
        }
    }

    async fn call(
        &self,
        args: serde_json::Value,
        ctx: &crate::tool::ToolContext,
    ) -> Result<crate::tool::ToolOutput> {
        let repo = args.get("repo").and_then(|r| r.as_str()).unwrap_or(".");
        let issues = lint(&ctx.resolve(repo))?;
        if issues.is_empty() {
            return Ok(crate::tool::ToolOutput::ok(
                "AGENTS.md свежий, нарушений нет",
            ));
        }
        let mut out = String::new();
        for i in &issues {
            let _ = writeln!(out, "[{}] {} — {}", i.severity, i.rule, i.message);
        }
        Ok(crate::tool::ToolOutput::ok(out))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_repo(root: &Path) -> PathBuf {
        let repo = root.join("payments-core");
        std::fs::create_dir_all(repo.join("src")).expect("src");
        std::fs::create_dir_all(repo.join(".arch-handoff")).expect("handoff");
        std::fs::create_dir_all(repo.join("docs/adr")).expect("adr");
        std::fs::write(repo.join("Cargo.toml"), "[package]\nname=\"pc\"\n").expect("cargo");
        std::fs::write(repo.join("docs/adr/ADR-001-x.md"), "# ADR-001\n").expect("adr file");
        std::fs::write(
            repo.join("docs/ARCHITECTURE-SPINE.md"),
            "# Spine\n\n## AD-1. Формат id\nBinds: все\nPrevents: рассинхрон\nRule: `grep -r serial src/` пуст\nStatus: Accepted\n",
        )
        .expect("spine");
        std::fs::write(
            repo.join(".arch-handoff/CONSTRAINTS.yaml"),
            "rules:\n  - name: no-unwrap\n    type: must_not_contain\n    glob: \"src/**\"\n    pattern: 'unwrap\\('\n    severity: warn\n",
        )
        .expect("constraints dir missing");
        repo
    }

    #[test]
    fn scan_detects_rust_repo_with_artifacts() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = fixture_repo(tmp.path());
        let facts = scan_repo(&repo).expect("scan");
        assert_eq!(facts.name, "payments-core");
        assert!(facts.stack.contains(&"rust".to_string()));
        assert!(
            facts
                .commands
                .iter()
                .any(|(l, c)| l == "Сборка" && c == "cargo build")
        );
        assert!(facts.spine.is_some());
        assert_eq!(facts.adr.as_ref().map(|(_, n)| *n), Some(1));
        assert!(facts.constraints.is_some());
    }

    #[test]
    fn generate_then_refresh_preserves_handwritten_zone() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = fixture_repo(tmp.path());
        let r1 = generate(&repo).expect("gen1");
        assert_eq!(r1.action, "created");
        assert_eq!(r1.invariants, 1);

        // Рукописная зона команды.
        let path = repo.join("AGENTS.md");
        let mut text = std::fs::read_to_string(&path).expect("read");
        text.push_str("\n## Наши договорённости\n\n- коммиты на русском\n");
        std::fs::write(&path, &text).expect("write");

        // Меняем источник → refresh обновляет зону, рукописное цело.
        std::fs::write(
            repo.join("docs/ARCHITECTURE-SPINE.md"),
            "# Spine\n\n## AD-1. Формат id\nBinds: все\nPrevents: рассинхрон\nRule: новое правило\n\n## AD-2. Шина\nBinds: все\nPrevents: зоопарк\nRule: kafka only\n",
        )
        .expect("spine2");
        let r2 = generate(&repo).expect("gen2");
        assert_eq!(r2.action, "refreshed");
        assert_eq!(r2.invariants, 2);
        let text = std::fs::read_to_string(&path).expect("read2");
        assert!(
            text.contains("коммиты на русском"),
            "рукописная зона затёрта"
        );
        assert!(text.contains("AD-2"), "новый инвариант не попал");
        assert_eq!(
            text.matches("agent:managed:begin").count(),
            1,
            "двойная зона"
        );
    }

    #[test]
    fn lint_detects_staleness_and_stubs() {
        let tmp = tempfile::tempdir().expect("tmp");
        let repo = fixture_repo(tmp.path());
        generate(&repo).expect("gen");
        assert!(
            lint(&repo).expect("lint fresh").is_empty(),
            "свежий файл чист"
        );

        // Дрейф: источник изменился.
        std::fs::write(
            repo.join("docs/ARCHITECTURE-SPINE.md"),
            "# Spine\n\n## AD-9. Новое\nBinds: всех\nPrevents: хаос\nRule: x\n",
        )
        .expect("spine3");
        let issues = lint(&repo).expect("lint stale");
        assert!(
            issues
                .iter()
                .any(|i| i.rule == "stale" && i.severity == "error"),
            "{issues:?}"
        );

        // Свежесть восстанавливается refresh'ем.
        generate(&repo).expect("regen");
        let path = repo.join("AGENTS.md");
        let mut text = std::fs::read_to_string(&path).expect("read");
        text.push_str("\nTODO: дописать\n");
        std::fs::write(&path, text).expect("write");
        let issues = lint(&repo).expect("lint stub");
        assert!(issues.iter().any(|i| i.rule == "stub_marker"));
        assert!(
            !issues.iter().any(|i| i.rule == "stale"),
            "после refresh дрейфа нет"
        );
    }

    #[test]
    fn splice_appends_zone_when_no_markers() {
        let body = "тело";
        let hash = crate::managed::Sha256::of_text(body);
        let zone = crate::managed::render_block("arch-generated", 1, &hash, None, body);
        let (out, action) = splice("# Рукописный файл\n\nТекст команды.\n", &zone).unwrap();
        assert_eq!(action, "appended");
        assert!(out.contains("Текст команды."));
        assert!(out.contains("arch-generated"));
    }

    #[test]
    fn splice_detects_manual_edit_in_managed_block() {
        let body = "тело v2";
        let hash = crate::managed::Sha256::of_text(body);
        let zone = crate::managed::render_block("arch-generated", 2, &hash, None, body);
        // Маркер заявляет один хэш, тело отредактировано вручную — HashMismatch.
        let existing = "# Рукописный\n\n<!-- agent:managed:begin id=arch-generated ver=1 hash=sha256:0000000000000000000000000000000000000000000000000000000000000000 -->\nтело (ручная правка)\n<!-- agent:managed:end id=arch-generated -->\n";
        let err = splice(existing, &zone).unwrap_err();
        assert!(
            err.to_string().to_lowercase().contains("hash"),
            "ожидался HashMismatch, получено: {err}"
        );
    }
}
