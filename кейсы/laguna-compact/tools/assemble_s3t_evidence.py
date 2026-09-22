#!/usr/bin/env python3
"""S3t — свод evidence компоненты K2: набор, гейт, база, перемер рук по двум компонентам.

Зачем свод отдельным шагом. Числа S3t рождаются в четырёх местах, и каждое пишет
свой отчёт: сборщик — карточку набора (что собрано и как), гейт — матрицу
пересечений (чем набор чист), проба — базу и потолок K2 (какую шкалу прибор даёт),
перемер — отношения четырёх рук по обеим компонентам. Контракт дельты требует их
**вместе**, и склеивать руками нельзя: руками не проверяется, что во все отчёты
попал **один и тот же файл набора**.

Что здесь проверяется, а не пересказывается:

* **один набор во всех отчётах** — sha256 набора из карточки, из гейта и из пробы
  обязаны совпасть; расхождение — отказ, а не «свод по последнему»;
* **наборы ревизии не изменены** — sha256 ``general_eval.txt``, ``general_eval_v2.txt``,
  ``general_eval_v3.txt`` и ``general_replay_ru.txt`` сверяются с историческими
  значениями из ``evidence/s3m-ppl-arms.json`` и ``evidence/s3q-eval-set.json``.
  Это и есть проверяемое утверждение «не трогали», а не обещание;
* **гейт пройден** — в свод попадает ``leak_check`` в форме ADR-025 п.2
  (``{dataset_sha256, overlap_docs, overlap_ngram_windows, window_size, verdict}``);
* **тождество прибора** — свод не собирается, если эталоны v1/v3 не воспроизвелись
  в том же прогоне, что и K2 (ADR-027 п.2: сравнение компонент осмысленно только
  на одном приборе);
* **конъюнкция считается здесь**, а не в отчёте человека: проход руки — это проход
  **обеих** компонент (ADR-027 п.1). Компоненты не усредняются: свод хранит
  k1_ratio и k2_ratio раздельно и отдельно их конъюнкцию.

Коды возврата::

    0 — свод собран
    1 — отказ: отчёты противоречат друг другу (разные наборы, набор нечист,
        прибор не воспроизвёл эталон)
    2 — NOT-VERIFIED: нет одного из входных отчётов

Запуск::

    python3 tools/assemble_s3t_evidence.py
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

CARD = "data/gen-eval-k2-card.json"
PURITY = "evidence/s3t-purity-k2.json"
BASELINE = "evidence/s3t-baseline-k2.json"
OUT = "evidence/s3t-k2-set.json"

#: Исторические отчёты, из которых берутся хеши «неизменных» наборов и опорные
#: числа. Сверка идёт с ними, а не с числами, вписанными в этот файл.
ARMS_S3M = "evidence/s3m-ppl-arms.json"
LEDGER_S3Q = "evidence/s3q-eval-set.json"

#: Наборы и корпус, которых эта дельта не касается. Сверка по хешу — единственная
#: проверка «не изменены», которую нельзя выполнить «на глаз». Пути — от корня
#: кейса: корпус реплея лежит на сетевом диске и подключён симлинком ``datasets/``
#: (AD-4), поэтому в кейсе он виден тем же относительным путём.
UNTOUCHED = {
    "v1_general": ("datasets/general_eval.txt", "s3m"),
    "v2_general": ("datasets/general_eval_v2.txt", "s3m"),
    "v3_general": ("datasets/general_eval_v3.txt", "s3q"),
    "replay_corpus": ("datasets/general_replay_ru.txt", "s3q"),
}

#: Каталог с отчётами перемера рук и контрольных рук по двум компонентам.
ARMS_DIR = "runs/s3t-arms-20260916"

#: Допуск согласия базы в отчёте руки с базой пробы. Оба числа снимает один и тот
#: же код на одной машине; расхождение может быть только сменой прибора, и тогда
#: отношение руки считается от другой базы — это отказ, а не «почти то же».
BASE_AGREEMENT_TOL = 1e-6

#: Четыре руки S3m (чекпойнты ``checkpoint_final.pt``), которые уже мерились на K1.
ARMS_S3M_EXPECTED = ["25-0.35", "25-0.7", "50-0.35", "50-0.7"]

#: Контрольные руки S3o: их чекпойнтов на момент дельты может ещё не быть, и это
#: не отказ свода, а названный следующий шаг (ADR-023 п.7).
ARMS_S3O_EXPECTED = ["ctrl-C1-100-0.35", "ctrl-C2-25-0.035"]

NEXT_STEP = (
    "Перемерить контрольные руки S3o (`ctrl-C1-100-0.35`, `ctrl-C2-25-0.035`) по "
    "обеим компонентам: на момент S3t их чекпойнты ещё писались (C1 — на шаге "
    "1000/2000, C2 — не стартовала). C1 — единственная рука без домена вовсе "
    "(100 % общего языка), и по ней видно, что даёт потолок без доменной части; "
    "её отсутствие в таблице — не «шум», а незакрытая клетка конъюнкции. "
    "Отдельно: решить вопрос жанра K2 — набор собран из аудиторско-бюджетной "
    "прозы, потому что другого корпуса другого жанра в контуре нет; список "
    "вариантов и их лицензионный статус — в open_questions"
)

OPEN_QUESTIONS = [
    "Жанр K2 — решение архитектора, а не следствие разведки: в контуре нашёлся "
    "ровно один корпус русской прозы другого жанра (выгрузка бюллетеней Счётной "
    "палаты РФ, официально-аналитическая проза). ADR-027 требует «другого жанра, "
    "не представленного в обучении» — этому требованию корпус удовлетворяет, но "
    "перечень «научпоп, новости, художественная проза» в ADR-027 он не покрывает: "
    "ни новостей, ни художественной прозы, ни научпопа вне домена в контуре нет. "
    "Смена жанра = одна пересборка (сборщик параметризован `--source`) плюс "
    "повторный перемер; до решения архитектора числа K2 читаются как «обобщение "
    "на аудиторско-бюджетную прозу», а не как «обобщение вообще»",
    "Лицензионный статус K2 назван как `internal_only` (ADR-005/AD-8): файла "
    "лицензии у выгрузки нет, публикация производных запрещена. Если архитектор "
    "выберет внешний источник, статус придётся назвать заново: у Wikimedia — "
    "CC BY-SA (как у реплея), у новостных корпусов — обычно NC/ND, и это меняет "
    "не измерение, а допустимость выноса артефактов за периметр",
    "Различимость K2 против K1: если база K2 окажется много выше базы K1, потолок "
    "2× станет «широким» и будет пропускать руки, которые на K1 валятся. Это не "
    "дефект набора, а свойство шкалы: 2× — правило ADR-022 п.3, и его отношение к "
    "наборам разной трудности архитектором не пересматривалось",
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


def historic_hashes(root: Path) -> dict[str, str]:
    """Хеши наборов и корпуса из исторических отчётов — по имени набора."""
    out: dict[str, str] = {}
    s3m = load(root, ARMS_S3M) or {}
    for name, info in (s3m.get("datasets") or {}).items():
        if isinstance(info, dict) and info.get("sha256"):
            out[name] = info["sha256"]
    ledger = load(root, LEDGER_S3Q) or {}
    node = (ledger.get("set") or {}).get("sha256")
    if node:
        out["v3_general"] = node
    replay = ((ledger.get("build") or {}).get("purity_scan") or {}).get("replay") or {}
    if replay.get("sha256"):
        out["replay_corpus"] = replay["sha256"]
    return out


def arms_remeasured(root: Path, k1_ppl: float, k2_ppl: float,
                    k1_ceiling: float, k2_ceiling: float) -> dict:
    """Перемер рук по двум компонентам: отношения, проходы и **конъюнкция**.

    Отсутствие отчётов — не отказ свода: перемер назван в задании дельты, но свод
    обязан собираться и без него, честно помечая `status`. Когда отчёты есть, они
    читаются **файлами**, а не пересказываются: числа в отчёте и в своде обязаны
    быть одним числом.
    """
    d = root / ARMS_DIR
    reports = sorted(d.glob("*.json")) if d.is_dir() else []
    reports = [p for p in reports if p.name != "run_manifest.json"]
    if not reports:
        return {
            "status": "not_done",
            "why": f"отчётов перемера нет в {ARMS_DIR}",
            "what_carries_over": (
                "числа S3q на K1 (три руки из четырёх в потолке) остаются верными "
                "для своего набора; переносится вывод, а не величины: K2 — другая "
                "шкала, и её потолок считался от её собственной базы"),
        }

    arms = []
    for path in reports:
        rep = json.loads(path.read_text(encoding="utf-8"))
        try:
            fin = rep["states"]["final"]["sets"]
            base = rep["states"]["base"]["sets"]
        except KeyError as exc:
            return {"status": "incomplete", "why": f"{path.name}: нет {exc}"}
        pipeline = rep["instrument"]["pipeline"]
        arm = pipeline.split("/")[1].replace("calib-", "").split("-2026")[0]
        #: База в отчёте руки обязана совпасть с базой пробы: отношение считается
        #: от **той же** базы, иначе числа руки и потолок стоят на разных шкалах
        #: (ровно тот дефект, от которого страхует ADR-027 п.2).
        for set_name, probe_ppl in (("v3_general", k1_ppl), ("k2_general", k2_ppl)):
            got = base[set_name]["ppl"]
            rel = abs(got - probe_ppl) / max(abs(probe_ppl), 1e-12)
            if rel > BASE_AGREEMENT_TOL:
                print(f"ОТКАЗ: {arm}: база {set_name} в отчёте руки {got!r} не совпала "
                      f"с базой пробы {probe_ppl!r} (Δ {rel:.2e}) — отношение "
                      f"считалось бы от другой базы", file=sys.stderr)
                return {"status": "refused",
                        "why": f"{arm}: база {set_name} расходится с пробой"}
        k1_ratio = fin["v3_general"]["ppl"] / base["v3_general"]["ppl"]
        k2_ratio = fin["k2_general"]["ppl"] / base["k2_general"]["ppl"]
        k1_pass = fin["v3_general"]["ppl"] <= k1_ceiling
        k2_pass = fin["k2_general"]["ppl"] <= k2_ceiling
        arms.append({
            "arm": arm,
            "report": str(path.relative_to(root)),
            "checkpoint": (rep["states"]["final"].get("load") or {}).get("checkpoint"),
            "k1_ppl": fin["v3_general"]["ppl"],
            "k1_base": base["v3_general"]["ppl"],
            "k1_ratio": k1_ratio,
            "k1_pass": k1_pass,
            "k2_ppl": fin["k2_general"]["ppl"],
            "k2_base": base["k2_general"]["ppl"],
            "k2_ratio": k2_ratio,
            "k2_pass": k2_pass,
            "conjunction": bool(k1_pass and k2_pass),
            "diagnosis": (
                "реплей держит свой жанр, обобщение потеряно" if k1_pass and not k2_pass
                else "обобщение сохранено, свой жанр не удержан" if k2_pass and not k1_pass
                else "обе компоненты пройдены" if k1_pass and k2_pass
                else "обе компоненты провалены"),
        })
    arms.sort(key=lambda a: a["arm"])

    passing = [a["arm"] for a in arms if a["conjunction"]]
    both_fail = [a["arm"] for a in arms if not a["k1_pass"] and not a["k2_pass"]]
    missing = [a for a in ARMS_S3M_EXPECTED if a not in {x["arm"] for x in arms}]
    control_present = [a["arm"] for a in arms if a["arm"].startswith("ctrl-")]
    control_missing = [a for a in ARMS_S3O_EXPECTED if a not in control_present]
    return {
        "status": "done",
        "reports": ARMS_DIR,
        "arms": arms,
        "arms_passing_conjunction": passing,
        "arms_failing_both": both_fail,
        "expected_arms_missing": missing,
        "control_arms_present": control_present,
        "control_arms_missing": control_missing,
        "control_arms_note": (
            "контрольные руки S3o на момент свода не перемерены полностью "
            "(чекпойнты писались): незакрытая клетка конъюнкции, а не «шум»"
            if control_missing else "контрольные руки перемерены"),
        "not_averaged": ("k1_ratio и k2_ratio не усредняются и не сворачиваются в "
                         "одно число: вердикт — конъюнкция (ADR-027 п.1/п.2)"),
    }


def reverse_gate(root: Path) -> tuple[dict, list[str]]:
    """Обратная проверка гейта: он обязан разделять наборы, а не «всегда чист».

    Гейт, который никогда не находил пересечения, не проверен. Поэтому в свод
    берутся **фактические** числа двух прогонов (а не пересказ ожидаемых):

    * ``general_eval_v2`` — набор собран из источника реплея: обязан дать
      пересечение по всем документам и всем окнам;
    * ``general_eval_v3`` — компонента K1: обязан дать нули.

    Расхождение с ожиданием — отказ: значит гейт перестал различать случаи, и
    нули на K2 ничего не доказывают.
    """
    out: dict = {"what": ("гейт обязан показывать ожидаемое и в обратную сторону: "
                          "на наборе из источника реплея — пересечение, на чистом "
                          "наборе — нули; иначе его нули на K2 пусты")}
    problems: list[str] = []
    cases = {
        "v2_general": ("evidence/s3t-purity-v2.json", "overlap"),
        "v3_general": ("evidence/s3q-purity.json", "clean"),
    }
    for name, (rel, expected) in cases.items():
        rep = load(root, rel)
        if rep is None:
            problems.append(f"{name}: нет отчёта {rel}")
            continue
        matrix = rep.get("overlap_matrix") or []
        docs = rep.get("set", {}).get("docs")
        worst_docs = max((m["overlap_docs"] for m in matrix), default=None)
        worst_ngram = max((m["overlap_ngram"] for m in matrix), default=None)
        entry = {
            "report": rel, "set_sha256": rep.get("set", {}).get("sha256"),
            "docs": docs, "verdict": rep.get("verdict"),
            "worst_overlap_docs": worst_docs, "worst_overlap_ngram": worst_ngram,
            "mixes": [m["mix"] for m in matrix],
            "expected": expected,
        }
        if expected == "overlap":
            ok = (rep.get("verdict") == "overlap" and docs is not None
                  and worst_docs == docs and worst_ngram is not None and worst_ngram > 0)
            entry["matched_expectation"] = (
                f"пересечение по всем документам набора: {worst_docs} из {docs} "
                f"документов и {worst_ngram} окон")
            want = f"пересечение по всем {docs} документам набора"
        else:
            ok = rep.get("verdict") == "clean" and worst_docs == 0 and worst_ngram == 0
            entry["matched_expectation"] = "нули по всем миксам"
            want = "нули по документам и окнам"
        entry["as_expected"] = ok
        if not ok:
            problems.append(f"{name}: гейт дал {rep.get('verdict')} "
                            f"({worst_docs} док, {worst_ngram} окон) вместо «{want}»")
        out[name] = entry
    return out, problems


def build(root: Path) -> tuple[dict, int]:
    card = load(root, CARD)
    purity = load(root, PURITY)
    baseline = load(root, BASELINE)
    if card is None or purity is None or baseline is None:
        return {}, EXIT_NOT_VERIFIED

    set_sha = card["set"]["sha256"]
    if purity["set"]["sha256"] != set_sha:
        print(f"ОТКАЗ: гейт проверял другой набор ({purity['set']['sha256'][:12]} "
              f"против {set_sha[:12]} в карточке)", file=sys.stderr)
        return {}, EXIT_FAIL
    comp = baseline["component"]
    if comp["set_sha256"] != set_sha:
        print(f"ОТКАЗ: проба меряла другой набор ({comp['set_sha256'][:12]} против "
              f"{set_sha[:12]})", file=sys.stderr)
        return {}, EXIT_FAIL
    if purity["verdict"] != "clean":
        print("ОТКАЗ: набор не прошёл гейт чистоты — свод не собирается", file=sys.stderr)
        return {}, EXIT_FAIL
    if not baseline["instrument_identity"]["reproduced"]:
        print("ОТКАЗ: прибор не воспроизвёл эталоны v1/v3 в том же прогоне — "
              "сравнивать компоненты нечем", file=sys.stderr)
        return {}, EXIT_FAIL

    # ── наборы и корпус ревизии: сверка с историческими хешами ───────────────
    historic = historic_hashes(root)
    untouched = {}
    for name, (rel, source) in UNTOUCHED.items():
        p = root / rel
        now = sha256_file(p) if p.is_file() else None
        untouched[name] = {
            "path": rel, "sha256_now": now, "sha256_historical": historic.get(name),
            "historical_source": ARMS_S3M if source == "s3m" else LEDGER_S3Q,
            "unchanged": now is not None and now == historic.get(name),
        }
    if not all(v["unchanged"] for v in untouched.values()):
        bad = [n for n, v in untouched.items() if not v["unchanged"]]
        print(f"ОТКАЗ: наборы ревизии изменились ({bad}) — K2 шёл не отдельным файлом",
              file=sys.stderr)
        return {}, EXIT_FAIL

    k1_ppl = baseline["peer_components"]["K1"]["ppl"]
    k1_ceiling = baseline["peer_components"]["K1"]["ceiling"]
    k2_ppl = comp["ppl"]
    k2_ceiling = comp["ceiling"]
    arms = arms_remeasured(root, k1_ppl, k2_ppl, k1_ceiling, k2_ceiling)
    if arms.get("status") == "refused":
        print(f"ОТКАЗ: {arms['why']} — свод не собирается", file=sys.stderr)
        return {}, EXIT_FAIL
    rev, rev_problems = reverse_gate(root)
    if rev_problems:
        print("ОТКАЗ: обратная проверка гейта не сошлась: "
              + "; ".join(rev_problems), file=sys.stderr)
        return {}, EXIT_FAIL
    idn = baseline["instrument_identity"]["checks"]

    evidence = {
        "schema": "s3t-k2-set/1",
        "stage": "S3t",
        "status": "complete",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": ("компонента K2 меры общего языка (ADR-027 п.1): набор вне "
                    "обучающего распределения, его чистота (ADR-025 п.1), база и "
                    "потолок (ADR-027 п.2) и перемер рук по двум компонентам"),
        "adr": ["ADR-027", "ADR-025 п.1", "ADR-025 п.2", "ADR-025 п.4", "ADR-025 п.5",
                "ADR-022 п.3", "ADR-005", "AD-4", "AD-11", "AD-12"],
        "artifacts": {
            "set": card["set"]["path"],
            "card": CARD,
            "builder": "tools/build_general_eval_k2.py",
            "gate": "tools/check_eval_set_purity.py",
            "gate_report": PURITY,
            "probe": "tools/ppl_probe_k2.py",
            "probe_report": BASELINE,
            "arms_dir": ARMS_DIR,
            "registry": "docs/specs/EVAL-SETS.md",
            "assembler": "tools/assemble_s3t_evidence.py",
        },
        "k2": {
            "path": card["set"]["path"],
            "sha256": set_sha,
            "bytes": card["set"]["bytes"],
            "docs": card["set"]["docs"],
            "tokens": card["set"]["tokens"],
            "tokens_counted_by_probe": comp["tokens"],
            "tokens_note": ("в наборе сумма len(encode(doc)) = "
                            f"{card['set']['tokens']}; прибор считает предсказанные "
                            f"позиции (len−1 на документ) = {comp['tokens']}; разница "
                            f"ровно {card['set']['docs']} — по одной на документ"),
            "source": card["source"]["path"],
            "source_sha256": card["source"]["sha256"],
            "provenance": card["source"]["provenance"],
            "genre": card["source"]["genre"],
            "language": card["source"]["language"],
            "distribution_relation": card["source"]["distribution_relation"],
            "distribution_why": card["source"]["distribution_why"],
            "license_status": card["source"]["license_status"],
            "license_note": card["source"]["license_note"],
            "segmentation": card["segmentation"],
            "doc_chars": {"min": card["set"]["doc_chars_min"],
                          "max": card["set"]["doc_chars_max"]},
            "doc_tokens": {"min": card["set"]["doc_tokens_min"],
                           "max": card["set"]["doc_tokens_max"]},
        },
        "build": {
            "filters": card["filters"],
            "selection": card["selection"],
            "purity_scan": card["purity"],
        },
        "overlap_matrix": purity["overlap_matrix"],
        "leak_check": {
            "dataset_sha256": set_sha,
            "overlap_docs": max(m["overlap_docs"] for m in purity["overlap_matrix"]),
            "overlap_ngram_windows": max(m["overlap_ngram"]
                                         for m in purity["overlap_matrix"]),
            "window_size": purity["instrument"]["window_size"],
            "verdict": purity["verdict"],
            "checked_mixes": [m["mix"] for m in purity["overlap_matrix"]],
            "checked_at": purity["date"],
            "gate_report": PURITY,
            "note": ("форма ADR-025 п.2: применение потолка к руке разрешено только "
                     "при нулях здесь; проверка идёт по корпусу-источнику целиком"),
        },
        "reverse_gate": rev,
        "k2_baseline": {
            "ppl": k2_ppl,
            "ceiling": k2_ceiling,
            "rule": "потолок = 2 × базы компоненты (ADR-022 п.3, ADR-027 п.2)",
            "doc_ppl": comp["doc_ppl"],
            "docs_counted": comp["docs_counted"],
            "set_sha256": comp["set_sha256"],
            "evidence": BASELINE,
        },
        "k1_reference": {
            "component": "K1 — набор в жанре реплея (ADR-027 п.1)",
            "set": baseline["peer_components"]["K1"]["set"],
            "set_sha256": baseline["peer_components"]["K1"]["set_sha256"],
            "ppl": k1_ppl,
            "ceiling": k1_ceiling,
            "note": ("снято тем же прибором в том же прогоне, что и K2, — иначе "
                     "конъюнкция сравнивала бы разные шкалы"),
        },
        "historical_reference": {
            "component": "v1_general — исторический опорный, не решающий (ADR-027 п.3)",
            "set": baseline["peer_components"]["historical"]["set"],
            "set_sha256": baseline["peer_components"]["historical"]["set_sha256"],
            "ppl": baseline["peer_components"]["historical"]["ppl"],
            "ceiling": baseline["peer_components"]["historical"]["ceiling"],
        },
        "instrument_identity": {
            "what": baseline["instrument_identity"]["what"],
            "v1_ppl": idn["v1_general"]["ppl_measured"],
            "v1_ppl_historical": idn["v1_general"]["ppl_historical"],
            "v1_rel_delta_pct": idn["v1_general"]["rel_delta_pct"],
            "v3_ppl": idn["v3_general"]["ppl_measured"],
            "v3_ppl_historical": idn["v3_general"]["ppl_historical"],
            "v3_rel_delta_pct": idn["v3_general"]["rel_delta_pct"],
            "tolerance_pct": baseline["instrument_identity"]["tolerance_pct"],
            "reproduced": baseline["instrument_identity"]["reproduced"],
            "instrument": baseline["instrument"]["measure"],
            "probe_revision_sha256": baseline["instrument"]["probe_revision"]["sha256"],
            "evidence": BASELINE,
        },
        "arms": arms,
        "untouched_check": untouched,
        "next_step": NEXT_STEP,
        "open_questions": OPEN_QUESTIONS,
    }
    return evidence, EXIT_OK


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="S3t: свод evidence компоненты K2")
    ap.add_argument("--case-root", default=None,
                    help="корень кейса (по умолчанию — каталог над tools/)")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--json", action="store_true", help="свод в stdout")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(args.case_root).resolve() if args.case_root else CASE_ROOT
    evidence, rc = build(root)
    if not evidence:
        return rc
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return rc
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    a = evidence["arms"]
    print(f"== S3t: компонента K2 ==")
    print(f"набор: {evidence['k2']['path']}  {evidence['k2']['docs']} док  "
          f"{evidence['k2']['tokens']} ток  sha256 {evidence['k2']['sha256'][:12]}")
    print(f"гейт: {evidence['leak_check']['verdict']} "
          f"({evidence['leak_check']['overlap_docs']} док, "
          f"{evidence['leak_check']['overlap_ngram_windows']} окон)")
    print(f"K2: база {evidence['k2_baseline']['ppl']:.6f} → потолок "
          f"{evidence['k2_baseline']['ceiling']:.6f}")
    print(f"K1: база {evidence['k1_reference']['ppl']:.6f} → потолок "
          f"{evidence['k1_reference']['ceiling']:.6f}")
    print(f"прибор: v1 {evidence['instrument_identity']['v1_ppl']:.6f} "
          f"({evidence['instrument_identity']['v1_rel_delta_pct']:+.4f} %), "
          f"v3 {evidence['instrument_identity']['v3_ppl']:.6f} "
          f"({evidence['instrument_identity']['v3_rel_delta_pct']:+.4f} %)")
    if a.get("status") == "done":
        print(f"\n{'рука':16s} {'K1 ×':>7s} {'K1':>5s} {'K2 ×':>7s} {'K2':>5s} "
              f"{'конъюнкция':>11s}")
        for arm in a["arms"]:
            print(f"{arm['arm']:16s} {arm['k1_ratio']:7.3f} "
                  f"{'да' if arm['k1_pass'] else 'НЕТ':>5s} {arm['k2_ratio']:7.3f} "
                  f"{'да' if arm['k2_pass'] else 'НЕТ':>5s} "
                  f"{'ПРОХОД' if arm['conjunction'] else 'провал':>11s}")
        print(f"\nконъюнкция пройдена: {a['arms_passing_conjunction'] or 'ни одной рукой'}")
    else:
        print(f"\nперемер рук: {a.get('status')} — {a.get('why')}")
    print(f"evidence: {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
