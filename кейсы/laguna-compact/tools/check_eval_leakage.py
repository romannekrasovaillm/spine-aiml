#!/usr/bin/env python3
"""C-009 — поведенческий страж AD-7 «eval-набор без утечек».

Механический аудит вместо ручной правки 8 задач из 200 (``EXPERIMENT_PLAN.md`` §2).
Два независимых гейта и один информационный сигнал:

* **Гейт A — ответ в промпте.** Ни один slug-ответ eval-задачи
  (``expected_slugs``/``expected_slug``) не встречается буквально в тексте её
  промпта. Это ровно тот класс утечки, что нашли руками: модель копирует slug
  из промпта и «решает» задачу, не обращаясь к концептам.
  Проверка вынесена в ``checked_in_prompt`` и **переиспользуется** лейк-фильтром
  RL-пула (``build_rev_envs.py``, S3f-fix/ADR-021 п.3): правило одно на оба
  контура, а не две похожие реализации.
* **Гейт B — пересечение eval ↔ train.** Пересечение по нормализованному тексту
  (lower + схлопнутые пробелы) между промптами eval и текстами train пусто.
* **INFO — шинглы.** Доля промптов eval, делящих с train хотя бы один
  n-грамм (по умолчанию 12 слов). Гейтом не является: ловит near-duplicate,
  который точное сравнение пропускает. Печатается всегда, чтобы «зелено по
  гейтам» не читалось как «утечек нет вообще».

Честный результат важнее зелёного цвета: найденные утечки перечисляются
поимённо (индекс задачи, слага, поле), а не сворачиваются в счётчик.

Коды возврата::

    0 — утечек нет (оба гейта пройдены)
    1 — утечка: список нарушений в stdout, exit 1
    2 — NOT-VERIFIED: вход отсутствует/нечитаем — не зелёный

Запуск::

    python3 tools/check_eval_leakage.py --eval datasets/eval_ood_clean.jsonl \\
                                        --train datasets/sft_train_v12.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Поля eval-задачи, где лежит ответ (slug-и).
SLUG_FIELDS = ("expected_slugs", "expected_slug")

#: Поля, из которых берётся текст промпта, если нет messages[].
PROMPT_FIELDS = ("prompt", "input", "question", "text")

#: Поля train-примера, если нет messages[].
TEXT_FIELDS = ("prompt", "input", "question", "text", "response", "completion")

_WS = re.compile(r"\s+")


class NotVerified(Exception):
    """Входа нет — судить не о чем."""


def norm(text: str | None) -> str:
    """Нормализация для гейта B: lower + схлопнутые пробелы."""
    return _WS.sub(" ", (text or "").lower()).strip()


def checked_in_prompt(needle: str | None, prompt: str | None) -> bool:
    """Гейт A в общей форме: проверяемая строка встречается в промпте буквально.

    Одно правило на **оба** контура: утечка eval-набора (C-009, AD-7) и лейк
    RL-пула (S3f-fix, ADR-021 п.3) проверяются одинаково — без учёта регистра,
    подстрокой. Не «похоже», а буквально: ровно так же замороженный верификатор
    пайплайна ищет проверяемый slug в ответе (`s.lower() in resp_lower`),
    поэтому буквальное вхождение в промпте означает, что ответ копируется.
    """
    if not needle:
        return False
    return str(needle).lower() in (prompt or "").lower()


def read_jsonl(path: Path, limit: int | None = None) -> Iterator[tuple[int, dict]]:
    """Потоковое чтение jsonl: файл train — сотни МБ, в память целиком не берём."""
    with path.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                return
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise NotVerified(f"{path}:{i + 1}: не JSON ({e})") from e
            if isinstance(obj, dict):
                yield i, obj


def as_list(value) -> list[str]:
    """``expected_slugs`` бывает строкой, списком и отсутствует."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value if v]
    return [str(value)]


def eval_slugs(task: dict) -> list[str]:
    slugs: list[str] = []
    for f in SLUG_FIELDS:
        slugs.extend(as_list(task.get(f)))
    return slugs


def eval_prompt_text(task: dict) -> str:
    """Текст промпта задачи: ``prompt`` либо не-assistant сообщения."""
    if task.get("prompt"):
        return str(task["prompt"])
    msgs = task.get("messages")
    if isinstance(msgs, list):
        parts = [str(m.get("content") or "") for m in msgs
                 if isinstance(m, dict) and m.get("role") != "assistant"]
        if parts:
            return "\n".join(parts)
    for f in PROMPT_FIELDS:
        if task.get(f):
            return str(task[f])
    raise NotVerified("eval-задача без текста промпта (нет prompt/messages)")


def train_texts(example: dict) -> list[str]:
    """Все тексты train-примера: сообщения по отдельности либо плоские поля."""
    msgs = example.get("messages")
    if isinstance(msgs, list):
        out = [str(m.get("content") or "") for m in msgs if isinstance(m, dict)]
        if out:
            return out
    return [str(example[f]) for f in TEXT_FIELDS if example.get(f)]


def train_user_texts(example: dict) -> list[str]:
    """Тексты пользовательской стороны — для near-duplicate сигнала."""
    msgs = example.get("messages")
    if isinstance(msgs, list):
        return [str(m.get("content") or "") for m in msgs
                if isinstance(m, dict) and m.get("role") == "user"]
    return [str(example[f]) for f in ("prompt", "input", "question")
            if example.get(f)]


