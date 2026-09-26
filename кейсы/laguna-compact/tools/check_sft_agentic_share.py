#!/usr/bin/env python3
"""C-022 (ADR-059) — страж **сопоставимости** агентной базы и отчёта SFT-стадии.

Дыра, которую правило закрывает (G4 плана стадии). ADR-033 п.2 сделал (в)
третьим компонентом критерия стадии — **agentic-поведение** (доля самостоятельных
вызовов инструмента и pass-rate). Критерий сравнивает два числа «против базы», и
страж обязан отказать на **несопоставимом** замере: это отказ (exit 1), а не «нет
данных».

**Почему сверка с цитатой ADR-033 отменена (ADR-059).** База критерия была
объявлена **замороженной цитатой** ADR-033 п.2 (45.1 % = 158/350, замер hr-68,
`evidence/s3aa-agentic-cpt.json`). Но этот замер снят в **legacy-режиме
декодирования** (в артефакте нет `protocol.decoding`; запрет повторов 4-грамм не
действовал). ADR-041 п.3 объявил базу **переснятию подлежащей**: сравнивать можно
только замеры одного прибора, одних флагов и **одного (штатного) режима**. Прежняя
редакция стража сверяла число переснятой базы с legacy-цитатой и **краснела ровно
на правильном замере** (инцидент 25.09.2026: `evidence/s3aa-agentic-cpt-nogram4.json`
отвергнут как «расхождение с цитатой»). Это класс дефекта «правило отстало от
контура, которое само же и создало».

**Что страж проверяет теперь (ADR-059 п.3) — сопоставимость, а не число:**

* **режим**: `protocol.decoding` штатный у обоих (ADR-041 п.4) — запрет повторов
  `STANDARD_NO_REPEAT_NGRAM`-грамм; legacy-артефакт (нет блока `decoding` либо
  `legacy_decoding = true`) → **отказ считать метрику**;
* **флаги**: `toolcall_force = false` у обоих, подсказки/prefill вызывающего тега
  нет (`--no-hint`) — иначе число описывает харнесс, а не модель;
* **предмет**: `protocol.checkpoint_sha256` базы и отчёта **различаются** (вход и
  выход стадии), и каждый совпадает с распиской брони (`RECEIPT.json`, ADR-058),
  **если** расписка есть (путь расписки — `--receipt` либо `RECEIPT.json` рядом с
  предметом);
* **состав**: `sha256` пулов и `n` совпадают у базы и отчёта — иначе измерены
  разные наборы и сравнение не выносится.

**Расхождение с цитатой ADR-033 — примечание (`warn`), а не отказ** (ADR-059 п.4):
цитата верна для своего замера (legacy-режим) и в вердикт не входит; сама цитата
не переписывается (ADR-028 п.4). Она остаётся носителем факта и используется
только для этого примечания.

**Решающего голоса по (в1)/(в2) у стража нет** (ADR-059 п.5). Страж проверяет
сопоставимость и **печатает условия** — числа (в1) «доля не падает» и (в2)
«pass-rate растёт» с пометкой `decisive = false`. Вердикт по (в) выносит сводка
(`tools/assemble_sft_point_chain.py::agentic_criterion`), читая ту же пару отчётов
и держа правило строгим. Страж, присвоивший себе вердикт по (в), либо наказывал бы
за исполнение ADR-041 (прежний дефект), либо скрывал бы несопоставимость.

Коды возврата::

    0 — PASS: замеры **сопоставимы** (режим штатный у обоих, флаги без
        форсирования, предметы разные/сверены с бронью, состав пулов и n сошлись);
        условия (в1)/(в2) напечатаны как данные для сводки
    1 — FAIL: отказ считать метрику — режим не штатный, смена флагов прибора,
        предметы совпадают либо не доказаны, состав пулов или n разошёлся
    2 — NOT-VERIFIED: нет данных — отчёт отсутствует/нечитаем, нет блока pools,
        не задана переснятая база, нет протокола или хешей предмета

Запуск::

    python3 tools/check_sft_agentic_share.py \\
        --report evidence/s3aa-agentic-sft.json \\
        --base-report evidence/s3aa-agentic-cpt-nogram4.json \\
        --receipt runs/sft-point-chain-20260925-1000/ckpt/RECEIPT.json \\
        --expect-pool-sha256 "v2=e678eb680d5abba0825f81d3e84cda3f680a51850f683fa6f1a3aa22de91d4da,v1=06b95b2f3b62d2f8b24a458ca49ff13232b9fc7a00c5390e4354d6b2956a5eb3"

JSON-контракт (``--json``): ``{"verdict": "PASS|FAIL|NOT-VERIFIED",
"criterion", "report", "base", "checks" (сопоставимость, решающие),
"conditions" ((в1)/(в2), не решающие), "warnings" (примечания, в т.ч. про цитату),
"reason", "notes", "inconclusive", "tolerance_pp"}``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Штатный режим декодирования стадии (ADR-041 п.1): запрет повторов 4-грамм —
#: умолчание прибора (`tools/passrate_probe.py:STANDARD_NO_REPEAT_NGRAM`).
STANDARD_NO_REPEAT_NGRAM = 4

#: Замороженная база ADR-033 п.2 (вставка 17.09.2026 по замеру hr-68): артефакт
#: `evidence/s3aa-agentic-cpt.json`, прогон `runs/passrate-agentic-cpt-20260917-0153/`,
#: прибор `tools/passrate_probe.py --no-toolcall-force --no-hint`. Числа — **цитата
#: решения, а не вычисление**, и сняты они в **legacy-режиме** (в артефакте нет
#: `protocol.decoding`). ADR-059 п.2: цитата становится **исторической** и в вердикт
#: не входит (переписывать её запрещено, ADR-028 п.4). Здесь она живёт ради двух вещей:
#: примечания «база расходится с цитатой — цитата снята в legacy-режиме» и совместимости
#: со сводом S3am (`tools/assemble_s3am_evidence.py` импортирует этот блок).
BASE_FROZEN = {
    "source": "ADR-033 п.2 (замер hr-68), evidence/s3aa-agentic-cpt.json",
    "pools": {
        "v2": {"n": 200, "n_tool_call": 72, "share": 0.36, "n_pass": 11, "pass_rate": 0.055},
        "v1": {"n": 150, "n_tool_call": 86, "share": 0.5733, "n_pass": 42, "pass_rate": 0.28},
    },
    "combined": {"n": 350, "n_tool_call": 158, "share": 0.4514,
                 "n_pass": 53, "pass_rate": 0.1514},
}

#: Конфигурация прибора, в которой вызов инструмента самостоятелен (``--no-hint``).
#: Всё остальное — отказ: число описывает харнесс, а не модель.
#:
#: Две формы одной и той же строки: операционно прибор подставляет реальный
#: перевод строки (``passrate_probe.py:669``), а в отчёт пишет **экранированную**
#: запись ``"<think>\\n"`` (``passrate_probe.py:1769``) — 9 символов, где последние
#: два это обратный слэш и ``n``. Проверены обе: сравнивать с одной и отвергать
#: вторую значило бы отказывать на верном отчёте из-за формы записи.
REQUIRED_PREFILL = ("<think>\\n", "<think>\n")

#: Допуск примечания «переснятая база разошлась с цитатой ADR-033». Число берётся
#: у стража сводом S3am (`assemble_s3am_evidence.LEGACY_TOL`), а не назначается там.
BASE_REDERIVE_TOL = 0.01

#: Сколько знаков печатать в долях.
ND = 4

#: Имя расписки брони предмета (ADR-058) — рядом с копией/хардлинком чекпойнта.
CKPT_RECEIPT_NAME = "RECEIPT.json"

#: Полные sha256 в расписке брони. Расписка хранит хеш и в поле `sha256`, и в списке
#: `aux_subjects` (входные состояния приборов), поэтому берём ВСЕ 64-hex значения —
#: так одна расписка покрывает оба предмета пары (SFT-финал и входной CPT).
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def n_from_share(n: int, share: float) -> int:
    """Счёт вызовов из доли отчёта.

    Прибор пишет долю, округлённую до 4 знаков, счёта не пишет; обратный переход
    даёт ±0.5 задачи. Ошибка меньше шага решения (шаг доли базы при n=350 —
    0.29 %), но она названа, а не спрятана.
    """
    return int(round(n * share))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class NotVerified(Exception):
    """Вход непригоден для суждения (нет данных)."""


class Refuse(Exception):
    """Вход пригоден, но несопоставим (отказ, не «нет данных»)."""


def standard_decoding(proto: dict | None) -> tuple:
    """Штатный ли режим декодирования у отчёта (ADR-041 п.4).

    Возвращает ``(штатный, имя_режима)``. Штатный — запрет повторов 4-грамм:
    поле ``protocol.decoding.standard`` (или ``no_repeat_ngram == 4`` в старых
    записях). Отсутствие блока ``decoding`` — тот самый legacy-артефакт, ради
    которого правило введено, поэтому «нет блока» это отказ, а не «неизвестно».
    """
    dec = (proto or {}).get("decoding")
    if not isinstance(dec, dict):
        return False, None
    if dec.get("legacy_decoding"):
        return False, dec.get("mode")
    standard = dec.get("standard")
    if standard is None:
        standard = dec.get("no_repeat_ngram") == STANDARD_NO_REPEAT_NGRAM
    return bool(standard), dec.get("mode")


def check_protocol(proto: dict | None, label: str, where: str) -> dict:
    """Конфигурация прибора: от неё зависит смысл числа.

    Возвращает ключ сравнимости. Отказ (``Refuse``) — на смену флагов и на
    нештатный режим декодирования: без этого числа базы и отчёта описывают
    разные вещи (харнесс против модели, legacy-декодер против штатного).
    """
    if not proto:
        raise Refuse(f"{label}: в отчёте нет блока 'protocol' ({where}) — конфигурацию "
                     f"прибора (режим, toolcall_force, prefill) проверить нечем, а от неё "
                     f"зависит смысл числа (ADR-041 п.4)")
    if proto.get("toolcall_force"):
        raise Refuse(f"{label}: отчёт снят с toolcall_force=true ({where}) — первый вызов "
                     f"вкладывает харнесс, самостоятельность вызова не измерена")
    prefill = proto.get("prefill")
    if prefill not in REQUIRED_PREFILL:
        raise Refuse(f"{label}: prefill={prefill!r} вместо {REQUIRED_PREFILL[0]!r} ({where}) — "
                     f"подсказка открывающего тега (--hint) делает вызов "
                     f"полуфорсированным, число не описывает модель")
    standard, mode = standard_decoding(proto)
    if not standard:
        raise Refuse(f"{label}: режим не штатный ({where}): protocol.decoding"
                     f"{'' if mode else ' отсутствует'} — запрет повторов "
                     f"{STANDARD_NO_REPEAT_NGRAM}-грамм не действовал (ADR-041 п.4), "
                     f"метрику по этому отчёту считать нельзя")
    return {"toolcall_force": bool(proto.get("toolcall_force")), "prefill": prefill,
            "decoding_mode": mode, "checkpoint_sha256": proto.get("checkpoint_sha256"),
            "checkpoint_path": proto.get("checkpoint_path") or proto.get("weights")}


def units_from_report(rep: dict, label: str, expect_sha: dict) -> tuple:
    """Сравнимые единицы отчёта прибора: по пулам + сводная.

    Единица — ``{"name", "n", "n_tool_call", "share", "n_pass", "pass_rate",
    "pool_sha256"}``.
    """
    pools = rep.get("pools")
    if not isinstance(pools, dict) or not pools:
        raise NotVerified(f"{label}: в отчёте нет блока 'pools' — "
                          f"доли вызовов и pass-rate взять не из чего")

    # Смысл числа зависит от конфигурации прибора, поэтому она проверяется первой —
    # и не только сводная: пересборка из сырых записей (`--summarize`) кладёт
    # протокол в каждый пул, и расхождение между пулами означало бы, что подвыборки
    # сняты по-разному (или на разных весах), а сводка по ним — среднее из разного.
    key = check_protocol(rep.get("protocol"), label, "на уровне отчёта")

    units: dict[str, dict] = {}
    notes: list[str] = []
    for name in sorted(pools):
        p = pools[name] or {}
        ov = p.get("overall") or {}
        if not ov.get("n"):
            notes.append(f"{label}: пул '{name}' без блока overall/n — пропущен")
            continue
        sha = p.get("sha256")
        if name in expect_sha and sha != expect_sha[name]:
            raise Refuse(f"{label}: пул '{name}' имеет sha256 {sha}, ожидался "
                         f"{expect_sha[name]} — измерен другой пул, числа несопоставимы")
        if isinstance(p.get("protocol"), dict):
            pkey = check_protocol(p["protocol"], label, f"пул '{name}'")
            if pkey["decoding_mode"] != key["decoding_mode"]:
                raise Refuse(f"{label}: пул '{name}' снят в другом режиме декодирования "
                             f"({pkey['decoding_mode']!r} против {key['decoding_mode']!r}) — "
                             f"это разные измерения, а не подвыборки одного")
            if (pkey["toolcall_force"], pkey["prefill"]) != (key["toolcall_force"], key["prefill"]):
                raise Refuse(f"{label}: пул '{name}' снят в другой конфигурации прибора "
                             f"(toolcall_force={pkey['toolcall_force']}, prefill={pkey['prefill']!r}) "
                             f"против остального отчёта (toolcall_force={key['toolcall_force']}, "
                             f"prefill={key['prefill']!r}) — это разные измерения, а не "
                             f"подвыборки одного")
            if (pkey["checkpoint_sha256"] and key["checkpoint_sha256"]
                    and pkey["checkpoint_sha256"] != key["checkpoint_sha256"]):
                raise Refuse(f"{label}: пул '{name}' снят на чекпойнте {pkey['checkpoint_sha256']}, "
                             f"а отчёт — на {key['checkpoint_sha256']} — сравнение моделей "
                             f"вместо сравнения подвыборок (ошибка постановки, ADR-006/S3j-2)")
        share = ov.get("tool_call_share")
        if share is None:
            notes.append(f"{label}: пул '{name}' без tool_call_share — пропущен")
            continue
        units[name] = {
            "name": name, "n": ov["n"], "n_tool_call": n_from_share(ov["n"], share),
            "share": share, "n_pass": ov.get("n_pass"),
            "pass_rate": ov.get("pass_rate"), "pool_sha256": sha,
        }

    if not units:
        raise NotVerified(f"{label}: ни в одном пуле нет overall с n и tool_call_share")

    #: Сводная единица — арифметика по измеренным пулам. Она сопоставима только при
    #: ТОМ ЖЕ составе подвыборок: у другого набора задач своя доля.
    n = sum(u["n"] for u in units.values())
    k = sum(u["n_tool_call"] for u in units.values())
    npass = sum(u["n_pass"] or 0 for u in units.values())
    sample = sorted(units)
    units["combined"] = {
        "name": "combined", "n": n, "n_tool_call": k, "sample": sample,
        "share": round(k / n, ND) if n else None,
        "n_pass": npass,
        "pass_rate": round(npass / n, ND) if n else None,
        "pool_sha256": None,
    }
    meta = {"pools": sample, "n": n, "protocol": key,
            "note": "счёт вызовов выведен из доли отчёта (round(n*share), ±0.5 задачи)"}
    return units, meta, notes


def base_units(base_report: str | None) -> tuple:
    """База критерия — **переснятая** в штатном режиме (ADR-059 п.1).

    Замороженная цитата ADR-033 базой больше не является (п.2): она снята в
    legacy-режиме и в вердикт не входит. Поэтому без ``--base-report`` судить
    нечем — это NOT-VERIFIED «нет данных», а не сверка с цитатой.
    """
    if not base_report:
        raise NotVerified(
            "база не задана: замороженная цитата ADR-033 — историческое число "
            "legacy-замера и в вердикт не входит (ADR-059 п.2); задайте "
            "--base-report с базой, переснятой в штатном режиме")

    p = Path(base_report)
    if not p.is_file():
        raise NotVerified(f"базовый отчёт не найден: {p}")
    units, meta, notes = units_from_report(read_json(p), str(p), {})
    meta["path"] = str(p)
    meta["source"] = f"переснятая база (штатный режим): {p}"
    return units, meta, notes


def _hex64_values(obj) -> list:
    """Все полные sha256 в структуре расписки брони (поля + aux_subjects)."""
    out: list[str] = []
    if isinstance(obj, str):
        out += _HEX64.findall(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            out += _hex64_values(value)
    elif isinstance(obj, list):
        for value in obj:
            out += _hex64_values(value)
    return out


def receipt_shas(proto: dict, explicit: str | None) -> tuple:
    """`(sha256 из расписки, путь расписки)` — или пусто, если расписки нет.

    Расписка брони (ADR-058) лежит рядом с предметом (`ckpt/RECEIPT.json`) либо
    задаётся явно (`--receipt`). Отсутствие расписки — не отказ: правило п.3
    требует сверки «при наличии расписки».
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    path = proto.get("checkpoint_path")
    if path:
        d = Path(str(path)).parent
        candidates += [d / CKPT_RECEIPT_NAME, d.parent / CKPT_RECEIPT_NAME]
    for cand in candidates:
        if not cand.is_file():
            continue
        try:
            data = read_json(cand)
        except (OSError, ValueError) as exc:
            note(f"расписка брони {cand} нечитаема ({type(exc).__name__}: {exc}) — "
                 f"сверка предмета с бронью пропущена")
            continue
        return sorted(set(_hex64_values(data))), str(cand)
    return [], None


