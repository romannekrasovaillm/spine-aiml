# Мост библиотеки Ариадна → харнесс Spine AI/ML (arch-ml)

- Статус: план переноса (не код). Дата: 2026-09-11.
- Источник: `~/library` («Ариадна — Concept Library v4.0»).
- Целевой потребитель: доменная зона `aiml/` форка `arch-ml` (`src/`, `aiml/plugins`, `aiml/presets`).
- Инвариант: ядро `src/` не должно зависеть от `aiml/` (AD-1 / AD-BE1). Домен поставляется
  данными (config + плагины/пресеты), а не правкой ядра.

Приоритеты: **P0** — уникальный ров/инструмент сразу; **P1** — важная доменная глубина;
**P2** — опционально/позже.

## Что забрать в харнесс (P0/P1/P2 таблицей)

| P | Что забрать | Почему (что даёт харнессу) | Куда/как (точка интеграции) |
|---|---|---|---|
| **P0** | **`concept_search` — поиск по базе концептов Ариадны** (`index/concept_index.json` 126 MB / **186 686** концептов и **~182 K** алиасов, `index/concept_graph.json` 50 MB) | Уникальный ров: ни у `arch-ml`, ни у `banking/` нет внешней базы на 186 K ML-концептов с резолвом синонимов и графом смежности. `kb_search` её не потянет — в `src/kb.rs` жёсткий лимит `MAX_CONTENT_BYTES = 5 MB` на файл, т.е. индексируются только имя/путь, а не содержимое | Новый доменный инструмент `src/concept.rs` (или расширение `src/model`), регистрируется в `tools.rs::domain_tools` гейтом по конфигу/пресету. Возможности: (1) lookup по slug; (2) резолв алиасов ПЕРЕД поиском (грязные unicode: `π0.5`→`0_5_model`, `GRPO`→`group_relative_policy_optimization`); (3) фасеты `type`/`level αβγ`/`formality ABC`/`family`; (4) `neighbors(slug, in|out|both)` по `edges_in`/`edges_out` с центральностью (число `edges_in`) как сигналом ранжирования (GRPO=514); (5) возврат тела карточки по полю `file`. Индекс грузить mmap/стримингом, не через walk kb |
| **P0** | **Подключить библиотеку как `knowledge.dirs`** — `~/library/distillate` + `~/library/pedagogy` + `~/library/concepts` | Самый дешёвый и быстрый мост: `kb_search` уже умеет BM25 по md со сниппетами и heading-boost. Педагогика (`faq` 18 410, `digests` 6 312, `tracks` 4 581, `glossary` 14 104 файла) + дайджесты статей (3 728) + карточки концептов становятся искомой базой без единой строки кода | Правка `[knowledge].dirs` в `aiml/presets/ml-researcher/config.toml` (сейчас заглушки `~/knowledge/*`). Расширения оставить `["md","txt","rst"]`. Паттерн Context Supply Chain: сузить до 2–3 доменных каталогов, не «вся библиотека» |
| **P0** | **Гейт «md vs skill» + правило трёх повторений** (`distillate/1_методология/distillate — библиотека дистиллята и критерий md vs skill.md`) | Закрывает главный дефект `skill_distill`/`/distill`: сейчас конвейер идёт **статья → SKILL.md напрямую**, пропуская digest/playbook — методология называет это «преждевременный скилл» (замороженная ошибка, которую агент уверенно повторяет). Гейт решает, что оседает в kb, а что становится скиллом | (1) В промпт `skill_distiller` (`src/assets.rs`, `assets/prompts/skill_distiller.md`) добавить первым шагом дерево решений из §7; (2) **fitness-правило** (`src/control.rs`): артефакт без триггера и стабильной процедуры не пишется в `plugins/<plugin>/skills/`; (3) мета-скилл `aiml/plugins/*/skills/knowledge-routing/SKILL.md` рядом с `skill-authoring`. Тест «агент ошибётся без этой инструкции?» — обязательный |
| **P1** | **Playbook-стадия — промежуточный слой** (`distillate/1_методология/концепт-playbook — роль между дистиллятом и скиллом.md`) | В харнессе нет среднего слоя между дистиллятом и скиллом. Playbook = процедура на человеческой скорости (`uses`-счётчик + `skill_candidate: true`), спасает от преждевременного скилла и сохраняет «почему именно так» (провенанс, отвергнутые варианты) после градации | Второй managed-путь «дистилляция в md» рядом с `distill_to_skill`: запись playbook в kb (`type: playbook`, frontmatter `uses`/`skill_candidate`) + команда/рубрика градации, проверяющая `uses ≥ 3` и стабильность шагов ПЕРЕД записью в managed-зону `arch-distilled`. Архивный playbook помечать `[graduated]` с указателем на скилл |
| **P1** | **Frontmatter как фасет поиска** (`distillate/1_методология/концепт-фронтматтер-в-markdown.md`) | `kb.rs` сегодня скорит только тело — метаданные как дешёвый сигнал маршрутизации не используются. Фронтматтер (`type`/`status`/`source`/`concepts[]`/`created`) даёт «поиск без чтения» через yq-подобные фильтры и колонки датасета | Расширить индексатор `kb.rs`: парсить YAML-frontmatter и уметь фильтровать по `type`/`status`/`concepts`. `concept_search` индексирует карточки по `type`/`level`/`formality`/`family`/`sources`. `skill_distill` и `/distill` обязаны эмитить валидный frontmatter; `status: draft|stable` = «сгенерировано агентом» vs «прошло ревью» |
| **P1** | **Managed-блоки и якорные диффы для записи** (`концепт-html-комментарии-в-markdown.md`, `Диффы для TUI-агентов — память, инструкции, хуки.md`) | Готовый протокол перезаписи сгенерированного фрагмента «на месте» с обнаружением ручных правок. Unified diff для прозы хрупок — нужны якорные операции по пути заголовков | Контракт для того, что пишет `/distill` в kb и `arch-distilled`: маркеры `<!-- agent:managed:begin id=… ver=… hash=sha256:… -->` … `end`, замена блока по id со сверкой hash, якорные операции replace/insert/append с `base_hash`, идемпотентность, атомарная запись tmp+rename, fail-fast на несовпадении `base_hash`. Для хуков — validate-before-install + upsert по id, обязательный `on_error` |
| **P1** | **Леджер дедупликации источников** (`distillate/skill_state.json`, `scripts/skill_candidates.py`) | Харнесс пишет дистилляты, но не ведёт реестр источников → `/distill` может пересобрать один и тот же arXiv-дайджест дважды. Плоский контракт `{articles[], digests[], note, last_update}` — проверенный в бою | Перенести дедуп-контракт как JSON-состояние рядом с плагином `arch-distilled` или запись в kb; шаг проверки в `/distill` и в cron-цикле (`skill_candidates.py` — детект новых дистиллятов/дайджестов за окно) |
| **P1** | **Расширение карточек рубрик** (`рубрики в оценке и обучении — коллекционирование через md, скиллы, хуки.md`) | `src/rubric.rs` + `assets/rubrics/*.yaml` не имеют полей `kappa` (согласие с человеком), `calibrated`, `version`, анти-примеров (reward hacking). Без них нет калибровочного контура и версионирования критерия — а рубрика двойного назначения (grading + reward-сигнал GRM/RLVR) | Добавить в YAML-карточку рубрики `version`/`kappa`/`calibrated`/`anti_examples`; поток калибровки: grading-скилл → замер каппы → якорное обновление frontmatter. Гард: суждение в хуках не живёт — только механическая проекция критерия; `/distill` не имеет права предложить хук для критерия-суждения |
| **P1** | **Доменные плагины из зон статей** (`distillate/2_статьи/`, 3 728 реальных дайджестов) | Существующие `ml-architecture`/`ml-continual-learning`/`ml-grafting` не покрывают профиль ML-исследователя. Концентраты: пост-тренинг RL (1 625), inference engineering (Baseten + `04_КОМПЬЮТ` 80), память/tool-use (83 + 235), техотчёты (228) | Плагины по зонам: `ml-post-training-rl` (grpo-recipe, credit-assignment, reward-verification, rubric-rewards, self-evolution-loop); `ml-inference-engineering` (quantization-fidelity, speculative-decoding, kv-cache-compression, pd-disaggregation, day0-serving); `ml-agent-memory`; `ml-tool-use-mcp`; `ml-frontier-model-facts`. Прогонять выбранные дайджесты через `skill_distill` (draft→playbook→SKILL.md), **не копировать пачкой**; отфильтровать ~190 заглушек <1.5 KB и `temp_pdfs_*` (staging, не финальный корпус) |
| **P1** | **Промпт извлечения концепций как скилл** (`prompts/extractor_v4.0.md`, `!ПРОМПТ v5 (извлечение концепций в md).md`) | Стабильная процедура + фиксированный формат + повторяется десятки раз — ровно случай «md vs skill» из самой библиотеки. Даёт харнессу второй managed-путь «extract-to-md» (декларативная карточка) рядом с процедурным `arch-distilled` | `skills/concept-extraction/SKILL.md` (тело = промпт + α/β/γ-уровни + формальность A/B/C), строгая JSON-схема карточки — в `references/` (progressive disclosure). Согласуется с `aiml/notes/domain-ontology.md` и полями «Векторизация»/«Training relevance» |
| **P1** | **Eval-контур скиллов: trigger-evals + output-evals** (`Рекомендации по генерации скиллов — разбор agentskills.io.md`) | У харнесса есть `src/rubric.rs` и `src/bench.rs`, но НЕТ trigger-evals для описаний скиллов и нет output-eval с дельтой «со скиллом/без». Правило «если агент справляется без скилла — скилл не нужен» — прямой антиспам для библиотеки плагинов | 7-шаговый конвейер + чеклист как quality-гейт `skill_distill`; формула `description` (what+when с триггер-фразами); trigger-evals (~20 запросов, 8–10 should/near-miss, порог 0.5); output-evals (with/without + assertions + `benchmark.json` delta) |
| **P2** | **RL-среды как доменный пресет** (`rl_envs/envs.yaml`, `tasks/*.jsonl`, `verifiers/*.py`, `judge/`, `arena/`) | 8 сред (E1_define…E8_plan_experiment) с весами `judge_weight`/`verifiable_weight` и `k_samples` — готовые рубрики и verifier-паттерны для оценки знаний по концептам | Доменный пресет/плагин: `assets` плагина + рубрики; переносим `judge/judge_cli.md` (контракт LLM-судьи), калибровочные рубрики, веса. Сами `tasks-jsonl` — объёмный датасет, не в ядро |
| **P2** | **Контракт selfplay-эпизода** (`selfplay/env_agent/ENV_CONTRACT.md`) | Переносимый доменный контракт: роли ENV-AGENT/SOLVER, reward назначает только внешний verifier (совпадает с fitness/rubric-дисциплиной харнесса) | plugin-skill «как устроен selfplay-эпизод»; сама генерация эпизодов (30 параллельных агентов) остаётся внешней batch-джобой |
| **P2** | **Goal-режим и синтетические напоминания** (`distillate/1_методология/distill_goal_mode_sinteticheskie_napominaniya.md`) | Паттерн управляющего цикла для автономных прогонов и крона: переинжект цели+критерия+статистики в хвост контекста каждый терн как untrusted-данные, дисциплина «один ограниченный шаг», проверяемый критерий завершения | Конфигурация контроль-петли (`docs/control.md`, `cron.example.toml`, `src/cron`), НЕ `skill_distill` |
| **P2** | **Маппинг тем library→plugin** (`scripts/skill_to_plugin.py`, `THEME_MAP`) | Библиотека УЖЕ умеет раскатывать плагины в формате agent-plugins.org 1.0.0 (тот же формат, что у харнесса) — мост можно переиспользовать, а не строить с нуля | Взять `THEME_MAP` как маппинг «тема дайджеста → имя плагина `aiml/plugins/ml-*`»; синхронизировать `DEFAULT_ROOT` скрипта с `aiml/plugins` при переносе |

