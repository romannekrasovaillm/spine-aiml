"""Типизированные результаты SDK (контракт v1, sdk/CONTRACT.md).

Типизированы только: FitnessReport (§2) и результат run (§1).
Receipt'ы archify (§3) передаются вызывающему коду целиком как dict —
контракт обязывает отдавать весь JSON, типизируются лишь общие поля.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RunResult:
    """Результат headless-прогона агента (`arch-ml run -q`)."""

    answer: str            # stdout — финальный ответ ассистента (произвольный текст)
    model: str | None      # модель, если запрошена вызывающим кодом
    duration_ms: int       # длительность процесса на стороне клиента


@dataclass
class Issue:
    """Одна находка fitness-контроля (§2). line == 0 — находка на файл целиком."""

    file: str
    line: int
    rule: str
    message: str
    severity: str          # "error" | "warn"

    @classmethod
    def from_json(cls, raw: dict) -> "Issue":
        return cls(
            file=str(raw.get("file", "")),
            line=int(raw.get("line", 0)),
            rule=str(raw.get("rule", "")),
            message=str(raw.get("message", "")),
            severity=str(raw.get("severity", "")),
        )


@dataclass
class FitnessReport:
    """Отчёт `arch-ml control check --json` (§2).

    passed == False — это ДАННЫЕ (гейт красный), а не ошибка исполнения.
    """

    repo: str
    passed: bool
    summary: str
    issues: list[Issue] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict) -> "FitnessReport":
        return cls(
            repo=str(raw.get("repo", "")),
            passed=bool(raw.get("passed", False)),
            summary=str(raw.get("summary", "")),
            issues=[Issue.from_json(i) for i in raw.get("issues", [])],
        )


def now_ms() -> int:
    """Текущее время в миллисекундах (для duration_ms)."""
    return int(time.monotonic() * 1000)
