"""Хук гипотез: top-N и диверсификация в HYPOTHESES.json и MANIFEST.json.

Проверяются критерии K4, K5 (и K7 — тесты обязаны быть зелёными): число
поднятых карточек из окружения, поля `norm`/`suppressed`, скиллы пакета
только с поднятых карточек. Карточки берутся из временного реестра.
"""
import ast
import json

from conftest import HOOK, run_hook, write_card, write_project


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def many_cards(registry, n=10):
    """Проект и n непересекающихся карточек: балл 3 у каждой, порядок — по id."""
    project = write_project(registry.parent / "p", files=[f"f{i}.py" for i in range(n)])
    for i in range(n):
        write_card(registry, f"card-{i:02d}", files=[f"f{i}.py"], skills=[f"skill-{i}"])
    return project


def pre_handoff(registry, project, handoff, env=None):
    return run_hook(registry, "pre-handoff", "--project", str(project),
                    "--handoff", str(handoff), env=env)


# ── K4: HYPOTHESES.json ──────────────────────────────────────────────────────

def test_pre_handoff_default_max_cards_8(registry, handoff):
    """K4: по умолчанию pre-handoff поднимает не больше 8 карточек."""
    project = many_cards(registry, 10)
    r = pre_handoff(registry, project, handoff)
    assert r.returncode == 0, r.stderr
    payload = read_json(handoff / "HYPOTHESES.json")
    assert payload["max_cards"] == 8 and payload["dedupe"] == 0.5
    assert [h["card"] for h in payload["hits"]] == [f"card-{i:02d}" for i in range(8)]
    assert payload["suppressed"] == []
    assert all(h["norm"] == 1.0 for h in payload["hits"])


def test_pre_handoff_env_max_cards_override(registry, handoff):
    project = many_cards(registry, 10)
    r = pre_handoff(registry, project, handoff, env={"HYPOTHESES_MAX_CARDS": "3"})
    assert r.returncode == 0, r.stderr
    payload = read_json(handoff / "HYPOTHESES.json")
    assert payload["max_cards"] == 3
    assert [h["card"] for h in payload["hits"]] == ["card-00", "card-01", "card-02"]


def test_pre_handoff_env_dedupe_off(registry, handoff):
    """HYPOTHESIS_DEDUPE=0 выключает диверсификацию; по умолчанию (0.5) — включает."""
    project = write_project(registry.parent / "p", files=["shared.py", "s1.py", "s2.py"])
    write_card(registry, "strong-card", files=["shared.py", "s1.py", "s2.py"], skills=["skill-s"])
    write_card(registry, "weak-card", files=["shared.py", "s1.py"], skills=["skill-w"])
    pre_handoff(registry, project, handoff, env={"HYPOTHESIS_DEDUPE": "0"})
    off = read_json(handoff / "HYPOTHESES.json")
    assert off["dedupe"] == 0.0 and off["suppressed"] == []
    assert [h["card"] for h in off["hits"]] == ["strong-card", "weak-card"]

    pre_handoff(registry, project, handoff)
    on = read_json(handoff / "HYPOTHESES.json")
    assert on["dedupe"] == 0.5
    assert [h["card"] for h in on["hits"]] == ["strong-card"]
    assert on["suppressed"] == [
        {"card": "weak-card", "suppressed_by": "strong-card", "jaccard": 0.6667}]


def test_payload_keeps_consumer_fields(registry, handoff):
    """Поля, которые читают потребители (ядро и card_review), на месте."""
    project = many_cards(registry, 2)
    pre_handoff(registry, project, handoff)
    payload = read_json(handoff / "HYPOTHESES.json")
    assert payload["min_score"] == 2
    assert all({"card", "state", "score", "skills", "norm"} <= set(h) for h in payload["hits"])
    assert isinstance(payload["suppressed"], list)
    assert "гипотезы" in pre_handoff(registry, project, handoff).stdout


def test_no_hits_payload_and_manifest(registry, handoff):
    """Нет попаданий — hits и suppressed пустые списки, скиллов в MANIFEST нет."""
    project = write_project(registry.parent / "p", files=["nobody.py"])
    write_card(registry, "far-card", files=["elsewhere.py"])
    manifest = handoff / "MANIFEST.json"
    manifest.write_text(json.dumps({"created_at": "x", "skills": ["stale-skill"]}), encoding="utf-8")
    r = pre_handoff(registry, project, handoff)
    assert r.returncode == 0, r.stderr
    payload = read_json(handoff / "HYPOTHESES.json")
    assert payload["hits"] == [] and payload["suppressed"] == []
    assert "skills" not in read_json(manifest)


