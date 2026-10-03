#!/usr/bin/env python3
"""S3ac — язык корпуса против языка ответа: из чего собран CPT-поток v12r.

Зачем. Финальный CPT-чекпойнт прошёл PPL-критерий (K1 ×0.9848, K2 ×1.0671,
ADR-032), но в свободной генерации отвечает по-английски: доля кириллицы 0.123
против 0.888 у базы и 0.87 у instruct (S3ab, ``evidence/s3ab-cpt-probes.json``).
Режим при этом приобретён (4/5 проб с ``<think>``/``<tool_call>``, у базы 0/5).
Профиль «режим есть / язык потерян» воспроизводится у всех 25 %-рук, «режима нет /
язык цел» — у всех 50 %-рук: сдвиг привнесён обучением на миксе ``v12r`` (домен
75 % / replay 25 %), а не длиной финального прогона (ADR-035).

Гипотеза, которая здесь проверяется числом. Домен-часть микса состоит не только из
русскоязычных концепт-карт: в неё влиты траектории учителя (10881 шт, 39.2 %
доменных токенов). Если рассуждения внутри этих траекторий англоязычны, то модель
видит ровно тот шаблон, который воспроизводит на генерации: ``<|im_start|>assistant``
→ ``<think>`` → английский текст. Предсказание русских текстов (PPL) при этом не
страдает: языковой режим рассуждений — не то же самое, что языковая модель текста.

Что делает инструмент. Считает языковой состав **по компонентам микса и по весам**,
а не «корпус целиком»:

1. ``--mode text`` — построчный разбор исходных текстов (домен и replay) конечным
   автоматом по маркерам: концепт-карты / ход system / ход user / ход assistant /
   внутри ``<think>`` / ``<tool_call>`` / ``<tool_response>``. Источники читаются
   потоком, только чтение (AD-4), копий не делается.
2. ``--mode stream`` — разбор **того, что реально училось**: строки преток-кэша
   ``cpt_corpus_v12r_8192_qwen25.npy`` декодируются обратно в текст и разбираются
   тем же автоматом; провенанс каждой строки (домен / replay) восстанавливается
   сопоставлением с доменным кэшем по хешу строки — то есть 7332/2444 строки
   проверяются, а не подразумеваются. Граница «карты / траектории» внутри домена
   берётся по номеру токена первого хода траектории (``<|im_start|>`` = id 151644)
   в самом доменном кэше — расщепление точное, без приближения по символам.
3. ``--sft`` — тот же разбор по датасету SFT стадии (тот же файл, что подан на стенд).
   Нужен, чтобы вопрос «лечится ли это пост-тренировкой» имел число, а не надежду: если
   SFT-корпус несёт тот же англоязычный ``<think>`` и ``<think>`` входит в лосс (маска
   проверяется по коду пайплайна), SFT закрепит режим, а не снимет его.
4. Метрика языка — та же, что у прибора S3ab (``probe_control.degenerate_metrics``):
   ``cyrillic_share = кириллических букв / (кириллических + латинских)``. Иначе
   «0.123 у генерации» и «столько-то у корпуса» были бы двумя разными арифметиками.

Ожидаемая доля кириллицы потока считается по буквам (пул), а не по документам: у
карт и у траекторий разная плотность токенов, и среднее по документам соврало бы.

Наблюдаемое (0.123 у финала) в отчёт не вписывается константой: если каталог пробы
S3ab доступен, ответы перемеряются тем же автоматом (``--observed-probe``), и рядом
печатается pooled-число против среднего по пробам. Если каталога нет — берётся
цитата свода S3ab, и это в отчёте названо (``observed.remeasured = false``).

Коды возврата::

    0 — отчёт собран
    1 — микс не тот, что подписан (провенанс строк разошёлся с манифестом v12r)
    2 — NOT-VERIFIED: нет входа (файл корпуса, кэш, токенизатор)

Запуск::

    python3 tools/corpus_language_probe.py --mode both \
        --out evidence/s3ac-corpus-language.json

Тексты генерации в отчёт не попадают (AD-8): публикуются только числа.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from codecs import getincrementaldecoder
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

GB10_SHARED = Path("/home/user/gb10-shared")
DEFAULTS = {
    "domain": GB10_SHARED / "datasets/cpt_corpus_v10.1.txt",
    "replay": GB10_SHARED / "datasets/general_replay_ru.txt",
    "mix_cache": GB10_SHARED / "datasets/tok/cpt_corpus_v12r_8192_qwen25.npy",
    "domain_cache": GB10_SHARED / "datasets/tok/cpt_corpus_v10.1_8192_qwen25.npy",
    "tokenizer": Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B"
                 "/snapshots/060db6499f32faf8b98477b0a26969ef7d8b9987/tokenizer.json",
    #: Датасет SFT стадии (ADR-033) — тот же файл, что подан на стенд. Нужен, чтобы
    #: ответ «лечится ли SFT» опирался на число, а не на надежду: если SFT-корпус
    #: несёт тот же англоязычный <think>, пост-тренировка закрепит сдвиг, а не снимет.
    "sft": GB10_SHARED / "datasets/sft_train_v12.jsonl",
}
#: Проба S3ab (каталог прогона живёт в ветке стадии; если он есть — ответы
#: перемеряются тем же автоматом, иначе берётся цитата свода).
DEFAULT_OBSERVED_PROBE = Path(
    "/home/user/.arch-ml/worktrees/spine-aiml-a366f5f2a510b02d/laguna-eval-set"
    "/кейсы/laguna-compact/runs/s3ab-probes-20260917/probe_control.json")

#: Микс v12r по манифесту (gb10-shared/cpt_corpus_v12r.txt): 7332 доменных чанка
#: + 2444 replay-чанка по 8192 токена, chunk-level shuffle seed=42.
CHUNK_TOKENS = 8192
MIX_CHUNKS = {"domain": 7332, "replay": 2444}
SPECIAL_TOKENS = ["<think>", "</think>", "<tool_call>", "</tool_call>",
                  "<tool_response>", "</tool_response>", "<reasoning>", "</reasoning>"]
IM_START_ID = 151644
#: Сколько символов после ``<|im_start|>assistant\\n`` считать «генеративной позицией».
#: Это тот отрезок, который модель порождает первой, получив вопрос; у проб S3ab
#: ответ целиком 218…305 слов, то есть первые 200 символов — начало рассуждения.
PREFIX_CHARS = 200
#: Бюджет генерации пробы S3ab (max_new_tokens) — с ним сравнивается длина
#: <think>-фрагмента: если медиана больше бюджета, окно пробы не выходит из
#: рассуждения, то есть из англоязычной части хода.
PROBE_NEW_TOKENS = 384
#: Хеши входов сборки микса — цитата манифеста ``gb10-shared/datasets/cpt_corpus_v12r50.txt``
#: (и ``data/mix-v12r50-build.json``): те же два файла, из которых собран домен и replay
#: v12r. Если файл на диске разошёлся с манифестом, текстовый разбор описывал бы не тот
#: корпус, что учился, — это отказ, а не «мелкое расхождение».
EXPECTED_SOURCE_SHA256 = {
    "domain": "1765628e6bde4c516068305b9d150196e59328550289ba98f9bc04683c27b92c",
    "replay": "6497022d24f25ec5250df7870807418f23474cf2c9a8a2554e04d6d552057063",
}
#: Наблюдаемое (S3ab, среднее по 5 пробам финального чекпойнта).
OBSERVED_GENERATION_CYRILLIC = 0.123
OBSERVED_BASE_CYRILLIC = 0.888
OBSERVED_INSTRUCT_CYRILLIC = 0.87

RE_CYR = re.compile(r"[а-яА-ЯёЁ]")
RE_LAT = re.compile(r"[a-zA-Z]")
RE_WORD = re.compile(r"[A-Za-z]+|[А-Яа-яЁё]+")
#: Подсчёт букв одним C-проходом (translate), а не тремя findall: на 80M символов
#: кэша это разница в минуты. Состав тот же, что у RE_CYR/RE_LAT.
_LETTERS = {ord(c): "\x01" for c in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
                                  "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"}
_LETTERS.update({ord(c): "\x02" for c in "abcdefghijklmnopqrstuvwxyz"
                                      "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
#: Маркеры формата v12. Роль берётся только в строгой форме шаблона чата
#: (``<|im_start|>system\\n``): упоминания маркеров внутри карт («используй
#: ``<think>`` для рассуждений») не должны переключать область.
RE_MARK = re.compile(
    r"<\|im_start\|>(system|user|assistant)\n"
    r"|<\|im_end\|>|<think>|</think>|<tool_call>|</tool_call>"
    r"|<tool_response>|</tool_response>")

THINK_EN_MAX, THINK_RU_MIN = 0.05, 0.5
#: Порог «рассуждение русскоязычно» — по большинству букв. Ниже него рассуждение
#: считается англоязычным, даже если это не «почти ноль» (0.22 у SFT против 0.05 у CPT).
REASONING_RU_MIN = 0.5
#: Порог «поток в целом русский» и «генеративная позиция английская» для вердикта.
STREAM_RU_MIN = 0.5
CARRIER_MAX = 0.15


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


class Lang:
    """Счётчик языка: буквы по алфавитам, слова по письменности, объём."""

    __slots__ = ("chars", "cyr", "lat", "ru_words", "en_words", "spans")

    def __init__(self) -> None:
        self.chars = 0
        self.cyr = 0
        self.lat = 0
        self.ru_words = 0
        self.en_words = 0
        self.spans = 0

    def feed(self, text: str) -> None:
        if not text:
            return
        self.chars += len(text)
        mapped = text.translate(_LETTERS)
        self.cyr += mapped.count("\x01")
        self.lat += mapped.count("\x02")
        for w in RE_WORD.findall(text):
            if w[0].isascii():
                self.en_words += 1
            else:
                self.ru_words += 1

    @property
    def letters(self) -> int:
        return self.cyr + self.lat

    @property
    def words(self) -> int:
        return self.ru_words + self.en_words

    def share(self) -> float:
        """Доля кириллицы — определение прибора S3ab (не наше)."""
        return self.cyr / max(self.letters, 1)

    def en_share(self) -> float:
        return self.en_words / max(self.words, 1)

    def as_dict(self, tokens: int | None = None, weight: float | None = None) -> dict:
        out = {
            "chars": self.chars,
            "letters": self.letters,
            "cyrillic_letters": self.cyr,
            "latin_letters": self.lat,
            "words": self.words,
            "ru_words": self.ru_words,
            "en_words": self.en_words,
            "cyrillic_share": round(self.share(), 4),
            "english_word_share": round(self.en_share(), 4),
            "think_fragments": self.spans,
        }
        if tokens is not None:
            out["tokens"] = tokens
        if weight is not None:
            out["weight"] = round(weight, 4)
        return out

    def merge(self, other: "Lang") -> None:
        self.chars += other.chars
        self.cyr += other.cyr
        self.lat += other.lat
        self.ru_words += other.ru_words
        self.en_words += other.en_words
        self.spans += other.spans


class SpanStats:
    """Инвентарь ``<think>``-фрагментов: шаблонность формата, язык, длина.

    Нужен, чтобы ответить не «трассы есть», а «трассы однотипны»: если открытие
    фрагмента у тысяч траекторий совпадает дословно, модель выучивает формат
    целиком и сразу, а не собирает его из примеров.
    """

    HEAD_CHARS = 48

    def __init__(self) -> None:
        self.lengths: list[int] = []
        self.token_lengths: list[int] = []
        self.heads: Counter = Counter()
        self.first_words: Counter = Counter()
        self.hashes: Counter = Counter()
        self.en = 0
        self.mixed = 0
        self.ru = 0
        self.cyr = 0
        self.lat = 0

    def close(self, lang: Lang, head: str, digest: str, tokens: int | None = None) -> None:
        self.lengths.append(lang.chars)
        if tokens is not None:
            self.token_lengths.append(tokens)
        self.cyr += lang.cyr
        self.lat += lang.lat
        self.heads[head] += 1
        first = RE_WORD.search(head)
        if first:
            self.first_words[first.group(0).lower()] += 1
        self.hashes[digest] += 1
        share = lang.share()
        if share < THINK_EN_MAX:
            self.en += 1
        elif share > THINK_RU_MIN:
            self.ru += 1
        else:
            self.mixed += 1

    def as_dict(self) -> dict:
        n = len(self.lengths)
        if not n:
            return {"think_fragments": 0}
        lens = sorted(self.lengths)
        dup = sum(c for c in self.hashes.values() if c > 1)
        top_head, top_head_n = (self.heads.most_common(1)[0] if self.heads else ("", 0))
        top_hash, top_hash_n = (self.hashes.most_common(1)[0] if self.hashes else ("", 0))
        out = {
            "think_fragments": n,
            "think_cyrillic_share": round(self.cyr / max(self.cyr + self.lat, 1), 4),
            "think_chars": sum(self.lengths),
            "length_chars": {"p10": lens[n // 10], "median": lens[n // 2],
                             "p90": lens[(9 * n) // 10], "max": lens[-1]},
            "language_split": {
                "english_share": round(self.en / n, 4),
                "mixed_share": round(self.mixed / n, 4),
                "russian_share": round(self.ru / n, 4),
                "thresholds": {"english_below": THINK_EN_MAX, "russian_above": THINK_RU_MIN},
            },
            "template": {
                "distinct_fragments": len(self.hashes),
                "duplicate_fragment_share": round(dup / n, 4),
                "top_fragment_share": round(top_hash_n / n, 4),
                "top_fragment_hash": top_hash,
                "distinct_openings": len(self.heads),
                "top_opening_share": round(top_head_n / n, 4),
                "top_opening": top_head,
                "top_first_words": [[w, c] for w, c in self.first_words.most_common(5)],
            },
        }
        if self.token_lengths:
            tl = sorted(self.token_lengths)
            m = len(tl)
            over = sum(1 for t in tl if t > PROBE_NEW_TOKENS)
            out["tokens"] = {
                "counted_fragments": m,
                "median": tl[m // 2], "p10": tl[m // 10], "p90": tl[(9 * m) // 10],
                "share_longer_than_probe_budget": round(over / m, 4),
                "probe_budget_tokens": PROBE_NEW_TOKENS,
                "note": ("длина фрагмента в токенах против бюджета пробы S3ab: если "
                         "медиана больше бюджета, окно генерации целиком лежит внутри "
                         "рассуждения"),
            }
        return out


class Scanner:
    """Конечный автомат по маркерам формата v12.

    Область (region) — то, чей текст сейчас читается: ``cards`` (концепт-карты,
    базовое состояние), ``system``/``user``/``assistant`` (ходы траектории) или
    ``replay`` (общий язык). Внутри хода assistant отдельно копится ``<think>``
    и «остальное» (``asst_answer``) — именно это разделение и есть предмет
    диагностики: рассуждение против ответа.
    """

    def __init__(self, base_region: str = "cards", collect_spans: bool = False,
                 span_tokenizer=None) -> None:
        self.span_tokenizer = span_tokenizer
        self._span_text: list[str] = []
        self.base = base_region
        self.region = base_region
        self.in_think = False
        self.tool: str | None = None
        self.collect = collect_spans
        self.acc: dict[str, Lang] = {}
        self.sub: dict[str, Lang] = {}
        self.think_stats = SpanStats()
        self.diag: Counter = Counter()
        self.asst_turns = 0
        #: Ход assistant начинается с <think> почти всегда; такие фрагменты
        #: считаются отдельно — их язык не зависит от парности маркеров-упоминаний.
        self.initial_spans = 0
        self._turn_nonws = 0
        self._initial = False
        self._head: list[str] = []
        self._hasher = hashlib.blake2b(digest_size=8)
        self._span_lang = Lang()
        self._prefix_left = 0

    def _lang(self, store: dict, key: str) -> Lang:
        if key not in store:
            store[key] = Lang()
        return store[key]

    def feed(self, text: str) -> None:
        pos = 0
        for m in RE_MARK.finditer(text):
            self._text(text[pos:m.start()])
            self._marker(m)
            pos = m.end()
        self._text(text[pos:])

    def _text(self, seg: str) -> None:
        if not seg:
            return
        self._lang(self.acc, self.region).feed(seg)
        if self.region == "assistant":
            if self.in_think:
                self._lang(self.sub, "asst_think").feed(seg)
                if self._initial:
                    self._lang(self.sub, "asst_think_initial").feed(seg)
                if self.collect:
                    self._span_lang.feed(seg)
                    self._hasher.update(seg.encode("utf-8"))
                    if len(self._head) < SpanStats.HEAD_CHARS:
                        self._head.append(seg)
                    if self.span_tokenizer is not None:
                        self._span_text.append(seg)
            else:
                self._lang(self.sub, "asst_answer").feed(seg)
                self._turn_nonws += len(seg.strip())
            if self._prefix_left > 0:
                take = seg[: self._prefix_left]
                self._lang(self.sub, "asst_prefix").feed(take)
                self._prefix_left -= len(take)
        if self.tool:
            self._lang(self.sub, self.tool).feed(seg)

    def _marker(self, m: re.Match) -> None:
        raw = m.group(0)
        role = m.group(1)
        if role:
            self.region = role
            self.in_think = False
            self.tool = None
            if role == "assistant":
                self.asst_turns += 1
                self._prefix_left = PREFIX_CHARS
                self._turn_nonws = 0
                if self.collect:
                    self._hasher = hashlib.blake2b(digest_size=8)
                    self._span_lang = Lang()
                    self._head = []
            return
        if raw == "<|im_end|>":
            if self.in_think:
                self.diag["think_unclosed_at_turn_end"] += 1
                self._close_think()
            self.region = self.base
            self.tool = None
            return
        if raw == "<think>":
            if self.region != "assistant":
                # упоминание маркера в карте или в системном промпте — не фрагмент
                self.diag["think_marker_outside_assistant"] += 1
                return
            if self.in_think:
                self.diag["think_open_nested"] += 1
                return
            self.in_think = True
            self._initial = (self._turn_nonws == 0)
            if self._initial:
                self.initial_spans += 1
            self._lang(self.acc, self.region).spans += 1
            if self.collect:
                self._hasher = hashlib.blake2b(digest_size=8)
                self._span_lang = Lang()
                self._head = []
            return
        if raw == "</think>":
            if not self.in_think:
                self.diag["think_close_unpaired"] += 1
                return
            self._close_think()
            return
        if raw in ("<tool_call>", "<tool_response>"):
            name = raw[1:-1]
            if self.tool:
                self.diag[f"{name}_open_inside_{self.tool}"] += 1
            self.tool = name
            self._lang(self.sub, name).spans += 1
            return
        if raw in ("</tool_call>", "</tool_response>"):
            name = raw[2:-1]
            if self.tool == name:
                self.tool = None
            else:
                self.diag[f"{name}_close_unpaired"] += 1
            return

    def _close_think(self) -> None:
        self.in_think = False
        self._initial = False
        if self.collect:
            head = "".join(self._head)[: SpanStats.HEAD_CHARS]
            ntokens = None
            if self.span_tokenizer is not None:
                ntokens = len(self.span_tokenizer.encode(
                    "".join(self._span_text), add_special_tokens=False).ids)
            self.think_stats.close(self._span_lang, head, self._hasher.hexdigest(), ntokens)
            self._span_text = []

    def finish(self) -> None:
        if self.in_think:
            self.diag["think_unclosed_at_eof"] += 1
            self._close_think()


def scan_text_file(path: Path, base_region: str, collect: bool,
                   block: int = 1 << 23) -> Scanner:
    """Потоковый разбор файла: блоки + хвост, автомат переносится между блоками."""
    sc = Scanner(base_region=base_region, collect_spans=collect)
    dec = getincrementaldecoder("utf-8")(errors="replace")
    carry = ""
    with path.open("rb") as f:
        for raw in iter(lambda: f.read(block), b""):
            buf = carry + dec.decode(raw)
            last = 0
            for m in RE_MARK.finditer(buf):
                sc.feed(buf[last:m.start()] + m.group(0))
                last = m.end()
            rest = buf[last:]
            # хвост без маркеров держим только на длину возможного маркера
            if len(rest) > 64:
                sc.feed(rest[:-64])
                rest = rest[-64:]
            carry = rest
        carry += dec.decode(b"", final=True)
        sc.feed(carry)
    sc.finish()
    return sc


def _row_hash(row) -> str:
    return hashlib.blake2b(row.tobytes(), digest_size=8).hexdigest()


def load_tokenizer(path: Path):
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(str(path))
    tok.add_special_tokens(SPECIAL_TOKENS)
    if tok.encode("<|im_start|>").ids != [IM_START_ID]:
        raise RuntimeError("токенизатор не даёт id 151644 для <|im_start|> — "
                           "кэш и разбор были бы о разном")
    return tok


def probe_stream(cfg: argparse.Namespace, tok) -> dict:
    """Разбор того, что реально училось: строки преток-кэша микса."""
    import numpy as np

    ct = cfg.chunk_tokens
    expect = {"domain": cfg.expect_domain_chunks, "replay": cfg.expect_replay_chunks}
    dom = np.load(str(cfg.domain_cache), mmap_mode="r")
    mix = np.load(str(cfg.mix_cache), mmap_mode="r")
    if dom.shape[1] != ct or mix.shape[1] != ct:
        raise RuntimeError(f"чанк не {ct} токенов: dom {dom.shape}, mix {mix.shape}")

    dom_index = {}
    for i in range(dom.shape[0]):
        dom_index.setdefault(_row_hash(dom[i]), i)
    prov: list[tuple[str, int]] = []
    for j in range(mix.shape[0]):
        h = _row_hash(mix[j])
        if h in dom_index:
            prov.append(("domain", dom_index[h]))
        else:
            prov.append(("replay", -1))
    n_dom = sum(1 for p, _ in prov if p == "domain")
    n_rep = len(prov) - n_dom
    same_chunks = n_dom == expect["domain"] and n_rep == expect["replay"]
    if not same_chunks:
        # Провенанс проверяется первым: без него и граница домена, и веса описывали бы
        # не тот поток, что учился, — а числа выглядели бы правдоподобно.
        raise RuntimeError(
            f"провенанс строк кэша не совпал с манифестом v12r "
            f"(домен {n_dom} вместо {expect['domain']}, "
            f"replay {n_rep} вместо {expect['replay']})")

    # Граница «карты / траектории» в доменном потоке — по номеру токена первого
    # хода траектории. Ищем в самом кэше: <|im_start|> + "system\n".
    sys_ids = tok.encode("system\n").ids
    t0 = None
    for i in range(dom.shape[0]):
        row = dom[i]
        for pos in np.flatnonzero(row == IM_START_ID):
            nxt = row[pos + 1: pos + 1 + len(sys_ids)]
            if list(nxt) == sys_ids:
                t0 = int(i) * ct + int(pos)
                break
        if t0 is not None:
            break
    if t0 is None:
        # без границы домен не расщепляется — отказ, а не «домен целиком»
        raise RuntimeError("в доменном кэше не найден ход траектории "
                           "(<|im_start|>system) — расщепление домена невозможно")

    dom_tokens = dom.shape[0] * ct
    rep_tokens = n_rep * ct
    total_tokens = dom_tokens + rep_tokens

    sc_dom = Scanner(base_region="cards", collect_spans=True, span_tokenizer=tok)
    dom_rows = sorted(i for p, i in prov if p == "domain")
    for k, i in enumerate(dom_rows):
        sc_dom.feed(tok.decode(dom[i].tolist(), skip_special_tokens=False))
    sc_dom.finish()

    sc_rep = Scanner(base_region="replay", collect_spans=False)
    rep_rows = [j for j, (p, _) in enumerate(prov) if p == "replay"]
    for j in rep_rows:
        sc_rep.feed(tok.decode(mix[j].tolist(), skip_special_tokens=False))
    sc_rep.finish()

    cards = sc_dom.acc.get("cards", Lang())
    traj = Lang()
    for k in ("system", "user", "assistant"):
        traj.merge(sc_dom.acc.get(k, Lang()))
    traj.spans = sc_dom.acc.get("assistant", Lang()).spans
    replay = sc_rep.acc.get("replay", Lang())

    comps = []
    for name, lang, tokens in (("domain_cards", cards, t0),
                               ("domain_trajectories", traj, dom_tokens - t0),
                               ("replay_general", replay, rep_tokens)):
        d = lang.as_dict(tokens=tokens, weight=tokens / max(total_tokens, 1))
        d["component"] = name
        d["source"] = ("голова доменного потока до первого <|im_start|>system"
                       if name == "domain_cards" else
                       "хвост доменного потока от первого <|im_start|>system"
                       if name == "domain_trajectories" else
                       "первые 2444 чанка потока general_replay_ru.txt")
        comps.append(d)

    pooled = Lang()
    for lang in (cards, traj, replay):
        pooled.merge(lang)
    pooled.spans = sc_dom.acc.get("assistant", Lang()).spans

    asst = sc_dom.acc.get("assistant", Lang())
    asst_think = sc_dom.sub.get("asst_think", Lang())
    asst_answer = sc_dom.sub.get("asst_answer", Lang())
    asst_prefix = sc_dom.sub.get("asst_prefix", Lang())

    return {
        "mix": {
            "mix_cache": str(cfg.mix_cache),
            "domain_cache": str(cfg.domain_cache),
            "rows_domain": n_dom, "rows_replay": n_rep,
            "rows_domain_expected": expect["domain"],
            "rows_replay_expected": expect["replay"],
            "provenance_matches_manifest": same_chunks,
            "weight_domain": round(dom_tokens / max(total_tokens, 1), 4),
            "weight_replay": round(rep_tokens / max(total_tokens, 1), 4),
            "chunk_tokens": ct,
            "domain_boundary_token": t0,
            "domain_boundary_note": "номер токена первого <|im_start|>system в доменном кэше",
        },
        "components": comps,
        "stream_pooled": pooled.as_dict(tokens=total_tokens, weight=1.0),
        "assistant_turn": {
            "turns": sc_dom.asst_turns,
            "whole": asst.as_dict(),
            "think_part": asst_think.as_dict(),
            "answer_part": asst_answer.as_dict(),
            "prefix_first_%d_chars" % PREFIX_CHARS: asst_prefix.as_dict(),
            "turn_initial_think": sc_dom.sub.get("asst_think_initial", Lang()).as_dict(),
            "turn_initial_spans": sc_dom.initial_spans,
            "note": ("ход assistant разделён на <think>-часть и остальное (ответ); "
                     "prefix — первые %d символов после <|im_start|>assistant, то есть "
                     "та позиция, с которой начинается генерация; turn_initial — "
                     "фрагменты, открывающиеся в самом начале хода (их язык не зависит "
                     "от парности маркеров-упоминаний)" % PREFIX_CHARS),
        },
        "think_inventory": sc_dom.think_stats.as_dict(),
        "tool_fragments": {name: sc_dom.sub.get(name, Lang()).as_dict()
                           for name in ("tool_call", "tool_response")},
        "diagnostics": dict(sc_dom.diag),
    }


def probe_text(cfg: argparse.Namespace) -> dict:
    """Языковой состав исходных текстов — независимый от кэша прибор."""
    dom = scan_text_file(Path(cfg.domain), "cards", collect=True)
    rep = scan_text_file(Path(cfg.replay), "replay", collect=False)
    cards = dom.acc.get("cards", Lang())
    traj = Lang()
    for k in ("system", "user", "assistant"):
        traj.merge(dom.acc.get(k, Lang()))
    traj.spans = dom.acc.get("assistant", Lang()).spans
    replay = rep.acc.get("replay", Lang())
    out: dict = {"components": []}
    for name, lang, src in (("domain_cards", cards, cfg.domain),
                            ("domain_trajectories", traj, cfg.domain),
                            ("replay_general", replay, cfg.replay)):
        d = lang.as_dict()
        d["component"] = name
        d["source_file"] = str(src)
        out["components"].append(d)
    asst = dom.acc.get("assistant", Lang())
    out["assistant_turn"] = {
        "turns": dom.asst_turns,
        "whole": asst.as_dict(),
        "think_part": dom.sub.get("asst_think", Lang()).as_dict(),
        "answer_part": dom.sub.get("asst_answer", Lang()).as_dict(),
        "prefix_first_%d_chars" % PREFIX_CHARS:
            dom.sub.get("asst_prefix", Lang()).as_dict(),
        "turn_initial_think": dom.sub.get("asst_think_initial", Lang()).as_dict(),
        "turn_initial_spans": dom.initial_spans,
    }
    out["think_inventory"] = dom.think_stats.as_dict()
    out["diagnostics"] = dict(dom.diag)
    d_sha, r_sha = sha256_file(cfg.domain), sha256_file(cfg.replay)
    out["sources"] = {
        "domain_file": str(cfg.domain), "replay_file": str(cfg.replay),
        "domain_sha256": d_sha, "replay_sha256": r_sha,
        "domain_sha256_expected": EXPECTED_SOURCE_SHA256["domain"],
        "replay_sha256_expected": EXPECTED_SOURCE_SHA256["replay"],
        "matches_mix_manifest": (d_sha == EXPECTED_SOURCE_SHA256["domain"]
                                 and r_sha == EXPECTED_SOURCE_SHA256["replay"]),
        "domain_chars": cards.chars + traj.chars,
        "replay_chars": replay.chars,
    }
    return out


#: Метки SFT в пайплайне: начало обучения — с хода assistant (system/user замаскированы),
#: а <think> внутри хода остаётся целевым токеном. Проверяется по коду, а не по памяти:
#: от этого зависит, закрепляет ли SFT англоязычное рассуждение или проходит мимо него.
SFT_MASK_MARKERS = ("labels[:first_asst + 2] = -100", "labels[mask == 0] = -100")


def check_sft_loss_mask(path: Path) -> dict:
    if not path.exists():
        return {"checked": False, "pipeline": str(path),
                "note": "пайплайна нет — входит ли <think> в лосс SFT, не проверено"}
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [i + 1 for i, l in enumerate(text.splitlines()) if "labels[:first_asst" in l]
    tr_lines = [i + 1 for i, l in enumerate(text.splitlines()) if "in_tr" in l]
    return {
        "checked": bool(lines),
        "pipeline": str(path),
        "sha256": sha256_file(path),
        "mask_start_lines": lines,
        "tool_response_mask_lines": tr_lines[:3],
        "thinking_is_a_trained_target": bool(lines),
        "reading": ("метки начинаются с хода assistant, <think> не маскируется "
                    "(исключаются только system/user и tool_response)"),
    }


def probe_sft(cfg: argparse.Namespace) -> dict | None:
    """Язык SFT-корпуса: несёт ли пост-тренировка тот же режим рассуждения.

    Ход assistant разбирается тем же автоматом, что и траектории CPT (роли
    восстанавливаются из сообщений, а не из шаблона чата): иначе «SFT исправит»
    осталось бы надеждой. Файл читается построчно, только чтение (AD-4).
    """
    if not cfg.sft:
        return None
    path = Path(cfg.sft)
    if not path.exists():
        return {"available": False, "path": str(path),
                "note": "датасета SFT нет — вопрос «лечит ли SFT» числом не закрыт"}
    sc = Scanner(base_region="sft", collect_spans=False)
    lines = 0
    msgs = Counter()
    with path.open(encoding="utf-8") as f:
        for line in f:
            if cfg.sft_limit and lines >= cfg.sft_limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            lines += 1
            for m in item.get("messages", []):
                role = m.get("role")
                content = m.get("content") or ""
                if not isinstance(content, str):
                    continue
                msgs[role] += 1
                sc.feed(f"<|im_start|>{role}\n{content}<|im_end|>\n")
    sc.finish()
    asst = sc.acc.get("assistant", Lang())
    return {
        "available": True,
        "loss_mask": check_sft_loss_mask(Path(cfg.pipeline)),
        "path": str(path),
        "sha256": sha256_file(path),
        "records": lines,
        "messages_by_role": dict(msgs),
        "limited": bool(cfg.sft_limit),
        "assistant_turn": {
            "turns": sc.asst_turns,
            "whole": asst.as_dict(),
            "think_part": sc.sub.get("asst_think", Lang()).as_dict(),
            "answer_part": sc.sub.get("asst_answer", Lang()).as_dict(),
            "prefix_first_%d_chars" % PREFIX_CHARS: sc.sub.get("asst_prefix", Lang()).as_dict(),
            "turn_initial_think": sc.sub.get("asst_think_initial", Lang()).as_dict(),
            "turn_initial_spans": sc.initial_spans,
        },
        "note": ("доля кириллицы в think-части хода assistant: если она так же низка, "
                 "как в CPT-траекториях, SFT воспроизводит режим, а не снимает его"),
    }


def observe_generation(path: Path | None) -> dict:
    """Перемерить ответы пробы S3ab тем же автоматом (если каталог прогона есть)."""
    if path is None or not Path(path).exists():
        return {"source": "цитата свода S3ab (каталог прогона недоступен)",
                "cfinal_cyrillic_share": OBSERVED_GENERATION_CYRILLIC,
                "base_cyrillic_share": OBSERVED_BASE_CYRILLIC,
                "instruct_cyrillic_share": OBSERVED_INSTRUCT_CYRILLIC,
                "remeasured": False}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {"source": str(path), "sha256": sha256_file(Path(path)), "remeasured": True}
    for state in ("cfinal", "base", "instruct"):
        runs = data.get("runs", {}).get(state, {}).get("probes", [])
        pooled, prefix, think_only, n_think = Lang(), Lang(), Lang(), 0
        for pr in runs:
            text = pr.get("response", "")
            pooled.feed(text)
            i = text.find("<think>")
            if i >= 0:
                n_think += 1
                j = text.find("</think>", i)
                think_only.feed(text[i + 7: j if j > 0 else len(text)])
                prefix.feed(text[i + 7: i + 7 + PREFIX_CHARS])
            else:
                prefix.feed(text[:PREFIX_CHARS])
        out[state] = {
            "probes": len(runs),
            "cyrillic_share": round(pooled.share(), 4),
            "prefix_cyrillic_share": round(prefix.share(), 4),
            "think_cyrillic_share": round(think_only.share(), 4),
            "probes_with_think": n_think,
            "chars": pooled.chars,
        }
    return out


def build_verdict(stream: dict, observed: dict, sft: dict | None = None) -> dict:
    """Вердикт считается по числам с объявленными порогами, а не по вкусу.

    Механизм подтверждается четырьмя проверками (M1…M4), каждая — с числом:

    * M1 «корпус не англоязычен целиком»: кириллица потока ≥ ``STREAM_RU_MIN``;
    * M2 «английский стоит в генеративной позиции»: кириллица первых
      ``PREFIX_CHARS`` символов хода assistant ≤ ``CARRIER_MAX``;
    * M3 «английский сконцентрирован в рассуждении, а не размазан»: кириллица
      траекторий < кириллицы карт, а кириллица ``<think>`` ниже обеих;
    * M4 «окно пробы не выходит из рассуждения»: медианный ``<think>`` длиннее
      бюджета пробы ``PROBE_NEW_TOKENS``.
    """
    comps = {c["component"]: c for c in stream["components"]}
    stream_cyr = stream["stream_pooled"]["cyrillic_share"]
    asst = stream["assistant_turn"]
    think_cyr = asst["turn_initial_think"]["cyrillic_share"]
    answer_cyr = asst["answer_part"]["cyrillic_share"]
    prefix_cyr = asst["prefix_first_%d_chars" % PREFIX_CHARS]["cyrillic_share"]
    rep_cyr = comps["replay_general"]["cyrillic_share"]
    cards_cyr = comps["domain_cards"]["cyrillic_share"]
    traj_cyr = comps["domain_trajectories"]["cyrillic_share"]
    inv = stream["think_inventory"]
    think_en = inv.get("language_split", {}).get("english_share")
    tok_stats = inv.get("tokens") or {}
    median_think_tokens = tok_stats.get("median")
    observed_cyr = OBSERVED_GENERATION_CYRILLIC
    observed_pooled = observed.get("cfinal", {}).get("cyrillic_share") \
        if observed.get("remeasured") else None

    m1 = stream_cyr >= STREAM_RU_MIN
    m2 = prefix_cyr <= CARRIER_MAX
    m3 = traj_cyr < cards_cyr and think_cyr < traj_cyr
    m4 = bool(median_think_tokens) and median_think_tokens > PROBE_NEW_TOKENS
    checks = {
        "M1_corpus_not_english": {
            "holds": m1, "stream_cyrillic": round(stream_cyr, 4),
            "threshold": STREAM_RU_MIN,
            "reading": "поток в целом не англоязычен — «корпус английский» тезисом быть не может"},
        "M2_english_at_generative_position": {
            "holds": m2, "prefix_cyrillic": prefix_cyr, "threshold": CARRIER_MAX,
            "reading": (f"первые {PREFIX_CHARS} символов хода assistant в корпусе — "
                        f"английские: это ровно та позиция, из которой начинается генерация")},
        "M3_english_concentrated_in_reasoning": {
            "holds": m3, "think_cyrillic": think_cyr, "trajectories_cyrillic": traj_cyr,
            "cards_cyrillic": cards_cyr, "replay_cyrillic": rep_cyr,
            "reading": "английский сидит в траекториях и внутри <think>, а не размазан по корпусу"},
        "M4_probe_window_inside_reasoning": {
            "holds": m4, "median_think_tokens": median_think_tokens,
            "probe_budget_tokens": PROBE_NEW_TOKENS,
            "share_longer_than_budget": tok_stats.get("share_longer_than_probe_budget"),
            "reading": ("медианный <think> длиннее бюджета пробы — проба не доходит до "
                        "русскоязычной части хода")},
    }
    #: M5 отвечает на вопрос «лечит ли это SFT»: если датасет SFT несёт тот же
    #: англоязычный <think>, пост-тренировка закрепит режим, а не снимет его.
    sft_think = None
    if sft and sft.get("available"):
        sft_think = sft["assistant_turn"]["turn_initial_think"]["cyrillic_share"]
        sft_answer = sft["assistant_turn"]["answer_part"]["cyrillic_share"]
        mask = sft.get("loss_mask", {})
        checks["M5_sft_carries_same_reasoning_language"] = {
            "holds": sft_think < REASONING_RU_MIN,
            "sft_think_cyrillic": sft_think,
            "sft_answer_cyrillic": sft_answer,
            "cpt_think_cyrillic_for_comparison": round(think_cyr, 4),
            "threshold": REASONING_RU_MIN,
            "reasoning_in_loss": mask.get("thinking_is_a_trained_target"),
            "reading": ("датасет SFT несёт преимущественно англоязычное рассуждение "
                        "(и <think> входит в лосс, если reasoning_in_loss=true): SFT "
                        "как подан воспроизводит режим, а не снимает его"),
        }
    confirmed = [k for k, v in checks.items() if v["holds"]]
    treatment_ru = (
        "и то, и другое: (а) состав микса — русскоязычные <think>-трассы в домене "
        "(перевод трасс или перенос рассуждений в русскоязычный replay), "
        "(б) пост-тренировка SFT/RL на русскоязычных ответах")
    treatment_mix = "(а) состав микса: доля домена и доля англоязычных <think>-трасс"
    treatment_sft = "(б) только пост-тренировка: SFT/RL на русскоязычных ответах"
    #: Лечение определяется фактами о языке (M1–M3). M4 говорит не о корпусе, а о том,
    #: куда попадает окно пробы, — поэтому в решение не входит, но в объяснение входит.
    if m1 and m2 and m3:
        treatment, why = treatment_ru, (
            f"поток не англоязычен (кириллица {stream_cyr:.3f}), но английский стоит "
            f"именно там, откуда модель начинает ответ: префикс хода assistant "
            f"{prefix_cyr:.4f}, рассуждение {think_cyr:.4f} при {answer_cyr:.3f} в "
            f"ответной части хода; медианный <think> {median_think_tokens} токенов "
            f"против бюджета пробы {PROBE_NEW_TOKENS}")
        if sft_think is not None and sft_think < REASONING_RU_MIN:
            treatment = (
                "и то, и другое, и правка датасета SFT: (а) состав микса — русскоязычные "
                "<think>-трассы, (б) SFT/RL на русскоязычных ответах, но SFT как подан "
                "(sft_train_v12) несёт преимущественно англоязычное рассуждение и без "
                "правки датасета закрепит сдвиг, а не снимет его")
            why += (f"; датасет SFT: кириллица его <think> {sft_think:.4f} "
                    f"(в CPT-траекториях {think_cyr:.4f})")
    elif not m1:
        treatment, why = treatment_mix, (
            f"кириллица потока {stream_cyr:.3f} < {STREAM_RU_MIN} — поток сам "
            f"англоязычен, микс объясняет сдвиг без пост-тренировки")
    else:
        treatment, why = treatment_sft, (
            "генеративная позиция не выделяется по языку из остального потока: "
            "миксом сдвиг не объясняется")
    margins = {
        "stream_cyrillic": round(stream_cyr, 4), "stream_threshold": STREAM_RU_MIN,
        "stream_margin": round(stream_cyr - STREAM_RU_MIN, 4),
        "generative_position_cyrillic": prefix_cyr, "generative_threshold": CARRIER_MAX,
        "generative_margin": round(CARRIER_MAX - prefix_cyr, 4),
    }
    #: Степень уверенности — по тому, насколько расходятся числа с порогами, а не
    #: по числу совпавших проверок (проверки не независимы).
    strong = prefix_cyr <= 0.05 and (think_en or 0) >= 0.5 and m1 and m4
    confidence = "высокая" if strong else "средняя" if (m1 and m2 and m3) else "низкая"
    return {
        "treatment": treatment,
        "confidence": confidence,
        "why": why,
        "checks": checks,
        "confirmed_by_numbers": confirmed,
        "margins": margins,
        "what_is_measured": [
            f"язык потока по компонентам и весам: карты {cards_cyr:.4f} / траектории "
            f"{traj_cyr:.4f} / replay {rep_cyr:.4f} при весах "
            f"{comps['domain_cards']['weight']}/"
            f"{comps['domain_trajectories']['weight']}/"
            f"{comps['replay_general']['weight']}",
            f"язык <think>-фрагментов потока: {think_cyr:.4f}; доля англоязычных "
            f"фрагментов {(think_en or 0):.4f}",
            f"ответ вне <think>: {answer_cyr:.4f}",
            f"генеративная позиция (первые {PREFIX_CHARS} символов хода assistant): {prefix_cyr:.4f}",
            f"наблюдаемая генерация финала: {observed_cyr} (S3ab, среднее по 5 пробам)",
        ],
        "what_is_hypothesis": [
            "что переносится: языковой режим рассуждения как часть формата (<think> → "
            "английский) или распределение первых токенов после <|im_start|>assistant — "
            "разложение по позициям внутри рассуждения здесь не измерялось",
            "причинная доля домена против replay: руки S3m меняют долю, но не язык "
            "трасс, поэтому «вес домена × язык трасс» разведены не полностью",
            "почему PPL русского текста не страдает при англоязычном рассуждении: "
            "PPL считает продолжение данного текста, проба — свободную генерацию; "
            "это разные величины, и расхождение линий здесь не приговор ни одной",
            "снимается ли режим на SFT, если датасет SFT переписать: пробы "
            "SFT-финала нет, а сам датасет измерен (M5)",
        ],
        "observed_remeasured": (None if observed_pooled is None else {
            "cfinal_pooled_cyrillic": observed_pooled,
            "note": ("перемер pooled-способом (буквы всех 5 проб в пул) против 0.123 "
                     "в S3ab (среднее по пробам): расхождение — способ усреднения, "
                     "не разные измерения"),
            "base": observed.get("base", {}).get("cyrillic_share"),
            "instruct": observed.get("instruct", {}).get("cyrillic_share"),
            "cfinal_prefix_cyrillic": observed.get("cfinal", {}).get("prefix_cyrillic_share"),
            "cfinal_think_cyrillic": observed.get("cfinal", {}).get("think_cyrillic_share"),
        }),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S3ac: язык корпуса против языка ответа")
    ap.add_argument("--mode", default="both", choices=["text", "stream", "both"])
    ap.add_argument("--domain", default=str(DEFAULTS["domain"]))
    ap.add_argument("--replay", default=str(DEFAULTS["replay"]))
    ap.add_argument("--mix-cache", default=str(DEFAULTS["mix_cache"]))
    ap.add_argument("--domain-cache", default=str(DEFAULTS["domain_cache"]))
    ap.add_argument("--tokenizer", default=str(DEFAULTS["tokenizer"]))
    ap.add_argument("--observed-probe", default=str(DEFAULT_OBSERVED_PROBE),
                    help="каталог пробы S3ab для перемерки ответов (пусто — только цитата)")
    ap.add_argument("--sft", default=str(DEFAULTS["sft"]),
                    help="датасет SFT стадии (пусто — не измерять)")
    ap.add_argument("--pipeline", default=str(CASE_ROOT / "laguna_pipeline_v8.py"),
                    help="пайплайн: по нему проверяется, входит ли <think> в лосс SFT")
    ap.add_argument("--allow-other-sources", action="store_true",
                    help="не отказывать, если sha256 корпуса разошёлся с манифестом "
                         "(расхождение всё равно пишется в отчёт)")
    ap.add_argument("--sft-limit", type=int, default=0,
                    help="ограничить число записей SFT (0 — все)")
    ap.add_argument("--out", default="evidence/s3ac-corpus-language.json")
    #: Параметры микса — по умолчанию манифест v12r; меняются только в тестах,
    #: где фикстура заведомо другого размера (и тогда ожидание объявляется явно).
    ap.add_argument("--chunk-tokens", type=int, default=CHUNK_TOKENS,
                    help="длина чанка в токенах (в тестах — маленькая)")
    ap.add_argument("--expect-domain-chunks", type=int, default=MIX_CHUNKS["domain"])
    ap.add_argument("--expect-replay-chunks", type=int, default=MIX_CHUNKS["replay"])
    cfg = ap.parse_args(argv)

    for key in ("domain", "replay") if cfg.mode in ("text", "both") else ():
        if not Path(getattr(cfg, key)).exists():
            note(f"NOT-VERIFIED: нет файла корпуса {getattr(cfg, key)}")
            return EXIT_NOT_VERIFIED
    if cfg.mode in ("stream", "both"):
        for key in ("mix_cache", "domain_cache", "tokenizer"):
            if not Path(getattr(cfg, key)).exists():
                note(f"NOT-VERIFIED: нет входа {getattr(cfg, key)}")
                return EXIT_NOT_VERIFIED

    text_out = probe_text(cfg) if cfg.mode in ("text", "both") else None
    #: Расхождение источников с манифестом сборки микса — отказ по умолчанию: иначе
    #: отчёт описывал бы не тот корпус, что учился. Флаг снимает отказ, но факт
    #: расхождения остаётся в отчёте (sources.matches_mix_manifest=false).
    if text_out is not None and cfg.mode == "both" \
            and not text_out["sources"]["matches_mix_manifest"]:
        if not cfg.allow_other_sources:
            note("ОТКАЗ: sha256 источников не совпал с манифестом сборки микса — "
                 "текстовый разбор описывал бы не тот корпус, что учился "
                 "(--allow-other-sources снимает отказ, но пишет расхождение в отчёт)")
            return EXIT_FAIL
        note("ВНИМАНИЕ: источники разошлись с манифестом сборки микса — "
             "отчёт помечен matches_mix_manifest=false")
    stream_out = None
    if cfg.mode in ("stream", "both"):
        tok = load_tokenizer(Path(cfg.tokenizer))
        try:
            stream_out = probe_stream(cfg, tok)
        except RuntimeError as exc:
            note(f"ОТКАЗ: {exc}")
            return EXIT_FAIL

    observed = observe_generation(Path(cfg.observed_probe) if cfg.observed_probe else None)
    sft_out = probe_sft(cfg)
    stream_pooled = stream_out["stream_pooled"]["cyrillic_share"] if stream_out \
        else text_out["components"][0]["cyrillic_share"]

    report = {
        "schema": "s3ac-corpus-language/1",
        "stage": "S3ac",
        "status": "complete",
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "purpose": ("языковой состав микса v12r по компонентам и весам против языка "
                    "свободной генерации финального CPT-чекпойнта (S3ab: кириллица 0.123)"),
        "method": {
            "cyrillic_share": "кириллические буквы / (кириллические + латинские) — "
                              "то же определение, что в probe_control.degenerate_metrics",
            "english_word_share": "английские слова / (английские + русские) по регулярному выражению",
            "volume": "веса — по токенам кэша (чанк 8192), доли языка — по буквам (пул), не по документам",
            "regions": "cards | system | user | assistant; внутри assistant — think / answer / prefix",
            "mix_manifest": "gb10-shared/cpt_corpus_v12r.txt: 7332 домен + 2444 replay чанка (75/25)",
            "modes": "text — разбор исходных файлов; stream — разбор строк преток-кэша микса",
        },
        "inputs": {
            "domain": str(cfg.domain), "replay": str(cfg.replay),
            "mix_cache": str(cfg.mix_cache), "domain_cache": str(cfg.domain_cache),
            "tokenizer": str(cfg.tokenizer), "mode": cfg.mode,
            "observed_probe": str(cfg.observed_probe) if cfg.observed_probe else None,
        },
        "observed": observed,
        "sft_scan": sft_out,
        "text_scan": text_out,
        "stream_scan": stream_out,
    }

    if stream_out:
        comps = {c["component"]: c for c in stream_out["components"]}
        #: Контракт отчёта: компоненты микса с весами и языком.
        report["corpus"] = [
            {"component": c["component"], "weight": c["weight"],
             "tokens": c["tokens"], "cyrillic_share": c["cyrillic_share"],
             "english_word_share": c["english_word_share"],
             "think_fragments": c["think_fragments"],
             "think_cyrillic_share": (
                 stream_out["assistant_turn"]["think_part"]["cyrillic_share"]
                 if c["component"] == "domain_trajectories" else None)}
            for c in stream_out["components"]]
        report["expected_stream_cyrillic"] = stream_pooled
        report["expected_stream_english_words"] = stream_out["stream_pooled"]["english_word_share"]
        #: В контрактном поле — число, на которое ссылается критерий стадии (S3ab:
        #: среднее по 5 пробам). Перемер пулом букв идёт рядом, в verdict.observed_remeasured.
        report["observed_generation_cyrillic"] = OBSERVED_GENERATION_CYRILLIC
        report["observed_generation_cyrillic_sources"] = {
            "s3ab_aggregate_mean_over_probes": OBSERVED_GENERATION_CYRILLIC,
            "s3ab_base": OBSERVED_BASE_CYRILLIC,
            "s3ab_instruct": OBSERVED_INSTRUCT_CYRILLIC,
        }
        at = stream_out["assistant_turn"]
        report["generative_position"] = {
            "assistant_prefix_cyrillic": at["prefix_first_%d_chars" % PREFIX_CHARS]["cyrillic_share"],
            "assistant_turn_initial_think_cyrillic": at["turn_initial_think"]["cyrillic_share"],
            "assistant_turn_initial_spans": at["turn_initial_spans"],
            "assistant_think_cyrillic": at["think_part"]["cyrillic_share"],
            "assistant_answer_cyrillic": at["answer_part"]["cyrillic_share"],
            "note": ("позиция генерации (первые %d символов хода assistant и открывающий "
                     "ход <think>) против позиции ответа внутри того же хода" % PREFIX_CHARS),
        }
        report["think_template"] = stream_out["think_inventory"].get("template", {})
        report["think_language_split"] = stream_out["think_inventory"].get("language_split", {})
        report["gap"] = {
            "observed_generation": report["observed_generation_cyrillic"],
            "stream": stream_pooled,
            "ratio_observed_to_stream": round(
                report["observed_generation_cyrillic"] / max(stream_pooled, 1e-9), 4),
            "generative_position": report["generative_position"]["assistant_prefix_cyrillic"],
            "ratio_observed_to_generative_position": round(
                report["observed_generation_cyrillic"] /
                max(report["generative_position"]["assistant_prefix_cyrillic"], 1e-9), 1),
            "baseline": OBSERVED_BASE_CYRILLIC,
        }
        gp = report["gap"]["generative_position"]
        report["gap"]["distance_to_stream"] = round(abs(report["gap"]["observed_generation"]
                                                        - stream_pooled), 4)
        report["gap"]["distance_to_generative_position"] = round(
            abs(report["gap"]["observed_generation"] - gp), 4)
        report["gap"]["reading"] = (
            "наблюдаемая генерация ближе к генеративной позиции корпуса "
            f"({gp:.4f}, расстояние {report['gap']['distance_to_generative_position']:.4f}), "
            f"чем к среднему языку потока ({stream_pooled:.4f}, расстояние "
            f"{report['gap']['distance_to_stream']:.4f}): сдвиг объясняется не «английским "
            "корпусом» целиком, а тем, какой язык обучен в старте ответа"
            if report["gap"]["distance_to_generative_position"]
            < report["gap"]["distance_to_stream"] else
            "наблюдаемая генерация ближе к среднему языку потока — механизм «английского "
            "старта ответа» числами не подтверждается")
        report["verdict"] = build_verdict(stream_out, observed, sft_out)
    else:
        report["corpus"] = [
            {"component": c["component"], "weight": None, "cyrillic_share": c["cyrillic_share"],
             "english_word_share": c["english_word_share"], "think_fragments": c["think_fragments"],
             "think_cyrillic_share": text_out["assistant_turn"]["think_part"]["cyrillic_share"]
             if c["component"] == "domain_trajectories" else None}
            for c in text_out["components"]]
        report["expected_stream_cyrillic"] = None
        report["observed_generation_cyrillic"] = OBSERVED_GENERATION_CYRILLIC
        report["verdict"] = {"treatment": "не считается без --mode stream",
                             "confidence": "нет", "what_is_hypothesis": []}

    report["open_questions"] = [
        "какая доля сдвига приходится на англоязычные <think>-трассы, а какая на "
        "остальной домен: рука с тем же весом домена, но русскоязычными трассами, "
        "не ставилась",
        "переносить ли рассуждения в русскоязычный replay или переводить трассы домена — "
        "это разные миксы при одинаковом весе (решение по ADR-003, здесь не принимается)",
        "восстанавливает ли SFT кириллицу в <think> или только в ответе: нужна та же "
        "проба на финале SFT",
    ]
    report["artifacts"] = [
        {"path": str(cfg.out), "what": "этот свод"},
        {"path": "tools/corpus_language_probe.py", "what": "инструмент измерения"},
        {"path": "docs/specs/LANGUAGE-DRIFT.md",
         "what": "разбор механизма, вердикт и цена вариантов лечения"},
        {"path": "tools/tests/run_tool_tests.sh", "what": "тесты инструмента, раздел 18"},
    ]

    out = Path(cfg.out)
    if not out.is_absolute():
        out = CASE_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    note(f"отчёт записан: {out}")
    if stream_out:
        note(f"поток: кириллица {stream_pooled:.4f}, англ. слова "
             f"{report['expected_stream_english_words']:.4f}, "
             f"веса домен/replay {stream_out['mix']['weight_domain']}/"
             f"{stream_out['mix']['weight_replay']}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
