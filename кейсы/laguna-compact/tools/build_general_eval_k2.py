#!/usr/bin/env python3
"""S3t — сборка компоненты K2: измерительный набор ВНЕ обучающего распределения.

Зачем вторая компонента. ADR-027 объявляет меру общего языка двухкомпонентной.
K1 (``general_eval_v3.txt``) собран из хвоста того же дампа, что реплей, и отвечает
на вопрос «защитил ли реплей тот класс текста, ради которого заведён». Он этого не
различает: совпадение **жанра** с обучающим материалом даёт рукам преимущество,
которого гейт ADR-025 п.1 не ловит (он про документы, не про распределение).
K2 отвечает на второй вопрос — «сохранила ли модель обобщение за пределами
обучающего распределения».

Что здесь источник и почему он такой. Обучение ревизии состоит из трёх корпусов:
русская Википедия (реплей), концепт-карты ML/AI (домен) и SFT-диалоги по тем же
концептам. Жанр K2 обязан быть **другим**: ни энциклопедическая статья, ни
структурированная карточка концепта, ни чат-разметка.

Разведка контура (S3t) нашла ровно один корпус русской прозы другого жанра,
физически лежащий на диске: ``bulletin_1998_2022.txt`` — выгрузка бюллетеней
Счётной палаты РФ 1998–2022 (государственный финансовый аудит, бюджетное право,
аналитические отчёты, публицистика и лекции). Его происхождение **независимо** от
Wikimedia и от доменного корпуса, и это делает его пригодным для роли K2.
Границы этой пригодности названы честно:

* **жанр узкий.** Это официально-аналитическая проза, а не новости, не научпоп и не
  художественная литература. ADR-027 требует «другого жанра, не представленного в
  обучении» — этому требованию корпус удовлетворяет; перечень «научпоп, новости,
  художественная проза» в ADR-027 — примеры, а не закрытый список. Смена жанра —
  решение архитектора, и сборщик параметризован источником (``--source``), чтобы
  смена стоила одной пересборки, а не новой дельты;
* **OCR-артефакты.** Текст выгружен из PDF: жёсткий перенос строки, склейка слов
  через дефис на переносе (``государствен-\\nный``), пропуски пробела после
  запятой, вставки таблиц и номеров страниц. Перенос снимается объявленным правилом
  (см. ``SEGMENTATION``), таблицы и мусор отсекаются фильтрами прозы, но полностью
  шум не устраняется: он общий для базы и для всех рук, то есть константа прибора,
  а не свойство отдельной руки;
* **лицензия.** Корпус — локальная выгрузка официальных публикаций Счётной палаты
  (проект ``inspector-bot``); файла лицензии в контуре нет. Режим — внутреннее
  использование (ADR-005, AD-8): обучение/eval/хранение внутри периметра
  допустимы, публикация производных запрещена. Статус назван в карточке явно
  (``license_status``), а не оставлен умолчанием.

Как устроена нарезка на документы. Прибор режет файл набора по ``\\n---\\n`` и
считает PPL по документу (обрезка до 1024 токенов). В Википедии документ — статья,
у неё естественная граница. У выгрузки бюллетеней естественной границы нет: это
поток жёстко перенесённых строк. Поэтому документ здесь — **блок текста между
пустыми строками** (граница самой выгрузки: пустая строка отделяет смысловой
фрагмент), приведённый к одной строке. Это конструкция, и она объявлена:
``SEGMENTATION`` печатается в карточку вместе с числом отброшенных блоков.

Почему это не «набор из мусора». Фильтры прозы (длина, концы предложений, доля
букв), языковой фильтр (доля кириллицы среди букв), порог по токенам и выброс
всего, что найдено в корпусах обучения, — те же по смыслу, что у K1, и объявлены
константами до прогона. Кандидат, забракованный фильтрами, не попадает в набор
никогда; набор собирается из **пула в десятки тысяч** блоков, из которых берётся
объявленное число.

Коды возврата::

    0 — набор собран (или проверка пройдена)
    1 — отказ: пул меньше требуемого, либо попытка перезаписи не своего файла
    2 — NOT-VERIFIED: нет источника/токенизатора — не зелёный

Запуск::

    python3 tools/build_general_eval_k2.py --plan
    python3 tools/build_general_eval_k2.py --report data/gen-eval-k2-card.json
    python3 tools/build_general_eval_k2.py --verify datasets/general_eval_k2.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
#: Гейт чистоты импортируется как библиотека: нормализация, нарезка на окна и
#: проход по корпусу — его функции, а не пересказ. Набор обязан быть чистым **по
#: тому же правилу**, по которому его потом принимают; две копии правила — две
#: правды о том, что считалось совпадением (та же причина, что у сборщика K1).
import check_eval_set_purity as purity  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Источник K2 — корпус русской прозы другого жанра, лежащий в контуре.
DEFAULT_SOURCE = "/home/user/inspector-bot/docs/bulletin_1998_2022.txt"

#: Куда пишется набор. Сетевой диск, а не кейс: AD-4 (в кейсе — симлинк
#: ``datasets/``). Имя несёт роль набора (ADR-027 п.4: адресация ролью, а не
#: сквозным номером) и **новое**: исходные наборы ревизии не перезаписываются.
DEFAULT_OUT = "/home/user/gb10-shared/datasets/general_eval_k2.txt"

#: Разделитель документов набора — ровно то, чем режет прибор (``_ppl_eval``:
#: ``text.split("\\n---\\n")``). Внутри документа этой последовательности быть не
#: может: иначе при чтении документ распался бы, и записанное перестало бы
#: совпадать с измеренным.
DOC_SEP = "\n---\n"

#: ── Нарезка (объявлена, а не «как получилось») ───────────────────────────────
#: Блок = текст между пустыми строками. Внутри блока строки соединяются пробелом:
#: выгрузка жёстко переносит строки по ~78 символов, и оставить переносы значило бы
#: измерять вёрстку PDF, а не язык.
SEGMENTATION = {
    "unit": "блок между пустыми строками (разделитель '\\n\\n')",
    "line_join": "внутри блока перевод строки заменяется пробелом",
    "dehyphenation": ("концевой дефис ('-', U+2010, U+2011) перед переводом строки "
                      "и строчной буквой снимается, строки склеиваются без пробела: "
                      "'государствен-\\nный' → 'государственный'"),
    "dehyphenation_limit": ("правило не отличает перенос от настоящего дефиса "
                            "('какой-\\nто' склеится в 'какойто'); доля таких "
                            "случаев не измерена и не заявляется как нулевая"),
    "whitespace": "повторные пробелы схлопываются, края обрезаются",
}

#: Минимальная длина документа в приборе: ``len(doc.strip()) > 50``.
PPLEVAL_MIN_CHARS = 50

#: Обрезка документа в приборе — по ней же считается «токенов в наборе».
PPLEVAL_MAX_TOKENS = 1024

#: ── Фильтры прозы: те же константы, что у сборщика K1 ────────────────────────
#: Одинаковые пороги у двух компонент — не косметика: разные пороги сделали бы
#: K1 и K2 несопоставимыми по трудности отбора, а сравнение компонент — часть
#: решения (ADR-027 п.2). Пороги объявлены до прогона и не подбирались под ответ.
PROSE_MIN_CHARS = 800
PROSE_MIN_SENTENCES = 5
PROSE_MIN_ALPHA_SHARE = 0.60
MIN_DOC_TOKENS = 200

#: Доля кириллицы среди **букв** — языковой фильтр. Нужен именно здесь: в выгрузке
#: Счётной палаты есть англоязычные блоки (оглавление, аннотации докладов), и без
#: фильтра «русская проза» превратилась бы в «русская проза плюс английское
#: оглавление». Порог 0.90, а не 1.0: транслитерации, латинские аббревиатуры и
#: названия организаций в русском деловом тексте — норма, а не другой язык.
CYRILLIC_MIN_SHARE = 0.90

#: Максимальная доля «склеенных» слов (≥25 символов без пробела) — признак сбоя
#: извлечения текста из PDF: ``чтовыручкаотреализациитоваров``. Это дефект
#: **извлечения**, а не языка: он делает текст нечитаемым для модели и подмешивает
#: в замер трудность, которой в русской прозе нет. Порог 1 %: медиана по пулу —
#: 0.0000, 75-й процентиль — 0.0032, то есть фильтр отсекает хвост, а не середину.
OCR_GLUE_MAX_SHARE = 0.01
#: Длина, с которой слово считается склеенным. Русское слово такой длины
#: существует (сложносоставные термины), поэтому порог не 20, а 25: короткие
#: склейки фильтр пропускает намеренно — иначе он начнёт выбраковывать
#: терминологию вместо дефекта.
OCR_GLUE_MIN_WORD = 25

#: Сид детерминированного отбора. Значение объявлено, а не «взято какое-то»:
#: пересборка обязана давать тот же файл, и это проверяет ``--verify``.
DEFAULT_SEED = 20260916

#: Сколько кандидатов уходит на проверку чистоты. Пул здесь — десятки тысяч
#: блоков, а окна всех кандидатов (12-граммовые, ~300 на документ) в память не
#: влезают. Поэтому проверяется **объявленный префикс** перемешанного пула:
#: сначала сид, потом отсев грязных, потом выбор — тогда сид влияет на состав,
#: но не на чистоту, и отсев не «подгоняет» набор под результат.
#:
#: Почему 800, а не «весь пул». Стоимость прохода ``purity.scan_corpus`` линейна
#: по числу проверяемых документов: их нормализованный текст ищется подстрокой в
#: каждом документе корпуса (172–199 тыс. документов у реплея и домена). Замер
#: S3t: окно 3000 даёт 767 тыс. окон и ~4 мин **на один** корпус из пяти, то есть
#: ~25 мин на сборку; окно 800 даёт ~200 тыс. окон и ~10 мин на все пять. Выигрыш
#: не в «меньше проверять», а в том, что проверка остаётся того же правила:
#: 800 кандидатов — вчетверо больше, чем в наборе, и отсев грязных виден числом.
SCAN_POOL = 800

#: ── Корпуса обучения, в которых кандидату быть не позволено ─────────────────
#: Роли названы так же, как в карточках миксов; разделитель документов корпуса
#: берётся по роли (``domain*`` — ``\\n---\\n``, ``replay`` — ``\\n\\n``, ``sft`` —
#: строка JSONL). Это не «список для красоты»: именно эти файлы видело обучение,
#: и именно против них проверяется кандидат.
TRAINING_CORPORA: dict[str, str] = {
    "replay": "/home/user/gb10-shared/datasets/general_replay_ru.txt",
    "domain_v10.1": "/home/user/gb10-shared/datasets/cpt_corpus_v10.1.txt",
    "domain_full": "/home/user/gb10-shared/datasets/cpt_corpus_full.txt",
    "sft_v12": "/home/user/gb10-shared/datasets/sft_train_v12.jsonl",
    "sft_base": "/home/user/gb10-shared/datasets/sft_train.jsonl",
}

#: Имена, которые этот инструмент не пишет **никогда**: наборы ревизии.
#: Запрет по имени, а не по хешу: даже ``--force`` не должен уметь их тронуть.
PROTECTED_NAMES = {"general_eval.txt", "general_eval_v2.txt", "general_eval_v3.txt",
                   "domain_eval.txt", "domain_eval_v2.txt"}

#: Нормализация — из гейта (lower + схлопнутые пробелы, как в
#: ``tools/check_eval_leakage.py``). Здесь она не переопределяется.
norm = purity.norm

#: Концевой дефис перед переводом строки: ASCII-дефис, U+2010 (HYPHEN),
#: U+2011 (NON-BREAKING HYPHEN). Смотрим вперёд на строчную кириллицу — заглавная
#: буква после дефиса означает, что это, скорее всего, настоящее тире между
#: частями, а не перенос.
_DEHYPHEN = re.compile("[-‐‑]\n(?=[а-яё])")
_WS_RUN = re.compile(r"[ \t]{2,}")
_CYR = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-z]")


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
    return {"path": "tools/build_general_eval_k2.py", "sha256": sha256_file(me)}


# ─────────────────────────── нарезка источника ───────────────────────────────


def to_document(block: str) -> str:
    """Блок выгрузки → документ набора: снять переносы, соединить строки."""
    text = _DEHYPHEN.sub("", block)
    text = text.replace("\n", " ")
    text = _WS_RUN.sub(" ", text)
    return text.strip()


def alpha_share(text: str) -> float:
    """Доля букв среди непробельных символов — отличает прозу от таблиц и цифр."""
    solid = [c for c in text if not c.isspace()]
    if not solid:
        return 0.0
    return sum(1 for c in solid if c.isalpha()) / len(solid)


def cyrillic_share(text: str) -> float:
    """Доля кириллицы среди букв — языковой фильтр (см. ``CYRILLIC_MIN_SHARE``)."""
    cyr = len(_CYR.findall(text))
    lat = len(_LAT.findall(text))
    total = cyr + lat
    return cyr / total if total else 0.0


def glue_share(text: str) -> float:
    """Доля слов длиной ≥ ``OCR_GLUE_MIN_WORD`` — признак сбоя извлечения из PDF."""
    words = text.split()
    if not words:
        return 0.0
    return sum(1 for w in words if len(w) >= OCR_GLUE_MIN_WORD) / len(words)


def sentence_ends(text: str) -> int:
    """Сколько раз в тексте кончается предложение — «. », «. », «!», «?»."""
    return (text.count(". ") + text.count("! ") + text.count("? ") + text.count(". "))


def prose_reject(text: str) -> str | None:
    """Почему кандидат не годится как документ набора — или ``None``, если годится.

    Причина возвращается строкой, а не булевым: в карточке должно быть видно
    **почему** отсеян пул, а не только «отсеяно 812».
    """
    if len(text) < PROSE_MIN_CHARS:
        return "short"
    if DOC_SEP in text or "\n" in text:
        return "doc_sep_inside"
    if sentence_ends(text) < PROSE_MIN_SENTENCES:
        return "not_prose_sentences"
    if alpha_share(text) < PROSE_MIN_ALPHA_SHARE:
        return "not_prose_alpha"
    if cyrillic_share(text) < CYRILLIC_MIN_SHARE:
        return "not_russian"
    if glue_share(text) > OCR_GLUE_MAX_SHARE:
        return "ocr_glued_words"
    return None


def token_len(tokenizer, text: str, max_len: int = PPLEVAL_MAX_TOKENS) -> int:
    """Сколько токенов увидит прибор: ``encode(add_special_tokens=False)[:1024]``."""
    return len(tokenizer.encode(text, add_special_tokens=False)[:max_len])


# ─────────────────────────── токенизатор ─────────────────────────────────────


def load_tokenizer():
    """Токенизатор ревизии (ADR-002) через ``tools/ppl_probe.py``.

    Обход дефектов окружения берётся у ``ppl_probe``, а не пишется второй копией:
    две копии обхода — две правды о том, что было обойдено.
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


