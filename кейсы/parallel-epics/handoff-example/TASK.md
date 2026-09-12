# Задача для кодового харнесса

Реализовать модуль spinecalc.amount: пакет spinecalc с функцией validate_amount(value: float, limit: float) -> bool. Семантика: value < 0 → False (отрицательные суммы запрещены); value > limit → False (превышение лимита); иначе True. Соблюдать AD-1 из ARCHITECTURE-SPINE.md: ровно одна публичная функция validate_amount, без print, без доступа к сети/файлам. Написать tests/test_amount.py (минимум 4 теста: отрицательная сумма, превышение лимита, в пределах лимита, граничное значение равно лимиту). Прогнать pytest. JSON-контракт результата: {"status": "complete", "assumptions": [...], "open_questions": [], "conflicts_with_prior_decisions": []}.

## Контракт результата

Финальный ответ обязан завершаться JSON-объектом (после него — ни символа):

```json
{"status": "complete|partial|blocked", "assumptions": [], "open_questions": [], "conflicts_with_prior_decisions": []}
```

- `status`: `complete` — выполнено полностью; `partial` — частично; `blocked` — заблокировано.
- `assumptions`: допущения, принятые при реализации.
- `open_questions`: вопросы к архитектору.
- `conflicts_with_prior_decisions`: расхождения с принятыми ранее решениями (ADR, spine).

Архитектурный контекст — `ARCHITECTURE.md`, ограничения — `CONSTRAINTS.yaml`, рубрика приёмки — `RUBRIC.yaml` (при наличии).
