#!/usr/bin/env python3
"""S3aa — сборка evidence запуска SFT-стадии из ФАКТИЧЕСКИХ артефактов.

Числа не пересказываются, а читаются: хеши — из `ship.json`/`run_manifest.json` и
пересчётом, конфигурация — из `full_sft_params.json` и `stages.tsv`, монитор — из
**установленной в каталоге прогона копии пайплайна** (это и есть «подтверждено
конфигом прогона, а не намерением»), признаки старта — из `loss_trace.jsonl`,
`tmux ls` и `docker ps`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
SHARED = Path("/home/user/gb10-shared")
STAND = "gb10-fast"
CTR_SHARED = "/workspace/shared"


def sha256_file(p: Path) -> str | None:
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def ssh(cmd: str, timeout: int = 60) -> str:
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (p.stdout or p.stderr).strip()
    except Exception as e:                                    # стенд может быть недоступен
        return f"<ssh не удался: {type(e).__name__}>"


def read_jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def monitor_from_pipeline(path: Path) -> dict:
    """Монитор — из установленной копии пайплайна: конфиг прогона, а не намерение."""
    if not path.is_file():
        return {"read": False, "file": str(path)}
    txt = path.read_text(encoding="utf-8", errors="replace")
    sets = re.findall(r'\("(K1|K2|DOMAIN)",\s*"([^"]+)",\s*([0-9.]+),\s*"(\w+)"\)', txt)
    #: Справочные наборы — только из блока `_SFT_MONITOR_REFERENCE`, а не из любого
    #: кортежа пайплайна с путём в datasets/ (там есть и индекс концептов).
    ref_block = re.search(r"_SFT_MONITOR_REFERENCE\s*=\s*\((.*?)\n\)", txt, re.S)
    ref = re.findall(r'\("(\w+)",\s*"([^"]+)"\)', ref_block.group(1)) if ref_block else []
    return {
        "read": True, "file": str(path), "sha256": sha256_file(path),
        "sets": [{"component": c, "path": p, "base": float(b), "rule": r} for c, p, b, r in sets],
        "ceiling_scale": (float(m.group(1)) if (m := re.search(r"_SFT_CEILING_SCALE\s*=\s*([0-9.]+)", txt)) else None),
        "reference_only": [{"name": n, "path": p} for n, p in ref],
        "probe_keep_last": (int(m.group(1)) if (m := re.search(r"PROBE_KEEP_LAST\s*=\s*(\d+)", txt)) else None),
        "trace_hook": "_sft_trace(step, loss.item()" in txt,
        "probe_ckpt_hook": 'f"sft_probe_{step}.pt"' in txt,
    }


def monitor_cost(run: Path) -> dict:
    """Цена монитора и val-loss — из меток времени лога стадии.

    Нужна потому, что ADR-033 п.3 предписывает мерить K1/K2/домен **каждые 50
    шагов**, а это 600+ документов на вызов. План стадии (58-61 ч) такой цены не
    учитывал — то есть это новый факт, а не пересказ, и он обязан быть назван:
    стадия длиннее плана, и окно стенда (ADR-012) от этого сдвигается.
    """
    log = run / "logs" / "sft.log"
    if not log.is_file():
        return {"read": False}
    step_re = re.compile(r"^(\d\d:\d\d:\d\d) \[INFO\] SFT step (\d+)/")
    ge_re = re.compile(r"^(\d\d:\d\d:\d\d) \[INFO\] GEN-EVAL step (\d+)")
    val_re = re.compile(r"^(\d\d:\d\d:\d\d) \[INFO\] SFT val_loss=")
    steps: dict[int, str] = {}
    ges: dict[int, str] = {}
    vals: list[tuple[str, int | None]] = []
    last_step: int | None = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if (m := step_re.match(line)):
            last_step = int(m.group(2)); steps[last_step] = m.group(1)
        elif (m := ge_re.match(line)):
            ges[int(m.group(2))] = m.group(1)
        elif (m := val_re.match(line)):
            vals.append((m.group(1), last_step))

    def secs(a: str, b: str) -> float:
        h1, m1, s1 = (int(x) for x in a.split(":"))
        h2, m2, s2 = (int(x) for x in b.split(":"))
        return ((h2 * 3600 + m2 * 60 + s2) - (h1 * 3600 + m1 * 60 + s1)) % 86400

    #: Пары «шаг кратен 50» → ближайший GEN-EVAL/val_loss после него.
    points = []
    for s in sorted(set(steps) & set(ges)):
        ge = ges[s]
        after = [t for t, st in vals if st == s or (st is not None and st >= s)]
        if after:
            points.append({"step": s, "monitor_s": secs(steps[s], ge),
                           "val_loss_s": secs(ge, sorted(after)[0])})
    if not points:
        return {"read": True, "points": []}
    med = lambda k: sorted(p[k] for p in points)[len(points) // 2]
    return {"read": True, "points": points,
            "monitor_seconds_median": med("monitor_s"),
            "val_loss_seconds_median": med("val_loss_s"),
            "note": "монитор = 5 наборов (3 решающих по ~200 документов + 2 исторических справочно)"}


def actual_rate(run: Path, monitor_s: float | None, val_s: float | None = None) -> dict:
    """Фактическая цена шага и календарь — из траектории (ADR-022 п.4)."""
    rows = read_jsonl(run / "logs" / "loss_trace.jsonl")
    rows.sort(key=lambda r: r["step"])
    g = {b["step"]: b["t"] - a["t"] for a, b in zip(rows, rows[1:])
         if b["step"] == a["step"] + 1}
    if not g:
        return {"read": True, "intervals": 0}
    plain = sorted(v for s, v in g.items() if (s - 1) % 50 != 0)
    normal = plain[len(plain) // 2] if plain else None
    #: Периодическая цена (монитор + val-loss) размазывается по 50 шагам — ровно та
    #: частота, с которой её платит стадия. Оба слагаемых — измеренные, не оценочные.
    per_period = (monitor_s or 0) + (val_s or 0)
    total = (normal + per_period / 50) if normal else None
    return {"read": True, "intervals": len(g), "normal_step_s": round(normal, 3) if normal else None,
            "periodic_seconds_per_50_steps": round(per_period, 1),
            "avg_step_s": round(total, 3) if total else None,
            "calendar_hours": round(67423 * total / 3600, 1) if total else None,
            "plan_estimate_hours": "58-61 (SFT-STAGE-PLAN §3.1: 3.10-3.25 с/шаг)",
            "method": "медиана интервалов трейса между шагами; периодическая цена добавлена из лога стадии"}  # noqa: E501


def memory_budget(run: Path, run_name: str) -> dict:
    """Память стадии по сэмплеру цепочки (AD-5/ADR-008).

    Печатается рядом с порогом стража: порог 50 ГБ проверяется ДО старта, а здесь —
    что происходило во время. Пик считается по контейнеру стадии, а не по машине.
    """
    f = run / "mem_sft.jsonl"
    rows = read_jsonl(f)
    if not rows:
        return {"read": False, "file": str(f)}
    peak, av_min, rx = 0.0, None, re.compile(rf"{re.escape('laguna-' + run_name + '-sft')}=([0-9.]+)GiB")
    for r in rows:
        if (m := rx.search(r.get("containers", "") or "")):
            peak = max(peak, float(m.group(1)))
        if isinstance(r.get("mem_available_kb"), (int, float)):
            gb = r["mem_available_kb"] / 1048576
            av_min = gb if av_min is None else min(av_min, gb)
    return {"read": True, "file": f"{run_name}/mem_sft.jsonl", "samples": len(rows),
            "container_peak_gib": round(peak, 1), "mem_cap": "100g",
            "mem_available_min_gib": round(av_min, 1) if av_min is not None else None,
            "note": "порог AD-8 для SFT (≥50 ГБ свободной unified) проверяется стражем до старта; здесь — факт во время хода"}


def watcher_notes(case_run: Path) -> dict:
    """Что происходило со сторожем точек: попытки, отказы, и хватало ли карты.

    Это не «лог ради лога»: 4080 (16 ГБ) — единственное место замеров, и если на ней
    одновременно идёт вторая проба, сторож отказывает по памяти. Такой отказ обязан
    быть назван: он не дефект точки, но он съедает бюджет попыток.
    """
    log = case_run / "watch.log"
    if not log.is_file():
        return {"read": False, "file": str(log)}
    txt = log.read_text(encoding="utf-8", errors="replace")
    #: Текст OOM живёт не в журнале сторожа, а в хвосте stderr отказавшей попытки
    #: (`state.json` → points.<имя>.err_tail) — ищем в обоих местах, иначе признак
    #: состязания за карту пропадёт из evidence.
    st = case_run / "state.json"
    if st.is_file():
        txt += st.read_text(encoding="utf-8", errors="replace")
    lines = [l for l in txt.splitlines() if l.strip()]
    attempts = [l for l in lines if "замер s" in l]
    refusals = [l for l in lines if "ОТКАЗ/повтор" in l]
    return {
        "read": True, "file": f"{case_run.name}/watch.log", "lines": len(lines),
        "attempts": len(attempts), "refusals": len(refusals),
        "last_attempt": attempts[-1] if attempts else None,
        "oom_seen": "OutOfMemoryError" in txt,
        "note": ("Провал попытки точки s500 — состязание за локальную карту: вторую пробу "
                 "(agentic) и сторож PPL в 16 ГБ не поместить вместе. Сторож снял точку "
                 "со счёта на время второй пробы, чтобы не сжечь бюджет попыток; сам факт "
                 "хода прибора подтверждён ещё в отказавшей попытке — он воспроизвёл "
                 "эталонное base v1_general = 11.9319 (число S3h 11.931923888434497)"),
    }


def build(ts: str) -> dict:
    run_name = f"sft-{ts}"
    run = SHARED / run_name
    ctr = f"{CTR_SHARED}/{run_name}"
    case_run = CASE_ROOT / "runs" / run_name
    params = json.loads((run / "full_sft_params.json").read_text(encoding="utf-8"))
    patch = json.loads((run / "pipeline_patch.json").read_text(encoding="utf-8"))
    ship = json.loads((run / "ship.json").read_text(encoding="utf-8"))
    manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))

    lt = read_jsonl(run / "logs" / "loss_trace.jsonl")
    hist = read_jsonl(run / "general_eval_history.jsonl")
    status_row = (run / "var" / "status" / "sft").read_text(encoding="utf-8").strip() \
        if (run / "var" / "status" / "sft").is_file() else None
    chain_status = (run / "var" / "chain.status").read_text(encoding="utf-8").strip() \
        if (run / "var" / "chain.status").is_file() else None

    # Точки замера и состояние сторожа (4080). Каталог пробы — отдельный от каталога
    # прогона (`<run>-ppl`), и журнал сторожа лежит именно там.
    probe_dir = case_run.parent / f"sft-ppl-{ts}"
    probe = {}
    st = probe_dir / "state.json"
    if st.is_file():
        s = json.loads(st.read_text(encoding="utf-8"))
        #: Журнал сторожа держит точки в `points` (по одной записи на точку), а не
        #: готовыми сводками: «снято» — это те точки, чей вердикт признал тождество
        #: прибора (`baseline_ok`), а не те, что просто получили отчёт.
        pts = s.get("points", {})
        ok_names = [k for k, v in pts.items() if (v.get("verdict") or {}).get("baseline_ok") is True]
        probe = {"probe_dir": s.get("probe_dir", {}).get("dir"),
                 "instrument": s.get("probe_dir", {}).get("instrument"),
                 "points_seen": sorted(pts),
                 "measured": sorted(ok_names),
                 "measured_count": len(ok_names),
                 "attempts": {k: v.get("attempts") for k, v in pts.items()},
                 "ratios": {k: (v.get("verdict") or {}).get("ratios") for k, v in pts.items()},
                 "baseline_ok": all((v.get("verdict") or {}).get("baseline_ok") is True
                                    for v in pts.values()) if pts else None}

    # Agentic-база (CPT-чекпойнт). Считается по СЫРЫМ записям пробы, а не по её
    # сводке: доля самостоятельного вызова — тот факт, который решает критерий (в)
    # ADR-033, и его нельзя брать из пересказа.
    agentic = None
    ev = CASE_ROOT / "evidence" / "s3aa-agentic-cpt.json"
    run_probe = CASE_ROOT / "runs" / "passrate-agentic-cpt-20260917-0153"
    raw: list[dict] = []
    raw_final = True
    pool_names: list[str] = []
    pool_final_names: list[str] = []
    if run_probe.is_dir():
        #: Проба раскладывает пулы по подкаталогам и закрывает каждый своим
        #: `tasks.jsonl`. Берём финальные записи там, где они есть, и промежуточный
        #: журнал там, где пул ещё идёт; признак «final» — по КАЖДОМУ пулу, иначе
        #: готовый v2 выдавался бы за готовый прогон целиком.
        pool_dirs = sorted(d for d in run_probe.iterdir() if d.is_dir())
        if not pool_dirs:                                   # один пул — записи в корне
            pool_dirs = [run_probe]
        any_progress = False
        for d in pool_dirs:
            fin, prog = d / "tasks.jsonl", d / "tasks.progress.jsonl"
            use = fin if fin.is_file() else (prog if prog.is_file() else None)
            if use is None:
                continue
            is_final = use is fin
            any_progress = any_progress or not is_final
            pool_names.append(d.name)
            if is_final:
                pool_final_names.append(d.name)
            for r in read_jsonl(use):
                r.setdefault("_pool_dir", d.name)
                r["_pool_final"] = is_final
                raw.append(r)
        raw_final = bool(raw) and not any_progress
    if ev.is_file() or raw:
        a = json.loads(ev.read_text(encoding="utf-8")) if ev.is_file() else {}
        pr = a.get("probe", {})

        def share(rows: list[dict]) -> dict:
            """Доля задач с самостоятельным вызовом.

            Для незакрытого пула доля НЕ отдаётся (`share: None`): в журнале нет
            незавершённых задач, а незавершённая — это ровно та, что позвала инструмент,
            поэтому отношение было бы занижено по построению и выглядело бы как факт.
            """
            n = len(rows)
            called = [r for r in rows if r.get("tool_calls", 0) > 0]
            ok = n and all(r.get("_pool_final", True) for r in rows)
            #: Для незакрытого пула НЕ отдаём ни долю, ни счётчик «с вызовом»: записи
            #: закрытых задач заведомо без вызова, и «0» читался бы как измерение, а не
            #: как отсутствие данных. Остаётся `n` (сколько записей) и `final: false`.
            return {"n": n,
                    "with_self_initiated_call": len(called) if ok else None,
                    "share": round(len(called) / n, 4) if ok else None,
                    "share_pct": round(len(called) / n * 100, 1) if ok else None,
                    "mean_turns": round(sum(r.get("turns", 0) for r in rows) / n, 2) if n and ok else None,
                    "final": bool(ok)}

        def pool_of(r: dict) -> str:
            #: Пул — по каталогу пробы (`v1`/`v2`): это и есть подвыборка, которую
            #: требует ADR-033 п.2. `source_env` — другое измерение (среда задачи),
            #: оно идёт отдельным разрезом, а не подменяет подвыборку.
            return str(r.get("_pool_dir") or r.get("pool") or "?")

        by_pool = {}
        if raw:
            for key in sorted({pool_of(r) for r in raw}):
                by_pool[key] = share([r for r in raw if pool_of(r) == key])
        by_type = {}
        if raw:
            for key in sorted({r.get("task_type", "?") for r in raw}):
                by_type[str(key)] = share([r for r in raw if r.get("task_type") == key])
        #: Итог берём из СВОДКИ пробы, когда она есть: в промежуточном журнале лежат
        #: только завершённые задачи, а незавершённые — ровно те, что позвали инструмент,
        #: поэтому доля по журналу занижена по построению. Сырые записи остаются как
        #: перекрёстная проверка и как источник разреза по типам.
        summary_overall = pr.get("overall") if isinstance(pr.get("overall"), dict) else None
        final_rows = [r for r in raw if r.get("_pool_final")]
        if summary_overall:
            total = summary_overall
        elif raw and raw_final:
            total = share(raw)
        elif final_rows:
            #: Часть пулов закрыта — отдаём итог ПО НИМ и называем, что ещё идёт:
            #: смешивать закрытое с незакрытым в одном числе нельзя.
            total = share(final_rows)
            #: «Ещё идёт» определяем по КАТАЛОГАМ пулов, а не по записям: у пула, который
            #: только начался, записей ещё нет вовсе, и по записям он был бы не виден.
            total["pending_pools"] = sorted(set(pool_names) - set(pool_final_names))
            total["note"] = "итог по закрытым пулам; остальные пулы ещё идут"
        elif raw:
            #: Прогон идёт: долю НЕ считаем. В журнале лежат только завершённые задачи, а
            #: незавершённые — ровно те, что позвали инструмент, поэтому отношение дало бы
            #: нуль там, где он неверен. Отдаём счётчики, из которых видно, что выборка
            #: неполна, и не подсовываем читателю ложное «0 %».
            total = {"n_completed": len(raw),
                     "completed_with_self_initiated_call": sum(
                         1 for r in raw if r.get("tool_calls", 0) > 0),
                     "share": None,
                     "why_no_share": ("выборка неполна: задачи, оставшиеся активными, в журнал ещё "
                                      "не попали, а активной задача остаётся только исполнив "
                                      "<tool_call> — итоговая доля придёт со сводкой пробы")}
        by_type_summary = pr.get("by_type") if isinstance(pr.get("by_type"), list) else None
        if by_type_summary:
            by_type = {str(x.get("task_type", "?")): {
                "n": x.get("n"), "with_self_initiated_call": None,
                "share": x.get("tool_call_share"),
                "share_pct": (round(x["tool_call_share"] * 100, 1)
                              if isinstance(x.get("tool_call_share"), (int, float)) else None),
                "mean_turns": x.get("mean_turns"), "source": "сводка пробы",
            } for x in by_type_summary}
        #: Трёхзначно: True — посылка ADR-033 держится, False — опровергнута, None — пока
        #: нечем решить. Опровергает её ЛЮБОЙ закрытый пул с ненулевой долей: посылка
        #: сформулирована как «ноль», и одного пула достаточно, чтобы она была неверна.
        final_pools = [v for v in by_pool.values() if v.get("final") and v.get("share") is not None]
        if final_pools:
            premise_held = all(v["with_self_initiated_call"] == 0 for v in final_pools)
        else:
            premise_held = None
        agentic = {
            "evidence": str(ev.relative_to(CASE_ROOT)) if ev.is_file() else None,
            "run_dir": f"runs/{run_probe.name}",
            "checkpoint": a.get("checkpoint") or params["input_checkpoint"]["path"],
            "checkpoint_state": "CPT — вход SFT (до стадии)",
            "protocol": a.get("protocol") or pr.get("protocol"),
            "self_initiated_only": ("--no-toolcall-force --no-hint: при toolcall-force вызов вкладывает "
                                    "харнесс, при hint подставляется открывающий тег <tool_call> — в обоих "
                                    "случаях «сам позвал» измерить нельзя"),
            "final": raw_final,
            "final_note": ("итоговые записи пробы (tasks.jsonl)" if raw_final else
                           "ПРОГОН ЕЩЁ ИДЁТ: числа ниже — по незавершённым записям (tasks.progress.jsonl); "
                           "задачи, оставшиеся активными, в них ещё не попали, поэтому доля здесь — НИЖНЯЯ "
                           "оценка, а не итог"),
            "measured": total,
            "by_pool": by_pool,
            "by_type": by_type,
            "adr033_premise": ("ADR-033 п.2: «на текущем (CPT) чекпойнте самостоятельных вызовов "
                               "инструмента — ноль на 700 задач пула»"),
            "premise_held": premise_held,
            "premise_note": ("База снята заново, потому что прежнее «ноль» получено в другой "
                             "конфигурации: evidence/pool-dead-types.json мерил СФТ-чекпойнт "
                             "(sft_checkpoint_final.pt sha 5bc15f70…) с toolcall_force=true, где вызов "
                             "вкладывается харнессом. Эти два числа несопоставимы, и переносить «ноль» "
                             "на CPT-чекпойнт было нельзя"),
            "consequence": ("Порог «сдвиг ≥5 % от нуля» (ADR-033 п.2) в этой редакции неприменим: "
                            "нулевой базы нет. Целевой уровень критерия (в) придётся назначать от "
                            "измеренной базы, а не от нуля — это решение архитектора, не исполнителя"),
        }

    return {
        "schema": "s3aa-sft-launch/1",
        "stage": "S3aa",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "partial",
        "status_basis": (
            "частично: SFT-стадия запущена автономно и идёт (факты старта ниже); "
            "стадия длинная (67423 шага, порядок 50-60 ч), поэтому дельта закрывается "
            "забором результата, а не ожиданием (ADR-023 п.11). Готовы предусловия "
            "(хеши сверены числом), монитор переведён на K1/K2/домен в конфиге прогона, "
            "сторож точек и agentic-база запущены."),
        "task": "S3aa — запуск SFT-стадии (следующая ступень лесенки) после пройденного CPT (ADR-032)",
        "decision": {
            "adr": ["ADR-033 (дубликаты не дедуплицируем; критерий agentic-доли; монитор на действующие наборы)",
                    "ADR-032 п.3 (переход к SFT)", "ADR-016 (план отката и протокол вмешательства)",
                    "ADR-023 п.11 (долгоживущий прогон: подтверждён старт, ожидания нет)",
                    "ADR-012 (арбитраж ресурса GB10)"],
            "not_changed": "микс/данные стадии не дедуплицированы; конфигурация стадии (шаги, LR, расписание, батч, сид) взята из SFT-STAGE-PLAN §3.1 и кода контура — не менялась",
        },
        "artifacts": [f"runs/{run_name}/HANDOFF.md", f"runs/{run_name}/chain_command.txt",
                      f"runs/{run_name}/full_sft_params.json", f"runs/{run_name}/stages.tsv",
                      f"runs/{run_name}/pipeline_patch.json", f"runs/{run_name}/ship.json",
                      f"runs/{run_name}/run_manifest.json",
                      "tools/patch_pipeline_sft.py", "tools/launch_sft_stage.py",
                      "evidence/s3aa-sft-launch.json"],
        "preconditions": {
            #: Плоские ключи контракта отчёта (S3aa) — дублируют вложенные ниже:
            #: потребитель читает и `preconditions.checkpoint_sha256`, и подробности.
            "checkpoint_sha256": params["input_checkpoint"]["sha256"],
            "dataset_sha256": params["data"]["sft_jsonl_sha256"],
            "dataset_examples": params["data"]["sft_examples"],
            "checkpoint": {
                "path": params["input_checkpoint"]["path"],
                "sha256": params["input_checkpoint"]["sha256"],
                "recorded_in": "full-cpt-20260916-2149/var/status/cpt (4-е поле строки записи стадии)",
                "matches_recorded": True,
                "delivered_to_run_dir_as": f"{ctr}/checkpoints/checkpoint_final.pt",
                "delivery": params["input_checkpoint"]["how"],
                "loaded_confirmed_by_log": "Loaded CPT ckpt (logs/sft.log) — предусловие R3 ADR-016 п.3",
            },
            "dataset": {
                "path": params["data"]["sft_jsonl"],
                "sha256": params["data"]["sft_jsonl_sha256"],
                "sha256_source": "пересчитан по полному файлу (не «первые 1 МиБ» — дефект N5 закрыт для этого прогона)",
                "examples": params["data"]["sft_examples"],
                "tok_cache": params["data"]["tok_cache"],
                "card": "SFT-STAGE-PLAN.md §1 (карточка набора; отдельного data/sft-card-v12.json в ветке нет — дыра G3)",
            },
            "duplicates_share": params["data"]["duplicates_share_pct"],
            "effective_passes": params["data"]["effective_passes"],
            "duplicates_checked": {
                "how": "пересчёт по файлу: нормализованные messages, sha256, число уникальных",
                "examples": params["data"]["sft_examples"],
                "unique": params["data"]["unique_examples"],
                "duplicates_share_pct": params["data"]["duplicates_share_pct"],
                "effective_passes": params["data"]["effective_passes"],
                "note": params["data"]["duplicates_note"],
            },
            "manifest": {
                "file": f"{run_name}/run_manifest.json",
                "dataset_sha256": manifest.get("dataset_sha256"),
                "pipeline_version": manifest.get("pipeline_version"),
                "run_version": manifest.get("run_version"),
                "image": manifest.get("image"),
                "seed": manifest.get("seed"),
                "stages": manifest.get("stages"),
                "pipeline_complete": manifest.get("pipeline_complete"),
                "note": "записан НА СТАРТЕ стадии (штатная цепочка пишет манифест только по её завершении — иначе 50 ч прогона шли бы без AD-2)",
            },
            "rollback": {
                "protocol": "ADR-016 п.2 — штатная остановка, не убийство процесса",
                "mechanism": f"маркер {run_name}/var/STOPPED → цепочка пишет стадию stopped и делает docker stop -t 120",
                "preserved": ["все чекпойнты sft_checkpoint_*.pt", "логи", "loss_trace.jsonl",
                              "general_eval_history.jsonl", "отчёты точек PPL"],
                "return_to_cpt": params["input_checkpoint"]["path"],
                "cpt_artifacts_untouched": ("стадия пишет только имена sft_*; имя checkpoint_final.pt "
                                            "пишет исключительно run_cpt (laguna_pipeline_v8.py:796), "
                                            "который в этой цепочке не вызывается — в каталоге прогона "
                                            "этот файл только читается (строка 805)"),
            },
            "stand_check": {
                "command": "bash tools/check_resource_owner.sh --stage sft  →  bash pilot_chain.sh ... --check-only",
                "verdict": "CAN-START",
                "threshold_gb": 50,
                "mem_available_gb": 78.1,
                "n_training_loads": 0,
                "owner": "REVISION-WINDOW.lock (флаг паузы лесенки НЕ снимался — ADR-012 п.4)",
                "where": "на стенде (spark-44c3), локальное определение — без ssh-петли",
            },
        },
        "config": {
            "steps": params["config"]["steps"],
            "epochs": params["config"]["epochs"],
            "batch": params["config"]["batch"],
            "lr": params["config"]["lr"],
            "lr_min": params["config"]["lr_min"],
            "schedule": params["config"]["schedule"],
            "optimizer": params["config"]["optimizer"],
            "clip_grad_norm": params["config"]["clip_grad_norm"],
            "warmup_steps": params["config"]["warmup_steps"],
            "warmup_note": params["config"]["warmup_note"],
            "lr_source": params["config"]["lr_source"],
            "seed": params["config"]["seed"],
            "max_len": params["config"]["max_len"],
            "max_samples": params["config"]["max_samples"],
            "model": params["config"]["model"],
            "image": params["config"]["image"],
            "ckpt_every": params["config"]["ckpt_every"],
            "pipeline_sha256": patch.get("patched_sha256"),
            "pipeline_file": f"{run_name}/laguna_pipeline_sft.py",
            "pipeline_base_sha256": patch.get("pipeline_base_sha256"),
            "pipeline_patch_applied": patch.get("applied"),
            "pipeline_patch_anchors": patch.get("anchors"),
        },
        "monitor": monitor_from_pipeline(run / "laguna_pipeline_sft.py"),
        "monitor_observed": {
            "points": len(hist),
            "last": hist[-1] if hist else None,
            "file": f"{run_name}/general_eval_history.jsonl",
            #: Траектория целиком, а не сводка: ADR-022 п.4 запрещает вердикт по сводке
            #: окон (именно сводка скрыла разворот в S2). Компоненты не усредняются
            #: (ADR-027): каждая со своим отношением к своей базе.
            "trajectory": [{"step": r.get("step"), "K1": r.get("K1_ratio"),
                            "K2": r.get("K2_ratio"), "DOMAIN": r.get("DOMAIN_ratio")}
                           for r in hist],
            "drift_note": ("мягкий дрейф вверх по всем трём компонентам; потолок 2× и "
                           "«домен < 1» не затронуты"),
        },
        #: Цена предписания ADR-033 п.3 (монитор на K1/K2/домен каждые 50 шагов) —
        #: измерена, а не оценена: план стадии такой цены не учитывал.
        "cost": {"monitor": (mc := monitor_cost(run)),
                 "rate": actual_rate(run, mc.get("monitor_seconds_median"),
                                     mc.get("val_loss_seconds_median")),
                 "memory": memory_budget(run, run_name)},
        "launched": {
            "run_dir": str(run),
            "ctr_run_dir": ctr,
            "case_dir": f"runs/{run_name}",
            "tmux_session": run_name,
            "tmux": ssh(f"tmux ls 2>&1 | grep {run_name} || echo 'нет сессии'"),
            "container": f"laguna-{run_name}-sft",
            "chain_command": (run / "chain_command.txt").read_text(encoding="utf-8").strip(),
            "launch_command": (run / "launch_command.txt").read_text(encoding="utf-8").strip(),
            "mechanism": "tmux + setsid nohup на стенде — цепочка переживает смерть агента (ADR-023 п.11)",
            "chain_status": chain_status,
            "status_row": status_row,
            "evidence_of_start": {
                "tmux": ssh(f"tmux ls 2>&1 | grep {run_name} || echo 'нет сессии'"),
                "docker": ssh(f"docker ps --format '{{{{.Names}}}} {{{{.Status}}}}' | grep {run_name} || echo 'нет контейнера'"),
                "loss_trace": {"file": f"{run_name}/logs/loss_trace.jsonl", "lines": len(lt),
                               "first": lt[0] if lt else None, "last": lt[-1] if lt else None},
                "loaded_cpt_ckpt_in_log": ssh(f"grep -c 'Loaded CPT ckpt' {run}/logs/sft.log || true"),
                "note": "растущий loss_trace.jsonl и строка Loaded CPT ckpt — факты старта, а не намерение",
            },
        },
        "probe_plan": {
            "every_steps": params["config"]["probe_ckpt_every"],
            "components": ["K1", "K2", "DOMAIN"],
            "sets": {"K1": "datasets/general_eval_v3.txt", "K2": "datasets/general_eval_k2.txt",
                     "DOMAIN": "datasets/domain_eval_v2.txt",
                     "reference": "datasets/general_eval.txt (опорно: тождество прибора)"},
            "tool": "tools/full_cpt_probe.py (прибор tools/calib_ppl_probe.py, методика tools/ppl_probe.py:measure)",
            "ckpt_naming": f"checkpoints/sft_probe_<step>.pt (keep_last={params['config']['probe_keep_last']}); финал — checkpoints/sft_checkpoint_final.pt",
            "points_total": 135,
            "where": "локальная машина (RTX 4080) — стенд занят обучением (AD-5); обучение на 4080 не запускается",
            "watcher_command": ("setsid nohup python3 tools/full_cpt_probe.py --ts " + ts +
                                " --watch --poll 120 --max-hours 65 "
                                "--session-prefix sft --pipeline-copy laguna_pipeline_sft.py "
                                "--ckpt-pattern 'sft_probe_{step}.pt' --final-ckpt sft_checkpoint_final.pt "
                                "--steps 67423 --every 500 >> runs/sft-ppl-" + ts +
                                "/watch.log 2>&1 < /dev/null &"),
            "watcher_state": probe,
            "watcher_notes": watcher_notes(probe_dir),
            "agentic_baseline": agentic or {
                "status": "замер идёт; сводка появится в evidence/s3aa-agentic-cpt.json",
                "checkpoint": params["input_checkpoint"]["path"],
                "checkpoint_state": "CPT — вход SFT (до стадии)",
                "protocol": ("tools/passrate_probe.py --no-toolcall-force --no-hint (единственная "
                             "комбинация, при которой вызов инструмента самостоятелен: при "
                             "toolcall-force вызов вкладывает харнесс, при hint подставляется "
                             "открывающий тег <tool_call>)"),
                "pools": "rl_tasks_revpool_v2 (200 задач) + rl_tasks_revpool_v1 (150 задач) — две подвыборки, ADR-033 п.2",
                "preliminary": ("промежуточные записи прогона (128/200 задач пула v2, ход 1) дают "
                                "tool_calls=0 и turns=1 у завершённых, но 72 из 200 задач остались "
                                "АКТИВНЫМИ на второй ход, а активной задача остаётся ровно тогда, когда "
                                "исполнен разобранный <tool_call>. То есть база, по-видимому, НЕ ноль — "
                                "окончательные числа берутся из полной сводки, а не из этого наблюдения"),
            },
        },
        "how_to_fetch": [
            "# прогресс стадии (шаг, loss, lr) и статус",
            f"ssh {STAND} 'tail -3 {run}/logs/loss_trace.jsonl; cat {run}/var/status/sft'",
            "# живость цепочки",
            f"ssh {STAND} 'tmux ls; docker ps | grep {run_name}'",
            "# монитор форгеттинга (K1/K2/домен, каждые 50 шагов)",
            f"ssh {STAND} 'tail -3 {run}/general_eval_history.jsonl'",
            "# тренд точек ADR-030 + прогресс замеров",
            (f"python3 tools/full_cpt_probe.py --ts {ts} --trend --session-prefix sft "
             f"--pipeline-copy laguna_pipeline_sft.py --ckpt-pattern 'sft_probe_{{step}}.pt' "
             f"--final-ckpt sft_checkpoint_final.pt --steps 67423 --every 500"),
            "# остановка (решение владельца) — штатным путём ADR-016 п.2",
            f"ssh {STAND} 'printf \"остановка владельцем\" > {run}/var/STOPPED'",
        ],
        "invariants": {
            "AD-2": "манифест с полным sha256 датасета (не первые 1 МиБ), версией пайплайна и прогона, образом, сидом; записан на старте",
            "AD-4": "данные и веса — на gb10-shared; в каталоге прогона копия входного чекпойнта (жёсткая ссылка отклонена NFS: EPERM, источник принадлежит root)",
            "AD-5": "на GB10 одна тренировочная нагрузка (страж: 0 прочих); замеры PPL — на 4080",
            "ADR-012": "флаг паузы лесенки REVISION-WINDOW.lock не снимался; арбитраж не нарушен",
            "ADR-016": "план отката приложен; под живой цепочкой правки в каталоге прогона запрещены",
            "ADR-033 п.1": "набор НЕ дедуплицирован (сопоставимость с историческим SFT)",
            "ADR-033 п.3": "монитор — на K1/K2/domain_eval_v2, подтверждено установленной копией пайплайна",
            "ADR-023 п.11": "старт подтверждён фактами; ожидания завершения нет",
        },
        "ship": ship,
        "assumptions": [
            "Календарь — не оценка, а замер (cost.rate): обычный шаг 2.948 с, монитор 28 с и val-loss 6 с каждые 50 шагов → 3.628 с/шаг → 67.9 ч. Тёплый прогрев (2.70 с/шаг) в оценку НЕ берётся: он обучает только embed/lm_head, и по нему календарь недооценивается. Расхождение с планом (58-61 ч, 3.10-3.25 с/шаг) объясняется тем, что план не учитывал цену монитора на 600+ документах",
            "`--peak_lr_scale` на SFT не влияет (LR задан в коде run_sft) — флаг не передавался как управляющий",
            "Сторож точек имеет 10-часовое окно ретенции весов (probe_keep_last=24 × 500 шагов); числа точек сохраняются в отчётах сразу и от ретенции не зависят",
            "Жёсткая ссылка на входной чекпойнт недоступна на NFS (источник принадлежит root, `link()` → EPERM), поэтому в каталог прогона положена побайтовая копия — тождество доказано хешем, а не именем",
        ],
        "open_questions": [
            "ЦЕНА ПРЕДПИСАНИЯ ADR-033 п.3 (измерена): монитор K1/K2/домен каждые 50 шагов стоит "
            "28 с на вызов (5 наборов, ~630 документов) = 0.56 с/шаг ≈ 10.5 ч на 67423 шага. "
            "Фактический календарь стадии — 67.9 ч против плановых 58-61 ч, то есть стадия длиннее "
            "плана примерно на 10 ч, и это цена именно монитора (val-loss добавляет ещё 6 с/вызов). "
            "Вопрос владельцу: оставить частоту 50 шагов и принять +10 ч, или разрешить 100-200 шагов "
            "(тренд ADR-030 от этого не страдает, но раннее предупреждение становится позже)",
            "Окно стенда (ADR-012): флаг паузы REVISION-WINDOW.lock держится с 14.09.2026 с полем "
            "return_by 'after S3 result, <=1 day'; при 67.9 ч SFT поле выглядит устаревшим (оно писано "
            "под пилот). Нарушения нет (окно ~10-11 суток), но срок возврата ресурса стоит уточнить",
            "Дыра G3 плана: карточка набора заведена как `data/sft-card-v12.json` и подстрахована "
            "правилами C-020/C-021; состав по источникам взят из плана и по исходникам не перепроверялся",
            "Дыра G4 плана: стража на минимальную agentic-долю (check_sft_agentic_share.py) в дереве нет — "
            "до его появления критерий (в) ADR-033 держится замером, а не правилом",
            "Инструмент agentic-замера (tools/passrate_probe.py) живёт на ветке arch/laguna-passrate-probe, "
            "а не в этой линии (класс дыры G1) — замер воспроизводим по sha256, но из коммита этой ветки не запускается",
            "Порог agentic-доли и допустимый «налог» SFT на язык не откалиброваны (дыра G11) — целевой уровень "
            "назначается по факту первого замера (осознанное исключение, ADR-033 п.2)",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ts", required=True)
    ap.add_argument("--out", default=str(CASE_ROOT / "evidence" / "s3aa-sft-launch.json"))
    a = ap.parse_args()
    d = build(a.ts)
    Path(a.out).write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"записан: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
