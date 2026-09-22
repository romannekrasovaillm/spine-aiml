#!/usr/bin/env python3
"""S3o — контрольный корпус C1: 100 % общий язык (домена нет вовсе).

Зачем. Четыре руки S3m (доля replay 25/50 % × пик LR ×0.35/×0.7) провалили потолок
ADR-022: `ppl_general` 55.3–113.1 против базы 11.93 при 2000 шагах CPT. После S3k
(штатное внимание разрушает язык не слабее flex — значит flex не причина) остались
два подозреваемых: **сам протокол CPT** и **данные/LR**. C1 отделяет протокол от
домена: те же 2000 шагов, батч 1, `max_len 8192`, сид 42, та же копия пайплайна и
тот же образ — но обучающий материал целиком из `general_replay_ru.txt`. Если язык
разрушается и здесь, виноват протокол, а не доменный корпус.

Состав. Источник реплея даёт ровно 3493 чанка по 8192 токена — это **весь поток**,
а не выбранная доля: у C1 домена нет, поэтому «100 % replay» выражается в «взять
всё, что есть». Чанковка, фильтры документов (`\\n\\n`, ≥50 символов), шафл
(`RandomState(42).shuffle` по оси чанков) и компаньон `position_ids` — дословно те
же, что у `tools/build_mix_v12r50.py`; инструмент **импортирует его как
библиотеку**, а не повторяет рецепт второй копией (две копии рецепта — две правды
о том, чем собран кэш).

Самопроверка процедуры — побайтовая и **без токенизации домена**. Оба существующих
кэша (`v12r`: 7332 домен + 2444 реплей; `v12r50`: 3493 + 3493) — это
`RandomState(42).shuffle` конкатенации домен+реплей, поэтому реплей-часть из них
**восстанавливается точно**: `shuffle` первой оси эквивалентен
`x[RandomState(seed).permutation(n)]`, значит `shuffled[inv[n_dom:n_dom+n_rep]]` —
это ровно исходный реплей-блок. Эта эквивалентность не предполагается, а
проверяется здесь же на обеих эталонных длинах (9776 и 6986). Отсюда сверка «наши
чанки [:2444] против v12r» и «наши чанки [:3493] против v12r50» — сравнение байт,
ловящее дрейф токенизатора, фильтров документов и чанкера разом. Мультимножества
для этого мало: совпавший состав в другом порядке дал бы другой обучающий поток.

Порядок «сначала сверка, потом запись» намеренный: пока процедура не воспроизвела
существующие кэши, писать новый нельзя (иначе отличие C1 от v12r50 объяснялось бы
дрейфом кода, а не составом корпуса).

Названное сопряжённое отличие. У C1 3493 чанка против 6986 у v12r50 и 9776 у v12r:
при 2000 шагах × батч 1 прогон проходит по корпусу иначе (доля увиденного за стадию
больше). Это не «то же самое, но без домена» — это записано в отчёте полем
`coupled_changes` и в evidence руки, а не замолчано.

Коды возврата::

    0 — кэши C1 собраны (или уже были), самопроверка пройдена
    1 — самопроверка не прошла: сборка не воспроизводит реплей-части v12r/v12r50
    2 — NOT-VERIFIED: нет входа (источник/токенизатор/эталонные кэши)

Запуск (в контейнере стенда: каталог `datasets/tok` принадлежит root)::

    python3 tools/build_ctrl_c1_corpus.py --plan
    python3 tools/build_ctrl_c1_corpus.py --datasets-dir /workspace/shared/datasets \\
        --report /workspace/shared/datasets/mix-ctrl100-build.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: Рецепт берётся у S3m-сборщика как библиотека: чанкер, фильтры, шафл и загрузка
#: токенизатора — его функции, а не пересказ. Файл обязан лежать рядом (в контейнер
#: доставляются оба), иначе импорт — отказ, а не «собралось как-то иначе».
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_mix_v12r50 as B  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

SEED = 42
OUT_STEM = "cpt_corpus_ctrl100"

#: Эталоны, против которых идёт самопроверка. Числа — из `mix-v12r50-build.json`
#: (там же: 3493 домена + 3493 реплея = 6986) и из рецепта v12r (7332 + 2444 = 9776).
V12R_DOMAIN_CHUNKS = B.V12R_DOMAIN_CHUNKS          # 7332
V12R_REPLAY_CHUNKS = B.V12R_REPLAY_CHUNKS          # 2444
V12R_TOTAL = V12R_DOMAIN_CHUNKS + V12R_REPLAY_CHUNKS   # 9776
V12R50_DOMAIN_CHUNKS = 3493
V12R50_REPLAY_CHUNKS = 3493
V12R50_TOTAL = V12R50_DOMAIN_CHUNKS + V12R50_REPLAY_CHUNKS  # 6986

#: Реплей-источник даёт ровно столько чанков — это потолок источника (im S3m он же
#: ограничил долю 50 % равными долями), а не выбранная доля C1.
CTRL100_CHUNKS = 3493


def note(msg: str) -> None:
    """Прогресс — с отметкой времени: молчащий процесс неотличим от зависшего."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def _own_revision_sha256() -> str:
    """Ревизия самого инструмента: без неё происхождение кэша недоказуемо."""
    me = Path(__file__)
    if not me.is_file():
        raise SystemExit(
            f"ОТКАЗ: инструмент запущен без файла на диске ({me}) — ревизия сборщика "
            f"недоказуема. Запускай файлом (python3 tools/build_ctrl_c1_corpus.py)")
    return sha256_file(me)


