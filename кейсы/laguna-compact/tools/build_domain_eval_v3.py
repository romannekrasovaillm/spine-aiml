#!/usr/bin/env python3
"""build_domain_eval_v3.py — S3ao: домен-набор из **другого** корпуса (ADR-043 п.3).

**Зачем новый набор, а не правка старого.** `domain_eval_v2.txt` собран из
`cpt_corpus_full.txt` — полного дампа **того же семейства карточек**, из которого
собран обучающий корпус `cpt_corpus_v10.1`. Определения концептов дословно
цитируются в `<tool_response>` обучающих траекторий, поэтому 119 документов из 200
делят с обучением 12-граммы. Доменное отношение ×0.35 на таком наборе частично
показывает **узнавание формулировок**, а не обобщение (ADR-043 п.3, ADR-025 п.6).

Набор из **другого источника** снимает это ограничение: падение доменного PPL на
нём нельзя объяснить знакомством с текстом обучения.

## Из чего собирается

Русскоязычные ML/AI-тексты **вне обучения**: рабочие разборы, отчёты и рецепты по
статьям из архива `~/Документы/КОД/gigachat/РАЗБОРЫ` (DOCX — разборы статей,
обзоры архитектур, отчёты по проектам; MD/TXT — то же в текстовом виде). Это
**другой корпус**, чем библиотека концептов Ariadna: другой жанр (рабочий документ,
а не карточка), другой авторский слой, другие источники.

## Три ступени, у каждой — своя проверка

1. **Извлечение.** DOCX читается `python-docx` (абзацы **и** ячейки таблиц: в
   разборах таблицы несут содержательный текст, и без них документ был бы обрывком).
   Кандидат отбрасывается, если: текста меньше `--min-chars`, доля кириллицы среди
   букв ниже `--min-cyr` (английские статьи для русского доменного PPL не годятся —
   TASK S3ao п.1), ML-лексика ниже `--min-ml` (нужен **домен**, а не любой русский
   текст), либо текст-двойник уже взят (`sha256` содержимого — в архиве лежат копии
   одного документа под разными именами и в разных каталогах).
2. **Сборка документов.** Текст режется на блоки по пустой строке; блоки
   сшиваются подряд до `--doc-target` символов, но не больше `--doc-max` (формат
   прибора: `tokenizer.encode(doc)[:1024]`, поэтому документ длиннее ~4000 символов
   читался бы только началом — это уже не документ, а его огрызок). Слишком
   короткие документы отбрасываются порогом прибора (`len > 50`).
   Документ, внутри которого оказался разделитель набора `\\n---\\n`, **разрезается
   по нему**: иначе при чтении он распался бы на части, и записанное множество
   перестало бы совпадать с измеренным.
3. **Отбор.** Детерминированный: пути сортируются, кандидаты перемешиваются
   `RandomState(--seed)`, документы берутся в этом порядке с ограничением
   `--max-per-source` на исходный файл (иначе одна большая подборка заняла бы весь
   набор, и «200 документов» оказались бы одним документом в 200 кусках).

**Чего инструмент НЕ делает.** Он не фильтрует кандидатов по пересечению с
обучением. Это осознанно: отбор «чтобы гейт позеленел» сделал бы гейт
бессмысленным (ADR-025: гейт — проверка, а не настройка). Пересечение меряется
отдельно, `tools/check_measurement_overlap.py`, и его результат — свойство
источника, а не сборки.

Коды возврата::

    0 — набор собран (или проверка --verify пройдена)
    1 — пул меньше требуемого объёма
    2 — NOT-VERIFIED: нет корня поиска / нечего извлекать

Запуск::

    python3 tools/build_domain_eval_v3.py --plan
    python3 tools/build_domain_eval_v3.py --out /home/user/gb10-shared/datasets/domain_eval_v3.txt \\
        --card runs/s3ao-domain-v3-<ts>/domain-eval-v3-card.json --pool-out runs/.../pool.jsonl
    python3 tools/build_domain_eval_v3.py --verify <файл набора>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Разделитель документов набора: ровно то, чем режет `_ppl_eval`
#: (`laguna_pipeline_v8.py`) и `ppl_probe.split_docs`.
DOC_SEP = "\n---\n"
#: Минимальная длина документа в приборе: `len(d.strip()) > 50`.
PPLEVAL_MIN_CHARS = 50

#: Корни по умолчанию — архив разборов. Домен здесь ML/AI-разборы, а не архив
#: целиком: в «Архив/» лежат и личные заметки, и они отсеиваются языковой и
#: тематической проверкой, а не именем каталога.
DEFAULT_ROOTS = ["~/Документы/КОД/gigachat/РАЗБОРЫ"]

#: Расширения-источники. PDF читается `pdftotext`, если он есть; сканы без
#: текстового слоя дают пустой текст и отсеиваются порогом `--min-chars`.
DOCX_EXT, TEXT_EXT, PDF_EXT = {".docx"}, {".md", ".txt", ".rst"}, {".pdf"}

_CYR = re.compile(r"[а-яёА-ЯЁ]")
_LAT = re.compile(r"[a-zA-Z]")
_WS = re.compile(r"[ \t ]+")
_MULTINL = re.compile(r"\n{3,}")

#: ML-лексика: без неё «русский текст» — это не домен. Список намеренно грубый
#: (подсчёт вхождений подстрок), потому что задача — отделить ML-разбор от
#: протокола совещания, а не классифицировать жанр.
ML_TERMS = [
    "обучени", "модел", "агент", "нейросет", "токен", "контекст", "промпт", "градиент",
    "трансформер", "эмбедд", "инференс", "датасет", "бенчмарк", "токенизац", "оптимизац",
    "функци потерь", "логит", "эпох", "файнтюн", "разметк", "валидац", "переобучен",
    "llm", "gpt", "rlhf", "sft", "lora", "rl ", "grpo", "ppo", "rag", "mcp", "vllm",
    "attention", "transformer", "fine-tun", "benchmark", "inference", "prompt",
    "agent", "reward", "rollout", "tokeniz", "dataset", "embedding", "perplexity",
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_ws(text: str) -> str:
    """Схлопнуть пробелы внутри строк, оставив абзацную структуру.

    Пустая строка — граница блока, и она нужна дальше; тройные переводы строк
    (артефакт конвертации docx) сворачиваются до двойного.
    """
    lines = [_WS.sub(" ", ln).rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return _MULTINL.sub("\n\n", "\n".join(lines)).strip()


def read_docx(path: Path) -> str:
    import docx
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            # Ячейки одной строки — одна строка текста: иначе таблица рассыпается
            # на столбец обрывков, и блоком становится «да» / «нет» / «100».
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def read_pdf(path: Path) -> str:
    import subprocess
    try:
        p = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", str(path), "-"],
                           capture_output=True, timeout=120, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.decode("utf-8", errors="replace") if p.returncode == 0 else ""


def cyr_share(text: str) -> float:
    c, l = len(_CYR.findall(text)), len(_LAT.findall(text))
    return c / (c + l) if (c + l) else 0.0


def ml_hits(text: str) -> int:
    low = text.lower()
    return sum(low.count(t) for t in ML_TERMS)


def iter_source_files(roots: list[Path], exts: set[str]):
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            if p.suffix.lower() not in exts:
                continue
            if p.resolve() in seen:
                continue
            seen.add(p.resolve())
            yield p


def extract(p: Path) -> tuple[Path, str]:
    try:
        if p.suffix.lower() in DOCX_EXT:
            return p, norm_ws(read_docx(p))
        if p.suffix.lower() in TEXT_EXT:
            return p, norm_ws(p.read_text(encoding="utf-8", errors="replace"))
        return p, norm_ws(read_pdf(p))
    except Exception:            # noqa: BLE001 — битый файл это «нет кандидата», а не отказ
        return p, ""


def build_pool(args) -> tuple[list[dict], dict]:
    roots = [Path(r).expanduser() for r in args.root]
    missing = [str(r) for r in roots if not r.is_dir()]
    exts = set()
    for e in args.ext.split(","):
        e = e.strip().lower()
        if e == "docx":
            exts |= DOCX_EXT
        elif e in ("md", "txt"):
            exts |= TEXT_EXT
        elif e == "pdf":
            exts |= PDF_EXT
        elif e:
            exts.add("." + e.lstrip("."))
    stats = {"roots": [str(r) for r in roots], "roots_missing": missing,
             "extensions": sorted(exts),
             "files_seen": 0, "excluded": defaultdict(int), "documents_read": 0}
    files = list(iter_source_files(roots, exts))
    stats["files_seen"] = len(files)
    # Извлечение — самая дорогая часть (разбор DOCX/PDF), и она независима по
    # файлам. Пул процессов здесь не оптимизация ради скорости: на одном процессе
    # сборка идёт минутами, и это отбивает повторный прогон, а повторяемость
    # сборки — часть её проверяемости.
    texts: list[tuple[Path, str]] = []
    if args.jobs > 1 and len(files) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(args.jobs) as ex:
            for r in ex.map(extract, files, chunksize=4):
                texts.append(r)
    else:
        texts = [extract(p) for p in files]

    pool: list[dict] = []
    by_text: dict[str, str] = {}
    for p, text in texts:
        if not text:
            stats["excluded"]["unreadable_or_empty"] += 1
            continue
        stats["documents_read"] += 1
        if len(text) < args.min_chars:
            stats["excluded"]["too_short"] += 1
            continue
        if cyr_share(text) < args.min_cyr:
            stats["excluded"]["not_russian"] += 1
            continue
        if ml_hits(text) < args.min_ml:
            stats["excluded"]["not_ml"] += 1
            continue
        key = sha256_text(text)
        if key in by_text:
            stats["excluded"]["duplicate_text"] += 1
            continue
        by_text[key] = str(p)
        pool.append({"path": str(p), "chars": len(text), "cyr": round(cyr_share(text), 4),
                     "ml_hits": ml_hits(text), "text_sha256": key, "_text": text})
    stats["pool"] = len(pool)
    stats["excluded"] = dict(stats["excluded"])
    return pool, stats


def form_documents(text: str, target: int, doc_max: int) -> list[str]:
    """Блоки текста → документы набора: сшивка до ``target``, потолок ``doc_max``.

    Разрезание по ``\\n---\\n`` — не косметика: прибор режет набор именно по этой
    строке, и неразрезанный документ при чтении распался бы на части, а
    отфильтрованное множество перестало бы совпадать с записанным.
    """
    blocks = [b.strip() for b in text.split("\n\n")]
    blocks = [b for b in blocks if b]
    # Блок длиннее потолка разрезается сам: без этого один абзац в 500 КБ стал бы
    # одним «документом», прибор прочитал бы его первые 1024 токена, и набор из
    # 200 документов оказался бы набором из одного документа и 199 огрызков.
    # Режется по ближайшему пробелу/переводу строки, а не по символу: разрезать
    # слово пополам значило бы измерить PPL на несуществующем тексте.
    expanded: list[str] = []
    for b in blocks:
        if len(b) <= doc_max:
            expanded.append(b)
            continue
        i = 0
        while i < len(b):
            end = min(i + target, len(b))
            if end < len(b):
                cut = b.rfind(" ", i + target // 2, end)
                if cut == -1:
                    cut = b.rfind("\n", i + target // 2, end)
                end = cut if cut > 0 else end
            piece = b[i:end].strip()
            if piece:
                expanded.append(piece)
            i = end
    blocks = expanded
    docs: list[str] = []
    cur: list[str] = []
    size = 0
    for b in blocks:
        if size and size + len(b) + 2 > doc_max:
            docs.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(b)
        size += len(b) + 2
        if size >= target:
            docs.append("\n\n".join(cur))
            cur, size = [], 0
    if cur:
        docs.append("\n\n".join(cur))
    out: list[str] = []
    for d in docs:
        d = d.strip()
        if len(d) <= PPLEVAL_MIN_CHARS:
            continue
        for piece in d.split(DOC_SEP):
            piece = piece.strip()
            if len(piece) > PPLEVAL_MIN_CHARS:
                out.append(piece)
    return out


def render_docs(docs: list[str]) -> str:
    for d in docs:
        if DOC_SEP in d:
            raise ValueError("документ содержит разделитель набора — формат не round-trip")
    return DOC_SEP.join(docs) + "\n"


def parse_eval_file(path: Path) -> list[str]:
    return [d.strip() for d in
            Path(path).read_text(encoding="utf-8", errors="replace").split(DOC_SEP)
            if len(d.strip()) > PPLEVAL_MIN_CHARS]


def length_stats(docs: list[str]) -> dict:
    ls = sorted(len(d) for d in docs)
    if not ls:
        return {}
    return {"min": ls[0], "median": ls[len(ls) // 2], "max": ls[-1],
            "mean": round(sum(ls) / len(ls), 1), "chars_total": sum(ls)}


def build(args) -> dict:
    pool, stats = build_pool(args)
    if not pool:
        return {"status": "blocked", "reason": "пул пуст", "stats": stats}

    rng = random.Random(args.seed)
    order = list(range(len(pool)))
    rng.shuffle(order)

    # Документы каждого источника — один раз; порядок источников — из перемешанного
    # списка, поэтому «какие источники попали» не зависит от порядка обхода ФС.
    per_source_docs: dict[str, list[str]] = {}
    src_of: dict[str, dict] = {}
    for i in order:
        p = pool[i]
        docs_i = form_documents(p["_text"], args.doc_target, args.doc_max)
        if docs_i:
            per_source_docs[p["path"]] = docs_i
            src_of[p["path"]] = p
    sources = list(per_source_docs)

    # Отбор «по кругу»: сначала по одному документу с каждого источника, потом по
    # второму и так далее. Так набор не оказывается одной подборкой, а число
    # документов из источника ограничено `--max-per-source` — иначе 200 документов
    # могли бы прийти из пяти файлов и быть пятью коррелированными точками замера.
    chosen: list[dict] = []
    for k in range(args.max_per_source):
        for src in sources:
            if len(chosen) >= args.docs:
                break
            docs_i = per_source_docs[src]
            if k >= len(docs_i):
                continue
            chosen.append({"doc": docs_i[k], "path": src, "cyr": src_of[src]["cyr"],
                           "text_sha256": src_of[src]["text_sha256"]})
        if len(chosen) >= args.docs:
            break

    # Перемешивание документов в файле: порядок не должен нести смысла
    # (соседние документы одного источника — коррелированные точки замера).
    final = chosen[:args.docs]
    rng.shuffle(final)
    docs = [c["doc"] for c in final]

    report = {
        "tool": "build_domain_eval_v3.py",
        "seed": args.seed,
        "docs_requested": args.docs,
        "docs_built": len(docs),
        "sources_used": len({c["path"] for c in final}),
        "max_per_source": args.max_per_source,
        "doc_target": args.doc_target, "doc_max": args.doc_max,
        "filters": {"min_chars": args.min_chars, "min_cyr": args.min_cyr,
                    "min_ml": args.min_ml},
        "pool_stats": stats,
        "length": length_stats(docs),
        "source_files": sorted({c["path"] for c in final}),
        "documents": [{"path": c["path"], "chars": len(c["doc"]),
                       "sha256_12": hashlib.sha256(c["doc"].encode()).hexdigest()[:12],
                       "head": c["doc"][:90].replace("\n", " ")} for c in final],
    }
    return {"docs": docs, "chosen": final, "report": report}


def verify(path: Path) -> int:
    """Проверка набора: формат и — главное — что прибор прочитает ровно записанное.

    Проверять «нет ли разделителя внутри документа» бессмысленно: разбор режет файл
    по разделителю, и в документе его по построению не остаётся. Настоящий дефект
    другой и он молчаливый: **документ, который прибор выбросит**. Прибор режет по
    ``\\n---\\n`` и оставляет куски длиннее 50 символов — значит кусок короче порога
    исчезнет из замера, и «200 документов» окажутся 199 без единого слова об этом.
    Поэтому проверка считает куски, не дожившие до замера, и называет их поимённо.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    parts = [d.strip() for d in text.split(DOC_SEP)]
    dropped = [(i, len(d), d[:60].replace("\n", " "))
               for i, d in enumerate(parts) if len(d) <= PPLEVAL_MIN_CHARS]
    docs = parse_eval_file(path)
    problems = []
    if not docs:
        problems.append("документов нет")
    if not raw.endswith(b"\n"):
        problems.append("файл не заканчивается переводом строки")
    for i, n, head in dropped:
        problems.append(f"кусок #{i} ({n} симв.) короче порога прибора и будет выброшен: «{head}…»")
    print(f"{path}: документов {len(docs)}, кусков {len(parts)}, "
          f"символов {sum(len(d) for d in docs)}, "
          f"байт {len(raw)}, sha256 {hashlib.sha256(raw).hexdigest()}")
    print(f"  длины: {length_stats(docs)}")
    if problems:
        for p in problems:
            print(f"  НАРУШЕНИЕ: {p}", file=sys.stderr)
        return EXIT_FAIL
    print("  формат: прибор прочитает все документы (ни один не короче порога)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append", default=[],
                    help=f"корень поиска (повторяемый); по умолчанию {DEFAULT_ROOTS}")
    ap.add_argument("--docs", type=int, default=200, help="сколько документов собрать")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--jobs", type=int, default=8, help="процессов извлечения текста")
    ap.add_argument("--ext", default="docx,md,txt",
                    help="какие расширения читать (docx,md,txt,pdf); PDF — отдельным "
                         "решением: их в архиве 21 867, и почти все английские")
    ap.add_argument("--min-chars", type=int, default=800, help="минимум текста в файле-источнике")
    ap.add_argument("--min-cyr", type=float, default=0.30, help="минимум доли кириллицы")
    ap.add_argument("--min-ml", type=int, default=30, help="минимум ML-вхождений в файле")
    ap.add_argument("--doc-target", type=int, default=2600, help="целевой размер документа")
    ap.add_argument("--doc-max", type=int, default=4200, help="потолок размера документа")
    ap.add_argument("--max-per-source", type=int, default=4,
                    help="сколько документов максимум берётся из одного файла-источника")
    ap.add_argument("--out", help="куда записать набор (на gb10-shared)")
    ap.add_argument("--card", help="куда записать карточку сборки (JSON)")
    ap.add_argument("--pool-out", help="куда записать пул кандидатов (JSONL, без текста)")
    ap.add_argument("--verify", metavar="FILE", help="проверить формат готового набора")
    ap.add_argument("--plan", action="store_true", help="что и как будет сделано")
    ap.add_argument("--dry-run", action="store_true", help="собрать без записи")
    a = ap.parse_args(argv)

    if a.verify:
        return verify(Path(a.verify))
    if not a.root:
        a.root = DEFAULT_ROOTS
    if a.plan:
        print("S3ao домен-набор v3 — план:")
        for r in a.root:
            print(f"  корень:            {r}")
        print(f"  фильтры:           ≥{a.min_chars} симв., кириллица ≥{a.min_cyr:.0%}, "
              f"ML-вхождений ≥{a.min_ml}")
        print(f"  документ:          сшивка до {a.doc_target} симв., потолок {a.doc_max}")
        print(f"  отбор:             сид {a.seed}, ≤{a.max_per_source} докум./источник, "
              f"цель {a.docs}")
        print(f"  выход:             {a.out or '(не задан)'}")
        return EXIT_OK

    built = build(a)
    if built.get("status") == "blocked":
        print(f"NOT-VERIFIED: {built['reason']}; статистика: "
              f"{json.dumps(built['stats'], ensure_ascii=False)}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    docs, final, report = built["docs"], built["chosen"], built["report"]
    print(f"пул: {report['pool_stats']['pool']} файлов-кандидатов из "
          f"{report['pool_stats']['files_seen']} просмотренных")
    print(f"отсев: {json.dumps(report['pool_stats']['excluded'], ensure_ascii=False)}")
    print(f"собрано документов: {len(docs)} из {report['sources_used']} источников; "
          f"длины: {report['length']}")
    if len(docs) < a.docs:
        print(f"ОТКАЗ: собрано {len(docs)} < заказанных {a.docs}", file=sys.stderr)
        if not a.dry_run and a.out:
            pass
        return EXIT_FAIL
    if a.dry_run:
        print("dry-run: файлы не записаны")
        return EXIT_OK

    text = render_docs(docs)
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        report["output"] = {"path": str(out), "bytes": len(text.encode()),
                            "sha256": sha256_file(out)}
        print(f"набор: {out} ({report['output']['bytes']} байт, "
              f"sha256 {report['output']['sha256']})")
    if a.pool_out:
        pp = Path(a.pool_out)
        pp.parent.mkdir(parents=True, exist_ok=True)
        with pp.open("w", encoding="utf-8") as fh:
            for c in final:
                fh.write(json.dumps({"path": c["path"], "chars": len(c["doc"]),
                                     "cyr": c["cyr"], "source_sha256": c["text_sha256"],
                                     "sha256": hashlib.sha256(c["doc"].encode()).hexdigest(),
                                     "head": c["doc"][:120].replace("\n", " ")},
                                    ensure_ascii=False) + "\n")
        report["pool_out"] = str(pp)
    if a.card:
        cp = Path(a.card)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"карточка: {cp}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
