#!/usr/bin/env python3
"""S3h — PPL-проба наборов GEN-EVAL (v1 и v2) на **локальной** машине.

Зачем проба. Прибор GEN-EVAL (`_ppl_eval` пайплайна) считает перплексию на двух
наборах: `general_eval.txt` / `domain_eval.txt` (v1) — по ним шёл пилот и по ним
же выведены полосы ADR-015 (внимание 1.5× / эскалация 2× от базы 127.5). Наборы
v2 (ADR-018, по 200 документов) собраны под фазу 2, но **базовая линия под них не
измерена**: без неё «+40 % к базе» неотличимо от «другой набор». Проба даёт эти
числа, не занимая стенд GB10.

Что переносится, а что нет. Переносятся **свойства данных** — PPL базовой модели
на конкретном наборе (смена набора смещает шкалу прибора, и порог, выведенный по
одной шкале, на другой означает другое). **Не** переносятся производительность,
время, память: 4080 — не GB10, и ни одно число отсюда не является замером стенда.
Поэтому в отчёт пишется машина замера, а числа называются «PPL набора», не «PPL
модели на стенде».

Методика повторяет `laguna_pipeline_v8.py:1556-1581` **дословно** — иначе числа
несопоставимы с историей:

* разбор: `text.split("\\n---\\n")`, `strip`, фильтр `len(doc) > 50`;
* токенизация: `tokenizer.encode(doc, add_special_tokens=False)[:1024]`, документ
  доживает до счёта при `len(enc) > 8`;
* батч 4, right-padding до максимума в батче, `attention_mask` по реальной длине;
* `log_softmax(logits[:, :-1].float())`, цель — `input_ids[:, 1:]`, маска —
  `attn[:, 1:]`; последний токен документа цели не имеет (предсказывать нечего);
* **PPL корпуса** = `exp(Σ nll / Σ токенов)` — по всем документам сразу, а не
  среднее по документам.

Среднее по документам тоже считается (median/mean по документу) и печатается
рядом: на v1 домен — это 5 документов, и расхождение «корпус vs документ»
показывает, насколько одна точка прибора зависит от одного документа. Обе
величины берутся из **одного** forward'а: суммы nll и счётчики токенов
разделяются по строкам батча, поэтому документная статистика не является вторым
(и чуть другим) замером.

Дефекты окружения этой машины. Окружение **не меняется** (AD-4: пакеты стенда и
машины read-only), поэтому два дефекта обходятся заглушками в ``sys.modules``
текущего процесса, и оба обхода записываются в отчёт (`env_shims`):

1. `transformers 4.44.2` падает на импорте из-за `huggingface-hub 1.27.0`
   (жёсткая верхняя граница `<1.0` в `dependency_versions_check`). Снимается
   **только проверка версии**; что API загрузки локальных весов/токенизатора в
   hub 1.x работает, подтверждается не импортом, а числом — замером на модели,
   чей лосс известен из лога стенда (см. `BASE_ANCHOR`), плюс согласием bf16 и
   fp32 между собой;
2. `accelerate` на импорте тянет `boto3` → `botocore` → `urllib3.contrib.pyopenssl`
   → `OpenSSL`, который падает на установленной `cryptography`
   (`module 'lib' has no attribute 'GEN_EMAIL'`). Заглушка ставится на самый
   нижний сломанный модуль (`urllib3.contrib.pyopenssl` — необязательный
   TLS-бэкенд botocore), а не на `boto3`: иначе `is_boto3_available()` в
   accelerate падает уже на подделке (`boto3.__spec__ is None`).

Коды возврата::

    0 — замер сделан, evidence записан (в т.ч. когда расхождение с известной базой
        объяснено провенансом якоря — см. ``reproduction``)
    1 — отказ: расхождение с известной базой выше порога и объяснить его нечем
    2 — NOT-VERIFIED: вход отсутствует (набор/веса/токенизатор) или нет CUDA

Запуск::

    python3 tools/ppl_probe.py --plan                # что и как будет измерено
    python3 tools/ppl_probe.py                       # 4 набора × {base, instruct}
    python3 tools/ppl_probe.py --models base         # только базовая модель
    python3 tools/ppl_probe.py --dtype bfloat16,float32   # + проверка чувствительности
    python3 tools/ppl_probe.py --json                # машинный отчёт в stdout

Ожидаемое время: единицы минут (0.5B, только forward, ≈430 тыс. токенов на v2).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
import types
from datetime import datetime
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Наборы GEN-EVAL. v1 — то, по чему считался пилот (ADR-015); v2 — прибор фазы 2
#: (ADR-018, по 200 документов). Пути — через симлинк кейса (AD-4: данные лежат на
#: сетевом диске, в кейсе только ссылки; проба их **только читает**).
SETS: dict[str, str] = {
    "v1_general": "datasets/general_eval.txt",
    "v1_domain": "datasets/domain_eval.txt",
    "v2_general": "datasets/general_eval_v2.txt",
    "v2_domain": "datasets/domain_eval_v2.txt",
}

#: Порог фильтра документов — как в ``_ppl_eval``: ``len(doc.strip()) > 50``.
MIN_DOC_CHARS = 50
#: Порог доживания до счёта — как в ``_ppl_eval``: ``len(enc) > 8``.
MIN_DOC_TOKENS = 8

DEFAULT_MAX_LEN = 1024
DEFAULT_BATCH = 4

#: Веса базовой модели (Qwen2.5-0.5B, ADR-002). Локальный HF-кэш содержит у этой
#: ревизии только токенизатор, поэтому веса берутся из хранилища на сетевом диске.
BASE_WEIGHTS_CANDIDATES = [
    "/home/user/gb10-shared/models-store/experiments/kda-graft/models/qwen2.5-0.5b-base",
]
#: Токенизатор: сначала локальный HF-кэш, затем снапшот на сетевом диске.
TOKENIZER_CANDIDATES = [
    "~/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B",
    "/home/user/gb10-shared/models-store/home-roman/huggingface-cache/hub/models--Qwen--Qwen2.5-0.5B",
]
#: Instruct-вариант (ADR-002: берётся как «вторая модель» для разброса между
#: моделями). В локальном кэше есть и веса, и токенизатор.
INSTRUCT_CANDIDATES = [
    "~/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B-Instruct",
    "/home/user/gb10-shared/models-store/home-roman/huggingface-cache/hub/models--Qwen--Qwen2.5-0.5B-Instruct",
]

#: Известная база из ``evidence/s2-smoke.json`` (``forgetting_ppl``, ``step 0``) —
#: обвязка S2 на стенде GB10, модель ``Qwen/Qwen2.5-0.5B``, наборы v1.
KNOWN_BASE = {
    "source": "evidence/s2-smoke.json → forgetting_ppl[step=0]",
    "model": "Qwen/Qwen2.5-0.5B",
    "sets": "v1",
    "ppl_general": 127.50231470270732,
    "ppl_domain": 11.75640733743271,
    "note": "замер обвязки S2 на GB10 (bf16); ADR-015 цитирует его как 127.5 / 11.76",
}
#: Порог «воспроизвелось». Расхождение допускается только от арифметики другой
#: карты (bf16 на sm_89 против sm_121) и это доли процента; 5 % — уже не округление,
#: а другая методика или другой набор.
REPRO_TOLERANCE_PCT = 5.0

#: Провенанс известного якоря 127.5 / 11.76. Строка взята из лога **стадии SFT**,
#: и перед ней стоит загрузка CPT-чекпойнта: то есть число снято не с базовой
#: модели, а с состояния после CPT (в логе CPT первое же измерение, шаг 10, даёт
#: 126.9). Один и тот же набор проверяется на это арифметикой, а не на слово.
ANCHOR_PROVENANCE = {
    "value": "127.5 / 11.76",
    "file": "runs/smoke-20260914-0930/logs/sft.log",
    "line": "06:30:29 [INFO] GEN-EVAL step 0: ppl_general=127.5 (+0.0% vs base), ppl_domain=11.8 (+0.0%)",
    "preceding_line": "06:30:01 [INFO] Loaded CPT ckpt",
    "why_it_is_not_base": (
        "«step 0» — первый вызов GEN-EVAL в процессе стадии SFT, а перед ним "
        "загружен CPT-чекпойнт (laguna_pipeline_v8.py:806-809); в логе CPT первое "
        "измерение (шаг 10) уже 126.9. Число относится к состоянию после CPT, не "
        "к исходной модели — в ADR-015 оно записано как «PPL базовой модели»"),
}

#: Независимый якорь базовой модели, снятый **самим стендом**: на шаге 0 CPT
#: learning rate равен нулю (wsd_schedule(0, ...) = 0), то есть лосс посчитан на
#: нетронутых весах. Это единственное место в артефактах, где базовая модель
#: измерена стендом. Корпус другой (CPT-корпус, не domain_eval), поэтому
#: сравнение с нашим замером — по порядку величины, а не по процентам.
BASE_ANCHOR = {
    "source": "runs/smoke-20260914-0930/logs/cpt.log",
    "line": "06:27:01 [INFO] CPT step 0/50 | loss=2.0989 | lr=0.00e+00 | tok/s=1984",
    "loss": 2.0989,
    "implied_ppl": math.exp(2.0989),
    "corpus": "cpt_corpus_v12r (доменный CPT-корпус, тот же класс текста, что domain_eval)",
    "why_valid": "lr=0.00e+00 на шаге 0 → веса нетронуты, это замер базовой модели",
}
#: Полоса сравнения с якорем: корпуса разные, поэтому «в разы», а не «в процентах».
#: Отношение PPL нашего замера домена к PPL якоря должно попасть в [1/2, 2].
BASE_ANCHOR_BAND = 2.0

#: Тот же якорь, но снятый **на своём корпусе**. ``BASE_ANCHOR`` сравнивает наш
#: ``domain_eval`` с лоссом стенда на CPT-корпусе — это «по порядку величины»,
#: потому что корпуса разные. Здесь корпус один и тот же: пакованные чанки, на
#: которых стенд реально считал ``CPT step 0`` (``laguna_pipeline_v8.py:1556``
#: их не читает, их читает ``CPTDataset``; лосс — тот же ``model(labels=)``).
#: Так сверка становится распределением, а не сопоставлением порядков.
CPT_PACK_CANDIDATES = [
    "/home/user/gb10-shared/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy",
    "~/gb10-shared/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy",
]
#: Траектория лосса CPT-стадии смоука: в ней видно, что прогон, с которого снято
#: 127.5, разошёлся (2.0989 → 4.66 на первых шагах), а не «чуть сдвинулся».
CPT_PROBE_LOG_CANDIDATES = [
    "runs/smoke-20260914-0930/probe_cpt.jsonl",
]
#: Сколько чанков мерять. 128 чанков × 8192 токенов ≈ 1 млн токенов и ≈ 50 с на
#: 4080 — этого хватает, чтобы увидеть размах лосса по корпусу (std ≈ 0.17) и
#: отличить «нашлась трудная пачка» от «модель ушла из режима».
CORPUS_ANCHOR_CHUNKS = 128
#: Позиций за один проход ``lm_head``: 512 × 151936 × 4 Б ≈ 0.3 ГБ — влезает
#: рядом с активациями чанка, и нарезка при этом крупная (накладных мало).
CORPUS_ANCHOR_SLICE = 512
#: Лосс CPT-шага 0 стенда: ``lr=0.00e+00``, то есть веса нетронуты — это
#: единственное место в артефактах, где стенд измерил базовую модель.
STAND_CPT_STEP0_LOSS = 2.0989
#: Уровень, которым проверяется гипотеза «после CPT модель хуже базовой на своём
#: же корпусе»: у базы на 128 чанках лосс не превышал 2.51 (см. evidence).
BASE_REGIME_LEVEL = 2.6

#: Спецтокены контура (`laguna_pipeline_v8.py:38`). Пайплайн добавляет их
#: токенизатору и расширяет embeddings **до** любого замера PPL, поэтому для
#: сверки со стендом ту же настройку надо повторить: иначе различие «мой замер
#: против стенда» спишут на неё, а не на измеряемое.
SPECIAL_TOKENS = ["<think>", "</think>", "<tool_call>", "</tool_call>",
                  "<tool_response>", "</tool_response>", "<reasoning>", "</reasoning>"]


# ─────────────────────────── разбор набора (чистые функции) ──────────────────


def split_docs(text: str) -> list[str]:
    """Документы набора — ровно как ``_ppl_eval``: разделитель ``\\n---\\n``.

    Разделитель — именно эта тройка символов: ``---`` в другом окружении
    (например, как подчёркивание заголовка) документ не разрезает. Фильтр
    ``len(d.strip()) > 50`` применяется к **уже обрезанной** строке, поэтому
    документ, который становится короче порога после ``strip``, отсекается.
    """
    return [d.strip() for d in text.split("\n---\n") if len(d.strip()) > MIN_DOC_CHARS]


def truncate_ids(ids: list[int], max_len: int = DEFAULT_MAX_LEN) -> list[int]:
    """Обрезка до ``max_len`` — как ``tokenizer.encode(doc)[:max_len]``."""
    return ids[:max_len]


def is_counted(ids: list[int]) -> bool:
    """Доживает ли документ до счёта — как ``if len(enc) > 8``."""
    return len(ids) > MIN_DOC_TOKENS


def predicted_tokens(n_ids: int) -> int:
    """Сколько позиций документа дают вклад в nll.

    Сдвиг «предсказание против следующего токена» убирает последний токен:
    предсказывать после него нечего. В ``_ppl_eval`` это выражено маской
    ``attn[:, 1:]``, и счётчик строки равен ровно ``len(enc) - 1`` — функция
    повторяет это число, чтобы его можно было проверить тестом, а не глазами.
    """
    return max(n_ids - 1, 0)


def batch_chunks(items: list, batch: int):
    """Батчи по ``batch`` подряд — как ``for i in range(0, len(ids), batch)``."""
    for i in range(0, len(items), batch):
        yield items[i:i + batch]


def corpus_ppl(nll_sums: list[float], tok_counts: list[int]) -> float:
    """PPL корпуса = ``exp(Σ nll / Σ токенов)`` — формула ``_ppl_eval``.

    Взвешивание по токенам, а не по документам: длинный документ входит
    пропорционально своей длине. Среднее по документам — другая величина, и
    подменять её нельзя (сильно короткие документы перевесили бы).
    """
    total_nll = float(sum(nll_sums))
    total_tok = int(sum(tok_counts))
    return math.exp(total_nll / max(total_tok, 1))


def doc_ppl_stats(nll_sums: list[float], tok_counts: list[int]) -> dict:
    """Разброс прибора: PPL каждого документа и его сводка.

    Нужен там, где корпусная PPL молчит о зависимости от одного документа
    (v1 домен — 5 документов).
    """
    if not nll_sums:
        return {"n": 0, "median": None, "mean": None, "min": None, "max": None,
                "stdev": None, "max_over_median": None}
    ppls = [math.exp(n / max(t, 1)) for n, t in zip(nll_sums, tok_counts)]
    med = statistics.median(ppls)
    return {
        "n": len(ppls),
        "median": med,
        "mean": statistics.fmean(ppls),
        "min": min(ppls),
        "max": max(ppls),
        "stdev": statistics.stdev(ppls) if len(ppls) > 1 else 0.0,
        "max_over_median": (max(ppls) / med) if med else None,
    }


def read_dataset(path: Path) -> dict:
    """Прочитать набор и снять его опознавательные знаки.

    ``sha256`` обязателен: наборы v2 в момент пробы пересобираются под 200
    документов, и без хеша число не привязано к версии файла.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    parts = text.split("\n---\n")
    docs = split_docs(text)
    return {
        "path": str(path),
        "resolved": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "raw_parts": len(parts),
        "docs_kept": len(docs),
        "docs_dropped_short": len(parts) - len(docs),
        "docs": docs,
    }


