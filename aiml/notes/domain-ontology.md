# Онтология ML-домена для Spine AI/ML Edition

- Статус: дизайн-док (не код). Целевой потребитель — адаптация
  `concept_search` / `significance_score` / `src/model` в форке `arch-ml`.
- Дата: 2026-09-11. Ревизия: черновик для домена `aiml/`.

## 0. Зачем это

Spine несёт **онтологию архитектуры ПО**: системы, компоненты, интеграции,
NFR, решения (ADR), риски. Доменный слой AI/ML говорит на другом языке:
модель, датасет, чекпоинт, стадия пост-тренинга, верификатор, шов прививки.
Этот документ фиксирует **соответствие словарей**, чтобы адаптировать три
механизма ядра под ML, не ломая инвариант AD-1 («тонкое ядро `src/` не
зависит от `aiml/`», подтверждён ADR-040 §3):

1. `significance_score` — маршрутизация хода по значимости (Fast/Standard/
   Critical) через 15 триггеров;
2. `concept_search` — поиск по доменной базе концептов (Ариадна) вместо grep;
3. `src/model` — сущности и граф связей (онтология + проверка циклов).

Правило №0 (ML-05, `neuralnet-design/SKILL.md`): ML-факты берутся из
первоисточника, а не из весов. Настоящий документ грундуется в исходниках
ядра и в доменной библиотеке; пути — в разделе «Источники».

Разделение ответственности, которое здесь защищается:

- **Ядро (`src/`)** — доменно-нейтральный механизм: как считается score, как
  маршрутизируется ход, как устроен поиск и граф.
- **Домен (`aiml/`)** — семантика: *какие* триггеры значимы в ML, *что*
  искать в базе концептов, *какие* виды сущностей заводит ML-исследователь.
  Домен поставляется данными (config + плагины), а не правкой `src/`.

## 1. Маппинг архитектурной онтологии на ML-домен

### 1.1. Основные соответствия

| Слой Spine | Spine-сущность | ML-эквивалент | Пример в ML | Грунт / инвариант |
|---|---|---|---|---|
| **System** (SYS) | эксплуатируемая система | **обученная модель** (served artifact) | `qwen2.5-7b-instruct` за vLLM на 127.0.0.1 | ADR-040 §2 (локальные open-weights), ML-01 |
| **Data source** | внешний/внутренний источник данных | **датасет / корпус / CPT-домен** | recipes_taxonomy, HF-датасет, синтетика ox-alpha | ADR-040 §4, `death-of-params-scaling` (200–900 ток./параметр) |
| **Component** (CMP) | модуль системы | **стадия пайплайна** (обучение/инференс) | CPT → SFT → RLHF → rubric-RL → RLVR → distillation | `ml-continual-learning/.../continual-learning-pipeline/SKILL.md` |
| **Artifact** | поставляемый артефакт | **чекпоинт / веса + манифест прогона** | `ckpt-0001200/`, run-manifest (config+versions+seed+hash) | ML-06, ML-07, ML-10 |
| **Integration** (INT) | связь систем/внешний API | **шов прививки и внешний провайдер** | projector/голова в графтинге; frontier-API (deepseek/kimi/glm) | `grafting/.../llm-grafting/SKILL.md`, ADR-040 §2 |
| **Process** | бизнес-процесс | **прогон эксперимента** (run) | GRPO-прогон на 4×RTX4090, KL=0.01 | ADR-040 §5, `grpo-kl-collapse-prevention` |
| **Operation** | эксплуатационная операция | **дистилляция / steering / графтинг** | logit-KD, retrofit attention, merge/TIES | `distillation`, `grafting`, `model-optimization` |
| **NFR-verification** | проверка нефункциональных требований | **eval-harness / верификатор / рубрика** | бенчмарк, rubric-judge, RLVR-верификатор | ML-12 (честный отчёт), ML-11 (судья рубрик) |