## Ключевые открытия

1. **Индекс — единственный источник истины, и он в 4 раза больше релиза.**
   `index/concept_index.json` (пересобран 2026-09-10, `stats.total_concepts = 186 686`) против
   `index/release_manifest.json` (`concepts_total = 48348`, снапшот `ariadna-2026-07-r1`).
   README/релиз-манифест нельзя использовать как статистику объёма; доверять только `concept_index.json`
   (для enum типов релиз-манифест годится). Библиотека активно растёт.

2. **`concept_index.json`/`concept_graph.json` физически не подключаются к `kb_search`.**
   В `src/kb.rs` лимит `MAX_CONTENT_BYTES = 5 MB` — файл 126 MB индексируется только по имени.
   Значит нужен **отдельный инструмент `concept_search`** поверх индекса (или предварительный экспорт
   в порезанные md/JSONL). Это и есть главный аргумент приоритета P0.

3. **Библиотека уже говорит на языке харнесса.** `scripts/skill_to_plugin.py` раскатывает скиллы в
   дерево Agent Plugins v1.0.0 (`plugin.json` + `skills/<name>/SKILL.md`) — **тот же формат**, что у
   `arch-ml` и его плагинной подсистемы. Мост library→harness — это переиспользование существующего
   генератора и его `THEME_MAP`, а не трансляция форматов.

