# Архитектура arch-harness

Устройство харнесса: карта модулей, замороженные контракты, поток данных
агентного цикла, акторная модель TUI, точки расширения.

Обзорная диаграмма продукта (поверхности → ядро → контуры контроля и
диаграмм → банковская зона): [diagrams/spine-be-architecture.html](diagrams/spine-be-architecture.html)
(интерактивная; превью-кадр — [diagrams/spine-be-architecture.png](diagrams/spine-be-architecture.png),
IR — [diagrams/spine-be-architecture.architecture.json](diagrams/spine-be-architecture.architecture.json)).
Догфуд: IR авторствован самим Spine-BE (headless-прогон `arch-ml run`),
рендер и приёмка — встроенным Archify (9/9 checks, composition showcase 0/0).

Принципы: тонкий харнесс (никакой магии поверх OpenAI-совместимого
function calling), ошибки инструментов — данные для модели, а не паника;
секреты только из окружения; `unsafe` запрещён (`Cargo.toml`:
`unsafe_code = "forbid"`); ошибки — `HarnessError`/`Result` (thiserror,
`src/error.rs`), прикладной слой — anyhow с `.context()`.

## Карта модулей `src/`

| Модуль | Назначение |
|---|---|
| `main.rs` | Тонкая точка входа: clap-парсинг → вызов lib → код возврата. Все CLI-подкоманды. |
| `lib.rs` | Корень библиотеки `arch_harness`; реэкспорт `Config`, `HarnessError`, `Result`. |
| `config.rs` | `Config` и секции (`ModelConfig`, `AgentConfig`, `KnowledgeConfig`, `WebConfig`, `CodingHarnessConfig`, `McpSettings`, `CronSettings`, `PathsConfig`). Порядок поиска: `--config` → `./arch-ml.toml` → `~/.config/arch-ml/config.toml` → дефолты. |
| `error.rs` | `HarnessError` (thiserror): доменные варианты Config/Llm/Tool/Agent/Rubric/Bench/Control/Harness/Mcp/Web/Kb/Cron/Tui/Io и пр. |
| `llm.rs` | Контракт провайдеров: трейт `LlmProvider`, `LlmRegistry`, типы `ChatMessage`, `ToolCall`, `ToolSpec`, `ChatRequest`, `Usage`, `LlmEvent`. |
| `llm/openai_compat.rs` | Общий OpenAI-совместимый клиент `/chat/completions`: SSE-стриминг, инкрементальная сборка `tool_calls`, один retry на 429/5xx; `generic_provider` для произвольных endpoint'ов. |
| `llm/{deepseek,kimi,glm}.rs` | Тонкие фабрики-пресеты над `openai_compat`. |
| `tool.rs` | Контракт инструментов: трейт `Tool`, `ToolRegistry`, `ToolContext`, `ToolOutput`. |
| `tools.rs`, `tools/{bash,fs}.rs` | Ядро: `bash` (таймаут, лимит вывода, env-scrub, опциональный sandbox bubblewrap — `[bash] sandbox`, ADR-038), `read_file`, `write_file`, `edit_file`, `glob`, `grep` (вывод читающих инструментов проходит warn-детектор prompt-инъекций — `src/injection.rs`, ADR-038); `core_registry()` / `full_registry()`. |
| `agent.rs` | Агентный цикл `AgentSession`, события `AgentEvent`, JSONL-журнал сессии. |
| `agent/slash.rs` | Слэш-команды TUI (`docs/slash_commands.md`). |
| `agent/prompts.rs` | Библиотека промптов `assets/prompts/*.md`; плейсхолдеры `{{var}}`. |
| `memory.rs` | Глобальная md-память (`paths.memory_file`, дефолт `~/.arch-ml/MEMORY.md`): загрузка, дописка заметок, секция в системном промпте. |
| `mermaid.rs`, `mermaid/{parse,model,layout,draw}.rs` | Подмножество mermaid (flowchart, sequenceDiagram, erDiagram, C4Context/C4Container/C4Component) → символьная сетка; ER/C4 понижаются к flowchart-AST (многострочные метки узлов); layered layout (Sugiyama-lite). |
| `rubric.rs` | Движок рубрик: якорные/динамические, LLM-судья (k сэмплов → медиана, метки `unstable`/`evidence_not_found`, механическая проверка цитат, лимит длины — явная ошибка, изоляция текста — ADR-004), `RubricReport`. |
| `bench.rs` | Бенчмарки: YAML-сценарий → ответ модели → оценка рубрикой → отчёты md+json; golden-set судьи (`assets/benchmarks/golden/`, `run_golden`, метрика MAE, гейт по `judge.golden_max_mae`). |
| `web.rs` | DuckDuckGo-поиск, site:-ограничение по кураторским сайтам, fetch html→text. |
| `kb.rs` | Локальная база знаний: walkdir + кэш корпуса (mtime+len, LRU 256 МБ) + BM25-скоринг (IDF, словоформы, фразы, md-заголовки, триграммный фолбэк) + сниппеты с breadcrumb. |
| `mcp.rs` | MCP-клиент: stdio NDJSON JSON-RPC, `McpManager`, `McpToolAdapter`. |
| `mcp_server.rs` | MCP-сервер (ADR-008): stdio NDJSON JSON-RPC, `arch-ml mcp serve`; 10 read-only инструментов (6 контроля + 4 чтения знаний — kb_search, skill_search, skill_load, mermaid_render, T4); -32700/-32601/-32602/isError, rubric_run без ключа → -32603. |
| `harness.rs` | Handoff-пакеты `.arch-handoff/` и запуск кодовых харнессов; `[fleet] require_worktree` — enforced-изоляция прогона в git worktree (`arch/<run-id>`), интеграция — гейтом владельца `arch-ml fleet merge` (`docs/fleet.md`). |
| `control.rs` | Архитектурный контроль: score, линтер spine, сенсоры, fitness, ADR. Fitness-правила — 9 типов, включая структурные `dependency_direction` (направление зависимостей/слои, ADR-029) и `context_boundary` (границы контекстов по `code_roots` CMP, ADR-030). |
| `rehearsal.rs` | Гейт A4 rollback-first: машиночитаемый план отката `ROLLBACK.yaml` в пакете, репетиция шагов во временном git-worktree на baseline_commit (denylist деструктивных/внешних шагов, fail-fast), evidence `REHEARSAL.json`; CLI `arch control gate A4 <repo> [--rehearse]`, для Critical репетиция обязательна (`--require-rehearsal`). |
| `model.rs`, `model/{parse,graph,validate,project,exchange}.rs` | Типизированная модель архитектуры (ADR-003): сущности model/*.md (frontmatter + проза; в т.ч. `QAS-*`, количественные поля ADR-007, `code_roots` CMP — ADR-030 и `contract` INT — ADR-035), валидация ссылок, граф (text/mermaid), проекция ADR в .arch-handoff/adr/, обмен с отраслевыми форматами (exchange: экспорт Structurizr DSL/PlantUML/drawio, импорт Structurizr DSL, round-trip — ADR-009; экспорт ArchiMate Open Exchange 3.2, только экспорт — ADR-032); инструмент model_query. |
| `adr_registry.rs` | Глобальный реестр ADR (ADR-036): `arch-ml adr registry <ROOT> [--json] [--strict]` — агрегация прозаических docs/adr/*.md и типизированных model/ADR-*.md по ROOT + непосредственным подкаталогам-проектам; индекс (проект | ADR | заголовок | статус | дата | источник) + находки (коллизия номеров, дубль заголовка, пропуск даты/статуса); read-only, файловая механика (ADR-033). |
| `landscape.rs` | Ландшафт систем (EA-3, ADR-037): `arch-ml model landscape <ROOT> [--mermaid]` — агрегация `model/` ROOT + непосредственных подкаталогов в реестр систем SYS/INT с дедупликацией по нормализованному имени (БЕЗ глобальных ID); находки id-divergence/status-conflict/dangling-ref/cross-project-link, топ-5 связности, mermaid `graph TD`; read-only (ADR-033, та же механика набора проектов, что ADR-036). |
| `publish.rs` | Файловые адаптеры публикации (ADR-033): `arch-ml publish confluence <file.md>` — markdown → Confluence storage format (заголовки, таблицы, код-блоки CDATA, списки с переносами, инлайн-подмножество); `arch-ml publish jira <result.json> [--project KEY]` — open_questions/conflicts/assumptions handoff-результата → Jira-CSV импорта. Живых коннекторов нет — git и файлы как транспорт. |
| `trace.rs` | Трассируемость как fitness-функция (ADR-006): позвенное покрытие REQ → NFR → AD/ADR → CMP → правило CONSTRAINTS.yaml, звено INT → контракт (поле `contract`: битый путь — error, нет поля — warn; ADR-035), сверка модели со spine, отчёт markdown для evidence bundle; инструмент trace_check. |
| `nfr.rs` | Количественные NFR (ADR-007): `arch-ml nfr budget` (сумма latency-бюджетов hop'ов INT-* против p99-цели NFR), `availability` (∏Aᵢ последовательно, 1−(1−A)ⁿ по replicas, сверка с SLA, цели RTO/RPO), `capacity` (instances × rps_per_instance против rps_target), `cost` (TCO и цена выхода из тарифов сущностей); error → exit 1. |
| `cron.rs` | Планировщик md-задач: `cron.toml`, дюжность, прогон, отчёт. |
| `goal.rs` | Goal-режим — контроль-петля автопродолжения (`aiml/notes/goal-mode.md`): цель с механической проверкой критерия (`objective \|\| criterion \|\| check`), состояния active/paused/blocked/complete, сборка синтетического `user`-continuation (цель как untrusted-данные в `<system-reminder>`). IO нет: переходы и текст — чистые функции, проверку критерия выполняет `agent`. |
| `tui.rs`, `tui/{app,render,text,theme}.rs` | TUI: event loop, состояние, отрисовка, markdown-lite, палитра Tokyo Night. |
| `assets.rs` | Встроенные ассеты (`include_str!` из `assets/`, `examples/`); `write_defaults` для `arch-ml init` (не затирает существующие файлы). |

Каждый доменный модуль экспортирует `tools() -> Vec<Arc<dyn Tool>>`;
`tools::full_registry()` собирает ядро + домены (mermaid, rubric, web, kb,
control, harness, model, trace).

CLI-контракт гейта A4: `arch control gate A4 <repo|пакет> [--rehearse]
[--require-rehearsal fast|standard|critical|never]` — репетиция отката
handoff-пакета во временном worktree на baseline_commit, evidence
`.arch-handoff/REHEARSAL.json`; FAIL → exit 1 (детали — `docs/control.md`).

CI: `.github/workflows/ci.yml` (fmt / clippy / test / MSRV 1.85 / cargo audit);
CLI-контракт (exit-коды, `arch-ml init`, `control check`, `harness-run`) покрыт
интеграционными тестами `tests/cli.rs` на `assert_cmd` (ADR-005).

## Контракты

### `Tool` (`src/tool.rs`)

```rust
#[async_trait]
pub trait Tool: Send + Sync {
    fn spec(&self) -> ToolSpec;                              // имя, описание, JSON Schema
    async fn call(&self, args: Value, ctx: &ToolContext) -> Result<ToolOutput>;
}
```

- `ToolOutput { content, is_error }` — `ok()` / `err()`; `truncated(max)`
  усекает с пометкой.
- `ToolRegistry::dispatch` превращает ошибку инструмента в
  `ToolOutput::err(...)` — агентный цикл не рвётся на сбое одного вызова.
- `ToolContext { cwd, config, llm }` — рабочий каталог (все относительные
  пути инструментов резолвятся от него), конфиг, опциональный реестр LLM
  (нужен рубрикам и динамическим проверкам).

### `LlmProvider` (`src/llm.rs`)

```rust
#[async_trait]
pub trait LlmProvider: Send + Sync + fmt::Debug {
    fn name(&self) -> &str;
    fn model(&self) -> &str;
    async fn complete(&self, req: ChatRequest) -> Result<ChatMessage>;
    async fn stream(&self, req: ChatRequest, tx: mpsc::Sender<LlmEvent>)
        -> Result<ChatMessage>;  // дефолт — обёртка над complete
}
```

- `LlmRegistry::from_config` строит провайдеров из `[models]`: имена
  `deepseek*`/`kimi*`/`glm*` → фабрики модулей, любое другое имя →
  `openai_compat::generic_provider`. `default_model` обязан присутствовать.
- API-ключ читается лениво из `ModelConfig::api_key_env` на запросе:
  отсутствие ключа одного провайдера не роняет реестр. Ключ не выводится
  в `Debug` и не логируется.

## Поток данных агентного цикла (`src/agent.rs`)

`AgentSession::send(input, events)`:

1. `input` пушится в историю и в журнал (`user`).
2. До `agent.max_tool_turns` итераций (дефолт 4800):
   - `compact_history()` — при превышении ЭФФЕКТИВНОГО бюджета
     `min(agent.context_budget_tokens, models.<активная>.context_limit)`
     (дефолт конфиг-бюджета 6 000 000; окно модели — напр. 1 000 000 у
     deepseek и kimi, 204 800 у glm; оценка 4 символа ≈ 1 токен): сначала усечение старых
     tool-сообщений до 500 символов (кроме 4 последних), затем удаление
     старейших сообщений (хвост из 6 не трогается); на ~95% окна — L3
     LLM-саммари; факт пишется в журнал. Принудительно — `/compact` в TUI
     (вне порогов, событие `compact_manual`).
   - `build_request()` — системный промпт + история + `tools.specs()`;
     `temperature`/`max_tokens` из `ModelConfig` активного провайдера;
     `thinking` — из сессионного переключателя `/think`.
     Изображения уходят только у последних `[agent].images_keep_last`
     сообщений (дефолт 1); у остальных кадр снимается с пометкой
     `[изображение не передано повторно…]`, чтобы модель не искала
     несуществующее изображение. Второй рубеж — байтовый бюджет
     `MAX_REQUEST_IMAGE_BYTES` (32 MiB при лимите тела 48 MiB у DeepSeek).
     Ретеншен применяется и к запросу, и к истории (память): факт снятия
     пишется в журнал событием `images_stripped` (ADR-041).
     Системный промпт собирается при старте сессии (TUI и `arch-ml run`) из
     шаблона `architect` библиотеки промптов и дополняется глобальной
     md-памятью: секция `memory::augment_system_prompt` инжектится всегда —
     содержимое `paths.memory_file` (если файл есть) + правило «дописывать
     факт в файл по явной просьбе пользователя»; ошибка чтения файла не
     фатальна — инжектится правило с пустой памятью.
   - Запрос: стриминг (`stream` + канал событий) или `complete`.
   - Ответ без `tool_calls` → пуш в историю/журнал; далее goal-шаг (ниже);
     если цель не требует продолжения — `AgentEvent::TurnDone` и возврат текста.
   - Иначе для каждого вызова: `AgentEvent::ToolStart` →
     `ToolRegistry::dispatch` с таймаутом 300 с → результат усекается до
     8192 символов → `ChatMessage::tool_result` в историю, `AgentEvent::ToolEnd`.
   - На каждый `ToolOutput::err` — память сбоев (`failure_memory`):
     подпись «инструмент + нормализованный класс ошибки» (пути/имена файлов
     схлопываются), счётчики персистируются в `paths.state_dir`; повтор
     подписи (второй случай, ровно один раз) порождает урок — заметка в
     транскрипт + `state/failure_lessons.md`, а в режиме `[agent]
     failure_memory = "write"` — append в AGENTS.md рабочего каталога
     (вне зоны ARCH:GENERATED). Подробности: `docs/failure_memory.md`.
3. Лимит исчерпан → ход НЕ падает: финальный запрос с пустым `tools`
   и служебной репликой (только в запрос) «дай ответ по собранному»;
   заметка в UI, событие `tool_turn_limit` в журнале.

### Goal-шаг (контроль-петля автопродолжения, `src/goal.rs`)

Если у сессии есть активная цель (`/goal …` в TUI, `run --goal` в headless),
на КАЖДОМ терминальном ответе модели ход не заканчивается:

1. Механическая проверка критерия: команда `spec.check` исполняется через
   штатный `ToolRegistry::dispatch` (policy R0-R5 применяется; отказ политики,
   таймаут и сигнал = «критерий не подтверждён») → `Goal::decide`.
2. `complete` — только при exit 0; самоотчёт модели «готово» цель не
   закрывает. `blocked` — после `[goal].blocker_repeats` повторов одного
   блокера; `paused` — бюджет токенов, лимит тернов, серия пустых тернов,
   отчёт модели, ошибка хода/Esc (автопауза, спека §8).
3. Иначе цель активна → в историю пушится синтетическое `user`-сообщение
   (continuation): протокол малых шагов + цель как untrusted-данные внутри
   `<system-reminder>` + статистика (терн, токены, время, бюджет). Цикл
   продолжается тем же ходом — цель снова в хвосте контекста (recency) и
   переживает компактизацию.
4. Каждый переход — запись `{"event":"goal"}` в журнал (AD-5); последняя
   запись восстанавливает цель при `/resume`, причём в состоянии `paused`
   (перезапуск — автопауза). Headless-коды: 0/3/6 (`docs/headless.md`).

Отмена хода (TUI ставит `CancellationToken` через `set_cancel_token` на
каждый ход): проверка между итерациями + гонка `race_cancel` против
LLM-запроса и каждого `dispatch`. Отмена во время вызова инструмента
дорасылает результат «прервано» текущему и незапущенным вызовам пачки
(контракт tool-пар не нарушается); в журнал — `turn_cancelled`, в UI —
заметка, возврат пустого текста. Дропнутый future `harness_run` убивает
дочерний процесс кодового харнесса (`kill_on_drop`).

Журнал: append-only JSONL `sessions/session-<yyyymmdd-hhmmss>.jsonl`,
события `system`/`user`/`assistant`/`tool`/`event`/`usage` с ISO-метками; каждая
запись флашится (журнал переживает падение). Запись `usage` — реальные токены
ответа LLM (`stream_options.include_usage`): модель, prompt, completion;
пишется после каждого assistant-сообщения стрим-ответа, провайдер usage не
отдал — записи нет (старые журналы без `usage` читаются, метрики таких сессий
работают на оценке chars/4). Недоступность журнала — не
фатальна (warn в tracing).

Headless (`arch-ml run`): бюджеты прогона — `--timeout SECS` (общий потолок,
превышение — причина в stderr и exit 1) и `--max-turns N` (лимит итераций
инструментов, перекрывает `agent.max_tool_turns` на клоне конфига). В
стрим-режиме stdout несёт только текст ответа, прогресс инструментов и
заметки уходят в stderr — пайп остаётся чистым (контракт `-q` не меняется).

## Акторная модель TUI (`src/tui.rs`, `src/tui/app.rs`)

- `App` владеет всем состоянием в event loop — **без `Arc<Mutex>`**.
- Ход агента и слэш-команды выполняются в `tokio::spawn`; сессия
  (`AgentSession` — не `Send`-ресурс в UI) возвращается владельцу сообщением
  `AppMessage` по bounded-каналу `mpsc(256)` (backpressure).
- Дельты/статусы инструментов форвардятся как `AgentEvent` по `mpsc(64)`.
- Цикл (`tui::run`): отрисовка кадра → `tokio::select!` по crossterm
  `EventStream` (клавиши), `AppMessage`, `Ctrl-C`, тикер спиннера 120 мс.
  На `Event::Resize` — `terminal.clear()`: терминал рефлоуит содержимое
  сам, без полной очистки кадровый буфер расходится с экраном и остаются
  «призрачные» артефакты.
- Терминал: RAII-гард `TerminalGuard` (raw mode + alternate screen, Drop
  восстанавливает) + panic hook, восстанавливающий терминал перед печатью
  паники. Выход: `q` / `Ctrl-C` / `Esc` (в простое).
- Правая панель (вкладки `◇ Mermaid`/`✓ Рубрика`/`◈ Знания`): вывод
  инструментов и слэш-команд раскладывается по вкладкам сам; F1–F3 —
  переключение, F4 — полноэкранный просмотрщик (панорама для широкого
  арта), F5 — скрыть/показать панель целиком.
- Диалог: блоки `User`/`Assistant`/`Thinking`/`Tool`/`System`/`Error`.
  `reasoning_content` («мысли» модели) стримится дельтами
  (`AgentEvent::ReasoningDelta`) в компактный приглушённый блок `Thinking`
  (кап `MAX_THINKING_LINES` строк + счётчик хвоста); на не-стрим путях агент
  эмитит ризонинг целиком после ответа. В заголовке поля ввода во время
  хода — «дышащая» точка (`PULSE`, тикер 120 мс; спокойная замена змейке).
- Очередь ввода во время хода: `Enter` — в конец (FIFO), `Alt+Enter` или
  префикс `!!` — в начало; `Esc`/`Alt+Enter` во время хода отменяют его
  через `CancellationToken` — срочное сообщение стартует немедленно после
  возврата сессии (`maybe_start_queued` на `TurnFinished`).
- Экраны: `Chat` — стартовый и единственный рабочий (логотип из
  `assets::BANNER` — первый блок лога); фатальная ошибка инициализации →
  экран `Fatal`.

## Как добавить свой инструмент

1. Реализуйте `Tool` (см. образцы: `src/tools/fs.rs` — файловые,
   `src/kb.rs::KbSearchTool` — доменный):
   - `spec()`: `ToolSpec { name: "my_tool" (snake_case), description
     (для модели: что делает и когда вызывать), parameters: json!({...JSON Schema}) }`;
   - `call()`: разбор аргументов, работа, `Ok(ToolOutput::ok(...))`;
     ожидаемые сбои — `ToolOutput::err(...)` (модель увидит и скорректирует).
   - Пути резолвите через `ctx.resolve(path)`, вывод усекайте
     `ToolOutput::truncated`.
2. Зарегистрируйте: своя доменная функция `tools()` + строка в
   `tools::domain_tools()` (`src/tools.rs`), либо в `core_registry()`,
   если инструмент ядерный.
3. Проверка: `/tools` в TUI, `arch-ml run "…"` с задачей, требующей вызова.
   Тесты — по образцу `agent.rs::tests::EchoTool`.

## Как добавить свой LLM-провайдер

Провайдеры — OpenAI-совместимые endpoint'ы; в большинстве случаев код не
нужен: добавьте секцию в `config.toml`:

```toml
[models.my-llm]
base_url = "https://llm.example.com/v1"
model = "my-model"
api_key_env = "MY_LLM_API_KEY"
```

Имя не из пресетов → `openai_compat::generic_provider` (`src/llm.rs::build`).
Особый транспорт: фабрика по образцу `src/llm/deepseek.rs` + ветка в
`LlmRegistry::build`. Подробности и сетевые замечания — `docs/models.md`.
