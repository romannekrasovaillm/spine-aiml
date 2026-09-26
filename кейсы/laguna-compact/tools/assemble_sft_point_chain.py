#!/usr/bin/env python3
"""assemble_sft_point_chain.py — сводка приборной цепочки завершённой SFT-стадии.

Шаг 7 спеки `docs/specs/SFT-POINT-CHAIN.md`: собрать `evidence/sft-point-chain.json`
и `runs/sft-point-chain-<ts>/REPORT.md` из **уже снятых** артефактов. Ничего не
перемеряется, GPU не занимается: сводка — чтение файлов и применение правил,
объявленных **до** замера.

**Почему вердикты считает сводка, а не отчёт рукой.** Правило, применённое после
числа, — это подгонка, даже когда подгонки не было. Поэтому пороги здесь —
константы, взятые из ADR/S4, и первое, что печатает сборщик, — объявленный список
порогов (`.thresholds`), а не результат. Числа-константы не подбираются: K1/K2 —
потолок 2× своей базы (ADR-027 п.1/п.2, S4-PROTOCOL §3), домен — отношение к базе
< 1 (ADR-044 п.1/п.6), agentic — доля не падает и pass-rate растёт (ADR-033 п.2в в
редакции 17.09.2026), формат — покрытие ответа и его составляющие против прежнего
входа при пороге различимости 0.10 (2σ при n = 104, SFT-STAGE-PLAN §13.2).

**Три состояния вердикта, а не два.** Прибор мог **отказать** (артефакта нет), мог
**не быть запущен** (шаг не входил в цепочку), а мог дать число, к которому правило
**неприменимо** (несопоставимая база). Это три разные вещи, и они не схлопываются в
«нет данных»: `verdict` принимает `pass | fail | not_measured | refused`, и у
`not_measured`/`refused` обязательна причина. Иначе «не мерили» читается как
«чисто», а это ровно тот класс ложных выводов, ради которого в кейсе заведены
ADR-041 п.5 и ADR-033 п.2.

**Что сводка не делает:** не пересчитывает базы; не меняет пороги; не выносит
вердикт по точке с n < 100 (SFT-POINT-CHAIN §4.4 — `n` печатается рядом с долей,
и меньше сотни он называет сам); не создаёт `evidence/result-report.json` (C-014:
точка решения не пройдена — нет RL).

Коды возврата::

    0 — все измеримые критерии сняты и пройдены
    1 — хотя бы один критерий снят и **не** пройден
    2 — NOT-VERIFIED: часть критериев не снята (сводка собрана, вопрос не закрыт)

Запуск::

    python3 tools/assemble_sft_point_chain.py --ts 20260925-1000 --report
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
SHARED = Path("/home/user/gb10-shared")
STAND_TMUX = "gb10-fast"

#: ── объявленные пороги (НЕ меняются задним числом; SFT-POINT-CHAIN §4.5) ─────
#: Язык: потолок 2× своей базы, база — из ADR-029, цитируется S4-PROTOCOL §3.
K1_BASE, K1_CEILING = 7.504686, 15.009373
K2_BASE, K2_CEILING = 6.159936, 12.319871
#: Домен: решающий набор — v3 (ADR-044 п.1/п.6), «выучен» = отношение к базе < 1.
#: База v3 — из S3ao/ADR-044; исторические шкалы v1/v2 с решающей роли сняты.
DOMAIN_BASE, DOMAIN_LEARNED = 13.559193, 1.0
#: Agentic: база 45.1 % (158/350, ADR-033 п.2, вставка 17.09.2026) — **историческая**
#: в режиме: ADR-041 п.3 объявляет её legacy и требует переснятия. Поэтому здесь она
#: хранится как справка, а вердикт (в1) считается против переснятой базы.
AGENTIC_LEGACY_BASE = {"n": 350, "n_tool_call": 158, "share": 0.4514,
                       "n_pass": 53, "pass_rate": 0.1514,
                       "source": "evidence/s3aa-agentic-cpt.json (legacy, ADR-041 п.3)"}
#: Порог различимости для форматных долей: 0.10 ≈ 2σ доли при n = 104
#: (SFT-STAGE-PLAN §13.2, объявлен там **до** чтения чисел). Различия меньше —
#: шум, и они называются шумом, а не «сдвигом».
FORMAT_NOISE = 0.10
FORMAT_MIN_N = 100

#: ── разбор коллизии баз (SFT-POINT-CHAIN §3#5: «сначала сверить сопоставимость») ──
#: В кейсе ходят два числа про «самостоятельные вызовы», и они выглядят как
#: противоречие только пока не названы прибор и конфигурация. Оба записаны здесь
#: **до** замера: разбор — это факт о прошлых артефактах, а не вывод из нового числа.
#: ── носители базовых констант (S4-PROTOCOL §5а, введён 25.09.2026) ──────────
#: «Числа — не константы из текста, а измерения конкретных артефактов. Вердикт без
#: носителя не принимается». Хеши наборов снимаются из самих отчётов-носителей, а не
#: переписываются руками: переписанный хеш доказывал бы наклейку, а не носителя.
CONSTANT_CARRIERS = {
    "k1": {"value": "7.504686 / 15.009373", "carrier": "evidence/s3q-baseline-v3.json",
           "set": "datasets/general_eval_v3.txt", "set_key": "v3_general", "docs": 200},
    "k2": {"value": "6.159936 / 12.319871", "carrier": "evidence/s3t-baseline-k2.json",
           "set": "datasets/general_eval_k2.txt", "set_key": "k2_general", "docs": 200},
    "domain_v3": {"value": "13.559193", "carrier": "runs/s3ao-domain-v3-20260919/ + "
                                                  "evidence/s3ao-domain-v3.json",
                  "set": "datasets/domain_eval_v3.txt", "set_key": "v3_domain", "docs": 200},
}


def _carriers_with_hashes(ppl: dict | None = None) -> dict:
    """Носители + хеш набора каждого, выведенный из отчёта-носителя.

    Хеш берётся **из носителя**, а не из головы; дополнительно он сверяется с хешем
    набора в **своём** PPL-отчёте. Совпадение доказывает, что порог и метрика стоят
    на одном ряду (S4-PROTOCOL §5а; RL-STAGE-PLAN §6.1: «иначе порог и метрика
    оказались бы на разных рядах»), а расхождение — находка, а не молчание.
    """
    out = {}
    for name, c in CONSTANT_CARRIERS.items():
        d = read_json(CASE_ROOT / c["carrier"].split(" + ")[-1])
        sha = ((d.get("datasets") or {}).get(c["set_key"]) or {}).get("sha256")
        if sha is None:
            sha = ((d.get("dataset") or {}) if isinstance(d.get("dataset"), dict) else {}).get("sha256")
        mine = (((ppl or {}).get("sets") or {}).get(c["set_key"]) or {}).get("sha256")
        out[name] = {k: v for k, v in c.items() if k != "set_key"}
        out[name]["set_sha256"] = sha
        out[name]["set_sha256_in_this_measurement"] = mine
        out[name]["same_set_bytes"] = (None if sha is None or mine is None else sha == mine)
    return out


AGENTIC_BASE_COLLISION = {
    "question": "45.1 % самостоятельных вызовов против «0 из 700» — одно и то же?",
    "answer": "нет: разные чекпойнты и разные конфигурации прибора",
    "number_a": {
        "value": "45.1 % (158/350; v1 86/150 = 57.3 %, v2 72/200 = 36.0 %)",
        "artifact": "evidence/s3aa-agentic-cpt.json",
        "run": "runs/passrate-agentic-cpt-20260917-0153",
        "checkpoint": "full-cpt-20260916-2149/checkpoints/checkpoint_final.pt",
        "checkpoint_sha256": "080c3ab6523f44378c87f443e4d10fb84e060f5c4ae3825e721aad066b54e086",
        "instrument": "tools/passrate_probe.py --no-toolcall-force --no-hint",
        "decoding": "legacy (в отчёте нет protocol.decoding; запрет повторов 4-грамм не действовал)",
        "device": "local-rtx4080s",
        "role": "измеряет ВХОДНОЙ CPT-чекпойнт SFT-стадии (не SFT)",
        "verdict": ("историческое число: ADR-041 п.3 объявляет базу переснятию подлежащей и "
                    "запрещает выносить вердикт по agentic-доле до переснятия"),
        "label_defect": ("в артефакте поле protocol.weights_kind='sft_checkpoint' при пути и "
                         "sha256 CPT-финала — сама эта наклейка и питает коллизию"),
    },
    "number_b": {
        "value": "0 самостоятельных вызовов на 700 задач",
        "artifact": "docs/specs/POOL-DEAD-TYPES.md (+ evidence/pool-dead-types.json)",
        "run": "runs/passrate-sft-20260916-1200",
        "checkpoint": "sft_checkpoint_final.pt sha 5bc15f708111 (историческая SFT-стадия sft-20260916-2246)",
        "instrument": "tools/passrate_probe.py С toolcall_force (форсированный первый вызов)",
        "decoding": "legacy",
        "role": "измеряет исторический SFT-чекпойнт, а не CPT; самовызов там не измерим по построению",
        "verdict": ("к CPT-финалу неприменимо (ADR-033 п.2, вставка 17.09.2026: «прежнее "
                    "„0 на 700“ получено на SFT-чекпойнте и с toolcall_force, где первый "
                    "вызов вкладывает харнесс, — измерить самовызов там нельзя»)"),
    },
    "what_is_comparable_now": {
        "legacy_base_a": "НЕ сопоставима: другой режим декодирования (ADR-041 п.3/п.5)",
        "number_b": "НЕ сопоставима: toolcall_force=true — самостоятельность не измерена",
        "required": ("база обязана быть переснята тем же прибором, с теми же флагами, в том же "
                     "режиме и на входном CPT-чекпойнте — тогда сравнение «SFT против входа» "
                     "отвечает на вопрос ADR-033 п.2в1/в2, а не на вопрос «какой режим»"),
    },
    "caveat_about_the_guard": (
        "tools/check_sft_agentic_share.py (ADR-059) проверяет СОПОСТАВИМОСТЬ базы и отчёта: "
        "штатный режим protocol.decoding у обоих (ADR-041 п.4), флаги без форсирования, разные "
        "предметы (вход и выход стадии) со сверкой с распиской брони, совпадающие sha256 пулов "
        "и n; legacy-отчёт он отвергает. Условия (в1)/(в2) страж печатает как данные, но "
        "решающего голоса по (в) у него нет: вердикт («доля не падает» ∧ «pass-rate растёт») "
        "выносит сводка — эта функция, — читая ту же пару отчётов. Так страж не краснеет на "
        "правильном переснятом замере (прежний дефект: сверка числа с legacy-цитатой ADR-033)"),
}


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{type(e).__name__}: {e}"}


def crit(verdict: str, **kw) -> dict:
    return {"verdict": verdict, **kw}


# ── критерий 3: язык K1 ∧ K2 ─────────────────────────────────────────────────

def language_criterion(ppl: dict) -> dict:
    st = (ppl.get("states") or {}).get("sft", {}).get("sets") or {}
    base = (ppl.get("states") or {}).get("base", {}).get("sets") or {}
    if not st or not base:
        return crit("not_measured", why="нет отчёта PPL для состояния sft/base")
    k1, k2 = st["v3_general"]["ppl"], st["k2_general"]["ppl"]
    parts = {
        "k1": {"ppl": k1, "base": K1_BASE, "ceiling": K1_CEILING,
               "base_in_run": base["v3_general"]["ppl"], "passed": k1 <= K1_CEILING},
        "k2": {"ppl": k2, "base": K2_BASE, "ceiling": K2_CEILING,
               "base_in_run": base["k2_general"]["ppl"], "passed": k2 <= K2_CEILING},
    }
    passed = parts["k1"]["passed"] and parts["k2"]["passed"]
    checks = [c.get("verdict") for c in (ppl.get("checks") or [])]
    return crit("pass" if passed else "fail", rule="конъюнкция: K1 ≤ 2× базы И K2 ≤ 2× базы (ADR-027 п.1/п.2)",
                **parts, conjunction=passed,
                instrument_identity="; ".join(
                    f"{c.get('name')}={c.get('verdict')}" for c in (ppl.get("checks") or [])),
                instrument_identity_ok=bool(checks) and all(v == "ok" for v in checks),
                source=str(ppl.get("__path__", "")))


def domain_criterion(ppl: dict) -> dict:
    st = (ppl.get("states") or {}).get("sft", {}).get("sets") or {}
    if not st:
        return crit("not_measured", why="нет отчёта PPL для состояния sft")
    ppl_v3 = st["v3_domain"]["ppl"]
    rel = ppl_v3 / DOMAIN_BASE
    return crit("pass" if rel < DOMAIN_LEARNED else "fail",
                rule="решающий набор v3: отношение к базе < 1 = домен выучен (ADR-044 п.1/п.6)",
                ppl=ppl_v3, base=DOMAIN_BASE, relation=rel, learned=rel < DOMAIN_LEARNED,
                calibration_scale_note=(
                    "калибровочная шкала монитора стадии (домен v2, ×0.348) решающей не "
                    "является: она снята с решающей роли ADR-044 п.1 — вердикт только по v3"))


# ── критерий 4: agentic (ADR-033 п.2в) ───────────────────────────────────────

def _pool_units(rep: dict) -> dict:
    out = {}
    for name, p in (rep.get("pools") or {}).items():
        ov = p.get("overall") or {}
        if ov.get("n"):
            out[name] = {"n": ov["n"], "share": ov.get("tool_call_share"),
                         "n_pass": ov.get("n_pass"), "pass_rate": ov.get("pass_rate"),
                         "pool_sha256": p.get("sha256")}
    n = sum(u["n"] for u in out.values())
    k = sum(round(u["n"] * u["share"]) for u in out.values() if u["share"] is not None)
    np_ = sum(u["n_pass"] or 0 for u in out.values())
    out["combined"] = {"n": n, "share": round(k / n, 4),
                       "n_pass": np_, "pass_rate": round(np_ / n, 4),
                       "pool_sha256": None, "sample": sorted(x for x in out)}
    return out


def agentic_criterion(sft: dict, base: dict | None, guard: dict | None) -> dict:
    if not sft or sft.get("__error__"):
        return crit("not_measured", why="нет отчёта агентной пробы по SFT-финалу",
                    artifact="evidence/s3aa-agentic-sft.json")
    proto = sft.get("protocol") or {}
    dec = proto.get("decoding") or {}
    mode = dec.get("mode") or dec.get("no_repeat_ngram")
    standard = bool(dec.get("standard")) if "standard" in dec else dec.get("no_repeat_ngram") == 4
    facts = {
        "sft_checkpoint_sha256": proto.get("checkpoint_sha256"),
        "sft_decoding": mode, "sft_standard_mode": standard,
        "sft_toolcall_force": proto.get("toolcall_force"), "sft_prefill": proto.get("prefill"),
        "sft_note": ("штатный режим: запрет повторов 4-грамм — умолчание прибора; "
                     "--no-toolcall-force --no-hint обязательны (ADR-033 п.2)"),
    }
    if not standard:
        return crit("refused", why="отчёт SFT снят не в штатном режиме декодирования", **facts)
    if proto.get("toolcall_force"):
        return crit("refused", why="отчёт SFT снят с toolcall_force — самостоятельность не измерена", **facts)
    sft_units = _pool_units(sft)
    facts["sft_units"] = sft_units

    if base is None or base.get("__error__"):
        return crit(
            "refused",
            why=("база не переснята в штатном режиме: evidence/s3aa-agentic-cpt.json — "
                 "legacy (без protocol.decoding), ADR-041 п.3 запрещает выносить вердикт "
                 "по agentic-доле до переснятия базы; сравнение шло бы между режимами"),
            legacy_base=AGENTIC_LEGACY_BASE, **facts)
    bproto = base.get("protocol") or {}
    bdec = bproto.get("decoding") or {}
    b_standard = bool(bdec.get("standard")) if "standard" in bdec else bdec.get("no_repeat_ngram") == 4
    facts["base_decoding"] = bdec.get("mode") or bdec.get("no_repeat_ngram")
    facts["base_standard_mode"] = b_standard
    facts["base_checkpoint_sha256"] = bproto.get("checkpoint_sha256")
    if not b_standard:
        return crit("refused",
                    why="база снята не в штатном режиме — сравнение недействительно", **facts)
    # Сопоставимость по пулам: хеш набора обязан совпасть у обоих отчётов.
    base_units = _pool_units(base)
    facts["base_units"] = base_units
    mismatch = [p for p in sft_units if p != "combined"
                and (p not in base_units
                     or sft_units[p]["pool_sha256"] != base_units[p]["pool_sha256"])]
    if mismatch:
        return crit("refused",
                    why=f"состав/хеш пулов разошёлся ({', '.join(mismatch)}) — измерены разные наборы",
                    **facts)

    checks, n_note = [], []
    for unit in sorted(p for p in sft_units if p != "combined") + ["combined"]:
        a, b = sft_units[unit], base_units[unit]
        n_note.append(a["n"])
        if unit == "combined" and a.get("sample") != b.get("sample"):
            continue
        d_share = a["share"] - b["share"]
        checks.append({"rule": "в1: доля самостоятельных вызовов не падает", "unit": unit,
                       "value": a["share"], "base": b["share"], "delta": round(d_share, 4),
                       "n": a["n"], "passed": a["share"] >= b["share"] - 1e-9,
                       "reading": f"{round(a['n']*a['share'])}/{a['n']} = {a['share']:.4f} "
                                  f"против базы {round(b['n']*b['share'])}/{b['n']} = {b['share']:.4f}"})
        checks.append({"rule": "в2: pass-rate растёт", "unit": unit,
                       "value": a["pass_rate"], "base": b["pass_rate"],
                       "delta": round((a["pass_rate"] or 0) - (b["pass_rate"] or 0), 4),
                       "n": a["n"], "passed": (a["pass_rate"] or 0) > (b["pass_rate"] or 0),
                       "reading": f"{a['n_pass']}/{a['n']} = {a['pass_rate']:.4f} "
                                  f"против базы {b['n_pass']}/{b['n']} = {b['pass_rate']:.4f}"})
    passed = all(c["passed"] for c in checks)
    return crit("pass" if passed else "fail",
                rule=("ADR-033 п.2в: (в1) доля самостоятельных вызовов не падает, "
                      "(в2) pass-rate растёт — против базы, переснятой в том же режиме"),
                checks=checks, n_min=min(n_note) if n_note else None,
                small_n_warning=(None if n_note and min(n_note) >= 100 else
                                 "есть единицы с n < 100 — вывод по ним не делается (§4.4)"),
                guard=guard, legacy_base=AGENTIC_LEGACY_BASE, **facts)


# ── критерий 6: формат и петли ───────────────────────────────────────────────

def _fmt(st: dict) -> dict | None:
    a = (st or {}).get("aggregate")
    if not a:
        return None
    return {
        "n": a.get("n"),
        "answer_coverage": (a.get("cyr_answer") or {}).get("coverage"),
        "answer_defined": (a.get("cyr_answer") or {}).get("defined"),
        "unclosed_think_share": a.get("unclosed_think_share"),
        "tool_call_share": a.get("mode_share_tool_call"),
        "truncated_share": a.get("stop", {}).get("truncated_share"),
        "stop_reasons_share": a.get("stop", {}).get("reasons_share"),
        "natural_stop_share": a.get("stop", {}).get("natural_stop_share"),
        "median_len": (a.get("lengths") or {}).get("median"),
        "checkpoint_sha256": st.get("checkpoint_sha256"),
        "checkpoint": st.get("checkpoint"),
    }


def format_criterion(fmt: dict, ppl_path_ok: bool = True) -> dict:
    states = (fmt or {}).get("states") or {}
    sft, cfin, base = _fmt(states.get("sft")), _fmt(states.get("cfinal")), _fmt(states.get("base"))
    if not sft:
        return crit("not_measured", why="нет отчёта форматной пробы для состояния sft",
                    artifact="runs/sft-point-chain-<ts>/format-wide-8192.json")
    proto = fmt.get("protocol") or {}
    dec = proto.get("decoding_protocol") or {}
    facts = {"protocol": {"prompts_set": proto.get("prompts_set"),
                          "max_new_tokens": proto.get("max_new_tokens"),
                          "stop_at_turn_end": proto.get("stop_at_turn_end"),
                          "decoding": proto.get("decoding"),
                          "standard": dec.get("standard"),
                          "no_repeat_ngram": dec.get("no_repeat_ngram")},
             "sft": sft, "cfinal": cfin, "base": base}
    if not dec.get("standard"):
        return crit("refused", why="проба снята не в штатном режиме (нет запрета повторов 4-грамм)", **facts)
    if not cfin:
        return crit("not_measured", why="прежний вход (CPT-финал) в отчёте не измерен — сравнивать не с чем", **facts)

    def delta(key):
        a, b = sft.get(key), cfin.get(key)
        if a is None or b is None:
            return None
        return round(a - b, 4)

    comparisons = {
        "answer_coverage": {"sft": sft["answer_coverage"], "prev_input": cfin["answer_coverage"],
                            "delta": delta("answer_coverage"),
                            "better": (delta("answer_coverage") or 0) > 0,
                            "significant": abs(delta("answer_coverage") or 0) >= FORMAT_NOISE,
                            "rule": "ADR-042 п.5(в): покрытие ответа не падает"},
        "truncated_share": {"sft": sft["truncated_share"], "prev_input": cfin["truncated_share"],
                            "delta": delta("truncated_share"),
                            "better": (delta("truncated_share") or 0) < 0},
        "unclosed_think_share": {"sft": sft["unclosed_think_share"], "prev_input": cfin["unclosed_think_share"],
                                 "delta": delta("unclosed_think_share"),
                                 "better": (delta("unclosed_think_share") or 0) < 0},
        "tool_call_share": {"sft": sft["tool_call_share"], "prev_input": cfin["tool_call_share"],
                            "delta": delta("tool_call_share"),
                            "better": (delta("tool_call_share") or 0) > 0},
    }
    small = [k for k, v in comparisons.items() if (v["sft"] is not None and sft["n"] and sft["n"] < FORMAT_MIN_N)]
    passed = comparisons["answer_coverage"]["better"] and not small
    return crit("pass" if passed else "fail",
                rule=("ADR-042 п.5: покрытие ответа и его составляющие против **прежнего входа** "
                      f"(CPT-финал), порог различимости {FORMAT_NOISE} (2σ при n = 104)"),
                comparisons=comparisons, noise_threshold=FORMAT_NOISE,
                n_min_warning=(f"n < {FORMAT_MIN_N} у: {', '.join(small)}" if small else None),
                **facts)


# ── критерий 2: осевой eval с судьёй ─────────────────────────────────────────

def eval_criterion(logs: Path, name: str) -> dict:
    p = logs / f"eval_results_{name}.json"
    d = read_json(p)
    if d.get("__error__"):
        #: not_measured ≠ дефект прибора: цепочка стадий идёт последовательно, и ось без
        #: артефакта — это ось, чья стадия ещё не закрыта, а не сломанный замер. Отчёт
        #: обязан назвать это словами, иначе читатель через сутки прочтёт «нет файла»
        #: как отказ (§5: отчёт — по числам, а причину неполноты надо называть).
        return crit("not_measured",
                    why=(f"нет артефакта eval ({p}): {d['__error__']} — стадия этой оси ещё "
                         "не закрыта (цепочка стадий последовательна, замер идёт в tmux-"
                         "сессии на стенде): not_measured до её финиша — честный статус, "
                         "не дефект прибора"),
                    artifact=str(p))
    judge = d.get("judge")
    if judge != "GLM-API":
        return crit("refused",
                    why=f"артефакт несёт judge={judge} — эвристика вместо судьи в приёмку не идёт "
                        f"(SFT-POINT-CHAIN §6; ось AD-1 не закрыта)",
                    artifact=str(p), judge=judge)
    return crit("measured", rule="осевой eval от судьи GLM-API (S4-PROTOCOL §1)",
                artifact=str(p), judge=judge, judge_mean=d.get("judge_mean"),
                slug_accuracy=d.get("slug_accuracy"), total=d.get("total"),
                note=("направление оси AD-1 (judge_mean RL > judge_mean SFT) читается только "
                      "после RL-стадии; здесь снимается SFT-половина оси"))


# ─────────────────────────────────────────────────────────────────────────────

def build(ts: str) -> dict:
    chain = SHARED / f"sft-point-chain-{ts}"
    case_run = CASE_ROOT / "runs" / f"sft-point-chain-{ts}"
    logs = chain / "logs"
    receipt = read_json(chain / "ckpt" / "RECEIPT.json")
    if receipt.get("__error__"):
        receipt = read_json(case_run / "RECEIPT.json")

    ppl = read_json(case_run / "ppl-sft.json")
    ppl["__path__"] = str(case_run / "ppl-sft.json")
    sft_probe = read_json(CASE_ROOT / "evidence" / "s3aa-agentic-sft.json")
    base_probe_p = CASE_ROOT / "evidence" / "s3aa-agentic-cpt-nogram4.json"
    base_probe = read_json(base_probe_p) if base_probe_p.is_file() else None
    guard_p = case_run / "guard-agentic.json"
    guard = read_json(guard_p) if guard_p.is_file() else None
    fmt = read_json(case_run / "format-wide-8192.json")

    #: ADR-058 п.1/п.5: предмет замера — **бронь**, и отчёт обязан называть, что измерено
    #: замороженное состояние. Проверяем это фактом артефакта против расписки, а не
    #: памятью исполнителя: если прибор записал живой путь вместо пути брони, сводка
    #: говорит это вслух. Совпадение inode делает измерение тем же состоянием — но
    #: буквальное правило «читает копию» при этом не выполнено, и разница названа.
    provenance_limits: list[str] = []

    def _provenance(label: str, rep, booked: dict | None) -> None:
        if not rep or rep.get("__error__") or not booked:
            return
        proto = rep.get("protocol") or {}
        read, copy = proto.get("checkpoint_path") or proto.get("weights"), booked.get("copy")
        if not read or not copy or Path(str(read)) == Path(str(copy)):
            return
        provenance_limits.append(
            f"{label}: прибор читал живой путь `{read}`, а не бронь `{copy}` — бронь "
            f"хардлинком на тот же inode {booked.get('inode')} и sha "
            f"{str(booked.get('sha256'))[:12]}…, предмет сверен в начале пробы, поэтому "
            "измерено то же состояние; но буквальная форма ADR-058 п.1 («все приборы читают "
            "копию») здесь не выполнена — названо, а не умолчано")

    _provenance("агентная проба SFT-финала", sft_probe,
                {"copy": receipt.get("copy"), "sha256": receipt.get("sha256"),
                 "inode": receipt.get("inode")})
    _provenance("переснятие агентной базы (входной CPT)",
                base_probe, next(iter(receipt.get("aux_subjects") or []), None))

    criteria = {
        "eval_axis_sft": eval_criterion(logs, "sft"),
        "eval_base": eval_criterion(logs, "base"),
        "language": language_criterion(ppl),
        "domain": domain_criterion(ppl),
        "agentic": agentic_criterion(sft_probe, base_probe, guard),
        "format": format_criterion(fmt),
    }
    #: Операционное состояние: что на диске, что в полёте и чем продолжить. Живёт в
    #: сводке, а не в переписке: читатель отчёта через сутки не имеет ни переписки,
    #: ни памяти о том, какой процесс куда писал.
    def _state(p: Path) -> str:
        return f"есть ({p.stat().st_size} Б)" if p.is_file() else "нет — не снят или проба в полёте"

    def _live(pattern: str) -> bool:
        import subprocess  # noqa: PLC0415
        return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0

    operating = {p: ("идёт" if _live(p) else "не запущен")
                 for p in ("point_local_probe_chain", "point_agentic_probe",
                           "passrate_probe", "probe_language_split")}
    operational = {
        "chain_dir": str(chain), "ctr_dir": f"/workspace/shared/{chain.name}",
        "case_mirror": str(case_run),
        "tmux": f"ssh {STAND_TMUX} 'tmux ls' → сессия `{chain.name}`",
        "artifacts": {
            "ckpt/RECEIPT.json (бронь предмета)": _state(chain / "ckpt" / "RECEIPT.json"),
            "evidence/s3aa-agentic-sft.json": _state(CASE_ROOT / "evidence" / "s3aa-agentic-sft.json"),
            "evidence/s3aa-agentic-cpt-nogram4.json":
                _state(CASE_ROOT / "evidence" / "s3aa-agentic-cpt-nogram4.json"),
            "runs/.../ppl-sft.json (язык + домен)": _state(case_run / "ppl-sft.json"),
            "runs/.../format-wide-8192.json": _state(case_run / "format-wide-8192.json"),
            "logs/eval_results_sft.json (стенд)": _state(logs / "eval_results_sft.json"),
            "logs/eval_results_base.json (стенд)": _state(logs / "eval_results_base.json"),
        },
        "resume": [
            f"# стенд: цепочка eval (tmux-сессия {chain.name}); повтор той же строки —",
            f"# снимок валится на месте, цепочка продолжает с первой неготовой стадии",
            f"ssh gb10-fast 'tmux ls; cat {chain}/var/chain.status'",
            f"bash {chain}/chain_sleep_guard.sh bash {chain}/pilot_chain.sh \\",
            f"    --run-dir {chain} --ctr-run-dir /workspace/shared/{chain.name} \\",
            f"    --stages-file {chain}/stages.tsv --exp-base {chain.name} \\",
            f"    --ctr-prefix laguna-{chain.name}   # + остальные флаги: {chain}/chain_command.txt",
            "# карта 4080: агентная проба (sft, затем cpt) и форматная — очередь их сериализует",
            f"setsid nohup bash tools/point_local_probe_chain.sh --ts {ts} \\",
            f"    >> runs/sft-point-chain-{ts}/local-chain.log 2>&1 < /dev/null &",
            "# сводка и отчёт — из уже снятых артефактов, GPU не занимает",
            f"python3 tools/build_sft_point_chain.py --ts {ts} --do mirror",
            f"python3 tools/assemble_sft_point_chain.py --ts {ts} --report",
        ],
        "running_probe_processes": operating,
    }
    measured = {k: v for k, v in criteria.items() if v["verdict"] in ("pass", "fail")}
    failed = [k for k, v in measured.items() if v["verdict"] == "fail"]
    not_measured = {k: v.get("why") for k, v in criteria.items()
                    if v["verdict"] in ("not_measured", "refused")}
    return {
        "tool": "assemble_sft_point_chain.py",
        "stage": "SFT-POINT-CHAIN",
        "title": "Приборная цепочка завершённой SFT-стадии: ось, язык, домен, агентность, формат",
        "adr": ["ADR-033 п.2", "ADR-041 п.3", "ADR-042 п.5", "ADR-044", "ADR-045 п.4",
                "ADR-051", "ADR-058 (бронь предмета)"],
        "spec": "docs/specs/SFT-POINT-CHAIN.md",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "complete" if not not_measured else "partial",
        "subject": {"receipt": receipt,
                    "chain_dir": str(chain),
                    "case_mirror": str(case_run)},
        "thresholds": {"k1": {"base": K1_BASE, "ceiling": K1_CEILING},
                       "k2": {"base": K2_BASE, "ceiling": K2_CEILING},
                       "domain_v3": {"base": DOMAIN_BASE, "learned_below": DOMAIN_LEARNED},
                       "agentic_legacy_base": AGENTIC_LEGACY_BASE,
                       "format_noise_2sigma_n104": FORMAT_NOISE,
                       "declared_before_measurement": True},
        "criteria": criteria,
        "operational": operational,
        "constants_carriers": _carriers_with_hashes(ppl),
        "agentic_base_collision": AGENTIC_BASE_COLLISION,
        "verdicts": {k: v["verdict"] for k, v in criteria.items()},
        "not_measured": not_measured,
        "failed": failed,
        "n_by_measurement": {
            "eval_sft": criteria["eval_axis_sft"].get("total"),
            "eval_base": criteria["eval_base"].get("total"),
            "language_domain_docs": 200,
            "agentic_tasks": (criteria["agentic"].get("sft_units") or {}).get("combined", {}).get("n"),
            "format_probes": (criteria["format"].get("sft") or {}).get("n"),
        },
        "limits": [
            "протокол узкий (AD-1): один сид, одна попытка на задачу — pass@k не экстраполируется",
            "направление оси AD-1 не выносится: RL не запускался, точки решения нет",
            "evidence/result-report.json не создавался (C-014: точка решения не пройдена)",
            "числа PPL сняты локальной 4080 (ADR-031/ADR-044); стенд GB10 в PPL-замерах не участвовал",
            ("адресация носителя (S4-PROTOCOL §1/§5а, введена 25.09.2026): eval-артефакты этой стадии "
             "даёт приборная цепочка runs/sft-point-chain-<ts>/logs/ (прогон-носитель стадии — "
             "sft-v13-2e6-20260921-2054); носители базовых констант — поле constants_carriers "
             "этой сводки; схема result-report не расширялась"),
        ] + provenance_limits,
    }


def report_md(ev: dict) -> str:
    c = ev["criteria"]
    L = []
    A = L.append
    A(f"# {ev['title']}")
    A("")
    A(f"- Дата: {ev['date']} · спека: `{ev['spec']}` · статус сводки: **{ev['status']}**")
    A("- Предмет брони: "
      f"`{ev['subject']['receipt'].get('sha256', '?')[:16]}…` "
      f"({ev['subject']['receipt'].get('method')}, "
      f"inode {ev['subject']['receipt'].get('inode')})")
    A("")
    A("## Вердикты")
    A("")
    A("| Критерий | Вердикт | Число |")
    A("|---|---|---|")
    for k, v in c.items():
        if k == "eval_axis_sft":
            num = f"judge={v.get('judge')} mean={v.get('judge_mean')} n={v.get('total')}"
        elif k == "eval_base":
            num = f"judge={v.get('judge')} mean={v.get('judge_mean')} n={v.get('total')}"
        elif k == "language":
            num = (f"K1 {v.get('k1', {}).get('ppl')} ≤ {K1_CEILING}; "
                   f"K2 {v.get('k2', {}).get('ppl')} ≤ {K2_CEILING}")
        elif k == "domain":
            num = f"v3 {v.get('ppl')} / {DOMAIN_BASE} = {v.get('relation')}"
        elif k == "agentic":
            u = (v.get("sft_units") or {}).get("combined", {})
            num = f"доля {u.get('share')} (n={u.get('n')}), pass_rate {u.get('pass_rate')}"
        elif k == "format":
            s = v.get("sft") or {}
            num = (f"покрытие {s.get('answer_coverage')} (n={s.get('n')}), "
                   f"усечение {s.get('truncated_share')}")
        else:
            num = ""
        A(f"| {k} | **{v['verdict']}** | {num} |")
    A("")
    if ev["not_measured"]:
        A("## Не снято / не вынесено")
        A("")
        for k, why in ev["not_measured"].items():
            A(f"- **{k}**: {why}")
        A("")
    for key in ("language", "domain", "agentic", "format"):
        v = c[key]
        if v["verdict"] not in ("pass", "fail"):
            continue
        A(f"## {key}")
        A("")
        A(f"- Правило: {v.get('rule')}")
        if key == "language":
            A(f"- K1 (v3_general): {v['k1']['ppl']:.6f} против базы {K1_BASE} "
              f"(потолок {K1_CEILING}); база в прогоне {v['k1']['base_in_run']:.6f}")
            A(f"- K2 (k2_general): {v['k2']['ppl']:.6f} против базы {K2_BASE} "
              f"(потолок {K2_CEILING}); база в прогоне {v['k2']['base_in_run']:.6f}")
            A(f"- Тождество прибора: {v.get('instrument_identity')}")
        if key == "domain":
            A(f"- Отношение: {v['relation']:.6f} < 1 → {v['learned']}")
            A(f"- {v.get('calibration_scale_note')}")
        if key == "agentic":
            for ch in v.get("checks", []):
                A(f"- [{'ok' if ch['passed'] else 'FAIL'}] {ch['unit']} | {ch['rule']}: {ch['reading']}")
            A(f"- Оговорка: {v.get('small_n_warning') or '—'}")
        if key == "format":
            for name, cm in v["comparisons"].items():
                A(f"- {name}: SFT {cm['sft']} против прежнего входа {cm['prev_input']} "
                  f"(Δ {cm['delta']}); порог различимости {FORMAT_NOISE}")
        A("")
    A("## Носители базовых констант (S4-PROTOCOL §5а)")
    A("")
    A("| Константа | Значение | Носитель | Набор (sha256) |")
    A("|---|---|---|---|")
    for name, c in ev["constants_carriers"].items():
        same = c.get("same_set_bytes")
        mark = {True: "совпал с замером", False: "РАСХОЖДЕНИЕ", None: "не с чем сверить"}[same]
        A(f"| {name} | {c['value']} | `{c['carrier']}` | `{c['set']}` "
          f"({c['docs']} док, `{(c.get('set_sha256') or '—')[:16]}…`) |")
        A(f"<!-- набор в замере: {(c.get('set_sha256_in_this_measurement') or '—')[:16]}… — {mark} -->")
    A("")
    A("## Разбор коллизии agentic-баз (SFT-POINT-CHAIN §3#5)")
    A("")
    col = ev["agentic_base_collision"]
    A(f"**Вопрос:** {col['question']} → **{col['answer']}**")
    A("")
    for key, title in (("number_a", "Число A"), ("number_b", "Число B")):
        n = col[key]
        A(f"### {title}: {n['value']}")
        A("")
        for f in ("artifact", "run", "checkpoint", "checkpoint_sha256", "instrument",
                  "decoding", "role", "verdict", "label_defect"):
            if f in n:
                A(f"- `{f}`: {n[f]}")
        A("")
    A("### Что с чем сравнимо")
    A("")
    for k, v in col["what_is_comparable_now"].items():
        A(f"- **{k}**: {v}")
    A("")
    A(f"> {col['caveat_about_the_guard']}")
    A("")
    A("## Ограничения")
    A("")
    for x in ev["limits"]:
        A(f"- {x}")
    A("")
    op = ev.get("operational")
    if op:
        A("## Операционное состояние и продолжение")
        A("")
        A(f"- Каталог цепочки (носитель): `{op['chain_dir']}` (контейнер `{op['ctr_dir']}`)")
        A(f"- Зеркало в кейсе: `{op['case_mirror']}`")
        A(f"- Сессия tmux на стенде: `{op['tmux']}`; замеры на карте 4080 — очередь "
          f"`tools/point_local_probe_chain.sh` (один GPU-замер за раз, AD-5)")
        A("- Артефакты:")
        for name, state in op["artifacts"].items():
            A(f"  - `{name}` — {state}")
        A("")
        A("**Продолжение (цепочка идемпотентна).** Повторный запуск не перемеряет снятое:")
        A("")
        A("```bash")
        for cmd in op["resume"]:
            A(cmd)
        A("```")
        A("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ts", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--report", action="store_true", help="записать REPORT.md в каталог цепочки кейса")
    args = ap.parse_args()

    ev = build(args.ts)
    out = Path(args.out) if args.out else CASE_ROOT / "evidence" / "sft-point-chain.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"сводка: {out}")
    print(json.dumps(ev["verdicts"], ensure_ascii=False, indent=2))
    for k, why in ev["not_measured"].items():
        print(f"  НЕ СНЯТО {k}: {why}")
    if args.report:
        rp = CASE_ROOT / "runs" / f"sft-point-chain-{args.ts}" / "REPORT.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(report_md(ev), encoding="utf-8")
        print(f"отчёт: {rp}")
    if ev["failed"]:
        return 1
    if ev["not_measured"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
