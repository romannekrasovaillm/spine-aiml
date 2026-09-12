#!/usr/bin/env python3
"""Заморозка входов бенчмарка: `prereg.lock.json` + таблица хэшей.

Пишет машиночитаемый слепок sha256 всех входов (задачи, гейты, корпус,
спека, пререгистрация) и печатает таблицу. С `--write` вписывает ту же
таблицу в `PREREGISTRATION.md` между маркерами `HASH-TABLE-START/END`.

Запускается ДО первой генерации. Любая правка `tasks/**` или `pack/**` после
этого инвалидирует слепок: `analyze.py` пометит прогон `prereg_mismatch`.

Использование:
    python3 runners/freeze.py            # записать слепок, напечатать таблицу
    python3 runners/freeze.py --write    # ещё и обновить PREREGISTRATION.md
    python3 runners/freeze.py --check    # только сверить, exit 1 при расхождении
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import svclib  # noqa: E402

START_MARK = "<!-- HASH-TABLE-START -->"
END_MARK = "<!-- HASH-TABLE-END -->"

#: Файл, в который вписывается таблица: свой хэш в таблицу не попадает
#: (иначе таблица не имеет неподвижной точки — правка меняет собственный хэш).
SELF_TABLE_FILE = "PREREGISTRATION.md"


def render_table(lock: dict) -> str:
    """Markdown-таблица слепка: версия харнесса + по строке на каждый вход."""
    lines = [
        START_MARK,
        "| Вход | sha256 |",
        "|---|---|",
        f"| harness | `{lock['arch_ml_version']}` |",
    ]
    for rel, digest in sorted(lock["hashes"].items()):
        if rel == SELF_TABLE_FILE:
            continue
        lines.append(f"| `{rel}` | `{digest[:16]}…` |")
    lines.append(END_MARK)
    return "\n".join(lines)


def update_prereg(table: str) -> bool:
    """Заменить блок таблицы в PREREGISTRATION.md; `False`, если маркеров нет."""
    path = svclib.ROOT / "PREREGISTRATION.md"
    text = svclib.read_text(path)
    start = text.find(START_MARK)
    end = text.find(END_MARK)
    if start < 0 or end < 0:
        return False
    end += len(END_MARK)
    path.write_text(text[:start] + table + text[end:], encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="обновить PREREGISTRATION.md")
    parser.add_argument("--check", action="store_true", help="только сверить слепок")
    args = parser.parse_args()

    table = render_table(svclib.compute_lock())

    if args.check:
        frozen = svclib.load_lock()
        if frozen is None:
            print("prereg.lock.json отсутствует — прогон не заморожен", file=sys.stderr)
            return 1
        current = svclib.compute_lock()
        diff = svclib.lock_mismatches(current, frozen)
        if diff:
            print("prereg_mismatch: входы изменились после заморозки:", file=sys.stderr)
            for rel in diff:
                print(f"  {rel}", file=sys.stderr)
            return 1
        print(f"ok: слепок совпадает ({len(current['hashes'])} входов)")
        return 0

    # Таблица печатается из слепка, но писаться должна ПОСЛЕ правки
    # PREREGISTRATION.md: иначе хэш этого файла в слепке — дотабличный, и
    # первый же `--check` объявит расхождение. Свой хэш в таблицу не входит,
    # поэтому одного прохода достаточно (таблица неподвижна).
    if args.write and not update_prereg(table):
        print("маркеры таблицы в PREREGISTRATION.md не найдены", file=sys.stderr)
        return 1

    current = svclib.compute_lock()
    svclib.write_json(svclib.LOCK_PATH, current)
    print(render_table(current))
    print()
    print(f"записано: {svclib.LOCK_PATH} ({len(current['hashes'])} входов)")
    if args.write:
        print(f"обновлено: {svclib.ROOT / 'PREREGISTRATION.md'}")

    print()
    print("дальше: python3 runners/prepare_cells.py <RUN-ID>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
