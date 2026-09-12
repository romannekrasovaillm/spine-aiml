"""Adversarial-тесты SDK (QA-прогон, 2026-09-03).

Враждебные сценарии против фейк-бинаря (SPINE_BE_BIN) и, где помечено,
против реального `arch-ml`. Живые LLM-прогоны НЕ выполняются: для `run`
проверяется только, что clap-разбор argv доходит до конца (невалидная
модель отклоняется харнессом ДО вызова провайдера).

Жёсткие дедлайны реализованы pytest-timeout (плагин установлен) как
страховкой от дедлока + ручной проверкой elapsed.
"""

import json
import locale
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from conftest import ARCH_BE, GATE_FIXTURES, requires_arch_be
from spine_be_sdk import (
    BinaryNotFound,
    ContractViolation,
    FitnessReport,
    ProcessFailed,
    SpineBE,
    Timeout,
)

# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _make_fake(tmp_path: Path, body: str, name: str = "arch-ml-fake") -> str:
    """Исполняемый скрипт-заглушка с произвольным именем (для env-тестов)."""
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return str(script)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


GREEN_REPORT = {
    "repo": ".", "passed": True,
    "summary": "Правил: 3, нарушений: 0 (error: 0, warn: 0)",
    "issues": [],
}

# ---------------------------------------------------------------------------
# 1. PIPE-DEADLOCK: валидный JSON в stdout + мегабайты мусора в stderr
# ---------------------------------------------------------------------------

@pytest.mark.timeout(30)  # жёсткий дедлайн: дедлок пайпа не должен повесить сьют
def test_pipe_deadlock_large_stderr_control_check(fake_binary):
    """3 МБ в stderr + валидный JSON в stdout, exit 0 → без зависания, JSON распарсен."""
    body = (
        "import sys\n"
        "sys.stderr.write('X' * (3 * 1024 * 1024))\n"
        f"sys.stdout.write({json.dumps(json.dumps(GREEN_REPORT))})"
    )
    client = SpineBE(binary=fake_binary(body))
    started = time.monotonic()
    report = client.control_check("repo")
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"подозрение на дедлок пайпа: {elapsed:.1f} с"
    assert report.passed is True


@pytest.mark.timeout(30)
def test_pipe_deadlock_large_stderr_run(fake_binary):
    """run(): 2 МБ в stderr + текстовый ответ в stdout → без зависания."""
    body = (
        "import sys\n"
        "sys.stderr.write('Y' * (2 * 1024 * 1024))\n"
        "sys.stdout.write('ответ ассистента')"
    )
    client = SpineBE(binary=fake_binary(body))
    started = time.monotonic()
    result = client.run("промпт")
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"подозрение на дедлок пайпа: {elapsed:.1f} с"
    assert result.answer == "ответ ассистента"


@pytest.mark.timeout(30)
def test_pipe_both_streams_large(fake_binary):
    """Оба потока по 2 МБ одновременно (stdout тоже большой) → без дедлока."""
    big_json = json.dumps({
        "repo": ".", "passed": False, "summary": "S" * (2 * 1024 * 1024),
        "issues": [],
    })
    body = (
        "import sys\n"
        "sys.stderr.write('Z' * (2 * 1024 * 1024))\n"
        f"sys.stdout.write({json.dumps(big_json)})\n"
        "sys.exit(1)"
    )
    client = SpineBE(binary=fake_binary(body))
    report = client.control_check("repo")
    assert report.passed is False
    assert len(report.summary) == 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# 2. TIMEOUT-KILL: процесс реально убит, без зомби
# ---------------------------------------------------------------------------

def test_timeout_kill_process_really_dead(fake_binary, tmp_path):
    """Сон 120 с + таймаут 1 с → Timeout, elapsed < 10 с, процесса нет в системе."""
    pidfile = tmp_path / "child.pid"
    body = (
        "import os, time\n"
        f"open({json.dumps(str(pidfile))}, 'w').write(str(os.getpid()))\n"
        "time.sleep(120)"
    )
    client = SpineBE(binary=fake_binary(body), timeout=1.0)
    started = time.monotonic()
    with pytest.raises(Timeout):
        client.control_check("repo")
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"клиент ждал дольше разумного: {elapsed:.1f} с"

    pid = int(pidfile.read_text().strip())
    # subprocess.run уже сделал kill+wait; даём ОС мгновение на реапинг.
    deadline = time.monotonic() + 5
    while _pid_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_is_alive(pid), f"процесс {pid} уцелел после Timeout (зомби/сирота)"


