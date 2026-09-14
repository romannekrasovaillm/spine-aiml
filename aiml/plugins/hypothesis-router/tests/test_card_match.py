"""card_match: нормированный балл, top-N и диверсификация по перекрытию.

Проверяются механические критерии K1–K3, K6, K8 дельты
`docs/routing-topn.delta.md` и запреты §4 (веса, порог, формат routing.json,
детерминизм, неприкосновенность карточек).
"""
import ast
import json
import re

from conftest import (CARD_MATCH, card_match, cardio, json_hits, raised_ids, raised_scores,
                      run_card_match, write_card, write_project)

NORM_RE = re.compile(r" norm=[0-9.]+$")


def strip_norm(line):
    """Строка отчёта без добавленного дельтой `norm=` — формат до дельты (K6)."""
    return NORM_RE.sub("", line)


# ── нормированный балл ───────────────────────────────────────────────────────

def test_norm_is_score_over_max_possible(registry, project):
    """norm = score / max_possible; потолок — все триггеры карточки."""
    write_card(registry, "narrow-card", files=["moe.py", "gone.py"])  # потолок 6, совпало 3
    write_card(registry, "wide-card", files=["moe.py"])               # потолок 3, совпало 3
    r = run_card_match(registry, "--project", str(project), "--json")
    assert r.returncode == 0, r.stderr
    got = {h["card"]: (h["score"], h["norm"]) for h in json_hits(r.stdout)}
    assert got == {"narrow-card": (3, 0.5), "wide-card": (3, 1.0)}


def test_norm_in_report_lines(registry, project):
    """norm выводится и в текстовом отчёте (не только в --json)."""
    write_card(registry, "wide-card", files=["moe.py"])
    r = run_card_match(registry, "--project", str(project))
    assert "norm=1.0" in r.stdout


def test_max_possible_uses_documented_weights():
    """Потолок = 3*|files| + 3*|keys| + 2*|deps| + 1*|words| (§2.1)."""
    card = {"triggers": {"files": ["a", "b"], "keys": ["c"], "deps": ["d"],
                         "words": ["e", "f", "g"]}}
    assert card_match.max_possible(card) == 3 * 2 + 3 * 1 + 2 * 1 + 1 * 3 == 14
    assert card_match.norm_of(10, card) == round(10 / 14, 4)
    assert card_match.norm_of(0, {"triggers": {}}) == 0.0  # карточка без триггеров


def test_weights_and_default_min_score_unchanged():
    """§4: веса триггеров и порог по умолчанию не менялись."""
    assert cardio.WEIGHTS == {"files": 3, "keys": 3, "deps": 2, "words": 1}
    card = {"triggers": {"files": [], "keys": [], "deps": [], "words": ["одно-слово"]}}
    assert card_match.match_card(card, None, "одно-слово")[0] == 1  # < порога 2


def test_min_score_default_two_is_behavioural(registry, project):
    """Одно совпавшее слово (вес 1) карточку не поднимает; с --min-score 1 — поднимает."""
    write_card(registry, "word-card", words=["претрейн"])
    assert raised_ids(run_card_match(registry, "--intent", "претрейн").stdout) == []
    assert raised_ids(run_card_match(registry, "--intent", "претрейн", "--min-score", "1").stdout) == ["word-card"]


# ── K1/K2: ограничение числа и детерминированный порядок ─────────────────────

def test_max_cards_zero_is_no_limit(registry, project):
    write_card(registry, "top-a", files=["moe.py", "infer.py"])
    write_card(registry, "top-b", files=["moe.py"])
    assert raised_ids(run_card_match(registry, "--project", str(project)).stdout) == ["top-a", "top-b"]
    assert raised_ids(run_card_match(registry, "--project", str(project), "--max-cards", "0").stdout) == ["top-a", "top-b"]


def test_max_cards_keeps_top_by_score(registry):
    """K1: --max-cards N оставляет N карточек в порядке балла по убыванию."""
    write_project(registry.parent / "p", files=["a.py", "b.py", "c.py"])
    write_card(registry, "top-a", files=["a.py", "b.py", "c.py"])   # 9
    write_card(registry, "top-b", files=["b.py", "c.py"])           # 6
    write_card(registry, "top-c", files=["c.py"])                   # 3
    r = run_card_match(registry, "--project", str(registry.parent / "p"), "--max-cards", "2")
    assert raised_ids(r.stdout) == ["top-a", "top-b"]
    assert raised_scores(r.stdout) == [9, 6]


