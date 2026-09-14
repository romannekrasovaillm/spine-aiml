#!/usr/bin/env python3
"""Здоровье RL-контура по rollouts_log.jsonl и/или агрегату по шагам.

    python3 rl_health.py rollouts_log.jsonl [--agg rl_steps_agg.csv] [--total-steps 500] [--json]
    python3 rl_health.py rollouts_log.jsonl --inspect

rollouts_log.jsonl — одна запись на роллаут (или на шаг). Поля угадываются:
  step: step|global_step|rl_step
  reward: reward|score|r
  entropy: entropy|ent|policy_entropy
  kl: kl|kl_div|approx_kl
  clip: clip_fraction|clipfrac|clip
  length: response_len|resp_len|num_tokens|length|completion_tokens
  tool_calls: tool_calls|n_tool_calls|num_tool_calls
  ppl_general: ppl_general|gen_ppl|ppl_gen ; ppl_domain: ppl_domain|domain_ppl
  parse_error: parse_error|format_error|error
Переопределение: --field reward=my_reward. Пороги — references/thresholds.md.
"""
import argparse
import csv
import json
import statistics as st
import sys
from collections import defaultdict

KEYS = {
    "step": ("step", "global_step", "rl_step", "iteration"),
    "reward": ("reward", "score", "r", "total_reward"),
    "entropy": ("entropy", "ent", "policy_entropy", "mean_entropy"),
    "kl": ("kl", "kl_div", "approx_kl", "kl_mean"),
    "clip": ("clip_fraction", "clipfrac", "clip", "clip_frac"),
    "length": ("response_len", "resp_len", "num_tokens", "length", "completion_tokens", "response_length"),
    "tool_calls": ("tool_calls", "n_tool_calls", "num_tool_calls", "tool_call_count"),
    "ppl_general": ("ppl_general", "gen_ppl", "ppl_gen", "general_ppl"),
    "ppl_domain": ("ppl_domain", "domain_ppl", "ppl_dom"),
    "parse_error": ("parse_error", "format_error", "error", "parse_failed"),
    "pass": ("pass", "passed", "success"),
}

TH = {  # (внимание, стоп)
    "entropy_low": (0.9, 0.5), "entropy_high": (2.0, 2.5),
    "kl": (0.001, 0.01), "clip": (0.05, 0.2), "ppl_dev": (0.005, 0.02),
    "reward_trend": (0.0, -0.05), "pass_rate": (0.30, 0.10),
    "len_trend": (0.15, 0.5), "tool_hi": (2.0, 3.0), "tool_lo": (1.0, 0.5),
    "parse_err": (0.02, 0.05),
}


def get(rec, name, fields):
    if name in fields:
        v = rec.get(fields[name])
        return v
    for k in KEYS[name]:
        if k in rec and rec[k] is not None:
            return rec[k]
    # вложенные метрики
    for sub in ("metrics", "stats", "info"):
        if isinstance(rec.get(sub), dict):
            for k in KEYS[name]:
                if k in rec[sub]:
                    return rec[sub][k]
    return None


def fnum(v):
    if isinstance(v, bool):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def count_tool_calls(rec):
    txt = None
    for k in ("response", "completion", "output", "text", "answer"):
        if isinstance(rec.get(k), str):
            txt = rec[k]
            break
    if txt is None:
        return None
    return txt.count("<tool_call>")


def load_jsonl(path, fields):
    per_step = defaultdict(lambda: defaultdict(list))
    n_bad = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                n_bad += 1
                continue
            step = fnum(get(rec, "step", fields))
            if step is None:
                continue
            step = int(step)
            for m in ("reward", "entropy", "kl", "clip", "length", "ppl_general", "ppl_domain"):
                v = fnum(get(rec, m, fields))
                if v is not None:
                    per_step[step][m].append(v)
            tc = fnum(get(rec, "tool_calls", fields))
            if tc is None:
                tc = count_tool_calls(rec)
            if tc is not None:
                per_step[step]["tool_calls"].append(float(tc))
            pe = get(rec, "parse_error", fields)
            per_step[step]["parse_error"].append(1.0 if pe else 0.0)
            ps = get(rec, "pass", fields)
            if ps is None:
                rw = fnum(get(rec, "reward", fields))
                ps = (rw is not None and rw >= 1.0)
            per_step[step]["pass"].append(1.0 if ps else 0.0)
    return per_step, n_bad


