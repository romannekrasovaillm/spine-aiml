# Кейс 008 — `legacy-survey` (обратное обследование legacy-монолита)

> Учебный кейс функции **reverse discovery** (`arch-be survey` / инструмент
> `reverse_survey`): синтетический legacy-монолит без документации, по которому
> детерминированный сканер собирает каркас карты обследования с честными
> метками `[confirmed]`/`[gap]`. Survey-ядро кейса — механическое,
> воспроизводится голым бинарём `arch-be` (как кейс 007); модель,
> контракты и дельта (см. «Акт 6 демо») — артефакты архитектора поверх
> карты: обследование → границы → дельта изменения.

## Что показывает кейс

- **Функция обследования как направление**: brownfield-работа начинается не с
  проектирования, а с карты системы, извлечённой из кода. Сканер (без LLM)
  покрывает 9 артефактов обследования из скилла `reverse-discovery`:
  компоненты/владельцы, точки входа, модель данных, интеграции, скрытые
  связи, тесты/наблюдаемость, долг и трупы, ограничения платформы, риски.
- **confirmed/gaps как противоядие от automation bias**: каждая находка —
  `[confirmed]` со ссылкой `файл:строка`; секции, где механически ничего не
  нашлось, остаются `[gap]` с вопросом владельцу домена, а не дозаполняются
  правдоподобным текстом. В этом монолите `[gap]` — ограничения платформы
  (версии нигде не запинаны) и риски по git churn (монолит — не git-корень).
- **Скрытые связи — главная находка**: два «сервиса» (`billing/` и
  `notifier/`) плюс импортёр и ad-hoc отчёт работают с ОДНОЙ общей таблицей
  `payments` — сканер называет все стороны поимённо; обмен с внешней
  системой идёт через drop-каталог csv, а не через API.
- **Свежесть как гейт (BF-3 в миниатюре)**: карта — living-spec со сроком
  годности. `CONSTRAINTS.yaml` кейса несёт правило `max_age` (365 дней) на
  `docs/reverse/survey.md`: просроченная карта — FAIL механического гейта
  `arch-be control check`, а не «устаревшая страница в вики».

## Анатомия кейса

```
monolith/                       синтетический legacy-монолит (20 файлов,
                                python + sql + shell + csv, без документации)
  billing/worker.py             HTTP (/health, /charge) + INSERT INTO payments;
                                закомментированный флаг FEATURE_DYNAMIC_TARIFF
  billing/legacy_export.py      мёртвый файл (маркер legacy в имени)
  billing/tests/                тесты есть ТОЛЬКО у биллинга
  notifier/sender.py            читает ТУ ЖЕ таблицу payments (скрытая связь!),
                                консьюмер RabbitMQ; тестов нет
  importer/import_csv.py        csv из drop/ → INSERT INTO payments
  drop/ + drop/processed/       файловый обмен с внешней системой
  migrations/0001…0003.sql      схема (миграция 0003 «догнана» после прода)
  scripts/                      nightly_reconcile.sh (cron), requeue_failed.sh,
                                report.sql (SQL вне миграций)
  crontab, ops/systemd/*.timer  джобы планировщиков
  config/app.yaml, notifier.toml  интеграции (userinfo в URL, localhost-стабы)
docs/reverse/survey.md          карта обследования — ЖИВОЙ результат прогона
                                `arch-be survey` (перезаписывается целиком)
docs/reverse/survey-notes.md    заготовка для [inferred]-заметок (survey её
                                никогда не трогает)
evidence/survey.md              зафиксированная копия карты на момент сборки кейса
CONSTRAINTS.yaml                правила: max_age (свежесть карты), context_boundary
                                (границы контекстов из модели), no-sync-tariff-call
model/                          модель «как есть» (7 сущностей): SYS-001 монолит,
                                CMP-001…003 billing/notifier/importer с code_roots,
                                INT-001…003 АБС / фид тарифов / очередь уведомлений
contracts/                      тарифы (tariffs-feed.asyncapi.yaml — под дельту 001)
                                и очередь (payments-notify.asyncapi.yaml — ретро
                                по коду); идемпотентные event_id, версии сообщений
changes/tariffs-idempotent-consumer/   дельта-спека «идемпотентный потребитель
                                тарифов» (DELTA.md, канон arch-be delta):
                                ADDED/MODIFIED/REMOVED с EARS-критериями и
                                ссылками на карту; статус proposed (цикл
                                propose → apply → archive)
```

