//! Контракт инструментов харнесса: трейт [`Tool`], реестр [`ToolRegistry`].
//!
//! Харнесс намеренно тонкий: bash + файловые инструменты + специализированные
//! инструменты архитектора (mermaid, рубрики, контроль, знания, веб, handoff).
//! Реализации — в модуле [`crate::tools`] и в доменных модулях (каждый домен
//! экспортирует `tools() -> Vec<Arc<dyn Tool>>`).

use std::collections::HashMap;
use std::fmt::Write as _;
use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value;
use tokio::sync::{mpsc, oneshot};

use crate::config::Config;
use crate::error::Result;
use crate::llm::{LlmRegistry, ToolSpec};

/// Вариант ответа на вопрос агента (инструмент `propose_options`).
#[derive(Debug, Clone)]
pub struct AskOption {
    /// Короткое название варианта (1–5 слов).
    pub label: String,
    /// Последствия/компромиссы варианта.
    pub description: String,
}

/// Запрос интерактивного выбора от инструмента к UI (TUI).
///
/// UI отвечает выбранным `label` через oneshot-канал; пустая строка —
/// отказ от выбора (Esc). Пока запрос висит без ответа, инструмент ждёт,
/// агентный ход стоит — это сознательно: решение за человеком.
#[derive(Debug)]
pub struct AskRequest {
    /// Вопрос пользователю (что выбираем и почему это важно).
    pub question: String,
    /// Варианты (2–4).
    pub options: Vec<AskOption>,
    /// `label` рекомендуемого варианта (если агент рекомендует).
    pub recommended: Option<String>,
    /// Канал ответа: выбранный `label` или пустая строка при отказе.
    pub reply: oneshot::Sender<String>,
}

/// Контекст вызова инструмента.
#[derive(Clone)]
pub struct ToolContext {
    /// Рабочий каталог (все относительные пути инструментов — от него).
    pub cwd: PathBuf,
    /// Конфигурация харнесса.
    pub config: Arc<Config>,
    /// Реестр LLM (некоторым инструментам нужна модель: рубрики, динамические проверки).
    pub llm: Option<Arc<LlmRegistry>>,
    /// Мост интерактивных вопросов к UI (TUI). None — headless-режим:
    /// `propose_options` деградирует до текстовой инструкции модели.
    pub ask: Option<mpsc::Sender<AskRequest>>,
    /// Активная модель чата (для субагентов и дистилляции). Поддерживается
    /// агентной сессией: `/model` обновляет это поле вместе с провайдером.
    pub provider: Option<Arc<dyn crate::llm::LlmProvider>>,
    /// Реестр фоновых субагентов (общий между сессией и слэш-командами).
    pub subagents: Option<crate::subagent::SubagentRegistry>,
}

impl ToolContext {
    /// Контекст без LLM (для CLI-подкоманд, не требующих модели).
    #[must_use]
    pub fn new(cwd: PathBuf, config: Arc<Config>) -> Self {
        Self {
            cwd,
            config,
            llm: None,
            ask: None,
            provider: None,
            subagents: None,
        }
    }

    /// Добавляет реестр LLM.
    #[must_use]
    pub fn with_llm(mut self, llm: Arc<LlmRegistry>) -> Self {
        self.llm = Some(llm);
        self
    }

    /// Подключает мост интерактивных вопросов к UI (TUI).
    #[must_use]
    pub fn with_ask(mut self, ask: mpsc::Sender<AskRequest>) -> Self {
        self.ask = Some(ask);
        self
    }

    /// Задаёт активную модель (для субагентов и дистилляции).
    #[must_use]
    pub fn with_provider(mut self, provider: Arc<dyn crate::llm::LlmProvider>) -> Self {
        self.provider = Some(provider);
        self
    }

    /// Подключает реестр фоновых субагентов.
    #[must_use]
    pub fn with_subagents(mut self, registry: crate::subagent::SubagentRegistry) -> Self {
        self.subagents = Some(registry);
        self
    }

