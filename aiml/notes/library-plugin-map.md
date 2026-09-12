# Маппинг «тема дайджеста → плагин `aiml/plugins/ml-*`»

- Статус: спека/план (не код). Дата: 2026-09-11.
- Отношение к плану: раскрывает P2-строку «Маппинг тем library→plugin» и уточняет P1-строку
  «Доменные плагины из зон статей» в `aiml/notes/library-bridge.md`.
- Вход (тема): зоны корпуса `library/distillate/2_статьи/**` (+ `3_блоги/`).
- Выход (плагин): каталог `aiml/plugins/<ml-*>/` в формате Agent Plugins v1.0.0.
- Инвариант: ничего не копируется пачкой. Дайджест → `skill_distill` (draft → playbook →
  SKILL.md) → `skill_to_plugin.py` → плагин. Маппинг задаёт только **адрес**, не факт переноса.

---

## 1. Что такое THEME_MAP и где он лежит (факты, не догадки)

| Артефакт | Путь | Что делает |
|---|---|---|
| `THEME_MAP` | `~/experiments/agents/0710-ariadna/package_themed_plugins.py:12-72` | Словарь **префикс имени скилла → имя плагина** (140 пар, напр. `"grpo-": "rl-training"`, `"memory-": "memory-systems"`, `"vllm-": "inference-engines"`) |
| `skill_to_plugin.py` | `~/library/scripts/skill_to_plugin.py` | Роутит один SKILL.md-каталог в тематическое дерево плагинов |
| `load_theme_map()` | `skill_to_plugin.py:35-40` | Импортирует `THEME_MAP` из пакетёра через `spec_from_file_location` |
| `PACKAGER` | `skill_to_plugin.py:30` | **Захардкожен** на внешний путь `~/experiments/agents/0710-ariadna/package_themed_plugins.py` |
| `DEFAULT_ROOT` | `skill_to_plugin.py:29` | **Захардкожен** на `~/experiments/agents/0710-ariadna/plugins` (внешнее дерево) |
| `package_themed_plugins.py` | `SRC:7`, `DST:8` | Тот же внешний мир: `0710-ariadna/0710_v1` → `0710-ariadna/plugins` (1104 скилла) |

**Важно (защита от вымысла):** `THEME_MAP` — это НЕ «тема дайджеста → плагин». Ключи в нём —
префиксы **имён скиллов** (`grpo-reward-shaping` → `rl-training`), а значения — плагины **чужой
вселенной** `0710-ariadna` (`rl-training`, `memory-systems`, `inference-engines`, `misc-tools`, …).
Ровно одно значение из 140 указывает в `ml-*`: `"ml-stack": "ml-infrastructure"` (строка 60) —
единственное пересечение с неймспейсом харнесса. Ни одного имени зоны корпуса
(`03_ПОСТ-ТРЕЙНИНГ_RL`) в `THEME_MAP` нет: это карта скиллов, не карта дайджестов.
Мост строится в два шага: `зона дайджеста → (дистилляция) → имя SKILL.md → плагин ml-*`,
поэтому цель этого документа — задать **доменный** маппинг и способ его подать в тот же скрипт.

---

## 2. Ключевое свойство скрипта: `--theme` обходит `THEME_MAP`

`resolve_plugin()` (`skill_to_plugin.py:113-137`) разрешает плагин в порядке:

1. `--theme` — если после `sanitize_name()` совпал с существующим плагином или его префиксом
   → **ранний `return`, `THEME_MAP` не читается вообще** (`:115-128`);
2. иначе перебор `THEME_MAP` по префиксу имени скилла (`:129-131`);
3. иначе создание нового плагина из `--theme` или из первого токена имени скилла (`:128`).

Следствие: **уже сегодня** можно раскатывать скиллы в `aiml/plugins` без правок библиотечного
кода — достаточно передать явный `--theme ml-post-training-rl` и `--root` на aiml-дерево
(см. §5, режим A). `THEME_MAP` нужен только для роутинга «по имени скилла без подсказки».

---

## 3. Маппинг: тема дайджеста → плагин (основная таблица)

Зоны и счётчики — замер по `library/distillate/2_статьи/` на 2026-09-11 (`find … -name '*.md' | wc -l`).
«сущ.» — плагин уже есть в `aiml/plugins/`; «новый» — предлагается этой спекой.

