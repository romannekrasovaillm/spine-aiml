#!/usr/bin/env python3
"""S3f-fix-2 — проба проекции цепочек: три вопроса приёмки, ответ — числа.

Вопрос дельты: **проверяется ли у цепочек промежуточное значение, а не концы.**
Проекция (`tools/build_rev_envs.py`, `PROJECTION_RULES["chain_definition_terms"]`)
собирает проверяемый набор из терминов определений концептов-условий — концы
остаются условием, названным в промпте. Проба отвечает на три вопроса **тем же
замороженным верификатором, что судит награду** (`laguna_pipeline_v8.verify_task`),
а не повтором правила конвертера:

1. **эталонный ответ принимается** — `gold_answer` задачи проходит верификатор.
   Ноль здесь означал бы гарантированный ложный ноль на всей задаче;
2. **копия промпта не награждается** — ответ = сам промпт. В базе этой дельты
   (проекция «концы», состояние S3f-fix) так награждались все 500 цепочек;
   это и есть дефект, ради которого дельта сделана;
3. **ответ только концами отвергается** — ответ, называющий начало и конец (то,
   что лежит в промпте), награды не получает. Плюс отдельная проба
   «промежуточное не проверяется»: ответ, называющий начало, конец и
   промежуточный концепт (имена без определений), тоже отвергается — проверяемое
   здесь содержание концептов-условий, а не имена звеньев.

Проба **ничего не пишет в источники**: пул и индекс только читаются, пайплайн
импортируется (torch) — поэтому числа снимаются пробой, а не сборкой (конвертер
пайплайн не тянет). Единственная запись — артефакт замера
(`evidence/s3f-fix-2-validation.json`, отключается `--no-evidence`).

Проба не является гейтом (AD-10: правила CONSTRAINTS ссылаются только на
`tools/check_*`): это замер, на который ссылается карточка пула.

Коды возврата::

    0 — замер состоялся (числа в артефакте; вердикт печатается, но не судится)
    1 — замер состоялся и **условие приёмки нарушено** (копия промпта награждается
        или эталон отвергнут) — сигнал архитектору, а не «зелёный» прогон
    2 — NOT-VERIFIED: пул/индекс/пайплайн отсутствуют или нечитаемы

Запуск::

    python3 tools/probe_chain_projection.py                     # пул кейса, артефакт
    python3 tools/probe_chain_projection.py --json              # машинный отчёт
    python3 tools/probe_chain_projection.py --no-evidence       # без записи
    python3 tools/probe_chain_projection.py --baseline-projection   # замер «как было»
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_rev_envs import (  # noqa: E402
    CHAIN_TASK_TYPES, NotVerified, chain_concepts, gold_answer, read_jsonl, sha256_file)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

DEFAULT_POOL = "runs/rev-pool-v2/rl_pool_filtered.jsonl"
DEFAULT_EVIDENCE = "evidence/s3f-fix-2-validation.json"
DEFAULT_INDEX = "datasets/concepts_search_index.jsonl"
PIPELINE = "laguna_pipeline_v8.py"


def ensure_concepts_index(path: str) -> None:
    """Индекс концептов для keyword-ветки верификатора — до первого её вызова.

    Пайплайн берёт путь из ``CONCEPTS_INDEX``, а по умолчанию смотрит в
    ``/workspace/shared/...`` — путь стенда, которого в каталоге кейса нет. Проба
    подставляет индекс кейса (симлинк на сетевой диск, AD-4), только если путь не
    задан снаружи: так замер воспроизводит проверку контура, а не окружение стенда.
    """
    if path and Path(path).is_file():
        os.environ.setdefault("CONCEPTS_INDEX", str(Path(path).resolve()))


def load_verifier(path: Path):
    """Замороженный верификатор пайплайна — импортом, без правок и копий.

    Копия запрещена по существу: мера должна быть той же, что судит награду
    (ADR-021/AD-6), а «похожая реализация» этого не гарантирует. Импорт тянет
    torch — поэтому пробу запускают отдельно от сборки пула.
    """
    if not path.is_file():
        raise NotVerified(f"пайплайн не найден: {path} — мера не подтверждена")
    sys.path.insert(0, str(path.resolve().parent))
    try:
        import laguna_pipeline_v8 as pipeline  # noqa: PLC0415
    except Exception as e:  # pragma: no cover — окружение без torch/vllm
        raise NotVerified(f"пайплайн не импортируется ({type(e).__name__}: {e})") from e
    if not hasattr(pipeline, "verify_task"):
        raise NotVerified("в пайплайне нет verify_task — мера не определена")
    return pipeline


def ends_only_answer(task: dict) -> str | None:
    """Ответ «только концы»: имена концептов-условий, без единого определения.

    Это буквально то, что модель может скопировать из промпта, и то, что
    проверяла прежняя проекция.
    """
    found = chain_concepts(str(task.get("prompt") or ""), task["task_type"])
    if found is None:
        return None
    start, end, _hint = found
    return f"Ответ: {start}, {end}"


def with_hint_answer(task: dict) -> str | None:
    """Ответ «концы + промежуточный концепт»: все имена цепочки, без содержания.

    Считается только на задачах, где промпт называет промежуточный концепт
    примером: у `formula_chain` подсказки в формате нет, и там этот ответ совпал
    бы с «только концы» — счёт без оговорки читался бы как лишние 192 замера.
    """
    found = chain_concepts(str(task.get("prompt") or ""), task["task_type"])
    if found is None:
        return None
    start, end, hint = found
    if not hint:
        return None
    return f"Ответ: {start}, {end}, {hint}"


def baseline_projected_answer(task: dict) -> str | None:
    """Ответ по **прежней** проекции: что задача считала проверяемым до дельты.

    Прежний проверяемый набор — `expected_slugs` источника (концы цепочки);
    именно его копировал промпт. Замер нужен, чтобы «было 500, стало 0» было
    воспроизводимым числом, а не ссылкой на прошлый отчёт.
    """
    slugs = task.get("_source_expected_slugs")
    if not slugs:
        return None
    return "Ответ: " + ", ".join(str(s) for s in slugs)


def measure(tasks: list[dict], pipeline) -> dict:
    """Три пробы приёмки по всем цепочкам пула, плюс разбивка по типам."""
    out: dict = {
        "gold_accepted": {"checked": 0, "accepted": 0, "rejected_examples": []},
        "prompt_copy_rewarded": {"checked": 0, "rewarded": 0, "rewarded_examples": []},
        "ends_only_rejected": {"checked": 0, "rejected": 0, "accepted_examples": []},
        "intermediate_not_checked": {"checked": 0, "rejected": 0,
                                     "accepted_examples": []},
    }
    for t in tasks:
        if t.get("task_type") not in CHAIN_TASK_TYPES:
            continue
        tid = t.get("source_task_id", "?")
        accepted = bool(pipeline.verify_task(gold_answer(t), t))
        out["gold_accepted"]["checked"] += 1
        out["gold_accepted"]["accepted"] += int(accepted)
        if not accepted and len(out["gold_accepted"]["rejected_examples"]) < 5:
            out["gold_accepted"]["rejected_examples"].append(
                {"task_id": tid, "expected": t.get("expected_slugs")})

        out["prompt_copy_rewarded"]["checked"] += 1
        copied = bool(pipeline.verify_task(str(t.get("prompt") or ""), t))
        out["prompt_copy_rewarded"]["rewarded"] += int(copied)
        if copied and len(out["prompt_copy_rewarded"]["rewarded_examples"]) < 5:
            out["prompt_copy_rewarded"]["rewarded_examples"].append(
                {"task_id": tid, "task_type": t.get("task_type")})

        ends = ends_only_answer(t)
        if ends is not None:
            out["ends_only_rejected"]["checked"] += 1
            ok = bool(pipeline.verify_task(ends, t))
            out["ends_only_rejected"]["rejected"] += int(not ok)
            if ok and len(out["ends_only_rejected"]["accepted_examples"]) < 5:
                out["ends_only_rejected"]["accepted_examples"].append(
                    {"task_id": tid, "answer": ends})

        hinted = with_hint_answer(t)
        if hinted is not None:
            out["intermediate_not_checked"]["checked"] += 1
            ok = bool(pipeline.verify_task(hinted, t))
            out["intermediate_not_checked"]["rejected"] += int(not ok)
            if ok and len(out["intermediate_not_checked"]["accepted_examples"]) < 5:
                out["intermediate_not_checked"]["accepted_examples"].append(
                    {"task_id": tid, "answer": hinted})
    return out


def measure_baseline_projection(tasks: list[dict], pipeline) -> dict:
    """Замер **прежней** проекции на тех же задачах: сколько награждалось копией.

    Ответ = промпт, проверяемый набор — концы цепочки (проекция «концы», состояние
    S3f-fix: до этой дельты). Число воспроизводит дефект, ради которого дельта
    сделана, а не пересказывает прежний отчёт; считается по **всем** цепочкам
    источника (500), включая те 30, что новая проекция снимает консервативно, —
    иначе «было/стало» сравнивало бы разные наборы задач.
    """
    checked = rewarded = 0
    for t in tasks:
        if t.get("task_type") not in CHAIN_TASK_TYPES:
            continue
        slugs = t.get("_source_expected_slugs") or t.get("expected_slugs")
        if not slugs:
            continue
        checked += 1
        probe = {"task_type": t["task_type"], "expected_slugs": [str(s) for s in slugs]}
        if pipeline.verify_task(str(t.get("prompt") or ""), probe):
            rewarded += 1
    return {"checked": checked, "prompt_copy_rewarded": rewarded,
            "projection": ("концы цепочки (expected_slugs источника) — состояние "
                           "S3f-fix, до этой дельты"),
            "scope": "все цепочки источника задач, включая снятые новой проекцией"}


def measure_pool_wide(tasks: list[dict], pipeline) -> dict:
    """Второй вопрос приёмки — по **всему** пулу, а не только по цепочкам.

    Цепочки — предмет дельты, но критерий приёмки сформулирован про пул: «задач,
    награждаемых за копирование промпта — 0». Мера та же (ответ = сам промпт, тот
    же замороженный верификатор); считается по всем типам, потому что фильтр пула
    и замер обязаны говорить об одном и том же артефакте.
    """
    rewarded, by_type = 0, {}
    for t in tasks:
        if pipeline.verify_task(str(t.get("prompt") or ""), t):
            rewarded += 1
            tt = str(t.get("task_type"))
            by_type[tt] = by_type.get(tt, 0) + 1
    return {"checked": len(tasks), "prompt_copy_rewarded": rewarded,
            "rewarded_by_task_type": by_type}


def load_pool_with_source(path: Path) -> tuple[list[dict], list[dict]]:
    """Пул и задачи-цепочки источника — для замера прежней проекции.

    `expected_slugs` в пуле уже перепроецированы (термины определений), поэтому
    прежний набор берётся из источника задач (`datasets/rl_tasks_env_types.jsonl`)
    по `source_task_id`: воспроизводить «как было» надо по факту, а не по памяти.
    Источник читается и целиком — чтобы «было» мерилось на всех 500 цепочках, а
    «стало» на тех, что вошли в пул.
    """
    tasks = [o for _i, o in read_jsonl(path)]
    source = Path("datasets/rl_tasks_env_types.jsonl")
    if not source.is_file():
        return tasks, []
    by_id, source_chains = {}, []
    for i, o in read_jsonl(source):
        tid = str(o.get("id") or o.get("task_id") or f"line:{i + 1}")
        if o.get("task_type") not in CHAIN_TASK_TYPES:
            continue
        rec = {"task_type": o["task_type"], "prompt": o.get("prompt") or "",
               "source_task_id": tid,
               "_source_expected_slugs": list(o.get("expected_slugs") or [])}
        source_chains.append(rec)
        by_id[tid] = rec["_source_expected_slugs"]
    for t in tasks:
        slugs = by_id.get(str(t.get("source_task_id")))
        if slugs:
            t["_source_expected_slugs"] = list(slugs)
    return tasks, source_chains


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="S3f-fix-2: проба проекции цепочек — эталон принимается, копия "
                    "промпта не награждается, ответ только концами отвергается.")
    ap.add_argument("--pool", default=DEFAULT_POOL, help=f"пул (.jsonl), по умолчанию {DEFAULT_POOL}")
    ap.add_argument("--pipeline", default=PIPELINE, help=f"пайплайн, по умолчанию {PIPELINE}")
    ap.add_argument("--index", default=DEFAULT_INDEX,
                    help=("индекс концептов для keyword-ветки верификатора "
                          f"(CONCEPTS_INDEX), по умолчанию {DEFAULT_INDEX}"))
    ap.add_argument("--evidence", default=DEFAULT_EVIDENCE,
                    help=f"артефакт замера, по умолчанию {DEFAULT_EVIDENCE}")
    ap.add_argument("--no-evidence", action="store_true", help="не писать артефакт")
    ap.add_argument("--baseline-projection", action="store_true",
                    help="дополнительно замерить прежнюю проекцию (концы) на тех же задачах")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    pool_path = Path(args.pool)
    try:
        if not pool_path.is_file():
            raise NotVerified(f"пул не найден: {pool_path}")
        ensure_concepts_index(args.index)
        tasks, source_chains = load_pool_with_source(pool_path)
        if not tasks:
            raise NotVerified(f"{pool_path}: ни одной задачи")
        pipeline = load_verifier(Path(args.pipeline))
    except NotVerified as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    measured = measure(tasks, pipeline)
    pool_wide = measure_pool_wide(tasks, pipeline)
    report: dict = {
        "probe": "S3f-fix-2 / проекция цепочек",
        "question": ("проверяются ли у цепочек промежуточные значения (содержание "
                     "концептов-условий), а не концы, названные в промпте"),
        "verifier": f"{args.pipeline}::verify_task (заморожен, импортирован живым прогоном)",
        "concepts_index": os.environ.get("CONCEPTS_INDEX", "(путь пайплайна по умолчанию)"),
        "pool": {"path": str(pool_path), "lines": len(tasks),
                 "sha256": sha256_file(pool_path)},
        "chains": {k: v for k, v in _by_type(tasks).items() if k in CHAIN_TASK_TYPES},
        "chains_in_source": _by_type(source_chains),
        "method": {
            "gold": "gold_answer(задача) — эталонный ответ конвертера",
            "prompt_copy": "ответ = prompt задачи (то, что можно скопировать из промпта)",
            "ends_only": "ответ = имена начала и конца (условие задачи)",
            "with_hint": "ответ = имена начала, конца и промежуточного концепта (без содержания)",
        },
        "measured": measured,
        "pool_wide": pool_wide,
    }
    if args.baseline_projection:
        report["baseline_projection"] = measure_baseline_projection(source_chains, pipeline)

    verdict = _verdict(measured)
    report["verdict"] = verdict
    report["acceptance"] = {
        "gold_accepted": measured["gold_accepted"]["accepted"] == measured["gold_accepted"]["checked"],
        "prompt_copy_rewarded_zero": pool_wide["prompt_copy_rewarded"] == 0,
        "ends_only_rejected": (measured["ends_only_rejected"]["rejected"]
                               == measured["ends_only_rejected"]["checked"]),
    }
    report["ok"] = all(report["acceptance"].values())

    written = None
    if not args.no_evidence:
        ev = Path(args.evidence)
        ev.parent.mkdir(parents=True, exist_ok=True)
        ev.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
        written = {"path": str(ev), "sha256": sha256_file(ev),
                   "pool_sha256_in_evidence": report["pool"]["sha256"]}

    if args.json:
        print(json.dumps({**report, "evidence_written": written},
                         ensure_ascii=False, indent=2))
    else:
        _print_report(report, written)
    return EXIT_OK if report["ok"] else EXIT_FAIL


def _by_type(tasks: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tasks:
        out[str(t.get("task_type"))] = out.get(str(t.get("task_type")), 0) + 1
    return dict(sorted(out.items()))


def _verdict(measured: dict) -> str:
    if measured["prompt_copy_rewarded"]["rewarded"]:
        return "ПРОВАЛ: копия промпта награждается — проекция не решает задачу дельты"
    if measured["gold_accepted"]["accepted"] != measured["gold_accepted"]["checked"]:
        return "ПРОВАЛ: эталонный ответ отвергается — гарантированный ложный ноль"
    if measured["ends_only_rejected"]["rejected"] != measured["ends_only_rejected"]["checked"]:
        return "ПРОВАЛ: ответ только концами принимается — проверяются концы, а не содержание"
    return "ПРИНЯТО: эталон принимается, копия промпта не награждается, концы не проходят"


def _print_report(report: dict, written: dict | None) -> None:
    m = report["measured"]
    print("== S3f-fix-2: проба проекции цепочек (ADR-021, уточнение 16.09.2026) ==")
    print(f"пул:    {report['pool']['path']}  задач: {report['pool']['lines']}  "
          f"sha256 {report['pool']['sha256'][:12]}…")
    print(f"цепочки: {report['chains']}")
    print(f"мера:   {report['verifier']}")
    print()
    g, p, e, i = (m["gold_accepted"], m["prompt_copy_rewarded"],
                  m["ends_only_rejected"], m["intermediate_not_checked"])
    print(f"1. эталонный ответ принимается:      {g['accepted']}/{g['checked']}")
    pw = report["pool_wide"]
    print(f"2. копия промпта награждается:       {pw['prompt_copy_rewarded']}/{pw['checked']}"
          f"  (по всему пулу; по цепочкам {p['rewarded']}/{p['checked']})")
    if "baseline_projection" in report:
        b = report["baseline_projection"]
        print(f"   (прежняя проекция, концы:         {b['prompt_copy_rewarded']}/{b['checked']})")
    print(f"3. ответ только концами отвергается: {e['rejected']}/{e['checked']}")
    print(f"4. ответ с промежуточным (без опр.): {i['rejected']}/{i['checked']}")
    print()
    print(report["verdict"])
    if written:
        print(f"артефакт: {written['path']}  sha256 {written['sha256'][:12]}…")


if __name__ == "__main__":
    sys.exit(main())
