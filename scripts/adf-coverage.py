#!/usr/bin/env python3
"""adf-coverage — гейт покрытия доменов ADF маппингом (замечание ревью №12).

Каждый `domains_id` из снапшота каталога ADF обязан встречаться ТОЧНЫМ
именем в `banking/adf/adf-mapping.md` — в карте доменов или в таблице
«Вне фокуса» (обе — осознанные строки с причиной). Домен, выпавший из
обеих таблиц, — регресс покрытия: сборка ломается до ревью, а не после
вопроса из зала (прецедент 2026-09-04: finances_cj и finances_motivation
выпали молча при заявленных «27/11»).

Прогон:  python3 scripts/adf-coverage.py [корень репозитория]
Гейт:    CONSTRAINTS.yaml, правило BE-21 (`command_succeeds`); CI-джоба dogfood.

Код выхода: 0 — все домены покрыты; 1 — есть непокрытые (список на stdout).
Только stdlib, без сети.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CATALOG = "banking/adf/catalog/parsed_questions_all_v2.json"
MAPPING = "banking/adf/adf-mapping.md"


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    catalog_path = root / CATALOG
    mapping_path = root / MAPPING
    if not catalog_path.is_file():
        print(f"adf-coverage: каталог не найден: {catalog_path}")
        return 1
    if not mapping_path.is_file():
        print(f"adf-coverage: маппинг не найден: {mapping_path}")
        return 1

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    domains: dict[str, int] = {}
    for q in catalog["questions"]:
        d = q["domains_id"]
        domains[d] = domains.get(d, 0) + 1

    mapping = mapping_path.read_text(encoding="utf-8")
    missing = [
        (d, n)
        for d, n in sorted(domains.items())
        if not re.search(rf"\b{re.escape(d)}\b", mapping)
    ]

    if missing:
        print(f"adf-coverage: НЕ покрыто доменов: {len(missing)} из {len(domains)}")
        for d, n in missing:
            print(f"  {d}: {n} вопросов — нет строки ни в карте, ни в «Вне фокуса»")
        return 1
    print(f"adf-coverage: OK (доменов покрыто: {len(domains)}/{len(domains)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
