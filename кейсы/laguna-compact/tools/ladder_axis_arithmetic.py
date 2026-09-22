#!/usr/bin/env python3
"""ladder_axis_arithmetic.py — арифметика оси лесенки (Н-6, LADDER-FULL-PLAN §6).

**Зачем инструмент, а не текст.** §6 плана называет, чего в оси лесенки нет:
метрики сравнения с явным `n`, порога различимости и критерия воспроизведения.
Их объявляет архитектор (ADR-052 п.2) — но объявлять он обязан **на числах**.
Этот прибор даёт числа: SE сравнения как функция числа задач `m` и попыток `n`,
минимально различимую разницу, требуемое `m` под заявленный эффект 5–10 % и
цену в задачах/попытках, которых нет. Решение он не принимает: числа не ADR.

**Модель.** Задача даёт исход в [0,1] (verifier `multi_slug_match` — 0/1;
судья — балл). Для любой такой величины `Var ≤ 0.25` (максимум при p = 0.5),
поэтому `p = 0.5` — **не допущение о данных, а верхняя граница** их разброса:
SE, посчитанный при p = 0.5, не занижен ни для какой [0,1]-метрики.

* SE одной руки (m задач × n попыток):        `sqrt(p(1-p)/(m·n))`
* SE **разности** двух независимых рук:        `sqrt(2·p(1-p)/(m·n))` = √2 × SE руки
* внутризадачная парная разность даёт не больше (ковариация только уменьшает
  дисперсию разности), поэтому независимый вариант — консервативный.

**Порогов три, и они разные.** 1 SE — унаследованный порог кейса (AD-1 «≥1 std»,
`S4-PROTOCOL.md` §3: «разница < SE неразличима»), но это СЛАБЫЙ порог: при нуле
он срабатывает с вероятностью ≈31.7 %, а мощность 80 % требует эффекта 1.84 SE.
95 % (`z = 1.96`) — обычная сравнительная заявка. `z = 2.394` — тот же 95 %, но
с поправкой Бонферрони на **три** сравнения (три семейства в волне В-1).

Запуск: `python3 tools/ladder_axis_arithmetic.py --json evidence/ladder-axis-arithmetic.json`
Ничего не пишет на стенд, GPU не трогает, наборы не меняет (AD-7): читает только
счётчики существующих наборов.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone

CASE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Число сравнений в волне В-1 (три семейства, каждое против своего SFT-базлайна).
COMPARISONS_WAVE1 = 3
Z_95 = 1.959964
Z_95_BONF3 = 2.394003  # двухсторонний квантиль для alpha = 0.05/3


def phi(x: float) -> float:
    """Функция стандартного нормального распределения."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def z_for_power(power: float, z_alpha: float) -> float:
    """Эффект в единицах SE, нужный для мощности `power` при пороге z_alpha."""
    return z_alpha + math.sqrt(2.0) * _erfinv(2.0 * power - 1.0)


def _erfinv(y: float) -> float:
    """Обратная функция ошибок (метод Уинкера–Ньютона, без scipy)."""
    if y >= 1.0:
        return 8.0
    if y <= -1.0:
        return -8.0
    x = 0.0
    for _ in range(3):
        err = math.erf(x) - y
        x -= err / (2.0 / math.sqrt(math.pi) * math.exp(-x * x) - x * err)
    return x


def se_arm(m: int, n: int, p: float = 0.5) -> float:
    return math.sqrt(p * (1.0 - p) / (m * n))


def se_diff(m: int, n: int, p: float = 0.5) -> float:
    return math.sqrt(2.0 * p * (1.0 - p) / (m * n))


def m_required(effect: float, n: int, z: float, p: float = 0.5) -> int:
    """Минимальное m, при котором z·SE_diff ≤ effect."""
    raw = 2.0 * p * (1.0 - p) * (z / effect) ** 2 / n
    return int(math.ceil(raw - 1e-9))


def p_at_least_k_of_3(p_family: float, k: int) -> float:
    """P(≥k из 3) при независимых семьях, вероятность события в семье p_family."""
    from math import comb
    return sum(comb(3, i) * p_family ** i * (1 - p_family) ** (3 - i)
               for i in range(k, 4))


