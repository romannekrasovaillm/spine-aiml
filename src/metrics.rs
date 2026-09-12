//! Операционные метрики харнесса из append-only журналов сессий и отчётов.
//!
//! Идеи из обзоров (§E.24 `SOURCE_BRIEF)`: first-pass validation rate, доля
//! ошибок инструментов, стоимость в токенах, прохождение рубрик/бенчей.
//! Всё считается локально из `sessions/*.jsonl` и `reports/`.
//!
//! Учёт токенов двухуровневый: реальные счётчики из записей `usage` журнала
//! (их пишет агент по `LlmEvent::Done` со `stream_options.include_usage`) —
//! основной источник; для сессий без usage остаётся грубая оценка chars/4,
//! в выводе она явно помечена как оценка. Денежная стоимость считается
//! ТОЛЬКО по тарифам `price_in_per_1m`/`price_out_per_1m` из конфига модели —
//! выдуманного курса нет: без тарифа отчёт показывает одни токены.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::Path;

use serde::Serialize;

use crate::config::ModelConfig;
use crate::error::Result;

/// Сводные метрики.
#[derive(Debug, Clone, Default, Serialize)]
pub struct HarnessMetrics {
    /// Сессий (журналов).
    pub sessions: usize,
    /// Сообщений пользователя.
    pub user_messages: usize,
    /// Ответов ассистента.
    pub assistant_messages: usize,
    /// Вызовов инструментов.
    pub tool_calls: usize,
    /// Ошибок инструментов (`is_error`).
    pub tool_errors: usize,
    /// Грубая оценка токенов (4 символа ≈ 1 токен) — fallback: суммируется
    /// только по сессиям БЕЗ записей `usage` (там реальные счётчики ниже).
    pub approx_tokens: u64,
    /// Реальные входные токены (сумма `prompt_tokens` записей `usage`).
    pub total_prompt_tokens: u64,
    /// Реальные выходные токены (сумма `completion_tokens` записей `usage`).
    pub total_completion_tokens: u64,
    /// Сессий, давших записи `usage` (из них считаются реальные токены).
    pub sessions_with_usage: usize,
    /// Суммарная стоимость по тарифам моделей из конфига (заполняет CLI
    /// из [`cost_report`], не [`collect`]); None — тарифы не заданы.
    pub total_cost: Option<f64>,
    /// Вызовы по инструментам (имя → счётчик).
    pub tools_by_name: BTreeMap<String, usize>,
    /// Рубричных отчётов в reports/.
    pub rubric_reports: usize,
    /// Средний взвешенный балл рубрик.
    pub rubric_avg: Option<f64>,
    /// Бенч-отчётов (json), из них прошедших.
    pub bench_reports: usize,
    /// Прошедших бенчей.
    pub bench_passed: usize,
    /// Handoff-пакетов (MANIFEST.json в .arch-handoff известных? нет — cron-отчёты).
    pub cron_reports: usize,
    /// Интерактивных выборов (`propose_options`), зафиксированных в журналах.
    pub asks: usize,
    /// Из них — отказ пользователя (Esc, «реши сам»).
    pub asks_declined: usize,
    /// Из них — выбор рекомендованного варианта без изменений.
    pub asks_chose_recommended: usize,
    /// Репозиториев в реестре `repos.txt` с дрейфом AGENTS.md (lint-ошибки).
    /// Заполняется из CLI (`arch-ml metrics`), не из `collect`.
    pub agentsmd_stale: usize,
    /// Репозиториев в реестре всего (0 — реестр не задан).
    pub agentsmd_total: usize,
}

impl HarnessMetrics {
    /// Доля ошибок инструментов (0..1).
    #[must_use]
    pub fn tool_error_rate(&self) -> f64 {
        if self.tool_calls == 0 {
            0.0
        } else {
            self.tool_errors as f64 / self.tool_calls as f64
        }
    }

    /// First-pass rate: сессии без ошибок инструментов / все сессии с инструментами.
    #[must_use]
    pub fn first_pass_note(&self) -> String {
        format!("{:.1}%", (1.0 - self.tool_error_rate()) * 100.0)
    }

    /// Доля «бездумных согласий» (Esc + выбор рекомендации без изменений),
    /// % от всех интерактивных выборов. None — выборов ещё не было.
    #[must_use]
    pub fn auto_approval_pct(&self) -> Option<f64> {
        if self.asks == 0 {
            None
        } else {
            Some(
                100.0 * (self.asks_declined + self.asks_chose_recommended) as f64
                    / self.asks as f64,
            )
        }
    }

