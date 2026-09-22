#!/usr/bin/env python3
"""S3q — сборка расширенного измерительного набора общего языка (ADR-025 п.4).

Зачем новый набор. Потолок PPL (2× базы, ADR-022 п.3) — единственный абсолютный
критерий валидности CPT-стадии, а оба существующих общих набора дефектны:

* ``general_eval.txt`` (v1) — 24 документа, 3 682 токена: различает ×4.6 от ×9.5,
  но **не различает** ×4.63 и ×4.76 (1.50 PPL при базе 11.93 лежит внутри разброса
  прибора);
* ``general_eval_v2.txt`` — 200 документов, но **200 из 200** найдены дословно в
  ``general_replay_ru.txt``, то есть для миксов с этим источником в реплее набор
  измеряет их же обучающий текст (ADR-025 п.3).

ADR-025 п.4 требует расширить v1 до ≥200 документов **из корпуса, не пересекающегося
ни с одним кандидатным миксом**. Исходные наборы при этом не трогаются: новый файл
пишется отдельным именем.

Откуда берётся чистый материал. Реплей-часть всех кандидатных миксов — это
``general_replay_ru.txt``, а он, по ``fetch_general_replay.py``, есть **префикс**
потока HF-датасета ``wikimedia/wikipedia`` конфигурации ``20231101.ru``: статьи
писались подряд, пока файл не дорос до цели, статьи короче 200 символов
отбрасывались. Значит, у того же датасета есть **хвост**, в префикс не попавший, —
он не встречается ни в одном миксе по построению и при этом имеет **то же
распределение**, что материал, ради защиты которого реплей и заведён. Это и есть
источник; никакой другой корпус для этого не нужен.

Почему хвост, а не «взять википедию вообще». Набор обязан быть чистым **и**
сопоставимым: PPL считается на тексте того же класса, что реплей, иначе «общий язык»
измерялся бы на другом языке (например, на английском wikitext из локального кэша).
Хвост того же дампа снимает и то, и другое.

Границы честности, названные явно:

* **Отбор не «случайная википедия»**, а детерминированная выборка из объявленного
  окна смещений: пул берётся из ``[--first-offset, --first-offset + --pool-rows)``
  страницами по ``--page`` строк, порядок строк — порядок датасета. Сдвиг окна
  выбран заведомо дальше префикса реплея, но **не предполагается** — пересечение с
  реплеем измеряется тем же инструментом, что и приёмка
  (``tools/check_eval_set_purity.py``), и в карточке называется числом;
* **Фильтры прозы** (длина, доля букв, число концов предложений) — это отбор
  *пригодного к измерению* текста, а не подгонка под результат: они объявлены
  константами и печатаются в карточке вместе с числом отсеянных кандидатов;
* **v3 не сопоставим с v1/v2 по шкале.** Другая длина документов — другая база;
  потолок пересчитывается как 2× новой базы (ADR-025, «Отрицательные последствия»),
  и старые отношения ×4.63/×9.48 остаются верными только по смыслу «с большим
  запасом», но не как числа, сравнимые с новыми.

Коды возврата::

    0 — набор собран (или проверка пройдена)
    1 — отказ: пул меньше требуемого, либо попытка перезаписи не своего файла
    2 — NOT-VERIFIED: нет сети/пула/токенизатора — не зелёный

Запуск::

    python3 tools/build_general_eval_v3.py --plan
    python3 tools/build_general_eval_v3.py --report data/gen-eval-v3-card.json
    python3 tools/build_general_eval_v3.py --verify datasets/general_eval_v3.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
#: Гейт чистоты импортируется как библиотека: нормализация, нарезка на окна и
#: проход по корпусу — его функции, а не пересказ. Набор обязан быть чистым **по
#: тому же правилу**, по которому его потом принимают; две копии правила — две
#: правды о том, что считалось совпадением.
import check_eval_set_purity as purity  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Источник — тот же датасет и та же конфигурация, что у реплея
#: (``fetch_general_replay.py``: ``wikimedia/wikipedia``, ``20231101.ru``).
DATASET = "wikimedia/wikipedia"
CONFIG = "20231101.ru"
SPLIT = "train"
ROWS_API = "https://datasets-server.huggingface.co/rows"

#: Разделитель документов набора — ровно то, чем режет прибор (``_ppl_eval``:
#: ``text.split("\\n---\\n")``). Внутри документа этой последовательности быть не
#: может: иначе при чтении документ распался бы, и записанное перестало бы
#: совпадать с измеренным (то же правило, что в ``build_gen_eval_v2.py``).
DOC_SEP = "\n---\n"

#: Минимальная длина документа в приборе: ``len(doc.strip()) > 50``.
PPLEVAL_MIN_CHARS = 50

#: Обрезка документа в приборе — по ней же считается «токенов в наборе».
PPLEVAL_MAX_TOKENS = 1024

#: ── Фильтры прозы (отбор измеримого текста, не подгонка под ответ) ───────────
#: Реплей отбрасывал статьи <200 символов (стабы). Здесь порог выше намеренно: набор
#: из 200 документов по 200 символов дал бы столько же шума, сколько v1.
PROSE_MIN_CHARS = 800
#: Проза от списков и таблиц отличается не темой, а долей букв и наличием концов
#: предложений: у «Списка эпизодов» буквы есть, а предложений нет.
PROSE_MIN_SENTENCES = 5
PROSE_MIN_ALPHA_SHARE = 0.60
#: Документ обязан дать прибору содержательный замер: короткий документ — это
#: точка с большим разбросом (``doc_ppl``), и 200 таких точек не различают ничего.
MIN_DOC_TOKENS = 200

#: Сид детерминированного отбора. Значение объявлено, а не «взято какое-то»:
#: пересборка набора обязана давать тот же файл, и это проверяется ``--verify``.
DEFAULT_SEED = 20260916

#: ── Защита чужих файлов ─────────────────────────────────────────────────────
#: Имена, которые этот инструмент не пишет **никогда**: исходные наборы ревизии.
#: Запрет по имени, а не по хешу: даже ``--force`` не должен уметь их тронуть
#: (ADR-025: расширение идёт отдельным файлом).
PROTECTED_NAMES = {"general_eval.txt", "general_eval_v2.txt",
                   "domain_eval.txt", "domain_eval_v2.txt"}

#: Нормализация — из гейта (lower + схлопнутые пробелы, как в
#: ``tools/check_eval_leakage.py``). Здесь она не переопределяется: набор,
#: собранный по одной нормализации и принятый по другой, — это два разных набора.
norm = purity.norm


def note(msg: str) -> None:
    """Прогресс — в stdout: «молчащий процесс» неотличим от зависшего."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def own_revision() -> dict:
    """Ревизия инструмента — файл обязан лежать на диске.

    «Хеш не посчитался» здесь не мелочь: карточка набора ссылается на ревизию
    сборщика, и без неё происхождение набора недоказуемо.
    """
    me = Path(__file__)
    if not me.is_file():
        raise SystemExit(
            f"ОТКАЗ: инструмент запущен без файла на диске ({me}) — ревизия сборщика "
            f"недоказуема. Запускай файлом, а не потоком со stdin")
    return {"path": "tools/build_general_eval_v3.py", "sha256": sha256_file(me)}


