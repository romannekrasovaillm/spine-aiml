//! Субагенты: фоновые мини-агенты со свежим контекстом.
//!
//! Методика (разборы SDD-харнессов `_24_августа`: Superpowers — «реализация
//! в свежих субагентах», GSD/Claude Code — параллельные линзы-ревьюеры
//! на ревью-гейте, включая состязательную):
//! - **свежий контекст**: субагент получает только поручение (+ явный блок
//!   контекста), история родителя не наследуется — защита от загрязнения
//!   и экономия токенов;
//! - **фон**: запуск не блокирует ход — родитель продолжает и забирает
//!   отчёт позже (`subagent_result`), статусы — `subagent_list`;
//! - **least privilege**: whitelist инструментов из frontmatter спеки
//!   (`plugins/*/agents/*.md`), пустой — все инструменты харнесса;
//! - **антирекурсия**: субагенту не выдаются `subagent_*`-инструменты;
//! - **лимит конкурентности**: слоты реестра (дефолт [`MAX_RUNNING`] = 400;
//!   реальный регулятор — rate-limit провайдера, покрытый ретраями);
//!
//! КОНТРАКТ (владелец: агент `subagent`):
//! - спека субагента — `agents/<name>.md` в плагине: frontmatter
//!   `name`/`description`/`tools` (через запятую) + тело = системный промпт;
//! - [`SubagentRegistry`] — общий реестр задач (сессия агента и слэш
//!   `/agents` видят одно состояние); отчёт дублируется файлом в
//!   `reports/subagents/<id>.md`;
//! - доставка результата: pull (`subagent_result`) + push — подписчик
//!   [`SubagentRegistry::set_notifier`] (TUI) получает [`BackgroundNotice`]
//!   о каждом завершении и запускает ход с отчётом, не дожидаясь пинка
//!   пользователя;
//! - инструменты: `subagent_run` (запуск), `subagent_list` (статусы),
//!   `subagent_result` (отчёт по id).

use std::fmt::Write as _;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use serde_json::{Value, json};

use crate::config::Config;
use crate::error::{HarnessError, Result};
use crate::llm::{LlmProvider, ToolSpec};
use crate::tool::{Tool, ToolContext, ToolOutput};

/// Максимум одновременно работающих фоновых задач (субагенты и ralph-циклы
/// делят одни слоты). 400 — фактически «без потолка»: реальный регулятор —
/// rate-limit провайдера (429 покрываются терпеливыми ретраями), а не слоты.
pub(crate) const MAX_RUNNING: usize = 400;
/// Лимит длины отчёта субагента в реестре (символов).
pub(crate) const REPORT_MAX_CHARS: usize = 6000;
/// Имена инструментов, не выдаваемых фоновым исполнителям (субагентам и
/// ralph-раундам).
///
/// Две группы:
/// - **оркестрация** — антирекурсия: дети не запускают собственных детей и циклов;
/// - **управление рабочим столом** — фоновая задача работает без человека в
///   цикле, а её контекст может быть загрязнён недоверенным текстом
///   (ADR-041): ввод без присмотра — ровно тот случай, когда Destructive-класс
///   некому подтвердить. Наблюдение (`screenshot`, `browser_page`,
///   `browser_screenshot`) остаётся: смотреть не значит действовать.
pub(crate) const SUBAGENT_TOOLS: [&str; 16] = [
    "subagent_run",
    "subagent_list",
    "subagent_result",
    "ralph_run",
    "computer_move",
    "computer_click",
    "computer_scroll",
    "computer_drag",
    "computer_key",
    "computer_type",
    "window_focus",
    "browser_open",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_eval",
];

/// Правила отчёта, дописываемые к системному промпту любого субагента.
const REPORT_RULES: &str = "\n\n---\nПравила отчёта родителю: плотный структурированный отчёт \
до 3000 символов — что сделано, ключевые находки со свидетельствами (файл:строка или URL), \
рекомендации. Без воды: родительский агент увидит только этот отчёт, а не твой ход рассуждений.";

/// Системный промпт универсального субагента (без спеки в плагинах).
const GENERAL_PROMPT: &str = "Ты — фоновый субагент-исследователь solution-архитектора AI/ML. \
Выполни поручение самостоятельно: читай артефакты инструментами, проверяй факты, \
не выдумывай содержимое репозитория.";

