---
id: AD-16
type: ad
title: "Стратегия развёртывания (canary + zero-downtime DB migrations)"
status: "ADOPTED"
affects: [CMP-002, CMP-007]
verified_by: [C-027]
---

- **Binds**: все сервисы ↔ Kubernetes deployment, CI/CD pipeline
- **Prevents**: downtime при деплое; миграция БД блокирует таблицы; битый релиз уходит на весь трафик
- **Rule**: canary deployment: 5% → 25% → 50% → 100% трафика, автоматический rollback при росте error rate > threshold (5 мин окно). Blue-green для Orchestrator и Ledger (stateful, критичные). Zero-downtime DB migrations: expand-contract pattern — (1) expand: добавить колонку/таблицу (обратно-совместимо), (2) migrate: заполнить данные, dual-write, (3) contract: удалить старое. Запрещены миграции с блокировкой таблиц на продакшене (ALTER TABLE lock). Миграции — через Flyway, каждая в транзакции, reversible.
- **Статус**: [ADOPTED]
