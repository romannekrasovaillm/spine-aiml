#!/usr/bin/env python3
"""S3ah — проверка гипотезы «только начала рассуждений».

**Гипотеза.** S3ac показал, что сдвиг языка живёт не в корпусе целиком, а в
**позиции генерации**: первые 200 символов хода assistant дают кириллицу 0.0003
при 0.6402 в ответной части того же хода. Если носитель — только начало, то
дешевле всего перегенерировать **первые ~100 токенов** рассуждения по-русски, а
продолжение оставить от исходной (англоязычной) трассы. Объём падает с 13.0M
токенов на уникальный фрагмент до 100 токенов на фрагмент — это в десятки раз,
и именно поэтому гипотеза названа главным рычагом объёма.

**Что проверяется, а не предполагается.** Прибор собирает гибридный фрагмент
(русское начало + исходное продолжение) и мерит то, что можно померить:

* сохранность формата: парность маркеров и число ``<tool_call>``/``<tool_response>``
  пар относительно источника — вырезаемое окно не имеет права унести вызов
  инструмента (иначе гибрид ломает контур, а не лечит язык);
* стык: длина головы в токенах **учителя** (а не «примерно 100»), позиция стыка,
  кончается ли голова фразой, начинается ли продолжение фразой, доля кириллицы
  в окне ±200 символов вокруг стыка, есть ли на стыке английский коннектор;
* носитель: доля кириллицы в первых 200 символах гибрида — той самой позиции,
  которую S3ac назвал причиной. Здесь гипотеза должна выигрывать у исходника;
* объём: какую долю русского даёт гибрид против полного перевода.

**Чего прибор не делает.** Не выносит вердикт о читаемости: «читается ли как
согласованный текст» — это чтение, а не измерение. Собранные фрагменты
складываются в ``heads.jsonl``, и вердикт вписывается названным (``--verdict``),
как в S3ag. Числа рядом с вердиктом нужны, чтобы вердикт был проверяем.

Режимы::

    run     — сгенерировать головы учителем, собрать гибриды, померить
    report  — собрать evidence/s3ah-think-heads.json (вердикт вписывается)

Коды возврата::

    0 — гибриды собраны (все фрагменты прошли механический контракт)
    1 — FAIL: контракт формата нарушен на собранном гибриде
    2 — NOT-VERIFIED: нет входа (набор, учитель, эндпоинт)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from russian_think_pilot import (  # noqa: E402
    RE_THINK_PAIR, RE_TOOLCALL_PAIR, cyr_share, en_word_share, load_source_records,
    marker_counts, parse_linenos, segment_assistant, split_edges,
)

CASE_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

GB10_SHARED = Path("/home/user/gb10-shared")
DEFAULT_SFT = GB10_SHARED / "datasets/sft_train_v12.jsonl"

#: Позиция, названная носителем сдвига (S3ac): первые 200 символов хода assistant.
CARRIER_CHARS = 200

HEAD_SYSTEM = (
    "Ты — агент, который рассуждает по-русски. Тебе показывают начало трассы "
    "ML/AI-агента, и ты продолжаешь её рассуждение."
)

# Вариант A — буквальное прочтение гипотезы: ровно первые ~100 токенов, обрыв
# как получится. Вариант B — лучшее возможное исполнение той же идеи: голова
# доводится до конца фразы, продолжение начинается с новой фразы. Если идея
# несостоятельна даже в лучшем исполнении, это её свойство, а не дефект нарезки.
HEAD_USER_CUT = """Ниже — запрос пользователя и (если есть) предыдущие ходы агента.
Начни рассуждение агента на русском языке, примерно {n} токенов — три-четыре фразы.
Это ТОЛЬКО текст рассуждения: не вызывай инструменты и не пиши угловых тегов,
вызов идёт отдельным блоком после рассуждения.

Контекст:
---
{context}
---
Начало рассуждения на русском:"""

HEAD_USER_SENTENCE = """Ниже — запрос пользователя и (если есть) предыдущие ходы агента.
Начни рассуждение агента на русском языке: одна законченная мысль, примерно {n} токенов.
Закончи предложение точкой. Это ТОЛЬКО текст рассуждения: не вызывай инструменты и
не пиши угловых тегов, вызов идёт отдельным блоком после рассуждения.

