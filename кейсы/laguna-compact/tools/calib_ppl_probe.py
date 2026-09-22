#!/usr/bin/env python3
"""S3m — PPL чекпойнтов калибровочных прогонов на наборах v1 и v2 (локальная машина).

Зачем отдельная проба. Основание вердикта по ADR-022 п.2 — деградация **общего**
языка на четырёх конфигурациях сетки, и её надо снять с состояний, которых в
прогоне нет:

* GEN-EVAL пайплайна пишет PPL только на наборах v1 и только на шагах, кратных
  `CPT_GEN_EVAL_EVERY`; наборы v2 (ADR-018) не измеряются вовсе;
* «база» в GEN-EVAL — не модель, а первый успешный вызов на шаге 50 (дефект
  подтверждён в S3k) — то есть опорного числа в истории прогона нет;
* чекпойнты шага 500/1000/1500/2000 сохраняются копией пайплайна прогона
  (`CALIB_CKPT_EVERY`), но GEN-EVAL их не посещает.

Методика не переписывается, а **импортируется**: замер идёт
`tools/ppl_probe.py`-функцией `measure` (та же `_ppl_eval`-методика S3h: разбор
`\\n---\\n`, `len(doc.strip()) > 50`, `encode(..., add_special_tokens=False)[:1024]`,
`len(enc) > 8`, батч 4, right-pad, `log_softmax(logits[:, :-1].float())`, PPL
корпуса = `exp(Σnll/Σтокенов)`). Загрузка состояния — функция
`_load_ckpt_with_resize` **из файла пайплайна того прогона**, чей чекпойнт
меряется: своя реализация загрузки разошлась бы с контуром на первой правке.

Проверки, встроенные в прогон (не отчёт, а отказ):

* **состояние `base` обязано воспроизвести числа S3h** (`evidence/ppl-baseline-v1v2.json`,
  блок `ppl`, комбинация base × bfloat16 × pipeline-токенизатор) — допуск
  `BASE_AGREEMENT_TOL`. Не совпало — проба не является тем же прибором, и числа
  чекпойнтов с ней сравнивать нельзя;
* **состояние `base_untouched`** (вакуумная точка: те же веса без загрузки
  чекпойнта) снимается после всех чекпойнтов: если веса модели не восстановлены
  из снимка базового state_dict, `base_untouched` разойдётся с `base`, и это
  отказ, а не «дрейф».

Коды возврата::

    0 — все состояния измерены, отчёт записан
    1 — отказ: `base` не воспроизвёл S3h либо `base_untouched` разошёлся с `base`
    2 — NOT-VERIFIED: нет входа (набор/веса/чекпойнт/пайплайн/CUDA)

Запуск::

    python3 tools/calib_ppl_probe.py --plan
    python3 tools/calib_ppl_probe.py \\
        --pipeline runs/calib-25-0.7-<ts>/laguna_pipeline_calib.py \\
        --state base=base \\
        --state c500=ckpt:/home/user/gb10-shared/calib/<arm>/checkpoint_500.pt \\
        --out runs/calib-ppl-<ts>/ppl.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: Корень кейса определяется от файла инструмента, но может быть задан явно:
#: проба запускается и **в контейнере стенда** (там 121 ГБ unified-памяти и
#: чекпойнты лежат локально), а в контейнере нет каталога кейса — есть только
#: смонтированный сетевой диск. Второй `sys.path` — каталог самого инструмента:
#: рядом с ним в контейнер кладётся `ppl_probe.py`, методика которого переиспользуется.
CASE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(CASE_ROOT / "tools"))

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Допуск согласия с S3h на состоянии `base`. Оба замера — один и тот же код на
#: одной машине, поэтому расхождение может быть только сменой прибора (другой
#: набор, другой токенизатор, другой dtype); 1e-6 — с запасом на порядок
#: суммирования float32.
BASE_AGREEMENT_TOL = 1e-6

#: Файл S3h — эталон прибора. Числа берутся из него, а не вписаны сюда: правка
#: эталона должна ломать пробу, а не незаметно совпадать с ней. Путь — от корня
#: кейса, который в контейнере стенда задаётся `--case-root`.
S3H_NAME = "evidence/ppl-baseline-v1v2.json"


def load_pipeline_module(path: Path):
    """Импорт файла пайплайна по пути (имя модуля может быть любым).

    На импорте модуль только defines — тяжёлое (transformers, vllm) живёт внутри
    `main()`, поэтому импорт дёшев и стадий не запускает.
    """
    spec = importlib.util.spec_from_file_location("laguna_pipeline_for_calib", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить модуль пайплайна: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def s3h_reference(case_root: Path, sets: list[str], s3h_path: Path | None = None) -> dict:
    """Эталонные числа S3h (base × bfloat16 × pipeline-токенизатор).

    Путь к эталону — параметр, потому что в контейнере стенда каталога кейса нет:
    там `--case-root` — это сетевой диск, а файл эталона лежит рядом с самой пробой
    (доставлен `run_mix_lr_calib.py --probe`). Без этого эталон «не найден», и
    проверка прибора превращается в «не проверено» — то есть в тишину там, где
    нужен ответ.
    """
    path = Path(s3h_path) if s3h_path else case_root / S3H_NAME
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    node = data.get("ppl", {})
    out = {}
    for name in sets:
        if isinstance(node.get(name), dict) and "ppl" in node[name]:
            out[name] = float(node[name]["ppl"])
    return out


def load_base_state(ppl_probe, args, torch):
    """Базовое состояние: веса ADR-002 + настройка токенизатора пайплайна (AD-3).

    Возвращает `(model, tokenizer, base_state_dict, provenance)`; снимок
    state_dict нужен, чтобы между чекпойнтами возвращать веса к базовым, а не
    грузить базовую модель заново (и не мерить «предыдущий чекпойнт с осевшими
    весами»).
    """
    model, tokenizer, prov, errs = ppl_probe.load_model(
        "base", args.dtype, args.device, pipeline_tokenizer=True)
    if model is None:
        return None, None, None, None, errs
    #: Снимок базовых весов держится на **CPU**: на карте он занимает столько же,
    #: сколько модель (~1 ГБ), а нужен только для возврата весов между чекпойнтами.
    #: Проба идёт на локальной машине рядом с чужими GPU-процессами, и лишний
    #: гигабайт на карте — это разница между замером и `CUDA out of memory`.
    snapshot = {k: v.detach().to("cpu", copy=True) for k, v in model.state_dict().items()}
    return model, tokenizer, snapshot, prov, []


def restore(model, snapshot, torch) -> None:
    """Вернуть веса к базовым (снимок на CPU — копирование идёт по одному тензору)."""
    with torch.no_grad():
        model.load_state_dict(snapshot, strict=True)
    model.eval()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def apply_checkpoint(pipe, model, snapshot, path: Path, torch, device: str) -> dict:
    """Загрузить чекпойнт в модель, стоящую в базовом состоянии.

    Перед загрузкой веса возвращаются к базовому снимку: иначе чекпойнт лёг бы
    на веса предыдущего чекпойнта, и `resize`-ветка `_load_ckpt_with_resize`
    копировала бы «старые» строки уже изменённых эмбеддингов.
    """
    restore(model, snapshot, torch)
    #: Чекпойнт грузится на CPU, а на устройство его переносит `load_state_dict`:
    #: `map_location=device` положил бы в память GPU ещё и состояние оптимизатора
    #: (~2/3 файла), которое пробе не нужно вовсе. Замер от этого не меняется —
    #: веса те же тензоры, просто скопированные по одному.
    ck = torch.load(str(path), map_location="cpu")
    if "model" not in ck:
        raise KeyError(f"{path}: в чекпойнте нет ключа 'model' (есть {sorted(ck)[:5]})")
    resized = pipe._load_ckpt_with_resize(model, ck["model"], log_prefix=f"calib[{path.name}]: ")
    model.eval()
    return {"checkpoint": str(path), "resized_embeddings": bool(resized),
            "optimizer_present": "optimizer" in ck}


def run(args) -> tuple[dict, int]:
    import ppl_probe as P  # noqa: E402 — tools/ в sys.path

    if args.plan:
        print(render_plan(args))
        return {}, EXIT_OK

    shims: list[str] = []
    shim = P.repair_broken_pyopenssl()
    if shim:
        shims.append(shim)
    try:
        P.import_transformers()
    except ImportError as exc:
        print(f"NOT-VERIFIED: transformers не импортируется: {exc}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED
    import torch  # noqa: E402

    if args.device.startswith("cuda") and not torch.cuda.is_available():
        print("NOT-VERIFIED: CUDA недоступна — замер PPL нечем сделать", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    pipe_path = Path(args.pipeline)
    if not pipe_path.is_file():
        print(f"NOT-VERIFIED: нет файла пайплайна прогона: {pipe_path}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    wanted_sets = [s for s in (P.SETS if args.sets == "all" else args.sets.split(","))]
    unknown = [s for s in wanted_sets if s not in P.SETS]
    if unknown:
        print(f"NOT-VERIFIED: неизвестные наборы: {unknown}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED
    datasets = {}
    for name in wanted_sets:
        path = args.case_root_resolved / P.SETS[name]
        if not path.is_file():
            print(f"NOT-VERIFIED: набор не найден: {path}", file=sys.stderr)
            return {}, EXIT_NOT_VERIFIED
        datasets[name] = P.read_dataset(path)

    states = parse_states(args.state)
    if not states:
        print("NOT-VERIFIED: не задано ни одного состояния (--state name=base|ckpt:PATH)",
              file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED
    for name, kind, path in states:
        if kind == "ckpt" and not Path(path).is_file():
            print(f"NOT-VERIFIED: чекпойнт {name} не найден: {path}", file=sys.stderr)
            return {}, EXIT_NOT_VERIFIED

    #: Эталон прибора: либо в корне кейса, либо доставлен рядом с пробой
    #: (контейнер стенда). Путь и хеш фиксируются в отчёте — «с чем сравнивали»
    #: должно быть проверяемо, а не подразумеваться.
    s3h_path = Path(args.s3h) if args.s3h else args.case_root_resolved / S3H_NAME
    s3h_info = {"path": str(s3h_path), "sha256": (P.sha256_file(s3h_path)
                                                  if s3h_path.is_file() else None)}
    if not s3h_path.is_file():
        print(f"ВНИМАНИЕ: эталон S3h не найден ({s3h_path}) — проверка прибора будет "
              f"«не проверено», и это отказ пробы, а не её успех", file=sys.stderr)

    pipe = load_pipeline_module(pipe_path)
    model, tokenizer, snapshot, prov, errs = load_base_state(P, args, torch)
    if model is None:
        print("NOT-VERIFIED: " + "; ".join(errs), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    report: dict = {
        "schema": "calib-ppl-probe/1",
        "stage": "S3m",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": "PPL чекпойнтов сетки replay × peak_lr_scale на наборах v1 и v2",
        "instrument": {
            "measure": "tools/ppl_probe.py:measure (методика _ppl_eval S3h)",
            "load_checkpoint": "laguna_pipeline_v8.py:_load_ckpt_with_resize (из копии пайплайна прогона)",
            "pipeline": str(pipe_path),
            "pipeline_sha256": P.sha256_file(pipe_path),
            "base_weights": prov["weights"], "tokenizer": prov["tokenizer"],
            "tokenizer_setup": prov["tokenizer_setup"],
            "max_len": args.max_len, "batch": args.batch, "dtype": args.dtype,
            "device": args.device,
        },
        "env_shims": shims,
        "case_root": str(args.case_root_resolved),
        "s3h_reference_file": s3h_info,
        "sets": {name: {"path": P.SETS[name],
                        "sha256": datasets[name]["sha256"],
                        "bytes": datasets[name]["bytes"],
                        "docs_kept": datasets[name]["docs_kept"],
                        "tokens": None} for name in wanted_sets},
        "states": {},
    }
    print(f"пайплайн: {pipe_path.name}; состояний: {len(states)}; наборов: {len(wanted_sets)}")

    for name, kind, path in states:
        load_info = {"kind": kind}
        if kind == "ckpt":
            try:
                load_info.update(apply_checkpoint(pipe, model, snapshot, Path(path),
                                                  torch, args.device))
            except Exception as exc:  # чекпойнт не загрузился — это отказ, не «пропуск»
                print(f"ОТКАЗ: состояние {name} не загрузилось: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                return {}, EXIT_FAIL
        else:
            restore(model, snapshot, torch)
        entry: dict = {"load": load_info, "sets": {}}
        for set_name in wanted_sets:
            t0 = time.time()
            res = P.measure(model, tokenizer, datasets[set_name]["docs"],
                            args.max_len, args.batch, args.device,
                            cross_check=args.cross_check)
            #: `measure` — чистая функция замера, времени в ней нет (его добавляет
            #: `ppl_probe.run`). Здесь оно нужно на состояние и набор: по нему видно,
            #: что проба из 17 состояний идёт минутами, а не висит.
            res["seconds"] = round(time.time() - t0, 3)
            res.pop("model", None)
            entry["sets"][set_name] = res
            print(f"  {name:12s} {set_name:11s} ppl={res['ppl']:.4f} "
                  f"docs={res['docs_counted']} tok={res['tokens']} "
                  f"({res['seconds']:.1f} с)", flush=True)
        report["states"][name] = entry
        # Инкрементальная запись: состояние замерено — оно уже в отчёте. Проба из
        # 17 состояний идёт минутами, и падение на пятнадцатом не имеет права
        # стирать четырнадцать сделанных замеров.
        write_report(report, args, states, final=False)

    checks = agreement_checks(report, states)
    report["checks"] = checks
    out = write_report(report, args, states, final=True)
    print(f"\nотчёт: {out}")
    bad = [c for c in checks if c["verdict"] != "ok"]
    for c in bad:
        print(f"ОТКАЗ: {c['name']}: {c['detail']}", file=sys.stderr)
    return report, EXIT_FAIL if bad else EXIT_OK


def write_report(report: dict, args, states: list, final: bool) -> Path:
    """Записать отчёт. Промежуточная запись — без `checks` и с явной пометкой."""
    out = Path(args.out) if args.out else Path("runs/calib-ppl/ppl.json")
    if not out.is_absolute():
        out = args.case_root_resolved / out
    out.parent.mkdir(parents=True, exist_ok=True)
    report["complete"] = final
    report["measured_states"] = [s[0] for s in states if s[0] in report["states"]]
    report["pending_states"] = [s[0] for s in states if s[0] not in report["states"]]
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def agreement_checks(report: dict, states: list) -> list[dict]:
    """Проверки прибора: `base` против S3h и `base_untouched` против `base`."""
    checks = []
    ref = s3h_reference(Path(report["case_root"]), list(report["sets"]),
                        s3h_path=report.get("s3h_reference_file", {}).get("path"))
    base = report["states"].get("base", {}).get("sets", {})
    if base and ref:
        worst, detail = 0.0, []
        for set_name, want in ref.items():
            got = base.get(set_name, {}).get("ppl")
            if got is None:
                continue
            rel = abs(got - want) / max(abs(want), 1e-12)
            worst = max(worst, rel)
            detail.append(f"{set_name}: {got:.6f} против {want:.6f} ({rel:.2e})")
        checks.append({
            "name": "base_vs_s3h",
            "what": "состояние base воспроизводит числа S3h (тот же прибор)",
            "reference": report.get("s3h_reference_file", {}).get("path", S3H_NAME),
            "reference_sha256": report.get("s3h_reference_file", {}).get("sha256"),
            "tolerance": BASE_AGREEMENT_TOL, "worst_rel_delta": worst,
            "detail": "; ".join(detail),
            "verdict": "ok" if worst <= BASE_AGREEMENT_TOL else "расхождение",
        })
    else:
        checks.append({"name": "base_vs_s3h", "verdict": "не проверено",
                       "detail": "нет состояния base или эталона S3h"})
    untouched = report["states"].get("base_untouched", {}).get("sets", {})
    if untouched and base:
        worst, detail = 0.0, []
        for set_name, res in base.items():
            got = untouched.get(set_name, {}).get("ppl")
            if got is None:
                continue
            rel = abs(got - res["ppl"]) / max(abs(res["ppl"]), 1e-12)
            worst = max(worst, rel)
            detail.append(f"{set_name}: {got:.6f} против {res['ppl']:.6f} ({rel:.2e})")
        checks.append({
            "name": "base_restored",
            "what": "после загрузки чекпойнтов веса возвращаются к базовым (вакуумная точка)",
            "tolerance": BASE_AGREEMENT_TOL, "worst_rel_delta": worst,
            "detail": "; ".join(detail),
            "verdict": "ok" if worst <= BASE_AGREEMENT_TOL else "расхождение",
        })
    return checks


def parse_states(specs: list[str]) -> list[tuple[str, str, str | None]]:
    """`name=base` | `name=ckpt:/path` → [(name, kind, path)]."""
    out = []
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(f"--state ожидает NAME=base|ckpt:PATH, получено: {spec}")
        name, value = spec.split("=", 1)
        if value == "base":
            out.append((name, "base", None))
        elif value.startswith("ckpt:"):
            path = value[len("ckpt:"):]
            if not path:
                # `ckpt:` без пути — это опечатка, а не «состояние по умолчанию»:
                # молча подставить сюда base значило бы мерить не то, что просили.
                raise SystemExit(f"--state {name}=ckpt: без пути к чекпойнту")
            out.append((name, "ckpt", path))
        else:
            raise SystemExit(f"неизвестное состояние: {value}")
    return out


def render_plan(args) -> str:
    states = parse_states(args.state) if args.state else []
    lines = ["план пробы PPL чекпойнтов S3m:",
             f"  корень кейса:     {args.case_root_resolved}",
             f"  пайплайн прогона: {args.pipeline}",
             f"  наборы:           {args.sets} (из tools/ppl_probe.py:SETS)",
             f"  dtype/batch/len:  {args.dtype} / {args.batch} / {args.max_len}",
             "  состояния:"]
    for name, kind, path in states:
        lines.append(f"    {name:14s} {kind}" + (f" → {path}" if path else ""))
    ref = s3h_reference(args.case_root_resolved, list(__import__("ppl_probe").SETS),
                        s3h_path=Path(args.s3h) if args.s3h else None)
    lines.append("  эталон S3h (base × bfloat16 × pipeline-токенизатор):")
    for k, v in ref.items():
        lines.append(f"    {k:11s} {v:.6f}")
    return "\n".join(lines)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="S3m: PPL чекпойнтов на наборах v1/v2")
    ap.add_argument("--case-root", default=None,
                    help="корень кейса (наборы, эталон S3h); по умолчанию — каталог над tools/")
    ap.add_argument("--pipeline", default=None, help="копия пайплайна прогона")
    ap.add_argument("--s3h", default=None,
                    help="файл эталона S3h (по умолчанию <корень кейса>/" + S3H_NAME + ")")
    ap.add_argument("--state", action="append", default=[], metavar="NAME=base|ckpt:PATH")
    ap.add_argument("--sets", default="all")
    ap.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float32"])
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--out", default=None)
    ap.add_argument("--cross-check", dest="cross_check", action="store_true", default=True)
    ap.add_argument("--no-cross-check", dest="cross_check", action="store_false")
    ap.add_argument("--plan", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    #: Корень кейса: явный флаг (контейнер стенда) либо каталог над `tools/`.
    args.case_root_resolved = (Path(args.case_root).resolve() if args.case_root
                               else CASE_ROOT)
    args.case_root = str(args.case_root_resolved)
    _, rc = run(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
