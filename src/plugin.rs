//! Плагины: пакеты «скиллы + MCP» по открытому стандарту agent-plugins.org.
//!
//! Layout плагина:
//! ```text
//! my-plugin/
//! ├── plugin.json            # манифест ($schema, name, version, description, keywords)
//! ├── skills/<name>/SKILL.md # Agent Skills (frontmatter name/description + тело)
//! ├── mcp.json               # MCP-серверы (стандарт agent-plugins.org)
//! └── .mcp.json              # то же, вариант Claude Code — поддерживаются оба
//! ```
//!
//! Субагенты (`agents/*.md`) исполняются как фоновые задачи (модуль
//! [`crate::subagent`]: `subagent_run`/`subagent_list`/`subagent_result`);
//! хуки (`hooks/hooks.json`) — клиентское расширение, исполняются при
//! `plugins.include_hooks = true` (конвертер — `hooks::specs_from_plugin_json`);
//! в `arch-ml plugins show`, но не исполняет.

use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::config::Config;
use crate::error::{HarnessError, Result};
use crate::mcp::McpServerConfig;
use crate::tool::Tool;

/// Манифест плагина (`plugin.json`, стандарт agent-plugins.org 1.0).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PluginManifest {
    /// Имя плагина.
    pub name: String,
    /// Версия.
    #[serde(default)]
    pub version: String,
    /// Описание (может быть длинным — показывать с усечением).
    #[serde(default)]
    pub description: String,
    /// Ключевые слова.
    #[serde(default)]
    pub keywords: Vec<String>,
}

/// Метаданные скилла (из frontmatter SKILL.md).
#[derive(Debug, Clone)]
pub struct SkillMeta {
    /// Имя скилла (frontmatter `name`).
    pub name: String,
    /// Описание (frontmatter `description`).
    pub description: String,
    /// Плагин-владелец.
    pub plugin: String,
    /// Путь к SKILL.md.
    pub path: PathBuf,
}

/// Обнаруженный плагин.
#[derive(Debug, Clone)]
pub struct Plugin {
    /// Корень плагина.
    pub dir: PathBuf,
    /// Манифест (возможно, синтезированный из имени каталога).
    pub manifest: PluginManifest,
    /// Скиллы плагина.
    pub skills: Vec<SkillMeta>,
}

impl Plugin {
    /// Дополнительные компоненты плагина: субагенты (`agents/*.md`) и
    /// хуки (`hooks/hooks.json`) — пути к файлам, если присутствуют.
    #[must_use]
    pub fn extra_components(&self) -> Vec<PathBuf> {
        let mut out = Vec::new();
        let agents = self.dir.join("agents");
        if let Ok(rd) = std::fs::read_dir(&agents) {
            for e in rd.flatten() {
                let p = e.path();
                if p.extension().is_some_and(|x| x == "md") {
                    out.push(p);
                }
            }
        }
        let hooks = self.dir.join("hooks").join("hooks.json");
        if hooks.is_file() {
            out.push(hooks);
        }
        out.sort();
        out
    }
}

/// Хит поиска по скиллам.
#[derive(Debug, Clone)]
pub struct SkillHit {
    /// Метаданные скилла.
    pub meta: SkillMeta,
    /// Скор (больше — лучше).
    pub score: f64,
    /// Сниппет из тела SKILL.md (пуст, если совпадение только в метаданных).
    pub snippet: String,
}

/// Обнаруживает плагины в каталогах. Битые манифесты/скиллы пропускаются —
/// харнесс не падает из-за одного битого плагина.
#[must_use]
pub fn discover(dirs: &[PathBuf]) -> Vec<Plugin> {
    let mut plugins = Vec::new();
    let mut seen_skills: HashSet<String> = HashSet::new();
    for dir in dirs {
        let Ok(rd) = std::fs::read_dir(dir) else {
            continue;
        };
        let mut entries: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        entries.sort();
        for entry in entries {
            if let Some(mut plugin) = load_plugin(&entry) {
                plugin.skills.retain(|s| seen_skills.insert(s.name.clone()));
                plugin.skills.sort_by(|a, b| a.name.cmp(&b.name));
                plugins.push(plugin);
            }
        }
    }
    plugins.sort_by(|a, b| a.manifest.name.cmp(&b.manifest.name));
    plugins
}

