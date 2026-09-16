#!/usr/bin/env bash
# pilot_chain.sh — цепочка стадий пилота ревизии (S3b, ADR-013 фаза 1) на стенде GB10.
#
# Запускается **раннером кейса** (`tools/run_pilot.py`) в tmux на стенде и живёт
# там ≈3.5 суток: CPT 9776 шагов → SFT 3 эпохи → RL 500 шагов → три eval-стадии.
# Скрипт лежит в каталоге прогона и повторяется вручную той же строкой, что записана
# в `logs/chain.log`: ручной путь и автоматический — один код, а не два похожих.
#
# ЧТО ЗДЕСЬ ГЛАВНОЕ
#
# 1. **Идемпотентность по артефактам** (как в рабочем раннере лесенки):
#    стадия считается пройденной, если её конечный артефакт на месте
#    (`checkpoint_final.pt`, `sft_checkpoint_final.pt`, `rl_checkpoint_final.pt`,
#    `eval_results_*.json`). Повторный запуск цепочки подхватывает готовое и
#    продолжает с первой неготовой стадии; незавершённая стадия резюмится
#    **внутри пайплайна** с последнего `*_checkpoint_N.pt` — цепочка для этого не
#    делает ничего, кроме повторного запуска той же строки.
#
# 2. **Предусловие перед каждой стадией — страж хозяина ресурса AD-9**
#    (`check_resource_owner.sh --stage {cpt|sft|rl}`): хозяин объявлен (флаг паузы
#    либо «сторож неисполняем + нет его строк в кроне») и активного
#    `@reboot`-автозапуска лесенки нет; свободной unified-памяти ≥ порога стадии
#    (CPT 35 / SFT 50 / RL 49 ГБ, ADR-008/ADR-011); тренировочных нагрузок ≤ 1 и
#    единственная — своя (`--exp-name`). Отказ стража = **стадия не стартует**,
#    причина печатается в `logs/chain.log`. Пороги не дублируются здесь: их
#    единственный источник — сам страж, и он же печатает их в вердикте.
#    Профиль `--unreachable-not-verified` (для fitness-правила) тут **не
#    используется**: стадия, стартующая на стенде, которого не видно, зелёного
#    вердикта не получает (ADR-007 п.5).
#
# 3. **Стоп-условия** (ADR-010 п.5, SPEC §6) — страж стадии, живущий отдельным
#    процессом: он опрашивает лог стадии и `dmesg`, а не читает поток построчно,
#    потому что при wedge (известный режим GB10) поток встаёт вместе со стадией.
#      · энтропия политики < 0.5 два наблюдения подряд → стоп RL (mode collapse);
#        наблюдения — строки пайплайна на шагах, кратных 10: своей шкалы энтропии
#        цепочка не вводит (та же оговорка, что в разведке S3-pre);
#      · два NVRM/Xid-инцидента (лог + дельта `dmesg`) → стоп **и пауза**:
#        ставится маркер `var/STOPPED`, цепочка не продолжается до решения
#        владельца (маркер снимает `run_pilot.py --clear-stop`, не цепочка);
#      · NVRM 0x51 → `storm_gap.sh` (ждать окно затишья), затем **повтор стадии**;
#        немедленный retry после бури запрещён — он попадает в ту же бурю;
#      · свободной памяти меньше предохранителя (4 ГБ) → стоп: платформенные
#        контейнеры `llm-platform-*` дороже незавершённой стадии (ADR-007 п.4);
#      · стадия молчит дольше `--stall-minutes` (120) → стоп как wedge.
#    Предохранители памяти/молчания — сверх ADR, названы явно: автономный прогон
#    на 3.5 суток без детектора зависания — это ровно тот режим отказа, ради
#    которого стоп-условия и вводились.
#
# 4. **Манифест AD-2 на каждую стадию**: после каждой стадии вызывается
#    `write_run_manifest.py` (генератор кейса, доставленный в каталог прогона) и
#    переписывает `run_manifest.json` со фактическим списком стадий; при
#    завершении всех стадий добавляется `--complete` (`pipeline_complete=true`).
#    Хеш датасета — whole-file (методика AD-2; пайплайновый манифест хеширует
#    первые 1 МиБ — расхождение методик названо в S3-pre).
#
# 5. **Запуск только через `nvrm-storm/safe_start.sh`** (absorbер drop-caches) —
#    тот же путь, что у рабочего раннера лесенки. Контейнеры `llm-platform-*` не
#    трогаются; останавливается только собственный контейнер стадии.
#
# Чего цепочка НЕ делает: не снимает `@reboot`/строки крона, не создаёт флаг
# паузы, не восстанавливает сторож лесенки (R4+, решение владельца), не удаляет
# чужие чекпойнты и контейнеры, не правит данные и пайплайн (AD-4/AD-7).
#
# Коды возврата:
#   0 — все стадии цепочки пройдены (артефакты на месте)
#   1 — стадия упала (резюмируемо: повторный запуск продолжит с чекпойнта)
#   2 — предусловие не выполнено: стадия не стартовала (страж AD-9 отказал либо
#       отсутствует вход: GLM-ключ для eval, чекпойнт для eval-стадии)
#   3 — стоп по стоп-условию: пауза (маркер var/STOPPED), нужно решение владельца
#
# Запуск (обычно — раннером; вручную та же строка):
#   bash <RUN_DIR>/pilot_chain.sh --run-dir <RUN_DIR> \
#        --stages-file <RUN_DIR>/stages.tsv --exp-base pilot-compact-s42-<ts> [...]
#   bash <RUN_DIR>/pilot_chain.sh --run-dir ... --check-only   # только предусловия