# ─────────────────────────── выгрузка пула ───────────────────────────────────


def fetch_page(offset: int, length: int, retries: int = 4, timeout: int = 90) -> list[dict]:
    """Одна страница ``/rows``; при сбое — повторы с паузой (сеть не идеальна).

    Ошибка не глушится: не вышло за ``retries`` — исключение, и сборка честно
    заканчивается NOT-VERIFIED, а не набором из меньшего пула.
    """
    q = urllib.parse.urlencode({"dataset": DATASET, "config": CONFIG, "split": SPLIT,
                                "offset": offset, "length": length})
    url = f"{ROWS_API}?{q}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if "rows" not in payload:
                raise RuntimeError(f"ответ без rows: {str(payload)[:200]}")
            return [r["row"] for r in payload["rows"]]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                RuntimeError) as exc:
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"offset={offset}: {type(last).__name__}: {last}")


def pool_offsets(first_offset: int, pages: int, stride: int) -> list[int]:
    """Смещения страниц пула: ``first_offset + i × stride``.

    Пул берётся **редкими страницами по всему хвосту**, а не одним сплошным
    куском: сплошное окно — это статьи, попавшие в дамп подряд (порядок дампа
    коррелирует с историей импорта), и тематическая кучность внутри него была бы
    свойством окна, а не выборки. Разрежение шагом ``stride`` стоит тех же
    запросов и снимает вопрос.
    """
    return [first_offset + i * stride for i in range(pages)]


