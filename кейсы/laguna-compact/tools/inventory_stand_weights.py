#!/usr/bin/env python3
"""inventory_stand_weights.py — read-only инвентаризация весов лесенки (Н-3).

**Зачем.** §1.2 `LADDER-FULL-PLAN.md` подтверждает веса **2 из 12** моделей — и это
замер по **общему** стору (`gb10-shared/models-store`, он же рабочий HF-кэш
рабочей станции через симлинк). Веса лесенки скачивались на **стенд** (`gb10-fast`,
свой `/home/user/.cache/huggingface`), и оттуда наличие не проверялось ни разу.
Прибор закрывает ровно эту неизвестность: **ничего не качает, не копирует, не
пишет** на стенд и не занимает GPU — только `scandir`/`stat`, чтение `config.json`,
заголовков `safetensors` (первые десятки КБ на файл) и `tokenizer.json`.

**Что проверяется по каждой позиции.**
* каталог модели в двух сторах (локальный HF-кэш стенда и общий стор);
* файлы весов: число шардов, размер, наличие `*.incomplete` и нулевых блобов;
* **это настоящие safetensors, а не LFS-указатели**: разбирается заголовок файла,
  считаются тензоры и сумма данных, сверяется с `metadata.total_size` индекса;
* `model_type`/`architectures` из `config.json` (риск `qwen3_5` — отдельный пункт);
* токенизатор: вокабуляр и то, какие из 6 токенов формата v12 **уже** единый id,
  а какие добавляет пайплайн (`pretokenize_v9.py:add_special_tokens`).

**Что прибор НЕ делает и говорит об этом прямо.** Он не доказывает загрузку
архитектуры в стек (это load-test на стенде — отдельная фаза, требующая GPU) и не
заменяет sha256 весов там, где имя блоба им не является: HF-кэш называет блоб
sha256 **только** для LFS-файлов, поэтому `--hash` считает хеши явно, а вывод
помечает, какой хеш снят, а какой прочитан из имени.

Запуск (стенд не занимается — GPU-работ нет)::

    python3 tools/inventory_stand_weights.py --remote gb10-fast --hash \
        --json evidence/ladder-weights-inventory.json \
        --markdown evidence/ladder-weights-inventory.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
from datetime import date, datetime, timezone

# 12 позиций ADR-052: (short, HF id, имя каталога HF-кэша)
POSITIONS = [
    ("qwen25-05b", "Qwen/Qwen2.5-0.5B", "models--Qwen--Qwen2.5-0.5B", "В-0 (идёт)", 2),
    ("qwen25-15b", "Qwen/Qwen2.5-1.5B", "models--Qwen--Qwen2.5-1.5B", "В-2", 1),
    ("qwen25-3b", "Qwen/Qwen2.5-3B", "models--Qwen--Qwen2.5-3B", "В-2", 1),
    ("qwen25-7b", "Qwen/Qwen2.5-7B", "models--Qwen--Qwen2.5-7B", "В-3", 1),
    ("qwen3-06b", "Qwen/Qwen3-0.6B", "models--Qwen--Qwen3-0.6B", "В-1", 2),
    ("qwen3-17b", "Qwen/Qwen3-1.7B", "models--Qwen--Qwen3-1.7B", "В-5", 2),
    ("qwen3-4b", "Qwen/Qwen3-4B", "models--Qwen--Qwen3-4B", "В-5", 1),
    ("qwen3-8b", "Qwen/Qwen3-8B", "models--Qwen--Qwen3-8B", "В-5", 1),
    ("qwen35-08b", "Qwen/Qwen3.5-0.8B-Base", "models--Qwen--Qwen3.5-0.8B-Base", "В-1", 2),
    ("qwen35-2b", "Qwen/Qwen3.5-2B-Base", "models--Qwen--Qwen3.5-2B-Base", "В-5", 1),
    ("qwen35-4b", "Qwen/Qwen3.5-4B-Base", "models--Qwen--Qwen3.5-4B-Base", "В-5", 1),
    ("qwen35-9b", "Qwen/Qwen3.5-9B-Base", "models--Qwen--Qwen3.5-9B-Base", "В-5", 1),
]

# Спецтокены формата v12, которым AD-3 требует единый id в обучающем кэше.
SPECIAL_V12 = ["<think>", "</think>", "<tool_call>", "</tool_call>",
               "<tool_response>", "</tool_response>"]
SPECIAL_DECLARED = ["<reasoning>", "</reasoning>"]

WINNER_EXT = (".safetensors", ".bin", ".pt", ".gguf")
MANIFEST_FILES = ("config.json", "model.safetensors.index.json",
                  "generation_config.json", "tokenizer_config.json",
                  "tokenizer.json")


def human(n: int) -> str:
    x = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if x < 1024 or unit == "ТБ":
            return f"{x:.1f} {unit}" if unit != "Б" else f"{int(x)} Б"
        x /= 1024.0
    return f"{n}"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def read_head(path: str, size: int) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def snapshot_of(model_dir: str):
    snaps = os.path.join(model_dir, "snapshots")
    if os.path.isdir(snaps):
        entries = sorted(os.listdir(snaps))
        return (os.path.join(snaps, entries[0]), "hf-hub (snapshots/)") if entries else (None, "hf-hub (snapshots/, пусто)")
    return (model_dir, "плоский (файлы в корне каталога)")


def safetensors_header(path: str) -> dict:
    """Разбор заголовка safetensors: тензоры, сумма данных, dtypes.

    Отличает настоящий файл весов от LFS-указателя и обрезанной закачки.
    """
    head = read_head(path, 8)
    if len(head) != 8:
        return {"error": "файл короче 8 байт"}
    n = struct.unpack("<Q", head)[0]
    if n == 0 or n > 200 * 1024 * 1024:
        return {"error": f"не safetensors: длина заголовка {n}"}
    blob = read_head(path, 8 + n)
    if len(blob) != 8 + n:
        return {"error": "заголовок обрезан"}
    try:
        meta = json.loads(blob[8:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"error": f"заголовок не JSON: {exc}"}
    tensors, data, dtypes = 0, 0, {}
    for name, spec in meta.items():
        if name == "__metadata__":
            continue
        tensors += 1
        try:
            start, end = spec["data_offsets"]
        except (KeyError, TypeError, ValueError):
            return {"error": f"непонятная запись тензора {name}"}
        data += end - start
        dtypes[spec["dtype"]] = dtypes.get(spec["dtype"], 0) + 1
    return {"tensor_count": tensors, "tensor_data_bytes": data, "dtypes": dtypes}


def tokenizer_facts(path: str) -> dict:
    raw = read_head(path, 64 * 1024 * 1024)
    try:
        tok = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}
    added = {t.get("content"): t.get("id") for t in tok.get("added_tokens", []) or []}
    vocab = (tok.get("model") or {}).get("vocab") or {}
    return {
        "vocab_size_dict": len(vocab) if isinstance(vocab, dict) else None,
        "added_tokens": len(added),
        "special_v12_native_single_id": sorted(t for t in SPECIAL_V12 if added.get(t) is not None),
        "special_v12_added_by_pipeline": sorted(t for t in SPECIAL_V12 if added.get(t) is None),
        "special_declared_single_id": sorted(t for t in SPECIAL_DECLARED if added.get(t) is not None),
        "note": "токены, отсутствующие в tokenizer.json, добавляет пайплайн "
                "(pretokenize_v9.py: add_special_tokens(SPECIAL_TOKENS)) — единый id в кэше обеспечен им",
    }


def inspect_model(model_dir: str, entry: dict, do_hash: bool) -> dict:
    snap, layout = snapshot_of(model_dir)
    entry["layout"] = layout
    entry["snapshot"] = snap
    if not snap:
        entry["state"] = "только метаданные (нет снапшота)"
        return entry
    names = sorted(os.listdir(snap))
    weights = [n for n in names if n.endswith(WINNER_EXT)]
    incomplete = [n for n in names if n.endswith(".incomplete")]
    zero = [n for n in names if os.path.getsize(os.path.join(snap, n)) == 0]
    entry["incomplete_files"] = incomplete
    entry["zero_byte_files"] = zero
    entry["files"] = {}
    for n in names:
        fp = os.path.join(snap, n)
        if not os.path.isfile(fp):
            continue
        rec = {"bytes": os.path.getsize(fp)}
        # имя блоба в HF-кэше = sha256 ТОЛЬКО для LFS-файлов; для остальных — git sha1
        blob = os.path.basename(os.path.realpath(fp))
        rec["blob_name"] = blob
        rec["blob_name_is_sha256"] = len(blob) == 64
        if do_hash and (n.endswith(WINNER_EXT) or n in MANIFEST_FILES):
            rec["sha256"] = sha256_file(fp)
            # HF называет блоб sha256 только у LFS-файлов; у xet-закачек имя иное.
            rec["blob_name_equals_sha256"] = (rec["sha256"] == blob)
        entry["files"][n] = rec
    if not weights:
        entry["state"] = ("пустой снапшот (файлов нет вовсе)" if not names
                          else "только метаданные (файлов весов нет)")
        return entry
    entry["weights_bytes"] = sum(os.path.getsize(os.path.join(snap, n)) for n in weights)
    entry["weights_count"] = len(weights)
    cfg_path = os.path.join(snap, "config.json")
    cfg = {}
    if os.path.isfile(cfg_path):
        try:
            cfg = json.loads(read_head(cfg_path, 1 << 20).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            cfg = {}
    text_cfg = cfg.get("text_config") or {}
    entry["config"] = {
        "model_type": cfg.get("model_type") or text_cfg.get("model_type"),
        "architectures": cfg.get("architectures"),
        "transformers_version": cfg.get("transformers_version"),
        "vocab_size": cfg.get("vocab_size", text_cfg.get("vocab_size")),
        "hidden_size": cfg.get("hidden_size", text_cfg.get("hidden_size")),
        "num_hidden_layers": cfg.get("num_hidden_layers", text_cfg.get("num_hidden_layers")),
        "max_position_embeddings": cfg.get("max_position_embeddings",
                                           text_cfg.get("max_position_embeddings")),
        "modality": sorted(k for k in cfg if k.endswith("_config")),
    }
    shards, bad = {}, []
    for n in weights:
        info = safetensors_header(os.path.join(snap, n))
        shards[n] = info
        if "error" in info:
            bad.append(n)
    entry["shards"] = shards
    entry["shards_are_safetensors"] = not bad
    entry["shards_bad"] = bad
    entry["params_estimate_b"] = round(
        sum(s.get("tensor_data_bytes", 0) for s in shards.values()) / 2.0 / 1e9, 3)
    idx = os.path.join(snap, "model.safetensors.index.json")
    declared = None
    if os.path.isfile(idx):
        try:
            declared = json.loads(read_head(idx, 4 << 20).decode("utf-8")).get(
                "metadata", {}).get("total_size")
        except (UnicodeDecodeError, json.JSONDecodeError):
            declared = None
    entry["index_total_size"] = declared
    entry["index_matches_shards"] = (
        declared is not None
        and declared == sum(s.get("tensor_data_bytes", 0) for s in shards.values()))
    # Сводка целостности одной строкой: что проверено механизмом, а что — нет.
    entry["integrity"] = {
        "safetensors_headers_valid": not bad,
        "index_present": declared is not None,
        "index_matches_shards": entry["index_matches_shards"],
        "sha256_taken_here": do_hash,
        "blob_names_carry_sha256": (
            "проверено равенством" if do_hash and all(
                entry["files"][n].get("blob_name_equals_sha256") for n in weights)
            else ("**не совпало** — имя блоба не sha256" if do_hash
                  else "не проверено (нужен --hash); длина 64 у всех: "
                       + str(all(entry["files"][n]["blob_name_is_sha256"] for n in weights)))),
        "no_incomplete_files": not incomplete,
    }
    tok_json = os.path.join(snap, "tokenizer.json")
    if os.path.isfile(tok_json):
        entry["tokenizer_facts"] = tokenizer_facts(tok_json)
    incomplete_share = sum(os.path.getsize(os.path.join(snap, n)) for n in incomplete)
    entry["state"] = ("представлены веса" if not incomplete and not zero
                      else f"веса частично ({len(incomplete)} incomplete, {human(incomplete_share)})")
    return entry


def scan(roots, do_hash: bool, positions) -> dict:
    report = {"host": os.uname().nodename,
              "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "mode": "read-only (scandir/stat/чтение config.json, заголовков safetensors, tokenizer.json)",
              "roots": roots, "positions": []}
    for short, hf_id, dirname, wave, batch in positions:
        pos = {"short": short, "hf_id": hf_id, "wave": wave,
               "batch_v12_runner": batch, "matches": []}
        for root in roots:
            md = os.path.join(root, dirname)
            if not os.path.isdir(md):
                continue
            entry = {"root": root, "path": md}
            pos["matches"].append(inspect_model(md, entry, do_hash))
        with_weights = [m for m in pos["matches"] if m.get("weights_count")]
        valid = [m for m in with_weights if m.get("shards_are_safetensors")]
        if valid:
            pos["verdict"] = "есть"
        elif with_weights:
            # Файлы с именем весов, но не safetensors (LFS-указатель, обрезанная
            # закачка): это НЕ «есть веса» — иначе прибор повторил бы ошибку,
            # которую сам ловит в поле integrity.
            pos["verdict"] = "файлы весов есть, но не safetensors"
        elif any(m.get("files") for m in pos["matches"]):
            pos["verdict"] = "только метаданные"
        else:
            pos["verdict"] = "нет (пустой снапшот)" if pos["matches"] else "нет"
        with_weights = valid
        pos["path_with_weights"] = with_weights[0]["path"] if with_weights else None
        pos["line"] = (f"{short}: {pos['verdict']}"
                       + (f" — {pos['path_with_weights']}"
                          f" ({human(with_weights[0]['weights_bytes'])}, "
                          f"{with_weights[0]['weights_count']} шард(ов), "
                          f"≈{with_weights[0].get('params_estimate_b')} B параметров)"
                          if with_weights else
                          " — ни в локальном HF-кэше стенда, ни в общем сторе"))
        report["positions"].append(pos)
    present = [p["short"] for p in report["positions"] if p["verdict"] == "есть"]
    meta = [p["short"] for p in report["positions"] if p["verdict"] == "только метаданные"]
    absent = [p["short"] for p in report["positions"] if p["verdict"].startswith("нет")]
    not_safetensors = [p["short"] for p in report["positions"]
                       if p["verdict"].startswith("файлы весов есть")]
    report["summary"] = {
        "of_12": len(present),
        "list_present": present,
        "list_metadata_only": meta,
        "list_absent": absent,
        "list_weights_but_not_safetensors": not_safetensors,
        # Сумма — по ОДНОЙ копии на позицию: один и тот же каталог может быть виден
        # из двух корней (локальный кэш стенда и общий стор), и двойной счёт завышал бы
        # объём хранения вдвое там, где файл один.
        "weights_bytes_total": sum(
            next((m.get("weights_bytes") or 0 for m in p["matches"]
                  if m["path"] == p["path_with_weights"]), 0)
            for p in report["positions"]),
        "arch_qwen3_5_positions": [p["short"] for p in report["positions"]
                                   if (p.get("matches") or [{}])[0].get("config", {}).get("model_type") == "qwen3_5"],
    }
    return report


def render_markdown(rep: dict) -> str:
    L = [f"<!-- сгенерировано tools/inventory_stand_weights.py {rep['date']} на {rep['host']} -->",
         "# Инвентаризация весов лесенки на стенде (Н-3, read-only)", "",
         f"Стенд: `{rep['host']}`, режим: {rep['mode']}.", "",
         "| # | Позиция | HF id | Волна | Веса | Где | Размер | Параметров | Токенизатор |",
         "|---|---|---|---|---|---|---|---|---|"]
    for i, p in enumerate(rep["positions"], 1):
        w = next((m for m in p["matches"] if m.get("weights_count")), None)
        tok = "есть" if (w or {}).get("tokenizer_facts") else "нет"
        where = (w or {}).get("layout", "—")
        size = human(w["weights_bytes"]) if w else "—"
        params = f"{w.get('params_estimate_b')} B" if w else "—"
        L.append(f"| {i} | `{p['short']}` | `{p['hf_id']}` | {p['wave']} | **{p['verdict']}** | "
                 f"{where} | {size} | {params} | {tok} |")
    s = rep["summary"]
    L += ["", f"**Итог: {s['of_12']} из 12 позиций с весами**, "
              f"только метаданные — {s['list_metadata_only'] or '—'}, "
              f"нет — {s['list_absent'] or '—'}; суммарно {human(s['weights_bytes_total'])}.", ""]
    L += ["## Целостность: что проверено механизмом", "",
          "| Позиция | заголовки safetensors | `*.incomplete` | индекс сверен | sha256 снят здесь | имя блоба = sha256 |",
          "|---|---|---|---|---|---|"]
    for p in rep["positions"]:
        w = next((m for m in p["matches"] if m.get("integrity")), None)
        if not w:
            continue
        i = w["integrity"]
        idx = "— (одношардовая)" if not i["index_present"] else ("да" if i["index_matches_shards"] else "**НЕТ**")
        L.append(f"| `{p['short']}` | {'валидны' if i['safetensors_headers_valid'] else '**битые**'} | "
                 f"{'нет' if i['no_incomplete_files'] else '**есть**'} | {idx} | "
                 f"{'да' if i['sha256_taken_here'] else 'нет (нужен `--hash`)'} | "
                 f"{i['blob_names_carry_sha256']} |")
    L += ["", "## Построчно (для протокола)", "", "```"]
    L += [p["line"] for p in rep["positions"]]
    L += ["```", ""]
    L += ["## Токен-пространство (AD-3) по семействам", "",
          "| Позиция | vocab в tokenizer.json | added | единый id нативно (6 токенов v12) | добавляет пайплайн |",
          "|---|---|---|---|---|"]
    for p in rep["positions"]:
        w = next((m for m in p["matches"] if m.get("tokenizer_facts")), None)
        if not w:
            continue
        t = w["tokenizer_facts"]
        L.append(f"| `{p['short']}` | {t.get('vocab_size_dict')} | {t.get('added_tokens')} | "
                 f"{', '.join(t.get('special_v12_native_single_id') or []) or '—'} | "
                 f"{', '.join(t.get('special_v12_added_by_pipeline') or []) or '—'} |")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+",
                    default=["/home/user/.cache/huggingface/hub",
                             "/home/user/gb10-shared/models-store/home-roman/huggingface-cache/hub",
                             "/home/user/gb10-shared/models-store/home-roman/huggingface-cache"])
    ap.add_argument("--hash", action="store_true",
                    help="снять sha256 весов и манифестов (тяжелее: читает все файлы весов)")
    ap.add_argument("--only", nargs="*", help="ограничить позиции (short-имена)")
    ap.add_argument("--remote", help="выполнить на стенде через ssh (read-only)")
    ap.add_argument("--json")
    ap.add_argument("--markdown")
    args = ap.parse_args()

    positions = [p for p in POSITIONS if not args.only or p[0] in args.only]

    if args.remote:
        fwd = ["--roots", *args.roots]
        if args.hash:
            fwd.append("--hash")
        if args.only:
            fwd += ["--only", *args.only]
        with open(os.path.abspath(__file__), "rb") as f:
            src = f.read()
        proc = subprocess.run(["ssh", args.remote, "python3", "-", *fwd],
                              input=src, capture_output=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
            return proc.returncode
        rep = json.loads(proc.stdout.decode("utf-8"))
        rep["transport"] = {"mode": "ssh", "target": args.remote,
                            "tool": "tools/inventory_stand_weights.py"}
        rep["local_report_host"] = os.uname().nodename
    else:
        do_hash = args.hash
        rep = scan(args.roots, do_hash, positions)
        rep["transport"] = {"mode": "local"}

    text = json.dumps(rep, ensure_ascii=False, indent=1)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"записано: {args.json}")
    else:
        print(text)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(render_markdown(rep))
        print(f"записано: {args.markdown}")
    # сводка — в stderr: в режиме --remote stdout обязан нести только JSON
    print(f"итог: {rep['summary']['of_12']}/12 с весами; нет: "
          f"{rep['summary']['list_absent']}; только метаданные: "
          f"{rep['summary']['list_metadata_only']}; файлы-не-safetensors: "
          f"{rep['summary'].get('list_weights_but_not_safetensors')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