def comparability(after_meta: dict, base_meta: dict,
                  after_units: dict, base_units_: dict,
                  explicit_receipt: str | None) -> tuple:
    """Проверка сопоставимости базы и отчёта (ADR-059 п.3).

    Возвращает ``(checks, notes, fail)``: список проверок (решающих), примечания и
    причину отказа (``None``, если сопоставимы). Отсутствие данных — ``NotVerified``.
    """
    checks: list[dict] = []
    notes: list[str] = []
    fail: str | None = None

    def add(rule: str, ok: bool, reading: str) -> None:
        nonlocal fail
        checks.append({"rule": rule, "passed": bool(ok), "decisive": True, "reading": reading})
        if not ok and fail is None:
            fail = f"{rule}: {reading}"

    # 1. режим обоих штатный (иначе check_protocol уже отказал бы) — печатаем как факт.
    for label, meta in (("отчёт", after_meta), ("база", base_meta)):
        add(f"режим {label} — штатный (ADR-041 п.4)", True,
            f"{meta['protocol']['decoding_mode']} (запрет повторов "
            f"{STANDARD_NO_REPEAT_NGRAM}-грамм)")

    # 2. флаги обоих без форсирования — тоже печатаем фактом.
    for label, meta in (("отчёт", after_meta), ("база", base_meta)):
        add(f"флаги {label} — без форсирования", True,
            f"toolcall_force={meta['protocol']['toolcall_force']}, "
            f"prefill={meta['protocol']['prefill']!r} (подсказки вызывающего тега нет)")

    # 3. предмет: чекпойнты базы и отчёта — вход и выход стадии, значит РАЗНЫЕ.
    a_ck = after_meta["protocol"]["checkpoint_sha256"]
    b_ck = base_meta["protocol"]["checkpoint_sha256"]
    if not a_ck or not b_ck:
        raise NotVerified(
            "предмет замера не доказан: нет protocol.checkpoint_sha256 "
            f"({'отчёта' if not a_ck else 'базы'}) — чекпойнты не сверить")
    add("предмет: чекпойнт отчёта и базы различаются (вход и выход стадии)",
        a_ck != b_ck,
        f"отчёт {a_ck[:12]}… против базы {b_ck[:12]}…")
    if a_ck == b_ck:
        raise Refuse(f"предметы совпадают: отчёт и база сняты на одном чекпойнте "
                     f"({a_ck[:12]}…), а стадия — переход между входом и выходом; "
                     f"сравнивать нечего")

    # Расписка брони (ADR-058) — при наличии: оба предмета обязаны ей соответствовать.
    for label, ck, proto in (("отчёт", a_ck, after_meta.get("protocol") or {}),
                             ("база", b_ck, base_meta.get("protocol") or {})):
        shas, rec_path = receipt_shas(proto, explicit_receipt)
        if rec_path:
            add(f"предмет {label}: чекпойнт совпал с распиской брони (ADR-058)",
                ck in shas, f"{ck[:12]}… в {rec_path}")
        else:
            notes.append(f"предмет {label}: расписка брони ({CKPT_RECEIPT_NAME}) не найдена "
                         f"— сверка с бронью не выполнялась (ADR-058: при наличии)")

    # 4. состав: пулы и n обязаны совпасть — иначе измерены разные наборы.
    a_pools = sorted(p for p in after_units if p != "combined")
    b_pools = sorted(p for p in base_units_ if p != "combined")
    add("состав: пулы базы и отчёта совпадают", a_pools == b_pools,
        f"отчёт {', '.join(a_pools)} против базы {', '.join(b_pools)}")
    if a_pools != b_pools:
        raise Refuse(f"состав пулов разошёлся (отчёт: {', '.join(a_pools)}; "
                     f"база: {', '.join(b_pools)}) — измерены разные наборы, "
                     f"сравнение не выносится")
    for name in a_pools:
        au, bu = after_units[name], base_units_[name]
        add(f"состав: n пула '{name}' совпадает", au["n"] == bu["n"],
            f"{au['n']} против {bu['n']}")
        add(f"состав: sha256 пула '{name}' совпадает",
            au["pool_sha256"] is not None and au["pool_sha256"] == bu["pool_sha256"],
            f"{au['pool_sha256']} против {bu['pool_sha256']}")
        if au["n"] != bu["n"]:
            raise Refuse(f"состав: n пула '{name}' разошёлся ({au['n']} против "
                         f"{bu['n']}) — подвыборки не те, сравнение не выносится")
        if au["pool_sha256"] is None or au["pool_sha256"] != bu["pool_sha256"]:
            raise Refuse(f"состав: sha256 пула '{name}' разошёлся "
                         f"({au['pool_sha256']} против {bu['pool_sha256']}) — измерены "
                         f"разные наборы, сравнение не выносится")
    return checks, notes, fail


