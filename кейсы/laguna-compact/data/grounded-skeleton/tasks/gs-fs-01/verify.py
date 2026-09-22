#!/usr/bin/env python3
"""Верификатор gs-fs-01 (G3: бинарный, внешний, читает состояние).

Ожидаемое значение верификатор собирает **сам** из исходных файлов задачи
(pristine-копия, не снимок состояния): если агент подменит src/*.txt в песочнице,
сверка с неизменённым оригиналом это покажет. Читается ровно один артефакт
состояния — app/config/service.json; текст ответа и транскрипт не читаются.

Контракт выхода — как у остальных верификаторов набора.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ALLOWED_READ_PREFIXES = ("snapshot:", "pristine:", "journal:", "holdout:", "reexec:")


def expected(seed: pathlib.Path) -> dict:
    return {
        "name": (seed / "src" / "name.txt").read_text(encoding="utf-8").strip(),
        "port": int((seed / "src" / "port.txt").read_text(encoding="utf-8").strip()),
        "flags": [
            line.strip()
            for line in (seed / "src" / "flags.txt").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ],
    }


def emit(score: int, reason: str, expected_public: str, reads: list[str], detail: dict) -> int:
    assert score in (0, 1)
    for r in reads:
        assert r.split(":", 1)[0] + ":" in ALLOWED_READ_PREFIXES, r
    print(
        json.dumps(
            {
                "score": score,
                "reason": reason,
                "expected_public": expected_public,
                "reads": reads,
                "detail": detail,
            },
            ensure_ascii=False,
        )
    )
    return 0


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

    _ = args.scratch  # зарезервирован под ре-execution верификаторы (см. gs-shell-02)

    want = expected(args.seed)
    public = json.dumps(want, ensure_ascii=False)
    reads = [
        "pristine:src/name.txt",
        "pristine:src/port.txt",
        "pristine:src/flags.txt",
        "snapshot:app/config/service.json",
    ]
    art = args.snapshot / "app" / "config" / "service.json"
    if not art.is_file():
        return emit(0, "app/config/service.json отсутствует — состояние не изменилось", public, reads, {})
    try:
        got = json.loads(art.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — диагностика важнее класса
        return emit(0, f"service.json не разбирается строгим JSON: {exc}", public, reads, {})
    detail = {"got": got}
    if not isinstance(got, dict):
        return emit(0, "service.json — не объект JSON", public, reads, detail)
    if set(got) != set(want):
        return emit(0, f"ключи {sorted(got)} вместо {sorted(want)}", public, reads, detail)
    if not isinstance(got.get("port"), int) or isinstance(got.get("port"), bool):
        return emit(0, f"port должен быть числом, получено {type(got.get('port')).__name__}", public, reads, detail)
    for key in ("name", "port", "flags"):
        if got[key] != want[key]:
            return emit(0, f"значение {key!r} не совпало с источником: {got[key]!r} != {want[key]!r}", public, reads, detail)
    return emit(1, "service.json собран верно из трёх источников", public, reads, detail)


if __name__ == "__main__":
    sys.exit(main())
