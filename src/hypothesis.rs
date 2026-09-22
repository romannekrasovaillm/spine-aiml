//! Доменные хуки плагинов: запуск внешних событийных программ, объявленных
//! в `plugin.json` → `hooks`.
//!
//! Механизм [`crate::hooks`] привязывает shell-команды к событиям
//! ИНСТРУМЕНТОВ (`PreToolUse`/`PostToolUse`/…) и читает их из
//! `hooks/hooks.json`. Здесь — второй, независимый канал: плагин объявляет
//! доменные события (`intent`, `pre_handoff`, `post_accept`,
//! `library_changed`) прямо в манифесте как команду с аргументами:
//!
//! ```json
//! {"hooks": {"pre_handoff": "hooks/hypothesis-hook.sh pre-handoff"}}
//! ```
//!
//! Ядро не знает семантики событий и НЕ зависит от плагина (AD-1): первый
//! токен команды резолвится от каталога плагина, остальные — фиксированный
//! `prefix`, к которому [`run_event`] добавляет позиционные аргументы вызова.
//! Хук — процесс без LLM (AD-2); его код выхода (0/2/3) и первая строка
//! stdout уезжают в [`DomainHookOutcome`] и решают вызывающие слои.
//!
//! Хук не должен рушить харнесс: ненулевой код не блокирует вызывающего,
//! таймаут/сбой запуска дают `code = -1` с причиной в [`DomainHookOutcome::line`].

use std::io::Read as _;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::plugin::PluginManifest;

/// Таймаут исполнения доменного хука, секунды.
const HOOK_TIMEOUT: Duration = Duration::from_secs(5);
/// Шаг опроса процесса при ожидании с таймаутом.
const POLL_INTERVAL: Duration = Duration::from_millis(5);

/// Разрешённый доменный хук плагина.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DomainHook {
    /// Имя плагина-владельца (из манифеста).
    pub plugin: String,
    /// Событие (ключ карты `hooks` в `plugin.json`).
    pub event: String,
    /// Абсолютный путь к исполняемому файлу (первый токен команды).
    pub program: PathBuf,
    /// Фиксированные аргументы команды (токены после первого).
    pub prefix: Vec<String>,
}

/// Итог исполнения доменного хука.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DomainHookOutcome {
    /// Плагин-владелец.
    pub plugin: String,
    /// Событие.
    pub event: String,
    /// Код выхода; `-1` — таймаут или сбой запуска.
    pub code: i32,
    /// Первая строка stdout; при таймауте/сбое — причина.
    pub line: String,
}

/// Собирает доменные хуки плагинов: `<plugin_dir>/<plugin>/plugin.json` →
/// `hooks`. Для каждого события первый токен команды резолвится от каталога
/// плагина (относительный путь), остальные становятся [`DomainHook::prefix`].
/// Если файл не существует или не является файлом — хук пропускается (не
/// ошибка). Направление обхода детерминировано: каталоги и имена плагинов —
/// по возрастанию пути, события — по алфавиту (`BTreeMap`).
#[must_use]
pub fn domain_hooks(plugin_dirs: &[PathBuf]) -> Vec<DomainHook> {
    let mut out = Vec::new();
    for dir in plugin_dirs {
        let Ok(rd) = std::fs::read_dir(dir) else {
            continue;
        };
        let mut entries: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_dir())
            .collect();
        entries.sort();
        for entry in entries {
            let Ok(text) = std::fs::read_to_string(entry.join("plugin.json")) else {
                continue;
            };
            let Ok(manifest) = serde_json::from_str::<PluginManifest>(&text) else {
                continue;
            };
            let plugin = plugin_name(&manifest, &entry);
            for (event, command) in &manifest.hooks {
                if let Some(hook) = resolve_hook(&plugin, &entry, event, command) {
                    out.push(hook);
                }
            }
        }
    }
    out
}

/// Имя плагина: из манифеста, иначе — имя каталога.
fn plugin_name(manifest: &PluginManifest, dir: &Path) -> String {
    if manifest.name.is_empty() {
        dir.file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    } else {
        manifest.name.clone()
    }
}

/// Разбирает одну команду `hooks`: первый токен — путь к программе от каталога
/// плагина, остальные — prefix. `None`, если файла нет.
fn resolve_hook(plugin: &str, dir: &Path, event: &str, command: &str) -> Option<DomainHook> {
    let mut tokens = command.split_whitespace();
    let program_token = tokens.next()?;
    let program = dir.join(program_token);
    if !program.is_file() {
        return None;
    }
    Some(DomainHook {
        plugin: plugin.to_string(),
        event: event.to_string(),
        program,
        prefix: tokens.map(str::to_string).collect(),
    })
}

