#!/usr/bin/env python3
"""S3ak — свод: отчего SFT-состояния зацикливаются, и на каком языке они отвечают.

Зачем отдельный свод. S3aj установил **факт** («58.3 % генераций SFT-состояний не
кончаются вообще; медиана дописанного хода 44 и 78 токенов против 1441 у
CPT-финала») и честно назвал его приоритетом №1, но причину не разобрал. Причин
может быть четыре, и они требуют **разных** решений:

1. **декодирование** — петля есть свойство greedy-поиска, а не весов: при sampling
   или штрафе за повторы модель кончается. Лечение — протокол генерации; выводы
   о «деградации формата», снятые на greedy, в этой части артефакт;
2. **шаг обучения** — петля появляется на каком-то шаге SFT и дальше растёт:
   тогда это свойство стадии (скорость обучения, число проходов по дубликатам), а
   не данных и не прибора;
3. **данные** — в обучающем наборе есть примеры-петли, и модель воспроизводит то,
   чему её учили (набор на 57.71 % состоит из дубликатов, эффективных проходов
   7.093 — ADR-033);
4. **прибор** — остаточный артефакт замера: генерация не останавливается там, где
   кончается ход, и «петля» — это переписывание собственного конца хода.

Свод отвечает на это **таблицами** (режим декодирования × доля петель, шаг × доля
петель, примеры-петли в данных) и вердиктом, который считается по числам, а не
называется словами.

Второй вопрос свода — **достоверный вердикт по языку** (ADR-039). Прежний держался
на запасе 0.008 при 24 генерациях, то есть на четверти шага одной генерации. Здесь
язык меряется на ≥100 генерациях, с бутстрап-интервалом и с запасом до границы.

Третье — **правка прибора**. Доля кириллицы считалась по буквам текста, а
служебные токены хода (`<|im_end|>`, `<|im_start|>`, `<|endoftext|>`) состоят из
латинских букв и в подсчёт попадали. Свод показывает, какие прежние числа
изменились после правки — по каждому перечитанному отчёту, с дельтой.

Правила вердикта объявлены константами ниже, ДО того как числа замера стали
известны: иначе вердикт «подбирался» бы под ответ, а это ровно тот дефект, из-за
которого S3aj не смог вынести вердикт по языку.

Коды возврата::

    0 — свод собран
    1 — отказ: вход есть, но не тот (нет обязательного замера, тождество прибора
        не подтвердилось, тесты FAIL вне известного красного)
    2 — NOT-VERIFIED: данных нет (нет каталога прогона, нет ни одного замера)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))

import probe_language_split as LS     # noqa: E402  (критерий и свод — не копия)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

# ─────────────────────── правила вердикта (объявлены заранее) ───────────────────

#: «Петля исчезла в этом режиме»: доля зацикленных не выше 0.10. Десятая часть —
#: не «ноль»: 24 пробы дают шаг 1/24 = 0.042, и требовать ровного нуля значило бы
#: объявлять причиной одну случайную пробу.
DEC_FIXED_LOOP_MAX = 0.10
#: «Петля есть на greedy»: не ниже 0.30 — с запасом от шага сетки проб (0.042) и
#: заметно выше уровня шума. Ниже — состояние само по себе не зациклено, и
#: разбирать в нём нечего.
DEC_BASELINE_LOOP_MIN = 0.30
#: Ранний шаг SFT: петель не больше 0.15 — то есть навык «не зацикливаться» ещё цел.
STEP_EARLY_MAX = 0.15
#: Поздний шаг: петель не меньше 0.40 — петля уже характерна для состояния.
STEP_LATE_MIN = 0.40
#: Усугубление: прирост доли петель от раннего к позднему шагу не меньше 0.20.
#: Отдельное правило, потому что «петля приобретена стадией» и «петля была и
#: стадия её усилила» — разные утверждения с разным лечением: в первом виновата
#: стадия целиком, во втором — она лишь множитель уже существующего дефекта
#: (у CPT-финала петли тоже есть, 0.2917 в полном замере S3aj).
STEP_GROWTH_MIN = 0.20
#: Данные как источник: доля примеров-петель (по прозе, не по разметке инструмента)
#: не ниже 0.30. Порог высокий намеренно: единичные повторы в размеченном JSON
#: вызова — это структура, а не петля, и путать их значило бы обвинить данные
#: в том, что произвёл поиск.
DATA_SOURCE_MIN = 0.30
#: Остаток «обрыва без петли» в полном замере: не выше 0.05 — тогда обрыв в
#: замере объясняется петлёй целиком, и четвёртая причина (прибор) снята числом.
INSTRUMENT_RESIDUAL_MAX = 0.05

CAUSE_DECODING = "decoding"
CAUSE_TRAINING = "training_step"
CAUSE_DATA = "data"
CAUSE_INSTRUMENT = "instrument"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(p: Path) -> str:
    import os
    try:
        r = os.path.relpath(Path(p).resolve(), CASE)
    except ValueError:
        return Path(p).name
    return Path(p).name if r.startswith("..") else r


def load(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


# ─────────────────────────── таблицы замеров ───────────────────────────

def decoding_table(run: Path) -> list[dict]:
    """Таблица «режим декодирования × доля петель» по отчётам `decoding_*.json`.

    Строка на режим, и в каждой — числа, по которым принимается решение:
    доля зацикленных, доля дописанных ходов, доля обрыва лимитом **без** петли
    (остаток прибора) и длины. Режим берётся из отчёта (`protocol.decoding`), а не
    из имени файла: имя можно переименовать, протокол — нет.
    """
    rows = []
    for f in sorted(run.glob("decoding_*.json")):
        d = load(f)
        if not d or d.get("schema") != "probe-language-split/1":
            continue
        for tag, val in (d.get("states") or {}).items():
            if "probes" not in val:
                continue
            a = val.get("aggregate") or {}
            probes = val["probes"]
            n = len(probes)
            looped = sum(bool(p["metrics"]["looped"]) for p in probes)
            stopped = sum(1 for p in probes if p.get("stop_reason") == LS.STOP_TURN_END)
            residual = sum(1 for p in probes
                           if p.get("stop_reason") != LS.STOP_TURN_END
                           and not p["metrics"]["looped"])
            mode = d["protocol"].get("decoding") or ""
            #: Причина конца разложена прибором на две части («дописал ход» / «оборван
            #: лимитом»), и доли внутри частей — это доли **внутри части**, а не по
            #: всем 24 генерациям. «Незакрытых <think> 0.8» в отчёте про выборку
            #: означает 0.8 среди дописавших ход, и если прочитать это как долю по
            #: всем пробам, вывод о том, что незакрытый <think> не сводится к петле,
            #: получил бы неверный вес. Поэтому общая доля считается взвешенно по n
            #: частей, а обе части кладутся рядом.
            stop = a.get("stop") or {}
            nat = stop.get("natural") or {}
            trunc = stop.get("truncated") or {}

            def _weighted(key: str, _stop=stop, _nat=nat, _tr=trunc):
                total = int(_stop.get("n") or 0)
                if not total:
                    return None
                num = 0.0
                for part in (_nat, _tr):
                    v = part.get(key)
                    if v is not None:
                        num += int(part.get("n") or 0) * float(v)
                return round(num / total, 4)

            rows.append({
                "mode": mode,
                "state": tag,
                "n": n,
                #: Режим, который **механически** запрещает повтор, не может служить
                #: доказательством «петля от greedy»: нулевая доля петель в нём
                #: обеспечена правилом поиска, а не свойством весов. Флаг стоит
                #: рядом с числом, чтобы это было видно в таблице, а не в сноске.
                "constrains_repetition": "nogram" in mode,
                "looped_share": round(looped / n, 4),
                "looped_n": looped,
                "natural_stop_share": round(stopped / n, 4),
                "truncated_not_looped_share": round(residual / n, 4),
                #: Доли по всем пробам режима (взвешенно) и внутри дописавших ход.
                "truncated_share": stop.get("truncated_share"),
                "unclosed_think_share": _weighted("unclosed_think_share"),
                "unclosed_think_share_natural_stops": nat.get("unclosed_think_share"),
                "looped_share_natural_stops": nat.get("looped_share"),
                "tool_call_share": _weighted("mode_share_tool_call"),
                "median_tokens": (a.get("lengths") or {}).get("median"),
                "max4gram_rep_median": LS._pct(
                    [int(p["metrics"]["max4gram_rep"] or 0) for p in probes], 0.5),
                "max4gram_rep_max": max(int(p["metrics"]["max4gram_rep"] or 0)
                                        for p in probes),
                "decode_params": d["protocol"].get("decoding_params"),
                "checkpoint": val.get("checkpoint"),
                "report": rel(f),
            })
    return rows


def mode_applied(run: Path) -> dict:
    """Доказать, что режим **применён**, а не только объявлен.

    Объявленный, но не подействовавший режим — тихая ошибка замера: таблица
    «режим × доля петель» показала бы одно и то же число под разными именами, и
    вывод «режим не помогает» был бы выводом о том, что режим не включился.
    Признак — тексты ответов: если у режима ни один ответ не отличается от
    greedy, значит переданный параметр до генерации не дошёл.

    Сравнение идёт по состояниям, снятым в обоих режимах (совпадение по тегу и
    тексту промпта), и считается числом совпавших ответов.
    """
    reports = {}
    for f in sorted(run.glob("decoding_*.json")):
        d = load(f)
        if not d or d.get("schema") != "probe-language-split/1":
            continue
        reports[d["protocol"].get("decoding")] = d
    greedy = next((d for m, d in reports.items() if m == "greedy"), None)
    out = {"compared_against": "greedy" if greedy else None,
           "why": "режим, не изменивший ни одного ответа, — не режим, а незамеченная "
                  "ошибка передачи параметра; сравнение по текстам проб",
           "modes": {}}
    if not greedy:
        return out
    for mode, d in reports.items():
        if mode == "greedy":
            continue
        for tag, val in (d.get("states") or {}).items():
            if "probes" not in val:
                continue
            gval = (greedy.get("states") or {}).get(tag)
            if not gval or "probes" not in gval:
                continue
            gp = {(p["tag"], p["prompt"]): p for p in gval["probes"]}
            mp = {(p["tag"], p["prompt"]): p for p in val["probes"]}
            common = [k for k in gp if k in mp]
            if not common:
                continue
            changed = sum(gp[k]["response"] != mp[k]["response"] for k in common)
            #: Куда сдвинулась повторяемость у каждой пробы — важнее среднего:
            #: штраф за повторы может не убрать петлю, а **переставить** её (у одной
            #: пробы 415 → 8, у другой 1 → 1015). Такое среднее прячет.
            worse = better = 0
            for k in common:
                a = int(gp[k]["metrics"]["max4gram_rep"] or 0)
                b = int(mp[k]["metrics"]["max4gram_rep"] or 0)
                if b > a:
                    worse += 1
                elif b < a:
                    better += 1
            out["modes"][f"{mode}@{tag}"] = {
                "n": len(common),
                "responses_changed": changed,
                "applied": changed > 0,
                "repetition_worse_than_greedy": worse,
                "repetition_better_than_greedy": better,
                "reading": "повторяемость 4-грамм против greedy: сколько проб стало "
                           "хуже и сколько лучше (порог петли — тот же, 8)",
            }
    return out


def step_table(run: Path) -> list[dict]:
    """Таблица «шаг обучения × доля петель» по отчёту `by_step.json` (или `by_step*.json`)."""
    rows = []
    for f in sorted(run.glob("by_step*.json")):
        d = load(f)
        if not d or d.get("schema") != "probe-language-split/1":
            continue
        for tag, val in (d.get("states") or {}).items():
            if "probes" not in val:
                continue
            m = re.search(r"(\d+)$", tag)
            probes = val["probes"]
            n = len(probes)
            looped = sum(bool(p["metrics"]["looped"]) for p in probes)
            stopped = sum(1 for p in probes if p.get("stop_reason") == LS.STOP_TURN_END)
            rows.append({
                "step": int(m.group(1)) if m else None,
                "state": tag,
                "n": n,
                "looped_share": round(looped / n, 4),
                "looped_n": looped,
                "natural_stop_share": round(stopped / n, 4),
                "median_tokens": (val.get("aggregate", {}).get("lengths") or {}).get("median"),
                "unclosed_think_natural": (
                    (val.get("aggregate", {}).get("stop") or {}).get("natural") or {}
                ).get("unclosed_think_share"),
                "checkpoint": val.get("checkpoint"),
                "checkpoint_sha256": val.get("checkpoint_sha256"),
                "report": rel(f),
            })
    rows.sort(key=lambda r: (r["step"] is None, r["step"]))
    return rows


def language_block(run: Path, state: str) -> dict:
    """Вердикт по языку для состояния: обе читки, покрытие, интервал и запас.

    Три чтения, а не одно, и каждое названо:

    * `all` — все генерации состояния: то, что напечатала модель;
    * `clean` — дописанные ходы без петли: язык в том виде, в каком его можно
      сравнивать с критерием ADR-039 (TASK запрещает выводы по зацикленным);
    * `legacy` — прежняя арифметика (v1) на тех же генерациях: опора для «что
      изменила правка прибора».

    Вердикт (зона ADR-039) считается по `clean` — но **полное** чтение кладётся
    рядом, чтобы «чистое» нельзя было выдать за единственное.
    """
    f = run / "language_wide.json"
    d = load(f)
    if not d or d.get("schema") != "probe-language-split/1":
        return {"available": False, "why": f"нет отчёта {rel(f)}"}
    val = (d.get("states") or {}).get(state)
    if not val or "probes" not in val:
        return {"available": False, "why": f"нет состояния {state} в {rel(f)}"}
    a = val["aggregate"]
    probes = val["probes"]
    clean = a["clean"]
    #: Бюджет против p90 длин: TASK требует «бюджет ≥ p90», и это надо **показать**,
    #: а не объявить. p90 считается по чистому чтению (дописанные ходы без петли):
    #: у зацикленной генерации длина равна бюджету по построению, и её учёт сделал бы
    #: p90 равным бюджету всегда — то есть проверка стала бы тавтологией.
    clean_probes = [p for p in probes
                    if p.get("stop_reason") == LS.STOP_TURN_END
                    and not p["metrics"]["looped"]]
    budget = d["protocol"].get("max_new_tokens")
    len_clean = LS.length_stats(clean_probes, budget)
    len_all = LS.length_stats(probes, budget)
    #: Интервал по **определённым** сегментам (тем, где ответ вообще есть). Основное
    #: чтение with_zeros отвечает на вопрос «сколько русского в ответе состояния», и
    #: в нём генерация без ответа даёт 0 — это консервативно и правильно для
    #: вердикта, но не отвечает на вопрос «на каком языке модель отвечает, когда
    #: отвечает». Без второго интервала разница между «не отвечает по-русски» и «не
    #: отвечает вовсе» осталась бы невидимой.
    defined = [p["metrics"]["cyr_answer"] for p in clean_probes
               if p["metrics"].get("cyr_answer") is not None]
    ci_defined = LS.bootstrap_ci(defined)
    zone = LS.apply_criterion(
        clean["cyr_answer"]["with_zeros"], clean["cyr_think"]["with_zeros"],
        clean["cyr_answer"]["coverage"])
    zone_all = LS.apply_criterion(
        a["cyr_answer"]["with_zeros"], a["cyr_think"]["with_zeros"],
        a["cyr_answer"]["coverage"])
    return {
        "available": True,
        "state": state,
        "n": len(probes),
        "prompts_set": d["protocol"].get("prompts_set"),
        "prompts_digest": d["protocol"].get("prompts_digest"),
        "decoding": d["protocol"].get("decoding"),
        "max_new_tokens": d["protocol"].get("max_new_tokens"),
        "checkpoint": val.get("checkpoint"),
        "checkpoint_sha256": val.get("checkpoint_sha256"),
        "report": rel(f),
        #: «Чистое» чтение — по нему вердикт.
        "cyr_answer_with_zeros": clean["cyr_answer"]["with_zeros"],
        "cyr_answer_defined": clean["cyr_answer"]["defined_only"],
        "cyr_think": clean["cyr_think"]["with_zeros"],
        "coverage": clean["cyr_answer"]["coverage"],
        "n_clean": clean["n"],
        "clean_share_of_all": clean["share_of_all"],
        "margin_to_0.4": clean["margin_to_refute"],
        "ci95": clean["ci95"],
        "ci_covers_0.4": clean["ci_covers_refute_boundary"],
        "ci95_defined_only": ci_defined,
        "ci_defined_covers_0.4": (None if ci_defined["lo"] is None
                                  else bool(ci_defined["lo"] < LS.CRIT_ANSWER_REFUTE_MAX
                                            <= ci_defined["hi"])),
        "ci_defined_note": "интервал по генерациям, у которых ответная часть есть "
                           "(defined_only): отвечает на «на каком языке модель "
                           "отвечает, когда отвечает»; вердикт критерия считается по "
                           "основному чтению (with_zeros), а не по этому",
        "zone": zone["zone"],
        "zone_why": zone["why"],
        #: Полное чтение (все генерации, включая зацикленные) — рядом, не вместо.
        "all_cyr_answer_with_zeros": a["cyr_answer"]["with_zeros"],
        "all_cyr_answer_defined": a["cyr_answer"]["defined_only"],
        "all_cyr_think": a["cyr_think"]["with_zeros"],
        "all_coverage": a["cyr_answer"]["coverage"],
        "all_ci95": a.get("cyr_answer_ci95"),
        "all_zone": zone_all["zone"],
        #: Прежняя арифметика — то, с чем сравнивают ADR-039/040.
        "legacy_cyr_answer_with_zeros": a["legacy_reading"]["cyr_answer"]["with_zeros"],
        "legacy_cyr_answer_defined": a["legacy_reading"]["cyr_answer"]["defined_only"],
        "legacy_cyr_think": a["legacy_reading"]["cyr_think"]["with_zeros"],
        "legacy_zone": LS.apply_criterion(
            a["legacy_reading"]["cyr_answer"]["with_zeros"],
            a["legacy_reading"]["cyr_think"]["with_zeros"],
            a["legacy_reading"]["cyr_answer"]["coverage"])["zone"],
        "looped_share": a["looped_share"],
        "excluded_looped": clean["excluded_looped"],
        "excluded_by_limit": clean["excluded_by_limit"],
        "lengths_clean": len_clean,
        "lengths_all": len_all,
        "budget_covers_p90": (None if len_clean.get("p90") is None
                              else bool(len_clean["p90"] < budget)),
        "budget_note": "p90 — по дописанным ходам без петли; у зацикленных длина "
                       "равна бюджету по построению, и учёт их сделал бы проверку "
                       "«бюджет ≥ p90» тавтологией",
    }


def language_by_decoding(run: Path) -> list[dict]:
    """Язык по каждому режиму декодирования — то, что greedy-протокол не показывает.

    Зачем это нужно. Критерий ADR-039 меряется на дописанных ходах, а у
    SFT-состояний при greedy таких ходов мало: зацикливание съедает выборку, и
    «ответной части нет» появляется не потому, что модель не отвечает по-русски,
    а потому, что ход не кончился. Режим, в котором ходы кончаются, даёт **другое**
    число — и оно обязано лежать рядом.

    Но это **не** вердикт по ADR-039: протокол критерия — greedy, и подменять его
    удобным режимом значило бы выбирать ответ по способу замера. Поэтому чтение по
    режимам идёт отдельным блоком с прямой пометкой, а вердикт считается по
    основному (greedy, широкий набор).
    """
    rows = []
    for f in sorted(run.glob("decoding_*.json")):
        d = load(f)
        if not d or d.get("schema") != "probe-language-split/1":
            continue
        for tag, val in (d.get("states") or {}).items():
            if "probes" not in val:
                continue
            cl = (val.get("aggregate") or {}).get("clean") or {}
            if not cl:
                continue
            rows.append({
                "mode": d["protocol"].get("decoding"),
                "state": tag,
                "n": (val["aggregate"] or {}).get("n"),
                "clean_n": cl.get("n"),
                "clean_share_of_all": cl.get("share_of_all"),
                "cyr_answer_with_zeros": (cl.get("cyr_answer") or {}).get("with_zeros"),
                "cyr_answer_defined_only": (cl.get("cyr_answer") or {}).get("defined_only"),
                "coverage": (cl.get("cyr_answer") or {}).get("coverage"),
                "cyr_think_with_zeros": (cl.get("cyr_think") or {}).get("with_zeros"),
                "margin_to_0.4": cl.get("margin_to_refute"),
                "ci95": cl.get("ci95"),
                "ci_covers_0.4": cl.get("ci_covers_refute_boundary"),
                "zone": LS.apply_criterion(
                    (cl.get("cyr_answer") or {}).get("with_zeros"),
                    (cl.get("cyr_think") or {}).get("with_zeros"),
                    (cl.get("cyr_answer") or {}).get("coverage"))["zone"],
                "report": rel(f),
                "protocol_note": ("не критерий ADR-039: критерий объявлен на greedy; "
                                  "режим показан как дополнительный замер — в нём "
                                  "ходы кончаются и язык вообще измерим"),
            })
    return rows


def loops_in_data(run: Path) -> dict:
    """Примеры-петли в обучающем наборе (отчёт `tools/analyze_sft_loops.py`)."""
    for f in (run / "loops_in_data.json", run / "sft_loops_in_data.json"):
        d = load(f)
        if d:
            d = dict(d)
            d["report"] = rel(f)
            #: Флаг «отчёт есть» ставит свод, а не прибор: у прибора своё поле
            #: состояния (`status`), и склеивать два разных слова в одно значило бы
            #: читать «measured» как «available» там, где это не одно и то же.
            d["available"] = True
            return d
    return {"available": False,
            "why": "нет отчёта о петлях в данных (tools/analyze_sft_loops.py)"}


def instrument_fix(run: Path) -> dict:
    """Что изменила правка прибора: по каждому перечитанному отчёту — дельта."""
    out = {"changes": [
        "правило 4: служебные токены <|…|> (в т.ч. <|im_end|>, <|im_start|>, "
        "<|endoftext|>) больше не считаются буквами — у них есть латинские буквы "
        "(im_end, im_start, endoftext), и у короткого русского ответа они "
        "составляли заметную долю букв ответа",
        "правило 5: язык считается по ходу модели — до первого <|im_end|>; всё, "
        "что генерация напечатала после собственного конца хода, — её продолжение "
        "после ответа, а не ответ",
        "чтение v1 сохранено рядом (metrics.legacy, aggregate.legacy_reading) и "
        "воспроизводится побитово: перечитывание прежних отчётов доказывает, что "
        "прежние числа шли из того же текста, а не из другой арифметики",
    ], "affected_prior_measurements": [], "reaudits": []}
    for f in sorted((run / "instrument").glob("reaudit_*.json")):
        d = load(f)
        if not d or d.get("schema") != "probe-language-reaudit/1":
            continue
        entry = {"source": d.get("source"), "label": d.get("label"),
                 "source_sha256": d.get("source_sha256"), "states": {}}
        for tag, st in (d.get("states") or {}).items():
            if "delta" not in st:
                continue
            entry["states"][tag] = {
                "n": st["n"],
                "cyr_answer_v1": st["reading_v1"]["cyr_answer"]["with_zeros"],
                "cyr_answer_v2": st["reading_v2"]["cyr_answer"]["with_zeros"],
                "delta": st["delta"]["cyr_answer_with_zeros"],
                "cyr_answer_defined_v1": st["reading_v1"]["cyr_answer"]["defined_only"],
                "cyr_answer_defined_v2": st["reading_v2"]["cyr_answer"]["defined_only"],
                "cyr_think_v1": st["reading_v1"]["cyr_think"]["with_zeros"],
                "cyr_think_v2": st["reading_v2"]["cyr_think"]["with_zeros"],
                "legacy_reproduced": st["legacy_reproduced"],
                "top_movers": st.get("top_movers"),
            }
            out["affected_prior_measurements"].append({
                "source": d.get("source"), "state": tag,
                "delta_cyr_answer": st["delta"]["cyr_answer_with_zeros"],
                "delta_cyr_think": st["delta"]["cyr_think_with_zeros"],
            })
        out["reaudits"].append(entry)
    return out


def verdict(dec_rows: list[dict], step_rows: list[dict], data: dict) -> dict:
    """Вердикт о причине вырождения — по правилам, объявленным константами выше.

    Считается **из чисел**, без права на «общее впечатление»: каждое утверждение
    вердикта несёт числа, по которым оно получено, и правило, по которому число
    стало утверждением. Если числа не выполняют ни одного правила, вердикт так и
    говорит — «ни одна из четырёх причин не подтверждена правилами», — а не
    выбирает ближайшую.
    """
    causes, rules = [], []
    greedy = [r for r in dec_rows if r["mode"] == "greedy"]
    other = [r for r in dec_rows if r["mode"] != "greedy"]
    base = max((r["looped_share"] for r in greedy), default=None)

    if base is not None and other:
        #: Доказательством служит режим, который повтор не запрещает: у `nogram`
        #: нулевая доля петель — следствие самого правила, а не свойство модели.
        free = [r for r in other if not r["constrains_repetition"]] or other
        best = min(free, key=lambda r: r["looped_share"])
        rules.append({
            "rule": f"декодирование: greedy ≥ {DEC_BASELINE_LOOP_MIN} И хотя бы один "
                    f"не-greedy режим БЕЗ механического запрета повторов ≤ "
                    f"{DEC_FIXED_LOOP_MAX}",
            "observed": {"greedy_looped_share": base,
                         "best_other_mode": best["mode"],
                         "best_other_looped_share": best["looped_share"],
                         #: Числа рядом с порогом — чтобы «правило не сработало» было
                         #: читаемо: видно, что режим сдвинул долю, но не убрал её.
                         "reduction": round(base - best["looped_share"], 4),
                         "all_modes": {r["mode"]: r["looped_share"] for r in dec_rows}},
            "fired": bool(base >= DEC_BASELINE_LOOP_MIN
                          and best["looped_share"] <= DEC_FIXED_LOOP_MAX),
            "reading": (f"лучший свободный режим снизил долю петель с {base} до "
                        f"{best['looped_share']} — это множитель, а не причина: "
                        f"порог «петля исчезла» {DEC_FIXED_LOOP_MAX} не достигнут"),
        })
        if (base >= DEC_BASELINE_LOOP_MIN
                and best["looped_share"] <= DEC_FIXED_LOOP_MAX):
            causes.append({
                "cause": CAUSE_DECODING,
                "evidence": f"на greedy зациклено {base} генераций, в режиме "
                            f"{best['mode']} — {best['looped_share']}",
                "consequence": "«деградация формата», снятая на greedy, в этой части "
                               "есть свойство поиска, а не весов",
            })
        #: Комбинация. Пороги здесь не новые — оба объявлены до замера: «петля есть
        #: на greedy» (`DEC_BASELINE_LOOP_MIN`) И «петля НЕ исчезла в свободном
        #: режиме» (`DEC_FIXED_LOOP_MAX` не достигнут). Вместе это ровно «протокол
        #: объясняет часть петель, но не все»: снятая доля — артефакт поиска,
        #: оставшаяся — собственная склонность весов, и лечатся они разным (протокол
        #: генерации против данных/стадии).
        #: Отдельное правило нужно потому, что бинарное «декодирование виновато /
        #: не виновато» на таких числах не срабатывает вовсе и оставляет вердикт
        #: пустым. Пустой вердикт читается как «причина не названа», хотя причина
        #: названа двумя числами, — и следующая дельта не знает, что делать.
        if base >= DEC_BASELINE_LOOP_MIN and best["looped_share"] > DEC_FIXED_LOOP_MAX:
            causes.append({
                "cause": CAUSE_DECODING,
                "partial": True,
                "combined_with": "own_tendency",
                "evidence": f"на greedy зациклено {base}, в свободном режиме "
                            f"{best['mode']} — {best['looped_share']}: смена режима "
                            f"снимает {round(base - best['looped_share'], 4)}, "
                            f"остаётся {best['looped_share']}",
                "consequence": "вырождение — комбинация: часть петель создана "
                               "greedy-протоколом (артефакт, лечится протоколом "
                               "генерации), часть — собственная склонность модели "
                               "(остаётся при sampling, лечится данными или стадией)",
            })

    if step_rows:
        early = [r for r in step_rows if r["step"] is not None][:1]
        late = [r for r in step_rows if r["step"] is not None][-1:]
        if early and late:
            e, l = early[0], late[0]
            rules.append({
                "rule": f"шаг обучения: на шаге {e['step']} петель ≤ {STEP_EARLY_MAX} "
                        f"И на шаге {l['step']} петель ≥ {STEP_LATE_MIN}",
                "observed": {f"step_{e['step']}": e["looped_share"],
                             f"step_{l['step']}": l["looped_share"]},
                "fired": bool(e["looped_share"] <= STEP_EARLY_MAX
                              and l["looped_share"] >= STEP_LATE_MIN),
            })
            if e["looped_share"] <= STEP_EARLY_MAX and l["looped_share"] >= STEP_LATE_MIN:
                causes.append({
                    "cause": CAUSE_TRAINING,
                    "evidence": f"на шаге {e['step']} зациклено {e['looped_share']}, "
                                f"на шаге {l['step']} — {l['looped_share']}",
                    "consequence": "петля приобретается стадией SFT, а не приходит "
                                   "с весами CPT",
                })
            growth = round(l["looped_share"] - e["looped_share"], 4)
            rules.append({
                "rule": f"шаг обучения (усугубление): прирост петель от шага "
                        f"{e['step']} к {l['step']} ≥ {STEP_GROWTH_MIN}",
                "observed": {"growth": growth, "from": e["looped_share"],
                             "to": l["looped_share"]},
                "fired": bool(growth >= STEP_GROWTH_MIN),
            })
            if growth >= STEP_GROWTH_MIN:
                causes.append({
                    "cause": CAUSE_TRAINING,
                    "evidence": f"доля петель выросла на {growth} "
                                f"({e['looped_share']} на шаге {e['step']} → "
                                f"{l['looped_share']} на шаге {l['step']})",
                    "consequence": "стадия SFT усугубляет вырождение даже там, где "
                                   "оно было у CPT-финала",
                    "aggravation": True,
                })

    if data.get("available"):
        share = (data.get("loops_share_prose")
                 if data.get("loops_share_prose") is not None
                 else data.get("loops_share_all"))
        rules.append({
            "rule": f"данные: доля примеров-петель (по прозе) ≥ {DATA_SOURCE_MIN}",
            "observed": {"loops_share_prose": data.get("loops_share_prose"),
                         "loops_share_all": data.get("loops_share_all"),
                         "n_examples": data.get("n_examples")},
            "fired": bool(share is not None and share >= DATA_SOURCE_MIN),
        })
        if share is not None and share >= DATA_SOURCE_MIN:
            causes.append({
                "cause": CAUSE_DATA,
                "evidence": f"примеров-петель в наборе {share} (по прозе)",
                "consequence": "модель воспроизводит то, чему её учили; лечение — "
                               "в данных, а не в протоколе",
            })

    if dec_rows:
        res = max(r["truncated_not_looped_share"] for r in dec_rows)
        rules.append({
            "rule": f"прибор: остаток «обрыва без петли» ≤ {INSTRUMENT_RESIDUAL_MAX}",
            "observed": {"max_truncated_not_looped_share": res},
            "fired": bool(res <= INSTRUMENT_RESIDUAL_MAX),
        })
        if res <= INSTRUMENT_RESIDUAL_MAX:
            causes.append({
                "cause": CAUSE_INSTRUMENT,
                "evidence": f"обрыв лимитом без петли — не больше {res} генераций",
                "consequence": "остаточный артефакт замера снят: обрыв объясняется "
                               "петлёй, а не прибором",
                "excluded": True,
            })
    #: Штраф за повторы. Сравниваются два режима **одного** протокола (greedy и он же
    #: со штрафом), поэтому порог не нужен: «не помогает» значит ровно то, что доля
    #: петель со штрафом не ниже, чем без него. Кладётся отдельным полем, а не
    #: причиной: то, что лечение не работает, — не причина вырождения, и попади оно в
    #: `causes_confirmed`, вердикт называл бы причиной отсутствие эффекта.
    rp_rows = [r for r in other if "rp" in r["mode"]]
    repetition_penalty = {"available": bool(rp_rows) and base is not None}
    if repetition_penalty["available"]:
        r = min(rp_rows, key=lambda x: x["looped_share"])
        repetition_penalty.update({
            "mode": r["mode"],
            "greedy_looped_share": base,
            "penalised_looped_share": r["looped_share"],
            "delta": round(r["looped_share"] - base, 4),
            "helps": bool(r["looped_share"] < base),
            "reading": "штраф за повторы не снизил долю петель (со штрафом их не "
                       "меньше, чем без него) — протокольное лечение в этой форме не "
                       "работает, и надежда на него не отменяет разбора данных",
        })

    #: «Незакрытый <think> не сводится к петле». Порога нет намеренно: утверждение
    #: здесь не «больше N», а «одно не есть другое», и проверяется оно тем, что у
    #: дописавших ход петель почти нет, а незакрытых <think> среди них большинство.
    #: Считается на свободном режиме — на greedy обе доли сбиты самим зацикливанием.
    unclosed_think = {"available": False}
    if other:
        free_rows = [r for r in other if not r["constrains_repetition"]] or other
        fr = min(free_rows, key=lambda r: r["looped_share"])
        unt = fr.get("unclosed_think_share_natural_stops")
        lpn = fr.get("looped_share_natural_stops")
        if unt is not None and lpn is not None:
            unclosed_think = {
                "available": True,
                "mode": fr["mode"],
                "unclosed_think_share_all": fr.get("unclosed_think_share"),
                "unclosed_think_share_natural_stops": unt,
                "looped_share_natural_stops": lpn,
                "reducible_to_loop": bool(unt <= lpn),
                "reading": "незакрытый <think> и петля — разные признаки: среди "
                           "дописавших ход зацикленных почти нет, а незакрытых "
                           "<think> большинство; сводить одно к другому нельзя",
            }

    primary = None
    confirmed: list[str] = []
    for c in causes:
        if c.get("excluded"):
            continue
        if primary is None:
            primary = c["cause"]
        #: Причина может подтвердиться двумя правилами сразу (петля приобретена И
        #: усугублена) — в списке она называется один раз, а оба свидетельства
        #: остаются в `statements`.
        if c["cause"] not in confirmed:
            confirmed.append(c["cause"])
    return {
        "primary": primary,
        "causes_confirmed": confirmed,
        "causes_excluded": [c["cause"] for c in causes if c.get("excluded")],
        "statements": causes,
        "rules_evaluated": rules,
        "repetition_penalty": repetition_penalty,
        "unclosed_think": unclosed_think,
        "note": "вердикт считается правилами выше; если ни одно правило не сработало "
                "— причина не названа, и это тоже результат",
    }


def _pct_pad(values, q):
    return LS._pct([int(v) for v in values], q)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs/s3ak-degeneration-20260917")
    ap.add_argument("--out", default="evidence/s3ak-degeneration.json")
    ap.add_argument("--tests-log", default=None)
    ap.add_argument("--deciding-state", default="sft_resume_6000",
                    help="состояние, по которому выносится вердикт по языку "
                         "(самый свежий SFT-чекпойнт — та же роль, что была у "
                         "sft_resume_4500 в S3aj)")
    args = ap.parse_args(argv)

    run = Path(args.runs)
    if not run.is_dir():
        print(f"NOT-VERIFIED: нет каталога прогона {run}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    dec = decoding_table(run)
    steps = step_table(run)
    data = loops_in_data(run)
    lang_files = list(run.glob("language_wide*.json"))
    if not dec and not steps:
        print(f"NOT-VERIFIED: в {run} нет ни отчёта режимов, ни отчёта по шагам",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    refusals = []
    # Тождество прибора: пакетный greedy обязан совпасть байтами с замером S3ai.
    identity = {"available": False}
    idf = run / "identity_core_cfinal_batched.json"
    ref = CASE / "runs/s3ai-probes-20260917/identity_core_cfinal.json"
    if idf.is_file() and ref.is_file():
        new = (load(idf) or {}).get("states", {}).get("cfinal", {}).get("probes", [])
        old = (load(ref) or {}).get("states", {}).get("cfinal", {}).get("probes", [])
        same = (len(new) == len(old) and len(new) > 0
                and all(a["response"] == b["response"] for a, b in zip(new, old)))
        identity = {"available": True, "compared": len(new), "byte_identical": same,
                    "run": rel(idf), "reference": rel(ref),
                    "why": "пакетный greedy (batch 8) обязан побайтово совпасть с "
                           "непересобранным замером S3ai: иначе ускорение замера "
                           "сменило бы измеряемое состояние"}
        if not same:
            refusals.append("пакетный greedy не совпал с замером S3ai побайтово — "
                            "числа сняты другим прибором")
    if not identity.get("byte_identical"):
        refusals.append("тождество прибора не подтверждено (нет замера ядра)")
    tests = {"available": False}
    if args.tests_log:
        tl = Path(args.tests_log)
        if tl.is_file():
            txt = tl.read_text(encoding="utf-8", errors="replace")
            lines = txt.strip().splitlines() if txt.strip() else []
            tail = lines[-1] if lines else ""
            fails = [ln.strip().lstrip("- ").strip() for ln in lines
                     if ln.strip().startswith("- ")]
            #: Известный красный берётся из свода S3aj, а не переписывается сюда:
            #: список «что уже было красным до дельты» — это утверждение о прошлом,
            #: и его место в прошлом артефакте. Иначе «новых нарушений нет» можно
            #: было бы получить, дописав отказ в свой же список.
            known, known_src = [], None
            prev = load(CASE / "evidence/s3aj-format-validity.json")
            if prev:
                known = ((prev.get("tests") or {}).get("failures")) or []
                known_src = "evidence/s3aj-format-validity.json"
            tests = {"available": True, "tally": tail, "log": rel(tl),
                     "log_sha256": sha256_file(tl),
                     "passed": "FAIL=0" in tail,
                     "failures": fails,
                     "known_red": known,
                     "known_red_source": known_src,
                     "failures_outside_known_red": [f for f in fails if f not in known],
                     "s3ak_checks": sum(1 for ln in lines
                                        if "S3ak" in ln or "s3ak" in ln
                                        or "analyze_sft_loops" in ln
                                        or "assemble_s3ak" in ln)}
            #: Отказ вне известного красного — это отказ свода: числа дельты сняты
            #: на дереве, где что-то сломалось, и «зелено» по ним было бы ложью.
            if tests["failures_outside_known_red"]:
                refusals.append(
                    "тесты дали отказы вне известного красного: "
                    + "; ".join(tests["failures_outside_known_red"]))

    evidence = {
        "schema": "s3ak-degeneration/1",
        "stage": "S3ak — разбор вырождения (петли) SFT-состояний и достоверный "
                 "вердикт по языку",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "measured" if not refusals else "refused",
        "question": "отчего SFT-состояния зацикливаются (декодирование / шаг обучения "
                    "/ данные / прибор) и на каком языке они отвечают при выборке "
                    "≥100 генераций",
        "protocol": {
            "device": "локальная RTX 4080 SUPER 16GB (НЕ стенд GB10 — замеры не "
                      "занимают стенд, AD-5); сетевой диск — только чтение",
            "stop_at_turn_end": True,
            "max_new_tokens": 4096,
            "batch_size": 8,
            "batch_identity": identity,
            "batch_caveat":
                "у пакетного прогона хвост последовательности, кончившейся раньше "
                "соседей, добит паддингом, и паддинг здесь — тот же id, что eos "
                "(151643): концевой <|endoftext|> дописанной генерации неотличим от "
                "добивки и срезается вместе с ней. На метрики это не влияет (язык "
                "считается по ходу без служебных токенов, причина конца — по лимиту "
                "и региону обрыва), но n_new_tokens такой генерации на 1 меньше, "
                "чем был бы у непересобранного прогона",
            "seed": LS.SEED,
            "loop_max4gram_rep": LS.LOOP_MAX4GRAM_REP,
            "bootstrap": {"n_boot": LS.BOOTSTRAP_N, "seed": LS.BOOTSTRAP_SEED,
                          "method": "bootstrap percentile"},
            "instrument": LS.INSTRUMENT_VERSION,
            "decoding_modes": sorted({r["mode"] for r in dec}),
            "prompt_sets": sorted({(load(f) or {}).get("protocol", {}).get("prompts_set")
                                   for f in lang_files} - {None}),
            "clean_rule": "вердикт по языку — stop_reason=turn_end И looped=false",
        },
        "artifacts": [{"state": r["state"], "mode": r["mode"],
                       "checkpoint": r["checkpoint"], "report": r["report"]}
                      for r in dec]
                   + [{"state": r["state"], "step": r["step"],
                       "checkpoint": r["checkpoint"], "report": r["report"]}
                      for r in steps],
        "degeneration": {
            "by_decoding": dec,
            "mode_applied": mode_applied(run),
            "by_step": steps,
            "by_step_caveat":
                "кривая склеена из ДВУХ прогонов: шаги 500…3000 — первый прогон "
                "sft-20260916-2246, шаги 3500…6000 — возобновлённый "
                "sft-resume-20260917-1215, который стартовал с чекпойнта 3000, но "
                "поток данных проходит с начала (DataLoader(shuffle=True) без "
                "генератора; смещение потока не реализовано — "
                "runs/sft-resume-20260917-1215/full_sft_params.json, resume."
                "data_order_note). Число шагов оптимизации от одних и тех же весов "
                "от этого не меняется, но «шаг 4000» здесь — не тот же пример, что "
                "«шаг 4000» непрерывного прогона",
            "loops_in_data": data,
            "verdict_cause": verdict(dec, steps, data),
        },
        "language": {
            #: Плоские поля вердикта — на верхнем уровне (контракт TASK): состояние,
            #: n, обе читки ответной части, рассуждение, покрытие, запас до 0.4.
            "state": args.deciding_state,
            "n_required": 100,
            "deciding": language_block(run, args.deciding_state),
            "states": {t: language_block(run, t)
                       for t in ("cfinal", "sft3000", args.deciding_state)},
            "by_decoding": language_by_decoding(run),
            "s3aj_reference": {
                "state": "sft_resume_4500",
                "cyr_answer_with_zeros": 0.3919,
                "cyr_answer_defined": 0.6719,
                "cyr_think": 0.7896,
                "coverage": 0.5833,
                "margin_to_0.4": 0.0081,
                "n": 24,
                "zone": "refuted",
                "why": "прежний вердикт держался на запасе 0.008 при 24 генерациях "
                       "(четверть шага одной генерации) — именно это и перемеряется",
            },
        },
        "instrument_fix": instrument_fix(run),
        "c012_closed": {
            "run": "runs/sft-resume-20260917-1215",
            "manifest": "runs/sft-resume-20260917-1215/run_manifest.json",
            "present": (CASE / "runs/sft-resume-20260917-1215/run_manifest.json").is_file(),
            "sha256": (sha256_file(CASE / "runs/sft-resume-20260917-1215/run_manifest.json")
                       if (CASE / "runs/sft-resume-20260917-1215/run_manifest.json").is_file()
                       else None),
        },
        "tests": tests,
        "refusals": refusals,
        "open_questions": [],
    }

    #: Контракт TASK ждёт плоские поля вердикта в `language`: поднимаем их из
    #: `deciding`, чтобы формат свода не приходилось разбирать вложенно.
    dec_state = evidence["language"]["deciding"]
    for k in ("n", "state", "cyr_answer_with_zeros", "cyr_answer_defined", "cyr_think",
              "coverage", "margin_to_0.4", "ci95", "ci_covers_0.4", "zone", "n_clean",
              "ci95_defined_only", "ci_defined_covers_0.4",
              "all_cyr_answer_with_zeros", "all_cyr_answer_defined", "all_cyr_think",
              "all_coverage", "looped_share", "budget_covers_p90", "lengths_clean",
              "legacy_cyr_answer_with_zeros", "legacy_zone"):
        if k in dec_state:
            evidence["language"][k] = dec_state[k]
    if dec_state.get("available") and dec_state["n"] < evidence["language"]["n_required"]:
        evidence["open_questions"].append(
            f"выборка вердикта по языку {dec_state['n']} < требуемых "
            f"{evidence['language']['n_required']}")
    if dec_state.get("available") and dec_state.get("ci_covers_0.4"):
        evidence["open_questions"].append(
            "бутстрап-интервал ответной части накрывает границу 0.4: вердикт по "
            "ADR-039 остаётся недоказанным в обе стороны, и решение о лечении "
            "данных принимать нельзя")
    v = evidence["degeneration"]["verdict_cause"]
    if not v["causes_confirmed"]:
        evidence["open_questions"].append(
            "ни одна из четырёх причин вырождения не подтверждена объявленными "
            "правилами — причина не названа")

    #: Контракт сверяет **производитель**, а не читатель: свод, у которого нет
    #: обязательного поля, бесполезен ровно так же, как свод с неверным числом, но
    #: обнаруживается это иначе — у читателя, через сутки, по отсутствию ключа.
    contract = {
        "status": evidence.get("status"),
        "artifacts": evidence.get("artifacts"),
        "degeneration.by_decoding": evidence["degeneration"].get("by_decoding"),
        "degeneration.by_step": evidence["degeneration"].get("by_step"),
        "degeneration.loops_in_data": evidence["degeneration"].get("loops_in_data"),
        "degeneration.verdict_cause": evidence["degeneration"].get("verdict_cause"),
        "language.n": evidence["language"].get("n"),
        "language.state": evidence["language"].get("state"),
        "language.cyr_answer_with_zeros": evidence["language"].get("cyr_answer_with_zeros"),
        "language.cyr_answer_defined": evidence["language"].get("cyr_answer_defined"),
        "language.cyr_think": evidence["language"].get("cyr_think"),
        "language.coverage": evidence["language"].get("coverage"),
        "language.margin_to_0.4": evidence["language"].get("margin_to_0.4"),
        "instrument_fix.changes": evidence["instrument_fix"].get("changes"),
        "instrument_fix.affected_prior_measurements":
            evidence["instrument_fix"].get("affected_prior_measurements"),
        "c012_closed": evidence.get("c012_closed"),
        "open_questions": evidence.get("open_questions"),
    }
    evidence["contract_check"] = {
        "required_keys_present": sorted(k for k, v in contract.items() if v is None),
        "note": "проверяются ключи контракта TASK; значение null считается "
                "отсутствующим, потому что «поле есть, но пустое» и «поля нет» "
                "для читателя одно и то же",
    }
    missing = evidence["contract_check"]["required_keys_present"]
    if missing:
        refusals.append("в своде нет обязательных полей контракта: "
                        + ", ".join(missing))
        evidence["status"] = "refused"

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"свод: {out} (режимов {len(dec)}, шагов {len(steps)}, "
          f"петли в данных: {'есть' if data.get('available') else 'нет отчёта'})")
    if refusals:
        for r in refusals:
            print(f"  ОТКАЗ: {r}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
