# Spine AI/ML Edition

> [!WARNING]
> **Локальный форк Spine Banking Edition** (`~/spine-bank`), точка
> форка `b928684`, 2026-09-11. Чистая локальная копия — история git не
> наследуется (см. `NOTICE.md`). Домен: харнесс AI/ML-исследователя
> (проектирование нейросетей, LLM-графтинг, CPT/SFT/RLHF/RL/RLVR, дистилляция,
> steering, агентные приложения). **Адаптация в процессе:** основной текст
> ниже пока описывает Banking Edition (унаследован); доменная зона `aiml/`
> наполняется — пресет ML-исследователя (`aiml/presets/ml-researcher/`,
> инварианты ML-01…ML-14), 8 доменных плагинов, fitness-библиотека
> (`aiml/library/fitness/`), роутинг гипотез по фактам проекта (ADR-047,
> `docs/hypotheses.md`); `banking/` оставлена как эталон толстого домена.

<p align="center">
  <b>🇷🇺 <a href="#русский">Русский</a></b> | <b>🇬🇧 <a href="#english">English</a></b>
</p>

<p align="center">
  <a href="https://github.com/romannekrasovaillm/spine-aiml-private/actions/workflows/ci.yml"><img src="https://github.com/romannekrasovaillm/spine-aiml-private/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

<p align="center">
  <img src="docs/screenshots/01-first-frame.png" alt="Первый кадр: чат сразу, без заставки — логотип в логе, поле ввода без рамки, подсказки клавиш из реестра · first frame: straight into the chat — logo in the log, frameless input, registry-driven key hints" width="88%">
</p>

<p align="center">
  <img src="docs/screenshots/02-chat-mermaid.png" alt="Spine: архитектурный ход — скиллы, база знаний, скоринг, живой mermaid-арт, индикатор контекста и фоновые субагенты · architecture turn: skills, KB, scoring, live mermaid, context gauge, background subagents" width="88%">
</p>

<p align="center">
  <img src="docs/screenshots/07-help.png" alt="Справка `?`: клавиши текущего экрана по разделам · `?` help: keys of the current screen, grouped by task" width="32%">
  <img src="docs/screenshots/03-model-picker.png" alt="Пикер моделей · model picker" width="32%">
  <img src="docs/screenshots/04-rubric.png" alt="Якорная рубрика, бенчмарк и индикатор контекста за порогом L1 · anchor rubric, benchmark and the context gauge past L1" width="32%">
</p>

<p align="center">
  <a href="docs/handoff_walkthrough.md"><img src="docs/screenshots/05-handoff.png" alt="Handoff кодовому харнессу: handoff_create собирает пакет .arch-handoff/, harness_run прогоняет Claude Code с умными таймаутами, контракт результата в сводке, control_check подтверждает целостность · handoff walkthrough" width="88%"></a>
</p>

<p align="center">
  <sub>Передача контекста кодовому харнессу — пошагово: <a href="docs/handoff_walkthrough.md">docs/handoff_walkthrough.md</a> ·
  Handing context to a coding harness, step by step.</sub>
</p>

<p align="center">
  <sub>Кадры — снимки реальных экранов TUI (рендерятся из кода тестом, не макеты); внизу каждого —
  живой индикатор контекстного окна · Screenshots are renders of the real TUI (generated from code,
  not mockups); note the live context-window gauge in each status bar.</sub>
</p>

> [!NOTE]
> **🇷🇺 Продукт:** Spine Banking Edition — продуктовый форк Spine (AD-BE4,
> ADR-010…014). Ядро — MIT (`LICENSE`), банковская надстройка `banking/` —
> proprietary (`LICENSE.banking`). Прежде чем строить на этом коде
> промышленное решение, зафиксируйте собственные инварианты и fitness-правила:
> артефакты (`docs/adr/`, `ARCHITECTURE-SPINE.md`) — рабочая методика, а не
> готовая гарантия под ваш контур.
> **🇬🇧 Product:** Spine Banking Edition — a product fork of Spine. Core is
> MIT-licensed (`LICENSE`); the banking layer `banking/` is proprietary
> (`LICENSE.banking`). Before building a production solution on this code,
> define your own invariants and fitness rules: the artifacts are a working
> methodology, not a ready-made guarantee for your context.

---

<a id="русский"></a>

## 🇷🇺 Русский

**Spine** — доменный харнесс solution-архитектора (банковский корпоративный
контур): тонкий агентный харнесс с TUI, библиотекой архитектурных скиллов
и контуром архитектурного контроля. Rust, edition 2024, один бинарь `arch-ml`:
TUI, CLI, библиотека.

Харнесс намеренно **тонкий**: ядерные инструменты (bash, файлы) плюс
специализированные инструменты архитектора. Тяжёлая часть — не код, а
дисциплина артефактов: спецификации, architecture-spine, ADR, рубрики,
fitness functions, handoff-пакеты кодовым агентам.

Идеи — разбор SDD-харнессов и корпоративных агентных фреймворков
(`docs/SOURCE_BRIEF.md`, август 2026: AI-Disrupt PDLC, AWS AI-DLC/Kiro,
BMAD, Spec Kit, OpenSpec и др.):

- **Маршрутизация изменений Fast/Standard/Critical** по Architecture
  Significance Score — 15 триггеров значимости (`src/control.rs`).
- **Architecture-spine**: позвоночник инвариантов с полями
  `Binds` / `Prevents` / `Rule` — фиксируется только то, в чём независимые
  исполнители могут разойтись несовместимо.
- **ADR-дисциплина**: решение записывается до реализации, с альтернативами,
  отрицательными последствиями и оценкой обратимости.
- **Evidence-подход**: оценка LLM-судьёй только со свидетельствами-цитатами;
  fitness functions — машинно-проверяемые утверждения о репозитории;
  headless JSON-контракт результата у кодовых харнессов и cron-задач.

### Пример: mermaid → ASCII в терминале

`arch-ml mermaid examples/mermaid/flow.mmd`:

```
       ┌──────────────────────────────┐
       │ Каналы: мобильный банк и веб │
       └──────────────────────────────┘
                      ┌┘
                      ▼ REST JSON
               ┌─────────────┐
               │ API Gateway │
               └─────────────┘
                      │
                      ▼ маршрутизация и аутентификация
          ┌───────────────────────┐
          │ Платёжный оркестратор │
          └───────────────────────┘
             ├────────┴статус-колбэк─┐
             ▼ команда списания      ▼ ISO 20022 платёжное поручение
┌────────────────────────┐   ┌───────────────┐
│ Ядро: счета и проводки │   │ Платёжный хаб │
└────────────────────────┘   └───────────────┘
             ┌авторизация─и─токены───┤
             ▼                       ▼ событие PaymentStatus
┌────────────────────────┐   ┌──────────────┐
│ Внешний платёжный шлюз │   │ Шина событий │
└────────────────────────┘   └──────────────┘
                       ┌─────────────┘
                       ▼ подписка на статусы
            ┌────────────────────┐
            │ Сервис уведомлений │
            └────────────────────┘
```

### Возможности

#### Модели и ризонинг

- **DeepSeek V4** (flash/pro), **GLM-5.2** (+ дешёвые 4.7/air/flash), **Kimi K3**
  (coding-поверхность) — переключение на лету: `/model` (пикер в TUI) или
  `arch-ml run --model`. Ключи — только из окружения или файла (`api_key_file`).
- **Любой OpenAI-совместимый провайдер** через `[models.<name>]` в конфиге
  (base_url, модель, `api_key_env`/`api_key_file`, лимит контекста) — появляется
  в пикере `/model` без пересборки (`docs/models.md`).
- **Переключатель ризонинга** `/think on|off|auto` (и `arch-ml run --think`):
  карты `thinking_on`/`thinking_off` в конфиге модели; CoT (`reasoning_content`)
  хранится и эхом возвращается в API (контракт DeepSeek thinking+tools);
  индикатор 🧠 в статус-баре.
- **Зрение и управление компьютером**: `screenshot`/`read_image` передают кадр
  модели (нативная мультимодальность `deepseek-flash`), `screen_size`/`window_list`
  дают размер экрана и окна; воздействие — `computer_*` (мышь, клавиатура) и
  `browser_*` (Chrome через CDP, клик по селектору, надёжный ввод кириллицы).
  Воздействие **выключено по умолчанию** и отнесено к классу `Destructive`
  (подтверждение на R4), координаты — нормализованные 0–1000, потому что
  провайдер ужимает кадр перед инференсом (`docs/tools.md`, ADR-041).
- Промышленная закалка стрима: ретрай обрыва SSE на любой фазе (с заметкой
  в чате), закрытие потока без `[DONE]`/`finish_reason` считается усечением
  и повторяется, вызов инструмента с обрезанными аргументами (потолок
  max_tokens / обрыв) отклоняется с точной причиной и стратегией
  восстановления — крупные файлы пишутся частями (`write_file mode=append`),
  таймаут тишины вместо общего таймаута, компактификация L1/L3,
  детекторы петель, редакция секретов в выводе и журнале.
- **Индикатор контекста** в статус-баре: `◈ 12.3k/1.0M ▰▰▱▱▱▱▱▱ 1%` —
  заполнение окна активной модели; шкала зелёная до порога L1, оранжевая
  до L3, дальше красная.

