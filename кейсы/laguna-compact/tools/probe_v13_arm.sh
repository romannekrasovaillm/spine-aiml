#!/usr/bin/env bash
# S3ax / проба руки «v13 + пониженный темп» — та же площадка, что рука сравнения и база.
#
# **Что меряется.** Точка 500 руки `sft-v13lr2e6-*` (набор v13_fixed, постоянный
# LR 2e-6) — состояние, которого ещё не мерили. Затем, в ЭТОЙ ЖЕ сессии, точка 500
# руки сравнения `sft-lr2e6-20260921-1453` (набор v12, тот же LR): она уже снята
# 21.09.2026 на этом же стенде, и повтор отвечает на вопрос, которого прежний замер
# не закрывал, — **переносится ли число через границу сессий** на одной площадке.
# Прибор детерминирован на устройстве; из этого следовала сопоставимость замеров
# разных сессий, но это принималось, а не проверялось.
#
# **Почему база CPT здесь не перемеряется.** Её число на этой площадке уже есть
# (`report_cfinal.json` цепочки lr-sens-20260921-1500, 8/104 = 7.7 %), и оно
# отделено от обеих рук расстоянием в 5-20 п.п. — сдвиг площадки такого размера
# не создаёт. Тесная пара (две руки) перемеряется, далёкая точка — нет.
#
# **Один прибор, один протокол.** `tools/probe_language_split.py` пиннут хешем
# `99dafa8d…` — тот же, что в решающих отчётах S3ap/S3aq/S3aw; greedy + штатный
# запрет повторов 4-грамм (ADR-041), набор `wide` (104 пробы), бюджет 4096,
# пакет 8, остановка на конце хода.
#
# **Чего здесь нет.** Манифест прогона (шаг AD-2) не пишется: на площадке он падает
# с `ModuleNotFoundError: No module named 'numpy'` (прибор тянет `probe_control`,
# а у хостового python3 нет numpy) — дефект стенда, назван в отчёте фактом и НЕ
# чинится здесь (ADR-016 п.1: под живой цепочкой не правят). Отчёты приборов
# самодостаточны: в каждом записан `checkpoint_sha256` и `tool_sha256`.
#
# **Что было сломано в первой редакции (21.09.2026, пакет v13-arm-probe-20260921-1905).**
# В `tools/` доставлялся ОДИН прибор, без `probe_control.py` и `ppl_probe.py`, которые
# он импортирует на уровне файла: обе пробы упали за две секунды
# (`ModuleNotFoundError`), отчётов не появилось — но `CHAIN_DONE` был записан
# (`done new=1 prev=1`), и пакет выглядел готовой опорой. Два урока записаны в код:
# (1) прибор доставляется ВМЕСТЕ с модулями и это проверяется по хешу; (2) провал
# полезной нагрузки НЕ заканчивается нулём — цепочка обязана назвать отказ, а
# `CHAIN_DONE` — нести код возврата, который читают (стартер читает).
#
# **Какие руки снимаются.** `ARMS` (по умолчанию `both`): `v13` — только новая рука
# (прямое сравнение для гейта), `both` — ещё и повтор руки сравнения (отвечает на
# вопрос переносимости числа через границу сессий).
set -u
STAGE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED=/home/user/gb10-shared
IMG=nvcr.io/nvidia/pytorch:26.07-py3-vllm
PREFIX=laguna-v13arm2
TOOLS="$STAGE/tools"
CKPTS="$STAGE/ckpts"
ARMS="${ARMS:-both}"

#: Рука сравнения: её прогон остановлен рукой, точка 500 лежит как артефакт.
ARM_CTR=laguna-sft-v13lr2e6-20260921-1748-sft
NEW_SRC="$SHARED/sft-v13lr2e6-20260921-1748/checkpoints/sft_probe_500.pt"
NEW_TAG=arm_v13_lr2e6_500
PREV_SRC="$SHARED/sft-lr2e6-20260921-1453/checkpoints/sft_probe_500.pt"
PREV_TAG=arm_v12_lr2e6_500

say() { echo "[$(date -Is)] $*"; }

