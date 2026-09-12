#!/usr/bin/env bash
# run_regression.sh — регрессионный прогон Platform V arch-bench после
# изменения продуктовых фич Spine (сборка arch-be из текущего worktree).
#
# Прогоняет быстрый поднабор (по умолчанию: 2 задачи × spine-arch × dsf × 1
# повтор), судит и сравнивает с базовой линией results/baseline_summary.json.
# Exit 1 = регрессия (падение среднего total spine-arch больше порога).
#
# Переменные:
#   PVBENCH_TASKS       — задачи через пробел (умолч. "PGL-ARCH-001 CRX-ARCH-001")
#   PVBENCH_CONDITIONS  — условия (умолч. "spine-arch")
#   PVBENCH_MODELS      — модели (умолч. "dsf")
#   PVBENCH_REPS        — повторы (умолч. 1)
#   PVBENCH_JUDGE_K     — прогонов судьи на ячейку (умолч. 1)
#   PVBENCH_THRESHOLD   — порог регрессии в баллах (умолч. 5)
set -euo pipefail
BENCH="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
export PVBENCH_RUNS="${PVBENCH_RUNS:-$BENCH/runs/regression-$TS}"
export PVBENCH_TASKS="${PVBENCH_TASKS:-PGL-ARCH-001 CRX-ARCH-001}"
export PVBENCH_CONDITIONS="${PVBENCH_CONDITIONS:-spine-arch}"
export PVBENCH_MODELS="${PVBENCH_MODELS:-dsf}"
export PVBENCH_REPS="${PVBENCH_REPS:-1}"
export PVBENCH_JUDGE_K="${PVBENCH_JUDGE_K:-1}"
export PVBENCH_NO_SWEEP=1

echo "== регрессионный прогон: $PVBENCH_RUNS"
python3 "$BENCH/runners/prepare_cells.py"
python3 "$BENCH/runners/run_matrix.py"
python3 "$BENCH/runners/mech_score.py"
python3 "$BENCH/runners/judge.py"
python3 "$BENCH/runners/analyze.py"

BASELINE="$BENCH/results/baseline_summary.json"
if [ ! -f "$BASELINE" ]; then
    echo "== базовой линии нет, сохраняю текущий прогон как baseline"
    mkdir -p "$BENCH/results"
    cp "$PVBENCH_RUNS/summary.json" "$BASELINE"
    echo "== baseline записан: $BASELINE"
    exit 0
fi
python3 "$BENCH/runners/compare_runs.py" "$BASELINE" \
    "$PVBENCH_RUNS/summary.json" --threshold "${PVBENCH_THRESHOLD:-5}"
