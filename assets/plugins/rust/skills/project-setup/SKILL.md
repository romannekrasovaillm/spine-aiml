---
name: project-setup
description: Создание и организация Rust-проектов по канонам Cargo Book и Rust API Guidelines - структура крейта и workspace, Cargo.toml (features, profiles, lints, MSRV), rust-toolchain.toml, CI (fmt, clippy, tests, cargo-deny, semver-checks), документация и публикация на crates.io. Используй этот навык ВСЕГДА, когда нужно создать новый Rust-проект/библиотеку/CLI с нуля, настроить или отрефакторить Cargo.toml или workspace, добавить CI/GitHub Actions для Rust, настроить линты и MSRV, подготовить крейт к релизу или публикации, а также при вопросах "как организовать модули/крейты", "как настроить features", "как версионировать по semver".
---

# Организация Rust-проекта

Навык о методологии: как заложить структуру, конфигурацию и процессы, чтобы проект масштабировался и не копил технический долг. Канон — The Cargo Book и практика rust-lang / экосистемных крейтов (tokio, serde).

## Старт нового проекта

```bash
cargo new myapp            # бинарник
cargo new mylib --lib      # библиотека
```

Сразу после генерации приведи `Cargo.toml` к виду:

```toml
[package]
name = "myapp"
version = "0.1.0"
edition = "2024"
rust-version = "1.85"        # MSRV — минимальная поддерживаемая версия; проверяй cargo-msrv
description = "One-line description"       # обязательны для публикации (C-METADATA)
license = "MIT OR Apache-2.0"              # дефолт экосистемы Rust
repository = "https://github.com/me/myapp"

[lints.rust]
# unsafe_code = "forbid"     # включи, если unsafe не нужен

[lints.clippy]
all = { level = "warn", priority = -1 }
pedantic = { level = "warn", priority = -1 }
module_name_repetitions = "allow"          # пример точечного отключения pedantic-линта

[profile.release]
lto = "thin"                 # для максимума: lto = true, codegen-units = 1
```

`Cargo.lock` коммить всегда (и для библиотек тоже — так CI воспроизводим; в реестр он не публикуется).

## Структура: бинарник, библиотека, workspace

**Правило тонкого main**: даже у CLI логика живёт в `src/lib.rs` (тестируемо, переиспользуемо), `src/main.rs` — 10–30 строк: парсинг аргументов → вызов lib → маппинг ошибки в exit code.

```
myapp/
├── Cargo.toml
├── src/
│   ├── main.rs          # тонкий: только запуск
│   ├── lib.rs           # pub use ключевых типов, //! crate docs
│   ├── config.rs        # модуль = файл
│   └── config/          # подмодули config'а (без mod.rs — стиль 2018+)
│       └── parser.rs
├── tests/               # интеграционные тесты (каждый файл — отдельный крейт)
├── benches/             # бенчмарки (criterion)
└── examples/            # запускаемые примеры: cargo run --example demo
```

**Когда переходить на workspace**: несколько связанных крейтов (core + cli + macros), долгая компиляция, желание изолировать зависимости. Разбиение на крейты — это ещё и граница приватности и единица инкрементальной компиляции.

```toml
# корневой Cargo.toml (virtual manifest)
[workspace]
resolver = "3"                       # соответствует edition 2024
members = ["crates/*"]

[workspace.package]                  # общие метаданные
edition = "2024"
license = "MIT OR Apache-2.0"
rust-version = "1.85"

[workspace.dependencies]             # единые версии зависимостей
serde = { version = "1", features = ["derive"] }
tokio = { version = "1", features = ["full"] }

[workspace.lints.clippy]
all = { level = "warn", priority = -1 }
```

В члене workspace: `serde.workspace = true`, `edition.workspace = true`, `[lints] workspace = true`. Так версии и политика линтов не расползаются.

## Features: правила безопасности

