---
id: INT-005
type: int
title: "Core Banking (АБС)"
status: "accepted"
latency_budget_ms: 3000
---

Проверка баланса, холды. Latency-бюджет hop'а p99 < 3000 мс (в цепочке NFR-017 ≤ 30 с). Взаимодействие через адаптер; API частично недокументировано (RISK-002).