set -uo pipefail

RUN_DIR=""
CTR_RUN_DIR=""
STAGES_FILE=""
EXP_BASE=""
CTR_PREFIX="laguna-pilot"
SHARED="/home/user/gb10-shared"
EXPERIMENTS="/home/user/experiments"
CTR_EXPERIMENTS="/workspace/experiments"
CTR_SHARED="/workspace/shared"
PIPELINE_HOST=""
PIPELINE_CTR="/workspace/shared/laguna_pipeline_v8.py"
GUARD=""
SAFE_START="/home/user/gb10-shared/nvrm-storm/safe_start.sh"
SAMPLER=""
STORM_GAP="/home/user/gb10-shared/nvrm-storm/storm_gap.sh"
MANIFEST_TOOL=""
DOCKER="docker"
IMAGE=""
GLM_ENV="/home/user/gb10-shared/.glm_env"
NVRN_LOG="/home/user/experiments/nvrm_watch.log"
RUNNER_SHA12=""
ARCH_BASE=""

MODEL="Qwen/Qwen2.5-0.5B"
SEED=42
MAX_LEN=8192
MAX_SAMPLES=50000
PEAK_LR_SCALE=0.7
RL_STEPS=500
EVAL_ITEMS=192
MEM_FRACTION=0.6
MEM_CAP=100g
ATTN=flex
CPT_GEN_EVAL_EVERY=50
RL_GEN_EVAL_EVERY=25
RESYNC_EVERY=10
VLLM_GPU_UTIL=0.32
VLLM_KV_CACHE_BYTES=8589934592
VLLM_EAGER=0

ENTROPY_FLOOR=0.5
ENTROPY_LOW_STREAK=2
NVRM_LIMIT=2
MEM_FLOOR_GB=4
POLL_SECONDS=5
MEM_POLL_SECONDS=60
STALL_MINUTES=120
STALL_SECONDS=0
STAGE_RETRIES=1
SAMPLER_SECONDS=300
CHECK_ONLY=0

usage() { sed -n '2,74p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --run-dir)            RUN_DIR="${2:?}"; shift 2 ;;
    --ctr-run-dir)        CTR_RUN_DIR="${2:?}"; shift 2 ;;
    --stages-file)        STAGES_FILE="${2:?}"; shift 2 ;;
    --exp-base)           EXP_BASE="${2:?}"; shift 2 ;;
    --ctr-prefix)         CTR_PREFIX="${2:?}"; shift 2 ;;
    --shared)             SHARED="${2:?}"; shift 2 ;;
    --experiments)        EXPERIMENTS="${2:?}"; shift 2 ;;
    --pipeline)           PIPELINE_HOST="${2:?}"; shift 2 ;;
    --pipeline-ctr)       PIPELINE_CTR="${2:?}"; shift 2 ;;
    --guard)              GUARD="${2:?}"; shift 2 ;;
    --safe-start)         SAFE_START="${2:?}"; shift 2 ;;
    --sampler)            SAMPLER="${2:?}"; shift 2 ;;
    --storm-gap)          STORM_GAP="${2:?}"; shift 2 ;;
    --manifest-tool)      MANIFEST_TOOL="${2:?}"; shift 2 ;;
    --docker)             DOCKER="${2:?}"; shift 2 ;;
    --image)              IMAGE="${2:?}"; shift 2 ;;
    --glm-env)            GLM_ENV="${2:?}"; shift 2 ;;
    --nvrm-log)           NVRN_LOG="${2:?}"; shift 2 ;;
    --runner-sha12)       RUNNER_SHA12="${2:?}"; shift 2 ;;
    --arch-base)          ARCH_BASE="${2:?}"; shift 2 ;;
    --model)              MODEL="${2:?}"; shift 2 ;;
    --seed)               SEED="${2:?}"; shift 2 ;;
    --max-len)            MAX_LEN="${2:?}"; shift 2 ;;
    --max-samples)        MAX_SAMPLES="${2:?}"; shift 2 ;;
    --peak-lr-scale)      PEAK_LR_SCALE="${2:?}"; shift 2 ;;
    --rl-steps)           RL_STEPS="${2:?}"; shift 2 ;;
    --eval-items)         EVAL_ITEMS="${2:?}"; shift 2 ;;
    --mem-fraction)       MEM_FRACTION="${2:?}"; shift 2 ;;
    --mem-cap)            MEM_CAP="${2:?}"; shift 2 ;;
    --attn)               ATTN="${2:?}"; shift 2 ;;
    --cpt-gen-eval-every) CPT_GEN_EVAL_EVERY="${2:?}"; shift 2 ;;
    --rl-gen-eval-every)  RL_GEN_EVAL_EVERY="${2:?}"; shift 2 ;;
    --resync-every)       RESYNC_EVERY="${2:?}"; shift 2 ;;
    --vllm-gpu-util)      VLLM_GPU_UTIL="${2:?}"; shift 2 ;;
    --kv-cache-bytes)     VLLM_KV_CACHE_BYTES="${2:?}"; shift 2 ;;
    --vllm-eager)         VLLM_EAGER="${2:?}"; shift 2 ;;
    --entropy-floor)      ENTROPY_FLOOR="${2:?}"; shift 2 ;;
    --entropy-low-streak) ENTROPY_LOW_STREAK="${2:?}"; shift 2 ;;
    --nvrm-limit)         NVRM_LIMIT="${2:?}"; shift 2 ;;
    --mem-floor-gb)       MEM_FLOOR_GB="${2:?}"; shift 2 ;;
    --poll-seconds)       POLL_SECONDS="${2:?}"; shift 2 ;;
    --mem-poll-seconds)   MEM_POLL_SECONDS="${2:?}"; shift 2 ;;
    --stall-minutes)      STALL_MINUTES="${2:?}"; shift 2 ;;
    --stall-seconds)      STALL_SECONDS="${2:?}"; shift 2 ;;
    --stage-retries)      STAGE_RETRIES="${2:?}"; shift 2 ;;
    --sampler-seconds)    SAMPLER_SECONDS="${2:?}"; shift 2 ;;
    --check-only)         CHECK_ONLY=1; shift ;;
    -h|--help)            usage; exit 0 ;;
    *) echo "неизвестный флаг: $1" >&2; exit 2 ;;
  esac