# ─────────────────────────── пул кандидатов ──────────────────────────────────


def split_blocks(text: str) -> list[str]:
    """Кандидаты — блоки между пустыми строками, в порядке источника (id = индекс)."""
    return text.split("\n\n")


def build_pool(source: Path, tokenizer) -> tuple[list[dict], dict]:
    """Пул кандидатов: нарезка, фильтры прозы, дедупликация, порог по токенам.

    Порядок шагов печатается в карточку (``order_of_operations``): результат обязан
    быть воспроизводим, а воспроизводимость без объявленного порядка шагов — это
    обещание, а не свойство.
    """
    t0 = time.time()
    raw = source.read_text(encoding="utf-8", errors="replace")
    blocks = split_blocks(raw)
    note(f"источник прочитан: {len(raw)/1e6:.1f} M символов, блоков {len(blocks)} "
         f"({time.time()-t0:.1f} с)")
    del raw

    rejects: dict[str, int] = {}
    seen_norm: set[str] = set()
    seen_head: set[str] = set()
    pool: list[dict] = []

    def reject(why: str) -> None:
        rejects[why] = rejects.get(why, 0) + 1

    for idx, block in enumerate(blocks):
        why = "short" if len(block) < PROSE_MIN_CHARS else None
        if why is None:
            text = to_document(block)
            why = prose_reject(text)
        if why:
            reject(why)
            continue
        n = norm(text)
        if n in seen_norm:
            reject("duplicate_in_pool")
            continue
        #: Повтор одной и той же публикации в разных выпусках отличается OCR-шумом,
        #: поэтому точного совпадения мало: сравнивается «голова» документа.
        head = sha256_text(n[:400])
        if head in seen_head:
            reject("duplicate_head_in_pool")
            continue
        ntok = token_len(tokenizer, text)
        if ntok < MIN_DOC_TOKENS:
            reject("too_few_tokens")
            continue
        seen_norm.add(n)
        seen_head.add(head)
        pool.append({"id": idx, "text": text, "chars": len(text), "tokens": ntok,
                     "norm_sha256": sha256_text(n), "norm": n})

    note(f"пул после фильтров: {len(pool)} документов (отсев: {rejects})")
    return pool, {"order_of_operations": [
        "split_blocks_blank_line", "dehyphenate_join_lines", "prose_filters",
        "language_filter_cyrillic", "ocr_glue_filter", "dedup_norm", "dedup_head",
        "min_tokens"],
        "rejects": rejects, "blocks_total": len(blocks),
        "seconds_parse": round(time.time() - t0, 1)}


