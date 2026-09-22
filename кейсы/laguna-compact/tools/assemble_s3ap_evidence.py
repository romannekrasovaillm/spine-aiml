#!/usr/bin/env python3
"""S3ap — свод: сработала ли нормализация данных (метрики формата на точках SFT v13).

**Вопрос дельты.** Стадия SFT перезапущена на исправленном наборе (ADR-042: незакрытые
`<think>` 7 580 → 0, `tool_call` внутри рассуждения 5 053 → 0, примеры без ответа
4 794 → 0). Нормализация — гипотеза, и проверяется она **числами**: воспроизводятся
ли три дефекта формата моделью после того, как их убрали из данных. Если да — дефект
данных не был единственной причиной, и это надо назвать прямо, а не ждать 66 157 шагов.

**Что сводится и откуда.**

1. **Замер** — `runs/s3ap-format-monitor-20260919/format_wide_standard.json`: прибор
   `probe_language_split.py` в **штатном режиме** (запрет повторов 4-грамм — умолчание,
   ADR-041; `--legacy-decoding` не передавался), набор `wide` (104 пробы на состояние —
   требование «n ≥ 100»), бюджет 4096 и остановка на конце хода (S3aj). Состояния: CPT-финал
   и три точки v13 (500 / 5000 / свежая) — **одним процессом**, чтобы разница состояний не
   несла дрейфа окружения.
2. **Эталон CPT** — normative-числа берутся из уже снятого замера
   `runs/s3al-decoding-protocol-20260918/language_wide_standard.json`
   (`states.cfinal.aggregate`), то есть **не воспроизводятся заново** (критерий приёмки).
   Числа эталона кладутся в отчёт вместе с путём-источником и хешем прибора из того отчёта.
   Дополнительно: CPT-финал снят и в новом прогоне — как **проверка воспроизводимости**
   эталона, а не как его замена; вердикт по формату строится на сравнении с CPT **в одном
   процессе** (тот же прибор, те же флаги, тот же набор промптов).
3. **Тождество прибора** — двумя независимыми свидетельствами: (а) хеш файла прибора равен
   записанному в отчёте-эталоне, (б) ядро из 5 промптов (протокол S3ab) в прежнем режиме
   побайтово совпало с замером S3ai (`runs/s3ai-probes-20260917/identity_core_cfinal.json`).

**Правило значимости объявлено до чтения чисел.** При n = 104 стандартная ошибка доли
худшего случая (p = 0.5) ≈ 4.9 п.п.; порог «значимо» — |Δ| ≥ 0.10 (≈ 2σ), меньшие
различия называются шумом и не превращаются в «улучшилось/ухудшилось». Дефектный SFT
(набор v12) служит второй линией отсчёта: его числа (0.5673 незакрытых, 0.5673 `tool_call`,
0.5288 покрытия) взяты из того же прибора и записаны здесь как исторические, с источником.

**Три вопроса вердикта** (формулировка TASK): (а) ушли ли обрывы рассуждений, (б) вернулась
ли агентность, (в) выросло ли покрытие ответа. По каждому — дельта к CPT, направление,
значимость; плюс тренд по шагам (500 → 5000 → свежая точка), потому что «ещё не обучилось»
и «обучается не туда» — разные диагнозы, и рекомендация по стадии зависит от того, какой
из них верен.

Коды возврата::

    0 — свод собран
    1 — отказ: вход противоречит контракту (штатный режим не подтверждён, n < 100, набор
        промптов не тот, тождество прибора нарушено, состояния не сходятся)
    2 — NOT-VERIFIED: нечего сводить (нет отчёта прогона)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))

import probe_language_split as LS              # noqa: E402  (порог петель — тот же)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

RUN = "runs/s3ap-format-monitor-20260919"
REPORT = RUN + "/format_wide_standard.json"
IDENTITY_NEW = RUN + "/identity_core_cfinal.json"
IDENTITY_REF = "runs/s3ai-probes-20260917/identity_core_cfinal.json"
BASELINE_REPORT = "runs/s3al-decoding-protocol-20260918/language_wide_standard.json"
DEFECT_REPORT = BASELINE_REPORT      # дефектные состояния сняты тем же прибором там же
EVIDENCE = "evidence/s3ap-format-monitor.json"

#: Порог значимости, объявленный ДО чтения чисел (см. шапку): 2σ доли при n = 104.
SIGNIFICANT = 0.10

#: Минимум проб на состояние (критерий приёмки дельты).
MIN_N = 100

#: Ключевые метрики формата: имя в своде → путь в aggregate прибора. Один список на
#: свод, чтобы «таблица состояний» и «дельта» не разъехались по разным метрикам.
METRIC_PATHS = {
    "unclosed_think": ("unclosed_think_share",),
    "tool_call": ("mode_share_tool_call",),
    "answer_coverage": ("cyr_answer", "coverage"),
    "truncated": ("truncated_share",),
    "looped": ("looped_share",),
    "median_len": ("lengths", "median"),
    "cyr_think": ("cyr_think", "with_zeros"),
    "cyr_answer": ("cyr_answer", "with_zeros"),
    "stray_think_close": ("stray_think_close_share",),
    "has_think": ("mode_share_think",),
}

#: Три вопроса вердикта: имя → метрика свода, которой он отвечает.
QUESTIONS = {
    "obryvy": ("unclosed_think", "обрывы рассуждений (незакрытые блоки <think>)"),
    "agentnost": ("tool_call", "агентность (доля генераций с <tool_call>)"),
    "pokrytie": ("answer_coverage", "покрытие ответа (доля генераций с ответной частью)"),
}


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load(p: Path) -> dict | None:
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


def dig(d: dict, path: tuple[str, ...], default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def nd(x, k: int = 4):
    return None if x is None else round(float(x), k)


def step_of(tag: str, ckpt: str | None) -> int | None:
    """Шаг состояния из метки/имени файла: `sft_v13_0500` / `sft_probe_500.pt` → 500."""
    for src in (tag, Path(ckpt).stem if ckpt else ""):
        if not src:
            continue
        tail = src.rsplit("_", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


# ─────────────────────────── чтение состояний ─────────────────────────────────

def state_metrics(agg: dict) -> dict:
    out = {name: nd(dig(agg, path)) for name, path in METRIC_PATHS.items()}
    out["n"] = agg.get("n")
    # Парность маркеров: доля генераций без нарушенной пары (незакрытый блок ИЛИ
    # лишний `</think>` без открытия). Два разных дефекта — один вопрос «маркеры
    # парны», и он не сводится к `unclosed_think_share` (тот видит только первый).
    unclosed = nd(agg.get("unclosed_think_share"))
    stray = nd(agg.get("stray_think_close_share"))
    out["marker_pairing_ok"] = None if unclosed is None or stray is None else \
        nd(1.0 - unclosed - stray)
    out["think_blocks_mean"] = nd(agg.get("think_blocks_mean"))
    out["stop_reasons_share"] = dig(agg, ("stop", "reasons_share"))
    out["natural_stop_share"] = nd(agg.get("stop", {}).get("natural_stop_share")
                                   if isinstance(agg.get("stop"), dict) else None)
    return out


def collect_states(rep: dict) -> list[dict]:
    rows = []
    for tag, st in rep.get("states", {}).items():
        agg = st.get("aggregate") or {}
        m = state_metrics(agg)
        m.update({
            "state": tag,
            "step": step_of(tag, st.get("checkpoint")),
            "checkpoint": st.get("checkpoint"),
            "checkpoint_sha256": st.get("checkpoint_sha256"),
            "checkpoint_bytes": st.get("checkpoint_bytes"),
        })
        rows.append(m)
    # База — первой (шаг неизвестен), точки — по возрастанию шага: таблица читается
    # как «откуда пришли → куда идём», и «эталон сверху» не надо додумывать.
    rows.sort(key=lambda r: (0 if r["step"] is None else 1, r["step"] or 0))
    return rows


# ───────────────────────────── вердикт ────────────────────────────────────────

#: Метрики, у которых «меньше — лучше». Список один на свод: без него «дельта +0.36»
#: читалась бы как улучшение там, где это рост обрывов (и наоборот).
LOWER_IS_BETTER = ("unclosed_think", "truncated", "looped", "stray_think_close")


def delta_row(base: float | None, cur: float | None, metric: str = "") -> dict:
    if base is None or cur is None:
        return {"delta": None, "direction": None, "significant": None}
    d = round(float(cur) - float(base), 4)
    if abs(d) < SIGNIFICANT:
        return {"delta": d, "direction": "шум", "significant": False}
    if d < 0:
        direction = "лучше" if metric in LOWER_IS_BETTER else "хуже"
    else:
        direction = "хуже" if metric in LOWER_IS_BETTER else "лучше"
    return {"delta": d, "direction": direction, "significant": True}


def question_verdict(metric: str, base: float | None, sft_rows: list[dict],
                     defect_value: float | None) -> dict:
    """Ответ на один вопрос вердикта: у CPT-базы против трёх точек v13.

    Знак дельты читается по смыслу метрики: у обрывов и усечений «меньше — лучше»,
    у агентности и покрытия — «больше — лучше». Это не украшение: без явного
    направления знака «дельта +0.2» означала бы разное в разных строках.
    """
    lower_is_better = metric in LOWER_IS_BETTER
    per_state = []
    for r in sft_rows:
        v = r.get(metric)
        dr = delta_row(base, v, metric)
        # В пределах шума — «ни лучше, ни хуже»: порог объявлен до чтения чисел.
        dr["better"] = {"лучше": True, "хуже": False}.get(dr["direction"])
        per_state.append({"state": r["state"], "step": r["step"], "value": v, **dr})
    vals = [p["value"] for p in per_state if p["value"] is not None]
    better_flags = [p["better"] for p in per_state]
    if not vals or base is None:
        summary = "NOT-VERIFIED: нет чисел"
    elif all(f is True for f in better_flags):
        summary = "да — лучше CPT на всех точках"
    elif any(f is True for f in better_flags):
        summary = "частично — лучше CPT не на всех точках"
    elif all(f is None for f in better_flags):
        summary = "нет — в пределах шума от CPT"
    else:
        summary = "нет — хуже CPT значимо"
    return {
        "metric": metric,
        "base_cpt": nd(base),
        "defect_v12_reference": nd(defect_value),
        "per_state": per_state,
        "summary": summary,
        "lower_is_better": lower_is_better,
    }


def trend(sft_rows: list[dict], metric: str) -> dict:
    """Тренд по шагам: «ещё не обучилось» и «обучается не туда» — разные диагнозы."""
    pts = [(r["step"], r.get(metric)) for r in sft_rows if r.get(metric) is not None]
    if len(pts) < 2:
        return {"points": pts, "direction": None, "slope_per_1k_steps": None}
    first_step, first_v = pts[0]
    last_step, last_v = pts[-1]
    span = (last_step - first_step) or 1
    slope = (float(last_v) - float(first_v)) / (span / 1000.0)
    if abs(float(last_v) - float(first_v)) < SIGNIFICANT:
        direction = "плоский (в пределах шума)"
    else:
        direction = "растёт" if last_v > first_v else "падает"
    return {"points": pts, "first": nd(first_v), "last": nd(last_v),
            "direction": direction, "slope_per_1k_steps": nd(slope)}


def trend_toward_cpt(metric: str, tr: dict) -> bool:
    """Идёт ли тренд метрики в сторону CPT (с учётом смысла метрики)."""
    d = tr.get("direction")
    if d in (None, "плоский (в пределах шума)"):
        return False
    return d == ("падает" if metric in LOWER_IS_BETTER else "растёт")


def recommendation(verdict: dict, trends: dict) -> dict:
    """Рекомендация по стадии — с числами и без решения за владельца (TASK п.4)."""
    worse = [k for k, v in verdict.items()
             if isinstance(v, dict) and v.get("summary", "").startswith("нет — хуже")]
    noise = [k for k, v in verdict.items()
             if isinstance(v, dict) and v.get("summary", "").startswith("нет — в пределах")]
    better = [k for k, v in verdict.items()
              if isinstance(v, dict) and v.get("summary", "").startswith(("да", "частично"))]
    # «Тренд идёт к CPT» — не то же, что «тренд растёт»: у обрывов улучшение — это
    # падение. Направление берётся по смыслу метрики (тот же список, что в дельтах).
    toward = [k for k in worse if trend_toward_cpt(k, trends.get(k, {}))]
    if worse and not better:
        key = "рекомендуется остановить стадию и разобрать"
        why = (f"метрики хуже CPT-финала по {len(worse)} из 3 вопросов "
               f"({', '.join(worse)}) и ни по одному не лучше; тренд по шагам "
               + ("движется к CPT, но разрыв на доступных точках не закрыт"
                  if len(toward) == len(worse) else
                  "не улучшается (по части метрик — уходит от CPT)"))
    elif better and not worse:
        key = "продолжать: формат не хуже CPT"
        why = f"по {len(better)} из 3 вопросов метрики улучшились относительно CPT"
    else:
        key = "продолжать под наблюдением: числа не дают основания ни остановить, ни закрыть вопрос"
        why = ("часть метрик хуже CPT, часть в пределах шума или лучше — "
               "вопрос о валидности стадии остаётся открытым до следующих точек")
    return {
        "recommendation": key,
        "why": why,
        "worse_than_cpt": worse,
        "within_noise": noise,
        "better_than_cpt": better,
        "next_checkpoint_proposal": (
            "снять те же метрики на следующей доступной точке (шаг ~20000, ≈1/3 стадии) "
            "тем же прибором и режимом; решение об остановке — за владельцем стадии"
        ),
        "decision_owner": "владелец стадии (сам прогон дельта не останавливала и не трогала)",
    }


# ─────────────────────────────── сборка ───────────────────────────────────────

def build(report_rel: str | None = None) -> tuple[int, dict]:
    # Путь читается из глобальной константы в момент вызова (а не как значение по
    # умолчанию): тесты подменяют REPORT на фикстуру, не трогая дерево кейса.
    report_rel = report_rel or REPORT
    rep = load(CASE / report_rel)
    if rep is None:
        return EXIT_NOT_VERIFIED, {"error": f"нет отчёта прогона {report_rel}"}
    base_rep = load(CASE / BASELINE_REPORT)
    if base_rep is None:
        return EXIT_NOT_VERIFIED, {"error": f"нет эталонного замера {BASELINE_REPORT}"}

    problems: list[str] = []

    # 1. Режим замера: не штатный — числа несопоставимы с базой (ADR-041).
    proto = dig(rep, ("protocol", "decoding_protocol"), {}) or {}
    if not proto.get("standard"):
        problems.append(f"замер не в штатном режиме: {proto.get('default_mode')}")
    if proto.get("legacy_decoding"):
        problems.append("замер снят с --legacy-decoding (прежний протокол)")
    if proto.get("allowed_for_conclusions") is False:
        problems.append("отчёт помечен allowed_for_conclusions=false")
    # 2. Набор промптов и n: сравнение обязано идти на том же наборе и при n ≥ 100.
    n_prompts = dig(rep, ("protocol", "n_prompts"))
    prompts_set = dig(rep, ("protocol", "prompts_set"))
    if prompts_set != dig(base_rep, ("protocol", "prompts_set")):
        problems.append(f"набор промптов не тот: {prompts_set} против "
                        f"{dig(base_rep, ('protocol', 'prompts_set'))} у эталона")
    if dig(rep, ("protocol", "prompts_digest")) != dig(base_rep, ("protocol", "prompts_digest")):
        problems.append("digest набора промптов расходится с эталонным замером")
    if dig(rep, ("protocol", "stop_at_turn_end")) is not True:
        problems.append("нет остановки на конце хода — обрыв мерил бы прибор (S3aj)")

    states = collect_states(rep)
    if not states:
        return EXIT_NOT_VERIFIED, {"error": "в отчёте нет состояний"}
    base_row = next((s for s in states if s["state"] == "cfinal"), None)
    sft_rows = [s for s in states if s["state"] != "cfinal"]
    if base_row is None:
        problems.append("в прогоне нет состояния cfinal (CPT-финал)")
    if not sft_rows:
        problems.append("в прогоне нет ни одной точки v13")
    for s in states:
        if (s.get("n") or 0) < MIN_N:
            problems.append(f"состояние {s['state']}: n={s.get('n')} < {MIN_N}")
    if n_prompts is not None and n_prompts < MIN_N:
        problems.append(f"проб в наборе {n_prompts} < {MIN_N}")

    # 3. Тождество прибора: (а) хеш файла, (б) ядро из 5 промптов побайтово.
    tool_sha = sha256_file(CASE / "tools/probe_language_split.py")
    ref_sha = base_rep.get("tool_sha256")
    identity_core = {"reference": IDENTITY_REF, "checked": False}
    id_new = load(CASE / IDENTITY_NEW)
    id_ref = load(CASE / IDENTITY_REF)
    if id_new and id_ref:
        p_new = dig(id_new, ("states", "cfinal", "probes"), [])
        p_old = dig(id_ref, ("states", "cfinal", "probes"), [])
        same = len(p_new) == len(p_old) > 0 and all(
            a.get("response") == b.get("response") for a, b in zip(p_new, p_old))
        identity_core.update({"checked": True, "n": len(p_new), "byte_identical": bool(same)})
        if not same:
            problems.append("тождество прибора нарушено: ядро проб не совпало побайтово с S3ai")
    else:
        problems.append("нет файлов гейта тождества ядра — тождество прибора не проверено")

    # 4. Эталон CPT: нормативные числа — из прежнего замера, не переснятые здесь.
    agg_base = dig(base_rep, ("states", "cfinal", "aggregate"), {}) or {}
    baseline_cpt = {
        "source": BASELINE_REPORT + "#states.cfinal.aggregate",
        "how": "штатный режим (greedy + запрет повторов 4-грамм), набор wide, n=104; "
               "числа взяты из прежнего замера, а не пересняты заново",
        "adr": "ADR-042 п.5",
        "metrics": state_metrics(agg_base),
    }

    # Дефектная линия отсчёта: то же состояние-семейство, снятое тем же прибором в S3al.
    defect_src = {}
    for tag in ("sft_resume_24000", "sft_resume_28000"):
        a = dig(base_rep, ("states", tag, "aggregate"), {}) or {}
        if a:
            defect_src[tag] = state_metrics(a)
    defect_ref = {
        "source": BASELINE_REPORT + "#states.{sft_resume_24000,sft_resume_28000}.aggregate",
        "how": "те же прибор и режим на дефектном наборе v12 (для контраста)",
        "metrics": defect_src,
    }

    # 5. Воспроизводимость эталона в этом же прогоне (не замена эталона).
    reproduction = {"checked": False}
    if base_row is not None:
        diffs = {}
        for metric in METRIC_PATHS:
            ref_v = baseline_cpt["metrics"].get(metric)
            new_v = base_row.get(metric)
            if ref_v is None or new_v is None:
                continue
            if round(float(ref_v), 4) != round(float(new_v), 4):
                diffs[metric] = {"reference": ref_v, "remeasured": new_v}
        reproduction = {
            "checked": True,
            "metrics_reproduced": len(diffs) == 0,
            "differences": diffs,
            "how": "CPT-финал снят в этом же прогоне тем же прибором и флагами; "
                   "вердикт строится на сравнении с ним (один процесс), а эталон ADR-042 п.5 "
                   "остаётся нормативной базой",
        }
        if diffs:
            note(f"внимание: эталон CPT не воспроизведён в этом прогоне: {diffs}")

    verdict = {}
    for q, (metric, _title) in QUESTIONS.items():
        verdict[q] = question_verdict(
            metric,
            (base_row or {}).get(metric, baseline_cpt["metrics"].get(metric)),
            sft_rows,
            dig(defect_ref["metrics"], ("sft_resume_28000", metric)),
        )
    trends = {metric: trend(sft_rows, metric)
              for metric in ("unclosed_think", "tool_call", "answer_coverage")}
    # net_effect — сводная оценка «стоила ли нормализация чего-то по формату».
    worse = [k for k, v in verdict.items() if v["summary"].startswith("нет — хуже")]
    better = [k for k, v in verdict.items() if v["summary"].startswith(("да", "частично"))]
    net_effect = {
        "worse_than_cpt": worse,
        "better_than_cpt": better,
        "reading": (
            "нормализация данных не перенесла формат на уровень CPT: как минимум одна "
            "метрика значимо хуже CPT" if worse else
            "формат не хуже CPT по всем трём вопросам" if better else
            "числа не отличаются от CPT значимо"),
        "tolerance": SIGNIFICANT,
        "tolerance_note": "порог значимости 0.10 объявлен до чтения чисел (≈2σ доли при n=104)",
        "defect_data_hypothesis": (
            "дефект данных был не единственной причиной деградации формата"
            if worse else
            "данных достаточно, чтобы формат не был хуже CPT (проверено на доступных точках)"),
    }
    rec = recommendation(verdict, trends)

    table = []
    for s in states:
        row = {"state": s["state"], "step": s["step"], "n": s["n"],
               "checkpoint": s["checkpoint"]}
        for metric in METRIC_PATHS:
            row[metric] = s.get(metric)
        row["marker_pairing_ok"] = s.get("marker_pairing_ok")
        row["think_blocks_mean"] = s.get("think_blocks_mean")
        row["natural_stop_share"] = s.get("natural_stop_share")
        row["deltas_vs_cpt"] = {
            metric: delta_row((base_row or {}).get(metric), s.get(metric), metric)
            for metric in ("unclosed_think", "tool_call", "answer_coverage",
                           "truncated", "looped")
        } if s["state"] != "cfinal" else None
        table.append(row)

    artifacts = {
        "report": REPORT,
        "report_sha256": sha256_file(CASE / REPORT) if (CASE / REPORT).is_file() else None,
        "identity_gate": IDENTITY_NEW,
        "identity_reference": IDENTITY_REF,
        "baseline_source": BASELINE_REPORT,
        "chain": RUN + "/chain.sh",
        "run_manifest": RUN + "/run_manifest.json",
        "device_check": RUN + "/device_check.json",
        "probe_record": RUN + "/probe_record.json",
    }
    for name, rel in (("report", REPORT), ("chain", RUN + "/chain.sh"),
                      ("tool", "tools/probe_language_split.py")):
        p = CASE / rel
        artifacts.setdefault("sha256", {})[name] = sha256_file(p) if p.is_file() else None

    doc = {
        "schema": "s3ap-format-monitor/1",
        "tool": "tools/assemble_s3ap_evidence.py",
        "stage": "S3ap",
        "title": "Метрики формата на точках SFT(v13) против CPT-базы",
        "date": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if not problems else "partial",
        "problem": ("нормализация SFT-набора (ADR-042) должна была снять обрывы рассуждений, "
                    "вернуть агентность и поднять покрытие ответа; проверка — числами, "
                    "не дожидаясь конца стадии"),
        "artifacts": artifacts,
        "framework": {
            "tool": "tools/probe_language_split.py",
            "tool_sha256": tool_sha,
            "mode": "штатный (greedy + запрет повторов 4-грамм, ADR-041); "
                    "--legacy-decoding не использовался",
            "n_per_state": n_prompts,
            "prompts_set": prompts_set,
            "max_new_tokens": dig(rep, ("protocol", "max_new_tokens")),
            "stop_at_turn_end": dig(rep, ("protocol", "stop_at_turn_end")),
            "device": rep.get("device"),
            "where": "локальная RTX 4080 SUPER (GB10 занят стадией SFT — AD-5)",
            "decoding_protocol": proto,
        },
        "instrument_identity": {
            "tool_sha256_current": tool_sha,
            "tool_sha256_at_baseline": ref_sha,
            "same_tool_file": tool_sha == ref_sha,
            "core_byte_identity": identity_core,
            "reading": ("прибор тот же: файл совпал по хешу и ядро проб совпало побайтово"
                        if (tool_sha == ref_sha and identity_core.get("byte_identical"))
                        else "тождество прибора НЕ подтверждено — числа несопоставимы"),
        },
        "states": table,
        "baseline_cpt": baseline_cpt,
        "defect_v12_reference": defect_ref,
        "baseline_reproduction": reproduction,
        "verdict": {**{q: verdict[q] for q in QUESTIONS}, "net_effect": net_effect},
        "trends": trends,
        "recommendation": rec,
        "answers": {
            "a_obryvy": verdict["obryvy"]["summary"],
            "b_agentnost": verdict["agentnost"]["summary"],
            "c_pokrytie": verdict["pokrytie"]["summary"],
        },
        "open_questions": [
            "Точки 500/5000/свежая — это 0.8–14 % стадии (66 157 шагов): числа говорят "
            "о начале обучения, а не о его исходе. Нужна ли ещё одна точка (~шаг 20000) "
            "перед решением об остановке — решает владелец.",
            "Эталон ADR-042 п.5 снят на n=104 в штатном режиме на том же наборе wide; "
            "если архитектор считает нужным переснять CPT-финал целиком как новую базу "
            "критерия (а не как эталон сравнения) — это отдельное решение.",
            "Порог значимости 0.10 (≈2σ) объявлен в приборе свода; при n=104 различие "
            "5–10 п.п. остаётся неразличимым, и это ограничение метода, а не результат.",
        ],
        "assumptions": [
            "Числа дефектного SFT (v12) взяты из того же замера S3al (sft_resume_24000/"
            "sft_resume_28000) — это историческая линия отсчёта, а не новая база.",
            "Свежая точка выбирается правилом «устоявшийся файл» (mtime > 180 с, размер "
            "не меняется 20 с) — прогон пишет чекпойнты прямо во время замера.",
            "Шаг состояния читается из имени чекпойнта (sft_probe_<шаг>.pt).",
        ],
        "problems": problems,
    }
    return (EXIT_FAIL if problems else EXIT_OK), doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=EVIDENCE, help=f"куда писать свод (по умолчанию {EVIDENCE})")
    ap.add_argument("--report", default=REPORT,
                    help="отчёт прибора вместо штатного (повторный свод по другому прогону)")
    ap.add_argument("--print", action="store_true", help="напечатать свод в stdout")
    args = ap.parse_args()

    rc, doc = build(args.report)
    if rc == EXIT_NOT_VERIFIED:
        note(f"NOT-VERIFIED: {doc.get('error')}")
        return rc
    out = CASE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.print:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    note(f"свод: {args.out} (status={doc['status']}, проблем: {len(doc['problems'])})")
    for p in doc["problems"]:
        note(f"  ! {p}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
