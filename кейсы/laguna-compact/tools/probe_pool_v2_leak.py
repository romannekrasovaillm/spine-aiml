#!/usr/bin/env python3
"""S3p — проба пула ревизии v2: награждается ли копия промпта (лейк-инвариант ADR-021 п.3).

**Вопрос.** Пул ревизии v2 собирался дважды: 9197 задач (S3f, коммит `35f933d`) и
9162 задачи (S3f-fix-2, коммит `c0a1480`). Расхождение — 35 задач, и часть его
объясняется лейк-фильтром S3f-fix, снявшим 500 цепочек, у которых проверяемые
строки названы в промпте. Проба отвечает на вопрос **измерением, а не повтором
правила конвертера**: остались ли в пуле задачи, которые решаются копированием
промпта.

**Судья — тот же замороженный верификатор, что судит награду**
(`laguna_pipeline_v8.verify_task`), а не предикат сборки: ответ = сам промпт. Это
ровно та форма лейка, против которой писалось правило (ADR-021 п.3 — тот же
принцип, что C-009 для eval-набора): если верификатор награждает копию промпта,
задача даёт награду без обучения, и число таких задач обязано быть нулём.

**Два независимых счёта.**

1. **Буквальное вхождение** проверяемых строк задачи в её промпт — форма гейта A
   стража C-009 (у slug-ветки верификатора проверка и есть буквальное `in` по
   ответу). Считается без индекса концептов и без импорта пайплайна.
2. **Копия промпта как ответ** через `verify_task` — покрывает и keyword-ветку
   (термины определений обоих концептов), которой нужен индекс концептов
   (``CONCEPTS_INDEX``). Пайплайн и индекс недоступны → проверка 2 честно
   называется NOT-VERIFIED, а не «зелёной».

**Почему keyword-ветка не судится первым счётом.** У `explain_relation` в
промпте лежат slug-и концептов по построению задачи, а верификатор проверяет не
их, а термины ОПРЕДЕЛЕНИЙ (замороженная мера, ADR-021). Поэтому буквальное
совпадение slug-а с промптом у этого типа — не лейк, и первый счёт его не
считает; число таких задач проба печатает отдельно как справку.

**Что проба не делает.** Не пишет в пул и источники (только чтение), не является
гейтом (AD-10: правила CONSTRAINTS ссылаются только на ``tools/check_*``), не
пересобирает пул: её предмет — уже лежащий файл.

Коды возврата::

    0 — измерение состоялось, лейка нет (все доступные проверки прошли)
    1 — лейк найден: задача награждается копией своего промпта либо проверяемая
        строка лежит в промпте буквально — сигнал архитектору, а не «мелкий шум»
    2 — NOT-VERIFIED: пул отсутствует/пуст, либо проверку 2 нельзя выполнить
        (нет пайплайна или индекса концептов)

Запуск::

    CONCEPTS_INDEX=datasets/concepts_search_index.jsonl python3 tools/probe_pool_v2_leak.py
    python3 tools/probe_pool_v2_leak.py --no-verifier    # только буквальный счёт
    python3 tools/probe_pool_v2_leak.py --pool <файл> --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POOL = "datasets/rl_tasks_revpool_v2.jsonl"

EXIT_OK, EXIT_LEAK, EXIT_NOT_VERIFIED = 0, 1, 2

#: Словарь типов — из конвертера (одна точка правды, ADR-023 п.7); тест S3f
#: отдельно сверяет его с диспетчером верификатора в пайплайне.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from build_rev_envs import SLUG_TASK_TYPES  # noqa: E402
except Exception as exc:  # pragma: no cover — сломанный конвертер = нет измерения
    print(f"NOT-VERIFIED: конвертер пула не импортируется ({exc})", file=sys.stderr)
    sys.exit(EXIT_NOT_VERIFIED)

#: Стоп-слова keyword-ветки: их совпадение с промптом ничего не значит (пайплайн
#: вычитает их до сравнения) — при счёте справки повторяем то же вычитание.
KEYWORD_STOP = ("related", "связаны", "связан")


def read_pool(path: Path) -> list[dict]:
    tasks = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def literal_check(tasks: list[dict]) -> tuple[list[dict], int]:
    """Проверка 1: проверяемая строка slug-ветки в промпте буквально.

    Возвращает (находки, справка) — справка = число keyword-задач, у которых
    slug концепта назван в промпте (не лейк: keyword-ветка проверяет термины
    определений, а не slug-и).
    """
    hits, keyword_in_prompt = [], 0
    for i, t in enumerate(tasks, 1):
        prompt = (t.get("prompt") or "").lower()
        if t.get("task_type") in SLUG_TASK_TYPES:
            checked = t.get("expected_slugs") or t.get("expected_slug") or []
            if isinstance(checked, str):
                checked = [checked]
            in_prompt = [s for s in checked if s.lower() in prompt]
            if in_prompt:
                hits.append({"line": i, "task_type": t.get("task_type"),
                             "task_id": t.get("source_task_id"),
                             "checked_in_prompt": in_prompt})
        else:
            kws = [k for k in (t.get("keywords") or []) if k.lower() not in KEYWORD_STOP]
            if any(k.lower() in prompt for k in kws):
                keyword_in_prompt += 1
    return hits, keyword_in_prompt


def verifier_check(tasks: list[dict]) -> tuple[list[dict], dict | None, str]:
    """Проверка 2: ответ = сам промпт, судья — замороженный verify_task.

    Возвращает (находки, счётчик по типам, причина NOT-VERIFIED|"").
    """
    sys.path.insert(0, str(CASE_ROOT))
    try:
        import laguna_pipeline_v8 as P  # noqa: PLC0415 — тяжёлый импорт (torch)
    except Exception as exc:  # pragma: no cover — окружение без пайплайна
        return [], None, f"пайплайн laguna_pipeline_v8 не импортируется ({exc})"
    hits, by_type = [], {}
    for i, t in enumerate(tasks, 1):
        try:
            rewarded = P.verify_task(t.get("prompt") or "", t)
        except FileNotFoundError as exc:  # индекс концептов: keyword-ветка
            return hits, None, f"индекс концептов недоступен ({exc})"
        except Exception as exc:  # noqa: BLE001 — любая поломка = не измерено
            return hits, None, f"верификатор отказал на строке {i} ({exc})"
        if rewarded:
            tt = t.get("task_type")
            by_type[tt] = by_type.get(tt, 0) + 1
            hits.append({"line": i, "task_type": tt,
                         "task_id": t.get("source_task_id")})
    return hits, by_type, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Проба пула v2 на лейк (S3p)")
    ap.add_argument("--pool", default=DEFAULT_POOL, help="пул задач (jsonl)")
    ap.add_argument("--no-verifier", action="store_true",
                    help="только буквальный счёт (без пайплайна и индекса)")
    ap.add_argument("--json", action="store_true", help="машинный отчёт")
    ap.add_argument("--max-shown", type=int, default=5, help="сколько находок печатать")
    args = ap.parse_args(argv)

    pool = Path(args.pool)
    if not pool.is_absolute():
        pool = (CASE_ROOT / pool) if (CASE_ROOT / pool).exists() else Path(args.pool)
    if not pool.is_file():
        print(f"NOT-VERIFIED: пула нет: {pool}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    tasks = read_pool(pool)
    if not tasks:
        print(f"NOT-VERIFIED: пул пуст: {pool}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    literal_hits, keyword_in_prompt = literal_check(tasks)
    report = {
        "pool": str(pool),
        "tasks": len(tasks),
        "by_task_type": {},
        "literal": {"hits": len(literal_hits), "examples": literal_hits[:args.max_shown],
                    "keyword_slugs_in_prompt": keyword_in_prompt},
        "prompt_copy": None,
        "concepts_index": os.environ.get("CONCEPTS_INDEX", "(не задан)"),
    }
    for t in tasks:
        tt = t.get("task_type")
        report["by_task_type"][tt] = report["by_task_type"].get(tt, 0) + 1

    #: off — проверка 2 выключена вызывающим; not_verified — её нельзя было
    #: выполнить; ok — выполнена. «Не выполнена» и «не выполнялась» — разные
    #: состояния: первое возвращает 2, второе нет.
    verifier_state, not_verified = "off", ""
    if not args.no_verifier:
        hits, by_type, why = verifier_check(tasks)
        if why:
            verifier_state, not_verified = "not_verified", why
        else:
            verifier_state = "ok"
            report["prompt_copy"] = {"rewarded": len(hits), "by_task_type": by_type,
                                     "examples": hits[:args.max_shown]}
    report["prompt_copy_state"] = verifier_state

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"пул: {pool} — {len(tasks)} задач")
        print(f"  проверка 1 (буквальное вхождение проверяемой строки в промпт): "
              f"{len(literal_hits)} находок")
        for h in literal_hits[:args.max_shown]:
            print(f"    строка {h['line']}: {h['task_type']} {h['task_id']} → {h['checked_in_prompt']}")
        print(f"  справка: keyword-задач со slug-ом в промпте (не лейк — ветка проверяет "
              f"термины определений): {keyword_in_prompt}")
        if report["prompt_copy"] is not None:
            pc = report["prompt_copy"]
            print(f"  проверка 2 (копия промпта как ответ, замороженный verify_task): "
                  f"награждено {pc['rewarded']} задач")
            for h in pc["examples"]:
                print(f"    строка {h['line']}: {h['task_type']} {h['task_id']}")
        elif verifier_state == "off":
            print("  проверка 2: отключена (--no-verifier) — судится только буквальный счёт")
        else:
            print(f"  проверка 2: NOT-VERIFIED — {not_verified}")

    if literal_hits or (report["prompt_copy"] or {}).get("rewarded"):
        if not args.json:
            print("ЛЕЙК: задача награждается копированием своего промпта", file=sys.stderr)
        return EXIT_LEAK
    if not_verified:
        if not args.json:
            print(f"NOT-VERIFIED: {not_verified}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