def test_invalid_env_falls_back_to_default(registry, handoff):
    """Мусор в окружении — предупреждение и дефолт события, не падение."""
    project = many_cards(registry, 10)
    r = pre_handoff(registry, project, handoff,
                    env={"HYPOTHESES_MAX_CARDS": "восемь", "HYPOTHESIS_DEDUPE": "2"})
    assert r.returncode == 0, r.stderr
    assert "HYPOTHESES_MAX_CARDS" in r.stderr and "HYPOTHESIS_DEDUPE" in r.stderr
    payload = read_json(handoff / "HYPOTHESES.json")
    assert (payload["max_cards"], payload["dedupe"]) == (8, 0.5)
    assert len(payload["hits"]) == 8


def test_missing_handoff_dir_exits_2(registry):
    assert run_hook(registry, "pre-handoff", "--project", ".", "--handoff", "/nope/nope").returncode == 2


# ── K5: MANIFEST.json ────────────────────────────────────────────────────────

def test_manifest_skills_only_from_raised_cards(registry, handoff):
    """K5: скиллы пакета — объединение скиллов только поднятых карточек."""
    project = many_cards(registry, 10)
    manifest = handoff / "MANIFEST.json"
    manifest.write_text(json.dumps({"created_at": "x", "skills": ["stale-skill"], "task": "t"}),
                        encoding="utf-8")
    pre_handoff(registry, project, handoff)
    payload = read_json(handoff / "HYPOTHESES.json")
    data = read_json(manifest)
    raised = [h["card"] for h in payload["hits"]]
    expected = sorted({s for h in payload["hits"] for s in h["skills"]})
    assert data["skills"] == expected == [f"skill-{i}" for i in range(8)]
    assert "stale-skill" not in data["skills"]
    assert [h["card"] for h in data["hypotheses"]] == raised
    assert {"card", "state", "score", "norm", "skills"} <= set(data["hypotheses"][0])
    assert data["created_at"] == "x" and data["task"] == "t"  # чужие поля целы


# ── intent ───────────────────────────────────────────────────────────────────

def test_intent_default_max_cards_5(registry):
    many_cards(registry, 8)
    r = run_hook(registry, "intent", "--text", "претрейн", "--project", str(registry.parent / "p"))
    assert r.returncode == 0, r.stderr
    for i in range(5):
        assert f"card-{i:02d}" in r.stdout
    for i in range(5, 8):
        assert f"card-{i:02d}" not in r.stdout
    hits = json.loads(run_hook(registry, "intent", "--text", "претрейн",
                               "--project", str(registry.parent / "p"), "--json").stdout)
    assert isinstance(hits, list) and len(hits) == 5
    assert all(h["norm"] == 1.0 for h in hits)


def test_intent_env_override_and_suppressed_note(registry):
    project = write_project(registry.parent / "p", files=["shared.py", "s1.py", "s2.py", "c.py"])
    write_card(registry, "strong-card", files=["shared.py", "s1.py", "s2.py"])
    write_card(registry, "weak-card", files=["shared.py", "s1.py"])
    write_card(registry, "solo-c", files=["c.py"])
    r = run_hook(registry, "intent", "--text", "претрейн", "--project", str(project),
                 env={"HYPOTHESES_MAX_CARDS": "2"})
    assert r.returncode == 0, r.stderr
    raised_part, _, note = r.stdout.partition("(подавлено перекрытием: ")
    assert "strong-card" in raised_part and "solo-c" in raised_part
    assert "weak-card" not in raised_part          # подавленная карточка не поднята
    assert note.startswith("1 — weak-card)")       # …но записана с причиной
    hits = json.loads(run_hook(registry, "intent", "--text", "претрейн", "--project", str(project),
                               "--json", env={"HYPOTHESES_MAX_CARDS": "2"}).stdout)
    assert [h["card"] for h in hits] == ["strong-card", "solo-c"]


def test_hook_imports_no_llm_or_network():
    """§4: хук остаётся детерминированным — без сетевых и LLM-зависимостей."""
    mods = set()
    for node in ast.walk(ast.parse(HOOK.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert mods <= {"argparse", "glob", "json", "os", "subprocess", "sys", "time"}