def citation_warnings(base_units_: dict) -> list:
    """Примечание «база разошлась с цитатой ADR-033» (ADR-059 п.4).

    Цитата снята в legacy-режиме, поэтому переснятая в штатном режиме база обязана
    от неё отличаться — это **не отказ**, а объяснение режимом. Цитату не правим
    (ADR-028 п.4), но и в вердикт не берём.
    """
    frozen = {**BASE_FROZEN["pools"], "combined": BASE_FROZEN["combined"]}
    warns = []
    for name, fz in frozen.items():
        u = base_units_.get(name)
        if not u:
            continue
        if u["n"] != fz["n"] or abs(u["share"] - fz["share"]) > BASE_REDERIVE_TOL:
            warns.append(
                f"цитата ADR-033 [{name}]: база даёт {u['n_tool_call']}/{u['n']} = "
                f"{u['share']} против {fz['n_tool_call']}/{fz['n']} = {fz['share']} — "
                f"расхождение объясняется режимом: цитата снята в legacy-режиме "
                f"декодирования и в вердикт не входит (ADR-059 п.2/п.4)")
    return warns


def conditions(after: dict, base: dict, *, tolerance_pp: float) -> list:
    """Условия (в1) и (в2) как ДАННЫЕ для сводки — не вердикт стража (ADR-059 п.5).

    Каждое условие помечено ``decisive = false``: решающий голос по (в) у сводки
    (`assemble_sft_point_chain.agentic_criterion`), а страж только печатает числа.
    """
    tol = tolerance_pp / 100.0
    out = []
    d_share = after["share"] - base["share"]
    out.append({
        "rule": "в1: доля самостоятельных вызовов не падает",
        "unit": after["name"], "value": after["share"],
        "threshold": round(base["share"] - tol, ND), "delta": round(d_share, ND),
        "passed": bool(after["share"] >= base["share"] - tol), "decisive": False,
        "reading": (f"{after['n_tool_call']}/{after['n']} = {after['share']:.4f} "
                    f"против базы {base['n_tool_call']}/{base['n']} = {base['share']:.4f} "
                    f"(Δ {d_share:+.4f}"
                    + (f", допуск {tolerance_pp:g} п.п." if tolerance_pp else "") + ")"),
    })
    if after["pass_rate"] is not None and base.get("pass_rate") is not None:
        dp = after["pass_rate"] - base["pass_rate"]
        out.append({
            "rule": "в2: pass-rate растёт", "unit": after["name"],
            "value": after["pass_rate"], "threshold": base["pass_rate"],
            "delta": round(dp, ND),
            "passed": bool(after["pass_rate"] > base["pass_rate"]), "decisive": False,
            "reading": (f"{after['n_pass']}/{after['n']} = {after['pass_rate']:.4f} "
                        f"против базы {base['n_pass']}/{base['n']} = {base['pass_rate']:.4f} "
                        f"(Δ {dp:+.4f})"),
        })
    else:
        out.append({
            "rule": "в2: pass-rate растёт", "unit": after["name"],
            "value": after["pass_rate"], "threshold": base.get("pass_rate"),
            "delta": None, "passed": False, "decisive": False,
            "reading": "pass-rate отсутствует в отчёте или в базе — рост не доказан",
        })
    return out


