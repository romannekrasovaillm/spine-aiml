# Spine-BE SDK v1

Тонкие клиенты поверх headless CLI `arch-ml` для встраивания Spine-BE в
продукты и процессы команд: CI/CD-пайплайны, pre-commit гейты, чат-боты,
кодовые агенты, внутренние сервисы банка.

SDK не содержит логики харнесса — он запускает процесс `arch-ml` (без shell),
читает stdout/stderr/exit code и разбирает JSON по **контракту v1**
([CONTRACT.md](CONTRACT.md)). Контракт — единственный источник истины;
все три SDK реализуют одну и ту же форму API и одни и те же ошибки.

## Языки

| SDK | Каталог | Зависимости | Тесты |
|-----|---------|-------------|-------|
| Python | [sdk/python](python/) | только stdlib (Python ≥ 3.10) | `python3 -m pytest tests/ -v` — 46 зелёных |
| Rust | [sdk/rust](rust/) | `serde` + `serde_json` (MSRV 1.85) | `cargo test` — 34 зелёных, clippy чист |
| Java | [sdk/java](java/) | только JDK 21 (ноль внешних) | `./build.sh && ./test.sh` — 118 зелёных |

## Что умеет клиент (одинаково на всех языках)

- `run(prompt, …)` — headless-вопрос агенту (`arch-ml run -q`): ответ текстом,
  exit-контракт для скриптов.
- `control_check(repo, constraints?)` — fitness-контроль репозитория:
  типизированный отчёт `FitnessReport` (правила, находки с файлом/строкой/
  severity). `passed=false` — данные, а не исключение.
- `archify_validate / archify_deliver / archify_compare` — диаграммы Archify:
  receipt с `ok`, 9 artifact checks, SHA-256 спецификации/артефакта,
  машинная дельта версий (added/removed/changed/rerouted).

Бинарь разрешается из `SPINE_BE_BIN` → `arch-ml` в `PATH`. Клиентский таймаут
убивает зависший процесс. Ошибки едины (§4 контракта): `BinaryNotFound`,
`Timeout`, `ProcessFailed`, `ContractViolation`.

## Типовые сценарии встраивания

- **CI-гейт репозитория команды**: `control_check` на PR — красный отчёт
  ломает пайплайн с точной диагностикой (файл:строка:правило).
- **Регламент выпуска документации**: `archify_deliver` + `archify_compare`
  на каждую версию диаграммы — SHA-256 receipt и diff для комитета.
- **Кодовый агент (Claude Code и др.)**: вызывает SDK/CLI как инструмент —
  архитектурные ответы и гейты без выхода из среды разработки.

## Ограничения v1

- Нужен установленный бинарь `arch-ml` (SDK — не самодостаточная библиотека).
- `run()` не стримит; stdin-режим (`-`) не задействован.
- LLM-прогоны в автотестах не выполняются (стоимость/время) — покрыты
  построением argv на фейк-бинарях.
