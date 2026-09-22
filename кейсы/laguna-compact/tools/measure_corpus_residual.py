#!/usr/bin/env python3
"""S3ao — остаток корпуса, не попавшего в обучающие чанки: замер, а не догадка.

**Зачем.** Вариант «собрать домен-набор из остатка ``cpt_corpus_v10.1.txt``»
звучит дёшево: остаток тематически доменный и по построению не в обучении. Но
«по построению» — это предположение о чужом коде. Здесь оно превращается в число.

**Как устроена чанковка (и почему наивный счёт неверен).**
``pretokenize_v9.py`` делает три вещи, и каждая влияет на число:

1. режет корпус **по ``\\n---\\n``** (не по пустой строке!) и выбрасывает единицы
   короче 50 символов;
2. кодирует **каждую единицу отдельно** (``tok.encode(doc, add_special_tokens=False)``)
   и склеивает потоки. Кодирование по единицам даёт **меньше** токенов, чем
   кодирование файла целиком: разделители ``\\n---\\n`` и краевые пробелы при
   ``strip`` не попадают в поток, а BPE не сливает токены через границу единицы.
   На срезе 30 МБ расхождение — 0.8 %, то есть на корпусе в 60 млн токенов
   порядка полумиллиона: замер «целиком» здесь ошибся бы на порядок в оценке
   остатка. Поэтому поток воспроизводится **их** способом, а не приблизительно;
3. берёт чанки ``[tokens[j:j+max_len] for j in range(0, len(tokens)-max_len, max_len)]``.
   ``range`` останавливается на ``len(tokens) - max_len``, поэтому **хвост короче
   чанка не попадает никуда**: остаток — это ``tokens[chunks*max_len:]``, и он
   целиком лежит в конце потока.

**Что мерит инструмент.** Воспроизводит поток их способом, сверяет, что число
чанков сходится с сохранённым кэшем (иначе посылка не подтвердилась — это отказ,
а не примечание), и печатает:

* ``tokens_total`` — длина потока; ``tokens_used`` = ``chunks × max_len``;
* ``tokens_residual`` = ``total − used`` и **документы**, в которые он попадает
  (первая единица, чей накопленный конец перешёл границу);
* ``residual_path`` — файл с остатком **в формате набора** (единицы через
  ``\\n---\\n``), чтобы его можно было прогнать гейтом
  ``tools/check_measurement_overlap.py`` как обычный корпус.

Коды возврата::

    0 — замер сделан
    1 — отказ: число чанков не сошлось с кэшем (посылка воспроизведения неверна)
    2 — NOT-VERIFIED: нет корпуса / кэша / токенизатора

Запуск::

    python3 tools/measure_corpus_residual.py \\
        --corpus datasets/cpt_corpus_v10.1.txt \\
        --chunks-npy datasets/tok/cpt_corpus_v10.1_8192_qwen25.npy \\
        --residual-out runs/s3ao-domain-v3-<ts>/v101_residual.txt \\
        --json runs/s3ao-domain-v3-<ts>/v101_residual.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Разделитель единиц корпуса — ровно то, чем режет ``pretokenize_v9.py``.
DOC_SEP = "\n---\n"
#: Фильтр единиц там же: ``len(d) >= 50`` (у прибора замера — строго ``> 50``).
MIN_UNIT_CHARS = 50

#: Спецтокены контура — те же, что добавляет претокенизация. Словарь из-за них
#: расширяется до 151671; без них счётчик был бы числом другого пространства.
SPECIAL_TOKENS = ["<think>", "</think>", "<tool_call>", "</tool_call>",
                  "<tool_response>", "</tool_response>", "<reasoning>", "</reasoning>"]

TOKENIZER_CANDIDATES = [
    "~/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B",
    "/home/user/gb10-shared/models-store/home-roman/huggingface-cache/hub/models--Qwen--Qwen2.5-0.5B",
]


def resolve_tokenizer_path(spec: str) -> str:
    """Путь к токенизатору: каталог кэша hub разворачивается до снапшота.

    ``AutoTokenizer.from_pretrained`` по каталогу ``models--Qwen--Qwen2.5-0.5B``
    падает («нет файла config.json»): файлы лежат не в корне, а в
    ``snapshots/<ревизия>/``. Прибор разворачивает так же.
    """
    p = Path(spec).expanduser()
    if not p.is_dir():
        return spec
    if (p / "config.json").is_file():
        return str(p)
    snaps = sorted((p / "snapshots").glob("*/config.json")) if (p / "snapshots").is_dir() else []
    return str(snaps[-1].parent) if snaps else str(p)


def import_transformers():
    """``transformers`` с обходом дефекта окружения — тем же, что у прибора.

    Импорт падает не из-за нашей задачи: ``transformers 4.44.2`` требует
    ``huggingface-hub <1.0``, а установлен 1.27.0. Обход живёт в
    ``tools/ppl_probe.py:import_transformers`` и **переиспользуется**, а не
    повторяется: своя копия разошлась бы с прибором на первой правке, и «тем же
    токенизатором» стало бы утверждением без основания.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ppl_probe", Path(__file__).resolve().parent / "ppl_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.import_transformers()


