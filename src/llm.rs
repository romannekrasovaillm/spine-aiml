//! Провайдеры LLM: единый трейт [`LlmProvider`], реестр [`LlmRegistry`].
//!
//! Все провайдеры (`DeepSeek`, Kimi, GLM, `GigaChat`) — OpenAI-совместимые
//! endpoint'ы; общая реализация живёт в [`openai_compat`], файлы провайдеров —
//! тонкие фабрики с пресетами `base_url`.

use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::mpsc;

use crate::config::{Config, ModelConfig};
use crate::error::{HarnessError, Result};

pub mod deepseek;
pub mod gigachat;
pub mod glm;
pub mod kimi;
pub mod openai_compat;

/// Роль сообщения в чате.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    /// Системный промпт.
    System,
    /// Сообщение пользователя.
    User,
    /// Ответ ассистента.
    Assistant,
    /// Результат инструмента.
    Tool,
}

impl Role {
    /// Строковое представление для API.
    #[must_use]
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::System => "system",
            Self::User => "user",
            Self::Assistant => "assistant",
            Self::Tool => "tool",
        }
    }
}

impl fmt::Display for Role {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Вызов инструмента, запрошенный моделью.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCall {
    /// Идентификатор вызова (связывает ответ инструмента).
    pub id: String,
    /// Имя инструмента.
    pub name: String,
    /// Аргументы (JSON по схеме из [`ToolSpec`]).
    pub arguments: Value,
}

/// Сообщение чата.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    /// Роль автора.
    pub role: Role,
    /// Текстовое содержимое (может быть пустым при `tool_calls`).
    pub content: String,
    /// Изображения (base64 data-URL), прикреплённые к сообщению. Только для
    /// роли `User` — `DeepSeek` принимает изображения лишь в user-сообщениях
    /// (иначе HTTP 400). Нативная мультимодальность `deepseek-flash`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub images: Vec<String>,
    /// Запрошенные вызовы инструментов (для роли Assistant).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_calls: Vec<ToolCall>,
    /// Идентификатор вызова, на который отвечает это сообщение (роль Tool).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    /// Цепочка рассуждений (роль Assistant, ризонинг-модели: `DeepSeek` V4
    /// thinking, Kimi k2.6/K3, GLM-4.x). `DeepSeek` ТРЕБУЕТ возвращать её в
    /// последующих запросах, если были `tool_calls` (иначе HTTP 400) — поэтому
    /// поле хранится и эхом уходит в API, но в чате не отображается.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning_content: Option<String>,
    /// Причина завершения генерации (`stop` | `length` | `tool_calls`…).
    /// `length` означает усечение потолком `max_tokens`: гигантские tool-вызовы
    /// обрываются на середине аргументов — агентный цикл такие отклоняет.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
}

impl ChatMessage {
    /// Системное сообщение.
    pub fn system(content: impl Into<String>) -> Self {
        Self {
            role: Role::System,
            content: content.into(),
            images: Vec::new(),
            tool_calls: Vec::new(),
            tool_call_id: None,
            reasoning_content: None,
            finish_reason: None,
        }
    }

    /// Сообщение пользователя.
    pub fn user(content: impl Into<String>) -> Self {
        Self {
            role: Role::User,
            content: content.into(),
            images: Vec::new(),
            tool_calls: Vec::new(),
            tool_call_id: None,
            reasoning_content: None,
            finish_reason: None,
        }
    }

    /// Ответ ассистента (возможно, с вызовами инструментов).
    pub fn assistant(content: impl Into<String>, tool_calls: Vec<ToolCall>) -> Self {
        Self {
            role: Role::Assistant,
            content: content.into(),
            images: Vec::new(),
            tool_calls,
            tool_call_id: None,
            reasoning_content: None,
            finish_reason: None,
        }
    }