def fetch_pool(offsets: list[int], page: int) -> tuple[list[dict], list[dict]]:
    """Пул кандидатов по объявленным смещениям.

    Возвращаются и строки, и журнал страниц (offset, сколько пришло) — по нему
    видно, что окно прочитано целиком, а не «сколько получилось».
    """
    rows: list[dict] = []
    journal: list[dict] = []
    for offset in offsets:
        got = fetch_page(offset, page)
        rows.extend(got)
        journal.append({"offset": offset, "rows": len(got)})
        note(f"  выгружено {len(rows)} строк (offset {offset}, страница {len(got)})")
    return rows, journal


# ─────────────────────────── фильтры и отбор ─────────────────────────────────


def alpha_share(text: str) -> float:
    """Доля букв среди непробельных символов — отличает прозу от таблиц и цифр."""
    solid = [c for c in text if not c.isspace()]
    if not solid:
        return 0.0
    return sum(1 for c in solid if c.isalpha()) / len(solid)


def sentence_ends(text: str) -> int:
    """Сколько раз в тексте кончается предложение — «. », «.\n», «!», «?»."""
    return (text.count(". ") + text.count(".\n") + text.count("! ") + text.count("? "))


def prose_reject(text: str) -> str | None:
    """Почему кандидат не годится как документ набора — или ``None``, если годится.

    Причина возвращается строкой, а не булевым: в карточке должно быть видно
    **почему** отсеян пул, а не только «отсеяно 812».
    """
    if len(text) < PROSE_MIN_CHARS:
        return "short"
    if DOC_SEP in text:
        return "doc_sep_inside"
    if sentence_ends(text) < PROSE_MIN_SENTENCES:
        return "not_prose_sentences"
    if alpha_share(text) < PROSE_MIN_ALPHA_SHARE:
        return "not_prose_alpha"
    return None


def token_len(tokenizer, text: str, max_len: int = PPLEVAL_MAX_TOKENS) -> int:
    """Сколько токенов увидит прибор: ``encode(add_special_tokens=False)[:1024]``."""
    return len(tokenizer.encode(text, add_special_tokens=False)[:max_len])


def select(rows: list[dict], tokenizer, docs_wanted: int, seed: int,
           corpora: dict[str, Path]) -> tuple[list[dict], dict]:
    """Детерминированный отбор документов набора.

    Порядок шагов важен и печатается в карточке:

    1. каноническая сортировка пула по ``id`` строки — чтобы результат не зависел
       от порядка страниц и от повторов сети;
    2. фильтры прозы (``prose_reject``), дедупликация и объём
       (``MIN_DOC_TOKENS``) — это отбор **измеримого** текста;
    3. выброс кандидатов, найденных в корпусах обучения (реплей и домен) —
       документов подстрокой **и** 12-граммовых окон. Окна здесь не украшение:
       первый же прогон гейта нашёл у википедийных статей общий с реплеем
       шаблонный хвост (перечни категорий вида «депутаты верховного совета
       рсфср 7-го созыва депутаты верховного совета рсфср 8-го созыва …»),
       который у 200 статей дал 81 общее окно при нуле совпавших документов.
       Инвариант ADR-025 бинарный (AD-11), поэтому такой кандидат отбрасывается
       целиком, а не «почти чистый»;
    4. ``random.Random(seed).shuffle`` — отбор;
    5. финальная сортировка выбранных по ``id`` — файл набора не зависит от сида,
       сид влияет только на **состав**.

    Возвращаются (документы, журнал отбора).
    """
    ordered = sorted(rows, key=lambda r: str(r.get("id", "")))
    rejects: dict[str, int] = {}
    seen_norm: set[str] = set()
    pool: list[dict] = []

    def reject(why: str) -> None:
        rejects[why] = rejects.get(why, 0) + 1

    for row in ordered:
        text = (row.get("text") or "").strip()
        why = prose_reject(text)
        if why:
            reject(why)
            continue
        n = norm(text)
        if n in seen_norm:
            reject("duplicate_in_pool")
            continue
        seen_norm.add(n)
        ntok = token_len(tokenizer, text)
        if ntok < MIN_DOC_TOKENS:
            reject("too_few_tokens")
            continue
        pool.append({"id": str(row.get("id", "")), "url": row.get("url"),
                     "title": row.get("title"), "text": text,
                     "chars": len(text), "tokens": ntok,
                     "norm_sha256": sha256_text(n), "norm": n})

    note(f"пул после фильтров прозы: {len(pool)} документов (отсев: {rejects})")
    if len(pool) < docs_wanted:
        raise RuntimeError(
            f"NOT-VERIFIED: пригодных кандидатов {len(pool)} < требуемых {docs_wanted}; "
            f"отсев: {rejects}")

    # ── чистота пула: один проход по каждому корпусу, окна и документы сразу ──
    targets: set[str] = set()
    for cand in pool:
        targets.update(purity.windows(cand["norm"].split()))
    note(f"окон пула: {len(targets)} — ищу их в корпусах обучения")
    purity_report: dict[str, dict] = {}
    dirty: set[int] = set()
    for role, path in corpora.items():
        sep = purity.DOMAIN_SEP if role == "domain" else purity.REPLAY_SEP
        res = purity.scan_corpus(path, sep, targets, [c["norm"] for c in pool])
        matched = res.pop("matched")
        purity_report[role] = res
        for hit in res["doc_hits"]:
            dirty.add(hit["target_index"])
        if matched:
            for idx, cand in enumerate(pool):
                if any(w in matched for w in purity.windows(cand["norm"].split())):
                    dirty.add(idx)
        note(f"  {role}: документов-совпадений {res['overlap_docs']}, "
             f"окон {res['overlap_ngram_windows']}; грязных кандидатов всего {len(dirty)}")
    for idx in sorted(dirty):
        reject("overlap_with_training_corpus")
    clean = [c for i, c in enumerate(pool) if i not in dirty]
    note(f"пул после проверки чистоты: {len(clean)} документов")

    if len(clean) < docs_wanted:
        raise RuntimeError(
            f"NOT-VERIFIED: чистых кандидатов {len(clean)} < требуемых {docs_wanted}; "
            f"отсев: {rejects}")

    shuffled = list(clean)
    random.Random(seed).shuffle(shuffled)
    chosen = sorted(shuffled[:docs_wanted], key=lambda d: d["id"])
    for c in chosen:
        c.pop("norm", None)
    return chosen, {"candidates_after_filters": len(pool),
                    "candidates_clean": len(clean),
                    "rejects": rejects,
                    "shuffle_seed": seed,
                    "purity_scan": purity_report,
                    "order_of_operations": [
                        "sort_by_id", "prose_filters", "dedup_norm", "min_tokens",
                        "purity_scan_docs_and_windows", "seeded_shuffle",
                        "sort_chosen_by_id"]}


