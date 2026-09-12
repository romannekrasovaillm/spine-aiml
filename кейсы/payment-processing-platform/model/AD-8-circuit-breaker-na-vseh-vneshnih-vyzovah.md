---
id: AD-8
type: ad
title: "Circuit breaker на всех внешних вызовах"
status: "ADOPTED"
affects: [CMP-006]
verified_by: [C-004]
---

- **Binds**: Rail Connectors ↔ внешние системы (NSPK, CBR, SWIFT, Core Banking)
- **Prevents**: каскадный отказ при недоступности внешней системы, истощение connection pool
- **Rule**: каждый вызов внешней системы — через circuit breaker (closed/open/half-open). Таймаут на вызов: card < 10s, SBP < 3s, SWIFT < 30s, CBR < 30s. При открытом breaker — быстрый fail с явным статусом (RAIL_UNAVAILABLE), не таймаут. Метрика: состояние breaker'ов дашборд.
- **Статус**: [ADOPTED]
