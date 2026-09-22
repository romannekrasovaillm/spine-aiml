#!/bin/sh
# Эталонное действие задачи gs-http-01: получить выборку и отправить принятый отчёт.
# Запускается раннером внутри контейнера с CWD=/work; MOCK_BASE приходит из окружения.
set -e
mkdir -p out
wget -q -O out/items.json "$MOCK_BASE/v1/items"
count=$(grep -o '"id"' out/items.json | wc -l | tr -d ' ')
total=$(grep -o '"qty": *[0-9]*' out/items.json | sed 's/.*:[ ]*//' | awk '{ s += $1 } END { print s }')
wget -q -O out/report.json \
  --header 'Content-Type: application/json' \
  --post-data "{\"items_count\":$count,\"total_qty\":$total}" \
  "$MOCK_BASE/v1/report"
cat out/report.json
