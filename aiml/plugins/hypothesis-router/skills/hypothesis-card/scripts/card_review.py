#!/usr/bin/env python3
"""Ревизия карточек и переходы состояний.

    python3 card_review.py --dir ~/hypotheses                       # сводка, залежавшиеся, предложения
    python3 card_review.py --dir ~/hypotheses --touch <id> --project <name>   # записать использование
    python3 card_review.py --dir ~/hypotheses --promote <id> --to active      # явный переход
    python3 card_review.py --dir ~/hypotheses --apply                # применить все предложения (спросит)

Правила предложений:
  fleeting/latent, касаний ≥ 2 разных проектов        → active
  active, нет касаний 12 мес.                          → latent
  fleeting/latent, нет касаний decay_months (по умолч. 6) → archived
Скрипт предлагает; переход выполняется только --promote или --apply.
"""
import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cardio  # noqa: E402


def months_since(d):
    try:
        y, m, dd = map(int, str(d).split("-"))
        t = date.today()
        return (t.year - y) * 12 + (t.month - m) - (1 if t.day < dd else 0)
    except (ValueError, AttributeError):
        return 0


def proposals(card):
    st = card["state"]
    idle = months_since(card.get("last_touched") or card.get("created"))
    projects = {p.get("name") for p in card.get("projects", []) if isinstance(p, dict)}
    decay = card.get("decay_months") or 6
    if st in ("fleeting", "latent") and len(projects) >= 2:
        return "active", f"использована в {len(projects)} проектах"
    if st == "active" and idle >= 12:
        return "latent", f"без касаний {idle} мес."
    if st in ("fleeting", "latent") and idle >= decay:
        return "archived", f"без касаний {idle} мес. (decay {decay})"
    return None, None


def log(card, line):
    body = card["_body"]
    if "## Журнал" in body:
        card["_body"] = body.rstrip("\n") + f"\n- {cardio.today()} — {line}\n"
    else:
        card["_body"] = body.rstrip("\n") + f"\n\n## Журнал\n- {cardio.today()} — {line}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--touch", default=None, help="id карточки, которую использовали")
    ap.add_argument("--project", default=None, help="имя проекта для --touch")
    ap.add_argument("--promote", default=None, help="id карточки для явного перехода")
    ap.add_argument("--to", default=None, choices=cardio.STATES)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--yes", action="store_true", help="не спрашивать при --apply")
    a = ap.parse_args()

    cards = cardio.load_all(a.dir, include_archived=True)
    by_id = {c["id"]: c for c in cards}

    if a.touch:
        c = by_id.get(a.touch) or sys.exit(f"нет карточки {a.touch}")
        if not a.project:
            sys.exit("--touch требует --project <имя>")
        c["projects"] = [p for p in c["projects"] if isinstance(p, dict)]
        c["projects"].append({"name": a.project, "date": cardio.today()})
        c["last_touched"] = cardio.today()
        if c["state"] == "archived":
            c["state"] = "latent"
            log(c, f"поднята из архива использованием в {a.project}")
        log(c, f"использована в проекте {a.project}")
        cardio.save(c)
        print(f"{a.touch}: касание записано (проект {a.project}); состояние {c['state']}")
        p, why = proposals(c)
        if p:
            print(f"  предложение: → {p} ({why}); применить: --promote {a.touch} --to {p}")
        return

    if a.promote:
        c = by_id.get(a.promote) or sys.exit(f"нет карточки {a.promote}")
        if not a.to:
            sys.exit("--promote требует --to <состояние>")
        old = c["state"]
        c["state"] = a.to
        c["last_touched"] = cardio.today()
        log(c, f"состояние {old} → {a.to}")
        cardio.save(c)
        print(f"{a.promote}: {old} → {a.to}")
        if a.to == "active":
            print("  перенесите скиллы карточки из теплицы в доменный ярус библиотеки")
        if a.to == "project":
            print("  добавьте в карточку ссылку на репо проекта (source_path/related)")
        return

    # сводка
    counts = {s: 0 for s in cardio.STATES}
    props = []
    for c in cards:
        counts[c["state"]] = counts.get(c["state"], 0) + 1
        p, why = proposals(c)
        if p:
            props.append((c, p, why))
    print("Карточек: " + ", ".join(f"{s} {n}" for s, n in counts.items() if n))
    for c in sorted(cards, key=lambda x: (cardio.STATES.index(x["state"]), x["id"])):
        idle = months_since(c.get("last_touched") or c.get("created"))
        nproj = len({p.get("name") for p in c.get("projects", []) if isinstance(p, dict)})
        ntrig = sum(len(v) for v in c["triggers"].values())
        warn = "  ! нет триггеров" if ntrig == 0 else ""
        print(f"  {c['state']:9} {c['id']:32} касаний-проектов {nproj}  тишина {idle} мес.  "
              f"триггеров {ntrig}  скиллов {len(c['skills'])}{warn}")
    if props:
        print("\nПредложения переходов:")
        for c, p, why in props:
            print(f"  {c['id']}: {c['state']} → {p}  ({why})")
        if a.apply:
            if not a.yes:
                ans = input("применить все? [y/N] ").strip().lower()
                if ans != "y":
                    return
            for c, p, why in props:
                old = c["state"]
                c["state"] = p
                log(c, f"состояние {old} → {p} (ревизия: {why})")
                cardio.save(c)
            print("применено")
    else:
        print("\nпредложений нет")


if __name__ == "__main__":
    main()
