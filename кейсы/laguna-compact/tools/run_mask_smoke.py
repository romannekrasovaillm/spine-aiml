#!/usr/bin/env python3
"""S3ae: короткий тест маски `<think>` на стенде — условие запуска длинной стадии.

> **Статус: тест пройден, длинная стадия по ADR-037 не запускалась.** Тест окупился
> дважды: (а) нашёл реальный отказ — маска получала 2-D батч валидации и падала на
> шаге 0 (`int(ids[i])` на строке тензора), чего проверка меток увидеть не могла;
> (б) дал цену маски на живом прогоне: 41.7 % целевых токенов на 512 сэмплах,
> откатов к базовой маске 0.

Зачем тест на стенде, если есть проверка меток. `tools/verify_think_mask.py`
доказывает **правило** (что именно обнулено) на кэше токенов, но не доказывает,
что пропатченная копия **исполняется**: что `_sft_mask_report` печатает числа, что
лосс считается (не `NaN`), что валидация с той же маской не падает, что чекпойнт
пишется. Это разные классы отказа, и второй стоит 68 часов прогона. Поэтому до
длинного старта патч прогоняется **тем же образом и в том же образе**, что стадия,
но на 12 шагах; при этом проверка меток идёт первым шагом в том же контейнере —
с живым токенайзером, то есть с замером языка снятого (ADR-036: снято должно быть
англоязычное рассуждение).

Тест идёт в **отдельном каталоге** (`sft-masktest-<ts>`), а не в каталоге стадии:
`run_sft` резюмирует с последнего `sft_checkpoint_N.pt`, и тестовый чекпойнт на
шаге 12 стал бы точкой возобновления настоящей стадии. Это не стилистика, а
механика (`laguna_pipeline_v8.py`, блок SFT RESUME).

Вход стадии кладётся **жёсткой ссылкой** на CPT-чекпойнт: тот же путь, что у
стадии (`<ckpt_dir>/checkpoint_final.pt`), ноль байт дублирования (AD-4).

Коды возврата::

    0 — тест пройден (инварианты меток PASS, лосс конечен, отчёт маски есть)
    1 — тест не пройден (числа печатаются)
    2 — NOT-VERIFIED: нет копии пайплайна/каталога прогона
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
SHARED = Path("/home/user/gb10-shared")
CTR_SHARED = "/workspace/shared"
STAND = "gb10-fast"
IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"
CPT_CKPT = SHARED / "full-cpt-20260916-2149" / "checkpoints" / "checkpoint_final.pt"
#: Немаскирующая копия — артефакт остановленного прогона (ADR-036): по ней
#: проверяется, что выключенный переключатель даёт ровно прежнюю редакцию меток.
BASELINE_PIPELINE = SHARED / "sft-20260916-2246" / "laguna_pipeline_sft.py"


def ssh(cmd: str, timeout: int = 900) -> tuple[int, str, str]:
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="каталог стадии (её копия пайплайна и проверяются)")
    ap.add_argument("--ts", default=datetime.now().strftime("%Y%m%d-%H%M"))
    ap.add_argument("--steps", type=int, default=12)
    ap.add_argument("--samples", type=int, default=2000)
    ap.add_argument("--out", help="куда записать отчёт (json)")
    ap.add_argument("--smoke-log", default=None,
                    help="evaluate: путь к сохранённому логу короткого прогона")
    ap.add_argument("--keep-checkpoint", action="store_true",
                    help="не удалять тестовый чекпойнт (2.9 ГБ) после теста")
    ap.add_argument("--do", choices=["all", "verify", "smoke", "evaluate"], default="all",
                    help="verify — только инварианты меток (быстро, без обучения); "
                         "smoke — только короткий прогон")
    a = ap.parse_args()

    run_dir = Path(a.run)
    pipe = run_dir / "laguna_pipeline_sft.py"
    if not pipe.is_file():
        print(json.dumps({"verdict": "NOT-VERIFIED", "why": "нет копии пайплайна стадии",
                          "pipeline": str(pipe)}, ensure_ascii=False, indent=2))
        return 2
    test_dir = SHARED / f"sft-masktest-{a.ts}"
    do_verify, do_smoke = a.do in ("all", "verify"), a.do in ("all", "smoke")
    do_eval = a.do == "evaluate"
    ctr_test = f"{CTR_SHARED}/sft-masktest-{a.ts}"
    ctr_run = f"{CTR_SHARED}/{run_dir.name}"

    # ── каталог теста: вход стадии ссылкой + место под чекпойнты и логи ───────
    # Вход стадии — ссылкой, если ФС это позволяет; иначе копией (на gb10-shared
    # hardlink запрещён — та же причина, что в ship.json прогонов: AD-4 требует
    # не дублировать 2.9 ГБ без нужды, но путь входа обязан существовать).
    rc, out, err = ssh(
        f"mkdir -p {shlex.quote(str(test_dir))}/checkpoints {shlex.quote(str(test_dir))}/logs && "
        f"(ln {shlex.quote(str(CPT_CKPT))} "
        f"{shlex.quote(str(test_dir))}/checkpoints/checkpoint_final.pt 2>/dev/null "
        f"|| cp --reflink=auto {shlex.quote(str(CPT_CKPT))} "
        f"{shlex.quote(str(test_dir))}/checkpoints/checkpoint_final.pt) && "
        f"ls -l {shlex.quote(str(test_dir))}/checkpoints/checkpoint_final.pt", timeout=1800)
    if rc != 0:
        print(json.dumps({"verdict": "NOT-VERIFIED", "why": f"тестовый каталог не собран: {err}"},
                         ensure_ascii=False, indent=2))
        return 2
    link = out

    def docker(cmd: str, name: str) -> tuple[int, str]:
        full = (f"docker run --rm --name {name} --gpus all --ipc=host --pid=host "
                f"--memory=100g --memory-swap=100g --security-opt seccomp=unconfined "
                f"--cap-add SYS_PTRACE --ulimit memlock=-1 --ulimit stack=67108864 "
                f"--ulimit nofile=262144:262144 "
                f"-v {SHARED}:{CTR_SHARED} -v /home/user/experiments:/workspace/experiments "
                f"-v /home/user/.cache/huggingface:/root/.cache/huggingface -w /workspace "
                f"{IMAGE} bash -lc {shlex.quote(cmd)}")
        r = ssh(full, timeout=3600)
        return r[0], r[1] + ("\n" + r[2] if r[2] else "")

    # ── 1. инварианты меток + язык снятого (в том же образе, с токенайзером) ──
    verify: dict = {"skipped": "--do smoke"}
    rc_v, out_v = 0, ""
    if do_verify:
        verify_cmd = (f"python3 {ctr_run}/verify_think_mask.py --pipeline {ctr_run}/laguna_pipeline_sft.py "
                  f"--baseline-pipeline {CTR_SHARED}/{BASELINE_PIPELINE.parent.name}/"
                  f"{BASELINE_PIPELINE.name} "
                  f"--sft-jsonl {CTR_SHARED}/datasets/sft_train_v12.jsonl "
                  f"--samples {a.samples} --out {ctr_test}/logs/mask_verify.json")
        rc_v, out_v = docker(verify_cmd, f"laguna-maskverify-{a.ts}")
        try:
            verify = json.loads(out_v[out_v.index("{"):]) if "{" in out_v \
                else {"verdict": "NO-JSON", "raw": out_v[-2000:]}
        except Exception:
            verify = {"verdict": "NO-JSON", "raw": out_v[-2000:]}
        ssh(f"mkdir -p {shlex.quote(str(test_dir))}/logs")   # каталог нужен и в режиме verify

    # ── 2. короткий прогон: тот же путь, что стадия, 12 шагов ────────────────
    smoke_cmd = (
        f"python3 {ctr_run}/laguna_pipeline_sft.py --model_name Qwen/Qwen2.5-0.5B --stage sft "
        f"--exp_name sft-masktest-{a.ts} --max_steps {a.steps} --max_samples 50000 --batch_size 2 "
        f"--max_len 8192 --seed 42 --cpt_data {CTR_SHARED}/datasets/cpt_corpus_v12r.txt "
        f"--sft_data {CTR_SHARED}/datasets/sft_train_v12.jsonl "
        f"--rl_data {CTR_SHARED}/datasets/rl_tasks_revpool_v2.jsonl "
        f"--eval_data {CTR_SHARED}/datasets/eval_ood_clean.jsonl "
        f"--ckpt_dir {ctr_test}/checkpoints --log_dir {ctr_test}/logs")
    rc_s, out_s = 0, ""
    if do_smoke:
        rc_s, out_s = docker(smoke_cmd, f"laguna-masksmoke-{a.ts}")
        ssh(f"cat > {shlex.quote(str(test_dir))}/logs/mask_smoke.log <<'EOF'\n{out_s}\nEOF")

    # ── факты теста ─────────────────────────────────────────────────────────
    #: В режиме evaluate лог берётся из артефакта: пересчёт проверок не должен
    #: требовать повторного 12-шагового прогона (и не должен его делать).
    if do_eval:
        log_path = Path(a.smoke_log) if a.smoke_log else test_dir / "logs" / "mask_smoke.log"
        out_s = log_path.read_text(encoding="utf-8", errors="replace")
        ckpt = "(evaluate: чекпойнты не пересматриваются)"
        mrep = test_dir / "logs" / "sft_mask_report.json"
        try:
            mask_report = json.loads(mrep.read_text(encoding="utf-8")) if mrep.is_file() else {}
        except json.JSONDecodeError:
            mask_report = {}
    tail = [l for l in out_s.splitlines() if l.strip()][-25:]
    loss_lines = [l for l in out_s.splitlines() if "SFT step" in l or "SFT warmup" in l]
    mask_lines = [l for l in out_s.splitlines() if "MASK REPORT" in l]
    #: Подстрочный поиск «nan» даёт ложные срабатывания на баннере PyTorch
    #: («Ronan Collobert») — проверка обязана искать число, а не три буквы.
    nan_re = re.compile(r"(?i)\b(loss|ema|gnorm|grad_norm|val_loss)\s*=\s*nan")
    nan = [l for l in out_s.splitlines() if nan_re.search(l)]
    #: Отказ исполнения ищется явно: NaN-проверка видит числа, а не исключения —
    #: первый короткий тест S3ae упал `ValueError` на 2-D батче валидации, и
    #: «шаги в логе есть» этот отказ не ловило (шаг 0 был записан).
    err_re = re.compile(r"Traceback \(most recent call last\)|^\s*(ValueError|RuntimeError|"
                        r"AssertionError|CUDA error|torch\.OutOfMemoryError)", re.M)
    errs = [l for l in out_s.splitlines() if err_re.search(l)]
    if not do_eval:
        ckpt = ssh(f"ls -l {shlex.quote(str(test_dir))}/checkpoints/ | tail -3")[1]
        mask_report = ssh(f"cat {shlex.quote(str(test_dir))}/logs/sft_mask_report.json "
                          f"2>/dev/null || echo '{{}}'")[1]
        try:
            mask_report = json.loads(mask_report)
        except Exception:
            mask_report = {"raw": mask_report}

    checks = []
    if do_verify:
        checks += [
            ("инварианты меток PASS", verify.get("verdict") == "PASS"),
            ("копия стадии и проверенная копия совпадают по sha256",
             verify.get("pipeline_sha256") ==
             __import__("hashlib").sha256(pipe.read_bytes()).hexdigest()),
            ("id токенайзера совпали с контрактом AD-3",
             verify.get("tokenizer_ids", {}).get("matches_pinned") is True),
            ("маска сработала не на пустом множестве (снято больше нуля)",
             (verify.get("masked_share") or 0) > 0),
        ]
    if do_smoke or do_eval:
        checks += [
            ("отчёт о маске напечатан прогоном", bool(mask_lines)),
            ("шаги пройдены (лог шагов непуст)", bool(loss_lines)),
            ("NaN в лоссе нет", not nan),
            ("прогон дошёл до конца (SFT complete / Pipeline finished)",
             any(("SFT complete" in l) or ("finished" in l) for l in out_s.splitlines())),
            ("в логе нет Traceback/исключения", not errs),
        ]
        if not do_eval:
            checks.append(("чекпойнт теста записан",
                           bool(ckpt.strip()) and "sft_checkpoint_final.pt" in ckpt))
    rep = {
        "tool": "tools/run_mask_smoke.py",
        "run_dir": str(run_dir), "test_dir": str(test_dir), "steps": a.steps,
        "mode": a.do, "nan_check": "регексп по (loss|ema|gnorm|val_loss)=nan",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "input_link": link,
        "verify": verify,
        "mask_report_from_run": mask_report,
        "mask_report_lines": mask_lines,
        "loss_lines": loss_lines[-5:],
        "nan_lines": nan[:5],
        "error_lines": errs[:5],
        "log_tail": tail,
        "checkpoints": ckpt,
        "checks": [{"name": n, "ok": bool(ok)} for n, ok in checks],
        "verdict": "PASS" if all(ok for _, ok in checks) else "FAIL",
        "smoke_log": str(test_dir / "logs" / "mask_smoke.log"),
    }
    if not a.keep_checkpoint and do_smoke:
        rc_c, out_c, _ = ssh(f"rm -f {shlex.quote(str(test_dir))}/checkpoints/sft_checkpoint_final.pt "
                             f"{shlex.quote(str(test_dir))}/checkpoints/checkpoint_final.pt && "
                             f"du -sh {shlex.quote(str(test_dir))} "
                             f"{shlex.quote(str(test_dir))}/logs/*")
        rep["checkpoint_cleanup"] = out_c
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    #: Отчёт кладётся и на стенд (в каталог теста): evidence собирается из артефактов
    #: прогона, а не из локального черновика.
    try:
        ssh(f"mkdir -p {shlex.quote(str(test_dir))}/logs && cat > "
            f"{shlex.quote(str(test_dir))}/logs/mask_smoke_report.json <<'EOF'\n"
            f"{json.dumps(rep, ensure_ascii=False, indent=2)}\nEOF")
    except Exception as e:                                     # noqa: BLE001
        print(f"отчёт не выложен на стенд: {e}", file=sys.stderr)
    if a.out:
        Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
