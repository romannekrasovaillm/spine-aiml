//! Риск-адаптивная автономность (R-уровни R0–R5, по AI-Disrupt PDLC):
//! автономия калибруется риском действия (обратимость, blast radius), а не
//! брендом модели. Политика применяется к каждому вызову инструмента в
//! [`crate::tool::ToolRegistry::dispatch`].
//!
//! - R0 — всё через человека (auto только чтения);
//! - R1 — + чтения и поиск авто;
//! - R2 — + изменения в рабочем каталоге авто (дефолт харнесса);
//! - R3 — + изменения авто с обязательным журналом (у нас журнал всегда);
//! - R4 — деструктивные действия только с подтверждением человека;
//! - R5 — полная автономия (не рекомендуется; красный флаг аудита).

use serde::{Deserialize, Serialize};

/// Класс риска действия.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub enum RiskClass {
    /// Чтение/поиск — обратимо тривиально (R0+).
    ReadOnly,
    /// Изменение в рабочем каталоге — обратимо (R2+).
    Mutating,
    /// Деструктивное/необратимое, выход за контур (R4 confirm / R5 auto).
    Destructive,
}

/// Решение политики.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PolicyDecision {
    /// Разрешено автоматически.
    Allow,
    /// Требуется подтверждение человека (headless → отказ с объяснением).
    RequireConfirm(String),
    /// Запрещено на текущем уровне автономии.
    Deny(String),
}

/// Политика автономии.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Policy {
    /// Уровень автономии 0–5.
    pub level: u8,
}

impl Default for Policy {
    fn default() -> Self {
        Self { level: 2 }
    }
}

impl Policy {
    /// Парсит уровень из конфига (`"R2"`, `"r3"`, `"4"`).
    ///
    /// # Errors
    /// Неизвестный уровень.
    pub fn parse(s: &str) -> Result<Self, crate::error::HarnessError> {
        let digits: String = s.chars().filter(char::is_ascii_digit).collect();
        let level: u8 = digits.parse().map_err(|_| {
            crate::error::HarnessError::Config(format!(
                "некорректный уровень автономии '{s}' (R0–R5)"
            ))
        })?;
        if level > 5 {
            return Err(crate::error::HarnessError::Config(format!(
                "уровень автономии R{level} недопустим (R0–R5)"
            )));
        }
        Ok(Self { level })
    }

    /// Решение по инструменту и его аргументам.
    #[must_use]
    pub fn check(&self, tool: &str, args: &serde_json::Value) -> PolicyDecision {
        let class = classify_tool(tool, args);
        match class {
            RiskClass::ReadOnly => PolicyDecision::Allow,
            RiskClass::Mutating => {
                if self.level >= 2 {
                    PolicyDecision::Allow
                } else {
                    PolicyDecision::RequireConfirm(format!(
                        "{tool}: изменяющее действие требует R2+, текущий уровень R{}",
                        self.level
                    ))
                }
            }
            RiskClass::Destructive => {
                if self.level >= 5 {
                    PolicyDecision::Allow
                } else if self.level == 4 {
                    PolicyDecision::RequireConfirm(format!(
                        "{tool}: деструктивное действие требует подтверждения человека (R4)"
                    ))
                } else {
                    PolicyDecision::Deny(format!(
                        "{tool}: деструктивное действие запрещено на уровне R{} (нужен R4+)",
                        self.level
                    ))
                }
            }
        }
    }
}

/// Классификация инструмента по риску; bash — по тексту команды.
#[must_use]
pub fn classify_tool(tool: &str, args: &serde_json::Value) -> RiskClass {
    match tool {
        "bash" => {
            let cmd = args.get("command").and_then(|c| c.as_str()).unwrap_or("");
            classify_bash(cmd)
        }
        // Изменяющие, но обратимые: правки файлов/артефактов и навигация
        // браузера (ADR-041; запуск браузера и переход по адресу обратимы,
        // а наблюдение за страницей — browser_page/browser_screenshot —
        // остаётся чтением и падает в `_ =>` ниже).
        "write_file" | "edit_file" | "adr_new" | "handoff_create" | "harness_run"
        | "agentsmd_generate" | "browser_open" | "browser_navigate" => RiskClass::Mutating,
        // Ввод (мышь, клавиатура, фокус окна) и исполнение кода на странице —
        // необратимые внешние действия: клик может отправить заказ или
        // публикацию, browser_eval исполняет произвольный JS. Деструктивный
        // класс: R5 — авто, R4 — подтверждение человека, ниже — запрет.
        "computer_move" | "computer_click" | "computer_scroll" | "computer_drag"
        | "computer_key" | "computer_type" | "window_focus" | "browser_click" | "browser_type"
        | "browser_eval" => RiskClass::Destructive,
        _ => RiskClass::ReadOnly,
    }
}

