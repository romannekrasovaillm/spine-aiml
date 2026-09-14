#!/usr/bin/env python3
"""Хук роутинга гипотез для харнесса (Spine). Детерминированный, без LLM.

События:
  intent       --text "<намерение>" [--project DIR]
               → поднятые карточки одной строкой + JSON в stdout (--json)
  pre-handoff  --project DIR [--handoff DIR/.arch-handoff] [--annotate]
               → пишет <handoff>/HYPOTHESES.json (hits — только неподавленные,
                 у каждого norm, плюс suppressed), добавляет в MANIFEST.json
                 ключ "hypotheses" и собирает "skills" только с поднятых
                 карточек; --annotate дописывает короткий блок в TASK.md
                 (≤ 400 символов, чтобы не есть epic-context)
  post-accept  --handoff DIR --project-name NAME
               → --touch для карточек из HYPOTHESES.json (факт использования)
  refresh      → пересобирает $HYPOTHESES_DIR/routing.json (таблица триггеров)
  check        --handoff DIR
               → exit 0, если HYPOTHESES.json есть и routing.json не старее карточек;
                 иначе exit 3 с причиной (для fitness-правила command_succeeds)

Окружение:
  HYPOTHESES_DIR   каталог карточек (по умолчанию ~/hypotheses)
  HYPOTHESIS_MIN_SCORE  порог балла (по умолчанию 2)
  HYPOTHESES_MAX_CARDS  сколько карточек поднимать (по умолчанию 8 для
                        pre-handoff и 5 для intent; 0 — без ограничения)
  HYPOTHESIS_DEDUPE     Jaccard-порог перекрытия совпавших триггеров
                        (по умолчанию 0.5; 0 — диверсификация выключена)
Выходные коды: 0 ок, 2 ошибка аргументов/окружения, 3 проверка не пройдена.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.environ.get("HYPOTHESIS_CARD_SCRIPTS") or os.path.join(
    HERE, "..", "skills", "hypothesis-card", "scripts")
HYP_DIR = os.path.expanduser(os.environ.get("HYPOTHESES_DIR", "~/hypotheses"))
MIN_SCORE = int(os.environ.get("HYPOTHESIS_MIN_SCORE", "2"))
ROUTING = os.path.join(HYP_DIR, "routing.json")

# Сколько карточек поднимать по умолчанию и с каким порогом перекрытия
# (HYPOTHESES_MAX_CARDS / HYPOTHESIS_DEDUPE переопределяют).
MAX_CARDS_DEFAULT = {"intent": 5, "pre-handoff": 8}
DEDUPE_DEFAULT = 0.5


def run(script, *args, capture=True):
    cmd = [sys.executable, os.path.join(SCRIPTS, script), *args]
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if r.returncode not in (0,):
        sys.stderr.write(r.stderr or r.stdout or "")
    return r


def env_int(name, default):
    """Целое из окружения; пусто/мусор/отрицательное — default с пометкой в stderr."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        if val < 0:
            raise ValueError("отрицательное")
    except ValueError:
        sys.stderr.write(f"{name}={raw!r}: ожидалось целое ≥ 0, беру {default}\n")
        return default
    return val


