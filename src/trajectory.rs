//! Исполнитель eval траекторий: разбор эпизодов и метрики.
//!
//! Входные форматы (определяются по первой непустой некомментарной строке,
//! [`detect_format`]):
//! - [`InputFormat::EpisodeJsonl`] — эталонный JSONL (скилл
//!   harness-trajectory-schema): одна строка — один эпизод, поля `task_id`,
//!   `episode_id`, `success`, `reward`, `steps`, `tokens`,
//!   `invalid_tool_calls`, `harness`;
//! - [`InputFormat::SessionJournal`] — реальные журналы
//!   `~/.arch-ml/sessions/session-*.jsonl`: записи `{ts, kind, …}`,
//!   `kind = tool` несёт `is_error`, `tool_calls` лежат в `assistant`-записи;
//!   эпизод — журнал целиком (его файл), шаги — вызовы инструментов;
//! - [`InputFormat::Selfplay`] — эпизоды `~/library/selfplay/episodes/**`
//!   (`{episode_id, turn, actor, type, content}` + `final_reward`); эпизод —
//!   группа записей с общим `episode_id`, вердикт берётся из явного
//!   `success`, а при его отсутствии — из `final_reward > 0`.
//!
//! ЗАФИКСИРОВАННЫЙ ВЫБОР ФОРМУЛ (иначе цифры невоспроизводимы):
//! - `success_rate` — доля `success = true` среди эпизодов с известным
//!   вердиктом (`labeled`), не среди всех эпизодов;
//! - `ci95` — интервал Уилсона по числу ЗАДАЧ (группировка по `task_id`),
//!   не по эпизодам; задача успешна, если успешен хотя бы один её
//!   размеченный эпизод;
//! - `pass_at_k` — несмещённая оценка Chen et al. 2021: для задачи с `n`
//!   эпизодами и `c` успехами `1 − C(n−c, k) / C(n, k)`, усреднённая по
//!   задачам; задачи с `n < k` исключаются и считаются в [`Metrics`] как
//!   `tasks_below_k` (счётчики `episodes`/`tasks` их сохраняют);
//! - `invalid_tool_calls` — суммарно по эпизодам;
//! - `mean_steps` / `mean_tokens` — по `labeled`-эпизодам.
//!
//! Строки, которые не разбираются как JSON-объект, — ошибка с номером
//! строки: молча пропустить их значит выдать цифры по неполным данным.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::Path;

use anyhow::Context;

/// Формат входного файла траекторий.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputFormat {
    /// Эталонный JSONL эпизодов.
    EpisodeJsonl,
    /// Журнал сессии (`~/.arch-ml/sessions/session-*.jsonl`).
    SessionJournal,
    /// Selfplay-эпизоды (`~/library/selfplay/episodes/**`).
    Selfplay,
}

/// Один эпизод траектории.
#[derive(Debug, Clone, Default)]
pub struct Episode {
    /// Идентификатор задачи (группа эпизодов для pass@k/ci95).
    pub task_id: String,
    /// Идентификатор эпизода.
    pub episode_id: String,
    /// Число шагов.
    pub steps: usize,
    /// Число токенов.
    pub tokens: usize,
    /// Вердикт эпизода; `None` — не размечен.
    pub success: Option<bool>,
    /// Награда, если известна.
    pub reward: Option<f64>,
    /// Невалидные вызовы инструментов за эпизод.
    pub invalid_tool_calls: usize,
    /// Харнесс, породивший эпизод.
    pub harness: Option<String>,
}

