#!/usr/bin/env python3
"""Роутинг карточек по фактам проекта и/или тексту намерения.

    python3 card_match.py --dir ~/hypotheses --project ~/work/repo \
        [--intent "поднять RL для 3B на Spark"] [--min-score 2] [--json] [--include-archived]
    python3 card_match.py --dir ~/hypotheses --emit-routing routing.json

Факты проекта:
  files — имена файлов в дереве проекта (glob по basename и относительному пути)
  keys  — ключи в конфигах/env: *.yaml *.yml *.toml *.json *.env *.cfg *.ini *.sh Makefile
  deps  — зависимости: requirements*.txt pyproject.toml package.json Cargo.toml go.mod environment.yml
  words — слова в --intent и в README*/AGENTS.md/CLAUDE.md проекта (без учёта регистра)
Балл = сумма весов уникальных совпавших триггеров (files 3, keys 3, deps 2, words 1).

Подъём:
  * порядок — балл убыв., при равенстве id по возрастанию (повторные прогоны
    дают тот же порядок). norm в порядке НЕ участвует: он сломал бы порядок
    равных баллов в выводе без новых флагов (K6) и правило «при равном балле —
    id по возрастанию» (K2); детерминизм обеспечивает уникальность id;
  * norm = score / max_possible, где
    max_possible = 3*|files| + 3*|keys| + 2*|deps| + 1*|words| по триггерам карточки
    (потолок балла: все триггеры совпали), округление — 4 знака;
  * --max-cards N — поднять не больше N карточек (0, по умолчанию, — без ограничения);
  * --dedupe J — не поднимать карточку, если множество её совпавших триггеров даёт
    Jaccard ≥ J с уже поднятой (0, по умолчанию, — выключено); подавленная
    уходит в suppressed отчёта и --json-full, а не в набор.

Без флагов --max-cards/--dedupe набор и порядок карточек прежние; в строках
отчёта и в --json добавлен только norm.
"""
import argparse
import fnmatch
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cardio  # noqa: E402

CONFIG_EXT = (".yaml", ".yml", ".toml", ".json", ".env", ".cfg", ".ini", ".sh")
CONFIG_NAMES = ("Makefile", ".env")
DEP_FILES = ("pyproject.toml", "package.json", "Cargo.toml", "go.mod", "environment.yml", "setup.py")
DOC_NAMES = ("README", "AGENTS.md", "CLAUDE.md", "ARCHITECTURE")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "checkpoints_cache"}
MAX_FILE = 512_000


def read(path):
    try:
        if os.path.getsize(path) > MAX_FILE:
            return ""
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def scan_project(root):
    root = os.path.expanduser(root)
    files, config_text, dep_text, doc_text = [], [], [], []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dp, root)
        for fn in fns:
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            files.append(rel)
            full = os.path.join(dp, fn)
            low = fn.lower()
            if low.endswith(CONFIG_EXT) or fn in CONFIG_NAMES:
                config_text.append(read(full))
            if fn in DEP_FILES or low.startswith("requirements"):
                dep_text.append(read(full))
            if any(fn.startswith(d) for d in DOC_NAMES):
                doc_text.append(read(full))
    return {"files": files, "config": "\n".join(config_text),
            "deps": "\n".join(dep_text).lower(), "docs": "\n".join(doc_text)}


def match_card(card, facts, intent):
    hits = {k: [] for k in cardio.TRIGGER_KINDS}
    tr = card["triggers"]
    if facts:
        for pat in tr["files"]:
            if any(fnmatch.fnmatch(os.path.basename(f), pat) or fnmatch.fnmatch(f, pat) for f in facts["files"]):
                hits["files"].append(pat)
        for key in tr["keys"]:
            if re.search(r"(?<![\w.])" + re.escape(str(key)) + r"(?![\w])", facts["config"]):
                hits["keys"].append(key)
        for dep in tr["deps"]:
            if re.search(r"(?<![\w-])" + re.escape(str(dep).lower()) + r"(?![\w])", facts["deps"]):
                hits["deps"].append(dep)
    text = ((intent or "") + "\n" + (facts["docs"] if facts else "")).lower()
    for w in tr["words"]:
        if re.search(r"(?<!\w)" + re.escape(str(w).lower()) + r"(?!\w)", text):
            hits["words"].append(w)
    score = sum(cardio.WEIGHTS[k] * len(v) for k, v in hits.items())
    return score, hits


def max_possible(card):
    """Потолок балла карточки: все её триггеры совпали.

    3*|files| + 3*|keys| + 2*|deps| + 1*|words| — те же веса, что в балле
    (`cardio.WEIGHTS`), поэтому norm ∈ [0, 1] и не зависит от размера реестра.
    """
    tr = card.get("triggers") or {}
    return sum(cardio.WEIGHTS[k] * len(tr.get(k) or []) for k in cardio.TRIGGER_KINDS)


def norm_of(score, card):
    """Нормированный балл карточки (4 знака). Карточка без триггеров — 0.0."""
    total = max_possible(card)
    return round(score / total, 4) if total else 0.0


def matched_set(hits):
    """Совпавшие триггеры как множество (вид, значение) — идентичность как в routing.json."""
    return {(kind, str(v)) for kind, vals in hits.items() for v in vals}


