#!/usr/bin/env bash
# Прогон тестов Spine-BE SDK v1 (Java): сборка + компиляция тестов + TestRunner.
# Живые интеграционные тесты используют SPINE_BE_BIN или ../../target/release/arch-ml;
# без бинаря — skip с предупреждением.
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

rm -rf out-test
mkdir -p out-test
javac -encoding UTF-8 -cp out -d out-test $(find test -name '*.java' | sort)

java -cp "out:out-test" bank.spine.sdk.TestRunner
