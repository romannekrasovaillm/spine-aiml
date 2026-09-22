#!/bin/sh
# Собирает сумму столбца value по всем data/*.csv в out/aggregate.txt
# Запуск: из каталога /work (sh bin/aggregate.sh)
set -e
mkdir -p out
awk -F, 'FNR > 1 { s += $1 } END { print s }' data/*.csv > out/aggregate.txt
