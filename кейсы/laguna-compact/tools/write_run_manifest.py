#!/usr/bin/env python3
"""C-012 — генератор ``run_manifest.json`` (AD-2: пиннинг прогона).

Считает фактически, а не со слов:

* ``dataset_sha256`` — sha256 файла датасета (симлинки разыменовываются);
* ``pipeline_version`` / ``pipeline_sha256`` — версия пайплайна = имя файла +
  хеш содержимого (AD-2: «``laguna_pipeline_vN.py`` + хеш файла»);
* ``arch_base_version`` — коммит кейса (архитектурная база прогона).

``image`` не угадывается: берётся из ``--image`` или ``$LAGUNA_IMAGE``, иначе
отказ — выдуманная версия образа делает манифест вредным, а не полезным.

Пути в манифесте относительные: от ``--relative-to`` (по умолчанию — корень
кейса, т.е. каталог над ``runs/``).

Идемпотентность: повторный запуск с теми же входами не перезатирает файл
(содержимое совпадает с точностью до ``created_at``) и возвращает 0. Расхождение
содержимого без ``--force`` — отказ (exit 1), чтобы не затереть манифест
чужого прогона.

Коды возврата::

    0 — манифест записан (или уже был записан с тем же содержимым)
    1 — отказ: файл существует и отличается (нужен --force)
    2 — NOT-VERIFIED: нет входа (датасет/пайплайн/образ)

Запуск::

    python3 tools/write_run_manifest.py --run-dir runs/smoke-20260914 \\
        --dataset datasets/cpt_corpus_v12r_8192_qwen25.npy \\
        --base-model Qwen/Qwen2.5-0.5B --pipeline laguna_pipeline_v8.py \\
        --seed 42 --stages cpt=done,sft=done,eval=pending \\
        --image "$LAGUNA_IMAGE" \\
        --run-version "tools/run_smoke.py@$(sha256sum tools/run_smoke.py | cut -c1-12)" \\
        --dataset-extra sft=datasets/tok/sft_train_v12_8192_qwen25.npz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_REFUSE, EXIT_NOT_VERIFIED = 0, 1, 2

MANIFEST_NAME = "run_manifest.json"
CHUNK = 1 << 20

#: Известные стадии контура — для предупреждения об опечатке.
KNOWN_STAGES = ("cpt", "sft", "rl", "eval", "probe", "docs")
KNOWN_STATUS = ("done", "pending", "failed", "skipped", "partial")


class NotVerified(Exception):
    """Входа нет — манифест без него был бы фикцией."""


def sha256_file(path: Path) -> str:
    """sha256 содержимого (симлинк разыменовывается)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def rel(path: Path, base: Path) -> str:
    """Относительный путь от base; если не получается — абсолютный.

    ``abspath``, а не ``resolve``: симлинк на gb10-shared остаётся видимым как
    ``datasets/...`` (путь от корня кейса), а не превращается в абсолютный путь
    сетевого диска — манифест должен читаться из кейса.
    """
    ap = Path(os.path.abspath(path))
    try:
        return str(ap.relative_to(os.path.abspath(base)))
    except ValueError:
        return str(ap)


def parse_stages(spec: str) -> list[dict]:
    """``cpt=done,sft`` → [{'name': 'cpt', 'status': 'done'}, {'name': 'sft', ...}]."""
    stages = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        name, _, status = raw.partition("=")
        name, status = name.strip(), (status.strip() or "done")
        if not name:
            raise NotVerified(f"--stages: пустое имя стадии в '{raw}'")
        stages.append({"name": name, "status": status})
    if not stages:
        raise NotVerified("--stages: не разобрано ни одной стадии")
    return stages


def parse_extra(spec: str) -> tuple[str, str]:
    """``NAME=PATH`` → (name, path) для дополнительно пиннуемого датасета."""
    name, sep, path = spec.partition("=")
    name, path = name.strip(), path.strip()
    if not sep or not name or not path:
        raise NotVerified(f"--dataset-extra: ожидается NAME=PATH, получено '{spec}'")
    return name, path


def parse_hyperparams(spec: str) -> tuple[str, object]:
    """``NAME=VALUE`` → (имя, значение) для фактического гиперпараметра прогона.

    AD-2 требует от манифеста **фактических** гиперпараметров, а не пересказа
    чужого манифеста: у пайплайна в его собственном ``run_manifest.json``
    ``resync_every`` записан как 25, тогда как RL-петля работает с 10 (ADR-010).
    Поэтому значения сюда передаёт тот, кто их измерил или прочитал в коде, и
    вместе с ними — источник (``--hyperparams-source``).
    """
    name, sep, raw = spec.partition("=")
    name, raw = name.strip(), raw.strip()
    if not sep or not name:
        raise NotVerified(f"--hyperparams: ожидается NAME=VALUE, получено '{spec}'")
    try:  # числа/списки/булевы — как JSON; иначе строка как есть
        value: object = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    return name, value