/// Спецификация субагента (из `agents/*.md` плагина).
#[derive(Debug, Clone)]
pub struct SubagentSpec {
    /// Имя (frontmatter `name`).
    pub name: String,
    /// Описание — когда звать (frontmatter `description`).
    pub description: String,
    /// Whitelist инструментов (frontmatter `tools`); пусто — все инструменты.
    pub tools: Vec<String>,
    /// Тело файла — системный промпт субагента.
    pub body: String,
    /// Плагин-владелец.
    pub plugin: String,
}

/// Встроенная спека «general» (без файла в плагине).
pub(crate) fn general_spec() -> SubagentSpec {
    SubagentSpec {
        name: "general".into(),
        description: "Универсальный фоновый исполнитель без специализации".into(),
        tools: Vec::new(),
        body: GENERAL_PROMPT.into(),
        plugin: String::new(),
    }
}

/// Статус фоновой задачи.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskStatus {
    /// Работает.
    Running,
    /// Завершена успешно (отчёт в `report`).
    Done,
    /// Завершена ошибкой (текст ошибки в `report`).
    Failed,
}

impl TaskStatus {
    /// Строка для таблиц статусов.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Done => "done",
            Self::Failed => "failed",
        }
    }
}

/// Уведомление о завершении фоновой задачи (push-канал для TUI).
#[derive(Debug, Clone)]
pub struct BackgroundNotice {
    /// Идентификатор задачи (`sa-…`, `hr-…`, `ralph-…`).
    pub id: String,
    /// Финальный статус.
    pub status: TaskStatus,
}

/// Фоновая задача субагента.
#[derive(Debug, Clone)]
pub struct SubagentTask {
    /// Идентификатор (`sa-<ts>-<seq>`).
    pub id: String,
    /// Имя спеки субагента.
    pub agent: String,
    /// Поручение (первые 2000 символов).
    pub task: String,
    /// Статус.
    pub status: TaskStatus,
    /// Отчёт (или текст ошибки) по завершении.
    pub report: String,
    /// Метка старта (ISO).
    pub started_at: String,
    /// Метка завершения (ISO; None — ещё работает).
    pub finished_at: Option<String>,
}

/// Общий реестр фоновых субагентов (клонируется дёшево — Arc внутри).
#[derive(Clone)]
pub struct SubagentRegistry {
    tasks: Arc<Mutex<Vec<SubagentTask>>>,
    /// Подписчик уведомлений о завершении задач (TUI). None — доставка
    /// остаётся pull (`subagent_list`/`subagent_result`), как в headless.
    notify: Arc<Mutex<Option<tokio::sync::mpsc::UnboundedSender<BackgroundNotice>>>>,
    /// Ёмкость слотов (по умолчанию [`MAX_RUNNING`]; меньше — в тестах).
    capacity: usize,
}

impl Default for SubagentRegistry {
    fn default() -> Self {
        Self {
            tasks: Arc::new(Mutex::new(Vec::new())),
            notify: Arc::new(Mutex::new(None)),
            capacity: MAX_RUNNING,
        }
    }
}

impl SubagentRegistry {
    /// Пустой реестр с дефолтной ёмкостью.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Реестр с усечённой ёмкостью (тесты лимита без сотен задач).
    #[cfg(test)]
    pub(crate) fn with_capacity(capacity: usize) -> Self {
        Self {
            tasks: Arc::new(Mutex::new(Vec::new())),
            notify: Arc::new(Mutex::new(None)),
            capacity,
        }
    }

    /// Подключает push-уведомления о завершении фоновых задач (TUI
    /// переводит их в сообщения event loop). Без подключения доставка
    /// результата — pull через `subagent_list`/`subagent_result`.
    pub fn set_notifier(&self, tx: tokio::sync::mpsc::UnboundedSender<BackgroundNotice>) {
        if let Ok(mut slot) = self.notify.lock() {
            *slot = Some(tx);
        }
    }

    /// Шлёт уведомление подписчику (best effort: приёмник мог уйти
    /// вместе с TUI — уведомление теряется, реестр остаётся источником
    /// истины для pull-чтения, поэтому ошибка send игнорируется).
    fn emit_notice(&self, id: &str, status: TaskStatus) {
        if let Ok(slot) = self.notify.lock() {
            if let Some(tx) = slot.as_ref() {
                let _ = tx.send(BackgroundNotice {
                    id: id.to_string(),
                    status,
                });
            }
        }
    }

