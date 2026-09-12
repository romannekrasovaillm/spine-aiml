#!/usr/bin/env python3
"""ЕДИНЫЙ детерминированный гейт для всех рук.

На каждую ячейку:

    arch-ml control check <CELL>/work --constraints tasks/<TASK>/CONSTRAINTS.yaml --json

плюс ровно три проверки, невыразимые правилами `RuleKind` (§6 SPEC.md):

    spec_binding          sha256 из `spec: sha256:<hex>` в DECISION.md == хэш SPEC.md
    acceptance_untouched  sha256 work/ACCEPTANCE.md == замороженному
    trace_coverage        каждая `AC-<nn>` адресно покрыта строкой `AC-nn -> ADR-nnn`,
                          и каждый ADR-nnn существует в work/docs/adr/

Никакого LLM. Вердикт:

    units_total = rules_total + 3          # знаменатель один для всех рук и повторов задачи
    units_green = rules_green + <три проверки: 0..3>
    mech_score  = 100 · units_green / units_total

Пишет на ячейку `gate.json` (сырой отчёт + проверки + вердикт) и `record.json`
(строка контракта §8), затем пересобирает `runs/<RUN>/results.jsonl` целиком.

Использование:
    SVC_RUN=<RUN-ID> python3 runners/gate.py [--only-cell C] [--only-task T] [--only-arm A]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import svclib  # noqa: E402

#: Зажим оценки при `hard_fail` (шкала platformv-arch-bench).
HARD_FAIL_CAP = 39.0

#: Порог «принято» / «отлично».
PASS_THRESHOLD = 70.0
OK_THRESHOLD = 85.0

#: Файлы-результаты: диагностика (в знаменатель не входят, см. §6 SPEC.md).
DELIVERABLES: dict[str, str] = {
    "spine": "docs/ARCHITECTURE-SPINE.md",
    "adr": "docs/adr/ADR-*.md",
    "decision": "DECISION.md",
    "trace": "TRACE.md",
    "acceptance_evidence": "ACCEPTANCE-EVIDENCE.md",
}


def run_fitness(work: Path, constraints: Path) -> tuple[dict | None, int, str]:
    """Запустить fitness-контроль. Возвращает `(отчёт, код, stderr)`.

    Ненулевой код возврата — не ошибка: `control check` возвращает 1, когда
    репозиторий не проходит гейт, но JSON-отчёт при этом пишется в stdout.
    """
    argv = [
        svclib.arch_bin(),
        "control",
        "check",
        str(work),
        "--constraints",
        str(constraints),
        "--json",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, 127, f"spawn failed: {exc}"
    err = proc.stderr.decode("utf-8", errors="replace")
    try:
        return json.loads(proc.stdout.decode("utf-8", errors="replace")), proc.returncode, err
    except ValueError:
        return None, proc.returncode, err or "unparseable JSON from control check"


def produced_files(work: Path, seeded: dict[str, str]) -> list[str]:
    """Файлы, которые рука создала или изменила относительно посева."""
    out: list[str] = []
    for path in sorted(work.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(work).as_posix()
        digest = svclib.sha256_file(path) or ""
        if rel not in seeded or seeded.get(rel) != digest:
            out.append(rel)
    return out


def check_spec_binding(work: Path, task: str) -> bool:
    """`spec: sha256:<hex>` в DECISION.md равен замороженному хэшу SPEC.md."""
    frozen = svclib.task_spec_sha256(task)
    if not frozen:
        return False
    text = svclib.read_text(work / "DECISION.md")
    return any(digest == frozen for digest in svclib.SPEC_HASH_RE.findall(text))


def check_acceptance_untouched(work: Path, task: str) -> bool:
    """sha256 `work/ACCEPTANCE.md` равен замороженному."""
    frozen = svclib.sha256_file(svclib.task_dir(task) / "ACCEPTANCE.md")
    return bool(frozen) and svclib.sha256_file(work / "ACCEPTANCE.md") == frozen


def adr_ids_present(work: Path) -> set[str]:
    """Идентификаторы ADR, подтверждённые файлами `work/docs/adr/*.md`.

    Идентификатор нормализуется к полной форме `ADR-NNN` — в такой же форме
    он стоит в правой части следа (`AC-NN -> ADR-NNN`, §6 SPEC.md), иначе
    сверка покрытия сравнивала бы `ADR-001` с `001` и всегда давала бы
    «цель не подтверждена».

    Смотрит только `work/docs/adr/`: spine-дистиллят из `pack/` намеренно
    исключён — ссылаться на ADR-имена из корпуса нельзя, объявить решение
    нужно собственным ADR-файлом.
    """
    ids: set[str] = set()
    adr_dir = work / "docs" / "adr"
    if not adr_dir.is_dir():
        return ids
    for path in sorted(adr_dir.glob("*.md")):
        numbers = svclib.ADR_NAME_RE.findall(path.name)
        numbers += svclib.ADR_HEADING_RE.findall(svclib.read_text(path))
        ids.update(f"ADR-{n}" for n in numbers)
    return ids


def check_trace(task: str, work: Path) -> dict:
    """Адресное покрытие приёмки: `AC-nn -> ADR-nnn`, цель существует."""
    acs = svclib.task_ac_ids(task)
    pairs = svclib.TRACE_PAIR_RE.findall(svclib.read_text(work / "TRACE.md"))
    covered = {ac for ac, _ in pairs}
    present = adr_ids_present(work)
    acs_orphan = [ac for ac in acs if ac not in covered]
    unresolved = sorted({adr for ac, adr in pairs if ac in set(acs) and adr not in present})
    return {
        "acs_total": len(acs),
        "acs_covered": len(acs) - len(acs_orphan),
        "acs_orphan": acs_orphan,
        "adr_unresolved": unresolved,
        "pairs_total": len(pairs),
        "coverage_ok": bool(pairs) and not acs_orphan and not unresolved,
    }


def deliverables(work: Path) -> dict[str, bool]:
    """Булевы «файл-результат на месте»: диагностика отчёта."""
    out: dict[str, bool] = {}
    for key, pattern in DELIVERABLES.items():
        if "*" in pattern:
            parent = work / Path(pattern).parent
            out[key] = parent.is_dir() and any(parent.glob(Path(pattern).name))
        else:
            out[key] = (work / pattern).is_file()
    return out


def gate_cell(cell_dir: Path, run_id: str) -> dict:
    """Провести одну ячейку через гейт; записать `gate.json` и `record.json`."""
    meta = svclib.read_json(cell_dir / "meta.json") or {}
    task = meta.get("task", "")
    work = cell_dir / "work"
    seeded = meta.get("seeded", {})
    rules = svclib.task_rules(task)
    rules_total = len(rules)
    by_name = {rule["name"]: rule.get("severity", "error") for rule in rules}
    critical_names = {name for name, sev in by_name.items() if sev == "critical"}

    report, rc, err = run_fitness(work, svclib.constraints_path(task))
    issues = (report or {}).get("issues", [])
    error_issues = [i for i in issues if i.get("severity") == "error"]
    warn_issues = [i for i in issues if i.get("severity") == "warn"]

    per_rule_errors: dict[str, int] = {}
    per_rule_warns: dict[str, int] = {}
    for issue in error_issues:
        per_rule_errors[issue.get("rule", "?")] = per_rule_errors.get(issue.get("rule", "?"), 0) + 1
    for issue in warn_issues:
        per_rule_warns[issue.get("rule", "?")] = per_rule_warns.get(issue.get("rule", "?"), 0) + 1

    failed_names = {name for name in per_rule_errors if name in by_name}
    rules_green = rules_total - len(failed_names)

    produced = produced_files(work, seeded) if work.is_dir() else []
    spec_binding = check_spec_binding(work, task)
    untouched = check_acceptance_untouched(work, task)
    trace = check_trace(task, work)
    checks = [spec_binding, untouched, trace["coverage_ok"]]
    units_total = rules_total + 3
    units_green = rules_green + sum(1 for c in checks if c)

    if not produced:
        gate = "skipped_no_code"
        mech_score = 0.0
        rules_green = 0
        units_green = 0
    elif report is None:
        gate = "gate_error"
        mech_score = 0.0
        rules_green = 0
        units_green = 0
    else:
        gate = "ok"
        mech_score = round(100.0 * units_green / units_total, 1) if units_total else 0.0

    critical_finding = any(i.get("rule") in critical_names for i in error_issues)
    hard_fail = bool(critical_finding or not all(checks)) and gate == "ok"
    if hard_fail:
        mech_score = min(mech_score, HARD_FAIL_CAP)

    decision_text = svclib.read_text(work / "DECISION.md")
    accountability_ok = spec_binding and bool(svclib.APPROVER_RE.search(decision_text))
    generation = meta.get("generation") or {}
    missed = len(trace["adr_unresolved"]) + len(trace["acs_orphan"])

    record = {
        "run": run_id,
        "cell": cell_dir.name,
        "task": task,
        "arm": meta.get("arm", ""),
        "model": meta.get("model", ""),
        "rep": meta.get("rep", 0),
        "secs": generation.get("secs"),
        "exit_code": generation.get("exit_code"),
        "bytes": generation.get("bytes"),
        "gen_error": generation.get("error"),
        "gate": gate,
        "passed": bool(gate == "ok" and mech_score >= OK_THRESHOLD and not hard_fail),
        "mech_score": mech_score,
        "hard_fail": hard_fail,
        "errors_total": len(error_issues),
        "warns_total": len(warn_issues),
        "rules_total": rules_total,
        "rules_green": rules_green,
        "units_total": units_total,
        "units_green": units_green,
        "per_rule_errors": per_rule_errors,
        "per_rule_warns": per_rule_warns,
        "deliverables": deliverables(work),
        "accountability_ok": accountability_ok,
        "spec_binding": spec_binding,
        "eval_bar": {
            "acceptance_untouched": untouched,
            "trace_coverage": trace["coverage_ok"],
            "coverage_ok": bool(untouched and trace["coverage_ok"]),
        },
        "trace": {
            "acs_total": trace["acs_total"],
            "acs_covered": trace["acs_covered"],
            "acs_orphan": trace["acs_orphan"],
            "adr_unresolved": trace["adr_unresolved"],
        },
        "outcome": {
            "found_by_gate": len(error_issues),
            "missed_by_gate": missed,
            "time_to_green_secs": None,
        },
        "judge": None,
        "produced_files": produced,
    }

    svclib.write_json(
        cell_dir / "gate.json",
        {
            "cell": cell_dir.name,
            "gate": gate,
            "returncode": rc,
            "stderr": err.strip()[-2000:],
            "fitness": report,
            "checks": {
                "spec_binding": spec_binding,
                "acceptance_untouched": untouched,
                **trace,
            },
            "verdict": {
                "rules_total": rules_total,
                "rules_green": rules_green,
                "units_total": units_total,
                "units_green": units_green,
                "mech_score": mech_score,
                "critical_finding": critical_finding,
                "hard_fail": hard_fail,
            },
        },
    )
    svclib.write_json(cell_dir / "record.json", record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only-cell", default="", help="фильтр по имени ячейки")
    parser.add_argument("--only-task", default="", help="фильтр по задаче")
    parser.add_argument("--only-arm", default="", help="фильтр по руке")
    args = parser.parse_args()

    run_id = os.environ.get("SVC_RUN", "").strip()
    if not run_id:
        parser.error("не задан SVC_RUN=<RUN-ID>")

    cells = sorted(p for p in svclib.cells_dir(run_id).glob("*") if (p / "meta.json").is_file())
    if not cells:
        parser.error(f"нет ячеек в {svclib.cells_dir(run_id)}")

    records: list[dict] = []
    for cell_dir in cells:
        meta = svclib.read_json(cell_dir / "meta.json") or {}
        if args.only_cell and cell_dir.name != args.only_cell:
            continue
        if args.only_task and meta.get("task") != args.only_task:
            continue
        if args.only_arm and meta.get("arm") != args.only_arm:
            continue
        record = gate_cell(cell_dir, run_id)
        records.append(record)
        print(
            f"{record['cell']}: {record['gate']} "
            f"mech={record['mech_score']} "
            f"(green {record['units_green']}/{record['units_total']}, "
            f"err {record['errors_total']}, hard_fail {record['hard_fail']})"
        )

    out_path = svclib.run_dir(run_id) / "results.jsonl"
    svclib.write_jsonl(out_path, sorted(records, key=lambda r: r["cell"]))
    print(f"\nзаписано: {out_path} ({len(records)} строк)")
    print(f"дальше: SVC_RUN={run_id} python3 runners/analyze.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
