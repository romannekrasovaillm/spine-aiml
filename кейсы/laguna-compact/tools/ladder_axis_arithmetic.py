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

**Счёт рук — две на модель (или на семейство), и это объявлено явно (дельта S3bk).**
Ось сравнивает **парно**: RL-рука против SFT-базы того же чекпойнта того же
семейства, поэтому цена волны считается по **двум** рукам на модель, а не по одной
(`arms_rule`, `arms_per_model`). Прежняя редакция считала руки «по семейству» (три
на волну В-1) и давала 36 ч вместо 72 ч — не потому, что арифметика была иной, а
потому, что счёт был другой. Прежняя редакция сохранена архивом
(`evidence/ladder-axis-arithmetic-2026-09-22.json`), содержимое не менялось
(ADR-023 п.9); `supersedes` называет файл, его sha256 и причину. **Порог и модель
дисперсии перевыпуск не трогает**: `p = 0.5`, формулы SE и порог ≈5,0 п.п. в
перевыпуске совпадают с прежними численно. Цена волн В-2/В-3/В-5 здесь **не
считается**: у них рука дороже калибровочной (0.5B), и единственный носитель этого
посчёта — `LADDER-FULL-PLAN.md` §5.1а (два носителя одного числа расходятся,
ADR-023 п.10).

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

# ── Счёт рук (Решение 3 архитектора, дельта S3bk) ────────────────────────────
# Рука — один прогон eval. Ось сравнивает ПАРНО (RL-рука против SFT-базы того же
# чекпойнта того же семейства, ADR-055 п.1), поэтому рук на модель ДВЕ, а не одна.
ARMS_PER_MODEL = 2
ARMS_RULE = ("число рук волны = две на модель: RL-рука и SFT-база одного семейства "
             "(ADR-055 п.1 — сравнение парное)")
# Состав волн — факт решения (ADR-056 п.1, LADDER-FULL-PLAN §5.1а), а не счёт прибора:
# В-1 — три семейства класса ≤1B (0,5B из В-0 + 0,6B + 0,8B); В-2 — два размера
# внутри Qwen2.5 (1,5B, 3B); В-3 — верхний размер (7B); В-5 — 5 остаточных моделей.
# В-4 отдельно: его руки — RL-руки двух дополнительных сидов над ОБЩЕЙ SFT-базой,
# уже посчитанной в В-1, поэтому «две руки на модель» его не удваивает.
WAVE_MODELS = {
    "В-1": {"models": 3, "unit": "семейства класса ≤1B", "note": "0,5B переоценивается внутри В-1: три семейства — один порог (иначе «≥2 из 3» вырождается в «2 из 2», §5.3)"},
    "В-2": {"models": 2, "unit": "размеры внутри Qwen2.5 (1,5B, 3B)", "note": ""},
    "В-3": {"models": 1, "unit": "верхний размер (7B)", "note": ""},
    "В-5": {"models": 5, "unit": "остаточные модели состава", "note": "Qwen3-1.7B в §2.3 не посчитан — цена 5 моделей из 6"},
}
WAVE_4_ARMS_OVER_WAVE_1 = 4  # RL-руки двух доп. сидов на двух опорных моделях

