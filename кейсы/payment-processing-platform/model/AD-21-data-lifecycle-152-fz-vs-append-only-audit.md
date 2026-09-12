---
id: AD-21
type: ad
title: "Data lifecycle — 152-ФЗ vs append-only audit"
status: "ADOPTED"
affects: [CMP-002, CMP-007]
verified_by: [C-032]
---

- **Binds**: все сервисы ↔ data retention, pseudonymization policy
- **Prevents**: конфликт между правом на забвение (152-ФЗ) и требованием сохранения фин. данных (115-ФЗ, 5 лет)
- **Rule**: разделение данных по классам: (1) Финансовые транзакции (payment, payment_events, ledger) — retention 5 лет (115-ФЗ), удаление запрещено. Право на забвение НЕ применяется. (2) Персональные данные клиента (ФИО, телефон, email в metadata) — псевдонимизация по запросу: замена на token, сохранение связи через отдельный secure mapping. (3) Технические данные (логи, traces) — retention 90 дней, затем удаление. (4) Карточные данные (PAN, CVV) — не хранятся (токенизация в DMZ, AD-6). Data minimization: в событиях Kafka — минимум ПДн, только payment_id и статус.
- **Статус**: [ADOPTED]
