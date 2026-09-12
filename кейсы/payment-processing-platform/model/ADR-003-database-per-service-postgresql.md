---
id: ADR-003
type: adr
title: "Database-per-service с PostgreSQL"
status: "Accepted"
date: "2026-08-15"
implements: [AD-3]
affects: [CMP-001, CMP-002, CMP-007]
---

## Context

Платформа состоит из 10+ bounded contexts (Payment Gateway, Orchestrator, Authorization, Fraud, AML, Ledger, Clearing, Rail Connectors, Notifications, Reporting). При shared-базе сервисы лезут в чужие таблицы — невозможность независимого деплоя, схема-связанность, изменение таблицы одного сервиса ломает другой. Для time-to-market и независимых команд — блокер.

PostgreSQL выбран как OLTP-движок: ACID, JSONB для гибких полей, хорошая репликация (синхронная для RPO=0), расширяемость (pg_partman для партицирования payment_events).

## Decision

**Database-per-service на PostgreSQL.**

- Один bounded context = одна база данных (отдельный logical cluster, изолированные credentials).
- Cross-service доступ — только через API (gRPC/REST) или события (Kafka). Прямой доступ к БД другого сервиса запрещён.
- Read-replica для аналитики/отчётности — отдельный read-only кластер, не на запросном пути.
- Синхронная репликация (synchronous_commit=on, synchronous_standby_names) для сервисов с RPO=0: Orchestrator, Ledger, Authorization.
- Партицирование `payment_events` по дате (месяц) — pg_partman, retention 5 лет.
- Миграции: каждый сервис управляет своей схемой (Flyway/Liquibase), запрещены cross-service миграции.

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **Shared database** (все сервисы в одной БД) | Простота, JOIN'ы cross-service. | Схема-связанность, невозможность независимого деплоя, блокировки. Классический distributed monolith. |
| **Mixed** (часть в shared, часть per-service) | Компромисс. | Размытие границ. Со временем деградирует в shared. |
| **MongoDB** (document store) | Гибкая схема, горизонтальное масштабирование. | Нет ACID для multi-document транзакций (до v4.0 — нет, после — ограничено). Для финансового состояния — риск. |

## Consequences

### Positive

- Независимый деплой и масштабирование сервисов.
- Изоляция сбоев (падение БД одного сервиса не влияет на другие).
- Чёткие границы данных (DDD alignment).

### Negative

- Нет cross-service JOIN'ов — данные нужно дублировать (через события) или запрашивать через API.
- Операционная сложность: N баз данных вместо одной (backup, мониторинг, patching).
- Distributed data consistency — нужен saga (AD-1) и outbox (AD-2).

## Reversibility

**Costly.** Объединение баз — обратный процесс, но потеря независимости деплоя. Практически необратимо без боли.

## References

- Spine: AD-3, AD-4
- Источник: https://microservices.io/patterns/data/database-per-service.html
