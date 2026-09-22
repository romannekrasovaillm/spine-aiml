#!/usr/bin/env bash
# qwen35_gate.sh — ворота поддержки архитектуры `qwen3_5` (предусловие волны В-1;
# ADR-055 шаг 4; план `docs/specs/LADDER-FULL-PLAN.md` §1.2б и §10).
#
# ПРЕДМЕТ. Один вопрос: берёт ли стек (`transformers` + `vLLM`) архитектуру
# `model_type: qwen3_5` на `Qwen/Qwen3.5-0.8B-Base`. Ответ — одно из трёх
# состояний, названных словом, а не догадкой:
#
#   not-run      — пробы не было: вердикта нет (ни «поддержан», ни «не поддержан»);
#   supported    — проба была и прошла: `HF_LOAD_OK` + `VLLM_LOAD_OK`;
#   unsupported  — проба была и не прошла (отказ или падение).
#
# ── ЧТО ТАКОЕ «not-run»: отсутствие носителя, а не файл-заглушка (S3bi) ──────
# Три состояния различаются НОСИТЕЛЕМ, а не наличием «удобных» файлов:
#   · `not-run` — носителя `<state-dir>/qwen35-loadtest.json` НЕТ ВОВСЕ, и «не
#     запускалось» произносится **словами в выводе гейта**. Заранее записанной
#     «not-run»-записи не бывает: файл с таким вердиктом читался бы как вердикт
#     (класс «зелёное без результата» — «есть запись, значит что-то проверено»),
#     тогда как на деле пробы не было и проверять нечего;
#   · носитель создаётся **только фактом прогона** — единственный писатель это
#     `run` (после исполнения пробы), и в него попадает ровно два значения:
#     `supported` или `unsupported`. Отказ `run` (занятость стенда, неизмеренная
#     занятость) носитель НЕ создаёт и НЕ трогает: «не запускалось» так и остаётся
#     отсутствием файла плюс словом в выводе.
# Проверяется механически: `status` на дереве без носителя обязан назвать состояние
# словом `not-run` и не оставить после себя файлов (секция 47 тестов).
#
# ЧТО ИСПРАВЛЕНО ПРОТИВ `~/gb10-shared/qwen35_gate.sh` (тот не выполнялся ни разу).
# Прежний гейт перед пробой ждал **чужое событие** — строку `V4 LADDER QWEN3
# COMPLETE` в журнале лесенки Qwen3, — поэтому к 07.08.2026 дал 14 циклов ожидания
# и ни одной строки пробы: `qwen35_loadtest.log` не появился вовсе. Здесь:
#
#   1. **Никакого ожидания чужого флага и чужой лесенки.** Ни `REVISION-WINDOW.lock`,
#      ни журнал лесенки, ни `ALL_QWEN35_BASE_DOWNLOADED` не читаются: ждать нечего —
#      веса на стенде есть (замер 22.09.2026, §1.2а). Проба — самостоятельный шаг
#      подготовки В-1, и она выполняется по окну, а не по чужому событию.
#   2. **Никакого автозапуска лесенки.** Прежний гейт при успехе сам поднимал
#      `run_v4_ladder_qwen35.sh`; здесь успех даёт **вердикт**, а не нагрузку.
#      Гейт не занимает стенд работой — он её только разрешает или запрещает.
#   3. **Никакого долгого держания стенда.** Проба ограничена `timeout`
#      (по умолчанию 900 с) и `docker run --rm`: зависший контейнер не держит GPU
#      бесконечно (в прежнем гейте ограничения не было вовсе).
#   4. **Режим по умолчанию — `status`, read-only.** Стенд при этом не трогается:
#      ни `docker`, ни `nvidia-smi`, ни `ssh`. Вердикт читается с носителя.
#   5. **Занятость стенда — единственное, что может отказать `run`**, и отказывает
#      она **фактом**, а не флагом: сенсор — страж сериализации
#      `tools/check_gb10_serialization.sh` (AD-5), у него же один носитель понятия
#      «тренировочная нагрузка». Читается его строка `тренировочных нагрузок: N`:
#      N = 0 — окно есть; N ≥ 1 — стенд занят. Сенсор не прочитан — **отказ закрыто**
#      («наверное, свободно» нагрузкой не считается). Платформенный `llama-server`
#      нагрузкой не считается: проба идёт с `gpu_memory_utilization=0.30` именно
#      затем, чтобы ужиться с ним, — гейт, красящийся от постоянного сервиса,
#      вернулся бы в состояние «не выполняется никогда».
#
# ПРЕДМЕТ ПРОБЫ НЕ ИЗМЕНЁН: тот же образ, те же две проверки, тот же
# `gpu_memory_utilization=0.30` и `max_model_len=4096`, что и в прежнем гейте, —
# меняется только то, **что её запускает** (человек/харнесс по окну, а не строка
# в чужом журнале). Прежний носитель пробы — `~/gb10-shared/qwen35_gate.sh`: он
# read-only (чужой контур) и этой дельтой не правится, поэтому приведён здесь
# как исторический источник предмета пробы, а не как действующий гейт.
#
# Носитель вердикта — один файл (ADR-023 п.10): `<state-dir>/qwen35-loadtest.json`,
# рядом сырой журнал пробы `<state-dir>/qwen35-loadtest.log`, его sha256 — в вердикте.
# По умолчанию `<state-dir>` = `evidence/`, то есть `evidence/qwen35-loadtest.json`.
#
# Использование:
#   bash tools/qwen35_gate.sh [status] [--state-dir DIR] [--json FILE] [--legacy-log FILE]
#   bash tools/qwen35_gate.sh run [--probe-cmd CMD] [--busy-cmd CMD]
#                                [--host HOST | --local] [--ignore-busy]
#                                [--probe-timeout S] [--connect-timeout S]
#                                [--state-dir DIR] [--json FILE]
#
# `--json FILE` — куда положить машинный вердикт (по умолчанию в `--state-dir`;
# при записи stdout остаётся человекочитаемым, JSON уходит в файл — как у
# `inventory_stand_weights.py --remote`).
#
# Коды возврата (у `status` и `run` одни и те же — три состояния):
#   0 — `supported`   (проба была и прошла)
#   1 — `unsupported` (проба была и не прошла)
#   2 — `not-run`     (вердикта нет) либо вход/сенсор не прочитан
#
# Запуск: bash tools/qwen35_gate.sh                 # вердикт по фактам, стенд не трогается
#         bash tools/qwen35_gate.sh run --host gb10-fast   # проба, ~10 мин окна стенда