| Зона / подзона корпуса | .md | Плагин `aiml/plugins/ml-*` | Статус |
|---|---:|---|---|
| `01_СРЕДЫ/01_Масштабирование_и_синтез` | 161 | `ml-agent-environments` | новый |
| `01_СРЕДЫ/02_Оценка_и_бенчмарки` | 1452 | `ml-eval-benchmarks` | новый |
| `02_МИД-ТРЕЙНИНГ/01_Теория_и_мотивация` | 80 | `ml-continual-learning` | **сущ.** |
| `02_МИД-ТРЕЙНИНГ/02_Данные_и_синтез_траекторий` | 159 | `ml-data-synthesis` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/01_Флагманские_рецепты` | 168 | `ml-post-training-rl` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/02_Алгоритмы_RL` | 263 | `ml-post-training-rl` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/03_Reward_и_верификация` | 134 | `ml-reward-verification` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/04_Память_в_RL` | 84 | `ml-agent-memory` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/05_Архитектура_агента` | 81 | `ml-agent-harness` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/06_Вне_фокуса` | 1 | `_misc` (не плагин) | — |
| `03_ПОСТ-ТРЕЙНИНГ_RL/07_Tool_Use_и_Retrieval` | 243 | `ml-tool-use-mcp` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/08_Инференс_и_скейлинг` | 182 | `ml-inference-engineering` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/09_Мульти-агентные_системы` | 120 | `ml-multi-agent` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/10_Safety_и_Alignment` | 370 | `ml-safety-alignment` | новый |
| `03_ПОСТ-ТРЕЙНИНГ_RL/11_Self_evo_агенты` | 14 | `ml-self-evolving` | новый |
| `04_КОМПЬЮТ/01_Инфраструктура_обучения` | 5 | `ml-infrastructure` | новый; единственный `ml-*` в `THEME_MAP` (`"ml-stack"`, `package_themed_plugins.py:60`); упомянут в `notes/domain-ontology.md:196` |
| `04_КОМПЬЮТ/02_Адаптивный_инференс` | 23 | `ml-inference-engineering` | новый |
| `04_КОМПЬЮТ/03_Инфраструктура_инференса` | 42 | `ml-inference-engineering` | новый |
| `04_КОМПЬЮТ/04_Edge_инференс` | 12 | `ml-inference-engineering` | новый |
| `05_МОДЕЛИ_И_АРХИТЕКТУРЫ` | 1 | `ml-architecture` | **сущ.** |
| `11_Техотчёты_лабораторий_LLM` | 228 | `ml-frontier-model-facts` | новый |
| `06_Вне_фокуса` | 9 | `_misc` (не плагин) | — |
| `3_блоги/Дистиллят — Inference Engineering Masterclass (Baseten).md` | 1 | `ml-inference-engineering` | новый |
| `temp_pdfs_*/` | 250 | — | staging, не мапить |

Итог: **3 существующих** (`ml-architecture`, `ml-continual-learning`, `ml-grafting`) + **12
предлагаемых** `ml-*`. Зоны `05_МОДЕЛИ_И_АРХИТЕКТУРЫ` в корпусе почти пусты (1 файл) — `ml-architecture`
и `ml-grafting` остаются **рукописными**, из дайджестов их не набирать.

Имена первых пяти новых плагинов (`ml-post-training-rl`, `ml-inference-engineering`,
`ml-agent-memory`, `ml-tool-use-mcp`, `ml-frontier-model-facts`) уже зафиксированы предложением в
`aiml/notes/library-bridge.md` (P1-строка «Доменные плагины из зон статей») — здесь они только
приняты и доразмечены; остальные — расширение того же ряда.

### Что НЕ переносится

- `temp_pdfs_*/` (250 md) — staging PDF-конвертации, не финальный корпус.
- Заглушки: **198** файлов `*.md` короче 1500 байт (замер `find … -size -1500c | wc -l`) —
  отсеивать до дистилляции.
- Всего `2_статьи`: 4082 md (3832 вне `temp_pdfs_*`); зона 03 — 1660 из них, зона 01 — 1613.

---

## 4. Доменный THEME_MAP для роутинга без `--theme` (предложение)

Если понадобится роутинг по имени скилла (как в шаге 2 `resolve_plugin`), внешний `THEME_MAP`
брать нельзя: его префиксы (`grpo-`, `vllm-`, `memory-`) указывают на плагины `rl-training` /
`inference-engines` / `memory-systems`, которых в `aiml/plugins` нет, — скрипт тогда **создаст**
их там, засорив `ml-*`-неймспейс. Нужен свой словарь, например
`aiml/plugins/themes.py` (только данные, без `main()`):

```python
# предлагаемые пары (префикс имени SKILL.md -> плагин aiml/plugins/ml-*)
DOMAIN_THEME_MAP = {
    "grpo-": "ml-post-training-rl",     "rl-": "ml-post-training-rl",
    "verl-": "ml-post-training-rl",     "credit-assignment": "ml-post-training-rl",
    "reward-": "ml-reward-verification", "rubric-": "ml-reward-verification",
    "rlvr-": "ml-reward-verification",  "verify-": "ml-reward-verification",
    "memory-": "ml-agent-memory",       "kv-cache": "ml-inference-engineering",
    "quant-": "ml-inference-engineering", "speculative-": "ml-inference-engineering",
    "vllm-": "ml-inference-engineering", "sglang-": "ml-inference-engineering",
    "mcp-": "ml-tool-use-mcp",          "rag-": "ml-tool-use-mcp",
    "tool-": "ml-tool-use-mcp",         "multi-agent": "ml-multi-agent",
    "harness-": "ml-agent-harness",     "self-evolve": "ml-self-evolving",
    "safety-": "ml-safety-alignment",   "eval-": "ml-eval-benchmarks",
    "bench-": "ml-eval-benchmarks",     "env-": "ml-agent-environments",
    "data-synth": "ml-data-synthesis",  "frontier-": "ml-frontier-model-facts",
    "cpt-": "ml-continual-learning",    "sft-": "ml-continual-learning",
}
```

Это черновик: пары выведены из §3, при реализации сверить с фактическими именами скиллов после
`skill_distill` и при конфликте префиксов выигрывает более длинный (правило — как в
`package_themed_plugins.py:169-172`: первый совпавший префикс в порядке вставки).

---

## 5. Как переиспользовать `skill_to_plugin.py` (три режима)

**Режим A — без правок библиотечного кода (рабочий сейчас).**
Явный `--theme` + `--root` на aiml-дерево; `THEME_MAP` не читается вовсе (§2):

```bash
python3 ~/library/scripts/skill_to_plugin.py \
  --skill /tmp/grpo-reward-shaping \
  --theme ml-post-training-rl \
  --root ~/spine-aiml/aiml/plugins \
  --copy