# ─────────────────── якорь корпуса стенда (чистые функции) ───────────────────


def parse_probe_losses(text: str) -> dict:
    """Лоссы шагов из ``probe_cpt.jsonl`` — по объекту JSON на строку.

    Строки без ``loss`` (события обвязки) и неразобранные строки считаются
    **раздельно**: иначе «шагов не нашлось» и «файл не прочитался» выглядели бы
    одинаково, а это разные диагнозы. Пустой результат — не ошибка чтения, а
    отсутствие данных, и в evidence он виден как ``n = 0``.
    """
    losses, no_loss, unparsed = [], 0, 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            unparsed += 1
            continue
        if isinstance(rec, dict) and isinstance(rec.get("loss"), (int, float)):
            losses.append(float(rec["loss"]))
        else:
            no_loss += 1
    return {"losses": losses, "skipped_without_loss": no_loss, "unparsed": unparsed}


def loss_stats(losses: list[float]) -> dict:
    """Сводка по траектории лосса: где она была и куда пришла.

    ``stdev`` здесь — не украшение: у базовой модели по чанкам лосс гуляет с
    std ≈ 0.17 (разная трудность пачек), и без этой величины «разброс 2.1…4.7»
    нельзя отличить от «попались трудные пачки».
    """
    if not losses:
        return {"n": 0, "mean": None, "stdev": None, "min": None, "max": None,
                "first": None, "last": None}
    return {
        "n": len(losses),
        "mean": statistics.fmean(losses),
        "stdev": statistics.stdev(losses) if len(losses) > 1 else 0.0,
        "min": min(losses),
        "max": max(losses),
        "first": losses[0],
        "last": losses[-1],
    }