set -uo pipefail

CASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

STATE_DIR=""
JSON_OUT=""
LEGACY_LOG="${QWEN35_LEGACY_LOG:-$HOME/experiments/qwen35_gate.log}"
HOST="${GB10_HOST:-gb10-fast}"
LOCAL=0
IGNORE_BUSY=0
PROBE_TIMEOUT="${QWEN35_PROBE_TIMEOUT:-900}"
CONNECT_TIMEOUT=8
PROBE_CMD=""
BUSY_CMD=""
MODE="status"

VERDICT_NAME="qwen35-loadtest.json"
LOG_NAME="qwen35-loadtest.log"

# Проба не меняется: тот же образ и те же две проверки, что в прежнем гейте.
DEFAULT_PROBE_CMD='docker run --rm --gpus all --ipc=host -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -v "$HOME/.cache/huggingface:/root/.cache/huggingface" nvcr.io/nvidia/pytorch:26.07-py3-vllm python3 -c "import torch; from transformers import AutoModelForCausalLM, AutoTokenizer; tok = AutoTokenizer.from_pretrained(\"Qwen/Qwen3.5-0.8B-Base\", trust_remote_code=True); m = AutoModelForCausalLM.from_pretrained(\"Qwen/Qwen3.5-0.8B-Base\", torch_dtype=torch.bfloat16, trust_remote_code=True); print(\"chat_template:\", bool(tok.chat_template)); print(\"HF_LOAD_OK\"); del m; torch.cuda.empty_cache(); from vllm import LLM; llm = LLM(model=\"Qwen/Qwen3.5-0.8B-Base\", dtype=\"bfloat16\", max_model_len=4096, gpu_memory_utilization=0.30); print(\"VLLM_LOAD_OK\")"'

