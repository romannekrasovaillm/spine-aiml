//! Хуки жизненного цикла (по опыту Claude Code hooks и Theseus
//! `hooks_ext.rs`): shell-команды на событиях агента. Корпоративный кейс —
//! встраивание харнесса в контур заказчика: аудит обращений к инструментам,
//! блокировка коммитов вне веток, оповещения о начале/конце сессии.
//!
//! КОНТРАКТ (владелец: агент `agent`):
//! - спецификации в конфиге (`[hooks] specs = [...]`): событие, опциональный
//!   фильтр инструмента (подстрока имени), shell-команда, таймаут;
//! - хук получает контекст через env: `ARCH_HOOK_EVENT`, `ARCH_HOOK_TOOL`,
//!   `ARCH_HOOK_CONTEXT` (JSON, ≤ 8 КБ); stdout захватывается (≤ 4 КБ);
//! - exit code 2 = БЛОК: `PreToolUse` отменяет вызов инструмента,
//!   `UserPromptSubmit` отклоняет весь промпт; прочие коды — наблюдатели;
//! - stdout PostToolUse-хуков дописывается к результату инструмента
//!   (маркер `[hook]`), остальных событий — только в журнал;
//! - хук не должен рушить агента: таймаут/ошибка запуска — заметка
//!   в outcome, ход продолжается.

use std::fmt::Write as _;
use std::io::Read as _;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// Максимум байт контекста в `ARCH_HOOK_CONTEXT`.
const MAX_CONTEXT_BYTES: usize = 8 * 1024;
/// Максимум байт stdout хука, попадающих в результат/журнал.
const MAX_HOOK_OUTPUT: usize = 4 * 1024;
/// Дефолтный таймаут хука, секунды.
const DEFAULT_TIMEOUT_SECS: u64 = 5;
/// Шаг опроса процесса при ожидании с таймаутом.
const POLL_INTERVAL: Duration = Duration::from_millis(5);

/// Событие жизненного цикла, на которое вешается хук.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum HookEvent {
    /// Перед вызовом инструмента; exit 2 отменяет вызов.
    PreToolUse,
    /// После вызова инструмента; stdout дописывается к результату.
    PostToolUse,
    /// Перед компактификацией контекста.
    PreCompact,
    /// После компактификации контекста.
    PostCompact,
    /// Старт сессии агента.
    SessionStart,
    /// Завершение сессии (Drop сессии).
    SessionEnd,
    /// Пользователь отправил промпт; exit 2 отклоняет промпт целиком.
    UserPromptSubmit,
}

impl HookEvent {
    /// Все события в фиксированном порядке (валидация конфига, тесты).
    pub const ALL: [Self; 7] = [
        Self::PreToolUse,
        Self::PostToolUse,
        Self::PreCompact,
        Self::PostCompact,
        Self::SessionStart,
        Self::SessionEnd,
        Self::UserPromptSubmit,
    ];

    /// Стабильное имя события (как в конфиге).
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::PreToolUse => "PreToolUse",
            Self::PostToolUse => "PostToolUse",
            Self::PreCompact => "PreCompact",
            Self::PostCompact => "PostCompact",
            Self::SessionStart => "SessionStart",
            Self::SessionEnd => "SessionEnd",
            Self::UserPromptSubmit => "UserPromptSubmit",
        }
    }

    /// Разбор имени из конфига; `None` при неизвестном.
    #[must_use]
    pub fn from_name(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|e| e.as_str() == name)
    }

    /// Блокирующее ли событие (exit 2 имеет силу отказа).
    #[must_use]
    pub const fn is_blocking(self) -> bool {
        matches!(self, Self::PreToolUse | Self::UserPromptSubmit)
    }
}

/// Спецификация хука из конфига (`[[hooks.specs]]`).
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
#[derive(Default)]
pub struct HookSpec {
    /// Имя события (`PreToolUse`, `PostToolUse`, …).
    pub event: String,
    /// Фильтр инструмента (подстрока имени; пусто — все). Имеет смысл
    /// только для PreToolUse/PostToolUse.
    pub tool: Option<String>,
    /// Shell-команда (исполняется через `sh -c`).
    pub command: String,
    /// Таймаут исполнения, секунды (дефолт 5).
    pub timeout_secs: Option<u64>,
}

