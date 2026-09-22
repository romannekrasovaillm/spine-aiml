#!/usr/bin/env python3
"""analyze_loop_origin.py — откуда петли SFT-состояния: выученное или дегенерация.

Вопрос. Состояние SFT (шаг 21 500) даёт петли в 17.31 % ходов при штатном режиме
(greedy с запретом повторов 4-грамм, S3aq), у CPT-финала — 0.96 %. Смена режима
декодирования петлю не снимает и усиливает (усечено 0.7308 против 0.4423, петель
0.6442 против 0.1731), то есть источник — веса, а не протокол. Но «в весах» — это
два разных механизма, и лечатся они противоположно:

* **воспроизведение выученного** — модель переписывает фрагмент, лежащий в обучающем
  наборе и кратно в нём повторённый (57.57 % строк набора — дубликаты, кратности
  {1,2,3,6}). Лечение — дедупликация набора;
* **собственная дегенерация** — цикл n-грамм, которого в наборе нет ни в дубликатах,
  ни в уникальных строках. Лечение — режим обучения; дедупликация не уберёт ничего.

Как различается. Петля размечается в **словах** хода модели: блок длиной ≥ `MIN_N`
слов, встретившийся в ходе ≥ `MIN_COUNT` раз (период — расстояние между первыми
двумя вхождениями, у тандема равный длине блока). Для каждого блока ищется источник:

* `dataset_verbatim` — блок целиком лежит в assistant-тексте строки набора;
* `dataset_fragment` — в наборе лежит фрагмент блока ≥ `MIN_N` слов (длиннейшее общее
  подвыражение по словам, `difflib`), но не блок целиком;
* `prompt` — блок лежит в промпте самих проб (петля-копия входа);
* `cpt` — блок лежит в CPT-корпусе стадии (вторичная ось: заучено ДО SFT);
* `none` — нигде: цикл рождён весами.

Метрика в двух ярусах, чтобы «петля» не смешивала случайный повтор фразы с затяжным
циклом: `mild` — буквальный критерий (≥ 8 слов, ≥ 2 раз), `strong` — затяжная петля
(≥ `STRONG_LEN` слов ИЛИ ≥ `STRONG_COUNT` повторов ИЛИ период ≤ `TANDEM_PERIOD`).

Контроль обязателен и берётся **из тех же генераций**: уникальные 8-граммы той же
генерации (рука `same_generation`) и того же сегмента (рука `same_region`). Без
контроля «нашлось в наборе» не значит ничего: русская фраза из восьми частых слов
найдётся в 292 МБ текста сама по себе, и доля находок у петель обязана сравниваться
с долей находок у произвольной речи, а не читаться порознь (приём S3al).

Проход по набору — **один**: в память кладутся счётчики целевых 8-грамм и хеши
дубликатов, полные тексты не собираются. Набор, карточка, тензор и исторические
evidence — только чтение (ADR-023 п.9).

Коды возврата::

    0 — отчёт собран
    1 — отказ: вход не тот (sha набора не совпал с ожидаемым)
    2 — NOT-VERIFIED: мерить нечего (нет отчёта проб, нет набора, петель нет вовсе)

Запуск::

    python3 tools/analyze_loop_origin.py \\
        --report runs/s3aq-budget-8192-20260920/format_wide_8192.json \\
        --dataset datasets/sft_train_v13_fixed.jsonl \\
        --out evidence/sft-loop-origin.json
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
import shlex
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))

#: Разметка сегментов хода — **импортом**, не копией: «где петля» (в рассуждении,
#: в вызове, в ответе) обязано считаться там же, где считались языковые чтения.
import probe_language_split as LS  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Критерий петли — объявлен ДО прогона, а не подобран по данным: блок ≥ 8 слов,
#: повторённый в ходе ≥ 2 раз. Восемь слов — тот же порядок, что у порога прибора
#: S3aj/S3ak (8 повторов 4-граммы), но единица здесь — блок, а не 4-грамма.
MIN_N = 8
MIN_COUNT = 2

#: Ярус «затяжная петля»: чем блок длиннее, чаще и периодичнее, тем меньше шансов,
#: что повтор случаен. Границы объявлены до прогона.
STRONG_LEN = 16
STRONG_COUNT = 3
TANDEM_PERIOD = 4

#: Контроль: сколько уникальных 8-грамм брать на одну генерацию с петлями.
CONTROLS_PER_GENERATION = 3

#: Потолки выгрузки. Полные списки в отчёт не влезают; у счётчиков, обрезанных
#: потолком, это видно (`capped`), а не выглядит как «больше не нашлось».
MAX_CANDIDATE_ROWS = 40
MAX_ROW_POOL = 4000
MAX_SRC_ROWS = 400
MAX_OCCURRENCES_PER_GRAM = 400
MAX_HITS_PER_GRAM = 400
MAX_EXAMPLES = 6
HEAD_CHARS = 160

#: Сегменты хода. `all` — ход целиком (то же чтение, что у генераций S3ak),
#: остальные — по разметке прибора `probe_language_split.split_segments`.
REGIONS = ["all", "think", "tool_call", "answer", "tool_response"]

#: Ранг сегмента при сведении одинаковых блоков: петля внутри рассуждения находится
#: и в чтении «ход целиком», и в чтении «рассуждение», и без ранга её сегмент
#: определялся бы порядком перебора, а не тем, где она лежит. `all` — самый общий,
#: поэтому младший: он остаётся только у блоков, которых в сегментах нет вовсе.
REGION_RANK = {"all": 0, "tool_response": 1, "tool_call": 2, "answer": 3, "think": 4}

#: Правила вердикта. Объявлены ДО прогона; сработавшее и несработавшее правило
#: остаются в отчёте, иначе отрицательный результат выглядел бы как отсутствие вопроса.
RULES = {
    "origin": {
        "rule": "доля блоков, найденных в наборе (dataset_verbatim + dataset_fragment), "
                "против той же доли у контроля тех же генераций: ≥ 0.5 при разрыве "
                "≥ 0.20 → memorized_prevails; разрыв ≤ 0.05 → own_degeneration; "
                "иначе mixed",
        "why": "абсолютная доля без контроля ничего не значит: частая русская фраза "
               "находится в наборе сама по себе, и без базы «нашлось» неотличимо от "
               "«нашлось случайно»",
    },
    "multiplicity": {
        "rule": "вердикт — только по НЕТРИВИАЛЬНЫМ блокам (доля букв ≥ 0.5); строки-источники — "
                "несущие самый редкий 8-грамм блока. Если строк у петель ≥ 20: "
                "их распределение кратности против кратности строк контроля проверяется "
                "χ² (df=3); p < 0.05 и перекос в кратность ≥ 3 → shift_to_repeated; иначе "
                "no_shift. Меньше 20 строк — underpowered (вердикта нет)",
        "why": "кратность — свойство СТРОКИ набора, а не петли; «- - - - - - - -» находит "
               "себе пару в любом тексте, и без исключения пунктуации таблица мерила бы "
               "тривиальность узора, а не источник",
    },
}


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(p) -> str:
    try:
        return str(Path(p).resolve().relative_to(CASE))
    except ValueError:
        return str(p)


def read_input(spec: str) -> dict | None:
    """Вход прибора: файл или `git:<ветка>:<путь>`.

    Отчёты проб лежат не во всех ветвях (S3aq-отчёт живёт в ветке
    `arch/laguna-control-arms`), а копировать чужие артефакты в свою ветвь запрещено
    (ADR-023 п.9). Поэтому вход берётся **по ссылке на ветку**, а в отчёт попадают
    и ссылка, и sha256 содержимого: число привязано к версии файла, а не к пути
    на диске конкретного дерева.
    """
    if spec.startswith("git:"):
        rest = spec[4:]
        ref, _, path = rest.partition(":")
        if not ref or not path:
            note(f"ОТКАЗ: разбор git-ссылки не удался: {spec}")
            return None
        import subprocess
        r = subprocess.run(["git", "-C", str(CASE), "show", f"{ref}:{path}"],
                           capture_output=True)
        if r.returncode != 0:
            note(f"NOT-VERIFIED: git show {ref}:{path} не удался: "
                 f"{r.stderr.decode('utf-8', 'replace').strip()[:200]}")
            return None
        return {"spec": spec, "ref": ref, "path": path, "name": Path(path).name,
                "bytes": len(r.stdout),
                "sha256": hashlib.sha256(r.stdout).hexdigest(),
                "text": r.stdout.decode("utf-8")}
    p = Path(spec)
    if not p.exists():
        note(f"NOT-VERIFIED: нет файла {spec}")
        return None
    raw = p.read_bytes()
    return {"spec": rel(p), "ref": None, "path": rel(p), "name": p.name, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "text": raw.decode("utf-8")}


RE_WS = re.compile(r"\s+")


def norm_text(text: str) -> str:
    """Нормализация поиска: регистр вниз, пробелы в один.

    Регистр снимается намеренно: «Итеративное обучение» и «итеративное обучение» —
    один выученный фрагмент, а не два разных. Цена решения названа в `assumptions`.
    """
    return RE_WS.sub(" ", (text or "").lower()).strip()


def norm_text_ws(text: str) -> str:
    """Пробелы в один, регистр СОХРАНЁН — уровень «вхождение с точностью до пробелов».

    Три уровня поиска, названные в вопросе, различаются строгостью: точное вхождение
    (байт в байт) ⊂ вхождение с точностью до пробелов ⊂ вхождение без регистра.
    Основной поиск идёт по самому широкому уровню, а этот уровень досчитывается по
    найденным кандидатам: он отделяет «нашлось дословно» от «нашлось без регистра».
    """
    return RE_WS.sub(" ", (text or "")).strip()


def norm_words(text: str) -> list[str]:
    return norm_text(text).split()


def grams_of(text: str, n: int = MIN_N) -> set[tuple]:
    w = text.split()
    return {tuple(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


# ───────────────────────── разметка петель ─────────────────────────

def mark_loops(words: list[str], min_n: int = MIN_N, max_blocks: int = 2000) -> list[dict]:
    """Повторяющиеся блоки в ходе: длина, период, число повторов, ярус.

    Механика. Индексируются все 8-граммы (ключ — кортеж слов), затем каждая пара
    соседних вхождений расширяется в обе стороны до максимального общего блока.
    Кандидаты сортируются по длине, и блок, пересекающийся с уже принятым, не
    берётся: два перекрывающихся описания одной петли — не две петли. `copies` —
    сколько раз блок повторяется подряд с шагом `period`, `span_words` — сколько слов
    хода занимает вся петля.
    """
    n = len(words)
    if n < min_n * MIN_COUNT:
        return []
    idx: dict[tuple, list[int]] = defaultdict(list)
    for i in range(n - min_n + 1):
        idx[tuple(words[i:i + min_n])].append(i)
    cands: list[tuple[int, int, int]] = []
    for pos in idx.values():
        if len(pos) < 2:
            continue
        for a, b in zip(pos, pos[1:]):
            length = min_n
            while (b + length < n and a + length < b
                   and words[a + length] == words[b + length]):
                length += 1
            s, t = a, b
            while s > 0 and t - 1 > s and words[s - 1] == words[t - 1]:
                s -= 1
                t -= 1
            cands.append((s, length, b - a))
    cands.sort(key=lambda x: (-x[1], x[0]))
    kept: list[tuple[int, int]] = []
    loops: list[dict] = []
    for s, length, period in cands:
        if any(not (s + length <= ks or s >= ks + kl) for ks, kl in kept):
            continue
        kept.append((s, length))
        block = words[s:s + length]
        #: Копии считаются подряд с шагом `period`: у тандема (period ≤ длины блока)
        #: копии перекрываются, и «сколько раз блок встретился где угодно» дало бы
        #: число, растущее от длины хода, а не от того, как долго петля тянется.
        copies = 1
        while (s + copies * period + length <= n
               and words[s + copies * period: s + copies * period + length] == block):
            copies += 1
        span = (copies - 1) * period + length
        loops.append({
            "start": s, "length": length, "period": period, "copies": copies,
            "span_words": span,
            "tandem": period <= TANDEM_PERIOD,
            "strong": (span >= STRONG_LEN or copies >= STRONG_COUNT
                       or period <= TANDEM_PERIOD),
            "text": " ".join(block),
        })
        if len(loops) >= max_blocks:
            break
    return loops


def region_texts(response: str) -> dict[str, str]:
    """Текст хода по сегментам — разметкой прибора, не своей."""
    seg = LS.split_segments(response or "")
    return {"all": response or "",
            "think": seg.get("think") or "",
            "tool_call": seg.get("tool_call") or "",
            "answer": seg.get("answer") or "",
            "tool_response": seg.get("tool_response") or ""}


def loops_of_probe(response: str, n_tokens: int) -> list[dict]:
    """Петли пробы по всем сегментам; позиция — доля сегмента и оценка в токенах."""
    out: list[dict] = []
    for region, text in region_texts(response).items():
        words = norm_words(text)
        if len(words) < MIN_N * MIN_COUNT:
            continue
        #: Те же слова с сохранённым регистром — для уровня «точное вхождение»:
        #: нормализация слов и регистра иначе стёрла бы разницу между уровнями.
        raw = RE_WS.sub(" ", text).split()
        for lp in mark_loops(words):
            lp = dict(lp)
            lp["text_raw"] = (" ".join(raw[lp["start"]:lp["start"] + lp["length"]])
                              if len(raw) == len(words) else lp["text"])
            lp["region"] = region
            lp["region_words"] = len(words)
            lp["position_frac"] = round(lp["start"] / max(1, len(words)), 4)
            lp["token_estimate"] = int(round(lp["position_frac"] * max(0, n_tokens)))
            out.append(lp)
    return out


# ───────────────────────── поиск источника ─────────────────────────

def build_queries(blocks: list[dict], controls: list[dict], n: int = MIN_N) -> set[tuple]:
    """Целевые 8-граммы: **все** 8-граммы блоков петель и контрольных юнитов.

    Все, а не «голова/хвост»: «найден» ставится тогда и только тогда, когда в наборе
    лежит ЛЮБОЙ 8-словный кусок блока. Выборка головы и хвоста пропускала бы
    совпадение в середине и занижала бы долю выученного — то есть решала бы вопрос молча.
    """
    q: set[tuple] = set()
    for b in blocks:
        q |= grams_of(b["text"], n)
    for c in controls:
        q |= grams_of(c["text"], n)
    return q


def scan_jsonl_dataset(path: Path, queries: set[tuple], limit: int | None,
                       n: int = MIN_N) -> dict:
    """Один проход по набору: кратность строк и попадания целевых 8-грамм.

    Кратность считается по ключу карточки набора (ADR-033: `messages` →
    `[[role, content]]` → `json.dumps(sort_keys=True)` → sha256), чтобы «кратность 3»
    означала ровно то же, что в карточке набора и в манифесте прогона.
    """
    group_count: Counter = Counter()
    group_rows: dict[str, list[int]] = defaultdict(list)
    rows: list[dict] = []
    gram_hits: dict[tuple, dict] = defaultdict(
        lambda: {"rows": [], "occurrences": 0, "capped": False})
    n_examples = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            idx = len(rows)
            key = json.dumps([[m.get("role"), m.get("content") or ""]
                              for m in (rec.get("messages") or [])],
                             ensure_ascii=False, sort_keys=True)
            key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
            group_count[key_hash] += 1
            group_rows[key_hash].append(idx)
            words = norm_words(" ".join((m.get("content") or "")
                                        for m in (rec.get("messages") or [])
                                        if m.get("role") == "assistant"))
            rows.append({"words": len(words), "group": key_hash})
            for i in range(len(words) - n + 1):
                k = tuple(words[i:i + n])
                if k in queries:
                    hit = gram_hits[k]
                    hit["occurrences"] += 1
                    if not hit["rows"] or hit["rows"][-1] != idx:
                        if len(hit["rows"]) < MAX_HITS_PER_GRAM:
                            hit["rows"].append(idx)
                        else:
                            hit["capped"] = True
            n_examples += 1
            if limit is not None and n_examples >= limit:
                break
            if n_examples % 10000 == 0:
                note(f"  ... {n_examples} строк набора")
    for row in rows:
        row["mult"] = group_count[row["group"]]
    return {"rows": rows, "group_count": group_count, "group_rows": group_rows,
            "gram_hits": dict(gram_hits), "n_examples": n_examples}


def declared_shas(manifest: Path) -> set[str]:
    """sha256, объявленные манифестом микса: с ними сверяется ось `cpt`.

    Манифест (`datasets/cpt_corpus_v12r*.txt`) — единственное место, где назван
    ВХОД стадии CPT. Без сверки ось «нашлось в CPT» могла бы быть снята по любому
    файлу с похожим именем, и её число ничего не доказывало бы.
    """
    if not manifest.exists():
        return set()
    return set(re.findall(r"\b[0-9a-f]{64}\b", manifest.read_text(encoding="utf-8")))


def scan_plain_corpus(path: Path, queries: set[tuple], n: int = MIN_N) -> dict:
    """Попадания 8-грамм в текстовый корпус (CPT): существование и число вхождений.

    Чтение идёт куском по 16 МиБ, и хвост куска переносится в следующий: иначе
    разрезанное границей куска слово дало бы ложное «не нашлось».
    """
    hits: dict[tuple, int] = defaultdict(int)
    capped = False
    read = 0
    tail = ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        while True:
            chunk = f.read(1 << 24)
            if not chunk:
                break
            read += len(chunk)
            buf = tail + chunk
            cut = max(buf.rfind("\n"), buf.rfind(" "), buf.rfind("\t"))
            if cut <= 0:
                tail = buf
                if len(tail) > (1 << 20):
                    cut, tail = len(tail), ""
                else:
                    continue
            else:
                tail = buf[cut + 1:]
                buf = buf[:cut]
            words = norm_words(buf)
            for i in range(max(0, len(words) - n + 1)):
                k = tuple(words[i:i + n])
                if k in queries:
                    if hits[k] < MAX_OCCURRENCES_PER_GRAM:
                        hits[k] += 1
                    else:
                        capped = True
    return {"hits": dict(hits), "capped": capped, "bytes_read": read}


def lcs_words(block: list[str], row_words: list[str]) -> int:
    """Длина длиннейшего общего подвыражения (в словах) — «нашлось с точностью».

    `difflib.SequenceMatcher.find_longest_match` — это ровно «самый длинный кусок
    блока, лежащий в строке подряд», без порогов и эвристик.
    """
    if not block or not row_words:
        return 0
    m = difflib.SequenceMatcher(None, block, row_words, autojunk=False).find_longest_match(
        0, len(block), 0, len(row_words))
    return m.size


def scan_line_offsets(path: Path) -> list[int]:
    """Байтовые смещения строк набора — произвольный доступ без второго чтения целиком."""
    offs: list[int] = []
    pos = 0
    with open(path, "rb") as f:
        for line in f:
            offs.append(pos)
            pos += len(line)
    return offs


def read_assistant(path: Path, line_offsets: list[int], idx: int) -> str:
    with open(path, "rb") as f:
        f.seek(line_offsets[idx])
        rec = json.loads(f.readline().decode("utf-8"))
    return " ".join((m.get("content") or "") for m in (rec.get("messages") or [])
                    if m.get("role") == "assistant")


def source_rows(block_text: str, dataset: dict) -> list[int]:
    """Строки-источники: несущие **самый редкий** 8-грамм блока.

    Почему самый редкий, а не любой. «Строки, где встречается хоть один 8-грамм»
    у блока из пунктуации — это пол-набора, и кратность такого множества не про
    источник. Самый редкий грамм — самый разборчивый: он и есть то место, которое
    блок повторил. Отбор механический (min по числу строк) и одинаковый для петель
    и для контроля, поэтому сравнение остаётся сравнением.
    """
    grams = [g for g in sorted(grams_of(block_text)) if g in dataset["gram_hits"]]
    if not grams:
        return []
    rarest = min(grams, key=lambda g: len(dataset["gram_hits"][g]["rows"]))
    return dataset["gram_hits"][rarest]["rows"][:MAX_SRC_ROWS]


def classify(block_text: str, dataset: dict, dataset_path: Path,
             line_offsets: list[int], cpt_hits: dict, prompt_grams: set[tuple],
             block_raw: str | None = None) -> dict:
    """Класс происхождения блока: набор → промпт → CPT → нигде.

    «Набор» — assistant-текст строки (то, что модель обязана порождать), а не вся
    строка: фрагмент, лежащий в промпте или в ответе инструмента, выучен не как цель.
    Это названо в `assumptions`, а не спрятано.

    Кандидаты для LCS собираются по всем 8-граммам блока в детерминированном
    порядке (граммы отсортированы): иначе потолок `MAX_CANDIDATE_ROWS` резал бы
    разные множества от прогона к прогону, и отчёт не воспроизводился бы побайтово.
    """
    words = block_text.split()
    block_raw = block_raw or block_text
    out = {"dataset_verbatim": False, "dataset_fragment": False, "dataset_case_exact": False,
           "dataset_lcs": 0,
           "dataset_rows": [], "dataset_groups": 0, "candidate_rows": 0,
           "prompt": False, "cpt": False, "cpt_grams_found": 0,
           "cpt_grams_total": 0, "src_rows": 0, "src_mults": {},
           "trivial_text": False, "origin": "none"}
    grams = sorted(grams_of(block_text))
    pool: list[int] = []
    seen: set[int] = set()
    for g in grams:
        hit = dataset["gram_hits"].get(g)
        if not hit:
            continue
        for r in hit["rows"]:
            if r not in seen:
                seen.add(r)
                pool.append(r)
        if len(pool) >= MAX_ROW_POOL:
            break
    pool.sort()
    rows_seen = pool[:MAX_CANDIDATE_ROWS]
    out["candidate_rows"] = len(pool)
    if rows_seen:
        best = 0
        best_rows: list[int] = []
        for r in rows_seen:
            size = lcs_words(words, norm_words(read_assistant(dataset_path, line_offsets, r)))
            if size > best:
                best, best_rows = size, [r]
            elif size == best and size:
                best_rows.append(r)
        raw_block = norm_text_ws(block_raw)
        out["dataset_case_exact"] = any(
            raw_block and raw_block in norm_text_ws(read_assistant(dataset_path, line_offsets, r))
            for r in rows_seen)
        out["dataset_lcs"] = best
        out["dataset_verbatim"] = best >= len(words)
        out["dataset_fragment"] = best >= MIN_N
        if best >= MIN_N:
            out["dataset_rows"] = best_rows[:10]
            out["dataset_groups"] = len({dataset["rows"][r]["group"] for r in best_rows})
    src = source_rows(block_text, dataset)
    out["src_rows"] = len(src)
    out["src_mults"] = {str(k): v for k, v in sorted(
        Counter(dataset["rows"][r]["mult"] for r in src).items())}
    out["prompt"] = bool(prompt_grams & set(grams))
    letters = sum(1 for ch in block_text if ch.isalpha())
    nonspace = sum(1 for ch in block_text if not ch.isspace())
    out["trivial_text"] = bool(nonspace and letters / nonspace < 0.5)
    out["cpt_grams_total"] = len(grams)
    out["cpt_grams_found"] = sum(1 for g in grams if g in cpt_hits)
    out["cpt"] = out["cpt_grams_found"] > 0
    if out["dataset_verbatim"]:
        out["origin"] = "dataset_verbatim"
    elif out["dataset_fragment"]:
        out["origin"] = "dataset_fragment"
    elif out["prompt"]:
        out["origin"] = "prompt"
    elif out["cpt"]:
        out["origin"] = "cpt"
    return out


# ───────────────────────── статистика ─────────────────────────

def chi2_sf(x: float, df: int) -> float:
    """P(χ² > x) для целых df — регуляризованная неполная гамма Q, без scipy."""
    if x <= 0:
        return 1.0
    k, x2 = df / 2.0, x / 2.0
    if x2 < k + 1:
        term = total = 1.0 / k
        for i in range(1, 300):
            term *= x2 / (k + i)
            total += term
            if abs(term) < 1e-15 * abs(total):
                break
        return max(0.0, min(1.0, 1.0 - total * math.exp(-x2 + k * math.log(x2) - math.lgamma(k))))
    tiny = 1e-300
    b = x2 + 1 - k
    c = 1 / tiny
    d = 1 / b if b else 1 / tiny
    h = d
    for i in range(1, 300):
        an = -i * (i - k)
        b += 2
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return max(0.0, min(1.0, h * math.exp(-x2 + k * math.log(x2) - math.lgamma(k))))


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> float:
    """p двустороннего z-теста долей (нормальное приближение)."""
    if n1 == 0 or n2 == 0:
        return 1.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    return math.erfc(abs(k1 / n1 - k2 / n2) / se / math.sqrt(2))


def share(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def block_row(b: dict) -> dict:
    """Компактная строка блока для выгрузки: всё, что нужно для пересборки вопроса.

    Полный текст блока в отчёт не влезает (блоки до 293 слов), но всё остальное —
    длина, период, число копий, протяжённость, сегмент, класс, LCS и строки-источники —
    выгружается по каждому блоку: иначе «доля найденных» нельзя было бы перепроверить.
    """
    return {"report": b.get("report_name"), "state": b.get("state"), "tag": b.get("tag"),
            "region": b.get("region"), "length": b["length"], "period": b["period"],
            "copies": b["copies"], "span_words": b["span_words"], "tandem": b["tandem"],
            "strong": b["strong"], "position_frac": b.get("position_frac"),
            "token_estimate": b.get("token_estimate"), "origin": b["origin"],
            "dataset_lcs": b.get("dataset_lcs"),
            "dataset_case_exact": b.get("dataset_case_exact"),
            "dataset_rows": b.get("dataset_rows"),
            "dataset_groups": b.get("dataset_groups"), "src_rows": b.get("src_rows"),
            "regions": b.get("regions_seen") or [b.get("region")],
            "cpt_grams_found": b.get("cpt_grams_found"),
            "cpt_grams_total": b.get("cpt_grams_total"),
            "trivial_text": b.get("trivial_text"),
            "head": (b.get("text") or "")[:80]}


def len_bucket(n: int) -> str:
    for lo, hi in ((8, 8), (9, 12), (13, 16), (17, 24), (25, 32), (33, 64), (65, 128), (129, 256)):
        if lo <= n <= hi:
            return f"{lo}-{hi}"
    return ">256"


# ───────────────────────── сборка ─────────────────────────

def collect(reports: list[dict], states: list[str], max_probes: int | None) -> dict:
    """Петли и контроль по всем отчётам и состояниям — до чтения набора."""
    per_report: dict[str, dict] = {}
    #: В промпт-ось входит и системный промпт проб — он тот же у всех доменных проб
    #: (`probe_control.UNIFIED_SYSTEM_PROMPT`, тот же объект, что у приборов S3ab+),
    #: и его текст в отчёте проб не сохраняется: без этой строки ось «петля-копия
    #: входа» проверяла бы только реплику пользователя.
    prompt_grams: set[tuple] = grams_of(norm_text(getattr(LS, "UNIFIED_SYSTEM_PROMPT", "")))
    for r in reports:
        rd = json.loads(r["text"])
        wanted = states or list((rd.get("states") or {}).keys())
        per_state: dict[str, dict] = {}
        for state in wanted:
            st = (rd.get("states") or {}).get(state)
            if st is None:
                raise KeyError(f"состояние {state} не найдено в {r['spec']}")
            probes = list(st.get("probes") or [])
            if max_probes is not None:
                probes = probes[:max_probes]
            blocks: list[dict] = []
            controls: list[dict] = []
            for p in probes:
                response = p.get("response") or ""
                if p.get("prompt"):
                    prompt_grams |= grams_of(norm_text(str(p["prompt"])))
                loops = loops_of_probe(response, int(p.get("n_new_tokens") or 0))
                mild = [l for l in loops
                        if l["length"] >= MIN_N and l["copies"] >= MIN_COUNT]
                for l in mild:
                    blocks.append({"report": r["spec"], "report_name": r["name"],
                                   "state": state, "tag": p.get("tag"),
                                   "probe_tokens": p.get("n_new_tokens"),
                                   "stop_reason": p.get("stop_reason"),
                                   "hit_limit": p.get("hit_limit"),
                                   "looped_instrument": bool((p.get("metrics") or {}).get("looped")),
                                   **l})
                for arm, text, region in (
                        ("same_generation", response, "all"),
                        *[("same_region", region_texts(response).get(l["region"], ""), l["region"])
                          for l in mild if l["strong"]]):
                    words = norm_words(text)
                    if len(words) < MIN_N:
                        continue
                    counts = Counter(tuple(words[i:i + MIN_N])
                                     for i in range(len(words) - MIN_N + 1))
                    uniq = [g for g, c in counts.items() if c == 1]
                    if not uniq:
                        continue
                    step = max(1, len(uniq) // CONTROLS_PER_GENERATION)
                    for j in list(range(0, len(uniq), step))[:CONTROLS_PER_GENERATION]:
                        controls.append({"report": r["spec"], "report_name": r["name"],
                                         "state": state, "tag": p.get("tag"),
                                         "arm": arm, "region": region,
                                         "text": " ".join(uniq[j])})
            per_state[state] = {
                "blocks": blocks, "controls": controls, "n_probes": len(probes),
                "checkpoint": st.get("checkpoint"),
                "instrument_loop_share": (st.get("aggregate") or {}).get("looped_share"),
                "truncated_share": (st.get("aggregate") or {}).get("truncated_share"),
                "lengths": (st.get("aggregate") or {}).get("lengths"),
            }
        per_report[r["spec"]] = per_state
    return {"per_report": per_report, "prompt_grams": prompt_grams}


def _dedupe(entries: list[dict]) -> list[dict]:
    """Свести одинаковые блоки внутри набора записей тем же правилом сегмента."""
    by_text: dict[str, dict] = {}
    for b in entries:
        cur = by_text.get(b["text"])
        if cur is None or REGION_RANK.get(b["region"], 0) > REGION_RANK.get(cur["region"], 0):
            by_text[b["text"]] = b
    return list(by_text.values())


def summarize(entries: list[dict]) -> dict:
    """Свод одного яруса: доли классов происхождения и распределение LCS."""
    n = len(entries)
    by_origin = Counter(e["origin"] for e in entries)
    found = by_origin.get("dataset_verbatim", 0) + by_origin.get("dataset_fragment", 0)
    return {
        "n": n,
        "by_origin": dict(sorted(by_origin.items())),
        "found_in_dataset": found,
        "found_share": share(found, n),
        "verbatim": by_origin.get("dataset_verbatim", 0),
        "fragment": by_origin.get("dataset_fragment", 0),
        "in_prompt": by_origin.get("prompt", 0),
        "in_cpt": by_origin.get("cpt", 0),
        "none": by_origin.get("none", 0),
        "lcs_ge_8": sum(1 for e in entries if e.get("dataset_lcs", 0) >= MIN_N),
        "lcs_ge_16": sum(1 for e in entries if e.get("dataset_lcs", 0) >= STRONG_LEN),
        "lcs_max": max((e.get("dataset_lcs", 0) for e in entries), default=0),
        "case_exact": sum(1 for e in entries if e.get("dataset_case_exact")),
        "no_source_rows": sum(1 for e in entries if not e.get("src_rows")),
        "no_source_rows_share": share(sum(1 for e in entries if not e.get("src_rows")), n),
        "cpt_covered": sum(1 for e in entries if e.get("cpt")),
        "span_max": max((e.get("span_words", 0) for e in entries), default=0),
        "span_median": (sorted(e.get("span_words", 0) for e in entries)[len(entries) // 2]
                        if entries else 0),
    }


def examples(entries: list[dict], origin: str, k: int = MAX_EXAMPLES) -> list[dict]:
    keys = ("state", "tag", "region", "length", "period", "copies", "span_words",
            "position_frac", "token_estimate", "dataset_lcs", "dataset_rows",
            "dataset_groups", "src_rows", "src_mults", "cpt_grams_found",
            "cpt_grams_total", "trivial_text", "probe_tokens", "stop_reason")
    out = []
    for e in entries:
        if e["origin"] != origin:
            continue
        out.append({key: e.get(key) for key in keys} | {"head": (e.get("text") or "")[:HEAD_CHARS]})
        if len(out) >= k:
            break
    return out


def build(args) -> tuple[dict, int]:
    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        note(f"NOT-VERIFIED: нет набора {dataset_path}")
        return {}, EXIT_NOT_VERIFIED
    reports = []
    for spec in args.report:
        r = read_input(spec)
        if r is None:
            return {}, EXIT_NOT_VERIFIED
        reports.append(r)
    dataset_sha = sha256_file(dataset_path)
    if args.expect_dataset_sha256 and dataset_sha != args.expect_dataset_sha256:
        note(f"ОТКАЗ: sha256 набора {dataset_sha} ≠ ожидаемого "
             f"{args.expect_dataset_sha256} — это другой вход")
        return {}, EXIT_FAIL

    col = collect(reports, args.state, args.max_probes)
    blocks = [b for ps in col["per_report"].values() for s in ps.values() for b in s["blocks"]]
    controls = [c for ps in col["per_report"].values() for s in ps.values() for c in s["controls"]]
    if not blocks:
        note("NOT-VERIFIED: ни в одной генерации нет петли по критерию "
             f"({MIN_N} слов × {MIN_COUNT} раза)")
        return {}, EXIT_NOT_VERIFIED

    queries = build_queries(blocks, controls)
    note(f"блоков петель {len(blocks)}, контрольных юнитов {len(controls)}, "
         f"целевых 8-грамм {len(queries)}")

    line_offsets = scan_line_offsets(dataset_path)
    scan = scan_jsonl_dataset(dataset_path, queries, args.limit)
    if args.limit is not None:
        note(f"ВНИМАНИЕ: --limit {args.limit} — набор прочитан не весь "
             f"({scan['n_examples']} строк): числа непригодны для вердикта")

    cpt_hits: dict[tuple, int] = {}
    cpt_files = []
    declared = declared_shas(Path(args.cpt_manifest)) if args.cpt_manifest else None
    for c in (args.cpt or []):
        cp = Path(c)
        if not cp.exists():
            note(f"CPT-корпус {cp} не найден — ось `cpt` не измерена (названо, не замазано)")
            continue
        sc = scan_plain_corpus(cp, queries)
        for k, v in sc["hits"].items():
            cpt_hits[k] = cpt_hits.get(k, 0) + v
        sha = sha256_file(cp)
        cpt_files.append({"path": rel(cp), "bytes_read": sc["bytes_read"],
                          "sha256": sha, "capped": sc["capped"],
                          "declared_in_manifest": (sha in declared) if declared is not None else None})

    for ps in col["per_report"].values():
        for s in ps.values():
            for e in s["blocks"] + s["controls"]:
                e.update(classify(e["text"], scan, dataset_path, line_offsets,
                                  cpt_hits, col["prompt_grams"], e.get("text_raw")))

    #: Сведение одинаковых блоков: единица анализа происхождения — ТЕКСТ повтора,
    #: а не его вхождение (иначе один и тот же повтор, найденный в двух режимах или
    #: двух состояниях, считался бы дважды). Сегмент при сведении берётся самый
    #: конкретный (`REGION_RANK`), а не последний обработанный.
    #: Сведение одинаковых блоков: единица анализа происхождения — ТЕКСТ повтора,
    #: а не его вхождение (иначе один и тот же повтор, найденный в двух режимах или
    #: двух состояниях, считался бы дважды). Сегмент при сведении берётся самый
    #: конкретный (`REGION_RANK`), а не последний обработанный; «в каких чтениях блок
    #: вообще встретился» запоминается отдельно — петля рассуждения видна и в чтении
    #: «ход целиком», и терять это нельзя.
    regions_of: dict[str, set] = {}
    for b in blocks:
        regions_of.setdefault(b["text"], set()).add(b["region"])
    by_text: dict[str, dict] = {}
    for b in blocks:
        cur = by_text.get(b["text"])
        if cur is None or REGION_RANK.get(b["region"], 0) > REGION_RANK.get(cur["region"], 0):
            by_text[b["text"]] = b
    distinct = list(by_text.values())
    for b in distinct:
        b["regions_seen"] = sorted(regions_of[b["text"]])
    region_any: Counter = Counter()
    for regs in regions_of.values():
        for r in regs:
            region_any[r] += 1
    strong = [b for b in distinct if b["strong"]]
    ctrl_distinct = list({c["text"]: c for c in controls}.values())
    ctrl_region = [c for c in ctrl_distinct if c["arm"] == "same_region"]

    base_rows = Counter(r["mult"] for r in scan["rows"])
    base_groups = Counter(scan["group_count"].values())
    #: Длина строки связана с кратностью, а «нашлось» растёт с длиной строки: без
    #: этого разреза перекос кратности у находок читался бы как свойство кратности,
    #: хотя часть его — свойство длины (строка кратности 1 вчетверо короче).
    words_by_mult: dict[int, list[int]] = defaultdict(list)
    for r in scan["rows"]:
        words_by_mult[r["mult"]].append(r["words"])
    length_by_mult = {str(m): {"rows": len(v), "median_words": sorted(v)[len(v) // 2],
                               "mean_words": round(sum(v) / len(v), 1)}
                      for m, v in sorted(words_by_mult.items())}

    #: Кратность источника считается по строкам, несущим самый редкий 8-грамм
    #: блока (см. `source_rows`), и отдельно — без блоков из знаков пунктуации:
    #: «- - - - - - - -» находит себе пару в любом тексте, и его кратность — про
    #: тривиальность узора, а не про источник.
    def mult_hist(entries: list[dict], nontrivial_only: bool = False) -> Counter:
        c: Counter = Counter()
        for e in entries:
            if nontrivial_only and e.get("trivial_text"):
                continue
            for k, v in (e.get("src_mults") or {}).items():
                c[int(k)] += v
        return c

    classes = (1, 2, 3, 6)

    def contingency(entries_loop: list[dict], entries_ctrl: list[dict],
                    nontrivial_only: bool) -> dict:
        a = mult_hist(entries_loop, nontrivial_only)
        b = mult_hist(entries_ctrl, nontrivial_only)
        table = [[a.get(m, 0) for m in classes], [b.get(m, 0) for m in classes]]
        na, nb = sum(table[0]), sum(table[1])
        chi2, grand = 0.0, na + nb
        for i in (0, 1):
            tot = sum(table[i])
            for j in range(len(classes)):
                col = table[0][j] + table[1][j]
                if tot and col and grand:
                    exp = tot * col / grand
                    chi2 += (table[i][j] - exp) ** 2 / exp
        return {"contingency_1_2_3_6": table, "rows_loops": na, "rows_controls": nb,
                "chi2": round(chi2, 4), "p_value": round(chi2_sf(chi2, 3) if grand else 1.0, 6),
                "share_mult_ge3_loops": share(sum(table[0][2:]), na),
                "share_mult_ge3_controls": share(sum(table[1][2:]), nb),
                "nontrivial_only": nontrivial_only}

    mult_all = contingency(distinct, ctrl_distinct, False)
    mult_nontrivial = contingency(distinct, ctrl_distinct, True)
    #: Вердикт по кратности считается ТОЛЬКО по нетривиальным блокам: у блока из
    #: пунктуации самый редкий 8-грамм всё равно нередок, и его «источник» — узор,
    #: а не текст. Таблица по пунктуации остаётся в отчёте описательной величиной.
    mult_used = mult_nontrivial

    spans = [max(v) - min(v) for v in scan["group_rows"].values() if len(v) > 1]
    adjacent = sum(1 for v in scan["group_rows"].values()
                   if len(v) > 1 and max(v) - min(v) <= len(v))
    passes = None
    if args.samples_seen:
        passes = round(args.samples_seen / max(1, scan["n_examples"]), 4)

    steps = {"available": False, "why": "ось шагов не запрошена (--steps-report)"}
    if args.steps_report:
        sp = read_input(args.steps_report)
        if sp is None:
            steps = {"available": False, "why": f"вход не читается: {args.steps_report}"}
        else:
            rd = json.loads(sp["text"])
            by_step = []
            for name, st in (rd.get("states") or {}).items():
                bs = [l for p in (st.get("probes") or [])
                      for l in loops_of_probe(p.get("response") or "",
                                              int(p.get("n_new_tokens") or 0))
                      if l["length"] >= MIN_N and l["copies"] >= MIN_COUNT]
                by_step.append({"state": name, "n_probes": len(st.get("probes") or []),
                                "blocks": len(bs),
                                "blocks_per_probe": round(len(bs) / max(1, len(st.get("probes") or [])), 2),
                                "blocks_strong": sum(1 for b in bs if b["strong"]),
                                "checkpoint": st.get("checkpoint"),
                                "instrument_loop_share": (st.get("aggregate") or {}).get("looped_share")})
            steps = {"available": True, "report": sp["spec"], "sha256": sp["sha256"],
                     "by_step": by_step,
                     "not_the_measured_run": "это разбор ДРУГОГО прогона SFT (sft-20260916-2246 → "
                                             "sft-resume-20260917-1215, набор v12, 24 пробы, бюджет 4096, "
                                             "greedy без запрета повторов): тренд по шагам относится к "
                                             "нему, а не к прогону sft-20260919-0810 (набор v13_fixed), "
                                             "на котором снят замер 21 500",
                     "protocol": {k: v for k, v in (rd.get("protocol") or {}).items()
                                  if k in ("prompts_set", "n_prompts", "decoding",
                                           "max_new_tokens", "instrument")}}
    ckpts = []
    for d in (args.ckpt_dir or []):
        dp = Path(d)
        if dp.is_dir():
            ckpts.append({"dir": rel(dp),
                          "files": sorted(p.name for p in dp.glob("sft_probe_*.pt"))})

    data = {
        "schema": "sft-loop-origin/1",
        "tool": rel(Path(__file__)),
        "tool_sha256": sha256_file(Path(__file__)),
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "question": "петли SFT-состояния — воспроизведение выученных из набора фрагментов "
                    "(следствие кратной повторяемости) или собственная дегенерация модели "
                    "(цикл n-грамм, которого в наборе нет)?",
        "status": "measured",
        "command": "python3 " + " ".join(shlex.quote(a) for a in sys.argv),
        "inputs": {
            "reports": [{"path": r["spec"], "ref": r["ref"], "bytes": r["bytes"],
                        "sha256": r["sha256"]} for r in reports],
            "dataset": {"path": rel(dataset_path), "sha256": dataset_sha,
                        "n_examples_read": scan["n_examples"],
                        "unique": len(scan["group_count"]),
                        "duplicates_share": share(scan["n_examples"] - len(scan["group_count"]),
                                                  scan["n_examples"]),
                        "multiplicity_rows": {str(k): v for k, v in sorted(base_rows.items())},
                        "multiplicity_groups": {str(k): v for k, v in sorted(base_groups.items())},
                        "full_file": len(scan["rows"]) == scan["n_examples"],
                        "limited": args.limit is not None},
            "cpt_corpora": cpt_files,
            "steps_report": args.steps_report,
            "checkpoint_dirs": ckpts,
        },
        "protocol": {
            "min_n_words": MIN_N, "min_count": MIN_COUNT,
            "strong": {"length": STRONG_LEN, "count": STRONG_COUNT,
                       "tandem_period": TANDEM_PERIOD},
            "controls_per_generation": CONTROLS_PER_GENERATION,
            "regions": REGIONS,
            "segment_splitter": "probe_language_split.split_segments (импорт, не копия)",
            "normalization": "регистр вниз, пробелы в один (norm_text)",
            "search_in_dataset": "assistant-текст строки",
            "caps": {"candidate_rows": MAX_CANDIDATE_ROWS,
                     "occurrences_per_gram": MAX_OCCURRENCES_PER_GRAM,
                     "hits_per_gram": MAX_HITS_PER_GRAM},
        },
        "origin": {
            "mild": summarize(distinct), "strong": summarize(strong),
            "nontrivial": summarize([e for e in distinct if not e.get("trivial_text")]),
            "trivial": summarize([e for e in distinct if e.get("trivial_text")]),
            "control_all": summarize(ctrl_distinct),
            "control_same_region": summarize(ctrl_region),
            "by_state": {key: summarize(_dedupe([
                b for b in blocks if "%s/%s" % (b["report_name"], b["state"]) == key]))
                for key in sorted({"%s/%s" % (b["report_name"], b["state"]) for b in blocks})},
            "examples": {o: examples(distinct, o) for o in
                         ("dataset_verbatim", "dataset_fragment", "prompt", "cpt", "none")}},
        "multiplicity": {
            "base_rows": {str(k): v for k, v in sorted(base_rows.items())},
            "base_groups": {str(k): v for k, v in sorted(base_groups.items())},
            "source_rule": "строки-источники — несущие самый редкий 8-грамм блока "
                           "(source_rows); кратность — по ключу карточки набора",
            "all_blocks": mult_all,
            "punctuation_excluded": mult_nontrivial,
            "used": mult_used,
            "trivial_blocks": sum(1 for e in distinct if e.get("trivial_text")),
            "length_by_mult": length_by_mult,
            #: Прямой ответ на вопрос о кратности источника: какую кратность несут
            #: строки, в которых нашлись сами блоки петель (а не все блоки вообще).
            "found_blocks": {
                "n": sum(1 for e in distinct if e["origin"].startswith("dataset_")),
                "src_mult_hist": dict(sorted(mult_hist(
                    [e for e in distinct if e["origin"].startswith("dataset_")]).items())),
                "rows_total": scan["n_examples"],
                "base_rate_rows": {str(k): share(v, scan["n_examples"])
                                   for k, v in sorted(base_rows.items())},
                "same_generation_control": dict(sorted(mult_hist(
                    [c for c in ctrl_distinct
                     if c["origin"].startswith("dataset_")]).items())),
            },
            "geometry": {
                "groups_with_copies": len(spans),
                "groups_within_one_run": adjacent,
                "share_within_one_run": share(adjacent, len(spans)),
                "median_gap_lines": sorted(spans)[len(spans) // 2] if spans else None,
                "max_gap_lines": max(spans) if spans else None,
                "note": "span — расстояние в строках между первой и последней копией одного "
                        "уникального текста; «в одном прогоне» — span ≤ числа копий (копии идут подряд)",
            },
            "exposure": {
                "samples_seen": args.samples_seen,
                "samples_seen_source": args.samples_seen_source,
                "dataset_passes": passes,
                "loader": "DataLoader(shuffle=True, batch_size=2) — внутри эпохи строка "
                          "берётся один раз",
                "passes_per_mult_class": ({str(m): round(passes * m, 2) for m in classes}
                                          if passes is not None else None),
                "note": "если dataset_passes ≤ 1, ни одна строка не показана дважды — "
                        "кратность как повторение внутри обучения ещё не могла сработать",
            },
        },
        "steps": steps,
        "blocks": {
            "total_mild": len(blocks), "distinct_mild": len(distinct),
            "total_strong": sum(1 for b in blocks if b["strong"]),
            "distinct_strong": len(strong),
            "by_state": {r: {s: {"blocks": sum(1 for b in v["blocks"] if b["length"] >= MIN_N
                                                    and b["copies"] >= MIN_COUNT),
                                 "blocks_strong": sum(1 for b in v["blocks"] if b["strong"]),
                                 "n_probes": v["n_probes"],
                                 "distinct_blocks": len({b["text"] for b in v["blocks"]}),
                                 "control_units": len(v["controls"]),
                                 "instrument_loop_share": v["instrument_loop_share"],
                                 "truncated_share": v["truncated_share"],
                                 "median_tokens": (v.get("lengths") or {}).get("median")}
                             for s, v in ps.items()} for r, ps in col["per_report"].items()},
            "by_region": dict(sorted(Counter(b["region"] for b in blocks).items())),
            "by_region_distinct": dict(sorted(Counter(b["region"] for b in distinct).items())),
            "by_region_any": dict(sorted(region_any.items())),
            "by_region_strong": dict(sorted(Counter(b["region"] for b in blocks
                                                    if b["strong"]).items())),
            "by_region_note": "сегмент `all` — ход целиком и пересекается с остальными: "
                              "петля рассуждения попадает и в `all`, и в `think`; "
                              "складывать числа регионов нельзя",
            "length_hist": {str(k): v for k, v in sorted(
                Counter(len_bucket(b["length"]) for b in distinct).items())},
            "distinct_list": [block_row(b) for b in
                               sorted(distinct, key=lambda x: -x["span_words"])],
            "period_hist": {str(k): v for k, v in sorted(
                Counter("1" if b["tandem"] else ">4" for b in distinct).items())},
        },
        "rules": RULES,
    }
    return data, EXIT_OK


def verdict(d: dict) -> dict:
    """Вердикт по объявленным правилам — числами, с несработавшими правилами рядом."""
    mild, ctrl = d["origin"]["mild"], d["origin"]["control_all"]
    n1, n2 = mild["n"], ctrl["n"]
    k1, k2 = mild["found_in_dataset"], ctrl["found_in_dataset"]
    delta = (k1 / n1 - k2 / n2) if n1 and n2 else None
    p_z = two_prop_z(k1, n1, k2, n2)
    if delta is None:
        branch = "insufficient_data"
        text = "нет одного из чтений (петель или контроля)"
    elif (mild["found_share"] or 0) >= 0.5 and delta >= 0.20:
        branch = "memorized_prevails"
        text = (f"петли в основном воспроизводят фрагменты набора: найдено {k1}/{n1} "
                f"= {mild['found_share']} против контроля {k2}/{n2} = {ctrl['found_share']}, "
                f"разрыв {delta:.4f}")
    elif delta <= 0.05:
        branch = "own_degeneration"
        text = (f"петли не воспроизводят набор: найдено {k1}/{n1} = {mild['found_share']} "
                f"против контроля {k2}/{n2} = {ctrl['found_share']}, разрыв {delta:.4f} ≤ 0.05")
    else:
        branch = "mixed"
        text = (f"часть петель воспроизводит набор: петель {k1}/{n1} = {mild['found_share']}, "
                f"контроль {k2}/{n2} = {ctrl['found_share']}, разрыв {delta:.4f}")
    #: Вердикт объявлен на контроле «та же генерация» (`control_all`); вторая рука
    #: (тот же сегмент) считается рядом — вердикт, который держится только на одной
    #: руке контроля, не устойчив, и это должно быть видно, а не выясняться потом.
    arms = {}
    for arm_key, arm in (("same_generation", ctrl), ("same_region", d["origin"]["control_same_region"])):
        k_arm, n_arm = arm["found_in_dataset"], arm["n"]
        delta_arm = (k1 / n1 - k_arm / n_arm) if n1 and n_arm else None
        if delta_arm is None:
            br = "insufficient_data"
        elif (mild["found_share"] or 0) >= 0.5 and delta_arm >= 0.20:
            br = "memorized_prevails"
        elif delta_arm <= 0.05:
            br = "own_degeneration"
        else:
            br = "mixed"
        arms[arm_key] = {"n_controls": n_arm, "found_in_dataset": k_arm,
                         "control_found_share": arm["found_share"],
                         "delta": round(delta_arm, 4) if delta_arm is not None else None,
                         "branch": br}
    m = d["multiplicity"]["used"]
    n_rows = m["rows_loops"]
    if n_rows < 20:
        mult_branch = "underpowered"
        mult_text = (f"строк-источников у нетривиальных блоков петель {n_rows} < 20 — "
                     "распределение кратности не измеряется; вердикта по кратности нет "
                     "(таблица по пунктуационным блокам — описательная, см. "
                     "multiplicity.all_blocks)")
    elif m["p_value"] < 0.05 and (m["share_mult_ge3_loops"] or 0) > (m["share_mult_ge3_controls"] or 0):
        mult_branch = "shift_to_repeated"
        mult_text = (f"источник петель смещён к размноженным строкам: доля кратности ≥3 "
                     f"{m['share_mult_ge3_loops']} против {m['share_mult_ge3_controls']} "
                     f"у контроля, χ²={m['chi2']}, p={m['p_value']}")
    else:
        mult_branch = "no_shift"
        mult_text = (f"перекоса кратности нет: χ²={m['chi2']}, p={m['p_value']}, кратность ≥3 "
                     f"у петель {m['share_mult_ge3_loops']} против "
                     f"{m['share_mult_ge3_controls']} у контроля")
    return {
        "question": d["question"],
        "verdict": branch,
        "rule_branch": {"origin": {"fired": branch, "loop_found_share": mild["found_share"],
                                   "control_found_share": ctrl["found_share"],
                                   "delta": None if delta is None else round(delta, 4),
                                   "two_prop_p": round(p_z, 6),
                                   "n_loops": n1, "n_controls": n2},
                       "by_control_arm": arms},
        "multiplicity_branch": {"fired": mult_branch, "text": mult_text,
                                "rows_matched_by_loops": n_rows},
        "strong_tier": {"n": d["origin"]["strong"]["n"],
                        "found_share": d["origin"]["strong"]["found_share"],
                        "found_in_dataset": d["origin"]["strong"]["found_in_dataset"],
                        "by_origin": d["origin"]["strong"]["by_origin"]},
        "text": text,
        "rules_evaluated": [{"key": k, **v} for k, v in RULES.items()],
    }


def selftest() -> int:
    """Фикстуры механики: тандем, мягкая петля, отсутствие петли, статистика."""
    w = ("альфа бета гамма дельта " * 6).split()
    loops = mark_loops(w)
    assert loops, "тандемный повтор обязан находиться"
    top = max(loops, key=lambda x: x["length"])
    assert top["length"] == 8 and top["period"] == 4, top
    assert top["copies"] == 5 and top["span_words"] == 24, top
    assert top["strong"] and top["tandem"], top
    w2 = ("раз два три четыре пять шесть семь восемь " * 2
          + " ".join(f"слово{i}" for i in range(20))).split()
    assert any(x["length"] == 8 and x["copies"] == 2 for x in mark_loops(w2)), "мягкая петля"
    assert mark_loops([f"уникальное{i}" for i in range(60)]) == [], "без повторов петель нет"
    assert chi2_sf(0.0, 3) == 1.0
    assert 0.0 <= chi2_sf(10.0, 3) <= 1.0
    # χ²=11.34 при df=3 — критическое значение для p=0.01: 0.005 < sf < 0.02
    sf = chi2_sf(11.34, 3)
    assert 0.005 < sf < 0.02, sf
    assert abs(two_prop_z(10, 100, 10, 100) - 1.0) < 1e-9
    assert two_prop_z(50, 100, 10, 100) < 1e-6
    assert len_bucket(8) == "8-8" and len_bucket(300) == ">256"
    print("selftest OK", file=sys.stderr)
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--report", action="append", default=[], help="отчёт проб (можно несколько)")
    ap.add_argument("--state", action="append", default=[], help="состояние отчёта (по умолчанию все)")
    ap.add_argument("--dataset", default="datasets/sft_train_v13_fixed.jsonl")
    ap.add_argument("--expect-dataset-sha256", default=None)
    ap.add_argument("--cpt-manifest", default=None,
                    help="манифест микса CPT: сверка sha256 сканируемых файлов с объявленными")
    ap.add_argument("--cpt", action="append", default=[],
                    help="текстовый CPT-корпус (вторичная ось «заучено до SFT»)")
    ap.add_argument("--steps-report", default=None,
                    help="отчёт с разбором по шагам обучения (ось шагов)")
    ap.add_argument("--ckpt-dir", action="append", default=[],
                    help="каталог чекпойнтов для описи «что доступно»")
    ap.add_argument("--samples-seen", type=int, default=None,
                    help="сколько примеров прогон прочитал к точке замера (шаг × batch)")
    ap.add_argument("--samples-seen-source", default=None,
                    help="откуда взято число примеров (манифест/имя состояния)")
    ap.add_argument("--limit", type=int, default=None, help="строк набора (отладка)")
    ap.add_argument("--max-probes", type=int, default=None, help="проб на состояние (отладка)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.report:
        note("NOT-VERIFIED: не задан ни один --report")
        return EXIT_NOT_VERIFIED
    try:
        data, code = build(args)
    except KeyError as e:
        note(f"ОТКАЗ: {e}")
        return EXIT_FAIL
    if code != EXIT_OK:
        return code
    data["answer"] = verdict(data)
    data["assumptions"] = [
        "«Найден в наборе» = найден в assistant-тексте строки (цель обучения), а не во всей "
        "строке: фрагмент из промпта или ответа инструмента выучен не как цель",
        "Поиск идёт по нормализованному тексту (регистр вниз, пробелы в один); поиск с "
        "сохранением регистра строже и дал бы меньше находок — здесь он не считается",
        "Порог «нашлось» — 8 слов подряд (MIN_N): более короткое совпадение неотличимо от "
        "частой русской фразы, а более длинное не покрыло бы петлю, собранную из частых слов",
        "Контроль берётся из тех же генераций (уникальные 8-граммы), поэтому разница «петля "
        "против контроля» не смешивается с разницей между состояниями или промптами",
        "Петли считаются по СЛОВАМ, а не токенам: прибор S3aj/S3ak меряет 4-граммы токенов, "
        "поэтому «петель» здесь и там разное число по построению",
        "Ось `cpt` измерена по файлам, названным в --cpt, и сверена с манифестом микса по "
        "sha256 (--cpt-manifest): это источники стадии CPT, а не точный срез её чанков и не "
        "весь предобучающий корпус базовой модели; переоценка возможна только в сторону «нашлось»",
        "Кратность источника читается по строкам, несущим самый редкий 8-грамм блока: у блока "
        "из пунктуации самый редкий грамм всё равно нередок, поэтому такие блоки исключаются "
        "из таблицы кратности отдельной строкой отчёта",
        "Ось `prompt` — текст проб и системный промпт приборов (`UNIFIED_SYSTEM_PROMPT` "
        "импортом из probe_control): системный промпт в отчёте проб текстом не сохранён, "
        "поэтому берётся тот же объект, что был у пробы",
    ]
    data["open_questions"] = [
        "Петли, не найденные ни в наборе, ни в CPT-корпусе, могли быть заучены на "
        "предобучении базовой модели — локально этот корпус не проверяется",
        "Замер стоит на одной точке обучения: отделить вклад темпа и эпох от вклада данных "
        "на одном срезе нельзя",
        "Почему тандем (период 1) рождается в tool_call чаще, чем в прозе, здесь не "
        "проверяется: названо распределение, не механизм",
    ]
    data["conflicts"] = [
        "Кратность входа названа как «effective_passes 7.071 при 44 105 строках / 18 713 "
        "уникальных» — это 3 эпохи × (строки/уникальные), то есть про весь план стадии; на "
        "шаге 21 500 прогон прошёл ≈0.98 эпохи, поэтому проходов по уникальной строке в этой "
        "точке меньше и зависит от её кратности (≈1 при кратности 1, ≈5.9 при кратности 6) — "
        "оба числа верны, но про разное",
        "«Петель 17.31 %» (прибор S3ak: 4-граммы токенов, greedy с запретом повторов) и "
        "«блоков петель» здесь — разные единицы счёта одного явления; сравнивать их напрямую "
        "нельзя, и в отчёте они стоят рядом именно как разные",
    ]
    if args.out and not args.no_write:
        Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
        note(f"отчёт записан: {args.out}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
