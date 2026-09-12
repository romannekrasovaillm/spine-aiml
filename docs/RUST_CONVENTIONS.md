Все 13 файлов прочитаны. Ниже — дистиллят обязательных конвенций под задачу «CLI+TUI харнесс на tokio + ratatui + reqwest».

---

# Конвенции Rust-скиллов (plugins/rust/skills/*) — выжимка для нового проекта

## 1. Канонический скелет проекта

### Cargo.toml

Канон (rust-project-setup/SKILL.md:19-39): сразу после `cargo new` привести манифест к виду:

```toml
[package]
name = "myapp"
version = "0.1.0"
edition = "2024"                 # обязательно edition 2024 (стабильна с 1.85)
rust-version = "1.85"            # MSRV; проверяется cargo-msrv и отдельной CI-джобой
description = "One-line description"
license = "MIT OR Apache-2.0"    # дефолт экосистемы
repository = "..."

[lints.rust]
unsafe_code = "forbid"           # для кода без FFI — включить обязательно
missing_docs = "warn"            # для библиотек

[lints.clippy]
all = { level = "warn", priority = -1 }
pedantic = { level = "warn", priority = -1 }   # точечно глушить с обоснованием
unwrap_used = "warn"
module_name_repetitions = "allow"

[profile.release]
lto = "thin"                     # максимум: lto = "fat" + codegen-units = 1
debug = "line-tables-only"       # чтобы профилировщики видели стеки (rust-performance)
```

Жёсткие правила:
- `Cargo.lock` **коммитить всегда** — и для библиотек тоже (воспроизводимый CI; в реестр не публикуется).
- Edition 2024: `unsafe_op_in_unsafe_fn` включён, атрибуты типа `no_mangle` — через `#[unsafe(...)]`, изменён захват лайфтаймов в RPIT.
- `rust-toolchain.toml` в корне: `channel = "1.87"` (или stable), `components = ["rustfmt", "clippy"]`.
- Если проект разрастётся (core + cli + tui) — workspace с virtual manifest: `resolver = "3"`, `[workspace.package]`, `[workspace.dependencies]` (единые версии: `serde = { version = "1", features = ["derive"] }`, `tokio = { version = "1", features = ["full"] }`), `[workspace.lints.clippy]`; в членах — `serde.workspace = true`, `edition.workspace = true`, `[lints] workspace = true`.

### Layout src/ — правило тонкого main

**Обязательно** (rust-project-setup/SKILL.md:45): «даже у CLI логика живёт в `src/lib.rs` (тестируемо, переиспользуемо), `src/main.rs` — 10–30 строк: парсинг аргументов → вызов lib → маппинг ошибки в exit code».

```
myapp/
├── Cargo.toml
├── rust-toolchain.toml
├── src/
│   ├── main.rs          # тонкий: только запуск
│   ├── lib.rs           # pub use ключевых типов, //! crate docs
│   ├── config.rs        # модуль = файл
│   └── config/          # подмодули (БЕЗ mod.rs — стиль 2018+)
│       └── parser.rs
├── tests/               # интеграционные; каждый файл — отдельный крейт
│   └── common/mod.rs    # общие хелперы (не common.rs — иначе станет тестом)
├── benches/             # criterion
└── examples/
```

### Импорты/экспорты и доки

- Re-export ключевых типов в корне: `pub use`, «чтобы пользователь не писал длинные пути» (patterns.md:89); внутренности — `pub(crate)`.
- Модули по файлам, не всё в `lib.rs`.
- У крейта — вводный `//!`-doc с примером уровня hello world; README синхронизировать через `#![doc = include_str!("../README.md")]` (примеры из README станут doctests).
- Каждый публичный элемент — rustdoc с **работающим примером** (doctest) и секциями `# Errors`, `# Panics`, `# Safety` где применимо. Ссылки — intra-doc (`` [`Config`] ``), не URL. В doctests с `?` — скрытая обёртка `# fn main() -> Result<(), Box<dyn std::error::Error>> { ... # Ok(()) }`.
- CHANGELOG.md в формате Keep a Changelog, заполняется по мере PR.