/// Метрики набора эпизодов.
#[derive(Debug, Clone, Default)]
pub struct Metrics {
    /// Всего эпизодов.
    pub episodes: usize,
    /// Число задач (уникальных `task_id`).
    pub tasks: usize,
    /// Эпизоды с известным вердиктом.
    pub labeled: usize,
    /// Эпизоды с `success = true`.
    pub successes: usize,
    /// Доля успехов среди `labeled`.
    pub success_rate: f64,
    /// 95%-интервал Уилсона по числу задач.
    pub ci95: Option<(f64, f64)>,
    /// Несмещённая pass@k (Chen et al. 2021).
    pub pass_at_k: Option<f64>,
    /// `k` для pass@k.
    pub k: usize,
    /// Задачи с `n < k`: в pass@k не оценены, в `tasks` учтены.
    pub tasks_below_k: usize,
    /// Среднее число шагов по `labeled`.
    pub mean_steps: f64,
    /// Среднее число токенов по `labeled`.
    pub mean_tokens: f64,
    /// Суммарные невалидные вызовы инструментов.
    pub invalid_tool_calls: usize,
}

/// Определить формат по первой строке (непустой, не комментарий).
///
/// `None` — строка не распознана ни как один из известных форматов.
#[must_use]
pub fn detect_format(sample_line: &str) -> Option<InputFormat> {
    let line = sample_line.trim();
    if line.is_empty() || line.starts_with('#') {
        return None;
    }
    let v: serde_json::Value = serde_json::from_str(line).ok()?;
    if v.get("kind").is_some() {
        return Some(InputFormat::SessionJournal);
    }
    if v.get("actor").is_some() && v.get("turn").is_some() {
        return Some(InputFormat::Selfplay);
    }
    if v.get("task_id").is_some() && v.get("episode_id").is_some() {
        return Some(InputFormat::EpisodeJsonl);
    }
    None
}

/// Прочитать эпизоды из файла заданного формата.
///
/// # Errors
/// Ошибка чтения файла или разбора строки (не JSON-объект) — с номером строки.
pub fn parse_episodes(path: &std::path::Path, format: InputFormat) -> anyhow::Result<Vec<Episode>> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("чтение траекторий {}", path.display()))?;
    match format {
        InputFormat::EpisodeJsonl => parse_episode_jsonl(&text, path),
        InputFormat::SessionJournal => parse_session_journal(&text, path),
        InputFormat::Selfplay => parse_selfplay(&text, path),
    }
}

/// Посчитать метрики по эпизодам.
///
/// Формулы зафиксированы в doc-комментарии модуля.
#[must_use]
pub fn compute(episodes: &[Episode], k: usize) -> Metrics {
    let mut m = Metrics {
        episodes: episodes.len(),
        tasks: episodes
            .iter()
            .map(|e| e.task_id.as_str())
            .collect::<std::collections::BTreeSet<_>>()
            .len(),
        k,
        ..Metrics::default()
    };

    // Задача → (размеченных эпизодов, успехов среди них).
    let mut by_task: BTreeMap<&str, (usize, usize)> = BTreeMap::new();
    for e in episodes {
        m.invalid_tool_calls += e.invalid_tool_calls;
        if let Some(ok) = e.success {
            m.labeled += 1;
            if ok {
                m.successes += 1;
            }
            let slot = by_task.entry(e.task_id.as_str()).or_insert((0, 0));
            slot.0 += 1;
            if ok {
                slot.1 += 1;
            }
        }
    }

    m.success_rate = if m.labeled == 0 {
        0.0
    } else {
        m.successes as f64 / m.labeled as f64
    };

    // ci95 — по задачам с вердиктом; задача успешна при ≥1 успешном эпизоде.
    let task_successes = by_task.values().filter(|(_, c)| *c > 0).count();
    m.ci95 = wilson_interval(task_successes, by_task.len());

    // pass@k — по задачам с n ≥ k; остальные считаются неоценёнными.
    let mut sum = 0.0_f64;
    let mut counted = 0usize;
    for (n, c) in by_task.values() {
        if *n < k {
            m.tasks_below_k += 1;
            continue;
        }
        sum += pass_at_k_value(*n, *c, k);
        counted += 1;
    }
    m.pass_at_k = (counted > 0).then(|| sum / counted as f64);

    if m.labeled > 0 {
        let mut steps = 0usize;
        let mut tokens = 0usize;
        for e in episodes.iter().filter(|e| e.success.is_some()) {
            steps += e.steps;
            tokens += e.tokens;
        }
        m.mean_steps = steps as f64 / m.labeled as f64;
        m.mean_tokens = tokens as f64 / m.labeled as f64;
    }

    m
}

