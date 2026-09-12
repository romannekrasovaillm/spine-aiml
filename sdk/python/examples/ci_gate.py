#!/usr/bin/env python3
"""ci_gate.py — архитектурный гейт для CI на Spine-BE SDK (Python).

Демо-сценарий banking/demos/sdk-embedding/scenario1-ci-gate:
пайплайн команды вызывает этот скрипт на каждый PR; красный fitness-отчёт
ломает пайплайн с точной диагностикой (файл:строка правило — сообщение).

Использование:
    python3 ci_gate.py <repo> [--constraints PATH]

Бинарь arch-ml разрешается SDK: env SPINE_BE_BIN → arch-ml из PATH (§0 контракта).
Коды выхода: 0 — гейт зелёный, 1 — гейт красный (нарушения — это ДАННЫЕ,
не ошибка исполнения), 2 — ошибка исполнения (бинарь не найден / процесс упал).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Бутстрап импорта SDK без установки пакета: examples/ → ../src.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spine_be_sdk import (  # noqa: E402
    BinaryNotFound,
    ContractViolation,
    ProcessFailed,
    SpineBE,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Архитектурный гейт: arch-ml control check через Spine-BE SDK."
    )
    parser.add_argument("repo", help="путь к репозиторию (корень проверки)")
    parser.add_argument(
        "--constraints",
        default=None,
        help="путь к CONSTRAINTS.yaml (иначе — дефолт arch-ml)",
    )
    args = parser.parse_args()

    client = SpineBE()  # таймаут/бинарь — дефолты SDK и env

    try:
        report = client.control_check(args.repo, constraints=args.constraints)
    except BinaryNotFound as exc:
        print(f"ГЕЙТ: ОШИБКА ИСПОЛНЕНИЯ — {exc}", file=sys.stderr)
        return 2
    except ProcessFailed as exc:
        print(
            f"ГЕЙТ: ОШИБКА ИСПОЛНЕНИЯ — arch-ml завершился с кодом {exc.exit_code}:",
            file=sys.stderr,
        )
        print(exc.stderr.strip(), file=sys.stderr)
        return 2
    except ContractViolation as exc:
        print(f"ГЕЙТ: ОШИБКА ИСПОЛНЕНИЯ — {exc}", file=sys.stderr)
        return 2

    # Красный отчёт — валидные данные (§4 контракта), сюда без исключений.
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
