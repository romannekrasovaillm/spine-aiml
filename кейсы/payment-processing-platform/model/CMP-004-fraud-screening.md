---
id: CMP-004
type: cmp
title: "Fraud Screening"
status: "designed"
implements: [AD-5]
availability: 0.995
replicas: 2
rps_per_instance: 1000
instances: 2
cost_per_instance_month: 70000
exit_cost: 600000
---

Правила, скоринг, блокировки. Зона App, БД fraud_db + Redis.