def se_table(ms, ns, p=0.5):
    rows = []
    for m in ms:
        for n in ns:
            d = se_diff(m, n, p)
            rows.append({
                "m": m, "n": n,
                "attempts": m * n,
                "se_arm_pct": round(100.0 * se_arm(m, n, p), 3),
                "se_diff_pct": round(100.0 * d, 3),
                "mdd_1se_pct": round(100.0 * d, 3),
                "mdd_95_pct": round(100.0 * Z_95 * d, 3),
                "mdd_95_bonf3_pct": round(100.0 * Z_95_BONF3 * d, 3),
                "mdd_80power_95_pct": round(100.0 * z_for_power(0.80, Z_95) * d, 3),
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="куда положить JSON-отчёт")
    ap.add_argument("--markdown", help="куда положить markdown-таблицы")
    args = ap.parse_args()

    ms = [192, 400, 800, 1600]
    ns = [1, 3, 5]
    effects = [0.05, 0.10]
    ev = os.path.join(CASE_ROOT, "datasets", "eval_ood_clean.jsonl")
    available_tasks = sum(1 for _ in open(ev, encoding="utf-8")) if os.path.exists(ev) else None

    out = {
        "tool": "tools/ladder_axis_arithmetic.py",
        "stage": "S3bg",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": "числа для объявления оси лесенки (ADR-052 п.2 / LADDER-FULL-PLAN §6)",
        "model": {
            "metric_space": "[0,1] на задачу (verifier multi_slug_match 0/1; судья — балл)",
            "p": 0.5,
            "why_p_half": "верхняя граница дисперсии для любой [0,1]-величины: Var ≤ 0.25",
            "se_arm_formula": "sqrt(p(1-p)/(m·n))",
            "se_diff_formula": "sqrt(2·p(1-p)/(m·n)) = sqrt(2) × SE руки",
            "pairing": "внутризадачная парная разность даёт SE не больше; независимый вариант консервативен",
            "thresholds": {
                "1se": {"z": 1.0, "false_positive_rate_two_sided_pct": round(100.0 * 2 * (1 - phi(1.0)), 1),
                        "power_at_1se_effect_pct": round(100.0 * (phi(1.0 - 1.0) + phi(-1.0 - 1.0)), 1),
                        "effect_for_80pct_power_in_se": round(z_for_power(0.80, 1.0), 3),
                        "note": "порог кейса (AD-1 «≥1 std», S4 §3); слабый: 31.7 % при нуле"},
                "95": {"z": Z_95, "note": "обычная сравнительная заявка"},
                "95_bonferroni3": {"z": Z_95_BONF3, "comparisons": COMPARISONS_WAVE1,
                                   "note": "три семейства волны В-1 → alpha 0.05/3"},
                "80power_at_95": {"effect_in_se": round(z_for_power(0.80, Z_95), 3),
                                  "note": "во сколько SE должен укладываться эффект, чтобы заявлять его с мощностью 80 %"},
            },
        },
        "se_table": se_table(ms, ns),
        "required_m": [],
        "available": {
            "task_set": {"path": "datasets/eval_ood_clean.jsonl",
                         "sha256": "f94fb8556cabf3d5386fec1569dfd5e4e796b15c97dfc935bf7bd8f9879bb1d6",
                         "tasks": available_tasks,
                         "task_type": "ood_pair ×192",
                         "verifier": "multi_slug_match",
                         "role": "единственный набор задач, на котором кейс считает judge_mean/slug_accuracy"},
            "ppl_sets": {"general_eval_v3": 200, "general_eval_k2": 200,
                         "domain_eval_v3": 200,
                         "note": "документы для PPL, не задачи: в арифметике оси не участвуют"},
            "other_task_files": {"eval_ood.jsonl": 192, "eval_ood_oxalpha.jsonl": "102/3000 — выведен (AD-7)",
                                 "rl_tasks_revpool_v2.jsonl": 9162},
            "rl_pool_note": ("9162 задачи пула v2 — обучающий пул RL-стадии; как eval-набор не берутся "
                             "без отдельного решения (пересечение с обучением)"),
        },
        "reproduction_criterion": {
            "plan_variant": "\"направление повторилось в 2 из 3 семейств\" (EXPERIMENT_PLAN §6.1)",
            "under_null_pct": round(100.0 * p_at_least_k_of_3(0.5, 2), 1),
            "three_of_three_under_null_pct": round(100.0 * p_at_least_k_of_3(0.5, 3), 1),
            "note": ("2 из 3 по ЗНАКУ при нуле — 50 %: критерий не различает. Варианты ниже "
                     "требуют покомпонентной значимости (95 %) в семье, а не только знака"),
            "alternative_per_family_95": {
                "k2_of_3_pct": round(100.0 * p_at_least_k_of_3(0.05, 2), 3),
                "k3_of_3_pct": round(100.0 * p_at_least_k_of_3(0.05, 3), 3),
            },
        },
        "limits": [
            "Это арифметика, а не ось: метрику, n и порог объявляет архитектор (ADR-052 п.2).",
            "p=0.5 — верхняя граница разброса; на фактических данных SE будет не больше (и не может быть больше).",
            "SE не описывает систематику: смена площадки (4080 ↔ GB10), смена режима декодирования и утечка набора в обучение дают смещение, которое SE не видит.",
            "Числа требуемого m не учитывают отказы/таймауты задач: неответившая задача — это исход, а не пропуск.",
        ],
    }

    for e in effects:
        out["required_m"].append({
            "effect_pct": round(100 * e, 2),
            "rows": [
                {
                    "n": n,
                    "m_1se": m_required(e, n, 1.0),
                    "m_95": m_required(e, n, Z_95),
                    "m_95_bonf3": m_required(e, n, Z_95_BONF3),
                    "m_80power_95": m_required(e, n, z_for_power(0.80, Z_95)),
                } for n in ns
            ],
            "note": ("эффект меньше заявленного порога не заявляется — в этом смысл порога, "
                     "а не в том, чтобы его обойти"),
        })

    avail = available_tasks or 0
    out["shortfall"] = {
        "available_tasks": available_tasks,
        "for_effect_5pct_95bonf3": [
            {"n": n, "m_needed": m_required(0.05, n, Z_95_BONF3),
             "shortfall_tasks": max(0, m_required(0.05, n, Z_95_BONF3) - avail)} for n in ns
        ],
    }

    rec = {
        "config_a_existing_set": {
            "m": avail, "n": 6,
            "mdd_95_bonf3_pct": round(100 * Z_95_BONF3 * se_diff(avail, 6), 2),
            "price": "eval ×6 на руку; набор не трогается (AD-7) — только новые прогоны",
        },
        "config_b_new_set": {
            "m": 400, "n": 3,
            "mdd_95_bonf3_pct": round(100 * Z_95_BONF3 * se_diff(400, 3), 2),
            "price": "+208 задач нового набора с аудитом чистоты (ADR-025) и решением AD-7; eval ×3",
        },
        "config_c_cheap_10pct": {
            "m": avail, "n": 2,
            "mdd_95_bonf3_pct": round(100 * Z_95_BONF3 * se_diff(avail, 2), 2),
            "price": "eval ×2 на руку; заявляются только различия ≳9 %",
        },
        "note": ("варианты — не решение: архитектор выбирает между «дороже мерить» и «слабее заявлять»"),
    }
    out["recommendation_numbers"] = rec

    # Цена eval: калибровка по ADR-013 п.1 (192 задачи + eval_sft ≈ 2 ч, n=1).
    cost_per_task_attempt_s = 2 * 3600 / (avail * 1) if avail else None
    out["eval_cost"] = {
        "calibration": "ADR-013 п.1: ≈2 ч на 192 задачи (n=1) вместе с eval_sft — верхняя граница",
        "seconds_per_task_attempt_upper": round(cost_per_task_attempt_s, 1) if cost_per_task_attempt_s else None,
        "per_arm_hours": [
            {"m": m, "n": n, "hours_upper": round(m * n * cost_per_task_attempt_s / 3600, 1)}
            for m, n in ((192, 1), (192, 2), (192, 3), (192, 6), (400, 3), (800, 3))
        ] if cost_per_task_attempt_s else [],
        "note": "цена линейна по m·n; число рук волны В-1 — три (по семейству)",
    }

    text = json.dumps(out, ensure_ascii=False, indent=1)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"записано: {args.json}")
    else:
        print(text)

    if args.markdown:
        md = render_markdown(out)
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"записано: {args.markdown}")
    return 0


