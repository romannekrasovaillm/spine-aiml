#!/usr/bin/env python3
"""analyze.py — сбор results.jsonl -> summary.json + сводные таблицы.

Для всех ячеек: task, condition, model, rep, mech (completeness, HF-хиты,
слова), judge totals, secs, bytes.
Таблицы: mean±sd total по condition×model, по задачам, completeness, HF-rate;
эффекты (разности средних + bootstrap 95% CI, 10000 ресэмплов, seed=42):
  spine-arch vs theseus-plain, spine-arch vs claude-plain,
  theseus-arch vs theseus-plain, claude-arch vs claude-plain.
Только stdlib.
"""
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

BASE = pvlib.BASE
CELLS = pvlib.CELLS_DIR
EFFECTS = [
    ("spine-arch", "theseus-plain"),
    ("spine-arch", "claude-plain"),
    ("spine-arch", "spine-min"),
    ("theseus-arch", "theseus-plain"),
    ("claude-arch", "claude-plain"),
]
BOOT_N = 10000
SEED = 42


def load_json(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def collect():
    records = []
    for cell in sorted(p for p in CELLS.iterdir() if p.is_dir()):
        meta = load_json(cell / "meta.json") or {}
        mech = load_json(cell / "mech.json")
        judge = load_json(cell / "judge.json")
        rec = {"cell": cell.name,
               "task": meta.get("task"), "condition": meta.get("condition"),
               "model": meta.get("model"), "rep": meta.get("rep"),
               "secs": meta.get("secs"), "bytes": meta.get("bytes"),
               "gen_error": meta.get("error")}
        if mech:
            rec["completeness"] = mech.get("completeness")
            rec["words"] = mech.get("words")
            rec["max_words_ok"] = mech.get("max_words_ok")
            rec["mech_hf"] = [h["id"] for h in mech.get("hard_fail_hits", [])]
        if judge:
            rec["judge_total"] = judge.get("total")
            rec["judge_hf"] = judge.get("hf_triggered")
            rec["judge_k"] = judge.get("k")
            rec["evidence_unverified"] = judge.get("evidence_unverified")
        records.append(rec)
    return records


def mean_sd(vals):
    if not vals:
        return None
    m = statistics.mean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return round(m, 2), round(sd, 2), len(vals)


def bootstrap_diff(a, b, rng):
    """95% CI разности mean(a)-mean(b), ресемплинг внутри групп."""
    if not a or not b:
        return None
    diffs = []
    for _ in range(BOOT_N):
        sa = [rng.choice(a) for _ in a]
        sb = [rng.choice(b) for _ in b]
        diffs.append(statistics.mean(sa) - statistics.mean(sb))
    diffs.sort()
    return {"diff": round(statistics.mean(a) - statistics.mean(b), 2),
            "ci_lo": round(diffs[int(0.025 * BOOT_N)], 2),
            "ci_hi": round(diffs[int(0.975 * BOOT_N) - 1], 2),
            "n_a": len(a), "n_b": len(b)}


def main():
    records = collect()
    with (pvlib.RUNS / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    judged = [r for r in records if r.get("judge_total") is not None]
    rng = random.Random(SEED)

    # total по condition×model
    by_cm = defaultdict(list)
    for r in judged:
        by_cm[(r["condition"], r["model"])].append(r["judge_total"])
    tbl_cm = {f"{c}|{m}": mean_sd(v) for (c, m), v in sorted(by_cm.items())}

    # total по задачам
    by_task = defaultdict(list)
    for r in judged:
        by_task[r["task"]].append(r["judge_total"])
    tbl_task = {t: mean_sd(v) for t, v in sorted(by_task.items())}

    # completeness и HF-rate по условиям (основной массив)
    by_cond = defaultdict(list)
    for r in records:
        if r.get("completeness") is not None:
            by_cond[r["condition"]].append(r)
    tbl_cond = {}
    for cond, rs in sorted(by_cond.items()):
        hj = [r for r in rs if r.get("judge_hf")]
        tbl_cond[cond] = {
            "n": len(rs),
            "completeness": round(statistics.mean(r["completeness"] for r in rs), 4),
            "hf_rate_judge": round(len(hj) / len(rs), 4),
            "evidence_unverified": round(statistics.mean(
                r["evidence_unverified"] for r in rs
                if r.get("evidence_unverified") is not None), 4) or None,
            "mean_secs": round(statistics.mean(
                r["secs"] for r in rs if r.get("secs") is not None), 1) or None,
        }

    # эффекты (основной массив: условия × обе модели, пулом)
    effects = {}
    for a, b in EFFECTS:
        va = [r["judge_total"] for r in judged
              if r["condition"] == a and r["model"] in ("dsf", "glm")]
        vb = [r["judge_total"] for r in judged
              if r["condition"] == b and r["model"] in ("dsf", "glm")]
        effects[f"{a} - {b}"] = bootstrap_diff(va, vb, rng)
    # эффект спайна по моделям раздельно (H3: модель-зависимость)
    effects_by_model = {}
    for model in ("dsf", "glm"):
        for a, b in (("spine-arch", "theseus-plain"),):
            va = [r["judge_total"] for r in judged
                  if r["condition"] == a and r["model"] == model]
            vb = [r["judge_total"] for r in judged
                  if r["condition"] == b and r["model"] == model]
            effects_by_model[f"{a} - {b} | {model}"] = bootstrap_diff(va, vb, rng)

    failed = [r["cell"] for r in records if r.get("gen_error") or
              not (CELLS / r["cell"] / "answer.md").is_file()]
    summary = {"cells_total": len(records), "judged": len(judged),
               "failed_or_missing": failed,
               "total_by_condition_model": tbl_cm,
               "total_by_task": tbl_task,
               "by_condition": tbl_cond,
               "effects": effects, "effects_by_model": effects_by_model,
               "bootstrap": {"n": BOOT_N, "seed": SEED}}
    (pvlib.RUNS / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- печать ---
    print(f"ячеек: {len(records)}, оценено судьёй: {len(judged)}, "
          f"без ответа/сбоев: {len(failed)}")
    if failed:
        print("сбойные:", failed)
    print("\n== Judge total (0-100) по condition × model: mean±sd (n) ==")
    for k, v in tbl_cm.items():
        print(f"  {k:32s} {v[0]:6.2f} ± {v[1]:5.2f} (n={v[2]})" if v else f"  {k}: нет данных")
    print("\n== Judge total по задачам ==")
    for k, v in tbl_task.items():
        print(f"  {k:16s} {v[0]:6.2f} ± {v[1]:5.2f} (n={v[2]})" if v else f"  {k}: нет данных")
    print("\n== Completeness / HF-rate / evidence_unverified / время по условиям ==")
    for cond, d in tbl_cond.items():
        print(f"  {cond:18s} n={d['n']:3d} compl={d['completeness']:.3f} "
              f"HF={d['hf_rate_judge']:.2f} ev_unv={d['evidence_unverified']} "
              f"secs={d['mean_secs']}")
    print("\n== Эффекты (разность средних, bootstrap 95% CI) ==")
    for k, v in list(effects.items()) + list(effects_by_model.items()):
        print(f"  {k:36s} {v['diff']:+6.2f} [{v['ci_lo']:+6.2f}; {v['ci_hi']:+6.2f}] "
              f"(n={v['n_a']} vs {v['n_b']})" if v else f"  {k}: нет данных")
    print("\nзаписано: results.jsonl, summary.json")


if __name__ == "__main__":
    main()
