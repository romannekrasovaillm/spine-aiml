#!/usr/bin/env python3
"""S3a — детерминизм-проба: воспроизводится ли прогон по манифесту (ADR-009).

Назначение дельты — **измерить**, а не обучить. Проба отвечает на вопрос, который
до неё стоял предположением: даёт ли «тот же сид + тот же манифест» тот же
результат. Наблюдение S2 (два прогона CPT с сидом 42 разошлись с шага ~2, а к шагу
9 — вдвое) опиралось на удалённые артефакты, то есть доказательством не было.

Три прогона CPT по ``--steps`` шагов (ADR-009: 20), один сид, один конфиг:

* **A** и **B** — в детерминированном режиме: ``torch.use_deterministic_algorithms(True)``
  и ``CUBLAS_WORKSPACE_CONFIG=:4096:8``; по ADR-009 обязаны совпасть бит-в-бит,
  и это проверяется **фактом**: сравнением пошаговых loss и побитовым сравнением
  финальных чекпойнтов (``tools/compare_checkpoints.py``);
* **C** — тот же прогон **без** детерминированного режима: он раскладывает
  расхождение на «накопление недетерминизма ядер» против «различия режима».

Что измеряется: пошаговые ``loss`` всех трёх прогонов (с какого шага расходится и
насколько), ``sha256`` финальных чекпойнтов, **цена детерминизма** — время шага
против 2.569 с/шаг из S2 (тот же конфиг: Qwen2.5-0.5B, batch 1, max_len 8192,
энтропия тем же замером) и против прогона C того же дня, инциденты OOM/NVRM.

Конфиг прогонов совпадает со смоуком S2 намеренно: иначе «цена» мерила бы не
режим, а другую стадию. Отличие ровно три — число шагов (20 вместо 50), каталог
чекпойнтов (у каждого прогона свой, иначе прогоны перетирали бы друг друга) и
сам режим детерминизма.

Стенд (AD-5, ADR-012): стадии идут через ``nvrm-storm/safe_start.sh``, перед
первой стадией вызывается страж хозяина ресурса ``tools/check_resource_owner.sh``
(AD-9, ``--stage cpt``), контейнеры ``llm-platform-*`` не трогаются и замеряются
до и после. Прогоны **не удаляются**: отрицательный результат — тоже результат,
и он остаётся в ``runs/det-probe-<ts>/`` вместе с чекпойнтами.

Артефакты: ``runs/det-probe-<ts>/{a,b,c}/`` (probe_cpt.jsonl, mem_cpt.jsonl,
логи, run_cpt.sh), ``runs/det-probe-<ts>/checkpoint_compare.json``,
``runs/det-probe-<ts>/run_manifest.json`` (AD-2, C-012) и
``evidence/s3-determinism.json`` с замером и **обязательным выводом** о
воспроизводимости.

Режимы без запуска: ``--plan``, ``--preflight-only``, ``--analyze-only``
(пересборка сводки и evidence из готовых артефактов), ``--stop-only`` (закрыть
контейнеры пробы по именам — план отката).

Коды возврата::

    0 — проба прошла (или запрошенный режим без запуска отработал)
    1 — предусловие нарушено / стадия упала / инцидент в замерах
    2 — NOT-VERIFIED: стенд недоступен или артефакт отсутствует

Запуск::

    python3 tools/run_det_probe.py --plan
    python3 tools/run_det_probe.py --preflight-only
    python3 tools/run_det_probe.py                    # три прогона по 20 шагов
    python3 tools/run_det_probe.py --analyze-only --ts 20260914-1500
    python3 tools/run_det_probe.py --stop-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Транспорт, разбор замеров и сэмплер памяти — общие с S2/S3-pre: своя вторая
#: реализация ssh-доставки разошлась бы с первой на первой же правке. Своё здесь
#: только то, что относится к детерминизму: три прогона, режим, сравнение.
import run_smoke as S  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "gb10-fast"
DEFAULT_IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"

STAND_SHARED = "/home/user/gb10-shared"
STAND_EXPERIMENTS = "/home/user/experiments"
SAFE_START = f"{STAND_SHARED}/nvrm-storm/safe_start.sh"
CTR_SHARED = "/workspace/shared"
CTR_EXPERIMENTS = "/workspace/experiments"
PIPELINE_CTR = f"{CTR_SHARED}/laguna_pipeline_v8.py"

CPT_DATA = f"{CTR_SHARED}/datasets/cpt_corpus_v12r.txt"
SFT_DATA = f"{CTR_SHARED}/datasets/sft_train_v12.jsonl"
RL_DATA = f"{CTR_SHARED}/datasets/rl_tasks_oxalpha.jsonl"
EVAL_DATA = f"{CTR_SHARED}/datasets/eval_ood_clean.jsonl"
CPT_TOK_CACHE = "datasets/tok/cpt_corpus_v12r_8192_qwen25.npy"

RUN_PREFIX = "det-probe-compact"
CONTAINER_PREFIX = "laguna-det-"
PLATFORM_PREFIX = "llm-platform-"

#: Три прогона пробы: (буква, детерминированный режим). Порядок — A, B, C:
#: сначала пара, которая обязана совпасть, затем недетерминированный прогон.
RUNS: tuple[tuple[str, bool], ...] = (("a", True), ("b", True), ("c", False))

#: Время шага CPT из замера S2 (`evidence/s2-smoke.json`: 50 шагов CPT, батч 1,
#: max_len 8192, интервалы с GEN-EVAL исключены). С ним сравнивается цена
#: детерминизма — задача ставит именно это сравнение.
STEP_SECONDS_S2 = 2.569

#: Порог памяти CPT (ADR-008 п.2) — тот же, что у стража хозяина ресурса.
MIN_FREE_GB_CPT = 35

#: Модель, сид и длина — ADR-002/ADR-009; шаги — 20 (ADR-009 п.3).
DEFAULT_STEPS = 20
MIN_STEPS, MAX_STEPS = 10, 50

#: Наблюдение S2, записанное в ADR-009 (Context): «с шага ~2 траектории расходятся»
#: (артефакты того прогона удалены, поэтому это наблюдение, а не доказательство).
#: Проба даёт этому числу замер, и совпадение проверяется, а не подразумевается.
S2_OBSERVED_ONSET_STEP = 2

#: Календарная арифметика цены: CPT по замеру ADR-008 (9776 шагов × 2.569 с = 7.0 ч
#: на сид), полный цикл ревизии по ADR-011 (≈10.0–10.7 суток). Это **не замер**
#: календаря, а пересчёт измеренного процента — и названо так же честно.
CPT_HOURS_PER_SEED = 7.0
CYCLE_DAYS = (10.0, 10.7)

PROGRESS_RE = re.compile(
    r"STAGE:|^\S+ \[INFO\] (CPT|SFT|RL|EVAL) |CKPT:|PROBES|GEN-EVAL|resume|RESUME|"
    r"Traceback|Error|error|OOM|Killed|NOT-VERIFIED|DET_PROBE_")


# ─── имена и пути ─────────────────────────────────────────────────────────────

def run_id_for(ts: str) -> str:
    return f"{RUN_PREFIX}-{ts}"


def exp_name_for(letter: str, ts: str) -> str:
    return f"det-probe-{letter}-{ts}"


def container_name(ts: str, letter: str) -> str:
    return f"{CONTAINER_PREFIX}{ts}-{letter}"


def deterministic_letter(letter: str) -> bool:
    return dict(RUNS)[letter]


# ─── предусловия ──────────────────────────────────────────────────────────────

def check_resource_owner(host: str) -> dict:
    """AD-9: право стартовать, а не «здоровье стенда» (tools/check_resource_owner.sh).

    Проба тренировочная — значит, первым делом проверяется хозяин ресурса. Страж
    вызывается **до** стадий: инцидент 14.09.2026 состоял ровно в том, что
    непроверенный хозяин обнаружился уже после старта.
    """
    script = CASE_ROOT / "tools" / "check_resource_owner.sh"
    cmd = ["bash", str(script), "--host", host, "--stage", "cpt", "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"name": "owner", "ok": False, "detail": f"страж AD-9 не запустился: {e}"}
    report: dict = {}
    for line in reversed(p.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                report = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    ok = p.returncode == 0
    return {"name": "owner", "ok": ok, "exit": p.returncode, "report": report,
            "detail": (f"AD-9 OK: {report.get('verdict', 'CAN-START')}"
                       if ok else
                       f"страж AD-9 красный (exit {p.returncode}): "
                       f"{report.get('verdict', 'нет машинного вердикта')}"),
            "output": p.stdout.strip().splitlines()[-6:]}


def stand_instrument_pins(host: str, ts: str) -> dict:
    """Хеши инструментов, лежащих в каталоге прогона **на стенде**.

    Это ответ на вопрос «чем именно измеряли»: файлы кейса могли быть отредактированы
    после прогона (и тогда их хеш описывает не прогон, а текущую ревизию), а файлы в
    каталоге прогона — те самые, что исполнились. Возвращаются и те, и другие; где
    они расходятся, это сказано явно.
    """
    run_root = f"{STAND_EXPERIMENTS}/{run_id_for(ts)}"
    names = ("smoke_probe.py", "smoke_mem_sampler.sh", "compare_checkpoints.py")
    rc, out, err = S.ssh(host, "sha256sum " + " ".join(f"{run_root}/{n}" for n in names))
    if rc != 0:
        return {"available": False, "detail": f"sha256sum на стенде: {err.strip()}"}
    stand = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            stand[Path(parts[1]).name] = parts[0]
    case = {}
    for n in names:
        case[n] = S.sha256_path(CASE_ROOT / "tools" / n)
    return {"available": True, "stand": stand, "case": case,
            "case_matches_stand": stand == case,
            "note": ("stand — файлы в каталоге прогона на стенде (то, что исполнялось); "
                     "case — файлы кейса сейчас; расхождение означает, что инструмент "
                     "кейса правился после прогона, и говорит это прямо, а не выдаёт "
                     "текущий хеш за хеш прогона")}


def preflight(host: str, args) -> dict:
    """Полный прогон предусловий. ``ok`` — можно ли запускать прогоны пробы."""
    checks: list[dict] = []
    rc, out, err = S.ssh(host, "hostname")
    reachable = rc == 0
    checks.append({"name": "host", "ok": reachable,
                   "detail": (f"{host} → {out.strip()}" if reachable
                              else f"{host} недоступен: {err.strip()}")})
    if not reachable:
        return {"ok": False, "checks": checks, "host": host}
    checks.append(check_resource_owner(host))
    checks.append(S.check_free_memory(host, args.min_free_gb))
    checks.append(S.check_platform_containers(host))
    checks.append(S.check_serialization_guard(host, strict=True))
    checks.append(S.check_image(host, args.image))
    checks.append(S.check_data(host, args.attn))
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "host": host,
            "dmesg_before": S.dmesg_counts(host)}


# ─── сценарий стадии ──────────────────────────────────────────────────────────

def build_stage_script(*, ts: str, letter: str, args) -> str:
    """Скрипт одного прогона пробы: сэмплер памяти + safe_start + docker run + обвязка.

    Скрипт кладётся на стенд целиком: прогон должен повторяться вручную без
    реконструкции строки запуска из логов раннера, а режим детерминизма — быть
    видимым в самом скрипте, а не только в отчёте.
    """
    det = deterministic_letter(letter)
    run_id = run_id_for(ts)
    host_root = f"{STAND_EXPERIMENTS}/{run_id}"
    ctr_root = f"{CTR_EXPERIMENTS}/{run_id}"
    host_run_dir = f"{host_root}/{letter}"
    ctr_run_dir = f"{ctr_root}/{letter}"
    ctr_name = container_name(ts, letter)
    exp_name = exp_name_for(letter, ts)
    ckpt_dir = f"{ctr_run_dir}/checkpoints"
    log_dir = f"{ctr_run_dir}/logs"
    #: Деterministic-режим на уровне env: CUBLAS_WORKSPACE_CONFIG читается cuBLAS
    #: при создании handle, то есть должен быть в окружении **до** первого матмула.
    #: У прогона C переменной нет намеренно — иначе «без режима» означало бы «без
    #: одной настройки из трёх» и раскладка расхождения была бы неполной.
    det_env = ("  -e CUBLAS_WORKSPACE_CONFIG=:4096:8 \\\n" if det else "")
    probe_args = " ".join([
        f"--model_name {args.model}", "--stage cpt", f"--exp_name {exp_name}",
        f"--max_steps {args.steps}", f"--max_samples {args.max_samples}",
        f"--batch_size {args.batch_size}", f"--max_len {args.max_len}",
        f"--seed {args.seed}", f"--peak_lr_scale {args.peak_lr_scale}",
        f"--cpt_data {CPT_DATA}", f"--sft_data {SFT_DATA}",
        f"--rl_data {RL_DATA}", f"--eval_data {EVAL_DATA}",
        f"--ckpt_dir {ckpt_dir}", f"--log_dir {log_dir}",
    ])
    return f"""#!/usr/bin/env bash
