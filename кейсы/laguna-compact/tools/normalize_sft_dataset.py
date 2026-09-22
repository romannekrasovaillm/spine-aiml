#!/usr/bin/env python3
"""S3an — механическая нормализация SFT-набора: три структурных дефекта (ADR-042).

Зачем отдельный инструмент, а не правка файла руками. Дефекты формата
(незакрытый ``<think>``, ``<tool_call>`` внутри ``<think>``, ответа нет после
рассуждения) — это **артефакты шаблона/декодирования**, повторяющиеся десятками
тысяч раз. Правится не пример, а класс: правило, применимое к любой строке и
проверяемое на фикстуре. Отсюда две обязанности инструмента:

1. **Каждое преобразование логируется и обратимо.** Журнал (`--log`) несёт по
   каждому изменённому примеру список правок в координатах **исходной** строки
   (``delete s..e`` / ``insert at s``), поэтому нормализация снимается обратно
   без исходного файла: ``apply(inverse(edits))``. Ничего «на усмотрение
   модели»: ответы не дорисовываются, хвосты рассуждений не восстанавливаются.
2. **Примеры, которые механикой не чинятся, исключаются с названной причиной**,
   а не «чинятся» догадкой. Доля исключённых — число в отчёте, а не умолчание.

Правила (R1–R7). Токенизация: содержимое assistant-сообщения режется на теги
``<think>|</think>|<tool_call>|</tool_call>|<tool_response>|</tool_response>`` и
текст между ними; дальше — один проход автомата с состоянием.

**Что считать вызовом.** Полезная нагрузка ``<tool_call>`` — это JSON-объект
(так объявлено в system-промпте набора). Разбор нагрузки даёт механический
признак, отличающий настоящий вызов от остатка шаблона: 55 245 тегов набора
закрыты и несут JSON, 7 077 — открыты без JSON (остаток шаблона: за тегом идёт
рассуждение, а не запрос), 18 — закрытая пара с нагрузкой-не-JSON (тег
**процитирован внутри рассуждения**: «выведи это в формате
``<tool_call>…</tool_call>``»).

* **R1 — выбросить ``<tool_call>`` без нагрузки-вызова** (пустая нагрузка либо
  не-JSON и тег не закрыт). Тег — остаток шаблона/декодирования, информации не
  несёт: настоящий вызов идёт дальше отдельным блоком с JSON. Текст между
  тегами остаётся на месте (это рассуждение), поэтому правка не теряет
  содержания. Пустая пара ``<tool_call></tool_call>`` выбрасывается целиком.
* **R2 — закрыть рассуждение перед вызовом** (``</think>`` вставляется
  непосредственно перед ``<tool_call>``/``<tool_response>``, встреченным внутри
  открытого ``<think>``). Это и есть «закрыть блок в точке фактического
  завершения рассуждения»: рассуждение кончается там, где начинается не-текст
  рассуждения. Если рассуждения до вызова не было вовсе (примеры вида
  ``<think><tool_call>…``), блок закрывается тут же — пустым.
* **R3 — выбросить повторный ``<think>``**, если рассуждение уже открыто
  (``<think><think>…`` даёт двойной вход при одном выходе).
* **R4 — выбросить ``</think>`` вне рассуждения** (парный тег без открытого
  блока). Внутри полезной нагрузки ``<tool_call>`` такого тега быть не может —
  это уже не нормализация, а порча JSON, поэтому там пример исключается.
* **R5 — выбросить повторный ``<tool_call>``/``<tool_response>``** при уже
  открытом блоке того же типа.
* **R6 — выбросить парный ``</tool_call>``/``</tool_response>`` вне блока.**
* **R7 — доставить пропущенный ``</tool_call>``** там, где нагрузка вызова —
  **валидный JSON**, а закрывающий тег отсутствует: конец вызова определён
  разбором (кончился JSON), а не догадкой.

Исключения (пример не пишется в новый набор, причина попадает в журнал и
отчёт): пара ``<tool_call>…</tool_call>`` с нагрузкой-не-JSON (тег процитирован
внутри рассуждения — от настоящего вызова механикой не отличается), незакрытый
блок в конце сообщения, ``<tool_response>`` при открытом ``<tool_call>``,
``</think>``/``<think>`` внутри нагрузки вызова, ``<tool_call>`` внутри
``<tool_response>``, и **ответ короче 20 символов** после снятия всех блоков
(ADR-042: «без ответа после рассуждения»).

Чего инструмент НЕ делает: не трогает ``system``/``user``-сообщения (в
system-промпте теги стоят как пример формата), не дедуплицирует (ADR-033 п.1),
не переводит и не переписывает текст, не маскирует ``<think>`` (ADR-037).

Коды возврата::

    0 — набор записан (или аудит пройден)
    1 — нормализация не сошлась: в выходе остались дефекты (внутренняя ошибка)
    2 — NOT-VERIFIED: вход отсутствует/нечитаем

Запуск::

    python3 tools/normalize_sft_dataset.py --audit --in datasets/sft_train_v12.jsonl
    python3 tools/normalize_sft_dataset.py --in datasets/sft_train_v12.jsonl \\
        --out /home/user/gb10-shared/datasets/sft_train_v13_fixed.jsonl \\
        --log runs/s3an-normalize-20260919/changes.jsonl \\
        --report runs/s3an-normalize-20260919/report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Шесть токенов формата v12 (ADR-004): те же, что объявлены спецтокенами кэша.
THINK_OPEN, THINK_CLOSE = "<think>", "</think>"
CALL_OPEN, CALL_CLOSE = "<tool_call>", "</tool_call>"
RESP_OPEN, RESP_CLOSE = "<tool_response>", "</tool_response>"

TAG_RE = re.compile(r"</?(?:think|tool_call|tool_response)>")

#: Порог «ответа нет» — из формулировки ADR-042 («хвост < 20 символов»).
MIN_ANSWER_CHARS = 20

#: Идентификаторы правил — они попадают в журнал и в карточку, менять нельзя
#: без пересборки набора: по ним читается, что именно было сделано с примером.
RULES = {
    "R1_drop_noncall_tool_call": "выброшен <tool_call> без нагрузки-вызова (пустой или не-JSON, не закрытый) — остаток шаблона",
    "R2_close_think_before_call": "вставлен </think> в точке перехода рассуждения в вызов/ответ",
    "R3_drop_duplicate_think_open": "выброшен повторный <think> при открытом рассуждении",
    "R4_drop_stray_think_close": "выброшен </think> вне открытого рассуждения",
    "R5_drop_duplicate_block_open": "выброшен повторный <tool_call>/<tool_response> при открытом блоке",
    "R6_drop_stray_block_close": "выброшен </tool_call>/</tool_response> вне блока",
    "R7_insert_missing_call_close": "вставлен </tool_call> в конце валидного JSON-вызова без закрывающего тега",
}

#: Остатки ПОСЛЕ нормализации: не дефекты ADR-042, но и не «всё хорошо».
RESIDUAL = {
    "tool_response_without_call":
        "в сообщении ответ инструмента без вызова перед ним: вызов был остатком "
        "шаблона и снят правилом R1 (класс ADR-042 «структура вызова в целом» не проверяется)",
}

#: Причины исключения — формулировки для отчёта; ключ = код.
EXCLUSIONS = {
    "quoted_tag_pair_in_reasoning": "пара <tool_call>…</tool_call> с нагрузкой-не-JSON: тег процитирован внутри рассуждения либо вызов разорван — от настоящего вызова механикой не отличается",
    "unclosed_block_at_end": "незакрытый блок в конце сообщения (рассуждение обрывается на середине, ответа нет)",
    "response_while_call_open": "<tool_response> при открытом <tool_call> — порядок блоков не восстанавливается",
    "think_close_inside_call": "</think> внутри полезной нагрузки <tool_call> — правка испортила бы JSON",
    "think_inside_call": "<think> внутри полезной нагрузки <tool_call> — правка испортила бы JSON",
    "call_inside_response": "<tool_call> внутри <tool_response> — правка испортила бы ответ инструмента",
    "no_answer": f"после снятия блоков текст ответа короче {MIN_ANSWER_CHARS} символов (ADR-042: «нет ответа после рассуждения»)",
    "bad_record": "запись не разбирается: нет messages[] или содержимое не строка",
}


class Unrepairable(Exception):
    """Пример не чинится механикой — исключается с названной причиной."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ── токенизация и правки ─────────────────────────────────────────────────────