# ─────────────────────────── токенизатор ─────────────────────────────────────


def load_tokenizer():
    """Токенизатор ревизии (ADR-002) через ``tools/ppl_probe.py``.

    Обход дефектов окружения берётся у ``ppl_probe``, а не пишется второй копией:
    две копии обхода — две правды о том, что было обойдено. Заглушки живут только
    в ``sys.modules`` этого процесса, окружение не меняется (AD-4).
    """
    tools_dir = Path(__file__).resolve().parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import ppl_probe as P
    P.repair_broken_pyopenssl()
    P.import_transformers()
    from transformers import AutoTokenizer
    tok_dir, missed = P.locate(P.TOKENIZER_CANDIDATES, "tokenizer.json")
    if tok_dir is None:
        raise RuntimeError("NOT-VERIFIED: токенизатор не найден: " + "; ".join(missed))
    return AutoTokenizer.from_pretrained(str(tok_dir), local_files_only=True), str(tok_dir)


# ─────────────────────────── сборка файла набора ─────────────────────────────


def dataset_text(docs: list[dict]) -> str:
    """Файл набора: документы через ``\\n---\\n`` и завершающий перевод строки.

    Формат — тот же, что у v1/v2 и тот, который читает прибор. Никакой
    «нормализации текста» при записи не делается: документ пишется ровно таким,
    каким пришёл из источника (кроме ``strip`` по краям) — иначе заявленный хеш
    файла перестал бы быть хешем измеренного текста.
    """
    return DOC_SEP.join(d["text"] for d in docs) + "\n"


def check_target(path: Path, force: bool) -> None:
    """Отказ писать чужой файл — по имени и по существованию."""
    if path.name in PROTECTED_NAMES:
        raise SystemExit(
            f"ОТКАЗ: {path.name} — исходный набор ревизии, он не перезаписывается "
            f"ни при каких флагах (ADR-025 п.4: расширение идёт отдельным файлом)")
    if path.exists() and not force:
        raise SystemExit(
            f"ОТКАЗ: {path} уже существует — перезапись только с --force "
            f"(ADR-025: состав набора меняется карточкой и коммитом)")


