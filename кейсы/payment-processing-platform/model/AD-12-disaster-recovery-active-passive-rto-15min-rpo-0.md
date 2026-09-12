---
id: AD-12
type: ad
title: "Disaster Recovery (active-passive, RTO=15min, RPO=0)"
status: "ADOPTED"
affects: [CMP-002, CMP-007]
verified_by: [C-024]
---

- **Binds**: все компоненты платформы ↔ DR-сайт, sync replication, failover runbook
- **Prevents**: потеря данных при отказе основного ЦОД; недоступность процессинга > 15 мин
- **Rule**: active-passive: основной ЦОД — active, DR-сайт — hot standby. PostgreSQL: синхронная репликация (synchronous_commit=on, synchronous_standby_names) → RPO=0. Kafka: MirrorMaker2 в режим sync (acks=all, min.insync.replicas=2). Redis: replica в DR-сайте. Failover: автоматическое для stateless-сервисов (K8s), ручное для stateful (PostgreSQL promote). DR drill: плановое переключение раз в квартал (проверка RTO). Runbook: пошаговая процедура failback. Метрика: `dr_drill_rto_actual` — фактическое время восстановления.
- **Статус**: [ADOPTED]