# ─────────────────────────── отбор ───────────────────────────────────────────


def select(pool: list[dict], corpora: dict[str, Path], docs_wanted: int,
           seed: int, scan_pool: int) -> tuple[list[dict], dict]:
    """Детерминированный отбор: сид → отсев грязных → выбор → сортировка.

    Проверка чистоты идёт по **объявленному префиксу** перемешанного пула
    (``scan_pool`` кандидатов): окна всего пула в память не влезают. Префикс
    берётся после перемешивания, поэтому он не «лучшие документы», а случайная
    выборка сидом — и состав набора от этого не смещается.
    """
    if len(pool) < scan_pool:
        raise RuntimeError(
            f"NOT-VERIFIED: пул {len(pool)} < объявленного окна проверки {scan_pool}")
    shuffled = list(pool)
    random.Random(seed).shuffle(shuffled)
    scanned = shuffled[:scan_pool]

    targets: set[str] = set()
    for cand in scanned:
        targets.update(purity.windows(cand["norm"].split()))
    note(f"окон в окне проверки: {len(targets)} — ищу их в корпусах обучения")

    purity_report: dict[str, dict] = {}
    dirty: set[int] = set()
    for role, path in corpora.items():
        if role.startswith("domain"):
            sep = purity.DOMAIN_SEP
        elif role.startswith("replay"):
            sep = purity.REPLAY_SEP
        else:
            sep = "\n"
        res = purity.scan_corpus(path, sep, targets, [c["norm"] for c in scanned])
        matched = res.pop("matched")
        purity_report[role] = {**res, "separator": repr(sep)}
        #: ``scan_corpus`` отдаёт в отчёт только первые 20 совпадений документов
        #: (чтобы отчёт не разбухал). Индекс грязного кандидата берётся оттуда, и
        #: если совпадений больше, чем в отчёте, — это отказ, а не «отсеяли 20 из 40».
        if res["overlap_docs"] > len(res["doc_hits"]):
            raise RuntimeError(
                f"NOT-VERIFIED: {role}: документов-совпадений "
                f"{res['overlap_docs']} > {len(res['doc_hits'])} в отчёте — "
                f"индексы грязных кандидатов неполны")
        for hit in res["doc_hits"]:
            dirty.add(hit["target_index"])
        if matched:
            for i, cand in enumerate(scanned):
                if any(w in matched for w in purity.windows(cand["norm"].split())):
                    dirty.add(i)
        note(f"  {role}: документов-совпадений {res['overlap_docs']}, "
             f"окон {res['overlap_ngram_windows']}; грязных кандидатов {len(dirty)}")

    clean = [c for i, c in enumerate(scanned) if i not in dirty]
    if len(clean) < docs_wanted:
        raise RuntimeError(
            f"NOT-VERIFIED: чистых кандидатов {len(clean)} < требуемых {docs_wanted}")
    #: Документы набора отдаются **копиями** без нормализованного текста: попади
    #: словарь кандидата в результат по ссылке — второй вызов на том же пуле
    #: упал бы на отсутствующем ``norm``, а вызывающий получил бы свои же данные
    #: изменёнными. Функция, портящая вход, ломается ровно там, где её переиспользуют.
    chosen = [{k: v for k, v in c.items() if k != "norm"}
              for c in sorted(clean[:docs_wanted], key=lambda d: d["id"])]
    return chosen, {
        "pool": len(pool), "scan_pool": scan_pool,
        "clean_in_scan_pool": len(clean),
        "rejected_by_purity": len(dirty),
        "shuffle_seed": seed,
        "purity_scan": purity_report,
    }


