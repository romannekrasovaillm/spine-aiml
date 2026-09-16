#!/usr/bin/env python3
"""C-012 — поведенческий страж AD-2 «прогон без манифеста не является доказательством».

На каждый каталог прогона ``<runs>/<id>/`` требует ``run_manifest.json`` с
полями пиннинга: ``dataset_sha256``, ``base_model_id``, ``pipeline_version``,
``image``, ``seed``, ``stages[]``, ``pipeline_complete``. Пути в манифесте —
относительные от корня кейса (абсолютный путь не переживает переезд прогона).

Вакуумная истина: прогонов ещё нет (нет каталога ``--runs`` или в нём нет
подкаталогов) — exit 0, ``no runs yet``. Это не «зелено по умолчанию»: правило
говорит «каждый каталог прогона несёт манифест», и при нуле каталогов ему
нечего нарушать. Отсутствие каталога не отличимо от «ещё не стартовали» — и
именно так это и печатается.

Прогон с ``pipeline_complete: false`` — **валидный** манифест (AD-2 требует
явной пометки частичности), но гейт по нему не закрывается: печатается
ПРЕДУПРЕЖДЕНИЕ. ``--require-complete`` превращает это в нарушение для гейта
приёмки.

Не-прогоны. Правило говорит про **каталоги прогонов**, а в ``runs/`` живут и не
прогоны: пул ревизии ``runs/rev-pool/`` (C-017 требует именно этот путь). Требовать
у пула манифест AD-2 значит требовать фикцию. Каталог объявляет себя не-прогоном
**файлом-маркером** ``NOT_A_RUN`` (первая строка — причина); каталог с маркером
печатается как пропущенный. Молчаливого исключения по имени каталога нет: имя —
не доказательство, а маркер — явное утверждение, которое видно в diff.

Маркер рядом с манифестом — **нарушение**, а не пропуск: каталог не может быть
одновременно прогоном и не-прогоном, и пропуск здесь означал бы, что маркер
прячет прогон от AD-2.

Коды возврата::

    0 — все каталоги прогонов несут конформный манифест (или прогонов нет)
    1 — нарушение: поимённый список в stdout
    2 — NOT-VERIFIED: ``--runs`` указывает на файл, а не каталог

Запуск::

    python3 tools/check_run_manifest.py --runs runs/
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

MANIFEST_NAME = "run_manifest.json"

#: Файл-маркер «каталог не является прогоном» (см. шапку). Первая строка — причина.
NOT_A_RUN_MARKER = "NOT_A_RUN"

#: Обязательные поля и их человеческое описание (AD-2).
REQUIRED = {
    "dataset_sha256": "sha256 датасета",
    "base_model_id": "идентификатор весов базы",
    "pipeline_version": "версия пайплайна (файл + хеш)",
    "image": "версия образа окружения",
    "seed": "сид прогона",
    "stages": "исполненные/запланированные стадии",
    "pipeline_complete": "признак полного прохождения стадий",
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
#: Поля-пути: обязаны быть относительными.
PATH_KEY_RE = re.compile(r"(path|dir|file|log|ckpt|checkpoint)", re.I)
#: Хеши и идентификаторы не проверяем на «относительность».
HASH_KEY_RE = re.compile(r"(sha|hash|sum|id)$", re.I)


class NotVerified(Exception):
    """Вход непригоден для суждения."""


def check_relative(key: str, value) -> list[str]:
    """Путь не должен быть абсолютным и не должен выходить вверх через ``..``."""
    problems: list[str] = []
    if HASH_KEY_RE.search(key):
        return problems
    if not PATH_KEY_RE.search(key):
        return problems
    items = value if isinstance(value, list) else [value]
    for v in items:
        if not isinstance(v, str) or not v:
            continue
        if v.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", v):
            problems.append(f"{key}: абсолютный путь '{v}' "
                            f"(AD-2: пути относительные от корня кейса)")
        elif ".." in Path(v).parts:
            problems.append(f"{key}: путь с '..' — '{v}'")
    return problems


def read_marker(path: Path) -> str:
    """Причина «не прогон» — первая непустая строка маркера (для отчёта)."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                return line.strip()
    except OSError as e:
        return f"маркер не читается: {e}"
    return "(причина не указана)"