# ---------------------------------------------------------------------------
# 3. BINARY-NOT-FOUND: несуществующий путь и неисполняемый файл
# ---------------------------------------------------------------------------

def test_binary_not_found_nonexistent_path_param():
    client = SpineBE(binary="/nonexistent/dir/arch-ml")
    with pytest.raises(BinaryNotFound) as excinfo:
        client.control_check("repo")
    assert "/nonexistent/dir/arch-ml" in str(excinfo.value)


def test_binary_not_found_nonexistent_path_env(monkeypatch):
    monkeypatch.setenv("SPINE_BE_BIN", "/nonexistent/dir/arch-ml")
    client = SpineBE()
    with pytest.raises(BinaryNotFound) as excinfo:
        client.control_check("repo")
    assert "/nonexistent/dir/arch-ml" in str(excinfo.value)


def test_binary_not_found_not_executable_param(tmp_path):
    plain = tmp_path / "arch-ml-plain"
    plain.write_text("не бинарь\n", encoding="utf-8")  # без +x
    client = SpineBE(binary=plain)
    with pytest.raises(BinaryNotFound) as excinfo:
        client.control_check("repo")
    assert str(plain) in str(excinfo.value)


def test_binary_not_found_not_executable_env(tmp_path, monkeypatch):
    plain = tmp_path / "arch-ml-plain"
    plain.write_text("не бинарь\n", encoding="utf-8")
    monkeypatch.setenv("SPINE_BE_BIN", str(plain))
    client = SpineBE()
    with pytest.raises(BinaryNotFound) as excinfo:
        client.control_check("repo")
    assert str(plain) in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. EXIT-2-NO-JSON: ненулевой exit + stderr без JSON → ProcessFailed
# ---------------------------------------------------------------------------

def test_exit2_no_json_process_failed(fake_binary):
    body = "import sys\nsys.stderr.write('clap: unexpected argument\\n')\nsys.exit(2)"
    client = SpineBE(binary=fake_binary(body))
    with pytest.raises(ProcessFailed) as excinfo:
        client.control_check("repo")
    assert excinfo.value.exit_code == 2
    assert "unexpected argument" in excinfo.value.stderr


# ---------------------------------------------------------------------------
# 5. CONTRACT-VIOLATION: exit 0 + битый/пустой stdout для JSON-команд
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("broken", [
    "это вообще не json",
    '{"repo": ".", "passed":',   # обрезанный JSON
    "",                           # пустой stdout
    "[1, 2",                      # битый массив
])
def test_contract_violation_exit0_broken_json(fake_binary, broken):
    body = f"import sys\nsys.stdout.write({json.dumps(broken)})"
    client = SpineBE(binary=fake_binary(body))
    with pytest.raises(ContractViolation):
        client.control_check("repo")


# ---------------------------------------------------------------------------
# 6. UNICODE: кириллица и эмодзи — round-trip без потерь
# ---------------------------------------------------------------------------

def test_unicode_roundtrip(fake_binary):
    report = {
        "repo": ".", "passed": False,
        "summary": "Правил: 3, нарушений: 1 ⚠️🔥",
        "issues": [{
            "file": "src/плохой.py", "line": 7, "rule": "no_pan_in_code",
            "message": "найден PAN «4276 5500 1234 5678» 🚨 — убрать немедленно",
            "severity": "error",
        }],
    }
    # JSON встраивается как bytes-литерал (repr(bytes) — ASCII-безопасен,
    # в отличие от str с \uXXXX-суррогатами от json.dumps).
    payload = json.dumps(report, ensure_ascii=False).encode("utf-8")
    body = (
        "import sys\n"
        f"sys.stdout.buffer.write({payload!r})\n"
        "sys.exit(1)"
    )
    client = SpineBE(binary=fake_binary(body))
    result = client.control_check("repo")
    assert result.summary == report["summary"]
    assert result.issues[0].message == report["issues"][0]["message"]
    assert result.issues[0].file == "src/плохой.py"


