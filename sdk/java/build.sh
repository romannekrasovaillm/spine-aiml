#!/usr/bin/env bash
# Сборка Spine-BE SDK v1 (Java): javac → out/ → sdk.jar.
# Внешних зависимостей нет — только JDK 21+.
set -euo pipefail
cd "$(dirname "$0")"

rm -rf out sdk.jar
mkdir -p out

javac -encoding UTF-8 -d out $(find src -name '*.java' | sort)
jar --create --file sdk.jar -C out .

echo "OK: собрано sdk.jar ($(du -h sdk.jar | cut -f1))"