done

export LC_ALL=C

VAR_DIR="$RUN_DIR/var"
STATUS_DIR="$VAR_DIR/status"
STOP_MARKER="$VAR_DIR/STOPPED"
STORM_FLAG="$VAR_DIR/storm_0x51"
CHAIN_PID_FILE="$VAR_DIR/chain.pid"
CHAIN_STATUS_FILE="$VAR_DIR/chain.status"

log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
fail_precondition() { log "ПРЕДУСЛОВИЕ НЕ ВЫПОЛНЕНО: $*"; printf 'precondition_failed\n' > "$CHAIN_STATUS_FILE"; exit 2; }

[ -n "$RUN_DIR" ] || { echo "--run-dir обязателен" >&2; exit 2; }
[ -n "$STAGES_FILE" ] || { echo "--stages-file обязателен" >&2; exit 2; }
[ -n "$EXP_BASE" ] || { echo "--exp-base обязателен" >&2; exit 2; }
[ -f "$STAGES_FILE" ] || { echo "нет файла стадий: $STAGES_FILE" >&2; exit 2; }
[ -n "$GUARD" ] || GUARD="$RUN_DIR/check_resource_owner.sh"
[ -n "$MANIFEST_TOOL" ] || MANIFEST_TOOL="$RUN_DIR/write_run_manifest.py"
[ -n "$PIPELINE_HOST" ] || PIPELINE_HOST="$SHARED/laguna_pipeline_v8.py"

mkdir -p "$RUN_DIR/logs" "$RUN_DIR/checkpoints" "$STATUS_DIR" "$VAR_DIR"

HOST_LOCAL="$(hostname)"
log "стенд (локальный): $HOST_LOCAL"

# ── единственная копия цепочки на каталог прогона ────────────────────────────
# Две одновременные цепочки в одном каталоге — это два прогона одной стадии
# (ровно то, от чего защищает AD-5). Проверка по pid, а не по времени файла.
if [ -f "$CHAIN_PID_FILE" ] && kill -0 "$(cat "$CHAIN_PID_FILE" 2>/dev/null)" 2>/dev/null; then
  log "цепочка уже идёт (pid $(cat "$CHAIN_PID_FILE")) — вторая копия не запускается"
  exit 2
fi
printf '%s\n' "$$" > "$CHAIN_PID_FILE"

# ── пауза прошлого стопа ─────────────────────────────────────────────────────
# Стоп по NVRM/энтропии — это «стоп и пауза» (ADR-010 п.5): пока маркер на месте,
# цепочка не продолжается. Снимает маркер владелец, а не она сама.
if [ -f "$STOP_MARKER" ] && [ "$CHECK_ONLY" = "0" ]; then
  log "ПАУЗА: маркер $STOP_MARKER на месте — причина: $(head -1 "$STOP_MARKER")"
  log "цепочка не продолжается; снятие маркера — решение владельца (run_pilot.py --clear-stop)"
  printf 'paused\n' > "$CHAIN_STATUS_FILE"
  exit 3
fi

printf 'running\n' > "$CHAIN_STATUS_FILE"
log "== цепочка пилота $EXP_BASE =="
log "каталог прогона: $RUN_DIR (контейнер: $CTR_RUN_DIR)"
log "стадии: $STAGES_FILE; режим: $( [ "$CHECK_ONLY" = "1" ] && echo 'только предусловия (--check-only)' || echo 'полный прогон')"
log "стоп-условия: entropy<$ENTROPY_FLOOR ×$ENTROPY_LOW_STREAK, NVRM≥$NVRM_LIMIT, память<$MEM_FLOOR_GB ГБ, молчание>${STALL_MINUTES}м, 0x51 → storm_gap"

