//! Диагностика окружения харнесса (по опыту Claude Code `/doctor` и Theseus
//! `doctor.rs`): проверки ДО того, как архитектор начнёт сессию, — ключи,
//! каталоги, плагины, кодовые харнессы в PATH, MCP, крон. Платные
//! chat-эндпоинты не вызываются (дорого и шумно); проверяется только то,
//! что видно локально.
//!
//! КОНТРАКТ (владелец: агент `agent`):
//! - [`run_checks`] — чистое ядро: список [`Check`] с вердиктами;
//! - [`render`] — текстовый отчёт (иконки ✓/⚠/✗); [`exit_code`] — 1 при
//!   хотя бы одном Fail (для CLI `arch-ml doctor`);
//! - проверки не мутируют состояние, кроме временного файла в `sessions_dir`
//!   (создаётся и тут же удаляется).

use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use crate::config::Config;

/// Вердикт одной проверки.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    /// Всё в порядке.
    Ok,
    /// Работать можно, но стоит обратить внимание.
    Warn,
    /// Критично: часть функций недоступна.
    Fail,
}

impl Verdict {
    /// Иконка вердикта для отчёта.
    #[must_use]
    pub const fn icon(self) -> &'static str {
        match self {
            Self::Ok => "✓",
            Self::Warn => "⚠",
            Self::Fail => "✗",
        }
    }
}

/// Результат одной проверки окружения.
#[derive(Debug, Clone)]
pub struct Check {
    /// Короткое имя проверки (для колонки отчёта).
    pub name: &'static str,
    /// Вердикт.
    pub verdict: Verdict,
    /// Пояснение (что проверено / что не так / как чинить).
    pub text: String,
}

/// Все проверки окружения по конфигу. Чистая функция — без вывода.
#[must_use]
pub fn run_checks(cfg: &Config) -> Vec<Check> {
    vec![
        check_default_model(cfg),
        check_api_keys(cfg),
        check_sessions_dir(cfg),
        check_plugins(cfg),
        check_knowledge(cfg),
        check_harnesses(cfg),
        check_gpus(cfg),
        check_mcp(cfg),
        check_cron(cfg),
        check_web(cfg),
        check_archify(cfg),
        check_computer(cfg),
        check_git(),
    ]
}

/// Контур управления компьютером и браузером (ADR-041).
///
/// Выключенный контур — не проблема (Ok). Включённый проверяется на то, без
/// чего он не работает: X11-дисплей и `xdotool` для ввода, Node и браузер для
/// CDP-подконтура. Вердикт всегда `Warn`, никогда `Fail`: ненастроенная
/// интеграция не должна блокировать остальное окружение (как у archify).
fn check_computer(cfg: &Config) -> Check {
    if !cfg.computer.enabled {
        return Check {
            name: "computer",
            verdict: Verdict::Ok,
            text: "выключен конфигом ([computer].enabled = false)".into(),
        };
    }
    let mut problems: Vec<String> = Vec::new();
    let display = std::env::var("DISPLAY").is_ok_and(|d| !d.trim().is_empty());
    if !display {
        problems.push("DISPLAY не задан (нужна X11-сессия)".into());
    }
    if !binary_in_path("xdotool") {
        problems.push("нет xdotool (sudo apt install xdotool)".into());
    }
    if cfg.computer.browser.enabled {
        if !binary_in_path(cfg.computer.browser.node_bin.trim()) {
            problems.push(format!(
                "нет Node.js '{}' — CDP-хелпер не запустится",
                cfg.computer.browser.node_bin
            ));
        }
        if let Err(e) = crate::computer::browser::browser_bin(cfg) {
            problems.push(e.to_string());
        }
    }
    if problems.is_empty() {
        return Check {
            name: "computer",
            verdict: Verdict::Ok,
            text: if cfg.computer.browser.enabled {
                format!(
                    "включён: xdotool + браузерный контур (CDP-порт {})",
                    cfg.computer.browser.debug_port
                )
            } else {
                "включён: xdotool (браузерный подконтур выключен)".into()
            },
        };
    }
    Check {
        name: "computer",
        verdict: Verdict::Warn,
        text: format!("включён, но не заработает: {}", problems.join("; ")),
    }
}