def shuffle_is_permutation(np, sizes) -> dict:
    """Фикстура: `shuffle` первой оси == `x[permutation(n)]` на самих эталонных длинах.

    На этом тождестве держится вся самопроверка: если оно неверно, восстановленный
    «реплей-блок» — не он, и сверка ниже превратилась бы в проверку неизвестно чего.
    Поэтому тождество проверяется машиной, а не берётся на веру из документации
    numpy, и проверяется на **тех же** n, что и сами эталоны: поток перестановки
    зависит от n.
    """
    out = {}
    for n in sizes:
        probe = np.arange(n, dtype=np.int64).reshape(n, 1)
        shuffled = probe.copy()
        np.random.RandomState(SEED).shuffle(shuffled)
        perm = np.random.RandomState(SEED).permutation(n)
        out[str(n)] = bool(np.array_equal(shuffled.ravel(), perm))
    return out


def replay_block_in_mix(np, cache: Path, pos_cache: Path | None, n_dom: int, n_rep: int):
    """Реплей-блок существующего кэша в исходном (дошафловом) порядке — или причина отказа.

    `shuffled = mixed[perm]`, где `mixed` — конкатенация домен+реплей. Обратная
    перестановка `inv[perm] = arange(n)` даёт `mixed = shuffled[inv]`, то есть
    `shuffled[inv[n_dom:n_dom+n_rep]]` — ровно исходный реплей-блок. Восстановление
    точное, без сопоставления по содержимому: иначе «совпало» означало бы лишь
    «такой чанк где-то есть», а нам нужен порядок.

    Несовпадение числа чанков — не исключение, а названный отказ: эталон собран по
    другому рецепту, и сверять с ним нечего.
    """
    ref = np.load(str(cache), mmap_mode="r")
    if ref.shape[0] != n_dom + n_rep:
        return None, None, (f"{cache.name}: {ref.shape[0]} чанков против ожидаемых "
                            f"{n_dom} + {n_rep} = {n_dom + n_rep}")
    perm = np.random.RandomState(SEED).permutation(ref.shape[0])
    inv = np.empty(ref.shape[0], dtype=np.int64)
    inv[perm] = np.arange(ref.shape[0], dtype=np.int64)
    idx = inv[n_dom:n_dom + n_rep]
    tokens = np.asarray(ref[idx], dtype=np.int32)
    positions = None
    if pos_cache is not None and Path(pos_cache).is_file():
        pos_ref = np.load(str(pos_cache), mmap_mode="r")
        positions = np.asarray(pos_ref[idx], dtype=np.int32)
    return tokens, positions, None