/// Загружает один плагин: манифест из plugin.json или синтез из каталога.
fn load_plugin(dir: &Path) -> Option<Plugin> {
    let manifest_path = dir.join("plugin.json");
    let manifest = if manifest_path.is_file() {
        if let Some(m) = std::fs::read_to_string(&manifest_path)
            .ok()
            .and_then(|t| serde_json::from_str::<PluginManifest>(&t).ok())
        {
            m
        } else {
            tracing::warn!("plugin: битый манифест {}", manifest_path.display());
            return None;
        }
    } else if dir.join("skills").is_dir()
        || dir.join("hooks").join("hooks.json").is_file()
        || dir.join("agents").is_dir()
        || dir.join("mcp.json").is_file()
        || dir.join(".mcp.json").is_file()
    {
        // Плагин без манифеста: имя синтезируется из каталога. Признак
        // плагина — любой носитель: skills/, hooks/hooks.json, agents/, mcp.json.
        let name = dir.file_name()?.to_string_lossy().to_string();
        PluginManifest {
            name,
            version: "0.0.0".into(),
            ..PluginManifest::default()
        }
    } else {
        return None;
    };
    let skills_dir = dir.join("skills");
    let mut skills = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&skills_dir) {
        let mut entries: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        entries.sort();
        for skill_dir in entries {
            let skill_md = skill_dir.join("SKILL.md");
            if !skill_md.is_file() {
                continue;
            }
            if let Some((name, description)) = parse_frontmatter(&skill_md) {
                skills.push(SkillMeta {
                    name,
                    description,
                    plugin: manifest.name.clone(),
                    path: skill_md,
                });
            }
        }
    }
    Some(Plugin {
        dir: dir.to_path_buf(),
        manifest,
        skills,
    })
}

/// Парсит YAML-frontmatter SKILL.md: `---\nname: …\ndescription: …\n---`.
///
/// Построчный толерантный разбор (`serde_yaml_ng` падает на `:` внутри
/// description — в дикой природе такое встречается): ключи `name`/`description`,
/// значение — до конца строки; folded-формы `>-`/`>`/`|` собирают следующие
/// indented-строки.
fn parse_frontmatter(path: &Path) -> Option<(String, String)> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut lines = text.lines();
    if lines.next()?.trim() != "---" {
        return None;
    }
    let mut name = String::new();
    let mut description = String::new();
    let mut current: Option<&str> = None; // чьи продолжения собираем
    for line in lines {
        if line.trim() == "---" {
            break;
        }
        if line.starts_with(' ') || line.starts_with('\t') {
            // Продолжение folded-значения.
            if let Some(which) = current {
                let t = line.trim();
                if !t.is_empty() {
                    let target = if which == "name" {
                        &mut name
                    } else {
                        &mut description
                    };
                    if !target.is_empty() {
                        target.push(' ');
                    }
                    target.push_str(t);
                }
            }
            continue;
        }
        if let Some(v) = line.strip_prefix("name:") {
            name = v.trim().trim_matches('"').trim_matches('\'').to_string();
            current = Some("name");
        } else if let Some(v) = line.strip_prefix("description:") {
            let v = v.trim();
            if matches!(v, ">-" | ">" | "|" | "|-") {
                current = Some("description");
            } else {
                description = v.trim_matches('"').trim_matches('\'').to_string();
                current = Some("description");
            }
        } else {
            current = None;
        }
    }
    if name.is_empty() {
        return None;
    }
    Some((name, description))
}

