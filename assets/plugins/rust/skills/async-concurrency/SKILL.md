---
name: async-concurrency
description: Асинхронный и многопоточный Rust по канонам Async Book, документации Tokio и The Book (гл. 16) - async/await, tokio (spawn, каналы, select!, таймауты, graceful shutdown), потоки и std::sync, Send/Sync, Arc/Mutex, атомики, rayon. Используй этот навык ВСЕГДА, когда в Rust-задаче встречаются async fn, .await, tokio, futures, каналы (mpsc, oneshot, broadcast, watch), многопоточность, распараллеливание, веб-серверы и сетевые клиенты (axum, reqwest, hyper), ошибки вида "future cannot be sent between threads" или "cannot block the current thread", вопросы про блокирующие операции в async, cancellation, дедлоки и выбор между потоками и async.
---

# Async и конкурентность в Rust

## Шаг 0: выбери правильную модель

| Задача | Инструмент |
|---|---|
| Много одновременных I/O-операций (сеть, тысячи соединений) | async + Tokio |
| CPU-bound параллелизм по данным (обработка коллекций) | rayon (`par_iter`) |
| Несколько долгоживущих фоновых работ | обычные `std::thread` + каналы |
| CPU-тяжёлое внутри async-приложения | `tokio::task::spawn_blocking` / отдельный rayon-пул |

Async — это не «быстрее», это дешёвле по памяти/переключениям при массовом I/O. Не тащи Tokio в программу, которая читает один файл. Правило из The Book: *message passing* по умолчанию («не общайтесь разделяя память — разделяйте память общаясь»), разделяемое состояние (`Arc<Mutex>`) — когда каналы неудобны.

## Основы async, которые все путают

- `async fn` возвращает **ленивую** Future: до `.await` или `spawn` ничего не выполняется.
- `.await` — точка, где задача может быть приостановлена и **отменена** (drop future = отмена; никакого исключения внутри не произойдёт — код после `.await` просто не выполнится).
- `tokio::spawn` требует `Future: Send + 'static` → внутри нельзя держать не-`Send` типы (`Rc`, `RefCell`) через `.await`, а данные захватывай по владению (`move` + `Arc`).
- Ошибка «future cannot be sent between threads safely» почти всегда означает: не-Send значение живёт через точку `.await`. Лечение: сузить scope (drop до await), заменить `Rc`→`Arc`, `RefCell`→`Mutex`, или `spawn_local`/`LocalSet` как осознанное исключение.
- Async-трейты стабильны (с 1.75): `async fn` в трейте работает, но такой трейт не dyn-compatible «из коробки» — для `Box<dyn Trait>` используй крейт `async-trait` или возвращай `Pin<Box<dyn Future>>` вручную.

## Правило №1 Tokio: не блокируй runtime

Worker-потоков мало (по числу ядер); одна блокирующая операция останавливает обслуживание сотен задач.

- ❌ `std::thread::sleep` → ✅ `tokio::time::sleep(...).await`
- ❌ `std::fs`, blocking DB-драйвер, тяжёлый цикл → ✅ `tokio::task::spawn_blocking(move || ...).await?` (или `tokio::fs`)
- ❌ `reqwest::blocking` внутри async → ✅ обычный async `reqwest`
- Ориентир из документации Tokio: между `.await` не проводи больше ~10–100 мкс CPU-времени; длиннее — `spawn_blocking` или `yield_now().await` в вычислительных циклах.

**Мьютексы**: короткая критическая секция без `.await` внутри — обычный `std::sync::Mutex` (быстрее). `tokio::sync::Mutex` — только если lock действительно надо держать через `.await`. Держать `std::sync::MutexGuard` через `.await` — не-Send + риск дедлока; паттерн лечения: `{ let mut g = m.lock().unwrap(); g.push(x); } // guard дропнут`, и только затем `.await`.

## Каналы Tokio: выбор

| Канал | Семантика | Типовое применение |
|---|---|---|
| `mpsc::channel(cap)` | многие→один, с backpressure | очередь работ к актору |
| `oneshot` | одно значение | ответ на запрос (request/response к актору) |
| `broadcast` | один→многие, каждый получает всё | события, pub/sub; отстающие получают `Lagged` |
| `watch` | один→многие, только последнее значение | конфиг, сигнал shutdown |