4. **Методология библиотеки закрывает ровно те пробелы, что есть у `/distill`.**
   `/distill`/`skill_distill` идут статья→SKILL напрямую (нет digest→playbook), не эмитят
   frontmatter-фасеты, не имеют trigger-evals и not-дедуп-леджера. 12 md в
   `distillate/1_методология/` — это готовые правила (md-vs-skill, три повторения, playbook с
   `uses`/`skill_candidate`, rubric-карточки с `kappa`, хуки только из рецидивов), которые ложатся
   в харнесс как мета-скилл + fitness-правила + расширение рубрик.

5. **Корпус размечен машинно-читаемым frontmatter'ом, а харнесс его не читает.**
   Дайджесты: `type: digest`, `status: draft`, `source: arXiv:…`, `concepts: [kebab-case]`, `created`.
   Карточки: `slug`/`type`/`level`/`formality`/`family`/`sources: [arxiv.*]`/`related`. Использование
   метаданных как фасета поиска — дешёвый O(1)-роутинг без чтения тел (сейчас `kb.rs` — frontmatter-blind).

6. **Доменная глубина сконцентрирована и не покрыта текущими aiml-плагинами.**
   Пост-тренинг RL (1 625 дайджестов), inference engineering (блог Baseten + 80 в `04_КОМПЬЮТ`),
   память агента (83) и tool-use/MCP (235), техотчёты лабораторий (228) — это точный профиль
   ML-исследователя, и он отсутствует в `ml-architecture`/`ml-continual-learning`/`ml-grafting`.
   `playbooks/` сейчас пуст — staging-слой существует как концепт, но на практике не заведён
   (харнессу предстоит реализовать его первым).