say "== S3ax: проба руки v13+LR2e-6 и повтор руки v12+LR2e-6, wide/4096 =="
say "стенд: $(hostname); контейнеры с префиксом $PREFIX; руки: $ARMS"

# ── 0a. Полнота прибора — ДО всего остального ─────────────────────────────────
# Прибор импортирует `probe_control` и `ppl_probe` на уровне файла. Пакет, куда
# положили один прибор, даёт не замер, а `ModuleNotFoundError` за две секунды —
# и это тем опаснее, что файлы-признаки («CHAIN_DONE») при этом появляются.
PROBE_MODULES="probe_language_split.py probe_control.py ppl_probe.py"
for m in $PROBE_MODULES; do
  [ -f "$TOOLS/$m" ] || { say "ОТКАЗ: в $TOOLS нет $m — прибор неполон, проба не стартует"; exit 3; }
done
for m in $PROBE_MODULES; do printf '  %s  %s\n' "$(sha256sum "$TOOLS/$m" | cut -c1-12)" "$m"; done
say "0a: прибор полон (три модуля на месте)"

# ── 0. Освобождение устройства — по ФАКТУ процесса, а не по наличию файла ──────
# Файл точки появляется атомарной заменой раньше, чем контейнер отдаёт память;
# проба, стартовавшая рядом, мерила бы конкуренцию за устройство, а не состояние.
say "0: ждём освобождения устройства: контейнер $ARM_CTR"
for _ in $(seq 1 240); do
  docker ps --format '{{.Names}}' | grep -q "^${ARM_CTR}$" || break
  sleep 30
done
if docker ps --format '{{.Names}}' | grep -q "^${ARM_CTR}$"; then
  say "ОТКАЗ: стадия не освободила устройство за 2 ч — проба не стартует"; exit 2
fi
say "0: устройство свободно"

# ── 1. Пиннинг чекпойнтов: копия отделяет замер от жизни чужого прогона ───────
mkdir -p "$CKPTS"
pin_one() {  # src dst tag
  local src="$1" dst="$2" tag="$3"
  [ -f "$src" ] || { say "ОТКАЗ: нет артефакта $tag: $src"; return 1; }
  cp -f "$src" "$dst" || return 1
  local a b; a=$(sha256sum "$src" | cut -d' ' -f1); b=$(sha256sum "$dst" | cut -d' ' -f1)
  [ "$a" = "$b" ] || { say "ОТКАЗ: копия $tag не совпала по sha256"; return 1; }
  printf '%s' "$a"
}
NEW_SHA=$(pin_one "$NEW_SRC" "$CKPTS/$NEW_TAG.pt" "$NEW_TAG") || exit 3
PREV_SHA=$(pin_one "$PREV_SRC" "$CKPTS/$PREV_TAG.pt" "$PREV_TAG") || exit 3
say "1: точки пиннуты (new ${NEW_SHA:0:12}, prev ${PREV_SHA:0:12})"

python3 - "$STAGE" "$NEW_SHA" "$PREV_SHA" "$NEW_SRC" "$PREV_SRC" \
  > "$STAGE/pin_checkpoints.json" 2>&1 <<'PY'
import hashlib, json, sys
from pathlib import Path
stage, new_sha, prev_sha, new_src, prev_src = sys.argv[1:6]

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()

tool = Path(stage) / "tools" / "probe_language_split.py"
out = {
    "why": "копия отделяет замер от жизни чужого прогона (ретенция точек) и не меняет "
           "источники: только чтение",
    "checkpoints": {
        "arm_v13_lr2e6_500": {"source": new_src, "pinned": str(Path(stage)/"ckpts"/"arm_v13_lr2e6_500.pt"),
                              "sha256_source": new_sha, "sha256_pinned": sha(Path(stage)/"ckpts"/"arm_v13_lr2e6_500.pt")},
        "arm_v12_lr2e6_500": {"source": prev_src, "pinned": str(Path(stage)/"ckpts"/"arm_v12_lr2e6_500.pt"),
                              "sha256_source": prev_sha, "sha256_pinned": sha(Path(stage)/"ckpts"/"arm_v12_lr2e6_500.pt")},
    },
    "instrument": {"path": str(tool), "sha256": sha(tool),
                   "expected": "99dafa8d9551caaa57b770d4cd321da6f0c09b28deb1885500034c9d62eeb276",
                   "is_expected": sha(tool) == "99dafa8d9551caaa57b770d4cd321da6f0c09b28deb1885500034c9d62eeb276"},
}
out["all_identical"] = all(v["sha256_source"] == v["sha256_pinned"]
                           for v in out["checkpoints"].values())
