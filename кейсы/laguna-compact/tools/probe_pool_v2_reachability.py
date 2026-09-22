#!/usr/bin/env python3
"""ADR-021 п.5 — достижимость эталона пула v2 через `search_concepts`.

**Вопрос.** ADR-021 (раздел «Отложенные решения», вопрос 5) фиксирует: «достижимость
эталона через `search_concepts` проверена ad-hoc (20 задач), в артефакт не входит —
вынести в evidence следующей дельты». Проба выполняет эту работу и заодно отвечает,
воспроизводима ли она: ad-hoc прогон не оставил ни списка задач, ни критерия, поэтому
«те же 20 задач» восстановить нельзя — проба берёт **свою** детерминированную выборку
и печатает её `task_id` поимённо, чтобы следующий прогон шёл по тем же задачам.

**Что значит «достижим».** Ровно одно: вывод `search_concepts`, полученный на запрос,
взятый **из самого промпта**, сам проходит замороженный верификатор пайплайна
(`laguna_pipeline_v8.verify_task`) — то есть модель может добыть инструментом
проверяемые строки и перенести их в ответ, не зная эталона заранее. Копия вывода
инструмента — верхняя граница того, что модель способна сделать за ход: верификатор
не считает текст `<tool_response>` (награждаются слова модели), поэтому вывод подаётся
как обычный ответ.

**Запрос берётся из промпта, а не из эталона.** Имя концепта извлекается из промпта
механически: прогоны латинских токенов (`Additive Angular Margin Softmax Loss`,
`joint_semantic_preservation_metric`). Эталон (`expected_slugs`, `keywords`,
`chain_roles`) в построении запроса **не участвует** — иначе замер доказывал бы, что
инструмент умеет искать по известному ответу, а не что задача решаема из промпта.

**Два счёта.**

1. **Достижимо** (`reachable`): объединённый вывод инструмента по всем запросам-кандидатам
   проходит `verify_task`. Это и есть ответ на вопрос п.5.
2. **Контроль** (`prompt_alone`): проходит ли верификатор сам промпт — лейк-инвариант
   ADR-021 п.3. Ожидание — 0; ненулевое значение означает, что задача решается копией
   промпта, и п.5 к ней неприменим.

Счёт 2 считается **тем же судьёй**, что и счёт 1: расхождение с `checks.gold_in_prompt_audit`
карточки было бы дефектом пробы, а не пула.

**Что проба не делает.** Не пишет в пул, источники и карточку (только чтение), не
пересобирает пул, не является гейтом (AD-10: правила CONSTRAINTS ссылаются только на
``tools/check_*``), не судит о качестве задач — только о добываемости проверяемых строк.

Коды возврата::

    0 — измерение состоялось
    3 — NOT-VERIFIED: индекс концептов, пул или пайплайн недоступны (не «зелёный» ноль)

Примеры::

    python3 tools/probe_pool_v2_reachability.py --json
    python3 tools/probe_pool_v2_reachability.py --n-per-type 5 --seed 42 --out evidence/x.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_NOT_VERIFIED = 3

#: Имя концепта в промпте — латиница/цифры, допускаются внутренние `._-/`.
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/]*")
#: Служебные слова шаблонов: имя концепта ими не является.
STOP_NAMES = {
    "latex", "json", "yaml", "html", "api", "ml", "llm", "n", "top", "k",
    "a", "b", "i", "q", "v", "x", "y", "z",
}
MIN_NAME_LEN = 4
MAX_CANDIDATES = 12


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def candidate_queries(prompt: str) -> list[str]:
    """Имена-кандидаты из промпта: прогоны латинских токенов (сам промпт, без эталона)."""
    names: list[str] = []
    run: list[str] = []
    for m in TOKEN_RE.finditer(prompt):
        tok = m.group(0)
        prev_end = run_end = None
        if run:
            prev_end = run[-1][1]
            run_end = m.start()
        if run and run_end is not None and prev_end is not None and run_end - prev_end == 1:
            run.append((tok, m.end()))
        else:
            if run:
                names.append(" ".join(t for t, _ in run))
            run = [(tok, m.end())]
    if run:
        names.append(" ".join(t for t, _ in run))

    out: list[str] = []
    for name in names:
        parts = name.split()
        # прогон целиком, затем его хвосты: имя может склеиться с соседним словом
        for i in range(len(parts)):
            cand = " ".join(parts[i:])
            if len(cand) >= MIN_NAME_LEN and cand.lower() not in STOP_NAMES:
                out.append(cand)
            if len(parts) - i <= 1:
                break
    seen, uniq = set(), []
    for c in out:
        if c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)
    return uniq[:MAX_CANDIDATES]


def load_pipeline(index: Path):
    os.environ.setdefault("CONCEPTS_INDEX", str(index))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import laguna_pipeline_v8 as P  # noqa: E402
    return P


def sample_tasks(pool: list[dict], n_per_type: int, seed: int,
                 task_ids: list[str] | None, key: str = "task_type") -> list[dict]:
    if task_ids:
        by_id = {t.get("source_task_id"): t for t in pool}
        missing = [i for i in task_ids if i not in by_id]
        if missing:
            raise SystemExit(f"NOT-VERIFIED: задач нет в пуле: {missing[:5]}")
        return [by_id[i] for i in task_ids]
    by_stratum: dict[str, list[dict]] = {}
    for t in pool:
        by_stratum.setdefault(t.get(key, "?"), []).append(t)
    rnd = random.Random(seed)
    picked: list[dict] = []
    for stratum in sorted(by_stratum):
        rows = by_stratum[stratum]
        k = min(n_per_type, len(rows))
        picked.extend(rnd.sample(rows, k))
    return picked


def measure(task: dict, P) -> dict:
    prompt = task.get("prompt", "")
    queries = candidate_queries(prompt)
    outputs = [P.search_concepts(q) for q in queries]
    combined = "\n".join(outputs)
    reached = bool(P.verify_task(combined, task))
    prompt_alone = bool(P.verify_task(prompt, task))
    # какие именно запросы дали строки эталона (справка, не вердикт)
    hits = [q for q, o in zip(queries, outputs)
            if gold_in_output(task, o)]
    return {
        "task_id": task.get("source_task_id"),
        "task_type": task.get("task_type"),
        "source_env": task.get("source_env"),
        "queries": queries,
        "queries_with_gold": hits,
        "reachable": reached,
        "prompt_alone_passes": prompt_alone,
        "prompt_excerpt": prompt[:160],
    }


def gold_in_output(task: dict, output: str) -> bool:
    """Справка: встречается ли проверяемая строка задачи в выводе инструмента."""
    low = output.lower()
    tt = task.get("task_type", "")
    if tt in ("find_concept", "chain_reasoning", "formula_chain"):
        return any(str(s).lower() in low for s in (task.get("expected_slugs") or []))
    if tt == "explain_relation":
        return any(str(k).lower() in low for k in (task.get("keywords") or []))
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ADR-021 п.5: достижимость эталона пула v2 через search_concepts")
    ap.add_argument("--pool", default="runs/rev-pool-v2/rl_pool_filtered.jsonl")
    ap.add_argument("--index", default="datasets/concepts_search_index.jsonl")
    ap.add_argument("--n-per-type", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--stratify-by", default="task_type",
                    choices=("task_type", "source_env"),
                    help="по какому полю брать n-per-type задач в страте")
    ap.add_argument("--task-ids", nargs="*", default=None,
                    help="явный список source_task_id (воспроизведение конкретной выборки)")
    ap.add_argument("--out", default=None, help="куда записать JSON (по умолчанию stdout)")
    args = ap.parse_args(argv)

    pool_path, index_path = Path(args.pool), Path(args.index)
    for p, what in ((pool_path, "--pool"), (index_path, "--index")):
        if not p.is_file():
            print(f"NOT-VERIFIED: {what}: файл не найден: {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED

    try:
        P = load_pipeline(index_path)
    except Exception as e:  # noqa: BLE001 — пайплайн вне дерева, причина важнее типа
        print(f"NOT-VERIFIED: пайплайн не загрузился: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    pool = read_jsonl(pool_path)
    tasks = sample_tasks(pool, args.n_per_type, args.seed, args.task_ids,
                         args.stratify_by)
    rows = [measure(t, P) for t in tasks]

    by_stratum: dict[str, dict] = {}
    for r in rows:
        b = by_stratum.setdefault(r[args.stratify_by], {"n": 0, "reachable": 0,
                                                        "prompt_alone": 0,
                                                        "gold_in_output": 0})
        b["n"] += 1
        b["reachable"] += int(r["reachable"])
        b["prompt_alone"] += int(r["prompt_alone_passes"])
        b["gold_in_output"] += int(bool(r["queries_with_gold"]))

    report = {
        "probe": "pool-v2-reachability (ADR-021 п.5)",
        "criterion": ("эталон достижим, если объединённый вывод search_concepts по запросам, "
                      "извлечённым из промпта, проходит замороженный verify_task — то есть "
                      "проверяемые строки добываются инструментом и переносятся в ответ"),
        "sample": {"n": len(rows), "n_per_type": args.n_per_type, "seed": args.seed,
                   "stratify_by": args.stratify_by,
                   "task_ids": [r["task_id"] for r in rows]},
        "inputs": {
            "pool": {"path": str(pool_path), "sha256": sha256_file(pool_path)},
            "index": {"path": str(index_path), "sha256": sha256_file(index_path)},
            "verifier": "laguna_pipeline_v8.verify_task (заморожен, читается живым импортом)",
        },
        "totals": {
            "n": len(rows),
            "reachable": sum(r["reachable"] for r in rows),
            "prompt_alone_passes": sum(r["prompt_alone_passes"] for r in rows),
            "gold_in_output": sum(bool(r["queries_with_gold"]) for r in rows),
        },
        "by_stratum": by_stratum,
        "tasks": rows,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
