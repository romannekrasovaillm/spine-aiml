//! Гейт приёмки: вердикт ревьювера блокирует интеграцию (ADR-046, п. 2).
//!
//! `worktree accept` перед merge читает журналы прогонов флота
//! (`<state_dir>/fleet/*.jsonl`) и ищет вердикты ревьюверов, отнесённые к
//! принимаемой ветке `arch/<name>`:
//!
//! - последний вердикт по ветке — `NOT-READY` → отказ с текстом находок,
//!   обход — только явным именным аппрувером (запись в журнал приёмки);
//! - последний вердикт — `READY` → merge без вопросов;
//! - вердиктов по ветке нет → merge с явной пометкой «вердиктов ревью не
//!   найдено — не проверено» (отсутствие проверки ≠ успех: журнал мог быть
//!   очищен).
//!
//! Связь «вердикт ↔ ветка» берётся из СТРУКТУРНЫХ полей журнала, без догадок
//! и без разбора текста: ревьювер судит код в СВОЁМ дереве, куда ветки
//! зависимостей влиты. Отсюда два признака связи, оба проверяемые:
//!
//! 1) ветка `arch/<name>` влита в дерево узла-ревьювера — структурное поле
//!    `branch` события вливания зависимости (`gate = "dependency:<id>"`,
//!    `branch = Some("arch/<имя>")`; паттерн `review_pair`: узел
//!    `review-<worker>` зависит от `<worker>`);
//! 2) `node_id` вердикта совпадает с именем принимаемого worktree — вердикт
//!    о собственной ветке узла.
//!
//! Текст `detail` не читается: формулировка — не контракт, её правка молча
//! ломала бы гейт. Журнал старого формата структурного поля не несёт — это
//! честное «вердиктов по ветке не найдено» (не ложный READY/NOT-READY).
//!
//! Нет ни одного признака — вердикт к этой ветке не относится (связь по
//! догадке не выдумывается: несовпадение честнее ложной блокировки).
//!
//! Журнал приёмки (`<state_dir>/accept/decisions.jsonl`) — append-only:
//! решение именного аппрувера (кто, ветка, вердикт, время, находки).
//! Запись фиксирует решение, но НЕ закрывает вердикт ревьювера: журнал
//! флота append-only и не переписывается, поэтому обход требуется каждый раз.

use std::collections::HashMap;
// Оба трейта анонимны: `write!`/`writeln!` в `String` — `fmt::Write`,
// допись строки журнала одним вызовом — `io::Write`.
use std::fmt::Write as _;
use std::io::Write as _;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{HarnessError, Result};
use crate::fleet_gate::ReviewVerdict;
use crate::fleet_run::{FleetEvent, FleetLog};

/// Каталог журналов прогонов флота внутри `state_dir`.
const FLEET_SUBDIR: &str = "fleet";

/// Каталог журнала приёмки внутри `state_dir`.
const ACCEPT_SUBDIR: &str = "accept";

/// Имя append-only файла решений приёмки.
const DECISIONS_FILE: &str = "decisions.jsonl";

/// Ключ вердикта «проверки не было» (в журнале приёмки).
pub const VERDICT_UNVERIFIED: &str = "unverified";

/// Вердикт ревьювера, отнесённый к принимаемой ветке.
#[derive(Debug, Clone)]
pub struct BranchVerdict {
    /// Прогон флота, в журнале которого найден вердикт.
    pub run_id: String,
    /// Узел-ревьювер.
    pub node_id: String,
    /// Вердикт гейта `Review`.
    pub verdict: ReviewVerdict,
    /// Находки ревьювера (из лога узла; пусто — разобрать не удалось).
    pub findings: Vec<String>,
    /// Как вердикт связан с веткой (признак, найденный в журнале).
    pub link: String,
}

