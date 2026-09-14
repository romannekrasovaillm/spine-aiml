# Плагины и библиотека скиллов

> **Единица установки — всегда ПЛАГИН, не скилл.** Скилл не устанавливается
> отдельно: он приезжает внутри плагина (каталог `skills/<имя>/SKILL.md`).
> «Установить скилл» = скопировать каталог плагина в одну из директорий
> `[plugins] dirs`. Команды `arch-ml skills …`/`/skills` — лишь плоский ИНДЕКС
> скиллов, собранный со всех плагинов: другой вид на те же плагины, а не
> отдельный реестр. «Библиотека скиллов» в быту = каталог каталогов-плагинов.
> Новые скиллы от `/distill` пишутся в плагин `arch-distilled` (managed-зона).

Харнесс поддерживает открытый стандарт [agent-plugins.org](https://agent-plugins.org/) —
переносимый пакет компонентов для агентов: **скиллы + MCP-серверы** в одном каталоге,
плюс клиентские расширения (субагенты, хуки).

## Библиотека плагинов: что это и зачем

**Библиотека плагинов** — это набор каталогов-плагинов в директориях
`[plugins] dirs` (по умолчанию `~/.arch-ml/plugins`). Она решает три
задачи доменного харнесса:

1. **Методики под рукой.** Скиллы — это дистиллированные методики
   архитектора (как писать ADR, как маршрутизировать по значимости, как
   считать NFR). Агент находит их сам по запросу (`skill_search`) и
   подгружает в контекст (`skill_load`), не раздувая системный промпт.
2. **Расширение без перекомпиляции.** Новый плагин = скопировать каталог в
   библиотеку — скиллы, MCP-серверы, субагенты и хуки подхватываются на
   старте сессии. Свой плагин собирается за 5 минут (рецепт ниже).
3. **Накопление знаний.** `/distill` и `skill_distill` превращают статьи и
   транскрипты сессий в новые скиллы managed-зоны `arch-distilled` —
   библиотека растёт от практики, а не только от поставщика.

Стандарт [agent-plugins.org](https://agent-plugins.org/) делает плагины
переносимыми между агентными харнессами: один каталог с манифестом читается
и Spine, и другими совместимыми клиентами.

## Стандарт: layout плагина

```
my-plugin/
├── plugin.json            # манифест: $schema, name, version, description, keywords, hooks (доменные)
├── skills/<name>/
│   ├── SKILL.md           # frontmatter (name, description) + методика
│   └── references/        # шаблоны, примеры (подгружаются по требованию)
├── mcp.json               # MCP-серверы (стандарт; поддерживается и .mcp.json)
├── agents/<name>.md       # субагенты (клиентское расширение)
└── hooks/hooks.json       # хуки событий (клиентское расширение)
```

Харнесс исполняет скиллы, MCP, субагентов и **хуки** плагинов. Хуки плагинов
(`hooks/hooks.json`, формат Claude Code) конвертируются в спеки `[hooks]`
(`hooks::specs_from_plugin_json`: `matcher` с `|`-альтернативами разворачивается
в отдельные спеки, наш фильтр — подстрока имени инструмента) и сливаются с
хуками конфига при старте сессии; выключается `[plugins] include_hooks = false`.
Включайте исполнение только для доверенных библиотек — хуки исполняют
shell-команды плагинов.

Дополнительно манифест поддерживает карту **доменных хуков** `hooks`
(`{"<событие>": "<команда с аргументами>"}`): агентский цикл их не
исполняет — резолвят и запускают доменные механизмы
(`hypothesis::domain_hooks` / `run_event`, ADR-047; пример —
роутинг гипотез `aiml/plugins/hypothesis-router`, `docs/hypotheses.md`).
Первый токен команды резолвится от каталога плагина, несуществующий файл
пропускается без ошибки, ненулевой код выхода и таймаут не роняют вызвавшую
операцию (исход едет в outcome-запись).

Субагент (`agents/<name>.md`: frontmatter `name`/`description`/`tools` + тело =
системный промпт) запускается в фоне инструментом `subagent_run` — свежий
контекст, whitelist инструментов из `tools` (пустой — все), отчёт забирается
`subagent_result`; статусы — `subagent_list` или слэш `/agents`. Отчёты
дублируются в `~/.arch-ml/reports/subagents/<id>.md`. Лимит — 4
параллельных задачи; `subagent_*`-инструменты субагенту не выдаются
(антирекурсия). Субагент наследует активную модель чата (или модель
по умолчанию в headless).

## Конфигурация

```toml
[plugins]
dirs = [
    "~/.arch-ml/plugins",                      # локальная библиотека (arch-ml init)
    # "~/knowledge/skills",                          # ваша общая библиотека плагинов
]
include_mcp = true   # MCP из плагинов → общий пул, имена `plugin.server`
```

- Каждый прямой потомок каталога — плагин. Нет `plugin.json`, но есть `skills/` —
  манифест синтезируется из имени каталога.
- Битый плагин/скилл пропускается с предупреждением — библиотека не падает целиком.
- Дубликаты имён скиллов: первый по порядку `dirs` wins.
- Имена MCP-серверов из плагинов — `plugin.server` (точка; `__` зарезервировано
  разделителем `server__tool`).

## Встроенные плагины (раскладываются `arch-ml init`)

- **arch-core** (15 скиллов) — ядро методов solution-архитектора: `adr-authoring`,
  `spine-invariants`, `significance-routing`, `c4-mermaid`, `nfr-design`,
  `readiness-gate`, `adversarial-review`, `handoff-packaging`, `reverse-discovery`,
  `delta-spec`, `fitness-functions`, `rubric-judging`, `skill-authoring` (мета-скилл
  создания скиллов), `agents-md-authoring` (AGENTS.md для команд), `dsh-harness-patterns`. Субагенты: `repo-scout`, `adr-reviewer`,
  `nfr-auditor`. Хуки: блок force-push в bash (PreToolUse, exit 2), напоминание о
  spine/ADR после правок (PostToolUse), заметка SessionStart. MCP: `arch-memory`.
- **patterns-integration** (5 скиллов) — дистилляты pattern language Криса Ричардсона
  ([microservices.io](https://microservices.io/patterns/)): `saga-transactions`,
  `transactional-outbox`, `cqrs-api-composition`, `strangler-acl`, `idempotent-consumer`;
  MCP `pattern-web` (fetch), субагент `pattern-selector`, хук-напоминание.
- **patterns-resilience** (5 скиллов) — дистилляты [Azure Cloud Design Patterns](https://learn.microsoft.com/azure/architecture/patterns/):
  `circuit-breaker-retry`, `bulkhead`, `queue-load-leveling`, `cache-aside`,
  `rate-limiting-throttling`; MCP `pattern-web`, субагент `resilience-auditor`, хук.
- **aws-builders-library** (9 скиллов) — дистилляты [Amazon Builders' Library](https://aws.amazon.com/builders-library/)
  (через Wayback Machine — aws.amazon.com блокируется DPI): `timeouts-backoff-jitter`,
  `avoiding-fallback`, `control-data-plane`, `load-shedding`, `static-stability`,
  `leader-election`, `eight-failure-modes`, `queue-backlogs`, `fairness-admission-control`;
  `references/catalog.md` — аннотированный каталог всех ~35 статей; субагент
  `distributed-systems-reviewer`.
- **aws-agentic-ai** (5 скиллов) — дистилляты AWS Prescriptive Guidance
  «[Agentic AI patterns and workflows on AWS](https://docs.aws.amazon.com/prescriptive-guidance/latest/agentic-ai-patterns/introduction.html)» (2026, PDF 85 стр.):
  `agent-patterns-overview` (таксономия 10 паттернов), `llm-workflow-patterns`,
  `saga-orchestration-agents`, `multi-agent-collaboration`, `reflect-refine-loops`;
  MCP `aws-docs` + `pattern-web`, субагент `agentic-architect`.
- **arch-office** (12 скиллов) — офисные артефакты архитектора с генераторами
  (`references/*_gen.py`: python-docx / python-pptx / openpyxl, корпоративные
  стили, чек-листы приёмки). docx: `docx-research-report` (аналитический отчёт
  для МД, домовый стиль серии отчётов), `docx-solution-design` (SAD), `docx-architecture-vision`
  (концепция для правления), `docx-integration-spec` (контракт стыка),
  `docx-current-state-assessment` (аудит), `docx-migration-roadmap` (волны);
  pptx: `pptx-board-deck` (правление), `pptx-architecture-review` (архкомитет);
  xlsx: `xlsx-system-catalog`, `xlsx-integration-matrix` (+ сводка формулами),
  `xlsx-decision-matrix` (взвешенная оценка, чувствительность),
  `xlsx-risk-register` (карта 5×5). Субагент `report-proofreader` (корректура
  отчётов для МД). MCP: `arch-time`.
- **arch-governance** (3 скилла) — управление библиотекой архитектурных правил
  (по документу «Источники solution-архитектуры, отработанные для Spine»,
  расширенное издание): `fitness-function-catalog` (20 готовых fitness-функций
  для CONSTRAINTS.yaml с реальными regex, три волны внедрения, карточка
  дистилляции из 7 полей), `rule-library-antipatterns` (8 антипаттернов
  расширения + фильтр включения из пяти форм правила),
  `architecture-sources-map` (навигация по 15 блокам источников: что встроено,
  что кандидат).
- **spine-be-docs** (1 скилл) — справка по самому продукту:
  `check-spine-be-docs` отвечает на вопросы о Spine-BE / arch-ml (команды,
  инструменты, конфигурация, модели, контур контроля, SDK, ADR) строго по
  документации репозитория (`docs/README.md` — карта входа), а не по памяти.

## Команды

| Где | Команда | Что делает |
|---|---|---|
| CLI | `arch-ml skills list` | все скиллы библиотеки (имя, плагин, описание) |
| CLI | `arch-ml skills search <q> [--limit N]` | поиск по скиллам (name ×12, description ×6, keywords ×4, тело ×1) |
| CLI | `arch-ml skills show <name>` | полный текст SKILL.md + список references/ |
| CLI | `arch-ml plugins list` / `arch-ml plugins show <name>` | плагины: состав (скиллы, MCP, agents, hooks) |
| TUI | `/skills [query]` | список / поиск |
| TUI | `/skill <name>` | загрузить скилл **в контекст сессии** (модель дальше следует методике) |
| TUI | `/plugins [name]` | список / подробности |
| LLM | инструменты `skill_search`, `skill_load`, `plugin_list` | модель сама ищет и подгружает скиллы в агентном цикле |

## Поиск: как работает

Скор = совпадения терминов запроса (>2 символов) в `name` (×12), `description` (×6),
`keywords` плагина (×4) и теле SKILL.md (×1, кап 30; файлы >1 МБ — только метаданные).
Сниппет — первое вхождение в теле ±1 строка с маркером `>>>`.
Frontmatter разбирается построчно и толерантен к `:` внутри description и folded-формам
(`>-`) — serde_yaml на таких падает.

## Свой плагин за 5 минут

1. `mkdir -p ~/.arch-ml/plugins/my-pack/skills/my-skill`
2. `plugin.json`: `{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "name": "my-pack", "version": "1.0.0", "description": "…", "keywords": [...]}`
3. `skills/my-skill/SKILL.md` по методике скилла **`skill-authoring`** (frontmatter
   с формулой «Используй этот навык ВСЕГДА, когда…», тело 40–90 строк, шаблоны в references/).
4. Проверка: `arch-ml skills list | grep my-skill`, `arch-ml skills search <типичный запрос>`
   (скилл должен быть в топ-3 — иначе усиль description).
5. Опционально: `mcp.json` (серверы плагина), `agents/`, `hooks/hooks.json`.

## Сессии: восстановление

Каждая сессия агента пишется append-only в `~/.arch-ml/sessions/session-<ts>.jsonl`.

- `/sessions` — список журналов (дата, число сообщений, первая реплика).
- `/resume last` — продолжить новейшую прошлую сессию; `/resume session-20260814-153000`
  — конкретную (суффикс `.jsonl` можно опустить).

Восстанавливаются реплики user/assistant; вызовы инструментов прошлой сессии в контекст
не переносятся (orphan tool_calls недопустимы для API), но остаются в журнале для аудита.
