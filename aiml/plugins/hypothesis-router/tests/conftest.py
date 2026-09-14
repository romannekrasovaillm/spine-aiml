"""Общие фикстуры тестов роутера гипотез.

Тесты герметичны: реестр карточек, проект и handoff-пакет собираются в
`tmp_path`; `~/hypotheses`, сеть и внешние сервисы не нужны. Карточки в
реестре не меняются — тест на это есть в `test_card_match.py`.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "hypothesis-card" / "scripts"
CARD_MATCH = SCRIPTS / "card_match.py"
HOOK = PLUGIN / "hooks" / "hypothesis_hook.py"

sys.path.insert(0, str(SCRIPTS))
import card_match  # noqa: E402
import cardio  # noqa: E402

# Переменные окружения, влияющие на роутинг: тест задаёт их сам, окружение
# разработчика не должно протекать в прогон.
ROUTING_ENV = ("HYPOTHESES_DIR", "HYPOTHESES_MAX_CARDS", "HYPOTHESIS_DEDUPE",
               "HYPOTHESIS_MIN_SCORE", "HYPOTHESIS_CARD_SCRIPTS", "HYPOTHESIS_ANNOTATE",
               "HYPOTHESIS_PLUGIN")

CARD_TEMPLATE = """---
id: {cid}
title: "{title}"
state: {state}
created: 2026-09-14
triggers:
  files: [{files}]
  keys: [{keys}]
  deps: [{deps}]
  words: [{words}]
skills: [{skills}]
plugins: []
projects: []
related: []
---

## Намерение
{title}
"""


def write_card(root, cid, title=None, state="latent", files=(), keys=(), deps=(), words=(),
               skills=(), dirname=None):
    """Кладёт карточку в `<root>/<dirname|id>/HYPOTHESIS.md`.

    `dirname` задаётся отдельно там, где важно, что сортировка идёт по `id`,
    а не по порядку обхода каталогов.
    """
    d = Path(root) / (dirname or cid)
    d.mkdir(parents=True, exist_ok=True)
    text = CARD_TEMPLATE.format(cid=cid, title=title or f"карточка {cid}", state=state,
                                files=", ".join(files), keys=", ".join(keys),
                                deps=", ".join(deps), words=", ".join(words),
                                skills=", ".join(skills))
    path = d / "HYPOTHESIS.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_project(root, files=(), config=None, deps=None, docs=None):
    """Проект-факты: файлы (по basename), ключи конфига, зависимости, README."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name in files:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    if config:
        (root / "config.yaml").write_text(config, encoding="utf-8")
    if deps:
        (root / "requirements.txt").write_text(deps, encoding="utf-8")
    (root / "README.md").write_text(docs or "", encoding="utf-8")
    return root


def hook_env(registry, extra=None):
    """Окружение хука: чистое от внешних HYPOTHESIS_*/HYPOTHESES_*, с реестром."""
    env = {k: v for k, v in os.environ.items() if k not in ROUTING_ENV}
    env["HYPOTHESES_DIR"] = str(registry)
    env.update(extra or {})
    return env


def run_card_match(registry, *args):
    """Прогон card_match.py как CLI (то, что проверяют критерии K1–K3, K6, K8)."""
    cmd = [sys.executable, str(CARD_MATCH), "--dir", str(registry), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=hook_env(registry))


def run_hook(registry, event, *args, env=None):
    """Прогон хука как CLI; окружение — только заданное тестом."""
    cmd = [sys.executable, str(HOOK), event, *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=hook_env(registry, env))


CARD_LINE_RE = re.compile(r"^  \[\s*(\d+)\]\s+(\S+)")


def raised_lines(stdout):
    """(балл, id) поднятых карточек из текстового отчёта (строка `  [ 3] id — …`)."""
    out = []
    for line in stdout.splitlines():
        m = CARD_LINE_RE.match(line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
    return out


def raised_ids(stdout):
    """id поднятых карточек из текстового отчёта."""
    return [cid for _, cid in raised_lines(stdout)]


def raised_scores(stdout):
    """Баллы поднятых карточек из текстового отчёта."""
    return [score for score, _ in raised_lines(stdout)]


def json_hits(stdout):
    """Попадания из `--json` (список) и `--json-full` (объект) — одной функцией."""
    import json

    data = json.loads(stdout)
    return data["hits"] if isinstance(data, dict) else data


@pytest.fixture
def registry(tmp_path):
    """Пустой реестр карточек в tmp_path."""
    root = tmp_path / "hypotheses"
    root.mkdir()
    return root


@pytest.fixture
def project(tmp_path):
    """Проект-факты: moe.py, infer.py, config.yaml с top_k, requirements.txt."""
    return write_project(tmp_path / "project", files=["moe.py", "infer.py"],
                         config="top_k: 8\nother_key: 1\n",
                         deps="torch==2.6.0\nflash-attn==2.7.0\n",
                         docs="прогон претрейна на GB10\n")


@pytest.fixture
def handoff(tmp_path):
    """Каталог handoff-пакета с TASK.md (хук читает его как текст намерения)."""
    d = tmp_path / "packet" / ".arch-handoff"
    d.mkdir(parents=True)
    (d / "TASK.md").write_text("# Задача\n\nПоднять пайплайн претрейна на GB10.\n", encoding="utf-8")
    return d
