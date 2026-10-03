#!/usr/bin/env python3
"""S3af-2/3 — пилот перевода `<think>` на русский и числовая проверка результата.

Зачем. S3ac (``evidence/s3ac-corpus-language.json``) показал: носитель английского —
не корпус целиком (кириллица потока 0.5916), а **позиция генерации** — первые 200
символов хода assistant дают 0.0003 при 0.6402 в ответной части того же хода.
В SFT-наборе ``sft_train_v12.jsonl`` рассуждение — 139.8M символов (46.1 % ответов
assistant), кириллица внутри ``<think>`` 0.1927 при 0.6570 в ответной части, и
``<think>`` **входит в лосс** (маска снимается только с system/user и
``<tool_response>``). Маскирование ``<think>`` отменено владельцем (ADR-037);
лечение — данные. Этот прибор делает пилот (``translate``) и проверяет его числами
(``verify``), а не «похоже на русский».

Режимы::

    translate — перевести рассуждения N записей, сохранив всё остальное байт в байт
    verify    — механические и числовые проверки пилота против источника
    report    — свод evidence/s3af-russian-thinks.json из teachers.json + пилота

Контракт сохранности (нарушение = FAIL, а не предупреждение):

* роли и порядок сообщений не меняются;
* ходы system/user копируются байт в байт;
* всё, что вне ``<think>`` в ходе assistant (ответная часть, ``<tool_call>``,
  ``<tool_response>``), копируется байт в байт — включая аргументы инструментов;
* ``<tool_call>...</tool_call>`` **внутри** ``<think>`` не отправляется учителю
  (защищённая вставка ⟦ZZi⟧), поэтому JSON аргументов не может быть переписан;
* парность маркеров сохраняется: счётчики ``<think>``/``</think>`` и
  ``<tool_call>``/``</tool_call>``/``<tool_response>``/``</tool_response>`` совпадают
  с источником;
* число ``<think>``-фрагментов и длины вне ``<think>`` совпадают с источником.

Пробелы по краям фрагмента (``\\n`` после ``<think>``) снимаются перед отправкой и
возвращаются после — так возврат учителя «голым переводом» не сдвигает формат.

Коды возврата::

    0 — режим отработал, контракт сохранности цел
    1 — FAIL: контракт сохранности нарушен (проверка нашла расхождение)
    2 — NOT-VERIFIED: нет входа (SFT-набор, файл пилота, учитель)

Запуск::

    python3 tools/russian_think_pilot.py --mode translate --n 100 --teacher http ... \
        --out runs/s3af-think-pilot-20260917/
    python3 tools/russian_think_pilot.py --mode verify \
        --pilot runs/s3af-think-pilot-20260917/pilot.jsonl \
        --out runs/s3af-think-pilot-20260917/
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

GB10_SHARED = Path("/home/user/gb10-shared")
DEFAULT_SFT = GB10_SHARED / "datasets/sft_train_v12.jsonl"
DEFAULT_TOKENIZER = Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B"

MARKERS = ["<think>", "</think>", "<tool_call>", "</tool_call>",
           "<tool_response>", "</tool_response>"]
RE_THINK_PAIR = re.compile(r"<think>(.*?)</think>", re.S)
RE_TOOLCALL_PAIR = re.compile(r"<tool_call>(.*?)</tool_call>", re.S)
RE_ANY_PAIR = re.compile(r"<think>.*?</think>|<tool_call>.*?</tool_call>"
                         r"|<tool_response>.*?</tool_response>", re.S)
PLACEHOLDER = "⟦ZZ{i}⟧"

#: Квантиль длины, ниже которой рассуждение считается «оставшимся английским» даже
#: если это не ноль: у SFT-набора кириллица `<think>` = 0.1927, то есть «почти ноль»
#: неотличимо от «смешанный». Порог — по большинству букв, как REASONING_RU_MIN у S3ac.
FRAGMENT_RU_MIN = 0.5
#: Разумные пределы длины перевода относительно источника (русский длиннее, но не вдвое).
LEN_RATIO_BOUNDS = (0.4, 2.5)
#: Доля записей, у которых парность маркеров и парсинг <tool_call> обязаны сойтись.
#: Не «выборочно»: проверяются все записи пилота, порог здесь — только для вердикта.
MARKER_PAIR_OK_MIN = 1.0

_LETTERS = {ord(c): "\x01" for c in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
                                  "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"}
_LETTERS.update({ord(c): "\x02" for c in "abcdefghijklmnopqrstuvwxyz"
                                      "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
RE_WORD = re.compile(r"[A-Za-z]+|[А-Яа-яЁё]")
RE_FENCE_WRAP = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.S)


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- метрики

def cyr_share(text: str) -> float:
    m = (text or "").translate(_LETTERS)
    c, l = m.count("\x01"), m.count("\x02")
    return c / max(c + l, 1)


def en_word_share(text: str) -> float:
    ru = en = 0
    for w in RE_WORD.findall(text or ""):
        if w[0].isascii():
            en += 1
        else:
            ru += 1
    return en / max(ru + en, 1)


def marker_counts(text: str) -> dict:
    return {m: (text or "").count(m) for m in MARKERS}


# ---------------------------------------------------------------- сегментация

def segment_assistant(content: str) -> list:
    """Ход assistant → [('think'|'other', текст)] с точным сохранением порядка.

    Режется только по **первому** ``</think>`` после ``<think>``: незакрытый
    ``<think>`` уводит остаток в ``other`` (он не переводится и не теряется).
    """
    segs, pos = [], 0
    while True:
        i = content.find("<think>", pos)
        if i < 0:
            segs.append(("other", content[pos:]))
            break
        j = content.find("</think>", i)
        if j < 0:
            segs.append(("other", content[pos:]))
            break
        segs.append(("other", content[pos:i]))
        segs.append(("think", content[i + len("<think>"):j]))
        pos = j + len("</think>")
    return segs


def join_segments(segs: list) -> str:
    """Обратная сборка: маркеры ``<think>``/``</think>`` возвращаются на место.

    Сегмент ``think`` хранит **внутренний** текст; сборка без возврата маркеров
    теряла бы их — это ловится проверкой парности, а не глазами.
    """
    return "".join(("<think>" + t + "</think>") if k == "think" else t for k, t in segs)


#: Любой одиночный маркер формата внутри переводимого текста. Нужен потому, что
#: маркеры в SFT-наборе **цитируются внутри рассуждения**: у 25.71 % ходов assistant
#: счётчики маркеров не сходятся (например ``<think>`` 6 против ``</think>`` 1), а
#: у 11.22 % фрагментов маркер встречается внутри текста. Отдать такое учителю
#: значит разрешить ему менять число маркеров в тексте — и получить либо потерянный
#: маркер, либо лишний. Поэтому внутри переводимой области защищается **каждое**
#: вхождение маркера, а не только настоящая пара вызова инструмента.
RE_MARKER_TOKEN = re.compile("|".join(re.escape(m) for m in MARKERS))


def protect_spans(text: str) -> tuple[str, dict]:
    """Спрятать от учителя всё, что обязано дойти байт в байт.

    Порядок важен: сначала целиком ``<tool_call>…</tool_call>`` (его тело — JSON
    аргументов, переводить запрещено), затем оставшиеся одиночные маркеры.
    """
    spans = {}

    def sub(m):
        key = PLACEHOLDER.format(i=len(spans))
        spans[key] = m.group(0)
        return key

    text = RE_TOOLCALL_PAIR.sub(sub, text)
    return RE_MARKER_TOKEN.sub(sub, text), spans


def restore_spans(text: str, spans: dict) -> str:
    for k, v in spans.items():
        text = text.replace(k, v)
    return text


#: Прежнее имя оставлено, чтобы вызывающий код не разъезжался с тестами.
protect_tool_calls = protect_spans
restore_tool_calls = restore_spans


def split_edges(text: str) -> tuple[str, str, str]:
    core = text.strip()
    if not core:
        return text, "", ""
    i = text.index(core[0])
    lead = text[:i]
    trail = text[i + len(core):]
    return core, lead, trail


#: Строка-разделитель, которой обрамлён фрагмент в промпте. Учитель иногда
#: возвращает её вместе с переводом — это не маркер формата, и контракт сохранности
#: такую утечку не ловит: она ломает текст, оставаясь «вне проверок». Поймана
#: содержательной выборочной проверкой пилота (3 пары из 10), а не автоматом.
RE_DELIM_LINE = re.compile(r"^\s*-{3,}\s*$")


def strip_delimiter_lines(text: str) -> tuple:
    """Снять строки-разделители по краям ответа: (текст, сколько снято)."""
    lines = text.split("\n")
    n = 0
    while lines and RE_DELIM_LINE.match(lines[0]):
        lines.pop(0)
        n += 1
    while lines and RE_DELIM_LINE.match(lines[-1]):
        lines.pop()
        n += 1
    return "\n".join(lines), n


def clean_output(text: str) -> tuple:
    """Снять обёртки, которые учитель добавляет вокруг перевода: (текст, сколько снято)."""
    t = (text or "").strip()
    m = RE_FENCE_WRAP.match(t)
    if m:
        t = m.group(1).strip()
    t, n = strip_delimiter_lines(t)
    for q in ('"', "«", "“"):
        if len(t) > 1 and t.startswith(q) and t.endswith({'"': '"', "«": "»", "“": "”"}[q]):
            t = t[1:-1].strip()
    t, extra = strip_delimiter_lines(t.strip())
    return t.strip(), n + extra


# ------------------------------------------------------------------ учитель

def http_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


TRANSLATE_SYSTEM = (
    "Ты — переводчик технических текстов по машинному обучению и ИИ. "
    "Ты переводишь фрагменты рассуждений агента с английского на русский."
)

TRANSLATE_USER = """Переведи на русский язык фрагмент рассуждения из трассы ML/AI-агента.

