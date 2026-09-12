---
id: NFR-004
type: nfr
title: "Доступность приёма платежей"
status: "accepted"
verification: "Uptime probe на API Gateway"
affects: [CMP-001, CMP-002]
availability_target: 0.9999
---

Цель: 99.99% (~52 min/year). Расчёт по участкам: `arch-be nfr availability` (CMP-001 ×3 реплики, CMP-002 ×2).