    /// Резолвит путь относительно рабочего каталога.
    pub fn resolve(&self, path: impl AsRef<std::path::Path>) -> PathBuf {
        let p = path.as_ref();
        if p.is_absolute() {
            p.to_path_buf()
        } else {
            self.cwd.join(p)
        }
    }
}

/// Результат выполнения инструмента.
#[derive(Debug, Clone)]
pub struct ToolOutput {
    /// Текстовое содержимое для модели/пользователя.
    pub content: String,
    /// Признак ошибки (модель должна увидеть и скорректировать план).
    pub is_error: bool,
    /// Изображения, которые инструмент вернул модели (base64 data-URL).
    /// Агентный цикл прикрепляет их к user-сообщению (мультимодальность).
    pub images: Vec<String>,
}

impl ToolOutput {
    /// Успешный результат.
    pub fn ok(content: impl Into<String>) -> Self {
        Self {
            content: content.into(),
            is_error: false,
            images: Vec::new(),
        }
    }

    /// Ошибочный результат (не паника, а сигнал модели).
    pub fn err(content: impl Into<String>) -> Self {
        Self {
            content: content.into(),
            is_error: true,
            images: Vec::new(),
        }
    }

    /// Прикрепить изображения к результату (base64 data-URL).
    #[must_use]
    pub fn with_images(mut self, images: Vec<String>) -> Self {
        self.images = images;
        self
    }

    /// Обрезает содержимое до `max_chars` с пометкой об усечении.
    #[must_use]
    pub fn truncated(mut self, max_chars: usize) -> Self {
        if self.content.len() > max_chars {
            let mut cut = self.content.chars().take(max_chars).collect::<String>();
            let _ = write!(
                cut,
                "\n… [усечено: {} из {} байт]",
                max_chars,
                self.content.len()
            );
            self.content = cut;
        }
        self
    }
}

/// Дефолтный таймаут одного вызова инструмента, секунды.
pub const DEFAULT_TOOL_TIMEOUT_SECS: u64 = 300;

/// Инструмент харнесса.
#[async_trait]
pub trait Tool: Send + Sync {
    /// Спецификация для function calling.
    fn spec(&self) -> ToolSpec;
    /// Выполнить вызов.
    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput>;
    /// Таймаут одного вызова, секунды (агентный цикл применяет его к `call`).
    /// Дефолт — [`DEFAULT_TOOL_TIMEOUT_SECS`]; долгие инструменты
    /// (`harness_run` — прогон кодового харнесса до 7200 с) переопределяют.
    fn timeout_secs(&self) -> u64 {
        DEFAULT_TOOL_TIMEOUT_SECS
    }
}

/// Реестр инструментов.
#[derive(Clone, Default)]
pub struct ToolRegistry {
    tools: HashMap<String, Arc<dyn Tool>>,
    /// Политика автономии (R-уровни); по умолчанию R2.
    policy: crate::policy::Policy,
}

impl ToolRegistry {
    /// Пустой реестр.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Установить политику автономии.
    #[must_use]
    pub fn with_policy(mut self, policy: crate::policy::Policy) -> Self {
        self.policy = policy;
        self
    }

    /// Зарегистрировать инструмент (по имени из спецификации).
    pub fn register(&mut self, tool: Arc<dyn Tool>) -> &mut Self {
        self.tools.insert(tool.spec().name.clone(), tool);
        self
    }

    /// Builder-вариант регистрации.
    #[must_use]
    pub fn with(mut self, tool: Arc<dyn Tool>) -> Self {
        self.register(tool);
        self
    }

