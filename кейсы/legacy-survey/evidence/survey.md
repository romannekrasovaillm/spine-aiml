---
title: Карта обследования (reverse discovery)
repo: monolith
created_at: 2026-09-04
expires_at: 2026-12-03
generator: arch-be survey
---

# Карта обследования: monolith

> Файл ПОЛНОСТЬЮ генерируется `arch-be survey`: повторный прогон
> перезаписывает его целиком. Человеческие/LLM-дополнения `[inferred]` —
> в соседний `survey-notes.md` (survey его никогда не трогает).
> Метки: `[confirmed]` — доказано кодом/конфигом (ссылка `файл:строка`);
> `[gap]` — пробел, нужен владелец домена. Срок годности — до
> `expires_at`, затем карту пересобирают (living-spec, не разовый аудит).

## 1. Карта компонентов и владельцев
- [confirmed] стек: Python (requirements) (requirements.txt:1)
- [confirmed] каталог `billing/` — 4 файлов (billing/)
- [confirmed] каталог `drop/` — 3 файлов (drop/)
- [confirmed] каталог `migrations/` — 3 файлов (migrations/)
- [confirmed] каталог `scripts/` — 3 файлов (scripts/)
- [confirmed] каталог `config/` — 2 файлов (config/)
- [confirmed] каталог `importer/` — 2 файлов (importer/)
- [confirmed] каталог `notifier/` — 2 файлов (notifier/)
- [confirmed] каталог `ops/` — 2 файлов (ops/)
- [confirmed] файлов в корне репозитория: 2 (./)

## 2. Точки входа (API, джобы, очереди)
- [confirmed] HTTP route (fastapi/flask): @app.get("/health (billing/worker.py:17)
- [confirmed] HTTP route (fastapi/flask): @app.post("/charge (billing/worker.py:22)
- [confirmed] консьюмер очереди (pika): channel.basic_consume(queue="payments.notify", on_message_callback=on_event) (notifier/sender.py:24)
- [confirmed] упоминание Rabbit: url = "amqp://guest:guest@rabbit.internal.example:5672/" (config/notifier.toml:2)
- [confirmed] упоминание Rabbit: (записывает её биллинг!) и рассылает уведомления через RabbitMQ.""" (notifier/sender.py:2)
- [confirmed] упоминание Rabbit: RABBIT_URL = os.environ.get("RABBIT_URL", "amqp://guest:guest@rabbit.internal.example:5672/") (notifier/sender.py:9)
- [confirmed] упоминание Rabbit: params = pika.URLParameters(RABBIT_URL) (notifier/sender.py:21)
- [confirmed] упоминание Rabbit: # Перекладка зависших сообщений обратно в очередь RabbitMQ (раз в неделю). (scripts/requeue_failed.sh:2)
- [confirmed] упоминание Rabbit: rabbitmqadmin --host rabbit.internal.example list queues name messages_ready (scripts/requeue_failed.sh:4)
- [confirmed] расписание планировщика: `crontab` (crontab:1)
- [confirmed] расписание планировщика: `ops/systemd/billing.timer` (ops/systemd/billing.timer:1)

## 3. Модель данных (миграции, SQL)
- [confirmed] каталог миграций `migrations/` — 3 файлов (migrations/)
- [confirmed] SQL вне миграций: `scripts/report.sql` (scripts/report.sql:1)

## 4. Интеграции (host:port из конфигов)
- [confirmed] внешний endpoint `corebank.internal.example:8443` (config/app.yaml:4)
- [confirmed] внешний endpoint `db.internal.example:5432` (config/app.yaml:10)
- [confirmed] внешний endpoint `rabbit.internal.example:5672` (config/notifier.toml:2)
- [confirmed] внешний endpoint `rates.internal.example:8080` (config/app.yaml:6)

## 5. Скрытые связи (общие таблицы, файловый обмен)
- [confirmed] общая таблица `payments` — используется из: billing, importer, migrations, notifier, scripts (billing/worker.py:29, importer/import_csv.py:29, migrations/0001_init.sql:2, notifier/sender.py:15, scripts/report.sql:6)
- [confirmed] каталог файлового обмена: `drop/` (drop/)

## 6. Тесты и наблюдаемость
- [confirmed] каталог тестов `billing/tests/` — 1 файлов (billing/tests/)

## 7. Долг и трупы
- [confirmed] маркер долга в имени файла: `billing/legacy_export.py` (billing/legacy_export.py:1)
- [confirmed] закомментированный feature-флаг: FEATURE_DYNAMIC_TARIFF=false (billing/worker.py:11)
- [confirmed] закомментированный feature-флаг: FEATURE_STRICT_RECONCILE=0 (scripts/nightly_reconcile.sh:3)

## 8. Ограничения платформы (версии из манифестов)
- [gap] Версии платформы не объявлены — какие рантаймы/зависимости, их EOL и лицензии?

## 9. Риски изменения (churn по git-истории)
- [gap] Git-история недоступна — где хрупкие места по истории инцидентов/коммитов?