def evaluate(args) -> tuple:
    rep_path = Path(args.report)
    if not rep_path.is_file():
        return {"verdict": "NOT-VERIFIED",
                "reason": f"нет отчёта прогона: {rep_path}"}, EXIT_NOT_VERIFIED

    expect_sha = {}
    for item in (args.expect_pool_sha256 or "").split(","):
        if item.strip():
            name, _, sha = item.partition("=")
            expect_sha[name.strip()] = sha.strip()

    try:
        after_units, after_meta, notes = units_from_report(
            read_json(rep_path), str(rep_path), expect_sha)
        base, base_meta, base_notes = base_units(args.base_report)
        notes += base_notes
        checks, comp_notes, fail = comparability(
            after_meta, base_meta, after_units, base, args.receipt)
        notes += comp_notes
        warns = citation_warnings(base)
    except NotVerified as exc:
        return {"verdict": "NOT-VERIFIED", "reason": str(exc)}, EXIT_NOT_VERIFIED
    except Refuse as exc:
        return {"verdict": "FAIL", "reason": str(exc)}, EXIT_FAIL

    out = {
        "verdict": "PASS" if fail is None else "FAIL",
        "criterion": ("ADR-059 п.3: сопоставимость базы и отчёта — штатный режим обоих "
                      "(ADR-041 п.4), флаги без форсирования, разные предметы (вход и выход "
                      "стадии) со сверкой с бронью, совпадающие состав пулов и n; вердикт "
                      "по (в1)/(в2) — у сводки (ADR-059 п.5)"),
        "report": {"path": str(rep_path), "pools": after_meta["pools"],
                   "n": after_meta["n"],
                   "combined_share": after_units["combined"]["share"],
                   "decoding": after_meta["protocol"]["decoding_mode"],
                   "checkpoint_sha256": after_meta["protocol"]["checkpoint_sha256"]},
        "base": {**base_meta, "path": base_meta.get("path")},
        "checks": checks,
        "warnings": warns,
        "reason": (fail if fail else
                   f"замеры сопоставимы ({len(checks)} условий); условия (в1)/(в2) "
                   f"напечатаны, вердикт по (в) выносит сводка"),
        "inconclusive": [],
        "tolerance_pp": args.tolerance_pp,
    }
    if fail is None:
        conds: list[dict] = []
        for name in after_meta["pools"]:
            conds += conditions(after_units[name], base[name], tolerance_pp=args.tolerance_pp)
        conds += conditions(after_units["combined"], base["combined"],
                            tolerance_pp=args.tolerance_pp)
        out["conditions"] = conds
    if notes:
        out["notes"] = notes
    return out, (EXIT_OK if fail is None else EXIT_FAIL)