def test_ties_ordered_by_id_and_stable(registry):
    """K2: при равном балле порядок — по id возрастанию и повторяем."""
    write_project(registry.parent / "p", files=["t1.py", "t2.py", "t3.py"])
    # имена каталогов обратны id: порядок обхода дал бы gamma, alpha, beta
    write_card(registry, "beta-card", files=["t1.py"], dirname="z-dir")
    write_card(registry, "alpha-card", files=["t2.py"], dirname="y-dir")
    write_card(registry, "gamma-card", files=["t3.py"], dirname="x-dir")
    args = ("--project", str(registry.parent / "p"), "--max-cards", "3")
    first = run_card_match(registry, *args).stdout
    second = run_card_match(registry, *args).stdout
    assert raised_ids(first) == ["alpha-card", "beta-card", "gamma-card"]
    assert first == second


def test_default_report_is_pre_delta_format_plus_norm(registry, project):
    """K6: без новых флагов набор, порядок и строки те же; добавлен только norm."""
    write_card(registry, "narrow-card", files=["moe.py", "gone.py"])
    r = run_card_match(registry, "--project", str(project))
    assert [strip_norm(line) for line in r.stdout.splitlines()] == [
        "Подходящие гипотезы (1 из 1):",
        "",
        "  [ 3] narrow-card — карточка narrow-card  (latent)",
        "       по: files: moe.py",
    ]


# ── K3: диверсификация по перекрытию ─────────────────────────────────────────

def test_dedupe_suppresses_lower_score_card(registry):
    """K3: карточка с перекрытием ≥ J не поднимается и попадает в suppressed."""
    write_project(registry.parent / "p", files=["shared.py", "s1.py", "s2.py"])
    write_card(registry, "strong-card", files=["shared.py", "s1.py", "s2.py"])  # 9
    write_card(registry, "weak-card", files=["shared.py", "s1.py"])             # 6
    r = run_card_match(registry, "--project", str(registry.parent / "p"),
                       "--dedupe", "0.5", "--json-full")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert [h["card"] for h in data["hits"]] == ["strong-card"]
    assert data["suppressed"] == [
        {"card": "weak-card", "suppressed_by": "strong-card", "jaccard": 0.6667}]


def test_dedupe_reports_pair_and_coefficient_in_text(registry):
    """K3: подавленная видна в отчёте с парой и коэффициентом."""
    write_project(registry.parent / "p", files=["shared.py", "s1.py", "s2.py"])
    write_card(registry, "strong-card", files=["shared.py", "s1.py", "s2.py"])
    write_card(registry, "weak-card", files=["shared.py", "s1.py"])
    r = run_card_match(registry, "--project", str(registry.parent / "p"), "--dedupe", "0.5")
    assert "Подавлены перекрытием (Jaccard ≥ 0.5): 1" in r.stdout
    assert "weak-card — перекрытие с strong-card = 0.6667" in r.stdout


def test_dedupe_tie_keeps_smaller_id(registry):
    """При равном балле остаётся карточка с меньшим id (§2.3); Jaccard = 0.5 — порог включительно."""
    write_project(registry.parent / "p", files=["shared.py", "p1.py", "p2.py", "p3.py"])
    write_card(registry, "dup-a", files=["shared.py", "p1.py", "p2.py"])
    write_card(registry, "dup-b", files=["shared.py", "p1.py", "p3.py"])
    data = json.loads(run_card_match(registry, "--project", str(registry.parent / "p"),
                                     "--dedupe", "0.5", "--json-full").stdout)
    assert [h["card"] for h in data["hits"]] == ["dup-a"]
    assert data["suppressed"] == [{"card": "dup-b", "suppressed_by": "dup-a", "jaccard": 0.5}]


def test_dedupe_off_by_default(registry):
    """По умолчанию (--dedupe 0) поведение прежнее: подавления нет."""
    write_project(registry.parent / "p", files=["shared.py", "s1.py", "s2.py"])
    write_card(registry, "strong-card", files=["shared.py", "s1.py", "s2.py"])
    write_card(registry, "weak-card", files=["shared.py", "s1.py"])
    r = run_card_match(registry, "--project", str(registry.parent / "p"))
    assert raised_ids(r.stdout) == ["strong-card", "weak-card"]
    assert "Подавлены" not in r.stdout
    r = run_card_match(registry, "--project", str(registry.parent / "p"), "--dedupe", "0")
    assert raised_ids(r.stdout) == ["strong-card", "weak-card"]