Порядок стадий пост-тренинга — **DAG, а не линейный конвейер**: дистилляция,
rubric-RL и RLVR можно применять на разных ветвях. Инвариант стадии —
**качество сигнала не выше качества верификатора/reward**
(`continual-learning-pipeline/SKILL.md`) — отсюда особая критичность триггера
смены верификатора (раздел 2).

### 1.2. Соответствие видов сущностей (`EntityKind`)

Ядро знает 11 префиксов (`src/model.rs`, `src/model/parse.rs`): CAP, SYS, CMP,
INT, NFR, REQ, AD, ADR, RISK, OWNER, QAS; связи — DependsOn / Implements /
Affects / VerifiedBy. ML-проекция (целевой состав, см. раздел 4.3):

| Spine `EntityKind` | ML-вид | Смысл | Куда ложится |
|---|---|---|---|
| SYS | **MODEL** | обученная/сервируемая модель | домен (`aiml/`) или расширение ядра — открытый вопрос 4.3 |
| — (новый) | **DATASET** | корпус/датасет с провенансом | домен |
| CMP | **STAGE** | стадия пайплайна (cpt/sft/rl/…) | домен |
| — (новый) | **RUN** | прогон эксперимента (манифест) | домен |
| — (новый) | **CKPT** | чекпоинт/веса (артефакт) | домен |
| INT | **SEAM** | шов прививки / MCP-инструмент | домен |
| NFR | **EVAL** | бенчмарк/рубрика/верификатор | домен |
| ADR | ADR | архитектурное решение (в т.ч. о модели) | ядро, без изменений |
| RISK | RISK | риск (забывание, коллапс, утечка) | ядро, без изменений |

Ключевая связь ML — **линейдж ассетов**: `DATASET → RUN → CKPT → MODEL →
EVAL` (кто из чего произведён), плюс `SEAM Implements STAGE`. Это расширение
`LinkKind` (новый тип `DerivedFrom`) — предмет раздела 4.3.

### 1.3. Что НЕ переносится дословно

- Банковские NFR-поля (`rto_minutes`, `rpo_seconds`, `rps_target`, `replicas`,
  `cost_per_instance_month`) в `parse.rs` остаются валидными для **serving**-
  сущностей (SYS/MODEL: пропускная способность инференса), но не осмысленны
  для RUN/STAGE/CKPT.
- `financial_impact` банковского скоринга в ML переиспользуется как **бюджет
  GPU/токенов** (ML-09), а не как денежный эффект изменения.

## 2. `significance_score` для ML

### 2.1. Механизм (сохраняется из ядра)

Ядро считает score как число сработавших триггеров, маршрутизирует по
порогам и **объединяет** с эвристиками `detect_diff_triggers`
(`src/control.rs`: `significance_score_with_limits` :162, fail-safe union в
`score_with_sources`; пороги — `DEFAULT_FAST_MAX = 1`, `DEFAULT_STANDARD_MAX = 4`
:134–136; 15 триггеров :112; 3 принудительно-Critical :141). Пороги
настраиваются в `[significance]` (`significance.fast_max < standard_max` —
`config.rs`), у неизменяемого минимума — 3 forcing-триггера (ADR-034).

**Решение AD-1:** *количество и семантика* триггеров — доменные данные. В
ядре остаётся механизм (загрузка набора триггеров из конфига/плагина, union,
пороговое сравнение); конкретные 15 ML-триггеров поставляются пресетом
`aiml/presets/ml-researcher/`. Ядро не должно содержать строк, читаемых как
ML-специфика.

### 2.2. 15 триггеров значимости на языке ML