def compare(np, ours, ours_pos, ref_path: Path, pos_path: Path, n_dom: int, n_rep: int,
            label: str) -> dict:
    """Сверка наших первых `n_rep` чанков с реплей-блоком эталонного микса."""
    ref, ref_pos, why = replay_block_in_mix(np, ref_path, pos_path, n_dom, n_rep)
    if ref is None:
        note(f"сверка {label}: ОТКАЗ — {why}")
        return {"mix": label, "reference": str(ref_path), "n_dom": n_dom, "n_rep": n_rep,
                "shape_ok": False, "tokens_equal": False, "mismatched_chunks": None,
                "pos_equal": None, "reason": why}
    ours_cut = np.asarray(ours[:n_rep], dtype=np.int32)
    shape_ok = ref.shape == ours_cut.shape
    equal = bool(shape_ok and np.array_equal(ref, ours_cut))
    diff = int((ref != ours_cut).sum()) if shape_ok else None
    pos_equal = None
    if ref_pos is not None and ours_pos is not None:
        ours_pos_cut = np.asarray(ours_pos[:n_rep], dtype=np.int32)
        pos_equal = bool(ref_pos.shape == ours_pos_cut.shape
                         and np.array_equal(ref_pos, ours_pos_cut))
    out = {"mix": label, "reference": str(ref_path), "n_dom": n_dom, "n_rep": n_rep,
           "shape": list(ref.shape), "shape_ok": bool(shape_ok),
           "tokens_equal": equal, "mismatched_chunks": diff,
           "pos_reference": str(pos_path) if ref_pos is not None else None,
           "pos_equal": pos_equal}
    note(f"сверка {label}: tokens={equal}"
         + (f", pos={pos_equal}" if pos_equal is not None else "")
         + (f", расхождений {diff}" if equal is False else ""))
    return out


def manifest_text(report: dict) -> str:
    """Манифест корпуса C1 — по образцу `cpt_corpus_v12r50.txt`.

    Это описание, а не данные: `CPTDataset` читает кэш по stem'у имени файла, и
    замена содержимого `.txt` на другой корпус ничего бы не изменила.
    """
    m, s = report["mix"], report["sources"]["replay"]
    return f"""# {m['out_stem']} — манифест контрольного CPT-корпуса C1 (S3o, 16.09.2026)
#
# НАЗНАЧЕНИЕ: контрольная рука C1 — проверка ГИПОТЕЗЫ О ПРОТОКОЛЕ (ADR-022 п.1–2).
# Тот же протокол, что у четырёх рук S3m (2000 шагов, батч 1, max_len 8192, сид 42,
# одна копия пайплайна), но обучающий материал — только общий язык: домена нет.
# Если язык разрушается и здесь, причина форгеттинга — протокол, а не домен.
#
# СОСТАВ (чанки {m['chunk_len']} tok, shuffle seed={m['seed']}, chunk-level):
#   {m['replay_chunks']} чанка — {s['path']} (общий язык, {m['mix_replay_ratio']:.0%}, домена нет)
#   ИТОГО        — {m['chunks']} чанков ≈ {m['tokens'] / 1e6:.1f}M токенов
#
# ПОТОЛОК ИСТОЧНИКА: реплей-источник даёт ровно {s['chunks_available']} чанков — это
# весь поток, поэтому «100 % replay» здесь означает «всё, что есть», а не выбор доли.
#
# СОПРЯЖЁННОЕ ОТЛИЧИЕ (названо, а не замолчано): корпус короче эталонных миксов —
# {m['chunks']} чанков против {V12R50_TOTAL} у v12r50 и {V12R_TOTAL} у v12r, поэтому при тех же
# 2000 шагах × батч 1 прогон проходит по корпусу иначе.
#
# ПРЕТОК-КЭШИ: tok/{m['out_stem']}_{m['chunk_len']}_{report['tokenizer']['tag']}.npy
# (+ _pos-компаньон для диагонального маскирования). Собраны tools/build_ctrl_c1_corpus.py
# тем же чанкером, что v12r50 (tools/build_mix_v12r50.py, импортирован как библиотека).
#
# САМОПРОВЕРКА: наши первые {V12R_REPLAY_CHUNKS} чанка побайтово равны реплей-блоку v12r,
# первые {V12R50_REPLAY_CHUNKS} — реплей-блоку v12r50 ({report['fidelity_check']['summary']}).
#
# sha256 ВХОДА:
#   реплей {s['sha256']}
# sha256 КЭША:
#   {report['written'][0]['sha256']}
"""