/// Поиск по библиотеке скиллов: name ×12, description ×6, keywords ×4,
/// тело ×1 (лениво, файлы ≤1 МБ). Сниппет — первое вхождение в теле.
#[must_use]
pub fn search(plugins: &[Plugin], query: &str, limit: usize) -> Vec<SkillHit> {
    let terms: Vec<String> = query
        .split_whitespace()
        .map(str::to_lowercase)
        .filter(|t| t.chars().count() > 2)
        .collect();
    if terms.is_empty() {
        return Vec::new();
    }
    let mut body_cache: HashMap<PathBuf, Option<String>> = HashMap::new();
    let mut hits = Vec::new();
    for p in plugins {
        for s in &p.skills {
            let name_l = s.name.to_lowercase();
            let desc_l = s.description.to_lowercase();
            let mut score = 0.0;
            for t in &terms {
                score += 12.0 * name_l.matches(t.as_str()).count() as f64;
                score += 6.0 * desc_l.matches(t.as_str()).count() as f64;
                score += 4.0
                    * p.manifest
                        .keywords
                        .iter()
                        .filter(|k| k.to_lowercase().contains(t.as_str()))
                        .count() as f64;
            }
            // Тело читаем, только если метаданные дали мало или нужен сниппет.
            let body = body_cache.entry(s.path.clone()).or_insert_with(|| {
                let meta = std::fs::metadata(&s.path).ok()?;
                if meta.len() > 1_048_576 {
                    return None;
                }
                std::fs::read_to_string(&s.path).ok()
            });
            let mut snippet = String::new();
            if let Some(body) = body {
                let body_l = body.to_lowercase();
                for t in &terms {
                    score += 1.0 * body_l.matches(t.as_str()).count().min(30) as f64;
                }
                if snippet.is_empty() {
                    snippet = make_snippet(body, &terms);
                }
            }
            if score > 0.0 {
                hits.push(SkillHit {
                    meta: s.clone(),
                    score,
                    snippet,
                });
            }
        }
    }
    hits.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    hits.truncate(limit);
    hits
}

/// Сниппет: строка первого вхождения ±1 строка контекста, матч — `>>> `.
fn make_snippet(body: &str, terms: &[String]) -> String {
    let lines: Vec<&str> = body.lines().collect();
    for (i, line) in lines.iter().enumerate() {
        let low = line.to_lowercase();
        if terms.iter().any(|t| low.contains(t.as_str())) {
            let from = i.saturating_sub(1);
            let to = (i + 2).min(lines.len());
            let mut out = String::new();
            for (j, l) in lines.iter().enumerate().take(to).skip(from) {
                let marker = if j == i { ">>> " } else { "    " };
                out.push_str(marker);
                out.push_str(l);
                out.push('\n');
            }
            return out.trim_end().to_string();
        }
    }
    String::new()
}

/// Скилл по точному имени (при дублях — первый по порядку каталогов).
#[must_use]
pub fn skill_by_name<'a>(plugins: &'a [Plugin], name: &str) -> Option<&'a SkillMeta> {
    plugins
        .iter()
        .flat_map(|p| p.skills.iter())
        .find(|s| s.name == name)
}

/// Полный текст скилла + постскриптум со списком `references/`.
///
/// # Errors
/// Файл не читается.
pub fn load_skill(meta: &SkillMeta) -> Result<String> {
    let mut text =
        std::fs::read_to_string(&meta.path).map_err(|e| HarnessError::io(&meta.path, e))?;
    let refs_dir = meta
        .path
        .parent()
        .unwrap_or(Path::new("."))
        .join("references");
    if refs_dir.is_dir() {
        let mut files: Vec<String> = Vec::new();
        if let Ok(rd) = std::fs::read_dir(&refs_dir) {
            files = rd
                .flatten()
                .map(|e| e.file_name().to_string_lossy().to_string())
                .collect();
        }
        files.sort();
        if !files.is_empty() {
            let _ = write!(
                text,
                "\n\n---\nМатериалы скилла ({}): {}\n",
                refs_dir.display(),
                files.join(", ")
            );
        }
    }
    Ok(text)
}

