---
id: AD-4
type: ad
title: "Сильная консистентность для финансового состояния"
status: "ADOPTED"
affects: [CMP-002, CMP-003, CMP-007]
verified_by: [C-025]
---

- **Binds**: Payment Orchestrator, Ledger, Authorization ↔ PostgreSQL (single-writer per payment_id)
- **Prevents**: гонки при параллельной обработке одного платежа, потеря обновлений, двойное списание
- **Rule**: финансовое состояние (статус платежа, остатки, холды) — сильная консистентность. Single-writer per aggregate (payment_id): партиционирование/маршрутизация запросов по payment_id на один обработчик. Оптимистичная блокировка через версию сущности. Уведомления, отчётность, фрод-скоринг — eventual consistency.
- **Статус**: [ADOPTED]
