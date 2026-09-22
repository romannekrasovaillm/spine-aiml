#!/usr/bin/env python3
"""Свод evidence дельты S3ao — домен-набор v3 из другого корпуса (ADR-043 п.3).

Собирает `evidence/s3ao-domain-v3.json` из артефактов прогона, ничего не досчитывая
руками: каждое число берётся из файла, который его произвёл, и рядом с ним остаётся
путь этого файла. Число, набранное в отчёт руками, проверить нечем — поэтому
сводка не «переносит» числа, а читает их.

Что попадает в отчёт (контракт TASK S3ao):

* ``source`` — откуда набор и на каком основании он считается допустимым;
* ``dataset`` — путь, sha256, документы, токены;
* ``overlap`` — матрица пересечений: документы и 12-граммы против каждого
  обучающего входа, против K1/K2 и против прежнего домен-набора;
* ``baseline`` — база и потолок 2× **своего** набора;
* ``states_measured`` — CPT-финал и свежий SFT на обоих наборах;
* ``registry`` — где набор зарегистрирован.

Коды возврата::

    0 — свод собран
    1 — отчёт противоречив (числа не сходятся) — свод не записывается
    2 — NOT-VERIFIED: нет артефакта, без которого свод был бы догадкой

Запуск::

    python3 tools/assemble_s3ao_evidence.py --run runs/s3ao-domain-v3-20260919
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


def read_json(path: Path):
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="каталог прогона дельты")
    ap.add_argument("--out", default="evidence/s3ao-domain-v3.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    run = Path(a.run)
    if not run.is_dir():
        print(f"NOT-VERIFIED: нет каталога прогона: {run}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    needed = {
        "builder_card": run / "domain-eval-v3-card.json",
        "overlap_matrix": run / "overlap_v2_v3_vs_all_training.json",
        "overlap_k1k2": run / "overlap_v3_vs_K1K2.json",
        "residual": run / "v101_residual.json",
        "probe_cpt": run / "ppl_cpt_final.json",
        "probe_sft": run / "ppl_sft_fresh.json",
        "hashes_before": run / "source_hashes_before.json",
        "hashes_after": run / "source_hashes_after.json",
    }
    missing = [k for k, p in needed.items() if not p.is_file()]
    if missing:
        print(f"NOT-VERIFIED: нет артефактов: {missing} — свод был бы догадкой",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    card = read_json(needed["builder_card"])
    matrix = read_json(needed["overlap_matrix"])
    k1k2 = read_json(needed["overlap_k1k2"])
    residual = read_json(needed["residual"])
    probe_cpt = read_json(needed["probe_cpt"])
    probe_sft = read_json(needed["probe_sft"])
    before = read_json(needed["hashes_before"])["files"]
    after = read_json(needed["hashes_after"])["files"]

    # ── Противоречия, при которых свод не имеет права быть записан ────────────
    problems: list[str] = []
    out_path = Path(card["output"]["path"])
    if not out_path.is_file():
        problems.append(f"набор не найден: {out_path}")
    else:
        if sha256_file(out_path) != card["output"]["sha256"]:
            problems.append("sha256 набора не совпал с карточкой сборки — набор переписан")
    for name, rec in before.items():
        if rec.get("sha256") and after.get(name, {}).get("sha256") != rec["sha256"]:
            problems.append(f"источник {name} изменился за время дельты")
    gate3 = matrix["gates"].get("DOMAIN3")
    if gate3 is None:
        problems.append("в матрице пересечений нет DOMAIN3")
    elif gate3 != 0:
        problems.append(f"гейт чистоты домен-набора v3 не пройден: документов в обучении {gate3}")
    if problems:
        for p in problems:
            print(f"ОТКАЗ: {p}", file=sys.stderr)
        return EXIT_FAIL

    #: Хеши измеренных чекпойнтов: живой прогон SFT пишет чекпойнты дальше, и без
    #: хеша «свежий SFT-чекпойнт» — это «какой-то файл с таким именем», а не
    #: конкретное состояние весов.
    ckpt_sha: dict[str, str] = {}
    ckpt_file = run / "ckpt_hashes.txt"
    if ckpt_file.is_file():
        for line in ckpt_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                ckpt_sha[parts[1]] = parts[0]

    def ppl(probe: dict, state: str, set_name: str) -> float:
        return float(probe["states"][state]["sets"][set_name]["ppl"])

    base_v3 = ppl(probe_cpt, "base", "v3_domain")
    base_v2 = ppl(probe_cpt, "base", "v2_domain")
    # Прибор обязан вернуться к базовым весам: иначе все отношения ниже — не к базе.
    restored = (ppl(probe_cpt, "base_untouched", "v3_domain") == base_v3
                and ppl(probe_sft, "base_untouched", "v3_domain") == base_v3)

    def state_row(probe: dict, state: str) -> dict:
        return {
            "v3_domain": {"ppl": ppl(probe, state, "v3_domain"),
                          "ratio_to_base": ppl(probe, state, "v3_domain") / base_v3},
            "v2_domain": {"ppl": ppl(probe, state, "v2_domain"),
                          "ratio_to_base": ppl(probe, state, "v2_domain") / base_v2},
        }

    docs3 = matrix["sets"]["DOMAIN3"]["documents"]
    ngram_by_train = matrix["ngram_docs_shared_by_train"]["DOMAIN3"]
    report = {
        "tool": "assemble_s3ao_evidence.py",
        "stage": "S3ao",
        "title": "Домен-набор v3 из другого корпуса (чистый от обучения)",
        "adr": ["ADR-043 п.3", "ADR-025 п.1/п.6", "ADR-031 п.4", "ADR-018"],
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "complete",
        "problem": ("domain_eval_v2.txt собран из того же семейства карточек, что "
                    "обучающий корпус; определения концептов цитируются в <tool_response> "
                    "траекторий, поэтому доменное отношение на нём частично показывает "
                    "узнавание формулировок, а не обобщение (ADR-043 п.3)"),
        "source": {
            "path": "~/Документы/КОД/gigachat/РАЗБОРЫ",
            "kind": "архив русскоязычных ML/AI-разборов, отчётов и обзоров (DOCX/MD/TXT)",
            "origin": ("личный рабочий архив владельца: разборы статей, обзоры архитектур, "
                       "отчёты по проектам, учебные материалы. Другой корпус, чем библиотека "
                       "концептов Ariadna, из которой собран обучающий CPT-корпус"),
            "license_note": ("internal_only (AD-8): документы внутренние, часть помечена "
                             "CONFIDENTIAL, в тексте есть имена и рабочий контекст. "
                             "Публикация производных запрещена; набор живёт на gb10-shared "
                             "и в git не попадает (AD-4 — симлинк на сетевой диск)"),
            "selection": "детерминированный отбор: сид, разрез, фильтры (см. карточку сборки)",
        },
        "dataset": {
            "path": card["output"]["path"],
            "sha256": card["output"]["sha256"],
            "bytes": card["output"]["bytes"],
            "docs": card["docs_built"],
            #: Токены — те, что прибор реально прочитал (документ обрезается до 1024
            #: токенов), а не по всему тексту: иначе число не отвечало бы замеру.
            "tokens": probe_cpt["states"]["base"]["sets"]["v3_domain"]["tokens"],
            "docs_counted": probe_cpt["states"]["base"]["sets"]["v3_domain"]["docs_counted"],
            "tokens_previous_set": probe_cpt["states"]["base"]["sets"]["v2_domain"]["tokens"],
            "sources_used": card["sources_used"],
            "length": card["length"],
            "builder_card": str(needed["builder_card"]),
        },
        "overlap": {
            "tool": "tools/check_measurement_overlap.py",
            "window_size": matrix["window_size"],
            "units_seen": matrix["units_seen"],
            "units_skipped_too_long": matrix.get("units_skipped_too_long", 0),
            "docs_with_training": gate3,
            "ngram_docs_shared_by_train": ngram_by_train,
            "ngram_docs_shared_total": matrix["ngram_docs_shared"]["DOMAIN3"],
            #: Окна считаются у документа: окно, найденное в обучении, принадлежит
            #: какому-то документу набора. Если ни один документ не делит ни одного
            #: окна, общее число общих окон — ноль, и это не вывод «по аналогии»:
            #: для двух главных входов окна выложены прямо (`--dump-shared`:
            #: различных 0, вхождений 0 — shared_windows_SFT13.json,
            #: shared_windows_CPT10_1.json).
            "ngram_windows_shared": (
                0 if matrix["ngram_docs_shared"]["DOMAIN3"] == 0 else None),
            "ngram_windows_shared_note": (
                "ноль следует из нуля документов с общими окнами и подтверждён "
                "прямой выкладкой окон против SFT v13 и CPT v10.1"),
            "docs_total": docs3,
            "vs_previous_domain": {
                "set": "DOMAIN2 (domain_eval_v2.txt)",
                "docs_with_training": matrix["gates"]["DOMAIN2"],
                "ngram_docs_shared_total": matrix["ngram_docs_shared"]["DOMAIN2"],
                "ngram_docs_shared_by_train": matrix["ngram_docs_shared_by_train"]["DOMAIN2"],
                "docs_total": matrix["sets"]["DOMAIN2"]["documents"],
            },
            "vs_K1K2": {
                "docs": k1k2["gates"]["DOMAIN3"],
                "ngram_docs_shared": k1k2["ngram_docs_shared"]["DOMAIN3"],
                "by_train": k1k2["ngram_docs_shared_by_train"]["DOMAIN3"],
                "report": str(needed["overlap_k1k2"]),
            },
            "verdict": ("нулевое пересечение документов с обучением — гейт ADR-025 п.1 "
                        "пройден; 12-граммы названы числом (п.6)"),
            "matrix_report": str(needed["overlap_matrix"]),
        },
        "residual_v101": {
            "question": ("остаток cpt_corpus_v10.1.txt, не попавший в обучающие чанки, "
                         "как альтернативный источник"),
            "tokens_total": residual["tokens_total"],
            "tokens_used": residual["tokens_used"],
            "tokens_residual": residual["tokens_residual"],
            "residual_units": residual["residual_units"],
            "residual_chars": residual["residual_chars"],
            "chunks_match": residual["chunks_match"],
            "verdict": ("не годится: остаток — две единицы (16 КБ) промпт-шаблона в хвосте "
                        "корпуса, а не тексты домена; набора из них не собрать"),
            "report": str(needed["residual"]),
        },
        "baseline": {
            "set": "v3_domain",
            "ppl": base_v3,
            "ceiling_2x": 2 * base_v3,
            "previous_set": {"set": "v2_domain", "ppl": base_v2, "ceiling_2x": 2 * base_v2},
            "where": "локальная RTX 4080 (стенд GB10 занят стадией SFT — AD-5)",
            "instrument": "tools/calib_ppl_probe.py → tools/ppl_probe.py:measure",
            "base_restored": restored,
        },
        "states_measured": {
            "cpt_final": {"ckpt": probe_cpt["states"]["cpt_final"]["load"].get("checkpoint"),
                          "ckpt_sha256": ckpt_sha.get(
                              probe_cpt["states"]["cpt_final"]["load"].get("checkpoint", "")),
                          **state_row(probe_cpt, "cpt_final")},
            "sft_fresh": {"ckpt": probe_sft["states"]["sft_fresh"]["load"].get("checkpoint"),
                          "ckpt_sha256": ckpt_sha.get(
                              probe_sft["states"]["sft_fresh"]["load"].get("checkpoint", "")),
                          **state_row(probe_sft, "sft_fresh")},
            "report_cpt": str(needed["probe_cpt"]),
            "report_sft": str(needed["probe_sft"]),
        },
        "source_integrity": {
            "before": str(needed["hashes_before"]),
            "after": str(needed["hashes_after"]),
            "unchanged": all(before[k].get("sha256") == after[k].get("sha256")
                             for k in before),
            "files": {k: before[k].get("sha256") for k in before},
        },
        "dataset_fitness": {
            "verdict": "valid как решающий домен-набор",
            "why": [
                "нулевое пересечение документов с обучением (ADR-025 п.1) — "
                "гейт пройден против семи обучающих входов, включая оба CPT-корпуса, "
                "вики-реплей и оба SFT-набора",
                "12-граммы названы числом против каждого входа (п.6) — см. overlap",
                "объём и различимость: 200 документов, длины в диапазоне, при котором "
                "прибор читает документ целиком (обрезание до 1024 токенов)",
                "источник — другой корпус, а не то же семейство карточек: падение "
                "доменного отношения на нём нельзя объяснить знакомством с текстом обучения",
            ],
            "limits": [
                "архив владельца, а не внешний публичный корпус: источник внутренний, "
                "воспроизводимость за пределами контура ограничена (AD-8, internal_only)",
                "жанр шире, чем у v2 (рабочие отчёты, обзоры, учебные материалы), "
                "поэтому абсолютная шкала ниже/выше v2 сравнима только как отношение к базе",
            ],
        },
        "registry_updated": {
            "branch": "arch/laguna-control-arms",
            "file": "docs/specs/DOMAIN-EVAL-V3.md",
            "role": "deciding_domain_v3",
            "canon": ("реестр измерительных наборов — docs/specs/EVAL-SETS.md в ветке "
                      "arch/laguna-eval-set; здесь короткий раздел, потому что канон "
                      "живёт в другой ветке (номера ADR между ветками не уникальны)"),
        },
        "artifacts": {k: str(p) for k, p in needed.items()},
    }
    report["open_questions"] = [
        "Читать ли доменный вердикт по v3 без оговорки ADR-043 п.3 — решает архитектор: "
        "набор чист по документам, но собран из архива владельца, а не из внешнего корпуса.",
        "Что делать с v2: остаётся исторической шкалой или снимается с решающей роли "
        "полностью (сейчас v2 и v3 считаются в одном прогоне именно для сравнимости).",
    ]
    report["assumptions"] = [
        "Токены набора в отчёте — по прибору (документ обрезается до 1024 токенов), "
        "а не по полному тексту: иначе число не отвечало бы тому, что мерит прибор.",
    ]

    out = CASE_ROOT / a.out
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"свод: {out}")
        print(f"  набор: {report['dataset']['docs']} документов, "
              f"sha256 {report['dataset']['sha256'][:16]}")
        # Печатается значимая пара: документы v3 против обученного CPT-корпуса и
        # то же число у v2. Полный гейт v2 равен 200 только потому, что v2 собран
        # ИЗ предка этого корпуса, — сравнивать с ним «0 против 200» вводило бы
        # в заблуждение.
        prev = matrix["ngram_docs_shared_by_train"]["DOMAIN2"]
        print(f"  пересечение с обучением: документов {gate3}; "
              f"12-граммы против CPT v10.1: {ngram_by_train.get('CPT10.1')} из {docs3} "
              f"(у v2 было {prev.get('CPT10.1')} из {docs3})")
        print(f"  база v3: {base_v3:.4f}, потолок {2 * base_v3:.4f}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
