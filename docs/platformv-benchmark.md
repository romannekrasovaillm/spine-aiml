# Platform V Arch-Bench — бенчмарк архитектурных задач

`benchmarks/platformv-arch-bench/` — бенчмарк из 24 архитектурных задач по
документации Platform V (СберТех) с рубриками, машиночитаемыми проверками и
LLM-судьёй. Источник истины — монорепозиторий Spine.

Три назначения:

1. **Выбор модели** — матрица условие × модель; разрез `spine-arch × модель`
   показывает, какая модель даёт Spine лучший архитектурный результат
   (`total_by_condition_model` в summary.json).
2. **Регрессии продуктовых фич** — `scripts/run_regression.sh` прогоняет
   быстрый поднабор на текущей сборке `arch-ml` и сравнивает с
   `results/baseline_summary.json`; падение среднего total условия
   `spine-arch` больше порога (по умолчанию 5 баллов) — exit 1.
3. **Выбор кодового харнесса для handoff** — сравнение `theseus-*`,
   `claude-*` и свипа (dsh, codewhale, hermes, openclaw, kimi) на общих
   задачах и моделях; эффекты с bootstrap CI в `effects` summary.json.

Отношение к встроенному `arch-ml bench`: встроенные бенчмарки
(`assets/benchmarks/*.yaml`) — одиночные быстрые проверки «задача → рубрика»
внутри бинаря; platformv-arch-bench — внешний многохарнессный конвейер с
пререгистрацией, повторами и статистическим анализом. Оба используют одни
рубрики (`assets/rubrics/`) и одного evidence-судью.

Полное описание: `benchmarks/platformv-arch-bench/README.md`,
пререгистрация — `benchmarks/platformv-arch-bench/PREREGISTRATION.md`.
