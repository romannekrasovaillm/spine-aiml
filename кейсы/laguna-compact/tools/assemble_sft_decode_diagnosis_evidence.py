#!/usr/bin/env python3
"""decode-diagnosis — свод: таблица «режим × метрика» против V1, прямой ответ на
вопрос дельты и границы опыта.

**Вход.** Разбор `runs/sft-decode-diagnosis-20260920/decode_modes_analysis.json`
(его собирает `tools/analyze_decode_modes.py`, вызывая арифметику прибора
`probe_language_split.aggregate` на пробах трёх отчётов) плюс манифест прогона и
запись прогона. Свод **не пересчитывает** метрики — иначе у одного числа стало бы
два носителя (ADR-023 п.10), и «свод» разошёлся бы с разбором молча. Он проверяет
контракт входа, переносит числа и формулирует ответ.

**Что в своде есть.**

1. ``answer`` — отдельным полем: вердикт (``weights_property`` /
   ``protocol_artifact`` / ``insufficient_data``) и **числа**, из которых он выведен
   (первичный индикатор по каждому режиму, дельта в п.п., p точного Мак-Немара).
   Формулировка собирается **из чисел**, а не пишется поверх них: строка, набранная
   руками, при пересчёте осталась бы прежней и стала бы ложью.
2. ``mode_metric_table`` — режим × метрика: ядро (из прибора) и добавленные сводом
   величины, каждая с определением.
3. ``deltas_vs_v1`` — по каждой метрике: значение, дельта, парный точный тест
   Мак-Немара, непарный двухдолевой z, признак значимости при объявленных порогах.
4. ``not_decided`` — чего этот опыт **не** решает (TASK п.4). Список переносится из
   разбора без правок: границы опыта — часть результата, а не примечание.
5. ``artifacts`` — хеши входов и отчётов режимов: свод обязан называть, из чего он
   собран, чтобы «числа в своде» и «числа в отчёте» можно было сверить, а не
   поверить (ADR-023 п.2).

**Чего в своде нет.** Вердикта стадии по формату и языку: числа альтернативных
режимов для него непригодны по построению (V2 несёт
``allowed_for_conclusions = false`` — прибор отказывает прогону без запрета повторов
n-грамм, и это его собственное правило; V3 — не штатный режим). ADR-041 остаётся в
силе, пороги ADR-045 не двигаются: это диагностика, а не смена протокола.

**Контроль может быть историческим** (``control_mode.kind =
historical_s3aq_report``). Тогда ``identity_control`` пуст, а его место занимает
``partial_identity_control`` — сверка проб оборванного луча V1 по журналу прибора.
Свод обязан **назвать** это в тексте ответа, а не только в поле: читатель строки
ответа не должен думать, что контроль побайтовый, если он частичный. Это слабее, и
слабее оно ровно в одном: сверены 8 проб из 104, тождество остальных 96 выведено из
детерминизма greedy, а не измерено.

Коды возврата::

    0 — свод собран, контракт входа соблюдён
    1 — отказ: вход противоречит контракту (разбор не собран, тождество V1 с S3aq
        нарушено, режим отсутствует или не тот, который объявлен, манифеста нет)
    2 — NOT-VERIFIED: нечего сводить (нет разбора)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

RUN = "runs/sft-decode-diagnosis-20260920"
ANALYSIS = RUN + "/decode_modes_analysis.json"
MANIFEST = RUN + "/run_manifest.json"
PROBE_RECORD = RUN + "/probe_record.json"
PIN_CHECK = RUN + "/pin_check.json"
REF_PROVENANCE = RUN + "/reference/provenance.json"
EVIDENCE = "evidence/sft-decode-diagnosis.json"

#: Метрики свода: имя в таблице → путь в разборе + определение. Один список, чтобы
#: таблица, дельты и текст ответа не разъехались по разным числам.
#: ``side``: core — из прибора, extra — добавлено разбором.
METRICS = (
    ("unfinished_tool_call", ("extra", "unfinished_tool_call", "share"), "core+",
     "доля ходов с stop_reason = limit_in_tool_call (индикатор ADR-050 п.3)"),
    ("budget_hit", ("core", "truncated_share"), "core",
     "доля ходов, упёршихся в бюджет 8192 (stop_reason ≠ turn_end)"),
    ("median_len", ("core", "lengths", "median"), "core",
     "медиана длины хода, токены (ближайший ранг)"),
    ("p90_len", ("core", "lengths", "p90"), "core", "p90 длины хода, токены"),
    ("p99_len", ("extra", "p99_len"), "extra", "p99 длины хода, токены"),
    ("unclosed_think_all", ("core", "unclosed_think_share"), "core",
     "доля ходов с незакрытым <think> — по всем ходам"),
    ("unclosed_think_natural", ("core", "stop", "natural", "unclosed_think_share"), "core",
     "то же среди естественно завершённых ходов (правило ADR-045 п.5)"),
    ("looped_all", ("core", "looped_share"), "core",
     "доля зацикленных ходов (max4gram_rep ≥ 8 — константа прибора)"),
    ("repeat4_all", ("extra", "repeat4_all", "defined_only"), "extra",
     "доля ходов с повторами 4-грамм (max4gram_rep ≥ 2; определена при ≥ 8 словах)"),
    ("json_valid_with_call", ("extra", "json_valid", "with_tool_call", "share"), "extra",
     "доля JSON-валидных вызовов среди ходов, где вызов есть (регексп пайплайна v8)"),
    ("tc_looped", ("extra", "among_unfinished_tool_call", "looped"), "extra",
     "доля зацикленных СРЕДИ ходов с незавершённым вызовом"),
    ("tc_repeat4", ("extra", "among_unfinished_tool_call", "repeat4"), "extra",
     "доля с повторами 4-грамм среди ходов с незавершённым вызовом"),
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


def dig(d: dict | None, path: tuple[str, ...], default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def metric_table(analysis: dict) -> dict:
    """Режим × метрика — переносом чисел разбора, без пересчёта."""
    modes = analysis["modes"]
    rows = {}
    for name, path, side, definition in METRICS:
        row = {"definition": definition, "source": side, "values": {}}
        for tag, m in modes.items():
            row["values"][tag] = dig(m, path)
        rows[name] = row
    return {
        "note": ("числа перенесены из разбора без пересчёта: у одной величины один носитель "
                 "(ADR-023 п.10); определения — рядом, потому что «доля чего» здесь "
                 "содержательно в каждой строке"),
        "n": {tag: m["n"] for tag, m in modes.items()},
        "rows": rows,
    }


def verdict_text(answer: dict, control: dict | None = None) -> str:
    """Строка ответа — из чисел, а не поверх них.

    Собирается кодом намеренно: текст, набранный руками, при пересчёте остался бы
    прежним и разошёлся бы с числами ровно в том случае, ради которого свод и
    делается (смена режима). Формулировка называет все три режима числом.
    """
    measured = answer.get("alternative_modes_measured") or []
    partial = (f" Сняты не все лучи: измерены {', '.join(measured) or '—'} из V2 и V3 — "
               f"вердикт по неполному набору (partial_evidence)." if answer.get("partial_evidence")
               else "")
    # Исторический контроль называется **в строке ответа**, а не только полем:
    # строка ответа — то, что читают, и «контроль» в ней не должен выглядеть
    # побайтовым, если сверено 8 проб из 104.
    hist = ""
    if control and control.get("kind") == "historical_s3aq_report":
        pi = control.get("partial_identity", {}) or {}
        hist = (f" Контроль ИСТОРИЧЕСКИЙ, не перезамер: живой луч V1 (контроль тождества) "
                f"оборван — сверено {pi.get('compared')} проб из {pi.get('n_probes_total')}, "
                f"совпало {pi.get('matched')}; тождество остальных "
                f"{pi.get('unmeasured')} выведено из детерминизма greedy, а не измерено. "
                f"Роль контроля играет состояние {control.get('source_state')} решающего "
                f"отчёта S3aq — тот же чекпойнт, прибор, набор, бюджет и режим.")
    branch = answer.get("rule_branch") or {}
    branch_note = (f" Ветка правила: {branch.get('fired')} — {branch.get('why')}"
                   if branch else "")
    prim = answer.get("primary_indicator") or {}
    per = answer.get("per_alternative_mode") or {}
    parts = [f"{t} {prim[t]:.4f}" for t in ("V1", "V2", "V3") if prim.get(t) is not None]
    tail = []
    for t in ("V2", "V3"):
        d = per.get(t)
        if not d or d.get("delta_pp_vs_V1") is None:
            continue
        tail.append(f"{t}: Δ {d['delta_pp_vs_V1']:+.4f} п.п., p={d['mcnemar_p']:.3g} "
                    f"(Мак-Немар), {'падение значимо' if d['significant_drop'] else 'падение не значимо'}")
    verdict = answer.get("verdict")
    if verdict == "weights_property":
        head = ("УБЕГАНИЕ СОХРАНЯЕТСЯ при смене режима декодирования ⇒ свойство весов: "
                "ни один альтернативный режим не уводит долю незавершённых вызовов под "
                "критерий стадии значимо")
    elif verdict == "protocol_artifact":
        head = ("УБЕГАНИЕ ИСЧЕЗАЕТ при смене режима декодирования ⇒ артефакт протокола: "
                "альтернативный режим уводит долю под критерий стадии значимо")
    else:
        head = ("НЕДОСТАТОЧНО ДАННЫХ: объявленное правило не даёт ни одного из двух исходов "
                "— назвать, что домерить")
    sec = answer.get("secondary_indicators") or {}
    length_parts = [f"{t}: доля упёршихся в бюджет {sec[t]['budget_hit']:.4f}, медиана "
                    f"{sec[t]['median_len']}, p90 {sec[t]['p90_len']}"
                    f"{' (= бюджет: распределение обрезано)' if sec[t]['p90_at_budget'] else ''}"
                    f", незакрытых <think> среди дописанных {sec[t]['unclosed_think_natural']}"
                    for t in ("V1", "V2", "V3") if t in sec]
    return (f"{head}. Первичный индикатор (доля ходов с незавершённым tool_call): "
            + ", ".join(parts) + ". " + "; ".join(tail) + ". "
            + "Убегание длины тем же замером: " + "; ".join(length_parts) + "."
            + branch_note + hist + partial)


def runaway_reading(answer: dict) -> dict:
    """Ответ по оси **длины** — отдельным полем, из чисел, и с названной границей.

    Вопрос дельты называет две вещи: убегающую длину и незавершённые вызовы. Правило
    вердикта объявлено по вызовам, поэтому «свойство весов или артефакт протокола»
    по длине — **второе** чтение того же замера, и оно обязано лежать отдельным
    полем, а не подразумеваться в тексте вердикта. Формулировка собирается кодом из
    чисел: направление берётся из `length_axis` разбора, а не пишется рукой поверх
    него (§ тот же довод, что у `verdict_text`).
    """
    axis = answer.get("length_axis") or {}
    if not axis:
        return {"available": False,
                "why": "ось длины не посчитана: альтернативных лучей нет"}
    parts, rows = [], {}
    for tag in ("V2", "V3"):
        a = axis.get(tag)
        if not a:
            continue
        rows[tag] = a
        parts.append(f"{tag}: доля упёршихся в бюджет {a['control_budget_hit']} → "
                     f"{a['budget_hit']} ({a['direction']}), медиана хода "
                     f"{a['control_median_len']} → {a['median_len']}"
                     f"{' (= бюджет: распределение обрезано)' if a['median_at_budget'] else ''}, "
                     f"петель {a['control_looped_all']} → {a['looped_all']}")
    strengthened = [t for t, a in rows.items() if a["direction"] == "усиливается"]
    statement = (
        "Убегание длины НЕ исчезает при смене декодирования"
        + (" — и усиливается" if strengthened else "")
        + f" ({'; '.join(parts)}). Значит оно свойство весов, а не артефакт протокола "
          "декодирования: снятие штатного запрета повторов 4-грамм его не убирает, то "
          "есть запрет работает средством подавления, а не причиной."
        if strengthened else
        "Ось длины не даёт направления, которое поддержало бы вывод о весах или "
        "протоколе — назвать, что домерить.")
    return {
        "available": True,
        "statement": statement,
        "per_mode": rows,
        "not_the_verdict": (
            "это чтение по оси длины, а не вердикт: вердикт считается объявленным "
            "правилом по первичному индикатору (незавершённые вызовы) и лежит в `answer`"),
        "caveats": [
            "V2 менял ДВА фактора сразу (снятие запрета 4-грамм и repetition_penalty "
            "1.15): чистый вариант «только штраф» при сохранённом запрете не гонялся, "
            "поэтому вклад штрафа отдельно не измерен",
            "V2 несёт allowed_for_conclusions=false: прибор объявляет выводы о языке и "
            "формате по такому прогону запрещёнными (без запрета повторов петля растёт "
            "механически), поэтому «усиливается» читается как «не исчезает», а не как "
            "точная величина роста",
            "значимость по длине не считается: бюджет 8192 обрезает ряд (p90 = 8192 у "
            "обоих режимов), и разница медиан измеряла бы бюджет, а не модель",
        ],
    }


def build(analysis_path: str = ANALYSIS, case: Path | None = None) -> tuple[int, dict]:
    """Свод по разбору. ``case`` — корень кейса (для тестов на фикстурах)."""
    case = case or CASE
    problems: list[str] = []
    analysis = load(case / analysis_path)
    if analysis is None:
        return EXIT_NOT_VERIFIED, {"error": f"нет разбора режимов {analysis_path}"}

    manifest = load(case / MANIFEST)
    probe_record = load(case / PROBE_RECORD)
    pin_check = load(case / PIN_CHECK)
    ref_prov = load(case / REF_PROVENANCE)

    # ── контракт входа ────────────────────────────────────────────────────────
    # `measurement_incomplete` разбора — не повод отказать своду: неполнота состава
    # видна своду и проверяется им самим (перечнем снятых режимов ниже). Отказ
    # здесь только на нарушении контракта: тогда числа описывают не тот вход.
    if analysis.get("status") == "input_contract_violated":
        problems.append(f"разбор помечен input_contract_violated: {analysis.get('problems')}")
    ident = analysis.get("identity_control_v1_vs_s3aq")
    partial_ident = analysis.get("partial_identity_control")
    control_mode = analysis.get("control_mode") or {"kind": "live_v1_report"}
    historical = control_mode.get("kind") == "historical_s3aq_report"
    if historical:
        # Исторический контроль: побайтового тождества нет по построению (сверять
        # отчёт сам с собой нечего), но частичный обязан быть снят, совпасть и
        # назвать неизмеренную долю.
        if not partial_ident:
            problems.append("контроль объявлен историческим, а частичного контроля "
                            "тождества в разборе нет — подстановка ничем не подтверждена")
        elif not partial_ident.get("identical"):
            problems.append("частичный контроль тождества НЕ пройден: "
                            f"{len(partial_ident.get('mismatches') or [])} расхождений из "
                            f"{partial_ident.get('compared')} сверенных проб")
        elif partial_ident.get("unmeasured", 0) <= 0:
            problems.append("частичный контроль не называет неизмеренную долю — "
                            "«сверено всё» и «сверено ничего» должны различаться")
    elif not ident:
        problems.append("в разборе нет контроля тождества V1 с S3aq — сравнивать не с чем")
    elif not ident.get("identical"):
        problems.append("тождество V1 с решающим отчётом S3aq НАРУШЕНО: "
                        f"{ident['n']['v1'] - ident['n_identical']} расхождений ответов из "
                        f"{ident['n']['v1']}")
    modes = analysis.get("modes") or {}
    for tag in ("V1", "V2", "V3"):
        if tag not in modes:
            problems.append(f"режим {tag} не снят — свод неполон по составу")
        elif modes[tag]["n"] != 104:
            problems.append(f"{tag}: проб {modes[tag]['n']} ≠ 104")
    if manifest is None:
        problems.append(f"нет манифеста прогона {MANIFEST} (AD-2/C-012)")
    else:
        # Манифест считает **снятые** лучи; исторический контроль снятым не был,
        # поэтому с его числом сверяются только живые лучи, а не все ключи разбора.
        swept = len(modes) - (1 if historical else 0)
        if dig(manifest, ("mode_set", "modes_present")) != swept:
            problems.append("манифест называет другое число снятых режимов, чем разбор: "
                            f"{dig(manifest, ('mode_set', 'modes_present'))} против {swept}")
    if probe_record is None:
        problems.append(f"нет записи прогона {PROBE_RECORD}")
    if pin_check is not None and not pin_check.get("all_identical"):
        problems.append("тождество входов (чекпойнт/прибор) не подтверждено записью прогона")
    if ref_prov is not None and not ref_prov.get("tool_identity"):
        problems.append("прибор решающего отчёта S3aq не совпал с прибором этого замера")

    answer = dict(analysis.get("answer") or {})
    control = {
        "kind": control_mode.get("kind"),
        "why": control_mode.get("why"),
        "source_report": control_mode.get("source_report"),
        "source_report_sha256": control_mode.get("source_report_sha256"),
        "source_state": analysis.get("state"),
        "live_report_absent": control_mode.get("live_report_absent"),
        "live_log": control_mode.get("live_log"),
        "not_a_substitute_for": control_mode.get("not_a_substitute_for"),
        "partial_identity": partial_ident,
    }
    answer["text"] = verdict_text(answer, control)
    answer["indicator_definition"] = analysis.get("answer", {}).get(
        "primary_indicator_definition")

    artifacts = {
        "analysis": {"path": analysis_path,
                     "sha256": sha256_file(case / analysis_path)},
        "run_manifest": ({"path": MANIFEST, "sha256": sha256_file(case / MANIFEST)}
                         if manifest else None),
        "probe_record": ({"path": PROBE_RECORD, "sha256": sha256_file(case / PROBE_RECORD)}
                         if probe_record else None),
        "pin_check": ({"path": PIN_CHECK, "sha256": sha256_file(case / PIN_CHECK)}
                      if pin_check else None),
        "reference": ({"path": REF_PROVENANCE, "sha256": sha256_file(case / REF_PROVENANCE),
                       "git_commit": ref_prov.get("git_commit"),
                       "sha256_report": ref_prov.get("sha256"),
                       "in_worktree": False,
                       "note": ("решающий отчёт S3aq в дереве этой ветви отсутствует "
                                "(прогон завершён в ветви arch/laguna-control-arms); "
                                "взят из git-коммита, провенанс назван")}
                      if ref_prov else None),
        "mode_reports": {tag: {"path": m.get("report"), "sha256": m.get("report_sha256"),
                               "decoding": m.get("decoding_params", {}).get("decoding"),
                               "allowed_for_conclusions": m.get("allowed_for_conclusions")}
                         for tag, m in modes.items()},
    }

    status = "complete" if not problems else "measurement_incomplete"
    doc = {
        "schema": "sft-decode-diagnosis/1",
        "tool": "tools/assemble_sft_decode_diagnosis_evidence.py",
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "question": analysis.get("question"),
        "state": analysis.get("state"),
        "budget": analysis.get("budget"),
        "where": ("локальная RTX 4080 SUPER 16GB; стенд GB10 не занят (AD-5); "
                  "сетевой диск — только чтение"),
        "answer": answer,
        "mode_metric_table": metric_table(analysis),
        "deltas_vs_v1": analysis.get("comparisons_vs_v1"),
        "control": control,
        "runaway_reading": runaway_reading(answer),
        "identity_control": ident,
        "analysis_status": analysis.get("status"),
        "analysis_incomplete": analysis.get("incomplete"),
        "definitions": analysis.get("definitions"),
        "not_decided": analysis.get("not_decided"),
        "boundaries": [
            "ADR-041 (единый штатный режим для вердиктов стадии) не меняется: V2/V3 — "
            "диагностические лучи, вердикт стадии по ним не выносится",
            "пороги ADR-045 не двигались; полное чтение остаётся индикатором сатурации прибора",
            "набор, обучающий тензор, карточки и исторические evidence не правились (ADR-023 п.9)",
            "обучение не перезапускалось, стадия не трогалась: замер только инференс",
        ] + ([
            "контроль исторический (historical_s3aq_report): живой луч V1 не снят, роль "
            "контроля играет состояние решающего отчёта S3aq; тождество подтверждено "
            "частично — сверкой проб оборванного прогона, а не побайтово",
        ] if historical else []) + ([
            "V3 (сэмплирование) — справочный луч, и к решению о стадии он не относится по "
            "построению: вердикты стадии считаются в штатном greedy-режиме (ADR-041), то "
            "есть стохастика в них не входит. Не снят — назван частичным, а не выдан за ноль: "
            "«луч не снят» и «луч ничего не изменил» — разные вещи"
            if "V3" not in modes else
            "V3 (сэмплирование) — справочный луч: к решению о стадии не относится, "
            "вердикты считаются в штатном greedy-режиме (ADR-041)",
        ]),
        "artifacts": artifacts,
        "problems": problems,
    }
    return (EXIT_FAIL if problems else EXIT_OK), doc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--analysis", default=ANALYSIS, help=f"разбор режимов ({ANALYSIS})")
    ap.add_argument("--case-root", default=None,
                    help="корень кейса (по умолчанию — каталог инструмента)")
    ap.add_argument("--out", default=EVIDENCE, help=f"куда писать свод ({EVIDENCE})")
    ap.add_argument("--print", action="store_true", help="напечатать свод в stdout")
    args = ap.parse_args()

    rc, doc = build(args.analysis, Path(args.case_root) if args.case_root else None)
    if rc == EXIT_NOT_VERIFIED:
        note(f"NOT-VERIFIED: {doc.get('error')}")
        return rc
    out = (Path(args.case_root) if args.case_root else CASE) / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.print:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    note(f"свод: {args.out} (status={doc['status']}, проблем: {len(doc['problems'])})")
    for p in doc["problems"]:
        note(f"  ! {p}")
    note(f"ответ: {doc['answer'].get('verdict')} — {doc['answer'].get('text')}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
