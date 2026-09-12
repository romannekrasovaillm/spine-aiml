//! Adversarial-тесты SDK (QA-прогон): враждебные сценарии против фейк-бинарей
//! (скрипты-заглушки через `Client.binary`, аналог `SPINE_BE_BIN`) и два живых
//! теста против настоящего `arch-ml`.
//!
//! Покрытие: PIPE-DEADLOCK, TIMEOUT-KILL (включая внуков, fd-leak, зомби),
//! BINARY-NOT-FOUND, EXIT-2-NO-JSON, CONTRACT-VIOLATION, UNICODE,
//! RED-AS-DATA, PROMPT-DASH, ERROR-SURFACING, RELATIVE-BINARY-CWD,
//! стресс `spawn_with_retry` (ETXTBSY) и `Client: Send + Sync`.
//!
//! LLM-прогоны не выполняются (§5 контракта): живой PROMPT-DASH дискриминирует
//! разбор clap по коду выхода (1 — модель не настроена, 2 — ошибка clap).

use spine_be_sdk::{Client, SpineBeError};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Счётчик уникальных имён временных каталогов тестов.
static TEMP_SEQ: AtomicUsize = AtomicUsize::new(0);

/// Жёсткий дедлайн для сценариев «не должен зависнуть».
const HARD_DEADLINE: Duration = Duration::from_secs(10);

/// Уникальный временный каталог теста.
fn temp_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "spine-be-sdk-qa-{}-{}-{tag}",
        std::process::id(),
        TEMP_SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&dir).expect("создать временный каталог теста");
    dir
}

/// Создаёт исполняемый скрипт-заглушку во временном каталоге, возвращает путь.
fn fake_binary(body: &str) -> PathBuf {
    let path = temp_dir("fake").join("fake-arch-ml");
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

/// Запускает замыкание с жёстким дедлайном: при превышении — паника теста
/// (рабочий поток при этом утекает, но сьют не висит вечно).
fn with_deadline<T: Send + 'static>(
    deadline: Duration,
    f: impl FnOnce() -> T + Send + 'static,
) -> T {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(f());
    });
    rx.recv_timeout(deadline)
        .expect("дедлайн превышен: клиент завис (pipe deadlock / потерянный kill)")
}

/// Число открытых fd текущего процесса (linux: /proc/self/fd).
#[cfg(target_os = "linux")]
fn fd_count() -> usize {
    std::fs::read_dir("/proc/self/fd")
        .expect("прочитать /proc/self/fd")
        .count()
}

/// Число «наших» зависших детей: дочерние процессы текущего процесса,
/// чей cmdline несёт УНИКАЛЬНЫЙ маркер этого теста (имя каталога фейк-
/// бинаря или маркерное время sleep). Уникальный маркер обязателен:
/// тесты в бинаре идут параллельно, и транзиентные дети соседних
/// тестов (их `sleep`, пайпы `head`/`tr`) — не дефект SDK.
#[cfg(target_os = "linux")]
fn stray_child_count(marker: &str) -> usize {
    let our_pid = std::process::id().to_string();
    std::fs::read_dir("/proc")
        .expect("прочитать /proc")
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.file_name().to_string_lossy().chars().all(|c| c.is_ascii_digit()))
        .filter(|entry| {
            let is_ours = std::fs::read_to_string(entry.path().join("stat"))
                .ok()
                .and_then(|stat| {
                    // поле ppid — второе после ')' (имя может содержать пробелы)
                    stat.rsplit_once(')')
                        .and_then(|(_, rest)| rest.split_whitespace().nth(1).map(str::to_owned))
                })
                .map(|ppid| ppid == our_pid)
                .unwrap_or(false);
            if !is_ours {
                return false;
            }
            let cmd = std::fs::read(entry.path().join("cmdline")).unwrap_or_default();
            cmd.windows(marker.len()).any(|w| w == marker.as_bytes())
        })
        .count()
}

