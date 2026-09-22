#!/usr/bin/env python3
"""S3m — сборка преток-кэшей CPT-микса v12r50 (доля replay 50 %).

Зачем новый микс. ADR-022 п.2: пилот (v12r, 75/25) разрушил общий язык (×8–16 при
базе 11.93), и среди двух оставшихся факторов — доля replay и пик LR — первый
проверяется только миксом с другой долей. v12r50 — это тот же рецепт v12r, у
которого доля replay поднята с 25 % до 50 %.

Почему доля 50 % недостижима при полном доменном пуле. Источник реплея
(`general_replay_ru.txt`, wikipedia 20231101.ru) даёт ровно **3493** чанка по
8192 токена — это потолок, а не выбор. Полный доменный пул v12r — 7332 чанка;
50 % из него требовали бы 7332 реплей-чанков. Поэтому микс собран **равными
долями**: 3493 домена + 3493 реплея. Существенно, что обе части — **префиксы**
соответствующих частей v12r: домен берётся первыми 3493 чанками того же потока,
реплей — первыми 3493 (у v12r это первые 2444). То есть материал нового микса
не «другой корпус», а подмножество того же.

Что НЕ переносится и названо явно: доменный пул v12r50 (3493) вдвое меньше
доменного пула v12r (7332). В окне 2000 шагов это влияет не на состав, а на
разнообразие доменного материала — но это содвинутый фактор, и он назван в
карточке (`coupled_changes`) и в ограничениях evidence, а не замолчан.

Порядок сборки (дословно тот же, что в `build_cpt_v12r_mix.py` +
`build_cpt_v12r_pos.py`): чанковка `[tokens[j:j+L] for j in range(0, len-L, L)]`,
конкатенация домен+реплей, `RandomState(seed).shuffle` по оси чанков. Ровно тот же
порядок применён к position_ids (компаньон для диагонального маскирования).

Самопроверка процедуры. Перед сборкой нового микса инструмент **воспроизводит**
существующий v12r (7332 + 2444, seed 42) и ассертит побайтовое совпадение с
`cpt_corpus_v12r_8192_{tag}.npy` и его pos-компаньоном. Совпало — значит процедура
повторена, и отличие v12r50 от v12r объясняется долями, а не дрейфом кода.
Существующие файлы только читаются: запись идёт в новые имена.

Коды возврата::

    0 — кэши v12r50 собраны (или уже были), самопроверка пройдена
    1 — самопроверка не прошла: сборка не воспроизводит v12r (отказ, писать нельзя)
    2 — NOT-VERIFIED: нет входа (источник/токенизатор/доменный кэш)

Запуск::

    python3 tools/build_mix_v12r50.py --plan
    python3 tools/build_mix_v12r50.py --datasets-dir /home/user/gb10-shared/datasets \\
        --tag qwen25 --report data/mix-v12r50-build.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def note(msg: str) -> None:
    """Прогресс сборки — в stdout с отметкой времени.

    Сборка считает десятки миллионов токенов, и «молчащий процесс» неотличим от
    зависшего: фазы печатаются, чтобы время ожидания было видно, а не угадывалось.
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Рецепт v12r — из `build_cpt_v12r_mix.py` (30.08). Числа не «примерно те же»:
#: 7332 = доменных чанков v10.1, 2444 = реплей-чанков v12r, seed 42 — шафл.
V12R_DOMAIN_CHUNKS = 7332
V12R_REPLAY_CHUNKS = 2444
V12R_SEED = 42

#: Спецтокены контура — единое токен-пространство (AD-3). Тот же набор, что в
#: `build_cpt_v12r_mix.py` и `pretokenize_v9`.
SPECIAL_TOKENS = ["<think>", "</think>", "<tool_call>", "</tool_call>",
                  "<tool_response>", "</tool_response>", "<reasoning>", "</reasoning>"]

