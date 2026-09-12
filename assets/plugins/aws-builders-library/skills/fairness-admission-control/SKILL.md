---
name: fairness-admission-control
description: Fairness и admission control в мультитенантных системах (David Yanacek, AWS) — rate-based квоты на tenant/workload, token bucket (составные для burst), распределённый admission control (локальный/consistent hashing/gossip), видимость квот и bursting, 429 vs 503, проектирование API во избежание квот (change streams, bulk API, проекция метаданных). Используй этот навык ВСЕГДА при проектировании shared-платформ и Open Banking API, при настройке лимитов шлюза, при разборе «один клиент задушил всех», при выборе single-tenant vs multi-tenant.
---

# Fairness в мультитенантных системах (David Yanacek, AWS)

Цель: каждому клиенту shared-системы — опыт single-tenant (предсказуемые latency и доступность независимо от соседей). Всплеск одного тенанта отсекается именно у него, остальные живут.

## Механика

- **Rate-based квоты** (и квоты конкурентности) по tenant/workload/API. 429 (+Retry-After) — «клиент превысил квоту»; 5xx — «сервер не смог» (legacy AWS отдаёт 503 — обратная совместимость).
- **Token bucket**: burst capacity = запас токенов; двойной bucket (высокий burst + низкая ставка) — гибкость без безграничного всплеска. Guava RateLimiter как эталон.
- **Распределённый AC**: (а) локальный с делением квоты на число серверов (работает при равномерном балансировании); (б) consistent hashing throttle-keys на rate-tracker флот (осторожно: hotspot на горячем ключе — градиентный откат на локальный контроль); (в) gossip-обмен наблюдаемыми ставками (точность vs масштаб).
- **Высокая кардинальность** (квота на IP/строку/объект): Heavy Hitters / Counting Bloom — ограниченная память с известной погрешностью.
- **Реактивный AC**: быстрая смена правил в проде (динамическая конфигурация + аудит); новые лимиты сначала в evaluation-режиме (меряем, что БЫЛО бы отброшено), потом live.
- **Точность**: логи с throttle key + лимитом; анализ true/false positive/negative отказов (CloudWatch Logs Insights/Athena).

## Клиентский опыт (главное!)

- Видимость: метрики «насколько ты близок к квоте» + алармы; сервис алертит, когда режет много клиентов сразу.
- Bursting: незанятая ёмкость доступна сверх квоты, но «заёмная» ёмкость отбирается мгновенно, когда владелец пришёл за своей долей; сигнал клиенту — «ты на заёмной».
- Квоты растут с органическим ростом клиента (auto-increase); квота как фича (cost control в Lambda concurrency).
- **API-дизайн во избежание квот**: change stream вместо поллинга (CloudTrail вместо DescribeInstances), экспорт манифестов (S3 Inventory), bulk-API для массовых записей (IoT Bulk Provisioning с файлом результатов), проекция control-plane данных туда, где они нужны (EC2 instance metadata — убийца массовых Describe-вызовов).

## Банковский маппинг

Open Banking/партнёрские API: квота — часть договора-тарифа; 429+Retry-After всегда; burst для зарплатных дней партнёра с авто-сигналом; антифрод-лимиты ≠ rate-лимиты (разные контуры).

Источник: https://aws.amazon.com/builders-library/fairness-in-multi-tenant-systems/
