#!/usr/bin/env python3
"""Регрессионный гейт между прогонами (§9 SPEC.md).

Сравнивает средний `mech_score` по рукам двух прогонов. Возвращает exit 1,
если средний `mech_score` руки `spine-arch` упал более чем на порог
(по умолчанию 5 пунктов). Остальные руки печатаются как диагностика и на код
возврата не влияют.

Использование:
    python3 runners/compare_runs.py <BASELINE-RUN> <CURRENT-RUN> [--threshold 5]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import svclib  # noqa: E402

#: Рука, регресс которой валит гейт.
TRACKED_ARM = "spine-arch"


def arm_means(run_id: str) -> dict[str, float]:
    """Средний `mech_score` по рукам прогона."""
    rows = svclib.read_jsonl(svclib.run_dir(run_id) / "results.jsonl")
    buckets: dict[str, list[float]] = {}
    for row in rows:
        buckets.setdefault(row.get("arm", "?"), []).append(float(row.get("mech_score") or 0.0))
    return {arm: round(sum(v) / len(v), 3) for arm, v in buckets.items() if v}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", help="прогон-эталон (RUN-ID)")
    parser.add_argument("current", help="проверяемый прогон (RUN-ID)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="допустимое падение mech_score (пункты), дефолт 5",
    )
    args = parser.parse_args()

    base = arm_means(args.baseline)
    cur = arm_means(args.current)
    if not base or not cur:
        print("нет данных: results.jsonl одного из прогонов пуст", file=sys.stderr)
        return 2

    regressed: list[str] = []
    print(f"{'рука':<14} {'эталон':>8} {'тек':>8} {'Δ':>8}")
    for arm in sorted(set(base) | set(cur)):
        b, c = base.get(arm), cur.get(arm)
        if b is None or c is None:
            print(f"{arm:<14} {b if b is not None else '—':>8} {c if c is not None else '—':>8}      —")
            continue
        delta = round(c - b, 3)
        flag = ""
        if arm == TRACKED_ARM and delta < -args.threshold:
            flag = "  РЕГРЕСС"
            regressed.append(arm)
        print(f"{arm:<14} {b:>8} {c:>8} {delta:>+8}{flag}")

    if regressed:
        print(
            f"\nрегресс: {', '.join(regressed)} упал более чем на {args.threshold} пункта",
            file=sys.stderr,
        )
        return 1
    tracked = base.get(TRACKED_ARM)
    if tracked is not None and TRACKED_ARM in cur:
        print(
            f"\nok: {TRACKED_ARM} Δ={round(cur[TRACKED_ARM] - tracked, 3)} "
            f"(порог −{args.threshold})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
