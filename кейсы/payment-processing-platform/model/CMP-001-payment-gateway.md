---
id: CMP-001
type: cmp
title: "Payment Gateway"
status: "designed"
depends_on: [CMP-002]
implements: [CAP-001, REQ-002, AD-5]
availability: 0.999
replicas: 3
rps_per_instance: 2000
instances: 4
cost_per_instance_month: 45000
exit_cost: 300000
---

Приём запросов, идемпотентность, валидация схемы. Зона DMZ→App, БД pgw_db.