# ── состояние стадий ─────────────────────────────────────────────────────────
stage_status() {  # $1 — имя; печатает done|failed|stopped|pending (артефакт важнее статуса)
  local name="$1" artifact="$2"
  if [ -f "$RUN_DIR/$artifact" ]; then printf 'done'; return; fi
  if [ -f "$STATUS_DIR/$name" ]; then cut -f1 "$STATUS_DIR/$name"; return; fi
  printf 'pending'
}
record_stage() {  # name status artifact wall_seconds note
  printf '%s\t%s\t%s\t%s\t%s\n' "$2" "$(date -Is)" "${4:-0}" "$(sha256_artifact "$3")" "${5:-}" > "$STATUS_DIR/$1"
}
sha256_artifact() {
  local f="$RUN_DIR/$1"
  [ -f "$f" ] && sha256sum "$f" 2>/dev/null | cut -d' ' -f1 || printf '-'
}
stages_spec() {   # "cpt=done,sft=pending,..." — все стадии файла, артефакт важнее статуса
  local spec="" name pstage gstage artifact steps batch eckpt exp st
  while IFS=$'\t' read -r name pstage gstage artifact steps batch eckpt exp; do
    [ -n "${name:-}" ] || continue
    st="$(stage_status "$name" "$artifact")"
    spec="$spec,$name=$st"
  done < "$STAGES_FILE"
  printf '%s' "${spec#,}"
}
#: «Стадия пройдена» строго: артефакт есть **и** записанный статус — done.
#: Артефакт важнее статуса при пропуске (идемпотентность лесенки: чекпойнт — это
#: сделанная работа), но `pipeline_complete` требует, чтобы не было стадии с
#: записанным `partial`/`failed`: иначе частичный прогон объявил бы себя полным.
stage_done_strict() {  # $1 — имя, $2 — артефакт
  local name="$1" artifact="$2"
  [ -f "$RUN_DIR/$artifact" ] || return 1
  [ -f "$STATUS_DIR/$name" ] || return 0
  [ "$(cut -f1 "$STATUS_DIR/$name")" = "done" ] || return 1
  return 0
}
all_done() {
  local name pstage gstage artifact steps batch eckpt exp
  while IFS=$'\t' read -r name pstage gstage artifact steps batch eckpt exp; do
    [ -n "${name:-}" ] || continue
    stage_done_strict "$name" "$artifact" || return 1
  done < "$STAGES_FILE"
  return 0
}

write_manifest() {
  [ -f "$MANIFEST_TOOL" ] || { log "манифест не записан: нет $MANIFEST_TOOL"; return 0; }
  local spec complete=()
  spec="$(stages_spec)"
  all_done && complete=(--complete)
  python3 "$MANIFEST_TOOL" --run-dir "$RUN_DIR" \
    --dataset "$SHARED/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy" \
    --base-model "$MODEL" --pipeline "$PIPELINE_HOST" \
    --seed "$SEED" --image "$IMAGE" --stages "$spec" \
    --run-version "tools/run_pilot.py@${RUNNER_SHA12:-?}" \
    --dataset-extra "sft=$SHARED/datasets/sft_train_v12.jsonl" \
    --dataset-extra "rl_pool=$SHARED/datasets/rl_tasks_revpool_v1.jsonl" \
    --dataset-extra "eval=$SHARED/datasets/eval_ood_clean.jsonl" \
    --hyperparams "arch_base_version=$ARCH_BASE" \
    --hyperparams "rl_steps=$RL_STEPS" \
    --hyperparams "eval_items=$EVAL_ITEMS" \
    --hyperparams "stages_file=$(basename "$STAGES_FILE")" \
    --hyperparams-source "фактические значения запуска цепочки (run_pilot.py → pilot_chain.sh)" \
    --relative-to "$SHARED" --force ${complete[@]+"${complete[@]}"} >/dev/null 2>&1 \
    || log "ВНИМАНИЕ: манифест AD-2 не записан (rc=$?)"
}

# ── предусловие AD-9 ─────────────────────────────────────────────────────────
# Отказ стража печатается целиком: причина отказа — это вердикт, а не лог.
guard_ok() {  # $1 — стадия стража, $2 — --exp-name
  # `--host` не передаётся: цепочка всегда исполняется **на** стенде, и страж
  # определяет его локально (`nvidia-smi`), без ssh к самому себе — ssh-петля
  # зависела бы от ключей и от sshd на стенде и могла бы отказать там, где стенд
  # в порядке. Если локальное определение не сработает, страж честно уйдёт в
  # ветвь «стенд недоступен» и откажет — это fail-closed, а не «пропущено».
  # </dev/null: цикл по стадиям читает stdin из файла стадий, и любая внешняя
  # команда без этой защиты съела бы его — цепочка пропустила бы стадии молча.
  local out rc
  out="$(bash "$GUARD" --stage "$1" --exp-name "$2" --json </dev/null 2>&1)"
  rc=$?
  printf '%s\n' "$out" | sed 's/^/    | /'
  return "$rc"
}

# ── стоп-условия: страж стадии ───────────────────────────────────────────────
nvrm_xid_count() {
  #: `grep -c` печатает 0 и возвращает 1 при отсутствии совпадений: без `|| true`
  #: функция вернула бы «0\n0», а сравнение чисел молча сломалось бы.
  local n
  n="$(sudo -n dmesg 2>/dev/null </dev/null | grep -icE 'NVRM|Xid' || true)"
  printf '%s' "${n:-0}"
}
mem_available_gb() {
  awk '/^MemAvailable:/{printf "%.2f", $2/1048576}' /proc/meminfo 2>/dev/null
}
trip() {  # $1 — причина; маркер паузы + остановка контейнера стадии
  local reason="$1" rc
  printf '%s\n' "$reason" > "$STOP_MARKER"
  log "СТОП ПО СТРАЖУ: $reason"
  "$DOCKER" stop "$2" >/dev/null 2>&1
  rc=$?
  log "остановка контейнера стадии $2: rc=$rc (контейнер не оставляется работать)"
  printf 'stopped\n' > "$CHAIN_STATUS_FILE"
}

