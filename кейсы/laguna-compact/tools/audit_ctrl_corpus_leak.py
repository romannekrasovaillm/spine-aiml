#!/usr/bin/env python3
"""S3o — проверка утечки eval-наборов PPL в корпуса контрольных рук (AD-7).

Зачем отдельная проверка, если аудит S3m уже сказал «CLEAN». Аудит
(`runs/calib-audit-20260916/leak_check.py`) отвечал на вопрос рук S3m и сверял
только **v1** общего языка (`general_eval.txt`) против источников микса. У
контрольных рук вопрос другой по существу:

* **C1 обучается на ВСЁМ реплей-источнике** (`general_replay_ru.txt`), а не на его
  префиксе: если документ из набора измерения попал в источник, PPL общего языка у
  C1 был бы занижен обучением, и вывод «протокол не разрушает язык» оказался бы
  артефактом утечки. Для C1 проверка идёт по корпусу C1 **целиком**.
* Проба PPL меряет **четыре** набора (v1/v2 × общий/домен), а не два: `v2_general`
  и `v2_domain` в аудите S3m не сверялись вовсе. Здесь сверяются оба общих набора.

Два уровня, оба считаются:

* **A (решающий, вербатим).** Нормализованный документ eval ищется подстрокой в
  нормализованном тексте источника. Это строже сравнения по хешам единиц: находит
  и «документ целиком», и «документ внутри абзаца другого размера».
* **B (INFO, ≥12 слов).** Скользящие 12-граммы по словам: непрерывное общее окно
  ≥12 слов попадает в счёт. Ловит невербатимное заимствование (правка пунктуации,
  вставки), которое уровень A пропустил бы.

Порог 12 слов — нижняя граница, а не доказательство непохожести: короткие штампы
и заголовки в счёт не идут. Это записано в отчёте (`method_limits`), чтобы «0» не
читалось как «доказано отсутствие всякого сходства».

Источники — только чтение, из `gb10-shared` (AD-4: копий не держим).

Коды возврата::

    0 — отчёт записан; вердикт внутри отчёта (CLEAN | LEAK | NOT-VERIFIED)
    2 — NOT-VERIFIED: нет входа (набор или источник не найден)

Запуск::

    python3 tools/audit_ctrl_corpus_leak.py --out runs/ctrl-audit-<ts>/leak.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SHARED = Path("/home/user/gb10-shared")
NGRAM = 12
_WORD = re.compile(r"\w+", re.UNICODE)
_WS = re.compile(r"\s+", re.UNICODE)


def norm(s: str) -> str:
    return _WS.sub(" ", s.lower()).strip()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


#: Разделитель документов набора — тот же, чем режет измеритель
#: (`ppl_probe._ppl_eval`: `split("\n---\n")`). Границей «документ = строка»
#: пользоваться нельзя: у v1 24 документа на 47 строк, у v2 — 200 на 632, и счёт по
#: строкам дал бы число, которого не меряет ни один прибор.
DOC_SEP = "\n---\n"


def eval_docs(path: Path) -> list[str]:
    """Документы набора измерения — ровно те, что меряет проба.

    Единица — блок между разделителями `\n---\n` (v1: 24 документа, v2: 200), без
    фильтра по длине: проверка берёт **надмножество** измеряемого (проба отбрасывает
    документы короче 50 символов), поэтому «нет утечки» относится и к её подмножеству.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = text.split(DOC_SEP) if DOC_SEP in text else text.splitlines()
    return [p.strip() for p in parts if p.strip()]


def shingles(words: list[str]) -> set[str]:
    return {" ".join(words[i:i + NGRAM]) for i in range(max(0, len(words) - NGRAM + 1))}


