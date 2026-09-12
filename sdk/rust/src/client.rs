//! Клиент SDK: запуск `arch-ml` без shell (argv), таймаут, разбор контракта.

use crate::error::{Result, SpineBeError};
use crate::types::{ArchifyReceipt, FitnessReport, RunResult};
use std::ffi::{OsStr, OsString};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

/// Таймаут по умолчанию для вызовов `arch-ml` (LLM-прогоны могут быть долгими).
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(600);

/// Шаг опроса завершения процесса при ожидании с таймаутом.
const POLL_INTERVAL: Duration = Duration::from_millis(50);

/// Фрагмент stdout, включаемый в ошибку `ContractViolation` (байт).
const STDOUT_PREVIEW_MAX: usize = 200;

/// Клиент Spine-BE SDK v1.
///
/// Тонкая обёртка над headless CLI `arch-ml`: запускает процесс без shell,
/// читает stdout/stderr/exit code и разбирает JSON по контракту v1.
/// Сетевых вызовов в SDK нет.
#[derive(Debug, Clone)]
pub struct Client {
    /// Путь к бинарю `arch-ml` (по умолчанию — из `SPINE_BE_BIN`, иначе имя для PATH).
    pub binary: PathBuf,
    /// Клиентский таймаут по умолчанию (переопределяется per-call в [`Client::run`]).
    pub default_timeout: Duration,
    /// Рабочий каталог процесса (`None` — наследовать каталог вызывающего).
    pub cwd: Option<PathBuf>,
}

impl Default for Client {
    fn default() -> Self {
        Self::new()
    }
}

impl Client {
    /// Создаёт клиента: бинарь из env `SPINE_BE_BIN` → иначе `arch-ml` из PATH.
    pub fn new() -> Self {
        Self {
            binary: resolve_binary(std::env::var_os("SPINE_BE_BIN")),
            default_timeout: DEFAULT_TIMEOUT,
            cwd: None,
        }
    }

    /// Headless-прогон агента: `arch-ml run -q [--model M] [--max-turns N] -- PROMPT`.
    ///
    /// `timeout` переопределяет клиентский таймаут на этот вызов (по истечении
    /// процесс убивается, ошибка [`SpineBeError::Timeout`]). Промпт `-`
    /// зарезервирован CLI под чтение stdin и SDK не поддерживается.
    pub fn run(
        &self,
        prompt: &str,
        model: Option<&str>,
        timeout: Option<Duration>,
        max_turns: Option<u32>,
    ) -> Result<RunResult> {
        let args = run_argv(prompt, model, max_turns);
        let timeout = timeout.unwrap_or(self.default_timeout);
        let started = Instant::now();
        let out = self.exec(&args, timeout)?;
        if !out.status.success() {
            return Err(SpineBeError::ProcessFailed {
                code: out.status.code(),
                stderr: String::from_utf8_lossy(&out.stderr).trim().to_string(),
            });
        }
        let answer = String::from_utf8_lossy(&out.stdout).trim_end().to_string();
        Ok(RunResult {
            answer,
            duration: started.elapsed(),
        })
    }

    /// Fitness-контроль: `arch-ml control check <REPO> [--constraints P] --json`.
    ///
    /// Красный гейт (`passed=false`, exit 1) — валидный отчёт-данные,
    /// а не ошибка: метод вернёт `Ok(FitnessReport)`. Ошибка — только
    /// невалидный JSON ([`SpineBeError::ContractViolation`]) либо сбой
    /// исполнения без JSON ([`SpineBeError::ProcessFailed`]).
    pub fn control_check(
        &self,
        repo: impl AsRef<Path>,
        constraints: Option<&Path>,
    ) -> Result<FitnessReport> {
        let args = control_check_argv(repo.as_ref(), constraints);
        let out = self.exec(&args, self.default_timeout)?;
        match serde_json::from_slice::<FitnessReport>(&out.stdout) {
            Ok(report) => Ok(report),
            Err(_) if !out.status.success() => Err(SpineBeError::ProcessFailed {
                code: out.status.code(),
                stderr: String::from_utf8_lossy(&out.stderr).trim().to_string(),
            }),
            Err(err) => Err(contract_violation("control check", &out.stdout, err)),
        }
    }