| # | ML-триггер | Что фиксирует (срабатывает, когда…) | BE-аналог | Класс |
|---|---|---|---|---|
| 1 | `new_model_component` | добавлен обучаемый узел/шов: проектор, голова, адаптер, LoRA, энкодер | new_component | обычный |
| 2 | `layer_retrofit` | ретрофит/замена слоя: full attention → GQA/MLA/linear, смена norm/RoPE | domain_ownership_change | обычный |
| 3 | `architecture_change` | смена семейства/бэкбона/размерностей (Transformer → MoE/SSM/hybrid) | new_component | обычный |
| 4 | `new_dataset_source` | новый корпус/датасет/CPT- или mid-training-домен | new_datastore | обычный |
| 5 | `new_stack_vendor` | новый движок/фреймворк/провайдер весов (veRL/vLLM/sglang; Kimi/GLM) | new_vendor | обычный |
| 6 | `checkpoint_format_change` | раскладка весов, имена тензоров, fused qkv, tie_word_embeddings | api_contract_change | обычный |
| 7 | `dataset_contract_change` | chat template, loss-mask, packing, схема записи, токенизатор/vocab | data_contract_change | обычный |
| 8 | `modality_grafting` | прививка чужой модальности/компонента (vision↔LLM, cross-lingual tokenizer) | cross_domain_integration | обычный |
| 9 | `security_boundary_change` | приватные данные/веса в промпт, лог сессии, W&B; утечка за контур | security_boundary_change | **forcing** |
| 10 | `training_objective_change` | алгоритм/лосс: SFT→RLHF/RLVR, GRPO→DPO, KL, advantage estimation | consistency_model_change | обычный |
| 11 | `reward_verifier_change` | reward-функция, RLVR-верификатор, судья рубрик — отравляет весь конвейер | criticality_or_exception | **forcing** |
| 12 | `distillation_to_frontier` | дистилляция/слияние/сжатие до целевого класса; потеря режимов | trust_zone_change | обычный |
| 13 | `eval_harness_change` | смена бенчмарка/рубрики/метрики — меняется само измерение результата | significant_nfr | обычный |
| 14 | `compute_budget_escalation` | выход за потолок GPU-часов/токенов API (ML-09) | financial_impact | обычный |
| 15 | `irreversible_weight_edit` | необратимая правка весов, merge без отката, teardown без выгрузки, удаление чекпоинта | irreversible_migration | **forcing** |

Обоснование forcing-набора:
- `security_boundary_change` — необратимая утечка данных (ML-01/ML-03);
- `reward_verifier_change` — сигнал стадии не выше качества верификатора
  (`continual-learning-pipeline/SKILL.md`), поэтому ошибка верификатора
  каскадирует и обесценивает прогон;
- `irreversible_weight_edit` — деструктив без отката (ML-10).

### 2.3. Пороги и маршрутизация (рекомендация для пресета)

| Score (сработавших) | Route | Что означает в ML | Действие |
|---|---|---|---|
| 0–1 | **Fast** | правка конфига/промпта, smoke-скрипт, дешёвый локальный ход | локальная модель, без pre-flight GPU |
| 2–4 | **Standard** | полный прогон: pre-flight VRAM/версии, манифест, делиберация старта | фронтьер для ризонинга, гейт перед спендом |
| ≥5 | **Critical** | аренда GPU, смена objective/верификатора, публикация весов | решение владельца, полный ADR-разбор |

Порог по умолчанию — ядровые `fast_max=1` / `standard_max=4`; пресет может
ужесточить (например `fast_max=0`), но обязан сохранить
`fast_max < standard_max` (`config.rs`, валидация `limits()`). **Любой
forcing-триггер → Critical независимо от score.** Набор forcing-триггеров —
часть доменной политики (data), но их *эффект* (непонижаемость) зашит в
механизм ядра.

### 2.4. Эвристики `detect_diff_triggers` для ML

`detect_diff_triggers` (`src/control.rs` :331) сегодня знает банковские
признаки (`DEP_MANIFESTS` = Cargo.toml/pom.xml/package.json/go.mod :194;
`CONFIG_EXTS` :197; regex миграций БД и datastore-connections). Для ML
доменный набор файловых признаков иной (поставляется как данные, ядро —
только механизм сопоставления путей с триггерами):