def render_markdown(out) -> str:
    L = [f"<!-- сгенерировано {out['tool']} {out['generated_at']} -->",
         f"# Арифметика оси лесенки (Н-6, {date.today().isoformat()})", "",
         "Модель: исход задачи в [0,1], `p = 0.5` — верхняя граница разброса; "
         "SE разности двух независимых рук = √2 × SE руки.", "",
         "## Таблица 1. SE и минимально различимая разница (порог 95 %, в скобках — с поправкой на 3 сравнения)",
         "",
         "| m задач | n попыток | попыток всего | SE руки, % | SE разности, % | MDD 1 SE, % | MDD 95 %, % | MDD 95 % Бонф.(3), % |",
         "|---|---|---|---|---|---|---|---|"]
    for r in out["se_table"]:
        L.append(f"| {r['m']} | {r['n']} | {r['attempts']} | {r['se_arm_pct']} | "
                 f"{r['se_diff_pct']} | {r['mdd_1se_pct']} | {r['mdd_95_pct']} | "
                 f"{r['mdd_95_bonf3_pct']} |")
    L += ["", "## Таблица 2. Сколько задач нужно под заявленный эффект", ""]
    for blk in out["required_m"]:
        L += [f"**Эффект {blk['effect_pct']} п.п.**", "",
              "| n попыток | m при пороге 1 SE | m при 95 % | m при 95 % + Бонф.(3) | m при 95 % и мощности 80 % |",
              "|---|---|---|---|---|"]
        for r in blk["rows"]:
            L.append(f"| {r['n']} | {r['m_1se']} | {r['m_95']} | {r['m_95_bonf3']} | "
                     f"{r['m_80power_95']} |")
        L.append("")
    av = out["available"]
    L += ["## Таблица 3. Что есть фактически", "",
          "| Набор | Задач | Роль |", "|---|---|---|",
          f"| `{av['task_set']['path']}` | **{av['task_set']['tasks']}** | единственный набор задач: judge_mean/slug_accuracy |",
          f"| `general_eval_v3.txt` | 200 док. | K1, PPL — не задачи |",
          f"| `general_eval_k2.txt` | 200 док. | K2, PPL — не задачи |",
          f"| `domain_eval_v3.txt` | 200 док. | домен, PPL — не задачи |",
          f"| `rl_tasks_revpool_v2.jsonl` | 9162 | обучающий пул RL, не eval |", ""]
    L += ["## Таблица 4. Нехватка задач под эффект 5 п.п. (95 % + Бонф. на 3 сравнения)", "",
          "| n попыток | нужно m | есть | не хватает |", "|---|---|---|---|"]
    for r in out["shortfall"]["for_effect_5pct_95bonf3"]:
        L.append(f"| {r['n']} | {r['m_needed']} | {out['shortfall']['available_tasks']} | "
                 f"**{r['shortfall_tasks']}** |")
    L += ["", "## Критерий воспроизведения", "",
          f"«2 из 3 семейств» по знаку при нуле даёт **{out['reproduction_criterion']['under_null_pct']} %** — "
          f"критерий не различает. «3 из 3» по знаку — {out['reproduction_criterion']['three_of_three_under_null_pct']} %. "
          "Если семьи обязаны пройти проверку значимости (95 %) порознь, то "
          f"«2 из 3» = {out['reproduction_criterion']['alternative_per_family_95']['k2_of_3_pct']} %, "
          f"«3 из 3» = {out['reproduction_criterion']['alternative_per_family_95']['k3_of_3_pct']} %.", ""]
    L += ["## Цена eval (калибровка ADR-013 п.1)", "",
          "| m | n | часов на руку (верхняя граница) |", "|---|---|---|"]
    for r in out["eval_cost"]["per_arm_hours"]:
        L.append(f"| {r['m']} | {r['n']} | {r['hours_upper']} |")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
