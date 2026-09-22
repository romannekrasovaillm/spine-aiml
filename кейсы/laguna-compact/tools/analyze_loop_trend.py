#!/usr/bin/env python3
"""analyze_loop_trend.py — тренд петель SFT-состояния по шагам обучения.

Вопрос. Петли SFT-состояния (S3aq: 17.31 % ходов при штатном режиме против 0.96 %
у CPT-финала) — воспроизведение выученного или собственная дегенерация — уже
разобран: источник не набор (S3ar: 6/409 находок против 29/751 у контроля, разрыв
-0.0239). Смена режима декодирования петлю не снимает, а усиливает. Длина целей в
наборе резать нечего (max 2274 токена). Остаётся вопрос, от которого зависит план
переделки стадии:

* дефект **откатывается** с обучением — тогда достаточно доучить/перезапустить и
  следить за кривой;
* дефект **стабилен или растёт** — тогда перезапуск с контролем длины сам по себе
  его не лечит, и правку надо вносить в данные или в режим обучения.

Что здесь меряется. По каждой доступной точке обучения берётся **тот же** прибор
(`probe_language_split.py`) на **том же** протоколе, что парный контроль S3aq
(набор `wide` 104 пробы, бюджет 4096, остановка на конце хода, пакет 8, штатный
запрет повторов 4-грамм), и из отчёта читаются метрики петель, усечений и формата.
Ни один параметр между точками не меняется: иначе тренд мерил бы разницу
протоколов, а не разницу шага.

Метрики петель по словам (блоки, различные тексты, ярусы mild/strong,
распределение по сегментам) берутся **не** здесь, а прибором происхождения
(`analyze_loop_origin.py`), и передаются сюда флагом ``--origin``: «петля» — это
диагноз, который в двух местах не должен считаться по-разному.

Правила тренда объявлены ДО замера (без них «тренд» доопределялся бы по месту,
то есть подгонялся бы под ответ):

1. **Единица наблюдения — проба, а не блок.** Блоки внутри одной генерации
   зависимы (одна петля даёт и `all`, и `think`), поэтому доля считается по 104
   пробам, а не по числу блоков. Числа блоков идут в таблицу как описательные и в
   тест значимости не входят.
2. **Тренд — наклон линейной вероятностной модели по пулу проб всех точек**, а не
   по средним точек: средние теряют n (8 точек вместо 832 наблюдений), и «тренд по
   средним» был бы слабее ровно настолько, насколько в нём меньше данных.
3. **Направление называется только при p < 0.05** (двусторонне). Иначе вердикт —
   «плато в пределах разрешения»: не «нет тренда», а «тренд меньше того, что этот
   замер способен различить». Границы при этом называются числом.
4. **Различимость выражается MDD** — минимальной разницей долей между двумя
   точками (по n = 104 на точку), которую замер ловит с мощностью 0.8 при
   α = 0.05 — и сравнивается с наблюдаемым изменением. «Визуально похоже на рост»
   вердиктом не является.
5. **Незакрытый `<think>` считается только среди естественно завершённых ходов**
   (ADR-045 п.5, там же поправка о знаменателе): у усечённого хода показателя нет,
   и подмешивание усечённых выдало бы дефект бюджета за дефект формата.

Коды возврата::

    0 — отчёт собран
    1 — отказ: вход не тот (шаг не читается из метки, нет общего набора проб)
    2 — NOT-VERIFIED: мерить нечего (меньше двух точек — тренда не бывает)

Запуск::

    python3 tools/analyze_loop_trend.py \\
        --report runs/sft-loop-trend-20260921/report_sft_v13_21500.json \\
        --report runs/sft-loop-trend-20260921/report_sft_v13_32600.json \\
        --origin runs/sft-loop-trend-20260921/loop_origin.json \\
        --ckpt-dir /home/user/gb10-shared/sft-20260919-0810/checkpoints \\
        --out evidence/sft-loop-trend.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from itertools import permutations
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOT_VERIFIED = 2

CASE = Path(__file__).resolve().parent.parent

#: Метки состояний прибора имеют вид ``sft_v13_21500`` — шаг стоит последним
#: числом метки. Разбор объявлен регуляркой, а не «последним токеном»: у метки
#: посторонние числа (``v13``) стоят раньше, и «последнее число» случайно
#: совпало бы с шагом только на этом наборе имён.
#: Порог — ТРИ цифры: шаги 500…999 в этом кейсе настоящие (прогон
#: sft-20260916-2246), и требование четырёх молча выбросило бы начало кривой.
#: Двузначное число в конце — уже не шаг, а версия (``v13``), и не берётся.
STEP_RE = re.compile(r"(\d{3,})\s*$")

#: Двусторонние квантили нормального распределения.
Z95 = 1.959963984540054     # α = 0.05
Z80 = 0.8416212335729143    # мощность 0.8

#: Метрики-доли, по которым считается тренд. Ключ — имя в артефакте, значение —
#: путь ВНУТРИ сводки ``states.<метка>.aggregate`` (сам ``aggregate`` в путь не
#: входит: он уже разыменован при чтении состояния). Лишний ведущий сегмент
#: ``aggregate`` делает путь неразрешимым — все доли выходят ``null``, а
#: производные счётчики молча нулями; проверено на снятых отчётах.
SHARE_METRICS = {
    "instrument_looped_share": ("looped_share",),
    "truncated_share": ("truncated_share",),
    "natural_stop_share": ("stop", "natural_stop_share"),
    "unclosed_think_natural": ("stop", "natural", "unclosed_think_share"),
}

#: Пороговые метрики (не доли): тренд по средним значениям точки, единица — сама
#: величина. Названы отдельно, потому что MDD для доли к ним не применим.
VALUE_METRICS = {
    "median_tokens": ("lengths", "median"),
    "mean_tokens": ("lengths", "mean"),
}


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── арифметика ───────────────────────────────────────────────────────────────

def _norm_sf(z: float) -> float:
    """P(Z > z) для стандартной нормали — через ``erf``, без внешних библиотек."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def two_sided_p(z: float) -> float:
    return 2.0 * _norm_sf(abs(z))


def wilson(k: int, n: int, z: float = Z95) -> dict:
    """Интервал Уилсона для доли.

    Уилсон, а не нормальное приближение: при долях у нуля (у CPT-финала петли
    0.96 %) нормальный интервал вылезает за [0, 1] и врёт о точности.
    """
    if n <= 0:
        return {"lo": None, "hi": None, "n": 0}
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return {"lo": max(0.0, center - half), "hi": min(1.0, center + half),
            "mean": p, "n": n, "k": k, "method": "wilson", "z": z}


