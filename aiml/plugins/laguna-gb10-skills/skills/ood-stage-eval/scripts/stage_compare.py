#!/usr/bin/env python3
"""Сравнение стадий лесенки на одном OOD-наборе.

    python3 stage_compare.py BASE=eval_results_base.json CPT=eval_results_cpt.json \
        SFT=eval_results_sft.json RL=eval_results.json [--md] [--pairs BASE:SFT,SFT:RL]

Для каждой стадии: попадания, доля, 95% ДИ (Wilson), judge (если валиден),
распределение вердиктов. Для соседних (или заданных) пар — точный тест
МакНемара по спаренным задачам: b = «мимо→попал», c = «попал→мимо».
Без внешних зависимостей.
"""
import argparse
import math
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evalio  # noqa: E402


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def mcnemar_exact(b, c):
    """Двусторонний точный биномиальный тест на b против c (H0: p=0.5)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    cdf = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * cdf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stages", nargs="+", help="NAME=path.json в порядке лесенки")
    ap.add_argument("--pairs", default=None, help="пары для теста, напр. BASE:SFT,SFT:RL (по умолчанию соседние + BASE:последняя)")
    ap.add_argument("--md", action="store_true", help="таблицы в Markdown")
    ap.add_argument("--field", action="append", default=[])
    a = ap.parse_args()
    fields = dict(f.split("=", 1) for f in a.field)

    names, data = [], {}
    for s in a.stages:
        name, path = s.split("=", 1)
        names.append(name)
        data[name] = {r["id"]: r for r in evalio.load(path, fields)}

    # общая сетка задач
    ids = set.intersection(*(set(d.keys()) for d in data.values()))
    if not ids:
        sys.exit("нет общих id задач между файлами — проверь поле id (--field id=...)")
    ids = sorted(ids, key=lambda x: (str(type(x)), x))
    n = len(ids)

    rows = []
    for nm in names:
        recs = [data[nm][i] for i in ids]
        hits = sum(r["hit"] for r in recs)
        lo, hi = wilson(hits, n)
        valid = [r for r in recs if evalio.judge_valid(r)]
        inv = n - len(valid)
        scores = [r["score"] for r in valid if r["score"] is not None]
        judge = (sum(scores) / len(scores)) if scores and inv / n <= 0.05 else None
        verd = Counter(r["verdict"] or "NONE" for r in recs)
        rows.append((nm, hits, lo, hi, judge, inv, verd))

    def fmt_j(j, inv):
        return f"{j:.3f}" if j is not None else f"н/д ({inv} невалид.)"

    if a.md:
        print(f"| Стадия | slug-попаданий (n={n}) | 95% ДИ | judge (валид.) | Вердикты |")
        print("|---|---|---|---|---|")
        for nm, h, lo, hi, j, inv, v in rows:
            vs = ", ".join(f"{c} {k}" for k, c in v.most_common())
            print(f"| {nm} | {h}/{n} | {lo:.0%}–{hi:.0%} | {fmt_j(j, inv)} | {vs} |")
    else:
        print(f"Задач: {n}")
        for nm, h, lo, hi, j, inv, v in rows:
            print(f"  {nm:5} попаданий {h:3}/{n}  ДИ95 [{lo:.0%}, {hi:.0%}]  judge {fmt_j(j, inv)}  "
                  f"вердикты {dict(v.most_common())}")

    if a.pairs:
        pairs = [p.split(":") for p in a.pairs.split(",")]
    else:
        pairs = [[names[i], names[i + 1]] for i in range(len(names) - 1)]
        if len(names) > 2:
            pairs.append([names[0], names[-1]])

    print()
    if a.md:
        print("| Переход | мимо→попал | попал→мимо | Δ | p (МакНемар, точный) |")
        print("|---|---|---|---|---|")
    for x, y in pairs:
        b = sum(1 for i in ids if not data[x][i]["hit"] and data[y][i]["hit"])
        c = sum(1 for i in ids if data[x][i]["hit"] and not data[y][i]["hit"])
        p = mcnemar_exact(b, c)
        verdict = "значимо" if p < 0.05 else "не различимы по попаданиям"
        if a.md:
            print(f"| {x}→{y} | {b} | {c} | {b - c:+d} | {p:.2g} ({verdict}) |")
        else:
            print(f"  {x}→{y}: мимо→попал {b}, попал→мимо {c}, Δ={b - c:+d}, p={p:.2g} — {verdict}")

    print()
    print("Напоминание: judge сравнивай только между стадиями с валидным судьёй; "
          "ноль от API_DOWN — артефакт.")


if __name__ == "__main__":
    main()
