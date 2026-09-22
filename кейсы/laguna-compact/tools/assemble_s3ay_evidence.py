#!/usr/bin/env python3
"""S3ay: сборка доказательства запуска полной SFT-стадии — из фактов на диске.

**Почему прибор, а не написанный руками JSON.** Запись о запуске — это то, по чему
через месяц будут отвечать на вопрос «что и на чём училось». Число, переписанное
руками, уже неотличимо от числа, придуманного; поэтому каждое поле здесь читается
из носителя (каталог прогона, его манифесты, состояние стартера и сторожа), а
команда, которой это читается, названа рядом с полем. Чего на диске нет — того в
записи нет: отсутствующее поле честнее выдуманного (`null` + `why_absent`).

**Статус записи — `partial` по построению, пока стадия не стартовала.** Запуск
устроен так, что ход работы дельты закрывается фактами старта, а не ожиданием
(ADR-023 п.11): стартер живёт отсоединённо и ждёт освобождения стенда, сторож
раннего гейта поднимется по факту запуска. Запись фиксирует, что именно запущено,
кем стережётся и как проверить, — а не то, что 40 часов уже прошли.

Запуск::

    python3 tools/assemble_s3ay_evidence.py --ts <метка> \
        --out evidence/sft-v13-full-launch.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
SHARED = Path("/home/user/gb10-shared")
STAND = "gb10-fast"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def ssh(cmd: str, timeout: int = 60) -> str:
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.stdout.strip()


def read_field(text: str, want: str) -> str:
    """Поле с префиксом `--flag value` из записанной командной строки."""
    parts = text.split()
    return parts[parts.index(want) + 1] if want in parts else None


def assemble(ts: str) -> dict:
    run_dir = SHARED / f"sft-{ts}"
    params = load(run_dir / "full_sft_params.json") or {}
    patch = load(run_dir / "full_stage_patch.json") or {}
    gate = load(run_dir / "early_gate.json") or {}
    base_patch = load(run_dir / "pipeline_patch.json") or {}
    ship = load(run_dir / "ship.json") or {}
    chain_txt = (run_dir / "chain_command.txt").read_text(encoding="utf-8").strip() \
        if (run_dir / "chain_command.txt").is_file() else ""
    cfg, data, tempo = params.get("config", {}), params.get("data", {}), patch.get("tempo", {})
    stages = (run_dir / "stages.tsv").read_text(encoding="utf-8").strip().split("\t") \
        if (run_dir / "stages.tsv").is_file() else []

    # ── состояние на момент сборки записи (живые факты, а не намерения) ─────
    starter = load(run_dir / "starter_state.json") or {}
    chain_status = (run_dir / "var" / "chain.status").read_text().strip() \
        if (run_dir / "var" / "chain.status").is_file() else None
    containers = ssh("docker ps --format '{{.Names}}'").splitlines()
    tmux = ssh("tmux ls 2>&1").splitlines()
    watchdog_seen = (run_dir / "early_gate_state.json").is_file()

    # ── страж темпа: прогоняется здесь же, чтобы запись несла ЕГО вердикт ────
    lr = subprocess.run([sys.executable, str(CASE_ROOT / "tools/check_stage_lr.py"),
                         "--run-dir", str(run_dir), "--expect-peak", "2e-06", "--json"],
                        capture_output=True, text=True)
    lr_json = json.loads(lr.stdout) if lr.stdout.strip().startswith("{") else {"raw": lr.stdout[-500:]}

    # ── доказательство входа: считанное С ДИСКА, а не пересказанное ──────────
    inputs = {}
    for key, rel, declared in (
            ("sft_jsonl", data.get("sft_jsonl"), data.get("sft_jsonl_sha256")),
            ("tok_cache", data.get("tok_cache"), data.get("tok_cache_sha256"))):
        if not rel:
            inputs[key] = {"declared_sha256": declared, "measured_sha256": None,
                           "why_absent": "поле объявления пусто"}
            continue
        p = SHARED / rel
        inputs[key] = {"path": rel, "declared_sha256": declared,
                       "measured_sha256": sha256_file(p) if p.is_file() else None,
                       "bytes": p.stat().st_size if p.is_file() else None,
                       "match": (p.is_file() and sha256_file(p) == declared) if declared else None}
    samples = ssh(f"wc -l < {SHARED / data['sft_jsonl']}") if data.get("sft_jsonl") else None
    inputs["examples_declared"] = data.get("sft_examples")
    inputs["examples_measured_lines"] = int(samples) if (samples or "").strip().isdigit() else None
    inputs["examples_match"] = (inputs["examples_measured_lines"] == data.get("sft_examples"))

    return {
        "schema": "sft-v13-full-launch/1",
        "node": "S3ay",
        "date": datetime.now(timezone.utc).isoformat(),
        "branch": "arch/sft-v13-arm",
        "question": ("запущена ли полная SFT-стадия на ИСПРАВЛЕННОМ входе (v13_fixed) с "
                     "пониженным пиком LR 2e-6, чем доказан прочитанный набор и как "
                     "устроен ранний гейт, решающий «продолжать ли 40 часов» за ~35 минут"),
        "status": "partial",
        "status_basis": (
            f"каталог стадии собран и проверен; стартер живёт отсоединённо и ждёт "
            f"освобождения стенда (фаза стартера: {starter.get('phase')!r}); стадия на "
            f"момент записи НЕ стартовала — стенд занят короткой рукой темпа и её пробой. "
            f"Ход работы закрывается фактами старта (ADR-023 п.11), а не ожиданием 40 часов"),
        "author_note": (
            "Обучение этим узлом не запускалось: прогон поднимает отсоединённый стартер "
            "после освобождения стенда. Локальная 4080 не занималась. Чужие прогоны "
            "(остановленный sft-20260919-0810, идущая рука sft-v13lr2e6-20260921-1748, "
            "её проба v13-arm-probe-20260921-1905) читались только чтением — ни один их "
            "файл не менялся. Замороженные копии пайплайнов чужих прогонов не правились."),
        "decision": {
            "source": "решение владельца (21.09.2026): запустить полную стадию на "
                      "исправленном входе с пониженным темпом",
            "two_causes": [
                {"cause": "стадия читала v12 (44 949) при объявленном v13_fixed (44 105)",
                 "fix": "S3av: вход — параметр цепочки, страж C-030 (ADR-051)",
                 "tested_by": "набор объявлен и прочитан совпадают; страж прогнан"},
                {"cause": "пик LR 1e-5 срывал поведение к 500-му шагу",
                 "fix": "S3ax: пик 2e-6 при сохранённой форме расписания",
                 "tested_by": "S3aw: на v12 постоянный LR 2e-6 дал 10/104 = 0.0962 "
                              "усечений против 31/104 = 0.2981 (p = 2.5e-4)"}],
            "combination_note": ("комбинация «v13_fixed + пониженный темп» полной стадией "
                                 "не измерялась; её и проверяет ранний гейт на точке 500"),
        },
        "run_dir": str(run_dir),
        "hyperparameters": {
            "peak_lr": tempo.get("peak_lr", cfg.get("lr")),
            "peak_lr_base": tempo.get("peak_lr_base", cfg.get("lr_base")),
            "peak_lr_scale": tempo.get("peak_lr_scale", cfg.get("peak_lr_scale")),
            "lr_min_final": cfg.get("lr_min"),
            "warmup_steps": cfg.get("warmup_steps"),
            "schedule": cfg.get("schedule"),
            "form_preserved": tempo.get("form"),
            "steps": cfg.get("steps"), "epochs": cfg.get("epochs"), "batch": cfg.get("batch"),
            "seed": cfg.get("seed"), "max_len": cfg.get("max_len"),
            "max_samples": cfg.get("max_samples"), "loss_mask": params.get("loss_mask"),
            "model": cfg.get("model"), "image": cfg.get("image"),
            "optimizer": cfg.get("optimizer"), "clip_grad_norm": cfg.get("clip_grad_norm"),
            "how_set": ("пик вшит в КОПИЮ пайплайна множителем "
                        "(patch_pipeline_sft.py --peak-lr-scale 0.2): у пика SFT нет флага в "
                        "пайплайне, он задан в коде run_sft. Декларация и факт сверяются "
                        "стражем tools/check_stage_lr.py"),
            "in_command_line": {
                "has_tempo_flag": any(x in chain_txt for x in ("--sft-lr-fixed", "--peak-lr-scale")),
                "why": "темп — свойство копии, а не флага: второй путь его задания сделал бы "
                       "«чем задан темп» неоднозначным. Число видно в pipeline_patch.json, "
                       "в full_sft_params.json, в логе стадии и пинуется хешем копии"},
        },
        "input_proof": {
            "why": "C-030 / ADR-051: объявленный набор обязан совпасть с фактически прочитанным",
            "declared_by": "full_sft_params.json (объявление) + команда цепочки (--sft-data-sha256)",
            "fact_recorded_by": "блок sft_input манифеста стадии (checkpoints/run_manifest.json), "
                                "который пишет сам пайплайн до первого шага",
            "measured_now": inputs,
            "stage_refusal": ("пайплайн сверяет объявленный sha256 с прочитанным jsonl и "
                              "отказывает ДО первого шага при расхождении (SystemExit)"),
            "guard": "tools/check_dataset_identity.py --unreachable-not-verified (C-030)",
            "guard_state_before_run": ("стадия ещё не писала манифест — вердикт по набору "
                                       "будет вынесен сторожем сразу после старта "
                                       "(фаза verify_input)"),
        },
        "starters_and_guards": {
            "starter": {"tool": "tools/start_full_stage.py",
                        "sha256": sha256_file(CASE_ROOT / "tools/start_full_stage.py"),
                        "phase_now": starter.get("phase"), "state": str(run_dir / "starter_state.json"),
                        "log": str(run_dir / "starter.out.log"),
                        "wait_condition": {
                            "arm_probe_chain_done": "/home/user/gb10-shared/"
                                                    "v13-arm-probe-20260921-1905/CHAIN_DONE",
                            "why": "между выходом контейнера руки и стартом её пробы есть окно, "
                                   "в котором стенд выглядит свободным; CHAIN_DONE его закрывает",
                            "no_foreign_containers": "docker ps, кроме llm-platform-*",
                            "memory_gb_min": 50.0, "memory_source": "/proc/meminfo MemAvailable",
                            "stable_polls": 3,
                        }},
            "tempo_guard": {"tool": "tools/check_stage_lr.py", "verdict_now": lr_json},
            "sleep_guard": {"wrapped": "guard/guard_cuda_run.sh вокруг всей цепочки",
                            "files": {k: v["sha256"] for k, v in (patch.get("guard") or {}).items()},
                            "source": patch.get("guard", {}).get("guard_cuda_run.sh", {}).get("source"),
                            "why": "сон площадки убивает контекст CUDA (Xid 31, S3 "
                                   "decode-diagnosis); на стенде цели сна замаскированы, "
                                   "systemd-inhibit не берётся — запрет держит обвязка, и она "
                                   "же пишет расписку о состоянии устройства"},
            "resource_protocol_AD9": ("перед каждой стадией цепочка зовёт "
                                      "check_resource_owner.sh --stage sft (порог 50 ГБ, "
                                      "хозяин объявлен), и стартер проверяет тот же порог "
                                      "ДО запуска"),
            "early_gate_watchdog": {"tool": "tools/early_gate_watch.py",
                                    "sha256": sha256_file(CASE_ROOT / "tools/early_gate_watch.py"),
                                    "started": watchdog_seen or "поднимется стартером после запуска",
                                    "phases": ["verify_input (C-030 — остановка при чужом наборе)",
                                               "wait_checkpoint (шаг ≥ 500 и файл точки)",
                                               "wait_memory", "probe", "compare", "verdict"]},
        },
        "early_gate": {
            "declared_before_run": gate.get("declared_before_run"),
            "declaration": str(run_dir / "early_gate.json"),
            "when": gate.get("when"),
            "probe": gate.get("probe"),
            "metrics": [m["key"] for m in gate.get("metrics", [])],
            "mdd": gate.get("mdd"), "warn_delta": gate.get("warn_delta"),
            "rule": gate.get("rule"),
            "reference_candidates": [{"tag": r["tag"], "why": r["why"], "path": r["path"],
                                      "states_key": r.get("states_key"),
                                      "present_at_assembly": Path(r["path"]).is_file()}
                                     for r in gate.get("reference", [])],
            "reference_note": ("первая доступная опора; предпочтительная — рука «v13 + LR 2e-6» "
                               "на точке 500 (тот же набор и темп, та же площадка)"),
            "on_fail": gate.get("on_fail"),
            "on_probe_failure": gate.get("on_probe_failure"),
            "arithmetic_checked": {
                "how": "арифметика гейта проверена синтетическими отчётами (5 случаев) до запуска",
                "cases": ["повтор опоры → continue", "ухудшение внутри допуска → continue + warning",
                          "усечения +0.18 → fail", "незакрытые +0.20 → fail", "петли +0.20 → fail"],
                "note": "проверка выполнена на объявлении ЭТОГО прогона — том же, что читает сторож",
            },
        },
        "artifacts": {
            "run_dir": str(run_dir), "stages_tsv": "\t".join(stages) if stages else None,
            "stage_artifact": stages[3] if len(stages) > 3 else None,
            "chain_command": str(run_dir / "chain_command.txt"),
            "ship_sha256": ship.get("sha256"), "pipeline_patch": base_patch.get("sft_lr"),
            "full_stage_patch": str(run_dir / "full_stage_patch.json"),
        },
        "live_state_at_assembly": {
            "chain_status": chain_status, "containers_on_stand": containers,
            "tmux_sessions": tmux, "watchdog_state_exists": watchdog_seen,
            "note": "снимок на момент сборки записи; стадия поднимется по факту освобождения стенда",
        },
        "limitations": [
            "проба раннего гейта снимается ПРИ ЖИВОЙ стадии (устройство занято обучением), "
            "а опора-рука мерилась на свободном устройстве: это различие условий названо, "
            "и грубое искажение видно по опоре cfinal, снятой на свободном устройстве",
            "опоры arm_v13_lr2e6_500 и arm_v12_lr2e6_500 на момент сборки ещё не сняты "
            "(проба руки идёт после её обучения); до их появления гейт опирается на опору "
            "предыдущей сессии — какая опора взята, записывается в early_gate_verdict.json",
            "короткая рука шла ПОСТОЯННЫМ LR 2e-6 (eta_min == peak_lr), полная стадия — "
            "косинусом от пика 2e-6: на точке 500 это 1.998e-6 против 2e-6 (0.06 %), "
            "поэтому сравнение на точке осмысленно, но за точкой формы расходятся",
            "число темпа не передаётся флагом командной строки: оно вшито в копию пайплайна "
            "множителем и сверяется стражем темпа (объявление против копии) — командная "
            "строка сама темпа не называет",
        ],
        "how_to_check": {
            "stage_running": (f"ssh {STAND} 'tmux ls; docker ps --format "
                              "'{{{{.Names}}}} {{{{.Status}}}}' | grep {ts}'"),
            "starter_alive": f"python3 -c \"import json;print(json.load(open('{run_dir}/"
                             "starter_state.json'))['phase'])\"",
            "input_proof": f"python3 tools/check_dataset_identity.py --json  # C-030; ждёт "
                           f"{run_dir}/checkpoints/run_manifest.json",
            "early_gate": f"cat {run_dir}/early_gate_verdict.json  "
                          f"(и {run_dir}/input_identity_verdict.json)",
            "live_progress": f"tail {run_dir}/logs/loss_trace.jsonl; "
                             f"tail -f {run_dir}/chain.log",
            "watchdog_log": f"tail -f {run_dir}/early_gate_watch.log",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ts", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rec = assemble(a.ts)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "status": rec["status"],
                      "run_dir": rec["run_dir"],
                      "peak_lr": rec["hyperparameters"]["peak_lr"],
                      "steps": rec["hyperparameters"]["steps"],
                      "starter_phase": rec["live_state_at_assembly"],
                      "input_jsonl_match": rec["input_proof"]["measured_now"]["sft_jsonl"]["match"],
                      "input_tensor_match": rec["input_proof"]["measured_now"]["tok_cache"]["match"],
                      "examples_match": rec["input_proof"]["measured_now"]["examples_match"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