- Фичи **аддитивны**: включение фичи не должно ломать или менять поведение существующего кода — любые сочетания фич у разных зависимых крейтов объединяются.
- Не делай взаимоисключающих фич; выбор реализации — через типы/generics, а не `#[cfg]`.
- Опциональная зависимость + фича: `serde = { version = "1", optional = true }` → фича `serde = ["dep:serde"]` (синтаксис `dep:` скрывает саму зависимость как фичу).
- Минимальный `default = []` у библиотек, широкий у приложений. Проверяй сборку комбинаций: `cargo hack check --feature-powerset` (крейт cargo-hack).
- Документируй фичи в crate docs и включай на docs.rs всё:
  ```toml
  [package.metadata.docs.rs]
  all-features = true
  ```

## Toolchain и MSRV

- `rust-toolchain.toml` в корне фиксирует версию для всей команды/CI:
  ```toml
  [toolchain]
  channel = "1.87"          # или "stable"
  components = ["rustfmt", "clippy"]
  ```
- MSRV-политика: указывай `rust-version` в Cargo.toml (cargo сам не даст собраться старее и учитывает при резолвинге), проверяй в CI отдельной джобой, повышай осознанно (для библиотек повышение MSRV — минорный, но чувствительный для пользователей шаг; фиксируй в CHANGELOG).

## CI: обязательный набор проверок

Полные YAML-шаблоны GitHub Actions и релизный процесс — в `references/ci-and-release.md` (читай при настройке CI или подготовке релиза). Минимальный джентльменский набор, который должен быть в любом проекте:

```bash
cargo fmt --all -- --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-features
cargo doc --no-deps          # RUSTDOCFLAGS="-D warnings" — ловить битые ссылки
```

Плюс по мере зрелости: `cargo deny check` (лицензии/уязвимости/дубликаты — конфиг deny.toml), `cargo audit` (RustSec advisory DB), `cargo semver-checks` (для библиотек — ловит непреднамеренные breaking changes перед релизом), MSRV-джоба, `cargo nextest run` вместо `cargo test` для скорости.

## Semver: что считается breaking change

Канон — глава SemVer Compatibility в Cargo Book (https://doc.rust-lang.org/cargo/reference/semver.html). Неочевидные breaking changes, которые чаще всего пропускают:

- добавление поля в публичную структуру без `#[non_exhaustive]` / варианта в enum;
- потеря auto-traits (`Send`/`Sync`) у публичного типа — например, добавил `Rc` внутрь;
- ужесточение bounds у generic;
- повышение версии публично видимой зависимости (тип из неё в твоём API);
- добавление метода в трейт без default-реализации.

До 1.0 minor играет роль major (`0.3.x → 0.4.0` — breaking). `cargo semver-checks` автоматизирует значительную часть проверок.

## Документация проекта

- `README.md` — синхронизируй с crate docs через `#![doc = include_str!("../README.md")]` (примеры из README станут doctests).
- CHANGELOG.md в формате Keep a Changelog; заполняется по мере PR, не перед релизом.
- `cargo doc --open` — регулярно смотри на API глазами пользователя.

## Первоисточники

| Источник | Что искать |
|---|---|
| https://doc.rust-lang.org/cargo/ | The Cargo Book: manifest, features, profiles, workspaces |
| https://doc.rust-lang.org/cargo/reference/semver.html | точные правила semver-совместимости |
| https://rust-lang.github.io/api-guidelines/ | C-METADATA, C-STABLE и пр. |
| https://doc.rust-lang.org/edition-guide/ | редакции и `cargo fix --edition` |
| https://rust-lang.github.io/rustup/ | toolchains, компоненты, override'ы |
| https://embarkstudios.github.io/cargo-deny/ | конфигурация cargo-deny |
| https://nexte.st/ | cargo-nextest |

## Смежные навыки пакета

Код и API → **idiomatic-code**; тесты в CI → **testing**; профили для производительности → **performance**.
