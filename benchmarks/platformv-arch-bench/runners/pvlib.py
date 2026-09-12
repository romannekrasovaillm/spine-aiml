#!/usr/bin/env python3
"""pvlib.py — общий парсинг задач/рубрик для прогонов Platform V arch-bench.

Только stdlib. Формат RUBRICS.md слегка различается между авторами:
- fenced yaml-блок с mechanical_checks извлекается ПОСТРОЧНО (закрывающий
  fence = строка ровно ```), т.к. внутри pattern'ов встречаются ``` в теле
  строки (RDS);
- yaml-подмножество парсится вручную (inline-записи {id, pattern, note},
  одиночные/двойные кавычки, escape-последовательности в двойных кавычках);
- у PGL-ARCH-001 блока mechanical_checks нет — подставляется из
  runners/mech_overrides.yaml.
"""
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TASKS_DIR = BASE / "tasks"
OVERRIDES = BASE / "runners" / "mech_overrides.yaml"

# Каталог прогонов (cells/, logs/, results-черновики) — тяжёлые артефакты,
# в git не входят. Переопределяется переменной PVBENCH_RUNS (позволяет
# переиспользовать ячейки, лежащие вне репозитория, и разводить прогоны).
RUNS = Path(os.environ.get("PVBENCH_RUNS", str(BASE / "runs")))
CELLS_DIR = RUNS / "cells"
LOGS_DIR = RUNS / "logs"


def extract_fenced(text, lang=None):
    """Список fenced-блоков (построчно; fence — строка ровно ```<lang> / ```)."""
    blocks, cur, cur_lang = [], None, None
    for line in text.splitlines():
        if cur is None:
            m = re.match(r"^```(\w*)\s*$", line)
            if m:
                cur, cur_lang = [], m.group(1)
        elif line.strip() == "```":
            blocks.append((cur_lang, "\n".join(cur)))
            cur, cur_lang = None, None
        else:
            cur.append(line)
    if lang is None:
        return blocks
    return [b for l, b in blocks if l == lang]


def yaml_unquote(s):
    """Минимальный unquote yaml-скаляра в '...' или \"...\"."""
    s = s.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        body = s[1:-1]
        out, i = [], 0
        esc = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                out.append(esc.get(body[i + 1], "\\" + body[i + 1]))
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)
    return s


def split_inline_map(s):
    """Разбор '{id: D1, pattern: '...'}' с учётом кавычек."""
    s = s.strip()
    assert s.startswith("{") and s.endswith("}"), s[:60]
    s = s[1:-1]
    parts, cur, q, i = [], [], None, 0
    while i < len(s):
        c = s[i]
        if q:
            cur.append(c)
            if c == "\\" and q == '"' and i + 1 < len(s):
                cur.append(s[i + 1]); i += 2; continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c; cur.append(c)
        elif c == ",":
            parts.append("".join(cur)); cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        parts.append("".join(cur))
    d = {}
    for p in parts:
        if ":" in p:
            k, v = p.split(":", 1)
            d[k.strip()] = yaml_unquote(v)
    return d


def parse_mech_yaml(block):
    """Ручной разбор блока mechanical_checks (yaml-подмножество)."""
    mech = {"max_words": None, "deliverables": [], "hard_fails": []}
    section = None
    for raw in block.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-") and section in ("deliverables", "hard_fails"):
            entry = stripped[1:].strip()
            if entry.startswith("{"):
                d = split_inline_map(entry)
                if "id" in d and ("pattern" in d or "note" in d):
                    mech[section].append(d)
            continue
        m = re.match(r"^(\w[\w-]*)\s*:\s*(.*)$", stripped)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key in ("deliverables", "hard_fails"):
                section = key
            elif key == "max_words" and val.isdigit():
                mech["max_words"] = int(val)
            else:
                section = None  # note/notes/прочие ключи закрывают список
    return mech


def load_mech(task):
    """mechanical_checks для задачи; для PGL — из overrides."""
    rub = (TASKS_DIR / task / "RUBRICS.md").read_text(encoding="utf-8")
    for block in extract_fenced(rub, "yaml"):
        if "mechanical_checks" in block:
            return parse_mech_yaml(block), "rubrics"
    if OVERRIDES.is_file():
        ovr = OVERRIDES.read_text(encoding="utf-8")
        m = re.search(rf"(?ms)^## {re.escape(task)}\n(.*?)(?=^## |\Z)", ovr)
        if m:
            for block in extract_fenced(m.group(1), "yaml"):
                if "mechanical_checks" in block:
                    return parse_mech_yaml(block), "override"
    raise RuntimeError(f"{task}: mechanical_checks не найден")


def load_task_files(task):
    d = TASKS_DIR / task
    return {name: (d / f"{name}.md").read_text(encoding="utf-8")
            for name in ("TASK", "CONTEXT", "RUBRICS")}


def extract_deliverables(task_md):
    """Таблица §3 TASK.md: строки '| D1 | описание | обязательно |'."""
    out = []
    for m in re.finditer(r"(?m)^\|\s*(D\d+)\s*\|\s*([^|]+?)\s*\|", task_md):
        out.append((m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()))
    return out


def extract_hard_fails(rubrics_md):
    """Таблица §2 RUBRICS.md: строки '| HF-01 | правило | как обнаружить |'."""
    m = re.search(r"(?ms)^## 2\..*?hard-fail.*?$(.*?)(?=^## )", rubrics_md)
    if not m:
        return []
    out = []
    for row in re.finditer(r"(?m)^\|\s*(HF-\d+[a-z]?)\s*\|\s*([^|]+?)\s*\|", m.group(1)):
        out.append((row.group(1), re.sub(r"\s+", " ", row.group(2)).strip()))
    return out


def extract_criteria_weights(rubrics_md):
    """criteria из frontmatter: - {id: C1, name: ..., weight: 15}."""
    fm = re.search(r"(?ms)^---\n(.*?)\n---", rubrics_md)
    weights = {}
    if fm:
        for m in re.finditer(r"\{id:\s*(C\d+),[^}]*?weight:\s*(\d+)", fm.group(1)):
            weights[m.group(1)] = int(m.group(2))
    return weights


def count_words(md_text):
    """Слова вне fenced-блоков кода и строк таблиц (метод A-02)."""
    words = 0
    in_code = False
    for line in md_text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or line.lstrip().startswith("|"):
            continue
        words += len(re.findall(r"\w+", line, re.UNICODE))
    return words


def list_tasks():
    return sorted(p.name for p in TASKS_DIR.iterdir()
                  if p.is_dir() and (p / "TASK.md").is_file())