/// Запускает один хук: `program + prefix + args`, рабочий каталог `cwd`,
/// ДОПОЛНИТЕЛЬНОЕ окружение `env` (родительское наследуется). stdout
/// усекается до первой строки; ненулевой код и таймаут не возвращают ошибку —
/// код едет в outcome (`-1` при таймауте/сбое запуска, причина в `line`).
#[must_use]
pub fn run_hook(
    hook: &DomainHook,
    args: &[&str],
    cwd: &Path,
    env: &[(String, String)],
) -> DomainHookOutcome {
    let mut cmd = Command::new(&hook.program);
    cmd.args(&hook.prefix)
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    for (key, value) in env {
        cmd.env(key, value);
    }
    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(e) => return outcome(hook, -1, format!("не запустился: {e}")),
    };
    // stdout читает отдельный поток — иначе хук, переполнивший pipe-буфер,
    // заблокируется до нашего `try_wait` и будет ложно сочтён зависшим.
    let reader = child.stdout.take().map(|mut pipe| {
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = pipe.read_to_end(&mut buf);
            buf
        })
    });
    let deadline = Instant::now() + HOOK_TIMEOUT;
    let exited = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    break None;
                }
                std::thread::sleep(POLL_INTERVAL);
            }
            Err(e) => {
                let _ = child.kill();
                let _ = child.wait();
                return outcome(hook, -1, format!("сбой ожидания: {e}"));
            }
        }
    };
    if let Some(status) = exited {
        // Нормальное завершение: читающий поток дождётся EOF (пайп держит
        // только сам процесс) и отдаст stdout.
        let stdout = reader
            .and_then(|handle| handle.join().ok())
            .unwrap_or_default();
        outcome(hook, status.code().unwrap_or(-1), first_line(&stdout))
    } else {
        // Процесс убит: потомок-внук мог унаследовать пайп и держать его
        // открытым, поэтому читающий поток НЕ дожидаемся — иначе таймаут
        // превратился бы в ожидание внука. На таймауте stdout не нужен:
        // `line` — причина.
        drop(reader);
        outcome(hook, -1, format!("таймаут {} с", HOOK_TIMEOUT.as_secs()))
    }
}

/// Запускает все хуки события: собирает [`domain_hooks`], фильтрует по
/// `event`, исполняет с окружением процесса харнесса (`std::env::vars`).
/// Нет объявленных хуков — пустой вектор.
#[must_use]
pub fn run_event(
    plugin_dirs: &[PathBuf],
    event: &str,
    args: &[&str],
    cwd: &Path,
) -> Vec<DomainHookOutcome> {
    let env: Vec<(String, String)> = std::env::vars().collect();
    domain_hooks(plugin_dirs)
        .iter()
        .filter(|hook| hook.event == event)
        .map(|hook| run_hook(hook, args, cwd, &env))
        .collect()
}

/// Первая строка stdout без завершающего `\r`.
fn first_line(buf: &[u8]) -> String {
    String::from_utf8_lossy(buf)
        .lines()
        .next()
        .unwrap_or("")
        .trim_end_matches('\r')
        .to_string()
}

