---
id: AD-15
type: ad
title: "Schema Registry для Kafka"
status: "ADOPTED"
affects: [CMP-002, CMP-009]
verified_by: [C-017]
---

- **Binds**: все продюсеры и консьюмеры событий ↔ Confluent Schema Registry
- **Prevents**: несовместимые изменения схемы события → consumer падает или теряет данные; отсутствие контракта событий
- **Rule**: формат событий — Avro (compact, schema-evolvable). Confluent Schema Registry — единый реестр схем. Compatibility: BACKWARD по умолчанию (новый consumer читает старые сообщения). Breaking change схемы — новая версия + стратегия миграции (dual-write period). CI-гейт: регистрация схемы в Schema Registry → проверка совместимости → при несовместимости — fail build. Запрещён JSON без схемы для событий платежа.
- **Статус**: [ADOPTED]
