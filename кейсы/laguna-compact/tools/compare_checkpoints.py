#!/usr/bin/env python3
"""Побитовое сравнение двух чекпойнтов контура — факт вместо предположения.

Зачем отдельный инструмент. Детерминизм-проба (ADR-009) утверждает: «два прогона
с одним сидом в детерминированном режиме обязаны совпасть **бит-в-бит**». Проверять
это по `sha256` файла нельзя: `torch.save` кладёт тензоры в zip-контейнер, и
совпадение байтов файла — достаточное, но не необходимое условие совпадения
**вычислений** (порядок записей, метаданные, выравнивание). Обратное тоже верно:
разные байты файла не отвечают на вопрос «разошлись ли числа».

Инструмент отвечает на вопрос по существу: сравнивает **значения** всех тензоров
чекпойнта (`model.state_dict()` и состояние оптимизатора) побитово — через
байтовое представление (`view(torch.uint8)`), потому что `a == b` для NaN даёт
`False` даже на идентичных битах.

Вердикт:

* ``IDENTICAL`` — все тензоры совпали побитово (форма, dtype и байты);
* ``DIFFERS`` — названы первый расходящийся тензор, число расходящихся элементов
  и максимальное абсолютное расхождение по float-тензорам;
* ``NOT-COMPARABLE`` — файл не читается или не является чекпойнтом контура
  (exit 2: «не смогли сравнить» — это не «совпало»).

Коды возврата: 0 — побитово идентичны; 1 — различаются; 2 — сравнение не состоялось.

Запуск::

    python3 tools/compare_checkpoints.py --a checkpoint_a.pt --b checkpoint_b.pt [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

EXIT_IDENTICAL, EXIT_DIFFERS, EXIT_NOT_COMPARABLE = 0, 1, 2


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def load_checkpoint(path: Path):
    """Читает чекпойнт. ``weights_only`` — если доступен, иначе обычный pickle.

    На стенде и локально стоят разные версии torch, и дефолт ``weights_only``
    между ними менялся; поэтому режим выбирается явно и попадает в отчёт — иначе
    «одинаковые» прогоны сравнивались бы разными читателями.
    """
    import torch

    try:
        obj = torch.load(path, map_location="cpu", weights_only=True)
        return obj, "weights_only=True"
    except Exception:
        obj = torch.load(path, map_location="cpu", weights_only=False)
        return obj, "weights_only=False"


def flatten(obj, prefix: str = "") -> dict:
    """Плоский словарь ``путь → значение`` (тензоры и скаляры)."""
    out: dict[str, object] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix or "<root>"] = obj
    return out


def compare_tensors(a, b) -> dict:
    """Побитовое сравнение двух тензоров (по байтам, а не по `==`)."""
    import torch

    if not (hasattr(a, "shape") and hasattr(b, "shape")):
        return {"kind": "scalar", "same": a == b and type(a) is type(b)}
    if tuple(a.shape) != tuple(b.shape):
        return {"kind": "tensor", "same": False, "reason": "shape",
                "a_shape": list(a.shape), "b_shape": list(b.shape)}
    if a.dtype != b.dtype:
        return {"kind": "tensor", "same": False, "reason": "dtype",
                "a_dtype": str(a.dtype), "b_dtype": str(b.dtype)}
    a_c = a.detach().contiguous()
    b_c = b.detach().contiguous()
    #: `reshape(-1)` перед байтовым представлением: у 0-мерного тензора (скаляр
    #: `step` оптимизатора, счётчики) `view(torch.uint8)` падает — а падение
    #: сравнения читалось бы как «не сравнимо», хотя сравнивать есть что.
    bytes_diff = int((a_c.reshape(-1).view(torch.uint8)
                      != b_c.reshape(-1).view(torch.uint8)).sum().item())
    same = bytes_diff == 0
    info = {"kind": "tensor", "same": same, "dtype": str(a.dtype),
            "numel": int(a_c.numel()), "bytes_differing": bytes_diff}
    if not same:
        if a_c.is_floating_point() or a_c.is_complex():
            diff = (a_c.float() - b_c.float()).abs()
            info["max_abs_diff"] = float(diff.max().item())
            #: NaN считается расхождением (`~=` вместо `!=`): NaN в одном чекпойнте
            #: и число в другом — это расхождение, а не «оба не равны».
            info["elements_differing"] = int((~(a_c == b_c)).sum().item())
        else:
            info["elements_differing"] = int((a_c != b_c).sum().item())
    return info


def compare(a_path: Path, b_path: Path) -> dict:
    """Сравнение двух чекпойнтов: сначала файлы, затем — значения тензоров."""
    report: dict = {
        "a": {"path": str(a_path), "size_bytes": a_path.stat().st_size,
              "sha256": sha256_file(a_path)},
        "b": {"path": str(b_path), "size_bytes": b_path.stat().st_size,
              "sha256": sha256_file(b_path)},
    }
    report["file_sha256_equal"] = report["a"]["sha256"] == report["b"]["sha256"]

    obj_a, reader = load_checkpoint(a_path)
    obj_b, reader_b = load_checkpoint(b_path)
    report["reader"] = {"a": reader, "b": reader_b}
    flat_a, flat_b = flatten(obj_a), flatten(obj_b)
    keys_a, keys_b = set(flat_a), set(flat_b)
    only_a = sorted(keys_a - keys_b)
    only_b = sorted(keys_b - keys_a)

    compared = same = 0
    differing: list[dict] = []
    elements_total = elements_differing = 0
    max_abs = None
    for key in sorted(keys_a & keys_b):
        compared += 1
        info = compare_tensors(flat_a[key], flat_b[key])
        if info["kind"] == "tensor":
            elements_total += info.get("numel", 0)
        if info["same"]:
            same += 1
            continue
        elements_differing += info.get("elements_differing", 0)
        if info.get("max_abs_diff") is not None:
            max_abs = info["max_abs_diff"] if max_abs is None else max(max_abs, info["max_abs_diff"])
        if len(differing) < 20:
            differing.append({"key": key, **info})

    identical = not only_a and not only_b and compared > 0 and same == compared
    report.update({
        "reader_used": reader,
        "tensors_compared": compared,
        "tensors_identical": same,
        "keys_only_in_a": only_a[:20],
        "keys_only_in_b": only_b[:20],
        "elements_compared": elements_total,
        "elements_differing": elements_differing,
        "max_abs_diff": max_abs,
        "first_differing": differing,
        "identical": identical,
        "verdict": "IDENTICAL" if identical else "DIFFERS",
        "identity_kind": ("побитовое совпадение всех тензоров"
                          if identical else "тензоры расходятся — см. first_differing"),
    })
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Побитовое сравнение двух чекпойнтов контура (ADR-009: «совпало бит-в-бит» "
                    "проверяется фактом, а не предположением).")
    ap.add_argument("--a", required=True, help="первый чекпойнт")
    ap.add_argument("--b", required=True, help="второй чекпойнт")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    a_path, b_path = Path(args.a), Path(args.b)
    missing = [str(p) for p in (a_path, b_path) if not p.is_file()]
    if missing:
        print(f"NOT-COMPARABLE: нет файла: {', '.join(missing)}", file=sys.stderr)
        if args.json:
            print(json.dumps({"verdict": "NOT-COMPARABLE", "missing": missing},
                             ensure_ascii=False))
        return EXIT_NOT_COMPARABLE
    try:
        report = compare(a_path, b_path)
    except Exception as e:
        print(f"NOT-COMPARABLE: {type(e).__name__}: {e}", file=sys.stderr)
        if args.json:
            print(json.dumps({"verdict": "NOT-COMPARABLE",
                              "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return EXIT_NOT_COMPARABLE

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"{report['verdict']}: {a_path.name} против {b_path.name}")
        print(f"  sha256 файлов {'совпали' if report['file_sha256_equal'] else 'различаются'}")
        print(f"  тензоров сравнено {report['tensors_compared']}, "
              f"идентичных {report['tensors_identical']}")
        if not report["identical"]:
            print(f"  различающихся элементов {report['elements_differing']} "
                  f"из {report['elements_compared']}"
                  + (f", max|Δ| = {report['max_abs_diff']}" if report["max_abs_diff"] is not None else ""))
            for d in report["first_differing"][:3]:
                print(f"    · {d['key']}: {d.get('reason') or 'значения'} "
                      f"(элементов {d.get('elements_differing')})")
    return EXIT_IDENTICAL if report["identical"] else EXIT_DIFFERS


if __name__ == "__main__":
    sys.exit(main())
