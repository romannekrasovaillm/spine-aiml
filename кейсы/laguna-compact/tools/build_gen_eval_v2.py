#!/usr/bin/env python3
"""build_gen_eval_v2.py — сборка наборов GEN-EVAL v2 (ADR-018).

Цель ADR-018: заменить шумный прибор (24 и 5 документов) на измеримый, **не испортив
идущий пилот**: старые ``general_eval.txt`` / ``domain_eval.txt`` не трогаются, новые
файлы пишутся под именами ``*_v2.txt``.

Ключевая часть — **фильтр пересечения с обучением** (ADR-018 п.3). Наивная реализация
(«сравнить кандидата с текстом обучающего корпуса») на этих данных вырождается: кандидат,
взятый из корпуса, совпадает с ним по построению. Поэтому обучающее множество
восстанавливается по фактическим входам пилота, а не по имени файла-источника:

* **общий язык** — реплей-часть CPT-микса. ``build_cpt_v12r_mix.py`` берёт
  ``general_replay_ru.txt``, режет на документы по ``\\n\\n``, токенизирует подряд и
  **оставляет первые** ``--replay-chunks`` чанков по 8192 токена
  (``rep_chunks[:2444]``) — то есть обучен ровно **префикс** потока документов в
  файловом порядке. Кандидат исключается, если его токен-спан пересекает префикс
  (позиционное правило, ловит и документ на границе) **или** его нормализованный текст
  совпал с текстом обучающего материала (правило ADR-018 п.3, литерально);
* **домен** — CPT-микс собран из ``cpt_corpus_v10.1.txt`` **целиком**
  (``pretok_v101.log``: 168 528 документов → 7332 чанка × 8192 = все токены).
  Следствие: доменный набор из этого файла получить нельзя — фильтр отсекает 100 %
  кандидатов. Источник доменных кандидатов — ``cpt_corpus_full.txt`` (полный дамп
  карточек того же семейства), а сам v10.1 остаётся корпусом-целью фильтра; отсев
  по v10.1 измеряется и печатается отдельно (``--domain-alias``).

Дополнительно к точному совпадению считается **шингл-сигнал** (n-граммы по словам,
по умолчанию 12 — та же идея, что INFO-сигнал в ``tools/check_eval_leakage.py``):
он ловит near-duplicate, который точное сравнение пропускает, и превращает «остаточная
утечка не оценена» в число.

Формат выхода — как у текущих наборов и как ждёт ``laguna_pipeline_v8._ppl_eval``:
документы через ``\\n---\\n``, файл заканчивается переводом строки, каждый документ
длиннее 50 символов. Внутри документа последовательности ``\\n---\\n`` быть не может —
такие кандидаты отбрасываются (иначе при чтении документ распался бы на части, и
отфильтрованное множество перестало бы совпадать с записанным).

Коды возврата::

    0 — наборы собраны (или проверка пройдена)
    1 — нарушение: пул меньше требуемого объёма; файлы пилота изменились
    2 — NOT-VERIFIED: нет входа (корпус, токенизатор) — не зелёный

Запуск::

    python3 tools/build_gen_eval_v2.py                  # сборка (CPU-only, ~10 мин)
    python3 tools/build_gen_eval_v2.py --dry-run --json # замер без записи
    python3 tools/build_gen_eval_v2.py --verify datasets/general_eval_v2.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from array import array
from collections.abc import Iterator
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Разделитель документов: ровно то, чем режет ``_ppl_eval`` (``split("\n---\n")``).
DOC_SEP = "\n---\n"

#: Минимальная длина документа в ``_ppl_eval``: ``len(d.strip()) > 50``.
PPLEVAL_MIN_CHARS = 50

#: Обрезка документа в ``_ppl_eval``.
PPLEVAL_MAX_TOKENS = 1024

#: Спецтокены корпуса (единое токен-пространство, ``pretokenize_v9.py``). На кодирование
#: обычного текста не влияют — нужны только для точного воспроизведения словаря.
SPECIAL_TOKENS = ["<think>", "</think>", "<tool_call>", "</tool_call>",
                  "<tool_response>", "</tool_response>", "<reasoning>", "</reasoning>"]

#: Токенизатор ревизии (ADR-002): им же считает ``_ppl_eval`` на стенде.
TOKENIZER_REPO = "models--Qwen--Qwen2.5-0.5B"

_WS = re.compile(r"\s+")

#: Секции концепт-карт: доменный eval-документ — цельная секция карточки.
SECTION_PREFIX = "## "


def norm(text: str | None) -> str:
    """Нормализация для фильтра — та же, что в ``tools/check_eval_leakage.py``."""
    return _WS.sub(" ", (text or "").lower()).strip()


def digest(text: str) -> bytes:
    """16-байтовый отпечаток нормализованного текста (множества хранят отпечатки, не текст)."""
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


def short_sha1(text: str) -> str:
    """Короткий sha1 нормализованного текста — для поимённого перечисления в карточке."""
    return hashlib.sha1(norm(text).encode("utf-8")).hexdigest()[:12]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def corpus_sha256(path: Path, args) -> str | None:
    """sha256 корпуса для карточки; ``--skip-source-hashes`` отключает (1.7 ГБ чтения)."""
    return None if getattr(args, "skip_source_hashes", False) else sha256_file(path)


# ── разбор текстовых корпусов ────────────────────────────────────────────────

def iter_blocks(path: Path, *, blank_line: bool) -> Iterator[str]:
    """Потоковый разбор корпуса на блоки (документы).

    ``blank_line=False`` — разделитель строка ``---`` (так режет корпус CPT
    ``pretokenize_v9.py``: ``text.split("\\n---\\n")``);
    ``blank_line=True`` — разделитель пустая строка (так режет реплей
    ``build_cpt_v12r_mix.py``: ``text.split("\\n\\n")``).

    Поток вместо ``read_text().split()``: ``cpt_corpus_full.txt`` — 771 МБ, а лимит
    памяти на процесс — 5 ГБ (ADR-018 п.6). Блоки отдаются в том же порядке, что при
    разборе целиком; пустые блоки не отдаются — их всё равно отсекает фильтр длины.
    """
    buf: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            raw = line[:-1] if line.endswith("\n") else line
            # условия — точное равенство: «пустая строка» = последовательность "\n\n",
            # строка "---" = последовательность "\n---\n" (пробельные строки не разделяют)
            if (blank_line and raw == "") or (not blank_line and raw == "---"):
                block = "".join(buf).strip()
                buf = []
                if block:
                    yield block
                continue
            buf.append(line)
    block = "".join(buf).strip()
    if block:
        yield block


def parse_eval_file(path: Path) -> list[str]:
    """Разбор eval-набора ровно так, как это делает ``_ppl_eval``."""
    return [d.strip() for d in Path(path).read_text(encoding="utf-8", errors="replace").split(DOC_SEP)
            if len(d.strip()) > PPLEVAL_MIN_CHARS]


def render_docs(docs: list[str]) -> str:
    """Сборка файла набора: документы через ``\\n---\\n``, в конце перевод строки."""
    for d in docs:
        if DOC_SEP in d:
            raise ValueError("документ содержит разделитель набора — формат не round-trip")
    return DOC_SEP.join(docs) + "\n"


#: Строка front matter ``slug: <...>`` — идентичность концепт-карты.
_FRONT_SLUG = re.compile(r"^slug:\s*(\S+)")

#: Любая строка ``slug:`` в блоке — для обучающего корпуса (там и упоминания считаются).
_ANY_SLUG = re.compile(r"^slug:\s*(\S+)", re.M)


def header_slug(block: str) -> str | None:
    """Slug из заголовка ``# Концепт: X`` — им начинается карточка."""
    m = re.match(r"^# Концепт:\s*(\S+)", block)
    return m.group(1) if m else None


