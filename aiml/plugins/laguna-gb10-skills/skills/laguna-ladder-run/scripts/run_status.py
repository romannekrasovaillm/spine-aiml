#!/usr/bin/env python3
"""Сводка состояния прогона лесенки Laguna (CPT → SFT → RL → eval).

Использование:
    python3 run_status.py ~/experiments/v12_qwen25-15b_s42 \
        [--status-log ~/experiments/v12_ladder_status.log] \
        [--restart-log ~/gb10-shared/v12_restart.log] \
        [--rl-steps 500] [--stale-hours 3] [--json]

Смотрит только файловую систему и логи — ничего не запускает и не убивает.
Имена артефактов типовые для v12; при другой раскладке задайте --ckpt-dir.
"""
import argparse
import glob
import json
import os
import re
import sys
import time

STAGES = [
    # (стадия, финальный артефакт, паттерн промежуточных чекпоинтов, eval-файлы)
    ("CPT", "checkpoint_final.pt", "checkpoint_[0-9]*.pt",
     ["eval_results_base.json", "eval_results_cpt.json"]),
    ("SFT", "sft_checkpoint_final.pt", "sft_checkpoint_[0-9]*.pt",
     ["eval_results_sft.json"]),
    ("RL", "rl_checkpoint_final.pt", "rl_checkpoint_[0-9]*.pt",
     ["eval_results.json"]),
]


def find(root, name):
    hits = glob.glob(os.path.join(root, "**", name), recursive=True)
    return sorted(hits, key=os.path.getmtime)


def step_of(path):
    m = re.search(r"_(\d+)\.pt$", os.path.basename(path))
    return int(m.group(1)) if m else None


def age_h(path):
    return (time.time() - os.path.getmtime(path)) / 3600.0


def gb(path):
    return os.path.getsize(path) / 1e9


def tail(path, n=50):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


def parse_restarts(path):
    """Строки вида: 2026-09-11 03:14 RL rc=1 resume=rl_checkpoint_460.pt"""
    out = []
    for line in tail(path, 500):
        rc = re.search(r"rc=(\d+)", line)
        if not rc:
            continue
        stage = next((s for s in ("CPT", "SFT", "RL", "eval") if s in line.upper()), "?")
        out.append({"line": line.strip(), "stage": stage, "rc": int(rc.group(1))})
    return out


def rollout_progress(root):
    hits = find(root, "rollouts_log.jsonl")
    if not hits:
        return None
    last_step = None
    for line in tail(hits[-1], 200):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for k in ("step", "global_step", "rl_step"):
            if k in rec:
                last_step = max(last_step or 0, int(rec[k]))
    return {"file": hits[-1], "last_step": last_step, "age_h": age_h(hits[-1])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--ckpt-dir", default=None, help="каталог чекпоинтов (по умолчанию run_dir)")
    ap.add_argument("--status-log")
    ap.add_argument("--restart-log")
    ap.add_argument("--rl-steps", type=int, default=500)
    ap.add_argument("--stale-hours", type=float, default=3.0,
                    help="порог: чекпоинт старше → подозрение на зависание")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    root = os.path.expanduser(a.run_dir)
    if not os.path.isdir(root):
        sys.exit(f"нет каталога: {root}")
    ckroot = os.path.expanduser(a.ckpt_dir) if a.ckpt_dir else root

    report = {"run": os.path.basename(os.path.abspath(root)), "stages": [], "notes": []}
    current = None
    for stage, final, pattern, evals in STAGES:
        fin = find(ckroot, final)
        inter = find(ckroot, pattern)
        ev = {e: bool(find(root, e)) for e in evals}
        st = {"stage": stage, "final": bool(fin), "evals": ev,
              "last_ckpt": None, "last_step": None, "ckpt_age_h": None, "ckpt_gb": None}
        if inter:
            last = inter[-1]
            st.update(last_ckpt=os.path.basename(last), last_step=step_of(last),
                      ckpt_age_h=round(age_h(last), 2), ckpt_gb=round(gb(last), 2))
        elif fin:
            st.update(last_ckpt=os.path.basename(fin[-1]), ckpt_age_h=round(age_h(fin[-1]), 2),
                      ckpt_gb=round(gb(fin[-1]), 2))
        complete = bool(fin) and all(ev.values())
        st["complete"] = complete
        if not complete and current is None:
            current = stage
        if fin and not all(ev.values()):
            report["notes"].append(
                f"{stage}: финальный чекпоинт есть, но eval неполный — не считать DONE")
        report["stages"].append(st)

    report["current_stage"] = current or "ALL DONE"

    if current == "RL":
        rp = rollout_progress(root)
        if rp:
            report["rl_progress"] = rp
            if rp["last_step"] is not None:
                report["rl_progress"]["remaining"] = a.rl_steps - rp["last_step"]

    for st in report["stages"]:
        if st["stage"] == current and st["ckpt_age_h"] is not None \
                and st["ckpt_age_h"] > a.stale_hours:
            report["notes"].append(
                f"{current}: последний чекпоинт не обновлялся {st['ckpt_age_h']} ч "
                f"(> {a.stale_hours}) — проверить живость процесса")

    if a.restart_log and os.path.exists(os.path.expanduser(a.restart_log)):
        rs = parse_restarts(os.path.expanduser(a.restart_log))
        report["restarts"] = {"total": len(rs),
                              "by_rc": {}, "last": rs[-3:]}
        for r in rs:
            key = f"{r['stage']} rc={r['rc']}"
            report["restarts"]["by_rc"][key] = report["restarts"]["by_rc"].get(key, 0) + 1
        if any(r["rc"] == 137 for r in rs[-3:]):
            report["notes"].append("среди последних рестартов есть rc=137 (OOM) — см. failure-modes.md")

    if a.status_log and os.path.exists(os.path.expanduser(a.status_log)):
        report["status_log_tail"] = tail(os.path.expanduser(a.status_log), 8)

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print(f"Прогон: {report['run']}    текущая стадия: {report['current_stage']}")
    for st in report["stages"]:
        mark = "✓" if st["complete"] else ("…" if st["last_ckpt"] else "·")
        evs = ", ".join(f"{k}{'✓' if v else '✗'}" for k, v in st["evals"].items())
        ck = (f"{st['last_ckpt']} (шаг {st['last_step']}, {st['ckpt_age_h']} ч назад, "
              f"{st['ckpt_gb']} ГБ)") if st["last_ckpt"] else "нет чекпоинтов"
        print(f" {mark} {st['stage']:3} | {ck} | eval: {evs}")
    if "rl_progress" in report:
        rp = report["rl_progress"]
        print(f"   RL роллауты: шаг {rp['last_step']} / {a.rl_steps}, осталось {rp.get('remaining')}, "
              f"лог обновлён {rp['age_h']:.1f} ч назад")
    if "restarts" in report:
        print(f" Рестарты: {report['restarts']['total']}  {report['restarts']['by_rc']}")
        for r in report["restarts"]["last"]:
            print(f"   {r['line']}")
    if report.get("status_log_tail"):
        print(" Статус-лог (хвост):")
        for line in report["status_log_tail"]:
            print("   " + line)
    for n in report["notes"]:
        print(f" ! {n}")


if __name__ == "__main__":
    main()