    /// Флаг approval theater (обзоры `_24_августа`: >90–95% согласий без
    /// замечаний = театр одобрений) — при выборке от 5 вопросов.
    #[must_use]
    pub fn approval_theater(&self) -> bool {
        self.asks >= 5 && self.auto_approval_pct().unwrap_or(0.0) >= 90.0
    }

    /// Проверенных результатов (рубричные отчёты + пройденные бенчи).
    fn outcomes(&self) -> usize {
        self.rubric_reports + self.bench_passed
    }

    /// Есть ли реальные счётчики токенов (хотя бы одна запись `usage`).
    #[must_use]
    pub fn has_real_usage(&self) -> bool {
        self.total_prompt_tokens + self.total_completion_tokens > 0
    }

    /// Токены на один проверенный результат: реальные usage, если они есть,
    /// иначе грубая оценка chars/4 (fallback). None — результатов ещё нет.
    #[must_use]
    pub fn tokens_per_outcome(&self) -> Option<f64> {
        let outcomes = self.outcomes();
        if outcomes == 0 {
            None
        } else {
            let tokens = if self.has_real_usage() {
                self.total_prompt_tokens + self.total_completion_tokens
            } else {
                self.approx_tokens
            };
            Some(tokens as f64 / outcomes as f64)
        }
    }

    /// Стоимость одного проверенного результата в деньгах: сумма по тарифам
    /// моделей из конфига / исходы. None — исходов нет или ни у одной модели
    /// не задан тариф (выдуманного курса нет — тогда см. [`Self::tokens_per_outcome`]).
    #[must_use]
    pub fn cost_per_outcome_money(&self) -> Option<f64> {
        let cost = self.total_cost?;
        let outcomes = self.outcomes();
        if outcomes == 0 {
            None
        } else {
            Some(cost / outcomes as f64)
        }
    }

    /// Markdown-отчёт.
    #[must_use]
    pub fn to_markdown(&self) -> String {
        let mut out = String::from("# Метрики харнесса arch-ml\n\n");
        let _ = writeln!(
            out,
            "- Сессий: **{}** (user: {}, assistant: {})",
            self.sessions, self.user_messages, self.assistant_messages
        );
        let _ = writeln!(
            out,
            "- Вызовов инструментов: **{}**, ошибок: {} ({:.1}%)",
            self.tool_calls,
            self.tool_errors,
            self.tool_error_rate() * 100.0
        );
        if self.has_real_usage() {
            let _ = writeln!(
                out,
                "- Токены (реальные, usage API): prompt **{}**, completion **{}** — {}/{} сессий с usage",
                self.total_prompt_tokens,
                self.total_completion_tokens,
                self.sessions_with_usage,
                self.sessions
            );
        }
        let _ = writeln!(
            out,
            "- Оценка токенов (fallback chars/4, сессии без usage): ~{}",
            self.approx_tokens
        );
        let _ = writeln!(
            out,
            "- Рубричных отчётов: {}, средний балл: {}",
            self.rubric_reports,
            self.rubric_avg
                .map_or_else(|| "—".into(), |a| format!("{a:.2}"))
        );
        let _ = writeln!(
            out,
            "- Бенчей: {}, прошло: {} ({})",
            self.bench_reports,
            self.bench_passed,
            if self.bench_reports > 0 {
                format!(
                    "{:.0}%",
                    100.0 * self.bench_passed as f64 / self.bench_reports as f64
                )
            } else {
                "—".into()
            }
        );
        let _ = writeln!(out, "- Cron-отчётов: {}", self.cron_reports);
        out.push_str("\n## Трансформационные KPI (обзоры _24_августа)\n\n");
        match self.auto_approval_pct() {
            Some(pct) => {
                let _ = writeln!(
                    out,
                    "- Согласия без изменений (Esc + рекомендованное): **{pct:.0}%** из {} выборов{}",
                    self.asks,
                    if self.approval_theater() {
                        " — ⚠ approval theater: решения фактически не проверяются человеком"
                    } else {
                        ""
                    }
                );
            }
            None => out.push_str("- Интерактивных выборов ещё не было (propose_options).\n"),
        }
        if self.agentsmd_total > 0 {
            let _ = writeln!(
                out,
                "- Architecture drift: **{}/{}** репозиториев реестра с дрейфом AGENTS.md",
                self.agentsmd_stale, self.agentsmd_total
            );
        }
        if let Some(cpo) = self.cost_per_outcome_money() {
            let _ = writeln!(
                out,
                "- Cost per validated outcome: **{cpo:.2}** (реальный usage × тарифы моделей / (рубрики + пройденные бенчи))"
            );
        } else if let Some(tpo) = self.tokens_per_outcome() {
            let source = if self.has_real_usage() {
                "реальные usage"
            } else {
                "оценка chars/4"
            };
            let _ = writeln!(
                out,
                "- Tokens per validated outcome: ≈ **{tpo:.0}** ({source}; тарифы не заданы — price_in_per_1m/price_out_per_1m в config.toml)"
            );
        }
        let _ = writeln!(
            out,
            "- Смета по моделям и сессиям: `arch-ml metrics --cost-report`"
        );
        if !self.tools_by_name.is_empty() {
            out.push_str("\n## Инструменты по вызовам\n\n");
            for (name, count) in &self.tools_by_name {
                let _ = writeln!(out, "- {name}: {count}");
            }
        }
        out
    }
}