/// Итог чтения журналов флота по принимаемой ветке.
#[derive(Debug, Clone)]
pub struct ReviewScan {
    /// Сколько журналов `<state_dir>/fleet/*.jsonl` прочитано.
    pub journals: usize,
    /// Сколько строк журналов пропущено толерантным разбором.
    pub skipped_lines: usize,
    /// Последний по журналу вердикт, относящийся к ветке (`None` — не найдено).
    pub last: Option<BranchVerdict>,
}

/// Список журналов прогонов флота в лексикографическом порядке имён.
///
/// Run-id начинается со слага и timestamp (`fpl-<millis>`, `<слаг>-<yyyymmddhhmmss>`),
/// поэтому порядок имён совпадает с хронологией прогонов — «последний
/// вердикт» читается детерминированно, без разбора отметок времени.
fn journal_paths(dir: &Path) -> Result<Vec<PathBuf>> {
    if !dir.is_dir() {
        return Ok(Vec::new());
    }
    let mut out = Vec::new();
    for entry in std::fs::read_dir(dir).map_err(|e| HarnessError::io(dir, e))? {
        let path = entry.map_err(|e| HarnessError::io(dir, e))?.path();
        if path.extension().and_then(std::ffi::OsStr::to_str) == Some("jsonl") {
            out.push(path);
        }
    }
    out.sort();
    Ok(out)
}

/// Вердикт ревьювера из значения гейта: `ok` — READY, `fail` — NOT-READY.
///
/// `fail` — это в том числе «вердикт не распознан»: нераспознанное не есть
/// READY, поэтому блокирует (находки берутся из `detail` гейта).
fn review_of(gate_verdict: &str) -> Option<ReviewVerdict> {
    match gate_verdict {
        "ok" => Some(ReviewVerdict::Ready),
        "fail" => Some(ReviewVerdict::NotReady),
        _ => None,
    }
}

/// Читает журналы флота и возвращает последний вердикт ревьювера по ветке
/// `arch/<name>`.
///
/// # Errors
/// Журнал не читается (I/O). Битая или неизвестная строка журнала — не
/// ошибка: строка пропускается толерантным разбором и попадает в счётчик
/// [`ReviewScan::skipped_lines`].
pub fn scan_branch(state_dir: &Path, name: &str) -> Result<ReviewScan> {
    let branch = format!("{}{name}", crate::worktree::BRANCH_PREFIX);
    let dir = state_dir.join(FLEET_SUBDIR);
    let journals = journal_paths(&dir)?;
    let mut skipped_lines = 0usize;
    let mut last: Option<BranchVerdict> = None;
    for path in &journals {
        let read = FleetLog::read_tolerant(path)?;
        skipped_lines += read.skipped.len();
        // Признак связи: какие ветки влиты в дерево каждого узла прогона.
        // Источник — структурное поле события, а не текст `detail`.
        let mut merged: HashMap<(&str, &str), Vec<String>> = HashMap::new();
        for event in &read.events {
            if let FleetEvent::NodeGated {
                run_id,
                node_id,
                gate,
                branch,
                ..
            } = event
            {
                if gate.starts_with("dependency:") {
                    if let Some(branch) = branch {
                        merged
                            .entry((run_id.as_str(), node_id.as_str()))
                            .or_default()
                            .push(branch.clone());
                    }
                }
            }
        }
        for event in &read.events {
            let FleetEvent::NodeGated {
                run_id,
                node_id,
                gate,
                verdict,
                detail,
                ..
            } = event
            else {
                continue;
            };
            if gate != "review" {
                continue;
            }
            let Some(found) = review_of(verdict) else {
                continue;
            };
            let own = node_id == name;
            let dependency = merged
                .get(&(run_id.as_str(), node_id.as_str()))
                .is_some_and(|branches| branches.iter().any(|b| b == &branch));
            let link = if own {
                format!("узел '{node_id}': имя worktree узла совпадает с принимаемым")
            } else if dependency {
                format!("ветка влита в дерево узла '{node_id}' как зависимость (прогон {run_id})")
            } else {
                continue;
            };
            let findings = if found == ReviewVerdict::NotReady {
                findings_for(&dir, run_id, node_id, detail)
            } else {
                Vec::new()
            };
            last = Some(BranchVerdict {
                run_id: run_id.clone(),
                node_id: node_id.clone(),
                verdict: found,
                findings,
                link,
            });
        }
    }
    Ok(ReviewScan {
        journals: journals.len(),
        skipped_lines,
        last,
    })
}