/// Есть ли в системе процесс с точным cmdline `sleep <secs>` (linux: /proc).
#[cfg(target_os = "linux")]
fn sleep_process_alive(secs: u32) -> bool {
    let wanted = format!("sleep\0{secs}\0");
    std::fs::read_dir("/proc")
        .expect("прочитать /proc")
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.file_name().to_string_lossy().chars().all(|c| c.is_ascii_digit()))
        .any(|entry| {
            std::fs::read(entry.path().join("cmdline"))
                .map(|cmd| cmd == wanted.as_bytes())
                .unwrap_or(false)
        })
}

/// Путь к живому бинарю `arch-ml`; `None` — живые тесты пропускаются.
fn find_real_binary() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("SPINE_BE_BIN") {
        let path = PathBuf::from(path);
        return path.is_file().then_some(path);
    }
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths).find_map(|dir| {
            let candidate = dir.join("arch-ml");
            candidate.is_file().then_some(candidate)
        })
    })
}

// --- 1. PIPE-DEADLOCK -------------------------------------------------------

#[test]
fn pipe_deadlock_stderr_flood_does_not_hang() {
    // Валидный JSON в stdout И ~3 МБ мусора в stderr, exit 0.
    // Без читателей-потоков ребёнок заблокировался бы на заполненном пайпе
    // stderr (64 КБ), а родитель — на ожидании exit: классический deadlock.
    let client = client_with(
        fake_binary(
            "#!/bin/sh\n\
             printf '%s' '{\"repo\":\".\",\"passed\":true,\"summary\":\"ок\",\"issues\":[]}'\n\
             head -c 3145728 /dev/zero | tr '\\0' 'x' >&2\n\
             exit 0\n",
        ),
        Duration::from_secs(30),
    );
    let report = with_deadline(HARD_DEADLINE, move || {
        client.control_check(Path::new("."), None)
    })
    .expect("JSON распарсен несмотря на флуд в stderr");
    assert!(report.passed);
}

#[test]
fn pipe_deadlock_stdout_flood_run_does_not_hang() {
    // Симметричный случай для run(): ~2 МБ ответа в stdout + мусор в stderr.
    let client = client_with(
        fake_binary(
            "#!/bin/sh\n\
             head -c 2097152 /dev/zero | tr '\\0' 'A'\n\
             head -c 1048576 /dev/zero | tr '\\0' 'e' >&2\n\
             exit 0\n",
        ),
        Duration::from_secs(30),
    );
    let result = with_deadline(HARD_DEADLINE, move || {
        client.run("вопрос", None, None, None)
    })
    .expect("run вернул ответ");
    assert_eq!(result.answer.len(), 2 * 1024 * 1024, "stdout дочитан целиком");
}

// --- 2. TIMEOUT-KILL --------------------------------------------------------

#[test]
fn timeout_kill_grandchild_returns_fast() {
    // Фейк форкает внука (sleep 107 в фоне) и ждёт его: kill обязан добраться
    // до всего дерева, иначе пайпы останутся открытыми у внука.
    let client = client_with(
        fake_binary("#!/bin/sh\nsleep 107 &\nwait\n"),
        Duration::from_secs(1),
    );
    let started = Instant::now();
    match client.run("вопрос", None, None, None) {
        Err(SpineBeError::Timeout { .. }) => {}
        other => panic!("ожидался Timeout, получено: {other:?}"),
    }
    assert!(
        started.elapsed() < HARD_DEADLINE,
        "клиентский таймаут обязан срабатывать быстро: {:?}",
        started.elapsed()
    );
}

#[cfg(target_os = "linux")]
#[test]
fn timeout_kill_leaves_no_zombies_no_grandchildren() {
    // Нечастое значение sleep — маркер, чтобы не спутать с чужими процессами
    // (тесты бинаря идут параллельно). Каталог фейк-бинаря — тоже маркер.
    let dir = temp_dir("zombie103");
    let marker = dir
        .file_name()
        .expect("имя каталога-маркера")
        .to_string_lossy()
        .into_owned();
    let fake = dir.join("fake-arch-ml");
    std::fs::write(&fake, "#!/bin/sh\nsleep 103 &\nwait\n").expect("записать фейк-бинарь");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755))
            .expect("сделать исполняемым");
    }
    let client = client_with(fake, Duration::from_millis(500));
    let _ = client.run("вопрос", None, None, None);
    // Даём реаперу/сигналам мгновение, затем проверяем.
    std::thread::sleep(Duration::from_millis(300));
    assert_eq!(
        stray_child_count(&marker),
        0,
        "остались дочерние процессы теста (зомби/неубитые)"
    );
    assert!(
        !sleep_process_alive(103),
        "внук (sleep 103) пережил kill клиента"
    );
}