def two_prop_test(k1: int, n1: int, k2: int, n2: int) -> dict:
    """Двухдолевой z-тест с объединённой дисперсией: первая точка против последней."""
    if n1 <= 0 or n2 <= 0:
        return {"p": None}
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return {"p": 1.0 if p1 == p2 else 0.0, "delta": p2 - p1, "z": 0.0}
    z = (p2 - p1) / se
    return {"delta": p2 - p1, "z": z, "p": two_sided_p(z),
            "p1": p1, "p2": p2, "n1": n1, "n2": n2}


def mdd(p: float, n: int, z_alpha: float = Z95, z_beta: float = Z80) -> float:
    """Минимальная различимая разница долей между ДВУМЯ точками по n проб каждая.

    Мощность 0.8, α = 0.05, объединённая дисперсия. Это ответ на вопрос «а при
    таком n изменение вообще различимо?» — и он должен стоять рядом с трендом,
    иначе «выросло с 0.44 до 0.47» читалось бы как рост, хотя это шум.
    """
    if n <= 0:
        return float("nan")
    return (z_alpha + z_beta) * math.sqrt(2.0 * p * (1.0 - p) / n)


def ols_slope(xs: list[float], ys: list[float]) -> dict:
    """Наклон МНК с 95 % интервалом. Квантиль нормальная: n здесь — сотни проб.

    Возвращает наклон **на 1000 шагов** — в шагах наклона доля меняется на
    величину порядка 1e-5, и читать её неудобно.
    """
    n = len(xs)
    if n < 3:
        return {"slope": None, "n": n}
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return {"slope": None, "n": n}
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    s2 = sum(r * r for r in resid) / (n - 2)
    se = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
    z = slope / se if se > 0 else (0.0 if slope == 0 else math.inf)
    return {"slope": slope, "slope_per_1000": slope * 1000.0,
            "se": se, "z": z, "p": two_sided_p(z) if se > 0 else (1.0 if slope == 0 else 0.0),
            "lo": slope - Z95 * se, "hi": slope + Z95 * se,
            "lo_per_1000": (slope - Z95 * se) * 1000.0,
            "hi_per_1000": (slope + Z95 * se) * 1000.0,
            "n": n, "x_unit": "шаг обучения"}


