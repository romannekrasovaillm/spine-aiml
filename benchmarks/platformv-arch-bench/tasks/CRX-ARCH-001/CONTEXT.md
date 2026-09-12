---
context_id: CRX-ARCH-001-CTX
task_id: CRX-ARCH-001
version: "1.0.0"
language: ru
product: Platform V Corax Event Streaming (СберТех)
product_version_baseline: "16.392.0"
docs_snapshot_date: "2026-09-02"
fact_labels:
  DOC: "факт из официальной документации Platform V Corax 16.392.0 на дату снимка"
  BENCH: "вымышленные данные сценария бенчмарка; не относятся к реальному заказчику"
  ASSUME: "допущение составителей; решающий может его переопределить, явно указав это"
primary_sources:
  - https://platformv.sbertech.ru/docs/public/KFK/16.392.0/common/
  - banking/plugins/ru-integration/skills/corax-event-streaming/ (SKILL.md + references/, от <SPINE_REPO>)
  - corpus/pv_docs/txt/kfk_*.txt (сырой съём, от <PVBENCH_HOME>)
---

# Контекст задачи CRX-ARCH-001

## 0. Как читать этот документ

- Раздел 1 — **факты о продукте** `[DOC]` по документации Platform V Corax
  16.392.0 (съём 2026-09-02); на них можно опираться без проверки. В конце
  каждого подраздела — строка `Источник:` с относительным путём файла.
- Раздел 2 — **сценарий заказчика** `[BENCH]`: банк, система и цифры
  вымышлены; совпадения случайны.
- Всё вне §1 и §2 решающий помечает как допущение либо (режим `agentic`)
  подтверждает ссылкой на документацию. `[ТРЕБУЕТ ПРОВЕРКИ]` — страница не
  вошла в съём; как подтверждённый факт подавать нельзя.

---

## 1. Продукт: Platform V Corax `[DOC]`

### 1.1 Что это и состав

- «Программный брокер сообщений… распределенная, отказоустойчивая,
  реплицированная и легко масштабируемая система передачи сообщений»;
  «все сообщения Platform V Corax хранит на диске».
- Состав компонента Corax (KFKA): **Apache Kafka** (обязателен), **Apache
  ZooKeeper** (по таблице состава обязателен — но см. KRaft в §1.2),
  **Corax Schema Registry**, **Corax UI**, **Corax Mirror Maker 2**
  (трое последних опциональны).
- Против ванильного Apache Kafka добавлены: Schema Registry, Corax UI,
  **Auto Data Balancer**, мониторинг (consumer lag, Prometheus exporter),
  Ansible-роль, функции безопасности данных, патчи без CVE, поддержка;
  заявлены kTLS и SecMan.
- Ядро — Apache Kafka; «наследуя архитектуру **Apache Kafka 3.9**»
  поддерживает exactly-once транзакции. Точная версия ядра в дистрибутиве
  16.392.0 — [ТРЕБУЕТ ПРОВЕРКИ].
- Клиентский API: брокеры ядра 0.10.0+; транзакционные API — 0.11.0+
  (иначе `UnsupportedVersionException`). Идемпотентный и транзакционный
  продюсер — начиная с Corax 5.272.0.
- Java: OpenJDK/SberJDK 17 и 21; с 16.392.0 OpenJDK 11 не поддерживается.

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/SKILL.md; corpus/pv_docs/txt/kfk__documents__about__objective.html.txt

### 1.2 Координационный слой: ZooKeeper-ансамбль или KRaft

- Два режима метаданных: ZooKeeper-ансамбль или KRaft-контроллеры (шаг 2
  Ansible-установки — «kraft controller или zookeeper»). НФТ: «метаданные
  кластера хранятся в отказоустойчивом хранилище (ZooKeeper-ансамбль или
  KRaft-контроллеры), что исключает единую точку отказа». Совмещение
  допустимо, но «брокер и KRaft-контроллер рекомендуется разворачивать на
  отдельных хостах».
- ZooKeeper: «минимальное число узлов… — 3», «только нечетное количество
  узлов», работоспособен при живом строгом большинстве; standalone «не
  рекомендован для использования в промышленной эксплуатации».
