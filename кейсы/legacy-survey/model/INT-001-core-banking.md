---
id: INT-001
type: int
title: "Core Banking (АБС)"
status: accepted
---

Внешнее ядро: `corebank.internal.example:8443` (config/app.yaml:4), путь
`/api/v2`. Контрактного артефакта в репозитории нет — интеграция без
контракта видна как warn в trace check (`int-without-contract`), это
легитимно для внешней системы, но должно быть на виду. Вопрос контракта —
владельцу домена (кандидат в survey-notes.md).