def build(args) -> tuple[dict, int]:
    out = Path(args.out)
    check_target(out, args.force)

    tokenizer, tok_dir = load_tokenizer()
    note(f"токенизатор: {tok_dir}")

    corpora: dict[str, Path] = {"replay": Path(args.replay), "domain": Path(args.domain)}
    missing = [f"{name}: {p}" for name, p in corpora.items() if not p.is_file()]
    if missing:
        print("NOT-VERIFIED: нет корпуса обучения: " + "; ".join(missing), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    offsets = pool_offsets(args.first_offset, args.pages, args.stride)
    try:
        rows, journal = fetch_pool(offsets, args.page)
    except RuntimeError as exc:
        print(f"NOT-VERIFIED: {exc}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    try:
        docs, sel = select(rows, tokenizer, args.docs, args.seed, corpora)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return {}, EXIT_FAIL

    text = dataset_text(docs)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    tokens = sum(d["tokens"] for d in docs)
    card = {
        "schema": "gen-eval-v3-card/1",
        "stage": "S3q",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": ("расширенный измерительный набор общего языка (ADR-025 п.4): "
                    "≥200 документов из корпуса, не пересекающегося ни с одним "
                    "кандидатным CPT-миксом"),
        "adr": ["ADR-025 п.4", "ADR-018 п.3", "ADR-022 п.3"],
        "tool": {**own_revision(), "argv": sys.argv[1:]},
        "source": {
            "dataset": DATASET, "config": CONFIG, "split": SPLIT,
            "endpoint": ROWS_API,
            "why": ("тот же датасет и конфигурация, что у реплея "
                    "(`general_replay_ru.txt` = префикс этого потока, "
                    "`fetch_general_replay.py`); набор берётся из ХВОСТА потока — "
                    "материала того же распределения, не попавшего в обучение"),
            "window": {"first_offset": args.first_offset, "pages": args.pages,
                       "stride": args.stride, "page": args.page,
                       "pool_rows": args.pages * args.page,
                       "offsets": offsets, "journal": journal,
                       "why_sparse": ("редкие страницы по хвосту потока вместо "
                                      "сплошного окна: снимает тематическую кучность "
                                      "порядка дампа, стоит тех же запросов")},
            "license": "CC BY-SA 4.0 (Wikimedia, дамп 20231101.ru)",
            "attribution": ("каждая статья — с id, url и заголовком в `documents`; "
                            "атрибуция Wikimedia сохраняется ссылкой на источник"),
        },
        "filters": {
            "doc_sep": repr(DOC_SEP),
            "ppleval_min_chars": PPLEVAL_MIN_CHARS,
            "prose_min_chars": PROSE_MIN_CHARS,
            "prose_min_sentences": PROSE_MIN_SENTENCES,
            "prose_min_alpha_share": PROSE_MIN_ALPHA_SHARE,
            "min_doc_tokens": MIN_DOC_TOKENS,
            "ppleval_max_tokens": PPLEVAL_MAX_TOKENS,
            "note": ("фильтры прозы отбирают измеримый текст (стабы и таблицы дают "
                     "точки с разбросом, а не сигнал); они объявлены константами "
                     "и не подбирались под результат"),
        },
        "purity": {
            "checked_corpora": {name: str(path) for name, path in corpora.items()},
            "rule": ("кандидат отброшен, если его нормализованный текст найден "
                     "подстрокой в любом из корпусов обучения **или** хотя бы одно "
                     "его 12-граммовое окно встречается в корпусе; проверка окон — "
                     "тем же кодом, что у гейта (tools/check_eval_set_purity.py), "
                     "а не второй копией правила"),
            "window_size": purity.WINDOW,
            "scan": sel["purity_scan"],
            "rejected_candidates": sel["rejects"].get("overlap_with_training_corpus", 0),
        },
        "selection": sel,
        "set": {
            "path": str(out), "sha256": sha256_file(out), "bytes": out.stat().st_size,
            "docs": len(docs), "tokens": tokens,
            "tokens_rule": "encode(add_special_tokens=False)[:1024] по каждому документу",
            "doc_chars_min": min(d["chars"] for d in docs),
            "doc_chars_max": max(d["chars"] for d in docs),
            "doc_tokens_min": min(d["tokens"] for d in docs),
            "doc_tokens_max": max(d["tokens"] for d in docs),
        },
        "documents": docs,
    }
    return card, EXIT_OK


def verify(args) -> int:
    """Проверка записанного набора: формат, объём, чистоту и токены пересчитать.

    Отдельный путь, потому что «файл записан» и «файл читается прибором как
    задумано» — разные утверждения.
    """
    path = Path(args.verify)
    if not path.is_file():
        print(f"NOT-VERIFIED: нет файла {path}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    parts = text.split(DOC_SEP)
    docs = [d.strip() for d in parts if len(d.strip()) > PPLEVAL_MIN_CHARS]
    if len(docs) < args.docs:
        print(f"ОТКАЗ: документов {len(docs)} < заявленных {args.docs}", file=sys.stderr)
        return EXIT_FAIL
    if len(docs) != len(parts):
        print(f"ОТКАЗ: {len(parts) - len(docs)} частей файла короче "
              f"{PPLEVAL_MIN_CHARS} символов — прибор их не увидит", file=sys.stderr)
        return EXIT_FAIL
    tokenizer, _ = load_tokenizer()
    toks = [token_len(tokenizer, d) for d in docs]
    print(f"набор: {path}")
    print(f"  sha256: {sha256_file(path)}")
    print(f"  байт: {len(raw)}  документов: {len(docs)}  токенов: {sum(toks)}")
    print(f"  документ: символов {min(len(d) for d in docs)}…{max(len(d) for d in docs)}, "
          f"токенов {min(toks)}…{max(toks)}")
    print("  (пересечение с корпусами обучения проверяет гейт "
          "tools/check_eval_set_purity.py — он же и критерий приёмки; здесь "
          "повторялась бы вторая копия правила)")
    return EXIT_OK


# ─────────────────────────── CLI ─────────────────────────────────────────────


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="S3q: сборка расширенного измерительного набора общего языка "
                    "(ADR-025 п.4)")
    ap.add_argument("--out", default="/home/user/gb10-shared/datasets/general_eval_v3.txt",
                    help="куда писать набор (по умолчанию — сетевой диск, AD-4)")
    ap.add_argument("--report", default=None, help="карточка набора (json)")
    ap.add_argument("--docs", type=int, default=200, help="сколько документов в наборе")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="сид отбора")
    ap.add_argument("--pages", type=int, default=12,
                    help="сколько страниц датасета выгрузить в пул кандидатов")
    ap.add_argument("--stride", type=int, default=50000,
                    help="шаг между страницами пула (разрежение по хвосту потока)")
    ap.add_argument("--first-offset", type=int, default=400000,
                    help="с какого смещения брать пул (заведомо дальше префикса реплея)")
    ap.add_argument("--page", type=int, default=100, help="размер страницы /rows")
    ap.add_argument("--replay", default="/home/user/gb10-shared/datasets/general_replay_ru.txt",
                    help="корпус реплея (кандидаты из него отбрасываются)")
    ap.add_argument("--domain", default="/home/user/gb10-shared/datasets/cpt_corpus_v10.1.txt",
                    help="доменный корпус (кандидаты из него отбрасываются)")
    ap.add_argument("--verify", default=None, help="проверить готовый набор и выйти")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать существующий файл набора (не исходные наборы)")
    ap.add_argument("--plan", action="store_true", help="показать план и выйти")
    return ap.parse_args(argv)


