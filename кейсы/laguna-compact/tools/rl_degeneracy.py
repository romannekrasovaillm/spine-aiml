#!/usr/bin/env python3
"""Критерий вырожденной награды RL (ADR-017): класс вырождения, порог, действие.

Зачем отдельный прибор. ADR-017 принят 14.09.2026 и объявляет критерий, остановку
и заготовку плана Б — но **в коде RL-контура критерия не было**: цепочка пилота
(`tools/pilot_chain.sh`) контролировала энтропию, NVRM, память и молчание, а
награда могла быть мертва при честно отработанных 500 шагах. Разведка
(`tools/run_rl_probe.py`) считала `zero_reward` **фоном** (`parse_rollouts`), но
стоп-условием это не становилось. Прибор закрывает ровно этот разрыв: измеряет
награду и **называет класс** вырождения, чтобы исход «RL не мог учиться» не
маскировался под «RL не помог».

Границы, объявленные заранее (без них прибор доопределялся бы по месту):

* **Окно — первые 50 шагов** (ADR-017 п.1). До его набора вердикт `watch`, а не
  «здорово»: «ещё не смотрели» и «посмотрели и чисто» — разные утверждения.
* **Класс вырождения — не одно число, а четыре** (см. `CLASSES`). У каждого —
  собственный порог и **названный источник**: порог из ADR или из уже принятого
  решения, а не «похожее число». Порог, взятого из карточки-гипотезы (а не из
  ADR), прибор называет и понижает до `warn` — гипотеза не даёт права на стоп.
* **Действие различается: `stop` против `warn`.** Стоп ставит только то, что
  предписано ADR-017 п.2 (остановка, а не смена алгоритма на ходу). Рост
  `clip_frac` — критерий точки решения S4 (§3 п.2), а не стоп стадии: он ведёт к
  разбору, а не к обрыву прогона.
* **Два измерения, а не одно.** Живая точка (`--points-file`, строки пайплайна) и
  итоговая (`--run-dir`, артефакты стадии) считаются **одной** функцией
  `evaluate`, а различаются только входом: две реализации одного критерия
  разошлись бы на первой правке.

Чего прибор НЕ делает: не меняет алгоритм кредита (ADR-017 п.2), не пересчитывает
задним числом прошлые прогоны (ADR-017 п.4), не правит данные и пул (AD-7), не
трогает пайплайн (`laguna_pipeline_v8.py` — симлинк на живой файл контура; стадия
SFT исполняется из копии, правка базы запрещена ADR-016).

Источники порогов (поимённо, чтобы число можно было проверить, а не поверить):

| Класс | Порог | Источник |
|---|---|---|
| `dead_reward` | `pass_rate = 0` **и** `reward_mean ≤ 0.05` | ADR-017 п.1 |
| `zero_reward_dominated` | доля `zero_reward ≥ 0.90` | ADR-017 п.1 |
| `no_group_variance` | `adv≠0 = 0` на **всех** наблюдениях окна | ADR-049 п.6 (структурно: нулевая дисперсия = нулевой градиент) |
| `template_collapse` | `max4gram_rep ≥ LOOP_MAX4GRAM_REP` (8) | `tools/probe_language_split.py:191` — порог приборов S3ai/S3aj/S3ar; карточка `degenerate-template-collapse-mi` |
| `clip_frac_high` | `clip_frac ≥ 0.2` | спайн AD-1; `docs/specs/S4-PROTOCOL.md` §3 п.2 |

Коды возврата::

    0 — вырождения не найдено (классы `stop` не сработали); `warn`-классы печатаются
    1 — вырождение: сработал класс действия `stop` (ADR-017 п.2 — остановка)
    2 — NOT-VERIFIED: нет входа/вход неразбираем — «не зелёный», а не «чисто»

Запуск::

    python3 tools/rl_degeneracy.py --points-file /tmp/points.jsonl --json
    python3 tools/rl_degeneracy.py --run-dir runs/pilot-compact-s42-<ts> --json
    python3 tools/rl_degeneracy.py --run-dir <run> --window-steps 50 --json
    python3 tools/rl_degeneracy.py --self-test          # контроль синтетикой
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXIT_OK, EXIT_DEGENERATE, EXIT_NOT_VERIFIED = 0, 1, 2

#: Окно критерия. Источник — ADR-017 п.1 («по первым 50 шагам RL»); п.5 того же
#: решения объясняет, зачем окно нужно: «чтобы вырождение было видно на 50-м шаге,
#: а не на 500-м». Число не подбиралось здесь.
WINDOW_STEPS = 50

#: Пороги классов — с источником в тексте, чтобы «откуда число» не искалось потом.
PASS_RATE_ZERO = 0.0
REWARD_MEAN_DEAD = 0.05
ZERO_REWARD_DOMINATED = 0.90
CLIP_FRAC_HIGH = 0.2

#: Порог петлевости — **импортом** из прибора, а не копией: тот же порог, по
#: которому S3ai/S3aj/S3ar считали петли в генерациях и данных (ADR-039/ADR-042).
#: Копия разошлась бы с прибором на первой его правке — и «зациклилось» в двух
#: местах значило бы разное.
try:
    from probe_language_split import LOOP_MAX4GRAM_REP as LOOP_MAX4GRAM_REP_IMPORT
    LOOP_THRESHOLD_SOURCE = "tools/probe_language_split.py (импорт константы)"
except Exception:  # pragma: no cover — прибор на месте; ветвь на случай урезанной копии
    LOOP_MAX4GRAM_REP_IMPORT = 8
    LOOP_THRESHOLD_SOURCE = ("tools/probe_language_split.py недоступен — порог взят "
                             "числом 8 (тот же, что объявлен прибором); расхождение "
                             "названо, а не скрыто")


#: Строка метрик RL-шага, которую печатает пайплайн каждые 10 шагов
#: (``laguna_pipeline_v8.py:1508``). Разбор — **свой**, а не импорт из
#: ``tools/run_rl_probe.py``: у того разбора шире назначение (цена шага, разбивка
#: фаз), и правка там меняла бы здесь критерий награды молча. Три поля, которых
#: прибору не хватает в журнале стадии, берутся прямо отсюда: `reward`, `pass`,
#: `clip`, `adv≠0`.
RL_STEP_RE = re.compile(
    r"RL step (\d+)/(\d+) \| reward=(-?[\d.]+) \| pass=(\d+)% \|")
CLIP_RE = re.compile(r"clip=(-?[\d.]+)")
ADV_RE = re.compile(r"adv≠0=(\d+)%")


def extract_step_lines(text: str) -> list[dict]:
    """Строки лога пайплайна → наблюдения. Непонятая строка пропускается молча.

    Пропуск здесь — не «тихая потеря»: число разобранных строк прибор печатает
    рядом с максимальным шагом, и по нему видно, что лог прочитан не весь.
    """
    out: list[dict] = []
    for line in text.splitlines():
        m = RL_STEP_RE.search(line)
        if not m:
            continue
        clip = CLIP_RE.search(line)
        adv = ADV_RE.search(line)
        out.append({
            "step": int(m.group(1)),
            "reward_mean": float(m.group(3)),
            "pass_rate": int(m.group(4)) / 100.0,
            "clip_frac": float(clip.group(1)) if clip else None,
            "adv_nonzero": int(adv.group(1)) / 100.0 if adv else None,
        })
    return out


# ─── оценка: одна функция на оба входа ────────────────────────────────────────

def _f(value) -> float | None:
    """Число или None. ``None`` — «величина не наблюдалась», и это не 0."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def evaluate(points: list[dict], *, window_steps: int = WINDOW_STEPS,
             final: bool = False) -> dict:
    """Вердикт по наблюдениям. ``points`` — по одному словарю на наблюдение шага.

    Обязательное поле — ``step``. Остальные величины необязательны: класс, для
    которого величина не наблюдалась, **не оценивается** и называется в отчёте
    причиной (`not_evaluated`) — «нет данных» и «данные чистые» не одно и то же.

    ``final=True`` — стадия закончилась (или наблюдений больше не будет): тогда
    неполное окно оценивается, но помечается ``window_complete=False``, а вывод по
    нему несёт оговорку. Так стоп-вердикт по первым 20 шагам не выдаётся за
    критерий ADR-017 (это было бы «правило, сработавшее не по своему условию»).
    """
    window = [p for p in points if (_f(p.get("step")) or 0) <= window_steps]
    window.sort(key=lambda p: _f(p.get("step")) or 0)
    steps_seen = [_f(p.get("step")) for p in window]
    window_complete = bool(window) and max(s for s in steps_seen if s is not None) >= window_steps

    classes: list[dict] = []
    not_evaluated: list[dict] = []

    def obs(field: str) -> list[float]:
        return [v for v in (_f(p.get(field)) for p in window) if v is not None]

    def add(key: str, title: str, action: str, source: str, hit: bool,
            evidence: dict, reason: str | None = None) -> None:
        classes.append({"class": key, "title": title, "action": action,
                        "threshold_source": source, "triggered": bool(hit),
                        "evidence": evidence, "reason": reason})

    # ── класс 1: мёртвая награда (ADR-017 п.1, первое условие) ────────────────
    pass_rates, reward_means = obs("pass_rate"), obs("reward_mean")
    if pass_rates and reward_means:
        pr_mean, rw_mean = _mean(pass_rates), _mean(reward_means)
        hit = pr_mean == PASS_RATE_ZERO and rw_mean <= REWARD_MEAN_DEAD
        add("dead_reward", "награда мертва: задачи не решаются и награда не начисляется",
            "stop", "ADR-017 п.1 (pass_rate = 0 и reward_mean ≤ 0.05)", hit,
            {"pass_rate_mean": pr_mean, "reward_mean": rw_mean,
             "observations": len(pass_rates),
             "pass_rate_zero_at_all_observations": all(v == PASS_RATE_ZERO for v in pass_rates)},
            None if hit else "порог не достигнут")
    else:
        not_evaluated.append({"class": "dead_reward", "field": "pass_rate/reward_mean",
                              "why": "в наблюдениях нет pass_rate или reward_mean — "
                                     "строки пайплайна «RL step» не прочитаны"})

    # ── класс 2: доля нулевой награды (ADR-017 п.1, второе условие) ───────────
    zero_shares = obs("zero_reward_share")
    if zero_shares:
        z = _mean(zero_shares)
        hit = z >= ZERO_REWARD_DOMINATED
        add("zero_reward_dominated",
            "доля траекторий с нулевой наградой ≥ 90 % (сигнала почти нет)",
            "stop", "ADR-017 п.1 (zero_reward ≥ 90 %)", hit,
            {"zero_reward_share_mean": z, "observations": len(zero_shares),
             "measured_from": "доля траекторий с reward == 0.0"},
            None if hit else "порог не достигнут")
    else:
        not_evaluated.append({"class": "zero_reward_dominated", "field": "zero_reward_share",
                              "why": "живая точка замера его не даёт (в строке пайплайна "
                                     "доли нулевой награды нет); величина считается по "
                                     "rollouts_log.jsonl — точка замера «итог стадии»"})

    # ── класс 3: нулевая дисперсия награды по группе ─────────────────────────
    #    Структурный признак, без порога: `adv≠0 = 0` означает, что преимущество
    #    обнулилось на **всех** траекториях группы — учиться не на чем. Печатает
    #    его сам пайплайн (laguna_pipeline_v8.py:1511), то есть величина не
    #    вводится прибором.
    advs = obs("adv_nonzero")
    if advs:
        hit = all(v == 0.0 for v in advs)
        add("no_group_variance",
            "нулевая дисперсия награды по группе (все преимущества обнулены)",
            "stop", "ADR-049 п.6 (все rollout с нулём) + adv≠0 в строке пайплайна", hit,
            {"adv_nonzero_mean": _mean(advs), "observations": len(advs),
             "zero_at_all_observations": all(v == 0.0 for v in advs)},
            None if hit else "дисперсия в группе наблюдалась")
    else:
        not_evaluated.append({"class": "no_group_variance", "field": "adv_nonzero",
                              "why": "в наблюдениях нет adv≠0"})

    # ── класс 4: схлопывание в один шаблон (карточка, не ADR → warn) ──────────
    reps = obs("max4gram_rep")
    if reps:
        hit = max(reps) >= LOOP_MAX4GRAM_REP_IMPORT
        add("template_collapse",
            "схлопывание в один шаблон: повтор 4-грамм выше порога петлевости",
            "warn", f"{LOOP_THRESHOLD_SOURCE}: LOOP_MAX4GRAM_REP="
                    f"{LOOP_MAX4GRAM_REP_IMPORT}", hit,
            {"max4gram_rep_max": max(reps), "observations": len(reps),
             "threshold": LOOP_MAX4GRAM_REP_IMPORT,
             "action_note": ("предупреждение, а не стоп: порог взят из прибора кейса, "
                             "но карточка degenerate-template-collapse-mi остаётся "
                             "latent — права на остановку стадии ADR не даёт")},
            None if hit else "повторов выше порога не зафиксировано")
    else:
        not_evaluated.append({"class": "template_collapse", "field": "max4gram_rep",
                              "why": "тексты роллаутов не читались (нужен rollouts_log.jsonl "
                                     "— точка замера «итог стадии»)"})

    # ── класс 5: clip_frac (критерий точки решения, не стоп) ─────────────────
    clips = obs("clip_frac")
    if clips:
        c = _mean(clips)
        hit = c >= CLIP_FRAC_HIGH
        add("clip_frac_high", "clip_frac ≥ 0.2 — анти-метрика дегенерации оси",
            "warn", "спайн AD-1; docs/specs/S4-PROTOCOL.md §3 п.2", hit,
            {"clip_frac_mean": c, "observations": len(clips),
             "threshold": CLIP_FRAC_HIGH,
             "action_note": ("предупреждение: это критерий точки решения S4, а не "
                             "стоп-условие стадии (ADR-017 п.2 запрещает менять "
                             "режим на ходу)")},
            None if hit else "ниже порога")
    else:
        not_evaluated.append({"class": "clip_frac_high", "field": "clip_frac",
                              "why": "в наблюдениях нет clip_frac"})

    stop_hits = [c for c in classes if c["triggered"] and c["action"] == "stop"]
    warn_hits = [c for c in classes if c["triggered"] and c["action"] == "warn"]
    #: Вердикт `degenerate_reward` — тот самый маркер, который ADR-017 п.2 требует
    #: поставить результату остановленной стадии.
    verdict = ("degenerate_reward" if stop_hits and (window_complete or final)
               else "watch" if not window_complete and not final
               else "healthy")
    return {
        "criterion": "ADR-017 (вырожденная награда RL)",
        "verdict": verdict,
        "action": ("stop" if verdict == "degenerate_reward" else
                   "warn" if warn_hits else "none"),
        "window_steps": window_steps,
        "window_complete": window_complete,
        "final": final,
        "observations": len(window),
        "steps_observed": [int(s) for s in steps_seen if s is not None],
        "classes": classes,
        "not_evaluated": not_evaluated,
        "warning_classes": [c["class"] for c in warn_hits],
        "stop_classes": [c["class"] for c in stop_hits],
        "limits": (["окно не набрано: вердикт `watch`, а не «здорово» — критерий "
                    "ADR-017 считается по первым 50 шагам"]
                   if not window_complete and not final else [])
                 + ["остановка не даёт ответа на ось AD-1: сравнение RL vs SFT не "
                    "состоится, точка решения получит insufficient_evidence "
                    "(ADR-017, «Отрицательные последствия»)"]
                 + [f"класс {n['class']} не оценён: {n['why']}" for n in not_evaluated],
    }


