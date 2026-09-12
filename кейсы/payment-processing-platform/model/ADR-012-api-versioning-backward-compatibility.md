---
id: ADR-012
type: adr
title: "API versioning и backward compatibility"
status: "Accepted"
date: "2026-08-15"
implements: [AD-14]
affects: [CMP-001]
---

## Context

Payment Gateway API потребляется разными каналами: мобильный банк (несколько версий приложения), веб-банк, Open API (внешние партнёры — маркетплейсы, B2B-клиенты). Каждый потребитель обновляется в своём темпе. Breaking change API без стратегии версионирования ломает потребителей молча. Партнёры через Open API — особенно чувствительны: изменение контракта = инцидент у партнёра.

## Decision

**Header-based API versioning + backward compatibility policy.**

### Версионирование
- Версия — в HTTP-заголовке: `Accept: application/vnd.bank.payments.v2+json`.
- Не URL-path (`/v2/payments`) — URL меняется → ломаются bookmark'и, кэш, мониторинг.
- Не query param (`?version=2`) — кэш-проблемы, proxy-разрывы.

### Compatibility
- **Backward-compatible (minor bump)**: новые поля, новые endpoints, новые optional параметры. Не ломают существующих потребителей. Пример: добавить `description` в ответ `/payments/{id}`.
- **Breaking (major bump)**: удаление поля, изменение типа, изменение семантики. Старая версия поддерживается 12 месяцев.
- **Depprecation**: заголовок `Sunset: Wed, 15 Aug 2027 00:00:00 GMT` + `Deprecation: true`. Уведомление партнёров за 6 месяцев.
- **Sunset**: после даты — 410 Gone с сообщением о новой версии.

### CI-гейт
- Schemathesis: автотесты по OpenAPI-спецификации — каждый PR.
- Совместимость: сравнение новой спецификации со старой (openapi-diff). Breaking change без major bump → fail build.
- OpenAPI-спецификация — источник истины: автогенерация клиентов (Go, Java, TypeScript), документация (Swagger UI).

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **URL-path versioning** (`/v2/payments`) | Понятно, кэшируемо. | URL меняется. Мониторинг ломается. Дублирование routes. |
| **No versioning** (just backward compatible) | Просто. | Невозможно сделать breaking change. Накапливается техдолг. |
| **GraphQL** | Клиент выбирает поля. | Overkill для платёжного API. Инфраструктура. |

## Consequences

### Positive

- Потребители обновляются в своём темпе.
- Breaking changes — плановые, с уведомлением.
- CI-гейт предотвращает случайные breaking changes.

### Negative

- Поддержка 2 версий одновременно (12 месяцев) — дополнительный код/тесты.
- Header-based — менее очевидно для разработчиков (нужна документация).

## Reversibility

**Reversible.** Переход на URL-path versioning — добавление routes. Header-based сохраняется.

## References

- Spine: AD-14, AD-23 (contract testing)