    /// Слот ёмкости реестра.
    pub(crate) fn capacity(&self) -> usize {
        self.capacity
    }

    /// Число работающих задач.
    #[must_use]
    pub fn running(&self) -> usize {
        self.tasks.lock().map_or(0, |t| {
            t.iter().filter(|x| x.status == TaskStatus::Running).count()
        })
    }

    /// Снимок задач (новые первыми).
    #[must_use]
    pub fn list(&self) -> Vec<SubagentTask> {
        self.tasks
            .lock()
            .map(|t| t.iter().rev().cloned().collect())
            .unwrap_or_default()
    }

    /// Задача по id.
    #[must_use]
    pub fn get(&self, id: &str) -> Option<SubagentTask> {
        self.tasks
            .lock()
            .ok()
            .and_then(|t| t.iter().find(|x| x.id == id).cloned())
    }

    /// Генерирует id задачи с префиксом (`sa-…` субагенты, `ralph-…` циклы).
    pub(crate) fn next_id(&self, prefix: &str) -> String {
        let seq = self.tasks.lock().map_or(0, |t| t.len());
        format!(
            "{prefix}-{}-{:02}",
            chrono::Local::now().format("%Y%m%d-%H%M%S"),
            seq
        )
    }

    /// Вставляет готовую запись задачи (используется ralph-циклом).
    pub(crate) fn insert(&self, task: SubagentTask) {
        if let Ok(mut tasks) = self.tasks.lock() {
            tasks.push(task);
        }
    }

    /// Завершает задачу: статус, отчёт и метка финиша (используется
    /// ralph-циклом и `harness_run`; `launch` тоже завершает через него —
    /// так уведомление подписчика гарантировано для всех видов задач).
    pub(crate) fn finish(&self, id: &str, status: TaskStatus, report: String) {
        let mut found = false;
        if let Ok(mut tasks) = self.tasks.lock() {
            if let Some(t) = tasks.iter_mut().find(|x| x.id == id) {
                t.status = status;
                t.report = report;
                t.finished_at = Some(now_iso());
                found = true;
            }
        }
        if found {
            self.emit_notice(id, status);
        }
    }

    /// Запускает субагента в фоне; возвращает id задачи.
    ///
    /// Субагент — полноценная [`crate::agent::AgentSession`] со свежей
    /// историей и урезанным реестром инструментов; по завершении статус
    /// и отчёт обновляются в реестре, отчёт дублируется файлом.
    ///
    /// # Errors
    /// Превышен лимит параллельных задач ([`MAX_RUNNING`]).
    pub fn launch(
        &self,
        spec: &SubagentSpec,
        task: &str,
        context: Option<&str>,
        provider: Arc<dyn LlmProvider>,
        tool_ctx: ToolContext,
    ) -> Result<String> {
        if self.running() >= self.capacity {
            return Err(HarnessError::Agent(format!(
                "все слоты субагентов заняты ({}); дождитесь завершения — subagent_list",
                self.capacity
            )));
        }
        let id = self.next_id("sa");
        let record = SubagentTask {
            id: id.clone(),
            agent: spec.name.clone(),
            task: task.chars().take(2000).collect(),
            status: TaskStatus::Running,
            report: String::new(),
            started_at: now_iso(),
            finished_at: None,
        };
        if let Ok(mut tasks) = self.tasks.lock() {
            tasks.push(record);
        }

        // Инструменты: полный реестр минус subagent_* (антирекурсия);
        // whitelist спеки — поверх.
        let mut tools = crate::tools::full_registry(&tool_ctx.config).excluding(&SUBAGENT_TOOLS);
        if !spec.tools.is_empty() {
            tools = tools.subset(&spec.tools);
        }
        let system = format!("{}{REPORT_RULES}", spec.body.trim());
        let mut user = task.to_string();
        if let Some(ctx_text) = context.map(str::trim).filter(|s| !s.is_empty()) {
            let _ = write!(
                user,
                "\n\nКонтекст от родительского агента:\n{}",
                ctx_text.chars().take(8000).collect::<String>()
            );
        }

        let registry = self.clone();
        let task_id = id.clone();
        tokio::spawn(async move {
            let mut session = crate::agent::AgentSession::new(
                tool_ctx.config.clone(),
                provider,
                tools,
                tool_ctx.clone(),
                system,
            );
            let result = session.send(&user, None).await;
            let (status, report) = match result {
                Ok(text) => (TaskStatus::Done, text),
                Err(e) => (
                    TaskStatus::Failed,
                    format!("субагент завершился ошибкой: {e}"),
                ),
            };
            let report: String = report.chars().take(REPORT_MAX_CHARS).collect();
            // Завершение через finish(): единая точка — статус, метка
            // финиша и push-уведомление подписчика (TUI).
            registry.finish(&task_id, status, report.clone());
            // Дубль отчёта файлом — аудит и чтение вне сессии (best effort).
            if let Some(t) = registry.get(&task_id) {
                let dir = tool_ctx.config.paths.reports_dir.join("subagents");
                let path = dir.join(format!("{task_id}.md"));
                let body = format!(
                    "# Субагент {} ({})\n\n- задача: {}\n- старт: {}\n- финиш: {}\n- статус: {}\n\n{}\n",
                    t.id,
                    t.agent,
                    t.task,
                    t.started_at,
                    t.finished_at.as_deref().unwrap_or(""),
                    status.as_str(),
                    report
                );
                if let Err(e) =
                    std::fs::create_dir_all(&dir).and_then(|()| std::fs::write(&path, body))
                {
                    tracing::warn!("отчёт субагента не записан {}: {e}", path.display());
                }
            }
        });
        Ok(id)
    }
}