def run(args) -> tuple[dict, int]:
    import numpy as np  # noqa: E402 — тяжёлый импорт только на исполнении

    datasets = Path(args.datasets_dir)
    tok_dir = datasets / "tok"
    general_txt = datasets / args.general_txt
    v12r_cache = tok_dir / f"{args.v12r_stem}_{args.max_len}_{args.tag}.npy"
    v12r_pos = tok_dir / f"{args.v12r_stem}_{args.max_len}_{args.tag}_pos.npy"
    v12r50_cache = tok_dir / f"{args.v12r50_stem}_{args.max_len}_{args.tag}.npy"
    v12r50_pos = tok_dir / f"{args.v12r50_stem}_{args.max_len}_{args.tag}_pos.npy"

    missing = [str(p) for p in (general_txt, v12r_cache, v12r50_cache) if not p.is_file()]
    if missing:
        print("NOT-VERIFIED: нет входа: " + "; ".join(missing), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    report: dict = {
        "schema": "ctrl-c1-corpus-build/1",
        "stage": "S3o",
        "date": datetime.now(timezone.utc).isoformat(),
        "tool": {"path": "tools/build_ctrl_c1_corpus.py",
                 "sha256": _own_revision_sha256(), "argv": sys.argv[1:],
                 "recipe_library": {"path": "tools/build_mix_v12r50.py",
                                    "sha256": B._own_revision_sha256()}},
        "recipe": {"chunk_len": args.max_len, "seed": args.seed,
                   "shuffle": "numpy RandomState(seed).shuffle по оси чанков (та же функция)",
                   "replay_sep": repr(B.REPLAY_SEP), "min_doc_chars": B.MIN_DOC_CHARS,
                   "chunking": "нехвостовые блоки max_len, хвост отброшен",
                   "domain": "отсутствует (n_dom = 0)"},
        "env_shims": {},
        "sources": {},
        "fidelity_check": {},
        "coupled_changes": [
            f"корпус короче эталонов: {CTRL100_CHUNKS} чанков против {V12R50_TOTAL} (v12r50) "
            f"и {V12R_TOTAL} (v12r) — при 2000 шагах × батч 1 доля корпуса, увиденная за "
            f"стадию, больше",
            "число чанков несёт только общий язык: сравнение с руками S3m по абсолютному "
            "уровню loss некорректно, вердикт — по PPL (ADR-022 п.3)",
        ],
    }

    tokenizer, tok_prov = B.load_tokenizer(args.tag, args.model_id)
    report["env_shims"] = dict(B.ENV_SHIMS)
    tok_prov = dict(tok_prov)
    tok_prov["tag"] = args.tag
    report["tokenizer"] = tok_prov

    # ── 1) поток реплея: тот же сплит документов и тот же фильтр, что в v12r50 ──
    t0 = time.time()
    rep_tokens, rep_starts, n_rep_docs = B.stream_with_starts(
        general_txt, B.REPLAY_SEP, B.MIN_DOC_CHARS, tokenizer, log=note)
    note(f"реплей токенизирован за {time.time() - t0:.0f} с")
    t0 = time.time()
    rep_chunks = B.chunks_from_stream(rep_tokens, args.max_len, np)
    rep_pos = B.positions_for_stream(rep_starts, len(rep_tokens), args.max_len, np)
    note(f"чанки и position_ids собраны за {time.time() - t0:.0f} с "
         f"({rep_chunks.shape})")
    report["sources"]["replay"] = {
        "path": str(general_txt), "bytes": general_txt.stat().st_size,
        "sha256": sha256_file(general_txt), "docs": n_rep_docs,
        "tokens": len(rep_tokens), "chunks_available": int(rep_chunks.shape[0]),
    }
    print(f"реплей: {n_rep_docs} док → {len(rep_tokens)} ток → {rep_chunks.shape[0]} чанков")

    n_rep = min(args.replay_chunks, int(rep_chunks.shape[0]))
    ref_specs = [("v12r", v12r_cache, v12r_pos,
                  args.ref_v12r_domain_chunks, args.ref_v12r_replay_chunks),
                 ("v12r50", v12r50_cache, v12r50_pos,
                  args.ref_v12r50_domain_chunks, args.ref_v12r50_replay_chunks)]
    if not args.skip_v12r_check:
        if n_rep < max(n for _, _, _, _, n in ref_specs) and not args.allow_short:
            print(f"NOT-VERIFIED: чанков {n_rep} меньше реплей-блока эталона "
                  f"({max(n for _, _, _, _, n in ref_specs)}) — сверка невозможна",
                  file=sys.stderr)
            return {}, EXIT_NOT_VERIFIED

    # ── 2) самопроверка процедуры: наши чанки == реплей-блоки обоих эталонов ───
    if not args.skip_v12r_check:
        identity = shuffle_is_permutation(
            np, [d + r for _, _, _, d, r in ref_specs])
        if not all(identity.values()):
            print("ОТКАЗ: shuffle первой оси не эквивалентен x[permutation(n)] — "
                  f"самопроверка на этом тождестве невозможна ({identity})", file=sys.stderr)
            return {}, EXIT_FAIL
        checks = [compare(np, rep_chunks, rep_pos, cache, pos, n_dom, n_ref_rep, label)
                  for label, cache, pos, n_dom, n_ref_rep in ref_specs]
        report["fidelity_check"] = {
            "what": "реплей-блок существующих кэшей восстановлен обратной перестановкой "
                    "(shuffled[inv[n_dom:n_dom+n_rep]]) и сравнён побайтово с нашими "
                    "первыми n_rep чанками; тождество shuffle==x[perm] проверено фикстурой "
                    "на тех же длинах",
            "shuffle_equals_permutation": identity,
            "checks": checks,
            "summary": "; ".join(f"{c['mix']}: tokens={c['tokens_equal']} "
                                 f"pos={c['pos_equal']}" for c in checks),
        }
        bad = [c["mix"] for c in checks if not c["tokens_equal"]]
        if bad:
            print(f"ОТКАЗ: сборка НЕ воспроизводит реплей-блок {', '.join(bad)} — процедура "
                  f"разошлась с эталоном, писать нельзя "
                  f"(расхождения: {[c['mismatched_chunks'] for c in checks]})", file=sys.stderr)
            return {}, EXIT_FAIL
        if any(c["pos_equal"] is False for c in checks):
            print("ОТКАЗ: pos-компаньон реплей-блока не воспроизведён", file=sys.stderr)
            return {}, EXIT_FAIL
    else:
        report["fidelity_check"] = {"skipped": True,
                                   "why": "--skip-v12r-check (диагностика; для приёмки не годится)"}

    # ── 3) сборка C1: тот же build_arrays с n_dom = 0 (шафл — его же функция) ──
    t0 = time.time()
    mixed, mixed_pos = B.build_arrays(rep_chunks, rep_chunks, rep_pos, rep_pos,
                                      0, n_rep, args.seed, np)
    note(f"C1 собран за {time.time() - t0:.0f} с: {mixed.shape}")

    out_cache = tok_dir / f"{args.out_stem}_{args.max_len}_{args.tag}.npy"
    out_pos = tok_dir / f"{args.out_stem}_{args.max_len}_{args.tag}_pos.npy"
    out_manifest = datasets / f"{args.out_stem}.txt"
    report["mix"] = {
        "out_stem": args.out_stem, "domain_chunks": 0, "replay_chunks": n_rep,
        "chunks": int(mixed.shape[0]), "chunk_len": args.max_len,
        "tokens": int(mixed.shape[0]) * args.max_len,
        "mix_domain_ratio": 0.0, "mix_replay_ratio": 1.0, "seed": args.seed,
        "arithmetic": f"0 домен + {n_rep} × {args.max_len} = {n_rep * args.max_len} реплей",
        "shuffled_by": "tools/build_mix_v12r50.py:build_arrays (n_dom=0)",
    }

    if args.plan:
        print(json.dumps(report["mix"], ensure_ascii=False, indent=2))
        return report, EXIT_OK

    if out_cache.exists() or out_pos.exists():
        if not args.force:
            print(f"ОТКАЗ: целевые файлы существуют ({out_cache.name} / {out_pos.name}) — "
                  f"перезапись только с --force (AD-7: состав меняется карточкой)",
                  file=sys.stderr)
            return {}, EXIT_FAIL
    np.save(out_cache, mixed)
    np.save(out_pos, mixed_pos)
    note("кэши записаны — считаю sha256")
    written = []
    for p in (out_cache, out_pos):
        written.append({"path": str(p), "bytes": p.stat().st_size, "sha256": sha256_file(p)})
        note(f"SAVED {p.name} sha256={written[-1]['sha256']}")
    report["written"] = written
    report["inputs_sha256"] = {"replay_txt": report["sources"]["replay"]["sha256"]}
    #: Манифест пишется ПОСЛЕ кэшей, но его собственный хеш берётся из готового
    #: текста: иначе в карточке стоял бы хеш файла, которого ещё нет.
    out_manifest.write_text(manifest_text(report), encoding="utf-8")
    written.append({"path": str(out_manifest), "bytes": out_manifest.stat().st_size,
                    "sha256": sha256_file(out_manifest)})
    report["manifest_sha256"] = sha256_file(out_manifest)
    report["written"] = written
    del mixed, mixed_pos
    return report, EXIT_OK


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="сборка контрольного CPT-корпуса C1 (100 % общий язык, S3o)")
    ap.add_argument("--datasets-dir", default="/home/user/gb10-shared/datasets")
    ap.add_argument("--general-txt", default="general_replay_ru.txt")
    ap.add_argument("--v12r-stem", default="cpt_corpus_v12r")
    ap.add_argument("--v12r50-stem", default="cpt_corpus_v12r50")
    ap.add_argument("--out-stem", default=OUT_STEM)
    ap.add_argument("--tag", default="qwen25", choices=["qwen25", "qwen3", "qwen35"])
    ap.add_argument("--model-id", default="Qwen/Qwen2.5-0.5B")
    ap.add_argument("--max-len", type=int, default=8192)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--replay-chunks", type=int, default=CTRL100_CHUNKS,
                    help="чанков реплея в корпусе (по умолчанию — весь источник)")
    #: Рецепты эталонов, против которых идёт самопроверка. Вынесены в флаги ради
    #: тестируемости: на синтетике (3+2 и 3+3 чанка) сверку иначе не прогнать, а
    #: именно она ловит класс дефекта «сверил не тот блок».
    ap.add_argument("--ref-v12r-domain-chunks", type=int, default=V12R_DOMAIN_CHUNKS)
    ap.add_argument("--ref-v12r-replay-chunks", type=int, default=V12R_REPLAY_CHUNKS)
    ap.add_argument("--ref-v12r50-domain-chunks", type=int, default=V12R50_DOMAIN_CHUNKS)
    ap.add_argument("--ref-v12r50-replay-chunks", type=int, default=V12R50_REPLAY_CHUNKS)
    ap.add_argument("--allow-short", action="store_true",
                    help="не требовать, чтобы чанков было не меньше реплей-блока эталона")
    ap.add_argument("--report", default=None, help="куда записать отчёт сборки (json)")
    ap.add_argument("--force", action="store_true", help="перезаписать целевые кэши")
    ap.add_argument("--skip-v12r-check", action="store_true",
                    help="не сверять с реплей-блоками v12r/v12r50 (диагностика)")
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
