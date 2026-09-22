#!/usr/bin/env python3
"""S3x — сводная матрица чистоты: **все** измерительные наборы × **все** кандидатные миксы.

Зачем отдельный инструмент, если гейт уже есть. ``tools/check_eval_set_purity.py``
отвечает на вопрос «чист ли **этот** набор против **этих** миксов» и вызывается по
одному набору за прогон. Реестр EVAL-SETS §4 держит свод из трёх наборов, собранный
**руками** из отдельных прогонов, — и это ровно тот способ, которым дефект v2 прожил
до S3m-2: свод, обновляемый руками, отстаёт от состава наборов. Здесь тот же инвариант
считается сразу по всем наборам и всем миксам, одним прогоном и одним артефактом.

Что именно проверяется — **не переписано, а импортировано**. ``norm``, длина окна,
разделители документов и правило «документ набора — подстрокой» берутся из гейта
(``check_eval_set_purity``): две копии этих определений означали бы две правды о том,
что считалось совпадением, и расхождение свода с гейтом читалось бы как расхождение
данных. Матрица обязана воспроизводить гейт **числом**, а не согласием формулировок.

Почему свод считается от **корпусов-источников**, а микс получается композицией.
Микс — это префикс потока корпуса (``chunks[:N]``); префикс — подмножество файла,
поэтому ноль по файлу **строже** нуля по миксу. Отсюда два уровня матрицы:

* ``source_matrix`` — примитив: набор × корпус-источник (документы / окна);
* ``mix_matrix`` — то, что требует ADR-025 п.1: набор × микс, где клетка микса есть
  **объединение** клеток его источников (объединение, а не сумма: одно и то же окно,
  найденное в двух источниках микса, — одно нарушение, а не два).

Один проход на корпус. Корпуса читаются потоково (документ за документом), а не
целиком в память: ``cpt_corpus_full.txt`` — 771 МБ, и ``read_text().split()`` на нём
это гигабайты на ровном месте. Окна всех наборов лежат в одном индексе
(``окно → битовая маска наборов``), поэтому проход по корпусу — один на все шесть
наборов, а не шесть.

Роли наборов — из ADR-027 пп.3–5 (язык) и ADR-031 п.4 (домен); они не выводятся из
чисел, а **названы** в реестре ниже и попадают в артефакт. Порог у роли свой:

* наборы общего языка (кроме ``invalid``) — ADR-025 п.1 буквально: **ноль** и по
  документам, и по 12-граммам против каждого кандидатного микса;
* доменные наборы — инвариант применяется в части, делающей число свойством модели
  (EVAL-SETS §3.4): **ноль документов** обязателен, 12-граммы **называются числом**
  и нарушением не являются (карточки набора и обучения — одного семейства, шаблонные
  обороты снижают PPL независимо от забывания);
* ``invalid`` (``general_eval_v2``) — пересечение **ожидаемо** (200/200 документов) и
  нарушением не считается: набор уже снят с решающей роли ADR-027 п.5. Он остаётся в
  матрице как обратная проверка гейта: гейт, который на нём не срабатывает, сломан.

Коды возврата::

    0 — свод чист: все решающие наборы проходят свой порог против всех миксов
    1 — нарушение: решающий набор пересекается с кандидатным миксом
    2 — NOT-VERIFIED: нет набора или корпуса — это не зелёный

Запуск::

    python3 tools/eval_purity_matrix.py                       # весь свод
    python3 tools/eval_purity_matrix.py --sources replay_ru   # частичный прогон
    python3 tools/eval_purity_matrix.py --report evidence/fleet-eval-purity-matrix.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Единственный источник определений «что считалось совпадением» — гейт ADR-025 п.1.
#: Импорт, а не копия: свод обязан воспроизводить гейт числом (см. докстринг).
import check_eval_set_purity as gate  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent
DATASETS = CASE_ROOT / "datasets"
CALIB = Path("/home/user/gb10-shared/calib")

WINDOW = gate.WINDOW          # 12 — как в аудите S3m и в INFO-сигнале гейта
SET_SEP = gate.SET_SEP        # "\n---\n" — как режет прибор (_ppl_eval)
DOMAIN_SEP = gate.DOMAIN_SEP  # "\n---\n" — как резался доменный корпус
REPLAY_SEP = gate.REPLAY_SEP  # "\n\n"   — как резался реплей-корпус

#: ── Реестр измерительных наборов ─────────────────────────────────────────────
#: ``gate`` — что именно требует роль от чисел этого набора:
#:   ``strict``          — ноль документов И ноль окон (ADR-025 п.1 буквально);
#:   ``docs_only``       — ноль документов обязателен, окна называются числом (§3.4);
#:   ``expected_overlap``— пересечение ожидаемо: набор снят с решающей роли (ADR-027 п.5).
SETS: list[dict] = [
    {
        "key": "v1_general", "path": "datasets/general_eval.txt", "gate": "strict",
        "role": "historical_reference", "role_ru": "исторический опорный, не решающий",
        "adr": "ADR-027 п.3", "genre": "незадокументирован (пришёл с пайплайном 09.08)",
        "distribution": "unknown",
        "why": "снят с решающей роли: провенанс не задокументирован, "
               "24 документа не различают эффекты",
    },
    {
        "key": "general_eval_v2", "path": "datasets/general_eval_v2.txt",
        "gate": "expected_overlap",
        "role": "invalid", "role_ru": "invalid — снят с употребления",
        "adr": "ADR-027 п.5", "genre": "энциклопедическая статья (Википедия)",
        "distribution": "in-distribution (обучающий текст)",
        "why": "200/200 документов — обучающий текст v12r50; в матрице остаётся "
               "обратной проверкой гейта",
    },
    {
        "key": "v3_general_K1", "path": "datasets/general_eval_v3.txt", "gate": "strict",
        "role": "deciding_K1", "role_ru": "решающий K1 — в жанре реплея",
        "adr": "ADR-027 п.1, п.4",
        "genre": "энциклопедическая статья (Википедия)",
        "distribution": "in-distribution по жанру, out-of-distribution по документам",
        "why": "отвечает: защитил ли реплей тот класс текста, ради которого заведён",
    },
    {
        "key": "k2_general", "path": "datasets/general_eval_k2.txt", "gate": "strict",
        "role": "deciding_K2", "role_ru": "решающий K2 — вне обучающего распределения",
        "adr": "ADR-027 п.1, п.4",
        "genre": "официально-аналитическая проза (бюллетени Счётной палаты)",
        "distribution": "out-of-distribution",
        "why": "отвечает: сохранила ли модель обобщение за пределами обучения",
    },
    {
        "key": "v1_domain", "path": "datasets/domain_eval.txt", "gate": "docs_only",
        "role": "historical_scale", "role_ru": "историческая домен-шкала, решающей не является",
        "adr": "ADR-031 п.4 (ADR-015 — слабый прибор по числу документов)",
        "genre": "концепт-карта ML/AI",
        "distribution": "in-distribution (тот же домен и шаблон)",
        "why": "5 документов: решение принималось бы внутри разброса прибора; "
               "хранится для непрерывности с ppl_domain S3n",
    },
    {
        "key": "v2_domain", "path": "datasets/domain_eval_v2.txt", "gate": "docs_only",
        "role": "deciding_domain", "role_ru": "домен-метрика, решающая",
        "adr": "ADR-031 п.4", "genre": "концепт-карта ML/AI",
        "distribution": "in-distribution по жанру и шаблону (обобщение внутри домена, не вне)",
        "why": "200 документов против 5: различимость ×48 — ADR-015 требовал именно этого",
    },
]

#: ── Реестр корпусов-источников обучения ──────────────────────────────────────
#: ``gates`` — участвует ли корпус в инварианте ADR-025 п.1 (то есть входит ли он в
#: какой-либо **кандидатный** микс). Исторические корпуса и SFT-стадия не гейтят
#: CPT-наборы, но называются: они отвечают на соседний вопрос («не обучающий ли это
#: текст вообще»), и молчать о них значило бы выдавать узкую проверку за широкую.
SOURCES: list[dict] = [
    {
        "key": "replay_ru", "path": "datasets/general_replay_ru.txt", "sep": REPLAY_SEP,
        "kind": "text", "gates": True, "role": "replay",
        "what": "реплей общего языка: префикс дампа wikimedia/wikipedia 20231101.ru",
        "mixes": ["v12r", "v12r50", "ctrl100"],
    },
    {
        "key": "domain_v10.1", "path": "datasets/cpt_corpus_v10.1.txt", "sep": DOMAIN_SEP,
        "kind": "text", "gates": True, "role": "domain",
        "what": "доменный корпус: концепт-карты ML/AI (Ariadna v10.1)",
        "mixes": ["v12r", "v12r50"],
    },
    {
        "key": "domain_full", "path": "datasets/cpt_corpus_full.txt", "sep": DOMAIN_SEP,
        "kind": "text", "gates": False, "role": "domain_historical",
        "what": "исторический полный доменный корпус (v9/v10): источник обучающего материала "
                "ранних прогонов и источник секций, из которых собран доменный набор",
        "mixes": [],
    },
    {
        "key": "sft_v12", "path": "datasets/sft_train_v12.jsonl", "sep": None,
        "kind": "jsonl", "gates": False, "role": "sft",
        "what": "SFT-стадия (следующий этап лесенки): многоходовая разметка с tool_call. "
                "Единица сравнения — строка сообщения, не пример целиком: окно, склеенное "
                "из двух сообщений, в обучении не встречалось",
        "mixes": [],
    },
]

#: ── Реестр кандидатных миксов ────────────────────────────────────────────────
#: Микс описан **составом источников**, а не своим файлом: файл микса — манифест
#: (текст комментариев), обучающий материал лежит в источниках. Состав зафиксирован
#: карточками сборки (ADR-047/C-010/S3o); числа чанков нужны для отчёта, проверка
#: идёт по источникам целиком (см. докстринг).
MIXES: list[dict] = [
    {
        "key": "v12r", "sources": ["domain_v10.1", "replay_ru"],
        "domain_chunks": 7332, "replay_chunks": 2444, "chunk_len": 8192,
        "manifest": "cpt_corpus_v12r.txt",
        "note": "заморожен ADR-003; 75/25, домен — весь пул v10.1 (7332 чанка)",
        "arms_note": "на этом же миксе идёт полный CPT стадии (LR×0.035, ADR-031 п.1): "
                     "его конфигурация лежит в runs/, а не в calib/, поэтому в arms не попадает",
    },
    {
        "key": "v12r50", "sources": ["domain_v10.1", "replay_ru"],
        "domain_chunks": 3493, "replay_chunks": 3493, "chunk_len": 8192,
        "manifest": "datasets/cpt_corpus_v12r50.txt",
        "note": "калибровка S3m (ADR-047): равные доли; реплей-источник даёт ровно 3493 чанка",
    },
    {
        "key": "ctrl100", "sources": ["replay_ru"],
        "domain_chunks": 0, "replay_chunks": 3493, "chunk_len": 8192,
        "manifest": "datasets/cpt_corpus_ctrl100.txt",
        "note": "контрольная рука C1 (S3o): домена нет вовсе — проверка гипотезы о протоколе",
    },
    {
        "key": "cpt_corpus_v12r50", "sources": ["domain_v10.1", "replay_ru"],
        "domain_chunks": 3493, "replay_chunks": 3493, "chunk_len": 8192,
        "manifest": "/home/user/gb10-shared/calib/mix-v12r50-build.json",
        "duplicate_of": "v12r50",
        "note": "найден гейтом автоматически в calib/ (карточка сборки). Состав "
                "тождествен v12r50 — оставлен в своде, чтобы имена миксов совпадали "
                "с исторической матрицей §4",
    },
]

#: Обратные сверки: отчёт гейта → набор. Свод обязан воспроизвести чужие числа
#: **числом** (ADR-011 п.4), иначе это второй прибор, а не сводный вид того же.
CROSS_CHECK: list[tuple[str, str]] = [
    ("evidence/s3q-purity.json", "v3_general_K1"),
    ("evidence/s3t-purity-k2.json", "k2_general"),
    ("evidence/s3t-purity-v2.json", "general_eval_v2"),
    ("evidence/s3w-purity-v1domain.json", "v1_domain"),
    ("evidence/s3w-purity-v2domain.json", "v2_domain"),
]


def note(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _redirect(registry: list[dict], spec: str, what: str) -> None:
    """``ключ=путь`` — перенаправление входа (нужно тестам, см. ``build``)."""
    if "=" not in spec:
        raise SystemExit(f"ОТКАЗ: {what} задан без пути ({spec!r}); нужно ключ=путь")
    key, path = spec.split("=", 1)
    for entry in registry:
        if entry["key"] == key:
            entry["path"] = path
            return
    raise SystemExit(f"ОТКАЗ: неизвестный ключ {what}а: {key}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def own_revision() -> dict:
    me = Path(__file__)
    if not me.is_file():
        raise SystemExit(f"ОТКАЗ: инструмент запущен без файла на диске ({me})")
    rev = {"path": "tools/eval_purity_matrix.py", "sha256": sha256_file(me)}
    #: Прибор, чьи определения импортированы, называется хешем: свод без хеша гейта
    #: нельзя сверить с гейтом.
    rev["imports"] = {"path": "tools/check_eval_set_purity.py",
                      "sha256": sha256_file(Path(gate.__file__))}
    return rev


# ─────────────────────────── чтение источников ───────────────────────────────


def iter_text_docs(path: Path, sep: str, block: int = 1 << 23):
    """Документы корпуса потоково: файл не поднимается в память целиком.

    ``read_text().split(sep)`` на 771 МБ — это сам текст (~1.4 ГБ как UCS-2 плюс
    список документов). Буфер переносит хвост через границу блока: разделитель
    может рассечь блок, и склеенный разделитель дал бы лишний документ.
    """
    carry = ""
    with path.open(encoding="utf-8", errors="replace") as f:
        while True:
            chunk = f.read(block)
            if not chunk:
                break
            parts = (carry + chunk).split(sep)
            carry = parts.pop()
            for p in parts:
                yield p
    if carry:
        yield carry


def iter_jsonl_strings(path: Path):
    """Строковые листья JSONL-строки — каждый отдельным «документом».

    Единица сравнения — сообщение, а не пример: пример собирается конкатенацией, и
    окно на стыке двух сообщений не встречалось в обучении. Считать его совпадением
    значило бы ловить собственную склейку (та же причина, по которой окна считаются
    внутри документа корпуса).
    """
    def walk(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                yield line  # битая строка — всё равно текст обучения, а не мусор
                continue
            yield from walk(obj)


def iter_source_docs(src: dict):
    path = Path(src["path"])
    if src["kind"] == "jsonl":
        yield from iter_jsonl_strings(path)
    else:
        yield from iter_text_docs(path, src["sep"])


# ─────────────────────────── индекс наборов ──────────────────────────────────


class SetIndex:
    """Все наборы в одном индексе: окно → битовая маска наборов.

    Маска (а не множество ключей) выбрана по памяти: окон по всем наборам ~200 тыс.,
    и на каждое приходится ещё множество — это десятки мегабайт на ровном месте.
    Битовая маска — одно число на окно.
    """

    def __init__(self, sets: list[dict]):
        self.sets = sets
        self.bit = {s["key"]: 1 << i for i, s in enumerate(sets)}
        self.win: dict[str, int] = {}
        self.sig: dict[str, list[tuple[str, int]]] = {}
        self.short: dict[str, list[tuple[str, str]]] = {}
        self.docs_norm: dict[str, list[str]] = {}
        self.meta: dict[str, dict] = {}
        #: Якорь поиска окна — его первое слово. Заполняется по мере наполнения
        #: индекса: ``join`` двенадцати слов делается только там, где первое слово
        #: вообще встречается среди искомых окон (приём гейта, не приближение:
        #: окна, которого нет в индексе, совпасть не может).
        self.anchor: set[str] = set()

    @staticmethod
    def _sig(words: list[str]) -> str | None:
        """Сигнатура документа набора — его первые 12 слов.

        Документ набора, целиком лежащий в документе корпуса, влечёт свою сигнатуру:
        нормализация (lower + схлопнутые пробелы) перевод-инвариантна, поэтому первые
        12 слов набора идут в корпусе подряд. Значит проверку «документ подстрокой»
        достаточно запускать там, где совпала сигнатура, — это ровно те кандидаты,
        которые иначе были бы найдены перебором всех документов набора по каждому
        документу корпуса (172 тыс. × 200 подстрок на корпус).
        """
        return " ".join(words[:WINDOW]) if len(words) >= WINDOW else None

    def add(self, key: str, path: Path) -> dict:
        #: Документы набора — ровно как их видит прибор: тем же ``read_set`` гейта
        #: (``SET_SEP`` + фильтр длины). Своя нарезка здесь означала бы свой набор.
        ds = gate.read_set(path)
        mask = self.bit[key]
        normed: list[str] = []
        windows: set[str] = set()
        n_short = 0
        for i, d in enumerate(ds["docs"]):
            nd = gate.norm(d)
            normed.append(nd)
            words = nd.split()
            if len(words) < WINDOW:
                n_short += 1
                if words:
                    self.short.setdefault(words[0], []).append((key, nd))
                continue
            self.sig.setdefault(" ".join(words[:WINDOW]), []).append((key, i))
            for w in gate.windows(words):
                windows.add(w)
        for w in windows:
            self.win[w] = self.win.get(w, 0) | mask
            self.anchor.add(w.split(" ", 1)[0])
        self.docs_norm[key] = normed
        self.meta[key] = {
            "path": str(path), "sha256": ds["sha256"], "bytes": ds["bytes"],
            "docs": len(ds["docs"]), "windows": len(windows), "short_docs": n_short,
        }
        return self.meta[key]


# ─────────────────────────── проход по корпусу ───────────────────────────────


def scan_source(src: dict, idx: SetIndex) -> dict:
    """Один проход по корпусу обслуживает все наборы сразу.

    Считается то же, что в гейте (``check_eval_set_purity.scan_corpus``): документы
    набора подстрокой в нормализованном документе корпуса и окна по 12 слов внутри
    документа. Числа обязаны совпасть с гейтом — это проверяется ``--cross-check``.
    """
    t0 = time.time()
    path = Path(src["path"])
    n_docs = 0
    matched: dict[str, set[str]] = {s["key"]: set() for s in idx.sets}
    occurrences: dict[str, int] = {s["key"]: 0 for s in idx.sets}
    doc_pairs: dict[str, int] = {s["key"]: 0 for s in idx.sets}
    doc_examples: dict[str, list[dict]] = {s["key"]: [] for s in idx.sets}
    win_examples: dict[str, list[str]] = {s["key"]: [] for s in idx.sets}
    anchor, bit, short_first = idx.anchor, idx.bit, set(idx.short)

    for raw in iter_source_docs(src):
        d = gate.norm(raw)
        if not d:
            continue
        n_docs += 1
        words = d.split()
        n = len(words)
        if n >= WINDOW:
            for i in range(n - WINDOW + 1):
                if words[i] not in anchor:
                    continue
                s = " ".join(words[i:i + WINDOW])
                mask = idx.win.get(s)
                if not mask:
                    continue
                for key, b in bit.items():
                    if mask & b:
                        matched[key].add(s)
                        occurrences[key] += 1
                        if len(win_examples[key]) < 20:
                            win_examples[key].append(s)
                # сигнатура набора совпала — проверяем документ целиком
                for key, di in idx.sig.get(s, ()):
                    if idx.docs_norm[key][di] in d:
                        doc_pairs[key] += 1
                        if len(doc_examples[key]) < 20:
                            doc_examples[key].append(
                                {"set_doc_index": di, "source_doc_chars": len(d)})
        # документы короче окна не дают ни одного окна — их ищем напрямую,
        # но только там, где вообще встречается их первое слово
        if short_first:
            for w0 in short_first & set(words):
                for key, want in idx.short[w0]:
                    if want in d:
                        doc_pairs[key] += 1
                        if len(doc_examples[key]) < 20:
                            doc_examples[key].append(
                                {"set_doc_index": -1, "source_doc_chars": len(d),
                                 "note": "документ набора короче окна"})
    return {
        "key": src["key"], "path": str(path), "bytes": path.stat().st_size,
        "sha256": sha256_file(path), "kind": src["kind"], "role": src["role"],
        "source_docs": n_docs, "matched": matched,
        "per_set": {
            s["key"]: {
                "overlap_docs": doc_pairs[s["key"]],
                "overlap_ngram": len(matched[s["key"]]),
                "overlap_ngram_occurrences": occurrences[s["key"]],
                "doc_examples": doc_examples[s["key"]],
                "window_examples": sorted(win_examples[s["key"]])[:20],
            } for s in idx.sets
        },
        "seconds": round(time.time() - t0, 1),
    }


def discover_arms(calib_dir: Path) -> dict[str, list[str]]:
    """Руки, обучавшиеся на каждом миксе: ``cpt_data_ctr`` из ``calib_params.json``.

    Нужны, чтобы в своде было видно **цену** пересечения: клетка матрицы относится не
    к абстрактному миксу, а к конкретным прогонам, чьи числа будут читаться.
    """
    arms: dict[str, list[str]] = {}
    if not calib_dir.is_dir():
        return arms
    for p in sorted(calib_dir.glob("*/calib_params.json")) + \
            sorted(calib_dir.glob("*/pipeline_patch.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ctr = d.get("cpt_data_ctr")
        arm = d.get("arm")
        if not ctr or not arm:
            continue
        mix = Path(ctr).stem  # cpt_corpus_v12r → v12r
        mix = mix.replace("cpt_corpus_", "")
        arms.setdefault(mix, [])
        if arm not in arms[mix]:
            arms[mix].append(arm)
    return arms


# ─────────────────────────── сборка отчёта ───────────────────────────────────


def build(args) -> tuple[dict, int]:
    #: Реестры копируются, а не правятся на месте: перенаправление путей нужно
    #: тестам (фикстуры в mktemp-каталоге — в дерево кейса ничего не пишется, AD-4),
    #: и оно не должно пережить прогон.
    sets = [dict(s) for s in SETS]
    sources = [dict(s) for s in SOURCES]
    for spec in (args.set_path or []):
        _redirect(sets, spec, "набор")
    for spec in (args.source_path or []):
        _redirect(sources, spec, "корпус")

    idx = SetIndex(sets)
    wanted_sets = set(args.sets.split(",")) if args.sets else {s["key"] for s in sets}
    wanted_src = set(args.sources.split(",")) if args.sources else {s["key"] for s in sources}
    idx.sets = [s for s in sets if s["key"] in wanted_sets]

    missing: list[str] = []
    for s in idx.sets:
        p = CASE_ROOT / s["path"]
        if not p.is_file():
            missing.append(f"набор {s['key']}: {p}")
        else:
            m = idx.add(s["key"], p)
            note(f"набор {s['key']}: {m['docs']} документов, {m['windows']} окон, "
                 f"sha256 {m['sha256'][:12]}")
    srcs = [s for s in sources if s["key"] in wanted_src]
    for s in srcs:
        if not Path(s["path"]).is_file():
            missing.append(f"корпус {s['key']}: {s['path']}")
    if missing:
        print("NOT-VERIFIED: нет данных: " + "; ".join(missing), file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED
    if not idx.sets or not srcs:
        print("NOT-VERIFIED: пустой набор или пустой список корпусов", file=sys.stderr)
        return {}, EXIT_NOT_VERIFIED

    note(f"окон в индексе: {len(idx.win)}; сигнатур документов: {len(idx.sig)}; "
         f"коротких документов набора: {sum(len(v) for v in idx.short.values())}")

    scans: dict[str, dict] = {}
    for s in srcs:
        note(f"корпус {s['key']} ({s['path']}) — проход по {s['kind']}…")
        scans[s["key"]] = scan_source(s, idx)
        per = scans[s["key"]]["per_set"]
        note(f"  документов {scans[s['key']]['source_docs']}; "
             + "; ".join(f"{k}: {v['overlap_docs']} док / {v['overlap_ngram']} окон"
                         for k, v in per.items())
             + f" ({scans[s['key']]['seconds']} с)")

    #: ── примитив: набор × корпус-источник ──
    source_matrix = []
    for s in idx.sets:
        for src in srcs:
            cell = scans[src["key"]]["per_set"][s["key"]]
            source_matrix.append({
                "set": s["key"], "set_role": s["role"], "source": src["key"],
                "source_role": src["role"], "source_gates": src["gates"],
                "overlap_docs": cell["overlap_docs"],
                "overlap_ngram": cell["overlap_ngram"],
                "overlap_ngram_occurrences": cell["overlap_ngram_occurrences"],
                "verdict": _cell_verdict(s, src, cell),
            })

    #: ── то, что требует ADR-025 п.1: набор × микс ──
    arms = discover_arms(CALIB)
    mix_matrix = []
    for mix in MIXES:
        members = [m for m in mix["sources"] if m in scans]
        complete = len(members) == len(mix["sources"])
        if not members:
            continue
        for s in idx.sets:
            #: Объединение, а не сумма: одно и то же окно, найденное в двух
            #: источниках микса, — одно нарушение, а не два.
            shared: set[str] = set()
            docs = 0
            by_source = {}
            for m in members:
                cell = scans[m]["per_set"][s["key"]]
                docs += cell["overlap_docs"]
                shared |= scans[m]["matched"][s["key"]]
                by_source[m] = {"overlap_docs": cell["overlap_docs"],
                                "overlap_ngram": cell["overlap_ngram"]}
            mix_matrix.append({
                "set": s["key"], "set_role": s["role"], "mix": mix["key"],
                "mix_sources": members, "checked_sources": members,
                "complete": complete,
                "composition": {f"{r}_chunks": mix.get(f"{r}_chunks")
                                for r in ("domain", "replay")},
                "arms": arms.get(mix.get("duplicate_of") or mix["key"], []),
                "duplicate_of": mix.get("duplicate_of"),
                "overlap_docs": docs,
                "overlap_ngram": len(shared),
                "overlap_ngram_by_source": by_source,
                "verdict": ("partial" if not complete
                            else _mix_verdict(s, mix, docs, len(shared))),
            })

    #: Нарушение считается один раз — на уровне микса: инвариант ADR-025 п.1
    #: сформулирован над миксами, а клетка источника есть тот же дефект, увиденный
    #: этажом ниже. Складывать их значило бы докладывать одну утечку дважды.
    violations = [m for m in mix_matrix if m["verdict"] == "violation"]
    source_violations = [c for c in source_matrix if c["verdict"] == "violation"]
    partial = [m for m in mix_matrix if m["verdict"] == "partial"]
    if any(not m["complete"] for m in mix_matrix) and not violations:
        #: Неполный прогон не даёт права на вердикт «чисто»: непроверенный источник
        #: микса — это непроверенный микс, и молчание о нём читалось бы как ноль.
        verdict = "partial"
    else:
        verdict = "clean" if not violations else "overlap"

    report = {
        "schema": "eval-purity-matrix/1",
        "stage": "S3x",
        "date": datetime.now(timezone.utc).isoformat(),
        "adr": ["ADR-025 п.1", "ADR-025 п.2", "ADR-025 п.5", "ADR-027 пп.1/3/4/5",
                "ADR-031 п.4", "ADR-011 п.4"],
        "tool": {**own_revision(), "argv": sys.argv[1:]},
        "invariant": {
            "rule": "нуль обязателен: каждый решающий набор не пересекается ни с одним "
                    "кандидатным CPT-миксом (ADR-025 п.1)",
            "doc_overlap": "нормализованный текст документа набора (lower + схлопнутые "
                           "пробелы) как подстрока нормализованного документа источника",
            "ngram_overlap": f"окна по {WINDOW} слов внутри документа; окно, склеенное "
                             f"из двух документов, совпадением не считается",
            "window_size": WINDOW,
            "semantics_source": "tools/check_eval_set_purity.py — определения импортированы, "
                                "а не переписаны; свод обязан совпасть с гейтом числом",
            "checking_full_source": "микс — префикс потока источника: ноль по файлу строже "
                                    "нуля по префиксу",
            "doc_pairs_semantics": "overlap_docs — число пар (документ источника × документ "
                                   "набора), как в гейте",
            "per_role_threshold": {
                "strict": "ноль документов И ноль окон (ADR-025 п.1 буквально)",
                "docs_only": "ноль документов обязателен, окна называются числом "
                             "(EVAL-SETS §3.4: набор и обучение — одного семейства карточек)",
                "expected_overlap": "пересечение ожидаемо: набор снят с решающей роли "
                                    "(ADR-027 п.5), служит обратной проверкой гейта",
            },
        },
        "sets": {s["key"]: {**{k: v for k, v in s.items() if k != "path"},
                            **idx.meta[s["key"]]} for s in idx.sets},
        "sources": {s["key"]: {**{k: v for k, v in s.items() if k != "path"},
                               "sha256": scans[s["key"]]["sha256"],
                               "source_docs": scans[s["key"]]["source_docs"],
                               "seconds": scans[s["key"]]["seconds"]}
                    for s in srcs},
        "mixes": {m["key"]: {**{k: v for k, v in m.items() if k != "sources"},
                             "sources": m["sources"],
                             "arms": arms.get(m.get("duplicate_of") or m["key"], [])}
                  for m in MIXES},
        "source_matrix": source_matrix,
        "mix_matrix": mix_matrix,
        "cross_check": _cross_check(mix_matrix, idx),
        "verdict": verdict,
        #: Контракт завершённости (AD-12/ADR-023): артефакт обязан нести `status`,
        #: иначе читатель не отличает «свод чист» от «свод не досчитан».
        "status": {"clean": "complete", "overlap": "complete",
                   "partial": "partial"}[verdict],
        "violations": violations,
        "source_violations": source_violations,
        "partial": partial,
    }
    rc = {"clean": EXIT_OK, "overlap": EXIT_FAIL, "partial": EXIT_NOT_VERIFIED}[verdict]
    return report, rc


def _cell_verdict(s: dict, src: dict, cell: dict) -> str:
    """Клетка примитива: что означает пара чисел для этой роли набора и источника."""
    if s["gate"] == "expected_overlap":
        return "expected_overlap" if (cell["overlap_docs"] or cell["overlap_ngram"]) else "clean"
    if not src["gates"]:
        #: Не входит в кандидатный микс: инвариант ADR-025 п.1 на него не распространяется.
        return "informational" if (cell["overlap_docs"] or cell["overlap_ngram"]) else "clean"
    if s["gate"] == "docs_only":
        #: EVAL-SETS §3.4: доменный набор — ноль документов, окна числом. Ненулевые
        #: окна здесь ожидаемы (набор и обучение — одного семейства карточек).
        return "violation" if cell["overlap_docs"] else (
            "windows_named" if cell["overlap_ngram"] else "clean")
    return "violation" if (cell["overlap_docs"] or cell["overlap_ngram"]) else "clean"


def _mix_verdict(s: dict, mix: dict, docs: int, windows: int) -> str:
    if s["gate"] == "expected_overlap":
        return "expected_overlap" if (docs or windows) else "clean"
    if s["gate"] == "docs_only":
        return "violation" if docs else ("windows_named" if windows else "clean")
    return "violation" if (docs or windows) else "clean"


def _cross_check(mix_matrix: list[dict], idx: SetIndex) -> dict:
    """Свод против чужих отчётов гейта: те же числа или расхождение названо.

    Порог — ноль, а не «близко»: сравниваются целые счётчики документов и окон,
    полученные тем же определением совпадения. Ненулевое расхождение означает, что
    свод и гейт считают разное, и тогда верен гейт (он первоисточник определения).
    """
    by = {(m["set"], m["mix"]): m for m in mix_matrix}
    present = {s["key"] for s in idx.sets}
    out: dict[str, dict] = {}
    for rel, set_key in CROSS_CHECK:
        p = CASE_ROOT / rel
        row: dict = {"path": rel, "set": set_key}
        if set_key not in present:
            row["status"] = "not_checked (набор не в этом прогоне)"
            out[rel] = row
            continue
        if not p.is_file():
            row["status"] = "NOT-VERIFIED: отчёта нет"
            out[rel] = row
            continue
        try:
            ref = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            row["status"] = f"NOT-VERIFIED: отчёт не читается ({e})"
            out[rel] = row
            continue
        same_set_sha = (ref.get("set", {}).get("sha256") == idx.meta[set_key]["sha256"])
        comparisons = []
        for m in ref.get("overlap_matrix", []):
            name = m["mix"]
            mine = by.get((set_key, name))
            if mine is None:
                comparisons.append({"mix": name, "status": "нет в своде"})
                continue
            comparisons.append({
                "mix": name,
                "ref_docs": m["overlap_docs"], "mine_docs": mine["overlap_docs"],
                "ref_ngram": m["overlap_ngram"], "mine_ngram": mine["overlap_ngram"],
                "docs_match": m["overlap_docs"] == mine["overlap_docs"],
                "ngram_match": m["overlap_ngram"] == mine["overlap_ngram"],
            })
        row["set_sha_match"] = same_set_sha
        row["ref_set_sha256"] = ref.get("set", {}).get("sha256")
        row["mine_set_sha256"] = idx.meta[set_key]["sha256"]
        row["comparisons"] = comparisons
        hard = [c for c in comparisons
                if c.get("docs_match") is False or c.get("ngram_match") is False]
        row["status"] = ("совпало" if not hard and same_set_sha else
                         "РАСХОЖДЕНИЕ" if hard else "набор другой ревизии")
        out[rel] = row
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="S3x — сводная матрица чистоты: все наборы × все кандидатные миксы")
    ap.add_argument("--sets", default=None, help="подмножество наборов через запятую")
    ap.add_argument("--sources", default=None, help="подмножество корпусов через запятую")
    ap.add_argument("--set-path", action="append", default=None, metavar="КЛЮЧ=ПУТЬ",
                    help="перенаправить вход набора (тесты; несколько раз)")
    ap.add_argument("--source-path", action="append", default=None, metavar="КЛЮЧ=ПУТЬ",
                    help="перенаправить вход корпуса (тесты; несколько раз)")
    ap.add_argument("--report", default=None, help="куда записать отчёт (json)")
    ap.add_argument("--json", action="store_true", help="отчёт в stdout")
    args = ap.parse_args(argv)

    report, rc = build(args)
    if args.report and report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"отчёт: {out}")
    if not report:
        return rc
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return rc

    print("\n== свод чистоты измерительных наборов (ADR-025 п.1) ==")
    print("наборы: " + ", ".join(f"{k} [{v['role']}]"
                                 for k, v in report["sets"].items()))
    print("\n-- набор × корпус-источник (документы / окна) --")
    srcs = list(report["sources"])
    head = f"{'набор':18s} " + " ".join(f"{s[:14]:>16s}" for s in srcs)
    print(head)
    for s in report["sets"]:
        cells = []
        for src in srcs:
            c = next(c for c in report["source_matrix"]
                     if c["set"] == s and c["source"] == src)
            mark = {"violation": "!", "expected_overlap": "~", "windows_named": "^",
                    "informational": ".", "clean": " "}[c["verdict"]]
            cells.append(f"{c['overlap_docs']:>7d}/{c['overlap_ngram']:<7d}{mark}")
        print(f"{s:18s} " + " ".join(f"{c:>16s}" for c in cells))
    print("  ! нарушение  ~ ожидаемо (набор снят)  ^ ноль документов, "
          "окна названы  . вне инварианта")

    print("\n-- набор × кандидатный микс (документы / окна) --")
    mixes = list(report["mixes"])
    print(f"{'набор':18s} " + " ".join(f"{m[:14]:>16s}" for m in mixes))
    for s in report["sets"]:
        cells = []
        for mix in mixes:
            m = next((x for x in report["mix_matrix"]
                      if x["set"] == s and x["mix"] == mix), None)
            if m is None:
                cells.append(f"{'—':>16s}")
                continue
            ngr = m["overlap_ngram"]
            mark = {"violation": "!", "expected_overlap": "~", "windows_named": "^",
                    "partial": "?", "clean": " "}[m["verdict"]]
            cells.append(f"{m['overlap_docs']:>7d}/{ngr:<7d}{mark}")
        print(f"{s:18s} " + " ".join(f"{c:>16s}" for c in cells))
    print("  ! нарушение  ~ ожидаемо (набор снят)  ^ ноль документов, "
          "окна названы  ? прогон неполон")

    print("\n-- сверка с чужими отчётами гейта --")
    for rel, row in report["cross_check"].items():
        print(f"  {row['status']:22s} {rel} → {row.get('set')}")
    print(f"\nвердикт: {report['verdict'].upper()}"
          + (f"; нарушений {len(report['violations'])}" if report["violations"] else ""))
    return rc


if __name__ == "__main__":
    sys.exit(main())
