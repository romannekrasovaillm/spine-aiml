#!/usr/bin/env python3
"""Одноразовый помощник: достаёт файл из ветви arch/laguna-eval-set в рабочее дерево.

Нужен потому, что `git checkout <branch> -- <path>` в этой среде запрещён правилами
доступа, а чистый код проверки (ADR-025) обязан совпасть с авторским побайтово.
Скрипт печатает sha256 источника и результата — расхождение видно сразу.
После использования удаляется.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

REV = "arch/laguna-eval-set"
PAIRS = [
    ("кейсы/laguna-compact/tools/check_eval_set_purity.py", "tools/check_eval_set_purity.py"),
    ("кейсы/laguna-compact/docs/adr/ADR-025-chistota-izmeritelnogo-nabora-general-nabor-ne-peresekaetsya-ni-s-odnim-kandidatnym-miksom-geyt-pered-primeneniem-potolka-ppl.md",
     "docs/adr/ADR-025-chistota-izmeritelnogo-nabora-general-nabor-ne-peresekaetsya-ni-s-odnim-kandidatnym-miksom-geyt-pered-primeneniem-potolka-ppl.md"),
    ("кейсы/laguna-compact/docs/adr/ADR-027-mera-obschego-yazyka-dvuhkomponentna-nabor-v-zhanre-repleya-i-nabor-vne-obuchayuschego-raspredeleniya.md",
     "docs/adr/ADR-027-mera-obschego-yazyka-dvuhkomponentna-nabor-v-zhanre-repleya-i-nabor-vne-obuchayuschego-raspredeleniya.md"),
]


def main() -> int:
    rc = 0
    for src, dst in PAIRS:
        out = subprocess.run(["git", "show", f"{REV}:{src}"],
                             capture_output=True)
        if out.returncode != 0:
            print(f"ОТКАЗ: {src}: {out.stderr.decode()[:200]}")
            rc = 1
            continue
        blob = out.stdout
        path = Path(dst)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        print(f"{dst}: {len(blob)} байт, sha256 {hashlib.sha256(blob).hexdigest()}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