#### Библиотека скиллов и плагины (agent-plugins.org)

> **Плагин — единственная единица установки**: скиллы отдельно не ставятся,
> они живут внутри плагина (`skills/<имя>/SKILL.md`). `arch-ml skills …` —
> плоский индекс скиллов со всех плагинов, а не отдельный реестр.

- Восемь встроенных плагинов (`arch-ml init` раскладывает в `~/.arch-ml/plugins`):
  **arch-core** (15 методических скиллов архитектора, вкл. мета-скилл `skill-authoring`),
  **patterns-integration** (сага, outbox, CQRS, strangler+ACL, идемпотентность —
  дистилляты microservices.io), **patterns-resilience** (circuit breaker+retry,
  bulkhead, load leveling, cache-aside, throttling — дистилляты Azure),
  **aws-builders-library** (9 дистиллятов Amazon Builders' Library),
  **aws-agentic-ai** (агентные AI-паттерны AWS PG 2026), **arch-office**
  (12 скиллов офисных артефактов: docx-отчёты для МД, SAD, концепция,
  интеграционные спецификации, аудит, план миграции; pptx для правления и
  архкомитета; xlsx-каталоги, матрицы, реестр рисков — с генераторами
  python-docx/pptx/openpyxl) и **arch-governance** (управление библиотекой
  правил: 20 готовых fitness-функций для CONSTRAINTS.yaml, три волны
  внедрения, карточка дистилляции, антипаттерны расширения, карта 15 блоков
  источников), а также **spine-be-docs** (справка по самому продукту: скилл
  `check-spine-be-docs` отвечает на вопросы о Spine-BE по документации
  репозитория, а не по памяти).
- Поиск `arch-ml skills search`, показ `arch-ml skills show`; в TUI — `/skills`,
  `/skill` (в контекст сессии), `/plugins`; модель зовёт `skill_search`/`skill_load`
  сама. Дистилляция статей/контекста в новые скиллы — `skill_distill` и `/distill`.
- Сессии: `/new` — чистый лист с ротацией журнала; `/resume` — пикер сессий
  (стрелки + Enter), `/resume last` — мгновенно к последней; `/sessions` —
  журналы из append-only архива.
- **Failure-memory** «ошибся дважды → урок»: повторная подпись сбоя
  инструмента (пути/имена файлов схлопываются) порождает урок — заметка в
  чате + `state/failure_lessons.md` (`/lessons`), в режиме
  `[agent] failure_memory = "write"` — append в AGENTS.md проекта
  (`docs/failure_memory.md`).

Подробности: `docs/plugins_and_skills.md`.

#### Фоновые субагенты, ralph-циклы, worktree-фабрика

- `subagent_run/list/result` — фоновые исполнители со свежим контекстом и
  whitelist инструментов (спеки `agents/*.md` в плагинах); индикатор в
  статус-баре (`· ⣿ субагенты: N`), живой реестр задач и превью отчётов —
  вкладка `◉ Субагенты` (`F7`).
- `ralph_run` — ralph-цикл: до 6 раундов к неизменной цели свежими агентами,
  состояние — файлы + handoff (status/summary/evidence/next_steps/blockers).
- `worktree_new` + `arch-ml worktree new|list|diff|accept|drop` — изоляция
  агентной работы в git worktree; review/accept — человеком. Вердикт
  ревьювера `NOT-READY` по ветке блокирует accept (журналы флота, ADR-046);
  обход — только `--approver "<имя>"` с записью решения в журнал приёмки.
  Приёмка по ADR-024: до мержа — сверка `sha256` редакции правил с основной
  веткой и список изменённых `evidence/**` (предупреждения), после мержа —
  обязательный гейт правил основной ветки (`CONSTRAINTS-FAILED` при красном,
  мерж не откатывается; коды возврата 0 / 4 / 1) (`docs/fleet.md`).

#### Слоистая модель 5.2 + дельта-протокол

Инструментарий для флотов worktree БЕЗ полных копий спайна (по разбору
реального кейса: 15 worktree × полная копия → 90% дублей, дрейф копий):
спайн — в одной копии (SSOT), компонент несёт lean-дельту, изменения спайна —
только дельтами `changes/<id>`.

- `arch-ml fleet audit <paths…>|--repo <path>` — SSOT-аудит флота: точные дубли
  документации, файлы-ядро, дрейф копий с поимёнными отступниками
  (канон — majority-версия); дрейф → **exit 1**, порог дублей —
  `--fail-on-dupes <pct>` (гейты для CI). Агенту — инструмент `fleet_audit`.
- `arch-ml delta guard [--base origin/main...HEAD] [--protect <префикс>]` —
  CI-запрет прямых правок спайна мимо дельты: изменённые файлы под
  `model/`, `ARCHITECTURE-SPINE.md`, `CONSTRAINTS.yaml` обязаны упоминаться
  в активной дельте `changes/*/DELTA.md`, иначе **exit 1**.
- **SPEC.md в handoff-пакете** — шаблон верифицируемых контрактов интерфейсов
  (входы/выходы, структуры данных, границы ошибок, критерии верификации;
  EARS-стиль) вместо прозаического ARCHITECTURE.md компонента; как и
  CONSTRAINTS.yaml, не затирается повторной генерацией.

Живой мини-кейс: [`кейсы/fleet-spine-drift`](кейсы/fleet-spine-drift/) (007).

#### AGENTS.md для команд репозиториев

- `arch-ml agents-md refresh <repo>` — компилирует AGENTS.md из spine-инвариантов,
  CONSTRAINTS.yaml и манифестов; рукописная зона команды не затирается.
- `arch-ml agents-md lint <repo>` / `lint-all` — дрейф-контроль по хэшу входов
  (CI/крон по флоту репозиториев); рубрика `agents_md_quality`.

Подробности: `docs/agents_md.md`.

#### Губернанс: автономия, доказательства, метрики, дельты

- **R-уровни автономности** (`[policy] autonomy = "R2"`): каждый вызов
  инструмента классифицируется по риску — `rm -rf` получает DENY на уровне
  R2, журнал фиксирует попытки (AI-Disrupt PDLC). `arch-ml policy --check "<cmd>"`.
- **Evidence Bundle** — аудиторский след как гейт выпуска:
  `arch-ml evidence pack/verify` с профилями Fast/Standard/Critical.
- **Метрики**: `arch-ml metrics` — сессии, инструменты, ошибки, токены/₽, баллы
  рубрик, pass rate бенчей + трансформационные KPI: approval theater (доля
  бездумных согласий), architecture drift (дрейф AGENTS.md по флоту),
  cost per validated outcome.
- **Дельта-спеки**: `arch-ml delta new|validate|archive` — state machine OpenSpec;
  `arch-ml delta guard` — гейт прямых правок спайна мимо дельты (см. выше блок
  про модель 5.2). **Адаптер OpenSpec** (`arch-ml openspec scan|coverage|init|gate`,
  `docs/openspec.md`): требования SHALL/MUST из openspec/specs и changes →
  отчёт покрытия fitness-правилами (связь — поле `covers:` правила),
  генерация скелета CONSTRAINTS.from-openspec.yaml + SPINE.draft.md,
  archive-гейт change (exit 1).

**Как переключить уровень автономии (R0–R5).** Уровень задаётся в конфиге:

```toml
[policy]
autonomy = "R2"   # допустимы формы "R2", "r3", "4"
```

Файл конфига ищется в порядке: `./arch-ml.toml` (каталог запуска) →
`~/.config/arch-ml/config.toml`; либо явно — `arch-ml --config /path/strict.toml`.
Разовое ужесточение для конкретного репозитория: положите `arch-ml.toml`
с секцией `[policy]` в его корень и запускайте `arch-ml` оттуда. CLI-подкоманды
читают конфиг на каждый запуск; в TUI политика вшивается в реестр инструментов
при старте — после правки перезапустите `arch-ml` (фоновые субагенты наследуют
снимок конфига на момент своего запуска).

| Уровень | Чтение/поиск | Изменения (`write_file`, `cargo test`, `git commit`) | Деструктив (`rm -rf`, `git push --force`, `kubectl delete`) |
|---|---|---|---|
| R0–R1 | авто | эскалация человеку | DENY |
| R2 (дефолт) | авто | авто | DENY |
| R3 | авто | авто + обязательный журнал (в Spine он ведётся всегда) | DENY |
| R4 | авто | авто | эскалация человеку |
| R5 | авто | авто | авто — не рекомендуется, красный флаг аудита |

«Эскалация человеку» означает, что действие не выполняется: модель получает
отказ с текстом эскалации и корректно останавливается (в том числе в
headless-режиме); человек либо выполняет действие сам, либо поднимает уровень.
Проверка без исполнения: `arch-ml policy` — текущий уровень;
`arch-ml policy --check "rm -rf /tmp/x"` — класс риска и вердикт по команде.
Отказы журналируются в сессионный JSONL — материал аудита и детектора
approval theater. Bash классифицируется по тексту команды (паттерны —
`src/policy.rs`).

Харнесс применяет эти механики к самому себе: корневые `ARCHITECTURE-SPINE.md`
(10 инвариантов кодовой базы) и `CONSTRAINTS.yaml` (64 fitness-правила) гейтятся
CI-джобой `dogfood` (`arch-ml control spine` + `arch-ml control check .` + скан
персональных путей) — инварианты живут не на бумаге, а в пайплайне.

Подробности: `docs/governance.md`.

#### Учебные кейсы

- [`кейсы/`](кейсы/AGENTS.md) — сквозные примеры цикла solution-архитектора,
  подготовленные харнессом: от spine-инвариантов до handoff-пакета кодовому
  харнессу. Кейс 001 — [`sbp-gateway`](кейсы/sbp-gateway/) (платёжный шлюз
  СБП, C2B-приём; DeepSeek V4 Flash): spine AD-001…008, solutioning, 7 ADR,
  контракты/NFR/RFP, handoff-пакет с рубрикой и fitness-правилами.
  Кейс 002 — [`payment-processing-platform`](кейсы/payment-processing-platform/)
  (процессинг банка: рельсы карты/СБП/SWIFT/БЭСП; GLM-5.2): 27 инвариантов
  spine, 16 ADR, fitness-констрейнты под Go-код, walking skeleton.
  Кейс 003 — [`govproc-platform`](кейсы/govproc-platform/) (электронная
  коммерция госсектора: B2G-закупки 44-ФЗ, ЕИС, УКЭП; Kimi K3): 7 AD spine,
  5 ADR, NFR, OpenAPI-контракт, эмулятор внешней системы.
  Кейс 004 — [`parallel-epics`](кейсы/parallel-epics/) (три параллельных
  Claude Code по worktree, бэкенд deepseek-v4-pro): спайн AD-1…3 склеил
  стыки без взаимной видимости исполнителей — 15/15 тестов, интеграция с
  первой сборки; handoff-пакет по MCP; дефекты прогона → фиксы харнесса;
  цветные кадры в `screenshots/`.
  Кейс 005 — [`fleet-of-ten`](кейсы/fleet-of-ten/) (десять параллельных
  Claude Code, бэкенд deepseek-v4-pro): десять эпиков за ~3,2 мин стены —
  10/10 complete, 42/42 тестов, 60/60 fitness; флот сам закоммитил работу
  (контракт «Финализация»); цветные кадры в `screenshots/`.
  Кейс 006 — [`drift-control`](кейсы/drift-control/) (дрейф-эксперимент,
  две руки; Claude Code): одна задача платёжного ядра — голая vs с
  handoff-пакетом; механический гейт: FAIL 2/6 exit 1 (нет thiserror, нет
  идемпотентности — дрейф при зелёных тестах) против PASS 6/6; обе руки
  воспроизводимо перепроверяются из репозитория; цветные кадры в
  `screenshots/`.
  Кейс 007 — [`fleet-spine-drift`](кейсы/fleet-spine-drift/) (механический,
  без LLM): флот из трёх worktree с полными копиями спайна — `arch-ml fleet
  audit` измеряет дубли (66.7%) и дрейф `CONSTRAINTS.yaml` (отступник wt-c,
  exit 1), `arch-ml delta guard` запрещает прямые правки спайна мимо дельты;
  воспроизводится голым бинарём.
  Кейс 010 — [`fleet-patterns`](кейсы/fleet-patterns/) (механический, без
  LLM): движок оркестрации флотов по паттернам (ADR-042) — паттерн
  компилируется в граф, узлы идут волнами через пул процессов, каждый узел
  проходит детерминированные гейты, `fleet resume` продолжает прогон из
  журнала, отказ гейта останавливает масштабирование, вердикт ревьювера
  NOT-READY блокирует интеграцию; воспроизводится голым бинарём.

  Эксперимент — [`openspec-vs-spine`](experiments/openspec-vs-spine/)
  (масштабный прогон 2026-09-05): 10 задач × 3 контекста × 3 прогона ×
  2 модели = 180 генераций, пререгистрация, один детерминированный гейт
  arch-ml для всех. Без контекста 367 error-нарушений (0 % чистых), с
  контекстом OpenSpec-стиля 32, со спайн-форматом 24 (за вычетом ложных
  срабатываний regex — 26/21); преимущество спайна модель-зависимое.
  Входы, контексты, протоколы и 180 записей results.jsonl — в каталоге.

#### Прочее

- **TUI** (ratatui, Tokyo Night): стриминг, markdown, mermaid-арт на боковой
  вкладке (панель сама расширяется под ширину схемы, до 60% экрана; рендер
  не усечается), мышь, скроллбар диалога и кнопка «▼» — прыжок к свежему ответу,
  **выделение текста мышью с автокопированием в буфер обмена** (драг по логам;
  нативно через `arboard` — внешние утилиты не нужны; фолбэки: wl-copy/xclip/
  xsel, OSC 52), **многострочный ввод** (перевод строки —
  Shift+Enter, Alt+Enter или Ctrl+J; поле растёт до 8 строк, Up/Down — по строкам,
  на крайней — история), **очередь сообщений во время хода** карточкой в окне
  логов (Enter — в очередь, Alt+Enter или префикс «!!» — срочно первым),
  **прерывание хода**: Esc или Alt+Enter во время хода отменяют текущий запрос
  к модели или вызов инструмента (включая ожидание `harness_run`) — срочное
  сообщение из очереди стартует немедленно, история сессии остаётся
  консистентной (висячие tool-вызовы получают результат «прервано»),
  полноэкранный просмотр `F4` с горизонтальной панорамой, экспорт экрана в
  Word/Excel (`/export`), модалки выбора (`propose_options`).
- **Диагностика** `arch-ml doctor` — 10 проверок окружения с ✓/⚠/✗.
- **Eval-сьюты конфигурации** (`docs/evals.md`): continuous evals — регрессионный
  сьют задач с гейтом pass-rate (встроенный сьют `agent-config` герметичен и
  гоняется в CI; `--judge` — слой LLM-судьи по рубрике).
- **Рубрики и бенчмарки** архитектурного контроля (`docs/rubrics_and_benchmarks.md`).
- **Platform V Arch-Bench** (`benchmarks/platformv-arch-bench/`,
  `docs/platformv-benchmark.md`): бенчмарк из 24 архитектурных задач по
  документации Platform V (СберТех) с пререгистрацией, LLM-судьёй и
  статистикой — выбор модели для Spine, регрессионный гейт продуктовых фич
  (`scripts/run_regression.sh`) и сравнение кодовых харнессов для
  handoff-пакетов.
- **Архитектурный контроль**: score, spine-линтер, сенсоры спек, fitness,
  генератор ADR (`docs/control.md`). JVM-гейты — настоящим ArchUnit из того
  же CONSTRAINTS.yaml: `arch-ml archunit gen|check|fetch` (ADR-039,
  `docs/archunit.md`). **Корпоративный спайн** (`docs/corp-spine.md`):
  наследование CONSTRAINTS слоями ДКА → домен → продукт (`extends` с пином
  версии — обновление родителя приходит как error-находка, а не молчаливая
  поломка), детектор тех-радара `deny_dependency` (Cargo.toml/pom.xml/
  requirements.txt), override только через ADR (rule+adr+until, протухает),
  `severity: block|warn`, отчёт вверх `arch-ml control report --level corp
  --json`.
- **Handoff кодовым харнессам**: Claude Code, Qwen Code, OpenClaw, Hermes,
  Theseus, CodeWhale, Kimi Code — пакеты `.arch-handoff/` + прогон инструментом
  `harness_run` прямо из диалога (адаптер знает режим промпта, флаги
  разрешений; JSON-контракт результата разбирается **механически** —
  валидация схемы `Valid`/`Invalid`/`Missing`, эскалация blocked/conflicts,
  в CLI `status=blocked` даёт код выхода 2, непустые conflicts — 3).
  `background=true` — **фоновый прогон**: инструмент возвращается сразу
  (задача `hr-*` в общем реестре фоновых задач), агент остаётся доступным
  пользователю во время работы харнесса; статус — `subagent_list`, результат —
  `subagent_result`, полный лог — `reports/harness/<id>.log`.
  Окружение хоста в процесс харнесса протекает по умолчанию; whitelist
  `env_allow` в адаптере стартует его с чистым окружением. Предгейт
  пакета: git-репозиторий гарантирован (`git init` + baseline-коммит — якорь
  **плана отката** в TASK.md), маршрут значимости Fast/Standard/Critical
  задаёт рекомендованный таймаут прогона (1800/3600/7200 с, MANIFEST.json
  подхватывается `harness_run`); при Critical и epic-context ниже окна
  рубрики сборка пакета отклоняется, а грязное дерево (отслеживаемые файлы)
  — предупреждением: откат на baseline его потеряет. План отката — ещё и
  машиночитаемый (`ROLLBACK.yaml` в пакете) и **репетируется на гейте A4**:
  `arch-ml control gate A4 <repo> --rehearse` прогоняет шаги во временном
  git-worktree на baseline_commit (деструктивные/внешние шаги отклоняются с
  диагностикой), результат — `REHEARSAL.json` в evidence пакета; для Critical
  без успешной репетиции гейт не проходит (порог — `--require-rehearsal`,
  см. `docs/control.md`). Контракт
  TASK.md требует от исполнителя **финального git-коммита** (секция
  «Финализация» — результат забирается из git log); если исполнитель его
  не сделал, харнесс сам фиксирует оставшиеся правки **авто-коммитом**
  (кроме `.arch-handoff/` и интерпретерного мусора; `auto_commit = false`
  в конфиге адаптера выключает). **Умные
  таймауты**: абсолютный потолок адаптера (по умолчанию 30 мин, до 120 мин
  по маршруту Critical) + таймаут тишины 10 мин (нет вывода
  и изменений файлов репо → завис); молча работающий харнесс не трогаем,
  при прерывании убивается вся процессная группа, частичный вывод
  возвращается с рекомендацией `git status`
  (`docs/harness_integrations.md`; пошаговый разбор с кадрами —
  `docs/handoff_walkthrough.md`).

  > **⚠ Безопасность исполнения кодовых харнессов.** Адаптеры запускают
  > харнессы с флагами, обходящими интерактивные подтверждения (пример:
  > `claude -p --dangerously-skip-permissions` — без него headless-режим
  > вечно ждёт permission-промпт). Это допустимо **только в изолированном
  > контуре**: отдельный git worktree (`arch-ml worktree new`), sandbox/VM или
  > контейнер. Никогда не направляйте такой прогон в основной рабочий
  > чекаут и тем более в продакшен-контур — blast radius процесса с
  > отключёнными разрешениями вне изолята неприемлем. Слои сдерживания в
  > Spine: изоляция worktree + baseline-коммит как якорь отката, чистое
  > окружение процесса через whitelist `env_allow`, завершение всей
  > процессной группы по таймауту, авто-коммит для аудиторского следа.
  > Ввод в промышленный контур банка — только после hardening-трека
  > (threat model, sandboxing bash/harness-инструментов, запрет
  > skip-permissions вне изолята, SBOM, подпись и провенанс плагинов);
  > трек входит в дорожную карту поставки Banking Edition
  > (`banking/delivery/`). Статус зрелости и доказательства — в релизах,
  > CI и кейсах `кейсы/`.
- **MCP-клиент** (`docs/mcp.md`), **веб-доступ** (11 кураторских сайтов
  архитектора) и **локальная база знаний** (`docs/web_kb.md`).
- **MCP-сервер** `arch-ml mcp serve` (ADR-008): инструменты `spine_lint`, `fitness_check`,
  `significance_score`, `trace_check`, `model_query`, `rubric_run` наружу кодовым агентам
  (Claude Code и др.) — структурированный verdict (`passed` + находки) в момент написания
  кода; read-only, пути аргументами вызова (`docs/mcp.md`).
- **Планировщик md-задач** «md + cron + LLM + баш-пайпы» (`docs/cron_and_md_pipes.md`).
- **Библиотека промптов** (`assets/prompts/`, `arch-ml prompts`).
- **Глобальная md-память** (`MEMORY.md` в `~/.arch-ml`, в стиле Kimi Code):
  секция памяти инжектится в системный промпт каждой сессии (TUI и `arch-ml run`);
  агент дописывает в неё факты по просьбе пользователя («запомни …») —
  fs-инструментами, а вручную — `/memory add` или
  `arch-ml memory add` (путь настраивается: `paths.memory_file`).

### Разработка в цифрах: токены и где окупилась архитектура

Проект за 4 дня (14–17 августа 2026) написан AI-агентом (Kimi K3) под
управлением solution-архитектора-человека. Расход измерен точно — сумма
событий `usage.record` из wire-логов трёх сессий (72 агента: основной +
рой субагентов):

| Метрика | Значение |
|---|---|
| LLM-запросов | 5 628 |
| Output (код, доки, ответы) | 4,2 млн токенов |
| Свежий input (новый контент) | 15,9 млн токенов |
| Cache-read (перечитывание контекста ходами) | 986,6 млн токенов |
| **Всего обработано** | **≈ 1,007 млрд токенов** |

Отдельно, не в этой сумме: прогоны самого харнесса (кейсы 001–006, флоты
исполнителей) на DeepSeek/GLM/Kimi K3 API — ещё ~5–10 млн токенов.

Где окупилась архитектурная дисциплина (работа архитектора, а не модели):

- **Спайн удерживает дрейф — доказано контролируемым экспериментом**
  ([кейс 006](кейсы/drift-control/)): одна задача двумя руками — голая →
  дрейф по орг-инвариантам при полностью зелёных тестах (гейт FAIL, exit
  1), с handoff-пакетом → PASS 6/6. Цена спайна по стене — нулевая
  (360 с против 372 с).
- **Спайн как клей параллелизма** (кейсы [004](кейсы/parallel-epics/) и
  [005](кейсы/fleet-of-ten/)): 3 и 10 исполнителей без взаимной видимости
  сошлись на контрактах с первой сборки; контракт «Финализация», рождённый
  из дефекта кейса 004, дал 10/10 самокоммитов флота в кейсе 005.
- **Якорные рубрики как детектор слабости обвязки**: оценка handoff_quality
  2.90/5 на Critical-кейсе локализовала разрыв (текстовый JSON-контракт
  результата не парсился механически) — фиксы ушли в генератор handoff и
  предгейты, а не в кодовый агент.
- **Внешнее архитектурное ревью → инженерные гейты**: по ревью появились
  догфудинг (свои `ARCHITECTURE-SPINE.md` + `CONSTRAINTS.yaml` и CI-джоба
  dogfood — она уже поймала реальный инцидент: personal-paths scan
  остановил коммит с мусором сборки), clippy `-D warnings` с явной
  политикой исключений, миграция на поддерживаемый YAML-крейт, блок
  безопасности исполнения кодовых харнессов.

### Быстрый старт

```bash
cargo build --release          # бинарь: target/release/arch-ml
ln -sf "$PWD/target/release/arch-ml" ~/.local/bin/arch-ml   # запуск одним словом `arch-ml`
arch-ml init                      # конфиг + ассеты в ~/.arch-ml и
                               # ~/.config/arch-ml/config.toml

# API-ключи — только через окружение (в конфиг пишутся лишь ИМЕНА переменных):
export DEEPSEEK_API_KEY=...    # deepseek (v4-flash, по умолчанию), deepseek-pro (v4-pro)
export ZHIPU_API_KEY=...       # glm (glm-5.2 + дешёвые 4.7/air/flash)
export KIMI_API_KEY=...        # kimi (k3, coding-поверхность) или файл ~/.kimi_api_key

arch-ml                           # интерактивный TUI (одно слово)
arch-ml run -q "собери ADR по саге" > adr.md   # строгий headless для скриптов
arch-ml run -q --timeout 600 --max-turns 24 "…" > adr.md   # с бюджетами для CI/cron
arch-ml models                    # проверить настроенные модели
```

Смоук без LLM: `arch-ml mermaid examples/mermaid/flow.mmd`,
`arch-ml control score --trigger new_component=true`,
`arch-ml control spine examples/specs/ARCHITECTURE-SPINE.example.md`.

### Монорепозиторий: вендоренный Archify

Движок диаграмм Archify (JSON IR → HTML/SVG, MIT) вендорен в
`vendor/archify/` — контур `arch-ml archify …` и инструменты `archify_*`
работают из клона без внешних скачиваний: достаточно указать в конфиге
`[archify].cli_path = "<клон>/vendor/archify/bin/archify.mjs"` (Node ≥18).
Политика вендоринга и обновление версии — `vendor/README.md`;
методика — плагин `ru-archify` (ADR-027).

### Настройка персональных путей

Вся привязка к машине пользователя — только в конфиге (в коде — нейтральные
плейсхолдеры). Порядок поиска конфига: `--config <path>` → `./arch-ml.toml`
→ `~/.config/arch-ml/config.toml` → встроенные дефолты. Все секции
опциональны — указывайте только отличия от дефолтов.

```toml
# ~/.config/arch-ml/config.toml
[knowledge]
dirs = ["~/Документы/архитектура", "~/library"]     # ваша база знаний (kb_search)

[plugins]
dirs = ["~/.arch-ml/plugins", "~/my-plugins"]  # ваши библиотеки плагинов

[paths]                          # куда писать данные харнесса
assets_dir = "~/.arch-ml/assets"     # промпты, рубрики, бенчи
reports_dir = "~/.arch-ml/reports"   # отчёты рубрик/крона/субагентов
sessions_dir = "~/.arch-ml/sessions" # журналы сессий
state_dir = "~/.arch-ml/state"       # состояние (failure-memory и др.)
```

- Тильда `~/` раскрывается автоматически; относительные пути — от текущего каталога.
- API-ключи: `api_key_env` (имя env-переменной) или `api_key_file` (путь к файлу,
  напр. `~/.kimi_api_key`) — сами ключи в конфиг не пишутся никогда.
- Проверка после правки: `arch-ml doctor` (покажет доступность каталогов и ключей)
  и `arch-ml kb "запрос"` (находит ли ваша база).

Полный образец с комментариями — `config.example.toml`.

### CLI

```
arch-ml [--config <path>] <command>   # без команды — TUI
```

| Команда | Назначение |
|---|---|
| `tui` | Интерактивный TUI (действие по умолчанию) |
| `init` | Инициализация `~/.arch-ml`: конфиг, ассеты, примеры |
| `run [prompt] [--model] [--no-stream] [--quiet] [--timeout SECS] [--max-turns N] [--think on\|off]` | Headless-прогон агента; `-` или пайп — stdin; `-q`: stdout только финальный ответ (для скриптов); бюджеты `--timeout`/`--max-turns` (превышение таймаута — exit 1); прогресс стрим-режима — в stderr |
| `models` | Список настроенных моделей |
| `prompts [name]` | Библиотека промптов |
| `memory [add <текст>]` | Глобальная md-память (`MEMORY.md`): показать / дописать заметку |
| `mermaid <file>` | Рендер mermaid в Unicode/ASCII-арт (flowchart, sequenceDiagram, erDiagram, C4Context/C4Container/C4Component) |
| `archify doctor` / `archify guide <q>` / `archify validate <тип> <файл>` / `archify deliver <тип> <файл> <html>` / `archify compare <base> <head> <html>` | Контур диаграмм Archify (JSON IR → HTML/SVG): валидация 9 checks, доставка с SHA-256 receipt, delta-review снапшотов; exit code = гейт CI (требуется `[archify].cli_path`, см. `config.example.toml`) |
| `rubric list` / `rubric run <rubric> <target>` | Рубрики: список / оценка LLM-судьёй |
| `bench list` / `bench run <name>` / `bench run --golden` | Архитектурные бенчмарки; `--golden` — калибровка LLM-судьи по golden-set (MAE против эталона; выше `judge.golden_max_mae` — exit 1) |
| `eval run [--suite <dir>] [--gate <pct>] [--judge]` | Регрессионный eval-сьют конфигурации харнесса (continuous evals): детерминированные проверки офлайн + опциональный LLM-судья; pass-rate ниже гейта (дефолт 100%) — exit 1 (`docs/evals.md`) |
| `kb <query> [--limit]` | Поиск по локальной базе знаний |
| `web search <query> [--arch]` / `web fetch <url>` / `web sites` | Веб: поиск, фетч, кураторские сайты |
| `mcp list` / `mcp call <server__tool>` | MCP-серверы и вызовы инструментов |
| `mcp serve` | MCP-сервер (stdio): архитектурный контроль кодовым агентам — verdict в момент написания кода (ADR-008, `docs/mcp.md`) |
| `handoff <harness> --repo <path> --task <text>` | Handoff-пакет `.arch-handoff/` |
| `harness-run <harness> --repo <path> [--task]` | Прогнать кодовый харнесс по пакету |
| `harnesses` | Известные кодовые харнессы и их доступность |
| `control check/spine/sensors/score/adr` | Архитектурный контроль (fitness, линтеры, значимость) |
| `control report --level corp [--json]` | Отчёт вверх по корп-спайну (`docs/corp-spine.md`): покрытие унаследованных правил, overrides, просроченные |
| `archunit gen/check/fetch` | ArchUnit-мост (ADR-039): JVM-гейты из CONSTRAINTS.yaml настоящим ArchUnit — standalone-раннер без правок Java-репо, JUnit-тест для встраивания, пинnutые jar'ы |
| `control gate A4 <repo> [--rehearse]` | Гейт A4: репетиция отката handoff-пакета (rollback-first для Critical) |
| `model validate/show/graph/project/export/import` | Типизированная модель архитектуры (model/): ссылочная целостность, карточки сущностей, граф связей, проекция ADR; обмен с отраслевыми форматами — экспорт SYS/CMP/INT в Structurizr DSL/PlantUML/drawio, импорт Structurizr DSL (round-trip, ADR-009) |
| `trace check <dir>` | Трассируемость модели: покрытие звеньев REQ → NFR → AD/ADR → CMP → fitness-правило, сироты, exit 1 на обязательных звеньях |
| `nfr budget/availability/capacity/cost <dir>` | Количественные NFR поверх модели (ADR-007): latency-бюджет по hop'ам INT-* против цели p99 (расхождение — error с виновными hop'ами), доступность участков против SLA (+RTO/RPO), ёмкость против RPS-цели, TCO и цена выхода; error → exit 1 |
| `agents-md refresh/lint/lint-all <repo>` | AGENTS.md для репозиториев команд |
| `evidence pack/verify` | Evidence Bundle как гейт выпуска |
| `metrics` | Операционные и трансформационные KPI |
| `delta new/list/validate/archive/guard` | Дельта-спецификации (OpenSpec); `guard` — гейт прямых правок спайна мимо дельты (exit 1) |
| `openspec scan/coverage/init/gate` | Адаптер OpenSpec (`docs/openspec.md`): требования SHALL/MUST → покрытие через `covers:` правил CONSTRAINTS (`--strict` → exit 1 на «без решения»), скелет CONSTRAINTS.from-openspec.yaml + SPINE.draft.md, archive-гейт change (exit 1) |
| `fleet audit [paths…] [--repo] [--include] [--fail-on-dupes]` | SSOT-аудит флота worktree: дубли и дрейф копий спайна (дрейф → exit 1) |
| `fleet plan propose\|validate\|show <plan>` | План флота (ADR-042): паттерн оркестрации → граф узлов; propose — черновик по handoff-пакету или из набора ADR (`--from-adrs [--adr-dir] [--status proposed\|accepted\|all]`, ADR-045), validate — механический гейт плана, включая независимость исполнителей (`--json`, exit 1) |
| `fleet run --repo <path> --plan <file> [--judge <путь>]` | Прогон плана волнами с пулом процессов и гейтами узла (legacy: `--items-file` — плоский веер); `--judge` фиксирует baseline-судью гейтов — судья внутри ворктри узла исполняется им, а без baseline такой гейт падает (ADR-046) |
| `fleet resume <run-id> [--force-rerun]` | Продолжение прогона плана из журнала событий: завершённые узлы пропускаются |
| `skills list/search/show` / `plugins list/show` | Библиотека скиллов и плагинов |
| `policy [--check "<cmd>"]` | Политика автономии R0–R5 |
| `doctor` | Диагностика окружения |
| `weights list\|verify [--manifest F] [--json]` | Реестр артефактов ML (`artifacts.yaml`): список / сверка с ФС — копия вместо симлинка при `kind=weights\|dataset` → Fail (C-032/C-033), `sha256` через `sha256sum` |
| `data-card check [--cards F\|D] [--dataset P] [--strict]` | Карточки датасетов: обязательные поля (пробел ≠ провал) и противоречия (`sha256`/`records` без датасета) |
| `trajectory metrics --input F [--format …] [--k N] [--json]` | Eval траекторий: `success_rate`, `ci95` (Уилсон по задачам), `pass@k` (Chen et al. 2021) |
| `export <word\|excel> <session> <out>` | Экспорт журнала сессии |
| `cron list/run/tick` | Планировщик md-задач |
| `worktree new/list/diff/accept [--approver]\|drop` | Worktree-фабрика; accept блокируется вердиктом ревьювера `NOT-READY` (ADR-046), делает сверки до мержа и post-merge гейт правил (ADR-024; exit 0 / 4 / 1) |
| `fleet merge <run-id> [--owner-approve] [--approver]` | Гейт мерджа прогона: без подтверждения — сводка и отказ; `--approver` — именной обход `NOT-READY` с записью в журнал приёмки (ADR-046) |
| `evolve propose/commit/list/reject` | Самоулучшение харнесса (H2.2): предложение → именной аппрувер → гейты baseline-судьёй → применение; managed-блоки для доменного слоя, `mode: file` для кода ядра `src/**` (ADR-046) |

