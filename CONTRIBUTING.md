# CONTRIBUTING — как внести вклад в Spine AI/ML Edition

Спасибо за интерес! Проект живёт в дисциплине артефактов: решения — в ADR,
инварианты — в спайне, проверки — в fitness-правилах. Ниже — всё, что нужно
для принятого PR.

## Сборка и тесты

```bash
cargo build                  # быстрая проверка: cargo check
cargo test                   # вся свита (live-LLM тесты помечены #[ignore])
cargo fmt --all              # форматирование обязательно
cargo clippy --all-targets -- -D warnings   # строгий режим, новое предупреждение = красная сборка
```

MSRV — Rust 1.85 (`rust-version` в `Cargo.toml`), edition 2024.

## Проверки перед PR (локальный гейт)

Репозиторий гейтится собственными механиками (dogfood, AD-9):

```bash
cargo build
./target/debug/arch-ml control spine ARCHITECTURE-SPINE.md
./target/debug/arch-ml control check . --constraints CONSTRAINTS.yaml
./target/debug/arch-ml adr registry . --strict
./target/debug/arch-ml eval run
```

CI (`.github/workflows/ci.yml` + `sdk.yml`) прогоняет: fmt, clippy, cargo
test, MSRV 1.85, cargo audit, supply-chain (SBOM + SHA256), dogfood,
eval-suite, тесты SDK (Python 3.10/3.12, Rust, Java 21) и плагинов.

## Конвенции

- **Коммиты** — Conventional Commits: `feat(scope): …`, `fix(…):`, `docs(…):`,
  `ci(…):`, `chore(…):`, `perf(…):`, `test(…):`. Текст — на русском.
- **Документация** — на русском; код и идентификаторы — на английском;
  doc-комментарии (`///`) — на русском.
- **Без `unsafe`**, без `unwrap`/`expect` вне тестов; ошибки — `HarnessError`
  (thiserror) в библиотеке, `anyhow` с `.context()` на CLI-краю.
- **Без секретов и персональных путей** в репозитории: ключи — только через
  `api_key_env`/`api_key_file`; личные каталоги — в пользовательском конфиге.
  CI-сканы (personal paths, identity guard) ломают сборку.
- **Тесты детерминированы**: `tempfile`, без сети, без реального `$HOME`.
- Остальное — `AGENTS.md` (карта модулей, как расширять) и
  `docs/RUST_CONVENTIONS.md`.

## Когда нужен ADR

Изменение затрагивает API/data-контракт, security boundary, новый
компонент/хранилище/вендора, необратимую миграцию — сначала ADR
(`docs/adr/`, шаблон: решение, альтернативы, негативные последствия,
обратимость), потом реализация. Маршрут значимости изменения:
`arch-ml control score --trigger …` (Fast/Standard/Critical) или
`--from-diff` — вычисление по факту диффа.

## Тесты для изменения

Новый инструмент/команда/правило — с тестами (см. `tests/` и модульные
`#[cfg(test)]`). Пользовательски видимое поведение — обнови документацию
(`docs/`, README — если меняется быстрый старт или состав команд).