```

`--copy` обязателен: по умолчанию скрипт **перемещает** каталог (`shutil.move`,
`skill_to_plugin.py:190`), а источник в библиотеке терять нельзя. Идемпотентность: если скилл уже
лежит в целевом плагине — `{"status":"skipped"}`, exit 0 (`:179-184`). `plugin.json` пересобирается
всегда (`:193-194`, `build_plugin_json:83-103`).

**Режим B — синхронизация дефолтов (когда нужно без `--root`/`--theme`).**
Правятся две константы скрипта:

- `DEFAULT_ROOT` (`:29`) → `~/spine-aiml/aiml/plugins`;
- `PACKAGER` (`:30`) → aiml-овский файл тем (напр. `aiml/plugins/themes.py` из §4).

`load_theme_map()` (`:35-40`) импортирует модуль целиком, но `main()` не запускается
(`__name__ != "__main__"`) — поэтому shim-файл только с `THEME_MAP`/`DOMAIN_THEME_MAP` безопасен;
копировать в него `package_themed_plugins.py` целиком не нужно (там `SRC`/`DST` внешнего мира):
`load_theme_map` ищет атрибут `THEME_MAP` — либо назвать словарь `THEME_MAP`, либо поправить
строку `:40`.

**Режим C — локальная копия.** Форк `skill_to_plugin.py` в `aiml/scripts/` (duplication, брать
только если нужны свои правила `resolve_plugin`). Предпочтительны A→B.

Проверка после любого прогона: `plugin.json` соответствует схеме
`https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`, число скиллов в `description`
совпадает с `ls skills/ | wc -l` (скрипт считает каталоги, `count_skills:106-110`).

---

## 6. Гейты перед переносом (согласовано с методологией библиотеки)

1. **Не пачкой.** Дайджест — сырьё; в плагин идёт SKILL.md, прошедший `draft → playbook → skill`
   (`library/distillate/1_методология/`, гейт «md vs skill», правило трёх повторений).
2. **Playbook-first.** `library-bridge.md` (P1) и `aiml/notes/p1-playbook-spec.md` вводят слой
   playbook; зона `2_статьи` — источник playbook'ов в первую очередь, скиллы — только по `uses ≥ 3`.
3. **Дедуп-леджер.** `library/distillate/skill_state.json` (`{articles[], digests[], note,
   last_update}`) + `scripts/skill_candidates.py` — детект уже перенесённых дайджестов, чтобы не
   собрать один arXiv-дайджест дважды.
4. **Frontmatter как фасет.** Дайджесты несут `type: digest`, `source: arXiv:…`, `concepts: []`,
   `created`; использовать при выборке (см. `aiml/notes/p1-frontmatter-spec.md`), не читать тела.
5. **Никаких `ml-*` мимо §3.** Новый плагин = новая строка в таблице §3, а не побочный эффект
   прогона скрипта (риск режима без `--theme`, §4).

---

## Источники

- `~/library/scripts/skill_to_plugin.py` (`DEFAULT_ROOT:29`, `PACKAGER:30`,
  `load_theme_map:35-40`, `resolve_plugin:113-137`, `main:140-199`)
- `~/experiments/agents/0710-ariadna/package_themed_plugins.py` (`THEME_MAP:12-72`,
  `SRC:7`, `DST:8`) — найден рядом с библиотекой по имени, как указано в задаче; в `library/scripts/`
  его нет
- `~/library/distillate/2_статьи/` (зоны `01_СРЕДЫ`…`11_Техотчёты_лабораторий_LLM`,
  `temp_pdfs_*`), `3_блоги/`
- `~/library/distillate/1_методология/` (md-vs-skill, playbook)
- `~/spine-aiml/aiml/notes/library-bridge.md` (P1/P2-строки), `domain-ontology.md:196`
- `~/spine-aiml/aiml/plugins/{ml-architecture,ml-continual-learning,ml-grafting,aiml-ops}/`
- `~/spine-aiml/aiml/presets/ml-researcher/config.toml`