### CI-минимум (обязательный набор)

```bash
cargo fmt --all -- --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-features
cargo doc --no-deps          # RUSTDOCFLAGS="-D warnings"
```

В CI: `RUSTFLAGS: "-D warnings"` глобально; экшены `dtolnay/rust-toolchain` + `Swatinem/rust-cache`; MSRV-джоба с `cargo check --all-features` на версии из `rust-version`. По мере зрелости: `cargo deny check`, `cargo audit`, `cargo nextest run`, `cargo llvm-cov`.

## 2. Идиоматика

### Ошибки: thiserror vs anyhow — жёсткое разделение (error-handling.md)

| | Библиотека (lib) | Приложение (bin) |
|---|---|---|
| Тип | свой enum через `thiserror` | `anyhow::Error` (или eyre) |
| Цель | дать вызывающему **матчиться** на варианты | читаемый отчёт с контекстом |
| main | — | `fn main() -> anyhow::Result<()>` |

Прямая цитата-запрет: «Библиотека, возвращающая `anyhow::Error`, лишает пользователя возможности программно различать ошибки — это ошибка дизайна». Для нашего харнесса: ядро (lib) — thiserror-enum'ы; main/TUI-слой — anyhow.

Обязательные требования к типу ошибки (C-GOOD-ERR):
- `#[derive(Debug, thiserror::Error)]`, `#[non_exhaustive]` на публичном enum;
- `Display` — строчная буква, без точки в конце, без дублирования source;
- причина сохраняется через `#[source]` / `#[from]`, **не «плющится» в строку**;
- `Send + Sync + 'static` (иначе нельзя переносить между потоками и заворачивать в anyhow);
- `#[from]` — только когда конверсия однозначна; когда нужен контекст (какой файл?) — `map_err` с явным вариантом `Io { path, source }`;
- `pub type Result<T, E = ConfigError> = std::result::Result<T, E>;`

В приложении: **каждый** `?` сопровождается `.context()`/`.with_context(|| ...)` («что мы пытались сделать»); печать цепочки — `{:#}`. `Option → Result` — `ok_or_else` (lazy). `Result → Option` (`.ok()`) — только когда ошибка реально безразлична, иначе логировать.

Паники: только нарушенные внутренние инварианты. `expect("описание инварианта — why this cannot fail")` вместо `unwrap()`; паникующие публичные функции обязаны иметь `# Panics`; `v.get(i)` вместо `v[i]` на недоверенном входе; не использовать `catch_unwind` как try/catch.

### API-дизайн (api-design.md) — коды правил из Rust API Guidelines

- **C-CASE**: типы `UpperCamelCase`, функции/модули/поля `snake_case`, константы `SCREAMING_SNAKE_CASE`; акронимы как слова: `HttpClient`, не `HTTPClient`.
- **C-CONV**: `as_` (бесплатно, ссылка→ссылка), `to_` (дорого, аллокация), `into_` (забирает self).
- **C-GETTER**: геттер называется как поле — `fn len(&self)`, не `get_len`.
- **C-ITER**: `iter()`/`iter_mut()`/`into_iter()`; типы итераторов — `Iter`/`IterMut`/`IntoIter`.
- Предикаты — `is_*`/`has_*`; fallible-варианты — префикс `try_`.
- **C-COMMON-TRAITS**: жадно derive `Debug` (практически обязателен), `Clone`, `PartialEq`/`Eq`, `Hash`, `Default` где семантика позволяет — orphan rule не даст пользователю добавить их самому.
- **C-CONV-TRAITS**: реализуй `From`/`TryFrom` (Into получишь бесплатно), `FromStr`, `AsRef`. «Не реализуй `Into` напрямую — только `From`».
- **Вход гибкий, выход конкретный**: принимай `impl AsRef<Path>`, `impl Into<String>`, `impl IntoIterator<Item = T>`; `&str` вместо `&String`, `&[T]` вместо `&Vec<T>` (clippy `ptr_arg`). Возвращай конкретные типы или `impl Iterator`; не навязывай `Box<dyn ...>`.
- **C-CALLER-CONTROL**: нужно владение — принимай по значению, а не `&T` + внутренний clone.
- **C-CTOR**: `new()`; если `new()` без аргументов ⇒ обязан быть `Default` (clippy `new_without_default`).
- **C-BUILDER**: «три и более опциональных параметра → builder». Методы consuming `self` (чейнинг) или `&mut self` (циклы/условия) — **один стиль на весь API**; `build()` возвращает `Result` при нетривиальной валидации.
- **C-STRUCT-PRIVATE**: поля приватны, доступ через методы. `#[non_exhaustive]` на растущих enum и структурах-конфигах. **C-NEWTYPE-HIDE**: сложные внутренности за newtype. **C-SEALED** для трейтов, которые не должны реализовывать пользователи.
- **C-SEND-SYNC**: типы должны оставаться `Send`/`Sync` (потеря — breaking change; тест `fn assert_send<T: Send>() {}`).
- **C-SERDE**: типы данных общего назначения — serde за feature-флагом `serde`.

