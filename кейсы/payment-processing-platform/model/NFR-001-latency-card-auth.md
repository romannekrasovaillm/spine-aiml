---
id: NFR-001
type: nfr
title: "Latency авторизации по картам"
status: "accepted"
verified_by: [C-012]
verification: "Histogram: payment_auth_duration_seconds"
affects: [CMP-001, CMP-002, INT-001]
p99_target_ms: 2000
---

Цель: p99 < 2s, p99.9 < 5s. Разложение по hop'ам: `arch-be nfr budget` (INT-001 ≤ 800 мс, резерв 1200 мс на внутренние компоненты).
