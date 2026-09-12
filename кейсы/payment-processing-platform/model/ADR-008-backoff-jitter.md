---
id: ADR-008
type: adr
title: "Стратегия ретраев с backoff и jitter"
status: "Accepted"
date: "2026-08-15"
implements: [AD-10]
affects: [CMP-002, CMP-006]
---

## Context

При сбое внешней системы (rail timeout, 5xx) ретраи необходимы для восстановления. Но ретраи «эгоистичны» (Amazon Builders' Library): каждый клиент ретраит независимо, создавая всплеск нагрузки на восстанавливающуюся систему. Ретраи на каждом уровне стека (Gateway → Orchestrator → Connector) умножают нагрузку экспоненциально. Фиксированная задержка → thundering herd: все ретраят одновременно. Результат: «ретраи добили зависимость», каскадный отказ.

## Decision

**Ретраи в одном слое + экспоненциальный backoff с джиттером + token bucket.**

### Один слой
- Ретраи — только на одном уровне стека. Решение: Rail Connector (ближайший к внешней системе).
- Выше (Orchestrator, Gateway) — не ретраят синхронные вызовы; вместо этого — таймаут → статус → компенсация/эскалация.

### Backoff с джиттером
- Формула: `delay = base * 2^attempt + random(0, base)` где base = 100ms.
- Попытки: 1 (100ms+jitter), 2 (200ms+jitter), 3 (400ms+jitter).
- Максимум 3 попытки (далее — circuit breaker / DLQ).
- Джиттер (random) — обязательно: предотвращает thundering herd.

### Token bucket
- Concurrent ретраи ограничены token bucket: не более N одновременных ретраев per dependency.
- При истощении bucket — быстрый fail (не ожидать).

### Только транзиентные сбои
- Ретраить: timeout, connection refused, 5xx, 429.
- НЕ ретраить: 4xx (кроме 429), бизнес-ошибки ( insufficient_funds, AML_REJECT).

### Таймауты
- Таймаут вызова выбирается по p99.9 latency зависимости (не «по умолчанию 30s»).
- Card: 10s, SBP: 3s, SWIFT: 30s, CBR: 30s, Core Banking: 5s.
- Таймаут > retry timeout: общий таймаут = sum(attempts + delays).

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **Ретраи на каждом уровне** | Каждый слой самостоятелен. | Экспоненциальный рост нагрузки. Каскадный отказ. |
| **Фиксированная задержка** | Просто. | Thundering herd. |
| **Без ретраев** | Нет лишней нагрузки. | Восстанавливающаяся система — потеря платежей. |

## Consequences

### Positive

- Предсказуемая нагрузка на зависимости при сбое.
- Предотвращение каскадных отказов.
- Чёткое разделение: кто ретраит, кто — нет.

### Negative

- Один слой ретраев — нужно явно документировать (может быть неочевидно для команд).
- Token bucket — дополнительный компонент (Redis/в памяти).
- При недостаточном количестве попыток — возможна потеря транзиентного сбоя (митигация: circuit breaker + DLQ).

## Reversibility

**Reversible.** Изменение политики ретраев — конфигурация (количество попыток, задержки), не архитектурное изменение.

## References

- Spine: AD-10, AD-8 (circuit breaker)
- Skills: `timeouts-backoff-jitter`, `circuit-breaker-retry`
- Источник: Amazon Builders' Library — "Timeouts, Retries, and Backoff with Jitter" (Marc Brooker)
