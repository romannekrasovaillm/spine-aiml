#!/usr/bin/env python3
"""Дельта `sft-lr-sensitivity`: объявленная правка пайплайна SFT под одну руку.

**Зачем патч, а не флаги.** Пайплайн стадии (sha256 `0f37e034…`) задаёт темп SFT
**в коде**: `peak_lr = 1e-5` и `CosineAnnealingLR(T_max=args.max_steps,
eta_min=2e-7)`. Флага темпа в нём нет — и это не случайность: стадия шла одним
объявленным темпом, а смена темпа посреди живой стадии запрещена (ADR-016 п.1).
Рука дельты — **новый прогон из того же CPT-входа**, поэтому темп задаётся не
правкой числа в живом прогоне, а **отдельной копией пайплайна** с двумя гнёздами:

1. `--sft_lr_fixed` — постоянный LR (0 = штатное поведение: пик 1e-5 + косинус).
   Постоянство даётся равенством `eta_min == peak_lr`: у `CosineAnnealingLR` при
   таком равенстве `lr = eta_min + (base - eta_min)·(1+cos(π·t/T))/2 = base`
   **точно**, то есть расписание остаётся штатным классом, а число — постоянным.
   Гнёзд два (пик и eta_min), а не одно: иначе «постоянный LR» был бы только на
   бумаге — косинус увёл бы темп к `2e-7` на 500-м шаге.
2. `--sft_stop_after_step` — остановка после шага N включительно (0 = до
   `max_steps`). Семантика **совпадает с именем чекпойнта**: штатный прогон
   пишет `sft_probe_N.pt` в итерации с `step == N` (после обновления N), поэтому
   «остановиться после N» даёт ровно тот же файл, что штатный прогон на той же
   точке, — без подмены шага.

**Чего патч НЕ делает** (иначе рука перестала бы быть одной переменной):

* не трогает данные, тензор, карточки (AD-7), идентификатор набора, batch, seed,
  `max_len`, `max_steps` — всё это остаётся как у контроля;
* не меняет `warmup_steps` и порядок данных: `max_steps` остаётся 66157, поэтому
  `warmup_steps = min(100, 66157//10) = 100` — как у контроля, и `DataLoader`
  (shuffle без генератора, сид 42) выдаёт **ту же** перестановку;
* не трогает сам прибор (`tools/probe_language_split.py` пиннут хешем);
* при заданном `--sft_stop_after_step` **не пишет `sft_checkpoint_final.pt`** и не
  запускает пробы конца стадии: имя «final» означало бы завершённую стадию, а
  стадия не завершена — артефакт руки это `sft_probe_<N>.pt`.

Правка якорная: каждый якорь обязан встретиться в файле **ровно один раз**, иначе
инструмент отказывает (пайплайн мог измениться — молчаливая правка не по тому
месту была бы хуже отказа). Диффы и хеши до/после пишутся в запись патча рядом с
копиями — по ним правка проверяема без запуска.

Тот же инструмент правит **цепочку** (`pilot_chain.sh`): она — единственный путь
запуска стадии (`pipeline_args` собирает командную строку пайплайна), и без
проброса двух новых флагов копия пайплайна получила бы их значения по умолчанию,
то есть рука молча стала бы контролем. Правка цепочки: две переменные, две строки
разбора флагов и один `printf` в `pipeline_args`.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

#: sha256 источников, на которых якоря проверены. Не «на память»: расхождение —
#: отказ, потому что якорь, поставленный в изменившийся файл, — это правка не туда.
EXPECT_PIPELINE_SHA = "0f37e034ab5410064c8caaed901027055ae965526a2bec0488a0a76f831f1b09"
EXPECT_CHAIN_SHA = "ce7bbecf00a7635b8f1b0338f2ba505e3a141402d43b25d6ef638cad61373643"

# ── якоря пайплайна ──────────────────────────────────────────────────────────
P_ARGS = '''    p.add_argument("--peak_lr_scale", type=float, default=1.0,
                   help="множитель пикового LR WSD в CPT (анти-форгеттинг: <1 — сниженный пик)")
'''
P_ARGS_NEW = P_ARGS + '''    # ДЕЛЬТА sft-lr-sensitivity: два гнезда руки. Умолчания 0 = штатное
    # поведение байт-в-байт (пик 1e-5 и косинус до 2e-7), поэтому копия пайплайна
    # без флагов тождественна источнику по поведению.
    p.add_argument("--sft_lr_fixed", type=float, default=0.0,
                   help="ДЕЛЬТА: постоянный LR SFT (0 — штатный пик 1e-5 и косинус)")
    p.add_argument("--sft_stop_after_step", type=int, default=0,
                   help="ДЕЛЬТА: остановить SFT после шага N включительно (0 — до max_steps)")
'''

P_PEAK = "    peak_lr = 1e-5\n"
P_PEAK_NEW = '''    peak_lr = 1e-5
    # ДЕЛЬТА: темп руки. Меняет ОДНО число — пик; форма расписания остаётся
    # штатной (см. eta_min ниже), поэтому различие с контролем — только масштаб.
    _lr_fixed = float(getattr(args, "sft_lr_fixed", 0.0) or 0.0)
    if _lr_fixed > 0:
        peak_lr = _lr_fixed
        log.info(f"SFT ARM: постоянный LR={peak_lr:.3e} (штатный пик 1e-5 не применяется)")
'''

P_SCHED = ("    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR"
           "(optimizer, T_max=args.max_steps, eta_min=2e-7)\n")
P_SCHED_NEW = '''    # ДЕЛЬТА: при постоянном темпе eta_min = base_lr — косинус вырождается в
    # константу (lr = eta_min + 0·(…)), а класс расписания остаётся штатным.
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=args.max_steps, eta_min=(peak_lr if _lr_fixed > 0 else 2e-7))
'''

P_LOOP = '''    ema = None
    while step < args.max_steps:
        for batch in loader:
            if step >= args.max_steps: break
'''
P_LOOP_NEW = '''    ema = None
    # ДЕЛЬТА: предел руки. 0 = штатный max_steps; N = остановка после шага N
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

P_TAIL = '''    save_checkpoint_atomic(model, optimizer, Path(args.ckpt_dir)/"sft_checkpoint_final.pt")
    log.info("SFT complete")
    run_probes(args, tokenizer, model, "sft")
'''
P_TAIL_NEW = '''    if _stop_after > 0:
        # ДЕЛЬТА: рука не выдаёт себя за завершённую стадию. `sft_checkpoint_final.pt`
        # не пишется (имя означало бы конец SFT), пробы конца стадии не запускаются
        # (они мерили бы состояние, которого стадия не достигла). Артефакт руки —
        # sft_probe_<N>.pt, записанный выше штатным механизмом точек замера.
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

# ── якоря цепочки ────────────────────────────────────────────────────────────
C_VARS = "PEAK_LR_SCALE=0.7\n"
C_VARS_NEW = '''PEAK_LR_SCALE=0.7
#: ДЕЛЬТА sft-lr-sensitivity: гнёзда руки. 0 = штатное поведение обоих флагов.
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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
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
                f"ОТКАЗ: якорь «{name}» в {what} встретился {n} раз(а), а обязан ровно один. "
                f"Файл-источник изменился — правка не выполнена.")
        text = text.replace(old, new, 1)
        applied.append({"anchor": name, "occurrences": 1, "bytes_before": len(old),
                        "bytes_after": len(new)})
    return text, applied


def unified(src: str, dst: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        src.splitlines(keepends=True), dst.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pipeline-src", required=True)
    ap.add_argument("--chain-src", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--lr", type=float, default=2e-6)
    ap.add_argument("--stop-after", type=int, default=500)
    ap.add_argument("--record", default=None)
    #: Объявленная ревизия источника. Умолчание — та, на которой якоря проверены.
    #: Флаг, а не «пропустить проверку»: вызывающий **называет**, какую редакцию он
    #: правит, и расхождение с фактом — отказ. Тесты дельты пользуются им, чтобы
    #: прогнать правку на синтетическом источнике (у него нет и не может быть
    #: штатного хеша), и проверяют, что при неверном хеше инструмент отказывает.
    ap.add_argument("--pipeline-sha", default=EXPECT_PIPELINE_SHA)
    ap.add_argument("--chain-sha", default=EXPECT_CHAIN_SHA)
    a = ap.parse_args()

    psrc, csrc = Path(a.pipeline_src), Path(a.chain_src)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    rec: dict = {
        "tool": "tools/patch_lr_arm.py",
        "delta": "sft-lr-sensitivity",
        "date": datetime.now(timezone.utc).isoformat(),
        "arms": {"lr_fixed": a.lr, "stop_after_step": a.stop_after},
        "sources": {},
        "patched": {},
        "why": ("темп SFT задан в коде пайплайна (peak_lr=1e-5, косинус до 2e-7); "
                "флага темпа нет, а правка живого прогона запрещена (ADR-016 п.1) — "
                "поэтому рука получает отдельную копию пайплайна с двумя гнёздами"),
        "not_touched": ["набор и тензор (AD-7)", "карточки набора", "batch/seed/max_len",
                        "max_steps=66157 (warmup=100 и T_max остаются как у контроля)",
                        "прибор tools/probe_language_split.py (пиннут хешем)"],
    }

    # ── пайплайн ──
    p_sha = sha256_file(psrc)
    p_text = psrc.read_text(encoding="utf-8")
    if a.pipeline_sha and p_sha != a.pipeline_sha:
        raise SystemExit(f"ОТКАЗ: sha256 пайплайна-источника {p_sha} ≠ объявленного "
                         f"{a.pipeline_sha} — якоря проверены на другой редакции")
    p_new, p_applied = apply_edits(p_text, PIPELINE_EDITS, "пайплайне")
    (out / "laguna_pipeline_sft.py").write_text(p_new, encoding="utf-8")
    p_out_sha = sha256_file(out / "laguna_pipeline_sft.py")

    # ── цепочка ──
    c_sha = sha256_file(csrc)
    c_text = csrc.read_text(encoding="utf-8")
    if a.chain_sha and c_sha != a.chain_sha:
        raise SystemExit(f"ОТКАЗ: sha256 цепочки-источника {c_sha} ≠ объявленного "
                         f"{a.chain_sha} — якоря проверены на другой редакции")
    c_new, c_applied = apply_edits(c_text, CHAIN_EDITS, "цепочке")
    (out / "pilot_chain.sh").write_text(c_new, encoding="utf-8")
    (out / "pilot_chain.sh").chmod(0o755)
    c_out_sha = sha256_file(out / "pilot_chain.sh")

    rec["sources"]["pipeline"] = {"path": str(psrc), "sha256": p_sha}
    rec["sources"]["chain"] = {"path": str(csrc), "sha256": c_sha}
    rec["patched"]["pipeline"] = {"path": str(out / "laguna_pipeline_sft.py"),
                                  "sha256": p_out_sha, "edits": p_applied,
                                  "diff": unified(p_text, p_new, "laguna_pipeline_sft.py")}
    rec["patched"]["chain"] = {"path": str(out / "pilot_chain.sh"),
                               "sha256": c_out_sha, "edits": c_applied,
                               "diff": unified(c_text, c_new, "pilot_chain.sh")}

    # ── проверка, что правка не пустая и не сломала синтаксис ──
    import ast
    ast.parse(p_new)
    rec["checks"] = {"pipeline_syntax": "ok", "anchors_all_unique": True,
                     "pipeline_sha_changed": p_out_sha != p_sha,
                     "chain_sha_changed": c_out_sha != c_sha}

    rec_path = Path(a.record) if a.record else out / "pipeline_patch.json"
    rec_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"пайплайн: {p_sha[:12]} → {p_out_sha[:12]}")
    print(f"цепочка:  {c_sha[:12]} → {c_out_sha[:12]}")
    print(f"запись патча: {rec_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