/// Текстовый отчёт по списку проверок.
#[must_use]
pub fn render(checks: &[Check]) -> String {
    let mut out = String::from("arch-ml doctor — диагностика окружения\n\n");
    for c in checks {
        let _ = writeln!(out, "  {} {:<14} {}", c.verdict.icon(), c.name, c.text);
    }
    let fails = checks.iter().filter(|c| c.verdict == Verdict::Fail).count();
    let warns = checks.iter().filter(|c| c.verdict == Verdict::Warn).count();
    let _ = writeln!(out);
    if fails == 0 && warns == 0 {
        let _ = writeln!(out, "Итог: здоров ({} проверок)", checks.len());
    } else {
        let _ = writeln!(
            out,
            "Итог: {fails} проблем(ы), {warns} предупреждений из {} проверок",
            checks.len()
        );
    }
    out
}

/// Код выхода CLI: 1 при любом Fail, иначе 0.
#[must_use]
pub fn exit_code(checks: &[Check]) -> i32 {
    i32::from(checks.iter().any(|c| c.verdict == Verdict::Fail))
}

/// `default_model` присутствует в реестре моделей.
fn check_default_model(cfg: &Config) -> Check {
    let ok = cfg.models.contains_key(&cfg.default_model);
    Check {
        name: "default_model",
        verdict: if ok { Verdict::Ok } else { Verdict::Fail },
        text: if ok {
            format!(
                "«{}» → {}",
                cfg.default_model, cfg.models[&cfg.default_model].model
            )
        } else {
            format!(
                "«{}» не найдена в [models] (есть: {})",
                cfg.default_model,
                cfg.models.keys().cloned().collect::<Vec<_>>().join(", ")
            )
        },
    }
}

/// API-ключи: env-переменные моделей установлены (значения не выводим!).
/// Ключ считается доступным и по `api_key_file` (запасной файл с ключом).
fn check_api_keys(cfg: &Config) -> Check {
    let key_available = |mc: &crate::config::ModelConfig| {
        if std::env::var_os(&mc.api_key_env).is_some_and(|v| !v.is_empty()) {
            return true;
        }
        mc.api_key_file.as_deref().is_some_and(|path| {
            let expanded = match path.strip_prefix("~/") {
                Some(rest) => {
                    dirs::home_dir().map_or_else(|| PathBuf::from(path), |h| h.join(rest))
                }
                None => PathBuf::from(path),
            };
            std::fs::metadata(&expanded).is_ok_and(|m| m.len() > 0)
        })
    };
    let mut missing = Vec::new();
    for (name, mc) in &cfg.models {
        if !key_available(mc) {
            missing.push(format!("{name} ({})", mc.api_key_env));
        }
    }
    let default_missing = cfg
        .models
        .get(&cfg.default_model)
        .is_some_and(|mc| !key_available(mc));
    let (verdict, text) = if missing.is_empty() {
        (
            Verdict::Ok,
            format!("все {} ключей на месте", cfg.models.len()),
        )
    } else if default_missing {
        (
            Verdict::Fail,
            format!(
                "нет ключа модели по умолчанию; отсутствуют: {}",
                missing.join(", ")
            ),
        )
    } else {
        (
            Verdict::Warn,
            format!(
                "нет части ключей (нужны только при выборе модели): {}",
                missing.join(", ")
            ),
        )
    };
    Check {
        name: "api-keys",
        verdict,
        text,
    }
}

/// Каталог сессий: создаётся и доступен на запись.
fn check_sessions_dir(cfg: &Config) -> Check {
    let dir = &cfg.paths.sessions_dir;
    let probe = dir.join(".doctor-probe");
    let result = std::fs::create_dir_all(dir)
        .and_then(|()| std::fs::write(&probe, b"ok"))
        .and_then(|()| std::fs::remove_file(&probe));
    Check {
        name: "sessions",
        verdict: if result.is_ok() {
            Verdict::Ok
        } else {
            Verdict::Fail
        },
        text: match result {
            Ok(()) => format!("{} — запись возможна", dir.display()),
            Err(e) => format!("{} — нет записи: {e}", dir.display()),
        },
    }
}

