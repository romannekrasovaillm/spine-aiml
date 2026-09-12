# Spine-BE SDK для Rust (`spine-be-sdk`)

Тонкий Rust-клиент поверх headless CLI `arch-ml` (Spine Banking Edition),
контракт v1 (`sdk/CONTRACT.md`). SDK запускает процесс `arch-ml` **без shell**
(argv-массив), читает stdout/stderr/exit code и разбирает JSON по контракту.
Сетевых вызовов в SDK нет — вся работа идёт через локальный процесс.

Зависимости: только `serde` (derive) + `serde_json`. Edition 2021, Rust 1.85+.
Крейт standalone: собирается независимо из каталога `sdk/rust`, корневой
`Cargo.toml` репозитория не затрагивается.

## Подключение

Крейт не публикуется в crates.io — подключается как path-зависимость:

```toml
[dependencies]
spine-be-sdk = { path = "sdk/rust" }  # путь относительно вашего Cargo.toml
```

Клиент берёт бинарь из переменной окружения `SPINE_BE_BIN`, иначе ищет
`arch-ml` в `PATH`:

```bash
export SPINE_BE_BIN=/path/to/target/release/arch-ml
```

## Примеры

### `run` — headless-прогон агента (LLM)

```rust
use spine_be_sdk::Client;
use std::time::Duration;

let client = Client::new();
let result = client.run(
    "Сформулируй ADR по переходу на saga",
    Some("deepseek"),            // модель (None — дефолт из конфига arch-ml)
    Some(Duration::from_secs(300)), // клиентский таймаут (None — дефолт 600 с)
    Some(10),                    // лимит итераций инструментов
)?;
println!("{} (за {:?})", result.answer, result.duration);
```

`timeout` — клиентский таймаут SDK: по истечении процесс убивается и
возвращается `SpineBeError::Timeout`. Флаг `--timeout` самого CLI SDK не
использует; промпт `-` (чтение stdin) не поддерживается.

### `control_check` — fitness-контроль репозитория

```rust
let report = client.control_check(
    ".",                                    // репозиторий
    None,                                   // constraints (None — <repo>/.arch-handoff/CONSTRAINTS.yaml)
)?;
if !report.passed {
    // Красный гейт (exit 1 у CLI) — это ДАННЫЕ, а не ошибка:
    for issue in &report.issues {
        eprintln!("{}:{} [{}] {}", issue.file, issue.line, issue.rule, issue.message);
    }
}
println!("{}", report.summary);
```

### `archify_validate` — валидация IR диаграммы

```rust
let receipt = client.archify_validate("architecture", "sbp-v1.architecture.json")?;
assert!(receipt.ok());
if let Some(checks) = receipt.checks() {
    for check in checks {
        println!("{}: {}", check["name"], check["ok"]);
    }
}
```

### `archify_deliver` — сборка HTML-артефакта

```rust
let receipt = client.archify_deliver(
    "architecture",
    "sbp-v1.architecture.json",
    "out/sbp-v1.html",
)?;
if let Some(artifact) = receipt.artifact() {
    println!("sha256={}, bytes={}", artifact["sha256"], artifact["bytes"]);
}
```

### `archify_compare` — дельта двух architecture-снапшотов

```rust
let receipt = client.archify_compare(
    "sbp-v1.architecture.json",
    "sbp-v2.architecture.json",
    "out/sbp-delta.html",
)?;
let summary = receipt.summary().unwrap();
println!("компонентов добавлено: {}", summary["components"]["added"]);
```

Receipt (`ArchifyReceipt`) — обёртка над `serde_json::Value`: типизированы
только общие поля контракта (`ok()`, `command()`, `schema_version()`,
`diagram_type()`, `summary()`, `checks()`, `artifact()`), всё остальное
доступно через `receipt.raw()`.

## Обработка ошибок

Единый набор ошибок по §4 контракта (`SpineBeError`):

| Вариант | Условие |
|---|---|
| `BinaryNotFound { binary, source }` | бинарь не найден или не исполняемый |
| `Timeout { timeout }` | клиентский таймаут, процесс убит |
| `ProcessFailed { code, stderr }` | ненулевой exit без валидного JSON-контракта |
| `ContractViolation { message }` | stdout не JSON там, где контракт требует JSON |

```rust
use spine_be_sdk::{Client, SpineBeError};

match Client::new().control_check(".", None) {
    Ok(report) => println!("гейт: {}", report.passed),
    Err(SpineBeError::BinaryNotFound { binary, .. }) => {
        eprintln!("установите arch-ml или задайте SPINE_BE_BIN ({})", binary.display())
    }
    Err(SpineBeError::Timeout { timeout }) => eprintln!("таймаут {timeout:?}"),
    Err(SpineBeError::ProcessFailed { code, stderr }) => {
        eprintln!("arch-ml упал (exit {code:?}): {stderr}")
    }
    Err(SpineBeError::ContractViolation { message }) => {
        eprintln!("ответ вне контракта: {message}")
    }
}
```