def front_slug(block: str) -> str | None:
    """Slug карточки из front matter — только в начале блока (первые две строки).

    Сканировать блок целиком нельзя: в корпусе есть блоки-траектории, где ``slug:``
    встречается как *упоминание* чужого концепта, а не как идентичность. Замер:
    10 888 таких блоков; накопление упоминаний в идентичность даёт квадратичный рост
    памяти (пик 6.4 ГБ против лимита 5 ГБ, ADR-018 п.6). Front matter же всегда стоит
    либо первой строкой блока, либо сразу под заголовком ``# Концепт:``.
    """
    for line in block.split("\n", 2)[:2]:
        m = _FRONT_SLUG.match(line)
        if m:
            return m.group(1)
    return None


def all_slugs(block: str) -> list[str]:
    """Все строки ``slug:`` блока — идентичности обучающего корпуса.

    Для обучающего множества лишняя идентичность только сужает пул кандидатов:
    если концепт упомянут в траектории обучения, его текст модель видела.
    """
    return _ANY_SLUG.findall(block)


def unit_identity(blocks: Iterator[str]) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Блоки корпуса вместе с идентичностями карточки, которой блок принадлежит.

    Карточка = ``# Концепт: X`` → front matter (``slug: x``) → секции ``## ...``.
    Идентичность нужна, чтобы отсекать кандидата, чья карточка уже в обучающем
    корпусе: точное сравнение текста такую карточку пропустит, если её переписали
    на одно слово (шаблон и смысл те же — запоминание осталось).

    Идентичностей у карточки не больше двух (заголовок и front matter), и блок несёт
    их кортеж: идентичность наследуется секциями, но не растёт от блока к блоку.
    """
    ids: tuple[str, ...] = ()
    for block in blocks:
        h = header_slug(block)
        if h is not None:  # заголовок начинает новую карточку
            ids = (h,)
        f = front_slug(block)
        if f is not None:
            ids = tuple(sorted({*ids, f}))
        yield block, ids


def iter_jsonl_texts(path: Path) -> Iterator[str]:
    """Тексты примеров jsonl (SFT/RL): сообщения по отдельности либо плоские поля.

    Поля — как в ``check_eval_leakage.train_texts``: фильтр обязан смотреть на те же
    тексты, что и страж AD-7, иначе «утечек нет» в одном отчёте и есть в другом.
    """
    fields = ("prompt", "input", "question", "text", "response", "completion")
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ex = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ex, dict):
                continue
            msgs = ex.get("messages")
            if isinstance(msgs, list) and msgs:
                for m in msgs:
                    if isinstance(m, dict) and m.get("content"):
                        yield str(m["content"])
                continue
            for fld in fields:
                if ex.get(fld):
                    yield str(ex[fld])


# ── шингл-индекс (near-duplicate) ────────────────────────────────────────────

class ShingleIndex:
    """Множество n-грамм по словам с точной проверкой «есть ли такая n-грамма».

    n-грамма хешируется скользящим полиномом по хешам слов, поэтому индекс — массив
    uint64 (12 млн шинглов ≈ 96 МБ), а не множество строк: строковое множество на
    20 млн токенов съело бы больше лимита памяти (ADR-018 п.6).
    """

    _B = 0x100000001B3
    _MASK = (1 << 64) - 1

    def __init__(self, n: int) -> None:
        self.n = n
        self._hashes = array("Q")
        self._sorted = None

    @staticmethod
    def _word_hashes(text: str) -> list[int]:
        return [int.from_bytes(hashlib.blake2b(w.encode("utf-8"), digest_size=8).digest(),
                               "little") for w in norm(text).split()]

    def _ngrams(self, text: str) -> list[int]:
        n = self.n
        wh = self._word_hashes(text)
        if len(wh) < n:
            return []
        bp = pow(self._B, n - 1, 1 << 64)
        out, h = [], 0
        for i, w in enumerate(wh):
            h = (h * self._B + w) & self._MASK
            if i >= n - 1:
                out.append(h)
                h = (h - wh[i - n + 1] * bp) & self._MASK
        return out

    def add_text(self, text: str) -> None:
        self._hashes.extend(self._ngrams(text))

    def finalize(self) -> None:
        import numpy as np
        self._sorted = np.sort(np.frombuffer(self._hashes, dtype="<u8"))

    def shared(self, text: str) -> tuple[int, int]:
        """Сколько n-грамм текста есть в индексе и сколько их всего."""
        import numpy as np
        hs = self._ngrams(text)
        if not hs:
            return 0, 0
        if self._sorted is None:
            self.finalize()
        if len(self._sorted) == 0:
            return 0, len(hs)
        arr = np.array(hs, dtype="<u8")
        pos = np.clip(np.searchsorted(self._sorted, arr), 0, max(0, len(self._sorted) - 1))
        return int((self._sorted[pos] == arr).sum()), len(hs)

    def count(self) -> int:
        return len(self._hashes)


def training_shingles(path: Path, n: int) -> ShingleIndex:
    """Индекс n-грамм по обучающему корпусу — для оценки остаточного пересечения.

    Гейтом не является (шаблонные обороты карточек дают общие n-граммы независимо
    от утечки): число нужно, чтобы «остаточная утечка не оценена» стало величиной.
    """
    ix = ShingleIndex(n)
    for block in iter_blocks(path, blank_line=False):
        ix.add_text(block)
    ix.finalize()
    return ix


def summarize_overlap(ix: ShingleIndex, docs: list[str], n: int) -> dict:
    """Сводка «сколько оборотов документов набора уже есть в обучающем корпусе»."""
    fracs, shared_docs, max_frac = [], 0, 0.0
    for d in docs:
        sh, total = ix.shared(d)
        frac = (sh / total) if total else 0.0
        fracs.append(frac)
        if sh:
            shared_docs += 1
        max_frac = max(max_frac, frac)
    fracs.sort()
    q = lambda p: round(fracs[min(len(fracs) - 1, int(p * len(fracs)))], 4) if fracs else 0.0
    return {"ngram": n, "index_shingles": ix.count(), "documents": len(docs),
            "documents_with_shared": shared_docs, "median_frac": q(0.5), "p90_frac": q(0.9),
            "max_frac": round(max_frac, 4),
            "caveat": ("нижняя граница пересечения по оборотам; включает общий шаблон "
                       "(карточки) и формульные обороты — не различает утечку и стиль, "
                       "гейтом не является")}


# ── токенизатор ──────────────────────────────────────────────────────────────

def find_tokenizer(spec: str) -> Path:
    """Найти tokenizer.json ревизии. ``spec`` — путь или ``auto`` (кэш HF)."""
    if spec != "auto":
        p = Path(spec)
        if not p.is_file():
            raise FileNotFoundError(f"tokenizer.json не найден: {p}")
        return p
    hf = Path.home() / ".cache" / "huggingface" / "hub" / TOKENIZER_REPO / "snapshots"
    found = sorted(hf.glob("*/tokenizer.json"))
    if not found:
        raise FileNotFoundError(
            f"tokenizer.json не найден в кэше HF ({hf}) — "
            "укажи --tokenizer PATH или выключи позиционную проверку (--replay-chunks 0)")
    return found[0]


def load_tokenizer(spec: str):
    from tokenizers import Tokenizer
    return Tokenizer.from_file(str(find_tokenizer(spec)))


# ── сборка наборов ───────────────────────────────────────────────────────────

def collect_training_hashes(shared: Path, args) -> tuple[dict[str, set[bytes]], dict, set[str]]:
    """Отпечатки текстов обучающего материала пилота (по источникам) и его идентичности.

    Обучающий материал — то, на чём учился именно этот чекпойнт: CPT-микс v12r
    (домен v10.1 + реплей), SFT-корпус, пул RL. База Qwen2.5-0.5B сюда не входит:
    её предобучение контуру неизвестно (это [gap], а не проверенный факт).

    Тексты блоков не удерживаются: нужны отпечатки и идентичности, а не текст
    (``cpt_corpus_v10.1.txt`` целиком — 213 млн символов).
    """
    sets: dict[str, set[bytes]] = {}
    stats: dict[str, dict] = {}
    trained_slugs: set[str] = set()
    for name, fname in (("domain_cpt", args.domain_alias), ("sft", args.sft), ("rl", args.rl)):
        path = shared / "datasets" / fname
        if not path.is_file():
            raise FileNotFoundError(f"обучающий корпус не найден: {path}")
        if name == "domain_cpt":
            hashes, n, n_chars = set(), 0, 0
            for block, ids in unit_identity(iter_blocks(path, blank_line=False)):
                hashes.add(digest(norm(block)))
                trained_slugs.update(all_slugs(block))
                n += 1
                n_chars += len(block)
            sets[name] = hashes
            stats[name] = {"file": str(path), "units": n, "chars": n_chars,
                           "unique_slugs": len(trained_slugs),
                           "adr_named_source": {
                               "file": str(path), "units": n, "excluded_exact_match": n,
                               "kept": 0,
                               "why": ("отсев 100 % тривиален: каждая единица этого файла и есть "
                                       "обучающее множество, то есть совпадает сама с собой "
                                       "(фильтр ADR-018 п.3). Набор из v10.1 не собирается")},
                           "sha256": corpus_sha256(path, args)}
        else:
            hashes, n = set(), 0
            for text in iter_jsonl_texts(path):
                hashes.add(digest(norm(text)))
                n += 1
            sets[name] = hashes
            stats[name] = {"file": str(path), "texts": n, "sha256": corpus_sha256(path, args)}
    return sets, stats, trained_slugs


def build_general(shared: Path, args, train_hashes: dict[str, set[bytes]],
                  tok) -> tuple[list[dict], dict]:
    """Кандидаты набора общего языка из реплей-корпуса + отсев.

    Исключение: (1) токен-спан документа пересекает обученный префикс реплея;
    (2) нормализованный текст совпал с обучающим материалом; (3) документ короче
    ``--min-chars``; (4) в тексте есть разделитель набора (формат не round-trip);
    (5) шингл-сигнал: доля общих 12-грамм с префиксом ≥ ``--ngram-frac-max``.
    """
    path = shared / "datasets" / args.general
    if not path.is_file():
        raise FileNotFoundError(f"реплей-корпус не найден: {path}")
    consumed_tokens = args.replay_chunks * args.chunk_tokens

    excluded = {"replay_token_prefix": 0, "exact_match_train": 0, "too_short": 0,
                "doc_separator_inside": 0, "near_duplicate_ngram": 0}
    by_source: dict[str, int] = {}
    examples: list[dict] = []
    pool: list[dict] = []
    consumed_texts: list[str] = []
    shingles = ShingleIndex(args.ngram) if args.ngram > 0 else None

    pos, consumed_docs, n_docs = 0, 0, 0
    for i, doc in enumerate(iter_blocks(path, blank_line=True)):
        if len(doc) < PPLEVAL_MIN_CHARS:
            continue
        n_docs += 1
        if tok is not None:
            span_start = pos
            pos += len(tok.encode(doc, add_special_tokens=False).ids)
        else:
            span_start = -1
        in_prefix = tok is not None and span_start < consumed_tokens

        if in_prefix:
            consumed_docs += 1
            if shingles is not None:
                consumed_texts.append(doc)
            excluded["replay_token_prefix"] += 1
            if len(examples) < 8:
                examples.append({"set": "general", "source_index": i,
                                 "reason": "replay_token_prefix",
                                 "span_tokens_start": span_start, "chars": len(doc),
                                 "normalized_sha1": short_sha1(doc), "head": doc[:80]})
            continue
        hit = next((name for name, hs in train_hashes.items() if digest(norm(doc)) in hs), None)
        if hit:
            excluded["exact_match_train"] += 1
            by_source[hit] = by_source.get(hit, 0) + 1
            if len(examples) < 8:
                examples.append({"set": "general", "source_index": i,
                                 "reason": f"exact_match_train:{hit}", "chars": len(doc),
                                 "normalized_sha1": short_sha1(doc), "head": doc[:80]})
            continue
        if DOC_SEP in doc:
            excluded["doc_separator_inside"] += 1
            continue
        if len(doc) < args.min_chars:
            excluded["too_short"] += 1
            continue
        pool.append({"source_index": i, "text": doc, "chars": len(doc)})

    near = {"ngram": args.ngram, "threshold_frac": args.ngram_frac_max,
            "index_shingles": 0, "docs_with_shared": 0, "docs_over_threshold": 0,
            "max_frac": 0.0, "median_frac": 0.0}
    if shingles is not None and pool:
        for t in consumed_texts:  # индекс строится только по обученному префиксу
            shingles.add_text(t)
        near["index_shingles"] = shingles.count()
        shingles.finalize()
        fracs = []
        for c in pool:
            sh, total = shingles.shared(c["text"])
            c["shared_ngrams"], c["ngram_total"] = sh, total
            frac = (sh / total) if total else 0.0
            c["shared_frac"] = round(frac, 4)
            fracs.append(frac)
            if sh:
                near["docs_with_shared"] += 1
        fracs.sort()
        near["max_frac"] = round(fracs[-1], 4)
        near["median_frac"] = round(fracs[len(fracs) // 2], 4)
        over = [c for c in pool if c["shared_frac"] >= args.ngram_frac_max]
        near["docs_over_threshold"] = len(over)
        excluded["near_duplicate_ngram"] = len(over)
        for c in over[:3]:
            examples.append({"set": "general", "source_index": c["source_index"],
                             "reason": "near_duplicate_ngram",
                             "shared_ngrams": c["shared_ngrams"],
                             "ngram_total": c["ngram_total"],
                             "shared_frac": c["shared_frac"],
                             "normalized_sha1": short_sha1(c["text"]),
                             "head": c["text"][:80]})
        pool = [c for c in pool if c["shared_frac"] < args.ngram_frac_max]
    if tok is None:
        excluded.pop("replay_token_prefix", None)

    stats = {
        "source": str(path),
        "source_sha256": corpus_sha256(path, args),
        "cut": f"документы реплей-корпуса (split '\\n\\n', ≥{PPLEVAL_MIN_CHARS} симв.) "
               f"в файловом порядке; обученный префикс = первые {args.replay_chunks} чанков "
               f"× {args.chunk_tokens} токенов = {consumed_tokens}",
        "docs_in_corpus": n_docs,
        "consumed_prefix_docs": consumed_docs,
        "consumed_prefix_tokens": consumed_tokens,
        "tokenizer": str(tok) if tok is not None else None,
        "candidates": n_docs,
        "excluded": excluded,
        "excluded_by_source": by_source,
        "examples": examples,
        "near_duplicate": near,
        "pool_after_filter": len(pool),
    }
    return pool, stats


def build_domain(shared: Path, args, train_hashes: dict[str, set[bytes]],
                 trained_slugs: set[str], alias: dict,
                 shingles: "ShingleIndex | None" = None) -> tuple[list[dict], dict]:
    """Кандидаты доменного набора из полного дампа концепт-карт + отсев.

    Исключение: (1) карточка кандидата уже в обучающем корпусе (по slug) — ловит
    переписанные карточки, которые точное сравнение текста пропускает;
    (2) точное совпадение нормализованного текста с обучающим материалом;
    (3) блок не является секцией карточки (``## ...``) или короче ``--min-chars``;
    (4) в тексте есть разделитель набора.
    Отдельно измеряется отсев по корпусу, названному в ADR-018 (``--domain-alias``).
    """
    path = shared / "datasets" / args.domain_source
    if not path.is_file():
        raise FileNotFoundError(f"доменный корпус не найден: {path}")

    excluded = {"card_identity_in_training": 0, "exact_match_train": 0,
                "not_section_unit": 0, "too_short": 0, "doc_separator_inside": 0}
    by_source: dict[str, int] = {}
    examples: list[dict] = []
    pool: list[dict] = []
    n_cards, n_blocks, n_sections = 0, 0, 0

    for i, (block, ids) in enumerate(unit_identity(iter_blocks(path, blank_line=False))):
        n_blocks += 1
        if block.startswith("# Концепт:"):
            n_cards += 1
        if not block.startswith(SECTION_PREFIX):
            excluded["not_section_unit"] += 1
            continue
        n_sections += 1
        if set(ids) & trained_slugs:
            excluded["card_identity_in_training"] += 1
            if len(examples) < 8:
                examples.append({"set": "domain", "source_index": i,
                                 "reason": "card_identity_in_training",
                                 "slug": ids[0] if ids else None,
                                 "slug_matched": sorted(set(ids) & trained_slugs)[0],
                                 "chars": len(block), "normalized_sha1": short_sha1(block),
                                 "head": block[:80]})
            continue
        hit = next((name for name, hs in train_hashes.items() if digest(norm(block)) in hs), None)
        if hit:
            excluded["exact_match_train"] += 1
            by_source[hit] = by_source.get(hit, 0) + 1
            continue
        if DOC_SEP in block:
            excluded["doc_separator_inside"] += 1
            continue
        if len(block) < args.min_chars:
            excluded["too_short"] += 1
            continue
        pool.append({"source_index": i, "text": block, "chars": len(block),
                     "slug": ids[0] if ids else None})

    stats = {
        "source": str(path),
        "source_sha256": corpus_sha256(path, args),
        "adr_named_source": alias,
        "cut": f"секции концепт-карт ('## ...', ≥{args.min_chars} симв.) из полного дампа; "
               f"карточка кандидата обязана отсутствовать в обучающем корпусе (по slug)",
        "cards": n_cards, "blocks": n_blocks, "section_units": n_sections,
        "candidates": n_sections,
        "excluded": excluded,
        "excluded_by_source": by_source,
        "examples": examples,
        "pool_after_filter": len(pool),
        "shingles": shingles,
    }
    return pool, stats


def sample_pool(pool: list[dict], n: int, seed: int, what: str) -> list[dict]:
    """Случайная выборка с фиксированным сидом; порядок в файле — исходный.

    Сид фиксирован (ADR-018 п.4), поток ``random.Random(seed)`` — поэтому повторный
    запуск на тех же данных даёт те же документы (идемпотентность).
    """
    if len(pool) < n:
        raise SystemExit(
            f"FAIL: {what}: пул после фильтра {len(pool)} < требуемых {n} "
            f"(ADR-018 п.2 — не менее {n})")
    idx = sorted(random.Random(seed).sample(range(len(pool)), n))
    return [pool[i] for i in idx]


def length_stats(docs: list[dict]) -> dict:
    ls = sorted(d["chars"] for d in docs)
    return {"min": ls[0], "median": ls[len(ls) // 2], "max": ls[-1],
            "mean": round(sum(ls) / len(ls), 1), "chars_total": sum(ls)}


def verify_eval_file(path: Path, tok, max_len: int = PPLEVAL_MAX_TOKENS) -> dict:
    """Разбор набора так, как это делает ``_ppl_eval`` (без модели — только счёт)."""
    if not path.is_file():
        raise FileNotFoundError(f"файл набора не найден: {path}")
    docs = parse_eval_file(path)
    res = {"file": str(path), "documents": len(docs),
           "chars_total": sum(len(d) for d in docs),
           "ppl_eval_min_chars": PPLEVAL_MIN_CHARS, "max_len": max_len,
           "tokenizer": str(tok) if tok is not None else None}
    if tok is None:
        return res
    n_trunc, n_short, n_tok = 0, 0, 0
    for d in docs:
        enc = tok.encode(d, add_special_tokens=False).ids[:max_len]
        if len(enc) > 8:
            n_tok += len(enc)
        else:
            n_short += 1
        if len(enc) >= max_len:
            n_trunc += 1
    res.update({"documents_used_by_ppl_eval": len(docs) - n_short,
                "documents_dropped_fewer_8_tokens": n_short,
                "documents_truncated": n_trunc, "tokens_total": n_tok})
    return res


def write_atomic(path: Path, data: str) -> str:
    """Записать файл целиком (новое имя ``*_v2.txt``) и вернуть sha256 записанного."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)
    return sha256_file(path)


def limits(report: dict) -> list[str]:
    """Раздел ``limits`` карточки: что фильтр НЕ ловит и что осталось непроверенным."""
    gen = report["sets"].get("general", {})
    return [
        "Фильтр ловит только дословные совпадения нормализованного текста (lower + "
        "схлопнутые пробелы) и совпадение карточки по slug. **Пересказы, сжатия и "
        "частичные перефразировки не ловятся** — остаточная утечка по этому каналу "
        "не оценена; шингл-сигнал (12-граммы) даёт только нижнюю оценку по общим оборотам.",
        "Реплей-часть CPT обучена не целиком: обучен префикс потока документов "
        f"({gen.get('consumed_prefix', {}).get('tokens', '—')} токенов, "
        f"{gen.get('consumed_prefix', {}).get('docs', '—')} документов). Документы за "
        "префиксом в обучении не были, но соседние по файлу (та же статья Википедии) — "
        "остаточный риск; проверка на уровне статьи не делалась.",
        "Домен: источник кандидатов — cpt_corpus_full.txt (полный дамп карточек), а не "
        "cpt_corpus_v10.1.txt, названный в ADR-018 п.2: из v10.1 набор получить нельзя — "
        "фильтр п.3 отсекает 100 % его единиц (числа в sets.domain.adr_named_source). "
        "Это расхождение с буквой ADR, а не с целью п.3 (тексты вне обучающего множества).",
        "Домен: карточки того же семейства и шаблона, что обучающие (Ariadna, те же секции). "
        "PPL по ним измеряет обобщение внутри домена, но НЕ «домен вне распределения»: "
        "общие с обучением шаблонные обороты снижают PPL независимо от забывания.",
        "Шингл-сигнал (12-граммы) гейтом считается только для общего набора; для домена он "
        "не гейт: общий шаблон карточек даёт высокую долю общих n-грамм независимо от "
        "утечки. Остаточное пересечение обоих наборов измерено и записано в "
        "sets.*.residual_overlap — как нижняя граница, включающая шаблон и формульные "
        "обороты, а не как доля утечки.",
        "Наборы **не откалиброваны**: пороги ADR-015 (внимание 1.5×, эскалация 2×) выведены "
        "для v1; после перехода на v2 их надо пересматривать по первым замерам.",
        "PPL на v2 не измерялась: сборка CPU-only, без модели (ADR-018 п.6). Ожидаемое "
        "снижение разброса — оценка по числу документов, а не замер.",
        "Определение обучающего материала взято из фактических входов пилота (CPT-кэш v12r "
        "= домен v10.1 + реплей, sft_train_v12, пул RL). Предобучение базовой модели "
        "Qwen2.5-0.5B контуру неизвестно и в фильтр не входит: если википедийный текст был "
        "в её предобучении, абсолютная PPL по нему занижена. На дельту «vs base» это влияет "
        "слабее, чем на абсолют: сравнение идёт с базовой моделью на том же наборе.",
        "Восстановление обученного префикса реплея опирается на повторную токенизацию "
        "Qwen2.5 локально (CPU). Совпадение с логом стенда проверено на доменном корпусе "
        "(7332 чанка × 8192 — как в pretok_v101.log), но окружение сборки иное.",
        "Стоимость вызова GEN-EVAL растёт вместе с набором (раздел eval_cost): больше "
        "токенов на вызов = больше времени стадии при той же частоте "
        "(`GEN_EVAL_EVERY`). Частоту стоит пересмотреть при переходе (ADR-018 п.5) — "
        "иначе выигрыш в точности прибора съест время прогона.",
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Сборка наборов GEN-EVAL v2 (ADR-018): ≥200 документов на набор, "
                    "фильтр пересечения с обучающим материалом пилота, старые файлы целы.")
    ap.add_argument("--shared", default="/home/user/gb10-shared")
    ap.add_argument("--out-dir", default=None, help="по умолчанию <shared>/datasets")
    ap.add_argument("--card", default="data/gen-eval-v2-card.json")
    ap.add_argument("--general", default="general_replay_ru.txt", help="реплей-корпус (общий язык)")
    ap.add_argument("--domain-source", default="cpt_corpus_full.txt",
                    help="источник доменных кандидатов")
    ap.add_argument("--domain-alias", default="cpt_corpus_v10.1.txt",
                    help="доменный корпус, названный в ADR-018 (корпус-цель фильтра)")
    ap.add_argument("--sft", default="sft_train_v12.jsonl")
    ap.add_argument("--rl", default="rl_tasks_revpool_v1.jsonl")
    ap.add_argument("--pilot-general", default="general_eval.txt",
                    help="существующий набор пилота (не должен измениться)")
    ap.add_argument("--pilot-domain", default="domain_eval.txt",
                    help="существующий набор пилота (не должен измениться)")
    ap.add_argument("--general-out", default="general_eval_v2.txt")
    ap.add_argument("--domain-out", default="domain_eval_v2.txt")
    ap.add_argument("--docs", type=int, default=256, help="документов в каждом наборе")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-chars", type=int, default=200, help="минимальная длина кандидата")
    ap.add_argument("--replay-chunks", type=int, default=2444,
                    help="чанков реплея в CPT-миксе (build_cpt_v12r_mix.py: 2444)")
    ap.add_argument("--chunk-tokens", type=int, default=8192, help="длина чанка микса")
    ap.add_argument("--max-len", type=int, default=PPLEVAL_MAX_TOKENS)
    ap.add_argument("--ngram", type=int, default=12, help="длина шингла (0 — выключить)")
    ap.add_argument("--ngram-frac-max", type=float, default=0.5,
                    help="доля общих n-грамм, при которой документ исключается")
    ap.add_argument("--skip-source-hashes", action="store_true",
                    help="не считать sha256 корпусов (дорого на 1.7 ГБ)")
    ap.add_argument("--tokenizer", default="auto", help="путь к tokenizer.json или auto")
    ap.add_argument("--dry-run", action="store_true", help="не писать файлы и карточку")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    ap.add_argument("--verify", nargs="+", metavar="FILE",
                    help="режим проверки: разобрать набор как _ppl_eval и напечатать счётчики")
    args = ap.parse_args(argv)
    if args.out_dir is None:
        args.out_dir = str(Path(args.shared) / "datasets")

    if args.verify:
        try:
            tok = load_tokenizer(args.tokenizer)
        except Exception as e:  # noqa: BLE001 — причина важнее типа исключения
            print(f"NOT-VERIFIED: токенизатор недоступен ({type(e).__name__}: {e})", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        out = []
        for f in args.verify:
            try:
                out.append(verify_eval_file(Path(f), tok, args.max_len))
            except FileNotFoundError as e:
                print(f"NOT-VERIFIED: {e}", file=sys.stderr)
                return EXIT_NOT_VERIFIED
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for r in out:
                print(f"== {r['file']} ==")
                print(f"  документов (split '\\n---\\n', len>{r['ppl_eval_min_chars']}): {r['documents']}")
                print(f"  из них берёт _ppl_eval: {r.get('documents_used_by_ppl_eval', '—')}"
                      f"  отброшено (<8 токенов): {r.get('documents_dropped_fewer_8_tokens', '—')}")
                print(f"  обрезано до {r['max_len']} токенов: {r.get('documents_truncated', '—')}")
                print(f"  токенов всего: {r.get('tokens_total', '—')}  символов: {r['chars_total']}")
        return EXIT_OK

    try:
        report = build(args)
    except FileNotFoundError as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    except SystemExit as e:  # пул меньше объёма / защита имён / файлы пилота изменились
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            return EXIT_FAIL
        return e.code if isinstance(e.code, int) else EXIT_FAIL

    report["limits"] = limits(report)
    card_path = Path(args.card)
    if not args.dry_run:
        card_path.parent.mkdir(parents=True, exist_ok=True)
        card_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("== GEN-EVAL v2 (ADR-018) ==")
        for key, s in report["sets"].items():
            print(f"{key}: кандидатов {s['candidates']}, пул после фильтра {s['pool_after_filter']}, "
                  f"в наборе {s['documents']}, {s['bytes']} Б, sha256 {s['sha256'][:16]}…")
            print(f"  отсев: {s['excluded']}")
            if s.get("adr_named_source"):
                a = s["adr_named_source"]
                print(f"  v10.1 (назван в ADR): единиц {a['units']}, отсеяно точным "
                      f"совпадением {a['excluded_exact_match']}, прошло {a['kept']}")
            if s.get("ppl_eval"):
                v = s["ppl_eval"]
                print(f"  _ppl_eval: документов {v['documents']}, берёт "
                      f"{v.get('documents_used_by_ppl_eval')}, токенов {v.get('tokens_total')}")
        print(f"файлы пилота: {report['pilot_files_after']}")
        if not args.dry_run:
            print(f"карточка: {card_path}")
    return EXIT_OK


def build(args) -> dict:
    """Полный прогон сборки: обучающий материал → пулы → выборка → файлы → карточка."""
    shared = Path(args.shared)
    report: dict = {"card": "gen-eval-v2", "generator": "tools/build_gen_eval_v2.py",
                    "adr": "ADR-018", "seed": args.seed, "target_docs": args.docs,
                    "dry_run": bool(args.dry_run), "not_verified": [], "sets": {}}

    # (0) файлы пилота — до сборки: критерий приёмки — они не должны измениться
    pilot_files = {}
    for name in (args.pilot_general, args.pilot_domain):
        p = shared / "datasets" / name
        if not p.is_file():
            raise FileNotFoundError(f"файл пилота не найден: {p}")
        pilot_files[name] = {"sha256": sha256_file(p), "mtime": p.stat().st_mtime,
                             "bytes": p.stat().st_size}
    report["pilot_files_before"] = pilot_files

    # (1) обучающий материал пилота
    train_hashes, train_stats, trained_slugs = collect_training_hashes(shared, args)
    report["training_material"] = train_stats

    # (2) токенизатор: без него позиционное правило общего набора не работает
    tok = None
    if args.replay_chunks > 0:
        try:
            tok = load_tokenizer(args.tokenizer)
        except Exception as e:  # noqa: BLE001
            print(f"NOT-VERIFIED: токенизатор недоступен ({type(e).__name__}: {e})", file=sys.stderr)
            raise SystemExit(EXIT_NOT_VERIFIED) from e
    else:
        report["not_verified"].append(
            "позиционная проверка префикса реплея выключена (--replay-chunks 0): "
            "кандидаты проверены только по точному совпадению текста")

    # (3) пулы кандидатов. Для домена заодно считается индекс n-грамм обучающего
    #     корпуса — им оценивается остаточное пересечение (не гейт, см. карточку).
    gen_pool, gen_stats = build_general(shared, args, train_hashes, tok)
    dom_shingles = training_shingles(shared / "datasets" / args.domain_alias, args.ngram) \
        if args.ngram > 0 else None
    dom_pool, dom_stats = build_domain(shared, args, train_hashes, trained_slugs,
                                       train_stats["domain_cpt"]["adr_named_source"],
                                       dom_shingles)

    # (4) выборка и запись. Выборка — до записи: иначе провал второго пула оставил бы
    #     в дереве первый файл (частичный артефакт хуже отсутствующего).
    picked_by_key = {key: sample_pool(pool, args.docs, args.seed, key)
                     for key, pool in (("general", gen_pool), ("domain", dom_pool))}
    for key, pool, stats, fname in (("general", gen_pool, gen_stats, args.general_out),
                                    ("domain", dom_pool, dom_stats, args.domain_out)):
        picked = picked_by_key[key]
        text = render_docs([c["text"] for c in picked])
        target = Path(args.out_dir) / fname
        entry = {"source": stats["source"], "source_sha256": stats["source_sha256"],
                 "cut": stats["cut"], "candidates": stats["candidates"],
                 "excluded": stats["excluded"],
                 "excluded_by_source": stats.get("excluded_by_source", {}),
                 "examples": stats["examples"], "pool_after_filter": len(pool),
                 "documents": len(picked), "length": length_stats(picked),
                 "source_indices": [c["source_index"] for c in picked],
                 "path": str(target), "bytes": len(text.encode("utf-8"))}
        if key == "general":
            entry["near_duplicate"] = stats["near_duplicate"]
            fr = sorted(c.get("shared_frac", 0.0) for c in picked)
            entry["residual_overlap"] = {
                "ngram": args.ngram, "documents": len(fr),
                "documents_with_shared": sum(1 for x in fr if x),
                "median_frac": fr[len(fr) // 2] if fr else 0.0,
                "p90_frac": fr[min(len(fr) - 1, int(0.9 * len(fr)))] if fr else 0.0,
                "max_frac": fr[-1] if fr else 0.0,
                "index_shingles": stats["near_duplicate"]["index_shingles"],
                "caveat": ("доля 12-грамм документа, встречающихся в обученном префиксе "
                           "реплея; включает формульные обороты — гейтом не является")}
            entry["consumed_prefix"] = {"docs": stats["consumed_prefix_docs"],
                                        "tokens": stats["consumed_prefix_tokens"],
                                        "docs_in_corpus": stats["docs_in_corpus"],
                                        "tokenizer": stats["tokenizer"]}
        else:
            entry["adr_named_source"] = stats["adr_named_source"]
            entry["cards_seen"] = stats["cards"]
            if stats.get("shingles") is not None:
                entry["residual_overlap"] = summarize_overlap(
                    stats["shingles"], [c["text"] for c in picked], args.ngram)
        if args.dry_run:
            entry["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            entry["written"] = False
        else:
            if not fname.endswith("_v2.txt"):
                raise SystemExit(f"FAIL: имя набора обязано оканчиваться на _v2.txt: {fname}")
            entry["sha256"] = write_atomic(target, text)
            entry["written"] = True
            back = parse_eval_file(target)
            entry["readback_documents"] = len(back)
            entry["readback_ok"] = back == [c["text"] for c in picked]
            if not entry["readback_ok"]:
                raise SystemExit("FAIL: прочитанный файл не совпал с отфильтрованным набором")
        report["sets"][key] = entry

    # (5) файлы пилота — после сборки
    changed = []
    for name, before in pilot_files.items():
        p = shared / "datasets" / name
        after = {"sha256": sha256_file(p), "mtime": p.stat().st_mtime,
                 "bytes": p.stat().st_size}
        if after != before:
            changed.append({"file": name, "before": before, "after": after})
    report["pilot_files_after"] = "unchanged" if not changed else "changed"
    report["pilot_files_changed"] = changed
    if changed:
        raise SystemExit(f"FAIL: файлы пилота изменены: {changed}")

    # (6) ожидаемое число документов на стороне пайплайна (формат-совместимость)
    if not args.dry_run:
        for key, fname in (("general", args.general_out), ("domain", args.domain_out)):
            report["sets"][key]["ppl_eval"] = verify_eval_file(
                Path(args.out_dir) / fname, tok, args.max_len)

        # (7) якорь стоимости вызова GEN-EVAL: v1 и v2 меряются одним токенизатором,
        #     чтобы у решения о переходе (ADR-018 п.5) были числа, а не «станет дороже»
        v1 = {}
        for key, name in (("general", args.pilot_general), ("domain", args.pilot_domain)):
            try:
                v1[key] = verify_eval_file(shared / "datasets" / name, tok, args.max_len)
            except FileNotFoundError:
                v1[key] = None
        if all(v1.values()):
            t1 = sum(v["tokens_total"] for v in v1.values())
            t2 = sum(report["sets"][k]["ppl_eval"]["tokens_total"] for k in ("general", "domain"))
            report["eval_cost"] = {
                "method": ("токены на один вызов _general_eval_step (оба набора), один и тот же "
                           "токенизатор Qwen2.5; время вызова = токены / пропускная способность"),
                "v1_tokens": t1, "v2_tokens": t2, "ratio": round(t2 / max(t1, 1), 2),
                "v1": v1, "call_freq_note": ("частота вызовов задаётся GEN_EVAL_EVERY шагов; "
                                             "на SFT пилота (67423 шага) это 1348 вызовов при 50"),
            }
    return report


if __name__ == "__main__":
    sys.exit(main())
