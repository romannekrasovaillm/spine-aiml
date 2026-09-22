#!/usr/bin/env python3
"""S3am — свод: штатный режим в АГЕНТНОЙ пробе и переснятая agentic-база.

Основание — ADR-041 (коммит `a091f23`): штатный режим декодирования (запрет повторов
4-грамм) распространяется на **все** пробы стадии, включая агентную. Причина названа
там же и она методическая, а не косметическая: язык и формат мерились в штатном
режиме, а агентность — в своём (`temperature 1.0`, `top_k 20`, без запрета), поэтому
критерий стадии (ADR-033 п.2в: «доля самостоятельных вызовов не падает, pass-rate
растёт») собирался из **несопоставимых** чисел, и прежняя база 45.1 % (158/350) в
этом критерии стоять больше не может.

Свод собирает две вещи, и обе — из сырых записей прогонов, а не из пересказа:

1. **Agentic-базу в штатном режиме** (`evidence/s3am-agentic-baseline.json`) — на том
   же CPT-чекпойнте, тех же подвыборках пулов v1/v2, том же приборе и тех же флагах,
   что и прежняя база, и с **явным сравнением** с legacy-числом: пересчитанным из
   сырых записей прежнего прогона (`runs/passrate-agentic-cpt-20260917-0153/`) и
   сверенным с замороженной цитатой ADR-033 (её держит страж
   `tools/check_sft_agentic_share.py`) — цитата и артефакт обязаны сойтись, иначе
   сравнивать не с чем. Различие (если есть) объясняется **измерением**, а не
   предположением: доля зацикленных ответов в обоих прогонах (тот же порог
   `probe_language_split.LOOP_MAX4GRAM_REP`, что у языковых проб) и распределение
   ходов. Плюс сверка протоколов, поле за полем: **единственное** различие прогонов
   обязано быть режимом декодирования; всё остальное попадает в
   `other_differences` — видимым списком, а не умолчанием.
2. **Вердикт по языку при n ≥ 100** (`evidence/s3am-language-verdict.json`) — остаток
   S3al, снятый той же цепочкой (`runs/s3al-decoding-protocol-20260918/chain.sh`) в
   штатном режиме. Арифметика вердикта **не переписана**: она берётся вызовом свода
   S3al (`assemble_s3al_evidence.language_block`), иначе «тот же критерий» стал бы
   обещанием.

Чего свод **не** делает: не переносит legacy-числа в критерий (они лежат рядом как
исторические, с названным режимом), не выносит вердикт по отчёту, помеченному
`allowed_for_conclusions = false`, и не подменяет решение архитектора о новой базе
критерия (ADR-033 п.2в) — он даёт числа и называет, где теперь стоит порог.

Коды возврата::

    0 — свод собран
    1 — отказ: вход противоречит контракту (штатный режим в приборе снят, прогон
        помечен непригодным для выводов, legacy-числа не сходятся с замороженной
        цитатой ADR-033)
    2 — NOT-VERIFIED: нечего сводить (нет прогона, нет сырых записей)
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

import passrate_probe as PR                      # noqa: E402  (прибор и его арифметика)
import probe_language_split as LS                # noqa: E402  (порог петель — тот же)
import probe_control as PC                       # noqa: E402  (метрики вырождения)
import assemble_s3al_evidence as AS              # noqa: E402  (вердикт по языку — вызовом)
import check_sft_agentic_share as GUARD          # noqa: E402  (замороженная база ADR-033)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Прогоны. Новый — S3am (штатный режим), прежний — S3aa (legacy, 17.09.2026) и
#: цепочка S3al (замер языка wide, штатный режим).
RUN = "runs/s3am-standard-mode-20260918"
AGENTIC = RUN + "/agentic"
LEGACY = "runs/passrate-agentic-cpt-20260917-0153"
S3AL_RUN = "runs/s3al-decoding-protocol-20260918"
S3AK_RUN = "runs/s3ak-degeneration-20260917"

#: Допуск сверки пересчитанного legacy-числа с замороженной цитатой ADR-033. Число
#: берётся у стража (`check_sft_agentic_share.BASE_REDERIVE_TOL`), а не назначается
#: здесь: сверяемся с тем же допуском, каким сверяется критерий стадии.
LEGACY_TOL = GUARD.BASE_REDERIVE_TOL

#: Поля протокола, которые обязаны совпасть у прежнего и нового прогона: различие
#: режима — предмет дельты, различие прочих полей — дефект постановки. Режим
#: (`decoding`) в список не входит: он и есть то, что меняется.
PROTOCOL_SAME_FIELDS = ("max_turns", "max_new_tokens", "temperature", "top_k",
                        "prefill", "toolcall_force", "n_attempts", "pass_at_k",
                        "seed", "batch", "weights", "weights_kind", "tokenizer",
                        "checkpoint_sha256", "init_special_tokens_subtoken")


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


def load_jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def nd(x, k: int = 4):
    return None if x is None else round(float(x), k)


# ─────────────────────── самопроверка правки прибора (S3am) ──────────────────

def probe_selfcheck() -> dict:
    """Штатный режим в приборе: проверка **вызовом**, а не чтением текста плана.

    Свод не пересказывает правку, а спрашивает сам прибор: чем разрешается умолчание,
    отклоняется ли прогон без запрета, чем именно назван выход для прежних чисел.
    Если прибор это перестанет делать, свод обязан покраснеть, а не «поверить».
    """
    default_n, refusal_default = PR.resolve_no_repeat_ngram(None, False)
    legacy_n, refusal_legacy = PR.resolve_no_repeat_ngram(None, True)
    zero_n, refusal_zero = PR.resolve_no_repeat_ngram(0, False)
    block = PR.decoding_protocol_block(default_n, None, False, 1.0, 20, 0)
    required = ("mode", "no_repeat_ngram", "temperature", "top_k")
    return {
        "default_mode": block["default_mode"],
        "standard_no_repeat_ngram": PR.STANDARD_NO_REPEAT_NGRAM,
        "default_resolves_to": default_n,
        "legacy_resolves_to": legacy_n,
        "run_without_ban_is_refused": bool(refusal_zero) and zero_n is None,
        "refusal_names_fix": bool(refusal_zero) and "--legacy-decoding" in refusal_zero,
        "refusal_text": refusal_zero,
        "protocol_field": "protocol.decoding " + "{" + ", ".join(required) + "}",
        "protocol_field_written": all(k in block for k in required),
        "same_resolver_as_language_probe": (
            PR.STANDARD_NO_REPEAT_NGRAM == LS.STANDARD_NO_REPEAT_NGRAM
            and PR.resolve_no_repeat_ngram(0, True) == LS.resolve_no_repeat_ngram(0, True)),
        "legacy_still_available": bool(refusal_legacy is None and legacy_n is None),
        "why": ("«закреплено в приборе» — утверждение о поведении, поэтому проверяется "
                "вызовом: разрешитель, отказ и блок протокола берутся у самого прибора"),
    }


# ─────────────────────────── числа прогона (agentic) ────────────────────────

def answer_repetition(answers_path: Path) -> dict:
    """Зацикленность ответов прогона — **по тому же порогу**, что у языковых проб.

    Зачем это здесь. Различие двух прогонов (legacy и штатного) обязано быть
    объяснено, а не названо. Запрет повторов 4-грамм снимает петлю **механически**
    (S3ak: 0.5417 → 0.0000), и если петли были в ответах прежнего прогона, различие
    чисел агентности объясняется режимом, а не «модель стала другой».

    Граница честности: в `answers.jsonl` текст хранится обрезанным до
    ``ANSWER_CAP`` символов (сырые ответы не коммитятся), а петля живёт как раз в
    длинных хвостах. Значит доля зацикленных — **нижняя оценка**, и число
    обрезанных рядом: без него «петель нет» читалось бы сильнее, чем измерено.
    """
    rows = load_jsonl(answers_path)
    n = len(rows)
    if not n:
        return {"available": False, "why": f"нет {rel(answers_path)}"}
    looped, uniq4, chars, truncated = 0, [], [], 0
    for r in rows:
        ans = r.get("answer") or ""
        if r.get("answer_truncated"):
            truncated += 1
        m = PC.degenerate_metrics(ans)
        rep = m.get("max4gram_rep")
        if rep is not None and rep >= LS.LOOP_MAX4GRAM_REP:
            looped += 1
        if m.get("uniq4") is not None:
            uniq4.append(m["uniq4"])
        chars.append(m["chars"])
    return {
        "available": True,
        "source": rel(answers_path),
        "n": n,
        "n_truncated": truncated,
        "truncated_note": (f"текст обрезан до {PR.ANSWER_CAP} символов — зацикленность "
                           f"измерена по обрезанному тексту и является **нижней** оценкой"),
        "rule": (f"зациклен = degenerate_metrics(text)[max4gram_rep] ≥ "
                 f"{LS.LOOP_MAX4GRAM_REP} (порог `probe_language_split.LOOP_MAX4GRAM_REP`, "
                 f"тот же, что у языковых проб)"),
        "looped": looped,
        "looped_share": nd(looped / n),
        "uniq4_mean": nd(sum(uniq4) / len(uniq4)) if uniq4 else None,
        "mean_answer_chars": nd(sum(chars) / n, 1),
    }


def pool_numbers(pool_dir: Path) -> dict:
    """Числа одного пула: доля самостоятельных вызовов, pass-rate, ходы, петли."""
    recs = load_jsonl(pool_dir / "tasks.jsonl")
    if not recs:
        return {"available": False, "why": f"нет сырых записей в {rel(pool_dir)}"}
    n = len(recs)
    n_self = sum(1 for r in recs if r.get("tool_calls", 0) > 0)
    n_pass = sum(int(r.get("pass") or 0) for r in recs)
    turns = [r.get("turns") or 0 for r in recs]
    lo_s, hi_s = PR.wilson_ci(n_self, n)
    lo_p, hi_p = PR.wilson_ci(n_pass, n)
    call_hist: dict[str, int] = {}
    for r in recs:
        k = str(r.get("tool_calls", 0))
        call_hist[k] = call_hist.get(k, 0) + 1
    by_type: dict[str, dict] = {}
    for r in recs:
        t = r.get("task_type", "?")
        d = by_type.setdefault(t, {"n": 0, "n_self": 0, "n_pass": 0})
        d["n"] += 1
        d["n_self"] += int(r.get("tool_calls", 0) > 0)
        d["n_pass"] += int(r.get("pass") or 0)
    for t, d in by_type.items():
        d["self_share"] = nd(d["n_self"] / d["n"])
        d["pass_rate"] = nd(d["n_pass"] / d["n"])
    return {
        "available": True,
        "n": n,
        "n_self_call": n_self,
        "self_call_share": nd(n_self / n),
        "self_call_share_ci95": [nd(lo_s), nd(hi_s)],
        "n_pass": n_pass,
        "pass_rate": nd(n_pass / n),
        "pass_rate_ci95": [nd(lo_p), nd(hi_p)],
        "turns_mean": nd(sum(turns) / n, 3),
        "turns_max": max(turns),
        "timeout_share": nd(sum(1 for r in recs if r.get("hit_timeout")) / n),
        "tool_calls_histogram": dict(sorted(call_hist.items(), key=lambda kv: int(kv[0]))),
        "by_task_type": dict(sorted(by_type.items())),
        "answer_repetition": answer_repetition(pool_dir / "answers.jsonl"),
        "self_call_note": ("самостоятельный вызов = траектория с tool_calls > 0 при "
                           "`--no-toolcall-force --no-hint`: первый вызов не вкладывает "
                           "харнесс, поэтому любой вызов — собственный"),
    }


def combined(pools: dict[str, dict]) -> dict:
    """Те же числа по обеим подвыборкам вместе (величина, которой живёт критерий)."""
    ok = {k: v for k, v in pools.items() if v.get("available")}
    n = sum(v["n"] for v in ok.values())
    n_self = sum(v["n_self_call"] for v in ok.values())
    n_pass = sum(v["n_pass"] for v in ok.values())
    lo_s, hi_s = PR.wilson_ci(n_self, n)
    lo_p, hi_p = PR.wilson_ci(n_pass, n)
    reps = [v["answer_repetition"] for v in ok.values()
            if v["answer_repetition"].get("available")]
    looped = sum(r["looped"] for r in reps)
    n_ans = sum(r["n"] for r in reps)
    return {
        "n": n,
        "n_self_call": n_self,
        "self_call_share": nd(n_self / n),
        "self_call_share_ci95": [nd(lo_s), nd(hi_s)],
        "n_pass": n_pass,
        "pass_rate": nd(n_pass / n),
        "pass_rate_ci95": [nd(lo_p), nd(hi_p)],
        "turns_mean": nd(sum(v["turns_mean"] * v["n"] for v in ok.values()) / n, 3),
        "timeout_share": nd(sum(v["timeout_share"] * v["n"] for v in ok.values()) / n),
        "looped": looped,
        "looped_share": nd(looped / n_ans) if n_ans else None,
        "pools": sorted(ok),
    }


# ─────────────────── сравнение с legacy-числом и объяснение ──────────────────

def legacy_reference(legacy_run: Path) -> dict:
    """Прежняя база: числа из **сырых записей** и сверка с замороженной цитатой.

    Числа не переписываются из ADR-033 в свод (иначе сверять было бы нечего):
    они считаются по сырым записям прежнего прогона и сравниваются с цитатой,
    которую держит страж критерия. Расхождение сверх допуска — отказ, а не
    «возьмём то, что удобнее»: сравнивать новое число не с чем.
    """
    pools = {name: pool_numbers(legacy_run / name) for name in ("v1", "v2")}
    if not any(p.get("available") for p in pools.values()):
        return {"available": False, "why": f"нет сырых записей прежнего прогона в "
                                           f"{rel(legacy_run)}"}
    comb = combined(pools)
    frozen = GUARD.BASE_FROZEN
    checks = []
    for name, exp in frozen["pools"].items():
        got = pools.get(name) or {}
        checks.append({
            "pool": name,
            "frozen_share": exp["share"], "derived_share": got.get("self_call_share"),
            "frozen_n": exp["n"], "derived_n": got.get("n"),
            "frozen_pass_rate": exp["pass_rate"], "derived_pass_rate": got.get("pass_rate"),
            "agrees": (bool(got.get("available"))
                       and got.get("n") == exp["n"]
                       and abs((got.get("self_call_share") or 0) - exp["share"]) <= LEGACY_TOL),
        })
    agrees = (all(c["agrees"] for c in checks)
              and comb["n"] == frozen["combined"]["n"]
              and abs(comb["self_call_share"] - frozen["combined"]["share"]) <= LEGACY_TOL)
    return {
        "available": True,
        "source": rel(legacy_run),
        "frozen_source": frozen["source"],
        "frozen_quote": {"n": frozen["combined"]["n"],
                         "n_self_call": frozen["combined"]["n_tool_call"],
                         "self_call_share": frozen["combined"]["share"],
                         "n_pass": frozen["combined"]["n_pass"],
                         "pass_rate": frozen["combined"]["pass_rate"]},
        "derived": {"n": comb["n"], "n_self_call": comb["n_self_call"],
                    "n_pass": comb["n_pass"], "self_call_share": comb["self_call_share"],
                    "pass_rate": comb["pass_rate"]},
        "pools": pools,
        "combined": comb,
        "frozen_agrees_with_derived": agrees,
        "frozen_checks": checks,
        "tolerance": LEGACY_TOL,
        "mode": "legacy (temperature 1.0, top_k 20, запрет повторов выключен)",
        "status": "историческая: вне критерия стадии (ADR-041 п.3)",
    }


def protocol_diff(new_meta: dict, old_meta: dict) -> dict:
    """Что различается в протоколах прогонов — списком, а не умолчанием.

    Дельта заявлена как «тот же прибор, те же флаги, изменён только режим». Это
    утверждение проверяется полем за полем: всё, что не совпало и не является
    режимом, попадает в `other_differences`. Молчаливое расхождение по флагам —
    ровно тот дефект, на котором уже спотыкалась посылка ADR-033 п.2.
    """
    new_p, old_p = (new_meta.get("protocol") or {}), (old_meta.get("protocol") or {})
    same, diff = {}, {}
    for f in PROTOCOL_SAME_FIELDS:
        a, b = new_p.get(f), old_p.get(f)
        (same if a == b else diff)[f] = {"new": a, "legacy": b}
    new_d, old_d = (new_p.get("decoding") or {}), (old_p.get("decoding") or {})
    return {
        "same": sorted(same),
        "other_differences": diff,
        "only_mode_differs": not diff,
        "decoding": {
            "new": {"mode": new_d.get("mode"), "no_repeat_ngram": new_d.get("no_repeat_ngram"),
                    "temperature": new_d.get("temperature"), "top_k": new_d.get("top_k")},
            "legacy": {"mode": ("legacy (temperature 1.0, top_k 20, без запрета повторов)"
                                if not old_d else old_d.get("mode")),
                       "no_repeat_ngram": old_d.get("no_repeat_ngram"),
                       "temperature": (old_d.get("temperature")
                                       if old_d else old_p.get("temperature")),
                       "top_k": old_d.get("top_k") if old_d else old_p.get("top_k")},
        },
        "why_it_matters": ("смена режима — это вмешательство в поведение, а не только в "
                           "метрику (ADR-041, «Negative»): запрет повторов меняет длину "
                           "и содержание ходов, поэтому различие чисел агентности "
                           "**ожидаемо** и объясняется режимом, а не читается как "
                           "«модель стала агентнее»"),
    }


def ban_interaction(run: Path) -> dict:
    """Отчёт прибора «запрет против канала вызова» — объяснение различия чисел.

    Свод не пересказывает механизм своими словами: он берёт отчёт прибора
    (`tools/analyze_agentic_ban_interaction.py`) и переносит в вывод только вердикт и
    числа, оставляя рядом путь и хеш. Если отчёта нет — это названо, а не замолчано:
    без него различие базы с legacy остаётся необъяснённым.
    """
    p = run / "ban_protocol_interaction.json"
    d = load(p)
    if d is None:
        return {"available": False,
                "why": f"нет {rel(p)} — различие с legacy не объяснено прибором"}
    b = d.get("behaviour") or {}
    return {
        "available": True,
        "report": rel(p),
        "report_sha256": sha256_file(p),
        "instrument": "tools/analyze_agentic_ban_interaction.py",
        "mechanism_confirmed": bool((d.get("verdict") or {}).get("mechanism_confirmed")),
        "statement": (d.get("verdict") or {}).get("statement"),
        "canonical_call_blocked": {f["form"]: f["under_standard_ban"]["would_emit"]
                                   for f in (d.get("canonical_forms") or [])
                                   if f.get("blocked_by_ban")},
        "control_without_ban": (d.get("verdict") or {}).get("control_without_ban"),
        "tool_errors": {
            "standard": ((b.get("standard_run") or {}).get("combined")),
            "legacy": ((b.get("legacy_run") or {}).get("combined")),
        },
        "alternative_domains": d.get("alternative_domains"),
        "what_it_does_not_decide": d.get("what_it_does_not_decide"),
    }


def delta_reading(new: dict, old: dict) -> dict:
    """Различие двух замеров с интервалом: значимо или неотличимо от нуля.

    Интервал — Newcombe (разность двух долей), тот же, что у прибора: считать
    «стало больше/меньше» по двум точкам без интервала — это то, чем числа стадии
    уже дважды вводили в заблуждение.

    Порядок аргументов назван явно: `newcombe_diff_ci(k1, n1, k2, n2)` возвращает
    интервал для ``p2 − p1``. Чтобы интервал был интервалом **дельты** (новое минус
    прежнее), первым идёт прежний замер — иначе знаки интервала и дельты разошлись бы,
    а читатель увидел бы «−0.086 при интервале [+0.013; +0.157]».
    """
    out = {}
    for key, n_key in (("self_call_share", "n"), ("pass_rate", "n")):
        k_new = int(round((new.get(key) or 0) * new[n_key]))
        k_old = int(round((old.get(key) or 0) * old[n_key]))
        lo, hi = PR.newcombe_diff_ci(k_old, old[n_key], k_new, new[n_key])
        diff = (new.get(key) or 0) - (old.get(key) or 0)
        out[key] = {
            "new": new.get(key), "legacy": old.get(key),
            "delta": nd(diff),
            "ci95_newcombe_delta": [nd(lo), nd(hi)],
            "ci95_note": "интервал дельты (новое − прежнее), метод Newcombe; "
                         "тот же, каким прибор сравнивает пулы",
            "significant": bool(lo > 0 or hi < 0),
            "reading": ("различие неотличимо от нуля при этом n — интервал накрывает 0"
                        if not (lo > 0 or hi < 0) else
                        "интервал не накрывает 0: различие переживает ошибку выборки"),
            "comparability_note": ("сравнение допустимо: протоколы совпадают по всем полям, "
                                   "кроме режима декодирования (см. protocol_diff); "
                                   "различие относится к режиму, а не к модели"),
        }
    return out


def pick_language_state(run: Path, requested: str | None) -> tuple[str | None, str]:
    """Какое состояние решает вердикт по языку — и почему именно оно.

    Свод S3al объявлял решающим `sft_resume_6000` (то состояние, на котором снят
    пилот). К S3am этого чекпойнта на диске **нет**: SFT-прогон идёт и держит только
    свежие точки (ретенция), поэтому `sft_probe_6000.pt` удалён. Выбор состояния —
    утверждение о вердикте, поэтому он не берётся молча: берётся свежайшее доступное
    SFT-состояние, а причина названа рядом. Если прежнее состояние вернётся на диск
    (или будет указано флагом), вердикт вернётся к нему.
    """
    d = load(run / "language_wide_standard.json")
    states = list((d or {}).get("states") or {})
    if requested:
        return requested, "состояние указано флагом --language-state"
    if not states:
        return "sft_resume_6000", f"нет отчёта {rel(run / 'language_wide_standard.json')} — "\
                                  "состояние по умолчанию (пилот S3ak)"
    if "sft_resume_6000" in states:
        return "sft_resume_6000", "состояние, объявленное решающим в своде S3al"
    sft = [s for s in states if s.startswith("sft_resume_")
           and s.rsplit("_", 1)[-1].isdigit()]
    if sft:
        best = max(sft, key=lambda s: int(s.rsplit("_", 1)[-1]))
        return best, ("sft_resume_6000 на диске нет (чекпойнт удалён ретенцией идущего "
                      f"SFT-прогона) — взято свежайшее доступное состояние {best}; "
                      "вердикт относится к нему")
    return states[0], f"состояний sft_resume_* нет — взято первое из {states}"


def agentic_block(run: Path, legacy_run: Path, delta_run: Path) -> dict:
    meta_new = load(run / "probe_meta.json") or load(run / "v2" / "probe_meta.json") or {}
    pools_new = {name: pool_numbers(run / name) for name in ("v1", "v2")}
    if not any(p.get("available") for p in pools_new.values()):
        return {"available": False, "why": f"нет сырых записей прогона в {rel(run)}"}
    comb_new = combined(pools_new)
    legacy = legacy_reference(legacy_run)
    decoding = ((meta_new.get("protocol") or {}).get("decoding") or {})
    block = {
        "available": True,
        "mode": decoding.get("mode"),
        "no_repeat_ngram": decoding.get("no_repeat_ngram"),
        "temperature": decoding.get("temperature"),
        "top_k": decoding.get("top_k"),
        "standard": decoding.get("standard"),
        "allowed_for_conclusions": decoding.get("allowed_for_conclusions"),
        "all_banned_fallbacks": decoding.get("all_banned_fallbacks"),
        "n": comb_new["n"],
        "self_call_share": comb_new["self_call_share"],
        "self_call_share_ci95": comb_new["self_call_share_ci95"],
        "n_self_call": comb_new["n_self_call"],
        "pass_rate": comb_new["pass_rate"],
        "pass_rate_ci95": comb_new["pass_rate_ci95"],
        "n_pass": comb_new["n_pass"],
        "looped_share": comb_new["looped_share"],
        "pools": pools_new,
        "combined": comb_new,
        "checkpoint": (meta_new.get("checkpoint") or {}).get("path"),
        "checkpoint_sha256": ((meta_new.get("protocol") or {}).get("checkpoint_sha256")),
        "run_manifest": rel(run / "run_manifest.json"),
        "raw_records": {name: rel(run / name / "tasks.jsonl") for name in ("v1", "v2")},
        "legacy_reference": legacy,
    }
    if legacy.get("available"):
        block["delta_vs_legacy"] = delta_reading(comb_new, legacy["combined"])
        block["mechanism"] = {
            "looped_share_standard": comb_new["looped_share"],
            "looped_share_legacy": legacy["combined"]["looped_share"],
            "looped_rule": (f"max4gram_rep ≥ {LS.LOOP_MAX4GRAM_REP} по тексту ответа "
                            f"(обрезан до {PR.ANSWER_CAP} символов — нижняя оценка)"),
            "reading": ("петля — механизм различия: запрет повторов 4-грамм снимает её "
                        "механически (S3ak: 0.5417 → 0.0000), и если в прежнем прогоне "
                        "зацикленных ответов было больше, различие чисел агентности "
                        "объясняется режимом декодирования"),
            "turns_mean_standard": comb_new.get("turns_mean"),
            "turns_mean_legacy": legacy["combined"].get("turns_mean"),
            "ban_interaction": ban_interaction(delta_run),
        }
        #: Вердикт о пригодности базы. Числа сняты — это факт; но **пригодна ли** база
        #: как точка отсчёта критерия — отдельное утверждение, и оно проверяется
        #: прибором: если запрет перерезает канал вызова, база измеряет не агентность.
        bi = block["mechanism"]["ban_interaction"]
        cut = bool(bi.get("available") and bi.get("mechanism_confirmed"))
        te_std = ((bi.get("tool_errors") or {}).get("standard") or {})
        te_leg = ((bi.get("tool_errors") or {}).get("legacy") or {})
        block["verdict"] = {
            "base_measured": True,
            "base_valid_as_criterion_reference": not cut,
            "why": (
                "штатный режим перерезает канал вызова инструмента: каноническая форма "
                "`tool_call` запрещена (её токен-4-граммы уже есть в системном промпте, "
                "где лежит образец вызова), поэтому модель печатает испорченный JSON — "
                f"ошибок инструмента {te_std.get('n_error_events')} на "
                f"{te_std.get('n_calls')} вызовов против {te_leg.get('n_error_events')} "
                f"на {te_leg.get('n_calls')} в legacy. Переснятая база измеряет поведение "
                "модели под запретом, который ей же запрещает печатать вызов, — а не "
                "«агентность модели»"
                if cut else
                "прибор «запрет против канала вызова» не подтвердил, что канал перерезан: "
                "переснятая база годна как точка отсчёта (при названной проверке)"),
            "do_not": ("не подставлять эти числа в критерий ADR-033 п.2в как точку отсчёта, "
                       "пока канал вызова перерезан; и не возвращать legacy 45.1 % как "
                       "«сопоставимое» — оно снято в другом режиме (ADR-041 п.3)"),
            "options_for_architect": [
                "исключить синтаксис вызова из запрета (запрет не действует внутри "
                "блока <tool_call>) — канал жив, но режим перестаёт быть единым",
                "считать запрет только по сгенерированным токенам, без промпта: "
                "каноническая форма проходит, повтор своего же вызова запрещён "
                "(измерено прибором) — расходится с правилом transformers и с «тем же "
                "режимом, что у языковых проб»",
                "оставить агентную пробу в legacy-режиме с явной пометкой: числа "
                "агентности тогда несопоставимы с языковыми — цена, которую ADR-041 "
                "как раз и отказывался платить",
                "пересмотреть формат: убрать образец вызова из системного промпта "
                "нельзя (это обученный формат), значит цена ложится на выбор выше",
            ],
            "measured_by": "tools/analyze_agentic_ban_interaction.py (отчёт в run-dir)",
        }
    return block


# ──────────────────────────── тесты и гейт ──────────────────────────────────

def tests_block(run: Path) -> dict:
    """Прогон тестов приборов: добавленные дельтой проверки и красное **вне** известного.

    «Известный красный» берётся из свода S3aj (как и у S3al), а не переписывается
    сюда: список «что было красным до дельты» — утверждение о прошлом.
    """
    p = run / "tool_tests.log"
    if not p.is_file():
        return {"available": False, "why": f"нет {rel(p)}"}
    lines = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    failures = [ln.strip().lstrip("- ").strip() for ln in lines
                if ln.strip().startswith("- ")]
    prev = load(CASE / "evidence/s3aj-format-validity.json")
    known = ((prev or {}).get("tests") or {}).get("failures") or []
    marker = "== 23. passrate_probe: штатный режим декодирования (S3am) =="
    start = lines.index(marker) if marker in lines else None
    section = lines[start:] if start is not None else []
    return {
        "available": True,
        "log": rel(p),
        "log_sha256": sha256_file(p),
        "tally": lines[-1] if lines else "",
        "failures": failures,
        "known_red": known,
        "known_red_source": "evidence/s3aj-format-validity.json",
        "failures_outside_known_red": [f for f in failures if f not in known],
        "s3am_checks": sum(1 for ln in section if ln.startswith("  ok")),
        "s3am_section_found": start is not None,
        "s3am_failures": [ln for ln in section if ln.startswith("  FAIL")],
    }


def cuda_block(run: Path) -> dict:
    """Доступность CUDA: проверка **настоящей аллокацией**, а не `nvidia-smi`."""
    d = load(run / "device_check.json")
    if d is None:
        return {"available": None, "why": f"нет {rel(run / 'device_check.json')}"}
    return {
        "available": bool(d.get("alloc_ok")),
        "evidence": rel(run / "device_check.json"),
        "interpreter": "/usr/bin/python3",
        "torch": d.get("torch"), "cuda_built": d.get("cuda_built"),
        "device_name": d.get("device_name"),
        "free_bytes_at_check": d.get("free_bytes"),
        "error": d.get("error"),
        "why_allocation": ("`nvidia-smi` показывает карту, но не отвечает на вопрос "
                           "«принимает ли она контекст»: 18.09 карта была видна, а "
                           "CUDA-контекст не создавался (Xid 31). Проверка аллокацией "
                           "отвечает на вопрос, который нужен запуску"),
    }


# ─────────────────────────────── main ───────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default=RUN)
    ap.add_argument("--agentic-run", default=None, help=f"по умолчанию {AGENTIC}")
    ap.add_argument("--legacy-run", default=LEGACY)
    ap.add_argument("--s3al-run", default=S3AL_RUN)
    ap.add_argument("--s3ak-run", default=S3AK_RUN)
    ap.add_argument("--out-agentic", default="evidence/s3am-agentic-baseline.json")
    ap.add_argument("--out-language", default="evidence/s3am-language-verdict.json")
    ap.add_argument("--language-state", default=None,
                    help="какое состояние решает вердикт ADR-039; по умолчанию — "
                         "решающее состояние S3al, если оно есть в отчёте, иначе "
                         "свежайшее доступное SFT-состояние (причина пишется в свод)")
    args = ap.parse_args(argv)

    run = CASE / args.run
    agentic_run = CASE / (args.agentic_run or run / "agentic")
    legacy_run = CASE / args.legacy_run

    self_check = probe_selfcheck()
    #: Отказ, если штатный режим в приборе снят: тогда весь свод мерил бы не то,
    #: а «штатный режим в агентной пробе» осталось бы утверждением в тексте.
    if self_check["default_resolves_to"] != self_check["standard_no_repeat_ngram"]:
        note("отказ: прибор не применяет штатный режим по умолчанию — свод построен "
             "на неверной посылке")
        return EXIT_FAIL
    if not self_check["run_without_ban_is_refused"] or not self_check["refusal_names_fix"]:
        note("отказ: прибор не отклоняет прогон без запрета повторов — штатный режим "
             "не закреплён")
        return EXIT_FAIL

    if not agentic_run.is_dir():
        note(f"NOT-VERIFIED: нет каталога прогона {rel(agentic_run)}")
        return EXIT_NOT_VERIFIED
    meta = load(agentic_run / "v2" / "probe_meta.json") or load(agentic_run / "probe_meta.json")
    if meta is None:
        note(f"NOT-VERIFIED: нет probe_meta.json в {rel(agentic_run)}")
        return EXIT_NOT_VERIFIED
    decoding = ((meta.get("protocol") or {}).get("decoding") or {})
    #: Прогон, помеченный непригодным для выводов (снят с --legacy-decoding), в свод
    #: базы не берётся: иначе он вернулся бы в критерий под новым именем.
    if decoding.get("allowed_for_conclusions") is False:
        note("отказ: прогон снят в режиме, помеченном непригодным для выводов "
             "(protocol.decoding.allowed_for_conclusions=false)")
        return EXIT_FAIL
    if decoding.get("no_repeat_ngram") is None:
        note("отказ: в отчёте не назван запрет повторов n-грамм — базу не отличить "
             "от legacy-числа (ADR-041 п.4)")
        return EXIT_FAIL

    #: Цитата ADR-033 и артефакт обязаны сойтись, иначе сравнивать новое число не с чем.
    legacy_probe = legacy_reference(legacy_run)
    if legacy_probe.get("available") and not legacy_probe["frozen_agrees_with_derived"]:
        note("отказ: пересчитанные legacy-числа не сходятся с замороженной цитатой "
             "ADR-033 (evidence/s3aa-agentic-cpt.json) — сравнение было бы с "
             "неизвестной базой")
        return EXIT_FAIL

    agentic = agentic_block(agentic_run, legacy_run, run)
    if not agentic.get("available"):
        note(f"NOT-VERIFIED: {agentic.get('why')}")
        return EXIT_NOT_VERIFIED
    legacy_meta = load(legacy_run / "v2" / "probe_meta.json") or {}
    agentic["protocol_diff"] = protocol_diff(meta, legacy_meta)

    lang_state, lang_state_why = pick_language_state(CASE / args.s3al_run, args.language_state)
    lang_src = AS.language_block(CASE / args.s3al_run, CASE / args.s3ak_run, lang_state)
    lang_by_state = AS.language_by_state(CASE / args.s3al_run, CASE / args.s3ak_run)

    tests = tests_block(run)
    fitness = AS.fitness_block(run)
    cuda = cuda_block(run)

    common_artifacts = [
        ("прибор: штатный режим в агентной пробе (правка S3am)",
         CASE / "tools/passrate_probe.py"),
        ("прибор: механизм «запрет против канала вызова»",
         CASE / "tools/analyze_agentic_ban_interaction.py"),
        ("отчёт прибора о механизме", run / "ban_protocol_interaction.json"),
        ("свод (этот файл собран им)", CASE / "tools/assemble_s3am_evidence.py"),
        ("тесты приборов (зелёный и красный путь, раздел 23)",
         CASE / "tools/tests/run_tool_tests.sh"),
        ("цепочка замеров S3am (объявлена целиком)",
         run / "chain.sh"),
        ("финализация: артефакты → своды", run / "finalize.sh"),
        ("проверка устройства настоящей аллокацией", run / "device_check.json"),
        ("прогон тестов", run / "tool_tests.log"),
        ("гейт fitness_check", run / "fitness_gate.json"),
        ("гейт fitness_check: база на HEAD", run / "fitness_gate_baseline_head.json"),
    ]

    out_agentic = {
        "schema": "s3am-agentic-baseline/1",
        "stage": "S3am — штатный режим декодирования в агентной пробе (ADR-041) и "
                 "переснятая agentic-база",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "measured",
        "question": "какие числа агентности даёт агентная проба в штатном режиме "
                    "стадии (запрет повторов 4-грамм) и как они соотносятся с "
                    "исторической базой 45.1 % (158/350)",
        "probe_patch": {
            **self_check,
            "tests": {
                "section": "23 (run_tool_tests.sh)",
                "checks_ok": tests.get("s3am_checks"),
                "failures": tests.get("s3am_failures"),
                "log": tests.get("log"), "tally": tests.get("tally"),
            },
            "refusal_without_flag": self_check["run_without_ban_is_refused"],
            "legacy_flag": "--legacy-decoding (мост воспроизводимости прежних чисел)",
        },
        "cuda": cuda,
        "agentic_baseline": {k: v for k, v in agentic.items() if k != "pools"},
        "agentic_by_pool": agentic.get("pools"),
        "tests": tests,
        "fitness_gate": fitness,
        "artifacts": [{"path": rel(p), "sha256": sha256_file(p) if p.is_file() else None,
                       "role": role, "exists": p.is_file()}
                      for role, p in common_artifacts + [
                          ("прогон: манифест", agentic_run / "run_manifest.json"),
                          ("прогон: сырые записи пула v1", agentic_run / "v1" / "tasks.jsonl"),
                          ("прогон: сырые записи пула v2", agentic_run / "v2" / "tasks.jsonl"),
                          ("прогон: протокол и метаданные пула v2",
                           agentic_run / "v2" / "probe_meta.json"),
                          ("исторический прогон (legacy-база)",
                           legacy_run / "run_manifest.json"),
                      ]],
        "open_questions": [
            "новая база критерия ADR-033 п.2в не назначена: свод даёт числа штатного "
            "режима, но годность их как точки отсчёта зависит от решения по каналу "
            "вызова (см. agentic_baseline.verdict) — решение архитектора",
            "штатный запрет повторов 4-грамм запрещает модели печатать канонический "
            "tool_call (её токен-4-граммы лежат в системном промпте образцом): выбрать "
            "область запрета — единый режим с перерезанным каналом, исключение синтаксиса "
            "вызова или запрет только по сгенерированному — решение архитектора",
            "страж критерия (`check_sft_agentic_share.BASE_FROZEN`) цитирует legacy-число "
            "45.1 %: после ADR-041 он сравнивает числа разных режимов, и это надо "
            "разрешить явно — исполнитель решение о критерии не подменяет",
            "формат канонического вызова из системного промпта — часть обученного "
            "контура; «убрать образец из промпта» вариантом не является, значит цена "
            "выбора ложится на режим, а не на промпт",
            "петли измерены по обрезанному тексту ответов (ANSWER_CAP): доля "
            "зацикленных — нижняя оценка, и для точного механизма нужен полный текст "
            "(в прогоне он не хранится)",
            "правило ADR-041 п.1 («во всех инструментах оценки») закрыто в этой дельте "
            "для агентной пробы; `tools/probe_control.py` (историческая проба ядра S3ab) "
            "и `tools/rl_probe_hook.py` штатного запрета не знают — их числа исторические "
            "и контурные, но если по ним будут делать выводы стадии, правило надо "
            "распространить и туда",
        ],
    }
    if args.out_agentic:
        Path(args.out_agentic).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_agentic).write_text(
            json.dumps(out_agentic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        note(f"свод: {args.out_agentic}")
    else:
        print(json.dumps(out_agentic, ensure_ascii=False, indent=2))

    out_lang = {
        "schema": "s3am-language-verdict/1",
        "stage": "S3am — вердикт по языку при n ≥ 100 (остаток S3al), штатный режим",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "measured" if lang_src.get("available") and not lang_src.get("is_pilot")
                  else "partial",
        "question": "удерживается ли вердикт ADR-039 (ответная часть русская при "
                    "английском рассуждении), когда петля снята штатным режимом и "
                    "выборка доведена до n ≥ 100",
        "source_report": rel(CASE / args.s3al_run / "language_wide_standard.json"),
        "arithmetic_from": "assemble_s3al_evidence.language_block (вызов, не копия)",
        "sample_floor": AS.SAMPLE_FLOOR,
        "deciding_state": lang_state,
        "deciding_state_why": lang_state_why,
        "language_verdict": lang_src,
        "language_by_state": lang_by_state,
        "agentic_reference": {
            "path": args.out_agentic,
            "mode": agentic.get("mode"),
            "n": agentic.get("n"),
            "self_call_share": agentic.get("self_call_share"),
            "pass_rate": agentic.get("pass_rate"),
            "why_here": ("числа агентности лежат здесь ссылкой, а не копией: критерий "
                         "стадии собирается из языка, формата и агентности, и читателю "
                         "нужно видеть, что все три сняты одним режимом"),
        },
        "artifacts": [{"path": rel(p), "sha256": sha256_file(p) if p.is_file() else None,
                       "role": role, "exists": p.is_file()}
                      for role, p in [
                          ("прибор языковых проб (штатный режим)", CASE / "tools/probe_language_split.py"),
                          ("свод S3al (арифметика вердикта)", CASE / "tools/assemble_s3al_evidence.py"),
                          ("цепочка S3al (замер языка wide)", CASE / args.s3al_run / "chain.sh"),
                          ("отчёт замера языка wide", CASE / args.s3al_run / "language_wide_standard.json"),
                          ("свод S3al с вердиктом", CASE / "evidence/s3al-decoding-protocol.json"),
                      ]],
        "open_questions": [
            "вердикт считается по состоянию sft_resume_6000 (шаг 6000 возобновлённого "
            "SFT): SFT-прогон идёт, и «свежее» состояние на момент замера названо в "
            "language_by_state — вердикт относится к нему, а не к финалу стадии",
            "штатный режим снят на бюджете 4096 токенов с остановкой на конце хода: "
            "смена бюджета — смена прибора, и сравнение с S3aj допустимо только по "
            "числам того же бюджета",
        ],
    }
    if args.out_language:
        Path(args.out_language).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_language).write_text(
            json.dumps(out_lang, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        note(f"свод: {args.out_language}")
    else:
        print(json.dumps(out_lang, ensure_ascii=False, indent=2))

    summary = {
        "agentic": {k: agentic.get(k) for k in
                    ("mode", "n", "self_call_share", "pass_rate", "looped_share")},
        "legacy": (legacy_probe.get("derived") if legacy_probe.get("available") else None),
        "delta": (agentic.get("delta_vs_legacy") or {}).get("self_call_share"),
        "language": {k: lang_src.get(k) for k in
                     ("n", "coverage", "cyr_answer_clean_with_zeros", "margin_to_0.4",
                      "verdict_adr039", "zone")},
        "cuda_available": cuda.get("available"),
        "only_mode_differs": agentic.get("protocol_diff", {}).get("only_mode_differs"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
