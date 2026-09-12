# Архитектурный контекст (epic-context)

Собран: 2026-08-16T18:42:48.425404948+00:00

Источники:
- ARCHITECTURE-SPINE.md

<!-- источник: ARCHITECTURE-SPINE.md -->

# ARCHITECTURE-SPINE — bankcalc-fleet

Общие инварианты для ПАРАЛЛЕЛЬНОЙ реализации 10 независимых модулей библиотеки
`bankcalc` (расчётный контур: валидация, округление, комиссии, курсы,
идентификаторы, календарь, ретраи, НДС, лимиты, сводка).

Каждый модуль — свой исполнитель в своём worktree; исполнители не видят работу
друг друга. Стыки держатся на этом спайне: дословные Rule + приёмочные правила
CONSTRAINTS.yaml. Все модули — чистые функции: без print, без сети/файлов/БД.

## AD-1: Валидация сумм

- **Binds**: модуль `bankcalc.amount` предоставляет `validate_amount(value, limit) -> bool`.
- **Prevents**: параллельные сигнатуры валидации (validate_sum, check_limit…).
- **Rule**: в `bankcalc/amount.py` ровно одна публичная функция `validate_amount`; value < 0 → False, value > limit → False, иначе True.

## AD-2: Округление денег

- **Binds**: модуль `bankcalc.money` предоставляет `round_money(value) -> str`.
- **Prevents**: расхождение формата денежных сумм между модулями.
- **Rule**: в `bankcalc/money.py` ровно одна публичная функция `round_money`, возвращающая строку с ровно двумя знаками после точки (`round_money(10.5) == "10.50"`).

## AD-3: Комиссия в базисных пунктах

- **Binds**: модуль `bankcalc.fee` предоставляет `calc_fee(amount, bps) -> float`.
- **Prevents**: разнобой в единицах комиссии (проценты vs базисные пункты).
- **Rule**: в `bankcalc/fee.py` ровно одна публичная функция `calc_fee`; fee = amount × bps / 10000.

## AD-4: Конвертация по курсу

- **Binds**: модуль `bankcalc.rate` предоставляет `convert(amount, rate) -> float`.
- **Prevents**: расхождение направления курса (умножение vs деление).
- **Rule**: в `bankcalc/rate.py` ровно одна публичная функция `convert`; convert = amount × rate.

## AD-5: Идентификатор платежа

- **Binds**: модуль `bankcalc.ids` предоставляет `payment_id(prefix, seq) -> str`.
- **Prevents**: разнобой формата идентификаторов между продуктами.
- **Rule**: в `bankcalc/ids.py` ровно одна публичная функция `payment_id`, формат `{prefix}-{seq:06d}` (`payment_id("SBP", 42) == "SBP-000042"`).

## AD-6: Календарь выходных

- **Binds**: модуль `bankcalc.calendar` предоставляет `is_weekend(weekday) -> bool`.
- **Prevents**: разнобой нумерации дней недели между модулями.
- **Rule**: в `bankcalc/calendar.py` ровно одна публичная функция `is_weekend`; weekday — int 0=пн…6=вс; True iff weekday >= 5.

## AD-7: Экспоненциальный backoff

- **Binds**: модуль `bankcalc.retry` предоставляет `backoff(attempt, base) -> float`.
- **Prevents**: разнобой стратегий повторов интеграций.
- **Rule**: в `bankcalc/retry.py` ровно одна публичная функция `backoff`; backoff = base × 2^attempt.

## AD-8: НДС

- **Binds**: модуль `bankcalc.tax` предоставляет `vat(amount, rate) -> float`.
- **Prevents**: разнобой формулы НДС (сверху vs «в том числе»).
- **Rule**: в `bankcalc/tax.py` ровно одна публичная функция `vat`; vat = amount × rate / 100 (начисление сверху).

## AD-9: Ключ дневного лимита

- **Binds**: модуль `bankcalc.limit` предоставляет `daily_limit_key(client, day) -> str`.
- **Prevents**: разнобой ключей идемпотентности/лимитов между контурами.
- **Rule**: в `bankcalc/limit.py` ровно одна публичная функция `daily_limit_key`, формат `{client}:{day}`.

## AD-10: Сводка набора сумм

- **Binds**: модуль `bankcalc.summary` предоставляет `summarize(amounts) -> str`.
- **Prevents**: разнобой формата итоговых строк отчётности.
- **Rule**: в `bankcalc/summary.py` ровно одна публичная функция `summarize`, формат `count={n}; total={sum с 2 знаками}` (`summarize([10.5, 20.0]) == "count=2; total=30.50"`).

## Deferred

- Внутренняя реализация (приватные помощники, docstring-стиль) — на усмотрение
  исполнителя, на стыки не влияет.
- Локализация сообщений и валютные справочники — возврат при появлении
  второго контура потребления.

