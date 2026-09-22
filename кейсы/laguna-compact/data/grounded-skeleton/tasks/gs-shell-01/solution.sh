#!/bin/sh
# Эталонное действие задачи gs-shell-01 (положительный контроль, не ответ).
# Запускается раннером внутри контейнера с CWD=/work.
set -e
mkdir -p out
awk -F, 'NR > 1 { s += $2 } END { print s }' in/series.csv > out/total.txt
