---
id: AD-17
type: ad
title: "Queue Load Leveling для пиковых нагрузок"
status: "ADOPTED"
affects: [CMP-001, CMP-002]
verified_by: [C-028]
---

- **Binds**: Payment Gateway ↔ Payment Orchestrator (через Kafka command topic)
- **Prevents**: зарплатные дни, валютные сессии — пиковая нагрузка 5–10× от средней → отказ процессинга при прямой синхронной обработке
- **Rule**: Payment Gateway публикует команду `ProcessPayment` в Kafka-топик `payments.commands` (не синхронный вызов Orchestrator). Orchestrator — competing consumers (N instances), масштабирование по lag. Backpressure: при lag > threshold → Gateway возвращает 202 Accepted (принято, будет обработано), не 200. Пиковые периоды: предмасштабирование по расписанию (зарплатные дни 25–27 числа). Приоритизация: отдельные партиции/топики для high-priority платежей (СБП, карты) vs low-priority (батч-переводы).
- **Статус**: [ADOPTED]