#: Страж стадии. Опрашивает лог и dmesg, а не читает поток: при wedge поток
#: встаёт вместе со стадией, и построчный читатель не сработал бы именно тогда,
#: когда он нужен (та же причина, что в разведке S3-pre).
watch_stage() {  # $1 — лог стадии, $2 — имя контейнера
  local log_file="$1" ctr="$2" off=0 size=0 chunk ent low=0 nvrm_log=0 n_hits=0
  local dmesg_before dmesg_now last_change last_dmesg now stall_seconds stall_limit
  #: Порог молчания в секундах: `--stall-minutes` для человека, `--stall-seconds`
  #: для коротких окон (тесты и разовые прогоны) — второе перебивает первое.
  stall_limit=$((STALL_SECONDS > 0 ? STALL_SECONDS : STALL_MINUTES * 60))
  dmesg_before="$(nvrm_xid_count)"
  last_change="$(date +%s)"; last_dmesg="$(date +%s)"
  while [ ! -f "$STOP_MARKER" ]; do
    now="$(date +%s)"
    # ── новые строки лога ───────────────────────────────────────────────────
    if [ -f "$log_file" ]; then
      size="$(stat -c %s "$log_file" 2>/dev/null || printf '0')"
      if [ "$size" -gt "$off" ]; then
        chunk="$(tail -c +$((off + 1)) "$log_file" 2>/dev/null)"
        off="$size"; last_change="$now"
        # энтропия политики: строки пайплайна «RL step N/M | ... | entropy=X | ...».
        # Наблюдение — строка лога (шаг, кратный 10), а **не опрос**: за один опрос
        # в лог может попасть несколько шагов, и «два подряд» обязано их считать
        # все — иначе стоп-условие тихо превратилось бы в «два опроса подряд».
        for ent in $(printf '%s\n' "$chunk" | grep -oE 'entropy=[0-9.]+' | cut -d= -f2); do
          if awk -v e="$ent" -v f="$ENTROPY_FLOOR" 'BEGIN{exit !(e < f)}'; then
            low=$((low + 1))
            log "СТРАЖ: энтропия политики $ent < $ENTROPY_FLOOR ($low/$ENTROPY_LOW_STREAK подряд)"
            if [ "$low" -ge "$ENTROPY_LOW_STREAK" ]; then
              trip "энтропия политики < $ENTROPY_FLOOR на $low наблюдениях подряд — mode collapse (ADR-010 п.5)" "$ctr"
              return 0
            fi
          else
            low=0
          fi
        done
        # NVRM/Xid в логе стадии
        n_hits="$(printf '%s\n' "$chunk" | grep -cE 'NVRM|Xid' || true)"
        nvrm_log=$((nvrm_log + n_hits))
        # 0x51 — отдельный признак: это буря, а не «ещё один инцидент»: после неё
        # цепочка ждёт окно затишья (storm_gap.sh) и повторяет стадию, а не
        # бросается перезапускать её сразу. Xid печатается с адресом PCI, в
        # котором есть цифры («Xid (PCI:0000:01:00): 51»), поэтому код ищется как
        # отдельное число после Xid, а не «через N нецифровых символов».
        if printf '%s\n' "$chunk" | grep -qE 'Xid.*[^0-9]51([^0-9]|$)|Xid.*0x51'; then
          log "СТРАЖ: NVRM Xid 0x51 в логе стадии — флаг бури"
          printf '%s\n' "$(date -Is)" > "$STORM_FLAG"
        fi
      fi
    fi
    # ── dmesg раз в `MEM_POLL_SECONDS`: жёсткая буря может не оставить строк в логе
    if [ $((now - last_dmesg)) -ge "$MEM_POLL_SECONDS" ]; then
      last_dmesg="$now"
      dmesg_now="$(nvrm_xid_count)"
      if [ -n "${dmesg_before:-}" ] && [ -n "${dmesg_now:-}" ] \
         && [ "$dmesg_now" -gt "$dmesg_before" ] 2>/dev/null; then
        log "СТРАЖ: NVRM/Xid в dmesg +$((dmesg_now - dmesg_before))"
        nvrm_log=$((nvrm_log + dmesg_now - dmesg_before))
        dmesg_before="$dmesg_now"
      fi
      if sudo -n dmesg 2>/dev/null </dev/null | grep -qE 'Xid.*[^0-9]51([^0-9]|$)|Xid.*0x51'; then
        printf '%s\n' "$(date -Is)" > "$STORM_FLAG"
      fi
      local gb; gb="$(mem_available_gb)"
      if [ -n "${gb:-}" ] && awk -v g="$gb" -v f="$MEM_FLOOR_GB" 'BEGIN{exit !(g < f)}'; then
        trip "свободной unified-памяти $gb ГБ < предохранителя $MEM_FLOOR_GB ГБ — стоп, чтобы не убить llm-platform-*" "$ctr"
        return 0
      fi
    fi
    if [ "$nvrm_log" -ge "$NVRM_LIMIT" ]; then
      trip "NVRM/Xid-инцидентов $nvrm_log ≥ $NVRM_LIMIT — стоп и пауза (ADR-010 п.5)" "$ctr"
      return 0
    fi
    # ── молчание стадии ────────────────────────────────────────────────────
    stall_seconds=$((now - last_change))
    if [ "$stall_limit" -gt 0 ] && [ "$stall_seconds" -ge "$stall_limit" ]; then
      trip "стадия молчит ${stall_seconds}с (порог ${stall_limit}с) — wedge: стоп с резюмом с чекпойнта" "$ctr"
      return 0
    fi
    sleep "$POLL_SECONDS"
  done
}

