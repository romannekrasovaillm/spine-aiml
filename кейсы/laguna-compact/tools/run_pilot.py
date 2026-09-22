#!/usr/bin/env python3
"""S3b — пилот ревизии: цепочка CPT → SFT → RL → eval на стенде GB10 (ADR-013, фаза 1).

Пилот — **один сид (42), полный цикл**: CPT 9776 шагов (≈7 ч) → SFT 3 эпохи (≈61 ч)
→ RL 500 шагов (12.3 … 17.3 ч) → три eval-стадии (192 задачи, `eval_ood_clean`).
Цикл идёт ≈3.5 суток **на стенде**, поэтому раннер устроен как «подготовить,
проверить право стартовать, запустить в tmux и вернуть управление»: ждать
завершения он не умеет и не должен.

Что делает раннер по шагам:

1. **предусловия** (до любого запуска): стенд отвечает; страж хозяина ресурса
   AD-9 (`tools/check_resource_owner.sh --stage cpt`, без профиля
   `--unreachable-not-verified` — стадия, стартующей на невидимом стенде,
   зелёного вердикта не бывает); свободная unified-память; `llm-platform-*` живы;
   страж AD-5 в `--strict`; образ; входы (преток-кэши, пайплайн, `safe_start.sh`,
   стабы ray, PPL-наборы, GLM-ключ для eval, пул ревизии — сверяется по sha256 с
   кейсом); диск; число примеров SFT (от него считаются шаги эпох — расхождение
   с зафиксированным ADR-008/ADR-011 — сигнал, а не повод пересчитать молча);
2. **доставка** цепочки и инструментов в каталог прогона на стенде со сверкой
   sha256 (что исполнялось — то и лежит в кейсе);
3. **проверка права стартовать цепочкой**: `pilot_chain.sh --check-only` —
   прогон предусловия AD-9 по каждой стадии, без обучения. Вердикт раннера и
   вердикт цепочки обязаны совпасть: расхождение — инцидент, а не «повезло»;
4. **запуск** в tmux (`--tmux-session pilot-compact-s42`) или, если цепочка
   отказала, — фиксация отказа без запуска: предусловие не выполнено, стадия не
   стартует, причина в лог;
5. **манифест AD-2** прогона (через `tools/write_run_manifest.py`) и evidence
   `evidence/s3-pilot.json`; подтверждение запуска — tmux-сессия, контейнер
   стадии и первые строки лога.

Остановка и пауза. Стоп-условия живут в цепочке (`tools/pilot_chain.sh`:
энтропия < 0.5 два наблюдения подряд, два NVRM/Xid-инцидента → стоп и **пауза**,
0x51 → `storm_gap.sh` и повтор, предохранитель памяти, детектор молчания).
Паузу снимает владелец: `run_pilot.py --clear-stop "<причина>"` (цепочка сама
маркер не снимает). `--stop-only` закрывает контейнеры пилота — план отката;
чекпойнты при этом **не удаляются**.

Режимы без запуска: `--plan` (что и как будет запущено), `--preflight-only`
(только предусловия), `--ship-only` (доставка + вердикт цепочки, без tmux),
`--analyze-only` (пересобрать evidence и манифест по артефактам прогона),
`--stop-only`, `--clear-stop`.

Коды возврата::

    0 — пилот запущен (или запрошенный режим без запуска отработал)
    1 — предусловие не выполнено / стадия упала / цепочка отказала: не запускается
    2 — NOT-VERIFIED: стенд недоступен или артефакт отсутствует

Запуск::

    python3 tools/run_pilot.py --plan
    python3 tools/run_pilot.py --preflight-only
    python3 tools/run_pilot.py                      # цепочка в tmux на стенде, ≈3.5 сут
    python3 tools/run_pilot.py --ship-only          # доставка + вердикт цепочки, без старта
    python3 tools/run_pilot.py --analyze-only --ts 20260914-1540
    python3 tools/run_pilot.py --stop-only --ts 20260914-1540
    python3 tools/run_pilot.py --clear-stop "бурю разобрали" --ts 20260914-1540
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Транспорт, сэмплер памяти и разбор манифестов — общие с S2/S3-pre/S3a: своя
#: вторая реализация ssh-доставки разошлась бы с первой на первой же правке.
import run_smoke as S  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "gb10-fast"
DEFAULT_IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"

STAND_SHARED = "/home/user/gb10-shared"
STAND_EXPERIMENTS = "/home/user/experiments"
SAFE_START = f"{STAND_SHARED}/nvrm-storm/safe_start.sh"
STORM_GAP = f"{STAND_SHARED}/nvrm-storm/storm_gap.sh"
NVRN_LOG = f"{STAND_EXPERIMENTS}/nvrm_watch.log"
GLM_ENV = f"{STAND_SHARED}/.glm_env"
CTR_SHARED = "/workspace/shared"
CTR_EXPERIMENTS = "/workspace/experiments"
PIPELINE_CTR = f"{CTR_SHARED}/laguna_pipeline_v8.py"

#: Набор курикулума RL-стадии — **v2** (ADR-054 п.1: пересечение с обучающим набором
#: 0.00 %, карточка AD-2 есть, лейк-фильтр пройден). Наборы `ox` в курикулум не
#: берутся (утечка 59.84 %), `v1` остаётся историческим носителем и стадией не
#: выбирается — он закреплён в раннерах до этого решения и карточки AD-2 не имеет.
#: Версионное имя на сетевом диске; в кейсе — симлинк
#: `runs/rev-pool-v2/rl_pool_filtered.jsonl` на тот же файл (AD-4/C-011).
RL_POOL_NAME = "rl_tasks_revpool_v2.jsonl"
RL_POOL_STAND = f"{STAND_SHARED}/datasets/{RL_POOL_NAME}"
RL_POOL_CASE = CASE_ROOT / "datasets" / RL_POOL_NAME

CPT_TOK_CACHE = "datasets/tok/cpt_corpus_v12r_8192_qwen25.npy"
SFT_TOK_CACHE = "datasets/tok/sft_train_v12_8192_qwen25.npz"
#: Обучающий набор SFT пилота (ADR-013 п.2 — заморожен). Один источник на три
#: места: вход стадии (`--sft-data` цепочки), объявление в манифесте AD-2
#: (`--dataset-extra sft=`) и проверку числа примеров. Пока вход брался литералом
#: внутри цепочки, объявление и факт расходились молча (S3av).
SFT_DATA_JSONL = "datasets/sft_train_v12.jsonl"
SFT_DATA_SHA256 = "39f616f1e47b1c50490bb9e01167271bac5191c71e4bff727e4940094d6d49a6"
CPT_TOK_POS = "datasets/tok/cpt_corpus_v12r_8192_qwen25_pos.npy"
PRETOKENIZER = f"{CTR_SHARED}/pretokenize_v9.py"

#: Платформенный инференс: не трогается (ADR-007 п.4), но не должен исчезнуть.
PLATFORM_PREFIX = "llm-platform-"

RUN_PREFIX = "pilot-compact-s42"
CONTAINER_PREFIX = "laguna-pilot-"

#: Число примеров SFT v12, от которого ADR-008/ADR-011 считают эпохи
#: (3 × 44 949 / 2 = 67 423 шага ≈ 61 ч). Расхождение — сигнал: шаги пересчитаны
#: не «как раньше», а от фактического файла, и это видно в отчёте.
EXPECT_SFT_SAMPLES = 44949

#: Файлы, доставляемые в каталог прогона: цепочка и те инструменты, которыми она
#: пользуется на стенде (страж AD-9, генератор манифеста, сэмплер памяти).
SHIPPED = (
    ("pilot_chain.sh", "tools/pilot_chain.sh"),
    ("check_resource_owner.sh", "tools/check_resource_owner.sh"),
    ("write_run_manifest.py", "tools/write_run_manifest.py"),
    ("smoke_mem_sampler.sh", "tools/smoke_mem_sampler.sh"),
    #: Критерий вырожденной награды (ADR-017). Едет на стенд тем же путём, что и
    #: остальные инструменты цепочки: критерий, считаемый только на хосте
    #: архитектора, не остановит стадию — а ADR-017 п.2 требует именно остановки.
    ("rl_degeneracy.py", "tools/rl_degeneracy.py"),
)


@dataclass(frozen=True)
class Stage:
    """Стадия цепочки. Порогов памяти здесь нет намеренно: их единственный
    источник — страж AD-9 (`--stage {cpt|sft|rl}` → 35/50/49 ГБ), и он же печатает
    порог в вердикте. Дублировать число в раннере значило бы завести вторую
    правду, которая разойдётся с первой при первом же ADR.

    `guard_stage` для eval-стадий — `sft`: eval не тренирует, но грузит модель и
    генерирует; собственного порога в ADR-008/ADR-011 нет, и брать меньший было бы
    выдумкой, а не калибровкой.
    """

    name: str
    pipeline_stage: str
    guard_stage: str
    artifact: str
    steps: int
    batch: int
    eval_ckpt: str = "-"


def build_stages(args, sft_samples: int) -> list[Stage]:
    """Стадии пилота — ADR-013 п.1 (порядок: CPT → SFT → RL → eval).

    `eval_base` добавлен как нулевая точка оси (в историческом контуре он есть:
    `eval_results_base.json`): без него «RL > SFT» не с чем сравнить по уровню,
    только между собой. Остальные три чекпойнта — из ADR-013 п.3(а) и списка
    задачи: `eval_sft` (базис оси), `eval_rl` (результат RL).
    """
    sft_steps = args.sft_epochs * sft_samples // args.sft_batch
    return [
        Stage("cpt", "cpt", "cpt", "checkpoints/checkpoint_final.pt",
              args.cpt_steps, args.cpt_batch),
        Stage("sft", "sft", "sft", "checkpoints/sft_checkpoint_final.pt",
              sft_steps, args.sft_batch),
        Stage("rl", "rl", "rl", "checkpoints/rl_checkpoint_final.pt",
              args.rl_steps, args.sft_batch),
        Stage("eval_base", "eval", "sft", "logs/eval_results_base.json", 1, 1, "base"),
        Stage("eval_sft", "eval", "sft", "logs/eval_results_sft.json", 1, 1, "sft"),
        Stage("eval_rl", "eval", "sft", "logs/eval_results.json", 1, 1, "rl"),
    ]


# ─── имена и пути ─────────────────────────────────────────────────────────────

def run_id_for(ts: str) -> str:
    return f"{RUN_PREFIX}-{ts}"


def exp_name_for(stage: Stage, ts: str) -> str:
    """`--exp_name` стадии: по нему страж AD-9 отличает свою нагрузку от чужой."""
    return f"{RUN_PREFIX}-{stage.name}-{ts}"


def container_for(stage: Stage, ts: str) -> str:
    return f"{CONTAINER_PREFIX}{ts}-{stage.name}"


def stages_tsv(stages: list[Stage], ts: str) -> str:
    """Таблица стадий (TSV) — единственный источник состава цепочки.

    Её читает `pilot_chain.sh`; порог памяти в таблицу не входит (см. Stage).
    """
    lines = []
    for st in stages:
        lines.append("\t".join([st.name, st.pipeline_stage, st.guard_stage, st.artifact,
                                str(st.steps), str(st.batch), st.eval_ckpt,
                                exp_name_for(st, ts)]))
    return "\n".join(lines) + "\n"


def eta_hours(stages: list[Stage], cpt_step_seconds: float = 2.569,
              sft_step_seconds: float = 3.251) -> dict:
    """Календарь пилота из **измеренных** времён шага (S2: CPT 2.569 с, SFT 3.251 с;
    ADR-011: RL 12.3–17.3 ч на 500 шагов) — арифметика, а не замер этого прогона."""
    cpt = next(s for s in stages if s.name == "cpt")
    sft = next(s for s in stages if s.name == "sft")
    cpt_h = cpt.steps * cpt_step_seconds / 3600
    sft_h = sft.steps * sft_step_seconds / 3600
    rl_lo, rl_hi = 12.27, 17.26
    eval_h = 2.0
    lo = cpt_h + sft_h + rl_lo + eval_h
    hi = cpt_h + sft_h + rl_hi + eval_h
    return {
        "basis": ("CPT 2.569 с/шаг и SFT 3.251 с/шаг — замер S2 (evidence/s2-smoke.json); "
                  "RL 12.27–17.26 ч на 500 шагов — две границы ADR-011 "
                  "(прямой замер и экстраполяция S3-pre); eval ≈2 ч — ADR-008"),
        "cpt_hours": round(cpt_h, 1), "sft_hours": round(sft_h, 1),
        "rl_hours_range": [rl_lo, rl_hi], "eval_hours": eval_h,
        "total_hours_range": [round(lo, 1), round(hi, 1)],
        "total_days_range": [round(lo / 24, 2), round(hi / 24, 2)],
        "note": ("оценка по измеренным временам шага, а не замер этого пилота; "
                 "верхняя граница RL получена на политике, договаривающей ход до потолка"),
    }


def chain_command(args, run_dir_stand: str, stages: list[Stage]) -> str:
    """Строка запуска цепочки на стенде — она же идёт в tmux и в лог.

    Собирается в одном месте: ручное повторение стадии (строка в `logs/chain.log`)
    и автоматический запуск обязаны быть одним и тем же вызовом.
    """
    return " ".join([
        f"bash {run_dir_stand}/pilot_chain.sh",
        f"--run-dir {run_dir_stand}",
        f"--ctr-run-dir {CTR_EXPERIMENTS}/{Path(run_dir_stand).name}",
        f"--stages-file {run_dir_stand}/stages.tsv",
        f"--exp-base {RUN_PREFIX}-{args.ts}",
        f"--ctr-prefix {CONTAINER_PREFIX}{args.ts}",
        f"--shared {STAND_SHARED}",
        f"--experiments {STAND_EXPERIMENTS}",
        f"--pipeline {STAND_SHARED}/laguna_pipeline_v8.py",
        f"--pipeline-ctr {PIPELINE_CTR}",
        f"--guard {run_dir_stand}/check_resource_owner.sh",
        f"--safe-start {SAFE_START}",
        f"--sampler {run_dir_stand}/smoke_mem_sampler.sh",
        f"--storm-gap {STORM_GAP}",
        f"--manifest-tool {run_dir_stand}/write_run_manifest.py",
        f"--degeneracy-tool {run_dir_stand}/rl_degeneracy.py",
        f"--degeneracy-window {args.degeneracy_window}",
        #: Набор SFT — тем же значением, что уходит в объявление манифеста ниже.
        #: Без этой строки цепочка брала бы умолчание (v12) — сегодня совпадающее,
        #: завтра могущее разойтись с объявлением молча (S3av).
        f"--sft-data {CTR_SHARED}/{SFT_DATA_JSONL}",
        f"--sft-data-sha256 {SFT_DATA_SHA256}",
        #: Набор курикулума RL — тем же значением, что уходит в объявление манифеста
        #: (`--dataset-extra rl_pool=`). Без срока давности: пока вход стадии брался
        #: литералом, объявление и факт расходились молча — дефект S3av, а ADR-054
        #: п.1–2 распространяет тот же принцип на RL-набор. Хеш не передаётся: его
        #: цепочка измеряет на стенде по файлу (ADR-028 п.1: объявление измеряется,
        #: а не берётся константой).
        f"--rl-data {CTR_SHARED}/datasets/{RL_POOL_NAME}",
        f"--image {args.image}",
        f"--glm-env {GLM_ENV}",
        f"--nvrm-log {NVRN_LOG}",
        f"--runner-sha12 {runner_sha12()}",
        f"--arch-base {arch_base()}",
        f"--model {args.model}",
        f"--seed {args.seed}",
        f"--max-len {args.max_len}",
        f"--max-samples {args.max_samples}",
        f"--peak-lr-scale {args.peak_lr_scale}",
        f"--rl-steps {args.rl_steps}",
        f"--eval-items {args.eval_items}",
        f"--mem-fraction {args.mem_fraction}",
        f"--mem-cap {args.mem_cap}",
        f"--attn {args.attn}",
        f"--cpt-gen-eval-every {args.cpt_gen_eval_every}",
        f"--rl-gen-eval-every {args.rl_gen_eval_every}",
        f"--resync-every {args.resync_every}",
        f"--vllm-gpu-util {args.vllm_gpu_util}",
        f"--kv-cache-bytes {args.kv_cache_bytes}",
        f"--vllm-eager {args.vllm_eager}",
        f"--entropy-floor {args.entropy_floor}",
        f"--entropy-low-streak {args.entropy_low_streak}",
        f"--nvrm-limit {args.nvrm_limit}",
        f"--mem-floor-gb {args.mem_floor_gb}",
        f"--poll-seconds {args.poll_seconds}",
        f"--stall-minutes {args.stall_minutes}",
        f"--stage-retries {args.stage_retries}",
        f"--sampler-seconds {args.sampler_seconds}",
    ])


def runner_sha12() -> str:
    return (S.sha256_path(CASE_ROOT / "tools" / "run_pilot.py") or "?")[:12]


def arch_base() -> str:
    """Коммит кейса — архитектурная база прогона (AD-2: `arch_base_version`)."""
    try:
        p = subprocess.run(["git", "-C", str(CASE_ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "?"
    return p.stdout.strip() or "?"


def render_plan(args, stages: list[Stage]) -> str:
    run_id = run_id_for(args.ts)
    eta = eta_hours(stages)
    lines = [
        "== S3b: план пилота ревизии (ADR-013 фаза 1: один сид, полный цикл) ==",
        f"стенд:            {args.host} (ssh), образ: {args.image}",
        f"каталог прогона:  стенд {STAND_EXPERIMENTS}/{run_id}/  ←  кейс "
        f"{args.runs_dir}/{run_id}/",
        f"модель/сид:       {args.model}, seed={args.seed}, max_len={args.max_len}",
        f"режим:            обычный режим контура (ADR-014: детерминизм — только для "
        f"проб-доказательств; A5-drift — отдельной 20–50-шаговой пробой)",
        "",
        "стадии (порядок — ADR-013 п.1; артефакт = признак пройденной стадии):",
    ]
    for st in stages:
        lines.append(f"  {st.name:<10} --stage {st.pipeline_stage:<4} шагов={st.steps:<6} "
                     f"batch={st.batch} страж={st.guard_stage:<3} "
                     f"eval_ckpt={st.eval_ckpt:<5} → {st.artifact}")
    lines += [
        f"  (шаги SFT = {args.sft_epochs} эпохи × n_samples / batch; n_samples берётся "
        f"с фактом сверки: ожидается {EXPECT_SFT_SAMPLES})",
        "",
        f"оценка календаря: {eta['total_hours_range'][0]}–{eta['total_hours_range'][1]} ч "
        f"= {eta['total_days_range'][0]}–{eta['total_days_range'][1]} сут "
        f"(CPT {eta['cpt_hours']} ч + SFT {eta['sft_hours']} ч + RL "
        f"{eta['rl_hours_range'][0]}–{eta['rl_hours_range'][1]} ч + eval ≈{eta['eval_hours']} ч)",
        "",
        "предусловия:      страж хозяина ресурса AD-9 перед **каждой** стадией "
        "(порог памяти 35/50/49 ГБ — из стража), llm-platform-* не трогаются, "
        "запуск только через safe_start.sh",
        "стоп-условия:     энтропия политики < 0.5 ×2 подряд → стоп RL; два NVRM/Xid "
        "→ стоп и пауза (маркер var/STOPPED); 0x51 → storm_gap.sh и повтор; "
        f"память < {args.mem_floor_gb} ГБ → стоп; молчание > {args.stall_minutes} мин → стоп",
        f"запуск:           tmux new-session -d -s {args.tmux_session} — цепочка "
        f"pilot_chain.sh (идемпотентна: повторный запуск подхватывает чекпойнты)",
        "манифест AD-2:    на каждую стадию (stand-side, генератор кейса) + манифест "
        "прогона в кейсе; хеши датасетов — whole-file",
        "",
        "чего план НЕ делает: не запускает RL-разведку и детерминизм-пробу (S3-pre/S3a "
        "выполнены), не применяет замкнутый цикл синтеза SFT (ADR-013 п.2 — отдельная "
        "дельта), не трогает чужие чекпойнты, не снимает @reboot и крон (R4+)",
    ]
    return "\n".join(lines)


# ─── предусловия ──────────────────────────────────────────────────────────────

def owner_guard(host: str, stage: str, exp_name: str, lax_unreachable: bool = False) -> dict:
    """Страж AD-9 как предусловие стадии. `lax_unreachable` — НЕ для стадии.

    Флаг `--unreachable-not-verified` существует для профиля fitness-правила;
    предусловие тренировочной стадии зовёт страж **без** него: «не смогли
    проверить» не даёт права стартовать (ADR-007 п.5).
    """
    cmd = ["bash", str(CASE_ROOT / "tools" / "check_resource_owner.sh"),
           "--host", host, "--stage", stage, "--exp-name", exp_name, "--json"]
    if lax_unreachable:
        cmd.append("--unreachable-not-verified")
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
    reasons = []
    if report.get("verdict") != "CAN-START":
        for line in p.stdout.splitlines():
            if line.strip().startswith("·"):
                reasons.append(line.strip().lstrip("· ").strip())
    return {"name": "owner", "ok": ok, "exit": p.returncode, "report": report,
            "stage": stage, "exp_name": exp_name, "reasons": reasons,
            "detail": (f"AD-9 OK: {report.get('verdict', 'CAN-START')}"
                       if ok else
                       f"страж AD-9 красный (exit {p.returncode}): "
                       f"{'; '.join(reasons) or report.get('verdict', 'нет машинного вердикта')}"),
            "output_tail": p.stdout.strip().splitlines()[-8:]}


def check_glm_key(host: str) -> dict:
    """GLM-ключ для eval-стадий: без него eval молча даёт эвристику вместо судьи.

    Значение ключа не читается и не печатается — проверяется только наличие
    строки `GLM_API_KEY=` в `$SHARED/.glm_env` (её же читает рабочий раннер).
    """
    rc, out, err = S.ssh(host, f"grep -c '^GLM_API_KEY=' {GLM_ENV} 2>/dev/null; exit 0")
    present = rc == 0 and out.strip().splitlines() and out.strip().splitlines()[0] not in ("", "0")
    return {"name": "glm_key", "ok": bool(present),
            "detail": (f"GLM_API_KEY найден в {GLM_ENV} — судья eval настоящий (не эвристика)"
                       if present else
                       f"в {GLM_ENV} нет GLM_API_KEY: eval дал бы judge=slug-verifier, "
                       f"то есть подменённую метрику оси (ADR-1)")}


def check_inputs(host: str, args) -> dict:
    """Входы стадий: преток-кэши, пайплайн, обёртки запуска, стабы и PPL-наборы."""
    required = [f"{STAND_SHARED}/{CPT_TOK_CACHE}", f"{STAND_SHARED}/{SFT_TOK_CACHE}",
                f"{STAND_SHARED}/laguna_pipeline_v8.py", SAFE_START, STORM_GAP,
                f"{STAND_SHARED}/wt_stubs", RL_POOL_STAND,
                f"{STAND_SHARED}/{SFT_DATA_JSONL}",
                f"{STAND_SHARED}/datasets/eval_ood_clean.jsonl",
                f"{STAND_SHARED}/datasets/general_eval.txt",
                f"{STAND_SHARED}/datasets/domain_eval.txt"]
    if args.attn == "flex":
        required.append(f"{STAND_SHARED}/{CPT_TOK_POS}")
    rc, out, _ = S.ssh(host, "for f in " + " ".join(required) +
                       '; do [ -e "$f" ] && echo "OK $f" || echo "MISSING $f"; done')
    if rc != 0:
        return {"name": "inputs", "ok": False, "detail": "проверка входов не отработала"}
    missing = [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("MISSING")]
    return {"name": "inputs", "ok": not missing,
            "detail": ("преток-кэши, пайплайн, обёртки, пул ревизии и PPL-наборы на месте"
                       if not missing else f"нет: {', '.join(missing)}"),
            "missing": missing}


def check_pool_integrity(host: str) -> dict:
    """Пул ревизии на стенде — тот же файл, что в кейсе (иначе мерили бы другой пул)."""
    local = S.sha256_path(RL_POOL_CASE)
    rc, out, _ = S.ssh(host, f"sha256sum {RL_POOL_STAND}; wc -l < {RL_POOL_STAND}")
    if rc != 0:
        return {"name": "pool", "ok": False, "detail": "хеш пула не прочитан"}
    parts = out.split()
    remote = parts[0] if parts else None
    lines = next((p for p in reversed(parts) if p.isdigit()), None)
    ok = bool(local and remote and local == remote)
    return {"name": "pool", "ok": ok, "lines": lines,
            "sha256_case": local, "sha256_stand": remote,
            "detail": (f"пул ревизии совпадает с кейсом: {lines} задач, sha256 "
                       f"{str(remote)[:12]}" if ok else
                       "пул на стенде НЕ совпадает с кейсом — стоп (мерили бы другой пул)")}


def check_sft_samples(host: str, expect: int) -> dict:
    """Число примеров SFT — от него считаются шаги эпох (ADR-008/ADR-011).

    Файл читается со стенда, а не из кейса: шаги считает та же копия, по которой
    пойдёт обучение. Расхождение с зафиксированным числом — сигнал и остановка, а
    не молчаливый пересчёт: иначе прогон перестал бы быть тем, что оценивал ADR.
    """
    rc, out, _ = S.ssh(host, f"wc -l < {STAND_SHARED}/{SFT_DATA_JSONL}")
    got = None
    if rc == 0:
        digits = out.strip().split()
        got = int(digits[0]) if digits and digits[0].isdigit() else None
    ok = got == expect
    return {"name": "sft_samples", "ok": ok, "samples": got, "expected": expect,
            "detail": (f"SFT-примеров {got} — совпадает с зафиксированным ({expect})"
                       if ok else
                       f"SFT-примеров {got}, ожидалось {expect} — шаги эпох пересчитаны "
                       f"от факта; расхождение с ADR-008/ADR-011 требует решения, не молчания")}


def check_disk(host: str, min_free_gb: float = 60.0) -> dict:
    """Диск стенда: `*_final` чекпойнты CPT/SFT/RL (~3 ГБ × 3) + hf_rollout/hf_eval."""
    rc, out, _ = S.ssh(host, "df -BG --output=avail /home/user | tail -1")
    avail = None
    if rc == 0:
        m = "".join(ch for ch in out if ch.isdigit())
        avail = int(m) if m else None
    ok = avail is not None and avail >= min_free_gb
    return {"name": "disk", "ok": ok, "avail_gb": avail, "required_gb": min_free_gb,
            "detail": (f"свободно {avail} ГБ (нужно ≥ {min_free_gb:.0f})"
                       if avail is not None else "df не прочитан")}


def preflight(host: str, args, stages: list[Stage]) -> dict:
    """Полный прогон предусловий. ``ok`` — можно ли запускать цепочку."""
    checks: list[dict] = []
    rc, out, err = S.ssh(host, "hostname")
    reachable = rc == 0
    checks.append({"name": "host", "ok": reachable,
                   "detail": (f"{host} → {out.strip()}" if reachable
                              else f"{host} недоступен: {err.strip()}")})
    if not reachable:
        return {"ok": False, "checks": checks, "host": host}
    first = stages[0]
    checks.append(owner_guard(host, first.guard_stage, exp_name_for(first, args.ts)))
    checks.append(S.check_free_memory(host, args.min_free_gb))
    checks.append(S.check_platform_containers(host))
    checks.append(S.check_serialization_guard(host, strict=True))
    checks.append(S.check_image(host, args.image))
    checks.append(check_inputs(host, args))
    checks.append(check_pool_integrity(host))
    checks.append(check_sft_samples(host, args.expect_sft_samples))
    checks.append(check_glm_key(host))
    checks.append(check_disk(host))
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "host": host,
            "dmesg_before": S.dmesg_counts(host)}


# ─── доставка и запуск ────────────────────────────────────────────────────────

def ship_files(host: str, run_dir_stand: str, args, stages: list[Stage]) -> dict:
    """Цепочка, страж, генератор манифеста, сэмплер и таблица стадий — на стенд.

    После доставки сверяются sha256: то, что исполняется на стенде, обязано быть
    байт-в-байт тем, что лежит в кейсе (иначе «прогон из git» — утверждение без
    основания). Таблица стадий — тот же принцип: состав цепочки один.
    """
    payloads = []
    for remote_name, case_rel in SHIPPED:
        path = CASE_ROOT / case_rel
        if not path.is_file():
            return {"ok": False, "error": f"нет инструмента кейса: {case_rel}"}
        payloads.append((remote_name, path.read_bytes()))
    payloads.append(("stages.tsv", stages_tsv(stages, args.ts).encode()))
    shipped = {}
    for name, data in payloads:
        rc, err = S.ssh_put(host, f"{run_dir_stand}/{name}", data)
        if rc != 0:
            return {"ok": False, "error": f"доставка {name} не удалась: {err.strip()}"}
        shipped[name] = S.sha256_bytes(data)
    rc, out, err = S.ssh(host, "sha256sum " + " ".join(f"{run_dir_stand}/{n}" for n in shipped))
    if rc != 0:
        return {"ok": False, "error": f"sha256sum на стенде: {err.strip()}"}
    broken = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and shipped.get(Path(parts[1]).name) != parts[0]:
            broken.append(Path(parts[1]).name)
    if broken or len(out.splitlines()) != len(shipped):
        return {"ok": False, "error": f"файлы на стенде не совпали с отправленными: {broken or out}"}
    case_hashes = {name: S.sha256_path(CASE_ROOT / case_rel)
                   for name, case_rel in SHIPPED if name != "stages.tsv"}
    return {"ok": True, "sha256": shipped,
            "case_sha256": case_hashes,
            "case_matches_ship": all(shipped.get(n) == h for n, h in case_hashes.items()),
            "note": ("stages.tsv — состав цепочки, собранный раннером (единственный "
                     "источник: tools/run_pilot.py)")}


def chain_gate_probe(host: str, run_dir_stand: str, args, stages: list[Stage],
                     timeout: int = 900) -> dict:
    """`pilot_chain.sh --check-only` — предусловие AD-9 по каждой стадии, без обучения.

    Это не «ещё одна проверка рядом»: вердикт выносит **та же цепочка**, которая
    пойдёт в работу, и тем же вызовом стража. Расхождение с вердиктом раннера —
    инцидент (стенд изменился между двумя вызовами либо проверки разошлись).
    """
    cmd = chain_command(args, run_dir_stand, stages) + " --check-only"
    rc, out, err = S.ssh(host, cmd, timeout=timeout)
    #: Отказ (и вердикт по каждой стадии) остаётся на стенде в каталоге прогона —
    #: это тот же «лог, в котором названа причина», что и при запуске из tmux;
    #: без этого отказ жил бы только в выводе раннера.
    payload = out + (("\n[stderr]\n" + err) if err.strip() else "")
    S.ssh_put(host, f"{run_dir_stand}/logs/chain-check.log", payload.encode())
    if rc == 124:
        return {"ok": False, "exit": 124, "output": out,
                "detail": f"проверка цепочки не завершилась за {timeout} с"}
    blocked_stage = None
    for line in out.splitlines():
        if "ОТКАЗ СТРАЖА: стадия" in line:
            blocked_stage = line.split("стадия", 1)[1].split()[0]
            break
    return {"ok": rc == 0, "exit": rc, "blocked_stage": blocked_stage,
            "command": cmd,
            "output": payload,
            "output_tail": out.strip().splitlines()[-40:],
            "detail": (f"цепочка пропускает все стадии ({len(stages)})"
                       if rc == 0 else
                       f"цепочка отказала (exit {rc})"
                       + (f" на стадии {blocked_stage}" if blocked_stage else "")),
            "stderr_tail": err.strip().splitlines()[-5:]}


def launch_tmux(host: str, run_dir_stand: str, args, stages: list[Stage]) -> dict:
    """Запуск цепочки в tmux стенда: она живёт там ≈3.5 суток, раннер возвращает управление."""
    cmd = chain_command(args, run_dir_stand, stages)
    log = f"{run_dir_stand}/logs/chain.log"
    tmux_cmd = (f"tmux kill-session -t {args.tmux_session} 2>/dev/null; "
                f"tmux new-session -d -s {args.tmux_session} "
                f"\"{cmd} >> {log} 2>&1\"")
    rc, out, err = S.ssh(host, tmux_cmd, timeout=120)
    if rc != 0:
        return {"ok": False, "detail": f"tmux не поднял сессию: {err.strip() or out.strip()}"}
    time.sleep(args.verify_seconds)
    rc, sessions, _ = S.ssh(host, "tmux ls 2>&1")
    alive = args.tmux_session in sessions
    rc2, log_tail, _ = S.ssh(host, f"tail -n 40 {log} 2>/dev/null")
    rc3, containers, _ = S.ssh(
        host, f"docker ps --format '{{{{.Names}}}} {{{{.ID}}}} {{{{.Status}}}}' "
              f"| grep '^{CONTAINER_PREFIX}{args.ts}' || true")
    return {"ok": True, "command": cmd, "log": log, "tmux_command": tmux_cmd,
            "session_alive": alive, "sessions": sessions.strip().splitlines()[-5:],
            "log_tail": log_tail.strip().splitlines()[-25:],
            "containers": [l for l in containers.strip().splitlines() if l.strip()],
            "verified_seconds": args.verify_seconds,
            "detail": (f"сессия {args.tmux_session} {'жива' if alive else 'уже завершилась'} "
                       f"(см. {log}); контейнеров пилота: "
                       f"{len([l for l in containers.splitlines() if l.strip()])}")}


def stop_containers(host: str, ts: str) -> dict:
    """План отката: закрыть контейнеры пилота. Чекпойнты не удаляются."""
    rc, out, err = S.ssh(host, f"docker ps -a --format '{{{{.Names}}}}'")
    if rc != 0:
        return {"ok": False, "stopped": [], "detail": f"docker ps недоступен: {err.strip()}"}
    names = [n for n in out.split() if n.startswith(f"{CONTAINER_PREFIX}{ts}")]
    if not names:
        return {"ok": True, "stopped": [],
                "detail": f"контейнеров пилота {ts} нет (уже закрыты)"}
    rc2, out2, err2 = S.ssh(host, "docker stop " + " ".join(names), timeout=300)
    if rc2 != 0:
        return {"ok": False, "stopped": [], "detail": f"docker stop rc={rc2}: {err2.strip()}"}
    return {"ok": True, "stopped": names,
            "detail": (f"остановлены: {', '.join(names)}; чекпойнты на месте (не удаляются)")}


def clear_stop(host: str, run_dir_stand: str, reason: str) -> dict:
    """Снятие паузы — действие владельца, а не цепочки (ADR-010 п.5).

    Причина пишется в `var/stop-cleared.log` до снятия маркера: пауза, снятая без
    причины, неотличима от «не заметили».
    """
    marker = f"{run_dir_stand}/var/STOPPED"
    rc, out, _ = S.ssh(host, f"[ -f {marker} ] && cat {marker} || echo '(маркера нет)'")
    was = out.strip()
    if "(маркера нет)" in was:
        return {"ok": True, "cleared": False, "reason": reason,
                "detail": "маркера паузы нет — снимать нечего"}
    rc2, _, err2 = S.ssh(host,
                         f"printf '%s\\t%s\\n' \"$(date -Is)\" {json.dumps(reason)} >> "
                         f"{run_dir_stand}/var/stop-cleared.log && rm -f {marker}")
    if rc2 != 0:
        return {"ok": False, "cleared": False, "reason": reason,
                "detail": f"маркер не снят: {err2.strip()}"}
    return {"ok": True, "cleared": True, "reason": reason, "was": was,
            "detail": f"пауза снята владельцем: «{reason}» (было: {was})"}


# ─── артефакты кейса ──────────────────────────────────────────────────────────

def write_manifest(run_dir: Path, args, stages: list[Stage], statuses: dict) -> Path | None:
    """Манифест AD-2 прогона в кейсе — генератором кейса (одна реализация на всё)."""
    spec = ",".join(f"{st.name}={statuses.get(st.name, 'pending')}" for st in stages)
    cmd = [sys.executable, str(CASE_ROOT / "tools" / "write_run_manifest.py"),
           "--run-dir", str(run_dir),
           "--dataset", str(CASE_ROOT / CPT_TOK_CACHE),
           "--base-model", args.model,
           "--pipeline", str(CASE_ROOT / "laguna_pipeline_v8.py"),
           "--seed", str(args.seed), "--image", args.image,
           "--stages", spec,
           "--run-version", f"tools/run_pilot.py@{runner_sha12()}",
           "--dataset-extra", f"sft={CASE_ROOT / SFT_DATA_JSONL}",
           "--dataset-extra", f"rl_pool={RL_POOL_CASE}",
           "--dataset-extra", f"eval={CASE_ROOT / 'datasets' / 'eval_ood_clean.jsonl'}",
           "--hyperparams", f"arch_base_version={json.dumps(arch_base())}",
           "--hyperparams", f"rl_steps={args.rl_steps}",
           "--hyperparams", f"sft_epochs={args.sft_epochs}",
           "--hyperparams", f"eval_items={args.eval_items}",
           "--hyperparams", "mode=обычный режим контура (ADR-014: детерминизм только для проб)",
           "--hyperparams-source",
           "фактические значения запуска пилота (tools/run_pilot.py → pilot_chain.sh)",
           "--relative-to", str(CASE_ROOT), "--force"]
    if all(s == "done" for s in statuses.values()) and statuses:
        cmd.append("--complete")
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"ПРЕДУПРЕЖДЕНИЕ: манифест не записан (rc={p.returncode}): "
              f"{p.stderr.strip() or p.stdout.strip()}", file=sys.stderr)
        return None
    return run_dir / "run_manifest.json"


def stand_state(host: str, pre: dict) -> dict:
    """Состояние стенда: контейнеры пилота, платформа, память, dmesg-дельта."""
    rc, out, _ = S.ssh(host, "docker ps --format '{{.Names}}'")
    running = out.split() if rc == 0 else []
    rc2, mem_raw, _ = S.ssh(host, "cat /proc/meminfo")
    return {
        "containers": running,
        "pilot_containers": [n for n in running if n.startswith(CONTAINER_PREFIX)],
        "platform_alive": [n for n in running if n.startswith(PLATFORM_PREFIX)],
        "platform_lost": sorted(set((pre.get("platform") or {}).keys())
                                - set(n for n in running if n.startswith(PLATFORM_PREFIX))),
        "mem_available_gb": S.free_gb(S.parse_meminfo(mem_raw)) if rc2 == 0 else None,
        "dmesg_after": S.dmesg_counts(host),
    }


def fetch_stand_files(host: str, run_dir_stand: str, local_dir: Path) -> dict:
    """Забрать в кейс лог цепочки, статусы стадий и stand-side манифест AD-2."""
    got, missing = {}, []
    for remote, local in (("logs/chain.log", "logs/chain.log"),
                          ("run_manifest.json", "stand_run_manifest.json"),
                          ("var/chain.status", "chain.status"),
                          ("var/STOPPED", "STOPPED")):
        rc, data, _ = S.ssh_get(host, f"{run_dir_stand}/{remote}", timeout=120)
        if rc == 0 and data:
            path = local_dir / local
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            got[local] = len(data)
        else:
            missing.append(remote)
    # статусы стадий: var/status/<name>
    rc, out, _ = S.ssh(host, f"ls {run_dir_stand}/var/status 2>/dev/null || true")
    statuses = {}
    for name in out.split():
        rc2, data, _ = S.ssh_get(host, f"{run_dir_stand}/var/status/{name}", timeout=60)
        if rc2 == 0 and data:
            (local_dir / "status").mkdir(parents=True, exist_ok=True)
            (local_dir / "status" / name).write_bytes(data)
            fields = data.decode("utf-8", "replace").strip().split("\t")
            statuses[name] = {"status": fields[0] if fields else "?",
                              "finished_at": fields[1] if len(fields) > 1 else None,
                              "wall_seconds": fields[2] if len(fields) > 2 else None,
                              "artifact_sha256": fields[3] if len(fields) > 3 else None,
                              "note": fields[4] if len(fields) > 4 else None}
    return {"fetched": got, "missing": missing, "stage_statuses": statuses}


def launch_statuses(stages: list[Stage]) -> dict:
    """Статусы стадий на момент запуска: ни одна не `done`.

    `done` в манифесте AD-2 — это артефакт стадии на месте (тот же критерий, что у
    цепочки: `stage_done_strict`). Стадия, которая только что стартовала, работы ещё
    не сделала, и объявлять её завершённой — утверждение без основания: файл
    `runs/<id>/run_manifest.json` коммитится в кейс и читается гейтом C-012 как
    свидетельство. Настоящие статусы приносит `--analyze-only` со стенда.
    """
    return {st.name: "pending" for st in stages}


def build_evidence(args, stages: list[Stage], pre: dict, shipped: dict, gate: dict,
                   launch: dict | None, post: dict, status: str, reason: str,
                   run_dir: Path) -> dict:
    """``evidence/s3-pilot.json`` — что проверено, чем запущено, чем это кончилось."""
    return {
        "stage": "S3b",
        "date": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "status_reason": reason,
        "purpose": ("пилот ревизии (ADR-013 фаза 1): один сид, полный цикл "
                    "CPT → SFT → RL → eval на Qwen2.5-0.5B; обычный режим контура "
                    "(ADR-014), замкнутый цикл синтеза SFT не применяется (ADR-013 п.2)"),
        "basis": [
            "ADR-013 п.1 — состав и порядок стадий пилота; п.2 — замороженный sft_train_v12; "
            "п.3 — точка решения после пилота; п.5 — проба S3a завершена до старта",
            "ADR-014 — детерминированный режим только для проб-доказательств",
            "ADR-008/ADR-011 — пороги памяти по стадиям (CPT 35 / SFT 50 / RL 49 ГБ) и календарь",
            "ADR-010 п.5 и SPEC §6 — стоп-условия (энтропия < 0.5 ×2, два NVRM, 0x51 → storm_gap)",
            "AD-9 / ADR-012 — хозяин ресурса объявлен; активный @reboot-автозапуск блокирует старт",
        ],
        "config": {
            "model": args.model, "seed": args.seed, "max_len": args.max_len,
            "max_samples": args.max_samples, "image": args.image, "attn": args.attn,
            "mem_fraction": args.mem_fraction, "mem_cap": args.mem_cap,
            "rl_steps": args.rl_steps, "sft_epochs": args.sft_epochs,
            "eval_items": args.eval_items, "peak_lr_scale": args.peak_lr_scale,
            "resync_every": args.resync_every,
            "gen_eval": {"cpt": args.cpt_gen_eval_every, "rl": args.rl_gen_eval_every},
            "stop_conditions": {"entropy_floor": args.entropy_floor,
                                "entropy_low_streak": args.entropy_low_streak,
                                "nvrm_limit": args.nvrm_limit,
                                "mem_floor_gb": args.mem_floor_gb,
                                "stall_minutes": args.stall_minutes,
                                #: Критерий вырожденной награды — не порог раннера, а
                                #: критерий ADR-017, живущий в приборе
                                #: (tools/rl_degeneracy.py). Здесь только окно и путь:
                                #: пороги классов дублировать нельзя — их единственный
                                #: носитель — прибор, и он же их печатает.
                                "degenerate_reward": {
                                    "criterion": "ADR-017",
                                    "tool": "rl_degeneracy.py",
                                    "window_steps": args.degeneracy_window,
                                    "thresholds_source": "прибор tools/rl_degeneracy.py "
                                                         "(ADR-017 п.1 + спайн AD-1)"}},
            "launch": f"{SAFE_START} -d 60 -i 5 -- docker run (absorbер включён)",
            "deterministic_mode": "off (обычный режим контура, ADR-014)",
        },
        "stand": {"host": args.host, "run_dir": launch.get("run_dir") if launch
                  else f"{STAND_EXPERIMENTS}/{run_id_for(args.ts)}",
                  "case_run_dir": S.rel_or_abs(run_dir)},
        "stages": [asdict(st) | {"exp_name": exp_name_for(st, args.ts),
                                 "container": container_for(st, args.ts),
                                 "guard_threshold_gb": (pre.get("owner") or {})
                                 .get("report", {}).get("threshold_gb")
                                 if st is stages[0] else None}
                   for st in stages],
        "eta": eta_hours(stages),
        "preconditions": {
            "ok": pre.get("ok"),
            "checks": pre.get("checks", []),
            "owner_guard": pre.get("owner"),
            "dmesg_before": pre.get("dmesg_before"),
        },
        "ship": shipped,
        "chain_gate": gate,
        "launch": launch,
        "postcheck": post,
        "verification": {
            "tmux_session": args.tmux_session,
            "session_alive": (launch or {}).get("session_alive"),
            "containers": (launch or {}).get("containers"),
            "log_tail": (launch or {}).get("log_tail"),
            "first_stage": stages[0].name if stages else None,
            "chain_command": (launch or {}).get("command") or (gate or {}).get("command"),
        },
        "next_actions": [
            *([f"смотреть прогресс: ssh {args.host} 'tail -f "
               f"{STAND_EXPERIMENTS}/{run_id_for(args.ts)}/logs/chain.log'"] if launch else
              [f"запуск не состоялся — лога ещё нет; каталог прогона на стенде: "
               f"ssh {args.host} 'ls {STAND_EXPERIMENTS}/{run_id_for(args.ts)}'"]),
            f"статусы стадий: ssh {args.host} 'ls "
            f"{STAND_EXPERIMENTS}/{run_id_for(args.ts)}/var/status'",
            f"пересобрать evidence: python3 tools/run_pilot.py --analyze-only --ts {args.ts}",
            f"план отката: python3 tools/run_pilot.py --stop-only --ts {args.ts} "
            f"(чекпойнты не удаляются)",
            f"пауза после стоп-условия: python3 tools/run_pilot.py --clear-stop \"причина\" "
            f"--ts {args.ts} — решение владельца",
        ],
        "not_verified": [
            "точка решения ADR-013 п.3 (направление оси, отсутствие дегенерации, "
            "ppl_general, полнота доказательств) — считается после завершения пилота, "
            "а не при запуске",
            "разброс по сидам и статистика AD-1: пилот — один сид; mean±std требует фазы 2",
            "воспроизводимость прогона по хешам: прогон идёт в обычном режиме контура "
            "(ADR-014), поэтому бит-в-бит не воспроизводим — воспроизводимость кода "
            "доказана пробой S3a",
            "замкнутый цикл синтеза SFT: вынесен за пределы пилота (ADR-013 п.2)",
            #: Условно по факту запуска: при `launch = null` запуска не было, и
            #: утверждать «подтверждён tmux и логом» — значит подменять evidence
            #: желаемым (integrity, а не косметика).
            *(["исполнитель не ждёт завершения пилота (≈3.5 суток): факт запуска подтверждён "
               "tmux-сессией и первыми строками лога, факт сходимости — нет"] if launch else
              ["запуск в этом прогоне не состоялся (см. status_reason): ни tmux-сессия, "
               "ни шаги стадии не подтверждены — подтверждено только состояние "
               "предусловий на момент прогона"]),
        ],
        "instrument": {
            "chain_sha256": shipped.get("sha256", {}).get("pilot_chain.sh"),
            "guard_sha256": shipped.get("sha256", {}).get("check_resource_owner.sh"),
            "manifest_tool_sha256": shipped.get("sha256", {}).get("write_run_manifest.py"),
            "runner_sha256": S.sha256_path(CASE_ROOT / "tools" / "run_pilot.py"),
            "case_matches_ship": shipped.get("case_matches_ship"),
            "note": ("порогов памяти в раннере нет: их единственный источник — страж AD-9, "
                     "он же печатает порог в вердикте; состав цепочки — один файл stages.tsv"),
        },
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="S3b: пилот ревизии на GB10 (ADR-013 фаза 1) — один сид, полный цикл "
                    "CPT → SFT → RL → eval; запуск в tmux и возврат управления.")
    ap.add_argument("--host", default=os.environ.get("GB10_HOST", DEFAULT_HOST),
                    help=f"стенд по ssh (по умолчанию {DEFAULT_HOST} / $GB10_HOST)")
    ap.add_argument("--ts", default=datetime.now().strftime("%Y%m%d-%H%M"),
                    help="метка прогона; повторный запуск раннера с той же меткой "
                         "подхватывает каталог прогона (идемпотентность)")
    ap.add_argument("--image", default=os.environ.get("LAGUNA_IMAGE", DEFAULT_IMAGE),
                    help="образ окружения")
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B", help="базовая модель (ADR-002)")
    ap.add_argument("--seed", type=int, default=42, help="сид пилота (ADR-013: 42)")
    ap.add_argument("--max-len", type=int, default=8192, help="длина чанка/примера")
    ap.add_argument("--cpt-steps", type=int, default=9776,
                    help="шагов CPT (полный проход корпуса v12r, ADR-008)")
    ap.add_argument("--cpt-batch", type=int, default=1,
                    help="батч CPT (flex eager — только 1, конфигурация лесенки)")
    ap.add_argument("--sft-epochs", type=int, default=3, help="эпох SFT (ADR-013 п.1)")
    ap.add_argument("--sft-batch", type=int, default=2, help="батч SFT (как в лесенке для 0.5B)")
    ap.add_argument("--rl-steps", type=int, default=500, help="шагов RL (ADR-013 п.1)")
    ap.add_argument("--max-samples", type=int, default=50000, help="примеров из кэшей")
    ap.add_argument("--peak-lr-scale", type=float, default=0.7,
                    help="множитель пикового LR WSD (анти-форгеттинг, как в лесенке)")
    ap.add_argument("--attn", default="flex", choices=["flex", "none"],
                    help="LAGUNA_ATTN (flex — путь рабочего раннера)")
    ap.add_argument("--mem-fraction", default="0.6", help="LAGUNA_MEM_FRACTION")
    ap.add_argument("--mem-cap", default="100g", help="cgroup-кап памяти контейнера")
    ap.add_argument("--cpt-gen-eval-every", type=int, default=50,
                    help="CPT_GEN_EVAL_EVERY (дефолт лесенки; анти-форгеттинг)")
    ap.add_argument("--rl-gen-eval-every", type=int, default=25,
                    help="GEN_EVAL_EVERY для RL (дефолт пайплайна)")
    ap.add_argument("--resync-every", type=int, default=10,
                    help="RESYNC_EVERY (дефолт RL-петли; задаётся явно, чтобы манифест не печатал 25)")
    ap.add_argument("--vllm-gpu-util", default="0.32", help="VLLM_GPU_UTIL (как в лесенке)")
    ap.add_argument("--kv-cache-bytes", default="8589934592", help="VLLM_KV_CACHE_BYTES")
    ap.add_argument("--vllm-eager", default="0", help="VLLM_EAGER")
    ap.add_argument("--eval-items", type=int, default=192,
                    help="задач eval (весь eval_ood_clean.jsonl; дефолт пайплайна — 100)")
    ap.add_argument("--expect-sft-samples", type=int, default=EXPECT_SFT_SAMPLES,
                    help="ожидаемое число примеров SFT (сверка перед расчётом шагов эпох)")
    ap.add_argument("--min-free-gb", type=float, default=35.0,
                    help="порог памяти раннера — тот же, что у стража для CPT (ADR-008)")
    ap.add_argument("--entropy-floor", type=float, default=0.5, help="стоп-порог энтропии (ADR-010 п.5)")
    ap.add_argument("--entropy-low-streak", type=int, default=2, help="наблюдений ниже порога подряд = стоп")
    ap.add_argument("--nvrm-limit", type=int, default=2, help="NVRM/Xid-инцидентов = стоп и пауза")
    ap.add_argument("--mem-floor-gb", type=float, default=4.0,
                    help="предохранитель памяти стенда во время стадии")
    ap.add_argument("--degeneracy-window", type=int, default=50,
                    help="окно критерия вырожденной награды в шагах (ADR-017 п.1: 50; "
                         "число не подбирается здесь, а наследуется из решения)")
    ap.add_argument("--stall-minutes", type=int, default=120,
                    help="порог молчания стадии (wedge) в минутах")
    ap.add_argument("--poll-seconds", type=int, default=5, help="период стража стадии")
    ap.add_argument("--stage-retries", type=int, default=1,
                    help="повторов стадии после окна затишья (0x51)")
    ap.add_argument("--sampler-seconds", type=int, default=None,
                    help="окно сэмплера памяти (по умолчанию — из календаря стадий)")
    ap.add_argument("--tmux-session", default="pilot-compact-s42", help="имя tmux-сессии на стенде")
    ap.add_argument("--verify-seconds", type=int, default=45,
                    help="сколько секунд ждать перед проверкой запуска (tmux/контейнер/лог)")
    ap.add_argument("--runs-dir", default="runs", help="каталог прогонов кейса")
    ap.add_argument("--evidence", default="evidence/s3-pilot.json", help="evidence-файл")
    ap.add_argument("--plan", action="store_true", help="напечатать план и выйти")
    ap.add_argument("--preflight-only", action="store_true", help="только предусловия")
    ap.add_argument("--ship-only", action="store_true",
                    help="доставка и вердикт цепочки (--check-only), без tmux")
    ap.add_argument("--analyze-only", action="store_true",
                    help="пересобрать evidence и манифест по артефактам прогона")
    ap.add_argument("--stop-only", action="store_true", help="закрыть контейнеры пилота (откат)")
    ap.add_argument("--clear-stop", metavar="ПРИЧИНА", default=None,
                    help="снять паузу после стоп-условия (решение владельца)")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)
    if args.plan and args.analyze_only:
        ap.error("--plan и --analyze-only несовместимы")
    if args.sampler_seconds is None:
        #: окно сэмплера — на самую длинную стадию (SFT) с запасом: сэмплер нужен
        #: для пика unified-памяти, а не как сторож времени (сторож — в цепочке).
        args.sampler_seconds = int(max(3600, 12 * 3600))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_id = run_id_for(args.ts)
    run_dir = CASE_ROOT / args.runs_dir / run_id
    run_dir_stand = f"{STAND_EXPERIMENTS}/{run_id}"

    #: Таблица стадий строится до обращения к стенду: `--plan` не должен требовать
    #: доступа к нему, а число SFT-шагов берётся из зафиксированного факта и
    #: сверяется со стендом в предусловиях (см. check_sft_samples).
    stages = build_stages(args, args.expect_sft_samples)

    if args.plan:
        print(render_plan(args, stages))
        return EXIT_OK

    if args.stop_only:
        res = stop_containers(args.host, args.ts)
        print(f"[run_pilot] закрытие контейнеров пилота: {res['detail']}")
        return EXIT_OK if res["ok"] else EXIT_FAIL

    if args.clear_stop is not None:
        res = clear_stop(args.host, run_dir_stand, args.clear_stop)
        print(f"[run_pilot] пауза: {res['detail']}")
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
        fetched = fetch_stand_files(args.host, run_dir_stand, run_dir)
        statuses = {st.name: "pending" for st in stages}
        for name, info in fetched["stage_statuses"].items():
            if name in statuses:
                statuses[name] = info["status"]
        post = stand_state(args.host, existing.get("preconditions", {}))
        manifest = write_manifest(run_dir, args, stages, statuses)
        ev = build_evidence(args, stages, existing.get("preconditions", {}),
                            existing.get("ship", {}), existing.get("chain_gate", {}),
                            existing.get("launch"), post,
                            existing.get("status", "partial"),
                            existing.get("status_reason", ""), run_dir)
        ev["rebuilt"] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "stage_statuses": statuses,
            "fetched": fetched["fetched"], "missing": fetched["missing"],
            "note": ("сводка пересобрана из артефактов прогона; предусловия, доставка и "
                     "вердикт цепочки взяты из прежнего evidence и повторным замером не "
                     "подменялись (состояние стенда — текущее)"),
        }
        S.write_evidence(ev_path, ev)
        print(f"[run_pilot] evidence пересобран: {args.evidence}; статусы стадий: "
              + ", ".join(f"{k}={v}" for k, v in statuses.items()))
        print(f"[run_pilot] манифест: {S.rel_or_abs(manifest) if manifest else '—'}")
        return EXIT_OK

    # ── предусловия ───────────────────────────────────────────────────────────
    pre = preflight(args.host, args, stages)
    pre["owner"] = next((c for c in pre["checks"] if c.get("name") == "owner"), {})
    pre["platform"] = next((c.get("platform", {}) for c in pre["checks"]
                            if c["name"] == "containers"), {})
    print(f"== предусловия ({args.host}) ==")
    for c in pre["checks"]:
        print(f"  [{'ok ' if c['ok'] else 'FAIL'}] {c['name']:<14} {c['detail']}")
    if args.preflight_only:
        if args.json:
            print(json.dumps(pre, ensure_ascii=False, indent=2))
        return EXIT_OK if pre["ok"] else EXIT_FAIL
    if not pre["checks"][0]["ok"]:
        print(f"\nСТОП: стенд {args.host} недоступен — пилот не запускается")
        return EXIT_FAIL

    # ── доставка ──────────────────────────────────────────────────────────────
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "stages.tsv").write_text(stages_tsv(stages, args.ts), encoding="utf-8")
    (run_dir / "pilot_chain.sh").write_text(
        (CASE_ROOT / "tools" / "pilot_chain.sh").read_text(encoding="utf-8"), encoding="utf-8")
    shipped = ship_files(args.host, run_dir_stand, args, stages)
    print(f"\n== доставка на стенд ({run_dir_stand}) ==")
    if not shipped["ok"]:
        print(f"СТОП: {shipped['error']}")
        return EXIT_FAIL
    for name, sha in sorted(shipped["sha256"].items()):
        mark = "" if shipped["case_matches_ship"] or name == "stages.tsv" else "  ← расходится с кейсом"
        print(f"  {name:<26} {sha[:16]}…{mark}")

    # ── вердикт цепочки (та же цепочка, что пойдёт в работу) ──────────────────
    gate = chain_gate_probe(args.host, run_dir_stand, args, stages)
    if gate.get("output"):
        (run_dir / "chain-check.log").write_text(gate["output"], encoding="utf-8")
    print(f"\n== право стартовать (pilot_chain.sh --check-only) ==")
    for line in (gate.get("output_tail") or [])[-12:]:
        print(f"  {line}")
    print(f"  вердикт цепочки: exit {gate['exit']} — {gate.get('detail')}")

    launch = None
    status, reason = "preflight_failed", ""
    if not pre["ok"]:
        failed = [c["name"] for c in pre["checks"] if not c["ok"]]
        status = "blocked" if "owner" in failed else "preflight_failed"
        owner = pre["owner"]
        reason = (f"предусловие не выполнено ({', '.join(failed)}); хозяин ресурса не доказан: "
                  f"{'; '.join(owner.get('reasons') or [])}" if "owner" in failed else
                  f"предусловие не выполнено: {', '.join(failed)}")
        print(f"\nСТОП: стадия не стартует — {reason}")
        print("Пилот НЕ запущен; каталог прогона и цепочка на стенде готовы: "
              f"повторный запуск `python3 tools/run_pilot.py --ts {args.ts}` "
              f"начнётся с той же точки.")
    elif not gate["ok"]:
        # Вердикты разошлись: раннер зелёный, цепочка — нет. Это инцидент, а не
        # «повезло»: старт не выполняется, причина фиксируется как есть.
        status = "blocked"
        reason = (f"вердикт цепочки красный (exit {gate['exit']}) при зелёных "
                  f"предусловиях раннера — расхождение вердиктов: {gate.get('detail')}")
        print(f"\nСТОП: {reason}")
    elif args.ship_only:
        status = "shipped"
        reason = "доставка и вердикт цепочки выполнены; запуск не запрошен (--ship-only)"
        print(f"\n--ship-only: пилот не запускается; вердикт цепочки зелёный, всё готово")
    else:
        launch = launch_tmux(args.host, run_dir_stand, args, stages)
        launch["run_dir"] = run_dir_stand
        if not launch["ok"]:
            status, reason = "launch_failed", launch["detail"]
            print(f"\nСТОП: не удалось запустить цепочку: {launch['detail']}")
        else:
            status = "launched"
            reason = launch["detail"]
            print(f"\n== запуск ==")
            print(f"  tmux:       {args.tmux_session} "
                  f"({'жива' if launch['session_alive'] else 'уже завершилась'})")
            for c in launch["containers"] or []:
                print(f"  контейнер:  {c}")
            for line in (launch["log_tail"] or [])[-8:]:
                print(f"  лог: {line}")

    statuses = launch_statuses(stages)
    manifest = write_manifest(run_dir, args, stages, statuses)
    ev = build_evidence(args, stages, pre, shipped, gate, launch,
                        stand_state(args.host, pre), status, reason, run_dir)
    S.write_evidence(CASE_ROOT / args.evidence, ev)
    (run_dir / "pilot_run.json").write_text(
        json.dumps({"ts": args.ts, "host": args.host, "status": status,
                    "status_reason": reason, "stages": [asdict(s) for s in stages],
                    "exp_names": {s.name: exp_name_for(s, args.ts) for s in stages},
                    "ship_sha256": shipped.get("sha256"), "chain_gate_exit": gate.get("exit"),
                    "chain_command": gate.get("command") or (launch or {}).get("command"),
                    "evidence": args.evidence}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"\nманифест: {S.rel_or_abs(manifest) if manifest else '—'}")
    print(f"evidence: {args.evidence}")
    print(f"статус:   {status} — {reason}")
    if args.json:
        print(json.dumps({"status": status, "reason": reason, "preflight_ok": pre["ok"],
                          "chain_gate": {k: gate.get(k) for k in
                                         ("ok", "exit", "blocked_stage", "detail")},
                          "launch": launch}, ensure_ascii=False, indent=2))
    return EXIT_OK if status in ("launched", "shipped") else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
