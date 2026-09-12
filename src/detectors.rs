//! Детекторы циклов агента (по опыту Theseus `agent/detectors.rs` +
//! эвристики окна из `OpenDev`, там же — маппинг на Grok `doom_loop`):
//! модель под давлением инструментов иногда закольцовывается — повторяет
//! идентичный вызов с теми же аргументами или бесконечно читает файлы
//! без перехода к действию. Детекторы смотрят на скользящее окно вызовов
//! и подменяют результат предупреждением, а при рецидиве — отказом.
//!
//! КОНТРАКТ (владелец: агент `agent`):
//! - [`LoopDetectors::check_call`] вызывается перед КАЖДЫМ исполнением
//!   инструмента; `Some(verdict)` — вызов не исполнять, вернуть verdict
//!   как результат инструмента (контракт tool-сообщений не нарушается:
//!   каждый `tool_call_id` получает ровно один ответ);
//! - [`LoopDetectors::note_reply_text`] — после ответа модели: детект
//!   идентичного текста два хода подряд (doom-text);
//! - напоминания ограничены (`MAX_FIRES` на тип), чтобы не спамить контекст;
//! - [`LoopDetectors::reset`] — на `/clear` и смене задачи.

use std::collections::{HashSet, VecDeque};

/// Размер скользящего окна fingerprint'ов вызовов.
const FP_WINDOW: usize = 20;
/// Сколько повторов идентичного вызова в окне — срабатывание doom-loop.
const DOOM_THRESHOLD: usize = 3;
/// Сколько read-only вызовов подряд — срабатывание exploration spiral.
const SPIRAL_THRESHOLD: usize = 5;
/// Максимум срабатываний одного типа напоминания за сессию.
const MAX_FIRES: usize = 2;

/// Инструменты харнесса, не меняющие состояние (для spiral-детектора).
const READONLY_TOOLS: &[&str] = &[
    "read_file",
    "glob",
    "grep",
    "kb_search",
    "skill_search",
    "plugin_list",
    "web_search",
    "web_fetch",
    "web_arch_sites",
    "spine_lint",
    "fitness_check",
    "significance_score",
];

/// Fingerprint вызова `(tool, args)` — основа doom-loop детектора.
#[must_use]
pub fn fingerprint(name: &str, args: &serde_json::Value) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    name.hash(&mut h);
    serde_json::to_string(args).unwrap_or_default().hash(&mut h);
    h.finish()
}

/// Read-only ли инструмент (не меняет файлы/процессы/сеть-записи).
#[must_use]
pub fn is_readonly_tool(name: &str) -> bool {
    READONLY_TOOLS.contains(&name)
}

/// Статусный текст события детектора (для TUI/журнала).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DetectorNote {
    /// Короткий человекочитаемый текст («⚠ doom-loop: …»).
    pub text: String,
}

/// Совокупность детекторов цикла сессии.
#[derive(Debug, Default)]
pub struct LoopDetectors {
    /// Скользящее окно fingerprint'ов последних вызовов.
    fp_window: VecDeque<u64>,
    /// Fingerprint'ы, по которым уже выдано предупреждение (рецидив = отказ).
    doom_warned: HashSet<u64>,
    /// Счётчик read-only вызовов подряд.
    spiral_reads: usize,
    /// Текст последнего ответа модели (doom-text).
    last_reply: Option<String>,
    /// Сколько раз сработало каждое напоминание (ограничение спама).
    fires: std::collections::HashMap<&'static str, usize>,
}

