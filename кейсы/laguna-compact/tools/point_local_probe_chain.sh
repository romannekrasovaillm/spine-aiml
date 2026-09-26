#!/usr/bin/env bash
# point_local_probe_chain.sh — приборная очередь **локальной 4080** для цепочки
# SFT-точки (шаги 4 и 6 спеки `docs/specs/SFT-POINT-CHAIN.md`).
#
# **Зачем очередь, а не два отдельных запуска.** На 4080 действует то же правило,
# что на стенде (AD-5, SFT-POINT-CHAIN §4.1): один GPU-замер за раз. Два
# `nohup`-прогона, запущенных «рядом», делят 16 ГБ карты, и оба становятся
# непригодны — а по логам это выглядит как отказ прибора, а не как нарушение
# порядка. Очередь делает порядок структурой, а не обещанием.
#
# Состав и цена (измерено ранее, S3aq/S3aa):
#   1. `point_agentic_probe.sh --stage both` — SFT-финал, затем входной CPT-чекпойнт
#      (переснятие agentic-базы, ADR-041 п.3): ≈45 мин на прогон, две штуки;
#   2. `point_format_probe.sh` — три состояния (base, sft, cfinal) на бюджете 8192:
#      ≈2 ч на состояние.
# Итого ≈7–8 ч; последовательность сохраняется при обрыве на любой точке — повторный
# запуск продолжает с первой неготовой (приборы пропускают снятое).
#
# Запуск (отсоединённо):
#
#   setsid nohup bash tools/point_local_probe_chain.sh --ts 20260925-1000 \
#       >> runs/sft-point-chain-20260925-1000/local-chain.log 2>&1 < /dev/null &
#
# Коды: 0 — все стадии сняты; 1 — отказ прибора на стадии (очередь остановлена,
# следующие не стартуют: отказ не размножается); 2 — нет входа.

set -uo pipefail
CASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$CASE_ROOT" || exit 2

TS=""
SKIP_AGENTIC=0
SKIP_FORMAT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ts) TS="${2:?}"; shift 2 ;;
    --skip-agentic) SKIP_AGENTIC=1; shift ;;
    --skip-format) SKIP_FORMAT=1; shift ;;
    *) echo "неизвестный флаг: $1" >&2; exit 2 ;;
  esac
done
[ -n "$TS" ] || { echo "--ts обязателен" >&2; exit 2; }

OUT_DIR="runs/sft-point-chain-${TS}"
mkdir -p "$OUT_DIR"
log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }

rc=0
if [ "$SKIP_AGENTIC" = "0" ]; then
  log "=== 1/2 агентная проба (SFT-финал, затем входной CPT — переснятие базы) ==="
  bash tools/point_agentic_probe.sh --ts "$TS" --stage both
  rc=$?
  log "агентная проба: rc=$rc"
  [ "$rc" = "0" ] || { log "очередь остановлена на агентной пробе"; exit "$rc"; }
fi

if [ "$SKIP_FORMAT" = "0" ]; then
  log "=== 2/2 форматная проба (wide, бюджет 8192, три состояния) ==="
  bash tools/point_format_probe.sh --ts "$TS"
  rc=$?
  log "форматная проба: rc=$rc"
fi
log "очередь завершена: rc=$rc"
exit "$rc"
