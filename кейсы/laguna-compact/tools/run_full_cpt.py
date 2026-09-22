#!/usr/bin/env python3
"""S3x — раннер полного CPT на замороженном миксе `v12r` (ADR-031).

Что исполняется. Пик LR полного CPT — **×0.035** (ADR-031 п.1, замена ADR-029
п.2). Основание — два независимых замера на одном миксе и одном сиде: цена
высокого LR по языку (S3v: ×1.520/×1.745 у `25-0.35` против ×1.005/×1.053 у
`C2`) при **сопоставимом домене** (S3w: ×0.391 против ×0.384, отношение
отношений 0.982 — выигрыша у высокого LR нет). Конфигурация LR×0.35 строго
доминируема, поэтому прогон `full-cpt-20260916-1933` (LR×0.35) остановлен по
протоколу ADR-016 п.2, а его чекпойнты сохранены как артефакт «цена высокого LR».

**Что меняется между конфигурациями.** Ровно одно число — `peak_lr_scale`; оно
параметр командной строки (`--peak-lr-scale`), а не константа: иначе повтор
прогона LR×0.35 после остановки был бы невозможен из того же кода. Микс, число
шагов, сид, образ и копия пайплайна не меняются — `--prepare` сверяет хеш копии
пайплайна с калибровочными руками и отказывает при расхождении.

Чем этот прогон отличается от калибровочных рук. Только числом шагов: руки сетки
S3m и контроля S3o шли по 2000 шагов (короткая калибровка), полный CPT — **9776
шагов**, один проход по корпусу. Всё остальное берётся у руки `25-0.035`
(она же `C2` в S3o/S3v) дословно: та же копия пайплайна
(`laguna_pipeline_calib.py`, sha256 `1571cbd1…`), тот же патч (траектория лосса по
шагам + точка замера каждые 500 шагов под именем `calib_checkpoint_<N>.pt`), тот
же образ, сид 42, `LAGUNA_ATTN=flex`.

Остановка живой стадии (ADR-016 п.2). `--stop` останавливает прогон **штатным
путём цепочки**, а не убийством процесса: маркер `var/STOPPED` — тот самый файл,
который пишет собственный страж цепочки в `trip()`; увидев его, цепочка
записывает стадии статус `stopped`, гасит контейнер через `docker stop` и не
резюмируется сама. Жёсткое `docker kill` не применяется: остановка идёт по
точке замера (файл чекпойнта обязан «устояться»), ничего из каталога прогона не
удаляется, чекпойнты и логи сохраняются как артефакт, а в манифест AD-2
дописывается пометка `stopped_by`.

**Откуда число шагов.** Бюджет стадии назван в ADR-008: «CPT на корпусе v12r:
9776 чанков / batch 1 = 9776 шагов × 2.569 с = 7.0 ч на сид». То же значение
стоит в конфигурации пилота (`runs/pilot-compact-s42-20260914-1626/pilot_run.json`,
`stages[cpt].steps = 9776`). 9776 — это ровно форма кэша корпуса (один проход), и
`--prepare` проверяет это по файлу, а не по памяти: если форма кэша разойдётся с
числом шагов, подготовка откажет.

Зачем отдельный раннер, а не рука в `run_mix_lr_calib.py`. У сетки и контроля
`STEPS = 2000` — константа, на которой стоят разбор (`--analyze`), вердикт и
`--status`; полный прогон отвечает на другой вопрос («проходит ли стадия язык на
длине»), и вписывание его рукой в сетку смешало бы два вердикта в одном
манифесте. Раннер переиспользует у сетки **патч пайплайна** (одна точка правды:
копия пайплайна обязана быть той же), а свою часть — каталог, стадии, запуск —
называет сам.

Где живут артефакты. Каталог прогона — `/home/user/gb10-shared/full-cpt-<ts>`
(он же `/workspace/shared/full-cpt-<ts>` в контейнере): чекпойнты и логи пишутся
**сразу на сетевой диск** (AD-4), в кейс приезжают только малые артефакты
(`runs/full-cpt-<ts>/` — манифест, параметры, строка запуска, `ship.json`).

Замеры языка идут **мимо стенда**: чекпойнты снимаются прибором
`tools/calib_ppl_probe.py` (методика `tools/ppl_probe.py:measure`) на **локальной
машине** — стенд занят обучением (AD-5: одна нагрузка за раз), а 4080 для замеров
и не занимается обучением. Сторож замеров — `tools/full_cpt_probe.py`.

Коды возврата::

    0 — шаг выполнен
    1 — отказ (подготовка/доставка/запуск не состоялись)
    2 — NOT-VERIFIED: нет права на стенд или нет входа

Запуск по шагам (каждая команда самостоятельна — прогон длиной ~7 ч нельзя делать
одним вызовом)::

    python3 tools/run_full_cpt.py --plan
    python3 tools/run_full_cpt.py --prepare --ts 20260916-1933
    python3 tools/run_full_cpt.py --launch  --ts 20260916-1933
    python3 tools/run_full_cpt.py --verify  --ts 20260916-1933

Остановка прогона (ADR-031 п.2 → протокол ADR-016 п.2) и сборка доказательства
переключения::

    python3 tools/run_full_cpt.py --stop     --ts 20260916-1933 --wait-point
    python3 tools/run_full_cpt.py --evidence --ts <ts нового прогона> \\
        --previous-ts 20260916-1933
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE_ROOT / "tools"))

#: Патч копии пайплайна и константы контура берутся у раннера сетки: копия
#: пайплайна у полного прогона обязана быть **той же**, что у калибровочных рук,
#: иначе сравнивать их числа нечем. Второй экземпляр патча разошёлся бы с первым
#: на первой правке.
import run_mix_lr_calib as C  # noqa: E402
import run_smoke as S  # noqa: E402 — транспорт стенда (ssh/ssh_put/sha256)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

SHARED = "/home/user/gb10-shared"
CTR_SHARED = "/workspace/shared"
STAND = S.DEFAULT_HOST

#: Корпус — замороженный ADR-003 микс (`v12r`, домен 75 % / replay 25 %). Ровно тот,
#: на котором мерены K1/K2 конфигурации `25-0.35`. Микс не пересобирается (ADR-029).
CORPUS = {
    "label": "v12r (75/25, заморожен ADR-003 — не пересобирался)",
    "txt_ctr": f"{CTR_SHARED}/datasets/cpt_corpus_v12r.txt",
    "cache_host": f"{SHARED}/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy",
    "replay_chunks": 2444,
    "domain_chunks": 7332,
}

#: Конфигурация решения (ADR-031 п.1): микс `v12r` + пик LR ×0.035. Значение —
#: параметр командной строки (`--peak-lr-scale`), а не только константа: прогон
#: LR×0.35 (`full-cpt-20260916-1933`, ADR-029 п.2) остановлен этим решением, и
#: воспроизвести его (или вернуться к нему по плану откатa) обязан тот же код.
REPLAY_SHARE_PCT = 25
PEAK_LR_SCALE = 0.035
#: Конфигурации, у которых есть решение. Ключ — значение `peak_lr_scale`; текст
#: идёт в параметры прогона и в evidence, чтобы «откуда это число» не собиралось
#: по памяти при разборе артефактов.
LR_DECISIONS = {
    0.035: {"adr": "ADR-031", "share": "25", "arm": "C2 (S3o/S3v) — тот же микс, тот же сид",
            "why": "домен сопоставим (×0.384 против ×0.391), язык лучше в ×1.51 (K1) и "
                   "×1.66 (K2) — конфигурация LR×0.35 строго доминируема"},
    0.35: {"adr": "ADR-029 п.2", "share": "25", "arm": "25-0.35 (S3m)",
           "why": "историческая конфигурация: проходит конъюнкцию K1 ∧ K2 (×1.520/×1.745), "
                  "но проигрывает низкому LR по языку при том же домене (ADR-031)"},
}
SEED = 42
MODEL = C.MODEL
MAX_LEN = C.MAX_LEN
MAX_SAMPLES = C.MAX_SAMPLES
BATCH = 1
CKPT_EVERY = C.CKPT_EVERY  # 500 — ADR-015: ранний сигнал деградации

#: Число шагов полного прогона — один проход по корпусу. Источник числа назван
#: здесь и печатается в отчёт: ADR-008 (бюджет стадии) + конфигурация пилота.
STEPS = 9776
STEPS_SOURCE = ("ADR-008: «CPT на корпусе v12r: 9776 чанков / batch 1 = 9776 шагов "
                "× 2.569 с = 7.0 ч на сид» (бюджет стадии); то же значение — в "
                "runs/pilot-compact-s42-20260914-1626/pilot_run.json, stages[cpt].steps")
#: Точки замера PPL: каждые CKPT_EVERY шагов (ADR-015) + финальное состояние
#: (`checkpoint_final.pt` = состояние после STEPS шагов оптимизатора).
CKPT_POINTS = list(range(CKPT_EVERY, STEPS, CKPT_EVERY))  # 500 … 9500

SESSION_PREFIX = "full-cpt"
PROBE_WATCHER = "tools/full_cpt_probe.py"

#: Хеш копии пайплайна калибровочных рук — сверка «тот же прибор обучения»:
#: копия пайплайна у полного прогона обязана быть ровно той же (шутит не число,
#: а сравнение с ним: `--prepare` печатает результат сверки).
CALIB_PIPELINE_SHA256 = "1571cbd1043d82a2a46a6fa603df12bad339447be626e5b1195c1674dc133baa"

#: Что доставляется в каталог прогона: те же файлы, что у калибровочных рук
#: (цепочка, страж, сэмплер, генератор манифеста). Прибор PPL **не** доставляется:
#: он исполняется на локальной машине и назван путём и хешем (AD-2 — версия и хеш,
#: а не копия; данные и веса копиями не разносятся вовсе, AD-4).
SHIPPED = list(C.SHIPPED)


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


def identity_proof(probe_ts: str) -> dict:
    """Тождество прибора **числом**, а не ссылкой на договорённость.

    Берётся из записанных артефактов: базовые числа, которые сторож считает
    ожидаемыми (`baseline` в его журнале), и то, что прибор реально получил на
    базовых весах в отчёте точки (`states.base.sets.<набор>.ppl`). Расхождение
    печатается абсолютное и относительное: «числа совпали» без числа — это
    утверждение о вере, а не о приборе.
    """
    d = probe_case_dir(probe_ts)
    j = d / "state.json"
    if not j.is_file():
        return {"available": False, "why": f"нет журнала сторожа: {j}"}
    try:
        state = json.loads(j.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"available": False, "why": f"журнал не разобран: {e}"}
    base = state.get("baseline", {})
    #: `s<int>.json` — зеркало отчёта точки; `state.json` (журнал) под шаблон `s*`
    #: тоже попадает, поэтому имена фильтруются регулярным выражением, а не глазом.
    reports = sorted(p for p in d.glob("s*.json") if re.fullmatch(r"s\d+\.json", p.name))
    if not reports:
        #: «Нет отчётов» — не «тождество не выполнено»: сторож проверяет его на
        #: каждой снятой точке (`baseline_ok`), и первая же точка заполнит этот блок.
        #: Пока точек нет, честный ответ — «ещё не проверено», а не «сошлось».
        return {"available": False, "why": f"точек ещё не снято: нет зеркал отчётов в {d}",
                "expected": base,
                "note": "тождество прибора проверяется на каждой точке вердиктом "
                        "baseline_ok; блок заполнится первой же снятой точкой "
                        "(повторный запуск: run_full_cpt.py --evidence --ts <ts>)"}
    rep = json.loads(reports[-1].read_text(encoding="utf-8"))
    got = {name: node.get("ppl") for name, node in
           rep.get("states", {}).get("base", {}).get("sets", {}).items()}
    rows = {}
    for set_name, want in base.items():
        g = got.get(set_name)
        rel = abs(g - want) / abs(want) if (g is not None and want) else None
        rows[set_name] = {"expected": want, "measured_base": g, "abs_delta": (None if rel is None
                          else abs(g - want)), "rel_delta": rel,
                          "within_1e-4": (None if rel is None else rel <= 1e-4)}
    return {"available": True, "probe_dir": str(d), "report": str(reports[-1]),
            "numbers": rows,
            "all_within_1e-4": (all(v["within_1e-4"] for v in rows.values()) if rows else None),
            "baseline_ok_verdicts": {n: p.get("verdict", {}).get("baseline_ok")
                                     for n, p in state.get("points", {}).items()},
            "rule": "базовые веса обязаны воспроизвести ожидаемые числа: иначе точка снята "
                    "другим прибором или другим набором (ADR-029 п.4, ADR-031 п.4)"}


def run_dir_name(ts: str) -> str:
    return f"{SESSION_PREFIX}-{ts}"


def stand_dir(ts: str) -> Path:
    return Path(f"{SHARED}/{run_dir_name(ts)}")


def case_dir(ts: str) -> Path:
    return CASE_ROOT / "runs" / run_dir_name(ts)


def probe_case_dir(ts: str) -> Path:
    return CASE_ROOT / "runs" / f"{SESSION_PREFIX}-ppl-{ts}"


def sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


# ─────────────────────────────── подготовка ──────────────────────────────────

def corpus_shape_check() -> dict:
    """Форма кэша корпуса обязана совпасть с числом шагов — иначе отказ.

    Не «для красоты»: 9776 шагов — это утверждение «один проход по корпусу», и
    оно проверяемо по файлу. Разошедшаяся форма означает либо другой корпус, либо
    другое число эпох; и то и другое — не решение ADR-029.
    """
    import numpy as np
    cache = Path(CORPUS["cache_host"])
    if not cache.is_file():
        return {"ok": False, "why": f"нет кэша корпуса: {cache}"}
    m = np.load(cache, mmap_mode="r")
    chunks = int(m.shape[0])
    return {"ok": chunks == STEPS, "cache": str(cache), "shape": list(m.shape),
            "chunks": chunks, "steps": STEPS,
            "why": ("форма совпала с числом шагов: один проход по корпусу"
                    if chunks == STEPS else
                    f"форма кэша {chunks} ≠ числа шагов {STEPS}: это НЕ один проход")}


def build_chain_command(ts: str, image: str, sampler_seconds: int,
                        stall_minutes: int, peak_lr_scale: float = PEAK_LR_SCALE) -> str:
    """Строка запуска цепочки — одна и та же для ручного повтора и для tmux."""
    d = f"{SHARED}/{run_dir_name(ts)}"
    name = run_dir_name(ts)
    return " ".join([
        f"bash {d}/pilot_chain.sh",
        f"--run-dir {d}",
        f"--ctr-run-dir {CTR_SHARED}/{name}",
        f"--stages-file {d}/stages.tsv",
        f"--exp-base {name}",
        f"--ctr-prefix laguna-{name}",
        f"--shared {SHARED}",
        "--experiments /home/user/experiments",
        f"--pipeline {d}/{C.PIPELINE_COPY_NAME}",
        f"--pipeline-ctr {CTR_SHARED}/{name}/{C.PIPELINE_COPY_NAME}",
        f"--cpt-data {CORPUS['txt_ctr']}",
        f"--dataset {CORPUS['cache_host']}",
        "--runner-name tools/run_full_cpt.py",
        f"--extra-hyperparam replay_share_pct={REPLAY_SHARE_PCT}",
        f"--extra-hyperparam peak_lr_scale={peak_lr_scale}",
        "--extra-hyperparam corpus=v12r",
        f"--extra-hyperparam cpt_steps={STEPS}",
        f"--guard {d}/check_resource_owner.sh",
        f"--safe-start {C.SAFE_START}",
        f"--sampler {d}/smoke_mem_sampler.sh",
        f"--sampler-seconds {sampler_seconds}",
        f"--storm-gap {C.STORM_GAP}",
        f"--manifest-tool {d}/write_run_manifest.py",
        f"--image {image}",
        f"--glm-env {C.GLM_ENV}",
        f"--nvrm-log {C.NVRN_LOG}",
        f"--runner-sha12 {S.sha256_bytes(Path(__file__).read_bytes())[:12]}",
        f"--model {MODEL}",
        f"--seed {SEED}",
        f"--max-len {MAX_LEN}",
        f"--max-samples {MAX_SAMPLES}",
        f"--peak-lr-scale {peak_lr_scale}",
        "--attn flex",
        #: GEN-EVAL пайплайна выключен: он берёт за «base» шаг 50 (дефект S3k) и
        #: меряет только v1. Язык и домен меряются внешним прибором на точках 500…9500.
        "--cpt-gen-eval-every 0",
        "--mem-cap 100g",
        f"--stall-minutes {stall_minutes}",
    ])


def do_prepare(args) -> int:
    ts = args.ts or now_ts()
    d = stand_dir(ts)
    cd = case_dir(ts)
    lr = float(args.peak_lr_scale)
    decision = LR_DECISIONS.get(round(lr, 6))

    shape = corpus_shape_check()
    print(f"корпус: {shape.get('cache')} → {shape.get('shape')} ({shape['why']})")
    if not shape["ok"]:
        print(f"ОТКАЗ: {shape['why']}", file=sys.stderr)
        return EXIT_FAIL

    if d.exists() and not args.force:
        print(f"ОТКАЗ: каталог прогона уже есть: {d} (--force перезапишет)", file=sys.stderr)
        return EXIT_FAIL
    for sub in ("logs", "var/status", "checkpoints"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    cd.mkdir(parents=True, exist_ok=True)

    # 1. Копия пайплайна: тот же патч, что у калибровочных рук (одна точка правды).
    src = C.PIPELINE_SRC.read_text(encoding="utf-8")
    patched, patch_record = C.patched_pipeline(src)
    (d / C.PIPELINE_COPY_NAME).write_text(patched, encoding="utf-8")
    (d / "pipeline_patch.json").write_text(
        json.dumps(patch_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 2. Доставка инструментов цепочки (те же sha256, что у рук S3m/S3o).
    shipped = {}
    for local_name, case_rel in SHIPPED:
        data = (CASE_ROOT / case_rel).read_bytes()
        (d / local_name).write_bytes(data)
        shipped[local_name] = {"source": case_rel, "sha256": S.sha256_bytes(data)}
        if local_name.endswith(".sh"):
            os.chmod(d / local_name, 0o755)

    # 3. Стадии и параметры.
    (d / "stages.tsv").write_text(
        "\t".join(["cpt", "cpt", "cpt", "checkpoints/checkpoint_final.pt",
                   str(STEPS), str(BATCH), "-", run_dir_name(ts)]) + "\n",
        encoding="utf-8")

    instrument = {}
    for rel in ("tools/ppl_probe.py", "tools/calib_ppl_probe.py"):
        instrument[Path(rel).name] = {"path": rel, "sha256": sha256_file(CASE_ROOT / rel)}

    params = {
        "task": f"{decision['adr'] if decision else 'S3x'} — полный CPT на замороженном "
                f"миксе v12r ({decision['adr'] if decision else 'решение не названо'})",
        "decision": (f"{decision['adr']}: конфигурация {decision['share']}-{lr:g} "
                     f"(рука {decision['arm']}) — {decision['why']}" if decision else
                     f"решение по LR×{lr:g} в ADR не названо — конфигурация вне замороженного набора"),
        "mix": "v12r", "replay_share_pct": REPLAY_SHARE_PCT,
        "peak_lr_scale": lr,
        "model": MODEL, "seed": SEED, "max_len": MAX_LEN, "max_samples": MAX_SAMPLES,
        "cpt_steps": STEPS, "steps_source": STEPS_SOURCE,
        "cpt_batch": BATCH, "ckpt_every": CKPT_EVERY, "ckpt_points": CKPT_POINTS,
        "cpt_data_ctr": CORPUS["txt_ctr"], "cpt_dataset_host": CORPUS["cache_host"],
        "corpus_label": CORPUS["label"],
        "corpus_shape": shape,
        "attn": "flex (LAGUNA_ATTN=flex, FLEX_COMPILE=0) — режим оставлен владельцем",
        "pipeline_patch": patch_record,
        "instrument_ppl": instrument,
        "probe_plan": {"every_steps": CKPT_EVERY, "components": ["K1", "K2", "DOMAIN"],
                       "sets": {"K1": "datasets/general_eval_v3.txt",
                                "K2": "datasets/general_eval_k2.txt",
                                "DOMAIN": "datasets/domain_eval_v2.txt"},
                       "domain_reading": "ADR-031 п.4: × базы < 1 = домен выучен; полоса "
                                         "сопоставимости конфигураций — отношение отношений ≤ ×2; "
                                         "domain_eval.txt (5 документов) не усредняется с ним",
                       "tool": "tools/calib_ppl_probe.py (методика tools/ppl_probe.py:measure)",
                       "where": "локальная машина (4080) — стенд занят обучением (AD-5)"},
        "shipped": shipped,
        "pipeline_base_sha256": patch_record["pipeline_base_sha256"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (d / "full_cpt_params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (cd / "full_cpt_params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    cmd = build_chain_command(ts, args.image, args.sampler_seconds, args.stall_minutes, lr)
    (d / "chain_command.txt").write_text(cmd + "\n", encoding="utf-8")
    (cd / "chain_command.txt").write_text(cmd + "\n", encoding="utf-8")

    # 4. Сверка доставленного глазами стенда. Локальная запись идёт по NFS-монтированию
    #    того же диска, поэтому проверка «со стороны стенда» — не формальность, а ответ
    #    на вопрос «видит ли стенд ровно те байты, что мы посчитали хешем».
    #    Путь сверки — **хостовый** (`/home/user/gb10-shared/...`): `/workspace/shared`
    #    существует только внутри контейнера, и на хосте его нет (проверено: ssh даёт
    #    «нет такого каталога»). Это один и тот же файл — диск монтируется в контейнер.
    names = sorted(p.name for p in d.iterdir() if p.is_file())
    rc, out, err = S.ssh(STAND, f"cd {d} && sha256sum " + " ".join(names), timeout=120)
    if rc != 0:
        print(f"ОТКАЗ: файлы не видны стенду по {d}: {err.strip()}", file=sys.stderr)
        return EXIT_FAIL
    seen = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            seen[parts[1].lstrip("*./")] = parts[0]
    mismatched = [n for n in names if seen.get(n) != sha256_file(d / n)]
    ship_record = {
        "ok": not mismatched,
        "stand_dir": str(d), "ctr_dir": f"{CTR_SHARED}/{run_dir_name(ts)}",
        "sha256": {n: sha256_file(d / n) for n in names},
        "mismatched": mismatched,
        "checked_from": f"ssh {STAND} — {d} (хостовый путь того же файла; "
                        f"в контейнере он же {CTR_SHARED}/{run_dir_name(ts)})",
        "detail": ("все файлы доставлены, sha256 совпал со стороны стенда"
                   if not mismatched else f"расхождение sha256: {', '.join(mismatched)}"),
    }
    (d / "ship.json").write_text(json.dumps(ship_record, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
    (cd / "ship.json").write_text(json.dumps(ship_record, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    if mismatched:
        print(f"ОТКАЗ доставки: {ship_record['detail']}", file=sys.stderr)
        return EXIT_FAIL

    # 5. Малые файлы в кейс (для git и C-012): копии **текстов**, не данных.
    for name in (C.PIPELINE_COPY_NAME, "pipeline_patch.json", "stages.tsv",
                 "pilot_chain.sh", "check_resource_owner.sh", "smoke_mem_sampler.sh",
                 "write_run_manifest.py"):
        (cd / name).write_bytes((d / name).read_bytes())
        if name.endswith(".sh"):
            os.chmod(cd / name, 0o755)
    (cd / "logs").mkdir(exist_ok=True)
    (cd / "var" / "status").mkdir(parents=True, exist_ok=True)

    write_manifest(ts, args, stages="cpt=running")
    print(f"каталог прогона: {d}")
    same = patch_record["patched_sha256"] == CALIB_PIPELINE_SHA256
    print(f"копия пайплайна: {C.PIPELINE_COPY_NAME} sha256={patch_record['patched_sha256'][:12]} "
          f"(та же, что у калибровочных рук: {same})")
    print(f"шагов: {STEPS} (источник: {STEPS_SOURCE})")
    print(f"пик LR: ×{lr:g} ({decision['adr'] if decision else 'решение не названо'})")
    if not same:
        print("ОТКАЗ: копия пайплайна разошлась с калибровочными руками — "
              "числа полного прогона и калибровки K1/K2 станут несопоставимы",
              file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def write_manifest(ts: str, args, stages: str, complete: bool = False) -> dict:
    """Манифест AD-2 в обоих каталогах: и на сетевом диске, и в кейсе.

    Пишется **сразу при старте** — иначе гейт C-012 красный всё время прогона
    (часы), и каталог с файлами неотличим от прогона. Цепочка перезапишет
    стендовый манифест своим по завершении стадии.
    """
    d = stand_dir(ts)
    cd = case_dir(ts)
    tool = CASE_ROOT / "tools" / "write_run_manifest.py"
    common = ["--base-model", MODEL, "--seed", str(SEED), "--image", args.image,
              "--stages", stages,
              "--run-version", f"tools/run_full_cpt.py@{S.sha256_bytes(Path(__file__).read_bytes())[:12]}",
              "--hyperparams", f"replay_share_pct={REPLAY_SHARE_PCT}",
              "--hyperparams", f"peak_lr_scale={float(args.peak_lr_scale):g}",
              "--hyperparams", f"cpt_steps={STEPS}",
              "--hyperparams", "corpus=v12r",
              "--hyperparams", f"ckpt_every={CKPT_EVERY}",
              "--hyperparams-source", "full_cpt_params.json прогона (фактический запуск цепочки)",
              "--force"]
    if complete:
        common.append("--complete")
    written = {}
    for target, root in ((d, SHARED), (cd, str(CASE_ROOT))):
        #: Датасет — через симлинк кейса, как у цепочки: путь остаётся в словаре
        #: кейса и выходит в манифест относительным (AD-2/C-012).
        dataset = CASE_ROOT / "datasets" / "tok" / Path(CORPUS["cache_host"]).name
        pipe = target / C.PIPELINE_COPY_NAME
        cmd = [sys.executable, str(tool), "--run-dir", str(target),
               "--dataset", str(dataset), "--pipeline", str(pipe),
               "--relative-to", root] + common
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        written[str(target)] = {"ok": p.returncode == 0,
                                "detail": (p.stderr or p.stdout).strip()[:200]}
    return written


# ──────────────────────────────── запуск ─────────────────────────────────────

def guard(args) -> dict:
    """Право на стенд (AD-9/C-018) — **до** запуска, тем же стражем, что у цепочки."""
    p = subprocess.run(["bash", str(CASE_ROOT / "tools" / "check_resource_owner.sh"),
                        "--stage", "cpt"], capture_output=True, text=True, check=False,
                       cwd=str(CASE_ROOT))
    verdict = "CAN-START" if p.returncode == 0 else f"ОТКАЗ (exit {p.returncode})"
    return {"command": "bash tools/check_resource_owner.sh --stage cpt",
            "exit": p.returncode, "verdict": verdict,
            "output_tail": p.stdout.strip().splitlines()[-6:]}


def do_launch(args) -> int:
    ts = args.ts or now_ts()
    d = stand_dir(ts)
    cd = case_dir(ts)
    if not (d / "stages.tsv").is_file():
        print(f"NOT-VERIFIED: каталог прогона не подготовлен: {d}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    g = guard(args)
    print("\n".join(g["output_tail"]))
    if g["exit"] != 0:
        print(f"ОТКАЗ: право на стенд не подтверждено ({g['verdict']}) — стадия не стартует",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    cmd = (cd / "chain_command.txt").read_text(encoding="utf-8").strip()
    session = run_dir_name(ts)
    #: Отсоединённый запуск: tmux-сервер живёт на стенде, `setsid` уводит цепочку в
    #: свою сессию. Смерть раннера (или обрыв ssh) прогон не задевает — это и есть
    #: требование «цепочка обязана пережить завершение агента».
    inner = (f"setsid nohup {cmd} >> {d}/chain.log 2>&1 < /dev/null")
    rc, out, err = S.ssh(STAND, f"tmux kill-session -t {session} 2>/dev/null; "
                                f"tmux new-session -d -s {session} \"{inner}\"", timeout=120)
    if rc != 0:
        print(f"ОТКАЗ запуска: {err.strip() or out.strip()}", file=sys.stderr)
        return EXIT_FAIL
    (cd / "launch_command.txt").write_text(
        f"# tmux-сессия на стенде {STAND}: {session}\n"
        f"ssh {STAND} 'tmux new-session -d -s {session} \"{inner}\"'\n", encoding="utf-8")
    print(f"tmux-сессия: {session}; лог: {d}/chain.log")
    return EXIT_OK


def evidence_of_start(ts: str, wait_sec: int = 300) -> dict:
    """Подтверждение старта фактами: сессия, контейнер, лог, строка стадии, шаг.

    Код возврата ssh ничего не доказывает (AD-12), поэтому признаком старта
    служат **наблюдаемые** факты: живая tmux-сессия, контейнер стадии, растущий
    лог, запись стадии в `var/status/cpt` и шаги в траектории лосса.
    """
    d = stand_dir(ts)
    session = run_dir_name(ts)
    out: dict = {"session": session, "tmux": None, "containers": [], "log_bytes": 0,
                 "status_row": None, "loss_trace_lines": 0, "last_step": None,
                 "seconds": None}
    t0 = time.time()
    while time.time() - t0 < wait_sec:
        rc, tm, _ = S.ssh(STAND, f"tmux ls 2>/dev/null | grep -F {session} || true")
        _, cps, _ = S.ssh(STAND, 'docker ps --format "{{.Names}}\t{{.Status}}" '
                                 f'| grep -F {run_dir_name(ts)} || true')
        _, lb, _ = S.ssh(STAND, f"stat -c %s {d}/chain.log 2>/dev/null || echo 0")
        _, st, _ = S.ssh(STAND, f"cat {d}/var/status/cpt 2>/dev/null || true")
        _, tr, _ = S.ssh(STAND, f"tail -1 {d}/logs/loss_trace.jsonl 2>/dev/null || true")
        out.update({
            "tmux": tm.strip() or None,
            "containers": [l for l in cps.splitlines() if l.strip()],
            "log_bytes": int(lb.strip() or 0),
            "status_row": st.strip() or None,
            "loss_trace_lines": int(S.ssh(STAND, f"wc -l < {d}/logs/loss_trace.jsonl "
                                                 "2>/dev/null || echo 0")[1].strip() or 0),
            "last_step": (json.loads(tr).get("step") if tr.strip() else None),
            "seconds": round(time.time() - t0, 1),
        })
        if out["containers"] and out["log_bytes"] > 0:
            break
        time.sleep(10)
    return out


def do_verify(args) -> int:
    ts = args.ts or now_ts()
    d = stand_dir(ts)
    if not d.is_dir():
        print(f"NOT-VERIFIED: нет каталога прогона: {d}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    st = evidence_of_start(ts, wait_sec=args.wait)
    status = "partial" if st["containers"] else "blocked"
    print(json.dumps(st, ensure_ascii=False, indent=2))
    if status != "partial":
        print("ОТКАЗ: признаков старта нет (контейнер стадии не поднялся)", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def _first_point(ts: str) -> dict | None:
    """Первая снятая точка прогона — из журнала сторожа, а не из пересказа.

    Зачем в отчёте. Первая же точка отвечает на вопрос, ради которого дельта и
    делалась: воспроизводится ли на длинном прогоне форма низкого LR (у руки `C2`
    ступеньки нет: ×1.014/×1.044 на шаге 500), и меряется ли домен (ADR-031 п.4).
    """
    j = probe_case_dir(ts) / "state.json"
    if not j.is_file():
        return None
    try:
        state = json.loads(j.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    pts = state.get("points", {})
    if not pts:
        return None
    name = min(pts, key=lambda n: pts[n].get("step", 0))
    v = pts[name].get("verdict", {})
    return {"point": name, "step": pts[name].get("step"), "ratios": v.get("ratios"),
            "baseline_ok": v.get("baseline_ok"), "base_restored": v.get("base_restored"),
            "measured_at": pts[name].get("measured_at"),
            "trend": (json.loads((probe_case_dir(ts) / "trend.json").read_text(encoding="utf-8"))
                      if (probe_case_dir(ts) / "trend.json").is_file() else None),
            "note": "первая точка — ранний сигнал: у руки C2 (тот же микс, LR×0.035) на шаге 500 "
                    "было ×1.014/×1.044 и домен ниже базы; ступенька шага 500 — свойство "
                    "высокого LR (ADR-030)"}


def do_evidence(args) -> int:
    """Собрать `evidence/s3x-switch-lr.json` из фактов, а не из пересказа.

    Дельта состоит из двух частей — остановки конфигурации LR×0.35 и запуска
    LR×0.035, — и доказательство обязано нести **обе**: иначе «переключение» читается
    как два несвязанных события. Часть остановки не пересказывается, а берётся из
    записи `runs/<старый ts>/stopped.json`, которую оставила команда `--stop`;
    часть запуска — живым опросом стенда. Ручных чисел в отчёте нет: число, вписанное
    рукой, нельзя перепроверить.

    Отдельно доказывается «меняется только LR»: параметры нового прогона сверяются с
    параметрами остановленного поле за полем, а строки запуска — по токенам.
    """
    ts = args.ts or now_ts()
    d = stand_dir(ts)
    cd = case_dir(ts)
    if not (d / "full_cpt_params.json").is_file():
        print(f"NOT-VERIFIED: нет каталога прогона {d}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    params = json.loads((d / "full_cpt_params.json").read_text(encoding="utf-8"))
    cmd = (d / "chain_command.txt").read_text(encoding="utf-8").strip()
    ship = json.loads((d / "ship.json").read_text(encoding="utf-8"))
    st = evidence_of_start(ts, wait_sec=args.wait)

    launch_file = cd / "launch_command.txt"
    launched_at = (datetime.fromtimestamp(launch_file.stat().st_mtime).astimezone()
                   .isoformat(timespec="seconds") if launch_file.is_file() else None)

    watcher = {"state": None, "log": None, "pid_alive": None}
    #: Журнал сторожа лежит в каталоге ПРОБЫ (`runs/full-cpt-ppl-<ts>/`), а не в
    #: каталоге прогона: он же зеркало отчётов прибора. Перепутать эти два каталога
    #: значит показать в отчёте «замеров нет» при живых замерах — проверено на себе.
    wstate = probe_case_dir(ts) / "state.json"
    if wstate.is_file():
        w = json.loads(wstate.read_text(encoding="utf-8"))
        pts = w.get("points", {})
        watcher = {
            "state": {
                "probe_dir": w.get("probe_dir", {}).get("dir"),
                "sets": w.get("sets"), "components": w.get("components"),
                "every_steps": w.get("every_steps"),
                "measured": sorted(pts),
                "measured_count": len(pts),
                "ratios": {n: v.get("verdict", {}).get("ratios", {}) for n, v in pts.items()},
                "baseline_ok": all(v.get("verdict", {}).get("baseline_ok") is True
                                   for v in pts.values()) if pts else None,
            },
            "command": ("setsid nohup python3 tools/full_cpt_probe.py --ts "
                        f"{ts} --watch --poll 120 --max-hours 11 >> "
                        f"runs/{SESSION_PREFIX}-ppl-{ts}/watch.log 2>&1 < /dev/null &"),
            "log": f"runs/{SESSION_PREFIX}-ppl-{ts}/watch.log",
        }

    ckpts = {}
    rc, out, _ = S.ssh(args.stand, f"ls -la {d}/checkpoints/ 2>/dev/null | tail -8")
    if rc == 0:
        ckpts = {"listing_tail": out.strip().splitlines()}

    #: Траектория лосса — по шагам, а не сводкой по окнам (ADR-022 п.4). Здесь она
    #: приводится как **факт прогона**, а не как вердикт.
    trace = {}
    rc, out, _ = S.ssh(args.stand, f"test -f {d}/logs/loss_trace.jsonl && wc -l < "
                                   f"{d}/logs/loss_trace.jsonl || echo 0")
    if rc == 0 and out.strip().isdigit() and int(out.strip()) > 0:
        rc2, out2, _ = S.ssh(args.stand, f"head -1 {d}/logs/loss_trace.jsonl; "
                                         f"tail -1 {d}/logs/loss_trace.jsonl")
        lines = [l for l in out2.splitlines() if l.strip().startswith("{")]
        last = json.loads(lines[-1]) if lines else {}
        trace = {"file": f"{d}/logs/loss_trace.jsonl", "lines": int(out.strip()),
                 "last_step": last.get("step"), "last_loss": last.get("loss"),
                 "last_lr": last.get("lr"),
                 "note": "судить по траектории (ADR-022 п.4), а не по сводке по окнам"}

    # ── часть 1: остановленный прогон ───────────────────────────────────────
    prev_ts = args.previous_ts
    stopped = None
    if prev_ts:
        sp = case_dir(prev_ts) / "stopped.json"
        if not sp.is_file():
            sp = stand_dir(prev_ts) / "stopped.json"
        stopped = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else None
        if stopped is None:
            print(f"ВНИМАНИЕ: нет записи остановки для {prev_ts} "
                  f"(ожидалась runs/{run_dir_name(prev_ts)}/stopped.json)", file=sys.stderr)
        else:
            stopped["record_file"] = str(sp)

    # ── часть 2: «меняется только LR» — сверка параметров и строки запуска ──
    def _normalize(cmd: str, own_ts: str) -> str:
        """Убрать из строки запуска то, что обязано отличаться у любого прогона.

        Имя каталога (и всё, что из него следует: tmux-сессия, имя контейнера,
        пути) и хеш файла раннера — не «изменения конфигурации»: первый отличает
        прогоны друг от друга, второй меняется вместе с правкой самого раннера
        (в этой дельте — добавлением остановки). Сравнивать конфигурации поверх
        этих различий значит либо не увидеть настоящее расхождение, либо
        объявить расхождением шум.
        """
        s = cmd.replace(run_dir_name(own_ts), "<RUN>")
        return re.sub(r"--runner-sha12 \S+", "--runner-sha12 <SHA>", s)

    def _tokens_diff(a: str, b: str) -> dict:
        ta, tb = _normalize(a, prev_ts).split(), _normalize(b, ts).split()
        diff = [{"i": i, "stopped": x, "launched": y}
                for i, (x, y) in enumerate(zip(ta, tb)) if x != y]
        return {"differing_positions": diff, "same_length": len(ta) == len(tb),
                "n_diff": len(diff),
                "normalized": "из строк убраны имя каталога прогона и хеш раннера"}

    only_lr = None
    if prev_ts and (stand_dir(prev_ts) / "full_cpt_params.json").is_file():
        prev = json.loads((stand_dir(prev_ts) / "full_cpt_params.json").read_text(encoding="utf-8"))
        keys = ("mix", "replay_share_pct", "model", "seed", "max_len", "max_samples",
                "cpt_steps", "cpt_batch", "ckpt_every", "cpt_data_ctr", "cpt_dataset_host",
                "corpus_label", "attn", "pipeline_base_sha256")
        diffs = {k: {"stopped": prev.get(k), "launched": params.get(k)}
                 for k in keys if prev.get(k) != params.get(k)}
        prev_cmd = (stand_dir(prev_ts) / "chain_command.txt").read_text(encoding="utf-8").strip()
        pipe_prev = prev.get("pipeline_patch", {}).get("patched_sha256")
        pipe_new = params.get("pipeline_patch", {}).get("patched_sha256")
        only_lr = {
            "params_compared": list(keys),
            "param_diffs": diffs,
            "pipeline_sha256": {"stopped": pipe_prev, "launched": pipe_new,
                                "same": pipe_prev == pipe_new,
                                "same_as_calib_arms": pipe_new == CALIB_PIPELINE_SHA256},
            "corpus_shape": {"stopped": prev.get("corpus_shape", {}).get("shape"),
                             "launched": params.get("corpus_shape", {}).get("shape"),
                             "same": prev.get("corpus_shape", {}).get("shape")
                                     == params.get("corpus_shape", {}).get("shape")},
            "chain_command": _tokens_diff(prev_cmd, cmd),
            "lr": {"stopped": prev.get("peak_lr_scale"), "launched": params.get("peak_lr_scale")},
            "runner_sha12": {
                "stopped": next((t for t in re.findall(r"--runner-sha12 (\S+)", prev_cmd)), None),
                "launched": next((t for t in re.findall(r"--runner-sha12 (\S+)", cmd)), None),
                "why": "раннер — инструмент, а не конфигурация: в этой дельте он получил "
                       "остановку по ADR-016 и параметр пика LR; пайплайн, микс, сид, образ "
                       "и число шагов при этом не менялись",
            },
            "verdict": ("изменён только peak_lr_scale" if
                        set(diffs) <= {"peak_lr_scale"}
                        and pipe_prev == pipe_new
                        and _tokens_diff(prev_cmd, cmd)["n_diff"] <= 2 else
                        "ЕСТЬ ДРУГИЕ РАСХОЖДЕНИЯ — см. param_diffs/chain_command"),
        }

    status = "partial" if st["containers"] else "blocked"
    ev = {
        "schema": "s3x-switch-lr/1",
        "stage": "S3x",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "status_basis": (
            "частично: прогон LR×0.35 остановлен (часть 1), новый прогон LR×0.035 "
            "запущен автономно и идёт (часть 2); стадия не завершена — 9776 шагов ≈ 7.0 ч, "
            "замеры K1/K2/домен идут по точкам каждые 500 шагов сторожем на локальной "
            "машине. Дельта закрывается забором результата (команда ниже), а не ожиданием"),
        "task": "S3x — полный CPT на замороженном ADR-003 миксе v12r при пике LR ×0.035 "
                "(ADR-031 п.1) вместо остановленного LR×0.35",
        "decision": {
            "adr": "ADR-031",
            "what": "пик LR полного CPT — ×0.035; прогон LR×0.35 остановлен по протоколу "
                    "ADR-016 п.2, чекпойнты сохранены как артефакт «цена высокого LR»",
            "why": "конфигурация LR×0.35 строго доминируема: домен сопоставим (×0.384 против "
                   "×0.391 — отношение отношений 0.982), язык хуже в ×1.51 (K1) и ×1.66 (K2)",
            "not_changed": "микс, число шагов, сид, образ, копия пайплайна — без изменений "
                           "(сверка ниже: only_lr)",
        },
        "launch_first_point": _first_point(ts),
        "identity_proof": {
            "requirement": "тождество прибора доказать числом: общая база 11.931923888, "
                           "K1 7.504686374, K2 6.159936, домен 11.134115856; допуск ±1e-4",
            "launch_run": identity_proof(ts),
            "previous_run": identity_proof(prev_ts) if prev_ts else None,
        },
        "part1_stopped": stopped,
        #: Правило точек ADR-030, посчитанное по журналу **остановленного** прогона
        #: (артефакт `runs/full-cpt-ppl-<ts>/trend.json`): независимый механический
        #: сигнал к тому же решению, что принято по замерам S3v/S3w. Читается из
        #: артефакта, а не пересчитывается здесь: одно правило — одна реализация.
        "stopped_run_trend": (
            json.loads((probe_case_dir(prev_ts) / "trend.json").read_text(encoding="utf-8"))
            if (prev_ts and (probe_case_dir(prev_ts) / "trend.json").is_file()) else None),
        "only_lr_changed": only_lr,
        "part2_launched": {
            "run": {
                "dir": str(d),
                "ctr_dir": f"{CTR_SHARED}/{run_dir_name(ts)}",
                "case_dir": f"runs/{run_dir_name(ts)}",
                "command": cmd,
                "session": st["session"],
                "mixin": "v12r (75/25, заморожен ADR-003)",
                "peak_lr_scale": params["peak_lr_scale"],
                "replay_share_pct": params["replay_share_pct"],
                "steps": params["cpt_steps"],
                "steps_source": params["steps_source"],
                "steps_check": params["corpus_shape"],
                "seed": params["seed"],
                "model": params["model"],
                "image": args.image,
                "attn": params["attn"],
                "ckpt_every": params["ckpt_every"],
                "pipeline": {"file": C.PIPELINE_COPY_NAME,
                             "sha256": params["pipeline_patch"]["patched_sha256"],
                             "same_as_calib_arms": (params["pipeline_patch"]["patched_sha256"]
                                                    == CALIB_PIPELINE_SHA256)},
                "launched_at": launched_at,
                "launch_mechanism": "tmux-сессия на стенде + setsid nohup (переживает смерть агента)",
                "evidence_of_start": st,
            },
            "ship": {"ok": ship["ok"], "files": len(ship["sha256"]),
                     "mismatched": ship["mismatched"],
                     "checked_from": ship["checked_from"], "detail": ship["detail"]},
            "checkpoints": ckpts,
            "loss_trace": trace,
            "manifest": (json.loads((cd / "run_manifest.json").read_text(encoding="utf-8"))
                         if (cd / "run_manifest.json").is_file() else None),
        },
        "probe_plan": {
            "every_steps": params["ckpt_every"],
            "components": ["K1", "K2", "DOMAIN"],
            "sets": {"K1": "datasets/general_eval_v3.txt", "K2": "datasets/general_eval_k2.txt",
                     "DOMAIN": "datasets/domain_eval_v2.txt (ADR-031 п.4 — решающий доменный "
                               "набор; domain_eval.txt из 5 документов не усредняется с ним)",
                     "reference": "datasets/general_eval.txt (опорно: тождество прибора)"},
            "tool": "tools/calib_ppl_probe.py (методика tools/ppl_probe.py:measure)",
            "instrument_sha256": params["instrument_ppl"],
            "where": "локальная машина (RTX 4080) — стенд занят обучением (AD-5); "
                     "обучение на 4080 не запускается",
            "identity_numbers": {
                "v1_general": {"value": 11.931923888434497,
                               "source": "эталон S3h (evidence/ppl-baseline-v1v2.json, "
                                         "ppl.v1_general) — число, к которому сверяется прибор"},
                "v2_domain": {"value": 11.134115855539092,
                              "source": "эталон S3h (ppl.v2_domain) — тот же эталон, набор домена"},
                "v3_general": {"value": 7.50468637420902,
                               "source": "базовое состояние прибора в отчётах точек "
                                         "(states.base.sets.v3_general.ppl) — число, которым мерены "
                                         "точки K1; в S3h набора v3 нет (он добавлен позже, ADR-025)"},
                "k2_general": {"value": 6.1599356842437585,
                               "source": "базовое состояние прибора в отчётах точек "
                                         "(states.base.sets.k2_general.ppl) — число, которым мерены "
                                         "точки K2; в S3h набора k2 нет (он добавлен позже, ADR-027)"},
                "tolerance": "±1e-4 (относительное расхождение базовых чисел, требование дельты); "
                             "сторож проверяет с запасом — 1e-3, и печатает вердикт baseline_ok "
                             "на каждой точке; достигнутое расхождение — в identity_proof",
            },
            "watcher": watcher,
            "points": [{"name": p["name"], "step": p["step"], "ckpt": p["ckpt"]}
                       for p in ([{"name": f"s{s}", "step": s,
                                   "ckpt": f"checkpoints/calib_checkpoint_{s}.pt"}
                                  for s in params["ckpt_points"]]
                                 + [{"name": f"s{params['cpt_steps']}",
                                     "step": params["cpt_steps"],
                                     "ckpt": "checkpoints/checkpoint_final.pt"}])],
            "rule": "ADR-030: тревога — точка выше потолка и следующая не лучше; остановка — "
                    "рост на трёх точках подряд выше потолка; контроль первой четверти — "
                    "к шагу 2500 конъюнкция K1 ∧ K2 и домен обязаны быть в потолке "
                    "(считает сторож: tools/full_cpt_probe.py --trend --ts " + ts + ")",
        },
        "how_to_fetch": [
            f"прогресс стадии: ssh {args.stand} 'tail -3 {d}/logs/loss_trace.jsonl; "
            f"cat {d}/var/status/cpt'",
            f"живость цепочки: ssh {args.stand} 'tmux ls; docker ps | grep {run_dir_name(ts)}'",
            f"цепочка целиком (руками): {cmd}",
            f"замеры K1/K2/домен: журнал runs/{SESSION_PREFIX}-ppl-{ts}/state.json; отчёты — "
            f"{SHARED}/{SESSION_PREFIX}-ppl-{ts}/<точка>.json",
            f"тренд по ADR-030: python3 tools/full_cpt_probe.py --trend --ts {ts}",
            f"забрать манифест и логи в кейс: ssh {args.stand} 'cat {d}/run_manifest.json' > "
            f"runs/{run_dir_name(ts)}/run_manifest.json",
            f"артефакт «цена высокого LR»: runs/{run_dir_name(prev_ts or '<ts>')}/stopped.json "
            f"и {SHARED}/{run_dir_name(prev_ts or '<ts>')}/ (чекпойнты 500…2500/3000 не удалены)",
            "судить стадию по конъюнкции (ADR-027/ADR-031): на каждой точке K1/K2 ≤ 2× своей "
            "базы (K1 15.009373, K2 12.319871), домен × базы < 1 — числа точек в отчётах сторожа",
        ],
        "artifacts": [
            f"runs/{run_dir_name(ts)}/ (манифест AD-2, параметры, строка запуска, ship.json, "
            f"копия пайплайна, HANDOFF.md)",
            f"{d}/ (чекпойнты и логи — на сетевом диске, AD-4)",
            f"runs/{SESSION_PREFIX}-ppl-{ts}/ (журнал и зеркала отчётов замеров)",
            f"runs/{run_dir_name(prev_ts)}/stopped.json — запись остановки (если --previous-ts)",
            f"runs/{SESSION_PREFIX}-ppl-{prev_ts}/trend.json — правило ADR-030 по точкам "
            f"остановленного прогона (тревога + контроль первой четверти), "
            f"runs/{SESSION_PREFIX}-ppl-{ts}/trend.json — то же для идущего",
        ],
        "invariants": {
            "AD-2": "run_manifest.json с хешем датасета (полный файл), версией пайплайна, образом, "
                    "сидом — на первом шаге; пометка остановки — в манифесте остановленного прогона",
            "AD-4": "данные и веса — на gb10-shared; в каталоге прогона только копии текстов "
                    "и симлинк datasets у пробы",
            "AD-5": "на GB10 одна тренировочная нагрузка; замеры PPL идут на локальной карте, "
                    "обучение на 4080 не запускается",
            "ADR-027": "компоненты меры не смешиваются: K1, K2 и домен считаются и публикуются раздельно",
            "ADR-030": "правило промежуточных точек: тревога по тренду, остановка по росту на трёх "
                       "точках, контроль первой четверти — вынесено в сторожа (--trend)",
            "ADR-031": "пик LR — ×0.035; остановка LR×0.35 корректна (не docker kill), артефакты целы",
            "ADR-016 п.2": "остановка — штатным путём цепочки (маркер var/STOPPED + docker stop); "
                           "правки в каталог прогона не вносились под живым читателем",
            "ADR-023 п.11": "долгоживущий прогон: подтверждён старт, ожидания завершения нет",
        },
        "assumptions": [
            "Маркер var/STOPPED прочитан как **объявленный канал управления**, а не как «правка "
            "под живой цепочкой» (запрет ADR-016 R2 назван для файлов-входов: stages.tsv, "
            "pilot_chain.sh, check_resource_owner.sh). Основание: этот файл штатно создаёт "
            "собственный страж цепочки в trip(), и цепочка на него реагирует статусом stopped "
            "— то есть это интерфейс остановки, а не обход правила. Запись сделана до docker stop, "
            "чтобы цепочка записала именно stopped, а не failed.",
            "«Дать цепочке завершить текущий шаг/точку» прочитано как «дождаться ближайшей точки "
            "замера (шаг, кратный 500) и её устоявшегося файла чекпойнта»: это снимает риск "
            "усечённого файла (2.9 ГБ пишутся секундами) и оставляет ещё одну полную точку "
            "в артефакте. Ожидание ограничено по времени и при неистечении фиксируется как факт.",
            "«В потолке» для домен-метрики прочитано по ADR-031 п.4 как «домен выучен» "
            "(× базы < 1), а не как потолок 2× базы: 2× для домена — это полоса сопоставимости "
            "конфигураций, а не признак выученности.",
            "Замеры идут на локальной карте (4080), а не в контейнере стенда: TASK резервирует "
            "4080 под замеры PPL, а замер в контейнере спорил бы с обучением за память (AD-5).",
        ],
        "open_questions": [
            "Профиль LR перенесён с 2000 шагов калибровки на бюджет 9776 шагов (названное "
            "допущение ADR-031 п.5): поведение низкого LR на длине не измерено — возможен более "
            "медленный выход на плато по домену. Точки 500…9776 дают ранний ответ на это.",
            "Порог остановки стадии по промежуточной точке теперь механический (ADR-030 правило "
            "в сторожe), но порог для ПРОМЕЖУТОЧНОЙ точки по домену числом не назван: ADR-031 п.4 "
            "даёт «× базы < 1 = выучен», но не говорит, что делать, если домен растёт (хуже) "
            "на трёх точках подряд — правило ADR-030 п.4 сформулировано для конъюнкции языка.",
        ],
    }
    out_path = CASE_ROOT / "evidence" / "s3x-switch-lr.json"
    out_path.write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"evidence: {out_path}")
    print(f"статус: {status}; шагов: {ev['part2_launched']['run']['steps']}"
          f" ({ev['part2_launched']['run']['steps_source'][:60]}…)")
    lrs = (only_lr or {}).get("lr", {})
    print(f"LR: было ×{lrs.get('stopped')} → стало ×{lrs.get('launched')}; "
          f"сверка «только LR»: {(only_lr or {}).get('verdict')}")
    print(f"замеров снято: {(watcher.get('state') or {}).get('measured_count', 0)}")
    return EXIT_OK


# ───────────────── остановка живой стадии (ADR-031 п.2 → ADR-016 п.2) ─────────

#: Текст маркера остановки. Первая строка читается цепочкой в лог (`head -1`
#: в `attempt_stage`), поэтому она самодостаточна: по журналу прогона должно быть
#: видно, **чьим решением** стадия остановлена, а не только «стоп-сработал».
STOP_REASON_HEAD = (
    "ADR-031 п.2 (протокол вмешательства ADR-016 п.2): конфигурация LR×0.35 строго "
    "доминируема — домен сопоставим (×0.384 против ×0.391, отношение 0.982), язык хуже "
    "в ×1.51 (K1) и ×1.66 (K2); стадия остановлена по решению, чекпойнты сохранены "
    "как артефакт «цена высокого LR», из каталога прогона ничего не удаляется")
STOP_REASON_EXTRA = (
    "Остановка выполнена штатным путём цепочки: маркер var/STOPPED (тот же файл, что "
    "пишет её собственный страж в trip()) + docker stop контейнера стадии. Жёсткое "
    "docker kill не применялось: дано завершиться текущей точке замера, файл чекпойнта "
    "проверен на «устоявшесть» (размер не растёт между опросами).")


def chain_container(ts: str) -> str:
    """Имя контейнера стадии. У цепочки `ctr = "$CTR_PREFIX-$name"`, а префикс задан
    строкой запуска как `laguna-<имя каталога прогона>` (см. `chain_command.txt`)."""
    return f"laguna-{run_dir_name(ts)}-cpt"


#: Снимок состояния прогона **со стороны стенда** — одним ssh-вызовом. Один вызов,
#: а не двенадцать: во-первых, это одна точка во времени (иначе «шаг» и «статус»
#: читались бы из разных моментов), во-вторых, стенд отвечает целиком или не
#: отвечает вовсе — промежуточное состояние не выдаётся за факт.
FACTS_SCRIPT = r"""
DOCKER="${DOCKER:-docker}"
d=%(d)s; ctr=%(ctr)s; s=%(session)s
printf 'host=%%s\n' "$(hostname)"
printf 'ctr_running=%%s\n' "$("$DOCKER" ps --format '{{.Names}}' 2>/dev/null | grep -qx "$ctr" && echo yes || echo no)"
printf 'ctr_exists=%%s\n' "$("$DOCKER" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$ctr" && echo yes || echo no)"
printf 'docker_line=%%s\n' "$("$DOCKER" ps -a --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -F "$ctr" || true)"
printf 'tmux=%%s\n' "$(tmux ls 2>/dev/null | grep -F "$s" || true)"
printf 'stage_status=%%s\n' "$(cut -f1 $d/var/status/cpt 2>/dev/null || true)"
printf 'stage_row=%%s\n' "$(head -c 400 $d/var/status/cpt 2>/dev/null | tr '\t' ' ' || true)"
printf 'chain_status=%%s\n' "$(cat $d/var/chain.status 2>/dev/null || true)"
printf 'chain_pid=%%s\n' "$(cat $d/var/chain.pid 2>/dev/null || true)"
printf 'stop_marker=%%s\n' "$(head -1 $d/var/STOPPED 2>/dev/null || true)"
printf 'trace_lines=%%s\n' "$(wc -l < $d/logs/loss_trace.jsonl 2>/dev/null || echo 0)"
printf 'trace_last=%%s\n' "$(tail -1 $d/logs/loss_trace.jsonl 2>/dev/null || true)"
printf 'ckpts=%%s\n' "$(ls -1 $d/checkpoints/ 2>/dev/null | tr '\n' ',' || true)"
printf 'final_exists=%%s\n' "$([ -f $d/checkpoints/checkpoint_final.pt ] && echo yes || echo no)"
printf 'dir_exists=%%s\n' "$([ -d $d ] && echo yes || echo no)"
"""


def stand_facts(ts: str, host: str | None = None) -> dict | None:
    """Состояние прогона со стороны стенда. `None` — стенд не ответил (не «пусто»)."""
    d = stand_dir(ts)
    script = FACTS_SCRIPT % {"d": d, "ctr": chain_container(ts),
                            "session": run_dir_name(ts)}
    rc, out, err = S.ssh(host or STAND, script, timeout=120)
    if rc != 0:
        return None
    facts = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            facts[k.strip()] = v.strip()
    facts["_ssh_rc"] = rc
    return facts


def loss_trace_last_step(facts: dict) -> int | None:
    """Номер последнего шага из снимка — по строке траектории, а не по догадке."""
    try:
        return int(json.loads(facts.get("trace_last", "")).get("step"))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None


def checkpoint_settled(ts: str, step: int, host: str | None = None,
                       gap: int = 25) -> dict:
    """Файл точки замера существует и **не растёт** между двумя опросами.

    Проверка не формальность: чекпойнт пишется на 2.9 ГБ, и остановка в момент
    записи оставила бы усечённый файл — то есть «сохранённый артефакт», которым
    нельзя воспользоваться. Существование файла (`test -f`) этого не ловит.
    """
    rel = f"checkpoints/calib_checkpoint_{step}.pt"
    path = stand_dir(ts) / rel
    sizes = []
    for i in range(2):
        #: `stat`, а не `wc -c < file`: рост файла виден по метаданным, а чтение
        #: 2.9 ГБ ради счётчика байт — это минуты ожидания ни за что.
        rc, out, _ = S.ssh(host or STAND, f"stat -c %s {path} 2>/dev/null || echo 0")
        try:
            sizes.append(int(out.strip().split()[0]))
        except (ValueError, IndexError):
            sizes.append(0)
        if i == 0:
            time.sleep(gap)
    return {"ckpt": rel, "size": sizes[-1], "size_before": sizes[0],
            "settled": sizes[0] > 0 and sizes[0] == sizes[-1], "gap_s": gap}


def wait_for_point(ts: str, facts: dict, minutes: int, host: str | None = None,
                   poll: int = 30) -> dict:
    """Дождаться конца записи точки замера — «дать цепочке завершить точку» (ADR-016).

    Что именно защищается: сохранность файла чекпойнта. Запись идёт **в момент
    шага, кратного `ckpt_every`** (файл 2.9 ГБ пишется секундами), поэтому ждать
    нужно ровно тогда, когда текущий шаг стоит на такой границе. Если шаг уже ушёл
    вперёд, писать нечего: проверяется лишь то, что последний файл точки **не
    растёт** — то есть записи в полёте нет, и остановка не оставит усечённый файл.

    Ожидание ограничено `minutes`; не дождались — это фиксируется фактом
    (`settled=false`), а не выдаётся за успех.
    """
    step = loss_trace_last_step(facts)
    if step is None:
        return {"waited": False, "why": "в траектории нет разбираемой последней строки"}
    on_boundary = step > 0 and step % CKPT_EVERY == 0
    if not on_boundary:
        #: Текущая точка (последняя пройденная) — уже записана; проверяем, что её
        #: файл не растёт, и на этом останавливаемся. Ждать СЛЕДУЮЩУЮ точку значит
        #: платить полчаса стенда за артефакт, которого решение не требует.
        last_point = (step // CKPT_EVERY) * CKPT_EVERY
        if last_point <= 0:
            return {"waited": False, "from_step": step,
                    "why": f"шаг {step} до первой точки — писать нечего"}
        settled = checkpoint_settled(ts, last_point, host=host)
        return {"waited": False, "from_step": step, "last_point": last_point,
                "checkpoint": settled,
                "why": (f"записи в полёте нет: файл точки {last_point} не растёт"
                        if settled["settled"] else
                        f"файл точки {last_point} растёт или пуст — остановка может "
                        f"оставить усечённый файл; решение принято владельцем")}
    t0 = time.time()
    last = {}
    while time.time() - t0 < minutes * 60:
        last = checkpoint_settled(ts, step, host=host)
        if last["settled"]:
            return {"waited": True, "from_step": step, "target_step": step,
                    "seconds": round(time.time() - t0, 1), "checkpoint": last,
                    "why": f"запись точки {step} завершена (файл устоялся)"}
        time.sleep(poll)
    return {"waited": False, "why": f"запись точки {step} не устоялась за {minutes} мин",
            "from_step": step, "target_step": step, "checkpoint": last,
            "seconds": round(time.time() - t0, 1)}


def write_stop_marker(ts: str, extra: str, host: str | None = None) -> dict:
    """Маркер остановки — штатный путь цепочки (`trip()` пишет ровно этот файл).

    Почему это не «правка под живой цепочкой» (запрет ADR-016 R2): запрет назван
    для файлов, которые живой цикл читает как **вход** (`stages.tsv` — его
    перезапись рвёт читателя). `var/STOPPED` — не вход, а объявленный канал
    управления: его создаёт сам страж цепочки, и цепочка на него реагирует
    (записывает стадии статус `stopped` и не резюмируется). Решение ADR-031 п.2
    требует именно остановки, а не «подождать, пока сработает что-то другое».
    """
    text = f"{STOP_REASON_HEAD}\n{extra}\n"
    rc, err = 0, ""
    path = stand_dir(ts) / "var" / "STOPPED"
    try:
        rc, err = S.ssh_put(host or STAND, str(path), text.encode("utf-8"))
    except OSError as e:
        rc, err = 125, str(e)
    return {"path": str(path), "text": text, "written": rc == 0, "exit": rc,
            "error": err[-200:] if err else ""}


def docker_stop(ts: str, timeout: int, host: str | None = None) -> dict:
    """Штатная остановка контейнера стадии: `docker stop` (SIGTERM + таймаут)."""
    ctr = chain_container(ts)
    rc, out, err = S.ssh(host or STAND, f"docker stop -t {timeout} {ctr} 2>&1",
                         timeout=timeout + 120)
    return {"command": f"docker stop -t {timeout} {ctr}", "exit": rc,
            "stdout": out.strip()[-200:], "stderr": err.strip()[-200:],
            "graceful": rc == 0}


def artifact_inventory(d: Path, hash_checkpoints: bool = False) -> dict:
    """Что сохранено в каталоге прогона: поимённо, с размером и хешем (AD-12).

    Хеши чекпойнтов считаются только по требованию (`--hash-checkpoints`):
    2.9 ГБ × 5 через сетевой диск — это минуты чтения, и тратить их на каждой
    проверке значит платить за доказательство дважды. По умолчанию фиксируются
    размер и время — их достаточно, чтобы увидеть подмену/усечение файла, а хеш
    добавляется отдельным прогоном той же команды (она идемпотентна).
    """
    out: dict = {"dir": str(d), "files": {}, "checkpoints": {}, "bytes": {"files": 0,
                                                                          "checkpoints": 0}}
    for rel in ("run_manifest.json", "full_cpt_params.json", "chain_command.txt",
                "stages.tsv", "chain.log", "pipeline_patch.json", "ship.json",
                "laguna_pipeline_calib.py", "logs/loss_trace.jsonl", "logs/cpt.log",
                "var/chain.status", "var/status/cpt", "var/STOPPED"):
        p = d / rel
        if p.is_file():
            st = p.stat()
            out["files"][rel] = {
                "bytes": st.st_size, "sha256": sha256_file(p),
                "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(
                    timespec="seconds")}
            out["bytes"]["files"] += st.st_size
    ck = d / "checkpoints"
    if ck.is_dir():
        for p in sorted(ck.glob("*.pt")):
            st = p.stat()
            rec = {"bytes": st.st_size,
                   "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(
                       timespec="seconds"),
                   "sha256": sha256_file(p) if hash_checkpoints else None}
            out["checkpoints"][p.name] = rec
            out["bytes"]["checkpoints"] += st.st_size
    out["hash_checkpoints"] = hash_checkpoints
    out["count"] = {"files": len(out["files"]), "checkpoints": len(out["checkpoints"])}
    return out


def trace_integrity(d: Path) -> dict:
    """Траектория лосса целиком разбирается — доказательство, что остановка не
    оставила усечённую строку (иначе «сохранённый лог» читается не весь)."""
    p = d / "logs" / "loss_trace.jsonl"
    if not p.is_file():
        return {"file": str(p), "exists": False}
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    bad, steps = [], []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            steps.append(int(json.loads(line)["step"]))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            bad.append(i)
    return {"file": str(p), "exists": True, "lines": len(lines),
            "parsed": len(steps), "unparsable_lines": bad,
            "first_step": steps[0] if steps else None,
            "last_step": steps[-1] if steps else None,
            "monotone": all(b > a for a, b in zip(steps, steps[1:])),
            "intact": not bad and bool(steps)}


def patch_manifest(ts: str, stop: dict) -> dict:
    """Пометка остановки в манифесте AD-2 — в обоих каталогах (стенд и кейс).

    Правка идёт **после** выхода цепочки: под живой цепочкой манифест не читается
    ею как вход, но правило ADR-016 «правки — после остановки» соблюдается буквально,
    чтобы не заводить исключений из правила.
    """
    written = {}
    for target in (stand_dir(ts), case_dir(ts)):
        p = target / "run_manifest.json"
        if not p.is_file():
            written[str(p)] = {"ok": False, "detail": "манифеста нет"}
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            written[str(p)] = {"ok": False, "detail": f"манифест не разобран: {e}"}
            continue
        note = {"stopped_by": stop["stopped_by"], "stopped_at_step": stop["stopped_at_step"],
                "purpose": stop["purpose"], "stopped_at": stop["stopped_at"],
                "reason": STOP_REASON_HEAD,
                "artifacts_kept": {"files": stop["artifacts_kept"]["count"]["files"],
                                   "checkpoints": stop["artifacts_kept"]["count"]["checkpoints"],
                                   "bytes": stop["artifacts_kept"]["bytes"],
                                   "record": "runs/<ts>/stopped.json"}}
        if stop.get("record_updated_at"):
            #: Повторный проход (`--hash-checkpoints`) уточняет запись, но не переписывает
            #: факт остановки: время остановки остаётся временем остановки.
            note["record_updated_at"] = stop["record_updated_at"]
        data["stop"] = note
        for st in data.get("stages", []):
            if st.get("name") == "cpt" and st.get("status") == "running":
                st["status"] = "stopped"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written[str(p)] = {"ok": True, "detail": "пометка stop добавлена, статус cpt=stopped"}
    return written


def do_stop(args) -> int:
    """Остановить живой прогон по протоколу ADR-016 п.2. Ничего не удаляется."""
    ts = args.ts or now_ts()
    d = stand_dir(ts)
    cd = case_dir(ts)
    if not d.is_dir():
        print(f"NOT-VERIFIED: нет каталога прогона {d}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    f0 = stand_facts(ts, host=args.stand)
    if f0 is None or f0.get("dir_exists") != "yes":
        print(f"NOT-VERIFIED: стенд не подтвердил каталог прогона {d} "
              f"(состояние не наблюдаемо — останавливать вслепую нельзя)", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    if f0.get("final_exists") == "yes":
        print("ОТКАЗ: у прогона есть финальный артефакт (checkpoint_final.pt) — стадия "
              "завершена, останавливать нечего", file=sys.stderr)
        return EXIT_FAIL

    print(f"до остановки: шаг {loss_trace_last_step(f0)}, контейнер "
          f"{f0.get('ctr_running')}, цепочка {f0.get('chain_status')}, "
          f"стадия {f0.get('stage_status')}")
    print(f"  чекпойнты: {f0.get('ckpts') or '—'}")

    running = f0.get("ctr_running") == "yes"
    wait = {"waited": False, "why": "контейнер не работал — ждать нечего"}
    if args.wait_point and running:
        print(f"ждём конца текущей точки замера (не дольше {args.wait_minutes} мин)…")
        wait = wait_for_point(ts, f0, args.wait_minutes, host=args.stand)
        print(f"  ожидание точки: {json.dumps(wait, ensure_ascii=False)}")

    marker = None
    if running:
        extra = (f"Прогон: {run_dir_name(ts)}; остановка выполнена раннером "
                 f"tools/run_full_cpt.py --stop; ожидание точки: "
                 f"{'да' if wait.get('waited') else 'нет'} ({wait.get('why', '')}).")
        marker = write_stop_marker(ts, extra, host=args.stand)
        print(f"маркер остановки: {marker['path']} (записан: {marker['written']})")
        if not marker["written"]:
            print("ОТКАЗ: маркер остановки не записан — без него docker stop даст статус "
                  "failed вместо stopped, и прогон станет неотличим от упавшего",
                  file=sys.stderr)
            return EXIT_FAIL
        stopped = docker_stop(ts, args.stop_timeout, host=args.stand)
        print(f"остановка контейнера: {stopped['command']} → rc={stopped['exit']} "
              f"{stopped['stdout'] or stopped['stderr']}")
    else:
        stopped = {"command": None, "exit": None,
                   "detail": "контейнер стадии не работал (уже остановлен)"}
        print("контейнер стадии не работал — повторная остановка не требуется "
              "(команда идемпотентна)")

    # Ждём, пока цепочка допишет статус стадии своим штатным путём. Ответ стенда
    # обязателен: подставлять сюда устаревший снимок (`f0`) значит ждать 300 с
    # на состоянии, которого уже нет (проверено на живой остановке).
    t0 = time.time()
    f1 = f0
    unanswered = 0
    while time.time() - t0 < args.stop_settle_seconds:
        f = stand_facts(ts, host=args.stand)
        if f is None:
            unanswered += 1
            time.sleep(10)
            continue
        f1 = f
        if f1.get("stage_status") in ("stopped", "done", "failed"):
            break
        if f1.get("ctr_running") == "no":
            break
        time.sleep(10)
    if unanswered:
        print(f"ВНИМАНИЕ: стенд не ответил {unanswered} раз(а) при ожидании статуса стадии")
    print(f"после остановки: контейнер {f1.get('ctr_running')}, стадия "
          f"{f1.get('stage_status')}, цепочка {f1.get('chain_status')}, "
          f"шаг {loss_trace_last_step(f1)}")

    stop_record = {
        "stopped_by": "ADR-031",
        "stopped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stopped_at_step": loss_trace_last_step(f1),
        "purpose": 'artifact "цена высокого LR"',
        "run_dir": str(d),
        "case_dir": str(cd),
        "container": chain_container(ts),
        "reason": STOP_REASON_HEAD,
        "method": {
            "marker": (marker or {}).get("path"),
            "marker_text": (marker or {}).get("text"),
            "docker_stop": stopped,
            "hard_kill_used": False,
            "point_wait": wait,
            "protocol": "ADR-016 п.2: штатный путь цепочки (var/STOPPED + docker stop), "
                        "не docker kill; из каталога прогона ничего не удалено",
        },
        "state_before": f0,
        "state_after": f1,
        "artifacts_kept": None,
        "manifest_note": None,
    }
    inv = artifact_inventory(d, hash_checkpoints=args.hash_checkpoints)
    trace = trace_integrity(d)
    stop_record["artifacts_kept"] = inv
    stop_record["loss_trace"] = trace
    #: Повторный проход (например, с `--hash-checkpoints`) уточняет запись, но не
    #: выдаёт себя за вторую остановку: время и способ берутся у первой записи.
    prev_rec = None
    prev_path = cd / "stopped.json"
    if prev_path.is_file():
        try:
            prev_rec = json.loads(prev_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev_rec = None
    if prev_rec and not running:
        updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stop_record["stopped_at"] = prev_rec.get("stopped_at", stop_record["stopped_at"])
        stop_record["stopped_at_step"] = prev_rec.get("stopped_at_step",
                                                       stop_record["stopped_at_step"])
        stop_record["method"] = prev_rec.get("method", stop_record["method"])
        stop_record["state_before"] = prev_rec.get("state_before", stop_record["state_before"])
        stop_record["record_updated_at"] = updated
        stop_record["record_updates"] = prev_rec.get("record_updates", []) + [updated]
    print(f"сохранено: файлов {inv['count']['files']} ({inv['bytes']['files']} Б), "
          f"чекпойнтов {inv['count']['checkpoints']} ({inv['bytes']['checkpoints']} Б); "
          f"траектория: строк {trace.get('lines')}, разобрано {trace.get('parsed')}, "
          f"битых {len(trace.get('unparsable_lines', []))}")
    if not trace.get("intact"):
        print("ВНИМАНИЕ: траектория лосса разобрана не полностью — это фиксируется "
              "как есть (строки не удаляются), а не замалчивается")

    stop_record["manifest_note"] = patch_manifest(ts, stop_record)
    #: Запись остановки кладётся в оба каталога; каталог кейса создаётся, если его
    #: нет (прогон мог быть подготовлен без него — например, на фикстуре проверки).
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "stopped.json").write_text(
        json.dumps(stop_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (d / "stopped.json").write_text(
        json.dumps(stop_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"запись остановки: {cd / 'stopped.json'}")
    ok = f1.get("stage_status") in ("stopped", "done") or f1.get("ctr_running") == "no"
    return EXIT_OK if ok else EXIT_FAIL


def do_plan(args) -> int:
    ts = args.ts or now_ts()
    lr = float(args.peak_lr_scale)
    decision = LR_DECISIONS.get(round(lr, 6))
    shape = corpus_shape_check()
    print(f"план полного CPT (S3x, {decision['adr'] if decision else 'решение по LR не названо'}):")
    print(f"  каталог прогона:  {stand_dir(ts)}  (в контейнере {CTR_SHARED}/{run_dir_name(ts)})")
    print(f"  каталог в кейсе:  {case_dir(ts)}")
    print(f"  корпус:           v12r (75/25), {shape.get('shape')} → {shape['why']}")
    print(f"  шагов:            {STEPS} (источник: {STEPS_SOURCE})")
    print(f"  конфигурация:     replay {REPLAY_SHARE_PCT} %, peak_lr_scale {lr:g}"
          + (f" — {decision['arm']}" if decision else " — ВНЕ замороженного набора")
          + f", сид {SEED}, образ {args.image}")
    print(f"  точки замера:     каждые {CKPT_EVERY} шагов → {CKPT_POINTS} + checkpoint_final.pt")
    print("  замер языка:      K1 (general_eval_v3.txt) + K2 (general_eval_k2.txt), прибор "
          "tools/calib_ppl_probe.py на локальной машине (4080)")
    print("  замер домена:     domain_eval_v2.txt (ADR-031 п.4: × базы < 1 = домен выучен), "
          "тот же прибор и те же точки")
    print(f"  сессия:           tmux {run_dir_name(ts)} на {STAND} (setsid nohup)")
    print(f"  ожидаемое время:  ≈ {STEPS * 2.569 / 3600:.1f} ч (по цене шага ADR-008)")
    if not shape["ok"]:
        print(f"  ОТКАЗ: {shape['why']}")
        return EXIT_FAIL
    return EXIT_OK


def main(argv=None) -> int:
    global SHARED, STAND
    ap = argparse.ArgumentParser(
        description="S3x: полный CPT на миксе v12r (ADR-031, пик LR ×0.035)")
    ap.add_argument("--ts", default=None)
    ap.add_argument("--image", default=C.DEFAULT_IMAGE)
    #: Пик LR — параметр, а не только константа: конфигурации LR×0.35 (ADR-029) и
    #: LR×0.035 (ADR-031) обязаны воспроизводиться одним и тем же кодом, иначе
    #: сравнение конфигураций опиралось бы на разные раннеры.
    ap.add_argument("--peak-lr-scale", type=float, default=PEAK_LR_SCALE,
                    help=f"пик LR полного CPT (по умолчанию {PEAK_LR_SCALE:g} — ADR-031)")
    ap.add_argument("--sampler-seconds", type=int, default=32400)
    ap.add_argument("--stall-minutes", type=int, default=30)
    ap.add_argument("--wait", type=int, default=300)
    ap.add_argument("--force", action="store_true")
    #: Остановка живой стадии (ADR-016 п.2).
    ap.add_argument("--previous-ts", default=None,
                    help="ts остановленного прогона — для evidence переключения")
    ap.add_argument("--wait-point", action="store_true",
                    help="перед остановкой дождаться конца текущей точки замера")
    ap.add_argument("--wait-minutes", type=int, default=25,
                    help="предел ожидания точки замера перед остановкой")
    ap.add_argument("--stop-timeout", type=int, default=120,
                    help="таймаут docker stop (SIGTERM → таймаут → SIGKILL)")
    ap.add_argument("--stop-settle-seconds", type=int, default=180,
                    help="сколько ждать, чтобы цепочка записала статус стадии после остановки")
    ap.add_argument("--hash-checkpoints", action="store_true",
                    help="посчитать sha256 чекпойнтов в записи остановки (минуты чтения)")
    #: Стенд и корень сетевого диска — параметры, а не константы: проверка
    #: остановки обязана прогоняться на фикстуре, без живого стенда и без данных.
    ap.add_argument("--stand", default=S.DEFAULT_HOST)
    ap.add_argument("--shared", default=SHARED)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true")
    g.add_argument("--prepare", action="store_true")
    g.add_argument("--launch", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--evidence", action="store_true")
    g.add_argument("--stop", action="store_true", dest="do_stop")
    args = ap.parse_args(argv)
    SHARED = args.shared
    STAND = args.stand
    if args.plan:
        return do_plan(args)
    if args.prepare:
        return do_prepare(args)
    if args.launch:
        return do_launch(args)
    if args.verify:
        return do_verify(args)
    if args.evidence:
        return do_evidence(args)
    if args.do_stop:
        return do_stop(args)
    ap.print_help()
    return EXIT_NOT_VERIFIED


if __name__ == "__main__":
    sys.exit(main())
