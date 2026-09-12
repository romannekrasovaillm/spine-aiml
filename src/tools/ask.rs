//! Инструмент `propose_options`: агент предлагает пользователю 2–4 варианта
//! решения и дожидается интерактивного выбора (уточнение направления до того,
//! как продолжать проектирование).
//!
//! КОНТРАКТ (владелец: агент `tools`):
//! - в TUI запрос уходит по мосту [`crate::tool::AskRequest`] в event loop,
//!   пользователь выбирает вариант в модальной панели (↑/↓, Enter, 1–4, Esc);
//! - в headless-режиме (нет моста) инструмент НЕ падает, а возвращает
//!   модели инструкцию представить варианты текстом;
//! - отказ пользователя (Esc) — валидный исход: модель обязана действовать
//!   самостоятельно и зафиксировать открытый вопрос.

use async_trait::async_trait;
use serde_json::{Value, json};
use tokio::sync::oneshot;

use crate::error::Result;
use crate::llm::ToolSpec;
use crate::tool::{AskOption, AskRequest, Tool, ToolContext, ToolOutput};

/// Имя инструмента: [`crate::agent`] подбирает по нему расширенный таймаут
/// (человеку на решение нужно больше стандартных 300 секунд).
pub const PROPOSE_OPTIONS: &str = "propose_options";

/// Максимум вариантов в одном вопросе (больше — плохая декомпозиция выбора).
const MAX_OPTIONS: usize = 4;

/// Интерактивный выбор вариантов решения пользователем.
#[derive(Debug)]
pub struct ProposeOptionsTool;

#[async_trait]
impl Tool for ProposeOptionsTool {
    fn spec(&self) -> ToolSpec {
        ToolSpec {
            name: PROPOSE_OPTIONS.into(),
            description: "Предложить пользователю 2–4 варианта и дождаться выбора. Зови, когда \
                дальнейшая работа зависит от решения человека: альтернативы архитектуры, \
                компромиссы стоимость/надёжность/скорость, приоритеты, допуск риска. Не угадывай \
                за пользователя значимый выбор — спроси. Не зови для мелочей, которые можно \
                решить самостоятельно, и не более одного вопроса за раз."
                .into(),
            parameters: json!({
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Вопрос пользователю: что выбираем и почему это важно для архитектуры"
                    },
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Короткое название варианта (1–5 слов)"
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Суть и последствия варианта: компромиссы, риски, стоимость"
                                }
                            },
                            "required": ["label", "description"]
                        }
                    },
                    "recommended": {
                        "type": "string",
                        "description": "label рекомендуемого варианта (если рекомендация есть)"
                    }
                },
                "required": ["question", "options"]
            }),
        }
    }

    async fn call(&self, args: Value, _ctx: &ToolContext) -> Result<ToolOutput> {
        let question = args
            .get("question")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        let mut options = parse_options(&args);
        if question.is_empty() || options.len() < 2 {
            return Ok(ToolOutput::err(
                "propose_options: нужны непустой question и минимум 2 варианта options",
            ));
        }
        options.truncate(MAX_OPTIONS);
        let recommended = args
            .get("recommended")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string);

        let Some(bridge) = &_ctx.ask else {
            // Headless (arch-ml run, bench): UI нет — не ломаем ход, а переводим
            // выбор в текстовый план: модель перечислит варианты в ответе.
            return Ok(ToolOutput::ok(format!(
                "Интерактивный выбор недоступен (нет TUI). Представь варианты пользователю \
                 в финальном ответе нумерованным списком с рекомендацией и дождись выбора \
                 следующим сообщением. Вопрос был: «{question}»."
            )));
        };

        let (reply_tx, reply_rx) = oneshot::channel();
        let request = AskRequest {
            question: question.clone(),
            options,
            recommended,
            reply: reply_tx,
        };
        if bridge.send(request).await.is_err() {
            return Ok(ToolOutput::ok(format!(
                "UI закрыт, вопрос «{question}» не доставлен. Представь варианты текстом \
                 в финальном ответе."
            )));
        }
        // Таймаут здесь не нужен: внешний — в агентном цикле (расширенный для
        // этого инструмента); отмена — закрытием канала при выходе из TUI.
        Ok(match reply_rx.await {
            Ok(answer) if !answer.trim().is_empty() => ToolOutput::ok(format!(
                "Пользователь выбрал: «{}» (вопрос: «{question}»). \
                 Продолжай, опираясь на этот выбор; альтернативы больше не обсуждай, \
                 если пользователь сам не вернётся к ним.",
                answer.trim()
            )),
            _ => ToolOutput::ok(format!(
                "Пользователь отказался отвечать на вопрос «{question}» (Esc). \
                 Действуй по своему усмотрению, выбери разумный дефолт и явно зафиксируй \
                 открытый вопрос в ответе."
            )),
        })
    }
}

