---
id: AD-19
type: ad
title: "Dispute/Chargeback flow"
status: "ADOPTED"
affects: [CMP-013]
verified_by: [C-030]
---

- **Binds**: Rail Connectors (card) ↔ Dispute Service (новый bounded context), Orchestrator
- **Prevents**: chargeback обрабатывается вручную — потеря сроков, штрафы от платёжной системы
- **Rule**: отдельный bounded context Dispute Service: приём ISO 8583 chargeback messages (message type 1420/1430), отдельная сага (оркестрация: получить → валидировать → проверить транзакцию → принять решение → проводка → ответ). Статусы: `DISPUTE_OPEN → DISPUTE_REVIEWED → DISPUTE_ACCEPTED/REJECTED → DISPUTE_SETTLED`. Сроки: Visa/MC — 45 дней на ответ, NSPK — 30 дней. Алерт при приближении дедлайна. Двойная запись в Ledger: возврат суммы + комиссия за chargeback (если применимо). Связь с оригинальным платежом по payment_id.
- **Статус**: [ADOPTED]
