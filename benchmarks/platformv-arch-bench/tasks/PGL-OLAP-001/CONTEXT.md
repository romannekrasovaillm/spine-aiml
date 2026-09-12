---
context_id: PGL-OLAP-001-CTX
task_id: PGL-OLAP-001
version: "1.0.0"
language: ru
product: Platform V Pangolin DB 8.2.0 + Platform V DataMarts 2.4.0 (СберТех)
product_version_baseline: "Pangolin DB 8.2.0 / DataMarts 2.4.0"
docs_snapshot_date: "2026-09-02"
fact_labels:
  DOC: "факт из официальной документации СберТеха на дату снимка"
  BENCH: "вымышленные данные сценария бенчмарка; не относятся к реальному заказчику"
  ASSUME: "допущение составителей; решающий может его переопределить, явно указав это"
primary_sources:
  - https://platformv.sbertech.ru/docs/public/PSQ/8.2.0/common/      # документация Pangolin DB 8.2.0
  - https://platformv.sbertech.ru/docs/public/DMS/2.4.0/common/      # документация DataMarts 2.4.0
  - banking/plugins/ru-data/skills/pangolin-db-architecture/ (SKILL.md + references/, от <SPINE_REPO>)
  - banking/plugins/ru-data/skills/datamarts-mpp-analytics/ (SKILL.md + references/, от <SPINE_REPO>)
  - corpus/pv_docs/txt/psq_*.txt, corpus/pv_docs/txt/dms_*.txt (сырой съём, от <PVBENCH_HOME>)
---

# Контекст задачи PGL-OLAP-001

## 0. Как читать этот документ

- Раздел 1 — **факты о продуктах** `[DOC]` по документации Platform V
  Pangolin DB 8.2.0 и Platform V DataMarts 2.4.0 (съём 2026-09-02); на них
  можно опираться без проверки. В конце каждого подраздела — строка
  `Источник:` с относительным путём файла.
- Раздел 2 — **сценарий заказчика** `[BENCH]`: банк, система и цифры
  вымышлены; совпадения случайны.
- Всё вне §1 и §2 решающий помечает как допущение либо (режим `agentic`)
  подтверждает ссылкой на документацию. `[ТРЕБУЕТ ПРОВЕРКИ]` — страница не
  вошла в съём; как подтверждённый факт подавать нельзя, корректный приём —
  открытый вопрос к вендору.

---

## 1. Продукты `[DOC]`

### 1.1 Pangolin DB: что это и позиционирование относительно аналитики

- Platform V Pangolin DB — **специальная сборка PostgreSQL** с доработками
  под повышенные требования к безопасности, доступности, надёжности и
  эксплуатации (раздел «Назначение»). Продукт **подходит для OLTP-нагрузок
  (Online Transaction Processing)**. Дословно из раздела «Преимущества»:
  «Для решения задач аналитики рекомендуется использовать
  специализированные продукты». Т.е. Pangolin — **не MPP и не DWH**;
  аналитический профиль линейки Platform V — продукт DataMarts (§1.7).
- Классы задач из документации: высоконагруженные OLTP, 1С:Предприятие
  (отдельный отключаемый режим оптимизаций — без 1С он может давать
  деградацию планов), time-series через timescaledb из дистрибутива,
  хранилища ПДн/критичных данных.
- Аргумент в пользу Pangolin в **смешанных** нагрузках: **64-битные
  идентификаторы транзакций** снимают проблему wraparound ванильного
  PostgreSQL (пример из документации: длительный OLAP-запрос на ванили при
  ~50 000 TPS OLTP привёл бы к отказу в обслуживании OLTP примерно через
  12 часов; с 64-битными XID граница отодвигается на миллионы лет). Это
  снимает **один конкретный риск** совмещения нагрузок, но не отменяет
  вендорскую рекомендацию выносить аналитику.
- Жёсткие лимиты ядра (раздел «Ограничения»): размер отношения ≤ 32 ТБ,
  размер поля ≤ 1 ГБ (TOAST), ≤ 1600 колонок на таблицу; размер базы
  данных не ограничен.

Источник: banking/plugins/ru-data/skills/pangolin-db-architecture/SKILL.md; corpus/pv_docs/txt/psq_documents__about__advantages.html.txt; corpus/pv_docs/txt/psq_documents__about__pattern-reliable-operation-ha.html.txt; corpus/pv_docs/txt/psq_documents__about__limits.html.txt

### 1.2 Pangolin DB: редакции (лицензионная граница)

- Редакции: **Standard**, **Standard для ERP-систем**, **Enterprise**,
  **Enterprise для ERP-систем** (матрица «Описание редакций» для 8.2.0).
- Только Enterprise / Enterprise для ERP: **TDE** (прозрачное защитное
  преобразование данных), **защита данных от привилегированных
  пользователей**, защита параметров конфигурации, интеграция с KMS
  HashiCorp Vault, маскирование запросов, внешняя авторизация LDAP,
  **нативное интервальное партиционирование**, **глобальные индексы**,
  pgloader, pgcopydb, отчёты детального анализа истории активности.
- Во всех редакциях: управление парольными политиками, сквозная
  аутентификация Pooler ↔ СУБД, двухфакторная аутентификация, аудит
  событий безопасности по ГОСТ Р 59548-2022.
- Глобальные индексы, созданные на Enterprise, продолжают работать при
  понижении лицензии, но новые создавать нельзя. Редакция — вопрос
  лицензии; её фиксируют в ADR до проектирования схемы данных.

Источник: banking/plugins/ru-data/skills/pangolin-db-architecture/references/editions-limits.md; corpus/pv_docs/txt/psq_documents__description-of-functionality__index.html.txt

### 1.3 Pangolin DB: HA-кластер, репликация, DR на два ЦОД

