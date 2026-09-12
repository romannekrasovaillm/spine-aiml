# Веб-доступ и локальная база знаний

Два канала доменных знаний архитектора: веб (`src/web.rs`) и локальные
каталоги документов (`src/kb.rs`). Оба доступны из CLI, из TUI слэш-командами
и как инструменты агентного цикла (`web_search`, `web_fetch`,
`web_arch_sites`, `kb_search`).

## Веб

Настройки — секция `[web]` (`src/config.rs::WebConfig`):

```toml
[web]
enabled = true              # false — веб-инструменты не регистрируются (контур банка, AD-BE5)
search_base = "https://html.duckduckgo.com/html/"
user_agent = "arch-ml/0.1 (+solution-architect harness)"
timeout_secs = 30            # таймаут каждого запроса
max_fetch_chars = 24000      # усечение текста страницы после html→text
```

`enabled = false` полностью выключает веб-канал: инструменты `web_search`,
`web_fetch`, `web_arch_sites` не попадают в инструментарий агентной сессии
(egress-дисциплина периметра банка, AD-BE5/GAP-C1). По умолчанию `true` —
без ключа поведение прежнее.

### Поиск

```bash
arch-ml web search "transactional outbox pattern"           # общий поиск
arch-ml web search "saga orchestration" --arch              # только кураторские сайты
arch-ml web sites                                           # список сайтов
```

- Движок — DuckDuckGo HTML (`search_base`), парсинг результатов (`scraper`),
  до 10 результатов (заголовок, URL, сниппет); пустая выдача — не ошибка.
- `--arch` (`search_arch_sites`): на каждый кураторский сайт — запрос
  `{query} site:{domain}`; запросы идут конкурентно (лимит 4), результаты
  сливаются с дедупликацией по URL, итог — до 15. Это режим «версии и
  паттерны проверяем по первоисточникам, не по памяти».

### Кураторские сайты (`[[web.arch_sites]]`)

Встроенный список (11 сайтов; поля: `name`, `base_url`, `domain`
для site:-ограничения, `description`):

| name | domain | Чем полезен |
|---|---|---|
| `aws-arch` | docs.aws.amazon.com | AWS Architecture Center, Well-Architected |
| `azure-arch` | learn.microsoft.com | Azure Architecture Center: паттерны, reference |
| `gcp-arch` | cloud.google.com | Google Cloud Architecture Center |
| `fowler` | martinfowler.com | Эссе по архитектуре, микросервисам |
| `infoq-arch` | infoq.com | InfoQ Architecture & Design |
| `microservices-io` | microservices.io | Каталог паттернов микросервисов (Ричардсон) |
| `c4` | c4model.com | Нотация C4 |
| `arc42` | docs.arc42.org | Шаблон документирования архитектуры |
| `togaf` | pubs.opengroup.org | TOGAF Standard |
| `sei` | insights.sei.cmu.edu | SEI: ATAM, тактики, quality attributes |
| `awesome-arch` | awesome-architecture.com | Кураторский список ресурсов |

Свои сайты добавляются секциями `[[web.arch_sites]]` в `config.toml`.

### Фетч страницы

```bash
arch-ml web fetch https://microservices.io/patterns/data/transactional-outbox.html
```

`fetch`: загрузка → html→text: целиком вырезаются `script`, `style`, `nav`,
`footer`, `header`, `aside`; текст собирается из `h1–h3`, `p`, `li`, `pre`,
`code`, `td`; итог усекается до `max_fetch_chars` (24 000) — страница
готова к вставке в контекст модели.

В TUI: `/web <query>` (поиск по кураторским сайтам), `/fetch <url>`,
`/sites`.

## Локальная база знаний (`kb`)

Секция `[knowledge]`:

```toml
[knowledge]
dirs = [
  "~/knowledge/architecture",   # разборы и статьи (замените на свои каталоги)
  "~/knowledge/papers",         # научные статьи/концепты
  "~/knowledge/skills",         # библиотека скиллов
]
extensions = ["md", "txt", "rst"]
```

(дефолтные каталоги — плейсхолдеры; сужайте под свои домены: принцип Context Supply
Chain — релевантные источники, а не «вся Wiki»). Тильда в `dirs`
раскрывается при загрузке конфига.

```bash
arch-ml kb "идемпотентность платежей" --limit 8
```

Механика (`kb::search`, v2):

- Обход каталогов walkdir (в `spawn_blocking`, не блокируя runtime);
  скрытые каталоги и `target`/`.git`/`node_modules` пропускаются;
  недоступные каталоги и нечитаемые файлы — пропускаются.
- **Кэш корпуса в памяти** (ключ — набор `dirs`+`extensions`; запись на файл:
  mtime+len, содержимое, токены строк, индекс md-заголовков). Повторный
  поиск перечитывает только изменившиеся файлы (сверка по mtime+len);
  бюджет кэша — 256 МБ с LRU-выселением целых файлов.
- **Скоринг BM25-подобный**: tf с насыщением (k1=1.5), IDF по документам
  корпуса, нормализация длины (b=0.75); отдельный канал имени файла
  (совпадение по имени тоже даёт хит со строкой 0); буст ×3 за совпадение
  в md-заголовке; фразовый бонус (все термины в одной строке / в окне
  ±3 строки). Топ разнообразится: не более 2 хитов на файл, при нехватке
  хитов лимит ослабляется.
- **Словоформы без словарей**: термины от 5 символов матчатся по общему
  корню токена («идемпотентность» ловит «идемпотентный»/«идемпотентного»);
  короткие — точный токен или подстрока. При полном промахе — триграммный
  фолбэк против опечаток, такие хиты помечаются «нечёткое совпадение».
- Сниппет: строка совпадения ±2 строки контекста, строка совпадения помечена
  маркером `>>>`; строки длиннее 240 символов усекаются. Для md в шапке
  хита — breadcrumb (ближайший заголовок выше, после `›`).
- **Файлы больше 5 МБ индексируются только по имени** (содержимое не
  читается); битый UTF-8 читается с потерями.

Параметры инструмента `kb_search` (кроме `query`/`limit`): `path_filter`
(подстрока или glob `*`/`?` по относительному пути), `extensions`
(переопределение `knowledge.extensions` на вызов), `names_only` (быстрый
поиск только по именам, содержимое не читается). CLI `arch-ml kb` и слэш
`/kb` используют базовый вариант (`query`+`limit`).

Формат вывода CLI:

```
── /path/to/spec.md:42 (score 87.5)
   строка контекста
>>> строка с совпадением
   строка контекста
```

В TUI: `/kb <query>` (до 10 хитов); результат дублируется на вкладку
«Знания». Агентный инструмент `kb_search` используется моделью в цикле —
например, cron-задача `kb_digest` (`docs/cron_and_md_pipes.md`) ходит
по тем же каталогам через `bash`.