## Акт 6 демо: границы контекстов, контракты, дельта

Brownfield-контур поверх карты (акт 6 сценария демо для архитекторов):

1. **Границы — в модели, не в памяти (ADR-030)**: `code_roots` CMP с
   двойными координатами legacy-реальности (`monolith/billing` — путь от
   корня кейса, `billing` — python-импорт от корня запуска монолита);
   правило `context-boundaries` в CONSTRAINTS — импорт через границу без
   `depends_on` ломает гейт.
2. **Контракты интеграций (ADR-035)**: INT-002/INT-003 несут поле
   `contract`; trace check показывает звено «INT → контракт» 2/3 — INT-001
   (АБС) без контракта честно светится warn'ом `int-without-contract`.
3. **Дельта вместо переписывания**: `changes/001-tariffs-idempotent-
   consumer.md` — каждое изменение со ссылкой на место в карте; маршрут
   Standard по `score --trigger new_component=true --from-diff`
   (происхождение триггеров: declared vs diff, ADR-034).
4. **Правила неретроактивны и зелёные ДО кода**: `no-sync-tariff-call`
   запрещает синхронный вызов фида из `/charge` до появления потребителя.

## Воспроизведение

```bash
# 1. Обследование монолита (перезапишет docs/reverse/survey.md, обновит mtime):
arch-be survey кейсы/legacy-survey/monolith --out ../docs/reverse

# 2. Гейт кейса: свежесть карты + границы контекстов + запрет синхр. вызова:
#    ожидание «Правил: 3, нарушений: 0 … Итог: PASS»
arch-be control check кейсы/legacy-survey --constraints кейсы/legacy-survey/CONSTRAINTS.yaml

# 3. Модель «как есть»: 7 сущностей, ссылки/циклы под контролем:
arch-be model validate кейсы/legacy-survey/model

# 4. Живой негатив границы (акт 6): инъекция импорта через контекст —
#    sed -i 's/^from billing import tariffs$/from billing import tariffs\nfrom notifier import sender/' кейсы/legacy-survey/monolith/billing/worker.py
#    → control check (шаг 2) даёт:
#    [error] monolith/billing/worker.py:8 context-boundaries — context_boundary:
#      импорт 'notifier' пересекает границу контекста: CMP-001 → CMP-002 (Notifier)
#      без depends_on в модели → Итог: FAIL; после отката — PASS.
#
# 5. Трассировка «INT → контракт» (ADR-035): 2/3 покрыто, INT-001 — warn
#    int-without-contract (внешняя АБС без контракта — легитимно, но видна):
arch-be trace check кейсы/legacy-survey

# 6. Дельта изменения — канонический валидатор продукта:
#    «дельта 'tariffs-idempotent-consumer': нарушений нет»
arch-be delta validate tariffs-idempotent-consumer --repo кейсы/legacy-survey
#    (контракты событий линтуются инструментом агента asyncapi_lint —
#    идемпотентные event_id и версионирование, 0 находок)

# 7. Маршрут дельты: заявленный new_component + механический дифф:
#    Score 2 (new_component (declared) + api_contract_change (diff)) → Standard
arch-be control score --trigger new_component=true --from-diff
```

Из корня репозитория харнесса. Шаг 2 без шага 1 через год после сборки кейса
упадёт с `max_age: файл устарел` — это и есть демонстрация living-spec.

## Что видно в карте (фрагмент)

- `общая таблица payments — используется из: billing, importer, migrations,
  notifier, scripts` — одна строка, объясняющая, почему изменение схемы
  биллинга бьёт по трём командам;
- `внешний endpoint corebank.internal.example:8443` — host:port без пути,
  userinfo из конфига срезан, localhost-стабы в карту не попали;
- `[gap] Версии платформы не объявлены…` — честный пробел: зависимости
  монолита не запинаны, и это вопрос владельцу, а не повод для выдумки.

## Ограничения

- Кейс синтетический: имена хостов/учёток — вымышленные плейсхолдеры, монолит
  не является работающим приложением (это объект сканирования, а не демо-стенд).
- Сканер — эвристика без LLM: он не «понимает» систему, а механически собирает
  доказательства. Выводы `[inferred]` — работа архитектора поверх карты
  (место для них — `docs/reverse/survey-notes.md`).