Контекст:
---
{context}
---
Начало рассуждения на русском:"""

#: Повтор для случая, когда учитель вместо рассуждения пишет вызов инструмента.
#: Контекст трассы прямо предлагает инструмент, и первый порыв модели — позвать
#: его; это факт о контексте, а не о языке. Повтор называет запрет жёстче и
#: убирает из контекста упоминания инструмента, оставляя сам запрос: меряется
#: способность выдать русское начало, а не послушание.
HEAD_USER_STRICT = """Ты пишешь начало рассуждения агента на русском языке — {n} токенов,
три-четыре фразы. Запрещено: вызывать инструменты, писать угловые теги, добавлять
пояснения. Только связный текст рассуждения на русском.

Запрос пользователя:
---
{context}
---
Начало рассуждения:"""

#: Английские коннекторы, с которых естественно начинается продолжение мысли.
#: Их наличие на стыке — лучшее, что может дать гибрид; отсутствие — не приговор,
#: но признак шва.
CONNECTIVES = ("thus", "therefore", "so ", "then", "next", "now ", "however",
               "but ", "first", "second", "finally", "hence", "this means",
               "note that", "i should", "i need", "i'll", "i will", "let me")

TERMINAL = ".!?…:;"


def note(msg: str) -> None:
    print(msg, file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_post_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def chat(endpoint: str, model: str, system: str, user: str, max_tokens: int,
         timeout: float) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    started = time.time()
    try:
        d = http_post_json(endpoint.rstrip("/") + "/v1/chat/completions", payload, timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"error": f"{type(e).__name__}: {e}", "seconds": time.time() - started}
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    t = d.get("timings") or {}
    return {
        "text": (msg.get("content") or "").strip(),
        "reasoning_chars": len(msg.get("reasoning_content") or ""),
        "seconds": round(time.time() - started, 3),
        "completion_tokens": (d.get("usage") or {}).get("completion_tokens"),
        "prompt_tokens": (d.get("usage") or {}).get("prompt_tokens"),
        "predicted_n": t.get("predicted_n"),
        "finish_reason": ch.get("finish_reason"),
    }


def token_count(endpoint: str, model: str, text: str, timeout: float,
                baseline: dict) -> int | None:
    """Сколько токенов у учителя в этом тексте.

    Отдельного `/tokenize` у роутера нет, зато есть `timings.prompt_n` — число
    токенов промпта, посчитанное самим движком. Вычитание пустого промпта даёт
    счётчик токенов учителя, а не догадку «4 символа на токен»: у русского и
    английского текста это отношение разное, и «первые 100 токенов» без такого
    счётчика — не порог, а пожелание.
    """
    r = chat(endpoint, model, "x", text or "x", 1, timeout)
    if r.get("error") or r.get("prompt_tokens") is None:
        return None
    return max(0, r["prompt_tokens"] - baseline.get("prompt_tokens", 0))


def cut_at_tokens(text: str, n_tokens: int, chars_per_tok: float) -> int:
    """Позиция в тексте, соответствующая ~n токенам (по замеренному отношению).

    Точный счётчик токенов доступен только на целом тексте, поэтому позиция
    оценивается по отношению, замеренному на самом этом фрагменте, а затем
    **проверяется**: `token_count(text[:cut])` возвращается рядом в отчёте.
    """
    return max(1, min(len(text), int(round(n_tokens * chars_per_tok))))


def protect_toolcalls(text: str, cut: int) -> tuple[int, int]:
    """Подвинуть рез так, чтобы ни один вызов инструмента не остался за краем.

    Возвращает (новый_cut, сколько_пар_защищено). Гибрид обязан сохранять все
    пары ``<tool_call>…</tool_call>``: потеря вызова — поломка контура, а не
    языковая правка. Пары, начинающиеся до реза, уходят в продолжение целиком —
    рез сдвигается к началу первой такой пары.
    """
    guarded = 0
    for m in RE_TOOLCALL_PAIR.finditer(text):
        if m.start() < cut:
            cut = m.start()
            guarded += 1
    return max(1, cut), guarded


def cut_at_sentence(text: str, nominal: int) -> tuple:
    """Ближайшая граница предложения к номинальному резу.

    Буквальный рез «по первым 100 токенам» падает в середину слова: продолжение
    начинается с обрывка. Проверять гипотезу на таком стыке — значит проверять
    свою нарезку, а не идею. Поэтому у варианта «до границы фразы» рез двигается
    к ближайшему концу предложения, а сдвиг называется числом.
    """
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s|$)", text)]
    if not ends:
        return nominal, 0
    best = min(ends, key=lambda e: abs(e - nominal))
    return best, best - nominal


def last_sentence_end(text: str, floor: int = 40) -> int:
    """Позиция конца последнего законченного предложения (или len(text))."""
    best = -1
    for m in re.finditer(r"[.!?…](?=\s|$)", text):
        if m.end() >= floor:
            best = m.end()
    return best if best > 0 else len(text)


def seam_metrics(assembled: str, head: str, source: str, cut: int) -> dict:
    """Что видно на стыке русского начала и английского продолжения."""
    h = len(head)
    win = 200
    before = assembled[max(0, h - win):h]
    after = assembled[h:h + win]
    head_stripped = head.rstrip()
    tail = source[cut:].lstrip()
    low = tail[:40].lower()
    # Началом новой единицы считается и конец предложения, и конец пункта списка:
    # в этих трассах рассуждение размечено markdown-ом, и «2. **slug**» — такое же
    # чистое начало, как «The user asks…». Требование заглавной буквы отвергало бы
    # половину настоящих границ и выдавало бы качество нарезки за качество стыка.
    return {
        "head_chars": h,
        "head_ends_with_terminal": bool(head_stripped) and head_stripped[-1] in TERMINAL,
        "head_last_chars": head_stripped[-24:],
        "tail_after_terminator": source[cut - 1:cut] in (".", "!", "?", "…", "\n"),
        "tail_at_word_boundary": bool(tail) and (source[cut - 1:cut].isspace()
                                                 or source[cut:cut + 1].isspace()
                                                 or source[cut:cut + 1] == ""),
        "tail_starts_sentence": source[cut - 1:cut] in (".", "!", "?", "…", "\n"),
        "tail_first_chars": tail[:24],
        "connective_at_seam": next((c for c in CONNECTIVES if low.startswith(c)), None),
        "cyr_before_seam": round(cyr_share(before), 4),
        "cyr_after_seam": round(cyr_share(after), 4),
        "en_word_share_after_seam": round(en_word_share(after), 4),
    }


def build_prompt_context(rec: dict, max_chars: int = 4000) -> str:
    """Контекст, который видит учитель: всё до хода assistant, как в наборе."""
    parts = []
    for m in rec.get("messages", []):
        if m.get("role") == "assistant":
            break
        parts.append(f"[{m.get('role')}]\n{m.get('content') or ''}")
    ctx = "\n\n".join(parts)
    return ctx[:max_chars]


def build_user_request(rec: dict, max_chars: int = 1500) -> str:
    """Только запрос пользователя — контекст для повтора без упоминаний инструмента."""
    for m in reversed(rec.get("messages", [])):
        if m.get("role") == "user":
            return (m.get("content") or "")[:max_chars]
    return ""


def head_is_clean(text: str) -> tuple[bool, str]:
    """Голова обязана быть текстом рассуждения, а не вызовом инструмента.

    Учитель, глядя на трассу с инструментом, первым делом зовёт инструмент —
    это свойство контекста. Голова с маркерами не гибрид, а второй вызов:
    она меняет число маркеров формата, и принимать её нельзя.
    """
    if not text.strip():
        return False, "пустая голова"
    bad = [m for m, c in marker_counts(text).items() if c]
    if bad:
        return False, f"учитель принёс маркеры в голову: {bad}"
    return True, ""


def gen_head(args, system: str, tmpl: str, rec: dict, max_tokens: int,
             baseline: dict) -> dict:
    """Голова с одним повтором: строгий шаблон, если первый ответ — не рассуждение."""
    ctx = build_prompt_context(rec)
    user = tmpl.replace("{n}", str(args.head_tokens)).replace("{context}", ctx)
    r = chat(args.endpoint, args.model, system, user, max_tokens, args.timeout)
    r["attempts"] = 1
    r["retried"] = False
    r["strict_context"] = False
    ok, why = head_is_clean(r.get("text") or "") if not r.get("error") else (False, r["error"])
    if ok:
        return r
    r["first_attempt_reject"] = why
    r["first_attempt_head"] = (r.get("text") or "")[:200]
    req = build_user_request(rec)
    user2 = (HEAD_USER_STRICT.replace("{n}", str(args.head_tokens)).replace("{context}", req))
    r2 = chat(args.endpoint, args.model,
              "Ты пишешь только текст рассуждения агента на русском языке.",
              user2, max_tokens, args.timeout)
    r2["attempts"] = 2
    r2["retried"] = True
    r2["strict_context"] = True
    r2["first_attempt_reject"] = why
    r2["first_attempt_head"] = (r.get("text") or "")[:200]
    return r2


def do_run(args) -> int:
    sft = Path(args.sft_jsonl)
    if not sft.exists():
        note(f"NOT-VERIFIED: нет SFT-набора {sft}")
        return EXIT_NOT_VERIFIED
    if not args.endpoint or not args.model:
        note("NOT-VERIFIED: не задан учитель (--endpoint/--model)")
        return EXIT_NOT_VERIFIED

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Базовая линия счётчика токенов: пустой промпт + один символ. Разница даёт
    # цену обёртки чат-шаблона, которую надо вычесть из prompt_n фрагмента.
    base = chat(args.endpoint, args.model, "x", "x", 1, args.timeout)
    if base.get("error"):
        note(f"NOT-VERIFIED: учитель не ответил: {base['error']}")
        return EXIT_NOT_VERIFIED
    baseline = {"prompt_tokens": base.get("prompt_tokens")}

    linenos = parse_linenos(args.linenos)
    recs = load_source_records(sft, args.n, linenos=linenos)
    if not recs:
        note("NOT-VERIFIED: ни одной записи с <think>")
        return EXIT_NOT_VERIFIED

    items, failures = [], []
    for r in recs:
        rec = json.loads(r["line"])
        asst = [m for m in rec["messages"] if m.get("role") == "assistant"][0]
        segs = segment_assistant(asst["content"])
        think_idx = [i for i, (k, _) in enumerate(segs) if k == "think"]
        if not think_idx:
            continue
        src_core, lead, trail = split_edges(segs[think_idx[0]][1])
        if len(src_core) < args.min_fragment:
            continue
        if len(items) >= args.n_fragments:
            break

        # A: буквально первые ~100 токенов; B: та же идея, но доведённая до фразы.
        head_a = gen_head(args, HEAD_SYSTEM, HEAD_USER_CUT, rec, args.head_tokens, baseline)
        head_b = gen_head(args, HEAD_SYSTEM, HEAD_USER_SENTENCE, rec,
                          int(args.head_tokens * args.sentence_slack), baseline)
        for name, h in (("cut100", head_a), ("sentence", head_b)):
            ok, why = (False, h["error"]) if h.get("error") else head_is_clean(h.get("text") or "")
            if not ok:
                failures.append({"lineno": r["lineno"], "variant": name, "error": why,
                                 "retried": h.get("retried"),
                                 "first_attempt_reject": h.get("first_attempt_reject"),
                                 "head": (h.get("text") or "")[:200]})
        if head_a.get("error") or head_b.get("error"):
            continue

        # Счётчик токенов учителя: голова меряется ЕГО счётчиком, а не символьной
        # оценкой; источник режется по замеренному отношению символов к токену.
        src_tokens = token_count(args.endpoint, args.model, src_core, args.timeout, baseline)
        cpt = (len(src_core) / src_tokens) if src_tokens else args.chars_per_tok

        variants = {}
        for name, h, do_trim in (("cut100", head_a, False), ("sentence", head_b, True)):
            if any(f["lineno"] == r["lineno"] and f["variant"] == name for f in failures):
                continue
            head = h["text"].strip()
            if do_trim:
                head = head[:last_sentence_end(head)].rstrip()
            if not head:
                failures.append({"lineno": r["lineno"], "variant": name, "error": "пустая голова"})
                continue
            nominal = cut_at_tokens(src_core, args.head_tokens, cpt)
            cut_shift = 0
            if do_trim:
                nominal, cut_shift = cut_at_sentence(src_core, nominal)
            cut, guarded = protect_toolcalls(src_core, nominal)
            head_tokens = token_count(args.endpoint, args.model, head, args.timeout, baseline)
            assembled_core = head + " " + src_core[cut:].lstrip()
            variants[name] = {
                "head": head,
                "head_tokens_teacher": head_tokens,
                "head_chars": len(head),
                "head_attempts": h.get("attempts"),
                "head_retried": h.get("retried"),
                "head_strict_context": h.get("strict_context"),
                "head_first_attempt_reject": h.get("first_attempt_reject"),
                "head_finish_reason": h.get("finish_reason"),
                "head_hit_max_tokens": h.get("finish_reason") == "length",
                "cut_chars_nominal": nominal,
                "cut_chars_used": cut,
                "cut_shift_to_sentence": cut_shift,
                "toolcall_pairs_guarded": guarded,
                "src_tokens_teacher": src_tokens,
                "chars_per_tok_measured": round(cpt, 3),
                "assembled_core": assembled_core,
                "assembled": lead + assembled_core + trail,
                "seam": seam_metrics(assembled_core, head, src_core, cut),
                "cyr_share_assembled": round(cyr_share(assembled_core), 4),
                "cyr_share_source": round(cyr_share(src_core), 4),
                "cyr_first200_assembled": round(cyr_share(assembled_core[:CARRIER_CHARS]), 4),
                "cyr_first200_source": round(cyr_share(src_core[:CARRIER_CHARS]), 4),
                "en_word_share_assembled": round(en_word_share(assembled_core), 4),
                "len_ratio": round(len(assembled_core) / max(len(src_core), 1), 4),
                "markers_source": marker_counts(src_core),
                "markers_assembled": marker_counts(assembled_core),
                "toolcall_pairs_source": len(RE_TOOLCALL_PAIR.findall(src_core)),
                "toolcall_pairs_assembled": len(RE_TOOLCALL_PAIR.findall(assembled_core)),
            }

        items.append({
            "lineno": r["lineno"],
            "seg_index": think_idx[0],
            "task_type": rec.get("task_type"),
            "source_core": src_core,
            "source_chars": len(src_core),
            "variants": variants,
        })
        note(f"строка {r['lineno']}: фрагмент {len(src_core)} симв., "
             f"голова A {variants.get('cut100', {}).get('head_tokens_teacher')} ток., "
             f"B {variants.get('sentence', {}).get('head_tokens_teacher')} ток.")

    # Ни одного гибрида — это NOT-VERIFIED, а не «прогон без расхождений»: пустой
    # результат не доказывает ничего, и молча вернуть его за успех нельзя.
    if not any(it["variants"] for it in items):
        note(f"NOT-VERIFIED: ни одного гибрида не собрано (отказов {len(failures)})")
        (out / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        return EXIT_NOT_VERIFIED

    doc = {
        "schema": "s3ah-think-heads-run/1",
        "stage": "S3ah",
        "date": now_iso(),
        "teacher": {"endpoint": args.endpoint, "model": args.model,
                    "temperature": 0.0, "thinking": False,
                    "head_tokens_requested": args.head_tokens},
        "source": {"path": str(sft), "linenos": [i["lineno"] for i in items],
                   "selection": "те же строки, что у пилотов S3ag"},
        "n_fragments": len(items),
        "failures": failures,
        "items": items,
    }
    (out / "heads-raw.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    with (out / "heads.jsonl").open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps({
                "lineno": it["lineno"], "task_type": it["task_type"],
                "source_core": it["source_core"],
                "assembled_cut100": it["variants"].get("cut100", {}).get("assembled"),
                "assembled_sentence": it["variants"].get("sentence", {}).get("assembled"),
            }, ensure_ascii=False) + "\n")
    note(f"записано: {out}/heads-raw.json, {out}/heads.jsonl")

    # Контракт формата на собранном гибриде: потеря вызова инструмента — FAIL.
    broken = []
    for it in items:
        for name, v in it["variants"].items():
            if v["toolcall_pairs_assembled"] != v["toolcall_pairs_source"]:
                broken.append({"lineno": it["lineno"], "variant": name,
                               "source": v["toolcall_pairs_source"],
                               "assembled": v["toolcall_pairs_assembled"]})
            if (v["markers_assembled"]["<tool_response>"] != v["markers_source"]["<tool_response>"]
                    or v["markers_assembled"]["</tool_response>"] != v["markers_source"]["</tool_response>"]):
                broken.append({"lineno": it["lineno"], "variant": name,
                               "error": "изменилось число маркеров tool_response"})
    if broken:
        note(f"FAIL: контракт формата нарушен на {len(broken)} гибридах")
        (out / "contract-failures.json").write_text(
            json.dumps(broken, ensure_ascii=False, indent=2), encoding="utf-8")
        return EXIT_FAIL
    return EXIT_OK


#: Слова, чей референт остался в ВЫРЕЗАННОМ английском начале. Хвост, который
#: начинается с такого слова, ссылается на содержание, которого в гибриде больше
#: нет: «They are analyzed…» — на что «they»? Это механический признак разрыва,
#: который не видно ни по длине, ни по доле кириллицы.
ANAPHORA = ("they", "them", "their", "this", "these", "those", "it", "its", "that",
            "such", "he", "she", "but", "and", "however", "therefore", "thus", "so",
            "then", "hence", "also", "moreover", "furthermore", "meanwhile",
            "instead", "rather", "here", "there", "the same", "both", "either",
            "neither", "another", "other")


def tail_anaphora(assembled: str, head_chars: int) -> str | None:
    """Первое слово продолжения, если оно ссылается на вырезанное начало."""
    tail = (assembled or "")[head_chars:].lstrip()
    low = tail[:24].lower()
    return next((w for w in ANAPHORA if low.startswith(w + " ") or low.startswith(w + ",")
                 or low.startswith(w + "'")), None)


def do_report(args) -> int:
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    verdict = (json.loads(Path(args.verdict).read_text(encoding="utf-8"))
               if args.verdict and Path(args.verdict).exists() else None)

    def agg(variant: str) -> dict:
        rows = [it["variants"][variant] for it in raw["items"] if variant in it["variants"]]
        if not rows:
            return {}
        # Признак разрыва считается на сборке отчёта, из сохранённого гибрида:
        # перезапускать прогон ради одной метки незачем.
        for r in rows:
            # Именно `assembled_core` (без ведущих пробелов сегмента): сдвиг на
            # пробел увёл бы срез с головы на хвост и метка молчала бы всегда.
            r["tail_anaphora"] = tail_anaphora(r.get("assembled_core", ""),
                                               r.get("head_chars", 0))
        n = len(rows)
        return {
            "n": n,
            "head_tokens_median": sorted(r["head_tokens_teacher"] for r in rows)[n // 2],
            # Русского в гибриде ровно столько, сколько стоит голова. Чтобы
            # сравнение с полным проходом было честным, рядом лежит доля от
            # полной генерации фрагмента: дешевле она РОВНО во столько же раз,
            # во сколько меньше русского, — это не экономия, а пропорция.
            "src_tokens_median": sorted(r["src_tokens_teacher"] or 0 for r in rows)[n // 2],
            "head_share_of_full_generation": round(
                sorted(r["head_tokens_teacher"] / max(r["src_tokens_teacher"] or 1, 1)
                       for r in rows)[n // 2], 4),
            "head_retried": sum(1 for r in rows if r.get("head_retried")),
            "cut_shift_median": sorted(r.get("cut_shift_to_sentence", 0) for r in rows)[n // 2],
            "cyr_first200_median": round(sorted(r["cyr_first200_assembled"] for r in rows)[n // 2], 4),
            "cyr_first200_source_median": round(sorted(r["cyr_first200_source"] for r in rows)[n // 2], 4),
            "cyr_whole_median": round(sorted(r["cyr_share_assembled"] for r in rows)[n // 2], 4),
            "cyr_whole_source_median": round(sorted(r["cyr_share_source"] for r in rows)[n // 2], 4),
            "len_ratio_median": round(sorted(r["len_ratio"] for r in rows)[n // 2], 4),
            "head_ends_with_terminal": sum(1 for r in rows if r["seam"]["head_ends_with_terminal"]),
            "tail_starts_sentence": sum(1 for r in rows if r["seam"]["tail_starts_sentence"]),
            "connective_at_seam": sum(1 for r in rows if r["seam"]["connective_at_seam"]),
            "both_boundaries_clean": sum(1 for r in rows if r["seam"]["head_ends_with_terminal"]
                                         and r["seam"]["tail_starts_sentence"]),
            "toolcall_pairs_preserved": sum(1 for r in rows
                                            if r["toolcall_pairs_assembled"] == r["toolcall_pairs_source"]),
            "cyr_after_seam_median": round(sorted(r["seam"]["cyr_after_seam"] for r in rows)[n // 2], 4),
            "tail_starts_with_anaphora": sum(1 for r in rows if r.get("tail_anaphora")),
            "anaphora_words": sorted({r["tail_anaphora"] for r in rows if r.get("tail_anaphora")}),
        }

    doc = {
        "schema": "s3ah-think-heads/1",
        "stage": "S3ah",
        "date": now_iso(),
        "status": "complete",
        "purpose": ("проверка главного рычага объёма: годится ли гибрид «русское начало "
                    "рассуждения + исходное продолжение» для обучения языка ВХОДА, или "
                    "рычаг закрыт и выбор переносится на долю объёма"),
        "teacher": raw["teacher"],
        "source": raw["source"],
        "n_fragments": raw["n_fragments"],
        "thresholds_declared_before": {
            "head_tokens": raw["teacher"]["head_tokens_requested"],
            "carrier_chars": CARRIER_CHARS,
            "note": ("пороги объявлены до чтения: голова ~100 токенов (буквальное прочтение "
                     "гипотезы) и «голова до конца фразы» (лучшее исполнение той же идеи)"),
        },
        "aggregate": {v: agg(v) for v in ("cut100", "sentence")},
        "examples": [{
            "lineno": it["lineno"],
            "source_chars": it["source_chars"],
            "source_core": it["source_core"][:1200],
            "cut100": it["variants"].get("cut100", {}).get("assembled", "")[:1200],
            "sentence": it["variants"].get("sentence", {}).get("assembled", "")[:1200],
            "seam_cut100": it["variants"].get("cut100", {}).get("seam"),
            "seam_sentence": it["variants"].get("sentence", {}).get("seam"),
        } for it in raw["items"]],
        "failures": raw["failures"],
        "verdict": verdict or {
            "verdict": "не назван",
            "why": "вердикт вписывается названным (--verdict), как в S3ag: читаемость — чтение, а не измерение",
        },
        "volume_effect": {},
    }
    if verdict:
        doc["volume_effect"] = verdict.get("volume_effect", {})
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        note(f"записано: {args.out}")
    else:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="S3ah: гипотеза «только начала рассуждений»")
    ap.add_argument("--mode", choices=["run", "report"], required=True)
    ap.add_argument("--sft-jsonl", default=str(DEFAULT_SFT))
    ap.add_argument("--endpoint", default="http://127.0.0.1:18081")
    ap.add_argument("--model", default="qwen3.8-27b@2026-08-18@llama.cpp")
    # По умолчанию — строки S3ag, у которых первый <think>-фрагмент длиннее 1200
    # символов (на коротком мерить нечего). Это те же записи, на которых уже
    # сравнивались учителя: выводы стыкуются без «а вот на других примерах».
    ap.add_argument("--linenos", default="434,864,1707,3013,3443,4713,5138,5567,5985",
                    help="строки источника (по умолчанию — длинные фрагменты из набора S3ag)")
    ap.add_argument("--n", type=int, default=40, help="сколько записей просмотреть при отборе")
    ap.add_argument("--n-fragments", type=int, default=8)
    ap.add_argument("--min-fragment", type=int, default=1200,
                    help="минимальная длина фрагмента: на коротком продолжение нечего мерить")
    ap.add_argument("--head-tokens", type=int, default=100)
    ap.add_argument("--sentence-slack", type=float, default=1.6)
    ap.add_argument("--chars-per-tok", type=float, default=3.9,
                    help="запасная оценка, если счётчик токенов недоступен")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--raw", default="")
    ap.add_argument("--verdict", default="")
    args = ap.parse_args()
    if args.mode == "run":
        if not args.out:
            note("--out обязателен в режиме run")
            return EXIT_NOT_VERIFIED
        return do_run(args)
    if not args.raw or not Path(args.raw).exists():
        note("NOT-VERIFIED: нет файла прогона (--raw)")
        return EXIT_NOT_VERIFIED
    return do_report(args)


if __name__ == "__main__":
    sys.exit(main())
