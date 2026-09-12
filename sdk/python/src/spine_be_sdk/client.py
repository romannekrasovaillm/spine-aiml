"""Клиент Spine-BE: тонкая обёртка над headless CLI `arch-ml`.

Контракт: sdk/CONTRACT.md (версия 1). Процесс запускается БЕЗ shell
(argv-массив), сетевых вызовов в SDK нет.

Разрешение бинаря (§0): параметр `binary` → env `SPINE_BE_BIN` → `arch-ml`
из PATH. Рабочий каталог `cwd` наследуется командами, оперирующими
репозиторием или относительными путями.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .errors import BinaryNotFound, ContractViolation, ProcessFailed, Timeout
from .types import FitnessReport, RunResult, now_ms

#: Клиентский таймаут по умолчанию (секунды), переопределяется per-call.
DEFAULT_TIMEOUT = 120.0

_ENV_BIN = "SPINE_BE_BIN"
_BIN_NAME = "arch-ml"


class SpineBE:
    """Клиент `arch-ml`. Один экземпляр — один бинарь и дефолтный таймаут."""

    def __init__(
        self,
        binary: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: str | os.PathLike[str] | None = None,
    ):
        self._binary = str(binary) if binary is not None else None
        self.timeout = timeout
        self.cwd = str(cwd) if cwd is not None else None

    # ------------------------------------------------------------------
    # §0. Разрешение бинаря и запуск процесса
    # ------------------------------------------------------------------

    def _resolve_binary(self) -> str:
        """Путь к бинарю: аргумент → SPINE_BE_BIN → PATH. Иначе BinaryNotFound.

        Путь нормализуется в абсолютный: иначе относительный SPINE_BE_BIN
        резолвился бы от cwd ДОЧЕРНЕГО процесса (параметр `cwd` клиента),
        а не от каталога вызывающего — и exec падал бы с FileNotFoundError.
        """
        candidate = self._binary or os.environ.get(_ENV_BIN)
        if candidate:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return os.path.abspath(candidate)
            raise BinaryNotFound(candidate)
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            path = Path(directory) / _BIN_NAME
            if path.is_file() and os.access(path, os.X_OK):
                return os.path.abspath(path)
        raise BinaryNotFound(_BIN_NAME)

    def _exec(
        self,
        argv: list[str],
        timeout: float | None,
        stdin: str | None = None,
    ) -> tuple[int, str, str]:
        """Запуск без shell. Возвращает (exit_code, stdout, stderr)."""
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            proc = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                # arch-ml (Rust) всегда печатает UTF-8, в т.ч. кириллицу из
                # контракта; локаль клиента на декодирование влиять не должна.
                # errors="replace" — страховка от битых байтов в stderr.
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                cwd=self.cwd,
            )
        except FileNotFoundError as exc:
            raise BinaryNotFound(argv[0]) from exc
        except PermissionError as exc:
            raise BinaryNotFound(argv[0]) from exc
        except subprocess.TimeoutExpired as exc:
            # subprocess.run уже убил процесс и дождался его завершения.
            raise Timeout(effective_timeout, argv) from exc
        return proc.returncode, proc.stdout, proc.stderr

    def _exec_json(
        self, argv: list[str], timeout: float | None = None
    ) -> dict:
        """Команда с JSON-контрактом на stdout.

        Валидный JSON — это данные (даже при ненулевом exit: красный гейт
        `control check` завершается с exit 1 и печатает отчёт). Исключения:
        невалидный JSON при exit 0 → ContractViolation; невалидный JSON при
        ненулевом exit → ProcessFailed (§2, §4).
        """
        exit_code, stdout, stderr = self._exec(argv, timeout)
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            if exit_code == 0:
                raise ContractViolation(stdout) from None
            raise ProcessFailed(exit_code, stderr) from None

    # ------------------------------------------------------------------
    # §1. run — headless-прогон агента (LLM)
    # ------------------------------------------------------------------

    def run(
        self,
        prompt: str,
        model: str | None = None,
        timeout: float | None = None,
        max_turns: int | None = None,
    ) -> RunResult:
        """`arch-ml run -q [--model M] [--max-turns N] PROMPT`.

        stdout — произвольный текст (не JSON). Промпт `-` означает чтение
        промпта из stdin самим CLI (§1). Перед промптом с ведущим '-'
        SDK вставляет разделитель '--' (clap иначе отвергнет его как флаг).
        """
        argv = [self._resolve_binary(), "run", "-q"]
        if model is not None:
            argv += ["--model", model]
        if max_turns is not None:
            argv += ["--max-turns", str(max_turns)]
        # Промпт, начинающийся с '-', clap отвергнет как неизвестный флаг —
        # отделяем его разделителем '--' (проверено на реальном arch-ml).
        # Сентинель '-' (чтение промпта из stdin, §1) передаётся как есть:
        # clap принимает одиночный дефис как позиционное значение.
        if prompt.startswith("-") and prompt != "-":
            argv.append("--")
        argv.append(prompt)

        started = now_ms()
        exit_code, stdout, stderr = self._exec(argv, timeout)
        duration_ms = now_ms() - started
        if exit_code != 0:
            raise ProcessFailed(exit_code, stderr)
        return RunResult(answer=stdout, model=model, duration_ms=duration_ms)

    # ------------------------------------------------------------------
    # §2. control check — fitness-контроль репозитория
    # ------------------------------------------------------------------

    def control_check(
        self,
        repo: str | os.PathLike[str],
        constraints: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
    ) -> FitnessReport:
        """`arch-ml control check <REPO> [--constraints PATH] --json`.

        passed == False (exit 1) — валидный отчёт-данные, НЕ исключение.
        """
        argv = [self._resolve_binary(), "control", "check", str(repo)]
        if constraints is not None:
            argv += ["--constraints", str(constraints)]
        argv.append("--json")
        return FitnessReport.from_json(self._exec_json(argv, timeout))

    # ------------------------------------------------------------------
    # §3. archify — диаграммы (receipt передаётся целиком как dict)
    # ------------------------------------------------------------------

    def archify_validate(
        self,
        type: str,
        path: str | os.PathLike[str],
        timeout: float | None = None,
    ) -> dict:
        """`arch-ml archify validate <TYPE> <IR_PATH> --json` → receipt (§3.1)."""
        argv = [
            self._resolve_binary(),
            "archify", "validate", type, str(path), "--json",
        ]
        return self._exec_json(argv, timeout)

    def archify_deliver(
        self,
        type: str,
        path: str | os.PathLike[str],
        output: str | os.PathLike[str],
        timeout: float | None = None,
    ) -> dict:
        """`arch-ml archify deliver <TYPE> <IR_PATH> <OUT_HTML> --json` → receipt (§3.2)."""
        argv = [
            self._resolve_binary(),
            "archify", "deliver", type, str(path), str(output), "--json",
        ]
        return self._exec_json(argv, timeout)

    def archify_compare(
        self,
        base: str | os.PathLike[str],
        head: str | os.PathLike[str],
        output: str | os.PathLike[str],
        timeout: float | None = None,
    ) -> dict:
        """`arch-ml archify compare <BASE_IR> <HEAD_IR> <OUT_HTML> --json` → receipt (§3.3)."""
        argv = [
            self._resolve_binary(),
            "archify", "compare", str(base), str(head), str(output), "--json",
        ]
        return self._exec_json(argv, timeout)
