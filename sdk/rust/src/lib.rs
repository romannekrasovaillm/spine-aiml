//! Spine-BE SDK v1 для Rust — тонкий клиент поверх headless CLI `arch-ml`.
//!
//! SDK запускает процесс `arch-ml` без shell (argv-массив), читает
//! stdout/stderr/exit code и разбирает JSON по контракту v1
//! (`sdk/CONTRACT.md`). Сетевых вызовов в SDK нет.
//!
//! ```no_run
//! use spine_be_sdk::Client;
//!
//! let client = Client::new(); // бинарь: SPINE_BE_BIN → arch-ml из PATH
//! let report = client.control_check(".", None)?;
//! println!("гейт пройден: {}", report.passed);
//! # Ok::<(), spine_be_sdk::SpineBeError>(())
//! ```

mod client;
mod error;
mod types;

pub use client::{Client, DEFAULT_TIMEOUT};
pub use error::{Result, SpineBeError};
pub use types::{ArchifyReceipt, FitnessReport, LintIssue, RunResult};
