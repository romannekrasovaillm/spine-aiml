---
id: CMP-003
type: cmp
title: "Authorization"
status: "designed"
depends_on: [INT-005]
implements: [AD-4, AD-5]
availability: 0.999
replicas: 2
rps_per_instance: 2500
instances: 2
cost_per_instance_month: 48000
exit_cost: 250000
---

Лимиты, холды, проверка баланса, взаимодействие с ядром. Зона App, БД auth_db + Redis.