| Признак в diff | Триггер |
|---|---|
| `*.yaml`/`*.toml` c `fsdp|deepspeed|zero|tp_size|pp_size`, `config.json` модели | `architecture_change`, `new_stack_vendor` |
| `run-manifest.*`, `seed`, пины версий (torch/CUDA/vllm) | (гейт воспроизводимости ML-06) |
| `requirements.txt`/`pyproject.toml`/`*.lock` (стек RL/inference) | `new_stack_vendor` |
| новый каталог `data/`/`corpus/`/манифест датасета (`license/source/hash`) | `new_dataset_source` |
| `reward*.py`, `verifier*`, `rubric*`, судья | `reward_verifier_change` (**forcing**) |
| `*.safetensors`, `*/ckpt-*`, `adapter_config.json` | `checkpoint_format_change`, `irreversible_weight_edit` |
| `tokenizer*`, `vocab*`, `chat_template*` | `dataset_contract_change` |
| выгрузка весов наружу (`hf upload`, публикация) | `distillation_to_frontier` / периметр |

Воспроизводимость (ML-06) трактуется как **гейт, а не триггер**: отсутствие
манифеста прогона не повышает score, но блокирует перевод в Standard
отдельным fitness-правилом пресета.

## 3. `concept_search` для ML

### 3.1. Зачем отдельный инструмент, а не grep

Артефакт домена — *термин и его источник*, а не строка в файле. В ML термины
2020–2026 (MoE, CSA/HCA, LatentMoE, KDA, RLVR, GRPO, RLHF, KD-варианты) **не
живут в весах** большой модели и плохо ищутся буквальным grep по разборам.
`concept_search` — типизированный запрос к доменной базе концептов (Ариадна,
локальная 4B; ADR-040 §2), возвращающий **определение + путь-первоисточник +
уверенность**. Это инструмент, не исполнитель (ML-11), и он работает только
в приватном контуре (ML-01/ML-14).

Отличие от существующего `kb_search` (`src/kb.rs`, BM25 по `[knowledge].dirs`
с фрагментами ±2 строки): `kb_search` ищет **текст** в локальных документах,
`concept_search` ищет **концепт** в обученной базе и обязан возвращать
ссылочный первоисточник (иначе ответ малой модели — гипотеза, ML-05/ML-11).

### 3.2. Классы запросов

| Класс | Пример вопроса | Что вернуть | Опора |
|---|---|---|---|
| Термин | «что такое LatentMoE / KDA / RLVR» | определение + arXiv-id/путь | `nemotron3-arch-latentmoe`, `canon-deviations-2026` |
| Таксономия | «какое семейство под длинный контекст» | семейство + почему (сдвиг) | `neuralnet-design/SKILL.md` §1 |
| Рецепт RL | «как стабилизировать GRPO / какой KL» | рецепт + параметры | `rl-training`, `grpo-kl-collapse-prevention` |
| Дистилляция | «как перенести способность в малую модель» | техника (logit/on-policy/MOPD) | `distillation/.../distillation-mechanics/SKILL.md` |
| Графтинг | «как привить vision-энкодер к LLM» | паттерн шва + карта совместимости | `grafting/.../llm-grafting/SKILL.md` |
| Данные/CPT | «как не забыть при CPT» | техника (реплей, diag-mask, packing) | `cpt/.../continual-learning-llm/SKILL.md` |
| Верификатор | «как устроить rubric-Judge / RLVR» | схема верификатора | `rlvr/.../bodhi-branching-entropy-rlvr/SKILL.md` |
| Инфраструктура | «как связать veRL × vLLM × torch» | матрица версий/ABI | `ml-infrastructure`, ADR-040 (совместимость стека) |

### 3.3. Контракт ответа и стратегия

- Ответ — **гипотеза со ссылкой**: `{term, definition, source_path|arxiv_id,
  confidence}`; без источника — помечается как неподтверждённый (ML-05).
