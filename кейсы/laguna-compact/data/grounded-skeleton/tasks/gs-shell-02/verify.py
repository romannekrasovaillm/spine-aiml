#!/usr/bin/env python3
"""Верификатор gs-shell-02 (G3: бинарный, внешний, execution-grounded).

Отличие от остальных задач набора: предъявлен не результат, а **программа**.
Верификатор берёт из снимка состояния `bin/aggregate.sh`, приносит СВОЙ набор
данных (`holdout/`, которого агент не видел), запускает скрипт в чистом
контейнере (--network none, CWD=/work) и сравнивает полученный файл с суммой,
вычисленной независимо здесь же в Python.

Почему так: зашитое число и «ответ словами» не проходят — сверка идёт на
другом входе. Это приём карточки `execution-grounded-benchmark-gen` (ADR-049,
раздел «Источники приёмов»): эталон строится исполнением, а не оценкой.

Контракт выхода — как у остальных верификаторов набора.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ALLOWED_READ_PREFIXES = ("snapshot:", "pristine:", "journal:", "holdout:", "reexec:")


def expected(holdout: pathlib.Path) -> int:
    total = 0
    for csv in sorted((holdout / "data").glob("*.csv")):
        for n, line in enumerate(csv.read_text(encoding="utf-8").splitlines()):
            if n == 0 or not line.strip():
                continue
            total += int(line.split(",")[1])
    return total


def emit(score: int, reason: str, expected_public: str, reads: list[str], detail: dict) -> int:
    assert score in (0, 1)
    for r in reads:
        assert r.split(":", 1)[0] + ":" in ALLOWED_READ_PREFIXES, r
    print(
        json.dumps(
            {
                "score": score,
                "reason": reason,
                "expected_public": expected_public,
                "reads": reads,
                "detail": detail,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True, type=pathlib.Path)
    ap.add_argument("--task", required=True, type=pathlib.Path)
    ap.add_argument("--seed", required=True, type=pathlib.Path)
    ap.add_argument("--image", required=True)
    ap.add_argument("--holdout", required=True, type=pathlib.Path)
    ap.add_argument("--journal", default="")
    # Каталог для staging-копии под docker-монтирование. Обязателен: docker на
    # этом хосте (snap-конфайнмент) не видит /tmp, монтировать можно только из $HOME.
    ap.add_argument("--scratch", required=True, type=pathlib.Path)
    args = ap.parse_args()

    want = expected(args.holdout)
    reads = ["snapshot:bin/aggregate.sh", "holdout:data/*.csv", "reexec:/work/out/aggregate.txt"]
    script = args.snapshot / "bin" / "aggregate.sh"
    if not script.is_file():
        return emit(0, "bin/aggregate.sh отсутствует — состояние не изменилось", str(want), reads, {})

    args.scratch.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="gs-shell-02-reexec-", dir=args.scratch))
    try:
        stage = tmp / "work"
        (stage / "bin").mkdir(parents=True)
        shutil.copy2(script, stage / "bin" / "aggregate.sh")
        shutil.copytree(args.holdout / "data", stage / "data")
        (stage / "out").mkdir()
        # Права: скрипт предъявлен агентом, поэтому запускаем его тем же
        # непривилегированным пользователем, что и остальные прогоны.
        for p in stage.rglob("*"):
            p.chmod(0o777 if p.is_dir() else 0o666)
        rc = subprocess.run(
            [
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--tmpfs", "/tmp", "--user", "1000:1000",
                "--memory", "256m", "--pids-limit", "256",
                "-v", f"{stage}:/work", "-w", "/work", args.image,
                "sh", "-c", "sh bin/aggregate.sh",
            ],
            capture_output=True, text=True, timeout=60,
        )
        produced = stage / "out" / "aggregate.txt"
        detail = {
            "reexec_exit_code": rc.returncode,
            "reexec_stderr_tail": rc.stderr[-200:],
            "holdout_files": sorted(p.name for p in (args.holdout / "data").glob("*.csv")),
        }
        if rc.returncode != 0:
            return emit(0, f"предъявленный скрипт на удержанных данных завершился кодом {rc.returncode}", str(want), reads, detail)
        if not produced.is_file():
            return emit(0, "предъявленный скрипт не создал out/aggregate.txt на удержанных данных", str(want), reads, detail)
        raw = produced.read_text(encoding="utf-8", errors="replace").strip()
        toks = raw.split()
        if len(toks) != 1 or not toks[0].lstrip("-").isdigit():
            detail["raw"] = raw[:80]
            return emit(0, "out/aggregate.txt — не одно целое число", str(want), reads, detail)
        got = int(toks[0])
        detail["got"] = got
        if got != want:
            return emit(0, f"на удержанных данных получено {got}, ожидалось {want}", str(want), reads, detail)
        return emit(1, f"скрипт верен: на удержанных данных {got} совпало с независимым пересчётом", str(want), reads, detail)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
