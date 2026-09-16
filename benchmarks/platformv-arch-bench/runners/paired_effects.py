#!/usr/bin/env python3
"""paired_effects.py — каноничные эффекты бенчмарка (README и docx-отчёт).

Читает $PVBENCH_RUNS/results.jsonl (после judge.py/analyze.py) и считает
эффекты как ПАРНУЮ разность по ячейкам «задача × повтор»: из каждой руки
берутся только общие (task, rep) ячейки пары, d_i = a_i − b_i, эффект =
mean(d); 95% CI — bootstrap по d (10000 ресэмплов, seed=42, общий ГСЧ на
все эффекты в порядке спецификации — идиома analyze.py). Эталон Spine —
spine-arch-think; все сравнения по отдельным конфигурациям, без
усреднения рук.

Режимы:
  (без флагов)  печать таблицы + сверка с committed
                results/effects_paired.json (публикованные каноничные
                значения из README; допуск — джиттер бутстрапа:
                |Δdiff| ≤ 0.15, |ΔCI| ≤ 0.5);
  --write       перезаписать results/effects_paired.json пересчитанным
                (для НОВЫХ данных; опубликованный снапшот не трогать —
                он зафиксирован в README и docx).

Только stdlib.
"""
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

RUNS = pvlib.RUNS
BOOT_N = 10000
SEED = 42
# допуски сверки с опубликованными значениями (джиттер бутстрапа)
TOL_DIFF = 0.15
TOL_CI = 0.5

# (метка, рука A, рука B, модель, пояснение) — порядок = порядок в README
EFFECTS = [
    ("think − theseus-plain (dsf)", "spine-arch-think", "theseus-plain",
     "dsf", "Spine значимо сильнее Theseus (H1 ✓)"),
    ("think − claude-plain, фабричный (dsf)", "spine-arch-think",
     "claude-plain", "dsf", "премия над фабричным Claude Code (паритет)"),
    ("think − claude-arch, arch-контекст (dsf)", "spine-arch-think",
     "claude-arch", "dsf",
     "премия над Claude Code с кастомизацией (паритет)"),
    ("think − kimi-plain (dsf)", "spine-arch-think", "kimi-plain", "dsf",
     "паритет с Kimi Code"),
    ("think − kimi-arch (dsf)", "spine-arch-think", "kimi-arch", "dsf",
     "паритет с Kimi Code + кастомизация"),
    ("think − spine-arch, без ризонинга (dsf)", "spine-arch-think",
     "spine-arch", "dsf", "премия ризонинга на V4.1 Flash"),
    ("think − spine-arch, без ризонинга (dsp)", "spine-arch-think",
     "spine-arch", "dsp", "премия ризонинга на V4 Pro — значима"),
    ("think − claude-plain (glm)", "spine-arch-think", "claude-plain",
     "glm", "на GLM think выше Claude Code — на грани значимости "
            "(8 общих задач, эффект вытягивает CMP-ARCH-001)"),
    ("think − kimi-plain (glm)", "spine-arch-think", "kimi-plain", "glm",
     "на GLM паритет с Kimi Code"),
    ("think − claude-plain (dsp)", "spine-arch-think", "claude-plain",
     "dsp", "на V4 Pro паритет с Claude Code"),
    ("spine-arch − claude-plain (dsf, без ризонинга)", "spine-arch",
     "claude-plain", "dsf", "без ризонинга — паритет-минус"),
    ("spine-arch − kimi-plain (dsf)", "spine-arch", "kimi-plain", "dsf",
     "без ризонинга — отставание от Kimi Code"),
    ("spine-arch − spine-min (dsf)", "spine-arch", "spine-min", "dsf",
     "вклад формата — слабый плюс поверх харнесса"),
    ("claude-arch − claude-plain (dsf)", "claude-arch", "claude-plain",
     "dsf", "H2 ✗: кастомизация Claude Code без эффекта"),
    ("kimi-arch − kimi-plain (dsf)", "kimi-arch", "kimi-plain", "dsf",
     "H2 ✗: кастомизация Kimi Code без эффекта"),
    ("claude-plain − raw-llm (dsf)", "claude-plain", "raw-llm", "dsf",
     "контур + ризонинг дают +15 над голой моделью (raw — неризонящий "
     "алиас, D21)"),
]