print(json.dumps(out, ensure_ascii=False, indent=1))
sys.exit(0 if out["all_identical"] and out["instrument"]["is_expected"] else 1)
PY
[ $? -eq 0 ] || { say "1 FAIL: копия или прибор не совпали"; exit 3; }
say "1 OK: копии тождественны источникам, прибор — штатный (99dafa8d)"

# ── 2. Проба новой руки ──────────────────────────────────────────────────────
rc_new=9; rc_prev=9
say "2: wide (104 пробы) × $NEW_TAG, бюджет 4096"
t0=$(date +%s)
docker rm -f "$PREFIX-new" >/dev/null 2>&1 || true
docker run --rm --name "$PREFIX-new" --gpus all --ipc=host \
  -v "$SHARED:$SHARED" -v /home/user/.cache/huggingface:/root/.cache/huggingface \
  -w "$STAGE" "$IMG" \
  python3 "$TOOLS/probe_language_split.py" --prompts wide --max-new-tokens 4096 \
    --stop-at-turn-end --batch-size 8 --sha \
    --ckpt "$NEW_TAG=$CKPTS/$NEW_TAG.pt" \
    --out "$STAGE/report_$NEW_TAG.json" \
  > "$STAGE/log_$NEW_TAG.log" 2>&1
rc_new=$?
say "2: код возврата $rc_new за $(( $(date +%s) - t0 ))с"
if [ "$rc_new" != 0 ]; then
  say "2 FAIL: проба не снята — хвост лога:"
  tail -5 "$STAGE/log_$NEW_TAG.log" | sed 's/^/    | /'
fi

# ── 3. Повтор руки сравнения — граница сессий на одной площадке ───────────────
if [ "$ARMS" = "both" ]; then
say "3: wide (104 пробы) × $PREV_TAG (повтор в этой же сессии), бюджет 4096"
t0=$(date +%s)
docker rm -f "$PREFIX-prev" >/dev/null 2>&1 || true
docker run --rm --name "$PREFIX-prev" --gpus all --ipc=host \
  -v "$SHARED:$SHARED" -v /home/user/.cache/huggingface:/root/.cache/huggingface \
  -w "$STAGE" "$IMG" \
  python3 "$TOOLS/probe_language_split.py" --prompts wide --max-new-tokens 4096 \
    --stop-at-turn-end --batch-size 8 --sha \
    --ckpt "$PREV_TAG=$CKPTS/$PREV_TAG.pt" \
    --out "$STAGE/report_$PREV_TAG.json" \
  > "$STAGE/log_$PREV_TAG.log" 2>&1
rc_prev=$?
say "3: код возврата $rc_prev за $(( $(date +%s) - t0 ))с"
if [ "$rc_prev" != 0 ]; then
  say "3 FAIL: повтор не снят — хвост лога:"
  tail -5 "$STAGE/log_$PREV_TAG.log" | sed 's/^/    | /'
fi
else
  say "3: рука сравнения не перемеряется (ARMS=$ARMS) — опора берётся из прежней сессии"
  rc_prev=skip
fi

say "== S3ax: пробы завершены (new rc=$rc_new, prev rc=$rc_prev) =="
#: `CHAIN_DONE` несёт КОДЫ возврата, а не слово «готово»: первая редакция писала
#: `done new=1 prev=1` и выходила нулём — пакет выглядел готовой опорой, отчётов
#: не было. Читает его стартер (`probe_chain_state`): опора есть только при нулях.
printf 'done new=%s prev=%s\n' "$rc_new" "$rc_prev" > "$STAGE/CHAIN_DONE"
if [ "$rc_new" != 0 ] || { [ "$ARMS" = "both" ] && [ "$rc_prev" != 0 ]; }; then
  say "ОТКАЗ цепочки: проба не снята — опорой этот пакет не считается"
  exit 1
fi