- Конфиг ансамбля: `server.N=ip:2888:3888`, `clientPort=2181`,
  обязательные `tickTime`/`initLimit`/`syncLimit`; поддерживаются
  иерархические кворумы (`group.x`/`weight.x`). Старые транзакционные
  логи и снепшоты ZooKeeper с диска не удаляет — чистка на администраторе.
- Страниц конфигурации KRaft (`process.roles`, `controller.quorum.voters`)
  в съёме нет — [ТРЕБУЕТ ПРОВЕРКИ]. Расхождение источников (ZooKeeper
  «обязателен» vs KRaft-режим) трактовать как «ZooKeeper обязателен в
  ZK-режиме».

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/cluster-ha.md; corpus/pv_docs/txt/kfk__documents__about__conceptual-model.html.txt

### 1.3 Брокеры, репликация, HA

- `server.properties`: уникальный `broker.id`, `listeners`
  (PLAINTEXT/SSL/SASL_PLAINTEXT, порт 9092), `zookeeper.connect`,
  `log.dirs` (несколько дисков через запятую). Партиция делится на
  сегменты; удаление по retention (`log.retention.hours`) — сегментами.
- **Ожидаемая доступность 99,9 %** при условиях: каждый компонент на
  отдельном хосте, фактор репликации ≥ 3, `min.insync.replicas` ≥ 2;
  rolling update без простоя; `unclean.leader.election.enable=false`.
- При отказе брокера лидеры партиций переназначаются из ISR; вернувшийся
  брокер догоняет репликацией и возвращается в ISR.
- `auto.leader.rebalance.enable=true` по умолчанию; Corax-специфичный
  `auto.leader.rebalance.on.not.isr.broker.enable=false` — брокер с
  несинхронизированными репликами не участвует в выборах лидера (защита
  от «дискового шторма»).
- **Auto Data Balancer**: режимы `--generate`/`--execute`/`--auto`; авто
  — только в технологическое окно; throttle и rack-awareness.
- Масштабирование: брокеры и партиции добавляются без простоя; «тысячи
  топиков»; потребителей в группе ≤ числа партиций (лишние простаивают).

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/cluster-ha.md; corpus/pv_docs/txt/kfk__documents__about__non-functional-features.html.txt

### 1.4 Гарантии доставки: acks, идемпотентность, транзакции

- **acks**: `0` — без подтверждения, retries не работают; `1` — лидер
  записал локально, при его отказе до репликации на ISR «запись будет
  потеряна»; `all` (=-1) — все ISR, «самая надежная из доступных
  гарантий»; для идемпотентности обязателен `acks=all`.
- **retries**: повторы без `max.in.flight.requests.per.connection=1`
  могут изменить порядок записей; «включение параметра retries может
  привести к задвоению данных».
- **Идемпотентный продюсер** (`enable.idempotence=true`): «усиливает
  семантику доставки… с at least once до exactly once». Требования:
  `max.in.flight.requests.per.connection ≤ 5`, `retries > 0`, `acks=all`.
  Оговорки: гарантия «только для сообщений, отправленных в одной сессии»;
  повторные отправки на уровне приложения идемпотентность не отменяют.
- **Транзакционный продюсер** (`transactional.id`, идемпотентность
  включается автоматически): атомарная запись в несколько партиций/
  топиков. Рекомендация вендора: «replication.factor = 3 и
  min.insync.replicas = 2 является оптимальной для большинства случаев»;
  недостаток ISR → `NotEnoughReplicas(AfterAppend)`; транзакции по
  умолчанию требуют ≥ 3 брокеров (`transaction.state.log.replication.factor`).
- **Потребитель**: `isolation.level=read_committed` — чтение только
  зафиксированных транзакционных сообщений до LSO (дефолт
  `read_uncommitted`).

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/dev-patterns.md; corpus/pv_docs/txt/kfk__documents__dev-guide__instructions__producer__usage-examples.html.txt

### 1.5 Потребители: смещения, группы, сбои

- Фиксация смещений: авто (`enable.auto.commit=true`, дефолт 5000 мс) или
  вручную `commitSync()`/`commitAsync()`; точка восстановления после сбоя
  — committed position.
