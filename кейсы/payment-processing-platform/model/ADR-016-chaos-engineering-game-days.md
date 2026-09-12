---
id: ADR-016
type: adr
title: "Chaos engineering — плановые game days"
status: "Accepted"
date: "2026-08-15"
implements: [AD-25]
affects: [CMP-002]
---

## Context

Архитектура имеет множество механизмов отказоустойчивости: circuit breaker (AD-8), retry (AD-10), DLQ (AD-9), DR (AD-12), saga compensation (AD-1). Но без практической валидации — это теория. Скрытые каскадные зависимости, неожиданные взаимодействия, некорректные таймауты — выявляются только в реальном инциденте. Для платформы процессинга инцидент в продакшене — финансовые потери.

## Decision

**Плановые game days раз в квартал + chaos testing в staging.**

### Game days (квартальные)
- Полное переключение на DR-сайт с реальным трафиком на 1 час.
- Параллельно — инъекции сбоев:
  - Kill pod случайного сервиса (K8s chaos monkey).
  - Network partition между зонами (AD-6).
  - PostgreSQL failover (promote standby).
  - Kafka broker down (один из 3).
  - Redis unavailable (network drop).
  - Rail Connector timeout (injection 30s delay).
- Метрики: время обнаружения (MTTD), время восстановления (MTTR), affected transactions count, error budget consumption.

### Chaos testing в staging (непрерывно)
- Автоматические chaos experiments в staging (Litmus/Chaos Mesh).
- Ежедневные: kill pod, network delay, disk pressure.
- Результаты — в CI-отчёте, регрессии — алерты.

### Правила
- Chaos — только в staging (или canary segment продакшена с 1% трафика, под контролем).
- Запрещён chaos в peak hours (зарплатные дни, валютные сессии).
- Каждый game day — postmortem: что сломалось, что не сработало, обновление runbooks.
- Участники: DevOps, SRE, on-call дежурный, архитектор (наблюдатель).

### Что валидируется
| Эксперимент | Что проверяем |
|-------------|--------------|
| Kill Orchestrator pod | Saga recovery, restart с последнего шага |
| PostgreSQL failover | RPO=0, RTO<10min, connection retry |
| Kafka broker down | Consumer rebalance, no message loss |
| Rail timeout | Circuit breaker open, компенсация |
| Network partition DMZ↔App | mTLS retry, fallback behaviour |
| Redis unavailable | Idempotency degradation (fallback to inbox) |

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **Только в продакшене** (Netflix style) | Реальные условия. | Риск для платежей. Недопустимо для банка. |
| **Только в staging** | Безопасно. | Staging ≠ продакшен (нагрузка, данные). |
| **Без chaos** | Нет работы. | Отказоустойчивость не валидирована. Инциденты — сюрприз. |

## Consequences

### Positive

- Практическая валидация отказоустойчивости.
- Выявление скрытых каскадных зависимостей до инцидента.
- Команда on-call — тренированная.
- Runbooks — актуальные (обновляются после каждого game day).

### Negative

- Game day — ресурсозатратный (команда + время).
- Risk: даже в staging — возможна деградация (митигация: blast radius ограничен).
- Ложная уверенность: staging может не покрывать все продакшен-сценарии.

## Reversibility

**Reversible.** Отключение chaos — прекращение экспериментов. Архитектура не меняется.

## References

- Spine: AD-25, AD-12 (DR), AD-8 (circuit breaker), AD-10 (retry)
- Инструменты: Chaos Mesh / Litmus (K8s), Gremlin (опционально)