- Стек кластера: **Pangolin Manager** (оркестратор; в разделе «Архитектура
  компонентов» поставляется документация Patroni 4.1.1 — Pangolin Manager
  является вендорской сборкой этого стека, CLI `pangolin-manager-ctl`
  patronictl-подобен), **Pangolin DCS** (распределённое хранилище
  конфигурации на базе **Raft**; из Patroni-наследия поддерживаются также
  etcd/ZooKeeper/Consul), **Pangolin Pooler** (форк PgBouncer).
- Целевые показатели кластера из документации: **RPO = 0** (нет потери
  данных при программном/аппаратном сбое) и **время простоя сервиса при
  сбое менее 6 минут в год** — через горячее резервирование двух
  экземпляров в разных ЦОД (Active/StandBy) и автоматический failover.
- Репликация физическая, потоковая, **однопоточная**; один лидер. Manager
  хранит запись о лидере в DCS (пара ключ-значение с TTL); при истечении
  TTL узлы сравнивают позиции журнала транзакций, первый записавший себя в
  DCS становится лидером. Операции: `switchover` (плановое, гарантия
  нулевых потерь даже в асинхронном режиме), `failover`, `reinit`
  (пересоздание реплики pg_basebackup), `pause`/`resume`.
- Режимы репликации (документ «Репликация»): **асинхронный (дефолт)** —
  потери в пределах `maximum_lag_on_failover` байт WAL + записи за
  последние `ttl` секунд (в среднем `loop_wait` = 2 с); **стандартная
  синхронная PostgreSQL** (`synchronous_commit: on` +
  `synchronous_standby_names`) — Manager трактует кластер как асинхронный,
  рекомендовано ≥ 2 реплик; **нестрогий синхронный Manager
  (`synchronous_mode: true`)** — при недоступности реплики лидер
  деградирует в async, доступность на запись сохраняется, failover только
  на реплику со всеми зафиксированными транзакциями; **строгий синхронный
  (`synchronous_mode_strict: true`)** — failover только на синхронную
  реплику, перевод в async запрещён: максимум сохранности ценой
  доступности.
- **Правило двух ЦОД** (документ «Мульти-ЦОД»): при только двух площадках
  документация предписывает два независимых DCS и **standby-кластер** во
  втором ЦОД (standby leader реплицируется с первичного, каскадные реплики
  — с него; scope кластеров различны; кластеры не знают друг о друге,
  кроме репликации). **Автоматическое повышение standby невозможно** (ЦОД-2
  не отличает сбой ЦОД-1 от сетевого разделения): promote — вручную
  удалением секции `standby_cluster` из динамической конфигурации (НЕ
  `pg_ctl promote`), перед promote — STONITH основного, иначе split-brain.
  Возврат — `pg_rewind` (требует `--data-checksums` и/или
  `wal_log_hints=on`) или пересоздание standby. При 3+ ЦОД возможен
  синхронный мульти-ЦОД кластер: минимум 3 узла СУБД, нечётное число узлов
  DCS (3 или 5).
- **DCS failsafe mode**: при полной недоступности DCS кластер продолжает
  работать, если все узлы видят лидера через его REST API.
- Имена узлов должны быть уникальны во всех связанных кластерах:
  совпадение application_name молча ломает синхронную репликацию.

Источник: banking/plugins/ru-data/skills/pangolin-db-architecture/references/ha.md; corpus/pv_docs/txt/psq_documents__administration-guide__administration-pangolin-manager__cluster.html.txt; corpus/pv_docs/txt/psq_documents__administration-guide__administration-pangolin-manager__replication.html.txt; corpus/pv_docs/txt/psq_documents__architecture-components__patroni__ha_multi_dc.html.txt; corpus/pv_docs/txt/psq_documents__architecture-components__patroni__standby_cluster.html.txt; corpus/pv_docs/txt/psq_documents__architecture-components__patroni__dcs_failsafe_mode.html.txt

### 1.4 Pangolin DB: пул соединений и N-ЦОД

- **Pangolin Pooler** — доработанный форк PgBouncer (в 8.2.0: Pangolin
  Pooler 3.2.0, ядро PgBouncer 1.24.0; порт по умолчанию 6544). Доработки:
  сквозная аутентификация пользователей (включая LDAP и аудит в лог
  пулера), prepared statements в транзакционном режиме, сертификаты
  PKCS#12 + Secret Management System, режим N-ЦОД, многопоточная версия.
- **Многопоточный Pooler** (только SberLinux; версия компонента 1.5.5 на
  момент документации): классический пулер однопроцессный, потолок —
  одно ядро CPU; многопоточный вариант распределяет соединения по
  воркерам с мониторингом утилизации потоков.
- Рекомендации вендора (документ «Рекомендации по настройке»):
  `max_connections` определяется нагрузочным тестом, стартовая формула
  `GREATEST(5 × CPU cores, 100)`, **одинаковое значение на Primary и
  StandBy** (иначе после failover часть клиентов не подключится); пулер
  нужен, когда `max_client_conn > max_connections`; `max_db_connections <=
  max_connections - superuser_reserved_connections - 30`; `min_pool_size =
  default_pool_size`; пример сбалансированной конфигурации (одна ТУЗ /
  одна БД): `max_client_conn=1000`, `pool_mode=transaction`,
  `min_pool_size=default_pool_size=max_db_connections=200`; проверка
  конфигурации — утилита Pangolin Tuner из дистрибутива.
