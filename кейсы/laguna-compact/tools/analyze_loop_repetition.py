#!/usr/bin/env python3
"""S3al — повторы в обучающем наборе SFT: нужна ли работа с данными/наградой.

Зачем отдельный прибор, если S3ak уже мерил петли в данных. S3ak ответил на
вопрос «есть ли в наборе примеры-петли» (есть: 1.45 % прозы, 650 строк из 44 949)
и оставил **три** открытых вопроса, из которых два решаются здесь — потому что от
них зависит решение «трогать ли данные», а не от самой доли:

* **(1) длина.** `max4gram_rep` механически растёт с длиной текста, а петлевые
  примеры длиннее не-петлевых (медиана 1 025 против 856 слов). Значит контраст
  «среди дубликатов 0.0162 против 0.0 среди уникальных» **не контролирован по
  длине**, и читать его как «дубликат вызывает петлю» нельзя. Здесь тот же разрез
  считается **внутри подобранных по длине корзин**: если внутри корзины разница
  исчезает, весь прежний контраст объясняется длиной;
* **(2) происхождение петли генерации.** Петля генерации — это повтор 4-граммы.
  Вопрос, нужна ли работа с данными: **лежит ли эта 4-грамма в наборе повтором**
  (то есть модель воспроизводит заученный фрагмент) или её там нет и повтор
  рождён декодером. Прежние числа (доля примеров-петель) на этот вопрос не
  отвечают: 1.45 % примеров-петель и 54 % зацикленных генераций — величины про
  разные вещи, и совпадение/расхождение долей ничего не доказывает.

Третье чтение — **сколько раз** найденная 4-грамма встречается в наборе и
встречается ли она **повтором внутри одного примера** (≥ порога петель). Это
разрез, который прямо отвечает «награда за отсутствие повторов в данных или
лечение данных»: если целевая 4-грамма нигде в наборе не повторяется 8 раз, то
набор не учит петле как образцу, и правка данных её не уберёт.

Что берётся из генераций. Берутся **зацикленные** генерации прежних замеров
(greedy-разбор по шагам обучения S3ak и greedy-разбор режимов) — той же метрикой
(`probe_control.degenerate_metrics`, импорт) и тем же порогом
(`probe_language_split.LOOP_MAX4GRAM_REP`, импорт, пиннуется). «Юнит петли» —
`top4gram` той же метрики: самая частая 4-грамма генерации, то есть ровно то, что
генерация переписывает. Контроль — **уникальные** 4-граммы той же генерации
(встретившиеся в ней один раз): они отвечают на вопрос «а как часто в наборе
встречается произвольная фраза модели такого же вида», без чего «нашлось в
наборе» не значит ничего (русская фраза из четырёх частых слов найдётся всегда).

Вердикт — по правилам, объявленным константами ДО прогона (`RULES`), с числами по
каждому сработавшему правилу; несработавшее правило тоже попадает в отчёт
(`rules_evaluated`). Прибор **не решает**, что делать с данными: он даёт числа, на
которых решение можно принять или отклонить.

Коды возврата::

    0 — отчёт собран
    1 — отказ: не тот вход (порог петель у прибора изменился, нет генераций,
        нет набора) — считается по новым числам молча нельзя
    2 — NOT-VERIFIED: мерить нечего (в генерациях нет ни одной зацикленной)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))

#: Метрика, порог и чтения — **импортом**, не копией: «зациклилось» и «проза»
#: должны считаться там же, где считались у генераций (см. шапку S3ak-прибора).
import analyze_sft_loops as AL      # noqa: E402
import probe_language_split as LS   # noqa: E402
from probe_control import degenerate_metrics  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Путь набора — относительный, от корня кейса (C-012: без абсолютных путей и `..`).
DEFAULT_INPUT = "datasets/sft_train_v12.jsonl"
#: Генерации прежних замеров: разбор по шагам (12 состояний × 24 пробы, S3ak) и
#: разбор режимов декодирования (24 пробы на режим, S3ak). Оба — greedy, потому
#: что вопрос о повторах задаётся там, где повтор и виден.
DEFAULT_GENERATIONS = ["runs/s3ak-degeneration-20260917/by_step.json",
                       "runs/s3ak-degeneration-20260917/decoding_greedy.json"]

#: Пин порога: отчёт объявляет «порог 8 из прибора S3ai/S3aj». Если прибор его
#: сменил — это не тот вход (код 1): переопределять порог здесь запрещено.
LOOP_MAX4GRAM_REP_EXPECTED = 8

#: Корзины длины прозы в **словах** (границы объявлены до прогона, не по данным).
#: Нужны затем, чтобы контраст «дубликат против одиночки» считался внутри
#: сопоставимой длины: метрика повторов растёт с длиной текста, и без корзин
#: контраст мерил бы длину, а не дубликатность.
LENGTH_BUCKETS = [0, 256, 512, 1024, 2048, 4096, 1 << 30]
LENGTH_BUCKET_LABELS = ["<256", "256-511", "512-1023", "1024-2047", "2048-4095", "≥4096"]

#: Сколько контрольных уникальных 4-грамм брать на одну петлевую генерацию.
#: Три — компромисс: контроль должен быть представительным, но не разбавлять
#: выборку так, чтобы «доля найденных» перестала быть про петлю.
CONTROL_UNITS_PER_GENERATION = 3

#: Сколько юнитов выгружать в отчёт построчно (полные списки не влезают).
TOP_UNITS_N = 20

#: Правила вердикта. Объявлены ДО прогона, каждое — с числом, по которому сработало.
#: Смысл: вопрос не «есть ли повторы в данных» (есть, S3ak посчитал), а «учит ли
#: набор петле как образцу» и «отвечает ли дубликатность за петлевость после
#: выравнивания по длине».
RULES = {
    "length_controlled": {
        "name": "длина объясняет контраст «дубликат против одиночки»",
        "rule": "если внутри корзин длины разница долей петлевых (дубликат − одиночка) "
                "≤ 0.01 хотя бы в 4 корзинах из 6 И общая разница > 0.01 — контраст "
                "объясняется длиной, а не дубликатностью",
        "consequence": "дедупликация как средство от петель данных не подтверждается: "
                       "она уберёт длинные тексты вместе с петлями",
    },
    "taught_pattern": {
        "name": "набор учит петле как образцу",
        "rule": "если доля юнитов петель, встречающихся в наборе повтором "
                "(reps ≥ 8 внутри одного примера) ≥ 0.3 — образец петли в наборе есть",
        "consequence": "работа с данными (перегенерация примеров-петель) имеет предмет",
    },
    "memorized_fragment": {
        "name": "петля — воспроизведение заученного фрагмента",
        "rule": "если доля юнитов петель, найденных в наборе хотя бы раз, ≥ 0.5 при "
                "медианной кратности в наборе ≥ 2× медианной кратности контроля — "
                "юниты петли в наборе есть и они там частотнее контроля",
        "consequence": "часть повторов идёт из данных; но это не то же самое, что "
                       "«данные учат петле» — различать с правилом taught_pattern",
    },
}

RE_WS = AL.RE_WS


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def bucket_of(words: int) -> str:
    """Корзина длины по объявленным границам; последняя — «всё, что длиннее»."""
    for i in range(len(LENGTH_BUCKET_LABELS)):
        if LENGTH_BUCKETS[i] <= words < LENGTH_BUCKETS[i + 1]:
            return LENGTH_BUCKET_LABELS[i]
    return LENGTH_BUCKET_LABELS[-1]


# ─────────────────────── юниты петель из генераций ───────────────────────

def loop_units_from_report(path: Path) -> tuple[list[dict], list[dict]]:
    """Юниты петель и контрольные юниты из отчёта проб.

    Возврат — ``(units, controls)``; в каждой записи: состояние, тег пробы,
    кратность юнита в генерации, сам юнит (строка из четырёх слов) и длина
    генерации в словах. Контроль берётся **из той же генерации** (уникальные
    4-граммы), поэтому разница «петля против контроля» не смешивается с разницей
    между состояниями или промптами.
    """
    report = json.loads(path.read_text(encoding="utf-8"))
    units: list[dict] = []
    controls: list[dict] = []
    for state, value in (report.get("states") or {}).items():
        for probe in value.get("probes") or []:
            if not (probe.get("metrics") or {}).get("looped"):
                continue
            text = probe.get("response") or ""
            words = text.split()
            if len(words) < 8:
                continue
            deg = degenerate_metrics(text)
            unit = deg.get("top4gram") or ""
            if not unit:
                continue
            units.append({"report": _rel(path), "state": state, "tag": probe.get("tag"),
                          "unit": unit, "reps_in_generation": deg.get("max4gram_rep"),
                          "generation_words": len(words)})
            #: Контроль: 4-граммы, встретившиеся в этой генерации ровно один раз.
            #: Шаг выборки — равномерный по списку уникальных: смещения «первые N»
            #: здесь не нужно, но и случайности не нужно — отчёт обязан
            #: воспроизводиться побайтово на том же входе.
            seen: dict[str, int] = {}
            for i in range(len(words) - 3):
                key = " ".join(words[i:i + 4])
                seen[key] = seen.get(key, 0) + 1
            unique = [k for k, v in seen.items() if v == 1]
            if unique:
                step = max(1, len(unique) // CONTROL_UNITS_PER_GENERATION)
                for j in range(0, len(unique), step)[:CONTROL_UNITS_PER_GENERATION]:
                    controls.append({"report": _rel(path), "state": state,
                                     "tag": probe.get("tag"), "unit": unique[j],
                                     "generation_words": len(words)})
    return units, controls


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(CASE))
    except ValueError:
        return str(p)


# ─────────────────────── один проход по набору ───────────────────────

def scan_dataset(input_path: Path, targets: set[str],
                 limit: int | None = None) -> tuple[list[dict], dict]:
    """Один проход по jsonl: строка-пример + счётчики целевых 4-грамм.

    Что считается по каждой строке: длина прозы в словах, петлевость (тем же
    прибором и порогом, что у генераций), хеш нормализации для поиска дословных
    дубликатов. Что считается по **целевым** 4-граммам (юниты петель и контроль):
    сколько раз каждая встретилась во всём assistant-тексте строки и сколько
    строк её содержат. Полные тексты в память не собираются.
    """
    rows: list[dict] = []
    per_unit: dict[str, dict] = {u: {"occurrences": 0, "examples": 0, "max_in_example": 0}
                                 for u in targets}
    n_bad = 0
    with open(input_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if limit is not None and len(rows) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                n_bad += 1
                continue
            info = AL.analyze_record(rec)
            rows.append({
                "line_no": i,
                "looped": info["looped_prose"],
                "rep_prose": info["rep_prose"],
                "words_prose": info["words_prose"],
                "words_all": info["words_all"],
                "dup_hash": hashlib.sha256(
                    AL.normalize_example(rec).encode("utf-8")).hexdigest(),
            })
            #: Целевые 4-граммы ищутся в **assistant-тексте целиком** (чтение (а)
            #: S3ak): модель учится порождать весь ход, а не только прозу, и
            #: «фрагмент лежит в данных, но в размеченном блоке» — тоже лежит.
            words = AL.assistant_text(rec).split()
            if len(words) >= 4:
                counts: dict[str, int] = {}
                for j in range(len(words) - 3):
                    key = " ".join(words[j:j + 4])
                    if key in per_unit:
                        counts[key] = counts.get(key, 0) + 1
                for key, cnt in counts.items():
                    per_unit[key]["occurrences"] += cnt
                    per_unit[key]["examples"] += 1
                    if cnt > per_unit[key]["max_in_example"]:
                        per_unit[key]["max_in_example"] = cnt
            if len(rows) % 10000 == 0:
                note(f"  ... {len(rows)} примеров")
    return rows, {"per_unit": per_unit, "n_bad_lines": n_bad}


# ─────────────────────── разрезы ───────────────────────

def duplication_index(rows: list[dict]) -> dict[str, int]:
    """Кратность каждой строки по нормализованному тексту (1 — уникальная)."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["dup_hash"]] = counts.get(r["dup_hash"], 0) + 1
    for r in rows:
        r["dup_multiplicity"] = counts[r["dup_hash"]]
        r["duplicated"] = counts[r["dup_hash"]] > 1
    return counts