/// Спеки субагентов всех плагинов (`agents/*.md`), битые файлы пропускаются.
#[must_use]
pub fn discover_specs(plugins: &[crate::plugin::Plugin]) -> Vec<SubagentSpec> {
    let mut out = Vec::new();
    for p in plugins {
        let agents_dir = p.dir.join("agents");
        let Ok(rd) = std::fs::read_dir(&agents_dir) else {
            continue;
        };
        let mut entries: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "md"))
            .collect();
        entries.sort();
        for path in entries {
            if let Some(mut spec) = parse_agent_md(&path) {
                spec.plugin.clone_from(&p.manifest.name);
                out.push(spec);
            }
        }
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out
}

/// Спеки по каталогам плагинов (для слэш-команды и инструментов).
#[must_use]
pub fn available_specs(dirs: &[PathBuf]) -> Vec<SubagentSpec> {
    discover_specs(&crate::plugin::discover(dirs))
}

/// Парсит `agents/<name>.md`: frontmatter `name`/`description`/`tools` + тело.
/// Толерантный построчный разбор (как в plugin.rs — `serde_yaml_ng` падает на
/// двоеточиях в description).
fn parse_agent_md(path: &std::path::Path) -> Option<SubagentSpec> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut lines = text.lines();
    if lines.next()?.trim() != "---" {
        return None;
    }
    let mut name = String::new();
    let mut description = String::new();
    let mut tools_raw = String::new();
    let mut body: Vec<&str> = Vec::new();
    let mut in_frontmatter = true;
    let mut current: Option<&str> = None;
    for line in lines {
        if in_frontmatter {
            if line.trim() == "---" {
                in_frontmatter = false;
                continue;
            }
            if line.starts_with(' ') || line.starts_with('\t') {
                if let Some(which) = current {
                    let t = line.trim();
                    if !t.is_empty() {
                        let target = match which {
                            "name" => &mut name,
                            "tools" => &mut tools_raw,
                            _ => &mut description,
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
            } else if let Some(v) = line.strip_prefix("tools:") {
                tools_raw = v.trim().to_string();
                current = Some("tools");
            } else {
                current = None;
            }
        } else {
            body.push(line);
        }
    }
    if name.is_empty() {
        return None;
    }
    let tools = tools_raw
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect();
    Some(SubagentSpec {
        name,
        description,
        tools,
        body: body.join("\n").trim().to_string(),
        plugin: String::new(),
    })
}

/// Инструменты домена: `subagent_run`, `subagent_list`, `subagent_result`.
#[must_use]
pub fn tools(cfg: &Config) -> Vec<Arc<dyn Tool>> {
    vec![
        Arc::new(SubagentRunTool {
            dirs: cfg.plugins.dirs.clone(),
        }),
        Arc::new(SubagentListTool),
        Arc::new(SubagentResultTool),
    ]
}

/// Инструмент `subagent_run`: запуск фонового субагента.
struct SubagentRunTool {
    dirs: Vec<PathBuf>,
}

#[async_trait]
impl Tool for SubagentRunTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "subagent_run".into(),
            description:
                "Запустить фонового субагента со свежим контекстом: он выполнит поручение \
                своими инструментами и вернёт плотный отчёт (забирается через subagent_result). \
                Зови для параллельных линз: аудит устойчивости, разведка репозитория, ревью \
                спецификации, веб-ресёрч — пока продолжаешь основную работу. Специализации — \
                из плагинов (список: subagent_list); без agent — универсальный 'general'."
                    .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "имя субагента из плагинов (например resilience-auditor); пусто — general"
                    },
                    "task": {
                        "type": "string",
                        "description": "поручение: что исследовать/проверить, какой результат нужен"
                    },
                    "context": {
                        "type": "string",
                        "description": "явный контекст для субагента (пути, решения, ограничения) — история чата ему НЕ видна"
                    }
                },
                "required": ["task"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let task = args
            .get("task")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if task.is_empty() {
            return Ok(ToolOutput::err("subagent_run: пустой task"));
        }
        let Some(registry) = &ctx.subagents else {
            return Ok(ToolOutput::err(
                "субагенты недоступны: реестр не подключён (headless-режим без TUI/раннера)",
            ));
        };
        let agent = args
            .get("agent")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("general");
        let spec = if agent == "general" {
            general_spec()
        } else {
            let specs = available_specs(&self.dirs);
            if let Some(s) = specs.into_iter().find(|s| s.name == agent) {
                s
            } else {
                let available = available_specs(&self.dirs)
                    .iter()
                    .map(|s| format!("{} [{}]", s.name, s.plugin))
                    .collect::<Vec<_>>()
                    .join(", ");
                return Ok(ToolOutput::err(format!(
                    "субагент '{agent}' не найден. Доступные: general, {available}"
                )));
            }
        };
        let Some(provider) = ctx
            .provider
            .clone()
            .or_else(|| ctx.llm.as_ref().map(|r| r.default()))
        else {
            return Ok(ToolOutput::err(
                "нет модели для субагента: LLM не настроен в контексте",
            ));
        };
        let context = args.get("context").and_then(Value::as_str);
        match registry.launch(&spec, &task, context, provider, ctx.clone()) {
            Ok(id) => Ok(ToolOutput::ok(format!(
                "субагент '{}' запущен в фоне: {id}. Работает со свежим контекстом; \
                 статус — subagent_list, отчёт — subagent_result(id=\"{id}\"). \
                 Не дожидайся специально: продолжай основную работу и забери отчёт, когда понадобится.",
                spec.name
            ))),
            Err(e) => Ok(ToolOutput::err(format!("{e}"))),
        }
    }
}

