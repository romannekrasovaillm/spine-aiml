# Документация Spine-BE — карта

Spine Banking Edition — доменный харнесс solution-архитектора: один бинарь
`arch-ml` (TUI + headless CLI + библиотека), контур архитектурного контроля,
детерминистичные диаграммы Archify, SDK для встраивания в продукты и процессы
команд. Этот файл — точка входа во всю техническую документацию.

Зоны репозитория: ядро (`src/`, `docs/`, `sdk/`) — MIT (`LICENSE`);
банковская надстройка `banking/` — proprietary (`LICENSE.banking`, ADR-013).

## С чего начать

| Я… | Мой путь |
|---|---|
| впервые запускаю Spine-BE | [getting_started.md](getting_started.md) — сборка → конфиг → doctor → первый гейт, диаграмма и вызов SDK |
| встраиваю Spine-BE в скрипты и CI (без SDK) | [headless.md](headless.md) — контракт stdout/stderr/exit, пайпы, гейты |
| встраиваю Spine-BE в продукт/CI команды | [sdk.md](sdk.md) → [sdk/CONTRACT.md](../sdk/CONTRACT.md) → примеры в `sdk/*/examples/` |
| хочу диаграммы как код (комитет, версии, дельта) | [archify.md](archify.md) → плагин `banking/plugins/ru-archify` |
| строю контур архитектурного контроля | [control.md](control.md) → [governance.md](governance.md) → `banking/library/` |
| подключаю модели и окружение | [models.md](models.md) → [mcp.md](mcp.md) → [web_kb.md](web_kb.md) |
| работаю в TUI | [tui.md](tui.md) — гид по интерфейсу → [slash_commands.md](slash_commands.md) → [tools.md](tools.md) |
| передаю задачу кодовому агенту (Claude Code и др.) | [handoff_walkthrough.md](handoff_walkthrough.md) → [harness_integrations.md](harness_integrations.md) |
| разрабатываю сам харнесс | [architecture.md](architecture.md) → [RUST_CONVENTIONS.md](RUST_CONVENTIONS.md) → `docs/adr/` |

## Справочники

| Документ | Содержание |
|---|---|
| [getting_started.md](getting_started.md) | Сквозной гайд: от сборки до первого гейта и диаграммы (все команды проверены живьём) |
| [tui.md](tui.md) | Гид по TUI: экран, статус-бар и индикатор контекста, пикеры, очередь, горячие клавиши, выбор поверхности |
| [tools.md](tools.md) | Все агентные инструменты: сигнатуры, контракты, CLI-эквиваленты |
| [slash_commands.md](slash_commands.md) | Слэш-команды TUI |
| [models.md](models.md) | Модельная матрица, провайдеры, `api_key_env`, thinking-режимы (ADR-012/021) |
| [mcp.md](mcp.md) | MCP-серверы: подключение, вызовы, server-mode (ADR-001/008) |
| [web_kb.md](web_kb.md) | Веб-поиск/фетч и локальная база знаний |
| [plugins_and_skills.md](plugins_and_skills.md) | Плагины и библиотека скиллов: структура, загрузка, доверие |
| [agents_md.md](agents_md.md) | Генерация AGENTS.md для репозиториев команд из архитектурных артефактов |

## Архитектурный контур

