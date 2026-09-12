---
name: rl-concept-environments
description: >-
  RL-среды оценки знания ML-концептов: реестр из 8 сред E1_define…E8
  (определение, формула, классификация, извлечение, связь, контраст, починка,
  план эксперимента), каждая = задача-формат + verifier-паттерн +
  опциональный LLM-судья по рубрике. Разбирает анатомию среды
  (tasks_file / verifier / judge_weight / verifiable_weight / k_samples /
  task_count), контракт verifier'а (float 0..1 либо dict
  {gate, components, evidence}), шкалу судьи 0–4 и формулу
  task_score = vw·verifier + jw·(judge/4), калибровку судьи по рубрике
  (Spearman ρ ≥ 0.75), известные дефекты верификаторов (E4 константный 0.5,
  E8 заглушка 0.0, E5-адаптер from/to → source/target, E1 word-overlap) и
  как собрать честный сигнал для трёх потребителей: оценка знания, arena-лига
  бесплатных моделей, reward для RL. ВСЕГДА используй этот навык, когда
  пользователь: «оценить знание концептов», «RL-среда для оценки», «какой
  verifier», «E1_define / E4_extract / E6_contrast», «веса judge_weight и
  verifiable_weight», «сколько k_samples», «LLM-судья по рубрике», «шкала
  0–4», «task_score», «калибровка судьи», «каппа / Spearman судьи», «arena
  лига на средах», «reward-сигнал из verifier», «detерминированный vs
  судейский verifier». Грундится в источнике `library/rl_envs/envs.yaml`
  (реестр сред), `library/rl_envs/verifiers/*.py` (код верификаторов),
  `library/rl_envs/judge/rubrics/*.md` (рубрики судьи) и
  `library/rl_envs/arena/SPEC.md` §3 (контракт судьи и формула скоринга) —
  не выдумывай веса и паттерны, цитируй реестр.
---

# RL-среды оценки знания концептов

Восемь сред E1…E8 — это машина, превращающая «знает ли модель концепт» в
число. Каждая среда — одна пара **(формат задачи → способ проверки)**, а
реестр задаёт, **чем** проверять (verifier-код или LLM-судья) и **с каким
весом**. Оценка знания концептов здесь не «спроси и поверь», а
`task_score = vw·verifier_score + jw·(judge_score/4)` — детерминированный
вердикт кода и семантический вердикт судьи сводятся по весам из реестра.

Источники (грундовка, не выдумывай):
- Реестр сред и веса: `~/library/rl_envs/envs.yaml`
- Код верификаторов: `~/library/rl_envs/verifiers/{v_define,v_extract,v_relate,v_contrast,v_plan,common}.py`
- Рубрики судьи: `~/library/rl_envs/judge/rubrics/E{1,4,5,6}_*.md`
- Калибровка судьи: `~/library/rl_envs/judge/calibration/calibration_template.json`
- Контракт судьи и формула скоринга: `~/library/rl_envs/arena/SPEC.md` §3 + `arena/arena.py`
- Задачи (объёмный датасет, НЕ копировать): `~/library/rl_envs/tasks/*.jsonl`

## Правило №0 — веса и паттерны берутся из реестра, а не из головы

`judge_weight`, `verifiable_weight`, `k_samples`, путь к verifier'у — всё это
**читается из `envs.yaml`**, а не назначается на глаз. Изменение веса меняет
смысл метрики: среда с `judge_weight=0.0` (E3, E7) — чистый детерминированный
тест, среда с `judge_weight=0.7` (E6) меряет в основном судейское суждение.
Если факта в реестре нет — так и скажи; реестр — источник истины по весам.

## 1. Анатомия среды (7 полей реестра)

Каждая запись `environments.<E>` в `envs.yaml`:

| Поле | Смысл |
|---|---|
| `status` | `ready` \| `planned` (E8 — planned, 0 задач) |
| `tasks_file` | JSONL с задачами: `{task_id, prompt, gold_*, meta}` (`meta.task_type` = тип задачи) |
| `verifier` | пустая функция-верификатор: `verifiers/v_*.py::fn` или `verifiers/common.py::fn` |
| `judge_weight` (jw) | вклад LLM-судьи в `task_score` |
| `verifiable_weight` (vw) | вклад кода-верификатора; `jw + vw = 1.0` |
| `k_samples` | сколько ответов на задачу сэмплировать (для RL-дисперсии; для лиги k=1) |
| `task_count` | число задач (снапшот реестра; фактические строки в JSONL могут быть больше) |