impl LoopDetectors {
    /// Новый набор детекторов (пустое окно).
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Проверка вызова перед исполнением.
    ///
    /// Возвращает `(verdict, note)`:
    /// - `verdict = Some(text)` — вызов НЕ исполнять; text отдать модели
    ///   как результат инструмента (предупреждение или DENIED при рецидиве);
    /// - `note` — статусная строка для TUI/журнала (при срабатывании).
    pub fn check_call(
        &mut self,
        name: &str,
        args: &serde_json::Value,
    ) -> (Option<String>, Option<DetectorNote>) {
        let fp = fingerprint(name, args);
        if self.doom_warned.contains(&fp) {
            return (
                Some(format!(
                    "DENIED (doom-loop guard): идентичный вызов «{name}» уже пропускался \
                     после предупреждения — измените подход, не повторяйте тот же вызов."
                )),
                None,
            );
        }
        self.fp_window.push_back(fp);
        if self.fp_window.len() > FP_WINDOW {
            self.fp_window.pop_front();
        }
        let count = self.fp_window.iter().filter(|x| **x == fp).count();
        if count >= DOOM_THRESHOLD {
            self.doom_warned.insert(fp);
            return (
                Some(format!(
                    "[SYSTEM WARNING: doom loop suspected — «{name}» с теми же аргументами \
                     уже встречался {count} раз в окне {FP_WINDOW}. Вызов пропущен. \
                     Измените стратегию.]",
                )),
                Some(DetectorNote {
                    text: format!(
                        "⚠ doom-loop: «{name}» ×{count} с одинаковыми аргументами (окно {FP_WINDOW})"
                    ),
                }),
            );
        }
        (None, self.check_spiral(name))
    }

    /// Детект идентичного текста ответа два хода подряд (doom-text).
    ///
    /// Возвращает напоминание для вклейки в контекст (не более
    /// [`MAX_FIRES`] раз за сессию) и статусную строку.
    pub fn note_reply_text(&mut self, text: &str) -> (Option<String>, Option<DetectorNote>) {
        let trimmed = text.trim();
        let repeated = self
            .last_reply
            .as_deref()
            .is_some_and(|prev| !trimmed.is_empty() && prev == trimmed);
        self.last_reply = Some(trimmed.to_string());
        if !repeated {
            return (None, None);
        }
        let fires = self.fires.entry("doom_text").or_insert(0);
        if *fires >= MAX_FIRES {
            return (None, None);
        }
        *fires += 1;
        (
            Some(
                "REMINDER: идентичный текст ответа два хода подряд — похоже на цикл. \
                 Смените формулировку или подход."
                    .to_string(),
            ),
            Some(DetectorNote {
                text: "⚠ doom-text: идентичный текст модели два хода подряд".to_string(),
            }),
        )
    }

    /// Exploration spiral: много read-only вызовов подряд без действия.
    fn check_spiral(&mut self, name: &str) -> Option<DetectorNote> {
        if is_readonly_tool(name) {
            self.spiral_reads += 1;
            if self.spiral_reads == SPIRAL_THRESHOLD {
                let fires = self.fires.entry("spiral").or_insert(0);
                if *fires < MAX_FIRES {
                    *fires += 1;
                    return Some(DetectorNote {
                        text: format!(
                            "⚠ exploration spiral: {SPIRAL_THRESHOLD} read-only вызовов подряд — \
                             пора переходить к действию (анализ/план/правки)"
                        ),
                    });
                }
            }
        } else {
            self.spiral_reads = 0;
        }
        None
    }

