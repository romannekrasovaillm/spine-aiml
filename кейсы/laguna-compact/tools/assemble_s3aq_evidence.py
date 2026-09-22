#!/usr/bin/env python3
"""S3aq — свод: обрывы блоков это усечение выросших рассуждений или деградация обучения.

**Вопрос дельты.** Замер S3ap (бюджет 4096) дал у свежей точки v13 обрывы блоков
0.7596, покрытие ответа 0.3269, усечений 0.5288 — при медиане длины хода, **упёршейся
в бюджет пробы** (408 → 928 → 4096 при бюджете 4096). Отсюда два несовместимых
диагноза, которые одним числом не различаются:

* **усечение** — модель пишет всё более длинные рассуждения, они не влезают в 4096
  токенов, ход не доходит ни до `</think>`, ни до ответа; тогда «обрыв» — свойство
  бюджета пробы, а не модели, и стадия по формату не провалена;
* **деградация** — модель разучилась закрывать блок; тогда бюджет не при чём, и это
  содержательный дефект обучения.

Различает их **тот же замер с бюджетом выше медианы с запасом** (ADR-045 п.2).

**Правило вердикта объявлено ДО замера и здесь не уточняется** (ADR-045 п.2):

===========  ==========================================================
обрывы 8192  вывод
===========  ==========================================================
≤ 0.35       механизм «усечение» подтверждён; стадия по формату не провалена,
             протокол замеров переводится на 8192, работа — на контроль длины
> 0.5        причина в модели/обучении, а не в бюджете; рекомендация остановить
             стадию (прогон дельта не останавливает — решение владельца)
0.35 … 0.5   «недостаточно данных»: назвать, что домерить
===========  ==========================================================

Пороги — константы модуля (ниже), а не параметры вывода: их нельзя подставить
после того, как числа стали известны.

**Что сводится.**

1. `budget_8192` — решающий прогон (`format_wide_8192.json`): CPT-финал и свежая
   точка v13, те же прибор/режим/набор промптов, что у S3ap, но бюджет 8192.
2. `budget_4096` — (а) нормативная база S3ap там, где состояния совпадают, и
   (б) **парный контроль** (`format_wide_4096_pair.json`): тот же свежий чекпойнт на
   бюджете 4096, снятый в те же сутки. Парное сравнение «то же состояние, два
   бюджета» — самая сильная форма различения: она не опирается ни на соседние шаги
   (обрывы растут по шагам), ни на прошлые сутки (дрейф окружения).
3. `unclosed_rule_applied` — правило ADR-045 п.5: незакрытый `<think>` — дефект
   **только среди естественно завершённых ходов**; для усечённых показатель не
   определён. Правило в приборе **уже есть** (`aggregate.stop.natural`), вводить его
   заново не нужно — и вредно: правка файла прибора сломала бы тождество с базой.
   Здесь оно применяется к обоим бюджетам и разница показывается числом.
4. `length_growth` — растёт ли длина и на 8192 (то есть «догоняет» ли модель бюджет)
   и какова доля ходов длиннее бюджета.

**Чего в своде нет и почему.** Точки v13@500/5000/9000, снятые S3ap на 4096,
**удалены ретенцией прогона** (PROBE_KEEP_LAST = 24 пробы) — перемерять их на 8192
нечем. Это фиксируется явно (`unavailable_states`), а не заменяется молча другими
шагами: 5000/9000 — не то же самое, что 10000/21500, и подмена сдвинула бы вывод.

Коды возврата::

    0 — свод собран, контракт входа соблюдён
    1 — отказ: вход противоречит контракту (не штатный режим, чужой набор промптов,
        нет остановки на конце хода, n < 104, тождество прибора не подтверждено,
        нет бюджетных пар, вердикт-числа отсутствуют)
    2 — NOT-VERIFIED: нечего сводить — **отсутствует хоть один объявленный вход**
        (решающий отчёт, парный отчёт, база S3ap, гейт тождества ядра, пиннинг
        чекпойнтов). Отсутствие входа и плохой вход — разные факты: первое означает
        «сводить нечего», второе — «числа несопоставимы». Различие обязательно, иначе
        свод, собранный на половине входов, читался бы как полный.

**Почему входы перечислены явно.** Список `INPUTS` — контракт, а не удобство: свод
обязан отказать (rc ≠ 0), если хоть один вход отсутствует. Ослаблять это нельзя —
именно отсутствие входа (а не его качество) даёт свод, который выглядит собранным,
но описывает не тот замер.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

RUN = "runs/s3aq-budget-8192-20260920"
REPORT_8192 = RUN + "/format_wide_8192.json"
REPORT_4096_PAIR = RUN + "/format_wide_4096_pair.json"
#: База S3ap: числа бюджета 4096 для состояний, которые тогда существовали.
BASELINE_4096 = "runs/s3ap-format-monitor-20260919/format_wide_standard.json"
IDENTITY_NEW = RUN + "/identity_core_cfinal.json"
IDENTITY_REF = "runs/s3ai-probes-20260917/identity_core_cfinal.json"
PIN_CHECK = RUN + "/pin_checkpoints.json"
EVIDENCE = "evidence/s3aq-budget-8192.json"

#: ── Контракт входа: без любого из этих файлов свод не собирается ──────────────
#: имя → (имя константы модуля, роль). Путь берётся из константы **в момент вызова**
#: (тесты подменяют константы на фикстуры), а состав входов подменить нельзя:
#: отсутствие любого входа — NOT-VERIFIED (rc 2), а не «свод без раздела». Свод,
#: собранный на половине входов, неотличим от полного.
INPUTS = {
    "report_8192": ("REPORT_8192", "решающий отчёт прибора (бюджет 8192)"),
    "report_4096_pair": ("REPORT_4096_PAIR", "парный контроль (тот же чекпойнт, 4096)"),
    "baseline_4096": ("BASELINE_4096", "нормативная база S3ap (бюджет 4096)"),
    "identity_core": ("IDENTITY_NEW", "гейт тождества ядра промптов"),
    "identity_reference": ("IDENTITY_REF", "эталон ядра промптов (S3ai)"),
    "pin_checkpoints": ("PIN_CHECK", "пиннинг чекпойнтов: sha256 копий против источников"),
}

#: ── Правило вердикта, объявленное ДО замера (ADR-045 п.2) ─────────────────────
#: Значения зашиты константами намеренно: правило, которое можно «уточнить» после
#: того, как числа стали известны, — не правило. Верхняя граница «уровня CPT»
#: названа в ADR-045 до замера (0.35 — фактический уровень CPT 0.3365).
CPT_LEVEL_MAX = 0.35          # обрывы ≤ этого — уровень CPT: механизм «усечение»
DEGRADATION_MIN = 0.50        # обрывы > этого — причина в модели/обучении

#: Минимум проб на состояние (критерий приёмки дельты: n ≥ 104).
MIN_N = 104

#: Порог «неотличимо от CPT» для покрытия ответа: 2σ доли при n = 104 ≈ 0.10 —
#: тот же порог значимости, что в своде S3ap. Им проверяется «восстановилось ли
#: покрытие к уровню CPT», а не «стало ли больше», чтобы рост на 3 п.п. не читался
#: как восстановление.
SIGNIFICANT = 0.10

BUDGETS = (4096, 8192)

#: Ключевые метрики: имя в своде → путь в aggregate прибора. Один список на свод,
#: чтобы таблица, дельты и вердикт не разъехались по разным числам.
METRIC_PATHS = {
    "unclosed_think": ("unclosed_think_share",),
    "tool_call": ("mode_share_tool_call",),
    "answer_coverage": ("cyr_answer", "coverage"),
    "truncated": ("truncated_share",),
    "looped": ("looped_share",),
    "median_len": ("lengths", "median"),
    "p90_len": ("lengths", "p90"),
    "mean_len": ("lengths", "mean"),
    "cyr_think": ("cyr_think", "with_zeros"),
    "cyr_answer": ("cyr_answer", "with_zeros"),
    "natural_stop_share": ("stop", "natural_stop_share"),
}

#: Метрики, у которых «меньше — лучше»: без этого «дельта +0.3» читалась бы как
#: улучшение там, где это рост обрывов.
LOWER_IS_BETTER = ("unclosed_think", "truncated", "looped")


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load(p: Path) -> dict | None:
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


def dig(d: dict, path: tuple[str, ...], default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def nd(x, k: int = 4):
    return None if x is None else round(float(x), k)


def share(values: list[int], pred) -> float | None:
    return None if not values else round(sum(1 for v in values if pred(v)) / len(values), 4)


# ─────────────────────────── чтение состояний ─────────────────────────────────

def probes_of(state: dict) -> list[dict]:
    return list(state.get("probes") or [])


def state_row(tag: str, state: dict, rep: dict) -> dict:
    """Строка состояния: метрики формата + длины + правило «только завершённые»."""
    agg = state.get("aggregate") or {}
    row: dict = {"state": tag, "budget": dig(rep, ("protocol", "max_new_tokens"))}
    for name, path in METRIC_PATHS.items():
        row[name] = nd(dig(agg, path))
    row["n"] = agg.get("n")
    row["checkpoint"] = state.get("checkpoint")
    row["checkpoint_sha256"] = state.get("checkpoint_sha256")
    row["n_new_tokens_sum"] = None            # заполняется ниже, если есть пробы

    toks = [int(p.get("n_new_tokens") or 0) for p in probes_of(state)]
    budget = row["budget"]
    if toks and budget:
        row["n_new_tokens_sum"] = sum(toks)
        #: Доля ходов, **упёршихся в бюджет пробы**: мера того, насколько замер
        #: меряет бюджет, а не модель.
        row["share_at_budget"] = share(toks, lambda v: v >= budget)
        #: Сколько ходов пережило бы бюджет 4096 — прямой ответ на вопрос «какая
        #: часть обрывов 4096 была бы снята бюджетом 8192» (только для 8192-прогона;
        #: у 4096-прогона величина структурно нулевая и не измеряется).
        row["share_longer_than_4096"] = (
            share(toks, lambda v: v > 4096) if budget > 4096 else None)
        row["median_over_budget"] = nd(dig(agg, ("lengths", "median_over_budget")))
    #: Правило ADR-045 п.5 — обрывы **среди естественно завершённых** ходов.
    nat = dig(agg, ("stop", "natural"), {}) or {}
    row["n_natural"] = nat.get("n")
    row["unclosed_think_natural"] = nd(nat.get("unclosed_think_share"))
    row["answer_coverage_natural"] = nd(nat.get("answer_coverage"))
    row["stop_reasons_share"] = dig(agg, ("stop", "reasons_share"))
    #: Насколько правило п.5 сдвигает число обрывов относительно полного чтения:
    #: положительная разница = часть обрывов сидела в усечённых ходах.
    if row["unclosed_think"] is not None and row["unclosed_think_natural"] is not None:
        row["unclosed_rule_shift"] = nd(row["unclosed_think"] - row["unclosed_think_natural"])
    else:
        row["unclosed_rule_shift"] = None
    return row


def collect(rep: dict) -> tuple[list[dict], list[dict]]:
    """Состояния отчёта: измеренные строки и явно пропущенные (нет чекпойнта)."""
    rows, missing = [], []
    for tag, st in (rep.get("states") or {}).items():
        if "probes" in st:
            rows.append(state_row(tag, st, rep))
        else:
            missing.append({"state": tag, "reason": st.get("skipped") or "нет проб",
                            "checkpoint": st.get("checkpoint")})
    rows.sort(key=lambda r: (0 if r["state"] == "cfinal" else 1, r["state"]))
    return rows, missing


def step_of(row: dict) -> int | None:
    tail = str(row.get("state", "")).rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


def fresh_row(rows: list[dict]) -> dict | None:
    """Свежая точка v13: максимальный шаг среди измеренных (не cfinal)."""
    cands = [(step_of(r), r) for r in rows if r["state"] != "cfinal"]
    cands = [(s, r) for s, r in cands if s is not None]
    return max(cands, key=lambda sr: sr[0])[1] if cands else None


# ───────────────────────────── вердикт ────────────────────────────────────────

def rule_zone(unclosed: float | None) -> tuple[str, str]:
    """Зона вердикта по числу обрывов — **только по правилу ADR-045 п.2**.

    Возвращает (зона, механизм). Зона называется дословно так, как объявлена в ADR:
    подтверждено / не подтверждено / недостаточно данных. Никаких «почти» — правило
    объявлено заранее именно для того, чтобы граница не сдвигалась под результат.
    """
    if unclosed is None:
        return "not_measured", "нет числа — нет вердикта"
    if unclosed <= CPT_LEVEL_MAX:
        return "truncation_confirmed", "усечение выросших рассуждений"
    if unclosed > DEGRADATION_MIN:
        return "degradation", "модель/обучение (бюджет не при чём)"
    return "insufficient_data", "недостаточно данных для вердикта"


def delta_row(base: float | None, cur: float | None, metric: str = "") -> dict:
    if base is None or cur is None:
        return {"delta": None, "direction": None, "significant": None}
    d = round(float(cur) - float(base), 4)
    if abs(d) < SIGNIFICANT:
        return {"delta": d, "direction": "шум", "significant": False}
    if d < 0:
        direction = "лучше" if metric in LOWER_IS_BETTER else "хуже"
    else:
        direction = "хуже" if metric in LOWER_IS_BETTER else "лучше"
    return {"delta": d, "direction": direction, "significant": True}


#: Журнал добора: строки `[ГГГГ-ММ-ДД ЧЧ:ММ:СС] …`. Времена берутся оттуда, а не
#: из mtime файлов: mtime меняется при любом касании, а запись в журнале — факт хода.
RESUME_LOG = RUN + "/resume_probe.log"
LOG_TS = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(?P<rest>.*)$")


def measurement_cost(rep_a: dict, rep_b: dict, log_path: str | None = None) -> dict | None:
    """Цена замера и добора — из журнала прогона и отчётов, а не из оценки.

    Считается только то, что измерено: сколько проб, сколько токенов, сколько секунд
    (по отметкам журнала) и какой из этого темп. Оценка добора даётся **отдельно** от
    факта и помечена как оценка: экстраполяция по измеренному темпу, а не замер.
    Журнала нет — блок не выдумывается (`available: false`), потому что «нет числа» и
    «ноль» — разные вещи.
    """
    #: Путь берётся из константы в момент вызова (тесты подменяют её на фикстуру).
    log_path = log_path or RESUME_LOG
    p = CASE / log_path
    if not p.is_file():
        return {"available": False, "why": f"нет журнала добора {log_path} — цена не измерена"}
    marks: list[tuple[str, str]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LOG_TS.match(line.strip())
        if m:
            marks.append((m.group("ts"), m.group("rest")))
    if len(marks) < 3:
        return {"available": False, "why": f"в журнале {log_path} нет отметок хода добора"}
    t = [datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") for ts, _ in marks]
    end_a = next((t[i] for i, (_, r) in enumerate(marks) if r.startswith("A: код возврата")), None)
    end_b = next((t[i] for i, (_, r) in enumerate(marks) if r.startswith("B: код возврата")), None)
    if end_a is None or end_b is None:
        return {"available": False, "why": "в журнале нет отметок завершения этапов A/B"}

    def leg(seconds: float, label: int, rep: dict) -> dict:
        toks = sum(int(pr.get("n_new_tokens") or 0) for st in (rep.get("states") or {}).values()
                   for pr in (st.get("probes") or []))
        n = sum(len(st.get("probes") or []) for st in (rep.get("states") or {}).values())
        return {"budget": label, "probes": n, "seconds": round(seconds),
                "hours": nd(seconds / 3600, 2), "tokens": toks,
                "tokens_per_s": nd(toks / seconds, 1) if seconds else None,
                "s_per_probe": nd(seconds / n, 1) if n else None}

    #: Токены каждого этапа — из его собственного отчёта: подставить один отчёт в оба
    #: этапа значило бы выдать длину 8192-прогона за длину 4096-прогона.
    leg_a = leg((end_a - t[0]).total_seconds(), 8192, rep_a)
    leg_b = leg((end_b - end_a).total_seconds(), 4096, rep_b)
    per_probe = (leg_a["tokens"] / leg_a["probes"]) if leg_a["probes"] else None
    rate = leg_a["tokens_per_s"]
    #: Оценка добора: тот же чекпойнт, бюджет 12288. Нижняя граница — длины не выросли,
    #: верхняя — растут как в этом замере (доля упёршихся 0.44 при длине хода ~x1.4).
    est = None
    if per_probe and rate:
        est = {"budget": 12288, "n_probes": 208,
               "lower_h": nd(208 * per_probe / rate / 3600, 1),
               "upper_h": nd(208 * per_probe * 1.4 / rate / 3600, 1),
               "pair_4096_h": nd(208 * (leg_b["tokens"] / max(leg_b["probes"], 1))
                                 / (leg_b["tokens_per_s"] or 1) / 3600, 1)}
    return {
        "available": True, "source": log_path, "log_marks": len(marks),
        "leg_a_8192": leg_a, "leg_b_4096": leg_b,
        "measured_total_h": nd(leg_a["hours"] + leg_b["hours"], 2),
        "extrapolated_further_measurement": est,
        "basis": ("секунды — по отметкам журнала добора; токены — суммой n_new_tokens "
                  "по пробам отчётов; темп — отношением, а не оценкой"),
        "caveat": ("числа этапов A/B включают загрузку модели и кодирование промптов, "
                   "то есть цена прогона целиком, а не чистой генерации; оценка добора — "
                   "экстраполяция по измеренному темпу того же прибора, а не замер"),
    }


def probe_fate(rep_a: dict, rep_b: dict, state: str) -> dict:
    """Судьба ходов, которым дополнительный бюджет снял усечение, — по пробам.

    Агрегат говорит «доля усечённых упала»; он не говорит, **что стало с этими ходами**.
    Ответ лежит в пробах, и он же отвечает на главный вопрос различения: ход, которому
    бюджета хватило, закрыл блок или нет. Пробы сопоставляются по индексу: greedy на
    общем префиксе воспроизводит ход, поэтому индекс — тот же промпт (совпадение
    промптов проверяется и называется, а не предполагается).

    Отдельно проверяется детерминизм: ходы, дописанные при меньшем бюджете, обязаны
    совпасть при большем побайтово. Это значит, что **совпадение агрегатов само по себе
    не является свидетельством о модели** — оно следует из детерминизма; свидетельство
    даёт судьба тех ходов, которые от бюджета зависят.
    """
    pa = (rep_a.get("states", {}).get(state) or {}).get("probes") or []
    pb = (rep_b.get("states", {}).get(state) or {}).get("probes") or []
    if not pa or not pb or len(pa) != len(pb):
        return {"available": False, "state": state,
                "why": f"пробы состояния {state} не сопоставимы по индексам "
                       f"({len(pb)} и {len(pa)}) — судьба ходов не восстанавливается"}
    aligned = all(x.get("prompt") == y.get("prompt") for x, y in zip(pa, pb))
    recovered = [i for i, (x, y) in enumerate(zip(pa, pb))
                 if y.get("hit_limit") and not x.get("hit_limit")]
    still = [i for i in recovered if (pa[i].get("metrics") or {}).get("unclosed_think")]
    short = [i for i, x in enumerate(pb) if not x.get("hit_limit")]
    identical = sum(1 for i in short if pa[i].get("response") == pb[i].get("response"))
    tr_b, tr_a = sum(1 for x in pb if x.get("hit_limit")), sum(1 for x in pa if x.get("hit_limit"))
    return {
        "available": True, "state": state,
        "prompts_aligned_by_index": aligned,
        "truncated_4096": tr_b, "truncated_8192": tr_a,
        "recovered_by_budget": {
            "n": len(recovered), "indexes": recovered,
            "still_unclosed_indexes": still,
            "closed_indexes": [i for i in recovered if i not in still],
        },
        "determinism": {
            "finished_at_4096": len(short), "byte_identical_at_8192": identical,
            "all_identical": bool(short) and identical == len(short),
        },
        "reading": (
            f"бюджет 8192 снял усечение у {len(recovered)} ходов из {tr_b}; из них "
            f"{len(still)} остались с незакрытым `<think>` — у этих ходов незавершение "
            f"не от бюджета"),
        "determinism_note": (
            f"ходы, дописанные при 4096 ({len(short)}), при 8192 совпали побайтово "
            f"({identical} из {len(short)}): совпадение агрегатов на двух бюджетах "
            "частично следует из детерминизма greedy, поэтому само по себе свидетельством "
            "о модели не является — свидетельство даёт судьба ходов, зависящих от бюджета"),
    }


def estimate_resolution(fresh: dict, u: float | None, u_nat: float | None) -> dict:
    """Насколько число способно отделить границу зоны при этом n (границы не двигаются).

    Правило ADR-045 п.2 — решающее, и оно применяется как объявлено. Но у оценки доли
    при n = 104 есть собственное разрешение (2σ ≈ 0.10), и молчать об этом значило бы
    выдавать попадание в зону за доказанное неравенство. Числа приводятся рядом.
    """
    def margin(p, n, bound):
        if p is None or not n:
            return None
        sd = math.sqrt(max(p * (1 - p), 1e-12) / n)
        return {"value": nd(p), "n": n, "bound": bound, "distance": nd(abs(p - bound), 4),
                "two_sigma": nd(2 * sd, 4), "sigmas": nd(abs(p - bound) / sd, 2) if sd else None}
    out = {
        "why": ("расстояние до границы зоны в сигмах — разрешение самой оценки, не сдвиг "
                "порогов: правило применяется как объявлено, а здесь видно, насколько "
                "число отделяет зоны при данном n"),
        "full_read": margin(u, fresh.get("n"), DEGRADATION_MIN if (u or 0) > DEGRADATION_MIN
                            else CPT_LEVEL_MAX),
        "rule_5_natural": margin(u_nat, fresh.get("n_natural"),
                                 DEGRADATION_MIN if (u_nat or 0) > DEGRADATION_MIN
                                 else CPT_LEVEL_MAX),
    }
    #: Сильнейшее свидетельство — не пересечение порога (это сравнение с фиксированной
    #: границей), а парный контроль: то же состояние, два бюджета, сдвиг ровно ноль.
    #: 0.5 — не содержательная граница, а точка смеси двух подвыборок: доля дефекта среди
    #: естественно завершённых ходов лежит **ниже** неё, полная доля — выше. Знак отклонения
    #: от «границы» меняется вместе со способом счёта, поэтому привязывать вывод к 0.5 как
    #: к содержательной границе нельзя — числа приводятся, чтобы это было видно.
    def sigmas_from(p, n, bound):
        if p is None or not n:
            return None
        sd = math.sqrt(max(p * (1 - p), 1e-12) / n)
        return nd((bound - p) / sd, 2)
    s_full = sigmas_from(u, fresh.get("n"), DEGRADATION_MIN)
    s_nat = sigmas_from(u_nat, fresh.get("n_natural"), DEGRADATION_MIN)
    def side(p, bound):
        return "выше" if (p or 0) > bound else "ниже"
    out["subsample_note"] = (
        ("0.5 — точка смеси, а не содержательная граница: полная доля %s лежит на %sσ %s неё, "
         "доля по естественно завершённым %s — на %sσ %s (знак отклонения обратный), поэтому "
         "вердикт не сводится к «выше/ниже 0.5»: зону даёт объявленное правило, а механизм — "
         "парный контроль"
         % (nd(u), abs(s_full), side(u, DEGRADATION_MIN),
            nd(u_nat), abs(s_nat), side(u_nat, DEGRADATION_MIN)))
        if s_full is not None and s_nat is not None else
        "чисел нет — смещение подвыборок не считается")
    m = out["full_read"]
    out["reading"] = (
        "число отделено от границы на %sσ — при n = %s это пересечение границы, а не "
        "доказанное неравенство; решающим свидетельством механизма служит парный "
        "контроль (то же состояние на двух бюджетах), а не величина относительно порога"
        % (m["sigmas"], m["n"]) if m and m.get("sigmas") is not None else
        "нет числа — разрешение не считается")
    return out


def make_verdict(fresh_8192: dict, cpt_8192: dict | None, fresh_4096: dict | None) -> dict:
    """Вердикт по правилу ADR-045 п.2 и рекомендация — с числами, без решения за владельца."""
    u = fresh_8192.get("unclosed_think")
    zone, mechanism = rule_zone(u)
    cpt_level = (cpt_8192 or {}).get("unclosed_think")
    cov = fresh_8192.get("answer_coverage")
    cpt_cov = (cpt_8192 or {}).get("answer_coverage")
    cov_u4096 = (fresh_4096 or {}).get("answer_coverage")
    cov_restored = (None if cov is None or cpt_cov is None
                    else bool(abs(float(cov) - float(cpt_cov)) < SIGNIFICANT))
    u_4096 = (fresh_4096 or {}).get("unclosed_think")

    if zone == "truncation_confirmed":
        recommendation = (
            "стадию по формату не считать проваленной; протокол проб и замеров "
            "перевести на бюджет 8192; дальнейшая работа — контроль длины рассуждений "
            "(нормирование длины в данных или предел в обучении), а не лечение формата")
        stop_stage = False
    elif zone == "degradation":
        recommendation = (
            "рекомендовать владельцу остановить стадию: бюджет не объясняет обрывы, "
            "причина в модели/обучении. Прогон дельта НЕ останавливала — решение владельца")
        stop_stage = True
    elif zone == "insufficient_data":
        recommendation = (
            "вердикт не выносится: домерить бюджет 12288 на том же чекпойнте тем же "
            "прибором и/или расширить выборку (n > 104), чтобы отделить 0.35–0.5")
        stop_stage = False
    else:
        recommendation = "нет числа обрывов — вердикт не выносится, замер не состоялся"
        stop_stage = False

    #: ── То же правило к числу по ADR-045 п.5 (только естественно завершённые) ────
    #: Вердикт считается по тому же ряду, на котором правило объявлено: порог 0.35
    #: назван по уровню CPT из таблицы ADR-045 (0.3365) — это **полное чтение**, и
    #: подменять ряд после того, как число стало известно, значило бы двигать правило.
    #: Число по п.5 идёт рядом второй зоной: оно отвечает на вопрос «а если считать
    #: дефектом только там, где показатель определён (п.5)?» — и когда зоны расходятся,
    #: это обязано быть названо, а не выбрано задним числом.
    u_nat = fresh_8192.get("unclosed_think_natural")
    zone_nat, mechanism_nat = rule_zone(u_nat)
    agree = (zone == zone_nat)
    if u_nat is None:
        zones_note = ("число по п.5 не измерено — сравнить нечем; вердикт по полному чтению")
    elif agree:
        zones_note = (f"оба чтения дают одну зону ({zone}): полное {nd(u)} и по п.5 "
                      f"{nd(u_nat)} — расхождения между чтениями нет")
    else:
        zones_note = (
            f"чтения расходятся: полное {nd(u)} → {zone}, по п.5 (только завершённые) "
            f"{nd(u_nat)} → {zone_nat}. Вердикт вынесен по полному чтению — ряду, на "
            f"котором объявлено правило (порог 0.35 назван по уровню CPT 0.3365 из того "
            f"же ряда); число по п.5 приведено рядом, решение читается по обоим")

    return {
        "mechanism": mechanism,
        "unclosed_8192_natural": nd(u_nat),
        "unclosed_8192_natural_cpt": nd((cpt_8192 or {}).get("unclosed_think_natural")),
        "unclosed_4096_natural_same_state": nd((fresh_4096 or {}).get("unclosed_think_natural")),
        "n_natural_8192": fresh_8192.get("n_natural"),
        "rule_zone_natural": zone_nat,
        "mechanism_natural": mechanism_nat,
        "rule_zones_agree": agree,
        "rule_zones_note": zones_note,
        "rule_zone_source": ("полное чтение (все ходы) — ряд, на котором правило "
                             "ADR-045 п.2 объявлено до замера"),
        #: Разрешение оценки: расстояние до границы зоны в сигмах. Пороги не двигаются —
        #: это ответ на другой вопрос: «насколько число вообще способно отделить зоны
        #: при n = 104». Печатается рядом с вердиктом, чтобы зона не читалась как
        #: доказанное неравенство там, где это пересечение границы на 1–2σ.
        "estimate_resolution": estimate_resolution(fresh_8192, u, u_nat),
        "unclosed_8192": nd(u),
        "unclosed_8192_cpt": nd(cpt_level),
        "unclosed_4096_same_state": nd(u_4096),
        "coverage_8192": nd(cov),
        "coverage_8192_cpt": nd(cpt_cov),
        "coverage_4096_same_state": nd(cov_u4096),
        "rule_zone": zone,
        "rule": {
            "source": "ADR-045 п.2 (объявлено до замера)",
            "cpt_level_max": CPT_LEVEL_MAX,
            "degradation_min": DEGRADATION_MIN,
            "reading": ("обрывы при 8192 ≤ %.2f → усечение; > %.2f → деградация; "
                        "между — недостаточно данных" % (CPT_LEVEL_MAX, DEGRADATION_MIN)),
        },
        "coverage_restored_to_cpt": cov_restored,
        "coverage_tolerance": SIGNIFICANT,
        "recommendation": recommendation,
        "recommend_stop_stage": stop_stage,
        #: Что именно сдвинуло бы вердикт: названо число и условие, а не «домерить».
        "what_would_change_decision": (
            "число по п.5 (%s) ниже 0.35 — это сдвинуло бы вердикт в «усечение» "
            "(при полном чтении ниже 0.35); полное чтение выше 0.5 подтвердило бы "
            "деградацию на расширенной выборке" % nd(u_nat)),
        #: Оговорка к рекомендации, когда две зоны расходятся: владелец обязан видеть
        #: обе, иначе решение примет за него выбор ряда, сделанный здесь.
        "recommendation_caveat": (None if agree else
                                  "рекомендация опирается на полное чтение; при чтении по п.5 "
                                  "(ADR-045 п.5) число %s попадает в зону %s — решение "
                                  "принимается владельцем по обоим числам" % (nd(u_nat), zone_nat)),
        "decision_owner": "владелец стадии (дельта прогон не останавливала и не трогала)",
    }


# ─────────────────────────── длина и правило п.5 ───────────────────────────────

def budget_note(r: dict, old: dict) -> dict:
    """Ограничитель ли бюджет: по состоянию, на обоих бюджетах, с числами.

    Числа, которые здесь называются, читаются вместе и порознь не значат ничего:
    доля упёршихся в лимит при **низкой** медиане означает не «рассуждения выросли»,
    а «часть ходов не завершается вовсе» — это другой механизм, и его нельзя
    записывать в ту же строку вывода, что плавный рост длины.
    """
    at82, over = r.get("share_at_budget"), r.get("median_over_budget")
    at41 = old.get("share_at_budget")
    if at82 is None:
        return {"budget_note": "нет длин — доля упёршихся в лимит не считается"}
    binds = at82 >= 0.25
    if not binds:
        note = (f"бюджет не ограничитель: в лимит упёрлось {at82:.4f} ходов "
                f"(медиана {r.get('median_len')} — {over:.2f} бюджета)")
    elif over is not None and over < 0.5:
        note = (f"бюджет ограничитель у меньшинства: медиана {r.get('median_len')} "
                f"({over:.2f} бюджета) далека от лимита, но в лимит упёрлось {at82:.4f} "
                f"ходов — распределение двумодальное, а не «длина выросла»")
    else:
        note = (f"бюджет ограничитель: в лимит упёрлось {at82:.4f} ходов, медиана "
                f"{r.get('median_len')} ({over:.2f} бюджета)")
    return {"budget_note": note, "budget_binds_minority":
            bool(binds and over is not None and over < 0.5),
            "at_budget_8192_vs_4096": (None if at41 is None
                                       else nd(float(at82) - float(at41), 4))}


def length_growth(rows_8192: list[dict], rows_4096: list[dict],
                  base_rows: list[dict] | None = None, budget: int = 8192) -> dict:
    """Растёт ли длина при 8192 — то есть «догоняет» ли модель бюджет.

    Число 4096 берётся из парного контроля (те же сутки, тот же чекпойнт), а где
    его нет — из базы S3ap; источник называется в строке, потому что сравнение
    «сегодня против 19.09» и «то же состояние в те же сутки» — разной силы.
    """
    by: dict[str, dict] = {r["state"]: r for r in (base_rows or [])}
    paired_states = {r["state"] for r in rows_4096}
    by.update({r["state"]: r for r in rows_4096})
    per_state = []
    for r in rows_8192:
        old = by.get(r["state"], {})
        m_new, m_old = r.get("median_len"), old.get("median_len")
        per_state.append({
            "state": r["state"],
            "budget_4096_source": (
                "парный контроль (те же сутки)" if r["state"] in paired_states
                else "база S3ap (19.09.2026)" if old else "нет"),
            "median_4096": m_old, "median_8192": m_new,
            "median_ratio": (None if not m_old or m_new is None
                             else nd(float(m_new) / float(m_old), 3)),
            "p90_4096": old.get("p90_len"), "p90_8192": r.get("p90_len"),
            "share_at_budget_8192": r.get("share_at_budget"),
            "share_at_budget_4096": old.get("share_at_budget"),
            "share_longer_than_4096": r.get("share_longer_than_4096"),
            "median_over_budget": r.get("median_over_budget"),
            "n_8192": r.get("n"), "n_4096": old.get("n"),
            **budget_note(r, old),
        })
    med = [(r.get("median_over_budget"), r["state"]) for r in rows_8192
           if r.get("median_over_budget") is not None]
    saturated = [s for v, s in med if v is not None and v >= 0.9]
    if not med:
        reading = "NOT-VERIFIED: нет длин"
    elif saturated:
        reading = ("медиана упирается в бюджет и на 8192 (%s): бюджет снова стал "
                   "ограничителем замера — рост длины не остановился" % ", ".join(saturated))
    else:
        minority = [p["state"] for p in per_state if p.get("budget_binds_minority")]
        shares_all = [p.get("share_at_budget_8192") for p in per_state
                      if p.get("share_at_budget_8192") is not None]
        if minority:
            reading = ("медиана ниже бюджета 8192 с запасом, но у %s в лимит упёрлась "
                       "заметная доля ходов (до %.4f): рост длины не плавный — у части "
                       "ходов ход не завершается вовсе, и по медиане этого не видно"
                       % (", ".join(minority), max(shares_all)))
        else:
            reading = ("медиана ниже бюджета 8192 с запасом — рост длины, если и есть, "
                       "замером не ограничен; бюджет достаточен")
    shares = [r.get("share_at_budget") for r in rows_8192 if r.get("share_at_budget") is not None]
    return {
        "budget_compared": budget,
        "per_state": per_state,
        "share_at_budget_max": max(shares) if shares else None,
        "saturated_states": saturated,
        "reading": reading,
        "method": ("медиана/p90 — ближайший ранг (nearest-rank), как в приборе; "
                   "сравнение по состояниям, измеренным на обоих бюджетах"),
    }


def unclosed_rule_applied(pairs: list[tuple[str, dict]]) -> dict:
    """Правило ADR-045 п.5 к обоим бюджетам: дефект — только среди завершённых ходов."""
    per_state = []
    for label, row in pairs:
        per_state.append({
            "state": row.get("state"), "budget": row.get("budget"), "source": label,
            "n_all": row.get("n"), "n_natural": row.get("n_natural"),
            "unclosed_all": row.get("unclosed_think"),
            "unclosed_natural": row.get("unclosed_think_natural"),
            "shift": row.get("unclosed_rule_shift"),
            "answer_coverage_all": row.get("answer_coverage"),
            "answer_coverage_natural": row.get("answer_coverage_natural"),
        })
    shifts = [p["shift"] for p in per_state if p["shift"] is not None]
    max_abs = max((abs(s) for s in shifts), default=None)
    #: Состояние без бюджета не называет строку: одно и то же состояние меряется на
    #: двух бюджетах, и «завышало на sft_v13_21500, sft_v13_21500» читалось бы как
    #: повтор, а не как две разные строки таблицы.
    def name(p: dict) -> str:
        return f"{p['state']} (бюджет {p['budget']}, {p['source']})"
    up = [name(p) for p in per_state if (p["shift"] or 0) > 0]
    down = [name(p) for p in per_state if (p["shift"] or 0) < 0]
    if max_abs is None:
        reading = "нет чисел — правило не применено"
    elif max_abs < SIGNIFICANT:
        reading = (f"правило не меняет картину: сдвиг не более {max_abs:.4f} (порог "
                   f"{SIGNIFICANT}) — обрывы сидят в завершённых ходах, а не в усечённых")
    else:
        reading = (f"правило меняет число обрывов до {max_abs:.4f}: полное чтение "
                   f"завышало его на состояниях {', '.join(up) or '—'} и занижало на "
                   f"{', '.join(down) or '—'}; вердикт считается по обеим величинам")
    return {
        "rule": ("незакрытый <think> — дефект только среди ходов, завершившихся "
                 "естественно (stop_reason=turn_end); для усечённых показатель не определён"),
        "adr": "ADR-045 п.5",
        "in_instrument": True,
        "instrument_field": "aggregate.stop.natural.unclosed_think_share",
        "instrument_note": (
            "правило уже реализовано прибором (`stop_breakdown` → `natural`); правка "
            "tools/probe_language_split.py не делалась — она сломала бы тождество "
            "прибора с базой S3ap (проверяется хешем файла)"),
        "per_state": per_state,
        "max_abs_shift": nd(max_abs),
        "shifted_up_states": up,
        "shifted_down_states": down,
        "reading": reading,
        "caveat": ("выборка «только завершённые» меньше полной (у усечённых состояний — "
                   "на долю усечений), поэтому метрика по правилу шумнее; она идёт "
                   "рядом с полным чтением, а не вместо него"),
    }


# ─────────────────────────────── сборка ───────────────────────────────────────

def declared_inputs(report_8192: str | None = None,
                    report_4096: str | None = None) -> dict[str, dict]:
    """Объявленные входы свода: имя → путь и роль. Аргументы перекрывают константы.

    Пути читаются в момент вызова (а не при импорте): тесты подменяют их на
    фикстуры, не трогая дерево кейса, — но **состав** входов подменить нельзя.
    """
    g = globals()
    out = {name: {"path": g[var], "role": role} for name, (var, role) in INPUTS.items()}
    if report_8192:
        out["report_8192"]["path"] = report_8192
    if report_4096:
        out["report_4096_pair"]["path"] = report_4096
    return out


def absent_inputs(inputs: dict[str, dict]) -> list[dict]:
    """Отсутствующие входы — поимённо, с ролью: «чего нет» должно быть названо."""
    return [{"input": name, **spec} for name, spec in inputs.items()
            if not (CASE / spec["path"]).is_file()]


def artefact(rel: str, role: str | None = None) -> dict:
    """Файл как факт: есть/нет, размер, sha256. Отсутствие не прячется."""
    p = CASE / rel
    return {"path": rel, "role": role, "exists": p.is_file(),
            "bytes": p.stat().st_size if p.is_file() else None,
            "sha256": sha256_file(p) if p.is_file() else None}


def build(report_8192: str | None = None, report_4096: str | None = None) -> tuple[int, dict]:
    # ── Контракт входа: отсутствие любого объявленного входа → NOT-VERIFIED ───────
    inputs = declared_inputs(report_8192, report_4096)
    gone = absent_inputs(inputs)
    if gone:
        return EXIT_NOT_VERIFIED, {
            "error": ("нечего сводить: нет входов " + ", ".join(g["path"] for g in gone)),
            "missing_inputs": gone, "inputs": inputs}

    return _build(inputs)


def _build(inputs: dict[str, dict]) -> tuple[int, dict]:
    report_8192 = inputs["report_8192"]["path"]
    report_4096 = inputs["report_4096_pair"]["path"]
    rep82, rep41 = load(CASE / report_8192), load(CASE / report_4096)
    base = load(CASE / BASELINE_4096)
    #: Вход есть, но не разобран (битый JSON / не объект) — это тоже «нечего сводить»,
    #: и отличается от «входа нет» только причиной: причина называется.
    unreadable = [name for name, doc in (("report_8192", rep82), ("report_4096_pair", rep41),
                                         ("baseline_4096", base)) if doc is None]
    if unreadable:
        return EXIT_NOT_VERIFIED, {
            "error": ("нечего сводить: вход есть, но не разобран как JSON — "
                      + ", ".join(unreadable)),
            "unreadable_inputs": unreadable, "inputs": inputs}

    problems: list[str] = []

    # 1. Протокол: штатный режим, тот же набор промптов, остановка на конце хода.
    for label, rep, want in (("8192", rep82, 8192), ("4096-парный", rep41, 4096)):
        proto = dig(rep, ("protocol", "decoding_protocol"), {}) or {}
        if not proto.get("standard"):
            problems.append(f"{label}: замер не в штатном режиме: {proto.get('default_mode')}")
        if proto.get("legacy_decoding"):
            problems.append(f"{label}: замер снят с --legacy-decoding (прежний протокол)")
        if proto.get("allowed_for_conclusions") is False:
            problems.append(f"{label}: отчёт помечен allowed_for_conclusions=false")
        if dig(rep, ("protocol", "stop_at_turn_end")) is not True:
            problems.append(f"{label}: нет остановки на конце хода — обрыв мерил бы прибор")
        if dig(rep, ("protocol", "max_new_tokens")) != want:
            problems.append(f"{label}: бюджет {dig(rep, ('protocol', 'max_new_tokens'))} "
                            f"вместо {want}")
        for key in ("prompts_set", "prompts_digest"):
            if dig(rep, ("protocol", key)) != dig(base, ("protocol", key)):
                problems.append(f"{label}: {key} расходится с базой S3ap — числа "
                                f"несопоставимы")
        if dig(rep, ("protocol", "batch_size")) != dig(base, ("protocol", "batch_size")):
            problems.append(f"{label}: batch_size расходится с базой S3ap "
                            "(различие протокола, а не бюджета)")

    # 2. Тождество прибора: хеш файла + побайтовое ядро (S3ai).
    tool_sha = sha256_file(CASE / "tools/probe_language_split.py")
    ref_sha = base.get("tool_sha256")
    ref_path = inputs["identity_reference"]["path"]
    identity_core = {"reference": ref_path, "checked": False}
    id_new, id_ref = load(CASE / inputs["identity_core"]["path"]), load(CASE / ref_path)
    if id_new and id_ref:
        p_new = dig(id_new, ("states", "cfinal", "probes"), [])
        p_old = dig(id_ref, ("states", "cfinal", "probes"), [])
        same = len(p_new) == len(p_old) > 0 and all(
            a.get("response") == b.get("response") for a, b in zip(p_new, p_old))
        identity_core.update({"checked": True, "n": len(p_new), "byte_identical": bool(same)})
        if not same:
            problems.append("тождество прибора нарушено: ядро проб не совпало "
                            "побайтово с S3ai")
    else:
        problems.append("нет файлов гейта тождества ядра — тождество прибора не проверено")
    if tool_sha != ref_sha:
        problems.append("файл прибора изменился относительно базы S3ap: числа "
                        "несопоставимы (правило ADR-045 п.5 уже в приборе — правка "
                        "не требуется)")

    # 3. Состояния и n.
    rows82, missing82 = collect(rep82)
    rows41, missing41 = collect(rep41)
    if not rows82:
        return EXIT_NOT_VERIFIED, {"error": "в отчёте 8192 нет измеренных состояний"}
    for r in rows82 + rows41:
        if (r.get("n") or 0) < MIN_N:
            problems.append(f"состояние {r['state']} (бюджет {r['budget']}): "
                            f"n={r.get('n')} < {MIN_N}")
    n_prompts = dig(rep82, ("protocol", "n_prompts"))
    if (n_prompts or 0) < MIN_N:
        problems.append(f"проб в наборе {n_prompts} < {MIN_N}")

    cpt82 = next((r for r in rows82 if r["state"] == "cfinal"), None)
    fresh82 = fresh_row(rows82)
    fresh41 = next((r for r in rows41 if r["state"] == (fresh82 or {}).get("state")), None)
    if cpt82 is None:
        problems.append("в прогоне 8192 нет состояния cfinal — уровень CPT не измерен")
    if fresh82 is None:
        problems.append("в прогоне 8192 нет ни одной точки v13")
    if fresh82 is not None and fresh41 is None:
        problems.append(f"нет парного замера 4096 для состояния {fresh82['state']} — "
                        "различение «то же состояние, два бюджета» не построить")

    # 4. Числа вердикта обязаны существовать: без них вердикт был бы словом.
    if fresh82 is not None and fresh82.get("unclosed_think") is None:
        problems.append("у свежей точки 8192 нет числа обрывов — вердикт не на чем строить")
    if fresh82 is not None and fresh82.get("answer_coverage") is None:
        problems.append("у свежей точки 8192 нет покрытия ответа")

    verdict = make_verdict(fresh82 or {}, cpt82, fresh41)

    # 5. Парное сравнение — самая сильная форма различения.
    paired = {"available": bool(fresh82 and fresh41), "state": (fresh82 or {}).get("state")}
    if fresh82 and fresh41:
        paired["per_metric"] = {
            metric: {"budget_4096": fresh41.get(metric), "budget_8192": fresh82.get(metric),
                     **delta_row(fresh41.get(metric), fresh82.get(metric), metric)}
            for metric in ("unclosed_think", "answer_coverage", "truncated",
                           "median_len", "tool_call", "unclosed_think_natural")
        }
        #: Различение «усечение против деградации» одной парой «то же состояние, два
        #: бюджета»: если вдвое больший бюджет не сдвигает обрывы за порог значимости,
        #: механизм «обрывы — артефакт бюджета» этим состоянием не подтверждается.
        d_uncl = paired["per_metric"]["unclosed_think"]["delta"]
        paired["unclosed_delta"] = d_uncl
        paired["budget_explains_unclosed"] = bool(
            d_uncl is not None and d_uncl < 0 and abs(d_uncl) >= SIGNIFICANT)
        paired["reading"] = (
            "на том же чекпойнте рост бюджета %s обрывы (%s → %s), покрытие (%s → %s); %s"
            % ("снизил" if (fresh82.get("unclosed_think") or 0)
               < (fresh41.get("unclosed_think") or 0) else "НЕ снизил",
               fresh41.get("unclosed_think"), fresh82.get("unclosed_think"),
               fresh41.get("answer_coverage"), fresh82.get("answer_coverage"),
               ("сдвиг ниже порога значимости %s — бюджет обрывы НЕ объясняет, "
                "число читается как свойство модели" % SIGNIFICANT
                if d_uncl is not None and abs(d_uncl) < SIGNIFICANT else
                "сдвиг значим — различие бюджетов объясняет часть обрывов")))
        #: Судьба ходов, зависящих от бюджета, — то, чего агрегат не показывает.
        paired["probe_fate"] = probe_fate(rep82, rep41, paired["state"])

    # 6. Применение правила п.5 к обоим бюджетам (и к базе S3ap, где состояние совпало).
    base_rows, _ = collect(base)
    base_by_budget = {(r["state"], r["budget"]): r for r in base_rows}
    pairs = [("замер 8192", fresh82)] if fresh82 else []
    if cpt82:
        pairs.append(("замер 8192", cpt82))
    if fresh41:
        pairs.append(("парный замер 4096 (те же сутки)", fresh41))
    same_state = base_by_budget.get(((fresh82 or {}).get("state"), 4096))
    if same_state:
        pairs.append(("S3ap 19.09.2026 (бюджет 4096)", same_state))
    rule_applied = unclosed_rule_applied(pairs)

    growth = length_growth(rows82, rows41, base_rows)

    # 7. Сводная таблица «состояние × бюджет × метрики».
    table = []
    for r in rows82:
        table.append({**{k: r.get(k) for k in METRIC_PATHS}, "state": r["state"],
                      "budget": 8192, "n": r.get("n"), "source": "S3aq",
                      "n_natural": r.get("n_natural"),
                      "unclosed_think_natural": r.get("unclosed_think_natural"),
                      "share_at_budget": r.get("share_at_budget"),
                      "share_longer_than_4096": r.get("share_longer_than_4096"),
                      "checkpoint": r.get("checkpoint")})
    for key in sorted(base_by_budget):
        r = base_by_budget[key]
        table.append({**{k: r.get(k) for k in METRIC_PATHS}, "state": r["state"],
                      "budget": 4096, "n": r.get("n"), "source": "S3ap (19.09.2026)",
                      "n_natural": r.get("n_natural"),
                      "unclosed_think_natural": r.get("unclosed_think_natural"),
                      "share_at_budget": r.get("share_at_budget"),
                      "share_longer_than_4096": r.get("share_longer_than_4096"),
                      "checkpoint": r.get("checkpoint")})
    for r in rows41:
        table.append({**{k: r.get(k) for k in METRIC_PATHS}, "state": r["state"],
                      "budget": 4096, "n": r.get("n"), "source": "S3aq (парный контроль)",
                      "n_natural": r.get("n_natural"),
                      "unclosed_think_natural": r.get("unclosed_think_natural"),
                      "share_at_budget": r.get("share_at_budget"),
                      "share_longer_than_4096": r.get("share_longer_than_4096"),
                      "checkpoint": r.get("checkpoint")})
    for m in missing82:
        table.append({"state": m["state"], "budget": 8192, "source": "S3aq",
                      "unavailable": True, "reason": m["reason"],
                      "checkpoint": m["checkpoint"], "n": None})
    table.sort(key=lambda r: (0 if r["state"] == "cfinal" else 1, r["state"], r["budget"] or 0))

    pin = load(CASE / inputs["pin_checkpoints"]["path"]) or {}
    #: Артефакты записываются **фактом** (есть/нет, размер, sha256), а не списком имён:
    #: объявленный, но не созданный файл обязан быть виден как отсутствующий
    #: (ADR-028 п.1 — «факт первичен»), иначе список артефактов читается как «всё есть».
    artifacts = {
        name: artefact(spec["path"], spec["role"]) for name, spec in inputs.items()}
    artifacts.update({
        "chain": artefact(RUN + "/chain.sh", "цепочка замера (объявленный протокол)"),
        "run_manifest": artefact(RUN + "/run_manifest.json", "манифест прогона (AD-2)"),
        "device_check": artefact(RUN + "/device_check.json", "устройство: аллокация CUDA"),
        "probe_record": artefact(RUN + "/probe_record.json",
                                 "запись прогона (пишет step 1 цепочки chain.sh)"),
        #: Снимок рабочего списка пиннинга (он живёт вне репозитория, в /home/user/s3aq-ckpts):
        #: в дереве остаётся копия, побайтово равная источнику, — иначе факт пиннинга
        #: нечем проверить из git. Побайтовое равенство проверяется сверкой sha256.
        "pin_manifest_snapshot": artefact(
            RUN + "/PIN.sha256",
            "снимок списка пиннинга; строка sft_probe_10000.pt помечена removed_by_retention "
            "(ADR-028 п.1: факт первичен — файла нет, строка приведена к факту)"),
        "tool": artefact("tools/probe_language_split.py", "прибор"),
    })
    #: Отсутствие probe_record.json — факт с причиной, а не пропуск: файл пишет шаг
    #: цепочки chain.sh, а решающий прогон добран отделённой сессией (sidecar
    #: resume_confirmed.json), шаги которой запись прогона не делают.
    if not artifacts["probe_record"]["exists"] and (CASE / (RUN + "/resume_confirmed.json")).is_file():
        artifacts["probe_record"]["why_absent"] = (
            "решающий прогон добран отделённой сессией (sidecar "
            "runs/s3aq-budget-8192-20260920/resume_confirmed.json), а probe_record.json "
            "пишет шаг 1 цепочки chain.sh — при доборе он не выполнялся")
    #: Манифест AD-2 лежит в каталоге один и перезаписывается на каждый прогон: если
    #: он описывает не решающий отчёт, это называется фактом, а не умалчивается —
    #: иначе «манифест прогона» читался бы как манифест именно решающего этапа.
    mf = load(CASE / (RUN + "/run_manifest.json")) or {}
    if artifacts["run_manifest"]["exists"] and mf.get("probe_run") not in (None, report_8192):
        artifacts["run_manifest"]["covers"] = mf.get("probe_run")
        artifacts["run_manifest"]["note"] = (
            f"манифест описывает последний прогон каталога ({mf.get('probe_run')}), а не "
            f"решающий ({report_8192}): файл один на каталог и перезаписывается каждым "
            "прогоном — решающий этап своего манифеста не оставил")
    facts = dict(artifacts)                      # до добавления сводных ключей
    artifacts["sha256"] = {name: rec["sha256"] for name, rec in facts.items()}
    artifacts["missing"] = [name for name, rec in facts.items() if not rec["exists"]]

    doc = {
        "schema": "s3aq-budget-8192/1",
        "tool": "tools/assemble_s3aq_evidence.py",
        "stage": "S3aq",
        "title": ("Различающий замер бюджета: 8192 против 4096 — усечение растущих "
                  "рассуждений или деградация обучения"),
        "date": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if not problems else "partial",
        "problem": ("замер S3ap при бюджете 4096 дал обрывы 0.76 и покрытие 0.33 при "
                    "медиане хода, упёршейся в бюджет; различить «усечение выросших "
                    "рассуждений» и «деградацию обучения» можно только бюджетом выше "
                    "медианы (ADR-045 п.2)"),
        "artifacts": artifacts,
        "framework": {
            "tool": "tools/probe_language_split.py",
            "tool_sha256": tool_sha,
            "mode": ("штатный (greedy + запрет повторов 4-грамм, ADR-041); "
                     "--legacy-decoding не использовался"),
            "n_per_state": n_prompts,
            "prompts_set": dig(rep82, ("protocol", "prompts_set")),
            "batch_size": dig(rep82, ("protocol", "batch_size")),
            "stop_at_turn_end": dig(rep82, ("protocol", "stop_at_turn_end")),
            "device": rep82.get("device"),
            "where": ("локальная RTX 4080 SUPER (стенд GB10 занят стадией SFT — AD-5); "
                      "чекпойнты пиннуются локально из-за ретенции прогона"),
            "decoding_protocol": dig(rep82, ("protocol", "decoding_protocol")),
        },
        "instrument_identity": {
            "tool_sha256_current": tool_sha,
            "tool_sha256_at_baseline": ref_sha,
            "same_tool_file": tool_sha == ref_sha,
            "core_byte_identity": identity_core,
            "checkpoint_pinning": pin,
            "reading": ("прибор тот же: файл совпал по хешу и ядро проб совпало "
                        "побайтово — числа 8192 и 4096 сопоставимы"
                        if (tool_sha == ref_sha and identity_core.get("byte_identical"))
                        else "тождество прибора НЕ подтверждено — числа несопоставимы"),
        },
        "table": table,
        "budget_8192": {
            "source": report_8192,
            "max_new_tokens": dig(rep82, ("protocol", "max_new_tokens")),
            "states": rows82,
            "unavailable": missing82,
        },
        "budget_4096": {
            "baseline_source": BASELINE_4096,
            "pair_source": report_4096,
            "states_baseline": base_rows,
            "states_paired": rows41,
            "unavailable": missing41,
            "how": ("база S3ap (19.09.2026, те же прибор/режим/набор) плюс парный "
                    "контроль в те же сутки на свежем чекпойнте: парное сравнение не "
                    "опирается ни на соседние шаги, ни на дрейф окружения"),
        },
        "paired_comparison": paired,
        "verdict": verdict,
        "length_growth": growth,
        "unclosed_rule_applied": rule_applied,
        "measurement_cost": measurement_cost(rep82, rep41),
        "unavailable_states": {
            "states": missing82,
            "why": ("точки v13@500/5000/9000, снятые S3ap на 4096, удалены ретенцией "
                    "прогона SFT (PROBE_KEEP_LAST = 24 пробы: держатся последние 24, "
                    "более старые стираются при записи новых). Перемерять их на 8192 "
                    "нечем — файлов нет"),
            "consequence": ("таблица «состояние × бюджет» полна там, где состояние "
                            "измеримо; для 5000/9000 столбца 8192 нет, и он не "
                            "заменяется другим шагом: подмена сдвинула бы вывод"),
        },
        "answers": {
            "a_obryvy_8192": verdict["unclosed_8192"],
            "a_obryvy_8192_cpt": verdict["unclosed_8192_cpt"],
            "b_coverage_8192": verdict["coverage_8192"],
            "b_coverage_restored_to_cpt": verdict["coverage_restored_to_cpt"],
            "c_zone": verdict["rule_zone"],
            "d_length_growth": growth["reading"],
            "e_rule_natural": rule_applied["reading"],
            "f_zone_by_rule": verdict["rule_zone"],
            "f_zone_by_rule_natural": verdict["rule_zone_natural"],
            "f_zones_agree": verdict["rule_zones_agree"],
        },
        "open_questions": [
            "Точки v13@500/5000/9000 утрачены ретенцией: полного «состояние × бюджет» "
            "по ним не будет. Нужно ли менять протокол проб так, чтобы нужные точки "
            "пиннились до прогона (сейчас пиннинг делает цепочка замера — задним "
            "числом это не помогает).",
            "Манифест AD-2 в каталоге один и описывает последний прогон (парный контроль "
            "4096), а решающий этап 8192 своего манифеста не оставил: нужен ли манифест "
            "на КАЖДЫЙ этап каталога (или один сводный), чтобы факт прогона не терялся "
            "при доборе?",
            "Чтения расходятся (полное 0.5865 → деградация, по п.5 0.3793 → промежуточная "
            "зона): какой ряд владелец считает показателем дефекта? Расхождение — не "
            "точность, а определение метрики, и домером оно не закрывается: 0.3793 стоит "
            "в 0.46σ от границы 0.35, то есть для отделения нужно n порядка тысячи.",
            "32 из 104 ходов (0.3077) упираются в лимит **внутри tool_call**, причём "
            "доля одна и та же при 4096 и 8192 — то есть от бюджета не зависит: "
            "считать ли незавершённый вызов "
            "инструмента отдельным дефектом формата/агентности, независимым от обрывов "
            "`<think>`? Сейчас он попадает в общую долю усечений.",
            "Если вердикт — «усечение»: рост длины рассуждений остаётся открытой "
            "проблемой. Что выбираем — нормирование длины в данных, предел длины в "
            "обучении или бюджет инференса ≥ 8192 как норма (ADR-045 п.3)?",
            "Порог значимости 0.10 (≈2σ при n = 104) объявлен в своде; различия "
            "5–10 п.п. остаются неразличимыми, и это ограничение метода, а не "
            "результат замера.",
        ],
        "assumptions": [
            "Числа бюджета 4096 там, где состояние не перемерялось, берутся из S3ap "
            "(те же прибор, режим, набор промптов, batch 8) — это нормативная база, "
            "а не новая база критерия.",
            "Парный контроль 4096 снят в те же сутки на том же чекпойнте, что и 8192: "
            "именно он, а не соседние шаги, отвечает на вопрос «снял ли бюджет обрывы».",
            "Свежая точка выбирается правилом «устоявшийся файл» (mtime > 180 с, размер "
            "не меняется 20 с) и пиннуется локально: прогон SFT пишет и удаляет "
            "чекпойнты прямо во время замера.",
            "Правило ADR-045 п.5 уже реализовано прибором (aggregate.stop.natural) — "
            "файл прибора не правился, иначе сломалось бы тождество с базой S3ap.",
            "Вердикт считается по тому же ряду, на котором правило ADR-045 п.2 объявлено "
            "до замера: порог 0.35 назван по уровню CPT из таблицы ADR-045 (0.3365) — "
            "это полное чтение (все ходы). Число по правилу п.5 (только естественно "
            "завершённые ходы) приводится рядом второй зоной; при расхождении зон "
            "названы обе, а не выбрана удобная — ряд не подменяется после того, как "
            "число стало известно.",
            "Строка sft_probe_10000.pt в списке пиннинга приведена к факту (файла нет — "
            "ретенция прогона SFT) пометкой removed_by_retention, а не удалением: "
            "ADR-028 п.1 — факт первичен, след сохраняется. Снимок списка лежит в "
            "каталоге прогона (pin_manifest_snapshot), sha256 сверяется с источником.",
        ],
        "problems": problems,
    }
    return (EXIT_FAIL if problems else EXIT_OK), doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=EVIDENCE, help=f"куда писать свод (по умолчанию {EVIDENCE})")
    ap.add_argument("--report-8192", default=REPORT_8192, help="отчёт решающего прогона")
    ap.add_argument("--report-4096", default=REPORT_4096_PAIR, help="отчёт парного контроля")
    ap.add_argument("--print", action="store_true", help="напечатать свод в stdout")
    args = ap.parse_args()

    rc, doc = build(args.report_8192, args.report_4096)
    if rc == EXIT_NOT_VERIFIED:
        note(f"NOT-VERIFIED: {doc.get('error')}")
        return rc
    out = CASE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.print:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    note(f"свод: {args.out} (status={doc['status']}, проблем: {len(doc['problems'])})")
    for p in doc["problems"]:
        note(f"  ! {p}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
