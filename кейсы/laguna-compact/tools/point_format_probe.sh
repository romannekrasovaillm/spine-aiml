#!/usr/bin/env bash
# point_format_probe.sh — форматная проба приборной цепочки SFT-точки (шаг 6 спеки
# `docs/specs/SFT-POINT-CHAIN.md`, ADR-042 п.5).
#
# Три состояния в **одном процессе**, а не три прогона: форматная метрика —
# свойство генерации, и склейка чисел из разных процессов мерила бы ещё и разницу
# запусков. Один процесс = один загруженный контекст CUDA, один прибор, один
# протокол — ровно то, на чём S3aq держал сопоставимость:
#
#   * `base`   — нетронутая Qwen2.5-0.5B: точка «откуда язык и формат пришли»;
#   * `sft`    — SFT-финал приборной цепочки (предмет брони, ADR-058);
#   * `cfinal` — входной CPT-чекпойнт, то есть **прежний вход** стадии: относительно
#                него читается критерий ADR-042 п.5 («покрытие ответа не падает»).
#
# Порядок `sft` раньше `cfinal` намеренно: отчёт пишется **инкрементально**, и при
# обрыве длинного прогона предметный вопрос («что стало с форматом на финале»)
# должен быть уже отвечен, а не ждать ориентира.
#
# Протокол — ровно тот, что объявлен в ADR-045 п.4 как решающий: набор `wide`
# (104 пробы), бюджет **8192**, остановка на конце хода, штатный запрет повторов
# 4-грамм (умолчание прибора; `--legacy-decoding` здесь не предусмотрен вовсе —
# мост воспроизводимости не режим выводов), greedy, пакет 8, сид 42.
#
# Запуск (отсоединённо; на локальной 4080, AD-9: стенд не занимается):
#
#   setsid nohup bash tools/point_format_probe.sh --ts 20260925-1000 \
#       >> runs/sft-point-chain-20260925-1000/format.log 2>&1 < /dev/null &
#
# Коды: 0 — отчёт снят; 1 — отказ прибора; 2 — нет входа.

set -uo pipefail

CASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$CASE_ROOT" || exit 2

TS=""
MAX_NEW_TOKENS=8192
BATCH=8
MIN_FREE_GB=4
while [ $# -gt 0 ]; do
  case "$1" in
    --ts) TS="${2:?}"; shift 2 ;;
    *) echo "неизвестный флаг: $1" >&2; exit 2 ;;
  esac
done
[ -n "$TS" ] || { echo "--ts обязателен" >&2; exit 2; }

CHAIN="/home/user/gb10-shared/sft-point-chain-${TS}"
OUT_DIR="runs/sft-point-chain-${TS}"
OUT="$OUT_DIR/format-wide-${MAX_NEW_TOKENS}.json"

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }

if [ -f "$OUT" ]; then
  # Отчёт пишется инкрементально, поэтому «файл есть» ещё не «проба снята»: полным
  # считается отчёт, где у всех трёх состояний есть aggregate с n проб.
  done_states="$(/usr/bin/python3.12 - "$OUT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
need = ("base", "sft", "cfinal")
got = [t for t in need if (d.get("states", {}).get(t, {}).get("aggregate", {}).get("n"))]
print(",".join(got))
PY
)"
  log "существующий отчёт: состояния с числами = [${done_states:-нет}]"
  if [ "$done_states" = "base,sft,cfinal" ]; then
    log "проба уже снята полностью — пропуск"
    exit 0
  fi
  # Неполный отчёт — свидетельство, а не мусор: прибор пишет инкрементально, и
  # снятые состояния (с их байтами ответов) дороже повторного прогона. Сдвигаем в
  # сторону и начинаем заново, а не затираем: «сколько успели» — часть результата.
  keep="$OUT_DIR/format-wide-${MAX_NEW_TOKENS}.partial-$(date +%Y%m%d-%H%M).json"
  log "отчёт неполон — сохраняю как $keep и начинаю заново"
  mv "$OUT" "$keep"
fi

for s in "$CHAIN/ckpt/sft_checkpoint_final.pt" "$CHAIN/ckpt/cpt_checkpoint_final.pt"; do
  [ -r "$s" ] || { log "NOT-VERIFIED: нет предмета $s (бронь не встала?)"; exit 2; }
done
mkdir -p "$OUT_DIR"
free="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1 | tr -d ' ')"
if [ -n "$free" ] && [ "$free" -lt $((MIN_FREE_GB * 1024)) ]; then
  log "NOT-VERIFIED: свободной памяти карты ${free} МиБ < ${MIN_FREE_GB} ГиБ — карта занята"
  exit 2
fi

log "проба: широкий набор, бюджет $MAX_NEW_TOKENS, конец хода, nogram4 (штатный режим)"
bash tools/guard_cuda_run.sh \
  --out "$OUT_DIR/guard-format.json" \
  --why "format probe SFT point chain: S3 during a CUDA run kills the context (Xid 31)" \
  --probe-log "$OUT_DIR/format.log" \
  -- /usr/bin/python3.12 tools/probe_language_split.py \
     --prompts wide --max-new-tokens "$MAX_NEW_TOKENS" --stop-at-turn-end \
     --batch-size "$BATCH" --sha \
     --base \
     --ckpt "sft=$CHAIN/ckpt/sft_checkpoint_final.pt" \
     --ckpt "cfinal=$CHAIN/ckpt/cpt_checkpoint_final.pt" \
     --out "$OUT" \
     > "$OUT_DIR/format.log" 2>&1
rc=$?
log "код прибора: $rc; отчёт: $OUT"
[ "$rc" = "0" ] || { tail -20 "$OUT_DIR/format.log" | sed 's/^/    | /'; exit 1; }
exit 0