def above_level(losses: list[float], level: float) -> dict:
    """Сколько шагов вышло за уровень — доля, а не «есть/нет».

    Замер, который ни разу не покинул режим базы, и замер, который покидал его
    на каждом втором шаге, — разные диагнозы, и различает их доля.
    """
    n = len(losses)
    hits = sum(1 for x in losses if x >= level)
    return {"level": level, "count": hits, "n": n,
            "fraction": (hits / n) if n else None}





def import_transformers():
    """``import transformers`` с обходом жёсткой верхней границы на hub.

    Окружение не меняется: заглушка живёт только в ``sys.modules`` этого
    процесса и снимает **проверку версии**, а не сам импорт huggingface-hub.
    Если установленные версии совместимы, функция не делает ничего.
    """
    try:
        import transformers  # noqa: F401
        return transformers, None
    except ImportError as exc:
        if "huggingface-hub" not in str(exc):
            raise
        reason = str(exc).strip().splitlines()[-1]
    stub = types.ModuleType("transformers.dependency_versions_check")
    stub.pkgs_to_check_at_runtime = []
    for name in ("dep_version_check", "require_version", "require_version_core"):
        setattr(stub, name, lambda *a, **k: None)
    sys.modules["transformers.dependency_versions_check"] = stub
    import transformers  # noqa: F401
    return transformers, f"проверка версий transformers снята: {reason}"


def repair_broken_pyopenssl() -> str | None:
    """Заглушить ``urllib3.contrib.pyopenssl``, если он неимпортируем.

    Он неимпортируем не сам по себе: он тянет ``OpenSSL`` (pyOpenSSL), который
    падает на установленной ``cryptography`` — ``module 'lib' has no attribute
    'GEN_EMAIL'``. Цепочка, которая из-за этого рушится: ``accelerate`` →
    ``boto3`` → ``botocore.httpsession`` → ``urllib3.contrib.pyopenssl``, то есть
    ломается импорт модели, хотя питоновский TLS-бэкенд у нас не используется.

    Заглушка ставится на **самый нижний** сломанный модуль, а не на ``boto3``:
    так ``boto3``/``botocore`` остаются настоящими (accelerate проверяет их
    наличие через ``find_spec``, и подделка верхнего уровня сломала бы уже эту
    проверку). ``botocore`` берёт отсюда только ``extract_from_urllib3`` /
    ``inject_into_urllib3`` / ``orig_util_SSLContext`` — все три объявлены.
    """
    try:
        import boto3  # noqa: F401
        return None
    except Exception as exc:  # noqa: BLE001 — нужен любой отказ импорта
        first = f"{type(exc).__name__}: {exc}"
    shim = types.ModuleType("urllib3.contrib.pyopenssl")
    shim.extract_from_urllib3 = lambda: None
    shim.inject_into_urllib3 = lambda: None
    shim.orig_util_SSLContext = None
    sys.modules.setdefault("urllib3.contrib.pyopenssl", shim)
    try:
        import boto3  # noqa: F401,F811 — повтор после подмены, это и есть проверка
    except Exception as exc:  # noqa: BLE001
        return (f"boto3 неимпортируем и после подмены urllib3.contrib.pyopenssl: "
                f"сначала {first}, затем {type(exc).__name__}: {exc}")
    return (f"urllib3.contrib.pyopenssl подменён заглушкой (pyOpenSSL сломан: {first}) — "
            f"необязательный TLS-бэкенд botocore, в пути пробы не используется")


def first_existing(paths: list[str]) -> tuple[Path | None, list[str]]:
    """Первый существующий **файл** из кандидатов; рядом — список отказов.

    ``locate`` ищет каталог по маркеру внутри, для одиночных файлов (логи,
    ``.npy``) нужен этот вариант. Отказы возвращаются, чтобы NOT-VERIFIED
    называл путь, которого не нашёл, а не просто «нет данных».
    """
    missed = []
    for p in paths:
        cand = Path(os.path.expanduser(p))
        if cand.is_file():
            return cand, missed
        missed.append(str(cand))
    return None, missed


def locate(paths: list[str], marker: str) -> tuple[Path | None, list[str]]:
    """Первый существующий кандидат, содержащий ``marker``; и список отказов."""
    missed = []
    for p in paths:
        cand = Path(os.path.expanduser(p))
        # Снапшот HF-кэша: сама ревизия лежит в подкаталоге snapshots/<hash>.
        snaps = sorted((cand / "snapshots").glob("*")) if (cand / "snapshots").is_dir() else []
        for use in ([snaps[-1]] if snaps else []) + [cand]:
            if (use / marker).exists():
                return use, missed
        missed.append(f"{cand}: нет {marker}")
    return None, missed


# ─────────────────────────── настройка как в пайплайне ───────────────────────


def init_special_tokens_subtoken(model, tokenizer) -> None:
    """Копия ``laguna_pipeline_v8.py:620-629``: спецтокен получает среднее своих
    субтокенов. Копия, а не импорт: пайплайн — монолит стенда (vLLM, пути
    ``/workspace``), импортировать его на локальной машине нечем."""
    import torch  # noqa: F401

    emb = model.get_input_embeddings().weight.data
    for token_str in SPECIAL_TOKENS:
        subtokens = tokenizer.encode(token_str, add_special_tokens=False)
        if subtokens:
            mean_emb = emb[subtokens].mean(dim=0)
            for tid in tokenizer.encode(token_str, add_special_tokens=True):
                if tid >= len(tokenizer) - len(SPECIAL_TOKENS):
                    emb[tid] = mean_emb


def apply_pipeline_tokenizer(model, tokenizer) -> dict:
    """Повторить настройку токенизатора и embeddings из ``main()`` пайплайна.

    ``laguna_pipeline_v8.py:1831`` добавляет 8 спецтокенов, ``:1851`` расширяет
    embeddings, ``init_special_tokens_subtoken`` инициализирует новые строки.
    На PPL русского текста это влиять не должно (новые токены в наборах не
    встречаются) — но «не должно» здесь проверяется замером: контроль
    ``--pipeline-tokenizer off`` даёт ту же величину без этой настройки.
    """
    before = len(tokenizer)
    tokenizer.add_special_tokens({"additional_special_tokens": SPECIAL_TOKENS})
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model.resize_token_embeddings(len(tokenizer))
    init_special_tokens_subtoken(model, tokenizer)
    return {"special_tokens": SPECIAL_TOKENS, "vocab_before": before,
            "vocab_after": len(tokenizer)}


# ─────────────────────────── замер ───────────────────────────────────────────


def measure(model, tokenizer, docs: list[str], max_len: int, batch: int, device: str,
            cross_check: bool = True) -> dict:
    """PPL набора по документам — батчами, ровно как ``_ppl_eval``.

    ``model`` ожидается в ``eval()``, вызов — под ``torch.no_grad()`` (вызывает
    ``run``; сюда модель приходит готовой, чтобы тесты могли подать заглушку).
    """
    import torch
    import torch.nn.functional as F

    ids, dropped_tokens = [], 0
    for d in docs:
        enc = truncate_ids(tokenizer.encode(d, add_special_tokens=False), max_len)
        if is_counted(enc):
            ids.append(enc)
        else:
            dropped_tokens += 1

    nll_sums: list[float] = []
    tok_counts: list[int] = []
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else 0
    mask_ok = True
    for chunk in batch_chunks(ids, batch):
        ml = max(len(c) for c in chunk)
        input_ids = torch.full((len(chunk), ml), pad_id, dtype=torch.long, device=device)
        attn = torch.zeros((len(chunk), ml), dtype=torch.long, device=device)
        for j, c in enumerate(chunk):
            input_ids[j, :len(c)] = torch.tensor(c, dtype=torch.long, device=device)
            attn[j, :len(c)] = 1
        logits = model(input_ids=input_ids, attention_mask=attn).logits
        sl = F.log_softmax(logits[:, :-1].float(), dim=-1)
        tgt = input_ids[:, 1:]
        m = attn[:, 1:].bool()
        nll = -sl.gather(-1, tgt.unsqueeze(-1)).squeeze(-1)
        # Счётчик позиций строки обязан равняться len(enc)-1: иначе разъехалась
        # семантика маски, и документная статистика считала бы другое, чем корпус.
        want = sum(predicted_tokens(len(c)) for c in chunk)
        mask_ok = mask_ok and int(m.sum().item()) == want
        nll_sums += nll.masked_fill(~m, 0.0).sum(dim=1).tolist()
        tok_counts += [int(v) for v in m.sum(dim=1).tolist()]
        del logits, sl, nll
    if device.startswith("cuda"):
        torch.cuda.empty_cache()

    return {
        "ppl": corpus_ppl(nll_sums, tok_counts),
        "docs_counted": len(ids),
        "docs_dropped_short_tokens": dropped_tokens,
        "tokens": int(sum(tok_counts)),
        "doc_ppl": doc_ppl_stats(nll_sums, tok_counts),
        "mask_check": mask_ok,
        "cross_check": _cross_check(model, ids, nll_sums, tok_counts, device) if cross_check else None,
    }


