#!/usr/bin/env python3
"""Страж уникальности номеров правил: номер правила — ресурс канона (ADR-046 п.8).

**Класс дефекта.** Номер правила выдаётся **локальным счётом каталога** в каждом
worktree. Пока ветви работают параллельно, каждая считает занятые номера по своему
файлу — и выдаёт следующий свободный **у себя**, а не в каноне. Дефект в этом кейсе
воспроизводился трижды и каждый раз ловился человеком, а не механизмом:

* ADR-024 — номер решения выдан дважды («процедура приёмки дельт» в `main` и
  «состав CPT-микса v12r50» в ветви), разведён по ADR-046;
* C-020/C-021 — ветвь карточки обучающего набора SFT выдала номера, занятые в
  каноне, уехали на C-023/C-024 (S3z-2);
* C-029 — правило «Доказательство исполнения» получило номер стража сна,
  уехало на C-031 (51ca0a5).

Номер — ресурс **канона**: его выдаёт линия, из которой выдаются дельты (ADR-046
п.8). Дисциплина без механизма здесь не работает по построению: ветвь не видит
чужих номеров до сведения.

**Что проверяется.** Для каждой **незалитой** ветви берётся множество номеров,
которое она **выдала сама** — её номера минус номера её точки ветвления
(`git merge-base`). Красное — если это множество пересекается с номерами канона:
номер уже отдан другому правилу, и сведение либо потеряет правило, либо сделает
номер неоднозначным.

**Второй класс — тот же дефект между кандидатами.** Два незалитых ветви, выдавшие
**один и тот же свободный** номер канона, сталкиваются друг с другом ровно так же,
как ветвь с каноном: в этом кейсе так и вышло — правило «Доказательство исполнения»
и страж сна были выданы номером C-029 **в разных ветвях**, и номер разводил человек
(51ca0a5). Поэтому номер, выданный более чем одной незалитой ветвью, — находка,
даже если канон его ещё не занял.

**Почему «выданные ветвью», а не «все номера ветви».** Ветвь, отставшая от канона,
несёт и старые номера — они не её. Красить их значило бы объявлять находкой сам
факт ветвления: любая ветвь старше последнего правила канона краснела бы всегда
(класс «тест, красный by construction», ADR-023 п.12).

**Почему слитые ветви пропускаются.** Ветвь, чья вершина — предок канона, уже
сведена: её номера по построению в каноне, кандидатом она больше не является
(ADR-024 — приёмка и есть точка, где ветвь перестаёт быть кандидатом). Без этого
страж краснел бы на собственной истории.

**Границы одной строкой** (подробно — ниже): вне проверки остаются правка
`CONSTRAINTS.yaml` в **рабочем дереве**, не доведённая до коммита; **detached HEAD**
(ссылки нет — `git for-each-ref` её не видит, и выданный в ней номер кандидатом не
становится); ветвь **без общей точки ветвления** (`no-merge-base`) — по ней
механически не определяется, что она выдала сама, и она печатается пропуском с
причиной, а не за проверенную.

**Чего страж не проверяет (названные границы, а не умолчание).**

* **Незафиксированную работу** — правку `CONSTRAINTS.yaml` в рабочем дереве, не
  доведённую до коммита: `git for-each-ref` видит ссылки, а не файлы. Ветвь,
  закоммитившая свой номер, проверяется; правка до коммита — нет. Граница названа
  потому, что молчаливый пропуск читался бы как «проверено».
* **Тексты правил** — только номера. Требование «при перенумерации меняется только
  `id`» проверяется сличением полей правила (номера совпали — тексты сверяются
  глазами ревьюера и записью акта), а не этим стражем.
* **Семантику занятости** — правило канона, снятое ветвью, печатается как
  `dropped` (ветвь отстала или сняла правило намеренно) и находкой не является:
  предмет проверки — столкновение номеров, а не расхождение редакций.
* **Ветви с несвязанной историей** (отдельный корень, например публикационная
  редакция): общей точки ветвления нет, и «что ветвь выдала сама» механически не
  определяется. Такая ветвь печатается в списке пропусков с причиной
  `no-merge-base` — кандидатом она не считается, но и за проверенную не выдаётся.

Коды возврата::

    0 — коллизий нет (у каждой незалитой ветви выданные ею номера свободны в каноне)
    1 — коллизия названа поимённо: ветвь, номер, точка ветвления
    2 — NOT-VERIFIED: нет git-репозитория, нет канона или нет файла правил канона

Запуск::

    python3 tools/check_rule_numbers.py                  # гейт кейса
    python3 tools/check_rule_numbers.py --json           # машинный вердикт
    python3 tools/check_rule_numbers.py --canon main     # канон явно
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

RULE_ID = re.compile(r"^\s+- id:\s*(C-\d+)\s*$", re.M)


def git(repo: pathlib.Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return p.returncode, (p.stdout if p.returncode == 0 else p.stderr).strip()


def rule_ids(repo: pathlib.Path, ref: str, path: str) -> set[str] | None:
    """Номера правил в файле `path` на ревизии `ref`; None — файла там нет."""
    rc, out = git(repo, "show", f"{ref}:{path}")
    if rc != 0:
        return None
    return set(RULE_ID.findall(out))


def rule_ids_worktree(case_root: pathlib.Path) -> list[str] | None:
    """Номера правил в рабочем дереве — списком, с кратностями (дубль виден)."""
    f = case_root / "CONSTRAINTS.yaml"
    if not f.is_file():
        return None
    return RULE_ID.findall(f.read_text(encoding="utf-8"))


def duplicates_in(ids: list[str] | None) -> list[str]:
    if not ids:
        return []
    seen: dict[str, int] = {}
    for i in ids:
        seen[i] = seen.get(i, 0) + 1
    return annotate([i for i, n in seen.items() if n > 1])


def branches(repo: pathlib.Path) -> list[tuple[str, str]]:
    rc, out = git(repo, "for-each-ref", "--format=%(refname:short)\t%(objectname)", "refs/heads/")
    if rc != 0:
        return []
    res = []
    for line in out.splitlines():
        if "\t" in line:
            name, tip = line.split("\t", 1)
            res.append((name, tip))
    return res


def annotate(ids: list[str]) -> list[str]:
    return sorted(set(ids), key=lambda s: int(s.split("-")[1]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Страж уникальности номеров правил (ADR-046 п.8)")
    ap.add_argument("--repo", default=None, help="корень git-репозитория (по умолчанию — найденный от кейса)")
    ap.add_argument("--case", default=None, help="корень кейса (по умолчанию — родитель tools/)")
    ap.add_argument("--canon", default="main", help="ревизия канона (по умолчанию main)")
    ap.add_argument("--json", action="store_true", help="машинный вердикт")
    args = ap.parse_args()

    case_root = pathlib.Path(args.case).resolve() if args.case else pathlib.Path(__file__).resolve().parent.parent
    if args.repo:
        repo = pathlib.Path(args.repo).resolve()
    else:
        rc, out = git(case_root, "rev-parse", "--show-toplevel")
        if rc != 0:
            return not_verified(args, "не git-репозиторий (или git недоступен)")
        repo = pathlib.Path(out)

    try:
        path = str((case_root / "CONSTRAINTS.yaml").relative_to(repo))
    except ValueError:
        path = str(case_root / "CONSTRAINTS.yaml")

    canon_ids = rule_ids(repo, args.canon, path)
    if canon_ids is None or not canon_ids:
        return not_verified(args, f"нет файла правил канона ({args.canon}:{path})")

    wt_raw = rule_ids_worktree(case_root)
    duplicates = duplicates_in(wt_raw)

    skips: list[dict] = []
    rows: list[dict] = []
    collisions: list[dict] = []

    for name, tip in branches(repo):
        rc, _ = git(repo, "merge-base", "--is-ancestor", tip, args.canon)
        if rc == 0:
            skips.append({"branch": name, "tip": tip, "reason": "merged-into-canon"})
            continue
        b_ids = rule_ids(repo, name, path)
        if b_ids is None:
            skips.append({"branch": name, "tip": tip, "reason": "no-constraints-of-case"})
            continue
        rc, base = git(repo, "merge-base", args.canon, name)
        if rc != 0:
            skips.append({"branch": name, "tip": tip, "reason": "no-merge-base"})
            continue
        base_ids = rule_ids(repo, base, path) or set()
        issued = annotate([i for i in b_ids if i not in base_ids])
        dropped = annotate([i for i in base_ids if i not in b_ids])
        collided = annotate([i for i in issued if i in canon_ids])
        row = {
            "branch": name,
            "tip": tip,
            "base": base,
            "issued": issued,
            "dropped": dropped,
            "collisions": collided,
        }
        rows.append(row)
        if collided:
            collisions.append(row)

    # Дефект между кандидатами: один и тот же номер выдан более чем одной незалитой ветвью.
    claims: dict[str, list[str]] = {}
    for r in rows:
        for rid in r["issued"]:
            claims.setdefault(rid, []).append(r["branch"])
    pair_collisions = {rid: br for rid, br in claims.items() if len(br) > 1}
    pair_collisions = {k: pair_collisions[k] for k in annotate(pair_collisions)}

    passed = not collisions and not duplicates and not pair_collisions
    verdict = {
        "tool": "check_rule_numbers",
        "passed": passed,
        "canon": args.canon,
        "case": path,
        "canon_rules": len(canon_ids),
        "branches": rows,
        "skipped": skips,
        "duplicates_in_constraints": duplicates,
        "collisions": [
            {"branch": r["branch"], "rules": r["collisions"], "base": r["base"]} for r in collisions
        ],
        "candidate_collisions": pair_collisions,
    }

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=1))
    else:
        print("== номера правил: канон против незалитых ветвей (ADR-046 п.8) ==")
        print(f"канон: {args.canon}:{path} — правил {len(canon_ids)}")
        print(f"ветвей просмотрено: {len(rows) + len(skips)} (кандидатов {len(rows)}, пропущено {len(skips)})")
        for s in skips:
            print(f"  пропуск: {s['branch']} — {s['reason']}")
        for r in rows:
            mark = "КОЛЛИЗИЯ" if r["collisions"] else "ok"
            print(
                f"  [{mark:8}] {r['branch']}: выдала {r['issued'] or '—'}"
                + (f", сняла {r['dropped']}" if r["dropped"] else "")
            )
        if duplicates:
            print(f"дубли номеров в файле правил: {duplicates}")
        if collisions or pair_collisions or duplicates:
            print()
            print(f"НАРУШЕНИЙ: {len(collisions) + len(pair_collisions) + len(duplicates)}")
            for r in collisions:
                for rid in r["collisions"]:
                    print(
                        f"  - {r['branch']}: номер {rid} выдан ветвью от {r['base'][:8]} "
                        f"и УЖЕ ЗАНЯТ в каноне ({args.canon}) — номер выдаёт канон (ADR-046 п.8): "
                        f"правило ветви обязано уехать на следующий свободный, текст не меняется"
                    )
            for rid, brs in pair_collisions.items():
                print(
                    f"  - номер {rid} выдан более чем одной незалитой ветвью: "
                    + ", ".join(sorted(brs))
                    + " — кандидаты столкнулись друг с другом до сведения; разводить по ADR-046 п.8"
                )
            for rid in duplicates:
                print(f"  - дубль {rid} в файле правил рабочего дерева: номер выдан дважды")
            return 1
        print()
        print("ВЕРДИКТ: коллизий нет — выданные ветвями номера свободны в каноне")
    return 0 if passed else 1


def not_verified(args, why: str) -> int:
    v = {"tool": "check_rule_numbers", "passed": None, "verdict": "NOT-VERIFIED", "why": why}
    if args.json:
        print(json.dumps(v, ensure_ascii=False, indent=1))
    else:
        print(f"NOT-VERIFIED: {why}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
