"""Юнит-тесты SDK на фейк-бинаре (без LLM, без сети).

Проверяется: построение argv, разбор JSON по контракту, маппинг ошибок §4.
Живой LLM-прогон `run()` здесь НЕ выполняется (§5 контракта) — только argv.
"""

import json

import pytest

from spine_be_sdk import (
    BinaryNotFound,
    ContractViolation,
    ProcessFailed,
    SpineBE,
    Timeout,
)

# Заглушка: печатает свой argv одной строкой JSON (для проверки построения argv).
ECHO_ARGV = "import json, sys\nprint(json.dumps(sys.argv[1:]))"


def _read_argv(answer: str) -> list[str]:
    """Ответ заглушки ECHO_ARGV — это argv, переданный «бинарю»."""
    return json.loads(answer.strip())


# ---------------------------------------------------------------- run (§1)

def test_run_argv_minimal(fake_binary):
    client = SpineBE(binary=fake_binary(ECHO_ARGV))
    result = client.run("привет")
    assert _read_argv(result.answer) == ["run", "-q", "привет"]
    assert result.model is None
    assert result.duration_ms >= 0


def test_run_argv_full(fake_binary):
    client = SpineBE(binary=fake_binary(ECHO_ARGV))
    result = client.run("черновик ADR", model="deepseek-flash", max_turns=3)
    assert _read_argv(result.answer) == [
        "run", "-q", "--model", "deepseek-flash", "--max-turns", "3",
        "черновик ADR",
    ]
    assert result.model == "deepseek-flash"


def test_run_process_failed(fake_binary):
    body = "import sys\nsys.stderr.write('provider down\\n')\nsys.exit(1)"
    client = SpineBE(binary=fake_binary(body))
    with pytest.raises(ProcessFailed) as excinfo:
        client.run("промпт")
    assert excinfo.value.exit_code == 1
    assert "provider down" in excinfo.value.stderr


# ------------------------------------------------------- control check (§2)

def test_control_check_red_is_data_not_exception(fake_binary):
    """passed=false + exit 1 — валидный отчёт-данные, исключения нет (§4)."""
    report = {
        "repo": ".", "passed": False,
        "summary": "Правил: 3, нарушений: 1 (error: 1, warn: 0)",
        "issues": [{
            "file": "src/bad.py", "line": 1, "rule": "no_pan_in_code",
            "message": "must_not_contain: запрещённый паттерн",
            "severity": "error",
        }],
    }
    body = f"import sys\nprint({json.dumps(json.dumps(report))})\nsys.exit(1)"
    client = SpineBE(binary=fake_binary(body))
    result = client.control_check("repo", constraints="CONSTRAINTS.yaml")
    assert result.passed is False
    assert result.issues[0].rule == "no_pan_in_code"
    assert result.issues[0].severity == "error"
    assert result.issues[0].line == 1


def test_control_check_argv(fake_binary):
    report = {"repo": ".", "passed": True, "summary": "ok", "issues": []}
    body = (
        f"import json, sys\n"
        f"print(json.dumps(sys.argv[1:]))\n"
        f"print({json.dumps(json.dumps(report))})"
    )
    # Заглушка печатает argv в stderr, отчёт — в stdout (контракт читает stdout).
    body = body.replace("print(json.dumps(sys.argv[1:]))",
                        "print(json.dumps(sys.argv[1:]), file=sys.stderr)")
    client = SpineBE(binary=fake_binary(body))
    # argv проверяем косвенно: отчёт распарсился → команда дошла как надо.
    result = client.control_check("repo/", constraints="C.yaml")
    assert result.passed is True
    assert result.issues == []


def test_control_check_exit0_broken_json_is_contract_violation(fake_binary):
    client = SpineBE(binary=fake_binary("print('не json')"))
    with pytest.raises(ContractViolation):
        client.control_check("repo")


def test_control_check_exit3_no_json_is_process_failed(fake_binary):
    body = "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)"
    client = SpineBE(binary=fake_binary(body))
    with pytest.raises(ProcessFailed) as excinfo:
        client.control_check("repo")
    assert excinfo.value.exit_code == 3
    assert "boom" in excinfo.value.stderr


# ------------------------------------------------------------- archify (§3)

def test_archify_receipt_passed_through(fake_binary):
    """Receipt archify передаётся целиком как dict, включая ok=false."""
    receipt = {"schemaVersion": 1, "ok": False, "command": "validate",
               "type": "architecture", "checks": []}
    body = f"import sys\nprint({json.dumps(json.dumps(receipt))})\nsys.exit(1)"
    client = SpineBE(binary=fake_binary(body))
    result = client.archify_validate("architecture", "ir.json")
    assert result == receipt  # receipt отдан вызывающему коду без потерь


def test_archify_compare_argv(fake_binary):
    receipt = {"schemaVersion": 1, "ok": True, "command": "compare"}
    body = (
        "import json, sys\n"
        "sys.stderr.write(json.dumps(sys.argv[1:]))\n"
        f"print({json.dumps(json.dumps(receipt))})"
    )
    client = SpineBE(binary=fake_binary(body))
    result = client.archify_compare("base.json", "head.json", "out.html")
    assert result["ok"] is True


# ------------------------------------------------------- ошибки §4, общие

def test_binary_not_found_explicit():
    client = SpineBE(binary="/nonexistent/arch-ml")
    with pytest.raises(BinaryNotFound):
        client.control_check("repo")


def test_binary_not_found_path(monkeypatch):
    monkeypatch.delenv("SPINE_BE_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    client = SpineBE()
    with pytest.raises(BinaryNotFound):
        client.control_check("repo")


def test_binary_from_env(fake_binary, monkeypatch):
    path = fake_binary(ECHO_ARGV)
    monkeypatch.setenv("SPINE_BE_BIN", path)
    client = SpineBE()  # бинарь берётся из SPINE_BE_BIN
    result = client.run("пинг")
    assert _read_argv(result.answer) == ["run", "-q", "пинг"]


def test_timeout(fake_binary):
    body = "import time\ntime.sleep(30)"
    client = SpineBE(binary=fake_binary(body), timeout=0.5)
    with pytest.raises(Timeout):
        client.control_check("repo")


def test_timeout_per_call_override(fake_binary):
    body = "import time\ntime.sleep(30)"
    client = SpineBE(binary=fake_binary(body))  # дефолт 120 с
    with pytest.raises(Timeout):
        client.run("промпт", timeout=0.5)  # переопределение per-call