def _cross_check(model, ids: list[list[int]], nll_sums: list[float], tok_counts: list[int],
                 device: str) -> dict:
    """Проверка батчевого пути против одиночного forward'а без padding.

    Первый документ считается ещё раз — одним тензором и через ``labels=``, где
    сдвиг «предсказание против следующего токена» делает сам transformers. Если
    маска и сдвиг в батчевом пути верны, обе величины совпадают (расхождение —
    только арифметика другого размера матрицы). Это проверка **своего** кода:
    при расхождении числам пробы верить нельзя, и это видно в evidence, а не
    выясняется при разборе.
    """
    import torch

    if not ids:
        return {"status": "нет документов"}
    first = torch.tensor([ids[0]], dtype=torch.long, device=device)
    loss = model(input_ids=first, labels=first).loss.item()
    batched = math.exp(nll_sums[0] / max(tok_counts[0], 1))
    return {
        "doc_index": 0,
        "n_tokens": len(ids[0]),
        "batched_ppl": batched,
        "unbatched_ppl": math.exp(loss),
        "rel_delta": (batched - math.exp(loss)) / math.exp(loss),
    }


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """sha256 потоком — файлы наборов и чанков бывают в сотни МБ."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _ce_sliced(model, ids, slice_size: int) -> float:
    """Shifted CE по чанку, но ``lm_head`` применяется срезами позиций.

    Зачем срезы. Полные логиты пакованного чанка [1, 8192, 151936] — это ≈5 ГБ
    в float32, и ровно так считает лосс сам transformers (на стенде с 121 ГБ
    unified memory это проходит). В 16 ГБ карты такой forward падает по памяти,
    а уменьшать чанк нельзя: он должен остаться тем же, что тренировал стенд.
    Скрытые состояния чанка при этом крошечные (8192 × 896 × 2 Б ≈ 15 МБ),
    поэтому ``lm_head`` применяется по ``slice_size`` позиций за раз. Лосс от
    нарезки не меняется — это проверяется контролем против ``model(labels=)``
    на коротком отрезке (см. ``corpus_anchor["control"]``), а не принимается
    на веру.
    """
    import torch

    length = ids.shape[1]
    hidden = model.model(input_ids=ids).last_hidden_state
    total_nll, total_tok = 0.0, 0
    for start in range(0, length - 1, slice_size):
        stop = min(start + slice_size, length - 1)
        logits = model.lm_head(hidden[:, start:stop, :]).float()
        target = ids[:, start + 1:stop + 1]
        total_nll += torch.nn.functional.cross_entropy(
            logits.reshape(-1, logits.shape[-1]), target.reshape(-1),
            reduction="sum").item()
        total_tok += target.numel()
        del logits
    return total_nll / max(total_tok, 1)


def corpus_anchor(model, path: Path, n_chunks: int, slice_size: int, device: str,
                  control_tokens: int = 512) -> dict:
    """Лосс базовой модели на **своём** корпусе стенда — тем же счётом.

    Зачем это отдельно от ``BASE_ANCHOR``. Тот якорь снят на CPT-корпусе, а наш
    замер — на ``domain_eval``: сравнивать их можно только «по порядку величины».
    Здесь набор тот же самый, на котором стенд получил ``CPT step 0 | loss=2.0989
    | lr=0.00e+00``, поэтому сверка становится числом против распределения:
    попадает ли лосс стенда в разброс базовой модели по её же чанкам.

    Читается через ``mmap_mode="r"`` — файл на сетевом диске, копий не делаем
    (AD-4), и берём первые ``n_chunks``, а не весь корпус.
    """
    import numpy as np
    import torch

    started = time.time()
    pack_sha = sha256_file(path)
    chunks = np.load(str(path), mmap_mode="r")
    n = min(n_chunks, chunks.shape[0])

    losses = []
    with torch.no_grad():
        for i in range(n):
            ids = torch.from_numpy(
                np.asarray(chunks[i], dtype=np.int64)).to(device).unsqueeze(0)
            losses.append(_ce_sliced(model, ids, slice_size))
            del ids

    # Контроль арифметики нарезки: на коротком отрезке полные логиты влезают,
    # поэтому там же сверяемся с лоссом самого transformers.
    control = None
    with torch.no_grad():
        short = torch.from_numpy(
            np.asarray(chunks[0][:control_tokens], dtype=np.int64)).to(device).unsqueeze(0)
        hf_loss = model(input_ids=short, labels=short.clone()).loss.item()
        sliced = _ce_sliced(model, short, slice_size)
    control = {"n_tokens": int(short.shape[1]), "hf_loss": hf_loss,
               "sliced_loss": sliced, "abs_delta": abs(hf_loss - sliced)}

    stats = loss_stats(losses)
    return {
        "what": ("лосс базовой модели на тех же пакованных чанках, на которых стенд "
                 "снял CPT step 0 (lr=0) — сверка на одном корпусе, а не «по порядку»"),
        "pack": str(path),
        "pack_sha256": pack_sha,
        "pack_shape": list(chunks.shape),
        "n_chunks": n,
        "chunk_tokens": int(chunks.shape[1]),
        "slice": slice_size,
        "tokens_measured": n * int(chunks.shape[1]),
        "base_loss": stats,
        "base_ppl": math.exp(stats["mean"]) if stats["mean"] else None,
        "stand_step0_loss": STAND_CPT_STEP0_LOSS,
        "stand_step0_within_base_range": (
            stats["min"] is not None
            and stats["min"] <= STAND_CPT_STEP0_LOSS <= stats["max"]),
        "stand_step0_z": (None if not stats["stdev"] else
                          (STAND_CPT_STEP0_LOSS - stats["mean"]) / stats["stdev"]),
        "chunks_above_base_regime_level": above_level(losses, BASE_REGIME_LEVEL),
        "control": control,
        "seconds": round(time.time() - started, 3),
    }


def load_model(model_key: str, dtype_name: str, device: str, pipeline_tokenizer: bool):
    """Загрузить модель и токенизатор указанной ревизии. ``None`` — NOT-VERIFIED."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if model_key == "base":
        weights, missed_w = locate(BASE_WEIGHTS_CANDIDATES, "model.safetensors")
        if weights is None:
            return None, None, None, ["веса base не найдены: " + "; ".join(missed_w)]
        # У этой ревизии в кэше только токенизатор, веса — рядом с ним (ADR-004).
        tok_dir, missed_t = locate(TOKENIZER_CANDIDATES, "tokenizer.json")
        if tok_dir is None:
            return None, None, None, ["токенизатор не найден: " + "; ".join(missed_t)]
        model_dir, tok_path = weights, tok_dir
    else:
        model_dir, missed_w = locate(INSTRUCT_CANDIDATES, "model.safetensors")
        if model_dir is None:
            return None, None, None, ["веса instruct не найдены: " + "; ".join(missed_w)]
        tok_path = model_dir

    dtype = {"bfloat16": torch.bfloat16, "float32": torch.float32}[dtype_name]
    tokenizer = AutoTokenizer.from_pretrained(str(tok_path), local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(model_dir), torch_dtype=dtype, local_files_only=True).to(device).eval()
    tok_setup = None
    if pipeline_tokenizer:
        tok_setup = apply_pipeline_tokenizer(model, tokenizer)
    prov = {"weights": str(model_dir), "tokenizer": str(tok_path),
            "tokenizer_setup": "pipeline (8 спецтокенов + resize)" if pipeline_tokenizer
                               else "как есть (без спецтокенов пайплайна)"}
    if tok_setup:
        prov.update(tok_setup)
    return model, tokenizer, prov, []


