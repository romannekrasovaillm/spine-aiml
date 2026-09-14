"""Чтение/запись HYPOTHESIS.md без внешних зависимостей.

Поддерживается подмножество YAML: скаляры, строки в кавычках, плоские
списки в квадратных скобках, вложенный блок `triggers:` с теми же списками,
список словарей `projects:` в inline-форме [{name: x, date: y}].
"""
import glob
import json
import os
import re
from datetime import date

STATES = ("fleeting", "latent", "active", "project", "archived")
TRIGGER_KINDS = ("files", "keys", "deps", "words")
WEIGHTS = {"files": 3, "keys": 3, "deps": 2, "words": 1}
LIST_FIELDS = ("skills", "plugins", "related")

BODY_TEMPLATE = """
## Намерение
<одна-две фразы: что хочу уметь/понять и зачем>

## Почему это может пригодиться
<сценарии; чем отличается от того, что уже есть>

## Что сделало бы это проектом
<условие перехода latent → project>

## Что выведено
{derived}

## Открытые вопросы
- 

## Журнал
- {today} — заведена{source_note}.
"""


def _parse_scalar(s):
    s = s.strip()
    if s.startswith('"') and s.endswith('"') or s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    if s.lower() in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _parse_list(s):
    s = s.strip()
    if not s.startswith("["):
        return [_parse_scalar(s)] if s else []
    inner = s[1:-1].strip()
    if not inner:
        return []
    if inner.startswith("{"):
        # список inline-словарей
        out = []
        for m in re.finditer(r"\{([^}]*)\}", inner):
            d = {}
            for kv in m.group(1).split(","):
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    d[k.strip()] = _parse_scalar(v)
            out.append(d)
        return out
    parts = re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', inner)
    return [_parse_scalar(p) for p in parts if p.strip()]


def parse(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        raise ValueError("нет фронтматтера ---")
    fm, body = m.group(1), m.group(2)
    data, current_block = {}, None
    for line in fm.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, val = line.strip().partition(":")
        key = key.strip()
        if indent == 0:
            current_block = None
            if val.strip() == "":
                data[key] = {}
                current_block = key
            elif val.strip().startswith("["):
                data[key] = _parse_list(val)
            else:
                data[key] = _parse_scalar(val)
        elif current_block:
            data[current_block][key] = _parse_list(val) if val.strip().startswith("[") else _parse_scalar(val)
    data.setdefault("triggers", {})
    for k in TRIGGER_KINDS:
        data["triggers"].setdefault(k, [])
    for k in LIST_FIELDS:
        data.setdefault(k, [])
    data.setdefault("projects", [])
    return data, body


def _dump_list(lst):
    def q(v):
        v = str(v)
        return f'"{v}"' if re.search(r"[,:#\s\[\]{}*?]", v) or v == "" else v
    if lst and isinstance(lst[0], dict):
        return "[" + ", ".join("{" + ", ".join(f"{k}: {q(v)}" for k, v in d.items()) + "}" for d in lst) + "]"
    return "[" + ", ".join(q(v) for v in lst) + "]"


def dump(data, body):
    order = ["id", "title", "state", "created", "last_touched", "source", "source_path", "owner",
             "triggers", "skills", "plugins", "projects", "related", "promote_when", "decay_months"]
    keys = order + [k for k in data if k not in order]
    lines = ["---"]
    for k in keys:
        if k not in data:
            continue
        v = data[k]
        if k == "triggers":
            lines.append("triggers:")
            for t in TRIGGER_KINDS:
                lines.append(f"  {t}: {_dump_list(v.get(t, []))}")
        elif isinstance(v, list):
            lines.append(f"{k}: {_dump_list(v)}")
        elif v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, int):
            lines.append(f"{k}: {v}")
        else:
            s = str(v)
            lines.append(f'{k}: "{s}"' if re.search(r"[:#\[\]{}]", s) or s != s.strip() else f"{k}: {s}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.lstrip("\n")


def load_all(root, include_archived=False):
    cards = []
    for path in sorted(glob.glob(os.path.join(os.path.expanduser(root), "*", "HYPOTHESIS.md"))):
        with open(path, encoding="utf-8") as f:
            try:
                data, body = parse(f.read())
            except ValueError as e:
                print(f"! {path}: {e}")
                continue
        data["_path"] = path
        data["_body"] = body
        if data.get("state") == "archived" and not include_archived:
            continue
        cards.append(data)
    return cards


def save(card):
    path = card["_path"]
    body = card["_body"]
    data = {k: v for k, v in card.items() if not k.startswith("_")}
    with open(path, "w", encoding="utf-8") as f:
        f.write(dump(data, body))


def today():
    return date.today().isoformat()


def to_json(card):
    return json.dumps({k: v for k, v in card.items() if not k.startswith("_")}, ensure_ascii=False, indent=1)