Доп. секции реестра: `splits` (`train_ratio: 0.8`, `split_by: paper_id`,
`splits_file: ../index/splits.json`) и `judge` (`rubrics_dir`,
`calibration_dir`, `cache_dir`, `contract`).

## 2. Восемь сред: формат → verifier → веса

Веса — **дословно из `envs.yaml`** (снапшот 2026-07-10); `k` = `k_samples`.

| Среда | task_type | Verifier | jw / vw | k | task_count |
|---|---|---|---|---|---|
| **E1_define** | `define` — дать формальное определение (тип, вход, выход) | `v_define.verify_define(answer, gold_card)` → float: доля общих слов с gold | **0.5 / 0.5** | 8 | 414 |
| **E2_formula** | `formula` — записать формулу в LaTeX + расшифровать обозначения | `common.verify_formula(resp, gold_card)` → dict | **0.1 / 0.9** | 8 | 75 |
| **E3_classify** | `classify` — тип (6 классов) / уровень (α/β) / формальность (A/B/C) | `common.verify_classify(resp, gold_card)` → dict | **0.0 / 1.0** | 12 | 412 |
| **E4_extract** | `extract` — извлечь карточку концепта из текста статьи | `v_extract.verify_extract(answer, gold_card)` → float | **0.4 / 0.6** | 4 | 45 |
| **E5_relate** | `relate` — описать отношение двух концептов (from→to) | `v_relate.verify_relate(answer, gold_relation)` → float | **0.2 / 0.8** | 8 | 216 |
| **E6_contrast** | `contrast` — сравнить два концепта, назвать отличие | `v_contrast.verify_contrast(answer, gold_diff)` → float | **0.7 / 0.3** | 8 | 100 |
| **E7_repair** | `repair` — найти и исправить порчу в карточке | `common.verify_repair(resp, corrupted_card, gold_card)` → dict | **0.0 / 1.0** | 8 | 100 |
| **E8_plan_experiment** | `plan_experiment` — план ML-эксперимента | `v_plan.verify_plan(...)` → float (заглушка) | **0.7 / 0.3** | 4 | 0 |

Читать эту таблицу как **спектр**: E3/E7 — чистый код (vw=1.0, судья не
нужен), E6/E8 — почти чистый судья (jw=0.7), E1 — ровно пополам. Чем
формальнее и однозначнее правильный ответ, тем больше весит verifier; чем
ответ — свободная проза, тем больше весит судья.

## 3. Контракт verifier'а: две формы возврата

Верификатор — **чистая функция** без побочных эффектов. Возвращает одно из:

- **float `0..1`** — E1, E4, E5, E6, E8. Берётся как есть.
- **dict** `{gate: bool, components: {name: float}, evidence: {...}}` — E2, E3, E7.
  `verifier_score = mean(components.values())`; `gate=False` означает
  провал структурного гейта (например, в ответе нет LaTeX), `evidence` —
  свидетельства для разбора.

**Паттерны по средам (из кода верификаторов):**

- **E1 word-overlap** — `|gold_terms ∩ answer_terms| / |gold_terms|`; простая
  лексическая близость, штрафует перефраз (синонимы — 0).
- **E2 LaTeX-гейт + бонус за точное совпадение** — `gate=False` без `$…$`;
  компонент `latex` = доля точных gold-формул × 1.5 (кап 1.0), `symbols` = 0.8
  при найденной расшифровке обозначений, иначе 0.3.
- **E3 фасеты** — три компонента `type_match` (один из 6 типов), `level_match`
  (α/β), `formality_match` (A/B/C); gold парсится из YAML-frontmatter карточки.
- **E4 сигнал введения** — ищет в ответе начало цитаты из секции
  «## Сигнал введения» gold-карточки; иначе **константный 0.5** (частичный
  кредит — верификатор слабый, вклад судьи jw=0.4 его компенсирует).
