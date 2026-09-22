#!/usr/bin/env python3
"""S3k — PPL-проба чекпойнтов диагностики flex-маски (исполняется **в контейнере стенда**).

Зачем отдельная проба. Диагностика ADR-022 п.1 сравнивает два CPT-прогона, и
основание вердикта — не только траектория loss, но и **деградация общего языка**.
GEN-EVAL пайплайна пишет PPL только на наборах v1 (`general_eval.txt`,
`domain_eval.txt`) и только на состояниях, попавших на шаг, кратный
`CPT_GEN_EVAL_EVERY`. Этого мало для трёх вопросов дельты:

* наборы **v2** (ADR-018, по 200 документов) в прогоне не измеряются вовсе;
* «база» в GEN-EVAL берётся не с модели, а с первого успешного вызова на шаге 50
  (дефект ADR-015 п.4) — то есть её в истории прогона нет;
* чекпойнт шага 200 в прогоне не сохраняется (`step % 200 == 0` не выполняется при
  `max_steps=200` — цикл заканчивается на 199), поэтому проба идёт по
  `checkpoint_final.pt` и по номерным чекпойнтам, если прогон их оставил.

Методика не переписывается. Проба **импортирует** `_ppl_eval`,
`SPECIAL_TOKENS` и `_load_ckpt_with_resize` из того самого файла пайплайна,
которым шёл прогон (`--pipeline`), и вызывает их как есть. Это снимает главный
риск копии: «своя реализация, разошедшаяся с контуром на первой правке».
Условия замера повторяют контур: токенизатор расширен восемью спецтокенами
(AD-3), `resize_token_embeddings` сделан до загрузки весов, dtype bf16, cuda —
то есть меряется ровно то состояние, которое видел GEN-EVAL внутри прогона.

Вторая агрегация. Кроме PPL корпуса (`exp(Σnll/Σтокенов)` — число, сравнимое с
GEN-EVAL) считается PPL **по документам** (mean/median и разброс). Это второй
проход тем же кодом с разделением сумм по строкам батча; его PPL корпуса
обязан совпасть с `_ppl_eval` до 1e-6, и расхождение — отказ пробы, а не «шум».
Так одна и та же величина получается двумя путями, и путь с документной
статистикой не является вторым (чуть другим) замером.

Коды возврата::

    0 — все состояния измерены, отчёт записан
    1 — расхождение двух путей счёта выше допуска, либо состояние не загрузилось
    2 — NOT-VERIFIED: нет входа (набор/веса/чекпойнт/пайплайн) или нет CUDA

Запуск (внутри контейнера; снаружи это делает `tools/run_flex_check.py --ppl`)::

    python3 flex_ppl_probe.py --plan
    python3 flex_ppl_probe.py \
        --pipeline /workspace/experiments/<run>/laguna_pipeline_flexcheck.py \
        --state base=base --state a200=ckpt:/workspace/experiments/<a>/checkpoints/checkpoint_final.pt \
        --set v1_general=/workspace/shared/datasets/general_eval.txt \
        --set v1_domain=/workspace/shared/datasets/domain_eval.txt \
        --set v2_general=/workspace/shared/datasets/general_eval_v2.txt \
        --set v2_domain=/workspace/shared/datasets/domain_eval_v2.txt \
        --out /workspace/experiments/<run>/ppl/ppl.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Допуск согласия двух путей счёта одной величины. Оба пути считают по одним и
#: тем же логитам, поэтому расхождение может быть только ошибкой агрегации, а не
#: численным шумом схемы; 1e-6 — с запасом на порядок суммирования float32.
CORPUS_AGREEMENT_TOL = 1e-6

#: Состояния диагностики: имя → как его собрать. `base` — модель **до первого
#: шага стадии**: те же веса, что у CPT, но с расширенным по AD-3 токенизатором и
#: resize эмбеддингов (именно в этом состоянии стадия начинает работу, и именно
#: его GEN-EVAL не измерял никогда — ADR-015 п.4).
STATE_BASE = "base"
STATE_HF = "base_hf"  # вакуумная точка: модель без расширения токенизатора


def load_pipeline_module(path: Path):
    """Импорт файла пайплайна по пути (имя модуля может быть любым).

    Модуль пайплайна на импорте только defines: тяжёлые импорты (transformers,
    vllm) живут внутри `main()`. Поэтому импорт дёшев и не запускает стадий.
    """
    spec = importlib.util.spec_from_file_location("laguna_pipeline_for_probe", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить модуль пайплайна: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doc_tokenized(tokenizer, path: Path, max_len: int) -> list[list[int]]:
    """Разбор набора — дословно как в `_ppl_eval` пайплайна.

    Продублирован здесь **намеренно**: нужны не только суммы, но и границы
    документов, а `_ppl_eval` их наружу не отдаёт. Совпадение результата с
    `_ppl_eval` проверяется на каждом наборе (см. `measure_set`), поэтому копия
    не является второй правдой — она верифицируется против первой.
    """
    docs = [d.strip() for d in path.read_text().split("\n---\n") if len(d.strip()) > 50]
    ids = []
    for d in docs:
        enc = tokenizer.encode(d, add_special_tokens=False)[:max_len]
        if len(enc) > 8:
            ids.append(enc)
    return ids


def measure_set_perdoc(model, tokenizer, path: Path, max_len: int, batch: int,
                       torch) -> dict:
    """PPL корпуса + документная статистика **одним** проходом (копия `_ppl_eval`).

    Единственное отличие от `_ppl_eval` — суммы nll и счётчики токенов
    разделяются по строкам батча. Логиты, маска и цель те же, поэтому PPL
    корпуса обязан совпасть с `_ppl_eval` до допуска (проверяется вызывающим).
    """
    import torch.nn.functional as F  # noqa: N812 — как в пайплайне

    ids = _doc_tokenized(tokenizer, path, max_len)
    total_nll, total_tok = 0.0, 0
    per_doc: list[float] = []
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else 0
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        ml = max(len(c) for c in chunk)
        input_ids = torch.full((len(chunk), ml), pad_id, dtype=torch.long, device="cuda")
        attn = torch.zeros((len(chunk), ml), dtype=torch.long, device="cuda")
        for j, c in enumerate(chunk):
            input_ids[j, :len(c)] = torch.tensor(c, dtype=torch.long, device="cuda")
            attn[j, :len(c)] = 1
        logits = model(input_ids=input_ids, attention_mask=attn).logits
        sl = F.log_softmax(logits[:, :-1].float(), dim=-1)
        tgt = input_ids[:, 1:]
        m = attn[:, 1:].bool()
        nll = -sl.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
        for j in range(len(chunk)):
            mj = m[j]
            n_j = float(nll[j][mj].sum().item())
            t_j = int(mj.sum().item())
            total_nll += n_j
            total_tok += t_j
            if t_j > 0:
                per_doc.append(math.exp(n_j / t_j))
        del logits, sl, nll
    torch.cuda.empty_cache()
    return {
        "docs": len(ids),
        "tokens": total_tok,
        "ppl_corpus": math.exp(total_nll / max(total_tok, 1)),
        "ppl_doc_mean": (sum(per_doc) / len(per_doc)) if per_doc else None,
        "ppl_doc_median": statistics.median(per_doc) if per_doc else None,
        "ppl_doc_min": min(per_doc) if per_doc else None,
        "ppl_doc_max": max(per_doc) if per_doc else None,
    }


def build_model(model_id: str, device: str, dtype, extend: bool, tokenizer, torch):
    """Модель в том же состоянии, в каком её видит стадия CPT.

    `extend=False` — вакуумная точка (веса и вокаб как в HF). `extend=True` —
    контур: `add_special_tokens` (AD-3) затем `resize_token_embeddings`, то есть
    то же состояние, в котором стадия начинает работу.

    Оговорка про 8 новых строк: их значения зависят от RNG и **не** воспроизводят
    побитово состояние стадии (проба не сеет torch тем же сидом в той же точке).
    Влияние ограничено восемью строками из ~151.7 тысячи и меряется отдельной
    точкой `base_hf` (тот же вес без расширения): если обе точки расходятся в
    пределах, оговорка не стоит ничего.
    """
    from transformers import AutoModelForCausalLM

    model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=dtype,
                                                 trust_remote_code=True)
    if extend:
        model.resize_token_embeddings(len(tokenizer))
    model.eval()
    for p in model.parameters():
        p.requires_grad = False
    model.to(device)
    return model


def run_probe(args) -> dict:
    """Порядок проверок — от дешёвого и обязательного к дорогому: сначала входы
    (пайплайн, наборы, чекпойнты), потом окружение. Иначе отсутствующий набор
    маскируется упавшим импортом transformers, а «NOT-VERIFIED по существу»
    выглядит как «сломанный стенд»."""
    started = time.time()
    pipe_path = Path(args.pipeline)
    if not pipe_path.is_file():
        print(f"NOT-VERIFIED: нет файла пайплайна {pipe_path}", file=sys.stderr)
        return {"status": "not_verified", "why": f"нет файла пайплайна: {pipe_path}"}
    try:
        mod = load_pipeline_module(pipe_path)
        _ppl_eval = mod._ppl_eval
        SPECIAL_TOKENS = mod.SPECIAL_TOKENS
        _load_ckpt_with_resize = mod._load_ckpt_with_resize
    except Exception as e:  # noqa: BLE001 — источник методики недоступен, и это NOT-VERIFIED
        print(f"NOT-VERIFIED: пайплайн {pipe_path} не импортируется ({type(e).__name__}: {e})",
              file=sys.stderr)
        return {"status": "not_verified",
                "why": f"пайплайн не импортируется ({type(e).__name__}: {e})"}
    pipeline_sha256 = _sha256_file(pipe_path)

    sets: dict[str, Path] = {}
    for spec in args.set:
        name, _, raw = spec.partition("=")
        p = Path(raw)
        if not p.is_file():
            print(f"NOT-VERIFIED: нет набора {name}: {p}", file=sys.stderr)
            return {"status": "not_verified", "why": f"нет набора {name}: {p}"}
        sets[name] = p

    states: list[tuple[str, str, Path | None]] = []
    for spec in args.state:
        name, _, raw = spec.partition("=")
        if raw in (STATE_BASE, STATE_HF):
            states.append((name, raw, None))
            continue
        if raw.startswith("ckpt:"):
            ck = Path(raw[5:])
            if not ck.is_file():
                print(f"NOT-VERIFIED: нет чекпойнта {name}: {ck}", file=sys.stderr)
                return {"status": "not_verified", "why": f"нет чекпойнта {name}: {ck}"}
            states.append((name, "ckpt", ck))
            continue
        print(f"неизвестное состояние {name}: {raw!r}", file=sys.stderr)
        return {"status": "not_verified", "why": f"неизвестное состояние {name}: {raw!r}"}

    try:
        import torch
        from transformers import AutoTokenizer
    except Exception as e:  # noqa: BLE001 — окружение пробы непригодно
        print(f"NOT-VERIFIED: окружение пробы не собрано ({type(e).__name__}: {e})", file=sys.stderr)
        return {"status": "not_verified",
                "why": f"окружение пробы не собрано ({type(e).__name__}: {e})"}
    if not torch.cuda.is_available():
        print("NOT-VERIFIED: нет CUDA — проба PPL не может быть снята", file=sys.stderr)
        return {"status": "not_verified", "why": "нет CUDA"}

    dtype = {"bfloat16": torch.bfloat16, "float32": torch.float32}[args.dtype]
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    tokenizer.add_special_tokens({"additional_special_tokens": SPECIAL_TOKENS})
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    report: dict = {
        "probe": "flex_ppl_probe",
        "method": ("_ppl_eval пайплайна, импортированный из файла прогона; "
                   "вторая агрегация — тем же кодом с разделением сумм по документам"),
        "pipeline": str(pipe_path),
        "pipeline_sha256": pipeline_sha256,
        "model_id": args.model,
        "dtype": args.dtype,
        "device": torch.cuda.get_device_name(0),
        "max_len": args.max_len,
        "batch": args.batch,
        "special_tokens": SPECIAL_TOKENS,
        "sets": {k: {"path": str(v), "sha256": _sha256_file(v)} for k, v in sets.items()},
        "states": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    ok = True
    for name, kind, ckpt in states:
        extend = kind != STATE_HF
        t_state = time.time()
        try:
            model = build_model(args.model, args.device, dtype, extend, tokenizer, torch)
            ckpt_note = None
            if ckpt is not None:
                blob = torch.load(ckpt, map_location=args.device)
                sd = blob["model"] if isinstance(blob, dict) and "model" in blob else blob
                resized = _load_ckpt_with_resize(model, sd, f"[{name}] ")
                ckpt_note = {"path": str(ckpt), "sha256": _sha256_file(ckpt),
                             "resized_on_load": bool(resized)}
                model.eval()
                for p in model.parameters():
                    p.requires_grad = False
        except Exception as e:  # noqa: BLE001 — отчёт важнее падения
            print(f"СОСТОЯНИЕ {name}: не загрузилось ({type(e).__name__}: {e})", file=sys.stderr)
            report["states"][name] = {"error": f"{type(e).__name__}: {e}"}
            ok = False
            continue

        entry: dict = {"kind": kind, "tokenizer_extended": extend,
                       "checkpoint": ckpt_note, "sets": {}}
        with torch.no_grad():
            for sname, spath in sets.items():
                t0 = time.time()
                ppl_ref = _ppl_eval(model, tokenizer, str(spath),
                                    max_len=args.max_len, batch=args.batch)
                per_doc = measure_set_perdoc(model, tokenizer, spath,
                                             args.max_len, args.batch, torch)
                delta = abs(ppl_ref - per_doc["ppl_corpus"])
                rel = delta / max(ppl_ref, 1e-9)
                agree = rel <= CORPUS_AGREEMENT_TOL
                ok = ok and agree
                entry["sets"][sname] = {
                    **per_doc,
                    "ppl_corpus_ppl_eval": ppl_ref,
                    "agreement_rel": rel,
                    "agreement_ok": agree,
                    "seconds": round(time.time() - t0, 2),
                }
                print(f"[{name}] {sname}: ppl_corpus={per_doc['ppl_corpus']:.4f} "
                      f"(ppl_eval={ppl_ref:.4f}, |Δ|rel={rel:.2e}, "
                      f"docs={per_doc['docs']}, tok={per_doc['tokens']})", flush=True)
        entry["seconds"] = round(time.time() - t_state, 2)
        report["states"][name] = entry
        # Чекпойнт ~3 ГБ на состояние (веса + моменты оптимизатора), и он жив
        # ровно до конца этого состояния: без `del` следующая итерация держала бы
        # и прошлый чекпойнт, и новый — на GB10 unified-память общая.
        del model
        if ckpt is not None:
            del blob, sd
        torch.cuda.empty_cache()

    report["seconds"] = round(time.time() - started, 2)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["status"] = "ok" if ok else "disagreement"
    return report


def _sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def render_plan(args) -> str:
    lines = [
        "== проба PPL чекпойнтов диагностики flex-маски ==",
        f"пайплайн (источник методики): {args.pipeline}",
        f"модель: {args.model} ({args.dtype}, {args.device})",
        f"наборы: {', '.join(args.set) if args.set else '(не заданы)'}",
        f"состояния: {', '.join(args.state) if args.state else '(не заданы)'}",
        f"выход: {args.out}",
        "",
        "Методика: _ppl_eval пайплайна импортируется из файла прогона и вызывается",
        "как есть; вторая агрегация (по документам) сверяется с ней до 1e-6.",
        "Проба только читает данные и чекпойнты; веса не копируются (AD-4).",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="PPL-проба чекпойнтов диагностики flex-маски (S3k)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pipeline", default=None,
                    help="файл пайплайна — источник _ppl_eval/SPECIAL_TOKENS (обязателен для замера)")
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B", help="базовая модель (ADR-002)")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=PATH",
                    help="набор GEN-EVAL (повторяемый)")
    ap.add_argument("--state", action="append", default=[], metavar="NAME=base|base_hf|ckpt:PATH",
                    help="измеряемое состояние (повторяемое)")
    ap.add_argument("--out", default=None, help="файл отчёта (json)")
    ap.add_argument("--max-len", type=int, default=1024, help="потолок длины документа (как в _ppl_eval)")
    ap.add_argument("--batch", type=int, default=4, help="батч (как в _ppl_eval)")
    ap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float32"])
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--plan", action="store_true", help="напечатать план и выйти")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.plan:
        print(render_plan(args))
        return EXIT_OK
    if not args.pipeline:
        print("--pipeline обязателен для замера (иначе методика не из контура)", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if not args.set or not args.state:
        print("нужны хотя бы один --set и один --state", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    report = run_probe(args)
    if report.get("status") == "not_verified":
        print(f"NOT-VERIFIED: {report.get('why')}", file=sys.stderr)
        if args.out:
            _write_json(Path(args.out), report)
        return EXIT_NOT_VERIFIED
    report["argv"] = sys.argv[1:]
    if args.out:
        _write_json(Path(args.out), report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"проба завершена: status={report['status']}, {report['seconds']} с"
              f"{'' if args.out else ' (--out не задан: отчёт только в stdout)'}")
    return EXIT_OK if report["status"] == "ok" else EXIT_FAIL


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    sys.exit(main())
