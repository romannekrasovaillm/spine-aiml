---
name: fitness-function-catalog
description: Каталог 20 готовых fitness-функций для CONSTRAINTS.yaml (таймауты, ретраи с джиттером, контракты OpenAPI/AsyncAPI, problem+json, идемпотентность, transactional outbox, SLO, телеметрия, IaC, модель угроз, DR, контракты данных) с реальными regex, плюс три волны внедрения библиотеки правил и шаблон карточки дистилляции источника в правило (7 полей). Используй, когда пополняешь CONSTRAINTS.yaml, проектируешь гейты A4/A5, дистиллируешь источник в исполняемое правило или выбираешь порядок внедрения правил.
---

# Каталог fitness-функций и дистилляция источников в правила

Двадцать проверок из библиотеки архитектурных источников; переносятся в CONSTRAINTS.yaml с минимальной адаптацией. Правило без проверки живёт до первого дедлайна — у каждой функции двоичный исход PASS/FAIL.

## Шесть типов проверок движка Spine

- `must_contain` / `must_not_contain` — regex по glob-набору файлов: обязан встречаться / не должен нигде.
- `each_file_must_contain` — regex обязателен в КАЖДОМ файле набора; пустой набор по glob — тоже находка.
- `file_exists` — обязательный файл по относительному `path` (без glob).
- `dir_must_have_file` — обязательный файл (`path`) в КАЖДОМ каталоге набора; пустой набор каталогов — находка.
- `command_succeeds` — команда завершается кодом 0 (с таймаутом): линтеры, контрактные и chaos-проверки.

Поля правила: `name`, `type`, далее по типу — `glob`+`pattern`, `path` или `command`; опционально `severity: error|warn` (дефолт error).

## Двадцать функций: правило — тип — проверка — источник

1. Таймаут на каждый исходящий вызов — `each_file_must_contain` — явный timeout в конфиге клиента — Release It!, AWS Builders' Library.
2. Ретраи с задержкой и джиттером — `must_contain` — backoff+jitter, бюджет на цепочку — AWS Builders' Library.
3. Контракт синхронного интерфейса — `dir_must_have_file` — openapi.yaml в каждом сервисе — OpenAPI.
4. Контракт событийной интеграции — `dir_must_have_file` — asyncapi.yaml на тему — AsyncAPI.
5. Единый формат ошибок периметра — `must_contain` — application/problem+json — RFC 9457.
6. Идемпотентность необратимых операций — `must_contain` — Idempotency-Key + тест на повтор — IETF httpapi draft.
7. Схемы не ломают потребителей — `command_succeeds` — buf breaking / режим совместимости — Buf, Schema Registry.
8. Атомарность события и состояния — `command_succeeds` — таблица outbox + релей — microservices.io, EIP.
9. Нет доступа к чужой БД — `must_not_contain` — строки подключения к чужим контекстам — DDD, Debezium.
10. Сквозная трассировка — `must_contain` — traceparent в заголовках — W3C Trace Context.
11. Минимум метрик сервиса — `each_file_must_contain` — rate, errors, duration — RED method.
12. Атрибуты телеметрии по соглашениям — `each_file_must_contain` — service.name, deployment.environment — OTel semconv.
13. У сервиса определены SLO — `dir_must_have_file` — slo.yaml: индикатор, цель, алертинг — SRE Workbook.
14. Зависимости слоёв не нарушены — `command_succeeds` — ArchUnit / dependency-cruiser — ArchUnit, dependency-cruiser.
15. IaC соответствует базовой линии — `command_succeeds` — Checkov/Conftest без high — CIS Benchmarks, OPA.
16. Происхождение артефакта подтверждено — `command_succeeds` — проверка provenance — SLSA.
17. ПДн не покидают контур РФ — `must_not_contain` — иностранные регионы в IaC — 152-ФЗ.
18. Модель угроз критичной системы — `file_exists`/`dir_must_have_file` — threat-model.md (STRIDE; при ПДн LINDDUN) — Shostack, LINDDUN.
19. Стратегия DR выбрана и проверена — `command_succeeds` — дата учений в интервале — AWS DR, ГОСТ Р 57580.4.
20. Контракт и владелец продукта данных — `dir_must_have_file` — datacontract.yaml: схема, SLA — Data Contract Spec, DAMA-DMBOK.

## Готовые фрагменты CONSTRAINTS.yaml (все шесть типов)

