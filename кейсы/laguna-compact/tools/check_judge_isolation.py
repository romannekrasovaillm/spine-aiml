#!/usr/bin/env python3
"""C-013 — поведенческий страж AD-6 «судья не участвует в награде».

Статический анализ пайплайна (AST, без импорта модуля — контур read-only и его
зависимости могут отсутствовать):

1. Строит граф вызовов внутри модуля и транзитивное замыкание от **корней
   награды** (``compute_reward``, ``verify_task``, ``keyword_coverage`` и их
   локальные помощники — ``_task_terms``, ``_has_relevant_query``, ``_sig_words``,
   ``_ensure_def_index``, ``length_weighted_loo_advantage``, ``cispo_surrogate_loss``).
2. Ищет в замыкании вызовы судьи/LLM-клиентов (``glm``, ``judge``, ``openai``,
   ``anthropic``, ``requests.post``, ``http``). Любая находка — FAIL: reward
   hacking (оптимизация судьи вместо задачи) должен ловиться по коду, а не по
   графику метрик.
3. Требует, чтобы вызов судьи **существовал** на eval-стадии (замыкание
   ``--eval-func``, по умолчанию ``run_eval``). Иначе проверка вакуумна: «нет
   вызовов судьи нигде» — это не изоляция, а отсутствие судьи, и зелёный гейт по
   нему был бы ложным.

Коды возврата::

    0 — judge isolated: reward path clean
    1 — FAIL: вызов судьи в контуре награды, либо судья не найден на eval-стадии
    2 — NOT-VERIFIED: пайплайн не найден/не разобран

Запуск::

    python3 tools/check_judge_isolation.py --pipeline laguna_pipeline_v8.py
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

DEFAULT_CONTOUR = "/home/user/gb10-shared"

#: Имена/модули судьи и LLM-клиентов (AD-6: «поиск glm, judge, openai, anthropic,
#: requests.post, http»). Регистр не важен.
JUDGE_RE = re.compile(r"(glm|judge|openai|anthropic|llm_judge)", re.I)
#: Сетевые клиенты: полное dotted-имя вызова начинается с модуля или «http» внутри.
HTTP_MODULE_RE = re.compile(r"^(requests|httpx|urllib3|aiohttp|http\.client)\b", re.I)
HTTP_NAME_RE = re.compile(r"http", re.I)

#: Корни контура награды (v8): имя функции → почему она в награде.
REWARD_ROOTS = {
    "compute_reward": "ступени награды RL (parse → min_steps → timeout → verifier)",
    "verify_task": "детерминированный верификатор ответа",
    "keyword_coverage": "shaped-сигнал покрытия терминов",
    "length_weighted_loo_advantage": "преимущество LOO по группе",
    "cispo_surrogate_loss": "суррогатный лосс CISPO",
}
#: Локальные помощники контура награды: попадают в замыкание по вызову, но если
#: их никто не зовёт (рефакторинг), всё равно обязаны быть чистыми.
REWARD_HELPERS = {
    "_task_terms", "_has_relevant_query", "_sig_words", "_ensure_def_index",
    "_load_concepts_index", "_def_index",
}


class NotVerified(Exception):
    """Судить не о чем — это не «зелено»."""


def dotted(func: ast.expr) -> str:
    """``requests.post`` → 'requests.post'; ``glm_judge`` → 'glm_judge'."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = dotted(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    if isinstance(func, ast.Subscript):
        return dotted(func.value)
    return ""


def is_judge_call(name: str) -> bool:
    """Вызов судьи или LLM-клиента."""
    if not name:
        return False
    head = name.split(".")[0]
    if JUDGE_RE.search(name):
        return True
    if HTTP_MODULE_RE.search(name) or HTTP_MODULE_RE.search(head):
        return True
    return bool(HTTP_NAME_RE.search(head))