- **N-ЦОД в Pooler**: режимы `NDC_ALLOWED`/`NDC_DISALLOWED`; команда
  `NDC_SUSPEND` рвёт клиентские и БД-соединения (кроме админ-консоли) и
  запрещает новые; `NDC_KEEPALIVE` возобновляет приём; параметр
  `ndc_suspending_timeout` (сек, 0 = выкл.) — если внешний арбитр ЦОД не
  шлёт keepalive, пулер САМ переходит в suspend (fail-closed: мёртвый
  арбитр = закрытый вход в БД).

Источник: banking/plugins/ru-data/skills/pangolin-db-architecture/references/partitioning-pooler.md; corpus/pv_docs/txt/psq_documents__administration-guide__administration-pangolin-pooler__advice.html.txt; corpus/pv_docs/txt/psq_documents__administration-guide__administration-pangolin-pooler__multi-pooler.html.txt; corpus/pv_docs/txt/psq_documents__administration-guide__administration-pangolin-pooler__support-data-center-topology.html.txt

### 1.5 Pangolin DB: партиционирование и глобальные индексы (Enterprise)

- **Нативное интервальное (авто-) партиционирование** (только Enterprise):
  `AUTO PARTITION BY RANGE (key) PERIOD (p) [OFFSET (o)]`, `AUTO PARTITION
  BY HASH (key) MODULUS (m)`, `AUTO PARTITION BY LIST (key)`; партиции
  создаются автоматически при вставке данных, не попадающих в существующие
  партиции; поддерживается вложенное автопартиционирование. **Пустые
  партиции при удалении данных НЕ удаляются.** **Схему
  автопартиционирования для уже созданной таблицы изменить нельзя** —
  решение о ключе и периоде принимается до наполнения. Ручное управление
  партициями совместимо, пересечения интервалов — ответственность
  проектировщика. Есть документированный переход с pg_pathman на
  декларативное партиционирование; оптимизация отсечения партиций
  (partition pruning) для композитного ключа.
- **Глобальные индексы** (только Enterprise): `CREATE [UNIQUE] INDEX ...
  ON partitioned_table (...) GLOBAL` — физически единый B-Tree над всеми
  партициями; уникальность по набору атрибутов, НЕ включающему ключ
  партиционирования (в ванильном PostgreSQL UNIQUE на партиционированной
  таблице обязан включать ключ). Ограничения: только B-Tree; не
  поддерживаются ограничения-исключения, субпартиционирование, Bottom-up
  Index Deletion, Deduplication; ряд DDL-комбинаций с автопартиционированием
  запрещён (полный список — в references/partitioning-pooler.md источника).
  Данные глобальных индексов подпадают под TDE.
- Смежные механизмы для высоких нагрузок (раздел «Надёжная эксплуатация
  высоконагруженных систем»): резервирование подключений для ролей
  (`pg_quota.conf` — в ванили резерв только для суперпользователя),
  управление планами запросов (фиксация/подмена плана на сервере без
  изменения текста в приложении), управление лёгкими блокировками (LWLock)
  под сценарии (read-mostly, много таблиц/JOIN/партиций, интенсивный
  буферный кеш), 64-битные XID (§1.1).

Источник: banking/plugins/ru-data/skills/pangolin-db-architecture/references/partitioning-pooler.md; corpus/pv_docs/txt/psq_documents__administration-guide__developer-functionality__native-partitioning.html.txt; corpus/pv_docs/txt/psq_documents__administration-guide__developer-functionality__global-indexes.html.txt; corpus/pv_docs/txt/psq_documents__administration-guide__developer-functionality__partition-pruning.html.txt

### 1.6 Pangolin DB: CDC и интеграции наружу

- **Логическая репликация** (публикация/подписка): снапшот + поток
  изменений в порядке коммитов; каскадирование; репликация между мажорными
  версиями и платформами. Доработка Pangolin — параметр
  `drop_repl_sync_slots_only_after_restart`.
- **wal2json** — плагин логического декодирования WAL: JSON на транзакцию
  (format-version 1) или на кортеж (2); опции include-xids / timestamp /
  schemas / types, filter-tables / add-tables (по схемам с экранированием),
  include-pk, include-lsn; требует `wal_level = 'logical'` + рестарт.
  Доступ к старым версиям строк UPDATE/DELETE — по REPLICA IDENTITY.
- **test_decoding** — тестовый модуль вывода логического декодирования.
- «Поддержка смещения значения LSN для облегчения работы с логической
  репликацией и CDC-решениями» (расширение `psql_logical_slot_rewind` —
  слот со сдвигом в прошлое).
- В «Основных функциональностях» заявлена **нативная поддержка работы с
  Platform V GraDeLy (GDL)** — отдельный CDC-продукт линейки («логическая
  репликация данных с захватом изменений на клиенте или из журнала БД»).
- **pgcopydb 0.14** (3rd Party, Enterprise): полная копия БД + CDC-follow
  (совместим с test_decoding и wal2json, дефолт test_decoding).
  Ограничения: follow **падает при перезапуске любой из БД**; требует
  суперпользователя на приёмнике; **follow не работает с TDE**.
- FDW: oracle_fdw 2.5.0, tds_fdw 2.0.3 (MS SQL/Sybase через FreeTDS).
- Утилиты разовой миграции (ora2pg 23.0 из `migration_tools/`, orafce
  4.4.0, pgloader 3.6.9 Enterprise) — для задач миграции, не для CDC.
- Шина линейки для CDC-контуров — Platform V Corax (Kafka-совместимый
  брокер).

Источник: banking/plugins/ru-data/skills/pangolin-db-architecture/references/migration-cdc.md; corpus/pv_docs/txt/psq_documents__programming-guide__logical-replication.html.txt; corpus/pv_docs/txt/psq_documents__extensions__test_decoding.html.txt; corpus/pv_docs/txt/psq_documents__administration-guide__administration-pangolin-pooler__support-data-center-topology.html.txt; corpus/pv_docs/txt/psq_documents__about__functions.html.txt; corpus/pv_docs/txt/psq_documents__extensions__pgcopydb.html.txt

