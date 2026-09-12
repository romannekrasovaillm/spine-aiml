//! Провайдер `DeepSeek` (`https://api.deepseek.com/v1`, OpenAI-совместимый).

use std::sync::Arc;

use crate::config::ModelConfig;
use crate::error::Result;
use crate::llm::LlmProvider;
use crate::llm::openai_compat::OpenAiCompat;

/// Базовый URL `DeepSeek` API (применяется, когда в конфиге `base_url` пуст).
const DEFAULT_BASE_URL: &str = "https://api.deepseek.com/v1";

/// Фабрика провайдера `DeepSeek` поверх [`super::openai_compat`].
/// API-ключ читается лениво на запросе, здесь не проверяется.
///
/// # Errors
/// Пустой `model`, ошибка сборки HTTP-клиента.
pub fn provider(name: &str, cfg: &ModelConfig) -> Result<Arc<dyn LlmProvider>> {
    Ok(Arc::new(OpenAiCompat::with_preset(
        name,
        cfg,
        DEFAULT_BASE_URL,
    )?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preset_base_url_applies_when_config_empty() {
        let cfg = ModelConfig {
            base_url: String::new(),
            model: "deepseek-chat".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            ..ModelConfig::default()
        };
        // Ключ ленивый — конструирование успешно; пресет виден в Debug.
        let p = provider("deepseek", &cfg).expect("конструирование без ключа");
        let dbg = format!("{p:?}");
        assert!(dbg.contains(DEFAULT_BASE_URL), "debug: {dbg}");
    }
}
