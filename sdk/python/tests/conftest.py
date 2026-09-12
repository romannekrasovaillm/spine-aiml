"""Общие приспособления тестов: путь к src/, фабрика фейк-бинарей, путь к arch-ml."""

import os
import stat
import sys
from pathlib import Path

import pytest

# Пакет импортируется из src/ без установки (тесты не требуют pip install).
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# Корень монорепо: sdk/python/tests → sdk/python → sdk → <repo>.
REPO_ROOT = Path(__file__).resolve().parents[3]

# Бинарь для живых тестов: env SPINE_BE_BIN, иначе release-сборка монорепо.
ARCH_BE = os.environ.get(
    "SPINE_BE_BIN", str(REPO_ROOT / "target" / "release" / "arch-ml")
)

FIXTURES = (
    REPO_ROOT / "banking" / "demos" / "cli-from-claude-code"
)
GATE_FIXTURES = FIXTURES / "scenario3-gate" / "fixtures"
SBP_V1 = FIXTURES / "scenario2-archify-cli" / "sbp-v1.architecture.json"
SBP_V2 = FIXTURES / "scenario2-archify-cli" / "sbp-v2.architecture.json"


@pytest.fixture
def fake_binary(tmp_path):
    """Фабрика исполняемых скриптов-заглушек `arch-ml` (без сети и LLM)."""

    def make(body: str) -> str:
        script = tmp_path / "arch-ml-fake"
        script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        return str(script)

    return make


# Живые тесты пропускаются, если бинарь не собран / не задан.
requires_arch_be = pytest.mark.skipif(
    not (os.path.isfile(ARCH_BE) and os.access(ARCH_BE, os.X_OK)),
    reason=f"бинарь arch-ml не найден: {ARCH_BE}",
)