/// Находки ревьювера из лога узла (`<fleet_dir>/<run_id>-<node_id>.log`).
///
/// Лог не найден или JSON-блок не разобран — в находки идёт `detail` гейта
/// (хотя бы причина отказа, а не пустой список).
fn findings_for(fleet_dir: &Path, run_id: &str, node_id: &str, detail: &str) -> Vec<String> {
    let log = fleet_dir.join(format!("{run_id}-{node_id}.log"));
    let text = std::fs::read_to_string(&log).unwrap_or_default();
    let parsed = findings_from_text(&text);
    if parsed.is_empty() {
        vec![detail.to_string()]
    } else {
        parsed
    }
}

/// Извлекает находки из последнего JSON-объекта с полем `findings` в тексте.
///
/// Установка ревьювера (`review_pair`) требует завершить вывод блоком
/// `{"verdict":"READY"|"NOT-READY","findings":[…]}` — он и ищется: от
/// последнего вхождения `"findings"` назад к открывающей скобке объекта.
fn findings_from_text(text: &str) -> Vec<String> {
    let Some(pos) = text.rfind("\"findings\"") else {
        return Vec::new();
    };
    let Some(start) = text[..pos].rfind('{') else {
        return Vec::new();
    };
    let Some(end) = json_object_end(text, start) else {
        return Vec::new();
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&text[start..=end]) else {
        return Vec::new();
    };
    let Some(items) = value.get("findings").and_then(serde_json::Value::as_array) else {
        return Vec::new();
    };
    items.iter().map(finding_text).collect()
}

/// Текст одной находки: строка — как есть, объект — по «говорящему» полю,
/// иначе компактный JSON (ничего не теряем).
fn finding_text(item: &serde_json::Value) -> String {
    if let Some(s) = item.as_str() {
        return s.to_string();
    }
    for key in ["finding", "message", "detail", "evidence", "description"] {
        if let Some(s) = item.get(key).and_then(serde_json::Value::as_str) {
            return s.to_string();
        }
    }
    item.to_string()
}

/// Индекс закрывающей скобки JSON-объекта, начинающегося в `start`.
///
/// Строки и экранирование учитываются; незакрытый объект — `None`.
fn json_object_end(text: &str, start: usize) -> Option<usize> {
    let mut depth = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    for (offset, ch) in text[start..].char_indices() {
        if in_string {
            if escaped {
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                in_string = false;
            }
            continue;
        }
        match ch {
            '"' => in_string = true,
            '{' => depth += 1,
            '}' => {
                depth = depth.checked_sub(1)?;
                if depth == 0 {
                    return Some(start + offset);
                }
            }
            _ => {}
        }
    }
    None
}

/// Метка вердикта для вывода (`READY` / `NOT-READY`).
const fn verdict_label(verdict: ReviewVerdict) -> &'static str {
    match verdict {
        ReviewVerdict::Ready => "READY",
        ReviewVerdict::NotReady => "NOT-READY",
    }
}