## Источники

- `library/index/concept_index.json` (стримингом — `stats`, `concepts.<slug>`, `aliases`; 126 MB не читать целиком)
- `library/index/concept_graph.json`, `library/index/release_manifest.json`, `library/index/manifest.jsonl`, `library/index/papers_to_extractions.json`
- `library/concepts/<type>/<slug>.md` (карточки; фронтматтер часто дублирован — брать последний блок)
- `library/distillate/2_статьи/**` (дайджесты: `type: digest` + 5 секций), `library/distillate/3_блоги/` (Baseten)
- `library/distillate/1_методология/*.md` (12 файлов методологии: md-vs-skill, playbook, frontmatter, HTML-комментарии, рубрики, хуки, диффы, промпты, goal-режим, дайджест Locus/Intology, agentskills.io)
- `library/distillate/skill_state.json` (леджер), `library/scripts/skill_candidates.py`, `library/scripts/skill_to_plugin.py`
- `library/pedagogy/{digests,faq,tracks,glossary}/`, `library/prompts/extractor_v3.3.md`…`v4.0.md`, `library/scripts/validate_extraction.py`
- `library/rl_envs/envs.yaml`, `library/rl_envs/{tasks,verifiers,judge,arena}/`
- `library/selfplay/env_agent/ENV_CONTRACT.md`
- Харнесс: `src/kb.rs` (лимит 5 MB, `kb_search`), `src/distill.rs` (`skill_distill`, `arch-distilled`), `src/rubric.rs` (`assets/rubrics/*.yaml`), `src/control.rs` (fitness), `config.example.toml` (`[knowledge].dirs`, `[plugins].dirs`), `docs/plugins_and_skills.md`, `aiml/notes/domain-ontology.md`