glm_key() {
  [ -f "$GLM_ENV" ] || return 1
  grep -m1 '^GLM_API_KEY=' "$GLM_ENV" 2>/dev/null | cut -d= -f2-
}

stage_env() {  # $1 — имя стадии; печатает строку `-e ...`
  local name="$1" glm
  printf -- '-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -e HF_HUB_OFFLINE=1'
  printf -- ' -e TRANSFORMERS_OFFLINE=1 -e LAGUNA_MEM_FRACTION=%s' "$MEM_FRACTION"
  printf -- ' -e VLLM_ALLOW_INSECURE_SERIALIZATION=1 -e PYTHONPATH=/workspace/shared/wt_stubs'
  case "$name" in
    cpt)
      printf -- ' -e LAGUNA_ATTN=%s -e FLEX_COMPILE=0 -e CPT_GEN_EVAL_EVERY=%s' "$ATTN" "$CPT_GEN_EVAL_EVERY" ;;
    sft)
      printf -- ' -e LAGUNA_ATTN=%s -e FLEX_COMPILE=0' "$ATTN" ;;
    rl)
      printf -- ' -e VLLM_GPU_UTIL=%s -e VLLM_KV_CACHE_BYTES=%s -e VLLM_EAGER=%s' \
        "$VLLM_GPU_UTIL" "$VLLM_KV_CACHE_BYTES" "$VLLM_EAGER"
      printf -- ' -e RESYNC_EVERY=%s -e GEN_EVAL_EVERY=%s' "$RESYNC_EVERY" "$RL_GEN_EVAL_EVERY"
      printf -- ' -e TORCHINDUCTOR_CACHE_DIR=/workspace/shared/inductor_cache' ;;
    eval_*)
      # GLM-ключ передаётся ТОЛЬКО стадиям eval: судья в контуре награды запрещён
      # (AD-6/C-013), и отсутствие ключа в RL-контейнере — это свойство запуска,
      # а не только свойство кода.
      glm="$(glm_key)" || glm=""
      printf -- ' -e VLLM_GPU_UTIL=%s -e EVAL_ITEMS=%s -e GLM_API_KEY=%s -e GLM_MODEL=glm-5.3-flash' \
        "$VLLM_GPU_UTIL" "$EVAL_ITEMS" "$glm" ;;
  esac
}

pipeline_args() {  # name pstage steps batch eckpt exp
  #: Позиции — по своей же сигнатуре выше: 5-й аргумент это `eval_ckpt` (поле 7
  #: TSV-ряда), 6-й — `exp` (поле 8). Перепутанные местами они дают `--exp_name -`
  #: и `--eval_ckpt <имя прогона>`, на котором пайплайн падает в argparse за 6 с:
  #: стадия не стартует вовсе, а цепочка остаётся резюмируемой (факт S3c).
  printf -- '--model_name %s --stage %s --exp_name %s' "$MODEL" "$2" "$6"
  printf -- ' --max_steps %s --max_samples %s --batch_size %s --max_len %s' "$3" "$MAX_SAMPLES" "$4" "$MAX_LEN"
  printf -- ' --rl_steps %s --seed %s --peak_lr_scale %s' "$RL_STEPS" "$SEED" "$PEAK_LR_SCALE"
  printf -- ' --cpt_data /workspace/shared/datasets/cpt_corpus_v12r.txt'
  printf -- ' --sft_data /workspace/shared/datasets/sft_train_v12.jsonl'
  printf -- ' --rl_data /workspace/shared/datasets/rl_tasks_revpool_v1.jsonl'
  printf -- ' --eval_data /workspace/shared/datasets/eval_ood_clean.jsonl'
  printf -- ' --ckpt_dir %s/checkpoints --log_dir %s/logs' "$CTR_RUN_DIR" "$CTR_RUN_DIR"
  [ "$5" != "-" ] && printf -- ' --eval_ckpt %s' "$5"
  return 0
}

#: eval-стадия без GLM-ключа даёт `judge: slug-verifier` — эвристику вместо судьи,
#: то есть подменённую метрику оси AD-1. Проверяется по артефакту, а не по логу.
eval_judge_ok() {  # $1 — артефакт eval
  python3 - "$RUN_DIR/$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print(f"артефакт не разобран: {e}"); sys.exit(1)
judge = d.get("judge")
print(f"judge={judge} judge_mean={d.get('judge_mean')} total={d.get('total')}")
sys.exit(0 if judge == "GLM-API" else 1)
PY
}

