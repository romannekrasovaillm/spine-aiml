---
id: INT-001
type: int
title: "Карточный рельс (MIR/Visa/MC, процессинг/NSPK)"
status: "accepted"
latency_budget_ms: 800
---

Acquiring + Issuing. Latency-бюджет hop'а p99 < 800 мс (в цепочке NFR-001 ≤ 2000 мс). Регуляторы: PCI DSS, NSPK. Синхронный вызов, circuit breaker (AD-8).
