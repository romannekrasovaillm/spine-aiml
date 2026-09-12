#!/usr/bin/env bash
# Тонкая обёртка: одна ячейка руки Spine (headless). cwd = <CELL>/work.
# Эквивалент строки раннера: `arch-ml run -q --model {model} --timeout 1100 "$(cat ../prompt.txt)"`.
#
# Использование: runners/run_spine.sh <CELL-DIR> [MODEL]
#   MODEL не задан -> берётся из <CELL-DIR>/meta.json
# Переменные: SVC_ARCH (путь к arch-ml), SVC_ARM_TIMEOUT (секунды).
set -euo pipefail

cell=${1:?укажите каталог ячейки: runners/run_spine.sh <CELL-DIR> [MODEL]}
model=${2:-$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["model"])' "$cell/meta.json")}
arch=${SVC_ARCH:-arch-ml}

cd "$cell/work"
"$arch" run -q --model "$model" --timeout "${SVC_ARM_TIMEOUT:-1100}" \
    "$(cat ../prompt.txt)" > ../answer.md
