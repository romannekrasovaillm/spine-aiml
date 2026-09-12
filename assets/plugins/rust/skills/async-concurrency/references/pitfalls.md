# Пифоллы async и конкурентности: диагностика и лечение

## 1. Future создана, но не запущена

```rust
let fut = send_email(user);      // ничего не происходит!
do_other_things();
// fut дропнута — письмо не отправлено, и даже warning'а может не быть
```
Симптом: «код не выполняется», clippy: `unused_must_use`/`let_underscore_future`. Лечение: `.await`, `tokio::spawn(fut)`, или `join!` для конкурентности.

Смежное: последовательные `.await` в цикле — это НЕ конкурентность:
```rust
for u in users { send_email(u).await?; }                 // последовательно
// конкурентно, с лимитом 10:
futures::stream::iter(users)
    .map(|u| send_email(u))
    .buffer_unordered(10)
    .try_collect::<Vec<_>>()
    .await?;
```

## 2. Блокирующий вызов в async-контексте

Симптомы: latency скачет, при малом числе ядер всё «замирает», tokio-console показывает busy-задачи. Частые виновники: `std::fs::*`, `std::net`, `std::thread::sleep`, синхронные клиенты БД/HTTP, `zip`/криптография/сериализация больших объёмов, `Command::output()` без `tokio::process`.

Лечение: `spawn_blocking`, `tokio::fs`/`tokio::process`, async-драйверы. Диагностический инструмент: `tokio-console` (https://github.com/tokio-rs/console) и `RUSTFLAGS="--cfg tokio_unstable"`.

## 3. MutexGuard через .await

```rust
let mut data = state.lock().unwrap();
fetch_more().await;                  // ← не компилируется в spawn (guard не Send),
data.push(x);                        //   а в LocalSet — компилируется и дедлочит
```
Лечение — сузить критическую секцию:
```rust
{ state.lock().unwrap().push(x); }   // guard дропнут до await
fetch_more().await;
```
Если удержание через await семантически необходимо — `tokio::sync::Mutex`. Но сперва подумай про актора: единоличный владелец состояния + mpsc убирает мьютекс вовсе.

## 4. Дедлок двух мьютексов / повторный lock

- Классика: поток A берёт m1→m2, поток B берёт m2→m1. Правило: глобальный порядок захвата или один мьютекс на агрегат.
- `RwLock`: read-lock в той же задаче, потом write-lock (или рекурсивный read при политике writer-preferring) — дедлок.
- Диагностика зависаний: `tokio-console`, дампы стеков (`gdb`/`lldb`), для std-потоков — `parking_lot` с фичей deadlock_detection.

## 5. Cancellation: тихая потеря работы

- `timeout(dur, save_to_db(x)).await` — по таймауту `save_to_db` отменяется на ближайшем await; запись может не случиться, rollback-логика после await не выполнится.
- `select!` в цикле с пересозданием future:
  ```rust
  loop {
      tokio::select! {
          n = socket.read_exact(&mut buf) => { ... }   // ❌ при выборе другой ветки
          msg = rx.recv() => { ... }                   //   частично считанное ТЕРЯЕТСЯ
      }
  }
  ```
  Лечение: держи future снаружи цикла (`tokio::pin!`) либо используй cancellation-safe операции (см. docs.rs/tokio — у методов явно указана Cancel safety).
- Критические секции, которые нельзя отменять: вынеси в отдельную `tokio::spawn`-задачу (spawn не отменяется дропом хэндла) и дождись `JoinHandle`.
- Очистка при отмене: паттерн guard с `Drop` (Drop выполняется синхронно; async-cleanup из Drop не сделать — планируй shutdown явно).

## 6. block_on внутри runtime

`Handle::block_on`/`Runtime::block_on`/`futures::executor::block_on` из async-контекста → паника «Cannot start a runtime from within a runtime» или дедлок. Если синхронный код должен вызвать async: `Handle::current().block_on` только из НЕ-worker-потока (например, внутри `spawn_blocking`), либо `tokio::task::block_in_place` (осознанно, только multi-thread runtime).

## 7. Задачи-сироты и проглоченные паники

- `tokio::spawn` без сохранения `JoinHandle`: паника в задаче уйдёт в лог `JoinError` только если хэндл кто-то await'ит; иначе — молча. Для системных задач: `JoinSet`/`TaskTracker` + централизованная обработка `join_next`.
- Тест «прошёл», а задача жива и пишет в стейт следующего теста → флаки. В тестах доводи shutdown до конца (`tracker.wait().await`).

## 8. Стримы и итерация

- `Stream` не итерируется `for`: `while let Some(item) = stream.next().await` (нужен `use futures::StreamExt` или `tokio_stream::StreamExt`).
- Комбинатор `then`/`map` + `buffer_unordered` меняет порядок результатов; нужен порядок — `buffered(N)`.
- Бесконечный стрим + `collect` = зависание.

## 9. Время в тестах и в проде

- В тестах не `sleep` реального времени: `#[tokio::test(start_paused = true)]` + `tokio::time::advance(...)` — мгновенно и детерминированно.
- `Instant` монотонен, `SystemTime` может прыгать назад — тайминги меряй `Instant`.

## 10. Микро-чеклист ревью async-кода

- [ ] Нет блокирующих вызовов на worker'ах (fs/net/sleep/CPU-циклы)
- [ ] Guard'ы std-мьютексов не живут через `.await`
- [ ] Все spawn-задачи кем-то join'ятся или явно «fire-and-forget» с комментарием
- [ ] Ветки `select!` cancellation-safe (или future запинена снаружи цикла)
- [ ] Каналы bounded; переполнение обработано (`send().await` vs `try_send`)
- [ ] Shutdown-путь существует и тестируется
- [ ] Таймауты на все внешние I/O
