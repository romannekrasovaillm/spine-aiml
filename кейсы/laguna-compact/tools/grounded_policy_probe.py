#!/usr/bin/env python3
"""Прибор измерения ДЕЙСТВИЯ политики по состоянию среды (спека `docs/specs/GROUNDED-POLICY-PROBE.md`).

Зачем (спека §1): `tools/grounded_runner.py` прогоняет контракт среды (положительный
контроль, no-action, лимит шагов) и **модель не загружает вовсе**; `tools/passrate_probe.py`
измеряет текстовый пул через `search_concepts`. Вопрос владельца «научилась ли модель
действовать умнее» без этого прибора неизмерим — ни до RL, ни после.

Что делает на задачу (спека §2): поднимает среду задачи, подаёт политике наблюдение,
даёт ей до `max_steps` действий, каждое действие исполняет в среде, затем прогоняет
**верификатор задачи по состоянию** и получает бинарный кредит (AD-11).

Семантика контура среды — **импортом** из `tools/grounded_runner.py`, не копией:
`Sandbox` (контейнер, read-only корень, лимит шагов из задачи, супервизор шага),
`MockSidecar`, `Task`/`load_tasks`, `run_verifier`, `rollback_state`, `mount_preflight`,
`set_digest`. Второй реализации контракта среды здесь нет и быть не должно.
Загрузка чекпойнта и генерация — **импортом** из `tools/passrate_probe.py`
(`load_checkpoint`, `verify_checkpoint_load`, `generate_turns`) и `laguna_pipeline_v8`
(`SPECIAL_TOKENS`, `_load_ckpt_with_resize`): семантика контура, не копия.

Политики (`--policy`): `model` (по умолчанию — реальный замер), `gold` (эталонное
действие задачи, положительный контроль), `empty` (`true`: действие без последствий),
`no-action` (ни одного действия, состязательный текст — проба G2).

Коды возврата::

    0 — прогон состоялся (числа в отчёте; провалы задач — это данные, а не отказ)
    1 — отказ считать замер (нарушен контракт: нет брони предмета, нет набора, нет run-dir)
    2 — NOT-VERIFIED: среды нет (нет docker/образа/пользователя), числа не выдумываются
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import sys
import time

CASE_ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION = "grounded-policy-probe/3"

EXIT_OK, EXIT_REFUSE, EXIT_NOT_VERIFIED = 0, 1, 2


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не загрузить модуль {name} из {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GR = _load("grounded_runner", pathlib.Path(__file__).resolve().parent / "grounded_runner.py")
PP = _load("passrate_probe", pathlib.Path(__file__).resolve().parent / "passrate_probe.py")

#: Разбор вызова инструмента — формат стадии (`laguna_pipeline_v8.py:42`):
#: `<tool_call>{"name": …, …}</tool_call>`. Прибор читает вызов, но исполняет его
#: в СРЕДЕ задачи (shell внутри контейнера), а не в текстовом пуле концептов.
TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>)?", re.DOTALL)

#: Имена инструментов, которые прибор умеет превратить в действие среды. Всё
#: остальное (например `search_concepts` текстового пула) — `tool_error`: среда
#: такого инструмента не предоставляет, и действие не исполняется.
SHELL_TOOLS = ("shell", "bash", "sh", "run", "exec", "filesystem", "fs", "python")

INSTRUCTION = (
    "Ты — агент, работающий в изолированной среде. Каталог /work — твоё рабочее "
    "пространство; состояние среды меняется только твоими действиями.\n"
    "Чтобы выполнить действие, ответь ОДНИМ вызовом: "
    '<tool_call>{"name": "shell", "command": "…"}</tool_call>\n'
    "Ответ инструмента придёт в блоке <tool_response>. Когда задача решена, дай "
    "финальный ответ БЕЗ <tool_call>.\n\nЗадача:\n"
)


def sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_action(text: str) -> tuple[dict | None, str]:
    """Вызов инструмента из ответа политики: ``(вызов, причина_отказа)``."""
    m = TOOL_CALL.search(text or "")
    if not m:
        return None, "в ответе нет <tool_call>…</tool_call>"
    try:
        payload = json.loads(m.group(1))
    except ValueError as exc:
        return None, f"нагрузка вызова не разобрана как JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "нагрузка вызова не объект"
    return payload, ""


def action_command(payload: dict) -> tuple[str | None, str]:
    """Команда среды из вызова. Незнакомый инструмент — ``tool_error`` (не отказ прогона)."""
    name = str(payload.get("name") or "").strip().lower()
    args = payload.get("arguments")
    if not isinstance(args, dict):
        args = {k: v for k, v in payload.items() if k != "name"}
    if name not in SHELL_TOOLS:
        return None, f"инструмент '{name}' среде задачи неизвестен (есть: shell)"
    for key in ("command", "cmd", "script", "code", "input"):
        if isinstance(args.get(key), str) and args[key].strip():
            return args[key], ""
    return None, "в вызове нет строкового действия (command/cmd/script)"


class Receipt:
    """Бронь предмета (ADR-058): копия/хардлинк, sha с КОПИИ, верификация в начале."""

    def __init__(self, src: pathlib.Path, run_dir: pathlib.Path) -> None:
        self.src = src
        self.dir = run_dir / "ckpt"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.copy = self.dir / src.name

    def take(self) -> dict:
        if not self.src.is_file():
            raise FileNotFoundError(f"предмет брони не найден: {self.src}")
        if not self.copy.exists():
            try:
                os.link(self.src, self.copy)          # хардлинк в пределах той же ФС
                method = "hardlink"
            except OSError:
                shutil.copy2(self.src, self.copy)     # иначе — приватная копия
                method = "copy"
        else:
            method = "exists"
        meta = {"source": str(self.src), "copy": str(self.copy), "method": method,
                "bytes": self.copy.stat().st_size,
                "sha256": GR.sha256_file(self.copy),
                "sha256_source": GR.sha256_file(self.src),
                "taken_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        # Верификация предмета В НАЧАЛЕ (спека §3 п.1): копия обязана быть побайтово той же.
        meta["verified"] = bool(meta["sha256"] == meta["sha256_source"]
                                and meta["bytes"] == self.src.stat().st_size)
        (self.dir / "RECEIPT.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return meta


def load_policy_model(args, run_dir: pathlib.Path):
    """Модель политики: чекпойнт из брони + семантика контура импортом."""
    import torch
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    sys.path.insert(0, str(CASE_ROOT))
    import laguna_pipeline_v8 as pipe  # noqa: E402

    torch.manual_seed(args.seed)
    tok = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
    tok.add_special_tokens({"additional_special_tokens": list(pipe.SPECIAL_TOKENS)})
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    cfg = AutoConfig.from_pretrained(args.config_from, local_files_only=True)
    model = AutoModelForCausalLM.from_config(cfg, torch_dtype=torch.bfloat16).eval()
    model.resize_token_embeddings(len(tok))
    ckpt_meta = PP.load_checkpoint(model, pathlib.Path(args.checkpoint_copy), torch, pipe, tok,
                                   init_special_tokens=args.init_special_tokens)
    model = model.cuda()
    ckpt_meta["load_verification"] = PP.verify_checkpoint_load(
        model, pathlib.Path(args.checkpoint_copy), torch)
    # Сэмплер — тот же, что в контуре (PP.make_sampler): один поток RNG на прогон,
    # поэтому при seed и temperature=0 прогон детерминирован.
    return model, tok, torch, pipe, ckpt_meta, PP.make_sampler(torch)


def policy_actions(task, args, sandbox, mock, run_dir, policy_state) -> dict:
    """Действия политики в среде задачи. Возвращает запись о ходе прогона.

    Действие исполняется ТОЛЬКО средой (`sandbox.exec`): бюджет шагов держит задача
    (`Task.effective_max_steps`), отказ по бюджету — работающая среда, а не сбой.
    """
    rd = run_dir / "rollouts" / task.id / args.policy
    shutil.rmtree(rd, ignore_errors=True)
    rd.mkdir(parents=True, exist_ok=True)
    transcript = rd / "transcript.txt"
    scratch = run_dir / "scratch"
    rec: dict = {"task": task.id, "tool": task.spec["tool"], "network": task.network,
                 "policy": args.policy, "container": sandbox.name, "repeat": 0}

    t0 = time.perf_counter()
    GR.rollback_state(task, sandbox, mock)
    seed_digest = GR.tree_digest(sandbox.workdir)
    rec["seed_s"] = round(time.perf_counter() - t0, 4)
    rec["state_writable"] = sandbox.probe_state_writable()

    # Ожидаемое значение — ДО действия: состязательный текст пробы no-action должен
    # называть точное значение и всё равно получить 0.
    pre = GR.run_verifier(task, GR.empty_snapshot(run_dir, task.id), args, mock, scratch)
    expected_public = str(pre.get("expected_public", ""))
    rec["expected_public"] = expected_public

    sandbox.reset_steps(task.effective_max_steps(getattr(args, "max_steps", None)))
    rec["max_steps_from_task"] = task.max_steps
    rec["max_steps_effective"] = sandbox.max_steps

    lines: list[str] = [f"policy: {args.policy}\ntask: {task.id}\n"]
    steps: list[dict] = []
    tool_errors: list[str] = []

    def run_step(command: str, index: int) -> GR.StepResult:
        t = time.perf_counter()
        st = sandbox.exec(command, timeout=task.timeout_s, count_step=True)
        steps.append({"index": index, "command_sha256": sha256_text(command),
                      "command_head": command.strip().splitlines()[0][:120] if command.strip() else "",
                      "refused": bool(st.refused), "timed_out": bool(st.timed_out),
                      "returncode": st.returncode, "elapsed_s": round(time.perf_counter() - t, 4),
                      "stdout_head": (st.stdout or "")[:400], "stderr_head": (st.stderr or "")[:200]})
        lines.append(f"\n[step {index}] {command.strip()[:400]}\n"
                     f"rc={st.returncode} refused={st.refused} timed_out={st.timed_out}\n"
                     f"{(st.stdout or '')[:2000]}\n")
        return st

    if args.policy == "gold":
        lines.append("(эталонное действие задачи — положительный контроль)\n")
        run_step(task.solution.read_text(encoding="utf-8"), 1)
    elif args.policy == "empty":
        lines.append("(действие без последствий: true)\n")
        run_step("true", 1)
    elif args.policy == "no-action":
        lines.append(f"(ни одного действия; состязательный текст: Готово. {expected_public})\n")
    elif args.policy == "model":
        model, tok, torch, pipe, ckpt_meta, sampler = policy_state["model_bits"]
        instruction = INSTRUCTION
        if args.instruction_file:
            instruction = pathlib.Path(args.instruction_file).expanduser().read_text(encoding="utf-8")
        task_prompt = str(task.spec.get("prompt", ""))
        if args.framing == "chat":
            #: Постановка входа шаблоном чата — как в обучении (pretokenize_v9.py:75):
            #: роли system/user + метка <|im_start|>assistant; токенизируется уже
            #: обёрнутый шаблоном текст (generate_turns получает готовую строку).
            context = tok.apply_chat_template(
                [{"role": "system", "content": instruction},
                 {"role": "user", "content": task_prompt}],
                tokenize=False, add_generation_prompt=True)
        else:
            #: raw — прежнее поведение (INSTRUCTION + текст задачи сырым текстом).
            context = instruction + task_prompt
        lines.append(f"(наблюдение подано политике; бюджет шагов {sandbox.max_steps})\n")
        for index in range(1, sandbox.max_steps + 1):
            if sandbox.steps_used >= sandbox.max_steps:
                break
            t = time.perf_counter()
            outs = PP.generate_turns(model, tok, [context], max_new_tokens=args.max_new_tokens,
                                     temperature=args.temperature, top_k=args.top_k,
                                     batch_size=1, torch=torch,
                                     sample=sampler, label=task.id,
                                     no_repeat_ngram=args.no_repeat_ngram)
            answer = outs[0] if outs else ""
            lines.append(f"\n[ход {index}] модель ({time.perf_counter() - t:.1f}s):\n{answer[:2000]}\n")
            payload, why = parse_action(answer)
            if payload is None:
                tool_errors.append(f"{task.id}#{index}: {why}")
                lines.append(f"[ход {index}] действия нет: {why}\n")
                break
            command, why = action_command(payload)
            if command is None:
                tool_errors.append(f"{task.id}#{index}: {why}")
                lines.append(f"[ход {index}] tool_error: {why}\n")
                continue
            st = run_step(command, index)
            context += (f"\n<tool_response>\n{(st.stdout or '')[:2000]}"
                        f"{('[stderr] ' + (st.stderr or '')[:500]) if st.stderr else ''}\n</tool_response>\n")
    else:
        raise ValueError(f"неизвестная политика: {args.policy}")

    transcript.write_text("".join(lines), encoding="utf-8")
    rec["transcript_sha256"] = GR.sha256_file(transcript)
    rec["steps"] = steps
    rec["steps_used"] = sandbox.steps_used
    rec["steps_refused"] = sandbox.steps_refused
    rec["tool_errors"] = tool_errors

    # Снимок состояния — снаружи контейнера; верификатор читает СНИМОК и pristine.
    t0 = time.perf_counter()
    snap = rd / "snapshot"
    snap.mkdir(parents=True, exist_ok=True)
    GR.copy_contents(sandbox.workdir, snap)
    rec["snapshot_s"] = round(time.perf_counter() - t0, 4)
    rec["state_digest"] = GR.tree_digest(snap)
    rec["state_changed"] = rec["state_digest"] != seed_digest
    rec["symlink_escape"] = GR.find_symlinks(snap)

    t0 = time.perf_counter()
    first = GR.run_verifier(task, snap, args, mock, scratch)
    rec["verify_s"] = round(time.perf_counter() - t0, 4)
    rec["score"] = first.get("score")
    rec["reason"] = first.get("reason", "")
    rec["reads"] = first.get("reads", [])
    rec["failure_class"] = classify_failure(rec, sandbox.max_steps)
    rec["total_s"] = round(rec["seed_s"] + rec["snapshot_s"] + rec["verify_s"], 4)
    return rec


def classify_failure(rec: dict, budget: int) -> str | None:
    """Классы отказа объявлены ЗАРАНЕЕ (спека §2): покрывают все провалы.

    `a_no_action` — политика не сделала ни одного действия;
    `b_action_no_effect` — действия были, состояние не изменилось;
    `c_wrong_state` — состояние изменилось, но не к цели;
    `d_cutoff` — упёрлась в лимит шагов.
    Приоритет — по порядку причинности: отсутствие действия → обрезка → эффект.
    """
    if rec.get("score") == 1:
        return None
    if rec.get("steps_used", 0) == 0:
        return "a_no_action"
    if budget is not None and rec.get("steps_used", 0) >= budget:
        return "d_cutoff"
    if not rec.get("state_changed"):
        return "b_action_no_effect"
    return "c_wrong_state"


def summarize(records: list[dict], protocol: dict, n_tasks: int) -> dict:
    n = len(records)
    passes = [1 if r.get("score") == 1 else 0 for r in records]
    steps = [r.get("steps_used", 0) for r in records]
    no_action = [1 if r.get("steps_used", 0) == 0 else 0 for r in records]
    tool_err = [1 if r.get("tool_errors") else 0 for r in records]
    fails = [r for r in records if r.get("score") != 1]
    classes: dict[str, int] = {}
    for r in fails:
        classes[r["failure_class"]] = classes.get(r["failure_class"], 0) + 1
    return {
        "n": n,
        "pass_rate": round(sum(passes) / n, 4) if n else None,
        "no_action_share": round(sum(no_action) / n, 4) if n else None,
        "tool_error_share": round(sum(tool_err) / n, 4) if n else None,
        "mean_steps": round(statistics.fmean(steps), 4) if n else None,
        "median_steps": round(statistics.median(steps), 4) if n else None,
        "no_action_successes": sum(1 for r in records
                                   if r.get("score") == 1 and r.get("steps_used", 0) == 0),
        "failure_classes": classes,
        "failure_share_of_failures": (
            {k: round(v / len(fails), 4) for k, v in sorted(classes.items())} if fails else {}),
        "by_task": [
            {"task": r["task"], "score": r.get("score"), "steps_used": r.get("steps_used", 0),
             "failure_class": r.get("failure_class"),
             "state_changed": r.get("state_changed"),
             "reason": r.get("reason", "")[:200],
             "tool_errors": len(r.get("tool_errors", []))}
            for r in sorted(records, key=lambda x: x["task"])
        ],
        "limits": ([f"signal_only: n = {n} (скелет; вердикт по доле не выносится — ADR-049 п.12)",
                    "текстовый пул этим прибором не измеряется (другая ось, ADR-061 п.6)"]
                   if n < 30 else []),
        "declared_n_tasks": n_tasks,
        "protocol": protocol,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--checkpoint", default="",
                    help="предмет брони: чекпойнт политики (ADR-058: берётся копия, sha — с копии)")
    ap.add_argument("--set-dir", default=str(CASE_ROOT / "data" / "grounded-skeleton"))
    ap.add_argument("--state", default="", help="тег состояния политики: base|cpt|sft|rl")
    ap.add_argument("--instruction-file", default="",
                    help="если задан — системный промпт берётся из файла (вместо INSTRUCTION); "
                         "иначе поведение прежнее (INSTRUCTION прибора)")
    ap.add_argument("--framing", default="chat", choices=["chat", "raw"],
                    help="постановка входа: chat — шаблон чата (system=системный промпт, "
                         "user=текст задачи, add_generation_prompt=True; новое по умолчанию); "
                         "raw — прежнее поведение (INSTRUCTION + текст задачи сырым текстом)")
    ap.add_argument("--out", default=str(CASE_ROOT / "evidence" / "grounded-policy-probe.json"))
    ap.add_argument("--run-dir", default="", help="каталог прогона (обязан лежать в $HOME: docker не видит /tmp)")
    ap.add_argument("--policy", default="model", choices=["model", "gold", "empty", "no-action"])
    ap.add_argument("--tasks", nargs="*", default=None)
    ap.add_argument("--seed", type=int, default=20260925)
    ap.add_argument("--max-steps", type=int, default=None, help="внешний потолок шагов (min с бюджетом задачи)")
    ap.add_argument("--repeat", type=int, default=1, help="повторов прогона (детерминизм на фикстуре)")
    ap.add_argument("--image", default=GR.DEFAULT_IMAGE)
    ap.add_argument("--sidecar-image", default=GR.DEFAULT_SIDECAR_IMAGE)
    ap.add_argument("--mock-seed", type=int, default=GR.MOCK_SEED_DEFAULT)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    # параметры генерации политики (контур: ADR-041 п.4 — режим в протоколе)
    ap.add_argument("--tokenizer", default="/home/user/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B/snapshots/060db6499f32faf8b98477b0a26969ef7d8b9987")
    ap.add_argument("--config-from", default="/home/user/gb10-shared/models-store/experiments/kda-graft/models/qwen2.5-0.5b-base")
    ap.add_argument("--max-new-tokens", type=int, default=192)
    #: Режим генерации — штатный режим стадии (ADR-041 п.1): sample, T=1, top_k=20,
    #: запрет повторов 4-грамм. `--temperature 0` (greedy) САМПЛЕРОМ КОНТУРА не
    #: поддерживается (деление на температуру в `passrate_probe.make_sampler` даёт
    #: inf → CUDA-assert), поэтому такой режим отвергается явно, а не молча падает.
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--no-repeat-ngram", type=int, default=4)
    ap.add_argument("--init-special-tokens", action="store_true")
    args = ap.parse_args(argv)

    def say(msg: str) -> None:
        print(msg, file=sys.stderr if args.json else sys.stdout, flush=True)

    set_dir = pathlib.Path(args.set_dir).resolve()
    if not set_dir.is_dir():
        say(f"NOT-VERIFIED: нет набора задач {set_dir}")
        return EXIT_NOT_VERIFIED

    ok, why = GR.runtime_user_ready(CASE_ROOT)
    if not ok:
        say(f"NOT-VERIFIED: {why}")
        return EXIT_NOT_VERIFIED
    ok, info = GR.docker_ok()
    if not ok:
        say(f"NOT-VERIFIED: {info}")
        return EXIT_NOT_VERIFIED

    if args.policy == "model" and not args.checkpoint:
        say("ОТКАЗ: политика model без --checkpoint не измеряется (бронь предмета, ADR-058)")
        return EXIT_REFUSE

    if args.policy == "model" and args.temperature <= 0:
        say("ОТКАЗ: --temperature должен быть > 0: самплер контура (passrate_probe.make_sampler) "
            "для greedy не поддерживается (деление на температуру даёт inf → CUDA-assert); "
            "штатный режим стадии — sample T=1 top_k=20 nogram4 (ADR-041 п.1)")
        return EXIT_REFUSE

    run_root = pathlib.Path(args.run_dir).expanduser() if args.run_dir else GR.DEFAULT_RUN_ROOT
    run_dir = run_root / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scratch").mkdir(exist_ok=True)

    # ── бронь предмета ДО первого действия (ADR-058 / спека §3 п.1) ───────────
    receipt = None
    if args.checkpoint:
        try:
            receipt = Receipt(pathlib.Path(args.checkpoint).expanduser(), run_dir).take()
        except FileNotFoundError as exc:
            say(f"ОТКАЗ: {exc}")
            return EXIT_REFUSE
        if not receipt["verified"]:
            say(f"ОТКАЗ: копия предмета не совпала с источником: {receipt}")
            return EXIT_REFUSE
        args.checkpoint_copy = receipt["copy"]
        say(f"бронь: {receipt['copy']} sha256 {receipt['sha256'][:12]}… ({receipt['method']})")

    try:
        tasks = GR.load_tasks(set_dir, args.tasks)
    except ValueError as exc:
        say(f"NOT-VERIFIED: {exc}")
        return EXIT_NOT_VERIFIED
    if not tasks:
        say("NOT-VERIFIED: набор задач пуст")
        return EXIT_NOT_VERIFIED

    ok, image_info = GR.ensure_image_measured(args.image)
    if not ok:
        say(f"NOT-VERIFIED: {image_info['error']}")
        return EXIT_NOT_VERIFIED
    if any(t.network != "none" for t in tasks):
        ok, sidecar_info = GR.ensure_image_measured(args.sidecar_image)
        if not ok:
            say(f"NOT-VERIFIED: {sidecar_info['error']}")
            return EXIT_NOT_VERIFIED

    args.execution = GR.execution_context()
    args.image = args.image
    args.runtime_user = {"euid": os.geteuid(), "runtime_uid": GR.RUNTIME_UID}

    # ── модель политики (только для model) ────────────────────────────────────
    policy_state: dict = {}
    if args.policy == "model":
        t0 = time.time()
        model, tok, torch, pipe, ckpt_meta, sampler = load_policy_model(args, run_dir)
        policy_state["model_bits"] = (model, tok, torch, pipe, ckpt_meta, sampler)
        policy_state["ckpt_meta"] = ckpt_meta
        say(f"веса загружены за {time.time() - t0:.1f}s; "
            f"сверено тензоров {ckpt_meta['load_verification']['tensors_compared']}/"
            f"{ckpt_meta['load_verification']['tensors_in_checkpoint']}, "
            f"расхождений {ckpt_meta['load_verification']['mismatches']}")

    by_net: dict[str, list] = {}
    for t in tasks:
        by_net.setdefault(t.network, []).append(t)

    mock = None
    sandboxes: dict = {}
    network = ""
    rc = EXIT_OK
    records: list[dict] = []

    def shutdown() -> None:
        for sb in sandboxes.values():
            sb.stop()
        if mock:
            mock.stop()
        GR.remove_network(network)

    try:
        args.mount_preflight = GR.mount_preflight(
            run_dir, set_dir, args.image, check_set_tree=any(t.network != "none" for t in tasks))
        if not args.mount_preflight["ok"]:
            say(f"NOT-VERIFIED: {args.mount_preflight['findings'][0]}")
            shutdown()
            return EXIT_NOT_VERIFIED
        if any(net != "none" for net in by_net):
            network = f"{GR.RUN_TAG}-{run_dir.name}-net"
            ok, gw = GR.create_internal_network(network)
            if not ok:
                say(f"NOT-VERIFIED: сеть --internal не создана: {gw}")
                return EXIT_NOT_VERIFIED
            try:
                mock = GR.MockSidecar(set_dir, run_dir, args.mock_seed, network,
                                      args.sidecar_image, f"{network}-mock")
                mock.start()
            except RuntimeError as exc:
                say(f"NOT-VERIFIED: sidecar-мок не поднялся: {exc}")
                shutdown()
                return EXIT_NOT_VERIFIED

        for net, group in sorted(by_net.items()):
            name = f"{GR.RUN_TAG}-{run_dir.name}-{args.policy}-{'offline' if net == 'none' else 'net'}"
            sb = GR.Sandbox(args.image, run_dir / f"work-{net}",
                            network if net != "none" else "none", name,
                            mock_url=(mock.url if net != "none" and mock else ""))
            sb.start()
            sandboxes[name] = sb
            for task in group:
                for rep in range(args.repeat):
                    rec = policy_actions(task, args, sb, mock, run_dir, policy_state)
                    rec["repeat"] = rep
                    records.append(rec)
                    say(f"  [{task.id}] policy={args.policy} score={rec.get('score')} "
                        f"steps={rec['steps_used']} class={rec.get('failure_class')} "
                        f"({rec.get('reason', '')[:80]})")
    finally:
        if not args.keep:
            shutdown()

    set_sha, _files = GR.set_digest(set_dir)
    protocol = {
        "instrument": {"name": "grounded_policy_probe", "version": VERSION,
                       "sha256": GR.sha256_file(pathlib.Path(__file__).resolve()),
                       "reuses": {
                           "environment": "tools/grounded_runner.py (импорт: Sandbox, MockSidecar, "
                                          "Task, run_verifier, rollback_state, mount_preflight)",
                           "model": "tools/passrate_probe.py (импорт: load_checkpoint, "
                                    "verify_checkpoint_load, generate_turns) + laguna_pipeline_v8",
                           "environment_sha256": GR.sha256_file(
                               pathlib.Path(__file__).resolve().parent / "grounded_runner.py"),
                           "model_module_sha256": GR.sha256_file(
                               pathlib.Path(__file__).resolve().parent / "passrate_probe.py")}},
        "state": args.state or None,
        "policy": args.policy,
        "framing": args.framing,
        "checkpoint": ({"receipt": receipt, "sha256": receipt["sha256"],
                        "verified_against_source": receipt["verified"],
                        "load_verification": (policy_state.get("ckpt_meta") or {}).get("load_verification")}
                       if receipt else None),
        "set": {"dir": str(set_dir), "sha256_full": set_sha, "n_tasks": len(tasks)},
        "seed": args.seed,
        "max_steps": {"outer_cap": args.max_steps,
                      "per_task": {t.id: t.effective_max_steps(args.max_steps) for t in tasks}},
        "max_turns": "равен лимиту шагов задачи (действие = один exec среды)",
        "decoding": {"temperature": args.temperature, "top_k": args.top_k,
                     "no_repeat_ngram": args.no_repeat_ngram,
                     "mode": (f"sample_T{args.temperature:g}_topk{args.top_k}"
                              if args.policy == "model" else "n/a (" + args.policy + ")")
                             + f"_nogram{args.no_repeat_ngram}",
                     "max_new_tokens": args.max_new_tokens,
                     "legacy_decoding": False, "allowed_for_conclusions": True},
        "docker": info,
        "execution": args.execution,
        "images": [image_info] + ([sidecar_info] if any(t.network != "none" for t in tasks) else []),
        "run_dir": str(run_dir),
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "credit": "бинарный, по состоянию среды (AD-11): верификатор набора, не текст ответа",
    }

    evidence = {
        "schema": "grounded-policy-probe/1",
        "spec": "docs/specs/GROUNDED-POLICY-PROBE.md §2",
        "status": "complete",
        "policy": args.policy,
        "metrics": summarize(records, protocol, len(tasks)),
        "records": records,
        "limits": summarize(records, protocol, len(tasks))["limits"],
    }
    pathlib.Path(args.out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    else:
        m = evidence["metrics"]
        say(f"n={m['n']} pass_rate={m['pass_rate']} no_action_share={m['no_action_share']} "
            f"tool_error_share={m['tool_error_share']} mean_steps={m['mean_steps']} "
            f"median_steps={m['median_steps']}")
        say(f"классы отказа: {m['failure_classes'] or 'нет провалов'}")
        say(f"артефакт: {args.out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
