---
id: CMP-002
type: cmp
title: "Payment Orchestrator"
status: "designed"
depends_on: [CMP-003, CMP-004, CMP-005, CMP-006, CMP-007, CMP-012]
implements: [CAP-002, REQ-001, AD-1, AD-4, AD-7]
availability: 0.999
replicas: 2
rps_per_instance: 1500
instances: 4
cost_per_instance_month: 55000
exit_cost: 400000
---

Saga-координация, state machine, маршрутизация на рельс. Зона App, БД orc_db.