# ── одна попытка стадии ──────────────────────────────────────────────────────
attempt_stage() {  # name pstage gstage artifact steps batch eckpt exp
  local name="$1" pstage="$2" gstage="$3" artifact="$4" steps="$5" batch="$6" eckpt="$7" exp="$8"
  local log_file="$RUN_DIR/logs/$name.log" ctr="$CTR_PREFIX-$name"
  local rc t0 t1 sampler_pid="" watch_pid=""

  # предусловие AD-9 — перед каждой стадией, без исключений для резюма
  log "предусловие AD-9: --stage $gstage --exp-name $exp"
  if ! guard_ok "$gstage" "$exp"; then
    log "ОТКАЗ СТРАЖА: стадия $name не стартует (см. вердикт выше) — это правило, а не сбой"
    record_stage "$name" "blocked" "$artifact" 0 "страж AD-9 отказал"
    fail_precondition "страж AD-9 отказал: стадия $name не стартовала"
  fi

  # предусловия стадии, которых не знает страж: чекпойнт для eval и GLM-ключ
  case "$name" in
    eval_sft) [ -f "$RUN_DIR/checkpoints/sft_checkpoint_final.pt" ] || fail_precondition "eval_sft: нет sft_checkpoint_final.pt" ;;
    eval_rl)  [ -f "$RUN_DIR/checkpoints/rl_checkpoint_final.pt" ] || fail_precondition "eval_rl: нет rl_checkpoint_final.pt" ;;
    eval_base) : ;;
  esac
  case "$name" in
    eval_*)
      [ -n "$(glm_key)" ] || fail_precondition "$name: в $GLM_ENV нет GLM_API_KEY — без него eval даёт эвристику вместо судьи, и метрика оси подменяется молча"
      ;;
  esac

  # остаток чужого контейнера с тем же именем: он наш (имя уникально), но стадия
  # сейчас стартует заново — держать рядом осиротевший контейнер нельзя (AD-5)
  if [ -n "$("$DOCKER" ps -aq -f "name=^${ctr}$" 2>/dev/null)" ]; then
    log "остаток контейнера $ctr — закрываю перед стартом стадии"
    "$DOCKER" stop "$ctr" >/dev/null 2>&1 || true
    "$DOCKER" rm -f "$ctr" >/dev/null 2>&1 || true
  fi

  rm -f "$STORM_FLAG"
  log "СТАДИЯ $name: старт (шагов=$steps batch=$batch exp=$exp)"
  t0="$(date +%s)"
  if [ -n "$SAMPLER" ] && [ -f "$SAMPLER" ]; then
    bash "$SAMPLER" "$RUN_DIR/mem_$name.jsonl" 5 "$SAMPLER_SECONDS" >/dev/null 2>&1 </dev/null &
    sampler_pid="$!"
  fi
  watch_stage "$log_file" "$ctr" &
  watch_pid="$!"

  # </dev/null: stdin зациклен на файл стадий (см. guard_ok) — контейнер не должен
  # его вычитывать; лог стадии пишется прямо в файл, который опрашивает страж.
  bash "$SAFE_START" -d 60 -i 5 -- "$DOCKER" run --rm --name "$ctr" \
    --gpus all --ipc=host --pid=host \
    --memory="$MEM_CAP" --memory-swap="$MEM_CAP" \
    --security-opt seccomp=unconfined --cap-add SYS_PTRACE \
    --ulimit memlock=-1 --ulimit stack=67108864 --ulimit nofile=262144:262144 \
    $(stage_env "$name") \
    -v "$EXPERIMENTS:$CTR_EXPERIMENTS" \
    -v "$SHARED:$CTR_SHARED" \
    -v /home/user/.cache/huggingface:/root/.cache/huggingface -w /workspace \
    "$IMAGE" \
    python3 "$PIPELINE_CTR" $(pipeline_args "$name" "$pstage" "$steps" "$batch" "$eckpt" "$exp") \
    </dev/null > "$log_file" 2>&1
  rc=$?
  t1="$(date +%s)"

  [ -n "$watch_pid" ] && kill "$watch_pid" 2>/dev/null
  [ -n "$sampler_pid" ] && kill "$sampler_pid" 2>/dev/null
  wait "$watch_pid" 2>/dev/null
  wait "$sampler_pid" 2>/dev/null

  if [ -f "$STOP_MARKER" ]; then
    log "стадия $name остановлена стражем: $(head -1 "$STOP_MARKER")"
    record_stage "$name" "stopped" "$artifact" "$((t1 - t0))" "стоп-условие"
    printf 'stopped\n' > "$CHAIN_STATUS_FILE"
    return 3
  fi
  log "СТАДИЯ $name: контейнер завершился rc=$rc за $((t1 - t0)) с (лог: logs/$name.log)"
  LAST_RC="$rc"; LAST_WALL="$((t1 - t0))"
  return 0
}

