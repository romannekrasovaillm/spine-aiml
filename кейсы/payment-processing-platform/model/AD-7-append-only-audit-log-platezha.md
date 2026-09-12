---
id: AD-7
type: ad
title: "Append-only аудит-лог платежа"
status: "ADOPTED"
affects: [CMP-002]
verified_by: [C-005]
---

- **Binds**: Payment Orchestrator ↔ `payment_events` (append-only table)
- **Prevents**: потеря аудиторского следа, невозможность реконструкции жизненного цикла платежа для регулятора/судебного запроса
- **Rule**: каждое изменение состояния платежа — запись в `payment_events` (event_type, payment_id, timestamp, actor, payload, prev_version). Текущий статус материализован из событий. Запрет на UPDATE/DELETE событий. Retention — 5 лет (115-ФЗ). Не путать с event sourcing: current state кэшируется, события — для аудита.
- **Статус**: [ADOPTED]
