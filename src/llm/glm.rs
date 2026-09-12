//! Провайдер GLM/Zhipu (`https://api.z.ai/api/paas/v4`, OpenAI-совместимый).

use std::sync::Arc;

use crate::config::ModelConfig;
use crate::error::Result;
use crate::llm::LlmProvider;
use crate::llm::openai_compat::OpenAiCompat;

/// Базовый URL GLM API (применяется, когда в конфиге `base_url` пуст).
/// Международная площадка Z.AI; китайский аналог — open.bigmodel.cn
/// (тот же аккаунт/ключ, но извне Китая ловит DPI-таймауты и вдвое медленнее).
const DEFAULT_BASE_URL: &str = "https://api.z.ai/api/paas/v4";

/// Фабрика провайдера GLM поверх [`super::openai_compat`].
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
            model: "glm-5.2".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            ..ModelConfig::default()
        };
        // Ключ ленивый — конструирование успешно; пресет виден в Debug.
        let p = provider("glm", &cfg).expect("конструирование без ключа");
        let dbg = format!("{p:?}");
        assert!(dbg.contains(DEFAULT_BASE_URL), "debug: {dbg}");
    }
}
