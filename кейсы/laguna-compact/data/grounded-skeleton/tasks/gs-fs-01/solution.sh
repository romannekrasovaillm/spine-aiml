#!/bin/sh
# Эталонное действие задачи gs-fs-01: собрать service.json из трёх источников.
# Запускается раннером внутри контейнера с CWD=/work.
set -e
mkdir -p app/config
name=$(cat src/name.txt)
port=$(cat src/port.txt)
f1=$(sed -n 1p src/flags.txt)
f2=$(sed -n 2p src/flags.txt)
f3=$(sed -n 3p src/flags.txt)
printf '{"name":"%s","port":%s,"flags":["%s","%s","%s"]}\n' "$name" "$port" "$f1" "$f2" "$f3" > app/config/service.json
