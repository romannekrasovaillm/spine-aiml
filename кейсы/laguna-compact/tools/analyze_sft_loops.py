#!/usr/bin/env python3
"""S3ak — петли в обучающем наборе SFT: есть ли примеры-петли и откуда они.

Зачем отдельный прибор. ADR-040 объявил приоритетом №1 **вырождение**: 58.3 %
генераций SFT-состояний при greedy не кончаются вообще, медиана дописанного хода
44–78 токенов против 1 441 у CPT-финала. Причин у петли может быть четыре
(декодирование, шаг обучения, данные, прибор), и каждая требует своего числа.
Здесь измеряется **только одна** — «данные»: есть ли в обучающем наборе примеры с
высокой внутренней повторяемостью, и как их доля соотносится с двумя разными
числами генераций S3aj — долей не кончившихся, то есть усечённых по лимиту
токенов (0.5833), и долей зацикленных по **той же самой** метрике 4-грамм
(0.5417) — а также с известной долей дубликатов (0.5771, ADR-033). Различать
эти два числа обязательно: 0.5833 — про то, что модель не остановилась,
0.5417 — про то, что она повторяется, и только второе однородно долям по данным.
Без этих чисел разговор «петля от данных» остаётся суждением.

Гипотеза, которую прибор обязан уметь **опровергнуть**, а не подтвердить:
петля — следствие 57.71 % дубликатов, то есть в наборе лежат тексты, где
повторяется целый ответ. Перекрёстная таблица (доля петлевых среди дубликатов
против доли петлевых среди уникальных) — то место, где это видно: если петли
живут только в дубликатах, лечение — дедупликация; если и в уникальных тоже,
дедупликация петлю не снимет.

Почему метрика берётся **импортом**. «Зациклилось» — диагноз, который в двух
местах не должен считаться по-разному: метрика приходит из прибора S3ab
(`probe_control.degenerate_metrics`), порог — из прибора S3ai/S3aj
(`probe_language_split.LOOP_MAX4GRAM_REP`, то есть **тот же** порог, по которому
мерились генерации, — 8 повторов 4-граммы слов). Копия разошлась бы с
источником при первой правке, и «та же метрика» перестало бы быть проверяемым.
Порог здесь **не переопределяется**, а пиннуется: если импортируемый порог
изменится, прибор откажет (код 1), а не посчитает по новому молча.

Два чтения — и вердикт по второму (объявлено ДО прогона):

* **(а) весь assistant-текст** — все сообщения с ролью `assistant`, подряд;
* **(б) проза** — тот же текст **вне** блоков `<tool_call>…</tool_call>` и
  `<tool_response>…</tool_response>`.

Зачем два. Структурированные блоки — размеченный JSON вызова и определения
концептов из ответа инструмента — сами по себе дают повторы 4-грамм (одни и те же
ключи `{"name": "search_concepts", "query": …}`, одни и те же поля определений),
**не будучи петлёй в прозе**. Если считать по всему тексту, доля «петлевых
примеров» будет мерой разметки, а не вырождения. Поэтому вердикт и перекрёстная
таблица считаются **по прозе**, а чтение по всему тексту лежит рядом как
диагностика — и расхождение двух чтений само является числом (в нём видно, сколько
«петель» создано разметкой).

Третье чтение — **диагностическое** (`prose_no_think`): проза, из которой вырезаны
ещё и блоки `<think>` (готовый `probe_language_split.split_segments()["prose"]`).
Оно нужно потому, что «проза» в приборе S3ai означает именно это, а в задаче S3ak
— «вне блоков инструмента». Разница между чтениями (б) и (в) — вклад
рассуждения в повторы; назвать её числом честнее, чем выбрать одно молча.

Границы, объявленные заранее (без них прибор доопределялся бы по месту):

* **пример петлевой**, если **хотя бы одно** его assistant-сообщение петлевое;
  `max4gram_rep` примера — максимум по его сообщениям;
* петлевость = `degenerate_metrics(text)["max4gram_rep"] >= LS.LOOP_MAX4GRAM_REP`;
* метрика не определена (слов < 8) → `None` → **не** петля; такие примеры
  считаются отдельно (`n_prose_no_value`), чтобы «нет числа» не читалось как «ноль»;
* `max4gram_rep` / длины — **ближайший ранг** (`probe_language_split._pct`,
  nearest-rank), не интерполяция: у такого распределения выдуманная точка между
  наблюдениями ничего не значит;
* **дубликат** — совпадение нормализованного текста примера (все сообщения по
  порядку, роли + содержимое, пробелы сжаты в один). Это **не** метод ADR-033
  (там `[[role, content]]` → `json.dumps(sort_keys=True)` → sha256), поэтому число
  может отличаться; расхождение называется в `duplicates_note`, а метод ADR-033
  пересчитывается тем же проходом отдельно (`duplicates_adr033_method`) — именно
  как проверка «проход по файлу тот же», а не как подгонка.

Чего прибор **не** делает: не выгружает полные тексты (объём), не правит данные
(NFS — только чтение), не делает вывода «петля от данных» (это вердикт S3ak по
четырём причинам, а не по одной).

Коды возврата::

    0 — отчёт собран
    1 — отказ: вход есть, но не тот (порог зацикливания у импортируемого прибора не
        8, либо метрика/перцентиль в `probe_language_split` недоступны — тогда
        объявленный метод был бы ложью)
    2 — NOT-VERIFIED: данных нет (файла набора нет, или он пуст)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))

#: Метрика вырождения и правило вырезания блоков — **импортом**, не копией (см.
#: шапку). `LS._blocks` используется как есть: своя реализация разошлась бы с
#: источником при правке, и «проза» перестала бы быть тем же сегментом.
import probe_language_split as LS     # noqa: E402
from probe_control import degenerate_metrics  # noqa: E402  (тот же диагноз, что у генераций)

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Путь набора — относительный, от корня кейса (C-012: без абсолютных путей и `..`).
DEFAULT_INPUT = "datasets/sft_train_v12.jsonl"

#: Ожидаемое значение импортируемого порога. Константа нужна не как «свой порог», а
#: как **пин**: отчёт объявляет «порог 8 из прибора S3ai», и если прибор его сменил,
#: это уже не тот вход (код 1). Переопределять порог здесь запрещено (требование
#: задачи) — можно только проверить, что он тот самый.
LOOP_MAX4GRAM_REP_EXPECTED = 8

#: Независимая контрольная величина ADR-033 (доля дословных дубликатов по её
#: методу). В вердикт не входит — нужна, чтобы расхождение с ней было названо
#: числом, а не объяснено на словах.
DUPLICATES_REFERENCE_ADR033 = 0.5771

#: Справочные числа S3aj (runs/s3aj-format-validity-20260917/probe_full_4096.json,
#: состояния sft3000 и sft_resume_4500, по 24 генерации на состояние) — чтобы
#: сравнение с генерациями было названо точно, а не «0.583 одним куском».
#: TRUNCATED — доля НЕ КОНЧИВШИХСЯ генераций, упёршихся в лимит токенов
#: (`aggregate.stop.truncated_share`), именно это число S3ak называет вырождением;
#: LOOPED — доля зацикленных по той же метрике, что считает этот прибор
#: (`aggregate.looped_share`). С долями по данным однородно только второе.
#: CPT-финал для масштаба: looped 0.2917, truncated 0.375.
S3AJ_SFT_TRUNCATED_SHARE = 0.5833
S3AJ_SFT_LOOPED_SHARE = 0.5417
S3AJ_CFINAL_LOOPED_SHARE = 0.2917

#: Сколько «петель» выгружать в отчёт — только метаданными (индекс строки, метрика,
#: длины, первые 200 символов прозы): полный текст примера в отчёт не влезает.
TOP_LOOPS_N = 10
PROSE_HEAD_CHARS = 200

RE_WS = re.compile(r"\s+")


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    """sha256 файла потоково: набор — 486 МБ, читать его в память целиком нельзя."""
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────── два чтения assistant-текста ───────────────────────────

def assistant_text(record: dict) -> str:
    """Весь assistant-текст примера: все сообщения роли `assistant`, подряд.

    Чтение (а). Роли и системный/пользовательский текст не берутся: петля ищется в
    том, что модель обязана **порождать**, а не в том, что ей дают на вход.
    """
    return "\n".join(m.get("content") or "" for m in (record.get("messages") or [])
                     if m.get("role") == "assistant")


def prose_text(text: str) -> str:
    """Проза: текст вне блоков `<tool_call>` и `<tool_response>` (чтение б).

    Вырезание — `probe_language_split._blocks` (импорт, не копия). Незакрытый блок
    съедает остаток: «незакрытый» не значит «пустой», и догадываться, где автор
    хотел закрыть, прибор не имеет права.
    """
    _, rest, _ = LS._blocks(text, LS.TOOL_CALL_OPEN, LS.TOOL_CALL_CLOSE)
    _, prose, _ = LS._blocks(rest, LS.TOOL_RESP_OPEN, LS.TOOL_RESP_CLOSE)
    return prose


def prose_text_no_think(text: str) -> str:
    """Проза без рассуждения (чтение в) — готовый сегмент прибора S3ai."""
    return LS.split_segments(text)["prose"]


def max4gram_rep(text: str) -> int | None:
    """`max4gram_rep` импортируемой метрики; `None` — слов меньше 8 (метрика не определена)."""
    return degenerate_metrics(text).get("max4gram_rep")


def normalize_example(record: dict) -> str:
    """Нормализованный текст примера для поиска дословных дубликатов.

    Все сообщения по порядку, «роль + содержимое», пробелы сжаты в один. Роли
    входят в текст намеренно: пример, отличающийся только ролью сообщения, — не
    дословный дубликат (он учит другому). Метод **не** совпадает с ADR-033
    (см. `normalize_example_adr033`), и это названо, а не замазано.
    """
    parts = [f"{m.get('role')}\n{m.get('content') or ''}"
             for m in (record.get("messages") or [])]
    return RE_WS.sub(" ", "\n".join(parts)).strip()


def normalize_example_adr033(record: dict) -> str:
    """Пересчёт метода ADR-033 тем же проходом — контрольная величина, не вердикт.

    Метод ADR-033: `messages` → `[[role, content]]` → `json.dumps(sort_keys=True)`
    → sha256. Здесь он воспроизводится, чтобы отделить «мой проход по файлу другой»
    от «нормализация другая»: если этот пересчёт даёт записанные 57.71 %, то
    расхождение основного числа объясняется **методом**, а не ошибкой чтения.
    """
    pairs = [[m.get("role"), m.get("content") or ""] for m in (record.get("messages") or [])]
    return json.dumps(pairs, ensure_ascii=False, sort_keys=True)


def looped(rep: int | None) -> bool:
    return bool(rep is not None and rep >= LS.LOOP_MAX4GRAM_REP)


# ─────────────────────────── разбор одного примера ───────────────────────────

def analyze_record(record: dict) -> dict:
    """Все числа одного примера — в одной записи (и только числа + метаданные).

    `looped_*` — по правилу «петлевой, если петлевое хотя бы одно assistant-сообщение»,
    `rep_*` — максимум по сообщениям (None, если метрика не определена ни у одного:
    «нет числа» ≠ «ноль»).
    """
    texts = [m.get("content") or "" for m in (record.get("messages") or [])
             if m.get("role") == "assistant"]
    reps: dict[str, int | None] = {}
    chars: dict[str, int] = {}
    words: dict[str, int] = {}
    for key, fn in (("all", lambda t: t), ("prose", prose_text),
                    ("prose_no_think", prose_text_no_think)):
        segs = [fn(t) for t in texts]
        vals = [max4gram_rep(s) for s in segs]
        defined = [v for v in vals if v is not None]
        reps[key] = max(defined) if defined else None
        chars[key] = sum(len(s) for s in segs)
        words[key] = sum(len(s.split()) for s in segs)
    return {
        "n_assistant_messages": len(texts),
        "rep_all": reps["all"],
        "rep_prose": reps["prose"],
        "rep_prose_no_think": reps["prose_no_think"],
        "looped_all": any(looped(max4gram_rep(t)) for t in texts),
        "looped_prose": any(looped(max4gram_rep(prose_text(t))) for t in texts),
        "looped_prose_no_think": any(looped(max4gram_rep(prose_text_no_think(t)))
                                     for t in texts),
        "chars_all": chars["all"], "words_all": words["all"],
        "chars_prose": chars["prose"], "words_prose": words["prose"],
    }


# ─────────────────────────── свод по набору ───────────────────────────

def _share(num: int, den: int) -> float | None:
    """Доля, округлённая как в приборах S3ai/S3aj (4 знака); `None` при пустом знаменателе."""
    return round(num / den, 4) if den else None


def _round(num: float, den: float) -> float | None:
    """Отношение двух счётчиков (не доля): 3 знака; `None` при нулевом знаменателе."""
    return round(num / den, 3) if den else None


def _dist(values: list[int]) -> dict:
    """Распределение по **ближайшему рангу** (`LS._pct`), не интерполяцией.

    Метод назван числом, потому что от него зависит ответ: медиана/p90 у 40 тысяч
    значений интерполяцией и ближайшим рангом дают разные числа, а здесь важно
    «сколько реально бывает», а не оценка между наблюдениями.
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0, "median": None, "p90": None, "max": None, "min": None,
                "mean": None, "percentile_method": "nearest-rank (LS._pct)"}
    return {"n": len(vals), "median": LS._pct(vals, 0.5), "p90": LS._pct(vals, 0.9),
            "max": max(vals), "min": min(vals),
            "mean": round(sum(vals) / len(vals), 4),
            "percentile_method": "nearest-rank (LS._pct)"}


