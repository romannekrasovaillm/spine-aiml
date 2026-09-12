"""spine-be-sdk — тонкий Python-клиент поверх headless CLI `arch-ml` (Spine-BE).

Контракт: sdk/CONTRACT.md (версия 1). Только стандартная библиотека.

Пример:
    from spine_be_sdk import SpineBE

    client = SpineBE()  # бинарь из SPINE_BE_BIN или PATH
    report = client.control_check("repo/", constraints="CONSTRAINTS.yaml")
    if not report.passed:
        for issue in report.issues:
            print(issue.file, issue.line, issue.rule, issue.message)
"""

from .client import DEFAULT_TIMEOUT, SpineBE
from .errors import (
    BinaryNotFound,
    ContractViolation,
    ProcessFailed,
    SpineBEError,
    Timeout,
)
from .types import FitnessReport, Issue, RunResult

__version__ = "0.1.0"

__all__ = [
    "SpineBE",
    "DEFAULT_TIMEOUT",
    "RunResult",
    "FitnessReport",
    "Issue",
    "SpineBEError",
    "BinaryNotFound",
    "Timeout",
    "ProcessFailed",
    "ContractViolation",
    "__version__",
]