/// Инструмент `subagent_list`: статусы фоновых задач и доступные спеки.
struct SubagentListTool;

#[async_trait]
impl Tool for SubagentListTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "subagent_list".into(),
            description: "Статусы фоновых субагентов (running/done/failed) и их отчёты-заголовки"
                .into(),
            parameters: json!({"type": "object", "properties": {}}),
        }
    }

    async fn call(&self, _args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let Some(registry) = &ctx.subagents else {
            return Ok(ToolOutput::err("субагенты недоступны: реестр не подключён"));
        };
        Ok(ToolOutput::ok(render_tasks(&registry.list())))
    }
}

/// Инструмент `subagent_result`: полный отчёт фоновой задачи по id.
struct SubagentResultTool;

#[async_trait]
impl Tool for SubagentResultTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: "subagent_result".into(),
            description: "Полный отчёт фонового субагента по id (после subagent_run/subagent_list)"
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "идентификатор задачи (sa-…)"}
                },
                "required": ["id"]
            }),
        }
    }

    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput> {
        let id = args.get("id").and_then(Value::as_str).unwrap_or("").trim();
        let Some(registry) = &ctx.subagents else {
            return Ok(ToolOutput::err("субагенты недоступны: реестр не подключён"));
        };
        let Some(task) = registry.get(id) else {
            return Ok(ToolOutput::err(format!(
                "задача '{id}' не найдена; актуальный список — subagent_list"
            )));
        };
        Ok(match task.status {
            TaskStatus::Running => ToolOutput::ok(format!(
                "{id} ещё работает (с {}): {}…",
                task.started_at,
                task.task.chars().take(80).collect::<String>()
            )),
            TaskStatus::Done => {
                ToolOutput::ok(format!("Отчёт {id} ({}):\n{}", task.agent, task.report))
            }
            TaskStatus::Failed => ToolOutput::err(format!("{id} failed: {}", task.report)),
        })
    }
}

