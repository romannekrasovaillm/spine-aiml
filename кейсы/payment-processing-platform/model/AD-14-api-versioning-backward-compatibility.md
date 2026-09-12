---
id: AD-14
type: ad
title: "API versioning + backward compatibility"
status: "ADOPTED"
affects: [CMP-001]
verified_by: [C-026]
---

- **Binds**: Payment Gateway API ↔ все каналы (мобильный банк, Open API, партнёры)
- **Prevents**: breaking change ломает потребителей без предупреждения; невозможность параллельного использования версий
- **Rule**: семантическое версионирование API: `major.minor`. Версия — в HTTP-заголовке `Accept: application/vnd.bank.payments.v2+json` (header-based, не URL-path). Backward-compatible изменения (additive: новые поля, новые endpoints) — minor bump, не ломают потребителей. Breaking changes (удаление поля, изменение типа) — major bump, старая версия поддерживается 12 месяцев с deprecation notice (header `Sunset`). CI-гейт: schemathesis-проверка совместимости схемы. OpenAPI-спецификация — источник истины, автогенерация клиентов.
- **Статус**: [ADOPTED]