#[cfg(target_os = "linux")]
#[test]
fn timeout_kill_no_fd_leak_over_50_runs() {
    let client = client_with(
        fake_binary("#!/bin/sh\nsleep 105 &\nwait\n"),
        Duration::from_millis(100),
    );
    // Прогрев: первый прогон может открыть ленивые ресурсы рантайма.
    let _ = client.run("вопрос", None, None, None);
    std::thread::sleep(Duration::from_millis(200));
    let before = fd_count();
    for _ in 0..50 {
        let _ = client.run("вопрос", None, None, None);
    }
    // Читатели-потоки завершаются асинхронно с reap'ом — даём фору.
    std::thread::sleep(Duration::from_millis(500));
    let after = fd_count();
    assert!(
        after <= before + 2,
        "утечка fd: было {before}, стало {after} после 50 таймаутов"
    );
}

// --- 3. BINARY-NOT-FOUND ----------------------------------------------------

#[test]
fn binary_not_found_nonexistent_path() {
    let client = client_with(
        PathBuf::from("/definitely/not/existing/arch-ml-qa"),
        Duration::from_secs(5),
    );
    match client.run("вопрос", None, None, None) {
        Err(SpineBeError::BinaryNotFound { binary, .. }) => {
            assert!(
                binary.display().to_string().contains("arch-ml-qa"),
                "ошибка обязана нести путь к бинарю"
            );
        }
        other => panic!("ожидался BinaryNotFound, получено: {other:?}"),
    }
}

#[test]
fn binary_not_found_non_executable_file() {
    let path = temp_dir("noexec").join("arch-ml");
    std::fs::write(&path, "это не исполняемый файл\n").expect("записать файл");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644))
            .expect("снять бит исполнения");
    }
    let client = client_with(path.clone(), Duration::from_secs(5));
    match client.run("вопрос", None, None, None) {
        Err(SpineBeError::BinaryNotFound { binary, source }) => {
            assert_eq!(binary, path, "ошибка несёт исходный путь");
            assert_eq!(
                source.kind(),
                std::io::ErrorKind::PermissionDenied,
                "причина — отсутствие права на исполнение"
            );
        }
        other => panic!("ожидался BinaryNotFound, получено: {other:?}"),
    }
}

// --- 4. EXIT-2-NO-JSON ------------------------------------------------------

#[test]
fn archify_exit_2_no_json_is_process_failed() {
    let client = client_with(
        fake_binary("#!/bin/sh\necho 'IR не читается: битый файл' >&2\nexit 2\n"),
        Duration::from_secs(5),
    );
    match client.archify_validate("architecture", "ir.json") {
        Err(SpineBeError::ProcessFailed { code, stderr }) => {
            assert_eq!(code, Some(2));
            assert!(
                stderr.contains("IR не читается"),
                "stderr сохранён целиком: {stderr}"
            );
        }
        other => panic!("ожидался ProcessFailed, получено: {other:?}"),
    }
}

// --- 5. CONTRACT-VIOLATION --------------------------------------------------

#[test]
fn archify_exit_0_broken_json_is_contract_violation() {
    let client = client_with(
        fake_binary("#!/bin/sh\nprintf '%s' '{\"schemaVersion\":1,'\nexit 0\n"),
        Duration::from_secs(5),
    );
    match client.archify_validate("architecture", "ir.json") {
        Err(SpineBeError::ContractViolation { message }) => {
            assert!(
                message.contains("archify validate"),
                "контекст команды в сообщении: {message}"
            );
        }
        other => panic!("ожидался ContractViolation, получено: {other:?}"),
    }
}

// --- 6. UNICODE -------------------------------------------------------------