/// Плагины: каталоги существуют, считаем плагины и скиллы.
fn check_plugins(cfg: &Config) -> Check {
    let mut plugins = 0usize;
    let mut skills = 0usize;
    let mut missing = Vec::new();
    for dir in &cfg.plugins.dirs {
        if !dir.is_dir() {
            missing.push(dir.display().to_string());
            continue;
        }
        if let Ok(rd) = std::fs::read_dir(dir) {
            for entry in rd.flatten() {
                let p = entry.path();
                if p.join("plugin.json").is_file() {
                    plugins += 1;
                }
                skills += count_files_named(&p, "SKILL.md", 3);
            }
        }
    }
    let verdict = if plugins == 0 {
        Verdict::Fail
    } else if missing.is_empty() {
        Verdict::Ok
    } else {
        Verdict::Warn
    };
    let mut text = format!("{plugins} плагинов, {skills} скиллов");
    if !missing.is_empty() {
        let _ = write!(text, "; нет каталогов: {}", missing.join(", "));
    }
    Check {
        name: "plugins",
        verdict,
        text,
    }
}

/// Счёт файлов с заданным именем в поддереве (глубина ограничена).
fn count_files_named(dir: &Path, name: &str, depth: usize) -> usize {
    if depth == 0 {
        return 0;
    }
    let mut n = usize::from(dir.join(name).is_file());
    if let Ok(rd) = std::fs::read_dir(dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.is_dir() {
                n += count_files_named(&p, name, depth - 1);
            }
        }
    }
    n
}

/// База знаний: сколько каталогов существует.
fn check_knowledge(cfg: &Config) -> Check {
    let existing: Vec<_> = cfg.knowledge.dirs.iter().filter(|d| d.is_dir()).collect();
    let verdict = if existing.is_empty() {
        Verdict::Warn
    } else {
        Verdict::Ok
    };
    Check {
        name: "knowledge",
        verdict,
        text: format!(
            "{} из {} каталогов доступны",
            existing.len(),
            cfg.knowledge.dirs.len()
        ),
    }
}

/// Кодовые харнессы: бинарь в PATH.
fn check_harnesses(cfg: &Config) -> Check {
    let mut found = Vec::new();
    let mut missing = Vec::new();
    for (name, hc) in &cfg.harnesses {
        if hc.binary.is_empty() {
            continue;
        }
        if binary_in_path(&hc.binary) {
            found.push(name.clone());
        } else {
            missing.push(format!("{name} ({})", hc.binary));
        }
    }
    let verdict = if missing.is_empty() {
        Verdict::Ok
    } else {
        Verdict::Warn
    };
    Check {
        name: "harnesses",
        verdict,
        text: format!(
            "в PATH: {} ({}); отсутствуют: {}",
            found.len(),
            found.join(", "),
            if missing.is_empty() {
                "—".into()
            } else {
                missing.join(", ")
            }
        ),
    }
}

/// Локальные GPU: объявлены в `[gpus.local]` и `nvidia-smi` в PATH (детекция).
fn check_gpus(cfg: &Config) -> Check {
    let declared = cfg.gpus.local.len();
    let nvidia = binary_in_path("nvidia-smi");
    let (verdict, text) = if declared == 0 {
        (
            Verdict::Warn,
            "локальные GPU не объявлены ([gpus.local] пуст — только облако)".into(),
        )
    } else if nvidia {
        (
            Verdict::Ok,
            format!("{declared} объявлено, nvidia-smi в PATH — детекция доступна"),
        )
    } else {
        (
            Verdict::Warn,
            format!("{declared} объявлено, но nvidia-smi не в PATH — детекция недоступна"),
        )
    };
    Check {
        name: "gpus",
        verdict,
        text,
    }
}

