#!/bin/sh
# Эталонное действие задачи gs-fs-02: разложить inbox по адресатам из первой строки.
# Запускается раннером внутри контейнера с CWD=/work.
set -e
mkdir -p sorted
for f in inbox/*; do
  tenant=$(awk 'NR == 1 { print $2 }' "$f")
  mkdir -p "sorted/$tenant"
  mv "$f" "sorted/$tenant/"
done