/// Бинари управления рабочим столом: их вызов через `bash` — тоже воздействие.
///
/// Без этого списка гейт был бы декоративным: `xdotool`/`ydotool`/`wtype`/
/// `wmctrl` не попадают ни в [`DESTRUCTIVE_PATTERNS`], ни в
/// [`MUTATING_PATTERNS`], поэтому `bash "xdotool click 500 300"` классифицировался
/// бы как чтение и разрешался уже с R0 — в обход инструментов `computer_*`.
/// `wmctrl` здесь же: управление окнами из bash обходит `window_focus`.
const DESTRUCTIVE_BINARIES: &[&str] = &["xdotool", "ydotool", "wtype", "wmctrl"];

/// Деструктивные паттерны команд (необратимые/внешние эффекты).
const DESTRUCTIVE_PATTERNS: &[&str] = &[
    "rm -rf",
    "rm -fr",
    "rm -r /",
    "mkfs",
    "dd if=",
    "dd of=",
    ":(){",
    "shutdown",
    "reboot",
    "kill -9",
    "pkill",
    "chmod -R /",
    "chown -R /",
    "> /dev/",
    "git push --force",
    "git push -f",
    "git reset --hard",
    "drop table",
    "DROP TABLE",
    "truncate table",
    "kubectl delete",
    "docker system prune",
    "terraform destroy",
    "ansible",
];

/// Изменяющие паттерны (обратимые, в рабочем контуре).
const MUTATING_PATTERNS: &[&str] = &[
    "rm ",
    "mv ",
    "cp ",
    "mkdir",
    "touch ",
    "sed -i",
    "sed -i.bak",
    "tee ",
    "> ",
    ">> ",
    "git add",
    "git commit",
    "git push",
    "git checkout",
    "git switch",
    "git merge",
    "git rebase",
    "cargo build",
    "cargo test",
    "cargo clippy",
    "cargo fmt",
    "npm install",
    "npm run",
    "pnpm ",
    "pip install",
    "mvn ",
    "gradle",
    "make ",
    "docker build",
    "kubectl apply",
    "curl -X POST",
    "curl -X PUT",
    "curl -X DELETE",
    "curl -d ",
    "wget -O",
];

/// Классификация bash-команды по тексту.
#[must_use]
pub fn classify_bash(command: &str) -> RiskClass {
    let cmd = command.trim();
    // Воздействие на рабочий стол проверяется ПЕРВЫМ: оно деструктивно
    // независимо от остальных частей команды.
    if invokes_desktop_binary(cmd) {
        return RiskClass::Destructive;
    }
    for pat in DESTRUCTIVE_PATTERNS {
        if cmd.contains(pat) {
            return RiskClass::Destructive;
        }
    }
    for pat in MUTATING_PATTERNS {
        if cmd.contains(pat) {
            return RiskClass::Mutating;
        }
    }
    RiskClass::ReadOnly
}