/// Отрисовать метрики в текст.
#[must_use]
pub fn render_metrics(m: &Metrics) -> String {
    let mut out = String::new();
    let _ = writeln!(
        out,
        "Эпизодов: {} (задач: {}, размечено: {})",
        m.episodes, m.tasks, m.labeled
    );
    let _ = writeln!(
        out,
        "success rate: {:.3}  mean steps: {:.1}  mean tokens: {:.1}",
        m.success_rate, m.mean_steps, m.mean_tokens
    );
    let _ = writeln!(out, "invalid tool calls: {}", m.invalid_tool_calls);
    if let Some((lo, hi)) = m.ci95 {
        let _ = writeln!(out, "ci95: [{lo:.3}, {hi:.3}]");
    }
    if let Some(p) = m.pass_at_k {
        let _ = writeln!(out, "pass@{}: {p:.3}", m.k);
    }
    if m.tasks_below_k > 0 {
        let _ = writeln!(
            out,
            "pass@{}: не оценено задач (n < {}): {}",
            m.k, m.k, m.tasks_below_k
        );
    }
    out
}

/// Разобрать эталонный JSONL: одна строка — один эпизод.
fn parse_episode_jsonl(text: &str, path: &Path) -> anyhow::Result<Vec<Episode>> {
    let mut out = Vec::new();
    for (line_no, line) in significant_lines(text) {
        let v = parse_object(line, line_no, path)?;
        let episode_id = json_str(&v, "episode_id").unwrap_or_default();
        let task_id = json_str(&v, "task_id")
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| episode_id.clone());
        out.push(Episode {
            task_id,
            episode_id,
            steps: json_usize(&v, "steps").unwrap_or(0),
            tokens: json_usize(&v, "tokens").unwrap_or(0),
            success: json_bool(&v, "success"),
            reward: json_f64(&v, "reward"),
            invalid_tool_calls: json_usize(&v, "invalid_tool_calls").unwrap_or(0),
            harness: json_str(&v, "harness"),
        });
    }
    Ok(out)
}

/// Разобрать журнал сессии: эпизод — журнал целиком.
fn parse_session_journal(text: &str, path: &Path) -> anyhow::Result<Vec<Episode>> {
    let lines = significant_lines(text);
    if lines.is_empty() {
        return Ok(Vec::new());
    }
    let mut ep = Episode {
        episode_id: session_id(path),
        ..Episode::default()
    };
    let mut tokens = 0usize;
    let mut calls = 0usize;
    let mut tool_records = 0usize;

    for (line_no, line) in lines {
        let v = parse_object(line, line_no, path)?;
        match json_str(&v, "kind").unwrap_or_default().as_str() {
            "assistant" => {
                if let Some(list) = v.get("tool_calls").and_then(serde_json::Value::as_array) {
                    calls += list.len();
                }
            }
            "tool" => {
                tool_records += 1;
                if json_bool(&v, "is_error") == Some(true) {
                    ep.invalid_tool_calls += 1;
                }
            }
            "usage" => {
                tokens += json_usize(&v, "prompt_tokens").unwrap_or(0);
                tokens += json_usize(&v, "completion_tokens").unwrap_or(0);
            }
            _ => {}
        }
        if let Some(ok) = json_bool(&v, "success") {
            ep.success = Some(ok);
        }
        if let Some(r) = json_f64(&v, "reward") {
            ep.reward = Some(r);
        }
        if ep.harness.is_none() {
            ep.harness = json_str(&v, "harness");
        }
    }

    // Шаги — вызовы инструментов из `assistant`-записей; журналы без них
    // (например, только `tool`-записи) считаются по числу исполнений.
    ep.steps = if calls > 0 { calls } else { tool_records };
    ep.tokens = tokens;
    if ep.task_id.is_empty() {
        ep.task_id.clone_from(&ep.episode_id);
    }
    Ok(vec![ep])
}