/// Итог исполнения одного хука.
#[derive(Debug, Clone)]
pub struct HookOutcome {
    /// Команда (для журнала).
    pub command: String,
    /// Код выхода (None — таймаут/не запустился).
    pub code: Option<i32>,
    /// Захваченный stdout (≤ [`MAX_HOOK_OUTPUT`] байт).
    pub stdout: String,
    /// Диагностика (таймаут, ошибка запуска).
    pub note: Option<String>,
}

/// Разбор хуков плагина (формат Claude Code `hooks/hooks.json`) в
/// спецификации [`HookSpec`]. Толерантный разбор: битый JSON/поля —
/// пустой список, библиотека не должна падать из-за одного плагина.
///
/// Отличия форматов: плагинный `matcher` — строка с `|`-альтернативами
/// (мы разворачиваем её в отдельные спеки, наш фильтр — подстрока имени
/// инструмента), `timeout` — секунды числом.
#[must_use]
pub fn specs_from_plugin_json(text: &str) -> Vec<HookSpec> {
    let Ok(v) = serde_json::from_str::<Value>(text) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    let Some(hooks) = v.get("hooks").and_then(Value::as_object) else {
        return out;
    };
    for (event, entries) in hooks {
        let Some(entries) = entries.as_array() else {
            continue;
        };
        for entry in entries {
            let matcher = entry.get("matcher").and_then(Value::as_str).unwrap_or("");
            let Some(commands) = entry.get("hooks").and_then(Value::as_array) else {
                continue;
            };
            for cmd in commands {
                let command = cmd
                    .get("command")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                if command.is_empty() {
                    continue;
                }
                let timeout_secs = cmd.get("timeout").and_then(Value::as_u64);
                let alternatives: Vec<&str> = matcher
                    .split('|')
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .collect();
                if alternatives.is_empty() {
                    out.push(HookSpec {
                        event: event.clone(),
                        tool: None,
                        command: command.to_string(),
                        timeout_secs,
                    });
                } else {
                    for alt in alternatives {
                        out.push(HookSpec {
                            event: event.clone(),
                            tool: Some(alt.to_string()),
                            command: command.to_string(),
                            timeout_secs,
                        });
                    }
                }
            }
        }
    }
    out
}

/// Собирает хуки всех плагинов из каталогов `dirs` (`<plugin>/hooks/hooks.json`).
/// Порядок: по порядку каталогов и имён плагинов (детерминированно).
#[must_use]
pub fn specs_from_plugin_dirs(dirs: &[std::path::PathBuf]) -> Vec<HookSpec> {
    let mut out = Vec::new();
    for plugin in crate::plugin::discover(dirs) {
        for path in plugin.extra_components() {
            if path.file_name().is_some_and(|n| n == "hooks.json") {
                if let Ok(text) = std::fs::read_to_string(&path) {
                    out.extend(specs_from_plugin_json(&text));
                }
            }
        }
    }
    out
}

/// Был ли хотя бы один блок (exit 2).
#[must_use]
pub fn any_blocked(outcomes: &[HookOutcome]) -> bool {
    outcomes.iter().any(|o| o.code == Some(2))
}

/// Причина блока: stdout блокирующих хуков (или пометка без stdout).
#[must_use]
pub fn block_reason(outcomes: &[HookOutcome]) -> String {
    let mut s = String::new();
    for o in outcomes.iter().filter(|o| o.code == Some(2)) {
        if !s.is_empty() {
            s.push_str("; ");
        }
        let _ = write!(s, "хук `{}`", o.command);
        if !o.stdout.trim().is_empty() {
            let _ = write!(s, ": {}", o.stdout.trim());
        }
    }
    s
}

/// Набор хуков сессии (валидированные спецификации).
#[derive(Debug, Default)]
pub struct HookSet {
    specs: Vec<(HookEvent, HookSpec)>,
}

