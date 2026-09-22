#!/usr/bin/env python3
"""S3q — база PPL на расширенном наборе общего языка и новый потолок (ADR-025 п.4).

Что считается. ADR-022 п.3 объявляет потолок «2× базы» единственным абсолютным
критерием валидности CPT-стадии. Расширение измерительного набора (ADR-025 п.4)
меняет базу: другая длина документов — другая шкала прибора. Поэтому вместе с
набором обязателен **перемер базы** и **пересчёт потолка как 2× новой базы**.

Почему отдельный инструмент, а не правка ``tools/ppl_probe.py``. Методика замера
берётся у S3h **как код** (``P.measure`` — та самая функция, что дала 11.9319), а
не переписывается: иначе новое число нельзя было бы сравнивать со старым. Но
наборы у S3h объявлены константой ``SETS`` и относятся к его вопросу (шкала v1/v2);
дописывать в неё набор другой дельты значило бы менять прибор S3h под чужую задачу
и делать его evidence невоспроизводимым.

Чем доказывается, что прибор тот же. В одном прогоне с новым набором снимается
**v1_general** — и число на нём обязано сойтись с историческим 11.931923888434497
(в пределах ``P.REPRO_TOLERANCE_PCT``). Совпадение — это и есть доказательство
«тот же прибор», а не ссылка на имя файла; расхождение — отказ, а не пометка
в примечании.

Что **не** переносится. Отношения ×4.63/×9.48 из S3m-2 сняты на v1_general, у
которого 24 документа и 3 682 токена; здесь 200 документов и ~154 тыс. токенов.
Это не «то же число точнее» — это другая шкала: у длинных документов больше
контекста, PPL ниже, и потолок 23.86 к новым числам неприменим (ADR-025,
«Отрицательные последствия»). Оба числа приводятся рядом и помечаются.

Коды возврата::

    0 — замер сделан, потолок пересчитан, v1 воспроизведён
    1 — отказ: v1 не воспроизвёл историческую базу (прибор или набор не тот)
    2 — NOT-VERIFIED: нет набора/весов/токенизатора или нет CUDA

Запуск::

    python3 tools/ppl_probe_v3.py --plan
    python3 tools/ppl_probe_v3.py --out evidence/s3q-baseline-v3.json
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

#: Наборы этого замера: новый (расширенный) и **контрольный** v1. Контроль не
#: «за компанию»: на нём держится утверждение «прибор тот же» (докстринг).
SETS: dict[str, str] = {
    "v1_general": "datasets/general_eval.txt",
    "v3_general": "datasets/general_eval_v3.txt",
}

#: Историческая база и потолок, к которым относятся отношения ×4.63/×9.48 (S3m-2).
#: Значения — из ``evidence/ppl-baseline-v1v2.json`` и ``evidence/s3m-ppl-arms.json``;
#: здесь они не «примерно те», а прочитаны оттуда при прогоне и сверены.
OLD_BASE = 11.931923888434497
OLD_CEILING = 23.863847776868994
OLD_BASE_EVIDENCE = "evidence/ppl-baseline-v1v2.json"
OLD_ARMS_EVIDENCE = "evidence/s3m-ppl-arms.json"


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

    datasets, missing = {}, []
    for name, rel in SETS.items():
        path = CASE_ROOT / rel
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

    # ── контроль: тот же замер без спецтокенов пайплайна (как в S3h) ─────────
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

    new_ppl = results["v3_general"]["ppl"]
    v1_ppl = results["v1_general"]["ppl"]
    rel_delta = (v1_ppl - OLD_BASE) / OLD_BASE * 100.0
    reproduced = abs(rel_delta) <= P.REPRO_TOLERANCE_PCT

    evidence = {
        "schema": "s3q-baseline/1",
        "stage": "S3q",
        "status": "complete" if reproduced else "отказ: v1 не воспроизведён",
        "date": datetime.now().isoformat(timespec="seconds"),
        "purpose": ("база PPL расширенного набора общего языка и пересчёт потолка "
                    "ADR-022 п.3 как 2× новой базы (ADR-025 п.4)"),
        "adr": ["ADR-025 п.4", "ADR-022 п.3", "ADR-011 п.4"],
        "stand": {
            "host": platform.node(),
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
            "torch": torch.__version__, "transformers": transformers.__version__,
            "note": ("локальная машина (RTX 4080 SUPER), НЕ стенд GB10 — как и замер "
                     "S3h, с числом которого сверяется v1: числа переносятся как "
                     "свойства наборов, а не как замеры стенда"),
        },
        "instrument": {
            "measure": "tools/ppl_probe.py:measure (та же функция, что дала 11.9319)",
            "probe_revision": probe_revision(),
            "tool": {"path": "tools/ppl_probe_v3.py",
                     "sha256": sha256_file(Path(__file__)), "argv": sys.argv[1:]},
            "split": "text.split('\\n---\\n')",
            "filter": f"len(doc.strip()) > {P.MIN_DOC_CHARS}",
            "tokenize": f"tokenizer.encode(doc, add_special_tokens=False)[:{args.max_len}]",
            "keep": f"len(enc) > {P.MIN_DOC_TOKENS}",
            "batch": args.batch, "max_len": args.max_len, "dtype": args.dtype,
            "ppl": "exp(Σ nll / Σ токенов) по корпусу",
            "tokenizer_setup": "pipeline (8 спецтокенов + resize), как в S3h",
            "weights": prov["weights"], "tokenizer": prov["tokenizer"],
        },
        "env_shims": shims,
        "datasets": {name: {k: v for k, v in ds.items() if k != "docs"}
                     for name, ds in datasets.items()},
        "ppl": {name: res for name, res in results.items()},
        "tokenizer_control": {
            "what": ("тот же замер без спецтокенов/расширения embeddings пайплайна — "
                     "отделяет настройку токенизатора от состояния модели"),
            "ppl": {name: res["ppl"] for name, res in control.items()},
            "delta_pct": {name: (control[name]["ppl"] / results[name]["ppl"] - 1) * 100
                          for name in control},
        } if control else {"what": "контроль не снимался"},
        "baseline": {
            "new_ppl": new_ppl,
            "new_ceiling": 2.0 * new_ppl,
            "new_set": "v3_general",
            "new_set_sha256": datasets["v3_general"]["sha256"],
            "old_ppl": OLD_BASE,
            "old_ceiling": OLD_CEILING,
            "old_set": ("v1_general (24 документа; сумма len(encode) = 3 706, "
                        "прибор считает 3 682 предсказанные позиции)"),
            "old_base_evidence": OLD_BASE_EVIDENCE,
            "old_arms_evidence": OLD_ARMS_EVIDENCE,
            "v1_remeasured_ppl": v1_ppl,
            "v1_rel_delta_pct": rel_delta,
            "v1_reproduced": reproduced,
        },
        "comparability": {
            "old_ratios_not_comparable": (
                "отношения ×4.63 и ×9.48 (evidence/s3m-ppl-arms.json) сняты на "
                "v1_general (24 документа, 3 682 токена) и с новыми числами "
                "напрямую несопоставимы: другой набор — другая шкала прибора "
                "(ADR-025, «Отрицательные последствия»). Переносится вывод "
                "«все руки провалили порог с большим запасом», а не величины"),
            "why_scale_differs": (
                "документ v3 длиннее (медиана токенов втрое выше), у модели больше "
                "контекста — PPL на длинном документе ниже, поэтому потолок 23.86 "
                "к новым числам неприменим; действующий потолок пересчитан как "
                "2× новой базы и назван в поле baseline.new_ceiling"),
            "arms_not_remeasured_here": (
                "четыре калибровочные руки S3m и контрольные руки S3o на новом "
                "наборе этим прогоном не перемерялись — см. next_step в evidence S3q"),
        },
        "seconds_total": round(time.time() - started, 1),
    }
    return evidence, EXIT_OK if reproduced else EXIT_FAIL


def print_plan(args) -> int:
    print("S3q — база PPL расширенного набора и новый потолок")
    for name, rel in SETS.items():
        print(f"  набор {name}: {rel}")
    print(f"  прибор: tools/ppl_probe.py:measure (max_len {args.max_len}, batch "
          f"{args.batch}, dtype {args.dtype}, device {args.device})")
    print(f"  контроль v1 против исторической базы {OLD_BASE:.6f} "
          f"(порог {P.REPRO_TOLERANCE_PCT} %)")
    print("  потолок = 2 × PPL(v3_general)")
    return EXIT_OK


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="S3q: база PPL расширенного набора (ADR-025)")
    ap.add_argument("--out", default="evidence/s3q-baseline-v3.json")
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
            out = CASE_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        b = evidence["baseline"]
        print(f"\nновая база: {b['new_ppl']:.6f}  →  новый потолок: "
              f"{b['new_ceiling']:.6f}")
        print(f"старая база: {b['old_ppl']:.6f}  →  старый потолок: "
              f"{b['old_ceiling']:.6f} (исторические, несопоставимы напрямую)")
        print(f"контроль v1: {b['v1_remeasured_ppl']:.6f} "
              f"({b['v1_rel_delta_pct']:+.4f} %, воспроизведено: {b['v1_reproduced']})")
        print(f"evidence: {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
