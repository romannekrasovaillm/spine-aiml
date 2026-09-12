# ADR-002. Transactional Outbox для надёжной публикации событий

- Date: 2026-08-15
- Status: Accepted

## Context

Каждый сервис (Payment Orchestrator, Authorization, Ledger) обновляет БД и одновременно публикует событие в Kafka (статус платежа изменился → топик `payments.status`). Запись в БД и публикация в Kafka — не атомарны (dual-write problem): упали между — расхождение (в БД платёж проведён, в Kafka нет → контуры рассинхронизированы, уведомления не ушли, клиринг не запустился). В банке это расхождение = финансовый риск и операционная ручная сверка.

## Decision

**Transactional Outbox** с CDC-ретрансляцией (Debezium).

- В одной локальной транзакции: (а) бизнес-изменение, (б) запись события в таблицу `outbox(id, aggregate_id, event_type, payload, created_at, published_at)`.
- Ретрансляция: **Debezium CDC** (чтение WAL PostgreSQL), не polling publisher. Нулевая задержка, нет нагрузки на запросный путь.
- Событие — полное самодостаточное тело (payment_id, status, amount, currency, timestamp, actor). Запрещён конверт «что-то изменилось, иди дочитай» — гонки читателей.
- Публикация at-least-once ⇒ потребитель ОБЯЗАН быть идемпотентным (AD-5, ADR-005).
- Ключ партиции = aggregate_id (payment_id). Глобальный порядок не гарантируется, порядок в рамках платежа — да.
- Мониторинг: `outbox_lag` (количество неотправленных записей) — метрика с алертом при росте > 100.
- Retention: отправленные записи архивируются (аудит), не удаляются мгновенно. TTL: 90 дней hot, затем cold storage.

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **Polling publisher** (SELECT FOR UPDATE SKIP LOCKED) | Простота, нет CDC-инфраструктуры. | Нагрузка на БД. Задержка = период опроса (1–5s). При росте TPS — деградация. |
| **Dual write** (БД, потом Kafka) | Проще всего. | Классическая причина расхождений. Запрещено. |
| **Kafka transactional producer** (exactly-once Kafka) | Гарантии внутри Kafka. | Не решает атомарность БД↔Kafka. Сквозной exactly-once не существует. |

## Consequences

### Positive

- Атомарность «БД + событие» гарантирована локальной транзакцией.
- Нулевая задержка публикации (CDC).
- Нет нагрузки на запросный путь (в отличие от polling).
- Самодостаточные события — нет гонок читателей.

### Negative

- Инфраструктурная сложность Debezium (разворачивание, мониторинг, WAL-конфигурация PostgreSQL).
- At-least-once доставка — дубли. Митигация: идемпотентные потребители (AD-5).
- Ретранслятор — новый компонент, который может «застрять». Митигация: метрика outbox_lag с алертом.

## Reversibility

**Reversible.** Переход на polling publisher — замена ретранслятора, схема outbox не меняется. Обратный переход — аналогично.

## References

- Spine: AD-2
- Skills: `transactional-outbox`, `idempotent-consumer`
- Источник: https://microservices.io/patterns/data/transactional-outbox.html