- **E5 обе сущности** — `1.0`, если в ответе есть и `source`, и `target`;
  иначе `0.0`. **Ловушка**: verifier читает ключи `source`/`target`, а данные
  задач хранят `from`/`to` — нужен адаптер `{'source': rel['from'], 'target': rel['to']}`,
  иначе пустые строки матчатся всегда и verifier дегенератен (константная 1.0).
  Defect зафиксирован в `arena/SPEC.md` §3; сам `v_relate.py` не трогаем.
- **E6 сравнение** — `0.7`, если есть слово «отличие»/«difference», иначе `0.3`.
  Грубый keyword-детектор — потому jw=0.7 и решает судья.
- **E7 починка** — `fixes_found` (доля исправленных порч фронтматтера) +
  `new_errors` (штраф за новые ошибки): `1 − min(1, new_errors/|gold_fm|)`.
  Требует `corrupted_card` (не gold!) вторым аргументом.

## 4. LLM-судья: шкала 0–4 и рубрика

Судья включается только в средах с `jw > 0` — по `envs.yaml` это **E1, E2, E4,
E5, E6, E8** (в arena-реализации — E1, E4, E5, E6, где есть файлы рубрик).

**Протокол вызова** (grounded: `arena/SPEC.md` §3, `arena/arena.py`):

```
system: You are an expert judge. Reply with valid JSON only.
user:   TASK: <prompt>
        RUBRIC: <текст judge/rubrics/<E>.md>
        REFERENCE (gold): <gold_card | gold_relation | gold_diff>
        MODEL ANSWER: <ответ модели>
        Score the model answer 0-4 according to the rubric.
        Respond with STRICT JSON only: {"score": <int 0-4>, "rationale": "<brief>"}
```

- Рубрика — markdown-файл `judge/rubrics/E<n>_<name>.md`, шкала **0–4**
  (0 — полностью неверно / фабрикация, 4 — образцово: формализация, edge
  cases, цитаты из источника).
- Ответ судьи — строгий JSON `{"score", "rationale"}`; при невалидном JSON —
  repair-промпт до `judge_json_retries` (по умолчанию 3), затем
  `judge_score = null`.
- Генерация судьи детерминирована: `temperature=0.0`, `max_tokens=4096`
  (низкий лимит режет reasoning-судьям скрытое рассуждение → `empty_choices`).
- `judge_score` зажимается в `[0, 4]`; нормализация — делением на 4.

**Контракт судьи**: `envs.yaml` ссылается на `judge/judge_cli.md`, но **этого
файла в библиотеке нет** (проверено: `find library -name 'judge_cli*'` пуст).
Действующий контракт судьи материализован в `arena/SPEC.md` §3 и `arena/arena.py`
(фаза `judge`) — ссылайся на них, а не на отсутствующий `judge_cli.md`.

## 5. Формула скоринга и три потребителя

```
task_score = vw · verifier_score + jw · (judge_score / 4)   # веса из envs.yaml
model_env  = mean(task_score по задачам среды без error)
overall    = mean(model_env по средам)                      # среды равнозначны
```

Если судья не вернул валидный JSON → `judge_score = null`, `task_score`
считается по верификатору с **пересчитанным весом**, флаг `judge_missing`.

Три потребителя одного сигнала:

1. **Оценка знания концептов** — `overall` по средам: профиль модель×среда
   показывает, *где* модель знает (E3-классификация дёшева и точна,
   E6-контраст требует глубины). Одна цифра врёт, матрица — нет.
2. **Arena-лига** (`arena/arena.py`) — те же среды и веса прогоняются по
   внешним моделям (15 free OpenRouter, судья `z-ai/glm-5.2:free`). Для лиги
   `k=1` на задачу (не `k_samples` — это для RL), `tasks_per_env=15`, `seed=42`.
   Судья судит и себя — дисклеймер обязателен.
3. **Reward для RL** — `k_samples` (4–12) дают группу ответов на задачу →
   групповую дисперсию → advantage (GRPO). Здесь `task_score` — награда, а не
   метрика: судейскую часть держи детерминированной (`temperature=0`) и по
   возможности малой (`jw`), verifier-часть — стабильной; нестабильный reward
   ломает обучение так же, как mode collapse.