/// Разобрать selfplay: эпизод — группа записей с общим `episode_id`.
fn parse_selfplay(text: &str, path: &Path) -> anyhow::Result<Vec<Episode>> {
    let mut by_episode: BTreeMap<String, Episode> = BTreeMap::new();
    let mut final_rewards: BTreeMap<String, f64> = BTreeMap::new();

    for (line_no, line) in significant_lines(text) {
        let v = parse_object(line, line_no, path)?;
        let episode_id = json_str(&v, "episode_id").unwrap_or_default();
        let task_id = json_str(&v, "task_id")
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| episode_id.clone());
        let ep = by_episode
            .entry(episode_id.clone())
            .or_insert_with(|| Episode {
                task_id,
                episode_id: episode_id.clone(),
                ..Episode::default()
            });
        ep.steps += 1;
        ep.tokens += json_usize(&v, "tokens").unwrap_or(0);
        if json_bool(&v, "is_error") == Some(true) {
            ep.invalid_tool_calls += 1;
        }
        if ep.harness.is_none() {
            ep.harness = json_str(&v, "harness");
        }
        if let Some(ok) = json_bool(&v, "success") {
            ep.success = Some(ok);
        }
        if let Some(r) = json_f64(&v, "final_reward").or_else(|| json_f64(&v, "reward")) {
            final_rewards.insert(episode_id.clone(), r);
        }
    }

    for (id, ep) in &mut by_episode {
        let reward = final_rewards.get(id).copied();
        ep.reward = reward;
        // Явный `success` старше выведенного из награды; бинарные среды
        // (награда 0/1) дают ровно тот же вердикт.
        if ep.success.is_none() {
            ep.success = reward.map(|r| r > 0.0);
        }
    }

    Ok(by_episode.into_values().collect())
}

/// Значимые строки файла: без пустых и без комментариев, с номерами (1-based).
fn significant_lines(text: &str) -> Vec<(usize, &str)> {
    text.lines()
        .enumerate()
        .filter(|(_, l)| {
            let t = l.trim();
            !t.is_empty() && !t.starts_with('#')
        })
        .map(|(i, l)| (i + 1, l))
        .collect()
}

/// Разобрать строку как JSON-объект; иначе — ошибка с номером строки.
fn parse_object(line: &str, line_no: usize, path: &Path) -> anyhow::Result<serde_json::Value> {
    let v: serde_json::Value = serde_json::from_str(line)
        .with_context(|| format!("строка {line_no} файла {}: не JSON", path.display()))?;
    if !v.is_object() {
        anyhow::bail!(
            "строка {line_no} файла {}: ожидался JSON-объект",
            path.display()
        );
    }
    Ok(v)
}

/// Строковое поле объекта.
fn json_str(v: &serde_json::Value, key: &str) -> Option<String> {
    v.get(key)
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
}

/// Целочисленное поле объекта.
fn json_usize(v: &serde_json::Value, key: &str) -> Option<usize> {
    v.get(key)
        .and_then(serde_json::Value::as_u64)
        .map(|n| n as usize)
}

/// Вещественное поле объекта.
fn json_f64(v: &serde_json::Value, key: &str) -> Option<f64> {
    v.get(key).and_then(serde_json::Value::as_f64)
}

/// Булево поле объекта.
fn json_bool(v: &serde_json::Value, key: &str) -> Option<bool> {
    v.get(key).and_then(serde_json::Value::as_bool)
}