- Стратегия двух моделей (ADR-040, `CLAUDE.md`): v10 GRPO — частотные
  термины, v1 SFT — редкие; для критичных фактов — обе, расхождение
  фиксируется.
- Запрос — короткий самодостаточный (у 4B малое окно, 2–8k; см. `[models.
  ariadna]` в `config.toml`), temperature 0–0.3.
- **Не вызывать** на приватных данных (ML-01): в базу концептов уходит
  только термин/вопрос, не строки датасета (ML-03).
- Если понятие отсутствует в базе — честное «нет факта», не достройка
  (ML-05). Это же — предохранитель против выдуманных id моделей/бенчмарков.

## 4. Что менять в коде (без самого кода)

Ниже — файлы и **ориентировочный объём**, с решениями по AD-1 (механизм в
ядре / семантика в домене). Итоговая цель: ядро остаётся нейтральным, ML
поставляется данными пресета.

### 4.1. `src/control.rs` (сейчас ~236 KB)

- `SIGNIFICANCE_TRIGGERS` (:112, `[&str; 15]`) и `FORCING_CRITICAL_TRIGGERS`
  (:141) — **вынести из константы в загружаемый набор**: триггеры и forcing-
  подмножество приходят из `[significance]` (fallback — текущий банковский
  набор). Это снимает AD-1-конфликт: ML-15 и ML-forcing живут в
  `aiml/presets/ml-researcher/config.toml`.
- `detect_diff_triggers` (:331) + таблицы `DEP_MANIFESTS`/`CONFIG_EXTS`
  (:194–299) — сделать признаковый набор конфигурируемым (список
  glob/regex → имя триггера), ML-таблица из §2.4 — данные пресета.
- `SignificanceScoreTool` (:3444) — описание инструмента меняет перечень
  триггеров на загруженный; поведение score/route/union сохраняется.
- Потребители, читающие константу напрямую, обновить: `src/mcp_server.rs`
  (:383), `src/agent/slash.rs` (:690–692), `src/detectors.rs`
  (`READONLY_TOOLS` — добавить новый инструмент).
- Ориентир: **~250–400 строк** (загрузка набора, признаковый набор,
  обновление потребителей, тесты скоринга на ML-наборе — не по памяти).

### 4.2. `src/kb.rs` (сейчас ~1536 строк; реестр `tools()` :143)

- Новый `ConceptSearchTool : Tool` (рядом с `KbSearchTool` :968) — отдельный
  инструмент поверх доменного эндпоинта Ариадны (MCP/Ollama на
  127.0.0.1:11434), **не переиспользование BM25**. Новые типы результата
  (`ConceptHit { term, definition, source_path, confidence }`) и опции
  запроса.
- Транспорт — через существующий слой провайдеров (AD-4, один OpenAI-
  совместимый путь); ключ — по имени env (`ARIADNA_KEY`, фиктивный для
  локального сервера — см. `config.toml`).
- Регистрация в `tools()`; описание инструмента отражает контракт §3.3.
- Ориентир: **~250–350 строк** (инструмент + парсинг + тесты на мок-сервере).

### 4.3. `src/model/graph.rs` (253 строки) и `src/model.rs` / `parse.rs`

- `graph.rs` уже даёт 3-цветный `find_cycle` по `depends_on` и рендер mermaid
  — **переиспользуется** для проверки циклов линейджа данных/пайплайна
  (`DATASET → RUN → CKPT → MODEL → EVAL`).
- Добавить обход **линейдж-рёбер** (новый `LinkKind::DerivedFrom`) и сводку
  «происхождение артефакта» (что из чего обучено/слито) — расширение
  `graph_text`/mermaid; цикл (RUN зависит от собственного CKPT) = ошибка.
