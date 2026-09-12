# Platform V Arch-Bench — архитектурный бенчмарк по документации Platform V (СберТех)

Бенчмарк для оценки агентных харнессов и моделей на задачах архитектора
решений в банковском домене: 24 задачи по документации Platform V
(Pangolin, Corax, SynGX, Synapse, Radish, DataMarts, Ocean DB, Architect Hub,
комплаенс-маппинг 719-П/683-П/851-П и сквозные сценарии DR/ёмкости/ИБ).

Источник истины — этот каталог монорепозитория Spine
(`benchmarks/platformv-arch-bench/`). Экспортный снапшот для отдельного
репозитория: `scripts/export_standalone.sh`.

## Состав

- `tasks/<TASK-ID>/{TASK,CONTEXT,RUBRICS}.md` — постановка, исходные данные
  (факты `[DOC]` со ссылками на источники, вымышленный сценарий `[BENCH]`),
  рубрики (критерии 0–4, веса = 100, hard-fail правила, машиночитаемый блок
  `mechanical_checks`).
- `runners/` — конвейер (только stdlib Python 3):
  `prepare_cells.py` (генерация ячеек матрицы и спайн-пакетов) →
  `run_matrix.py` (прогон харнессов, идемпотентно) →
  `mech_score.py` (детерминированный слой: deliverables, HF-regex, объём) →
  `judge.py` (LLM-судья с цитатами-доказательствами, k прогонов) →
  `analyze.py` (summary.json, bootstrap CI) →
  `compare_runs.py` (сравнение двух прогонов, регрессионный гейт).
- `spine/<TASK>/` — спайн-пакеты (ARCHITECTURE-SPINE.md + CONSTRAINTS.yaml),
  генерируются из рубрик задач; используются условием `spine-arch`.
- `customization/architect.md` — кастомизация «кодовый агент → архитектор»
  (AGENTS.md/CLAUDE.md) для условий `*-arch`.
- `results/` — сводные результаты прогонов (в git); тяжёлые ячейки
  (`runs/`) в git не входят.
- `PREREGISTRATION.md` — зафиксированный до прогонов дизайн, гипотезы,
  хэши входов.

## Матрица

Условия (харнессы): `spine-arch` (arch-be + спайн-пакет + fitness-гейт),
`spine-min` (arch-be без спайн-пакета), `theseus-plain` / `theseus-arch`,
`claude-plain` / `claude-arch`, `raw-llm` (голый API-вызов, базовая линия).
Модели: `dsf` (deepseek-flash), `glm` (glm-5.3-flash) — полная матрица;
`glm53` (glm-5.3) и `dsp` (deepseek-v4-pro) — ключевые условия.
Повторы: dsf/glm ×2, glm53/dsp ×1. Расширенный свип: dsh, codewhale,
hermes, openclaw, kimi на 2 задачах.

## Три сценария использования

### 1. Выбор модели для Spine

```bash
cd benchmarks/platformv-arch-bench
PVBENCH_MODELS="dsf glm glm53 dsp" PVBENCH_CONDITIONS="spine-arch" \
  python3 runners/prepare_cells.py && python3 runners/run_matrix.py
python3 runners/mech_score.py && python3 runners/judge.py && python3 runners/analyze.py
# таблица total_by_condition_model — разрез «spine-arch × модель»
```

### 2. Регрессии после изменения продуктовых фич Spine

```bash
# однократно — зафиксировать базовую линию (первый запуск сам станет baseline):
benchmarks/platformv-arch-bench/scripts/run_regression.sh
# после изменения фич (сборка arch-be из текущего worktree):
benchmarks/platformv-arch-bench/scripts/run_regression.sh   # exit 1 = регрессия
```

Порог по умолчанию 5 баллов (PVBENCH_THRESHOLD); поднабор задаётся
PVBENCH_TASKS / PVBENCH_CONDITIONS / PVBENCH_MODELS / PVBENCH_REPS.

### 3. Выбор кодового харнесса для хэндофф-пакетов Spine

Сравнение условий `theseus-*`, `claude-*` и свипа (dsh, codewhale, hermes,
openclaw, kimi) на одних и тех же задачах и моделях показывает, какому
кодовому харнессу банк может отдавать handoff-пакеты Spine с наименьшей
потерей качества. Сводка — `total_by_condition_model` и `effects` в
summary.json (bootstrap 95% CI).

## Требования окружения

- бинари харнессов в PATH: `arch-be` (обязателен), `theseus`, `claude`,
  опционально `dsh`, `codewhale`, `hermes`, `openclaw`, `kimi`
  (путь к kimi — KIMI_BIN);
- ключи только из окружения: `DEEPSEEK_API_KEY`, `ZHIPU_API_KEY`;
- локальные шлюзы: llm-proxy `http://127.0.0.1:8787` (glm/deepseek-pro),
  claude-прокси `:8765`;
- `PVBENCH_RUNS` — каталог прогонов (по умолчанию `runs/` рядом с
  бенчмарком; в git не входит);
- `PV_DOCS` — сырой слепок документации Platform V (используется авторами
  задач, в репозиторий не входит; ссылки `<PV_DOCS>` в CONTEXT.md).

## Методология

См. `PREREGISTRATION.md` (гипотезы H1–H3, судья, отклонения) и
`docs/methodology.md`. Формула итога задачи:
`total = 100·Σ(wᵢ·sᵢ)/(4·Σwᵢ)`, при любом hard-fail — `min(total, 39)`.
Пороги: pass ≥ 70, excellent ≥ 85 (рубрики задач).
