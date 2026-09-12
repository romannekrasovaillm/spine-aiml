---
id: NFR-002
type: nfr
title: "Latency СБП"
status: "accepted"
verification: "Histogram: sbp_payment_duration_seconds"
affects: [CMP-006, INT-002]
p99_target_ms: 500
---

Цель: p99 < 500ms. Разложение по hop'ам: `arch-be nfr budget` (INT-002 ≤ 300 мс, резерв 200 мс).
