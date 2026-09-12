# Обработка ошибок в Rust: методология

Основано на: The Rust Book (гл. 9), Rust API Guidelines (C-GOOD-ERR), документации `std::error::Error`, README крейтов `thiserror` и `anyhow`, Effective Rust (Item 4).

## Главное разделение: библиотека vs приложение

| | Библиотека (lib) | Приложение (bin) |
|---|---|---|
| Тип ошибки | Свой enum через `thiserror` | `anyhow::Error` (или `eyre`) |
| Цель | Дать вызывающему **матчиться** на варианты | Дать человеку **читаемый отчёт** с контекстом |
| Зависимости | `thiserror` (не тянет ничего в публичный API) | `anyhow` |
| main | — | `fn main() -> anyhow::Result<()>` |

Библиотека, возвращающая `anyhow::Error`, лишает пользователя возможности программно различать ошибки — это ошибка дизайна.

## Библиотечный тип ошибки: шаблон

```rust
use std::path::PathBuf;
use thiserror::Error;

#[derive(Debug, Error)]
#[non_exhaustive]                    // позволяет добавлять варианты без breaking change
pub enum ConfigError {
    #[error("config file {path} not found")]
    NotFound { path: PathBuf },

    #[error("failed to read {path}")]
    Io {
        path: PathBuf,
        #[source]                    // сохраняем причину — цепочка ошибок
        source: std::io::Error,
    },

    #[error("invalid value for key `{key}`: {reason}")]
    InvalidValue { key: String, reason: String },
}

pub type Result<T, E = ConfigError> = std::result::Result<T, E>;
```

Требования к хорошему типу ошибки (C-GOOD-ERR):
- реализует `std::error::Error` + `Debug` + `Display` (thiserror делает это за тебя);
- `Display` — строчная буква, без точки в конце, без дублирования source (цепочку печатает вызывающий);
- сохраняет причину через `#[source]` или `#[from]`, не «плющит» её в строку;
- `Send + Sync + 'static` — иначе ошибку нельзя переносить между потоками и заворачивать в `anyhow`.

`#[from]` генерирует `impl From<Io> for ConfigError` и позволяет писать просто `?`. Используй его, только если конверсия однозначна; когда нужен контекст (какой файл?), пиши `map_err` с явным конструктором варианта, как в `Io { path, source }` выше.

## Приложение: anyhow + контекст

```rust
use anyhow::{Context, Result};

fn load(path: &std::path::Path) -> Result<Config> {
    let raw = std::fs::read_to_string(path)
        .with_context(|| format!("reading config {}", path.display()))?;
    let cfg: Config = toml::from_str(&raw)
        .with_context(|| format!("parsing config {}", path.display()))?;
    Ok(cfg)
}

fn main() -> Result<()> {
    let cfg = load("app.toml".as_ref())?;
    // {:#} печатает всю цепочку: parsing config app.toml: expected `]` at line 3
    Ok(())
}
```

Правило: **каждый** `?` в приложении сопровождай `.context()`/`.with_context()`, отвечающим на вопрос «что мы пытались сделать». Без этого итоговое сообщение — голое `No such file or directory` без указания файла.

## `?`, конверсии и Option

- `?` работает в функциях, возвращающих `Result`/`Option`, и автоматически применяет `From::from` к ошибке.
- `Option → Result`: `opt.ok_or(MyError::Missing)` или `ok_or_else(|| ...)` (lazy — если конструирование ошибки не бесплатно).
- `Result → Option`: `.ok()` — только когда ошибка реально безразлична; иначе логируй перед отбрасыванием.
- Комбинируй, не вложенно матчись: `and_then`, `map_err`, `unwrap_or_else`, `transpose` для `Option<Result<T>>`.

## Паники: когда допустимы

Паника = баг, а не ошибка. Допустимо паниковать:
- при нарушении внутреннего инварианта, которое означает баг в *этой* программе (`unreachable!()`, `assert!` с сообщением);
- в тестах и примерах (`unwrap` ок);
- в прототипах — но помечай `// TODO: proper error handling`.

Правила:
- `expect("...")` вместо `unwrap()`: сообщение формулируй как описание инварианта — «why this cannot fail», напр. `expect("regex is valid: checked at compile time")`.
- Публичная функция, способная паниковать на каком-то входе, обязана иметь секцию `# Panics` в rustdoc — либо лучше верни `Result`/предложи try_-вариант (`C-DEBUG-ASSERT`: в горячем коде — `debug_assert!`).
- Индексация `v[i]` паникует; в коде, где вход не доверен, используй `v.get(i)`.
- Не используй `panic!` для управления потоком выполнения и не полагайся на `catch_unwind` как на try/catch (при `panic = "abort"` он не работает).

## Ошибки в тестах

Тесты могут возвращать `Result`: `fn test_x() -> anyhow::Result<()>` — позволяет пользоваться `?` вместо каскада unwrap. Сообщения ассертов: `assert_eq!(got, want, "case: {name}")`.

## Быстрый чеклист ревью

- [ ] В lib нет `unwrap`/`expect` на путях с недоверенным вводом
- [ ] Тип ошибки реализует Error и `Send + Sync + 'static`
- [ ] Причины сохранены (`#[source]`), а не превращены в строки
- [ ] `Display` без точки, с маленькой буквы, без дублирования source
- [ ] В приложении у каждого `?` есть context
- [ ] enum ошибки помечен `#[non_exhaustive]`, если API публичный и будет расти
- [ ] Панигующие публичные функции документированы `# Panics`

## Первоисточники

- https://doc.rust-lang.org/book/ch09-00-error-handling.html
- https://rust-lang.github.io/api-guidelines/interoperability.html#c-good-err
- https://docs.rs/thiserror / https://docs.rs/anyhow
- https://doc.rust-lang.org/std/error/index.html — устройство `Error::source` и цепочек