### Ownership / клонирование / Arc (SKILL.md «Ownership», patterns.md)

- Заимствуй по умолчанию (`&str`, `&[T]`, `&T`); владение забирай только когда функция реально потребляет/сохраняет. «`clone()` — осознанное решение, а не способ утихомирить компилятор»; если клон напрашивается — сигнал перестроить (сузить scope, разбить структуру, `std::mem::take`/`replace`, индексы вместо ссылок).
- Лестница разделяемого владения: `Rc` (однопоточно) / `Arc` (многопоточно); внутренняя изменяемость: `Cell` (Copy) → `RefCell` → `Mutex`/`RwLock` → `OnceLock`/`LazyLock` для статик. **«Выбирай минимально мощный инструмент»**.
- `Cow<'_, str>` — когда функция иногда модифицирует вход, а чаще нет.
- Самоссылочные структуры запрещены как подход: «реструктурируй, используй индексы/арены».

### Паттерны: ОБЯЗАТЕЛЬНЫЕ и ЗАПРЕЩЁННЫЕ (patterns.md)

Обязательные/поощряемые:
- **Make invalid states unrepresentable**: enum вместо bool+Option-полей (пример `enum Connection { Disconnected, Connecting{..}, Connected{..}, Failed{..} }`).
- **Newtype** для доменных типов (`UserId(u64)` vs `OrderId(u64)`, единицы измерения `Meters(f64)`) и обхода orphan rule.
- **Typestate** для протоколов с фазами (метод существует только у нужного состояния).
- **RAII-guard** для ресурсов.
- `let else` для ранних выходов; `matches!` для булевых проверок паттерна; исчерпывающий `match`.
- `std::mem::take`/`replace` — забрать из `&mut` без клона.
- Итераторные цепочки (`filter_map`, `collect::<Result<Vec<_>,_>>()` — останавливается на первой ошибке) — «но без фанатизма: если цепочка нечитаема, обычный `for` лучше».
- `#[derive(Default)] + struct update`: `Config { port: 8080, ..Default::default() }`.
- Числа: `try_into()` вместо `as` для сужающих конверсий; осознанный выбор `checked_*`/`saturating_*` (в release переполнение молча заворачивается!).
- Generics/`impl Trait` — статическая диспетчеризация по умолчанию; `dyn Trait` — для гетерогенных коллекций/плагинов/размера бинарника; конечное число вариантов — enum + match лучше обоих.

