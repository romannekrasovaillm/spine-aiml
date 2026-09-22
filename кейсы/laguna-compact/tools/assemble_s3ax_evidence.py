#!/usr/bin/env python3
"""S3ax: свод «складываются ли два исправления» — идентичность входа и темп.

**Вопрос.** У дефекта «срыв длины» было две независимые причины, и каждая починена
отдельно: (1) стадия читала НЕ объявленный набор — v12 (44 949) вместо v13_fixed
(44 105), S3av/ADR-051; (2) темп начала стадии — постоянный LR 2e-6 снимает
усечения до уровня CPT-базы, S3aw. Комбинация «v13 + LR 2e-6» не измерена.
Здесь свод трёх состояний, снятых ОДНИМ прибором на ОДНОЙ площадке (GB10):

    v13_lr2e6   рука этой дельты: набор v13_fixed, постоянный LR 2e-6, шаг 500
    v12_lr2e6   рука сравнения: набор v12, тот же LR, тот же шаг (уже снята)
    base_cpt    CPT-финал — уровень «до SFT» на той же площадке (уже снят)

**Правило вердикта объявлено ДО чтения чисел** (`DECLARED_RULE`): единица — проба
хода, n = 104 на состояние, метрика — доля усечённых ходов. «Складываются» = второй
эффект добавляет снижение, значимое И за разрешением замера. «Неразличимо» — не
«эффекта нет»: это значит, что замер разницы такого размера не ловит (n = 104).
Формулировка в отчёт попадает целиком, вместе с ветвями, которые НЕ сработали.

**Откуда числа.** Арифметика не пересчитывается здесь: `p` и MDD берутся из свода
`tools/analyze_lr_sensitivity.py` (дельты S3aw), который импортирует z-тест и MDD из
замороженных копий (`runs/lr-sensitivity-20260921/reference/`). Метрики состояний —
из отчётов прибора (`aggregate`), петли — из переписи `analyze_loop_origin`. Вторая
реализация одного правила разошлась бы с первой молча, поэтому её здесь нет.

Коды возврата::

    0 — свод собран
    1 — отказ: несопоставимый вход (прибор/протокол/площадка) или нет обязательного входа
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Правило вердикта. Объявлено здесь — до чтения чисел, и попадает в отчёт текстом.
DECLARED_RULE = {
    "question": ("складываются ли два исправления: даёт ли набор v13_fixed "
                 "дополнительное снижение усечений к руке «v12 + LR 2e-6»"),
    "unit": "проба хода; n = 104 на состояние",
    "metric": "доля усечённых ходов (stop_reason != turn_end), aggregate.truncated_share",
    "delta": "Δ = доля(v13_lr2e6) − доля(v12_lr2e6); отрицательное Δ — v13 ниже",
    "confirm": ("Δ < 0 И p < 0.05 (двусторонне) И |Δ| ≥ MDD → ИСПРАВЛЕНИЯ СКЛАДЫВАЮТСЯ: "
                "второй эффект добавляет снижение, замер его ловит"),
    "indistinguishable": ("направление любое, но |Δ| < MDD или p ≥ 0.05 → НЕРАЗЛИЧИМО: "
                          "замер разницы такого размера не ловит. Это НЕ «эффекта нет»; "
                          "граница названа числом MDD, а не словом"),
    "refute": ("Δ > 0 И p < 0.05 И |Δ| ≥ MDD → ПРОТИВОРЕЧИТ: набор v13 значимо хуже "
               "набора v12 на том же темпе"),
    "alpha": 0.05,
    "mdd": "минимальная различимая разница долей, мощность 0.8, α = 0.05, n = 104",
    "secondary": ("незакрытый <think> среди естественно завершённых (ADR-045 п.5) и петли "
                  "(блоки, различные тексты, по сегментам) — поддерживающие метрики: они "
                  "не отменяют вердикт по усечениям, но обязаны быть названы"),
    "sanity": "состояние руки не должно быть значимо ВЫШЕ базы по усечениям (уровень CPT не потерян)",
}

EXIT_OK, EXIT_FAIL = 0, 1


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def kv(pairs, what: str) -> dict:
    """`tag=value` → словарь. Дубли тега — отказ: молчаливая перезапись скрыла бы состояние."""
    out: dict = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"ОТКАЗ: {what} задан не как tag=value: {item!r}")
        tag, val = item.split("=", 1)
        if tag in out:
            raise SystemExit(f"ОТКАЗ: {what} «{tag}» задан дважды")
        out[tag] = val
    return out


def state_numbers(report: dict, tag: str) -> dict:
    """Числа состояния — из `aggregate` отчёта прибора, без пересчёта."""
    st = report["states"][tag]
    agg = st["aggregate"]
    stop = agg["stop"]
    return {
        "checkpoint_sha256": st.get("checkpoint_sha256"),
        "n_probes": agg["n"],
        #: k восстанавливается из доли прибора: сам прибор печатает долю, а не счётчик
        #: (доля округлена до 4 знаков — отсюда round, а не «точное» умножение).
        "truncated": {"k": round(stop["truncated_share"] * agg["n"]),
                      "k_n": f"{round(stop['truncated_share'] * agg['n'])}/{agg['n']}",
                      "share": stop["truncated_share"]},
        "stop_reasons": dict(stop["reasons"]),
        "stop_reasons_share": dict(stop["reasons_share"]),
        "natural_stop_share": stop["natural_stop_share"],
        "unclosed_think_all": agg["unclosed_think_share"],
        "unclosed_think_natural": stop["natural"]["unclosed_think_share"],
        "n_natural": stop["natural"]["n"],
        "lengths": agg["lengths"],
        "instrument_looped_share": agg["looped_share"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="append", default=[],
                    help="tag=<отчёт прибора> (v13_lr2e6 | v12_lr2e6 | base_cpt)")
    ap.add_argument("--report-state", action="append", default=[],
                    help="tag=<имя состояния ВНУТРИ отчёта> (по умолчанию совпадает с тегом)")
    ap.add_argument("--census", action="append", default=[],
                    help="tag=<перепись петель того же состояния>")
    ap.add_argument("--analyze", required=True, help="свод analyze_lr_sensitivity.py (JSON)")
    ap.add_argument("--run-manifest", required=True,
                    help="run_manifest.json стадии руки — блок sft_input (факт входа)")
    ap.add_argument("--identity-guard", required=True,
                    help="вердикт tools/check_dataset_identity.py --json (страж C-030)")
    ap.add_argument("--arm-run", required=True, help="каталог прогона руки на стенде")
    ap.add_argument("--arm-patch", required=True, help="arm_patch.json каталога прогона")
    ap.add_argument("--repeat", default=None,
                    help="tag=<отчёт ПОВТОРА того же чекпойнта в другой сессии> — "
                         "проверка переносимости числа через границу сессий")
    ap.add_argument("--repeat-of", default=None, help="tag=<отчёт, который повторяется>")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    reports = kv(a.report, "состояние")
    states = kv(a.report_state, "имя состояния") if a.report_state else {}
    censuses = kv(a.census, "перепись")
    states = {t: states.get(t, t) for t in reports}

    # ── отказ вместо мягкого свода: без обязательных входов отчёт был бы пустым ──
    for tag in ("v13_lr2e6", "v12_lr2e6", "base_cpt"):
        if tag not in reports:
            raise SystemExit(f"ОТКАЗ: нет состояния {tag} (--report {tag}=...)")
    analyze = json.loads(Path(a.analyze).read_text(encoding="utf-8"))
    comp = analyze.get("comparability", {})
    if comp.get("verdict") != "comparable":
        raise SystemExit(f"ОТКАЗ: свод несопоставим: {comp.get('verdict')} "
                         f"{comp.get('mismatches', [])} {comp.get('off_expectation', [])}")

    manifest = json.loads(Path(a.run_manifest).read_text(encoding="utf-8"))
    guard = json.loads(Path(a.identity_guard).read_text(encoding="utf-8"))
    patch = json.loads(Path(a.arm_patch).read_text(encoding="utf-8"))

    # ── 1. Идентичность входа: факт против объявления (S3av, ADR-051) ───────────
    si = manifest.get("sft_input")
    if not si:
        raise SystemExit("ОТКАЗ: в манифесте стадии нет блока sft_input — идентичность "
                         "входа не доказана, опыт бессмысленен (ADR-051)")
    declared = str(si.get("declared_sha256") or "").lower()
    actual = str(si["jsonl"]["sha256"]).lower()
    identity = {
        "declared_path": si.get("declared_path"),
        "declared_sha256": declared,
        "read_jsonl_path": si["jsonl"]["path"],
        "read_jsonl_sha256": actual,
        "read_jsonl_samples": si["jsonl"]["samples"],
        "hash_scope": si["jsonl"]["hash_scope"],
        "read_tensor_path": si["tensor"]["path"],
        "read_tensor_sha256": si["tensor"]["sha256"],
        "read_tensor_samples": si["tensor"]["samples"],
        "declared_equals_read": declared == actual,
        "recorded_at": si.get("recorded_at"),
        "expected_v13_jsonl_sha256":
            "71c4bd2b011b210b906e7c77eca373ad094c14264b86353189faaa0596983b01",
        "expected_v13_tensor_sha256":
            "dc3d4838d15b83c3f1abefb549473e1efd1dc822000a675e34f548f759b94d75",
        "guard_c030": {"tool": guard.get("tool"), "passed": guard.get("passed"),
                       "summary": guard.get("summary"), "runs_checked": guard.get("runs_checked"),
                       "runs_legacy": guard.get("runs_legacy"),
                       "findings": guard.get("findings")},
    }
    identity["is_v13_fixed"] = (actual == identity["expected_v13_jsonl_sha256"]
                                and identity["read_tensor_sha256"]
                                == identity["expected_v13_tensor_sha256"]
                                and si["jsonl"]["samples"] == 44105)

    # ── 2. Числа состояний и сравнения (арифметика — из свода, не своя) ─────────
    per_state = {}
    for tag, path in reports.items():
        rep = json.loads(Path(path).read_text(encoding="utf-8"))
        per_state[tag] = {
            "report": path, "report_sha256": sha256_file(Path(path)),
            "state_in_report": states[tag],
            "tool_sha256": rep.get("tool_sha256"),
            "device": rep.get("device"),
            **state_numbers(rep, states[tag]),
        }
    comparisons = analyze["comparisons"]
    cmp_trunc = comparisons["arm_vs_control"]["truncated"]
    #: Свод называет состояния arm/control/base — здесь они названы своими именами.
    cmp_map = {"arm": "v13_lr2e6", "control": "v12_lr2e6", "base": "base_cpt"}

    # ── 3. Вердикт по объявленному правилу ──────────────────────────────────────
    d, p, mdd = cmp_trunc["delta"], cmp_trunc["p"], cmp_trunc["mdd"]
    sig, beyond = cmp_trunc["significant"], cmp_trunc["beyond_mdd"]
    if d < 0 and sig and beyond:
        verdict, branch = "складываются", "confirm"
    elif d > 0 and sig and beyond:
        verdict, branch = "противоречит", "refute"
    else:
        verdict, branch = "неразличимо", "indistinguishable"
    base_cmp = comparisons["arm_vs_base"]["truncated"]
    sanity_lost = base_cmp["delta"] > 0 and base_cmp["significant"] and base_cmp["beyond_mdd"]

    # ── 4. Петли по состояниям (из переписи, тем же определением) ───────────────
    loops = {}
    for tag, path in censuses.items():
        c = json.loads(Path(path).read_text(encoding="utf-8"))
        st = next(iter(c["blocks"]["by_state"].values()))
        sub = next(iter(st.values()))
        loops[tag] = {
            "census": path, "census_sha256": sha256_file(Path(path)),
            "census_tool_sha256": c.get("tool_sha256"),
            "blocks_total": sub["blocks"], "blocks_strong": sub["blocks_strong"],
            "distinct": sub["distinct_blocks"], "n_probes": sub["n_probes"],
            "probes_with_loop": {"k": round(sub["instrument_loop_share"] * sub["n_probes"]),
                                 "n": sub["n_probes"], "share": sub["instrument_loop_share"]},
            "by_region_blocks": c["blocks"].get("by_region"),
            "by_region_distinct": c["blocks"].get("by_region_distinct"),
            "origin_verdict": c.get("answer", {}).get("verdict"),
        }

    evidence = {
        "schema": "sft-v13-arm/1",
        "node": "S3ax",
        "date": datetime.now(timezone.utc).isoformat(),
        "branch": "arch/sft-v13-arm",
        "question": ("складываются ли два исправления дефекта «срыв длины»: идентичность "
                     "входа (v13_fixed вместо v12, S3av/ADR-051) и пониженный темп "
                     "(постоянный LR 2e-6, S3aw) — даёт ли первое дополнительное снижение "
                     "усечений, когда второе уже применено"),
        "declared_rule": DECLARED_RULE,
        "answers": {
            "read_v13": {
                "value": identity["is_v13_fixed"],
                "what_it_means": ("стадия ПРОЧИТАЛА объявленный набор v13_fixed: полные "
                                  "sha256 jsonl и тензора совпали с объявленными, число "
                                  "примеров 44 105 — правка литерала (S3av) сработала"),
                "proof": {"declared_equals_read": identity["declared_equals_read"],
                          "read_jsonl_sha256": identity["read_jsonl_sha256"],
                          "read_tensor_sha256": identity["read_tensor_sha256"],
                          "samples": identity["read_jsonl_samples"],
                          "guard_C030": identity["guard_c030"]["passed"]},
            },
            "effects_add_up": {
                "value": verdict,
                "branch_fired": branch,
                "numbers": {
                    "metric": "доля усечённых ходов",
                    "v13_lr2e6": f"{round(cmp_trunc['share_a'] * cmp_trunc['n_a'])}/{cmp_trunc['n_a']}",
                    "v13_lr2e6_share": cmp_trunc["share_a"],
                    "v12_lr2e6": f"{round(cmp_trunc['share_b'] * cmp_trunc['n_b'])}/{cmp_trunc['n_b']}",
                    "v12_lr2e6_share": cmp_trunc["share_b"],
                    "delta": d, "p": p, "mdd": mdd,
                    "significant": sig, "beyond_mdd": beyond,
                },
                "why": _why(verdict, d, p, mdd, sig, beyond),
                "sanity_base_level": {
                    "base_cpt": f"{round(base_cmp['share_b'] * base_cmp['n_b'])}/{base_cmp['n_b']}",
                    "base_cpt_share": base_cmp["share_b"],
                    "delta_vs_base": base_cmp["delta"], "p_vs_base": base_cmp["p"],
                    "mdd_vs_base": base_cmp["mdd"],
                    "arm_above_base_significantly": sanity_lost,
                },
            },
        },
        "identity": identity,
        "states": per_state,
        #: Статус свода сравнений приводится как есть: `partial` там означает, что
        #: НЕОБЯЗАТЕЛЬНАЯ ось не заполнена (мост устройств, не снятый повтор), а не
        #: что сравнение трёх состояний неполно. Умолчать об этом значило бы выдать
        #: частичный свод за полный.
        "comparisons_status": {"status": analyze.get("status"),
                               "reasons": analyze.get("status_reasons"),
                               "tool": analyze.get("tool"),
                               "tool_sha256": analyze.get("tool_sha256"),
                               "comparability": {"verdict": comp.get("verdict"),
                                                 "device_uniform": comp.get("device_uniform")}},
        "comparisons": comparisons,
        "loops": loops,
        "arm": {"run_dir": a.arm_run, "patch": patch.get("arm"),
                "patch_tool": patch.get("tool"),
                "pipeline_sha256_after": patch["edits"]["pipeline"]["sha256_after"],
                "chain_sha256_after": patch["edits"]["chain"]["sha256_after"],
                "artifact": patch["stages_tsv"]["artifact_after"],
                "guard_sha256": {k: v["sha256"] for k, v in patch["guard"].items()}},
        "facts": [
            {"fact": "каждая проба прибора перезаписывает манифест каталога целиком",
             "consequence": ("манифест односостоянийный: он описывает ПОСЛЕДНЮЮ снятую пробу, "
                             "а не все состояния сессии; читать его как «манифест прогона» нельзя"),
             "status": "не чинится здесь — дефект стенда/прибора, вне дельты"},
            {"fact": ("шаг манифеста AD-2 в цепочке проб падает с "
                      "ModuleNotFoundError: No module named 'numpy'"),
             "where": "обвязка проб: `python3 probe_language_split.py` на хосте тянет "
                      "probe_control, а у хостового python3 нет numpy",
             "consequence": ("манифест AD-2 для сессии проб не пишется; отчёты приборов "
                             "самодостаточны (в них checkpoint_sha256 и tool_sha256)"),
             "status": "не чинится здесь — вне дельты (ADR-016 п.1: под живой цепочкой не правят)"},
        ],
        "boundaries": [
            "одна рука, одна точка (шаг 500), один сид — полная стадия этим не проверяется",
            "замер на инференсе: обучение не перемеряется, читается его артефакт",
            "n = 104 пробы на состояние: Δ меньше MDD объявлено неразличимым, а не отсутствующим",
            "«неразличимо» по усечениям не означает «v13 не нужен»: идентичность входа — "
            "требование AD-2/ADR-051 само по себе, независимо от эффекта на длину",
        ],
        "open_questions": [
            "отделить вклад набора от вклада темпа на ОДНОЙ точке нельзя: нужна вторая точка "
            "или перекрёстный план (v13 при LR 1e-5)",
        ],
    }
    evidence["session_boundary"] = session_check(a.repeat, a.repeat_of)
    out = Path(a.out)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "read_v13": identity["is_v13_fixed"],
                      "effects_add_up": verdict, "delta": d, "p": p, "mdd": mdd},
                     ensure_ascii=False, indent=1))
    return EXIT_OK


def session_check(repeat_spec: str | None, of_spec: str | None) -> dict:
    """Переносится ли число через границу СЕССИЙ на одной площадке.

    Сопоставимость замеров разных сессий принималась как следствие детерминизма
    прибора на устройстве, но не проверялась. Проверка дешёвая: тот же чекпойнт
    меряется второй раз в другой сессии. Сравниваются и метрики, и сами ответы —
    побайтово: расхождение ответов при совпавших метриках тоже несовпадение.
    """
    if not repeat_spec:
        return {"checked": False,
                "why": "повтор не снят: уступка не проверена, а названа (см. boundaries)"}
    tag, rp = repeat_spec.split("=", 1)
    otag, op = (of_spec or "").split("=", 1) if of_spec else (tag, None)
    rep_new = json.loads(Path(rp).read_text(encoding="utf-8"))
    n = state_numbers(rep_new, tag)
    out = {"checked": True, "repeat": {"tag": tag, "report": rp,
                                       "sha256": sha256_file(Path(rp)),
                                       "device": rep_new.get("device"),
                                       "truncated_k_n": n["truncated"]["k_n"],
                                       "unclosed_think_natural": n["unclosed_think_natural"]}}
    if op:
        rep_old = json.loads(Path(op).read_text(encoding="utf-8"))
        o = state_numbers(rep_old, otag)
        same_ckpt = (n["checkpoint_sha256"] == o["checkpoint_sha256"])
        new_p = rep_new["states"][tag]["probes"]
        old_p = rep_old["states"][otag]["probes"]
        identical = sum(1 for x, y in zip(new_p, old_p) if x["response"] == y["response"])
        out["original"] = {"tag": otag, "report": op, "sha256": sha256_file(Path(op)),
                           "truncated_k_n": o["truncated"]["k_n"],
                           "unclosed_think_natural": o["unclosed_think_natural"]}
        out.update({
            "same_checkpoint": same_ckpt,
            "truncated_delta": round(n["truncated"]["share"] - o["truncated"]["share"], 4),
            "unclosed_natural_delta": round(n["unclosed_think_natural"]
                                            - o["unclosed_think_natural"], 4),
            "responses_byte_identical": identical, "n_probes": len(new_p),
            "reading": ("чекпойнт тот же и ответы совпали побайтово: число переносится через "
                        "границу сессий, и сравнение новой руки с прежними замерами "
                        "действительно" if same_ckpt and identical == len(new_p) else
                        "ответы разошлись: числа разных сессий на одной площадке ставить "
                        "рядом нельзя без поправки — сравнение новой руки с прежними "
                        "замерами ослаблено, и это названо, а не умолчано"),
        })
    return out


def _why(verdict: str, d: float, p: float, mdd: float, sig: bool, beyond: bool) -> str:
    if verdict == "складываются":
        return (f"Δ = {d:+.4f} < 0, p = {p:.2e} < 0.05, |Δ| ≥ MDD = {mdd:.4f}: набор v13 "
                f"добавляет снижение усечений к руке «v12 + LR 2e-6», и замер его ловит")
    if verdict == "противоречит":
        return (f"Δ = {d:+.4f} > 0, p = {p:.2e} < 0.05, |Δ| ≥ MDD = {mdd:.4f}: на том же "
                f"темпе набор v13 значимо ХУЖЕ v12 — исправления не складываются, а спорят")
    return (f"Δ = {d:+.4f}, p = {p:.4f}, MDD = {mdd:.4f} (значимо: {sig}, за разрешением: "
            f"{beyond}): разница в пределах разрешения замера при n = 104 — НЕРАЗЛИЧИМО. "
            f"Это утверждение о замере, а не об отсутствии эффекта: чтобы поймать разницу "
            f"такого размера, нужен больший n или меньший разброс")


if __name__ == "__main__":
    raise SystemExit(main())