### 1.7 DataMarts: что это и позиционирование

- Дословно из раздела «Концептуальная модель»: **«DMS представляет собой
  OLAP СУБД»**. Развёрнуто: «высокопроизводительная аналитическая СУБД с
  поддержкой SQL и колоночной моделью хранения данных»; целевые задачи —
  «интерактивная аналитика и построение информационных панелей»,
  «выполнение онлайн-запросов с задержкой менее секунды».
- **Ядро — ClickHouse**: в документации фигурируют ClickHouse Server
  (systemd-юнит `clickhouse-server.service`) и ClickHouse Keeper
  (`/usr/bin/clickhouse-keeper`); на index-странице продукта DMS назван
  «аналитической базой данных на базе open source ClickHouse».
  **Точная версия ядра ClickHouse в DMS 2.4.0 в публичных документах не
  указана** — [ТРЕБУЕТ ПРОВЕРКИ].
- Запись в DMS устроена как **пакетные INSERT + фоновые мутации**
  (UPDATE/DELETE отражаются в мониторинге метрикой «Частичная мутация»), а
  не как транзакционные точечные записи — под OLTP-нагрузку DMS не
  проектировать. Прямой запрет в документах не сформулирован, но модель
  записи это исключает по построению.
- Производительность (формулировки вендора из раздела «Преимущества»):
  «сотни миллионов строк в секунду», «петабайты данных на обычных жёстких
  дисках», векторная обработка («данные обрабатываются не по одной строке,
  а по вектору значений»), пропускная способность «до десятков гигабайт в
  секунду на чтение и запись». Это маркетинговые оценки, а не гарантия на
  конкретной конфигурации; вендор рекомендует нагрузочное тестирование на
  целевой конфигурации при горизонтальном масштабировании.
- Оговорка вендора: «использование продукта иными способами», чем описано
  в документации, — «работоспособность не гарантируется».

Источник: banking/plugins/ru-data/skills/datamarts-mpp-analytics/SKILL.md; corpus/pv_docs/txt/dms_about__conceptual-model.html.txt; corpus/pv_docs/txt/dms_about__objective.html.txt; corpus/pv_docs/txt/dms_about__advantages.html.txt; corpus/pv_docs/txt/dms_about__non-functional-features.html.txt; corpus/pv_docs/txt/dms_administration-guide__monitoring.html.txt; corpus/pv_docs/txt/dms_about__functions.html.txt

### 1.8 DataMarts: компонентный состав и кластер

- **`datamarts-server`** — сама СУБД (ClickHouse Server; пользователь ОС
  `clickhouse`; рабочие каталоги `/opt/datamarts/{clickhouse,keeper,
  safeguard}`). Рекомендовано **3+ узла**; горизонтальное масштабирование
  — «Да».
- **`datamarts-keeper`** — ClickHouse Keeper (консенсус **Raft**, конфиг
  `raft_configuration`, логи консенсуса и снапшоты Raft, digest-аутенти-
  фикация с superdigest из SecMan). Допускается внешний ZooKeeper.
  **RPM-пакет datamarts-keeper конфликтует с datamarts-server** — keeper
  ставится на отдельные серверы; «разверните как минимум три экземпляра
  datamarts-keeper на разных серверах», каждому — уникальный `server_id`.
- **`datamarts-safeguard`** — REST API управления нагрузкой (§1.11).
- **`datamarts-rrm` + `datamarts-rrm-udf`** — развёртывание ролевой модели
  (§1.12); **`datamarts-vault-provider`** — выпуск/перевыпуск
  wrapped_token для SecMan; аудит-компонент (метрики `audit_*`).
- **PSQ/PostgreSQL — часть архитектуры DMS**: «используется для управления
  пользователями, правами доступа и ведения аудита операций»;
  рекомендуемая СУБД — **Platform V Pangolin DB 5.2.2+**.
- Реплицированные таблицы — механизм **ReplicatedMergeTree**; на узлах
  настраиваются макросы (номера сегментов и реплик) и параметры репликации
  и сегментирования. Пример кластера из документации `cluster_1S_2R`
  (1 шард, 2 реплики): `internal_replication=true`, общий `<secret>`,
  `secure=1`, порт реплик 9000, keeper-порт 9181.
- Доступность **«до 99.99%»** при условиях: **минимум три реплики каждого
  сегмента данных**, datamarts-keeper/ZooKeeper, регулярное резервирование
  и мониторинг узлов. Надёжность: MTBF «более 1 года», Failure Rate
  «менее 0.1% в год», контрольные суммы при чтении/записи, автоматическое
  восстановление повреждённых сегментов.
- Движок Distributed и табличное шардирование в публичных страницах
  раскрыты лишь косвенно (макросы сегментов, `clusterAllReplicas` в
  safeguard) — полная конфигурация [ТРЕБУЕТ ПРОВЕРКИ].
- Системные требования: **Platform V SberLinux OS Server 8.7+** (един-
  ственная обязательная строка); опционально Pangolin DB 5.2.2+, Apache
  Kafka 0.9+ / Platform V Corax 12.381.1+, SecMan 01.015.03-06, DevOps
  Tools 1.9.1+, IDM 2.3.1+, Ansible 2.16+. На элемент datamarts-server:
  CPU запрос 4 ядра (лимит «зависит от нагрузки»), RAM запрос 8 ГБ (лимит
  «зависит от объёма данных»), диски 25+ ГБ. Стенд НТ вендора: 3 узла ×
  8 CPU / 32 ГБ RAM / 40 ГБ.