```yaml
rules:
  - name: every_client_has_timeout            # 1
    type: each_file_must_contain
    glob: 'services/*/config.{yaml,yml,toml}'
    pattern: '(?i)(connect|read|request)?_?timeout\s*[:=]'
  - name: every_service_has_openapi           # 3
    type: dir_must_have_file
    glob: 'services/*'
    path: openapi.yaml
  - name: threat_model_exists                 # 18 (монорепо)
    type: file_exists
    path: threat-model.md
  - name: error_format_problem_json           # 5
    type: must_contain
    glob: 'services/*/openapi.yaml'
    pattern: 'application/problem\+json'
  - name: no_foreign_db_connections           # 9
    type: must_not_contain
    glob: 'services/*/config.{yaml,yml}'
    pattern: '(?i)(postgres|mysql|mongodb)://[^\s"]*(billing|orders|crm)-db'
  - name: pdn_stays_in_rf                     # 17
    type: must_not_contain
    glob: 'iac/**/*.tf'
    pattern: '(?i)region\s*=\s*"(eu|us|ap|sa|af|me|ca)-[a-z]+-[0-9]"'
  - name: outbox_table_and_relay              # 8
    type: command_succeeds
    command: >-
      test -n "$(grep -rli outbox db/migrations/)" &&
      test -n "$(grep -rliE 'outbox[-_]?relay' services/*/config/)"
```

Остальные функции — из тех же шаблонов: 2, 6, 10 — `must_contain` (`backoff.*jitter|jitter.*backoff`, `Idempotency-Key`, `traceparent`); 4, 13, 20 — `dir_must_have_file` (asyncapi/slo/datacontract .yaml); 7, 14–16, 19 — `command_succeeds` (buf breaking, depcruise, checkov, slsa-verifier, сверка даты учений DR). Сузить glob — решение о границе применимости правила, а не ослабление.

## Три волны внедрения

Библиотека, выросшая в пять раз за один заход, перестаёт применяться. Реалистичный шаг — 25–40 правил первой волны.

- **Волна 1. Механизируемое** — API-контракты, интеграции, устойчивость, policy-as-code: OpenAPI, RFC 9457, Idempotency-Key, outbox, совместимость схем, таймауты/ретраи, ArchUnit/OPA/Kyverno. Превращаются в проверки почти без потерь смысла, дают прирост зелёных гейтов A4.
- **Волна 2. Обязательное извне** — безопасность и регуляторные NFR: ГОСТ Р 57580.3/.4, положения Банка России, 152-ФЗ, КИИ, ZTA, ASVS, модель угроз. Обоснования не требуют — только корректный перевод в проверку; ошибка здесь дороже всех, поэтому волна вторая, а не последняя.
- **Волна 3. Смысловое** — границы доменов, данные, описание архитектуры, миграции, ИИ-системы. Требуют суждения; вводятся поверх работающей первой волны, иначе терминология останется словарём.

## Карточка дистилляции источника в правило (7 полей)

Любой источник сворачивается в карточку; с пустым Check или Owner она в CONSTRAINTS.yaml не попадает. Пример — transactional outbox (EIP + microservices.io):

- **Trigger** — признак применимости (тип интеграции, критичность, ПДн, внешний периметр). Пример: операция изменяет состояние и публикует событие о нём.
- **Rule** — через MUST / MUST NOT, без смягчающих слов. Пример: публикация события MUST выполняться через таблицу исходящих сообщений в той же транзакции, что и изменение состояния.
- **Rationale** — одно предложение: какой отказ предотвращается. Пример: двойная запись в БД и брокер без общей транзакции теряет или дублирует события при отказе между операциями.
- **Check** — тип проверки и выражение. Пример: `command_succeeds` — миграция создаёт таблицу outbox, релей зарегистрирован в конфигурации (правило 8).
- **Evidence** — артефакт после проверки, публикуемый владельцу. Пример: отчёт fitness_check в каталоге сервиса.
- **Reversibility** — обратимо / дорого / необратимо. Пример: дорого — снятие требует ревизии всех потребителей событий.
- **Owner / Expiry** — владелец и триггер пересмотра. Пример: архитектор интеграционного контура; пересмотр при переходе на брокер с транзакционной семантикой.

Источник: «Источники solution-архитектуры, отработанные для Spine» (расширенное издание): Приложение А (таблица 19), «Три волны», «Шаблон дистилляции» (таблица 18). Карта блоков — `architecture-sources-map`, антипаттерны — `rule-library-antipatterns`.