/// Вызывает ли команда бинарь управления рабочим столом.
///
/// Сравнение идёт по ГРАНИЦАМ ТОКЕНОВ, а не подстрокой: наивный `contains`
/// ловил бы `xdotool` внутри пути (`~/notes/xdotool.md`), а имя с дефисом
/// (`xdotool-wrapper`) не должно считаться вызовом. Путь отсекается по
/// последнему `/`, поэтому `/usr/bin/xdotool` распознаётся.
#[must_use]
fn invokes_desktop_binary(command: &str) -> bool {
    command
        .split(|c: char| {
            c.is_whitespace()
                || matches!(
                    c,
                    ';' | '|' | '&' | '(' | ')' | '`' | '$' | '>' | '<' | '=' | '"' | '\''
                )
        })
        .any(|token| DESTRUCTIVE_BINARIES.contains(&token.rsplit('/').next().unwrap_or(token)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readonly_commands_are_always_allowed() {
        let p = Policy::default();
        for cmd in [
            "ls -la",
            "cat spec.md",
            "grep -r foo src/",
            "git status",
            "pwd",
        ] {
            assert_eq!(classify_bash(cmd), RiskClass::ReadOnly, "{cmd}");
            assert_eq!(
                p.check("bash", &serde_json::json!({"command": cmd})),
                PolicyDecision::Allow
            );
        }
    }

    #[test]
    fn mutating_requires_r2() {
        let cmd = serde_json::json!({"command": "cargo test"});
        assert!(matches!(
            Policy::parse("R1").expect("R1").check("bash", &cmd),
            PolicyDecision::RequireConfirm(_)
        ));
        assert_eq!(
            Policy::parse("R2").expect("R2").check("bash", &cmd),
            PolicyDecision::Allow
        );
        assert!(matches!(
            Policy::parse("R0")
                .expect("R0")
                .check("write_file", &serde_json::json!({})),
            PolicyDecision::RequireConfirm(_)
        ));
    }

    #[test]
    fn destructive_denied_below_r4_and_confirmed_at_r4() {
        let cmd = serde_json::json!({"command": "rm -rf /tmp/x"});
        assert!(matches!(
            Policy::default().check("bash", &cmd),
            PolicyDecision::Deny(_)
        ));
        assert!(matches!(
            Policy::parse("R4").unwrap().check("bash", &cmd),
            PolicyDecision::RequireConfirm(_)
        ));
        assert_eq!(
            Policy::parse("R5").unwrap().check("bash", &cmd),
            PolicyDecision::Allow
        );
        assert!(matches!(
            Policy::default().check("bash", &serde_json::json!({"command": "git push --force"})),
            PolicyDecision::Deny(_)
        ));
    }

    #[test]
    fn parse_validates_levels() {
        assert_eq!(Policy::parse("r3").unwrap().level, 3);
        assert!(Policy::parse("R9").is_err());
        assert!(Policy::parse("auto").is_err());
    }

    #[test]
    fn computer_input_tools_are_destructive() {
        // ADR-041: клик и ввод необратимы и видны за пределами харнесса.
        for tool in [
            "computer_move",
            "computer_click",
            "computer_scroll",
            "computer_drag",
            "computer_key",
            "computer_type",
            "window_focus",
            "browser_click",
            "browser_type",
            "browser_eval",
        ] {
            assert_eq!(
                classify_tool(tool, &serde_json::json!({})),
                RiskClass::Destructive,
                "{tool}"
            );
        }
    }

    #[test]
    fn browser_navigation_is_mutating_observation_is_readonly() {
        for tool in ["browser_open", "browser_navigate"] {
            assert_eq!(
                classify_tool(tool, &serde_json::json!({})),
                RiskClass::Mutating,
                "{tool}"
            );
        }
        for tool in [
            "screenshot",
            "read_image",
            "screen_size",
            "window_list",
            "browser_page",
            "browser_screenshot",
        ] {
            assert_eq!(
                classify_tool(tool, &serde_json::json!({})),
                RiskClass::ReadOnly,
                "{tool}: наблюдение не должно требовать подтверждения"
            );
        }
    }

    #[test]
    fn computer_click_requires_r4_at_default_and_is_denied_below() {
        let args = serde_json::json!({"x": 500, "y": 500});
        assert!(
            matches!(
                Policy::default().check("computer_click", &args),
                PolicyDecision::Deny(_)
            ),
            "дефолтный R2 обязан отклонять клик"
        );
        assert!(matches!(
            Policy::parse("R3").unwrap().check("computer_click", &args),
            PolicyDecision::Deny(_)
        ));
        assert!(matches!(
            Policy::parse("R4").unwrap().check("computer_click", &args),
            PolicyDecision::RequireConfirm(_)
        ));
        assert_eq!(
            Policy::parse("R5").unwrap().check("computer_click", &args),
            PolicyDecision::Allow
        );
    }

    #[test]
    fn bash_desktop_binaries_are_destructive() {
        // Дыра, которую закрывает список: без него это было ReadOnly→Allow с R0.
        for cmd in [
            "xdotool click 500 300",
            "xdotool type hello",
            "sudo xdotool mousemove 10 20",
            "/usr/bin/xdotool click 1",
            "wdctl && wmctrl -l",
            "ydotool click 0xC0",
            "wtype привет",
        ] {
            assert_eq!(classify_bash(cmd), RiskClass::Destructive, "{cmd}");
        }
        let args = serde_json::json!({"command": "xdotool click 500 300"});
        assert!(matches!(
            Policy::default().check("bash", &args),
            PolicyDecision::Deny(_)
        ));
        assert!(matches!(
            Policy::parse("R4").unwrap().check("bash", &args),
            PolicyDecision::RequireConfirm(_)
        ));
    }

    #[test]
    fn desktop_binary_match_respects_token_boundaries() {
        // Не должно быть ложных срабатываний на путях и словах с дефисом.
        for cmd in [
            "cat ~/notes/xdotool.md",
            "ls docs/xdotool-notes/",
            "grep -r xdotool-wrapper src/",
            "echo wxdotoolx",
        ] {
            assert_eq!(
                classify_bash(cmd),
                RiskClass::ReadOnly,
                "{cmd}: подстрока не является вызовом бинаря"
            );
        }
    }
}