#: Разделители и фильтры документов — как в оригинальных сборщиках: домен
#: разбит `\n---\n`, реплей — пустой строкой; документы короче 50 символов
#: отбрасываются в обоих.
DOMAIN_SEP = "\n---\n"
REPLAY_SEP = "\n\n"
MIN_DOC_CHARS = 50


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _own_revision_sha256() -> str:
    """sha256 файла этого инструмента — или отказ, если файла на диске нет.

    Отдельная проверка, потому что «хеш не посчитался» здесь не мелочь: карточка
    ссылается на ревизию сборщика, и без неё происхождение кэша недоказуемо.
    """
    me = Path(__file__)
    if not me.is_file():
        raise SystemExit(
            f"ОТКАЗ: инструмент запущен без файла на диске ({me}) — ревизия сборщика "
            f"недоказуема. Запускай файлом (python3 tools/build_mix_v12r50.py), "
            f"а не потоком со stdin")
    return sha256_file(me)


def chunk_tokens(tokens: list[int], max_len: int) -> list[list[int]]:
    """Чанковка оригинала: нехвостовые блоки max_len, хвост < max_len отброшен.

    Оставлена как **эталон рецепта** (её проверяют тесты). Исполняемый путь —
    ``chunks_from_stream``: она даёт те же чанки, но без списка списков (на 60 млн
    токенов список Python-объектов — это гигабайты и минуты на конвертацию).
    Эквивалентность закреплена тестом, а не комментарием.
    """
    return [tokens[j:j + max_len] for j in range(0, len(tokens) - max_len, max_len)]