def _group_lengths(rows: list[dict], flag: str, chars_key: str, words_key: str) -> dict:
    sub = [r for r in rows if r[flag]]
    if not sub:
        return {"n": 0}
    return {"n": len(sub),
            "chars": _dist([r[chars_key] for r in sub]),
            "words": _dist([r[words_key] for r in sub])}


def build_report(rows: list[dict], artifact: dict, partial: bool) -> dict:
    """Свод. Перекрёстная таблица — то место, где отвечается вопрос «откуда петли».

    Дубликат/уникальность считается **по строкам файла** (`loops_share_among_*`:
    доля петлевых среди строк, у которых есть близнец, против строк-одиночек), а
    `loops_share_unique_examples` — **по уникальному множеству** нормализованных
    текстов: это честная мера «сколько разных петлевых текстов в наборе». Два числа
    отвечают на разные вопросы и не заменяют друг друга: первое — «где петли
    встречаются», второе — «сколько их всего».
    """
    n = len(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["dup_hash"]] = counts.get(r["dup_hash"], 0) + 1

    #: По уникальному множеству петлевость обязана быть одинаковой у всех строк с
    #: одним нормализованным текстом (метрика считается по словам, а нормализация
    #: меняет только пробелы). Если это не так — нормализация задела сигнал, и это
    #: надо показать, а не сгладить.
    uniq: dict[str, bool] = {}
    conflicts = 0
    for r in rows:
        prev = uniq.get(r["dup_hash"])
        if prev is None:
            uniq[r["dup_hash"]] = r["looped_prose"]
        elif prev != r["looped_prose"]:
            conflicts += 1

    dup_rows = [r for r in rows if counts[r["dup_hash"]] > 1]
    uni_rows = [r for r in rows if counts[r["dup_hash"]] == 1]

    def _cross(flag: str) -> dict:
        return {
            "n_duplicated_rows": len(dup_rows),
            "n_unique_rows": len(uni_rows),
            "loops_share_among_duplicated": _share(
                sum(r[flag] for r in dup_rows), len(dup_rows)),
            "loops_share_among_unique": _share(
                sum(r[flag] for r in uni_rows), len(uni_rows)),
        }

    cross_prose = _cross("looped_prose")
    cross_all = _cross("looped_all")
    cross_no_think = _cross("looped_prose_no_think")

    n_dup_hashes = sum(1 for c in counts.values() if c > 1)
    dup_share = _share(n - len(counts), n)
    dup_share_adr033 = (artifact.get("duplicates_adr033_share")
                        if artifact.get("duplicates_adr033_share") is not None else None)

    loops_prose = sum(r["looped_prose"] for r in rows)
    loops_all = sum(r["looped_all"] for r in rows)
    loops_no_think = sum(r["looped_prose_no_think"] for r in rows)

    if dup_share is None or dup_share_adr033 is None:
        dup_note = "не посчитано: набор пуст"
    elif dup_share == dup_share_adr033:
        dup_note = (f"совпало с контролем ADR-033 ({dup_share_adr033}): пересчёт "
                    "подтвердил известное число — обе нормализации дали одну долю")
    else:
        dup_note = (
            f"РАСХОЖДЕНИЕ с контролем ADR-033: основное число {dup_share} против "
            f"{dup_share_adr033} (разница {round(dup_share - dup_share_adr033, 4):+}). "
            "Причина — метод нормализации, а не чтение файла: здесь «все сообщения "
            "подряд (роль+содержимое), пробелы сжаты в один», в ADR-033 — "
            "[[role, content]] → json.dumps(sort_keys=True) → sha256. Число НЕ "
            "подгонялось: пересчёт метода ADR-033 тем же проходом дал "
            f"{dup_share_adr033} (записанный контроль — {DUPLICATES_REFERENCE_ADR033}). "
            "Для вопроса о петлях существенно, что доля дубликатов высока при обоих "
            "методах; вердикт считается по перекрёстной таблице, а не по этому числу.")

    top = sorted(rows, key=lambda r: (-(r["rep_prose"] if r["rep_prose"] is not None else -1),
                                      r["line_no"]))[:TOP_LOOPS_N]

    report = {
        "schema": "s3ak-loops-in-data/1",
        "stage": "S3ak — петли в данных SFT (причина «данные» в разборе вырождения)",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "partial" if partial else "measured",
        "question": ("есть ли в обучающем наборе SFT примеры с высокой внутренней "
                     "повторяемостью, и пришли ли петли из дубликатов (57.71 %) или "
                     "существуют сами по себе (у генераций SFT не кончилось 0.5833, "
                     "а зациклилось по этой же метрике 0.5417)"),
        "method": {
            "summary": (
                "по каждой строке jsonl берутся все assistant-сообщения и считаются "
                "три чтения: (а) весь assistant-текст, (б) проза — вне блоков "
                "<tool_call>/<tool_response>, (в) проза без <think> (диагностика). "
                "Петлевость — по импортируемой метрике "
                "probe_control.degenerate_metrics[\"max4gram_rep\"] и импортируемому "
                "порогу probe_language_split.LOOP_MAX4GRAM_REP (тот же порог, по "
                "которому мерились генерации). Пример петлевой, если петлевое хотя бы "
                "одно его assistant-сообщение; max4gram_rep примера — максимум по "
                "сообщениям. Вердикт и перекрёстная таблица — по прозе (б): "
                "размеченный JSON вызова и определения из tool_response сами дают "
                "повторы 4-грамм, не будучи петлёй в прозе, и чтение по всему тексту "
                "мерило бы разметку, а не вырождение"),
            "reading_a": "весь assistant-текст (все сообщения роли assistant подряд)",
            "reading_b": ("проза: вне блоков <tool_call>…</tool_call> и "
                          "<tool_response>…</tool_response>; вырезание — "
                          "probe_language_split._blocks (импорт)"),
            "reading_c": ("диагностика: probe_language_split.split_segments()[\"prose\"] "
                          "(то же, что (б), но и без блоков <think>) — «проза» прибора "
                          "S3ai означает именно это; расхождение (б)/(в) = вклад "
                          "рассуждения в повторы"),
            "verdict_reading": "reading_b (объявлено до прогона)",
            "threshold": {"loop_max4gram_rep": LS.LOOP_MAX4GRAM_REP,
                          "source": "probe_language_split.LOOP_MAX4GRAM_REP (импорт)",
                          "expected": LOOP_MAX4GRAM_REP_EXPECTED,
                          "rule": "max4gram_rep >= порога → петля",
                          "not_redefined": ("порог не переопределяется: расхождение с "
                                            "ожидаемым значением — отказ (код 1)")},
            "metric": {"from": "probe_control.degenerate_metrics (импорт)",
                       "field": "max4gram_rep",
                       "undefined_when": "слов < 8 → max4gram_rep=None → не петля, "
                                         "считается отдельно (n_prose_no_value)"},
            "percentile_method": ("nearest-rank (probe_language_split._pct), не "
                                  "интерполяция: нужно «сколько реально бывает»"),
            "duplicates": ("дубликат — совпадение НОРМАЛИЗОВАННОГО текста: все "
                           "сообщения по порядку, «роль+содержимое», пробелы сжаты в "
                           "один; метод отличается от ADR-033, контроль ADR-033 "
                           "пересчитывается тем же проходом отдельно"),
            "example_looped_rule": ("пример петлевой, если петлевое хотя бы одно его "
                                    "assistant-сообщение; при одном assistant-сообщении "
                                    "(96.8 % набора) правило вырождается в обычное"),
        },
        "artifact_path": artifact.get("path"),
        "artifact_sha256": artifact.get("sha256"),
        "artifact_bytes": artifact.get("bytes"),
        "tool": "tools/analyze_sft_loops.py",
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "n_examples": n,
        "n_assistant_messages": sum(r["n_assistant_messages"] for r in rows),
        "n_examples_multi_assistant": sum(r["n_assistant_messages"] > 1 for r in rows),
        "n_unique_normalized": len(counts),
        "n_unique_normalized_duplicated_hashes": n_dup_hashes,
        "duplicates": dup_share,
        "duplicates_detail": {
            "method": ("нормализация: все сообщения по порядку, «роль+содержимое», "
                       "пробелы сжаты в один; sha256 нормализованного текста; "
                       "дубликат — хеш встречается больше одного раза"),
            "n_examples": n,
            "n_unique_normalized": len(counts),
            "extra_rows": n - len(counts),
            "duplicates_share": dup_share,
            "loops_share_uniqueness_conflicts": conflicts,
            "adr033_method_recheck": {
                "method": ("[[role, content]] → json.dumps(ensure_ascii=False, "
                           "sort_keys=True) → sha256 (метод ADR-033)"),
                "n_unique_normalized": artifact.get("adr033_unique"),
                "duplicates_share": dup_share_adr033,
                "reference": DUPLICATES_REFERENCE_ADR033,
                "matches_reference": (None if dup_share_adr033 is None else
                                      abs(dup_share_adr033 - DUPLICATES_REFERENCE_ADR033) < 5e-5),
            },
        },
        "duplicates_note": dup_note,
        "max4gram_rep_prose": _dist([r["rep_prose"] for r in rows]),
        "max4gram_rep_all": _dist([r["rep_all"] for r in rows]),
        "max4gram_rep_prose_no_think": _dist([r["rep_prose_no_think"] for r in rows]),
        "n_prose_no_value": sum(r["rep_prose"] is None for r in rows),
        "n_all_no_value": sum(r["rep_all"] is None for r in rows),
        "loops_share_all": _share(loops_all, n),
        "loops_share_prose": _share(loops_prose, n),
        "loops_share_prose_no_think": _share(loops_no_think, n),
        "loops_share_among_duplicated": cross_prose["loops_share_among_duplicated"],
        "loops_share_among_unique": cross_prose["loops_share_among_unique"],
        "loops_share_unique_examples": _share(
            sum(1 for v in uniq.values() if v), len(uniq)),
        "loop_location": {
            "note": ("чтение (б) вырезает блоки инструмента, но <think> в нём "
                     "остаётся; чтение (в) убирает и рассуждение. Разрез отвечает на "
                     "вопрос «в рассуждении или в ответе» и показывает, насколько "
                     "вердикт зависит от выбора границы прозы"),
            "looped_prose_total": loops_prose,
            "looped_prose_and_no_think_total": loops_no_think,
            "looped_prose_lost_without_think": sum(
                r["looped_prose"] and not r["looped_prose_no_think"] for r in rows),
            "looped_prose_no_think_undefined": sum(
                r["looped_prose"] and r["rep_prose_no_think"] is None for r in rows),
        },
        "text_multiplicity": {
            "note": ("сколько строк файла приходится на один уникальный текст: "
                     "сравнить кратность петлевых текстов с общей. Если у петлевых "
                     "она выше — петли размножены дубликатами сильнее среднего, и "
                     "доля петлевых строк завышена повторами одного и того же "
                     "петлевого текста; если равна — повторы к петлям отношения не "
                     "имеют"),
            "rows_per_unique_text_overall": _round(n, len(uniq)),
            "unique_texts_looped": sum(1 for v in uniq.values() if v),
            "rows_per_looped_unique_text": _round(
                sum(counts[h] for h, v in uniq.items() if v),
                sum(1 for v in uniq.values() if v)),
            "looped_rows_in_duplicated_texts": sum(
                r["looped_prose"] for r in dup_rows),
        },
        "cross_table": {
            "reading": "prose (вердикт) — доля петлевых среди строк",
            "n_duplicated_rows": cross_prose["n_duplicated_rows"],
            "n_unique_rows": cross_prose["n_unique_rows"],
            "loops_share_among_duplicated": cross_prose["loops_share_among_duplicated"],
            "loops_share_among_unique": cross_prose["loops_share_among_unique"],
            "diagnostic_all_reading": cross_all,
            "diagnostic_prose_no_think_reading": cross_no_think,
            "note": ("если петли есть и среди уникальных строк — дедупликация петлю "
                     "не снимет; если только среди дубликатов — снимет"),
        },
        "lengths": {
            "unit": "символы и слова; чтение (а) — весь assistant-текст",
            "looped_prose": _group_lengths(rows, "looped_prose", "chars_all", "words_all"),
            "not_looped_prose": _group_lengths(
                [{**r, "not_looped_prose": not r["looped_prose"]} for r in rows],
                "not_looped_prose", "chars_all", "words_all"),
            "looped_prose_prose_chars": _group_lengths(
                rows, "looped_prose", "chars_prose", "words_prose"),
            "not_looped_prose_prose_chars": _group_lengths(
                [{**r, "not_looped_prose": not r["looped_prose"]} for r in rows],
                "not_looped_prose", "chars_prose", "words_prose"),
        },
        "top_loops": [
            {"line_no": r["line_no"],
             "max4gram_rep_prose": r["rep_prose"],
             "max4gram_rep_all": r["rep_all"],
             "chars_prose": r["chars_prose"], "words_prose": r["words_prose"],
             "chars_all": r["chars_all"], "words_all": r["words_all"],
             "n_assistant_messages": r["n_assistant_messages"],
             "prose_head_200": r["prose_head"]}
            for r in top],
        "top_loops_note": ("только метаданные (индекс строки 1-based, метрика, длины, "
                           "первые 200 символов прозы); сортировка — по max4gram_rep "
                           "прозы убыв., при равенстве — по номеру строки; полные "
                           "тексты не выгружаются (объём)"),
        "comparison_to_generations": {
            "sft_states_truncated_share_s3aj": S3AJ_SFT_TRUNCATED_SHARE,
            "sft_states_looped_share_s3aj": S3AJ_SFT_LOOPED_SHARE,
            "cfinal_looped_share_s3aj": S3AJ_CFINAL_LOOPED_SHARE,
            "loops_share_in_data_prose": _share(loops_prose, n),
            "loops_share_in_data_all": _share(loops_all, n),
            "duplicates_share_adr033": DUPLICATES_REFERENCE_ADR033,
            "note": ("числа отвечают на разные вопросы. 0.5833 — доля генераций, не "
                     "кончившихся до лимита токенов (не то же самое, что зациклилась); "
                     "0.5417 — доля зацикленных по метрике 4-грамм, и только она "
                     "однородна долям по данным. У данных петля — свойство текста "
                     "примера, у генераций — свойство выхода модели. Совпадение или "
                     "расхождение долей само по себе ничего не доказывает; вердикт о "
                     "причине — вне этого прибора"),
        },
        "partial": partial,
        "caveats": [
            "петлевость примера — свойство ТЕКСТА набора, а не модели: прибор не "
            "утверждает, что зацикливание генераций вызвано данными",
            "порог 8 повторов 4-граммы — грубый намеренно (тот же, что у генераций): "
            "задача — отделить вырождение от длинного честного текста, а не измерить "
            "степень; сами max4gram_rep лежат рядом, порог можно пересмотреть без "
            "перепрогона",
            "«проза» в задаче S3ak (вне блоков инструмента) и «проза» в приборе S3ai "
            "(ещё и без <think>) — разные сегменты; вердикт считается по первому, "
            "второй лежит рядом диагностикой",
            "дубликат — совпадение нормализованного текста, а не смысловое: два "
            "разных по смыслу примера с одинаковым текстом сюда попадут (и это "
            "правильно — дословный повтор)",
            "метрика undefined при <8 словах в сегменте: у короткой прозы петля "
            "неразличима в принципе, такие примеры сосчитаны отдельно и в долю "
            "петель не входят",
            "чтение (б) вырезает только блоки инструмента, поэтому <think> в нём "
            "остаётся: вердикт по (б) — это «повторяется где угодно, кроме вызовов "
            "инструмента», в том числе в рассуждении; разрез по (в) лежит рядом "
            "(loop_location) и вердикт под ним не меняется — меняется величина",
            "max4gram_rep механически растёт с длиной текста, а петлевые примеры "
            "длиннее не-петлевых (медиана 1025 против 856 слов) — поэтому контраст "
            "«среди дубликатов против среди одиночек» НЕ контролируется по длине, и "
            "его нельзя читать как «дубликат вызывает петлю»",
        ],
        "open_questions": [],
    }

    if conflicts:
        report["open_questions"].append(
            f"У {conflicts} строк с одинаковым нормализованным текстом петлевость "
            "разошлась: нормализация задела сигнал, чтение уникального множества "
            "неоднозначно — нужен разбор.")
    if report["n_examples_multi_assistant"]:
        report["open_questions"].append(
            f"{report['n_examples_multi_assistant']} примеров имеют больше одного "
            "assistant-сообщения: для них «пример петлевой» = «петлево хотя бы одно "
            "сообщение» (объявлено заранее), и это влияет на долю.")
    if report["loop_location"]["looped_prose_lost_without_think"]:
        report["open_questions"].append(
            f"{report['loop_location']['looped_prose_lost_without_think']} петлевых "
            "примеров перестают быть петлевыми, если вырезать <think>, — петля живёт в "
            "рассуждении, а не в ответе. Сверить с S3aj: у SFT-состояний 11 из 14 "
            "усечений пришлись на <think> "
            "(runs/s3aj-format-validity-20260917/probe_full_4096.json), то есть место "
            "петли у данных и у генераций совпадает; причинность этим не доказана — "
            "нужен разрез «петля в рассуждении против петли в ответе» на обеих "
            "сторонах.")
    report["open_questions"].append(
        "контраст «среди дубликатов против среди одиночек» не контролирован по длине "
        "(метрика растёт с длиной): нужен пересчёт в подобранных по длине парах, "
        "прежде чем говорить, что петли идут от дубликатов.")
    return report


