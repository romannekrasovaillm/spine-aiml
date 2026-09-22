#!/usr/bin/env python3
"""C-012 — генератор ``run_manifest.json`` (AD-2: пиннинг прогона).

Считает фактически, а не со слов:

* ``dataset_sha256`` — sha256 файла датасета (симлинки разыменовываются);
* ``pipeline_version`` / ``pipeline_sha256`` — версия пайплайна = имя файла +
  хеш содержимого (AD-2: «``laguna_pipeline_vN.py`` + хеш файла»);
* ``run_started_at_commit`` — коммит, из которого **запущен** прогон;
* ``manifest_written_at_commit`` — коммит, в котором **записан** манифест.

Два поля вместо ``arch_base_version`` — ADR-028 п.2: одно имя несло два разных
смысла («откуда запущен прогон» и «где записан манифест»), поэтому поле
исключено из новых манифестов. ``run_started_at_commit`` **не угадывается**:
если коммит запуска неизвестен, пишется ``null`` (ADR-028 п.2: не выдумывать);
``manifest_written_at_commit`` по умолчанию — HEAD кейса в момент записи.
Старые манифесты с ``arch_base_version`` остаются как есть (ADR-028 п.2).

``instruments`` — sha256 приборов, которыми снят прогон (``--instrument``,
повторяемый; словарь путь → {path, sha256}). Причина поля — разбор S3ao:
прогон домен-набора снят редакцией прибора P-4, но **не записал**, какой именно, и
провенанс числа остался на коммите, а не на хеше; отсюда класс «цитата против
дерева» (назван в ``docs/specs/INSTRUMENT-VERSIONS.md`` §2.1.4). Словарь, а не
одно поле ``instrument_sha256``: один прогон снимается несколькими приборами
(в S3ao участвовали ``calib_ppl_probe.py``, ``ppl_probe.py`` и пайплайн), и одно
поле молча оставило бы остальные за бортом. Прибор **не угадывается**: не задан
``--instrument`` — поля нет, а в stderr печатается предупреждение, чтобы дыра
была видна, а не молчала.

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
        --instrument tools/ppl_probe.py --instrument tools/calib_ppl_probe.py \\
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
#: ``running`` — состояние стадии в манифесте, снятом **в момент старта** прогона
#: (S3o: каталог прогона существует с первой минуты, и AD-2 требует, чтобы он нёс
#: манифест; финальный манифест переписывается цепочкой по завершении стадии).
KNOWN_STATUS = ("done", "pending", "running", "failed", "skipped", "partial")


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


def parse_instrument(spec: str) -> str:
    """Путь прибора из ``--instrument`` (имя прибора — сам путь, отдельного имени нет).

    Отличия от ``--dataset-extra``: у датасета имя смысловое (``sft``, ``eval``),
    у прибора идентичность и есть путь — поэтому формат ``PATH``, а не ``NAME=PATH``.
    """
    path = spec.strip()
    if not path:
        raise NotVerified("--instrument: пустой путь прибора")
    return path


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
    ap.add_argument("--instrument", action="append", default=[], metavar="PATH",
                    help="прибор прогона (повторяемый): в манифест пишется его sha256 — "
                         "иначе провенанс числа держится на коммите (класс S3ao)")
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
    ap.add_argument("--manifest-written-at-commit", default=None,
                    help="ADR-028 п.2: коммит, в котором ЗАПИСАН манифест "
                         "(по умолчанию — HEAD кейса в момент записи)")
    ap.add_argument("--run-started-at-commit", default=None,
                    help="ADR-028 п.2: коммит, из которого ЗАПУЩЕН прогон; "
                         "неизвестен — не выдумывать, пишется null")
    ap.add_argument("--note", action="append", default=[], metavar="TEXT",
                    help="примечание к манифесту (повторяемое): факты прогона, "
                         "которые не выражаются полями, — напр. почему стадия "
                         "помечена failed (ADR-028 п.7) или как пиннится датасет")
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
        instruments = [parse_instrument(spec) for spec in args.instrument]
        hyperparams = dict(parse_hyperparams(spec) for spec in args.hyperparams)
        for name, path in extras:
            if not Path(path).is_file():
                raise NotVerified(f"--dataset-extra {name}: файл не найден: {path}")
        for path in instruments:
            if not Path(path).is_file():
                raise NotVerified(f"--instrument: файл прибора не найден: {path}")
    except NotVerified as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    case_root = Path(args.relative_to) if args.relative_to else (
        run_dir.parent.parent if run_dir.parent.name == "runs" else run_dir.parent)

    ds_sha = sha256_file(dataset)
    pl_sha = sha256_file(pipeline)

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
        # ADR-028 п.2: два поля вместо двусмысленного arch_base_version.
        "run_started_at_commit": args.run_started_at_commit,
        "manifest_written_at_commit": (args.manifest_written_at_commit
                                       or git_arch_version(case_root)),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if args.run_version:
        manifest["run_version"] = args.run_version
    for name, path in extras:
        manifest.setdefault("datasets_extra", {})[name] = {
            "path": rel(Path(path), case_root), "sha256": sha256_file(Path(path))}
    if instruments:
        # Ключ словаря — путь прибора, как он назван в манифесте: у прибора нет
        # смыслового имени (в отличие от датасета), а путь — он и есть идентичность.
        # Порядок ключей — порядок флагов; повтор пути перезаписывает значение тем
        # же хешем (идемпотентность AD-2 сохраняется).
        manifest["instruments"] = {
            rel(Path(path), case_root): {
                "path": rel(Path(path), case_root), "sha256": sha256_file(Path(path))}
            for path in instruments
        }
    if hyperparams:
        manifest["hyperparameters"] = hyperparams
        if args.hyperparams_source:
            manifest["hyperparameters_source"] = args.hyperparams_source
    if args.note:
        manifest["notes"] = list(args.note)

    mf = run_dir / MANIFEST_NAME
    for st in stages:
        if st["name"] not in KNOWN_STAGES:
            print(f"ПРЕДУПРЕЖДЕНИЕ: стадия '{st['name']}' вне известных "
                  f"({', '.join(KNOWN_STAGES)}) — опечатка?", file=sys.stderr)
        if st["status"] not in KNOWN_STATUS:
            print(f"ПРЕДУПРЕЖДЕНИЕ: статус '{st['status']}' вне известных "
                  f"({', '.join(KNOWN_STATUS)})", file=sys.stderr)

    if not instruments:
        # Поля нет намеренно (прибор не угадывается: у прогонов CPT/SFT его может
        # не быть вовсе) — но и молчать нельзя: молчание и есть дефект S3ao.
        print("ПРЕДУПРЕЖДЕНИЕ: --instrument не задан — манифест не назовёт редакцию "
              "прибора, которым снят прогон (класс S3ao: провенанс числа останется "
              "на коммите, а не на хеше)", file=sys.stderr)

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
        if instruments:
            print("  instruments:       "
                  + ", ".join(f"{p}@{v['sha256'][:12]}"
                              for p, v in manifest["instruments"].items()))
        print(f"  pipeline_complete: {manifest['pipeline_complete']}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
