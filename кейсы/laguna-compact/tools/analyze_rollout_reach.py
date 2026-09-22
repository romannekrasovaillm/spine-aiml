#!/usr/bin/env python3
"""analyze_rollout_reach.py — сколько ходов доходят до ответа без петель.

**Вопрос, на который отвечает инструмент.** RL сэмплирует rollout'ы: траектория
годится, если модель **дошла до ответа** и **не зациклилась**. Прежде чем считать
это вопросом режима обучения, его надо уметь посчитать по готовому отчёту проб —
и посчитать так, чтобы число можно было перепроверить, а не пересказать.

**Что именно считается.** Инструмент ничего не переизмеряет: он читает те поля,
которые уже проставил прибор (`stop_reason`, `hit_limit`, `metrics.looped`,
`metrics.unclosed_think`), и складывает из них один ответ:

    дошёл до ответа без петли := stop_reason == "turn_end"
                                И не metrics.looped
                                И не metrics.unclosed_think

Почему конъюнкция именно такая. `turn_end` — ход дописан моделью (усечённый ход
назван `limit_in_<регион>` и в ответ не годится: он не кончился). Незакрытый
`<think>` по правилу прибора означает, что **ответной части у хода нет вовсе**
(остаток хода уходит в рассуждение, ADR-045 п.5) — значит закрытый блок нужен,
чтобы «дошёл до ответа» было правдой. Петля обнуляет траекторию для RL: повтор
блока не несёт нового сигнала, а награда за таким ходом неотличима от награды за
его обрывок.

**Две границы, которые инструмент обязан назвать, а не проглотить.**

1. ADR-045 п.5: «незакрытый `<think>`» — дефект **только среди естественно
   завершённых** ходов. Поэтому доля считается на знаменателе `turn_end`, а не на
   всех ходах, и оба числа печатаются рядом: подмена знаменателя — ровно тот
   дефект, который вскрыла поправка ADR-050 п.4.
2. ADR-050 п.4.3: если в бюджет упёрлось ≥ `SATURATION_LIMIT` ходов, прибор
   измеряет бюджет, а не модель, и вердикт по формату не выносится вовсе. Поле
   `format_verdict_issuable` проставляет это правило; `reached_answer_no_loop`
   при этом остаётся числом, но читается как **нижняя** оценка: часть ходов
   оборвана бюджетом, а не поведением.

Коды возврата::

    0 — отчёт собран
    1 — отказ: отчёт не читается или в нём нет проб (вход назван)
    2 — NOT-VERIFIED: отчётов не передано (мерить нечего)

Запуск::

    python3 tools/analyze_rollout_reach.py \\
        --report runs/<run>/format_wide_4096_pair.json \\
        --out evidence/rl-rollout-viability.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Порог сатурации прибора (ADR-050 п.4.3): доля ходов, упёршихся в бюджет, выше
#: которой вердикт по формату не выносится. Объявлен константой, а не числом в
#: тексте: он один на инструмент и на решение.
SATURATION_LIMIT = 0.20

#: Правило «дошёл до ответа без петли» — в отчёт полем, чтобы читатель числа не
#: восстанавливал его из кода и не подменял знаменатель по памяти.
REACH_RULE = ("stop_reason == turn_end И не metrics.looped И не "
              "metrics.unclosed_think (закрытый блок рассуждения обязателен: "
              "по правилу прибора незакрытый <think> означает отсутствие "
              "ответной части, ADR-045 п.5)")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def percentile_nearest_rank(values: list[int], q: float) -> int | None:
    """Ближайший ранг, как в приборе: интерполяция дала бы число, которого нет."""
    if not values:
        return None
    xs = sorted(values)
    idx = max(0, min(len(xs) - 1, math.ceil(q * len(xs)) - 1))
    return xs[idx]


def survey_state(probes: list[dict], aggregate: dict | None) -> dict:
    """Один ответ на один ряд проб. Считается по полям прибора, без переизмерения."""
    n = len(probes)
    nat = [p for p in probes if p.get("stop_reason") == "turn_end"]
    trunc = [p for p in probes if p.get("hit_limit")]
    looped = [p for p in probes if p.get("metrics", {}).get("looped")]
    unclosed = [p for p in nat if p.get("metrics", {}).get("unclosed_think")]
    reached = [p for p in probes
               if p.get("stop_reason") == "turn_end"
               and not p.get("metrics", {}).get("looped")
               and not p.get("metrics", {}).get("unclosed_think")]
    looped_nat = [p for p in nat if p.get("metrics", {}).get("looped")]
    lengths = [int(p.get("n_new_tokens", 0)) for p in probes]
    truncated_share = len(trunc) / n if n else None
    sat = (truncated_share is not None and truncated_share >= SATURATION_LIMIT)
    return {
        "n": n,
        "reached_answer_no_loop": len(reached),
        "reached_answer_no_loop_share": (len(reached) / n) if n else None,
        "truncated": len(trunc),
        "truncated_share": truncated_share,
        "natural_stop": len(nat),
        "natural_stop_share": (len(nat) / n) if n else None,
        "looped": len(looped),
        "looped_share": (len(looped) / n) if n else None,
        "looped_among_natural": len(looped_nat),
        "looped_share_among_natural": (len(looped_nat) / len(nat)) if nat else None,
        "unclosed_think_among_natural": len(unclosed),
        "unclosed_think_share_among_natural": (len(unclosed) / len(nat)) if nat else None,
        "stop_reasons": dict(Counter(p.get("stop_reason") for p in probes)),
        "lengths": {
            "median": percentile_nearest_rank(lengths, 0.5),
            "p90": percentile_nearest_rank(lengths, 0.9),
            "max": max(lengths) if lengths else None,
            "budget": (aggregate or {}).get("lengths", {}).get("budget"),
        },
        "saturation": {
            "limit": SATURATION_LIMIT,
            "share": truncated_share,
            "saturated": bool(sat),
            "note": ("в бюджет упёрлось ≥ 20 % ходов: прибор измеряет бюджет, а не "
                     "модель (ADR-050 п.4.3) — вердикт по формату не выносится, "
                     "а reach читается как нижняя оценка"
                     if sat else
                     "доля усечённых ниже порога сатурации: ряд пригоден для "
                     "вердикта по формату (ADR-050 п.4.3)"),
        },
        "format_verdict_issuable": not sat,
    }


def survey_report(path: Path, states: list[str] | None) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    proto = d.get("protocol", {})
    out = {
        "report": str(path),
        "report_sha256": sha256_file(path),
        "schema": d.get("schema"),
        "tool_sha256": d.get("tool_sha256"),
        "device": d.get("device"),
        "protocol": {
            k: proto.get(k) for k in
            ("prompts_set", "n_prompts", "prompts_digest", "decoding",
             "decoding_params", "batch_size", "max_new_tokens", "stop_at_turn_end",
             "instrument")
        },
        "states": {},
    }
    for tag, st in d.get("states", {}).items():
        if states and tag not in states:
            continue
        if not isinstance(st, dict) or "probes" not in st:
            out["states"][tag] = {"skipped": "в состоянии нет проб"}
            continue
        rec = survey_state(st["probes"], st.get("aggregate"))
        rec["checkpoint"] = st.get("checkpoint")
        rec["checkpoint_sha256"] = st.get("checkpoint_sha256")
        out["states"][tag] = rec
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", action="append", default=[],
                    help="отчёт проб прибора (можно несколько)")
    ap.add_argument("--state", action="append", default=[],
                    help="состояние внутри отчёта (по умолчанию все)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if not args.report:
        print("NOT-VERIFIED: ни одного --report — считать нечего (это не результат)",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    rows, refused = [], []
    for r in args.report:
        p = Path(r)
        if not p.is_file():
            refused.append({"report": r, "why": "файла нет"})
            continue
        try:
            rows.append(survey_report(p, args.state or None))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            refused.append({"report": r, "why": f"{type(exc).__name__}: {exc}"})

    usable = [r for r in rows
              if any("n" in st for st in r["states"].values())]
    if not usable:
        print("NOT-VERIFIED: ни в одном отчёте нет проб — мерить нечего "
              "(отказ входа, а не результат о модели)", file=sys.stderr)
        for r in refused:
            print(f"  отказ: {r['report']}: {r['why']}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    doc = {
        "schema": "rollout-reach/1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": "tools/analyze_rollout_reach.py",
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "question": ("сколько ходов доходят до ответа без петель — то есть сколько "
                     "траекторий rollout'а годны для RL"),
        "rule": REACH_RULE,
        "counts_by": "поля прибора (stop_reason, hit_limit, metrics.looped, "
                     "metrics.unclosed_think); инструмент их не переизмеряет",
        "saturation_limit": SATURATION_LIMIT,
        "reports": usable,
        "refused": refused,
    }
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    for r in usable:
        for tag, st in r["states"].items():
            if "n" not in st:
                continue
            print(f"{Path(r['report']).name}/{tag}: n={st['n']} "
                  f"дошли до ответа без петель {st['reached_answer_no_loop']} "
                  f"({st['reached_answer_no_loop_share']:.4f}), усечено "
                  f"{st['truncated_share']:.4f}, "
                  f"сатурация={st['saturation']['saturated']}")
    if refused:
        print(f"отказ: {len(refused)} отчёт(ов) не прочитаны — это отказ входа, а не "
              "результат: ряд неполон, и молчаливое «ok» выдало бы неполное за "
              "полное", file=sys.stderr)
        for r in refused:
            print(f"  отказ: {r['report']}: {r['why']}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