impl HookSet {
    /// Собирает набор из спецификаций конфига. Неизвестные имена событий
    /// пропускаются с предупреждением в tracing (конфиг не должен ронять
    /// сессию).
    #[must_use]
    pub fn from_specs(specs: &[HookSpec]) -> Self {
        let mut out = Vec::new();
        for spec in specs {
            if let Some(ev) = HookEvent::from_name(&spec.event) {
                out.push((ev, spec.clone()));
            } else {
                tracing::warn!("хук с неизвестным событием «{}» пропущен", spec.event);
            }
        }
        Self { specs: out }
    }

    /// Пуст ли набор (быстрый путь без аллокаций в горячем цикле).
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.specs.is_empty()
    }

    /// Исполняет хуки события. `tool` — имя инструмента (для фильтра;
    /// None на не-инструментальных событиях), `context` — JSON-контекст.
    #[must_use]
    pub fn fire(&self, event: HookEvent, tool: Option<&str>, context: &str) -> Vec<HookOutcome> {
        self.specs
            .iter()
            .filter(|(ev, spec)| {
                *ev == event
                    && match (&spec.tool, tool) {
                        (Some(filter), Some(name)) => name.contains(filter.as_str()),
                        (Some(_), None) => false,
                        (None, _) => true,
                    }
            })
            .map(|(_, spec)| run_hook(spec, event, tool, context))
            .collect()
    }
}