/// Текст отказа гейта ревью: причина, связь с веткой, находки и подсказка
/// об именном аппрувере.
#[must_use]
pub fn refusal_message(name: &str, found: &BranchVerdict) -> String {
    let branch = format!("{}{name}", crate::worktree::BRANCH_PREFIX);
    let mut out = String::new();
    let _ = writeln!(
        out,
        "worktree '{name}': ревью ветки {branch} — {} (узел {}, прогон {}) — merge ОТМЕНЁН: \
         интеграцию блокирует вердикт ревьювера",
        verdict_label(found.verdict),
        found.node_id,
        found.run_id
    );
    let _ = writeln!(out, "связь вердикта с веткой: {}", found.link);
    if found.findings.is_empty() {
        let _ = writeln!(
            out,
            "находки ревью: разобрать не удалось (лог узла {} не найден)",
            found.node_id
        );
    } else {
        let _ = writeln!(out, "находки ревью:");
        for (n, finding) in found.findings.iter().enumerate() {
            let _ = writeln!(out, "  {}) {finding}", n + 1);
        }
    }
    let _ = writeln!(
        out,
        "обход — только явным именным аппрувером (решение пишется в журнал приёмки):"
    );
    let _ = write!(out, "  arch-ml worktree accept {name} --approver \"<имя>\"");
    out
}

/// Пометка о вердикте ревью для успешного (`merge`) вывода accept.
#[must_use]
pub fn decision_note(name: &str, scan: &ReviewScan, approver: Option<&str>) -> String {
    let branch = format!("{}{name}", crate::worktree::BRANCH_PREFIX);
    match (&scan.last, approver) {
        (Some(found), Some(approver)) => format!(
            "ревью: {} (узел {}, прогон {}) — решение именного аппрувера '{approver}'",
            verdict_label(found.verdict),
            found.node_id,
            found.run_id
        ),
        (Some(found), None) => format!(
            "ревью: {} (узел {}, прогон {})",
            verdict_label(found.verdict),
            found.node_id,
            found.run_id
        ),
        (None, approver) => {
            let mut note = format!(
                "⚑ вердиктов ревью не найдено — не проверено (ветка {branch}, журналов флота \
                 прочитано {})",
                scan.journals
            );
            if let Some(approver) = approver {
                let _ = write!(note, "; решение именного аппрувера '{approver}'");
            }
            note
        }
    }
}

/// Решение приёмки — строка append-only журнала `<state_dir>/accept/decisions.jsonl`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AcceptDecision {
    /// Имя аппрувера (обход возможен только именем человека).
    pub approver: String,
    /// Ветка, к которой относится решение.
    pub branch: String,
    /// Вердикт ревью на момент решения: `ready | not-ready | unverified`.
    pub verdict: String,
    /// Находки ревью (пусто — находок нет или вердиктов не было).
    #[serde(default)]
    pub findings: Vec<String>,
    /// Время решения (RFC 3339, UTC).
    pub at: String,
}

/// Решение приёмки по итогам скана журналов флота.
#[must_use]
pub fn accept_decision(name: &str, scan: &ReviewScan, approver: &str) -> AcceptDecision {
    let found = scan.last.as_ref();
    AcceptDecision {
        approver: approver.to_string(),
        branch: format!("{}{name}", crate::worktree::BRANCH_PREFIX),
        verdict: found.map_or_else(
            || VERDICT_UNVERIFIED.to_string(),
            |f| f.verdict.as_str().to_string(),
        ),
        findings: found.map_or_else(Vec::new, |f| f.findings.clone()),
        at: chrono::Utc::now().to_rfc3339(),
    }
}

