---
id: AD-23
type: ad
title: "Контрактное тестирование"
status: "ADOPTED"
affects: [CMP-001]
verified_by: [C-023]
---

- **Binds**: все пары consumer↔provider ↔ CI/CD pipeline
- **Prevents**: изменение контракта одного сервиса ломает consumer молча (обнаруживается в продакшене)
- **Rule**: consumer-driven contract testing (Pact): каждый consumer описывает ожидания от provider. CI-гейт: изменение provider → прogoн Pact-тестов → при несовпадении — fail build. Schemathesis для OpenAPI-спецификаций: авто-генерация тест-кейсов по схеме. Контракты Kafka-событий — через Schema Registry compatibility checks (AD-15). Отдельный pipeline stage: `contract-tests` — обязателен для merge в main.
- **Статус**: [ADOPTED]
