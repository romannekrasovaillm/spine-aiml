---
id: AD-20
type: ad
title: "Вебхук/колбэк-стратегия для async рельсов"
status: "ADOPTED"
affects: [CMP-002, CMP-006]
verified_by: [C-031]
---

- **Binds**: Rail Connectors (SWIFT, БЭСП) ↔ Orchestrator, callback endpoint
- **Prevents**: async-ответ от рельса не коррелируется с платежом → платёж «зависает» в RAIL_SENT
- **Rule**: async-рельсы (SWIFT, БЭСП) — двунаправленная корреляция: (1) исходящий: payment_id в поле_reference (назначение платежа / remittance info), (2) входящий: callback endpoint `/callbacks/{rail}` принимает ответ, извлекает payment_id из reference. Таймаут ожидания ответа: SWIFT — 24ч, БЭСП — 1ч. При таймауте: переход в `RAIL_TIMEOUT` → компенсация (если возможно) или эскалация в операционную очередь. Polling fallback:.periodic status query (MT199 для SWIFT). Callback endpoint — идемпотентный (AD-5), mTLS, IP-allowlist.
- **Статус**: [ADOPTED]
