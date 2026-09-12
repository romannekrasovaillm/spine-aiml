# Паттерны и антипаттерны Rust

Источники: Rust Design Patterns (https://rust-unofficial.github.io/patterns/), Effective Rust, The Book (гл. 17–18), Clippy.

## Дизайн типами

### Make invalid states unrepresentable

```rust
// Плохо: четыре поля, половина комбинаций бессмысленна
struct Connection { connected: bool, addr: Option<SocketAddr>, retries: u32, error: Option<String> }

// Хорошо: каждое состояние несёт только свои данные
enum Connection {
    Disconnected,
    Connecting { addr: SocketAddr, retries: u32 },
    Connected { addr: SocketAddr, stream: TcpStream },
    Failed { error: ConnectError },
}
```

### Newtype

```rust
pub struct UserId(u64);          // нельзя перепутать с OrderId(u64)
pub struct Meters(f64);          // единицы измерения в типах
struct Wrapper(Vec<String>);     // обход orphan rule: impl Display for Wrapper
```
Реализуй нужные трейты (`Deref` — НЕ для эмуляции наследования, см. антипаттерны; лучше явные `as_inner`/`From`).

### Typestate: протокол в типах

```rust
struct Request<S> { /* ... */ _state: PhantomData<S> }
struct Draft; struct Signed;

impl Request<Draft> {
    fn sign(self, key: &Key) -> Request<Signed> { /* ... */ }
}
impl Request<Signed> {
    fn send(self) -> Result<Response, SendError> { /* ... */ }   // send есть ТОЛЬКО у подписанных
}
```

### RAII-guard

Ресурс освобождается в `Drop`, доступ — только через guard (`MutexGuard` — эталон). Для отложенных действий: guard-структура с `Drop`, отменяемая через `std::mem::forget`/флаг.

## Идиомы повседневного кода

- **Итераторы**: `filter_map`, `flat_map`, `zip`, `enumerate`, `fold`, `sum`, `collect::<Result<Vec<_>, _>>()` (коллект Result'ов останавливается на первой ошибке), `partition`, `windows`/`chunks`. Итераторы ленивые — без потребителя (`collect`, `for`, `sum`) ничего не произойдёт (clippy ловит).
- **`let else`** для ранних выходов:
  ```rust
  let Some(user) = find_user(id) else { return Err(Error::NotFound { id }) };
  ```
- **`matches!`** для булевых проверок паттерна: `matches!(state, State::Ready | State::Idle)`.
- **`std::mem::take` / `replace`** — забрать значение из `&mut` без клона (поле `String`/`Vec` → `take` оставляет Default).
- **Деструктуризация в аргументах и `if let` chains** (2024): `if let Some(x) = a && x > 0 { ... }`.
- **`impl Trait` в аргументах** для простых случаев; именованные generics `<T: ...>`, когда параметр повторяется.
- **`#[derive(Default)] + struct update`**: `Config { port: 8080, ..Default::default() }`.
- **Числа**: явные конверсии `try_into()` вместо `as` для сужающих (clippy: `cast_possible_truncation`); `checked_*`/`saturating_*`/`wrapping_*` — выбери семантику переполнения осознанно (в release переполнение молча заворачивается, если не включён `overflow-checks`).
- **Строки**: конкатенация через `format!`; посимвольная работа — помни про UTF-8 (`chars()` — скаляры Unicode, не графемы; байтовая длина ≠ количество символов).

## Диспетчеризация: generics vs dyn

| Критерий | `impl Trait` / `<T: Trait>` | `dyn Trait` |
|---|---|---|
| Скорость вызова | статическая, инлайнится | vtable, косвенный вызов |
| Размер бинарника/компиляция | мономорфизация — растёт | компактно |
| Гетерогенные коллекции | нет | да: `Vec<Box<dyn Draw>>` |
| Требования к трейту | любой | dyn-compatible (без generic-методов, `Self: Sized` и т.п.) |

Если вариантов конечное известное число — enum + match часто лучше обоих (быстрее dyn, проще generics; clippy предлагает при `large_enum_variant` боксить большие варианты).

## Антипаттерны (Rust Design Patterns, ч. «Anti-patterns»)

- **`Deref`-полиморфизм**: реализовать `Deref<Target = Base>` для «наследования» — ломает ожидания (Deref — для умных указателей), методы разрешаются неявно и хрупко. Используй композицию + делегирующие методы или трейты.
- **Clone для borrow checker**: `let x = data.clone()` только чтобы код скомпилировался. Сначала: сузить scope заимствования, `{}`-блок, разбить структуру, `mem::take`, изменить порядок операций. Клон — когда семантически нужна копия.
- **`unwrap`/`expect` как стиль** — см. references/error-handling.md.
- **`#[allow(...)]` без комментария-обоснования** — глушение линта без причины; в 2024 предпочитай `#[expect(...)]` (упадёт предупреждением, когда линт перестанет срабатывать).
- **Булевы флаги в API**: `render(true, false)` нечитаемо → два enum'а (`Compact`/`Pretty`, `WithHeader`/`NoHeader`).
- **Строково-типизированный код**: `&str`-статусы/ключи вместо enum; `stringly-typed` API.
- **Глобальное изменяемое состояние**: `static mut` (в 2024 фактически запрещён) → `OnceLock`/`LazyLock`/`Mutex<T>` в static, а лучше — явная передача зависимостей.
- **Преждевременный `Arc<Mutex<T>>`**: сначала подумай про владение одним потоком + каналы (message passing, The Book гл. 16).
- **Игнорирование `must_use`**: `Result`, итераторы, guards — не бросай на пол; `let _ = ...` только осознанно.

## Организация кода

- Модули — по файлам (`src/parser.rs` + `src/parser/`), а не всё в `lib.rs`; `pub(crate)` для внутренностей; re-export ключевых типов в корне (`pub use`), чтобы пользователь не писал длинные пути.
- Трейты определяй у потребителя функциональности (где нужна абстракция), а не «на всякий случай» у каждой структуры.
- Функции > 50–70 строк или с 5+ аргументами — кандидаты на разбиение/параметр-структуру (clippy: `too_many_arguments`).