# Детерминизм-проба S3a, прогон {letter} (детерминированный режим: {"on" if det else "off"}).
# Сгенерировано tools/run_det_probe.py (ts={ts}).
# Ручное повторение: bash {host_run_dir}/run_cpt.sh
set -uo pipefail
RUN_DIR="{host_run_dir}"          # хост-путь (сэмплер памяти, лог, tee, sha256)
CTR_RUN_DIR="{ctr_run_dir}"       # тот же каталог глазами контейнера
RUN_ROOT="{host_root}"
CTR="{ctr_name}"
LOG="$RUN_DIR/logs/cpt.log"
METRICS="$CTR_RUN_DIR/probe_cpt.jsonl"
MEM="$RUN_DIR/mem_cpt.jsonl"
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/checkpoints"
# Обвязка и сэмплер лежат в корне прогона — общие для трёх прогонов пробы.
cp -f "$RUN_ROOT/smoke_probe.py" "$RUN_ROOT/smoke_mem_sampler.sh" "$RUN_DIR/" 2>/dev/null || true

bash "$RUN_DIR/smoke_mem_sampler.sh" "$MEM" 5 {args.sampler_seconds} &
SAMPLER=$!
trap 'kill "$SAMPLER" 2>/dev/null' EXIT

echo "DET_PROBE_STAGE_START={letter} deterministic={"1" if det else "0"} ts=$(date -Is)"
bash {SAFE_START} -d 60 -i 5 -- docker run --rm --name "$CTR" \\
  --gpus all --ipc=host --pid=host \\
  --memory=100g --memory-swap=100g \\
  --security-opt seccomp=unconfined --cap-add SYS_PTRACE \\
  --ulimit memlock=-1 --ulimit stack=67108864 --ulimit nofile=262144:262144 \\
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -e HF_HUB_OFFLINE=1 \\
  -e TRANSFORMERS_OFFLINE=1 -e LAGUNA_MEM_FRACTION={args.mem_fraction} \\
  -e VLLM_ALLOW_INSECURE_SERIALIZATION=1 -e PYTHONPATH=/workspace/shared/wt_stubs \\
  -e CPT_GEN_EVAL_EVERY={args.gen_eval_every} \\
{det_env}  -e LAGUNA_ATTN={args.attn} -e FLEX_COMPILE=0 \\
  -v {STAND_EXPERIMENTS}:{CTR_EXPERIMENTS} \\
  -v {STAND_SHARED}:{CTR_SHARED} \\
  -v /home/user/.cache/huggingface:/root/.cache/huggingface -w /workspace \\
  {args.image} \\
  python3 "$CTR_RUN_DIR/smoke_probe.py" \\
    --metrics "$METRICS" --stage cpt --exp-name "{exp_name}" \\
    --deterministic {"on" if det else "off"} \\
    --entropy {args.entropy} --entropy-every {args.entropy_every} \\
    --entropy-pos-stride {args.entropy_pos_stride} \\
    --pipeline {PIPELINE_CTR} \\
    -- {probe_args} \\
  2>&1 | tee "$LOG"
RC=${{PIPESTATUS[0]}}
kill "$SAMPLER" 2>/dev/null
# sha256 финального чекпойнта — на стенде: файл ~3 ГБ, копировать его в кейс
# нельзя (AD-4), а доказательству нужен именно хеш артефакта, а не лога.
if [ -f "$RUN_DIR/checkpoints/checkpoint_final.pt" ]; then
  echo "DET_PROBE_CKPT_SHA256={letter} $(sha256sum "$RUN_DIR/checkpoints/checkpoint_final.pt" | cut -d' ' -f1)"
  echo "DET_PROBE_CKPT_BYTES={letter} $(stat -c %s "$RUN_DIR/checkpoints/checkpoint_final.pt")"