/// MCP-серверы, объявленные плагинами: `mcpServers` в plugin.json,
/// `mcp.json` (стандарт) и `.mcp.json` (Claude Code) в корне плагина.
/// Имена — `plugin.server` (точка: `__` зарезервирован в mcp.rs).
#[must_use]
pub fn mcp_servers(plugins: &[Plugin]) -> Vec<McpServerConfig> {
    let mut out = Vec::new();
    for p in plugins {
        for candidate in [
            p.dir.join("plugin.json"),
            p.dir.join("mcp.json"),
            p.dir.join(".mcp.json"),
        ] {
            let Ok(text) = std::fs::read_to_string(&candidate) else {
                continue;
            };
            let Ok(json) = serde_json::from_str::<serde_json::Value>(&text) else {
                continue;
            };
            let Some(servers) = json.get("mcpServers").and_then(|s| s.as_object()) else {
                continue;
            };
            for (name, spec) in servers {
                let Some(command) = spec.get("command").and_then(|c| c.as_str()) else {
                    continue;
                };
                let args = spec
                    .get("args")
                    .and_then(|a| a.as_array())
                    .map(|a| {
                        a.iter()
                            .filter_map(|v| v.as_str().map(str::to_string))
                            .collect()
                    })
                    .unwrap_or_default();
                let env = spec
                    .get("env")
                    .and_then(|e| e.as_object())
                    .map(|m| {
                        m.iter()
                            .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string())))
                            .collect()
                    })
                    .unwrap_or_default();
                out.push(McpServerConfig {
                    name: format!("{}.{name}", p.manifest.name),
                    command: command.to_string(),
                    args,
                    env,
                });
            }
        }
    }
    out
}

/// Инструменты домена: `skill_search`, `skill_load`, `plugin_list`.
#[must_use]
pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>> {
    let dirs = cfg.plugins.dirs.clone();
    vec![
        Arc::new(SkillSearchTool { dirs: dirs.clone() }),
        Arc::new(SkillLoadTool { dirs: dirs.clone() }),
        Arc::new(PluginListTool { dirs }),
    ]
}

/// Общий хелпер инструментов: поиск плагинов по каталогам.
fn discover_dirs(dirs: &[PathBuf]) -> Vec<Plugin> {
    discover(dirs)
}

/// Инструмент `skill_search`: поиск по библиотеке скиллов.
struct SkillSearchTool {
    dirs: Vec<PathBuf>,
}

#[async_trait::async_trait]
impl Tool for SkillSearchTool {
    fn spec(&self) -> crate::llm::ToolSpec {
        crate::llm::ToolSpec {
            name: "skill_search".into(),
            description:
                "Поиск по библиотеке архитектурных скиллов (плагины: навыки, MCP, субагенты). \
                          Вызывай, когда нужна методика по теме (ADR, saga, NFR, рубрики…)"
                    .into(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "поисковый запрос"},
                    "limit": {"type": "integer", "description": "максимум результатов (≤20)"}
                },
                "required": ["query"]
            }),
        }
    }

    async fn call(
        &self,
        args: serde_json::Value,
        _ctx: &crate::tool::ToolContext,
    ) -> Result<crate::tool::ToolOutput> {
        let query = args
            .get("query")
            .and_then(|q| q.as_str())
            .ok_or_else(|| HarnessError::Tool("skill_search: нет аргумента query".into()))?;
        let limit = args
            .get("limit")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(8)
            .min(20) as usize;
        let plugins = discover_dirs(&self.dirs);
        let hits = search(&plugins, query, limit);
        if hits.is_empty() {
            return Ok(crate::tool::ToolOutput::ok(format!(
                "по запросу '{query}' ничего не найдено (скиллов в индексе: {})",
                plugins.iter().map(|p| p.skills.len()).sum::<usize>()
            )));
        }
        let mut out = String::new();
        for h in &hits {
            let _ = write!(
                out,
                "── {} [{}] (score {:.1})\n   {}\n",
                h.meta.name,
                h.meta.plugin,
                h.score,
                h.meta.description.lines().next().unwrap_or("")
            );
            if !h.snippet.is_empty() {
                out.push_str(&h.snippet);
                out.push('\n');
            }
        }
        out.push_str("\nПолный текст: skill_load(name).");
        Ok(crate::tool::ToolOutput::ok(out).truncated(12_000))
    }
}

/// Инструмент `skill_load`: полный текст скилла.
struct SkillLoadTool {
    dirs: Vec<PathBuf>,
}

