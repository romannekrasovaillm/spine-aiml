#!/usr/bin/env python3
"""S3t — база и потолок компоненты K2 в одном прогоне с тождеством прибора.

Что здесь считается. ADR-027 п.2: потолок 2× считается **от собственной базы
каждой компоненты**. Для K2 это значит: набор ``general_eval_k2.txt`` обязан иметь
свою базу, снятую тем же прибором, что и база K1, — иначе отношения двух компонент
несопоставимы, и конъюнкция (ADR-027 п.1) превращается в сравнение разных шкал.

Чем доказывается, что прибор тот же. В **одном прогоне** с K2 снимаются два
набора с известными историческими числами:

* ``v1_general`` — обязан дать 11.931923888434497 (эталон S3h, ``ppl-baseline-v1v2.json``);
* ``v3_general`` — обязан дать 7.50468637420902 (эталон S3q, ``s3q-baseline-v3.json``).

Совпадение обоих — это и есть доказательство «тот же прибор», а не ссылка на имя
файла: одно совпадение объяснялось бы совпадением набора, два — только тем, что
измеряет та же функция. Расхождение — отказ (код 1), а не пометка в примечании.

Почему отдельный инструмент, а не правка ``tools/ppl_probe_v3.py``. Ревизия
прибора S3q зафиксирована в её evidence (``instrument.tool.sha256``); правка того
же файла сделала бы записанное там описание не соответствующим файлу. Методика
берётся **кодом** (``P.measure`` — та же функция, что дала оба эталона), а список
наборов у этого прогона свой.

Что не переносится. Отношения рук, снятые на K1, к K2 не переносятся: другой
набор — другая база и другой потолок. Компоненты не усредняются (ADR-027 п.2).

Коды возврата::

    0 — замер сделан, оба эталона воспроизведены, потолок K2 пересчитан
    1 — отказ: v1 или v3 не воспроизвели свои исторические базы
    2 — NOT-VERIFIED: нет набора/весов/токенизатора или нет CUDA

Запуск::

    python3 tools/ppl_probe_k2.py --plan
    python3 tools/ppl_probe_k2.py --out evidence/s3t-baseline-k2.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ppl_probe as P  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Наборы этого замера: компонента K2 и **два контрольных** набора, на которых
#: держится утверждение «прибор тот же» (докстринг). Порядок — от опорных к новому:
#: контроль обязан быть снят в том же прогоне, а не взят из чужого отчёта.
SETS: dict[str, str] = {
    "v1_general": "datasets/general_eval.txt",
    "v3_general": "datasets/general_eval_v3.txt",
    "k2_general": "datasets/general_eval_k2.txt",
}

#: Исторические эталоны, к которым относятся контрольные наборы. Значения
#: **читаются** из файлов evidence при прогоне и сверяются, а не вписаны сюда:
#: правка эталона должна ломать пробу, а не незаметно совпадать с ней.
REFERENCES = {
    "v1_general": {"ppl": 11.931923888434497, "evidence": "evidence/ppl-baseline-v1v2.json",
                   "stage": "S3h"},
    "v3_general": {"ppl": 7.50468637420902, "evidence": "evidence/s3q-baseline-v3.json",
                   "stage": "S3q"},
}

#: Компонента, для которой этот прогон снимает базу и потолок.
COMPONENT = "k2_general"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def probe_revision() -> dict:
    """sha256 прибора S3h — методика заимствована кодом, значит её ревизия в отчёте."""
    return {"path": "tools/ppl_probe.py", "sha256": sha256_file(Path(P.__file__))}


def read_dataset(path: Path) -> dict:
    """Набор читается **той же** функцией, что у S3h (``P.read_dataset``)."""
    return P.read_dataset(path)


def check_references(root: Path) -> tuple[dict, list[str]]:
    """Прочитать эталоны из файлов evidence и сверить с объявленными числами.

    Если файл эталона изменён (или число в нём не то, что объявлено здесь), проба
    обязана упасть до замера: иначе «сверка с историей» сверялась бы с копией
    числа в самом инструменте.
    """
    out: dict[str, dict] = {}
    problems: list[str] = []
    for name, ref in REFERENCES.items():
        path = root / ref["evidence"]
        if not path.is_file():
            problems.append(f"{name}: нет файла эталона {ref['evidence']}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        node = data.get("ppl", {})
        got = node.get(name, {}).get("ppl") if isinstance(node.get(name), dict) else None
        if got is None:
            problems.append(f"{name}: в {ref['evidence']} нет поля ppl.{name}")
            continue
        if abs(float(got) - ref["ppl"]) > 1e-9:
            problems.append(f"{name}: эталон в файле {got!r} ≠ объявленного {ref['ppl']!r}")
            continue
        out[name] = {**ref, "ppl_in_file": float(got),
                     "evidence_sha256": sha256_file(path)}
    return out, problems


def run(args) -> tuple[dict, int]:
    shims: list[str] = []
    shim = P.repair_broken_pyopenssl()
    if shim:
        shims.append(shim)
    try:
        transformers, tshim = P.import_transformers()
    except ImportError as exc:
        print(f"NOT-VERIFIED: transformers не импортируется: {exc}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED
    if tshim:
        shims.append(tshim)
    import torch

    if args.device.startswith("cuda") and not torch.cuda.is_available():
        print("NOT-VERIFIED: CUDA недоступна — замер PPL нечем сделать", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    root = Path(args.case_root).resolve() if args.case_root else CASE_ROOT
    refs, problems = check_references(root)
    if problems:
        print("NOT-VERIFIED: эталоны не прочитаны: " + "; ".join(problems), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    datasets, missing = {}, []
    for name, rel in SETS.items():
        path = root / rel
        if not path.is_file():
            missing.append(f"{name}: {path} не найден")
            continue
        datasets[name] = read_dataset(path)
    if missing:
        print("NOT-VERIFIED: " + "; ".join(missing), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    model, tokenizer, prov, errs = P.load_model("base", args.dtype, args.device,
                                                pipeline_tokenizer=True)
    if model is None:
        print("NOT-VERIFIED: " + "; ".join(errs), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    results: dict = {}
    started = time.time()
    for name, ds in datasets.items():
        t0 = time.time()
        with torch.no_grad():
            res = P.measure(model, tokenizer, ds["docs"], args.max_len, args.batch,
                            args.device, cross_check=True)
        res.update({"seconds": round(time.time() - t0, 3), "max_len": args.max_len,
                    "batch": args.batch, "device": args.device, "dtype": args.dtype})
        results[name] = res
        print(f"  {name:11s} ppl={res['ppl']:.6f} docs={res['docs_counted']} "
              f"tok={res['tokens']} median={res['doc_ppl']['median']:.3f} "
              f"({res['seconds']:.1f} с)", flush=True)

    # ── контроль: тот же замер без спецтокенов пайплайна (как в S3q) ─────────
    control: dict = {}
    if args.tokenizer_control:
        del model
        if args.device.startswith("cuda"):
            torch.cuda.empty_cache()
        model2, tok2, prov2, errs2 = P.load_model("base", args.dtype, args.device,
                                                  pipeline_tokenizer=False)
        if model2 is None:
            print("NOT-VERIFIED: " + "; ".join(errs2), file=sys.stderr)
            return {}, EXIT_NOT_VERIFIED
        for name, ds in datasets.items():
            with torch.no_grad():
                control[name] = P.measure(model2, tok2, ds["docs"], args.max_len,
                                          args.batch, args.device)
            print(f"  [контроль без спецтокенов] {name:11s} "
                  f"ppl={control[name]['ppl']:.6f}", flush=True)
        del model2
        if args.device.startswith("cuda"):
            torch.cuda.empty_cache()

    # ── тождество прибора: оба эталона обязаны сойтись в этом же прогоне ─────
    identity: dict[str, dict] = {}
    worst = 0.0
    for name, ref in refs.items():
        got = results[name]["ppl"]
        rel = (got - ref["ppl"]) / ref["ppl"] * 100.0
        worst = max(worst, abs(rel))
        identity[name] = {
            "ppl_measured": got, "ppl_historical": ref["ppl"],
            "rel_delta_pct": rel, "tolerance_pct": P.REPRO_TOLERANCE_PCT,
            "within_tolerance": abs(rel) <= P.REPRO_TOLERANCE_PCT,
            "historical_evidence": ref["evidence"],
            "historical_stage": ref["stage"],
            "historical_evidence_sha256": ref["evidence_sha256"],
        }
    reproduced = all(v["within_tolerance"] for v in identity.values())

    k2 = results[COMPONENT]
    evidence = {
        "schema": "s3t-baseline-k2/1",
        "stage": "S3t",
        "status": "complete" if reproduced else "отказ: эталон прибора не воспроизведён",
        "date": datetime.now().isoformat(timespec="seconds"),
        "purpose": ("база и потолок компоненты K2 меры общего языка (ADR-027 п.2) "
                    "с доказательством тождества прибора числом в том же прогоне"),
        "adr": ["ADR-027 п.1", "ADR-027 п.2", "ADR-022 п.3", "AD-11"],
        "stand": {
            "host": platform.node(),
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
            "torch": torch.__version__, "transformers": transformers.__version__,
            "note": ("локальная машина (RTX 4080 SUPER), НЕ стенд GB10: числа "
                     "переносятся как свойства наборов (PPL), а не как замеры "
                     "производительности стенда"),
        },
        "instrument": {
            "measure": ("tools/ppl_probe.py:measure — та же функция, что дала и "
                        "11.9319 (S3h), и 7.5047 (S3q)"),
            "probe_revision": probe_revision(),
            "tool": {"path": "tools/ppl_probe_k2.py",
                     "sha256": sha256_file(Path(__file__)), "argv": sys.argv[1:]},
            "split": "text.split('\\n---\\n')",
            "filter": f"len(doc.strip()) > {P.MIN_DOC_CHARS}",
            "tokenize": f"tokenizer.encode(doc, add_special_tokens=False)[:{args.max_len}]",
            "keep": f"len(enc) > {P.MIN_DOC_TOKENS}",
            "batch": args.batch, "max_len": args.max_len, "dtype": args.dtype,
            "ppl": "exp(Σ nll / Σ токенов) по корпусу",
            "tokenizer_setup": "pipeline (8 спецтокенов + resize), как в S3h/S3q",
            "weights": prov["weights"], "tokenizer": prov["tokenizer"],
        },
        "env_shims": shims,
        "datasets": {name: {k: v for k, v in ds.items() if k != "docs"}
                     for name, ds in datasets.items()},
        "ppl": results,
        "tokenizer_control": {
            "what": ("тот же замер без спецтокенов/расширения embeddings пайплайна — "
                     "отделяет настройку токенизатора от состояния модели"),
            "ppl": {name: res["ppl"] for name, res in control.items()},
            "delta_pct": {name: (control[name]["ppl"] / results[name]["ppl"] - 1) * 100
                          for name in control},
        } if control else {"what": "контроль не снимался"},
        "instrument_identity": {
            "what": ("два набора с историческими числами сняты в том же прогоне, что "
                     "и K2: одно совпадение объяснялось бы совпадением набора, два — "
                     "только тем, что измеряет та же функция"),
            "checks": identity,
            "worst_rel_delta_pct": worst,
            "tolerance_pct": P.REPRO_TOLERANCE_PCT,
            "reproduced": reproduced,
        },
        "component": {
            "role": "K2",
            "set": COMPONENT,
            "set_sha256": datasets[COMPONENT]["sha256"],
            "ppl": k2["ppl"],
            "ceiling": 2.0 * k2["ppl"],
            "docs_counted": k2["docs_counted"],
            "tokens": k2["tokens"],
            "doc_ppl": k2["doc_ppl"],
            "not_to_be_averaged_with": (
                "числа K1 и потолки K1/K2 не усредняются (ADR-027 п.2): компоненты "
                "отвечают на разные вопросы, и вердикт стадии — их конъюнкция"),
        },
        "peer_components": {
            "K1": {"set": "v3_general",
                   "set_sha256": datasets["v3_general"]["sha256"],
                   "ppl": results["v3_general"]["ppl"],
                   "ceiling": 2.0 * results["v3_general"]["ppl"],
                   "note": "снято в этом же прогоне тем же прибором — для конъюнкции"},
            "historical": {"set": "v1_general (исторический опорный, не решающий)",
                           "set_sha256": datasets["v1_general"]["sha256"],
                           "ppl": results["v1_general"]["ppl"],
                           "ceiling": 2.0 * results["v1_general"]["ppl"]},
        },
        "seconds_total": round(time.time() - started, 1),
    }
    return evidence, EXIT_OK if reproduced else EXIT_FAIL


def print_plan(args) -> int:
    print("S3t — база и потолок компоненты K2 (вне обучающего распределения)")
    for name, rel in SETS.items():
        print(f"  набор {name}: {rel}")
    print(f"  прибор: tools/ppl_probe.py:measure (max_len {args.max_len}, batch "
          f"{args.batch}, dtype {args.dtype}, device {args.device})")
    for name, ref in REFERENCES.items():
        print(f"  эталон {name}: {ref['ppl']:.6f} из {ref['evidence']} ({ref['stage']}), "
              f"порог {P.REPRO_TOLERANCE_PCT} %")
    print(f"  потолок K2 = 2 × PPL({COMPONENT})")
    return EXIT_OK


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="S3t: база и потолок компоненты K2 (ADR-027)")
    ap.add_argument("--out", default="evidence/s3t-baseline-k2.json")
    ap.add_argument("--case-root", default=None,
                    help="корень кейса (по умолчанию — каталог над tools/)")
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--max-len", type=int, default=P.DEFAULT_MAX_LEN)
    ap.add_argument("--batch", type=int, default=P.DEFAULT_BATCH)
    ap.add_argument("--tokenizer-control", action="store_true", default=True,
                    help="снять контроль без спецтокенов пайплайна (по умолчанию да)")
    ap.add_argument("--no-tokenizer-control", dest="tokenizer_control",
                    action="store_false")
    ap.add_argument("--plan", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.plan:
        return print_plan(args)
    evidence, rc = run(args)
    if evidence:
        out = Path(args.out)
        if not out.is_absolute():
            out = (Path(args.case_root).resolve() if args.case_root else CASE_ROOT) / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        c = evidence["component"]
        print(f"\nкомпонента K2: база {c['ppl']:.6f}  →  потолок {c['ceiling']:.6f}")
        for name, chk in evidence["instrument_identity"]["checks"].items():
            print(f"  тождество прибора, {name}: {chk['ppl_measured']:.6f} против "
                  f"{chk['ppl_historical']:.6f} ({chk['rel_delta_pct']:+.4f} %)")
        print(f"evidence: {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