/// Строка сметы по одной модели.
#[derive(Debug, Clone, Default, Serialize)]
pub struct ModelUsageRow {
    /// Идентификатор модели (из записей `usage` журнала).
    pub model: String,
    /// Сессий с usage этой модели.
    pub sessions: usize,
    /// Суммарные входные токены.
    pub prompt_tokens: u64,
    /// Суммарные выходные токены.
    pub completion_tokens: u64,
    /// Стоимость по тарифу из конфига; None — тариф не задан.
    pub cost: Option<f64>,
}

/// Строка сметы по одной сессии (журналу).
#[derive(Debug, Clone, Default, Serialize)]
pub struct SessionUsageRow {
    /// Имя файла журнала (`session-*.jsonl`).
    pub session: String,
    /// Суммарные входные токены сессии.
    pub prompt_tokens: u64,
    /// Суммарные выходные токены сессии.
    pub completion_tokens: u64,
    /// Стоимость сессии по тарифам; None — ни у одной модели сессии нет тарифа.
    pub cost: Option<f64>,
}

/// Смета по реальным записям `usage` журналов сессий.
#[derive(Debug, Clone, Default, Serialize)]
pub struct CostReport {
    /// Строки по моделям (алфавитный порядок).
    pub by_model: Vec<ModelUsageRow>,
    /// Сессии с usage (все; топ-N выбирает рендер).
    pub sessions: Vec<SessionUsageRow>,
    /// Суммарные входные токены.
    pub total_prompt_tokens: u64,
    /// Суммарные выходные токены.
    pub total_completion_tokens: u64,
    /// Суммарная стоимость по моделям с тарифом; None — тарифов нет вовсе.
    pub total_cost: Option<f64>,
}

/// Стоимость одной usage-записи по тарифу модели из конфига; None — тариф
/// не задан (оба поля `price_*_per_1m` пусты). При частичном тарифе
/// незаданная сторона считается нулём.
fn record_cost(mc: Option<&ModelConfig>, prompt: u64, completion: u64) -> Option<f64> {
    let mc = mc?;
    if mc.price_in_per_1m.is_none() && mc.price_out_per_1m.is_none() {
        return None;
    }
    Some(
        prompt as f64 * mc.price_in_per_1m.unwrap_or(0.0) / 1_000_000.0
            + completion as f64 * mc.price_out_per_1m.unwrap_or(0.0) / 1_000_000.0,
    )
}

/// Ищет конфиг модели по идентификатору из записи `usage` (поле `model`
/// в запросах API); имя секции `[models.<name>]` — запасной вариант.
fn model_config_by_id<'a>(
    models: &'a BTreeMap<String, ModelConfig>,
    model_id: &str,
) -> Option<&'a ModelConfig> {
    models
        .values()
        .find(|m| m.model == model_id)
        .or_else(|| models.get(model_id))
}

/// Читает записи `usage` одного журнала: (модель, prompt, completion).
fn parse_usage_records(text: &str) -> Vec<(String, u64, u64)> {
    let mut out = Vec::new();
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        if v.get("kind").and_then(|k| k.as_str()) != Some("usage") {
            continue;
        }
        let model = v
            .get("model")
            .and_then(|m| m.as_str())
            .unwrap_or("unknown")
            .to_string();
        let prompt = v
            .get("prompt_tokens")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(0);
        let completion = v
            .get("completion_tokens")
            .and_then(serde_json::Value::as_u64)
            .unwrap_or(0);
        out.push((model, prompt, completion));
    }
    out
}

