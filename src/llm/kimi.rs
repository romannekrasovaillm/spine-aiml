//! Провайдер Kimi Code (`https://api.kimi.com/coding/v1`, OpenAI-совместимый).
//!
//! Официальная coding-поверхность (доки kimi.com/code): модели `k3` /
//! `k3-256k`; старая `/v1` отдаёт 404, платформа `api.moonshot.ai` душится
//! DPI провайдера по SNI (флапает). Thinking-параметры поверхность не
//! принимает (K3 ризонит всегда); temperature — только 1 (не шлём вовсе).
//! Ключ: env `KIMI_API_KEY` или файл `~/.kimi_api_key` (см. `api_key_file`).

use std::sync::Arc;

use crate::config::ModelConfig;
use crate::error::Result;
use crate::llm::LlmProvider;
use crate::llm::openai_compat::OpenAiCompat;

/// Базовый URL coding-поверхности Kimi Code.
const DEFAULT_BASE_URL: &str = "https://api.kimi.com/coding/v1";

/// Фабрика провайдера Kimi поверх [`super::openai_compat`].
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
            model: "k3".into(),
            api_key_env: "ARCH_HARNESS_TEST_MISSING_KEY_XYZ".into(),
            ..ModelConfig::default()
        };
        // Ключ ленивый — конструирование успешно; пресет виден в Debug.
        let p = provider("kimi", &cfg).expect("конструирование без ключа");
        let dbg = format!("{p:?}");
        assert!(dbg.contains(DEFAULT_BASE_URL), "debug: {dbg}");
    }
}