def _window_hashes(words: list[str], np) -> "np.ndarray":
    """63-битный blake2b каждого 12-граммового окна — устойчивый и широкий хеш.

    Почему не CRC32 (первая редакция уровня C на нём и стояла) и не встроенный
    ``hash``. CRC32 — 32 бита: при 10.1M окон источника и ~60k окон набора ожидается
    ~140 ложных совпадений, и они видны как «документ с общим окном в 12 слов» при
    нуле настоящих (наблюдалось на первой редакции уровня C: набор v1, чистый по
    дословному уровню, показывал попадания; на 63-битном хеше — 0). ``hash`` не
    годится по другой причине: он рандомизирован на процесс
    (PYTHONHASHSEED), то есть отчёт перестал бы воспроизводиться.

    Длину серии ложное совпадение почти не раздувает (соседние окна должны совпасть
    все), поэтому и пороги в отчёте разделены: «≥20 слов» — сигнал, «≥1 окно» — шум.
    """
    import hashlib
    n = max(0, len(words) - NGRAM + 1)
    out = np.empty(n, dtype=np.int64)
    for i in range(n):
        digest = hashlib.blake2b(" ".join(words[i:i + NGRAM]).encode("utf-8"),
                                 digest_size=8).digest()
        out[i] = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
    return out


def doc_overlap(text: str, sets: dict[str, list[str]], np) -> dict:
    """Уровень C: у скольких документов набора есть с источником общее окно ≥12 слов.

    Зачем поверх уровня B. Уровень B отвечает «есть ли хоть одно общее окно на весь
    источник» — этого мало для вопроса «годен ли набор как метрика у этой руки».
    Набор может быть чист для одной руки и заражён для другой: у S3o рука C1 обучается
    на **всём** реплей-источнике, а набор `v2_general` строился (ADR-018 п.3) с
    исключением документов только против префикса пилота (2444 чанка). Уровень C
    считает это по документам: сколько документов набора лежит в корпусе целиком или
    куском, и какова самая длинная общая серия слов.

    Заражение здесь — не «стилистическое сходство»: документ набора, найденный в
    обучающем материале, даёт заниженный PPL именно у этой руки, и вердикт «протокол
    не разрушает язык» становится артефактом утечки. Поэтому число считается и
    попадает в отчёт, а не остаётся на усмотрение читателя.
    """
    words = [w.group(0).lower() for w in _WORD.finditer(text)]
    src = np.sort(_window_hashes(words, np))
    out: dict = {"source_words": len(words), "sets": {}}
    for set_name, docs in sets.items():
        rows = []
        for i, d in enumerate(docs):
            w = [x.group(0).lower() for x in _WORD.finditer(norm(d))]
            if len(w) < NGRAM:
                continue
            hashes = _window_hashes(w, np)
            pos = np.searchsorted(src, hashes)
            pos[pos >= len(src)] = 0
            hit = (src[pos] == hashes)
            cur = mx = 0
            for h in hit:
                cur = cur + 1 if h else 0
                mx = max(mx, cur)
            if mx:
                rows.append({"doc": i, "chars": len(d), "words": len(w),
                             "max_run_words": mx, "head": d[:70]})
        rows.sort(key=lambda r: -r["max_run_words"])
        out["sets"][set_name] = {
            "documents": len(docs),
            "documents_over_50_chars": sum(1 for d in docs if len(d) > 50),
            "docs_with_shared_run": len(rows),
            "docs_run_ge_5": sum(1 for r in rows if r["max_run_words"] >= 5),
            "docs_run_ge_20": sum(1 for r in rows if r["max_run_words"] >= 20),
            "max_run_words": rows[0]["max_run_words"] if rows else 0,
            "examples": rows[:5],
        }
    return out


def scan_shingles(text: str, wanted: set[str]) -> dict:
    """Проход по словам источника: сколько 12-граммовых окон попало в набор eval."""
    hits: list[dict] = []
    n_words = n_windows = n_hits = cur_run = max_run = 0
    window: list[str] = []
    for w in _WORD.finditer(text):
        n_words += 1
        window.append(w.group(0).lower())
        if len(window) > NGRAM:
            del window[0]
        if len(window) == NGRAM:
            n_windows += 1
            if " ".join(window) in wanted:
                n_hits += 1
                cur_run += 1
                max_run = max(max_run, cur_run)
                if len(hits) < 10:
                    hits.append({"text": " ".join(window)[:120]})
            else:
                cur_run = 0
    return {"words": n_words, "windows": n_windows, "hit_windows": n_hits,
            "max_consecutive_hit_windows": max_run, "examples": hits}


