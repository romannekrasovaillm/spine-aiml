#!/usr/bin/env python3
"""Диагностика template domination после CPT.

    python3 template_dominance.py eval_results_cpt.json [--base eval_results_base.json]
        [--corpus corpus.jsonl --corpus-field text] [--ngram 6] [--top 10]
        [--leak grpo,advantage_estimation] [--head 200]

Метрики: input attribution (any/both), доминирующие n-граммы в началах
ответов, leak rate, language drift (доля кириллицы/латиницы), при наличии
корпуса — частота тех же n-грамм/сущностей в нём.
Зависимости: только стандартная библиотека. Загрузчик eval-файлов берётся
из соседнего скилла ood-stage-eval (evalio.py) либо из копии рядом.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (HERE, os.path.join(HERE, "..", "..", "ood-stage-eval", "scripts")):
    if os.path.exists(os.path.join(cand, "evalio.py")):
        sys.path.insert(0, cand)
        break
import evalio  # noqa: E402

WORD = re.compile(r"[A-Za-zА-Яа-яЁё_][\w'’]*")


def tokens(s):
    return [t.lower() for t in WORD.findall(s)]


def ngrams(toks, n):
    return [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]


def concept_forms(slug):
    """slug → варианты упоминания: сам slug, слова через пробел, без подчёркиваний."""
    base = slug.lower()
    return {base, base.replace("_", " "), base.replace("_", "")}


def mentions(ans, slug):
    a = ans.lower()
    return any(f in a for f in concept_forms(slug))


def script_share(s):
    cyr = len(re.findall(r"[А-Яа-яЁё]", s))
    lat = len(re.findall(r"[A-Za-z]", s))
    tot = cyr + lat
    return (cyr / tot, lat / tot) if tot else (0.0, 0.0)


def analyze(recs, n, head, leak, top):
    N = len(recs)
    any_m = sum(1 for r in recs if r["expected"] and any(mentions(r["answer"], s) for s in r["expected"]))
    both_m = sum(1 for r in recs if r["expected"] and all(mentions(r["answer"], s) for s in r["expected"]))
    starts = Counter()
    for r in recs:
        t = tokens(r["answer"][:head])
        if len(t) >= n:
            starts[" ".join(t[:n])] += 1
    allng = Counter()
    for r in recs:
        allng.update(set(ngrams(tokens(r["answer"][:head]), n)))
    leaks = 0
    for r in recs:
        exp = " ".join(r["expected"]).lower()
        if any(p in r["answer"].lower() and p not in exp for p in leak):
            leaks += 1
    cyr = [script_share(r["answer"])[0] for r in recs if r["answer"].strip()]
    return {
        "n": N,
        "attribution_any": any_m / N if N else 0,
        "attribution_both": both_m / N if N else 0,
        "top_starts": starts.most_common(top),
        "top_ngrams": allng.most_common(top),
        "dominant_start_share": (starts.most_common(1)[0][1] / N) if starts and N else 0,
        "leak_rate": leaks / N if N else 0,
        "cyrillic_share_mean": sum(cyr) / len(cyr) if cyr else 0,
    }


def corpus_stats(path, field, n, leak, probes, limit=200_000):
    cnt = Counter()
    docs = 0
    leak_docs = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if docs >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                text = rec.get(field) if isinstance(rec, dict) else str(rec)
                if text is None and isinstance(rec, dict):
                    text = " ".join(str(v) for v in rec.values())
            except ValueError:
                text = line
            text = str(text)
            docs += 1
            low = text.lower()
            if any(p in low for p in leak):
                leak_docs += 1
            ng = set(ngrams(tokens(text[:2000]), n))
            for p in probes:
                if p in ng:
                    cnt[p] += 1
    return docs, leak_docs, cnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("eval_file")
    ap.add_argument("--base", default=None)
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--corpus-field", default="text")
    ap.add_argument("--ngram", type=int, default=6)
    ap.add_argument("--head", type=int, default=200, help="сколько символов начала ответа анализировать")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--leak", default="grpo,advantage_estimation")
    ap.add_argument("--field", action="append", default=[])
    a = ap.parse_args()
    fields = dict(f.split("=", 1) for f in a.field)
    leak = [p.strip().lower() for p in a.leak.split(",") if p.strip()]

    cur = analyze(evalio.load(a.eval_file, fields), a.ngram, a.head, leak, a.top)
    base = analyze(evalio.load(a.base, fields), a.ngram, a.head, leak, a.top) if a.base else None

    def row(label, key, fmt="{:.2f}"):
        b = (fmt.format(base[key]) if base else "—")
        print(f"  {label:38} {fmt.format(cur[key]):>8}   base: {b}")

    print(f"Файл: {a.eval_file}  (n={cur['n']})")
    row("input attribution (хотя бы один концепт)", "attribution_any")
    row("input attribution (оба концепта)", "attribution_both")
    row("доля ответов с топ-1 началом", "dominant_start_share")
    row(f"leak rate ({', '.join(leak)})", "leak_rate")
    row("доля кириллицы в ответах", "cyrillic_share_mean")
    print(f"\nТоп начал ответов ({a.ngram}-граммы, первые {a.head} симв.):")
    for s, c in cur["top_starts"]:
        print(f"  {c:3}  {s}")

    flags = []
    if cur["dominant_start_share"] > 0.2:
        flags.append("template domination: >20% ответов с одним началом")
    if base and cur["attribution_any"] < base["attribution_any"] - 0.2:
        flags.append("attribution упал > 0.2 относительно базы — CPT ломает чтение входа")
    if cur["attribution_any"] < 0.5:
        flags.append("attribution < 0.5 — модель в основном игнорирует вход")
    if cur["leak_rate"] > 0.05:
        flags.append("leak rate > 5% — сущности шаблона просачиваются в чужие задачи")
    if base and cur["cyrillic_share_mean"] < base["cyrillic_share_mean"] - 0.3:
        flags.append("language drift: доля кириллицы упала > 0.3 относительно базы")

    if a.corpus:
        probes = [s for s, _ in cur["top_starts"][:5]]
        docs, leak_docs, cnt = corpus_stats(a.corpus, a.corpus_field, a.ngram, leak, probes)
        print(f"\nКорпус: {docs} документов; с сущностями шаблона: {leak_docs} ({leak_docs / docs:.1%})"
              if docs else "\nКорпус пуст/не прочитан")
        for p in probes:
            print(f"  {cnt[p]:6} док.  {p}")
        if docs and leak_docs / docs > 0.02:
            flags.append(f"корпус перекошен: {leak_docs / docs:.1%} документов с сущностями шаблона (порог 2%)")

    print()
    if flags:
        for f in flags:
            print("  ! " + f)
    else:
        print("  признаков template domination не найдено")


if __name__ == "__main__":
    main()