    /// Валидация IR диаграммы: `arch-ml archify validate <TYPE> <IR> --json`.
    pub fn archify_validate(
        &self,
        diagram_type: &str,
        ir_path: impl AsRef<Path>,
    ) -> Result<ArchifyReceipt> {
        let args = vec![
            os("archify"),
            os("validate"),
            os(diagram_type),
            ir_path.as_ref().as_os_str().to_os_string(),
            os("--json"),
        ];
        self.exec_receipt(&args)
    }

    /// Сборка HTML: `arch-ml archify deliver <TYPE> <IR> <OUT_HTML> --json`.
    pub fn archify_deliver(
        &self,
        diagram_type: &str,
        ir_path: impl AsRef<Path>,
        output: impl AsRef<Path>,
    ) -> Result<ArchifyReceipt> {
        let args = vec![
            os("archify"),
            os("deliver"),
            os(diagram_type),
            ir_path.as_ref().as_os_str().to_os_string(),
            output.as_ref().as_os_str().to_os_string(),
            os("--json"),
        ];
        self.exec_receipt(&args)
    }

    /// Дельта двух architecture-снапшотов: `arch-ml archify compare <BASE> <HEAD> <OUT> --json`.
    pub fn archify_compare(
        &self,
        base: impl AsRef<Path>,
        head: impl AsRef<Path>,
        output: impl AsRef<Path>,
    ) -> Result<ArchifyReceipt> {
        let args = vec![
            os("archify"),
            os("compare"),
            base.as_ref().as_os_str().to_os_string(),
            head.as_ref().as_os_str().to_os_string(),
            output.as_ref().as_os_str().to_os_string(),
            os("--json"),
        ];
        self.exec_receipt(&args)
    }

    /// Общий путь команд archify: exit + разбор receipt по §3 контракта.
    ///
    /// При ненулевом exit с валидным JSON receipt всё равно возвращается
    /// (контракт: `ok=false` — данные), без JSON — `ProcessFailed`.
    fn exec_receipt(&self, args: &[OsString]) -> Result<ArchifyReceipt> {
        let out = self.exec(args, self.default_timeout)?;
        match serde_json::from_slice::<serde_json::Value>(&out.stdout) {
            Ok(value) => Ok(ArchifyReceipt::new(value)),
            Err(_) if !out.status.success() => Err(SpineBeError::ProcessFailed {
                code: out.status.code(),
                stderr: String::from_utf8_lossy(&out.stderr).trim().to_string(),
            }),
            Err(err) => Err(contract_violation(
                &args
                    .iter()
                    .map(|a| a.to_string_lossy().into_owned())
                    .collect::<Vec<_>>()
                    .join(" "),
                &out.stdout,
                err,
            )),
        }
    }

    /// Запускает процесс без shell, читает stdout/stderr, применяет клиентский таймаут.
    ///
    /// stdout/stderr читаются в отдельных потоках — иначе заполнение буфера
    /// пайпа заблокировало бы дочерний процесс до истечения таймаута.
    fn exec(&self, args: &[OsString], timeout: Duration) -> Result<ExecOutcome> {
        let mut command = Command::new(self.spawn_binary());
        command
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(cwd) = &self.cwd {
            command.current_dir(cwd);
        }
        // unix: ребёнок — лидер своей группы процессов, чтобы kill по
        // таймауту добирался до всего дерева (внуки, унаследовавшие пайпы).
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = spawn_with_retry(&mut command).map_err(|err| {
            if matches!(
                err.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::PermissionDenied
            ) {
                SpineBeError::BinaryNotFound {
                    binary: self.binary.clone(),
                    source: err,
                }
            } else {
                SpineBeError::ProcessFailed {
                    code: None,
                    stderr: format!("не удалось запустить процесс: {err}"),
                }
            }
        })?;

        let stdout_thread = spawn_reader(child.stdout.take());
        let stderr_thread = spawn_reader(child.stderr.take());

        let started = Instant::now();
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) => {
                    if started.elapsed() > timeout {
                        kill_child_tree(&mut child);
                        // Дочитываем читателей до EOF: после группового kill
                        // пайпы закрываются и потоки завершаются — иначе
                        // они бы висели на чтении, удерживая fd (утечка).
                        let _ = join_reader(stdout_thread);
                        let _ = join_reader(stderr_thread);
                        return Err(SpineBeError::Timeout { timeout });
                    }
                    std::thread::sleep(POLL_INTERVAL);
                }
                Err(err) => {
                    kill_child_tree(&mut child);
                    let _ = join_reader(stdout_thread);
                    let _ = join_reader(stderr_thread);
                    return Err(SpineBeError::ProcessFailed {
                        code: None,
                        stderr: format!("ошибка ожидания процесса: {err}"),
                    });
                }
            }
        };

        let stdout = join_reader(stdout_thread);
        let stderr = join_reader(stderr_thread);
        Ok(ExecOutcome {
            status,
            stdout,
            stderr,
        })
    }

    /// Путь к бинарю для spawn: относительный путь с разделителем каталога
    /// абсолютизируется от cwd ВЫЗЫВАЮЩЕГО процесса. Иначе при заданном
    /// [`Client::cwd`] относительный путь резолвился бы относительно рабочего
    /// каталога РЕБЁНКА (platform-specific, на unix — именно так), и
    /// `SPINE_BE_BIN=./arch-ml` молча ломался бы. Голое имя без разделителя
    /// (`arch-ml`) не трогаем — это поиск по PATH.
    fn spawn_binary(&self) -> PathBuf {
        let binary = &self.binary;
        let has_separator = binary.components().count() > 1;
        if binary.is_relative() && has_separator {
            std::path::absolute(binary).unwrap_or_else(|_| binary.clone())
        } else {
            binary.clone()
        }
    }
}