def _share(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def length_controlled(rows: list[dict]) -> dict:
    """Разрез «дубликат против одиночки» **внутри корзин длины** (вопрос (1)).

    Возврат — таблица по корзинам плюс pooled-разница (взвешенная по числу строк
    корзины). Рядом — контраст без выравнивания: если он есть, а внутри корзин
    исчезает, то прежнее чтение мерило длину.
    """
    table = []
    pooled_num = pooled_den = 0
    n_buckets_with_contrast = 0
    for label in LENGTH_BUCKET_LABELS:
        sub = [r for r in rows if r["bucket"] == label]
        dup = [r for r in sub if r["duplicated"]]
        uniq = [r for r in sub if not r["duplicated"]]
        s_dup = _share(sum(r["looped"] for r in dup), len(dup))
        s_uniq = _share(sum(r["looped"] for r in uniq), len(uniq))
        diff = None if (s_dup is None or s_uniq is None) else round(s_dup - s_uniq, 4)
        table.append({"bucket": label, "n": len(sub), "n_duplicated": len(dup),
                      "n_unique": len(uniq),
                      "looped_share_duplicated": s_dup,
                      "looped_share_unique": s_uniq,
                      "difference": diff,
                      "median_words": LS._pct([r["words_prose"] for r in sub], 0.5)
                      if sub else None})
        if diff is not None and len(dup) and len(uniq):
            w = min(len(dup), len(uniq))
            pooled_num += diff * w
            pooled_den += w
            if diff > 0.01:
                n_buckets_with_contrast += 1
    dup_all = [r for r in rows if r["duplicated"]]
    uniq_all = [r for r in rows if not r["duplicated"]]
    share_dup = _share(sum(r["looped"] for r in dup_all), len(dup_all))
    share_uniq = _share(sum(r["looped"] for r in uniq_all), len(uniq_all))
    pooled = round(pooled_num / pooled_den, 4) if pooled_den else None
    overall = None if (share_dup is None or share_uniq is None) else round(share_dup - share_uniq, 4)
    return {
        "reading": "looped — по прозе, тем же прибором и порогом, что у генераций",
        "buckets": table,
        "overall": {"looped_share_duplicated": share_dup,
                    "looped_share_unique": share_uniq,
                    "difference": overall,
                    "n_duplicated": len(dup_all), "n_unique": len(uniq_all)},
        "pooled_matched_difference": pooled,
        "pooled_weights": "min(число дубликатов, число одиночек) в корзине",
        "buckets_with_contrast": n_buckets_with_contrast,
        "n_buckets": len(LENGTH_BUCKET_LABELS),
    }


def provenance(units: list[dict], controls: list[dict], per_unit: dict[str, dict]) -> dict:
    """Есть ли юниты петель в наборе и повторяются ли они там (вопрос (2))."""
    def _summarize(items: list[dict]) -> dict:
        found = [u for u in items if per_unit[u["unit"]]["occurrences"] > 0]
        taught = [u for u in items if per_unit[u["unit"]]["max_in_example"]
                  >= LS.LOOP_MAX4GRAM_REP]
        occ = [per_unit[u["unit"]]["occurrences"] for u in items]
        ex = [per_unit[u["unit"]]["examples"] for u in items]
        return {
            "n_units": len(items),
            "found_in_dataset": len(found),
            "found_share": _share(len(found), len(items)),
            "taught_as_pattern": len(taught),
            "taught_share": _share(len(taught), len(items)),
            "occurrences_median": LS._pct(occ, 0.5),
            "occurrences_p90": LS._pct(occ, 0.9),
            "examples_median": LS._pct(ex, 0.5),
            "top": sorted(
                ({"unit": u["unit"], "state": u["state"], "tag": u["tag"],
                  "reps_in_generation": u.get("reps_in_generation"),
                  **per_unit[u["unit"]]} for u in items),
                key=lambda d: -d["occurrences"])[:TOP_UNITS_N],
        }

    loop_u = _summarize(units)
    ctrl_u = _summarize(controls)
    l_med = loop_u["occurrences_median"]
    c_med = ctrl_u["occurrences_median"]
    return {
        "loop_units": loop_u,
        "control_units": ctrl_u,
        "ratio_median_occurrences": (None if not c_med else
                                     round((l_med or 0) / c_med, 4)),
        "reading": (
            "юнит петли — самая частая 4-грамма зацикленной генерации (top4gram той же "
            "метрики); контроль — уникальные 4-граммы той же генерации. «Найден в "
            "наборе» без контроля ничего не значит: русская фраза из четырёх частых "
            "слов найдётся всегда, поэтому сравнивать надо с контролем"),
    }


def verdict(length_ctrl: dict, prov: dict) -> dict:
    """Вердикт по правилам RULES — те же правила, с числами по каждому."""
    evaluated = []
    lc = RULES["length_controlled"]
    diff = length_ctrl["pooled_matched_difference"]
    overall = length_ctrl["overall"]["difference"]
    #: Условие переписано из RULES дословно: «разница внутри корзин ≤ 0.01 хотя бы
    #: в 4 корзинах из 6» = «корзин с контрастом > 0.01 не больше двух», и общая
    #: разница при этом должна быть **большой** — иначе правило срабатывало бы и на
    #: данных, где контраста нет вовсе (объяснять нечего).
    fired_lc = bool(diff is not None and overall is not None
                    and diff <= 0.01
                    and length_ctrl["buckets_with_contrast"] <= 2
                    and overall > 0.01)
    evaluated.append({"rule": lc["rule"], "key": "length_controlled",
                      "observed": {"pooled_matched_difference": diff,
                                   "overall_difference": overall,
                                   "buckets_with_contrast": length_ctrl["buckets_with_contrast"],
                                   "n_buckets": length_ctrl["n_buckets"]},
                      "fired": fired_lc})

    tp = RULES["taught_pattern"]
    taught_share = prov["loop_units"]["taught_share"]
    fired_tp = bool(taught_share is not None and taught_share >= 0.3)
    evaluated.append({"rule": tp["rule"], "key": "taught_pattern",
                      "observed": {"taught_share": taught_share,
                                   "n_taught": prov["loop_units"]["taught_as_pattern"],
                                   "n_units": prov["loop_units"]["n_units"]},
                      "fired": fired_tp})

    mf = RULES["memorized_fragment"]
    found_share = prov["loop_units"]["found_share"]
    ratio = prov["ratio_median_occurrences"]
    fired_mf = bool(found_share is not None and found_share >= 0.5
                    and ratio is not None and ratio >= 2.0)
    evaluated.append({"rule": mf["rule"], "key": "memorized_fragment",
                      "observed": {"found_share": found_share,
                                   "ratio_median_occurrences": ratio},
                      "fired": fired_mf})

    consequences = [{"key": r["key"], "name": RULES[r["key"]]["name"],
                     "consequence": RULES[r["key"]]["consequence"]}
                    for r in evaluated if r["fired"]]
    return {"rules_evaluated": evaluated, "fired": consequences,
            "not_fired": [r["key"] for r in evaluated if not r["fired"]],
            "note": "вердикт считается правилами выше; несработавшее правило остаётся "
                    "в отчёте — иначе отрицательный результат выглядел бы как "
                    "отсутствие вопроса"}


# ─────────────────────── прогон ───────────────────────

def run(args) -> int:
    if args.selftest:
        return selftest()

    if LS.LOOP_MAX4GRAM_REP != LOOP_MAX4GRAM_REP_EXPECTED:
        note(f"отказ: LOOP_MAX4GRAM_REP у прибора = {LS.LOOP_MAX4GRAM_REP}, "
             f"а ожидается {LOOP_MAX4GRAM_REP_EXPECTED}: порог петель здесь не "
             "переопределяется, и считать по новому молча нельзя")
        return EXIT_FAIL

    input_path = CASE / args.input
    if not input_path.is_file():
        note(f"NOT-VERIFIED: набора нет: {input_path}")
        return EXIT_NOT_VERIFIED

    gen_paths = [CASE / p for p in args.generations]
    missing = [str(p) for p in gen_paths if not p.is_file()]
    if missing:
        note("NOT-VERIFIED: нет отчётов генераций: " + "; ".join(missing))
        return EXIT_NOT_VERIFIED

    units: list[dict] = []
    controls: list[dict] = []
    for p in gen_paths:
        u, c = loop_units_from_report(p)
        units += u
        controls += c
        note(f"  {_rel(p)}: юнитов петель {len(u)}, контрольных {len(c)}")
    if not units:
        note("NOT-VERIFIED: в генерациях нет ни одной зацикленной — юнитов петель нет")
        return EXIT_NOT_VERIFIED

    targets = {u["unit"] for u in units} | {c["unit"] for c in controls}
    note(f"проход по набору: {len(targets)} целевых 4-грамм, "
         f"{args.limit or 'все'} примеров")

    rows, scan_info = scan_dataset(input_path, targets, args.limit)
    if not rows:
        note("NOT-VERIFIED: набор пуст или не разобран")
        return EXIT_NOT_VERIFIED
    duplication_index(rows)
    for r in rows:
        r["bucket"] = bucket_of(r["words_prose"])

    length_ctrl = length_controlled(rows)
    prov = provenance(units, controls, scan_info["per_unit"])
    seen = {r["dup_hash"] for r in rows}
    out = {
        "schema": "s3al-loop-repetition/1",
        "stage": "S3al — повторы в данных SFT: нужна ли работа с данными/наградой "
                 "(подготовка решения, не решение)",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "measured",
        "question": "объясняется ли петлевость примеров дубликатностью после "
                    "выравнивания по длине, и воспроизводит ли модель петлю из набора",
        #: Хеш прибора — в отчёте, а не только в имени файла: если прибор позже
        #: изменится, свод увидит это и скажет, а не пересчитает числа по новой
        #: арифметике молча.
        "tool": "tools/analyze_loop_repetition.py",
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "method": {
            "dataset": {"path": args.input, "bytes": input_path.stat().st_size,
                        "sha256": sha256_file(input_path),
                        "n_examples": len(rows), "n_unique_normalized": len(seen),
                        "duplicates_share": _share(len(rows) - len(seen), len(rows)),
                        "n_bad_lines": scan_info["n_bad_lines"]},
            "generations": [{"path": _rel(p), "sha256": sha256_file(p)} for p in gen_paths],
            "loops_from": "probe_language_split.LOOP_MAX4GRAM_REP (импорт, пиннут)",
            "metric_from": "probe_control.degenerate_metrics (импорт)",
            "readings_from": "analyze_sft_loops (импорт): проза, нормализация, петлевость",
            "instrument": LS.INSTRUMENT_VERSION,
            "length_buckets": LENGTH_BUCKET_LABELS,
            "length_bucket_edges_words": LENGTH_BUCKETS,
            "control_units_per_generation": CONTROL_UNITS_PER_GENERATION,
            "targets": len(targets),
            "partial": bool(args.limit),
        },
        "length_controlled_duplication": length_ctrl,
        "provenance": prov,
        "verdict": verdict(length_ctrl, prov),
        "caveats": [
            "петлевость примера — свойство ТЕКСТА набора, а не модели: прибор не "
            "утверждает, что зацикливание генераций вызвано данными",
            "юниты взяты из greedy-генераций прежних замеров (S3ak): штатный режим "
            "(запрет повторов 4-грамм) петлю снимает механически, и юнитов в нём нет "
            "по построению — вопрос «откуда петля» задаётся там, где петля есть",
            "дубликат — совпадение нормализованного текста, а не смысловое",
            "контроль берётся из той же генерации, что и юнит, но контрольных юнитов "
            f"в {CONTROL_UNITS_PER_GENERATION} раза больше — доли сравнимы, абсолютные "
            "числа нет",
            "порог петель 8 повторов 4-граммы грубый намеренно (тот же, что у "
            "генераций): задача — отделить вырождение от длинного честного текста",
        ],
        "open_questions": [
            "юниты петель взяты из 177 зацикленных генераций 13 состояний: если петля "
            "у разных состояний разная, смешение состояний усредняет её источник; "
            "разрез по состояниям лежит в top по каждому юниту",
        ],
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        note(f"отчёт: {args.out}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK


# ─────────────────────── самопроверка на фикстурах ───────────────────────

FIXTURES = [
    # (имя, записи jsonl, целевые 4-граммы, ожидания)
    ("петля в одном примере",
     [{"messages": [{"role": "assistant", "content": "альфа бета гамма дельта " * 10}]}],
     {"альфа бета гамма дельта"},
     {"occurrences": 10, "examples": 1, "max_in_example": 10}),
    ("юнит встречается в двух примерах по разу",
     [{"messages": [{"role": "assistant", "content": "раз два три четыре"}]},
      {"messages": [{"role": "assistant", "content": "пять раз два три четыре шесть"}]}],
     {"раз два три четыре"},
     {"occurrences": 2, "examples": 2, "max_in_example": 1}),
    ("юнит только в user-сообщении не считается",
     [{"messages": [{"role": "user", "content": "раз два три четыре"},
                    {"role": "assistant", "content": "ответ"}]}],
     {"раз два три четыре"},
     {"occurrences": 0, "examples": 0, "max_in_example": 0}),
]


def selftest() -> int:
    """Фикстуры разбора: без них «нашлось в наборе» — обещание, а не факт.

    Проверяется ровно то, что легко сломать правкой: считается ли повтор внутри
    примера, считается ли вхождение в разных примерах, и не считается ли вход
    пользователя (модель его не порождает).
    """
    import tempfile
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, records, targets, expect in FIXTURES:
            path = Path(tmp) / "fx.jsonl"
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records),
                            encoding="utf-8")
            _, info = scan_dataset(path, set(targets))
            unit = next(iter(targets))
            got = info["per_unit"][unit]
            for key, want in expect.items():
                if got[key] != want:
                    bad.append(f"{name}: {key} = {got[key]}, ожидалось {want}")
    if bad:
        note("SELFTEST FAIL: " + "; ".join(bad))
        return EXIT_FAIL
    note(f"SELFTEST OK: {len(FIXTURES)} фикстур подсчёта юнитов")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help="набор SFT (jsonl), читается только на чтение")
    ap.add_argument("--generations", action="append", default=None, metavar="JSON",
                    help="отчёт проб с зацикленными генерациями (повторяемый; "
                         "по умолчанию — разборы S3ak по шагам и по режимам)")
    ap.add_argument("--out", default=None, help="файл отчёта (JSON)")
    ap.add_argument("--limit", type=int, default=None,
                    help="ограничить число примеров (для проверки прибора; в отчёт "
                         "попадёт partial=true)")
    ap.add_argument("--selftest", action="store_true",
                    help="прогнать фикстуры подсчёта и выйти")
    args = ap.parse_args()
    if args.generations is None:
        args.generations = list(DEFAULT_GENERATIONS)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