/// Собирает outcome с сохранением идентичности хука.
fn outcome(hook: &DomainHook, code: i32, line: String) -> DomainHookOutcome {
    DomainHookOutcome {
        plugin: hook.plugin.clone(),
        event: hook.event.clone(),
        code,
        line,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Пишет исполняемый скрипт (создавая родителей).
    fn write_script(path: &Path, body: &str) {
        use std::os::unix::fs::PermissionsExt as _;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("mkdir");
        }
        std::fs::write(path, body).expect("write script");
        let mut perms = std::fs::metadata(path).expect("meta").permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(path, perms).expect("chmod");
    }

    /// Раскладывает плагин с манифестом-картой `hooks` и одним скриптом.
    fn plugin(root: &Path, name: &str, hooks: &str, script_rel: &str, body: &str) -> PathBuf {
        let dir = root.join(name);
        std::fs::create_dir_all(&dir).expect("mkdir plugin");
        let manifest = format!(r#"{{"name":"{name}","hooks":{hooks}}}"#);
        std::fs::write(dir.join("plugin.json"), manifest).expect("manifest");
        write_script(&dir.join(script_rel), body);
        dir
    }

    /// Хук на скрипт, выводящий переданные аргументы.
    fn echoing_hook(program: PathBuf, prefix: &[&str]) -> DomainHook {
        DomainHook {
            plugin: "test".into(),
            event: "intent".into(),
            program,
            prefix: prefix
                .iter()
                .map(std::string::ToString::to_string)
                .collect(),
        }
    }

    #[test]
    fn hook_is_taken_from_manifest() {
        let tmp = tempfile::tempdir().expect("tmp");
        let dir = plugin(
            tmp.path(),
            "hyp",
            r#"{"intent":"hooks/h.sh intent --flag"}"#,
            "hooks/h.sh",
            "#!/bin/sh\necho ok\n",
        );
        let hooks = domain_hooks(&[tmp.path().to_path_buf()]);
        assert_eq!(hooks.len(), 1, "hooks: {hooks:?}");
        assert_eq!(hooks[0].plugin, "hyp");
        assert_eq!(hooks[0].event, "intent");
        assert_eq!(hooks[0].program, dir.join("hooks/h.sh"));
        assert_eq!(hooks[0].prefix, ["intent", "--flag"]);
    }

    #[test]
    fn missing_hook_file_is_skipped_not_error() {
        let tmp = tempfile::tempdir().expect("tmp");
        plugin(
            tmp.path(),
            "hyp",
            r#"{"intent":"hooks/absent.sh intent"}"#,
            "hooks/other.sh",
            "#!/bin/sh\necho ok\n",
        );
        assert!(domain_hooks(&[tmp.path().to_path_buf()]).is_empty());
    }

    #[test]
    fn run_hook_returns_first_stdout_line_and_code() {
        let tmp = tempfile::tempdir().expect("tmp");
        let script = tmp.path().join("hook.sh");
        write_script(
            &script,
            "#!/bin/sh\nprintf 'строка1\\nстрока2\\n'\nexit 0\n",
        );
        let hook = echoing_hook(script, &["intent"]);
        let out = run_hook(&hook, &[], tmp.path(), &[]);
        assert_eq!(out.code, 0);
        assert_eq!(out.line, "строка1");
        assert_eq!(out.event, "intent");
        assert_eq!(out.plugin, "test");
    }

    #[test]
    fn nonzero_code_is_not_error() {
        let tmp = tempfile::tempdir().expect("tmp");
        let script = tmp.path().join("hook.sh");
        write_script(&script, "#!/bin/sh\necho причина\nexit 7\n");
        let out = run_hook(&echoing_hook(script, &[]), &[], tmp.path(), &[]);
        assert_eq!(out.code, 7, "ненулевой код едет в outcome, не Err");
        assert_eq!(out.line, "причина");
    }

    #[test]
    fn timeout_kills_process_and_reports_minus_one() {
        let tmp = tempfile::tempdir().expect("tmp");
        let script = tmp.path().join("hook.sh");
        write_script(&script, "#!/bin/sh\nsleep 30\n");
        let started = Instant::now();
        let out = run_hook(&echoing_hook(script, &[]), &[], tmp.path(), &[]);
        let elapsed = started.elapsed();
        assert!(
            elapsed < Duration::from_secs(15),
            "таймаут сработал, elapsed {elapsed:?}"
        );
        assert_eq!(out.code, -1);
        assert!(out.line.contains("таймаут"), "line: {}", out.line);
    }

    #[test]
    fn env_from_argument_reaches_child() {
        let tmp = tempfile::tempdir().expect("tmp");
        let script = tmp.path().join("hook.sh");
        write_script(&script, "#!/bin/sh\nprintf '%s' \"$SPINE_TEST_VAR\"\n");
        let out = run_hook(
            &echoing_hook(script, &[]),
            &[],
            tmp.path(),
            &[("SPINE_TEST_VAR".into(), "доехало".into())],
        );
        assert_eq!(out.code, 0);
        assert_eq!(out.line, "доехало", "env из параметра доезжает до ребёнка");
    }

    #[test]
    fn empty_plugin_list_gives_empty_results() {
        assert!(domain_hooks(&[]).is_empty());
        assert!(run_event(&[], "intent", &[], Path::new(".")).is_empty());
    }

    #[test]
    fn run_event_filters_event_and_passes_args() {
        let tmp = tempfile::tempdir().expect("tmp");
        plugin(
            tmp.path(),
            "hyp",
            r#"{"intent":"hooks/h.sh intent","pre_handoff":"hooks/h.sh pre-handoff"}"#,
            "hooks/h.sh",
            "#!/bin/sh\nprintf '%s' \"$*\"\n",
        );
        let dirs = [tmp.path().to_path_buf()];
        let outcomes = run_event(&dirs, "pre_handoff", &["~/repo"], Path::new("."));
        assert_eq!(outcomes.len(), 1, "outcomes: {outcomes:?}");
        assert_eq!(outcomes[0].event, "pre_handoff");
        assert_eq!(outcomes[0].code, 0);
        assert_eq!(outcomes[0].line, "pre-handoff ~/repo");
        // Событие без объявленных хуков — пусто, не ошибка.
        assert!(run_event(&dirs, "post_accept", &[], Path::new(".")).is_empty());
    }

    #[test]
    fn manifest_without_hooks_yields_no_domain_hooks() {
        let tmp = tempfile::tempdir().expect("tmp");
        let dir = tmp.path().join("plain");
        std::fs::create_dir_all(&dir).expect("mkdir");
        std::fs::write(dir.join("plugin.json"), r#"{"name":"plain"}"#).expect("manifest");
        assert!(domain_hooks(&[tmp.path().to_path_buf()]).is_empty());
    }
}