/// Разбор текста-результата для журнала (метрика approval theater).
/// Возвращает `(выбор, был_отказ)` или `None` для не-UI исходов (headless).
#[must_use]
pub fn classify_answer(content: &str) -> Option<(String, bool)> {
    if let Some(rest) = content.strip_prefix("Пользователь выбрал: «") {
        let choice = rest.split('»').next().unwrap_or("").to_string();
        return Some((choice, false));
    }
    if content.starts_with("Пользователь отказался отвечать") {
        return Some((String::new(), true));
    }
    None
}

/// Разбирает массив `options` из аргументов; битые элементы пропускаются.
fn parse_options(args: &Value) -> Vec<AskOption> {
    args.get("options")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| {
                    let label = item.get("label").and_then(Value::as_str)?.trim();
                    if label.is_empty() {
                        return None;
                    }
                    Some(AskOption {
                        label: label.to_string(),
                        description: item
                            .get("description")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .trim()
                            .to_string(),
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use std::path::PathBuf;
    use std::sync::Arc;
    use tokio::sync::mpsc;

    fn ctx() -> ToolContext {
        ToolContext::new(PathBuf::from("/tmp"), Arc::new(Config::default()))
    }

    fn good_args() -> Value {
        json!({
            "question": "Какой брокер сообщений для событийной шины?",
            "options": [
                {"label": "Kafka", "description": "масштаб, но операционно тяжёлый"},
                {"label": "NATS", "description": "простой, но нет длительного хранения"},
                {"label": "RabbitMQ", "description": "знаком команде, но не лог"}
            ],
            "recommended": "Kafka"
        })
    }

    #[tokio::test]
    async fn headless_without_bridge_returns_text_instruction() {
        let tool = ProposeOptionsTool;
        let out = tool.call(good_args(), &ctx()).await.expect("call");
        assert!(
            !out.is_error,
            "headless — не ошибка, а инструкция: {}",
            out.content
        );
        assert!(
            out.content.contains("недоступен"),
            "инструкция: {}",
            out.content
        );
        assert!(
            out.content.contains("брокер"),
            "вопрос сохранён: {}",
            out.content
        );
    }

    #[tokio::test]
    async fn rejects_missing_question_or_single_option() {
        let tool = ProposeOptionsTool;
        let out = tool
            .call(json!({"options": [{"label": "a"}, {"label": "b"}]}), &ctx())
            .await
            .expect("call");
        assert!(out.is_error);
        let out = tool
            .call(
                json!({"question": "q", "options": [{"label": "a"}]}),
                &ctx(),
            )
            .await
            .expect("call");
        assert!(out.is_error);
    }

    #[tokio::test]
    async fn interactive_choice_comes_back_as_tool_result() {
        let (tx, mut rx) = mpsc::channel::<AskRequest>(1);
        let ctx = ctx().with_ask(tx);
        let tool = ProposeOptionsTool;
        let call = tokio::spawn(async move { tool.call(good_args(), &ctx).await });
        let req = rx.recv().await.expect("запрос дошёл до UI");
        assert_eq!(req.options.len(), 3);
        assert_eq!(req.recommended.as_deref(), Some("Kafka"));
        req.reply.send("NATS".to_string()).expect("ответ");
        let out = call.await.expect("join").expect("call");
        assert!(!out.is_error);
        assert!(
            out.content.contains("NATS"),
            "выбор в результате: {}",
            out.content
        );
    }

    #[tokio::test]
    async fn declined_choice_tells_model_to_decide_itself() {
        let (tx, mut rx) = mpsc::channel::<AskRequest>(1);
        let ctx = ctx().with_ask(tx);
        let tool = ProposeOptionsTool;
        let call = tokio::spawn(async move { tool.call(good_args(), &ctx).await });
        let req = rx.recv().await.expect("запрос");
        req.reply
            .send(String::new())
            .expect("отказ — пустая строка");
        let out = call.await.expect("join").expect("call");
        assert!(out.content.contains("отказался"), "отказ: {}", out.content);
    }

    #[tokio::test]
    async fn more_than_four_options_are_truncated() {
        let (tx, mut rx) = mpsc::channel::<AskRequest>(1);
        let ctx = ctx().with_ask(tx);
        let tool = ProposeOptionsTool;
        let args = json!({
            "question": "q",
            "options": [
                {"label": "a"}, {"label": "b"}, {"label": "c"},
                {"label": "d"}, {"label": "e"}, {"label": "f"}
            ]
        });
        let call = tokio::spawn(async move { tool.call(args, &ctx).await });
        let req = rx.recv().await.expect("запрос");
        assert_eq!(req.options.len(), MAX_OPTIONS);
        req.reply.send("a".into()).expect("ответ");
        let _ = call.await.expect("join").expect("call");
    }
}