    /// Результат выполнения инструмента.
    pub fn tool_result(call_id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: Role::Tool,
            content: content.into(),
            images: Vec::new(),
            tool_calls: Vec::new(),
            tool_call_id: Some(call_id.into()),
            reasoning_content: None,
            finish_reason: None,
        }
    }

    /// Прикрепить изображения (base64 data-URL) к сообщению.
    #[must_use]
    pub fn with_images(mut self, images: Vec<String>) -> Self {
        self.images = images;
        self
    }

    /// Грубая оценка размера в токенах (4 символа ≈ 1 токен; изображение ≈ 1024).
    #[must_use]
    pub fn rough_tokens(&self) -> usize {
        (self.content.len()
            + self.reasoning_content.as_deref().unwrap_or("").len()
            + self
                .tool_calls
                .iter()
                .map(|c| c.arguments.to_string().len())
                .sum::<usize>())
            / 4
            + self.images.len() * 1024
    }
}

/// Потолок суммарного размера изображений в одном запросе, байты base64.
///
/// У `DeepSeek` тело запроса ограничено 48 MiB, и в него входят ВСЕ
/// изображения истории. 32 MiB — консервативный запас на текст и запас
/// прочих провайдеров; срабатывает только как страховка поверх
/// [`ChatMessage`]-ретеншена (`[agent].images_keep_last`).
pub const MAX_REQUEST_IMAGE_BYTES: usize = 32 * 1024 * 1024;

/// Пометка в тексте сообщения, у которого изображение снято ради контекста.
///
/// Нужна, чтобы модель не получила «проанализируй 1 изображение» без самого
/// изображения и не начала выдумывать его содержимое.
pub const IMAGE_STRIPPED_MARKER: &str =
    "[изображение не передано повторно: экономия контекста — сними кадр заново, если нужен]";

/// Снять изображения со старых сообщений, оставив их у последних `keep_last`.
///
/// Изображения — самые тяжёлые элементы запроса, а vision-цикл добавляет по
/// кадру на каждый ход. Без ретеншена тело запроса растёт линейно и упирается
/// в лимит провайдера (у `DeepSeek` — 48 MiB), а платится за это каждый ход.
///
/// Дополнительно работает байтовый бюджет `max_bytes`: если даже `keep_last`
/// свежих кадров его превышают, снимаются и они — от старых к новым.
/// Критерий именно байтовый: [`ChatMessage::rough_tokens`] считает изображение
/// за 1024 токена и для размера тела запроса бесполезен.
///
/// Возвращает число сообщений, у которых изображения сняты.
pub fn strip_stale_images(
    messages: &mut [ChatMessage],
    keep_last: usize,
    max_bytes: usize,
) -> usize {
    let with_images: Vec<usize> = messages
        .iter()
        .enumerate()
        .filter(|(_, m)| !m.images.is_empty())
        .map(|(i, _)| i)
        .collect();
    if with_images.is_empty() {
        return 0;
    }
    // Новейшие — первыми: и ретеншен, и байтовый бюджет отдают приоритет
    // свежему кадру (по нему модель и действует).
    let mut keep: Vec<usize> = with_images.iter().rev().take(keep_last).copied().collect();
    let mut bytes = 0usize;
    keep.retain(|&idx| {
        let size: usize = messages[idx].images.iter().map(String::len).sum();
        if bytes + size > max_bytes {
            return false;
        }
        bytes += size;
        true
    });

    let mut dropped = 0usize;
    for &idx in &with_images {
        if keep.contains(&idx) {
            continue;
        }
        messages[idx].images.clear();
        if !messages[idx].content.contains(IMAGE_STRIPPED_MARKER) {
            if !messages[idx].content.is_empty() {
                messages[idx].content.push(' ');
            }
            messages[idx].content.push_str(IMAGE_STRIPPED_MARKER);
        }
        dropped += 1;
    }
    dropped
}

/// Спецификация инструмента для function calling (JSON Schema).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolSpec {
    /// Имя инструмента (`snake_case`).
    pub name: String,
    /// Описание для модели: что делает и когда вызывать.
    pub description: String,
    /// JSON Schema объекта параметров.
    pub parameters: Value,
}

/// Запрос к модели.
#[derive(Debug, Clone, Default)]
pub struct ChatRequest {
    /// История сообщений.
    pub messages: Vec<ChatMessage>,
    /// Доступные инструменты.
    pub tools: Vec<ToolSpec>,
    /// Температура (None — дефолт провайдера/конфига).
    pub temperature: Option<f32>,
    /// Максимум токенов ответа.
    pub max_tokens: Option<u32>,
    /// Переключатель ризонинга для этого запроса: `Some(true/false)` —
    /// слить в тело `thinking_on`/`thinking_off` из конфига модели;
    /// `None` — ничего не слать (дефолт провайдера).
    pub thinking: Option<bool>,
}

