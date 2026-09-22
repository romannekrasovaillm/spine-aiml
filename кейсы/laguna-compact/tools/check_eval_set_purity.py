#!/usr/bin/env python3
"""S3q — гейт ADR-025 п.1: измерительный набор не пересекается ни с одним миксом.

Зачем отдельный инструмент. ADR-025 п.1 объявляет **инвариант**: каждый набор
``general_eval*`` обязан иметь нулевое пересечение с каждым **кандидатным** CPT-миксом,
а не только с текущим. Инвариант без стража доходит до дельты руками — ровно так и
реализовался дефект v2 (200/200 документов оказались обучающим текстом v12r50):
фильтр ADR-018 отсеивал пересечение с **v12r**, и при смене микса «чистый» набор
тихо стал обучающим. Поэтому проверка здесь — гейт с кодом возврата, а не абзац в
методике.

Что именно проверяется. Два независимых условия на каждый микс:

* **документы** — нормализованный текст документа набора (lower + схлопнутые
  пробелы, нормализация `tools/check_eval_leakage.py`) найден **подстрокой** в
  нормализованном тексте корпуса-источника. Подстрокой, а не равенством: набор и
  реплей — статьи одного дампа, и статья могла попасть в обучение частями;
  равенство документов такую утечку пропустило бы;
* **12-граммовые окна** — окна по 12 слов внутри документа (та же длина, что в
  аудите S3m). Ловит near-duplicate, который точное сравнение пропускает:
  переписанный абзац, склейку двух статей, шаблонный кусок.

Почему проверка идёт по корпусу-источнику **целиком**, а не по префиксу микса.
Микс — это префикс потока корпуса (``chunks[:N]``), а префикс — подмножество файла.
Значит, ноль пересечений с файлом **сильнее** нуля пересечений с префиксом: если
документ набора не встречается нигде в файле, он не встречается и в обученной части.
Обратное неверно, поэтому проверять по префиксу было бы слабее, а не дешевле:
сначала пришлось бы токенизировать 300 МБ корпуса, чтобы узнать границу префикса.
Состав каждого микса (домен/реплей в чанках) при этом печатается — чтобы было
видно, **что** именно покрывает проверка.

Коды возврата::

    0 — чисто по всем миксам (оба нуля у каждого)
    1 — пересечение найдено: нарушение ADR-025 п.1, набор негоден для этих миксов
    2 — NOT-VERIFIED: нет набора или корпуса — не зелёный

Запуск::

    python3 tools/check_eval_set_purity.py --set datasets/general_eval_v3.txt
    python3 tools/check_eval_set_purity.py --set datasets/general_eval_v3.txt --json
    python3 tools/check_eval_set_purity.py --set datasets/general_eval_v2.txt --report evidence/x.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Корпуса-источники всех кандидатных миксов. Роли названы так же, как в карточках
#: миксов (``sources.domain`` / ``sources.replay``), чтобы состав читался буквально.
DOMAIN = "/home/user/gb10-shared/datasets/cpt_corpus_v10.1.txt"
REPLAY = "/home/user/gb10-shared/datasets/general_replay_ru.txt"

#: Разделители документов корпусов — те же, чем корпуса резались при сборке миксов
#: (``tools/build_mix_v12r50.py``: домен ``\\n---\\n``, реплей ``\\n\\n``). Окна
#: считаются **внутри** документа: окно, склеенное из двух документов, в обучении
#: не встречалось, и считать его совпадением значило бы ловить собственную склейку.
DOMAIN_SEP = "\n---\n"
REPLAY_SEP = "\n\n"

#: Разделитель документов **набора** — как режет прибор (``_ppl_eval``).
SET_SEP = "\n---\n"

#: Длина окна. 12 — как в аудите S3m и в INFO-сигнале ``check_eval_leakage.py``:
#: короче ловит общие обороты русского языка, длиннее перестаёт ловить склейки.
WINDOW = 12

#: ── Реестр кандидатных миксов ───────────────────────────────────────────────
#: Состав зафиксирован карточками (ADR-018/C-010): у каждого микса названы домен и
#: реплей в чанках по 8192 токена. Числа нужны для отчёта («что покрыто»), сама
#: проверка идёт по файлам целиком — см. докстринг.
CANDIDATE_MIXES: dict[str, dict] = {
    "v12r": {
        "domain_chunks": 7332, "replay_chunks": 2444, "chunk_len": 8192,
        "card": "data/corpus-card.json",
        "note": "пилот S3; домен — весь пул v10.1 (7332 чанка), реплей — первые 2444",
    },
    "v12r50": {
        "domain_chunks": 3493, "replay_chunks": 3493, "chunk_len": 8192,
        "card": "data/corpus-card-v12r50.json",
        "note": "калибровка S3m; равные доли, реплей — все 3493 чанка источника",
    },
    "ctrl100": {
        "domain_chunks": 0, "replay_chunks": 3493, "chunk_len": 8192,
        "card": "/home/user/gb10-shared/calib/ctrl-build-20260916-1623/mix-ctrl100-build.json",
        "note": "контрольная рука C1 (S3o): домена нет вовсе, обучение только на общем языке",
    },
}

_WS = re.compile(r"\s+")


def note(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def norm(text: str) -> str:
    """Нормализация — та же, что в ``tools/check_eval_leakage.py`` и сборщике набора."""
    return _WS.sub(" ", (text or "").lower()).strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def own_revision() -> dict:
    me = Path(__file__)
    if not me.is_file():
        raise SystemExit(f"ОТКАЗ: инструмент запущен без файла на диске ({me})")
    return {"path": "tools/check_eval_set_purity.py", "sha256": sha256_file(me)}


# ─────────────────────────── набор ───────────────────────────────────────────


def read_set(path: Path) -> dict:
    """Документы набора — ровно как их видит прибор (``split`` + фильтр длины)."""
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    docs = [d.strip() for d in text.split(SET_SEP) if len(d.strip()) > 50]
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "docs": docs}


def windows(words: list[str]) -> list[str]:
    """12-граммовые окна внутри документа (список, не множество: нужен поиск причины)."""
    n = len(words)
    if n < WINDOW:
        return []
    return [" ".join(words[i:i + WINDOW]) for i in range(n - WINDOW + 1)]


# ─────────────────────────── корпус-источник ─────────────────────────────────


def scan_corpus(path: Path, sep: str, targets: set[str],
                docs_norm: list[str]) -> dict:
    """Пересечение **искомого множества** с корпусом: документы подстрокой + окна.

    Один и тот же проход обслуживает два вопроса, и это намеренно:

    * гейт (``check_eval_set_purity``) спрашивает «что из набора встречается в
      корпусе» — ``targets`` это окна набора, ``docs_norm`` его документы;
    * сборщик набора спрашивает «что из пула кандидатов встречается в корпусе» —
      ``targets`` это окна пула: тогда ``matched`` говорит, какие кандидаты грязны
      **до** того, как набор собран, и инвариант выполняется по построению.

    Две копии этого прохода означали бы две правды о том, что считалось совпадением.

    Проход потоковый: корпус читается, нормализуется и режется на документы по
    одному, а ``targets`` лежит в памяти целиком. Обратный порядок (множество окон
    корпуса в память) на 288 МБ текста — это десятки миллионов строк, то есть
    гигабайты на ровном месте.

    Окно ищется по «якорю» — первому слову: ``join`` двенадцати слов делается
    только там, где первое слово вообще встречается в ``targets``. Это не
    приближение: окно, которого нет в множестве, совпасть не может, и ``join``
    для него — работа впустую.
    """
    anchor = {w.split(" ", 1)[0] for w in targets}
    t0 = time.time()
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = text.split(sep)
    del text

    doc_hits: list[dict] = []
    matched: set[str] = set()
    occurrences = 0
    n_docs = 0
    for doc in parts:
        d = norm(doc)
        if not d:
            continue
        n_docs += 1
        for i, want in enumerate(docs_norm):
            if want and want in d:
                doc_hits.append({"target_index": i, "source_doc_chars": len(d)})
        w = d.split()
        if len(w) < WINDOW:
            continue
        for i in range(len(w) - WINDOW + 1):
            if w[i] in anchor:
                s = " ".join(w[i:i + WINDOW])
                if s in targets:
                    occurrences += 1
                    matched.add(s)
    return {
        "path": str(path), "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "source_docs": n_docs,
        "overlap_docs": len(doc_hits),
        "overlap_ngram_windows": len(matched),
        "overlap_ngram_occurrences": occurrences,
        "doc_hits": doc_hits[:20],
        "window_examples": sorted(matched)[:20],
        "matched": matched,
        "seconds": round(time.time() - t0, 1),
    }


def discover_extra_mixes(calib_dir: Path) -> dict[str, dict]:
    """Миксы, появившиеся в ``calib/`` **после** объявления реестра (ADR-025 п.5).

    Новый кандидатный микс обязан быть проверен до своего замера, а не после.
    Инструмент не может знать о нём заранее, поэтому он ищет карточки сборки
    (``mix-*-build.json``) и берёт источники **из самой карточки** — если карточка
    называет корпуса, которых нет в реестре, это видно в отчёте, а не молчит.
    """
    found: dict[str, dict] = {}
    if not calib_dir.is_dir():
        return found
    for card in sorted(calib_dir.glob("*/mix-*-build.json")) + sorted(calib_dir.glob("mix-*-build.json")):
        try:
            data = json.loads(card.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        src = data.get("sources") or {}
        mix = data.get("mix") or {}
        name = mix.get("out_stem") or card.stem
        if name in CANDIDATE_MIXES or name in found:
            continue
        found[name] = {
            "domain_chunks": mix.get("domain_chunks"),
            "replay_chunks": mix.get("replay_chunks"),
            "chunk_len": mix.get("chunk_len"),
            "card": str(card),
            "sources_from_card": {role: (val or {}).get("path")
                                  for role, val in src.items() if isinstance(val, dict)},
            "note": "найден в calib/ автоматически: карточка сборки, не реестр",
        }
    return found


# ─────────────────────────── отчёт ───────────────────────────────────────────


def build(args) -> tuple[dict, int]:
    path = Path(args.set)
    if not path.is_file():
        print(f"NOT-VERIFIED: нет набора {path}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    ds = read_set(path)
    note(f"набор {path}: {len(ds['docs'])} документов, sha256 {ds['sha256'][:12]}")

    set_docs_norm = [norm(d) for d in ds["docs"]]
    set_windows: set[str] = set()
    for d in set_docs_norm:
        set_windows.update(windows(d.split()))
    note(f"окон набора: {len(set_windows)} (по {WINDOW} слов)")

    corpora: dict[str, str] = {"domain": args.domain, "replay": args.replay}
    missing = [f"{role}: {p}" for role, p in corpora.items() if not Path(p).is_file()]
    if missing:
        print("NOT-VERIFIED: нет корпуса: " + "; ".join(missing), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    scanned: dict[str, dict] = {}
    matched_by_role: dict[str, set[str]] = {}
    for role, p in corpora.items():
        sep = DOMAIN_SEP if role == "domain" else REPLAY_SEP
        res = scan_corpus(Path(p), sep, set_windows, set_docs_norm)
        matched_by_role[role] = res.pop("matched")
        scanned[role] = res
        note(f"корпус {role}: документов {res['source_docs']}, "
             f"пересечение документов {res['overlap_docs']}, "
             f"окон {res['overlap_ngram_windows']} "
             f"(вхождений {res['overlap_ngram_occurrences']}) ({res['seconds']} с)")

    mixes = dict(CANDIDATE_MIXES)
    mixes.update(discover_extra_mixes(Path(args.calib_dir)))

    matrix = []
    for name, mix in mixes.items():
        roles = [r for r in ("domain", "replay")
                 if (mix.get(f"{r}_chunks") or 0) > 0]
        #: Окна объединяются множеством, а не складываются: одно и то же окно,
        #: найденное в обоих корпусах, — одно нарушение, а не два.
        shared = set().union(*(matched_by_role[r] for r in roles)) if roles else set()
        docs_hits = sum(scanned[r]["overlap_docs"] for r in roles)
        matrix.append({
            "mix": name,
            "composition": {f"{r}_chunks": mix.get(f"{r}_chunks") for r in ("domain", "replay")},
            "chunk_len": mix.get("chunk_len"),
            "card": mix.get("card"),
            "checked_corpora": roles,
            "overlap_docs": docs_hits,
            "overlap_ngram": len(shared),
            "verdict": "clean" if docs_hits == 0 and not shared else "overlap",
        })
    verdict = "clean" if all(m["verdict"] == "clean" for m in matrix) else "overlap"

    report = {
        "schema": "eval-set-purity/1",
        "stage": "S3q",
        "date": datetime.now(timezone.utc).isoformat(),
        "adr": ["ADR-025 п.1", "ADR-025 п.2", "ADR-025 п.5"],
        "tool": {**own_revision(), "argv": sys.argv[1:]},
        "set": {"path": ds["path"], "sha256": ds["sha256"], "bytes": ds["bytes"],
                "docs": len(ds["docs"])},
        "instrument": {
            "doc_overlap": ("нормализованный текст документа набора (lower + схлопнутые "
                            "пробелы) как подстрока нормализованного документа корпуса"),
            "ngram_overlap": (f"окна по {WINDOW} слов внутри документа; считается, "
                              f"сколько окон корпуса совпало с окнами набора"),
            "window_size": WINDOW,
            "corpora_checked_in_full": (
                "проверка идёт по корпусу-источнику целиком, а микс — его префикс: "
                "ноль по файлу строже нуля по префиксу (докстринг инструмента)"),
            "set_docs_norm": len(set_docs_norm), "set_windows": len(set_windows),
        },
        "corpora": scanned,
        "overlap_matrix": matrix,
        "verdict": verdict,
    }
    rc = EXIT_OK if verdict == "clean" else EXIT_FAIL
    return report, rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="S3q/ADR-025: гейт чистоты измерительного набора против кандидатных миксов")
    ap.add_argument("--set", required=True, help="файл набора (general_eval*.txt)")
    ap.add_argument("--domain", default=DOMAIN, help="доменный корпус")
    ap.add_argument("--replay", default=REPLAY, help="корпус реплея")
    ap.add_argument("--calib-dir", default="/home/user/gb10-shared/calib",
                    help="каталог, где ищутся карточки новых миксов")
    ap.add_argument("--report", default=None, help="куда записать отчёт (json)")
    ap.add_argument("--json", action="store_true", help="отчёт в stdout")
    args = ap.parse_args(argv)

    report, rc = build(args)
    if args.report and report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"отчёт: {out}")
    if not report:
        return rc
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return rc
    print(f"\n== чистота набора {report['set']['path']} (ADR-025 п.1) ==")
    print(f"набор: {report['set']['docs']} документов, "
          f"{report['instrument']['set_windows']} окон по {WINDOW} слов")
    print(f"{'микс':12s} {'состав (домен/реплей, чанки)':32s} {'док':>5s} {'окон':>6s}  вердикт")
    for m in report["overlap_matrix"]:
        c = m["composition"]
        comp = f"{c['domain_chunks']}/{c['replay_chunks']} × {m['chunk_len']}"
        print(f"{m['mix']:12s} {comp:32s} {m['overlap_docs']:5d} {m['overlap_ngram']:6d}  "
              f"{m['verdict']}")
    print(f"\nвердикт: {report['verdict'].upper()}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
