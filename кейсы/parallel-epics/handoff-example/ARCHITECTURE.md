# Архитектурный контекст (epic-context)

Собран: 2026-08-16T10:33:01.822452622+00:00

Источники:
- ARCHITECTURE-SPINE.md

<!-- источник: ARCHITECTURE-SPINE.md -->

# ARCHITECTURE-SPINE — spine-parallel

Общие инварианты для параллельной реализации 3 независимых модулей библиотеки `spinecalc`.

## AD-1: Единый контракт валидации сумм

- **Binds**: все реализации валидатора обязаны предоставлять функцию `validate_amount(value: float, limit: float) -> bool` в модуле `spinecalc.amount`.
- **Prevents**: появление параллельных сигнатур валидации (validate_sum, check_limit и т.п.).
- **Rule**: в `spinecalc/amount.py` ровно одна публичная функция с именем `validate_amount`.

## AD-2: Единый формат логов

- **Binds**: все реализации логгера обязаны предоставлять функцию `format_log(level: str, msg: str) -> str` в модуле `spinecalc.logfmt`, формат строки `[LEVEL] msg`.
- **Prevents**: расхождение форматов лога между модулями.
- **Rule**: в `spinecalc/logfmt.py` ровно одна публичная функция `format_log`, возвращающая строку вида `[LEVEL] msg`.

## AD-3: Единый формат отчёта

- **Binds**: все реализации отчёта обязаны предоставлять функцию `build_report(amounts: list) -> str` в модуле `spinecalc.report`, строки по одной записи на сумму.
- **Prevents**: расхождение структуры отчёта между модулями.
- **Rule**: в `spinecalc/report.py` ровно одна публичная функция `build_report`.

## Deferred

- Внутренняя реализация (алгоритм, приватные функции) — на усмотрение исполнителя, не влияет на стыки.