def report_text(res: dict) -> str:
    lines = [f"{res['verdict']}: сопоставимость агентной базы и отчёта "
             f"(ADR-059 п.3)"]
    if res.get("report"):
        r = res["report"]
        lines.append(f"  отчёт: {r['path']} | пулы: {', '.join(r['pools'])} | "
                     f"задач: {r['n']} | сводная доля вызовов: {r['combined_share']} | "
                     f"режим: {r['decoding']} | чекпойнт: {str(r['checkpoint_sha256'])[:12]}…")
    if res.get("base"):
        b = res["base"]
        b_proto = b.get("protocol") or {}
        lines.append(f"  база: {b.get('source', b.get('path'))} | "
                     f"задач: {b.get('n')} | режим: {b_proto.get('decoding_mode')} | "
                     f"чекпойнт: {str(b_proto.get('checkpoint_sha256'))[:12]}…")
    for c in res.get("checks", []):
        lines.append(f"  [{'ok' if c['passed'] else 'FAIL'}] {c['rule']}: {c['reading']}")
    for w in res.get("warnings", []):
        lines.append(f"  [warn] {w}")
    for c in res.get("conditions", []):
        lines.append(f"  [условие, решает сводка] {'ok' if c['passed'] else 'FAIL'} "
                     f"{c['unit']} | {c['rule']}: {c['reading']}")
    for inc in res.get("inconclusive", []):
        lines.append(f"  [--] {inc}")
    lines.append(f"  причина: {res.get('reason', '')}")
    for n in res.get("notes", []):
        lines.append(f"  примечание: {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", required=True,
                    help="отчёт прогона: evidence-JSON прибора tools/passrate_probe.py, "
                         "снятый БЕЗ --toolcall-force и БЕЗ --hint, в штатном режиме "
                         "декодирования (ADR-041 п.4)")
    ap.add_argument("--base-report", default=None,
                    help="артефакт базы, ПЕРЕСНЯТОЙ в штатном режиме на входном чекпойнте "
                         "стадии (ADR-059 п.1). Замороженная цитата ADR-033 базой не "
                         "является; без этого флага — NOT-VERIFIED")
    ap.add_argument("--receipt", default=None,
                    help=f"расписка брони предмета ({CKPT_RECEIPT_NAME}, ADR-058). Если не "
                         f"задана, расписка ищется рядом с чекпойнтом (ckpt/{CKPT_RECEIPT_NAME})")
    ap.add_argument("--tolerance-pp", type=float, default=0.0,
                    help="допуск на падение доли (в1), п.п. — только для ПЕЧАТИ условий; "
                         "вердикт стража — сопоставимость, а вердикт по (в) выносит сводка")
    ap.add_argument("--expect-pool-sha256", default="",
                    help='ожидаемые хеши пулов, "v2=<sha>,v1=<sha>" — защита от подмены '
                         'набора между базовым замером и замером после стадии')
    ap.add_argument("--json", default=None, help="куда записать машинный результат")
    args = ap.parse_args(argv)

    res, code = evaluate(args)
    print(report_text(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