Источник: banking/plugins/ru-data/skills/datamarts-mpp-analytics/references/architecture-security.md; corpus/pv_docs/txt/dms_installation-guide__installation-comp-keeper.html.txt; corpus/pv_docs/txt/dms_administration-guide__administration-scenarios-dms-replication.html.txt; corpus/pv_docs/txt/dms_about__non-functional-features.html.txt; corpus/pv_docs/txt/dms_about__system-requirements.html.txt

### 1.9 DataMarts: таблицы, агрегаты, версионность

- Движки таблиц, названные в документации: `MergeTree`,
  `ReplicatedMergeTree`, `Kafka`, `PostgreSQL`, `Executable` (служебная).
- «Выбор правильных первичных ключей и индексация столбцов ускоряет
  доступ к данным» (паттерн «Агрегация больших объёмов данных»).
- **Материализованные представления**: «автоматическое обновление
  агрегатов при добавлении новых данных», хранение результатов в отдельной
  таблице, «возможность настраивать триггеры и условия обновления» —
  основной механизм предрасчёта витрин и регулярной отчётности.
- **Версионные таблицы**: «сохраняют историю изменений каждой строки и
  восстанавливают состояние на любой момент времени; минимальные затраты
  памяти благодаря специально реализованному механизму версионирования» —
  конкретный движок не назван — [ТРЕБУЕТ ПРОВЕРКИ].
- Загрузка — `INSERT INTO`; выгрузка — `SELECT INTO OUTFILE`.
- **Партиционирование таблиц и TTL данных в публичных страницах не
  раскрыты** — [ТРЕБУЕТ ПРОВЕРКИ] (TTL упоминается только для паролей
  SecMan и очистки служебной `metrics_history`).
- Документированные паттерны использования: агрегация больших объёмов;
  хранение исторических данных с версиями (аудит, состояние на момент
  времени); оптимизация запросов материализованными представлениями;
  мониторинг производительности приложений (DMS как хранилище метрик);
  анализ поведения пользователей в реальном времени («сотни тысяч событий
  в секунду», данные «через секунды после их совершения», оконные функции).

Источник: banking/plugins/ru-data/skills/datamarts-mpp-analytics/references/architecture-security.md; banking/plugins/ru-data/skills/datamarts-mpp-analytics/references/integrations-load.md; corpus/pv_docs/txt/dms_about__pattern-aggregation.html.txt; corpus/pv_docs/txt/dms_about__pattern-materialized-views.html.txt; corpus/pv_docs/txt/dms_about__pattern-historical-data.html.txt; corpus/pv_docs/txt/dms_about__pattern-users-analysis.html.txt

### 1.10 DataMarts: загрузка и интеграции

- **Белый список интеграций** продукта (таблица документации): LDAP,
  SecMan, white-list (списки доверенных адресов), Kafka, PostgreSQL, IDM.
- **Табличный движок Kafka**: `ENGINE = Kafka()` (параметры
  `kafka_broker_list`, `kafka_topic_list`, `kafka_group_name`,
  `kafka_format`, `kafka_security_protocol`, `kafka_sasl_mechanism`,
  `kafka_sasl_username`, `kafka_sasl_password`); сообщения «в формате
  JSONEachRow или другом указанном формате»; SASL PLAIN /
  SCRAM-SHA-256 / SCRAM-SHA-512 по `sasl_ssl`, Kerberos (GSSAPI) с ключами
  из SecMan. Требуемые версии брокеров: **Apache Kafka 0.9+ или
  Platform V Corax 12.381.1+**. Kafka служит и шиной событий между
  компонентами DMS (топик `data_marts_events`). Предписание документации:
  не передавать учётные данные открытым текстом.
- **Табличный движок PostgreSQL + `named_collections`** (config.xml:
  `host`, `port`, `user`, `database`, `schema`, `sslmode` вплоть до
  `verify-full`, сертификаты из SecMan; либо Kerberos
  `gssencmode=require`) — для чтения из внешних PostgreSQL/Pangolin и для
  downstream-связки с передачей Correlation ID (§1.11).
- **CDC как класс в публичных документах DMS не описан** — [ТРЕБУЕТ
  ПРОВЕРКИ]. Штатный потоковый входной паттерн — движок Kafka; то есть
  CDC из Pangolin идёт через внешний инструмент (в линейке Platform V —
  GraDeLy (GDL), §1.6) в брокер Kafka/Corax, далее движком Kafka в DMS.
- HTTP-интерфейс (порт 8123), JDBC/ODBC в публичных страницах не описаны —
  [ТРЕБУЕТ ПРОВЕРКИ]. HDFS-интеграция описана (Kerberos); FreeIPA —
  «получение актуальных keytab из FreeIPA engine».

Источник: banking/plugins/ru-data/skills/datamarts-mpp-analytics/references/integrations-load.md; corpus/pv_docs/txt/dms_installation-guide__install-integration.html.txt; corpus/pv_docs/txt/dms_installation-guide__installation-integration-kafka.html.txt; corpus/pv_docs/txt/dms_administration-guide__administration-scenarios-kafka-authentification.html.txt; corpus/pv_docs/txt/dms_administration-guide__administration-scenarios-connection-config.html.txt; corpus/pv_docs/txt/dms_installation-guide__installation-integration-postgre.html.txt

### 1.11 DataMarts: управление нагрузкой (datamarts-safeguard)

- **Профили настроек**: `CREATE SETTINGS PROFILE … SETTINGS
  max_memory_usage, max_execution_time, max_threads … READONLY` +
  `ALTER USER … SETTINGS PROFILE`; «для каждого параметра профиля
  устанавливается режим readonly, чтобы пользователи не могли
  самостоятельно обойти ограничения».
