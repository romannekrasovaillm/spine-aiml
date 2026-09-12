---
name: performance
description: Оптимизация производительности Rust-кода по методологии The Rust Performance Book (Nethercote) - профилирование (flamegraph, perf, samply, dhat, tokio-console), бенчмарки criterion, настройка release-профиля (lto, codegen-units, PGO, target-cpu), снижение аллокаций и клонов, выбор структур данных и хэшеров, итераторы против индексации, size оптимизации бинарника. Используй этот навык ВСЕГДА, когда Rust-код работает медленно, потребляет много памяти или CPU, когда просят "ускорить", "оптимизировать", "профилировать" Rust-программу, сравнить производительность реализаций, уменьшить размер бинарника или время компиляции, а также при ревью производительно-критичного кода.
---

# Производительность Rust

Канон методологии — The Rust Performance Book (https://nnethercote.github.io/perf-book/). Главный тезис: **измеряй, потом меняй**. Интуиция о горячих местах ошибается систематически; оптимизация без профиля — это генерация случайных диффов.

## Методология: цикл оптимизации

1. **Проверь режим сборки.** №1 причина «Rust медленный» — debug-сборка. Всё измеряй только в `--release` (разница 10–100×).
2. **Зафиксируй метрику и нагрузку**: repeatable-сценарий (бенчмарк criterion / hyperfine для CLI / нагрузочный тест сервиса). Без базовой линии прогресс неотличим от шума.
3. **Сними профиль** и найди, где реально тратится время/память (инструменты ниже).
4. **Измени одну вещь**, перемерь против baseline (`cargo bench -- --baseline main`). Ускорения <2–3% при шумном бенче — не значимы.
5. **Останавливайся**, когда метрика достигнута: каждая следующая оптимизация обычно платит читаемостью.

Перед микрооптимизациями проверь алгоритмическую сложность (O(n²) не победить твиками) и I/O-паттерны (небуферизованные чтения, N+1 запросы).

## Инструменты профилирования

| Что ищем | Инструмент |
|---|---|
| CPU-время по функциям | `cargo flamegraph` (крейт flamegraph), `samply record ./target/release/app`, `perf record`/`perf report` (Linux) |
| Аллокации: кто и сколько | `dhat` крейт (`dhat::Profiler`), heaptrack, `valgrind --tool=dhat` |
| Async: блокировки, busy-задачи | `tokio-console` |
| Кэш-промахи, ветвления | `perf stat`, `cachegrind` |
| Ассемблер горячей функции | `cargo asm` (cargo-show-asm), godbolt.org |
| CLI end-to-end | `hyperfine 'app args'` |

Для честных стеков в release добавь символы: `[profile.release] debug = "line-tables-only"` (или полный `debug = true` в отдельном профиле `profiling`).

## Настройки сборки (бесплатные проценты)

```toml
[profile.release]
lto = "fat"              # или "thin" — компромисс времени сборки; +5..20% перфа
codegen-units = 1        # лучше оптимизации, дольше сборка
panic = "abort"          # меньше код, быстрее; НО catch_unwind перестаёт работать
# strip = true           # размер бинарника
```

- `RUSTFLAGS="-C target-cpu=native"` — использовать инструкции конкретного CPU (не для дистрибутивных бинарников).
- PGO (profile-guided optimization) — ещё 5–15% на больших приложениях: см. главу Build Configuration perf-book и `cargo-pgo`.
- Аллокатор: `mimalloc`/`jemallocator` как global_allocator часто даёт 5–20% на alloc-интенсивных нагрузках — измерь.
- Размер бинарника: `opt-level = "z"`, lto, strip, `panic = "abort"` + https://github.com/johnthagen/min-sized-rust.

## Каталог типовых оптимизаций (в порядке частоты пользы)

**Аллокации и клоны** — обычно главный источник:
- `String`/`Vec` в цикле → `with_capacity` заранее; переиспользуй буфер (`buf.clear()` в цикле вместо нового `Vec`);
- убери `clone()` по пути горячих данных: заимствования, `Rc`/`Arc` (клон = инкремент счётчика), `Cow<'_, str>`;
- `format!` в горячем цикле → `write!(buf, ...)` в переиспользуемый буфер; конкатенация → `push_str`;
- возвращай `impl Iterator` вместо собирания промежуточных `Vec`;
- мелкие строки/векторы: `smallvec`, `compact_str`/`smol_str`, интернирование повторяющихся строк;
- clippy-группа perf включена по умолчанию; дополнительные сигналы: `clippy::redundant_clone` (nursery).

**Структуры данных и хэширование**:
- std `HashMap` использует SipHash (устойчив к HashDoS, но медленный) → для не-adversarial ключей `rustc-hash` (FxHashMap) или `ahash`, для целочисленных плотных ключей — `Vec`-индексация;
- ищешь по отсортированному → `binary_search`; очередь с приоритетом → `BinaryHeap`; много вставок в середину — почти всегда всё равно `Vec` (кэш-локальность бьёт асимптотику `LinkedList`);
- уменьшай размер горячих типов: `Box` большим вариантам enum (clippy: `large_enum_variant`), порядок полей Rust оптимизирует сам; проверь `std::mem::size_of`; niche-типы (`NonZeroU32`, `Option<NonNull>`), битовые флаги — `bitflags`.

**Циклы, границы, SIMD**:
- итераторы вместо индексации — компилятор устраняет bounds checks (`for x in &v` вместо `for i in 0..v.len() { v[i] }`); порционная обработка — `chunks_exact`;
- hoisting инвариантов из цикла компилятор делает сам, но проверь профилем виртуальные вызовы (`dyn`) в горячем цикле → generics/enum-диспетч;
- автовекторизация: простые циклы по слайсам LLVM векторизует; проверь `cargo asm`; явный SIMD — `std::simd` (nightly) или крейт `wide`;
- параллелизм по данным: `rayon` — `iter()` → `par_iter()`; порог: работа на элемент должна окупать координацию (измерь!).

**I/O**:
- файлы/сокеты оборачивай `BufReader`/`BufWriter` — небуферизованный построчный ввод медленнее на порядок;
- `stdout` лочится на каждый `println!` → `let mut out = io::stdout().lock();` + `writeln!`;
- сериализация больших объёмов: рассмотри бинарный формат (bincode/postcard против JSON), `serde_json::to_writer` вместо `to_string`+write.

**Компиляция долгая** (тоже производительность): `cargo build --timings`, дели крейты, убирай неиспользуемые features зависимостей (`cargo-machete`), меньше proc-macro в горячих путях пересборки, линкер lld/mold.

## Анти-паттерны оптимизации

- Оптимизировать без профиля / в debug-режиме.
- `unsafe` ради скорости до того, как исчерпаны safe-приёмы (обычно итераторы+capacity дают то же).
- Микробенчмарк, который LLVM выкинул (нет `black_box`) или который меряет аллокацию сетапа.
- Жертвовать корректностью: убирать проверки, полагаться на UB «оно же работает».
- Кэшировать всё подряд без измерения hit rate.

## Первоисточники

| Источник | Что искать |
|---|---|
| https://nnethercote.github.io/perf-book/ | The Rust Performance Book — канон целиком |
| https://bheisler.github.io/criterion.rs/book/ | корректные бенчмарки |
| https://doc.rust-lang.org/cargo/reference/profiles.html | все опции профилей сборки |
| https://github.com/flamegraph-rs/flamegraph | флеймграфы |
| https://docs.rs/dhat | heap-профилирование |
| https://github.com/rust-lang/portable-simd | std::simd (portable SIMD) |
| https://min-sized-rust.github.io/ (johnthagen/min-sized-rust) | минимизация размера бинарника |

## Смежные навыки пакета

Настройка бенчей/criterion → **testing**; профили и CI → **project-setup**; unsafe в горячих местах → **unsafe-ffi**; блокировки в async → **async-concurrency**.