def _rank(vals: list[float]) -> list[float]:
    """Ранги со средними при связках (нужно для Спирмена по точкам)."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman_exact(xs: list[float], ys: list[float]) -> dict:
    """Спирмен по точкам с ТОЧНЫМ перестановочным p (перебор всех перестановок).

    Точно, а не по таблице: точек 6–8, перестановок ≤ 40320 — перебор дешевле
    любой аппроксимации и не требует допущения о нормальности при n = 8.
    """
    n = len(xs)
    if n < 3:
        return {"rho": None, "n": n}
    rx, ry = _rank(xs), _rank(ys)

    def pearson(a: list[float], b: list[float]) -> float:
        ma, mb = sum(a) / n, sum(b) / n
        sa = math.sqrt(sum((v - ma) ** 2 for v in a))
        sb = math.sqrt(sum((v - mb) ** 2 for v in b))
        if sa == 0 or sb == 0:
            return 0.0
        return sum((u - ma) * (v - mb) for u, v in zip(a, b)) / (sa * sb)

    rho = pearson(rx, ry)
    if n > 9:                       # перебор не влезает — отказ, а не «примерно»
        return {"rho": rho, "n": n, "p": None,
                "note": "точный перестановочный p не считается при n > 9"}
    cnt = 0
    total = 0
    for permuted in permutations(range(n)):
        total += 1
        if abs(pearson(rx, [ry[i] for i in permuted])) >= abs(rho) - 1e-12:
            cnt += 1
    return {"rho": rho, "n": n, "p": cnt / total if total else None,
            "method": "exact permutation", "permutations": total}


def get_path(d: dict, path: tuple):
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


# ── чтение входов ────────────────────────────────────────────────────────────

def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        note(f"не читается {path}: {type(e).__name__}: {e}")
        return None


def read_input(spec: str):
    """Вход — файл или ``git:<ref>:<путь>`` (отчёты живут в ветвях, не в дереве)."""
    if spec.startswith("git:"):
        rest = spec[4:]
        ref, _, rel = rest.partition(":")
        try:
            out = subprocess.run(["git", "show", f"{ref}:{rel}"],
                                 capture_output=True, cwd=str(CASE), check=True)
        except subprocess.CalledProcessError as e:
            note(f"git show не удался для {spec}: {e.stderr.decode()[:200]}")
            return None
        try:
            return json.loads(out.stdout.decode("utf-8"))
        except Exception as e:                               # noqa: BLE001
            note(f"не разобран JSON из {spec}: {e}")
            return None
    p = Path(spec)
    if not p.is_file():
        note(f"нет файла {spec}")
        return None
    return read_json(p)


def step_of(tag: str):
    m = STEP_RE.search(tag)
    return int(m.group(1)) if m else None


def inventory(ckpt_dirs: list[str], lo: int, hi: int) -> dict:
    """Что ФАКТИЧЕСКИ лежит на диске в диапазоне — и чего там нет.

    Ретенция снимает старые точки молча, и «тренд с шага 500» тогда опирался бы
    на файлы, которых нет. Перепись идёт по диску, а не по памяти о прогоне.
    """
    present_probe, present_ckpt, dirs = [], [], []
    unnamed: list[dict] = []
    for d in ckpt_dirs:
        p = Path(d).expanduser()
        entry = {"dir": str(p), "exists": p.is_dir()}
        if not p.is_dir():
            dirs.append(entry)
            continue
        names = sorted(x.name for x in p.iterdir() if x.is_file())
        entry["files"] = names
        dirs.append(entry)
        for name in names:
            m = re.search(r"(?:probe|checkpoint)[_](\d+)\.pt$", name)
            if not m:
                #: Файл без шага в имени — не «мусор»: в каталоге прогона так
                #: лежит ВХОД стадии (`checkpoint_final.pt` = копия финала CPT,
                #: `loaded_as` из full_sft_params.json), то есть состояние шага 0,
                #: а не точка этого прогона. Назвать его обязательно: без этого
                #: «финал» читался бы как конец SFT-траектории, хотя это её начало.
                unnamed.append({"file": name, "dir": str(p),
                                "bytes": (p / name).stat().st_size})
                continue
            step = int(m.group(1))
            if not (lo <= step <= hi):
                continue
            (present_probe if "probe" in name else present_ckpt).append(step)
    present = sorted(set(present_probe) | set(present_ckpt))
    grid_probe = list(range(lo, hi + 1, 500))
    grid_ckpt = list(range(lo, hi + 1, 200))
    return {
        "dirs": dirs,
        "range": [lo, hi],
        "probe_points_present": sorted(set(present_probe)),
        "checkpoint_points_present": sorted(set(present_ckpt)),
        "probe_grid_missing": [s for s in grid_probe if s not in set(present_probe)],
        "checkpoint_grid_missing": [s for s in grid_ckpt if s not in set(present_ckpt)],
        "files_without_step_in_name": unnamed,
        "coverage": _coverage(sorted(set(present_probe)), ckpt_dirs),
        "note": ("шаг 200 — ретенция стадии (checkpoint_*), шаг 500 — точки проб "
                 "(probe_*); обе сетки переписаны по диску, а не по объявлению прогона. "
                 "Файлы без шага в имени названы отдельно: в каталоге прогона так лежит "
                 "ВХОД стадии, и принять его за точку прогона значило бы измерить "
                 "начальное состояние под именем конечного"),
    }


#: Доли, рост которых означает ХУЖЕ (дефект), а падение — лучше. Вынесено
#: константой, потому что от неё зависит знак вывода: без неё «decline» читался бы
#: как «дефект растёт» на метрике, где рост — это хорошо.
DEFECT_METRICS = ("instrument_looped_share", "truncated_share", "unclosed_think_natural")

def _early_coverage_limit(inv: dict | None, points: list[dict]) -> str:
    """Граница «ранняя часть не покрыта» — с числами, а не «где-то ниже».

    Без переписи диска остаётся общая формулировка: назвать потерю нечем, и
    подставлять сюда правдоподобное число было бы хуже, чем сказать «не считано».
    """
    head = ("НЕ покрывает раннюю часть прогона: "
            "чекпойнты ниже первой уцелевшей точки сняты ретенцией стадии")
    cov = ((inv or {}).get("coverage") or {})
    if not cov.get("available"):
        return head + " (перепись диска не снята — какие именно, не названо)"
    lost = cov.get("probe_points_below_first") or {}
    parts = [head]
    if lost.get("count"):
        parts.append(f"потеряно точек проб: {lost['count']} "
                     f"(шаги {lost['from']}…{lost['to']} с шагом {lost['grid_step']})")
    if cov.get("first_surviving_probe_step"):
        parts.append(f"самая ранняя уцелевшая — {cov['first_surviving_probe_step']}")
    win = cov.get("surviving_probe_window") or {}
    if cov.get("first_surviving_probe_step"):
        parts.append(f"уцелевшее окно {win.get('first')}…{win.get('last')}")
    if cov.get("declared_max_steps") and win.get("first_share_of_plan") is not None:
        parts.append(
            f"это {win['first_share_of_plan']:.1%}…{win['last_share_of_plan']:.1%} "
            f"объявленного расписания ({cov['declared_max_steps']} шагов)")
    return "; ".join(parts)


def _answer(trends: dict, points: list[dict], inv: dict | None) -> dict:
    """Ответ на вопрос замера и что он означает для выбора плана.

    Формулировка выводится из вердиктов, а не пишется рядом с ними: у каждой
    ветки назван довод, который она СНИМАЕТ или ДАЁТ. Если данных не хватает,
    ветка обязана сказать это, а не выбрать ближайший удобный вывод.
    """
    verdicts = {k: (t.get("verdict") if t.get("available") else None)
                for k, t in trends.items()}
    defect = {k: verdicts.get(k) for k in DEFECT_METRICS}
    known = {k: v for k, v in defect.items() if v}

    if not known:
        shape, shape_ru = "no_data", "недостаточно данных"
    elif all(v == "decline" for v in known.values()):
        shape, shape_ru = "falls", "убывает"
    elif all(v == "growth" for v in known.values()):
        shape, shape_ru = "grows", "растёт"
    #: Порядок веток важен: чистые исходы проверяются ДО смешанных. Обратный
    #: порядок делал «плато» недостижимым — все-плато попадало в «плато или
    #: убывает», то есть замер докладывал о возможном откате там, где его нет
    #: (поймано фикстурой, а не глазами).
    elif all(v == "plateau_within_resolution" for v in known.values()):
        shape, shape_ru = "plateau", "плато в пределах разрешения"
    elif all(v in ("plateau_within_resolution", "decline") for v in known.values()):
        shape, shape_ru = "flat_or_falls", "плато или убывает"
    elif all(v in ("plateau_within_resolution", "growth") for v in known.values()):
        shape, shape_ru = "flat_or_grows", "плато или растёт"
    else:
        shape, shape_ru = "unresolved", "не различимо при этом n"

    mdd_note = ("MDD печатается рядом с каждой долей и не зависит от вердикта: "
                "«не различимо» — это утверждение о разрешении замера, а не о том, "
                "что дефекта нет")
    if shape == "falls":
        implication = (
            "Довод за «доучивать остановленный прогон»: дефект убывает с шагом, и "
            "продолжение обучения с 32600 движет его в нужную сторону. Это НЕ значит, "
            "что доучивание снимет дефект — только что оно не противоречит данным.")
    elif shape == "grows":
        implication = (
            "Довод за «переделывать режим обучения»: дефект растёт с шагом, поэтому "
            "продолжение того же режима с 32600 увеличит его. Контроль длины при этом "
            "остаётся непроверенным — он лечит усечения, а не петли, и этого замера "
            "не заменяет.")
    elif shape == "plateau":
        implication = (
            "Ни один из двух планов этим замером не подтверждается. Снят довод, на "
            "котором держалось «доучивать»: возврата к норме по ходу обучения нет — "
            "доля петель на 32600 та же, что на 21500 (полные числа в by_point), а не "
            "«ещё немного — и рассосётся». Точно так же не подтверждено и «копится»: "
            "направления нет. Выбор между «доучивать с 32600» и «переделывать режим» "
            "опирается на то, чего в этом замере НЕТ: на сравнение РЕЖИМОВ (контроль "
            "длины, иной декодировщик, иные данные), а не на положение внутри одного "
            "режима. Замер отвечает только за одно: продолжение того же режима не "
            "обещает самоустранения дефекта.")
    else:
        implication = (
            f"Вывод о выборе не делается: исход — «{shape_ru}». Названные ветки "
            "опираются на направление, а его здесь нет.")

    out = {
        "defect_direction": shape,
        "defect_direction_ru": shape_ru,
        "direction_convention": ("растут петли, усечения и незакрытые блоки — ХУЖЕ; "
                                 "поэтому decline доли = дефект откатывается"),
        "by_metric": {k: {"verdict": v,
                          "pooled_share": trends[k].get("pooled_share"),
                          "mdd": (trends[k].get("mdd_two_points_n104") or {}).get("value"),
                          "change_over_range": (trends[k].get("change_over_range") or {})
                          .get("point_estimate")}
                      for k, v in defect.items()},
        "distinguishable_at_n104": any(
            trends[k].get("span_exceeds_mdd") for k in DEFECT_METRICS if trends.get(k)),
        "mdd_note": mdd_note,
        "implication_for_choice": implication,
        "what_this_does_not_answer": [
            "Сравнение РЕЖИМОВ обучения: замер стоит внутри одного режима, и «поможет "
            "ли контроль длины рассуждений» из него не следует",
            "Доучивание как таковое: продолжение с 32600 не измерено — измерено "
            "состояние НА 32600, а не то, что будет после следующих шагов",
            "Возврат дефекта позже: прогон остановлен, за 32600 данных нет",
        ],
    }
    if inv and inv.get("coverage", {}).get("available"):
        c = inv["coverage"]
        out["coverage_of_run"] = {
            "measured_window": [min(p["step"] for p in points),
                                max(p["step"] for p in points)],
            "first_surviving_probe_step": c.get("first_surviving_probe_step"),
            "probe_points_lost_below": (c.get("probe_points_below_first") or {}).get("count"),
            "declared_max_steps": c.get("declared_max_steps"),
            "surviving_probe_window_share_of_plan": c.get("surviving_probe_window"),
            "reading": ("тренд снят на уцелевшем окне точек; всё, что было ниже "
                        "первой уцелевшей, ретенция удалила до замера — ранняя часть "
                        "обучения не покрыта и восстановлению из артефактов не подлежит"),
        }
    return out


def _coverage(probe_steps: list[int], ckpt_dirs: list[str]) -> dict:
    """Что осталось ЗА нижней границей: ранняя часть прогона, снятая ретенцией.

    Считается от сетки самих точек (шаг между соседними), а не от константы:
    константа ретенции живёт в пайплайне стадии, и брать её отсюда значило бы
    утверждать чужое число. Шаг сетки восстанавливается по диску, поэтому
    «сколько точек ниже первой» — арифметика, а не воспоминание.

    Объявленное расписание (``max_steps``) берётся из манифеста стадии, если он
    лежит рядом с чекпойнтами или назван в `--ckpt-dir`: он даёт знаменатель для
    доли покрытия — без него «покрыта пятая часть» было бы сравнением с ничем.
    """
    out: dict = {"n_probe_points_on_disk": len(probe_steps)}
    if not probe_steps:
        return {**out, "available": False, "why": "точек проб на диске нет"}
    first = min(probe_steps)
    diffs = [b - a for a, b in zip(probe_steps, probe_steps[1:])]
    grid = min(diffs) if diffs else None
    out["first_surviving_probe_step"] = first
    out["probe_grid_step"] = grid

    #: Ниже первой уцелевшей точки сетка шла тем же шагом от нуля: считаем, сколько
    #: её узлов не дожило. Точка 0 не считается — это вход стадии, а не её шаг.
    if grid:
        lost = [s for s in range(grid, first, grid)]
        out["probe_points_below_first"] = {
            "count": len(lost),
            "from": lost[0] if lost else None,
            "to": lost[-1] if lost else None,
            "grid_step": grid,
            "reason": ("сняты ретенцией стадии: на диске ровно столько точек проб, "
                       "сколько держит её политика (совпадение с константой "
                       "проверяется глазами по пайплайну — она не читается отсюда)"),
        }

    #: Названо «уцелевшее окно НА ДИСКЕ», а не «измеренное»: сетка точек на диске
    #: шире набора снятых проб (точек 24, замер сделан по 8), и одно имя для двух
    #: разных множеств читалось бы как одно и то же. Окно — факт диска и есть
    #: всегда; доля расписания появляется только вместе с его знаменателем, иначе
    #: поле то исчезало бы, то появлялось от наличия манифеста рядом с чекпойнтами.
    out["surviving_probe_window"] = {
        "first": min(probe_steps), "last": max(probe_steps),
        "note": ("окно уцелевших точек проб на диске; сетка шире снятого набора, "
                 "поэтому это НЕ то же самое, что измеренное окно тренда"),
    }

    man = None
    for d in ckpt_dirs:
        p = Path(d).expanduser()
        for cand in (p / "run_manifest.json", p.parent / "run_manifest.json"):
            if cand.is_file():
                man = cand
                break
        if man:
            break
    if man:
        try:
            decl = json.loads(man.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001
            decl = None
            note(f"манифест стадии {man} не прочитан: {exc}")
        if isinstance(decl, dict) and decl.get("max_steps"):
            total = int(decl["max_steps"])
            out["declared_max_steps"] = total
            out["declared_max_steps_source"] = str(man)
            out["surviving_probe_window"]["first_share_of_plan"] = min(probe_steps) / total
            out["surviving_probe_window"]["last_share_of_plan"] = max(probe_steps) / total
    else:
        note("манифест стадии не найден — доля покрытия расписания не считается "
             "(знаменателя нет, и подставлять вместо него число точек было бы подлогом)")
    out["available"] = True
    return out


def collect_points(reports: list[tuple[str, dict]], origin: dict | None) -> list[dict]:
    """Свести по каждой точке: доли по пробам + описательные числа блоков."""
    origin_by_state: dict[str, dict] = {}
    if origin:
        for rep, states in ((origin.get("blocks") or {}).get("by_state") or {}).items():
            for state, val in states.items():
                origin_by_state[state] = val
        for entry in ((origin.get("blocks") or {}).get("distinct_list") or []):
            st = entry.get("state")
            if st:
                origin_by_state.setdefault(st, {}).setdefault("_distinct", []).append(entry)
        #: Блоки по сегментам — из `distinct_list` (там у каждого блока назван и
        #: отчёт, и состояние): сводные `by_region` в приборе происхождения
        #: сквозные по всем состояниям, и приписать их одной точке нельзя.
        for st, val in origin_by_state.items():
            regions: dict[str, int] = {}
            for e in val.get("_distinct", []):
                for r in (e.get("regions") or [e.get("region")]):
                    if r:
                        regions[r] = regions.get(r, 0) + 1
            val["regions"] = regions

    points, excluded = [], []
    for name, rep in reports:
        for tag, st in (rep.get("states") or {}).items():
            step = step_of(tag)
            if step is None:
                #: Состояние без шага в метке — это не точка тренда (у S3aq рядом
                #: со `sft_v13_21500` стоит `cfinal` — финал CPT, не шаг SFT).
                #: Исключается ЯВНО и попадает в артефакт: молчаливый пропуск
                #: неотличим от «такой точки и не было».
                excluded.append({"tag": tag, "report": name,
                                 "why": "в метке нет шага обучения — не точка тренда",
                                 "n_probes": len(st.get("probes") or [])})
                note(f"исключено состояние {tag!r}: в метке нет шага обучения")
                continue
            probes = st.get("probes") or []
            agg = st.get("aggregate") or {}
            if not probes:
                note(f"точка {tag}: проб нет — пропущена")
                continue
            rec = {
                "tag": tag, "step": step, "report": name,
                "checkpoint": st.get("checkpoint"),
                "checkpoint_sha256": st.get("checkpoint_sha256"),
                "n_probes": len(probes),
                "metrics": {}, "vectors": {}, "blocks": None,
            }
            for key, path in SHARE_METRICS.items():
                val = get_path(agg, path)
                rec["metrics"][key] = val
                vec = metric_vector(probes, key)
                rec["vectors"][key] = vec
                #: Описательная доля и вектор считаются из ОДНИХ проб, поэтому
                #: их расхождение — не «шум», а признак, что путь в сводку
                #: разошёлся с прибором. Молчаливый `null` тут уже один раз
                #: обнулил производные счётчики — теперь он говорит вслух.
                #: Допуск между округлением сводки и вектором. Прибор пишет долю
                #: с 4 знаками (шум округления ≤ 5e-5), а одна проба из 104 — это
                #: 0.0096: порог 1e-3 лежит между ними, поэтому округление не
                #: поднимает ложную тревогу, а расхождение на пробу — поднимает.
                if val is None:
                    note(f"точка {tag}: метрика {key!r} не найдена в сводке отчёта — "
                         f"в артефакте null (путь {'.'.join(path)})")
                elif vec and abs(val - sum(vec) / len(vec)) > 1e-3:
                    note(f"точка {tag}: {key!r} сводки ({val}) расходится с пробами "
                         f"({sum(vec)}/{len(vec)}) — читать по пробам")
            for key, path in VALUE_METRICS.items():
                val = get_path(agg, path)
                rec["metrics"][key] = val
                if val is None:
                    note(f"точка {tag}: метрика {key!r} не найдена в сводке отчёта — "
                         f"в артефакте null (путь {'.'.join(path)})")

            #: Знаменатель п.5 ADR-045: у естественно завершённых ходов. Берётся
            #: из отчёта прибора (`stop.natural`), а не пересчитывается здесь —
            #: «естественно завершён» определено прибором (stop_reason=turn_end).
            rec["natural_n"] = get_path(agg, ("stop", "natural", "n"))
            rec["truncated_n"] = get_path(agg, ("stop", "truncated", "n"))
            #: Счётчики выводятся из ВЕКТОРА (пробы — источник решения), а не из
            #: описательной доли: так опечатка в пути не превращается в honest-
            #: выглядящий ноль. Знаменатель незакрытых — естественно завершённые.
            rec["unclosed_natural_k"] = sum(rec["vectors"]["unclosed_think_natural"])
            rec["truncated_k"] = sum(rec["vectors"]["truncated_share"])
            rec["looped_k"] = sum(rec["vectors"]["instrument_looped_share"])
            rec["unclosed_any_k"] = sum(1 for p in probes
                                        if (p.get("metrics") or {}).get("unclosed_think"))
            rec["unclosed_any_share"] = rec["unclosed_any_k"] / len(probes)

            blk = origin_by_state.get(tag)
            if blk:
                rec["blocks"] = {
                    "total_mild": blk.get("blocks"),
                    "total_strong": blk.get("blocks_strong"),
                    "distinct_mild": blk.get("distinct_blocks"),
                    "n_probes": blk.get("n_probes"),
                    "control_units": blk.get("control_units"),
                    "instrument_loop_share": blk.get("instrument_loop_share"),
                    "truncated_share": blk.get("truncated_share"),
                    "median_tokens": blk.get("median_tokens"),
                    "by_region": blk.get("regions"),
                }
            points.append(rec)
    points.sort(key=lambda r: r["step"])
    return points, excluded


def _decoding_field(reports: list[tuple[str, dict]], field: str):
    """Поле режима декодирования из отчётов — плоский ключ, иначе вложенный.

    Прибор называет запрет 4-грамм в ``protocol.decoding_protocol`` (там же
    ``standard`` и ``allowed_for_conclusions``). Возвращается только значение,
    общее для ВСЕХ точек: если отчёты разошлись по режиму, это не «одно число»,
    и подставлять первое попавшееся значило бы выдать разницу режимов за факт.
    """
    vals = set()
    for _, rep in reports:
        proto = rep.get("protocol") or {}
        val = proto.get(field)
        if val is None:
            val = (proto.get("decoding_protocol") or {}).get(field)
        vals.add(json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list))
                 else val)
    if len(vals) != 1:
        note(f"ВНИМАНИЕ: точки расходятся по protocol.{field}: {sorted(map(str, vals))}")
        return None
    return vals.pop()


def metric_vector(probes: list[dict], key: str) -> list[int]:
    """Вектор 0/1 по пробам — то, на чём считается тренд (правило 1).

    Все четыре доли выводятся из одних и тех же 104 проб и не пересекаются с
    блоками: блоки одной генерации зависимы, пробы — нет.
    """
    out = []
    for p in probes:
        m = p.get("metrics") or {}
        if key == "instrument_looped_share":
            out.append(1 if m.get("looped") else 0)
        elif key == "truncated_share":
            out.append(1 if p.get("hit_limit") else 0)
        elif key == "natural_stop_share":
            out.append(1 if p.get("stop_reason") == "turn_end" else 0)
        elif key == "unclosed_think_natural":
            #: Знаменатель — естественно завершённые (ADR-045 п.5): у усечённого
            #: хода показателя нет, поэтому проба в вектор не входит вовсе.
            if p.get("stop_reason") == "turn_end":
                out.append(1 if m.get("unclosed_think") else 0)
        else:
            raise KeyError(f"нет вектора для метрики {key!r}")
    return out


# ── тренд ────────────────────────────────────────────────────────────────────

def trend_for(points: list[dict], key: str) -> dict:
    """Тренд доли по пулу проб (правило 2) + Спирмен по точкам + MDD."""
    xs, ys, per_point = [], [], []
    for rec in points:
        vec = rec["vectors"].get(key) or []
        if not vec:
            continue
        per_point.append({
            "step": rec["step"], "k": sum(vec), "n": len(vec),
            "share": sum(vec) / len(vec),
            "ci95": wilson(sum(vec), len(vec)),
        })
        xs.extend([float(rec["step"])] * len(vec))
        ys.extend([float(v) for v in vec])
    if not per_point:
        return {"available": False,
                "why": ("нет проб с определённым показателем (для незакрытых <think> "
                        "это возможно, если все ходы усечены — ADR-045 п.5)")}

    pooled = sum(p["k"] for p in per_point) / sum(p["n"] for p in per_point)
    fit = ols_slope(xs, ys)
    span = per_point[-1]["step"] - per_point[0]["step"]
    first, last = per_point[0], per_point[-1]

    #: Изменение за весь диапазон и его интервал — в ПОЛНЫХ долях (не на 1000
    #: шагов): именно оно сравнивается с MDD при ответе «различимо ли».
    total = fit["slope"] * span if fit.get("slope") is not None else None
    total_lo = fit["lo"] * span if fit.get("lo") is not None else None
    total_hi = fit["hi"] * span if fit.get("hi") is not None else None
    detect = mdd(pooled, min(p["n"] for p in per_point))
    pairwise = two_prop_test(first["k"], first["n"], last["k"], last["n"])

    #: Вердикт — три исхода, а не два. «Наклон незначим» — это ещё не «плато»:
    #: если интервал изменения ШИРЕ того, что замер способен различить, честное
    #: слово — «не различимо», а не «плато», иначе нехватка данных читалась бы
    #: как утверждение об отсутствии тренда.
    if fit.get("slope") is None:
        verdict, verdict_ru, why = ("indistinguishable", "не различимо",
                                    "наклон не считается: мало точек")
    elif fit["p"] is not None and fit["p"] < 0.05:
        verdict = "growth" if fit["slope"] > 0 else "decline"
        verdict_ru = "растут" if fit["slope"] > 0 else "падают"
        why = (f"наклон {fit['slope_per_1000']:+.4f} доли на 1000 шагов, "
               f"p = {fit['p']:.2e} < 0.05")
    elif total_lo is not None and total_hi is not None and max(abs(total_lo),
                                                              abs(total_hi)) <= detect:
        verdict, verdict_ru = "plateau_within_resolution", "плато"
        why = (f"наклон неотличим от нуля (p = {fit['p']:.3f} ≥ 0.05), и весь 95 % "
               f"интервал изменения за диапазон [{total_lo:+.4f}; {total_hi:+.4f}] "
               f"укладывается в MDD {detect:.4f} — тренд, если он есть, меньше того, "
               "что этот замер различает")
    else:
        verdict, verdict_ru = "indistinguishable_underpowered", "не различимо"
        why = (f"наклон неотличим от нуля (p = {fit['p']:.3f} ≥ 0.05), НО 95 % интервал "
               f"изменения за диапазон [{total_lo:+.4f}; {total_hi:+.4f}] шире MDD "
               f"{detect:.4f}: тренд такого размера замер бы не поймал — это нехватка "
               "разрешения, а не доказанное отсутствие тренда")

    span_detectable = (total is not None and abs(total) > detect)
    return {
        "available": True,
        "unit": "проба (0/1), пул всех точек",
        "n_observations": len(xs),
        "pooled_share": pooled,
        "by_point": per_point,
        "ols": fit,
        "spearman": spearman_exact([p["step"] for p in per_point],
                                   [p["share"] for p in per_point]),
        "first_vs_last": {**pairwise, "step_first": first["step"], "step_last": last["step"]},
        "change_over_range": {
            "point_estimate": total, "lo95": total_lo, "hi95": total_hi,
            "step_span": span,
            "observed_first_to_last": last["share"] - first["share"],
        },
        "mdd_two_points_n104": {
            "value": detect,
            "at_pooled_share": pooled,
            "n_per_point": min(p["n"] for p in per_point),
            "power": 0.8, "alpha": 0.05,
            "worst_case_at_p_half": mdd(0.5, min(p["n"] for p in per_point)),
            "reading": ("минимальная разница долей между двумя точками по "
                        f"{min(p['n'] for p in per_point)} проб, различимая с "
                        "мощностью 0.8 при α = 0.05"),
        },
        "verdict": verdict,
        "verdict_ru": verdict_ru,
        "why": why,
        "span_exceeds_mdd": span_detectable,
        "reading": (
            "изменение за диапазон "
            + (f"{total:+.4f} доли " if total is not None else "— ")
            + (f"({'больше' if span_detectable else 'меньше'} MDD {detect:.4f})"
               if total is not None else "")
        ),
    }


def build(args) -> tuple[dict, int]:
    reports = []
    for spec in args.report:
        rep = read_input(spec)
        if rep is None:
            return {}, EXIT_NOT_VERIFIED
        reports.append((spec, rep))
    #: Число точек проверяется ПОСЛЕ разбора состояний, а не по числу файлов:
    #: прибор кладёт все состояния в один отчёт (`--out`), и «два файла» — не то
    #: же самое, что «две точки обучения».
    origin = read_input(args.origin) if args.origin else None
    if args.origin and origin is None:
        note("ВНИМАНИЕ: --origin не прочитан — блоки по словам в артефакт не попадут "
             "(доли по пробам считаются из отчётов проб и не зависят от него)")

    points, excluded = collect_points(reports, origin)
    if len(points) < 2:
        note("NOT-VERIFIED: после разбора осталось меньше двух точек")
        return {}, EXIT_NOT_VERIFIED

    #: Единый набор проб — условие сопоставимости: если точки сняты на разных
    #: наборах промптов, «тренд» мерил бы разницу наборов.
    digests = {(rep.get("protocol") or {}).get("prompts_digest") for _, rep in reports}
    sets = {(rep.get("protocol") or {}).get("prompts_set") for _, rep in reports}
    budgets = {(rep.get("protocol") or {}).get("max_new_tokens") for _, rep in reports}
    if len(sets) > 1 or len(digests) > 1:
        note(f"ОТКАЗ: точки сняты на разных наборах промптов {sorted(map(str, sets))} — "
             "тренд мерил бы разницу наборов, а не обучения")
        return {}, EXIT_FAIL

    trends = {}
    for key in SHARE_METRICS:
        trends[key] = trend_for(points, key)

    steps = [p["step"] for p in points]
    values = {}
    for key in VALUE_METRICS:
        ys = [p["metrics"].get(key) for p in points]
        if any(v is None for v in ys):
            values[key] = {"available": False, "why": "в части отчётов метрики нет"}
            continue
        fit = ols_slope([float(s) for s in steps], [float(v) for v in ys])
        values[key] = {"available": True, "by_point": [
            {"step": s, "value": v} for s, v in zip(steps, ys)], "ols": fit,
            "unit": "токены", "note": "тренд по средним точки; MDD для доли неприменим"}

    #: Перепись диска считается один раз: из неё же берётся ответ про покрытие,
    #: и второй обход каталога чекпойнтов дал бы два места, где он читается.
    inventory_block = inventory(args.ckpt_dir, args.range_lo, args.range_hi) \
        if args.ckpt_dir else None
    answer = _answer(trends, points, inventory_block)

    data = {
        "schema": "sft-loop-trend/1",
        "tool": "tools/analyze_loop_trend.py",
        "question": ("дефект петель SFT-состояния откатывается с обучением, стоит на "
                     "плато или растёт — и различимо ли изменение при n = 104 на точку"),
        "protocol": {
            "instrument": "tools/probe_language_split.py (тот же, что S3aq)",
            "prompts_set": sorted(map(str, sets)),
            "prompts_digest": sorted(map(str, digests)),
            "max_new_tokens": sorted(map(str, budgets)),
            "stop_at_turn_end": True,
            "batch_size": (reports[0][1].get("protocol") or {}).get("batch_size"),
            "decoding": (reports[0][1].get("protocol") or {}).get("decoding"),
            #: Прибор кладёт запрет 4-грамм во вложенный `decoding_protocol`
            #: (`no_repeat_ngram` + `standard`), а плоского ключа в протоколе нет.
            #: Чтение только плоского давало `null` — то есть артефакт утверждал
            #: «запрета нет» ровно про тот режим, на котором стоит весь замер.
            "no_repeat_ngram": _decoding_field(reports, "no_repeat_ngram"),
            "decoding_standard": _decoding_field(reports, "standard"),
            "allowed_for_conclusions": _decoding_field(reports, "allowed_for_conclusions"),
            "comparability": ("протокол совпадает с парным контролем S3aq "
                              "(format_wide_4096_pair.json): набор wide, бюджет 4096, "
                              "остановка на конце хода, пакет 8, штатный запрет 4-грамм"),
        },
        "rules": {
            "unit": ("наблюдение — ПРОБА, не блок: блоки одной генерации зависимы, и "
                     "доля по блокам завышала бы n"),
            "trend": ("наклон линейной вероятностной модели по пулу проб всех точек; "
                      "направление называется при p < 0.05, иначе — «плато в пределах "
                      "разрешения»"),
            "resolution": ("MDD — минимальная разница долей между двумя точками по n проб "
                           "каждая при мощности 0.8 и α = 0.05; сравнивается с изменением "
                           "за диапазон"),
            "unclosed": ("незакрытый <think> — только среди естественно завершённых ходов "
                         "(ADR-045 п.5); у усечённого показателя нет"),
            "regions": ("распределение по сегментам берётся из `distinct_list` прибора "
                        "происхождения; сегмент `all` — ход целиком и пересекается с "
                        "остальными, поэтому суммы по сегментам НЕ складываются "
                        "(петля рассуждения попадает и в `all`, и в `think`)"),
        },
        "points": [{k: v for k, v in p.items() if k != "vectors"} for p in points],
        "steps_covered": steps,
        "states_excluded": excluded,
        "trend": trends,
        "values": values,
        "answer": answer,
        "inventory": inventory_block,
        "limits": [
            "НЕ отделяет темп обучения от длительности: шаг — и то, и другое сразу; "
            "«петли растут к шагу N» не различает «дефект копится с обучением» и "
            "«дефект растёт со временем прогона»",
            _early_coverage_limit(inventory_block, points),
            "ОДИН прогон, ОДИН набор: переносимость на другой набор или другой сид "
            "этим замером не проверяется",
            "Причинность не измеряется: тренд говорит «меняется вместе с шагом», "
            "а не «шаг это вызывает»",
            "Замер стоит на ИНФЕРЕНСЕ, а не на обучении: читается поведение "
            "сохранённых весов на фиксированном наборе промптов, а не сам ход "
            "обучения (ни лосса, ни порядка данных, ни эпох здесь нет) — «на шаге N "
            "стало хуже» и «в обучении на шаге N что-то сломалось» это разные "
            "утверждения, и второе из этого замера не следует",
            "Шум на точку: 104 пробы — это n, при котором разница долей меньше MDD "
            "неразличима в принципе, а не «пока не набралось»",
            "Числа блоков по словам (блоки, различные, сегменты) — ОПИСАТЕЛЬНЫ: "
            "единица наблюдения в тесте — проба, и блоки в значимость не входят",
        ],
        "open_questions": [
            "Откат, если он есть, может идти не по шагу, а по эпохам (кратность строки): "
            "разделить их на этом замере нельзя, для этого нужен прогон с одной эпохой",
            "Если тренд окажется плато — это не отвечает, вернётся ли дефект позже: "
            "прогон остановлен на 49 %, за его пределом данных нет",
        ],
    }
    return data, EXIT_OK


def selftest() -> int:
    """Фикстуры арифметики: без них «тренд» — это то, что инструмент сказал."""
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    ci = wilson(20, 100)
    check("Уилсон: центр близко к доле", abs(ci["mean"] - 0.2) < 1e-9)
    check("Уилсон: интервал содержит долю", ci["lo"] < 0.2 < ci["hi"])
    check("Уилсон: границы внутри [0,1]", 0 <= ci["lo"] and ci["hi"] <= 1)

    t = two_prop_test(10, 104, 30, 104)
    check("двухдолевой: рост 10→30 различим", t["p"] < 0.01 and t["delta"] > 0)
    t0 = two_prop_test(20, 104, 20, 104)
    check("двухдолевой: равные доли не различимы", t0["p"] > 0.99)

    m = mdd(0.4, 104)
    check("MDD при n=104 и p=0.4 порядка 0.19", 0.15 < m < 0.25)
    check("MDD растёт при меньшем n", mdd(0.4, 26) > m)

    xs = list(range(0, 800))
    flat = ols_slope(xs, [1.0] * len(xs))
    check("МНК: постоянный ряд даёт ровно нулевой наклон", flat["slope"] == 0.0)

    #: Чередование 1/0 — НЕ горизонтальный ряд: знак y меняется вместе со знаком
    #: (x - x̄), и произведения складываются, а не гасятся. Фикстура держит именно
    #: это: шум без тренда обязан быть неразличим (p ≥ 0.05), но не равен нулю.
    alt = ols_slope(xs, [1.0 if i % 2 == 0 else 0.0 for i in xs])
    check("МНК: чередование 1/0 неотличимо от нуля", alt["p"] > 0.05)

    xs2 = [0] * 100 + [100000] * 100
    ys2 = [0.0] * 100 + [1.0] * 100
    grow = ols_slope(xs2, ys2)
    check("МНК: ступенька даёт рост", grow["slope"] > 0 and grow["p"] < 0.001)

    sp = spearman_exact([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
    check("Спирмен: монотонный ряд rho = 1", abs(sp["rho"] - 1) < 1e-9)
    check("Спирмен: перестановок 120", sp["permutations"] == 120)

    #: Пути долей — внутри УЖЕ разыменованной сводки. Фикстура ловит ровно ту
    #: ошибку, что была: ведущий сегмент `aggregate` делал все доли `null`, а
    #: производные счётчики — нулями, и артефакт выглядел собранным.
    agg_fixture = {"looped_share": 0.0577, "truncated_share": 0.4519,
                   "lengths": {"median": 1042, "mean": 2042.2},
                   "stop": {"natural_stop_share": 0.5481,
                            "natural": {"n": 57, "unclosed_think_share": 0.3684}}}
    resolved = {k: get_path(agg_fixture, p)
                for k, p in {**SHARE_METRICS, **VALUE_METRICS}.items()}
    check("пути метрик разрешаются в сводке отчёта",
          all(v is not None for v in resolved.values()))
    check("путь метрики не повторяет имя aggregate",
          all("aggregate" not in p for p in
              list(SHARE_METRICS.values()) + list(VALUE_METRICS.values())))

    #: Ответ выводится из вердиктов: фикстуры держат все три ветки, чтобы
    #: формулировка не могла «съехать» на удобную при смене данных.
    def _tr(v, span=False):
        return {"available": True, "verdict": v, "pooled_share": 0.3,
                "mdd_two_points_n104": {"value": 0.1},
                "change_over_range": {"point_estimate": 0.01},
                "span_exceeds_mdd": span}

    all_flat = {k: _tr("plateau_within_resolution") for k in DEFECT_METRICS}
    check("ответ: плато во всех долях → ветка «ни один план не подтверждён»",
          _answer(all_flat, [], None)["defect_direction"] == "plateau")
    all_down = {k: _tr("decline") for k in DEFECT_METRICS}
    check("ответ: падение долей → дефект убывает",
          _answer(all_down, [], None)["defect_direction"] == "falls")
    mixed = {k: _tr("growth" if k == DEFECT_METRICS[0] else "plateau_within_resolution")
             for k in DEFECT_METRICS}
    check("ответ: рост по одной доле не выдаётся за плато",
          _answer(mixed, [], None)["defect_direction"] == "flat_or_grows")
    none_avail = {k: {"available": False} for k in DEFECT_METRICS}
    check("ответ: нет данных → «недостаточно данных», а не плато",
          _answer(none_avail, [], None)["defect_direction"] == "no_data")

    #: Перепись: сетка точек восстанавливается по диску, потеря ниже первой —
    #: арифметика. Фикстура держит именно счёт, ради которого поле заведено.
    cov = _coverage([21000, 21500, 22000, 32500], [])
    check("перепись: шаг сетки восстановлен", cov["probe_grid_step"] == 500)
    check("перепись: ниже первой уцелевшей 41 точка",
          cov["probe_points_below_first"]["count"] == 41)
    check("перепись: уцелевшее окно есть и без манифеста стадии",
          cov["surviving_probe_window"]["first"] == 21000
          and "measured_window" not in cov)
    check("перепись: без манифеста доля расписания не выдумывается",
          "declared_max_steps" not in cov
          and "first_share_of_plan" not in cov["surviving_probe_window"])
    check("перепись: потеря названа диапазоном 500…20500",
          (cov["probe_points_below_first"]["from"],
           cov["probe_points_below_first"]["to"]) == (500, 20500))
    check("граница без переписи не выдумывает число",
          "не названо" in _early_coverage_limit(None, []))
    check("граница с переписью называет потерю числом",
          "потеряно точек проб: 41" in _early_coverage_limit(
              {"coverage": cov}, []))

    check("шаг читается из метки", step_of("sft_v13_21500") == 21500)
    check("шаг из трёх цифр читается (500…999)", step_of("sft_v13_500") == 500)
    check("метка без шага не даёт числа", step_of("cfinal") is None)
    check("двузначная версия за шаг не берётся", step_of("model_v13") is None)
    print("selftest:", "OK" if ok else "FAIL")
    return EXIT_OK if ok else EXIT_FAIL


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="append", default=[],
                    help="отчёт проб (можно несколько); допускается git:<ref>:<путь>")
    ap.add_argument("--origin", default=None,
                    help="свод прибора происхождения (analyze_loop_origin.py) — блоки по словам")
    ap.add_argument("--ckpt-dir", action="append", default=[],
                    help="каталог чекпойнтов для переписи диска (повторяемый)")
    ap.add_argument("--range-lo", type=int, default=20000, help="нижняя граница переписи")
    ap.add_argument("--range-hi", type=int, default=32600, help="верхняя граница переписи")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.report:
        note("NOT-VERIFIED: не задан ни один --report")
        return EXIT_NOT_VERIFIED
    data, code = build(args)
    if code != EXIT_OK:
        return code
    if args.out and not args.no_write:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
        note(f"отчёт записан: {args.out}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