class Edit:
    """Правка в двух координатах: ``s..e`` — в исходной строке (для журнала),
    ``n`` — в результирующей (для снятия правки).

    Одна координата не годится: снятие правок по исходным позициям требует
    пересчёта сдвигов, который ломается, когда вставка и удаление стоят в одной
    позиции. Позиция в результате известна в момент сборки — она и хранится.

    ``delete`` — в исходной строке вырезан ``text[s:e]``, в результате он стоял
    на ``n``; ``insert`` — в результат на ``n`` вставлен ``text``.
    """

    __slots__ = ("op", "s", "e", "n", "text")

    def __init__(self, op: str, s: int, e: int, n: int, text: str):
        self.op, self.s, self.e, self.n, self.text = op, s, e, n, text

    def as_dict(self) -> dict:
        return {"op": self.op, "s": self.s, "e": self.e, "n": self.n, "text": self.text}


def _tokenize(text: str) -> list[tuple[str, str, int, int]]:
    """Режет содержимое на (kind, value, start, end); kind ∈ {text, tag}."""
    out: list[tuple[str, str, int, int]] = []
    pos = 0
    for m in TAG_RE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()], pos, m.start()))
        out.append(("tag", m.group(0), m.start(), m.end()))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:], pos, len(text)))
    return out