- **Граница управления**: safeguard управляет только профилями с префиксом
  `profile_prefix` (по умолчанию `beholder`); профили без префикса
  safeguard не трогает; «если одна настройка задана в нескольких
  профилях, порядок её применения не определён».
- **Правила токсичности**: блокировка пользователя предсозданной квотой с
  минимальными лимитами (дословно `queries: 1, read_bytes: 1,
  written_bytes: 1, result_bytes: 1`), имя квоты — `blocked_quota_name`;
  исключения `never_blocked_users`; запрещённые `banned_sql_keywords`.
  Мониторинг запросов онлайн и по истории (глубина по умолчанию 168 часов).
- **Расписания**: «в DMS не предусмотрен встроенный механизм автоматического
  переключения профилей настроек по расписанию» — его выполняет **фоновый
  процесс safeguard** (`cron.profile_schedule`), исполняющий `ALTER USER …
  ADD/DROP SETTINGS PROFILE` по таблице `settings_profiles_schedule`;
  постоянные назначения (`assign`) фоновый процесс не трогает.
- **Сквозная идентификация (Correlation ID)**: `SET tag_correlation_id =
  '<id>'` (или `SELECT … SETTINGS tag_correlation_id=…`); сегмент DMS в
  цепочке — `Click_<SYSTEM_ID>_<QUERY_ID>`; сегменты разделяются `|`;
  передача в downstream PostgreSQL — через движок PostgreSQL командой
  `SET LOCAL` (параметры `forward_correlation_id`,
  `correlation_id_parameter_name`). Назначение — сквозной трейсинг цепочек
  загрузки витрин.
- Доступ к safeguard: только mTLS + ТУЗ, аутентификация по CN клиентского
  сертификата X.509 (`subject_regexp` + белый список `allowed_certs`);
  healthcheck `/health` (200 / 5xx).

Источник: banking/plugins/ru-data/skills/datamarts-mpp-analytics/references/integrations-load.md; corpus/pv_docs/txt/dms_administration-guide__administration-scenarios-load-management.html.txt; corpus/pv_docs/txt/dms_installation-guide__installation-comp-safeguard.html.txt

### 1.12 DataMarts: безопасность, аудит, мониторинг

- **RBAC**: «механизм безопасности основан на управлении доступом на
  основе ролей»; объекты — учётные записи, роли, **политики строк**,
  профили настроек, квоты. «По умолчанию в системе нет других ролей кроме
  администратора — администратор сам создаёт ролевую модель». Развёртывание
  модели — через **datamarts-rrm**: запросы с `task_id` в служебную
  таблицу → UDF `udf_rrm` → проверка правил ограничений → исполнение,
  журнал `UDF_LOGS` (статусы OK / FAILED / INCOMPLETE). «В производственных
  средах рекомендуется отключать пользователя по умолчанию после
  настройки».
- **Аутентификация**: LDAP, Kerberos, внутренние схемы; TLS; проверка
  отзыва сертификатов **OCSP** (секции клиентских HTTPS, PostgreSQL,
  Kafka; кеш с TTL) — включена по умолчанию.
- **Секреты — SecMan**: wrapped_token (выпуск/перевыпуск —
  datamarts-vault-provider), AppRole-аутентификация, подстановки
  `${secman:…}` / `${vault:…}` в конфигах; «все секреты (клиентские
  сертификаты, пароль ТУЗ datamarts-server, сертификаты и приватные ключи
  сервера) загружаются из SecMan». TTL паролей локальных пользователей —
  из секрета. Документация предписывает не хранить пароли открытым текстом.
- **Аудит**: штатная передача событий аудита (источник —
  `query_log`/`session_log`, дедупликация, метрики `audit_*`); у
  safeguard — отдельный файл `/var/log/audit/datamarts-audit.log` в
  формате rsyslog; события аудита безопасности и техническая информация
  пишутся в разные файлы.
- **Мониторинг**: системные таблицы `system.metrics`, `system.events`,
  `system.metric_log`, `system.query_log`, `system.latency_log`; внешние
  системы «например в формате Prometheus» (mTLS) и Graphite; ключевые
  метрики: Запрос, Слияние, **Частичная мутация**, `rrm_tasks_executed/
  failed`, `vault_tokens_issued`.
- Механика резервного копирования в публичных страницах не раскрыта
  (функция заявлена в условиях доступности 99.99%, механика — нет) —
  [ТРЕБУЕТ ПРОВЕРКИ].

Источник: banking/plugins/ru-data/skills/datamarts-mpp-analytics/references/architecture-security.md; corpus/pv_docs/txt/dms_administration-guide__administration-scenarios-users-and-roles.html.txt; corpus/pv_docs/txt/dms_administration-guide__monitoring.html.txt; corpus/pv_docs/txt/dms_administration-guide__parameters.html.txt

### 1.13 Где искать подробности

| Раздел документации | URL |
|---|---|
| Корень документации Pangolin DB 8.2.0 | https://platformv.sbertech.ru/docs/public/PSQ/8.2.0/common/ |
| Корень документации DataMarts 2.4.0 | https://platformv.sbertech.ru/docs/public/DMS/2.4.0/common/ |

Разрешённые домены для режима `agentic` (web_fetch):
`platformv.sbertech.ru`, `sbertech.ru`.

---

## 2. Сценарий заказчика `[BENCH]`

### 2.1 Бизнес-контекст

АО «Банк "Прикамский Расчётный"» (вымышленный, топ-40 по активам)
эксплуатирует АС «Единый учёт кредитных портфелей» (АС ЕУКП) — систему
учёта кредитных договоров корпоративного и розничного портфеля, графиков
платежей, просроченной задолженности и резервов. Год назад АС ЕУКП
мигрирована на **Platform V Pangolin DB 8.2.0, редакция Enterprise**
(миграция завершена и в задачу не входит).

