#!/usr/bin/env bash
# point_agentic_probe.sh — агентная проба приборной цепочки SFT-точки (шаг 5 спеки
# `docs/specs/SFT-POINT-CHAIN.md`, критерий ADR-033 п.2в).
#
# **Почему отдельный раннер, а не строка в отчёте.** Проба длинная (≈45 мин на
# прогон), запускается отсоединённо, и её вход — предмет, который обязан быть
# **забронирован** (ADR-058): чекпойнт берётся из каталога цепочки (`ckpt/`), а не
# из живого прогона. Строка, набранная руками, теряет именно это — и тогда «проба
# не сошлась с бронью» обнаруживается отчётом через час, а не отказом на старте.
#
# **Почему база переснимается здесь же** (`--stage cpt`). Критерий (в) читается
# как «доля не падает относительно базы». База 45,1 % (158/350,
# `evidence/s3aa-agentic-cpt.json`) снята в **legacy-режиме** — без запрета
# повторов 4-грамм, — и ADR-041 п.3 объявляет её исторической: «до переснятия базы
# вердикт по agentic-доле не выносится — сравнение было бы между разными
# режимами». Значит, у шага 5 два предмета: SFT-финал (что спрашивают) и его
# **входной CPT-чекпойнт** (с чем сравнивать). Оба — в штатном режиме, на одном
# приборе и одном устройстве; иначе меряется смена режима, а не стадии.
#
# **Штатный режим — не деталь вызова.** `--no-toolcall-force --no-hint` обязательны
# (ADR-033 п.2, вставка 17.09.2026): при `toolcall-force` первый вызов вкладывает
# харнесс, при `hint` подставляется открывающий тег, и число описывает харнесс, а
# не модель. Запрет повторов 4-грамм (ADR-041) — умолчание прибора, здесь не
# переопределяется: снять его можно только `--legacy-decoding`, и этот флаг в
# раннере не предусмотрен вовсе (мост воспроизводимости — не режим выводов).
#
# Порядок: только последовательно (AD-5 — один GPU-замер за раз, и на локальной
# 4080 тоже: прогон на 16 ГБ не делится с чужой нагрузкой флота).
#
# Отсоединённый запуск (штатный путь; `--stage both` идёт sft → cpt):
#
#   setsid nohup bash tools/point_agentic_probe.sh --ts 20260925-1000 --stage both \
#       >> runs/passrate-agentic-20260925-1000/chain.log 2>&1 < /dev/null &
#
# Коды: 0 — проба снята (или уже была снята); 1 — отказ прибора; 2 — нет входа.

set -uo pipefail

CASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$CASE_ROOT" || exit 2

#: Интерпретатор называется **явно**: у `python3` из PATH на этой машине
#: (miniconda, 3.11) сломан transformers (`huggingface-hub==1.27.0` вместо
#: `<1.0`), и проба падает ImportError за 3 с — с распиской `payload_failed`, то
#: есть «отказ, а не замер», но с диагнозом, который надо читать глазами. Тот же
#: интерпретатор, что у приборов критерия (`LOCAL_IMAGE`: `/usr/bin/python3.12`,
#: torch 2.5.1+cu121, transformers 4.44.2) — иначе числа шли бы с другого ряда.
PY=/usr/bin/python3.12

TS=""
STAGE=""
PER_TYPE=50
MAX_TURNS=5
BATCH=32
MAX_NEW_TOKENS=4096
SEED=42
MIN_FREE_MIB=2000

CHAIN_CKPT_DIR="/home/user/gb10-shared/sft-point-chain-${TS}/ckpt"
CPT_CKPT="/home/user/gb10-shared/full-cpt-20260916-2149/checkpoints/checkpoint_final.pt"
CPT_CKPT_SHA="080c3ab6523f44378c87f443e4d10fb84e060f5c4ae3825e721aad066b54e086"
POOL_V1="datasets/rl_tasks_revpool_v1.jsonl"
POOL_V2="datasets/rl_tasks_revpool_v2.jsonl"
POOL_V1_SHA="06b95b2f3b62d2f8b24a458ca49ff13232b9fc7a00c5390e4354d6b2956a5eb3"
POOL_V2_SHA="e678eb680d5abba0825f81d3e84cda3f680a51850f683fa6f1a3aa22de91d4da"

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ts) TS="${2:?}"; shift 2 ;;
    --stage) STAGE="${2:?}"; shift 2 ;;
    --per-type) PER_TYPE="${2:?}"; shift 2 ;;
    *) echo "неизвестный флаг: $1" >&2; exit 2 ;;
  esac
done
[ -n "$TS" ] && [ -n "$STAGE" ] || { echo "--ts и --stage обязательны" >&2; exit 2; }
CHAIN_CKPT_DIR="/home/user/gb10-shared/sft-point-chain-${TS}/ckpt"

