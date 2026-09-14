#!/usr/bin/env python3
"""Создать карточку гипотезы.

    python3 card_new.py --dir ~/hypotheses --id laguna-gb10-ladder \
        --title "Лесенка маленьких моделей на GB10" --state latent \
        --source "Отчёт v12, 13.09.2026" [--source-path file.docx] \
        --trigger-file 'rollouts_log.jsonl' --trigger-key LAGUNA_MEM_FRACTION \
        --trigger-dep vllm --trigger-word GB10 \
        --skill laguna-ladder-run --plugin laguna-gb10-skills \
        --promote-when "есть бокс GB10 и задача ≤3B" [--related other-id] [--force]

Создаёт <dir>/<id>/HYPOTHESIS.md с фронтматтером и заготовкой тела.
Без единого триггера отказывается (карточка без триггеров — заметка).
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cardio  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--state", default="latent", choices=cardio.STATES)
    ap.add_argument("--source", default="")
    ap.add_argument("--source-path", default="")
    ap.add_argument("--owner", default="me")
    ap.add_argument("--trigger-file", action="append", default=[])
    ap.add_argument("--trigger-key", action="append", default=[])
    ap.add_argument("--trigger-dep", action="append", default=[])
    ap.add_argument("--trigger-word", action="append", default=[])
    ap.add_argument("--skill", action="append", default=[])
    ap.add_argument("--plugin", action="append", default=[])
    ap.add_argument("--related", action="append", default=[])
    ap.add_argument("--promote-when", default="")
    ap.add_argument("--decay-months", type=int, default=6)
    ap.add_argument("--force", action="store_true", help="перезаписать существующую")
    a = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.id):
        sys.exit("id: только строчные латинские буквы, цифры и дефис")
    triggers = {"files": a.trigger_file, "keys": a.trigger_key, "deps": a.trigger_dep, "words": a.trigger_word}
    if not any(triggers.values()):
        sys.exit("нужен хотя бы один триггер (--trigger-file/-key/-dep/-word): без триггеров карточка не всплывёт")
    if not (a.trigger_file or a.trigger_key) and a.state != "fleeting":
        print("! только словесные триггеры — карточку поднимет лишь совпадение слов в намерении; "
              "добавьте файл или ключ конфига, если известны")

    root = os.path.expanduser(a.dir)
    cdir = os.path.join(root, a.id)
    path = os.path.join(cdir, "HYPOTHESIS.md")
    if os.path.exists(path) and not a.force:
        sys.exit(f"уже есть: {path} (используйте --force или card_review.py для правок)")
    os.makedirs(cdir, exist_ok=True)

    t = cardio.today()
    data = {
        "id": a.id, "title": a.title, "state": a.state, "created": t, "last_touched": t,
        "source": a.source, "source_path": a.source_path, "owner": a.owner,
        "triggers": triggers, "skills": a.skill, "plugins": a.plugin, "projects": [],
        "related": a.related, "promote_when": a.promote_when, "decay_months": a.decay_months,
    }
    derived = "\n".join(f"- {s} — <что делает>" for s in a.skill) or "- <скилл/плагин — что делает>"
    body = cardio.BODY_TEMPLATE.format(
        derived=derived, today=t,
        source_note=f" из источника: {a.source}" if a.source else "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cardio.dump(data, body))
    print(f"создана: {path}")
    print("допишите тело: Намерение, Почему пригодится, Что сделало бы проектом, Открытые вопросы")


if __name__ == "__main__":
    main()
