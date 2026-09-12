---
id: AD-26
type: ad
title: "Operator actions audit"
status: "ADOPTED"
affects: [CMP-002]
verified_by: [C-020]
---

- **Binds**: все операционные действия (ручной ввод, отмена, форсирование статуса) ↔ operator audit log
- **Prevents**: действия оператора невидимы — невозможность разбора инцидента, нарушение разделения полномочий
- **Rule**: каждое действие оператора (manual override, force status, manual reconciliation, DLQ replay) — запись в `operator_audit(operator_id, action, target_payment_id, reason, before_state, after_state, timestamp, ip).` Отдельно от `payment_events` (системные события). Требует reason (обоснование) — обязательное поле. RBAC: разные роли — разные действия (operator, supervisor, admin). Dual control: критичные действия (force COMPLETED, manual settlement) — требуют двух подписей. Alert: аномальное количество manual overrides оператором.
- **Статус**: [ADOPTED]
