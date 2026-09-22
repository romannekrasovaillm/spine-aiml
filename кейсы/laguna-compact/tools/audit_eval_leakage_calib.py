#!/usr/bin/env python3
"""A3 calib-protocol-audit: утечка general_eval.txt (v1) и v2 в обучающие корпуса рук S3m.

Методика — та же, что tools/check_eval_leakage.py / tools/build_gen_eval_v2.py:
- нормализация: lower + схлопнутые пробелы;
- гейт B: точное совпадение нормализованного текста документа eval с нормализованной
  единицей обучающего корпуса;
- INFO: доля общих 12-грамм (шинглы по словам) — нижняя граница near-duplicate.

Обучающие корпуса рук:
- домен: cpt_corpus_v10.1.txt (документы по '\\n---\\n', строка '---')
- реплей: general_replay_ru.txt (документы по '\\n\\n' — пустой строке)
- (справочно) sft_train_v12.jsonl, rl_tasks_revpool_v1.jsonl — на случай, если
  PPL-прибор калибровки касается и они.

Наборы: general_eval.txt (sha 11b3164d…), domain_eval.txt, general_eval_v2.txt.
Реплей-часть, обучаемая руками: v12r = первые 2444 чанка, v12r50 = 3493 чанка
(по 8192 токена) — позиционные границы считаем токенайзером Qwen2.5 (как
build_gen_eval_v2.py), чтобы проверить и вхождение в ОБА обученных префикса.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_gen_eval_v2 import (  # noqa: E402
    DOC_SEP, PPLEVAL_MIN_CHARS, ShingleIndex, iter_blocks, load_tokenizer,
    norm, parse_eval_file,
)

SHARED = Path("/home/user/gb10-shared")
OUT = Path("/home/user/.arch-ml/worktrees/spine-aiml-a366f5f2a510b02d/"
           "laguna-fleet-analysis/кейсы/laguna-compact/runs/calib-audit-20260916")

NGRAM = 12
CHUNK_TOKENS = 8192
REPLAY_PREFIX_V12R = 2444 * CHUNK_TOKENS    # чему учит реплей-часть v12r (руки 25%)
REPLAY_PREFIX_V12R50 = 3493 * CHUNK_TOKENS  # чему учит реплей-часть v12r50 (руки 50%)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def digest16(text: str) -> bytes:
    return hashlib.blake2b(norm(text).encode("utf-8"), digest_size=16).digest()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"schema": "calib-eval-leakage/1", "ngram": NGRAM, "sets": {},
                    "training_material": {}, "notes": []}

    # ── токенизатор (позиционные границы реплей-префиксов) ─────────────────────
    tok = None
    try:
        tok = load_tokenizer("auto")
        report["tokenizer"] = str(tok)
    except Exception as e:  # noqa: BLE001
        report["notes"].append(f"токенизатор недоступен ({e}) — позиционная проверка выключена")

    # ── обучающий материал: домен v10.1 (это ВЕСЬ домен обеих рук) ─────────────
    dom_path = SHARED / "datasets" / "cpt_corpus_v10.1.txt"
    dom_hashes: set[bytes] = set()
    dom_sh = ShingleIndex(NGRAM)
    n_dom = 0
    for block in iter_blocks(dom_path, blank_line=False):
        dom_hashes.add(digest16(block))
        dom_sh.add_text(block)
        n_dom += 1
    dom_sh.finalize()
    report["training_material"]["domain_v10.1"] = {
        "path": str(dom_path), "sha256": sha256_file(dom_path), "units": n_dom}

    # ── обучающий материал: реплей (с разметкой позиций токенов) ────────────────
    rep_path = SHARED / "datasets" / "general_replay_ru.txt"
    rep_hashes: set[bytes] = set()
    rep_docs: list[dict] = []  # {text, span_start, span_end}
    pos = 0
    n_rep = 0
    for i, doc in enumerate(iter_blocks(rep_path, blank_line=True)):
        if len(doc) < PPLEVAL_MIN_CHARS:
            continue
        n_rep += 1
        n_tok = len(tok.encode(doc, add_special_tokens=False).ids) if tok else 0
        rep_docs.append({"i": i, "start": pos, "end": pos + n_tok, "chars": len(doc),
                         "text": doc})
        pos += n_tok
        rep_hashes.add(digest16(doc))
    report["training_material"]["replay"] = {
        "path": str(rep_path), "sha256": sha256_file(rep_path), "docs": n_rep,
        "total_tokens": pos,
        "prefix_v12r_tokens": REPLAY_PREFIX_V12R, "prefix_v12r50_tokens": REPLAY_PREFIX_V12R50}

    # шингл-индексы обученных префиксов реплея
    def shingles_of_prefix(limit_tokens: int) -> ShingleIndex:
        ix = ShingleIndex(NGRAM)
        for d in rep_docs:
            if d["start"] >= limit_tokens:
                break
            ix.add_text(d["text"])
        ix.finalize()
        return ix

    rep_sh_v12r = shingles_of_prefix(REPLAY_PREFIX_V12R)      # обучено руками 25%
    rep_sh_v12r50 = shingles_of_prefix(REPLAY_PREFIX_V12R50)  # обучено руками 50%
    rep_sh_all = ShingleIndex(NGRAM)
    for d in rep_docs:
        rep_sh_all.add_text(d["text"])
    rep_sh_all.finalize()

    # ── проверка наборов ────────────────────────────────────────────────────────
    for set_name, fname in (("general_v1", "general_eval.txt"),
                            ("domain_v1", "domain_eval.txt"),
                            ("general_v2", "general_eval_v2.txt"),
                            ("domain_v2", "domain_eval_v2.txt")):
        p = SHARED / "datasets" / fname
        docs = parse_eval_file(p)
        entry: dict = {"path": str(p), "sha256": sha256_file(p), "documents": len(docs),
                       "exact_hits": [], "ngram": {}}

        for idx, d in enumerate(docs):
            dg = digest16(d)
            hits = []
            if dg in dom_hashes:
                hits.append("domain_v10.1")
            if dg in rep_hashes:
                hits.append("replay_general")
            if hits:
                entry["exact_hits"].append({"doc": idx, "in": hits,
                                            "head": d[:70]})
            # шинглы: обученный префикс реплея v12r / v12r50, весь реплей, весь домен
            for label, ix in (("replay_prefix_v12r(25%)", rep_sh_v12r),
                              ("replay_prefix_v12r50(50%)", rep_sh_v12r50),
                              ("replay_all", rep_sh_all),
                              ("domain_v10.1_all", dom_sh)):
                sh, total = ix.shared(d)
                if sh:
                    entry["ngram"].setdefault(label, []).append(
                        {"doc": idx, "shared": sh, "total": total,
                         "frac": round(sh / total, 4) if total else 0.0})
        # агрегация долей
        agg = {}
        for label, ix in (("replay_prefix_v12r(25%)", rep_sh_v12r),
                          ("replay_prefix_v12r50(50%)", rep_sh_v12r50),
                          ("replay_all", rep_sh_all),
                          ("domain_v10.1_all", dom_sh)):
            fracs = []
            for d in docs:
                sh, total = ix.shared(d)
                fracs.append((sh / total) if total else 0.0)
            fracs.sort()
            agg[label] = {
                "docs_with_shared_ngram": sum(1 for f in fracs if f > 0),
                "max_frac": round(fracs[-1], 4) if fracs else 0.0,
                "median_frac_nonzero": round(fracs[len(fracs) // 2], 4),
                "docs_over_0.5": sum(1 for f in fracs if f >= 0.5),
                "docs_over_0.9": sum(1 for f in fracs if f >= 0.9),
            }
        entry["ngram_summary"] = agg
        entry["verdict"] = ("LEAK: точное совпадение с обучающим материалом"
                            if entry["exact_hits"] else
                            "нет точных совпадений (шинглы — информативно)")
        report["sets"][set_name] = entry
        print(f"== {set_name}: {len(docs)} доков, точных попаданий "
              f"{len(entry['exact_hits'])}, max_frac(v12r50-префикс)="
              f"{agg['replay_prefix_v12r50(50%)']['max_frac']}")

    (OUT / "eval_leakage.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"отчёт: {OUT / 'eval_leakage.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