def run(args) -> tuple[dict, int]:
    """Прогон пробы. Возвращает (evidence, код возврата)."""
    shims: list[str] = []
    shim = repair_broken_pyopenssl()
    if shim:
        shims.append(shim)
    try:
        transformers, tshim = import_transformers()
    except ImportError as exc:
        print(f"NOT-VERIFIED: transformers не импортируется: {exc}", file=sys.stderr)
        return {}, 2
    if tshim:
        shims.append(tshim)
    from transformers import AutoModelForCausalLM  # noqa: F401  (проверка доступности)
    import torch

    if args.device.startswith("cuda") and not torch.cuda.is_available():
        print("NOT-VERIFIED: CUDA недоступна — замер PPL нечем сделать", file=sys.stderr)
        return {}, 2

    wanted_sets = list(SETS) if args.sets == "all" else args.sets.split(",")
    unknown = [s for s in wanted_sets if s not in SETS]
    if unknown:
        print(f"NOT-VERIFIED: неизвестные наборы: {unknown}", file=sys.stderr)
        return {}, 2

    datasets, missing = {}, []
    for name in wanted_sets:
        path = CASE_ROOT / SETS[name]
        if not path.is_file():
            missing.append(f"{name}: {path} не найден")
            continue
        datasets[name] = read_dataset(path)
    if missing:
        print("NOT-VERIFIED: " + "; ".join(missing), file=sys.stderr)
        return {}, 2

    dtypes = args.dtype.split(",")
    models = ["base", "instruct"] if args.models == "all" else args.models.split(",")
    variants = ({"on": True, "off": False} if args.pipeline_tokenizer == "both"
                else {args.pipeline_tokenizer: args.pipeline_tokenizer == "on"})
    results: dict = {m: {} for m in models}
    anchors: dict = {}
    base_dtype = dtypes[0]
    started = time.time()
    for model_key in models:
        for dtype_name in dtypes:
            if model_key != "base" and dtype_name != base_dtype:
                # Чувствительность к dtype снимается на базовой модели: это её
                # свойство как эталона, а не отдельный вопрос про instruct.
                continue
            for variant, use_pipeline_tok in variants.items():
                if variant == "off" and (model_key != "base" or dtype_name != base_dtype):
                    # Контроль «без спецтокенов пайплайна» — только на эталоне.
                    continue
                model, tokenizer, prov, errs = load_model(
                    model_key, dtype_name, args.device, use_pipeline_tok)
                if model is None:
                    print("NOT-VERIFIED: " + "; ".join(errs), file=sys.stderr)
                    return {}, 2
                for name in wanted_sets:
                    t0 = time.time()
                    with torch.no_grad():
                        res = measure(model, tokenizer, datasets[name]["docs"],
                                      args.max_len, args.batch, args.device,
                                      cross_check=args.cross_check)
                    res.update({
                        "model": model_key,
                        "dtype": dtype_name,
                        "tokenizer_variant": variant,
                        "weights": prov["weights"],
                        "tokenizer": prov["tokenizer"],
                        "tokenizer_setup": prov["tokenizer_setup"],
                        "max_len": args.max_len,
                        "batch": args.batch,
                        "device": args.device,
                        "seconds": round(time.time() - t0, 3),
                    })
                    results[model_key].setdefault(dtype_name, {}).setdefault(variant, {})[name] = res
                    print(f"  {model_key:8s} {dtype_name:10s} tok={variant:3s} {name:11s} "
                          f"ppl={res['ppl']:.4f} docs={res['docs_counted']} "
                          f"tok={res['tokens']} median={res['doc_ppl']['median']:.3f} "
                          f"({res['seconds']:.1f} с)", flush=True)
                # Якорь на своём корпусе стенда снимается один раз — на эталонной
                # комбинации (база, bf16, настройка токенизатора пайплайна):
                # это свойство эталона, а не отдельный вопрос про instruct.
                if (args.corpus_anchor and model_key == "base"
                        and dtype_name == base_dtype and variant == "on"):
                    pack, missed_pack = first_existing(CPT_PACK_CANDIDATES)
                    if pack is None:
                        anchors["corpus"] = {
                            "status": "NOT-VERIFIED",
                            "reason": "пакованные чанки стенда не найдены: "
                                      + "; ".join(missed_pack),
                        }
                    else:
                        anchors["corpus"] = corpus_anchor(
                            model, pack, CORPUS_ANCHOR_CHUNKS, CORPUS_ANCHOR_SLICE,
                            args.device)
                        st = anchors["corpus"]["base_loss"]
                        print(f"  якорь корпуса: {anchors['corpus']['n_chunks']} чанков "
                              f"× {anchors['corpus']['chunk_tokens']} ток → лосс базы "
                              f"{st['mean']:.4f}±{st['stdev']:.4f} "
                              f"(стенд step 0: {STAND_CPT_STEP0_LOSS}, в разбросе: "
                              f"{anchors['corpus']['stand_step0_within_base_range']}) "
                              f"({anchors['corpus']['seconds']:.0f} с)", flush=True)
                del model
                if args.device.startswith("cuda"):
                    torch.cuda.empty_cache()

    evidence = build_evidence(args, datasets, results, shims, time.time() - started,
                              anchors=anchors)
    out = Path(args.out) if args.out else CASE_ROOT / "evidence" / "ppl-baseline-v1v2.json"
    if not out.is_absolute():
        out = CASE_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nevidence: {out}")
    return evidence, 1 if evidence["reproduction"]["verdict"] == "РАСХОЖДЕНИЕ БЕЗ ОБЪЯСНЕНИЯ" else 0