Запрещённые антипаттерны (прямой список):
- **`Deref`-полиморфизм** (эмуляция наследования) — композиция + делегирование вместо.
- **clone() ради borrow checker**.
- **unwrap/expect как стиль** в неигрушечном коде.
- **`#[allow(...)]` без комментария-обоснования**; в 2024 предпочитай `#[expect(...)]` (упадёт, когда линт перестанет срабатывать).
- **Булевы параметры-флаги** — заменять на enum'ы (`render(true,false)` → `Compact/Pretty`).
- **Stringly-typed API** (`&str`-статусы вместо enum).
- **Глобальное изменяемое состояние** (`static mut` в 2024 фактически запрещён) → `OnceLock`/`LazyLock` или явная передача зависимостей.
- **Преждевременный `Arc<Mutex<T>>`** — сначала «владение одним потоком + каналы».
- Игнорирование `must_use`; `let _ = ...` только осознанно.
- `Vec<Box<dyn Trait>>` там, где хватит enum-диспетчеризации.
- Функции >50–70 строк или с 5+ аргументами — кандидаты на разбиение/параметр-структуру (clippy `too_many_arguments`).

## 3. Async/tokio: каноны и грабли

### Модель и правило №1

- «`async fn` возвращает **ленивую** Future: до `.await` или `spawn` ничего не выполняется». `.await` — точка возможной отмены: drop future = отмена, «код после `.await` просто не выполнится».
- `tokio::spawn` требует `Send + 'static`: не-Send типы (`Rc`, `RefCell`) не должны жить через `.await`; данные захватывать `move` + `Arc`. Ошибка «future cannot be sent between threads safely» ⇒ не-Send значение живёт через точку `.await`; лечение: сузить scope, `Rc→Arc`, `RefCell→Mutex`.
- **Правило №1 Tokio: не блокируй runtime.** ❌ `std::thread::sleep` → ✅ `tokio::time::sleep().await`; ❌ `std::fs`, blocking DB/HTTP (`reqwest::blocking` внутри async запрещён — для харнесса только async `reqwest`), тяжёлые CPU-циклы → ✅ `spawn_blocking(move || ...).await?` / `tokio::fs`. Ориентир: «между `.await` не проводи больше ~10–100 мкс CPU-времени; длиннее — `spawn_blocking` или `yield_now().await`».
- **Мьютексы**: короткая секция без `.await` — обычный `std::sync::Mutex` (быстрее!); `tokio::sync::Mutex` — только если lock держится через `.await`. `std::sync::MutexGuard` через `.await` — не-Send + дедлок; паттерн: `{ let mut g = m.lock().unwrap(); g.push(x); }` и только затем `.await`.
- Message passing по умолчанию; `Arc<Mutex>` — когда каналы неудобны. Акторный паттерн: «задача владеет состоянием (никаких Mutex), общение — `mpsc` + `oneshot` для ответов» — это прямая рекомендация для архитектуры TUI-харнесса (event loop + акторы).

### Каналы — таблица выбора

| Канал | Семантика | Применение |
|---|---|---|
| `mpsc::channel(cap)` | многие→один, backpressure | очередь работ к актору |
| `oneshot` | одно значение | ответ request/response к актору |
| `broadcast` | один→многие, каждый всё | события; отстающие получают `Lagged` |
| `watch` | один→многие, только последнее | конфиг, сигнал shutdown |

«Bounded (`channel(cap)`) по умолчанию — backpressure бесплатно; `unbounded` — осознанное решение с риском OOM».

### select!, таймауты, отмена, shutdown

