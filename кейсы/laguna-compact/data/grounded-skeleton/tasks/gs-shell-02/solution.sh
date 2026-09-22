#!/bin/sh
# Эталонное действие задачи gs-shell-02: починить агрегатор (столбец value, а не label).
# Запускается раннером внутри контейнера с CWD=/work.
set -e
cat > bin/aggregate.sh <<'AGG'
#!/bin/sh
# Собирает сумму столбца value по всем data/*.csv в out/aggregate.txt
# Запуск: из каталога /work (sh bin/aggregate.sh)
set -e
mkdir -p out
awk -F, 'FNR > 1 { s += $2 } END { print s }' data/*.csv > out/aggregate.txt
AGG
# Проверка эталона на месте (в контейнере, до верификатора): скрипт должен работать.
sh bin/aggregate.sh
