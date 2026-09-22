#!/usr/bin/env python3
"""S3aj — свод: снят ли артефакт усечения, и что тогда с форматом и агентностью.

Зачем отдельный свод. Вопрос S3aj — не «какие числа у формата», а «можно ли по
прежним числам вообще что-то решать». Базовые замеры S3ai дали
`truncated_share = 1.0`, то есть **все** генерации были обрезаны, и рост
«незакрытых `<think>`» (0.4583 → 0.7917) мог быть свойством замера, а не модели.
Разбор (`probe_language_split`, S3aj) показал, что обрыв складывается из **трёх**
разных причин, и свод обязан их развести, а не сложить в одно число:

1. **Прибор не останавливался на конце хода** — веса базы несут
   `eos_token_id = 151643`, а ход в формате v12 кончается токеном 151645
   (`<|im_end|>`); генерация переписывала свой конец хода и уходила в следующий
   круг. Лечится флагом `--stop-at-turn-end`.
2. **Бюджет токенов был мал** — 1024 токена против медианной длины хода самой
   модели. Лечится бюджетом `FULL_MAX_NEW_TOKENS = 4096`.
3. **Вырождение (зацикливание)** — генерация уходит в повторы и не кончается
   никаким бюджетом. Не лечится ни тем, ни другим: это свойство модели.

Три причины требуют трёх замеров, и все три в своде есть:

* `truncated_measure` — прежний замер S3ai (1024, без остановки): то, по чему
  нельзя было решать;
* `recut_measure` — тот же замер, перечитанный правилом «ход кончается на первом
  `<|im_end|>`»: изолирует причину 1 при **том же** бюджете (greedy префиксно
  детерминирован, поэтому перечитанный текст равен тексту прогона с исправленной
  остановкой);
* `full_measure` — новый замер (4096, с остановкой): снимает причины 1 и 2.

Вердикт «артефакт / деградация» считается **по полному замеру** и при этом
отдельно смотрит на **дописанные** ходы: если незакрытые `<think>` остались
только среди обрезанных и все они зациклены, то формат тут ни при чём — причина
названа третьей, и это разные решения (работа с форматом против работы с
вырождением). Правило вердикта объявлено константами ниже, до того как числа
полного замера стали известны.

Коды возврата::

    0 — свод собран
    1 — отказ: вход есть, но не тот (тождество прибора не подтвердилось, тесты FAIL,
        нет обязательного замера)
    2 — NOT-VERIFIED: данных нет (нет полного замера, нет отчётов прибора)
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

import probe_language_split as LS     # noqa: E402  (критерий, разбор, свод — не копия)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Пороги вердикта о формате — объявлены ДО того, как числа полного замера стали
#: известны, ровно как пороги критерия ADR-039 в приборе.
#:   * «артефакт» — доля незакрытых `<think>` в полном замере упала до 0.2 и ниже:
#:     рост из прежнего замера объясняется усечением, а не потерей формата;
#:   * «деградация» — осталась на 0.5 и выше: формат не восстановился ни бюджетом,
#:     ни исправленной остановкой;
#:   * между — «смешанно»: обе причины вносят вклад, и решение требует разбора
#:     по причинам (см. `third_cause`), а не одним словом.
FMT_ARTIFACT_MAX = 0.2
FMT_DEGRADATION_MIN = 0.5
#: Дописанные ходы — опора, на которую усечение не влияет вовсе. Если у них
#: незакрытых `<think>` не больше порога артефакта, остаток объясняется не
#: форматом, а вырождением: зацикленная генерация не кончается и не закрывает
#: блоки. Это разные диагнозы и разные решения.
FMT_NATURAL_CLEAN_MAX = 0.2


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(p) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def merge_states(files: list[str], aliases: dict[str, str]) -> tuple[dict, list[dict]]:
    """Слить состояния из нескольких отчётов прибора в один словарь.

    Прибор пишет по отчёту на прогон, а состояние могло мериться в разных
    прогонах (S3ai мерил SFT-resume отдельным файлом). Слияние — по имени
    состояния; `--alias OLD=NEW` переименовывает при слиянии, потому что имя
    содержит шаг чекпойнта (`sftresume_4000`), а состояние — это стадия
    (`sft_resume`), и сопоставлять их «на глаз» свод не имеет права.
    Совпадение имён после переименования — отказ, а не «взяли последнее».
    """
    merged: dict = {}
    inputs: list[dict] = []
    for f in files:
        p = Path(f)
        if not p.is_file():
            continue
        d = load_json(p)
        if d.get("schema") != "probe-language-split/1":
            note(f"  {p}: не отчёт прибора (schema={d.get('schema')!r}) — пропуск")
            continue
        inputs.append({"file": str(p), "sha256": sha256_file(p),
                       "max_new_tokens": (d.get("protocol") or {}).get("max_new_tokens"),
                       "stop_at_turn_end": (d.get("protocol") or {}).get("stop_at_turn_end"),
                       "states": sorted(d.get("states") or {})})
        for tag, val in (d.get("states") or {}).items():
            if "probes" not in val:
                continue
            key = aliases.get(tag, tag)
            if key in merged and merged[key]["source"] != str(p):
                note(f"  ОТКАЗ: состояние {key} встречается в двух отчётах — "
                     "сопоставление неоднозначно")
                raise SystemExit(EXIT_FAIL)
            rec = dict(val)
            rec["source"] = str(p)
            merged[key] = rec
    return merged, inputs


def natural_lengths(state: dict) -> dict:
    """Длины **дописанных** ходов — отдельно от общих.

    Нужны потому, что общая медиана у состояния, половина генераций которого
    упёрлась в бюджет, равна бюджету и о модели не говорит ничего. Медиана
    дописанных ходов отвечает на вопрос, который и задавался: сколько токенов
    ходу нужно на самом деле. Разница вышла решающей: у CPT-финала 1 441, у
    SFT-состояний — десятки токенов (ход обрывается почти сразу), то есть у SFT
    дефицит **не** в бюджете.
    """
    toks = [int(p.get("n_new_tokens") or 0) for p in (state.get("probes") or [])
            if p.get("stop_reason") == LS.STOP_TURN_END]
    if not toks:
        return {"n": 0}
    return {"n": len(toks), "median": LS._pct(toks, 0.5), "p90": LS._pct(toks, 0.9),
            "max": max(toks), "min": min(toks)}


def state_view(tag: str, state: dict) -> dict:
    """Состояние в форме, по которой считается вердикт: метрики + причины конца."""
    agg = state.get("aggregate") or LS.aggregate(state.get("probes", []))
    stop = agg.get("stop") or {}
    return {
        "state": tag,
        "n": agg.get("n"),
        "cyr_think": (agg.get("cyr_think") or {}).get("with_zeros"),
        "cyr_answer": (agg.get("cyr_answer") or {}).get("with_zeros"),
        "cyr_answer_defined_only": (agg.get("cyr_answer") or {}).get("defined_only"),
        "answer_coverage": (agg.get("cyr_answer") or {}).get("coverage"),
        "cyr_answer_prose": (agg.get("cyr_answer_prose") or {}).get("with_zeros"),
        "mode_share_think": agg.get("mode_share_think"),
        "mode_share_tool_call": agg.get("mode_share_tool_call"),
        "unclosed_think_share": agg.get("unclosed_think_share"),
        "unclosed_think_share_natural": (stop.get("natural") or {}).get("unclosed_think_share"),
        "unclosed_think_share_truncated": (stop.get("truncated") or {}).get("unclosed_think_share"),
        "truncated_share": agg.get("truncated_share"),
        "natural_stop_share": stop.get("natural_stop_share"),
        "where_truncated": stop.get("where_truncated") or {},
        "looped_share": agg.get("looped_share"),
        "looped_share_truncated": (stop.get("truncated") or {}).get("looped_share"),
        "looped_share_natural": (stop.get("natural") or {}).get("looped_share"),
        "lengths": agg.get("lengths") or {},
        "lengths_natural": natural_lengths(state),
        "checkpoint": state.get("checkpoint"),
        "checkpoint_sha256": state.get("checkpoint_sha256"),
        "source": state.get("source"),
    }


def verdict_format(full: dict[str, dict]) -> dict:
    """Артефакт или деградация — по правилу, объявленному константами выше.

    Считается **по полному замеру** (4096 + остановка на конце хода): только он
    свободен от обеих измерительных причин. Отдельно называются две опоры, без
    которых одно число читалось бы неверно:

    * доля незакрытых `<think>` **среди дописанных ходов** — на неё усечение не
      влияет вовсе;
    * доля зацикленных среди обрезанных — если остаток обрывов зациклен, то
      причина не «формат потерян», а «генерация не кончается».
    """
    rows, zones = [], {}
    for tag, st in full.items():
        u = st["unclosed_think_share"]
        nat = st["unclosed_think_share_natural"]
        loops = st["looped_share_truncated"]
        if u is None:
            zone = "not_measured"
        elif u <= FMT_ARTIFACT_MAX:
            zone = "artifact"
        elif u >= FMT_DEGRADATION_MIN:
            zone = "degradation"
        else:
            zone = "mixed"
        zones[tag] = zone
        rows.append({
            "state": tag,
            "unclosed_think_share_full": u,
            "unclosed_think_share_natural": nat,
            "unclosed_think_share_truncated": st["unclosed_think_share_truncated"],
            "natural_stop_share": st["natural_stop_share"],
            "looped_share": st["looped_share"],
            "looped_share_truncated": loops,
            "zone": zone,
        })

    #: Итог берётся по **худшему** состоянию SFT-линии: вопрос был не «бывает ли
    #: хорошо», а «исчез ли рост незакрытых блоков». CPT-финал — опора направления
    #: (он и в прежнем замере был лучше), поэтому в итог не входит.
    sft = {t: z for t, z in zones.items() if t not in ("cfinal", "base")}
    if not sft:
        overall = "not_measured"
    elif all(z == "artifact" for z in sft.values()):
        overall = "artifact"
    elif any(z == "degradation" for z in sft.values()):
        overall = "degradation"
    else:
        overall = "mixed"

    #: Третья причина: остались ли незакрытые блоки **только** в обрезанных и
    #: зацикленных генерациях. Тогда «формат» — не то, что сломалось.
    worst = max((r for r in rows if r["state"] in sft),
                key=lambda r: (r["unclosed_think_share_full"] or 0), default=None)
    third = None
    if worst and worst["unclosed_think_share_full"] is not None:
        nat = worst["unclosed_think_share_natural"]
        if (nat is not None and nat <= FMT_NATURAL_CLEAN_MAX
                and (worst["looped_share_truncated"] or 0) >= 0.5
                and worst["unclosed_think_share_full"] > FMT_ARTIFACT_MAX):
            third = ("вырождение: у дописанных ходов формат цел "
                     f"({nat} ≤ {FMT_NATURAL_CLEAN_MAX}), а весь остаток обрывов "
                     "приходится на зацикленные генерации — работа нужна не с "
                     "форматом, а с вырождением")

    return {
        "artifact_or_degradation": overall,
        "rule": (f"полный замер: незакрытых <think> ≤ {FMT_ARTIFACT_MAX} — артефакт; "
                 f"≥ {FMT_DEGRADATION_MIN} — деградация; между — смешанно; "
                 "итог по худшему состоянию SFT-линии; опоры — дописанные ходы и "
                 "зацикленность названы рядом"),
        #: Числа, по которым вынесен вердикт, — по состоянию, чтобы решение можно
        #: было проверить, а не принять на слово.
        "evidence": rows,
        "zones": zones,
        "third_cause": third,
        "compared": ("полный замер (4096 + остановка на конце хода) против усечённого "
                     "(1024 без остановки); перечитанный замер (1024 + правило конца "
                     "хода) разводит причину «прибор» и причину «бюджет»"),
    }


def verdict_adr039(full: dict[str, dict], final_state: str) -> dict:
    """Критерий ADR-039 — тем же кодом прибора, что и в S3ai (не пересказ)."""
    st = full.get(final_state)
    if st is None:
        return {"zone": "not_measured",
                "why": f"состояния {final_state} в полном замере нет"}
    crit = LS.apply_criterion(st["cyr_answer"], st["cyr_think"], st["answer_coverage"])
    #: Вторая, **прозрачная** читка того же числа: зона по одним порогам, без
    #: стража покрытия. Нужна потому, что здесь они расходятся: ответной части нет
    #: у большинства генераций, и страж говорит «недостаточно данных», тогда как по
    #: порогам вышло бы «опровергнуто». Показать обе читки честнее, чем спрятать
    #: одну за стражем: решение о лечении данных принимается человеком.
    plain = LS.apply_criterion(st["cyr_answer"], st["cyr_think"])
    return {"state": final_state, "cyr_answer": st["cyr_answer"],
            "cyr_think": st["cyr_think"], "answer_coverage": st["answer_coverage"],
            "thresholds_only_zone": plain["zone"],
            "thresholds_only_why": plain["why"],
            "criteria": {"answer_ru_min": LS.CRIT_ANSWER_RU_MIN,
                         "think_en_max": LS.CRIT_THINK_EN_MAX,
                         "answer_refute_max": LS.CRIT_ANSWER_REFUTE_MAX,
                         "answer_coverage_floor": LS.ANSWER_COVERAGE_FLOOR},
            **crit}


#: Что считается «своими» проверками дельты в журнале тестов: строка «итого» —
#: из журнала, а не пересказом; проверки других дельт в вердикт не входят.
OWN_CHECKS = ("probe_language_split", "assemble_s3aj_evidence")


def tests_verdict(log_path, known_red: list[str]) -> dict:
    if log_path is None or not Path(log_path).is_file():
        return {"available": False, "why": f"журнала тестов нет: {log_path}"}
    text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tally = next((l for l in reversed(lines) if l.startswith("итого:")), None)
    if tally is None:
        return {"available": True, "tally": None, "passed": False,
                "why": "в журнале нет строки «итого» — прогон не завершён"}
    ours = [l for l in lines
            if any(k in l for k in OWN_CHECKS) and l.strip().startswith(("ok", "FAIL"))]
    bad = [l.strip() for l in ours if l.strip().startswith("FAIL")]
    failures = []
    if "FAIL=" in tally and "FAIL=0" not in tally:
        failures = [l.strip()[2:] for l in lines[lines.index(tally):]
                    if l.strip().startswith("- ")]
    unknown = [f for f in failures if not any(k in f for k in known_red)]
    return {"available": True, "tally": tally, "passed": not bad and not unknown,
            "s3aj_checks": len(ours),
            "s3aj_failed": bad, "failures": failures,
            "failures_outside_known_red": unknown,
            "log": str(log_path), "log_sha256": sha256_file(Path(log_path)),
            "checks": [l.strip() for l in ours]}


def identity_check(identity_run, reference) -> dict:
    """Тождество прибора после правок S3aj — байтами ответов, а не обещанием.

    Правки S3aj добавляют поля и флаг, но протокол по умолчанию не меняют. Это
    проверяется числом: прогон ядра (5 промптов, 384 токена, **без** остановки на
    конце хода) обязан побайтово воспроизвести ответы, снятые тем же прибором до
    правок. Расхождение хотя бы в одном ответе = отказ: значит, прибор мерит не
    то же самое, и сопоставимость с S3ai/S3ab потеряна.
    """
    if not identity_run or not Path(identity_run).is_file():
        return {"available": False, "identical": None,
                "why": (f"прогона тождества нет: {identity_run} — байтовое сравнение не "
                        "выполнено (это НЕ «совпало»)")}
    run = load_json(identity_run)
    state = "cfinal"
    mine = (run.get("states") or {}).get(state, {})
    if not mine.get("probes"):
        return {"available": False, "why": f"в прогоне тождества нет проб {state}"}
    out = {"available": True, "run": str(identity_run),
           "run_sha256": sha256_file(Path(identity_run)),
           "tool_sha256": run.get("tool_sha256"),
           "protocol": {k: (run.get("protocol") or {}).get(k)
                        for k in ("prompts_set", "prompts_digest", "max_new_tokens",
                                  "stop_at_turn_end", "decoding")},
           "compared": "байты ответов и метрики сегментов"}
    if not reference or not Path(reference).is_file():
        out["reference"] = None
        out["why"] = (f"опубликованного прогона тождества нет: {reference} — "
                      "байтовое сравнение не выполнено (это НЕ «совпало»)")
        out["identical"] = None
        return out
    ref = load_json(reference)
    theirs = (ref.get("states") or {}).get(state, {}).get("probes", [])
    a = [p["response"] for p in mine["probes"]]
    b = [p.get("response") for p in theirs]
    same = sum(1 for x, y in zip(a, b) if x == y)
    metric_mismatch = []
    for p, q in zip(mine["probes"], theirs):
        for k in ("cyr_think", "cyr_answer", "unclosed_think", "has_tool_call"):
            if p["metrics"].get(k) != (q.get("metrics") or {}).get(k):
                metric_mismatch.append({"probe": p["tag"], "field": k,
                                        "now": p["metrics"].get(k),
                                        "before": (q.get("metrics") or {}).get(k)})
    out.update({"reference": str(reference),
                "reference_sha256": sha256_file(Path(reference)),
                "n_compared": min(len(a), len(b)),
                "bytes_identical_responses": same,
                "metric_mismatches": metric_mismatch,
                "identical": bool(a and b and same == len(a) == len(b)
                                  and not metric_mismatch)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full-run", required=True,
                    help="полный замер S3aj (4096 + остановка на конце хода)")
    ap.add_argument("--truncated-run", action="append", default=[],
                    help="прежние замеры S3ai (1024, без остановки); повторяемый")
    ap.add_argument("--recut-run", action="append", default=[],
                    help="те же замеры, перечитанные правилом конца хода; повторяемый")
    ap.add_argument("--alias", action="append", default=[], metavar="OLD=NEW",
                    help="переименовать состояние при слиянии (имя содержит шаг)")
    ap.add_argument("--identity-run", default=None,
                    help="прогон ядра (5 промптов, 384) после правок S3aj")
    ap.add_argument("--identity-reference", default=None,
                    help="тот же прогон ядра, снятый ДО правок S3aj")
    ap.add_argument("--tests-log", default=None, help="журнал tools/tests/run_tool_tests.sh")
    ap.add_argument("--known-red", action="append", default=[],
                    help="известные красные, воспроизводимые вне дельты")
    ap.add_argument("--final-state", default="sft_resume",
                    help="состояние, по которому выносится вердикт ADR-039")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    aliases = {}
    for spec in args.alias:
        old, _, new = spec.partition("=")
        if old and new:
            aliases[old] = new

    if not Path(args.full_run).is_file():
        note(f"NOT-VERIFIED: полного замера нет: {args.full_run}")
        return EXIT_NOT_VERIFIED
    full_raw = merge_states([args.full_run], aliases)[0]
    if not full_raw:
        note(f"NOT-VERIFIED: в {args.full_run} нет состояний прибора")
        return EXIT_NOT_VERIFIED
    trunc_raw, trunc_inputs = merge_states(args.truncated_run, aliases)
    recut_raw, recut_inputs = merge_states(args.recut_run, aliases)

    full = {t: state_view(t, s) for t, s in full_raw.items()}
    trunc = {t: state_view(t, s) for t, s in trunc_raw.items()}
    recut = {t: state_view(t, s) for t, s in recut_raw.items()}

    vfmt = verdict_format(full)
    vadr = verdict_adr039(full, args.final_state)
    tests = tests_verdict(args.tests_log, args.known_red)
    ident = identity_check(args.identity_run, args.identity_reference)

    refusals = []
    if ident.get("available") and ident.get("identical") is False:
        refusals.append("тождество прибора не подтвердилось: ответы ядра разошлись "
                        "с прогоном до правок S3aj — числа несопоставимы")
    if tests.get("available") and not tests.get("passed", False):
        refusals.append("тесты не PASS: " + "; ".join(
            tests.get("s3aj_failed") or tests.get("failures_outside_known_red") or ["?"]))

    artifacts = []
    for tag, st in full.items():
        artifacts.append({"state": tag, "checkpoint": st["checkpoint"],
                          "checkpoint_sha256": st["checkpoint_sha256"],
                          "report": st["source"], "n": st["n"]})
    for lst in (trunc_inputs, recut_inputs):
        for i in lst:
            artifacts.append({"report": i["file"], "sha256": i["sha256"],
                              "max_new_tokens": i["max_new_tokens"],
                              "stop_at_turn_end": i["stop_at_turn_end"],
                              "states": i["states"]})
    artifacts.append({"tool": "tools/probe_language_split.py",
                      "tool_sha256": sha256_file(CASE / "tools/probe_language_split.py")})

    #: Длины — по состояниям полного замера; `where_truncated` — разбор причин
    #: обрыва (в рассуждении / на вызове / в ответе инструмента / в ответе).
    #: `censored_share` — доля генераций, чья длина **равна бюджету**: у них
    #: истинная длина не измерена, и если таких много, то p90 — это бюджет, а не
    #: потребность модели. Без этого числа «p90 = 4096» читалось бы как «модели
    #: нужно 4096 токенов».
    lengths = [{"state": st["state"],
                "median_tokens": (st["lengths"] or {}).get("median"),
                "p90_tokens": (st["lengths"] or {}).get("p90"),
                "max_tokens": (st["lengths"] or {}).get("max"),
                "budget": (st["lengths"] or {}).get("budget"),
                "censored_share": st["truncated_share"],
                "p90_censored": ((st["lengths"] or {}).get("p90")
                                 == (st["lengths"] or {}).get("budget")),
                "natural_stop_share": st["natural_stop_share"],
                "where_truncated": st["where_truncated"],
                "median_tokens_natural": (st["lengths_natural"] or {}).get("median"),
                "p90_tokens_natural": (st["lengths_natural"] or {}).get("p90"),
                "max_tokens_natural": (st["lengths_natural"] or {}).get("max"),
                "looped_share": st["looped_share"]}
               for st in full.values()]
    lengths.sort(key=lambda r: r["state"])

    out = {
        "schema": "s3aj-format-validity/1",
        "stage": "S3aj — снятие артефакта усечения, валидные метрики формата и агентности",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "refused" if refusals else "measured",
        "question": ("рост незакрытых <think> (0.4583 → 0.7917) при выравнивании языка — "
                     "артефакт усечения или деградация формата; и каков вердикт ADR-039 "
                     "по достоверному (полному) замеру"),
        "artifacts": artifacts,
        "protocol": {
            "full": {"max_new_tokens": LS.FULL_MAX_NEW_TOKENS, "stop_at_turn_end": True,
                     "why_budget": ("4096 покрывает p90 длины трассы SFT (2914) и почти "
                                    "весь максимум (4583) — runs/s3ai-probes-20260917/"
                                    "sft-trace-lengths.json"),
                     "why_stop": ("веса базы несут eos 151643, а ход кончается 151645 "
                                  "(<|im_end|>): без остановки генерация переписывает "
                                  "свой конец хода и меряется её продолжение")},
            "truncated": {"max_new_tokens": 1024, "stop_at_turn_end": False},
            "recut": {"rule": "ход кончается на первом <|im_end|>",
                      "why": ("greedy префиксно детерминирован → перечитанный текст "
                              "равен тексту прогона с исправленной остановкой на том же "
                              "бюджете; разводит причину «прибор» и причину «бюджет»")},
            "loop_max4gram_rep": LS.LOOP_MAX4GRAM_REP,
            "percentile_method": "nearest-rank",
        },
        "lengths": lengths,
        "truncated_measure": trunc,
        "recut_measure": recut,
        "full_measure": full,
        "verdict_format": vfmt,
        "verdict_language_by_adr039": vadr,
        "identity": ident,
        "tests": tests,
        "refusals": refusals,
        "assumptions": [
            "greedy-генерация префиксно детерминирована: смена условия остановки не "
            "меняет ни одного токена до точки остановки (на этом стоит recut)",
            "перечитанный замер изолирует причину «прибор» при бюджете 1024, но не "
            "может починить генерации, у которых конца хода внутри окна не было: "
            "покрытие перечитывания названо числом",
            "дописанным ходом считается генерация, остановившаяся сама (stop_reason = "
            "turn_end); «упёрлась в лимит ровно на последнем токене» читается как "
            "лимит — осторожное чтение",
            "полный замер снят на локальной 4080; стенд GB10 не задействован (AD-5), "
            "прогон SFT на нём не останавливался",
            "третье состояние (SFT-resume) в полном замере — проба 4500, в прежнем "
            "(1024) — проба 4000: между ними 500 шагов обучения, и разница бюджета "
            "для этого состояния смешана с разницей шага; для CPT-финала и SFT-3000 "
            "пара замеров снята на одних и тех же весах",
            "правило «маркеры не считаются речью» перечисляет 6 токенов формата "
            "(<think>, </think>, <tool_call>, </tool_call>, <tool_response>, "
            "</tool_response>); маркеры хода <|im_end|>/<|im_start|> в список не "
            "входят, и их латинские буквы считаются речью — в ответной части это "
            "до 6 букв против сотен, но у генерации, чей ответ состоит только из "
            "маркера, различие принципиально (None «нет букв» против 0.0 «латиница»)",
        ],
        "open_questions": [],
    }

    if vfmt["artifact_or_degradation"] == "mixed":
        out["open_questions"].append(
            "Вердикт «смешанно»: незакрытые <think> в полном замере лежат между "
            f"{FMT_ARTIFACT_MAX} и {FMT_DEGRADATION_MIN} — часть объясняется усечением, "
            "часть нет. Решение о работе с форматом на таком замере принимать нельзя.")
    if vfmt["third_cause"]:
        out["open_questions"].append(
            "Остаток обрывов объясняется вырождением, а не форматом: " + vfmt["third_cause"]
            + ". Это меняет предмет работы (вырождение/данные против формата) — нужно "
              "решение архитектора.")
    if (full.get(args.final_state, {}).get("looped_share") or 0) >= 0.5:
        out["open_questions"].append(
            "Больше половины генераций финального состояния зациклены при greedy-декодинге. "
            "Метрики языка и формата на зацикленном тексте измеряют петлю, а не поведение "
            "модели: нужен отдельный разбор (декодирование/данные/шаг обучения).")
    if vadr["zone"] in ("insufficient_coverage", "not_measured"):
        out["open_questions"].append(
            "Язык ответа по полному замеру всё ещё не валиден: " + str(vadr.get("why")))
    #: Расхождение двух читок одного числа — не деталь: основная метрика
    #: (with_zeros, объявлена заранее) и метрика по дошедшим до ответа генерациям
    #: отвечают на разные вопросы, и когда они расходятся сильно, вердикт по
    #: основной читается как «языка ответа нет», хотя там, где ответ есть, он есть.
    fin = full.get(args.final_state) or {}
    if (fin.get("cyr_answer") is not None and fin.get("cyr_answer_defined_only") is not None
            and abs(fin["cyr_answer_defined_only"] - fin["cyr_answer"]) > 0.2):
        out["open_questions"].append(
            f"Основная метрика ответной части ({fin['cyr_answer']}, генерация без букв "
            f"считается нулём) и метрика только по дошедшим до ответа "
            f"({fin['cyr_answer_defined_only']}) расходятся больше чем на 0.2: вердикт "
            "вынесен по основной (объявлена заранее), но расхождение означает, что у "
            "части генераций ответа нет вовсе — язык тут ни при чём, и решать по "
            "одному числу нельзя.")
    if vadr.get("cyr_answer") is not None and abs(
            vadr["cyr_answer"] - LS.CRIT_ANSWER_REFUTE_MAX) < 0.05:
        out["open_questions"].append(
            f"Число ответной части {vadr['cyr_answer']} стоит вплотную к границе "
            f"{LS.CRIT_ANSWER_REFUTE_MAX}: вердикт «опровергнуто» держится на 0.05 "
            "и меньше — это не запас, а шум выборки из 24 генераций.")
    if not tests.get("available"):
        out["open_questions"].append(
            "Журнал тестов не передан: приёмка «тесты PASS» не подтверждена.")
    if not ident.get("available"):
        out["open_questions"].append(
            "Тождество прибора не подтверждено прогоном ядра: " + str(ident.get("why")))
    out["open_questions"].append(
        "Маркеры хода <|im_end|>/<|im_start|> не входят в список разметки прибора: их "
        "латинские буквы попадают в долю языка ответной части (до 6 букв на ход, а у "
        "хода из одного маркера — различие None/0.0). Правка сменит правила разбора и "
        "потребует перечитать прежние замеры под новой арифметикой — это отдельное "
        "решение, а не молчаливая правка посреди замера.")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    note(f"свод записан: {args.out}")
    for r in lengths:
        note(f"  {r['state']}: медиана={r['median_tokens']} p90={r['p90_tokens']} "
             f"бюджет={r['budget']} дописано={r['natural_stop_share']} "
             f"обрыв={r['where_truncated']}")
    note(f"  формат: {vfmt['artifact_or_degradation']} ({vfmt['zones']})")
    note(f"  ADR-039: {vadr['zone']} (cyr_answer={vadr.get('cyr_answer')})")
    if refusals:
        for r in refusals:
            note(f"  ОТКАЗ: {r}")
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