- `select!` **дропает невыбранные ветки** ⇒ futures должны быть cancellation-safe. Из доков Tokio: `recv()` — safe; `read_exact`/`write_all` и составные операции — **НЕ safe** (теряют частично считанное). Если future нельзя терять: `let fut = ...; tokio::pin!(fut);` и селектить `&mut fut` в цикле.
- Таймаут: `tokio::time::timeout(dur, fut).await`; по таймауту future отменяется. **Таймауты на все внешние I/O** (пункт чеклиста).
- Graceful shutdown (канон tokio.rs/topics/shutdown): сигнал через `watch`/`CancellationToken` (tokio-util) + ожидание задач через `TaskTracker`/`JoinSet`; слушать `tokio::signal::ctrl_c()` — для TUI это обязательный сценарий.
- Запуск задач: `JoinSet` для групп однотипных (`set.spawn(...)`, `while let Some(res) = set.join_next().await`; drop JoinSet отменяет всё). `JoinHandle` при drop **не** отменяет задачу — результат теряется; `handle.abort()` для отмены. Паника в задаче не роняет процесс — проверять `JoinError::is_panic()`.
- Конкурентность без spawn: `tokio::join!`/`try_join!`; для коллекций — `futures::stream::iter(items).map(work).buffer_unordered(N)` (конкурентность с лимитом N; нужен порядок — `buffered(N)`); глобальные лимиты — `Semaphore`.
- Async-трейты: `async fn` в трейте стабилен, но не dyn-compatible — для `Box<dyn Trait>` крейт `async-trait` или `Pin<Box<dyn Future>>` вручную.

### Грабли из pitfalls.md (диагностика → лечение)

1. **Future создана, но не запущена** (`let fut = send_email(u);` — ничего не происходит, письмо не отправлено). Лечение: `.await`/`spawn`/`join!`. Смежное: последовательные `.await` в цикле — НЕ конкурентность; конкурентно — `buffer_unordered(10)` + `try_collect`.
2. **Блокирующий вызов в async**: виновники — `std::fs::*`, `std::net`, `std::thread::sleep`, синхронные клиенты, zip/крипто/большая сериализация, `Command::output()` без `tokio::process`. Диагностика: `tokio-console` + `RUSTFLAGS="--cfg tokio_unstable"`.
3. **MutexGuard через .await** — не компилируется в spawn, а в LocalSet «компилируется и дедлочит».
4. **Дедлок двух мьютексов** — глобальный порядок захвата или один мьютекс на агрегат; `RwLock`: read-lock → write-lock в той же задаче = дедлок. Диагностика: tokio-console, `parking_lot` с фичей `deadlock_detection`.
5. **Cancellation — тихая потеря работы**: `timeout(dur, save_to_db(x))` — запись может не случиться; `select!` в цикле с пересозданием `read_exact` — частично считанное ТЕРЯЕТСЯ. Критические секции, которые нельзя отменять, — в отдельную `spawn`-задачу и ждать `JoinHandle`. Async-cleanup из Drop не сделать (Drop синхронен) — «планируй shutdown явно».
6. **`block_on` внутри runtime** → паника «Cannot start a runtime from within a runtime» или дедлок; `block_in_place` — осознанно и только multi-thread.
7. **Задачи-сироты и проглоченные паники**: spawn без сохранения JoinHandle → паника уходит молча; для системных задач — `JoinSet`/`TaskTracker` + централизованный `join_next`. В тестах доводить shutdown до конца (`tracker.wait().await`), иначе флаки.
8. **Стримы**: `while let Some(item) = stream.next().await` (`futures::StreamExt`/`tokio_stream`); `buffer_unordered` меняет порядок; бесконечный стрим + `collect` = зависание.
9. **Время**: в тестах `#[tokio::test(start_paused = true)]` + `time::advance()`; тайминги мерять `Instant` (монотонен), не `SystemTime`.

Микро-чеклист ревью async-кода (целиком обязателен): нет блокирующих вызовов на worker'ах; guard'ы std-мьютексов не живут через `.await`; все spawn-задачи кем-то join'ятся или явно «fire-and-forget» с комментарием; ветки `select!` cancellation-safe; каналы bounded; shutdown-путь существует и тестируется; таймауты на все внешние I/O.

## 4. Тестирование

### Методология и структура

- «Тестируй поведение через публичное API, а не внутренности»; приватное — только если нетривиальный алгоритм.
- «Пирамида по-растовски: значительную часть работы делает компилятор + типы»; фокус — логика, граничные случаи, ошибочные пути, инварианты. **Каждый баг → сначала воспроизводящий тест, потом фикс** (имя с номером issue).
- Детерминизм: без реального времени, сети, порядка-зависимости. «Flaky-тест — это баг».