- Группы: каждая партиция — ровно одному потребителю группы; групп на
  топик любое число без дублирования; при ребалансировке «все потребители
  приостанавливают чтение». `auto.offset.reset`: earliest/latest/none.
- Сбои: `session.timeout.ms` + heartbeat; «живая блокировка» —
  `max.poll.interval.ms`, коммит после ухода из группы →
  `CommitFailedException`.
- Рекомендация вендора: при непредсказуемом времени обработки — обработка
  в отдельном потоке, автофиксация выключена, ручной коммит после
  обработки, `pause` партиции.
- MessagePack: «должен использоваться отдельный топик, в который не
  пишутся сообщения в других форматах».

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/dev-patterns.md; corpus/pv_docs/txt/kfk__documents__dev-guide__instructions__consumer__offset.html.txt

### 1.6 Corax Schema Registry

- Централизованное хранилище схем + serdes. Форматы: по `sr-overview` —
  Avro, JSON Schema, Protobuf; по `sr-concepts` — Avro и JSON (расхождение
  зафиксировано; Protobuf — [ТРЕБУЕТ ПРОВЕРКИ]). Serdes — **только Java**.
- «Работает по принципу **Active-Active**», георезервирование; ставится
  отдельно от брокеров. Хранение — топик **`_crx_schemas`** (1 партиция,
  `cleanup.policy=compact`): «Параметры менять запрещено».
- Компоненты: SR Server (REST API, порт 8081), SR Client (Java API с
  кэшем), SR Serde, **Records Policy** — проверка соответствия схеме **на
  стороне брокера**; «НЕ ВХОДИТ в состав Apache Kafka и доступен только
  при использовании Corax». ID схемы пишется в заголовок сообщения;
  субъект по умолчанию `<топик>-value` / `<топик>-key`.
- Совместимость: **BACKWARD** (дефолт, не транзитивный; сначала
  потребители), BACKWARD_TRANSITIVE, FORWARD (сначала производители),
  FORWARD_TRANSITIVE, FULL (порядок любой), FULL_TRANSITIVE, NONE (без
  проверок). В не-TRANSITIVE режиме совместно работают версии схемы,
  различающиеся не более чем на 1. Kafka Streams — только FULL/TRANSITIVE/
  BACKWARD. Режим через REST API глобален. При несовместимом изменении —
  одновременное обновление всех клиентов или новый топик
  («предпочтительный вариант»).
- Серверная валидация JSON-схем **по умолчанию отключена** (включается
  `schema.registry.rest.schemas.custom.checks=true`; отказ — HTTP 422).
- Безопасность SR: HTTP Basic (пароли только зашифрованные); ACL, роли
  ADMIN / ADMIN_AS / USER, операции SCHEMA_*/CONFIG_*/MUTABILITY_*.
- Рекомендация вендора: «всегда начинайте с Schema Registry».
- Миграция с Confluent Schema Registry — [ТРЕБУЕТ ПРОВЕРКИ].

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/security-schema-registry.md; corpus/pv_docs/txt/kfk__documents__schema-registry__sr-architecture.html.txt

### 1.7 Безопасность

- **Каналы**: TLS 1.2/1.3, mTLS, настраиваемые cipher suites (все связки,
  включая брокеры↔брокеры и Schema Registry/Corax UI/ZooKeeper). Требование:
  TLS ≥ 1.2, рекомендуется 1.3. «Включение SSL значительно увеличивает
  утилизацию процессора».
- **Аутентификация**: X.509 (в т.ч. `FingerprintKafkaPrincipalBuilder`);
  Kerberos (SASL/GSSAPI, keytab).
- **ACL**: ресурсы CLUSTER, GROUP, TOPIC, TRANSACTIONAL_ID,
  DELEGATION_TOKEN, USER; ALLOW/DENY; управление через Corax UI и CLI.
  «Restricted Authorizer» упомянут ([ТРЕБУЕТ ПРОВЕРКИ]).
