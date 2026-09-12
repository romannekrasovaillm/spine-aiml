---
id: QAS-002
type: qas
title: "Отказ primary-инстанса оркестратора"
status: "accepted"
implements: [NFR-004]
source: "инфраструктура ЦОД"
stimulus: "внезапный отказ primary-инстанса Payment Orchestrator"
artifact: "CMP-002 Payment Orchestrator"
response: "трафик переключается на реплику, приём платежей продолжается"
measure: "доступность приёма ≥ 99.99% (NFR-004) — проверяется `arch-be nfr availability`; RTO ≤ 15 мин (NFR-006)"
---

Отказоустойчивость приёма: CMP-002 работает минимум в двух репликах
(`replicas: 2`), сага-состояние в orc_db переживает инстанс. Ручные
действия — по DR-runbook (C-024).