def check_manifest(obj: dict) -> tuple[list[str], list[str]]:
    """Возвращает (нарушения, предупреждения) для одного манифеста."""
    problems: list[str] = []
    warnings: list[str] = []
    for field, what in REQUIRED.items():
        if field not in obj:
            problems.append(f"нет обязательного поля '{field}' ({what})")
    if problems:
        return problems, warnings

    if not (isinstance(obj["dataset_sha256"], str) and SHA256_RE.match(obj["dataset_sha256"])):
        problems.append("dataset_sha256: не sha256 в нижнем регистре (64 hex)")
    if not (isinstance(obj["base_model_id"], str) and obj["base_model_id"].strip()):
        problems.append("base_model_id: пусто или не строка")
    if not (isinstance(obj["pipeline_version"], str) and obj["pipeline_version"].strip()):
        problems.append("pipeline_version: пусто или не строка")
    if not (isinstance(obj["image"], str) and obj["image"].strip()):
        problems.append("image: пусто или не строка")
    seed = obj["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        problems.append(f"seed: не целое ({seed!r})")
    if not isinstance(obj["pipeline_complete"], bool):
        problems.append(f"pipeline_complete: не bool ({obj['pipeline_complete']!r})")
    stages = obj["stages"]
    if not isinstance(stages, list) or not stages:
        problems.append("stages: пусто или не список")
    else:
        for i, st in enumerate(stages):
            if not isinstance(st, dict):
                problems.append(f"stages[{i}]: не объект ({st!r})")
                continue
            if not (isinstance(st.get("name"), str) and st["name"].strip()):
                problems.append(f"stages[{i}]: нет непустого 'name'")
            status = st.get("status")
            if not (isinstance(status, str) and status.strip()):
                problems.append(f"stages[{i}] ({st.get('name')}): нет непустого 'status' "
                                f"(стадия без статуса неотличима от забытой)")
            for k, v in st.items():
                problems.extend(f"stages[{i}].{p}" for p in check_relative(k, v))
    for k, v in obj.items():
        if k == "stages":
            continue
        problems.extend(check_relative(k, v))

    if obj.get("pipeline_complete") is False:
        done = [s.get("name") for s in stages
                if isinstance(s, dict) and s.get("status") in ("done", "complete", "ok")]
        warnings.append("pipeline_complete=false — прогон помечен частичным, "
                        f"гейт по нему не закрывается (завершено стадий: {done or 'нет'})")
    return problems, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="C-012: каждый каталог прогона несёт run_manifest.json "
                    "с хешами данных/весов/пайплайна, образом и сидом.")
    ap.add_argument("--runs", default="runs/", help="каталог прогонов (по умолчанию runs/)")
    ap.add_argument("--require-complete", action="store_true",
                    help="pipeline_complete=false считать нарушением (гейт приёмки)")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    runs = Path(args.runs)
    report: dict = {"check": "C-012", "runs_dir": str(runs), "runs": [],
                    "skipped": [], "violations": [], "warnings": []}

    if runs.exists() and not runs.is_dir():
        print(f"NOT-VERIFIED: --runs указывает не на каталог: {runs}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if not runs.is_dir():
        report["note"] = "no runs yet"
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"== C-012 / AD-2: манифесты прогонов ==\nкаталог {runs} отсутствует")
            print("\nno runs yet")
        return EXIT_OK

    run_dirs = sorted(d for d in runs.iterdir() if d.is_dir())
    if not run_dirs:
        report["note"] = "no runs yet"
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"== C-012 / AD-2: манифесты прогонов ==\nв {runs} нет подкаталогов")
            print("\nno runs yet")
        return EXIT_OK

    for d in run_dirs:
        mf = d / MANIFEST_NAME
        marker = d / NOT_A_RUN_MARKER
        entry = {"run": d.name, "manifest": str(mf)}
        if marker.is_file():
            entry["not_a_run"] = read_marker(marker)
            if mf.is_file():
                # Противоречие: каталог не может быть одновременно прогоном и не
                # прогоном. Пропуск здесь означал бы, что маркер прячет прогон от
                # AD-2, — поэтому это нарушение, а не предупреждение.
                entry["problems"] = [
                    f"в каталоге есть и {NOT_A_RUN_MARKER}, и {MANIFEST_NAME}: "
                    f"каталог объявлен не-прогоном, но несёт манифест — "
                    f"убери маркер или перенеси прогон (AD-2); маркер не должен "
                    f"прятать прогон за маркером"]
            report["skipped"].append(entry)
            report["violations"].extend(f"{d.name}: {p}" for p in entry.get("problems", []))
            continue
        if not mf.is_file():
            entry["problems"] = [f"нет {MANIFEST_NAME} — прогон без манифеста "
                                 f"не является доказательством (AD-2)"]
        else:
            try:
                obj = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                entry["problems"] = [f"{MANIFEST_NAME} не читается как JSON: {e}"]
                obj = None
            if obj is not None:
                if not isinstance(obj, dict):
                    entry["problems"] = ["манифест не объект JSON"]
                else:
                    problems, warnings = check_manifest(obj)
                    if args.require_complete and obj.get("pipeline_complete") is False:
                        problems.append("pipeline_complete=false, а гейт требует "
                                        "полного прохождения стадий")
                    entry["problems"] = problems
                    entry["warnings"] = warnings
                    entry["pipeline_complete"] = obj.get("pipeline_complete")
        report["runs"].append(entry)
        report["violations"].extend(f"{d.name}: {p}" for p in entry.get("problems", []))
        report["warnings"].extend(f"{d.name}: {w}" for w in entry.get("warnings", []))

    ok = not report["violations"]
    report["ok"] = ok

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK if ok else EXIT_FAIL

    print("== C-012 / AD-2: манифесты прогонов ==")
    print(f"каталог: {runs}  прогонов: {len(run_dirs) - len(report['skipped'])}")
    print()
    for e in report["runs"]:
        state = "OK" if not e.get("problems") else f"FAIL ({len(e['problems'])})"
        print(f"  {e['run']:<28} {state}"
              + ("" if e.get("pipeline_complete", True) else "  [частичный]"))
        for p in e.get("problems", []):
            print(f"      - {p}")
    for e in report["skipped"]:
        state = "пропущен (не прогон)" if not e.get("problems") else \
            f"FAIL ({len(e['problems'])}) [маркер+манифест]"
        print(f"  {e['run']:<28} {state}: {e['not_a_run']}")
        for p in e.get("problems", []):
            print(f"      - {p}")
    if report["warnings"]:
        print()
        print("ПРЕДУПРЕЖДЕНИЯ:")
        for w in report["warnings"]:
            print(f"  - {w}")
    print()
    if not ok:
        print(f"FAIL: нарушений AD-2 — {len(report['violations'])}")
        return EXIT_FAIL
    print("RUN MANIFESTS OK")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