/// Таблица задач для `subagent_list` и слэша `/agents`.
#[must_use]
pub fn render_tasks(tasks: &[SubagentTask]) -> String {
    if tasks.is_empty() {
        return "фоновых задач нет".into();
    }
    let mut out = String::new();
    for t in tasks {
        let preview: String = t.task.chars().take(60).collect();
        let _ = writeln!(
            out,
            "── {} [{}] {} · {} · {preview}{}",
            t.id,
            t.agent,
            t.status.as_str(),
            t.started_at,
            if t.task.chars().count() > 60 {
                "…"
            } else {
                ""
            }
        );
        if t.status == TaskStatus::Done && !t.report.is_empty() {
            let _ = writeln!(
                out,
                "   {}",
                t.report
                    .lines()
                    .next()
                    .unwrap_or("")
                    .chars()
                    .take(100)
                    .collect::<String>()
            );
        }
    }
    out
}

/// Текущее время в ISO 8601 (для меток задач).
pub(crate) fn now_iso() -> String {
    chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm::{ChatMessage, ChatRequest, LlmProvider};
    use std::sync::Arc;

    /// Провайдер-заглушка: мгновенный фиксированный отчёт.
    #[derive(Debug)]
    struct FakeLlm;

    #[async_trait]
    impl LlmProvider for FakeLlm {
        fn name(&self) -> &'static str {
            "fake"
        }
        fn model(&self) -> &'static str {
            "fake-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            Ok(ChatMessage::assistant(
                "ОТЧЁТ: всё проверено, замечаний нет",
                Vec::new(),
            ))
        }
    }

    /// Провайдер, который «думает» бесконечно (для теста лимита слотов).
    #[derive(Debug)]
    struct SlowLlm;

    #[async_trait]
    impl LlmProvider for SlowLlm {
        fn name(&self) -> &'static str {
            "slow"
        }
        fn model(&self) -> &'static str {
            "slow-1"
        }
        async fn complete(&self, _req: ChatRequest) -> Result<ChatMessage> {
            tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
            unreachable!("тест не дожидается slow-провайдера");
        }
    }

    fn test_ctx(dir: &std::path::Path) -> ToolContext {
        let mut cfg = Config::default();
        cfg.paths.sessions_dir = dir.join("sessions");
        cfg.paths.reports_dir = dir.join("reports");
        // Изоляция тестов от плагинных хуков реальной библиотеки.
        cfg.plugins.include_hooks = false;
        ToolContext::new(dir.to_path_buf(), Arc::new(cfg))
    }

    #[test]
    fn parses_agent_md_with_tools_whitelist() {
        let tmp = tempfile::tempdir().expect("tmp");
        let dir = tmp.path().join("plug/agents");
        std::fs::create_dir_all(&dir).expect("mkdir");
        std::fs::write(
            dir.join("auditor.md"),
            "---\nname: auditor\ndescription: Аудитор: проверяет стыки\ntools: read_file, grep\n---\n\nТы — аудитор.\nШаг 1.\n",
        )
        .expect("write");
        let spec = parse_agent_md(&dir.join("auditor.md")).expect("спека");
        assert_eq!(spec.name, "auditor");
        assert_eq!(spec.description, "Аудитор: проверяет стыки");
        assert_eq!(spec.tools, vec!["read_file", "grep"]);
        assert!(spec.body.contains("Ты — аудитор."));
    }

    #[test]
    fn finish_emits_background_notice() {
        let registry = SubagentRegistry::new();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        registry.set_notifier(tx);
        registry.insert(SubagentTask {
            id: "hr-test-01".into(),
            agent: "claude-code".into(),
            task: "аудит".into(),
            status: TaskStatus::Running,
            report: String::new(),
            started_at: "t0".into(),
            finished_at: None,
        });
        registry.finish("hr-test-01", TaskStatus::Done, "отчёт".into());
        let notice = rx.try_recv().expect("уведомление о завершении");
        assert_eq!(notice.id, "hr-test-01");
        assert_eq!(notice.status, TaskStatus::Done);
        // Неизвестный id — уведомления нет.
        registry.finish("hr-нет-такого", TaskStatus::Failed, String::new());
        assert!(rx.try_recv().is_err(), "лишнего уведомления быть не должно");
    }

    #[test]
    fn unattended_executors_cannot_drive_the_desktop() {
        // ADR-041: фоновый исполнитель не управляет столом — подтверждать
        // Destructive-действие в фоне некому, а контекст может быть загрязнён.
        let mut cfg = crate::config::Config::default();
        cfg.computer.enabled = true;
        cfg.computer.browser.enabled = true;
        let reg = crate::tools::full_registry(&cfg);
        assert!(
            reg.get("computer_click").is_some(),
            "предусловие: инструменты ввода вообще есть в полном реестре"
        );
        let sub = reg.excluding(&SUBAGENT_TOOLS);
        for banned in [
            "computer_click",
            "computer_type",
            "window_focus",
            "browser_click",
            "browser_eval",
            "subagent_run",
        ] {
            assert!(
                sub.get(banned).is_none(),
                "{banned} не должен попадать фоновому исполнителю"
            );
        }
        // Наблюдение остаётся доступным.
        for allowed in ["screenshot", "screen_size", "browser_page"] {
            assert!(
                sub.get(allowed).is_some(),
                "{allowed} — наблюдение, не запрещено"
            );
        }
    }

    #[test]
    fn subset_and_excluding_shape_subagent_tools() {
        let reg = crate::tools::core_registry();
        let sub = reg.subset(&[
            "read_file".to_string(),
            "grep".to_string(),
            "unknown".to_string(),
        ]);
        let mut names = sub.names();
        names.sort();
        assert_eq!(names, ["grep", "read_file"]);
        let no_ask = reg.excluding(&["propose_options"]);
        assert!(no_ask.get("propose_options").is_none());
        assert!(no_ask.get("bash").is_some());
    }

    #[tokio::test]
    async fn launch_runs_background_task_and_stores_report() {
        let tmp = tempfile::tempdir().expect("tmp");
        let registry = SubagentRegistry::new();
        let ctx = test_ctx(tmp.path()).with_subagents(registry.clone());
        let id = registry
            .launch(
                &general_spec(),
                "проверь стыки",
                Some("контекст: repo X"),
                Arc::new(FakeLlm),
                ctx,
            )
            .expect("launch");
        // Дожидаемся завершения фоновой задачи (FakeLlm отвечает мгновенно).
        let mut task = None;
        for _ in 0..100 {
            if let Some(t) = registry.get(&id) {
                if t.status != TaskStatus::Running {
                    task = Some(t);
                    break;
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        let task = task.expect("задача завершилась");
        assert_eq!(task.status, TaskStatus::Done);
        assert!(task.report.contains("ОТЧЁТ"), "report: {}", task.report);
        assert!(task.finished_at.is_some());
        // Отчёт продублирован файлом.
        let file = tmp.path().join(format!("reports/subagents/{id}.md"));
        assert!(file.is_file(), "нет файла отчёта: {}", file.display());
    }

    #[tokio::test]
    async fn launch_respects_concurrency_cap() {
        let tmp = tempfile::tempdir().expect("tmp");
        // Ёмкость 4 — тест лимита без сотен фоновых задач.
        let registry = SubagentRegistry::with_capacity(4);
        let ctx = test_ctx(tmp.path()).with_subagents(registry.clone());
        for _ in 0..4 {
            registry
                .launch(
                    &general_spec(),
                    "долгая работа",
                    None,
                    Arc::new(SlowLlm),
                    ctx.clone(),
                )
                .expect("слот есть");
        }
        let err = registry
            .launch(&general_spec(), "лишняя", None, Arc::new(SlowLlm), ctx)
            .expect_err("лимит исчерпан");
        assert!(err.to_string().contains("слоты"), "err: {err}");
    }
}
