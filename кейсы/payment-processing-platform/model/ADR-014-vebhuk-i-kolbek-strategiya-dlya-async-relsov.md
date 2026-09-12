---
id: ADR-014
type: adr
title: "Вебхук и колбэк-стратегия для async рельсов"
status: "Accepted"
date: "2026-08-15"
implements: [AD-20]
affects: [CMP-002, CMP-006]
---

## Context

Карты и СБП — синхронные рельсы: запрос → ответ в одной сессии. Но SWIFT и БЭСП — асинхронные: платформа отправляет сообщение, ответ приходит через минуты/часы/дни как отдельное сообщение. Без стратегии корреляции: платформа не может связать ответ с исходным платежом → платёж «зависает» в `RAIL_SENT`, клиент не получает статус.

## Decision

**Двунаправленная корреляция + callback endpoint + polling fallback.**

### Корреляция
- **Исходящий**: payment_id помещается в reference-поле сообщения (SWIFT: tag 50/FBNK — remittance info; БЭСП: назначение платежа).
- **Входящий**: callback endpoint `/callbacks/{rail}` принимает ответное сообщение, извлекает payment_id из reference-поля.
- **Извлечение**: regex/parser для каждого рельса (формат reference различается).

### Callback endpoint
- Endpoint: `POST /callbacks/{rail}` (в Integration Zone, AD-6).
- Безопасность: mTLS + IP-allowlist (только IP-адреса рельса).
- Идемпотентность: AD-5 (входящий message_id → inbox-таблица).
- Обработка: извлечь payment_id → обновить статус в Orchestrator (state machine переход `RAIL_SENT → RAIL_CONFIRMED` или `RAIL_SENT → REJECTED_BY_RAIL`).

### Таймауты
- SWIFT: 24 часа (MT103 → MT202/ACK/NAK).
- БЭСП: 1 час (ответ БЭСП).
- При таймауте: переход `RAIL_SENT → RAIL_TIMEOUT` → компенсация (если возможно) или эскалация в операционную очередь.

### Polling fallback
- Если callback не пришёл в течение 50% таймаута: инициировать status query (SWIFT: MT199, БЭСП: запрос статфика).
- Polling: каждые 30 мин до получения ответа или истечения таймаута.
- Результат polling обрабатывается так же, как callback.

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **Только polling** (без callback) | Просто. | Нагрузка на рельс. Высокая задержка обнаружения. |
| **Только callback** (без polling) | Быстро. | Если callback потерян — платёж зависнет. |
| **Long polling** | Меньше запросов. | Рельсы не поддерживают. |

## Consequences

### Positive

- Async-ответ коррелируется с платежом.
- Polling fallback — страховка от потерянного callback.
- Чёткие таймауты — нет «вечных» RAIL_SENT.

### Negative

- Парсинг reference-полей — хрупкий (формат рельса может измениться).
- Polling — нагрузка на рельс (митигация: только при отсутствии callback).
- Окно неопределённости (до таймаута) — клиент видит RAIL_SENT.

## Reversibility

**Reversible.** Изменение стратегии корреляции — замена парсера. Callback endpoint остаётся.

## References

- Spine: AD-20, AD-5 (idempotency), AD-6 (trust zones)
- Связь: ADR-006 (state machine — RAIL_SENT, RAIL_TIMEOUT)
