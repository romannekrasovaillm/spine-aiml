#!/usr/bin/env python3
"""Parity-пример: каноническая сводка четырёх вызовов SDK одной строкой JSON.

Кросс-языковое сравнение поведения SDK (sdk/CONTRACT.md): Rust и Java
parity-примеры печатают ту же строку. Запуск из корня монорепо:

    python3 sdk/python/examples/parity.py [КОРЕНЬ_РЕПО]
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spine_be_sdk import SpineBE  # noqa: E402


def main() -> None:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
    fixtures = repo / "banking/demos/cli-from-claude-code"
    gate = fixtures / "scenario3-gate/fixtures"
    ir1 = fixtures / "scenario2-archify-cli/sbp-v1.architecture.json"
    ir2 = fixtures / "scenario2-archify-cli/sbp-v2.architecture.json"

    client = SpineBE()
    green = client.control_check(gate, constraints=gate / "CONSTRAINTS.yaml")
    validate = client.archify_validate("architecture", ir1)
    out = Path(tempfile.gettempdir()) / "sdk-parity-delta-py.html"
    compare = client.archify_compare(ir1, ir2, out)
    summary = compare["summary"]

    print(json.dumps({
        "green_passed": green.passed,
        "green_issues": len(green.issues),
        "validate_ok": validate["ok"],
        "checks": len(validate["checks"]),
        "compare_ok": compare["ok"],
        "comp_added": summary["components"]["added"],
        "conn_added": summary["connections"]["added"],
    }, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