def load_agg(path, fields, per_step):
    with open(path, encoding="utf-8") as f:
        for rec in csv.DictReader(f):
            step = fnum(get(rec, "step", fields))
            if step is None:
                continue
            step = int(step)
            for m in ("reward", "entropy", "kl", "clip", "length", "tool_calls", "ppl_general", "ppl_domain"):
                v = fnum(get(rec, m, fields))
                if v is not None and not per_step[step][m]:
                    per_step[step][m].append(v)
            pr = fnum(rec.get("pass_rate") or rec.get("pass"))
            if pr is not None and not per_step[step]["pass"]:
                per_step[step]["pass"].append(pr if pr <= 1 else pr / 100)


def series(per_step, m):
    return [(s, st.mean(v[m])) for s, v in sorted(per_step.items()) if v[m]]


def quarters(ser):
    if not ser:
        return []
    q = max(1, len(ser) // 4)
    return [st.mean(x for _, x in ser[i * q:(i + 1) * q if i < 3 else None]) for i in range(4)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rollouts")
    ap.add_argument("--agg", default=None)
    ap.add_argument("--total-steps", type=int, default=500)
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--field", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    fields = dict(f.split("=", 1) for f in a.field)

    if a.inspect:
        with open(a.rollouts, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 2:
                    break
                try:
                    print(json.dumps(json.loads(line), ensure_ascii=False, indent=1)[:2500])
                except ValueError:
                    print(line[:500])
        return

    per_step, n_bad = load_jsonl(a.rollouts, fields)
    if a.agg:
        load_agg(a.agg, fields, per_step)
    if not per_step:
        sys.exit("не найдено записей с полем step — используй --inspect и --field step=...")

    steps = sorted(per_step)
    out = {"steps_seen": len(steps), "first_step": steps[0], "last_step": steps[-1],
           "total_steps": a.total_steps, "bad_lines": n_bad, "metrics": {}, "flags": []}
    flags = out["flags"]

    def summarize(m):
        ser = series(per_step, m)
        if not ser:
            return None
        vals = [x for _, x in ser]
        q = quarters(ser)
        d = {"mean": st.mean(vals), "min": min(vals), "max": max(vals),
             "q1": q[0], "q4": q[-1], "n_steps": len(ser)}
        out["metrics"][m] = d
        return d

    r = summarize("reward")
    p = summarize("pass")
    e = summarize("entropy")
    k = summarize("kl")
    c = summarize("clip")
    ln = summarize("length")
    tc = summarize("tool_calls")
    pg = summarize("ppl_general")
    summarize("ppl_domain")
    pe = summarize("parse_error")

    def flag(level, text):
        flags.append({"level": level, "text": text})

    if e:
        low = sum(1 for _, x in series(per_step, "entropy") if x < TH["entropy_low"][0])
        if e["min"] < TH["entropy_low"][1]:
            flag("STOP", f"энтропия опускалась до {e['min']:.2f} (< {TH['entropy_low'][1]}) — коллапс")
        elif low > 20:
            flag("WARN", f"энтропия ниже {TH['entropy_low'][0]} на {low} шагах")
        if e["max"] > TH["entropy_high"][1]:
            flag("STOP", f"энтропия до {e['max']:.2f} — шум/сломан формат")
    if k:
        lvl = "STOP" if k["max"] > TH["kl"][1] else ("WARN" if k["max"] > TH["kl"][0] else None)
        if lvl:
            flag(lvl, f"KL до {k['max']:.4f}")
    if c:
        lvl = "STOP" if c["mean"] > TH["clip"][1] else ("WARN" if c["mean"] > TH["clip"][0] else None)
        if lvl:
            flag(lvl, f"clip-fraction {c['mean']:.3f}")
    if pg:
        ser = series(per_step, "ppl_general")
        base = ser[0][1]
        dev = max(abs(x - base) / base for _, x in ser)
        out["metrics"]["ppl_general"]["max_rel_dev"] = dev
        lvl = "STOP" if dev > TH["ppl_dev"][1] else ("WARN" if dev > TH["ppl_dev"][0] else None)
        if lvl:
            flag(lvl, f"ppl_general отклонился на {dev:.1%} от первого замера — забывание?")
    if r:
        tr = r["q4"] - r["q1"]
        lvl = "STOP" if tr < TH["reward_trend"][1] else ("WARN" if tr < TH["reward_trend"][0] else None)
        if lvl:
            flag(lvl, f"награда: Q1 {r['q1']:.3f} → Q4 {r['q4']:.3f} ({tr:+.3f})")
    if p:
        lvl = "STOP" if p["mean"] < TH["pass_rate"][1] else ("WARN" if p["mean"] < TH["pass_rate"][0] else None)
        if lvl:
            flag(lvl, f"pass-rate {p['mean']:.0%} — сигнал редкий")
    if ln and ln["q1"] > 0:
        tr = (ln["q4"] - ln["q1"]) / ln["q1"]
        lvl = "STOP" if tr > TH["len_trend"][1] else ("WARN" if abs(tr) > TH["len_trend"][0] else None)
        if lvl:
            flag(lvl, f"длина ответа: Q1 {ln['q1']:.0f} → Q4 {ln['q4']:.0f} ({tr:+.0%})")
    if tc:
        if tc["mean"] > TH["tool_hi"][1] or tc["mean"] < TH["tool_lo"][1]:
            flag("STOP", f"tool-calls/роллаут {tc['mean']:.2f} — формат вызова сломан?")
        elif tc["mean"] > TH["tool_hi"][0] or tc["mean"] < TH["tool_lo"][0]:
            flag("WARN", f"tool-calls/роллаут {tc['mean']:.2f}")
    if pe and pe["mean"] > 0:
        lvl = "STOP" if pe["mean"] > TH["parse_err"][1] else ("WARN" if pe["mean"] > TH["parse_err"][0] else None)
        if lvl:
            flag(lvl, f"ошибок парсинга {pe['mean']:.1%}")

    # профиль
    if r and k and not any(f["level"] == "STOP" for f in flags):
        if k["max"] <= TH["kl"][0] and abs(r["q4"] - r["q1"]) < 0.05:
            out["profile"] = "регуляризованная полировка (KL≈0, награда плоская): ждать роста качества, не попаданий"
        elif r["q4"] - r["q1"] >= 0.05:
            out["profile"] = "рост: награда растёт при стабильных KL/ppl"
        else:
            out["profile"] = "смешанный: см. флаги"
    elif any(f["level"] == "STOP" for f in flags):
        out["profile"] = "НЕЗДОРОВ: есть STOP-флаги"

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(f"Шаги: {out['first_step']}–{out['last_step']} / {a.total_steps} "
          f"({out['steps_seen']} с данными; битых строк {n_bad})")
    names = {"reward": "награда", "pass": "pass-rate", "entropy": "энтропия", "kl": "KL",
             "clip": "clip-fraction", "length": "длина ответа", "tool_calls": "tool-calls/роллаут",
             "ppl_general": "ppl_general", "ppl_domain": "ppl_domain", "parse_error": "ошибки парсинга"}
    for m, d in out["metrics"].items():
        extra = f"  откл. {d['max_rel_dev']:.1%}" if "max_rel_dev" in d else ""
        print(f"  {names.get(m, m):20} mean {d['mean']:.4g}  min {d['min']:.4g}  max {d['max']:.4g}  "
              f"Q1 {d['q1']:.4g} → Q4 {d['q4']:.4g}{extra}")
    print()
    for f in flags:
        print(f"  [{f['level']}] {f['text']}")
    if out.get("profile"):
        print(f"\nПрофиль: {out['profile']}")


if __name__ == "__main__":
    main()
