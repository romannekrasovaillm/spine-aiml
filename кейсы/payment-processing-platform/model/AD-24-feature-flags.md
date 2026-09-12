---
id: AD-24
type: ad
title: "Feature flags"
status: "ADOPTED"
affects: [CMP-015]
verified_by: [C-019]
---

- **Binds**: все сервисы ↔ feature flag service (например, Unleash/LaunchDarkly)
- **Prevents**: новый рельс/фича требует деплоя для включения; нет kill switch для экстренного отключения канала
- **Rule**: feature flags для: новых рельсов (включение по банкам), A/B-тестов, canary-фич, kill switch (экстренное отключение канала/партнёра). Типы флагов: on/off, percentage rollout, targeted (по bank_id/channel). Конфигурация — без деплоя (feature flag service). Kill switch — отдельный флаг с policy: включение только через approval (двое рук). Audit: кто, когда, какой флаг изменил. Метрика: `feature_flag_evaluations_total` — мониторинг использования.
- **Статус**: [ADOPTED]