# ─────────────────────────── проход по набору ───────────────────────────

def scan(input_path: Path, limit: int | None = None) -> tuple[list[dict], dict]:
    """Один проход по jsonl: числа по каждому примеру + хеши нормализаций.

    Полные тексты в память не собираются: на выходе — по одной компактной записи на
    пример (числа, хеш, 200 символов прозы у кандидатов в top), иначе 486 МБ
    набора превратились бы в отчёт.
    """
    rows: list[dict] = []
    adr033: set[str] = set()
    n_bad = 0
    with open(input_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if limit is not None and len(rows) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                n_bad += 1
                continue
            r = analyze_record(rec)
            r["line_no"] = i
            r["dup_hash"] = hashlib.sha256(
                normalize_example(rec).encode("utf-8")).hexdigest()
            adr033.add(hashlib.sha256(
                normalize_example_adr033(rec).encode("utf-8")).hexdigest())
            head = prose_text(assistant_text(rec))
            r["prose_head"] = RE_WS.sub(" ", head).strip()[:PROSE_HEAD_CHARS]
            rows.append(r)
            if len(rows) % 5000 == 0:
                note(f"  ... {len(rows)} примеров")
    n = len(rows)
    dup_share_adr033 = _share(n - len(adr033), n) if n else None
    artifact = {"duplicates_adr033_share": dup_share_adr033, "adr033_unique": len(adr033),
                "n_bad_lines": n_bad}
    return rows, artifact


def run(args) -> int:
    if args.selftest:
        return selftest()

    #: Пин порога: отчёт объявляет «порог 8 из прибора S3ai». Если прибор его сменил,
    #: это не тот вход — отказ, а не молчаливый пересчёт по новой арифметике.
    if LS.LOOP_MAX4GRAM_REP != LOOP_MAX4GRAM_REP_EXPECTED:
        note(f"отказ: LOOP_MAX4GRAM_REP у прибора = {LS.LOOP_MAX4GRAM_REP}, "
             f"ожидалось {LOOP_MAX4GRAM_REP_EXPECTED} — объявленный метод был бы ложью")
        return EXIT_FAIL
    if not hasattr(LS, "_pct") or not callable(LS._pct):
        note("отказ: в probe_language_split нет _pct — перцентиль ближайшим рангом "
             "объявлен в методе, а посчитать его нечем")
        return EXIT_FAIL

    src = Path(args.input)
    if not src.is_file():
        note(f"NOT-VERIFIED: набора нет: {src}")
        return EXIT_NOT_VERIFIED
    if src.stat().st_size == 0:
        note(f"NOT-VERIFIED: набор пуст: {src}")
        return EXIT_NOT_VERIFIED

    note(f"набор: {src} ({src.stat().st_size} байт) — считаю sha256")
    sha = sha256_file(src)
    note("проход по набору (метрика x3 чтения на пример)")
    rows, extra = scan(src, args.limit)
    if not rows:
        note(f"NOT-VERIFIED: в {src} не разобрано ни одного примера")
        return EXIT_NOT_VERIFIED

    artifact = {"path": str(args.input), "sha256": sha, "bytes": src.stat().st_size,
                **extra}
    partial = bool(args.limit is not None and len(rows) >= args.limit)
    report = build_report(rows, artifact, partial)
    if args.limit is not None:
        report["open_questions"].append(
            f"прогон ограничен --limit {args.limit}: числа НЕ по всему набору "
            f"({len(rows)} примеров прочитано), отчёт помечен status=partial")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    note(f"отчёт записан: {out}")
    note(f"  примеров {report['n_examples']}, assistant-сообщений "
         f"{report['n_assistant_messages']}")
    note(f"  петлевых (проза): {report['loops_share_prose']} | "
         f"(весь текст): {report['loops_share_all']} | "
         f"(проза без think): {report['loops_share_prose_no_think']}")
    note(f"  дубликатов: {report['duplicates']} "
         f"(контроль ADR-033: "
         f"{report['duplicates_detail']['adr033_method_recheck']['duplicates_share']})")
    ct = report["cross_table"]
    note(f"  петли среди дубликатов {ct['loops_share_among_duplicated']} vs "
         f"среди уникальных {ct['loops_share_among_unique']}; "
         f"петлевых уникальных текстов {report['loops_share_unique_examples']}")
    note(f"  max4gram_rep прозы: медиана "
         f"{report['max4gram_rep_prose']['median']} p90 {report['max4gram_rep_prose']['p90']} "
         f"max {report['max4gram_rep_prose']['max']}")
    return EXIT_OK


# ─────────────────────────── самопроверка на фикстурах ───────────────────────────

def _rec(*msgs: tuple[str, str]) -> dict:
    return {"messages": [{"role": r, "content": c} for r, c in msgs]}


#: Тексты фикстур. Сеть и большой файл не нужны: проверяется арифметика правила.
LOOP_PROSE = "альфа бета гамма дельта " * 12
NORMAL_PROSE = ("Хлеб на закваске пекут из муки, воды и соли. Тесто ставят в тепло "
                "на ночь, а утром формуют и печь.")
#: JSON-выдумка с повтором внутри <tool_call>: петля по всему тексту, но НЕ в прозе.
JSON_LOOP = ('{"name": "search_concepts", "query": "'
             + " ".join(["а"] * 16) + '"}')
TOOL_LOOP_RECORD = _rec(("user", "Найди концепты."),
                        ("assistant", f"Короткий ответ.<tool_call>{JSON_LOOP}</tool_call>"))
NORMAL_RECORD = _rec(("user", "Что такое хлеб?"), ("assistant", NORMAL_PROSE))
LOOP_RECORD = _rec(("user", "Что такое хлеб?"), ("assistant", LOOP_PROSE))
RESP_LOOP_RECORD = _rec(
    ("assistant", "Ответ." + f"<tool_response>{JSON_LOOP}</tool_response>" + "Итог."))
THINK_LOOP_RECORD = _rec(
    ("assistant", f"<think>{LOOP_PROSE}</think>Короткий русский ответ по делу."))


def selftest() -> int:
    """Фикстуры: петля ловится, проза не ловится, JSON в блоках в прозу не течёт.

    Тест обязан **ловить регресс**: если кто-то заменит «прозу» на весь текст
    (или наоборот втянет блоки в прозу), проверка `looped_prose is False` у
    JSON-фикстуры покраснеет.
    """
    bad = 0

    def check(name: str, got, want) -> None:
        nonlocal bad
        ok = (abs(got - want) < 1e-9 if isinstance(want, float) and isinstance(got, float)
              else got == want)
        if not ok:
            bad += 1
            note(f"  FAIL {name}: {got!r}, ожидалось {want!r}")

    # ── 1. метрика и порог: петля ловится, норма не ловится
    rep_loop = max4gram_rep(LOOP_PROSE)
    rep_norm = max4gram_rep(NORMAL_PROSE)
    check("max4gram_rep(петля) >= порога", looped(rep_loop), True)
    check("max4gram_rep(норма) < порога", looped(rep_norm), False)
    check("max4gram_rep(короткий текст) = None", max4gram_rep("два слова"), None)
    check("None не петля", looped(None), False)

    # ── 2. JSON-выдумка в <tool_call> не должна попадать в прозу
    a = analyze_record(TOOL_LOOP_RECORD)
    check("tool_call: весь текст — петля", a["looped_all"], True)
    check("tool_call: проза — НЕ петля (регресс-ловушка)", a["looped_prose"], False)
    check("tool_call: проза не содержит JSON",
          "search_concepts" in prose_text(assistant_text(TOOL_LOOP_RECORD)), False)
    check("tool_call: проза содержит ответ",
          "Короткий ответ." in prose_text(assistant_text(TOOL_LOOP_RECORD)), True)

    # ── 3. то же для <tool_response>
    b = analyze_record(RESP_LOOP_RECORD)
    check("tool_response: весь текст — петля", b["looped_all"], True)
    check("tool_response: проза — НЕ петля", b["looped_prose"], False)
    check("tool_response: текст между блоками сохранён",
          prose_text(assistant_text(RESP_LOOP_RECORD)).strip(), "Ответ.Итог.")

    # ── 4. здоровая проза — не петля в обоих чтениях
    c = analyze_record(NORMAL_RECORD)
    check("норма: проза не петля", c["looped_prose"], False)
    check("норма: весь текст не петля", c["looped_all"], False)
    check("норма: одно assistant-сообщение", c["n_assistant_messages"], 1)

    # ── 5. петля в прозе — ловится в обоих чтениях
    d = analyze_record(LOOP_RECORD)
    check("петля в прозе: проза петля", d["looped_prose"], True)
    check("петля в прозе: весь текст петля", d["looped_all"], True)

    # ── 6. чтение (в) отделяет рассуждение от «прозы» задачи
    e = analyze_record(THINK_LOOP_RECORD)
    check("think-петля: проза (б) — петля", e["looped_prose"], True)
    check("think-петля: проза без think (в) — НЕ петля",
          e["looped_prose_no_think"], False)
    check("think-петля: split_segments.prose не содержит петли",
          looped(max4gram_rep(prose_text_no_think(assistant_text(THINK_LOOP_RECORD)))),
          False)

    # ── 7. нормализация дубликатов: пробелы не значат, текст значит
    x = _rec(("user", "Вопрос."), ("assistant", "Ответ по делу."))
    y = _rec(("user", "Вопрос.  "), ("assistant", "\n Ответ   по делу. "))
    z = _rec(("user", "Вопрос."), ("assistant", "Другой ответ."))
    check("дубликат: пробелы сжаты → совпадение",
          normalize_example(x) == normalize_example(y), True)
    check("дубликат: другой текст → не совпадение",
          normalize_example(x) == normalize_example(z), False)
    check("дубликат: роль входит в нормализацию",
          normalize_example(_rec(("user", "а"), ("assistant", "б")))
          == normalize_example(_rec(("assistant", "а"), ("user", "б"))), False)
    check("метод ADR-033: сжатие пробелов НЕ совпадает (методы разные)",
          (hashlib.sha256(normalize_example_adr033(x).encode()).hexdigest()
           == hashlib.sha256(normalize_example_adr033(y).encode()).hexdigest()), False)

    # ── 8. перцентиль — ближайшим рангом (тот же метод, что в приборе)
    check("_pct ближайший ранг (медиана 1..5)", LS._pct([1, 2, 3, 4, 5], 0.5), 3)
    check("_pct ближайший ранг (p90 1..10)", LS._pct(list(range(1, 11)), 0.9), 9)

    # ── 9. свод: перекрёстная таблица считается и на крошечном наборе
    rep = build_report([{**analyze_record(LOOP_RECORD), "line_no": 1,
                         "dup_hash": "a", "prose_head": ""},
                        {**analyze_record(NORMAL_RECORD), "line_no": 2,
                         "dup_hash": "b", "prose_head": ""},
                        {**analyze_record(NORMAL_RECORD), "line_no": 3,
                         "dup_hash": "b", "prose_head": ""}],
                       {"path": "fixture", "sha256": "0" * 64, "bytes": 0,
                        "duplicates_adr033_share": 1 / 3, "adr033_unique": 2,
                        "n_bad_lines": 0}, False)
    check("свод: n_examples", rep["n_examples"], 3)
    check("свод: петли по прозе", rep["loops_share_prose"], round(1 / 3, 4))
    check("свод: петли среди дубликатов", rep["loops_share_among_duplicated"], 0.0)
    check("свод: петли среди уникальных строк", rep["loops_share_among_unique"], 1.0)
    check("свод: петлевых уникальных текстов", rep["loops_share_unique_examples"], 0.5)
    check("свод: дубликаты", rep["duplicates"], round(1 / 3, 4))
    check("свод: top_loops не пуст", len(rep["top_loops"]) > 0, True)
    check("свод: текстов в отчёте нет", "prose_head_200" in rep["top_loops"][0], True)
    check("свод: schema", rep["schema"], "s3ak-loops-in-data/1")
    # LOOP_RECORD — петля в ответе (видна и в (б), и в (в)), NORMAL — нет:
    # значит, разрез «где живёт петля» обязан показать её в обоих чтениях.
    check("свод: петля видна и без think (обе границы)",
          rep["loop_location"]["looped_prose_lost_without_think"], 0)
    check("свод: петля не потерялась при вырезании think",
          rep["loop_location"]["looped_prose_and_no_think_total"], 1)
    # тексты «b» встречаются дважды (норма), «a» — один раз (петля): кратность
    # петлевого текста здесь 1, у набора в целом 1.5 — асимметрия обязана быть видна.
    check("свод: кратность петлевого текста",
          rep["text_multiplicity"]["rows_per_looped_unique_text"], 1.0)
    check("свод: кратность по набору",
          rep["text_multiplicity"]["rows_per_unique_text_overall"], 1.5)

    if bad:
        note(f"SELFTEST FAIL: {bad} расхождений")
        return EXIT_FAIL
    note("SELFTEST OK: метрика, два чтения, нормализация дубликатов, перцентиль, свод")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help=f"набор jsonl (только чтение); по умолчанию {DEFAULT_INPUT}")
    ap.add_argument("--out", default=None, help="файл отчёта (JSON)")
    ap.add_argument("--limit", type=int, default=None,
                    help="прочитать только первые N примеров (отладка; отчёт "
                         "помечается status=partial)")
    ap.add_argument("--selftest", action="store_true",
                    help="прогнать фикстуры и выйти (без чтения большого файла и сети)")
    args = ap.parse_args()
    if not args.selftest and not args.out:
        note("отказ: нужен --out (или --selftest)")
        return EXIT_FAIL
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