def build_evidence(args, datasets: dict, results: dict, shims: list[str],
                   seconds: float, anchors: dict | None = None) -> dict:
    """Собрать evidence по контракту S3h: числа + статистика + воспроизведение."""
    import torch
    import transformers

    base_dtype = args.dtype.split(",")[0]
    primary = results.get("base", {}).get(base_dtype, {}).get("on", {})
    ppl_block = {name: _flat(res, datasets[name]) for name, res in primary.items()}
    control = results.get("base", {}).get(base_dtype, {}).get("off", {})
    evidence = {
        "schema": "ppl-baseline-v1v2/1",
        "stage": "S3h",
        "status": "complete",
        "date": datetime.now().isoformat(timespec="seconds"),
        "purpose": ("Первые числа PPL наборов v1/v2 на базовой модели — опора для "
                    "калибровки полос ADR-015 (1.5× / 2×) под v2 и проверка, что "
                    "методика пайплайна воспроизводится вне стенда"),
        "stand": {
            "host": platform.node(),
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu",
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "note": ("локальная машина, НЕ стенд GB10: числа переносятся как свойства "
                     "наборов (PPL), а не как замеры производительности стенда"),
        },
        "instrument": {
            "source": "laguna_pipeline_v8.py:1556-1581 (_ppl_eval), методика повторена",
            "split": "text.split('\\n---\\n')",
            "filter": f"len(doc.strip()) > {MIN_DOC_CHARS}",
            "tokenize": f"tokenizer.encode(doc, add_special_tokens=False)[:{args.max_len}]",
            "keep": f"len(enc) > {MIN_DOC_TOKENS}",
            "batch": args.batch,
            "padding": "right-pad до максимума в батче, attention_mask по длине",
            "loss": "log_softmax(logits[:, :-1].float()) против input_ids[:, 1:], маска attn[:, 1:]",
            "ppl": "exp(Σ nll / Σ токенов) по корпусу",
            "tokenizer": ("токенизатор + 8 спецтокенов и resize embeddings, как в main() "
                          "пайплайна; контроль без них — блок tokenizer_control"),
            "doc_ppl": ("exp(nll_док / токены_док); median/mean/min/max по документам "
                        "из того же forward'а (разделение сумм по строкам батча)"),
        },
        "env_shims": shims,
        "datasets": {
            name: {k: v for k, v in ds.items() if k != "docs"}
            for name, ds in datasets.items()
        },
        "ppl": ppl_block,
        "tokenizer_control": {
            "what": ("тот же замер без спецтокенов/расширения embeddings пайплайна — "
                     "отделяет настройку токенизатора от состояния модели"),
            "sets": {name: _flat(res, datasets[name]) for name, res in control.items()},
            "delta_vs_ppl": {
                name: (control[name]["ppl"] / ppl_block[name]["ppl"] - 1.0) * 100.0
                for name in control if name in ppl_block
            },
        } if control else {"what": "контроль не снимался (--pipeline-tokenizer)", "sets": {}},
        "ppl_instruct": {
            dt: {name: _flat(res, datasets[name])
                 for name, res in results.get("instruct", {}).get(dt, {}).get("on", {}).items()}
            for dt in results.get("instruct", {})
        },
        "dtype_sensitivity": {
            dt: {name: _flat(res, datasets[name]) for name, res in variants_.get("on", {}).items()}
            for dt, variants_ in results.get("base", {}).items() if dt != base_dtype
        },
        "results_all": results,
        "seconds_total": round(seconds, 1),
        "artifacts": [
            {"path": "tools/ppl_probe.py", "what": "проба PPL (этот инструмент)"},
            {"path": "evidence/ppl-baseline-v1v2.json", "what": "числа пробы"},
            {"path": "evidence/s2-smoke.json", "what": "известная база 127.5/11.76 (v1, GB10)"},
            {"path": "runs/smoke-20260914-0930/probe_cpt.jsonl",
             "what": "траектория лосса CPT стенда — источник провенанса якоря"},
            {"path": "runs/smoke-20260914-0930/logs/cpt.log",
             "what": "CPT step 0 | lr=0.00e+00 — независимый якорь базовой модели"},
            {"path": "/home/user/gb10-shared/datasets/tok/cpt_corpus_v12r_8192_qwen25.npy",
             "what": "пакованные чанки CPT (только чтение) — сверка на одном корпусе"},
        ],
    }
    evidence["reproduction"] = reproduction(
        evidence["ppl"],
        corpus=(anchors or {}).get("corpus"),
        div=divergence((anchors or {}).get("corpus")),
    )
    evidence["v1_vs_v2_shift"] = shift(evidence["ppl"])
    tok_delta = evidence["tokenizer_control"].get("delta_vs_ppl", {})
    evidence["tokenizer_note"] = (
        "Настройка токенизатора пайплайна ничего не меняет на наборах v1 (в их тексте "
        "спецтокенов нет) и почти ничего на v2, кроме domain: там встречаются <think> и "
        "</think> (по одному разу), поэтому токенизация зависит от настройки "
        + (f"({', '.join(f'{k}: {v:+.4f} %' for k, v in tok_delta.items())})" if tok_delta else "")
        + ". Числа блока ppl сняты с настройкой пайплайна — она и есть эталон для сверки")
    evidence["assumptions"] = [
        "Методика _ppl_eval воспроизведена по коду пайплайна, а не по описанию: "
        "разбор, обрезка, батч и формула взяты из laguna_pipeline_v8.py:1556-1581",
        "dtype по умолчанию — bfloat16, как в пайплайне (BF16 = torch.bfloat16); "
        "чисел в float32 пайплайн не даёт",
        "Базовая модель — веса из models-store/.../qwen2.5-0.5b-base (ADR-002), "
        "токенизатор — локальный HF-кэш Qwen2.5-0.5B: ревизия та же, что в пайплайне",
        "Числа 4080 переносятся как свойства наборов; замером стенда GB10 они не являются",
        "Сверка на одном корпусе опирается на то, что пакованные чанки стенда "
        "(cpt_corpus_v12r_8192_qwen25.npy, sha256 в corpus_anchor) — те же самые, "
        "что читал CPTDataset: файл лежит на сетевом диске и пробой не копируется, "
        "поэтому связь подтверждается хешем и формой (9776 × 8192), а не происхождением",
        "Лосс CPT-шага 0 (2.0989) относится к базовой модели только потому, что "
        "wsd_schedule(0, ...) = 0: прогон на шаге 0 весов не менял",
    ]
    evidence["not_verified"] = [
        "Наборы v2 в момент пробы пересобираются: числа привязаны к sha256 в "
        "datasets — при новой сборке пробу надо повторить",
        "Перенос чисел на GB10 не проверен замером (сверка возможна только на стенде)",
        "Что 127.5 снято после CPT-чекпойнта — прочитано из логов S2 (sft.log: "
        "«Loaded CPT ckpt» перед строкой GEN-EVAL) и подтверждено лоссом стенда при "
        "lr=0; отдельного прогона «_ppl_eval на нетронутой базовой модели на стенде» "
        "в артефактах нет, и на локальной машине его не заменить",
        "Расхождение CPT-стадии измерено по её же лоссу на её же корпусе; почему "
        "прогон разошёлся (LR, flex-маска, окружение) — проба не разбирает: на "
        "локальной машине нет ни flex-пути, ни стека стенда",
        "Сверка на одном корпусе берёт первые 128 чанков из 9776 (не случайные): "
        "порядок чанков — свойство файла, а шаг 0 стенда читал перемешанный поток "
        "(DataLoader shuffle), поэтому совпадение проверяется по разбросу, а не по "
        "номеру чанка",
    ]
    evidence["open_questions"] = [
        "Пересматривать ли полосы ADR-015 (внимание 1.5× / эскалация 2×): они выведены "
        "от 127.5, а базовая модель даёт ~11.9/9.3 на v1. Если якорь сменится, полосы "
        "надо пересчитать, а вердикты чек-пойнтов пилота (ADR-015: «выполнены с "
        "запасом») перечитать",
        "Полосы ADR-015 выведены не просто «от состояния после CPT», а от CPT, "
        "который разошёлся (44 шага из 50 выше потолка базовой модели на её же "
        "корпусе). Считать ли S2-смоук валидным доказательством сходимости контура "
        "(его критерий «loss убывает» смотрел на сводку окон и разворота не увидел), "
        "и не задет ли пилот той же причиной — это вопрос к владельцу, не к пробе",
        "Не испорчен ли сам CPT окружением стенда: там включён LAGUNA_ATTN=flex "
        "(блочная маска из position_ids), а прогон на PyTorch 2.13 пошёл вверх по "
        "лоссу. GEN-EVAL этот путь не задевает (в патче нет position_ids → штатный "
        "SDPA), но обучение CPT идёт через него. Локально flex не воспроизводится: "
        "нужен стенд или отдельная проверка паритета flex против SDPA",
        "Какую полосу считать под v2: сдвиг домена ×1.20 (9.29 → 11.13) и общего языка "
        "×0.90 (11.93 → 10.77) — то есть под v2 «та же деградация» выглядит слабее "
        "по домену и сильнее по общему языку; одним множителем оба не покрываются",
        "Нужен ли явный замер базовой модели в самом пайплайне (до первого шага "
        "стадии): сейчас первый GEN-EVAL идёт после шагов обучения, и «база» в "
        "истории — это состояние, а не модель (дефект уже признан в ADR-015 п.4, "
        "но починка не внесена)",
        "Домен v1 — 5 документов с общим шаблоном (ADR-018): его PPL 9.29 держится на "
        "пяти точках (median 11.17 против корпусной 9.29). Переходить ли на v2 в "
        "качестве основного прибора, не имея v1-истории по 200 документам",
    ]
    return evidence


def _flat(res: dict, ds: dict) -> dict:
    """Плоская запись одного замера: число + статистика + опознавательные знаки."""
    out = {
        "ppl": res["ppl"],
        "docs": res["docs_counted"],
        "docs_in_file": ds["docs_kept"],
        "docs_dropped_short_tokens": res["docs_dropped_short_tokens"],
        "tokens": res["tokens"],
        "doc_ppl_median": res["doc_ppl"]["median"],
        "doc_ppl_mean": res["doc_ppl"]["mean"],
        "doc_ppl_min": res["doc_ppl"]["min"],
        "doc_ppl_max": res["doc_ppl"]["max"],
        "doc_ppl_stdev": res["doc_ppl"]["stdev"],
        "doc_ppl_max_over_median": res["doc_ppl"]["max_over_median"],
        "max_len": res["max_len"],
        "batch": res["batch"],
        "dtype": res["dtype"],
        "model": res["model"],
        "weights": res["weights"],
        "tokenizer": res["tokenizer"],
        "tokenizer_setup": res["tokenizer_setup"],
        "dataset": ds["resolved"],
        "sha256": ds["sha256"],
        "dataset_bytes": ds["bytes"],
        "dataset_mtime": ds["mtime"],
        "mask_check": res["mask_check"],
        "cross_check": res.get("cross_check"),
        "seconds": res["seconds"],
    }
    return out