Отчётная нагрузка исторически исполняется в том же OLTP-контуре: часть
запросов — на пишущем лидере, часть — на синхронной реплике в ЦОД-1.
В отчётные окна (конец дня, закрытие месяца) фиксируется деградация p99
OLTP-транзакций до 2,5× от нормы; два инцидента за квартал классифицированы
как «значимые» службой мониторинга. Аналитики риск-функции жалуются на
отмены ad-hoc запросов по таймаутам.

Программа развития ДКА предписывает **выделить аналитический контур**
(витрины данных для регуляторной отчётности, управленческих дашбордов и
ad-hoc аналитики) без остановки работы АС ЕУКП. Кандидат-стандарт
аналитической СУБД банка — **Platform V DataMarts** (лицензия на пилотный
объём уже закуплена).

### 2.2 Ландшафт AS-IS

**OLTP-контур (Pangolin, ЦОД-1 + ЦОД-2):**

- Кластер: 1 лидер + 2 реплики в ЦОД-1 (одна — синхронная,
  `synchronous_mode: true`), standby-кластер (standby leader + 1 реплика)
  в ЦОД-2; два независимых кворума Pangolin DCS (по 3 узла) в каждом ЦОД;
  Pangolin Pooler перед лидером и перед синхронной репликой.
- Данные: **4,5 ТБ** (данные ~2,9 ТБ, индексы ~1,6 ТБ), рост **~0,9
  ТБ/год**; горячий период оперативных запросов — 13 месяцев; политика
  хранения в OLTP — 7 лет, далее выгрузка в архив. Крупнейшие таблицы
  (договоры, графики, платежи) партиционированы по месяцам нативным
  автопартиционированием.
- Нагрузка: пик **8 000 TPS** (короткие OLTP-транзакции, 65 % чтение /
  35 % запись); ~150 млн коммитов в сутки; приложение — Java/Spring Boot,
  40 подов Kubernetes × 20 соединений HikariCP (до ~800 клиентских
  соединений через Pooler в транзакционном режиме).
- Ночной регламент: расчёт резервов и реклассификация портфеля —
  до 60 млн обновляемых строк за окно 01:00–05:00.

**Отчётно-аналитическая нагрузка (сейчас — на OLTP-контуре):**

- Регламентная отчётность: **~120 отчётов в день** (из них ~60 «тяжёлых» —
  полные сканы/агрегации по портфелю, до 45 минут каждый); часть идёт на
  лидер, часть — на синхронную реплику.
- Управленческие дашборды: ~200 пользователей, опрос каждые 5 минут
  (агрегации «на утро» и «за текущий день»).
- Ad-hoc: до **30 аналитиков риск-функции одновременно**, запросы по
  истории до 7 лет; попытки полных выгрузок «в Excel».
- Витринный слой как таковой отсутствует — отчёты строятся SQL прямо по
  оперативным таблицам.

**Прочее:**

- В банке эксплуатируется кластер **Platform V Corax 16.392.0**
  (Kafka-совместимый брокер, контур интеграции); выделение новых топиков —
  по заявке, SLA 5 рабочих дней.
- CDC-инструмент Platform V GraDeLy в банке **не развёрнут** (лицензии
  нет); опыт команды по wal2json — отсутствует.
- Хранилище секретов: корпоративный **HashiCorp Vault** (используется
  Pangolin для TDE). **SecMan в банке не закуплен.**
- Мониторинг — Zabbix; журналирование — централизованный SIEM;
  привилегированный доступ — PAM.

### 2.3 Инфраструктура и ограничения `INF-xx`

| ID | Ограничение |
|---|---|
| INF-01 | Два ЦОД (ЦОД-1 «Основной», ЦОД-2 «Резервный»), выделенные каналы 2×10 Гбит/с, RTT 2–3 мс. Третьей площадки нет; есть «облачная зона» банка (до 2 лёгких ВМ 4 vCPU / 8 ГБ, сетево независимая от обоих ЦОД) |
| INF-02 | Под аналитический контур можно выделить до **5 серверов в каждом ЦОД** (2×16 ядер, 256 ГБ RAM, 4×3,84 ТБ NVMe каждый). Расширение существующего Pangolin-контура — только в рамках уже выделенных серверов |
| INF-03 | ОС на всех новых серверах — **Platform V SberLinux OS Server 8.10** |
| INF-04 | Corax 16.392.0 доступен для CDC-транспорта (требование DMS к брокеру — Corax 12.381.1+); размещение компонентов DMS на брокерах запрещено |
| INF-05 | Секреты — корпоративный Vault; покупка SecMan возможна не ранее следующего бюджетного цикла (9–12 месяцев) |
| INF-06 | Мониторинг — Zabbix (возможна интеграция с Prometheus-экспортом), аудит — SIEM; все административные сессии — через PAM |
| INF-07 | Команда: 4 DBA (Pangolin — уверенно), 2 администратора КХД (старый контур на коммерческом MPP, выводится), 1 администратор ИБ; обучение у вендора возможно. Развёртывание — только Ansible из корпоративного репозитория |
| INF-08 | Окно планового простоя АС ЕУКП: 1 раз в квартал, ночь сб/вс, **не более 4 часов**. Аналитический контур допускает плановый простой до 8 часов вне отчётных дат |
| INF-09 | Новые выделенные каналы ЦОД↔облачная зона пропускают только TCP; пропускная способность до зоны — 1 Гбит/с |

### 2.4 Нефункциональные требования `NFR-xx`

