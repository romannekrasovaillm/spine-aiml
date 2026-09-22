#!/usr/bin/env python3
"""S3ai (ADR-039): раздельный замер языков рассуждения и ответа.

Зачем отдельный прибор. До сих пор язык генерации мерился **одним** числом —
долей кириллицы по всему ответу (`probe_control.degenerate_metrics`). При ответе,
где есть и `<think>`, и текст ответа, такое число **смешивает два разных вопроса**:
«модель думает по-английски, но отвечает по-русски» (приемлемо) и «модель и думает,
и отвечает по-английски» (неприемлемо) дают **одинаковый** скор. ADR-039 п.2 требует
разделять их, иначе вердикт по гипотезе «ризонинг — навык, язык рассуждения не
важен» не на чем строить.

Что считается (ADR-039 п.2, объявлено ДО замера):

* **(а) кириллица в `<think>`** — по содержимому всех блоков `<think>…</think>`;
* **(б) кириллица в ответной части** — по всему, что **вне** этих блоков (включая
  текст после `</think>` и до конца хода);
* **(в) доля генераций с режимом** — присутствие `<think>` / `<tool_call>`;
* **общий скор** (`cyr_overall`) считается, но он **справочный**: решения по нему
  не принимаются (ADR-039 п.2).

Три правила разбора, объявленные заранее (без них прибор «доопределялся» бы по
месту, то есть подгонялся бы под ответ):

1. **Маркеры не считаются речью.** Сами `<think>` / `</think>` из обоих сегментов
   исключаются: это разметка (ASCII), и её учёт сдвигал бы долю языка на ровно
   одинаковую величину во всех состояниях — шум, а не сигнал.
2. **Незакрытый `<think>`** — остаток хода целиком идёт в сегмент рассуждения
   (`unclosed_think=true`), ответной части у такой генерации нет. Это не «0 %
   русского в ответе», это «ответа нет» — и в сводке такие генерации видны
   отдельным счётчиком `answer_coverage`, а в основной метрике дают 0.0
   (см. п.3).
3. **Нет букв — нет доли.** `share` сегмента без букв (`None`), а не `0.0`:
   0.0 означало бы «здесь латиница», хотя здесь ничего. В сводке даются **обе**
   величины: `*_with_zeros` (генерация без букв в сегменте даёт 0.0 — основная,
   консервативная: «русского ответа в такой генерации нет») и `*_defined_only`
   (среднее только по генерациям, где сегмент вообще есть). Расхождение между
   ними — диагностика покрытия, а не второй шанс для вердикта: вердикт считается
   **по основной**.

Протокол проб — тот же, что у `probe_control.py` (S3ab/S3ag): те же 5 промптов
ядра, тот же `UNIFIED_SYSTEM_PROMPT`, chat template, greedy, `max_new_tokens=384`.
Отличие ровно одно и объявлено: **набор промптов расширен** до 24 (≥20 генераций
на состояние — требование TASK). Ядро (5) сохранено дословно и берётся **импортом**
из `probe_control`, а не копией, — чтобы «тот же протокол» было тождеством
объектов, а не обещанием. Тождество прибора доказывается **числом**: повторный
прогон ядра на CPT-финале обязан побайтово воспроизвести ответы, записанные S3ab
в `evidence/s3ab-cpt-probes.json` (генедика детерминирована), — проверка в
`tools/assemble_s3ai_evidence.py`, а не на слово.

## S3aj: снятие артефакта усечения

Базовые замеры S3ai дали `truncated_share = 1.0` — **все** генерации упёрлись в
лимит токенов. Из этого следовало, что «незакрытых `<think>` стало больше»
(0.4583 → 0.7917) может быть артефактом обрыва, а не деградацией формата. Разбор
показал, что обрыв складывается из **двух разных причин**, и их нельзя мерить
одним числом:

1. **Прибор не останавливается на конце хода.** Веса базы несут
   `eos_token_id = 151643` (`<|endoftext|>`), а ход в формате v12 заканчивается
   токеном `151645` (`<|im_end|>`). `model.generate` без явного `eos_token_id`
   останавливается только на первом токене — которого модель в чат-формате не
   порождает. Поэтому генерация **переписывает собственный конец хода и уходит в
   следующий**: в логах S3ai видны ходы, где после дописанного ответа модель
   печатает `<|im_end|><|im_start|>user…` и начинает новый круг. Такой обрыв —
   свойство прибора, а не модели.
2. **Вырождение (зацикливание).** Даже в размеченном пределе генерация уходит в
   повторы («размер группы G, размер группы G, …»), и обрыв там — свойство
   модели.

Флаг `--stop-at-turn-end` чинит первую причину: `<|im_end|>` добавляется в
`eos_token_id` генерации, и ход обрывается там, где его закончила модель.
Флаг **выключен по умолчанию** — протокол S3ab (и тождество с ним) обязан
воспроизводиться побайтово, а его трогать нельзя.

Разделение причин требует и разделения чтения: у каждой генерации считается
`stop_reason` — `turn_end` (ход дописан) либо `limit_in_think` /
`limit_in_tool_call` / `limit_in_tool_response` / `limit_in_answer` (где именно
оборван лимитом), плюс признак зацикливания (`looped`). Замер без этих полей
отвечает на вопрос «сколько обрывов», но не на вопрос «чья это вина».

**Доопределение по уже снятым данным** (режим `--recut-report`): greedy-генерация
префиксно детерминирована, поэтому остановка на `<|im_end|>` не меняет ни одного
токена до него — меняется только точка обрыва. Значит, к уже снятому отчёту можно
применить правило «ход кончается на первом `<|im_end|>`» и получить **ровно тот**
результат, который дал бы прибор с исправленной остановкой на том же бюджете. Это
не второй шанс для вердикта, а способ развести две причины обрыва на данных,
которые уже есть.

## S3al: штатный режим декодирования

Замер S3ak установил причину вырождения: петля — это **повторяемость n-грамм**, и
запрет повторов 4-грамм снимает её механически (`looped_share` 0.0000 против
0.5417 при greedy); штраф за повторы не помогает (0.5833), sampling снижает
частично (0.3333). Отсюда — правило стадии, зашитое в прибор, а не в текст свода:

* **штатный режим** — greedy + `no_repeat_ngram = 4`, включён **по умолчанию**
  (`STANDARD_NO_REPEAT_NGRAM`);
* **sampling** — дополнительная проверка свободного режима, не штатный режим;
* **прогон без запрета повторов** (то есть прежний протокол S3ab/S3ai/S3aj и
  «greedy-only») для выводов о языке и формате **запрещён**: он отклоняется с
  кодом 1, пока не назван явно флагом `--legacy-decoding`, и даже тогда отчёт
  помечается `decoding_protocol.allowed_for_conclusions = false`.

Флаг `--legacy-decoding` существует ровно для одного: воспроизвести прежние
замеры **побайтово** (мост тождества прибора), а не для новых чисел. Именно так
проверяется, что смена умолчания не сдвинула старый протокол.

Коды возврата::

    0 — отчёт собран
    1 — отказ: неверный вход прибора (неизвестный набор промптов, нет --out при
        --run, прогон без запрета повторов n-грамм без --legacy-decoding)
    2 — NOT-VERIFIED: нечего мерить (ни одного состояния не загрузилось)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))

#: Ядро протокола берётся **импортом** из прибора S3ab/S3ag: копия промптов
#: разошлась бы с источником при первой же правке, и «тот же протокол» перестало
#: бы быть проверяемым. Импорт дешёвый: тяжёлые библиотеки в ppl_probe/probe_control
#: подгружаются лениво, внутри функций.
from probe_control import PROBE_PROMPTS as CORE_PROMPTS  # noqa: E402
from probe_control import UNIFIED_SYSTEM_PROMPT  # noqa: E402  (тот же системный промпт)
#: Метрики вырождения берутся **импортом** из S3ab-прибора по той же причине, что
#: и промпты: «зациклилось» — это диагноз, который в двух местах не должен
#: считаться по-разному. `uniq4` и `max4gram_rep` смотрят на слова, поэтому
#: зацикливание внутри одного «слова» они не видят — в отчёт идёт ещё и
#: `max_repeat_of_char_block`. Импорт дешёвый: `degenerate_metrics` — чистая
#: функция над текстом, тяжёлых библиотек не тянет.
from probe_control import degenerate_metrics  # noqa: E402

SEED = 42                     # как у стадии: проба не сеет, но манифест обязан назвать сид
MAX_NEW_TOKENS = 384          # как в probe_control (S3ab): смена сломала бы сопоставимость
#: Бюджет генерации языка. 384 токена (протокол S3ab) оказались **малы**: замер
#: 17.09.2026 на CPT-финале дал hit_limit = 5/5 — ход обрывается внутри
#: рассуждения, и «ответной части» у генерации просто нет (`answer_coverage` → 0).
#: Мерить на таком бюджете язык ответа нельзя: получилось бы «модель не отвечает
#: по-русски» там, где модель не успевает ответить вообще. Поэтому у прогонов
#: языка бюджет больше (объявлено ДО замера, одинаково для всех трёх состояний),
#: а 384 остаётся у проб ядра — они служат мостом тождества с S3ab.
LANG_MAX_NEW_TOKENS = 1024
#: Бюджет «полного» замера S3aj. Объявлен ДО замера и обоснован независимо от
#: модели: p90 длины трассы обучающего набора SFT — 2 914 токенов, максимум —
#: 4 583 (`runs/s3ai-probes-20260917/sft-trace-lengths.json`). Бюджет 4 096
#: покрывает p90 с запасом и почти весь хвост распределения; остаточное усечение
#: на нём обязано быть названо, а не замолчано.
FULL_MAX_NEW_TOKENS = 4096
#: Порог валидности замера: меньше половины генераций дошли до ответной части —
#: мерить язык ответа не на чем, вердикт не выносится (не «ноль», а «нет данных»).
ANSWER_COVERAGE_FLOOR = 0.5
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
TOOL_CALL_OPEN, TOOL_CALL_CLOSE = "<tool_call>", "</tool_call>"
TOOL_RESP_OPEN, TOOL_RESP_CLOSE = "<tool_response>", "</tool_response>"
#: Конец хода в формате v12. Токен 151645 в базовом словаре Qwen2.5, но **не** в
#: `eos_token_id` весов базы (там 151643, `<|endoftext|>`): без явного указания
#: генерация на нём не останавливается. Это и есть первая причина артефакта.
TURN_END = "<|im_end|>"

#: Имена состояний хода для `stop_reason`. «В ответе» — значит вне всех блоков
#: разметки; «в рассуждении» — внутри незакрытого `<think>` и т. д. Если блоки
#: вложены (вызов инструмента внутри рассуждения), называется **внутренний**:
#: обрыв приходится на него, а не на объемлющий.
REGION_ANSWER = "answer"
REGION_TAGS = {THINK_OPEN: "think", TOOL_CALL_OPEN: "tool_call",
               TOOL_RESP_OPEN: "tool_response"}
REGION_CLOSERS = {THINK_CLOSE: THINK_OPEN, TOOL_CALL_CLOSE: TOOL_CALL_OPEN,
                  TOOL_RESP_CLOSE: TOOL_RESP_OPEN}

STOP_TURN_END = "turn_end"           # ход дописан: модель сама поставила конец
STOP_LIMIT_PREFIX = "limit_in_"      # обрыв лимитом токенов, дальше — имя региона

#: Порог «зациклилось» — объявлен ДО замера, как и пороги критерия. Выбран по
#: смыслу, а не по результату: 8 повторов одной и той же 4-граммы слов в одной
#: генерации — это не проза ни на одном языке (здоровая речь даёт 1–2). Порог
#: грубый намеренно: задача — отделить вырождение от длинного честного текста,
#: а не измерить степень вырождения. Рядом в отчёте лежат сами `uniq4` и
#: `max4gram_rep`, чтобы порог можно было пересмотреть, не перемеряя.
LOOP_MAX4GRAM_REP = 8

#: Штатный режим декодирования стадии (S3al, ADR-040 п.6–7) — **запрет повторов
#: 4-грамм, включённый по умолчанию**. Зачем это константа прибора, а не строка в
#: отчёте: замер S3ak показал, что петля — это повторяемость n-грамм и что запрет
#: повторов 4-грамм снимает её **механически** (0.0000 против 0.5417 при greedy).
#: Значит greedy без запрета — не «нейтральная настройка», а режим с известным
#: дефектом: у SFT-состояний он тратит бюджет на переписывание одной и той же
#: 4-граммы, и метрики языка и формата, снятые в нём, измеряют петлю, а не модель.
#: Поэтому запрет включён **по умолчанию**, а прогон без него требует явного
#: `--legacy-decoding` и помечается в отчёте как непригодный для выводов
#: (`decoding_protocol.allowed_for_conclusions = false`). Прежний протокол
#: (S3ab/S3ai/S3aj) от этого не теряется: он воспроизводится тем же флагом
#: побайтово, и именно так проверяется, что смена умолчания не сдвинула старые
#: числа.
STANDARD_NO_REPEAT_NGRAM = 4

#: Критерий ADR-039 п.3 — объявлен ДО замера, зашит константами, чтобы его нельзя
#: было «уточнить» после того, как числа станут известны.
CRIT_ANSWER_RU_MIN = 0.5      # подтверждение: ответная часть русская — ≥ 0.5
CRIT_THINK_EN_MAX = 0.3       # ...при рассуждении, оставшемся английским — ≤ 0.3
CRIT_ANSWER_REFUTE_MAX = 0.4  # опровержение: ответная часть ушла в английский — < 0.4

ZONE_CONFIRMED = "confirmed"
ZONE_REFUTED = "refuted"
ZONE_INTERMEDIATE = "intermediate"           # 0.4–0.5 → «недостаточно данных»
ZONE_OUT_OF_SCOPE = "out_of_scope_think_not_english"  # ответ русский, но и think не английский

RE_CYR = re.compile(r"[а-яА-ЯёЁ]")
RE_LAT = re.compile(r"[a-zA-Z]")

#: Вся разметка хода. Из подсчёта букв исключается целиком (правило 1): тег —
#: это тег, а не речь, и его учёт сдвигал бы долю языка на одну и ту же величину
#: во всех состояниях. Содержимое блоков (JSON вызова, текст ответа инструмента)
#: разметкой не является и остаётся.
MARKUP_TAGS = (THINK_OPEN, THINK_CLOSE, TOOL_CALL_OPEN, TOOL_CALL_CLOSE,
               TOOL_RESP_OPEN, TOOL_RESP_CLOSE)

# ─────────────────── S3ak: служебные токены — не речь (прибор v2) ───────────────────
#
# Что было не так в v1 (S3ai/S3aj). Правило 1 исключало из букв только теги блоков
# (`<think>` и родню), но **не** служебные токены хода. А они состоят из латинских
# букв: `<|im_end|>` — это шесть латинских букв «im_end», `<|im_start|>` — восемь.
# Каждая генерация SFT-состояния кончается токеном `<|im_end|>` (генерация его
# порождает и он же в неё возвращается), поэтому у **короткого русского ответа**
# («Извлекаю короткий заголовок для статьи о пользе чтения.» и т. п.) шесть
# подмешанных латинских букв — это не шум в третьем знаке, а сдвиг, сравнимый с
# самим сигналом: доля 1.0 превращается в 0.7. У длинных ответов CPT-состояний
# эффект мал, у коротких ответов SFT — велик, то есть ошибка прибора **росла ровно
# там, где мерился вердикт**.
#
# Прибор v2 (S3ak) правит это двумя объявленными правилами:
#
#   4. **Служебные токены вида `<|…|>` — разметка, а не речь.** Исключаются все,
#      а не три названных: у ревизий Qwen2.5 набор служебных токенов разный
#      (`<|im_end|>`, `<|im_start|>`, `<|endoftext|>`, `<|object_ref_start|>`…), и
#      «список из трёх» стал бы зависимостью от ревизии. Список фактически
#      вычищенных токенов кладётся в отчёт (`special_tokens_stripped`) — чтобы
#      правило было видно числом, а не обещанием.
#   5. **Язык меряется по ходу модели** — до первого `<|im_end|>` (`first_turn`).
#      Всё, что генерация напечатала после собственного конца хода, — это её
#      продолжение после ответа (переписанный следующий круг), а не ответ. На
#      замерах с остановкой на конце хода обрезки не происходит вовсе (кроме
#      самого маркера), поэтому правило ничего не отнимает у честных ходов; на
#      прежних замерах без остановки оно отделяет ответ от «эха».
#
# v1 сохранён в приборе **как отдельное чтение** (`legacy` в каждом замере и режим
# `--reaudit-report`): без него нельзя было бы показать, какие прежние числа
# изменились и на сколько. Правило «основное чтение — v2» объявлено здесь, до
# того как новые числа стали известны.

#: Версия арифметики прибора. 1 — S3ai/S3aj (только теги блоков), 2 — S3ak
#: (плюс служебные токены и обрезка по концу хода).
INSTRUMENT_VERSION = 2

#: Служебные токены хода, названные в TASK S3ak явно. Полное правило — регулярка
#: ниже (она шире и не зависит от ревизии словаря); кортеж остаётся затем, чтобы
#: названные три были видны в коде, а не растворялись в шаблоне.
TURN_MARKERS = ("<|im_end|>", "<|im_start|>", "<|endoftext|>")

#: Любой служебный токен `<|…|>`. Ограничение длины (40) — от жадного захвата
#: прозы, если модель напечатала одинокая `<|` без закрытия: без ограничения
#: такая строка съела бы весь остаток хода до следующей `|>`, то есть вычла бы из
#: букв настоящую речь.
SPECIAL_TOKEN_RE = re.compile(r"<\|[^|>\n]{0,40}\|>")


def strip_special_tokens(text: str) -> tuple[str, list[str]]:
    """Убрать служебные токены `<|…|>` и назвать, какие именно убраны (правило 4).

    Возвращает ``(текст, список_токенов)``: второй элемент — диагностика, по
    которой видно, что правило сработало на этом тексте, а не осталось декларацией.
    """
    found = SPECIAL_TOKEN_RE.findall(text)
    return SPECIAL_TOKEN_RE.sub("", text), found


def strip_markup(text: str, instrument: int = INSTRUMENT_VERSION) -> str:
    """Убрать разметку хода. ``instrument=1`` — арифметика S3ai/S3aj (только теги)."""
    if instrument >= 2:
        text, _ = strip_special_tokens(text)
    for t in MARKUP_TAGS:
        text = text.replace(t, "")
    return text


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ─────────────────────────── разбор на сегменты ───────────────────────────

def _blocks(text: str, open_tag: str, close_tag: str) -> tuple[str, str, int]:
    """Вырезать блоки ``open_tag…close_tag``.

    Возвращает ``(внутри, снаружи, число_блоков)``. Незакрытый блок съедает
    остаток строки (как и `<think>`): «незакрытый» не значит «пустой», и
    догадываться, где автор хотел закрыть, прибор не имеет права.
    """
    inside: list[str] = []
    outside: list[str] = []
    pos, n = 0, 0
    while True:
        i = text.find(open_tag, pos)
        if i < 0:
            outside.append(text[pos:])
            break
        outside.append(text[pos:i])
        j = text.find(close_tag, i + len(open_tag))
        if j < 0:                       # незакрытый блок — до конца хода
            inside.append(text[i + len(open_tag):])
            n += 1
            break
        inside.append(text[i + len(open_tag):j])
        n += 1
        pos = j + len(close_tag)
    return "".join(inside), "".join(outside), n


def split_segments(text: str, instrument: int = INSTRUMENT_VERSION) -> dict:
    """Разделить ход на рассуждение и ответную часть (ADR-039 п.2, правила 1–2).

    ``instrument`` передаётся дальше в `strip_markup`: перечитывая прежний замер
    арифметикой v1, сегменты обязаны быть очищены **как тогда** — иначе «прежнее
    число» получилось бы смесью двух арифметик, и дельта правки прибора ничего не
    значила бы.

    Возвращает::

        {"think": str, "answer": str, "prose": str,
         "tool_call": str, "tool_response": str,
         "n_think_blocks": int, "n_tool_call_blocks": int,
         "unclosed_think": bool, "stray_think_close": bool}

    ``answer`` — всё вне `<think>` (объявленная метрика «б»), ``prose`` — тот же
    ``answer`` без блоков `<tool_call>`/`<tool_response>` (диагностика: размеченный
    JSON и вставленный `tool_response` — не речь модели, и смешивать их с ответом
    значит мерить цитату из системного промпта вместо ответа).
    """
    think, answer, n_think = _blocks(text, THINK_OPEN, THINK_CLOSE)
    #: `<think>` без парного закрытия — состояние особое: ответной части нет вовсе.
    unclosed = text.count(THINK_OPEN) > text.count(THINK_CLOSE)
    #: Одиночный `</think>` без открытия (баг v7/v8 из ADR-036) — маркер, не речь;
    #: он остаётся в ответной части как есть (учитывается флагом, а не чисткой:
    #: чистка скрыла бы дефект генерации, ради обнаружения которого флаг и нужен).
    stray = text.count(THINK_CLOSE) > text.count(THINK_OPEN)

    tc, rest, n_tc = _blocks(answer, TOOL_CALL_OPEN, TOOL_CALL_CLOSE)
    tr, prose, n_tr = _blocks(rest, TOOL_RESP_OPEN, TOOL_RESP_CLOSE)
    #: Сегменты отдаются уже без разметки (правило 1): `answer` собирается из
    #: сырого остатка **после** вырезания блоков — иначе вырезать их было бы
    #: не из чего.
    return {"think": strip_markup(think, instrument), "answer": strip_markup(answer, instrument),
            "prose": strip_markup(prose, instrument),
            "tool_call": tc, "tool_response": tr,
            "n_think_blocks": n_think, "n_tool_call_blocks": n_tc,
            "unclosed_think": unclosed, "stray_think_close": stray}


def share(text: str, instrument: int = INSTRUMENT_VERSION) -> float | None:
    """Доля кириллицы среди букв разметки-очищенного текста.
    ``None`` — букв нет (правило 3, а не 0.0).

    ``instrument=1`` — арифметика S3ai/S3aj: служебные токены хода **считаются**
    буквами. Оставлено затем, чтобы прежние числа можно было перечесть и показать
    разницу, а не объявить их недействительными задним числом.
    """
    text = strip_markup(text, instrument)
    cyr, lat = len(RE_CYR.findall(text)), len(RE_LAT.findall(text))
    if cyr + lat == 0:
        return None
    return round(cyr / (cyr + lat), 4)


# ─────────────────────── где кончается ход (S3aj) ───────────────────────

def tail_region(text: str) -> str:
    """В каком месте хода обрывается текст: ответ, рассуждение, вызов, ответ инструмента.

    Читается **стеком**, а не поиском «последнего тега»: блоки вложены (вызов
    инструмента внутри рассуждения — штатная форма v12), и «внутри чего» обязано
    называть **внутренний** блок. Одиночный закрывающий тег без открывающего
    (баг v7/v8) состояние не меняет: это маркер, а не открытие блока.
    """
    stack: list[str] = []
    pos = 0
    while True:
        best: tuple[int, str] | None = None
        for tag in list(REGION_TAGS) + list(REGION_CLOSERS):
            j = text.find(tag, pos)
            if j >= 0 and (best is None or j < best[0]):
                best = (j, tag)
        if best is None:
            break
        j, tag = best
        if tag in REGION_TAGS:
            stack.append(tag)
        elif stack and stack[-1] == REGION_CLOSERS[tag]:
            stack.pop()
        pos = j + len(tag)
    return REGION_TAGS[stack[-1]] if stack else REGION_ANSWER


def stop_reason(text: str, hit_limit: bool) -> str:
    """Почему генерация кончилась (S3aj, объявлено ДО замера).

    Два исхода принципиально разные, и один счётчик `truncated_share` их
    смешивает. ``turn_end`` — ход дописан моделью (генерация остановилась сама);
    ``limit_in_<регион>`` — обрыв лимитом токенов, и регион говорит, **где**
    именно: в рассуждении, на вызове инструмента, в ответе инструмента или в
    ответной части.

    Граница «упёрлась в лимит ровно на последнем токене» трактуется как лимит:
    отличить её от остановки без запаса нельзя, а осторожное чтение здесь — «не
    знаем, дописан ли ход».
    """
    if not hit_limit:
        return STOP_TURN_END
    return STOP_LIMIT_PREFIX + tail_region(text)


def first_turn(text: str) -> str:
    """Ход до первого `<|im_end|>` — то, что модель считала своим ответом.

    Зачем. Greedy-генерация **префиксно детерминирована**: остановка на
    `<|im_end|>` не меняет ни одного токена до него, только точку обрыва. Значит,
    по уже снятому отчёту можно восстановить ровно тот ход, который дал бы прибор
    с исправленной остановкой, — и развести «обрыв прибором» и «обрыв лимитом»
    на данных, которые уже есть. Если конца хода в генерации нет, текст
    возвращается целиком: дописывать за модель прибор не имеет права.
    """
    i = str(text).find(TURN_END)
    return str(text) if i < 0 else str(text)[:i]


def degenerate(text: str) -> dict:
    """Признак зацикливания генерации (S3aj): вырождение — вторая причина обрыва.

    Метрики — импортом из S3ab-прибора (`degenerate_metrics`), решение о пороге —
    здесь и объявлено заранее (`LOOP_MAX4GRAM_REP`). В отчёт идут и сами числа:
    порог можно пересмотреть, не перемеряя, а «зациклилось» без чисел — это
    суждение, а не замер.
    """
    m = degenerate_metrics(text)
    rep = m.get("max4gram_rep")
    return {"uniq4": m.get("uniq4"), "max4gram_rep": rep,
            "max_repeat_of_char_block": m.get("max_repeat_of_char_block"),
            "looped": bool(rep is not None and rep >= LOOP_MAX4GRAM_REP)}


def _language_readings(text: str, diagnostics_from: str | None = None) -> dict:
    """Языковые величины одного текста в арифметике v2 (правила 1–5).

    ``diagnostics_from`` — текст, по которому считается **диагностика** вычищенных
    служебных токенов. Он отличается от ``text`` намеренно: язык меряется по ходу
    модели (правило 5), а «какие служебные токены вообще были в генерации» —
    свойство всей генерации. Иначе у хода, кончающегося маркером `<|im_end|>`, поле
    `special_tokens_stripped` было бы пустым (маркер срезала не чистка, а обрезка),
    и правило 4 выглядело бы неработающим ровно там, где оно работает.
    """
    seg = split_segments(text)
    plain = strip_markup(text)
    _, specials = strip_special_tokens(diagnostics_from if diagnostics_from is not None
                                       else text)
    return {
        "segments": seg,
        "cyr_think": share(seg["think"]),
        "cyr_answer": share(seg["answer"]),
        "cyr_answer_prose": share(seg["prose"]),
        "cyr_overall": share(plain),
        "letters_think": len(RE_CYR.findall(strip_markup(seg["think"])))
                         + len(RE_LAT.findall(strip_markup(seg["think"]))),
        "letters_answer": len(RE_CYR.findall(strip_markup(seg["answer"])))
                          + len(RE_LAT.findall(strip_markup(seg["answer"]))),
        "letters_overall": len(RE_CYR.findall(plain)) + len(RE_LAT.findall(plain)),
        "special_tokens_stripped": sorted(set(specials)),
    }


def legacy_metrics(text: str) -> dict:
    """Тот же текст в арифметике v1 (S3ai/S3aj) — для перечитывания прежних замеров.

    Считается **по сырому тексту** (без обрезки по концу хода) и **со служебными
    токенами в буквах** — ровно так, как считали S3ai/S3aj. Нужен затем, чтобы
    «числа изменились» было проверяемым утверждением с дельтой, а не оговоркой.
    """
    seg = split_segments(text, instrument=1)   # сегменты чищены арифметикой v1
    return {
        "cyr_think": share(seg["think"], instrument=1),
        "cyr_answer": share(seg["answer"], instrument=1),
        "cyr_answer_prose": share(seg["prose"], instrument=1),
        "cyr_overall": share(text, instrument=1),
        "letters_answer": len(RE_CYR.findall(strip_markup(seg["answer"], 1)))
                          + len(RE_LAT.findall(strip_markup(seg["answer"], 1))),
    }


def language_metrics(text: str) -> dict:
    """Раздельные метрики одной генерации (ADR-039 п.2 + S3aj + прибор v2 S3ak).

    Основное чтение — v2: язык считается по ходу модели (`first_turn`) и служебные
    токены `<|…|>` буквами не считаются. Прежнее чтение v1 кладётся рядом в поле
    ``legacy``: вердикт по нему не выносится, но без него нельзя показать, что
    именно изменила правка прибора.
    """
    own = first_turn(text)              # правило 5: ход модели, не её продолжение
    r = _language_readings(own, diagnostics_from=text)
    seg = r["segments"]
    plain = strip_markup(own)
    cyr_t = len(RE_CYR.findall(strip_markup(seg["think"])))
    lat_t = len(RE_LAT.findall(strip_markup(seg["think"])))
    cyr_a = len(RE_CYR.findall(strip_markup(seg["answer"])))
    lat_a = len(RE_LAT.findall(strip_markup(seg["answer"])))
    deg = degenerate(text)
    out = {
        "cyr_think": share(seg["think"]),
        "cyr_answer": share(seg["answer"]),
        "cyr_answer_prose": share(seg["prose"]),
        "cyr_overall": share(plain),         # справочно: решения по нему не принимаются
        "letters_think": cyr_t + lat_t,
        "letters_answer": cyr_a + lat_a,
        "letters_overall": len(RE_CYR.findall(plain)) + len(RE_LAT.findall(plain)),
        "has_think": THINK_OPEN in text,
        "has_tool_call": TOOL_CALL_OPEN in text,
        #: Число блоков — диагностика копирования системного промпта: в нём самом
        #: есть пример с двумя `<think>` и блоками инструмента, и генерация, которая
        #: его переписывает, даёт «рассуждение» из чужого текста. Отличить это от
        #: своего ризонинга прибор не может — но обязан показать, что случай есть.
        "n_think_blocks": seg["n_think_blocks"],
        "n_tool_call_blocks": seg["n_tool_call_blocks"],
        "unclosed_think": seg["unclosed_think"],
        "stray_think_close": seg["stray_think_close"],
        "chars": len(text),
        #: S3aj: где кончается ход и не выродилась ли генерация в повтор. Без этих
        #: полей «обрыв» не отличим от «модель не дописала», а вырождение — от
        #: длинного честного рассуждения.
        "tail_region": tail_region(text),
        "n_turn_ends": text.count(TURN_END),
        "looped": deg["looped"],
        "uniq4": deg["uniq4"],
        "max4gram_rep": deg["max4gram_rep"],
        "max_repeat_of_char_block": deg["max_repeat_of_char_block"],
        #: v2: на каком ходу считан язык и что вычищено из букв. Оба поля — не
        #: украшение: по ним проверяется, что правило 4 сработало (список непуст
        #: у всякой генерации SFT-состояния), а правило 5 не отняло у честного хода
        #: ничего лишнего (`own_turn_chars == chars`, если конца хода в окне не было).
        "instrument": INSTRUMENT_VERSION,
        "special_tokens_stripped": r["special_tokens_stripped"],
        "own_turn_chars": len(own),
        "own_turn_truncated": own != text,
        #: Прежнее чтение (v1) — рядом, вердикт по нему не выносится.
        "legacy": legacy_metrics(text),
    }
    out["mode_share"] = 1.0 if (out["has_think"] or out["has_tool_call"]) else 0.0
    return out


# ─────────────────────────── сводка по состоянию ───────────────────────────

def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _metric(records: list[dict], key: str, path: tuple[str, ...] = ("metrics",)) -> dict:
    """Сводка одной раздельной метрики: основная (с нулями) + диагностика покрытия.

    ``path`` позволяет считать ту же сводку по вложенному чтению (``metrics.legacy``)
    — иначе прежнюю арифметику пришлось бы дублировать отдельной функцией и она
    разошлась бы с основной при первой правке.
    """
    vals = []
    for r in records:
        node = r
        for step in path:
            node = node.get(step) if isinstance(node, dict) else None
        vals.append(node.get(key) if isinstance(node, dict) else None)
    defined = [v for v in vals if v is not None]
    return {
        "with_zeros": _mean([0.0 if v is None else v for v in vals]),
        "defined_only": _mean(defined),
        "coverage": round(len(defined) / len(vals), 4) if vals else None,
        "defined": len(defined),
        "n": len(vals),
    }


#: Бутстрап для доверительного интервала доли. Объявлено ДО замера: 2000
#: перевыборок с возвращением, сид 42, границы — 2.5-й и 97.5-й перцентили
#: распределения средних (percentile-метод). Зачем вообще интервал: S3aj дал
#: запас до границы 0.4 всего 0.008 при 24 генерациях — то есть вердикт держался
#: на разнице, меньшей шага одной генерации (1/24 = 0.042). Число без интервала
#: в такой ситуации не отвечает на вопрос «это сигнал или шум», а именно этот
#: вопрос и решает, можно ли менять данные.
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 42
BOOTSTRAP_LO, BOOTSTRAP_HI = 2.5, 97.5


def bootstrap_ci(values: list[float], n: int = BOOTSTRAP_N,
                 seed: int = BOOTSTRAP_SEED) -> dict:
    """Доверительный интервал среднего — бутстрапом по генерациям (объявлено выше).

    Возвращает ``{lo, hi, mean, n, method, n_boot, seed}`` или ``None``-поля при
    пустом входе. Это **не** проверка гипотезы: интервал показывает, на сколько
    число может уехать от одной лишь выборки, и вердикт «опровергнуто» при
    интервале, накрывающем границу, объявляется недоказанным.
    """
    if not values:
        return {"lo": None, "hi": None, "mean": None, "n": 0,
                "method": "bootstrap percentile", "n_boot": n, "seed": seed}
    import random
    rng = random.Random(seed)
    k = len(values)
    means = []
    for _ in range(n):
        s = 0.0
        for _ in range(k):
            s += values[rng.randrange(k)]
        means.append(s / k)
    means.sort()
    return {"lo": round(means[max(0, math.ceil(BOOTSTRAP_LO / 100 * n) - 1)], 4),
            "hi": round(means[min(n - 1, math.ceil(BOOTSTRAP_HI / 100 * n) - 1)], 4),
            "mean": _mean(values), "n": k,
            "method": "bootstrap percentile", "n_boot": n, "seed": seed}


def _delta(before, after, ndigits: int = 4):
    """Разница двух чисел, переживающая ``None`` (нет числа — нет дельты, а не 0)."""
    if before is None or after is None:
        return None
    return round(after - before, ndigits)


def _clean_reading(records: list[dict]) -> dict:
    """Чтение языка по ходам, которые заведомо не измеряют петлю (S3ak).

    Правило объявлено ДО замера: в «чистое» чтение попадает генерация, у которой
    (а) ход **дописан** (`stop_reason = turn_end`) и (б) **нет петли**
    (`looped = false`). Первое отсекает обрыв лимитом, второе — повторы.

    Зачем это отдельное чтение, а не общий счётчик. TASK S3ak прямо запрещает
    делать выводы о языке по зацикленным генерациям, и запрет предметный: у
    зацикленной генерации ответ либо отсутствует, либо переписан повтором, и её
    «доля кириллицы» отвечает на вопрос «сколько русского в повторе», а не «на
    каком языке модель отвечает». Одновременно «чистое» чтение легко превратить в
    способ показать только удобные генерации, поэтому оно идёт **рядом** с полным
    (в `aggregate.cyr_answer`), а не вместо него, и оба числа попадают в свод.

    ``margin_to_refute`` — запас до границы опровержения ADR-039 (0.4). Он и есть
    ответ на «вердикт не должен держаться на 0.008»: рядом лежит CI95, и если
    интервал накрывает границу, вердикт объявляется недоказанным.
    """
    sub = [r for r in records
           if r.get("stop_reason") == STOP_TURN_END and not r["metrics"]["looped"]]
    m = _metric(sub, "cyr_answer")
    ci = _ci(sub, "cyr_answer")
    return {
        "rule": "stop_reason=turn_end И looped=false; прочие генерации помечены отдельно",
        "n": len(sub),
        "share_of_all": round(len(sub) / len(records), 4) if records else None,
        "cyr_answer": m,
        "cyr_think": _metric(sub, "cyr_think"),
        "ci95": ci,
        "margin_to_refute": (None if m["with_zeros"] is None
                             else round(m["with_zeros"] - CRIT_ANSWER_REFUTE_MAX, 4)),
        "ci_covers_refute_boundary": (
            None if ci["lo"] is None else bool(ci["lo"] < CRIT_ANSWER_REFUTE_MAX <= ci["hi"])),
        #: Сколько генераций исключено и почему — иначе «чистое» чтение читалось бы
        #: как «все генерации», и запрет TASK был бы выполнен молча.
        "excluded_looped": sum(bool(r["metrics"]["looped"]) for r in records),
        "excluded_by_limit": sum(1 for r in records
                                 if r.get("stop_reason") != STOP_TURN_END
                                 and not r["metrics"]["looped"]),
    }


def _ci(records: list[dict], key: str, path: tuple[str, ...] = ("metrics",)) -> dict:
    """Бутстрап-интервал метрики по генерациям, читаемой по тому же пути, что `_metric`."""
    vals = []
    for r in records:
        node = r
        for step in path:
            node = node.get(step) if isinstance(node, dict) else None
        v = node.get(key) if isinstance(node, dict) else None
        vals.append(0.0 if v is None else v)     # правило 3: нет букв — нет русского
    return bootstrap_ci(vals)


def _pct(values: list[int], q: float) -> int | None:
    """Перцентиль по **ближайшему рангу** (nearest-rank), а не интерполяцией.

    Способ объявлен потому, что от него зависит число: у 24 генераций
    интерполяция и ближайший ранг дают разные ответы. Ближайший ранг выбран
    намеренно — это **наблюдённая** длина генерации, а не оценка между двумя
    наблюдениями: бюджет токенов назначается по «сколько реально бывает», и
    выдуманная точка между двумя прогонами для этого не годится.
    """
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(q * len(s)))
    return s[k - 1]


def length_stats(records: list[dict], budget: int | None = None) -> dict:
    """Длины генераций состояния: медиана/p90 и насколько они упираются в бюджет."""
    toks = [int(r.get("n_new_tokens") or 0) for r in records]
    if not toks:
        return {"n": 0}
    out = {"n": len(toks), "median": _pct(toks, 0.5), "p90": _pct(toks, 0.9),
           "min": min(toks), "max": max(toks), "mean": _mean([float(t) for t in toks]),
           "percentile_method": "nearest-rank",
           #: Доля бюджета, которую съедает медианная генерация: если медиана
           #: равна бюджету — замер меряет бюджет, а не модель.
           "budget": budget,
           "median_over_budget": (round(_pct(toks, 0.5) / budget, 4) if budget else None)}
    return out


def stop_breakdown(records: list[dict]) -> dict:
    """Разбор причин конца генерации + формат **среди дописанных ходов**.

    Зачем разделение. `unclosed_think_share` по всем генерациям смешивает
    «модель не закрыла рассуждение» и «генерацию оборвал лимит внутри
    рассуждения». Первое — деградация формата, второе — свойство бюджета. Вердикт
    о формате может опираться только на первое, поэтому число считается отдельно
    по генерациям с `stop_reason = turn_end`, а общее остаётся рядом.
    """
    if not records:
        return {"n": 0}
    n = len(records)
    reasons: dict[str, int] = {}
    for r in records:
        reasons[r.get("stop_reason") or "?"] = reasons.get(r.get("stop_reason") or "?", 0) + 1
    natural = [r for r in records if r.get("stop_reason") == STOP_TURN_END]
    truncated = [r for r in records if r.get("stop_reason") != STOP_TURN_END]

    def _shares(sub: list[dict]) -> dict:
        if not sub:
            return {"n": 0}
        k = len(sub)
        return {
            "n": k,
            "unclosed_think_share": round(sum(bool(r["metrics"]["unclosed_think"]) for r in sub) / k, 4),
            "mode_share_tool_call": round(sum(bool(r["metrics"]["has_tool_call"]) for r in sub) / k, 4),
            "looped_share": round(sum(bool(r["metrics"]["looped"]) for r in sub) / k, 4),
            "cyr_answer_with_zeros": _metric(sub, "cyr_answer")["with_zeros"],
            "answer_coverage": _metric(sub, "cyr_answer")["coverage"],
        }

    return {
        "n": n,
        "reasons": reasons,
        "reasons_share": {k: round(v / n, 4) for k, v in sorted(reasons.items())},
        "natural_stop_share": round(len(natural) / n, 4),
        "truncated_share": round(len(truncated) / n, 4),
        #: Где именно обрывается лимит — по обрезанным генерациям.
        "where_truncated": {k: v for k, v in sorted(reasons.items())
                            if k != STOP_TURN_END},
        "natural": _shares(natural),
        "truncated": _shares(truncated),
    }


def aggregate(records: list[dict], budget: int | None = None) -> dict:
    """Свод по всем генерациям состояния.

    Основные величины — ``cyr_think``/``cyr_answer`` в чтении ``with_zeros``
    (правило 3); ``defined_only`` и ``coverage`` идут рядом и вердикт не двигают.
    """
    n = len(records)
    if not n:
        return {"n": 0}
    agg = {
        "n": n,
        "aggregation": "среднее по генерациям; доля кириллицы внутри сегмента — по буквам",
        "cyr_think": _metric(records, "cyr_think"),
        "cyr_answer": _metric(records, "cyr_answer"),
        "cyr_answer_prose": _metric(records, "cyr_answer_prose"),
        "cyr_overall_reference_only": _metric(records, "cyr_overall"),
        "mode_share_any": round(sum(r["metrics"]["mode_share"] for r in records) / n, 4),
        "mode_share_think": round(sum(bool(r["metrics"]["has_think"]) for r in records) / n, 4),
        "mode_share_tool_call": round(sum(bool(r["metrics"]["has_tool_call"]) for r in records) / n, 4),
        "unclosed_think_share": round(sum(bool(r["metrics"]["unclosed_think"]) for r in records) / n, 4),
        "stray_think_close_share": round(sum(bool(r["metrics"]["stray_think_close"]) for r in records) / n, 4),
        "multi_think_block_share": round(
            sum(r["metrics"]["n_think_blocks"] >= 2 for r in records) / n, 4),
        "think_blocks_mean": round(
            sum(r["metrics"]["n_think_blocks"] for r in records) / n, 4),
        "truncated_share": round(sum(bool(r.get("hit_limit")) for r in records) / n, 4),
        #: S3aj: длины, причины конца и вырождение — то, по чему снимается артефакт.
        "lengths": length_stats(records, budget),
        "stop": stop_breakdown(records),
        "looped_share": round(sum(bool(r["metrics"]["looped"]) for r in records) / n, 4),
        #: S3ak: зацикленные генерации помечены отдельно и исключены из «чистого»
        #: чтения. Причина прямая: у зацикленной генерации ответной части либо нет,
        #: либо она переписана повтором, и её доля кириллицы меряет петлю, а не язык.
        #: Основное чтение языка (`clean`) — **дописанные ходы без петли**; полное
        #: чтение остаётся рядом, чтобы подмена одного другим была видна.
        "clean": _clean_reading(records),
        "cyr_answer_ci95": _ci(records, "cyr_answer"),
        #: Прежняя арифметика (v1) на тех же генерациях — дельта правки прибора.
        "legacy_reading": {
            "cyr_think": _metric(records, "cyr_think", ("metrics", "legacy")),
            "cyr_answer": _metric(records, "cyr_answer", ("metrics", "legacy")),
            "cyr_answer_prose": _metric(records, "cyr_answer_prose", ("metrics", "legacy")),
        },
        "instrument": INSTRUMENT_VERSION,
        "per_tag": {},
    }
    agg["instrument_fix"] = {
        "instrument": INSTRUMENT_VERSION,
        "cyr_answer_before_v1": agg["legacy_reading"]["cyr_answer"]["with_zeros"],
        "cyr_answer_after_v2": agg["cyr_answer"]["with_zeros"],
        "delta": _delta(agg["legacy_reading"]["cyr_answer"]["with_zeros"],
                        agg["cyr_answer"]["with_zeros"]),
        "cyr_think_before_v1": agg["legacy_reading"]["cyr_think"]["with_zeros"],
        "cyr_think_after_v2": agg["cyr_think"]["with_zeros"],
    }
    for tag in sorted({r["tag"] for r in records}):
        sub = [r for r in records if r["tag"] == tag]
        agg["per_tag"][tag] = {
            "n": len(sub),
            "cyr_think": _metric(sub, "cyr_think")["with_zeros"],
            "cyr_answer": _metric(sub, "cyr_answer")["with_zeros"],
            "mode_share_any": round(sum(r["metrics"]["mode_share"] for r in sub) / len(sub), 4),
        }
    return agg


# ─────────────────────────── критерий (ADR-039 п.3) ───────────────────────────

def apply_criterion(cyr_answer: float | None, cyr_think: float | None,
                    answer_coverage: float | None = None) -> dict:
    """Зона вердикта по числам, объявленным ДО замера (ADR-039 п.3).

    Гипотеза владельца — «ризонинг есть навык, язык рассуждения не важен» — имеет
    **две** посылки: ответная часть русская И рассуждение осталось английским.
    ADR-039 объявляет три исхода: подтверждение (ответ ≥ 0.5 при think ≤ 0.3),
    опровержение (ответ < 0.4) и промежуточную зону 0.4–0.5 («недостаточно
    данных»). Четвёртая комбинация — ответ русский, но и рассуждение
    русскоязычное — **в критерии не описана**; прибор не имеет права трактовать её
    в чью-либо пользу, поэтому она получает собственную метку, а решение по ней
    уходит архитектору (а не в «частичное подтверждение»).

    Перед критерием стоит **страж валидности замера** (`answer_coverage`): если
    ответной части нет у большинства генераций, число `cyr_answer` измеряет
    обрыв, а не язык, и вердикт не выносится ни в одну сторону. Правило
    симметрично (блокирует и подтверждение, и опровержение) и объявлено заранее.
    """
    if cyr_answer is None or cyr_think is None:
        return {"zone": "not_measured", "why": "нет числа — нет вердикта"}
    if answer_coverage is not None and answer_coverage < ANSWER_COVERAGE_FLOOR:
        return {"zone": "insufficient_coverage",
                "why": f"ответная часть есть лишь у {answer_coverage} генераций "
                       f"(порог {ANSWER_COVERAGE_FLOOR}): число cyr_answer={cyr_answer} "
                       "измеряет обрыв генерации, а не язык ответа — недостаточно данных"}
    if cyr_answer < CRIT_ANSWER_REFUTE_MAX:
        return {"zone": ZONE_REFUTED,
                "why": f"ответная часть кириллица {cyr_answer} < {CRIT_ANSWER_REFUTE_MAX}: "
                       "язык входа протекает, ответ ушёл в английский"}
    if cyr_answer < CRIT_ANSWER_RU_MIN:
        return {"zone": ZONE_INTERMEDIATE,
                "why": f"ответная часть кириллица {cyr_answer} в зоне "
                       f"[{CRIT_ANSWER_REFUTE_MAX}; {CRIT_ANSWER_RU_MIN}) — не решение, "
                       "а повод увеличить выборку"}
    if cyr_think > CRIT_THINK_EN_MAX:
        return {"zone": ZONE_OUT_OF_SCOPE,
                "why": f"ответ русский ({cyr_answer} ≥ {CRIT_ANSWER_RU_MIN}), но рассуждение "
                       f"тоже русскоязычное ({cyr_think} > {CRIT_THINK_EN_MAX}): посылка "
                       "«think остался английским» критерием ADR-039 не выполнена — "
                       "комбинация вне объявленного критерия"}
    return {"zone": ZONE_CONFIRMED,
            "why": f"ответ русский ({cyr_answer} ≥ {CRIT_ANSWER_RU_MIN}) при английском "
                   f"рассуждении ({cyr_think} ≤ {CRIT_THINK_EN_MAX})"}


# ─────────────────────────── набор промптов ───────────────────────────

#: Расширение ядра до 24 проб (≥20 генераций на состояние — требование TASK).
#: Ядро (5) — дословно из probe_control (импорт выше), новые 19 повторяют его
#: устройство: доменные — с UNIFIED_SYSTEM_PROMPT (условие деплоя), общие и
#: инструкции — без system (сырая языковая способность).
EXTRA_PROMPTS = [
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про оценку качества генерации текста."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про методы борьбы с катастрофическим забыванием."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов, связанных с механизмом внимания в трансформерах."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое спекулятивное декодирование и зачем оно нужно?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Чем отличается LoRA от полного дообучения модели?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое групповая относительная оценка преимущества."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое энтропийный коллапс при обучении с подкреплением."},
    {"tag": "general_language", "system": False,
     "text": "Опиши, как устроен процесс фотосинтеза у растений."},
    {"tag": "general_language", "system": False,
     "text": "Объясни, почему небо кажется синим."},
    {"tag": "general_language", "system": False,
     "text": "Расскажи, чем полезна утренняя зарядка."},
    {"tag": "general_language", "system": False,
     "text": "Напиши короткий рассказ о собаке, которая нашла дом."},
    {"tag": "general_reasoning", "system": False,
     "text": "У Пети было 12 марок, треть он отдал другу. Сколько марок осталось? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "Поезд идёт со скоростью 60 километров в час два с половиной часа. "
             "Какой путь он пройдёт? Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "В корзине 5 красных и 3 зелёных яблока. Какая доля яблок красная? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "У Ани вдвое больше конфет, чем у Бори, а вместе у них 18 конфет. "
             "Сколько конфет у Ани? Ответь одним предложением."},
    {"tag": "instruction", "system": False,
     "text": "Перечисли три причины заниматься спортом."},
    {"tag": "instruction", "system": False,
     "text": "Напиши одно предложение о пользе сна."},
    {"tag": "instruction", "system": False,
     "text": "Сформулируй одно правило безопасного поведения в интернете."},
    {"tag": "instruction", "system": False,
     "text": "Придумай короткий заголовок для статьи о пользе чтения."},
]

#: S3ak: расширение до ≥100 проб. Зачем ещё раз расширять набор.
#: Вердикт S3aj по языку (0.3919 против границы 0.4) держался на запасе 0.008 при
#: 24 генерациях, то есть **на четверти шага одной генерации** (1/24 = 0.042):
#: замена одной пробы переворачивала вывод. Это не замер, а жребий. TASK S3ak
#: требует выборку ≥100 генераций — здесь она берётся **расширением набора
#: промптов**, а не повторами одного и того же промпта: у greedy повторы дали бы
#: побайтово те же ответы, и «100 генераций» оказались бы одной, пересчитанной
#: сто раз. Протокол (chat template, системный промпт, greedy, остановка на конце
#: хода, бюджет) не меняется — меняется только число проб.
#:
#: Состав подобран ДО замера по устройству прежнего набора (ядро 5 + 19): те же
#: пять тегов в тех же пропорциях, доменные — с `UNIFIED_SYSTEM_PROMPT` (условие
#: деплоя: у модели есть инструмент), общие и инструкции — без него (сырая
#: языковая способность). Промпты не отбирались по результату: набор объявлен
#: здесь целиком и пиннуется хешем в отчёте (`prompts_digest`).
EXTRA_PROMPTS_2 = [
    # ── доменные с инструментом (system) — продолжение тега domain_tool ──
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про методы интерпретируемости нейронных сетей."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про разреженные смеси экспертов."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про квантование моделей."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про состязательные атаки на модели."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про обучение без учителя."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про рекуррентные сети."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про оценку неопределённости моделей."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про дистилляцию знаний."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про градиентный спуск."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про обработку длинных контекстов."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про обучение с учителем."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про нормализацию в нейронных сетях."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про планирование в агентных системах."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про генерацию текста."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про функции потерь."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про байесовские методы в машинном обучении."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про мультимодальные модели."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про метрики качества классификации."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про обучение на малых данных."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про состязательную устойчивость."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про нейронные сети с памятью."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про разреживание весов."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про обучение ранжированию."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про регуляризацию."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про аудио-модели."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про языковые модели для кода."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про встраивание графов."},
    {"tag": "domain_tool", "system": True,
     "text": "Подбери концепты про обучение без подкрепления с подкреплением."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди концепты про предобучение на больших корпусах."},
    {"tag": "domain_tool", "system": True,
     "text": "Найди определения концептов про разметку данных."},
    # ── доменные без инструмента (system) — продолжение тега domain_knowledge ──
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое механизм самовнимания?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое переобучение и как с ним борются?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое эмбеддинги слов."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Чем отличается батч-нормализация от слоевой?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое остаточные связи в нейронных сетях?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое функция активации GELU."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое токенизация и зачем она нужна?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое градиентный взрыв."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое обучение с частичным учителем?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Чем отличается полное дообучение модели от настройки адаптеров?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое окно контекста модели?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое матрица внимания."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое разметка промптов в обучении?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое кросс-валидация."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое смещение и разброс модели?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Чем отличается авторегрессионная модель от маскированной?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое пакетная обработка последовательностей?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое скрытое состояние модели."},
    {"tag": "domain_knowledge", "system": True,
     "text": "Что такое скорость обучения и как её выбирают?"},
    {"tag": "domain_knowledge", "system": True,
     "text": "Объясни, что такое регуляризация отсева."},
    # ── общие языковые (без system) ──
    {"tag": "general_language", "system": False,
     "text": "Объясни, почему зимой идёт снег."},
    {"tag": "general_language", "system": False,
     "text": "Опиши, как работает электрический чайник."},
    {"tag": "general_language", "system": False,
     "text": "Расскажи, как люди научились измерять время."},
    {"tag": "general_language", "system": False,
     "text": "Объясни, почему листья зеленеют весной."},
    {"tag": "general_language", "system": False,
     "text": "Опиши, как устроен велосипед."},
    {"tag": "general_language", "system": False,
     "text": "Расскажи, откуда берётся дождь."},
    {"tag": "general_language", "system": False,
     "text": "Объясни, почему море солёное."},
    {"tag": "general_language", "system": False,
     "text": "Опиши, как пчёлы делают мёд."},
    {"tag": "general_language", "system": False,
     "text": "Расскажи, как работает компас."},
    {"tag": "general_language", "system": False,
     "text": "Объясни, почему небо меняет цвет на закате."},
    {"tag": "general_language", "system": False,
     "text": "Опиши, как растёт дерево из семени."},
    {"tag": "general_language", "system": False,
     "text": "Расскажи, как люди придумали колесо."},
    # ── общие рассуждения (без system) ──
    {"tag": "general_reasoning", "system": False,
     "text": "В классе 30 учеников, две трети из них девочки. Сколько мальчиков? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "Рабочий делает 8 деталей в час. Сколько деталей он сделает за 6 часов? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "Книга стоит 400 рублей, скидка 25 процентов. Сколько заплатит покупатель? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "В баке было 50 литров воды, вылили 20 процентов. Сколько осталось? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "Поезд прошёл 240 километров за 3 часа. Какова его скорость? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "У Пети 15 конфет, у Васи в три раза меньше. Сколько конфет у обоих? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "Товар подорожал с 200 до 250 рублей. На сколько процентов? "
             "Ответь одним предложением."},
    {"tag": "general_reasoning", "system": False,
     "text": "Садовник посадил 12 деревьев в 4 ряда поровну. Сколько деревьев в ряду? "
             "Ответь одним предложением."},
    # ── инструкции (без system) ──
    {"tag": "instruction", "system": False,
     "text": "Перечисли три способа экономить воду."},
    {"tag": "instruction", "system": False,
     "text": "Напиши одно предложение о пользе прогулок."},
    {"tag": "instruction", "system": False,
     "text": "Сформулируй одно правило вежливого разговора."},
    {"tag": "instruction", "system": False,
     "text": "Придумай короткий заголовок для заметки о зимнем спорте."},
    {"tag": "instruction", "system": False,
     "text": "Перечисли три причины учить иностранный язык."},
    {"tag": "instruction", "system": False,
     "text": "Напиши одно предложение о пользе домашних животных."},
    {"tag": "instruction", "system": False,
     "text": "Сформулируй одно правило безопасности на дороге."},
    {"tag": "instruction", "system": False,
     "text": "Придумай короткий заголовок для статьи о пользе музыки."},
    {"tag": "instruction", "system": False,
     "text": "Перечисли три способа начать утро бодро."},
    {"tag": "instruction", "system": False,
     "text": "Напиши одно предложение о пользе планирования дня."},
]

LANG_PROMPTS = list(CORE_PROMPTS) + EXTRA_PROMPTS      # 5 + 19 = 24
#: Широкий набор S3ak: 5 + 19 + 80 = 104 ≥ 100 — требование TASK по выборке.
WIDE_PROMPTS = LANG_PROMPTS + EXTRA_PROMPTS_2

PROMPT_SETS = {"core": list(CORE_PROMPTS), "extended": LANG_PROMPTS,
               "wide": WIDE_PROMPTS}


def prompts_digest(prompts: list[dict]) -> str:
    """Хеш набора промптов: набор — часть прибора, и он должен быть пиннут."""
    blob = json.dumps([[p["tag"], bool(p["system"]), p["text"]] for p in prompts],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ─────────────────────────── генерация (протокол S3ab) ───────────────────────────

def turn_end_ids(tokenizer) -> list[int]:
    """Id токенов, на которых кончается ход: `<|im_end|>` и, если он есть, eos.

    Список собирается по токенизатору, а не константой: у ревизий Qwen2.5
    `eos_token_id` разный (у весов базы 151643, у инструкт-моделей 151645), и
    зашитое число сделало бы «остановку на конце хода» зависимой от того, какая
    ревизия подсунута.
    """
    ids = []
    for tok in (TURN_END,):
        i = tokenizer.convert_tokens_to_ids(tok)
        if isinstance(i, int) and i >= 0 and i != tokenizer.unk_token_id:
            ids.append(i)
    if tokenizer.eos_token_id is not None:
        ids.append(int(tokenizer.eos_token_id))
    return sorted(set(ids))


def resolve_no_repeat_ngram(requested: int | None,
                            legacy: bool) -> tuple[int | None, str | None]:
    """Разрешить режим запрета повторов n-грамм: штатный — по умолчанию (S3al).

    Три случая, и каждый назван, а не выведен из молчания:

    * флаг не задан → штатный режим (`STANDARD_NO_REPEAT_NGRAM`); при
      `--legacy-decoding` — прежний протокол без запрета (S3ab/S3ai/S3aj);
    * задано положительное N → запрет N-грамм (N=4 — штатный; другое N допустимо,
      потому что это уже осознанный выбор, и он виден в имени режима);
    * задан 0 (или меньше) → прогон **без** запрета. Это и есть запрещённый для
      выводов режим: он отклоняется, пока не назван `--legacy-decoding`. Отказ, а
      не тихое согласие: молчаливый прогон без запрета — ровно тот дефект, из-за
      которого замеры S3aj пришлось переснимать (петли приняли за деградацию).

    Возврат — ``(эффективное N или None, текст отказа или None)``. Отказ
    возвращается строкой, а не исключением: прибор обязан напечатать причину и
    выйти с кодом 1, а не упасть трейсбеком.
    """
    if requested is None:
        return (None, None) if legacy else (STANDARD_NO_REPEAT_NGRAM, None)
    if int(requested) <= 0:
        if legacy:
            return None, None
        return None, (
            "отказ: прогон без запрета повторов n-грамм (--no-repeat-ngram "
            f"{int(requested)}) запрещён — в этом режиме метрики языка и формата "
            "измеряют петлю, а не модель (S3ak: зациклено 0.5417 против 0.0000 при "
            "запрете 4-грамм). Штатный режим — запрет 4-грамм (включён по умолчанию); "
            "для воспроизведения прежнего протокола S3ab/S3ai/S3aj добавьте "
            "--legacy-decoding")
    return int(requested), None


def decoding_protocol_block(no_repeat_ngram: int | None, requested: int | None,
                            legacy: bool) -> dict:
    """Режим декодирования как часть протокола стадии — в отчёт, а не в текст свода.

    Блок отвечает на три вопроса, которые до S3al приходилось восстанавливать по
    флагам командной строки в логах запусков: какой режим **штатный**, включён ли
    он **в этом** отчёте и **можно ли** по этому отчёту делать выводы о языке и
    формате.
    """
    standard = bool(no_repeat_ngram)
    if requested is None:
        source = ("умолчание прибора (STANDARD_NO_REPEAT_NGRAM)" if standard
                  else "прежний протокол: умолчание снято --legacy-decoding")
    else:
        source = "явно указан флагом --no-repeat-ngram"
    return {
        "default_mode": "greedy + запрет повторов 4-грамм (no_repeat_ngram=4)",
        "standard_no_repeat_ngram": STANDARD_NO_REPEAT_NGRAM,
        "no_repeat_ngram": no_repeat_ngram,
        "no_repeat_ngram_requested": requested,
        "standard": standard,
        "legacy_decoding": bool(legacy),
        "source": source,
        #: Выводы о языке и формате по такому отчёту разрешены ровно тогда, когда
        #: запрет повторов включён: иначе числа описывают петлю.
        "allowed_for_conclusions": standard,
        "rules": {
            "greedy_only_forbidden": "любые выводы о формате и языке по генерациям без "
                                     "запрета повторов n-грамм запрещены (S3al, ADR-040 п.6)",
            "sampling_is_extra_check": "sampling — дополнительная проверка свободного "
                                       "режима, а не штатный режим: он петлю снижает "
                                       "частично (0.3333), а не снимает",
            "legacy_is_for_identity": "--legacy-decoding существует для воспроизведения "
                                      "прежних замеров побайтово (мост тождества), а не "
                                      "для новых выводов",
        },
    }


def decoding_label(decoding: str, temperature: float, top_p: float,
                   repetition_penalty: float | None, no_repeat_ngram: int | None) -> str:
    """Имя режима декодирования для отчёта — из самих параметров, а не из флага.

    Почему не просто «sample»: два прогона с разными `temperature` — разные режимы,
    и в таблице «режим × доля петель» они обязаны различаться именами. Имя
    собирается из чисел, поэтому подделать его флагом нельзя.
    """
    parts = [decoding]
    if decoding == "sample":
        parts += [f"T{temperature:g}", f"top_p{top_p:g}"]
    if repetition_penalty is not None and float(repetition_penalty) != 1.0:
        parts.append(f"rp{repetition_penalty:g}")
    if no_repeat_ngram:
        parts.append(f"nogram{int(no_repeat_ngram)}")
    return "_".join(parts)


def _gen_kwargs(decoding: str, temperature: float, top_p: float,
                repetition_penalty: float | None, no_repeat_ngram: int | None,
                eos_ids: list[int] | None, pad_id: int | None) -> dict:
    """Параметры `generate` из режима декодирования (объявлены флагами CLI)."""
    kw: dict = {"do_sample": decoding == "sample"}
    if decoding == "sample":
        kw["temperature"] = temperature
        kw["top_p"] = top_p
    if repetition_penalty is not None:
        kw["repetition_penalty"] = repetition_penalty
    if no_repeat_ngram:
        kw["no_repeat_ngram_size"] = int(no_repeat_ngram)
    if eos_ids is not None:
        kw["eos_token_id"] = eos_ids
    kw["pad_token_id"] = pad_id
    return kw


def generate_all(model, tokenizer, torch, tag: str, device: str,
                 prompts: list[dict], max_new_tokens: int = MAX_NEW_TOKENS,
                 stop_at_turn_end: bool = False, decoding: str = "greedy",
                 temperature: float = 0.7, top_p: float = 0.9,
                 repetition_penalty: float | None = None,
                 no_repeat_ngram: int | None = None, seed: int = SEED,
                 batch_size: int = 1) -> list[dict]:
    """Пробы одним проходом, как в probe_control.generate_all: chat template, greedy, 384.

    Отличие от S3ab — только в записи и в бюджете токенов (объявлен флагом):
    сохраняются число сгенерированных токенов и признак упора в лимит
    (`hit_limit`). Без него «ответа нет» неотличимо от «ответ не влез в лимит»,
    а это разные диагнозы, и второй делает замер языка недействительным.

    ``stop_at_turn_end`` (S3aj) добавляет к условию остановки конец хода
    (`<|im_end|>`): без него генерация переписывает свой же конец хода и уходит в
    следующий круг, а меряется при этом не ответ модели, а её продолжение после
    ответа. Флаг по умолчанию выключен, чтобы протокол S3ab воспроизводился
    побайтово.
    """
    mode = decoding_label(decoding, temperature, top_p, repetition_penalty, no_repeat_ngram)
    eos_ids = turn_end_ids(tokenizer) if stop_at_turn_end else None
    pad_id = tokenizer.pad_token_id or tokenizer.eos_token_id
    old_padding_side = tokenizer.padding_side
    results: list[dict] = []
    if batch_size < 1:
        batch_size = 1
    #: Батч — ускорение замера, а не смена протокола: при `batch_size == 1` код
    #: идёт ровно тем же путём, что у S3ab/S3ai/S3aj (одна последовательность,
    #: `torch.manual_seed(seed + i)` перед каждой пробой). При `batch_size > 1`
    #: слева добавляется паддинг с маской внимания — для greedy это не меняет
    #: ни одного токена (проверяется тождеством байтов в своде S3ak: пакетный
    #: прогон ядра обязан совпасть с ответами S3ab), а для sampling меняет
    #: разыгранные числа, поэтому сид ставится на **батч**, а не на пробу, и это
    #: записано в протокол.
    try:
        if batch_size > 1:
            tokenizer.padding_side = "left"
        for start in range(0, len(prompts), batch_size):
            chunk = prompts[start:start + batch_size]
            texts = []
            for j, p in enumerate(chunk):
                msgs = ([{"role": "system", "content": UNIFIED_SYSTEM_PROMPT}]
                        if p["system"] else []) + [{"role": "user", "content": p["text"]}]
                texts.append(tokenizer.apply_chat_template(
                    msgs, tokenize=False, add_generation_prompt=True))
            #: Сид ставится только в режиме sampling: у greedy он не влияет ни на
            #: один токен, а лишний вызов RNG в пути S3ab сделал бы «тот же
            #: протокол» обещанием, а не тождеством кода.
            if decoding == "sample":
                torch.manual_seed(seed + start)
            enc = tokenizer(texts, return_tensors="pt",
                            padding=(batch_size > 1)).to(device)
            gen_kw = _gen_kwargs(decoding, temperature, top_p, repetition_penalty,
                                 no_repeat_ngram, eos_ids, pad_id)
            gen_kw["max_new_tokens"] = max_new_tokens
            with torch.no_grad():
                out = model.generate(**enc, **gen_kw)
            prompt_len = enc["input_ids"].shape[1]
            for j, p in enumerate(chunk):
                new_ids = out[j][prompt_len:]
                #: У пакетной генерации хвост добит паддингом: срезаем его, иначе
                #: «длина генерации» измерила бы длину самого длинного соседа по
                #: батчу, а не эту пробу.
                if batch_size > 1:
                    keep = (new_ids != pad_id).nonzero(as_tuple=True)[0]
                    new_ids = new_ids[:int(keep[-1]) + 1] if len(keep) else new_ids[:0]
                resp = tokenizer.decode(new_ids, skip_special_tokens=False).strip()
                hit_limit = int(new_ids.shape[0]) >= max_new_tokens
                results.append({"tag": p["tag"], "system": p["system"], "prompt": p["text"],
                                "response": resp, "n_new_tokens": int(new_ids.shape[0]),
                                "hit_limit": hit_limit,
                                "stop_reason": stop_reason(resp, hit_limit),
                                "decoding": mode, "batch_size": batch_size,
                                "metrics": language_metrics(resp)})
                m = results[-1]["metrics"]
                note(f"  [{tag}/{mode}] {p['tag']}: cyr_think={m['cyr_think']} "
                     f"cyr_answer={m['cyr_answer']} (v1 {m['legacy']['cyr_answer']}) "
                     f"mode={m['has_think']}/{m['has_tool_call']} tok={len(new_ids)}"
                     f" stop={results[-1]['stop_reason']}"
                     f"{' (ЛИМИТ)' if hit_limit else ''}"
                     f"{' ЗАЦИКЛ' if m['looped'] else ''} | {resp[:70]!r}")
    finally:
        tokenizer.padding_side = old_padding_side
    return results


def load_ckpt_into(model, path: Path, torch) -> dict:
    """Копия `_load_ckpt_with_resize` пайплайна (как в probe_control): без неё
    строки спецтокенов 151665..151670 не совпадут по форме, `strict=False` молча
    оставит случайную инициализацию — и проба измерила бы не чекпойнт."""
    ck = torch.load(str(path), map_location="cpu", mmap=True, weights_only=False)
    sd = ck["model"] if isinstance(ck, dict) and "model" in ck else ck
    cur = model.state_dict()
    moved = []
    for key in list(sd.keys()):
        if key in cur and tuple(sd[key].shape) != tuple(cur[key].shape):
            old = sd.pop(key)
            if "embed_tokens" in key or key in ("lm_head.weight",):
                moved.append({"key": key, "from": list(old.shape), "to": list(cur[key].shape)})
                new = cur[key].clone()
                k = min(old.shape[0], new.shape[0])
                new[:k] = old[:k]
                sd[key] = new
                if key == "lm_head.weight" and hasattr(model, "lm_head"):
                    model.lm_head.weight = torch.nn.Parameter(new)
            else:
                moved.append({"key": key, "from": list(old.shape),
                              "to": list(cur[key].shape), "dropped": True})
    missing, unexpected = model.load_state_dict(sd, strict=False)
    del ck, sd
    torch.cuda.empty_cache()
    return {"moved_for_resize": moved,
            "missing_keys": len(missing), "unexpected_keys": len(unexpected)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def device_free_bytes(torch, device: str) -> int | None:
    if not device.startswith("cuda") or not torch.cuda.is_available():
        return None
    free, _total = torch.cuda.mem_get_info()
    return int(free)


def device_problem(torch, device: str) -> str | None:
    """Работает ли устройство **сейчас** — проверка до загрузки модели.

    Зачем отдельно от подсчёта свободной памяти. `torch.cuda.is_available()`
    отвечает про драйвер, а не про то, отзывается ли он в этом процессе: после
    ошибки драйвера на шине (Xid) `cuInit` отдаёт `CUDA_ERROR_UNKNOWN`, и первый
    же, кто обратится к устройству, получает трейсбек вместо диагноза — замер
    «падает», хотя должен «не состояться». Проба, которая падает, неотличима от
    сломанного прибора; NOT-VERIFIED с названной причиной — отличима.

    Проверка настоящая: одна аллокация и синхронизация. Читать
    `is_available()` и верить ему нельзя — оно кэширует ответ, а состояние
    драйвера меняется под ногами.
    """
    if not device.startswith("cuda"):
        return None
    try:
        if not torch.cuda.is_available():
            return "torch.cuda.is_available() = False (драйвер или устройство недоступны)"
        probe = torch.zeros(1, device=device)
        _ = int(probe.sum().item())
        torch.cuda.synchronize()
        del probe
    except Exception as exc:  # noqa: BLE001 — нужна любая причина отказа устройства
        return f"{type(exc).__name__}: {exc}"
    return None


# ─────────────────────────── прогон ───────────────────────────

def parse_ckpt_specs(specs: list[str]) -> list[tuple[str, str | None]]:
    """Разобрать `--ckpt TAG=PATH` заранее, до загрузки модели.

    Нужно не для красоты: разбор «на месте» означал бы, что опечатка в аргументе
    стоит полной загрузки весов, и NOT-VERIFIED приходит через минуты вместо
    секунд. Возвращаются **все** записи, включая битые (`None`) — битая запись
    должна попасть в отчёт, а не исчезнуть.
    """
    out: list[tuple[str, str | None]] = []
    for spec in specs:
        tag, _, path = spec.partition("=")
        if not tag or not path:
            note(f"  --ckpt '{spec}': ожидается TAG=PATH — пропуск")
            out.append((spec or "?", None))
        else:
            out.append((tag, path))
    return out


def _rel(p: Path) -> str:
    """Путь для манифеста: относительный, без выхода через `..` (C-012)."""
    p = Path(p)
    try:
        r = os.path.relpath(p.resolve(), CASE)
    except ValueError:                      # другой диск — сравнивать нечего
        return p.name
    return p.name if r.startswith("..") else r


def write_run_manifest(out_path: Path, out: dict, args=None) -> dict:
    """Манифест прогона проб (AD-2/C-012) — рядом с отчётом, в каталоге прогона.

    Почему у пробы вообще есть манифест: каталог `runs/<прогон>/` без манифеста —
    нарушение AD-2 («прогон без манифеста не является доказательством»), и гейт
    C-012 краснеет на всём дереве из-за одного такого каталога. Что здесь
    «датасет» и «пайплайн»: набор промптов и код прибора — то есть **сам прибор**
    (он и есть источник чисел), а измеряемые состояния пиннуются их хешами.

    Пути — относительные: C-012 запрещает абсолютные и выход через `..`.
    """
    tool = Path(__file__).resolve()
    rel = _rel(tool)
    sha = out.get("tool_sha256") or sha256_file(tool)
    states = {tag: {"checkpoint": v.get("checkpoint"),
                    "checkpoint_sha256": v.get("checkpoint_sha256"),
                    "checkpoint_bytes": v.get("checkpoint_bytes"),
                    "probes": len(v.get("probes", [])),
                    "max_new_tokens": v.get("max_new_tokens"),
                    "report": v.get("report") or _rel(out_path)}
              for tag, v in out["states"].items() if "probes" in v}
    manifest = {
        "dataset_path": rel,
        "dataset_sha256": sha,
        "dataset_note":
            "прогон не меряет корпус: измеряются состояния (чекпойнты) на "
            "ФИКСИРОВАННОМ наборе промптов, и набор — часть прибора "
            "(PROMPT_SETS['%s'], digest %s)" % (out["protocol"].get("prompts_set"),
                                                out["protocol"].get("prompts_digest")),
        "base_model_id": "Qwen/Qwen2.5-0.5B",
        "pipeline_path": rel,
        "pipeline_version": f"{Path(rel).name}@{sha[:12]}",
        "pipeline_sha256": sha,
        #: Устройство берётся из прогона, а не из константы: строка «RTX 4080» у
        #: прогона на CPU доказывала бы не тот прогон — манифест обязан называть
        #: то устройство, на котором числа сняты (иначе подмена устройства,
        #: вынужденная или случайная, осталась бы незамеченной).
        "image": (f"не контейнер: локальная машина, устройство {out.get('device')}, "
                  "НЕ стенд GB10; torch/transformers — локальное окружение; "
                  f"{out['protocol'].get('decoding')}, "
                  f"max_new_tokens={out['protocol'].get('max_new_tokens')}; "
                  "стенд в этом прогоне не задействован (AD-5), "
                  "сетевой диск — только чтение"),
        #: Сид берётся из протокола прогона, а не из константы: у режима sampling
        #: он — часть протокола, и манифест, показывающий 42 у прогона с другим
        #: сидом, доказывал бы не тот прогон.
        "seed": (out["protocol"].get("decoding_params") or {}).get("seed", SEED),
        "stages": [{"name": "probes", "status": "done"}],
        "pipeline_complete": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "probe_run": _rel(out_path),
        "probe_run_sha256": sha256_file(out_path),
        "probed_states": states,
        "protocol": out["protocol"],
    }
    target = out_path.parent / "run_manifest.json"
    old = {}
    if target.is_file():
        old = json.loads(target.read_text(encoding="utf-8"))
        #: Дописанное другими шагами (стадии, журнал тестов) не теряется: манифест
        #: обновляется прогонами, а не переписывается каждым из них.
        for k in ("stages", "tool_tests_log", "tool_tests_log_sha256", "evidence"):
            if k in old and k not in manifest:
                manifest[k] = old[k]
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    if old.get("stages"):
        manifest["stages"] = old["stages"]
    return manifest


def manifest_only(target: Path) -> int:
    """Собрать манифест прогона проб по **уже снятым** отчётам, без нового замера.

    Нужен потому, что паспорт прогона (AD-2) обязан существовать и у прогонов,
    снятых до того, как прибор научился его писать, — а перемерять их ради
    паспорта значило бы получить другие байты ответов. Все числа манифеста
    берутся из самих отчётов и их хешей; ничего не досчитывается заново.
    """
    #: Канонические отчёты обрабатываются первыми: файлы с префиксом `_` — пилоты
    #: и забракованные прогоны, и по алфавиту они шли бы впереди, забирая себе
    #: «чистые» имена состояний. Имя состояния в манифесте должно указывать на
    #: основной замер, а не на пилот.
    files = ([target] if target.is_file()
             else sorted((x for x in target.glob("*.json")),
                         key=lambda x: (x.name.startswith("_"), x.name)))
    reports = []
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("schema") == "probe-language-split/1":
            reports.append((f, d))
    if not reports:
        note(f"NOT-VERIFIED: отчётов прибора в {target} нет")
        return 2
    #: Устройство переносится из отчётов, а не подставляется: манифест — паспорт
    #: прогона (AD-2), и «устройство None» в нём означало бы, что по паспорту
    #: нельзя узнать, на чём сняты числа (подмена устройства, вынужденная или
    #: случайная, осталась бы незамеченной).
    merged: dict = {"schema": "probe-language-split/1", "protocol": {},
                    "tool_sha256": reports[0][1].get("tool_sha256"),
                    "device": reports[0][1].get("device"),
                    "model_provenance": reports[0][1].get("model_provenance"),
                    "states": {}}
    for f, d in reports:
        if not merged["protocol"]:
            merged["protocol"] = d.get("protocol", {})
        for tag, val in (d.get("states") or {}).items():
            if "probes" not in val:
                continue
            key = tag if tag not in merged["states"] else f"{tag}@{f.stem}"
            val = dict(val)
            val["report"] = _rel(f)
            val["max_new_tokens"] = d.get("protocol", {}).get("max_new_tokens")
            merged["states"][key] = val
    man = write_run_manifest(reports[0][0], merged, None)
    note(f"манифест: {target / 'run_manifest.json'} — состояний {len(man['probed_states'])}")
    for k, v in man["probed_states"].items():
        note(f"  {k}: {v.get('checkpoint_sha256', '')[:12] if v.get('checkpoint_sha256') else '—'} "
             f"({v.get('probes')} проб, бюджет {v.get('max_new_tokens')})")
    return 0


def recut_report(src: Path, dst: Path) -> int:
    """Перечесть снятый отчёт правилом «ход кончается на первом `<|im_end|>`».

    Зачем это законно. Greedy-генерация **префиксно детерминирована**: остановка
    на конце хода не меняет ни одного токена до него — меняется только точка
    обрыва. Значит, по уже снятому отчёту восстанавливается **ровно тот** текст,
    который дал бы прибор с исправленной остановкой на том же бюджете. Это
    позволяет развести две причины обрыва (прибор / лимит) там, где бюджет
    фиксирован, — то есть отделить починку остановки от увеличения бюджета.

    Чего здесь **нет**: длин в токенах. В отчёте хранится текст, а не id префикса;
    пересчитывать токены по тексту значило бы выдать оценку за замер. Поэтому
    длины в перечитанном отчёте помечены как длины **исходного окна**, а обрезание
    измерено тем, что есть, — числом символов до конца хода.

    Коды возврата: 0 — отчёт перечитан; 2 — NOT-VERIFIED (файла нет / не отчёт прибора).
    """
    if not Path(src).is_file():
        note(f"NOT-VERIFIED: отчёта {src} нет")
        return 2
    d = json.loads(Path(src).read_text(encoding="utf-8"))
    if d.get("schema") != "probe-language-split/1":
        note(f"NOT-VERIFIED: {src} — не отчёт прибора (schema={d.get('schema')!r})")
        return 2
    out = dict(d)
    proto = dict(d.get("protocol") or {})
    proto["recut"] = {
        "from": _rel(src),
        "rule": "ход кончается на первом <|im_end|>; хвост отбрасывается",
        "why": "greedy префиксно детерминирован → перечитанный текст равен тексту "
               "прогона с исправленной остановкой на том же бюджете",
        "lengths_note": "длины в токенах — исходного окна: id префикса в отчёте нет, "
                        "оценка по тексту не выдаётся за замер",
        "cut_char_rule": "позиция первого <|im_end|> в символах",
    }
    proto["stop_at_turn_end"] = True
    out["protocol"] = proto
    states = {}
    for tag, val in (d.get("states") or {}).items():
        if "probes" not in val:
            states[tag] = val
            continue
        probes = []
        for p in val["probes"]:
            text = p.get("response") or ""
            cut = first_turn(text)
            ended = cut != text
            q = dict(p)
            q["response"] = cut
            q["cut_at_turn_end"] = ended
            q["cut_char"] = len(cut) if ended else None
            q["chars_original"] = len(text)
            q["n_new_tokens_original"] = p.get("n_new_tokens")
            #: Упор в лимит снимается ровно у тех генераций, где конец хода был
            #: **внутри** окна: исправленный прибор остановился бы там.
            q["hit_limit"] = bool(p.get("hit_limit")) and not ended
            q["stop_reason"] = STOP_TURN_END if ended else (p.get("stop_reason")
                                                            or stop_reason(text, bool(p.get("hit_limit"))))
            q["metrics"] = language_metrics(cut)
            probes.append(q)
        v2 = dict(val)
        v2["probes"] = probes
        v2["aggregate"] = aggregate(probes, proto.get("max_new_tokens"))
        states[tag] = v2
    out["states"] = states
    Path(dst).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for tag, val in states.items():
        if "aggregate" not in val:
            continue
        a = val["aggregate"]
        note(f"  {tag}: дописано ходов {a['stop']['natural_stop_share']}, "
             f"незакрытых <think> {a['unclosed_think_share']} "
             f"(было {d['states'][tag]['aggregate']['unclosed_think_share']})")
    return 0


def reaudit_report(src: Path, dst: Path, label: str | None = None) -> int:
    """Перечесть **прежний** замер арифметикой v2 и показать, что изменилось.

    Зачем отдельный режим. Правка прибора (служебные токены больше не буквы,
    язык считается по ходу модели) меняет числа **задним числом** — и молча
    переписать их было бы подменой: прежние выводы делались по прежним числам.
    Поэтому прежние отчёты перечитываются здесь и кладутся **рядом** с новыми:
    `reading_v1` — что было, `reading_v2` — что стало, `delta` — разница.

    Опора законности: перечитывается **текст генерации**, а не оценка по нему.
    Прежняя арифметика воспроизводится из того же текста и обязана совпасть с
    записанными числами — это проверяется (`legacy_reproduced`) и расхождения
    называются поимённо. Если бы v1 не воспроизводилась, перечитывать было бы
    нечем: числа в отчёте шли бы не из текста, а из неизвестно чего.

    Коды возврата: 0 — перечитано; 2 — NOT-VERIFIED (файла нет / не отчёт прибора).
    """
    if not Path(src).is_file():
        note(f"NOT-VERIFIED: отчёта {src} нет")
        return 2
    d = json.loads(Path(src).read_text(encoding="utf-8"))
    if d.get("schema") != "probe-language-split/1":
        note(f"NOT-VERIFIED: {src} — не отчёт прибора (schema={d.get('schema')!r})")
        return 2
    out: dict = {
        "schema": "probe-language-reaudit/1",
        "stage": "S3ak — правка прибора: служебные токены хода вне речи, язык по ходу модели",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": _rel(src),
        "source_sha256": sha256_file(Path(src)),
        "source_tool_sha256": d.get("tool_sha256"),
        "instrument": INSTRUMENT_VERSION,
        "rules": {
            "v1": "арифметика S3ai/S3aj: из букв исключались только теги блоков "
                  "(<think>, <tool_call>, <tool_response>); служебные токены <|…|> "
                  "считались латиницей",
            "v2": "арифметика S3ak: исключаются все служебные токены <|…|> (правило 4) "
                  "и язык считается по ходу модели — до первого <|im_end|> (правило 5)",
            "why": "у служебных токенов есть латинские буквы (im_end, im_start, "
                   "endoftext), и у короткого русского ответа SFT-состояния они "
                   "составляли заметную долю букв ответа",
        },
        "label": label,
        "states": {},
    }
    for tag, val in (d.get("states") or {}).items():
        if "probes" not in val:
            out["states"][tag] = {"skipped": "нет проб в отчёте"}
            continue
        probes_v2, mismatches, movers = [], [], []
        for i, p in enumerate(val["probes"]):
            raw = p.get("response") or ""
            stored = p.get("metrics") or {}
            lg = legacy_metrics(raw)
            for key in ("cyr_answer", "cyr_think", "cyr_answer_prose"):
                a, b = stored.get(key), lg.get(key)
                if a is None and b is None:
                    continue
                if a is None or b is None or abs(float(a) - float(b)) > 1e-9:
                    mismatches.append({"i": i, "tag": p.get("tag"), "key": key,
                                       "stored": a, "recomputed_v1": b})
            q = dict(p)
            q["metrics"] = language_metrics(raw)
            probes_v2.append(q)
            if (stored.get("cyr_answer") is not None
                    and q["metrics"]["cyr_answer"] is not None):
                movers.append({"i": i, "tag": p.get("tag"),
                               "cyr_answer_v1": stored.get("cyr_answer"),
                               "cyr_answer_v2": q["metrics"]["cyr_answer"],
                               "special_tokens_stripped":
                                   q["metrics"]["special_tokens_stripped"]})
        movers.sort(key=lambda x: abs(x["cyr_answer_v2"] - x["cyr_answer_v1"]),
                    reverse=True)
        agg = aggregate(probes_v2, d.get("protocol", {}).get("max_new_tokens"))
        out["states"][tag] = {
            "n": len(probes_v2),
            "reading_v1": agg["legacy_reading"],
            "reading_v2": {k: agg[k] for k in ("cyr_think", "cyr_answer",
                                               "cyr_answer_prose")},
            "delta": {
                "cyr_answer_with_zeros": _delta(
                    agg["legacy_reading"]["cyr_answer"]["with_zeros"],
                    agg["cyr_answer"]["with_zeros"]),
                "cyr_answer_defined_only": _delta(
                    agg["legacy_reading"]["cyr_answer"]["defined_only"],
                    agg["cyr_answer"]["defined_only"]),
                "cyr_think_with_zeros": _delta(
                    agg["legacy_reading"]["cyr_think"]["with_zeros"],
                    agg["cyr_think"]["with_zeros"]),
            },
            "clean": agg["clean"],
            "legacy_reproduced": not mismatches,
            "legacy_mismatches": mismatches[:10],
            "top_movers": movers[:5],
            "checkpoint": val.get("checkpoint"),
        }
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    for tag, st in out["states"].items():
        if "delta" not in st:
            continue
        note(f"  {tag}: cyr_answer v1 {st['reading_v1']['cyr_answer']['with_zeros']} → "
             f"v2 {st['reading_v2']['cyr_answer']['with_zeros']} "
             f"(Δ {st['delta']['cyr_answer_with_zeros']}), "
             f"воспроизведение v1: {'да' if st['legacy_reproduced'] else 'НЕТ'}")
    return 0


def base_record(prov: dict, probes: list[dict], device: str,
                budget: int | None = None) -> dict:
    """Состояние «база» — та же проба на нетронутых весах Qwen2.5-0.5B.

    Зачем: у критерия ADR-039 три состояния (CPT-финал, SFT-3000, SFT-после), но
    без базы непонятно, **откуда** пришёл русский ответ: если он есть уже у базы,
    то CPT его потерял, а SFT вернул, — и лечение данных тут ни при чём. У базы нет
    чекпойнта (веса берутся из локального кэша), поэтому `checkpoint` пуст, а
    происхождение записано отдельно: «нет чекпойнта» и «чекпойнт не найден» —
    разные вещи, и отчёт обязан их различать.
    """
    return {"checkpoint": None, "checkpoint_sha256": None, "checkpoint_bytes": None,
            "checkpoint_note": "нетронутая база (веса из локального кэша, без чекпойнта)",
            "model_provenance": prov, "device": device,
            "probes": probes, "aggregate": aggregate(probes, budget)}


def run(args) -> int:
    if args.selftest:
        return selftest()
    if args.write_manifest_only:
        return manifest_only(Path(args.write_manifest_only))
    if args.recut_report:
        if not args.out:
            note("отказ: --recut-report требует --out (иначе перечитанному отчёту "
                 "некуда лечь, а исходный перезаписывать запрещено)")
            return 1
        return recut_report(Path(args.recut_report), Path(args.out))
    if args.reaudit_report:
        if not args.out:
            note("отказ: --reaudit-report требует --out (перечитанному отчёту нужно "
                 "куда лечь, а исходный перезаписывать запрещено)")
            return 1
        return reaudit_report(Path(args.reaudit_report), Path(args.out), args.label)
    prompts = PROMPT_SETS.get(args.prompts)
    if prompts is None:
        note(f"NOT-VERIFIED: неизвестный набор промптов {args.prompts!r}; "
             f"есть {sorted(PROMPT_SETS)}")
        return 2
    specs = parse_ckpt_specs(args.ckpt)
    if not specs or not any(p for _, p in specs):
        note("NOT-VERIFIED: нечего мерить — ни одного --ckpt TAG=PATH "
             "(модель не загружалась)")
        return 2

    #: Штатный режим (S3al) разрешается **до** загрузки модели: отказ в прогоне без
    #: запрета повторов — отказ конфигурации, и стоить он должен секунды, а не
    #: минуту загрузки весов и чтения чекпойнта по сети.
    nogram, refusal = resolve_no_repeat_ngram(args.no_repeat_ngram, args.legacy_decoding)
    if refusal:
        note(refusal)
        return 1
    if not nogram:
        note("ВНИМАНИЕ: прогон без запрета повторов n-грамм (--legacy-decoding): "
             "отчёт помечен как непригодный для выводов о языке и формате")

    import ppl_probe as P
    P.repair_broken_pyopenssl()
    P.import_transformers()
    import torch
    if args.device == "cpu":
        import os
        torch.set_num_threads(max(1, (os.cpu_count() or 4) - 2))

    problem = device_problem(torch, args.device)
    if problem:
        note(f"NOT-VERIFIED: устройство {args.device} недоступно: {problem} — "
             "замер не состоялся (это не результат о модели)")
        return 2

    free = device_free_bytes(torch, args.device)
    if free is not None and free < args.min_free_gb * 1024 ** 3:
        note(f"NOT-VERIFIED: на {args.device} свободно {free / 1024 ** 3:.1f} ГБ "
             f"< порога {args.min_free_gb} ГБ — проба конкурировала бы за устройство (AD-5)")
        return 2

    model, tokenizer, prov, errs = P.load_model("base", "float32", args.device,
                                                pipeline_tokenizer=True)
    if model is None:
        note("NOT-VERIFIED: база не загрузилась: " + "; ".join(errs))
        return 2
    note(f"база загружена ({args.device}): {prov}")

    mode = decoding_label(args.decoding, args.temperature, args.top_p,
                          args.repetition_penalty, nogram)
    note(f"режим декодирования: {mode} (штатный: запрет повторов "
         f"{STANDARD_NO_REPEAT_NGRAM}-грамм)")
    gen = {"decoding": args.decoding, "temperature": args.temperature,
           "top_p": args.top_p, "repetition_penalty": args.repetition_penalty,
           "no_repeat_ngram": nogram, "seed": args.seed,
           "batch_size": args.batch_size}
    out: dict = {
        "schema": "probe-language-split/1",
        "stage": ("S3ai (ADR-039: раздельные языки ответа и рассуждения)"
                  + (" + S3aj (снятие артефакта усечения)" if args.stop_at_turn_end else "")
                  + (" + S3ak (прибор v2: служебные токены вне речи; режимы декодирования)"
                     if INSTRUMENT_VERSION >= 2 else "")
                  + " + S3al (штатный режим: запрет повторов 4-грамм по умолчанию)"),
        "tool": "tools/probe_language_split.py",
        #: Хеш самого прибора в отчёте: если файл позже изменится, свод это увидит
        #: и скажет, а не пересчитает числа по новой арифметике молча.
        "tool_sha256": sha256_file(Path(__file__).resolve()),
        "device": args.device,
        "model_provenance": prov,
        "protocol": {
            "prompts_set": args.prompts,
            "prompts_digest": prompts_digest(prompts),
            "prompts": [{"tag": p["tag"], "system": p["system"]} for p in prompts],
            "n_prompts": len(prompts),
            "decoding": mode,
            "decoding_params": {
                "decoding": args.decoding, "temperature": args.temperature,
                "top_p": args.top_p, "repetition_penalty": args.repetition_penalty,
                "no_repeat_ngram": nogram, "seed": args.seed,
            },
            "decoding_note": (
                "S3ak: режим — часть замера, а не его настройка. Сравнение режимов "
                "отвечает на вопрос «петля — свойство greedy или модели»; протокол "
                "S3ab (greedy) при этом не меняется: он остаётся одним из режимов"),
            #: S3al: штатный режим — в отчёте, а не только в тексте плана. Прогон
            #: без запрета повторов разрешён лишь как воспроизведение прежнего
            #: протокола и помечен непригодным для выводов.
            "decoding_protocol": decoding_protocol_block(nogram, args.no_repeat_ngram,
                                                         args.legacy_decoding),
            "batch_size": args.batch_size,
            "batch_note": (
                "1 — путь S3ab/S3ai/S3aj дословно (одна последовательность за раз); "
                ">1 — пробы идут пакетом слева с паддингом и маской внимания: для "
                "greedy это не меняет ни одного токена (тождество байтов проверяется "
                "сводом), для sampling меняет разыгранные числа, поэтому сид ставится "
                "на батч"),
            "instrument": INSTRUMENT_VERSION,
            "instrument_note": (
                "v2 (S3ak): из букв исключаются все служебные токены <|…|> (правило 4), "
                "язык считается по ходу модели — до первого <|im_end|> (правило 5). "
                "v1 (S3ai/S3aj) считается рядом в metrics.legacy и в aggregate."
                "legacy_reading; режим --reaudit-report перечитывает прежние отчёты"),
            "bootstrap": {"n_boot": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED,
                          "method": "bootstrap percentile",
                          "bounds": [BOOTSTRAP_LO, BOOTSTRAP_HI]},
            "max_new_tokens": args.max_new_tokens,
            "max_new_tokens_note": (
                "384 — протокол S3ab (пробы ядра, мост тождества); "
                f"{LANG_MAX_NEW_TOKENS} — бюджет прогонов языка S3ai (объявлен до замера: "
                "на 384 замер 17.09.2026 дал hit_limit 5/5, ответной части нет); "
                f"{FULL_MAX_NEW_TOKENS} — бюджет полного замера S3aj (покрывает p90 длины "
                "трассы SFT 2 914 токенов и почти весь максимум 4 583)"),
            "stop_at_turn_end": bool(args.stop_at_turn_end),
            "stop_at_turn_end_note": (
                "выключено — протокол S3ab воспроизводится побайтово; включено — к "
                "условию остановки добавлен конец хода <|im_end|> (S3aj: без него "
                "генерация переписывает свой же конец хода и меряется её продолжение, "
                "а не ответ)"),
            "stop_token_ids": (turn_end_ids(tokenizer) if args.stop_at_turn_end else None),
            "answer_coverage_floor": ANSWER_COVERAGE_FLOOR,
            "loop_max4gram_rep": LOOP_MAX4GRAM_REP,
            "system_prompt": "копия run_probes пайплайна (тот же, что у probe_control S3ab)",
            "core_prompts_from": "probe_control.PROBE_PROMPTS (импорт, не копия)",
            "degenerate_metrics_from": "probe_control.degenerate_metrics (импорт, не копия)",
            "segments": {
                "think": "внутри <think>…</think>",
                "answer": "вне <think> (включая текст после </think> и до конца хода)",
                "prose": "answer минус блоки <tool_call>/<tool_response> — диагностика",
            },
            "rules": {
                "markers_excluded": "теги не считаются буквами ни в одном сегменте",
                "special_tokens_excluded": "v2: служебные токены <|…|> (в т.ч. <|im_end|>, "
                                           "<|im_start|>, <|endoftext|>) буквами не "
                                           "считаются — у них есть латинские буквы",
                "language_region": "v2: язык меряется по ходу модели (до первого "
                                   "<|im_end|>), а не по её продолжению после ответа",
                "clean_reading": "язык по дописанным ходам без петли — отдельно от "
                                 "полного чтения; зацикленные генерации помечены",
                "unclosed_think": "остаток хода → think; у генерации нет ответной части",
                "no_letters": "доля сегмента без букв = None; в сводке with_zeros=0.0, "
                              "рядом defined_only/coverage",
                "primary": "решения принимаются по with_zeros (консервативно: русского "
                           "ответа в такой генерации нет)",
                "stop_reason": "turn_end — ход дописан моделью; limit_in_<регион> — обрыв "
                               "лимитом токенов, регион называется по внутреннему блоку",
                "percentile": "медиана/p90 — ближайший ранг (nearest-rank), не интерполяция",
            },
        },
        "states": {},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True) if args.out else None

    def flush() -> None:
        if not args.out:
            return
        for tag_, val in out["states"].items():
            if "probes" in val:
                val["aggregate"] = aggregate(val["probes"], args.max_new_tokens)
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                  encoding="utf-8")

    if args.base:
        note("проба: base (нетронутая Qwen2.5-0.5B — опора «откуда пришёл язык»)")
        out["states"]["base"] = base_record(prov, generate_all(
            model, tokenizer, torch, "base", args.device, prompts, args.max_new_tokens,
            stop_at_turn_end=args.stop_at_turn_end, **gen), args.device,
            args.max_new_tokens)
        flush()

    measured = 0
    for tag, path_str in specs:
        if path_str is None:
            out["states"][tag] = {"skipped": "формат аргумента не TAG=PATH"}
            flush()
            continue
        path = Path(path_str).expanduser()
        if not path.is_file():
            out["states"][tag] = {"skipped": f"чекпойнт не найден: {path}"}
            note(f"  {tag}: чекпойнт не найден ({path})")
            flush()
            continue
        note(f"проба: {tag} ({path})")
        info = load_ckpt_into(model, path, torch)
        res = generate_all(model, tokenizer, torch, tag, args.device, prompts,
                           args.max_new_tokens, stop_at_turn_end=args.stop_at_turn_end,
                           **gen)
        out["states"][tag] = {
            "checkpoint": str(path),
            "checkpoint_bytes": path.stat().st_size,
            "checkpoint_sha256": sha256_file(path) if args.sha else None,
            "load": info, "probes": res}
        flush()
        measured += 1

    flush()
    if measured == 0:
        note("NOT-VERIFIED: ни одного состояния не измерено")
        return 2
    if args.out:
        man = write_run_manifest(Path(args.out), out, args)
        note(f"манифест прогона проб: {Path(args.out).parent / 'run_manifest.json'} "
             f"({len(man['probed_states'])} состояний)")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ─────────────────────────── самопроверка на фикстурах ───────────────────────────

FIXTURES = [
    # (имя, текст, ожидания)
    ("ответ без рассуждения",
     "Хлеб на закваске пекут из муки, воды и соли.",
     {"think": "", "answer": "Хлеб на закваске пекут из муки, воды и соли.",
      "unclosed_think": False, "cyr_answer": 1.0, "cyr_think": None,
      "letters_think": 0}),
    ("think без закрытия",
     "Начало.<think> The user asks about bread and I should answer briefly",
     {"think": " The user asks about bread and I should answer briefly",
      "answer": "Начало.", "unclosed_think": True, "cyr_answer": 1.0, "cyr_think": 0.0}),
    ("нормальный ход",
     "<think>Reasoning in English.</think>Ответ по-русски.",
     {"think": "Reasoning in English.", "answer": "Ответ по-русски.",
      "unclosed_think": False, "cyr_answer": 1.0, "cyr_think": 0.0}),
    ("два блока think",
     "<think>First.</think>Середина.<think>Second.</think>Конец.",
     {"think": "First.Second.", "answer": "Середина.Конец.", "unclosed_think": False,
      "n_think_blocks": 2}),
    ("одиночный </think> (баг v7/v8)",
     "</think>Ответ без открывающего тега.",
     {"think": "", "answer": "Ответ без открывающего тега.",
      "stray_think_close": True, "cyr_answer": 1.0}),
    ("tool_call и tool_response в ответной части",
     '<think>Need a tool.</think><tool_call>{"name": "search_concepts"}</tool_call>'
     '<tool_response>\nslug: grpo\nОпределение: алгоритм.\n</tool_response>Итог: grpo.',
     # ответная часть (объявленная метрика): 23 кириллицы против 30 латинских букв
     # в JSON вызова и определении → 23/53; прозаическая часть («Итог: grpo.») — 4/8
     {"think": "Need a tool.", "cyr_answer": 0.434, "cyr_answer_prose": 0.5,
      "n_tool_call_blocks": 1, "has_tool_call": True}),
    ("только разметка — букв нет",
     "<think></think><tool_call></tool_call>",
     {"cyr_answer": None, "cyr_think": None, "letters_overall": 0,
      "has_think": True, "has_tool_call": True}),
]


#: Фикстуры правил v2 (S3ak). Отдельным списком, потому что проверяются не только
#: значения, но и **отношение** v2 к v1: правка прибора обязана иметь ровно тот
#: эффект, ради которого делалась, — а не «числа стали другие».
FIXTURES_V2 = [
    # (имя, текст, ожидание_v2, ожидание_v1). Числа v1 — вручную проверяемая
    # арифметика v1: 13 кириллических букв ответа против латинских букв тега
    # («im_end» — 5 букв: подчёркивание буквой не является), плюс слова чужого хода.
    ("служебный токен в конце короткого ответа",
     "<think>Reasoning.</think>Ответ по-русски.<|im_end|>",
     1.0, round(13 / 18, 4)),
    ("продолжение после конца хода не речь модели",
     "Ответ.<|im_end|><|im_start|>user and here is more latin text",
     1.0, round(5 / 43, 4)),
    ("служебный токен вне названной тройки",
     "<|object_ref_start|>Ответ по-русски.", 1.0, round(13 / 27, 4)),
]


def selftest() -> int:
    bad = 0
    for name, text, exp in FIXTURES:
        got = split_segments(text)
        m = language_metrics(text)
        for key, want in exp.items():
            have = got.get(key, m.get(key))
            if isinstance(want, float) and isinstance(have, float):
                ok = abs(have - want) < 1e-9
            else:
                ok = have == want
            if not ok:
                bad += 1
                note(f"  FAIL {name}: {key} = {have!r}, ожидалось {want!r}")
    #: Правило 4/5 (v2): служебные токены не буквы, язык — по ходу модели. Проверка
    #: идёт **парой** (v2 и v1), потому что ценность правки именно в расхождении:
    #: если v2 совпадёт с v1 на фикстуре со служебным токеном, правило не работает.
    for name, text, want_v2, want_v1 in FIXTURES_V2:
        m = language_metrics(text)
        got_v2 = m.get("cyr_answer")
        got_v1 = m["legacy"].get("cyr_answer")
        if got_v2 is None or abs(float(got_v2) - want_v2) > 1e-3:
            bad += 1
            note(f"  FAIL v2 {name}: cyr_answer = {got_v2!r}, ожидалось {want_v2!r}")
        if got_v1 is None or abs(float(got_v1) - want_v1) > 1e-3:
            bad += 1
            note(f"  FAIL v1 {name}: legacy cyr_answer = {got_v1!r}, ожидалось {want_v1!r}")
        if got_v1 is not None and got_v2 is not None and got_v2 <= got_v1:
            bad += 1
            note(f"  FAIL {name}: правка прибора не подняла долю ответа "
                 f"(v1 {got_v1} → v2 {got_v2}) — правило 4 не сработало")
    if bad:
        note(f"SELFTEST FAIL: {bad} расхождений")
        return 1
    note(f"SELFTEST OK: {len(FIXTURES)} фикстур разбора, {len(FIXTURES_V2)} фикстур v2")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ckpt", action="append", default=[], metavar="TAG=PATH",
                    help="проба чекпойнта по точному пути (повторяемый)")
    ap.add_argument("--prompts", choices=sorted(PROMPT_SETS), default="extended",
                    help="набор промптов: core (5 — тождество с S3ab) или extended (24)")
    ap.add_argument("--max-new-tokens", type=int, default=MAX_NEW_TOKENS,
                    help=f"бюджет генерации; 384 — протокол S3ab, {LANG_MAX_NEW_TOKENS} — "
                         "прогоны языка (см. LANG_MAX_NEW_TOKENS)")
    ap.add_argument("--out", default=None, help="файл отчёта (пишется инкрементально)")
    ap.add_argument("--device", default="cuda", help="cuda | cpu")
    ap.add_argument("--sha", action="store_true",
                    help="считать sha256 чекпойнтов (3 ГБ чтения на состояние)")
    ap.add_argument("--min-free-gb", type=float, default=4.0,
                    help="порог свободной памяти устройства: ниже — NOT-VERIFIED, "
                         "а не конкуренция за GPU (AD-5)")
    ap.add_argument("--base", action="store_true",
                    help="дополнительно снять те же пробы с нетронутой базы "
                         "(состояние base без чекпойнта) — опора «откуда пришёл язык»")
    ap.add_argument("--selftest", action="store_true",
                    help="прогнать фикстуры разбора и выйти (без torch)")
    ap.add_argument("--stop-at-turn-end", action="store_true",
                    help="останавливать генерацию на конце хода <|im_end|> (S3aj): без "
                         "этого прибор мерит продолжение после ответа, а не ответ; "
                         "по умолчанию выключено — протокол S3ab воспроизводится "
                         "побайтово")
    ap.add_argument("--recut-report", default=None, metavar="JSON",
                    help="перечесть снятый отчёт правилом «ход кончается на первом "
                         "<|im_end|>» (нужен --out): разводит обрыв прибором и обрыв "
                         "лимитом на одном и том же бюджете; без нового замера")
    ap.add_argument("--write-manifest-only", default=None, metavar="DIR|JSON",
                    help="собрать манифест прогона (AD-2) по уже снятым отчётам "
                         "прибора, ничего не перемеряя")
    # ── S3ak: режим декодирования как часть замера ────────────────────────────
    ap.add_argument("--decoding", choices=("greedy", "sample"), default="greedy",
                    help="режим декодирования. greedy — протокол S3ab/S3ai/S3aj; "
                         "sample — проверка «петля это свойство greedy или модели»")
    ap.add_argument("--temperature", type=float, default=0.7,
                    help="температура режима sample (по умолчанию 0.7)")
    ap.add_argument("--top-p", type=float, default=0.9, help="top_p режима sample")
    ap.add_argument("--repetition-penalty", type=float, default=None,
                    help="штраф за повторы (например 1.1); по умолчанию выключен")
    ap.add_argument("--no-repeat-ngram", type=int, default=None, metavar="N",
                    help="запрет повторов n-грамм: N=4 — штатный режим стадии (S3al) и "
                         "умолчание прибора; N=0 — прогон без запрета, разрешён только "
                         "вместе с --legacy-decoding (иначе отказ, код 1)")
    ap.add_argument("--legacy-decoding", action="store_true",
                    help="снять штатный запрет повторов n-грамм и разрешить прогон в "
                         "прежнем режиме (S3ab/S3ai/S3aj). Только для воспроизведения "
                         "прежних замеров побайтово: отчёт помечается непригодным для "
                         "выводов о языке и формате (allowed_for_conclusions=false)")
    ap.add_argument("--seed", type=int, default=SEED,
                    help="сид режима sample (на пробу при batch=1, на батч иначе)")
    ap.add_argument("--batch-size", type=int, default=1, metavar="N",
                    help="пробы пакетом по N (1 — путь S3ab дословно). Пакет для greedy "
                         "не меняет токенов, но ускоряет замер в разы")
    # ── S3ak: перечитывание прежних замеров под новой арифметикой ─────────────
    ap.add_argument("--reaudit-report", default=None, metavar="JSON",
                    help="перечесть ПРЕЖНИЙ отчёт арифметикой v2 (служебные токены вне "
                         "речи, язык по ходу модели) и показать дельту к записанным "
                         "числам; нужен --out, исходный отчёт не перезаписывается")
    ap.add_argument("--label", default=None,
                    help="метка перечитанного отчёта (какой замер перечитан)")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