/// Журналы сессий каталога (`session-*.jsonl`), отсортированные по имени —
/// детерминированный порядок сметы.
fn session_files(sessions_dir: &Path) -> Vec<std::path::PathBuf> {
    let mut files: Vec<std::path::PathBuf> = std::fs::read_dir(sessions_dir)
        .map(|rd| {
            rd.flatten()
                .map(|e| e.path())
                .filter(|p| {
                    p.file_name().is_some_and(|n| {
                        let n = n.to_string_lossy();
                        n.starts_with("session-") && n.ends_with(".jsonl")
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    files.sort();
    files
}

/// Смета по реальным записям `usage`: по моделям (сессии, токены, стоимость
/// при заданном тарифе) и по сессиям. Тарифы берутся из `ModelConfig`
/// (`price_in_per_1m`/`price_out_per_1m`); без тарифа — только токены.
#[must_use]
pub fn cost_report(sessions_dir: &Path, models: &BTreeMap<String, ModelConfig>) -> CostReport {
    let mut report = CostReport::default();
    // Индекс строки модели в by_model (BTreeMap — алфавитный порядок).
    let mut model_idx: BTreeMap<String, usize> = BTreeMap::new();
    for path in session_files(sessions_dir) {
        let Ok(text) = std::fs::read_to_string(&path) else {
            continue;
        };
        let records = parse_usage_records(&text);
        if records.is_empty() {
            continue;
        }
        let name = path.file_name().map_or_else(
            || path.display().to_string(),
            |n| n.to_string_lossy().into_owned(),
        );
        let mut row = SessionUsageRow {
            session: name,
            ..SessionUsageRow::default()
        };
        // Модели этой сессии — для счётчика «сессий» по модели (одна сессия
        // со сменой модели засчитывается каждой из них один раз).
        let mut seen_models: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
        for (model, prompt, completion) in records {
            let idx = *model_idx.entry(model.clone()).or_insert_with(|| {
                report.by_model.push(ModelUsageRow {
                    model: model.clone(),
                    ..ModelUsageRow::default()
                });
                report.by_model.len() - 1
            });
            let cost = record_cost(model_config_by_id(models, &model), prompt, completion);
            let mrow = &mut report.by_model[idx];
            mrow.prompt_tokens += prompt;
            mrow.completion_tokens += completion;
            if let Some(c) = cost {
                *mrow.cost.get_or_insert(0.0) += c;
                *row.cost.get_or_insert(0.0) += c;
                *report.total_cost.get_or_insert(0.0) += c;
            }
            row.prompt_tokens += prompt;
            row.completion_tokens += completion;
            report.total_prompt_tokens += prompt;
            report.total_completion_tokens += completion;
            seen_models.insert(idx);
        }
        for idx in seen_models {
            report.by_model[idx].sessions += 1;
        }
        report.sessions.push(row);
    }
    report
}

/// Текстовый рендер сметы (`arch-ml metrics --cost-report`): таблица по
/// моделям, топ-10 дорогих сессий (токены ↓), итоговая строка. Без записей
/// `usage` — понятное сообщение про fallback chars/4.
#[must_use]
pub fn render_cost_report(report: &CostReport) -> String {
    let mut out = String::from("# Смета по реальному usage (журналы сессий)\n\n");
    if report.sessions.is_empty() {
        out.push_str(
            "Записей `usage` в журналах пока нет: данные появятся после сессий, \
             где API вернул статистику токенов (`stream_options.include_usage`). \
             До этого `arch-ml metrics` показывает оценку chars/4.\n",
        );
        return out;
    }
    out.push_str("| Модель | Сессий | Prompt | Completion | Стоимость |\n");
    out.push_str("|---|---:|---:|---:|---:|\n");
    let mut unpriced: Vec<&str> = Vec::new();
    for row in &report.by_model {
        let cost = if let Some(c) = row.cost {
            format!("{c:.2}")
        } else {
            unpriced.push(row.model.as_str());
            "—".into()
        };
        let _ = writeln!(
            out,
            "| {} | {} | {} | {} | {} |",
            row.model, row.sessions, row.prompt_tokens, row.completion_tokens, cost
        );
    }
    let total_cost = report
        .total_cost
        .map_or_else(|| "—".into(), |c| format!("{c:.2}"));
    let _ = writeln!(
        out,
        "| **Итого** | {} | {} | {} | {} |",
        report.sessions.len(),
        report.total_prompt_tokens,
        report.total_completion_tokens,
        total_cost
    );
    if !unpriced.is_empty() {
        let _ = writeln!(
            out,
            "\nТариф не задан для: {} — токены учтены, в денежную сумму не входят \
             (price_in_per_1m/price_out_per_1m в config.toml).",
            unpriced.join(", ")
        );
    }

    out.push_str("\n## Топ-10 самых дорогих сессий (по токенам)\n\n");
    let mut sessions = report.sessions.clone();
    sessions.sort_by_key(|s| std::cmp::Reverse(s.prompt_tokens + s.completion_tokens));
    out.push_str("| Сессия | Prompt | Completion | Всего | Стоимость |\n");
    out.push_str("|---|---:|---:|---:|---:|\n");
    for s in sessions.iter().take(10) {
        let cost = s.cost.map_or_else(|| "—".into(), |c| format!("{c:.2}"));
        let _ = writeln!(
            out,
            "| {} | {} | {} | {} | {} |",
            s.session,
            s.prompt_tokens,
            s.completion_tokens,
            s.prompt_tokens + s.completion_tokens,
            cost
        );
    }
    let _ = writeln!(
        out,
        "\nИтог: сессий с usage: {}; prompt {}; completion {}; стоимость: {}",
        report.sessions.len(),
        report.total_prompt_tokens,
        report.total_completion_tokens,
        total_cost
    );
    out
}

/// Считает метрики по каталогам сессий и отчётов.
///
/// # Errors
/// Каталоги недоступны на чтение.
pub fn collect(sessions_dir: &Path, reports_dir: &Path) -> Result<HarnessMetrics> {
    let mut m = HarnessMetrics::default();
    for path in session_files(sessions_dir) {
        m.sessions += 1;
        if let Ok(text) = std::fs::read_to_string(&path) {
            parse_journal(&text, &mut m);
        }
    }
    collect_reports(reports_dir, &mut m);
    Ok(m)
}

/// Разбор одного журнала: счётчики сообщений/инструментов/токенов.
/// Реальные токены — из записей `usage`; оценка chars/4 копится только
/// для журналов БЕЗ usage (fallback, чтобы не двоить учёт).
fn parse_journal(text: &str, m: &mut HarnessMetrics) {
    let mut approx: u64 = 0;
    let mut saw_usage = false;
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<serde_json::Value>(line) else {
            continue;
        };
        let kind = v.get("kind").and_then(|k| k.as_str()).unwrap_or("");
        let content_len = v
            .get("content")
            .and_then(|c| c.as_str())
            .map_or(0, str::len) as u64;
        approx += content_len / 4;
        match kind {
            "user" => m.user_messages += 1,
            "assistant" => {
                m.assistant_messages += 1;
                if let Some(calls) = v.get("tool_calls").and_then(|t| t.as_array()) {
                    for c in calls {
                        if let Some(name) = c.get("name").and_then(|n| n.as_str()) {
                            m.tool_calls += 1;
                            *m.tools_by_name.entry(name.into()).or_default() += 1;
                        }
                    }
                }
            }
            "tool" => {
                let is_err = v
                    .get("is_error")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false);
                if is_err {
                    m.tool_errors += 1;
                }
            }
            "usage" => {
                saw_usage = true;
                m.total_prompt_tokens += v
                    .get("prompt_tokens")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0);
                m.total_completion_tokens += v
                    .get("completion_tokens")
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0);
            }
            "event" if v.get("event").and_then(|e| e.as_str()) == Some("ask") => {
                m.asks += 1;
                if v.get("declined")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false)
                {
                    m.asks_declined += 1;
                }
                if v.get("chose_recommended")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or(false)
                {
                    m.asks_chose_recommended += 1;
                }
            }
            _ => {}
        }
    }
    if saw_usage {
        m.sessions_with_usage += 1;
    } else {
        m.approx_tokens += approx;
    }
}

/// Сбор из отчётов: рубрики (md с «взвешенный итог»), бенчи (json), крон.
fn collect_reports(dir: &Path, m: &mut HarnessMetrics) {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return;
    };
    let mut rubric_sum = 0.0;
    for entry in rd.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with("rubric-")
            && path
                .extension()
                .is_some_and(|ext| ext.eq_ignore_ascii_case("md"))
        {
            if let Ok(text) = std::fs::read_to_string(&path) {
                m.rubric_reports += 1;
                // Ищем «X.XX/5» в строке итога.
                for line in text.lines() {
                    let low = line.to_lowercase();
                    if low.contains("взвешенн") || low.contains("weighted") {
                        // Формат отчёта rubric.rs: «**Взвешенный итог:** 4.20/5».
                        if let Some(score) = line.split_whitespace().find_map(|t| {
                            let t = t.trim_matches(['*', ':', '—']);
                            let (num, denom) = t.split_once('/')?;
                            if denom.trim().parse::<f64>().is_ok() {
                                num.parse::<f64>().ok()
                            } else {
                                None
                            }
                        }) {
                            rubric_sum += score;
                        }
                        break;
                    }
                }
            }
        } else if name.starts_with("bench-")
            && path
                .extension()
                .is_some_and(|ext| ext.eq_ignore_ascii_case("json"))
        {
            if let Ok(text) = std::fs::read_to_string(&path) {
                if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                    m.bench_reports += 1;
                    if v.get("passed")
                        .and_then(serde_json::Value::as_bool)
                        .unwrap_or(false)
                    {
                        m.bench_passed += 1;
                    }
                }
            }
        }
    }
    // Крон-отчёты в подкаталоге cron/.
    if let Ok(rd) = std::fs::read_dir(dir.join("cron")) {
        m.cron_reports = rd
            .flatten()
            .filter(|e| e.path().extension().is_some_and(|x| x == "md"))
            .count();
    }
    if m.rubric_reports > 0 {
        m.rubric_avg = Some(rubric_sum / m.rubric_reports as f64);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ask_events_feed_approval_theater_metric() {
        let mut m = HarnessMetrics::default();
        // 4 согласия (2 Esc + 2 рекомендованное) + 1 самостоятельный выбор.
        let journal = concat!(
            "{\"kind\":\"event\",\"event\":\"ask\",\"declined\":true,\"chose_recommended\":false}\n",
            "{\"kind\":\"event\",\"event\":\"ask\",\"declined\":true,\"chose_recommended\":false}\n",
            "{\"kind\":\"event\",\"event\":\"ask\",\"declined\":false,\"chose_recommended\":true}\n",
            "{\"kind\":\"event\",\"event\":\"ask\",\"declined\":false,\"chose_recommended\":true}\n",
            "{\"kind\":\"event\",\"event\":\"ask\",\"declined\":false,\"chose_recommended\":false}\n",
            "{\"kind\":\"event\",\"event\":\"compact\",\"folded\":3}\n"
        );
        parse_journal(journal, &mut m);
        assert_eq!(m.asks, 5);
        assert_eq!(m.asks_declined, 2);
        assert_eq!(m.asks_chose_recommended, 2);
        let pct = m.auto_approval_pct().expect("выборы есть");
        assert!((pct - 80.0).abs() < 0.1, "pct: {pct}");
        assert!(!m.approval_theater(), "80% — ещё не театр");
        m.asks_chose_recommended += 1; // 5/6 = 83%… нужно ≥90%
        m.asks = 6;
        assert!(!m.approval_theater());
        m.asks_declined += 4; // 9/10 = 90% — театр
        m.asks = 10;
        assert!(m.approval_theater(), "90% бездумных согласий — театр");
        // Без выборов — None, флага нет.
        let empty = HarnessMetrics::default();
        assert!(empty.auto_approval_pct().is_none());
        assert!(!empty.approval_theater());
    }

    #[test]
    fn usage_records_sum_real_tokens_and_skip_approx_fallback() {
        let mut m = HarnessMetrics::default();
        // Журнал С usage: реальные токены суммируются, оценка chars/4 — нет.
        let with_usage = concat!(
            "{\"ts\":\"t\",\"kind\":\"assistant\",\"content\":\"ответ на восемьдесят символов плюс ещё немного текста для оценки\"}\n",
            "{\"ts\":\"t\",\"kind\":\"usage\",\"model\":\"deepseek-v4-flash\",\"prompt_tokens\":1200,\"completion_tokens\":300}\n",
            "{\"ts\":\"t\",\"kind\":\"usage\",\"model\":\"deepseek-v4-flash\",\"prompt_tokens\":800,\"completion_tokens\":200}\n"
        );
        parse_journal(with_usage, &mut m);
        assert_eq!(m.total_prompt_tokens, 2000);
        assert_eq!(m.total_completion_tokens, 500);
        assert_eq!(m.sessions_with_usage, 1);
        assert_eq!(m.approx_tokens, 0, "сессия с usage не даёт оценку chars/4");
        assert!(m.has_real_usage());
        // Журнал БЕЗ usage: fallback chars/4, реальные счётчики не растут.
        let without_usage =
            "{\"ts\":\"t\",\"kind\":\"assistant\",\"content\":\"1234567812345678\"}\n";
        parse_journal(without_usage, &mut m);
        assert_eq!(m.approx_tokens, 4, "16 символов / 4");
        assert_eq!(m.sessions_with_usage, 1);
        assert_eq!(m.total_prompt_tokens, 2000);
        // В выводе видно, какие цифры реальные, какие — оценка.
        let md = m.to_markdown();
        assert!(md.contains("реальные, usage API"), "{md}");
        assert!(md.contains("fallback chars/4"), "{md}");
    }

    #[test]
    fn per_outcome_metrics_without_fantasy_tariff() {
        let mut m = HarnessMetrics::default();
        assert!(m.tokens_per_outcome().is_none());
        assert!(m.cost_per_outcome_money().is_none());
        // Без тарифа — только токены (оценка chars/4), денег нет.
        m.approx_tokens = 40_000;
        m.rubric_reports = 2;
        m.bench_passed = 2;
        let tpo = m.tokens_per_outcome().expect("outcomes есть");
        assert!((tpo - 10_000.0).abs() < 0.01, "tpo: {tpo}");
        assert!(
            m.cost_per_outcome_money().is_none(),
            "тарифа нет — денег нет"
        );
        // Реальные usage приоритетнее оценки.
        m.total_prompt_tokens = 6_000;
        m.total_completion_tokens = 2_000;
        let tpo = m.tokens_per_outcome().expect("outcomes есть");
        assert!((tpo - 2_000.0).abs() < 0.01, "tpo: {tpo}");
        // Тариф задан (total_cost заполняет CLI из cost_report) — деньги.
        m.total_cost = Some(8.0);
        let cpo = m.cost_per_outcome_money().expect("тариф есть");
        assert!((cpo - 2.0).abs() < 0.01, "cpo: {cpo}");
        let md = m.to_markdown();
        assert!(md.contains("Cost per validated outcome: **2.00**"), "{md}");
    }

    /// Фикстура конфига моделей с тарифом у `deepseek-v4-flash`.
    fn priced_models() -> BTreeMap<String, ModelConfig> {
        let mut models = BTreeMap::new();
        models.insert(
            "deepseek".to_string(),
            ModelConfig {
                model: "deepseek-v4-flash".to_string(),
                price_in_per_1m: Some(100.0),
                price_out_per_1m: Some(200.0),
                ..ModelConfig::default()
            },
        );
        models
    }

    #[test]
    fn cost_report_sums_by_model_applies_tariff_and_ranks_sessions() {
        let tmp = tempfile::tempdir().expect("tmp");
        let sessions = tmp.path().join("sessions");
        std::fs::create_dir_all(&sessions).expect("sessions");
        // Дорогая сессия: два ответа deepseek.
        std::fs::write(
            sessions.join("session-20260814-100000.jsonl"),
            concat!(
                "{\"ts\":\"t\",\"kind\":\"assistant\",\"content\":\"a\"}\n",
                "{\"ts\":\"t\",\"kind\":\"usage\",\"model\":\"deepseek-v4-flash\",\"prompt_tokens\":1000000,\"completion_tokens\":500000}\n",
                "{\"ts\":\"t\",\"kind\":\"usage\",\"model\":\"deepseek-v4-flash\",\"prompt_tokens\":1000000,\"completion_tokens\":500000}\n"
            ),
        )
        .expect("journal 1");
        // Дешёвая сессия: модель без тарифа.
        std::fs::write(
            sessions.join("session-20260815-100000.jsonl"),
            "{\"ts\":\"t\",\"kind\":\"usage\",\"model\":\"local-free\",\"prompt_tokens\":100,\"completion_tokens\":50}\n",
        )
        .expect("journal 2");
        // Сессия без usage в смету не входит.
        std::fs::write(
            sessions.join("session-20260816-100000.jsonl"),
            "{\"ts\":\"t\",\"kind\":\"user\",\"content\":\"привет\"}\n",
        )
        .expect("journal 3");

        let report = cost_report(&sessions, &priced_models());
        assert_eq!(report.sessions.len(), 2, "{report:?}");
        assert_eq!(report.by_model.len(), 2, "{report:?}");
        // by_model в алфавитном порядке: deepseek-v4-flash, local-free.
        let ds = &report.by_model[0];
        assert_eq!(ds.model, "deepseek-v4-flash");
        assert_eq!(ds.sessions, 1);
        assert_eq!(ds.prompt_tokens, 2_000_000);
        assert_eq!(ds.completion_tokens, 1_000_000);
        // 2×1M prompt × 100 + 2×0.5M completion × 200 = 200 + 200 = 400.
        let cost = ds.cost.expect("тариф задан");
        assert!((cost - 400.0).abs() < 0.01, "cost: {cost}");
        let free = &report.by_model[1];
        assert_eq!(free.model, "local-free");
        assert!(free.cost.is_none(), "без тарифа — только токены");
        let total = report.total_cost.expect("есть тарифные записи");
        assert!((total - 400.0).abs() < 0.01, "total: {total}");

        let text = render_cost_report(&report);
        assert!(
            text.contains("| deepseek-v4-flash | 1 | 2000000 | 1000000 | 400.00 |"),
            "{text}"
        );
        assert!(
            text.contains("| **Итого** | 2 | 2000100 | 1000050 | 400.00 |"),
            "{text}"
        );
        // Топ сессий: дорогая первая (0 — остаток заголовка, 1 — пустая,
        // 2 — шапка таблицы, 3 — разделитель, 4 — первая строка данных).
        let top = text.split("Топ-10").nth(1).expect("секция топа");
        let first_row = top.lines().nth(4).expect("строка топа");
        assert!(first_row.contains("session-20260814-100000.jsonl"), "{top}");
        // Модель без тарифа названа в сноске.
        assert!(text.contains("local-free"), "{text}");
        assert!(text.contains("Тариф не задан для: local-free"), "{text}");
    }

    #[test]
    fn cost_report_without_usage_shows_fallback_message() {
        let tmp = tempfile::tempdir().expect("tmp");
        let sessions = tmp.path().join("sessions");
        std::fs::create_dir_all(&sessions).expect("sessions");
        std::fs::write(
            sessions.join("session-20260814-100000.jsonl"),
            "{\"ts\":\"t\",\"kind\":\"user\",\"content\":\"привет\"}\n",
        )
        .expect("journal");
        let report = cost_report(&sessions, &priced_models());
        assert!(report.sessions.is_empty());
        assert!(report.total_cost.is_none());
        let text = render_cost_report(&report);
        assert!(
            text.contains("Записей `usage` в журналах пока нет"),
            "{text}"
        );
        assert!(text.contains("chars/4"), "{text}");
        // Пустой каталог — тот же понятный вывод, без паники.
        let report = cost_report(&tmp.path().join("nope"), &BTreeMap::new());
        assert!(render_cost_report(&report).contains("пока нет"));
    }

    #[test]
    fn collects_metrics_from_journals_and_reports() {
        let tmp = tempfile::tempdir().expect("tmp");
        let sessions = tmp.path().join("sessions");
        let reports = tmp.path().join("reports");
        std::fs::create_dir_all(&sessions).expect("sessions");
        std::fs::create_dir_all(reports.join("cron")).expect("cron");
        std::fs::write(
            sessions.join("session-20260814-100000.jsonl"),
            concat!(
                "{\"ts\":\"t\",\"kind\":\"system\",\"content\":\"sys\"}\n",
                "{\"ts\":\"t\",\"kind\":\"user\",\"content\":\"привет архитектор\"}\n",
                "{\"ts\":\"t\",\"kind\":\"assistant\",\"content\":\"\",\"tool_calls\":[{\"name\":\"bash\",\"arguments\":{}}]}\n",
                "{\"ts\":\"t\",\"kind\":\"tool\",\"is_error\":false}\n",
                "{\"ts\":\"t\",\"kind\":\"assistant\",\"content\":\"готово\"}\n"
            ),
        )
        .expect("journal");
        std::fs::write(
            reports.join("rubric-solution_architecture-20260814.md"),
            "# Отчёт\n\nВзвешенный итог: **4.20/5**\n",
        )
        .expect("rubric");
        std::fs::write(
            reports.join("bench-payment_integration-deepseek-x.json"),
            "{\"bench_name\":\"p\",\"model\":\"m\",\"response\":\"\",\"rubric_report\":null,\"passed\":true}",
        )
        .expect("bench");
        std::fs::write(reports.join("cron/kb-digest-x.md"), "# Дайджест\n").expect("cron rep");

        let m = collect(&sessions, &reports).expect("collect");
        assert_eq!(m.sessions, 1);
        assert_eq!(m.user_messages, 1);
        assert_eq!(m.assistant_messages, 2);
        assert_eq!(m.tool_calls, 1);
        assert_eq!(m.tools_by_name.get("bash"), Some(&1));
        assert_eq!(m.tool_errors, 0);
        assert_eq!(m.sessions_with_usage, 0);
        assert!(!m.has_real_usage());
        assert!(m.approx_tokens > 0, "fallback chars/4 для сессии без usage");
        assert_eq!(m.rubric_reports, 1);
        assert_eq!(m.rubric_avg, Some(4.2));
        assert_eq!(m.bench_reports, 1);
        assert_eq!(m.bench_passed, 1);
        assert_eq!(m.cron_reports, 1);
        let md = m.to_markdown();
        assert!(md.contains("4.20"), "{md}");
    }

    #[test]
    fn empty_dirs_yield_zeroes() {
        let tmp = tempfile::tempdir().expect("tmp");
        let m = collect(&tmp.path().join("nope"), &tmp.path().join("nada")).expect("collect");
        assert_eq!(m.sessions, 0);
        assert_eq!(m.tool_error_rate(), 0.0);
    }
}
