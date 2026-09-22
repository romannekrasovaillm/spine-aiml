#!/usr/bin/env python3
"""S3ae — сборка evidence дельты: остановка SFT, патч маски (НЕ применён), отказ ADR-037.

Предмет дельты изменился по ходу: решение ADR-036 (маскировать `<think>`) отменено
ADR-037 — замер показал, что рассуждение это **46.1 % символов ответов и 86.1 %
примеров**, а корень сдвига (язык входа в рассуждение) маска не лечит. Поэтому
отчёт несёт **две части**: сделанное (остановка стадии, сохранённые артефакты,
готовые инструменты) и **несделанное по решению** (маска не применена, длинный
прогон не запущен) — с числами объёма рассуждений и ссылкой на ADR-037.

Числа не пересказываются, а читаются: шаг остановки — из `STOP-RECORD.json` и
`logs/loss_trace.jsonl` остановленного прогона; правило и хеш патча — из
`pipeline_patch.json` и копии пайплайна в каталоге прогона; числа маски — из
отчёта проверки (`mask_verify.json`, контейнер с живым токенайзером) и из отчёта
самого прогона (`sft_mask_report.json`); факты старта — из `loss_trace.jsonl`,
`tmux ls`, `docker ps` нового прогона.

Почему не «сводка словами»: у этой дельты два предмета доказательства, и оба
числовые — (1) остановка не потеряла артефакты и помечена в манифесте, (2) маска
снимает рассуждения, а не ответы и не вызовы инструмента. Пересказ любого из них
проверить нельзя.
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
CTR_SHARED = "/workspace/shared"
STAND = "gb10-fast"


def sha256_file(p: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def ssh(cmd: str, timeout: int = 60) -> str:
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (p.stdout or p.stderr).strip()
    except Exception as e:                                     # стенд может быть недоступен
        return f"<ssh не удался: {type(e).__name__}>"


def read_json(p: Path) -> dict | None:
    #: Чтение через `try`, а не через `is_file()`: на сетевом томе (autofs/NFS)
    #: проверка существования проходит, а чтение следом даёт ENOENT — каталог
    #: прогона может быть снят, и отчёт обязан это пережить, а не падать.
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


# ─────────────────────────── остановленный прогон ───────────────────────────

def stopped_part(run: Path) -> dict:
    rec = read_json(run / "STOP-RECORD.json") or {}
    man = read_json(run / "run_manifest.json") or {}
    status = (run / "var" / "status" / "sft")
    chain = (run / "var" / "chain.status")
    marker = (run / "var" / "STOPPED")
    snap = rec.get("snapshot", {})
    ckpts = snap.get("checkpoints", [])
    return {
        "run_dir": str(run),
        "stopped_at_step": rec.get("stopped_at_step"),
        "stopped_by": rec.get("stopped_by"),
        "purpose": rec.get("purpose"),
        "loss_mask": rec.get("loss_mask"),
        "chain_status": chain.read_text().strip() if chain.is_file() else None,
        "stage_status": status.read_text().strip().split("\t")[0] if status.is_file() else None,
        "marker": marker.read_text().strip() if marker.is_file() else None,
        "checkpoints_kept": {"count": snap.get("checkpoints_count"),
                             "bytes": snap.get("checkpoints_bytes"),
                             "names": [c["name"] for c in ckpts]},
        "loss_trace_lines": snap.get("loss_trace_lines"),
        "manifest_note": rec.get("manifest_note"),
        "manifest_stage_status": next((s.get("status") for s in man.get("stages", [])
                                       if s.get("name") == "sft"), None),
        "sft_log_tail": snap.get("sft_log_tail"),
        "chain_log_tail": snap.get("chain_log_tail"),
    }


# ─────────────────────────── патч маски ───────────────────────────

def patch_part(run: Path, verify: dict | None, smoke: dict | None) -> dict:
    passport = read_json(run / "pipeline_patch.json") or {}
    staged_pipe = run / "laguna_pipeline_sft.py"
    vstats = (verify or {}).get("stats", {})
    return {
        "file": "tools/patch_pipeline_sft.py",
        "sha256": sha256_file(CASE_ROOT / "tools/patch_pipeline_sft.py"),
        "applied": False,
        "why_not_applied": "ADR-037: маскирование <think> отменено (46.1 % сигнала, 86.1 % "
                          "примеров; корень — язык входа в рассуждение — маска не лечит). "
                          "Патч сохранён как готовый инструмент, к длинному прогону не "
                          "применялся",
        "pipeline": {"path": str(staged_pipe), "sha256": sha256_file(staged_pipe),
                     "in_run_manifest": (read_json(run / "run_manifest.json") or {})
                     .get("pipeline_sha256")},
        "rule": (passport.get("think_mask") or {}).get("rule"),
        "switch": (passport.get("think_mask") or {}).get("switch"),
        "think_mask_verified": {
            "verdict": (verify or {}).get("verdict"),
            "checks": (verify or {}).get("checks"),
            "samples": vstats.get("samples"),
            "reasoning_must_mask_positions": vstats.get("must_mask_positions"),
            "reasoning_left_unmasked": vstats.get("reason_content_unmasked"),
            "tool_call_kept": [vstats.get("tool_call_unmasked"), vstats.get("tool_call_positions")],
            "answer_kept": [vstats.get("answer_after"), vstats.get("answer_before")],
            "delims_kept": [vstats.get("delims_unmasked"), vstats.get("delims_targets_before")],
            "empty_after": vstats.get("empty_after"),
            "leaked_positions": vstats.get("leaked_positions"),
            "switch_off_equals_unmasked_copy": vstats.get("base_mismatch") == 0,
            "tokenizer_ids": (verify or {}).get("tokenizer_ids"),
            "language": (verify or {}).get("language"),
            "how": "tools/verify_think_mask.py в том же образе и на том же кэше токенов, "
                   "что стадия; класс SFTDataset берётся из пропатченной копии",
        },
        "target_tokens_before": vstats.get("targets_before"),
        "target_tokens_after": vstats.get("targets_after"),
        "masked_share": (verify or {}).get("masked_share"),
        "mask_report_from_smoke_run": (smoke or {}).get("mask_report_from_run"),
        "smoke": {
            #: Отчёт короткого теста = пересчёт проверок по СОХРАНЁННОМУ логу
            #: (`--do evaluate`): пересчёт не требует повторного прогона и не
            #: зависит от того, что инструмент печатал в тот момент.
            "verdict": (smoke or {}).get("verdict"),
            "mode": (smoke or {}).get("mode"),
            "check_evolution": {
                "first_editor_bug": "проверка «NaN в лоссе нет» искала подстроку «nan» и ловила "
                                    "баннер PyTorch («Ronan Collobert») — прогон помечен FAIL "
                                    "при чистом лоссе (1.2861 на шаге 0, «SFT complete»)",
                "fixed": "регексп по (loss|ema|gnorm|val_loss)=nan + отдельная проверка "
                         "Traceback/исключения (первый прогон падал ValueError на шаге 0 — "
                         "NaN-проверка этот отказ не видит)",
                "negative_path_verified": "тот же гейт на логе упавшего прогона (0910) — FAIL "
                                          "по двум проверкам: «дошёл до конца» и «нет Traceback»",
            },
            "test_dir": (smoke or {}).get("test_dir"),
            "steps": (smoke or {}).get("steps"),
            "checks": (smoke or {}).get("checks"),
            "loss_lines": (smoke or {}).get("loss_lines"),
            "nan_lines": (smoke or {}).get("nan_lines"),
            "first_short_test_failure": {
                "pipeline_sha256": "ab06764715ae6810f9d6c99620e4302ac0b8574b830e1465b3dfffd8597a7dbe",
                "why": "первый короткий тест упал на шаге 0 в val_loss: маска получала "
                       "2-D батч (`int(ids[i])` на строке тензора). Поймано коротким "
                       "тестом, а не длинным прогоном; исправлено обходом по строкам",
            },
        },
    }


# ─────────────────────────── запущенный прогон ───────────────────────────

def launched_part(run: Path, settle_note: str | None = None) -> dict:
    if not (run / "var" / "chain.status").is_file():
        # Каталог стадии был собран под маскированный перезапуск, но по ADR-037
        # длинный прогон не запускался (и каталог снят, см. report["not_launched"]).
        return {
            "run_dir": None, "launched": False, "tmux": None, "docker": None,
            "evidence_of_start": None,
            "why": "ADR-037 п.2/п.4: маска отменена, длинный прогон не запускался; "
                   "возобновление SFT — отдельной дельтой после приёмки нового набора "
                   "(русскоязычные трассы)",
            "prepared_but_not_started": str(run) if run.is_dir() else None,
        }
    man = read_json(run / "run_manifest.json") or {}
    params = read_json(run / "full_sft_params.json") or {}
    trace = read_jsonl(run / "logs" / "loss_trace.jsonl")
    hist = read_jsonl(run / "general_eval_history.jsonl")
    mask_rep = read_json(run / "logs" / "sft_mask_report.json")
    chain_status = (run / "var" / "chain.status")
    stage_status = (run / "var" / "status" / "sft")
    sft_log = (run / "logs" / "sft.log")
    log_lines = sft_log.read_text(encoding="utf-8", errors="replace").splitlines() \
        if sft_log.is_file() else []
    step_lines = [l for l in log_lines if "SFT step" in l]
    return {
        "run_dir": str(run),
        "launched": True,
        "chain_command": (run / "chain_command.txt").read_text().strip()
        if (run / "chain_command.txt").is_file() else None,
        "chain_status": chain_status.read_text().strip() if chain_status.is_file() else None,
        "stage_status": stage_status.read_text().strip() if stage_status.is_file() else None,
        "tmux": ssh(f"tmux ls 2>&1 | grep {run.name} || echo 'нет сессии'"),
        "docker": ssh(f"docker ps --format '{{{{.Names}}}} {{{{.Status}}}}' | grep {run.name} "
                      f"|| echo 'нет контейнера'"),
        "evidence_of_start": {
            "loss_trace": {"file": str(run / "logs" / "loss_trace.jsonl"),
                           "lines": len(trace),
                           "first": trace[0] if trace else None,
                           "last": trace[-1] if trace else None},
            "stage_line": next((l for l in log_lines if "STAGE: SFT" in l), None),
            "loaded_cpt_ckpt": any("Loaded CPT ckpt" in l for l in log_lines),
            "mask_report_line": next((l for l in log_lines if "MASK REPORT" in l), None),
            "mask_report_file": mask_rep,
            "monitor_points": len(hist),
            "monitor_first": hist[0] if hist else None,
            "monitor_last": hist[-1] if hist else None,
            "sft_step_lines": len(step_lines),
            "sft_log_tail": log_lines[-6:],
            "note": settle_note,
        },
        "params": {
            "loss_mask": params.get("loss_mask"),
            "input_checkpoint": params.get("input_checkpoint"),
            "config": params.get("config"),
            "monitor": params.get("monitor"),
            "data": params.get("data"),
        },
        "manifest": {"pipeline_sha256": man.get("pipeline_sha256"),
                     "seed": man.get("seed"),
                     "hyperparameters": man.get("hyperparameters")},
    }


# ─────────────────────────── план замеров ───────────────────────────

def probe_plan(run: Path, stopped: dict) -> dict:
    #: План замеров адресован БУДУЩЕЙ стадии (ADR-037: возобновление после приёмки
    #: нового набора), а не снятому каталогу под маску: пути даны шаблоном.
    ck = run / "checkpoints"
    return {
        "addressed_to": "стадия SFT после приёмки набора с русскоязычными трассами "
                        "(ADR-037 п.2-5); каталог под маскированный перезапуск снят",
        "why": "ADR-035: PPL слеп к языку ответа (0.12 против 0.89 базы), поэтому стадия "
               "судится не только по PPL. Замеры проб — на точках и на финале.",
        "generative_probes": {
            "tool": "tools/probe_control.py (протокол S3ab: 5 промптов, chat template, "
                    "greedy, max_new_tokens=384; метрики degenerate_metrics)",
            "states": {
                "baseline_cpt": f"{CTR_SHARED}/full-cpt-20260916-2149/checkpoints/checkpoint_final.pt",
                "stopped_run": f"{CTR_SHARED}/{stopped.get('run_dir', '').split('/')[-1]}/"
                               "checkpoints/sft_checkpoint_3200.pt",
                "new_run_points": f"{CTR_SHARED}/sft-<ts>/checkpoints/sft_probe_*.pt "
                                  "(каждые 500 шагов, ретенция 24)",
                "new_run_final": f"{CTR_SHARED}/sft-<ts>/checkpoints/sft_checkpoint_final.pt",
            },
            "command": "python3 tools/probe_control.py --skip-base --ckpt "
                       "sft500=/home/user/gb10-shared/sft-<ts>/checkpoints/sft_probe_500.pt "
                       "--out runs/probes-sft-<ts>.json",
            "watch": "кириллица в свободной генерации (цель — рост против 0.12 финала CPT) "
                     "и режим (<think>/<tool_call>)",
            "where": "4080, inference-only; стенд GB10 не задействован (AD-5: одна нагрузка)",
        },
        "agentic_share": {
            "guard": "tools/check_sft_agentic_share.py (C-022; база 45.1 % из "
                     "evidence/s3aa-agentic-cpt.json)",
            "instrument": "tools/passrate_probe.py --no-toolcall-force --no-hint",
            "command": "python3 tools/passrate_probe.py --checkpoint "
                       "/home/user/gb10-shared/sft-<ts>/checkpoints/sft_checkpoint_final.pt "
                       "--pool ... --no-toolcall-force --no-hint --out runs/passrate-agentic-sft/ "
                       f"&& python3 tools/check_sft_agentic_share.py --report "
                       "evidence/s3aa-agentic-sft.json "
                       "--base-report evidence/s3aa-agentic-cpt.json",
            "why_masked_actions": "маска <think> не трогает спаны <tool_call> именно потому, "
                                  "что иначе регрессировала бы компонента (в1) критерия "
                                  "ADR-033 — замер инструментом это и проверяет",
        },
        "ppl_monitor": {
            "sets": "K1 general_eval_v3, K2 general_eval_k2, DOMAIN domain_eval_v2",
            "every_steps": 50, "history": "/home/user/gb10-shared/sft-<ts>/general_eval_history.jsonl",
            "note": "ADR-033 п.3: частота 50 оставлена на стадии; смена частоты — решение "
                    "после отчёта (цена измерена: +10.5 ч на 100)",
        },
        "checkpoints_planned": [
            "sft_probe_{500,1000,…}.pt — точки PPL и проб (keep_last=24)",
            "sft_checkpoint_{200,400,…}.pt — штатная ретенция (keep_last=2)",
            "sft_checkpoint_final.pt — артефакт стадии (stages.tsv)",
        ],
        "existing_points": sorted(p.name for p in ck.glob("*.pt")) if ck.is_dir() else [],
    }


def how_to_fetch(run: Path) -> list[str]:
    r = SHARED / run.name
    return [
        f"ssh {STAND} 'tail -20 {r}/logs/sft.log'  — ход стадии",
        f"ssh {STAND} 'tail -5 {r}/logs/loss_trace.jsonl'  — траектория по шагам (ADR-022 п.4)",
        f"ssh {STAND} 'tail -3 {r}/general_eval_history.jsonl'  — точки монитора K1/K2/домен",
        f"ssh {STAND} 'cat {r}/var/status/sft {r}/var/chain.status'  — статусы стадии и цепочки",
        f"ssh {STAND} 'ls -la {r}/checkpoints/ | tail'  — чекпойнты и точки",
        f"ssh {STAND} 'cat {r}/var/STOPPED'  — маркер остановки предыдущего прогона",
        f"tmux attach -t {run.name}  (на стенде) — живая сессия цепочки",
        f"{SHARED}/sft-20260916-2246/ — остановленный прогон (артефакт «SFT без маски think»), "
        "STOP-RECORD.json и run_manifest.json с пометкой",
    ]


#: Приборы, перенесённые в кейс из ветки S3ab (там они и снимались) — с blob-хешами,
#: иначе «инструмент тот же» проверить нельзя (так же переносил probe_control S3ab).
PORTED_TOOLS = {
    "source_branch": "arch/laguna-eval-set",
    "source_commit": "3499d084c41a274f58435511559701aff2b3b1d6 (S3ab)",
    "files": {
        "tools/probe_control.py": "973fdfb275da2cb80af62a0116f87b51d94149fd",
        "tools/passrate_probe.py": "228aa92a6c00c94a7d9af0ed47edb30ab801c2cd",
        "tools/check_sft_agentic_share.py": "105ae8a2a21a7e789d831620bae29a2bb6f384da",
    },
    "why": "ADR-035 п.7 и ADR-037 п.7 требуют мерить стадию тем же протоколом (пробы "
           "S3ab) и тем же прибором agentic-доли; те же файлы лежат в ветке приёмки "
           "наборов — перенос без правок, сверка по blob",
}


#: Объём рассуждений в SFT-наборе — ЦИТАТА решения ADR-037 (замер владельца по
#: набору целиком). Здесь она хранится как ссылка, а не как вычисление: числа
#: принадлежат решению, а независимое подтверждение идёт ниже — счётом по ТОКЕНАМ.
REASONING_VOLUME_ADR037 = {
    "source": "ADR-037 (замер по sft_train_v12.jsonl: 44 949 примеров, 464 МБ)",
    "assistant_answer_chars": 303.0e6,
    "chars_inside_think": 139.8e6,
    "share_of_answer_chars": 0.461,
    "examples": 44949,
    "examples_with_think": 38716,
    "share_of_examples": 0.861,
    "median_reasoning_share_in_example": 0.474,
    "cyrillic_inside_think": 0.067,
}


def reasoning_cost(patch: dict) -> dict:
    """Независимое подтверждение цены маски — счётом по токенам, а не по символам.

    Тот же вопрос («сколько сигнала снимает маска»), другой прибор: символы против
    целевых токенов. Согласие двух приборов и есть основание отчёта.
    """
    before, after = patch.get("target_tokens_before"), patch.get("target_tokens_after")
    return {
        **REASONING_VOLUME_ADR037,
        "token_level": {
            "source": "tools/verify_think_mask.py (тот же образ, что стадия; "
                      "SFTDataset берётся из пропатченной копии)",
            "samples": (patch.get("think_mask_verified") or {}).get("samples"),
            "targets_before": before,
            "targets_after": after,
            "share_masked": patch.get("masked_share"),
            "tool_call_kept": (patch.get("think_mask_verified") or {}).get("tool_call_kept"),
            "answer_kept": (patch.get("think_mask_verified") or {}).get("answer_kept"),
            "language": (patch.get("think_mask_verified") or {}).get("language"),
        },
        "note": "два прибора согласны: маска снимала бы ~40-46 % обучающего сигнала — "
                "это не точечная правка, а вырезание навыка рассуждения (ADR-037)",
    }


def build(run_name: str, stopped: str, mask_test: str | None, prepared: str | None,
          smoke_fallback: str | None = None) -> dict:
    run, stop_run = SHARED / run_name, SHARED / stopped
    smoke = None
    if mask_test:
        smoke = read_json(SHARED / mask_test / "logs" / "mask_smoke_report.json")
    if smoke is None and smoke_fallback:
        # Отчёт короткого теста писался версией инструмента, которая ещё не выкладывала
        # его на стенд, — берём его вывод как есть и это называем.
        smoke = read_json(Path(smoke_fallback))
        if smoke is not None:
            smoke["report_origin"] = (f"локальный вывод tools/run_mask_smoke.py "
                                      f"({Path(smoke_fallback)}): версия инструмента "
                                      f"на момент прогона не писала отчёт на стенд")
    verify = read_json(SHARED / mask_test / "logs" / "mask_verify.json") if mask_test else None
    st = stopped_part(stop_run)
    patch = patch_part(run, verify, smoke)
    return {
        "tool": "tools/s3ae_sft_remask_evidence.py",
        "stage": "S3ae",
        "adr": ["ADR-037 (отказ от маски: лечим данные)", "ADR-036 (маскирование — отменено в п.1)",
                "ADR-016 (протокол вмешательства)", "ADR-033 (критерий стадии)",
                "ADR-035 (генеративная компонента)"],
        "built_at": datetime.now(timezone.utc).isoformat(),
        "status": "partial",
        "status_note": "остановка стадии выполнена и её артефакты целы; маска НЕ применена "
                       "(ADR-037); длинный прогон не запускался — возобновление SFT отдельной "
                       "дельтой после приёмки нового набора",
        "decision": {
            "superseded_by": "ADR-037",
            "what_changed": "маскирование содержимого <think> в лоссе SFT отменено; "
                            "лечение — русскоязычные трассы рассуждений (данные, не лосс)",
            "why": "рассуждение это 46.1 % символов ответов и 86.1 % примеров; корень "
                   "(язык входа в рассуждение) маска не лечит",
            "effect_on_this_delta": "пункты «патч + перезапуск» сняты; остались остановка "
                                    "стадии и сохранённые артефакты; инструменты патча и "
                                    "проверки сохранены как готовые и не применялись",
        },
        "ported_tools": PORTED_TOOLS,
        "artifacts": {
            "stopped_run": str(stop_run),
            "prepared_not_started_run": prepared,
            "mask_test_dir": str(SHARED / mask_test) if mask_test else None,
            "patch_tool": "tools/patch_pipeline_sft.py",
            "verifier": "tools/verify_think_mask.py",
            "smoke_runner": "tools/run_mask_smoke.py",
            "stopper": "tools/stop_stage_run.py",
            "launcher": "tools/launch_sft_stage.py",
            "adr": "docs/adr/ADR-037-otkaz-ot-maskirovaniya-think-lechim-dannye-russkoyazychnye-trassy-a-ne-loss.md",
        },
        "stopped": st,
        "patch": patch,
        "reasoning_volume": reasoning_cost(patch),
        "launched": {"run_dir": None, "launched": False, "tmux": None, "evidence_of_start": None,
                     "why": "ADR-037: длинный прогон не запускался; возобновление SFT — "
                            "отдельной дельтой (S3af) после приёмки нового набора",
                     "prepared_then_removed": prepared},
        "probe_plan": probe_plan(stopped_run_placeholder(stop_run), st),
        "how_to_fetch": how_to_fetch(stop_run),
        "open_questions": [
            "Стенд GB10 простаивает после остановки: длинный прогон не запускается по "
            "ADR-037. Наблюдение на момент отчёта: смежная дельта S3af (пилот русскоязычных "
            "трасс) идёт в другом рабочем дереве и работает на платформенных моделях "
            "(llm-platform/ollama), то есть стенда не занимает. Вопрос к архитектору: простой "
            "ожидаем до приёмки набора (тогда это норма) или стенд следует занять другим узлом "
            "ревизии (ADR-012: окно до 24.09)?",
            "Порог доли русскоязычных начал рассуждения (ADR-037 п.4) проверяется пилотным "
            "обучением — какой прогон считать пилотным и на какой длине?",
            "Инструменты маски сохранены; есть ли конфигурация, где маска оправдана "
            "(например, при большом объёме русскоязычных трасс — как вспомогательная мера "
            "по ADR-037, вариант C)?",
        ],
        "conflicts_with_prior_decisions": [
            "Задание S3ae требовало «патч маски + перезапуск с маской» (ADR-036 п.1). "
            "ADR-037 (коммит f241658, 17.09.2026) отменяет маскирование: пункты патча и "
            "запуска НЕ выполнены — не по сбою, а по решению. Остановка стадии выполнена "
            "и ADR-037 опирается на её артефакты.",
        ],
    }


def stopped_run_placeholder(run: Path) -> Path:
    """Каталог, по которому строится план замеров: остановленный прогон (создан на стенде)."""
    return SHARED / "sft-20260916-2246"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="sft-20260917-0905",
                    help="каталог стадии, собранный под маску (длинный прогон не запускался)")
    ap.add_argument("--stopped-run", default="sft-20260916-2246")
    ap.add_argument("--mask-test", default="sft-masktest-20260917-0925")
    ap.add_argument("--smoke-report", default="/tmp/mask_smoke2.json",
                    help="вывод короткого теста, если на стенде отчёта нет")
    ap.add_argument("--prepared-inventory", default="/tmp/prepared_inventory.json",
                    help="инвентарь собранного-но-не-запущенного каталога стадии (снят)")
    ap.add_argument("--out", default=str(CASE_ROOT / "evidence/s3ae-sft-remask.json"))
    a = ap.parse_args()
    rep = build(a.run, a.stopped_run, a.mask_test, str(SHARED / a.run), a.smoke_report)
    inv = read_json(Path(a.prepared_inventory)) if a.prepared_inventory else None
    if inv:
        rep["prepared_not_started"] = inv
        # Каталог снят после снятия инвентаря — хеш копии берётся из инвентаря,
        # иначе в отчёте осталось бы `null` вместо доказанного числа.
        pre = (inv.get("files") or {}).get("laguna_pipeline_sft.py", {})
        if rep["patch"]["pipeline"].get("sha256") is None and pre.get("sha256"):
            rep["patch"]["pipeline"]["sha256"] = pre["sha256"]
            rep["patch"]["pipeline"]["sha256_source"] = "инвентарь снятого каталога"
    Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"written": a.out, "status": rep["status"],
                      "stopped_at_step": rep["stopped"]["stopped_at_step"],
                      "launched": rep["launched"]["launched"],
                      "mask_applied": rep["patch"]["applied"],
                      "mask_verdict": rep["patch"]["think_mask_verified"]["verdict"],
                      "targets": [rep["patch"]["target_tokens_before"],
                                  rep["patch"]["target_tokens_after"]],
                      "masked_share": rep["patch"]["masked_share"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
