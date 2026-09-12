---
name: significance-routing
description: Маршрутизация изменений по Architecture Significance Score — 15 триггеров риска, маршруты Fast/Standard/Critical, контрольные точки A0–A5, детектор approval theater. Используй этот навык ВСЕГДА при тriage новой задачи/изменения, при решении «нужен ли архитектор», при настройке гейтов процесса, при анализе «почему всё идёт через архитектурный комитет».
---

# Маршрутизация по значимости

Корпоративная ошибка — гнать каждую задачу через одинаковый процесс. Маршрут определяется риском, обратимостью и blast radius, а не брендом команды или модели.

## 15 триггеров (инструмент `significance_score`)

new_component · new_datastore · new_vendor · domain_ownership_change · cross_domain_integration · api_contract_change · data_contract_change · security_boundary_change · trust_zone_change · consistency_model_change · significant_nfr · rto_rpo_targets · irreversible_migration · financial_impact · criticality_or_exception

## Маршруты

- **Fast** (0–1 триггера): Intent Lite → дельта-спека (навык `delta-spec`) → агентная реализация → авто-валидация. Архитектор не нужен; работают constitution/golden path.
- **Standard** (2–4): + контракт Spec→Plan→Tasks, автоматический Architecture Fit (A0) и Impact (A1); архитектор подключается ПО ТРИГГЕРУ.
- **Critical** (5+ или любой из security_boundary_change / irreversible_migration / criticality_or_exception): полный Solutioning (spine + ADR + NFR), обязательная человеческая точка A3, walking skeleton до массовой генерации, evidence-гейты.

## Контрольные точки A0–A5

- A0 Architecture Fit (авто): укладывается в существующий паттерн?
- A1 Impact Assessment (агент готовит): blast radius, затронутые владельцы.
- A2 Solutioning trigger: порог задан человеком заранее, не «на глаз».
- A3 Human Architecture Decision — ЕДИНСТВЕННАЯ обязательная человеческая точка: machine-readable итог {choice, rationale, constraints, rejected options, expiry}.
- A4 Conformance Evidence: соответствие решению + fitness functions (`fitness_check`), sampling допустим.
- A5 Post-deploy Drift Check: по инциденту/тренду телеметрии.

## Approval theater — детектор

Если >95% однотипных запросов на утверждение проходят без замечаний — граница автономии пересмотреть: человек утюжит, не думая. Человек включается в decisions, не в events.

## Антипаттерны

- «На всякий случай всё через комитет» — очередь месяца и shadow IT.
- Fast Path на платежах/КИИ/необратимых миграциях — регуляторный риск.
- A3 без rejected options — решение непрозрачно для аудита.