#[test]
fn unicode_roundtrip_cyrillic_and_emoji() {
    let client = client_with(
        fake_binary(
            "#!/bin/sh\nprintf '%s' '{\"repo\":\"банк/платёжный-контур\",\"passed\":false,\"summary\":\"Правил: 1, нарушений: 1 🔥\",\"issues\":[{\"file\":\"src/пан.py\",\"line\":7,\"rule\":\"no_pan_in_code\",\"message\":\"найден PAN 💳 — замаскируйте\",\"severity\":\"error\"}]}'\nexit 1\n",
        ),
        Duration::from_secs(5),
    );
    let report = client
        .control_check(Path::new("."), None)
        .expect("валидный unicode-отчёт");
    assert_eq!(report.repo, "банк/платёжный-контур");
    assert_eq!(report.summary, "Правил: 1, нарушений: 1 🔥");
    assert_eq!(report.issues[0].file, "src/пан.py");
    assert_eq!(report.issues[0].message, "найден PAN 💳 — замаскируйте");
}

// --- 7. RED-AS-DATA ---------------------------------------------------------

#[test]
fn archify_ok_false_nonzero_exit_is_data_not_error() {
    // По аналогии с §2: ненулевой exit + валидный receipt с ok=false — данные.
    let client = client_with(
        fake_binary(
            "#!/bin/sh\nprintf '%s' '{\"schemaVersion\":1,\"ok\":false,\"command\":\"validate\",\"type\":\"architecture\",\"input\":\"ir.json\",\"checks\":[{\"name\":\"single_svg\",\"ok\":false,\"details\":[\"два svg\"]}]}'\necho 'проверки провалены' >&2\nexit 1\n",
        ),
        Duration::from_secs(5),
    );
    let receipt = client
        .archify_validate("architecture", "ir.json")
        .expect("ok=false с валидным JSON — данные, не исключение");
    assert!(!receipt.ok());
    assert_eq!(receipt.command(), Some("validate"));
}

// --- 8. PROMPT-DASH ---------------------------------------------------------

#[test]
fn prompt_starting_with_dash_passed_after_separator() {
    let record = temp_dir("argv").join("argv.txt");
    let client = client_with(
        fake_binary(&format!(
            "#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done > '{}'\nprintf 'ответ\\n'\n",
            record.display()
        )),
        Duration::from_secs(5),
    );
    client
        .run("-сформулируй ADR", Some("glm-5.3-flash"), None, Some(1))
        .expect("run на моке");
    let argv = std::fs::read_to_string(&record).expect("прочитать записанный argv");
    let argv: Vec<&str> = argv.lines().collect();
    assert_eq!(
        argv,
        vec![
            "run",
            "-q",
            "--model",
            "glm-5.3-flash",
            "--max-turns",
            "1",
            "--",
            "-сформулируй ADR"
        ],
        "промпт с дефисом обязан идти после `--`"
    );
}

#[test]
fn prompt_dash_real_binary_clap_accepts_separator() {
    // Живой тест БЕЗ LLM: модель заведомо не настроена. Если clap дошёл до
    // разрешения модели — exit 1 и причина в stderr; если промпт с дефисом
    // сломал clap — exit 2 (usage error). SDK всегда вставляет `--`.
    let Some(binary) = find_real_binary() else {
        eprintln!("skip: arch-ml не найден (SPINE_BE_BIN / PATH)");
        return;
    };
    let client = client_with(binary, Duration::from_secs(60));
    match client.run(
        "-промпт с дефисом",
        Some("definitely-not-a-model-qa"),
        None,
        Some(1),
    ) {
        Err(SpineBeError::ProcessFailed { code, stderr }) => {
            assert_eq!(
                code,
                Some(1),
                "ожидался exit 1 (модель не настроена), не clap-ошибка: {stderr}"
            );
            assert!(
                stderr.contains("не настроена"),
                "clap разобрал `--` + промпт, упали на модели: {stderr}"
            );
        }
        other => panic!("ожидался ProcessFailed, получено: {other:?}"),
    }
}

// --- 9. ERROR-SURFACING (живой) ---------------------------------------------