def _payload_is_call(payload: str) -> bool:
    """Нагрузка ``<tool_call>`` — это вызов, если она разбирается как JSON-объект.

    Единственный признак, отличающий вызов от остатка шаблона, который не
    зависит от содержания рассуждения. Формулировка «нагрузка похожа на JSON»
    не годится: она пропускала бы ``{"name": …}. <текст рассуждения>``.
    """
    p = payload.strip()
    if not p:
        return False
    try:
        return isinstance(json.loads(p), dict)
    except Exception:
        return False


def _strip_blocks(text: str) -> str:
    """Снимает все парные блоки формата — остаётся текст ответа.

    Применяется к НОРМАЛИЗОВАННОМУ тексту (блоки парны и не вложены), поэтому
    одного прохода достаточно; цикл — страховка от вложенности, которой быть не
    должно, и он же ловит её как непустой остаток, а не молча съедает.
    """
    prev = None
    cur = text
    while cur != prev:
        prev = cur
        for a, b in ((THINK_OPEN, THINK_CLOSE), (CALL_OPEN, CALL_CLOSE), (RESP_OPEN, RESP_CLOSE)):
            cur = re.sub(re.escape(a) + r".*?" + re.escape(b), "", cur, flags=re.S)
    return cur


# ── ядро: нормализация одного сообщения ──────────────────────────────────────

