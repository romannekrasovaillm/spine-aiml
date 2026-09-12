#!/usr/bin/env python3
"""Агрегация `results.jsonl` → `summary.json` и вердикт H0.1.

Считает средние по руке, руке×модели и задаче; парные разности средних
`mech_score` с bootstrap 95% CI (10000 ресэмплов, seed 42) для контрастов §4
SPEC.md. Вердикт H0.1 — по правилу §9, зафиксированному ДО прогона.

Сначала сверяется `prereg.lock.json`: если входы (`tasks/**`, `pack/**`, спека)
изменились после заморозки, прогон помечается `prereg_mismatch` и НЕ
агрегируется (только `--force` это перекрывает).

Использование:
    SVC_RUN=<RUN-ID> python3 runners/analyze.py [--force]
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import svclib  # noqa: E402

#: Параметры bootstrap (§9 SPEC.md).
BOOTSTRAP_N = 10000
BOOTSTRAP_SEED = 42
ALPHA = 0.05

#: Контрасты §4: `(левая рука, правая рука, смысл)`.
CONTRASTS: tuple[tuple[str, str, str], ...] = (
    ("spine-arch", "claude-plain", "H0.1 контроль: полный эффект харнесса"),
    ("spine-arch", "claude-mcp", "H0.1 основной: вклад агентного цикла"),
    ("spine-arch", "spine-min", "разложение: вклад корпуса"),
    ("claude-mcp", "claude-arch", "разложение: вклад MCP-инструментов"),
)

#: Порог «не хуже» для контрольной руки (пункты mech_score).
NON_INFERIOR_MARGIN = 5.0


def rate(rows: list[dict], pred) -> float:
    """Доля записей, удовлетворяющих предикату (0..1, с округлением)."""
    if not rows:
        return 0.0
    return round(sum(1 for r in rows if pred(r)) / len(rows), 4)


def mean(values: list[float]) -> float:
    """Среднее значение (0.0 на пустом списке)."""
    return round(sum(values) / len(values), 3) if values else 0.0


def mech_pass(row: dict) -> bool:
    """Ячейка прошла механический порог: `≥70` и без `hard_fail`."""
    return row.get("gate") == "ok" and row.get("mech_score", 0) >= 70 and not row.get("hard_fail")


def eval_bar(row: dict) -> bool:
    """Приёмка не тронута и адресный след покрыт (H0.3)."""
    return bool((row.get("eval_bar") or {}).get("coverage_ok"))


def stats(rows: list[dict]) -> dict:
    """Сводка по подмножеству ячеек."""
    scores = [float(r.get("mech_score") or 0.0) for r in rows]
    secs = [float(r["secs"]) for r in rows if isinstance(r.get("secs"), (int, float))]
    return {
        "n": len(rows),
        "mech_score_mean": mean(scores),
        "mech_pass_rate": rate(rows, mech_pass),
        "hard_fail_rate": rate(rows, lambda r: bool(r.get("hard_fail"))),
        "accountability_rate": rate(rows, lambda r: bool(r.get("accountability_ok"))),
        "eval_bar_rate": rate(rows, eval_bar),
        "skipped_no_code_rate": rate(rows, lambda r: r.get("gate") == "skipped_no_code"),
        "gen_error_rate": rate(rows, lambda r: bool(r.get("gen_error"))),
        "mean_secs": mean(secs),
    }


def group(rows: list[dict], key) -> dict[str, dict]:
    """Сгруппировать сводки по ключу."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(str(key(row)), []).append(row)
    return {name: stats(items) for name, items in sorted(buckets.items())}


def bootstrap_ci(diffs: list[float]) -> tuple[float, float]:
    """Percentile bootstrap 95% CI среднего (10000 ресэмплов, seed 42)."""
    if not diffs:
        return 0.0, 0.0
    if len(diffs) == 1:
        return round(diffs[0], 3), round(diffs[0], 3)
    rng = random.Random(BOOTSTRAP_SEED)
    k = len(diffs)
    means: list[float] = []
    for _ in range(BOOTSTRAP_N):
        total = 0.0
        for _ in range(k):
            total += diffs[rng.randrange(k)]
        means.append(total / k)
    means.sort()
    lo = means[max(0, int(ALPHA / 2 * BOOTSTRAP_N) - 1)]
    hi = means[min(BOOTSTRAP_N - 1, int((1 - ALPHA / 2) * BOOTSTRAP_N))]
    return round(lo, 3), round(hi, 3)


def pair_key(row: dict) -> tuple:
    """Ключ сопоставления для парного bootstrap: задача × модель × повтор."""
    return (row.get("task"), row.get("model"), row.get("rep"))


def contrast(rows: list[dict], left: str, right: str, label: str) -> dict:
    """Парная разность средних `mech_score` (левая − правая) с 95% CI."""
    lhs = {pair_key(r): float(r.get("mech_score") or 0.0) for r in rows if r["arm"] == left}
    rhs = {pair_key(r): float(r.get("mech_score") or 0.0) for r in rows if r["arm"] == right}
    shared = sorted(set(lhs) & set(rhs))
    paired = len(shared) >= 2
    if paired:
        diffs = [lhs[k] - rhs[k] for k in shared]
    else:
        diffs = [a - b for a in lhs.values() for b in rhs.values()]
    lo, hi = bootstrap_ci(diffs)
    return {
        "label": label,
        "left": left,
        "right": right,
        "n_left": len(lhs),
        "n_right": len(rhs),
        "n_pairs": len(shared),
        "paired": paired,
        "mean_diff": mean(diffs),
        "ci_lo": lo,
        "ci_hi": hi,
    }