### Документация

- `docs/architecture.md` — устройство, контракты, как расширять.
- `AGENTS.md` — путеводитель для агентов и контрибьюторов (установка, карта модулей, конвенции); `AGENTS-READERS.md` — для агентов-читателей (навигация по идеям за 5 минут, безопасное чтение без ключей).
- `docs/slash_commands.md` — слэш-команды TUI; `docs/tools.md` — инструменты (карта «база vs архитектурные» + полные параметры).
- `docs/models.md` — подключение LLM (DeepSeek/Kimi/GLM, свои endpoint'ы).
- `docs/plugins_and_skills.md` — плагины и библиотека скиллов.
- `docs/hypotheses.md` — карточки гипотез и роутинг по фактам проекта (ADR-047).
- `docs/failure_memory.md` — память сбоев инструментов («ошибся дважды → урок»).
- `docs/rubrics_and_benchmarks.md`, `docs/control.md`, `docs/governance.md`,
  `docs/harness_integrations.md`, `docs/handoff_walkthrough.md` (передача
  контекста кодовому харнессу, кадр за кадром), `docs/mcp.md`, `docs/cron_and_md_pipes.md`,
  `docs/web_kb.md`, `docs/agents_md.md`, `docs/evals.md` (continuous evals
  конфигурации харнесса), `docs/archify.md` (контур диаграмм JSON IR →
  HTML/SVG), `docs/SOURCE_BRIEF.md` (источник идей).

Конфигурация: `config.example.toml`, `cron.example.toml`. Тесты: `cargo test` (включая
интеграционные CLI-тесты `tests/cli.rs` на `assert_cmd`; live-LLM — `#[ignore]`d).
CI: fmt / clippy / test / MSRV 1.85 / cargo audit — `.github/workflows/ci.yml`.

---

<a id="english"></a>

## 🇬🇧 English

**Spine** is a *domain agent harness for solution architects* (banking-grade
corporate environments): a thin, Rust-built agent that lives in your terminal
and speaks the language of architecture work — ADRs, architecture-spine
invariants, rubrics, fitness functions, handoff packages for coding agents.
One binary, `arch-ml`: TUI, CLI, and library.

The harness is deliberately **thin**: core tools (bash, files) plus a small
set of architect-specific tools. The heavy lifting is artifact discipline,
not code. Ideas come from a review of SDD harnesses and corporate agentic
frameworks (`docs/SOURCE_BRIEF.md`, Aug 2026: AI-Disrupt PDLC, AWS AI-DLC/Kiro,
BMAD, Spec Kit, OpenSpec, and more):

- **Fast/Standard/Critical change routing** via a 15-trigger Architecture
  Significance Score (`src/control.rs`).
- **Architecture-spine**: a backbone of invariants with `Binds`/`Prevents`/`Rule`
  fields — only what independent implementers could get *incompatibly wrong*.
- **ADR discipline**: decisions are recorded *before* implementation, with
  alternatives, negative consequences, and reversibility assessment.
- **Evidence over opinion**: rubric scoring by an LLM judge only with quoted
  evidence; fitness functions as machine-checkable claims about the repo;
  headless JSON result contracts for coding harnesses and cron tasks.

### Feature tour

**Models & reasoning**

- DeepSeek V4 (flash/pro), GLM-5.2 (+ budget 4.7/air/flash), Kimi K3 (coding
  surface) — switch mid-session via `/model` (TUI picker) or `arch-ml run --model`.
  Keys come from the environment or a key file (`api_key_file`) — never stored.
- **Any OpenAI-compatible provider** via `[models.<name>]` in the config
  (base_url, model id, `api_key_env`/`api_key_file`, context limit) — shows up
  in the `/model` picker without a rebuild (`docs/models.md`).
- **Reasoning toggle** `/think on|off|auto` (and `arch-ml run --think`): per-model
  `thinking_on`/`thinking_off` maps merged into the request body; chain-of-thought
  (`reasoning_content`) is stored and echoed back (DeepSeek thinking+tools
  contract); 🧠 indicator in the status bar.
- **Vision and computer control**: `screenshot`/`read_image` feed a frame to the
  model (native `deepseek-flash` multimodality), `screen_size`/`window_list`
  report the display and windows; actuation is `computer_*` (mouse, keyboard)
  and `browser_*` (Chrome over CDP — click by selector, reliable Cyrillic
  input). Actuation is **off by default** and classified `Destructive`
  (confirmation at R4); coordinates are normalized 0–1000 because the provider
  downscales the frame before inference (`docs/tools.md`, ADR-041).
- Hardened streaming: mid-stream break auto-retry with an in-chat note; a
  stream closed without `[DONE]`/`finish_reason` is treated as truncated and
  retried; a tool call whose arguments arrive cut off (max_tokens ceiling or
  a broken stream) is rejected with the precise cause and a recovery
  strategy — large files are written in chunks (`write_file mode=append`);
  silence-timeout instead of a whole-request timeout, L1/L3 compaction,
  loop detectors, secret redaction in tool output and journals.
- **Context gauge** in the status bar: `◈ 12.3k/1.0M ▰▰▱▱▱▱▱▱ 1%` — live
  fill of the active model's context window; the bar is green up to the L1
  threshold, orange up to L3, red beyond.

**Skills library & plugins** ([agent-plugins.org](https://agent-plugins.org) layout)

> **The plugin is the only install unit**: skills never install separately —
> they live inside a plugin (`skills/<name>/SKILL.md`). `arch-ml skills …` is a
> flat index over all plugins, not a separate registry.

- Eight built-in plugins (deployed by `arch-ml init`): **arch-core** (15 architecture
  method skills incl. the `skill-authoring` meta-skill), **patterns-integration**
  (saga, outbox, CQRS, strangler+ACL — distilled from microservices.io),
  **patterns-resilience** (circuit breaker, bulkhead, load leveling — Azure
  patterns), **aws-builders-library** (9 distillates of Amazon Builders'
  Library), **aws-agentic-ai** (AWS agentic-AI patterns), **arch-office**
  (12 office-artifact skills with python-docx/pptx/openpyxl generators:
  board reports, SAD, architecture vision, integration specs, audits,
  migration roadmaps, decision matrices, risk registers),
  **arch-governance** (rule-library governance: 20 ready fitness functions
  for CONSTRAINTS.yaml, three rollout waves, distillation card, library
  antipatterns, a map of 15 architecture-source blocks), and
  **spine-be-docs** (product self-help: the `check-spine-be-docs` skill
  answers questions about Spine-BE from the repository documentation,
  not from memory).
- `skill_search`/`skill_load` tools, `/skills`, `/distill` (distill articles or
  the session transcript into new skills), `/new` (fresh session with journal
  rotation), `/resume` (session picker: arrows + Enter), `/sessions`.
- **Failure memory** ("fail twice → lesson"): a repeated tool-failure
  signature (paths/file names collapsed) produces a lesson — chat note +
  `state/failure_lessons.md` (`/lessons`); with
  `[agent] failure_memory = "write"` it is appended to the project AGENTS.md.

**Background sub-agents, ralph loops, worktree factory**

- `subagent_run/list/result` — fresh-context background executors with
  least-privilege tool whitelists (specs in plugin `agents/*.md`); live
  status-bar indicator (`· ⣿ subagents: N`), with the `◉ Subagents` right-panel
  tab (`F7`) showing the live registry and report previews.
- `ralph_run` — multi-round cycles toward an immutable objective, each round a
  fresh agent; state travels via workspace files + bounded handoff JSON.
- `worktree_new` + `arch-ml worktree …` — isolated git worktrees for risky or
  parallel agent work; review/accept stays with the human (accept runs the
  ADR-024 acceptance procedure: rules-edition `sha256` compare and modified
  `evidence/**` warnings before the merge, mandatory fitness gate on the main
  tree after it — exit codes 0 / 4 / 1, see `docs/fleet.md`).

**Layered model 5.2 + delta protocol**

Tooling for worktree fleets WITHOUT full spine copies (from a real-case
review: 15 worktrees × full spine copy → 90% duplicate docs, measurable
copy drift): the spine lives in one root copy (SSOT), a component carries a
lean delta, and spine changes travel only via `changes/<id>` deltas.

- `arch-ml fleet audit <paths…>|--repo <path>` — fleet SSOT audit: exact
  documentation duplicates, core files present in every worktree, and content
  drift with named deviants (canon = majority version); drift → **exit 1**,
  plus a `--fail-on-dupes <pct>` threshold — both are CI gates. For the
  agent, the same audit is exposed as the `fleet_audit` tool.
- `arch-ml delta guard [--base origin/main...HEAD] [--protect <prefix>]` —
  CI ban on direct spine edits bypassing a delta: changed files under
  `model/`, `ARCHITECTURE-SPINE.md`, `CONSTRAINTS.yaml` must be mentioned in
  an active `changes/*/DELTA.md`, otherwise **exit 1**.
- **SPEC.md in the handoff package** — a verifiable interface-contracts
  template (inputs/outputs, data structures, error boundaries, verification
  criteria; EARS style) replacing the component's prose ARCHITECTURE.md;
  like CONSTRAINTS.yaml, it survives regeneration untouched.

Live mini-case: [`кейсы/fleet-spine-drift`](кейсы/fleet-spine-drift/) (007).

**Governance & control**

- **R0–R5 autonomy levels** (`[policy] autonomy`): every tool call is risk-classified
  (`rm -rf` → DENY at R2), attempts journaled.
- **Evidence Bundle** (`arch-ml evidence pack/verify`), **delta-specs**
  (OpenSpec state machine + `delta guard` CI gate, see "Layered model 5.2"
  above), **OpenSpec adapter** (`arch-ml openspec scan|coverage|init|gate`,
  `docs/openspec.md`): SHALL/MUST requirements from `openspec/` mapped to
  fitness rules via the rule's `covers:` field — coverage report
  (`--strict` exits 1 on undecided requirements), skeleton
  CONSTRAINTS.from-openspec.yaml + SPINE.draft.md generation, and a change
  archive gate (exit 1). **AGENTS.md generator + drift linter** for team
  repos.
- **Metrics** (`arch-ml metrics`): operational counters plus transformation KPIs —
  approval-theater detection, architecture drift rate, cost per validated outcome.
- **Platform V Arch-Bench** (`benchmarks/platformv-arch-bench/`,
  `docs/platformv-benchmark.md`): 24 architecture tasks built on Platform V
  (SberTech) documentation with preregistration, an evidence-bound LLM judge
  and bootstrap statistics — model selection for Spine, a regression gate for
  product features (`scripts/run_regression.sh`), and coding-harness
  comparison for handoff packages.
- **Anchor & dynamic rubrics** with an evidence-bound LLM judge (k-sample
  median, quote verification, prompt-injection isolation — ADR-004), banking
  architecture benchmarks, fitness functions, spine linter; JVM gates are
  executed by real ArchUnit from the same CONSTRAINTS.yaml —
  `arch-ml archunit gen|check|fetch` (ADR-039, `docs/archunit.md`).
  **Corporate spine** (`docs/corp-spine.md`): layered CONSTRAINTS
  inheritance (corp → domain → product) via `extends` with a version pin —
  a parent update surfaces as an error finding ("parent updated", re-pin
  consciously) instead of a silent break; tech-radar `deny_dependency`
  detector (Cargo.toml/pom.xml/requirements.txt); overrides only via ADR
  (rule+adr+until, they expire); `severity: block|warn`; upward reporting
  `arch-ml control report --level corp --json`; judge
  calibration
  gate: `arch-ml bench run --golden` (MAE vs golden set, exit 1 above
  `judge.golden_max_mae`). **Continuous evals of the harness configuration**
  (`arch-ml eval run`, `docs/evals.md`): a deterministic suite with a pass-rate
  gate (default 100%, exit 1 below) — the built-in `agent-config` suite runs
  hermetically and gates every CI build; `--judge` adds the rubric-LLM layer.
- **Typed architecture model** (`arch-ml model validate/show/graph/project/export/import`):
  markdown+frontmatter entities (CAP/SYS/CMP/INT/NFR/REQ/AD/ADR/RISK/OWNER/QAS)
  with referential-integrity validation, relation graph, and ADR projection
  (ADR-003); industry-format exchange — export SYS/CMP/INT to Structurizr
  DSL/PlantUML/drawio, import Structurizr DSL back (round-trip, ADR-009).
  **Traceability as a fitness function** (`arch-ml trace check`):
  REQ → NFR → AD/ADR → CMP → fitness-rule coverage with named orphans,
  exit 1 on mandatory links (ADR-006).
- **Quantitative NFRs** (`arch-ml nfr budget/availability/capacity/cost`, ADR-007):
  latency-budget decomposition over `INT-*` hops vs the p99 target (mismatch →
  error naming the guilty hops), availability composition (serial ∏Aᵢ,
  parallel 1−(1−A)ⁿ) vs SLA with RTO/RPO targets, capacity vs RPS target, TCO
  and exit price — all from entity data, deterministic, no LLM. **Quality
  attribute scenarios** (`QAS-*` entities: source/stimulus/artifact/response/
  measure) unfold automatically into the acceptance-criteria section of the
  handoff `TASK.md`.
- **MCP server** `arch-ml mcp serve` (ADR-008): exposes `spine_lint`, `fitness_check`,
  `significance_score`, `trace_check`, `model_query`, `rubric_run` to coding agents
  (Claude Code etc.) — structured verdict (`passed` + findings) at code-writing
  time; read-only, all targets passed as call arguments (`docs/mcp.md`).

**Switching the autonomy level (R0–R5).** The level lives in the config:

```toml
[policy]
autonomy = "R2"   # "R2", "r3" and "4" are all accepted
```

Config resolution order: `./arch-ml.toml` (launch directory) →
`~/.config/arch-ml/config.toml`; or pass an explicit file —
`arch-ml --config /path/strict.toml`. To harden a single repository, drop an
`arch-ml.toml` with a `[policy]` section into its root and run `arch-ml`
from there. CLI subcommands re-read the config on every invocation; in the TUI
the policy is baked into the tool registry at startup, so restart `arch-ml` after
editing (background subagents inherit the config snapshot taken at spawn time).

| Level | Read/search | Mutating (`write_file`, `cargo test`, `git commit`) | Destructive (`rm -rf`, `git push --force`, `kubectl delete`) |
|---|---|---|---|
| R0–R1 | auto | escalates to human | DENY |
| R2 (default) | auto | auto | DENY |
| R3 | auto | auto + mandatory journal (Spine journals everything anyway) | DENY |
| R4 | auto | auto | escalates to human |
| R5 | auto | auto | auto — not recommended, audit red flag |

"Escalates to human" means the action is not executed: the model receives a
refusal with escalation text and stops cleanly (including headless mode); the
human either performs the action themselves or raises the level. Verify without
executing: `arch-ml policy` prints the current level;
`arch-ml policy --check "rm -rf /tmp/x"` shows the risk class and verdict. Denials
are journaled to the session JSONL — input for audit and the approval-theater
detector. Bash commands are classified by command text (patterns in
`src/policy.rs`).

Spine eats its own dog food: the repo root carries `ARCHITECTURE-SPINE.md`
(10 invariants of the harness codebase) and `CONSTRAINTS.yaml` (64 fitness
rules), enforced by the `dogfood` CI job (`arch-ml control spine` +
`arch-ml control check .` + a personal-path scan) — the invariants live in the
pipeline, not on paper.

**Handoff to coding harnesses**: Claude Code, Qwen Code, OpenClaw, Hermes,
Theseus, CodeWhale, Kimi Code — `.arch-handoff/` packages with invariants, acceptance
criteria, and a headless JSON contract, plus in-chat execution via the
`harness_run` tool (the adapter knows the prompt mode and permission flags;
the JSON result contract is **parsed mechanically** — schema validation
`Valid`/`Invalid`/`Missing`, blocked/conflicts escalation, and the CLI
exits with code 2 on `status=blocked` and 3 on non-empty conflicts).
`background=true` runs the harness **in the background**: the tool returns
immediately (an `hr-*` task in the shared background-task registry), so the
agent stays responsive while the harness works; check `subagent_list` for
status and `subagent_result` for the report, full log at
`reports/harness/<id>.log`. The
host environment leaks into the harness process by default; the adapter
`env_allow` whitelist starts it with a clean environment. Package pre-gate:
a git repository is guaranteed (`git init` + a baseline commit — the anchor
of the **rollback plan** in TASK.md), and the significance route
Fast/Standard/Critical sets the recommended run timeout (1800/3600/7200 s,
carried in MANIFEST.json and picked up by `harness_run`); on Critical with
epic context below the rubric window the package build is refused, and a
dirty tracked tree triggers a warning (rollback to baseline would lose it).
The rollback plan is also machine-readable (`ROLLBACK.yaml` in the package)
and is **rehearsed at gate A4** (rollback-first): `arch-ml control gate A4
<repo> --rehearse` runs the steps in a throwaway git worktree on
`baseline_commit` (destructive/outward steps are refused with diagnostics)
and records `REHEARSAL.json` as package evidence; the Critical route cannot
pass A4 without a successful rehearsal (threshold: `--require-rehearsal`,
see `docs/control.md`). The TASK.md
contract requires a **final git commit** from the executor (the
"Finalization" section — results are collected from the git log); if the
executor finishes without committing, the harness commits the leftover
changes itself (**auto-commit**, excluding `.arch-handoff/` and interpreter
junk; disable with `auto_commit = false` in the adapter config).
**Smart timeouts**:
the adapter's absolute ceiling (30 min by default, up to 120 min on the
Critical route) plus a 10-min silence timeout (no output and no
repo file changes → the run is hung); a quiet but working harness is left
alone, on abort the whole process group is killed (no orphans), and partial
output comes back with a `git status` recommendation
(`docs/harness_integrations.md`; step-by-step walkthrough with frames —
`docs/handoff_walkthrough.md`).

> **⚠ Coding-harness execution safety.** Adapters launch harnesses with flags
> that bypass interactive confirmations (e.g. `claude -p
> --dangerously-skip-permissions` — without it the headless mode waits on a
> permission prompt forever). This is acceptable **only inside an isolated
> boundary**: a separate git worktree (`arch-ml worktree new`), a sandbox/VM, or
> a container. Never point such a run at your main working checkout, let
> alone a production environment — the blast radius of a process with
> permissions switched off is unacceptable outside an isolate. Containment
> layers in Spine: worktree isolation + a baseline commit as the rollback
> anchor, a clean process environment via the `env_allow` whitelist, whole
> process-group kill on timeout, and auto-commit for the audit trail.
> Entering a bank's production perimeter requires the hardening track
> first (threat model, sandboxing of the bash/harness tools, a ban on
> skip-permissions outside isolates, SBOM, plugin signing and provenance);
> the track is part of the Banking Edition delivery roadmap
> (`banking/delivery/`). Maturity and evidence: releases, CI, and the
> `кейсы/` cases.

**Training cases**: [`кейсы/`](кейсы/AGENTS.md) — end-to-end samples of the
solution-architect cycle produced with the harness. Case 001:
[`sbp-gateway`](кейсы/sbp-gateway/) (faster-payments C2B gateway; DeepSeek V4
Flash) — spine invariants, solutioning, 7 ADRs, contracts/NFR/RFP, and a
handoff package with a rubric and fitness rules. Case 002:
[`payment-processing-platform`](кейсы/payment-processing-platform/) (bank
processing rails: cards/SBP/SWIFT; GLM-5.2) — 27 spine invariants, 16 ADRs,
fitness constraints targeting Go code, walking-skeleton handoff. Case 003:
[`govproc-platform`](кейсы/govproc-platform/) (government-sector e-commerce:
B2G procurement 44-FZ, EIS integration, qualified e-signature; Kimi K3) —
7 spine invariants, 5 ADRs, NFR, OpenAPI contract, external-system emulator.
Case 004: [`parallel-epics`](кейсы/parallel-epics/) (three parallel Claude
Code executors on isolated worktrees, deepseek-v4-pro backend) — the
AD-1…3 spine glued the module seams with zero cross-visibility: 15/15 tests
green, first-build integration OK; handoff package served over MCP;
run-surfaced defects → same-day harness fixes; color frames in
`screenshots/`. Case 005: [`fleet-of-ten`](кейсы/fleet-of-ten/) (ten
parallel Claude Code executors, ten epics of the bankcalc library) — ~3.2
min wall clock, 10/10 complete, 42/42 tests, 60/60 fitness rules,
first-build integration; every executor committed its own work (the
"Finalization" contract) — the auto-commit safety net never fired; color
frames in `screenshots/`. Case 006: [`drift-control`](кейсы/drift-control/)
(a controlled drift experiment, two arms; Claude Code) — the same
payment-core task bare vs with a handoff package (3 spine invariants + 6
fitness rules); the mechanical gate fails the bare arm 2/6 with exit 1
(no thiserror, no idempotency — drift with all tests green) and passes
the spine arm 6/6 with a real idempotency inbox; both solutions ship in
the case and are re-checkable from the repo with one `arch-ml control check`
command; color frames in `screenshots/`. Case 007:
[`fleet-spine-drift`](кейсы/fleet-spine-drift/) (mechanical, no LLM) — a
three-worktree fleet carrying full spine copies: `arch-ml fleet audit` measures
duplicates (66.7%) and `CONSTRAINTS.yaml` drift (deviant wt-c, exit 1),
`arch-ml delta guard` bans direct spine edits bypassing a delta; reproducible
with the bare `arch-ml` binary.

**Plus**: beautiful Tokyo Night TUI (markdown chat, mermaid→Unicode diagrams —
the side panel auto-widens up to 60% of the screen so renders are never
truncated, mouse, dialog scrollbar with a "▼" jump-to-latest button,
**mouse text selection with auto-copy to the clipboard** — drag across the log
pane, release to copy (native via `arboard`, no external tools needed;
fallbacks: wl-copy/xclip/xsel, OSC 52),
**multi-line input** — newline via Shift+Enter, Alt+Enter or Ctrl+J, the field
grows up to 8 lines, Up/Down move across lines and fall back to history,
**message queue while the agent works** shown as a card in the log pane —
Enter queues, Alt+Enter or a "!!" prefix jumps to the front, **turn
interrupt**: Esc or Alt+Enter during a turn cancels the in-flight LLM request
or tool call (including a `harness_run` wait), so an urgent queued message
starts immediately — the session history stays consistent (pending tool calls
get a "cancelled" result), fullscreen viewer
with horizontal pan, docx/xlsx export, option-picker modals), MCP client,
curated architecture websites + local knowledge base, markdown-task cron,
`arch-ml doctor` diagnostics, **global markdown memory** (`MEMORY.md` in
`~/.arch-ml`, Kimi Code style: a memory section is injected into every
session's system prompt — TUI and `arch-ml run`; the agent appends facts on the
user's request ("remember …") via fs tools, or manually with
`/memory add` / `arch-ml memory add`; path configurable as
`paths.memory_file`).

### Development in numbers: tokens and where architecture paid off

The project was built in 4 days (August 14–17, 2026) by an AI agent (Kimi
K3) steered by a human solution architect. Usage is measured exactly —
the sum of `usage.record` events across three session wire logs (72
agents: the main loop plus the sub-agent swarm):

| Metric | Value |
|---|---|
| LLM requests | 5,628 |
| Output (code, docs, replies) | 4.2M tokens |
| Fresh input (new content) | 15.9M tokens |
| Cache-read (context re-reads per turn) | 986.6M tokens |
| **Total processed** | **≈ 1.007B tokens** |

Not included: the harness's own runs (cases 001–006, executor fleets) on
the DeepSeek/GLM/Kimi K3 APIs — roughly another 5–10M tokens.

Where architectural discipline (the architect's work, not the model's)
paid off:

- **The spine holds against drift — proven by a controlled experiment**
  ([case 006](кейсы/drift-control/)): the same task in two arms — bare →
  drift on org invariants with all tests green (gate FAIL, exit 1), with
  a handoff package → PASS 6/6. The spine costs zero wall time
  (360s vs 372s).
- **Spine as the glue of parallelism** (cases [004](кейсы/parallel-epics/)
  and [005](кейсы/fleet-of-ten/)): 3 and 10 executors with zero
  cross-visibility converged on first-build integration; the
  "Finalization" contract, born from a case-004 defect, yielded 10/10
  self-commits in case 005.
- **Anchored rubrics as a wiring-weakness detector**: a handoff_quality
  score of 2.90/5 on a Critical case pinpointed the gap (the textual JSON
  result contract was not machine-parsed) — fixes went into the handoff
  generator and pre-gates, not into the coding agent.
- **External architecture review → engineering gates**: the review
  produced dogfooding (our own `ARCHITECTURE-SPINE.md` +
  `CONSTRAINTS.yaml` and a dogfood CI job — which already caught a real
  incident: the personal-paths scan stopped a commit carrying build
  junk), clippy `-D warnings` with an explicit allow policy, a migration
  to the maintained YAML crate, and the code-harness execution-safety
  block.

### Quick start

```bash
cargo build --release          # binary: target/release/arch-ml
ln -sf "$PWD/target/release/arch-ml" ~/.local/bin/arch-ml   # one-word launch: `arch-ml`
arch-ml init                      # config + assets into ~/.arch-ml and
                               # ~/.config/arch-ml/config.toml

# API keys via environment only (config stores *names* of the variables):
export DEEPSEEK_API_KEY=...    # deepseek (v4-flash, default), deepseek-pro (v4-pro)
export ZHIPU_API_KEY=...       # glm (glm-5.2 + budget 4.7/air/flash)
export KIMI_API_KEY=...        # kimi (k3, coding surface) or file ~/.kimi_api_key

arch-ml                           # interactive TUI (one word)
arch-ml run -q "draft an ADR for saga adoption" > adr.md   # strict headless
arch-ml run -q --timeout 600 --max-turns 24 "…" > adr.md   # with budgets for CI/cron
arch-ml models                    # list configured models
```

No-LLM smoke: `arch-ml mermaid examples/mermaid/flow.mmd`,
`arch-ml control score --trigger new_component=true`, `arch-ml doctor`.

### Configuring personal paths

All machine-specific wiring lives in the config file — the code ships only
neutral placeholders. Config lookup order: `--config <path>` →
`./arch-ml.toml` → `~/.config/arch-ml/config.toml` → built-in
defaults. Every section is optional — override only what differs.

```toml
# ~/.config/arch-ml/config.toml
[knowledge]
dirs = ["~/Documents/architecture", "~/library"]     # your knowledge base (kb_search)

[plugins]
dirs = ["~/.arch-ml/plugins", "~/my-plugins"]   # your plugin libraries

[paths]                          # where the harness writes its data
assets_dir = "~/.arch-ml/assets"     # prompts, rubrics, benchmarks
reports_dir = "~/.arch-ml/reports"   # rubric/cron/subagent reports
sessions_dir = "~/.arch-ml/sessions" # session journals
state_dir = "~/.arch-ml/state"       # state (failure memory etc.)
```

- `~/` is expanded automatically; relative paths resolve from the cwd.
- API keys: `api_key_env` (name of an env variable) or `api_key_file` (path to
  a key file, e.g. `~/.kimi_api_key`) — key values never go into the config.
- Verify after editing: `arch-ml doctor` (dirs and keys) and `arch-ml kb "query"`.

Fully commented sample: `config.example.toml`.

### Documentation

- `docs/architecture.md` — system design, contracts, how to extend.
- `AGENTS.md` — guide for agents and contributors (install, module map, conventions); `AGENTS-READERS.md` — for reading agents (5-minute idea map, key-free safe exploration).
- `docs/tools.md` — full tool reference (core vs architecture map, 30+ tools with parameters).
- `docs/slash_commands.md`, `docs/models.md`, `docs/plugins_and_skills.md`,
  `docs/rubrics_and_benchmarks.md`, `docs/harness_integrations.md`,
  `docs/handoff_walkthrough.md` (handing context to a coding harness,
  frame by frame), `docs/governance.md`, `docs/mcp.md`, `docs/cron_and_md_pipes.md`,
  `docs/web_kb.md`, `docs/agents_md.md`, `docs/failure_memory.md`,
  `docs/hypotheses.md` (hypothesis cards, project-fact routing, ADR-047),
  `docs/SOURCE_BRIEF.md` (idea sources).
  The detailed docs are mostly in Russian — the code and CLI speak English.

Configuration: `config.example.toml`, `cron.example.toml` — fully commented.
Tests: `cargo test` (incl. CLI integration tests in `tests/cli.rs` via `assert_cmd`; live-LLM tests are `#[ignore]`d).
CI: fmt / clippy / test / MSRV 1.85 / cargo audit — `.github/workflows/ci.yml`.

### License

MIT — see [LICENSE](LICENSE).
