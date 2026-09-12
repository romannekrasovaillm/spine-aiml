#!/usr/bin/env python3
"""Разворачивание ячеек прогона по матрице.

Для каждой ячейки `runs/<RUN>/cells/<CELL>/` создаёт:

    prompt.txt          постановка TASK+CONTEXT+SPEC+ACCEPTANCE (одинакова у всех рук)
    work/               рабочий каталог руки (репозиторий-заготовка)
    meta.json           кто/что/чем посеяно (`seeded`: путь → sha256)

Рабочий каталог у всех рук один и тот же по базе; spine-пакет (`pack/**`)
добавляется только рукам с корпусом (см. `svclib.CORPUS_ARMS`). `ACCEPTANCE.md`
кладётся только для чтения и защищён хэшем: его правка ловится гейтом.

Использование:
    python3 runners/prepare_cells.py <RUN-ID> [--force]
                                     [--only-task T] [--only-arm A]
                                     [--models m1,m2] [--reps N]
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import svclib  # noqa: E402

#: Каталоги репозитория-заготовки внутри `work/`.
SCAFFOLD_DIRS: tuple[str, ...] = ("docs", "docs/adr", "model", "artifacts")

#: Файлы, отдаваемые руке только для чтения (правка ловится гейтом).
READONLY_FILES: tuple[str, ...] = ("ACCEPTANCE.md",)

#: Права «только чтение» (владелец/группа/остальные).
READONLY_MODE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH

#: Права на запись владельцу (для переразвёртывания по `--force`).
WRITABLE_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH


def seed_relative(rel: str, data: bytes, work: Path, seeded: dict[str, str]) -> None:
    """Записать посев `<work>/<rel>` и зафиксировать его sha256."""
    dst = work / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.chmod(WRITABLE_MODE)  # снять прошлую защиту чтения при --force
    dst.write_bytes(data)
    seeded[rel] = svclib.sha256_bytes(data)


def build_cell(cell: dict, run_id: str, force: bool) -> str:
    """Развернуть одну ячейку. Возвращает `created|skipped`."""
    cell_dir = svclib.cells_dir(run_id) / cell["cell"]
    if (cell_dir / "meta.json").exists() and not force:
        return "skipped"

    work = cell_dir / "work"
    seeded: dict[str, str] = {}

    for rel in SCAFFOLD_DIRS:
        (work / rel).mkdir(parents=True, exist_ok=True)

    repo_readme = (
        f"# {cell['task']} — рабочий каталог\n\n"
        "Репозиторий-заготовка. Артефакты результата кладите сюда, в текущий каталог.\n"
    ).encode("utf-8")
    seed_relative("README.md", repo_readme, work, seeded)

    for name in svclib.PROMPT_FILES:
        seed_relative(name, (svclib.task_dir(cell["task"]) / name).read_bytes(), work, seeded)

    for name in READONLY_FILES:
        (work / name).chmod(READONLY_MODE)

    if cell["arm"] in svclib.CORPUS_ARMS:
        for name in svclib.PACK_FILES:
            seed_relative(name, (svclib.PACK_DIR / name).read_bytes(), work, seeded)

    config = os.environ.get("SVC_ARCH_CONFIG")
    if config and cell["arm"] in svclib.SPINE_ARMS:
        src = Path(config)
        if src.is_file():
            seed_relative("arch-ml.toml", src.read_bytes(), work, seeded)

    if cell["arm"] in svclib.MCP_ARMS:
        tpl = svclib.read_text(svclib.RUNNERS_DIR / "mcp.arch.json.tpl")
        rendered = tpl.replace("{{ARCH_ML}}", svclib.arch_bin())
        seed_relative(".mcp.arch.json", rendered.encode("utf-8"), work, seeded)

    cell_dir.mkdir(parents=True, exist_ok=True)
    (cell_dir / "prompt.txt").write_text(svclib.build_prompt(cell["task"]), encoding="utf-8")

    svclib.write_json(
        cell_dir / "meta.json",
        {
            "run": run_id,
            "cell": cell["cell"],
            "task": cell["task"],
            "arm": cell["arm"],
            "model": cell["model"],
            "rep": cell["rep"],
            "created_at": svclib.now_iso(),
            "corpus": cell["arm"] in svclib.CORPUS_ARMS,
            "seeded": seeded,
            "arch_ml": svclib.arch_bin(),
            "arch_ml_version": svclib.arch_version(),
        },
    )
    return "created"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", help="идентификатор прогона (RUN-ID)")
    parser.add_argument("--force", action="store_true", help="переразвернуть существующие ячейки")
    parser.add_argument("--only-task", default="", help="фильтр по задаче")
    parser.add_argument("--only-arm", default="", help="фильтр по руке")
    parser.add_argument("--models", default="", help="список моделей через запятую")
    parser.add_argument("--reps", type=int, default=svclib.REPS, help="число повторов")
    args = parser.parse_args()

    tasks = (args.only_task,) if args.only_task else svclib.TASKS
    arms = (args.only_arm,) if args.only_arm else svclib.ARMS
    models = tuple(args.models.split(",")) if args.models else svclib.MODELS

    for name, values, known in (
        ("--only-task", tasks, svclib.TASKS),
        ("--only-arm", arms, svclib.ARMS),
        ("--models", models, None),
    ):
        if known is not None:
            bad = [v for v in values if v not in known]
            if bad:
                parser.error(f"{name}: неизвестные значения {bad}")

    svclib.cells_dir(args.run).mkdir(parents=True, exist_ok=True)
    counts = {"created": 0, "skipped": 0}
    for cell in svclib.matrix(tasks, arms, models, args.reps):
        counts[build_cell(cell, args.run, args.force)] += 1

    total = sum(counts.values())
    print(f"ячеек: {total} (создано {counts['created']}, пропущено {counts['skipped']})")
    print(f"каталог: {svclib.cells_dir(args.run)}")
    print()
    print(f"дальше: SVC_RUN={args.run} python3 runners/run_matrix.py --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