Требования:
1. Передай смысл полностью и точно. Ничего не добавляй, не сокращай, не упрощай, не объясняй.
2. Сохрани структуру: абзацы, пустые строки, списки, нумерацию, markdown (**жирный**, `код`, ```блоки```), заголовки.
3. Оставь без перевода и без изменений: slug-и и идентификаторы концептов (например `mr_molar_refractivity`), имена функций, полей и инструментов, латинские аббревиатуры (GRPO, SFT, KL, PPL), числа, формулы, URL.
4. Если в тексте встречаются вставки вида ⟦ZZ0⟧ — оставь их ровно на месте, не меняя и не удаляя. Никаких угловых тегов (любых слов в угловых скобках) в ответ не добавляй: их в переводе быть не должно.
5. Ответь ТОЛЬКО переводом. Без пояснений, без собственных рассуждений, без кавычек вокруг всего текста.

Фрагмент:
---
{fragment}
---"""


def translate_chunk(teacher: dict, text: str, spans: dict, timeout: float) -> dict:
    """Один вызов учителя. Возвращает {text, seconds, usage, error}."""
    core, lead, trail = split_edges(text)
    if not core:
        return {"text": text, "seconds": 0.0, "usage": {}, "skipped": "пусто"}
    user = TRANSLATE_USER.replace("{fragment}", core)
    payload = {
        "model": teacher["model"],
        "messages": [{"role": "system", "content": TRANSLATE_SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": teacher.get("temperature", 0.0),
        "max_tokens": teacher.get("max_tokens", 8192),
    }
    started = time.time()
    if teacher.get("kind") == "ollama":
        payload.pop("max_tokens", None)
        payload["stream"] = False
        payload["think"] = False
        payload["options"] = {"temperature": teacher.get("temperature", 0.0),
                              "num_ctx": teacher.get("num_ctx", 32768),
                              "num_predict": teacher.get("max_tokens", 8192)}
        url = teacher["endpoint"].rstrip("/") + "/api/chat"
        d = http_json(url, payload, timeout)
        msg = d.get("message", {}) or {}
        out = msg.get("content") or ""
        usage = {"completion_tokens": d.get("eval_count"),
                 "prompt_tokens": d.get("prompt_eval_count")}
    else:
        # Рассуждающий учитель по умолчанию тратит бюджет на цепочку рассуждений,
        # а перевод кладёт в content — при нехватке токенов content пуст. `--no-think`
        # выключает цепочку (llama.cpp: chat_template_kwargs.enable_thinking=false).
        # Сравнивать учителей можно только при ОДИНАКОВОЙ настройке рассуждений,
        # иначе меряется конфигурация, а не учитель.
        if teacher.get("no_think"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        url = teacher["endpoint"].rstrip("/") + "/v1/chat/completions"
        d = http_json(url, payload, timeout)
        ch = (d.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        out = msg.get("content") or ""
        usage = d.get("usage") or {}
        # Рассуждающие учителя (qwen3.8-27b) кладут цепочку в reasoning_content, а
        # перевод — в content. Если бюджета токенов не хватило, content пуст, а
        # finish_reason == "length": молча принять это нельзя — фрагмент остался бы
        # пустым. Пустой ответ на непустой вход — отказ, а не «перевод».
        if not out.strip() and core.strip():
            raise ValueError(
                f"пустой content (finish_reason={ch.get('finish_reason')}, "
                f"reasoning_chars={len(msg.get('reasoning_content') or '')}, "
                f"completion_tokens={(usage or {}).get('completion_tokens')})")
    dt = time.time() - started
    cleaned, stripped = clean_output(out)
    out = restore_tool_calls(cleaned, spans)
    return {"text": lead + out + trail, "seconds": dt, "usage": usage,
            "raw_chars": len(out), "delimiter_lines_stripped": stripped}


# --------------------------------------------------------------- translate

def parse_linenos(spec) -> list | None:
    """`--linenos` в список номеров строк источника (0-based). Пусто → None.

    Принимает «1,5,9» или путь к JSON-файлу со списком. Нужен, чтобы поставить
    пилот на ТЕ ЖЕ примеры, что другой пилот: `--n` даёт равномерную выборку по
    всему файлу, и у разных `n` она своя — 15 записей из 100 не подмножество
    (пересечение случайно и мало).
    """
    if not spec:
        return None
    p = Path(spec)
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("linenos") or raw.get("source_lines") or []
        items = raw
    else:
        items = [x for x in re.split(r"[,\s]+", spec) if x]
    out, seen = [], set()
    for x in items:
        i = int(x)
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def load_source_records(sft: Path, want: int, stride_start: int = 0,
                        linenos: list | None = None) -> list:
    """Записи SFT с непустым `<think>`; отбор равномерный по файлу, детерминированный.

    Не «первые N»: набор отсортирован по типу задачи, и первые N дали бы смещение
    по типу. Берётся каждая k-я запись с `<think>`, k подбирается так, чтобы
    набрать `want` штук по всему файлу.

    Если задан `linenos` — берутся ровно эти строки файла, в заданном порядке;
    `want` и `stride_start` при этом не действуют. Так пилот одного учителя
    ставится на те же примеры, что пилот другого (см. `parse_linenos`).
    """
    if linenos is not None:
        wanted = list(dict.fromkeys(int(x) for x in linenos))
        pool = set(wanted)
        by_line = {}
        with sft.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i in pool:
                    by_line[i] = line.rstrip("\n")
        out = []
        for i in wanted:
            line = by_line.get(i)
            if line is None:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            asst = [m for m in rec.get("messages", []) if m.get("role") == "assistant"]
            if not asst or not RE_THINK_PAIR.search(asst[0].get("content") or ""):
                continue
            out.append({"lineno": i, "line": line})
        return out

    cand = []
    with sft.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < stride_start:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            asst = [m for m in rec.get("messages", []) if m.get("role") == "assistant"]
            if not asst or not RE_THINK_PAIR.search(asst[0].get("content") or ""):
                continue
            cand.append((i, line.rstrip("\n")))
    if len(cand) <= want:
        return [{"lineno": i, "line": l} for i, l in cand]
    step = len(cand) / want
    return [{"lineno": cand[min(len(cand) - 1, int(k * step))][0],
             "line": cand[min(len(cand) - 1, int(k * step))][1]} for k in range(want)]


def do_translate(args) -> int:
    sft = Path(args.sft_jsonl)
    if not sft.exists():
        note(f"NOT-VERIFIED: нет SFT-набора {sft}")
        return EXIT_NOT_VERIFIED
    if not args.teacher_model or not args.teacher_endpoint:
        note("NOT-VERIFIED: не задан учитель (--teacher-endpoint/--teacher-model)")
        return EXIT_NOT_VERIFIED

    teacher = {"kind": args.teacher_kind, "endpoint": args.teacher_endpoint,
               "model": args.teacher_model, "temperature": args.temperature,
               "max_tokens": args.max_tokens, "num_ctx": args.num_ctx,
               "no_think": args.no_think}

    digest_before = sha256_file(sft)
    records = load_source_records(sft, args.n, args.stride_start,
                                  parse_linenos(getattr(args, "linenos", "")))
    if not records:
        note("NOT-VERIFIED: в SFT-наборе нет записей с <think>")
        return EXIT_NOT_VERIFIED

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    pilot_path = outdir / "pilot.jsonl"
    raw_path = outdir / "translations.jsonl"

    n_frag = n_calls = 0
    seconds = 0.0
    chars_in = chars_out = 0
    failures = []
    call_rows_all = []
    rejected = []          # ответы, забракованные стражей возврата (маркеры разошлись)
    t0 = time.time()
    with pilot_path.open("w", encoding="utf-8") as fp, raw_path.open("w", encoding="utf-8") as fr:
        for k, rec in enumerate(records):
            src = json.loads(rec["line"])
            msgs_out = []
            call_rows = []
            for mi, m in enumerate(src.get("messages", [])):
                if m.get("role") != "assistant":
                    msgs_out.append(dict(m))
                    continue
                content = m.get("content") or ""
                segs = segment_assistant(content)
                new_segs = []
                for si, (kind, text) in enumerate(segs):
                    if kind != "think":
                        new_segs.append((kind, text))
                        continue
                    protected, spans = protect_tool_calls(text)
                    n_frag += 1
                    try:
                        r = translate_chunk(teacher, protected, spans, args.timeout)
                    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
                        failures.append({"lineno": rec["lineno"], "seg": si,
                                         "error": f"{type(e).__name__}: {e}"})
                        new_segs.append((kind, text))  # фрагмент остаётся исходным
                        continue
                    n_calls += 1
                    seconds += r["seconds"]
                    chars_in += len(text)
                    # Стража возврата: учитель иногда дописывает угловые теги сам
                    # (промпт их называет) — тогда сборка сдвигает границы сегментов,
                    # и чужой текст уезжает из <think> наружу. Такой ответ не
                    # принимается: фрагмент остаётся исходным, а факт считается
                    # отбраковкой, а не «почти получилось».
                    if marker_counts(r["text"]) != marker_counts(text):
                        rejected.append({"lineno": rec["lineno"], "seg": si,
                                         "markers_in": marker_counts(text),
                                         "markers_out": marker_counts(r["text"])})
                        new_segs.append((kind, text))
                        continue
                    chars_out += len(r["text"])
                    new_segs.append((kind, r["text"]))
                    call_rows.append({
                        "lineno": rec["lineno"], "seg": si,
                        "chars_in": len(text), "chars_out": len(r["text"]),
                        "cyrillic_before": round(cyr_share(text), 4),
                        "cyrillic_after": round(cyr_share(r["text"]), 4),
                        "seconds": round(r["seconds"], 2),
                        "usage": r["usage"], "protected_spans": len(spans),
                        "sha_in": sha256_text(text)[:16], "sha_out": sha256_text(r["text"])[:16],
                    })
                nm = dict(m)
                nm["content"] = join_segments(new_segs)
                msgs_out.append(nm)
            new = dict(src)
            new["messages"] = msgs_out
            new["s3af"] = {
                "source_line": rec["lineno"],
                "source_sha256": sha256_text(rec["line"]),
                "teacher": teacher["model"],
                "date": now_iso(),
            }
            fp.write(json.dumps(new, ensure_ascii=False) + "\n")
            for row in call_rows:
                call_rows_all.append(row)
                fr.write(json.dumps(row, ensure_ascii=False) + "\n")
            # Сброс на каждой записи: учитель на 11 ток/с идёт десятки минут, и без
            # этого журнал не виден до заполнения буфера — «идёт или встал» не отличить.
            fp.flush()
            fr.flush()
            note(f"  [{k + 1}/{len(records)}] строка {rec['lineno']}, "
                 f"фрагментов {n_frag}, {time.time() - t0:.0f}с")

    digest_after = sha256_file(sft)
    report = {
        "schema": "s3af-pilot-translate/1",
        "stage": "S3af-2",
        "date": now_iso(),
        "mode": "translate",
        "teacher": {k: v for k, v in teacher.items()},
        "source": {"path": str(sft), "sha256_before": digest_before,
                   "sha256_after": digest_after,
                   "unchanged": digest_before == digest_after},
        "records": len(records),
        "selection": {
            "n": args.n,
            "stride_start": args.stride_start,
            "linenos_explicit": parse_linenos(getattr(args, "linenos", "")) is not None,
            "source_lines": [r["lineno"] for r in records],
            "note": "при явных --linenos набор совпадает с набором другого пилота "
                    "запись в запись; иначе --n даёт равномерную выборку по файлу",
        },
        "think_fragments": n_frag,
        "teacher_calls": n_calls,
        "failures": failures,
        "delimiter_lines_stripped": sum(
            c.get("delimiter_lines_stripped", 0) for c in call_rows_all),
        "rejected_marker_divergence": {
            "n": len(rejected),
            "note": "учитель дописал угловые теги сам — фрагмент оставлен исходным; "
                    "это отбраковка, а не молчаливая правка",
            "items": rejected[:20],
        },
        "chars_in": chars_in, "chars_out": chars_out,
        "seconds_total": round(seconds, 1),
        "chars_per_s": round(chars_out / max(seconds, 1e-6), 1),
        "artifacts": [{"path": str(pilot_path), "what": "пилотный набор (формат v12)"},
                      {"path": str(raw_path), "what": "пофрагментный журнал вызовов"},
                      {"path": "tools/russian_think_pilot.py", "what": "этот прибор"}],
    }
    (outdir / "translate-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    note(f"пилот: {pilot_path} ({len(records)} записей, {n_frag} фрагментов, "
         f"{chars_in}→{chars_out} симв., {seconds:.0f}с)")
    if digest_before != digest_after:
        note("FAIL: исходный SFT-набор изменился во время пилота")
        return EXIT_FAIL
    return EXIT_OK


# ------------------------------------------------------------------ verify

def parse_toolcalls(content: str) -> dict:
    """Парсится ли JSON внутри `<tool_call>...</tool_call>`."""
    total = ok = 0
    bad = []
    for m in RE_TOOLCALL_PAIR.finditer(content or ""):
        inner = m.group(1).strip()
        if not inner:
            continue
        total += 1
        try:
            json.loads(inner)
            ok += 1
        except ValueError as e:
            bad.append({"head": inner[:40], "error": str(e)[:80]})
    return {"total": total, "parsed": ok, "bad": bad[:3]}


def verify_record(src: dict, out: dict) -> dict:
    """Пошаговая сверка одной записи: что изменено, что обязано быть тем же."""
    res = {"checks": {}, "fragments": []}
    sm, om = src.get("messages", []), out.get("messages", [])
    res["checks"]["roles_equal"] = [m.get("role") for m in sm] == [m.get("role") for m in om]
    res["checks"]["n_messages_equal"] = len(sm) == len(om)
    res["checks"]["system_user_identical"] = all(
        a.get("content") == b.get("content")
        for a, b in zip(sm, om) if a.get("role") != "assistant")

    # ход assistant: посегментная сверка
    non_think_identical = True
    frag_pairs = []
    for a, b in zip(sm, om):
        if a.get("role") != "assistant":
            continue
        sa = segment_assistant(a.get("content") or "")
        sb = segment_assistant(b.get("content") or "")
        if len(sa) != len(sb):
            non_think_identical = False
            continue
        for (ka, ta), (kb, tb) in zip(sa, sb):
            if ka != kb:
                non_think_identical = False
            elif ka == "other":
                if ta != tb:
                    non_think_identical = False
            else:
                frag_pairs.append((ta, tb))
    res["checks"]["non_think_identical"] = non_think_identical
    res["checks"]["n_think_fragments_src"] = sum(
        1 for a in sm if a.get("role") == "assistant"
        for k, _ in segment_assistant(a.get("content") or "") if k == "think")
    res["checks"]["n_think_fragments_out"] = len(frag_pairs)

    csrc = "".join((a.get("content") or "") for a in sm if a.get("role") == "assistant")
    cout = "".join((b.get("content") or "") for b in om if b.get("role") == "assistant")
    res["checks"]["markers_src"] = marker_counts(csrc)
    res["checks"]["markers_out"] = marker_counts(cout)
    res["checks"]["markers_equal"] = res["checks"]["markers_src"] == res["checks"]["markers_out"]
    # JSON вызовов инструмента: в источнике он **не всегда** JSON (7.91 % пар
    # ``<tool_call>`` содержат прозу — модель цитирует формат). Требовать
    # «в выходе всё парсится» значило бы валить прибор за дефект источника.
    # Контракт: выход не парсится ХУЖЕ источника.
    res["checks"]["tool_call_json_src"] = parse_toolcalls(csrc)
    res["checks"]["tool_call_json_out"] = parse_toolcalls(cout)
    res["checks"]["tool_call_json_not_worse"] = (
        res["checks"]["tool_call_json_out"]["parsed"]
        >= res["checks"]["tool_call_json_src"]["parsed"]
        and res["checks"]["tool_call_json_out"]["total"]
        >= res["checks"]["tool_call_json_src"]["total"])

    # Повтор фрагмента внутри записи — не дефект сам по себе: если источник дважды
    # содержит один и тот же фрагмент, перевод обязан быть тем же (иначе один и тот
    # же текст получит два разных перевода). Считается РАЗНИЦА с источником, а не
    # «в выходе есть повторы» — второе валило бы верную работу.
    def dups(items):
        seen = {}
        for i, t in enumerate(items):
            seen.setdefault(t, []).append(i)
        return sum(len(v) - 1 for v in seen.values() if len(v) > 1)

    res["checks"]["duplicated_fragments"] = max(
        0, dups([b for _, b in frag_pairs]) - dups([a for a, _ in frag_pairs]))

    ratios = []
    for ta, tb in frag_pairs:
        ratio = len(tb) / max(len(ta), 1)
        ratios.append(ratio)
        cb, ca = cyr_share(ta), cyr_share(tb)
        # «Не переведён» отделено от «уже был русским»: фрагменты SFT-набора бывают
        # смешанными (кириллица 0.19 в среднем), и часть рассуждения уже русская —
        # это не брак учителя, а отсутствие работы.
        already_ru = cb >= FRAGMENT_RU_MIN
        res["fragments"].append({
            "chars_in": len(ta), "chars_out": len(tb),
            "cyrillic_before": round(cb, 4),
            "cyrillic_after": round(ca, 4),
            "english_words_after": round(en_word_share(tb), 4),
            "len_ratio": round(ratio, 3),
            "untouched": ta == tb,
            "already_russian": already_ru,
            "not_translated": (ta == tb) and not already_ru,
            "condensed": (not already_ru) and ratio < LEN_RATIO_BOUNDS[0],
            "not_russian": (not already_ru) and ca < FRAGMENT_RU_MIN,
            "empty": (not tb.strip()) and bool(ta.strip()),
            # Утечка строки-разделителя из промпта: не маркер формата, контрактом
            # не ловится — считается отдельно, потому что текст она всё равно портит.
            "delimiter_lines_in": len(RE_DELIM_LINE.findall(ta)),
            "delimiter_lines_out": len(RE_DELIM_LINE.findall(tb)),
            "delimiter_leak": (len(RE_DELIM_LINE.findall(tb))
                               > len(RE_DELIM_LINE.findall(ta))),
        })
    res["checks"]["len_ratio_min"] = round(min(ratios), 3) if ratios else None
    res["checks"]["len_ratio_max"] = round(max(ratios), 3) if ratios else None
    res["checks"]["len_ratio_out_of_bounds"] = sum(
        1 for r in ratios if not (LEN_RATIO_BOUNDS[0] <= r <= LEN_RATIO_BOUNDS[1]))
    return res


def do_verify(args) -> int:
    sft = Path(args.sft_jsonl)
    pilot = Path(args.pilot)
    if not pilot.exists():
        note(f"NOT-VERIFIED: нет файла пилота {pilot}")
        return EXIT_NOT_VERIFIED
    if not sft.exists():
        note(f"NOT-VERIFIED: нет SFT-набора {sft}")
        return EXIT_NOT_VERIFIED

    # Пилот читаем целиком (он мал), источник — потоком: набор 486 МБ в память не берём.
    only = parse_linenos(getattr(args, "linenos", ""))
    only_set = set(only) if only is not None else None
    wanted = {}
    for line in pilot.open(encoding="utf-8", errors="replace"):
        if not line.strip():
            continue
        out = json.loads(line)
        s3 = out.get("s3af") or {}
        ln = s3.get("source_line")
        if ln is None:
            continue
        if only_set is not None and ln not in only_set:
            continue
        wanted[ln] = (out, s3.get("source_sha256"))
    if only is not None and not wanted:
        note(f"NOT-VERIFIED: в пилоте нет записей из --linenos ({len(only)} шт.)")
        return EXIT_NOT_VERIFIED

    per_record, all_frags = [], []
    src_line_mismatch = 0
    seen_lines = set()
    with sft.open(encoding="utf-8", errors="replace") as f:
        for i, raw in enumerate(f):
            if i not in wanted:
                continue
            seen_lines.add(i)
            out, want_sha = wanted[i]
            text = raw.rstrip("\n")
            if want_sha and sha256_text(text) != want_sha:
                src_line_mismatch += 1
                continue
            try:
                src = json.loads(text)
            except ValueError:
                src_line_mismatch += 1
                continue
            v = verify_record(src, out)
            v["source_line"] = i
            per_record.append(v)
            all_frags.extend(v["fragments"])
    # записи, чьей строки в источнике не нашлось (пилот от другого файла)
    src_line_mismatch += len(set(wanted) - seen_lines)

    if not per_record:
        note("NOT-VERIFIED: в пилоте нет ни одной записи, сверяемой с источником")
        return EXIT_NOT_VERIFIED

    def pooled() -> dict:
        """Доли языка — по буквам (пул), не среднее по фрагментам.

        У фрагментов разная плотность букв, среднее по штукам соврало бы; тем же
        способом считает прибор S3ac, поэтому числа сопоставимы.
        """
        cis = sum(f["chars_in"] for f in all_frags)
        cos = sum(f["chars_out"] for f in all_frags)
        cyr_in = sum(f["cyrillic_before"] * max(f["chars_in"], 1) for f in all_frags)
        cyr_out = sum(f["cyrillic_after"] * max(f["chars_out"], 1) for f in all_frags)
        return {"chars_in": cis, "chars_out": cos,
                "cyrillic_before_approx": round(cyr_in / max(cis, 1), 4),
                "cyrillic_after_approx": round(cyr_out / max(cos, 1), 4)}

    checks = {
        "n_records": len(per_record),
        "n_fragments": len(all_frags),
        "src_line_mismatch": src_line_mismatch,
        "roles_equal_all": all(r["checks"]["roles_equal"] for r in per_record),
        "n_messages_equal_all": all(r["checks"]["n_messages_equal"] for r in per_record),
        "system_user_identical_all": all(r["checks"]["system_user_identical"] for r in per_record),
        "non_think_identical_all": all(r["checks"]["non_think_identical"] for r in per_record),
        "fragment_count_equal_all": all(
            r["checks"]["n_think_fragments_src"] == r["checks"]["n_think_fragments_out"]
            for r in per_record),
        "markers_equal_all": all(r["checks"]["markers_equal"] for r in per_record),
        "tool_call_json_not_worse_all": all(
            r["checks"]["tool_call_json_not_worse"] for r in per_record),
        "tool_call_pairs_src": sum(r["checks"]["tool_call_json_src"]["total"] for r in per_record),
        "tool_call_pairs_src_not_json": sum(
            r["checks"]["tool_call_json_src"]["total"] - r["checks"]["tool_call_json_src"]["parsed"]
            for r in per_record),
        "duplicated_fragments": sum(r["checks"]["duplicated_fragments"] for r in per_record),
        "empty_fragments": sum(1 for f in all_frags if f["empty"]),
        "untouched_fragments": sum(1 for f in all_frags if f["untouched"]),
        "len_ratio_out_of_bounds": sum(
            r["checks"]["len_ratio_out_of_bounds"] for r in per_record),
        "fragments_cyrillic_ge_0.5": sum(1 for f in all_frags if f["cyrillic_after"] >= FRAGMENT_RU_MIN),
    }
    pooled_stats = pooled()

    # Качество перевода — отдельно от сохранности формата. Формат может быть цел,
    # а смысл потерян (учитель «сжал» длинный фрагмент): доля кириллицы такое НЕ
    # ловит, ловит отношение длин. Эти флаги — не вердикт, а материал для фильтра
    # сборки v13 (отбраковка и повтор на более сильном учителе).
    need = [f for f in all_frags if not f["already_russian"]]
    flagged = [f for f in need if f["not_translated"] or f["condensed"] or f["not_russian"]]
    # Где именно отказывает учитель: разрез по длине фрагмента. Нужен потому, что
    # «сжатие» — не размазанный шум, а свойство длинных фрагментов; от этого
    # зависит, лечится ли оно нарезкой по абзацам или требует другого учителя.
    buckets = [(0, 2000), (2000, 4000), (4000, 6000), (6000, 10 ** 9)]
    by_len = []
    for lo, hi in buckets:
        grp = [f for f in need if lo <= f["chars_in"] < hi]
        by_len.append({
            "chars_in": f"{lo}..{'∞' if hi > 10 ** 8 else hi}",
            "fragments": len(grp),
            "flagged": sum(1 for f in grp if f["not_translated"] or f["condensed"] or f["not_russian"]),
            "not_translated": sum(1 for f in grp if f["not_translated"]),
            "condensed": sum(1 for f in grp if f["condensed"]),
            "not_russian": sum(1 for f in grp if f["not_russian"]),
            "delimiter_leak": sum(1 for f in grp if f["delimiter_leak"]),
        })

    quality = {
        "fragments": len(all_frags),
        "already_russian": sum(1 for f in all_frags if f["already_russian"]),
        "needing_translation": len(need),
        "not_translated": sum(1 for f in need if f["not_translated"]),
        "condensed": sum(1 for f in need if f["condensed"]),
        "not_russian": sum(1 for f in need if f["not_russian"]),
        "delimiter_leak": sum(1 for f in all_frags if f["delimiter_leak"]),
        "flagged": len(flagged),
        "reject_rate": round(len(flagged) / max(len(need), 1), 4),
        "reading": ("доля кириллицы высока и у «сжатого» фрагмента — язык не ловит "
                    "потерю смысла; флаг даёт отношение длин"),
        "by_length": by_len,
        "flagged_fragments": [
            {"source_line": r["source_line"], "index": i,
             "chars_in": f["chars_in"], "chars_out": f["chars_out"],
             "cyrillic_after": f["cyrillic_after"], "len_ratio": f["len_ratio"]}
            for r in per_record for i, f in enumerate(r["fragments"])
            if (not f["already_russian"]) and (f["not_translated"] or f["condensed"] or f["not_russian"])
        ][:20],
    }

    # механический контракт сохранности: нарушение = FAIL
    hard = {
        "roles_equal_all": checks["roles_equal_all"],
        "n_messages_equal_all": checks["n_messages_equal_all"],
        "system_user_identical_all": checks["system_user_identical_all"],
        "non_think_identical_all": checks["non_think_identical_all"],
        "fragment_count_equal_all": checks["fragment_count_equal_all"],
        "markers_equal_all": checks["markers_equal_all"],
        "tool_call_json_not_worse_all": checks["tool_call_json_not_worse_all"],
        "src_line_mismatch_zero": src_line_mismatch == 0,
        "no_duplicates": checks["duplicated_fragments"] == 0,
        "no_empty": checks["empty_fragments"] == 0,
    }
    contracted = all(hard.values())

    report = {
        "schema": "s3af-pilot-verify/1",
        "stage": "S3af-3",
        "date": now_iso(),
        "mode": "verify",
        "inputs": {"pilot": str(pilot), "pilot_sha256": sha256_file(pilot),
                   "sft_jsonl": str(sft),
                   "linenos_subset": only,
                   "subset_note": ("проверка ограничена этими строками источника — "
                                   "так два пилота меряются на одних и тех же "
                                   "примерах") if only is not None else ""},
        "pilot": {
            "n": len(per_record),
            "fragments": len(all_frags),
            "cyrillic_before": pooled_stats["cyrillic_before_approx"],
            "cyrillic_after": pooled_stats["cyrillic_after_approx"],
            "chars_in": pooled_stats["chars_in"],
            "chars_out": pooled_stats["chars_out"],
            "share_fragments_russian": round(
                checks["fragments_cyrillic_ge_0.5"] / max(len(all_frags), 1), 4),
            # Отношение длин: среднее по фрагментам и пуловое. Пуловое устойчивее к
            # мелким фрагментам, среднее — к одному очень длинному.
            "len_ratio_mean": round(
                sum(f["len_ratio"] for f in all_frags) / max(len(all_frags), 1), 4),
            "len_ratio_min": round(min((f["len_ratio"] for f in all_frags), default=0.0), 4),
            "len_ratio_max": round(max((f["len_ratio"] for f in all_frags), default=0.0), 4),
            "len_ratio_pooled": round(pooled_stats["chars_out"] / max(pooled_stats["chars_in"], 1), 4),
            # Доля латинских слов — тот же показатель, по которому инвентаризация
            # учителей выносит вердикт. Читать его надо с оговоркой: слаг концепта,
            # имя поля и аббревиатура (GRPO, KL) — тоже «английские слова», а
            # промпт велит их сохранять. Высокая доля может значить «сохранил
            # термины», а не «не перевёл»; различить их — работа чтения (§4.3).
            "english_words_after_mean": round(
                sum(f["english_words_after"] for f in all_frags) / max(len(all_frags), 1), 4),
        },
        "format_checks": checks,
        "quality": quality,
        "hard_contract": hard,
        "contract_ok": contracted,
        "per_record": [{"source_line": r["source_line"], **r["checks"]} for r in per_record],
        "artifacts": [{"path": "tools/russian_think_pilot.py", "what": "этот прибор"}],
    }
    outdir = Path(args.out) if args.out else pilot.parent
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "verify.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    note(f"проверка: кириллица <think> {pooled_stats['cyrillic_before_approx']} → "
         f"{pooled_stats['cyrillic_after_approx']}; контракт сохранности: "
         f"{'цел' if contracted else 'НАРУШЕН'}")
    note(f"   качество: флагов {quality['flagged']} из {quality['needing_translation']} "
         f"переводившихся (не переведено {quality['not_translated']}, "
         f"сжато {quality['condensed']}, не русский {quality['not_russian']})")
    for k, v in hard.items():
        if not v:
            note(f"   нарушение: {k}")
    return EXIT_OK if contracted else EXIT_FAIL


# ------------------------------------------------------------------- report

# -------------------------------------------------------------- объём и цена

def scan_think_volume(sft: Path, limit_records: int = 0) -> dict:
    """Полный проход по SFT-набору: сколько рассуждения, сколько записей и сколько **уникального**.

    Уникальность считается не для красоты: дубликат рассуждения обязан получить
    **тот же** перевод, иначе один и тот же ход assistant станет в наборе
    двуязычным. Заодно это делит цену обработки: переводить надо уникальные
    фрагменты, а не вхождения.
    """
    n_rec = n_with = n_frag = chars = n_toolcall = 0
    n_turn = 0
    uniq_frag: dict = {}
    uniq_turn: set = set()
    with sft.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if limit_records and i >= limit_records:
                break
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            n_rec += 1
            has = False
            for m in rec.get("messages", []):
                if m.get("role") != "assistant":
                    continue
                content = m.get("content") or ""
                n_turn += 1
                uniq_turn.add(sha256_text(content))
                for t in RE_THINK_PAIR.findall(content):
                    has = True
                    n_frag += 1
                    chars += len(t)
                    if "<tool_call>" in t:
                        n_toolcall += 1
                    h = sha256_text(t)
                    if h not in uniq_frag:
                        uniq_frag[h] = len(t)
                break
            n_with += 1 if has else 0
    n_turn_uniq = len(uniq_turn)
    return {
        "records": n_rec, "records_with_think": n_with,
        "assistant_turns": n_turn, "assistant_turns_unique": n_turn_uniq,
        "assistant_duplicate_share": round(1 - n_turn_uniq / max(n_turn, 1), 4),
        "think_fragments": n_frag, "think_fragments_unique": len(uniq_frag),
        "fragment_duplicate_share": round(1 - len(uniq_frag) / max(n_frag, 1), 4),
        "think_chars": chars, "think_chars_unique": sum(uniq_frag.values()),
        "unique_char_share": round(sum(uniq_frag.values()) / max(chars, 1), 4),
        "fragments_with_tool_call": n_toolcall,
    }


def token_ratio(sft: Path, tokenizer_dir: Path, n_frag: int = 300) -> dict:
    """Сколько токенов в символе рассуждения — на выборке, тем же токенизатором контура.

    Токенизатор учителя (Qwen3.8/Qwen3) не совпадает с контурным (Qwen2.5) пофайлово,
    но оба — BPE одного семейства; расхождение названо, а не спрятано. Числа нужны
    для цены, а не для решения о качестве.
    """
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return {"available": False, "why": "нет пакета tokenizers"}
    tok_file = tokenizer_dir / "tokenizer.json"
    if not tok_file.exists():
        snaps = sorted(tokenizer_dir.glob("snapshots/*/tokenizer.json"))
        if not snaps:
            return {"available": False, "why": f"нет tokenizer.json в {tokenizer_dir}"}
        tok_file = snaps[-1]
    tk = Tokenizer.from_file(str(tok_file))
    frags, step = [], max(1, 38716 // n_frag)
    with sft.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for m in rec.get("messages", []):
                if m.get("role") == "assistant":
                    for t in RE_THINK_PAIR.findall(m.get("content") or ""):
                        frags.append(t)
                    break
            if len(frags) >= n_frag * 3:
                break
    frags = frags[::step][:n_frag]
    ch = sum(len(t) for t in frags)
    ntok = sum(len(tk.encode(t).ids) for t in frags)
    en_ratio = ntok / max(ch, 1)
    return {"available": True, "tokenizer": str(tok_file), "fragments": len(frags),
            "chars": ch, "tokens": ntok, "tokens_per_char_en": round(en_ratio, 4),
            "note": "доля английского текста; для русского перевода доля выше "
                    "(кириллица дороже в BPE) — точное число берётся из пилота"}


def ru_token_ratio(pilot: Path, tokenizer_dir: Path, n_frag: int = 300) -> dict:
    """То же для **русского перевода**: измеряется на пилоте, не оценивается."""
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return {"available": False, "why": "нет пакета tokenizers"}
    tok_file = tokenizer_dir / "tokenizer.json"
    if not tok_file.exists():
        snaps = sorted(tokenizer_dir.glob("snapshots/*/tokenizer.json"))
        if not snaps:
            return {"available": False, "why": f"нет tokenizer.json в {tokenizer_dir}"}
        tok_file = snaps[-1]
    tk = Tokenizer.from_file(str(tok_file))
    frags = []
    for line in pilot.open(encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for m in rec.get("messages", []):
            if m.get("role") == "assistant":
                frags.extend(RE_THINK_PAIR.findall(m.get("content") or ""))
                break
    frags = frags[:n_frag]
    ch = sum(len(t) for t in frags)
    ntok = sum(len(tk.encode(t).ids) for t in frags)
    return {"available": True, "fragments": len(frags), "chars": ch, "tokens": ntok,
            "tokens_per_char_ru": round(ntok / max(ch, 1), 4)}


def do_report(args) -> int:
    """Свод evidence/s3af-russian-thinks.json из teachers.json + verify.json + translate."""
    teachers = Path(args.teachers) if args.teachers else None
    outdir = Path(args.out) if args.out else Path(args.pilot).parent
    verify_path = outdir / "verify.json"
    if not verify_path.exists():
        note(f"NOT-VERIFIED: нет {verify_path} — сначала --mode verify")
        return EXIT_NOT_VERIFIED
    verify = json.loads(verify_path.read_text(encoding="utf-8"))
    tdata = json.loads(teachers.read_text(encoding="utf-8")) if teachers and teachers.exists() else None
    # Содержательная выборочная проверка — чтение, а не измерение. Вердикты
    # приносит человек (файл --spotcheck); прибор их только вписывает и требует,
    # чтобы каждый вердикт был назван, а не остался «в целом нормально».
    spot = {"available": False,
            "why": "файл вердиктов не подан (--spotcheck)"}
    if args.spotcheck and Path(args.spotcheck).exists():
        raw = json.loads(Path(args.spotcheck).read_text(encoding="utf-8"))
        verdicts = raw.get("items", raw if isinstance(raw, list) else [])
        allowed = {"смысл сохранён", "упрощён", "искажён"}
        bad = [v for v in verdicts if v.get("verdict") not in allowed]
        spot = {
            "available": True,
            "n": len(verdicts),
            "kind": "чтение человеком пар «источник → перевод»; не измерение",
            "scale": sorted(allowed),
            "counts": {k: sum(1 for v in verdicts if v.get("verdict") == k) for k in sorted(allowed)},
            "items": verdicts,
            "unnamed_verdicts": bad,
            "method": raw.get("method") if isinstance(raw, dict) else None,
        }

    evidence = {
        "schema": "s3af-russian-thinks/1",
        "stage": "S3af",
        "status": "partial",
        "date": now_iso(),
        "purpose": ("разведка источника русскоязычных трасс рассуждений и пилот: чем переводить "
                    "англоязычные <think> SFT-набора, что даёт перевод по числам, сколько это стоит"),
        "decision": {
            "adr": "ADR-037 (ветка arch/laguna-control-arms)",
            "what": "маскирование <think> отменено; лечение — русскоязычные трассы рассуждений",
            "measured_here": "пригодность учителей, пилот, цена, план набора v13",
        },
        "teachers": (tdata or {}).get("teachers", []),
        "teachers_verdict": (tdata or {}).get("verdict"),
        "availability": (tdata or {}).get("availability"),
        "pilot": verify.get("pilot"),
        "format_checks": verify.get("format_checks"),
        "content_spotcheck": spot,
        "hard_contract": verify.get("hard_contract"),
        "inputs": verify.get("inputs"),
        "artifacts": [{"path": "tools/teacher_inventory.py", "what": "инвентаризация учителей"},
                      {"path": "tools/russian_think_pilot.py", "what": "пилот и проверка"},
                      {"path": str(verify_path), "what": "числовая проверка пилота"}],
    }
    if tdata and tdata.get("criteria"):
        evidence["criteria"] = tdata["criteria"]

    # ── цена массовой обработки: объём измеряется, а не берётся из задания ──
    sft = Path(args.sft_jsonl)
    pilot_path = Path(args.pilot) if args.pilot else outdir / "pilot.jsonl"
    if sft.exists():
        vol = scan_think_volume(sft)
        tok = token_ratio(sft, Path(args.tokenizer))
        ru = ru_token_ratio(pilot_path, Path(args.tokenizer)) if pilot_path.exists() else {}
        tr = {}
        tr_path = outdir / "translate-report.json"
        if tr_path.exists():
            tr = json.loads(tr_path.read_text(encoding="utf-8"))
        chars_per_s = tr.get("chars_per_s") or 0.0
        en_rate = tok.get("tokens_per_char_en")
        ru_rate = ru.get("tokens_per_char_ru")
        # Цена считается по УНИКАЛЬНЫМ фрагментам: дубликат обязан получить тот же
        # перевод, поэтому вхождения не переводятся повторно. Наивная цена (по всем
        # вхождениям) приводится рядом — чтобы видно было, на чём именно экономия.
        chars_uniq = vol["think_chars_unique"]
        chars_all = vol["think_chars"]
        in_tokens = int(chars_uniq * en_rate) if en_rate else None
        out_tokens = int(chars_uniq * ru_rate) if ru_rate else None
        evidence["cost_estimate"] = {
            "volume_measured": vol,
            "tokens": {
                "tokenizer": args.tokenizer,
                "en_tokens_per_char": en_rate, "ru_tokens_per_char": ru_rate,
                "input_tokens_en": in_tokens, "output_tokens_ru": out_tokens,
                "total_tokens": (in_tokens + out_tokens) if in_tokens and out_tokens else None,
                "counted_on": "уникальные фрагменты (вхождения не переводятся повторно)",
            },
            "throughput_measured": {
                "teacher": tr.get("teacher"),
                "chars_per_s": chars_per_s,
                "teacher_calls": tr.get("teacher_calls"),
                "seconds_total": tr.get("seconds_total"),
                "note": "замер пилота, не оценка",
            },
            "wall_clock_hours": (round(chars_uniq / chars_per_s / 3600, 1)
                                 if chars_per_s else None),
            "wall_clock_hours_naive": (round(chars_all / chars_per_s / 3600, 1)
                                       if chars_per_s else None),
            "saving_from_dedup": round(1 - chars_uniq / max(chars_all, 1), 4),
            "device": args.device_note,
            "stand_contention": args.contention_note,
            "not_measured": [
                "цена на другой модели: время пересчитывается от замеренной "
                "пропускной способности, заново не мерилось",
                "цена повторного прохода при отбраковке: пилот не измеряет долю брака "
                "на полном наборе",
                "время сходимости пилотного прогона SFT с русскоязычными трассами: "
                "прогон не ставился (отдельная дельта)",
            ],
        }
        evidence["artifacts"].append(
            {"path": str(sft), "what": "SFT-набор v12 (источник; только чтение)"})

        # ── план набора v13: решения с опорой на измеренное, а не на пожелания ──
        evidence["plan_v13"] = {
            "plan_doc": "docs/specs/LANGUAGE-TREATMENT-PLAN.md",
            "share_translated": 1.0,
            "share_reason": (
                "дубликаты хода assistant 57.71 %: «перевести половину» разведёт "
                "одинаковые ходы по разным языкам — это ровно тот шум, от которого "
                "лечим. Переводится 1.0 по УНИКАЛЬНЫМ фрагментам, а не выборочно "
                "по записям"),
            "unit_of_translation": "уникальный <think>-фрагмент (вхождения получают тот же перевод)",
            "unique_fragments": vol["think_fragments_unique"],
            "unique_chars": vol["think_chars_unique"],
            "build": {
                "output": "sft_train_v13.jsonl — НОВЫЙ файл на /home/user/gb10-shared/datasets",
                "source_untouched": "sft_train_v12.jsonl не перезаписывается и не правится",
                "card": "data/sft-v13-card.json: sha256 набора, число записей и фрагментов, "
                        "доля кириллицы <think> до и после, версия учителя и промпта, "
                        "ссылка на пилот как на источник способа",
            },
            "checks_before_submission": [
                "механический контракт §1 — уже на ПОЛНОМ наборе, не на пилоте",
                "tools/check_eval_leakage.py (ADR-025 / AD-7): пересечение с eval-набором",
                "tools/check_sft_rl_overlap.py: пересечение SFT ↔ RL-пул",
                "сверка «один ход assistant — один язык» (следствие перевода по уникальным)",
            ],
            "overlap_expectation": (
                "перевод меняет только <think>; промпты и slug-ответы не меняются, "
                "поэтому пересечение по нормализованному тексту и n-граммам может "
                "только уменьшиться — проверка подтверждает, а не ищет"),
            "next_delta": {
                "what": "пилотный прогон SFT 200–500 шагов на v13 с замером языка генерации",
                "why": "единственный замер, отвечающий «сдвигает ли русскоязычная доля "
                       "язык входа»; проба — та же, что дала кириллицу 0.123",
                "blocked_by": "стенд GB10 (AD-5, ADR-012: одна нагрузка за раз, окно арбитража)",
            },
        }
    # Вопросы к архитектору — из фактов прогона, а не «на всякий случай».
    oq = [
        {"q": "Ставить ли пилотный прогон SFT 200–500 шагов на русскоязычных трассах "
              "и на каком окне арбитража стенда (AD-5, ADR-012)?",
         "why": "это единственный замер, отвечающий «сдвигает ли русскоязычная доля "
                "язык входа»; без него причинность остаётся предсказанием S3ac",
         "blocked_by": "стенд GB10 занят стадией SFT соседней дельты"},
        {"q": "Собирать v13 под тот же микс v12r или микс тоже пересматривается (ADR-003)?",
         "why": "перевод трасс снимает носитель, но не меняет долю домена; вес домена "
                "и язык трасс разводятся только вместе с решением по миксу"},
        {"q": "Нужен ли отдельный замер цены на qwen3.8-27b под массовую обработку?",
         "why": "27B — то же семейство, что писало исходные трассы, и даёт более высокое "
                "качество, но измеренная пропускная способность ниже на порядок"},
    ]
    if not spot.get("available"):
        oq.append({"q": "Содержательная выборочная проверка не подтверждена чтением — "
                        "принимать ли пилот по механическим проверкам и числам языка?",
                   "why": "машина не отличает «смысл сохранён» от «смысл упрощён»"})
    evidence["open_questions"] = oq
    if not spot.get("available"):
        evidence["status"] = "partial"
    out = Path(args.evidence)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    note(f"evidence: {out}")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="S3af: пилот перевода <think> и его проверка")
    ap.add_argument("--mode", choices=["translate", "verify", "report"], required=True)
    ap.add_argument("--sft-jsonl", default=str(DEFAULT_SFT))
    ap.add_argument("--pilot", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--evidence", default="evidence/s3af-russian-thinks.json")
    ap.add_argument("--teachers", default="")
    ap.add_argument("--spotcheck", default="",
                    help="JSON с вердиктами содержательной выборочной проверки")
    ap.add_argument("--tokenizer", default=str(DEFAULT_TOKENIZER))
    ap.add_argument("--device-note", default="")
    ap.add_argument("--contention-note", default="")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--stride-start", type=int, default=0)
    ap.add_argument("--linenos", default="",
                    help="явные номера строк источника (0-based) через запятую или "
                         "JSON-файл со списком: пилот на тех же примерах, что другой "
                         "пилот. Задан — --n/--stride-start не действуют")
    ap.add_argument("--teacher-kind", default="openai", choices=["openai", "ollama"])
    ap.add_argument("--teacher-endpoint", default="")
    ap.add_argument("--teacher-model", default="")
    ap.add_argument("--no-think", action="store_true",
                    help="выключить цепочку рассуждений учителя (openai-совместимый "
                         "эндпоинт: chat_template_kwargs.enable_thinking=false) — "
                         "иначе рассуждающий учитель тратит бюджет на reasoning")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()

    if args.mode == "translate":
        return do_translate(args)
    if args.mode == "verify":
        args.pilot = args.pilot or str(Path(args.out) / "pilot.jsonl")
        return do_verify(args)
    return do_report(args)


if __name__ == "__main__":
    sys.exit(main())
