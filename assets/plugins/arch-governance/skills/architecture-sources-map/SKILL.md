---
name: architecture-sources-map
description: Карта 15 тематических блоков архитектурных источников Spine (168 источников: дисциплина решений, риск, описание архитектуры, NFR, устойчивость, интеграции, API-контракты, границы доменов, данные, безопасность и комплаенс, поставка и policy-as-code, наблюдаемость, стоимость, миграции, ИИ-системы) с пометками, что уже встроено в Spine и что кандидат на дистилляцию. Используй, когда пополняешь библиотеку правил и плагинов, ищешь источник для нового правила, скилла или ADR-поля, планируешь волну внедрения.
---

# Карта архитектурных источников Spine

Пространство выбора для пополнения библиотеки: 12 источников первой редакции (блок 0, встроены) + 156 новых по 15 блокам. Источник берётся не за авторитетность, а за механику: правило, поле шаблона, гейт или исполняемая проверка; всё, из чего извлекается только эрудиция, остаётся литературой. Навигация: нашёл блок → дистиллируй по карточке из 7 полей (`fitness-function-catalog`) → проверь фильтром из пяти форм (`rule-library-antipatterns`).

**Блок 0. База (встроено)**: ADR-дисциплина (Nygard), ASR (SEI), fitness-функции, C4, arc42, закон Конвея, walking skeleton, ATAM/NFR, паттерны Ричардсона, облачная устойчивость, TOGAF ADM (гейты A0–A5), обратимость. Дальше — кандидаты.

**Блок 1. Дисциплина решений** — MADR, log4brains, Design Docs at Google, Y-statements, Hohpe (решение как опцион), RFC 2119, Zalando. Встроено: adr_new, статусы. Кандидат: обязательные Alternatives (красный на A2), неретроактивность, лексика MUST NOT вместо «желательно».

**Блок 2. Значимость и риск** — Fairbanks (risk-driven), ATAM/QAW, risk-storming, Cynefin, Richards/Ford, Hard Parts (квант, саги), Haiku. Встроено: significance_score, маршруты. Кандидат: карта рисков поверх C4 → Critical; лимит инвариантов на систему (3–7 драйверов).

**Блок 3. Описание архитектуры** — ISO 42010:2022, Rozanski & Woods, 4+1, SEI Views and Beyond, Structurizr DSL, Diátaxis, ArchiMate, Zachman, IT4IT. Встроено: C4-mermaid. Кандидат: «диаграмма без адресата удаляется», диаграмма порождается из модели, типы документации в handoff.

**Блок 4. NFR как бюджеты** — ISO 25010:2023, SRE (SLI/SLO, burn-rate), USE/RED, USL, три Well-Architected, ГОСТ Р 57580.3/.4, положения ЦБ (683-П, 719-П, 757-П, 851-П, 716-П). Встроено: nfr-design. Кандидат: RTO/RPO из критичности актива, SLO без алертинга не внедрён, критичная архитектура → Critical автоматически.

**Блок 5. Устойчивость** — Release It!, AWS timeouts/retries/jitter, Azure Patterns, cell-based, DR-стратегии, Chaos Engineering/FIS, Fallacies, Jepsen, каскады (SRE), resilience4j. Встроено: patterns-resilience, aws-builders-library. Кандидат: «нет вызова без таймаута» грепом, бюджет ретраев на цепочку, NFR деградации, chaos-прогон перед A4.

**Блок 6. Интеграции** — EIP (65 паттернов), многомерная связанность (Hohpe), outbox, Debezium, AsyncAPI, Schema Registry, buf, Kafka, W3C Trace Context, Stopford, Newman. Встроено: patterns-integration. Кандидат: поле «какое измерение связанности снимаем/добавляем», asyncapi.yaml на тему, идемпотентный потребитель.

**Блок 7. API-контракты** — Google AIP, Zalando, Microsoft, OpenAPI, RFC 9457, Idempotency-Key, RFC 8594 (Sunset), OAuth 2.1/OIDC, FAPI 2.0, Pact, protobuf, GraphQL. Самый механизируемый блок — ядро волны 1. Кандидат: openapi.yaml + линтер как гейт A2, один формат ошибки, pact verify как evidence A4.

**Блок 8. Границы и домены** — DDD, Context Mapping, EventStorming, Team Topologies, Modular Monolith, BFF, Micro Frontends, Wardley, Tech Radar. Встроено: тест принадлежности инвариантов. Кандидат: тип связи контекстов в Binds, события до сервисов на A1, «распределённость требует ADR, монолит — нет».

**Блок 9. Данные** — DDIA, DAMA-DMBOK, Data Mesh, data contract, Medallion, Kimball, PACELC, OpenLineage, 152-ФЗ, Parallel Change. Необратимые решения — про данные, а не сервисы. Кандидат: гарантии по данным в каждом ADR, владелец данных блокирует A2, expand-migrate-contract.

**Блок 10. Безопасность и комплаенс** — NIST 800-207/207A (ZTA), CSF 2.0, SSDF, SLSA, OWASP ASVS/API Top 10, threat modeling, LINDDUN, ATT&CK, CIS, PCI DSS, ГОСТ Р 57580.1/.2, 187-ФЗ (КИИ), реестр российского ПО, DORA (EU). Регуляторика — готовые инварианты. Кандидат: уровень защиты как вход инвариантов, threat-model.md блокирует гейт, provenance артефакта.

**Блок 11. Поставка и policy-as-code** — Continuous Delivery, Trunk Based, DORA, Feature Toggles, Blue-Green/Canary, Argo Rollouts, OpenGitOps, ArchUnit, dependency-cruiser, OPA, Kyverno, Checkov, Backstage, CNCF Platforms, Twelve-Factor, SPACE. Кандидат: «правило не в конвейере — не правило», флаг с владельцем и датой, аудит→принуждение; здесь же главный ограничитель — правило → возможность платформы.

**Блок 12. Наблюдаемость и эксплуатация** — OTel semconv, Observability Engineering, postmortem culture, Being On-Call, Incident Response. Встроено: handoff-пакет. Кандидат: must_contain по service.name/deployment.environment, handoff по списку дежурного, инцидент → пересмотр ADR.

**Блок 13. Стоимость** — FinOps, Cost Optimization/Sustainability Pillars, SCI. Бюджет, который растёт молча. Кандидат: «стоимость на транзакцию» с методом измерения, превышение — триггер пересмотра.

**Блок 14. Миграции и наследие** — Strangler Fig, Branch by Abstraction, Parallel Run, 7 R, Feathers (seams), ACL, Big Ball of Mud. Встроено: strangler-acl. Кандидат: полная переписка — отдельный ADR, parallel run для денежных потоков, стратегия называется явно.

**Блок 15. ИИ-системы** — Anthropic Building Effective Agents, Azure orchestration, MCP, MLOps 0/1/2, Hidden Tech Debt (CACE), OWASP GenAI, OTel GenAI semconv, EU AI Act. Недетерминированный компонент в детерминированном контуре. Встроено: aws-agentic-ai. Кандидат: «агент требует обоснования, workflow — нет», инвариант полномочий, интеграция только через MCP, NFR «стоимость на задачу».

Порядок внедрения — три волны (см. `fitness-function-catalog`): волна 1 — блоки 7, 6, 5, 11; волна 2 — блоки 10 и 4; волна 3 — блоки 8, 9, 3, 14, 15. Реалистичный шаг — 25–40 правил первой волны.

Источник: «Источники solution-архитектуры, отработанные для Spine» (расширенное издание, 168 источников, URL проверены веб-поиском 31.08.2026).