#[test]
fn control_check_missing_repo_surfaces_stderr() {
    let Some(binary) = find_real_binary() else {
        eprintln!("skip: arch-ml не найден (SPINE_BE_BIN / PATH)");
        return;
    };
    let client = client_with(binary, Duration::from_secs(60));
    match client.control_check(Path::new("/definitely/no/such/repo-qa"), None) {
        Err(SpineBeError::ProcessFailed { code, stderr }) => {
            assert_eq!(code, Some(1), "arch-ml: exit 1 на недоступном репо");
            assert!(
                stderr.contains("репозиторий недоступен"),
                "понятный stderr поднят наверх: {stderr}"
            );
        }
        other => panic!("ожидался ProcessFailed, получено: {other:?}"),
    }
}

// --- 10. RELATIVE-BINARY-CWD --------------------------------------------------

#[test]
fn relative_binary_path_with_foreign_cwd() {
    // Тестовый процесс живёт в корне пакета (sdk/rust): кладём фейк-бинарь
    // в target/qa-rel и ссылаемся на него ОТНОСИТЕЛЬНЫМ путём, а cwd процесса
    // назначаем другой каталог. Относительный путь обязан резолвиться от cwd
    // вызывающего (иначе — platform-specific поведение spawn).
    let bin_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/qa-rel-bin");
    std::fs::create_dir_all(&bin_dir).expect("создать каталог фейк-бинаря");
    let bin_abs = bin_dir.join("fake-arch-ml");
    std::fs::write(&bin_abs, "#!/bin/sh\nprintf 'ответ\\n'\n").expect("записать фейк-бинарь");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&bin_abs, std::fs::Permissions::from_mode(0o755))
            .expect("сделать исполняемым");
    }
    let client = Client {
        binary: PathBuf::from("target/qa-rel-bin/fake-arch-ml"),
        default_timeout: Duration::from_secs(5),
        cwd: Some(temp_dir("cwd")),
    };
    let result = client
        .run("вопрос", None, None, None)
        .expect("относительный SPINE_BE_BIN + чужой cwd обязан работать");
    assert_eq!(result.answer, "ответ");
}

// --- Специфика Rust: spawn_with_retry (ETXTBSY), Send/Sync -------------------

#[test]
fn spawn_with_retry_parallel_stress() {
    // 10 потоков × 20 вызовов против быстрого фейк-бинаря: гонка fork/exec
    // из многопоточного процесса — та самая среда, где всплывает ETXTBSY.
    // Все 200 spawn'ов обязаны завершиться успехом.
    let client = Arc::new(client_with(
        fake_binary("#!/bin/sh\nprintf 'ok\\n'\n"),
        Duration::from_secs(10),
    ));
    let started = Instant::now();
    let mut handles = Vec::new();
    for _ in 0..10 {
        let client = Arc::clone(&client);
        handles.push(std::thread::spawn(move || {
            for _ in 0..20 {
                let result = client.run("вопрос", None, None, None);
                assert!(result.is_ok(), "spawn в гонке: {result:?}");
            }
        }));
    }
    for handle in handles {
        handle.join().expect("поток стресс-теста не паникует");
    }
    assert!(
        started.elapsed() < Duration::from_secs(120),
        "стресс-прогон не должен деградировать: {:?}",
        started.elapsed()
    );
}

#[test]
fn client_is_send_sync_and_shared_across_threads() {
    // Компайл-тайм контракт: Client можно делить между std::thread.
    fn assert_send_sync<T: Send + Sync>() {}
    assert_send_sync::<Client>();

    let client = Arc::new(client_with(
        fake_binary("#!/bin/sh\nprintf 'ok\\n'\n"),
        Duration::from_secs(5),
    ));
    let handles: Vec<_> = (0..4)
        .map(|_| {
            let client = Arc::clone(&client);
            std::thread::spawn(move || client.run("вопрос", None, None, None))
        })
        .collect();
    for handle in handles {
        let result = handle.join().expect("поток не паникует");
        assert_eq!(result.expect("run из чужого потока").answer, "ok");
    }
}