impl ChatRequest {
    /// Запрос без инструментов из списка сообщений.
    #[must_use]
    pub fn chat(messages: Vec<ChatMessage>) -> Self {
        Self {
            messages,
            tools: Vec::new(),
            temperature: None,
            max_tokens: None,
            thinking: None,
        }
    }

    /// Установить инструменты.
    #[must_use]
    pub fn with_tools(mut self, tools: Vec<ToolSpec>) -> Self {
        self.tools = tools;
        self
    }
}

/// Статистика токенов.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Usage {
    /// Токены промпта.
    pub prompt_tokens: u64,
    /// Токены ответа.
    pub completion_tokens: u64,
}

impl Usage {
    /// Суммарные токены.
    #[must_use]
    pub fn total(&self) -> u64 {
        self.prompt_tokens + self.completion_tokens
    }
}

/// Событие стриминга ответа модели.
#[derive(Debug, Clone)]
pub enum LlmEvent {
    /// Порция текста.
    Delta(String),
    /// Порция `reasoning_content` («мысли» модели): для UI, в собираемый
    /// текст ответа не входит (в итоговое сообщение попадает отдельно).
    ReasoningDelta(String),
    /// Операционная заметка провайдера (например, «поток оборвался, повторяю
    /// запрос»): для UI, НЕ часть собираемого ответа и журнала сессии.
    Note(String),
    /// Финал: статистика токенов.
    Done(Usage),
}

/// Провайдер LLM (OpenAI-совместимый чат-комплишн).
#[async_trait]
pub trait LlmProvider: Send + Sync + fmt::Debug {
    /// Короткое имя провайдера (`deepseek`, `kimi`, `glm`, …).
    fn name(&self) -> &str;
    /// Идентификатор модели.
    fn model(&self) -> &str;
    /// Нестриминговый запрос: полный ответ разом.
    async fn complete(&self, req: ChatRequest) -> Result<ChatMessage>;
    /// Стриминговый запрос: дельты в `tx`, возвращает собранный ответ.
    ///
    /// Реализация по умолчанию — обёртка над [`LlmProvider::complete`]:
    /// отправляет весь текст одной дельтой.
    async fn stream(&self, req: ChatRequest, tx: mpsc::Sender<LlmEvent>) -> Result<ChatMessage> {
        let msg = self.complete(req).await?;
        // Дефолтная обёртка: «мысли» и текст — двумя дельтами (мысли первыми).
        if let Some(reasoning) = msg.reasoning_content.as_deref() {
            if !reasoning.is_empty() {
                let _ = tx
                    .send(LlmEvent::ReasoningDelta(reasoning.to_string()))
                    .await;
            }
        }
        if !msg.content.is_empty() {
            let _ = tx.send(LlmEvent::Delta(msg.content.clone())).await;
        }
        let _ = tx.send(LlmEvent::Done(Usage::default())).await;
        Ok(msg)
    }
}

/// Реестр провайдеров из конфигурации.
#[derive(Clone)]
pub struct LlmRegistry {
    providers: HashMap<String, Arc<dyn LlmProvider>>,
    default_name: String,
}

impl fmt::Debug for LlmRegistry {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("LlmRegistry")
            .field("providers", &self.providers.keys().collect::<Vec<_>>())
            .field("default_name", &self.default_name)
            .finish()
    }
}

impl LlmRegistry {
    /// Строит реестр из конфигурации. Известным именам (`deepseek`, `kimi`,
    /// `glm`, `gigachat`) соответствуют фабрики модулей; остальные — generic
    /// OpenAI-compat.
    ///
    /// # Errors
    /// `default_model` отсутствует в `models`.
    pub fn from_config(cfg: &Config) -> Result<Self> {
        let mut providers: HashMap<String, Arc<dyn LlmProvider>> = HashMap::new();
        for (name, mc) in &cfg.models {
            let provider = Self::build(name, mc)?;
            providers.insert(name.clone(), provider);
        }
        if !providers.contains_key(&cfg.default_model) {
            return Err(HarnessError::Config(format!(
                "default_model '{}' отсутствует в [models]",
                cfg.default_model
            )));
        }
        Ok(Self {
            providers,
            default_name: cfg.default_model.clone(),
        })
    }

