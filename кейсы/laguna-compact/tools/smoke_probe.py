#!/usr/bin/env python3
"""Обвязка смоука: per-step метрики стадии контура **без правки пайплайна**.

Зачем обвязка. Пайплайн `laguna_pipeline_v8.py` логирует loss и tok/s раз в
50 шагов (`step % 50 == 0`), а энтропию не логирует вовсе — этим он и хорош для
многочасовых прогонов. Смоук S2 живёт 20–50 шагов, и на таком окне встроенные
логи дают **одну** точку loss и ноль точек энтропии, то есть приёмку («loss
убывает», «entropy в коридоре») проверить нечем. Правильный ответ — измерять
снаружи, не трогая родительский контур (он read-only, AD-4/AD-7): пайплайн
запускается со своей же строкой аргументов, а метрики собирает обёртка.

Что именно измеряется, по шагам (каждый шаг = один обучающий forward):

* ``loss`` — значение, которое вернула модель (то же, что попадает в backward);
* ``tokens`` — токены шага (batch × длина) и ``dt`` — время forward+backward:
  ``tok/s`` считается как ``Σtokens / Σdt``, то есть end-to-end по стадии, а не
  по одному удачному шагу;
* ``entropy`` — средняя токенная энтропия распределения модели на
  **супервизируемых** позициях (``labels != -100``), по подвыборке позиций с
  шагом ``--entropy-pos-stride`` (иначе матрица логитов [B, L, V] превращает
  замер в ещё один источник памяти на unified-памяти GB10). Это методика
  измерения, а не «энтропия контура»: пайплайн её не определяет.

Записываются только обучающие проходы: считается ``model.training`` и
``torch.is_grad_enabled()``, поэтому eval-проходы (`_general_eval_step`,
`_ppl_eval`, `run_probes`/``generate``) в метрики не попадают и не портят ни
loss-тренд, ни tok/s. Число пропущенных проходов пишется в итоговую запись —
чтобы «мало шагов» было видно, а не выглядело как «стадия не запускалась».

Время шага — **интервал между началами обучающих forward'ов**, а не длительность
forward'а: backward и ``optimizer.step()`` живут вне ``model(...)``, и замер
только forward'а занизил бы время шага в разы (на замере 0.5B — 0.02 с против
0.3 с). Eval-проходы стоят **между** обучающими шагами и растянули бы интервал,
поэтому у каждого шага пишется ``skipped_before``, и в tok/s идут только
интервалы без eval'ов внутри. Причина не косметическая: сам пайплайн считает
свой ``tok/s`` как ``(step+1)*batch_tokens/(time-t0)`` при ``t0`` перед циклом
(``laguna_pipeline_v8.py:760,778``), то есть **включая** GEN-EVAL, — с таким
числом нельзя ни сравнить конфигурации, ни поймать регресс шага.

Детерминированный режим (``--deterministic on``, ADR-009). Обвязка — единственное
место, где режим включается **до** первого обращения к CUDA: пайплайн
``use_deterministic_algorithms`` не вызывает, а после создания модели включать его
поздно. Что выставляется: ``torch.use_deterministic_algorithms(True)``,
``cudnn.deterministic``/``benchmark``, ``CUBLAS_WORKSPACE_CONFIG=:4096:8`` (env
читается cuBLAS при создании handle; если переменной нет, обвязка выставляет её —
и честно пишет, что выставила, а не что «так и было»). Сид остаётся делом
пайплайна (``--seed``, ``torch.manual_seed`` + ``torch.cuda.manual_seed_all``):
вторая точка посева в обвязке сделала бы невоспроизводимым сравнение с прогонами
без неё. Фактическое состояние режима пишется в записи ``start`` и ``end``
метрик — вердикт о детерминизме опирается на замер, а не на флаг запуска.

Метрики — JSONL: первая запись ``{"event": "start"}``, далее по записи на
шаг, последняя ``{"event": "end"}`` с пиком памяти torch и ошибкой, если
стадия упала. Обвязка **пробрасывает** исключение дальше: код возврата —
это код возврата стадии.

Запуск (внутри контейнера; строка аргументов пайплайна — после ``--``)::

    python3 smoke_probe.py --metrics /workspace/experiments/<run>/probe_cpt.jsonl \\
        --pipeline /workspace/shared/laguna_pipeline_v8.py \\
        -- --stage cpt --model_name Qwen/Qwen2.5-0.5B --max_steps 50 ...

Коды возврата: 0 — стадия прошла; не 0 — код стадии или код argparse.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

#: Величины по умолчанию для замера энтропии (см. docstring).
DEFAULT_POS_STRIDE = 16
DEFAULT_BLOCK = 64


def entropy_of_logits(logits, labels, pos_stride: int, block_rows: int) -> float | None:
    """Средняя токенная энтропия (наты) на супервизируемых позициях.

    ``logits`` [B, L, V], ``labels`` [B, L] (``-100`` = позиция не
    супервизируется). Позиции берутся с шагом ``pos_stride``; логиты
    обрабатываются блоками по ``block_rows`` строк, чтобы пик памяти не зависел
    от длины последовательности.
    """
    import torch

    if logits is None or labels is None:
        return None
    with torch.no_grad():
        b, seq = labels.shape[0], labels.shape[1]
        rows = []
        for i in range(b):
            valid = (labels[i] != -100).nonzero(as_tuple=True)[0]
            if valid.numel() == 0:
                continue
            rows.append(valid[::max(1, pos_stride)])
        if not rows:
            return None
        total, n = 0.0, 0
        for start in range(0, len(rows), max(1, block_rows)):
            chunk = rows[start:start + max(1, block_rows)]
            idx = torch.cat(chunk)
            batch_idx = torch.cat([torch.full_like(c, i) for i, c in enumerate(chunk)])
            picked = logits[batch_idx, idx, :].float()
            logp = torch.log_softmax(picked, dim=-1)
            ent = -(logp.exp() * logp).sum(dim=-1)
            total += float(ent.sum().item())
            n += int(ent.numel())
            del picked, logp, ent
    return total / n if n else None


class Recorder:
    """Пишет JSONL-поток метрик стадии; каждая строка — самостоятельная запись."""

    def __init__(self, path: Path, entropy_every: int, determinism: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entropy_every = max(1, entropy_every)
        #: Режим детерминизма — в обеих граничных записях (ADR-009): вердикт
        #: «прогон шёл в детерминированном режиме» должен опираться на запись
        #: самой стадии, а не на флаг, который кто-то передал снаружи.
        self.determinism = determinism or {"requested": False}
        self.steps = 0
        self.skipped_forwards = 0
        self.skipped_seconds = 0.0
        self.started_at = time.time()
        self._last_train_start: float | None = None
        self._skipped_since = 0
        self._eval_seconds_since = 0.0
        self._fh = self.path.open("w", encoding="utf-8")

    def _emit(self, obj: dict) -> None:
        self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._fh.flush()

    def start(self, meta: dict) -> None:
        self.started_at = time.time()
        #: Режим подставляется из самого Recorder'а, а не из переданной меты: одно
        #: место правды. Иначе запись «прогон шёл детерминированно» держалась бы на
        #: том, что вызывающий не забыл положить её в meta.
        payload = {"event": "start", "ts": self.started_at, "determinism": self.determinism}
        payload.update(meta)
        self._emit(payload)

    def note_skipped(self, seconds: float) -> None:
        """Проход не является обучающим (eval/generate): он растягивает интервал."""
        self.skipped_forwards += 1
        self.skipped_seconds += seconds
        self._skipped_since += 1
        self._eval_seconds_since += seconds

    def step(self, t_start: float, forward_seconds: float, tokens: int, loss: float,
             entropy: float | None) -> None:
        dt_step = (None if self._last_train_start is None
                   else round(t_start - self._last_train_start, 4))
        self._emit({"event": "step", "i": self.steps, "ts": t_start,
                    "dt": round(forward_seconds, 4), "dt_step": dt_step,
                    "tokens": tokens, "loss": round(loss, 6),
                    "entropy": None if entropy is None else round(entropy, 6),
                    "skipped_before": self._skipped_since,
                    "eval_seconds_before": round(self._eval_seconds_since, 3)})
        self._last_train_start = t_start
        self._skipped_since = 0
        self._eval_seconds_since = 0.0
        self.steps += 1

    def finish(self, error: str | None = None) -> None:
        extra: dict = {"event": "end", "ts": time.time(),
                       "train_steps": self.steps,
                       "skipped_forwards": self.skipped_forwards,
                       "skipped_seconds": round(self.skipped_seconds, 3),
                       "probe_seconds": round(time.time() - self.started_at, 3),
                       "determinism": self.determinism,
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


def wrap_model(model, recorder: Recorder, cfg: dict):
    """Оборачивает ``forward`` модели: метрики на обучающих проходах.

    Уровень обёртки — метод экземпляра, а не подмена класса: пайплайн получает
    обычную модель (resize_token_embeddings, gradient checkpointing, peft —
    работают как есть), а мы видим ровно те вызовы, что делает стадия.
    """
    import torch

    original = model.forward
    stride = cfg["pos_stride"]
    block = cfg["block"]

    def forward(*args, **kwargs):
        training = bool(getattr(model, "training", False)) and torch.is_grad_enabled()
        t_start = time.time()
        if not training:
            out = original(*args, **kwargs)
            recorder.note_skipped(time.time() - t_start)
            return out
        out = original(*args, **kwargs)
        dt = time.time() - t_start
        loss = getattr(out, "loss", None)
        if loss is None:
            recorder.note_skipped(dt)
            return out
        input_ids = kwargs.get("input_ids")
        if input_ids is None and args:
            input_ids = args[0]
        tokens = int(input_ids.numel()) if hasattr(input_ids, "numel") else 0
        entropy = None
        if cfg["entropy"] and recorder.steps % recorder.entropy_every == 0:
            try:
                entropy = entropy_of_logits(getattr(out, "logits", None),
                                            kwargs.get("labels"), stride, block)
            except Exception as e:  # энтропия — дополнительная величина, не барьер
                entropy = None
                print(f"[probe] entropy недоступна: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
        recorder.step(t_start, dt, tokens, float(loss.detach().item()), entropy)
        return out

    model.forward = forward
    return model


def enable_determinism() -> dict:
    """Включает детерминированный режим вычислений и возвращает его фактическое состояние.

    Вызывается **до** загрузки пайплайна и до любого обращения к CUDA: часть
    настроек (``CUBLAS_WORKSPACE_CONFIG``) читается при создании handle cuBLAS, и
    после первого матмула они уже не действуют. Возвращается отчёт о факте, а не о
    намерении — в отчёте по детерминизму (ADR-009) нужны значения, при которых
    прогон действительно шёл.
    """
    import os

    import torch

    det: dict = {"requested": True}
    cfg = os.environ.get("CUBLAS_WORKSPACE_CONFIG")
    if cfg:
        det["cublas_workspace_config"] = cfg
        det["cublas_workspace_config_source"] = "env запуска (docker -e)"
    else:
        #: Фолбэк: без этой переменной cuBLAS сам выбирает workspace и часть
        #: редукций становится недетерминированной. Помечаем источник честно —
        #: «выставила обвязка» и «пришло из env запуска» это разные уровни
        #: гарантии, потому что env действует с самого старта процесса.
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
        det["cublas_workspace_config"] = ":4096:8"
        det["cublas_workspace_config_source"] = ("выставлен обвязкой (в env запуска "
                                                 "переменной не было — гарантия слабее)")
    torch.use_deterministic_algorithms(True)
    det["use_deterministic_algorithms"] = bool(torch.are_deterministic_algorithms_enabled())
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    det["cudnn_deterministic"] = bool(torch.backends.cudnn.deterministic)
    det["cudnn_benchmark"] = bool(torch.backends.cudnn.benchmark)
    try:
        det["cuda_matmul_allow_tf32"] = bool(torch.backends.cuda.matmul.allow_tf32)
        det["cudnn_allow_tf32"] = bool(torch.backends.cudnn.allow_tf32)
    except Exception as e:  # настройка не обязательна для вердикта
        det["tf32_error"] = f"{type(e).__name__}: {e}"
    det["cuda_available"] = bool(torch.cuda.is_available())
    det["torch_version"] = torch.__version__
    #: Сид ставит пайплайн (`torch.manual_seed(args.seed)` + `cuda.manual_seed_all`):
    #: вторая точка посева здесь сдвинула бы RNG относительно прогонов без обвязки.
    det["seeding"] = "пайплайн (--seed); обвязка сид не ставит"
    return det


def load_pipeline(path: Path):
    """Импортирует пайплайн как модуль (``main()`` вызовем сами)."""
    spec = importlib.util.spec_from_file_location("laguna_pipeline_under_smoke", str(path))
    if spec is None or spec.loader is None:
        raise SystemExit(f"не удалось загрузить пайплайн: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Обвязка смоука: per-step loss/tok-s/entropy стадии контура без "
                    "правки пайплайна (аргументы пайплайна — после --).")
    ap.add_argument("--pipeline", required=True, help="путь к laguna_pipeline_v8.py")
    ap.add_argument("--metrics", required=True, help="куда писать JSONL метрик")
    ap.add_argument("--entropy", default="on", choices=["on", "off"],
                    help="считать ли токенную энтропию (по умолчанию on)")
    ap.add_argument("--entropy-every", type=int, default=1,
                    help="считать энтропию каждый N-й обучающий шаг")
    ap.add_argument("--entropy-pos-stride", type=int, default=DEFAULT_POS_STRIDE,
                    help=f"шаг подвыборки позиций для энтропии (по умолчанию {DEFAULT_POS_STRIDE})")
    ap.add_argument("--entropy-block", type=int, default=DEFAULT_BLOCK,
                    help=f"сколько строк логитов обрабатывать за раз (по умолчанию {DEFAULT_BLOCK})")
    ap.add_argument("--stage", default=None, help="имя стадии — только для записи в метрики")
    ap.add_argument("--exp-name", default=None, help="имя эксперимента — только для метрик")
    ap.add_argument("--deterministic", default="off", choices=["on", "off"],
                    help="детерминированный режим вычислений (ADR-009): "
                         "use_deterministic_algorithms + cudnn.deterministic + "
                         "CUBLAS_WORKSPACE_CONFIG; по умолчанию off — как в прогонах контура")
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
    #: Режим включается до загрузки пайплайна: `CUBLAS_WORKSPACE_CONFIG` действует
    #: только до создания handle cuBLAS, а он появится на первом же матмуле стадии.
    det = enable_determinism() if args.deterministic == "on" else {
        "requested": False, "reason": "--deterministic off (режим контура по умолчанию)"}
    rec = Recorder(Path(args.metrics), args.entropy_every, determinism=det)
    cfg = {"entropy": args.entropy == "on", "pos_stride": args.entropy_pos_stride,
           "block": args.entropy_block}
    module = load_pipeline(pipeline)

    import transformers
    original = transformers.AutoModelForCausalLM.from_pretrained

    def patched(*a, **kw):
        model = original(*a, **kw)
        return wrap_model(model, rec, cfg)

    transformers.AutoModelForCausalLM.from_pretrained = patched

    rec.start({"pipeline": str(pipeline), "pipeline_args": args.pipeline_args,
               "stage": args.stage, "exp_name": args.exp_name,
               "entropy": cfg, "entropy_every": rec.entropy_every,
               "pid": os.getpid()})
    sys.argv = [str(pipeline)] + args.pipeline_args
    error = None
    try:
        module.main()
    except BaseException as e:
        error = f"{type(e).__name__}: {e}"
        rec.finish(error)
        raise
    rec.finish(None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
