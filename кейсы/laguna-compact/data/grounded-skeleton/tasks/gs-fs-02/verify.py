#!/usr/bin/env python3
"""Верификатор gs-fs-02 (G3: бинарный, внешний, читает состояние).

Эталонную раскладку верификатор строит **сам** по исходным файлам задачи
(pristine-копия): адресат берётся из первой строки файла, содержимое — по sha256.
Сравнивается всё дерево состояния целиком, поэтому и «скопировал вместо
переместил», и «подменил содержимое», и «оставил лишнее» видны как расхождение,
а не как мнение. Текст ответа не читается.

Контракт выхода — как у остальных верификаторов набора.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

ALLOWED_READ_PREFIXES = ("snapshot:", "pristine:", "journal:", "holdout:", "reexec:")
ALLOWED_TOP_DIRS = {"sorted", "inbox"}


def sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def expected(seed: pathlib.Path) -> dict[str, str]:
    want: dict[str, str] = {}
    for f in sorted((seed / "inbox").iterdir()):
        first = f.read_text(encoding="utf-8").splitlines()[0]
        tenant = first.split(":", 1)[1].strip()
        want[f"sorted/{tenant}/{f.name}"] = sha256(f)
    return want


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
    public = "разложить по адресатам: " + ", ".join(sorted(want))
    reads = ["pristine:inbox/*", "snapshot:sorted/**", "snapshot:inbox"]

    snap = args.snapshot
    if not snap.is_dir():
        return emit(0, "снимок состояния отсутствует", public, reads, {})

    got: dict[str, str] = {}
    extra_dirs: list[str] = []
    for p in sorted(snap.rglob("*")):
        rel = p.relative_to(snap).as_posix()
        if p.is_symlink():
            return emit(0, f"в состоянии есть символическая ссылка {rel} — попытка выхода из песочницы", public, reads, {"symlink": rel})
        if p.is_dir():
            if rel.split("/")[0] not in ALLOWED_TOP_DIRS:
                extra_dirs.append(rel)
            continue
        got[rel] = sha256(p)
    if extra_dirs:
        return emit(0, f"в /work появились лишние каталоги: {extra_dirs}", public, reads, {"extra_dirs": extra_dirs})

    if set(got) != set(want):
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        detail = {"missing": missing, "extra": extra}
        return emit(0, f"дерево не совпало: нет {missing}, лишнее {extra}", public, reads, detail)
    bad = [k for k in want if got[k] != want[k]]
    if bad:
        return emit(0, f"содержимое изменено или скопировано не тем файлом: {bad}", public, reads, {"content_mismatch": bad})
    if any((snap / "inbox").iterdir()):
        return emit(0, "каталог inbox не пуст", public, reads, {})
    return emit(1, f"дерево совпало с эталонной раскладкой ({len(want)} файлов), inbox пуст", public, reads, {"files": len(want)})


if __name__ == "__main__":
    sys.exit(main())
