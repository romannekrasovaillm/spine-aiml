#!/usr/bin/env python3
"""S3ax: рука «набор v13 + пониженный темп» — сборка каталога стадии и её запуск.

**Вопрос дельты.** Дефект «срыва длины» имел две независимые причины, и каждая
починена отдельно: (1) **идентичность входа** — стадия читала v12 (44 949
сэмплов), тогда как объявлен был v13_fixed (44 105), S3av/ADR-051; (2) **темп** —
постоянный LR 2e-6 на первых 500 шагах снимает усечения с 31/104 = 29.8 % до
10/104 = 9.6 %, уровня CPT-базы (S3aw). Комбинация «v13 + LR 2e-6» **не измерена**.
Этот инструмент собирает и запускает ровно её.

**Почему инструмент, а не ручная строка.** Сборка каталога стадии — работа
`tools/launch_sft_stage.py` (S3aa/S3av: чекпойнт-вход ссылкой, патч копии
пайплайна, доставка файлов цепочки, `stages.tsv`, команда запуска). Здесь она
**вызывается как есть** и не переписывается: расхождение двух сборщиков одного
каталога — это дрейф. Инструмент добавляет ровно то, чего в S3av нет:

1. **два гнезда темпа** в копии пайплайна (`--sft_lr_fixed`, `--sft_stop_after_step`)
   и два одноимённых флага в копии цепочки. Правка якорная: каждый якорь обязан
   встретиться РОВНО один раз, иначе отказ. Якоря и тексты вставок — те же, что в
   инструменте дельты `sft-lr-sensitivity` (`tools/patch_lr_arm.py`, sha256
   `6a5b1f9c…` в `PATCH_LR_ARM_SHA`), применённые к редакции ПОСЛЕ S3av: тот
   инструмент проверял якоря на редакции до S3av и на этой файл бы не принял.
   Копии определения не заводится там, где её можно не заводить: гнёзд два, и оба
   названы здесь текстом, потому что чужой инструмент не поставляется в каталог
   прогона (ADR-023 п.12: пропуск видимый, а не молчаливый).

2. **артефакт стадии** в `stages.tsv` — `checkpoints/sft_probe_<N>.pt`, а не
   `sft_checkpoint_final.pt`: рука остановлена на шаге N, и имя «final» означало бы
   завершённую стадию, которой нет.

3. **обвязка стражем CUDA-прогона** (`guard/guard_cuda_run.sh`): сон площадки во
   время прогона уничтожает контекст CUDA (Xid 31, S3 decode-diagnosis). Страж и
   его чекер доставляются копиями с записью sha256; источник — копия, которой
   охранялся прогон СРАВНЕНИЯ (`sft-lr2e6-20260921-1453/guard/`), чтобы у трёх
   состояний была одна обвязка, а не три похожих.

Чего инструмент НЕ делает: не меняет данные, тензоры, карточки (AD-7) и вход
стадии; не трогает `batch`/`seed`/`max_len`/`max_steps`; не правит живой прогон
(ADR-016 п.1); не занимает локальную 4080 — стадия идёт на стенде.

Запуск::

    python3 tools/launch_v13_lr_arm.py --ts v13lr2e6-20260921-1730 --do build
    python3 tools/launch_v13_lr_arm.py --ts v13lr2e6-20260921-1730 --do launch
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE_ROOT / "tools"))

import launch_sft_stage as L  # noqa: E402  — сборщик каталога стадии (S3av)

#: Редакция инструмента дельты темпа, у которой взяты якоря и тексты вставок.
#: Не «на память»: расхождение — сигнал, что гнёзда разошлись, и это видно в
#: записи патча, а не выясняется задним числом.
PATCH_LR_ARM_SHA = "509dea27b509bb9d2ee2ce2a23b497dd43aa3d4a6856a3433d051ea38ba32c5a"
#: Копии обвязки сна: прогон сравнения (S3aw). Из него же взяты имена файлов.
GUARD_SRC = Path("/home/user/gb10-shared/sft-lr2e6-20260921-1453/guard")
GUARD_FILES = ("guard_cuda_run.sh", "check_gpu_sleep_guard.py")

# ── гнёзда темпа: пайплайн ───────────────────────────────────────────────────
#: Гнездо 1 — параметры. Умолчания 0 = штатное поведение байт-в-байт.
P_ARGS = '''    p.add_argument("--peak_lr_scale", type=float, default=1.0,
                   help="множитель пикового LR WSD в CPT (анти-форгеттинг: <1 — сниженный пик)")
'''
P_ARGS_NEW = P_ARGS + '''    # S3ax: два гнезда руки. Умолчания 0 = штатное поведение байт-в-байт
    # (пик 1e-5 и косинус до 2e-7), поэтому копия без флагов тождественна по поведению.
    p.add_argument("--sft_lr_fixed", type=float, default=0.0,
                   help="S3ax: постоянный LR SFT (0 — штатный пик 1e-5 и косинус)")
    p.add_argument("--sft_stop_after_step", type=int, default=0,
                   help="S3ax: остановить SFT после шага N включительно (0 — до max_steps)")
'''

#: Гнездо 2 — сам темп. Меняется ОДНО число (пик); форма расписания остаётся
#: штатной, поэтому различие с контролем — только масштаб.
P_PEAK = "    peak_lr = 1e-5\n"
P_PEAK_NEW = '''    peak_lr = 1e-5
    # S3ax: темп руки.
    _lr_fixed = float(getattr(args, "sft_lr_fixed", 0.0) or 0.0)
    if _lr_fixed > 0:
        peak_lr = _lr_fixed
        log.info(f"SFT ARM: постоянный LR={peak_lr:.3e} (штатный пик 1e-5 не применяется)")
'''

#: Гнездо 3 — расписание: при постоянном темпе eta_min = base_lr, и косинус
#: вырождается в константу (lr = eta_min + 0·(…)) при штатном классе расписания.
P_SCHED = ("    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR"
           "(optimizer, T_max=args.max_steps, eta_min=2e-7)\n")
P_SCHED_NEW = '''    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=args.max_steps, eta_min=(peak_lr if _lr_fixed > 0 else 2e-7))
'''

#: Гнездо 4 — предел шага: N = остановка после шага N включительно, то есть ровно
#: та итерация, в которой штатный прогон пишет `sft_probe_N.pt`.
P_LOOP = '''    ema = None
    while step < args.max_steps:
        for batch in loader:
            if step >= args.max_steps: break
'''
P_LOOP_NEW = '''    ema = None
    # S3ax: предел руки. 0 = штатный max_steps; N = остановка после шага N
    # включительно (ровно та итерация, в которой штатный прогон пишет probe_N).
    _stop_after = int(getattr(args, "sft_stop_after_step", 0) or 0)
    _step_limit = min(args.max_steps, _stop_after + 1) if _stop_after > 0 else args.max_steps
    if _stop_after > 0:
        log.info(f"SFT ARM: предел шага {_stop_after} (max_steps={args.max_steps} остаётся "
                 f"для warmup={min(100, args.max_steps // 10)} и T_max расписания)")
    while step < _step_limit:
        for batch in loader:
            if step >= _step_limit: break
'''

#: Гнездо 5 — хвост стадии: рука не выдаёт себя за завершённую.
P_TAIL = '''    save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/"sft_checkpoint_final.pt")
    log.info("SFT complete")
    run_probes(args, tokenizer, model, "sft")
'''
P_TAIL_NEW = '''    if _stop_after > 0:
        # S3ax: `sft_checkpoint_final.pt` не пишется (имя означало бы конец SFT),
        # пробы конца стадии не запускаются (они мерили бы недостигнутое состояние).
        log.info(f"SFT ARM STOP: рука остановлена после шага {_stop_after}; "
                 f"sft_checkpoint_final.pt не пишется, пробы конца стадии не запускаются")
    else:
        save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/"sft_checkpoint_final.pt")
        log.info("SFT complete")
        run_probes(args, tokenizer, model, "sft")
'''

PIPELINE_EDITS = [
    ("args", P_ARGS, P_ARGS_NEW),
    ("peak_lr", P_PEAK, P_PEAK_NEW),
    ("scheduler", P_SCHED, P_SCHED_NEW),
    ("loop", P_LOOP, P_LOOP_NEW),
    ("tail", P_TAIL, P_TAIL_NEW),
]

# ── гнёзда темпа: цепочка ────────────────────────────────────────────────────
#: Без проброса флагов копия пайплайна получила бы умолчания, то есть рука молча
#: стала бы контролем — ровно тот класс дефекта, который чинит S3av (п.7 шапки).
C_VARS = "PEAK_LR_SCALE=0.7\n"
C_VARS_NEW = '''PEAK_LR_SCALE=0.7
#: S3ax: гнёзда руки темпа. 0 = штатное поведение обоих флагов.
SFT_LR_FIXED=0
SFT_STOP_AFTER=0
'''

C_CASE = '    --peak-lr-scale)      PEAK_LR_SCALE="${2:?}"; shift 2 ;;\n'
C_CASE_NEW = C_CASE + '''    --sft-lr-fixed)       SFT_LR_FIXED="${2:?}"; shift 2 ;;
    --sft-stop-after)     SFT_STOP_AFTER="${2:?}"; shift 2 ;;
'''

C_ARGS = ("  printf -- ' --rl_steps %s --seed %s --peak_lr_scale %s'"
          " \"$RL_STEPS\" \"$SEED\" \"$PEAK_LR_SCALE\"\n")
C_ARGS_NEW = C_ARGS + '''  [ "$SFT_LR_FIXED" != "0" ] && printf -- ' --sft_lr_fixed %s' "$SFT_LR_FIXED"
  [ "$SFT_STOP_AFTER" != "0" ] && printf -- ' --sft_stop_after_step %s' "$SFT_STOP_AFTER"
'''

CHAIN_EDITS = [
    ("vars", C_VARS, C_VARS_NEW),
    ("flags", C_CASE, C_CASE_NEW),
    ("pipeline_args", C_ARGS, C_ARGS_NEW),
]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_edits(text: str, edits, what: str) -> tuple[str, list[dict]]:
    """Якорная правка: каждый якорь обязан встретиться ровно один раз."""
    applied = []
    for name, old, new in edits:
        n = text.count(old)
        if n != 1:
            raise SystemExit(
                f"ОТКАЗ: якорь «{name}» в {what} встретился {n} раз(а), а обязан ровно "
                f"один. Источник изменился — правка не выполнена.")
        text = text.replace(old, new, 1)
        applied.append({"anchor": name, "occurrences": 1,
                        "bytes_before": len(old), "bytes_after": len(new)})
    return text, applied


def build(ts: str, lr: float, stop_after: int, why: str | None = None) -> dict:
    # 1. Каталог стадии — штатной сборкой S3av (набор выбирается ИМЕНЕМ).
    d = L.build(ts, "prefix_only", None, "v13")
    run_dir: Path = d["run_dir"]
    rec: dict = {
        "tool": "tools/launch_v13_lr_arm.py",
        "node": "S3ax",
        "date": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "arm": {"lr_fixed": lr, "stop_after_step": stop_after},
        "why": why or (f"рука S3ax: набор v13_fixed + постоянный LR {lr:g} на первых "
                       f"{stop_after} шагах из того же CPT-входа, что контроль и база"),
        "base_builder": {"tool": "tools/launch_sft_stage.py",
                         "sha256": sha256_file(CASE_ROOT / "tools/launch_sft_stage.py"),
                         "dataset": d["dataset"], "steps": d["steps"]},
        "anchors_source": {"tool": "tools/patch_lr_arm.py (дельта sft-lr-sensitivity)",
                           "sha12": PATCH_LR_ARM_SHA,
                           "why": "якоря и тексты вставок взяты у неё; на редакции после "
                                  "S3av она бы отказала (её EXPECT_*_SHA — до S3av)"},
        "not_touched": ["набор, тензор, карточки (AD-7)", "вход стадии (CPT-финал)",
                        "batch/seed/max_len/max_steps", "warmup и порядок данных",
                        "прибор tools/probe_language_split.py (пиннут хешем)"],
        "edits": {},
    }

    # 2. Гнёзда темпа: пайплайн.
    pipe = run_dir / "laguna_pipeline_sft.py"
    p_before = sha256_file(pipe)
    p_text = pipe.read_text(encoding="utf-8")
    p_new, p_applied = apply_edits(p_text, PIPELINE_EDITS, "копии пайплайна")
    pipe.write_text(p_new, encoding="utf-8")
    import ast
    ast.parse(p_new)                                    # синтаксис — до запуска
    rec["edits"]["pipeline"] = {"path": str(pipe), "sha256_before": p_before,
                                "sha256_after": sha256_file(pipe), "anchors": p_applied}

    # 3. Гнёзда темпа: цепочка.
    chain = run_dir / "pilot_chain.sh"
    c_before = sha256_file(chain)
    c_text = chain.read_text(encoding="utf-8")
    c_new, c_applied = apply_edits(c_text, CHAIN_EDITS, "копии цепочки")
    chain.write_text(c_new, encoding="utf-8")
    chain.chmod(0o755)
    rec["edits"]["chain"] = {"path": str(chain), "sha256_before": c_before,
                             "sha256_after": sha256_file(chain), "anchors": c_applied}

    # 4. Обвязка сна — копиями того же стража, что у прогона сравнения.
    guard_dir = run_dir / "guard"
    guard_dir.mkdir(exist_ok=True)
    guard_ship = {}
    for name in GUARD_FILES:
        src = GUARD_SRC / name
        if not src.is_file():
            raise SystemExit(f"ОТКАЗ: нет стража сна {src} — обвязка без него не ставится")
        body = src.read_bytes()
        (guard_dir / name).write_bytes(body)
        guard_ship[name] = {"source": str(src), "sha256": hashlib.sha256(body).hexdigest()}
    (guard_dir / "guard_cuda_run.sh").chmod(0o755)
    rec["guard"] = guard_ship

    # 5. Артефакт стадии — точка руки, а не «финал» (рука остановлена).
    stages = run_dir / "stages.tsv"
    fields = stages.read_text(encoding="utf-8").strip().split("\t")
    if len(fields) != 8:
        raise SystemExit(f"ОТКАЗ: stages.tsv не в формате 8 полей: {fields!r}")
    artifact_before = fields[3]
    fields[3] = f"checkpoints/sft_probe_{stop_after}.pt"
    stages.write_text("\t".join(fields) + "\n", encoding="utf-8")
    rec["stages_tsv"] = {"path": str(stages), "artifact_before": artifact_before,
                         "artifact_after": fields[3],
                         "why": "рука остановлена на шаге — «final» означал бы конец стадии"}

    # 6. Команда запуска: обвязка стражем + два флага руки.
    orig_chain = d["chain_cmd"]
    arm_chain = (f"bash {guard_dir}/guard_cuda_run.sh --out {run_dir}/guard.json "
                 f"--why {shlex.quote(rec['why'])} -- {orig_chain}"
                 f" --sft-lr-fixed {lr:g} --sft-stop-after {stop_after}")
    (run_dir / "chain_command.txt").write_text(arm_chain + "\n", encoding="utf-8")
    launch_cmd = (f"ssh {L.STAND} 'tmux kill-session -t {d['name']} 2>/dev/null; "
                  f"tmux new-session -d -s {d['name']} \"setsid nohup {arm_chain} "
                  f">> {run_dir}/chain.log 2>&1 < /dev/null\"'")
    (run_dir / "launch_command.txt").write_text(launch_cmd + "\n", encoding="utf-8")
    rec["chain_command"] = {"path": str(run_dir / "chain_command.txt"),
                            "builder_command": orig_chain, "arm_command": arm_chain,
                            "flags_appended": [f"--sft-lr-fixed {lr:g}",
                                               f"--sft-stop-after {stop_after}"]}

    # 7. `ship.json` — хеши фактически доставленного. Порядок доставки был: сборка →
    #    правка темпа → страж, поэтому запись пересчитывается ПОСЛЕ всех правок:
    #    иначе она называла бы файлы, которых в каталоге уже нет.
    ship_path = run_dir / "ship.json"
    ship = json.loads(ship_path.read_text(encoding="utf-8"))
    ship["sha256"]["pilot_chain.sh"] = sha256_file(chain)
    ship["sha256"]["laguna_pipeline_sft.py"] = sha256_file(pipe)
    ship["sha256"]["guard/guard_cuda_run.sh"] = guard_ship["guard_cuda_run.sh"]["sha256"]
    ship["sha256"]["guard/check_gpu_sleep_guard.py"] = guard_ship["check_gpu_sleep_guard.py"]["sha256"]
    ship["arm_patch"] = {
        "tool": rec["tool"], "lr_fixed": lr, "stop_after_step": stop_after,
        "why": ("sha256 цепочки и пайплайна пересчитаны после правки темпа: запись "
                "называет файлы, лежащие в каталоге, а не собранные сборщиком"),
        "stages_tsv_artifact": fields[3],
    }
    ship_path.write_text(json.dumps(ship, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    rec["ship"] = ship["sha256"]

    (run_dir / "arm_patch.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    #: Паспорт стадии дополняется фактическим темпом: `full_sft_params.json` собран
    #: сборщиком и о руке темпа не знает — без этой записи прогон описывал бы себя
    #: штатным расписанием 1e-5 (класс дефекта AD-2: декларация ≠ факт).
    params_path = run_dir / "full_sft_params.json"
    params = json.loads(params_path.read_text(encoding="utf-8"))
    params["arm_tempo"] = {
        "lr_fixed": lr, "stop_after_step": stop_after,
        "lr_source": "S3ax: peak_lr и eta_min расписания заменены постоянным LR "
                     "(см. arm_patch.json, гнёзда peak_lr/scheduler)",
        "schedule_note": "класс расписания штатный (CosineAnnealingLR); при eta_min == base_lr "
                         "он вырождается в константу",
        "artifact": f"checkpoints/sft_probe_{stop_after}.pt",
        "declared_lr_control": params["config"]["lr"],
    }
    params_path.write_text(json.dumps(params, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return rec


def launch(ts: str) -> dict:
    """Запуск отсоединённо: tmux + setsid + nohup — цепочка переживает агента."""
    d = L.stage_dirs(ts)
    cmd = (d["run_dir"] / "chain_command.txt").read_text(encoding="utf-8").strip()
    inner = (f"tmux kill-session -t {d['name']} 2>/dev/null; "
             f"tmux new-session -d -s {d['name']} \"setsid nohup {cmd} "
             f">> {d['run_dir']}/chain.log 2>&1 < /dev/null\"")
    rc, out, err = L.ssh(inner, timeout=120)
    return {"rc": rc, "stdout": out, "stderr": err, "launched_command": cmd,
            "run_dir": str(d["run_dir"])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ts", required=True, help="метка прогона; каталог — sft-<ts>")
    ap.add_argument("--do", required=True, choices=["build", "launch", "facts"])
    ap.add_argument("--lr", type=float, default=2e-6)
    ap.add_argument("--stop-after", type=int, default=500)
    ap.add_argument("--settle", type=int, default=60, help="пауза перед фактами старта")
    a = ap.parse_args()

    if a.do == "build":
        rec = build(a.ts, a.lr, a.stop_after)
        print(json.dumps({"run_dir": rec["run_dir"], "arm": rec["arm"],
                          "pipeline_sha_after": rec["edits"]["pipeline"]["sha256_after"],
                          "chain_sha_after": rec["edits"]["chain"]["sha256_after"],
                          "artifact": rec["stages_tsv"]["artifact_after"],
                          "guard": {k: v["sha256"] for k, v in rec["guard"].items()}},
                         ensure_ascii=False, indent=2))
    elif a.do == "launch":
        print(json.dumps(launch(a.ts), ensure_ascii=False, indent=2))
    else:
        d = L.stage_dirs(a.ts)
        f = L.facts(a.ts, a.settle)
        print(json.dumps({"run_dir": str(d["run_dir"]), **f}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