- **Аудит**: встроенный плагин (`kafka.se.audit.enable`, дефолт true):
  топики, ACL, конфигурации, перераспределение партиций, группы, квоты.
  Провайдеры: `TsAuditProvider` — в компонент **COTE** продукта Platform V
  Monitor по HTTPS; `LogAuditProvider` — файл `kafka-audit.log`. Фильтры
  `kafka.se.audit.suppress.*`; возможен свой AuditProvider.
- **Сквозное (e2e) шифрование**: на клиенте, «не зависит от настройки
  соединения с брокерами». Симметричное — `EncryptedByteArraySerializer`
  (ключ + соль ≥ 16 байт); асимметричное — `EncryptedCertByteArraySerializer`
  (RSA/ECB/PKCS1Padding, только кодом).
- **Пароли в конфигурациях**: `crx-encoding.sh` / `crx-password-encrypt.sh`
  (PBKDF2WithHmacSHA1 + AES-256; дешифрование не поддерживается);
  ConfigProvider `${имя:пароль}`; профиль with-auth шифрует по умолчанию.
  Дефолтный пароль инсталлятора `qwe123` — заменить обязательно.
- **SecMan** (СберТех): хранение секретов, выпуск сертификатов из PKI.
  Заявлен Vault Java Driver — «интеграция с HashiCorp Vault для
  динамического получения секретов». kTLS — [ТРЕБУЕТ ПРОВЕРКИ].
- **SSL live update**: ротация сертификатов без недоступности — по одному
  брокеру, после запуска ждать полной репликации
  (`--under-replicated-partitions` = 0).
- **JMX** по умолчанию выключен; в PROD — только с аутентификацией.

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/security-schema-registry.md; corpus/pv_docs/txt/kfk__documents__administration-guide__audit.html.txt

### 1.8 Межкластерная репликация (Corax Mirror Maker 2) и архив в S3

- **Corax Mirror Maker 2 (MM2)** — репликация данных и метаданных между
  кластерами на платформе Kafka Connect; «отличается от механизма
  внутрикластерной репликации в Kafka» (т.е. не синхронная).
- Компоненты: Source/Sink Connector, Checkpoint Connector (синхронизация
  offsets между кластерами), Heartbeat Connector, Config Connector.
- Реплицирует: сообщения, метаданные топиков, группы потребителей,
  контрольные точки смещений, **ACL**. Топологии Active-Active /
  Active-Standby; сценарии: DR, консолидация, изоляция пром/тест,
  миграция; есть метрики лага репликации.
- **Бэкап в S3**: Kafka Connect + доработанный плагин Lenses Stream
  Reactor: sink-коннектор (выгрузка) и source-коннектор (восстановление),
  KCQL, `EncryptedByteArrayConverter`, DLQ, управление через REST API.
  НФТ: «архивирование и восстановление данных выходят за рамки функций
  Corax… Исключение — облачные хранилища S3». Опции коннекторов —
  [ТРЕБУЕТ ПРОВЕРКИ].

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/cluster-ha.md; corpus/pv_docs/txt/kfk__documents__mm2__architecture.html.txt

### 1.9 Квоты, лимиты, системные требования, профиль нагрузки

- **Quota Management**: «встроенное квотирование пропускной способности
  по клиентам».
- Лимиты: `max.message.bytes` по умолчанию 5 МБ (брокер отклоняет
  превышающие), `max.request.size` 1 МБ, `fetch.max.bytes` 50 МБ,
  `max.partition.fetch.bytes` 1 МБ, очередь `queued.max.requests`.
- Минимум (пром): брокер 4 CPU / 16 ГБ / 200 ГБ; контроллер ZK/KRaft
  4 / 4 ГБ / 64 ГБ; Schema Registry и Corax UI по 2 CPU / 1 ГБ heap /
  32 ГБ. Каждый компонент — отдельный хост; диски RAID10 + XFS;
  максимальная производительность — SSD + сеть 10 Гбит/с.
- ОС: CentOS 8.7+, Astra Linux SE 1.7.3+, Альт СП 8+, SberLinux OS Server
  9.7.0+/8.10.4+. Ansible 2.9.X–3.X. Опционально: SecMan 1.7.0,
  Platform V Monitor 6.0.200+ (аудит в COTE), GigaChat API v1+.