def test_dedupe_runs_before_max_cards(registry):
    """Диверсификация — до усечения: в top-N не остаётся перекрывающегося дубля."""
    write_project(registry.parent / "p", files=["shared.py", "s1.py", "s2.py", "c.py"])
    write_card(registry, "dup-a", files=["shared.py", "s1.py", "s2.py"])  # 9
    write_card(registry, "dup-b", files=["shared.py", "s1.py"])           # 6
    write_card(registry, "solo-c", files=["c.py"])                        # 3
    data = json.loads(run_card_match(registry, "--project", str(registry.parent / "p"),
                                     "--dedupe", "0.5", "--max-cards", "2", "--json-full").stdout)
    assert [h["card"] for h in data["hits"]] == ["dup-a", "solo-c"]
    assert [s["card"] for s in data["suppressed"]] == ["dup-b"]


def test_trigger_identity_is_kind_and_value(registry):
    """Совпадение по файлу и по ключу с тем же именем — разные триггеры."""
    a = card_match.matched_set({"files": ["config.json"], "keys": [], "deps": [], "words": []})
    b = card_match.matched_set({"files": [], "keys": ["config.json"], "deps": [], "words": []})
    assert card_match.jaccard(a, b) == 0.0
    assert card_match.jaccard(a, a) == 1.0
    assert card_match.jaccard(set(), set()) == 0.0


# ── форма вывода: --json не ломается (K6), --json-full несёт suppressed ──────

def test_json_stays_a_list_of_hits_with_norm(registry, project):
    write_card(registry, "wide-card", files=["moe.py"], skills=["skill-a"])
    data = json.loads(run_card_match(registry, "--project", str(project), "--json").stdout)
    assert isinstance(data, list)
    old_keys = {"card", "title", "state", "score", "hits", "skills", "plugins", "promote_when"}
    assert old_keys <= set(data[0])          # поля потребителей на месте
    assert data[0]["norm"] == 1.0            # добавлено дельтой


def test_json_full_is_selection_object(registry, project):
    write_card(registry, "wide-card", files=["moe.py"])
    data = json.loads(run_card_match(registry, "--project", str(project), "--json-full").stdout)
    assert set(data) == {"min_score", "max_cards", "dedupe", "matched", "hits", "suppressed"}
    assert (data["min_score"], data["max_cards"], data["dedupe"]) == (2, 0, 0.0)
    assert data["matched"] == 1 and data["suppressed"] == []


def test_invalid_flag_values_rejected(registry, project):
    for args in (["--max-cards", "-1"], ["--dedupe", "-0.1"], ["--dedupe", "1.5"],
                 ["--json", "--json-full"]):
        r = run_card_match(registry, "--project", str(project), *args)
        assert r.returncode == 2, (args, r.returncode)


# ── K8: routing.json и запреты §4 ────────────────────────────────────────────

def test_emit_routing_format_unchanged(registry, project):
    """K8: формат routing.json прежний; флаги отбора на таблицу не влияют."""
    write_card(registry, "wide-card", files=["moe.py"], keys=["top_k"], skills=["skill-a"])
    out = registry.parent / "routing.json"
    r = run_card_match(registry, "--emit-routing", str(out), "--max-cards", "1", "--dedupe", "0.5")
    assert r.returncode == 0, r.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert list(data) == ["min_score", "rows"]
    assert data["min_score"] == 2
    assert [row["trigger"] for row in data["rows"]] == ["moe.py", "top_k"]
    assert all(list(row) == ["trigger", "kind", "weight", "card", "state", "skills"]
               for row in data["rows"])


def test_registry_cards_are_not_touched(registry, project):
    """§4: прогон отбора не меняет и не удаляет карточки."""
    paths = [write_card(registry, "wide-card", files=["moe.py"]),
             write_card(registry, "narrow-card", files=["moe.py", "gone.py"])]
    before = {p: p.read_bytes() for p in paths}
    run_card_match(registry, "--project", str(project), "--max-cards", "1", "--dedupe", "0.5")
    assert {p: p.read_bytes() for p in paths} == before


def test_selection_imports_no_llm_or_network():
    """§4: отбор детерминированный — в card_match нет сетевых/LLM-зависимостей."""
    mods = set()
    for node in ast.walk(ast.parse(CARD_MATCH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert mods <= {"argparse", "fnmatch", "json", "os", "re", "sys", "cardio"}