Важно: `control check` с `passed=false` — **не** `Err`, а `Ok(FitnessReport)`
(красный гейт — данные, §4 контракта).

## Тесты

```bash
# юнит-тесты (argv, разбор на скрипте-заглушке) + живые интеграционные:
SPINE_BE_BIN=/path/to/target/release/arch-ml cargo test

# без бинаря живые тесты пропускаются (skip), юнит-тесты работают всегда:
cargo test

# линт:
cargo clippy --all-targets
```

Живые тесты используют эталонные фикстуры репозитория (§5 контракта):
IR диаграмм `banking/demos/cli-from-claude-code/scenario2-archify-cli/` и
гейт-фикстуру `scenario3-gate/fixtures/` (красный кейс собирается во временной
копии добавлением `src/bad.py` с 16-значным числом). LLM-прогон `run` в
автотестах не выполняется — проверяется только построение argv и разбор
ответа на скрипте-заглушке (§5 контракта).

`tests/adversarial.rs` — враждебный сьют (QA): pipe-deadlock на флуде
stdout/stderr, таймаут с внуками (групповой kill, зомби, утечка fd по
`/proc/self/fd` на 50 прогонах), битый JSON, unicode, промпт с дефиса
(включая живой clap-разбор без LLM-вызова), относительный `SPINE_BE_BIN`
с чужим cwd, стресс параллельных spawn и `Client: Send + Sync`.

## Известные ограничения

- Промпт `-` (чтение из stdin) в `run` не поддерживается — промпт всегда
  передаётся позиционным аргументом после `--`.
- Флаг `--timeout` самого CLI не пробрасывается: таймаут только клиентский
  (kill процесса), per-call — в `run`, иначе `client.default_timeout`.
- Параметр `--quality` команд archify не выставляется (дефолт CLI — showcase).
- Промежуточный вывод LLM-прогона не стримится: `run` возвращает финальный
  ответ целиком после завершения процесса (контракт `-q`).

## Ограничения/найденное в QA (adversarial-прогон 2026-09-03)

Раздел фиксирует факты, найденные враждебными тестами (`tests/adversarial.rs`),
и поведение, которое важно знать потребителю SDK.

- **Kill по таймауту — групповой (unix).** Процесс `arch-ml` запускается
  лидером своей группы процессов (`process_group(0)`), и по клиентскому
  таймауту убивается вся группа (`SIGKILL`): иначе форкнутые CLI внуки
  переживали бы kill, удерживали пайпы stdout/stderr открытыми и вешали
  читателей-потоков (утечка fd и потоков — до фикса +2 fd на каждый
  таймаут). Побочный эффект: `arch-ml` не состоит в группе процесса
  вызывающего — Ctrl-C в терминале родителя на него не распространяется,
  жизненным циклом управляет только SDK.
- **Групповой kill — только через `/bin/sh` builtin kill.** Отрицательный
  pid у builtin — это killpg(2) по POSIX. Внешний `/usr/bin/kill` из
  **procps-ng 4.0.4 группы НЕ убивает**: молча игнорирует отрицательный
  pid и возвращает exit 0 (найдено QA-прогоном). На unix требуется
  наличие `/bin/sh` (базовое требование POSIX-системы); новых
  crate-зависимостей и unsafe нет.
- **Относительный `SPINE_BE_BIN` нормализуется.** Путь с разделителем
  каталога (`./arch-ml`, `target/debug/arch-ml`) абсолютизируется от cwd
  ВЫЗЫВАЮЩЕГО процесса: без этого при заданном `Client.cwd` относительный
  путь резолвился бы относительно рабочего каталога РЕБЁНКА
  (platform-specific, на Linux — именно так) и spawn падал бы с
  `BinaryNotFound`. Голое имя без разделителя (`arch-ml`) не трогается —
  это поиск по PATH.
- **Читатели-потоки join'ятся на всех путях**, включая таймаут и ошибку
  `try_wait`: fd пайпов закрываются сразу после группового kill
  (проверка `/proc/self/fd` до/после 50 таймаутов — дельта 0).
- **Промпт с дефиса** (`run("-что-то", ...)`) всегда идёт после `--`;
  проверено против реального `arch-ml`: clap принимает `--` + промпт
  (exit 1 на ненастроенной модели), без `--` был бы exit 2 (usage error).
- **Красный гейт и `ok=false` — данные, не исключения** (§4 контракта):
  `control check` exit 1 + валидный JSON → `Ok(FitnessReport)`,
  archify exit 1 + валидный receipt → `Ok(ArchifyReceipt)`.
- **`Client` — `Send + Sync`** (компайл-тайм проверка + прогон из
  нескольких `std::thread` по `Arc<Client>`); стресс `spawn_with_retry`:
  10 потоков × 20 вызовов без деградации.
- **Ограничение таймаута на не-unix** (Windows): группового kill нет,
  убивается только прямой ребёнок; внуки, унаследовавшие пайпы, могут
  задержать читателей до своего завершения.