Layout: юнит-тесты рядом с кодом (`#[cfg(test)] mod tests` + `use super::*;`), doctests в `///`, интеграционные в `tests/*.rs` (только публичное API), хелперы — `tests/common/mod.rs`.

Правила оформления:
- Имя теста — утверждение о поведении: `returns_error_on_empty_input`, не `test1`.
- Один смысловой аспект на тест; `assert_eq!(got, want, "case: {name}")` с контекстом.
- Тесты могут возвращать `Result`: `fn t() -> anyhow::Result<()>` — `?` вместо каскада unwrap.
- `#[should_panic(expected = "substring")]` — всегда с `expected`; для ошибок предпочитать `matches!` на `Err`.
- Дорогие/внешние — `#[ignore = "reason"]`, запуск `cargo test -- --ignored`.
- Изоляция общего состояния (тесты в одном бинаре идут параллельно): `tempfile::tempdir()`, свободный порт `TcpListener::bind("127.0.0.1:0")`, для env — крейт `temp-env` или `#[serial]` из `serial_test`.

### Async / CLI / файлы — конкретные рецепты (recipes.md)

- **Tokio**: `#[tokio::test]` (каждый тест — свой однопоточный runtime); `#[tokio::test(start_paused = true)]` — виртуальное время, `sleep` мгновенны: «60 "виртуальных" секунд пройдут мгновенно и детерминированно».
- **CLI end-to-end**: `assert_cmd` + `predicates`:
  ```rust
  Command::cargo_bin("myapp").unwrap()
      .args(["--config", "nope.toml"])
      .assert().failure().code(2)
      .stderr(predicate::str::contains("nope.toml"));
  ```
  Файловые фикстуры — `tempfile::tempdir()`; golden-вывод — в сочетании с `insta`.
- **proptest** (dev-dep `proptest = "1"`): инварианты — roundtrip (`decode(encode(x)) == x`), «парсер не паникует ни на каком вводе», идемпотентность, эквивалентность наивной реализации. Каталог `proptest-regressions/` **коммитить**.
- **insta**: снапшоты больших выводов (JSON, рендеры, error messages) — `assert_yaml_snapshot!`/`assert_json_snapshot!`; нестабильные поля — редакция `{ ".created_at" => "[ts]" }`; ревью через `cargo insta review`; снапшоты коммитить. Для TUI — естественный инструмент снапшотить рендер кадров.
- **mockall**: мокать **свои трейты-порты** (границы: БД, HTTP, время — `#[cfg_attr(test, mockall::automock)] trait Clock`), не чужие типы. «Если expectations становятся сложными сценариями — это запах: замени мок фейком (in-memory реализация)».
- **criterion**: `[[bench]] harness = false`, `black_box` на входах, setup вне `b.iter`, baseline: `cargo bench -- --save-baseline main` → `--baseline main`.
- **Запуск**: `cargo nextest run` (быстрее, retries) — но «НЕ гоняет doctests», поэтому `cargo test --doc` отдельно. Конфиг CI-профиля nextest: `retries = 2`, `fail-fast = false`, `slow-timeout = { period = "60s", terminate-after = 2 }`.

Чеклист покрытия (обязателен): happy path каждого публичного метода; каждый вариант ошибки конструируем и возвращается когда должен; границы (пустой вход, максимум, unicode, нулевые длительности); паникующие пути с `#[should_panic(expected)]`; roundtrip/идемпотентность property-тестами; конкурентный код — shutdown, таймауты, отсутствие дедлока (тест с `timeout`); regression на каждый баг.

## 5. Performance — что реально предписано

Методология (жёстко): «**измеряй, потом меняй**… оптимизация без профиля — это генерация случайных диффов». Всё измерять только в `--release`; зафиксировать baseline (criterion/hyperfine); менять одну вещь за раз; ускорения <2–3% при шумном бенче не значимы.