| Документ | Содержание |
|---|---|
| [control.md](control.md) | Архитектурный контроль: significance score, fitness functions (`control check`, в т.ч. `--json`), линтер спайна, сенсоры, ADR, гейты |
| [archify.md](archify.md) | Контур диаграмм Archify: 5 типов IR, validate/deliver/compare, quality-профили, вендоринг (ADR-027) |
| [governance.md](governance.md) | Политика автономии (R-уровни), evidence bundle, метрики, дельта-спеки |
| [openspec.md](openspec.md) | Адаптер OpenSpec: требования SHALL/MUST → покрытие через `covers:`, скелет CONSTRAINTS + SPINE.draft, archive-гейт |
| [corp-spine.md](corp-spine.md) | Корпоративный спайн: наследование CONSTRAINTS (`extends` + пины версий), deny_dependency, overrides через ADR, report |
| [rubrics_and_benchmarks.md](rubrics_and_benchmarks.md) | Якорные рубрики, калибровка судьи, архитектурные бенчмарки (ADR-004) |
| [evals.md](evals.md) | Регрессионные eval-сьюты конфигурации (continuous evals) |
| [fleet.md](fleet.md) | Worktree-фабрика и аудит флота копий (review/accept/drop) |
| [fleet_patterns.md](fleet_patterns.md) | Планы флота: паттерны оркестрации (fanout/pipeline/map_reduce/tournament/review_pair/ralph/walking_skeleton/dag), гейты узла, resume, масштаб (ADR-042) |

## Интеграции и встраивание

| Документ | Содержание |
|---|---|
| [headless.md](headless.md) | Headless-режим: процесс как протокол (stdout/stderr/exit), `run -q`, пайпы, Makefile/pre-commit, отладка (ADR-020) |
| [sdk.md](sdk.md) | SDK v1 (Python/Rust/Java): контракт, API, ошибки, примеры встраивания (ADR-028) |
| [handoff_walkthrough.md](handoff_walkthrough.md) | Пошаговая передача контекста кодовому харнессу со скриншотами |
| [harness_integrations.md](harness_integrations.md) | Интеграция кодовых харнессов: handoff-пакеты, harness_run |
| [cron_and_md_pipes.md](cron_and_md_pipes.md) | Планировщик md-задач и баш-пайпы поверх headless-режима |

## Внутреннее устройство

| Документ | Содержание |
|---|---|
| [architecture.md](architecture.md) | Архитектура харнесса: модули, потоки данных, границы + обзорная Archify-диаграмма ([diagrams/](diagrams/)), авторствованная самим Spine-BE |
| [SOURCE_BRIEF.md](SOURCE_BRIEF.md) | Дистиллят фреймворков-источников идей (что перенято и почему) |
| [RUST_CONVENTIONS.md](RUST_CONVENTIONS.md) | Обязательные Rust-конвенции проекта |
| [theseus_hardening.md](theseus_hardening.md) | Закалка по результатам разбора Theseus (шестая волна) |
| [failure_memory.md](failure_memory.md) | Failure-memory: «ошибся дважды → урок» |
| [adr/](adr/) | Архитектурные решения ADR-001…039 (37 документов, нумерация с пропусками; история и догмы продукта) |

## Банковская зона (`banking/`, proprietary)

- [banking/README.md](../banking/README.md) — карта зоны: пресеты (payments,
  compliance, bank-profile), библиотека правил, плагины, ADF, compliance-карты.
- Демо-пакеты: [demos/archify-adf](../banking/demos/archify-adf/) (TUI-сценарии
  с ADF), [demos/cli-from-claude-code](../banking/demos/cli-from-claude-code/)
  (headless CLI), [demos/sdk-embedding](../banking/demos/sdk-embedding/)
  (встраивание SDK).
- SDK и контракт: [sdk/README.md](../sdk/README.md),
  [sdk/CONTRACT.md](../sdk/CONTRACT.md) (источник истины машинного контракта).

## Правила документации

- Команды в гайдах — только проверенные живьём; LLM-выводы — из записанных
  эталонов (помечаются).
- Догфуд: документацию про Spine-BE пишет сам Spine-BE, диаграммы — встроенный
  Archify (IR в `docs/diagrams/*.json`, приёмка `arch-ml archify validate` →
  `deliver`; см. [archify.md](archify.md)).
- Без абсолютных путей и секретов: `<репо>`, `~`, плейсхолдеры endpoint'ов.
- Изменил поведение — обнови соответствующий документ и, при архитектурной
  значимости, напиши ADR (`arch-ml control adr`).