# Занятость стенда: вердикт стража сериализации (AD-5) — факт о нагрузке, а не флаг
# в чужом контуре. Один носитель понятия «тренировочная нагрузка» — тот же страж.
DEFAULT_BUSY_CMD=""

usage() { sed -n '2,83p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    status)            MODE="status"; shift ;;
    run)               MODE="run"; shift ;;
    --state-dir)       STATE_DIR="${2:?--state-dir требует значение}"; shift 2 ;;
    --json)            JSON_OUT="${2:?--json требует значение}"; shift 2 ;;
    --legacy-log)      LEGACY_LOG="${2:?--legacy-log требует значение}"; shift 2 ;;
    --host)            HOST="${2:?--host требует значение}"; shift 2 ;;
    --local)           LOCAL=1; shift ;;
    --probe-cmd)       PROBE_CMD="${2:?--probe-cmd требует значение}"; shift 2 ;;
    --busy-cmd)        BUSY_CMD="${2:?--busy-cmd требует значение}"; shift 2 ;;
    --ignore-busy)     IGNORE_BUSY=1; shift ;;
    --probe-timeout)   PROBE_TIMEOUT="${2:?--probe-timeout требует секунды}"; shift 2 ;;
    --connect-timeout) CONNECT_TIMEOUT="${2:?--connect-timeout требует секунды}"; shift 2 ;;
    -h|--help)         usage ;;
    *) echo "qwen35_gate: неизвестный аргумент «$1» (--help)" >&2; exit 2 ;;
  esac
done

[ -n "$STATE_DIR" ] || STATE_DIR="$CASE_ROOT/evidence"
[ -n "$JSON_OUT" ]  || JSON_OUT="$STATE_DIR/$VERDICT_NAME"
LOG_PATH="$STATE_DIR/$LOG_NAME"
[ -n "$PROBE_CMD" ] || PROBE_CMD="$DEFAULT_PROBE_CMD"
[ -n "$BUSY_CMD" ]  || BUSY_CMD="$DEFAULT_BUSY_CMD"

# busy_sensor — вердикт стража сериализации о тренировочных нагрузках. Страж
# сам умеет и локальную площадку, и стенд по ssh, поэтому транспорт здесь его,
# а не наш: `--busy-cmd` (проба/тест) заменяет сенсор целиком.
busy_sensor() {
  if [ -n "$BUSY_CMD" ]; then
    bash -c "$BUSY_CMD" 2>&1
  elif [ "$LOCAL" = "1" ]; then
    bash "$CASE_ROOT/tools/check_gb10_serialization.sh" 2>&1
  else
    bash "$CASE_ROOT/tools/check_gb10_serialization.sh" --host "$HOST" 2>&1
  fi
}

json_get() {  # json_get <файл> <ключ>
  python3 - "$1" "$2" <<'PYGATE' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(2)
v = d.get(sys.argv[2])
sys.exit(3) if v is None else print(v)
PYGATE
}

