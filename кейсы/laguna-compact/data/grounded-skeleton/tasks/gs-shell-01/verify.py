#!/usr/bin/env python3
"""Верификатор gs-shell-01 (G3: бинарный, внешний, читает состояние, а не текст).

Что делает: считает сумму столбца `value` **сам** — по исходному (pristine)
файлу задачи, копию которого агент не может подменить, — и сравнивает с тем,
что лежит в снимке состояния (`out/total.txt`). Ни текст ответа, ни транскрипт
агента не читаются: аргумента с ними у верификатора нет вовсе.

Контракт (см. data/grounded-skeleton/card.json):
  выход — JSON {"score": 0|1, "reason": str, "expected_public": str, "reads": [str]}

`expected_public` возвращается и при score=0 — он нужен раннеру, чтобы собрать
**состязательный** текстовый ответ пробы no-action: ответ, который называет
точное ожидаемое значение словами и всё равно обязан получить 0.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ALLOWED_READ_PREFIXES = ("snapshot:", "pristine:", "journal:", "holdout:")


def expected(seed: pathlib.Path) -> int:
    total = 0
    lines = (seed / "in" / "series.csv").read_text(encoding="utf-8").splitlines()
    for n, line in enumerate(lines):
        if n == 0 or not line.strip():
            continue
        total += int(line.split(",")[1])
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, type=pathlib.Path)
    ap.add_argument("--task", required=True, type=pathlib.Path)
    ap.add_argument("--seed", required=True, type=pathlib.Path)
    ap.add_argument("--image", default="")
    ap.add_argument("--holdout", default="")
    ap.add_argument("--journal", default="")
    ap.add_argument("--scratch", default="")
    args = ap.parse_args()

    want = expected(args.seed)
    reads = ["pristine:in/series.csv", "snapshot:out/total.txt"]
    out = args.snapshot / "out" / "total.txt"
    if not out.is_file():
        return emit(0, "out/total.txt отсутствует — состояние не изменилось", want, reads)
    raw = out.read_text(encoding="utf-8", errors="replace").strip()
    toks = raw.split()
    if len(toks) != 1 or not toks[0].lstrip("-").isdigit():
        return emit(0, f"out/total.txt не является одним целым числом: {raw[:80]!r}", want, reads)
    got = int(toks[0])
    if got != want:
        return emit(0, f"сумма неверна: получено {got}, ожидалось {want}", want, reads)
    return emit(1, f"сумма {got} совпала с независимым пересчётом по исходным данным", want, reads)


def emit(score: int, reason: str, expected_public: int, reads: list[str]) -> int:
    assert score in (0, 1)
    for r in reads:
        assert r.split(":", 1)[0] + ":" in ALLOWED_READ_PREFIXES, r
    print(
        json.dumps(
            {
                "score": score,
                "reason": reason,
                "expected_public": str(expected_public),
                "reads": reads,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