Профиль release (бесплатные проценты): `lto = "fat"` (+5–20%), `codegen-units = 1`, опционально `panic = "abort"` (ломает `catch_unwind`), `strip = true`; `RUSTFLAGS="-C target-cpu=native"` не для дистрибутивных бинарников; аллокатор `mimalloc`/`jemallocator` — 5–20% на alloc-интенсивных нагрузках (измерить!).

Каталог предписанных оптимизаций (в порядке частоты пользы):
- **Аллокации/клоны — главный источник**: `with_capacity` заранее; переиспользование буфера (`buf.clear()` в цикле вместо нового `Vec`); убрать `clone()` с горячего пути (заимствования, `Rc`/`Arc`, `Cow<'_, str>`); `format!` в горячем цикле → `write!(buf, ...)` в переиспользуемый буфер; конкатенация → `push_str`; возвращать `impl Iterator` вместо промежуточных `Vec`.
- **Мелкие строки/векторы**: `smallvec`, `compact_str`/`smol_str`, интернирование повторяющихся строк — прямо названы.
- **Хэширование**: std `HashMap` (SipHash) медленный → `rustc-hash` (FxHashMap) или `ahash` для не-adversarial ключей; плотные целочисленные ключи — `Vec`-индексация.
- **Размеры типов**: `Box` большим вариантам enum (clippy `large_enum_variant`), проверка `std::mem::size_of`, niche-типы (`NonZeroU32`), `bitflags` для флагов.
- **Циклы**: итераторы вместо индексации (устранение bounds checks), `chunks_exact` для порционной обработки; `dyn` в горячем цикле → generics/enum.
- **I/O** (критично для TUI!): «файлы/сокеты оборачивай `BufReader`/`BufWriter` — небуферизованный построчный ввод медленнее на порядок»; «`stdout` лочится на каждый `println!` → `let mut out = io::stdout().lock();` + `writeln!`»; большие объёмы — `serde_json::to_writer` вместо `to_string`+write, бинарные форматы (bincode/postcard).
- **Параллелизм по данным**: `rayon` (`iter()` → `par_iter()`), но «работа на элемент должна окупать координацию (измерь!)».
- Время компиляции: `cargo build --timings`, убирать неиспользуемые features зависимостей (`cargo-machete`), линкер lld/mold.

Антипаттерны: оптимизировать без профиля/в debug; `unsafe` ради скорости до исчерпания safe-приёмов; микробенч без `black_box`; жертвовать корректностью; кэшировать без измерения hit rate.

## 6. Unsafe/FFI — коротко (проекту почти не нужно)

Для харнесса: поставить `unsafe_code = "forbid"` в `[lints.rust]` и закрыть вопрос. Если вдруг понадобится: unsafe не отключает проверки, а перекладывает доказательство на автора; сначала доказать необходимость (большинство случаев решается safe-средствами — индексы/арены, `OnceLock`, `bytemuck`/`zerocopy`); минимальные `unsafe {}`-блоки, на каждый — комментарий `// SAFETY:` (почему корректно, не что делаем), на каждую `unsafe fn` — секция `# Safety`; линты `undocumented_unsafe_blocks = "deny"`, `missing_safety_doc = "deny"`; инкапсуляция в safe-абстракцию (граница доверия = модуль); верификация Miri (`cargo +nightly miri test`), а не глазами. FFI — слоёная архитектура `foo-sys` + safe-обёртка, биндинги только через bindgen/cbindgen, паники через `extern "C"` запрещены (`catch_unwind`).

## 7. Одобренные крейты и антипаттерны зависимостей

### Крейты, прямо названные в скиллах