# Прежняя редакция носителя: сохранена архивом, содержимое не менялось (ADR-023 п.9).
SUPERSEDES = {
    "file": "evidence/ladder-axis-arithmetic-2026-09-22.json",
    "sha256": "7d41c67148782f812cda26c49e2a69f87af8509a82d4b318c10bdc50f3a04355",
    "reason": ("прежняя редакция считала руки волны В-1 «по семейству» (три) и несла цену eval 36 ч; "
               "объявленный счёт — две руки на модель (RL-рука и SFT-база одного семейства), то есть "
               "6 рук и 72 ч. Содержимое прежней редакции не изменялось (ADR-023 п.9). Порог ≈5,0 п.п. "
               "и модель дисперсии перевыпуском НЕ затронуты: p, формулы SE, se_table, required_m и "
               "reproduction_criterion совпадают с прежними численно — изменились цена и число рук"),
}


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
        "stage": "S3bk",
        "editorial_history": ("первая редакция — S3bg (2026-09-22; архив evidence/ladder-axis-arithmetic-2026-09-22.json); "
                              "перевыпуск S3bk (Решение 3 архитектора): счёт рук волны назван явно, цена eval "
                              "пересчитана с двух рук на модель; порог и модель дисперсии не менялись"),
        "supersedes": SUPERSEDES,
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
    arm_hours_n6 = round(avail * 6 * cost_per_task_attempt_s / 3600, 1) if cost_per_task_attempt_s else None
    wave_arms = {w: WAVE_MODELS[w]["models"] * ARMS_PER_MODEL for w in WAVE_MODELS}
    out["eval_cost"] = {
        "calibration": "ADR-013 п.1: ≈2 ч на 192 задачи (n=1) вместе с eval_sft — верхняя граница",
        "seconds_per_task_attempt_upper": round(cost_per_task_attempt_s, 1) if cost_per_task_attempt_s else None,
        "arms_rule": ARMS_RULE,
        "arms_per_model": ARMS_PER_MODEL,
        "wave_arms": [
            {"wave": w, "models": WAVE_MODELS[w]["models"], "unit": WAVE_MODELS[w]["unit"],
             "arms": wave_arms[w], "note": WAVE_MODELS[w]["note"]}
            for w in ("В-1", "В-2", "В-3", "В-5")
        ] + [{"wave": "В-4", "models": 2, "unit": "дополнительные сиды двух опорных ≤1B",
              "arms": WAVE_4_ARMS_OVER_WAVE_1,
              "note": ("сверх В-1: это только RL-руки доп. сидов, SFT-база у трёх сидов общая "
                       "(форк, ADR-056 п.2) и уже посчитана в В-1 — «две руки на модель» В-4 не удваивает")}],
        "per_arm_hours": [
            {"m": m, "n": n, "hours_upper": round(m * n * cost_per_task_attempt_s / 3600, 1)}
            for m, n in ((192, 1), (192, 2), (192, 3), (192, 6), (400, 3), (800, 3))
        ] if cost_per_task_attempt_s else [],
        "wave_eval_cost_calibration_model": {
            "wave": "В-1",
            "arms": wave_arms.get("В-1"),
            "hours_per_arm": arm_hours_n6,
            "hours": round(wave_arms.get("В-1", 0) * arm_hours_n6, 1) if arm_hours_n6 else None,
            "days": round(wave_arms.get("В-1", 0) * arm_hours_n6 / 24, 2) if arm_hours_n6 else None,
            "scope": ("точная цена только для руки класса 0,5B (калибровка здесь). Для моделей крупнее "
                      "рука дороже, поэтому это НИЖНЯЯ граница. Цены волн В-2/В-3/В-5 здесь НЕ считаются: "
                      "их единственный носитель — LADDER-FULL-PLAN.md §5.1а (два носителя одного числа "
                      "расходятся, ADR-023 п.10)"),
        },
        "note": ("цена линейна по m·n; число рук волны В-1 — шесть (три семейства × две руки). "
                 "Поправка дельты S3bk: прежде стояло «три (по семейству)» и цена 36 ч — это был счёт "
                 "одной руки на семейство, а не арифметическая разница"),
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
          f"Счёт рук: **{out['eval_cost']['arms_rule']}** — `arms_per_model = "
          f"{out['eval_cost']['arms_per_model']}`.", "",
          "| m | n | часов на руку (верхняя граница) |", "|---|---|---|"]
    for r in out["eval_cost"]["per_arm_hours"]:
        L.append(f"| {r['m']} | {r['n']} | {r['hours_upper']} |")
    L += ["", "| Волна | Моделей | Рук | Примечание |", "|---|---|---|---|"]
    for r in out["eval_cost"]["wave_arms"]:
        L.append(f"| {r['wave']} | {r['models']} ({r['unit']}) | **{r['arms']}** | {r['note']} |")
    wc = out["eval_cost"]["wave_eval_cost_calibration_model"]
    L += ["", f"Волна В-1 при калибровочной руке (0,5B, n=6): {wc['arms']} × {wc['hours_per_arm']} ч = "
              f"**{wc['hours']} ч = {wc['days']} сут**. {wc['scope']}.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
