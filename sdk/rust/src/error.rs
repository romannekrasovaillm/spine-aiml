//! Ошибки SDK по §4 контракта (`sdk/CONTRACT.md`).
//!
//! `thiserror` не используется осознанно: зависимости SDK ограничены
//! `serde` + `serde_json`, `Display`/`Error` реализованы вручную.

use std::fmt;
use std::path::PathBuf;
use std::time::Duration;

/// Ошибка SDK (единый набор для трёх языков, см. §4 контракта).
///
/// Обратите внимание: `control check` с `passed=false` — это НЕ ошибка,
/// а валидный отчёт-данные (`FitnessReport` с exit 1). Исключение
/// возникает только при сбое исполнения или нарушении контракта.
#[derive(Debug)]
pub enum SpineBeError {
    /// Бинарь не найден или не исполняемый (`SPINE_BE_BIN` / `arch-ml` из PATH).
    BinaryNotFound {
        /// Путь/имя бинаря, который не удалось запустить.
        binary: PathBuf,
        /// Исходная ошибка запуска процесса.
        source: std::io::Error,
    },
    /// Истёк клиентский таймаут; процесс убит.
    Timeout {
        /// Таймаут, который был превышен.
        timeout: Duration,
    },
    /// Ненулевой exit без валидного JSON-контракта.
    ProcessFailed {
        /// Код выхода процесса (`None` — завершён сигналом или не удалось запустить).
        code: Option<i32>,
        /// Содержимое stderr процесса (или текст ошибки запуска).
        stderr: String,
    },
    /// stdout не парсится как JSON там, где контракт требует JSON.
    ContractViolation {
        /// Контекст нарушения (команда и фрагмент stdout).
        message: String,
    },
}

impl fmt::Display for SpineBeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SpineBeError::BinaryNotFound { binary, source } => write!(
                f,
                "бинарь arch-ml не найден или не исполняемый: {} ({source})",
                binary.display()
            ),
            SpineBeError::Timeout { timeout } => write!(
                f,
                "клиентский таймаут {} с: процесс arch-ml убит",
                timeout.as_secs_f64()
            ),
            SpineBeError::ProcessFailed { code, stderr } => {
                let code = code
                    .map(|c| c.to_string())
                    .unwrap_or_else(|| "сигнал".to_string());
                write!(f, "arch-ml завершился с кодом {code}: {stderr}")
            }
            SpineBeError::ContractViolation { message } => {
                write!(f, "нарушение контракта SDK: {message}")
            }
        }
    }
}

impl std::error::Error for SpineBeError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            SpineBeError::BinaryNotFound { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// Удобный псевдоним результата SDK.
pub type Result<T> = std::result::Result<T, SpineBeError>;