# ─────────────────────────── сборка файла набора ─────────────────────────────


def dataset_text(docs: list[dict]) -> str:
    """Файл набора: документы через ``\\n---\\n`` и завершающий перевод строки."""
    return DOC_SEP.join(d["text"] for d in docs) + "\n"


def check_target(path: Path, force: bool) -> None:
    """Отказ писать чужой файл — по имени и по существованию."""
    if path.name in PROTECTED_NAMES:
        raise SystemExit(
            f"ОТКАЗ: {path.name} — набор ревизии, он не перезаписывается ни при "
            f"каких флагах (ADR-027: компонента K2 идёт отдельным файлом)")
    if path.exists() and not force:
        raise SystemExit(
            f"ОТКАЗ: {path} уже существует — перезапись только с --force "
            f"(ADR-025 п.5: состав набора меняется карточкой и коммитом)")


# ─────────────────────────── сборка ──────────────────────────────────────────


def build(args) -> tuple[dict, int]:
    out = Path(args.out)
    check_target(out, args.force)

    source = Path(args.source)
    if not source.is_file():
        print(f"NOT-VERIFIED: нет источника K2: {source}", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    corpora = {role: Path(p) for role, p in args.corpus}
    missing = [f"{role}: {p}" for role, p in corpora.items() if not p.is_file()]
    if missing:
        print("NOT-VERIFIED: нет корпуса обучения: " + "; ".join(missing), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    try:
        tokenizer, tok_dir = load_tokenizer()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED
    note(f"токенизатор: {tok_dir}")

    pool, parse = build_pool(source, tokenizer)
    try:
        docs, sel = select(pool, corpora, args.docs, args.seed, args.scan_pool)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return {}, EXIT_FAIL

    text = dataset_text(docs)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    tokens = sum(d["tokens"] for d in docs)
    card = {
        "schema": "gen-eval-k2-card/1",
        "stage": "S3t",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": ("компонента K2 меры общего языка (ADR-027): ≥200 документов "
                    "русской прозы жанра, не представленного в обучении"),
        "adr": ["ADR-027", "ADR-025 п.1", "ADR-025 п.4", "ADR-025 п.5", "ADR-005", "AD-4"],
        "tool": {**own_revision(), "argv": sys.argv[1:]},
        "source": {
            "path": str(source),
            "sha256": sha256_file(source),
            "bytes": source.stat().st_size,
            "what": ("выгрузка бюллетеней Счётной палаты РФ 1998–2022: отчёты о "
                     "проверках, аналитика по бюджету и праву, публицистика и лекции"),
            "genre": "официально-аналитическая проза государственного финансового аудита",
            "provenance": ("локальная выгрузка публикаций Счётной палаты РФ (проект "
                           "inspector-bot, каталог docs/); источник независим от "
                           "Wikimedia и от доменного корпуса ML/AI"),
            "language": "ru",
            "distribution_relation": "out-of-distribution",
            "distribution_why": ("в обучающем распределении ревизии три класса текста: "
                                 "энциклопедическая статья (реплей), концепт-карта ML/AI "
                                 "(домен) и чат-разметка SFT. Ни один из них не является "
                                 "аудиторской/бюджетно-правовой прозой; происхождение "
                                 "набора независимо от обоих источников обучения"),
            "license_status": "internal_only",
            "license_note": ("файла лицензии в контуре нет; корпус — выгрузка "
                             "официальных публикаций государственного органа, но "
                             "содержит и авторские материалы (лекции, статьи). Режим "
                             "ADR-005/AD-8: внутреннее использование разрешено, "
                             "публикация производных запрещена"),
        },
        "segmentation": {**SEGMENTATION,
                         "why": ("у выгрузки нет естественной границы документа "
                                 "(сплошной поток жёстко перенесённых строк); блок "
                                 "между пустыми строками — граница самой выгрузки"),
                         "consequence": ("документ K2 — фрагмент потока, а не "
                                         "публикация целиком: возможен стык тем внутри "
                                         "одного документа. Это названное ограничение, "
                                         "а не незамеченный дефект")},
        "filters": {
            "doc_sep": repr(DOC_SEP),
            "ppleval_min_chars": PPLEVAL_MIN_CHARS,
            "prose_min_chars": PROSE_MIN_CHARS,
            "prose_min_sentences": PROSE_MIN_SENTENCES,
            "prose_min_alpha_share": PROSE_MIN_ALPHA_SHARE,
            "cyrillic_min_share": CYRILLIC_MIN_SHARE,
            "ocr_glue_max_share": OCR_GLUE_MAX_SHARE,
            "ocr_glue_min_word": OCR_GLUE_MIN_WORD,
            "min_doc_tokens": MIN_DOC_TOKENS,
            "ppleval_max_tokens": PPLEVAL_MAX_TOKENS,
            "note": ("пороги прозы совпадают с K1 намеренно: разные пороги сделали бы "
                     "компоненты несопоставимыми по трудности отбора (ADR-027 п.2); "
                     "языковой фильтр и фильтр склейки добавлены здесь, потому что "
                     "выгрузка из PDF даёт и англоязычные блоки, и сбои извлечения"),
        },
        "purity": {
            "checked_corpora": {role: str(p) for role, p in corpora.items()},
            "rule": ("кандидат отброшен, если его нормализованный текст найден "
                     "подстрокой в любом корпусе обучения **или** хотя бы одно его "
                     "12-граммовое окно встречается в корпусе; проверка — тем же "
                     "кодом, что у гейта (tools/check_eval_set_purity.py)"),
            "window_size": purity.WINDOW,
            "scope": (f"объявленный префикс перемешанного пула: {args.scan_pool} "
                      f"кандидатов из {len(pool)}; окна всего пула в память не влезают"),
            "scan": sel["purity_scan"],
            "rejected_candidates": sel["rejected_by_purity"],
        },
        "selection": {**parse, **sel, "docs_wanted": args.docs,
                      "chosen_ids": [d["id"] for d in docs]},
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
    """Проверка записанного набора: формат, объём, токены пересчитать.

    Отдельный путь, потому что «файл записан» и «файл читается прибором как
    задумано» — разные утверждения.
    """
    path = Path(args.verify)
    if not path.is_file():
        print(f"NOT-VERIFIED: нет файла {path}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    raw = path.read_bytes()
    parts = raw.decode("utf-8").split(DOC_SEP)
    docs = [d.strip() for d in parts if len(d.strip()) > PPLEVAL_MIN_CHARS]
    if len(docs) != len(parts):
        print(f"ОТКАЗ: {len(parts) - len(docs)} частей файла короче "
              f"{PPLEVAL_MIN_CHARS} символов — прибор их не увидит", file=sys.stderr)
        return EXIT_FAIL
    if len(docs) < args.docs:
        print(f"ОТКАЗ: документов {len(docs)} < заявленных {args.docs}", file=sys.stderr)
        return EXIT_FAIL
    non_ru = [i for i, d in enumerate(docs) if cyrillic_share(d) < CYRILLIC_MIN_SHARE]
    if non_ru:
        print(f"ОТКАЗ: документы без русского языка: {non_ru[:10]}", file=sys.stderr)
        return EXIT_FAIL
    glued = [i for i, d in enumerate(docs) if glue_share(d) > OCR_GLUE_MAX_SHARE]
    if glued:
        print(f"ОТКАЗ: документы со сбоем извлечения из PDF: {glued[:10]}", file=sys.stderr)
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
        description="S3t: сборка компоненты K2 — измерительного набора вне "
                    "обучающего распределения (ADR-027)")
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="корпус русской прозы другого жанра (в контуре)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="куда писать набор (по умолчанию — сетевой диск, AD-4)")
    ap.add_argument("--report", default=None, help="карточка набора (json)")
    ap.add_argument("--docs", type=int, default=200, help="сколько документов в наборе")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="сид отбора")
    ap.add_argument("--scan-pool", type=int, default=SCAN_POOL,
                    help="сколько кандидатов уходит на проверку чистоты")
    ap.add_argument("--corpus", action="append", default=[],
                    metavar="ROLE=PATH",
                    help="корпус обучения (можно повторять); по умолчанию — реестр "
                         "TRAINING_CORPORA")
    ap.add_argument("--verify", default=None, help="проверить готовый набор и выйти")
    ap.add_argument("--force", action="store_true",
                    help="перезаписать существующий файл K2 (не наборы ревизии)")
    ap.add_argument("--plan", action="store_true", help="показать план и выйти")
    args = ap.parse_args(argv)
    if not args.corpus:
        args.corpus = [f"{role}={path}" for role, path in TRAINING_CORPORA.items()]
    args.corpus = [tuple(spec.split("=", 1)) for spec in args.corpus]
    return args


def print_plan(args) -> int:
    print("S3t — сборка компоненты K2 (набор вне обучающего распределения)")
    print(f"  источник: {args.source}")
    print(f"  жанр: официально-аналитическая проза (не Википедия, не домен ML/AI)")
    print(f"  нарезка: блоки между пустыми строками, переносы снимаются")
    print(f"  фильтры: ≥{PROSE_MIN_CHARS} символов, ≥{PROSE_MIN_SENTENCES} концов "
          f"предложений, доля букв ≥{PROSE_MIN_ALPHA_SHARE}, "
          f"кириллица ≥{CYRILLIC_MIN_SHARE}, склейка ≤{OCR_GLUE_MAX_SHARE}, "
          f"≥{MIN_DOC_TOKENS} токенов")
    print(f"  отбор: {args.docs} документов, сид {args.seed}, "
          f"окно проверки чистоты {args.scan_pool} кандидатов")
    print(f"  корпуса обучения ({len(args.corpus)}): "
          + ", ".join(f"{role}" for role, _ in args.corpus))
    print(f"  запись: {args.out} (наборы ревизии не трогаются)")
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