def divergence(corpus: dict | None) -> dict:
    """Траектория лосса CPT-стадии смоука — того прогона, с которого снято 127.5.

    Зачем это рядом с ``ANCHOR_PROVENANCE``. Провенанс показывает, что число
    снято **после** CPT. Но из этого ещё не следует, что состояние «после CPT»
    здоровое: CPT мог отработать штатно, и тогда 127.5 — честное свойство
    доменного чекпойнта, просто не базовой модели. Траектория отвечает прямо:
    лосс на **своём** корпусе пошёл с 2.0989 (шаг 0, lr=0) вверх — 2.63, 2.80,
    3.15, 3.71, 3.96, 4.08, 4.66 — и до конца ходил в полосе 2.1…4.7.

    Разброс сам по себе ничего не доказывает: пачки бывают разной трудности.
    Доказательство — сравнение с разбросом **базовой** модели на тех же чанках
    (блок ``corpus``): у неё std ≈ 0.17 и потолок 2.51, то есть наблюдаемая
    полоса не объясняется данными. Поэтому вывод формулируется как «чекпойнт
    хуже базы на собственном обучающем корпусе», а не «лосс прыгает».
    """
    out: dict = {
        "what": ("лосс CPT-стадии смоука по шагам — прогон, из которого взято "
                 "127.5; проверка, что состояние «после CPT» вообще здоровое"),
        "sources": [str(p) for p in CPT_PROBE_LOG_CANDIDATES],
    }
    path, missed = first_existing(CPT_PROBE_LOG_CANDIDATES)
    if path is None:
        out["status"] = "NOT-VERIFIED"
        out["reason"] = "лог шагов CPT не найден: " + "; ".join(missed)
        return out

    parsed = parse_probe_losses(path.read_text(encoding="utf-8", errors="replace"))
    losses = parsed["losses"]
    out["source"] = str(path)
    out["sha256"] = sha256_file(path)
    out["parse"] = {k: v for k, v in parsed.items() if k != "losses"}
    out["trajectory"] = loss_stats(losses)
    out["head"] = [round(x, 4) for x in losses[:8]]
    out["stand_step0_loss"] = STAND_CPT_STEP0_LOSS
    out["step0_matches_log"] = bool(losses) and abs(
        losses[0] - STAND_CPT_STEP0_LOSS) < 5e-4

    base_max = (corpus or {}).get("base_loss", {}).get("max")
    if base_max is None:
        out["status"] = "NOT-VERIFIED"
        out["reason"] = ("разброс базовой модели по тем же чанкам не снят — "
                         "сравнивать полосу не с чем (нужен блок corpus_anchor)")
        return out

    out["base_max_loss"] = base_max
    out["steps_above_base_max"] = above_level(losses, base_max)
    out["steps_above_base_regime_level"] = above_level(losses, BASE_REGIME_LEVEL)
    worst = out["trajectory"]["max"]
    out["status"] = "OK"
    out["verdict"] = (
        "ПРОГОН РАЗОШЁЛСЯ" if out["steps_above_base_max"]["count"] else "полоса в пределах базы")
    out["summary"] = (
        f"Лосс CPT на своём корпусе: шаг 0 = {losses[0]:.4f} (lr=0, веса нетронуты), "
        f"максимум {worst:.4f}; базовая модель на тех же чанках не выходит за "
        f"{base_max:.4f}. Шагов выше потолка базы: "
        f"{out['steps_above_base_max']['count']} из {len(losses)} "
        f"({out['steps_above_base_max']['fraction'] * 100:.0f} %). "
        "То есть чекпойнт, на котором снято 127.5, хуже базовой модели на "
        "собственном обучающем корпусе: 127.5 — не «доменный сдвиг», а след "
        "расходящегося CPT")
    return out


def reproduction(ppl: dict, corpus: dict | None = None, div: dict | None = None) -> dict:
    """Совпал ли замер с известной базой 127.5 / 11.76 (S2, стенд, наборы v1).

    Три исхода, и они различаются механически, а не на глаз:

    * ``СОВПАЛО`` — расхождение в пределах допуска: методика воспроизводится;
    * ``ЯКОРЬ НЕ БАЗОВАЯ МОДЕЛЬ`` — расхождение выше допуска, **и** независимый
      якорь базовой модели (её же лосс при lr=0, см. ``BASE_ANCHOR``) по порядку
      величины согласен с нашим замером, а не с известным числом. Тогда
      разошёлся не прибор, а подпись под якорем;
    * ``РАСХОЖДЕНИЕ БЕЗ ОБЪЯСНЕНИЯ`` — ни то, ни другое: числам по v2 в этом
      прогоне верить нельзя, пока причина не найдена.
    """
    out = {"known": KNOWN_BASE, "tolerance_pct": REPRO_TOLERANCE_PCT, "checks": {}}
    worst = 0.0
    for name, key in (("v1_general", "ppl_general"), ("v1_domain", "ppl_domain")):
        if name not in ppl:
            out["checks"][name] = {"status": "не измерялся"}
            continue
        got = ppl[name]["ppl"]
        want = KNOWN_BASE[key]
        delta = (got - want) / want * 100.0
        worst = max(worst, abs(delta))
        out["checks"][name] = {
            "known": want, "measured": got, "delta_pct": delta,
            "within_tolerance": abs(delta) <= REPRO_TOLERANCE_PCT,
        }
    out["anchor_provenance"] = ANCHOR_PROVENANCE
    out["base_anchor"] = dict(BASE_ANCHOR)
    if corpus is not None:
        out["corpus_anchor"] = corpus
    if div is not None:
        out["divergence"] = div

    # Сверять нечего, если наборы v1 в этом прогоне не измерялись (например,
    # `--models instruct` или `--dtype float32` без bf16): пустая сверка — не
    # «совпало», а «не проверялось». Ноль расхождения здесь означал бы зелень,
    # полученную отсутствием входа.
    compared = [c for c in out["checks"].values() if "delta_pct" in c]
    if not compared:
        out["verdict"] = "не проверялось (наборы v1 в этом прогоне не измерялись)"
        out["summary"] = ("Сверка с известной базой 127.5 / 11.76 требует набора v1 на "
                          "базовой модели: запустите без --models instruct и с тем dtype, "
                          "в котором снимался блок ppl")
        return out

    # Проверка гипотезы «якорь — не базовая модель»: наша доменная PPL против
    # доменной PPL базовой модели, снятой стендом при lr=0.
    measured_domain = ppl.get("v1_domain", {}).get("ppl")
    anchor_ratio = (measured_domain / BASE_ANCHOR["implied_ppl"]) if measured_domain else None
    anchor_agrees = anchor_ratio is not None and 1 / BASE_ANCHOR_BAND <= anchor_ratio <= BASE_ANCHOR_BAND
    out["base_anchor_check"] = {
        "measured_v1_domain": measured_domain,
        "anchor_implied_ppl": BASE_ANCHOR["implied_ppl"],
        "ratio_measured_over_anchor": anchor_ratio,
        "band": f"[1/{BASE_ANCHOR_BAND}, {BASE_ANCHOR_BAND}]",
        "anchor_agrees_with_measurement": anchor_agrees,
        "note": ("корпуса разные (domain_eval против CPT-корпуса), поэтому сравнение "
                 "по порядку величины; совпадение = наш замер и лосс стенда описывают "
                 "одну и ту же базовую модель"),
    }

    # Второй якорь, сильнее первого: тот же корпус, тот же счёт. Лосс стенда при
    # lr=0 обязан попасть в разброс базовой модели по её же чанкам. Если не
    # попал — разошлись не подписи, а модели/методика, и вердикт «якорь не базовая
    # модель» был бы самоуспокоением: он списал бы на чужую ошибку свою.
    same_corpus_agrees = None
    if corpus is not None and "base_loss" in corpus:
        same_corpus_agrees = bool(corpus.get("stand_step0_within_base_range"))
        out["same_corpus_check"] = {
            "stand_step0_loss": STAND_CPT_STEP0_LOSS,
            "base_loss_range": [corpus["base_loss"]["min"], corpus["base_loss"]["max"]],
            "base_loss_mean": corpus["base_loss"]["mean"],
            "base_loss_stdev": corpus["base_loss"]["stdev"],
            "n_chunks": corpus["n_chunks"],
            "stand_step0_z": corpus.get("stand_step0_z"),
            "agrees": same_corpus_agrees,
            "note": ("сверка на ОДНОМ корпусе (пакованные чанки CPT): сильнее, чем "
                     "сопоставление порядков с base_anchor_check, потому что корпус "
                     "и счёт совпадают точно"),
        }

    if worst <= REPRO_TOLERANCE_PCT:
        out["verdict"] = "СОВПАЛО"
        out["summary"] = (
            f"Методика воспроизводится вне стенда: худшее расхождение с базой S2 "
            f"{worst:.3f} % (порог {REPRO_TOLERANCE_PCT} %)")
    elif anchor_agrees and same_corpus_agrees is not False:
        out["verdict"] = "ЯКОРЬ НЕ БАЗОВАЯ МОДЕЛЬ"
        diverged = (div or {}).get("verdict") == "ПРОГОН РАЗОШЁЛСЯ"
        # Предложения собираются по отдельности: у каждого свой источник, и
        # пропуск одного (нет якоря на корпусе / нет траектории) не должен
        # склеивать чужие выводы в одно предложение.
        parts = [
            f"С известным числом разошлось на {worst:.1f} % — но разошлась подпись, "
            f"а не методика: наш замер домена {measured_domain:.2f} согласен по порядку "
            f"с лоссом стенда на нетронутых весах (lr=0: {BASE_ANCHOR['loss']} → "
            f"PPL {BASE_ANCHOR['implied_ppl']:.2f})"
        ]
        if same_corpus_agrees and corpus and "base_loss" in corpus:
            base = corpus["base_loss"]
            z = corpus.get("stand_step0_z")
            parts.append(
                f"На одном корпусе (пакованные чанки CPT, {corpus['n_chunks']} шт.) лосс "
                f"стенда при lr=0 тоже попадает в разброс базы: "
                f"{base['mean']:.3f}±{base['stdev']:.3f}, z="
                + (f"{z:.2f}" if z is not None else "н/д"))
        if diverged:
            parts.append((div or {}).get("summary", ""))
        else:
            parts.append("127.5 снято после загрузки CPT-чекпойнта (см. anchor_provenance)")
        parts.append("Базовая линия ADR-015 требует пересмотра: пороги 1.5×/2× выведены "
                     "от состояния после CPT")
        out["summary"] = ". ".join(p for p in parts if p)
    else:
        out["verdict"] = "РАСХОЖДЕНИЕ БЕЗ ОБЪЯСНЕНИЯ"
        out["summary"] = (
            f"Худшее расхождение {worst:.3f} % > порога {REPRO_TOLERANCE_PCT} %, и "
            f"независимый якорь базовой модели его не объясняет — числа по v2 в этом "
            f"прогоне несопоставимы с историей, пока причина не найдена")
    return out


