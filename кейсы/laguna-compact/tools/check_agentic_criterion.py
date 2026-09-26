#!/usr/bin/env python3
"""C-022 (ADR-059, фаза) — обёртка правила агентного критерия.

Зачем обёртка, а не прямой вызов стража (спека `docs/specs/C-022-PHASED.md` §2):
правило `command_succeeds` передаёт только командную строку, а стражу
`tools/check_sft_agentic_share.py` обязательны `--report` и `--base-report`.
Прямой вызов дал бы красное на **любом** дереве, где артефактов ещё нет, —
«красный по построению», запрещённый ADR-053 (правило требовало бы артефакт,
появление которого само же ограничено моментом замера). Обёртка решает, в каком
состоянии находится предмет правила, и **ничего не решает по существу**.

Контракт (спека §2):

    2 — NOT-VERIFIED: предмета ещё нет (нет ни одного из двух артефактов)
    1 — нарушение: предмет частичный (есть ровно один) — неполнота
    0/1 — предмет есть: проброс вердикта стража как есть

Обёртка **не пересчитывает** метрику и не подменяет вердикт: при наличии предмета
она вызывает страж (`tools/check_sft_agentic_share.py`) и возвращает **его** код.
Семантика контура берётся импортом модуля стража, а не копией его логики: страж —
единственный носитель правила сопоставимости (ADR-059 п.3), вердикт по (в1)/(в2)
остаётся у сводки (ADR-059 п.5).

Печатается одна строка состояния (`state: absent|partial|present`, `verdict: …`)
и — при `present` — вывод стража. Машинный контракт — флагом `--json`.

Запуск::

    python3 tools/check_agentic_criterion.py
    python3 tools/check_agentic_criterion.py --report <путь> --base-report <путь> --json out.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Пути по умолчанию — боевая пара точки S4 (ADR-059 п.1: база — переснятая в
#: штатном режиме на входном чекпойнте стадии). Переопределяются флагами для
#: будущих стадий («SFT → RL») и сидов.
DEFAULT_REPORT = "evidence/s3aa-agentic-sft.json"
DEFAULT_BASE = "evidence/s3aa-agentic-cpt-nogram4.json"

#: Страж — носитель правила сопоставимости. Обёртка вызывает его, а не повторяет.
GUARD = "tools/check_sft_agentic_share.py"


def load_guard(path: Path):
    """Импорт стража по пути (семантика контура — импортом, не копией)."""
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("check_sft_agentic_share", str(path))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state_of(report: Path, base: Path) -> str:
    """Состояние предмета правила: absent | partial | present (спека §2)."""
    have_r, have_b = report.is_file(), base.is_file()
    if have_r and have_b:
        return "present"
    if have_r or have_b:
        return "partial"
    return "absent"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help=f"отчёт агентной пробы (по умолчанию {DEFAULT_REPORT})")
    ap.add_argument("--base-report", default=DEFAULT_BASE,
                    help=f"переснятая база критерия (по умолчанию {DEFAULT_BASE})")
    ap.add_argument("--json", default=None, help="куда записать машинный результат")
    ap.add_argument("--tolerance-pp", type=float, default=0.0,
                    help="допуск на падение доли (в1), п.п. — печать условий (не вердикт)")
    args = ap.parse_args(argv)

    report, base = Path(args.report), Path(args.base_report)
    state = state_of(report, base)
    out = {
        "criterion": "C-022",
        "adr": "ADR-059",
        "state": state,
        "report": {"path": str(report), "exists": report.is_file()},
        "base": {"path": str(base), "exists": base.is_file()},
        "verdict": None,
        "exit": None,
        "guard": GUARD,
    }

    def emit(code: int, verdict: str, line: str) -> int:
        out["verdict"], out["exit"] = verdict, code
        print(line)
        if args.json:
            Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        return code

    if state == "absent":
        return emit(
            EXIT_NOT_VERIFIED, "NOT-VERIFIED",
            f"state: absent | verdict: NOT-VERIFIED | предмета ещё нет: ни "
            f"'{report}', ни '{base}' — фаза «ещё не может быть» (ADR-053)")

    if state == "partial":
        have = str(report) if report.is_file() else str(base)
        missing = str(base) if report.is_file() else str(report)
        return emit(
            EXIT_FAIL, "FAIL",
            f"state: partial | verdict: FAIL | предмет неполон: есть '{have}', нет "
            f"'{missing}' — неполнота есть нарушение, а не «ещё не может быть»")

    module = load_guard(Path(GUARD))
    if module is None:
        return emit(EXIT_NOT_VERIFIED, "NOT-VERIFIED",
                    f"state: present | verdict: NOT-VERIFIED | страж '{GUARD}' не найден — "
                    f"сопоставимость проверить нечем")
    print(f"state: present | verdict: по стражу {GUARD} | отчёт '{report}', база '{base}'")
    code = module.main(["--report", str(report), "--base-report", str(base),
                        "--tolerance-pp", str(args.tolerance_pp)])
    out["verdict"] = {0: "PASS", 1: "FAIL"}.get(code, "NOT-VERIFIED")
    out["exit"] = code
    print(f"state: present | verdict: {out['verdict']} | код стража проброшен как есть ({code})")
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
