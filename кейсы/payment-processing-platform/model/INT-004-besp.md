---
id: INT-004
type: int
title: "БЭСП (ЦБ РФ)"
status: "accepted"
latency_budget_ms: 20000
---

Large-value платежи. Latency-бюджет hop'а p99 < 20 с; NFR-цель цепочки не заявлена (`arch-be nfr budget` показывает это предупреждением `budget-hop-uncovered`). Асинхронный рельс с колбэком (AD-20). Регулятор: ЦБ РФ.
