#!/usr/bin/env bash
# export_standalone.sh — экспорт бенчмарка в отдельный каталог (для
# самостоятельного репозитория с результатами сравнения Spine с другими
# кодовыми агентами).
#
# Использование: export_standalone.sh <целевой_каталог>
# Копирует задачи, раннеры, спайн-пакеты, кастомизации, документацию и
# СВОДНЫЕ результаты (results/). Тяжёлые ячейки (runs/) не копируются.
# Источник истины — монорепо Spine (benchmarks/platformv-arch-bench);
# экспорт — снапшот для внешней публикации/демонстрации.
set -euo pipefail
BENCH="$(cd "$(dirname "$0")/.." && pwd)"
DST="${1:?использование: export_standalone.sh <целевой_каталог>}"
mkdir -p "$DST"
for d in tasks runners spine customization scripts results docs; do
    [ -e "$BENCH/$d" ] && cp -r "$BENCH/$d" "$DST/"
done
cp "$BENCH/README.md" "$BENCH/PREREGISTRATION.md" "$DST/" 2>/dev/null || true
rm -rf "$DST/runners/__pycache__" "$DST/runs"
cat > "$DST/SOURCE.md" <<EOF
# Источник истины

Этот каталог — экспортный снапшот бенчмарка. Источник истины и разработка:
монорепозиторий Spine, каталог \`benchmarks/platformv-arch-bench/\`.
Дата экспорта: $(date -Iseconds)
EOF
echo "== экспортировано в $DST"
find "$DST" -type f | wc -l