- ML-виды сущностей (MODEL/DATASET/STAGE/RUN/CKPT/SEAM/EVAL): **рекомендация —
  держать в домене** (конфигурируемый реестр префиксов), а не расширять
  `EntityKind` в ядре, чтобы не тянуть ML-специфику в `src/model.rs`
  (ADR-040 §3). Если реестр типов не удаётся вынести в конфиг без правки
  ядра — компромисс: расширить `EntityKind::ALL` доменно-нейтральными
  артефактными видами (artifact/run), а ML-ярлыки навесить в пресете.
- Ориентир: **~60–120 строк** в `graph.rs` (линейдж-рёбра + обход) и
  **~80–120 строк** в `config.rs` (секции `[significance].triggers`,
  реестр доменных видов сущностей).

Итог: механизм — в ядре, ML-семантика — в `aiml/presets/ml-researcher/`
(config) и в плагинах скиллов (`aiml/plugins/`). Ни одна правка не вводит
обратную зависимость `src/ → aiml/`.

## 5. Чек-лист адаптации

- [ ] Набор триггеров и forcing-подмножество читаются из конфига пресета
- [ ] ML-признаки diff (`detect_diff_triggers`) заданы данными, не кодом
- [ ] `concept_search` — отдельный tool с контрактом «термин + источник +
      уверенность», не executor (ML-11)
- [ ] Линейдж данных проверяется на циклы тем же `find_cycle`
- [ ] Ядро `src/` не содержит ML-строк; домен — только через config/plugin
- [ ] Пороги `fast_max < standard_max` валидируются; forcing → Critical всегда

## Источники (грундовка)

Ядро форка:

- `src/control.rs` — 15 триггеров (:112), forcing (:141), пороги (:134–136),
  `significance_score_with_limits` (:162), `detect_diff_triggers` (:331),
  `SignificanceScoreTool` (:3444)
- `src/kb.rs` — реестр инструментов `tools()` (:143), `KbSearchTool` (:968)
- `src/model.rs`, `src/model/parse.rs`, `src/model/graph.rs` — онтология
  сущностей, связи, проверка циклов
- `src/config.rs` — `SignificanceConfig` / валидация `limits()`
- `src/mcp_server.rs` (:383), `src/agent/slash.rs` (:690) — потребители
  `SIGNIFICANCE_TRIGGERS`

Домен и решения:

- `aiml/presets/ml-researcher/SPINE-ML.md` — инварианты ML-01…ML-14
- `aiml/presets/ml-researcher/config.toml` — `[models.ariadna]`, `[knowledge]`,
  `[policy]`
- `docs/adr/ADR-040-fork-spine-aiml-identichnost-modelnaya-matrica-domen.md` —
  идентичность, двойная матрица моделей, граница `src/` ∤ `aiml/`
- `ARCHITECTURE-SPINE.md` — AD-1 (тонкое ядро), AD-2 (детерминированный
  контроль)

Библиотека рецептов (`~/experiments/agents/0710-ariadna/plugins/`):

- `grafting/skills/llm-grafting/SKILL.md` — шов прививки, карта совместимости
- `distillation/skills/distillation-mechanics/SKILL.md` — техники дистилляции
- `cpt/skills/continual-learning-llm/SKILL.md` — CPT / катастрофическое
  забывание
- `rlvr/skills/bodhi-branching-entropy-rlvr/SKILL.md` и
  `postrain/skills/reward-verifier-lab/SKILL.md` — верификатор/reward
- `agentic-rl/skills/lego-rl-harness-native-rl/SKILL.md` — RL-харнесс
- Собственные скиллы зоны: `aiml/plugins/ml-continual-learning/skills/
  continual-learning-pipeline/SKILL.md`, `aiml/plugins/ml-architecture/skills/
  neuralnet-design/SKILL.md`, `aiml/plugins/ml-grafting/skills/
  llm-grafting/SKILL.md`

Дорожная карта:

- `~/Загрузки/Spine-BE_Дорожная_карта_развития_2026-09-11.md` —
  ось 3 (гибридная маршрутизация, оценка локальной модели на классификации
  значимости), ось 5 (Harness Card, доменный бенчмарк)
