"""Ошибки SDK по §4 контракта (sdk/CONTRACT.md).

Иерархия едина для всех языков SDK: BinaryNotFound, Timeout, ProcessFailed,
ContractViolation. `CheckFailed` (control check с passed=false) — НЕ ошибка
исполнения: отчёт передаётся как данные (FitnessReport.passed == False).
"""


class SpineBEError(Exception):
    """Базовый класс ошибок SDK."""


class BinaryNotFound(SpineBEError):
    """Бинарь `arch-ml` не найден (SPINE_BE_BIN/PATH) или не исполняемый."""

    def __init__(self, binary: str):
        self.binary = binary
        super().__init__(f"бинарь arch-ml не найден или не исполняем: {binary!r}")


class Timeout(SpineBEError):
    """Истёк клиентский таймаут; процесс убит."""

    def __init__(self, timeout: float, argv: list[str]):
        self.timeout = timeout
        self.argv = argv
        super().__init__(f"таймаут {timeout} с: {argv!r}")


class ProcessFailed(SpineBEError):
    """Ненулевой exit без валидного JSON-контракта. Несёт exit_code и stderr."""

    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(
            f"arch-ml завершился с кодом {exit_code}: {stderr.strip()!r}"
        )


class ContractViolation(SpineBEError):
    """stdout не парсится как JSON там, где контракт требует JSON."""

    def __init__(self, stdout: str):
        self.stdout = stdout
        super().__init__(
            "stdout не является валидным JSON: " + stdout[:200].strip()
        )