def normalize_message(text: str) -> tuple[str, list[Edit], Counter]:
    """Нормализует содержимое assistant-сообщения.

    Возвращает (новый текст, правки в координатах исходного, счётчик правил).
    Бросает Unrepairable, если пример механикой не чинится.
    """
    rules: Counter = Counter()
    edits: list[Edit] = []
    toks = _tokenize(text)
    out: list[str] = []
    pos = 0  # длина собранного результата — она же координата n для правки

    def emit(piece: str) -> None:
        nonlocal pos
        out.append(piece)
        pos += len(piece)

    def drop(s: int, e: int, rule: str) -> None:
        """Тег выброшен: в результате он стоял на текущем конце сборки."""
        edits.append(Edit("delete", s, e, pos, text[s:e]))
        rules[rule] += 1

    def insert(at: int, piece: str, rule: str) -> None:
        emit(piece)
        edits.append(Edit("insert", at, at, pos - len(piece), piece))
        rules[rule] += 1

    in_think = in_call = in_resp = False
    i, n = 0, len(toks)
    while i < n:
        kind, val, s, e = toks[i]
        if kind == "text":
            emit(val)
            i += 1
            continue

        if val == CALL_OPEN:
            # Разбор нагрузки решает, вызов это или остаток шаблона, — и только
            # после этого тег попадает в автомат (порядок важен: иначе остаток
            # шаблона «открывает вызов» и всё, что за ним, читается как JSON).
            j = i + 1
            payload, pend = "", e
            if j < n and toks[j][0] == "text":
                payload, pend = toks[j][1], toks[j][3]
                j += 1
            closed = j < n and toks[j][1] == CALL_CLOSE
            if payload.strip() == "":  # пустой фрагмент: тег (и пара) целиком
                drop(s, e, "R1_drop_noncall_tool_call")
                i += 1
                if closed:
                    drop(toks[j][2], toks[j][3], "R1_drop_noncall_tool_call")
                    i = j + 1
                continue
            if not _payload_is_call(payload):
                if closed:
                    # Пара с нагрузкой-не-JSON. Выбросить теги — значит вписать
                    # «нагрузку» в текст ответа; дорисовать JSON — значит
                    # выдумать запрос. Механика здесь не судья → исключение.
                    raise Unrepairable("quoted_tag_pair_in_reasoning")
                drop(s, e, "R1_drop_noncall_tool_call")
                i += 1
                continue
            #: Страховка: на текущих правилах ветка недостижима — вызов закрывается
            #: либо явным `</tool_call>`, либо правилом R7, поэтому повторного
            #: открытия в открытом вызове быть не может. Оставлена, потому что
            #: «недостижимо» здесь — свойство набора правил, а не структуры данных.
            if in_call:
                drop(s, e, "R5_drop_duplicate_block_open")
                i += 1
                continue
            if in_resp:
                raise Unrepairable("call_inside_response")
            if in_think:  # R2: рассуждение кончилось здесь
                insert(s, THINK_CLOSE, "R2_close_think_before_call")
                in_think = False
            emit(val)
            in_call = True
            if not closed:  # R7: конец вызова там, где кончился JSON
                emit(payload)
                insert(pend, CALL_CLOSE, "R7_insert_missing_call_close")
                in_call = False
                i = j
                continue
            i += 1
            continue

        if val == THINK_OPEN:
            if in_think:
                drop(s, e, "R3_drop_duplicate_think_open")
                i += 1
                continue
            if in_call:
                raise Unrepairable("think_inside_call")
            if in_resp:
                raise Unrepairable("call_inside_response")
            emit(val)
            in_think = True
        elif val == THINK_CLOSE:
            if in_think:
                emit(val)
                in_think = False
            elif in_call:
                raise Unrepairable("think_close_inside_call")
            else:
                drop(s, e, "R4_drop_stray_think_close")
        elif val == CALL_CLOSE:
            if in_call:
                emit(val)
                in_call = False
            else:
                drop(s, e, "R6_drop_stray_block_close")
        elif val == RESP_OPEN:
            if in_resp:
                drop(s, e, "R5_drop_duplicate_block_open")
                i += 1
                continue
            #: Страховка, как и выше: ответ инструмента не может быть встречен в
            #: открытом вызове — до него вызов закрывается (R7 либо явный тег).
            if in_call:
                raise Unrepairable("response_while_call_open")
            if in_think:  # R2: рассуждение кончилось здесь же
                insert(s, THINK_CLOSE, "R2_close_think_before_call")
                in_think = False
            emit(val)
            in_resp = True
        elif val == RESP_CLOSE:
            if in_resp:
                emit(val)
                in_resp = False
            else:
                drop(s, e, "R6_drop_stray_block_close")
        i += 1

    if in_think or in_call or in_resp:
        raise Unrepairable("unclosed_block_at_end")
    return "".join(out), edits, rules


def normalize_record(rec: dict) -> tuple[dict, list[dict], Counter, str | None]:
    """Нормализует запись целиком.

    Возвращает (запись, правки по сообщениям, счётчик правил, код исключения).
    Правится только роль ``assistant``: в system/user теги стоят как пример
    формата, и их правка была бы порчей промпта.
    """
    msgs = rec.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return rec, [], Counter(), "bad_record"
    for m in msgs:
        if not isinstance(m, dict) or not isinstance(m.get("content"), str):
            return rec, [], Counter(), "bad_record"

    rules: Counter = Counter()
    log_edits: list[dict] = []
    out_rec = None
    for idx, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        try:
            new, edits, r = normalize_message(m["content"])
        except Unrepairable as exc:
            return rec, [], Counter(), exc.code
        if edits or new != m["content"]:
            if out_rec is None:
                out_rec = {**rec, "messages": [dict(x) for x in msgs]}
            out_rec["messages"][idx]["content"] = new
            log_edits.append({
                "msg_index": idx,
                "content_sha256_before": hashlib.sha256(m["content"].encode()).hexdigest(),
                "content_sha256_after": hashlib.sha256(new.encode()).hexdigest(),
                "edits": [e.as_dict() for e in edits],
            })
        rules.update(r)
    # Ответ — в последнем assistant-сообщении (там же, где его искал замер ADR-042).
    last = [m for m in (out_rec or rec)["messages"] if m.get("role") == "assistant"][-1]
    if len(_strip_blocks(last["content"]).strip()) < MIN_ANSWER_CHARS:
        return rec, [], Counter(), "no_answer"
    return out_rec or rec, log_edits, rules, None