def test_unicode_under_non_utf8_locale(fake_binary):
    """Клиент под C-локалью обязан декодировать вывод arch-ml как UTF-8.

    Контракт несёт кириллицу (§2); Rust-бинарь всегда печатает UTF-8,
    локаль клиента на это влиять не должна.
    """
    body = (
        "import sys\n"
        f"sys.stdout.buffer.write({json.dumps(GREEN_REPORT, ensure_ascii=False).encode('utf-8')!r})"
    )
    client = SpineBE(binary=fake_binary(body))
    previous = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, "C")  # nl_langinfo → ANSI_X3.4-1968
        report = client.control_check("repo")
    finally:
        locale.setlocale(locale.LC_CTYPE, previous)
    assert report.passed is True


# ---------------------------------------------------------------------------
# 7. RED-AS-DATA: exit 1 + валидный отчёт passed=false — ДАННЫЕ (§4)
# ---------------------------------------------------------------------------

def test_red_as_data_fitness_report(fake_binary):
    report = {
        "repo": ".", "passed": False,
        "summary": "Правил: 3, нарушений: 2 (error: 1, warn: 1)",
        "issues": [
            {"file": "a.py", "line": 1, "rule": "r1", "message": "m1", "severity": "error"},
            {"file": "b.py", "line": 0, "rule": "r2", "message": "m2", "severity": "warn"},
        ],
    }
    body = f"import sys\nprint({json.dumps(json.dumps(report))})\nsys.exit(1)"
    client = SpineBE(binary=fake_binary(body))
    result = client.control_check("repo")  # исключения быть НЕ должно
    assert isinstance(result, FitnessReport)
    assert result.passed is False
    assert len(result.issues) == 2


def test_red_as_data_archify_receipt(fake_binary):
    """archify receipt ok=false + exit 1 — тоже данные, а не ProcessFailed."""
    receipt = {"schemaVersion": 1, "ok": False, "command": "validate",
               "type": "architecture", "checks": [{"name": "x", "ok": False}]}
    body = f"import sys\nprint({json.dumps(json.dumps(receipt))})\nsys.exit(1)"
    client = SpineBE(binary=fake_binary(body))
    assert client.archify_validate("architecture", "ir.json") == receipt


# ---------------------------------------------------------------------------
# 8. PROMPT-DASH: промпт, начинающийся с '-'
# ---------------------------------------------------------------------------

ECHO_ARGV = "import json, sys\nprint(json.dumps(sys.argv[1:]))"


def test_prompt_dash_argv(fake_binary):
    """Промпт '-файл' обязан уходить в CLI после разделителя '--'."""
    client = SpineBE(binary=fake_binary(ECHO_ARGV))
    result = client.run("-файл")
    argv = json.loads(result.answer.strip())
    assert argv[-2:] == ["--", "-файл"], (
        f"промпт с '-' передан без разделителя '--' — clap его отвергнет: {argv!r}"
    )


def test_prompt_stdin_sentinel_no_dashdash(fake_binary):
    """Промпт '-' (stdin-сентинель §1) передаётся как есть, без '--'."""
    client = SpineBE(binary=fake_binary(ECHO_ARGV))
    result = client.run("-")
    argv = json.loads(result.answer.strip())
    assert argv[-1] == "-"
    assert "--" not in argv


@requires_arch_be
def test_prompt_dash_real_binary_clap_accepts():
    """Реальный arch-ml: clap-разбор с промптом '-файл' доходит до конца.

    Модель заведомо несуществующая — харнесс откажет ДО вызова LLM
    (exit 1, «модель не настроена»). Если бы clap споткнулся о промпт,
    был бы exit 2 («unexpected argument»).
    """
    client = SpineBE(binary=ARCH_BE, timeout=60.0)
    with pytest.raises(ProcessFailed) as excinfo:
        client.run("-файл", model="__no_such_model__", max_turns=1)
    err = excinfo.value
    assert err.exit_code != 2, f"clap отверг промпт с '-': {err.stderr!r}"
    assert "не настроена" in err.stderr