# ── статус: читает носитель и НИЧЕГО не запускает ────────────────────────────
# Три состояния различаются по факту носителя, а не по наличию/отсутствию
# «удобных» файлов: вердикт есть → проба была; вердикта нет → пробы не было,
# и это названо именно так, а не «не поддержан по умолчанию».
do_status() {
  local verdict="${1:-}"
  if [ -z "$verdict" ] && [ -r "$JSON_OUT" ]; then
    verdict="$(json_get "$JSON_OUT" verdict)"
  fi

  case "$verdict" in
    supported)
      echo "состояние: supported — проба была и прошла (HF_LOAD_OK + VLLM_LOAD_OK)"
      echo "носитель: $JSON_OUT"
      return 0 ;;
    unsupported)
      echo "состояние: unsupported — проба была и не прошла"
      echo "носитель: $JSON_OUT"
      return 1 ;;
    "")
      echo "состояние: not-run — пробы не было, вердикта НЕТ"
      echo "  Это «не проверено», а не «не поддержано»: ни положительного, ни"
      echo "  отрицательного вердикта у гейта сейчас нет (§1.2б плана)."
      echo "  Носителя нет вовсе: отсутствие файла — это и есть «не запускалось»"
      echo "  (заранее записанной «not-run»-записи не бывает; файл создаётся только"
      echo "  фактом прогона)."
      # Почему вердикта нет — называется фактом, если он читается. Прежний гейт
      # ждал финиша чужой лесенки; циклы ожидания в его журнале — это он и есть.
      if [ -r "$LEGACY_LOG" ]; then
        local waits probes
        waits="$(grep -c "Жду финиша лесенки Qwen3" "$LEGACY_LOG" 2>/dev/null || true)"
        probes="$(grep -c "Load-test" "$LEGACY_LOG" 2>/dev/null || true)"
        if [ "${waits:-0}" -gt 0 ] && [ "${probes:-0}" -eq 0 ]; then
          echo "  Причина (журнал прежнего гейта $LEGACY_LOG): $waits цикл(ов) ожидания"
          echo "  чужой лесенки «V4 LADDER QWEN3 COMPLETE» и ни одной строки пробы."
        fi
      fi
      echo "  Условие запуска: окно стенда ≈10 мин без живой тренировочной нагрузки."
      return 2 ;;
    *)
      echo "состояние: not-run — носитель $JSON_OUT есть, но вердикт не разобран («$verdict»)"
      return 2 ;;
  esac
}

