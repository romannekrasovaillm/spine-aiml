#!/usr/bin/env python3
"""Страж заземления (ADR-049 п.5, п.11): «успех без tool_call > 0 → красное».

**Что проверяется.** Правило ADR-049 п.5 работает по заземлённому артефакту: есть
скелет (`evidence/grounded-skeleton.json`, прогон `tools/grounded_runner.py`) или
пул стадии (`datasets/rl_tasks_grounded*.jsonl`). Страж читает доказательство и
сверяет его с самим набором:

  * доля успехов пробы no-action = 0 (и то же для вырожденной пробы empty-action);
  * положительный контроль проходит (иначе среда не пропускает действия —
    вырождение второго вида, ADR-049 п.6);
  * id задачи не содержит ожидаемого ответа (ADR-049 п.9);
  * утечек gold в промпт, seed и ответ мока нет;
  * доказательство описывает ИМЕННО текущий набор: sha256 набора и хеши
    верификаторов совпадают с записанными (иначе evidence устарел, ADR-012).

**Канонический хеш набора не зависит от пересобираемого байткода.** В каноническую
карту входят только содержательные файлы: `__pycache__/` и `*.pyc` исключены
(решение архитектора 20.09.2026). Причина не в удобстве, а в воспроизводимости:
байткод производен от исходников, в git не хранится и появляется или исчезает от
одного импорта на машине — хеш, посчитанный вместе с ним, из чистого клона не
воспроизводится, и правило краснеет по построению (ADR-023 п.12). Ту же политику
держит раннер (`tools/grounded_runner.set_digest`); совпадение формул сверяет тест
`tools/tests/run_tool_tests.sh` (раздел 30) — иначе страж и карточка разошлись бы.

**Цепочка идентичностей не объявляет прежнюю запись устаревшей.** Пересборка карточки
оставляет прежнюю пару рядом с новой (`previous_sha256_full` и соседние поля), а более
раннюю — в `previous_history` (ADR-028 п.1: факт первичен, прежнее не затирается; у
цепочки нет «одного слота»). Доказательство, записанное до пересборки, опознаётся по
ней как описание ТОЙ ЖЕ содержательной части набора, но не «на слово»: запись несёт
**полную каноническую карту** (`previous_files`), её хеш обязан из карты
восстанавливаться, а отличия от текущей карты — быть разложены по трём объявленным
классам: пересобираемый байткод (`previous_files_excluded`), документы КОРНЯ набора
(`previous_files_doc_delta`) и измеренные артефакты задач и мока
(`previous_files_measured_delta`).

Классы не косметика. Необъявленное отличие — красное (`card-previous-inconsistent`):
подмена задачи обязана быть названа, а не спрятана за «прежней идентичностью». А
доказательство опознаётся как «прежнее» только пока **измеренные артефакты не
менялись**: изменилась задача — доказательство устарело и пересобирается (ADR-012),
даже если прежняя запись честно объявила это классом.

**Почему два режима, а не один.** ADR-049 п.11: при отсутствии заземлённого пула
правило обязано давать PENDING-EVIDENCE, а не error — иначе повторяется класс
«тест, красный by construction» (ADR-023 п.12). Поэтому состояний три:

  `--mode guard`   (правило severity: error) — красное ТОЛЬКО когда артефакт есть
                   и в нём нарушение; нет артефакта → зелёный (exit 0);
  `--mode pending` (правило severity: warn)  — красное ТОЛЬКО когда артефактов нет
                   вовсе; имя правила в отчёте и есть видимое «PENDING-EVIDENCE»;
  нарушение при существующем артефакте в pending-режиме — не его предмет
                   (за него отвечает guard-правило), поэтому здесь exit 0.

**Границы.** Страж механический: он читает числа доказательства, а не судит
качество задач. Устаревшее доказательство страж не чинит: после правки содержательных
файлов набор пересобирается (`python3 tools/grounded_runner.py --card`) и прогон
повторяется. Пересборка карточки при неизменной содержательной части — не правка
набора: запись, сделанная до неё, остаётся действительной, если её идентичность
восстанавливается (см. выше), и страж называет это вслух в выводе, а не молчит.

**Границы предмета.** Контракт СРЕДЫ (выход наружу закрыт, лимит шагов — из задачи,
шаг под супервизором) держит отдельный страж `tools/check_env_contract.py` (правило
C-028): он проверяет это живой пробой, а здесь читается только основание награды
(G1–G3) и опознание набора.

Коды возврата::

    0 — состояние зелёное для выбранного режима
    1 — красное по предмету режима (см. выше)
    2 — NOT-VERIFIED: артефакт есть, но нечитаем/не той схемы (молчание не доказательство)

Примеры::

    python3 tools/check_grounded_pool.py              # режим guard (правило C-026)
    python3 tools/check_grounded_pool.py --mode pending   # режим PENDING (правило C-027)
    python3 tools/check_grounded_pool.py --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

SCHEMA = "grounded-skeleton-evidence/1"
SET_SCHEMA = "grounded-skeleton-card/1"

# Политика канонического хеша: пересобираемый байткод вне карты (см. шапку модуля).
# Формулу держат два файла — здесь и `tools/grounded_runner.py`; совпадение сверяет
# тест `tools/tests/run_tool_tests.sh`, раздел 30.
REBUILDABLE_DIRS = ("__pycache__",)
REBUILDABLE_SUFFIXES = (".pyc",)


def sha256_file(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def is_rebuildable(rel: str) -> bool:
    """Пересобираемый артефакт набора: байткод Python — вне канонической карты."""
    parts = pathlib.PurePosixPath(rel).parts
    return any(part in REBUILDABLE_DIRS for part in parts[:-1]) or rel.endswith(REBUILDABLE_SUFFIXES)


def canonical_digest(files: dict[str, str]) -> str:
    """sha256 канонической карты {относительный путь: sha256 файла}, без усечения."""
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def set_digest(set_dir: pathlib.Path) -> tuple[str, dict[str, str]]:
    """Полный sha256 набора (та же функция, что в tools/grounded_runner.py --card)."""
    files: dict[str, str] = {}
    for p in sorted(set_dir.rglob("*")):
        if p.is_file() and p.name != "card.json":
            rel = p.relative_to(set_dir).as_posix()
            if not is_rebuildable(rel):
                files[rel] = sha256_file(p)
    return canonical_digest(files), files


def _read_entry(block: dict, prefix: str = "") -> dict:
    """Запись цепочки идентичностей: `previous_*` и элементы `previous_history` — одна форма."""
    def g(name: str):
        return block.get(f"{prefix}{name}") if prefix else block.get(name)

    return {
        "sha256_full": g("sha256_full"),
        "method": g("sha256_full_method"),
        "at": g("sha256_full_at"),
        "reason": g("sha256_full_reason"),
        "files": g("files") or {},
        "excluded": g("files_excluded") or {},
        "doc_delta": g("files_doc_delta") or {},
        "measured_delta": g("files_measured_delta") or {},
        "size_bytes": g("size_bytes"),
    }


def _delta(old_files: dict, new_files: dict) -> list[str]:
    """Отличия прежней карты от текущей — полным перебором, без догадок о классах."""
    rels = set(old_files) | set(new_files)
    return sorted(rel for rel in rels if old_files.get(rel) != new_files.get(rel))


def _check_entry(label: str, entry: dict, files: dict) -> tuple[list[str], dict]:
    """Проверка одной записи цепочки: объявленные классы и сходимость с текущей картой."""
    problems: list[str] = []
    if not entry["sha256_full"]:
        return problems, {}
    for field in ("method", "at", "reason"):
        if not str(entry[field] or "").strip():
            problems.append(f"{label}.{field} не заполнено: прежнее объявление без метода, даты и причины "
                            "неотличимо от подмены")
    for rel in entry["excluded"]:
        if not is_rebuildable(rel):
            problems.append(
                f"{label}.files_excluded[{rel!r}]: пересобираемым артефактом не является — "
                "из канонической карты можно исключать только байткод (__pycache__/, *.pyc)"
            )
    for rel in entry["doc_delta"]:
        if is_rebuildable(rel) or "/" in rel:
            problems.append(
                f"{label}.files_doc_delta[{rel!r}]: класс «документ корня набора» — только файлы корня "
                "(без каталогов и не байткод); измеренные артефакты задач и мока сюда не входят"
            )
    for rel in entry["measured_delta"]:
        if is_rebuildable(rel) or "/" not in rel:
            problems.append(
                f"{label}.files_measured_delta[{rel!r}]: класс «измеренный артефакт» — только файлы "
                "задач и мока (путь с каталогом); документы корня объявляются отдельным классом"
            )
    detail: dict = {
        "previous_sha256_full": entry["sha256_full"],
        "previous_files_excluded": sorted(entry["excluded"]),
        "previous_files_doc_delta": sorted(entry["doc_delta"]),
        "previous_files_measured_delta": sorted(entry["measured_delta"]),
    }
    if not entry["files"]:
        # Объявление до введения полной карты: сводится к текущей только классами,
        # поэтому доказательство под ним не опознаётся — его придётся пересобрать.
        detail.update({
            "verified": False, "acceptable_for_evidence": False,
            "note": "объявление без полной канонической карты (files): проверяемо только классами, "
                    "к текущей карте не сводится",
        })
        return problems, detail
    restored = canonical_digest(entry["files"])
    detail["restored_sha256_full"] = restored
    if restored != entry["sha256_full"]:
        problems.append(
            f"{label}: объявленный хеш не сходится с объявленной картой — объявлено {entry['sha256_full']}, "
            f"из карты выходит {restored}"
        )
        detail.update({"verified": False, "acceptable_for_evidence": False})
        return problems, detail

    # Отличия прежней карты от текущей обязаны быть объявлены ПО КЛАССАМ и полностью:
    # необъявленное изменение — это подмена, а не «прежняя идентичность».
    delta = _delta(entry["files"], files)
    for rel in delta:
        old = entry["files"].get(rel)
        if is_rebuildable(rel):
            declared, klass = entry["excluded"].get(rel, ""), "files_excluded"
        elif "/" not in rel:
            declared, klass = entry["doc_delta"].get(rel, ""), "files_doc_delta"
        else:
            declared, klass = entry["measured_delta"].get(rel, ""), "files_measured_delta"
        if rel not in (entry["excluded"] if is_rebuildable(rel) else
                       (entry["doc_delta"] if "/" not in rel else entry["measured_delta"])):
            problems.append(
                f"{label}: отличие {rel!r} не объявлено ни одним классом — содержательная часть набора "
                "изменилась не объявленным способом (объявляй классом: байткод / документ корня / измеренный артефакт)"
            )
        elif declared != (old or ""):
            problems.append(
                f"{label}: {klass}[{rel!r}] объявляет {declared!r}, а в прежней карте {old!r} — "
                "объявление расходится с картой"
            )
    measured_changed = sorted(r for r in delta if "/" in r and not is_rebuildable(r))
    detail.update({
        "verified": not problems,
        "delta": {r: {"was": entry["files"].get(r), "now": files.get(r)} for r in delta},
        "measured_changed": measured_changed,
        # Опознание доказательства: под прежней идентичностью оно описывает ту же
        # содержательную часть только если измеренные артефакты не менялись.
        "acceptable_for_evidence": not measured_changed,
    })
    if measured_changed:
        detail["note"] = ("изменились измеренные артефакты: " + ", ".join(measured_changed)
                          + " — доказательство, записанное под прежней идентичностью, устарело (ADR-012)")
    return problems, detail


def previous_identity(card_doc: dict, files: dict) -> tuple[dict[str, dict], list[str], dict]:
    """Цепочка идентичностей набора: `previous_*` и `previous_history` (ADR-028 п.1).

    Прежнее объявление нужно стражу не как архив. По нему доказательство, записанное
    ДО пересборки, остаётся опознаваемым как описание ТОЙ ЖЕ содержательной части
    набора. Но опознание не «на слово»: запись обязана нести полную каноническую
    карту (`files`), её хеш обязан из карты восстанавливаться, а отличия от текущей
    карты — быть разложены по трём классам:

      * `files_excluded` — пересобираемый байткод, выпавший из карты;
      * `files_doc_delta` — документы КОРНЯ набора (описание, не измерение);
      * `files_measured_delta` — измеренные артефакты задач и мока.

    Классы не косметика: доказательство опознаётся как «прежнее» только пока
    измеренные артефакты не менялись. Изменение задачи обязано краснеть
    (`evidence-stale`), а не выглядеть «прежней идентичностью».

    Возвращает `(опознаваемые идентичности, проблемы, детали)`.
    """
    entries: list[tuple[str, dict]] = []
    if card_doc.get("previous_sha256_full"):
        entries.append(("previous", _read_entry(card_doc, "previous_")))
    for i, item in enumerate(card_doc.get("previous_history") or []):
        if isinstance(item, dict):
            entries.append((f"history[{i}]", _read_entry(item)))
    problems: list[str] = []
    detail: dict = {}
    accepted: dict[str, dict] = {}
    for label, entry in entries:
        p, d = _check_entry(label, entry, files)
        problems += p
        if not d:
            continue
        if label == "previous":
            detail = d
        else:
            detail.setdefault("history", {})[label] = d
        if d.get("acceptable_for_evidence"):
            accepted[entry["sha256_full"]] = d
    return accepted, problems, detail



def ground_artifacts(root: pathlib.Path, set_dir: pathlib.Path, evidence: pathlib.Path) -> dict:
    pool_files = sorted(str(p.relative_to(root)) for p in root.glob("datasets/rl_tasks_grounded*.jsonl"))
    return {
        "stage_pool": pool_files,
        "skeleton_set": set_dir.is_dir(),
        "skeleton_evidence": evidence.is_file(),
    }


def check_guard(root: pathlib.Path, set_dir: pathlib.Path, evidence: pathlib.Path) -> tuple[int, list[dict], dict]:
    findings: list[dict] = []
    detail: dict = {}

    def fail(rule: str, message: str) -> None:
        findings.append({"severity": "error", "rule": rule, "message": message})

    canon, files = set_digest(set_dir)
    detail["set_sha256_full_recomputed"] = canon
    card = set_dir / "card.json"
    accepted: dict[str, dict] = {}
    prev_sha: str | None = None
    if card.is_file():
        card_doc = json.loads(card.read_text(encoding="utf-8"))
        detail["card_schema_ok"] = card_doc.get("schema") == SET_SCHEMA
        if card_doc.get("sha256_full") != canon:
            fail("card-stale", f"карточка набора устарела: sha256_full {card_doc.get('sha256_full')} != пересчитанный {canon}")
        # Цепочка идентичностей (ADR-028 п.1): смена содержательной части не
        # объявляет прежнюю запись устаревшей — но и не принимается на слово.
        accepted, prev_problems, prev_detail = previous_identity(card_doc, files)
        for problem in prev_problems:
            fail("card-previous-inconsistent", problem)
        if prev_detail:
            detail["previous_identity"] = prev_detail
            prev_sha = prev_detail.get("previous_sha256_full")
        detail["card_n_tasks"] = card_doc.get("n_tasks")
        detail["card_identity_history"] = [h.get("sha256_full") for h in card_doc.get("previous_history") or []]

    doc = json.loads(evidence.read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        return 2, [], {"error": f"схема доказательства {doc.get('schema')!r} вместо {SCHEMA!r}"}
    detail["evidence_generated_at"] = doc.get("generated_at")
    detail["evidence_status"] = doc.get("status")

    recorded_set = doc.get("set", {}).get("sha256_full")
    if recorded_set == canon:
        detail["evidence_identity"] = "current"
    elif recorded_set in accepted:
        detail["evidence_identity"] = "previous"
        entry = accepted[recorded_set]
        delta = entry.get("previous_files_doc_delta") or []
        detail["evidence_identity_note"] = (
            "доказательство записано под прежней идентичностью набора "
            f"({str(recorded_set)[:12]}…); содержательная часть набора та же"
            + (f", отличается только документ корня набора: {', '.join(delta)}" if delta else "")
        )
    else:
        fail("evidence-stale", "доказательство описывает не текущий набор: sha256_full расходится — прогони tools/grounded_runner.py")

    if doc.get("status") != "ok":
        fail("evidence-status", f"прогон скелета завершился со статусом {doc.get('status')!r}: {doc.get('status_note')!r}")

    for task in doc.get("tasks", []):
        tid = task.get("id", "?")
        res = task.get("res", {})
        gates = task.get("gates", {})
        if not res:
            fail("no-result", f"{tid}: в доказательстве нет ни одного прогона")
            continue
        run = res.get("run") or {}
        na = res.get("no-action") or {}
        ea = res.get("empty-action") or {}
        if na.get("score") != 0:
            fail("success-without-tool-call", f"{tid}: проба no-action дала {na.get('score')!r} вместо 0 — задача не заземлена")
        if ea.get("score") != 0:
            fail("empty-action", f"{tid}: вырожденная проба «действие без последствий» дала {ea.get('score')!r} вместо 0")
        if run.get("score") != 1:
            fail("environment-blocks-actions", f"{tid}: положительный контроль дал {run.get('score')!r} вместо 1 — среда не пропускает действия (ADR-049 п.6)")
        leak = gates.get("leak", {})
        for key in ("gold_in_prompt", "gold_in_id", "gold_in_seed", "mock_response_has_gold"):
            if leak.get(key) is True:
                fail("gold-leak", f"{tid}: {key} = true (ADR-049 п.9/п.3(ii))")
        for g in ("G1", "G2", "G3", "G4"):
            if gates.get(g, {}).get("pass") is not True:
                fail("gate", f"{tid}: {g} не подтверждён доказательством")
        verifier = set_dir / "tasks" / tid / task.get("spec", {}).get("verifier", "")
        recorded = gates.get("G3", {}).get("verifier_sha256")
        if verifier.is_file() and recorded and sha256_file(verifier) != recorded:
            fail("verifier-drift", f"{tid}: верификатор изменился после прогона — доказательство относится к прежней версии")
        detail.setdefault("tasks", {})[tid] = {
            "no_action": na.get("score"),
            "empty_action": ea.get("score"),
            "run": run.get("score"),
        }
    detail["n_tasks"] = len(doc.get("tasks", []))
    detail["no_action_share"] = doc.get("no_action", {}).get("share")
    if doc.get("no_action", {}).get("share") not in (0.0, 0, None):
        fail("no-action-share", f"доля успехов без действий {doc['no_action']['share']} > 0 (ADR-049 п.5)")
    return (1 if findings else 0), findings, detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Страж заземления (ADR-049 п.5/п.11)")
    ap.add_argument("--root", default=".", help="корень кейса")
    ap.add_argument("--set-dir", default="", help="каталог набора (по умолчанию <root>/data/grounded-skeleton)")
    ap.add_argument("--evidence", default="", help="файл доказательства (по умолчанию <root>/evidence/grounded-skeleton.json)")
    ap.add_argument("--mode", choices=("guard", "pending"), default="guard")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    set_dir = pathlib.Path(args.set_dir).resolve() if args.set_dir else root / "data" / "grounded-skeleton"
    evidence = pathlib.Path(args.evidence).resolve() if args.evidence else root / "evidence" / "grounded-skeleton.json"

    art = ground_artifacts(root, set_dir, evidence)
    art["stage_pool_note"] = (
        "пул стадии (datasets/rl_tasks_grounded*.jsonl) не объявлен — правило п.5 применяется к скелету; "
        "появление пула расширит проверку без изменения правила"
    )
    out: dict = {"mode": args.mode, "artifacts": art}

    if args.mode == "pending":
        empty = not art["skeleton_set"] and not art["skeleton_evidence"] and not art["stage_pool"]
        rc = 1 if empty else 0
        msg = (
            "PENDING-EVIDENCE: заземлённого артефакта нет (ни скелета, ни пула стадии) — правило п.5 не применяется, "
            "красное здесь было бы ложным (ADR-023 п.12)"
            if empty
            else "заземлённый артефакт есть — состояние PENDING снято, действует правило-страж"
        )
        out.update({"rc": rc, "message": msg, "findings": []})
    elif not art["skeleton_set"]:
        # Доказательство есть, а набора, о котором оно говорит, нет: проверять
        # нечего — это NOT-VERIFIED, а не зелёный (молчание не доказательство).
        out.update({"rc": 2, "findings": [], "message": "NOT-VERIFIED: доказательство есть, а набора нет — проверять нечего"})
    else:
        if not evidence.is_file():
            out.update({"rc": 0, "message": "заземлённого артефакта нет: красное по п.5 не срабатывает (см. --mode pending)", "findings": []})
        else:
            rc, findings, detail = check_guard(root, set_dir, evidence)
            out.update({"rc": rc, "findings": findings, "detail": detail})
            out["message"] = (
                "нарушений нет: успехов без tool_call 0, доказательство описывает текущий набор"
                if rc == 0
                else f"КРАСНОЕ: {len(findings)} нарушений (ADR-049 п.5)"
            )
            # Опознание по прежней идентичности называется вслух: молчаливый зелёный
            # после смены политики читался бы как «ничего не менялось».
            if rc == 0 and detail.get("evidence_identity_note"):
                out["message"] += f"; {detail['evidence_identity_note']}"
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(out["message"])
        for f in out.get("findings", []):
            print(f"  - [{f['severity']}] {f['rule']}: {f['message']}")
    return out["rc"]


if __name__ == "__main__":
    sys.exit(main())