/// MCP: файл серверов существует и парсится; бинари команд в PATH.
fn check_mcp(cfg: &Config) -> Check {
    let file = &cfg.mcp.servers_file;
    if !file.is_file() {
        return Check {
            name: "mcp",
            verdict: Verdict::Warn,
            text: format!("{} не найден (MCP опциональны)", file.display()),
        };
    }
    let parsed = std::fs::read_to_string(file)
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok());
    let Some(v) = parsed else {
        return Check {
            name: "mcp",
            verdict: Verdict::Fail,
            text: format!("{} — не JSON", file.display()),
        };
    };
    let servers = v
        .get("mcpServers")
        .and_then(|s| s.as_object())
        .map_or(0, serde_json::Map::len);
    let mut missing_cmds = Vec::new();
    if let Some(map) = v.get("mcpServers").and_then(|s| s.as_object()) {
        for (name, spec) in map {
            let cmd = spec.get("command").and_then(|c| c.as_str()).unwrap_or("");
            if !cmd.is_empty() && !binary_in_path(cmd) {
                missing_cmds.push(format!("{name} ({cmd})"));
            }
        }
    }
    let verdict = if missing_cmds.is_empty() {
        Verdict::Ok
    } else {
        Verdict::Warn
    };
    let mut text = format!("{servers} серверов в {}", file.display());
    if !missing_cmds.is_empty() {
        let _ = write!(text, "; нет бинарей: {}", missing_cmds.join(", "));
    }
    Check {
        name: "mcp",
        verdict,
        text,
    }
}

/// Крон: файл расписания существует и не пуст.
fn check_cron(cfg: &Config) -> Check {
    let file = &cfg.cron.file;
    let ok = file.is_file() && std::fs::metadata(file).is_ok_and(|m| m.len() > 0);
    Check {
        name: "cron",
        verdict: if ok { Verdict::Ok } else { Verdict::Warn },
        text: if ok {
            format!("{} на месте", file.display())
        } else {
            format!("{} отсутствует или пуст (крон опционален)", file.display())
        },
    }
}

/// Веб: кураторский список архитектурных сайтов наполнен.
fn check_web(cfg: &Config) -> Check {
    let n = cfg.web.arch_sites.len();
    Check {
        name: "web",
        verdict: if n == 0 { Verdict::Warn } else { Verdict::Ok },
        text: format!("{n} кураторских сайтов архитектурных знаний"),
    }
}

/// Archify-контур диаграмм: node в PATH + настроенный `[archify].cli_path`.
/// Ненастроенная интеграция — Warn (не блокирует остальное окружение).
fn check_archify(cfg: &Config) -> Check {
    if !cfg.archify.enabled {
        return Check {
            name: "archify",
            verdict: Verdict::Ok,
            text: "выключен конфигом ([archify].enabled = false)".into(),
        };
    }
    let node_ok = binary_in_path(&cfg.archify.node_bin);
    let cli = &cfg.archify.cli_path;
    let cli_ok = !cli.as_os_str().is_empty() && cli.is_file();
    let (verdict, text) = match (node_ok, cli_ok) {
        (true, true) => {
            let base = format!(
                "node '{}' ({}) + CLI {}",
                cfg.archify.node_bin,
                node_version(&cfg.archify.node_bin).unwrap_or_else(|| "версия?".into()),
                cli.display()
            );
            match crate::archify::check_cli_version(cli) {
                crate::archify::CliVersionVerdict::Pinned(v) => {
                    (Verdict::Ok, format!("{base} (версия {v} — проверенная)"))
                }
                crate::archify::CliVersionVerdict::Mismatch(v) => (
                    Verdict::Warn,
                    format!(
                        "{base} — версия {v} отличается от проверенной {} (контракт \
                         schemaVersion: 1): движок ставить из вендоренного tarball'а \
                         релиза, а не из сети",
                        crate::archify::PINNED_CLI_VERSION
                    ),
                ),
                crate::archify::CliVersionVerdict::Unknown => (
                    Verdict::Warn,
                    format!(
                        "{base} — версия CLI не определена (нет skill-release.json/\
                         package.json); проверенная — {}",
                        crate::archify::PINNED_CLI_VERSION
                    ),
                ),
            }
        }
        (false, _) => (
            Verdict::Warn,
            format!(
                "node '{}' не найден в PATH — Archify-инструменты недоступны",
                cfg.archify.node_bin
            ),
        ),
        (true, false) => (
            Verdict::Warn,
            "не настроен [archify].cli_path (путь к bin/archify.mjs; движок — \
             вендоренный tarball archify-vendored-*.tar.gz из релиза дистрибутива, \
             распаковать в ~/.arch-ml/)"
                .into(),
        ),
    };
    Check {
        name: "archify",
        verdict,
        text,
    }
}

