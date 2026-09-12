---
id: AD-11
type: ad
title: "Сверка (reconciliation) с рельсами"
status: "ADOPTED"
affects: [CMP-008, CMP-011]
verified_by: [C-015]
---

- **Binds**: Clearing & Settlement ↔ Rail Connectors (NSPK, CBR, SWIFT), Ledger
- **Prevents**: расхождение между внутренним состоянием платформы и внешним рельсом — не выявленные финансовые расхождения
- **Rule**: end-of-day сверка: получение реестров от каждого рельса, auto-match по payment_id/amount/status, unmatched записи → exception queue (ручная разборка). Сверка идёт в обоих направлениях: платформа → рельс (отправленные, но не подтверждённые) и рельс → платформа (полученные от рельса, но не в платформе). Результат сверки: `reconciliation_report(date, rail, matched, unmatched_platform, unmatched_rail, exceptions)`. Алерт при unmatched > 0. Сверка — предусловие для settlement.
- **Статус**: [ADOPTED]
