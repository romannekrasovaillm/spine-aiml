#!/usr/bin/env python3
"""decode-diagnosis — разбор: убегающая длина и незавершённые вызовы это свойство
весов или артефакт протокола декодирования.

**Вопрос дельты.** Стадия SFT остановлена (ADR-050) с решением перезапустить её с
контролем длины рассуждений. Основание — замер S3aq на пиннутом чекпойнте
`sft_probe_21500.pt` (бюджет 8192, штатный режим): медиана хода 1027, p90 = 8192,
32 из 104 ходов (0.3077) кончаются внутри **незавершённого** `tool_call`, и в этих
ходах 43.8 % циклов и 78 % повторов 4-грамм. Профиль набора решение опроверг: в
данных длинных целей нет. Значит вопрос не в длине данных, а в том, **где дефект**:
в весах (чекпойнт обучен писать всё длиннее и не доводить вызов) или в протоколе
декодирования (greedy при запрете повторов 4-грамм не может закрыть блок и уходит
в переписывание).

**Как различается.** Один чекпойнт, один набор (104 промпта), один бюджет, три
режима. V1 — штатный (greedy + запрет 4-грамм, ADR-041), он же контроль тождества
против S3aq. V2 — greedy + repetition_penalty 1.15, запрет 4-грамм снят. V3 —
sample T=0.7 top_p=0.9 rp1.1.

**Исторический контроль вместо перезамера (`--historical-control`).** Живой луч V1
— это контроль тождества, и он же самый дорогой: он обязан быть побайтово равен
решающему отчёту S3aq. На медленном хосте он не доехал: прогон снят после первой
восьмёрки проб, отчёта `v1_greedy_nogram4.json` нет, есть только журнал прибора.
Гонять его заново — это ещё ≈1 ч на число, которое **обязано** совпасть (greedy
детерминирован), то есть час на подтверждение того, что иначе не может быть.

Поэтому флаг разрешает взять контроль **историческим**: роль V1 играет состояние
`sft_v13_21500` самого решающего отчёта S3aq (тот же чекпойнт, тот же прибор, тот
же набор, тот же бюджет, то же декодирование — `greedy_nogram4`). Подстановка
**не молчаливая**: она возможна только при флаге, требует журнала оборванного
прогона и даёт вместо побайтового контроля **частичный** — сверку тех проб, о
которых журнал успел сообщить (`partial_identity_control`). Сверяются четыре
поля: тег, длина в токенах, `stop_reason` и `cyr_think`. Токеновые длины
(154, 44, 492, 451 на `turn_end`) — отпечаток, который смена чекпойнта или
прибора сдвинула бы; совпадение всех сверенных проб и есть измеренная часть
тождества. Неизмеренная часть (пробы за пределами журнала) **названа числом** и
опирается на аргумент, а не на молчание: при greedy расхождение невозможно без
смены входа. Это **слабее** побайтового контроля, и именно так и помечено —
`control_mode.kind = historical_s3aq_report`, а не `live_v1_report`.

**Определения — импортом из прибора, а не копией.** Свод берёт `aggregate()` из
`tools/probe_language_split.py` (того же файла, чей хеш проверен против S3aq) и
вызывает его на пробах отчёта. Тем самым «доля незакрытых `<think>`», «доля
упёршихся в бюджет», «медиана/p90», «доля петель» считаются ровно той арифметикой,
что и в решающем отчёте, — совпадение определений доказывается вызовом, а не
сравнением текстов. Сверх прибора свод считает только то, чего в нём нет:

* ``p99`` — тем же правилом ближайшего ранга (прибор печатает медиану и p90);
* ``tool_call_unfinished`` — доля ходов с ``stop_reason = limit_in_tool_call``
  (индикатор ADR-050 п.3: критерий стадии «незавершённый вызов ≤ 5 %»);
* ``tc_looped`` / ``tc_repeat4`` — циклы и повторы 4-грамм **среди** этих ходов
  (числа 43.8 % и 78 % из контекста дельты — отсюда);
* ``json_valid`` — валидность JSON вызова **определением пайплайна v8**
  (``execute_tool_call``: регексп ``<tool_call>\\s*(\\{.*?\\})\\s*(?:</tool_call>)?``
  с DOTALL, затем ``json.loads``). Регексп сверяется с исходником пайплайна
  (строка обязана там присутствовать), чтобы «то же определение» было проверено,
  а не заявлено.

**Правило ответа объявлено ДО разбора (константы ниже) и не уточняется после.**
Индикатор первичный — доля ходов, кончающихся внутри незавершённого ``tool_call``
(у CPT-финала 0.0, у штатного режима 0.3077; критерий ADR-050 п.3 — ≤ 0.05).

=================================  ===============================================
исход по альтернативным режимам    чтение при первичном индикаторе
(V2 и V3)                          (``answer.verdict``)
=================================  ===============================================
во всех > 0.05 и ни в одном        ``weights_property``: убегание **сохраняется**
падение не значимо                 при смене декодирования ⇒ свойство весов,
                                   протокол его не создаёт
хотя бы в одном ≤ 0.05 и падение   ``protocol_artifact``: убегание **исчезает** ⇒
значимо против V1                  артефакт протокола, лечится режимом, а не данными
иначе                              ``insufficient_data``: назвать, что домерить
=================================  ===============================================

Значимость даётся **двумя** способами, и оба печатаются: парный точный тест
Мак-Немара (промпты по индексам совпадают — пары настоящие, тест мощнее) и
двухдолевой z без пар (консервативнее). Порог по величине — 0.10 п.п.: тот же,
что уже объявлен в своде S3aq при n = 104 (``SIGNIFICANT`` в
``assemble_s3aq_evidence.py``), — чтобы «значимо» в двух сводах означало одно.

**Чего этот разбор НЕ решает** — отдельным полем ``not_decided`` (TASK п.4).

Коды возврата::

    0 — разбор собран, контракт входа соблюдён
    1 — отказ: вход противоречит контракту (не тот чекпойнт/прибор/набор/бюджет,
        n ≠ 104, режим не тот, который объявлен, тождество V1 с S3aq нарушено)
    2 — NOT-VERIFIED: нечего разбирать (нет отчётов режимов)
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

import probe_language_split as LS  # noqa: E402  (арифметика прибора — импортом)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

RUN = "runs/sft-decode-diagnosis-20260920"
REFERENCE = RUN + "/reference/s3aq_format_wide_8192.json"
#: Журнал прибора оборванного луча V1 — единственная измеренная связь этой сессии
#: с решающим отчётом: без него исторический контроль был бы чистым утверждением.
V1_LOG = RUN + "/v1_greedy_nogram4.log"
PIPELINE = "laguna_pipeline_v8.py"
OUT = RUN + "/decode_modes_analysis.json"
EVIDENCE = "evidence/sft-decode-diagnosis.json"

#: Состояние замера — одно на все три режима (иначе разница режимов несла бы ещё
#: и разницу чекпойнта).
STATE = "sft_v13_21500"
PROMPTS_SET = "wide"
N_MIN = 104
BUDGET = 8192

#: Тождество входов: те же значения, что проверяет цепочка прогона (chain.sh 0a/0b).
CKPT_SHA = "72bcbe76a8bc93ba0cb796abebc0b7a4955c1f99a81400e106bd19091531b360"
TOOL_SHA = "99dafa8d9551caaa57b770d4cd321da6f0c09b28deb1885500034c9d62eeb276"

#: Режимы: имя → (файл, ожидаемое имя режима в отчёте, роль). Ожидаемое имя —
#: из самих параметров (`decoding_label` прибора), поэтому подделать его флагом
#: нельзя; расхождение — отказ, а не «режим с другим именем».
MODES = {
    "V1": {"stem": "v1_greedy_nogram4", "decoding": "greedy_nogram4",
           "role": "штатный профиль ADR-041 — контроль тождества против S3aq"},
    "V2": {"stem": "v2_greedy_rp115", "decoding": "greedy_rp1.15",
           "role": "greedy + repetition_penalty 1.15, запрет 4-грамм снят"},
    "V3": {"stem": "v3_sample_t07_p09_rp11", "decoding": "sample_T0.7_top_p0.9_rp1.1_nogram4",
           "role": "sample T=0.7 top_p=0.9 rp1.1 (запрет 4-грамм оставлен)"},
}

#: ── Правило ответа, объявленное ДО разбора ────────────────────────────────────
#: 0.05 — критерий стадии из ADR-050 п.3 («доля ходов, кончающихся внутри
#: незавершённого tool_call ≤ 5 %, у CPT 0»). Взят оттуда, а не подобран: это уже
#: принятый порог, и он объявлен раньше этого разбора.
RUNAWAY_MAX = 0.05
#: Порог значимости по величине при n = 104 — как в своде S3aq (SIGNIFICANT =
#: 0.10), чтобы «значимо» не значило в двух сводах разное.
SIG_PP = 0.10
#: Порог значимости по тесту (парный точный Мак-Немар / двухдолевой z).
SIG_P = 0.05

#: ── Источник контроля ─────────────────────────────────────────────────────────
#: `live_v1_report` — побайтовый контроль: отчёт луча V1 снят этой сессией, и он
#: обязан совпасть с решающим отчётом S3aq. `historical_s3aq_report` — контроль
#: исторический: роль V1 играет сам решающий отчёт, а тождество подтверждается
#: частично (`partial_identity_control`) по журналу оборванного прогона.
CONTROL_LIVE = "live_v1_report"
CONTROL_HISTORICAL = "historical_s3aq_report"

#: Строка пробы в журнале прибора (`probe_language_split`: `note(f"  [{tag}/{mode}] ...")`).
#: Разбор журнала — копия формата, поэтому ниже есть проверка, что формат взят из
#: исходника прибора (`log_format_check`), а не из памяти: иначе смена формата
#: журнала превратила бы «0 сверенных проб» в «0 расхождений».
PROBE_LOG_RE = re.compile(
    r"\[(?P<state>[^/\]]+)/(?P<mode>[^\]]+)\] (?P<tag>\w+): "
    r"cyr_think=(?P<cyr_think>\S+) cyr_answer=(?P<cyr_answer>\S+) "
    r"\(v1 (?P<cyr_answer_v1>\S+)\) mode=(?P<mode_flags>\S+) "
    r"tok=(?P<tok>\d+) stop=(?P<stop>\w+)"
    r"(?P<limit> \(ЛИМИТ\))?(?P<looped> ЗАЦИКЛ)? \| ")

#: Определение JSON-валидности вызова — регексп пайплайна v8 (`execute_tool_call`).
#: Копия объявлена, а сверка с исходником — ниже (`pipeline_definition_check`):
#: определение, скопированное без проверки, разошлось бы с источником молча.
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>)?", re.DOTALL)

NOT_DECIDED = (
    "не решает, ЧТО именно чинить в данных: длина, дубликаты (57.57 % при "
    "effective_passes 7.07) или то и другое — политика длины в этот замер не входит",
    "не отделяет вклад дубликатов и эффективных проходов: набор с повторами и "
    "набор без них в этот замер не входят, поэтому «убегание от данных» и "
    "«убегание от кратности проходов» здесь не разведены",
    "не отвечает, лечится ли убегание обучением меньшей длительности: замер "
    "меняет только инференс, обучение на этих режимах не шло, и вариант "
    "«дообучить на коротких целях» не проверялся",
    "не проверяет вариант «только штраф за повторы»: V2 менял два фактора сразу "
    "(снятие запрета 4-грамм и repetition_penalty 1.15), а луч с одним штрафом "
    "при сохранённом запрете не гонялся — вклад штрафа отдельно не измерен",
    "не решает, воспроизводится ли найденный эффект на других шагах обучения: "
    "замер стоит на одной точке (21500), траектория по шагам не снята",
    "не решает, что будет на CPT-финале и на других чекпойнтах: сравнение внутри "
    "одного состояния, а не между состояниями",
    "не даёт вердикт стадии по формату и языку в смысле ADR-045/ADR-041: числа "
    "альтернативных режимов для этого непригодны по построению (V2 несёт "
    "allowed_for_conclusions=false, V3 — не штатный режим), и пороги не двигаются",
    "не решает, каким режимом мерить стадию дальше: это смена протокола (ADR-041), "
    "и решение принимает архитектор, а не замер",
    "не решает, тождественны ли веса «своему» протоколу обучения: замер меняет "
    "только инференс, обучение на этих режимах не шло",
)


def note(msg: str) -> None:
    print(msg, file=sys.stderr)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load(p: Path | str) -> dict | None:
    path = Path(p)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def dig(d: dict, path: tuple[str, ...], default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def pct(values: list[int], q: float) -> int | None:
    """Перцентиль ближайшим рангом — правило прибора (``_pct``), не интерполяция.

    Скопировано из прибора сознательно: прибор печатает медиану и p90, а p99 в
    нём нет. Копия правила, а не вызов, потому что вызывать нечего; тождество
    проверяется ниже сверкой медианы и p90 с тем, что посчитал прибор.
    """
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(q * len(s)))
    return s[k - 1]


# ── значимость: два теста, оба печатаются ─────────────────────────────────────

def round_p(x: float) -> float:
    """p-значение без потери порядка: 4.66e-10, а не 0.0.

    Округление до четырёх знаков превращало бы «p < 10⁻⁹» в «p = 0.0» — то есть
    в утверждение, которого данные не дают (нулевая вероятность не наблюдается,
    наблюдается малое число). Для малых p сохраняются три значащие цифры.
    """
    return float(f"{x:.3g}") if x < 1e-4 else round(x, 4)


def _binom_two_sided(k: int, n: int) -> float:
    """Точный двусторонний биномиальный тест при p = 0.5 (для знаков и Мак-Немара).

    Считается суммой хвостов, а не нормальной аппроксимацией: n мал (десятки), и
    аппроксимация на таких n даёт p, отличающийся в разы — то есть решала бы
    «значимо/не значимо» арифметика, а не данные.
    """
    if n == 0:
        return 1.0
    probs = [math.comb(n, i) for i in range(n + 1)]
    total = float(sum(probs))
    lo = min(k, n - k)
    tail = float(sum(probs[i] for i in range(lo + 1)))
    return min(1.0, 2.0 * tail / total)


def mcnemar_exact(base_flags: list[bool], mode_flags: list[bool]) -> dict:
    """Парный точный тест Мак-Немара: промпты по индексам совпадают, пары настоящие.

    Учитываются только расхождения (``base_only`` и ``mode_only``): согласия не
    несут информации о разнице режимов. Тест мощнее непарного z ровно потому, что
    выбрасывает общий для обоих режимов «фон».
    """
    a = sum(1 for b, m in zip(base_flags, mode_flags) if b and not m)
    b_ = sum(1 for b, m in zip(base_flags, mode_flags) if m and not b)
    both = sum(1 for b, m in zip(base_flags, mode_flags) if b and m)
    neither = len(base_flags) - a - b_ - both
    return {"base_only": a, "mode_only": b_, "both": both, "neither": neither,
            "n_discordant": a + b_,
            "p_exact": round_p(_binom_two_sided(min(a, b_), a + b_))}


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> dict:
    """Двухдолевой z без пар — консервативное чтение (пары не используются)."""
    if not n1 or not n2:
        return {"z": None, "p": None}
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return {"z": 0.0, "p": 1.0}
    z = (p2 - p1) / se
    return {"z": round(z, 4), "p": round_p(math.erfc(abs(z) / math.sqrt(2)))}


def sign_test(diffs: list[int]) -> dict:
    """Знаковый тест на парных разностях длин: точный биномиальный на знаках."""
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    zero = sum(1 for d in diffs if d == 0)
    return {"plus": pos, "minus": neg, "zero": zero, "n_nonzero": pos + neg,
            "p_exact": round_p(_binom_two_sided(min(pos, neg), pos + neg))}


# ── метрики режима ────────────────────────────────────────────────────────────

def probe_flags(probes: list[dict]) -> dict:
    """Булевы ряды по пробам — то, на чём считаются доли и парные тесты."""
    def _m(p: dict) -> dict:
        return p.get("metrics") or {}

    return {
        "unfinished_tool_call": [p.get("stop_reason") == "limit_in_tool_call" for p in probes],
        "budget_hit": [p.get("stop_reason") != LS.STOP_TURN_END for p in probes],
        "unclosed_think": [bool(_m(p).get("unclosed_think")) for p in probes],
        "looped": [bool(_m(p).get("looped")) for p in probes],
        "repeat4": [(_m(p).get("max4gram_rep") or 0) >= 2 for p in probes],
        "repeat4_defined": [_m(p).get("max4gram_rep") is not None for p in probes],
    }


def json_validity(probes: list[dict]) -> dict:
    """Доля JSON-валидных вызовов — определением пайплайна v8.

    Три знаменателя, и все три печатаются, потому что вопрос «доля чего» здесь
    содержательный: (а) среди ходов, где вызов **есть** (`has_tool_call`); (б)
    среди **всех** ходов (ход без вызова — не валидный вызов, а его отсутствие);
    (в) среди ходов, кончающихся внутри незавершённого вызова, — там валидность
    особенно важна: незакрытый блок либо не парсится, либо парсится, но обрезан.

    Основное чтение — (а) по **первому ходу** модели (текст до первого
    ``<|im_end|>``): агентный цикл видит ход, а не продолжение после ответа
    (правило 5 прибора). Вариант по всей генерации печатается рядом как проверка
    устойчивости чтения.
    """
    rows = []
    for p in probes:
        first = LS.first_turn(p["response"])
        rows.append({
            "has_call": bool(p["metrics"].get("has_tool_call")),
            "first_turn_match": bool(TOOL_CALL_RE.search(first)),
            "first_turn_json": _json_ok(first),
            "full_match": bool(TOOL_CALL_RE.search(p["response"])),
            "full_json": _json_ok(p["response"]),
            "unfinished": p.get("stop_reason") == "limit_in_tool_call",
        })

    def _share(sub, key):
        if not sub:
            return {"n": 0}
        k = sum(1 for r in sub if r[key])
        return {"n": len(sub), "valid": k, "share": round(k / len(sub), 4)}

    with_call = [r for r in rows if r["has_call"]]
    unfinished = [r for r in rows if r["unfinished"]]
    return {
        "definition": ("регексп <tool_call>\\s*(\\{.*?\\})\\s*(?:</tool_call>)? (DOTALL) + "
                       "json.loads — копия laguna_pipeline_v8.execute_tool_call"),
        "primary_region": "первый ход модели (до первого <|im_end|>)",
        "with_tool_call": _share(with_call, "first_turn_json"),
        "all_probes": _share(rows, "first_turn_json"),
        "unfinished_tool_call": _share(unfinished, "first_turn_json"),
        "full_generation_variant": {
            "with_tool_call": _share(with_call, "full_json"),
            "all_probes": _share(rows, "full_json"),
        },
        "call_block_found": {
            "with_tool_call": _share(with_call, "first_turn_match"),
            "all_probes": _share(rows, "first_turn_match"),
        },
    }


def _json_ok(text: str) -> bool:
    m = TOOL_CALL_RE.search(text)
    if not m:
        return False
    try:
        obj = json.loads(m.group(1))
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict)


def pipeline_definition_check(case: Path = CASE) -> dict:
    """Регексп свода присутствует в исходнике пайплайна — определение то же.

    Проверка именно по **вхождению строки регекспа** в файл пайплайна: если
    определение вызова там поменяют, свод об этом узнает и назовёт расхождение,
    вместо того чтобы тихо считать по своей копии.
    """
    src = case / PIPELINE
    if not src.is_file() or not src.resolve().is_file():
        return {"checked": False, "why": f"исходник пайплайна недоступен: {PIPELINE}"}
    text = src.resolve().read_text(encoding="utf-8", errors="replace")
    present = TOOL_CALL_RE.pattern in text
    return {"checked": True, "path": PIPELINE, "sha256": sha256_file(src.resolve()),
            "pattern_present": present,
            "why": ("паттерн свода присутствует в execute_tool_call пайплайна"
                    if present else
                    "паттерн свода в пайплайне НЕ найден — определения разошлись")}


def extra_metrics(probes: list[dict]) -> dict:
    """Числа сверх прибора: p99, незавершённые вызовы, петли/повторы среди них, JSON."""
    toks = [int(p.get("n_new_tokens") or 0) for p in probes]
    flags = probe_flags(probes)
    tc = [p for p in probes if p.get("stop_reason") == "limit_in_tool_call"]
    tc_def = [p for p in tc if (p.get("metrics") or {}).get("max4gram_rep") is not None]

    def _share(flags_list: list[bool]) -> float | None:
        return round(sum(flags_list) / len(flags_list), 4) if flags_list else None

    return {
        "p99_len": pct(toks, 0.99),
        "p50_len": pct(toks, 0.50),
        "p90_len": pct(toks, 0.90),
        "max_len": max(toks) if toks else None,
        "n_at_budget": sum(1 for t in toks if t >= BUDGET),
        "unfinished_tool_call": {
            "n": len(tc),
            "share": _share(flags["unfinished_tool_call"]),
            "note": "индикатор ADR-050 п.3 (критерий стадии ≤ 0.05)",
        },
        "among_unfinished_tool_call": {
            "n": len(tc),
            "looped": _share([bool((p.get("metrics") or {}).get("looped")) for p in tc]),
            "repeat4": _share([((p.get("metrics") or {}).get("max4gram_rep") or 0) >= 2
                               for p in tc_def]),
            "repeat4_defined_n": len(tc_def),
            "json_valid": json_validity(probes)["unfinished_tool_call"],
        },
        "repeat4_all": {
            "defined_only": _share([f for f, d in
                                    zip(flags["repeat4"], flags["repeat4_defined"]) if d]),
            "coverage": _share(flags["repeat4_defined"]),
            "n_defined": sum(flags["repeat4_defined"]),
            "note": ("повтор 4-грамм = max4gram_rep ≥ 2; метрика определена при ≥ 8 словах "
                     "(probe_control.degenerate_metrics), покрытие названо рядом"),
        },
        "looped_all": _share(flags["looped"]),
        "json_valid": json_validity(probes),
    }


def mode_metrics(probes: list[dict], stored_aggregate: dict | None) -> dict:
    """Метрики режима: ядро — ``aggregate()`` прибора, сверх него — ``extra_metrics``.

    Сверка с сохранённым агрегатом отчёта — обязательна: если импортированная
    арифметика даёт не то, что лежит в отчёте, значит между отчётом и сводом
    разошлась версия прибора, и сводить нельзя (это отказ, а не предупреждение).
    """
    agg = LS.aggregate(probes, BUDGET)
    compat = {"checked": bool(stored_aggregate), "keys_compared": 0, "mismatches": []}
    if stored_aggregate:
        for key in ("unclosed_think_share", "truncated_share", "looped_share",
                    "mode_share_tool_call", "stray_think_close_share"):
            if key in stored_aggregate:
                compat["keys_compared"] += 1
                if agg.get(key) != stored_aggregate.get(key):
                    compat["mismatches"].append(
                        {"key": key, "from_report": stored_aggregate.get(key),
                         "recomputed": agg.get(key)})
        for key in ("median", "p90", "mean", "min", "max"):
            a, b = dig(agg, ("lengths", key)), dig(stored_aggregate, ("lengths", key))
            compat["keys_compared"] += 1
            if a != b:
                compat["mismatches"].append({"key": f"lengths.{key}", "from_report": b,
                                             "recomputed": a})
        compat["identical"] = not compat["mismatches"]
    return {
        "n": agg["n"],
        "core": agg,
        "extra": extra_metrics(probes),
        "recompute_check": compat,
        "prompt_tags": _tag_counts(probes),
    }


def _tag_counts(probes: list[dict]) -> dict:
    out: dict[str, int] = {}
    for p in probes:
        out[p["tag"]] = out.get(p["tag"], 0) + 1
    return out


# ── сравнение режимов с V1 ────────────────────────────────────────────────────

def _metric_row(name: str, base_val, mode_val, base_flags=None, mode_flags=None,
                base_lens=None, mode_lens=None, kind: str = "share") -> dict:
    """Строка сравнения: значение, дельта и значимость — тестами, объявленными выше.

    Булевы ряды (``base_flags``/``mode_flags``) нужны только там, где метрика —
    доля и ряды по пробам совпадают по индексам (пары настоящие). Там, где свод
    считает долю по подмножеству с другим знаменателем (повторы 4-грамм
    определены не на всех пробах, JSON-валидность — по ходам с вызовом), пара
    «проба ↔ проба» не определена, и строка несёт только дельту: подставлять
    туда ряд с другим знаменателем значило бы считать тест на несопоставимых
    рядах и выдавать это за значимость. Это названо, а не замолчано.
    """
    row: dict = {"metric": name, "V1": base_val, "value": mode_val}
    if kind == "share" and base_val is not None and mode_val is not None:
        row["delta_pp"] = round(mode_val - base_val, 4)
        row["abs_delta_ge_threshold"] = abs(row["delta_pp"]) >= SIG_PP
        if base_flags is not None and mode_flags is not None:
            row["mcnemar_exact"] = mcnemar_exact(base_flags, mode_flags)
            n = len(base_flags)
            row["two_prop_z"] = two_prop_z(sum(base_flags), n, sum(mode_flags), n)
            row["significant"] = bool(
                row["abs_delta_ge_threshold"]
                and row["mcnemar_exact"]["p_exact"] < SIG_P)
    elif kind == "length" and base_lens is not None and mode_lens is not None:
        diffs = [m - b for b, m in zip(base_lens, mode_lens)]
        row["median_delta_tokens"] = round(float(sorted(diffs)[len(diffs) // 2]), 1)
        row["mean_delta_tokens"] = round(sum(diffs) / len(diffs), 1)
        row["sign_test"] = sign_test(diffs)
        row["significant"] = bool(row["sign_test"]["p_exact"] < SIG_P)
    return row


def compare(metrics: dict, base: dict) -> dict:
    """Таблица «режим × метрика» против V1: числом и оценкой значимости (n = 104)."""
    b_probes, m_probes = base["_probes"], metrics["_probes"]
    bf, mf = probe_flags(b_probes), probe_flags(m_probes)
    b_core, m_core = base["core"], metrics["core"]
    b_ex, m_ex = base["extra"], metrics["extra"]
    b_lens = [int(p.get("n_new_tokens") or 0) for p in b_probes]
    m_lens = [int(p.get("n_new_tokens") or 0) for p in m_probes]

    rows = [
        _metric_row("unfinished_tool_call", dig(b_ex, ("unfinished_tool_call", "share")),
                    dig(m_ex, ("unfinished_tool_call", "share")),
                    bf["unfinished_tool_call"], mf["unfinished_tool_call"]),
        _metric_row("budget_hit", b_core.get("truncated_share"), m_core.get("truncated_share"),
                    bf["budget_hit"], mf["budget_hit"]),
        _metric_row("unclosed_think_all", b_core.get("unclosed_think_share"),
                    m_core.get("unclosed_think_share"),
                    bf["unclosed_think"], mf["unclosed_think"]),
        _metric_row("unclosed_think_natural",
                    dig(b_core, ("stop", "natural", "unclosed_think_share")),
                    dig(m_core, ("stop", "natural", "unclosed_think_share")),
                    [f for f, r in zip(bf["unclosed_think"], b_probes)
                     if r.get("stop_reason") == LS.STOP_TURN_END],
                    [f for f, r in zip(mf["unclosed_think"], m_probes)
                     if r.get("stop_reason") == LS.STOP_TURN_END]),
        _metric_row("looped_all", b_core.get("looped_share"), m_core.get("looped_share"),
                    bf["looped"], mf["looped"]),
        _metric_row("repeat4_all", dig(b_ex, ("repeat4_all", "defined_only")),
                    dig(m_ex, ("repeat4_all", "defined_only")), None, None),
        _metric_row("json_valid_with_call",
                    dig(b_ex, ("json_valid", "with_tool_call", "share")),
                    dig(m_ex, ("json_valid", "with_tool_call", "share")), None, None),
        _metric_row("median_len", dig(b_core, ("lengths", "median")),
                    dig(m_core, ("lengths", "median")), kind="length",
                    base_lens=b_lens, mode_lens=m_lens),
        _metric_row("p90_len", dig(b_core, ("lengths", "p90")), dig(m_core, ("lengths", "p90")),
                    kind="length", base_lens=b_lens, mode_lens=m_lens),
        _metric_row("p99_len", b_ex.get("p99_len"), m_ex.get("p99_len"),
                    kind="length", base_lens=b_lens, mode_lens=m_lens),
    ]
    return {
        "n": {"V1": len(b_probes), "mode": len(m_probes)},
        "significance": {"threshold_pp": SIG_PP, "p": SIG_P, "n": N_MIN,
                         "paired_test": "McNemar exact (two-sided binomial on discordant pairs)",
                         "unpaired_test": "two-proportion z (pooled)",
                         "threshold_source": "SIGNIFICANT=0.10 в assemble_s3aq_evidence.py (n = 104)"},
        "rows": rows,
        "stop_reason": {
            "V1": dig(b_core, ("stop", "reasons")), "mode": dig(m_core, ("stop", "reasons")),
            "V1_share": dig(b_core, ("stop", "reasons_share")),
            "mode_share": dig(m_core, ("stop", "reasons_share")),
        },
    }


# ── контроль тождества V1 против решающего отчёта S3aq ────────────────────────

def identity_control(v1: dict, ref: dict) -> dict:
    """V1 обязан совпасть с решающим отчётом S3aq побайтово — по каждому промпту.

    Почему побайтово, а не «по числам». Greedy детерминирован, пакет не меняет ни
    одного токена; значит любое расхождение — это смена входа (чекпойнт, прибор,
    окружение), и тогда разница режимов несла бы ещё и эту смену. Совпадение
    агрегатов при расхождении ответов — не тождество: два разных набора ответов
    могут дать одинаковую долю (так уже было в S3aq: агрегаты на двух бюджетах
    совпали частично из-за детерминизма).
    """
    a = v1["states"][STATE]["probes"]
    b = ref["states"][STATE]["probes"]
    same_prompts = [x["prompt"] == y["prompt"] for x, y in zip(a, b)]
    same_resp = [x["response"] == y["response"] for x, y in zip(a, b)]
    mism = [i for i, ok in enumerate(same_resp) if not ok]
    agg_keys = ("unclosed_think_share", "truncated_share", "looped_share",
                "mode_share_tool_call")
    agg_diff = {k: {"v1": v1["states"][STATE]["aggregate"].get(k),
                    "s3aq": ref["states"][STATE]["aggregate"].get(k)}
                for k in agg_keys
                if v1["states"][STATE]["aggregate"].get(k)
                != ref["states"][STATE]["aggregate"].get(k)}
    return {
        "reference": {"path_in_task": "runs/s3aq-budget-8192-20260920/format_wide_8192.json",
                      "in_worktree": False,
                      "extracted_from_git_to": REFERENCE,
                      "role": "решающий отчёт S3aq, состояние sft_v13_21500, бюджет 8192"},
        "n": {"v1": len(a), "s3aq": len(b)},
        "prompts_identical": all(same_prompts) and len(a) == len(b),
        "responses_identical": all(same_resp) and len(a) == len(b),
        "n_identical": len(same_resp) - len(mism),
        "first_mismatches": mism[:10],
        "aggregate_mismatches": agg_diff,
        "identical": bool(len(a) == len(b) and all(same_resp)),
        "consequence": ("числа режимов стоят на том же входе, что решающий отчёт"
                        if len(a) == len(b) and all(same_resp) else
                        "ТОЖДЕСТВО НАРУШЕНО: числа V1 и S3aq несопоставимы, "
                        "разница режимов несёт ещё и смену входа"),
    }


def log_format_check(case: Path = CASE) -> dict:
    """Формат строки журнала, которым пользуется разбор, присутствует в приборе.

    Та же дисциплина, что у проверки регекспа пайплайна: разбор журнала — копия
    формата. Если формат в приборе поменяют, а копия останется, разбор молча
    вернул бы «сверено 0 проб» — что читается как «расхождений нет». Поэтому
    отсутствие формата в исходнике — отказ, а не пустой результат.
    """
    src = case / "tools" / "probe_language_split.py"
    if not src.is_file():
        return {"checked": False, "why": "исходник прибора недоступен"}
    text = src.read_text(encoding="utf-8", errors="replace")
    present = "cyr_think={m['cyr_think']}" in text and "tok={len(new_ids)}" in text
    return {"checked": True, "path": "tools/probe_language_split.py",
            "sha256": sha256_file(src), "format_present": present,
            "why": ("поля строки журнала найдены в приборе" if present else
                    "поля строки журнала в приборе НЕ найдены — разбор журнала "
                    "разошёлся бы с прибором и дал бы ноль сверенных проб")}


def load_probe_log(path: Path, mode_label: str) -> dict:
    """Журнал прибора с диска; отсутствие файла — не «ноль проб», а отказ.

    Различие существенное: пустой список проб дал бы `compared=0`, а это уже
    отказ в `partial_identity_control` (`identical` требует `compared > 0`), так
    что тихо «сверить нечего» здесь невозможно.
    """
    if not path.is_file():
        return {"rows": [], "tail_incomplete": False, "missing": str(path)}
    return parse_probe_log(path.read_text(encoding="utf-8", errors="replace"), mode_label)


def parse_probe_log(text: str, mode_label: str) -> dict:
    """Пробы, о которых журнал прибора успел сообщить до обрыва прогона.

    Читаются только **полные** строки (с закрывающим переводом строки): оборванная
    запись — это не проба, а её половина, и принимать её за сверенную значило бы
    выдать обрыв за совпадение. Незавершённый хвост называется отдельно.
    """
    rows, tail_incomplete = [], False
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = PROBE_LOG_RE.search(line)
        if not m:
            continue
        if i == len(lines) - 1:
            # последняя строка файла без перевода строки — запись оборвана
            tail_incomplete = True
            continue
        if m.group("state") != STATE or m.group("mode") != mode_label:
            continue
        rows.append({
            "index": len(rows),
            "tag": m.group("tag"),
            "n_new_tokens": int(m.group("tok")),
            "stop_reason": m.group("stop"),
            "cyr_think": None if m.group("cyr_think") == "None" else float(m.group("cyr_think")),
            "looped": bool(m.group("looped")),
        })
    return {"rows": rows, "tail_incomplete": tail_incomplete}


def partial_identity_control(rows: list[dict], ref: dict, tail_incomplete: bool) -> dict:
    """Частичный контроль тождества: сверка проб, о которых журнал успел сказать.

    Сверяются пять полей, и все пять — отпечаток входа, а не агрегат: тег, длина в
    токенах, ``stop_reason``, ``cyr_think`` и признак цикла. Длина в токенах здесь
    главная: 154, 44, 492, 451 на ``turn_end`` — это не «примерно то же», это
    точное число, которое сдвинула бы и смена чекпойнта, и смена прибора, и смена
    версии torch. Совпадение агрегатов вместо этого ничего бы не доказало (два
    разных набора ответов дают одну и ту же долю).

    Что этот контроль **не** даёт — названо полем ``unmeasured``: пробы за
    пределами журнала не измерены, и их тождество опирается на аргумент
    (``inference``), а не на сверку. Это слабее побайтового контроля, и поле
    ``strength`` называет это прямо.
    """
    probes = ref["states"][STATE]["probes"]
    mismatches = []
    for r in rows:
        i = r["index"]
        if i >= len(probes):
            mismatches.append({"index": i, "why": "в решающем отчёте нет пробы с таким индексом"})
            continue
        p = probes[i]
        rt = p["metrics"].get("cyr_think")
        rt = None if rt is None else round(float(rt), 4)
        got = {"tag": r["tag"], "n_new_tokens": r["n_new_tokens"],
               "stop_reason": r["stop_reason"], "cyr_think": r["cyr_think"],
               "looped": r["looped"]}
        want = {"tag": p["tag"], "n_new_tokens": int(p["n_new_tokens"]),
                "stop_reason": p["stop_reason"], "cyr_think": rt,
                "looped": bool(p["metrics"].get("looped"))}
        diff = {k: {"log": got[k], "s3aq": want[k]} for k in got if got[k] != want[k]}
        if diff:
            mismatches.append({"index": i, "fields": diff})
    compared = len(rows)
    return {
        "kind": "partial",
        "why": ("живой луч V1 оборван на первой восьмёрке проб: отчёта "
                "v1_greedy_nogram4.json нет, есть журнал прибора — сверять можно "
                "только то, что он успел сообщить"),
        "fields_compared": ["tag", "n_new_tokens", "stop_reason", "cyr_think", "looped"],
        "compared": compared,
        "matched": compared - len(mismatches),
        "mismatches": mismatches,
        "tail_incomplete": tail_incomplete,
        "n_probes_total": len(probes),
        "unmeasured": len(probes) - compared,
        "identical": bool(compared > 0 and not mismatches),
        "strength": (f"частичный: измерено {compared} из {len(probes)} проб; "
                     f"побайтовое тождество остальных {len(probes) - compared} не измерено"),
        "inference": ("тождество остальных проб выводится, а не измерено: при greedy "
                      "и неизменных чекпойнте, приборе, наборе, бюджете и режиме "
                      "расхождение ответа невозможно без смены входа"),
        "consequence": ("сверенные пробы совпали со входом решающего отчёта"
                        if compared > 0 and not mismatches else
                        "ТОЖДЕСТВО НАРУШЕНО на сверенных пробах: числа режимов стоят "
                        "на другом входе, чем решающий отчёт"),
    }


# ── сборка ────────────────────────────────────────────────────────────────────

def declared_definitions() -> dict:
    return {
        "core_from_instrument": {
            "source": "tools/probe_language_split.py → aggregate(probes, budget)",
            "tool_sha256": TOOL_SHA,
            "why": ("определения ядра не переписываются: свод вызывает арифметику прибора "
                    "на пробах отчёта, поэтому «доля упёршихся в бюджет», «доля незакрытых "
                    "think», «медиана/p90», «доля петель» — те же величины, что в решающем "
                    "отчёте, по построению, а не по совпадению текста"),
            "values": {
                "budget_hit": "stop_reason != turn_end (прибор: truncated_share)",
                "stop_reason": "turn_end — ход дописан моделью; limit_in_<регион> — обрыв лимитом",
                "median/p90": "ближайший ранг (nearest-rank), не интерполяция",
                "unclosed_think_all": "доля генераций с незакрытым <think> по всем ходам",
                "unclosed_think_natural": ("то же по ходам с stop_reason = turn_end — правило "
                                           "ADR-045 п.5 (прибор: aggregate.stop.natural)"),
                "looped": f"max4gram_rep ≥ {LS.LOOP_MAX4GRAM_REP} (константа прибора)",
                "mode_share_tool_call": "доля генераций, содержащих <tool_call>",
            },
        },
        "added_by_this_analysis": {
            "p99_len": "перцентиль ближайшим рангом (прибор печатает медиану и p90)",
            "unfinished_tool_call": ("stop_reason = limit_in_tool_call — индикатор ADR-050 п.3, "
                                     "критерий стадии ≤ 0.05 (у CPT 0)"),
            "tc_looped / tc_repeat4": ("циклы и повторы 4-грамм среди ходов, кончающихся "
                                       "внутри незавершённого вызова (числа 0.4375 и 0.7812 "
                                       "контекста дельты — отсюда)"),
            "repeat4": ("max4gram_rep ≥ 2; определена при ≥ 8 словах "
                        "(probe_control.degenerate_metrics), покрытие печатается рядом"),
            "json_valid": ("регексп пайплайна v8 + json.loads; основное чтение — первый ход "
                           "модели, вариант по всей генерации печатается рядом"),
        },
        "significance": {
            "paired": "точный Мак-Немар (двусторонний бином на расхождениях) — пары настоящие",
            "unpaired": "двухдолевой z (пулированный) — консервативное чтение",
            "lengths": "знаковый тест на парных разностях длин",
            "thresholds": {"pp": SIG_PP, "p": SIG_P, "n": N_MIN,
                           "source": "SIGNIFICANT = 0.10 в assemble_s3aq_evidence.py"},
        },
    }


def answer_of(modes: dict, comparisons: dict) -> dict:
    """Прямой ответ на вопрос дельты — правилом, объявленным выше.

    Ответ считается из чисел, а не формулируется словами поверх них: veredict —
    функция от первичного индикатора по альтернативным режимам, и рядом лежат все
    числа, из которых он выведен, чтобы читатель мог проверить вывод сам.
    """
    primary = {}
    for tag in ("V1", "V2", "V3"):
        if tag in modes:
            primary[tag] = dig(modes[tag]["extra"], ("unfinished_tool_call", "share"))
    alt = {t: v for t, v in primary.items() if t in ("V2", "V3") and v is not None}
    if primary.get("V1") is None or not alt:
        # Ранний выход тоже обязан называть свою неполноту: «нет альтернативного луча»
        # — это нуль снятых лучей, а не «лучи ничего не изменили».
        return {"verdict": "insufficient_data", "why": "нет первичного индикатора по режимам",
                "alternative_modes_measured": sorted(alt), "partial_evidence": True,
                "primary_indicator": primary, "rule": _rule_text()}

    #: Вторичные индикаторы: вопрос дельты называет две вещи — убегающую **длину**
    #: и незавершённые вызовы. Первичный индикатор отвечает про вызовы; про длину
    #: отвечают эти числа, и они печатаются рядом, а не подразумеваются. Значимость
    #: по ним не считается: бюджет 8192 обрезает распределение, и «длина не выросла»
    #: на обрезанном ряде означало бы «не выросла сверх бюджета», а не «не выросла».
    secondary = {tag: {"budget_hit": dig(modes[tag], ("core", "truncated_share")),
                       "median_len": dig(modes[tag], ("core", "lengths", "median")),
                       "p90_len": dig(modes[tag], ("core", "lengths", "p90")),
                       "p90_at_budget": dig(modes[tag], ("core", "lengths", "p90")) == BUDGET,
                       "unclosed_think_natural": dig(modes[tag], ("core", "stop", "natural",
                                                                  "unclosed_think_share")),
                       "looped_all": dig(modes[tag], ("core", "looped_share"))}
                   for tag in ("V1", "V2", "V3") if tag in modes}

    rows = {t: next((r for r in comparisons[t]["rows"]
                     if r["metric"] == "unfinished_tool_call"), None) for t in alt}
    sig_drop = {t: bool(r and r.get("significant") and r.get("delta_pp", 0) < 0)
                for t, r in rows.items()}
    below = {t: bool(v <= RUNAWAY_MAX) for t, v in alt.items()}

    if any(below[t] and sig_drop[t] for t in alt):
        verdict = "protocol_artifact"
    elif all((not below[t]) and (not sig_drop[t]) for t in alt):
        verdict = "weights_property"
    else:
        verdict = "insufficient_data"

    #: Какая ветка правила сработала и почему — **выведено** из тех же флагов, а не
    #: написано словами поверх чисел. Нужно потому, что исход «падение значимо, но
    #: порога не достигло» таблица правила заранее не предусматривала: он попадает
    #: в третью ветку (`insufficient_data`) и читается как «данных нет», хотя данных
    #: много — правило просто не имеет ветки для «сильно лучше, но ещё не норма».
    #: Читатель обязан видеть это различие, иначе третья ветка скроет результат.
    branch = {
        "below_threshold": {t: below[t] for t in alt},
        "significant_drop": {t: sig_drop[t] for t in alt},
        "fired": ("first" if any(below[t] and sig_drop[t] for t in alt) else
                  "second" if all((not below[t]) and (not sig_drop[t]) for t in alt) else
                  "third"),
        "why": ("хотя бы один луч ушёл под критерий стадии значимо"
                if any(below[t] and sig_drop[t] for t in alt) else
                "ни один луч не ушёл под критерий и ни в одном падение не значимо"
                if all((not below[t]) and (not sig_drop[t]) for t in alt) else
                "есть луч со значимым падением, но ни один не достиг критерия стадии: "
                "объявленная таблица правила ветки для этого исхода не имеет, поэтому "
                "вердикт — insufficient_data, а не «данных нет». Домерить: луч, "
                "который может дойти до критерия (V3), и вариант «только штраф»"),
    }

    #: ── Ось длины: второе чтение того же вопроса ─────────────────────────────
    #: Вопрос дельты называет **две** вещи — убегающую длину и незавершённые
    #: вызовы, — и они отвечают по-разному. Первичный индикатор отвечает про
    #: вызовы; про длину отвечает эта ось, и её направление **выводится**
    #: сравнением доли упёршихся в бюджет, а не назначается словом. Порог сдвига —
    #: тот же SIG_PP: «усиливается» на 0.01 п.п. было бы шумом.
    #:
    #: Значимость по этой оси не считается по построению (`why_not_significant`):
    #: бюджет 8192 обрезает ряд, и разница медиан на обрезанном ряде читалась бы
    #: как свойство модели, хотя меряет бюджет.
    def _direction(alt_hit, base_hit) -> str:
        if alt_hit is None or base_hit is None:
            return "неизвестно"
        d = alt_hit - base_hit
        return ("усиливается" if d >= SIG_PP else
                "ослабевает" if d <= -SIG_PP else "без значимого сдвига")

    length_axis = {
        t: {
            "direction": _direction(dig(modes[t], ("core", "truncated_share")),
                                    dig(modes["V1"], ("core", "truncated_share"))),
            "control_budget_hit": dig(modes["V1"], ("core", "truncated_share")),
            "budget_hit": dig(modes[t], ("core", "truncated_share")),
            "control_median_len": dig(modes["V1"], ("core", "lengths", "median")),
            "median_len": dig(modes[t], ("core", "lengths", "median")),
            "median_at_budget": dig(modes[t], ("core", "lengths", "median")) == BUDGET,
            "looped_all": dig(modes[t], ("core", "looped_share")),
            "control_looped_all": dig(modes["V1"], ("core", "looped_share")),
            "allowed_for_conclusions": dig(modes[t], ("allowed_for_conclusions",)),
        } for t in alt}

    return {
        "question": ("убегающая длина и незавершённые вызовы — свойство весов или артефакт "
                     "протокола декодирования"),
        "verdict": verdict,
        "rule_branch": branch,
        "length_axis": length_axis,
        "length_axis_note": (
            "ось длины — **второе** чтение вопроса, а не вердикт: вердикт выше считается "
            "по первичному индикатору (незавершённые вызовы) объявленным правилом. "
            "Направление выведено сдвигом доли упёршихся в бюджет с порогом 0.10 п.п.; "
            "значимость по длине не считается, потому что бюджет обрезает ряд. "
            "У луча с allowed_for_conclusions=false числа оси длины описывают режим, "
            "который прибор объявил непригодным для выводов о языке и формате "
            "(в нём петля растёт: см. looped_all против control_looped_all), поэтому "
            "«усиливается» читается как «не исчезает при снятии запрета», а не как "
            "точная величина роста"),
        #: Какие альтернативные режимы реально сняты. Прогон длинный (≈2.2 ч на режим:
        #: запрет повторов 4-грамм считается на стороне Python), и отчёт обязан
        #: называть свою неполноту числом, а не выглядеть полным: вердикт по одному
        #: лучу слабее вердикта по двум, и это видно в поле, а не в примечании.
        "alternative_modes_measured": sorted(alt),
        "partial_evidence": len(alt) < 2,
        "primary_indicator": primary,
        "primary_indicator_definition": ("доля ходов, кончающихся внутри незавершённого "
                                         "tool_call (stop_reason = limit_in_tool_call); "
                                         f"критерий стадии ADR-050 п.3 — ≤ {RUNAWAY_MAX}"),
        "secondary_indicators": secondary,
        "secondary_definition": {
            "budget_hit": "доля ходов, упёршихся в бюджет 8192 (stop_reason ≠ turn_end)",
            "median_len": "медиана длины хода, токены",
            "p90_len": "p90 длины хода; равен бюджету — распределение обрезано бюджетом",
            "unclosed_think_natural": "незакрытый <think> среди естественно завершённых (ADR-045 п.5)",
            "looped_all": "доля зацикленных ходов (max4gram_rep ≥ 8)",
            "why_not_significant": ("значимость по длинам не считается: бюджет обрезает ряд, и "
                                    "разница медиан на обрезанном ряде читалась бы как свойство "
                                    "модели, хотя меряет бюджет"),
        },
        "per_alternative_mode": {
            t: {"share": alt[t], "at_or_below_threshold": below[t],
                "delta_pp_vs_V1": (rows[t] or {}).get("delta_pp"),
                "mcnemar_p": dig(rows[t] or {}, ("mcnemar_exact", "p_exact")),
                "two_prop_z_p": dig(rows[t] or {}, ("two_prop_z", "p")),
                "significant_drop": sig_drop[t]}
            for t in alt},
        "rule": _rule_text(),
        "reading": {
            "weights_property": ("убегание сохраняется при смене декодирования ⇒ оно свойство "
                                 "весов: ни один из альтернативных режимов не уводит долю "
                                 "незавершённых вызовов под критерий стадии значимо"),
            "protocol_artifact": ("убегание исчезает при смене декодирования ⇒ оно артефакт "
                                  "протокола: режим уводит долю под критерий значимо, и "
                                  "чинить нужно протокол/декодирование, а не данные"),
            "insufficient_data": ("числа не дают ни того, ни другого исхода по объявленному "
                                  "правилу — назвать, что домерить"),
        }[verdict],
    }


def _rule_text() -> str:
    return (f"вердикт = f(первичный индикатор по V2 и V3) — по тем из них, что сняты "
            f"(поле alternative_modes_measured; снят один луч — вердикт помечен "
            f"partial_evidence=true, и это слабее двух: "
            f"все альтернативные режимы > {RUNAWAY_MAX} и ни одного значимого падения → "
            f"weights_property; хотя бы один ≤ {RUNAWAY_MAX} и значимое падение vs V1 "
            f"(порог {SIG_PP} п.п. и p < {SIG_P} по точному Мак-Немару) → protocol_artifact; "
            f"иначе insufficient_data). Порог {RUNAWAY_MAX} — критерий стадии ADR-050 п.3; "
            f"порог значимости — SIGNIFICANT = {SIG_PP} при n = 104 (assemble_s3aq_evidence.py).")


def build(run: str = RUN, case: Path | None = None,
          allow_historical_control: bool = False) -> tuple[int, dict]:
    """Разбор по каталогу прогона. ``case`` — корень кейса (для тестов на фикстурах).

    ``allow_historical_control`` разрешает взять контроль историческим, если отчёта
    луча V1 нет (см. блок в docstring). По умолчанию запрещено: подстановка чужого
    отчёта на место своего не должна происходить от одной лишь нехватки файла.
    """
    case = case or CASE
    run_dir = case / run
    problems: list[str] = []
    #: Неполнота состава — это **не** нарушение контракта: прогон длинный, лучи
    #: снимаются по одному, и «V3 ещё не снят» не делает числа V2 неверными.
    #: Нарушение контракта (чужой чекпойнт, разошедшаяся арифметика, сломанное
    #: тождество) — делает. Держать их в одном списке значило бы называть
    #: неполноту нарушением, а нарушение — неполнотой; поэтому списки разные, а
    #: код возврата общий (1) — он объявлен тестами и означает «не полный успех».
    incomplete: list[str] = []

    ref = load(case / REFERENCE)
    reports, missing = {}, []
    for tag, spec in MODES.items():
        d = load(run_dir / f"{spec['stem']}.json")
        if d is None:
            missing.append(f"{tag} ({spec['stem']}.json)")
        else:
            reports[tag] = d
    if not reports:
        return EXIT_NOT_VERIFIED, {"error": f"нет ни одного отчёта режима в {run}",
                                  "missing": missing}

    # ── источник контроля ─────────────────────────────────────────────────────
    # Отчёта V1 нет — либо это отказ (по умолчанию), либо исторический контроль.
    control_mode: dict = {
        "kind": CONTROL_LIVE,
        "why": "отчёт луча V1 снят этой сессией, тождество проверяется побайтово",
    }
    if "V1" not in reports:
        if not allow_historical_control:
            pass  # откажет общий цикл ниже — с подсказкой про исторический контроль
        elif ref is None:
            problems.append("исторический контроль запрошен, но решающего отчёта S3aq нет — "
                            "взять контроль неоткуда")
        elif not (run_dir / "v1_greedy_nogram4.log").is_file():
            problems.append("исторический контроль запрошен, но журнала оборванного луча V1 "
                            "нет — тождество было бы утверждением, а не сверкой")
        else:
            # Роль V1 играет состояние решающего отчёта: тот же чекпойнт, прибор,
            # набор, бюджет и режим. Отчёта-копии на диске **не создаётся**: файл,
            # неотличимый от снятого прогона, отравил бы каталог прогона и любой
            # другой инструмент, читающий его как измерение.
            reports["V1"] = ref
            control_mode = {
                "kind": CONTROL_HISTORICAL,
                "why": ("живой луч V1 оборван: отчёта нет, есть журнал прибора — роль "
                        "контроля играет состояние решающего отчёта S3aq"),
                "source_report": REFERENCE,
                "source_report_sha256": sha256_file(case / REFERENCE),
                "live_report_absent": "v1_greedy_nogram4.json",
                "live_log": V1_LOG,
                "identity_with_live_run": "частичный (partial_identity_control)",
                "not_a_substitute_for": ("побайтовый контроль тождества живого луча V1: "
                                         "он не снят, и это названо, а не замолчано"),
            }

    # Режим, которого нет, — это **отказ**, а не молчание: доля величин от
    # отсутствующего луча не отличается от нуля, и «свод без V2» читался бы как
    # «V2 ничего не изменил». Частичный прогон обязан называть себя частичным.
    for tag, spec in MODES.items():
        if tag not in reports:
            hint = (" (если отчёта нет, а живой луч оборван — есть --historical-control)"
                    if tag == "V1" and not allow_historical_control else "")
            incomplete.append(f"режим {tag} ({spec['stem']}.json) не снят — "
                              f"сравнение неполно{hint}")
    if ref is None:
        problems.append(f"нет решающего отчёта S3aq по пути {REFERENCE} — "
                        f"контроль тождества V1 невозможен")

    # ── контракт входа: тот же чекпойнт, прибор, набор, бюджет, режим ──────────
    checks = {"checkpoint_sha256": {}, "instrument_sha256": {}, "protocol": {},
              "decoding_label": {}, "n_probes": {}, "mode_present": {}}
    metrics: dict[str, dict] = {}
    for tag, d in reports.items():
        st = d["states"].get(STATE)
        if not st or "probes" not in st:
            problems.append(f"{tag}: в отчёте нет состояния {STATE} с пробами")
            continue
        probes = st["probes"]
        checks["checkpoint_sha256"][tag] = st.get("checkpoint_sha256")
        checks["instrument_sha256"][tag] = d.get("tool_sha256")
        checks["n_probes"][tag] = len(probes)
        checks["decoding_label"][tag] = dig(d, ("protocol", "decoding"))
        checks["protocol"][tag] = {
            "prompts_set": dig(d, ("protocol", "prompts_set")),
            "max_new_tokens": dig(d, ("protocol", "max_new_tokens")),
            "stop_at_turn_end": dig(d, ("protocol", "stop_at_turn_end")),
            "batch_size": dig(d, ("protocol", "batch_size")),
        }
        checks["mode_present"][tag] = True
        if st.get("checkpoint_sha256") != CKPT_SHA:
            problems.append(f"{tag}: чекпойнт {st.get('checkpoint_sha256')} ≠ пиннутый {CKPT_SHA}")
        if d.get("tool_sha256") != TOOL_SHA:
            problems.append(f"{tag}: прибор {d.get('tool_sha256')} ≠ прибор S3aq {TOOL_SHA}")
        if dig(d, ("protocol", "prompts_set")) != PROMPTS_SET:
            problems.append(f"{tag}: набор промптов {dig(d, ('protocol', 'prompts_set'))} ≠ {PROMPTS_SET}")
        if dig(d, ("protocol", "max_new_tokens")) != BUDGET:
            problems.append(f"{tag}: бюджет {dig(d, ('protocol', 'max_new_tokens'))} ≠ {BUDGET}")
        if not dig(d, ("protocol", "stop_at_turn_end")):
            problems.append(f"{tag}: нет остановки на конце хода — мерялось бы продолжение хода")
        if dig(d, ("protocol", "batch_size")) != 8:
            problems.append(f"{tag}: пакет {dig(d, ('protocol', 'batch_size'))} ≠ 8")
        if len(probes) != N_MIN:
            problems.append(f"{tag}: проб {len(probes)} ≠ {N_MIN}")
        if dig(d, ("protocol", "decoding")) != MODES[tag]["decoding"]:
            problems.append(f"{tag}: имя режима {dig(d, ('protocol', 'decoding'))} ≠ "
                            f"объявленное {MODES[tag]['decoding']}")
        m = mode_metrics(probes, st.get("aggregate"))
        if m["recompute_check"].get("checked") and not m["recompute_check"].get("identical", True):
            problems.append(f"{tag}: импортированная арифметика расходится с агрегатом отчёта: "
                            f"{m['recompute_check']['mismatches'][:3]}")
        m["_probes"] = probes
        m["decoding_params"] = dig(d, ("protocol", "decoding_params"))
        m["allowed_for_conclusions"] = dig(d, ("protocol", "decoding_protocol",
                                              "allowed_for_conclusions"))
        m["role"] = (MODES[tag]["role"]
                     + " (КОНТРОЛЬ ИСТОРИЧЕСКИЙ: роль играет состояние решающего отчёта "
                       "S3aq; живой луч не снят)"
                     if tag == "V1" and control_mode["kind"] == CONTROL_HISTORICAL
                     else MODES[tag]["role"])
        #: У исторического контроля носитель числа — файл решающего отчёта, а не
        #: отсутствующий отчёт луча V1: `report_sha256` обязан называть то, из чего
        #: число **взято**, иначе хеш указывал бы на несуществующий файл.
        src = (case / REFERENCE if tag == "V1" and control_mode["kind"] == CONTROL_HISTORICAL
               else run_dir / f"{MODES[tag]['stem']}.json")
        m["report"] = (REFERENCE if tag == "V1" and control_mode["kind"] == CONTROL_HISTORICAL
                       else f"{run}/{MODES[tag]['stem']}.json")
        m["report_sha256"] = sha256_file(src)
        metrics[tag] = m

    comparisons = {tag: compare(metrics[tag], metrics["V1"])
                   for tag in ("V2", "V3") if tag in metrics and "V1" in metrics}

    ident, partial_ident = None, None
    have_state = lambda d: bool(dig(d, ("states", STATE, "probes")))  # noqa: E731
    if control_mode["kind"] == CONTROL_HISTORICAL:
        # Сверять отчёт сам с собой бессмысленно: `control` и `reference` — один и
        # тот же файл, и `identical=True` здесь ничего не значило бы. Поэтому вместо
        # побайтового контроля идёт частичный — по журналу оборванного луча.
        log = load_probe_log(case / V1_LOG, MODES["V1"]["decoding"])
        fmt = log_format_check(case)
        partial_ident = partial_identity_control(log["rows"], ref, log["tail_incomplete"])
        partial_ident["log_format_check"] = fmt
        if fmt.get("checked") and not fmt.get("format_present"):
            problems.append("формат строки журнала разошёлся с прибором: сверка проб "
                            "измеряла бы не то, что прибор печатает")
        if not partial_ident["identical"]:
            problems.append("частичный контроль тождества нарушен: "
                            f"{len(partial_ident['mismatches'])} расхождений из "
                            f"{partial_ident['compared']} сверенных проб — числа режимов "
                            "стоят не на том входе, что решающий отчёт")
    elif ref is not None and "V1" in reports:
        if have_state(reports["V1"]) and have_state(ref):
            ident = identity_control(reports["V1"], ref)
            if not ident["identical"]:
                problems.append("ТОЖДЕСТВО V1 с S3aq нарушено: "
                                f"{ident['n']['v1'] - ident['n_identical']} расхождений ответов; "
                                "числа режимов стоят на другом входе")
        else:
            problems.append(f"контроль тождества невозможен: в отчёте V1 или в решающем "
                            f"отчёте S3aq нет состояния {STATE} с пробами")

    pipeline = pipeline_definition_check(case)
    if pipeline.get("checked") and not pipeline.get("pattern_present"):
        problems.append("определение JSON-валидности разошлось с пайплайном: паттерн не найден")

    answer = answer_of(metrics, comparisons) if "V1" in metrics else {
        "verdict": "insufficient_data", "why": "нет V1 — сравнивать не с чем"}

    status = ("input_contract_violated" if problems else
              "measurement_incomplete" if incomplete else "ok")
    doc = {
        "schema": "sft-decode-diagnosis/1",
        "tool": "tools/analyze_decode_modes.py",
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "question": ("убегающая длина и незавершённые вызовы — свойство весов или артефакт "
                     "протокола декодирования (на одном чекпойнте, наборе и бюджете, три режима)"),
        "state": STATE,
        "budget": BUDGET,
        "inputs": {
            "run": run,
            "reference": {"path": REFERENCE, "sha256": sha256_file(case / REFERENCE)
                          if (case / REFERENCE).is_file() else None},
            "modes": {tag: {"report": metrics[tag]["report"],
                            "report_sha256": metrics[tag]["report_sha256"],
                            "decoding": checks["decoding_label"].get(tag),
                            "decoding_params": metrics[tag]["decoding_params"],
                            "allowed_for_conclusions": metrics[tag]["allowed_for_conclusions"],
                            "role": metrics[tag]["role"]}
                      for tag in metrics},
            "not_measured": missing,
        },
        "contract_checks": checks,
        "definitions": declared_definitions(),
        "pipeline_definition": pipeline,
        "modes": {tag: {k: v for k, v in m.items() if k != "_probes"}
                  for tag, m in metrics.items()},
        "comparisons_vs_v1": comparisons,
        "identity_control_v1_vs_s3aq": ident,
        "partial_identity_control": partial_ident,
        "control_mode": control_mode,
        "answer": answer,
        "not_decided": list(NOT_DECIDED),
        "problems": problems,
        "incomplete": incomplete,
    }
    return (EXIT_OK if status == "ok" else EXIT_FAIL), doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=RUN, help=f"каталог прогона (по умолчанию {RUN})")
    ap.add_argument("--case-root", default=None,
                    help="корень кейса (по умолчанию — каталог инструмента)")
    ap.add_argument("--out", default=OUT, help=f"куда писать разбор (по умолчанию {OUT})")
    ap.add_argument("--print", action="store_true", help="напечатать разбор в stdout")
    ap.add_argument("--historical-control", action="store_true",
                    help=("взять контроль историческим (состояние решающего отчёта S3aq), "
                          "если отчёта луча V1 нет; тождество подтверждается частично по "
                          "журналу оборванного прогона"))
    args = ap.parse_args()

    rc, doc = build(args.run, Path(args.case_root) if args.case_root else None,
                    allow_historical_control=args.historical_control)
    if rc == EXIT_NOT_VERIFIED:
        note(f"NOT-VERIFIED: {doc.get('error')} (нет: {', '.join(doc.get('missing', []))})")
        return rc
    out = (Path(args.case_root) if args.case_root else CASE) / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.print:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    note(f"разбор: {args.out} (status={doc['status']}, проблем: {len(doc['problems'])})")
    for p in doc["problems"]:
        note(f"  ! {p}")
    note(f"ответ: verdict={doc['answer'].get('verdict')} "
         f"первичный индикатор={doc['answer'].get('primary_indicator')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