Bounded (`channel(cap)`) по умолчанию — backpressure бесплатно; `unbounded` — осознанное решение с риском OOM. Акторный паттерн: задача владеет состоянием (никаких Mutex), общение — `mpsc` + `oneshot` для ответов.

## select!, отмена и таймауты

```rust
tokio::select! {
    res = do_work() => handle(res?),
    _ = shutdown_rx.changed() => { cleanup().await; return Ok(()); }
}
```

- `select!` **дропает** невыбранные ветки → futures в них должны быть *cancellation-safe*. Из доков Tokio: `recv()` каналов — safe; `read_exact`/`write_all` и многие «составные» операции — НЕ safe (теряют частично считанное). Если future нельзя терять — сначала `let fut = ...; tokio::pin!(fut);` и селекти `&mut fut` в цикле.
- Таймаут: `tokio::time::timeout(dur, fut).await` — помни, что по таймауту future дропается (отменяется).
- Graceful shutdown (канон — tokio.rs/tokio/topics/shutdown): сигнал через `watch`/`CancellationToken` (tokio-util) + ожидание задач через `TaskTracker`/`JoinSet`; слушай `tokio::signal::ctrl_c()`.

## Структурированный запуск задач

- `JoinSet` — группа задач одного типа: `set.spawn(...)`, `while let Some(res) = set.join_next().await`; drop JoinSet отменяет всё.
- `JoinHandle` не «детачит» задачу при drop (задача продолжит выполняться) — но результат потеряешь; `handle.abort()` для отмены.
- Паника в задаче не роняет процесс — проверяй `JoinError::is_panic()` при `join`.
- Параллельное выполнение без spawn: `tokio::join!(a, b)` (конкурентно на одной задаче), `try_join!` (до первой ошибки); для коллекций — `futures::stream::iter(items).map(work).buffer_unordered(N)` — конкурентность с лимитом N; семафор `tokio::sync::Semaphore` для глобальных лимитов.

## Потоки и std::sync (без async)

- `std::thread::scope` — заимствование локальных данных потоками без `'static` и `Arc`.
- `Arc<Mutex<T>>`/`RwLock` — классика; lock возвращает `Result` из-за *poisoning* (паника держателя): `.lock().unwrap()` приемлем, осознанная обработка — `unwrap_or_else(|e| e.into_inner())`.
- Атомики: счётчики/флаги. Ordering: не изобретай — `Relaxed` для независимых счётчиков, `Acquire/Release` для передачи данных «флагом», `SeqCst` если не уверен (и прокомментируй). Сложнее — сверься с Rustonomicon/std docs, не по памяти.
- `Send` = можно передать в другой поток; `Sync` = `&T` можно шарить (T Sync ⇔ &T Send). Автовыводятся; ручной `unsafe impl Send` — территория навыка unsafe-ffi.

## Типовые пифоллы

Разбор с примерами — `references/pitfalls.md` (читай при отладке зависаний, дедлоков, «почему ничего не выполняется», при ревью async-кода): не-awaited futures, блокировки через await, cancellation-unsafe select-циклы, дедлоки двух мьютексов, `block_on` внутри runtime, испарившиеся задачи, `Stream`-нюансы.

## Первоисточники

| Источник | Что искать |
|---|---|
| https://rust-lang.github.io/async-book/ | модель async/await, pinning, executors |
| https://tokio.rs/tokio/tutorial | канонический туториал Tokio (spawn, каналы, select) |
| https://docs.rs/tokio | точная семантика API, cancellation safety каждого метода |
| https://tokio.rs/tokio/topics/shutdown | graceful shutdown |
| https://ryhl.io/blog/actors-with-tokio/ и /async-what-is-blocking/ | акторы; что считать блокирующим (статьи мейнтейнера Tokio) |
| https://doc.rust-lang.org/book/ch16-00-concurrency.html | потоки, каналы, Send/Sync |
| https://marabos.nl/atomics/ | «Rust Atomics and Locks» (Mara Bos) — атомики и примитивы |

## Смежные навыки пакета

Тесты async-кода (`#[tokio::test]`, `time::pause`) → **testing**; ручной `unsafe impl Send/Sync` → **unsafe-ffi**; профилирование → **performance**.