def git_arch_version(case_root: Path) -> str | None:
    """Версия архитектурной базы: коммит кейса (если он в git)."""
    try:
        out = subprocess.run(["git", "-C", str(case_root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="C-012: записать run_manifest.json прогона (хеши датасета и "
                    "пайплайна, база, образ, сид, стадии).")
    ap.add_argument("--run-dir", required=True, help="каталог прогона (создаётся)")
    ap.add_argument("--dataset", required=True, help="файл датасета прогона")
    ap.add_argument("--base-model", required=True, help="идентификатор весов базы")
    ap.add_argument("--pipeline", required=True, help="файл пайплайна")
    ap.add_argument("--seed", type=int, required=True, help="сид прогона")
    ap.add_argument("--stages", required=True,
                    help="стадии: 'cpt=done,sft=done,eval=pending'")
    ap.add_argument("--run-version", default=None,
                    help="версия прогона: раннер стадий (SPEC §1a — в манифесте "
                         "обе версии: pipeline_version и run_version)")
    ap.add_argument("--dataset-extra", action="append", default=[], metavar="NAME=PATH",
                    help="дополнительно пиннуемый датасет прогона (повторяемый); "
                         "C-012 пиннит основной --dataset, остальные — тем же хешем")
    ap.add_argument("--hyperparams", action="append", default=[], metavar="NAME=VALUE",
                    help="фактический гиперпараметр прогона (повторяемый): AD-2 требует "
                         "гиперпараметры по факту, а не пересказ манифеста пайплайна")
    ap.add_argument("--hyperparams-source", default=None,
                    help="откуда взяты гиперпараметры (напр. 'код пайплайна, регулярки')")
    ap.add_argument("--image", default=os.environ.get("LAGUNA_IMAGE"),
                    help="версия образа окружения ($LAGUNA_IMAGE)")
    ap.add_argument("--relative-to", default=None,
                    help="база относительных путей (по умолчанию корень кейса)")
    ap.add_argument("--complete", action="store_true",
                    help="все стадии пройдены (pipeline_complete=true)")
    ap.add_argument("--force", action="store_true", help="перезаписать существующий манифест")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    run_dir = Path(args.run_dir)
    dataset, pipeline = Path(args.dataset), Path(args.pipeline)
    try:
        for p, what in ((dataset, "--dataset"), (pipeline, "--pipeline")):
            if not p.is_file():
                raise NotVerified(f"{what}: файл не найден: {p}")
        if not args.image:
            raise NotVerified("не задан --image и пуст $LAGUNA_IMAGE: версия образа "
                              "не угадывается")
        stages = parse_stages(args.stages)
        extras = [parse_extra(spec) for spec in args.dataset_extra]
        hyperparams = dict(parse_hyperparams(spec) for spec in args.hyperparams)
        for name, path in extras:
            if not Path(path).is_file():
                raise NotVerified(f"--dataset-extra {name}: файл не найден: {path}")
    except NotVerified as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    case_root = Path(args.relative_to) if args.relative_to else (
        run_dir.parent.parent if run_dir.parent.name == "runs" else run_dir.parent)

    ds_sha = sha256_file(dataset)
    pl_sha = sha256_file(pipeline)
    arch = git_arch_version(case_root)

    manifest = {
        "dataset_path": rel(dataset, case_root),
        "dataset_sha256": ds_sha,
        "base_model_id": args.base_model,
        "pipeline_path": rel(pipeline, case_root),
        "pipeline_version": f"{pipeline.name}@{pl_sha[:12]}",
        "pipeline_sha256": pl_sha,
        "image": args.image,
        "seed": args.seed,
        "stages": stages,
        "pipeline_complete": bool(args.complete),
        "arch_base_version": arch,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if args.run_version:
        manifest["run_version"] = args.run_version
    for name, path in extras:
        manifest.setdefault("datasets_extra", {})[name] = {
            "path": rel(Path(path), case_root), "sha256": sha256_file(Path(path))}
    if hyperparams:
        manifest["hyperparameters"] = hyperparams
        if args.hyperparams_source:
            manifest["hyperparameters_source"] = args.hyperparams_source

    mf = run_dir / MANIFEST_NAME
    for st in stages:
        if st["name"] not in KNOWN_STAGES:
            print(f"ПРЕДУПРЕЖДЕНИЕ: стадия '{st['name']}' вне известных "
                  f"({', '.join(KNOWN_STAGES)}) — опечатка?", file=sys.stderr)
        if st["status"] not in KNOWN_STATUS:
            print(f"ПРЕДУПРЕЖДЕНИЕ: статус '{st['status']}' вне известных "
                  f"({', '.join(KNOWN_STATUS)})", file=sys.stderr)

    if mf.exists() and not args.force:
        try:
            old = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = None
        same = isinstance(old, dict) and {
            k: v for k, v in old.items() if k != "created_at"} == {
            k: v for k, v in manifest.items() if k != "created_at"}
        if same:
            if args.json:
                print(json.dumps({"written": False, "reason": "unchanged",
                                  "manifest": str(mf)}, ensure_ascii=False))
            else:
                print(f"манифест уже актуален, не перезаписываю: {mf}")
            return EXIT_OK
        msg = (f"{mf} существует и отличается от нового — не перезатираю "
               "(идемпотентность AD-2); сверь прогон или передай --force")
        report = {"written": False, "reason": "exists_and_differs",
                  "manifest": str(mf), "old": old, "new": manifest}
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"ОТКАЗ: {msg}", file=sys.stderr)
        return EXIT_REFUSE

    run_dir.mkdir(parents=True, exist_ok=True)
    tmp = mf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(mf)  # атомарно: прогон не увидит полузаписанный манифест

    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    else:
        print(f"записан: {mf}")
        print(f"  dataset_sha256:    {ds_sha}")
        print(f"  pipeline_version:  {manifest['pipeline_version']}")
        if args.run_version:
            print(f"  run_version:       {manifest['run_version']}")
        print(f"  image:             {manifest['image']}")
        print(f"  seed:              {manifest['seed']}")
        print("  stages:            "
              + ", ".join(f"{s['name']}={s['status']}" for s in stages))
        if hyperparams:
            print("  hyperparameters:   "
                  + ", ".join(f"{k}={v}" for k, v in hyperparams.items())
                  + (f"  ({args.hyperparams_source})" if args.hyperparams_source else ""))
        print(f"  pipeline_complete: {manifest['pipeline_complete']}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