## 6. Калибровка судьи (обязательна перед доверием к jw>0)

Из `judge/calibration/calibration_template.json`:

- Нужно **≥50 человеческих аннотаций** (формат: `task_type`, `concept_slug`,
  `model_answer`, `gold_answer`, `human_score 0–4`, `human_rationale`;
  поля `judge_score`/`judge_rationale` заполняются потом).
- Цель: **Spearman ρ ≥ 0.75** между судьёй и человеком.
- Пока калибровка не сделана (`status: needs_human_annotation`), вес
  `judge_weight` — заявка, а не измеренная величина. Не повышай `jw` без
  калибровки; не сравнивай среды с jw=0 и jw=0.7 как равные, пока судья не
  откалиброван.

Харнесс: рубрика `assets/rubrics/rl_environments.yaml` — это **карточка
пригодности контура оценки** (version/kappa/calibrated/anti_examples), а не
замена судейским рубрикам библиотеки. Суждение в хуках не живёт — только
механическая проекция критерия.

## 7. Известные дефекты (не повторять, чинить осознанно)

- **E4 verifier константный 0.5** — без точной цитаты «Сигнал введения» всегда
  0.5; вклад судьи 0.4 компенсирует, но чистого кода тут мало.
- **E8 verifier — заглушка** (`verify_plan` возвращает 0.0), среда `planned`,
  0 задач: E8 пока нерабочая — не строишь на ней выводы.
- **E5 адаптер** — см. §3: без `from/to → source/target` verifier дегенератен.
- **E1 word-overlap** — синонимичный правильный ответ скорится низко;
  не путай «модель не знает» с «модель сказала иначе».
- **`task_count` в реестре — снапшот**, фактические JSONL могут быть больше
  (реестр E3=412, файл `classify.jsonl` — 6568 строк). Не считай метрику по
  устаревшему `task_count`.
- **Датасет не копируется.** `tasks/*.jsonl` (~9 282 строки) — объёмный
  датасет библиотеки; ссылайся на путь, не таскай в плагин.

## Чек-лист

- [ ] Веса `jw`/`vw` и `k_samples` взяты из `envs.yaml`, не назначены вручную
- [ ] Известна форма возврата verifier'а (float или dict) и как из неё выходит `verifier_score`
- [ ] Для E5 применён адаптер `{source: from, target: to}`; для E7 передан `corrupted_card`
- [ ] Судья включается только там, где `jw > 0`; его JSON строгий, `temperature=0`
- [ ] `task_score` = `vw·verifier + jw·judge/4`; `judge_missing` учтён
- [ ] Калибровка судьи (Spearman ≥ 0.75) сделана ДО доверия к `jw > 0`
- [ ] Для RL взяты `k_samples>1`; для лиги — `k=1`, `seed=42`
- [ ] Отчёт — матрица модель×среда, а не одна усреднённая цифра; дисклеймер о самосудействе раскрыт
- [ ] `tasks/*.jsonl` не скопированы — только путь библиотеки

## Источники (грундовка)

- `~/library/rl_envs/envs.yaml` — реестр 8 сред, веса, k_samples, splits, judge-конфиг
- `~/library/rl_envs/verifiers/v_define.py`, `v_extract.py`, `v_relate.py`, `v_contrast.py`, `v_plan.py`, `common.py` — код и паттерны верификаторов
- `~/library/rl_envs/judge/rubrics/E1_define.md`, `E4_extract.md`, `E5_relate.md`, `E6_contrast.md` — рубрики судьи, шкала 0–4
- `~/library/rl_envs/judge/calibration/calibration_template.json` — формат калибровки, цель Spearman ρ ≥ 0.75
- `~/library/rl_envs/arena/SPEC.md` (§2 участники, §3 контракт судьи и формула скоринга), `arena/arena.yaml`, `arena/arena.py`
- `~/library/rl_envs/oxalpha/envs_oxalpha.yaml` — свежий набор задач (5 000, seed 7)
- `~/library/rl_envs/tasks/*.jsonl` — датасет (ссылка, НЕ копировать)
- План моста: `~/spine-aiml/aiml/notes/library-bridge.md` (P2-строки: RL-среды как пресет)