#[async_trait::async_trait]
impl Tool for SkillLoadTool {
    fn spec(&self) -> crate::llm::ToolSpec {
        crate::llm::ToolSpec {
            name: "skill_load".into(),
            description: "Загрузить полный текст архитектурного скилла по точному имени \
                          (после skill_search). Скилл — методика: приёмы, чек-листы, антипаттерны."
                .into(),
            parameters: serde_json::json!({
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "точное имя скилла"}
                },
                "required": ["name"]
            }),
        }
    }

    async fn call(
        &self,
        args: serde_json::Value,
        _ctx: &crate::tool::ToolContext,
    ) -> Result<crate::tool::ToolOutput> {
        let name = args
            .get("name")
            .and_then(|q| q.as_str())
            .ok_or_else(|| HarnessError::Tool("skill_load: нет аргумента name".into()))?;
        let plugins = discover_dirs(&self.dirs);
        let Some(meta) = skill_by_name(&plugins, name) else {
            return Ok(crate::tool::ToolOutput::err(format!(
                "скилл '{name}' не найден; сначала skill_search"
            )));
        };
        let text = load_skill(meta)?;
        Ok(crate::tool::ToolOutput::ok(text).truncated(16_000))
    }
}

/// Инструмент `plugin_list`: список плагинов с составом.
struct PluginListTool {
    dirs: Vec<PathBuf>,
}

#[async_trait::async_trait]
impl Tool for PluginListTool {
    fn spec(&self) -> crate::llm::ToolSpec {
        crate::llm::ToolSpec {
            name: "plugin_list".into(),
            description: "Список плагинов библиотеки (скиллы + MCP + субагенты + хуки)".into(),
            parameters: serde_json::json!({"type": "object", "properties": {}}),
        }
    }

