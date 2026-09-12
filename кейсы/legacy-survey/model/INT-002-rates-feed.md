---
id: INT-002
type: int
title: "Rates feed (фид тарифов)"
status: proposed
contract: contracts/tariffs-feed.asyncapi.yaml
---

Источник тарифов: `rates.internal.example:8080/feed` (config/app.yaml:6).
Сейчас в коде не потребляется — тариф зашит константой
(billing/tariffs.py:3), ссылка на фид в конфиге мертва. Дельта
tariffs-idempotent-consumer
(`changes/tariffs-idempotent-consumer/DELTA.md`, status: proposed)
вводит идемпотентного потребителя событий фида; контракт события
зафиксирован до реализации (ADR-035: интеграция → контрактный артефакт).
