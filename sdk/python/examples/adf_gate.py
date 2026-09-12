#!/usr/bin/env python3
"""adf_gate.py — архитектурный гейт, собираемый анкетой ADF (Python SDK).

Демо-сценарий banking/demos/sdk-embedding/scenario4-adf-gate:
ответы анкеты ADF (гейт A1) машинно открывают блоки требований; скрипт
извлекает из открытых блоков id PAY-правил, отбирает их из исполняемого
пресета, собирает эффективный CONSTRAINTS.yaml и прогоняет fitness-гейт
по репозиторию сервиса. Изменение скоупа в анкете («данные карт — да» →
блок PAY-12) меняет гейт БЕЗ изменения кода сервиса.

Использование:
    python3 adf_gate.py <repo> [--answers PATH] [--preset PATH]
                        [--exclude-block PAY-XX]...

Зависимости: pyyaml (в отличие от самого SDK — он stdlib-only).
Бинарь arch-ml разрешается SDK: env SPINE_BE_BIN → arch-ml из PATH (§0 контракта).
Пути по умолчанию резолвятся от __file__ к корню монорепо (examples/ → ../../..).
Коды выхода: 0 — ГЕЙТ: PASS, 1 — ГЕЙТ: FAIL (нарушения — данные, не сбой),
2 — ошибка исполнения (нет бинаря/файлов/валидного ответа процесса).
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

import yaml  # pyyaml — единственная внешняя зависимость примера

# Бутстрап импорта SDK без установки пакета: examples/ → ../src.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spine_be_sdk import (  # noqa: E402
    BinaryNotFound,
    ContractViolation,
    ProcessFailed,
    SpineBE,
)

# Корень монорепо: examples/ → sdk/python → sdk → <repo>.
REPO_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_ANSWERS = (
    REPO_ROOT / "banking" / "demos" / "archify-adf"
    / "scenario2-адф" / "adf-answers.yaml"
)
DEFAULT_PRESET = (
    REPO_ROOT / "banking" / "presets" / "payments" / "profiles" / "go-kafka-pg.yaml"
)

#: id правила/блока: «PAY-27a» → базовый 27; диапазоны «PAY-01..10», «PAY-11..PAY-13».
_RE_RANGE = re.compile(r"PAY-(\d+)\.\.(?:PAY-)?(\d+)")
_RE_SINGLE = re.compile(r"PAY-(\d+)")


def pay_ids_in_text(text: str) -> set[int]:
    """Базовые id PAY-правил в строке блока анкеты (синглы + диапазоны)."""
    ids: set[int] = set()
    for lo, hi in _RE_RANGE.findall(text):
        ids.update(range(int(lo), int(hi) + 1))
    ids.update(int(m) for m in _RE_SINGLE.findall(text))
    return ids


def base_pay_id(rule_id: str) -> int | None:
    """Базовый id правила пресета: «PAY-12b» → 12; «PAY-BUILD» → None."""
    m = _RE_SINGLE.search(rule_id)
    return int(m.group(1)) if m else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Гейт, собираемый анкетой ADF: opened blocks → PAY-правила → control check."
    )
    parser.add_argument("repo", help="путь к репозиторию сервиса (корень проверки)")
    parser.add_argument("--answers", default=str(DEFAULT_ANSWERS),
                        help="adf-answers.yaml (по умолчанию — эталон демо)")
    parser.add_argument("--preset", default=str(DEFAULT_PRESET),
                        help="исполняемый пресет PAY-правил (по умолчанию go-kafka-pg)")
    parser.add_argument("--exclude-block", action="append", default=[],
                        metavar="PAY-XX",
                        help="исключить блок из гейта (повторяемый флаг; демо акта 1)")
    args = parser.parse_args()

    # --- читаем анкету и пресет (ошибки входных файлов → exit 2) -----------
    try:
        answers = yaml.safe_load(Path(args.answers).read_text(encoding="utf-8"))
        preset = yaml.safe_load(Path(args.preset).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"ГЕЙТ: ОШИБКА ИСПОЛНЕНИЯ — не прочитаны входные файлы: {exc}",
              file=sys.stderr)
        return 2
    blocks = (answers or {}).get("opened_conditional_blocks") or []
    preset_rules = (preset or {}).get("constraints") or []

    # --- открытые блоки → исполняемые (PAY-id) и неисполняемые -------------
    executable, non_executable = [], []
    for entry in blocks:
        block = str(entry.get("block", ""))
        opened_by = [str(x) for x in entry.get("opened_by", [])]
        ids = pay_ids_in_text(block)
        (executable if ids else non_executable).append((block, opened_by, ids))

    open_ids: set[int] = set().union(*(ids for _, _, ids in executable)) if executable else set()

    excluded_ids: set[int] = set()
    for raw in args.exclude_block:
        ids = pay_ids_in_text(raw)
        if not ids:
            print(f"ГЕЙТ: ОШИБКА ИСПОЛНЕНИЯ — не распознан --exclude-block: {raw!r}",
                  file=sys.stderr)
            return 2
        excluded_ids |= ids
    effective_ids = open_ids - excluded_ids

    # --- отбор правил пресета по базовому id (PAY-27a → PAY-27) ------------
    active = [r for r in preset_rules
              if (bid := base_pay_id(str(r.get("id", "")))) is not None
              and bid in effective_ids]
    covered = {base_pay_id(str(r.get("id", ""))) for r in active}
    gaps = sorted(effective_ids - covered)  # открыты анкетой, но правила нет

    # --- печать сборки гейта ------------------------------------------------
    print(f"Анкета ADF: {args.answers}")
    print(f"Пресет правил: {args.preset}")
    print(f"Открытые блоки анкеты ({len(executable)}):")
    for block, opened_by, _ in executable:
        print(f"  {block}  ← ответы: {', '.join(opened_by) or '—'}")
    if non_executable:
        print(f"Открытые неисполняемые блоки ({len(non_executable)}) — в гейт не включены:")
        for block, _, _ in non_executable:
            print(f"  {block}")
    if excluded_ids:
        shown = ", ".join(f"PAY-{i:02d}" for i in sorted(excluded_ids & open_ids))
        print(f"Исключено ключом --exclude-block: {shown}")
    print(f"Активные fitness-правила ({len(active)}):")
    for rule in active:
        print(f"  {rule.get('id')} [{rule.get('severity')}] {rule.get('name')}")
    if gaps:
        print("ВНИМАНИЕ: открыты анкетой, но нет исполняемого правила в пресете: "
              + ", ".join(f"PAY-{i:02d}" for i in gaps))

    # --- эффективный CONSTRAINTS.yaml → control check -----------------------
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".constraints.yaml", prefix="adf-gate-", delete=False,
        encoding="utf-8",
    )
    try:
        with tmp:
            yaml.safe_dump({"constraints": active}, tmp, allow_unicode=True,
                           sort_keys=False)
        report = SpineBE().control_check(args.repo, constraints=tmp.name)
    except BinaryNotFound as exc:
        print(f"ГЕЙТ: ОШИБКА ИСПОЛНЕНИЯ — {exc}", file=sys.stderr)
        return 2
    except ProcessFailed as exc:
        print(f"ГЕЙТ: ОШИБКА ИСПОЛНЕНИЯ — arch-ml завершился с кодом {exc.exit_code}:",
              file=sys.stderr)
        print(exc.stderr.strip(), file=sys.stderr)
        return 2
    except ContractViolation as exc:
        print(f"ГЕЙТ: ОШИБКА ИСПОЛНЕНИЯ — {exc}", file=sys.stderr)
        return 2
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    # --- красный отчёт — валидные данные (§4 контракта) ---------------------
    print(f"Репозиторий: {report.repo}")
    print(f"Сводка: {report.summary}")
    if report.passed:
        print("ГЕЙТ: PASS")
        return 0
    print("Нарушения:")
    for issue in report.issues:
        where = issue.file if issue.line == 0 else f"{issue.file}:{issue.line}"
        print(f"  {where} [{issue.severity}] {issue.rule} — {issue.message}")
    print("ГЕЙТ: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
