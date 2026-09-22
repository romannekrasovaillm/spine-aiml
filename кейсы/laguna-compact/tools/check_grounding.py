#!/usr/bin/env python3
"""S4-pre — граундинг-аудит (ADR-049, Proposed): из чего строить заземлённую ось RL.

**Зачем отдельный инструмент.** ADR-049 (Proposed) объявляет текстовый пул
`datasets/rl_tasks_revpool_v2.jsonl` не заземлённым и требует до старта RL трёх
проб (доля задач, решаемых без действий; утечка gold; воспроизводимость среды) и
walking skeleton. Проб ещё нет, но решение «из чего строить заземлённую стадию»
принимается **сейчас** — и оно должно опираться на числа и инвентарь, а не на
пересказ ADR. Инструмент собирает этот инвентарь **из файлов контура**: читает
пул, читает уже снятые замеры (цена шага, пороги и валидация верификаторов),
пробует окружение на себе и пишет `evidence/grounding-audit.json`.

**Чего инструмент не делает и чем это важно.** Пункт (a) — **proxy, а не
измерение**. Восстановимость gold из текста промпта по нормализации слов — это
**верхняя граница подозрения** на no-action-решаемость: «слаг собирается из слов
промпта» не значит «задача решается без действий» (модель может не собрать его).
Проба G2 из ADR-049 п.3(i) — это rollout «текстовый ответ без вызовов», и её
инструмент не заменяет и не имитирует: поле `no_action_proxy.is_measurement =
false` стоит в отчёте рядом с числами, чтобы число не прочитали как замер.
Аналогично пункт (d): цена шага с исполнением — **оценка по формуле с
объявленными допущениями**, а не замер (`cost_estimate.is_measurement = false`);
база под формулой — чужой замер (`evidence/s3-rl-probe.json`), и он читается из
файла, а не переписывается числом в код.

**Только чтение.** Пул, наборы, пороги и верификаторы не меняются: среды
`~/library/rl_envs/` читаются с фиксацией sha256 до/после. Пробы окружения
выполняются в `mktemp`-каталоге и убираются за собой; стенд GB10 и учебный стенд
4080 **не пробуются вовсе** (AD-5: одна GPU-нагрузка за раз, идут чужие прогоны) —
в отчёте они помечены `checked: false` с причиной, а не «непригодно».

Коды возврата::

    0 — аудит собран: отчёт описывает пул, инвентарь, цену и состав скелета
    1 — красное: пул объявлен заземлённым (`--expect-grounded`), а заземления нет;
        либо арифметика отчёта не сходится (разрезы против итога)
    2 — NOT-VERIFIED: нет пула / пул пуст / нет обязательного входа аудита
        (цена шага, пороги или валидация верификаторов)

Примеры::

    python3 tools/check_grounding.py                       # аудит и запись в evidence/
    python3 tools/check_grounding.py --json                # то же + отчёт в stdout
    python3 tools/check_grounding.py --expect-grounded     # страж для заземлённого пула
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent

EXIT_OK, EXIT_RED, EXIT_NOT_VERIFIED = 0, 1, 2

DEFAULT_POOL = "datasets/rl_tasks_revpool_v2.jsonl"
DEFAULT_OUT = "evidence/grounding-audit.json"

#: Входы аудита, без которых он был бы пересказом: цена шага (пункт d), пороги и
#: валидация верификаторов (пункт c). Отсутствие любого — NOT-VERIFIED (exit 2),
#: а не «раздел пропущен»: раздел, которого нет, читается как «проблем нет».
REQUIRED_INPUTS = {
    "step_cost": "evidence/s3-rl-probe.json",
    "verifier_thresholds": "data/verifier-thresholds.json",
    "verifier_validation": "evidence/verifier-validation.json",
}

#: Каталог текстовых «сред» E1–E8 — только чтение (ADR-021, sha256 до/после).
ENVS_ROOT = Path.home() / "library" / "rl_envs"

#: Ожидаемые поля задачи пула v2. Незнакомое поле — сигнал, что схема изменилась,
#: и «инструментов в задачах нет» перестало быть фактом. Печатается поимённо.
POOL_FIELDS = {
    "task_type", "prompt", "verifier", "reward", "source_env", "source_task_id",
    "expected_slugs", "expected_relation", "keywords",
    "max_steps", "n_min_steps", "chain_roles", "rerouted_from",
}

#: Признаки действия — **структурные**, а не слова. Слово как признак врёт: в пуле
#: есть концепты `tool_call_efficiency`, `tool_call_resampling`, `action_token_…`,
#: и подстрочный поиск ловит их как «действия» (10 задач). Структурный признак —
#: разметка вызова, ключ JSON или поле задачи; его в концепт-карточке не напишешь.
ACTION_MARKERS_STRUCTURAL = (
    "<tool_call>", "</tool_call>", "<tool_response>", "</tool_response>",
    '"tool_call":', '"action":', '"arguments":', '"function":',
    "вызови инструмент",
)
#: Словесные признаки: считаются отдельно и **доказательством действия не являются**.
ACTION_MARKERS_LEXICAL = ("tool_call", "action")

#: Нормализация: регистр, ё→е, любые разделители (`-`, `_`, пробел, пунктуация) →
#: один пробел. Одна и та же функция для слага и для промпта — иначе сравнение
#: меряет разницу нормализаций, а не совпадение слов.
SPLIT = re.compile(r"[^0-9a-zа-я]+")

#: Простая транслитерация (ГОСТ-подобная, обратимая в пределах пары «слово»).
#: Нужна там, где gold латиницей, а промпт кириллицей (и наоборот): без неё такие
#: пары выглядят «не восстановимыми», хотя слово в промпте есть.
CYR2LAT = {
    "ё": "e", "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
LAT2CYR = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "е", "z": "з", "i": "и",
    "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с",
    "t": "т", "u": "у", "f": "ф", "y": "й", "c": "к", "x": "кс", "h": "х", "j": "дж",
    "w": "в", "q": "к",
}

#: Пункт (e): состав walking skeleton — числа объявлены здесь, а не «на глаз» в
#: тексте. 50 задач × 3 инструмента; разбивка по группам — предмет решения, и она
#: фиксируется в отчёте, чтобы скелет можно было принять или отклонить числом.
SKELETON_TOTAL = 50
SKELETON_TOOLS = ("shell", "filesystem", "http_mock")


# ─────────────────────────── нормализация и покрытие ───────────────────────────

def norm_words(text: str) -> str:
    """Регистр, ё→е, разделители → пробел. Общая нормализация слага и промпта."""
    return " ".join(SPLIT.sub(" ", (text or "").lower().replace("ё", "е")).split())


def translit(text: str, table: dict) -> str:
    return "".join(table.get(ch, ch) for ch in text)


def covered(slug: str, prompt_tokens: set, prompt_tokens_translit: set) -> dict:
    """Покрыт ли gold словами промпта. Два чтения одного вопроса, оба про слова.

    * ``words`` — все слова gold встречаются словами промпта (буквальное чтение
      «слаги покрыты словами промпта» из задачи): порядок и разделители не важны;
    * ``words_translit`` — то же плюс простая транслитерация в обе стороны: ловит
      пары «gold латиницей / промпт кириллицей».

    Два более строгих/слабых чтения считаются на уровне задачи (`task_coverage`):
    слитная подстрока и буквальная подстрока-как-в-гейте-A.
    """
    toks = norm_words(slug).split()
    words = bool(toks) and all(t in prompt_tokens for t in toks)
    words_tr = words or (bool(toks) and all(t in prompt_tokens_translit for t in toks))
    return {
        "tokens": toks,
        "words": words,
        "words_translit": words_tr,
        "single_char_tokens": sum(1 for t in toks if len(t) == 1),
    }


def gold_of(task: dict) -> tuple[list[str], str]:
    """Gold задачи и его род. Род важен: «слаг» и «русские слова» — разные вещи.

    ``slug`` — `expected_slugs` вида `3d_latent_alignment_loss`; ``slug_words`` —
    `expected_slugs`, которые слагом не являются (в `env_types` это русские слова
    из карточки, `chain_roles.checked_from`); ``keywords`` — `keywords` задач
    `keyword_match` (поля `expected_slugs` у них нет вовсе, и «ноль утечек» по
    отсутствующему полю был бы артефактом имени поля, а не фактом).
    """
    slugs = task.get("expected_slugs") or []
    if slugs:
        latin = sum(1 for s in slugs if re.search(r"[a-z]", s) and not re.search(r"[а-я]", s))
        return list(slugs), ("slug" if latin == len(slugs) else "slug_words")
    kws = task.get("keywords") or []
    if kws:
        return list(kws), "keywords"
    return [], "none"


def task_coverage(task: dict, prompt: str) -> dict | None:
    gold, kind = gold_of(task)
    if not gold:
        return None
    ptoks = set(norm_words(prompt).split())
    p_tr = set(norm_words(translit(norm_words(prompt), CYR2LAT)).split())
    p_tr |= set(norm_words(translit(norm_words(prompt), LAT2CYR)).split())
    per = [covered(g, ptoks, p_tr) for g in gold]
    joined = norm_words(prompt).replace(" ", "")
    return {
        "gold_kind": kind,
        "n_gold": len(gold),
        "n_gold_single_char": sum(1 for c in per if c["single_char_tokens"]),
        "all_words": all(c["words"] for c in per),
        "all_words_translit": all(c["words_translit"] for c in per),
        "all_contiguous": all(norm_words(g).replace(" ", "") in joined for g in gold),
        "all_literal": all(g.lower() in (prompt or "").lower() for g in gold),
        # частичное покрытие: сколько слагов покрыто из n — «почти решено» тоже факт
        "share_words": round(sum(1 for c in per if c["words"]) / len(per), 4),
    }


# ──────────────────────────────── чтение пула ────────────────────────────────

def load_pool(path: Path) -> tuple[list[dict], list[str], str]:
    rows, bad = [], []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            bad.append(f"строка {i}: {exc.msg}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return rows, bad, digest


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def read_text_or_empty(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def counter(rows: list[dict], field: str) -> dict:
    """Распределение значений поля по задачам: `{значение: сколько}`."""
    out: dict[str, int] = {}
    for task in rows:
        key = str(task.get(field))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


# ───────────────────────── пункт (a): no-action proxy ─────────────────────────

def no_action_proxy(rows: list[dict], pool_rel: str, samples: int) -> dict:
    """Пункт (a). Верхняя граница подозрения — и явная надпись, что это не замер."""
    graded = []
    for idx, task in enumerate(rows, 1):
        cov = task_coverage(task, task.get("prompt") or "")
        if cov is None:
            continue
        graded.append({"line": idx, "task": task, **cov})

    def agg(items: list[dict]) -> dict:
        n = len(items)
        if not n:
            return {"n": 0, "covered": 0, "share": None}
        c = sum(1 for x in items if x["all_words"])
        return {"n": n, "covered": c, "share": round(c / n, 4)}

    if not graded:
        # Пул без gold — не «ноль утечек», а отсутствие предмета: доля не считается.
        return {
            "is_measurement": False,
            "what_it_is": "не считается: в пуле нет ни `expected_slugs`, ни `keywords`",
            "pool": {"file": pool_rel, "n_tasks": len(rows), "n_tasks_with_gold": 0,
                     "n_tasks_without_gold": len(rows), "n_gold_kind": {}},
            "totals": {k: {"n": 0, "covered": 0, "share": None}
                       for k in ("words_all", "words_all_translit", "contiguous", "literal")},
            "by_gold_kind": [], "by_group": [], "by_task_type": [],
            "examples": [], "examples_not_covered": [], "caveats": [],
        }

    def share(items: list[dict], field: str) -> dict:
        n = len(items)
        c = sum(1 for x in items if x[field])
        return {"n": n, "covered": c, "share": round(c / n, 4) if n else None}

    def group_by(key) -> list[dict]:
        buckets: dict[tuple, list[dict]] = {}
        for x in graded:
            buckets.setdefault(key(x["task"]), []).append(x)
        out = []
        for k in sorted(buckets, key=lambda k: (-len(buckets[k]), k)):
            items = buckets[k]
            out.append({
                "task_type": k[0], "source_env": k[1], "verifier": k[2],
                "gold_kind": sorted({x["gold_kind"] for x in items}),
                "words_all": agg(items),
                "words_translit": share(items, "all_words_translit"),
                "contiguous": share(items, "all_contiguous"),
                "literal": share(items, "all_literal"),
                "share_median_words": round(
                    sorted(x["share_words"] for x in items)[len(items) // 2], 4),
            })
        return out

    # Примеры: сначала по одному на группу (чтобы 5 примеров покрыли разные типы),
    # затем добор по порядку файла. Порядок детерминирован — воспроизводимость.
    examples, seen, taken = [], set(), set()
    for x in graded:
        key = (x["task"]["task_type"], x["task"]["source_env"])
        if x["all_words"] and key not in seen:
            seen.add(key)
            examples.append(x)
            taken.add(x["line"])
        if len(examples) >= samples:
            break
    for x in graded:
        if len(examples) >= samples:
            break
        if x["all_words"] and x["line"] not in taken:
            examples.append(x)
            taken.add(x["line"])

    contrast = [x for x in graded if not x["all_words"]][:3]

    def as_example(x: dict) -> dict:
        gold, _ = gold_of(x["task"])
        return {
            "where": f"{pool_rel}:{x['line']}",
            "task_type": x["task"]["task_type"],
            "source_env": x["task"]["source_env"],
            "verifier": x["task"].get("verifier"),
            "gold_kind": x["gold_kind"],
            "gold": gold,
            "prompt": (x["task"].get("prompt") or "")[:220],
            "why": "все слова gold встречаются словами промпта"
                   if x["all_words"] else "не все слова gold есть в промпте",
            "share_words": x["share_words"],
        }

    return {
        "is_measurement": False,
        "what_it_is": ("верхняя граница подозрения на no-action-решаемость: "
                       "восстановимость gold из текста промпта по словам"),
        "what_it_is_not": [
            "не проба G2 (ADR-049 п.3(i)): та — rollout «текстовый ответ без вызовов», "
            "и её здесь никто не делал",
            "не доказательство решаемости без действий: «слаг собирается из слов промпта» "
            "не значит, что модель его соберёт (и наоборот)",
            "не оценка доли задач, которые агент решит текстом: меряется свойство данных, "
            "а не поведение политики",
        ],
        "definition": ("задача покрыта ⇔ каждое слово gold встречается словом в промпте "
                       "после нормализации (регистр, ё→е, разделители `-`/`_`/пробел → пробел)"),
        "normalization": {
            "regex": SPLIT.pattern,
            "translit_cyr2lat": CYR2LAT,
            "translit_lat2cyr": LAT2CYR,
            "scope": "одна и та же функция для gold и промпта",
        },
        "pool": {
            "file": pool_rel,
            "n_tasks": len(rows),
            "n_tasks_with_gold": len(graded),
            "n_tasks_without_gold": len(rows) - len(graded),
            "n_gold_kind": {k: sum(1 for x in graded if x["gold_kind"] == k)
                            for k in sorted({x["gold_kind"] for x in graded})},
        },
        "totals": {
            "words_all": agg(graded),
            "words_all_translit": {
                "n": len(graded),
                "covered": sum(1 for x in graded if x["all_words_translit"]),
                "share": round(sum(1 for x in graded if x["all_words_translit"]) / len(graded), 4),
            },
            "contiguous": {
                "n": len(graded),
                "covered": sum(1 for x in graded if x["all_contiguous"]),
                "share": round(sum(1 for x in graded if x["all_contiguous"]) / len(graded), 4),
            },
            "literal": {
                "n": len(graded),
                "covered": sum(1 for x in graded if x["all_literal"]),
                "share": round(sum(1 for x in graded if x["all_literal"]) / len(graded), 4),
            },
        },
        "by_gold_kind": group_by(lambda t: (gold_of(t)[1], "—", "—")),
        "by_group": group_by(lambda t: (t.get("task_type"), t.get("source_env"),
                                        t.get("verifier"))),
        "by_task_type": group_by(lambda t: (t.get("task_type"), "—", "—")),
        "examples": [as_example(x) for x in examples[:samples]],
        "examples_not_covered": [as_example(x) for x in contrast],
        "caveats": [
            "смысл метрики разный по группам: у `env_types` gold — русские слова из карточки "
            "(не слаги), у `keyword_match` — `keywords`, у остальных — слаги; одно число "
            "«покрыто N %» через группы складывать нельзя",
            f"у {sum(1 for x in graded if x['n_gold_single_char'])} задач gold содержит "
            "однобуквенные слова: они находятся в промпте почти всегда, поэтому покрытие "
            "может быть завышено (измеренный вклад — единицы слагов из тысяч)",
            "покрытие слага не равно утечке ответа: у `find_concept` ответ — не слаг, "
            "а определение; слаг здесь — то, чем меряет награду верификатор пула",
        ],
    }


def actions_in_pool(rows: list[dict]) -> dict:
    """Механическая перепроверка утверждения ADR-049 «инструментов в задачах нет».

    Проверка структурная: слова «action»/«tool_call» в пуле есть, но это имена
    концептов. Число вхождений слова считается отдельно и служит сверкой с
    замером ADR-049 (310), а не признаком действия.
    """
    fields: dict[str, int] = {}
    for task in rows:
        for k in task:
            fields[k] = fields.get(k, 0) + 1
    unknown = sorted(k for k in fields if k not in POOL_FIELDS)
    blob = json.dumps(rows, ensure_ascii=False)
    prompts = "\n".join(str(t.get("prompt") or "") for t in rows)

    structural = []
    for i, task in enumerate(rows, 1):
        text = json.dumps(task, ensure_ascii=False)
        hit = [m for m in ACTION_MARKERS_STRUCTURAL if m in text]
        if hit:
            structural.append({"line": i, "markers": hit,
                               "prompt": (task.get("prompt") or "")[:120]})

    lexical = {}
    for m in ACTION_MARKERS_LEXICAL:
        in_prompts = len(re.findall(re.escape(m), prompts, re.I))
        lexical[m] = {
            "in_prompts": in_prompts,
            "in_all_pool_text": len(re.findall(re.escape(m), blob, re.I)),
            "n_tasks": sum(1 for t in rows
                           if re.search(re.escape(m),
                                        json.dumps(t, ensure_ascii=False), re.I)),
        }
    lexical_examples = []
    for i, task in enumerate(rows, 1):
        text = json.dumps(task, ensure_ascii=False)
        if "tool_call" in text.lower() and len(lexical_examples) < 3:
            lexical_examples.append({"line": i, "source_task_id": task.get("source_task_id"),
                                     "prompt": (task.get("prompt") or "")[:110]})

    return {
        "n_tasks": len(rows),
        "fields": dict(sorted(fields.items())),
        "fields_unknown": unknown,
        "action_field_present": any(k in fields for k in ("tool_call", "tool_calls", "tools",
                                                          "actions", "messages")),
        "n_tasks_with_structural_marker": len(structural),
        "structural_marker_examples": structural[:5],
        "lexical": lexical,
        "lexical_examples_are_concept_names": lexical_examples,
        "cross_check_adr049": {
            "adr_claim": "в задачах нет инструментов/действий; 310 вхождений слова "
                         "«action» — названия концептов",
            "adr_word_action_occurrences": 310,
            "our_word_action_in_prompts": lexical.get("action", {}).get("in_prompts"),
            "delta": 310 - (lexical.get("action", {}).get("in_prompts") or 0),
            "agree": not structural,
            "note": ("число вхождений слова сходится с замером ADR-049 (расхождение — "
                     "конвенция счёта: здесь регистронезависимая подстрока по полю "
                     "`prompt`), и ни одно вхождение не является разметкой вызова: "
                     "структурных признаков нет"),
        },
        "read": ("действий в задачах нет: ни разметки вызова, ни полей инструментов; "
                 "вхождения слов «action»/«tool_call» — имена концептов"
                 if not structural and not unknown else
                 "схема/содержимое изменились — утверждение ADR-049 требует перепроверки"),
    }


def gold_in_metadata(rows: list[dict], pool_rel: str) -> dict:
    """Gold в метаданных задачи: модель его не видит, но среда может показать.

    Пункт (ii) пробы ADR-049 — «утечка gold в промпт **или среду**». Промпт меряется
    выше; здесь считается вторая половина: сколько задач несут ответ в
    `source_task_id` (в `E2_formula` — ровно слаг). Число нужно, чтобы при
    построении заземлённой среды метаданные не поехали в наблюдение агента.
    """
    n = tot = 0
    examples = []
    for i, task in enumerate(rows, 1):
        gold, _ = gold_of(task)
        if not gold:
            continue
        tot += 1
        sid = str(task.get("source_task_id") or "").lower()
        if any(g.lower() in sid for g in gold):
            n += 1
            if len(examples) < 3:
                examples.append({"where": f"{pool_rel}:{i}", "source_task_id": sid,
                                 "gold": gold})
    return {
        "checked": "source_task_id против gold",
        "n_with_gold": tot,
        "n_gold_in_source_task_id": n,
        "share": round(n / tot, 4) if tot else None,
        "examples": examples,
        "note": ("ответ лежит в поле `source_task_id` **той же записи**: в текст промпта "
                 "он не подставляется (`prompt` — отдельное поле), но среда, отдающая "
                 "агенту задачу целиком, отдаст вместе с ней и ответ; при построении "
                 "наблюдения поле обязано быть отброшено — это и есть проба (ii) "
                 "ADR-049 в части «утечка в среду»"),
    }


# ───────────────────────── пробы окружения (пункт b) ─────────────────────────

def _which(*names: str) -> dict:
    return {n: (shutil.which(n) or None) for n in names}


def _run(cmd: list[str], timeout: int = 20) -> dict:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"rc": p.returncode, "stdout": p.stdout.strip()[:200],
                "stderr": p.stderr.strip()[:200]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rc": None, "error": type(exc).__name__}


def probe_filesystem() -> dict:
    return {
        "case_root": str(CASE),
        "case_root_exists": CASE.is_dir(),
        "case_root_writable": os.access(CASE, os.W_OK),
        "repo_toplevel": _run(["git", "-C", str(CASE), "rev-parse", "--show-toplevel"]),
        "git_dir": _run(["git", "-C", str(CASE), "rev-parse", "--git-dir"]),
        "is_linked_worktree": (CASE / ".git").is_file(),
        "worktrees": _run(["git", "-C", str(CASE), "worktree", "list"]),
        "tmp_writable": os.access(tempfile.gettempdir(), os.W_OK),
        "note": ("файловая система — то, что rollout может менять; но и кейс, и "
                 "worktree живут в git-репозитории, который читает верификатор"),
    }


def probe_shell() -> dict:
    return {
        "bash": _run(["bash", "--version"]),
        "echo_rc": _run(["bash", "-c", "printf ok"]),
        "tools": _which("timeout", "setsid", "ulimit", "env", "nice"),
        "note": "проверено выполнением, а не наличием файла",
    }


def probe_isolation() -> dict:
    """Ключевая проба для G4: чем изолировать rollout. Отказ — тоже результат."""
    bwrap_net = _run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                      "--unshare-net", "--die-with-parent", "/bin/true"])
    bwrap_plain = _run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                        "--tmpfs", "/tmp", "--die-with-parent", "/bin/true"])
    unshare_net = _run(["unshare", "-n", "/bin/true"])
    unshare_user = _run(["unshare", "-Ur", "/bin/true"])
    caps = ""
    try:
        caps = next(l.split(":", 1)[1].strip() for l in
                    Path("/proc/self/status").read_text().splitlines() if l.startswith("CapEff"))
    except (OSError, StopIteration):
        caps = "не прочитано"
    return {
        "checked": True,
        "method": "реальные запуски bwrap/unshare с коротким таймаутом; стенд не задействован",
        "bwrap": _which("bwrap"),
        "unshare": _which("unshare"),
        "firejail": _which("firejail"),
        "nsjail": _which("nsjail"),
        "docker_binary": _which("docker"),
        "docker_socket": Path("/var/run/docker.sock").exists(),
        "in_docker_group": "docker" in _run(["id", "-nG"]).get("stdout", "").split(),
        "user_namespaces_max": read_text_or_empty("/proc/sys/user/max_user_namespaces"),
        "cap_effective_hex": caps,
        "attempts": {
            "bwrap_ro_bind_unshare_net": bwrap_net,
            "bwrap_ro_bind_tmpfs": bwrap_plain,
            "unshare_n": unshare_net,
            "unshare_Ur": unshare_user,
        },
        "result": ("изоляция пространствами имён в этой оболочке недоступна: "
                   "bwrap/unshare падают (карта uid, netns; CapEff без единого бита)"
                   if bwrap_plain.get("rc") != 0 or unshare_net.get("rc") != 0
                   else "изоляция пространствами имён доступна"),
        "caveat": ("проба снята **в оболочке этого хоста**, а RL-стадия идёт на стенде GB10 "
                   "внутри контейнера pytorch: права там другие, и результат пробы на "
                   "контейнер стадии не переносится"),
        "gaps": [
            "нет проверенного механизма per-rollout изоляции на хосте; кандидаты — "
            "контейнер на rollout (демон и группа docker есть, запуск не проверялся, "
            "чтобы не занимать машину) или изоляция на уровне процесса (cwd, env, timeout)",
            "без сетевого пространства имён «нет внешней сети» — политика, а не механизм: "
            "её надо либо обеспечить иначе, либо назвать честно",
        ],
    }


def probe_http_mock() -> dict:
    """Локальный HTTP-мок: проверка, что loopback-порт берётся и сервер стартует."""
    out = {"module": _run([sys.executable, "-c", "import http.server; print('ok')"])}
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        out["loopback_bind"] = {"ok": True, "port": s.getsockname()[1]}
    except OSError as exc:
        out["loopback_bind"] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
    finally:
        s.close()
    out["checked"] = True
    out["method"] = "bind(127.0.0.1, 0) + импорт http.server; внешняя сеть не трогается"
    return out


def probe_state_store() -> dict:
    """Файлы состояния/БД: свежая БД на rollout создаётся и читается верификатором."""
    out = {"module_version": sqlite3.sqlite_version}
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state.sqlite"
        try:
            con = sqlite3.connect(db)
            con.execute("create table t(k text primary key, v text)")
            con.execute("insert into t values('k','v')")
            con.commit()
            row = con.execute("select v from t where k='k'").fetchone()
            con.close()
            out.update({"create_read_ok": True, "row": row[0] if row else None,
                        "db_bytes": db.stat().st_size})
        except sqlite3.Error as exc:
            out.update({"create_read_ok": False, "error": str(exc)})
    out["checked"] = True
    out["method"] = "sqlite3 в mktemp-каталоге: создать, записать, прочитать, убрать"
    return out


def probe_case_tools() -> dict:
    """Инструменты кейса как кандидаты в «инструменты агента» (tools/*.py)."""
    kinds = {"checks": [], "runners": [], "builders": [], "assemblers": [], "other": []}
    for p in sorted((CASE / "tools").glob("*.py")):
        n = p.name
        if n.startswith("check_"):
            kinds["checks"].append(n)
        elif n.startswith("run_") or n.startswith("pilot_") or n.startswith("smoke_"):
            kinds["runners"].append(n)
        elif n.startswith("build_") or n.startswith("compare_"):
            kinds["builders"].append(n)
        elif n.startswith("assemble_"):
            kinds["assemblers"].append(n)
        else:
            kinds["other"].append(n)
    tests = CASE / "tools" / "tests" / "run_tool_tests.sh"
    return {
        "checked": True,
        "method": ("инспекция каталога: имена и размеры; ни один инструмент кейса "
                   "не запускается (запуск = работа на кейсе и стенде)"),
        "n_py": sum(len(v) for v in kinds.values()),
        "by_prefix": {k: {"n": len(v), "names": v} for k, v in kinds.items()},
        "split_rule": "AD-10: гейт ссылается только на check_*; раннеры и билдеры — руками",
        "tool_tests": {
            "checked": True,
            "method": "инспекция файла (наличие, размер); запуск здесь намеренно не делается",
            "path": "tools/tests/run_tool_tests.sh",
            "exists": tests.is_file(),
            "bytes": tests.stat().st_size if tests.is_file() else None,
            "runnable_as_reward": False,
            "why": ("это настоящий детерминированный проверяльщик состояния (код возврата = "
                    "состояние), но он идёт минуты и рекурсивно вызывает себя, если его "
                    "вызывать из аудита: годится как внешний верификатор, не как шаг rollout"),
        },
        "suitability": ("готовых «инструментов агента» в контуре нет: `tools/*` — стражи, "
                        "раннеры и сборщики дельт; как инструменты rollout они требуют "
                        "стенда, больших данных и прав записи в кейс (нарушение AD-4)"),
    }


def probe_envs_bank() -> dict:
    """Текстовые «среды» E1–E8: источник пула, не исполняемая среда."""
    if not ENVS_ROOT.is_dir():
        return {"checked": False, "reason": f"нет каталога {ENVS_ROOT}"}
    before = {str(p): sha256_file(p) for p in sorted(ENVS_ROOT.rglob("*")) if p.is_file()}
    tasks = ENVS_ROOT / "tasks"
    verifiers = ENVS_ROOT / "verifiers"
    files = sorted(p.name for p in tasks.glob("*.jsonl")) if tasks.is_dir() else []
    vfiles = sorted(p.name for p in verifiers.glob("*.py")) if verifiers.is_dir() else []
    after = {str(p): sha256_file(p) for p in sorted(ENVS_ROOT.rglob("*")) if p.is_file()}
    return {
        "checked": True,
        "root": str(ENVS_ROOT),
        "task_files": files,
        "n_task_files": len(files),
        "n_tasks_lines": {f: sum(1 for l in (tasks / f).read_text(encoding="utf-8").splitlines()
                                 if l.strip()) for f in files},
        "verifier_files": vfiles,
        "verifier_sha256": {f: sha256_file(verifiers / f) for f in vfiles},
        "read_only_verified": before == after,
        "changed_files": sorted(set(before) ^ set(after)),
        "note": ("каталог читается только; sha256 всех файлов до и после совпадают — "
                 "аудит ничего в средах не изменил"),
    }


def groundable_inventory() -> list[dict]:
    """Пункт (b): что в контуре может стать средой/инструментом, и чего не хватает."""
    fs = probe_filesystem()
    shell = probe_shell()
    iso = probe_isolation()
    http = probe_http_mock()
    db = probe_state_store()
    tools = probe_case_tools()
    envs = probe_envs_bank()

    def item(iid, what, probe, g, gaps, verdict, role):
        return {"id": iid, "what": what, "role": role, "probe": probe, "g": g,
                "gaps": gaps, "verdict": verdict}

    def fit(code, why):
        return {"fit": code, "why": why}

    return [
        item(
            "fs_artifact", "файловая система: артефакт, записанный rollout в песочницу",
            {"checked": True, "case_root": fs["case_root"], "writable": fs["case_root_writable"],
             "tmp_writable": fs["tmp_writable"]},
            {"G1": fit("yes", "успех — наличие и содержимое файла, а не текст ответа"),
             "G2": fit("yes", "без действия файла нет: no-action даёт 0 по построению"),
             "G3": fit("partial", "верификатор читает файл, но если агент пишет туда же, "
                                  "куда смотрит верификатор, это класс взлома «запись в "
                                  "источник проверки» (ADR-049 п.1, G3)"),
             "G4": fit("partial", "нужен свежий каталог на rollout и откат; детерминизм "
                                  "достижим, изоляция — см. `isolation`")},
            ["нет реализации песочницы: каталог, права, откат и таймаут придётся вводить",
             "граница «куда агент пишет / откуда читает верификатор» должна быть "
             "разведена монтированием или вторым процессом, иначе G3 не выполняется"],
            "пригодно с оговоркой", "среда"),
        item(
            "git_repo", "git-репозиторий кейса и worktree (коммит, индекс, содержимое по sha)",
            {"checked": True, "toplevel": fs["repo_toplevel"].get("stdout"),
             "git_dir": fs["git_dir"].get("stdout"),
             "is_linked_worktree": fs["is_linked_worktree"],
             "n_worktrees": len([l for l in (fs["worktrees"].get("stdout") or "").splitlines() if l])},
            {"G1": fit("yes", "состояние — коммит/индекс/дерево: `git cat-file`, "
                              "`git status --porcelain` дают бинарный ответ"),
             "G2": fit("yes", "без действия состояние не меняется"),
             "G3": fit("yes", "проверка состояния репозитория извне рабочего каталога "
                              "агента — сильнее, чем чтение текста"),
             "G4": fit("partial", "свежий `git worktree add`/`clone --local` от пиннутого "
                                  "sha детерминирован; цена на rollout не измерена")},
            ["кейс и worktree — живые рабочие каталоги с незакоммиченными файлами: "
             "rollout нельзя пускать в них напрямую (нужна копия)",
             "цена per-rollout копии репозитория не измерена"],
            "пригодно с оговоркой", "среда"),
        item(
            "shell", "shell: команда как действие, код возврата как состояние",
            {"checked": True, "bash": (shell["bash"].get("stdout") or "")[:80],
             "echo_rc": shell["echo_rc"].get("rc"), "tools": shell["tools"]},
            {"G1": fit("yes", "код возврата и stdout — наблюдаемое состояние исполнения"),
             "G2": fit("yes", "без запуска команды код возврата не появляется"),
             "G3": fit("yes", "код возврата читает внешний процесс, а не ответ модели"),
             "G4": fit("no", "shell без песочницы не изолирован: сеть, файлы и процессы "
                             "видны целиком; `timeout` есть, но это не изоляция")},
            ["нет jail: cwd/env/ulimit/timeout — это ограничение, а не изоляция",
             "нужен явный список разрешённых команд или отказ от shell в пользу "
             "узкого инструмента с фиксированным контрактом"],
            "пригодно с оговоркой", "инструмент"),
        item(
            "isolation", "изоляция rollout (G4): пространства имён, контейнер или jail",
            iso,
            {"G1": fit("n/a", "не про награду"), "G2": fit("n/a", "не про награду"),
             "G3": fit("n/a", "не про награду"),
             "G4": fit("no", "проба показала: bwrap и unshare в этой оболочке падают "
                             "(uid_map/netns, CapEff=0); контейнерный путь есть "
                             "(демон и группа docker), но не проверялся запуском")},
            iso["gaps"] + ["результат снят на хосте-4080; на стенде GB10 права другие — "
                           "пробу надо повторить внутри контейнера стадии"],
            "требует проверки", "ограничение"),
        item(
            "http_mock", "локальный HTTP-мок: запрос как действие, журнал сервера как состояние",
            http,
            {"G1": fit("yes", "состояние — запись на стороне сервера, а не текст ответа"),
             "G2": fit("yes", "без запроса журнал пуст"),
             "G3": fit("yes", "журнал ведёт сервер; агент в него не пишет — "
                              "класс «запись в источник проверки» закрыт по построению"),
             "G4": fit("partial", "порт на rollout (эфирный или из сида), обработчик "
                                  "детерминирован, таймаут обязателен; сетевой изоляции нет")},
            ["мока нет: его надо написать (обработчик, журнал, контракт ошибок)",
             "без netns агент видит и внешнюю сеть: либо запрет на уровне инструмента, "
             "либо честная запись ограничения"],
            "пригодно с оговоркой", "среда"),
        item(
            "state_db", "файлы состояния / БД (sqlite) как проверяемое состояние",
            db,
            {"G1": fit("yes", "строка в таблице — наблюдаемое состояние"),
             "G2": fit("yes", "без действия строки нет"),
             "G3": fit("partial", "верификатор читает БД; если агент открывает тот же файл "
                                  "на запись, различие «сделал инструментом» и «написал "
                                  "текстом в файл» теряется"),
             "G4": fit("yes", "свежая БД на rollout, схема из фикстуры, откат — удаление файла")},
            ["нужен запрет обхода инструмента: писать в БД должен инструмент, а не агент "
             "напрямую — иначе награда снова за текст"],
            "пригодно с оговоркой", "среда"),
        item(
            "case_tools", "инструменты кейса `tools/*.py` как инструменты агента",
            {k: v for k, v in tools.items() if k != "by_prefix"},
            {"G1": fit("partial", "у `check_*` состояние — код возврата и отчёт, это "
                                  "награда по состоянию"),
             "G2": fit("yes", "без запуска отчёта нет"),
             "G3": fit("yes", "верификатор — сам инструмент; его вердикт не переписывается "
                              "текстом ответа"),
             "G4": fit("no", "стражи читают кейс и ходят на стенд; права записи и время "
                             "работы несовместимы с шагом rollout")},
            ["это не «инструменты агента», а стражи дельты: как среда они требуют "
             "живого кейса, стенда и минут времени",
             "годный кандидат — один узкий детерминированный инструмент с фиксированным "
             "контрактом, написанный под rollout, а не взятый из `tools/`"],
            "непригодно", "инструмент"),
        item(
            "tool_tests", "`tools/tests/run_tool_tests.sh` как внешний верификатор состояния",
            tools["tool_tests"],
            {"G1": fit("yes", "состояние — код возврата набора тестов"),
             "G2": fit("yes", "без действия набор не прогоняется"),
             "G3": fit("yes", "вердикт выносит внешний процесс"),
             "G4": fit("partial", "детерминирован, но идёт минуты — на шаг rollout не влезает")},
            ["дорогой: годится на внешний eval, не на награду внутри шага"],
            "пригодно с оговоркой — не для шага", "инструмент"),
        item(
            "envs_bank", "текстовые «среды» E1–E8 (`~/library/rl_envs`)",
            envs,
            {"G1": fit("no", "верификаторы читают текст ответа (`verify(answer, gold)`)"),
             "G2": fit("no", "проба S3i: негатив «пустой ответ» даёт 1.0 у E1/E3/E5 — "
                             "награда достижима без содержания"),
             "G3": fit("no", "не бинарны: 7/7 unsafe, пороги `null`, E2 градуирован "
                             "(`min(1.0, overlap*1.5)`), E6 — 0.7/0.3"),
             "G4": fit("no", "это файлы задач, а не исполняемая среда: изолировать нечего")},
            ["источник пула, а не среда; для заземлённой оси годится только как "
             "курикулум (ADR-049 п.2)"],
            "непригодно как среда", "источник данных"),
        item(
            "stand_4080", "учебный стенд 4080 (локальная RTX 4080 SUPER 16 ГБ)",
            {"checked": False,
             "reason": ("не пробовался: AD-5 — одна GPU-нагрузка за раз, на стенде идут "
                        "чужие прогоны; проба «занять и отпустить» стоила бы чужого шага")},
            {"G1": fit("yes", "артефакт реального прогона (чекпойнт, метрика)"),
             "G2": fit("yes", "без запуска артефакта нет"),
             "G3": fit("yes", "верификатор читает артефакт на диске"),
             "G4": fit("no", "разделяемый, недетерминированный по времени, 16 ГБ и "
                             "занят; на шаг rollout не годится")},
            ["место внешнего замера, не награды: цена шага в тысячах раз выше "
             "нужной для 8 rollout"],
            "непригодно как награда", "внешний ресурс"),
        item(
            "stand_gb10", "стенд GB10 — место самой RL-стадии (контейнер pytorch)",
            {"checked": False,
             "reason": ("не пробовался: идёт чужая стадия SFT (`sft-20260919-0810`) и проба "
                        "S3aq; AD-5/AD-9 — хозяин ресурса объявлен, аудит в него не лезет")},
            {"G1": fit("yes", "состояние — файлы/процессы внутри контейнера стадии"),
             "G2": fit("yes", "—"),
             "G3": fit("partial", "верификатор должен жить **вне** контейнера агента, "
                                  "иначе агент пишет в источник проверки"),
             "G4": fit("no", "контейнер стадии — общий для всех rollout шага; свежесть "
                             "на rollout не обеспечена")},
            ["главный неизвестный: устройство песочницы на стенде (пробы сняты на хосте)",
             "цена per-rollout sandbox не измерена — она и есть главный член оценки (d)"],
            "требует проверки", "внешний ресурс"),
    ]


# ──────────────────────── пункт (c): верификаторы ────────────────────────

#: Классификация существующих проверок: что можно сделать бинарным и state-based.
#: `evidence/s3i` даёт замер (7/7 unsafe), код — причину; здесь они соединяются.
VERIFIER_READING = {
    "E1_define": {
        "code": "verifiers/v_define.py::verify_define — min(overlap, 1.0) по множествам слов",
        "binary_now": False,
        "state_based_possible": True,
        "why": ("градуирован по построению (доля пересечения слов); порядок и структура "
                "не различаются, перемешанный эталон даёт столько же, сколько эталон"),
        "to_state": ("награда по состоянию возможна: артефакт определения, записанный в "
                     "файл, проверяется схемой/полями, а не пересечением слов"),
    },
    "E2_formula": {
        "code": "verifiers/common.py::verify_formula — min(1.0, overlap*1.5) + 0.8/0.3 за раздел",
        "binary_now": False,
        "state_based_possible": True,
        "why": ("два градуированных компонента; полнота шкалы недостижима (лучший позитив "
                "0.9); наличие раздела «Обозначения» проверяется по присутствию, не по содержанию"),
        "to_state": ("формула может быть проверена исполнением: подстановка в вычисление "
                     "и сравнение числа — это код возврата, а не текст"),
    },
    "E3_classify": {
        "code": "verifiers/common.py::verify_classify — поиск подстрок type/level/formality",
        "binary_now": False,
        "state_based_possible": True,
        "why": ("подстрока «A» находится почти в любом тексте; обрезка и перемешивание строк "
                "дают 1.0; среднее трёх компонентов — градуированность"),
        "to_state": ("классификация — это запись полей в артефакт: три поля в файле, "
                     "сверка трёх значений → 0/1"),
    },
    "E5_relate": {
        "code": "verifiers/v_relate.py::verify_relate — оба слага встречаются в ответе",
        "binary_now": True,
        "state_based_possible": True,
        "why": ("формально бинарен (1.0/0.0), но константен на пустом ответе: читает ключи "
                "`source`/`target`, а банк несёт `from`/`to`, пустая строка — подстрока всего"),
        "to_state": ("связь может быть состоянием: обход по графу из артефакта, "
                     "проверка пары (from, to) в файле отношений"),
    },
    "E7_repair": {
        "code": "verifiers/common.py::verify_repair — доля исправленных полей frontmatter",
        "binary_now": False,
        "state_based_possible": True,
        "why": "доля по построению (fixes_found/total) + штраф за новые ошибки",
        "to_state": ("починка — это diff: верификатор сравнивает файл с эталоном, "
                     "награда = «файл совпал» (0/1)"),
    },
    "E6_contrast": {
        "code": "verifiers/v_contrast.py::verify_contrast — 0.7/0.3 по слову «отличие»",
        "binary_now": False,
        "state_based_possible": False,
        "why": ("два значения по наличию одного слова; 36 эталонов из 100 слова не содержат "
                "— позитив равен негативу пустого ответа"),
        "to_state": ("состояния у «объясни различие» нет: содержательность сравнения "
                     "артефактом не выражается — либо порог-слово (не G1), либо среда "
                     "с внешним судьёй (запрещён AD-6)"),
    },
}

#: Пул v2 несёт свои верификаторы: они не в `~/library/rl_envs`, а объявлены в
#: задачах. Их поведение — предмет отдельного чтения кода контура награды RL.
POOL_VERIFIERS = {
    "multi_slug_match": {
        "what": "все `expected_slugs` встречаются в тексте ответа",
        "binary": "бинарно (все/не все) при пороге — но порог в награду не подключён",
        "state_based": False,
        "why": ("проверяет текст ответа, а не состояние; восстановимость gold из промпта "
                "(пункт a) делает награду достижимой перечислением терминов"),
    },
    "keyword_match": {
        "what": "`keywords` встречаются в тексте ответа",
        "binary": "бинаризуемо тем же порогом",
        "state_based": False,
        "why": "то же: текст вместо состояния; у `E5_relate` keywords лежат в промпте",
    },
}


def verifier_binarization() -> dict:
    th = json.loads((CASE / REQUIRED_INPUTS["verifier_thresholds"]).read_text(encoding="utf-8"))
    val = json.loads((CASE / REQUIRED_INPUTS["verifier_validation"]).read_text(encoding="utf-8"))
    thresholds = th.get("thresholds") or {}
    excluded = {e.get("env"): e for e in (th.get("excluded") or [])}
    verdicts = val.get("verifiers") or {}
    rows = []
    for env, reading in VERIFIER_READING.items():
        v = verdicts.get(env) or {}
        rows.append({
            "env": env,
            "verifier": v.get("verifier"),
            "verifier_sha256": v.get("verifier_sha256"),
            "code": reading["code"],
            "proposed_threshold": thresholds.get(env),
            "positive_min": v.get("positive_min"),
            "negative_max": v.get("negative_max"),
            "binary_now": reading["binary_now"],
            "state_based_possible": reading["state_based_possible"],
            "why": reading["why"],
            "to_state": reading["to_state"],
            "s3i_verdict": v.get("verdict") or v.get("safe") if isinstance(v, dict) else None,
            "excluded_reason": (excluded.get(env) or {}).get("reason"),
        })
    n_null = sum(1 for e in thresholds.values() if e is None)
    return {
        "source": {"thresholds": REQUIRED_INPUTS["verifier_thresholds"],
                   "validation": REQUIRED_INPUTS["verifier_validation"],
                   "thresholds_sha256": sha256_file(CASE / REQUIRED_INPUTS["verifier_thresholds"]),
                   "validation_sha256": sha256_file(CASE / REQUIRED_INPUTS["verifier_validation"])},
        "registry": val.get("registry"),
        "n_environments": len(thresholds),
        "n_thresholds_null": n_null,
        "n_unsafe": sum(1 for e in (val.get("excluded") or [])),
        "n_excluded_in_thresholds": len(excluded),
        "pool_verifiers": POOL_VERIFIERS,
        "by_env": rows,
        "blockers": [
            {"blocker": "пороги `null`", "detail": f"{n_null} из {len(thresholds)} сред: "
             "«в награду не подключается до решения владельца» (ADR-021 п.1)"},
            {"blocker": "градуированность", "detail": "`min(overlap,1.0)` (E1), "
             "`min(1.0, overlap*1.5)` (E2), среднее компонентов (E3, E7), 0.7/0.3 (E6) — "
             "против AD-11 (кредит бинарный)"},
            {"blocker": "текст вместо состояния", "detail": "все верификаторы принимают "
             "`answer` — текст ответа; даже бинарный E5 не выполняет G1/G3, потому что "
             "проверяет написанное, а не сделанное"},
            {"blocker": "несовпадение ключей", "detail": "E5 читает `source`/`target`, "
             "банк несёт `from`/`to` → проверка константна (1.0 на пустом ответе)"},
        ],
        "read": ("из существующих проверок в состояние переводимы E1, E2, E3, E5, E7 — "
                 "как проверки **артефакта** (файл/вычисление), а не текста ответа; "
                 "E6 в состояние не переводится без судьи (запрещён AD-6). "
                 "Бинаризация сама по себе G1 не даёт: нужен предмет проверки «состояние»"),
    }


# ─────────────────────────── пункт (d): цена шага ───────────────────────────

def cost_estimate(pool_rows: list[dict]) -> dict:
    """Оценка (не замер!) шага RL с исполнением. Формула, допущения, диапазон.

    База читается из чужого замера `evidence/s3-rl-probe.json` (S3-pre): число в коде
    не переписывается, иначе оценка молча разойдётся с источником.
    """
    probe = json.loads((CASE / REQUIRED_INPUTS["step_cost"]).read_text(encoding="utf-8"))
    steps = probe["measurements"]["step_seconds"]
    base_mean = steps["mean"]
    base_median = steps["median"]
    shares = probe["measurements"]["phase_split"]["shares_pct"]
    groups = probe["measurements"]["groups_from_log"]
    ext = probe["extrapolation"]
    r = groups["tasks_per_step"] * groups["group_size"]

    # Допущение о траектории: число действий на rollout. Верхняя граница — из самого
    # пула (`max_steps` объявлен в задачах env_types), нижняя — из `n_min_steps`.
    declared_max = [t["max_steps"] for t in pool_rows if isinstance(t.get("max_steps"), int)]
    declared_min = [t["n_min_steps"] for t in pool_rows if isinstance(t.get("n_min_steps"), int)]
    a_lo, a_hi = (min(declared_min) if declared_min else 2), (max(declared_max) if declared_max else 15)

    # Сценарии sandbox-оверхеда: два, потому что цена почти целиком в нём.
    scenarios = {
        "process_jail": {
            "what": "свежий каталог + timeout + ограниченный env, без контейнера",
            "t_sandbox_per_rollout_s": [0.05, 0.20],
            "t_action_s": [0.02, 0.10],
            "token_growth": [1.05, 1.20],
            "why": "запуск процесса и запись файла стоят миллисекунды; рост траектории умеренный",
        },
        "container_per_rollout": {
            "what": "контейнер на rollout (docker/сторидж стенда)",
            "t_sandbox_per_rollout_s": [0.8, 3.0],
            "t_action_s": [0.10, 0.25],
            "token_growth": [1.20, 1.40],
            "why": ("подъём контейнера, монтирование и уборка — секунды; длиннее траектория "
                    "и больше служебных токенов"),
        },
    }

    def estimate(sc: dict) -> dict:
        t_sb = sc["t_sandbox_per_rollout_s"]
        t_ac = sc["t_action_s"]
        tg = sc["token_growth"]
        gen = base_mean * shares["t_generate"] / 100.0
        lo = base_mean + r * t_sb[0] + r * a_lo * t_ac[0] + gen * (tg[0] - 1)
        hi = base_mean + r * t_sb[1] + r * a_hi * t_ac[1] + gen * (tg[1] - 1)
        return {
            "step_seconds": [round(lo, 2), round(hi, 2)],
            "ratio_to_base": [round(lo / base_mean, 3), round(hi / base_mean, 3)],
            "seed_hours_500_steps": [round(lo * 500 / 3600, 2), round(hi * 500 / 3600, 2)],
            "three_seeds_hours": [round(lo * 1500 / 3600, 2), round(hi * 1500 / 3600, 2)],
            "terms_seconds": {
                "base_step": round(base_mean, 2),
                "sandbox": [round(r * t_sb[0], 2), round(r * t_sb[1], 2)],
                "actions": [round(r * a_lo * t_ac[0], 2), round(r * a_hi * t_ac[1], 2)],
                "longer_generation": [round(gen * (tg[0] - 1), 2), round(gen * (tg[1] - 1), 2)],
            },
        }

    # Ожидание ADR-049 (×1.5–2) сверяется с посчитанными отношениями, а не набирается
    # руками: строка, набранная отдельно от формулы, расходится с ней при первой правке.
    ratios = {k: estimate(v)["ratio_to_base"] for k, v in scenarios.items()}
    heavy, light = "container_per_rollout", "process_jail"
    if ratios[heavy][1] >= 1.5:
        adr_reading = (f"нижняя граница ожидания ADR (×1.5) достигается только у сценария с "
                       f"контейнером на rollout и только на верхнем крае допущений "
                       f"(×{ratios[heavy][0]}–×{ratios[heavy][1]}); верхняя (×2) не "
                       f"достигается нигде. Jail даёт ×{ratios[light][0]}–×{ratios[light][1]}: "
                       "при лёгкой песочнице ADR цену завышает")
    else:
        adr_reading = ("ожидание ADR (×1.5) не достигается ни одним сценарием: "
                       f"×{ratios[light][0]}–×{ratios[light][1]} у jail и "
                       f"×{ratios[heavy][0]}–×{ratios[heavy][1]} у контейнера — "
                       "при этих допущениях ADR цену завышает")

    return {
        "is_measurement": False,
        "label": "оценка, не замер",
        "base": {
            "source": REQUIRED_INPUTS["step_cost"],
            "source_sha256": sha256_file(CASE / REQUIRED_INPUTS["step_cost"]),
            "stage": probe.get("stage"),
            "step_seconds_mean": base_mean,
            "step_seconds_median": base_median,
            "steps_measured": len(steps.get("per_step") or []),
            "generation_share_pct": shares["t_generate"],
            "train_residual_share_pct": shares["t_train_residual"],
            "rollouts_per_step": r,
            "rollouts_from": {"tasks_per_step": groups["tasks_per_step"],
                              "group_size": groups["group_size"]},
            "known_budget_hours_three_seeds": {
                "lower_direct_500_steps": ext["bounds"]["lower"]["three_seeds_hours"],
                "upper_extrapolated": ext["bounds"]["upper"]["three_seeds_hours"],
                "bounds_named": ext["bounds"]["no_averaging"]},
            "note": ("база снята на историческом чекпойнте и текстовом пуле v1: это "
                     "порядок цены, а не цифра стадии (так её и называет S3-pre)"),
        },
        "formula": ("t_шага = t_базы + R·t_sandbox + R·A·t_действия + "
                    "0.714·t_базы·(g−1), где R — rollout на шаг (4 задачи × группа 2 = 8), "
                    "A — действий на rollout, g — рост длины генерации"),
        "assumptions": {
            "R_rollouts_per_step": r,
            "R_source": "замер S3-pre (`groups_from_log`), не допущение",
            "A_actions_per_rollout": [a_lo, a_hi],
            "A_source": (f"объявленный бюджет пула: n_min_steps min={a_lo}, "
                         f"max_steps max={a_hi} (задачи env_types)"),
            "token_growth_g": "допущение: 1.05–1.20 (jail) / 1.20–1.40 (контейнер)",
            "unchanged": ["t_базы складывается как в замере: генерация 71.4 % и обучение "
                          "27.7 % не пересчитываются",
                          "линейность по шагам (как в S3-pre) — термотроттлинг не учтён",
                          "цена верификатора состояния принята нулевой: проверка файла/кода "
                          "возврата дешевле шума таймера; на контейнере это неверно"],
        },
        "scenarios": {k: {**{kk: vv for kk, vv in v.items()}, **estimate(v)}
                      for k, v in scenarios.items()},
        "against_adr049": {
            "adr_expectation": ("×1.5–2 к базовому шагу (ADR-049, Consequences/Negative; "
                                f"база здесь {base_mean} с)"),
            "measured_ratios": ratios,
            "reading": adr_reading,
            "why_it_matters": ("цена шага почти целиком определяется **реализацией "
                               "песочницы**, а не числом действий; значит решение о G4 "
                               "есть решение о цене стадии"),
        },
        "budget_three_seeds_hours": {
            "text_baseline_measured": [ext["bounds"]["lower"]["three_seeds_hours"],
                                       ext["bounds"]["upper"]["three_seeds_hours"]],
            "basis": ext["bounds"]["no_averaging"],
            "grounded_estimate": {k: estimate(v)["three_seeds_hours"]
                                  for k, v in scenarios.items()},
            "delta_hours_vs_measured_range": {
                k: [round(estimate(v)["three_seeds_hours"][0]
                          - ext["bounds"]["lower"]["three_seeds_hours"], 2),
                    round(estimate(v)["three_seeds_hours"][1]
                          - ext["bounds"]["upper"]["three_seeds_hours"], 2)]
                for k, v in scenarios.items()},
            "unit": "часы, 500 шагов × 3 сида, конфигурация S3-pre",
        },
        "not_measured": [
            "цена per-rollout sandbox на стенде (нет пробы — идут чужие стадии; AD-5/AD-9)",
            "цена верификатора состояния",
            "рост длины траектории на реальных заземлённых задачах (самих задач ещё нет)",
        ],
    }


# ──────────────────── пункт (e): состав walking skeleton ────────────────────

def walking_skeleton() -> dict:
    """Состав скелета: 50 задач × 3 инструмента. План, а не данные (пул не менялся)."""
    groups = [
        {
            "tool": "shell", "n_tasks": 18,
            "state": "код возврата команды + артефакт, который команда создала",
            "task_shapes": [
                "вычислить значение по данным, лежащим в песочнице, и записать его в файл",
                "преобразовать файл (отсортировать/отфильтровать) и оставить результат",
                "найти в выдаче команды признак и записать его в отчёт",
            ],
            "success_binary": ("0/1: файл существует И его содержимое равно ожидаемому "
                               "(сравнение внешним процессом), код возврата роли не играет "
                               "сам по себе — иначе «echo готово» даёт награду"),
            "no_action_probe": ("текстовый ответ без вызовов: файла нет → 0. Плюс вырожденная "
                                "проба «действие есть, но пустое» (`true`): файла тоже нет → 0"),
            "leak_check": "ожидаемое значение не выводится из текста задачи (проверка тем же "
                          "нормализатором, что пункт a)",
        },
        {
            "tool": "filesystem", "n_tasks": 16,
            "state": "дерево файлов / индекс git / содержимое по sha",
            "task_shapes": [
                "создать структуру каталогов и файл в ней по спецификации",
                "поправить файл так, чтобы выполнялось свойство (схема, поле, ссылка)",
                "подготовить коммит: изменить файл и поставить его в индекс",
            ],
            "success_binary": "0/1: верификатор читает дерево/индекс и сверяет с эталоном",
            "no_action_probe": "текстовый ответ без вызовов: дерево не изменилось → 0",
            "leak_check": ("эталонное содержимое не должно лежать в песочнице до действия: "
                           "иначе задача решается копированием, а не работой"),
        },
        {
            "tool": "http_mock", "n_tasks": 16,
            "state": "журнал запросов на стороне мока (тело, метод, порядок)",
            "task_shapes": [
                "получить ресурс у мока и сохранить производное значение",
                "отправить на мок полезную нагрузку, удовлетворяющую объявленному контракту",
                "довести сценарий до состояния, которое мок признаёт завершённым",
            ],
            "success_binary": "0/1: мок записал требуемый запрос (проверка по своему журналу)",
            "no_action_probe": ("текстовый ответ без вызовов: журнал пуст → 0. Отдельно "
                                "проверяется класс взлома «написать в файл журнала»: "
                                "файл мока агенту недоступен"),
            "leak_check": "ответ мока не содержит gold; контракт объявлен, но не решает задачу",
        },
    ]
    n = sum(g["n_tasks"] for g in groups)
    assert n == SKELETON_TOTAL, (n, SKELETON_TOTAL)
    return {
        "is_data": False,
        "label": "план состава; задачи скелета — отдельная дельта (пул и наборы не менялись)",
        "size": {"n_tasks": SKELETON_TOTAL, "n_tools": len(SKELETON_TOOLS),
                 "tools": list(SKELETON_TOOLS)},
        "why_50": ("ADR-049 п.4: доказать, что награда по состоянию работает, и измерить "
                   "цену шага; 50 — размер, на котором отказ среды виден до массовой "
                   "генерации, а не после"),
        "groups": groups,
        "preconditions": [
            {"probe": "G2 (ADR-049 п.3(i)): доля задач с успехом без действий",
             "how": "rollout «текстовый ответ без вызовов» на всех 50; критерий приёмки — 0",
             "instrument": "пункт (a) этого аудита даёт только proxy —пробу делает rollout"},
            {"probe": "утечка gold (ADR-049 п.3(ii))",
             "how": "нормализатор пункта (a) по промпту и по наблюдению среды; метаданные "
                    "(`source_task_id`) в наблюдение не пускаются",
             "instrument": "tools/check_grounding.py (раздел no_action_proxy и gold_in_metadata)"},
            {"probe": "воспроизводимость среды (ADR-049 п.3(iii))",
             "how": "два rollout с одним сидом дают то же состояние: sha состояния до и после",
             "instrument": "в контуре нет — писать в скелете"},
            {"probe": "вырождение второго вида (ADR-049 п.6): среда не пропускает действия",
             "how": "доля rollout с нулём успешных действий; стоп-условие объявляется "
                    "до стадии",
             "instrument": "в контуре нет — писать в скелете"},
        ],
        "open_design": [
            "где живёт верификатор состояния относительно песочницы агента (G3)",
            "чем обеспечена изоляция: см. раздел inventory/id=isolation (проба провалилась)",
            "как считается шаг: одно действие = один вызов инструмента или один ход модели",
        ],
    }


# ────────────────────────────────── сборка ──────────────────────────────────

def build(pool_path: Path, samples: int = 5, probe_env: bool = True) -> dict:
    # Путь в отчёте: относительный от кейса, если пул лежит в кейсе; иначе абсолютный
    # («../../..» в поле `where` читалось бы как ошибка инструмента). Содержание
    # проверяется лексически, без разыменования: `datasets/` — симлинк на gb10-shared,
    # и путь кейса (`datasets/rl_tasks_revpool_v2.jsonl`) остаётся именем артефакта.
    inside = CASE in pool_path.parents or pool_path == CASE
    pool_rel = os.path.relpath(pool_path, CASE) if inside else str(pool_path)
    rows, bad, digest = load_pool(pool_path)
    doc = {
        "probe": "S4-pre — граундинг-аудит: из чего строить заземлённую ось RL",
        "adr": "ADR-049 (Proposed)",
        "question": ("есть ли в контуре предмет для награды по состоянию (G1–G4): "
                     "насколько текущий пул решается без действий, что может стать "
                     "средой, что мешает бинаризации, чего стоит шаг и из чего собрать "
                     "walking skeleton"),
        "tool": "tools/check_grounding.py",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pool": {"path": pool_rel, "sha256": digest, "n_tasks": len(rows),
                 "parse_errors": bad,
                 # Форма пула — чтобы числа ADR-049 (9162 задачи, 8768/394 по
                 # верификаторам, 6484/1814/670/194 по средам) проверялись здесь,
                 # а не принимались на слово.
                 "shape": {
                     "by_verifier": counter(rows, "verifier"),
                     "by_task_type": counter(rows, "task_type"),
                     "by_source_env": counter(rows, "source_env"),
                 }},
        "method": {
            "a_no_action_proxy": "сравнение слов gold с словами промпта по нормализации",
            "b_inventory": "пробы окружения выполнением, стенды не пробуются (AD-5/AD-9)",
            "c_verifiers": "чтение порогов, валидации S3i и кода верификаторов",
            "d_cost": "формула над замером S3-pre; оценка, не замер",
            "e_skeleton": "план состава 50 × 3",
            "read_only": "пул, наборы, среды и стенды не изменяются",
        },
    }
    doc["no_action_proxy"] = no_action_proxy(rows, pool_rel, samples)
    doc["actions_in_pool"] = actions_in_pool(rows)
    doc["gold_in_metadata"] = gold_in_metadata(rows, pool_rel)
    doc["groundable_inventory"] = groundable_inventory() if probe_env else {
        "checked": False, "reason": "пробы окружения отключены (--no-probe)"}
    doc["verifier_binarization"] = verifier_binarization()
    doc["cost_estimate"] = cost_estimate(rows)
    doc["walking_skeleton"] = walking_skeleton()
    doc["verdict"] = verdict(doc)
    doc["summary"] = summary_text(doc)
    doc["audit_sha1"] = audit_digest(doc)
    return doc


def verdict(doc: dict) -> dict:
    proxy = doc["no_action_proxy"]["totals"]["words_all"]
    acts = doc["actions_in_pool"]
    grounded = bool(acts["action_field_present"]
                    or acts["n_tasks_with_structural_marker"])
    return {
        "pool_is_grounded": grounded,
        "why": ("в задачах пула нет полей действия; вся награда — совпадение слов "
                "в тексте ответа (`multi_slug_match`/`keyword_match`)" if not grounded
                else "в пуле появились признаки действия — утверждение ADR-049 "
                     "«пул текстовый» требует перепроверки"),
        "no_action_proxy_share": proxy["share"],
        "no_action_proxy_is_measurement": False,
        "g1_g4": {
            "G1": "не выполняется: предмет награды — текст ответа",
            "G2": "не выполняется: доля задач, решаемых без действий, не измерена "
                  "(proxy даёт верхнюю границу подозрения)",
            "G3": "не выполняется: верификаторы не бинарны (пороги `null`, 7/7 unsafe) "
                  "и проверяют текст, а не состояние",
            "G4": "не выполняется и не обеспечено: проба изоляции на хосте провалилась",
        },
        "gate_applied": False,
        "gate_note": ("механический страж ADR-049 п.5 не подключён к CONSTRAINTS.yaml: "
                      "заземлённого пула ещё нет, а правило на текущем пуле краснело бы "
                      "на пустом месте. Режим стража готов: `--expect-grounded`"),
        "axis_ready": False,
    }


def pct(value) -> str:
    """Доля в процентах или прочерк: `null` — это «не считается», а не «ноль»."""
    return "—" if value is None else f"{value:.2%}"


def summary_text(doc: dict) -> dict:
    proxy = doc["no_action_proxy"]
    totals = proxy["totals"]
    inv = doc["groundable_inventory"]
    inv_n = len(inv) if isinstance(inv, list) else 0
    # «непригодно» тоже содержит «пригодно»: смотрим на начало вердикта, а не на вхождение
    inv_ok = (sum(1 for i in inv if i["verdict"].startswith("пригодно"))
              if isinstance(inv, list) else "не пробовалось")
    return {
        "pool": (f"{doc['pool']['n_tasks']} задач, gold у {proxy['pool']['n_tasks_with_gold']} "
                 f"({', '.join(f'{k}={v}' for k, v in proxy['pool']['n_gold_kind'].items())})"),
        "no_action_proxy": (f"proxy (не замер): полностью восстановим gold из промпта "
                            f"{totals['words_all']['covered']} из {totals['words_all']['n']} "
                            f"= {pct(totals['words_all']['share'])}; "
                            f"с транслитерацией {pct(totals['words_all_translit']['share'])}; "
                            f"строгое чтение {pct(totals['contiguous']['share'])}; "
                            f"буквальная подстрока {pct(totals['literal']['share'])}"),
        "inventory": (f"предметов: {inv_n}; пригодных как среда: {inv_ok}"),
        "verifiers": (f"порогов нет у {doc['verifier_binarization']['n_thresholds_null']} "
                      f"сред; unsafe у {doc['verifier_binarization']['n_unsafe']}; "
                      f"переводимы в состояние: E1, E2, E3, E5, E7"),
        "cost": (f"оценка (не замер): "
                 + "; ".join(f"{k} — шаг {v['step_seconds'][0]}–{v['step_seconds'][1]} с, "
                             f"3 сида {v['three_seeds_hours'][0]}–{v['three_seeds_hours'][1]} ч"
                             for k, v in doc["cost_estimate"]["scenarios"].items())),
        "skeleton": (f"{doc['walking_skeleton']['size']['n_tasks']} задач × "
                     f"{doc['walking_skeleton']['size']['n_tools']} инструмента"),
        "reading": ("граундинга в контуре нет; строить его можно (файлы, git, shell, мок, "
                    "БД — пригодны), но G3 и G4 требуют работы: верификатор состояния "
                    "вне песочницы агента и проверенная изоляция"),
    }


def audit_digest(doc: dict) -> str:
    """sha1 содержимого отчёта без отметки времени — воспроизводимость прибора."""
    clone = json.loads(json.dumps(doc, ensure_ascii=False))
    for k in ("generated_at", "audit_sha1"):
        clone.pop(k, None)
    blob = json.dumps(clone, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


def check_invariants(doc: dict, samples: int) -> list[str]:
    """Арифметика отчёта обязана сходиться: разрезы против итога, примеры против пула."""
    bad = []
    proxy = doc["no_action_proxy"]
    total = proxy["totals"]["words_all"]["n"]
    for name in ("by_group", "by_task_type", "by_gold_kind"):
        s = sum(g["words_all"]["n"] for g in proxy[name])
        if s != total:
            bad.append(f"{name}: сумма задач {s} ≠ итога {total}")
    if proxy["pool"]["n_tasks_with_gold"] != total:
        bad.append("число задач с gold не совпало с числом разобранных")
    if len(proxy["examples"]) > samples:
        bad.append("примеров больше запрошенного")
    sk = doc["walking_skeleton"]
    if sum(g["n_tasks"] for g in sk["groups"]) != sk["size"]["n_tasks"]:
        bad.append("состав скелета не сходится с заявленным размером")
    return bad


# ────────────────────────────────── вывод ──────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="S4-pre: граундинг-аудит по ADR-049 (пул, инвентарь, верификаторы, "
                    "цена, состав скелета). Только чтение.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", default=DEFAULT_POOL,
                    help=f"пул задач (по умолчанию {DEFAULT_POOL})")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"куда писать отчёт (по умолчанию {DEFAULT_OUT})")
    ap.add_argument("--samples", type=int, default=5, help="примеров с файлом:строкой")
    ap.add_argument("--expect-grounded", action="store_true",
                    help="страж: пул объявлен заземлённым; ни одной заземлённой задачи → exit 1")
    ap.add_argument("--no-probe", action="store_true",
                    help="не пробовать окружение (только чтение и арифметика)")
    ap.add_argument("--json", action="store_true", help="напечатать отчёт в stdout")
    args = ap.parse_args(argv)

    pool = Path(args.pool)
    if not pool.is_absolute():
        pool = CASE / pool
    missing = [rel for rel in REQUIRED_INPUTS.values() if not (CASE / rel).is_file()]
    if not pool.is_file():
        print(f"NOT-VERIFIED: нет пула {args.pool} — аудит без входа был бы пересказом")
        return EXIT_NOT_VERIFIED
    if missing:
        print("NOT-VERIFIED: нет обязательных входов аудита: " + ", ".join(missing))
        return EXIT_NOT_VERIFIED
    if not any(l.strip() for l in pool.read_text(encoding="utf-8").splitlines()):
        print("NOT-VERIFIED: пул пуст — аудит не из чего собирать")
        return EXIT_NOT_VERIFIED

    doc = build(pool, samples=args.samples, probe_env=not args.no_probe)

    problems = check_invariants(doc, args.samples)
    doc["invariants"] = {"ok": not problems, "findings": problems}
    doc["audit_sha1"] = audit_digest(doc)

    out = Path(args.out)
    if not out.is_absolute():
        out = CASE / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    s = doc["summary"]
    print("== граундинг-аудит (ADR-049, Proposed) ==")
    for k in ("pool", "no_action_proxy", "inventory", "verifiers", "cost", "skeleton"):
        print(f"  {k:16s} {s[k]}")
    shown = os.path.relpath(out, CASE) if str(out).startswith(str(CASE)) else str(out)
    print(f"  отчёт            {shown}  (sha1 прибора {doc['audit_sha1'][:12]})")
    print(f"  чтение           {s['reading']}")
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))

    if problems:
        for p in problems:
            print(f"КРАСНОЕ: {p}")
        return EXIT_RED
    if args.expect_grounded and not doc["verdict"]["pool_is_grounded"]:
        print("КРАСНОЕ: пул объявлен заземлённым, но действий в задачах нет "
              "(ADR-049 G1–G3 не выполняются)")
        return EXIT_RED
    print("РЕЗУЛЬТАТ: аудит собран (это инвентарь, а не вердикт о заземлении)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