def build_tokenizer(spec: str, transformers):
    """Токенизатор претокенизации: та же ревизия, те же спецтокены."""
    tok = transformers.AutoTokenizer.from_pretrained(spec, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    tok.add_special_tokens({"additional_special_tokens": SPECIAL_TOKENS})
    return tok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True, help="корпус целиком (тот, что претокенизировался)")
    ap.add_argument("--chunks-npy", required=True, help="кэш чанков (npy: chunks × max_len)")
    ap.add_argument("--tokenizer", default=TOKENIZER_CANDIDATES[0],
                    help="ревизия токенизатора (путь кэша или repo id)")
    ap.add_argument("--residual-out", help="куда записать остаток в формате набора")
    ap.add_argument("--json", help="куда записать машинный отчёт")
    ap.add_argument("--limit", type=int, default=0,
                    help="обрезать корпус до N символов (для быстрой прикидки)")
    a = ap.parse_args()

    corpus, npy = Path(a.corpus), Path(a.chunks_npy)
    for p, what in ((corpus, "корпус"), (npy, "кэш чанков")):
        if not p.is_file():
            print(f"NOT-VERIFIED: нет {what}: {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
    tok_spec = resolve_tokenizer_path(a.tokenizer)

    try:
        import numpy as np
    except ImportError as exc:
        print(f"NOT-VERIFIED: нет зависимости: {exc}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    try:
        transformers, shim = import_transformers()
    except Exception as exc:
        print(f"NOT-VERIFIED: transformers не импортируется: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED
    try:
        tok = build_tokenizer(tok_spec, transformers)
    except Exception as exc:
        print(f"NOT-VERIFIED: токенизатор не загрузился: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    arr = np.load(npy, mmap_mode="r")
    if arr.ndim != 2:
        print(f"NOT-VERIFIED: кэш чанков не двумерный: shape={arr.shape}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    chunks, max_len = int(arr.shape[0]), int(arr.shape[1])
    tokens_used = chunks * max_len

    text = corpus.read_text(encoding="utf-8", errors="replace")
    if a.limit:
        text = text[:a.limit]
    units = [d.strip() for d in text.split(DOC_SEP)]
    units = [d for d in units if len(d) >= MIN_UNIT_CHARS]

    cumulative = 0
    cutoff_unit = len(units)          # первая единица остатка
    for i, u in enumerate(units):
        n = len(tok.encode(u, add_special_tokens=False))
        if cumulative + n > tokens_used:
            cutoff_unit = i
            cumulative += n
            break
        cumulative += n
    else:
        cutoff_unit = len(units)
    total = cumulative if cutoff_unit < len(units) else cumulative
    residual_units = units[cutoff_unit:]
    residual_tokens = total - tokens_used
    residual_text = DOC_SEP.join(residual_units) + ("\n" if residual_units else "")

    # Посылка воспроизведения: чанков должно выйти ровно столько, сколько в кэше.
    predicted = max(0, math.ceil((total - max_len) / max_len)) if total > max_len else 0
    predicted = max(0, (total - max_len + max_len - 1) // max_len) if total > max_len else 0
    chunks_match = predicted == chunks

    report = {
        "tool": "measure_corpus_residual.py",
        "corpus": str(corpus),
        "corpus_sha256_note": "хеш корпуса не снимается: файл читается только на чтение",
        "chunks_npy": str(npy),
        "chunks": chunks, "max_len": max_len,
        "tokens_used": tokens_used,
        "tokens_total": total,
        "tokens_residual": residual_tokens,
        "units_total": len(units),
        "residual_units": len(residual_units),
        "residual_chars": len(residual_text),
        "residual_first_unit_head": (residual_units[0][:200].replace("\n", " ")
                                     if residual_units else None),
        "chunks_predicted_from_total": predicted,
        "chunks_match": chunks_match,
        "tokenizer": tok_spec,
        "tokenizer_vocab": len(tok),
        "env_shims": [shim] if shim else [],
        "method": ("поток воспроизведён способом pretokenize_v9: разрез по \\n---\\n, "
                   "фильтр ≥50 симв., кодирование каждой единицы отдельно"),
        "limit_chars": a.limit or None,
    }
    if a.residual_out and residual_units:
        out = Path(a.residual_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(residual_text, encoding="utf-8")
        report["residual_path"] = str(out)
    if a.json:
        jp = Path(a.json)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"корпус:            {corpus}")
    print(f"единиц (\\n---\\n):  {len(units)}")
    print(f"чанков в кэше:     {chunks} × {max_len} = {tokens_used} токенов в обучении")
    print(f"токенов в потоке:  {total} (предсказано чанков из этого числа: {predicted})")
    print(f"ОСТАТОК:           {residual_tokens} токенов, {len(residual_units)} единиц, "
          f"{len(residual_text)} символов")
    if residual_units:
        print(f"начало остатка:    «{report['residual_first_unit_head'][:120]}…»")

    if not chunks_match:
        print(f"\nОТКАЗ: воспроизведение не сошлось: из потока вышло бы {predicted} чанков, "
              f"в кэше {chunks} (расхождение {predicted - chunks} × {max_len} ≈ "
              f"{(predicted - chunks) * max_len} токенов). Числа остатка читать нельзя "
              f"как «то, что не попало в обучение»", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
