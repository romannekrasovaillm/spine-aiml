#!/usr/bin/env python3
"""compare_runs.py — сравнение двух summary.json (базовый vs текущий прогон).

Использование:
    python3 compare_runs.py BASELINE_SUMMARY CURRENT_SUMMARY [--threshold 5]

Выход: таблица дельт по condition×model и по задачам; exit 1, если средний
total по spine-arch упал больше чем на threshold баллов (регрессия).
"""
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base = load(sys.argv[1])
    cur = load(sys.argv[2])
    threshold = 5.0
    if "--threshold" in sys.argv:
        threshold = float(sys.argv[sys.argv.index("--threshold") + 1])

    tbl_b = base.get("total_by_condition_model", {})
    tbl_c = cur.get("total_by_condition_model", {})
    print(f"{'условие × модель':40s} {'база':>8s} {'текущий':>8s} {'дельта':>8s}")
    worst = 0.0
    for key in sorted(set(tbl_b) | set(tbl_c)):
        b = (tbl_b.get(key) or {}).get("mean")
        c = (tbl_c.get(key) or {}).get("mean")
        if b is None or c is None:
            print(f"{key:40s} {str(b):>8s} {str(c):>8s}      n/a")
            continue
        d = c - b
        worst = min(worst, d) if key.startswith("spine-arch") else worst
        mark = "  <-- РЕГРЕССИЯ" if key.startswith("spine-arch") and d < -threshold else ""
        print(f"{key:40s} {b:8.1f} {c:8.1f} {d:+8.1f}{mark}")

    tasks_b = base.get("total_by_task", {})
    tasks_c = cur.get("total_by_task", {})
    print(f"\n{'задача':40s} {'база':>8s} {'текущий':>8s} {'дельта':>8s}")
    for key in sorted(set(tasks_b) | set(tasks_c)):
        b = (tasks_b.get(key) or {}).get("mean")
        c = (tasks_c.get(key) or {}).get("mean")
        if b is None or c is None:
            continue
        print(f"{key:40s} {b:8.1f} {c:8.1f} {c - b:+8.1f}")

    if worst < -threshold:
        print(f"\nИТОГ: РЕГРЕССИЯ spine-arch (худшая дельта {worst:+.1f} при "
              f"пороге {-threshold:.1f})")
        return 1
    print(f"\nИТОГ: регрессии spine-arch нет (худшая дельта {worst:+.1f}, "
          f"порог {-threshold:.1f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