/// Идентификатор сессии — имя файла журнала без расширения.
fn session_id(path: &Path) -> String {
    path.file_stem().map_or_else(
        || path.display().to_string(),
        |s| s.to_string_lossy().into_owned(),
    )
}

/// Интервал Уилсона (95%, z = 1.96) для `successes` из `n`; `None` при `n = 0`.
fn wilson_interval(successes: usize, n: usize) -> Option<(f64, f64)> {
    if n == 0 {
        return None;
    }
    let n = n as f64;
    let p = successes as f64 / n;
    let z = 1.96_f64;
    let z2 = z * z;
    let denom = 1.0 + z2 / n;
    let center = (p + z2 / (2.0 * n)) / denom;
    let margin = (z / denom) * (p * (1.0 - p) / n + z2 / (4.0 * n * n)).sqrt();
    Some(((center - margin).max(0.0), (center + margin).min(1.0)))
}

/// Несмещённая pass@k для задачи с `n` эпизодами и `c` успехами:
/// `1 − C(n−c, k) / C(n, k)` (Chen et al. 2021, без численных комбинаторик).
fn pass_at_k_value(n: usize, c: usize, k: usize) -> f64 {
    if k == 0 {
        return 0.0;
    }
    if n.saturating_sub(c) < k {
        return 1.0;
    }
    let mut ratio = 1.0_f64;
    for i in 0..k {
        ratio *= (n - c - i) as f64 / (n - i) as f64;
    }
    (1.0 - ratio).clamp(0.0, 1.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Эпизод с заданными задачей, вердиктом и счётчиками.
    fn episode(task: &str, id: &str, success: Option<bool>, steps: usize) -> Episode {
        Episode {
            task_id: task.to_string(),
            episode_id: id.to_string(),
            steps,
            tokens: steps * 10,
            success,
            ..Episode::default()
        }
    }

    /// Файл во временном каталоге.
    fn write_tmp(text: &str, name: &str) -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().expect("временный каталог");
        let path = dir.path().join(name);
        std::fs::write(&path, text).expect("файл траекторий");
        (dir, path)
    }

    #[test]
    fn detects_session_journal_line() {
        let line = r#"{"content":"x","is_error":false,"kind":"tool","name":"skill_search","ts":"2026-09-13T05:32:12.370+03:00"}"#;
        assert_eq!(detect_format(line), Some(InputFormat::SessionJournal));
    }

    #[test]
    fn detects_selfplay_and_episode_lines() {
        let selfplay = r#"{"episode_id":"e1","turn":0,"actor":"env","type":"task"}"#;
        let episode = r#"{"task_id":"t1","episode_id":"e1","success":true}"#;
        assert_eq!(detect_format(selfplay), Some(InputFormat::Selfplay));
        assert_eq!(detect_format(episode), Some(InputFormat::EpisodeJsonl));
        assert_eq!(detect_format("# баннер"), None);
        assert_eq!(detect_format("   "), None);
        assert_eq!(detect_format(r#"{"no":"known fields"}"#), None);
    }

    #[test]
    fn parse_episode_jsonl_reads_fields() {
        let (_dir, path) = write_tmp(
            "# шапка\n\
             {\"task_id\":\"t1\",\"episode_id\":\"e1\",\"success\":true,\"steps\":3,\"tokens\":30,\"harness\":\"claude\"}\n\
             \n\
             {\"task_id\":\"t1\",\"episode_id\":\"e2\",\"success\":false,\"steps\":5,\"tokens\":50,\"invalid_tool_calls\":2}\n",
            "episodes.jsonl",
        );
        let eps = parse_episodes(&path, InputFormat::EpisodeJsonl).expect("разбор");
        assert_eq!(eps.len(), 2);
        assert_eq!(eps[0].task_id, "t1");
        assert_eq!(eps[0].success, Some(true));
        assert_eq!(eps[0].steps, 3);
        assert_eq!(eps[0].harness.as_deref(), Some("claude"));
        assert_eq!(eps[1].invalid_tool_calls, 2);
    }

    #[test]
    fn parse_episode_jsonl_falls_back_to_episode_task() {
        let (_dir, path) = write_tmp(r#"{"episode_id":"e1","success":true}"#, "e.jsonl");
        let eps = parse_episodes(&path, InputFormat::EpisodeJsonl).expect("разбор");
        assert_eq!(eps[0].task_id, "e1");
    }

    #[test]
    fn parse_episode_jsonl_rejects_broken_line() {
        let (_dir, path) = write_tmp("{\"task_id\":\"t\"}\nне json\n", "broken.jsonl");
        let err = parse_episodes(&path, InputFormat::EpisodeJsonl).expect_err("должна быть ошибка");
        assert!(err.to_string().contains("строка 2"), "{err}");
    }

    #[test]
    fn parse_empty_file_yields_no_episodes() {
        let (_dir, path) = write_tmp("", "empty.jsonl");
        for fmt in [
            InputFormat::EpisodeJsonl,
            InputFormat::SessionJournal,
            InputFormat::Selfplay,
        ] {
            let eps = parse_episodes(&path, fmt).expect("пустой вход");
            assert!(eps.is_empty(), "{fmt:?}");
        }
        let m = compute(&[], 4);
        assert_eq!(m.episodes, 0);
        assert_eq!(m.tasks, 0);
        assert_eq!(m.labeled, 0);
        assert_eq!(m.k, 4);
        assert_eq!(m.success_rate, 0.0);
        assert_eq!(m.mean_steps, 0.0);
        assert_eq!(m.mean_tokens, 0.0);
        assert!(m.ci95.is_none());
        assert!(m.pass_at_k.is_none());
        assert_eq!(m.tasks_below_k, 0);
    }

    #[test]
    fn parse_session_journal_collects_counters() {
        let (_dir, path) = write_tmp(
            "{\"ts\":\"t\",\"kind\":\"system\",\"content\":\"prompt\"}\n\
             {\"ts\":\"t\",\"kind\":\"assistant\",\"tool_calls\":[{\"name\":\"bash\"},{\"name\":\"fs\"}]}\n\
             {\"ts\":\"t\",\"kind\":\"tool\",\"name\":\"bash\",\"is_error\":true}\n\
             {\"ts\":\"t\",\"kind\":\"tool\",\"name\":\"fs\",\"is_error\":false}\n\
             {\"ts\":\"t\",\"kind\":\"usage\",\"prompt_tokens\":10,\"completion_tokens\":5}\n",
            "session-20260913-120000.jsonl",
        );
        let eps = parse_episodes(&path, InputFormat::SessionJournal).expect("разбор");
        assert_eq!(eps.len(), 1);
        assert_eq!(eps[0].episode_id, "session-20260913-120000");
        assert_eq!(eps[0].task_id, "session-20260913-120000");
        assert_eq!(eps[0].steps, 2);
        assert_eq!(eps[0].tokens, 15);
        assert_eq!(eps[0].invalid_tool_calls, 1);
        assert_eq!(eps[0].success, None, "в журнале вердикта нет");
    }

    #[test]
    fn parse_selfplay_derives_verdict_from_final_reward() {
        let (_dir, path) = write_tmp(
            "{\"episode_id\":\"e1\",\"turn\":0,\"actor\":\"env\",\"type\":\"task\",\"content\":\"...\"}\n\
             {\"episode_id\":\"e1\",\"turn\":1,\"actor\":\"agent\",\"type\":\"action\",\"tokens\":7}\n\
             {\"episode_id\":\"e1\",\"turn\":2,\"actor\":\"env\",\"type\":\"final\",\"final_reward\":1.0}\n\
             {\"episode_id\":\"e2\",\"turn\":0,\"actor\":\"env\",\"type\":\"task\"}\n\
             {\"episode_id\":\"e2\",\"turn\":1,\"actor\":\"env\",\"type\":\"final\",\"final_reward\":0.0}\n",
            "episodes.jsonl",
        );
        let eps = parse_episodes(&path, InputFormat::Selfplay).expect("разбор");
        assert_eq!(eps.len(), 2);
        assert_eq!(eps[0].episode_id, "e1");
        assert_eq!(eps[0].steps, 3);
        assert_eq!(eps[0].tokens, 7);
        assert_eq!(eps[0].reward, Some(1.0));
        assert_eq!(eps[0].success, Some(true));
        assert_eq!(eps[1].success, Some(false));
    }

    #[test]
    fn parse_selfplay_prefers_explicit_success() {
        let (_dir, path) = write_tmp(
            "{\"episode_id\":\"e1\",\"turn\":0,\"actor\":\"env\",\"type\":\"task\"}\n\
             {\"episode_id\":\"e1\",\"turn\":1,\"actor\":\"env\",\"type\":\"final\",\"final_reward\":0.5,\"success\":false}\n",
            "e.jsonl",
        );
        let eps = parse_episodes(&path, InputFormat::Selfplay).expect("разбор");
        assert_eq!(eps[0].success, Some(false));
        assert_eq!(eps[0].reward, Some(0.5));
    }

    #[test]
    fn success_rate_uses_labeled_episodes_only() {
        let eps = vec![
            episode("t1", "e1", Some(true), 1),
            episode("t1", "e2", Some(false), 3),
            episode("t2", "e3", None, 5),
        ];
        let m = compute(&eps, 1);
        assert_eq!(m.episodes, 3);
        assert_eq!(m.tasks, 2);
        assert_eq!(m.labeled, 2);
        assert_eq!(m.successes, 1);
        assert_eq!(m.success_rate, 0.5);
        // mean_steps — только по размеченным: (1 + 3) / 2.
        assert_eq!(m.mean_steps, 2.0);
        assert_eq!(m.mean_tokens, 20.0);
    }

    #[test]
    fn ci95_groups_by_task_not_episodes() {
        // Задача A: 3 эпизода, 1 успех. Задача B: 1 эпизод, 1 успех.
        // По эпизодам доля 0.5; по задачам — 1.0 (обе задачи успешны).
        let eps = vec![
            episode("a", "a1", Some(true), 1),
            episode("a", "a2", Some(false), 1),
            episode("a", "a3", Some(false), 1),
            episode("b", "b1", Some(true), 1),
        ];
        let m = compute(&eps, 1);
        let (lo, hi) = m.ci95.expect("интервал");
        assert!(lo > 0.3, "нижняя граница по задачам: {lo}");
        assert!(hi <= 1.0);
    }

    #[test]
    fn ci95_matches_wilson_for_four_tasks() {
        let eps = vec![
            episode("t1", "1", Some(true), 1),
            episode("t2", "2", Some(true), 1),
            episode("t3", "3", Some(false), 1),
            episode("t4", "4", Some(false), 1),
        ];
        let (lo, hi) = compute(&eps, 1).ci95.expect("интервал");
        assert!((lo - 0.15004).abs() < 1e-3, "{lo}");
        assert!((hi - 0.84996).abs() < 1e-3, "{hi}");
    }

    #[test]
    fn pass_at_k_is_unbiased_estimate() {
        // Задача A: n=4, c=2, k=2 → 1 − C(2,2)/C(4,2) = 5/6.
        // Задача B: n=2, c=1, k=2 → 1 − C(1,2)/C(2,2) = 1.
        let eps = vec![
            episode("a", "a1", Some(true), 1),
            episode("a", "a2", Some(true), 1),
            episode("a", "a3", Some(false), 1),
            episode("a", "a4", Some(false), 1),
            episode("b", "b1", Some(true), 1),
            episode("b", "b2", Some(false), 1),
        ];
        let m = compute(&eps, 2);
        let expected = f64::midpoint(5.0 / 6.0, 1.0);
        let got = m.pass_at_k.expect("pass@2");
        assert!((got - expected).abs() < 1e-9, "{got} vs {expected}");
        assert_eq!(m.tasks_below_k, 0);
    }

    #[test]
    fn pass_at_one_is_mean_per_task_success_rate() {
        // k=1: 1 − C(n−c,1)/C(n,1) = c/n, усреднённое по задачам.
        let eps = vec![
            episode("a", "a1", Some(true), 1),
            episode("a", "a2", Some(false), 1),
            episode("b", "b1", Some(true), 1),
        ];
        let got = compute(&eps, 1).pass_at_k.expect("pass@1");
        assert!((got - f64::midpoint(0.5, 1.0)).abs() < 1e-9, "{got}");
    }

    #[test]
    fn pass_at_k_excludes_tasks_below_k() {
        // Единственная задача с n=1 при k=4: не оценена, но посчитана.
        let eps = vec![episode("a", "a1", Some(true), 1)];
        let m = compute(&eps, 4);
        assert_eq!(m.episodes, 1);
        assert_eq!(m.tasks, 1);
        assert_eq!(m.tasks_below_k, 1);
        assert!(m.pass_at_k.is_none());
    }

    #[test]
    fn pass_at_k_mixes_evaluable_and_below_k_tasks() {
        let eps = vec![
            episode("small", "s1", Some(true), 1),
            episode("big", "b1", Some(true), 1),
            episode("big", "b2", Some(false), 1),
            episode("big", "b3", Some(false), 1),
            episode("big", "b4", Some(true), 1),
        ];
        let m = compute(&eps, 4);
        assert_eq!(m.tasks_below_k, 1);
        // Только задача `big`: n=4, c=2, k=4 → 1 − C(2,4)/C(4,4) = 1.
        let got = m.pass_at_k.expect("pass@4");
        assert!((got - 1.0).abs() < 1e-9, "{got}");
    }

    #[test]
    fn metrics_ignore_unlabeled_tasks_for_pass_at_k() {
        let eps = vec![
            episode("a", "a1", Some(true), 1),
            episode("a", "a2", None, 1),
            episode("b", "b1", None, 1),
            episode("b", "b2", None, 1),
        ];
        let m = compute(&eps, 2);
        // Размечена только задача `a` с n=1 < 2 — не оценена, `b` без вердикта.
        assert_eq!(m.labeled, 1);
        assert_eq!(m.tasks, 2);
        assert_eq!(m.tasks_below_k, 1);
        assert!(m.pass_at_k.is_none());
        // Одна размеченная задача с успехом: Уилсон(1, 1) ≈ [0.207, 1.0].
        let (lo, hi) = m.ci95.expect("интервал");
        assert!((lo - 0.20654).abs() < 1e-3, "{lo}");
        assert!(hi <= 1.0, "{hi}");
    }

    #[test]
    fn pass_at_k_clamps_when_all_succeed() {
        let eps = vec![
            episode("a", "a1", Some(true), 1),
            episode("a", "a2", Some(true), 1),
        ];
        assert_eq!(pass_at_k_value(2, 2, 2), 1.0);
        assert_eq!(compute(&eps, 2).pass_at_k, Some(1.0));
    }

    #[test]
    fn invalid_tool_calls_are_summed() {
        let mut a = episode("t", "1", Some(true), 1);
        a.invalid_tool_calls = 2;
        let mut b = episode("t", "2", Some(false), 1);
        b.invalid_tool_calls = 3;
        assert_eq!(compute(&[a, b], 1).invalid_tool_calls, 5);
    }

    #[test]
    fn render_metrics_mentions_unassessed_tasks() {
        let eps = vec![episode("a", "a1", Some(true), 1)];
        let text = render_metrics(&compute(&eps, 4));
        assert!(text.contains("Эпизодов: 1"), "{text}");
        assert!(text.contains("не оценено задач"), "{text}");
    }
}
