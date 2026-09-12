#!/usr/bin/env python3
"""mech_score.py — механические проверки answer.md в каждой ячейке.

Для каждой ячейки cells/<name>/answer.md извлекает fenced yaml-блок
mechanical_checks из RUBRICS.md задачи (fallback: runners/mech_overrides.yaml),
применяет deliverables-regex и hard_fails-regex, считает слова (вне блоков
кода и таблиц, метод A-02) -> cells/<name>/mech.json:
  {deliverables: {D1: true, ...}, hard_fail_hits: [{id, note}], words: N,
   completeness: 0..1, max_words, max_words_ok}
HF с паттерном __derived_from_deliverables__ засчитывается как hit, если
отсутствует хотя бы один deliverable. Идемпотентно (перезаписывает mech.json).
Только stdlib.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

BASE = pvlib.BASE
CELLS = pvlib.CELLS_DIR


def score_cell(cell):
    name = cell.name
    task = name.split("__")[0]
    answer_p = cell / "answer.md"
    if not answer_p.is_file():
        return None
    answer = answer_p.read_text(encoding="utf-8")
    mech, src = pvlib.load_mech(task)
    deliverables, missing = {}, []
    for e in mech["deliverables"]:
        ok = bool(re.search(e["pattern"], answer))
        deliverables[e["id"]] = ok
        if not ok:
            missing.append(e["id"])
    hits = []
    for e in mech["hard_fails"]:
        pat = e.get("pattern", "")
        if pat == "__derived_from_deliverables__":
            if missing:
                hits.append({"id": e["id"], "note": e.get("note", ""),
                             "via": f"отсутствуют {missing}"})
            continue
        if pat and re.search(pat, answer):
            hits.append({"id": e["id"], "note": e.get("note", "")})
    words = pvlib.count_words(answer)
    total = len(mech["deliverables"])
    rec = {"cell": name, "task": task, "mech_source": src,
           "deliverables": deliverables,
           "missing_deliverables": missing,
           "hard_fail_hits": hits,
           "words": words,
           "max_words": mech["max_words"],
           "max_words_ok": (words <= mech["max_words"]) if mech["max_words"] else None,
           "completeness": round((total - len(missing)) / total, 4) if total else None}
    (cell / "mech.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def main():
    n, skipped = 0, 0
    for cell in sorted(p for p in CELLS.iterdir() if p.is_dir()):
        rec = score_cell(cell)
        if rec is None:
            skipped += 1
            continue
        n += 1
        print(f"{rec['cell']}: completeness={rec['completeness']} "
              f"hf={[h['id'] for h in rec['hard_fail_hits']]} words={rec['words']}")
    print(f"\nобработано: {n}, пропущено (нет answer.md): {skipped}")


if __name__ == "__main__":
    main()