class ModuleIndex:
    """Индекс модуля: определения функций и их вызовы."""

    def __init__(self, tree: ast.Module, path: Path):
        self.path = path
        self.tree = tree
        self.source = path.read_text(encoding="utf-8", errors="replace").splitlines()
        self.funcs: dict[str, ast.AST] = {}
        self.calls: dict[str, list[tuple[str, int]]] = {}
        self.judge_calls: list[tuple[str, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[node.name] = node
                self.calls.setdefault(node.name, [])
        for fname, node in self.funcs.items():
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                name = dotted(sub.func)
                if not name:
                    continue
                self.calls[fname].append((name, sub.lineno))
                if is_judge_call(name):
                    self.judge_calls.append((fname, sub.lineno, name))

    def closure(self, roots: list[str]) -> set[str]:
        """Транзитивное замыкание вызовов от roots по локальным функциям."""
        seen: set[str] = set()
        stack = [r for r in roots if r in self.funcs]
        while stack:
            fn = stack.pop()
            if fn in seen:
                continue
            seen.add(fn)
            for name, _ln in self.calls.get(fn, []):
                head = name.split(".")[0]
                target = head if head in self.funcs else name
                if target in self.funcs and target not in seen:
                    stack.append(target)
        return seen

    def line(self, n: int) -> str:
        return self.source[n - 1].strip() if 0 < n <= len(self.source) else ""


def resolve_pipeline(arg: str, contour: Path) -> Path:
    """Пайплайн: как задан → в корне контура → отказ (без угадывания)."""
    p = Path(arg)
    if p.is_file():
        return p
    candidate = contour / p.name
    if candidate.is_file():
        return candidate
    raise NotVerified(f"пайплайн не найден: {arg} (и {candidate})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="C-013: судья/LLM-клиенты не вызываются в контуре награды (AD-6).")
    ap.add_argument("--pipeline", default="laguna_pipeline_v8.py",
                    help="файл пайплайна (по умолчанию laguna_pipeline_v8.py)")
    ap.add_argument("--contour", default=os.environ.get("LAGUNA_CONTOUR", DEFAULT_CONTOUR),
                    help=f"корень контура (по умолчанию {DEFAULT_CONTOUR})")
    ap.add_argument("--eval-func", default="run_eval",
                    help="функция eval-стадии, где вызов судьи обязателен")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    report: dict = {"check": "C-013", "problems": [], "reward_closure": [],
                    "judge_call_sites": [], "eval_path_judge_calls": 0}
    try:
        pipeline = resolve_pipeline(args.pipeline, Path(args.contour))
        tree = ast.parse(pipeline.read_text(encoding="utf-8", errors="replace"),
                         filename=str(pipeline))
    except NotVerified as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    except (OSError, SyntaxError) as e:
        print(f"NOT-VERIFIED: пайплайн не разобран: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    report["pipeline"] = str(pipeline)
    idx = ModuleIndex(tree, pipeline)

    roots = sorted(set(REWARD_ROOTS) | REWARD_HELPERS)
    closure = idx.closure(roots)
    # Корни, которых нет в модуле, — не ошибка, но их отсутствие сообщаем: без них
    # замыкание может оказаться пустым и «чистым» по недоразумению.
    missing_roots = [r for r in REWARD_ROOTS if r not in idx.funcs]
    if missing_roots:
        report["problems"].append(
            "в пайплайне нет ожидаемых корней награды: " + ", ".join(missing_roots)
            + " — проверь, что контур награды не переименован")
    if not closure:
        report["problems"].append("замыкание контура награды пусто — проверка вакуумна")
    report["reward_closure"] = sorted(closure)

    # ── 1) судья/LLM-клиенты внутри контура награды ───────────────────────────
    for fname, lineno, call in idx.judge_calls:
        if fname in closure:
            report["problems"].append(
                f"вызов судьи/LLM-клиента в контуре награды: {call}() "
                f"в {fname}():{lineno} → {idx.line(lineno)}")

    # ── 2) вызов судьи обязан существовать на eval-стадии ─────────────────────
    eval_closure = idx.closure([args.eval_func])
    if not eval_closure:
        report["problems"].append(
            f"eval-функция '{args.eval_func}' не найдена — судья на eval не подтверждён")
    eval_judge = [(f, ln, c) for f, ln, c in idx.judge_calls if f in eval_closure]
    report["eval_path_judge_calls"] = len(eval_judge)
    if eval_closure and not eval_judge:
        report["problems"].append(
            f"в замыкании '{args.eval_func}' нет вызова судьи/LLM — изоляция недоказуема "
            f"(«нет судьи нигде» ≠ «судья вне награды»)")

    for f, ln, c in idx.judge_calls:
        report["judge_call_sites"].append(
            {"function": f, "line": ln, "call": c, "in_eval_path": f in eval_closure,
             "in_reward_path": f in closure, "source": idx.line(ln)})

    ok = not report["problems"]
    report["ok"] = ok

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK if ok else EXIT_FAIL

    print("== C-013 / AD-6: изоляция судьи от награды ==")
    print(f"pipeline: {pipeline}")
    print(f"контур награды ({len(closure)}): {', '.join(sorted(closure)) or '—'}")
    print()
    print(f"вызовы судьи/LLM ({len(idx.judge_calls)}):")
    for s in report["judge_call_sites"]:
        where = "eval" if s["in_eval_path"] else "reward" if s["in_reward_path"] else "прочее"
        print(f"  - {s['function']}():{s['line']} → {s['call']}()  [{where}]")
        print(f"      {s['source']}")
    print()
    if report["problems"]:
        print(f"FAIL ({len(report['problems'])}):")
        for p in report["problems"]:
            print(f"  - {p}")
        return EXIT_FAIL
    print("judge isolated: reward path clean")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
