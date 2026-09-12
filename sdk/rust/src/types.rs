//! Типы данных контракта v1 (`sdk/CONTRACT.md`, §1–§3).
//!
//! Типизированы только поля, зафиксированные контрактом; всё остальное
//! доступно как generic JSON (`serde_json::Value`) — добавление новых
//! полей в ответы `arch-ml` не ломает SDK.

use serde::Deserialize;
use std::time::Duration;

/// Результат headless-прогона агента `arch-ml run -q` (§1 контракта).
#[derive(Debug, Clone)]
pub struct RunResult {
    /// Финальный ответ ассистента (stdout целиком, без завершающего перевода строки).
    pub answer: String,
    /// Клиентская длительность прогона (wall time от spawn до exit).
    pub duration: Duration,
}

/// Отчёт fitness-контроля `arch-ml control check --json` (§2 контракта).
///
/// `passed=false` — это данные (красный гейт), а не ошибка исполнения.
#[derive(Debug, Clone, Deserialize)]
pub struct FitnessReport {
    /// Репозиторий, на котором запускался контроль.
    pub repo: String,
    /// Гейт пройден (`true`) или есть нарушения (`false`, exit 1 у CLI).
    pub passed: bool,
    /// Человекочитаемая сводка («Правил: 3, нарушений: 0 (error: 0, warn: 0)»).
    pub summary: String,
    /// Находки; пусто при `passed=true`.
    #[serde(default)]
    pub issues: Vec<LintIssue>,
}

/// Одна находка fitness-контроля (§2 контракта).
#[derive(Debug, Clone, Deserialize)]
pub struct LintIssue {
    /// Файл с нарушением (относительно репозитория).
    pub file: String,
    /// Строка; `0` — находка на файл целиком.
    pub line: u32,
    /// Имя правила из CONSTRAINTS.yaml.
    pub rule: String,
    /// Текст нарушения.
    pub message: String,
    /// Критичность: `"error"` | `"warn"` (строка — для совместимости с новыми значениями).
    pub severity: String,
}

/// Receipt команд `archify` (validate / deliver / compare), §3 контракта.
///
/// Обёртка над `serde_json::Value`: receipt передаётся вызывающему коду
/// целиком, типизированы только общие поля контракта — остальное читается
/// через [`ArchifyReceipt::raw`].
#[derive(Debug, Clone)]
pub struct ArchifyReceipt {
    raw: serde_json::Value,
}

impl ArchifyReceipt {
    /// Оборачивает распарсенный JSON-receipt.
    pub(crate) fn new(raw: serde_json::Value) -> Self {
        Self { raw }
    }

    /// Весь receipt как generic JSON (поля вне контракта).
    pub fn raw(&self) -> &serde_json::Value {
        &self.raw
    }

    /// Разбирает receipt обратно в `serde_json::Value`.
    pub fn into_raw(self) -> serde_json::Value {
        self.raw
    }

    /// Версия схемы receipt'а (контракт v1 — `1`).
    pub fn schema_version(&self) -> Option<u64> {
        self.raw.get("schemaVersion")?.as_u64()
    }

    /// Признак успеха команды (`ok`).
    pub fn ok(&self) -> bool {
        self.raw
            .get("ok")
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false)
    }

    /// Имя команды: `"validate"` | `"deliver"` | `"compare"`.
    pub fn command(&self) -> Option<&str> {
        self.raw.get("command")?.as_str()
    }

    /// Тип диаграммы (`"architecture"`, `"workflow"`, …).
    pub fn diagram_type(&self) -> Option<&str> {
        self.raw.get("type")?.as_str()
    }

    /// Сводка команды (для compare — счётчики added/changed/removed и т.п.).
    pub fn summary(&self) -> Option<&serde_json::Value> {
        self.raw.get("summary")
    }

    /// Список проверок валидации (validate / validation внутри deliver).
    pub fn checks(&self) -> Option<&[serde_json::Value]> {
        self.raw.get("checks")?.as_array().map(Vec::as_slice)
    }

    /// Сведения о собранном артефакте (deliver/compare: sha256, bytes).
    pub fn artifact(&self) -> Option<&serde_json::Value> {
        self.raw.get("artifact")
    }
}