/// Версия node для строки диагностики (`node --version`, первая строка).
/// `None` при любой ошибке запуска — диагностика деградирует к «версия?».
fn node_version(node_bin: &str) -> Option<String> {
    let out = std::process::Command::new(node_bin)
        .arg("--version")
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines().next().map(|l| l.trim().to_string())
}

/// git в PATH (нужен agentsmd, контролю репозиториев).
fn check_git() -> Check {
    let ok = binary_in_path("git");
    Check {
        name: "git",
        verdict: if ok { Verdict::Ok } else { Verdict::Warn },
        text: if ok {
            "в PATH".into()
        } else {
            "не найден в PATH".into()
        },
    }
}

/// Бинарь доступен в PATH (через `which`, без запуска самого бинаря).
/// Есть ли бинарь в `PATH` (общий помощник: doctor + контур `computer`).
pub(crate) fn binary_in_path(name: &str) -> bool {
    std::process::Command::new("which")
        .arg(name)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .is_ok_and(|s| s.success())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Конфиг с минимальным окружением в tempdir.
    fn test_config(dir: &Path) -> Config {
        let mut cfg = Config::default();
        cfg.paths.sessions_dir = dir.join("sessions");
        cfg.knowledge.dirs = vec![dir.join("kb")];
        cfg.plugins.dirs = vec![dir.join("plugins")];
        cfg.cron.file = dir.join("cron.toml");
        cfg.mcp.servers_file = dir.join("mcp.json");
        cfg
    }

    #[test]
    fn healthy_minimal_environment() {
        let tmp = tempfile::tempdir().expect("tmp");
        let cfg = test_config(tmp.path());
        // Плагин-заглушка.
        let p = tmp.path().join("plugins/demo");
        std::fs::create_dir_all(p.join("skills/s1")).expect("mkdir");
        std::fs::write(p.join("plugin.json"), "{}").expect("write");
        std::fs::write(p.join("skills/s1/SKILL.md"), "---\nname: s1\n---").expect("write");
        std::fs::create_dir_all(tmp.path().join("kb")).expect("mkdir kb");
        std::fs::write(tmp.path().join("cron.toml"), "[tasks]").expect("write cron");

        let checks = run_checks(&cfg);
        let by = |name: &str| checks.iter().find(|c| c.name == name).expect("check");
        assert_eq!(by("sessions").verdict, Verdict::Ok);
        assert_eq!(by("plugins").verdict, Verdict::Ok, "1 плагин, 1 скилл");
        assert!(by("plugins").text.contains("1 плагинов"));
        assert!(by("plugins").text.contains("1 скиллов"));
        assert_eq!(by("knowledge").verdict, Verdict::Ok);
        assert_eq!(by("cron").verdict, Verdict::Ok);
        assert_eq!(
            by("mcp").verdict,
            Verdict::Warn,
            "mcp.json не создан — warn"
        );
        assert!(render(&checks).contains("arch-ml doctor"));
    }

    #[test]
    fn missing_default_model_and_plugins_fail() {
        let tmp = tempfile::tempdir().expect("tmp");
        let mut cfg = test_config(tmp.path());
        cfg.default_model = "нет-такой".into();
        cfg.plugins.dirs = vec![tmp.path().join("пусто")];
        let checks = run_checks(&cfg);
        let by = |name: &str| checks.iter().find(|c| c.name == name).expect("check");
        assert_eq!(by("default_model").verdict, Verdict::Fail);
        assert_eq!(by("plugins").verdict, Verdict::Fail);
        assert_eq!(exit_code(&checks), 1);
        assert!(render(&checks).contains("проблем"));
    }

    #[test]
    fn mcp_invalid_json_fails() {
        let tmp = tempfile::tempdir().expect("tmp");
        let cfg = test_config(tmp.path());
        std::fs::write(tmp.path().join("mcp.json"), "не json").expect("write");
        let checks = run_checks(&cfg);
        let mcp = checks.iter().find(|c| c.name == "mcp").expect("mcp");
        assert_eq!(mcp.verdict, Verdict::Fail);
    }
}