    /// Сброс состояния (на `/clear`, смену задачи, смену модели).
    pub fn reset(&mut self) {
        self.fp_window.clear();
        self.doom_warned.clear();
        self.spiral_reads = 0;
        self.last_reply = None;
        self.fires.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn doom_loop_warns_then_denies() {
        let mut d = LoopDetectors::new();
        let args = json!({ "path": "SPEC.md" });
        // Первые два вызова проходят.
        assert!(d.check_call("read_file", &args).0.is_none());
        assert!(d.check_call("read_file", &args).0.is_none());
        // Третий — предупреждение, вызов пропущен.
        let (verdict, note) = d.check_call("read_file", &args);
        let v = verdict.expect("verdict");
        assert!(v.contains("doom loop"), "текст: {v}");
        assert!(note.expect("note").text.contains("doom-loop"));
        // Рецидив — DENIED.
        let (verdict2, _) = d.check_call("read_file", &args);
        assert!(verdict2.expect("v2").starts_with("DENIED"));
        // Другие аргументы не затронуты.
        assert!(
            d.check_call("read_file", &json!({ "path": "other.md" }))
                .0
                .is_none()
        );
    }

    #[test]
    fn spiral_fires_once_per_streak_and_budget_is_two() {
        let mut d = LoopDetectors::new();
        let mut notes = 0;
        // Первая серия из 7 чтений: срабатывание ровно на пятом.
        for i in 0..7 {
            let (_, note) = d.check_call("grep", &json!({ "q": format!("q{i}") }));
            notes += usize::from(note.is_some());
        }
        assert_eq!(notes, 1, "внутри одной серии — одно напоминание");
        // Мутирующий вызов сбрасывает серию.
        d.check_call("edit_file", &json!({ "path": "x" }));
        // Вторая серия — второе (и последнее) напоминание.
        for i in 0..6 {
            let (_, note) = d.check_call("grep", &json!({ "q": format!("s{i}") }));
            notes += usize::from(note.is_some());
        }
        assert_eq!(notes, 2, "вторая серия — второе напоминание");
        // Третья серия — бюджет исчерпан, тишина.
        d.check_call("write_file", &json!({ "path": "y" }));
        for i in 0..6 {
            let (_, note) = d.check_call("grep", &json!({ "q": format!("t{i}") }));
            assert!(note.is_none(), "бюджет напоминаний исчерпан");
        }
    }

    #[test]
    fn spiral_resets_on_mutating_tool() {
        let mut d = LoopDetectors::new();
        for i in 0..4 {
            d.check_call("read_file", &json!({ "path": format!("f{i}") }));
        }
        d.check_call("edit_file", &json!({ "path": "x" }));
        for i in 0..4 {
            let (_, note) = d.check_call("read_file", &json!({ "path": format!("g{i}") }));
            assert!(note.is_none(), "счётчик сброшен мутирующим вызовом");
        }
    }

    #[test]
    fn doom_text_detects_identical_replies() {
        let mut d = LoopDetectors::new();
        assert!(d.note_reply_text("ответ один").0.is_none());
        let (reminder, note) = d.note_reply_text("ответ один");
        assert!(reminder.expect("reminder").contains("REMINDER"));
        assert!(note.expect("note").text.contains("doom-text"));
        // Третий подряд — тоже срабатывание (второй и последний раз).
        assert!(d.note_reply_text("ответ один").0.is_some());
        // Четвёртый — лимит напоминаний исчерпан.
        assert!(d.note_reply_text("ответ один").0.is_none());
        // Другой текст — тишина.
        assert!(d.note_reply_text("другой").0.is_none());
    }

    #[test]
    fn reset_clears_everything() {
        let mut d = LoopDetectors::new();
        let args = json!({ "a": 1 });
        for _ in 0..3 {
            d.check_call("bash", &args);
        }
        d.reset();
        assert!(
            d.check_call("bash", &args).0.is_none(),
            "после reset снова чисто"
        );
    }

    #[test]
    fn fingerprint_stable_and_distinct() {
        let a = fingerprint("read_file", &json!({ "path": "x" }));
        assert_eq!(a, fingerprint("read_file", &json!({ "path": "x" })));
        assert_ne!(a, fingerprint("read_file", &json!({ "path": "y" })));
        assert_ne!(a, fingerprint("grep", &json!({ "path": "x" })));
    }

    #[test]
    fn readonly_classification() {
        assert!(is_readonly_tool("read_file"));
        assert!(is_readonly_tool("kb_search"));
        assert!(!is_readonly_tool("bash"));
        assert!(!is_readonly_tool("edit_file"));
        assert!(!is_readonly_tool("write_file"));
    }
}