- Профиль нагрузки вендора: **13 000 tps** при среднем сообщении 10 000
  байт; утилизация ≤ 80 % CPU/RAM/HDD. Стенд: 3 брокера, топик 20
  партиций, RF 3, `acks=1`, `min.insync.replicas=1` — параметры **теста**,
  не рекомендация для прода. Вендор рекомендует нагрузочное тестирование.
  SLA p99 указан как «1375» (единица — секунды; аномалия,
  [ТРЕБУЕТ ПРОВЕРКИ], опираться нельзя).
- Факторы производительности: партиционирование, `compression.type`
  (none/gzip/snappy/lz4/zstd), `batch.size`, `acks`, RAID10/XFS.

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/cluster-ha.md; corpus/pv_docs/txt/kfk__documents__about__system-requirements.html.txt

### 1.10 Мониторинг

- **HTTPReporter** (`ru.sbrf.kafka.HttpReporter`): метрики Prometheus,
  порт 7011 (HTTP) / 7012 (HTTPS).
- «Здоровые» значения: `ActiveControllerCount` = 1,
  `OfflinePartitionsCount` = 0, `UnderReplicatedPartitions` = 0,
  `UncleanLeaderElectionsPerSec` = 0 (ненулевое = потеря данных).
- Брокер: `MessagesInPerSec`, `BytesIn/OutPerSec`, `RequestQueueSize`
  (лимит `queued.max.requests`), `IsrShrinksPerSec`, `MaxLag`.
- Consumer lag: клиентская `records-lag-max`; координаторские
  `offset.metadata.group.*` и `timestamp.metadata.group.*`.
- Шаблоны «SBT Corax ConsumerLag» — [ТРЕБУЕТ ПРОВЕРКИ].

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/references/cluster-ha.md; corpus/pv_docs/txt/kfk__documents__administration-guide__monitoring.html.txt

### 1.11 Установка и паттерны применения

- Установка — штатной **Ansible-ролью**: инвентарь `inv/stand-corax.yml`,
  секции `zookeeper`/`kafka_controller`/`kafka`; профили безопасности
  (пример `SSL__ZK_mTLS_WITH_AUTH__KAFKA_SSL_WITH_AUTH`); критерий успеха
  `unreachable=0 failed=0`; роли идемпотентны. Сертификаты JKS; для прода —
  подпись корпоративным CA.
- Паттерн вендора «Единый журнал операций»: источники (банкоматы, ДБО,
  платёжные шлюзы, процессинг, POS) — каждый в свой топик (JSON/Avro +
  Schema Registry); нормализация Kafka Streams/ksqlDB; общий топик читают
  антифрод, DWH, поиск, регуляторная отчётность.
- «Правила эксплуатации» содержательно пусты — нормы банк докладывает сам.
  Оговорка вендора: «использование продукта иными способами» —
  «работоспособность… не гарантируется».

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/SKILL.md; corpus/pv_docs/txt/kfk__documents__installation-guide__kafka-zookeeper-ansible-installation.html.txt

### 1.12 Пробелы съёма документации

В съём не вошли 49 из ~90 страниц, включая: детальную конфигурацию KRaft;
миграцию с Confluent Schema Registry; сценарии MM2; опции S3-коннекторов;
шаблоны consumer-lag; CLI-команды; страницы Corax UI по топикам/брокерам.
Всё, что на них опирается, — [ТРЕБУЕТ ПРОВЕРКИ]: помечать как допущение
или вопрос к вендору.

Разрешённые домены для режима `agentic` (web_fetch):
`platformv.sbertech.ru`, `sbertech.ru`.

Источник: banking/plugins/ru-integration/skills/corax-event-streaming/SKILL.md (раздел «Ограничения и пометки»)

---

## 2. Сценарий заказчика `[BENCH]`

### 2.1 Бизнес-контекст

АО «Банк "Меридиан"» (вымышленный, топ-20 по активам) строит **платёжный
хаб**: мгновенные платежи СБП (C2B, B2C, возвраты) и внутрибанковские
переводы переводятся на событийную модель. Платформа класса **Mission
Critical**: события публикуются 24×7, недоступность шины более 10 минут
блокирует платежи и влечёт регуляторную отчётность (инцидент по 719-П).
Целевой брокер утверждён: **Platform V Corax 16.392.0**.

