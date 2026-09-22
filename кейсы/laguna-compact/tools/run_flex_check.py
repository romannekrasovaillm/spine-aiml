#!/usr/bin/env python3
"""S3k — диагностика причины форгеттинга: flex-маска против штатного внимания (ADR-022 п.1).

Что запускается. Два **коротких CPT-прогона по 200 шагов**, различающиеся ровно
одним: режимом внимания.

* рука **A** — `LAGUNA_ATTN=flex` (как шёл CPT пилота): блочная маска из сбросов
  `position_ids` (`patch_flex_attention.py`), загрузка через `/workspace/shared`;
* рука **B** — `LAGUNA_ATTN=none`: того же пайплайна ветка «без flex», то есть
  `_flex_mode=False` → `P.register()`/`P.attach()` не вызываются, `forward` идёт без
  `position_ids`, внимание — штатный SDPA transformers. Отключение flex —
  **отсутствие переменной** `LAGUNA_ATTN=flex`, а не отдельный флаг пайплайна;
  поэтому проверка «режим действительно штатный» идёт по логу (`ATTN: flex_docmask`
  есть в A и отсутствует в B), а не по коду возврата.

Всё остальное совпадает: Qwen2.5-0.5B, микс v12r (`cpt_corpus_v12r_8192_qwen25.npy`
+ pos-компаньон), batch 1, `max_len 8192`, `seed 42`, `peak_lr_scale 0.7`. Пик LR у
обоих прогонов один и тот же — `wsd_optimal_lr` упирается в потолок `5e-4`, и
`×0.7` даёт `3.50e-04` и на 200 шагах, и на 9776 (числа напечатаны в манифесте).
Форма расписания WSD при этом **другая**, чем у пилота: `warmup = min(100, steps//10)`
даёт 20 шагов вместо 100, то есть на 200-шаговом окне LR выходит на пик раньше —
это делает окно чувствительнее к деградации, а не мягче.

Почему копия пайплайна, а не рабочий файл. Контурный `laguna_pipeline_v8.py`
читается живыми стадиями (`--pipeline-ctr /workspace/shared/laguna_pipeline_v8.py`)
и править его нельзя (AD-4/AD-7, запрет задания). Нужны же две вещи, которых в
нём нет:

1. **траектория loss по шагам** — основание вердикта (ADR-022 п.4: сводка по окнам
   запрещена; в пайплайне `CPT step` печатается раз в 50 шагов, то есть на 200-шаговом
   прогоне это 4 точки вместо 200);
2. **номерные чекпойнты внутри окна** — пайплайн сохраняет `step % 200 == 0`, а при
   `max_steps=200` цикл заканчивается на шаге 199, поэтому без правки на руке
   остаётся один `checkpoint_final.pt` и «когда именно разошлось» не видно.

Поэтому раннер собирает **копию** в каталоге прогона: два текстовых патча с якорями
(применяются с проверкой, что якорь ровно один), sha256 до и после, unified diff —
всё в отчёт. Копия едет в контейнер как `--pipeline-ctr` и она же хешируется в
манифесте AD-2: что исполнялось — то и записано.

Замеры. Траектория — из `logs/loss_trace.jsonl` (пишет патч); PPL — из GEN-EVAL
прогона (v1, внутри окна) и из `tools/flex_ppl_probe.py` (v1+v2, чекпойнты шагов
50/100/150/200 и база); время шага — из меток траектории; память — из
`mem_cpt.jsonl` сэмплера. Вердикт считает `analyze`, но печатает и все числа, по
которым его можно перечесть руками.

Чего раннер НЕ делает: не трогает рабочий пайплайн, чекпойнты пилота и
`llm-platform-*`; не запускает полный CPT и не меняет микс (это ADR-022 п.2);
не снимает и не ставит флаг паузы (AD-9) — только спрашивает стража.

Запуск::

    python3 tools/run_flex_check.py --plan
    python3 tools/run_flex_check.py --preflight            # только предусловия
    python3 tools/run_flex_check.py --run                  # обе руки, затем PPL
    python3 tools/run_flex_check.py --analyze-only         # evidence из готовых артефактов
    python3 tools/run_flex_check.py --stop-only            # закрыть контейнеры диагностики
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Транспорт (ssh/put/get), сэмплер памяти и разбор манифестов — общие с S2/S3-pre/
#: S3a/S3b: своя вторая реализация ssh-доставки разошлась бы с первой на первой правке.
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

PIPELINE_CASE = CASE_ROOT / "laguna_pipeline_v8.py"
PIPELINE_COPY_NAME = "laguna_pipeline_flexcheck.py"
PPL_PROBE_NAME = "flex_ppl_probe.py"

RUN_PREFIX = "flex-check"
PPL_RUN_SUFFIX = "ppl"

MODEL = "Qwen/Qwen2.5-0.5B"
SEED = 42
MAX_LEN = 8192
MAX_SAMPLES = 50000
PEAK_LR_SCALE = 0.7
CPT_STEPS = 200
CPT_BATCH = 1

#: Шаг номерного чекпойнта внутри окна (патч копии). 50 выбран так, чтобы точки
#: совпали с шагами GEN-EVAL: тогда PPL v1 из лога и PPL v1/v2 из пробы лежат в
#: одних и тех же состояниях и сверяются между собой.
CKPT_EVERY = 50

#: Руки диагностики. `attn` уезжает в `LAGUNA_ATTN`; «none» — это отсутствие
#: flex-режима, ровно как у прочих стадий контура.
ARMS: dict[str, dict] = {
    "a": {"label": "a_flex", "attn": "flex",
          "desc": "LAGUNA_ATTN=flex — блочная маска из position_ids (как CPT пилота)"},
    "b": {"label": "b_stock", "attn": "none",
          "desc": "LAGUNA_ATTN=none — штатный SDPA, flex не подключается"},
}
ARM_ORDER = ("a", "b")

#: Наборы GEN-EVAL: v1 — те, по которым шёл пилот; v2 — прибор фазы 2 (ADR-018).
#: Два пути на каждый набор — **хозяйский** (для подписи в evidence и манифеста) и
#: **контейнерный** (тот же файл, но через монтирование `-v $SHARED:/workspace/shared`).
#: Путать их нельзя: проба исполняется внутри контейнера, и хозяйский путь там не
#: существует — первый запуск S3k на этом и отказал (NOT-VERIFIED по всем наборам).
EVAL_SETS: dict[str, dict[str, str]] = {
    "v1_general": {"host": f"{STAND_SHARED}/datasets/general_eval.txt"},
    "v1_domain": {"host": f"{STAND_SHARED}/datasets/domain_eval.txt"},
    "v2_general": {"host": f"{STAND_SHARED}/datasets/general_eval_v2.txt"},
    "v2_domain": {"host": f"{STAND_SHARED}/datasets/domain_eval_v2.txt"},
}
for _name, _set in EVAL_SETS.items():
    _set["ctr"] = _set["host"].replace(STAND_SHARED, CTR_SHARED, 1)
del _name, _set

#: Порог «язык разрушен» из ADR-022 п.3(б): потолок 2× реальной базы (v1: 23.9).
STAGE_CEILING_X = 2.0
#: Порог «flex — причина»: во сколько раз деградация руки A должна превышать
#: деградацию руки B, чтобы различие нельзя было списать на разброс.
FLEX_BLAME_X = 2.0

#: Файлы, доставляемые в каталог прогона (те же, что у пилота: цепочка и её
#: инструменты на стенде).
SHIPPED = (
    ("pilot_chain.sh", "tools/pilot_chain.sh"),
    ("check_resource_owner.sh", "tools/check_resource_owner.sh"),
    ("write_run_manifest.py", "tools/write_run_manifest.py"),
    ("smoke_mem_sampler.sh", "tools/smoke_mem_sampler.sh"),
    (PPL_PROBE_NAME, f"tools/{PPL_PROBE_NAME}"),
)


# ─── патч копии пайплайна ─────────────────────────────────────────────────────

#: Якоря патча: (имя, что искать, чем заменить). Оба — однострочные вставки,
#: поведение контура не меняется ни на одном шаге: без наших переменных окружения
#: патч добавляет только запись траектории (она и есть цель диагностики).
ANCHOR_LOSS = "            loss = out.loss\n"
PATCH_LOSS = "            loss = out.loss\n            _flexcheck_trace(step, loss.item(), lr, args)\n"

#: Якорь покрывает **весь** блок сохранения вместе с ретенцией. Это не
#: избыточность: ретенция — строки того же уровня отступа, что и тело `elif`, и
#: если оставить её за якорем, новая ветка «проглотит» её как продолжение своего
#: тела. Ровно этот дефект и случился в первом прогоне S3k: `elif` встал перед
#: ретенцией, ретенция стала его телом и снесла `checkpoint_50.pt` вместо того,
#: чтобы остаться при `step % 200`. Тест §16 держит структуру AST-проверкой.
ANCHOR_CKPT = (
    '            if step % 200 == 0 and step > 0:\n'
    '                save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/f"checkpoint_{step}.pt")\n'
    '                # v6: храним только 2 последних — 7B full-CPT иначе пишет ~5.7 ТБ (192 ckpt × 30GB)\n'
    '                _cks = sorted(Path(args.ckpt_dir).glob("checkpoint_[0-9]*.pt"),\n'
    '                              key=lambda p: p.stat().st_mtime)\n'
    '                for _old in _cks[:-2]:\n'
    '                    _old.unlink()\n')
#: Собирается конкатенацией, а не `%`-форматом: в тексте есть свои знаки `%`
#: (`step % 200`), и любая подстановка по формату спотыкается о них.
PATCH_CKPT = (
    '            _fc_every = int(os.environ.get("FLEX_CHECK_CKPT_EVERY", "'
    + str(CKPT_EVERY) + '") or 0)\n'
    + ANCHOR_CKPT
    + '            elif _fc_every > 0 and step > 0 and step % _fc_every == 0:\n'
    '                # FLEX-CHECK (S3k): номерной чекпойнт внутри окна, БЕЗ ретенции — иначе\n'
    '                # штатное «держим 2 последних» съест как раз те точки, ради которых прогон и идёт.\n'
    '                save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/f"checkpoint_{step}.pt",\n'
    '                                       keep_last=0)\n')

#: Хелпер траектории. Пишется в конец модуля: `step`, `loss` и `lr` берутся из
#: цикла CPT, таймстемп — свой (по нему считается время шага без разбора лога).
TRACE_HELPER = '''

# ── FLEX-CHECK (S3k) ─────────────────────────────────────────────────────────
# Вставлено раннером диагностики flex-маски (tools/run_flex_check.py). В рабочем
# пайплайне контура этого блока нет и не должно быть: он существует ровно затем,
# чтобы вердикт по ADR-022 п.4 опирался на **траекторию по шагам**, а не на сводку.
def _flexcheck_trace(step, loss_value, lr, args):
    """Построчная запись (step, loss, lr, t) в <log_dir>/loss_trace.jsonl.

    Файл пишется всегда при наличии `--log_dir` (копия пайплайна живёт только в
    каталоге диагностического прогона). `flush` на каждой строке обязателен:
    траектория читается стражем и раннером по живому прогону.
    """
    log_dir = getattr(args, "log_dir", None)
    if not log_dir:
        return
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(log_dir) / "loss_trace.jsonl", "a") as f:
            f.write(json.dumps({"step": int(step), "loss": float(loss_value),
                                "lr": float(lr), "t": time.time()}) + "\\n")
            f.flush()
    except Exception as e:  # трассировка не имеет права уронить стадию
        log.warning(f"FLEX-CHECK: траектория не записана на шаге {step}: {e}")
'''


#: Хелпер обязан оказаться **до** `if __name__ == "__main__": main()`: модуль
#: исполняется сверху вниз, и определённая после точки входа функция не существует
#: к моменту, когда цикл CPT её вызовет.
ANCHOR_MAIN = 'if __name__ == "__main__":\n    main()\n'


def patched_pipeline(src: str) -> tuple[str, dict]:
    """Копия пайплайна с тремя вставками. Якорь обязан встречаться ровно один раз.

    Проверка якоря — не формальность: если в контуре тронут цикл CPT, патч должен
    упасть с объяснением, а не «примениться не туда» и выдать траекторию чужого шага.
    """
    for name, anchor in (("loss", ANCHOR_LOSS), ("ckpt", ANCHOR_CKPT), ("main", ANCHOR_MAIN)):
        n = src.count(anchor)
        if n != 1:
            raise SystemExit(f"патч не применён: якорь «{name}» встречается {n} раз "
                             f"(ожидался ровно 1) — контурный пайплайн изменился, "
                             f"сверь ANCHOR_* в tools/run_flex_check.py")
    out = src.replace(ANCHOR_LOSS, PATCH_LOSS, 1).replace(ANCHOR_CKPT, PATCH_CKPT, 1)
    out = out.replace(ANCHOR_MAIN, TRACE_HELPER.strip("\n") + "\n\n" + ANCHOR_MAIN, 1)
    return out, {
        "applied": ["loss_trace (per-step)", f"numbered_ckpt_every={CKPT_EVERY} (keep_last=0)"],
        "anchors": {"loss": ANCHOR_LOSS.strip()[:60], "ckpt": ANCHOR_CKPT.splitlines()[0].strip(),
                    "main": ANCHOR_MAIN.splitlines()[0]},
        "base_sha256": _sha256_text(src),
        "patched_sha256": _sha256_text(out),
        "size_bytes": {"base": len(src.encode()), "patched": len(out.encode())},
    }


def _sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─── каталог прогона ──────────────────────────────────────────────────────────

def run_id_for(arm: str, ts: str) -> str:
    return f"{RUN_PREFIX}-{arm}-{ts}"


def ppl_run_id(ts: str) -> str:
    return f"{RUN_PREFIX}-{PPL_RUN_SUFFIX}-{ts}"


def stages_tsv(arm: str, ts: str) -> str:
    """Одна стадия — CPT 200 шагов. Формат тот же, что читает `pilot_chain.sh`."""
    name = run_id_for(arm, ts)
    return "\t".join(["cpt", "cpt", "cpt", "checkpoints/checkpoint_final.pt",
                      str(CPT_STEPS), str(CPT_BATCH), "-", name]) + "\n"


def build_run_dir(arm: str, ts: str, local_runs: Path) -> dict:
    """Собрать каталог прогона в кейсе: копия пайплайна, цепочка, инструменты, stages.tsv."""
    run_dir = local_runs / run_id_for(arm, ts)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "checkpoints").mkdir(parents=True, exist_ok=True)

    src = PIPELINE_CASE.read_text(encoding="utf-8")
    patched, patch_record = patched_pipeline(src)
    (run_dir / PIPELINE_COPY_NAME).write_text(patched, encoding="utf-8")
    (run_dir / "pipeline_patch.json").write_text(
        json.dumps(patch_record, ensure_ascii=False, indent=2), encoding="utf-8")

    for local_name, case_rel in SHIPPED:
        content = (CASE_ROOT / case_rel).read_bytes()
        (run_dir / local_name).write_bytes(content)
    os.chmod(run_dir / "pilot_chain.sh", 0o755)
    os.chmod(run_dir / "check_resource_owner.sh", 0o755)
    os.chmod(run_dir / "smoke_mem_sampler.sh", 0o755)
    (run_dir / "stages.tsv").write_text(stages_tsv(arm, ts), encoding="utf-8")

    params = {
        "diagnostic": "S3k — flex-маска против штатного внимания (ADR-022 п.1)",
        "arm": arm,
        "label": ARMS[arm]["label"],
        "attn": ARMS[arm]["attn"],
        "attn_desc": ARMS[arm]["desc"],
        "model": MODEL, "seed": SEED, "max_len": MAX_LEN, "max_samples": MAX_SAMPLES,
        "cpt_steps": CPT_STEPS, "cpt_batch": CPT_BATCH, "peak_lr_scale": PEAK_LR_SCALE,
        "ckpt_every": CKPT_EVERY,
        "cpt_data": f"{STAND_SHARED}/datasets/cpt_corpus_v12r.txt",
        "tok_cache": f"{STAND_SHARED}/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy",
        "image": None,  # заполняется на доставке
        "pipeline_patch": patch_record,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "flex_check_params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"run_dir": run_dir, "params": params, "patch": patch_record}


def build_ppl_dir(ts: str, local_runs: Path) -> Path:
    """Каталог замера PPL: не тренировочный прогон, но прогон **измерения** — AD-2
    требует манифест и на него (стадия `ppl`), поэтому он отдельный, а не подкаталог
    чужой руки: тогда видно, что мерилось и чем."""
    ppl_dir = local_runs / ppl_run_id(ts)
    (ppl_dir / "logs").mkdir(parents=True, exist_ok=True)
    (ppl_dir / PPL_PROBE_NAME).write_bytes((CASE_ROOT / "tools" / PPL_PROBE_NAME).read_bytes())
    (ppl_dir / "sets.json").write_text(
        json.dumps(EVAL_SETS, ensure_ascii=False, indent=2), encoding="utf-8")
    return ppl_dir


def ship(host: str, run_dir: Path, stand_dir: str) -> dict:
    """Доставка каталога прогона на стенд со сверкой sha256 (что исполнялось — то и лежит)."""
    rc, _, err = S.ssh(host, f"mkdir -p {stand_dir}/logs {stand_dir}/checkpoints", timeout=60)
    if rc != 0:
        return {"ok": False, "detail": f"каталог не создан: {err.strip()}"}
    shipped, mismatched = {}, []
    for path in sorted(run_dir.iterdir()):
        if path.is_dir():
            continue
        data = path.read_bytes()
        rc, err = S.ssh_put(host, f"{stand_dir}/{path.name}", data)
        if rc != 0:
            return {"ok": False, "detail": f"{path.name}: доставка не удалась: {err.strip()}"}
        rc2, out2, _ = S.ssh(host, f"sha256sum {stand_dir}/{path.name} | cut -d' ' -f1")
        local = S.sha256_bytes(data)
        if rc2 != 0 or out2.strip() != local:
            mismatched.append(path.name)
        shipped[path.name] = local
    return {"ok": not mismatched, "sha256": shipped, "mismatched": mismatched,
            "detail": ("все файлы доставлены, sha256 совпал" if not mismatched
                       else f"расхождение sha256: {', '.join(mismatched)}")}


def chain_command(arm: str, ts: str, stand_dir: str, args) -> str:
    """Строка запуска цепочки — одна и та же для ручного повтора и для tmux."""
    name = run_id_for(arm, ts)
    return " ".join([
        f"bash {stand_dir}/pilot_chain.sh",
        f"--run-dir {stand_dir}",
        f"--ctr-run-dir {CTR_EXPERIMENTS}/{name}",
        f"--stages-file {stand_dir}/stages.tsv",
        f"--exp-base {name}",
        f"--ctr-prefix laguna-flexcheck-{ts}-{arm}",
        f"--shared {STAND_SHARED}",
        f"--experiments {STAND_EXPERIMENTS}",
        f"--pipeline {stand_dir}/{PIPELINE_COPY_NAME}",
        f"--pipeline-ctr {CTR_EXPERIMENTS}/{name}/{PIPELINE_COPY_NAME}",
        f"--guard {stand_dir}/check_resource_owner.sh",
        f"--safe-start {SAFE_START}",
        f"--sampler {stand_dir}/smoke_mem_sampler.sh",
        f"--storm-gap {STORM_GAP}",
        f"--manifest-tool {stand_dir}/write_run_manifest.py",
        f"--image {args.image}",
        f"--glm-env {GLM_ENV}",
        f"--nvrm-log {NVRN_LOG}",
        f"--runner-sha12 {_runner_sha12()}",
        f"--model {MODEL}",
        f"--seed {SEED}",
        f"--max-len {MAX_LEN}",
        f"--max-samples {MAX_SAMPLES}",
        f"--peak-lr-scale {PEAK_LR_SCALE}",
        f"--attn {ARMS[arm]['attn']}",
        f"--cpt-gen-eval-every 50",
        f"--mem-cap 100g",
        # стоп-условие «стадия молчит»: на 200-шаговом окне соседние строки лога
        # разнесены на ~2 минуты, поэтому 120 минут молчания — это уже wedge,
        # просто более поздний, чем нужно для десятиминутного прогона.
        f"--stall-minutes {args.stall_minutes}",
    ])


def launch_tmux(host: str, session: str, cmd: str, log: str) -> dict:
    tmux_cmd = (f"tmux kill-session -t {session} 2>/dev/null; "
                f"tmux new-session -d -s {session} \"{cmd} >> {log} 2>&1\"")
    rc, out, err = S.ssh(host, tmux_cmd, timeout=120)
    if rc != 0:
        return {"ok": False, "detail": f"tmux не поднял сессию: {err.strip() or out.strip()}"}
    return {"ok": True, "tmux_command": tmux_cmd, "log": log}


def wait_chain(host: str, stand_dir: str, session: str, timeout_sec: int,
               poll: int = 20) -> dict:
    """Ждать завершения цепочки. Условие выхода — файл `var/chain.status` (его пишет
    сама цепочка на каждом исходе) ИЛИ исчезновение tmux-сессии. Ждём именно по
    файлу: сессия может закрыться раньше, чем цепочка запишет статус."""
    t0 = time.time()
    last = ""
    dead_since: float | None = None
    while time.time() - t0 < timeout_sec:
        rc, status, _ = S.ssh(host, f"cat {stand_dir}/var/chain.status 2>/dev/null || true")
        rc2, sessions, _ = S.ssh(host, "tmux ls 2>/dev/null || true")
        alive = any(line.startswith(f"{session}:") for line in sessions.splitlines())
        last = status.strip()
        if last and last not in ("running",):
            return {"ok": True, "chain_status": last, "waited_sec": round(time.time() - t0),
                    "session_alive": alive}
        if not alive:
            # Сессия исчезла, а статус так и остался `running` — цепочку убили извне
            # (или tmux не пережил запуск). Ждать полный таймаут в этом случае значит
            # не отличить «долго считает» от «умерло»: ждём минуту и фиксируем отказ.
            if dead_since is None:
                dead_since = time.time()
            elif time.time() - dead_since > 60:
                return {"ok": False, "chain_status": last or None,
                        "detail": (f"tmux-сессия исчезла, `var/chain.status`={last or 'нет'} — "
                                   "цепочка оборвалась (см. logs/tmux.log и logs/chain.log)"),
                        "waited_sec": round(time.time() - t0)}
        else:
            dead_since = None
        time.sleep(poll)
    return {"ok": False, "chain_status": last or None, "timeout": True,
            "detail": f"таймаут ожидания {timeout_sec} с (status={last or 'нет'})",
            "waited_sec": round(time.time() - t0)}


def fetch(host: str, stand_dir: str, run_dir: Path) -> dict:
    """Забрать в кейс лог цепочки, статусы стадий, stand-side манифест и артефакты."""
    got, missing = {}, []
    wanted = (("logs/chain.log", "logs/chain.log"),
              ("run_manifest.json", "stand_run_manifest.json"),
              ("var/chain.status", "chain.status"),
              ("logs/cpt.log", "logs/cpt.log"),
              ("logs/loss_trace.jsonl", "logs/loss_trace.jsonl"),
              ("general_eval_history.jsonl", "general_eval_history.jsonl"),
              ("mem_cpt.jsonl", "mem_cpt.jsonl"),
              ("pipeline_patch.json", "pipeline_patch.json"))
    for remote, local in wanted:
        rc, data, _ = S.ssh_get(host, f"{stand_dir}/{remote}", timeout=180)
        if rc == 0 and data:
            path = run_dir / local
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            got[local] = len(data)
        else:
            missing.append(remote)
    return {"got": got, "missing": missing}


def stand_checkpoints(host: str, stand_dir: str) -> dict:
    """Перечень чекпойнтов на стенде с sha256 — без копирования весов (AD-4)."""
    rc, out, _ = S.ssh(
        host, f"cd {stand_dir}/checkpoints 2>/dev/null && "
              f"for f in *.pt; do [ -f \"$f\" ] && echo \"$f $(stat -c%s \"$f\") $(sha256sum \"$f\" | cut -d' ' -f1)\"; done")
    ckpts = {}
    if rc == 0:
        for line in out.strip().splitlines():
            parts = line.split()
            if len(parts) == 3:
                ckpts[parts[0]] = {"bytes": int(parts[1]), "sha256": parts[2]}
    return ckpts


# ─── PPL-проба на стенде ──────────────────────────────────────────────────────

def probe_states(host: str, ts: str) -> list[str]:
    """Состояния пробы: база (два варианта) + чекпойнты обеих рук.

    `base` — модель с расширенным по AD-3 токенизатором и resize эмбеддингов, то
    есть **состояние до первого шага стадии** (его GEN-EVAL не измерял никогда —
    ADR-015 п.4). `base_hf` — вакуумная точка того же веса без расширения: по ней
    видно, сколько в PPL даёт сам resize, и она же сверяется с замером S3h 11.93.
    """
    states = ["base=base", "base_hf=base_hf"]
    for arm in ARM_ORDER:
        stand_dir = f"{STAND_EXPERIMENTS}/{run_id_for(arm, ts)}"
        for name in sorted(stand_checkpoints(host, stand_dir)):
            m = re.match(r"checkpoint_(?:final|(\d+))\.pt$", name)
            if not m:
                continue
            step = m.group(1) or str(CPT_STEPS)
            states.append(f"{arm}_{step}=ckpt:{CTR_EXPERIMENTS}/{run_id_for(arm, ts)}/checkpoints/{name}")
    return states


def ppl_docker_command(ts: str, states: list[str], image: str) -> str:
    """Контейнер пробы. Запуск через `safe_start.sh` — тот же путь, что у стадий
    (AD-5): проба грузит модель и считает логиты, это нагрузка на unified-память."""
    state_args = " ".join(f"--state {s}" for s in states)
    set_args = " ".join(f"--set {k}={v['ctr']}" for k, v in EVAL_SETS.items())
    inner = " ".join([
        f"python3 {CTR_EXPERIMENTS}/{ppl_run_id(ts)}/{PPL_PROBE_NAME}",
        f"--pipeline {CTR_EXPERIMENTS}/{run_id_for('a', ts)}/{PIPELINE_COPY_NAME}",
        f"--model {MODEL}",
        state_args,
        set_args,
        f"--out {CTR_EXPERIMENTS}/{ppl_run_id(ts)}/ppl.json",
        "--json",
    ])
    ctr = f"laguna-flexcheck-ppl-{ts}"
    docker = (f"docker run --rm --name {ctr} --gpus all --ipc=host --pid=host "
              f"--memory=100g --memory-swap=100g --security-opt seccomp=unconfined "
              f"--cap-add SYS_PTRACE --ulimit memlock=-1 --ulimit stack=67108864 "
              f"-e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -e HF_HUB_OFFLINE=1 "
              f"-e TRANSFORMERS_OFFLINE=1 -e PYTHONPATH={CTR_SHARED}/wt_stubs "
              f"-v {STAND_EXPERIMENTS}:{CTR_EXPERIMENTS} -v {STAND_SHARED}:{CTR_SHARED} "
              f"-v /home/user/.cache/huggingface:/root/.cache/huggingface -w /workspace "
              f"{image} {inner}")
    return f"bash {SAFE_START} -d 60 -i 5 -- {docker}"


def wait_probe(host: str, session: str, ppl_stand: str, timeout_sec: int,
               poll: int = 15) -> dict:
    """Ожидание пробы: у неё нет `var/chain.status` — признак конца это файл отчёта.

    Ждать по файлу, а не только по tmux-сессии: сессия закрывается в тот же миг,
    когда контейнер вернул код, и «сессии нет» неотличимо от «не стартовала».
    """
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        rc, out, _ = S.ssh(host, f"test -f {ppl_stand}/ppl.json && echo yes || echo no")
        if out.strip() == "yes":
            return {"ok": True, "waited_sec": round(time.time() - t0), "report_written": True}
        rc2, sessions, _ = S.ssh(host, "tmux ls 2>/dev/null || true")
        alive = any(line.startswith(f"{session}:") for line in sessions.splitlines())
        if not alive:
            return {"ok": False, "report_written": False, "waited_sec": round(time.time() - t0),
                    "detail": "сессия пробы завершилась, файла отчёта нет — см. logs/ppl.log"}
        time.sleep(poll)
    return {"ok": False, "timeout": True, "waited_sec": round(time.time() - t0)}


def run_ppl(host: str, ts: str, args) -> dict:
    """Проба PPL в контейнере стенда: чекпойнты обеих рук + база, наборы v1 и v2."""
    states = probe_states(host, ts)
    if len(states) <= 2:
        return {"ok": False, "states": states,
                "detail": "чекпойнтов диагностики на стенде нет — проба не имеет входов"}
    ppl_stand = f"{STAND_EXPERIMENTS}/{ppl_run_id(ts)}"
    log = f"{ppl_stand}/logs/ppl.log"
    session = f"flexcheck-ppl-{ts}"
    # Снос прежнего отчёта обязателен: `wait_probe` ждёт **появления файла**, и
    # отчёт, оставшийся от прошлой (отказавшей) попытки, был бы принят за результат
    # новой — то есть evidence описывал бы не тот запуск, который только что прошёл.
    S.ssh(host, f"rm -f {ppl_stand}/ppl.json", timeout=30)
    launched = launch_tmux(host, session, ppl_docker_command(ts, states, args.image), log)
    if not launched.get("ok"):
        return {"ok": False, "states": states, **launched}
    waited = wait_probe(host, session, ppl_stand, timeout_sec=args.ppl_timeout)
    return {"ok": waited.get("ok", False), "states": states, "log": log, "wait": waited}


# ─── предусловия ──────────────────────────────────────────────────────────────

def preflight(host: str, args) -> dict:
    """Предусловия: стенд отвечает, страж AD-9 пропускает, память, платформа, диск, данные."""
    out: dict = {}
    rc, stdout, err = S.ssh(host, "hostname; free -g | sed -n 2p", timeout=30)
    if rc != 0:
        return {"ok": False, "detail": f"стенд не отвечает: {err.strip()}"}
    out["stand"] = stdout.strip().splitlines()

    # Страж AD-9 зовётся ровно тем же способом, что и перед стадией (без
    # `--unreachable-not-verified`): предусловие прогона и предусловие стадии
    # обязаны совпадать, иначе «раннер пропустил, цепочка отказала» — норма.
    guard_local = subprocess.run(
        ["bash", str(CASE_ROOT / "tools" / "check_resource_owner.sh"), "--stage", "cpt"],
        capture_output=True, text=True, check=False)
    out["owner_guard"] = {"exit": guard_local.returncode,
                          "tail": guard_local.stdout.strip().splitlines()[-3:]}
    if guard_local.returncode != 0:
        return {"ok": False, "detail": "страж AD-9 отказал: стадия не стартует", **out}

    rc, mem, _ = S.ssh(host, "cat /proc/meminfo", timeout=30)
    out["mem_available_gb"] = S.free_gb(S.parse_meminfo(mem)) if rc == 0 else None

    rc, plat, _ = S.ssh(host, "docker ps --format '{{.Names}}' | sort", timeout=30)
    running = plat.split() if rc == 0 else []
    out["platform_alive"] = [n for n in running if n.startswith("llm-platform-")]
    out["other_containers"] = [n for n in running if not n.startswith("llm-platform-")]

    rc, disk, _ = S.ssh(host, "df -BG --output=avail /home/user | tail -1", timeout=30)
    out["disk_avail_gb"] = int(re.sub(r"\D", "", disk.strip()) or 0) if rc == 0 else None

    rc, data, _ = S.ssh(
        host, "ls -la "
              f"{STAND_SHARED}/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy "
              f"{STAND_SHARED}/datasets/tok/cpt_corpus_v12r_8192_qwen25_pos.npy "
              f"{STAND_SHARED}/patch_flex_attention.py 2>&1 | tail -3", timeout=30)
    out["inputs"] = data.strip().splitlines()

    rc, cp, _ = S.ssh(host, f"ls {STAND_EXPERIMENTS}/pilot-compact-s42-20260914-1626/checkpoints/ "
                            f"{STAND_SHARED}/checkpoints/ 2>/dev/null | head -20", timeout=30)
    out["untouched_checkpoints_present"] = cp.strip().splitlines()[:12]

    ok = (out["mem_available_gb"] is None or out["mem_available_gb"] >= 35.0) and \
         (out["disk_avail_gb"] is None or out["disk_avail_gb"] >= 33)
    return {"ok": ok, **out}


#: Пути, которые задание запрещает трогать. Снимок снимается **до** и **после**
#: прогона одной и той же командой: «мы не трогали» — утверждение, которое обязано
#: быть проверяемым, а не подразумеваемым.
UNTOUCHABLE = (
    "laguna_pipeline_v8.py",
    "pilot-compact-s42-20260914-1626/checkpoints",
    "v12_qwen25-3b_s42",
)


def verify_untouched(host: str) -> dict:
    """Снимок запрещённых к изменению артефактов: хеши и дерево с размерами/mtime.

    Хешируются только файлы, которые заведомо небольшие или немногочисленные
    (пайплайн ~115 КБ, пять чекпойнтов пилота по ~3 ГБ). Дерево `v12_qwen25-3b_s42`
    (92 ГБ) хешируется **по листингу** «путь+размер+mtime» — этого достаточно,
    чтобы поймать запись, и не требует читать 92 ГБ (AD-4: веса не копируются и не
    перечитываются без нужды).
    """
    out: dict = {"host": host, "at": datetime.now(timezone.utc).isoformat()}
    sh = f"{STAND_SHARED}/laguna_pipeline_v8.py"
    rc, val, _ = S.ssh(host, f"sha256sum {sh} 2>/dev/null | cut -d' ' -f1")
    out["pipeline_v8_sha256"] = val.strip() or None

    # `LC_ALL=C` обязателен: `sort` зависит от локали, и `tokenizer.json` против
    # `tokenizer_config.json` встают в разном порядке под UTF-8 и под C. Тогда
    # снимок «до» и снимок «после» расходятся порядком строк и выглядят как
    # изменение файлов — ровно этот ложный сигнал и поймал первый снимок S3k.
    rc, listing, _ = S.ssh(
        host, "cd /home/user/experiments/pilot-compact-s42-20260914-1626/checkpoints 2>/dev/null && "
              "sha256sum *.pt 2>/dev/null | LC_ALL=C sort")
    out["pilot_checkpoints"] = dict(
        (line.split()[1], line.split()[0]) for line in listing.strip().splitlines()
        if len(line.split()) == 2)

    rc, tree, _ = S.ssh(
        host, "find /home/user/experiments/v12_qwen25-3b_s42 -type f "
              "-printf '%p\\t%s\\t%T@\\n' 2>/dev/null | LC_ALL=C sort")
    files = [l for l in tree.strip().splitlines() if l.strip()]
    out["v12_3b_s42_files"] = len(files)
    out["v12_3b_s42_tree_sha256"] = _sha256_text("\n".join(files))

    rc, names, _ = S.ssh(host, "docker ps --format '{{.Names}}' | sort")
    running = [n for n in names.split() if n.strip()]
    out["platform_containers"] = [n for n in running if n.startswith("llm-platform-")]
    out["diagnostic_containers_running"] = [
        n for n in running if n.startswith(f"laguna-flexcheck")]
    return out


def compare_untouched(before: dict, after: dict) -> dict:
    """Сравнение снимков: расхождение по любому защищённому пути — нарушение."""
    gaps = []
    for key in ("pipeline_v8_sha256", "pilot_checkpoints", "v12_3b_s42_tree_sha256",
                "v12_3b_s42_files"):
        if before.get(key) != after.get(key):
            gaps.append({"field": key, "before": before.get(key), "after": after.get(key)})
    return {"paths": list(UNTOUCHABLE), "identical": not gaps, "differences": gaps,
            "platform_alive_before": before.get("platform_containers"),
            "platform_alive_after": after.get("platform_containers"),
            "diagnostic_containers_running_after": after.get("diagnostic_containers_running")}


def stop_containers(host: str, ts: str) -> dict:
    """Закрыть контейнеры диагностики (стенд остаётся свободным). Чужие не трогаются:
    имена отбираются по префиксу, который раннер сам и создал."""
    rc, out, _ = S.ssh(host, "docker ps --format '{{.Names}}'", timeout=60)
    # Два шаблона имён: контейнер стадии (`laguna-flexcheck-<ts>-<arm>-cpt`) и
    # контейнер пробы (`laguna-flexcheck-ppl-<ts>`) — у пробы метка идёт до `ppl`.
    names = [n for n in out.split()
             if n.startswith(f"laguna-flexcheck-{ts}") or n == f"laguna-flexcheck-ppl-{ts}"]
    if not names:
        return {"ok": True, "stopped": [], "detail": "контейнеров диагностики не запущено"}
    rc2, _, err2 = S.ssh(host, "docker stop " + " ".join(names), timeout=300)
    if rc2 != 0:
        return {"ok": False, "stopped": [], "detail": f"docker stop rc={rc2}: {err2.strip()}"}
    return {"ok": True, "stopped": names, "detail": f"остановлены: {', '.join(names)}"}


# ─── разбор результатов ───────────────────────────────────────────────────────

def read_loss_trace(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "step" in d and "loss" in d:
            rows.append(d)
    rows.sort(key=lambda d: d["step"])
    return rows


def thirds(values: list[float]) -> dict:
    """Тренд по третям — то, чем ADR-022 п.3(а) заменяет «loss убывает по сводке»:
    сравниваются последняя и первая треть **траектории**, с разбросом внутри третей."""
    n = len(values)
    if n < 6:
        return {"available": False, "why": f"точек {n} — третей не набирается"}
    k = n // 3
    first, last = values[:k], values[-k:]
    m1, m3 = statistics.fmean(first), statistics.fmean(last)
    s1 = statistics.pstdev(first) if len(first) > 1 else 0.0
    s3 = statistics.pstdev(last) if len(last) > 1 else 0.0
    se = ((s1 ** 2 / len(first)) + (s3 ** 2 / len(last))) ** 0.5
    delta = m3 - m1
    return {
        "available": True, "n": n, "third_size": k,
        "first_third_mean": round(m1, 4), "last_third_mean": round(m3, 4),
        "first_third_std": round(s1, 4), "last_third_std": round(s3, 4),
        "delta": round(delta, 4), "delta_se": round(se, 4),
        "direction": "убывает" if delta < -2 * se else ("растёт" if delta > 2 * se else "плоско"),
    }


def parse_cpt_log(path: Path) -> dict:
    """Траектория GEN-EVAL, шаги с временем, признаки режима внимания и предупреждения."""
    res = {"gen_eval": [], "steps_logged": [], "flex_lines": [], "mask_warnings": [],
           "warnings_all": [], "errors": [], "peak_lr": None}
    if not path.is_file():
        return res
    txt = path.read_text(encoding="utf-8", errors="replace")
    for line in txt.splitlines():
        m = re.search(r"GEN-EVAL step (\d+): ppl_general=([\d.]+).*?ppl_domain=([\d.]+)", line)
        if m:
            res["gen_eval"].append({"step": int(m.group(1)),
                                    "ppl_general": float(m.group(2)),
                                    "ppl_domain": float(m.group(3))})
        m = re.search(r"(\d\d:\d\d:\d\d) \[INFO\] CPT step (\d+)/\d+ \| loss=([\d.]+) \| lr=([\d.e+-]+) \| tok/s=(\d+)", line)
        if m:
            res["steps_logged"].append({"t": m.group(1), "step": int(m.group(2)),
                                        "loss": float(m.group(3)), "lr": float(m.group(4)),
                                        "tok_s": int(m.group(5))})
        if "ATTN:" in line or "flex" in line.lower():
            res["flex_lines"].append(line.strip()[:200])
        low = line.lower()
        if "warning" in low or "[warn" in low or "предупрежд" in low:
            res["warnings_all"].append(line.strip()[:240])
            if "mask" in low or "маск" in low or "flex" in low:
                res["mask_warnings"].append(line.strip()[:240])
        if re.search(r"\b(Error|Traceback|CUDA out of memory|NVRM|Xid)\b", line):
            res["errors"].append(line.strip()[:240])
        m = re.search(r"CPT: .*peak_lr=([\d.e+-]+)", line)
        if m:
            res["peak_lr"] = float(m.group(1))
    return res


def parse_mem(path: Path) -> dict:
    """Пик памяти по сэмплеру: сколько unified-памяти съел прогон (S2 мерил так же)."""
    if not path.is_file():
        return {"available": False}
    avail, total = [], None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "mem_available_kb" not in d:
            continue
        avail.append(d["mem_available_kb"] / 1048576)
        total = d["mem_total_kb"] / 1048576
    if not avail:
        return {"available": False}
    return {"available": True, "samples": len(avail),
            "mem_total_gb": round(total, 1),
            "mem_available_min_gb": round(min(avail), 1),
            "mem_available_max_gb": round(max(avail), 1),
            "peak_used_gb": round(total - min(avail), 1)}


def step_seconds(trace: list[dict]) -> dict:
    """Время шага по меткам траектории (свой таймстемп патча, без разбора лога)."""
    ts = [r["t"] for r in trace if "t" in r]
    if len(ts) < 3:
        return {"available": False}
    d = [b - a for a, b in zip(ts, ts[1:])]
    d = [x for x in d if 0 < x < 600]
    if not d:
        return {"available": False}
    return {"available": True, "median_sec": round(statistics.median(d), 3),
            "mean_sec": round(statistics.fmean(d), 3),
            "min_sec": round(min(d), 3), "max_sec": round(max(d), 3), "n": len(d)}


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Коэффициент корреляции Пирсона — «одинаковой ли формы» кривые, независимо
    от постоянного сдвига между ними."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def paired_delta(ta: list[dict], tb: list[dict]) -> dict:
    """Пошаговое сравнение рук.

    Руки идут одним сидом и одним потоком данных, поэтому сравнивать надо
    **попарно по номеру шага**, а не по средним: средние по двумстам шумным точкам
    теряют именно то, что ищем.

    Но «попарно» не значит «совпадает»: режим внимания меняет **уровень** лосса на
    том же самом батче — блочная маска из `position_ids` запрещает внимание между
    документами внутри пакованного чанка, а штатный SDPA его разрешает, и тот же
    шаг даёт другой (обычно меньший) лосс. Поэтому здесь три величины, а не одна:
    средний сдвиг (`mean_loss_a_minus_b`), его значимость (`se`) и **корреляция** —
    форма кривых. Диагностический вопрос «искажает ли flex обучение» — это вопрос о
    форме и о деградации языка, а не о постоянном сдвиге уровня.
    """
    a = {r["step"]: r["loss"] for r in ta}
    b = {r["step"]: r["loss"] for r in tb}
    common = sorted(set(a) & set(b))
    if len(common) < 6:
        return {"available": False, "common_steps": len(common)}
    xa = [a[s] for s in common]
    xb = [b[s] for s in common]
    diff = [p - q for p, q in zip(xa, xb)]
    mean = statistics.fmean(diff)
    sd = statistics.pstdev(diff) if len(diff) > 1 else 0.0
    se = sd / (len(diff) ** 0.5)
    r = pearson(xa, xb)
    return {"available": True, "common_steps": len(common),
            "mean_loss_a_minus_b": round(mean, 4), "std": round(sd, 4),
            "se": round(se, 4),
            "abs_max": round(max(abs(x) for x in diff), 4),
            "correlation": round(r, 4) if r is not None else None,
            "first_step": {"a": round(xa[0], 4), "b": round(xb[0], 4)},
            "within_noise_2se": abs(mean) <= 2 * se if se > 0 else abs(mean) < 1e-9,
            "shape_agrees": (r is not None and r >= 0.5)}


def _non_interference(ts: str) -> dict:
    """Доказательство «запрещённое не тронуто»: два снимка, снятых одним инструментом.

    Снимки лежат в каталоге руки A (`non_interference_before.json` /
    `_after.json`). Если хотя бы одного нет — не «всё хорошо», а «не проверено»:
    утверждение о ненарушении обязано быть проверяемым, иначе оно декларация.
    """
    base = CASE_ROOT / "runs" / run_id_for("a", ts)
    before = _read_json(base / "non_interference_before.json")
    after = _read_json(base / "non_interference_after.json")
    if not before or not after:
        return {"checked": False,
                "why": ("нет обоих снимков защиты от изменений — снимаются "
                        "`--verify-untouched --out-snapshot ...` до и после прогона"),
                "paths": list(UNTOUCHABLE)}
    return {"checked": True, **compare_untouched(before, after),
            "before": "non_interference_before.json", "after": "non_interference_after.json"}


def _patch_drift(ts: str, runs: dict[str, dict]) -> list[dict]:
    """Расхождение «патч прогона» против «патча нынешнего инструмента».

    Запись о патче в каталоге прогона — это то, что **исполнялось**; если
    инструмент после прогона поправлен, расхождение обязано быть видно в
    evidence, а не растворяться в «мы потом починили». Молчание здесь означало бы,
    что отчёт описывает не тот код.
    """
    try:
        _, now = patched_pipeline(PIPELINE_CASE.read_text(encoding="utf-8"))
        now_sha = now["patched_sha256"]
    except SystemExit as e:
        return [{"kind": "patch_unavailable", "detail": str(e)}]
    out = []
    for arm in ARM_ORDER:
        params = (runs.get(arm) or {}).get("params") or _load_params(
            CASE_ROOT / "runs" / run_id_for(arm, ts))
        rec = (params or {}).get("pipeline_patch") or {}
        was = rec.get("patched_sha256")
        if was and was != now_sha:
            out.append({
                "kind": "patch_drift",
                "arm": arm,
                "ran_patched_sha256": was,
                "tool_patched_sha256": now_sha,
                "detail": ("копия пайплайна в прогоне собрана прежней редакцией патча; "
                           "в первой редакции ветка `elif` стояла перед ретенцией чекпойнтов "
                           "и ретенция стала её телом — на шаге 150 удалялся `checkpoint_50.pt`. "
                           "Следствие: точка шага 50 у чекпойнтов отсутствует у ОБЕИХ рук "
                           "(симметрично, сравнение рук не затронуто); PPL v1 на шаге 50 "
                           "сохранена в GEN-EVAL лога. Инструмент исправлен, структура "
                           "закреплена AST-проверкой в tools/tests/run_tool_tests.sh §16."),
            })
    return out


def arm_summary(arm: str, run_dir: Path) -> dict:
    label = ARMS[arm]["label"]
    trace = read_loss_trace(run_dir / "logs" / "loss_trace.jsonl")
    losses = [r["loss"] for r in trace]
    log_info = parse_cpt_log(run_dir / "logs" / "cpt.log")
    mem = parse_mem(run_dir / "mem_cpt.jsonl")
    status_file = run_dir / "chain.status"
    return {
        "label": label,
        "run_dir": str(run_dir.relative_to(CASE_ROOT)) if run_dir.is_relative_to(CASE_ROOT) else str(run_dir),
        "attn": ARMS[arm]["attn"],
        "attn_desc": ARMS[arm]["desc"],
        "attn_mode_in_log": (
            "flex_docmask" if any("ATTN: flex_docmask" in l for l in log_info["flex_lines"])
            else ("штатный SDPA (строка ATTN: flex_docmask отсутствует)" if log_info["flex_lines"]
                  else "строк о внимании нет")),
        "loss_trace": [round(x, 6) for x in losses],
        "loss_trace_steps": [r["step"] for r in trace],
        "loss_points": len(losses),
        "loss_min": round(min(losses), 4) if losses else None,
        "loss_min_step": trace[losses.index(min(losses))]["step"] if losses else None,
        "loss_final": round(losses[-1], 4) if losses else None,
        "loss_first": round(losses[0], 4) if losses else None,
        "loss_mean": round(statistics.fmean(losses), 4) if losses else None,
        "trend": thirds(losses),
        "gen_eval_in_run": log_info["gen_eval"],
        "peak_lr": log_info["peak_lr"],
        "step_sec": step_seconds(trace),
        "step_log_lines": log_info["steps_logged"],
        "peak_mem": mem,
        "mask_warnings": log_info["mask_warnings"],
        "warnings_all": log_info["warnings_all"][:40],
        "warnings_count": len(log_info["warnings_all"]),
        "errors": log_info["errors"],
        "chain_status": status_file.read_text().strip() if status_file.is_file() else None,
        "checkpoints": {},
    }


def verdict_of(trace_a: list[dict], trace_b: list[dict], ppl: dict) -> dict:
    """Вердикт по ADR-022 п.2 — с числами, по которым его можно перечесть.

    Три вопроса дельты, каждый со своим основанием:

    1. **Совпадают ли траектории** — попарно по номеру шага: **сдвиг уровня**,
       его значимость и **корреляция формы**. Совпадение уровня здесь не ожидается
       и не требуется: блочная маска запрещает внимание между документами внутри
       пакованного чанка, штатный SDPA его разрешает, поэтому один и тот же батч
       даёт разные лоссы. Диагностический вопрос — общая ли у кривых форма.
    2. **Различается ли деградация языка** — отношение PPL руки к базе на шаге 200
       по каждому набору; плюс потолок ADR-022 п.3(б) = 2× базы.
    3. **Есть ли основание винить flex** — во сколько раз деградация A превышает
       деградацию B. Меньше `FLEX_BLAME_X` (2×) — основания нет.

    Вердикт выносится по **деградации языка**; траектория лосса сама по себе
    вердикта не даёт (в пилоте лосс по третям убывал, а язык был разрушен — ровно
    поэтому ADR-022 п.4 и требует смотреть на траекторию, а не на сводку).
    """
    out: dict = {"criteria": {"stage_ceiling_x": STAGE_CEILING_X, "flex_blame_x": FLEX_BLAME_X}}
    paired = paired_delta(trace_a, trace_b)
    out["trajectories"] = paired
    trend_a = thirds([r["loss"] for r in trace_a]) if len(trace_a) >= 6 else {"available": False}
    trend_b = thirds([r["loss"] for r in trace_b]) if len(trace_b) >= 6 else {"available": False}
    out["trend"] = {"a_flex": trend_a, "b_stock": trend_b}
    out["trajectories_identical"] = bool(paired.get("shape_agrees") and paired.get("within_noise_2se"))
    out["trajectories_same_shape"] = bool(paired.get("shape_agrees"))

    def ppl_at(state_prefix: str, set_name: str):
        st = ppl.get("states", {}).get(state_prefix, {})
        s = st.get("sets", {}).get(set_name, {})
        return s.get("ppl_corpus")

    base_v1g = ppl_at("base", "v1_general")
    base_v2g = ppl_at("base", "v2_general")
    out["base"] = {"v1_general": base_v1g, "v2_general": base_v2g}
    ratios = {}
    for label in ("a_flex", "b_stock"):
        st = ppl.get("states", {}).get(f"{label.split('_')[0]}_{CPT_STEPS}", {})
        v1, v2 = st.get("sets", {}).get("v1_general", {}).get("ppl_corpus"), \
            st.get("sets", {}).get("v2_general", {}).get("ppl_corpus")
        ratios[label] = {
            "v1_general": v1, "v2_general": v2,
            "x_base_v1": round(v1 / base_v1g, 3) if (v1 and base_v1g) else None,
            "x_base_v2": round(v2 / base_v2g, 3) if (v2 and base_v2g) else None,
        }
    out["degradation"] = ratios
    a_x, b_x = ratios["a_flex"]["x_base_v1"], ratios["b_stock"]["x_base_v1"]
    flex_ratio = (a_x / b_x) if (a_x and b_x) else None
    out["flex_vs_stock_x"] = round(flex_ratio, 3) if flex_ratio else None

    no_blame = flex_ratio is None or flex_ratio < FLEX_BLAME_X
    ceiling_hit = [lbl for lbl, r in ratios.items()
                   if r["x_base_v1"] is not None and r["x_base_v1"] > STAGE_CEILING_X]

    r_corr = paired.get("correlation")
    shift = paired.get("mean_loss_a_minus_b")
    shape_phrase = (
        "формы траекторий совпадают (r={:.2f}) при сдвиге уровня {:+.3f}".format(r_corr, shift)
        if paired.get("shape_agrees") and shift is not None else
        ("формы траекторий различаются (r={})".format(r_corr if r_corr is not None else "н/д")))

    if flex_ratio is None:
        text = ("частичный вердикт: PPL чекпойнтов не снята — о причине судить нельзя; "
                f"основание — только траектория loss ({shape_phrase})")
        code = "ppl_missing"
    elif no_blame:
        text = (f"flex НЕ причина: {shape_phrase}, а деградация языка у руки A не превышает "
                f"деградацию руки B в {FLEX_BLAME_X:g}× (отношение {flex_ratio:.2f}×) — "
                f"переходим к калибровке микса (ADR-022 п.2, вторая ветка)")
        code = "flex_not_cause"
    else:
        text = (f"flex — причина: деградация языка у руки A превышает деградацию руки B "
                f"в {flex_ratio:.2f}× (порог {FLEX_BLAME_X:g}×); {shape_phrase} — CPT "
                f"перезапускается без flex при неизменных миксе и LR (ADR-022 п.2, первая ветка)")
        code = "flex_is_cause"
    out["code"] = code
    out["text"] = text
    out["ceiling_exceeded_by"] = ceiling_hit
    return out


# ─── evidence и манифесты ─────────────────────────────────────────────────────

def write_case_manifest(run_dir: Path, arm: str, ts: str, status: str, args) -> Path | None:
    """Манифест AD-2 прогона в кейсе — генератором кейса (одна реализация на всё)."""
    cmd = [sys.executable, str(CASE_ROOT / "tools" / "write_run_manifest.py"),
           "--run-dir", str(run_dir),
           "--dataset", str(CASE_ROOT / "datasets" / "tok" / "cpt_corpus_v12r_8192_qwen25.npy"),
           "--base-model", MODEL,
           "--pipeline", str(run_dir / PIPELINE_COPY_NAME),
           "--seed", str(SEED), "--image", args.image,
           "--stages", f"cpt={status}",
           "--run-version", f"tools/run_flex_check.py@{_runner_sha12()}",
           "--dataset-extra", f"pos={CASE_ROOT / 'datasets' / 'tok' / 'cpt_corpus_v12r_8192_qwen25_pos.npy'}",
           "--hyperparams", f"arm={arm}",
           "--hyperparams", f"attn={ARMS[arm]['attn']}",
           "--hyperparams", f"cpt_steps={CPT_STEPS}",
           "--hyperparams", f"cpt_batch={CPT_BATCH}",
           "--hyperparams", f"max_len={MAX_LEN}",
           "--hyperparams", f"max_samples={MAX_SAMPLES}",
           "--hyperparams", f"peak_lr_scale={PEAK_LR_SCALE}",
           "--hyperparams", f"ckpt_every={CKPT_EVERY}",
           "--hyperparams", "pipeline=копия контурного пайплайна с трассировкой loss и номерными чекпойнтами (pipeline_patch.json)",
           "--hyperparams-source",
           "фактические значения запуска диагностики (tools/run_flex_check.py → pilot_chain.sh)",
           "--relative-to", str(CASE_ROOT), "--force"]
    if status == "done":
        cmd.append("--complete")
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"ПРЕДУПРЕЖДЕНИЕ: манифест не записан (rc={p.returncode}): "
              f"{p.stderr.strip() or p.stdout.strip()}", file=sys.stderr)
        return None
    return run_dir / "run_manifest.json"


def write_ppl_manifest(ppl_dir: Path, ts: str, status: str, args,
                       states: list[str] | None = None) -> Path | None:
    cmd = [sys.executable, str(CASE_ROOT / "tools" / "write_run_manifest.py"),
           "--run-dir", str(ppl_dir),
           "--dataset", str(CASE_ROOT / "datasets" / "general_eval.txt"),
           "--base-model", MODEL,
           "--pipeline", str(ppl_dir / PPL_PROBE_NAME),
           "--seed", str(SEED), "--image", args.image,
           "--stages", f"ppl={status}",
           "--run-version", f"tools/run_flex_check.py@{_runner_sha12()}",
           "--dataset-extra", f"v1_domain={CASE_ROOT / 'datasets' / 'domain_eval.txt'}",
           "--dataset-extra", f"v2_general={CASE_ROOT / 'datasets' / 'general_eval_v2.txt'}",
           "--dataset-extra", f"v2_domain={CASE_ROOT / 'datasets' / 'domain_eval_v2.txt'}",
           "--hyperparams", f"sets={','.join(EVAL_SETS)}",
           "--hyperparams", f"states={len(states or [])}",
           "--hyperparams", "method=_ppl_eval пайплайна, импортированный из файла прогона",
           "--hyperparams-source", "замер PPL чекпойнтов диагностики (tools/flex_ppl_probe.py)",
           "--relative-to", str(CASE_ROOT), "--force"]
    if status == "done":
        cmd.append("--complete")
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"ПРЕДУПРЕЖДЕНИЕ: манифест пробы не записан (rc={p.returncode}): "
              f"{p.stderr.strip() or p.stdout.strip()}", file=sys.stderr)
        return None
    return ppl_dir / "run_manifest.json"


def arm_ppl(ppl: dict, arm: str, label: str) -> dict:
    """PPL руки по всем измеренным состояниям — с раскладкой по наборам v1/v2.

    `ppl_v1`/`ppl_v2` — состояние шага 200 (`checkpoint_final.pt`): именно его
    требует отчёт дельты; остальные точки (50/100/150) идут рядом, потому что
    «насколько хуже» без «когда стало хуже» не различает деградацию от старта и
    разворот по ходу окна.
    """
    states = {k: v for k, v in (ppl.get("states") or {}).items() if k.startswith(f"{arm}_")}
    by_step: dict = {}
    for key, st in states.items():
        step = key.split("_", 1)[1]
        by_step[step] = {name: {"ppl_corpus": s.get("ppl_corpus"),
                                "ppl_doc_median": s.get("ppl_doc_median"),
                                "docs": s.get("docs")}
                         for name, s in (st.get("sets") or {}).items()}
    final = by_step.get(str(CPT_STEPS), {})
    return {"label": label, "by_step": by_step,
            "ppl_v1": {k: v for k, v in final.items() if k.startswith("v1_")} or None,
            "ppl_v2": {k: v for k, v in final.items() if k.startswith("v2_")} or None,
            "base": {name: {"ppl_corpus": s.get("ppl_corpus"), "docs": s.get("docs")}
                     for name, s in ((ppl.get("states") or {}).get("base", {}).get("sets") or {}).items()},
            "base_hf": {name: {"ppl_corpus": s.get("ppl_corpus")}
                        for name, s in ((ppl.get("states") or {}).get("base_hf", {}).get("sets") or {}).items()}}


def analyze(ts: str, args, runs: dict[str, dict], ppl_report: dict | None) -> dict:
    """Собрать evidence/flex-check.json из артефактов прогонов и пробы PPL."""
    ppl = ppl_report or {}
    runs_out = {}
    for arm in ARM_ORDER:
        run_dir = CASE_ROOT / "runs" / run_id_for(arm, ts)
        summary = arm_summary(arm, run_dir)
        summary["stand_checkpoints"] = stand_checkpoints(args.host, f"{STAND_EXPERIMENTS}/{run_id_for(arm, ts)}")
        # Параметры прогона — из памяти (когда только что гоняли) или из каталога
        # прогона (когда отчёт пересобирается `--analyze-only`): пересборка обязана
        # давать тот же отчёт, а не более бедный.
        summary["params"] = (runs.get(arm) or {}).get("params") or _load_params(run_dir)
        summary.update(arm_ppl(ppl, arm, ARMS[arm]["label"]))
        runs_out[ARMS[arm]["label"]] = summary
    payload = {
        "stage": "S3k",
        "title": "Диагностика причины форгеттинга: flex-маска против штатного внимания",
        "adr": "ADR-022 п.1",
        "ts": ts,
        "host": args.host,
        "image": args.image,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "design": {
            "difference_between_arms": "только режим внимания (LAGUNA_ATTN)",
            "identical": {"model": MODEL, "seed": SEED, "max_len": MAX_LEN,
                          "cpt_steps": CPT_STEPS, "cpt_batch": CPT_BATCH,
                          "peak_lr_scale": PEAK_LR_SCALE,
                          "data": "cpt_corpus_v12r_8192_qwen25.npy + pos-компаньон"},
            "flex_off_how": ("LAGUNA_ATTN отсутствует/не равен flex → _flex_mode=False → "
                             "P.register()/P.attach() не вызываются, forward без position_ids, "
                             "внимание — штатный SDPA transformers"),
        },
        "runs": runs_out,
        "ppl_probe": ppl,
        "verdict": verdict_of(
            read_loss_trace(CASE_ROOT / "runs" / run_id_for("a", ts) / "logs" / "loss_trace.jsonl"),
            read_loss_trace(CASE_ROOT / "runs" / run_id_for("b", ts) / "logs" / "loss_trace.jsonl"),
            ppl),
        "artifacts": {
            "runs": [f"runs/{run_id_for(a, ts)}/" for a in ARM_ORDER],
            "ppl_run": f"runs/{ppl_run_id(ts)}/",
            "evidence": "evidence/flex-check.json",
        },
        "known_defects": _patch_drift(ts, runs),
        "non_interference": _non_interference(ts),
        "assumptions": [
            "обе руки видят один и тот же поток данных в одном порядке (один сид, "
            "одинаковый размер пула). Прямого побайтового доказательства у раннера "
            "нет; косвенные: loss шага 0 руки A (2.0989) совпал с loss шага 0 пилота "
            "(тот же режим, тот же сид, 2.0989), а попарная корреляция траекторий "
            "r=0.92 — при разных батчах её не было бы. Сам сдвиг уровня между руками "
            "объясняется маской: блочная маска запрещает внимание между документами "
            "внутри пакованного чанка, штатный SDPA его разрешает",
            "пик LR у рук один и тот же (3.50e-04): wsd_optimal_lr упирается в потолок "
            "5e-4 и на 200, и на 9776 шагах, поэтому окно не «мягче» пилота по LR, "
            "но форма расписания иная (warmup 20 против 100)",
            "«база» измерена на том же устройстве и тем же кодом, что и чекпойнты; "
            "сверка с локальным замером S3h (11.93) — в runs/<ppl>/ppl.json (base_hf)",
            "flex выключен отсутствием LAGUNA_ATTN=flex, а не флагом пайплайна; "
            "факт проверен по логу (ATTN: flex_docmask)",
        ],
        "open_questions": [
            "масштабируется ли вывод на 9776 шагов: окно 200 шагов показывает "
            "направление, а не сходимость (ADR-022 п.1)",
            "что именно делает flex: если причина в нём, нужен разбор маски "
            "(границы документов из position_ids) до перезапуска CPT",
            "переносить ли переопределённый критерий валидности стадии (ADR-022 п.3) "
            "в пайплайн как стоп-условие, а не как ручную проверку",
        ],
    }
    # Статус отчёта: цепочки обеих рук завершены и проба PPL снята. «Не гоняли»
    # (`--analyze-only`) — не повод объявить partial: отсутствие записи о запуске
    # здесь означает «отчёт пересобран из артефактов», а не «прогон сорвался».
    chain_states = [r.get("status") or (r.get("wait") or {}).get("chain_status")
                    for r in runs.values()]
    payload["status"] = ("complete"
                         if all(s in (None, "done") for s in chain_states)
                         and ppl.get("status") == "ok"
                         else "partial")
    return payload


def write_evidence(payload: dict, path: Path | None = None) -> Path:
    path = path or (CASE_ROOT / "evidence" / "flex-check.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


# ─── служебное ────────────────────────────────────────────────────────────────

_RUNNER_SHA12: str | None = None


def _runner_sha12() -> str:
    """Первые 12 символов sha256 самого раннера — версия прогона (AD-2)."""
    global _RUNNER_SHA12
    if _RUNNER_SHA12 is None:
        _RUNNER_SHA12 = _sha256_text(Path(__file__).read_text(encoding="utf-8"))[:12]
    return _RUNNER_SHA12


def render_plan(args) -> str:
    ts = args.ts or datetime.now().strftime("%Y%m%d-%H%M")
    lines = [
        "== S3k: диагностика flex-маски против штатного внимания (ADR-022 п.1) ==",
        f"стенд: {args.host}; ts: {ts}; образ: {args.image}",
        "",
        "Две руки, различающиеся ТОЛЬКО режимом внимания (200 шагов CPT каждая):",
    ]
    for arm in ARM_ORDER:
        lines.append(f"  {arm}: {ARMS[arm]['label']:<8} LAGUNA_ATTN={ARMS[arm]['attn']:<5} — {ARMS[arm]['desc']}")
    lines += [
        "",
        "Общее у рук: Qwen2.5-0.5B, микс v12r (npy + pos-компаньон), "
        f"batch {CPT_BATCH}, max_len {MAX_LEN}, seed {SEED}, peak_lr_scale {PEAK_LR_SCALE}, "
        f"{CPT_STEPS} шагов",
        f"пайплайн: копия контурного в каталоге прогона ({PIPELINE_COPY_NAME}) с двумя вставками —",
        f"  · траектория loss по шагам (logs/loss_trace.jsonl) — основание вердикта (ADR-022 п.4);",
        f"  · номерной чекпойнт каждые {CKPT_EVERY} шагов без ретенции — иначе точек внутри окна нет.",
        "Рабочий laguna_pipeline_v8.py не правится и не подменяется (AD-4/AD-7).",
        "",
        "Замеры: траектория loss, PPL v1+v2 на чекпойнтах 50/100/150/200 и базе "
        "(tools/flex_ppl_probe.py, методика _ppl_eval из файла прогона), "
        "время шага, пик памяти, предупреждения о маске.",
        f"Каталоги прогонов: runs/{run_id_for('a', ts)}/, runs/{run_id_for('b', ts)}/; "
        f"проба: runs/{ppl_run_id(ts)}/",
        "",
        "Порядок: A → B → проба PPL → evidence/flex-check.json (руки последовательно, AD-5).",
        "Чего план НЕ делает: полного CPT и смены микса (ADR-022 п.2), правок ADR/спайна, "
        "снятия флага паузы и остановки llm-platform-*.",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="S3k — диагностика flex-маски против штатного внимания",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("GB10_HOST", DEFAULT_HOST))
    ap.add_argument("--ts", default=None, help="метка прогона (по умолчанию — текущее время)")
    ap.add_argument("--image", default=os.environ.get("LAGUNA_IMAGE", DEFAULT_IMAGE))
    ap.add_argument("--runs-dir", default="runs", help="каталог прогонов кейса")
    ap.add_argument("--evidence", default="evidence/flex-check.json")
    ap.add_argument("--arms", default="a,b", help="какие руки гонять (по умолчанию обе)")
    ap.add_argument("--stage-timeout", type=int, default=2700,
                    help="ожидание одной руки, с (200 шагов ≈ 10 мин + GEN-EVAL и пробы)")
    ap.add_argument("--ppl-timeout", type=int, default=3600, help="ожидание пробы PPL, с")
    ap.add_argument("--stall-minutes", type=int, default=20,
                    help="стоп-условие «стадия молчит», мин (для 200-шагового окна)")
    ap.add_argument("--plan", action="store_true", help="напечатать план и выйти")
    ap.add_argument("--preflight", action="store_true", help="только предусловия")
    ap.add_argument("--run", action="store_true", help="прогнать руки и пробу PPL")
    ap.add_argument("--ppl", action="store_true", help="только проба PPL")
    ap.add_argument("--analyze-only", action="store_true", help="собрать evidence из готового")
    ap.add_argument("--stop-only", action="store_true", help="закрыть контейнеры диагностики")
    ap.add_argument("--verify-untouched", action="store_true",
                    help="снимок запрещённых к изменению артефактов (до/после прогона)")
    ap.add_argument("--out-snapshot", default=None,
                    help="куда записать снимок (--verify-untouched); по умолчанию — stdout")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.ts = args.ts or datetime.now().strftime("%Y%m%d-%H%M")
    if args.plan:
        print(render_plan(args))
        return EXIT_OK
    if args.preflight:
        rep = preflight(args.host, args)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else
              "\n".join(f"{k}: {v}" for k, v in rep.items()))
        return EXIT_OK if rep.get("ok") else EXIT_FAIL
    if args.stop_only:
        rep = stop_containers(args.host, args.ts)
        print(rep["detail"])
        return EXIT_OK if rep.get("ok") else EXIT_FAIL
    if args.verify_untouched:
        snap = verify_untouched(args.host)
        text = json.dumps(snap, ensure_ascii=False, indent=2)
        if args.out_snapshot:
            path = CASE_ROOT / args.out_snapshot
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"снимок записан: {args.out_snapshot}")
        print(text)
        return EXIT_OK

    local_runs = CASE_ROOT / args.runs_dir
    local_runs.mkdir(parents=True, exist_ok=True)

    runs: dict[str, dict] = {}
    if args.run:
        pre = preflight(args.host, args)
        if not pre.get("ok"):
            print(f"ПРЕДУСЛОВИЕ НЕ ВЫПОЛНЕНО: {pre.get('detail', pre)}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        for arm in [a for a in ARM_ORDER if a in args.arms.split(",")]:
            built = build_run_dir(arm, args.ts, local_runs)
            built["params"]["image"] = args.image
            stand_dir = f"{STAND_EXPERIMENTS}/{run_id_for(arm, args.ts)}"
            shipped = ship(args.host, built["run_dir"], stand_dir)
            if not shipped.get("ok"):
                print(f"ДОСТАВКА НЕ УДАЛАСЬ ({arm}): {shipped.get('detail')}", file=sys.stderr)
                return EXIT_FAIL
            cmd = chain_command(arm, args.ts, stand_dir, args)
            session = f"flexcheck-{args.ts}-{arm}"
            launched = launch_tmux(args.host, session, cmd, f"{stand_dir}/logs/tmux.log")
            if not launched.get("ok"):
                print(f"ЗАПУСК НЕ УДАЛСЯ ({arm}): {launched.get('detail')}", file=sys.stderr)
                return EXIT_FAIL
            waited = wait_chain(args.host, stand_dir, session, args.stage_timeout)
            fetched = fetch(args.host, stand_dir, built["run_dir"])
            status = "done" if waited.get("chain_status") == "done" else "partial"
            if waited.get("chain_status") != "done":
                status = "failed" if waited.get("chain_status") in (None, "failed") else "partial"
            write_case_manifest(built["run_dir"], arm, args.ts, status, args)
            runs[arm] = {**built, "ship": shipped, "wait": waited, "fetch": fetched,
                         "command": cmd, "status": status}
            print(f"[{arm}] {ARMS[arm]['label']}: chain={waited.get('chain_status')} "
                  f"({waited.get('waited_sec')} с), точек траектории "
                  f"{len(read_loss_trace(built['run_dir'] / 'logs' / 'loss_trace.jsonl'))}")

    ppl_report = None
    ppl_dir = None
    if args.run or args.ppl:
        ppl_dir = build_ppl_dir(args.ts, local_runs)
        stand_ppl = f"{STAND_EXPERIMENTS}/{ppl_run_id(args.ts)}"
        ship(args.host, ppl_dir, stand_ppl)
        res = run_ppl(args.host, args.ts, args)
        rc, raw, _ = S.ssh_get(args.host, f"{stand_ppl}/ppl.json", timeout=180)
        if rc == 0 and raw:
            (ppl_dir / "ppl.json").write_bytes(raw)
            try:
                ppl_report = json.loads(raw)
            except json.JSONDecodeError:
                ppl_report = {"status": "unreadable"}
        rc2, log_raw, _ = S.ssh_get(args.host, f"{stand_ppl}/logs/ppl.log", timeout=180)
        if rc2 == 0 and log_raw:
            (ppl_dir / "logs" / "ppl.log").write_bytes(log_raw)
        write_ppl_manifest(ppl_dir, args.ts, "done" if ppl_report else "failed", args,
                           res.get("states"))
        print(f"[ppl] status={(ppl_report or {}).get('status')}, состояния={len(res.get('states', []))}")

    if not (args.run or args.ppl):
        # analyze-only: собираем из того, что уже лежит в каталогах прогонов
        for arm in ARM_ORDER:
            rd = local_runs / run_id_for(arm, args.ts)
            if rd.is_dir():
                runs[arm] = {"run_dir": rd, "params": _load_params(rd)}
        ppl_dir = local_runs / ppl_run_id(args.ts)
        pf = ppl_dir / "ppl.json"
        if pf.is_file():
            ppl_report = json.loads(pf.read_text(encoding="utf-8"))
            try:
                states = probe_states(args.host, args.ts)
            except Exception:  # стенд недоступен — манифест не повод падать
                states = []
            write_ppl_manifest(ppl_dir, args.ts,
                               "done" if ppl_report.get("status") == "ok" else "failed",
                               args, states)

    payload = analyze(args.ts, args, runs, ppl_report)
    details = {a: {k: v for k, v in r.items() if k in ("ship", "wait", "status", "command")}
               for a, r in runs.items()}
    if not any(details.values()):
        # `--analyze-only` пересобирает отчёт из артефактов, не запуская прогонов:
        # данные о доставке и запуске берём из прежнего отчёта, чтобы пересборка не
        # вычёркивала то, что было измерено при запуске.
        prev = _read_json(CASE_ROOT / args.evidence)
        details = (prev or {}).get("run_details") or details
    payload["run_details"] = details
    out = write_evidence(payload, CASE_ROOT / args.evidence)
    print(f"evidence: {out.relative_to(CASE_ROOT)}")
    print(f"вердикт: {payload['verdict']['text']}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    failed = [r.get("status") for r in runs.values() if r.get("status") not in (None, "done")]
    if failed:
        print(f"НЕ ВСЕ РУКИ ЗАВЕРШЕНЫ УСПЕШНО: {failed}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_params(run_dir: Path) -> dict | None:
    p = run_dir / "flex_check_params.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    sys.exit(main())