# ── применимость обратной операции (доказывает обратимость на фикстурах) ─────

def revert(text: str, edits: list[dict]) -> str:
    """Снимает правки и возвращает ИСХОДНЫЙ текст (ADR-042 п.6: «каждое
    преобразование обратимо»).

    Правки снимаются по координате ``n`` в порядке УБЫВАНИЯ: снятие правки при
    большей ``n`` не сдвигает позиции меньших, поэтому пересчёт сдвигов не
    нужен вовсе. Вставка обязана найтись на своём месте — иначе журнал не
    соответствует набору, и это ошибка, а не «не сошлось».
    """
    cur = text
    for e in sorted(edits, key=lambda x: x["n"], reverse=True):
        p = e["n"]
        if e["op"] == "insert":
            assert cur[p:p + len(e["text"])] == e["text"], f"вставка не найдена: {e!r}"
            cur = cur[:p] + cur[p + len(e["text"]):]
        else:
            cur = cur[:p] + e["text"] + cur[p:]
    return cur


# ── замеры (те же определения, что дали числа ADR-042) ───────────────────────

def assistant_text(rec: dict) -> str:
    return "".join(m["content"] for m in rec["messages"] if m.get("role") == "assistant")


def last_assistant(rec: dict) -> str:
    return [m["content"] for m in rec["messages"] if m.get("role") == "assistant"][-1]


_PAIR_RE = re.compile(re.escape(THINK_OPEN) + r"(.*?)" + re.escape(THINK_CLOSE), re.S)


def defects_of(rec: dict) -> dict:
    """Три дефекта ADR-042 — определения, воспроизводящие числа ADR до знака.

    * ``unclosed_think`` — число ``<think>`` ≠ числу ``</think>``;
    * ``tool_call_in_think`` — ``<tool_call>`` внутри ПАРНОГО блока ``<think>…</think>``
      (непарные блоки пропускаются: их содержимое границы не имеет);
    * ``no_answer`` — в последнем assistant-сообщении есть ``<think>``, но нет
      ``</think>`` (рассуждение не закрыто → ответа после него нет).
    """
    txt, last = assistant_text(rec), last_assistant(rec)
    return {
        "unclosed_think": txt.count(THINK_OPEN) != txt.count(THINK_CLOSE),
        "tool_call_in_think": any(CALL_OPEN in g for g in _PAIR_RE.findall(txt)),
        "no_answer": THINK_OPEN in last and THINK_CLOSE not in last,
    }


def residual_notes(rec: dict) -> set[str]:
    """Что осталось неидеальным ПОСЛЕ нормализации, хотя трёх проверок не касается.

    «0 % дефектов» — это про три класса ADR-042, а не про «пример идеален».
    Остаток называется числом, чтобы зелёная проверка не читалась как гарантия:
    у ответа без вызова (``<tool_response>`` без ``<tool_call>`` в том же
    сообщении) причина — выброшенный остаток шаблона на месте вызова; класс
    назван в ADR-042 как непроверенный («tool_call-структура в целом»).
    """
    out: set[str] = set()
    for m in rec.get("messages", []):
        if m.get("role") != "assistant":
            continue
        txt = m["content"]
        if RESP_OPEN in txt:
            #: Ищем вызов ПЕРЕД каждым ответом: между парами call→response
            #: счётчики обязаны идти в ногу.
            calls = txt.count(CALL_CLOSE)
            resps = txt.count(RESP_OPEN)
            if resps > calls:
                out.add("tool_response_without_call")
    return out


