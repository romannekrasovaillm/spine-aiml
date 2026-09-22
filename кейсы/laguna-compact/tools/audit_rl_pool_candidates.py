#!/usr/bin/env python3
"""Факты о пулах-кандидатах в RL: путь, объём, происхождение, пересечение с SFT.

Зачем отдельный прибор. Вопрос «какой пул идёт в RL» решается владельцем (AD-2,
AD-8), и решение обязано опираться на **механически посчитанные** числа, а не на
память: у кандидатов разное происхождение, разные карточки AD-2 и разная степень
пересечения с обучающим набором. Прибор не выбирает пул и не фильтрует данные
(AD-7: состав заморожен) — он называет факты, на которых выбор делается.

Что считается по каждому кандидату:

* **объём и тождество файла** — строки, байты, полный ``sha256`` (не head-хеш:
  ADR-011 п.4 разрешает head-хеш только с явной пометкой, а решение по данным не
  должно опираться на усечённую меру);
* **происхождение** — поля-источники самой задачи (``source_env``, ``source``) и
  наличие сборщика в кейсе: происхождение, которого нет в репозитории, называется
  «неизвестно», а не додумывается;
* **карточка AD-2** — путь карточки кейса, если она есть; отсутствие карточки
  называется прямо (AD-2: прогон без манифеста не является доказательством);
* **пересечение с обучающим набором** — доля задач пула, чей ``prompt`` дословно
  (по нормализации гейта C-009: lower + схлопнутые пробелы) совпадает с
  пользовательским текстом SFT-примера. Методика берётся **импортом** из
  ``tools/check_eval_leakage.py`` (та же норма, что у C-009/C-017): своя копия
  нормы дала бы другой ответ на том же файле;
* **кросс-проверка с записанным замером** — если число по кандидату уже объявлено
  в evidence кейса, оно сверяется с фактом, и расхождение называется находкой, а
  не подгоняется (ADR-028 п.1: факт первичен).

Коды возврата::

    0 — факты собраны; записанные замеры сошлись (или их не было)
    1 — факты собраны, но есть расхождение: замер разошёлся с объявленным
        (файл изменился после объявления) либо заявленное закрепление пула не
        найдено в коде — решение по объявленному было бы решением по
        несуществующим данным (ADR-028 п.1)
    2 — NOT-VERIFIED: обучающий набор не прочитан — пересечение не измерено

Запуск::

    python3 tools/audit_rl_pool_candidates.py --plan
    python3 tools/audit_rl_pool_candidates.py --out evidence/rl-pool-candidates.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_eval_leakage import norm, read_jsonl, train_user_texts  # noqa: E402

EXIT_OK, EXIT_DIVERGENT, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent
DATASETS = CASE_ROOT / "datasets"

#: Обучающий набор стадии SFT — то, относительно чего меряется пересечение:
#: именно он идёт в обучение (S3av: вход стадии — параметр цепочки), поэтому
#: «пул пересекается с SFT» означает пересечение с этим файлом, а не с v12.
SFT_BASELINE = "datasets/sft_train_v13_fixed.jsonl"

#: Кандидаты. `origin` — то, что известно о сборке; `card` — карточка кейса.
#: Список ведётся здесь, а не выводится из каталога: в `datasets/` лежат десятки
#: файлов, и «все файлы, похожие на пул» — не список кандидатов, а свалка.
CANDIDATES: tuple[dict, ...] = (
    {"name": "v1 (пул ревизии, закреплён раннерами)",
     "file": "datasets/rl_tasks_revpool_v1.jsonl",
     "origin": "пул ревизии ADR-007; имя версии закреплено в раннерах",
     #: Закрепление ищется по образцу, а не хранится строкой с номером: номер
    #: строки растёт от любой правки выше, и «пин на строку 98» устаревает молча.
     "pinned_in": [{"file": "tools/run_pilot.py", "pattern": "rl_tasks_revpool_v1"},
                   {"file": "tools/run_rl_probe.py", "pattern": "rl_tasks_revpool_v1"},
                   {"file": "tools/pilot_chain.sh", "pattern": "rl_tasks_revpool_v1"}],
     "card": None,
     "recorded_overlap": None},
    {"name": "v2 (пул ревизии, лейк снят)",
     "file": "datasets/rl_tasks_revpool_v2.jsonl",
     "origin": "пул ревизии v2 (ADR-021); лейк-фильтр S3f-fix; канонический носитель — "
               "файл (ADR-049 п.7), карточка приведена к факту дельтой pool-v2-reconcile",
     "pinned_in": [],
     "card": "evidence/s3p-pool-v2-card.json",
     "recorded_overlap": None},
    {"name": "ox-merged (RL-задачи + учитель, суффикс merged)",
     "file": "datasets/rl_tasks_ox_merged.jsonl",
     "origin": "НЕИЗВЕСТНО: сборщик merged-файлов не найден ни в репозитории, ни на "
               "gb10-shared (карточка ox-merged-datasets, provenance.resolution — "
               "разложение по источникам, builder — «неизвестен»)",
     "pinned_in": [],
     "card": "data/ox-merged-card.json",
     "recorded_overlap": "evidence/ox-datasets-audit.json"},
    {"name": "ox (исходный набор ox-серии)",
     "file": "datasets/rl_tasks_ox.jsonl",
     "origin": "ox-серия (учитель stealth/ox-alpha, генерация 24–26.08.2026), "
               "источник merged-файла",
     "pinned_in": [],
     "card": "data/ox-merged-card.json",
     "recorded_overlap": None},
    {"name": "oxalpha (исторический пул разведки)",
     "file": "datasets/rl_tasks_oxalpha.jsonl",
     "origin": "исторический пул контура (до ревизии); на нём шёл прогон "
               "v12_qwen25-05b_s42 (28 000 задач)",
     "pinned_in": [],
     "card": None,
     "recorded_overlap": None},
    {"name": "env_types (среды E-серии, 700 задач)",
     "file": "datasets/rl_tasks_env_types.jsonl",
     "origin": "задачи с полями max_steps/n_min_steps (среды E-серии); источник "
               "merged-файла",
     "pinned_in": [],
     "card": "data/ox-merged-card.json",
     "recorded_overlap": None},
)


def sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def sft_user_texts(path: Path) -> tuple[set[str], int]:
    """Нормализованные пользовательские тексты SFT и число примеров."""
    texts: set[str] = set()
    n = 0
    for _, ex in read_jsonl(path):
        n += 1
        for t in train_user_texts(ex):
            key = norm(t)
            if key:
                texts.add(key)
    return texts, n


def measure_candidate(path: Path, sft_texts: set[str]) -> dict:
    n = 0
    matched = 0
    by_type: dict[str, int] = {}
    by_type_matched: dict[str, int] = {}
    for _, ex in read_jsonl(path):
        n += 1
        tt = str(ex.get("task_type") or "?")
        by_type[tt] = by_type.get(tt, 0) + 1
        if norm(ex.get("prompt")) in sft_texts:
            matched += 1
            by_type_matched[tt] = by_type_matched.get(tt, 0) + 1
    return {
        "tasks": n,
        "matched_tasks": matched,
        "overlap_share": round(matched / n, 4) if n else None,
        "by_task_type": by_type,
        "matched_by_task_type": by_type_matched,
    }


def resolve_pins(case_root: Path, pins: list) -> tuple[list[str], list[dict]]:
    """Закрепления пула: где имя версии действительно встречается — по факту файла.

    Возвращает найденные ``file:N`` и **не найденные** заявки. Второе важнее
    первого: «заявлено закрепление, а в файле его нет» — это факт, который иначе
    читался бы как «пул закреплён» (объявление отстало от кода).
    """
    found: list[str] = []
    missing: list[dict] = []
    for pin in pins:
        p = case_root / pin["file"]
        if not p.is_file():
            missing.append({**pin, "why": "файла нет"})
            continue
        hits = [i for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace")
                                           .splitlines(), start=1)
                if pin["pattern"] in line]
        if hits:
            found.extend(f"{pin['file']}:{h}" for h in hits)
        else:
            missing.append({**pin, "why": "образец не найден в файле"})
    return found, missing


def recorded_overlap(case_root: Path, evidence_rel: str) -> dict | None:
    """Записанный замер пересечения — для сверки, а не как источник истины."""
    p = case_root / evidence_rel
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ov = d.get("sft_rl_overlap") or {}
    if not ov:
        return None
    return {"evidence": evidence_rel,
            "rl_tasks": ov.get("rl_tasks"), "rl_matched_tasks": ov.get("rl_matched_tasks"),
            "sft": ov.get("sft"),
            "tolerance": ov.get("tolerance")}


def build(case_root: Path, sft_rel: str) -> dict:
    sft_path = case_root / sft_rel
    if not sft_path.is_file():
        raise FileNotFoundError(f"обучающий набор не найден: {sft_path}")
    sft_texts, n_sft = sft_user_texts(sft_path)
    facts: list[dict] = []
    divergences: list[dict] = []
    for cand in CANDIDATES:
        pinned, pins_missing = resolve_pins(case_root, cand.get("pinned_in") or [])
        cand = {**cand, "pinned_in": pinned}
        if pins_missing:
            divergences.append({"candidate": cand["name"], "evidence": "код кейса",
                                "recorded_matched_tasks": None, "measured_matched_tasks": None,
                                "why": f"заявленное закрепление не найдено: {pins_missing}",
                                "kind": "pin_not_found"})
        p = case_root / cand["file"]
        if not p.is_file():
            facts.append({**cand, "exists": False})
            continue
        measured = measure_candidate(p, sft_texts)
        rec = (recorded_overlap(case_root, cand["recorded_overlap"])
               if cand.get("recorded_overlap") else None)
        if rec and rec.get("rl_matched_tasks") is not None \
                and rec["rl_matched_tasks"] != measured["matched_tasks"]:
            divergences.append({
                "candidate": cand["name"], "evidence": rec["evidence"],
                "recorded_matched_tasks": rec["rl_matched_tasks"],
                "measured_matched_tasks": measured["matched_tasks"],
                "why": ("файл изменился после объявления либо замер сделан на другом "
                        "наборе SFT — решение по объявленному числу было бы решением "
                        "по несуществующим данным (ADR-028 п.1)")})
        st = p.stat()
        facts.append({
            **cand,
            "exists": True,
            "ssot": str(p.resolve()),
            "bytes": st.st_size,
            "sha256": sha256_file(p),
            "is_symlinked_from_case": p.is_symlink() or DATASETS.is_symlink(),
            **measured,
            "recorded_overlap": rec,
            "leak_status": ("пересечение с обучающим набором найдено — фильтр/решение "
                            "архитектора (AD-7: состав заморожен)"
                            if measured["matched_tasks"] else "совпадений промптов нет"),
        })
    return {
        "tool": "tools/audit_rl_pool_candidates.py",
        "purpose": ("факты о кандидатах в пул RL — вход в вопрос владельцу "
                    "(docs/specs/RL-POOL-QUESTION.md); прибор ничего не выбирает "
                    "и не фильтрует (AD-7)"),
        "sft_baseline": {"path": sft_rel, "examples": n_sft,
                         "distinct_user_texts": len(sft_texts),
                         "sha256": sha256_file(sft_path)},
        "method": {
            "overlap": ("нормализованный prompt RL-задачи ∈ множество нормализованных "
                        "пользовательских текстов SFT (role=user); нормализация — "
                        "norm() из tools/check_eval_leakage.py (lower + схлопнутые "
                        "пробелы), та же, что у гейтов C-009/C-017"),
            "hash": "полный sha256 файла (не head-хеш: ADR-011 п.4)",
        },
        "candidates": facts,
        "divergences": divergences,
        "limits": [
            "пересечение считается по дословному совпадению промпта; смысловое "
            "совпадение (перефраз) здесь не измеряется — у него другой прибор",
            "происхождение merged-файлов названо «неизвестно»: сборщик не найден; "
            "это факт, а не пробел прибора",
            "какой пул идёт в RL — решение владельца (AD-2/AD-8); прибор его не "
            "принимает и не рекомендует",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Факты о пулах-кандидатах в RL: объём, происхождение, карточка AD-2, "
                    "пересечение с обучающим набором SFT.")
    ap.add_argument("--sft", default=SFT_BASELINE, help="обучающий набор (по умолчанию v13_fixed)")
    ap.add_argument("--out", help="куда записать evidence (json)")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    ap.add_argument("--plan", action="store_true", help="напечатать состав кандидатов и выйти")
    args = ap.parse_args(argv)

    if args.plan:
        print("== кандидаты в пул RL (факты считает прибор, выбор делает владелец) ==")
        for c in CANDIDATES:
            print(f"  {c['name']:<48} {c['file']}")
            print(f"      карточка AD-2: {c['card'] or '— нет'}; закреплён: "
                  f"{', '.join(c['pinned_in']) or '—'}")
        print(f"  пересечение мерится против {args.sft}")
        return EXIT_OK

    try:
        report = build(CASE_ROOT, args.sft)
    except FileNotFoundError as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    rc = EXIT_DIVERGENT if report["divergences"] else EXIT_OK
    if args.out:
        out = CASE_ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"факты о пулах записаны: {args.out}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"SFT-база: {report['sft_baseline']['path']} "
              f"({report['sft_baseline']['examples']} примеров, "
              f"{report['sft_baseline']['distinct_user_texts']} различных user-текстов)")
        for c in report["candidates"]:
            if not c.get("exists"):
                print(f"  {c['name']:<48} НЕТ ФАЙЛА")
                continue
            print(f"  {c['name']:<48} {c['tasks']:>6} задач  "
                  f"пересечение {c['matched_tasks']:>5} "
                  f"({(c['overlap_share'] or 0) * 100:5.2f} %)  "
                  f"карточка AD-2: {'да' if c['card'] else 'НЕТ'}")
        for d in report["divergences"]:
            print(f"  РАСХОЖДЕНИЕ с {d['evidence']}: объявлено {d['recorded_matched_tasks']}, "
                  f"измерено {d['measured_matched_tasks']} ({d['candidate']})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