# ── проба: единственный путь, который трогает стенд ──────────────────────────
do_run() {
  echo "== qwen35_gate: проба поддержки архитектуры qwen3_5 =="
  if [ "$LOCAL" = "1" ]; then
    echo "площадка: локальная (--local)"
  else
    echo "площадка: $HOST (ssh, BatchMode)"
  fi

  # 1. Занятость стенда — факт, а не флаг. Единственное, что может отказать.
  if [ "$IGNORE_BUSY" = "1" ]; then
    echo "занятость: НЕ проверяется (--ignore-busy — решение владельца, названо явно)"
  else
    local out rc n
    out="$(busy_sensor)"; rc=$?
    n="$(printf '%s\n' "$out" | sed -n 's/.*тренировочных нагрузок: *\([0-9]\+\).*/\1/p' | head -1)"
    if [ -z "$n" ]; then
      echo "занятость: НЕ ИЗМЕРЕНА (сенсор сериализации не дал вердикта, rc=$rc) — отказ закрыто" >&2
      printf '%s\n' "$out" | sed 's/^/     | /' | head -5 >&2
      echo "qwen35_gate: проба не запускалась (стенд не измерен); --ignore-busy — явное решение владельца" >&2
      return 2
    fi
    if [ "$n" -gt 0 ]; then
      echo "занятость: стенд ЗАНЯТ — тренировочных нагрузок $n, проба не запускалась (AD-5/AD-9)" >&2
      printf '%s\n' "$out" | grep -E '^ +' | cut -c1-150 | sed 's/^/     | /' | head -3 >&2
      echo "qwen35_gate: окно стенда занято; --ignore-busy — явное решение владельца" >&2
      return 2
    fi
    echo "занятость: окно есть (тренировочных нагрузок 0; платформенный сервер нагрузкой не считается)"
  fi

  # 2. Проба — с жёстким пределом времени: гейт не держит стенд.
  mkdir -p "$STATE_DIR"
  local rc2
  timeout --signal=TERM --kill-after=30 "$PROBE_TIMEOUT" bash -c "$(if [ "$LOCAL" = "1" ]; then printf '%s' "$PROBE_CMD"; else printf 'ssh -o BatchMode=yes -o ConnectTimeout=%s %s %q' "$CONNECT_TIMEOUT" "$HOST" "$PROBE_CMD"; fi)" > "$LOG_PATH" 2>&1
  rc2=$?
  if [ "$rc2" -eq 124 ]; then
    echo "  проба снята по таймауту ${PROBE_TIMEOUT}s (стенд не удерживается)" | tee -a "$LOG_PATH"
  fi

  # 3. Вердикт: обе строки, а не одна. Половина ответа вердиктом не считается.
  local verdict="unsupported" hf vllm where
  hf="$(grep -c "HF_LOAD_OK" "$LOG_PATH" 2>/dev/null || true)"
  vllm="$(grep -c "VLLM_LOAD_OK" "$LOG_PATH" 2>/dev/null || true)"
  if [ "${hf:-0}" -gt 0 ] && [ "${vllm:-0}" -gt 0 ]; then
    verdict="supported"
  fi
  if [ "$LOCAL" = "1" ]; then where="local"; else where="$HOST"; fi

  python3 - "$JSON_OUT" "$LOG_PATH" "$verdict" "$rc2" "$PROBE_TIMEOUT" "$where" "$CASE_ROOT" <<'PYGATE'
import hashlib, json, os, sys
out, log, verdict, rc, probe_timeout, where, root = sys.argv[1:8]
raw = open(log, "rb").read()
rel = os.path.relpath(os.path.abspath(log), root)
if rel.startswith(".."):
    rel = os.path.abspath(log)
rep = {
    "tool": "tools/qwen35_gate.sh",
    "stage": "S3bh / ADR-055 шаг 4",
    "subject": "поддержка архитектуры qwen3_5 (model_type qwen3_5) стеком transformers + vLLM",
    "model": "Qwen/Qwen3.5-0.8B-Base",
    "verdict": verdict,
    "platform": where,
    "probe": {
        "rc": int(rc),
        "timeout_s": int(probe_timeout),
        "timed_out": int(rc) == 124,
        "hf_load_ok": b"HF_LOAD_OK" in raw,
        "vllm_load_ok": b"VLLM_LOAD_OK" in raw,
    },
    "carrier": {
        "log": rel,
        "log_sha256": hashlib.sha256(raw).hexdigest(),
        "log_bytes": len(raw),
    },
    "limits": [
        "Проба отвечает на вопрос «грузится ли архитектура», а не «сходится ли обучение».",
        "Занятость стенда проверяется фактом (тренировочные нагрузки, страж сериализации AD-5), "
        "а не флагом чужого контура; платформенный сервер нагрузкой не считается.",
        "Вердикт не запускает лесенку: он её только разрешает или запрещает (ADR-055 шаг 4).",
        "Таймаут ограничивает саму пробу; он не доказывает, что за ней не осталось процессов "
        "на стенде — это проверяет страж сериализации отдельно.",
    ],
}
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w") as f:
    json.dump(rep, f, ensure_ascii=False, indent=1)
    f.write("\n")
print("вердикт записан: %s (%s, sha256 журнала %s…)"
      % (out, verdict, rep["carrier"]["log_sha256"][:16]))
PYGATE

  case "$verdict" in
    supported)   echo "состояние: supported — HF_LOAD_OK + VLLM_LOAD_OK";   return 0 ;;
    *)           echo "состояние: unsupported — см. $LOG_PATH (хвост)";    return 1 ;;
  esac
}

if [ "$MODE" = "run" ]; then
  do_run
else
  do_status
fi
