---
id: CMP-007
type: cmp
title: "Ledger"
status: "designed"
implements: [CAP-003, AD-3, AD-4]
availability: 0.9995
replicas: 2
rps_per_instance: 2000
instances: 2
cost_per_instance_month: 60000
exit_cost: 500000
---

Двойная запись, проводки, гласный журнал. Зона Data, БД ledger_db.
