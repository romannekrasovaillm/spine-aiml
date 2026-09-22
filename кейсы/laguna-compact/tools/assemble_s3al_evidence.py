#!/usr/bin/env python3
"""S3al — свод: штатный режим декодирования и достоверный вердикт по языку.

Основание — ADR-040 п.6–7 (коммит `4fcaf83`): петля SFT-состояний есть
повторяемость n-грамм и снимается **механически** запретом повторов 4-грамм
(`looped_share` 0.0000 против 0.5417 при greedy), штраф за повторы не помогает
(0.5833), sampling снижает частично (0.3333). Из этого следуют три вещи, и все три
здесь сводятся в один артефакт:

1. **Штатный режим закреплён в приборе**, а не в тексте свода: запрет повторов
   4-грамм — умолчание (`probe_language_split.STANDARD_NO_REPEAT_NGRAM`), прогон
   без него — отказ (код 1), пока не назван `--legacy-decoding`. Свод это не
   пересказывает, а **проверяет**: импортирует прибор и вызывает его функцию
   разрешения режима (`protocol.self_check`).
2. **Вердикт по языку** (ADR-039) считается по **чистым** генерациям (ход дописан,
   петли нет) при штатном режиме и n ≥ 100 на состояние; рядом лежат все три числа,
   которых требует TASK: кириллица по чистым (`defined_only`), покрытие (доля
   чистых) и основная метрика (`with_zeros`), плюс запас до границы 0.4 и интервал.
3. **Формат: артефакт против реального дефекта.** Разложение **точной арифметикой**
   (вклады складываются в итог): доля признака по всем генерациям раскладывается на
   вклад трёх групп — зацикленных (это и есть артефакт протокола), оборванных
   лимитом без петли (это бюджет) и дописанных ходов (это дефект модели). Правило
   «реальный дефект или артефакт» объявлено константой до чисел.

Чего свод **не** делает: не пересчитывает числа прибора своей арифметикой (берёт
`aggregate` как есть) и не выносит вердикт там, где вход не тот (нет отчёта, мало
генераций, тождество прибора не подтверждено) — вместо вердикта ставится зона и
причина.

Коды возврата::

    0 — свод собран
    1 — отказ: вход противоречит контракту (штатный режим в приборе снят,
        отчёт штатного режима помечен непригодным для выводов)
    2 — NOT-VERIFIED: нечего сводить (нет ни одного отчёта прогона)
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

import probe_language_split as LS   # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Прогон S3al и прогон-предшественник (S3ak). Предшественник нужен не для
#: вердикта, а для пары «greedy против штатного»: greedy-ногу S3ak нельзя
#: перемерить без устройства, а числа формата обязаны сравниваться на одном
#: наборе и одних параметрах.
RUN = "runs/s3al-decoding-protocol-20260918"
PRIOR_RUN = "runs/s3ak-degeneration-20260917"

#: Порог выборки: вердикт ADR-039 выносится при n ≥ 100 генераций на состояние.
#: Число объявлено TASK S3al (а не выведено из данных): при 24 генерациях шаг
#: одной пробы равен 0.042, то есть запас 0.008 у S3aj был вчетверо меньше шага.
SAMPLE_FLOOR = 100

#: Правило «реальный дефект против артефакта» — объявлено ДО чисел.
#: Признак считается **реальным**, если он есть у ходов, которые нечем объяснить,
#: кроме модели: ход дописан (не оборван бюджетом) и петли нет (не артефакт
#: протокола). Порог 0.5 — тот же, что у критерия ADR-039 для «русского ответа»:
#: «больше половины» как граница выбрана в самом критерии, и брать здесь другое
#: число значило бы менять строгость по месту.
REAL_DEFECT_MIN = 0.5
#: Ниже этого — признак у необъяснённых ходов почти отсутствует: тогда прежнее
#: число было артефактом, а не дефектом.
REAL_DEFECT_MAX_AS_ARTIFACT = 0.2

#: Зоны вердикта по языку, которые добавляет S3al (остальные — из ADR-039).
ZONE_SMALL_SAMPLE = "insufficient_sample"
ZONE_BLOCKED = "not_measured_device"


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(CASE))
    except ValueError:
        return str(p)


def load(p: Path) -> dict | None:
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


# ─────────────────────── протокол: закреплён и проверен ───────────────────────

def protocol_block() -> dict:
    """Штатный режим: где он задан и что прибор действительно его применяет.

    Проверка **вызовом**, а не чтением текста: свод импортирует прибор и
    спрашивает его, какой режим получится при пустых флагах. Текст можно
    поправить отдельно от поведения, вызов — нет.
    """
    default, _ = LS.resolve_no_repeat_ngram(None, False)
    legacy, _ = LS.resolve_no_repeat_ngram(None, True)
    _, refusal = LS.resolve_no_repeat_ngram(0, False)
    block = LS.decoding_protocol_block(default, None, False)
    return {
        "default_mode": block["default_mode"],
        "standard_no_repeat_ngram": LS.STANDARD_NO_REPEAT_NGRAM,
        "flag_where_set": {
            "tool": "tools/probe_language_split.py",
            "constant": "STANDARD_NO_REPEAT_NGRAM",
            "resolver": "resolve_no_repeat_ngram()",
            "cli_standard": "--no-repeat-ngram N (по умолчанию N=4)",
            "cli_legacy": "--legacy-decoding (снимает запрет; отчёт помечается "
                          "непригодным для выводов)",
            "report_field": "protocol.decoding_protocol.allowed_for_conclusions",
        },
        "self_check": {
            "default_resolves_to": default,
            "legacy_resolves_to": legacy,
            "run_without_ban_is_refused": bool(refusal),
            "refusal_names_fix": bool(refusal and "--legacy-decoding" in refusal),
            "why": "проверка вызовом прибора, а не чтением текста: «закреплено в "
                   "конфиге» — утверждение о поведении",
        },
        "rules": block["rules"],
        "gates": instrument_gates(),
    }


def instrument_gates() -> dict:
    """Гейты тождества прибора: смена умолчания не сдвинула прежний протокол.

    Два гейта, и оба обязательны. Первый — мост к S3ai (ядро промптов, прежний
    бюджет): он отвечает «пакетная генерация не меняет байтов». Второй — мост к
    S3ak (расширенный набор, 4096, конец хода): он отвечает «явный запрос прежнего
    режима даёт прежние байты **после** того, как умолчание сменилось». Без
    второго смена умолчания осталась бы непроверенной.
    """
    out: dict = {}
    pairs = [
        ("core_vs_s3ai", RUN + "/identity_core_cfinal_batched.json",
         "runs/s3ai-probes-20260917/identity_core_cfinal.json", "cfinal", "cfinal"),
        ("greedy_vs_s3ak", RUN + "/legacy_greedy_extended.json",
         PRIOR_RUN + "/decoding_greedy.json", "sft_resume_6000", "sft_resume_6000"),
        ("sample_vs_s3ak", RUN + "/legacy_sample_extended.json",
         PRIOR_RUN + "/decoding_sample.json", "sft_resume_6000", "sft_resume_6000"),
    ]
    for name, new_p, old_p, new_state, old_state in pairs:
        new = load(CASE / new_p)
        old = load(CASE / old_p)
        if not new or not old:
            out[name] = {"available": False,
                         "why": f"нет отчёта: {new_p if not new else old_p}"}
            continue
        p_new = (new.get("states") or {}).get(new_state, {}).get("probes") or []
        p_old = (old.get("states") or {}).get(old_state, {}).get("probes") or []
        diffs = [i for i, (a, b) in enumerate(zip(p_new, p_old))
                 if a.get("response") != b.get("response")]
        out[name] = {
            "available": bool(p_new and p_old),
            "compared": min(len(p_new), len(p_old)),
            "byte_identical": bool(p_new and p_old) and not diffs and len(p_new) == len(p_old),
            "diff_indexes": diffs[:10],
            "run": new_p, "reference": old_p,
            "legacy_flag": bool(((new.get("protocol") or {})
                                 .get("decoding_protocol") or {}).get("legacy_decoding")),
        }
    return out


# ─────────────────────── метрики формата ───────────────────────

#: Признаки формата, по которым считается разложение. Каждый — функция от пробы:
#: так «незакрытый think», «нет вызова инструмента» и «ход не дописан» считаются
#: одной арифметикой, а не тремя похожими кусками кода.
FORMAT_FLAGS = {
    "unclosed_think": lambda p: bool(p["metrics"].get("unclosed_think")),
    "no_tool_call": lambda p: not bool(p["metrics"].get("has_tool_call")),
    "not_finished": lambda p: p.get("stop_reason") != LS.STOP_TURN_END,
}


def probes_of(path: Path, state: str) -> tuple[list[dict], dict]:
    d = load(path)
    if not d or d.get("schema") != "probe-language-split/1":
        return [], {}
    val = (d.get("states") or {}).get(state) or {}
    return (val.get("probes") or []), d


def format_table(path: Path, state: str) -> dict:
    """Метрики формата одного отчёта: по всем генерациям и по дописанным ходам.

    Два чтения обязательны. «Незакрытых <think> 0.7» по всем генерациям смешивает
    «модель не закрыла рассуждение» и «генерацию оборвал бюджет внутри
    рассуждения»; вердикт о формате может опираться только на первое.
    """
    probes, d = probes_of(path, state)
    if not probes:
        return {"available": False, "why": f"нет состояния {state} в {rel(path)}"}
    n = len(probes)
    natural = [p for p in probes if p.get("stop_reason") == LS.STOP_TURN_END]
    proto = (d.get("protocol") or {})

    def share(sub: list[dict], flag: str) -> float | None:
        if not sub:
            return None
        return round(sum(FORMAT_FLAGS[flag](p) for p in sub) / len(sub), 4)

    toks = [int(p.get("n_new_tokens") or 0) for p in probes]
    return {
        "available": True,
        "report": rel(path),
        "tool_sha256": d.get("tool_sha256"),
        "state": state,
        "n": n,
        "decoding": proto.get("decoding"),
        "decoding_protocol": proto.get("decoding_protocol"),
        "prompts_set": proto.get("prompts_set"),
        "prompts_digest": proto.get("prompts_digest"),
        "max_new_tokens": proto.get("max_new_tokens"),
        "stop_at_turn_end": proto.get("stop_at_turn_end"),
        "looped_share": round(sum(bool(p["metrics"].get("looped")) for p in probes) / n, 4),
        "natural_stop_share": round(len(natural) / n, 4),
        "unclosed_think_share": share(probes, "unclosed_think"),
        "tool_call_share": round(sum(bool(p["metrics"].get("has_tool_call"))
                                     for p in probes) / n, 4),
        "median_tokens": LS._pct(toks, 0.5),
        "p90_tokens": LS._pct(toks, 0.9),
        "natural_stops": {
            "n": len(natural),
            "unclosed_think_share": share(natural, "unclosed_think"),
            "tool_call_share": (None if not natural else
                                round(sum(bool(p["metrics"].get("has_tool_call"))
                                          for p in natural) / len(natural), 4)),
            "cyr_answer_with_zeros": (None if not natural else
                                      LS._metric(natural, "cyr_answer")["with_zeros"]),
        },
        "decomposition": format_decomposition(probes),
    }


def format_decomposition(probes: list[dict]) -> dict:
    """Разложение признака формата на вклады: артефакт протокола / бюджет / модель.

    Арифметика **точная**: доля признака по всем генерациям равна сумме вкладов
    трёх групп, где вклад = (доля группы среди генераций) × (доля признака внутри
    группы). Поэтому «сколько было артефактом» — число, а не оценка: у зацикленной
    генерации обрыв и незакрытый блок объясняются петлёй, у оборванной лимитом —
    бюджетом, у дописанного хода без петли объяснять нечем, кроме модели.
    """
    n = len(probes)
    if not n:
        return {}
    groups = {
        "looped": [p for p in probes if p["metrics"].get("looped")],
        "truncated_not_looped": [p for p in probes
                                 if not p["metrics"].get("looped")
                                 and p.get("stop_reason") != LS.STOP_TURN_END],
        "natural_stops": [p for p in probes
                          if not p["metrics"].get("looped")
                          and p.get("stop_reason") == LS.STOP_TURN_END],
    }
    reading = {
        "looped": "артефакт протокола (петля; снимается запретом повторов 4-грамм)",
        "truncated_not_looped": "бюджет (ход оборван лимитом токенов без петли)",
        "natural_stops": "нечем объяснить, кроме модели (дефект)",
    }
    out: dict = {"n": n, "groups": {k: len(v) for k, v in groups.items()},
                 "group_share": {k: round(len(v) / n, 4) for k, v in groups.items()},
                 "reading": reading, "flags": {}}
    for flag in FORMAT_FLAGS:
        total = sum(FORMAT_FLAGS[flag](p) for p in probes) / n
        contrib = {}
        for name, sub in groups.items():
            inside = (sum(FORMAT_FLAGS[flag](p) for p in sub) / len(sub)) if sub else 0.0
            contrib[name] = {"share_inside": round(inside, 4),
                             "contribution": round(len(sub) / n * inside, 4)}
        out["flags"][flag] = {
            "share_all": round(total, 4),
            "contributions": contrib,
            "sum_of_contributions": round(sum(c["contribution"]
                                              for c in contrib.values()), 4),
            "artifact_share_of_total": (None if total == 0 else
                                        round(contrib["looped"]["contribution"] / total, 4)),
            "budget_share_of_total": (None if total == 0 else
                                      round(contrib["truncated_not_looped"]["contribution"]
                                            / total, 4)),
            "real_share_of_total": (None if total == 0 else
                                    round(contrib["natural_stops"]["contribution"] / total, 4)),
        }
    return out


def format_pair(run: Path, prior: Path) -> dict:
    """Пара «greedy против штатного» на одном наборе и одних параметрах.

    Пара обязана быть снята **одним прибором и одним набором промптов**: иначе
    разница между режимами спишется на прибор или на набор. Тождество набора
    проверяется по `prompts_digest`, а не по имени набора.
    """
    std_path = run / "standard_extended.json"
    greedy_path = run / "legacy_greedy_extended.json"
    greedy_fallback = prior / "decoding_greedy.json"
    used_greedy = greedy_path if greedy_path.is_file() else greedy_fallback
    std = format_table(std_path, "sft_resume_6000")
    greedy = format_table(used_greedy, "sft_resume_6000")
    pilot_std = format_table(prior / "decoding_nogram.json", "sft_resume_6000")
    if not std.get("available"):
        std = dict(pilot_std, pilot=True)
    if not greedy.get("available"):
        return {"available": False, "why": f"нет отчёта greedy: {rel(used_greedy)}"}
    rows = []
    for metric in ("looped_share", "natural_stop_share", "unclosed_think_share",
                   "tool_call_share", "median_tokens", "p90_tokens"):
        g, s = greedy.get(metric), std.get(metric)
        rows.append({"metric": metric, "greedy": g, "standard": s,
                     "delta": (None if g is None or s is None else round(s - g, 4))})
    same_set = (greedy.get("prompts_set") == std.get("prompts_set")
                and greedy.get("prompts_digest") == std.get("prompts_digest"))
    same_tool = greedy.get("tool_sha256") == std.get("tool_sha256")
    return {
        "available": True,
        "state": "sft_resume_6000",
        "greedy_report": greedy.get("report"),
        "standard_report": std.get("report"),
        "standard_is_pilot": bool(std.get("pilot")),
        "same_instrument": greedy.get("report", "").startswith(RUN),
        "same_prompt_set": same_set,
        #: Хеш прибора у двух отчётов пары. У прежних отчётов S3ak он разный
        #: (greedy снят прибором `dc4a13a7`, режимы — `87ba4d29`), и это **названо**,
        #: а не замазано: правка прибора между ними касалась чтения языка
        #: (служебные токены исключены из букв), а не генерации, и тождество
        #: генерации между версиями доказывается гейтом `greedy_vs_s3ak` цепочки.
        #: Пока гейт не выполнен, «разница режимов» и «разница приборов» формально
        #: не разделены — это оговорка пары, а не её достоинство.
        "same_tool_sha256": same_tool,
        "tool_sha256": {"greedy": greedy.get("tool_sha256"),
                        "standard": std.get("tool_sha256")},
        "caveat": (None if same_tool else
                   "отчёты пары сняты разными версиями прибора; гейт тождества "
                   "генерации (greedy_vs_s3ak) не выполнен — разница режимов и "
                   "разница приборов формально не разделены"),
        "same_parameters": (greedy.get("max_new_tokens") == std.get("max_new_tokens")
                            and greedy.get("stop_at_turn_end") == std.get("stop_at_turn_end")
                            and greedy.get("prompts_digest") == std.get("prompts_digest")),
        "why_same_matters": "сравнение режимов на разных наборах или параметрах "
                            "списало бы разницу на набор или бюджет, а не на режим",
        "rows": rows,
        "greedy": greedy,
        "standard": std,
        "sample_without_ban": format_table(prior / "decoding_sample.json", "sft_resume_6000"),
        "sample_with_ban": format_table(run / "standard_sample_extended.json",
                                        "sft_resume_6000"),
    }


def artifact_vs_real(pair: dict) -> dict:
    """Сколько прежней «деградации формата» было артефактом протокола, а сколько — дефектом.

    Правило объявлено константами до чисел (`REAL_DEFECT_MIN`,
    `REAL_DEFECT_MAX_AS_ARTIFACT`): признак у **необъяснённых** ходов (дописан и без
    петли) — дефект модели; у зацикленных — артефакт протокола; у оборванных
    лимитом без петли — бюджет. Признак «незакрытый `<think>`» взят как основной:
    именно он в S3aj вырос с 0.2917 до 0.8333.
    """
    if not pair.get("available"):
        return {"available": False, "why": pair.get("why")}
    std = pair["standard"]
    greedy = pair["greedy"]
    dec = greedy.get("decomposition", {}).get("flags", {}).get("unclosed_think", {})
    std_natural = ((std.get("natural_stops") or {}).get("unclosed_think_share"))
    gr_natural = ((greedy.get("natural_stops") or {}).get("unclosed_think_share"))
    if std_natural is None:
        verdict = "not_measured"
        why = "у штатного режима нет дописанных ходов — нечем отделить дефект от обрыва"
    elif std_natural >= REAL_DEFECT_MIN:
        verdict = "real_defect"
        why = (f"у дописанных ходов без петли незакрытый <think> есть в {std_natural} "
               f"(≥ {REAL_DEFECT_MIN}) — это дефект модели, а не артефакт протокола: "
               "петли в этом режиме нет по построению")
    elif std_natural <= REAL_DEFECT_MAX_AS_ARTIFACT:
        verdict = "artifact"
        why = (f"у дописанных ходов без петли незакрытый <think> есть лишь в "
               f"{std_natural} (≤ {REAL_DEFECT_MAX_AS_ARTIFACT}) — прежняя деградация "
               "формата объясняется петлёй и обрывом, а не форматом")
    else:
        verdict = "mixed"
        why = (f"у дописанных ходов без петли незакрытый <think> есть в {std_natural} — "
               "между порогами: часть дефект, часть следствие протокола")
    return {
        "available": True,
        "verdict": verdict,
        "why": why,
        "rule": {
            "real_defect_if": f"доля признака среди дописанных ходов без петли ≥ {REAL_DEFECT_MIN}",
            "artifact_if": f"≤ {REAL_DEFECT_MAX_AS_ARTIFACT}",
            "arithmetic": "вклады групп складываются в долю по всем генерациям "
                          "(looped + truncated_not_looped + natural_stops)",
        },
        "greedy_unclosed_think_all": greedy.get("unclosed_think_share"),
        "greedy_artifact_share": dec.get("artifact_share_of_total"),
        "greedy_budget_share": dec.get("budget_share_of_total"),
        "greedy_real_share": dec.get("real_share_of_total"),
        "greedy_unclosed_think_natural_stops": gr_natural,
        "standard_unclosed_think_all": std.get("unclosed_think_share"),
        "standard_unclosed_think_natural_stops": std_natural,
        "standard_looped_share": std.get("looped_share"),
        "groups": greedy.get("decomposition", {}).get("groups"),
    }


# ─────────────────────── язык ───────────────────────

def language_block(run: Path, prior: Path, state: str = "sft_resume_6000") -> dict:
    """Вердикт ADR-039 по чистым генерациям штатного режима.

    Три числа, которых требует TASK, лежат рядом и названы: `cyr_answer_defined`
    (кириллица по чистым, где ответ есть), `coverage` (доля чистых среди всех
    генераций) и `cyr_answer_with_zeros` (основная метрика, по которой критерий и
    считается). Запас до границы 0.4 даётся по основной метрике **и** по
    определённым сегментам: у S3aj расхождение двух читок одного числа было больше
    0.2, и решать по одному числу нельзя.

    Отчёты:
    * `wide_standard` — замер S3al (104 пробы на состояние) — вердикт;
    * `pilot` — прежний замер штатного режима на 24 пробах (S3ak `decoding_nogram`),
      он приводится рядом, но вердиктом **не является**: 24 < 100.
    """
    wide = load(run / "language_wide_standard.json")
    pilot = load(prior / "decoding_nogram.json")
    src, is_pilot = (wide, False) if wide else (pilot, True)
    if not src:
        return {"available": False, "why": "нет ни штатного замера языка, ни пилота",
                "state": state}
    val = (src.get("states") or {}).get(state) or {}
    probes = val.get("probes") or []
    if not probes:
        return {"available": False, "why": f"нет состояния {state}", "state": state}
    a = val.get("aggregate") or {}
    clean = a.get("clean") or {}
    clean_probes = [p for p in probes
                    if p.get("stop_reason") == LS.STOP_TURN_END
                    and not p["metrics"].get("looped")]
    n = len(probes)
    n_clean = len(clean_probes)
    coverage_guard = (clean.get("cyr_answer") or {}).get("coverage")
    zone = LS.apply_criterion((clean.get("cyr_answer") or {}).get("with_zeros"),
                              (clean.get("cyr_think") or {}).get("with_zeros"),
                              coverage_guard)
    #: Порог выборки S3al — над зонами ADR-039: критерий, посчитанный на 24
    #: генерациях, остаётся жребием, и прибор обязан это сказать, а не показать зону.
    small = n < SAMPLE_FLOOR
    defined_vals = [p["metrics"]["cyr_answer"] for p in clean_probes
                    if p["metrics"].get("cyr_answer") is not None]
    ci_defined = LS.bootstrap_ci(defined_vals)
    margin = (clean.get("cyr_answer") or {}).get("with_zeros")
    return {
        "available": True,
        "source": ("замер S3al (штатный режим, wide)" if not is_pilot else
                   "пилот: прежний замер штатного режима на 24 пробах (S3ak)"),
        "is_pilot": is_pilot,
        "state": state,
        "n": n,
        "n_clean": n_clean,
        "coverage": clean.get("share_of_all"),
        "coverage_note": "доля чистых (ход дописан, петли нет) среди всех генераций "
                         "состояния — это число TASK",
        "cyr_answer_coverage_of_clean": coverage_guard,
        "cyr_answer_coverage_note": "доля чистых генераций, у которых ответная часть "
                                    "вообще есть; это страж критерия ADR-039 "
                                    f"(порог {LS.ANSWER_COVERAGE_FLOOR}), а не доля чистых",
        "cyr_answer_clean": (clean.get("cyr_answer") or {}).get("defined_only"),
        "cyr_answer_clean_with_zeros": (clean.get("cyr_answer") or {}).get("with_zeros"),
        "cyr_think": (clean.get("cyr_think") or {}).get("with_zeros"),
        "margin_to_0.4": (None if margin is None
                          else round(margin - LS.CRIT_ANSWER_REFUTE_MAX, 4)),
        "margin_to_0.4_defined_only": (None if not defined_vals else
                                       round(sum(defined_vals) / len(defined_vals)
                                             - LS.CRIT_ANSWER_REFUTE_MAX, 4)),
        "ci95": clean.get("ci95"),
        "ci95_defined_only": ci_defined,
        "zone": zone["zone"],
        "zone_why": zone["why"],
        "verdict_adr039": (ZONE_SMALL_SAMPLE if small else zone["zone"]),
        "verdict_why": (f"выборка {n} < {SAMPLE_FLOOR}: вердикт ADR-039 не выносится "
                        "(шаг одной пробы больше запаса до границы)" if small
                        else zone["why"]),
        "decoding": (src.get("protocol") or {}).get("decoding"),
        "decoding_protocol": (src.get("protocol") or {}).get("decoding_protocol"),
        "checkpoint": val.get("checkpoint"),
        "checkpoint_sha256": val.get("checkpoint_sha256"),
        "report": rel(run / "language_wide_standard.json") if not is_pilot
                  else rel(prior / "decoding_nogram.json"),
        "all_readings": {
            "cyr_answer_with_zeros": (a.get("cyr_answer") or {}).get("with_zeros"),
            "cyr_answer_defined": (a.get("cyr_answer") or {}).get("defined_only"),
            "cyr_think": (a.get("cyr_think") or {}).get("with_zeros"),
            "looped_share": a.get("looped_share"),
            "coverage": (a.get("cyr_answer") or {}).get("coverage"),
        },
    }


def language_by_state(run: Path, prior: Path) -> dict:
    """То же по каждому состоянию штатного замера: не только решающее.

    Пока замера нет, берутся состояния пилота — с пометкой, что это пилот: пустой
    блок читался бы как «состояний не было», а состояния были.
    """
    src = load(run / "language_wide_standard.json") or load(prior / "decoding_nogram.json")
    if not src:
        return {}
    return {st: language_block(run, prior, st) for st in (src.get("states") or {})}


# ─────────────────────── повторы в данных ───────────────────────

def tests_block(run: Path) -> dict:
    """Прогон тестов приборов: сколько прошло и есть ли красное **вне** известного.

    «Известный красный» берётся из свода S3aj, а не переписывается сюда: список
    «что уже было красным до дельты» — утверждение о прошлом, и его место в
    прошлом артефакте. Иначе «новых нарушений нет» можно было бы получить,
    дописав отказ в свой же список.
    """
    p = run / "tool_tests.log"
    if not p.is_file():
        return {"available": False, "why": f"нет {rel(p)}"}
    lines = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    tally = lines[-1] if lines else ""
    failures = [ln.strip().lstrip("- ").strip() for ln in lines
                if ln.strip().startswith("- ")]
    prev = load(CASE / "evidence/s3aj-format-validity.json")
    known = ((prev or {}).get("tests") or {}).get("failures") or []
    return {
        "available": True,
        "log": rel(p),
        "log_sha256": sha256_file(p),
        "tally": tally,
        "failures": failures,
        "known_red": known,
        "known_red_source": "evidence/s3aj-format-validity.json",
        "failures_outside_known_red": [f for f in failures if f not in known],
        #: Сколько проверок добавлено этой дельтой: считается по секции 22, а не по
        #: подстроке в тексте — «тестов стало больше» должно быть проверяемым.
        "s3al_checks": sum(1 for ln in lines[lines.index("== 22. probe_language_split: "
                                                        "штатный режим декодирования (S3al) =="):]
                           if ln.startswith("  ok"))
        if any(ln.startswith("== 22.") for ln in lines) else 0,
    }


def fitness_block(run: Path) -> dict:
    """Гейт `fitness_check`: нарушения в этой ревизии против нарушений на HEAD.

    Сравнение с **базой**, а не «ноль нарушений»: часть правил кейса красная по
    состоянию (память стенда занята идущей стадией; отчёт результата появится
    только на S4), и требование дельты — не «зелено», а «не хуже, чем было».
    База снимается тем же гейтом на дереве HEAD, выгруженном `git archive`
    (рабочее дерево при этом не трогается).
    """
    cur = load(run / "fitness_gate.json")
    base = load(run / "fitness_gate_baseline_head.json")
    if not cur:
        return {"available": False, "why": f"нет {rel(run / 'fitness_gate.json')}"}
    rules = [i.get("rule") for i in cur.get("issues") or []]
    base_rules = [i.get("rule") for i in (base or {}).get("issues") or []]
    return {
        "available": True,
        "command": "arch-be control check --json .",
        "summary": cur.get("summary"),
        "passed": cur.get("passed"),
        "violations": rules,
        "baseline": {
            "how": "тот же гейт на дереве HEAD: git archive HEAD | tar -x -C <tmp>; "
                   "скопирован только .arch-handoff/ (он не в git)",
            "available": bool(base),
            "summary": (base or {}).get("summary"),
            "violations": base_rules,
        },
        "new_violations": [r for r in rules if r not in base_rules],
        "no_new_violations": bool(base) and not [r for r in rules if r not in base_rules],
        "why_state_dependent": "AD-9 (память стенда) читает **живую** стадию на GB10: "
                               "в одном и том же дереве проверка даёт то 32.5 ГБ (отказ), "
                               "то 60.4–66.3 ГБ (можно стартовать) — это состояние "
                               "стенда, а не результат дельты",
        "reports": [rel(run / "fitness_gate.json"),
                    rel(run / "fitness_gate_baseline_head.json")],
    }


def loops_block(run: Path) -> dict:
    """Повторы в данных — с проверкой, что вердикт отчёта сходится с прибором.

    Проверка нужна потому, что отчёт пишется **один раз** и долго: числа в нём
    записаны, а правило вердикта живёт в коде прибора. Если правило после этого
    поправить, отчёт останется со старым вердиктом и будет выглядеть свежим.
    Поэтому свод пересчитывает вердикт по записанным числам **функцией прибора** и
    сравнивает: расхождение — отказ, а не «две версии правды рядом».
    """
    d = load(run / "loops_in_data.json")
    if not d:
        return {"available": False,
                "why": f"нет отчёта {rel(run / 'loops_in_data.json')}"}
    recheck: dict = {"available": False}
    if d.get("length_controlled_duplication") and d.get("provenance"):
        try:
            import analyze_loop_repetition as LR
            fresh = LR.verdict(d["length_controlled_duplication"], d["provenance"])
            stored_keys = [r["key"] for r in (d.get("verdict") or {}).get("rules_evaluated", [])]
            fresh_keys = [r["key"] for r in fresh["rules_evaluated"]]
            stored_fired = [r["key"] for r in (d.get("verdict") or {}).get("rules_evaluated", [])
                            if r.get("fired")]
            fresh_fired = [r["key"] for r in fresh["rules_evaluated"] if r["fired"]]
            recheck = {
                "available": True,
                "same_rules": stored_keys == fresh_keys,
                "same_fired": stored_fired == fresh_fired,
                "stored_fired": stored_fired,
                "recomputed_fired": fresh_fired,
                "why": "вердикт отчёта пересчитан по записанным в нём числах текущей "
                       "ревизией прибора: отчёт пишется один раз, а правило вердикта "
                       "живёт в коде",
            }
        except Exception as exc:  # noqa: BLE001 — причину отказа надо назвать, а не упасть
            recheck = {"available": False, "why": f"{type(exc).__name__}: {exc}"}
    return {
        "available": True,
        "schema": d.get("schema"),
        "tool": d.get("tool") or "tools/analyze_loop_repetition.py",
        "tool_sha256": d.get("tool_sha256"),
        "verdict_recheck": recheck,
        "report": rel(run / "loops_in_data.json"),
        "dataset": (d.get("method") or {}).get("dataset"),
        "length_controlled": d.get("length_controlled_duplication"),
        "provenance": d.get("provenance"),
        "verdict": d.get("verdict"),
        "caveats": d.get("caveats"),
        "reading": "подготовка решения, а не решение: прибор даёт числа, на которых "
                   "решение о работе с данными принимается или отклоняется",
    }


# ─────────────────────── свод ───────────────────────

def open_questions_block(lang: dict, missing: list[str]) -> list[str]:
    """Открытые вопросы — **по состоянию чисел**, а не списком из прошлой редакции.

    Свод пересобирается (в S3am — уже с замером языка), и вопрос, на который замер
    ответил, обязан из отчёта уйти: список, оставшийся от прежнего прогона,
    выглядел бы как «вопросы не закрыты», хотя числа лежат рядом. Вопрос про
    агентную пробу закрыт ADR-041 и дельтой S3am — он переезжает в
    ``resolved_questions``, а не остаётся в открытых.
    """
    out = []
    if not lang.get("available") or lang.get("is_pilot") or (lang.get("n") or 0) < SAMPLE_FLOOR:
        why = ("пилотом (S3ak), а не замером S3al" if lang.get("is_pilot")
               else "замер не состоялся или выборка ниже порога")
        out.append(f"вердикт по языку при n ≥ {SAMPLE_FLOOR} не вынесен: числа получены "
                   f"{why} (missing: {', '.join(missing) or '—'})")
    if lang.get("verdict_adr039") == ZONE_SMALL_SAMPLE:
        out.append("выборка меньше порога: шаг одной пробы больше запаса до границы — "
                   "вердикт остаётся жребием, сколько бы зон ни было заполнено")
    out += [
        "порог «реальный дефект» 0.5 взят тем же, что граница «русского ответа» в "
        "ADR-039; если у формата должна быть своя граница, её надо объявить отдельно",
    ]
    return out


def resolved_questions_block(lang: dict) -> list[dict]:
    """Вопросы, закрытые **после** первой редакции свода (с указанием чем)."""
    out = [
        {"question": "нужен ли отдельный ADR на штатный режим декодирования",
         "closed_by": "ADR-041 (коммит a091f23): штатный режим распространён на все "
                      "пробы стадии, включая агентную"},
        {"question": "распространяется ли штатный режим на агентную пробу "
                     "(`tools/passrate_probe.py`) — прибор декодировал своим циклом",
         "closed_by": "ADR-041 п.1–2; исполнено дельтой S3am: запрет повторов 4-грамм "
                      "стал умолчанием агентной пробы, прогон без него отклоняется "
                      "(код 1), agentic-база переснята — evidence/s3am-agentic-baseline.json"},
    ]
    if lang.get("available") and not lang.get("is_pilot") and (lang.get("n") or 0) >= SAMPLE_FLOOR:
        out.append({"question": "каков вердикт ADR-039 при n ≥ 100 в штатном режиме",
                    "closed_by": f"замер S3al (wide, n={lang.get('n')}): зона "
                                 f"{lang.get('verdict_adr039')}, запас до границы 0.4 "
                                 f"{lang.get('margin_to_0.4')}"})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=RUN)
    ap.add_argument("--prior-run", default=PRIOR_RUN)
    ap.add_argument("--out", default="evidence/s3al-decoding-protocol.json")
    ap.add_argument("--state", default="sft_resume_6000")
    ap.add_argument("--device-note", default=None,
                    help="почему замер не состоялся, если устройство недоступно "
                         "(пишется в отчёт как есть)")
    args = ap.parse_args(argv)

    run, prior = CASE / args.run, CASE / args.prior_run
    if not run.is_dir():
        note(f"NOT-VERIFIED: нет каталога прогона {run}")
        return EXIT_NOT_VERIFIED
    if not prior.is_dir():
        note(f"NOT-VERIFIED: нет каталога прежнего прогона {prior}")
        return EXIT_NOT_VERIFIED

    proto = protocol_block()
    #: Отказ, если штатный режим в приборе снят: тогда весь свод мерил бы не то.
    if proto["self_check"]["default_resolves_to"] != LS.STANDARD_NO_REPEAT_NGRAM:
        note("отказ: прибор не применяет штатный режим по умолчанию — "
             "свод построен на неверной посылке")
        return EXIT_FAIL
    if not proto["self_check"]["run_without_ban_is_refused"]:
        note("отказ: прибор не отклоняет прогон без запрета повторов — "
             "штатный режим не закреплён")
        return EXIT_FAIL

    #: Отчёт штатного режима, помеченный непригодным для выводов (снят с
    #: --legacy-decoding), в свод языка не берётся: сначала это надо заметить, а не
    #: получить «вердикт» по запрещённому режиму.
    wide = load(run / "language_wide_standard.json") or {}
    dp = ((wide.get("protocol") or {}).get("decoding_protocol") or {})
    if wide and dp and dp.get("allowed_for_conclusions") is False:
        note("отказ: отчёт языка снят в режиме, помеченном непригодным для выводов "
             "(allowed_for_conclusions=false)")
        return EXIT_FAIL

    pair = format_pair(run, prior)
    lang = language_block(run, prior, args.state)
    #: Полнота дельты — считается по числам, а не заявляется: без замера языка при
    #: n ≥ 100 дельта **не** полная, сколько бы блоков ни было заполнено. Это то
    #: место, где свод обязан быть строже к себе, чем читатель.
    missing = []
    if not lang.get("available"):
        missing.append("нет замера языка при штатном режиме")
    elif lang.get("is_pilot"):
        missing.append("язык измерен пилотом (S3ak), а не замером S3al")
    if (lang.get("n") or 0) < SAMPLE_FLOOR:
        missing.append(f"выборка {lang.get('n')} < {SAMPLE_FLOOR}")
    if any(not g.get("byte_identical") for g in proto["gates"].values()):
        missing.append("гейты тождества прибора не подтверждены")
    out = {
        "schema": "s3al-decoding-protocol/1",
        "completeness": {
            "delta_status": "partial" if missing else "complete",
            "missing": missing,
            "why": "статус выведен из чисел: вердикт по языку при n ≥ 100 — условие "
                   "полноты дельты, и без него расчёт остаётся частичным, даже если "
                   "все прочие блоки заполнены",
        },
        "stage": "S3al — штатный режим декодирования (запрет повторов 4-грамм) и "
                 "вердикт по языку на чистых генерациях",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "measured",
        "question": "какие выводы о языке и формате SFT-состояний держатся, когда "
                    "петля снята штатным режимом декодирования",
        "protocol": proto,
        "language": lang,
        "language_by_state": language_by_state(run, prior),
        "format": {
            "greedy_vs_standard": pair,
            "artifact_share": artifact_vs_real(pair),
            "real_defects": {
                "rule": "дефект считается реальным, если признак есть у ходов, "
                        "которые нечем объяснить, кроме модели (ход дописан, петли нет)",
                "primary_flag": "unclosed_think",
                "verdict": artifact_vs_real(pair).get("verdict"),
                "numbers": {
                    "greedy_all": pair.get("greedy", {}).get("unclosed_think_share"),
                    "standard_all": pair.get("standard", {}).get("unclosed_think_share"),
                    "greedy_natural_stops": (pair.get("greedy", {}).get("natural_stops")
                                             or {}).get("unclosed_think_share"),
                    "standard_natural_stops": (pair.get("standard", {}).get("natural_stops")
                                               or {}).get("unclosed_think_share"),
                },
            },
        },
        "loops_in_data": loops_block(run),
        "fitness_gate": fitness_block(run),
        "tests": tests_block(run),
        "blocked": ({"what": "замер языка при штатном режиме на n ≥ 100",
                     "why": args.device_note,
                     "unblocks": "локальное устройство снова принимает CUDA-контекст "
                                 "(`nvidia-smi -i 0 -r` не поможет: карта объявлена "
                                 "primary, сброс запрещён; помогает перезагрузка "
                                 "модулей `nvidia_uvm` или машины) — цепочка "
                                 "runs/s3al-decoding-protocol-20260918/chain.sh "
                                 "готова и запускается одной командой",
                     "resume": [
                         "bash runs/s3al-decoding-protocol-20260918/chain.sh",
                         "python3 tools/assemble_s3al_evidence.py "
                         "--out evidence/s3al-decoding-protocol.json",
                     ]}
                    if args.device_note else None),
        "artifacts": [
            {"path": rel(p), "sha256": sha256_file(p),
             "role": role, "exists": p.is_file()}
            for role, p in [
                ("прибор: штатный режим и отказ на прогон без запрета повторов",
                 CASE / "tools/probe_language_split.py"),
                ("прибор: повторы в данных (длина + происхождение юнитов петель)",
                 CASE / "tools/analyze_loop_repetition.py"),
                ("свод (этот файл собран им)", CASE / "tools/assemble_s3al_evidence.py"),
                ("тесты приборов (зелёный и красный путь)", CASE / "tools/tests/run_tool_tests.sh"),
                ("план стадии: раздел 11", CASE / "docs/specs/SFT-STAGE-PLAN.md"),
                ("цепочка замеров (объявлена целиком, ждёт устройства)",
                 run / "chain.sh"),
                ("числа повторов в данных", run / "loops_in_data.json"),
                ("проверка подмены устройства (CPU отклонён числом)",
                 run / "device_check_cpu.json"),
                ("прогон тестов", run / "tool_tests.log"),
            ]
        ],
        "open_questions": open_questions_block(lang, missing),
        "resolved_questions": resolved_questions_block(lang),
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        note(f"свод: {args.out}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