def n_chunks(total_tokens: int, max_len: int) -> int:
    """Сколько нехвостовых чанков в потоке — арифметика ``range(0, T-L, L)``."""
    return max(0, -(-(total_tokens - max_len) // max_len))


def chunks_from_stream(tokens, max_len: int, np, limit: int | None = None):
    """Чанки потока как массив [N, max_len]: конкатенация нехвостовых блоков.

    Блоки ``chunk_tokens`` — непрерывные непересекающиеся срезы потока, поэтому
    плоский массив + ``reshape`` даёт ровно тот же результат. Поток **обрезается**
    до ``N*max_len``: хвост короче чанка отброшен рецептом, и reshape без обрезки
    падает на размере (эта ошибка ловится тестом на синтетике, а не на 60 млн
    токенов).
    """
    k = n_chunks(len(tokens), max_len)
    if limit is not None:
        k = min(k, limit)
    flat = np.asarray(tokens[:k * max_len], dtype=np.int32)
    return flat.reshape(k, max_len)


def positions_for_stream(starts, tokens_len: int, max_len: int, np,
                         limit: int | None = None):
    """position_ids потока как массив [N, max_len] — та же обрезка, что у чанков."""
    k = n_chunks(tokens_len, max_len)
    if limit is not None:
        k = min(k, limit)
    flat = pos_stream(starts, k * max_len, np)
    return np.asarray(flat, dtype=np.int32).reshape(k, max_len)


def stream_with_starts(path: Path, sep: str, min_chars: int, tokenizer, log=None):
    """Токены документов потоком + стартовые смещения (для position_ids)."""
    text = path.read_text()
    docs = []
    for part in text.split(sep):
        d = part.strip()
        if len(d) >= min_chars:
            docs.append(d)
    del text
    tokens: list[int] = []
    starts: list[int] = []
    for i, d in enumerate(docs):
        starts.append(len(tokens))
        tokens.extend(tokenizer.encode(d, add_special_tokens=False))
        if log and i and i % 20000 == 0:
            log(f"  токенизация: {i}/{len(docs)} док, {len(tokens)} ток")
    return tokens, starts, len(docs)


def pos_stream(starts, total_tokens: int, np):
    """position_ids всего потока: смещение от старта своего документа.

    Одним ``searchsorted`` на весь поток, а не по вызову на чанк: результат тот же
    (``pos_for_range`` на срезе), а работы — на порядок меньше.
    """
    starts_arr = np.asarray(starts, dtype=np.int64)
    idx = np.searchsorted(starts_arr, np.arange(total_tokens, dtype=np.int64), side="right") - 1
    return np.arange(total_tokens, dtype=np.int64) - starts_arr[idx]


def pos_for_range(starts: list[int], lo: int, hi: int, np):
    """position_ids потока [lo, hi): смещение от старта своего документа."""
    idx = np.searchsorted(starts, np.arange(lo, hi), side="right") - 1
    return np.arange(lo, hi, dtype=np.int64) - np.asarray(starts, dtype=np.int64)[idx]


def build_arrays(dom_chunks, rep_chunks, dom_pos, rep_pos, n_dom, n_rep, seed, np):
    """Конкатенация домен+реплей и шафл — ровно как в оригинальных сборщиках."""
    mixed = np.concatenate([np.asarray(dom_chunks[:n_dom], dtype=np.int32),
                            np.asarray(rep_chunks[:n_rep], dtype=np.int32)], axis=0)
    np.random.RandomState(seed).shuffle(mixed)
    mixed_pos = np.concatenate([np.asarray(dom_pos[:n_dom], dtype=np.int32),
                                np.asarray(rep_pos[:n_rep], dtype=np.int32)], axis=0)
    np.random.RandomState(seed).shuffle(mixed_pos)
    return mixed, mixed_pos


#: Кэш корпуса лежит рядом с прочими (`datasets/tok`), а этот каталог на стенде
#: принадлежит root: сборщик запускается **в контейнере стенда**, как и оригинальные
#: `build_cpt_v12r_*.py` («CPU-only, запуск в docker на GB10»). Поэтому инструмент
#: не привязан к каталогу кейса: локальные заглушки импортируются, если доступны.
ENV_SHIMS = {"from": None, "why": None}


def load_tokenizer(tag: str, model_id: str):
    """Токенизатор + 8 спецтокенов — как в `build_cpt_v12r_mix.py`.

    Окружение **локальной** машины сломано (см. `tools/ppl_probe.py`), поэтому
    заглушки берутся оттуда же, а не пишутся второй раз: две копии обхода — две
    правды о том, что было обойдено. На стенде заглушки не нужны: там окружение
    целое, а `ppl_probe` рядом нет — это не «тихо пропущенный обход», а другой
    путь, и он записывается в отчёт (`env_shims`).
    """
    tools_dir = Path(__file__).resolve().parent
    if (tools_dir / "ppl_probe.py").is_file():
        import sys as _sys
        if str(tools_dir) not in _sys.path:
            _sys.path.insert(0, str(tools_dir))
    try:
        import ppl_probe as P
    except ImportError:
        #: Путь стенда: окружение целое, `ppl_probe` рядом нет — токенизатор берётся
        #: по id из локального HF-кэша контейнера (HF_HUB_OFFLINE=1 в запуске).
        ENV_SHIMS["from"] = "не требуются (окружение без известных дефектов)"
        ENV_SHIMS["why"] = ("tools/ppl_probe.py недоступен — путь стенда "
                            "(сборка идёт в контейнере, токенизатор из HF-кэша)")
        from transformers import AutoTokenizer
        tok_dir = Path(f"hf-cache:{model_id}")
        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True,
                                                  local_files_only=True)
    else:
        P.repair_broken_pyopenssl()
        P.import_transformers()
        from transformers import AutoTokenizer
        ENV_SHIMS["from"] = "tools/ppl_probe.py (заглушки локальной машины)"
        ENV_SHIMS["why"] = "pyOpenSSL сломан в установленной cryptography; версия transformers снята"
        tok_dir, missed = P.locate(P.TOKENIZER_CANDIDATES, "tokenizer.json")
        if tok_dir is None:
            raise SystemExit(f"NOT-VERIFIED: токенизатор {tag} не найден: {'; '.join(missed)}")
        tokenizer = AutoTokenizer.from_pretrained(str(tok_dir), local_files_only=True)
    before = len(tokenizer)
    tokenizer.add_special_tokens({"additional_special_tokens": SPECIAL_TOKENS})
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    return tokenizer, {"tokenizer_dir": str(tok_dir), "vocab_before": before,
                       "vocab_after": len(tokenizer), "model_id": model_id}


def run(args) -> tuple[dict, int]:
    import numpy as np  # noqa: E402 — тяжёлый импорт только на исполнении

    datasets = Path(args.datasets_dir)
    tok_dir = datasets / "tok"
    domain_txt = datasets / args.domain_txt
    general_txt = datasets / args.general_txt
    dom_cache = tok_dir / f"{args.domain_stem}_{args.max_len}_{args.tag}.npy"
    v12r_cache = tok_dir / f"{args.out_stem_base}_{args.max_len}_{args.tag}.npy"
    v12r_pos = tok_dir / f"{args.out_stem_base}_{args.max_len}_{args.tag}_pos.npy"

    missing = [str(p) for p in (domain_txt, general_txt) if not p.is_file()]
    if missing:
        print("NOT-VERIFIED: нет источника: " + "; ".join(missing), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED
    if not dom_cache.is_file() and not args.skip_v12r_check:
        print(f"NOT-VERIFIED: нет доменного кэша {dom_cache}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    tokenizer, tok_prov = load_tokenizer(args.tag, args.model_id)
    report: dict = {
        "schema": "mix-v12r50-build/1",
        "stage": "S3m",
        "date": datetime.now(timezone.utc).isoformat(),
        #: Чем собран кэш — файл + хеш **той самой** ревизии инструмента. Без этого
        #: карточка ссылалась бы на имя, которое к моменту чтения уже другое.
        #: Запуск «скриптом со stdin» (`python3 -`) ревизии не оставляет, поэтому
        #: это отказ, а не `null` в поле: недоказуемое происхождение кэша хуже
        #: отсутствующего отчёта.
        "tool": {"path": "tools/build_mix_v12r50.py",
                 "sha256": _own_revision_sha256(), "argv": sys.argv[1:]},
        "recipe": {"chunk_len": args.max_len, "seed": args.seed,
                   "shuffle": "numpy RandomState(seed).shuffle по оси чанков",
                   "domain_sep": repr(DOMAIN_SEP), "replay_sep": repr(REPLAY_SEP),
                   "min_doc_chars": MIN_DOC_CHARS,
                   "chunking": "нехвостовые блоки max_len, хвост отброшен"},
        "tokenizer": tok_prov,
        "env_shims": dict(ENV_SHIMS),
        "sources": {},
        "fidelity_check": {},
    }

    # ── 1) потоки: домен и реплей, те же сплиты/фильтры, что в оригинале ──────
    t0 = time.time()
    dom_tokens, dom_starts, n_dom_docs = stream_with_starts(
        domain_txt, DOMAIN_SEP, MIN_DOC_CHARS, tokenizer, log=note)
    note(f"домен токенизирован за {time.time() - t0:.0f} с")
    t0 = time.time()
    rep_tokens, rep_starts, n_rep_docs = stream_with_starts(
        general_txt, REPLAY_SEP, MIN_DOC_CHARS, tokenizer, log=note)
    note(f"реплей токенизирован за {time.time() - t0:.0f} с")
    t0 = time.time()
    dom_chunks = chunks_from_stream(dom_tokens, args.max_len, np)
    rep_chunks = chunks_from_stream(rep_tokens, args.max_len, np)
    dom_pos = positions_for_stream(dom_starts, len(dom_tokens), args.max_len, np)
    rep_pos = positions_for_stream(rep_starts, len(rep_tokens), args.max_len, np)
    note(f"чанки и position_ids собраны за {time.time() - t0:.0f} с "
         f"(домен {dom_chunks.shape}, реплей {rep_chunks.shape})")
    report["sources"] = {
        "domain": {"path": str(domain_txt), "bytes": domain_txt.stat().st_size,
                   "sha256": sha256_file(domain_txt), "docs": n_dom_docs,
                   "tokens": len(dom_tokens), "chunks_available": len(dom_chunks)},
        "replay": {"path": str(general_txt), "bytes": general_txt.stat().st_size,
                   "sha256": sha256_file(general_txt), "docs": n_rep_docs,
                   "tokens": len(rep_tokens), "chunks_available": len(rep_chunks)},
    }
    print(f"домен:  {n_dom_docs} док → {len(dom_tokens)} ток → {len(dom_chunks)} чанков")
    print(f"реплей: {n_rep_docs} док → {len(rep_tokens)} ток → {len(rep_chunks)} чанков")

    # ── 2) самопроверка: сборка воспроизводит существующий v12r ─────────────
    #: Две разные сверки, и путать их нельзя:
    #:  (а) доменный вход — пересчитанная чанковка против доменного кэша v10.1;
    #:  (б) вся процедура — пересобранный микс против кэша v12r (9776×8192).
    #: Сверка (а) против кэша v12r падает на формах: 7332 ≠ 9776.
    if not args.skip_v12r_check:
        same_dom, same_tok, same_pos = None, None, None
        if dom_cache.is_file():
            dom_ref = np.asarray(np.load(str(dom_cache), mmap_mode="r"), dtype=np.int32)
            if dom_ref.shape != dom_chunks.shape:
                print(f"NOT-VERIFIED: доменный кэш {dom_ref.shape} против пересчитанных "
                      f"{dom_chunks.shape} чанков", file=sys.stderr)
                return {}, EXIT_NOT_VERIFIED
            same_dom = bool(np.array_equal(dom_ref, dom_chunks))
            if not same_dom:
                print(f"ОТКАЗ: доменная чанковка не совпала с кэшем v10.1 "
                      f"({int((dom_ref != dom_chunks).sum())} расхождений)", file=sys.stderr)
                return {}, EXIT_FAIL
            del dom_ref
        if not v12r_cache.is_file():
            print(f"NOT-VERIFIED: нет кэша v12r для сверки процедуры: {v12r_cache}",
                  file=sys.stderr)
            return {}, EXIT_NOT_VERIFIED
        if len(dom_chunks) < args.v12r_domain_chunks or len(rep_chunks) < args.v12r_replay_chunks:
            print("NOT-VERIFIED: чанков меньше, чем в рецепте v12r — сверка невозможна",
                  file=sys.stderr)
            return {}, EXIT_NOT_VERIFIED
        t0 = time.time()
        mixed, mixed_pos = build_arrays(dom_chunks, rep_chunks, dom_pos, rep_pos,
                                        args.v12r_domain_chunks, args.v12r_replay_chunks,
                                        V12R_SEED, np)
        note(f"v12r пересобран за {time.time() - t0:.0f} с — сверяю с существующим кэшем")
        ref = np.asarray(np.load(str(v12r_cache), mmap_mode="r"), dtype=np.int32)
        if ref.shape != mixed.shape:
            print(f"ОТКАЗ: пересобранный v12r {mixed.shape} против кэша {ref.shape}",
                  file=sys.stderr)
            return {}, EXIT_FAIL
        same_tok = bool(np.array_equal(ref, mixed))
        if not same_tok:
            print(f"ОТКАЗ: сборка НЕ воспроизводит v12r "
                  f"({int((ref != mixed).sum())} расхождений из {ref.size}) — процедура "
                  f"разошлась с оригиналом, писать нельзя", file=sys.stderr)
            return {}, EXIT_FAIL
        if v12r_pos.is_file():
            ref_pos = np.asarray(np.load(str(v12r_pos), mmap_mode="r"), dtype=np.int32)
            same_pos = bool(ref_pos.shape == mixed_pos.shape
                            and np.array_equal(ref_pos, mixed_pos))
        report["fidelity_check"] = {
            "what": "пересборка v12r тем же кодом (7332 домен + 2444 реплей, seed 42) "
                    "побайтово сравнена с существующими кэшами; отдельно сверена "
                    "доменная чанковка с кэшем v10.1",
            "domain_chunks_equal": same_dom,
            "tokens_equal": same_tok,
            "pos_equal": same_pos,
            "reference": {"path": str(v12r_cache), "sha256": sha256_file(v12r_cache)},
            "reference_pos": ({"path": str(v12r_pos), "sha256": sha256_file(v12r_pos)}
                              if v12r_pos.is_file() else None),
            "reference_domain": ({"path": str(dom_cache), "sha256": sha256_file(dom_cache)}
                                 if dom_cache.is_file() else None),
        }
        note(f"самопроверка: домен={same_dom}, v12r tokens={same_tok}, pos={same_pos}")
        del mixed, mixed_pos, ref
        if v12r_pos.is_file() and same_pos is False:
            print("ОТКАЗ: pos-компаньон v12r не воспроизведён", file=sys.stderr)
            return {}, EXIT_FAIL

    # ── 3) сборка v12r50: равные доли, обе части — префиксы частей v12r ──────
    n_rep = min(args.replay_chunks, len(rep_chunks))
    n_dom = n_rep if args.domain_chunks <= 0 else args.domain_chunks
    if n_dom > len(dom_chunks):
        print(f"NOT-VERIFIED: доменных чанков {len(dom_chunks)} < {n_dom}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED
    t0 = time.time()
    mixed, mixed_pos = build_arrays(dom_chunks, rep_chunks, dom_pos, rep_pos,
                                    n_dom, n_rep, args.seed, np)
    note(f"v12r50 собран за {time.time() - t0:.0f} с: {mixed.shape}")
    out_cache = tok_dir / f"{args.out_stem}_{args.max_len}_{args.tag}.npy"
    out_pos = tok_dir / f"{args.out_stem}_{args.max_len}_{args.tag}_pos.npy"
    out_manifest = datasets / f"{args.out_stem}.txt"

    report["mix"] = {
        "out_stem": args.out_stem,
        "domain_chunks": n_dom, "replay_chunks": n_rep, "chunks": int(mixed.shape[0]),
        "chunk_len": args.max_len,
        "tokens": int(mixed.shape[0]) * args.max_len,
        "mix_domain_ratio": round(n_dom / mixed.shape[0], 6),
        "mix_replay_ratio": round(n_rep / mixed.shape[0], 6),
        "seed": args.seed,
        "prefix_of_v12r": {
            "domain": f"первые {n_dom} чанков того же доменного потока, что v12r (там {V12R_DOMAIN_CHUNKS})",
            "replay": f"первые {n_rep} чанков того же реплей-потока, что v12r (там {V12R_REPLAY_CHUNKS})",
        },
        "arithmetic": f"{n_dom} × {args.max_len} = {n_dom * args.max_len} домен; "
                      f"{n_rep} × {args.max_len} = {n_rep * args.max_len} реплей; "
                      f"сумма {int(mixed.shape[0]) * args.max_len}",
    }

    written = []
    if args.plan:
        print(json.dumps(report["mix"], ensure_ascii=False, indent=2))
        return report, EXIT_OK

    if out_cache.exists() or out_pos.exists():
        if not args.force:
            print(f"ОТКАЗ: целевые файлы существуют ({out_cache.name} / {out_pos.name}) — "
                  f"перезапись только с --force (AD-7: состав меняется карточкой)", file=sys.stderr)
            return {}, EXIT_FAIL
    np.save(out_cache, mixed)
    np.save(out_pos, mixed_pos)
    note("кэши записаны — считаю sha256")
    for p in (out_cache, out_pos):
        written.append({"path": str(p), "bytes": p.stat().st_size, "sha256": sha256_file(p)})
        note(f"SAVED {p.name} sha256={written[-1]['sha256']}")
    out_manifest.write_text(manifest_text(report), encoding="utf-8")
    written.append({"path": str(out_manifest), "bytes": out_manifest.stat().st_size,
                    "sha256": sha256_file(out_manifest)})
    report["written"] = written
    report["manifest_sha256"] = sha256_file(out_manifest)
    #: Хеши входов в карточке — не «взяты из документации», а посчитаны здесь.
    report["inputs_sha256"] = {
        "domain_txt": report["sources"]["domain"]["sha256"],
        "replay_txt": report["sources"]["replay"]["sha256"],
    }
    del mixed, mixed_pos
    return report, EXIT_OK


def manifest_text(report: dict) -> str:
    """Манифест микса — по образцу `cpt_corpus_v12r.txt` (это описание, не данные:
    `CPTDataset` читает кэш по stem'у имени файла)."""
    m, s = report["mix"], report["sources"]
    return f"""# {m['out_stem']} — манифест смешанного CPT-корпуса (S3m, 16.09.2026)
#
# НАЗНАЧЕНИЕ: калибровка доли replay (ADR-022 п.2). Тот же рецепт, что v12r,
# с долей replay, поднятой с 25 % до 50 %.
#
# СОСТАВ (чанки {m['chunk_len']} tok, shuffle seed={m['seed']}, chunk-level):
#   {m['domain_chunks']} чанка — {s['domain']['path']} (домен, {m['mix_domain_ratio']:.1%})
#   {m['replay_chunks']} чанка — {s['replay']['path']} (общий язык, {m['mix_replay_ratio']:.1%})
#   ИТОГО       — {m['chunks']} чанков ≈ {m['tokens'] / 1e6:.1f}M токенов
#
# ОГРАНИЧЕНИЕ ИСТОЧНИКА: реплей-источник даёт ровно {s['replay']['chunks_available']} чанков —
# это потолок, поэтому 50 % достигнуты равными долями ({m['domain_chunks']} + {m['replay_chunks']}),
# а не полным доменным пулом v12r ({V12R_DOMAIN_CHUNKS}). Обе части — префиксы
# соответствующих частей v12r.
#
# ПРЕТОК-КЭШИ: tok/{m['out_stem']}_{m['chunk_len']}_{report['tokenizer']['model_id'].split('/')[-1].lower()}.npy
# (+ _pos-компаньон для диагонального маскирования). Собраны tools/build_mix_v12r50.py.
#
# sha256 ВХОДОВ:
#   домен   {s['domain']['sha256']}
#   реплей  {s['replay']['sha256']}
"""


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="сборка CPT-микса v12r50 (50 % replay)")
    ap.add_argument("--datasets-dir", default="/home/user/gb10-shared/datasets")
    ap.add_argument("--domain-txt", default="cpt_corpus_v10.1.txt")
    ap.add_argument("--general-txt", default="general_replay_ru.txt")
    ap.add_argument("--domain-stem", default="cpt_corpus_v10.1")
    ap.add_argument("--out-stem-base", default="cpt_corpus_v12r")
    ap.add_argument("--out-stem", default="cpt_corpus_v12r50")
    ap.add_argument("--tag", default="qwen25", choices=["qwen25", "qwen3", "qwen35"])
    ap.add_argument("--model-id", default="Qwen/Qwen2.5-0.5B")
    ap.add_argument("--max-len", type=int, default=8192)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--replay-chunks", type=int, default=3493,
                    help="реплей-чанков в миксе (по умолчанию — весь источник)")
    ap.add_argument("--domain-chunks", type=int, default=0,
                    help="доменных чанков; 0 = столько же, сколько реплея (равные доли)")
    #: Рецепт v12r, против которого идёт самопроверка. Вынесен в флаги ради
    #: тестируемости: на синтетике (3+2 чанка) сверку иначе не прогнать, а именно
    #: она ловит класс дефекта «сравнил не те массивы» (16.09: сверка домена
    #: против микса падала на формах 7332 против 9776).
    ap.add_argument("--v12r-domain-chunks", type=int, default=V12R_DOMAIN_CHUNKS)
    ap.add_argument("--v12r-replay-chunks", type=int, default=V12R_REPLAY_CHUNKS)
    ap.add_argument("--report", default=None,
                    help="куда записать отчёт сборки (json); относительный путь — от CWD")
    ap.add_argument("--force", action="store_true", help="перезаписать целевые кэши")
    ap.add_argument("--skip-v12r-check", action="store_true",
                    help="не воспроизводить v12r (диагностика; для приёмки не годится)")
    ap.add_argument("--plan", action="store_true", help="показать состав и выйти")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    report, rc = run(args)
    if args.report and report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"отчёт: {out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