def shift(ppl: dict) -> dict:
    """Насколько набор v2 смещает базовую линию относительно v1."""
    out: dict[str, dict] = {}
    for kind in ("general", "domain"):
        a, b = f"v1_{kind}", f"v2_{kind}"
        if a in ppl and b in ppl:
            out[kind] = {
                "v1": ppl[a]["ppl"], "v2": ppl[b]["ppl"],
                "ratio_v2_over_v1": ppl[b]["ppl"] / ppl[a]["ppl"],
                "docs_v1": ppl[a]["docs"], "docs_v2": ppl[b]["docs"],
                "doc_median_v1": ppl[a]["doc_ppl_median"],
                "doc_median_v2": ppl[b]["doc_ppl_median"],
            }
    if out:
        out["note"] = ("Полосы ADR-015 (внимание 1.5× / эскалация 2×) выведены от базы v1; "
                       "под v2 их надо пересчитать от базы v2 — иначе «+50 %» будет "
                       "наполовину сдвигом набора. Сдвиг не единый: домен уходит вверх, "
                       "общий язык вниз, поэтому одним множителем на оба набора "
                       "порог не переносится")
    return out


# ─────────────────────────── план и CLI ─────────────────────────────────────


def print_plan(args) -> int:
    """Показать, что и как будет измерено, не загружая модель."""
    print("S3h — PPL-проба наборов GEN-EVAL (v1/v2) на локальной машине")
    print(f"  кейс:      {CASE_ROOT}")
    print(f"  наборы:    {args.sets}")
    print(f"  модели:    {args.models}")
    print(f"  dtype:     {args.dtype}  (пайплайн считает в bfloat16 — BF16)")
    print(f"  обрезка:   {args.max_len} токенов, батч {args.batch}, устройство {args.device}")
    print("\n  наборы на диске (только чтение, gb10-shared):")
    for name, rel in SETS.items():
        p = CASE_ROOT / rel
        if p.is_file():
            ds = read_dataset(p)
            print(f"    {name:11s} {ds['docs_kept']:>4d} док.  {ds['bytes']:>7d} Б  "
                  f"sha256={ds['sha256'][:12]}…  {rel}")
        else:
            print(f"    {name:11s} НЕ НАЙДЕН: {p}")
    print("\n  методика (повтор _ppl_eval, laguna_pipeline_v8.py:1556-1581):")
    print("    split('\\n---\\n') → strip → len>50 → encode(add_special_tokens=False)[:1024]")
    print("    → len(enc)>8 → батч 4 с right-pad и маской → exp(Σnll/Σтокенов)")
    print("    плюс median/mean PPL по документам из того же forward'а")
    print("\n  сверка: база S2 (v1, GB10) 127.5 / 11.76 — воспроизведение методики")
    pack, missed_pack = first_existing(CPT_PACK_CANDIDATES)
    if args.corpus_anchor and pack is not None:
        print(f"  якорь на корпусе стенда: {pack}")
        print(f"    {CORPUS_ANCHOR_CHUNKS} чанков × {DEFAULT_MAX_LEN * 8} токенов, "
              f"срез lm_head по {CORPUS_ANCHOR_SLICE} позиций → распределение лосса базы")
    elif args.corpus_anchor:
        print("  якорь на корпусе стенда: NOT-VERIFIED, чанки не найдены "
              f"({'; '.join(missed_pack)})")
    else:
        print("  якорь на корпусе стенда: выключен (--no-corpus-anchor)")
    log, missed_log = first_existing(CPT_PROBE_LOG_CANDIDATES)
    print(f"  траектория CPT стенда: {log}" if log
          else f"  траектория CPT стенда: NOT-VERIFIED ({'; '.join(missed_log)})")
    print("  НЕ делается: запись в gb10-shared, обращение к GB10, правка ADR/спайна")
    print(f"  evidence: {args.out}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Проба PPL наборов GEN-EVAL v1/v2 на локальной машине (S3h)")
    ap.add_argument("--plan", action="store_true",
                    help="напечатать план пробы и выйти (модель не загружается)")
    ap.add_argument("--sets", default="all",
                    help="all либо список через запятую: " + ",".join(SETS))
    ap.add_argument("--models", default="all", choices=["all", "base", "instruct"],
                    help="какие ревизии мерить (all = base + instruct)")
    ap.add_argument("--dtype", default="bfloat16",
                    help="bfloat16 (как пайплайн), float32, либо оба через запятую")
    ap.add_argument("--pipeline-tokenizer", default="both", choices=["on", "off", "both"],
                    help="повторять ли настройку токенизатора пайплайна (8 спецтокенов + "
                         "resize embeddings); both = основной замер и контроль")
    ap.add_argument("--device", default="cuda", help="cuda или cpu")
    ap.add_argument("--max-len", type=int, default=DEFAULT_MAX_LEN)
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    ap.add_argument("--out", default="evidence/ppl-baseline-v1v2.json",
                    help="куда записать evidence (путь относительно кейса)")
    ap.add_argument("--cross-check", dest="cross_check", action="store_true", default=True,
                    help="сверять батчевый путь с одиночным forward'ом (по умолчанию да)")
    ap.add_argument("--no-cross-check", dest="cross_check", action="store_false",
                    help="выключить самопроверку батчевого пути")
    ap.add_argument("--corpus-anchor", dest="corpus_anchor", action="store_true", default=True,
                    help="снять лосс базы на пакованных чанках стенда (~50 с, по умолчанию да)")
    ap.add_argument("--no-corpus-anchor", dest="corpus_anchor", action="store_false",
                    help="не снимать якорь на корпусе стенда (быстрый прогон)")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.plan:
        return print_plan(args)
    evidence, code = run(args)
    if args.json and evidence:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    if evidence:
        rep = evidence["reproduction"]
        print(f"\nвоспроизведение известной базы (127.5 / 11.76, v1, GB10): {rep['verdict']}")
        for name, chk in rep["checks"].items():
            if "delta_pct" in chk:
                print(f"  {name:11s} измерено {chk['measured']:.4f} против "
                      f"{chk['known']:.4f} → {chk['delta_pct']:+.3f} %")
        if rep.get("summary"):
            print(f"  {rep['summary']}")
        for kind, sh in evidence["v1_vs_v2_shift"].items():
            if kind == "note":
                continue
            print(f"  сдвиг {kind}: v1 {sh['v1']:.3f} → v2 {sh['v2']:.3f} "
                  f"(×{sh['ratio_v2_over_v1']:.3f}), документов {sh['docs_v1']} → {sh['docs_v2']}")
    return code


if __name__ == "__main__":
    sys.exit(main())