def duplicates_share(records: list[dict]) -> dict:
    """Дубликаты по нормализованным messages — метод карточки v12 (ADR-033 п.1)."""
    seen = set()
    for rec in records:
        norm = [[m.get("role"), m.get("content")] for m in rec["messages"]]
        seen.add(hashlib.sha256(json.dumps(norm, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
    n = len(records)
    uniq = len(seen)
    return {
        "examples": n,
        "unique_normalized": uniq,
        "duplicates_share_pct": round((n - uniq) / n * 100, 2) if n else 0.0,
        "method": "messages → [[role, content]] → json(sort_keys) → sha256; мощность множества хешей",
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── проход по набору ─────────────────────────────────────────────────────────

def run(in_path: Path, out_path: Path | None, log_path: Path | None,
        report_path: Path | None, audit: bool) -> dict:
    before_defects = Counter()
    after_defects = Counter()
    excluded = Counter()
    rules_total = Counter()
    changed = 0
    n = 0
    rec_before: list[dict] = []
    rec_after: list[dict] = []
    log_fh = log_path.open("w", encoding="utf-8") if (log_path and not audit) else None
    out_fh = out_path.open("w", encoding="utf-8") if (out_path and not audit) else None
    #: Записи без правок обязаны выходить байт-в-байт: иначе «нормализация» тихо
    #: меняет весь набор (порядок ключей, разделители) и сравнивать нечем.
    byte_identical = 0
    revert_failures = 0
    empty_think_after = 0
    residual = Counter()
    try:
        with in_path.open(encoding="utf-8") as fh:
            for line in fh:
                raw = line.rstrip("\n")
                if not raw.strip():
                    continue
                n += 1
                rec = json.loads(raw)
                for k, v in defects_of(rec).items():
                    if v:
                        before_defects[k] += 1
                new_rec, edits, rules, exc = normalize_record(rec)
                if exc is not None:
                    excluded[exc] += 1
                    if log_fh:
                        log_fh.write(json.dumps({
                            "index": n, "excluded": exc,
                            "exclusion_meaning": EXCLUSIONS[exc],
                            "source": rec.get("source"),
                        }, ensure_ascii=False) + "\n")
                    continue
                for k, v in defects_of(new_rec).items():
                    if v:
                        after_defects[k] += 1
                # Пустое рассуждение — честная цена правила R2 там, где
                # рассуждения до вызова не было вовсе. Считается отдельно,
                # чтобы «0 дефектов» не читалось как «всё содержательно».
                for m in new_rec["messages"]:
                    if m.get("role") == "assistant" and re.search(
                            re.escape(THINK_OPEN) + r"\s*" + re.escape(THINK_CLOSE), m["content"]):
                        empty_think_after += 1
                        break
                for r in residual_notes(new_rec):
                    residual[r] += 1
                #: Номер строки в новом наборе считается ДО добавления — иначе
                #: журнал указывает на следующий пример (проверено end-to-end:
                #: снятие правок по журналу возвращает исходный текст).
                out_index = len(rec_after) + 1
                rec_after.append(new_rec)
                if edits:
                    changed += 1
                    rules_total.update(rules)
                    # Обратимость проверяется на КАЖДОМ изменённом примере, а не
                    # на фикстуре: правка, которую нельзя снять, — это порча
                    # замороженных данных, а не нормализация (ADR-003).
                    for je in edits:
                        orig = rec["messages"][je["msg_index"]]["content"]
                        got = new_rec["messages"][je["msg_index"]]["content"]
                        if revert(got, je["edits"]) != orig:
                            revert_failures += 1
                    if log_fh:
                        #: `out_index` — номер строки в НОВОМ наборе. Без него журнал
                        #: обратим только вместе с исходным файлом, а с ним — парой
                        #: (новый набор + журнал): правки снимаются с новой строки.
                        log_fh.write(json.dumps({
                            "index": n, "out_index": out_index,
                            "rules": dict(rules), "messages": edits,
                            "source": rec.get("source"),
                        }, ensure_ascii=False) + "\n")
                if out_fh:
                    dumped = json.dumps(new_rec, ensure_ascii=False)
                    out_fh.write(dumped + "\n")
                    if dumped == raw:
                        byte_identical += 1
                if audit:
                    rec_before.append(rec)
    finally:
        if log_fh:
            log_fh.close()
        if out_fh:
            out_fh.close()

    report = {
        "tool": "tools/normalize_sft_dataset.py",
        "adr": "ADR-042",
        "input": {"path": str(in_path), "examples": n},
        "output": {"path": str(out_path) if out_path and not audit else None,
                   "examples": len(rec_after) if not audit else None},
        "defects_before": {k: {"examples": v, "share_pct": round(v / n * 100, 2)} for k, v in before_defects.items()},
        "defects_after": {k: {"examples": v, "share_pct": round(v / len(rec_after) * 100, 2) if rec_after else 0.0}
                          for k, v in after_defects.items()},
        "changed_examples": changed,
        "excluded": {"total": sum(excluded.values()),
                     "share_pct": round(sum(excluded.values()) / n * 100, 2) if n else 0.0,
                     "by_reason": {k: {"examples": v, "meaning": EXCLUSIONS[k]} for k, v in excluded.most_common()}},
        "rules": {k: {"examples": rules_total.get(k, 0), "meaning": RULES[k]} for k in RULES},
        "notes": [
            "правится только роль assistant: в system-промпте теги стоят как пример формата",
            "ответы не дорисовываются; примеры, которые механикой не чинятся, исключаются с причиной",
            "порядок блоков после нормализации проверяется: </think> → <tool_call> → </tool_call> → ответ",
        ],
    }
    report["empty_think_blocks_after"] = {
        "examples": empty_think_after,
        "share_pct": round(empty_think_after / len(rec_after) * 100, 2) if rec_after else 0.0,
        "note": "цена правила R2: там, где рассуждения до вызова не было, блок закрыт пустым (ADR-042 п.2а — «закрыть в точке завершения», а не удалить)",
    }
    report["residual_after_normalization"] = {
        k: {"examples": v, "meaning": RESIDUAL[k] if k in RESIDUAL else k} for k, v in residual.most_common()
    } or {"examples": 0, "meaning": "структурных остатков названных классов нет"}
    report["reversibility"] = {
        "checked_examples": changed,
        "failures": revert_failures,
        "method": "для каждого изменённого сообщения правки снимаются обратно и сверяются с исходным текстом побайтно",
    }
    if not audit:
        report["byte_identical_records"] = byte_identical
    else:
        report["duplicates_before"] = duplicates_share(rec_before)
    if report_path and not audit:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def verify_file(path: Path) -> dict:
    """Три проверки приёмки ADR-042 по готовому файлу + дубликаты + sha256."""
    n = 0
    bad = Counter()
    recs: list[dict] = []
    for line in path.open(encoding="utf-8"):
        if not line.strip():
            continue
        n += 1
        rec = json.loads(line)
        recs.append(rec)
        for k, v in defects_of(rec).items():
            if v:
                bad[k] += 1
    return {
        "path": str(path),
        "examples": n,
        "sha256": sha256_file(path),
        "checks": {
            "unclosed_think": {"examples": bad.get("unclosed_think", 0), "pass": bad.get("unclosed_think", 0) == 0},
            "tool_call_in_think": {"examples": bad.get("tool_call_in_think", 0),
                                   "pass": bad.get("tool_call_in_think", 0) == 0},
            "no_answer": {"examples": bad.get("no_answer", 0), "pass": bad.get("no_answer", 0) == 0},
        },
        "duplicates": duplicates_share(recs),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out")
    ap.add_argument("--log")
    ap.add_argument("--report")
    ap.add_argument("--audit", action="store_true",
                    help="только измерить и показать, ничего не писать")
    ap.add_argument("--verify", action="store_true",
                    help="проверить готовый набор тремя проверками приёмки")
    a = ap.parse_args()
    inp = Path(a.inp)
    if not inp.is_file():
        print(f"нет входа: {inp}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if a.verify:
        rep = verify_file(inp)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        # Проверка приёмки, а не справка: не прошедший набор обязан дать не-ноль,
        # иначе «0 % дефектов» проверялось бы глазами того, кто и так знает ответ.
        return EXIT_FAIL if not all(c["pass"] for c in rep["checks"].values()) else EXIT_OK
    if not a.audit and not a.out:
        print("нужен --out (или --audit)", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    rep = run(inp, Path(a.out) if a.out else None, Path(a.log) if a.log else None,
              Path(a.report) if a.report else None, a.audit)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    residual = sum(v["examples"] for v in rep["defects_after"].values())
    return EXIT_FAIL if (residual and not a.audit) else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