    fn build(name: &str, mc: &ModelConfig) -> Result<Arc<dyn LlmProvider>> {
        match name {
            n if n.starts_with("deepseek") => deepseek::provider(name, mc),
            n if n.starts_with("kimi") => kimi::provider(name, mc),
            n if n.starts_with("glm") => glm::provider(name, mc),
            n if n.starts_with("gigachat") => gigachat::provider(name, mc),
            _ => openai_compat::generic_provider(name, mc),
        }
    }

    /// Провайдер по имени.
    ///
    /// # Errors
    /// Имя не найдено в реестре.
    pub fn get(&self, name: &str) -> Result<Arc<dyn LlmProvider>> {
        self.providers
            .get(name)
            .cloned()
            .ok_or_else(|| HarnessError::Llm(format!("модель '{name}' не настроена")))
    }

    /// Провайдер по умолчанию.
    pub fn default(&self) -> Arc<dyn LlmProvider> {
        self.providers.get(&self.default_name).map_or_else(
            || unreachable!("default_model проверен в from_config"),
            Arc::clone,
        )
    }

    /// Имя модели по умолчанию.
    #[must_use]
    pub fn default_name(&self) -> &str {
        &self.default_name
    }

    /// Имена всех настроенных провайдеров.
    #[must_use]
    pub fn names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.providers.keys().cloned().collect();
        names.sort();
        names
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constructors_set_expected_fields() {
        let sys = ChatMessage::system("s");
        assert_eq!(sys.role, Role::System);
        assert!(
            sys.tool_calls.is_empty()
                && sys.tool_call_id.is_none()
                && sys.reasoning_content.is_none()
        );
        let tool = ChatMessage::tool_result("id-1", "ok");
        assert_eq!(tool.role, Role::Tool);
        assert_eq!(tool.tool_call_id.as_deref(), Some("id-1"));
        let asst = ChatMessage::assistant("a", Vec::new());
        assert_eq!(asst.role, Role::Assistant);
        assert!(asst.reasoning_content.is_none());
    }

    #[test]
    fn rough_tokens_counts_content_reasoning_and_tool_args() {
        let mut msg = ChatMessage::assistant(
            "x".repeat(400),
            vec![ToolCall {
                id: "c".into(),
                name: "bash".into(),
                arguments: serde_json::json!({"command": "y".repeat(388)}),
            }],
        );
        // arguments сериализуются в JSON: 400 символов строки-значения + обёртка.
        msg.reasoning_content = Some("r".repeat(400));
        let tokens = msg.rough_tokens();
        let args_len = msg.tool_calls[0].arguments.to_string().len();
        assert_eq!(tokens, (400 + 400 + args_len) / 4, "content+reasoning+args");
        let plain = ChatMessage::user("x".repeat(400));
        assert_eq!(plain.rough_tokens(), 100);
    }

    #[test]
    fn registry_from_default_config_resolves_names_and_default() {
        let cfg = crate::config::Config::default();
        let registry = LlmRegistry::from_config(&cfg).expect("registry");
        assert_eq!(registry.default_name(), "deepseek");
        assert_eq!(registry.default().model(), "deepseek-flash");
        for name in ["deepseek", "deepseek-pro", "kimi", "glm", "glm-flash"] {
            assert!(registry.names().iter().any(|n| n == name), "нет {name}");
        }
        assert_eq!(registry.get("kimi").expect("kimi").model(), "k3");
        assert!(registry.get("unknown-model").is_err());
    }

    #[test]
    fn registry_rejects_unknown_default_model() {
        let cfg = crate::config::Config {
            default_model: "ghost".into(),
            ..Default::default()
        };
        assert!(LlmRegistry::from_config(&cfg).is_err());
    }

