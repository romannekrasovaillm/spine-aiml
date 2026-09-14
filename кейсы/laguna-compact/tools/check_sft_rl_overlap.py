#!/usr/bin/env python3
"""S1 — детектор пересечения пулов SFT и RL (AD-7: состав данных под стражем).

Отвечает на один вопрос: **сколько промптов SFT-пула дословно совпадают с
промптами RL-пула** и какие именно. Совпадение промпта между стадиями — не
дефект сам по себе (RL-задачи домена и SFT-примеры на тех же концептах — часть
замысла контура), но это факт, который обязан быть измерен до прогона: он
определяет, что именно оптимизирует RL — новую задачу или заученный пример.

Инструмент **ничего не фильтрует и не переписывает** (AD-7: состав заморожен).
Фильтрация — решение следующей дельты, принимаемое архитектором по этому числу.

Нормализация — та же, что в гейте C-009 (``norm`` из ``check_eval_leakage.py``):
lower + схлопнутые пробелы. Сравниваются пользовательские тексты SFT
(``role == "user"``) с полем ``prompt`` RL-задачи: именно они попадают в
контекст модели, остальное — служебные поля.

``--expect-matched N`` — кросс-проверка против известного замера (например,
39 из 44 949 по дельте S0-fix+S1). Расхождение — такой же сигнал, как найденные
совпадения: инструмент, который молча «перемерил» и разошёлся с известным
числом, обесценивает и замер, и число.

Коды возврата::

    0 — измерение состоялось: совпадений нет (и кросс-проверка, если задана,
        сошлась)
    1 — измерение состоялось, но есть сигнал: совпадения найдены ИЛИ результат
        разошёлся с ``--expect-matched`` (не «тихая» правка данных: вердикт о
        фильтрации не выносится)
    2 — NOT-VERIFIED: вход отсутствует/нечитаем — не зелёный

Запуск::

    python3 tools/check_sft_rl_overlap.py                       # симлинки кейса
    python3 tools/check_sft_rl_overlap.py --expect-matched 39   # кросс-проверка
    python3 tools/check_sft_rl_overlap.py --sft A.jsonl --rl B.jsonl --json
    python3 tools/check_sft_rl_overlap.py --no-evidence          # без записи
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

#: Соседние инструменты — источник нормализации и потокового чтения jsonl:
#: одна реализация нормы на два стража (иначе «как в C-009» разъедется).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_eval_leakage import NotVerified, norm, read_jsonl, train_user_texts  # noqa: E402

EXIT_OK, EXIT_OVERLAP, EXIT_NOT_VERIFIED = 0, 1, 2

DEFAULT_SFT = "datasets/sft_train_v12.jsonl"
DEFAULT_RL = "datasets/rl_tasks_oxalpha.jsonl"
DEFAULT_EVIDENCE = "evidence/s1-data-audit.json"

#: Поля-идентификаторы задачи, если они есть в записи (в oxalpha-пуле их нет —
#: тогда идентификатором служит номер строки файла, 1-based, как в самом файле).
ID_FIELDS = ("id", "task_id", "taskId", "slug")


def task_id(record: dict, line_no: int) -> str:
    """Идентификатор задачи: явное поле, иначе номер строки."""
    for f in ID_FIELDS:
        if record.get(f) not in (None, ""):
            return str(record[f])
    return f"line:{line_no}"


def preview(text: str, width: int = 120) -> str:
    """Первые ``width`` символов нормализованного промпта — для поимённого списка."""
    return text if len(text) <= width else text[: width - 1] + "…"


def rl_prompts(path: Path, limit: int | None) -> tuple[dict[str, list[dict]], int]:
    """Нормализованный промпт RL → список задач с этим промптом.

    Индекс строится по RL-пулу: он на порядок меньше SFT (12 МБ против 486 МБ),
    поэтому в память берём его, а SFT читаем потоком.
    """
    index: dict[str, list[dict]] = {}
    n = 0
    for i, obj in read_jsonl(path, limit):
        n += 1
        prompt = obj.get("prompt") or obj.get("input") or obj.get("question")
        if not prompt:
            raise NotVerified(f"{path}:{i + 1}: RL-задача без текста промпта")
        key = norm(str(prompt))
        if not key:
            continue
        index.setdefault(key, []).append({
            "rl_line": i + 1,
            "rl_id": task_id(obj, i + 1),
            "rl_task_type": obj.get("task_type"),
        })
    return index, n


def scan_sft(path: Path, index: dict[str, list[dict]], limit: int | None,
             max_matches: int) -> dict:
    """Потоковый скан SFT: сверяет пользовательские тексты с промптами RL."""
    n_examples = 0
    matched_prompts = 0
    pairs = 0
    matched_rl: set[str] = set()
    matches: list[dict] = []
    sft_user_texts: set[str] = set()

    for i, obj in read_jsonl(path, limit):
        n_examples += 1
        users = [norm(t) for t in train_user_texts(obj)]
        users = [u for u in users if u]
        if not users:
            continue
        sft_user_texts.update(users)
        hits = [(u, rl) for u in users if u in index for rl in index[u]]
        if not hits:
            continue
        matched_prompts += 1
        pairs += len(hits)
        for u, rl in hits:
            matched_rl.add(rl["rl_id"])
            if max_matches == 0 or len(matches) < max_matches:
                matches.append({
                    "sft_line": i + 1,
                    "sft_id": task_id(obj, i + 1),
                    "rl_line": rl["rl_line"],
                    "rl_id": rl["rl_id"],
                    "rl_task_type": rl["rl_task_type"],
                    "normalized_sha1": hashlib.sha1(u.encode()).hexdigest()[:12],
                    "prompt_preview": preview(u),
                })
    rl_side = sum(1 for key, tasks in index.items() if key in sft_user_texts
                  for _ in tasks)
    return {
        "sft_examples": n_examples,
        "sft_matched_prompts": matched_prompts,
        "sft_rl_pairs": pairs,
        "rl_matched_tasks": len(matched_rl),
        "rl_matched_tasks_from_sft_side": rl_side,
        "matches": matches,
        "matches_truncated": pairs > len(matches),
    }


def merge_evidence(path: Path, payload: dict) -> str:
    """Дописывает поле в evidence-файл, не затирая остальные (S1 — общий отчёт)."""
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError as e:
            print(f"NOT-VERIFIED: {path} не разбирается как JSON ({e})", file=sys.stderr)
            raise SystemExit(EXIT_NOT_VERIFIED) from e
    data["sft_rl_overlap"] = payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="S1: пересечение пулов SFT и RL по нормализованному тексту "
                    "промптов (детекция и отчёт; данные не меняются).")
    ap.add_argument("--sft", default=DEFAULT_SFT, help=f"SFT-пул (.jsonl), по умолчанию {DEFAULT_SFT}")
    ap.add_argument("--rl", default=DEFAULT_RL, help=f"RL-пул (.jsonl), по умолчанию {DEFAULT_RL}")
    ap.add_argument("--limit-matches", type=int, default=20,
                    help="сколько совпадений перечислить поимённо (0 — все)")
    ap.add_argument("--max-sft-lines", type=int, default=None,
                    help="ограничить разбор SFT (диагностика)")
    ap.add_argument("--max-rl-lines", type=int, default=None,
                    help="ограничить разбор RL (диагностика)")
    ap.add_argument("--expect-matched", type=int, default=None,
                    help="кросс-проверка: ожидаемое число совпавших промптов SFT "
                         "(расхождение — сигнал, exit 1)")
    ap.add_argument("--evidence", default=DEFAULT_EVIDENCE,
                    help=f"evidence-файл для записи поля sft_rl_overlap (по умолчанию {DEFAULT_EVIDENCE})")
    ap.add_argument("--no-evidence", action="store_true", help="не писать evidence")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    sft_path, rl_path = Path(args.sft), Path(args.rl)
    for p in (sft_path, rl_path):
        if not p.is_file():
            print(f"NOT-VERIFIED: файл не найден: {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED

    try:
        index, n_rl = rl_prompts(rl_path, args.max_rl_lines)
        res = scan_sft(sft_path, index, args.max_sft_lines, args.limit_matches)
    except NotVerified as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    except OSError as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    payload = {
        "sft": str(sft_path),
        "rl": str(rl_path),
        "comparison": "пользовательские тексты SFT (role=user) ↔ поле prompt RL-задачи",
        "normalization": "lower + схлопнутые пробелы (norm из tools/check_eval_leakage.py)",
        "id_scheme": "id задачи — явное поле (id/task_id/slug), иначе line:<N> (1-based)",
        "rl_tasks": n_rl,
        "rl_unique_prompts": len(index),
        **res,
        "verdict": ("совпадения найдены — фильтрация данных решением архитектора "
                    "(AD-7: состав заморожен, инструмент данные не меняет)"
                    if res["sft_matched_prompts"] else "совпадений нет"),
    }
    discrepancy = None
    if args.expect_matched is not None:
        cross_ok = res["sft_matched_prompts"] == args.expect_matched
        payload["cross_check"] = {
            "expected_sft_matched_prompts": args.expect_matched,
            "actual_sft_matched_prompts": res["sft_matched_prompts"],
            "matched": cross_ok,
            "source": "известный замер, переданный флагом --expect-matched",
        }
        if not cross_ok:
            discrepancy = (f"ожидалось {args.expect_matched}, получено "
                           f"{res['sft_matched_prompts']}")
            payload["verdict"] += f"; РАСХОЖДЕНИЕ с известным замером: {discrepancy}"

    written = None
    if not args.no_evidence:
        written = merge_evidence(Path(args.evidence), payload)
    payload["evidence_written"] = written

    ok = res["sft_matched_prompts"] == 0 and discrepancy is None
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK if ok else EXIT_OVERLAP

    print("== S1 / AD-7: пересечение пулов SFT и RL ==")
    print(f"sft: {sft_path}  примеров: {res['sft_examples']}")
    print(f"rl:  {rl_path}  задач: {n_rl} (уникальных промптов: {len(index)})")
    print()
    print(f"совпадений: {res['sft_matched_prompts']} из {res['sft_examples']} промптов SFT "
          f"(пар sft↔rl: {res['sft_rl_pairs']}; RL-задач затронуто: {res['rl_matched_tasks']})")
    if res["sft_matched_prompts"]:
        print()
        print(f"поимённо (первые {len(res['matches'])}"
              f"{' из ' + str(res['sft_rl_pairs']) if res['matches_truncated'] else ''}):")
        for m in res["matches"]:
            print(f"  - sft {m['sft_id']} ↔ rl {m['rl_id']} "
                  f"[{m['rl_task_type']}] sha1 {m['normalized_sha1']}")
            print(f"      {m['prompt_preview']}")
    if "cross_check" in payload:
        cc = payload["cross_check"]
        print()
        print(f"кросс-проверка: известно {cc['expected_sft_matched_prompts']}, "
              f"получено {cc['actual_sft_matched_prompts']} — "
              f"{'совпало' if cc['matched'] else 'РАСХОЖДЕНИЕ'}")
    if written:
        print()
        print(f"evidence: {written} → поле sft_rl_overlap")
    print()
    if ok:
        print("SFT_RL_OVERLAP OK: совпадений не найдено")
        return EXIT_OK
    if discrepancy:
        print(f"SFT_RL_OVERLAP: РАСХОЖДЕНИЕ с известным замером ({discrepancy}) — "
              f"сигнал; данные не изменены (AD-7)")
    else:
        print("SFT_RL_OVERLAP: совпадения найдены — сигнал архитектору; данные не изменены (AD-7)")
    return EXIT_OVERLAP


if __name__ == "__main__":
    sys.exit(main())
