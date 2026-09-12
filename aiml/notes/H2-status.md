# Статус H2 (self-improving harness + RL-через-харнесс)

## H2.2 — Guarded harness evolution (propose/commit): РЕАЛИЗОВАНО

`src/evolve.rs` + `arch-ml evolve propose|list|commit|reject`:
- предложение изменения доменного слоя (managed-блок + target) записывается
  только с именным аппрувером (govern);
- коммит — гейт: аппрувер + полнота → committed, применение через managed-блок
  (upsert + сверка хэша). Детерминированный слой (fitness-правила) — внешний
  страж: контрольный прогон `control check` отдельным шагом CI, а не внутри commit.

## H2.3 — Собственный харнесс как категория скиллов: РЕАЛИЗОВАНО

`assets/plugins/spine-aiml-docs/` (вшит в бинарь): скилл `check-spine-aiml-docs`
отвечает на вопросы о харнессе по документации репозитория (бинарь arch-ml,
доменные инструменты, fitness-правила, модельная матрица, мост библиотеки).

## H2.4 — Единый eval/бенчмарк-раннер: ЧАСТИЧНО (уже есть)

- `src/bench.rs` — рубричный бенчмарк (golden-сет, MAE, Spearman, `bench run --golden/--baseline`).
- `src/eval.rs` — continuous eval-сьют (`agent-config` герметичен, гоняется в CI).
- `benchmarks/platformv-arch-bench/` (24 задачи) + `benchmarks/spine-vs-claude/` (H0.1 A/B).

**Пробел:** нет единого `arch-ml bench compare <baseline> <candidate>` над
произвольным бенчмарк-каталогом (сейчас `--baseline` только для criterion-стиля
внутри `bench run`). Низкий приоритет — фундамент есть.

## H2.1 — RL-через-харнесс: ПЕТЛЯ ПРОВЕРЕНА НА СТЕНДЕ (0.5B, 2026-09-11)

Обучение доменной модели через траектории харнесса (по образцу Agent Lightning /
Harvey×Baseten GRPO-in-harness) на архитектурных задачах, а не SWE.

**Харнесс-сторона уже даёт нужное:**
- событийный журнал сессий (`sessions/*.jsonl`) — траектории как данные;
- рубрики (`src/rubric.rs`, golden-калибровка) — reward-сигнал;
- fitness-правила + `arch-ml control check` — верифицируемый гейт.

**Проверено на стенде (vast.ai 4090 24GB, Qwen2.5-0.5B, ручной GRPO `benchmarks/spine-vs-claude/rl/grpo_manual.py`):**
- петля end-to-end работает: rollout → fitness-reward → advantage → градиент → update, без OOM/ошибок;
- разреженный reward (доля точных regex-паттернов) на 0.5B вырожден: advantage≈0, градиент нулевой;
- soft-score (0.5·exact + 0.5·structural) + `n=8` + `temp=1.5` → ненулевая std_group, живой loss. Диагноз и фикс подтверждены.
- **Обучение на 0.5B НЕ доказано** (7/24 шагов, LR 1e-5, mean≈0.11–0.15 без устойчивого тренда). Это демо осуществимости, не результат.

**Что нужно для реального обучения (вне кода харнесса):** veRL/vLLM-стенд (GPU ≥40GB),
модель 3B+, больше шагов и rollouts, дешёвый прокси endpoint (по Agent Lightning),
верифицируемая среда архитектурных задач. Рецепты уже в памяти/библиотеке (veRL-0.4.0, GRPO, multi-turn).

## Итог H2

- **Реализовано кодом:** H2.2 (evolve), H2.3 (self-docs).
- **Уже есть, задокументировано:** H2.4 (bench/eval).
- **Петля проверена на стенде (0.5B), реальное обучение — нужен стенд ≥40GB:** H2.1.