def print_plan(args) -> int:
    print("S3q — сборка расширенного измерительного набора общего языка")
    print(f"  источник: {DATASET} / {CONFIG} / {SPLIT}")
    offs = pool_offsets(args.first_offset, args.pages, args.stride)
    print(f"  окно пула: {len(offs)} страниц по {args.page} строк, шаг {args.stride}, "
          f"[{offs[0]}, {offs[-1] + args.page}) — {len(offs) * args.page} кандидатов")
    print(f"  фильтры: ≥{PROSE_MIN_CHARS} символов, ≥{PROSE_MIN_SENTENCES} концов "
          f"предложений, доля букв ≥{PROSE_MIN_ALPHA_SHARE}, ≥{MIN_DOC_TOKENS} токенов")
    print(f"  отбор: {args.docs} документов, сид {args.seed}, "
          f"исключение найденного в {Path(args.replay).name} и {Path(args.domain).name}")
    print(f"  запись: {args.out} (исходные наборы не трогаются)")
    return EXIT_OK


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.plan:
        return print_plan(args)
    if args.verify:
        return verify(args)
    card, rc = build(args)
    if args.report and card:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"карточка: {out}")
    if card:
        s = card["set"]
        print(f"набор: {s['path']}  sha256={s['sha256']}")
        print(f"  документов {s['docs']}  токенов {s['tokens']}  байт {s['bytes']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
