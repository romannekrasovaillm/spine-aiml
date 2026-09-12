#!/usr/bin/env python3
"""Прогон рук по развёрнутым ячейкам.

Идемпотентен: ячейка с непустым `answer.md` пропускается (кроме `--force`).
Команды рук зафиксированы пререгистрацией (cwd = `work/`):

    spine-*     arch-ml run -q --model {model} --timeout {secs} <prompt>
    claude-*    claude -p <prompt> --model {model} --output-format text
                       --dangerously-skip-permissions [--mcp-config .mcp.arch.json]

stdout → `cells/<CELL>/answer.md`, stderr → `cells/<CELL>/errors.txt`;
ненулевой код или ответ короче `MIN_ANSWER_BYTES` → `meta.json:error`.
Ячейка при этом остаётся: провал учитывается гейтом как есть.

Глобальный лимит `SVC_MAX_SECS` (стоп-правило §7 пререгистрации): по
исчерпании раннер останавливается, незавершённые ячейки не досочиняются.

Использование:
    SVC_RUN=<RUN-ID> python3 runners/run_matrix.py --dry-run
    SVC_RUN=<RUN-ID> python3 runners/run_matrix.py
                     [--only-cell C] [--only-task T] [--only-arm A]
                     [--models m1,m2] [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import svclib  # noqa: E402

#: Дополнительный запас к таймауту руки на запуск процесса (секунды).
PROCESS_SLACK_SECS = 60


def elide_prompt(argv: list[str], prompt: str) -> str:
    """Команда для печати: элемент-промпт свёрнут в плейсхолдер.

    Свёртка по равенству, а не по позиции: у Spine промпт — последний
    аргумент, у Claude Code — второй, перед `--model`.
    """
    return " ".join(
        f"<prompt {len(prompt)}b>" if arg == prompt else shlex.quote(arg) for arg in argv
    )


def arm_argv(cell_meta: dict, work: Path, prompt: str) -> list[str]:
    """Команда руки (argv, без шелла): одна и та же строка модели у всех рук."""
    arm, model = cell_meta["arm"], cell_meta["model"]
    if arm in svclib.SPINE_ARMS:
        return [
            svclib.arch_bin(),
            "run",
            "-q",
            "--model",
            model,
            "--timeout",
            str(svclib.ARM_TIMEOUT_SECS),
            prompt,
        ]
    argv = [
        svclib.claude_bin(),
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ]
    if arm in svclib.MCP_ARMS:
        argv += ["--mcp-config", str(work / ".mcp.arch.json")]
    return argv


def gen_error(exit_code: int, answer: bytes, err: str) -> str | None:
    """Причина сбоя генерации или `None`, если ответ годен."""
    if exit_code != 0:
        tail = err.strip().splitlines()[-1] if err.strip() else ""
        return f"exit {exit_code}: {tail[:300]}"
    if len(answer) < svclib.MIN_ANSWER_BYTES:
        return f"short answer: {len(answer)} bytes < {svclib.MIN_ANSWER_BYTES}"
    return None


def run_cell(cell_dir: Path, dry: bool) -> dict:
    """Прогнать одну ячейку; вернуть событие для `logs/generations.jsonl`."""
    meta_path = cell_dir / "meta.json"
    meta = svclib.read_json(meta_path) or {}
    work = cell_dir / "work"
    prompt = svclib.read_text(cell_dir / "prompt.txt")
    argv = arm_argv(meta, work, prompt)

    event = {
        "cell": cell_dir.name,
        "arm": meta.get("arm", ""),
        "model": meta.get("model", ""),
        "task": meta.get("task", ""),
        "rep": meta.get("rep", 0),
        "argv": argv[:-1] + ["<prompt>"] if argv else [],
        "started_at": svclib.now_iso(),
        "secs": 0.0,
        "exit_code": None,
        "bytes": 0,
        "error": None,
        "dry_run": dry,
    }
    if dry:
        return event

    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(work),
            capture_output=True,
            timeout=svclib.ARM_TIMEOUT_SECS + PROCESS_SLACK_SECS,
        )
        exit_code, out, err_bytes = proc.returncode, proc.stdout, proc.stderr
        err_text = err_bytes.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        out = exc.stdout or b""
        err_text = f"timeout after {svclib.ARM_TIMEOUT_SECS + PROCESS_SLACK_SECS}s"
    except OSError as exc:
        exit_code = 127
        out = b""
        err_text = f"spawn failed: {exc}"

    elapsed = round(time.monotonic() - started, 1)
    (cell_dir / "answer.md").write_bytes(out)
    (cell_dir / "errors.txt").write_text(err_text, encoding="utf-8")

    err = gen_error(exit_code, out, err_text)
    event.update(
        {"secs": elapsed, "exit_code": exit_code, "bytes": len(out), "error": err}
    )

    meta["generation"] = {
        "finished_at": svclib.now_iso(),
        "secs": elapsed,
        "exit_code": exit_code,
        "bytes": len(out),
        "error": err,
    }
    svclib.write_json(meta_path, meta)
    return event


def git_rev() -> str:
    """Ревизия репозитория харнесса (`git rev-parse --short HEAD`) или `unknown`."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(svclib.REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def lock_sha256() -> str:
    """sha256 файла `prereg.lock.json` (или `MISSING`)."""
    data = svclib.sha256_file(svclib.LOCK_PATH)
    return data or "MISSING"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="показать команды без запуска")
    parser.add_argument("--only-cell", default="", help="фильтр по имени ячейки")
    parser.add_argument("--only-task", default="", help="фильтр по задаче")
    parser.add_argument("--only-arm", default="", help="фильтр по руке")
    parser.add_argument("--models", default="", help="список моделей через запятую")
    parser.add_argument("--limit", type=int, default=0, help="не более N ячеек за запуск")
    parser.add_argument("--force", action="store_true", help="перегенерировать готовые ячейки")
    args = parser.parse_args()

    run_id = os.environ.get("SVC_RUN", "").strip()
    if not run_id:
        parser.error("не задан SVC_RUN=<RUN-ID>: прогон должен быть развёрнут prepare_cells.py")

    cells = sorted(p for p in svclib.cells_dir(run_id).glob("*") if (p / "meta.json").is_file())
    if not cells:
        parser.error(f"нет ячеек в {svclib.cells_dir(run_id)} — сначала prepare_cells.py")

    models = tuple(args.models.split(",")) if args.models else ()
    selected = []
    for cell_dir in cells:
        meta = svclib.read_json(cell_dir / "meta.json") or {}
        if args.only_cell and cell_dir.name != args.only_cell:
            continue
        if args.only_task and meta.get("task") != args.only_task:
            continue
        if args.only_arm and meta.get("arm") != args.only_arm:
            continue
        if models and meta.get("model") not in models:
            continue
        selected.append(cell_dir)

    log_path = svclib.run_dir(run_id) / "logs" / "generations.jsonl"
    started = time.monotonic()
    done = skipped = failed = 0

    for cell_dir in selected:
        if args.limit and done >= args.limit:
            break
        if time.monotonic() - started > svclib.MAX_RUN_SECS:
            print(f"стоп-правило: исчерпан SVC_MAX_SECS={svclib.MAX_RUN_SECS}s", file=sys.stderr)
            break

        answer = cell_dir / "answer.md"
        if answer.exists() and answer.stat().st_size > 0 and not args.force:
            skipped += 1
            continue
        if args.dry_run:
            work = cell_dir / "work"
            meta = svclib.read_json(cell_dir / "meta.json") or {}
            prompt = svclib.read_text(cell_dir / "prompt.txt")
            print(f"[dry] {cell_dir.name}")
            print(f"      cwd={work}")
            print(f"      {elide_prompt(arm_argv(meta, work, prompt), prompt)}")
            done += 1
            continue

        event = run_cell(cell_dir, dry=False)
        svclib.append_jsonl(log_path, event)
        status = event["error"] or "ok"
        if event["error"]:
            failed += 1
        done += 1
        print(f"{cell_dir.name}: {status} ({event['secs']}s, {event['bytes']}b)")

    if args.dry_run:
        print(f"\n[dry] ячеек к запуску: {done}, уже готово: {skipped}")
        return 0

    run_meta_path = svclib.run_dir(run_id) / "meta.json"
    run_meta = svclib.read_json(run_meta_path) or {}
    run_meta.update(
        {
            "run": run_id,
            "git_rev": git_rev(),
            "prereg_lock_sha256": lock_sha256(),
            "arch_ml_version": svclib.arch_version(),
            "finished_at": svclib.now_iso(),
            "cells_total": len(cells),
            "cells_done": sum(
                1
                for c in cells
                if (c / "answer.md").is_file() and (c / "answer.md").stat().st_size > 0
            ),
            "deviations": run_meta.get("deviations", []),
        }
    )
    run_meta.setdefault("started_at", svclib.now_iso())
    svclib.write_json(run_meta_path, run_meta)

    print(f"\nсгенерировано {done} (сбоев {failed}), пропущено готовых {skipped}")
    print("дальше: SVC_RUN=" + run_id + " python3 runners/gate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