### 2.2 Ландшафт AS-IS

- Интеграция платёжного контура: **IBM MQ** (кластер в ЦОД-1) + точечные
  синхронные вызовы; событийной шины нет.
- Потребители событий (сегодня — по расписанию из БД): антифрод,
  ДБО-уведомления (push/SMS), DWH/витрины, сверки, регуляторная отчётность.
- Сервисы хаба: Java 17 / Spring Boot, ~40 сервисов; Kubernetes банка в
  обоих ЦОД (active/standby).
- Проблемы AS-IS: задержка событий до потребителей (минуты), нет порядка
  по счёту, дубли уведомлений при ретраях, нельзя проиграть события заново.

### 2.3 Нагрузка и объёмы

- События СБП + внутренние переводы: среднее **2 500 событий/с**, вечерний
  пик **9 000 событий/с**; рост **+30 % в год** на 5 лет.
- Средний размер сообщения **4 КБ**; реестры возвратов/сверок — до **3 МБ**
  (учесть в `max.message.bytes`).
- ~12 доменных топиков, до 60 топиков с окружениями и служебными.
- Потребители: антифрод (p99 доставки ≤ 3 с), ДБО-уведомления (≤ 10 с),
  DWH (лаг до 15 мин, не должен деградировать платёжный контур), сверки
  (ночью пакетно).
- Онлайн-хранение в топиках — **30 дней**; регуляторный архив — **5 лет**
  с восстановлением топика на дату.

### 2.4 Инфраструктура и ограничения `INF-xx`

| ID | Ограничение |
|---|---|
| INF-01 | Два ЦОД (ЦОД-1 «Основной», ЦОД-2 «Резервный»), ~50 км, 2×10 Гбит/с, RTT 2–4 мс. Третьей площадки нет; «облачная зона» банка, сетево независимая от обоих ЦОД: до 2 лёгких ВМ (4 vCPU / 8 ГБ RAM / 100 ГБ) |
| INF-02 | В каждом ЦОД до 8 хостов: 16 CPU / 64 ГБ RAM / RAID10 NVMe 4 ТБ (XFS); ОС — только Platform V SberLinux OS Server 9.7; Java — SberJDK 17 |
| INF-03 | Развёртывание — только Ansible из корпоративного репозитория; ручные изменения конфигураций запрещены |
| INF-04 | S3-совместимое хранилище в обоих ЦОД (раздельные бакеты); NFS для архивов нет |
| INF-05 | Мониторинг — Zabbix + Prometheus-совместимый сбор; SIEM принимает события по HTTPS и из лог-файлов агентами. **Platform V Monitor (COTE) в банке не развёрнут** |
| INF-06 | Хранилище секретов — **HashiCorp Vault** (3 узла между ЦОД, RTO ≤ 5 мин). SecMan отсутствует и не планируется |
| INF-07 | Команда: 3 инженера middleware (Kafka — средне, Corax — базово), 1 администратор ИБ, 2 DevOps; нагрузочное тестирование перед запуском обязательно |
| INF-08 | Плановых окон почти нет (СБП 24×7): 1 раз в квартал, ночь вс→пн, ≤ 2 часа, с согласования НСПК |

### 2.5 Нефункциональные требования `NFR-xx`