# ─── входы: живая точка и итог стадии ─────────────────────────────────────────

def points_from_jsonl(path: Path) -> list[dict]:
    """Живые наблюдения (по словарю на строку) — то, что пишет стража стадии."""
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("step") is not None:
            rows.append(obj)
    return rows


def points_from_run(run_dir: Path) -> tuple[list[dict], dict]:
    """Наблюдения из артефактов стадии: журнал RL (`rl_metrics.json`) + траектории.

    Журнал даёт ряд шагов (`rewards`, `pass_rates`, `entropy`, `clip_frac`), но
    **не даёт** ни доли ``zero_reward``, ни ``hit_timeout`` — они считаются по
    ``rollouts_log.jsonl``, где лежит по строке на траекторию. Доля нулевой
    награды — доля траекторий с ``reward == 0.0``: ровно та величина, по которой
    ADR-017 п.5 требует видеть вырождение, и ровно та, что замерена в разведке
    (405 из 800).
    """
    notes: dict = {}
    per_step: dict[int, dict] = {}

    metrics = run_dir / "logs" / "rl_metrics.json"
    if not metrics.is_file():
        metrics = run_dir / "rl_metrics.json"
    if metrics.is_file():
        try:
            data = json.loads(metrics.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            notes["rl_metrics_error"] = f"rl_metrics.json не разбирается: {e}"
            data = {}
        for idx, rw in enumerate(data.get("rewards") or []):
            #: Индекс ряда = номер шага: пайплайн пишет вектор по шагам
            #: (laguna_pipeline_v8.py:1543–1545), отдельного поля «step» в нём нет.
            per_step.setdefault(idx, {"step": idx})["reward_mean"] = _f(rw)
        for field, key in (("pass_rates", "pass_rate"), ("clip_frac", "clip_frac"),
                           ("entropy", "entropy")):
            for idx, v in enumerate(data.get(field) or []):
                per_step.setdefault(idx, {"step": idx})[key] = _f(v)
        notes["rl_metrics"] = {"path": str(metrics), "steps": len(data.get("rewards") or []),
                               "has_zero_reward_share": False,
                               "has_hit_timeout": False,
                               "why": ("журнал стадии несёт rewards/pass_rates/entropy/"
                                       "clip_frac; доли zero_reward и hit_timeout в нём "
                                       "нет — они считаются по rollouts_log.jsonl")}
    else:
        notes["rl_metrics"] = {"path": str(metrics), "present": False,
                               "why": "журнал не найден (пишется в конце стадии)"}

    rollouts = run_dir / "rollouts_log.jsonl"
    if rollouts.is_file():
        rewards_by_step: dict[int, list[float]] = {}
        timeouts = 0
        n_traj = 0
        reps: list[float] = []
        try:
            from probe_control import degenerate_metrics
        except Exception:
            degenerate_metrics = None
        for line in rollouts.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            st = obj.get("step")
            if st is None:
                continue
            st = int(st)
            rw = _f(obj.get("reward"))
            if rw is not None:
                rewards_by_step.setdefault(st, []).append(rw)
            n_traj += 1
            timeouts += 1 if obj.get("hit_timeout") else 0
            if degenerate_metrics is not None and len(reps) < 400:
                m = degenerate_metrics(str(obj.get("text") or ""))
                if m.get("max4gram_rep") is not None:
                    reps.append(float(m["max4gram_rep"]))
        for st, rws in rewards_by_step.items():
            rec = per_step.setdefault(st, {"step": st})
            rec["reward_mean"] = _mean(rws)
            rec["zero_reward_share"] = round(
                sum(1 for r in rws if r == 0.0) / len(rws), 6)
            rec["pass_rate"] = round(sum(1 for r in rws if r >= 1.0) / len(rws), 6)
        if reps:
            overall = max(reps)
            for rec in per_step.values():
                rec.setdefault("max4gram_rep", overall)
        notes["rollouts"] = {"path": str(rollouts), "trajectories": n_traj,
                             "steps": len(rewards_by_step),
                             "hit_timeout": timeouts,
                             "texts_measured_for_template_collapse": len(reps),
                             "why_step_scoped": ("zero_reward_share считается по "
                                                 "траекториям своего шага; "
                                                 "max4gram_rep — максимум по выборке "
                                                 "первых 400 траекторий (метрика "
                                                 "текста, шаг ей не нужен)")}
    else:
        notes["rollouts"] = {"path": str(rollouts), "present": False,
                             "why": "траектории не записаны — доля zero_reward не измерена"}

    return [per_step[k] for k in sorted(per_step)], notes


# ─── CLI ──────────────────────────────────────────────────────────────────────

def render(report: dict, notes: dict | None) -> str:
    lines = [f"критерий: {report['criterion']}",
             f"вердикт:  {report['verdict']} (действие: {report['action']})",
             f"окно:     первые {report['window_steps']} шагов, наблюдений "
             f"{report['observations']}, окно набрано: {report['window_complete']}"]
    for c in report["classes"]:
        mark = "СРАБОТАЛ" if c["triggered"] else "чисто"
        lines.append(f"  [{mark:>9}] {c['class']:<24} действие={c['action']:<5} "
                     f"порог: {c['threshold_source']}")
    for n in report["not_evaluated"]:
        lines.append(f"  [не оценён ] {n['class']:<24} {n['why']}")
    for lim in report["limits"]:
        lines.append(f"  оговорка: {lim}")
    if notes:
        for k, v in notes.items():
            lines.append(f"  вход {k}: {json.dumps(v, ensure_ascii=False)[:220]}")
    return "\n".join(lines)


def self_test() -> int:
    """Контроль синтетикой: здоровый ряд не краснеет, вырожденный — краснеет.

    Это **зуб прибора**, а не замена тестам кейса (`tools/tests/run_tool_tests.sh`):
    здесь проверяется, что обе стороны различимы на одном и том же коде.
    """
    healthy = [{"step": s, "reward_mean": 0.3782, "pass_rate": 0.3887,
                "zero_reward_share": 0.5063, "adv_nonzero": 0.62, "clip_frac": 0.01,
                "max4gram_rep": 3} for s in range(0, 51, 10)]
    dead = [{"step": s, "reward_mean": 0.0, "pass_rate": 0.0,
             "zero_reward_share": 1.0, "adv_nonzero": 0.0, "clip_frac": 0.0,
             "max4gram_rep": 1} for s in range(0, 51, 10)]
    r_ok, r_bad = evaluate(healthy), evaluate(dead)
    checks = [(r_ok["verdict"] == "healthy", f"здоровый ряд → {r_ok['verdict']}"),
              (r_ok["action"] == "none", f"здоровый ряд действие={r_ok['action']}"),
              (r_bad["verdict"] == "degenerate_reward",
               f"вырожденный ряд → {r_bad['verdict']}"),
              ("dead_reward" in r_bad["stop_classes"] and
               "zero_reward_dominated" in r_bad["stop_classes"] and
               "no_group_variance" in r_bad["stop_classes"],
               f"классы вырожденного ряда: {r_bad['stop_classes']}")]
    for ok, what in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    return EXIT_OK if all(ok for ok, _ in checks) else EXIT_DEGENERATE


def extract_mode(points_file: Path, text: str) -> int:
    """Режим стража: строки лога со stdin → jsonl наблюдений, в stdout — макс. шаг.

    Печатается **максимальный шаг** (а не «сколько строк»): стража стадии
    интересует только «окно набрано или нет», и сравнивать надо шаг с окном, а не
    число строк с ним — на шагах, кратных 10, это разные числа.
    """
    obs = extract_step_lines(text)
    if obs:
        with points_file.open("a", encoding="utf-8") as fh:
            for o in obs:
                fh.write(json.dumps(o, ensure_ascii=False, sort_keys=True) + "\n")
    print(max((o["step"] for o in obs), default=-1))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ADR-017: критерий вырожденной награды RL (класс, порог, действие).")
    ap.add_argument("--points-file", help="наблюдения (jsonl, по словарю на шаг)")
    ap.add_argument("--run-dir", help="каталог стадии: logs/rl_metrics.json + rollouts_log.jsonl")
    ap.add_argument("--window-steps", type=int, default=WINDOW_STEPS,
                    help=f"окно критерия в шагах (ADR-017 п.1: {WINDOW_STEPS})")
    ap.add_argument("--final", action="store_true",
                    help="наблюдений больше не будет: неполное окно оценивается с оговоркой")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    ap.add_argument("--self-test", action="store_true", help="контроль синтетикой")
    ap.add_argument("--extract", action="store_true",
                    help="режим стража: строки лога со stdin → jsonl наблюдений "
                         "(нужен --points-file), в stdout — максимальный шаг")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.extract:
        if not args.points_file:
            ap.error("--extract требует --points-file")
        return extract_mode(Path(args.points_file), sys.stdin.read())
    if bool(args.points_file) == bool(args.run_dir):
        ap.error("нужен ровно один вход: --points-file или --run-dir")

    notes = None
    if args.points_file:
        p = Path(args.points_file)
        if not p.is_file():
            print(f"NOT-VERIFIED: нет файла наблюдений {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        points = points_from_jsonl(p)
    else:
        rd = Path(args.run_dir)
        if not rd.is_dir():
            print(f"NOT-VERIFIED: нет каталога стадии {rd}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        points, notes = points_from_run(rd)
    if not points:
        print("NOT-VERIFIED: ни одного наблюдения шага не прочитано", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    report = evaluate(points, window_steps=args.window_steps, final=args.final)
    if args.json:
        print(json.dumps({"report": report, "inputs": notes}, ensure_ascii=False,
                         indent=2, sort_keys=True))
    else:
        print(render(report, notes))
    return EXIT_DEGENERATE if report["verdict"] == "degenerate_reward" else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