fi
echo "DET_PROBE_STAGE_EXIT={letter} $RC"
exit "$RC"
"""


def render_plan(args) -> str:
    """План пробы: что будет запущено, в каком режиме и что из этого следует."""
    run_id = run_id_for(args.ts)
    lines = [
        "== S3a: план детерминизм-пробы (ADR-009) ==",
        f"стенд:            {args.host} (ssh), образ: {args.image}",
        f"каталог прогона:  стенд {STAND_EXPERIMENTS}/{run_id}/{{a,b,c}}  ←  кейс "
        f"{args.runs_dir}/det-probe-{args.ts}/",
        f"модель/сид/шаги:  {args.model}, seed={args.seed}, шагов {args.steps} "
        f"(ADR-009: 20), max_len={args.max_len}, batch={args.batch_size}",
        f"корпус:           {CPT_TOK_CACHE} (тот же кэш, что в S2)",
        "",
        "прогоны:",
    ]
    for letter, det in RUNS:
        lines.append(
            f"  {letter}: --exp_name {exp_name_for(letter, args.ts)}  "
            f"режим: {'детерминированный (use_deterministic_algorithms + CUBLAS_WORKSPACE_CONFIG=:4096:8)' if det else 'без детерминированного режима (режим контура)'}")
    lines += [
        "",
        f"предусловия:      страж хозяина ресурса AD-9 (tools/check_resource_owner.sh "
        f"--stage cpt), свободной unified-памяти ≥ {args.min_free_gb} ГБ, страж AD-5 "
        f"в --strict, llm-platform-* не трогаются",
        f"запуск стадии:    {SAFE_START} -d 60 -i 5 -- docker run (absorbер включён)",
        "",
        "замеры:           пошаговые loss трёх прогонов (с какого шага расходится и "
        "насколько), sha256 и побитовое сравнение чекпойнтов, цена детерминизма "
        f"(время шага против {STEP_SECONDS_S2} с/шаг из S2 и против прогона C), инциденты",
        f"артефакты:        {args.runs_dir}/det-probe-{args.ts}/ (манифест AD-2, логи, "
        f"probe_cpt.jsonl, mem_cpt.jsonl, checkpoint_compare.json, чекпойнты — на стенде) "
        f"+ {args.evidence}",
        "",
        "вывод:            обязателен — воспроизводится ли прогон по манифесту после "
        "включения детерминизма (да/нет/частично) и что это значит для A5-drift",
        "чего проба НЕ делает: не запускает полный протокол S3, не трогает чужие "
        "чекпойнты и контейнеры llm-platform-*, не восстанавливает сторож лесенки",
    ]
    return "\n".join(lines)


# ─── исполнение ───────────────────────────────────────────────────────────────

def ship_instruments(host: str, run_dir_stand: str) -> dict:
    """Обвязка, сэмплер и компаратор — в корень прогона (общие для трёх прогонов)."""
    shipped = {}
    for name, payload in (
            ("smoke_probe.py", (CASE_ROOT / "tools" / "smoke_probe.py").read_bytes()),
            ("smoke_mem_sampler.sh",
             (CASE_ROOT / "tools" / "smoke_mem_sampler.sh").read_bytes()),
            ("compare_checkpoints.py",
             (CASE_ROOT / "tools" / "compare_checkpoints.py").read_bytes())):
        rc, err = S.ssh_put(host, f"{run_dir_stand}/{name}", payload)
        if rc != 0:
            return {"ok": False, "error": f"доставка {name} на стенд не удалась: {err.strip()}"}
        shipped[name] = S.sha256_bytes(payload)
    # Сверка доставки: инструмент на стенде обязан быть байт-в-байт тем, что в кейсе.
    rc, out, err = S.ssh(host, "sha256sum " + " ".join(f"{run_dir_stand}/{n}" for n in shipped))
    if rc != 0:
        return {"ok": False, "error": f"sha256sum на стенде: {err.strip()}"}
    broken = [line.split()[1] for line in out.splitlines()
              if len(line.split()) == 2 and shipped.get(Path(line.split()[1]).name)
              != line.split()[0]]
    if broken or len(out.splitlines()) != len(shipped):
        return {"ok": False, "error": f"инструменты на стенде не совпали с отправленными: {broken or out}"}
    return {"ok": True, "sha256": shipped}


def run_one(host: str, args, letter: str, local_dir: Path) -> dict:
    """Один прогон пробы: доставка, запуск, поток лога, возврат артефактов."""
    ts, det = args.ts, deterministic_letter(letter)
    run_id = run_id_for(ts)
    run_dir = f"{STAND_EXPERIMENTS}/{run_id}/{letter}"
    script = build_stage_script(ts=ts, letter=letter, args=args)
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "run_cpt.sh").write_text(script, encoding="utf-8")
    rc, err = S.ssh_put(host, f"{run_dir}/run_cpt.sh", script.encode())
    if rc != 0:
        return {"run": letter, "ok": False,
                "error": f"доставка run_cpt.sh на стенд не удалась: {err.strip()}"}

    log_path = local_dir / "logs" / "cpt.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run_det_probe] прогон {letter} (детерминированный режим: "
          f"{'on' if det else 'off'}): запуск на {host}, лог → {log_path}")
    t0 = time.time()
    rc_stage = None
    ckpt_sha = None
    ckpt_bytes = None
    log_text: list[str] = []
    try:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", host, f"bash {run_dir}/run_cpt.sh"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        return {"run": letter, "ok": False, "error": f"ssh не запустился: {e}"}

    with log_path.open("w", encoding="utf-8") as fh:
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            fh.flush()
            log_text.append(line)
            m = re.search(rf"DET_PROBE_STAGE_EXIT={letter} (\d+)", line)
            if m:
                rc_stage = int(m.group(1))
            m = re.search(rf"DET_PROBE_CKPT_SHA256={letter} ([0-9a-f]{{64}})", line)
            if m:
                ckpt_sha = m.group(1)
            m = re.search(rf"DET_PROBE_CKPT_BYTES={letter} (\d+)", line)
            if m:
                ckpt_bytes = int(m.group(1))
            if PROGRESS_RE.search(line):
                print("    " + line.rstrip()[:200], flush=True)
    proc.wait(timeout=60)
    wall = time.time() - t0

    result = {"run": letter, "exp_name": exp_name_for(letter, ts), "det": det,
              "exit": rc_stage, "ssh_exit": proc.returncode, "wall_seconds": round(wall, 1),
              "script_sha256": S.sha256_bytes(script.encode()),
              "checkpoint_sha256": ckpt_sha, "checkpoint_bytes": ckpt_bytes,
              "checkpoint_sha256_source":
                  ("sha256sum на стенде по финальному чекпойнту (файл ~3 ГБ в кейс не копируется, AD-4)"
                   if ckpt_sha else None),
              "local_log": str(log_path), "ok": rc_stage == 0,
              "incidents_in_log": S.scan_incidents("".join(log_text))}
    if rc_stage is None:
        result["ok"] = False
        result["error"] = f"прогон не напечатал DET_PROBE_STAGE_EXIT (ssh rc={proc.returncode})"

    # Контейнер закрываем всегда: проба не оставляет за собой нагрузку (AD-5).
    name = container_name(ts, letter)
    rc, out, _ = S.ssh(host, f"docker ps -q -f name=^{name}$")
    if rc == 0 and out.strip():
        rc2, out2, err2 = S.ssh(host, f"docker stop {name}", timeout=180)
        result["container_stop"] = {"rc": rc2, "detail": out2.strip() or err2.strip()}

    for remote, local in (("probe_cpt.jsonl", "probe_cpt.jsonl"),
                          ("mem_cpt.jsonl", "mem_cpt.jsonl"),
                          ("logs/probes_cpt.json", "probes_cpt.json"),
                          ("general_eval_history.jsonl", "general_eval_history.jsonl"),
                          ("checkpoints/run_manifest.json", "pipeline_run_manifest.json")):
        rc, data, err = S.ssh_get(host, f"{run_dir}/{remote}", timeout=300)
        if rc == 0 and data:
            (local_dir / local).write_bytes(data)
            result[f"fetched_{local}"] = len(data)
        else:
            result.setdefault("missing_artifacts", []).append(remote)
    return result


def compare_checkpoints_on_stand(host: str, args) -> dict:
    """Побитовое сравнение финальных чекпойнтов — по существу, а не по sha256 файла.

    Запускается в том же образе, но **без GPU**: сравнение — это чтение двух
    словарей тензоров и байтовое сравнение, GPU-нагрузки в нём нет, и в очередь
    AD-5 оно не встаёт. Контейнер без ``--gpus`` не может приблизить NVRM-инцидент,
    от которого инвариант защищает.
    """
    run_id = run_id_for(args.ts)
    ctr_root = f"{CTR_EXPERIMENTS}/{run_id}"
    host_root = f"{STAND_EXPERIMENTS}/{run_id}"
    pairs = [("a", "b"), ("a", "c"), ("b", "c")]
    out: dict = {"pairs": {}, "runs": {}, "note":
                 "сравнение в образе прогона без GPU (чтение чекпойнтов и байтовое "
                 "сравнение тензоров); sha256 файла приведён рядом, но вердикт "
                 "выносится по значениям тензоров: torch.save кладёт их в "
                 "zip-контейнер, и разные байты файла не означают разные числа"}
    for a, b in pairs:
        cmd = (f"docker run --rm -v {STAND_EXPERIMENTS}:{CTR_EXPERIMENTS} -w /workspace "
               f"{args.image} python3 {ctr_root}/compare_checkpoints.py "
               f"--a {ctr_root}/{a}/checkpoints/checkpoint_final.pt "
               f"--b {ctr_root}/{b}/checkpoints/checkpoint_final.pt --json")
        rc, stdout, stderr = S.ssh(host, cmd, timeout=900)
        report: dict = {}
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    report = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        key = f"{a}_vs_{b}"
        out["pairs"][key] = {"exit": rc, "report": report,
                             "stderr_tail": stderr.strip().splitlines()[-3:] if rc != 0 else []}
        if not report and rc != 0:
            out["pairs"][key]["error"] = "сравнение не состоялось"
    #: Хеши и размеры чекпойнтов — из каталогов прогона (без чтения файлов в кейс).
    for letter, _ in RUNS:
        rc, stdout, _ = S.ssh(
            host, f"sha256sum {host_root}/{letter}/checkpoints/checkpoint_final.pt; "
                  f"stat -c %s {host_root}/{letter}/checkpoints/checkpoint_final.pt")
        lines = [l.strip() for l in stdout.splitlines() if l.strip()]
        out["runs"][letter] = {
            "sha256": lines[0].split()[0] if lines and len(lines[0].split()) == 2 else None,
            "bytes": int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None}
    return out


# ─── разбор замеров (чистые функции — тестируются без стенда) ──────────────────

def losses_of(summary: dict) -> list[float]:
    """Пошаговые loss прогона в порядке шагов (по записанной точности 1e-6).

    Ряд кладёт ``summarize_run``: у сводки S2 ``steps`` — это **число** шагов, и
    читать из неё ряд нельзя (проверено на синтетике — именно так теряются шаги).
    """
    series = (summary or {}).get("loss_series")
    return list(series) if series else []


def pair_divergence(name_a: str, xs: list[float], name_b: str, ys: list[float]) -> dict:
    """С какого шага и насколько расходятся два набора пошаговых loss.

    Сравнение — по записанной обвязкой точности (6 знаков): это то, что осталось
    в доказательной базе. Полная точность проверяется отдельно — побитовым
    сравнением чекпойнтов, где округления нет вовсе.
    """
    n = min(len(xs), len(ys))
    out: dict = {"a": name_a, "b": name_b, "steps_compared": n,
                 "steps_a": len(xs), "steps_b": len(ys),
                 "loss_first_a": xs[0] if xs else None, "loss_first_b": ys[0] if ys else None,
                 "loss_last_a": xs[-1] if xs else None, "loss_last_b": ys[-1] if ys else None,
                 "precision": "loss записан с точностью 1e-6 (обвязка tools/smoke_probe.py)"}
    if n == 0:
        out.update({"verdict": "NO-DATA", "identical": None,
                    "note": "нет пошаговых замеров хотя бы у одного прогона"})
        return out
    divergent = [i for i in range(n) if xs[i] != ys[i]]
    out["identical"] = not divergent
    out["divergent_steps"] = len(divergent)
    out["first_divergent_step"] = divergent[0] if divergent else None
    if divergent:
        i = divergent[0]
        out["first_divergence"] = {
            "step": i, "a": xs[i], "b": ys[i],
            "abs": round(abs(xs[i] - ys[i]), 6),
            "rel": (round(abs(xs[i] - ys[i]) / abs(xs[i]), 6) if xs[i] else None)}
    diffs = [abs(xs[i] - ys[i]) for i in range(n)]
    out["max_abs_diff"] = round(max(diffs), 6) if diffs else 0.0
    out["max_abs_diff_step"] = int(max(range(n), key=lambda i: diffs[i])) if diffs else None
    rels = [abs(xs[i] - ys[i]) / abs(xs[i]) for i in range(n) if xs[i]]
    out["max_rel_diff"] = round(max(rels), 6) if rels else None
    out["verdict"] = "BIT-IDENTICAL" if out["identical"] else "DIVERGED"
    return out


def determinism_price(det_seconds: float | None, base_seconds: float | None,
                      base_label: str, base_value: float | None = None) -> dict:
    """Цена детерминизма в процентах к базе (и абсолютные числа рядом).

    Проценты без абсолютных чисел бесполезны: «+12%» при 2.5 с и при 25 с — разные
    решения. Поэтому в отчёте и то, и другое, а база названа явно.
    """
    out: dict = {"deterministic_step_seconds": det_seconds, "base_label": base_label,
                 "base_step_seconds": base_value if base_value is not None else base_seconds}
    if det_seconds is None or out["base_step_seconds"] in (None, 0):
        out["percent"] = None
        out["note"] = "цена не посчитана: нет одного из времён шага"
        return out
    pct = (det_seconds / out["base_step_seconds"] - 1.0) * 100.0
    out["percent"] = round(pct, 2)
    out["absolute_seconds"] = round(det_seconds - out["base_step_seconds"], 4)
    return out


def price_calendar(percent: float | None) -> dict:
    """Пересчёт цены режима в календарь ревизии (ADR-008 / ADR-011) — арифметика.

    Замер сделан на 20 шагах CPT; календарь протокола — 9776 шагов на сид и
    ~10 суток на 3 сида. Пересчёт разделён на два блока: «только CPT» (там, где
    цена измерена) и «если та же цена на всех стадиях» (там, где она **не**
    измерена — SFT/RL пробой не покрыты).
    """
    if percent is None:
        return {"percent": None, "note": "цена не измерена — календарь не пересчитывается"}
    factor = 1.0 + percent / 100.0
    extra_stage_days = 3.0 * CPT_HOURS_PER_SEED * (factor - 1.0) / 24.0
    return {
        "basis": ("ADR-008 п.2: CPT 9776 шагов × 2.569 с = 7.0 ч/сид, CPT+SFT на 3 сида "
                  "≈ 8.5 сут; ADR-011: полный цикл ≈ 10.0–10.7 сут"),
        "measured_stage": "CPT (20 шагов); SFT/RL пробой не покрыты",
        "factor": round(factor, 4),
        "cpt_hours_per_seed_nondet": CPT_HOURS_PER_SEED,
        "cpt_hours_per_seed_det": round(CPT_HOURS_PER_SEED * factor, 1),
        "cycle_days_if_det_only_cpt": [round(CYCLE_DAYS[0] + extra_stage_days, 1),
                                       round(CYCLE_DAYS[1] + extra_stage_days, 1)],
        "cycle_days_if_same_price_all_stages": [round(CYCLE_DAYS[0] * factor, 1),
                                                round(CYCLE_DAYS[1] * factor, 1)],
        "note": ("пересчёт измеренного процента, а не замер календаря; второй блок "
                 "помечен отдельно, потому что цена на SFT/RL не измерялась"),
    }


def divergence_onset_cross_check(divergence: dict, losses: dict) -> dict:
    """Сверка замера с наблюдением S2 (ADR-009 Context): «расхождение с шага ~2»."""
    ac = divergence.get("a_vs_c", {})
    onset = ac.get("first_divergent_step")
    a, c = losses.get("a") or [], losses.get("c") or []
    head = 0
    while head < min(len(a), len(c)) and a[head] == c[head]:
        head += 1
    return {
        "s2_observation": ("ADR-009 Context: при одном сиде loss шага 0 совпадал бит-в-бит, "
                           "с шага ~2 траектории расходились (наблюдение; артефакты удалены)"),
        "s2_observed_onset_step": S2_OBSERVED_ONSET_STEP,
        "measured_first_divergent_step": onset,
        "identical_head_steps": head,
        "match": onset == S2_OBSERVED_ONSET_STEP,
        "note": ("наблюдение S2 теперь либо подтверждено замером, либо опровергнуто; "
                 "проба не «ссылается» на него как на доказательство"),
    }


def verdict_of(runs: dict, divergence: dict, ckpt: dict, price: dict,
               steps_adr: int) -> dict:
    """Обязательный вывод пробы: воспроизводится ли прогон по манифесту.

    Вывод строится **только из замеров**: пара A/B (детерминированный режим)
    против пары с C (режим контура). Если A/B совпали по loss и по чекпойнтам —
    детерминизм воспроизводим; если совпали по одному и разошлись по другому —
    это «частично», и это надо назвать, а не округлить в удобную сторону.
    """
    ab = divergence.get("a_vs_b", {})
    ab_ckpt = ((ckpt.get("pairs") or {}).get("a_vs_b") or {}).get("report") or {}
    ab_loss_same = ab.get("identical")
    ab_ckpt_same = ab_ckpt.get("identical")
    ac = divergence.get("a_vs_c", {})
    bc = divergence.get("b_vs_c", {})

    if ab_loss_same is None or ab_ckpt_same is None:
        answer, why = "не определён", "нет полных замеров пары A/B (loss или чекпойнты)"
    elif ab_loss_same and ab_ckpt_same:
        answer = "да"
        why = ("пара A/B в детерминированном режиме совпала: пошаговые loss идентичны "
               f"({ab.get('steps_compared')} шагов) и тензоры финальных чекпойнтов "
               "совпали побитово")
    elif ab_loss_same or ab_ckpt_same:
        answer = "частично"
        why = (f"A/B: loss {'совпали' if ab_loss_same else 'разошлись'}, "
               f"чекпойнты {'совпали побитово' if ab_ckpt_same else 'разошлись'}")
    else:
        answer = "нет"
        why = ("пара A/B разошлась даже в детерминированном режиме: "
               f"первый расходящийся шаг {ab.get('first_divergent_step')}, "
               f"max|Δloss| {ab.get('max_abs_diff')}, "
               f"расхождений {ab.get('divergent_steps')} из {ab.get('steps_compared')}")

    det_ab_same = bool(ab_loss_same and ab_ckpt_same)
    ac_diff = (ac.get("identical") is False)
    bc_diff = (bc.get("identical") is False)
    base = [
        f"пара A/B (детерминированный режим, {steps_adr} шагов, сид один): "
        f"loss {'идентичны' if ab_loss_same else 'расходятся'} "
        f"(первый расходящийся шаг: {ab.get('first_divergent_step')}); "
        f"чекпойнты {'побитово идентичны' if ab_ckpt_same else 'различаются'}",
        f"A/C и B/C (детерминированный против режима контура): "
        f"{'расходятся' if (ac_diff or bc_diff) else 'не расходятся'} "
        f"(A/C первый расходящийся шаг {ac.get('first_divergent_step')}, "
        f"B/C {bc.get('first_divergent_step')})",
        f"цена детерминизма: {price.get('percent')}% к {price.get('base_label')}",
    ]

    if answer == "да":
        consequence = (
            "A5-drift в формулировке «повторить прогон по манифесту и сравнить артефакты» "
            "выполним **только в детерминированном режиме**: он даёт совпадение и loss, и "
            "тензоров чекпойнта. Прогоны без детерминированного режима (и все исторические "
            "прогоны контура) для доказательства воспроизводимости не годятся — их "
            "расхождение измерено в паре с A/C. Для оси AD-1 это меняет немного, и это важно "
            "сказать прямо: совпадение пары A/B — не статистика. Запрет ADR-009 п.2 на выводы "
            "по одиночным прогонам остаётся в силе (два прогона с одним сидом — не k сидов), "
            "мера успеха по-прежнему mean±std и k-из-k по трём сидам. Детерминизм даёт другое: "
            "он делает PoC воспроизводимым — один сид можно повторить и получить тот же "
            "чекпойнт, а значит, расхождение между сидами перестаёт смешиваться с "
            "недетерминизмом вычислений. Ограничение по цене: если детерминизм обязателен для "
            "A4/A5, время стадии растёт на измеренный процент — это решение владельца "
            "(ADR-009 п.4), а не тихий выбор исполнителя.")
    elif answer == "частично":
        consequence = (
            "A5-drift нельзя закрыть ни хешами, ни сравнением траекторий: детерминированный "
            "режим совпал по одной величине и разошёлся по другой. Пока расхождение не "
            "разложено (какая именно часть контура недетерминирована), корректная форма "
            "A5-drift — сравнение распределений по k сидам (ADR-009 п.4), а «воспроизвели "
            "по манифесту» остаётся недоказанным.")
    elif answer == "нет":
        consequence = (
            "Детерминированный режим не воспроизводит прогон: A5-drift в форме сравнения "
            "хешей **невыполним** — ни в детерминированном режиме, ни в режиме контура. "
            "Следствие по ADR-009 п.4: детерминированный режим как обязательный для A4/A5 "
            "не вводится (он не даёт того, ради чего вводился), а A5-drift переформулируется "
            "в сравнение распределений по k сидам. Дополнительный вопрос к владельцу — цена "
            "режима, который не даёт воспроизводимости.")
    else:
        consequence = ("Вывод не построен: в замерах нет полной пары A/B. Проба не "
                       "принимается — вопрос воспроизводимости остаётся открытым.")

    pct = price.get("percent")
    price_note = None
    if pct is not None:
        price_note = (f"цена режима измерена: {pct:+}% к «{price.get('base_label')}» "
                      f"({price.get('deterministic_step_seconds')} с против "
                      f"{price.get('base_step_seconds')} с на шаг). Решение о том, "
                      f"становится ли режим обязательным для A4/A5-прогонов, ADR-009 п.4 "
                      f"относит к владельцу (новый ADR) — исполнитель его не принимает; "
                      f"здесь только замер и его арифметика")
    return {"question": ("воспроизводится ли прогон по манифесту после включения "
                         "детерминизма"),
            "answer": answer, "basis": base,
            "det_pair_identical": det_ab_same,
            "nondet_pair_differs": bool(ac_diff or bc_diff),
            "consequence_for_a5_drift": consequence,
            "price_note": price_note}


def summarize_run(local_sub: Path, letter: str, args) -> dict:
    """Сводка одного прогона из его артефактов (тот же разбор, что в S2)."""
    probe = local_sub / "probe_cpt.jsonl"
    if not probe.is_file():
        return {"run": letter, "error": f"нет {probe.name}"}
    parsed = S.parse_probe(probe)
    log_path = local_sub / "logs" / "cpt.log"
    wall = None
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
    else:
        text = ""
    summary = S.summarize_steps(parsed["steps"], parsed["end"], wall)
    summary["run"] = letter
    #: Ряд loss — отдельным полем: в сводке S2 `steps` это число шагов, и
    #: положиться на него как на ряд нельзя (см. losses_of).
    summary["loss_series"] = [round(float(s["loss"]), 6) for s in parsed["steps"]
                              if s.get("loss") is not None]
    summary["step_seconds_series"] = [s.get("dt_step") for s in parsed["steps"]]
    summary["deterministic_mode"] = parsed["start"].get("determinism")
    mem = local_sub / "mem_cpt.jsonl"
    if mem.is_file():
        summary["memory"] = S.parse_mem_samples(mem, container_hint=container_name(args.ts, letter))
    summary["incidents_in_log"] = S.scan_incidents(text)
    return summary


def analyze(run_dir: Path, args) -> dict:
    """Сводка по трём прогонам: замеры, расхождения, цена, инциденты."""
    per_run: dict[str, dict] = {}
    for letter, _ in RUNS:
        per_run[letter] = summarize_run(run_dir / letter, letter, args)

    losses = {k: losses_of(v) for k, v in per_run.items()}
    divergence = {
        "a_vs_b": pair_divergence("a", losses["a"], "b", losses["b"]),
        "a_vs_c": pair_divergence("a", losses["a"], "c", losses["c"]),
        "b_vs_c": pair_divergence("b", losses["b"], "c", losses["c"]),
    }

    def step_seconds(letter: str) -> float | None:
        return (per_run[letter] or {}).get("step_seconds_mean")

    det_mean = None
    det_values = [v for v in (step_seconds("a"), step_seconds("b")) if v is not None]
    if det_values:
        det_mean = round(sum(det_values) / len(det_values), 4)
    price_s2 = determinism_price(det_mean, None, "S2 (тот же конфиг, 50 шагов CPT)",
                                 STEP_SECONDS_S2)
    price_c = determinism_price(det_mean, None, "прогон C того же дня (без режима)",
                                step_seconds("c"))

    ckpt_path = run_dir / "checkpoint_compare.json"
    ckpt: dict = {}
    if ckpt_path.is_file():
        try:
            ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            ckpt = {"error": "checkpoint_compare.json не разобран"}

    incidents = {
        "by_run": {k: (v or {}).get("incidents_in_log", []) for k, v in per_run.items()},
        "oom_or_nvrm": sorted({i for v in per_run.values()
                               for i in (v or {}).get("incidents_in_log", [])}),
    }

    return {"runs": per_run, "losses": losses, "divergence": divergence,
            "determinism_price": {"vs_s2": price_s2, "vs_run_c": price_c,
                                  "deterministic_step_seconds_mean": det_mean,
                                  "definition": ("время шага — интервал между началами "
                                                 "обучающих forward'ов (тот же замер, что в S2: "
                                                 "eval-интервалы исключены, энтропия внутри)")},
            "price_calendar": price_calendar(price_c.get("percent")),
            "onset_cross_check": divergence_onset_cross_check(divergence, losses),
            "checkpoint_compare": ckpt, "incidents": incidents}


# ─── evidence ─────────────────────────────────────────────────────────────────

def build_evidence(run_dir: Path, args, pre: dict, stage_results: dict,
                   summary: dict, manifest: Path | None, post: dict) -> dict:
    """``evidence/s3-determinism.json`` — замеры пробы и обязательный вывод."""
    price = summary["determinism_price"]
    verdict = verdict_of(summary["runs"], summary["divergence"],
                         summary["checkpoint_compare"], price.get("vs_s2", {}),
                         args.steps)
    return {
        "stage": "S3a",
        "date": datetime.now().isoformat(timespec="seconds"),
        "purpose": ("детерминизм-проба ADR-009: измерить, воспроизводится ли прогон по "
                    "манифесту (тот же сид) и какова цена детерминированного режима"),
        "basis": [
            "ADR-009 п.3 — проба вводится в S3: два прогона CPT по 20 шагов, один сид, "
            "детерминированный гейтовый профиль (use_deterministic_algorithms + "
            "CUBLAS_WORKSPACE_CONFIG)",
            "ADR-009 п.4 — исход пробы меняет план (A5-drift), а не откладывается",
            "ADR-001 — ось успеха и k-из-k; ADR-002 — семейство Qwen2.5-0.5B; "
            "ADR-007 п.5 — недоступность стенда это красный вердикт",
        ],
        "config": {
            "model": args.model, "seed": args.seed, "steps": args.steps,
            "max_len": args.max_len, "batch_size": args.batch_size,
            "max_samples": args.max_samples, "peak_lr_scale": args.peak_lr_scale,
            "image": args.image, "attn": args.attn,
            "cpt_data": CPT_DATA, "tok_cache": CPT_TOK_CACHE,
            "gen_eval_every": args.gen_eval_every,
            "entropy": {"mode": args.entropy, "every": args.entropy_every,
                        "pos_stride": args.entropy_pos_stride},
            "deterministic_runs": [l for l, d in RUNS if d],
            "nondeterministic_runs": [l for l, d in RUNS if not d],
            "launch": f"{SAFE_START} -d 60 -i 5 -- docker run (absorbер включён)",
            "same_as_s2": ("конфиг совпадает со смоуком S2, кроме числа шагов (20 против 50) "
                           "и каталогов чекпойнтов: иначе цена мерила бы другую стадию"),
        },
        "stand": {"host": args.host, "run_dir": f"{STAND_EXPERIMENTS}/{run_id_for(args.ts)}",
                  "case_run_dir": str(run_dir)},
        "preconditions": {"checks": pre.get("checks", []), "ok": pre.get("ok"),
                          "dmesg_before": pre.get("dmesg_before"),
                          "resource_owner": next((c for c in pre.get("checks", [])
                                                  if c.get("name") == "owner"), {})},
        "runs": {letter: {"exp_name": exp_name_for(letter, args.ts),
                          "deterministic": det,
                          "stage_result": stage_results.get(letter, {}),
                          "summary": summary["runs"].get(letter, {})}
                 for letter, det in RUNS},
        "measurements": {
            "loss_by_step": summary["losses"],
            "loss_divergence": summary["divergence"],
            "step_seconds": {k: (v or {}).get("step_seconds_mean")
                             for k, v in summary["runs"].items()},
            "tok_per_s": {k: (v or {}).get("tok_per_s")
                          for k, v in summary["runs"].items()},
            "determinism_price": summary["determinism_price"],
            "price_calendar": summary.get("price_calendar"),
            "divergence_onset_cross_check": summary.get("onset_cross_check"),
            "checkpoint_sha256": {k: (stage_results.get(k) or {}).get("checkpoint_sha256")
                                  for k, _ in RUNS},
            "checkpoint_bytes": {k: (stage_results.get(k) or {}).get("checkpoint_bytes")
                                 for k, _ in RUNS},
            "checkpoint_compare": summary["checkpoint_compare"],
            "tensor_identity_note": (
                "вердикт «совпало бит-в-бит» вынесен по значениям тензоров "
                "(compare_checkpoints.py), а не по sha256 файла: torch.save упаковывает "
                "тензоры в zip-контейнер, и разные байты файла не означают разные числа "
                "(и наоборот) — измерено на синтетической паре в тестах инструмента"),
            "incidents": summary["incidents"],
        },
        "determinism_verdict": verdict,
        "instrument": {
            "case_runner_sha256": S.sha256_path(CASE_ROOT / "tools" / "run_det_probe.py"),
            "case_probe_sha256": S.sha256_path(CASE_ROOT / "tools" / "smoke_probe.py"),
            "case_comparator_sha256": S.sha256_path(CASE_ROOT / "tools" / "compare_checkpoints.py"),
            "case_sampler_sha256": S.sha256_path(CASE_ROOT / "tools" / "smoke_mem_sampler.sh"),
            "case_owner_guard_sha256": S.sha256_path(CASE_ROOT / "tools" / "check_resource_owner.sh"),
            "stand_sha256": (stage_results.get("instruments") or {}).get("sha256"),
            "pins_recheck": stage_results.get("instrument_pins"),
            "note": ("проба измерена обвязкой кейса; пайплайн и данные не менялись "
                     "(read-only, AD-4/AD-7)"),
        },
        "manifest": (S.rel_or_abs(manifest) if manifest else None),
        "postcheck": post,
        "measurements_limits": [
            "loss сравнивается с точностью записи обвязки (1e-6); полная точность — "
            "только в побитовом сравнении чекпойнтов",
            "цена детерминизма измерена на 20 шагах CPT: это замер короткой стадии, "
            "не всего календаря (ADR-008); перенос на полный прогон — арифметика, "
            "а не замер",
            "проба не проверяет детерминизм SFT/RL-стадий и eval-пути — только CPT "
            "(ADR-009 п.3 называет именно его)",
            "наблюдение S2 о расхождении траекторий с удалёнными артефактами остаётся "
            "наблюдением: эта проба его не «подтверждает», а заменяет собственным замером",
        ],
    }


def postcheck(host: str, ts: str, pre: dict) -> dict:
    """Состояние стенда после пробы: контейнеры пробы закрыты, платформа жива."""
    rc, out, _ = S.ssh(host, "docker ps --format '{{.Names}}'")
    running = out.split() if rc == 0 else []
    probe_running = [n for n in running if n.startswith(CONTAINER_PREFIX)]
    platform = [n for n in running if n.startswith(PLATFORM_PREFIX)]
    rc2, mem_raw, _ = S.ssh(host, "cat /proc/meminfo")
    return {
        "containers": running,
        "probe_containers_running": probe_running,
        "container_closed": not probe_running,
        "platform_alive": platform,
        "platform_lost": sorted(set((pre.get("platform") or {}).keys()) - set(platform)),
        "mem_available_gb": S.free_gb(S.parse_meminfo(mem_raw)) if rc2 == 0 else None,
        "dmesg_after": S.dmesg_counts(host),
    }


def write_manifest(run_dir: Path, args, stage_results: dict, status: str) -> Path | None:
    """Манифест AD-2 (C-012) — генератором кейса, с фактическими гиперпараметрами."""
    cmd = [sys.executable, str(CASE_ROOT / "tools" / "write_run_manifest.py"),
           "--run-dir", str(run_dir),
           "--dataset", str(CASE_ROOT / CPT_TOK_CACHE),
           "--base-model", args.model,
           "--pipeline", str(CASE_ROOT / "laguna_pipeline_v8.py"),
           "--seed", str(args.seed), "--image", args.image,
           "--stages", f"cpt={status}",
           "--run-version",
           "tools/run_det_probe.py@" + (S.sha256_path(CASE_ROOT / "tools" / "run_det_probe.py") or "?")[:12],
           "--relative-to", str(CASE_ROOT), "--force"]
    extras = {
        #: Исходный текст корпуса лежит в корне кейса симлинком на gb10-shared, а не
        #: в `datasets/`: в `datasets/` его нет ни на стенде, ни в кейсе (читается
        #: преток-кэш, а путь нужен для lineage-хеша — см. run_smoke.check_data).
        "cpt_text": str(CASE_ROOT / "cpt_corpus_v12r.txt"),
        "sft_data": str(CASE_ROOT / "datasets" / "sft_train_v12.jsonl"),
        "eval_data": str(CASE_ROOT / "datasets" / "eval_ood_clean.jsonl"),
    }
    for name, path in extras.items():
        cmd += ["--dataset-extra", f"{name}={path}"]
    hyper = {"steps": args.steps, "max_len": args.max_len, "batch_size": args.batch_size,
             "max_samples": args.max_samples, "peak_lr_scale": args.peak_lr_scale,
             "gen_eval_every": args.gen_eval_every, "attn": args.attn,
             "runs": ",".join(l for l, _ in RUNS),
             "deterministic_runs": ",".join(l for l, d in RUNS if d),
             "nondeterministic_runs": ",".join(l for l, d in RUNS if not d),
             "measurement": "детерминизм-проба ADR-009 (loss по шагам + побитовое сравнение чекпойнтов)",
             "run_manifest_note": ("три прогона CPT по 20 шагов с одним сидом; чекпойнты "
                                   "остаются на стенде (AD-4), в кейс не копируются")}
    for letter, _ in RUNS:
        sha = (stage_results.get(letter) or {}).get("checkpoint_sha256")
        if sha:
            hyper[f"checkpoint_sha256_{letter}"] = sha
    for k, v in hyper.items():
        cmd += ["--hyperparams", f"{k}={json.dumps(str(v))}"]
    cmd += ["--hyperparams-source",
            "фактические значения прогонов пробы (аргументы docker run в runs/det-probe-*/[abc]/run_cpt.sh)"]
    if status == "done":
        cmd.append("--complete")
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"ПРЕДУПРЕЖДЕНИЕ: манифест не записан (rc={p.returncode}): "
              f"{p.stderr.strip() or p.stdout.strip()}", file=sys.stderr)
        return None
    return run_dir / "run_manifest.json"


def stop_containers(host: str, ts: str) -> dict:
    """План отката: закрыть контейнеры пробы по именам (llm-platform-* не трогаем)."""
    stopped, missing = [], []
    for letter, _ in RUNS:
        name = container_name(ts, letter)
        rc, out, err = S.ssh(host, "docker ps -a --format '{{.Names}}'")
        if rc != 0:
            return {"ok": False, "stopped": stopped,
                    "detail": f"docker ps недоступен: {err.strip()}"}
        if name not in out.split():
            missing.append(name)
            continue
        rc2, out2, err2 = S.ssh(host, f"docker stop {name}", timeout=180)
        if rc2 == 0:
            stopped.append(name)
        else:
            return {"ok": False, "stopped": stopped,
                    "detail": f"docker stop {name} rc={rc2}: {err2.strip()}"}
    return {"ok": True, "stopped": stopped,
            "detail": (f"остановлены: {', '.join(stopped)}" if stopped else "контейнеров пробы нет")
                      + (f"; не найдены (уже закрыты): {', '.join(missing)}" if missing else "")}


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="S3a: детерминизм-проба (ADR-009) — три прогона CPT по 20 шагов, "
                    "один сид; два в детерминированном режиме, один без.")
    ap.add_argument("--host", default=os.environ.get("GB10_HOST", DEFAULT_HOST),
                    help=f"стенд по ssh (по умолчанию {DEFAULT_HOST} / $GB10_HOST)")
    ap.add_argument("--ts", default=datetime.now().strftime("%Y%m%d-%H%M"),
                    help="метка прогона (по умолчанию — текущее время)")
    ap.add_argument("--image", default=os.environ.get("LAGUNA_IMAGE", DEFAULT_IMAGE),
                    help="образ окружения")
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B", help="базовая модель (ADR-002)")
    ap.add_argument("--seed", type=int, default=42, help="сид прогона (один для всех трёх)")
    ap.add_argument("--steps", type=int, default=DEFAULT_STEPS,
                    help=f"шагов CPT в каждом прогоне (ADR-009: {DEFAULT_STEPS})")
    ap.add_argument("--max-len", type=int, default=8192, help="длина чанка (как в S2)")
    ap.add_argument("--batch-size", type=int, default=1, help="батч CPT (как в S2)")
    ap.add_argument("--max-samples", type=int, default=9776,
                    help="чанков CPT из кэша (весь микс v12r, как в S2)")
    ap.add_argument("--peak-lr-scale", type=float, default=0.7,
                    help="множитель пикового LR WSD (как в run_v12_ladder.sh)")
    ap.add_argument("--attn", default="flex", choices=["flex", "none"],
                    help="LAGUNA_ATTN: flex — путь рабочего раннера (как в S2)")
    ap.add_argument("--mem-fraction", default="0.6", help="LAGUNA_MEM_FRACTION")
    ap.add_argument("--gen-eval-every", type=int, default=10,
                    help="CPT_GEN_EVAL_EVERY (как в S2)")
    ap.add_argument("--entropy", default="on", choices=["on", "off"],
                    help="замер энтропии обвязкой (как в S2 — иначе цена мерилась бы "
                         "на другой стадии)")
    ap.add_argument("--entropy-every", type=int, default=1, help="энтропия каждый N-й шаг")
    ap.add_argument("--entropy-pos-stride", type=int, default=16,
                    help="шаг подвыборки позиций (как в S2)")
    ap.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB_CPT,
                    help=f"порог свободной unified-памяти (ADR-008: CPT {MIN_FREE_GB_CPT} ГБ)")
    ap.add_argument("--sampler-seconds", type=int, default=None,
                    help="окно сэмплера памяти (по умолчанию — из шагов)")
    ap.add_argument("--runs-dir", default="runs", help="каталог прогонов кейса")
    ap.add_argument("--evidence", default="evidence/s3-determinism.json",
                    help="evidence-файл пробы")
    ap.add_argument("--plan", action="store_true", help="напечатать план и выйти")
    ap.add_argument("--preflight-only", action="store_true", help="только предусловия")
    ap.add_argument("--analyze-only", action="store_true",
                    help="пересобрать сводку и evidence из готовых артефактов")
    ap.add_argument("--write-manifest", action="store_true",
                    help="при --analyze-only: перезаписать run_manifest.json (AD-2) "
                         "текущей ревизией раннера — для прогона, чей манифест не "
                         "записался при запуске (например, из-за отсутствующего "
                         "необязательного датасета-extra)")
    ap.add_argument("--stop-only", action="store_true", help="закрыть контейнеры пробы")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)
    if args.plan and args.analyze_only:
        ap.error("--plan и --analyze-only несовместимы")
    if args.write_manifest and not args.analyze_only:
        ap.error("--write-manifest имеет смысл только с --analyze-only")
    if not MIN_STEPS <= args.steps <= MAX_STEPS:
        ap.error(f"--steps {args.steps} вне коридора пробы ({MIN_STEPS}–{MAX_STEPS}): "
                 f"ADR-009 называет {DEFAULT_STEPS} шагов, проба не полный протокол")
    if args.sampler_seconds is None:
        args.sampler_seconds = int(max(1800, args.steps * 300))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = CASE_ROOT / args.runs_dir / f"det-probe-{args.ts}"

    if args.plan:
        print(render_plan(args))
        return EXIT_OK

    if args.stop_only:
        res = stop_containers(args.host, args.ts)
        print(f"[run_det_probe] закрытие контейнеров пробы: {res['detail']}")
        return EXIT_OK if res["ok"] else EXIT_FAIL

    if args.analyze_only:
        if not run_dir.is_dir():
            print(f"NOT-VERIFIED: каталога прогона нет: {run_dir}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        existing: dict = {}
        ev_path = CASE_ROOT / args.evidence
        if ev_path.is_file():
            try:
                existing = json.loads(ev_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        summary = analyze(run_dir, args)
        stage_results = {l: (existing.get("runs", {}).get(l, {}).get("stage_result") or {})
                         for l, _ in RUNS}
        #: Хеши инструментов переснимаются со стенда, если он доступен (файлы прогона
        #: там сохраняются), иначе переносятся из прежнего evidence. Подставить сюда
        #: хеши локальных файлов значило бы выдать «чем измеряли сейчас» за «чем
        #: измеряли прогон».
        pins = stand_instrument_pins(args.host, args.ts)
        stage_results["instruments"] = {
            "sha256": (pins.get("stand") if pins.get("available")
                       else (existing.get("instrument") or {}).get("stand_sha256"))}
        stage_results["instrument_pins"] = pins
        pre = {"host": args.host, "checks": existing.get("preconditions", {}).get("checks", []),
               "ok": existing.get("preconditions", {}).get("ok"),
               "dmesg_before": existing.get("preconditions", {}).get("dmesg_before")}
        post = existing.get("postcheck") or postcheck(args.host, args.ts, pre)
        manifest = run_dir / "run_manifest.json"
        regenerated = False
        if args.write_manifest:
            status_for_manifest = "done" if existing.get("status") == "done" else "partial"
            written = write_manifest(run_dir, args, stage_results, status_for_manifest)
            if written is not None:
                manifest, regenerated = written, True
                print(f"[run_det_probe] манифест AD-2 перезаписан текущей ревизией раннера: "
                      f"{S.rel_or_abs(written)}")
        ev = build_evidence(run_dir, args, pre, stage_results, summary,
                            manifest if manifest.is_file() else None, post)
        ev["status"] = existing.get("status", "partial")
        if existing:
            ev["rebuilt"] = {"at": datetime.now().isoformat(timespec="seconds"),
                             "note": ("сводка пересобрана из артефактов прогона; предусловия, "
                                      "результаты стадий и состояние стенда взяты из прежнего "
                                      "evidence и повторным замером не подменялись")}
        if regenerated:
            ev["rebuilt"]["manifest_regenerated"] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "why": ("манифест AD-2 записан после прогона: при запуске он не записался "
                        "из-за необязательного датасета-extra, которого нет ни на стенде, "
                        "ни в кейсе (читается преток-кэш)"),
                "runner_sha256": S.sha256_path(CASE_ROOT / "tools" / "run_det_probe.py"),
            }
        S.write_evidence(ev_path, ev)
        print(f"[run_det_probe] evidence пересобран из артефактов: {args.evidence}")
        return EXIT_OK

    pre = preflight(args.host, args)
    print(f"== предусловия ({args.host}) ==")
    for c in pre["checks"]:
        print(f"  [{'ok ' if c['ok'] else 'FAIL'}] {c['name']:<14} {c['detail']}")
    if not pre["ok"]:
        failed = [c["name"] for c in pre["checks"] if not c["ok"]]
        print(f"\nСТОП: предусловия не выполнены ({', '.join(failed)}) — проба не запускается")
        if args.json:
            print(json.dumps(pre, ensure_ascii=False, indent=2))
        return EXIT_FAIL
    if args.preflight_only:
        if args.json:
            print(json.dumps(pre, ensure_ascii=False, indent=2))
        return EXIT_OK

    ship = ship_instruments(args.host, f"{STAND_EXPERIMENTS}/{run_id_for(args.ts)}")
    if not ship["ok"]:
        print(f"СТОП: инструменты не доставлены: {ship['error']}", file=sys.stderr)
        return EXIT_FAIL

    print(f"\n== три прогона CPT по {args.steps} шагов, сид {args.seed} ==")
    stage_results: dict[str, dict] = {"instruments": {"sha256": ship["sha256"]}}
    for letter, det in RUNS:
        res = run_one(args.host, args, letter, run_dir / letter)
        stage_results[letter] = res
        print(f"[run_det_probe] прогон {letter}: exit={res.get('exit')} "
              f"wall={res.get('wall_seconds')} с, чекпойнт {str(res.get('checkpoint_sha256'))[:12]}…")
        if not res.get("ok"):
            print(f"СТОП: прогон {letter} не завершился успешно "
                  f"({res.get('error') or 'см. лог'}) — проба неполна, evidence собран как partial")
            break

    print("\n== побитовое сравнение чекпойнтов ==")
    ckpt = compare_checkpoints_on_stand(args.host, args)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "checkpoint_compare.json").write_text(
        json.dumps(ckpt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for key, val in ckpt.get("pairs", {}).items():
        rep = val.get("report") or {}
        print(f"  {key}: {rep.get('verdict', 'NOT-COMPARABLE')} "
              f"(тензоров {rep.get('tensors_compared')}, идентичных {rep.get('tensors_identical')})")

    summary = analyze(run_dir, args)
    post = postcheck(args.host, args.ts, pre)
    all_ok = all((stage_results.get(l) or {}).get("ok") for l, _ in RUNS)
    status = "done" if all_ok else "partial"
    manifest = write_manifest(run_dir, args, stage_results, status)
    ev = build_evidence(run_dir, args, pre, stage_results, summary, manifest, post)
    ev["status"] = status
    S.write_evidence(CASE_ROOT / args.evidence, ev)

    v = ev["determinism_verdict"]
    print(f"\n== вывод о воспроизводимости ==")
    print(f"  воспроизводится ли прогон по манифесту после включения детерминизма: "
          f"{v['answer'].upper()}")
    for b in v["basis"]:
        print(f"  · {b}")
    print(f"  следствие для A5-drift: {v['consequence_for_a5_drift']}")
    print(f"\n[run_det_probe] evidence: {args.evidence} (status={status})")
    print(f"[run_det_probe] чекпойнты остаются на стенде: "
          f"{STAND_EXPERIMENTS}/{run_id_for(args.ts)}/{{a,b,c}}/checkpoints/ (в кейс не копируются, AD-4)")

    if args.json:
        print(json.dumps(ev, ensure_ascii=False, indent=2))
    if not all_ok:
        return EXIT_FAIL
    if not post.get("container_closed") or post.get("platform_lost"):
        print("ВНИМАНИЕ: состояние стенда после пробы требует внимания (контейнеры/платформа)")
        return EXIT_FAIL
    if os.environ.get("DET_PROBE_STRICT_VERDICT", "0") == "1" and v["answer"] != "да":
        #: Флаг для гейта: проба, которая НЕ подтвердила воспроизводимость, — это не
        #: провал измерения, а изменение плана (ADR-009 п.4). По умолчанию не гейтится.
        print("СТОП(--strict-verdict): воспроизводимость не подтверждена")
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
