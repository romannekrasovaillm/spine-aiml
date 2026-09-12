"""Датасет H2.1: 4 задачи spine-vs-claude -> строки Parquet.

Колонки: data_source, prompt, ability, reward_model (style=rule), extra_info
(JSON с patterns — must_contain-паттерны CONSTRAINTS задачи).
"""
import json
import re
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

TASKS = [
    "gpu-quota-policy",
    "ml-serving-split",
    "experiment-lineage",
    "model-registry-migration",
]


def load_patterns(task):
    """must_contain-паттерны из CONSTRAINTS.yaml задачи."""
    with open(f"tasks/{task}/CONSTRAINTS.yaml") as f:
        doc = yaml.safe_load(f)
    pats = []
    for r in doc.get("constraints", []):
        if r.get("type") in ("must_contain", "each_file_must_contain"):
            p = r.get("pattern")
            if p:
                pats.append(p)
    return pats


def main(out="data/train.parquet"):
    rows = []
    for task in TASKS:
        task_md = open(f"tasks/{task}/TASK.md").read()
        spec_md = open(f"tasks/{task}/SPEC.md").read()
        patterns = load_patterns(task)
        prompt = (
            f"{spec_md}\n\n{task_md}\n\n"
            "Сгенерируй артефакты решения одним markdown-документом: "
            "## AD-<n> инварианты, # ADR-NNN (контекст/решение/последствия), "
            "## Решение с Approver: <ФИО> и spec: sha256:<hex>. "
            "Проверит механический гейт."
        )
        rows.append({
            "data_source": task,
            "prompt": [{"role": "user", "content": prompt}],
            "ability": "arch",
            "reward_model": {"style": "rule"},
            "extra_info": json.dumps({"task": task, "patterns": patterns}),
        })
    tbl = pa.Table.from_pylist(rows)
    pq.write_table(tbl, out)
    print(f"записано {len(rows)} строк -> {out}; паттернов: "
          f"{[len(load_patterns(t)) for t in TASKS]}")


if __name__ == "__main__":
    main(*sys.argv[1:])
