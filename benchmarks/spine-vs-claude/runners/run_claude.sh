#!/usr/bin/env bash
# Тонкая обёртка: одна ячейка руки Claude Code (headless). cwd = <CELL>/work.
# MCP-конфиг подключается автоматически, если в work/ есть .mcp.arch.json
# (рука claude-mcp); иначе это claude-plain / claude-arch.
#
# Эквивалент строки раннера:
#   claude -p "$(cat ../prompt.txt)" --model {model} --output-format text \
#          --dangerously-skip-permissions [--mcp-config .mcp.arch.json]
#
# Использование: runners/run_claude.sh <CELL-DIR> [MODEL]
#   MODEL не задан -> берётся из <CELL-DIR>/meta.json
# Переменные: SVC_CLAUDE (путь к claude).
set -euo pipefail

cell=${1:?укажите каталог ячейки: runners/run_claude.sh <CELL-DIR> [MODEL]}
model=${2:-$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["model"])' "$cell/meta.json")}
claude=${SVC_CLAUDE:-claude}

cd "$cell/work"
mcp_args=()
if [ -f .mcp.arch.json ]; then
    mcp_args=(--mcp-config "$PWD/.mcp.arch.json")
fi

"$claude" -p "$(cat ../prompt.txt)" --model "$model" \
    --output-format text --dangerously-skip-permissions "${mcp_args[@]}" > ../answer.md