def env_float(name, default):
    """Число из окружения; пусто/мусор/вне [0,1] — default с пометкой в stderr."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
        if not 0.0 <= val <= 1.0:
            raise ValueError("вне диапазона")
    except ValueError:
        sys.stderr.write(f"{name}={raw!r}: ожидалось число из [0, 1], беру {default}\n")
        return default
    return val


def selection(event):
    """(max_cards, dedupe) для события: окружение важнее дефолта события."""
    return (env_int("HYPOTHESES_MAX_CARDS", MAX_CARDS_DEFAULT[event]),
            env_float("HYPOTHESIS_DEDUPE", DEDUPE_DEFAULT))


def match(project=None, intent=None, max_cards=0, dedupe=0.0):
    """Отбор карточек: (поднятые, подавленные перекрытием)."""
    args = ["--dir", HYP_DIR, "--min-score", str(MIN_SCORE), "--json-full",
            "--max-cards", str(max_cards), "--dedupe", str(dedupe)]
    if project:
        args += ["--project", project]
    if intent:
        args += ["--intent", intent]
    r = run("card_match.py", *args)
    if r.returncode != 0:
        return [], []
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return [], []
    if isinstance(data, list):
        # card_match без --json-full (чужой/старый путь скриптов): попадания есть,
        # сведений о подавлении нет.
        return data, []
    return data.get("hits") or [], data.get("suppressed") or []


def one_line(hits, suppressed=None):
    if not hits:
        return "гипотезы: совпадений нет"
    parts = [f"{h['card']} [{h['score']}] ({len(h['skills'])} скиллов)" for h in hits]
    line = "гипотезы, пересекающиеся с задачей: " + "; ".join(parts) + " — поднять?"
    if suppressed:
        ids = ", ".join(s["card"] for s in suppressed[:3])
        more = f" и ещё {len(suppressed) - 3}" if len(suppressed) > 3 else ""
        line += f" (подавлено перекрытием: {len(suppressed)} — {ids}{more})"
    return line


def read_task_text(handoff):
    text = []
    for name in ("TASK.md", "ARCHITECTURE.md"):
        p = os.path.join(handoff, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="ignore") as f:
                text.append(f.read()[:20000])
    return "\n".join(text)


def cmd_intent(a):
    if not os.path.isdir(HYP_DIR):
        sys.exit(2)
    max_cards, dedupe = selection("intent")
    hits, suppressed = match(a.project, a.text, max_cards, dedupe)
    if a.json:
        print(json.dumps(hits, ensure_ascii=False))
    else:
        print(one_line(hits, suppressed))
    return 0


def cmd_pre_handoff(a):
    handoff = a.handoff or os.path.join(a.project, ".arch-handoff")
    if not os.path.isdir(handoff):
        sys.stderr.write(f"нет handoff-каталога: {handoff}\n")
        return 2
    max_cards, dedupe = selection("pre-handoff")
    hits, suppressed = match(a.project, read_task_text(handoff), max_cards, dedupe)
    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "hypotheses_dir": HYP_DIR,
               "min_score": MIN_SCORE, "max_cards": max_cards, "dedupe": dedupe,
               "hits": hits, "suppressed": suppressed}
    with open(os.path.join(handoff, "HYPOTHESES.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    manifest_path = os.path.join(handoff, "MANIFEST.json")
    manifest = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            try:
                manifest = json.load(f)
            except ValueError:
                manifest = {}
    manifest["hypotheses"] = []
    for h in hits:
        ref = {"card": h["card"], "state": h["state"], "score": h["score"]}
        if "norm" in h:
            ref["norm"] = h["norm"]
        ref["skills"] = h["skills"]
        manifest["hypotheses"].append(ref)
    # Скиллы пакета — только с поднятых карточек: поднятое сузилось (top-N и
    # диверсификация), а скиллы прошлого, более широкого прогона — уже не факт
    # о задаче (MANIFEST.skills сверяется с routing.json по карточкам-попаданиям).
    skills = sorted({s for h in hits for s in h["skills"]})
    if skills:
        manifest["skills"] = skills
    else:
        manifest.pop("skills", None)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    if a.annotate and hits:
        task = os.path.join(handoff, "TASK.md")
        block = "\n\n## Гипотезы из теплицы\n" + "\n".join(
            f"- {h['card']} → скиллы: {', '.join(h['skills'])}" for h in hits[:3])
        block = block[:400]
        with open(task, "a", encoding="utf-8") as f:
            f.write(block + "\n")
    print(one_line(hits, suppressed))
    return 0


def cmd_post_accept(a):
    p = os.path.join(a.handoff, "HYPOTHESES.json")
    if not os.path.exists(p):
        print("HYPOTHESES.json нет — касаний не записано")
        return 0
    with open(p, encoding="utf-8") as f:
        hits = json.load(f).get("hits", [])
    for h in hits:
        r = run("card_review.py", "--dir", HYP_DIR, "--touch", h["card"], "--project", a.project_name)
        print(r.stdout.strip())
    if hits:
        run("card_match.py", "--dir", HYP_DIR, "--emit-routing", ROUTING)
    return 0


def cmd_refresh(a):
    r = run("card_match.py", "--dir", HYP_DIR, "--emit-routing", ROUTING)
    print(r.stdout.strip())
    return 0 if r.returncode == 0 else 2


def cmd_check(a):
    problems = []
    if not os.path.exists(os.path.join(a.handoff, "HYPOTHESES.json")):
        problems.append("в handoff-пакете нет HYPOTHESES.json (pre-handoff не выполнен)")
    cards = glob.glob(os.path.join(HYP_DIR, "*", "HYPOTHESIS.md"))
    if cards:
        newest = max(os.path.getmtime(c) for c in cards)
        if not os.path.exists(ROUTING):
            problems.append("routing.json отсутствует (выполните refresh)")
        elif os.path.getmtime(ROUTING) < newest:
            problems.append("routing.json старее карточек (выполните refresh)")
    if problems:
        for p in problems:
            print("FAIL: " + p)
        return 3
    print("OK: гипотезы приложены, роутинг актуален")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="event", required=True)
    s = sub.add_parser("intent"); s.add_argument("--text", required=True); s.add_argument("--project"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("pre-handoff"); s.add_argument("--project", required=True); s.add_argument("--handoff"); s.add_argument("--annotate", action="store_true")
    s = sub.add_parser("post-accept"); s.add_argument("--handoff", required=True); s.add_argument("--project-name", required=True)
    sub.add_parser("refresh")
    s = sub.add_parser("check"); s.add_argument("--handoff", required=True)
    a = ap.parse_args()
    if not os.path.isdir(SCRIPTS):
        sys.stderr.write(f"не найдены скрипты hypothesis-card: {SCRIPTS}\n")
        sys.exit(2)
    fn = {"intent": cmd_intent, "pre-handoff": cmd_pre_handoff, "post-accept": cmd_post_accept,
          "refresh": cmd_refresh, "check": cmd_check}[a.event]
    sys.exit(fn(a))


if __name__ == "__main__":
    main()
