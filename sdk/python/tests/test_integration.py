"""Живые интеграционные тесты SDK против настоящего бинаря `arch-ml`.

Фикстуры — эталонные из §5 контракта (sdk/CONTRACT.md). LLM-прогоны `run()`
здесь НЕ выполняются (стоимость/время) — построение argv покрыто юнит-тестами.
"""

import shutil

import pytest

from conftest import ARCH_BE, GATE_FIXTURES, SBP_V1, SBP_V2, requires_arch_be
from spine_be_sdk import SpineBE

#: Живые команды (deliver/compare) могут идти десятки секунд — запасной таймаут.
LIVE_TIMEOUT = 300.0

pytestmark = requires_arch_be


@pytest.fixture
def client() -> SpineBE:
    return SpineBE(binary=ARCH_BE, timeout=LIVE_TIMEOUT)


# 1. Зелёный гейт на эталонной фикстуре.
def test_control_check_green(client):
    report = client.control_check(
        GATE_FIXTURES, constraints=GATE_FIXTURES / "CONSTRAINTS.yaml"
    )
    assert report.passed is True
    assert report.issues == []


# 2. Красный кейс: копия фикстуры + файл с тестовым PAN.
def test_control_check_red(client, tmp_path):
    repo = tmp_path / "fixtures-red"
    shutil.copytree(GATE_FIXTURES, repo)
    (repo / "src" / "bad.py").write_text(
        'pan = "4276550012345678"\n', encoding="utf-8"
    )
    report = client.control_check(repo, constraints=repo / "CONSTRAINTS.yaml")
    assert report.passed is False
    pan_issues = [i for i in report.issues if i.rule == "no_pan_in_code"]
    assert pan_issues, f"нет находки no_pan_in_code: {report.issues!r}"
    assert pan_issues[0].severity == "error"
    assert pan_issues[0].line == 1


# 3. archify validate эталонной архитектуры СБП v1.
def test_archify_validate(client):
    receipt = client.archify_validate("architecture", SBP_V1)
    assert receipt["ok"] is True
    assert len(receipt["checks"]) == 9
    assert receipt["composition"]["status"] == "pass"


# 4. archify deliver: HTML-артефакт создаётся, sha256 непуст.
def test_archify_deliver(client, tmp_path):
    out = tmp_path / "sbp-v1.html"
    receipt = client.archify_deliver("architecture", SBP_V1, out)
    assert receipt["ok"] is True
    assert receipt["artifact"]["sha256"]
    assert out.is_file() and out.stat().st_size > 0


# 5. archify compare v1 → v2: +1 компонент, +2 связи.
def test_archify_compare(client, tmp_path):
    out = tmp_path / "sbp-delta.html"
    receipt = client.archify_compare(SBP_V1, SBP_V2, out)
    assert receipt["ok"] is True
    assert receipt["summary"]["components"]["added"] == 1
    assert receipt["summary"]["connections"]["added"] == 2