def shingles(text: str, k: int) -> set[str]:
    """Множество k-грамм по словам — дешёвый аналог near-duplicate."""
    w = text.split()
    if len(w) < k:
        return set()
    return {" ".join(w[i:i + k]) for i in range(len(w) - k + 1)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="C-009: аудит утечек eval-набора (slug-ответ в промпте; "
                    "пересечение eval ↔ train по нормализованному тексту).")
    ap.add_argument("--eval", dest="eval_path", required=True, help="eval-набор (.jsonl)")
    ap.add_argument("--train", dest="train_path", required=True,
                    help="SFT/RL-пул (.jsonl), с которым проверяется пересечение")
    ap.add_argument("--ngram", type=int, default=12,
                    help="длина шингла для INFO-сигнала near-duplicate (0 — выключить)")
    ap.add_argument("--max-train-lines", type=int, default=None,
                    help="ограничить разбор train (диагностика)")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    report: dict = {"check": "C-009", "violations": [], "info": {}, "gate": {}}
    try:
        ev_path, tr_path = Path(args.eval_path), Path(args.train_path)
        for p in (ev_path, tr_path):
            if not p.is_file():
                raise NotVerified(f"файл не найден: {p}")
        tasks = [obj for _i, obj in read_jsonl(ev_path)]
        if not tasks:
            raise NotVerified(f"{ev_path}: ни одной задачи")
    except NotVerified as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    except OSError as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    # ── Гейт A: slug-ответ не встречается в промпте своей задачи ──────────────
    checked = 0
    slug_violations: list[dict] = []
    for idx, task in enumerate(tasks):
        try:
            prompt = eval_prompt_text(task)
        except NotVerified as e:
            print(f"NOT-VERIFIED: задача #{idx}: {e}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        checked += 1
        for slug in eval_slugs(task):
            if checked_in_prompt(slug, prompt):
                slug_violations.append({"task_index": idx, "slug": slug,
                                        "task_type": task.get("task_type")})
    report["gate"]["slug_in_own_prompt"] = {"tasks_checked": checked,
                                            "violations": len(slug_violations)}
    report["violations"].extend(
        {"gate": "A", "kind": "slug_in_prompt", **v} for v in slug_violations)

    # ── Гейт B: пересечение eval ↔ train по нормализованному тексту ───────────
    try:
        train_set: set[str] = set()
        n_examples = 0
        for _i, ex in read_jsonl(tr_path, args.max_train_lines):
            n_examples += 1
            for t in train_texts(ex):
                n = norm(t)
                if n:
                    train_set.add(n)
    except (NotVerified, OSError) as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    overlap: list[dict] = []
    for idx, task in enumerate(tasks):
        n = norm(eval_prompt_text(task))
        if n and n in train_set:
            overlap.append({"task_index": idx, "task_type": task.get("task_type"),
                            "normalized_sha1": hashlib.sha1(
                                n.encode()).hexdigest()[:12]})
    report["gate"]["eval_train_text_overlap"] = {
        "train_examples": n_examples, "train_texts": len(train_set),
        "violations": len(overlap)}
    report["violations"].extend(
        {"gate": "B", "kind": "eval_prompt_in_train", **v} for v in overlap)

    # ── INFO: near-duplicate по шинглам ──────────────────────────────────────
    if args.ngram > 0:
        try:
            train_sh: set[str] = set()
            for _i, ex in read_jsonl(tr_path, args.max_train_lines):
                for t in train_user_texts(ex):
                    train_sh |= shingles(norm(t), args.ngram)
        except (NotVerified, OSError) as e:
            print(f"NOT-VERIFIED: {e}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        near = []
        for idx, task in enumerate(tasks):
            shared = shingles(norm(eval_prompt_text(task)), args.ngram) & train_sh
            if shared:
                near.append({"task_index": idx, "shared_ngrams": len(shared)})
        report["info"]["near_duplicate_ngram"] = {
            "n": args.ngram, "train_shingles": len(train_sh), "tasks": len(near)}
        report["info"]["near_duplicate_tasks"] = near[:20]

    ok = not report["violations"]
    report["ok"] = ok

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK if ok else EXIT_FAIL

    print("== C-009 / AD-7: аудит утечек eval ==")
    print(f"eval:  {ev_path}  задач: {len(tasks)}")
    print(f"train: {tr_path}  примеров: {n_examples}  текстов: {len(train_set)}")
    print()
    print(f"гейт A (slug-ответ в промпте): проверено задач {checked}, "
          f"нарушений {len(slug_violations)}")
    print(f"гейт B (eval ∩ train по тексту): нарушений {len(overlap)}")
    if args.ngram > 0:
        info = report["info"]["near_duplicate_ngram"]
        print(f"INFO near-duplicate ({args.ngram}-граммы): промптов с общим "
              f"n-граммом {info['tasks']} из {len(tasks)} "
              f"(шинглов train: {info['train_shingles']}) — не гейт")
    print()
    if report["violations"]:
        print(f"УТЕЧКИ ({len(report['violations'])}):")
        for v in report["violations"]:
            if v["kind"] == "slug_in_prompt":
                print(f"  - гейт A: задача #{v['task_index']} "
                      f"({v.get('task_type')}): slug '{v['slug']}' встречается в промпте")
            else:
                print(f"  - гейт B: задача #{v['task_index']} ({v.get('task_type')}): "
                      f"нормализованный текст промпта совпал с train "
                      f"(sha1 {v['normalized_sha1']})")
        return EXIT_FAIL
    print("LEAKAGE OK: утечек не найдено")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