METHOD = ("парная разность по ячейкам «задача × повтор», bootstrap 95% CI "
          f"({BOOT_N} ресэмплов, seed={SEED}); эталон Spine — "
          "spine-arch-think; сравнения по отдельным конфигурациям Spine, "
          "без усреднения рук")


def load_judged():
    recs = [json.loads(x) for x in
            open(RUNS / "results.jsonl", encoding="utf-8")]
    return [r for r in recs if r.get("judge_total") is not None]


def paired_diffs(judged, a, b, model):
    """d_i = a_i − b_i по общим ячейкам (task, rep) двух рук на модели."""
    ca, cb = {}, {}
    for r in judged:
        if r["model"] != model:
            continue
        key = (r["task"], r["rep"])
        if r["condition"] == a:
            ca[key] = r["judge_total"]
        elif r["condition"] == b:
            cb[key] = r["judge_total"]
    common = sorted(set(ca) & set(cb))
    return [ca[k] - cb[k] for k in common]


def bootstrap_ci(diffs, rng):
    boots = []
    for _ in range(BOOT_N):
        boots.append(statistics.mean(rng.choice(diffs) for _ in diffs))
    boots.sort()
    return boots[int(0.025 * BOOT_N)], boots[int(0.975 * BOOT_N) - 1]


def compute(judged):
    rng = random.Random(SEED)
    out = {}
    for label, a, b, model, note in EFFECTS:
        diffs = paired_diffs(judged, a, b, model)
        if not diffs:
            out[label] = None
            continue
        lo, hi = bootstrap_ci(diffs, rng)
        out[label] = {"diff": round(statistics.mean(diffs), 2),
                      "ci_lo": round(lo, 2), "ci_hi": round(hi, 2),
                      "n_pairs": len(diffs), "note": note}
    return out


def to_json(effects):
    return {"method": METHOD, "source": "runners/paired_effects.py из "
            "results.jsonl", "reference": "spine-arch-think",
            "effects": effects}


def main():
    judged = load_judged()
    effects = compute(judged)
    print(f"оценённых ячеек: {len(judged)}")
    print(f"{'сравнение':46s} {'Δ':>6s} {'95% CI':>18s} {'пар':>4s}")
    for label, _, _, _, _ in EFFECTS:
        v = effects.get(label)
        if not v:
            print(f"{label:46s} — нет общих ячеек")
            continue
        print(f"{label:46s} {v['diff']:+6.1f} "
              f"[{v['ci_lo']:+6.1f}; {v['ci_hi']:>+6.1f}] {v['n_pairs']:>4d}")

    if "--write" in sys.argv:
        dst = RUNS / "effects_paired.json"
        dst.write_text(json.dumps(to_json(effects), ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        print(f"\nзаписано: {dst}")
        return

    # сверка с committed (публикация README/docx)
    pub_path = RUNS / "effects_paired.json"
    if not pub_path.is_file():
        print(f"\n{pub_path} отсутствует — сверять не с чем (--write)")
        return
    pub = json.loads(pub_path.read_text(encoding="utf-8")).get("effects", {})
    print(f"\nсверка с {pub_path.name} (допуск: Δdiff ≤ {TOL_DIFF}, "
          f"ΔCI ≤ {TOL_CI} — джиттер бутстрапа):")
    worst = 0.0
    bad = 0
    for label, _, _, _, _ in EFFECTS:
        v, p = effects.get(label), pub.get(label)
        if not v or not p:
            print(f"  {label}: пропуск (нет данных)")
            continue
        dev = max(abs(v["diff"] - p["diff"]), abs(v["ci_lo"] - p["ci_lo"]),
                  abs(v["ci_hi"] - p["ci_hi"]))
        worst = max(worst, dev)
        ok = (abs(v["diff"] - p["diff"]) <= TOL_DIFF and
              abs(v["ci_lo"] - p["ci_lo"]) <= TOL_CI and
              abs(v["ci_hi"] - p["ci_hi"]) <= TOL_CI)
        bad += not ok
        print(f"  {'OK  ' if ok else 'DIFF'} {label}: пересчёт "
              f"{v['diff']:+.1f} [{v['ci_lo']:+.1f}; {v['ci_hi']:+.1f}] vs "
              f"README {p['diff']:+.1f} [{p['ci_lo']:+.1f}; "
              f"{p['ci_hi']:+.1f}] (max |Δ| {dev:.2f})")
    print(f"\nхудшее отклонение: {worst:.2f}; расхождений сверх допуска: "
          f"{bad}")


if __name__ == "__main__":
    main()