| ID | Требование | Целевое значение |
|---|---|---|
| NFR-01 | Доступность OLTP-контура в операционное время (05:00–23:00 МСК) | ≥ 99,95 % в год |
| NFR-02 | RPO при отказе узла OLTP внутри ЦОД-1 | **0** |
| NFR-03 | Влияние отчётно-аналитической нагрузки на OLTP (p99 транзакций) | деградация ≤ 5 % в любые окна, включая закрытие месяца |
| NFR-04 | Актуальность управленческих витрин («данные за текущий день») | лаг ≤ 15 мин от коммита в OLTP |
| NFR-05 | Регламентная отчётность на утро | к 08:00 T+1 (данные на закрытие дня) |
| NFR-06 | Ad-hoc запросы аналитиков по витринам до 2 млрд строк | p95 ≤ 5 с |
| NFR-07 | Доступность аналитического контура в операционное время | ≥ 99,9 % |
| NFR-08 | Ёмкость аналитического контура | история 7 лет (из OLTP: 4,5 ТБ текущий объём + 0,9 ТБ/год) + агрегаты + 30 % запас |
| NFR-09 | Ночной регламент OLTP (60 млн обновлений) | ≤ 4 ч, не должен срывать NFR-04 утром |
| NFR-10 | DR аналитического контура | RTO ≤ 4 ч; RPO ≤ 24 ч (допускается восстановление витрин повторной загрузкой из OLTP) |
| NFR-11 | Обновления СУБД обоих контуров | без простоя OLTP; аналитика — в окне INF-08 |
| NFR-12 | Изоляция нагрузок в аналитическом контуре | токсичные ad-hoc запросы не должны срывать SLA регламентной отчётности (NFR-05) |

### 2.5 Требования информационной безопасности `SEC-xx`

| ID | Требование |
|---|---|
| SEC-01 | Данные кредитных договоров (ПДн + банковская тайна) в OLTP шифруются «на диске» (уже реализовано TDE с ключами в Vault); для аналитического контура требуется эквивалентная защита либо обоснованная минимизация состава чувствительных данных в витринах |
| SEC-02 | Аналитики риск-функции не должны видеть ПДн клиентов (ФИО, паспортные данные); агрегаты и деперсонализированные атрибуты — допустимы |
| SEC-03 | Доступ в обе СУБД — ролевой, с минимальными правами; административные сессии персонализированы и идут через PAM; пользователь по умолчанию в DMS отключён |
| SEC-04 | TLS на всех соединениях: приложение ↔ Pooler ↔ Pangolin, CDC-канал ↔ Corax ↔ DataMarts, DataMarts ↔ служебная PostgreSQL, административные API |
| SEC-05 | Секреты компонентов — в корпоративном хранилище; открытые пароли в конфигурационных файлах запрещены (в т.ч. в конфигурации движка Kafka в DMS) |
| SEC-06 | Аудит действий пользователей и администраторов обоих контуров с передачей в SIEM |
| SEC-07 | ТУЗ загрузки CDC имеет в Pangolin права только на чтение реплицируемых таблиц; ТУЗ записи в DMS — только на целевые витрины |
| SEC-08 | Политики строк в витринах: аналитик видит данные только своего макрорегиона |

### 2.6 Организационные ожидания `[BENCH]`

- Архитектурный комитет ожидает **одно рекомендуемое решение** плюс
  1–2 отвергнутые альтернативы с причинами.
- Комитет особенно чувствителен к трём вопросам: (а) честная граница —
  что принципиально остаётся в Pangolin, а что уходит в DataMarts;
  (б) реальный лаг данных в витринах и его влияние на регламентную
  отчётность T+1; (в) кворумы координационных слоёв обоих контуров при
  двух ЦОД.
- Решение должно быть реализуемо силами INF-07; там, где документация не
  раскрывает механику (`[ТРЕБУЕТ ПРОВЕРКИ]`), комитет ожидает явный
  список вопросов к вендору, а не домыслы.

### 2.7 Что можно принять допущением `[ASSUME]`

- Доработка приложения АС ЕУКП допустима в части строк подключения,
  маршрутизации чтений и перевода отчётных модулей на SQL диалекта
  аналитической СУБД; переписывание транзакционной логики — вне рамок.
- Брокер Corax имеет свободную ёмкость под поток CDC (~оценить из данных
  AS-IS) и допускает выделенные топики с retention ≥ 72 ч.
- Лицензии: Pangolin Enterprise — действующая; DataMarts — пилотная,
  расширение на промышленный объём согласуется отдельно (архитектурное
  решение не должно зависеть от лицензионных деталей).
- Версия DataMarts на момент внедрения — 2.4.0 или новее; точную версию
  ядра ClickHouse решающий не знает и должен пометить как вопрос к
  вендору.

---

## 3. Глоссарий

| Термин | Значение |
|---|---|
| OLTP / OLAP | оперативная (транзакционная) / аналитическая обработка |
| CDC | Change Data Capture — захват изменений из журнала СУБД |
| DCS | Distributed Configuration Store — хранилище состояния кластера Pangolin (Pangolin DCS на Raft; etcd/ZooKeeper/Consul из Patroni-наследия) |
| Switchover / Failover | плановое / аварийное переключение роли лидера Pangolin |
| Standby-кластер | DR-конфигурация Pangolin на 2 ЦОД с ручным promote |
| Keeper | datamarts-keeper — ClickHouse Keeper, консенсус Raft для кластера DataMarts |
| Сегмент (DMS) | шард данных кластера DataMarts; реплики сегмента — через ReplicatedMergeTree |
| Safeguard | datamarts-safeguard — REST API управления нагрузкой DMS (профили, токсичность) |
| МП | материализованное представление |
| ТУЗ | техническая учётная запись |
| Correlation ID | сквозной идентификатор цепочки загрузки/запроса |
| SecMan | Secret Management System линейки Platform V |
| RPO / RTO | допустимая потеря данных / допустимое время восстановления |