| ID | Требование | Целевое значение |
|---|---|---|
| NFR-01 | Доступность платформы | ≥ 99,95 % в год (24×7) |
| NFR-02 | RPO при отказе брокера/стойки внутри ЦОД | **0** (подтверждённые события не теряются) |
| NFR-03 | RTO при отказе брокера внутри ЦОД | ≤ 5 мин, автоматически |
| NFR-04 | RPO при полной потере ЦОД-1 | ≤ 5 мин (честно оценить и обосновать) |
| NFR-05 | RTO при полной потере ЦОД-1 (ручное решение допустимо) | ≤ 60 мин, включая переключение потребителей на свежие смещения |
| NFR-06 | Сквозная доставка события до антифрода | p99 ≤ 3 с при пике |
| NFR-07 | Упорядоченность | строгий порядок в пределах счёта/операции; перестановка при ретраях продюсера недопустима |
| NFR-08 | Финансово значимые события | семантика «ровно один раз» на пути продюсер → топик → потребитель; дубли зачислений недопустимы |
| NFR-09 | Изоляция нагрузки | аналитика (DWH) не деградирует платёжных продюсеров более чем на 5 % |
| NFR-10 | Хранение и восстановление | 30 дней онлайн; архив 5 лет; восстановление топика на дату ≤ 24 ч |
| NFR-11 | Обновления и ротация сертификатов | без простоя (rolling) |
| NFR-12 | Ёмкость | 5 лет хранения при росте +30 %/год + 30 % запас на дисках брокеров |

### 2.6 Требования информационной безопасности `SEC-xx`

| ID | Требование |
|---|---|
| SEC-01 | TLS не ниже 1.2 (рекомендовано 1.3) на всех каналах: клиенты↔брокеры, брокеры↔брокеры, брокеры↔координация, Schema Registry, UI, мониторинг |
| SEC-02 | Аутентификация всех клиентов; персональный принципал на сервис; анонимный доступ запрещён (включая Schema Registry и Corax UI) |
| SEC-03 | ACL с минимальными правами на топики/группы/кластер; запрет wildcard-административных ACL для прикладных сервисов |
| SEC-04 | Аудит всех административных операций (топики, ACL, конфигурации, квоты, группы) с выгрузкой в SIEM банка |
| SEC-05 | ПДн в событиях (ФИО, счета) защищаются сквозным шифрованием: администратор платформы и обладатель архива S3 не читают содержимое без ключей |
| SEC-06 | Пароли/секреты в конфигурациях — только зашифрованные; ключи — в Vault банка; дефолтные пароли инсталлятора заменяются |
| SEC-07 | Ротация сертификатов не реже раза в год, без недоступности кластера |
| SEC-08 | Доступ администраторов персонализированный; их действия попадают в аудит (SEC-04) |

### 2.7 Организационные ожидания `[BENCH]`

- Комитет ожидает **одно рекомендуемое решение** плюс 1–2 отвергнутые
  альтернативы с причинами; реализация силами INF-07 штатной
  Ansible-автоматизацией.
- Особая чувствительность: **размещение кворума координационного слоя при
  двух ЦОД** (поведение при потере ЦОД-1 и при потере облачной зоны) и
  **честный RPO между ЦОД** (комитет знает, что межкластерная репликация
  асинхронна, и ждёт оценки, а не декларации «ноль»).
- Выбор ZooKeeper vs KRaft — явно, с оценкой зрелости документации
  (см. §1.2, §1.12).

### 2.8 Что можно принять допущением `[ASSUME]`

- Точная версия ядра Apache Kafka в 16.392.0 не задана — не использовать
  или пометить как допущение (§1.1).
- Приложение допускает доработку: клиентские библиотеки, ключи сообщений,
  стратегии фиксации смещений.
- Клиенты платформы — Java-сервисы (serdes Schema Registry — только Java,
  §1.6); не-Java потребители — отдельный открытый вопрос.
- Vault доступен из обоих ЦОД с латентностью ≤ 5 мс.
- Межцодового канала хватает для репликации пикового потока с запасом
  ≥ 3× (подтверждается расчётом решающего).

---

## 3. Глоссарий

| Термин | Значение |
|---|---|
| ISR / RF | In-Sync Replicas / replication.factor |
| `min.insync.replicas` | минимум ISR для подтверждения записи при `acks=all` |
| Exactly-once | идемпотентный + транзакционный продюсер, `read_committed` на потребителе |
| LSO | Last Stable Offset — граница чтения при `read_committed` |
| Records Policy | проверка сообщений схеме на стороне брокера (только Corax) |
| MM2 | Corax Mirror Maker 2 — межкластерная репликация на Kafka Connect |
| COTE | компонент аудита продукта Platform V Monitor |
| KRaft | режим Kafka без ZooKeeper (контроллеры метаданных) |
| СБП | Система быстрых платежей |
