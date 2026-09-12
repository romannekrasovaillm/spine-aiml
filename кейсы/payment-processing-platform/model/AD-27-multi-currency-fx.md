---
id: AD-27
type: ad
title: "Multi-currency / FX"
status: "ADOPTED"
affects: [CMP-002, CMP-014]
verified_by: [C-034]
---

- **Binds**: Payment Orchestrator ↔ FX Service (новый bounded context), Ledger
- **Prevents**: конвертация валют захардкожена — невозможность мультивалютных платежей (SWIFT)
- **Rule**: отдельный bounded context FX Service: курсы валют (ежедневные от treasury, +real-time для major pairs), spread (конфигурируемый per channel/rail). Конвертация: `amount_from * rate = amount_to`, фиксация курса в момент авторизации (rate lock). Запись в Ledger: двойная проводка в двух валютах + проводка курсовой разницы (доход банка). Курсы — версионные: какая версия курса применена — в payment_events. Округление: банковское (half-up), до 2 знаков (или 4 для экзотических валют). Запрещён плавающий курс после авторизации.
- **Статус**: [ADOPTED]
