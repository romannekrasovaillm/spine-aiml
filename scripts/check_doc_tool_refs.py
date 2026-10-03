#!/usr/bin/env python3
"""check_doc_tool_refs.py — документы не должны ссылаться в пустоту.

Зачем. Документы кейсов ссылаются на инструменты (`tools/<имя>.py`) как на носители
доказательств. Если файла нет нигде в дереве, ссылка мертва: читатель не может
перепроверить утверждение, а «доказательство» превращается в декларацию. Этот дефект
уже случался: часть инструментов осталась только на неслитых ветках после чистки
истории и переноса кейсов.

Что проверяется. По всем `*.md` под указанными корнями собираются упоминания
`tools/<имя>.(py|sh)`. Для каждого имени проверяется, существует ли файл с таким
именем ГДЕ-НИБУДЬ в рабочем дереве (это важно: ссылка может быть относительной к
другому кейсу). Отсутствующие печатаются с адресами ссылающихся документов.

Использование:
    python3 scripts/check_doc_tool_refs.py [--root кейсы] [--quiet]
Код возврата: 0 — все ссылки разрешаются; 1 — есть мёртвые ссылки; 2 — нет корней.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

REF = re.compile(r"\btools/([A-Za-z0-9_.\-]+\.(?:py|sh))")


def iter_markdown(roots: list[str]):
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "target", ".venv"}]
            for fn in filenames:
                if fn.endswith(".md"):
                    yield os.path.join(dirpath, fn)


def collect_present_names(roots: list[str]) -> set[str]:
    """Имена файлов, реально существующих в дереве (по basename)."""
    present: set[str] = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", "target", ".venv"}]
            present.update(filenames)
    return present


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Мёртвые ссылки на инструменты в документах")
    ap.add_argument("--root", action="append", default=None,
                    help="корень для сканирования (по умолчанию: кейсы)")
    ap.add_argument("--quiet", action="store_true", help="печатать только итог")
    args = ap.parse_args(argv)

    roots = args.root or ["кейсы"]
    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        print("ОТКАЗ: ни один из корней не найден", file=sys.stderr)
        return 2

    present = collect_present_names(roots)

    #: исключения: имена, которые в документах упомянуты НЕ как ссылка на файл
    #: (например, в таблице расхождений названо ошибочное имя). Список — файл
    #: `scripts/doc_tool_refs_allow.txt`, по строке на имя, `#` — причина.
    allowed: dict[str, str] = {}
    allow_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doc_tool_refs_allow.txt")
    if os.path.isfile(allow_path):
        for line in open(allow_path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if line:
                reason = ""
                raw = open(allow_path, encoding="utf-8")
                for src in raw:
                    if src.strip().startswith(line):
                        reason = src.split("#", 1)[1].strip() if "#" in src else ""
                        break
                allowed[line] = reason
    missing: dict[str, list[str]] = defaultdict(list)
    total_refs = 0

    for doc in sorted(iter_markdown(roots)):
        try:
            text = open(doc, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for name in set(REF.findall(text)):
            total_refs += 1
            if name not in present and name not in allowed:
                missing[name].append(doc)

    if not args.quiet:
        print(f"корни: {', '.join(roots)}")
        print(f"ссылок на инструменты найдено: {total_refs}")
    if allowed and not args.quiet:
        print(f"исключений объявлено: {len(allowed)}")
        for nm, why in sorted(allowed.items()):
            print(f"  {nm:34s} {why[:100]}")
    print(f"МЁРТВЫХ ССЫЛОК: {len(missing)}")

    if missing:
        print("\nимя → где упомянуто:")
        for name in sorted(missing):
            docs = missing[name]
            head = ", ".join(docs[:3]) + (" …" if len(docs) > 3 else "")
            print(f"  {name:34s} {len(docs)} док. | {head}")
        return 1
    print("все ссылки на инструменты разрешаются")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