/// Исполнение одного хука: `sh -c`, env-контекст, таймаут с опросом.
fn run_hook(spec: &HookSpec, event: HookEvent, tool: Option<&str>, context: &str) -> HookOutcome {
    let timeout = Duration::from_secs(spec.timeout_secs.unwrap_or(DEFAULT_TIMEOUT_SECS).max(1));
    let ctx_trunc: String = context.chars().take(MAX_CONTEXT_BYTES).collect();
    let spawned = std::process::Command::new("sh")
        .arg("-c")
        .arg(&spec.command)
        .env("ARCH_HOOK_EVENT", event.as_str())
        .env("ARCH_HOOK_TOOL", tool.unwrap_or(""))
        .env("ARCH_HOOK_CONTEXT", &ctx_trunc)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn();
    let mut child = match spawned {
        Ok(c) => c,
        Err(e) => {
            return HookOutcome {
                command: spec.command.clone(),
                code: None,
                stdout: String::new(),
                note: Some(format!("не запустился: {e}")),
            };
        }
    };
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut stdout = String::new();
                if let Some(mut pipe) = child.stdout.take() {
                    let mut buf = Vec::new();
                    let _ = pipe.read_to_end(&mut buf);
                    stdout = String::from_utf8_lossy(&buf)
                        .chars()
                        .take(MAX_HOOK_OUTPUT)
                        .collect();
                }
                return HookOutcome {
                    command: spec.command.clone(),
                    code: status.code(),
                    stdout,
                    note: None,
                };
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return HookOutcome {
                        command: spec.command.clone(),
                        code: None,
                        stdout: String::new(),
                        note: Some(format!("таймаут {}с", timeout.as_secs())),
                    };
                }
                std::thread::sleep(POLL_INTERVAL);
            }
            Err(e) => {
                return HookOutcome {
                    command: spec.command.clone(),
                    code: None,
                    stdout: String::new(),
                    note: Some(format!("сбой ожидания: {e}")),
                };
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(event: &str, tool: Option<&str>, command: &str) -> HookSpec {
        HookSpec {
            event: event.into(),
            tool: tool.map(str::to_string),
            command: command.into(),
            timeout_secs: Some(2),
        }
    }

    #[test]
    fn event_names_roundtrip() {
        for ev in HookEvent::ALL {
            assert_eq!(HookEvent::from_name(ev.as_str()), Some(ev));
        }
        assert_eq!(HookEvent::from_name("NoSuchEvent"), None);
        assert!(HookEvent::PreToolUse.is_blocking());
        assert!(HookEvent::UserPromptSubmit.is_blocking());
        assert!(!HookEvent::PostToolUse.is_blocking());
    }

    #[test]
    fn unknown_event_skipped_with_warning() {
        let set = HookSet::from_specs(&[spec("BadEvent", None, "true")]);
        assert!(set.is_empty());
    }

    #[test]
    fn exit_2_blocks_and_stdout_is_reason() {
        let set = HookSet::from_specs(&[spec(
            "PreToolUse",
            Some("bash"),
            "echo 'нельзя rm в проде'; exit 2",
        )]);
        let out = set.fire(HookEvent::PreToolUse, Some("bash"), "{}");
        assert!(any_blocked(&out));
        assert!(block_reason(&out).contains("нельзя rm в проде"));
        // Другой инструмент фильтром отсекается.
        let out = set.fire(HookEvent::PreToolUse, Some("read_file"), "{}");
        assert!(out.is_empty());
    }

    #[test]
    fn plugin_hooks_json_parses_and_splits_alternation() {
        let text = r#"{
          "hooks": {
            "PostToolUse": [{
              "matcher": "write_file|edit_file",
              "hooks": [{"type": "command", "command": "echo check", "timeout": 3}]
            }],
            "SessionStart": [{
              "matcher": "",
              "hooks": [{"type": "command", "command": "echo hi"}]
            }]
          }
        }"#;
        let specs = specs_from_plugin_json(text);
        assert_eq!(specs.len(), 3, "2 альтернативы + 1 без matcher: {specs:?}");
        assert_eq!(specs[0].tool.as_deref(), Some("write_file"));
        assert_eq!(specs[1].tool.as_deref(), Some("edit_file"));
        assert_eq!(specs[1].timeout_secs, Some(3));
        assert_eq!(
            specs[2].tool, None,
            "пустой matcher — все инструменты/события"
        );
        assert_eq!(specs[2].event, "SessionStart");
        // Битый JSON и отсутствующие поля — пусто, не паника.
        assert!(specs_from_plugin_json("{битый").is_empty());
        assert!(specs_from_plugin_json("{}").is_empty());
        assert!(
            specs_from_plugin_json(
                r#"{"hooks":{"PostToolUse":[{"matcher":"bash","hooks":[{"type":"command"}]}]}}"#
            )
            .is_empty()
        );
    }

    #[test]
    fn plugin_dirs_loader_collects_hooks_from_plugin_layout() {
        let tmp = tempfile::tempdir().expect("tmp");
        let hook_dir = tmp.path().join("plug/hooks");
        std::fs::create_dir_all(&hook_dir).expect("mkdir");
        std::fs::write(
            hook_dir.join("hooks.json"),
            r#"{"hooks":{"PreToolUse":[{"matcher":"bash","hooks":[{"type":"command","command":"exit 0"}]}]}}"#,
        )
        .expect("write");
        let specs = specs_from_plugin_dirs(&[tmp.path().to_path_buf()]);
        assert_eq!(specs.len(), 1, "specs: {specs:?}");
        assert_eq!(specs[0].event, "PreToolUse");
        assert_eq!(specs[0].tool.as_deref(), Some("bash"));
        // Собранный HookSet реально срабатывает на событии.
        let set = HookSet::from_specs(&specs);
        let outcomes = set.fire(HookEvent::PreToolUse, Some("bash"), "{}");
        assert_eq!(outcomes.len(), 1);
        assert_eq!(outcomes[0].code, Some(0));
    }

    #[test]
    fn success_hook_is_observer() {
        let set = HookSet::from_specs(&[spec("SessionStart", None, "echo привет")]);
        let out = set.fire(HookEvent::SessionStart, None, "{}");
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].code, Some(0));
        assert_eq!(out[0].stdout.trim(), "привет");
        assert!(!any_blocked(&out));
    }

    #[test]
    fn timeout_does_not_hang() {
        let set = HookSet::from_specs(&[spec("SessionStart", None, "sleep 30")]);
        let started = Instant::now();
        let out = set.fire(HookEvent::SessionStart, None, "{}");
        assert!(
            started.elapsed() < Duration::from_secs(10),
            "таймаут сработал"
        );
        assert!(out[0].code.is_none());
        assert!(out[0].note.as_deref().unwrap_or("").contains("таймаут"));
    }

    #[test]
    fn env_context_reaches_hook() {
        let set = HookSet::from_specs(&[spec(
            "PreToolUse",
            None,
            "printf \"%s|%s\" \"$ARCH_HOOK_EVENT\" \"$ARCH_HOOK_TOOL\"",
        )]);
        let out = set.fire(HookEvent::PreToolUse, Some("bash"), "{}");
        assert_eq!(out[0].stdout, "PreToolUse|bash");
    }
}