#: Каталог прогона — **плоский сосед** в `runs/`, а не подкаталог общего контейнера
#: (урок этой дельты): гейт AD-2 читает **один** уровень вложенности
#: (`tools/check_run_manifest.py:187` — `runs.iterdir()`), поэтому два прогона,
#: сложенные в один контейнер, оставляют его без манифеста — и либо находка C-012,
#: либо маркер `NOT_A_RUN`, прячущий прогон за маркером. Плоские имена дают каждому
#: прогону свой манифест там, где гейт его ищет (прецедент — `passrate-agentic-cpt-
#: 20260917-0153`).

#: Выход прогона — там же, где сырые записи: сводка пересобирается из них без GPU
#: (`--summarize`), поэтому каталог прогона и evidence-файл обязаны жить вместе.
run_one() {  # $1 — sft|cpt ; печатает путь к evidence
  local kind="$1" ckpt run_dir evidence want_sha free
  case "$kind" in
    sft)
      ckpt="$CHAIN_CKPT_DIR/sft_checkpoint_final.pt"
      #: sha берётся из брони, а не из головы: проба обязана мерить **тот** предмет,
      #: который забронирован (SFT-POINT-CHAIN §6 — «манифест/проба указывают на
      #: другой чекпойнт, чем бронь» это стоп-условие, а не предупреждение).
      want_sha="$($PY -c "import json,sys;print(json.load(open(sys.argv[1]))['sha256'])" \
                  "$CHAIN_CKPT_DIR/RECEIPT.json" 2>/dev/null)"
      run_dir="runs/passrate-agentic-sft-${TS}"
      evidence="evidence/s3aa-agentic-sft.json"
      ;;
    cpt)
      ckpt="$CPT_CKPT"; want_sha="$CPT_CKPT_SHA"
      run_dir="runs/passrate-agentic-cpt-nogram4-${TS}"
      evidence="evidence/s3aa-agentic-cpt-nogram4.json"
      ;;
    *) echo "неизвестный предмет: $kind" >&2; return 2 ;;
  esac
  [ -f "$evidence" ] && { log "$kind: evidence уже на месте ($evidence) — пропуск"; echo "$evidence"; return 0; }
  if [ ! -r "$ckpt" ]; then log "NOT-VERIFIED $kind: чекпойнт не читается: $ckpt"; return 2; fi
  mkdir -p "$run_dir"
  free="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1 | tr -d ' ')"
  if [ -n "$free" ] && [ "$free" -lt "$MIN_FREE_MIB" ]; then
    log "NOT-VERIFIED $kind: свободной памяти карты ${free} МиБ < ${MIN_FREE_MIB} — чужая нагрузка флота"
    return 2
  fi
  log "$kind: проба на $ckpt (sha ${want_sha:0:12}…), режим штатный (nogram4, no-toolcall-force, no-hint)"
  # guard_cuda_run.sh: запрет сна вокруг нагрузки + расписка с состоянием устройства
  # до/после и счётчиком Xid (C-029). Нагрузка без запрета не стартует вовсе.
  bash tools/guard_cuda_run.sh \
      --out "$run_dir/guard.json" \
      --why "agentic probe: S3 during a CUDA run kills the context (Xid 31)" \
      --probe-log "$run_dir/probe.log" \
      -- "$PY" tools/passrate_probe.py \
        --checkpoint "$ckpt" \
        --pool "v1=$POOL_V1" --pool "v2=$POOL_V2" \
        --expect-pool-sha256 "v1=$POOL_V1_SHA,v2=$POOL_V2_SHA" \
        --run-dir "$run_dir" \
        --evidence "$evidence" \
        --per-type "$PER_TYPE" --seed "$SEED" --batch "$BATCH" \
        --max-turns "$MAX_TURNS" --max-new-tokens "$MAX_NEW_TOKENS" \
        --temperature 1.0 --top-k 20 \
        --no-toolcall-force --no-hint \
        --index datasets/concepts_search_index.jsonl \
        --image "local-rtx4080s: python3 + torch + transformers (без контейнера)" \
        --evidence-note "предмет: $( [ "$kind" = sft ] && echo 'SFT-финал приборной цепочки, бронь ckpt/' || echo 'CPT-финал — входной чекпойнт SFT (переснятие базы ADR-041 п.3)' )" \
        > "$run_dir/probe.log" 2>&1
  local rc=$?
  if [ "$rc" != "0" ]; then
    log "ОТКАЗ $kind: прибор вернул rc=$rc (вердикт расписки — $run_dir/guard.json)"
    tail -20 "$run_dir/probe.log" | sed 's/^/    | /'
    return 1
  fi
  log "$kind: проба снята → $evidence"
  echo "$evidence"
  return 0
}

rc=0
case "$STAGE" in
  sft|cpt) run_one "$STAGE" >/dev/null || rc=$? ;;
  both)
    run_one sft >/dev/null || rc=$?
    if [ "$rc" = "0" ]; then run_one cpt >/dev/null || rc=$?; fi
    ;;
  *) echo "--stage: sft|cpt|both" >&2; exit 2 ;;
esac
log "итог стадии $STAGE: rc=$rc"
exit "$rc"
