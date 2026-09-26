#!/usr/bin/env python3
"""S3j/S3j-2 — pass-rate пула ревизии: какие задачи решаемы на данной модели.

Измеряет распределение решаемости задач пула в том же agentic-цикле, что и
RL-стадия (ADR-007/ADR-010): системный промпт ``UNIFIED_SYSTEM_PROMPT``, до
``max_turns`` ходов, инструмент ``search_concepts``, потолок хода
``max_new_tokens``, ``temperature=1.0``, ``top_k=20``, бинарный верификатор
``verify_task`` (AD-11 — кредит бинарный).

## S3am: штатный режим декодирования агентной пробы

Прибор языковых проб закрепил штатный режим стадии — **запрет повторов 4-грамм**
(``probe_language_split.py``, S3al/ADR-040 п.6–7): замер S3ak показал, что петля
SFT-генераций есть повторяемость n-грамм и снимается этим запретом механически
(``looped_share`` 0.5417 → 0.0000). Агентная проба до S3am декодировала **своим**
циклом и о запрете не знала, то есть язык и формат мерились в одном режиме, а
агентность — в другом, и сравнивать их между собой (и с базой 45.1 %, снятой в
legacy-режиме) было нельзя. ADR-041 распространяет штатный режим на **все** пробы
стадии, включая эту:

* **штатный режим** — те же параметры контура (``temperature=1.0``, ``top_k=20``,
  сэмплирование: агентная проба мерит поведение контура RL, а не greedy-ветку) плюс
  **запрет повторов 4-грамм**, включённый **по умолчанию**
  (``STANDARD_NO_REPEAT_NGRAM``);
* **прогон без запрета** отклоняется с кодом 1, пока не назван явно
  ``--legacy-decoding``: это мост воспроизводимости прежних чисел (45.1 % = 158/350),
  а не режим для новых выводов;
* режим записывается в отчёт как ``protocol.decoding`` = {``mode``,
  ``no_repeat_ngram``, ``temperature``, ``top_k``} — тождество протокола проверяется
  по отчёту, а не по тексту плана.

**Запрет повторов — это правило fairseq/transformers**, а не «штраф» и не
постобработка текста: ``NoRepeatNGramLogitsProcessor`` запрещает токен ``T``, если
n-грамма «последние n−1 токенов + T» уже встречалась в последовательности (включая
промпт). Реализация здесь своя (``NoRepeatNGramBan``), потому что цикл пробы
декодирует сам, и подсунуть в него процессор transformers некуда; повторено
**правило**, и оно проверяется тестом на фикстурах, а не обещанием.

Две роли инструмента:

* **S3j, baseline пула** — веса базовой модели (``--model``), вопрос «есть ли в
  пуле задачи, за которые можно дать награду» (ADR-006, карточка
  ``laguna-rl-passrate-curriculum``);
* **S3j-2, сравнение пулов** — веса **обученного чекпойнта** (``--checkpoint``),
  одна и та же модель прогоняется по нескольким пулам (``--pool`` повторяемый),
  и отчёт несёт сравнительную таблицу, дельту ``v2 − v1``, диагностику нерешённых
  и вердикт о пригодности пула. Сравнение пулов на РАЗНЫХ моделях (база против
  SFT) — ошибка постановки, которую этот режим и закрывает.

Что инструмент НЕ делает:

* не обучает и не меняет пул — пул только читается, ``sha256`` прочитанного
  фиксируется до и после пробы;
* не выдаёт результат за ``pass@k``: **одна попытка на задачу** (``n_attempts=1``),
  поэтому полоса ``0.25–0.80`` считается по типам задач (агрегат), а не по задачам —
  для per-task полосы нужен ``k > 1`` (карточка ``passk-identifiability``);
* не вводит градуированный кредит: ``verify_task`` возвращает ``bool``;
* не пере-инициализирует спецтокены чекпойнта по умолчанию: строки ``<think>``/
  ``</think>``/``<tool_response>``/``</tool_response>`` **обучены** стадией SFT, и
  ``init_special_tokens_subtoken`` стёр бы их. Флаг ``--init-special-tokens``
  воспроизводит контур (``run_rl``/``run_eval`` делают это) для проверки
  чувствительности; основной замер — артефакт как он есть.

Верность контуру. Семантика инструмента, парсера хода и верификатора **не
переписана**, а импортируется из пайплайна (``laguna_pipeline_v8.py``):
``search_concepts``, ``execute_tool_call``, ``verify_task``,
``keyword_coverage``, ``compute_reward``, ``UNIFIED_SYSTEM_PROMPT``,
``Rollout``. Копия неизбежно разошлась бы с контуром; импорт — нет.

Отличие от vLLM ровно одно: сэмплирование идёт циклом на ``transformers``
(vLLM на локальном стенде нет). Логиты/фильтры воспроизведены точно
(``temperature`` → ``top_k`` → ``softmax`` → ``multinomial``), стоп-строки
пайплайна ``["</tool_call>", "<|im_end|>"]`` — это одиночные спец-токены
(151658/151645), поэтому остановка по токену точная, а не по строке. Поток
случайных чисел не совпадает с vLLM — сравнивать траектории с RL-прогоном
построчно нельзя, сопоставимы только агрегаты.

Запуск::

    python3 tools/passrate_probe.py --pool datasets/rl_tasks_revpool_v2.jsonl \\
        --run-dir runs/passrate-probe-20260916-0700 \\
        --evidence evidence/passrate-baseline-v2.json --per-type 200 --seed 42

    # S3j-2: один обученный чекпойнт на двух пулах → сравнительный отчёт
    CKPT=~/gb10-shared/models-store/experiments/laguna_qwen25-05b/sft_checkpoint_final.pt
    python3 tools/passrate_probe.py --checkpoint "$CKPT" \\
        --pool v1=datasets/rl_tasks_revpool_v1.jsonl \\
        --pool v2=datasets/rl_tasks_revpool_v2.jsonl \\
        --run-dir runs/passrate-sft-20260916-1200 --per-type 100 --seed 42 \\
        --evidence evidence/passrate-v1-v2-sft.json

    # воспроизведение прежних чисел (до S3am) — только явным флагом:
    python3 tools/passrate_probe.py --legacy-decoding ...   # отчёт помечен legacy

    # агрегаты из сырых записей, без GPU (проверка воспроизводимости сводки):
    python3 tools/passrate_probe.py --summarize runs/passrate-probe-*/tasks.jsonl \\
        --evidence /tmp/x.json

Коды возврата::

    0 — проба выполнена (или сводка собрана)
    1 — отказ: пул не тот, что ожидался (``--expect-pool-sha256``), нет задач,
        чекпойнт не читается, некорректные аргументы, прогон без запрета повторов
        n-грамм без ``--legacy-decoding`` (S3am: штатный режим — умолчание)
    2 — NOT-VERIFIED: недоступен вход (пул/веса/индекс/записи)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_REFUSE, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Стоп-строки пайплайна (laguna_pipeline_v8.py:1266). Оба — одиночные
#: спец-токены Qwen2.5, поэтому в цикле они ловятся по id, без строкового поиска.
STOP_STRINGS = ["</tool_call>", "<|im_end|>"]
STOP_TOKEN_IDS = (151658, 151645)  # </tool_call>, <|im_end|>
EOS_TOKEN_ID = 151643

#: Порог полосы ADR-006 (Ultra): pass-rate типа задачи внутри [0.25, 0.80].
BAND_LOW, BAND_HIGH = 0.25, 0.80

#: Ориентир, с которым сопоставляется пул v2 (ADR-010/ADR-017, `evidence/s3-rl-probe.json`):
#: RL-разведка на **историческом SFT-чекпойнте** (не база и не чекпойнт ревизии), пул v1,
#: 800 траекторий, 100 шагов. Числа — цитата, а не повторный замер: сопоставление
#: качественное (где пул v2 относительно), потому что отличается и модель, и пул.
ANCHOR_V1 = {
    "source": "ADR-010 / ADR-017, evidence/s3-rl-probe.json (background)",
    "model": "исторический SFT-чекпойнт laguna_qwen25-05b (30.07.2026), не база ревизии",
    "pool": "rl_tasks_revpool_v1.jsonl",
    "trajectories": 800,
    "pass_rate": 0.3887,
    "reward_mean": 0.3782,
    "zero_reward_share": 0.5063,       # 405 из 800
    "hit_timeout": 0,
    "turns_mean": 1.0,
}

#: Замер S3j (``evidence/passrate-baseline-v2.json``, hr-39) — тот же пул v2, но
#: **базовая** модель. Числа цитируются, а не пересчитываются: сравнивать пулы на
#: базе и на SFT нельзя (разные модели), поэтому здесь они лежат как «пол» базовой
#: модели, ниже которого обученный чекпойнт опуститься не должен.
ANCHOR_BASE_V2 = {
    "source": "evidence/passrate-baseline-v2.json (S3j, hr-39)",
    "model": "базовая модель без SFT (веса qwen2.5-0.5b-base)",
    "pool": "rl_tasks_revpool_v2.jsonl",
    "n_taken": 792,
    "pass_rate": 0.029,
    "dead_share": 0.971,
    "trivial_share": 0.029,
    "tool_call_share": 0.2525,
    "median_turns": 1.0,
    "reward_mean": -0.046,
    "read_as": ("нижняя граница решаемости пула v2: 2.9 % — это то, что берёт модель "
                "без обучения агентному поведению, а не свойство пула"),
}

#: Типы задач с slug-веткой верификатора (verify_task: все slug-и должны быть в ответе).
SLUG_TYPES = ("find_concept", "chain_reasoning", "multi_hop_search", "common_neighbor",
              "formula_chain", "ood_link", "ood_pair")

#: Классы причин отказа (S3j-2, п.4 задания). Порядок разбора = порядок в списке:
#: «не позвала инструмент» старше «упёрлась в лимит» — если модель ни разу не
#: вызвала инструмент, корень отказа в этом, а не в потолке ходов.
DIAG_CLASSES = (
    ("a_no_tool_call", "модель не вызвала инструмент"),
    ("d_cutoff", "обрыв/лимит ходов (потолок ходов или защита контекста)"),
    ("b_no_checkable_values", "инструмент вызван, но ответ не содержит проверяемых значений"),
    ("c_wrong_values", "проверяемые значения есть, но не те"),
)

#: Сколько символов ответа хранить в примерах диагностики (сырые ответы не коммитим).
ANSWER_CAP = 3000

#: Штатный режим декодирования стадии (ADR-041, S3am) — **запрет повторов 4-грамм,
#: включённый по умолчанию и в агентной пробе**. Число берётся тем же, что у
#: языковых проб (`probe_language_split.STANDARD_NO_REPEAT_NGRAM`), и это не
#: совпадение, а требование сопоставимости: критерий стадии (ADR-033 п.2) собирается
#: из языка, формата и агентности, и мерить их разными режимами нельзя. Менять это
#: число здесь нельзя молча — иначе агентность снова начнёт мериться не тем, чем
#: язык, а расхождение будет видно только по полю `protocol.decoding`.
STANDARD_NO_REPEAT_NGRAM = 4


# ─────────────────────────── утилиты ────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Интервал Вильсона для доли: честная граница при малых n и долях у 0/1."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def newcombe_diff_ci(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """Интервал разности двух долей (Newcombe, метод 10): ``p2 − p1``.

    Разность двух интервалов Вильсона нельзя вычитать наивно — берётся
    квадратурная комбинация полуширин. Нужен, чтобы «v2 выше v1» было
    утверждением с границей, а не сравнением двух точек.
    """
    if n1 == 0 or n2 == 0:
        return (-1.0, 1.0)
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson_ci(k1, n1)
    l2, u2 = wilson_ci(k2, n2)
    lo = (p2 - p1) - ((p2 - l2) ** 2 + (u1 - p1) ** 2) ** 0.5
    hi = (p2 - p1) + ((u2 - p2) ** 2 + (p1 - l1) ** 2) ** 0.5
    return (max(-1.0, lo), min(1.0, hi))


# ───────────────── отбор проб: имя пула и разбор спецификации ───────────────

def parse_pool_spec(spec: str) -> tuple[str, Path]:
    """``NAME=PATH`` или ``PATH`` → (имя, путь). Имя нужно для дельты и отчёта.

    Без имени имя выводится из файла (``rl_tasks_revpool_v1.jsonl`` → ``v1``):
    иначе в сравнительной таблице пулы были бы безымянными и дельта «v2 − v1»
    оказалась бы дельтой «пул A − пул B».
    """
    if "=" in spec:
        name, _, raw = spec.partition("=")
        return name.strip(), Path(raw.strip())
    path = Path(spec)
    stem = path.stem
    for prefix in ("rl_tasks_revpool_", "revpool_", "rl_tasks_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    return (stem or "pool"), path


def parse_expect_shas(spec: str | None) -> dict:
    """``NAME=SHA`` через запятую; голый SHA относится ко всем пулам."""
    out: dict[str, str] = {}
    if not spec:
        return out
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, _, sha = part.partition("=")
            out[name.strip()] = sha.strip()
        else:
            out["*"] = part
    return out


# ─────────────────────── отбор задач (стратификация) ────────────────────────

def stratify(rows: list[dict], per_type: int, seed: int) -> tuple[list[dict], dict]:
    """Стратифицированная выборка по ``task_type``.

    Отбор детерминирован: задачи типа сортируются по индексу строки в пуле
    (порядок файла — часть артефакта), затем ``random.Random(seed).sample``.
    Возвращает (выборка, отчёт по стратам), где отчёт несёт число задач каждого
    типа в пуле и сколько взято — иначе «200 на тип» неотличимо от «сколько было».
    """
    by_type: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        by_type.setdefault(r.get("task_type", "?"), []).append(i)
    rng = random.Random(seed)
    picked: list[int] = []
    report: dict[str, dict] = {}
    for t in sorted(by_type):
        idxs = by_type[t]                       # уже в порядке строк пула
        take = min(per_type, len(idxs))
        chosen = sorted(rng.sample(idxs, take))
        picked.extend(chosen)
        report[t] = {"in_pool": len(idxs), "taken": take, "seed": seed}
    picked.sort()
    return [rows[i] for i in picked], report


# ───────────── статический аудит пула: «мертва ли задача по построению» ──────

def gold_resolvable(task: dict, def_index: dict[str, str], sig_words, pipe,
                    stop_cache: dict | None = None) -> tuple[bool, str]:
    """Может ли верификатор вообще дать 1 на этой задаче (без учёта модели).

    Отличает «мёртвые, потому что модель слабая» от «мёртвых по конструкции»:
    задача, у которой проверяемое условие недостижимо ни при каком ответе, — дефект
    данных, а не трудность, и в полосу решаемости она не входит.

    Ветки повторяют ``verify_task`` (пороги и стоп-слова — из пайплайна, не свои):
    slug-ветка — нужна хотя бы одна проверяемая строка; ``explain_relation`` —
    хотя бы одно из двух определений должно давать значимые слова (порог берётся
    ``min(3, len)``, поэтому определение из одного слова тоже достижимо);
    ``compare_concepts`` — нужен хотя бы один значимый keyword после стоп-фильтра.
    """
    tt = task.get("task_type", "")
    if tt in SLUG_TYPES:
        slugs = task.get("expected_slugs", task.get("expected_slug", []))
        if isinstance(slugs, str):
            slugs = [slugs]
        if not slugs or any(not s for s in slugs):
            return False, "нет expected_slugs"
        return True, ""
    if tt == "explain_relation":
        kws = [k for k in task.get("keywords", [])
               if k.lower() not in ("related", "связаны", "связан")]
        if not kws:
            return False, "нет keywords"
        t1 = set(sig_words(def_index.get(kws[0], "")))
        t2 = set(sig_words(def_index.get(kws[1], ""))) if len(kws) > 1 else set()
        if not t1 and not t2:
            return False, "ни одно определение не даёт значимых слов — порог недостижим"
        return True, ""
    if tt == "compare_concepts":
        kws = []
        for k in task.get("expected_answer_keywords", []):
            w = k.lower().strip(".,;:()\"—")
            if len(w) >= 4 and w not in pipe._COMPARE_STOP:
                kws.append(w)
        if not kws:
            return False, "нет значимых expected_answer_keywords после стоп-фильтра"
        return True, ""
    return False, f"тип '{tt}' не обслуживается verify_task"


def audit_pool(rows: list[dict], def_index: dict, sig_words, pipe) -> dict:
    """Аудит всего пула (не выборки): у скольких задач верификатор вообще достижим.

    Считается ровно по той же функции, что и per-task признак в прогоне
    (``gold_resolvable``), — иначе «мёртвых по построению» в аудите и в сводке
    было бы два разных числа про одно и то же.
    """
    by_type: dict[str, dict] = {}
    dead = 0
    for r in rows:
        tt = r.get("task_type", "?")
        rec = by_type.setdefault(tt, {"n": 0, "dead_by_construction": 0, "reasons": {}})
        rec["n"] += 1
        ok, reason = gold_resolvable(r, def_index, sig_words, pipe)
        if not ok:
            dead += 1
            rec["dead_by_construction"] += 1
            rec["reasons"][reason] = rec["reasons"].get(reason, 0) + 1
    return {
        "n": len(rows),
        "dead_by_construction": dead,
        "dead_by_construction_share": round(dead / len(rows), 4) if rows else None,
        "by_task_type": by_type,
        "definition": ("задача считается мёртвой по построению, если верификатор не может "
                       "выдать 1 ни на каком ответе: нет проверяемой строки (slug-ветка), "
                       "оба определения пусты (explain_relation) или после стоп-фильтра "
                       "не осталось значимых keyword-ов (compare_concepts); пороги — из "
                       "verify_task, а не свои"),
    }


# ─────────────────────── чекпойнт стадии SFT (S3j-2) ────────────────────────

def _torch_load_ckpt(path: Path, torch):
    """``torch.load`` с mmap, если версия умеет: 2.9 ГБ чекпойнта в RAM не нужны."""
    try:
        return torch.load(path, map_location="cpu", mmap=True, weights_only=False)
    except TypeError:
        return torch.load(path, map_location="cpu", weights_only=False)


def load_checkpoint(model, ckpt_path: Path, torch, pipe, tokenizer, *,
                    init_special_tokens: bool) -> dict:
    """Кладёт ``state_dict`` чекпойнта в модель так же, как это делает пайплайн.

    Порядок повторяет контур (``run_rl``/``run_eval``): resize под
    ``SPECIAL_TOKENS`` → ``_load_ckpt_with_resize`` (переносит embeddings, если
    словарь чекпойнта короче) → опционально ``init_special_tokens_subtoken``.

    По умолчанию спецтокены НЕ переинициализируются: строки ``<think>``,
    ``</think>``, ``<tool_response>``, ``</tool_response>`` обучены стадией SFT, и
    переинициализация стёрла бы их — замер перестал бы быть замером артефакта.
    """
    blob = _torch_load_ckpt(ckpt_path, torch)
    sd = blob.get("model") if isinstance(blob, dict) else None
    if not isinstance(sd, dict) or not sd:
        raise ValueError(f"в чекпойнте нет state_dict модели под ключом 'model': {ckpt_path}")
    vocab_ckpt = None
    for key in ("model.embed_tokens.weight", "lm_head.weight"):
        if key in sd:
            vocab_ckpt = int(sd[key].shape[0])
            break
    n_tensors = len(sd)
    resized = bool(pipe._load_ckpt_with_resize(model, sd, "PROBE: "))
    if init_special_tokens:
        pipe.init_special_tokens_subtoken(model, tokenizer)
    return {
        "path": str(ckpt_path),
        "sha256": sha256_file(ckpt_path),
        "bytes": ckpt_path.stat().st_size,
        "n_tensors": n_tensors,
        "vocab_in_checkpoint": vocab_ckpt,
        "vocab_model": int(model.get_input_embeddings().weight.shape[0]),
        "vocab_tokenizer": len(tokenizer),
        "resized_on_load": resized,
        "init_special_tokens_subtoken": bool(init_special_tokens),
        "has_optimizer_state": bool(isinstance(blob, dict) and blob.get("optimizer") is not None),
        "loader": ("transformers AutoModelForCausalLM + laguna_pipeline_v8."
                   "_load_ckpt_with_resize (семантика контура, не копия)"),
    }


def verify_checkpoint_load(model, ckpt_path: Path, torch) -> dict:
    """Доказательство, что в модели лежит ИМЕННО этот чекпойнт, а не похожий.

    Сверяются все тензоры чекпойнта, форма которых совпала с моделью: число
    сверенных, число расхождений и максимальную абсолютную разницу. Число без
    этой сверки отвечает на вопрос «чекпойнт указан в отчёте», а не «чекпойнт
    загружен».
    """
    blob = _torch_load_ckpt(ckpt_path, torch)
    sd = blob.get("model") if isinstance(blob, dict) else {}
    cur = model.state_dict()
    n_cmp = n_mismatch = n_prefix = 0
    max_abs = 0.0
    per_key: list[str] = []
    with torch.no_grad():
        for key, val in sd.items():
            if key not in cur:
                per_key.append(f"{key}: нет в модели — сверка пропущена")
                continue
            a = cur[key].detach().float().cpu()
            b = val.detach().float().cpu()
            if tuple(a.shape) == tuple(b.shape):
                diff = float((a - b).abs().max())
                n_cmp += 1
            elif a.dim() == 2 and a.shape[1] == b.shape[1] and a.shape[0] > b.shape[0]:
                # resize-ветка контура: строки чекпойнта переносятся в начало словаря,
                # хвост остаётся инициализированным заново — сверяем перенесённый префикс
                diff = float((a[:b.shape[0]] - b).abs().max())
                n_cmp += 1
                n_prefix += 1
            else:
                per_key.append(f"{key}: форма {tuple(b.shape)} vs {tuple(a.shape)} — сверка пропущена")
                continue
            max_abs = max(max_abs, diff)
            if diff != 0.0:
                n_mismatch += 1
                per_key.append(f"{key}: max|Δ|={diff:.6g}")
    return {
        "tensors_compared": n_cmp,
        "tensors_in_checkpoint": len(sd),
        "compared_as_resized_prefix": n_prefix,
        "mismatches": n_mismatch,
        "max_abs_diff": max_abs,
        "mismatch_keys": per_key[-8:],
        "note": ("max_abs_diff = 0 означает побитовое совпадение загруженных весов "
                 "с файлом чекпойнта (bf16 → bf16 копия); для словаря, выросшего при "
                 "resize, сверяется перенесённый префикс строк"),
    }


def weights_fingerprint(model, torch) -> str:
    """Отпечаток весов: доказывает, что между пулами модель не изменилась."""
    h = hashlib.sha256()
    sd = model.state_dict()
    for key in sorted(sd):
        if key.endswith("weight") or key.endswith("bias"):
            t = sd[key].detach().float().cpu()
            h.update(key.encode())
            h.update(t.reshape(-1)[::max(1, t.numel() // 4096)].numpy().tobytes())
    return h.hexdigest()


# ─────────────────── режим декодирования агентной пробы (S3am) ──────────────

def resolve_no_repeat_ngram(requested: int | None,
                            legacy: bool) -> tuple[int | None, str | None]:
    """Разрешить режим запрета повторов n-грамм: штатный — по умолчанию (ADR-041).

    Семантика та же, что у разрешителя языковых проб
    (`probe_language_split.resolve_no_repeat_ngram`): один и тот же вопрос о режиме
    стадии не должен решаться в двух приборах по-разному — иначе «единый протокол»
    станет обещанием.

    * флаг не задан → штатный режим (``STANDARD_NO_REPEAT_NGRAM``); при
      ``--legacy-decoding`` — прежний протокол без запрета (числа до S3am, в т.ч.
      agentic-база 158/350 = 45.1 %);
    * задано положительное N → запрет N-грамм (N=4 — штатный);
    * задан 0 (или меньше) → режим **без** запрета: он отклоняется, пока не назван
      ``--legacy-decoding``. Отказ, а не тихое согласие: прогон без запрета — ровно
      тот режим, в котором петля принималась за деградацию, а метрика агентности
      становилась несопоставимой с языковой.

    Возврат — ``(эффективное N или None, текст отказа или None)``. Отказ
    возвращается строкой: прибор обязан напечатать причину и выйти с кодом 1, а не
    упасть трейсбеком.
    """
    if requested is None:
        return (None, None) if legacy else (STANDARD_NO_REPEAT_NGRAM, None)
    if int(requested) <= 0:
        if legacy:
            return None, None
        return None, (
            "отказ: прогон без запрета повторов n-грамм (--no-repeat-ngram "
            f"{int(requested)}) запрещён — в этом режиме число самостоятельных "
            "вызовов и pass-rate измеряются в другом режиме, чем язык и формат "
            "(ADR-041): сопоставлять их нельзя, а именно на такой смене режима "
            "числа стадии уже расходились. Штатный режим — запрет 4-грамм "
            "(включён по умолчанию); для воспроизведения прежних чисел "
            "(agentic-база 45.1 % = 158/350) добавьте --legacy-decoding — отчёт "
            "будет помечен как непригодный для выводов о стадии")
    return int(requested), None


def decoding_label(temperature: float, top_k: int,
                   no_repeat_ngram: int | None) -> str:
    """Имя режима декодирования — из самих параметров, а не из флагов.

    Прибор агентной пробы сэмплирует всегда (так устроен контур RL), поэтому имя
    начинается с ``sample``; ``greedy``-ветку стадии меряет прибор языковых проб.
    Имя собирается из чисел: подделать его флагом нельзя, а два прогона с разными
    параметрами обязаны различаться именами в таблице «режим × метрика».
    """
    parts = ["sample", f"T{temperature:g}", f"topk{int(top_k)}"]
    parts.append(f"nogram{int(no_repeat_ngram)}" if no_repeat_ngram else "nogram0")
    return "_".join(parts)


def decoding_protocol_block(no_repeat_ngram: int | None, requested: int | None,
                            legacy: bool, temperature: float, top_k: int,
                            all_banned_fallbacks: int = 0) -> dict:
    """Режим декодирования агентной пробы как часть протокола — в отчёт (ADR-041 п.4).

    Блок отвечает на те же три вопроса, что и у языковых проб: какой режим
    **штатный**, включён ли он **в этом** отчёте и **можно ли** по этому отчёту
    делать выводы о стадии. Сверх того он несёт `temperature`/`top_k`: у агентной
    пробы штатный режим — это запрет повторов **при параметрах контура**, а не
    greedy, и без этих двух чисел «штатный» читалось бы как «greedy».
    """
    standard = bool(no_repeat_ngram)
    if requested is None:
        source = ("умолчание прибора (STANDARD_NO_REPEAT_NGRAM)" if standard
                  else "прежний протокол: умолчание снято --legacy-decoding")
    else:
        source = "явно указан флагом --no-repeat-ngram"
    return {
        "mode": decoding_label(temperature, top_k, no_repeat_ngram),
        "no_repeat_ngram": no_repeat_ngram,
        "temperature": temperature,
        "top_k": top_k,
        "default_mode": (f"sample (temperature {temperature:g}, top_k {top_k}) + "
                         f"запрет повторов {STANDARD_NO_REPEAT_NGRAM}-грамм"),
        "standard_no_repeat_ngram": STANDARD_NO_REPEAT_NGRAM,
        "no_repeat_ngram_requested": requested,
        "standard": standard,
        "legacy_decoding": bool(legacy),
        "source": source,
        #: Выводы по критерию стадии (ADR-033 п.2в: доля самостоятельных вызовов и
        #: pass-rate) разрешены ровно тогда, когда запрет включён: иначе числа
        #: сняты в режиме с известным дефектом и несопоставимы с языковыми.
        "allowed_for_conclusions": standard,
        "all_banned_fallbacks": int(all_banned_fallbacks),
        "all_banned_fallbacks_note": (
            "сколько раз запрет снимался сам, потому что запрещёнными оказывались "
            "все токены словаря (в норме 0; ненулевое значение — запрет не подействовал "
            "и это названо, а не замолчано)"),
        "rules": {
            "same_mode_as_language": "язык, формат и агентность меряются одним "
                                     "режимом: запрет 4-грамм включён во всех пробах "
                                     "стадии (ADR-041 п.1)",
            "ban_is_fairseq_rule": "запрет — правило fairseq/transformers "
                                   "(NoRepeatNGramLogitsProcessor): запрещён токен T, "
                                   "если n-грамма «последние n−1 токенов + T» уже "
                                   "встречалась (включая промпт)",
            "legacy_is_for_identity": "--legacy-decoding существует для "
                                      "воспроизведения прежних чисел (45.1 % = 158/350), "
                                      "а не для новых выводов",
        },
    }


class NoRepeatNGramBan:
    """Запрет повторов n-грамм для цикла пробы — то же правило, что у transformers.

    Зачем своя реализация. Проба декодирует **своим** циклом на KV-кэше (логиты
    только последней позиции, `lm_head` по одной позиции), и подсунуть туда
    `NoRepeatNGramLogitsProcessor` некуда. Поэтому повторено **правило**
    (`NoRepeatNGramLogitsProcessor` + `_calc_banned_ngram_tokens` из transformers),
    и оно проверяется тестом на фикстурах, а не обещанием в комментарии.

    Правило (fairseq, через transformers): запрещён токен ``T``, если n-грамма
    «последние ``n−1`` токенов + ``T``» уже встречалась в последовательности —
    **включая промпт и вставки инструмента**, потому что процессор transformers
    видит весь ``input_ids``, а не только сгенерированное. Отсюда устройство:
    словарь ``префикс (n−1 токенов) → множество токенов, что за ним уже шли``,
    построенный по последовательности и **достраиваемый по одному токену** на шаг.
    Пересчёт словаря целиком на каждом шаге (как это делает HF в stateless-вызове)
    был бы O(L²) на 4096 шагов; приращение даёт тот же результат за O(1).

    Состояние — чистая питоновская структура: она строится и в тестах без torch, а
    логиты правит отдельная функция (`apply_ngram_ban`).
    """

    def __init__(self, n: int, sequences: list[list[int]]):
        if int(n) <= 0:
            raise ValueError(f"n-грамм должно быть ≥ 1, получено {n!r}")
        self.n = int(n)
        self.k = self.n - 1                       # длина ключа-префикса
        self.seq: list[list[int]] = []
        self.succ: list[dict[tuple, set[int]]] = []
        #: Счётчик «запрет не подействовал»: запрещёнными оказались все токены
        #: словаря. В норме 0; ненулевое значение попадает в отчёт, а не молчит.
        self.all_banned_fallbacks = 0
        for ids in sequences:
            seq = [int(i) for i in ids]
            self.seq.append(seq)
            self.succ.append(self._build(seq))

    def _build(self, seq: list[int]) -> dict[tuple, set[int]]:
        d: dict[tuple, set[int]] = {}
        for i in range(max(0, len(seq) - self.n + 1)):
            d.setdefault(tuple(seq[i:i + self.k]), set()).add(seq[i + self.k])
        return d

    def _key(self, b: int) -> tuple:
        seq = self.seq[b]
        # ``seq[-0:]`` — это вся последовательность, а не пустой префикс: при n=1
        # ключ обязан быть пустым кортежем, поэтому срез берётся явной длиной.
        return tuple(seq[len(seq) - self.k:]) if self.k else ()

    def banned(self, b: int) -> set[int]:
        """Токены, запрещённые на следующем шаге для последовательности ``b``."""
        return self.succ[b].get(self._key(b), set())

    def extend(self, b: int, token: int) -> None:
        """Учесть выбранный токен: завершённая n-грамма становится запретом."""
        seq = self.seq[b]
        if len(seq) >= self.k:
            self.succ[b].setdefault(self._key(b), set()).add(int(token))
        seq.append(int(token))


def apply_ngram_ban(logits, ban: "NoRepeatNGramBan | None", torch):
    """Проставить ``-inf`` запрещённым токенам — **до** temperature и top_k.

    Порядок не косметика: в ``LogitsProcessorList`` transformers
    `NoRepeatNGramLogitsProcessor` стоит **раньше** `TemperatureLogitsWarper` и
    `TopKLogitsWarper`, поэтому запрет ставится на сырые логиты, а температура и
    top-k применяются уже к ним. Иначе запрещённый токен мог бы попасть в top-k и
    быть выбран — то есть запрет не запрещал бы.
    """
    if ban is None:
        return logits
    for b in range(logits.shape[0]):
        forbid = ban.banned(b)
        if not forbid:
            continue
        row = logits[b]
        if len(forbid) >= row.shape[0]:
            # Запрещено всё: softmax по строке из одних -inf дал бы NaN. Такой
            # случай невозможен по построению (запретов не больше, чем токенов в
            # последовательности, а словарь на порядки больше), но если он случится,
            # запрет снимается **и это считается**, а не молчит.
            ban.all_banned_fallbacks += 1
            continue
        idx = torch.tensor(sorted(forbid), dtype=torch.long, device=logits.device)
        row.index_fill_(0, idx, float("-inf"))
    return logits


# ───────────────────────── батчевая генерация хода ──────────────────────────

def make_sampler(torch):
    def sample(logits, top_k: int, temperature: float):
        """``temperature`` → ``top_k`` → ``softmax`` → ``multinomial``.

        Порядок и математика — как у ``LogitsProcessorList`` transformers
        (TemperatureLogitsWarper, затем TopKLogitsWarper), поэтому при тех же
        параметрах распределение то же; поток RNG — свой.

        Штатный запрет повторов n-грамм (S3am) ставится **до** вызова сэмплера
        (`apply_ngram_ban`) — в том же порядке, что у transformers, где
        `NoRepeatNGramLogitsProcessor` идёт раньше температурного варпера.
        """
        logits = logits.float()
        if temperature != 1.0:
            logits = logits / temperature
        if top_k and top_k > 0:
            k = min(top_k, logits.size(-1))
            values, indices = torch.topk(logits, k, dim=-1)
            probs = torch.softmax(values, dim=-1)
            pick = torch.multinomial(probs, num_samples=1)
            return indices.gather(-1, pick).squeeze(-1)
        probs = torch.softmax(logits, dim=-1)
        return torch.multinomial(probs, num_samples=1).squeeze(-1)
    return sample


def _generate_chunk(model, tok, chunk: list[str], *, max_new_tokens: int, temperature: float,
                    top_k: int, torch, sample, no_repeat_ngram: int | None = None) -> list[str]:
    """Один ход для пачки контекстов (левое дополнение). Тексты — без стоп-строки.

    ``no_repeat_ngram`` (S3am) включает штатный запрет повторов n-грамм. Состояние
    запрета строится **из `input_ids` этого же вызова**, а не приходит снаружи:
    процессор transformers видит ровно ``input_ids`` (промпт + вставки инструмента +
    сгенерированное внутри хода), и состояние, собранное в одном месте, разошлось бы
    с ним на первом же ходе с ответом инструмента. При ``None`` запрет не тронут ни
    одним вызовом — прежний протокол воспроизводится побайтово.
    """
    from transformers.cache_utils import DynamicCache

    enc = tok(chunk, return_tensors="pt", padding=True)
    input_ids = enc["input_ids"].cuda()
    attn = enc["attention_mask"].cuda()
    bs, plen = input_ids.shape
    ban = (NoRepeatNGramBan(no_repeat_ngram, input_ids.tolist())
           if no_repeat_ngram else None)
    total = plen + max_new_tokens
    # Маска и позиции предвыделены: ``torch.cat`` на каждый токен — это O(n²) работы
    # и лишние аллокации на самом горячем пути.
    full_mask = torch.zeros(bs, total, dtype=attn.dtype, device="cuda")
    full_mask[:, :plen] = attn
    full_mask[:, plen:] = 1
    pos = (full_mask.cumsum(-1) - 1).clamp_min(0)

    gen: list[list[int]] = [[] for _ in range(bs)]
    finished = torch.zeros(bs, dtype=torch.bool, device="cuda")
    cur = plen
    # Без inference_mode граф тянет активации всех ходов (включая префилл на всю
    # длину): на 16 ГБ это OOM уже при bs=2. Инференс — не обучение, автоград не нужен.
    with torch.inference_mode():
        # префилл: hidden states всей последовательности, логиты — только последней позиции.
        # ``generate`` считает lm_head по всем позициям: batch × seq × vocab × 4 байта —
        # на 152k словаре это 12 ГБ при bs=32 и seq=614, из-за чего батч упирался в 4.
        cache = DynamicCache()
        o = model.model(input_ids=input_ids, attention_mask=full_mask[:, :plen],
                        position_ids=pos[:, :plen], past_key_values=cache, use_cache=True)
        cache = o.past_key_values
        logits = model.lm_head(o.last_hidden_state[:, -1:, :])[:, -1, :]
        for _ in range(max_new_tokens):
            # Запрет повторов — на сырые логиты, до temperature/top_k (порядок
            # процессоров transformers); при ban=None строка ниже не исполняется
            # ни разу, и путь прежнего протокола остаётся нетронутым.
            apply_ngram_ban(logits, ban, torch)
            next_ids = sample(logits, top_k, temperature)
            stop_now = torch.zeros(bs, dtype=torch.bool, device="cuda")
            for b in range(bs):
                if bool(finished[b]):
                    continue
                tid = int(next_ids[b])
                gen[b].append(tid)
                if ban is not None:
                    # Токен учтён в состоянии запрета **до** следующего шага:
                    # завершённая им n-грамма становится запретом ровно один раз.
                    ban.extend(b, tid)
                if tid in STOP_TOKEN_IDS or tid == EOS_TOKEN_ID:
                    stop_now[b] = True
            finished |= stop_now
            if bool(finished.all()):
                break
            cur += 1
            o = model.model(input_ids=next_ids.unsqueeze(1), attention_mask=full_mask[:, :cur],
                            position_ids=pos[:, cur - 1:cur], past_key_values=cache, use_cache=True)
            cache = o.past_key_values
            logits = model.lm_head(o.last_hidden_state[:, -1:, :])[:, -1, :]

    out = []
    for b in range(bs):
        ids = gen[b]
        if ids and ids[-1] in STOP_TOKEN_IDS + (EOS_TOKEN_ID,):
            ids = ids[:-1]                         # стоп-строка в контекст не попадает (как vLLM)
        text = tok.decode(ids, skip_special_tokens=False)
        for s in STOP_STRINGS:                     # страховка: стоп-строка внутри текста
            if s in text:
                text = text.split(s)[0]
        out.append(text)
    return out, (0 if ban is None else ban.all_banned_fallbacks)


def generate_turns(model, tok, contexts: list[str], *, max_new_tokens: int, temperature: float,
                   top_k: int, batch_size: int, torch, sample, label: str = "",
                   no_repeat_ngram: int | None = None,
                   ban_stats: dict | None = None) -> list[str]:
    """Ходы для всех контекстов; OOM на батче — делим его пополам, а не теряем прогон.

    Цикл побайтово-эквивалентен прямолинейному (tuple-cache, ``torch.cat`` маски,
    логиты последней позиции): при одном сиде и одном батче оба выдают один и тот
    же текст — проверено сравнением на 8 контекстах по 96 токенов.

    Батч печатает строку до и после генерации: внутри хода задача не финализируется,
    и без этих строк часовой прогон молчал бы десятками минут подряд — «зелёный по
    тишине» неотличим от зависшего.

    ``no_repeat_ngram``/``ban_stats`` — штатный режим (S3am): запрет повторов
    n-грамм на каждом ходу и счётчик случаев «запрет не подействовал», который
    уходит в отчёт.
    """
    out_texts: list[str | None] = [None] * len(contexts)
    stack = [(s, min(s + batch_size, len(contexts)))
             for s in range(0, len(contexts), batch_size)][::-1]
    n_chunks = len(stack)
    done_chunks = 0
    while stack:
        start, end = stack.pop()
        if start >= end:
            continue
        chunk = contexts[start:end]
        t_batch = time.time()
        print(f"  [{label or 'pool'}] батч {done_chunks + 1}/{n_chunks} "
              f"(задачи {start + 1}–{end} из {len(contexts)}): генерация…",
              file=sys.stderr, flush=True)
        try:
            texts, all_banned = _generate_chunk(
                model, tok, chunk, max_new_tokens=max_new_tokens, temperature=temperature,
                top_k=top_k, torch=torch, sample=sample, no_repeat_ngram=no_repeat_ngram)
            if ban_stats is not None:
                ban_stats["all_banned_fallbacks"] = (
                    ban_stats.get("all_banned_fallbacks", 0) + all_banned)
        except torch.cuda.OutOfMemoryError:
            # Соседний прогон на той же карте (local GPU делят параллельные пробы) делает
            # свободную память переменной: терять часовой прогон из-за этого нельзя.
            torch.cuda.empty_cache()
            if end - start == 1:
                raise
            mid = (start + end) // 2
            print(f"  OOM на батче {end - start} — делю пополам (батч пробы: {batch_size})",
                  file=sys.stderr, flush=True)
            stack.append((mid, end))
            stack.append((start, mid))
            continue
        out_texts[start:end] = texts
        done_chunks += 1
        print(f"  [{label or 'pool'}] батч {done_chunks}/{n_chunks} готов "
              f"за {time.time() - t_batch:.1f}s", file=sys.stderr, flush=True)
    return [t if t is not None else "" for t in out_texts]


# ─────────────────────────── проба (agentic-цикл) ───────────────────────────

def requirement_detail(task: dict, answer: str, pipe, def_index, sig_words) -> dict:
    """Что требует верификатор и что из этого есть в ответе — для рассказа о примере.

    Это **пояснение к примеру**, а не вердикт: ``pass`` всегда берётся из
    ``pipe.verify_task``. Здесь считается только «сколько проверяемого материала
    в ответе», чтобы отличить «нечего проверять» от «проверяемое есть, но не то».
    """
    resp_lower = re.sub(r"<tool_response>.*?</tool_response>", "", answer, flags=re.S).lower()
    tt = task.get("task_type", "")
    if tt in SLUG_TYPES:
        slugs = task.get("expected_slugs", task.get("expected_slug", []))
        if isinstance(slugs, str):
            slugs = [slugs]
        hit = [s for s in slugs if s.lower() in resp_lower]
        return {"kind": "все expected_slugs должны быть в ответе (slug-ветка verify_task)",
                "required": slugs, "required_n": len(slugs), "found": hit, "found_n": len(hit)}
    if tt == "explain_relation":
        kws = [k for k in task.get("keywords", [])
               if k.lower() not in ("related", "связаны", "связан")]
        defs = [def_index.get(k, "") for k in kws]
        t1 = set(sig_words(defs[0])) if defs else set()
        t2 = set(sig_words(defs[1])) if len(defs) > 1 else set()
        h1 = sum(1 for w in t1 if w in resp_lower)
        h2 = sum(1 for w in t2 if w in resp_lower)
        return {"kind": ("термины ОПРЕДЕЛЕНИЙ обоих концептов: порог 3 на каждый "
                         "(2, если определение короче)"),
                "concepts": kws[:2], "thresholds": [min(3, len(t1)), min(3, len(t2))],
                "term_pool": [len(t1), len(t2)], "found": [h1, h2],
                "terms_in_answer": [sorted(w for w in t1 if w in resp_lower)[:6],
                                    sorted(w for w in t2 if w in resp_lower)[:6]]}
    if tt == "compare_concepts":
        kws = []
        for k in task.get("expected_answer_keywords", []):
            w = k.lower().strip(".,;:()\"—")
            if len(w) >= 4 and w not in pipe._COMPARE_STOP:
                kws.append(w)
        hit = [w for w in kws if w in resp_lower]
        need = max(2, int(len(kws) * 0.67)) if kws else 0
        return {"kind": "перекрытие ключевых слов >= max(2, 67 % от значимых)",
                "required_n": len(kws), "threshold": need,
                "found_n": len(hit), "found": hit[:6]}
    return {"kind": f"тип '{tt}' не обслуживается verify_task — задача недостижима"}


def classify_failure(rec: dict, coverage: float) -> str:
    """Класс причины отказа — по мете хода и покрытию (AD-11: кредит бинарный).

    Порядок разбора важен: «не позвала инструмент» старше «упёрлась в лимит»
    (если вызова не было ни разу, корень в этом), а «нечего проверять» — старше
    «проверяемое не то» (``keyword_coverage`` = 0 против > 0).
    """
    if rec.get("tool_calls", 0) == 0:
        return "a_no_tool_call"
    if rec.get("hit_timeout") or rec.get("hit_context_guard"):
        return "d_cutoff"
    if not coverage:
        return "b_no_checkable_values"
    return "c_wrong_values"


def answer_record(task: dict, rec: dict, ans: str) -> dict:
    """Строка ``answers.jsonl``: исход и разборные поля успеха.

    INSTRUMENT-SUCCESS-EXAMPLES §2.1: к прежним полям (``task_index``, ``task_type``,
    ``prompt``, ``answer``, ``answer_truncated``) **аддитивно** добавляются ``pass``,
    ``coverage``, ``turns``, ``tool_calls``, ``diag_class`` (null для успешных); порядок
    прежних полей не меняется.
    """
    return {"task_index": rec["task_index"], "task_type": rec["task_type"],
            "prompt": (task.get("prompt") or "")[:600],
            "answer": ans[:ANSWER_CAP],
            "answer_truncated": len(ans) > ANSWER_CAP,
            "pass": rec["pass"], "coverage": rec.get("coverage"),
            "turns": rec["turns"], "tool_calls": rec["tool_calls"],
            "diag_class": rec.get("diag_class")}


def run_probe(args, pipe, torch, tok, model, tasks: list[dict], def_index, sig_words,
              *, pool_name: str = "", progress_path: Path | None = None,
              ban_stats: dict | None = None) -> list[dict]:
    """Прокатывает модель в agentic-цикле и возвращает сырые записи по задачам.

    Записи финализируются **по мере готовности задачи** (а не в конце цикла) и
    сразу уходят строкой в ``progress_path`` и в лог: прогон на час, который
    молчит до последнего хода, неотличим от зависшего.

    ``ban_stats`` — счётчик случаев «запрет повторов не подействовал» (S3am):
    накапливается по ходам и уходит в отчёт, а не остаётся в логе.
    """
    sample = make_sampler(torch)
    hint = args.hint and not args.toolcall_force
    prefill = "<think>\n<tool_call>" if hint else "<think>\n"
    max_turns = args.max_turns
    nogram = getattr(args, "no_repeat_ngram", None)

    contexts, assistant, records = [], [], []
    for task in tasks:
        msgs = [{"role": "system", "content": pipe.UNIFIED_SYSTEM_PROMPT},
                {"role": "user", "content": task["prompt"]}]
        prompt_text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        contexts.append(prompt_text + prefill)
        # segments как в пайплайне: (текст, награждаемый?) — ответ инструмента не
        # награждается, верификатор видит только слова модели
        assistant.append([(prefill, True)])
        records.append({
            "task_index": task["_pool_index"],
            "task_type": task.get("task_type", "?"),
            "source_env": task.get("source_env"),
            "source_task_id": task.get("source_task_id"),
            "turns": 0, "tool_calls": 0, "tool_errors": 0,
            "hit_timeout": False, "hit_context_guard": False,
            "answer_chars": 0, "assistant_tokens": 0,
            "pass": 0, "reward": None, "tool_error": False,
        })

    done = [False] * len(tasks)
    finalized = [False] * len(tasks)
    answers: list[str] = [""] * len(tasks)
    n_tool_calls = [0] * len(tasks)
    n_tool_errors = [0] * len(tasks)
    turn_count = [0] * len(tasks)
    prog = open(progress_path, "a", encoding="utf-8") if progress_path else None

    def finalize(i: int) -> None:
        """Записывает результат задачи, как только он стал известен.

        Строка прогресса на задачу — требование дисциплины размера: часовой
        прогон обязан быть наблюдаемым, а не «зелёным по тишине».
        """
        if finalized[i]:
            return
        finalized[i] = True
        task = tasks[i]
        answer = "".join(t for t, is_assistant in assistant[i] if is_assistant)
        full_text = "".join(t for t, _is_assistant in assistant[i])
        passed = bool(pipe.verify_task(answer, task))
        rollout = pipe.Rollout(
            text=full_text, n_tool_calls=n_tool_calls[i], n_tool_errors=n_tool_errors[i],
            # как в пайплайне: пустой/мусорный ответ = <5 символов во всём накопленном тексте
            has_parse_error=len(full_text.strip()) < 5, hit_timeout=turn_count[i] >= max_turns,
            verifier_passed=passed, n_assistant_tokens=max(len(tok.encode(answer)), 1),
            task=task, n_turns=turn_count[i],
        )
        coverage = float(pipe.keyword_coverage(answer, task))
        r = records[i]
        r.update({
            "turns": turn_count[i],
            "tool_calls": n_tool_calls[i],
            "tool_errors": n_tool_errors[i],
            "tool_error": n_tool_errors[i] > 0,
            "hit_timeout": turn_count[i] >= max_turns,
            "answer_chars": len(answer),
            "assistant_tokens": rollout.n_assistant_tokens,
            "pass": int(passed),
            "reward": pipe.compute_reward(rollout),
            "gold_resolvable": gold_resolvable(task, def_index, sig_words, pipe)[0],
            # покрытие — из контура (keyword_coverage), не своя арифметика:
            # по нему различаются классы «нечего проверять» и «проверяемое не то»
            "coverage": round(coverage, 4),
            "diag_class": None if passed else classify_failure(r, coverage),
        })
        answers[i] = answer
        if prog is not None:
            prog.write(json.dumps(r, ensure_ascii=False) + "\n")
            prog.flush()
        print(f"  [{pool_name or 'pool'}] готово {sum(finalized)}/{len(tasks)} "
              f"idx={r['task_index']} {r['task_type']} ходов={r['turns']} "
              f"инструмент={r['tool_calls']} pass={r['pass']} покрытие={coverage:.2f}",
              file=sys.stderr, flush=True)

    # TOOLCALL_FORCE (дефолт пайплайна, laguna_pipeline_v8.py:1234-1252): teacher-forced
    # первый вызов — query выводится из задачи, ответ инструмента кладётся в контекст
    # ДО первой генерации. У compare_concepts/explain_relation query есть, у остальных
    # типов пустой → принудительного вызова нет. Это часть контура, а не поблажка;
    # флаг выключается, чтобы увидеть, сколько даёт сам бутстрап.
    if args.toolcall_force:
        for i, task in enumerate(tasks):
            tt = task.get("task_type", "")
            if tt == "compare_concepts":
                m = re.search(r"Чем (.+?) отличается", task.get("prompt", ""))
                q = m.group(1) if m else ""
            elif tt == "explain_relation":
                q = " ".join(k for k in task.get("keywords", [])
                             if k.lower() not in ("related", "связаны", "связан"))
            else:
                q = ""
            if not q:
                continue
            call = '<tool_call>{"name": "search_concepts", "query": "%s"}</tool_call>' % q
            tr, te, _ = pipe.execute_tool_call(call)
            if tr is None:
                continue
            block = call + "\n<tool_response>\n" + tr + "\n</tool_response>\n"
            contexts[i] += block
            # В пайплайне этот блок помечен НЕ-assistant (segments → is_assistant=False):
            # верификатор видит только собственные слова модели, не подложенный ответ.
            assistant[i].append((block, False))
            n_tool_calls[i] += 1

    for turn in range(max_turns):
        active = [i for i in range(len(tasks)) if not done[i]]
        if not active:
            break
        t0 = time.time()
        texts = generate_turns(model, tok, [contexts[i] for i in active],
                               max_new_tokens=args.max_new_tokens, temperature=args.temperature,
                               top_k=args.top_k, batch_size=args.batch, torch=torch, sample=sample,
                               no_repeat_ngram=nogram, ban_stats=ban_stats,
                               label=f"{pool_name or 'pool'} ход {turn + 1}")
        for idx, i in enumerate(active):
            turn_text = texts[idx].strip()
            parse_text = ("<tool_call>" + turn_text) if (hint and turn == 0) else turn_text
            tool_resp, tool_err, clean_text = pipe.execute_tool_call(parse_text)
            turn_text = clean_text.strip()
            assistant[i].append(("\n" + turn_text, True))
            turn_count[i] += 1
            if tool_resp is not None:
                n_tool_calls[i] += 1
                if tool_err:
                    n_tool_errors[i] += 1
                tr = f"\n<tool_response>\n{tool_resp}\n</tool_response>"
                assistant[i].append((tr, False))
                contexts[i] = contexts[i] + turn_text + tr + "\n"
                if len(contexts[i]) > 48000:      # защита контекста пайплайна (max_model_len=16384)
                    done[i] = True
                    records[i]["hit_context_guard"] = True
                continue
            done[i] = True                        # ход без <tool_call> = финальный ответ
        for i in active:
            if done[i]:
                finalize(i)
        print(f"  ход {turn+1}/{max_turns}: активных {len(active)}, {time.time()-t0:.1f}s",
              file=sys.stderr, flush=True)

    for i in range(len(tasks)):                   # упёршиеся в потолок ходов — в конце
        finalize(i)
    if prog is not None:
        prog.close()
    return records, answers


# ────────────────────────────── сводка ──────────────────────────────────────

def summarize(records: list[dict], strata: dict, pool_meta: dict, protocol: dict) -> dict:
    """Агрегаты по типам и по пулу — считаются из сырых записей, не из сводки."""
    types = sorted({r["task_type"] for r in records})
    by_type = []
    for t in types:
        rs = [r for r in records if r["task_type"] == t]
        n = len(rs)
        k = sum(r["pass"] for r in rs)
        lo, hi = wilson_ci(k, n)
        turns = [r["turns"] for r in rs]
        rw = [r["reward"] for r in rs if r.get("reward") is not None]
        by_type.append({
            "task_type": t,
            "n": n,
            "n_pass": k,
            "in_pool": strata.get(t, {}).get("in_pool"),
            "pass_rate": round(k / n, 4) if n else None,
            "pass_rate_ci95": [round(lo, 4), round(hi, 4)],
            "dead_share": round(sum(1 for r in rs if r["pass"] == 0) / n, 4) if n else None,
            "trivial_share": round(k / n, 4) if n else None,
            "median_turns": statistics.median(turns) if turns else None,
            "mean_turns": round(statistics.fmean(turns), 3) if turns else None,
            "timeout_share": round(sum(1 for r in rs if r["hit_timeout"]) / n, 4) if n else None,
            "tool_error_share": round(sum(1 for r in rs if r["tool_error"]) / n, 4) if n else None,
            "tool_call_share": round(sum(1 for r in rs if r["tool_calls"] > 0) / n, 4) if n else None,
            "mean_answer_chars": round(statistics.fmean([r["answer_chars"] for r in rs]), 1) if n else None,
            "reward_mean": round(statistics.fmean(rw), 4) if rw else None,
            "zero_reward_share": (round(sum(1 for x in rw if x == 0.0) / len(rw), 4)
                                  if rw else None),
            "dead_by_construction_share": (round(sum(1 for r in rs if not r.get("gold_resolvable", True)) / n, 4)
                                           if n else None),
        })
    n = len(records)
    k = sum(r["pass"] for r in records)
    lo, hi = wilson_ci(k, n)
    rewards = [r["reward"] for r in records if r.get("reward") is not None]
    overall = {
        "n": n,
        "n_pass": k,
        "pass_rate": round(k / n, 4) if n else None,
        "pass_rate_ci95": [round(lo, 4), round(hi, 4)],
        "dead_share": round(sum(1 for r in records if r["pass"] == 0) / n, 4) if n else None,
        "trivial_share": round(k / n, 4) if n else None,
        "median_turns": statistics.median([r["turns"] for r in records]) if n else None,
        "tool_error_share": round(sum(1 for r in records if r["tool_error"]) / n, 4) if n else None,
        "tool_call_share": round(sum(1 for r in records if r["tool_calls"] > 0) / n, 4) if n else None,
        "timeout_share": round(sum(1 for r in records if r["hit_timeout"]) / n, 4) if n else None,
        "dead_by_construction_share": (round(sum(1 for r in records if not r.get("gold_resolvable", True)) / n, 4)
                                       if n else None),
        "zero_reward_share": (round(sum(1 for r in rewards if r == 0.0) / len(rewards), 4)
                              if rewards else None),
        "reward_mean": round(statistics.fmean(rewards), 4) if rewards else None,
    }

    # Полоса ADR-006: 0.25–0.80. При одной попытке на задачу (n_attempts=1) полоса
    # измерима только на агрегате типа задачи, не на отдельной задаче.
    band = {"low": BAND_LOW, "high": BAND_HIGH, "measure": "type-level (n_attempts=1)",
            "types_in_band": [], "tasks_in_band_types": 0, "tasks_total": n}
    for row in by_type:
        if row["pass_rate"] is not None and BAND_LOW <= row["pass_rate"] <= BAND_HIGH:
            band["types_in_band"].append(row["task_type"])
            band["tasks_in_band_types"] += row["n"]
    band["in_band_tasks_share"] = round(band["tasks_in_band_types"] / n, 4) if n else None
    band["dead_tasks"] = sum(1 for r in records if r["pass"] == 0)
    band["trivial_tasks"] = sum(1 for r in records if r["pass"] == 1)
    band["note"] = ("полоса названа для агрегата по типу задачи: одна попытка на задачу "
                    "не даёт per-task оценки вероятности (passk-identifiability, ADR-006)")

    on_checkpoint = bool(protocol.get("checkpoint_sha256"))
    why = ("ориентир снят в vLLM-прогоне RL-разведки (800 траекторий, 100 шагов), здесь — "
           "цикл на transformers с одной попыткой на задачу: сопоставимы агрегаты по порядку "
           "величины, а не «стало лучше/хуже»")
    if on_checkpoint:
        why = ("ориентир снят в vLLM-прогоне RL-разведки, здесь — цикл на transformers с "
               "одной попыткой на задачу: модель того же семейства (SFT-чекпойнт), но другой "
               "инструмент и другой поток RNG — сопоставимы агрегаты, не построчные траектории")
    return {
        "pool_sha256": pool_meta.get("sha256"),
        "n_total": pool_meta.get("lines"),
        "n_taken": n,
        "n_attempts": protocol.get("n_attempts", 1),
        "strata": strata,
        "by_type": by_type,
        "overall": overall,
        "band": band,
        "anchor_v1": ANCHOR_V1,
        "vs_anchor_v1": {
            "pass_rate_delta": (round(overall["pass_rate"] - ANCHOR_V1["pass_rate"], 4)
                                if overall["pass_rate"] is not None else None),
            "zero_reward_share_delta": (round(overall["zero_reward_share"]
                                              - ANCHOR_V1["zero_reward_share"], 4)
                                        if overall["zero_reward_share"] is not None else None),
            "comparable": False,
            "why_not_comparable": why,
            "reading": ("pass-rate ниже ориентира означает, что пул не выродился в тривиальный: "
                        "награду есть за что давать, и она ещё не взята"),
        },
        "anchor_base_v2": ANCHOR_BASE_V2,
        "vs_anchor_base_v2": {
            "pass_rate_delta": (round(overall["pass_rate"] - ANCHOR_BASE_V2["pass_rate"], 4)
                                if overall["pass_rate"] is not None else None),
            "comparable": bool(on_checkpoint),
            "why": ("тот же пул v2 и тот же инструмент, но другая модель (SFT-чекпойнт против "
                    "базы) — это и есть величина, которую S3j-2 отделяет от свойства пула"
                    if on_checkpoint else
                    "замер S3j: тот же пул, та же модель — сравнение прямое"),
            "reading": ("фигура «пул v2 мёртв» из S3j снята на базовой модели; если на обученном "
                        "чекпойнте pass-rate выше, мёртвой была модель, а не пул"),
        },
    }


# ─────────────── диагностика нерешённых задач (S3j-2, п.4) ──────────────────

def diagnose(records: list[dict], answers: list[str], tasks: list[dict], pipe, def_index,
             sig_words, *, n_sample: int, seed: int) -> dict:
    """Разбор нерешённых задач по классам причин, с примерами.

    Выборка детерминирована: индексы нерешённых сортируются (порядок строк пула —
    часть артефакта) и берутся ``random.Random(seed).sample``. Класс каждой задачи
    посчитан в момент финализации (``classify_failure``) и лежит в сырых записях —
    значит пересборка сводки даёт те же числа без GPU.

    INSTRUMENT-SUCCESS-EXAMPLES §2.2: кроме примеров отказов, ``examples`` несёт
    ветвь ``pass`` — до 3 успешных траекторий по одной на тип задачи (носитель
    «что сработало», а не только агрегат).
    """
    failed = [i for i, r in enumerate(records) if r["pass"] == 0]
    take = min(n_sample, len(failed))
    picked = sorted(random.Random(seed).sample(failed, take)) if take else []
    for i in picked:
        records[i]["diag_sampled"] = True

    by_class: dict[str, list[dict]] = {c: [] for c, _ in DIAG_CLASSES}
    for i in picked:
        r, task = records[i], tasks[i]
        cls = r.get("diag_class") or "?"
        by_class.setdefault(cls, []).append({
            "task_index": r["task_index"],
            "task_type": r["task_type"],
            "source_env": r.get("source_env"),
            "prompt": (task.get("prompt") or "")[:600],
            "required": requirement_detail(task, answers[i], pipe, def_index, sig_words),
            "answer_excerpt": answers[i][:ANSWER_CAP],
            "answer_chars": r["answer_chars"],
            "turns": r["turns"], "tool_calls": r["tool_calls"],
            "hit_timeout": r["hit_timeout"], "hit_context_guard": r["hit_context_guard"],
            "coverage": r.get("coverage"), "reward": r.get("reward"),
        })

    counts = {c: len(v) for c, v in by_class.items()}
    # INSTRUMENT-SUCCESS-EXAMPLES §2.2: у успеха появляется носитель для разбора —
    # до 3 траекторий, по одной на тип задачи (тип не повторяется, пока есть непокрытые),
    # структура записи та же, что у примеров отказов. Отбор детерминирован (порядок задач).
    pass_examples: list[dict] = []
    seen_pass_types: set[str] = set()
    for i, r in enumerate(records):
        if r["pass"] != 1:
            continue
        tt = r["task_type"]
        if tt in seen_pass_types:
            continue
        seen_pass_types.add(tt)
        task = tasks[i]
        pass_examples.append({
            "task_index": r["task_index"],
            "task_type": tt,
            "source_env": r.get("source_env"),
            "prompt": (task.get("prompt") or "")[:600],
            "required": requirement_detail(task, answers[i], pipe, def_index, sig_words),
            "answer_excerpt": answers[i][:ANSWER_CAP],
            "answer_chars": r["answer_chars"],
            "turns": r["turns"], "tool_calls": r["tool_calls"],
            "hit_timeout": r["hit_timeout"], "hit_context_guard": r["hit_context_guard"],
            "coverage": r.get("coverage"), "reward": r.get("reward"),
        })
        if len(pass_examples) >= 3:
            break
    return {
        "sampled": len(picked),
        "n_failed": len(failed),
        "n_taken_total": len(records),
        "seed": seed,
        "class_counts": counts,
        "class_share_of_sampled": {c: (round(n / len(picked), 4) if picked else None)
                                   for c, n in counts.items()},
        "class_definitions": {c: d for c, d in DIAG_CLASSES},
        "precedence": ("порядок разбора: (а) нет вызова инструмента → (г) обрыв/лимит → "
                       "(б) нечего проверять (coverage=0) → (в) проверяемое есть, но не то; "
                       "класс считается по keyword_coverage из пайплайна, не своей арифметикой"),
        "examples": {**{c: v[:3] for c, v in by_class.items()}, "pass": pass_examples},
        "note": ("ответы хранятся обрезанными до %d символов; тексты примеров — из "
                 "answers.jsonl прогона, вердикт pass всегда из verify_task" % ANSWER_CAP),
    }


# ─────────── сравнение пулов на одном чекпойнте (S3j-2, п.3 и п.5) ──────────

DELTA_METRICS = ("pass_rate", "dead_share", "trivial_share", "tool_call_share",
                 "median_turns", "reward_mean", "zero_reward_share", "timeout_share")


def _delta_pair(a, b, keys=DELTA_METRICS) -> dict:
    """``b − a`` по метрикам: обе величины рядом с дельтой, иначе дельта нечитаема."""
    out = {}
    for key in keys:
        va, vb = a.get(key), b.get(key)
        out[key] = {"left": va, "right": vb,
                    "delta": (round(vb - va, 4) if isinstance(va, (int, float))
                              and isinstance(vb, (int, float)) else None)}
    return out


def build_delta(left: dict, right: dict, left_name: str, right_name: str) -> dict:
    """Дельта двух проб. Имена пулов — часть результата: «v2 − v1» ≠ «B − A»."""
    lo = left["overall"]
    ro = right["overall"]
    n1, k1 = lo.get("n") or 0, lo.get("n_pass") or 0
    n2, k2 = ro.get("n") or 0, ro.get("n_pass") or 0
    dlo, dhi = newcombe_diff_ci(k1, n1, k2, n2)
    by_type = {}
    lt = {r["task_type"]: r for r in left["by_type"]}
    rt = {r["task_type"]: r for r in right["by_type"]}
    for t in sorted(set(lt) | set(rt)):
        if t in lt and t in rt:
            by_type[t] = _delta_pair(lt[t], rt[t])
            by_type[t]["both_pools"] = True
        else:
            by_type[t] = {"only_in": left_name if t in lt else right_name,
                          "left": lt.get(t, {}).get("pass_rate"),
                          "right": rt.get(t, {}).get("pass_rate"),
                          "both_pools": False,
                          "note": "тип есть только в одном пуле — дельта не считается"}
    in_both = sorted(t for t in by_type if by_type[t]["both_pools"])
    return {
        "left": left_name,
        "right": right_name,
        "types_in_both_pools": in_both,
        "types_only_in_left": sorted(t for t in lt if t not in rt),
        "types_only_in_right": sorted(t for t in rt if t not in lt),
        "aggregate_caveat": ("агрегаты пулов покрывают разные наборы типов: сводная дельта "
                             "отвечает на вопрос «где пул целиком», а построчно сопоставимы "
                             "только типы, измеренные на обоих пулах"
                             + (f" (их {len(in_both)}: {', '.join(in_both)})" if in_both
                                else " (общих типов нет)")),
        "metric_names": DELTA_METRICS,
        "overall": _delta_pair(lo, ro),
        "pass_rate_delta_ci95_newcombe": [round(dlo, 4), round(dhi, 4)],
        "pass_rate_delta_reading": (
            "интервал разности долей (Newcombe): если он накрывает 0, различие пулов "
            "на этой выборке не отделено от шума одной попытки на задачу"),
        "by_type": by_type,
        "measurement": "одна попытка на задачу (n_attempts=1) — не pass@k",
    }


def build_verdict(delta: dict, pools: dict, left_name: str, right_name: str) -> dict:
    """Вердикт: пригоден ли правый пул для RL относительно левого, и почему.

    Критерии названы поимённо и посчитаны из чисел — читатель может не согласиться
    с порогом, но не с величинами. Мнение о причинах берётся из диагностики:
    доминирующий класс отказа — это и есть ответ «что править».
    """
    ro = pools[right_name]["overall"]
    rt = {r["task_type"]: r for r in pools[right_name]["by_type"]}
    dci = delta["pass_rate_delta_ci95_newcombe"]
    floor = ANCHOR_BASE_V2["pass_rate"]
    diag = pools[right_name].get("diagnosis") or {}
    counts = diag.get("class_counts") or {}
    dominant = max(counts, key=counts.get) if counts else None

    criteria = [
        {"name": "непустой сигнал на чекпойнте",
         "value": ro.get("pass_rate"), "threshold": f"> {floor} (пол базовой модели, S3j)",
         "passed": bool(ro.get("pass_rate") is not None and ro["pass_rate"] > floor)},
        {"name": f"{right_name} не хуже {left_name} по порядку величины",
         "value": dci, "threshold": "нижняя граница разности > −0.05",
         "passed": bool(dci[0] > -0.05)},
        {"name": "различие пулов отделено от шума",
         "value": dci, "threshold": "интервал разности не накрывает 0",
         "passed": bool(dci[0] > 0 or dci[1] < 0)},
        {"name": "есть типы в полосе ADR-006 (0.25–0.80)",
         "value": [t for t, r in rt.items()
                   if r.get("pass_rate") is not None and BAND_LOW <= r["pass_rate"] <= BAND_HIGH],
         "threshold": "хотя бы один тип", "passed": any(
             r.get("pass_rate") is not None and BAND_LOW <= r["pass_rate"] <= BAND_HIGH
             for r in rt.values())},
    ]
    usable = criteria[0]["passed"] and criteria[1]["passed"]
    rec: list[str] = []
    if ro.get("pass_rate") is not None and ro["pass_rate"] > floor:
        rec.append(f"{right_name} даёт на обученном чекпойнте pass-rate "
                   f"{ro['pass_rate']:.3f} против {floor:.3f} на базовой модели (S3j): "
                   f"фигура «пул мёртв» относится к базовой модели, а не к пулу")
    if not criteria[1]["passed"]:
        rec.append(f"{right_name} проигрывает {left_name} по pass-rate: дельта "
                   f"{delta['overall']['pass_rate']['delta']} (CI {dci}) — пул как есть "
                   f"для RL не эквивалентен {left_name}")
    elif dci[0] > 0:
        rec.append(f"{right_name} выше {left_name} на {delta['overall']['pass_rate']['delta']} "
                   f"(CI {dci}) — на этой выборке пул не хуже инкумбента")
    else:
        rec.append(f"различие {right_name} и {left_name} не отделено от шума (CI {dci}): "
                   f"пул сопоставим, выбор одной попытки на задачу этого не различает")
    if dominant:
        rec.append("доминирующая причина отказа на "
                   f"{right_name}: {dominant} — {dict(DIAG_CLASSES).get(dominant, dominant)}")
        rec.append({
            "a_no_tool_call": "править формат ответа/бутстрап вызова инструмента "
                              "(TOOLCALL_FORCE/hint), а не задачи",
            "b_no_checkable_values": "править проекцию задачи на ответ: промпт не выводит "
                                     "модель на slug/термины, которые требует верификатор",
            "c_wrong_values": "править задачи: проверяемое есть, но модель выдаёт другое — "
                              "проверить, по силам ли целевые concept-ы семейству",
            "d_cutoff": "править потолок ходов/контекста (max_turns, длина задачи)",
        }.get(dominant, "причина не классифицирована — смотреть примеры диагностики"))
    rec.append("одна попытка на задачу: pass@k не заявляется; полоса ADR-006 названа "
               "для агрегата по типу задачи")
    # Построчно сопоставимы только типы, измеренные на обоих пулах: у пулов разный
    # состав типов, и сводная дельта смешивает состав с решаемостью.
    controlled = {}
    for t, row in (delta.get("by_type") or {}).items():
        if row.get("both_pools"):
            controlled[t] = {"pass_rate_left": row["pass_rate"]["left"],
                             "pass_rate_right": row["pass_rate"]["right"],
                             "pass_rate_delta": row["pass_rate"]["delta"],
                             "tool_call_share_left": row["tool_call_share"]["left"],
                             "tool_call_share_right": row["tool_call_share"]["right"]}
    rec.append(delta.get("aggregate_caveat", ""))
    return {
        "question": f"пригоден ли {right_name} для RL относительно {left_name} "
                    f"на одном чекпойнте",
        "v2_suitable_for_rl": usable,
        "criteria": criteria,
        "controlled_comparison": controlled,
        "recommendation": [s for s in rec if s],
        "dominant_failure_class": dominant,
        "measured_on": "один и тот же чекпойнт на обоих пулах (см. checkpoint)",
    }


def build_assumptions(protocol: dict) -> list[str]:
    """Допущения замера — то, без чего числа отчёта читаются неверно.

    Список выводится из протокола, а не из прозы: если протокол сменился
    (``n_attempts``, ``toolcall_force``), сменится и допущение.
    """
    a = [
        f"одна попытка на задачу (n_attempts={protocol.get('n_attempts', 1)}): измерен "
        f"baseline, а не pass@k — разброс одной попытки в полосу не заложен",
        "кредит бинарный (AD-11): verify_task возвращает bool, градуированных чисел в отчёте нет",
        "полоса 0.25–0.80 (ADR-006) названа для агрегата по типу задачи, а не для отдельной задачи",
        "инференс — transformers на локальной RTX 4080S (vLLM на стенде нет); поток RNG "
        "отличается от vLLM, поэтому сопоставимы агрегаты, а не построчные траектории",
    ]
    if protocol.get("checkpoint_sha256"):
        a += [
            f"веса — SFT-чекпойнт {protocol.get('checkpoint_path')} "
            f"(sha256 {str(protocol.get('checkpoint_sha256'))[:12]}…), а не базовая модель: "
            f"числа описывают артефакт после SFT",
            ("спецтокены чекпойнта НЕ переинициализированы по сабтокенам: строки <think>/"
             "</think>/<tool_response>/</tool_response> обучены стадией SFT, и re-init стёр бы их"
             if not protocol.get("init_special_tokens_subtoken") else
             "спецтокены переинициализированы по сабтокенам (init_special_tokens_subtoken) — "
             "как в run_rl/run_eval: это контурная, а не артефактная конфигурация"),
            "оба пула пройдены в ОДНОМ процессе одной и той же моделью (один загруженный "
            "чекпойнт): сравнение пулов не смешано со сравнением моделей",
            "сравнение с ориентиром ADR-010 (пул v1, vLLM-разведка) качественное: другой "
            "инструмент инференса и другая выборка",
        ]
    else:
        a.append("веса — базовая модель без SFT и без RL: это нижняя граница решаемости пула, "
                 "а не результат обучения")
        a.append("прямое сравнение с ориентиром ADR-010 некорректно: там SFT-чекпойнт, "
                 "здесь база — сравнивать пулы на разных моделях нельзя")
    if protocol.get("toolcall_force"):
        a.append("TOOLCALL_FORCE включён (дефолт контура): принудительный первый вызов "
                 "инструмента только там, где query выводится из задачи "
                 "(compare_concepts/explain_relation); в остальных типах первый вызов "
                 "обязан сгенерировать сам модель")
    return a


def build_open_questions(probe: dict, delta: dict | None = None,
                         on_checkpoint: bool = False) -> list[str]:
    """Что замер не закрывает — вопросы к архитектору, выведенные из чисел."""
    q = [
        "полоса 0.25–0.80 per-task при n_attempts=1 не измерима: чтобы назвать долю "
        "задач в полосе, нужен k>1 на задачу (карточка passk-identifiability) — "
        "запускать ли повторную пробу с k>1",
    ]
    if delta and delta.get("overall"):
        dci = delta.get("pass_rate_delta_ci95_newcombe")
        if dci and dci[0] <= 0 <= dci[1]:
            q.append(f"различие пулов по pass-rate не отделено от шума (дельта "
                     f"{delta['overall']['pass_rate']['delta']}, CI {dci}): нужен ли "
                     f"повторный замер с k>1 на задачу, чтобы решить судьбу пула")
    if on_checkpoint:
        q.append("спецтокены чекпойнта НЕ переинициализированы по сабтокенам (артефакт как "
                 "есть), тогда как run_rl начинает RL с re-init — нужен ли контрольный "
                 "прогон --init-special-tokens, чтобы увидеть, зависит ли вывод от этого шага")
    overall = probe.get("overall") or {}
    by_type = {r["task_type"]: r for r in probe.get("by_type", [])}
    fc = by_type.get("find_concept")
    if fc and fc.get("tool_call_share") == 0.0:
        q.append("find_concept: базовая модель ни разу не сгенерировала <tool_call> "
                 "(tool_call_share=0), поэтому 0 % там означает «не умеет позвать "
                 "инструмент», а не «не умеет решить» — нужен ли замер с "
                 "подсказанным форматом вызова (--hint), чтобы разделить эти причины")
    for t, r in sorted(by_type.items()):
        if r.get("tool_error_share"):
            q.append(f"{t}: доля задач с tool_error {r['tool_error_share']} — "
                     f"проверить, не портит ли ошибка инструмента картину решаемости")
    if overall.get("timeout_share"):
        q.append(f"доля упоров в потолок ходов {overall['timeout_share']}: при "
                 f"max_turns={probe.get('max_turns')} часть задач недосчитана — "
                 f"поднимать ли потолок")
    return q


def build_limits(protocol: dict | None = None) -> list[str]:
    """Границы применимости чисел — то, чем замер НЕ является."""
    protocol = protocol or {}
    out = [
        "одна попытка на задачу (n_attempts=1): pass@k не измерен, полоса 0.25–0.80 названа "
        "для агрегата по типу задачи, а не для отдельной задачи",
        "поток RNG отличается от vLLM: построчные траектории с RL-прогоном не сопоставимы",
    ]
    if protocol.get("checkpoint_sha256"):
        out.append("веса — SFT-чекпойнт (не RL-чекпойнт и не база): числа описывают "
                   "решаемость пула ДО RL, а не результат RL")
        out.append("выборка ≤100 задач на тип при одной попытке: тип с редким отличием "
                   "пулов может не показать его — интервалы Вильсона/Ньюкомба в отчёте")
    else:
        out.append("веса — базовая модель без SFT: это нижняя граница решаемости, а не "
                   "результат RL и не результат SFT-чекпойнта (ориентир ADR-010 снят на SFT)")
    out.append("pool_sha256 — снимок, прочитанный в память целиком: пул фиксируется по хешу "
               "до и после пробы, а не блокировкой файла")
    return out


def build_not_verified(pool_meta: dict) -> list[str]:
    """Что в этом отчёте не подтверждено — по мете прогона, а не по памяти."""
    out = []
    after = pool_meta.get("sha256_after")
    if after and after != pool_meta.get("sha256"):
        out.append("sha256 пула изменился за время пробы — измерен снимок, не текущий файл")
    return out


def attach_notes(ev: dict, notes: list[str]) -> dict:
    """Сведения прогона, без которых числа читаются неверно.

    Отделены от ``assumptions`` намеренно: допущения выводит инструмент из протокола,
    а здесь — то, что знает только запускающий (например, на каких данных обучен
    чекпойнт). Молча приклеить это к выводам значило бы приписать инструменту
    суждение, которого он не делал.
    """
    if notes:
        ev["notes"] = {
            "items": list(notes),
            "framing": ("это сведения запускающего о прогоне, а не вывод инструмента: "
                        "числа отчёта посчитаны из сырых записей, а эти строки "
                        "объясняют, как их читать"),
        }
    return ev


def build_evidence(probe: dict, pool_meta: dict, protocol: dict, run_dir: Path,
                   raw_path: Path, extra: dict | None = None, status: str = "complete") -> dict:
    on_ckpt = bool(protocol.get("checkpoint_sha256"))
    ev = {
        "stage": "S3j-2" if on_ckpt else "S3j",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": ("решаемость задач пула на ОБУЧЕННОМ чекпойнте (S3j-2): отделить "
                    "свойство пула от силы модели"
                    if on_ckpt else
                    "распределение решаемости задач пула v2 базовой моделью до обучения: "
                    "полоса pass-rate (ADR-006), проверка «пул не мёртвый» до фазы 2, "
                    "ожидания по вырожденной награде (ADR-017)"),
        # Контракт отчёта (TASK §«Отчёт»): strictly complete|partial|blocked.
        # Проба дошла до конца → complete; сокращённый объём помечается partial в main().
        "status": status,
        "stand": {"host": "local-rtx4080s", "gpu": "RTX 4080 SUPER 16GB",
                  "gpu_note": "GB10 не задействован; gb10-shared — только чтение",
                  "inference": "transformers (vLLM на стенде нет)"},
        "pool": pool_meta,
        "protocol": protocol,
        "probe": probe,
        # Поля контракта продублированы на верхнем уровне: отчёт читается как
        # status/artifacts/probe/band/assumptions/open_questions, а probe.band
        # остаётся рабочей копией для --summarize.
        "band": probe.get("band"),
        "assumptions": build_assumptions(protocol),
        "open_questions": build_open_questions(probe, on_checkpoint=on_ckpt),
        "artifacts": {"run_dir": str(run_dir.name), "raw_records": str(raw_path.name)},
        # и limits, и not_verified считаются из меты, а не набиваются в main():
        # иначе пересборка сводки через --summarize молча теряла бы и то, и другое.
        "not_verified": build_not_verified(pool_meta),
        "limits": build_limits(protocol),
    }
    if extra:
        ev.update(extra)
    return ev


def build_comparison(per_pool: dict, ckpt_meta: dict | None, protocol: dict, run_dir: Path,
                     fp_before: str, fp_after: str) -> dict:
    """Сравнительный отчёт S3j-2: пулы на ОДНОМ чекпойнте, дельта, диагностика, вердикт.

    Форма — по JSON-контракту дельты: ``status``, ``artifacts``,
    ``checkpoint`` {path, sha256}, ``pools`` {v1: {by_type, overall}, v2: {...}},
    ``delta_v2_minus_v1``, ``diagnosis``, ``verdict``.
    """
    names = sorted(per_pool)
    pools = {}
    for name in names:
        one = per_pool[name]
        pools[name] = {
            "pool_name": name,
            "path": one["pool"]["path"],
            "sha256": one["pool"]["sha256"],
            "lines": one["pool"]["lines"],
            "by_task_type": one["pool"]["by_task_type"],
            "strata": one["probe"]["strata"],
            "n_taken": one["probe"]["n_taken"],
            "by_type": one["probe"]["by_type"],
            "overall": one["probe"]["overall"],
            "band": one["probe"]["band"],
            "vs_anchor_v1": one["probe"]["vs_anchor_v1"],
            "vs_anchor_base_v2": one["probe"]["vs_anchor_base_v2"],
            "pool_audit": one["pool_audit"],
            "sha256_note": one["pool"]["sha256_note"],
            "run_dir": one["run_dir"],
            "raw_records": one["raw_records"],
            "measurements": one["measurements"],
            # диагностика внутри пула: вердикт называет доминирующий класс отказа,
            # а он берётся отсюда, а не из прозы
            "diagnosis": one["diagnosis"],
        }
    delta = build_delta(pools[names[0]], pools[names[1]], names[0], names[1]) if len(names) == 2 \
        else {"left": names[0], "right": names[-1],
              "note": "пулов не два — дельта v2 − v1 не считается"}
    verdict = build_verdict(delta, pools, names[0], names[-1]) if len(names) == 2 else {
        "question": "нужны ровно два пула для дельты", "v2_suitable_for_rl": None}
    diagnosis = {name: per_pool[name]["diagnosis"] for name in names}
    # Объединённые допущения: часть приходит из протокола одной пробы, часть — из
    # того факта, что пулы сравниваются, а не измеряются поодиночке.
    assumptions = build_assumptions(protocol)
    assumptions.append("пулы измерены одним процессом и одной моделью: "
                       f"отпечаток весов до {fp_before[:12]}… и после {fp_after[:12]}… "
                       f"{'совпал' if fp_before == fp_after else 'РАЗОШЁЛСЯ'}")
    status = "complete" if fp_before == fp_after else "partial"
    ev = {
        "stage": "S3j-2",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": ("честное сравнение пулов v1 и v2 на ОДНОМ обученном чекпойнте: "
                    "отделить свойство пула от силы модели (в S3j пул v2 мерился базовой "
                    "моделью, а ориентир ADR-010 снят на SFT — сравнивались модели)"),
        "status": status,
        "stand": {"host": "local-rtx4080s", "gpu": "RTX 4080 SUPER 16GB",
                  "gpu_note": "GB10 не задействован; gb10-shared — чтение чекпойнта и пулов",
                  "inference": "transformers (vLLM на стенде нет)"},
        "checkpoint": ckpt_meta,
        "protocol": protocol,
        "pools": pools,
        "delta_v2_minus_v1": delta,
        "diagnosis": diagnosis,
        "verdict": verdict,
        "artifacts": {"run_dir": run_dir.name,
                      "per_pool": {n: {"dir": pools[n]["run_dir"],
                                       "raw_records": pools[n]["raw_records"],
                                       "answers": "answers.jsonl"}
                                   for n in names},
                      "note": ("сырые записи и тексты ответов лежат в каталоге прогона; "
                               "сводка пересобирается из них без GPU (--summarize)")},
        "assumptions": assumptions,
        "open_questions": build_open_questions(pools[names[-1]], delta, ckpt_meta is not None),
        "limits": build_limits(protocol),
        "not_verified": [n for name in names for n in build_not_verified(per_pool[name]["pool"])],
    }
    return ev


# ─────────────── манифест прогона (AD-2, страж C-012) ───────────────────────
#
# Манифест пишет ГЕНЕРАТОР КЕЙСА (``tools/write_run_manifest.py``) — одна реализация
# на весь кейс (ADR-019): своя копия формата разошлась бы с чужой молча, и страж
# C-012 проверял бы не то, что пишет проба. Проба лишь передаёт фактические входы.

#: Образ окружения локального прогона: контейнера нет, поэтому называется то, чем
#: прогон действительно исполнен. Перекрывается ``--image`` или ``$LAGUNA_IMAGE``.
#:
#: Строка названа **фактом, а не намерением**: агентную пробу исполняет
#: ``/usr/bin/python3`` (в ``python3`` из PATH — miniconda 3.11 — сломан импорт
#: transformers: ``huggingface-hub 1.27.0`` не проходит проверку версии, см.
#: HANDOFF SFT-прогона). Прежняя редакция константы называла «python3.11 + torch
#: 2.4.0» — это описание miniconda-окружения, которым проба не запускается; в S3am
#: строка приведена к тому, что печатает сам интерпретатор прогона.
LOCAL_IMAGE = "local-rtx4080s: /usr/bin/python3.12 + torch 2.5.1+cu121 + transformers 4.44.2 (без контейнера)"


def write_manifest(run_dir: Path, *, datasets: list[tuple[str, Path]], base_model_id: str,
                   seed: int, run_version: str, image: str, hyperparams: dict,
                   force: bool = False, complete: bool = True) -> tuple[Path | None, str]:
    """Зовёт генератор манифеста кейса. Возвращает (путь или None, сообщение).

    Прогон на несколько пулов: первый по имени уходит в ``--dataset`` (его хеш
    становится ``dataset_sha256``), остальные — в ``--dataset-extra``, то есть
    «дополнительно пиннуемые датасеты прогона» ровно по контракту генератора.
    """
    if not datasets:
        return None, "нет датасетов — манифест не о чем писать"
    gen = CASE_ROOT / "tools" / "write_run_manifest.py"
    if not gen.is_file():
        return None, f"генератор манифеста недоступен: {gen}"
    ordered = sorted(datasets, key=lambda x: x[0])
    cmd = [sys.executable, str(gen), "--run-dir", str(run_dir),
           "--dataset", str(ordered[0][1]), "--base-model", base_model_id,
           "--pipeline", str(CASE_ROOT / "laguna_pipeline_v8.py"),
           "--seed", str(seed), "--image", image,
           "--stages", "probe=done" if complete else "probe=partial",
           "--run-version", run_version,
           "--relative-to", str(CASE_ROOT)]
    for name, path in ordered[1:]:
        cmd += ["--dataset-extra", f"pool_{name}={path}"]
    for key, value in hyperparams.items():
        cmd += ["--hyperparams", f"{key}={json.dumps(value, ensure_ascii=False)}"]
    if complete:
        cmd.append("--complete")
    if force:
        cmd.append("--force")
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        return None, (f"генератор манифеста вернул {p.returncode}: "
                      f"{(p.stderr or p.stdout).strip()[:400]}")
    # Сообщение генератора отдаётся как есть: «уже актуален, не перезаписываю» —
    # это другой исход, чем «записан», и склеивать их в одну строку нельзя.
    return run_dir / "run_manifest.json", (p.stdout.strip().splitlines() or ["записан"])[0]


def cmd_manifest_for(args) -> int:
    """Восстановить манифест для прогона, записанного до того, как проба его писала.

    Значения берутся из боковой меты самого прогона (``probe_meta.json``), а не из
    повторного измерения; происхождение помечается в ``run_version`` явно.
    """
    run_dir = Path(args.manifest_for)
    side = run_dir / "probe_meta.json"
    if not side.is_file():
        print(f"NOT-VERIFIED: нет меты прогона {side}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if (run_dir / "run_manifest.json").is_file():
        print(f"ОТКАЗ: манифест уже есть: {run_dir / 'run_manifest.json'}", file=sys.stderr)
        return EXIT_REFUSE
    meta = json.loads(side.read_text(encoding="utf-8"))
    pool, protocol = meta.get("pool", {}), meta.get("protocol", {})
    name = meta.get("pool_name") or Path(str(pool.get("path", "pool"))).stem or "pool"
    path = Path(pool.get("path", ""))
    if not path.is_file():
        print(f"NOT-VERIFIED: пул прогона недоступен ({path}) — манифест был бы без хеша",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED
    ckpt = meta.get("checkpoint") or {}
    weights = protocol.get("weights") or "?"
    base_id = (f"sft_checkpoint_final.pt@{str(ckpt.get('sha256'))[:12]} (Qwen/Qwen2.5-0.5B)"
               if ckpt.get("sha256") else str(weights))
    hyper = {"checkpoint_sha256": ckpt.get("sha256"), "weights": str(weights)}
    # Пул мог быть переписан после прогона: тогда хеш файла — уже не то, что измерялось,
    # и манифест обязан назвать ИЗМЕРЕННЫЙ снимок, а не текущий файл.
    if pool.get("sha256") != sha256_file(path):
        hyper["measured_snapshot_sha256"] = pool.get("sha256")
        hyper["measured_snapshot_note"] = ("хеш снимка, который читал прогон; файл пула "
                                          "переписан позже — текущий хеш в dataset_sha256")
    out, msg = write_manifest(
        run_dir, datasets=[(name, path)], base_model_id=base_id,
        seed=int(protocol.get("seed", 0)),
        run_version=("reconstructed: laguna-passrate-probe --manifest-for из probe_meta.json "
                     "этого же прогона (манифест прогоном не писался)"),
        image=args.image, hyperparams=hyper)
    if out is None:
        print(f"NOT-VERIFIED: {msg}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    print(json.dumps({"manifest": str(out), "message": msg}, ensure_ascii=False, indent=2))
    return EXIT_OK


# ─────────────────────────────── main ───────────────────────────────────────

FINGERPRINTS_FILE = "fingerprints.json"


def _load_fingerprints(raw: Path) -> dict:
    """Отпечатки весов прогона: лежат рядом с прогоном (``run_dir/<пул>/`` или сам ``run_dir``)."""
    for cand in (raw.parent / FINGERPRINTS_FILE, raw.parent.parent / FINGERPRINTS_FILE):
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
    return {}


def _rebuild_one(raw: Path) -> dict | None:
    """Сводка + мета одного прогона из сырых записей (без GPU)."""
    if not raw.is_file():
        return None
    records = load_jsonl(raw)
    if not records:
        return None
    side = raw.parent / "probe_meta.json"
    meta = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
    probe = summarize(records, meta.get("strata", {}), meta.get("pool", {}), meta.get("protocol", {}))
    # Классы отказов и признак попадания в диагностическую выборку считаются в момент
    # финализации и лежат в сырых записях — значит counts пересобираются, а примеры
    # (тексты ответов) берутся из answers.jsonl рядом.
    sampled = [r for r in records if r.get("diag_sampled")]
    if sampled:
        counts = {c: 0 for c, _ in DIAG_CLASSES}
        for r in sampled:
            cls = r.get("diag_class") or "?"
            counts[cls] = counts.get(cls, 0) + 1
        probe["diagnosis_counts_from_raw"] = {
            "sampled": len(sampled), "class_counts": counts,
            "class_share_of_sampled": {c: (round(v / len(sampled), 4))
                                       for c, v in counts.items()},
            "note": "примеры не восстанавливаются без answers.jsonl — их несёт отчёт прогона",
        }
    return {"probe": probe, "meta": meta, "pool_name": meta.get("pool_name")}


def cmd_summarize(args) -> int:
    raws = [Path(x) for x in args.summarize]
    built = []
    for raw in raws:
        one = _rebuild_one(raw)
        if one is None:
            print(f"NOT-VERIFIED: нет сырых записей или они пусты: {raw}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        built.append((raw, one))
    raw, one = built[0]
    ev = build_evidence(one["probe"], one["meta"].get("pool", {}), one["meta"].get("protocol", {}),
                        raw.parent, raw, extra={"pool_audit": one["meta"].get("pool_audit")})
    ev["measurements"] = one["meta"].get("measurements") or {"wall_seconds": None}
    ev["measurements"]["note"] = ("замеры стены относятся к прогону, сводка пересобрана из "
                                  "сырых записей без GPU")
    ev["rebuilt_from"] = str(raw)
    attach_notes(ev, args.evidence_note or [])
    if len(built) > 1:                            # сравнительная пересборка без GPU
        pools = {}
        for raw_i, one_i in built:
            name = one_i.get("pool_name") or raw_i.parent.name
            pools[name] = dict(one_i["probe"])
            pools[name]["pool_name"] = name
            pools[name]["path"] = one_i["meta"].get("pool", {}).get("path")
            pools[name]["sha256"] = one_i["meta"].get("pool", {}).get("sha256")
            pools[name]["protocol"] = one_i["meta"].get("protocol", {})
            pools[name]["diagnosis"] = one_i["meta"].get("diagnosis") or \
                pools[name].get("diagnosis_counts_from_raw") or {}
        ev["stage"] = "S3j-2"
        ev["pools"] = pools
        ev["diagnosis"] = {n: pools[n].get("diagnosis") for n in pools}
        ev["artifacts"] = {
            "per_pool": {name: {"dir": raw_i.parent.name, "raw_records": raw_i.name,
                                "answers": (raw_i.parent / "answers.jsonl").name}
                         for raw_i, one_i in built
                         for name in [one_i.get("pool_name") or raw_i.parent.name]},
            "note": "отчёт пересобран из сырых записей прогона; веса не перечитывались",
        }
        # Чекпойнт и отпечаток весов берутся из боковых файлов прогона: в пересборке
        # веса не перечитываются, но всё, что о них записано прогоном, обязано дойти.
        ck = built[0][1]["meta"].get("checkpoint")
        if ck:
            ev["checkpoint"] = ck
        fp = _load_fingerprints(built[0][0])
        if fp:
            ev["weights_fingerprint_before"] = fp.get("before")
            ev["weights_fingerprint_after"] = fp.get("after")
            ev["weights_unchanged_across_pools"] = bool(fp.get("before") == fp.get("after"))
        names = sorted(pools)
        if len(names) == 2:
            ev["delta_v2_minus_v1"] = build_delta(pools[names[0]], pools[names[1]],
                                                  names[0], names[1])
            ev["verdict"] = build_verdict(ev["delta_v2_minus_v1"], pools, names[0], names[1])
        ev["rebuilt_from"] = [str(r) for r, _ in built]
    if args.evidence:
        Path(args.evidence).write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
        print(f"сводка пересобрана из {[str(r) for r, _ in built]} → {args.evidence}")
    else:
        print(json.dumps(ev, ensure_ascii=False, indent=2))
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S3j: baseline pass-rate пула ревизии")
    ap.add_argument("--pool", action="append", default=None,
                    help="пул задач (jsonl) или NAME=PATH; повторяется для S3j-2 "
                         "(сравнение пулов на одном чекпойнте). "
                         "Дефолт — datasets/rl_tasks_revpool_v2.jsonl")
    ap.add_argument("--run-dir", help="каталог пробы (создаётся)")
    ap.add_argument("--evidence", help="куда положить сводку (JSON)")
    ap.add_argument("--model", default="/home/user/gb10-shared/models-store/experiments/"
                                       "kda-graft/models/qwen2.5-0.5b-base")
    ap.add_argument("--tokenizer", default="Qwen/Qwen2.5-0.5B")
    ap.add_argument("--index", default="datasets/concepts_search_index.jsonl",
                    help="индекс концептов для search_concepts (CONCEPTS_INDEX)")
    ap.add_argument("--per-type", type=int, default=200, help="сколько задач брать на тип")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--batch", type=int, default=8, help="размер батча генерации")
    ap.add_argument("--max-turns", type=int, default=15)
    ap.add_argument("--max-new-tokens", type=int, default=4096, help="потолок хода (контур)")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--top-k", type=int, default=20)
    # ── S3am: штатный режим декодирования агентной пробы (ADR-041) ────────────
    ap.add_argument("--no-repeat-ngram", type=int, default=None, metavar="N",
                    help="запрет повторов n-грамм (штатный режим стадии): N=4 — "
                         "умолчание прибора; N=0 — прогон без запрета, разрешён "
                         "только вместе с --legacy-decoding (иначе отказ, код 1). "
                         "Параметры контура (temperature/top_k) не меняются: "
                         "штатный режим агентной пробы — запрет повторов, а не greedy")
    ap.add_argument("--legacy-decoding", action="store_true",
                    help="снять штатный запрет повторов n-грамм и разрешить прогон в "
                         "прежнем режиме (числа до S3am, в т.ч. agentic-база "
                         "45.1 % = 158/350). Только для воспроизведения прежних чисел: "
                         "отчёт помечается непригодным для выводов по критерию стадии "
                         "(protocol.decoding.allowed_for_conclusions=false)")
    ap.add_argument("--toolcall-force", dest="toolcall_force", action="store_true", default=True,
                    help="teacher-forced первый вызов инструмента (дефолт контура)")
    ap.add_argument("--no-toolcall-force", dest="toolcall_force", action="store_false")
    ap.add_argument("--hint", dest="hint", action="store_true", default=True,
                    help="prefill '<think>\\n<tool_call>' (действует только без force)")
    ap.add_argument("--no-hint", dest="hint", action="store_false")
    ap.add_argument("--expect-pool-sha256", default=None,
                    help="отказ, если sha256 пула не совпал (пул переписывается параллельно)")
    ap.add_argument("--checkpoint", default=None,
                    help="S3j-2: .pt стадии SFT ({'model': state_dict, 'optimizer': ...}); "
                         "замер идёт на нём, а не на --model")
    ap.add_argument("--config-from", default="Qwen/Qwen2.5-0.5B",
                    help="откуда взять архитектуру для --checkpoint (config.json модели, "
                         "под которую обучался чекпойнт)")
    ap.add_argument("--init-special-tokens", dest="init_special_tokens", action="store_true",
                    default=False,
                    help="переинициализировать спецтокены по сабтокенам (как run_rl/run_eval) — "
                         "контурная, а не артефактная конфигурация")
    ap.add_argument("--diagnose", type=int, default=25,
                    help="сколько нерешённых задач разбирать по классам причин (S3j-2, п.4)")
    ap.add_argument("--diagnose-seed", type=int, default=42,
                    help="сид детерминированной выборки нерешённых")
    ap.add_argument("--dry-run", action="store_true", help="отбор без модели")
    ap.add_argument("--summarize", metavar="RAW_JSONL", action="append",
                    help="пересобрать сводку из сырых записей (без GPU); несколько раз — "
                         "сравнительный отчёт по нескольким прогонам")
    ap.add_argument("--manifest-for", metavar="RUN_DIR",
                    help="восстановить run_manifest.json (AD-2) для прогона, записанного до "
                         "того, как проба стала писать манифест")
    ap.add_argument("--image", default=os.environ.get("LAGUNA_IMAGE", LOCAL_IMAGE),
                    help="образ/окружение прогона для манифеста (AD-2); "
                         "дефолт — фактическое локальное окружение")
    ap.add_argument("--evidence-note", action="append", default=[], metavar="TEXT",
                    help="сведение о прогоне, которое отчёт обязан донести (повторяемый); "
                         "попадает в notes отчёта — это НЕ вывод инструмента, а то, "
                         "что читатель иначе не увидел бы из чисел")
    args = ap.parse_args(argv)

    if args.manifest_for:
        return cmd_manifest_for(args)
    if args.summarize:
        return cmd_summarize(args)

    #: Штатный режим (S3am) разрешается **до** чтения пулов, индекса и весов: отказ в
    #: прогоне без запрета повторов — отказ конфигурации, и стоить он должен секунды,
    #: а не минуты загрузки чекпойнта по сети. Порядок проверок наблюдаем: прогон без
    #: запрета упирается в код 1 **даже** при несуществующем пуле (код 2), и это
    #: проверяется тестом — иначе отказ можно было бы «пройти», сломав вход.
    nogram, refusal = resolve_no_repeat_ngram(args.no_repeat_ngram, args.legacy_decoding)
    if refusal:
        print(refusal, file=sys.stderr)
        return EXIT_REFUSE
    #: Запрошенное значение (что стояло в командной строке) не теряется: в отчёт
    #: идут и «что просили», и «что получилось» — иначе `--no-repeat-ngram 8`
    #: выглядел бы в отчёте умолчанием.
    nogram_requested = args.no_repeat_ngram
    args.no_repeat_ngram = nogram
    if not nogram:
        print("ВНИМАНИЕ: прогон без запрета повторов n-грамм (--legacy-decoding): "
              "числа сняты в прежнем режиме и для выводов по критерию стадии не "
              "годятся — сравнивать их можно только с числами того же режима",
              file=sys.stderr)
    print(f"режим декодирования: {decoding_label(args.temperature, args.top_k, nogram)} "
          f"(штатный: запрет повторов {STANDARD_NO_REPEAT_NGRAM}-грамм при параметрах "
          f"контура temperature={args.temperature:g}, top_k={args.top_k})",
          file=sys.stderr)

    pools_spec = args.pool or ["datasets/rl_tasks_revpool_v2.jsonl"]
    pools: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for spec in pools_spec:
        name, path = parse_pool_spec(spec)
        if name in seen:
            print(f"ОТКАЗ: имя пула '{name}' повторяется — дельта была бы неоднозначной",
                  file=sys.stderr)
            return EXIT_REFUSE
        seen.add(name)
        pools.append((name, path))
    expects = parse_expect_shas(args.expect_pool_sha256)

    loaded: list[dict] = []
    for name, path in pools:
        if not path.is_file():
            print(f"NOT-VERIFIED: пул не найден: {path}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        raw_bytes = path.read_bytes()
        pool_sha = sha256_bytes(raw_bytes)
        want = expects.get(name) or expects.get("*")
        if want and pool_sha != want:
            print(f"ОТКАЗ: sha256 пула '{name}' {pool_sha} != ожидаемого {want}", file=sys.stderr)
            return EXIT_REFUSE
        rows = [json.loads(l) for l in raw_bytes.decode("utf-8").splitlines() if l.strip()]
        for i, r in enumerate(rows):
            r["_pool_index"] = i
        tasks, strata = stratify(rows, args.per_type, args.seed)
        if not tasks:
            print(f"ОТКАЗ: в пуле '{name}' нет задач", file=sys.stderr)
            return EXIT_REFUSE
        loaded.append({"name": name, "path": path, "sha256": pool_sha, "rows": rows,
                       "tasks": tasks, "strata": strata})
        print(f"пул [{name}]: {len(rows)} задач, sha256 {pool_sha[:12]}…, типы: "
              f"{ {t: v['in_pool'] for t, v in sorted(strata.items())} }", file=sys.stderr)
        print(f"  взято: {len(tasks)} задач ({ {t: v['taken'] for t, v in sorted(strata.items())} })",
              file=sys.stderr)

    if args.dry_run:
        if len(loaded) == 1:
            one = loaded[0]
            print(json.dumps({"pool": {"path": str(one["path"]), "sha256": one["sha256"],
                                       "lines": len(one["rows"]),
                                       "by_task_type": {t: v["in_pool"]
                                                        for t, v in sorted(one["strata"].items())},
                                       "sha256_after": None, "sha256_note": None},
                              "strata": one["strata"], "n_taken": len(one["tasks"])},
                             ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"pools": {one["name"]: {
                "path": str(one["path"]), "sha256": one["sha256"], "lines": len(one["rows"]),
                "strata": one["strata"], "n_taken": len(one["tasks"])} for one in loaded}},
                ensure_ascii=False, indent=2))
        return EXIT_OK

    if not args.run_dir:
        print("ОТКАЗ: без --run-dir пробу не запускаем (AD-2: прогон без каталога)", file=sys.stderr)
        return EXIT_REFUSE
    index = Path(args.index)
    if not index.is_file():
        print(f"NOT-VERIFIED: индекс концептов не найден: {index}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    ckpt_path = Path(args.checkpoint) if args.checkpoint else None
    if ckpt_path is not None and not ckpt_path.is_file():
        print(f"NOT-VERIFIED: чекпойнт не найден: {ckpt_path}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Семантика контура — импортом, не копией: search_concepts/execute_tool_call/
    # verify_task/compute_reward/Rollout берутся из самого пайплайна.
    os.environ["CONCEPTS_INDEX"] = str(index.resolve())
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    sys.path.insert(0, str(CASE_ROOT))
    import laguna_pipeline_v8 as pipe  # noqa: E402

    import torch  # noqa: E402
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

    torch.manual_seed(args.seed)
    tok = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)
    # Тот же порядок, что в пайплайне: сначала спецтокены, потом resize модели под них.
    tok.add_special_tokens({"additional_special_tokens": list(pipe.SPECIAL_TOKENS)})
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    t0 = time.time()
    ckpt_meta = None
    if ckpt_path is not None:
        from transformers import AutoConfig  # noqa: E402
        # Архитектура берётся конфигом, а не весами базы: все тензоры всё равно
        # перекрываются чекпойнтом, и грузить 1 ГБ базовых весов «на выброс» незачем.
        cfg = AutoConfig.from_pretrained(args.config_from, local_files_only=True)
        model = AutoModelForCausalLM.from_config(cfg, torch_dtype=torch.bfloat16).eval()
        model.resize_token_embeddings(len(tok))
        ckpt_meta = load_checkpoint(model, ckpt_path, torch, pipe, tok,
                                    init_special_tokens=args.init_special_tokens)
        model = model.cuda()
        ckpt_meta["load_verification"] = verify_checkpoint_load(model, ckpt_path, torch)
    else:
        model = AutoModelForCausalLM.from_pretrained(args.model, local_files_only=True,
                                                     torch_dtype=torch.bfloat16).eval().cuda()
    print(f"веса загружены за {time.time()-t0:.1f}s, "
          f"{torch.cuda.memory_allocated()/2**30:.2f} GiB", file=sys.stderr)
    if ckpt_meta is not None:
        v = ckpt_meta["load_verification"]
        print(f"чекпойнт: {ckpt_meta['path']} sha256 {ckpt_meta['sha256'][:12]}…, "
              f"сверено тензоров {v['tensors_compared']}/{v['tensors_in_checkpoint']}, "
              f"расхождений {v['mismatches']}, max|Δ|={v['max_abs_diff']:g}", file=sys.stderr)
        print(f"  словарь: чекпойнт {ckpt_meta['vocab_in_checkpoint']} → модель "
              f"{ckpt_meta['vocab_model']} (токенизатор {ckpt_meta['vocab_tokenizer']}), "
              f"re-init спецтокенов: {ckpt_meta['init_special_tokens_subtoken']}",
              file=sys.stderr)

    t0 = time.time()
    pipe._load_concepts_index()
    def_index = pipe._ensure_def_index()
    print(f"индекс концептов: {len(pipe._load_concepts_index())} записей за {time.time()-t0:.1f}s",
          file=sys.stderr)

    #: Счётчик «запрет повторов не подействовал» (S3am) — наполняется ходами пробы и
    #: уходит в протокол/замеры. Ноль в норме; ненулевое значение означает, что в
    #: каких-то ходах штатный режим фактически не применился, и это обязано быть
    #: видно в отчёте, а не только в логе.
    ban_stats: dict = {"all_banned_fallbacks": 0}

    protocol = {
        "harness": "transformers (цикл на KV-кэше, логиты только последней позиции)",
        "system_prompt": "UNIFIED_SYSTEM_PROMPT (laguna_pipeline_v8.py)",
        "tool": "search_concepts (импорт из пайплайна)",
        "verifier": "verify_task (импорт из пайплайна), кредит бинарный (AD-11)",
        "max_turns": args.max_turns,
        "max_new_tokens": args.max_new_tokens,
        "temperature": args.temperature,
        "top_k": args.top_k,
        #: S3am: режим декодирования — часть протокола, а не настройка запуска.
        #: `mode`/`no_repeat_ngram`/`temperature`/`top_k` читаются сводом, поэтому
        #: смешать режимы в одной таблице молча больше нельзя (ADR-041 п.4).
        "decoding": decoding_protocol_block(nogram, nogram_requested,
                                            args.legacy_decoding, args.temperature,
                                            args.top_k, ban_stats.get("all_banned_fallbacks", 0)),
        "no_repeat_ngram": nogram,
        "decoding_note": (
            "штатный режим стадии — запрет повторов 4-грамм при параметрах контура "
            "(ADR-041): язык, формат и агентность обязаны мериться одним режимом, "
            "иначе критерий стадии собирается из несопоставимых чисел"),
        "stop": STOP_STRINGS,
        "prefill": "<think>\\n<tool_call>" if (args.hint and not args.toolcall_force) else "<think>\\n",
        "toolcall_force": bool(args.toolcall_force),
        "n_attempts": 1,
        "pass_at_k": False,
        "seed": args.seed,
        "diagnose": args.diagnose,
        "diagnose_seed": args.diagnose_seed,
        "batch": args.batch,
        "weights": ckpt_meta["path"] if ckpt_meta else args.model,
        "weights_kind": "sft_checkpoint" if ckpt_meta else "base_model",
        "tokenizer": args.tokenizer,
        "special_tokens": list(pipe.SPECIAL_TOKENS),
        "checkpoint_path": ckpt_meta["path"] if ckpt_meta else None,
        "checkpoint_sha256": ckpt_meta["sha256"] if ckpt_meta else None,
        "init_special_tokens_subtoken": bool(args.init_special_tokens),
        "rng_note": ("поток RNG не совпадает с vLLM RL-прогона: сопоставимы агрегаты, "
                     "не построчные траектории"),
    }
    fp_before = weights_fingerprint(model, torch)

    per_pool: dict[str, dict] = {}
    for one in loaded:
        name, path, tasks = one["name"], one["path"], one["tasks"]
        print(f"── проба пула [{name}]: {len(tasks)} задач", file=sys.stderr)
        pool_run_dir = run_dir if len(loaded) == 1 else (run_dir / name)
        pool_run_dir.mkdir(parents=True, exist_ok=True)
        rows = one["rows"]
        audit = audit_pool(rows, def_index, pipe._sig_words, pipe)
        print(f"аудит пула [{name}]: мёртвых по построению "
              f"{audit['dead_by_construction']} из {audit['n']}", file=sys.stderr)

        t0 = time.time()
        ban_before = ban_stats.get("all_banned_fallbacks", 0)
        records, answers = run_probe(args, pipe, torch, tok, model, tasks, def_index,
                                     pipe._sig_words, pool_name=name,
                                     ban_stats=ban_stats,
                                     progress_path=pool_run_dir / "tasks.progress.jsonl")
        # Счётчик запрета — по пулу, а не только суммарно: если штатный режим где-то
        # не применился, должно быть видно, где именно (S3am).
        nogram_fallbacks = ban_stats.get("all_banned_fallbacks", 0) - ban_before
        wall = time.time() - t0
        print(f"проба [{name}]: {len(records)} задач за {wall/60:.1f} мин", file=sys.stderr)

        # Диагностика идёт ДО записи сырых записей: она проставляет признак
        # diag_sampled, и без него пересборка сводки без GPU потеряла бы, из каких
        # задач собраны классы отказов.
        diag = diagnose(records, answers, tasks, pipe, def_index, pipe._sig_words,
                        n_sample=args.diagnose, seed=args.diagnose_seed)

        raw_path = pool_run_dir / "tasks.jsonl"
        with open(raw_path, "w", encoding="utf-8") as f:      # финальный порядок = порядок задач
            for r in sorted(records, key=lambda x: x["task_index"]):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(pool_run_dir / "answers.jsonl", "w", encoding="utf-8") as f:
            for task, rec, ans in zip(tasks, records, answers):
                f.write(json.dumps(answer_record(task, rec, ans), ensure_ascii=False) + "\n")

        pool_meta = {"path": str(path), "sha256": one["sha256"], "lines": len(rows),
                     "by_task_type": {t: v["in_pool"] for t, v in sorted(one["strata"].items())},
                     "sha256_after": None, "sha256_note": None}
        pool_meta["sha256_after"] = sha256_file(path)
        pool_meta["sha256_note"] = ("пул не менялся за время пробы"
                                    if pool_meta["sha256_after"] == one["sha256"]
                                    else "ПУЛ ПЕРЕПИСАН ВО ВРЕМЯ ПРОБЫ: измерен снимок с sha256 выше")

        probe = summarize(records, one["strata"], pool_meta, protocol)
        per_pool[name] = {
            "pool_name": name,
            "pool": pool_meta,
            "pool_audit": audit,
            "probe": probe,
            "diagnosis": diag,
            "run_dir": pool_run_dir.name,
            "measurements": {"wall_seconds": round(wall, 1),
                             "seconds_per_task": round(wall / len(records), 2),
                             "nogram_all_banned_fallbacks": nogram_fallbacks},
            "raw_records": str(raw_path.name),
        }
        (pool_run_dir / "probe_meta.json").write_text(
            json.dumps({"pool_name": name, "pool": pool_meta, "strata": one["strata"],
                        "protocol": protocol, "pool_audit": audit,
                        "diagnosis": diag, "checkpoint": ckpt_meta,
                        "measurements": {"wall_seconds": round(wall, 1),
                                         "nogram_all_banned_fallbacks": nogram_fallbacks}},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Запрет повторов работает по ходам пробы, поэтому его счётчик известен только
    # здесь — и он попадает в протокол отчёта (а не остаётся в логе прогона).
    protocol["decoding"]["all_banned_fallbacks"] = ban_stats.get("all_banned_fallbacks", 0)

    fp_after = weights_fingerprint(model, torch)
    # Отпечатки — в боковой файл прогона: тогда и пересборка без GPU доказывает, что
    # оба пула прошла одна и та же модель, а не две по очереди.
    (run_dir / FINGERPRINTS_FILE).write_text(
        json.dumps({"before": fp_before, "after": fp_after,
                    "unchanged": fp_before == fp_after,
                    "pools": [one["name"] for one in loaded],
                    "note": "отпечаток считается по выборке весов модели до и после прогонов"},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if len(loaded) == 1:
        one = per_pool[loaded[0]["name"]]
        ev = build_evidence(one["probe"], one["pool"], protocol, run_dir,
                            run_dir / one["raw_records"], extra={"pool_audit": one["pool_audit"]})
        ev["measurements"] = dict(one["measurements"], gen_eval=None)
        if ckpt_meta:
            ev["checkpoint"] = ckpt_meta
        if len(loaded) == 1:
            ev["diagnosis"] = one["diagnosis"]
    else:
        ev = build_comparison(per_pool, ckpt_meta, protocol, run_dir, fp_before, fp_after)
    ev["weights_fingerprint_before"] = fp_before
    ev["weights_fingerprint_after"] = fp_after
    ev["weights_unchanged_across_pools"] = (fp_before == fp_after)
    # AD-2: каталог прогона без манифеста не является доказательством — манифест
    # пишется тем же прогоном, а не вспоминается после.
    base_id = (f"sft_checkpoint_final.pt@{ckpt_meta['sha256'][:12]} (Qwen/Qwen2.5-0.5B)"
               if ckpt_meta else str(args.model))
    mf_path, mf_msg = write_manifest(
        run_dir, datasets=[(one["name"], one["path"]) for one in loaded],
        base_model_id=base_id, seed=args.seed,
        run_version="tools/passrate_probe.py@" + sha256_file(Path(__file__))[:12],
        image=args.image,
        hyperparams={"pools": ",".join(sorted(one["name"] for one in loaded)),
                     "per_type": args.per_type, "max_turns": args.max_turns,
                     "max_new_tokens": args.max_new_tokens, "temperature": args.temperature,
                     "top_k": args.top_k, "n_attempts": 1, "pass_at_k": False,
                     # S3am: режим декодирования обязан быть в манифесте (AD-2) — по
                     # нему прогон и признаётся сопоставимым с другими прогонами стадии.
                     "no_repeat_ngram": nogram,
                     "decoding_mode": protocol["decoding"]["mode"],
                     "legacy_decoding": bool(args.legacy_decoding),
                     "toolcall_force": bool(args.toolcall_force),
                     "weights": protocol["weights"], "weights_kind": protocol["weights_kind"],
                     "checkpoint_sha256": protocol["checkpoint_sha256"],
                     "init_special_tokens_subtoken": bool(args.init_special_tokens)})
    attach_notes(ev, args.evidence_note or [])
    ev["run_manifest"] = {"path": str(mf_path) if mf_path else None, "message": mf_msg}
    if mf_path is None:
        print(f"ПРЕДУПРЕЖДЕНИЕ: манифест прогона не записан ({mf_msg}) — AD-2 не закрыт",
              file=sys.stderr)
    if args.evidence:
        Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
        Path(args.evidence).write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
        print(f"сводка → {args.evidence}", file=sys.stderr)
    if len(loaded) == 1:
        one = per_pool[loaded[0]["name"]]
        print(json.dumps({"pool_sha256": one["pool"]["sha256"],
                          "n_taken": one["probe"]["n_taken"],
                          "overall": one["probe"]["overall"], "by_type": one["probe"]["by_type"],
                          "band": one["probe"]["band"]}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"pools": {n: p["probe"]["overall"] for n, p in per_pool.items()},
                          "delta": ev["delta_v2_minus_v1"]["overall"],
                          "verdict": {"suitable": ev["verdict"]["v2_suitable_for_rl"],
                                      "dominant": ev["verdict"]["dominant_failure_class"]}},
                         ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
