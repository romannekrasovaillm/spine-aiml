# Amazon Builders' Library — полный каталог (Wayback Machine)

Источник: https://aws.amazon.com/builders-library/ (блокируется DPI провайдера; снапшоты — web.archive.org).
Скиллы плагина помечены ✅ — они дистиллированы полностью. Остальные — аннотация по каталогу.

## Таймауты, ретраи, перегрузка

- ✅ timeouts-retries-and-backoff-with-jitter — Брукер: перцентильные таймауты, эгоистичные ретраи, джиттер.
- ✅ using-load-shedding-to-avoid-overload — Янацек: goodput, дешёвый отказ, LIFO, timeout hints.
- ✅ avoiding-overload-in-distributed-systems-by-putting-the-smaller-service-in-control — Маджеррамов: control/data plane, push, S3-конфиг.
- ✅ avoiding-insurmountable-queue-backlogs — Янацек: бимодальность, shuffle-sharding, DLQ, heartbeat.
- ✅ fairness-in-multi-tenant-systems — Янацек: квоты, token bucket, admission control.
- resilience-lessons-from-the-lunch-rush — подготовка к пиковым нагрузкам на примере Prime Day/продаж.
- lessons-learned-addressing-cascading-failures — разбор каскадных сбоев AWS и контрмеры.

## Надёжность дизайна

- ✅ avoiding-fallback-in-distributed-systems — Габриэльсон: почему fallback хуже, чем его отсутствие.
- ✅ challenges-with-distributed-systems — Габриэльсон: 8 мод отказа, UNKNOWN.
- ✅ static-stability-using-availability-zones — Вайс/Фурр: статическая стабильность, зональная арифметика.
- ✅ leader-election-in-distributed-systems — Брукер: leases, два лидера, TLA+.
- fault-isolation-boundaries — границы изоляции отказов (зоны, ячейки, шарды).
- dependency-isolation — изоляция зависимостей: уменьшение blast radius.
- amazon-approach-to-building-resilient-services — общий подход Amazon к resilience.
- amazon-approach-to-failing-successfully — проектирование на случай сбоя.
- beyond-five-9s-lessons-from-our-highest-available-data-planes — уроки самых доступных data planes.
- making-your-site-reliably-available — доступность сайта: практики.
- choosing-a-distributed-database-solution-with-cap-theorem-in-mind — выбор БД с учётом CAP.
- journaling-architectures-for-durability-and-evolution — журналирующие архитектуры (durability+эволюция схем).
- architecting-and-operating-resilient-serverless-systems-at-scale — serverless resilience на масштабе.

## Деплой и эксплуатация

- automating-safe-hands-off-deployments — полностью автоматические безопасные деплои.
- ensuring-rollback-safety-during-deployments — безопасность откатов (backward compat).
- going-faster-with-continuous-delivery — CD в Amazon.
- automating-continuous-delivery-pipelines-at-amazon — конвейеры CD Amazon.
- amazon-approach-to-high-availability-deployment — деплой без влияния на доступность.
- cicd-pipeline — анатомия CI/CD пайплайна.
- amazon-approach-to-security-during-development — безопасность в процессе разработки.

## Наблюдаемость и операции

- instrumenting-distributed-systems-for-operational-visibility — инструментовка распределённых систем.
- building-dashboards-for-operational-visibility — дашборды для операционной видимости.
- operational-excellence-at-amazon — operational excellence как дисциплина (COE, post-mortem).
- implementing-health-checks — Янацек: health checks (shallow vs deep), ловушки.
- caching-challenges-and-strategies — кэширование: стратегии и риски.
- monitoring-network-performance-with-open-source-tools — мониторинг сети open-source.
- implementing-distributed-rate-limiting-using-redis — rate limiting через Redis.

Wayback-шаблон доступа: `https://web.archive.org/web/2024/https://aws.amazon.com/builders-library/<slug>/`
