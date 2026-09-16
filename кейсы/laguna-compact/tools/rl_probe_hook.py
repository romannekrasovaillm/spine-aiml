#!/usr/bin/env python3
"""Обвязка RL-разведки: per-step разбивка времени стадии RL **без правки пайплайна**.

Зачем отдельная обвязка, а не ``tools/smoke_probe.py``. Смоук-обвязка меряет
обучающий проход по ``out.loss`` и токенную энтропию по ``labels``: у CPT/SFT
модель вызывается с ``labels``, и оба числа есть. В RL-петле пайплайна модель
вызывается **без** ``labels`` (``model(input_ids=full_ids).logits``,
``laguna_pipeline_v8.py:1357``): ``out.loss`` там нет, а ``labels`` — тем более.
Обвязка смоука приняла бы каждый обучающий forward RL за eval-проход и записала
бы «0 обучающих шагов» — то есть замер не состоялся бы молча. Поэтому у RL
своя обвязка, а шкала времени — единственное, что у стадий общее.

Что измеряется (единица — блок между стартами шагов, см. ниже):

* ``t_generate`` — время внутри ``vLLM LLM.generate`` (генерация роллаутов,
  все ходы всех траекторий шага), плюс ``gen_tokens`` (реально сгенерированные
  токены из ``outputs[].token_ids``) и ``n_generate_calls`` (волн генерации);
* ``t_policy_fwd`` — суммарное время обучающего forward политики (grad включён);
* ``t_other_fwd`` — forward'ы без grad: снапшот ``ref_model`` (old_logprobs —
  часть обучения), PPL-прогоны GEN-EVAL и пробы;
* ``t_gen_eval`` — целиком вызов ``_general_eval_step`` (анти-форгеттинг);
* ``t_resync`` — подмена весов роллаута (hot-sync или пересоздание engine);
  ``n_hotsync`` и ``n_engine_init`` считаются отдельно — это и есть «число
  ресников весов»;
* ``t_ckpt`` — ``save_checkpoint_atomic``; ``t_probe`` — ``run_probes``;
* ``dt`` — интервал между стартами шагов.

Границы шага берутся по ``RLDataset.sample``: пайплайн зовёт его ровно
``TASKS_PER_STEP`` раз на шаг (4 при ``LAGUNA_PURE=0``), до всего остального
тела шага. Первый вызов в шаге k закрывает блок шага k−1 — поэтому resync,
чекпоинт и GEN-EVAL, стоящие в конце тела шага k−1, попадают в его же блок, а
не в следующий. Последний блок (стадия кончилась) помечается ``partial``.

Энтропия политики здесь **не** измеряется: её считает сам пайплайн
(``entropy_h``, ``laguna_pipeline_v8.py:1412``) и печатает на шагах, кратных 10.
Обвязка её не дублирует и не подменяет — иначе в отчёте оказались бы две разные
величины под одним именем. Источник энтропии для стража и evidence — лог стадии
и ``rl_metrics.json`` (см. ``tools/run_rl_probe.py``).

Проброс: исключение из пайплайна не глушится — код возврата обвязки равен коду
возврата стадии (как у смоук-обвязки).

Запуск (внутри контейнера; строка аргументов пайплайна — после ``--``)::

    python3 rl_probe_hook.py --metrics /workspace/experiments/<run>/rl_probe.jsonl \\
        --tasks-per-step 4 --group-size 2 \\
        --pipeline /workspace/shared/laguna_pipeline_v8.py \\
        -- --stage rl --model_name Qwen/Qwen2.5-0.5B --rl_steps 100 ...

Коды возврата: 0 — стадия прошла; иначе — код стадии или argparse.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

#: Сколько задач на шаг и траекторий на задачу при LAGUNA_PURE=0 (умолчания
#: пайплайна, ``laguna_pipeline_v8.py:1122``). Передаются явно, потому что по
#: ним определяется граница шага; расхождение с фактом ловится в ``end``-записи
#: (``samples_per_step_observed``), а не тонет в среднем.
DEFAULT_TASKS_PER_STEP = 4
DEFAULT_GROUP_SIZE = 2


class StepRecorder:
    """Пишет JSONL: ``start`` → ``init``* → ``step``* → ``end``."""

    def __init__(self, path: Path, tasks_per_step: int, group_size: int):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tasks_per_step = max(1, tasks_per_step)
        self.group_size = max(1, group_size)
        self.started_at = time.time()

        self.samples_total = 0
        self._samples_in_step = 0
        self._step_index = 0
        self._step_start: float | None = None
        self._engine_inits = 0
        self._reset_bucket()

        self._fh = self.path.open("w", encoding="utf-8")

    # ── запись ────────────────────────────────────────────────────────────────
    def _emit(self, obj: dict) -> None:
        self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._fh.flush()

    def start(self, meta: dict) -> None:
        self._emit({"event": "start", "ts": self.started_at, **meta})

    def _reset_bucket(self) -> None:
        self.b = {"t_generate": 0.0, "t_policy_fwd": 0.0, "t_other_fwd": 0.0,
                  "t_gen_eval": 0.0, "t_resync": 0.0, "t_ckpt": 0.0, "t_probe": 0.0,
                  "n_generate_calls": 0, "gen_tokens": 0, "prompt_tokens": 0,
                  "n_hotsync": 0, "n_engine_init": 0, "n_policy_fwd": 0, "n_other_fwd": 0}

    # ── хуки ──────────────────────────────────────────────────────────────────
    def on_sample(self) -> None:
        """Вызов ``RLDataset.sample``: начало шага или продолжение текущего."""
        now = time.time()
        if self._samples_in_step == 0:
            if self._step_index > 0:
                self._emit_step(self._step_index - 1, now - (self._step_start or now),
                                partial=False)
            self._step_start = now
            #: Явная метка старта шага — по ней считается время запуска стадии
            #: (``startup_seconds`` = первый старт − старт обвязки: загрузка SFT-чекпойнта,
            #: экспорт HF-весов, инициализация vLLM). Без неё старт стадии пришлось бы
            #: вычитать из времени шага, а это разные статьи расхода.
            self._emit({"event": "step_start", "i": self._step_index, "ts": now})
            self._step_index += 1
        self._samples_in_step += 1
        self.samples_total += 1
        if self._samples_in_step >= self.tasks_per_step:
            self._samples_in_step = 0

    def on_generate(self, seconds: float, n_prompts: int, gen_tokens: int,
                    prompt_tokens: int) -> None:
        self.b["t_generate"] += seconds
        self.b["n_generate_calls"] += 1
        self.b["gen_tokens"] += gen_tokens
        self.b["prompt_tokens"] += prompt_tokens

    def on_forward(self, seconds: float, training: bool, tokens: int) -> None:
        if training:
            self.b["t_policy_fwd"] += seconds
            self.b["n_policy_fwd"] += 1
        else:
            self.b["t_other_fwd"] += seconds
            self.b["n_other_fwd"] += 1

    def on_engine_init(self, seconds: float) -> None:
        """Создание vLLM-движка: первый — старт стадии, остальные — ресник.

        Первый инициализируется **до** первого шага (``laguna_pipeline_v8.py:1179``),
        поэтому в блок шага не попадает: иначе время старта стадии приписалась бы
        шагу 0. Пересоздания (fallback-ресник) идут внутри тела шага — их время
        честно уходит в ``t_resync`` того шага, в котором они случились.
        """
        self._engine_inits += 1
        initial = self._engine_inits == 1
        self._emit({"event": "engine_init", "i": self._engine_inits,
                    "seconds": round(seconds, 2), "ts": time.time(),
                    "kind": "initial" if initial else "resync_recreate"})
        if not initial:
            self.b["n_engine_init"] += 1
            self.b["t_resync"] += seconds

    def on_hotsync(self, seconds: float) -> None:
        self.b["t_resync"] += seconds
        self.b["n_hotsync"] += 1

    def on_resync_other(self, seconds: float) -> None:
        """Ресник без hot-sync (обновление ref_model/ожидание VRAM) — тоже время."""
        self.b["t_resync"] += seconds

    def on_ckpt(self, seconds: float) -> None:
        self.b["t_ckpt"] += seconds

    def on_gen_eval(self, seconds: float) -> None:
        self.b["t_gen_eval"] += seconds

    def on_probes(self, seconds: float) -> None:
        self.b["t_probe"] += seconds

    # ── итоги ─────────────────────────────────────────────────────────────────
    def _emit_step(self, index: int, dt: float, partial: bool) -> None:
        rec = {"event": "step", "i": index, "dt": round(dt, 3), "partial": partial,
               "ts": time.time()}
        rec.update({k: (round(v, 3) if isinstance(v, float) else v)
                    for k, v in self.b.items()})
        self._emit(rec)
        self._reset_bucket()

    def finish(self, error: str | None) -> None:
        if self._step_index > 0 and self._step_start is not None:
            # Незакрытый последний шаг: стадия кончилась (или её остановил страж).
            # Пишется с partial=True — «шаг начался, но блок не закрыт» — чтобы
            # неполный блок не вошёл в средние как полноценный.
            self._emit_step(self._step_index - 1, time.time() - self._step_start,
                            partial=True)
        extra = {"event": "end", "ts": time.time(),
                 "wall_seconds": round(time.time() - self.started_at, 2),
                 "steps_closed": max(0, self._step_index - 1) if self._step_index else 0,
                 "steps_started": self._step_index,
                 "samples_total": self.samples_total,
                 "tasks_per_step": self.tasks_per_step,
                 "group_size": self.group_size,
                 "engine_inits_total": self._engine_inits,
                 "error": error}
        try:
            import torch
            if torch.cuda.is_available():
                extra["torch_peak_allocated_bytes"] = int(torch.cuda.max_memory_allocated())
                extra["torch_peak_reserved_bytes"] = int(torch.cuda.max_memory_reserved())
        except Exception as e:  # замер памяти не должен ломать отчёт
            extra["torch_peak_error"] = f"{type(e).__name__}: {e}"
        self._emit(extra)
        self._fh.close()


def wrap_model_forward(model, rec: StepRecorder):
    """Оборачивает ``forward`` экземпляра: время и токены обучающих проходов.

    Отличие от смоука: ``loss``/``labels`` тут не нужны (RL зовёт модель без
    них), поэтому классификация прохода — только по grad-режиму. Snapshot
    ``ref_model`` (deepcopy) получает **тот же** closure: deepcopy функций
    возвращает тот же объект, и это правильно — его forward'ы идут под
    ``no_grad`` и попадают в ``t_other_fwd``.
    """
    import torch

    original = model.forward

    def forward(*args, **kwargs):
        training = bool(torch.is_grad_enabled())
        t0 = time.time()
        out = original(*args, **kwargs)
        dt = time.time() - t0
        input_ids = kwargs.get("input_ids")
        if input_ids is None and args:
            input_ids = args[0]
        tokens = int(input_ids.numel()) if hasattr(input_ids, "numel") else 0
        rec.on_forward(dt, training, tokens)
        return out

    model.forward = forward
    return model


def instrument_pipeline(module, rec: StepRecorder) -> dict:
    """Патчит точки пайплайна, по которым считается разбивка времени.

    Патчи — обёртки над **существующими** функциями (поведение не меняется):
    оригинал вызывается и его результат возвращается как есть. Ни одного
    изменения в родительском контуре (AD-4/AD-7): он read-only, и разведка не
    повод его править.
    """
    patched = {}

    # 1. Границы шага: RLDataset.sample (TASKS_PER_STEP вызовов на шаг).
    orig_sample = module.RLDataset.sample

    def sample(self):
        rec.on_sample()
        return orig_sample(self)

    module.RLDataset.sample = sample
    patched["RLDataset.sample"] = True

    # 2. vLLM: обёртка instance-метода generate у готового движка (там настоящая
    #    генерация и настоящие token_ids; generate_batch их выбрасывает).
    orig_init = module.VLLMGenerator.__init__

    def __init__(self, *a, **kw):
        t0 = time.time()
        orig_init(self, *a, **kw)
        rec.on_engine_init(time.time() - t0)
        orig_generate = self.llm.generate

        def generate(prompts, params, *ga, **gkw):
            t = time.time()
            outs = orig_generate(prompts, params, *ga, **gkw)
            dt = time.time() - t
            gen_tokens = 0
            prompt_tokens = 0
            for o in outs:
                try:
                    gen_tokens += len(o.outputs[0].token_ids)
                    prompt_tokens += len(o.prompt_token_ids or [])
                except (AttributeError, IndexError, TypeError):
                    pass
            rec.on_generate(dt, len(prompts), gen_tokens, prompt_tokens)
            return outs

        self.llm.generate = generate

    module.VLLMGenerator.__init__ = __init__
    patched["VLLMGenerator.__init__/generate"] = True

    # 3. Ресник весов: hot-sync (живой engine) отдельно от прочего ресника.
    orig_hot = module.VLLMGenerator.hot_sync_weights

    def hot_sync_weights(self, model):
        t0 = time.time()
        try:
            return orig_hot(self, model)
        finally:
            rec.on_hotsync(time.time() - t0)

    module.VLLMGenerator.hot_sync_weights = hot_sync_weights
    patched["VLLMGenerator.hot_sync_weights"] = True

    # 4. Чекпоинт, GEN-EVAL, пробы — время стадии, а не обучения.
    for name, hook in (("save_checkpoint_atomic", rec.on_ckpt),
                       ("_general_eval_step", rec.on_gen_eval),
                       ("run_probes", rec.on_probes)):
        orig = getattr(module, name)

        def make(orig, hook):
            def wrapper(*a, **kw):
                t0 = time.time()
                try:
                    return orig(*a, **kw)
                finally:
                    hook(time.time() - t0)
            return wrapper

        setattr(module, name, make(orig, hook))
        patched[name] = True

    return patched


def load_pipeline(path: Path):
    """Импортирует пайплайн как модуль (``main()`` вызовем сами)."""
    spec = importlib.util.spec_from_file_location("laguna_pipeline_under_rl_probe", str(path))
    if spec is None or spec.loader is None:
        raise SystemExit(f"не удалось загрузить пайплайн: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Обвязка RL-разведки: per-step разбивка времени RL-стадии без "
                    "правки пайплайна (аргументы пайплайна — после --).")
    ap.add_argument("--pipeline", required=True, help="путь к laguna_pipeline_v8.py")
    ap.add_argument("--metrics", required=True, help="куда писать JSONL метрик")
    ap.add_argument("--tasks-per-step", type=int, default=DEFAULT_TASKS_PER_STEP,
                    help=f"задач на шаг (граница шага; умолчание пайплайна "
                         f"{DEFAULT_TASKS_PER_STEP})")
    ap.add_argument("--group-size", type=int, default=DEFAULT_GROUP_SIZE,
                    help=f"траекторий на задачу (умолчание {DEFAULT_GROUP_SIZE})")
    ap.add_argument("pipeline_args", nargs=argparse.REMAINDER,
                    help="строка аргументов пайплайна (после --)")
    args = ap.parse_args(argv)
    rest = list(args.pipeline_args)
    if rest and rest[0] == "--":
        rest = rest[1:]
    args.pipeline_args = rest
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pipeline = Path(args.pipeline)
    if not pipeline.is_file():
        print(f"NOT-VERIFIED: пайплайн не найден: {pipeline}", file=sys.stderr)
        return 2
    rec = StepRecorder(Path(args.metrics), args.tasks_per_step, args.group_size)
    module = load_pipeline(pipeline)
    patched = instrument_pipeline(module, rec)

    import transformers
    original = transformers.AutoModelForCausalLM.from_pretrained

    def patched_from_pretrained(*a, **kw):
        return wrap_model_forward(original(*a, **kw), rec)

    transformers.AutoModelForCausalLM.from_pretrained = patched_from_pretrained

    rec.start({"pipeline": str(pipeline), "pipeline_args": args.pipeline_args,
               "patched": sorted(patched), "pid": os.getpid(),
               "tasks_per_step": args.tasks_per_step, "group_size": args.group_size})

    # Стоп по стражу — это `docker stop` → SIGTERM, а не исключение: без
    # обработчика последний блок шага и `end`-запись не были бы записаны, и
    # «остановлено стражем» выглядело бы как «обвязка ничего не увидела».
    finished = {"done": False}

    def finish_once(error: str | None) -> None:
        if not finished["done"]:
            finished["done"] = True
            rec.finish(error)

    def on_signal(signum, _frame):
        finish_once(f"signal {signum}")
        sys.exit(128 + signum)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, on_signal)
        except (ValueError, OSError):  # не главный поток / платформа без сигналов
            pass

    sys.argv = [str(pipeline)] + args.pipeline_args
    error = None
    try:
        module.main()
    except BaseException as e:
        error = f"{type(e).__name__}: {e}"
        finish_once(error)
        raise
    finish_once(None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
