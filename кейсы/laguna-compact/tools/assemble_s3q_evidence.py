#!/usr/bin/env python3
"""S3q — свод evidence расширенного измерительного набора (ADR-025 п.2/п.4/п.5).

Зачем свод отдельным шагом. Числа S3q рождаются в трёх местах, и каждое пишет свой
отчёт: сборщик — карточку набора (что собрано и как), гейт — матрицу пересечений
(чем набор чист), проба — базу PPL (какую шкалу прибор даёт). Контракт дельты
(`evidence/s3q-eval-set.json`) требует их **вместе**, и склеивать их руками нельзя:
руками не проверяется, что во все три отчёта попал **один и тот же файл набора**.

Что здесь проверяется, а не пересказывается:

* **один набор во всех трёх отчётах** — sha256 набора из карточки, из гейта и из
  пробы обязаны совпасть; расхождение — отказ, а не «свод по последнему»;
* **исходные наборы не изменены** — sha256 ``general_eval.txt`` и
  ``general_eval_v2.txt`` сверяются с историческими значениями из
  ``evidence/s3m-ppl-arms.json`` (тот самый замер, в котором дефект v2 и нашёлся).
  Это и есть проверяемое утверждение «не трогали», а не обещание;
* **гейт пройден** — в свод попадает ``leak_check`` в форме ADR-025 п.2
  (``{dataset_sha256, overlap_docs, overlap_ngram_windows, window_size, verdict}``),
  чтобы калибровочные дельты ссылались на него, а не собирали свой.

Коды возврата::

    0 — свод собран
    1 — отказ: отчёты противоречат друг другу (разные наборы, набор нечист)
    2 — NOT-VERIFIED: нет одного из входных отчётов

Запуск::

    python3 tools/assemble_s3q_evidence.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

CARD = "data/gen-eval-v3-card.json"
PURITY = "evidence/s3q-purity.json"
BASELINE = "evidence/s3q-baseline-v3.json"
OUT = "evidence/s3q-eval-set.json"

#: Исторический замер, в котором дефект v2 и был найден: из него берутся и хеши
#: исходных наборов (для сверки «не изменены»), и старая база (для пары чисел).
ARMS = "evidence/s3m-ppl-arms.json"

#: Файлы, которых эта дельта не касается. Сверка по хешу — единственная проверка
#: «не изменены», которую нельзя выполнить «на глаз».
UNTOUCHED = {
    "v1_general": "datasets/general_eval.txt",
    "v2_general": "datasets/general_eval_v2.txt",
}

#: Каталог, куда положил отчёты перемер рук (см. ``remeasured_arms``).
ARMS_DIR = "runs/s3q-arms-20260916"

#: Следующая дельта, названная явно (ADR-023 п.7: не «когда-нибудь потом»).
NEXT_STEP = (
    "Разрешить вопрос жанра: перемер рук показал, что вердикт о потолке зависит "
    "от того, из какого жанра измерительный набор (`remeasured_arms.genre_question`). "
    "Дискриминирующий опыт назван там же и стоит одного прогона: набор общего языка "
    "вне жанра реплея (~200 документов не-википедийной русской прозы) и тот же "
    "перемер на нём. До этого вердикт «руки проходят потолок» читать нельзя — "
    "контрольные руки S3o (C1, 100 % общего языка) на новом наборе тоже не "
    "перемерялись: их чекпойнтов на момент дельты ещё нет")

OPEN_QUESTIONS = [
    "Считать ли гейт ADR-025 п.2 обязательным для уже сделанных замеров "
    "ретроспективно: числа S3m-2 на v1_general лежат в наборе, который чист "
    "(подтверждено гейтом), но их сверка с потолком шла до пересчёта — "
    "перечитывать ли вердикт «ни одна рука не проходит» на новом потолке",
    "Что считать мерой «общего языка»: язык того класса, который защищал реплей "
    "(тогда верен v3, и вывод S3m-2 о разрушении языка относится к прозе вне "
    "жанра), или язык вообще, вне обучающего распределения (тогда нужен второй "
    "набор общего языка вне жанра реплея, и вердикт о потолке читается только по "
    "нему). Перемер рук показал, что вердикт зависит от этого выбора: на v3 "
    "потолок проходят три руки из четырёх, на v1 — ни одна. Инвариант ADR-025 п.1 "
    "(нулевое пересечение документов) выполнен в обоих случаях и этот вопрос не "
    "закрывает: он про документы, а не про распределение",
    "Что делать с наборами v2: ADR-025 п.3 объявляет их условно валидными "
    "(годны для миксов без general_replay_ru в реплее), но таких кандидатных "
    "миксов сейчас нет ни одного — все три содержат этот источник. Оставлять "
    "набор в реестре «условно годным» при отсутствии годного микса или "
    "переводить в invalid_for всех",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def load(root: Path, rel: str) -> dict | None:
    p = root / rel
    if not p.is_file():
        print(f"NOT-VERIFIED: нет отчёта {rel}", file=sys.stderr)
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def arms_remeasured(root: Path, new_ppl: float, old_ppl: float) -> dict:
    """Перемер четырёх калибровочных рук на новом наборе (ADR-025, «Отрицательные»).

    Отсутствие отчётов — не отказ свода: перемер назван опциональным в задании
    дельты («если остаётся время»), и свод обязан собираться без него, честно
    помечая `status`. Но когда отчёты есть, они читаются **файлами**, а не
    пересказываются: числа в отчёте и в своде обязаны быть одним числом.
    """
    d = root / ARMS_DIR
    reports = sorted(p for p in d.glob("*.json") if not p.stem.endswith("-ml192")) \
        if d.is_dir() else []
    if not reports:
        return {
            "status": "not_done",
            "why": "отчётов перемера нет в " + ARMS_DIR,
            "what_carries_over": (
                "вывод S3m-2 «все четыре руки провалили порог с большим запасом» "
                "остаётся верным как снятый на чистом наборе v1_general (гейт это "
                "подтверждает); не переносятся величины отношений ×4.63…×9.48 и их "
                "сопоставление с новым потолком — другой набор, другая шкала"),
        }

    arms = []
    for path in reports:
        rep = json.loads(path.read_text(encoding="utf-8"))
        try:
            fin = rep["states"]["final"]["sets"]
            base_final = rep["states"]["base"]["sets"]
        except KeyError as exc:
            return {"status": "incomplete", "why": f"{path.name}: нет {exc}"}
        new = fin["v3_general"]["ppl"]
        old = fin["v1_general"]["ppl"]
        arm = rep["instrument"]["pipeline"].split("/")[1].replace("calib-", "").split("-2026")[0]
        arms.append({
            "arm": arm,
            "report": str(path.relative_to(root)),
            "checkpoint": rep["states"]["final"]["load"].get("checkpoint"),
            "ppl_new_set": new,
            "ppl_old_set": old,
            "base_new_set": base_final["v3_general"]["ppl"],
            "base_old_set": base_final["v1_general"]["ppl"],
            "ratio_new_set": new / base_final["v3_general"]["ppl"],
            "ratio_old_set": old / base_final["v1_general"]["ppl"],
            "passes_new_ceiling": new <= 2.0 * base_final["v3_general"]["ppl"],
        })
    passing = [a["arm"] for a in arms if a["passes_new_ceiling"]]

    # Диагностика длины: те же документы, обрезанные до 192 токенов. Она отделяет
    # «длинный контекст лечит повреждённую модель» от «модель знакома с жанром».
    diag = []
    for path in sorted(d.glob("*-ml192.json")):
        rep = json.loads(path.read_text(encoding="utf-8"))
        fin, base = rep["states"]["final"]["sets"], rep["states"]["base"]["sets"]
        diag.append({
            "arm": path.stem.replace("-ml192", ""),
            "max_len": rep["instrument"].get("max_len") or 192,
            "base_v3": base["v3_general"]["ppl"], "arm_v3": fin["v3_general"]["ppl"],
            "ratio_v3": fin["v3_general"]["ppl"] / base["v3_general"]["ppl"],
            "base_v1": base["v1_general"]["ppl"], "arm_v1": fin["v1_general"]["ppl"],
            "ratio_v1": fin["v1_general"]["ppl"] / base["v1_general"]["ppl"],
            "instrument_check": next((c["verdict"] for c in rep.get("checks", [])
                                      if c["name"] == "base_vs_s3h"), None),
        })

    return {
        "status": "done",
        "reports": str(ARMS_DIR),
        "arms": arms,
        "arms_passing_new_ceiling": passing,
        "new_ceiling": 2.0 * new_ppl,
        "genre_question": {
            "what": (
                "вердикт о потолке зависит от жанра измерительного набора: на новом "
                "наборе (русская Википедия — тот же жанр и распределение, что у "
                "реплей-части микса) проходят потолок "
                f"{len(passing)} руки из {len(arms)}, а на v1_general (авторская "
                "проза вне этого жанра) те же чекпойнты дают ×4.6…×9.5"),
            "diagnostic": (
                "длина контекста проверена отдельно: те же документы, обрезанные до "
                "192 токенов, дают то же отношение (см. diagnostics_max_len_192) — "
                "значит дело не в длине документа, а в жанре/распределении"),
            "diagnostics_max_len_192": diag,
            "two_readings": [
                ("набор верен: реплей защищает именно общий русский язык того же "
                 "класса, что в нём самом, и перемер показывает, что руки этот язык "
                 "сохраняют — тогда вывод S3m-2 о разрушении языка относится к прозе "
                 "вне жанра, а не к языку вообще"),
                ("набор снисходителен: совпадение жанра с обучающим материалом даёт "
                 "рукам преимущество без совпадения документов (гейт ADR-025 п.1 "
                 "такое не ловит — он про документы, не про распределение)"),
            ],
            "not_a_conflict_with_ADR_025": (
                "ADR-025 п.1 требует нулевого пересечения ДОКУМЕНТОВ и окон; он "
                "выполнен. Обнаруженное — следующий по тонкости дефект прибора: "
                "набор может быть документно чистым и при этом жанрово совпадать с "
                "обучением. Решение о том, что считать мерой «общего языка», — "
                "архитектурное, и оно не принято этой дельтой"),
        },
        "what_carries_over": (
            "числа S3m-2 на v1_general остаются верными для своего набора; "
            "«все руки провалили порог» — для прозы вне жанра реплея"),
    }


def build(root: Path) -> tuple[dict, int]:
    card = load(root, CARD)
    purity = load(root, PURITY)
    baseline = load(root, BASELINE)
    arms = load(root, ARMS)
    if card is None or purity is None or baseline is None or arms is None:
        return {}, EXIT_NOT_VERIFIED

    set_sha = card["set"]["sha256"]
    if purity["set"]["sha256"] != set_sha:
        print(f"ОТКАЗ: гейт проверял другой набор ({purity['set']['sha256'][:12]} "
              f"против {set_sha[:12]} в карточке)", file=sys.stderr)
        return {}, EXIT_FAIL
    if baseline["baseline"]["new_set_sha256"] != set_sha:
        print(f"ОТКАЗ: проба меряла другой набор "
              f"({baseline['baseline']['new_set_sha256'][:12]} против {set_sha[:12]})",
              file=sys.stderr)
        return {}, EXIT_FAIL
    if purity["verdict"] != "clean":
        print("ОТКАЗ: набор не прошёл гейт чистоты — свод не собирается", file=sys.stderr)
        return {}, EXIT_FAIL

    # ── исходные наборы: сверка с историческими хешами ───────────────────────
    historical = {name: info["sha256"] for name, info in arms["datasets"].items()
                  if name in UNTOUCHED}
    untouched = {}
    for name, rel in UNTOUCHED.items():
        p = root / rel
        now = sha256_file(p) if p.is_file() else None
        untouched[name] = {
            "path": rel, "sha256_now": now, "sha256_at_s3m": historical.get(name),
            "unchanged": now == historical.get(name),
        }
    if not all(v["unchanged"] for v in untouched.values()):
        print("ОТКАЗ: исходный набор изменился — расширение шло не отдельным файлом",
              file=sys.stderr)
        return {}, EXIT_FAIL

    b = baseline["baseline"]
    evidence = {
        "schema": "s3q-eval-set/1",
        "stage": "S3q",
        "status": "complete",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": ("расширенный измерительный набор общего языка (ADR-025 п.4), "
                    "доказательство его чистоты против всех кандидатных миксов "
                    "(ADR-025 п.1) и новая база PPL с пересчитанным потолком"),
        "adr": ["ADR-025", "ADR-022 п.3", "ADR-018 п.3", "ADR-011 п.4", "AD-11"],
        "artifacts": {
            "set": card["set"]["path"],
            "card": CARD,
            "gate": "tools/check_eval_set_purity.py",
            "gate_report": PURITY,
            "builder": "tools/build_general_eval_v3.py",
            "probe": "tools/ppl_probe_v3.py",
            "probe_report": BASELINE,
            "registry": "docs/specs/EVAL-SETS.md",
            "assembler": "tools/assemble_s3q_evidence.py",
        },
        "set": {
            "path": card["set"]["path"],
            "sha256": set_sha,
            "bytes": card["set"]["bytes"],
            "docs": card["set"]["docs"],
            "tokens": card["set"]["tokens"],
            "tokens_counted_by_probe": baseline["ppl"]["v3_general"]["tokens"],
            "tokens_note": ("в наборе сумма len(encode(doc)) = "
                            f"{card['set']['tokens']}; прибор считает предсказанные "
                            "позиции (len-1 на документ) = "
                            f"{baseline['ppl']['v3_general']['tokens']}; разница "
                            f"ровно {card['set']['docs']} — по одной на документ"),
            "source": {
                "dataset": card["source"]["dataset"],
                "config": card["source"]["config"],
                "split": card["source"]["split"],
                "selection": ("хвост потока того же дампа, что и реплей; "
                              "детерминированная выборка из объявленного окна "
                              "смещений по сиду"),
                "window": card["source"]["window"],
                "license": card["source"]["license"],
            },
            "doc_chars": {"min": card["set"]["doc_chars_min"],
                          "max": card["set"]["doc_chars_max"]},
            "doc_tokens": {"min": card["set"]["doc_tokens_min"],
                           "max": card["set"]["doc_tokens_max"]},
        },
        "build": {
            "filters": card["filters"],
            "selection": card["selection"],
            "rejected_by_purity": card["purity"]["rejected_candidates"],
            "purity_scan": card["purity"]["scan"],
        },
        "overlap_matrix": purity["overlap_matrix"],
        "leak_check": {
            "dataset_sha256": set_sha,
            "overlap_docs": max(m["overlap_docs"] for m in purity["overlap_matrix"]),
            "overlap_ngram_windows": max(m["overlap_ngram"] for m in purity["overlap_matrix"]),
            "window_size": purity["instrument"]["window_size"],
            "verdict": purity["verdict"],
            "checked_mixes": [m["mix"] for m in purity["overlap_matrix"]],
            "checked_at": purity["date"],
            "gate_report": PURITY,
            "note": ("форма ADR-025 п.2: применение потолка к руке разрешено только "
                     "при нулях здесь; проверка идёт по корпусу-источнику целиком, "
                     "что строже проверки по обученному префиксу микса"),
        },
        "baseline": {
            "new_ppl": b["new_ppl"], "new_ceiling": b["new_ceiling"],
            "old_ppl": b["old_ppl"], "old_ceiling": b["old_ceiling"],
            "old_set": b["old_set"],
            "v1_remeasured_ppl": b["v1_remeasured_ppl"],
            "v1_rel_delta_pct": b["v1_rel_delta_pct"],
            "v1_reproduced": b["v1_reproduced"],
            "instrument": baseline["instrument"]["measure"],
            "probe_revision_sha256": baseline["instrument"]["probe_revision"]["sha256"],
            "tokenizer_control_delta_pct": baseline["tokenizer_control"].get("delta_pct"),
            "comparability": baseline["comparability"],
        },
        "sources_unchanged": untouched,
        "remeasured_arms": arms_remeasured(root, b["new_ppl"], b["old_ppl"]),
        "next_step": NEXT_STEP,
        "open_questions": OPEN_QUESTIONS,
    }
    return evidence, EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S3q: свод evidence измерительного набора")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--case-root", default=None,
                    help="корень кейса (по умолчанию — каталог этого инструмента)")
    ap.add_argument("--json", action="store_true", help="свод в stdout")
    args = ap.parse_args(argv)
    root = Path(args.case_root).resolve() if args.case_root else CASE_ROOT
    evidence, rc = build(root)
    if not evidence:
        return rc
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"свод: {out}")
    print(f"  набор: {evidence['set']['sha256'][:16]}  документов "
          f"{evidence['set']['docs']}  токенов {evidence['set']['tokens']}")
    print(f"  гейт: {evidence['leak_check']['verdict']} "
          f"(документов {evidence['leak_check']['overlap_docs']}, окон "
          f"{evidence['leak_check']['overlap_ngram_windows']})")
    print(f"  база {evidence['baseline']['new_ppl']:.6f} → потолок "
          f"{evidence['baseline']['new_ceiling']:.6f}")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