- **Ошибки**: `thiserror` (lib), `anyhow`/`eyre` (app).
- **Async**: `tokio` (`features = ["full"]` в workspace-шаблоне), `tokio-util` (CancellationToken, TaskTracker), `futures` (buffer_unordered), `tokio_stream`, `async-trait`, `reqwest` (только async-вариант; `reqwest::blocking` внутри async — запрещён), `axum`/`hyper` (упомянуты в описании скилла).
- **Сериализация**: `serde` (за feature-флагом для библиотек, `features = ["derive"]`), `serde_json`, `bincode`/`postcard` (бинарные), `toml` (в примере).
- **Конкурентность**: `rayon`, `parking_lot` (deadlock_detection).
- **Тесты**: `proptest`(+`proptest-derive`), `insta`, `mockall`, `criterion` (+альтернативы `divan`, `iai-callgrind`), `trybuild`, `assert_cmd` + `predicates`, `tempfile`, `temp-env`, `serial_test`.
- **Производительность**: `smallvec`, `compact_str`/`smol_str`, `rustc-hash` (FxHashMap), `ahash`, `bitflags`, `mimalloc`/`jemallocator`, `bytemuck`/`zerocopy`.
- **Инструменты**: cargo-nextest, cargo-llvm-cov, cargo-fuzz, Miri, cargo careful, flamegraph, samply, dhat, tokio-console, hyperfine, cargo-show-asm, cargo-hack, cargo-deny, cargo audit, cargo-semver-checks, cargo-msrv, cargo-machete, cargo-pgo, cross, cargo-dist, release-plz/cargo-release, cargo-chef.
- **FFI** (не для этого проекта): bindgen, cbindgen, cc, cxx, PyO3+maturin.

**Чего скиллы НЕ упоминают** (честная оговорка): `clap`, `ratatui`/`crossterm`, `tracing`/`log` в тексте скиллов отсутствуют — выбирать их придётся вне канона этих скиллов (де-факто стандарт экосистемы: clap derive для CLI, crossterm-бэкенд для ratatui, tracing + tracing-subscriber для логов в tokio-приложениях).

### Антипаттерны зависимостей/features

- Фичи **только аддитивны**: «включение фичи не должно ломать или менять поведение существующего кода»; взаимоисключающие фичи запрещены — выбор реализации через типы/generics, не `#[cfg]`.
- Опциональная зависимость: `serde = { version = "1", optional = true }` → фича `serde = ["dep:serde"]`.
- «Минимальный `default = []` у библиотек, широкий у приложений»; проверка комбинаций `cargo hack check --feature-powerset`; на docs.rs — `all-features = true`.
- Не навязывать `anyhow` пользователям библиотеки («не тянет ничего в публичный API» — про thiserror).
- «Не тащи Tokio в программу, которая читает один файл» — модель конкурентности выбирается по задаче (таблица: async для массового I/O, rayon для CPU-bound, std::thread для фоновых работ).
- Повышение версии публично видимой зависимости — **breaking change** (тип из неё в твоём API); ловить `cargo semver-checks`.
- Неиспользуемые зависимости/features выпиливать (`cargo-machete`); дубликаты/лицензии/уязвимости — `cargo deny check` + `cargo audit`.
- Semver-ловушки: добавление поля в публичную структуру без `#[non_exhaustive]`, потеря `Send`/`Sync` (например, добавил `Rc` внутрь), ужесточение bounds у generic, метод трейта без default-реализации. До 1.0 minor играет роль major (`0.3.x → 0.4.0` — breaking).

---

**Резюме для харнесса (CLI+TUI, tokio, ratatui, reqwest)**: тонкий `main.rs` (парсинг → lib → exit code), вся логика в `lib.rs` с thiserror-ошибками, приложение-слой на anyhow с `.context()`; акторная архитектура (задача владеет состоянием, `mpsc` bounded + `oneshot`), shutdown через `CancellationToken`/`watch` + `TaskTracker` + `ctrl_c()`, таймауты на всех reqwest-вызовах, никаких блокирующих вызовов на worker'ах (файлы — `tokio::fs`, CPU — `spawn_blocking`); тесты: `#[tokio::test(start_paused = true)]`, `assert_cmd` для CLI, `insta` для снапшотов рендера TUI, `tempfile` для фикстур; CI: fmt + clippy `-D warnings` + test + doc, edition 2024, MSRV 1.85, `lto = "thin"`.
</agent_swarm_result>
