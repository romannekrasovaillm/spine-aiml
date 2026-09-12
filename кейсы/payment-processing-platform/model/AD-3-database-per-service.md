---
id: AD-3
type: ad
title: "Database-per-service"
status: "ADOPTED"
affects: [CMP-001, CMP-002, CMP-007]
verified_by: [C-003]
---

- **Binds**: каждый bounded context ↔ собственная схема/БД
- **Prevents**: shared-database coupling — сервисы лезут в чужие таблицы, невозможность независимого деплоя/масштабирования
- **Rule**: один bounded context = одна база данных (PostgreSQL). Cross-service доступ — только через API или события. Запрещён прямой доступ к БД другого сервиса. Исключение: read-replica для аналитики (read-only, отдельный кластер).
- **Статус**: [ADOPTED]
