#!/usr/bin/env python3
"""assemble_rl_rollout_viability.py — свод: условие падения V3, корень, починка и
числа пригодности rollout'ов для RL.

**Почему отдельный свод, а не дописка в `sft-decode-diagnosis.json`.** Тот свод
отвечает на вопрос дельты «петля — свойство весов или протокола» и опирается на три
режима. Этот отвечает на другой вопрос — «годны ли траектории сэмплирования для
RL-rollout'ов» — и часть его входов **не отчёты проб**: факты отказа лежат в журнале
ядра, а число дошедших проб — в логе прибора. Складывать их в один вердикт значило
бы смешать измеренное с происшедшим.

**Что свод проверяет, а не пересказывает.**

1. Прибор не тронут: `sha256(tools/probe_language_split.py)` равен и объявленному
   входу дельты, и `tool_sha256` контрольного отчёта. Если бы он разошёлся, все
   сравнения с контролем были бы сравнениями двух разных приборов.
2. Лог упавшего прогона: хеш сходится, в нём есть строка `unspecified launch
   failure`, и число строк-проб равно объявленному (24 из 104). Это и есть «обрыв
   на 26-й пробе» в проверяемом виде.
3. Журнал ядра: строки S3 и Xid из фактов обязаны найтись в `journalctl -k`. Не
   нашлись — свод пишет `unverifiable` с причиной, а не оставляет факт без пометки
   (журнал ротируется, и «процитировано по памяти» должно быть видно).
4. Числа пригодности — из `tools/analyze_rollout_reach.py`, вердикт петель — из
   `tools/analyze_loop_origin.py`. Свод их **не пересчитывает**.

**Ответ выводится из чисел** с объявленными заранее порогами: доля ходов, дошедших
до ответа без петель, ≥ 0.80 — траектории годны, 0.50…0.80 — годны частично,
< 0.50 — не годны как есть. Отдельной ветвью идёт случай «режим сэмплирования не
измерен»: тогда вердикт по нему не выносится вовсе, и это записано в тексте ответа,
а не спрятано в поле.

Коды возврата::

    0 — свод собран
    1 — отказ: вход не тот (хеш прибора, хеш лога, число проб, контракт входа)
    2 — NOT-VERIFIED: мерить нечего (нет разбора пригодности)

Запуск::

    python3 tools/assemble_rl_rollout_viability.py \\
        --reach runs/sft-decode-diagnosis-20260920/reach_survey.json \\
        --loop-origin runs/sft-decode-diagnosis-20260920/loop_origin_greedy4096.json \\
        --crash-facts runs/sft-decode-diagnosis-20260920/repro/crash_facts.json \\
        --prior evidence/s3ak-degeneration.json \\
        --guard tools/guard_cuda_run.sh \\
        --out evidence/rl-rollout-viability.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE = Path(__file__).resolve().parent.parent

#: Прибор, которым сняты и контроль, и упавший прогон. Число стоит здесь не как
#: «текущее значение», а как **объявленный вход**: свод обязан отказать, если файл
#: разошёлся с ним.
INSTRUMENT_SHA = "99dafa8d9551caaa57b770d4cd321da6f0c09b28deb1885500034c9d62eeb276"

#: Пороги ответа — объявлены до счёта, чтобы их нельзя было подобрать под результат.
REACH_VIABLE = 0.80
REACH_PARTIAL = 0.50


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: str | None) -> dict | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def journal_lines(since: str, until: str) -> tuple[list[str], str]:
    """Строки журнала ядра за окно. Пустой список + причина — если журнал недоступен."""
    if shutil.which("journalctl") is None:
        return [], "journalctl не найден в PATH"
    try:
        out = subprocess.run(["journalctl", "-k", "--since", since, "--until", until],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"journalctl: {type(exc).__name__}: {exc}"
    if out.returncode != 0:
        return [], f"journalctl вернул {out.returncode}"
    return out.stdout.splitlines(), ""


def verify_crash(facts: dict, errors: list[str]) -> dict:
    """Проверка фактов отказа по артефактам: прибор, лог, журнал ядра."""
    cr = facts["crash"]
    out: dict = {"facts_source": None, "instrument": {}, "log": {}, "journal": {}}

    inst = CASE / "tools" / "probe_language_split.py"
    inst_sha = sha256_file(inst)
    out["instrument"] = {
        "path": "tools/probe_language_split.py",
        "sha256": inst_sha,
        "declared_in_facts": cr["instrument_sha256"],
        "matches_declared": inst_sha == cr["instrument_sha256"],
        "matches_pinned_instrument": inst_sha == INSTRUMENT_SHA,
    }
    if not out["instrument"]["matches_declared"] or not out["instrument"]["matches_pinned_instrument"]:
        errors.append("прибор разошёлся с объявленным: правка прибора сломала бы "
                      "сопоставимость с контролем (ADR-041)")

    log = CASE / cr["log"]
    if not log.is_file():
        errors.append(f"лог прогона не найден: {cr['log']}")
        out["log"] = {"path": cr["log"], "present": False}
    else:
        text = log.read_text(encoding="utf-8", errors="replace")
        n_probes = sum(1 for ln in text.splitlines() if ln.startswith("  ["))
        out["log"] = {
            "path": cr["log"], "present": True, "sha256": sha256_file(log),
            "sha256_matches": sha256_file(log) == cr["log_sha256"],
            "has_launch_failure": "unspecified launch failure" in text,
            "probes_in_log": n_probes,
            "probes_declared": cr["probes_completed"],
            "probes_match": n_probes == cr["probes_completed"],
        }
        if not out["log"]["sha256_matches"]:
            errors.append("хеш лога прогона разошёлся с объявленным")
        if not out["log"]["has_launch_failure"]:
            errors.append("в логе нет строки 'unspecified launch failure' — "
                          "объявленная причина не подтверждается логом")
        if not out["log"]["probes_match"]:
            errors.append("число проб в логе не равно объявленному")

    lines, why = journal_lines("2026-09-21 05:03:00", "2026-09-21 05:04:00")
    if why:
        out["journal"] = {"verifiable": False, "why": why}
    else:
        found = []
        for want in ("Preparing to enter system sleep state S3",
                     "Waking up from system sleep state S3",
                     "Xid (PCI:0000:01:00): 31"):
            found.append({"line": want,
                          "present": any(want in ln for ln in lines)})
        out["journal"] = {"verifiable": True, "window": "2026-09-21 05:03:00…05:04:00",
                          "lines_checked": found,
                          "all_present": all(f["present"] for f in found),
                          "raw": [ln for ln in lines if "Xid" in ln or "S3" in ln][:6]}
        if not out["journal"]["all_present"]:
            errors.append("журнал ядра не подтверждает строки S3/Xid из фактов "
                          "(журнал мог ротироваться) — факт остаётся непроверенным")
    return out


def derive_answer(reach: dict, loop_origin: dict | None, crash: dict) -> dict:
    """Ответ из чисел. Пороги объявлены константами выше, а не подобраны здесь."""
    rows = []
    for rep in reach.get("reports", []):
        for tag, st in rep["states"].items():
            if "n" not in st:
                continue
            rows.append({
                "report": rep["report"],
                "state": tag,
                "budget": rep["protocol"]["max_new_tokens"],
                "decoding": rep["protocol"]["decoding"],
                "n": st["n"],
                "reached": st["reached_answer_no_loop"],
                "share": st["reached_answer_no_loop_share"],
                "truncated_share": st["truncated_share"],
                "natural_stop_share": st["natural_stop_share"],
                "unclosed_think_share_among_natural": st["unclosed_think_share_among_natural"],
                "looped_share": st["looped_share"],
                "saturated": st["saturation"]["saturated"],
                "format_verdict_issuable": st["format_verdict_issuable"],
            })
    pinned = [r for r in rows if r["state"] == "sft_v13_21500"]
    sample_rows = [r for r in rows if r["decoding"].startswith("sample")]
    out: dict = {
        "thresholds": {"viable_at_or_above": REACH_VIABLE,
                       "partial_at_or_above": REACH_PARTIAL,
                       "note": "пороги объявлены до счёта (см. константы свода)"},
        "sample_arm_measured": bool(sample_rows),
        "rows": rows,
        "rows_pinned_checkpoint": pinned,
    }
    if not sample_rows:
        base = pinned[0] if pinned else None
        out["verdict"] = "not_established_sample_arm_not_measured"
        if base:
            out["text"] = (
                f"Режим сэмплирования на пиннутом чекпойнте 21 500 НЕ ИЗМЕРЕН "
                f"(устройство мертво: {crash['device_after']['cuInit_note']}), поэтому "
                f"вердикт о пригодности rollout'ов не выносится. Что известно числом: "
                f"в штатном режиме ({base['decoding']}, бюджет {base['budget']}) до ответа "
                f"без петель доходят {base['reached']} из {base['n']} ходов "
                f"({base['share']:.4f}) — ниже порога «годны» {REACH_VIABLE} и ниже порога "
                f"«годны частично» {REACH_PARTIAL}; в бюджет упёрлось "
                f"{base['truncated_share']:.4f} ходов, то есть по ADR-050 п.4.3 прибор на "
                f"этом бюджете измеряет бюджет, а не модель. Сэмплирование петлю "
                f"исторически снижает, а не снимает (S3ak: 0.5417 без запрета → 0.3333 при "
                f"сэмплировании, другой чекпойнт), поэтому ждать от него числа выше "
                f"штатного режима оснований нет, но это ожидание, а не измерение")
        else:
            out["text"] = ("ни одного ряда с пиннутым чекпойнтом 21 500 в разборе нет — "
                           "вердикт не выносится")
    else:
        worst = min(sample_rows, key=lambda r: r["share"])
        if worst["share"] >= REACH_VIABLE:
            out["verdict"] = "viable"
        elif worst["share"] >= REACH_PARTIAL:
            out["verdict"] = "partially_viable"
        else:
            out["verdict"] = "not_viable_asis"
        out["text"] = (f"режим сэмплирования измерен: худший ряд даёт {worst['reached']} из "
                       f"{worst['n']} ({worst['share']:.4f}) — вердикт {out['verdict']}")
    if loop_origin:
        out["loop_origin"] = {
            "verdict": loop_origin.get("answer", {}).get("verdict"),
            "text": loop_origin.get("answer", {}).get("text"),
            "n_loops": loop_origin.get("origin", {}).get("mild", {}).get("n"),
            "by_region": loop_origin.get("blocks", {}).get("by_region_any"),
            "meaning": ("петли штатного режима — собственная дегенерация весов, а не "
                        "воспроизведение набора: смена режима декодирования их не "
                        "вылечит, а лишь ослабит (S3ak)"),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reach", required=True,
                    help="разбор пригодности (tools/analyze_rollout_reach.py)")
    ap.add_argument("--loop-origin", default=None, help="разбор происхождения петель")
    ap.add_argument("--crash-facts", required=True, help="факты отказа (вход дельты)")
    ap.add_argument("--prior", default=None, help="прежний evidence с числами сэмплирования")
    ap.add_argument("--guard", default=None, help="обвязка CUDA-прогона")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    reach = load(args.reach)
    if reach is None:
        print(f"NOT-VERIFIED: разбор пригодности не читается ({args.reach}) — "
              "это отказ входа, а не результат", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    facts = load(args.crash_facts)
    if facts is None:
        print(f"NOT-VERIFIED: факты отказа не читаются ({args.crash_facts})",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED
    loop_origin = load(args.loop_origin)

    errors: list[str] = []
    crash = verify_crash(facts, errors)
    crash["facts_source"] = {"path": args.crash_facts,
                             "sha256": sha256_file(Path(args.crash_facts))}
    crash.update({k: facts["crash"][k] for k in
                  ("run", "arm", "arm_cli", "checkpoint", "checkpoint_sha256",
                   "started_at", "died_at", "python_exception",
                   "python_exception_note", "traceback_site", "traceback_site_note",
                   "suspend_window", "xid_lines", "xid_times", "xid_reading",
                   "suspend_rarity", "device_after", "recovery", "probes_completed",
                   "probes_total", "exit_code")})

    prior = load(args.prior)
    prior_block = None
    if prior:
        s3ak = prior.get("degeneration", {}).get("by_decoding", [])
        sample = [r for r in s3ak if r.get("mode", "").startswith("sample")]
        prior_block = {
            "source": args.prior,
            "source_sha256": sha256_file(Path(args.prior)),
            "mode": sample[0]["mode"] if sample else None,
            "state": sample[0]["state"] if sample else None,
            "n": sample[0]["n"] if sample else None,
            "looped_share": sample[0]["looped_share"] if sample else None,
            "natural_stop_share": sample[0]["natural_stop_share"] if sample else None,
            "unclosed_think_share_natural_stops":
                sample[0].get("unclosed_think_share_natural_stops") if sample else None,
            "mismatches_with_this_delta": [
                "другой чекпойнт (sft_resume_6000, не 21 500)",
                f"другой набор промптов (n={sample[0]['n'] if sample else '—'}, не 104)",
                "режим сэмплирования без запрета 4-грамм и без repetition_penalty "
                "(не тот режим, что просит TASK)",
            ],
            "reading": ("переносить эти числа на чекпойнт 21 500 нельзя: они говорят "
                        "о направлении (сэмплирование петлю ослабляет, но не снимает), "
                        "а не о величине"),
        }

    guard_block = None
    if args.guard and Path(args.guard).is_file():
        guard_block = {"path": args.guard, "sha256": sha256_file(Path(args.guard))}

    answer = derive_answer(reach, loop_origin, crash)

    #: Статус — одним полем, чтобы «диагноз поставлен, а измерение не снято» нельзя
    #: было прочитать как «измерено»: у свода два разных исхода, и они не сводятся
    #: к одному слову «готово».
    status = ("measured" if answer["sample_arm_measured"]
              else "diagnosis_complete_measurement_partial")

    doc = {
        "schema": "rl-rollout-viability/1",
        "status": status,
        "status_note": (
            "условие отказа, корень и починка установлены; измерение режима "
            "сэмплирования не снято (устройство мертво), поэтому числа есть только "
            "по штатному режиму — и это названо в answer и not_checked, а не "
            "подразумевается"
            if status != "measured" else
            "режим сэмплирования измерен на пиннутом чекпойнте: числа и вердикт ниже"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": "tools/assemble_rl_rollout_viability.py",
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "question": ("при каком условии падал режим сэмплирования V3, что это было — "
                     "прибор, окружение или модель — и пригодны ли траектории "
                     "rollout'а для RL"),
        "answer": answer,
        "crash": crash,
        "bisection": facts["bisection"],
        "fix": {**facts["fix"], "guard": guard_block},
        "metrics": {
            "source": "tools/analyze_rollout_reach.py (числа не пересчитываются сводом)",
            "reach": reach,
            "prior_evidence": prior_block,
        },
        "not_checked": facts["not_checked"],
        "artifacts": {
            "crash_facts": crash["facts_source"],
            "reach_survey": {"path": args.reach, "sha256": sha256_file(Path(args.reach))},
            "loop_origin": ({"path": args.loop_origin,
                             "sha256": sha256_file(Path(args.loop_origin)),
                             "tool": (loop_origin or {}).get("tool"),
                             "tool_sha256": (loop_origin or {}).get("tool_sha256"),
                             "tool_provenance": (
                                 "инструмент взят из ветви arch/sft-loop-origin "
                                 "(коммит 718df3e, S3ar) и не скопирован в эту ветвь: "
                                 "копия дала бы два расходящихся носителя одного "
                                 "инструмента (дрейф флота)")}
                            if loop_origin else None),
            "crash_log": crash["log"].get("path"),
            "crash_log_sha256": crash["log"].get("sha256"),
            "instrument_sha256": crash["instrument"]["sha256"],
            "guard": guard_block,
        },
        "input_errors": errors,
    }

    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"свод записан: {args.out}")
    print(f"  условие падения: S3 во время CUDA-прогона "
          f"({crash['suspend_window']['entry']}) → Xid 31 → "
          f"{crash['python_exception']}")
    print(f"  корень: окружение (прибор не тронут, sha256 {crash['instrument']['sha256'][:12]}…)")
    print(f"  ответ: {answer['verdict']}")
    if errors:
        print("ОТКАЗ: вход не тот — свод собран, но числам верить нельзя:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