/// Итог исполнения процесса: exit status + захваченные stdout/stderr.
struct ExecOutcome {
    status: ExitStatus,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

/// os error 26 (ETXTBSY, unix): исполняемый файл временно открыт на запись.
/// На не-unix платформах значение не встречается — проверка безвредна.
const ETXTBSY: i32 = 26;

/// Сколько раз повторяем spawn при ETXTBSY.
const SPAWN_RETRY_MAX: u32 = 20;

/// Пауза между повторами spawn при ETXTBSY.
const SPAWN_RETRY_DELAY: Duration = Duration::from_millis(10);

/// Запускает процесс, переживая гонку ETXTBSY: при fork из многопоточного
/// процесса ребёнок наследует чужие временно открытые на запись файлы,
/// и ядро отклоняет execve — классический transient, лечится повтором.
fn spawn_with_retry(command: &mut Command) -> std::io::Result<Child> {
    let mut attempt = 0;
    loop {
        match command.spawn() {
            Err(err)
                if cfg!(unix)
                    && err.raw_os_error() == Some(ETXTBSY)
                    && attempt < SPAWN_RETRY_MAX =>
            {
                attempt += 1;
                std::thread::sleep(SPAWN_RETRY_DELAY);
            }
            result => return result,
        }
    }
}

/// Читает поток до EOF в отдельном потоке (ошибки чтения игнорируются:
/// частичный вывод полезнее паники, а сбой отразится в exit code).
fn spawn_reader(pipe: Option<impl Read + Send + 'static>) -> std::thread::JoinHandle<Vec<u8>> {
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        if let Some(mut pipe) = pipe {
            let _ = pipe.read_to_end(&mut buf);
        }
        buf
    })
}

/// Дожидается поток-читатель; при панике читателя возвращает пустой буфер
/// (содержимое пайпа потеряно, но это не должно ронять клиента).
fn join_reader(handle: std::thread::JoinHandle<Vec<u8>>) -> Vec<u8> {
    handle.join().unwrap_or_default()
}

