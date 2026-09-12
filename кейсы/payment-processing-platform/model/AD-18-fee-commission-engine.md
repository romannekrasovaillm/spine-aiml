---
id: AD-18
type: ad
title: "Fee/Commission engine"
status: "ADOPTED"
affects: [CMP-002, CMP-012]
verified_by: [C-029]
---

- **Binds**: Payment Orchestrator ↔ Fee Engine (новый bounded context), Ledger
- **Prevents**: расчёт комиссии захардкожен в оркестраторе — невозможность тарифных планов, изменение требует деплоя
- **Rule**: отдельный bounded context Fee Engine: тарифные планы (per channel, per rail, per partner), многоуровневые правила (процент + фикс + tiered), распределение (банк/НСПК/партнёр). Расчёт — синхронный вызов из Orchestrator (после авторизации, до проводки). Результат — запись в Ledger отдельной проводкой (комиссия = доход банка). Тарифы — конфигурируются без деплоя (DB-driven, admin UI). Версионирование тарифов: какая версия применена к платежу — фиксируется в payment_events.
- **Статус**: [ADOPTED]
