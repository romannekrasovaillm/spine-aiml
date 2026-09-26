#!/usr/bin/env python3
"""C-022 (спека `docs/specs/C-022-PHASED.md` §3, критерий 5–6) — страж номеров
разделов набора тестов и согласованности реестра «номер → набор».

Что проверяется:

1. **согласованность реестра с телом.** В шапке `tools/tests/run_tool_tests.sh`
   лежит реестр «номер → набор» (строки вида `#   | 51 | check_… |`). Он обязан
   описывать тело файла: множества номеров совпадают, и на каждый номер — столько
   записей, сколько разделов с этим номером в теле (номера исторически повторяются).
2. **дубель сверх реестра** — дефект. Перенумерация запрещена (ADR-028 п.4):
   повтор, названный в реестре, — история ветвления, а не находка; повтор, которого
   в реестре нет (новый раздел вписан в тело, но не в реестр), — красное с поимённым
   списком разделов.

Номера читаются только у **настоящих** разделов: строка `echo "== N. <набор> =="` вне
heredoc-блоков (внутри них тесты строят синтетические файлы — их содержимое разделами
не является). Заголовки сверяются с реестром той же формой записи, что и в теле.

Коды возврата::

    0 — реестр согласован с телом, дублей сверх реестра нет
    1 — находка: дубель сверх реестра либо реестр разошёлся с телом (поимённо)
    2 — NOT-VERIFIED: нет файла тестов или в шапке нет реестра

Запуск::

    python3 tools/check_test_section_numbers.py
    python3 tools/check_test_section_numbers.py --file /tmp/dup.sh   # подсунутый дубель
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

DEFAULT_FILE = "tools/tests/run_tool_tests.sh"

#: Настоящий раздел: `echo "== N. <набор> =="` с начала строки.
SECTION = re.compile(r'^echo "== (\d+)\. (.*) =="\s*$')

#: Строка реестра: `#   | N | набор |` (в комментарии шапки).
REGISTRY = re.compile(r"^#\s*\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*$")

#: Открытие heredoc: `<<'WORD'`, `<<"WORD"`, `<<-WORD`, `<<\WORD` (слово — последнее в строке).
HEREDOC = re.compile(r"<<-?\s*'?\"?\\?([A-Za-z_][A-Za-z0-9_]*)'?\"?\s*$")

#: Нормализация заголовка для сверки с реестром (пробелы, кавычки не трогаем).
def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def body_sections(path: Path) -> list[tuple[int, int, str]]:
    """Разделы тела вне heredoc: ``(строка, номер, заголовок)``."""
    out: list[tuple[int, int, str]] = []
    heredoc: str | None = None
    for num_line, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if heredoc is None:
            match = SECTION.match(line)
            if match:
                out.append((num_line, int(match.group(1)), norm(match.group(2))))
            opener = HEREDOC.search(line)
            if opener and "<<" in line:
                heredoc = opener.group(1)
        elif line.strip() == heredoc:
            heredoc = None
    return out


def registry_rows(path: Path) -> list[tuple[int, str]]:
    """Строки реестра из шапки: ``(номер, заголовок)`` в порядке появления."""
    out: list[tuple[int, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") is False and not line.startswith("#"):
            # реестр живёт в комментариях; тело начинается после `set -uo pipefail`
            continue
        match = REGISTRY.match(line)
        if match:
            out.append((int(match.group(1)), norm(match.group(2))))
    return out


def compare(sections: list[tuple[int, int, str]],
            registry: list[tuple[int, str]]) -> tuple[list[str], dict]:
    """Находки и сводка: дубель сверх реестра + расхождение реестра с телом."""
    body = Counter(num for _, num, _ in sections)
    reg = Counter(num for num, _ in registry)
    findings: list[str] = []

    extra = body - reg                     # номера, которых в реестре меньше, чем в теле
    for num in sorted(extra):
        titles = [t for _, n, t in sections if n == num]
        findings.append(
            f"номер {num}: разделов в теле {body[num]}, записей в реестре {reg.get(num, 0)} "
            f"— дубель сверх реестра: " + "; ".join(f"«{t}»" for t in titles))

    missing = reg - body                   # записи реестра без раздела в теле
    for num in sorted(missing):
        findings.append(
            f"номер {num}: записей в реестре {reg[num]}, разделов в теле {body.get(num, 0)} "
            f"— реестр описывает раздел, которого в теле нет")

    body_titles = {num: Counter(t for _, n, t in sections if n == num) for num in body}
    reg_titles = {num: Counter(t for n, t in registry if n == num) for num in reg}
    for num in sorted(set(body) & set(reg)):
        if body_titles[num] != reg_titles[num]:
            findings.append(
                f"номер {num}: заголовки в теле {sorted(body_titles[num])} против реестра "
                f"{sorted(reg_titles[num])} — реестр разошёлся с телом")

    summary = {
        "sections": len(sections),
        "numbers": len(body),
        "registry_rows": len(registry),
        "duplicated_numbers": sorted(n for n, c in body.items() if c > 1),
        "unregistered_duplicates": sorted(extra),
        "findings": len(findings),
    }
    return findings, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", default=DEFAULT_FILE,
                    help=f"файл набора тестов (по умолчанию {DEFAULT_FILE})")
    ap.add_argument("--json", default=None, help="куда записать машинный результат")
    args = ap.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"NOT-VERIFIED: нет файла тестов: {path}")
        return EXIT_NOT_VERIFIED

    sections = body_sections(path)
    registry = registry_rows(path)
    if not registry:
        print(f"NOT-VERIFIED: в шапке '{path}' нет реестра «номер → набор» "
              f"(строки вида `#   | N | набор |`) — сверить не с чем")
        return EXIT_NOT_VERIFIED
    if not sections:
        print(f"NOT-VERIFIED: в '{path}' не найдено ни одного раздела "
              f"(`echo \"== N. … ==\"`) — проверять нечего")
        return EXIT_NOT_VERIFIED

    findings, summary = compare(sections, registry)
    result = {"criterion": "C-022 §3 (реестр разделов)", "file": str(path),
              **summary, "findings_list": findings}

    if findings:
        print(f"FAIL: реестр разделов разошёлся с телом '{path}' — "
              f"{len(findings)} находок (разделов {summary['sections']}, "
              f"номеров {summary['numbers']}, строк реестра {summary['registry_rows']})")
        for item in findings:
            print(f"  - {item}")
        code = EXIT_FAIL
    else:
        dups = ", ".join(str(n) for n in summary["duplicated_numbers"]) or "нет"
        print(f"OK: реестр согласован с телом '{path}' — разделов {summary['sections']}, "
              f"номеров {summary['numbers']}, строк реестра {summary['registry_rows']}; "
              f"повторы номеров названы в реестре: {dups}")
        code = EXIT_OK

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