/// Дописывает решение в append-only журнал приёмки; возвращает путь файла.
///
/// Строка пишется одним `write_all` (payload вместе с `\n`) — дописи разных
/// процессов не склеиваются в битую строку.
///
/// # Errors
/// Каталог журнала не создаётся; файл не открывается или не пишется.
pub fn record_decision(state_dir: &Path, decision: &AcceptDecision) -> Result<PathBuf> {
    let dir = state_dir.join(ACCEPT_SUBDIR);
    std::fs::create_dir_all(&dir).map_err(|e| HarnessError::io(&dir, e))?;
    let path = dir.join(DECISIONS_FILE);
    let mut line = serde_json::to_string(decision).map_err(HarnessError::Json)?;
    line.push('\n');
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| HarnessError::io(&path, e))?;
    file.write_all(line.as_bytes())
        .map_err(|e| HarnessError::io(&path, e))?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Строка события `node_gated` БЕЗ структурного поля `branch` — формат
    /// журнала до ADR-046 (связь по тексту `detail` не восстанавливается).
    fn gated(run_id: &str, node_id: &str, gate: &str, verdict: &str, detail: &str) -> String {
        serde_json::json!({
            "type": "node_gated",
            "run_id": run_id,
            "node_id": node_id,
            "gate": gate,
            "verdict": verdict,
            "detail": detail,
            "at": "2026-09-13T00:00:00Z",
        })
        .to_string()
    }

    /// Строка события `node_gated` со структурным полем `branch` (ADR-046).
    fn gated_branch(
        run_id: &str,
        node_id: &str,
        gate: &str,
        verdict: &str,
        detail: &str,
        branch: &str,
    ) -> String {
        serde_json::json!({
            "type": "node_gated",
            "run_id": run_id,
            "node_id": node_id,
            "gate": gate,
            "verdict": verdict,
            "detail": detail,
            "branch": branch,
            "at": "2026-09-13T00:00:00Z",
        })
        .to_string()
    }

    /// Пишет журнал флота-фикстуру в `state_dir/fleet/<run_id>.jsonl`.
    fn write_journal(state_dir: &Path, run_id: &str, lines: &[String]) {
        let dir = state_dir.join(FLEET_SUBDIR);
        std::fs::create_dir_all(&dir).expect("каталог журналов");
        let mut text = lines.join("\n");
        text.push('\n');
        std::fs::write(dir.join(format!("{run_id}.jsonl")), text).expect("журнал записан");
    }

    #[test]
    fn branch_field_links_verdict_ignoring_detail_text() {
        // Связь держит структурное поле `branch`: даже если текст события
        // вообще не называет ветку, вердикт по ней находится (ADR-046, п. 2).
        let tmp = tempfile::tempdir().expect("tmp");
        let state = tmp.path();
        write_journal(
            state,
            "fpl-struct",
            &[
                gated_branch(
                    "fpl-struct",
                    "review-impl",
                    "dependency:impl",
                    "ok",
                    "результат зависимости влит", // без имени ветки в тексте
                    "arch/impl-wt",
                ),
                gated(
                    "fpl-struct",
                    "review-impl",
                    "review",
                    "fail",
                    "вердикт not-ready: находки блокируют интеграцию",
                ),
            ],
        );
        let scan = scan_branch(state, "impl-wt").expect("скан");
        let found = scan.last.expect("вердикт найден по структурному полю");
        assert_eq!(found.verdict, ReviewVerdict::NotReady);
        assert_eq!(found.node_id, "review-impl");
    }

    #[test]
    fn legacy_journal_without_branch_field_yields_no_verdict() {
        // Журнал старого формата: ветку называл только текст `detail`, поля
        // `branch` нет. Вердиктов по ветке не найдено — честное «не проверено»,
        // а не ложный READY/NOT-READY (текст как источник истины не читается).
        let tmp = tempfile::tempdir().expect("tmp");
        let state = tmp.path();
        write_journal(
            state,
            "fpl-legacy",
            &[
                gated(
                    "fpl-legacy",
                    "review-impl",
                    "dependency:impl",
                    "ok",
                    "ветка arch/impl-wt влита в дерево узла",
                ),
                gated(
                    "fpl-legacy",
                    "review-impl",
                    "review",
                    "fail",
                    "вердикт not-ready: находки блокируют интеграцию",
                ),
            ],
        );
        let scan = scan_branch(state, "impl-wt").expect("скан");
        assert!(scan.last.is_none(), "старый журнал — связи нет");
        assert_eq!(
            scan.skipped_lines, 0,
            "старые строки разбираются толерантно"
        );
    }

    #[test]
    fn findings_parsed_from_single_line_and_pretty_json() {
        let single = "шум\n{\"verdict\":\"NOT-READY\",\"findings\":[\"src/pay.rs:42 — нет проверки подписи\"]}\n";
        assert_eq!(
            findings_from_text(single),
            vec!["src/pay.rs:42 — нет проверки подписи".to_string()]
        );
        let pretty = "итог:\n{\n  \"verdict\": \"NOT-READY\",\n  \"findings\": [\n    {\"severity\":\"high\",\"finding\":\"гонка в кэше\"},\n    \"второе\"\n  ]\n}\n";
        assert_eq!(
            findings_from_text(pretty),
            vec!["гонка в кэше".to_string(), "второе".to_string()]
        );
        assert!(findings_from_text("без блока находок").is_empty());
        assert!(findings_from_text("{\"findings\": [не json}").is_empty());
    }

    #[test]
    fn scan_takes_last_verdict_and_requires_branch_link() {
        let tmp = tempfile::tempdir().expect("tmp");
        let state = tmp.path();
        // Ветка arch/impl-wt влита в дерево ревьювера; затем READY и NOT-READY.
        write_journal(
            state,
            "fpl-1",
            &[
                gated_branch(
                    "fpl-1",
                    "review-impl",
                    "dependency:impl",
                    "ok",
                    "ветка arch/impl-wt влита в дерево узла",
                    "arch/impl-wt",
                ),
                gated("fpl-1", "review-impl", "review", "ok", "вердикт ready"),
                gated(
                    "fpl-1",
                    "review-other",
                    "review",
                    "fail",
                    "вердикт not-ready: находки блокируют интеграцию",
                ),
            ],
        );
        // Вердикт по чужой ветке (arch/other-wt) к arch/impl-wt не относится.
        let scan = scan_branch(state, "impl-wt").expect("скан");
        assert_eq!(scan.journals, 1);
        let found = scan.last.expect("вердикт по ветке найден");
        assert_eq!(found.verdict, ReviewVerdict::Ready);
        assert!(found.link.contains("review-impl"), "{}", found.link);

        // Второй прогон закрывает READY вердиктом NOT-READY — он и берётся.
        write_journal(
            state,
            "fpl-2",
            &[
                gated_branch(
                    "fpl-2",
                    "review-impl",
                    "dependency:impl",
                    "ok",
                    "ветка arch/impl-wt влита в дерево узла",
                    "arch/impl-wt",
                ),
                gated(
                    "fpl-2",
                    "review-impl",
                    "review",
                    "fail",
                    "вердикт not-ready: находки блокируют интеграцию",
                ),
            ],
        );
        let scan = scan_branch(state, "impl-wt").expect("скан");
        let found = scan.last.expect("вердикт");
        assert_eq!(found.verdict, ReviewVerdict::NotReady);
        assert_eq!(found.run_id, "fpl-2");
        // Лога узла нет — находкой становится detail гейта (не пусто).
        assert_eq!(found.findings.len(), 1);
        assert!(
            found.findings[0].contains("not-ready"),
            "{:?}",
            found.findings
        );

        // Ветка без вердиктов — не найдено.
        assert!(scan_branch(state, "ghost").expect("скан").last.is_none());
        // Журналов нет вовсе — тоже не найдено, но не ошибка.
        let empty = tempfile::tempdir().expect("tmp");
        let scan = scan_branch(empty.path(), "impl-wt").expect("скан");
        assert_eq!(scan.journals, 0);
        assert!(scan.last.is_none());
    }

    #[test]
    fn scan_reports_node_own_branch_and_skips_broken_lines() {
        let tmp = tempfile::tempdir().expect("tmp");
        let state = tmp.path();
        let mut lines = vec![
            "не json".to_string(),
            gated("fpl-3", "impl-wt", "review", "fail", "вердикт not-ready"),
        ];
        lines.insert(
            0,
            serde_json::json!({
                "type": "node_gated",
                "run_id": "fpl-3",
                "node_id": "impl-wt",
                "gate": "future_gate",
                "verdict": "ok",
                "detail": "",
                "at": "2026-09-13T00:00:00Z",
            })
            .to_string(),
        );
        write_journal(state, "fpl-3", &lines);
        let scan = scan_branch(state, "impl-wt").expect("скан");
        assert_eq!(scan.skipped_lines, 1, "битая строка пропущена");
        let found = scan.last.expect("вердикт");
        assert_eq!(found.verdict, ReviewVerdict::NotReady);
        assert!(found.link.contains("worktree"), "{}", found.link);
    }

    #[test]
    fn refusal_and_notes_name_findings_and_approver_hint() {
        let found = BranchVerdict {
            run_id: "fpl-9".into(),
            node_id: "review-impl".into(),
            verdict: ReviewVerdict::NotReady,
            findings: vec!["src/x.rs:1 — пропущен краевой случай".into()],
            link: "ветка влита в дерево узла 'review-impl'".into(),
        };
        let text = refusal_message("impl-wt", &found);
        assert!(text.contains("NOT-READY"), "{text}");
        assert!(text.contains("src/x.rs:1"), "{text}");
        assert!(text.contains("--approver"), "{text}");
        assert!(text.contains("arch/impl-wt"), "{text}");

        let scan = ReviewScan {
            journals: 2,
            skipped_lines: 0,
            last: None,
        };
        let note = decision_note("impl-wt", &scan, None);
        assert!(
            note.contains("не найдено") && note.contains("не проверено"),
            "{note}"
        );
        assert_eq!(
            accept_decision("impl-wt", &scan, "roman").verdict,
            VERDICT_UNVERIFIED
        );
        let ready = ReviewScan {
            journals: 1,
            skipped_lines: 0,
            last: Some(BranchVerdict {
                verdict: ReviewVerdict::Ready,
                findings: Vec::new(),
                ..found.clone()
            }),
        };
        assert!(decision_note("impl-wt", &ready, None).contains("READY"));
        assert!(
            decision_note("impl-wt", &ready, Some("roman")).contains("roman"),
            "решение аппрувера названо"
        );
    }

    #[test]
    fn decision_journal_is_append_only() {
        let tmp = tempfile::tempdir().expect("tmp");
        let scan = ReviewScan {
            journals: 1,
            skipped_lines: 0,
            last: Some(BranchVerdict {
                run_id: "fpl-1".into(),
                node_id: "review-impl".into(),
                verdict: ReviewVerdict::NotReady,
                findings: vec!["находка".into()],
                link: "связь".into(),
            }),
        };
        let first = accept_decision("impl-wt", &scan, "roman");
        let second = accept_decision("impl-wt", &scan, "anna");
        let path = record_decision(tmp.path(), &first).expect("запись");
        record_decision(tmp.path(), &second).expect("дозапись");
        assert_eq!(path, tmp.path().join("accept/decisions.jsonl"));
        let text = std::fs::read_to_string(&path).expect("чтение");
        let rows: Vec<AcceptDecision> = text
            .lines()
            .map(|l| serde_json::from_str(l).expect("строка журнала"))
            .collect();
        assert_eq!(rows.len(), 2, "журнал append-only: обе записи на месте");
        assert_eq!(rows[0].approver, "roman");
        assert_eq!(rows[1].approver, "anna");
        assert_eq!(rows[0].verdict, "not-ready");
        assert_eq!(rows[0].branch, "arch/impl-wt");
        assert_eq!(rows[0].findings, vec!["находка".to_string()]);
        assert!(!rows[0].at.is_empty(), "время решения записано");
    }
}