def verdict(effects: dict[str, dict]) -> dict:
    """Вердикт H0.1 по правилу §9 SPEC.md (зафиксировано до прогона)."""
    primary = effects.get("spine-arch - claude-plain")
    control = effects.get("spine-arch - claude-mcp")
    if not primary or not control or not primary["n_left"] or not primary["n_right"]:
        return {"H0.1": "inconclusive", "reason": "нет данных по контрасту"}
    if control["ci_lo"] < -NON_INFERIOR_MARGIN:
        return {
            "H0.1": "rejected",
            "reason": f"spine-arch хуже claude-mcp: CI [{control['ci_lo']}, {control['ci_hi']}]",
        }
    if primary["ci_lo"] > 0:
        return {
            "H0.1": "supported",
            "reason": (
                f"spine-arch > claude-plain (CI [{primary['ci_lo']}, {primary['ci_hi']}], "
                f"нижняя граница > 0) и не хуже claude-mcp "
                f"(нижняя граница {control['ci_lo']} ≥ −{NON_INFERIOR_MARGIN})"
            ),
        }
    if primary["ci_hi"] <= 0:
        return {
            "H0.1": "rejected",
            "reason": f"CI разности с claude-plain [{primary['ci_lo']}, {primary['ci_hi']}] ≤ 0",
        }
    return {
        "H0.1": "inconclusive",
        "reason": f"CI накрывает 0: [{primary['ci_lo']}, {primary['ci_hi']}]",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", nargs="?", default="", help="идентификатор прогона (или SVC_RUN)")
    parser.add_argument("--force", action="store_true", help="агрегировать даже при prereg_mismatch")
    args = parser.parse_args()

    run_id = args.run or os.environ.get("SVC_RUN", "").strip()
    if not run_id:
        parser.error("не задан RUN-ID (аргументом или SVC_RUN)")

    current = svclib.compute_lock()
    frozen = svclib.load_lock()
    mismatches = svclib.lock_mismatches(current, frozen) if frozen else sorted(current["hashes"])

    rows = svclib.read_jsonl(svclib.run_dir(run_id) / "results.jsonl")
    if not rows:
        print(f"нет строк: {svclib.run_dir(run_id) / 'results.jsonl'}", file=sys.stderr)
        return 1

    summary: dict = {
        "run": run_id,
        "generated_at": svclib.now_iso(),
        "arch_ml_version": current.get("arch_ml_version"),
        "cells": len(rows),
        "prereg_mismatch": mismatches,
        "bootstrap": {"n": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED, "alpha": ALPHA},
        "deviations": (svclib.read_json(svclib.run_dir(run_id) / "meta.json") or {}).get(
            "deviations", []
        ),
    }

    if mismatches:
        print(
            "prereg_mismatch: входы изменились после заморозки — агрегация пропущена:",
            file=sys.stderr,
        )
        for rel in mismatches:
            print(f"  {rel}", file=sys.stderr)
        summary["aggregated"] = False
        svclib.write_json(svclib.run_dir(run_id) / "summary.json", summary)
        if not args.force:
            return 1

    summary["aggregated"] = True
    summary["by_arm"] = group(rows, lambda r: r["arm"])
    summary["by_arm_model"] = group(rows, lambda r: f"{r['arm']}|{r['model']}")
    summary["by_task"] = group(rows, lambda r: r["task"])

    effects = {
        f"{left} - {right}": contrast(rows, left, right, label)
        for left, right, label in CONTRASTS
    }
    summary["effects"] = effects
    summary["primary"] = verdict(effects)

    svclib.write_json(svclib.run_dir(run_id) / "summary.json", summary)

    print(f"ячеек: {summary['cells']}  версия харнесса: {summary['arch_ml_version']}")
    print("\nпо рукам:")
    for arm, st in summary["by_arm"].items():
        print(
            f"  {arm:<12} n={st['n']:<4} mech={st['mech_score_mean']:<7} "
            f"pass={st['mech_pass_rate']:<6} eval_bar={st['eval_bar_rate']:<6} "
            f"accountability={st['accountability_rate']:<6} "
            f"hard_fail={st['hard_fail_rate']:<6} skip={st['skipped_no_code_rate']}"
        )
    print("\nконтрасты (spine_score, bootstrap 95% CI):")
    for name, eff in effects.items():
        print(
            f"  {name:<28} Δ={eff['mean_diff']:<8} CI=[{eff['ci_lo']}, {eff['ci_hi']}] "
            f"пар={eff['n_pairs']}"
        )
    print(f"\nH0.1: {summary['primary']['H0.1']} — {summary['primary']['reason']}")
    print(f"\nзаписано: {svclib.run_dir(run_id) / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
