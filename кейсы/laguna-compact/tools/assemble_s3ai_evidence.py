#!/usr/bin/env python3
"""S3ai — свод раздельных языковых метрик и вердикт по критерию ADR-039 п.3.

Зачем свод, а не «числа в отчёте». Вердикт по гипотезе владельца («ризонинг —
навык, язык рассуждения не важен») — это решение, которое отменяет или не отменяет
десятки часов работы. Такое решение должно опираться на числа, которые:

1. **сняты прибором, чьё тождество доказано** — пробы ядра повторяются побайтово
   против опубликованных ответов S3ab, а не «тем же протоколом по описанию»;
2. **посчитаны раздельно** — `cyr_think` и `cyr_answer` отдельно, общий скор
   помечен справочным (ADR-039 п.2);
3. **проверены тестами** — строка «итого» берётся из журнала прогона тестов;
   отсутствие журнала или FAIL — отказ, а не «наверное, прошло»;
4. **сверены с критерием, объявленным заранее** — пороги вынесены в код прибора
   до замера; здесь они только применяются.

Свод **не выносит суждения**: он печатает зону, которую даёт функция критерия, и
все числа, по которым эта зона получена, включая диагностические (покрытие
ответной части, доля обрезки, прозаическая часть ответа). Если числа показывают
случай, которого в критерии нет (ответ русский и рассуждение тоже), свод называет
это «вне критерия» и выносит в open_questions — трактовка не его дело.

Коды возврата::

    0 — свод собран
    1 — отказ: вход есть, но не тот (тождество не подтвердилось, тесты FAIL,
        хеш чекпойнта разошёлся с записанным)
    2 — NOT-VERIFIED: нет данных (прогон проб, журнал тестов, факты старта)

Запуск::

    python3 tools/assemble_s3ai_evidence.py \\
        --probe-run runs/s3ai-probes-20260917/probe_lang_points.json \\
        --identity-run runs/s3ai-probes-20260917/identity_core_cfinal.json \\
        --s3ab-evidence <путь>/s3ab-cpt-probes.json \\
        --tests-log runs/s3ai-probes-20260917/tool_tests.log \\
        --final-state sft_resume \\
        --out evidence/s3ai-language-split.json
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

import probe_control as PC            # noqa: E402  (метрики S3ab — не копия арифметики)
import probe_language_split as LS     # noqa: E402  (критерий и разбор сегментов)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Гипотеза, которая проверяется, — дословно из ADR-039.
HYPOTHESIS = ("ризонинг — навык: язык рассуждения не важен, SFT на исходном наборе "
              "даёт русский ответ при английском рассуждении (лечение данных не нужно)")


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def tests_verdict(log_path: Path | None, known_red: list[str], probe_tool: str) -> dict:
    """Итог прогона тестов — строкой «итого» из журнала, а не пересказом.

    Разделяются два разных вопроса: (а) прошли ли **проверки S3ai** — это то, за
    что отвечает дельта, и (б) какие красные остались в дереве. Красное, которое
    воспроизводится на **чистом HEAD** (объявлено в `--known-red`), — не заслуга
    дельты и не её вина; но и молча пропустить его нельзя, поэтому оно называется
    в отчёте поимённо. Любое красное вне списка — отказ.
    """
    if log_path is None or not Path(log_path).is_file():
        return {"available": False, "why": f"журнала тестов нет: {log_path}"}
    text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tally = next((l for l in reversed(lines) if l.startswith("итого:")), None)
    if tally is None:
        return {"available": True, "tally": None, "passed": False,
                "why": "в журнале нет строки «итого» — прогон не завершён"}
    ours = [l for l in lines
            if (probe_tool in l or "launch_sft_stage --resume" in l
                or "stop_stage_run: пометка остановки" in l
                or "assemble_s3ai_evidence" in l)
            and l.strip().startswith(("ok", "FAIL"))]
    ok = [l for l in ours if l.strip().startswith("ok")]
    bad = [l.strip() for l in ours if l.strip().startswith("FAIL")]
    #: Красные всего прогона — из блока сводки после «итого» (строки «  - …»).
    failures = []
    if "FAIL=" in tally and "FAIL=0" not in tally:
        tail = lines[lines.index(tally):]
        failures = [l.strip()[2:] for l in tail if l.strip().startswith("- ")]
    unknown = [f for f in failures if not any(k in f for k in known_red)]
    return {"available": True, "tally": tally,
            "passed": not bad and not unknown,
            "s3ai_checks": len(ours), "s3ai_ok": len(ok), "s3ai_failed": bad,
            "failures": failures, "known_red": known_red,
            "failures_outside_known_red": unknown,
            "known_red_note":
                "красные из списка воспроизведены на чистом HEAD (см. артефакт "
                "known-red-reproduction в отчёте) — к дельте S3ai отношения не имеют",
            "log": str(log_path), "log_sha256": sha256_file(Path(log_path)),
            "checks": [l.strip() for l in ours]}


def identity_check(identity_run: Path, expected_sha: str | None) -> dict:
    """Тождество прибора — числом: пробы ядра против опубликованных проб S3ab.

    Сравниваются **все** поля метрик, которые публикует S3ab (`degenerate_metrics`
    применяется заново, не берётся из отчёта), и — если исходный прогон S3ab ещё
    доступен — байты ответов. Расхождение хотя бы в одном поле = отказ: значит,
    прибор мерит не то же самое, и числа S3ai несопоставимы с S3ab.
    """
    s3ab_path = Path(identity_run["s3ab_evidence"])
    if not s3ab_path.is_file():
        return {"available": False, "why": f"опубликованных проб S3ab нет: {s3ab_path}"}
    s3ab = load_json(s3ab_path)
    run = load_json(Path(identity_run["run"]))
    state = identity_run.get("state", "cfinal")
    mine = run.get("states", {}).get(state, {})
    theirs = s3ab.get("states", {}).get(state, {})
    if not mine.get("probes") or not theirs.get("probes"):
        return {"available": False, "why": f"нет проб состояния {state} с одной из сторон"}
    fields = ["cyrillic_share", "has_think", "has_tool_call", "words", "chars",
              "uniq4", "max_repeat_of_char_block"]
    mismatches, matched = [], 0
    for a, b in zip(mine["probes"], theirs["probes"]):
        rec = PC.degenerate_metrics(a["response"])
        for k in fields:
            if rec.get(k) == b.get(k):
                matched += 1
            else:
                mismatches.append({"probe": a["tag"], "field": k,
                                   "s3ai": rec.get(k), "s3ab": b.get(k)})
    out = {
        "available": True,
        "state": state,
        "s3ab_evidence": str(s3ab_path),
        "s3ab_evidence_sha256": sha256_file(s3ab_path),
        "probe_tool": "tools/probe_control.py",
        "probe_tool_sha256": sha256_file(CASE / "tools/probe_control.py"),
        "compared_fields": fields,
        "values_matched": matched,
        "values_total": matched + len(mismatches),
        "mismatches": mismatches,
        "identical": not mismatches and matched > 0,
        "how": "повторный прогон проб ядра тем же прибором (greedy → детерминировано) "
               "и применение СВОИХ метрик S3ab (probe_control.degenerate_metrics) "
               "к полученным ответам: совпадение — по значениям, не по описанию",
    }
    #: `or ""` — потому что необязательный путь приходит как None, а `Path(None)`
    #: падает: «не задан» и «задан пустым» должны вести себя одинаково.
    raw = Path(identity_run.get("s3ab_raw_run") or "")
    if raw.is_file():
        raw_d = load_json(raw)
        rp = raw_d.get("runs", {}).get(state, {}).get("probes", [])
        same = sum(1 for a, b in zip(mine["probes"], rp)
                   if a["response"] == b.get("response"))
        out["bytes_identical_responses"] = same
        out["bytes_compared"] = min(len(mine["probes"]), len(rp))
        out["s3ab_raw_run"] = str(raw)
    else:
        out["bytes_identical_responses"] = None
        out["bytes_note"] = ("исходный прогон S3ab недоступен по указанному пути — "
                             "байтовое сравнение не выполнено (это НЕ «совпало»)")
    if expected_sha:
        got = mine.get("checkpoint_sha256")
        out["checkpoint_sha256_expected"] = expected_sha
        out["checkpoint_sha256_measured"] = got
        out["checkpoint_sha256_match"] = (got == expected_sha)
    return out


def degeneracy_view(state: dict, floor: float = 0.5) -> dict:
    """Вырождение генераций по правилам S3ab — тем же прибором, а не «на глаз».

    Зачем рядом с языком: «кириллица 0.83 в рассуждении» читается как «модель думает
    по-русски», но зацикленная генерация даёт ровно ту же буквенную долю. Правила
    S3ab (ADR-026 п.2) уже назвали этот дефект, и здесь применяются **дословно**,
    без собственных порогов:

    * **вето** — `uniq4 == 1.0` при повторе блока символов (цикл внутри «слова»);
    * **вырождение** — `uniq4 < floor` (повтор на уровне 4-грамм слов);
    * **разнообразие** — ни то, ни другое.

    Блок не двигает вердикт (критерий ADR-039 про язык), но без него отчёт читался бы
    как «рассуждение стало русским», даже если это петля.
    """
    probes = state.get("probes") or []
    rows = []
    for p in probes:
        m = PC.degenerate_metrics(p["response"])
        veto = bool(m["uniq4"] == 1.0 and (m["max_repeat_of_char_block"] or 1) > 1)
        low = bool(m["uniq4"] is not None and m["uniq4"] < floor)
        rows.append({"tag": p["tag"], "uniq4": m["uniq4"],
                     "max_repeat_of_char_block": m["max_repeat_of_char_block"],
                     "cyr_think": p["metrics"].get("cyr_think"),
                     "cyr_answer": p["metrics"].get("cyr_answer"),
                     "veto_intra_word_loop": veto, "low_diversity": low,
                     "degenerate": veto or low})
    uniq = [r["uniq4"] for r in rows if r["uniq4"] is not None]
    return {
        "rule": f"S3ab/ADR-026 п.2, дословно: вето — uniq4 == 1.0 при повторе блока "
                f"символов; вырождение — uniq4 < {floor}; иначе разнообразие",
        "measured_by": "probe_control.degenerate_metrics (импорт, не копия арифметики)",
        "n": len(rows),
        "uniq4_floor": floor,
        "uniq4_median": (sorted(uniq)[len(uniq) // 2] if uniq else None),
        "uniq4_min": min(uniq) if uniq else None,
        "measured": len(uniq),
        "veto": sum(1 for r in rows if r["veto_intra_word_loop"]),
        "degenerate": sum(1 for r in rows if r["degenerate"]),
        "degenerate_share": round(sum(1 for r in rows if r["degenerate"]) / len(rows), 4)
                            if rows else None,
        "per_probe": rows,
    }


def point_view(tag: str, state: dict, *, prompt_set: str, max_new_tokens: int) -> dict:
    """Точка замера в форме отчёта: раздельные метрики + диагностика."""
    agg = state.get("aggregate") or LS.aggregate(state.get("probes", []))
    return {
        "state": tag,
        "n": agg.get("n"),
        "cyr_think": agg.get("cyr_think", {}).get("with_zeros"),
        "cyr_answer": agg.get("cyr_answer", {}).get("with_zeros"),
        "mode_share": agg.get("mode_share_any"),
        "mode_share_think": agg.get("mode_share_think"),
        "mode_share_tool_call": agg.get("mode_share_tool_call"),
        #: диагностика — она не двигает вердикт, но без неё «0.0» читалось бы как
        #: «модель ответила по-английски» там, где она вообще не дошла до ответа
        "cyr_think_defined_only": agg.get("cyr_think", {}).get("defined_only"),
        "cyr_answer_defined_only": agg.get("cyr_answer", {}).get("defined_only"),
        "answer_coverage": agg.get("cyr_answer", {}).get("coverage"),
        "cyr_answer_prose": agg.get("cyr_answer_prose", {}).get("with_zeros"),
        "cyr_overall_reference_only": agg.get("cyr_overall_reference_only", {}).get("with_zeros"),
        "truncated_share": agg.get("truncated_share"),
        "unclosed_think_share": agg.get("unclosed_think_share"),
        "stray_think_close_share": agg.get("stray_think_close_share"),
        "checkpoint": state.get("checkpoint"),
        "checkpoint_sha256": state.get("checkpoint_sha256"),
        "checkpoint_bytes": state.get("checkpoint_bytes"),
        "load": state.get("load"),
        "prompt_set": prompt_set,
        "max_new_tokens": max_new_tokens,
        "degeneracy": degeneracy_view(state),
        "aggregate_full": agg,
    }


def monitor_view(path: Path | None) -> dict:
    """Монитор стадии (K1/K2/домен, ADR-033 п.3): что он показал за дообучение.

    Нужен потому, что возобновление идёт с тем же монитором, и его числа — вторая
    линия доказательства, что прогон продолжился, а не начался заново: уровень
    отношений обязан продолжиться, а не прыгнуть к значениям CPT.
    """
    if path is None or not Path(path).is_file():
        return {"available": False, "why": f"истории монитора нет: {path}"}
    points = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").strip().splitlines():
        try:
            points.append(json.loads(line))
        except Exception:
            continue
    if not points:
        return {"available": True, "n": 0, "why": "история пуста"}
    def ratios(field):
        return [p[field] for p in points if field in p]
    k1, k2, dom = ratios("K1_ratio"), ratios("K2_ratio"), ratios("DOMAIN_ratio")
    return {
        "available": True,
        "history": str(path),
        "history_sha256": sha256_file(Path(path)),
        "n_points": len(points),
        "first_step": points[0].get("step"), "last_step": points[-1].get("step"),
        "first": {k: points[0].get(k) for k in ("K1_ratio", "K2_ratio", "DOMAIN_ratio")},
        "last": {k: points[-1].get(k) for k in ("K1_ratio", "K2_ratio", "DOMAIN_ratio")},
        "K1_ratio_max": max(k1) if k1 else None,
        "K2_ratio_max": max(k2) if k2 else None,
        "DOMAIN_ratio_max": max(dom) if dom else None,
        "rule": "K1,K2 ≤ 2× базы; домен × базы < 1 (ADR-027, ADR-031 п.4)",
        "within_rule": bool(k1 and k2 and dom
                            and max(k1) <= 2 and max(k2) <= 2 and max(dom) < 1),
        "note": "стадия остановлена на объявленном шаге до конца прогона, поэтому "
                "вердикт стадии по монитору не выносится: числа показывают, что за "
                "добавленные шаги потолки не пробиты",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe-run", required=True, action="append",
                    help="прогон проб прибора (JSON); повторяемый — состояния разных "
                         "прогонов сводятся, но только если протокол у них один и тот же")
    ap.add_argument("--identity-run", default=None, help="прогон тождества (пробы ядра)")
    ap.add_argument("--s3ab-evidence", default=None,
                    help="опубликованные пробы S3ab (для тождества по числам)")
    ap.add_argument("--s3ab-raw-run", default=None,
                    help="исходный прогон S3ab (для байтового сравнения ответов)")
    ap.add_argument("--tests-log", default=None, help="журнал tools/tests/run_tool_tests.sh")
    ap.add_argument("--known-red", action="append", default=[],
                    help="имя проверки, красной ДО дельты (воспроизводится на чистом HEAD); "
                         "повторяемый. Красное вне списка — отказ свода")
    ap.add_argument("--known-red-reproduction", default=None,
                    help="файл с воспроизведением известного красного на чистом HEAD")
    ap.add_argument("--facts", default=None, help="факты старта возобновлённого прогона")
    ap.add_argument("--arbitration", default=None, help="снимок арбитража стенда (ADR-012)")
    ap.add_argument("--run-manifest", default=None, help="манифест возобновлённого прогона")
    ap.add_argument("--stop-record", default=None, help="след остановки возобновлённого прогона")
    ap.add_argument("--monitor-history", default=None,
                    help="general_eval_history.jsonl возобновлённого прогона")
    ap.add_argument("--final-state", required=True,
                    help="тег состояния «SFT после дообучения» в прогоне проб")
    ap.add_argument("--cfinal-sha", default=None,
                    help="ожидаемый sha256 CPT-финала (сверка, что мерился он)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    probe_runs = [Path(x) for x in args.probe_run]
    missing = [str(x) for x in probe_runs if not x.is_file()]
    if missing:
        note(f"NOT-VERIFIED: прогона проб нет: {missing}")
        return EXIT_NOT_VERIFIED
    runs = [load_json(x) for x in probe_runs]
    #: Сводить состояния разных прогонов можно **только** при одном протоколе:
    #: иначе «раздельные метрики по трём состояниям» сравнили бы числа, снятые
    #: разными приборами, и вердикт опирался бы на разницу протоколов, а не языка.
    proto_keys = ("prompts_set", "prompts_digest", "decoding", "max_new_tokens")
    protos = [{k: r.get("protocol", {}).get(k) for k in proto_keys} for r in runs]
    if any(p != protos[0] for p in protos[1:]):
        note(f"ОТКАЗ: протоколы прогонов проб разошлись: {protos}")
        return EXIT_FAIL
    tools_sha = sorted({r.get("tool_sha256") for r in runs})
    run = runs[0]
    states = {}
    for r in runs:
        for tag, val in (r.get("states") or {}).items():
            if tag in states and states[tag] != val:
                note(f"ОТКАЗ: состояние {tag} встречается дважды и по-разному")
                return EXIT_FAIL
            states[tag] = val
    if args.final_state not in states or "probes" not in states[args.final_state]:
        note(f"NOT-VERIFIED: в прогоне нет состояния {args.final_state} "
             f"(есть: {sorted(k for k, v in states.items() if 'probes' in v)})")
        return EXIT_NOT_VERIFIED

    protocol = run.get("protocol", {})
    prompt_set = protocol.get("prompts_set", "?")
    max_new = protocol.get("max_new_tokens")
    tests = tests_verdict(Path(args.tests_log) if args.tests_log else None,
                          args.known_red, "probe_language_split")
    if args.known_red_reproduction:
        tests["known_red_reproduction"] = args.known_red_reproduction
        if Path(args.known_red_reproduction).is_file():
            tests["known_red_reproduction_sha256"] = sha256_file(Path(args.known_red_reproduction))

    identity = {"available": False, "why": "прогон тождества не задан"}
    if args.identity_run and args.s3ab_evidence:
        identity = identity_check({"run": args.identity_run, "s3ab_evidence": args.s3ab_evidence,
                                   "s3ab_raw_run": args.s3ab_raw_run, "state": "cfinal"},
                                  args.cfinal_sha)
    elif args.identity_run:
        identity = {"available": False,
                    "why": "не задан --s3ab-evidence: тождество нечем подтвердить"}

    points = [point_view(t, s, prompt_set=prompt_set, max_new_tokens=max_new)
              for t, s in states.items() if "probes" in s]

    final = next(p for p in points if p["state"] == args.final_state)
    verdict_zone = LS.apply_criterion(final["cyr_answer"], final["cyr_think"],
                                      final["answer_coverage"])

    #: Свод отказывает, когда вход не тот: тождество не подтвердилось, тесты
    #: провалены, хеш чекпойнта разошёлся. Это не «придирки»: без них отчёт с
    #: вердиктом опирался бы на прибор, которого никто не проверял.
    refusals = []
    if identity.get("available") and not identity.get("identical"):
        refusals.append("тождество прибора с S3ab не подтвердилось числом")
    if identity.get("checkpoint_sha256_match") is False:
        refusals.append("чекпойнт прогона тождества не тот, на котором сняты пробы S3ab")
    if tests.get("available") and not tests.get("passed"):
        refusals.append(f"тесты не прошли: {tests.get('tally')}")
    if not tests.get("available"):
        refusals.append("журнала тестов нет — «тесты прошли» нечем подтвердить")

    monitor = monitor_view(Path(args.monitor_history) if args.monitor_history else None)
    facts = load_json(Path(args.facts)) if args.facts and Path(args.facts).is_file() else None
    arbitration = (load_json(Path(args.arbitration))
                   if args.arbitration and Path(args.arbitration).is_file() else None)
    manifest = (load_json(Path(args.run_manifest))
                if args.run_manifest and Path(args.run_manifest).is_file() else None)
    stop = (load_json(Path(args.stop_record))
            if args.stop_record and Path(args.stop_record).is_file() else None)
    resume_info = (manifest or {}).get("hyperparameters", {}) if manifest else {}
    resume_block = {
        "from_step": resume_info.get("resume_step"),
        "to_step": (stop or {}).get("stopped_at_step"),
        "run_dir": (manifest or {}).get("run_dir") or (stop or {}).get("run_dir"),
        "evidence_of_start": {
            "facts": str(args.facts) if args.facts else None,
            "tmux": (facts or {}).get("tmux"),
            "container": (facts or {}).get("docker"),
            "chain_status": (facts or {}).get("chain_status"),
            "loss_trace": (facts or {}).get("loss_trace"),
            "resumed_from_line": next((l for l in ((facts or {}).get("sft_log_tail") or [])
                                       if "SFT RESUME" in l), None),
        },
        "manifest": str(args.run_manifest) if args.run_manifest else None,
        "stop_record": str(args.stop_record) if args.stop_record else None,
        "arbitration": arbitration.get("decision") if arbitration else None,
        "arbitration_snapshot": str(args.arbitration) if args.arbitration else None,
        "planned_stop_step": resume_info.get("planned_stop_step"),
        "resume_ckpt_sha256": resume_info.get("resume_ckpt_sha256"),
        "pipeline_sha256": resume_info.get("resume_pipeline_sha256"),
        "stopped_by": (stop or {}).get("stopped_by"),
    }

    consequence_by_zone = {
        LS.ZONE_CONFIRMED:
            "лечение данных отменяется (ADR-037/038 переводятся в «отложено»), "
            "генерация 27B не запускается, следующий шаг — RL с наградой, "
            "закрепляющей язык ответа",
        LS.ZONE_REFUTED:
            "язык входа протекает: возврат к линиям ADR-037/038 (русскоязычные трассы) "
            "или награда за язык в RL — решение архитектора; генерация 27B "
            "остаётся не запущенной до этого решения",
        LS.ZONE_INTERMEDIATE:
            "не решение: промежуточная зона 0.4–0.5 требует увеличения выборки и/или "
            "длины прогона; ни лечение данных, ни отказ от него не обоснованы",
        LS.ZONE_OUT_OF_SCOPE:
            "вне объявленного критерия: посылка «рассуждение осталось английским» "
            "не выполнена — решение архитектора (в open_questions)",
        "insufficient_coverage":
            "замер недействителен: ответной части нет у большинства генераций — "
            "нужен бюджет генерации больше либо прогон длиннее, трактовка запрещена",
        "not_measured": "нет числа — нет вердикта",
    }
    zone = verdict_zone["zone"]

    status = "complete"
    if refusals:
        status = "partial"
    if zone in ("insufficient_coverage", "not_measured"):
        status = "partial"

    out = {
        "schema": "s3ai-language-split/1",
        "stage": "S3ai",
        "status": status,
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": "ADR-039: проверить раздельно, даёт ли SFT русский ответ при "
                   "английском рассуждении (гипотеза «ризонинг — навык»)",
        "artifacts": {
            "probe_runs": [{"path": str(x), "sha256": sha256_file(x),
                            "states": sorted(k for k, v in load_json(x).get("states", {}).items()
                                             if "probes" in v)} for x in probe_runs],
            "probe_tool_sha256": tools_sha,
            "probe_run": str(probe_runs[0]), "probe_run_sha256": sha256_file(probe_runs[0]),
            "identity_run": args.identity_run,
            "tests_log": tests.get("log"),
            "facts": args.facts, "arbitration": args.arbitration,
            "run_manifest": args.run_manifest, "stop_record": args.stop_record,
        },
        "instrument": {
            "probe": {
                "tool": "tools/probe_language_split.py",
                "sha256": sha256_file(CASE / "tools/probe_language_split.py"),
                "method": "протокол проб S3ab (те же промпты ядра импортом из "
                          "probe_control, тот же системный промпт, chat template, "
                          "greedy) + разбор на сегменты <think>/ответ",
            },
            "segments_supported": {
                "think": "внутри <think>…</think> (незакрытый — до конца хода)",
                "answer": "вне <think>, включая текст после </think> и до конца хода",
                "mode_share": "доля генераций с <think> и/или <tool_call>",
                "prose_diagnostic": "answer без блоков <tool_call>/<tool_response>",
                "overall_reference_only": "общий скор кириллицы — решения по нему не принимаются",
            },
            "rules": protocol.get("rules"),
            "criteria_thresholds": {
                "answer_ru_min": LS.CRIT_ANSWER_RU_MIN,
                "think_en_max": LS.CRIT_THINK_EN_MAX,
                "answer_refute_max": LS.CRIT_ANSWER_REFUTE_MAX,
                "answer_coverage_floor": LS.ANSWER_COVERAGE_FLOOR,
                "declared_before": "ADR-039 п.3 (пороги) + прибор (страж покрытия)",
            },
            "identity_with_s3ab": identity,
            "tests": tests,
        },
        "protocol": protocol,
        "monitor": monitor,
        "points": points,
        "resume": resume_block,
        "verdict": {
            "hypothesis": HYPOTHESIS,
            "by_criterion": f"ADR-039 п.3: ответ ≥ {LS.CRIT_ANSWER_RU_MIN} при think "
                            f"≤ {LS.CRIT_THINK_EN_MAX} → подтверждена; ответ < "
                            f"{LS.CRIT_ANSWER_REFUTE_MAX} → опровергнута; "
                            f"{LS.CRIT_ANSWER_REFUTE_MAX}–{LS.CRIT_ANSWER_RU_MIN} → "
                            "недостаточно данных",
            "cyr_answer_final": final["cyr_answer"],
            "cyr_think_final": final["cyr_think"],
            "answer_coverage_final": final["answer_coverage"],
            "zone": zone,
            "why": verdict_zone["why"],
            "refusals": refusals,
        },
        "consequences": consequence_by_zone.get(zone),
        "limitations": [
            "прогон возобновлялся с чекпойнта шага 3000 без изменения данных и без "
            "маски <think>: первые 3395 шагов сделаны на исходном наборе, результат "
            "несопоставим с будущим лечением данных (ADR-039, Negative)",
            "расписание LR продолжено (T_max не менялся), но поток данных "
            "возобновлённого прогона начинается с начала перемешанной эпохи: "
            "шаги 3001+ повторно проходят начало набора",
            "бюджет генерации у прогонов языка — 1024 токена (объявлено до замера): "
            "на 384 токенах (протокол S3ab) замер дал hit_limit 5/5 и ответной части "
            "у генераций не было",
            "CPT-финал не завершает ход ни на одном опробованном бюджете "
            "(384/1024/2048): его точка — направление, а не абсолют языка ответа",
        ],
        "open_questions": [],
    }

    if zone == LS.ZONE_OUT_OF_SCOPE:
        out["open_questions"].append(
            "Ответная часть русская, но и рассуждение русскоязычное: ADR-039 описывает "
            "случай «русский ответ при английском think». Нужно решение архитектора, "
            "считать ли гипотезу подтверждённой при русском рассуждении (лечение данных "
            "тогда касается не языка, а чего-то другого).")
    if zone == LS.ZONE_INTERMEDIATE:
        out["open_questions"].append(
            f"Промежуточная зона: ответная часть {final['cyr_answer']} лежит между "
            f"{LS.CRIT_ANSWER_REFUTE_MAX} и {LS.CRIT_ANSWER_RU_MIN}. ADR-039 п.3 запрещает "
            "трактовать это в чью-либо пользу — нужен больший объём выборки или более "
            "длинный прогон, и только потом решение о лечении данных.")
    if zone in ("insufficient_coverage", "not_measured"):
        out["open_questions"].append(
            "Замер языка ответа недействителен: у большинства генераций ответной части "
            "нет (ход обрывается в рассуждении). Решение о лечении данных на таком "
            "замере невозможно; нужен бюджет генерации больше или прогон длиннее.")
    if not tests.get("available"):
        out["open_questions"].append(
            "Журнал тестов не передан: строка «итого» и перечень проверок S3ai в отчёт "
            "не попали — приёмка «тесты PASS» не подтверждена.")
    if final["cyr_answer_defined_only"] is not None and final["cyr_answer"] is not None \
            and abs(final["cyr_answer_defined_only"] - final["cyr_answer"]) > 0.2:
        out["open_questions"].append(
            f"Основная метрика ({final['cyr_answer']}, без ответной части считается 0) "
            f"и метрика только по дошедшим до ответа генерациям "
            f"({final['cyr_answer_defined_only']}) расходятся больше чем на 0.2 — "
            "вердикт вынесен по основной (объявлена заранее), но расхождение "
            "показывает, что часть генераций до ответа не доходит.")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    note(f"свод записан: {args.out}")
    note(f"  зона: {zone} | {verdict_zone['why']}")
    for p in points:
        note(f"  {p['state']}: n={p['n']} cyr_think={p['cyr_think']} "
             f"cyr_answer={p['cyr_answer']} покрытие={p['answer_coverage']} "
             f"режим={p['mode_share']}")
    if refusals:
        for r in refusals:
            note(f"  ОТКАЗ: {r}")
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
