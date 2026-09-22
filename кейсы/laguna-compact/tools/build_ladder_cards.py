#!/usr/bin/env python3
"""build_ladder_cards.py — карточки AD-2 на три семейства лесенки (Н-5).

**Зачем.** `LADDER-FULL-PLAN.md` §7 (Н-5): без карточек AD-2 (база, набор,
токенизатор, sha256) прогоны лесенки несопоставимы — ровно то, чем ADR-001
отклоняла вариант «воспроизвести лесенку как есть». Носителя не было.

**Что здесь есть и чего нет.**
* **База** берётся из `evidence/ladder-weights-inventory.json` (прибор Н-3,
  read-only замер на стенде) — то есть из замера, а не из памяти;
* **набор** берётся из существующих карточек (`data/sft-card-v13.json`,
  `data/corpus-card.json`), а существование и sha **кэшей** проверяются здесь
  же (`--hash-caches`) — то есть карточка несёт то, что **фактически прочитано**,
  а не то, что объявлено (ADR-051);
* карточка **не меняет** ни наборы, ни веса, ни существующие карточки (AD-7):
  это новый файл.

Отсутствие кэша записывается **фактом** («не собран»), а не «предполагается»:
на 22.09.2026 SFT-кэш под `qwen3`/`qwen35` не существует — это цена Н-1.

Запуск::

    python3 tools/build_ladder_cards.py --out-dir data/ad2-cards --hash-caches
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

CASE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOK_DIR_DEFAULT = "/home/user/gb10-shared/datasets/tok"

# Семейство → (позиции лесенки, тег претокен-кэша, HF id токенизатора из раннера)
FAMILIES = {
    "qwen25": {
        "tag": "qwen25",
        "positions": ["qwen25-05b", "qwen25-15b", "qwen25-3b", "qwen25-7b"],
        "tokenizer_hf_id": "Qwen/Qwen2.5-0.5B",
        "tokenizer_source": "run_v12_ladder.sh:193 (case в преток-блоке)",
        "waves": {"qwen25-05b": "В-0 (идёт)", "qwen25-15b": "В-2",
                  "qwen25-3b": "В-2", "qwen25-7b": "В-3 (нужен LoRA r16)"},
    },
    "qwen3": {
        "tag": "qwen3",
        "positions": ["qwen3-06b", "qwen3-17b", "qwen3-4b", "qwen3-8b"],
        "tokenizer_hf_id": "Qwen/Qwen3-0.6B",
        "tokenizer_source": "run_v12_ladder.sh:194 (case в преток-блоке)",
        "waves": {"qwen3-06b": "В-1 (класс ≤1B)", "qwen3-17b": "В-5",
                  "qwen3-4b": "В-5", "qwen3-8b": "В-5"},
    },
    "qwen35": {
        "tag": "qwen35",
        "positions": ["qwen35-08b", "qwen35-2b", "qwen35-4b", "qwen35-9b"],
        "tokenizer_hf_id": "Qwen/Qwen3.5-0.8B-Base",
        "tokenizer_source": "run_v12_ladder.sh:195 (case в преток-блоке)",
        "waves": {"qwen35-08b": "В-1 (класс ≤1B)", "qwen35-2b": "В-5",
                  "qwen35-4b": "В-5", "qwen35-9b": "В-5"},
    },
}

# Кэши, которые лесенке действительно нужны (вход волн), и их роль.
REQUIRED_CACHES = [
    ("cpt", "cpt_corpus_v12r_8192_{tag}.npy", "CPT-микс v12r (домен+реплей), вход CPT-стадии"),
    ("sft", "sft_train_v13_fixed_8192_{tag}.npz", "SFT-набор v13_fixed, вход SFT-стадии"),
]

# Исторический рецепт (`run_v12_ladder.sh` сегодня зовёт именно этот набор):
# существование этих кэшей — факт о дереве, а не признак готовности волны: вход
# волны определяется рецептом, а он у лесенки объявлен как v13_fixed (§7 Н-1).
LEGACY_SFT_CACHES = "sft_train_v12_8192_{tag}.npz"


def sha256_file(path: str, size_hint: int | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def audit_cache(tok_dir: str, name: str, do_hash: bool) -> dict:
    path = os.path.join(tok_dir, name)
    rec = {"path": path, "name": name, "exists": os.path.isfile(path)}
    if rec["exists"]:
        rec["bytes"] = os.path.getsize(path)
        rec["sha256"] = sha256_file(path) if do_hash else None
        rec["sha256_note"] = ("снят прибором" if do_hash else "не снят (нужен --hash-caches)")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", default=os.path.join("evidence", "ladder-weights-inventory.json"))
    ap.add_argument("--out-dir", default=os.path.join("data", "ad2-cards"))
    ap.add_argument("--tok-dir", default=TOK_DIR_DEFAULT)
    ap.add_argument("--hash-caches", action="store_true")
    args = ap.parse_args()

    inv = load_json(os.path.join(CASE_ROOT, args.inventory))
    by_short = {p["short"]: p for p in inv["positions"]}
    sft_card = load_json(os.path.join(CASE_ROOT, "data", "sft-card-v13.json"))
    corpus_card = load_json(os.path.join(CASE_ROOT, "data", "corpus-card.json"))
    out_dir = os.path.join(CASE_ROOT, args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    written, lines = [], []
    for family, meta in FAMILIES.items():
        tag = meta["tag"]
        bases = []
        for short in meta["positions"]:
            pos = by_short[short]
            # Только ВАЛИДНЫЕ веса: файл с именем весов и мусором внутри (LFS-указатель,
            # обрезанная закачка) базой в карточке не становится — иначе карточка
            # зафиксировала бы путь и sha256 того, что весами не является.
            match = next((m for m in pos["matches"]
                          if m.get("weights_count")
                          and (m.get("integrity") or {}).get("safetensors_headers_valid")), None)
            rec = {
                "short": short, "hf_id": pos["hf_id"], "wave": meta["waves"][short],
                "verdict": pos["verdict"],
            }
            if match:
                files = {n: {"bytes": r["bytes"], "sha256": r.get("sha256"),
                             "blob_name_equals_sha256": r.get("blob_name_equals_sha256")}
                         for n, r in match["files"].items()
                         if n.endswith(".safetensors")}
                rec.update({
                    "path": match["path"], "layout": match["layout"],
                    "revision": os.path.basename(match["snapshot"] or ""),
                    "weights_bytes": match["weights_bytes"],
                    "params_estimate_b": match.get("params_estimate_b"),
                    "weights_sha256": files,
                    "config": match.get("config"),
                    "integrity": match.get("integrity"),
                    "tokenizer_facts": match.get("tokenizer_facts"),
                    "source": f"{args.inventory} (прибор tools/inventory_stand_weights.py, read-only замер на стенде)",
                })
            else:
                rec["note"] = ("весов нет: " + "; ".join(
                    f"{m['path']} — {m.get('state')}" for m in pos["matches"])
                    or "каталога нет ни в одном сторе")
            bases.append(rec)

        tok_facts = next((b.get("tokenizer_facts") for b in bases if b.get("tokenizer_facts")), None)
        tok_sha = None
        for b in bases:
            if b.get("path"):
                match = next(m for m in by_short[b["short"]]["matches"] if m.get("weights_count"))
                f = match["files"].get("tokenizer.json")
                if f and f.get("sha256"):
                    tok_sha = f["sha256"]
                    break

        caches, missing = [], []
        for stage, pattern, role in REQUIRED_CACHES:
            name = pattern.format(tag=tag)
            rec = audit_cache(args.tok_dir, name, args.hash_caches)
            rec.update({"stage": stage, "role": role})
            if rec["exists"]:
                caches.append(rec)
            else:
                missing.append({"stage": stage, "name": name, "role": role,
                                "state": "не собран (цена Н-1 плана §7)"})
        legacy = audit_cache(args.tok_dir, LEGACY_SFT_CACHES.format(tag=tag), args.hash_caches)

        card = {
            "card": f"ad2-ladder-{family}",
            "schema": "ad2-model-card/1",
            "date": datetime.now(timezone.utc).date().isoformat(),
            "constraint": ("AD-2 (прогон без манифеста не является доказательством) + "
                           "AD-7 (состав данных — версионируемый конфиг). Новый файл: "
                           "существующие карточки и наборы не правятся (AD-7)"),
            "family": family,
            "ladder_role": (
                "позиции волны В-1 (класс ≤1B): "
                + ", ".join(s for s, w in meta["waves"].items() if w.startswith("В-1"))
                + "; прочие позиции семейства: "
                + ", ".join(f"{s} — {w.split(' ')[0]}" for s, w in meta["waves"].items()
                            if not w.startswith("В-1"))),
            "bases": bases,
            "tokenizer": {
                "tag": tag,
                "hf_id": meta["tokenizer_hf_id"],
                "hf_id_source": meta["tokenizer_source"],
                "tokenizer_json_sha256": tok_sha,
                "vocab_size": (tok_facts or {}).get("vocab_size_dict"),
                "added_tokens": (tok_facts or {}).get("added_tokens"),
                "special_v12_single_id_natively": (tok_facts or {}).get("special_v12_native_single_id"),
                "special_v12_added_by_pipeline": (tok_facts or {}).get("special_v12_added_by_pipeline"),
                "contract": ("AD-3 / C-008: 6 токенов формата v12 обязаны маппиться в единый id "
                             "в обучающем кэше. Токены, которых нет в tokenizer.json, добавляет "
                             "пайплайн (pretokenize_v9.py: add_special_tokens) — единый id обеспечен "
                             "им, а не HF-репозиторием"),
                "one_tokenizer_per_family": True,
            },
            "sets": {
                "cpt_mix": {
                    "name": "cpt_corpus_v12r",
                    "path": "/home/user/gb10-shared/datasets/cpt_corpus_v12r.txt",
                    "cache": f"/home/user/gb10-shared/datasets/tok/cpt_corpus_v12r_8192_{tag}.npy",
                    "sha256_by_tag": corpus_card.get("sha256"),
                    "mix": corpus_card.get("mix"),
                    "card": "data/corpus-card.json (не правится)",
                },
                "sft": {
                    "name": sft_card.get("card"),
                    "path": "/home/user/gb10-shared/datasets/sft_train_v13_fixed.jsonl",
                    "sha256_full": sft_card.get("size", {}).get("sha256_full"),
                    "examples": sft_card.get("size", {}).get("examples"),
                    "max_len": 8192,
                    "normalizer": sft_card.get("path", {}).get("normalizer"),
                    "card": "data/sft-card-v13.json (не правится)",
                },
                "eval_tasks": {
                    "name": "eval_ood_clean",
                    "path": "/home/user/gb10-shared/datasets/eval_ood_clean.jsonl",
                    "sha256": "f94fb8556cabf3d5386fec1569dfd5e4e796b15c97dfc935bf7bd8f9879bb1d6",
                    "tasks": 192,
                    "task_type": "ood_pair",
                    "verifier": "multi_slug_match",
                    "role": "набор задач оси лесенки (judge_mean/slug_accuracy)",
                },
                "eval_ppl": {
                    "k1": {"path": "general_eval_v3.txt", "docs": 200, "role": "общий язык в жанре реплея"},
                    "k2": {"path": "general_eval_k2.txt", "docs": 200, "role": "общий язык вне распределения"},
                    "domain": {"path": "domain_eval_v3.txt", "docs": 200, "role": "домен, отношение к базе < 1"},
                },
            },
            "tokens_cache": {"present": caches, "missing": missing,
                             "audited_at_tok_dir": args.tok_dir,
                             "legacy_recipe_cache": dict(
                                 legacy,
                                 note=("исторический рецепт (`run_v12_ladder.sh` зовёт сегодня "
                                       "именно его): наличие этого файла НЕ закрывает вход волны — "
                                       "вход объявлен как v13_fixed (§7 Н-1 плана)")),},
            "limits": [
                "Карточка описывает ПРЕДМЕТ стадии (база/набор/токенизатор), а не прогон: "
                "тождество конкретного прогона доказывает манифест (C-012).",
                f"sha256 кэшей снят прибором: {'да' if args.hash_caches else 'НЕТ (--hash-caches не передан)'}.",
                "Наличие весов на стенде проверено в двух сторах; отсутствие в общем сторе "
                "не означает отсутствия на стенде и наоборот.",
                "Архитектура qwen3_5: загрузка в стек (transformers/vLLM) карточкой НЕ доказана — "
                "это load-test фазы В-1 (требует стенда).",
            ],
        }
        path = os.path.join(out_dir, f"ad2-{family}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=1)
            f.write("\n")
        written.append(os.path.relpath(path, CASE_ROOT))
        present = [b["short"] for b in bases if b.get("path")]
        lines.append(f"{family}: весов {len(present)}/{len(bases)} ({', '.join(present) or '—'}), "
                     f"кэшей есть {len(caches)}/{len(REQUIRED_CACHES)}, "
                     f"не собрано: {[m['stage'] for m in missing] or '—'}")

    print("\n".join(lines))
    print("записано: " + ", ".join(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