    #[test]
    fn registry_routes_gigachat_prefix_to_oauth_adapter() {
        // Имя с префиксом gigachat уходит в тонкую фабрику (ADR-021), а не
        // в generic_provider: пресет base_url виден в Debug провайдера.
        let cfg = crate::config::Config {
            models: [(
                "gigachat".into(),
                crate::config::ModelConfig {
                    base_url: String::new(),
                    model: "GigaChat-2-Pro".into(),
                    api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
                    oauth: Some(crate::config::OAuthConfig {
                        token_url: String::new(),
                        scope: "GIGACHAT_API_PERS".into(),
                    }),
                    ..crate::config::ModelConfig::default()
                },
            )]
            .into(),
            default_model: "gigachat".into(),
            ..Default::default()
        };
        let registry = LlmRegistry::from_config(&cfg).expect("registry");
        let provider = registry.get("gigachat").expect("провайдер");
        assert_eq!(provider.model(), "GigaChat-2-Pro");
        let dbg = format!("{provider:?}");
        assert!(
            dbg.contains("https://api.giga.chat/v1"),
            "пресет base_url GigaChat: {dbg}"
        );
    }

    /// Сообщение с изображением (data-URL длиной `bytes`).
    fn image_message(text: &str, bytes: usize) -> ChatMessage {
        ChatMessage::user(text).with_images(vec!["x".repeat(bytes)])
    }

    #[test]
    fn strip_keeps_only_last_n_image_messages() {
        let mut history = vec![
            ChatMessage::user("первый"),
            image_message("кадр 1", 10),
            ChatMessage::assistant("ок", Vec::new()),
            image_message("кадр 2", 10),
            image_message("кадр 3", 10),
        ];
        let dropped = strip_stale_images(&mut history, 1, MAX_REQUEST_IMAGE_BYTES);
        assert_eq!(dropped, 2, "сняты два старых кадра");
        assert!(history[1].images.is_empty());
        assert!(history[3].images.is_empty());
        assert_eq!(history[4].images.len(), 1, "свежий кадр остался");
        // Маркер есть только у снятых — модель не должна думать, что
        // изображение приложено.
        assert!(history[1].content.contains(IMAGE_STRIPPED_MARKER));
        assert!(!history[4].content.contains(IMAGE_STRIPPED_MARKER));
        assert_eq!(history[0].content, "первый", "текст без картинок не тронут");
    }

    #[test]
    fn strip_is_noop_when_images_fit() {
        let mut history = vec![image_message("кадр 1", 10), image_message("кадр 2", 10)];
        let dropped = strip_stale_images(&mut history, 5, MAX_REQUEST_IMAGE_BYTES);
        assert_eq!(dropped, 0);
        assert!(history.iter().all(|m| m.images.len() == 1));
    }

    #[test]
    fn strip_zero_keep_removes_every_image() {
        let mut history = vec![image_message("кадр 1", 10), image_message("кадр 2", 10)];
        assert_eq!(
            strip_stale_images(&mut history, 0, MAX_REQUEST_IMAGE_BYTES),
            2
        );
        assert!(history.iter().all(|m| m.images.is_empty()));
        assert!(
            history
                .iter()
                .all(|m| m.content.contains(IMAGE_STRIPPED_MARKER))
        );
    }

    #[test]
    fn strip_honours_byte_budget_from_the_newest_end() {
        // Бюджет вмещает только один кадр — остаётся самый свежий.
        let mut history = vec![
            image_message("старый кадр", 100),
            image_message("средний кадр", 100),
            image_message("свежий кадр", 100),
        ];
        let dropped = strip_stale_images(&mut history, 3, 150);
        assert_eq!(dropped, 2);
        assert!(history[0].images.is_empty());
        assert!(history[1].images.is_empty());
        assert_eq!(history[2].images.len(), 1, "выживает самый свежий кадр");
    }

    #[test]
    fn strip_does_not_duplicate_marker_on_repeat() {
        let mut history = vec![image_message("кадр", 10), image_message("кадр 2", 10)];
        strip_stale_images(&mut history, 1, MAX_REQUEST_IMAGE_BYTES);
        strip_stale_images(&mut history, 1, MAX_REQUEST_IMAGE_BYTES);
        let markers = history[0].content.matches(IMAGE_STRIPPED_MARKER).count();
        assert_eq!(markers, 1, "повторный прогон не дублирует пометку");
    }

    #[test]
    fn strip_handles_history_without_images() {
        let mut history = vec![ChatMessage::user("просто текст")];
        assert_eq!(
            strip_stale_images(&mut history, 1, MAX_REQUEST_IMAGE_BYTES),
            0
        );
        assert_eq!(history[0].content, "просто текст");
    }
}