# ── основной цикл ────────────────────────────────────────────────────────────
while IFS=$'\t' read -r NAME PSTAGE GSTAGE ARTIFACT STEPS BATCH ECKPT EXP; do
  [ -n "${NAME:-}" ] || continue
  # Предусловие проверяется и для готовой стадии: `--check-only` должен отвечать
  # на вопрос «право стартовать есть?» по всем стадиям, а не по первой неготовой.
  if [ "$CHECK_ONLY" = "1" ]; then
    log "--- $NAME: предусловие AD-9 (--stage $GSTAGE --exp-name $EXP)"
    if ! guard_ok "$GSTAGE" "$EXP"; then
      log "ОТКАЗ СТРАЖА: стадия $NAME не стартует"
      printf 'precondition_failed\n' > "$CHAIN_STATUS_FILE"
      exit 2
    fi
    log "--- $NAME: страж пропускает (стадия не запускается: --check-only)"
    continue
  fi
  if [ -f "$RUN_DIR/$ARTIFACT" ]; then
    log "SKIP $NAME: артефакт на месте ($ARTIFACT)"
    continue
  fi
  attempt=0
  while : ; do
    attempt=$((attempt + 1))
    attempt_stage "$NAME" "$PSTAGE" "$GSTAGE" "$ARTIFACT" "$STEPS" "$BATCH" "$ECKPT" "$EXP"
    rc=$?
    if [ "$rc" = "3" ]; then exit 3; fi
    if [ "$rc" = "2" ]; then exit 2; fi
    if [ "$rc" != "0" ]; then exit 1; fi
    if [ -f "$RUN_DIR/$ARTIFACT" ]; then
      # Артефакт есть, но стадия вышла с ненулевым кодом (напр. упали пробы ПОСЛЕ
      # сохранения финального чекпойнта): работа сделана, и повторять 61 час SFT
      # из-за этого нельзя — статус `partial` с кодом, и это видно в манифесте.
      if [ "$LAST_RC" = "0" ]; then
        record_stage "$NAME" "done" "$ARTIFACT" "$LAST_WALL" ""
        log "СТАДИЯ $NAME: артефакт записан ($ARTIFACT)"
      else
        record_stage "$NAME" "partial" "$ARTIFACT" "$LAST_WALL" "rc=$LAST_RC"
        log "ВНИМАНИЕ: стадия $NAME оставила артефакт, но вышла с rc=$LAST_RC — \
статус partial (работа сделана, хвост стадии — нет); pipeline_complete не будет выставлен"
      fi
      case "$NAME" in
        eval_*) if ! eval_judge_ok "$ARTIFACT"; then
                  log "ОТКАЗ: $NAME — метрика не от судьи (см. выше); ось AD-1 на этой стадии не закрыта"
                  record_stage "$NAME" "failed" "$ARTIFACT" "$LAST_WALL" "judge != GLM-API"
                  write_manifest
                  printf 'failed\n' > "$CHAIN_STATUS_FILE"
                  exit 1
                fi ;;
      esac
      write_manifest
      break
    fi
    # Стадия не оставила артефакта. Если это была буря 0x51 — сначала окно
    # затишья (storm_gap.sh), и только потом повтор: немедленный retry попадает
    # в ту же бурю. Прочие падения — резюмируемы повторным запуском цепочки.
    if [ -f "$STORM_FLAG" ]; then
      rm -f "$STORM_FLAG"
      log "NVRM 0x51 в стадии $NAME — жду окно затишья (storm_gap.sh), затем повтор"
      if ! bash "$STORM_GAP" "$NVRN_LOG" 90 1800; then
        log "ОТКАЗ: storm_gap не открыл окно затишья — эскалация владельцу"
        printf '%s\n' "storm_gap не открыл окно после 0x51 в стадии $NAME" > "$STOP_MARKER"
        printf 'stopped\n' > "$CHAIN_STATUS_FILE"
        exit 3
      fi
      if [ "$attempt" -le "$STAGE_RETRIES" ]; then
        log "повтор стадии $NAME (попытка $((attempt + 1)))"
        continue
      fi
      log "ОТКАЗ: исчерпан бюджет повторов ($STAGE_RETRIES) для $NAME"
      record_stage "$NAME" "failed" "$ARTIFACT" "$LAST_WALL" "0x51, повторы исчерпаны"
      write_manifest
      printf 'failed\n' > "$CHAIN_STATUS_FILE"
      exit 1
    fi
    record_stage "$NAME" "failed" "$ARTIFACT" "$LAST_WALL" "rc=$LAST_RC, артефакта нет"
    write_manifest
    log "ОТКАЗ: стадия $NAME не оставила артефакта ($ARTIFACT), rc=$LAST_RC — цепочка остановлена и резюмируема"
    printf 'failed\n' > "$CHAIN_STATUS_FILE"
    exit 1
  done
done < "$STAGES_FILE"

#: `--check-only` отвечает на вопрос «есть ли право стартовать у каждой стадии» и
#: на этом заканчивается: полнота артефактов тут ни при чём, а `write_manifest`
#: писал бы манифест по прогону, которого не было.
if [ "$CHECK_ONLY" = "1" ]; then
  log "== проверка права стартовать пройдена по всем стадиям (обучение не запускалось) =="
  printf 'checked\n' > "$CHAIN_STATUS_FILE"
  exit 0
fi

write_manifest
if all_done; then
  log "== ЦЕПОЧКА ЗАВЕРШЕНА: все стадии на месте и со статусом done, манифест AD-2 pipeline_complete=true =="
  printf 'done\n' > "$CHAIN_STATUS_FILE"
  exit 0
fi
log "цепочка завершилась без ошибок, но полнота не подтверждена: часть стадий без артефакта \
или со статусом partial (см. $STATUS_DIR) — pipeline_complete не выставлен"
printf 'partial\n' > "$CHAIN_STATUS_FILE"
exit 1
