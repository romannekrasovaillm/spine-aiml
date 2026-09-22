#!/usr/bin/env python3
"""Проверка ADR-025 для S3an: измерительные наборы не входят в обучающий набор.

**Зачем отдельный страж, а не `check_eval_leakage.py`.** Тот ловит утечку
*задачи* eval в *промпты* train (гейт B — точное равенство нормализованного
текста) и работает с jsonl-набором задач. Здесь предмет другой: K1/K2/домен —
это **документы PPL-прибора** (`general_eval_v3.txt`, `general_eval_k2.txt`,
`domain_eval_v2.txt`), и вопрос ставится не «совпал ли текст единицы», а
«не лежит ли документ замера внутри обучающего примера». Разница
существенная: ответ инструмента `<tool_response>` дословно цитирует определения
библиотеки концептов, а домен-набор собран из той же библиотеки — совпадение
целиком в примере точным равенством единиц не поймается.

Два уровня:

* **ГЕЙТ — вхождение документа в пример.** Нормализованный документ замера
  (lower + схлопнутые пробелы, длина ≥ ``--min-doc``) ищется как подстрока в
  нормализованном тексте каждого сообщения обучающего примера. Любое вхождение
  — нарушение: документ замера присутствует в обучении.
* **INFO — 12-граммы.** Доля документов, делящих с обучением хотя бы один
  12-грамм. Гейтом не является (нижняя граница near-duplicate), печатается
  всегда, чтобы «зелено по гейту» не читалось как «пересечений нет вообще».

**Два рода обучающего входа** (S3ao). Обучение — не только jsonl-набор задач:
CPT-корпуса (``cpt_corpus_*``, ``general_replay_ru``) в jsonl не лежат, а «документ
замера внутри обучения» — вопрос ко всему обучению. Поэтому:

* ``--train`` — jsonl с ``messages``; единица = **сообщение** (не склейка: см.
  ``train_units``);
* ``--train-raw`` — сырой текст; единица = **блок** (разрез ``\\n\\n``, как режет
  реплей-корпус ``build_cpt_v12r_mix.py``) **и склейка соседних блоков** — пара
  закрывает случай, когда документ замера лёг на границу блоков. Без пары гейт
  молча пропускал бы такое вхождение: блоки режутся по источнику, а не по нашему
  набору.

Род входа не меняет ни нормализацию, ни n-грамм: иначе числа разных корпусов
были бы несопоставимы, а «ноль» — не ноль.

Честный результат важнее зелёного: найденные вхождения перечисляются поимённо
(набор, индекс документа, начало документа, единица), а не сворачиваются в счётчик.

Коды возврата::

    0 — вхождений нет
    1 — вхождение: список нарушений в stdout
    2 — NOT-VERIFIED: вход отсутствует/нечитаем — не зелёный

Запуск::

    python3 tools/check_measurement_overlap.py \\
        --train datasets/sft_train_v13_fixed.jsonl \\
        --set K1:datasets/general_eval_v3.txt --set K2:datasets/general_eval_k2.txt \\
        --set DOMAIN:datasets/domain_eval_v2.txt

    # S3ao: домен v3 против всего обучения, включая сырые CPT-корпуса
    python3 tools/check_measurement_overlap.py \\
        --train SFT13:datasets/sft_train_v13_fixed.jsonl \\
        --train-raw CPT10.1:datasets/cpt_corpus_v10.1.txt \\
        --train-raw REPLAY:datasets/general_replay_ru.txt \\
        --set DOMAIN3:datasets/domain_eval_v3.txt --json runs/.../overlap.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Порог длины документа замера — **тот же, что у прибора**: `ppl_probe.py:231`
#: (`len(d.strip()) > 50`). Ставить свой порог значило бы судить не тот набор,
#: который прибор читает.
MIN_DOC = 51

#: Длина n-грамма для INFO-сигнала — как в `check_eval_leakage.py` и
#: `audit_eval_leakage_calib.py`: методика одна, числа сопоставимы.
SHINGLE = 12

#: Разделитель единиц CPT-корпусов: ровно то, чем режет `pretokenize_v9.py`
#: (`docs = [d.strip() for d in text.split("\n---\n")]`). У реплей-корпуса
#: разделитель другой (`\n\n`), поэтому единицы выдаются для обоих разрезов.
DOC_SEP_RAW = "\n---\n"

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub(" ", text.lower()).strip()


def load_documents(path: Path) -> list[str]:
    """Документы замера — **как их читает прибор**: разделитель ровно ``\\n---\\n``.

    Это не деталь стиля. ``ppl_probe.py`` (и дословно повторяющий его
    ``laguna_pipeline_v8.py``) режет набор именно по ``\\n---\\n`` с фильтром
    ``len > 50``. Разбивка по пустой строке разрезала бы документ на его же
    разделы (``## Определение``, ``## Мотивация``…), и проверка мерила бы не тот
    предмет: обрывки разделов — это шаблонные строки, они находятся подстрокой
    где угодно и дают ложные «вхождения». Первая редакция этого стража так и
    ошибалась: 62 «нарушения» по домену оказались разделами документов, а не
    документами (200, а не 1625).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return [d.strip() for d in text.split("\n---\n") if len(d.strip()) > 50]


def train_units(rec: dict) -> list[str]:
    """Единицы обучающего примера: каждое сообщение отдельно.

    Не склейка всех сообщений: склейка дала бы вхождение «через границу ролей»,
    которого в обучении нет (роли разделены шаблоном чата).
    """
    out: list[str] = []
    for m in rec.get("messages", []):
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            out.append(c)
    return out


def iter_jsonl_units(path: Path) -> Iterator[tuple[int, str, str]]:
    """Единицы jsonl-набора: ``(номер примера, «message», текст сообщения)``."""
    for idx, line in enumerate(path.open(encoding="utf-8", errors="replace"), start=1):
        if not line.strip():
            continue
        rec = json.loads(line)
        for unit in train_units(rec):
            yield idx, "message", unit


def iter_raw_units(path: Path, max_unit_chars: int, stats: dict) -> Iterator[tuple[int, str, str]]:
    """Единицы сырого текста: ``(номер, род единицы, текст)`` — **два разреза сразу**.

    Разрез корпуса — не деталь: у каждого корпуса он свой, и задан он **потребителем**,
    а не нами. ``pretokenize_v9.py`` (CPT) режет ``cpt_corpus_*`` по ``\\n---\\n`` и
    кодирует каждую единицу отдельно; ``build_cpt_v12r_mix.py`` (реплей) режет
    ``general_replay_ru.txt`` по ``\\n\\n``. Гейт, взявший один разрез за оба, мерил бы
    не тот предмет: документ замера, целиком лежащий внутри единицы потребителя,
    мог бы не найтись ни в одной единице гейта.

    Поэтому единицы выдаются для **обоих** разрезов — ``\\n---\\n`` (``section``) и
    ``\\n\\n`` (``block``), — и для каждого вдобавок **склейка соседних** единиц
    (``*-pair``, номер = номер старшей): документ замера мог лечь на границу, а
    границы заданы источником, а не нашим набором. Склейка одной пары закрывает
    документ через одну границу — этого достаточно, потому что документы набора
    ограничены по длине (``build_domain_eval_v3.py --doc-max``), а единицы CPT
    короче: вложение документа в склейку через две границы требовало бы единиц
    короче пары символов.

    Стоит это немного: ``section`` крупнее ``block``, и на CPT-корпусе добавка —
    примерно пятая часть уже перебираемых единиц.

    Единица длиннее ``max_unit_chars`` пропускается и считается в
    ``stats["units_skipped_too_long"]``: это предохранитель от файла без единого
    разделителя (тогда «единицей» оказался бы весь корпус, и шинглы по нему
    съели бы память). Пропуск называется числом, а не умалчивается.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    sections = text.split(DOC_SEP_RAW)
    del text
    #: ``\\n---\\n`` в файле нет — значит и «единиц» этого разреза нет: разрез по нему
    #: дал бы один кусок во весь корпус. Это не единица потребителя, а отсутствие
    #: разреза; подставить сюда корпус целиком значило бы считать шинглы по сотням
    #: мегабайт ради проверки, которую делают абзацы. **Абзацы при этом считаются
    #: всегда** — иначе у корпуса без ``---`` не осталось бы ни одной единицы.
    emit_sections = len(sections) > 1
    prev_section: str | None = None
    prev_block: str | None = None
    ns = nb = skipped = 0
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        if emit_sections:
            if len(sec) > max_unit_chars:
                skipped += 1
            else:
                i = ns
                ns += 1
                if prev_section is not None:
                    yield i, "section-pair", prev_section + "\n\n" + sec
                yield i, "section", sec
                prev_section = sec
        for blk in sec.split("\n\n"):
            blk = blk.strip()
            if not blk or len(blk) > max_unit_chars:
                skipped += 1 if blk else 0
                continue
            j = nb
            nb += 1
            if prev_block is not None:
                yield j, "block-pair", prev_block + "\n\n" + blk
            yield j, "block", blk
            prev_block = blk
    stats["units_skipped_too_long"] += skipped


def shingles(text: str, n: int = SHINGLE) -> set[str]:
    words = text.split()
    if len(words) < n:
        return set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


#: База и маска роллингового хеша n-граммов. Хеш — 64-битный: коллизия
#: теоретически возможна, поэтому по хешу отбираются ТОЛЬКО кандидаты, а решение
#: принимает точное вхождение подстроки (гейт обязан быть точным, а не быстрым).
_HASH_BASE = 1_000_003
_HASH_MASK = (1 << 64) - 1


def shingle_hashes(text: str, n: int = SHINGLE) -> set[int]:
    """Хеши n-граммов по словам — без склейки строк (она и была узким местом)."""
    words = text.split()
    if len(words) < n:
        return set()
    ids = [hash(w) & _HASH_MASK for w in words]
    b = pow(_HASH_BASE, n - 1, 1 << 64)
    h = 0
    for i in range(n):
        h = (h * _HASH_BASE + ids[i]) & _HASH_MASK
    out = {h}
    for i in range(n, len(ids)):
        h = ((h - ids[i - n] * b) * _HASH_BASE + ids[i]) & _HASH_MASK
        out.add(h)
    return out


def shared_windows(unit_words: list[str], common: set[int], n: int = SHINGLE) -> Iterator[str]:
    """Сами окна, а не только их хеши: по ним и решается «формула или определение».

    ADR-025 п.6 требует для доменного набора не «число окон», а проверку того, что
    совпавшие окна — формульные обороты, а не содержательные фрагменты. Проверка
    числом без текста окон недоказуема: «совпало 40 окон» одинаково верно и для
    `group relative policy optimization`, и для целой цитаты определения. Поэтому
    инструмент умеет выкладывать сами окна — с частотой, по которой видно, что
    перед нами шаблон (одна и та же строка в сотнях единиц), а не фрагмент ответа
    (уникальная строка).
    """
    if len(unit_words) < n:
        return
    ids = [hash(w) & _HASH_MASK for w in unit_words]
    b = pow(_HASH_BASE, n - 1, 1 << 64)
    h = 0
    for i in range(n):
        h = (h * _HASH_BASE + ids[i]) & _HASH_MASK
    if h in common:
        yield " ".join(unit_words[:n])
    for i in range(n, len(ids)):
        h = ((h - ids[i - n] * b) * _HASH_BASE + ids[i]) & _HASH_MASK
        if h in common:
            yield " ".join(unit_words[i - n + 1:i + 1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train", action="append", default=[], metavar="PATH",
                    help="обучающий набор (messages jsonl); можно несколько раз; "
                         "имя берётся из имени файла (или NAME:PATH)")
    ap.add_argument("--train-raw", action="append", default=[], metavar="NAME:PATH",
                    help="обучающий корпус сырым текстом (блоки по \\n\\n и их пары); "
                         "можно несколько раз")
    ap.add_argument("--set", action="append", default=[], metavar="NAME:PATH",
                    help="измерительный набор; можно несколько раз")
    ap.add_argument("--max-unit-chars", type=int, default=2_000_000,
                    help="предохранитель: единица длиннее — пропускается (числом)")
    ap.add_argument("--dump-shared", type=int, default=0, metavar="N",
                    help="собрать до N самых частых общих 12-граммовых окон (ADR-025 п.6: "
                         "доказать, что совпадения формульные, а не определения)")
    ap.add_argument("--limit-report", type=int, default=10, help="сколько нарушений печатать")
    ap.add_argument("--json", metavar="PATH",
                    help="куда записать машинный отчёт (для evidence); stdout не меняется")
    a = ap.parse_args()

    trains: list[tuple[str, Path, str]] = []
    for spec in a.train or []:
        # Имя — по имени файла; «NAME:PATH» принимается тоже, но только если по
        # обе стороны двоеточия действительно файл (иначе двоеточие в пути
        # считалось бы разделителем имени и было бы молча съедено).
        name, _, raw = spec.partition(":")
        if name and raw and Path(raw).is_file() and not Path(spec).is_file():
            pass
        else:
            name, raw = Path(spec).name, spec
        p = Path(raw)
        if not p.is_file():
            print(f"нет обучающего набора: {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        trains.append((name, p, "jsonl"))
    for spec in a.train_raw:
        name, _, raw = spec.partition(":")
        if not name or not raw:
            print(f"не разобран --train-raw: {spec!r} (ожидается NAME:PATH)", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        p = Path(raw)
        if not p.is_file():
            print(f"нет обучающего корпуса {name}: {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        trains.append((name, p, "raw"))
    if not trains:
        print("не задан ни один обучающий вход (--train / --train-raw)", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    sets: list[tuple[str, Path, list[str]]] = []
    for spec in a.set:
        name, _, raw = spec.partition(":")
        p = Path(raw)
        if not p.is_file():
            print(f"нет набора {name}: {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        docs = load_documents(p)
        sets.append((name, p, docs))
    if not sets:
        print("не задан ни один --set", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    print("== ADR-025: измерительные наборы против обучающего набора ==")
    for name, p, kind in trains:
        print(f"  обучение [{kind}]: {name} — {p}")
    for name, p, docs in sets:
        print(f"  замер {name}: {p} — документов: {len(docs)}")

    # ── Один проход по обучению: кандидаты по хешам 12-граммов ───────────────
    # Прямой перебор «документ × единица» — сотни миллионов поисков подстроки
    # (десятки минут), а поиск документа сразу по всему стогу — сотни гигабайт
    # сканирования. Поэтому: (1) хеши 12-граммов документов — множество
    # небольших чисел; (2) один проход по словам обучения с роллинговым хешем
    # отбирает ЕДИНИЦЫ-КАНДИДАТЫ; (3) решение по кандидату принимает точное
    # вхождение подстроки. Хеш ускоряет, но не судит.
    needles: dict[str, list[tuple[int, str, str, set[int]]]] = {}
    for name, _, docs in sets:
        needles[name] = [(i, normalize(d), d[:90].replace("\n", " "), shingle_hashes(normalize(d)))
                         for i, d in enumerate(docs)]
        needles[name] = [n for n in needles[name] if n[1]]
    doc_hashes: dict[tuple[str, int], set[int]] = {
        (name, i): hs for name, lst in needles.items() for i, _, _, hs in lst}
    #: Объединение всех хешей документов — грубый фильтр ценой ОДНОГО пересечения
    #: множеств (в C), а не перебора документов на каждую единицу. Если общего нет,
    #: документ в единицу не входит и точная проверка не нужна вовсе.
    doc_all: set[int] = set().union(*doc_hashes.values()) if doc_hashes else set()
    hash_to_docs: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for key, hs in doc_hashes.items():
        for h in hs:
            hash_to_docs[h].append(key)
    #: пересечение множеств «документ → единицы-кандидаты» (в чистом случае пусто).
    #: Кандидаты дедуплицируются: одна и та же единица приходит на каждый общий
    #: 12-грамм, и без дедупликации текст единицы хранился бы столько раз, сколько
    #: у неё общих окон (на CPT-корпусах это те же сотни мегабайт, только в списках).
    candidates: dict[tuple[str, int], list[tuple[str, int, str]]] = defaultdict(list)
    cand_keys: set[tuple[str, int, str, int, str]] = set()
    unit_text: dict[tuple[str, int, str], str] = {}
    info_docs: dict[str, set[int]] = defaultdict(set)
    info_docs_by_train: dict[tuple[str, str], set[int]] = defaultdict(set)
    units_seen = 0
    checked = 0
    unit_stats: dict = {"units_skipped_too_long": 0}
    #: Счётчик общих окон (только при --dump-shared). Ограничен сверху: у набора из
    #: 200 документов против корпуса в сотни МБ общих окон может оказаться много, и
    #: «сколько их всего» здесь не нужно — нужны самые частые, по ним и видно шаблон.
    dump_counter: Counter[str] = Counter()
    dump_capped = False
    DUMP_CAP = 500_000

    for tname, tpath, tkind in trains:
        gen = (iter_jsonl_units(tpath) if tkind == "jsonl"
               else iter_raw_units(tpath, a.max_unit_chars, unit_stats))
        for idx, uk, unit in gen:
            if uk == "message":
                checked += 1
            nu = normalize(unit)
            if not nu:
                continue
            units_seen += 1
            uh = shingle_hashes(nu)
            if not uh:
                continue
            common = uh & doc_all
            if not common:
                continue
            for h in common:                       # только общие — их единицы
                for key in hash_to_docs[h]:
                    info_docs[key[0]].add(key[1])
                    info_docs_by_train[(tname, key[0])].add(key[1])
                    ck = (key[0], key[1], tname, idx, uk)
                    if ck in cand_keys:
                        continue
                    cand_keys.add(ck)
                    unit_text[(tname, idx, uk)] = nu
                    candidates[key].append((tname, idx, uk))
            if a.dump_shared and common:
                # Окна собираются по единицам, а не по документам: частота окна —
                # это сколько раз оно встретилось в КОРПУСЕ, и именно она отличает
                # шаблон от цитаты.
                for w in shared_windows(nu.split(), common):
                    if w in dump_counter or len(dump_counter) < DUMP_CAP:
                        dump_counter[w] += 1
                    else:
                        dump_capped = True

    hits: list[dict] = []
    for name, _, docs in sets:
        for di, nd, head, _ in needles[name]:
            for tname, idx, uk in candidates.get((name, di), ()):   # точное решение
                if nd in unit_text[(tname, idx, uk)]:
                    hits.append({"set": name, "doc_index": di, "train": tname,
                                 "unit": idx, "unit_kind": uk,
                                 "doc_head": head,
                                 "unit_chars": len(unit_text[(tname, idx, uk)])})
                    break

    print(f"\nпроверено обучающих единиц: {units_seen} (сообщений: {checked})")
    if unit_stats["units_skipped_too_long"]:
        print(f"  пропущено единиц длиннее {a.max_unit_chars} символов: "
              f"{unit_stats['units_skipped_too_long']}")

    def _unit_label(h: dict) -> str:
        kind, i = h["unit_kind"], h["unit"]
        if kind == "message":
            return f"{h['train']}: пример {i}"
        if kind == "section":
            return f"{h['train']}: единица {i} (разрез \\n---\\n)"
        if kind == "section-pair":
            return f"{h['train']}: единицы {i - 1}+{i} (разрез \\n---\\n)"
        if kind == "block":
            return f"{h['train']}: абзац {i} (разрез \\n\\n)"
        return f"{h['train']}: абзацы {i - 1}+{i} (разрез \\n\\n)"

    per_train: dict[str, dict[str, int]] = {}
    for name, _, docs in sets:
        n_hit = sum(1 for h in hits if h["set"] == name)
        cand = sum(len(v) for k, v in candidates.items() if k[0] == name)
        print(f"\n{name}:")
        print(f"  гейт (документ замера внутри обучающей единицы): нарушений {n_hit}")
        for h in [x for x in hits if x["set"] == name][:a.limit_report]:
            print(f"    - документ #{h['doc_index']} в {_unit_label(h)}: «{h['doc_head']}…»")
        pct = (len(info_docs[name]) / len(docs) * 100) if docs else 0.0
        print(f"  INFO (12-граммы, общий хотя бы один): документов {len(info_docs[name])} "
              f"из {len(docs)} ({pct:.1f} %) — не гейт; кандидатов на точную проверку: {cand}")
        per_train[name] = {}
        for tname, _, _ in trains:
            k = len(info_docs_by_train[(tname, name)])
            per_train[name][tname] = k
            print(f"    против {tname}: {k} из {len(docs)} "
                  f"({(k / len(docs) * 100) if docs else 0.0:.1f} %)")

    report = {
        "window_size": SHINGLE,
        "min_doc_chars": MIN_DOC,
        "train": [{"name": n, "path": str(p), "kind": k} for n, p, k in trains],
        "sets": {n: {"path": str(p), "documents": len(d)}
                 for n, p, d in sets},
        "units_seen": units_seen,
        "units_skipped_too_long": unit_stats["units_skipped_too_long"],
        "messages_seen": checked,
        "gates": {n: sum(1 for h in hits if h["set"] == n) for n, _, _ in sets},
        "ngram_docs_shared": {n: len(info_docs[n]) for n, _, _ in sets},
        "ngram_docs_shared_by_train": per_train,
        "hits": hits,
        "verdict": "FAIL" if hits else "OK",
    }
    if a.dump_shared:
        report["shared_windows"] = {
            "distinct": len(dump_counter),
            "capped": dump_capped,
            "total_occurrences": sum(dump_counter.values()),
            "top": [{"window": w, "occurrences_in_train": c}
                    for w, c in dump_counter.most_common(a.dump_shared)],
        }
        print(f"\nобщих 12-граммовых окон: различных {len(dump_counter)} "
              f"(всего вхождений {sum(dump_counter.values())}"
              f"{', список обрезан' if dump_capped else ''}); "
              f"самые частые — в машинном отчёте")
    if a.json:
        out = Path(a.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"\nмашинный отчёт: {out}")

    if hits:
        print(f"\nMEASUREMENT OVERLAP: нарушений {len(hits)} — измерительный набор присутствует в обучении")
        return EXIT_FAIL
    print("\nMEASUREMENT OVERLAP OK: документов измерительных наборов в обучении нет")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