    /// Инструмент по имени.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<Arc<dyn Tool>> {
        self.tools.get(name).cloned()
    }

    /// Таймаут вызова инструмента, секунды (per-tool [`Tool::timeout_secs`];
    /// неизвестный инструмент — дефолт).
    #[must_use]
    pub fn timeout_secs(&self, name: &str) -> u64 {
        self.get(name)
            .map_or(DEFAULT_TOOL_TIMEOUT_SECS, |t| t.timeout_secs())
    }

    /// Подмножество реестра по whitelist имён (ограничение инструментов
    /// субагента). Неизвестные имена пропускаются; политика наследуется.
    #[must_use]
    pub fn subset(&self, names: &[String]) -> Self {
        let keep: std::collections::HashSet<&str> = names.iter().map(String::as_str).collect();
        let mut out = Self::new().with_policy(self.policy);
        for (name, tool) in &self.tools {
            if keep.contains(name.as_str()) {
                out.tools.insert(name.clone(), Arc::clone(tool));
            }
        }
        out
    }

    /// Реестр без перечисленных инструментов (антирекурсия: субагенту не
    /// выдаются `subagent_*`). Политика наследуется.
    #[must_use]
    pub fn excluding(&self, names: &[&str]) -> Self {
        let mut out = Self::new().with_policy(self.policy);
        for (name, tool) in &self.tools {
            if !names.contains(&name.as_str()) {
                out.tools.insert(name.clone(), Arc::clone(tool));
            }
        }
        out
    }

    /// Спецификации всех инструментов (для `ChatRequest`).
    #[must_use]
    pub fn specs(&self) -> Vec<ToolSpec> {
        let mut specs: Vec<ToolSpec> = self.tools.values().map(|t| t.spec()).collect();
        specs.sort_by(|a, b| a.name.cmp(&b.name));
        specs
    }

    /// Имена всех инструментов.
    #[must_use]
    pub fn names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.tools.keys().cloned().collect();
        names.sort();
        names
    }

    /// Выполнить вызов по имени. Ошибка превращается в [`ToolOutput::err`],
    /// чтобы агентный цикл не рвался на сбое одного инструмента.
    /// Перед вызовом — проверка политики автономии (R-уровни).
    pub async fn dispatch(&self, name: &str, args: Value, ctx: &ToolContext) -> ToolOutput {
        match self.policy.check(name, &args) {
            crate::policy::PolicyDecision::Allow => {}
            crate::policy::PolicyDecision::RequireConfirm(reason) => {
                return ToolOutput::err(format!(
                    "ТРЕБУЕТСЯ ПОДТВЕРЖДЕНИЕ ЧЕЛОВЕКА: {reason}. В неинтерактивном режиме действие отклонено — эскалируйте архитектору или повторите с повышенным уровнем автономии."
                ));
            }
            crate::policy::PolicyDecision::Deny(reason) => {
                return ToolOutput::err(format!("ЗАПРЕЩЕНО политикой автономии: {reason}"));
            }
        }
        match self.get(name) {
            Some(tool) => match tool.call(args, ctx).await {
                Ok(out) => out,
                Err(e) => ToolOutput::err(format!("{name}: {e}")),
            },
            None => ToolOutput::err(format!("неизвестный инструмент: {name}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use serde_json::json;
    use tempfile::TempDir;

    use super::*;
    use crate::config::Config;

    fn ctx(dir: &TempDir) -> ToolContext {
        ToolContext::new(dir.path().to_path_buf(), Arc::new(Config::default()))
    }

    /// Пробный инструмент с настраиваемым именем (для dispatch/политики).
    struct Probe(&'static str);

    #[async_trait]
    impl Tool for Probe {
        fn spec(&self) -> ToolSpec {
            ToolSpec {
                name: self.0.into(),
                description: "проба".into(),
                parameters: json!({"type": "object"}),
            }
        }
        async fn call(&self, _args: Value, _ctx: &ToolContext) -> Result<ToolOutput> {
            Ok(ToolOutput::ok("probe-ran"))
        }
    }

    /// Пробный инструмент с переопределённым таймаутом.
    struct SlowProbe;

    #[async_trait]
    impl Tool for SlowProbe {
        fn spec(&self) -> ToolSpec {
            ToolSpec {
                name: "slow".into(),
                description: "долгая проба".into(),
                parameters: json!({"type": "object"}),
            }
        }
        async fn call(&self, _args: Value, _ctx: &ToolContext) -> Result<ToolOutput> {
            Ok(ToolOutput::ok("slow-ran"))
        }
        fn timeout_secs(&self) -> u64 {
            7200
        }
    }

    #[test]
    fn registry_timeout_secs_per_tool_with_default() {
        let mut reg = ToolRegistry::new();
        // Пустой реестр / неизвестный инструмент — дефолт.
        assert_eq!(reg.timeout_secs("ghost"), DEFAULT_TOOL_TIMEOUT_SECS);
        reg.register(Arc::new(Probe("probe")));
        reg.register(Arc::new(SlowProbe));
        // Зарегистрированный без переопределения — дефолт, с переопределением — своё.
        assert_eq!(reg.timeout_secs("probe"), DEFAULT_TOOL_TIMEOUT_SECS);
        assert_eq!(reg.timeout_secs("slow"), 7200);
    }

    #[test]
    fn output_ok_err_and_truncation_marker() {
        assert!(!ToolOutput::ok("x").is_error);
        assert!(ToolOutput::err("x").is_error);
        let short = ToolOutput::ok("коротко").truncated(100);
        assert_eq!(short.content, "коротко", "короткий текст не трогаем");
        let long = ToolOutput::ok("y".repeat(500)).truncated(100);
        assert!(
            long.content.contains("[усечено: 100 из 500 байт]"),
            "{}",
            long.content
        );
        assert!(long.content.chars().count() < 140, "усечение работает");
        // Кириллица: len() в байтах — маркер честно показывает байты.
        let cyr = ToolOutput::ok("я".repeat(500)).truncated(100);
        assert!(cyr.content.contains("из 1000 байт"), "{}", cyr.content);
    }

    #[test]
    fn registry_register_get_names_specs_sorted() {
        let reg = ToolRegistry::new()
            .with(Arc::new(Probe("zeta")))
            .with(Arc::new(Probe("alpha")));
        assert!(reg.get("zeta").is_some());
        assert!(reg.get("nope").is_none());
        assert_eq!(reg.names(), ["alpha", "zeta"], "имена отсортированы");
        let specs = reg.specs();
        assert_eq!(specs[0].name, "alpha");
        assert_eq!(specs[1].name, "zeta");
    }

    #[test]
    fn subset_and_excluding_keep_policy() {
        let reg = ToolRegistry::new()
            .with(Arc::new(Probe("a")))
            .with(Arc::new(Probe("b")))
            .with(Arc::new(Probe("c")));
        let sub = reg.subset(&["a".to_string(), "unknown".to_string()]);
        assert_eq!(sub.names(), ["a"], "неизвестные имена отброшены");
        let exc = reg.excluding(&["b"]);
        assert_eq!(exc.names(), ["a", "c"]);
    }

    #[tokio::test]
    async fn dispatch_unknown_tool_is_soft_error() {
        let dir = tempfile::tempdir().expect("tmp");
        let reg = ToolRegistry::new();
        let out = reg.dispatch("ghost", json!({}), &ctx(&dir)).await;
        assert!(out.is_error);
        assert!(
            out.content.contains("неизвестный инструмент"),
            "{}",
            out.content
        );
    }

    #[tokio::test]
    async fn dispatch_policy_denies_before_call() {
        let dir = tempfile::tempdir().expect("tmp");
        // Инструмент называется bash: деструктивная команда при R2 — DENY,
        // тело инструмента не должно исполняться (ответ не «probe-ran»).
        let reg = ToolRegistry::new().with(Arc::new(Probe("bash")));
        let out = reg
            .dispatch("bash", json!({"command": "rm -rf /tmp/x"}), &ctx(&dir))
            .await;
        assert!(out.is_error);
        assert!(out.content.contains("ЗАПРЕЩЕНО"), "{}", out.content);
        assert!(!out.content.contains("probe-ran"), "вызов не дошёл");
        // Разрешённая команда доходит до инструмента.
        let out = reg
            .dispatch("bash", json!({"command": "ls"}), &ctx(&dir))
            .await;
        assert_eq!(out.content, "probe-ran");
    }

    #[test]
    fn resolve_joins_relative_and_keeps_absolute() {
        let dir = tempfile::tempdir().expect("tmp");
        let c = ctx(&dir);
        assert_eq!(c.resolve("sub/f.md"), dir.path().join("sub/f.md"));
        let abs = c.resolve("/etc/hostname");
        assert_eq!(abs, PathBuf::from("/etc/hostname"));
    }
}
