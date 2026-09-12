---
id: NFR-017
type: nfr
title: "Latency cross-border (SWIFT)"
status: "accepted"
verification: "Histogram: swift_payment_duration_seconds"
affects: [INT-003, INT-005, CMP-006]
p99_target_ms: 30000
---

Цель: p99 < 30s end-to-end для cross-border платежа. Разложение по hop'ам: `arch-be nfr budget` (INT-003 ≤ 25000 мс + INT-005 ≤ 3000 мс, резерв 2000 мс на коннектор CMP-006 и внутреннюю обработку). Асинхронный рельс (AD-20): колбэк/polling не блокируют приём.