/// Убивает дерево дочернего процесса и дожидается его (reap), чтобы не
/// оставить зомби. На unix ребёнок запущен лидером своей группы
/// (`process_group(0)`), поэтому бьём по ГРУППЕ: иначе внуки (например,
/// форкнутый в фоне `sleep`) переживали бы kill и удерживали открытыми
/// пайпы stdout/stderr — читатели-потоки висели бы до их завершения,
/// утекая fd.
///
/// Групповой kill делаем через builtin `kill` интерпретатора `/bin/sh`:
/// отрицательный pid там — killpg(2) по POSIX, а std не экспонирует kill(2)
/// без unsafe (запрещён конвенциями репозитория), а новые crate-зависимости
/// SDK не заводит. ВНИМАНИЕ: внешний `/usr/bin/kill` из procps-ng группы
/// НЕ убивает (молча игнорирует отрицательный pid, exit 0) — проверено
/// QA-прогоном на procps-ng 4.0.4. Прямой `child.kill()` — страховка
/// на случай отсутствия sh или уже мёртвой группы.
fn kill_child_tree(child: &mut Child) {
    #[cfg(unix)]
    {
        // pgid == pid ребёнка (process_group(0)); минус — «вся группа».
        let pgid = child.id();
        // Игнорируем ошибку запуска/статуса sh: страховочный kill ниже.
        let _ = Command::new("/bin/sh")
            .args(["-c", &format!("kill -KILL -{pgid}")])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    // Игнорируем ошибку kill: процесс мог уже завершиться.
    let _ = child.kill();
    // Игнорируем ошибку wait: reap уже мёртвого процесса безопасен.
    let _ = child.wait();
}

/// Разрешает путь к бинарю: значение `SPINE_BE_BIN` → иначе имя для поиска в PATH.
fn resolve_binary(env_value: Option<OsString>) -> PathBuf {
    match env_value {
        Some(value) if !value.is_empty() => PathBuf::from(value),
        _ => PathBuf::from("arch-ml"),
    }
}

/// argv для `run` (вынесено в чистую функцию ради юнит-тестов, §5 контракта).
fn run_argv(prompt: &str, model: Option<&str>, max_turns: Option<u32>) -> Vec<OsString> {
    let mut args = vec![os("run"), os("-q")];
    if let Some(model) = model {
        args.push(os("--model"));
        args.push(os(model));
    }
    if let Some(max_turns) = max_turns {
        args.push(os("--max-turns"));
        args.push(os(max_turns.to_string()));
    }
    // `--` защищает промпт, начинающийся с `-`, от разбора как флага.
    args.push(os("--"));
    args.push(os(prompt));
    args
}

/// argv для `control check` (чистая функция ради юнит-тестов).
fn control_check_argv(repo: &Path, constraints: Option<&Path>) -> Vec<OsString> {
    let mut args = vec![os("control"), os("check"), repo.as_os_str().to_os_string()];
    if let Some(constraints) = constraints {
        args.push(os("--constraints"));
        args.push(constraints.as_os_str().to_os_string());
    }
    args.push(os("--json"));
    args
}

/// Формирует ошибку контракта с фрагментом stdout для диагностики.
fn contract_violation(command: &str, stdout: &[u8], err: serde_json::Error) -> SpineBeError {
    let mut preview = String::from_utf8_lossy(stdout).into_owned();
    if preview.len() > STDOUT_PREVIEW_MAX {
        preview.truncate(STDOUT_PREVIEW_MAX);
        preview.push('…');
    }
    SpineBeError::ContractViolation {
        message: format!("`{command}`: stdout не JSON ({err}); фрагмент: {preview:?}"),
    }
}

fn os(value: impl AsRef<OsStr>) -> OsString {
    value.as_ref().to_os_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// Счётчик уникальных имён временных каталогов тестов.
    static TEMP_SEQ: AtomicUsize = AtomicUsize::new(0);

    fn strings(args: &[OsString]) -> Vec<String> {
        args.iter()
            .map(|a| a.to_string_lossy().into_owned())
            .collect()
    }

    #[test]
    fn resolve_binary_prefers_env() {
        assert_eq!(
            resolve_binary(Some(OsString::from("/opt/arch-ml"))),
            PathBuf::from("/opt/arch-ml")
        );
        assert_eq!(resolve_binary(None), PathBuf::from("arch-ml"));
        assert_eq!(
            resolve_binary(Some(OsString::new())),
            PathBuf::from("arch-ml")
        );
    }

    #[test]
    fn run_argv_minimal() {
        assert_eq!(
            strings(&run_argv("сделай ADR", None, None)),
            vec!["run", "-q", "--", "сделай ADR"]
        );
    }

    #[test]
    fn run_argv_full_and_dash_prompt() {
        assert_eq!(
            strings(&run_argv("-начинается-с-дефиса", Some("deepseek"), Some(7))),
            vec![
                "run",
                "-q",
                "--model",
                "deepseek",
                "--max-turns",
                "7",
                "--",
                "-начинается-с-дефиса"
            ]
        );
    }

    #[test]
    fn control_check_argv_with_and_without_constraints() {
        assert_eq!(
            strings(&control_check_argv(Path::new("repo"), None)),
            vec!["control", "check", "repo", "--json"]
        );
        assert_eq!(
            strings(&control_check_argv(
                Path::new("repo"),
                Some(Path::new("c/CONSTRAINTS.yaml"))
            )),
            vec![
                "control",
                "check",
                "repo",
                "--constraints",
                "c/CONSTRAINTS.yaml",
                "--json"
            ]
        );
    }

    /// Создаёт исполняемый скрипт-заглушку во временном каталоге, возвращает путь.
    fn fake_binary(body: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "spine-be-sdk-test-{}-{}",
            std::process::id(),
            TEMP_SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).expect("создать временный каталог теста");
        let path = dir.join("fake-arch-ml");
        std::fs::write(&path, body).expect("записать скрипт-заглушку");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
                .expect("сделать заглушку исполняемой");
        }
        path
    }

    fn client_with(binary: PathBuf, timeout: Duration) -> Client {
        Client {
            binary,
            default_timeout: timeout,
            cwd: None,
        }
    }

    #[test]
    fn run_parses_answer_on_mock() {
        let client = client_with(
            fake_binary("#!/bin/sh\nprintf 'ответ ассистента\\n'\n"),
            Duration::from_secs(5),
        );
        let result = client.run("вопрос", None, None, None).expect("run на моке");
        assert_eq!(result.answer, "ответ ассистента");
    }

    #[test]
    fn run_nonzero_exit_is_process_failed() {
        let client = client_with(
            fake_binary("#!/bin/sh\necho 'провайдер недоступен' >&2\nexit 1\n"),
            Duration::from_secs(5),
        );
        match client.run("вопрос", None, None, None) {
            Err(SpineBeError::ProcessFailed { code, stderr }) => {
                assert_eq!(code, Some(1));
                assert_eq!(stderr, "провайдер недоступен");
            }
            other => panic!("ожидался ProcessFailed, получено: {other:?}"),
        }
    }

    #[test]
    fn control_check_failed_gate_is_data_not_error() {
        // Красный гейт: exit 1 + валидный JSON — это отчёт, а не исключение (§4).
        let client = client_with(
            fake_binary(
                "#!/bin/sh\nprintf '%s' '{\"repo\":\".\",\"passed\":false,\"summary\":\"Правил: 1, нарушений: 1\",\"issues\":[{\"file\":\"src/bad.py\",\"line\":2,\"rule\":\"no_pan_in_code\",\"message\":\"PAN\",\"severity\":\"error\"}]}'\nexit 1\n",
            ),
            Duration::from_secs(5),
        );
        let report = client
            .control_check(Path::new("."), None)
            .expect("красный гейт — данные");
        assert!(!report.passed);
        assert_eq!(report.issues.len(), 1);
        assert_eq!(report.issues[0].rule, "no_pan_in_code");
        assert_eq!(report.issues[0].severity, "error");
    }

    #[test]
    fn control_check_garbage_stdout_is_contract_violation() {
        let client = client_with(
            fake_binary("#!/bin/sh\necho 'не JSON'\n"),
            Duration::from_secs(5),
        );
        match client.control_check(Path::new("."), None) {
            Err(SpineBeError::ContractViolation { message }) => {
                assert!(message.contains("не JSON"), "сообщение: {message}");
            }
            other => panic!("ожидался ContractViolation, получено: {other:?}"),
        }
    }

    #[test]
    fn control_check_garbage_and_nonzero_exit_is_process_failed() {
        let client = client_with(
            fake_binary("#!/bin/sh\necho 'нет constraints' >&2\nexit 2\n"),
            Duration::from_secs(5),
        );
        match client.control_check(Path::new("."), None) {
            Err(SpineBeError::ProcessFailed { code, stderr }) => {
                assert_eq!(code, Some(2));
                assert!(stderr.contains("нет constraints"));
            }
            other => panic!("ожидался ProcessFailed, получено: {other:?}"),
        }
    }

    #[test]
    fn timeout_kills_process() {
        let client = client_with(
            fake_binary("#!/bin/sh\nsleep 30\n"),
            Duration::from_millis(200),
        );
        let started = Instant::now();
        match client.run("вопрос", None, None, None) {
            Err(SpineBeError::Timeout { .. }) => {}
            other => panic!("ожидался Timeout, получено: {other:?}"),
        }
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "процесс должен быть убит по таймауту"
        );
    }

    #[test]
    fn missing_binary_is_binary_not_found() {
        let client = client_with(
            PathBuf::from("/definitely/not/existing/arch-ml"),
            Duration::from_secs(5),
        );
        match client.run("вопрос", None, None, None) {
            Err(SpineBeError::BinaryNotFound { binary, .. }) => {
                assert_eq!(binary, PathBuf::from("/definitely/not/existing/arch-ml"));
            }
            other => panic!("ожидался BinaryNotFound, получено: {other:?}"),
        }
    }
}
