#!/usr/bin/env python3
"""Живые примеры «до/после» по стадиям лесенки.

    python3 extract_examples.py --stages BASE=... CPT=... SFT=... RL=... \
        [--pick transitions|ids|leaks] [--ids 17,52,19] [--n 3] [--maxlen 300] \
        [--leak-pattern grpo,advantage_estimation] [--json out.json]

Режимы выбора:
  transitions — задачи, где попадание появилось между стадиями (мимо→попал),
                приоритет тем, где путь длиннее (BASE ✗ → ... → RL ✓)
  ids         — явный список задач
  leaks       — задачи, где в ответе есть «утечка» шаблона (--leak-pattern),
                а ожидаемые slug'и другие (для cpt-template-check)
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (os.path.join(HERE, "..", "..", "..", "lib"), HERE):
    if os.path.exists(os.path.join(cand, "evalio.py")):
        sys.path.insert(0, cand)
        break
import evalio  # noqa: E402


def clip(s, n):
    s = re.sub(r"\s*\n+\s*", " · ", s.strip())
    return s if len(s) <= n else s[:n] + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", nargs="+", required=True)
    ap.add_argument("--pick", default="transitions", choices=["transitions", "ids", "leaks"])
    ap.add_argument("--ids", default="")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--maxlen", type=int, default=300)
    ap.add_argument("--leak-pattern", default="grpo,advantage_estimation")
    ap.add_argument("--json", default=None)
    ap.add_argument("--field", action="append", default=[])
    a = ap.parse_args()
    fields = dict(f.split("=", 1) for f in a.field)

    names, data = [], {}
    for s in a.stages:
        nm, path = s.split("=", 1)
        names.append(nm)
        data[nm] = {r["id"]: r for r in evalio.load(path, fields)}
    ids = sorted(set.intersection(*(set(d) for d in data.values())), key=str)

    def path_of(i):
        return [data[nm][i]["hit"] for nm in names]

    if a.pick == "ids":
        chosen = [type(ids[0])(x) if ids and not isinstance(ids[0], str) else x
                  for x in a.ids.split(",") if x]
    elif a.pick == "leaks":
        pats = [p.strip().lower() for p in a.leak_pattern.split(",") if p.strip()]
        chosen = []
        for i in ids:
            for nm in names:
                r = data[nm][i]
                exp = " ".join(r["expected"]).lower()
                ans = r["answer"].lower()
                if any(p in ans and p not in exp for p in pats):
                    chosen.append(i)
                    break
        chosen = chosen[:a.n]
    else:
        scored = []
        for i in ids:
            p = path_of(i)
            if not p[-1] or all(p):
                continue
            first_hit = p.index(True)
            # длиннее путь до первого попадания — интереснее пример
            scored.append((first_hit, sum(len(data[nm][i]["answer"]) for nm in names), i))
        scored.sort(reverse=True)
        chosen = [i for _, _, i in scored[:a.n]]

    out = []
    for i in chosen:
        exp = data[names[0]][i]["expected"]
        print(f"\n**Задача #{i}** — ожидаемые slug'и: {', '.join(exp)}")
        item = {"id": i, "expected": exp, "stages": {}}
        for nm in names:
            r = data[nm][i]
            mark = "✅ попал" if r["hit"] else "❌ мимо"
            v = r["verdict"] or "—"
            note = " (судья упал)" if not evalio.judge_valid(r) else ""
            sc = f"score={r['score']:.2f}" if r["score"] is not None else "score=—"
            print(f"[{nm}] {mark} · {v}{note} · {sc} — {clip(r['answer'], a.maxlen)}")
            item["stages"][nm] = {"hit": r["hit"], "verdict": v, "judge_valid": evalio.judge_valid(r),
                                  "score": r["score"], "answer": clip(r["answer"], a.maxlen)}
        out.append(item)

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\nсохранено: {a.json}")


if __name__ == "__main__":
    main()
