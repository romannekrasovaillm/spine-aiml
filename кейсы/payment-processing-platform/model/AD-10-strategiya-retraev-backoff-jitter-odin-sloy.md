---
id: AD-10
type: ad
title: "Стратегия ретраев (backoff + jitter, один слой)"
status: "ADOPTED"
affects: [CMP-002, CMP-006]
verified_by: [C-014]
---

- **Binds**: все межсервисные и внешние вызовы ↔ circuit breaker + retry policy
- **Prevents**: «ретраи добили зависимость» — каскадный отказ при перегрузке; ретраи на каждом уровне стека → экспоненциальный рост нагрузки
- **Rule**: ретраи — **только в одном слое** стека (обычно — Rail Connector или Orchestrator). Экспоненциальный backoff с джиттером: `delay = base * 2^attempt + random(0, base)`. Token bucket для ограничения concurrent ретраев. Ретраи — только для транзиентных сбоев (timeout, 5xx, connection refused); 4xx — не ретраить. Таймаут вызова выбирается по p99.9 latency зависимости, не «по умолчанию». Запрещены ретраи на каждом уровне (Gateway → Orchestrator → Connector — ретрай только на одном).
- **Статус**: [ADOPTED]
