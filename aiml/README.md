# aiml/ — доменная зона Spine AI/ML Edition

**Статус:** наполняется (начато при форке 2026-09-11). Зона уже несёт первые
доменные артефакты (см. «Текущее наполнение» ниже); целевая структура — по
образцу `banking/`, дорожная карта —
`~/Загрузки/Spine-BE_Дорожная_карта_развития_2026-09-11.md`.

Зона `aiml/` — аналог `banking/` из Spine Banking Edition: толстый домен за
типизированной границей. Ядро (`src/`) не должно зависеть от `aiml/`
(инвариант AD-1 / AD-BE1, перенесённый на новый домен).

## Текущее наполнение (2026-09-13)

- `presets/ml-researcher/` — пресет AI/ML-исследователя: `SPINE-ML.md`
  (14 инвариантов ML-01…ML-14 с Binds/Prevents/Rule), `config.toml`, README.
- `plugins/` — 8 доменных плагинов: шесть по скиллу — `ml-architecture`
  (neuralnet-design), `ml-grafting` (llm-grafting), `ml-continual-learning`
  (continual-learning-pipeline), `ml-rl-environments` (rl-concept-environments),
  `ml-selfplay` (selfplay-episode-contract), `aiml-ops` (knowledge-routing);
  плюс два механизменных — `hypothesis-router` (карточки гипотез
  `HYPOTHESIS.md` + роутинг по фактам проекта, скилл `hypothesis-card`,
  хук на 5 событий) и `laguna-gb10-skills` (5 скиллов лесенки маленьких
  моделей на GB10: `laguna-ladder-run`, `ood-stage-eval`,
  `cpt-template-check`, `rl-health-check`, `ladder-interpretation-report`).
- `library/fitness/wave1-ml-experiment.yaml` — 11 fitness-правил домена
  (seed, veRL/vLLM-пины, ABI-образ, gpu_memory_utilization, KL, chat-формат,
  ALFWorld dqn, HF_ENDPOINT, teardown, requirements) со структурированными
  карточками (trigger/rationale/evidence/reversibility/owner/expiry).
- `library/fitness/hypothesis-routing.yaml` — 4 fitness-правила роутинга
  гипотез (HYP-01…HYP-04) в схеме движка: handoff-пакет несёт гипотезы,
  `routing.json` не старее карточек, у каждой карточки есть триггеры,
  касания после принятия (unverifiable). Правила в гейт `CONSTRAINTS.yaml`
  не включены — запускаются явно (`control check --constraints ...`), см.
  `docs/hypotheses.md`.
- `demos/steering/` — демо активационного стиринга (html + dataflow.json).
- `notes/` — рабочие заметки адаптации (онтология, спеки, статусы H/H2).

Смежное: `docs/hypotheses.md` (в корне репо) — референс по карточкам
гипотез и роутингу: состояния и переходы, как поднять карточку, структура
`routing.json`, что делает хук на каждом событии, чего механизм не делает.
Не наполнено пока: `adf/` (анкеты-сенсоры), `library/` вне wave1,
дополнительные пресеты и демо (графтинг, дистилляция), `banking/` остаётся
эталоном толстого домена.

## Целевая структура (по образцу `banking/`)

```
aiml/
├── presets/        # доменные пресеты: ML-исследователь, графтинг, CPT/SFT/RL, дистилляция
├── plugins/        # доменные плагины: ru-ml-*, дистилляты рецептов, архитектур нейросетей
├── library/        # библиотека правил + fitness-функции домена (wave1/2, карточки дистилляции)
├── demos/          # доменные кейсы: нейросеть с нуля, LLM-графтинг, steering, дистилляция
├── adf/            # анкеты-сенсоры домена (входы гейтов)
└── notes/          # рабочие заметки адаптации
```

## Первые решения адаптации (по порядку)

1. Имя бинаря: **`arch-ml`** — переименован из `arch-be` при форке (избегает
   коллизии со Spine BE на одной машине). Crate `arch-harness`, конфиг-пути и
   MCP-имена не переименованы (по образцу AD-BE7). Сделано 2026-09-11.
2. Модельная матрица: DeepSeek / Kimi / GLM / любой OpenAI-совместимый +
   локальные open-weights; привязка к базе концептов (Ариадна) как
   доменному инструменту.
3. Доменные инструменты: concept_search/significance_score переносятся на
   домен ML (выбор архитектуры, графтинг, континуальное обучение, RL-рецепты).
4. `banking/` остаётся эталоном, пока `aiml/` не наберёт критическую массу;
   затем — архив.

Лицензия зоны — фиксируется при первом наполнении (см. `NOTICE.md`).