def jaccard(a, b):
    """Jaccard двух множеств совпавших триггеров; два пустых — 0.0 (не перекрытие)."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def select(results, max_cards=0, dedupe=0.0):
    """Порядок, диверсификация по перекрытию и ограничение числа поднятых.

    results — список (score, card, hits) с баллом ≥ порога.
    Возвращает (raised, suppressed): поднятые (score, card, hits) и подавленные
    [{"card", "suppressed_by", "jaccard"}]. Порядок — балл убыв., id возр.;
    карточка с перекрытием ≥ dedupe уступает уже поднятой (у неё балл не меньше,
    при равенстве — меньший id), но всегда остаётся в suppressed.
    """
    ordered = sorted(results, key=lambda r: (-r[0], r[1]["id"]))
    raised, kept, suppressed = [], [], []
    for score, card, hits in ordered:
        trig = matched_set(hits)
        rival = None
        if dedupe > 0:
            for kept_card, kept_trig in kept:
                j = jaccard(trig, kept_trig)
                if j >= dedupe:
                    rival = (kept_card["id"], round(j, 4))
                    break
        if rival:
            suppressed.append({"card": card["id"], "suppressed_by": rival[0], "jaccard": rival[1]})
            continue
        kept.append((card, trig))
        raised.append((score, card, hits))
    if max_cards > 0:
        raised = raised[:max_cards]
    return raised, suppressed


def hit_obj(score, card, hits):
    """Проекция карточки для --json / --json-full."""
    return {"card": card["id"], "title": card["title"], "state": card["state"], "score": score,
            "norm": norm_of(score, card), "hits": hits, "skills": card["skills"],
            "plugins": card["plugins"], "promote_when": card.get("promote_when")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--project", default=None)
    ap.add_argument("--intent", default=None)
    ap.add_argument("--min-score", type=int, default=2)
    ap.add_argument("--max-cards", type=int, default=0,
                    help="поднять не больше N карточек (0 — без ограничения)")
    ap.add_argument("--dedupe", type=float, default=0.0,
                    help="Jaccard-порог перекрытия совпавших триггеров (0 — выключено)")
    ap.add_argument("--include-archived", action="store_true")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--json-full", action="store_true",
                     help="JSON-объект {hits, suppressed} вместо списка попаданий")
    ap.add_argument("--emit-routing", default=None, help="выгрузить плоскую таблицу триггеров в JSON")
    a = ap.parse_args()
    if a.max_cards < 0:
        ap.error("--max-cards не может быть отрицательным")
    if not 0.0 <= a.dedupe <= 1.0:
        ap.error("--dedupe вне диапазона [0, 1]")

    cards = cardio.load_all(a.dir, include_archived=a.include_archived)
    if not cards:
        sys.exit("карточек не найдено")

    if a.emit_routing:
        rows = []
        for c in cards:
            for kind in cardio.TRIGGER_KINDS:
                for t in c["triggers"][kind]:
                    rows.append({"trigger": t, "kind": kind, "weight": cardio.WEIGHTS[kind],
                                 "card": c["id"], "state": c["state"], "skills": c["skills"]})
        with open(a.emit_routing, "w", encoding="utf-8") as f:
            json.dump({"min_score": a.min_score, "rows": rows}, f, ensure_ascii=False, indent=1)
        print(f"таблица роутинга: {a.emit_routing} ({len(rows)} триггеров, {len(cards)} карточек)")
        return

    if not a.project and not a.intent:
        sys.exit("укажите --project и/или --intent (или --emit-routing)")
    facts = scan_project(a.project) if a.project else None

    results = []
    for c in cards:
        score, hits = match_card(c, facts, a.intent)
        if score >= a.min_score:
            results.append((score, c, hits))
    raised, suppressed = select(results, a.max_cards, a.dedupe)

    if a.json:
        print(json.dumps([hit_obj(s, c, h) for s, c, h in raised], ensure_ascii=False, indent=1))
        return

    if a.json_full:
        print(json.dumps({"min_score": a.min_score, "max_cards": a.max_cards, "dedupe": a.dedupe,
                          "matched": len(results), "hits": [hit_obj(s, c, h) for s, c, h in raised],
                          "suppressed": suppressed}, ensure_ascii=False, indent=1))
        return

    if not results:
        print(f"совпадений с баллом ≥ {a.min_score} нет ({len(cards)} карточек проверено)")
        return
    print(f"Подходящие гипотезы ({len(raised)} из {len(cards)}):")
    for s, c, h in raised:
        why = "; ".join(f"{k}: {', '.join(map(str, v))}" for k, v in h.items() if v)
        print(f"\n  [{s:2}] {c['id']} — {c['title']}  ({c['state']}) norm={norm_of(s, c)}")
        print(f"       по: {why}")
        if c["skills"]:
            print(f"       скиллы: {', '.join(c['skills'])}")
        if c.get("promote_when") and c["state"] in ("fleeting", "latent"):
            print(f"       станет проектом, когда: {c['promote_when']}")
    if suppressed:
        print(f"\nПодавлены перекрытием (Jaccard ≥ {a.dedupe}): {len(suppressed)}")
        for sp in suppressed:
            print(f"  {sp['card']} — перекрытие с {sp['suppressed_by']} = {sp['jaccard']}")


if __name__ == "__main__":
    main()