    async fn call(
        &self,
        _args: serde_json::Value,
        _ctx: &crate::tool::ToolContext,
    ) -> Result<crate::tool::ToolOutput> {
        let plugins = discover_dirs(&self.dirs);
        if plugins.is_empty() {
            return Ok(crate::tool::ToolOutput::ok(
                "плагинов нет — выполните `arch-ml init` или настройте [plugins] dirs в config.toml",
            ));
        }
        let mut out = String::new();
        for p in &plugins {
            let extras = p.extra_components().len();
            let mcp = mcp_servers(std::slice::from_ref(p)).len();
            let _ = write!(
                out,
                "── {} v{}: скиллов {}, mcp {}, прочих компонентов {}\n   {}\n",
                p.manifest.name,
                p.manifest.version,
                p.skills.len(),
                mcp,
                extras,
                p.manifest.description.lines().next().unwrap_or("")
            );
        }
        Ok(crate::tool::ToolOutput::ok(out).truncated(12_000))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Пишет файл в tempdir, создавая родителей.
    fn put(root: &Path, rel: &str, content: &str) {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        std::fs::write(path, content).expect("write");
    }

    const SKILL_A: &str = "---\nname: adr-authoring\ndescription: Дисциплина ADR до реализации\n---\n\n# ADR\n\nРешение фиксируется до кода. Сага и альтернативы.\n";
    const SKILL_B: &str = "---\nname: saga-transactions\ndescription: Распределённые транзакции сагой\n---\n\n# Saga\n\nЛокальные транзакции и компенсации.\n";

    fn fixture_tree(root: &Path) {
        put(
            root,
            "plug-a/plugin.json",
            r#"{"name":"plug-a","version":"1.2.0","description":"тестовый","keywords":["adr","bank"],"mcpServers":{"fetch":{"command":"uvx","args":["mcp-server-fetch"]}}}"#,
        );
        put(root, "plug-a/skills/adr-authoring/SKILL.md", SKILL_A);
        put(
            root,
            "plug-a/.mcp.json",
            r#"{"mcpServers":{"fs":{"command":"npx","args":["server-filesystem"],"env":{"ROOT":"/tmp"}}}}"#,
        );
        // Плагин без манифеста: синтез из имени каталога.
        put(root, "plug-b/skills/saga-transactions/SKILL.md", SKILL_B);
        // Битый манифест — пропуск плагина.
        put(root, "plug-broken/plugin.json", "{не json");
        // Скилл без frontmatter — пропуск скилла.
        put(root, "plug-a/skills/no-fm/SKILL.md", "# без фронтматтера\n");
    }

    #[test]
    fn discover_finds_plugins_and_skips_broken() {
        let tmp = tempfile::tempdir().expect("tmp");
        fixture_tree(tmp.path());
        let plugins = discover(&[tmp.path().to_path_buf()]);
        let names: Vec<&str> = plugins.iter().map(|p| p.manifest.name.as_str()).collect();
        assert_eq!(names, ["plug-a", "plug-b"], "names: {names:?}");
        let a = &plugins[0];
        assert_eq!(a.manifest.version, "1.2.0");
        assert_eq!(a.skills.len(), 1, "no-fm должен быть пропущен");
        assert_eq!(a.skills[0].name, "adr-authoring");
        assert_eq!(a.skills[0].plugin, "plug-a");
        assert_eq!(plugins[1].manifest.version, "0.0.0", "синтез манифеста");
    }

    #[test]
    fn frontmatter_tolerates_colons_and_folding() {
        let tmp = tempfile::tempdir().expect("tmp");
        // Двоеточие внутри description — serde_yaml_ng упал бы, построчный парсер — нет.
        put(
            tmp.path(),
            "p/skills/with-colon/SKILL.md",
            "---\nname: with-colon\ndescription: Важно: двоеточие внутри значения; и ещё: одно\n---\n\n# Тело\n",
        );
        // Folded description (многострочная форма `>-` с продолжением).
        put(
            tmp.path(),
            "p/skills/folded/SKILL.md",
            "---\nname: folded\ndescription: >-\n  первая часть\n  вторая часть\n---\n\n# Тело\n",
        );
        let plugins = discover(&[tmp.path().to_path_buf()]);
        let colon = skill_by_name(&plugins, "with-colon").expect("colon skill");
        assert!(
            colon.description.contains("двоеточие внутри значения"),
            "{:?}",
            colon.description
        );
        let folded = skill_by_name(&plugins, "folded").expect("folded skill");
        assert_eq!(folded.description, "первая часть вторая часть");
    }

    #[test]
    fn search_ranks_name_above_body_and_snippets() {
        let tmp = tempfile::tempdir().expect("tmp");
        fixture_tree(tmp.path());
        let plugins = discover(&[tmp.path().to_path_buf()]);
        // 'сага'/'транзакции': у saga-transactions матч в description (+6 за терм),
        // у adr-authoring — только в теле (+1).
        let hits = search(&plugins, "сага транзакции", 10);
        assert!(!hits.is_empty());
        assert_eq!(hits[0].meta.name, "saga-transactions");
        // 'adr': у adr-authoring матч в имени (+12), описании и keywords.
        let hits = search(&plugins, "adr", 10);
        assert_eq!(hits[0].meta.name, "adr-authoring");
        let adr_hit = hits
            .iter()
            .find(|h| h.meta.name == "adr-authoring")
            .expect("adr hit");
        assert!(
            adr_hit.snippet.contains(">>> "),
            "snippet: {}",
            adr_hit.snippet
        );
        let limited = search(&plugins, "сага", 1);
        assert_eq!(limited.len(), 1);
    }

    #[test]
    fn mcp_servers_merge_plugin_json_and_mcp_json_with_dotted_names() {
        let tmp = tempfile::tempdir().expect("tmp");
        fixture_tree(tmp.path());
        let plugins = discover(&[tmp.path().to_path_buf()]);
        let servers = mcp_servers(&plugins);
        let names: Vec<&str> = servers.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"plug-a.fetch"), "names: {names:?}");
        assert!(names.contains(&"plug-a.fs"), "names: {names:?}");
        let fs = servers.iter().find(|s| s.name == "plug-a.fs").expect("fs");
        assert_eq!(fs.command, "npx");
        assert_eq!(fs.env.get("ROOT").map(String::as_str), Some("/tmp"));
    }

    #[test]
    fn load_skill_appends_references_listing() {
        let tmp = tempfile::tempdir().expect("tmp");
        fixture_tree(tmp.path());
        put(
            tmp.path(),
            "plug-a/skills/adr-authoring/references/adr-template.md",
            "# Шаблон ADR\n",
        );
        let plugins = discover(&[tmp.path().to_path_buf()]);
        let meta = skill_by_name(&plugins, "adr-authoring").expect("skill");
        let text = load_skill(meta).expect("load");
        assert!(text.contains("# ADR"));
        assert!(text.contains("adr-template.md"), "text: {text}");
    }

    #[test]
    fn tools_expose_three_domain_tools() {
        let cfg = Config::default();
        let list = tools(&cfg);
        let mut names: Vec<String> = list.iter().map(|t| t.spec().name).collect();
        names.sort();
        assert_eq!(names, ["plugin_list", "skill_load", "skill_search"]);
    }
}
