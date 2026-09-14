#!/usr/bin/env python3
"""Проверка валидности LLM-судьи в файлах eval_results_*.json.

    python3 judge_health.py eval_results_base.json eval_results_cpt.json ... [--max-invalid 0.05]
    python3 judge_health.py file.json --inspect      # показать структуру записи

Выход: по каждому файлу — доля невалидных вердиктов (API_DOWN и т.п.),
распределение вердиктов, средний score по валидным и признак «судейство
файла валидно/нет». Порог по умолчанию 5% невалидных.
"""
import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evalio  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--max-invalid", type=float, default=0.05)
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--field", action="append", default=[],
                    help="переопределение поля: expected=gold_slugs, answer=out, verdict=..., score=...")
    a = ap.parse_args()
    fields = dict(f.split("=", 1) for f in a.field)

    if a.inspect:
        for f in a.files:
            print("==", f)
            evalio.inspect(f)
        return

    bad_any = False
    for f in a.files:
        recs = evalio.load(f, fields)
        n = len(recs)
        valid = [r for r in recs if evalio.judge_valid(r)]
        inv = n - len(valid)
        frac = inv / n if n else 1.0
        verdicts = Counter(r["verdict"] or "NONE" for r in recs)
        scores = [r["score"] for r in valid if r["score"] is not None]
        mean = sum(scores) / len(scores) if scores else None
        ok = frac <= a.max_invalid and n > 0
        bad_any |= not ok
        hits = sum(r["hit"] for r in recs)
        print(f"{os.path.basename(f)}: n={n} slug-попаданий={hits} "
              f"невалидных вердиктов={inv} ({frac:.0%}) "
              f"judge={'%.3f' % mean if mean is not None else 'н/д'} "
              f"→ {'судья ВАЛИДЕН' if ok else 'судья НЕВАЛИДЕН — в отчёте «н/д»'}")
        print("   вердикты:", dict(verdicts.most_common()))
        if not ok and inv:
            print("   ! не усреднять score по выжившим; нужен ре-суд всего файла")
    sys.exit(2 if bad_any else 0)


if __name__ == "__main__":
    main()