# ---------------------------------------------------------------------------
# 9. ERROR-SURFACING: реальный control check по несуществующему пути (живой)
# ---------------------------------------------------------------------------

@requires_arch_be
def test_error_surfacing_missing_repo_real_binary():
    client = SpineBE(binary=ARCH_BE, timeout=60.0)
    with pytest.raises(ProcessFailed) as excinfo:
        client.control_check("/nonexistent/repo/path-qa")
    err = excinfo.value
    assert err.exit_code == 1
    assert "репозиторий недоступен" in err.stderr


# ---------------------------------------------------------------------------
# 10. RELATIVE-BINARY-CWD: относительный SPINE_BE_BIN + другой cwd клиента
# ---------------------------------------------------------------------------

def test_relative_binary_with_other_cwd(tmp_path, monkeypatch):
    """Относительный SPINE_BE_BIN нормализуется в абсолютный путь (§0):
    клиентский cwd не должен ломать запуск бинаря.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    body = f"import sys\nprint({json.dumps(json.dumps(GREEN_REPORT))})"
    _make_fake(bin_dir, body)

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    monkeypatch.chdir(bin_dir)  # относительный путь резолвится отсюда
    monkeypatch.setenv("SPINE_BE_BIN", "arch-ml-fake")
    client = SpineBE(cwd=work_dir)  # а процесс стартует отсюда
    report = client.control_check("repo")
    assert report.passed is True


# ---------------------------------------------------------------------------
# Специфика Python-SDK: timeout per-call vs дефолт клиента (в обе стороны)
# ---------------------------------------------------------------------------

def test_timeout_precedence_client_default_applies(fake_binary):
    """Без per-call значения действует дефолт клиента (0.3 с < сна 2 с)."""
    body = "import time\ntime.sleep(2)"
    client = SpineBE(binary=fake_binary(body), timeout=0.3)
    started = time.monotonic()
    with pytest.raises(Timeout):
        client.control_check("repo")
    assert time.monotonic() - started < 5


def test_timeout_precedence_per_call_overrides_default(fake_binary):
    """Per-call timeout=5 перекрывает короткий дефолт клиента (0.3 с): сон 1 с проходит."""
    body = (
        "import time\n"
        "time.sleep(1)\n"
        f"print({json.dumps(json.dumps(GREEN_REPORT))})"
    )
    client = SpineBE(binary=fake_binary(body), timeout=0.3)
    report = client.control_check("repo", timeout=5.0)
    assert report.passed is True


def test_timeout_precedence_per_call_tightens_default(fake_binary):
    """Per-call timeout=0.3 сужает широкий дефолт клиента (120 с)."""
    body = "import time\ntime.sleep(2)"
    client = SpineBE(binary=fake_binary(body))  # дефолт DEFAULT_TIMEOUT
    started = time.monotonic()
    with pytest.raises(Timeout):
        client.run("промпт", timeout=0.3)
    assert time.monotonic() - started < 5


# ---------------------------------------------------------------------------
# Специфика Python-SDK: параметр cwd клиента
# ---------------------------------------------------------------------------

def test_cwd_passed_to_child_process(fake_binary, tmp_path):
    """Процесс наследует cwd клиента (§0): заглушка видит его через getcwd()."""
    work = tmp_path / "subdir"
    work.mkdir()
    body = "import os\nprint(os.getcwd())"
    client = SpineBE(binary=fake_binary(body), cwd=work)
    result = client.run("промпт")
    assert os.path.realpath(result.answer.strip()) == os.path.realpath(str(work))


@requires_arch_be
def test_cwd_relative_repo_live():
    """Живой прогон: относительный repo '.' + cwd на фикстуру гейта (§0, §5)."""
    client = SpineBE(binary=ARCH_BE, timeout=120.0, cwd=GATE_FIXTURES)
    report = client.control_check(".", constraints="CONSTRAINTS.yaml")
    assert report.passed is True
    assert report.issues == []