def check_source(name: str, path: Path, sets: dict[str, list[str]],
                 norms: dict[str, list[str]], np) -> dict:
    """Один источник против всех наборов измерения сразу (текст читается один раз)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    flat = norm(text)
    entry: dict = {"role": name, "path": str(path), "sha256": sha256_file(path),
                   "bytes": path.stat().st_size, "sets": {}}
    for set_name, docs in norms.items():
        def hits(seq):
            out = []
            for i, d in enumerate(seq):
                if d and d in flat:
                    out.append({"doc": i, "chars": len(d), "head": d[:80]})
            return out

        all_hits = hits(docs)
        #: Решающий счёт — по документам, которые реально меряет проба (>50 символов,
        #: тот же порог, что в `_ppl_eval`). Счёт по всем непустым строкам остаётся
        #: в отчёте, но решающим быть не может: строка из трёх символов («---», «см.»)
        #: находится в любом корпусе по построению и утечкой не является. Первая
        #: редакция считала решающим именно полный список — и печатала «verbatim 23
        #: из 47» там, где настоящих попаданий ноль.
        big = [d for d in docs if len(d) > 50]
        big_hits = hits(big)
        entry["sets"][set_name] = {
            "documents": len(docs),
            "documents_over_50_chars": len(big),
            "verbatim_hits": len(big_hits),
            "verbatim_hits_any_length": len(all_hits),
            "examples": big_hits[:5],
            "examples_any_length": all_hits[:3],
        }
    wanted: set[str] = set()
    for docs in sets.values():
        for d in docs:
            wanted |= shingles(_WORD.findall(norm(d)))
    sc = scan_shingles(text, wanted)
    entry["ngram_scan"] = sc
    #: Уровень C считается по документам: он и отвечает на вопрос «годен ли набор как
    #: метрика у КОНКРЕТНОЙ руки» (см. doc_overlap). Порог «≥1 общее окно» — сигнал,
    #: а не доказательство: у длинных наборов короткие общие окна бывают и случайно.
    entry["doc_overlap"] = doc_overlap(text, sets, np)
    entry["verdict"] = ("LEAK" if any(s["verbatim_hits"] for s in entry["sets"].values())
                        or sc["max_consecutive_hit_windows"] > 0 else "CLEAN")
    entry["verdict_note"] = ("уровень A решается попаданиями документов >50 символов; счёт "
                             "по всем строкам приведён рядом и решающим не является")
    return entry


def main(argv=None) -> int:
    import numpy as np  # noqa: E402 — нужен уровню C (сортировка хешей окон)

    ap = argparse.ArgumentParser(
        description="утечка eval-наборов PPL в корпуса контрольных рук (S3o, AD-7)")
    ap.add_argument("--out", default=None, help="куда записать отчёт (json)")
    ap.add_argument("--shared", default=str(SHARED))
    args = ap.parse_args(argv)
    shared = Path(args.shared)

    eval_files = {
        "v1_general": shared / "datasets" / "general_eval.txt",
        "v2_general": shared / "datasets" / "general_eval_v2.txt",
    }
    sources = {
        # C1: корпус — весь реплей-источник, поэтому проверяется он целиком.
        "ctrl100_c1 (реплей целиком)": shared / "datasets" / "general_replay_ru.txt",
        # C2 (и руки S3m 25 %): домен + реплей микса v12r.
        "v12r_domain_c2": shared / "datasets" / "cpt_corpus_v10.1.txt",
    }
    missing = [str(p) for p in list(eval_files.values()) + list(sources.values())
               if not p.is_file()]
    if missing:
        print("NOT-VERIFIED: нет входа: " + "; ".join(missing), file=sys.stderr)
        return 2

    report: dict = {
        "schema": "ctrl-corpus-leak/1", "stage": "S3o", "ngram": NGRAM,
        "date": datetime.now(timezone.utc).isoformat(),
        "question": "Есть ли в обучающем материале контрольных рук текст наборов, на "
                    "которых меряется PPL?",
        "why": "C1 обучается на ВСЁМ реплей-источнике (не на префиксе), поэтому "
               "утечка занизила бы PPL общего языка именно у C1 — и вывод «протокол "
               "не разрушает язык» стал бы артефактом утечки",
        "sets": {}, "sources": {}, "doc_unit": DOC_SEP, "method": {
            "A": "нормализованный (lower + схлопнутые пробелы) документ eval ищется "
                 "подстрокой в нормализованном тексте источника; решающий счёт — по "
                 "документам >50 символов (как их меряет проба), счёт по всем строкам "
                 "приведён рядом (короткие строки находятся в любом корпусе)",
            "B": "скользящие 12-граммы по словам; общее непрерывное окно ≥12 слов — "
                 "INFO-уровень (вербатимное заимствование не требуется)",
            "C": "по документам: у скольких документов набора есть с источником общее "
                 "непрерывное окно ≥12 слов и какова максимальная серия. Отвечает на "
                 "вопрос «годен ли набор как метрика у конкретной руки»: документ "
                 "набора, найденный в обучающем материале, занижает PPL именно у неё",
        },
        "method_limits": [
            f"порог {NGRAM} слов: совпадения короче (штампы, заголовки, имена) утечкой "
            f"не считаются и не ловятся — это нижняя граница, а не полное доказательство "
            f"непохожести",
            "v1_domain/v2_domain в этой проверке не участвуют: они по построению из "
            "домена и в корпусе C1 домена нет вовсе; у C2 домен тот же, что у рук S3m, "
            "и проверен аудитом S3m",
        ],
    }
    norms: dict[str, list[str]] = {}
    for name, path in eval_files.items():
        docs = eval_docs(path)
        norms[name] = [norm(d) for d in docs]
        report["sets"][name] = {"path": str(path), "sha256": sha256_file(path),
                                "documents": len(docs),
                                "chars": sum(len(d) for d in docs)}
        print(f"набор {name}: {len(docs)} документов, {sum(len(d) for d in docs)} символов",
              flush=True)

    sets = {name: eval_docs(path) for name, path in eval_files.items()}
    for name, path in sources.items():
        print(f"источник {name}: {path.name} — читаю и сверяю…", flush=True)
        entry = check_source(name, path, sets, norms, np)
        report["sources"][name] = entry
        for set_name, s in entry["sets"].items():
            print(f"  {name} × {set_name}: verbatim {s['verbatim_hits']} из "
                  f"{s['documents_over_50_chars']} документов >50 символов "
                  f"(по всем строкам {s['verbatim_hits_any_length']} из {s['documents']})",
                  flush=True)
        sc = entry["ngram_scan"]
        print(f"  {name}: 12-грамм {sc['windows']}, совпавших окон {sc['hit_windows']}, "
              f"макс. серия {sc['max_consecutive_hit_windows']}", flush=True)
        for set_name, d in entry["doc_overlap"]["sets"].items():
            print(f"  {name} × {set_name} (по документам): с общим окном "
                  f"{d['docs_with_shared_run']} из {d['documents_over_50_chars']} "
                  f"(>50 символов), ≥20 слов — {d['docs_run_ge_20']}, "
                  f"макс. серия {d['max_run_words']} слов", flush=True)

    bad = [n for n, e in report["sources"].items() if e["verdict"] != "CLEAN"]
    report["verdict"] = ("LEAK: " + ", ".join(bad)) if bad else "CLEAN"
    report["overlap_docs_total"] = sum(s["verbatim_hits"] for e in report["sources"].values()
                                       for s in e["sets"].values())
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"отчёт: {out}")
    print("вердикт:", report["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
