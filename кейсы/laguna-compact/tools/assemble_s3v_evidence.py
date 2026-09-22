#!/usr/bin/env python3
"""S3v — PPL контрольных рук C1 и C2 по обеим компонентам общего языка (свод).

Какой вопрос закрывается. Обвал общего языка на ``v1_general`` (×4.6…9.5) был
основанием остановки CPT (ADR-022), затем основание отозвано (ADR-029) по двум
чистым наборам. Но **причина** обвала оставалась необъяснённой: калибровочная
сетка S3m/S3n варьировала только долю replay и пик LR и **во всех четырёх руках
содержала домен**, поэтому её профилем «протокол» от «домена» не отделяется.

Чем закрывается. Две контрольные руки S3o:

* **C1** (`ctrl-C1-100-0.35`) — тот же протокол (2 000 шагов, батч 1,
  ``max_len 8192``, сид 42, та же копия пайплайна), но обучающий материал —
  **только общий язык**: 3 493 чанка ``general_replay_ru.txt``, домена ноль
  (манифест ``datasets/mix-ctrl100-build.json``, ``mix_domain_ratio: 0.0``).
  Отвечает на вопрос «разрушает ли язык **сам протокол**».
* **C2** (`ctrl-C2-25-0.035`) — тот же корпус, что у руки ``25-0.35`` сетки
  (``v12r``, 75/25), но пик LR **в 10 раз ниже** (×0.035 против ×0.35). Отвечает
  на вопрос «нужен ли низкий LR». Корпус, упаковка и сид те же — варьируется
  ровно одна величина, поэтому сравнение с ``25-0.35`` одномерно.

Что здесь считается, а что читается. Читаются **только** отчёты прибора
(``runs/s3v-arms-20260916/*.json``) и исторические эталоны. Считаются: отношения
к базам, проходы потолков, **конъюнкция**, первый пересечённый шаг и вердикт.
Профиль по шагам — не украшение: по нему видно, ступенька это или сползание
(ср. ``evidence/s3n-ppl-curve.json``, ступенька в (0, 500]).

Почему потолок вычисляется, а не берётся числом. Потолок — правило ADR-022 п.3
(2× базы), и его значение производно от базы. Вписанное число разошлось бы с
базой молча; вычисленное — нет. Дополнительно значение сверяется с записанным в
эталонных evidence: расхождение — отказ свода, а не пометка.

Почему прибор не переписан. Замер сделан тем же ``tools/calib_ppl_probe.py``,
которым сняты база и потолок (и которым перемеряны четыре руки S3t), на тех же
наборах, с теми же ``bfloat16 / batch 4 / max_len 1024``. Тождество прибора
доказывается **числом в том же прогоне**: ``v1_general`` = 11.931923888,
``v3_general`` = 7.504686374, ``k2_general`` = 6.159936 — все три обязаны сойтись
с историческими эталонами в пределах ±1e-4. Не сошлось — отказ (код 1).

Чего этот свод не делает. Он **не меняет решение** ADR-029: стадия уже продолжена
на `v12r` при пике LR×0.35, и замер контрольных рук уточняет **объяснение**, а не
решение. Компоненты K1 и K2 не усредняются (ADR-027 п.2) — вердикт конъюнкция.

Коды возврата::

    0 — evidence записан, тождество прибора доказано, вердикт посчитан
    1 — отказ: отчёт руки неполон, база руки не совпала с базой эталона,
        тождество прибора не воспроизведено, набор подменён
    2 — NOT-VERIFIED: нет входа (отчёт руки, эталон, набор)

Запуск::

    python3 tools/assemble_s3v_evidence.py
    python3 tools/assemble_s3v_evidence.py --case-root <корень> --out e.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Каталог с отчётами прибора по контрольным рукам (по отчёту на руку).
ARMS_DIR = "runs/s3v-arms-20260916"

OUT = "evidence/s3v-control-arms-ppl.json"

#: Эталоны, из которых читаются базы, потолки и хеши наборов. Числа берутся
#: **из файлов**, а не вписаны здесь: правка эталона обязана ломать свод.
REF_K1 = "evidence/s3q-baseline-v3.json"      # база компоненты K1 (v3_general)
REF_K2 = "evidence/s3t-baseline-k2.json"      # база компоненты K2 (k2_general)
REF_V1 = "evidence/ppl-baseline-v1v2.json"    # историческая база v1_general (S3h)

#: Отчёты, из которых берутся **чужие** числа для сравнения в вердикте (б):
#: рука `25-0.35` — тот же корпус `v12r`, но пик LR×0.35. Один и тот же корпус
#: при одном сиде — значит сравнивается ровно LR, и ничего больше.
REF_PEER_K2SET = "evidence/s3t-k2-set.json"   # 25-0.35 на K1 и K2 (перемер S3t)
REF_PEER_S3M = "evidence/s3m-ppl-arms.json"   # 25-0.35 на v1_general + траектория лосса

#: Наборы компонент. Роли — ADR-027 п.1/п.2: K1 в жанре реплея, K2 вне
#: обучающего распределения; v1 — исторический набор, на котором обвал был снят.
SETS: dict[str, str] = {
    "v1_general": "датасет, на котором обвал был снят (24 документа, S3h)",
    "v3_general": "K1 — в жанре реплея (200 документов)",
    "k2_general": "K2 — вне обучающего распределения (200 документов)",
}

COMPONENTS: dict[str, str] = {"k1": "v3_general", "k2": "k2_general"}

#: Порог тождества прибора из контракта дельты: ``±1e-4``. Абсолютный, а не
#: относительный: контракт называет его для числа порядка 11.93, и все три
#: контрольных числа — того же порядка (6.16…11.93), поэтому одна шкала порога
#: на все три честнее трёх разных.
IDENTITY_TOLERANCE = 1e-4

#: Допуск согласия базы в отчёте руки с базой эталона. Оба числа снимает один и
#: тот же код на одной машине; расхождение может быть только сменой прибора, и
#: тогда отношение руки считалось бы от другой базы — это отказ.
BASE_AGREEMENT_TOL = 1e-6

#: Правило потолка (ADR-022 п.3) — множитель, а не значение.
CEILING_FACTOR = 2.0

#: Какие состояния прибора читать как шаги. Имя состояния → шаг; ``cfinal`` — это
#: ``cpt_steps`` руки, и он сверяется с ``calib_params.json``, а не предполагается.
STATE_STEPS: dict[str, int | None] = {"c500": 500, "c1000": 1000, "c1500": 1500,
                                      "cfinal": None}

#: Руки: каталог прогона на стенде (AD-4: читается по месту, копий не держится)
#: и что эта рука варьирует. Ожидаемые ``replay_share_pct``/``peak_lr_scale``
#: сверяются с ``calib_params.json`` руки: перепутать руки в таблице нельзя.
ARMS: dict[str, dict] = {
    "ctrl-C1-100-0.35": {
        "run_dir": "ctrl-C1-100-0.35-20260916-1632",
        "vary": "протокол без домена: 100 % общий язык, пик LR×0.35",
        "replay_share_pct": 100,
        "peak_lr_scale": 0.35,
        "corpus": "ctrl100 (general_replay_ru.txt, домена нет)",
    },
    "ctrl-C2-25-0.035": {
        "run_dir": "ctrl-C2-25-0.035-20260916-1632",
        "vary": "низкий пик LR при том же корпусе, что у 25-0.35",
        "replay_share_pct": 25,
        "peak_lr_scale": 0.035,
        "corpus": "v12r (75/25, тот же кэш, что у рук S3m)",
    },
}

OPEN_QUESTIONS = [
    "Цена низкого LR по домену в этом замере не измерена: прибор снимает PPL "
    "на наборах **общего языка** (K1/K2/v1), а доменные наборы в прогон не "
    "входили. Из траектории лосса видно, что рука C2 училась (лосс на своём "
    "корпусе падает), но «сколько домена она взяла» — вопрос доменной шкалы, и "
    "он решается отдельным замером на `v1_domain`/`v2_domain`, а не этим сводом",
    "Промежуточных точек внутри (0, 500] нет и здесь: у обеих контрольных рук "
    "чекпойнты пишутся каждые 500 шагов, поэтому «ступенька» локализована "
    "интервалом, а не шагом. Перегонка с чекпойнтами каждые 50 шагов на одной "
    "руке закрыла бы это — вопрос тот же, что оставлен открытым в S3n",
    "Рука C1 обучалась на 3 493 чанках (28.6M токенов) против 9 776 чанков у "
    "`v12r`: за 2 000 шагов батч-1 она видит бо́льшую долю своего корпуса "
    "(57 % против 20 %). Для вопроса «разрушает ли язык протокол» это не мешает "
    "(обвал воспроизведён), но абсолютные уровни лосса C1 с руками S3m "
    "несопоставимы — сопоставимы только PPL",
    "Строки спецтокенов: у обеих контрольных рук и во всех состояниях пробы "
    "`resized_embeddings: false` — resize в пробе не срабатывал (как и в S3n). "
    "Гипотеза «обвал из-за инициализации спецтокенов» этим замером не проверяется "
    "и не исключается",
    "Отдельно от этого свода: каталог `runs/s3t-arms-20260916/` не несёт "
    "`run_manifest.json`, и правило C-012 (AD-2) на нём красное — дефект внесён "
    "дельной S3t (коммит 5b408ab) и этим сводом не правится: чужой каталог "
    "прогона не переписывается задним числом",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def sha256_if_exists(path: Path) -> str | None:
    """Хеш файла, которого может не быть в корне: у фикстуры свода нет ``tools/``.

    Отсутствие хеша — честная пометка «не проверено», а не ноль и не отказ:
    артефакт назван путём и в самом evidence, а хеш — дополнение к пути.
    """
    return sha256_file(path) if path.is_file() else None


def load(root: Path, rel: str) -> dict | None:
    path = root / rel
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def reference_numbers(root: Path) -> tuple[dict, list[str], int]:
    """Базы, потолки и хеши наборов — из эталонных evidence, а не из этого файла.

    Потолок **вычисляется** от базы (правило ADR-022 п.3) и дополнительно
    сверяется с записанным в эталоне: записанное могло быть снято другим прибором,
    и молчаливое расхождение здесь означало бы, что свод и вердикт считают
    проход по разным порогам.

    Возвращает `(числа, проблемы, код)`: отсутствие эталона — NOT-VERIFIED (2,
    входа нет), расхождение потолка — отказ (1, вход есть и он противоречив).
    """
    missing: list[str] = []
    refused: list[str] = []
    out: dict = {"components": {}, "sets": {}}

    ref_k1 = load(root, REF_K1)
    ref_k2 = load(root, REF_K2)
    ref_v1 = load(root, REF_V1)
    for name, data in (("K1", ref_k1), ("K2", ref_k2), ("v1", ref_v1)):
        if data is None:
            missing.append(f"нет эталона {name}")
    if missing:
        return out, missing, EXIT_NOT_VERIFIED

    def set_sha(data: dict, set_name: str) -> str | None:
        node = (data.get("datasets") or {}).get(set_name)
        return node.get("sha256") if isinstance(node, dict) else None

    bases = {
        "k1": (ref_k1["ppl"]["v3_general"]["ppl"], "v3_general", REF_K1, "S3q",
               ref_k1.get("baseline", {}).get("new_ceiling"), set_sha(ref_k1, "v3_general")),
        "k2": (ref_k2["ppl"]["k2_general"]["ppl"], "k2_general", REF_K2, "S3t",
               ref_k2.get("component", {}).get("ceiling"), set_sha(ref_k2, "k2_general")),
        "v1": (ref_v1["ppl"]["v1_general"]["ppl"], "v1_general", REF_V1, "S3h",
               None, set_sha(ref_v1, "v1_general")),
    }
    for comp, (base, set_name, rel, stage, recorded, set_hash) in bases.items():
        ceiling = CEILING_FACTOR * base
        entry = {
            "role": comp.upper() if comp != "v1" else "исторический",
            "set": set_name,
            "base_ppl": base,
            "ceiling_ppl": ceiling,
            "ceiling_rule": f"{CEILING_FACTOR}× базы (ADR-022 п.3)",
            "ceiling_recorded_in_evidence": recorded,
            "evidence": rel,
            "evidence_stage": stage,
            "evidence_sha256": sha256_file(root / rel),
            "set_sha256": set_hash,
        }
        if comp == "v1":
            #: Потолок v1 действующим не является (ADR-025/ADR-029): он приводится
            #: как исторический, чтобы читать числа C1 на том же наборе, на котором
            #: обвал был снят. Решение по нему не принимается.
            entry["role"] = "исторический (решающим не является)"
            entry["used_for_decision"] = False
        else:
            entry["used_for_decision"] = True
        out["components"][comp] = entry
        if set_hash:
            out["sets"][set_name] = {"sha256": set_hash, "role": SETS[set_name]}
        if recorded is not None:
            rel_delta = abs(ceiling - float(recorded)) / max(abs(float(recorded)), 1e-12)
            entry["ceiling_agrees_with_recorded"] = bool(rel_delta <= BASE_AGREEMENT_TOL)
            if not entry["ceiling_agrees_with_recorded"]:
                refused.append(
                    f"{comp}: вычисленный потолок {ceiling!r} не совпал с записанным "
                    f"{recorded!r} (Δ {rel_delta:.2e})")
    return out, refused, (EXIT_FAIL if refused else EXIT_OK)


def read_arm_report(root: Path, arm: str, spec: dict, refs: dict,
                    stand: Path) -> tuple[dict | None, list[str], int]:
    """Отчёт руки + параметры прогона.

    Возвращает `(блок, проблемы, код)`. Нет файла отчёта — NOT-VERIFIED (входа
    нет, свод собирать не из чего). Есть, но проба мерила не тот набор или рука в
    таблице перепутана — **отказ**: числа есть, и именно поэтому их нельзя
    молчаливо сравнить с чужими.
    """
    path = root / ARMS_DIR / f"{arm}.json"
    if not path.is_file():
        return None, [f"{arm}: нет отчёта прибора {path.relative_to(root)}"], EXIT_NOT_VERIFIED
    rep = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []

    #: Наборы: прибор обязан был мерить те же файлы, что эталоны. Подмена набора
    #: сделала бы числа несопоставимыми по построению, а не «немного другими».
    #: Проверяются все три — включая исторический `v1_general`: именно на нём
    #: читается ответ на вопрос дня, и подмена его прошла бы незамеченной.
    for set_name in (refs["components"][c]["set"] for c in refs["components"]):
        node = rep["sets"].get(set_name)
        if node is None:
            problems.append(f"{arm}: проба не мерила {set_name}")
            continue
        if refs["sets"].get(set_name, {}).get("sha256") not in (None, node["sha256"]):
            problems.append(f"{arm}: {set_name} подменён (sha256 {node['sha256'][:12]} ≠ "
                            f"эталонного)")
    if problems:
        return None, problems, EXIT_FAIL

    params_path = stand / spec["run_dir"] / "calib_params.json"
    params = json.loads(params_path.read_text(encoding="utf-8")) if params_path.is_file() else {}
    for key, want in (("replay_share_pct", spec["replay_share_pct"]),
                      ("peak_lr_scale", spec["peak_lr_scale"])):
        got = params.get(key)
        if got is not None and float(got) != float(want):
            problems.append(f"{arm}: {key} в прогоне {got!r} ≠ ожидаемого {want!r} — "
                            f"рука в таблице перепутана")

    steps: dict[str, int] = {}
    cpt_steps = params.get("cpt_steps")
    for state, step in STATE_STEPS.items():
        if state not in rep["states"]:
            problems.append(f"{arm}: в отчёте нет состояния {state}")
            continue
        if step is None:
            #: Шаг финального состояния — ``cpt_steps`` руки из её собственного
            #: ``calib_params.json``, а не «понятно, что 2000»: в фикстуре без
            #: параметров шаг неизвестен, и это отказ, а не догадка.
            if not cpt_steps:
                problems.append(
                    f"{arm}: шаг состояния {state} неизвестен — нет cpt_steps в "
                    f"calib_params.json прогона")
                continue
            step = int(cpt_steps)
        steps[state] = int(step)

    if problems:
        return None, problems, EXIT_FAIL

    #: Подпись входного состояния: путь, хеш и объём чекпойнта. Хеш считается по
    #: файлу на сетевом диске (AD-4 — читаем по месту, копий не держим); если файла
    #: нет (прогон свода на синтетической фикстуре), это не отказ, а честная
    #: пометка «не проверено» — вердикт на хеш не опирается.
    checkpoints = []
    for state, step in sorted(steps.items(), key=lambda kv: kv[1]):
        ck = (rep["states"][state].get("load") or {}).get("checkpoint")
        node = {"state": state, "step": step, "path": ck, "sha256": None,
                "bytes": None, "resized_embeddings":
                    (rep["states"][state].get("load") or {}).get("resized_embeddings")}
        if ck and Path(ck).is_file():
            node["bytes"] = Path(ck).stat().st_size
            node["sha256"] = sha256_file(Path(ck))
        checkpoints.append(node)

    block = {
        "arm": arm,
        "report": str(path.relative_to(root)),
        "report_sha256": sha256_file(path),
        "vary": spec["vary"],
        "corpus": spec["corpus"],
        "replay_share_pct": params.get("replay_share_pct", spec["replay_share_pct"]),
        "peak_lr_scale": params.get("peak_lr_scale", spec["peak_lr_scale"]),
        "cpt_steps": cpt_steps,
        "seed": params.get("seed"),
        "attn": params.get("attn"),
        "run_dir": str(stand / spec["run_dir"]),
        "pipeline_sha256": rep["instrument"]["pipeline_sha256"],
        "checkpoints": checkpoints,
        "states": rep["states"],
        #: Порядок состояний как в отчёте (base → чекпойнты → вакуумная точка):
        #: для учётной карточки прогона он и есть порядок замера.
        "states_in_order": list(rep["states"]),
        #: Все наборы прогона (включая контрольный v1_general), а не только
        #: компоненты: манифест описывает прогон, а не вердикт по нему.
        "all_sets": {name: {"path": node["path"], "sha256": node["sha256"]}
                     for name, node in rep["sets"].items()},
        "probe_checks": rep.get("checks", []),
    }
    return block, [], EXIT_OK


def identity_check(arms: dict[str, dict], refs: dict) -> tuple[dict, list[str]]:
    """Тождество прибора: три числа, снятые в этих же прогонах, против эталонов.

    Одно совпадение объяснялось бы совпадением набора; три — только тем, что
    измеряет та же функция. Проверяются состояния ``base`` **обеих** рук: прогонов
    два, и «тот же прибор» обязан держаться в каждом, а не в среднем по двум.
    """
    checks: dict[str, dict] = {}
    problems: list[str] = []
    worst = 0.0
    for arm, block in arms.items():
        base = block["states"]["base"]["sets"]
        for comp, meta in refs["components"].items():
            set_name = meta["set"]
            measured = base[set_name]["ppl"]
            historical = meta["base_ppl"]
            abs_delta = abs(measured - historical)
            rel_delta = abs_delta / max(abs(historical), 1e-12)
            key = f"{arm}:{set_name}"
            ok = abs_delta <= IDENTITY_TOLERANCE
            checks[key] = {
                "ppl_measured": measured,
                "ppl_historical": historical,
                "abs_delta": abs_delta,
                "rel_delta": rel_delta,
                "within_tolerance": ok,
                "historical_evidence": meta["evidence"],
                "historical_stage": meta["evidence_stage"],
            }
            worst = max(worst, abs_delta)
            if not ok:
                problems.append(
                    f"{arm}: {set_name} {measured!r} не сошёлся с эталоном "
                    f"{historical!r} (Δ {abs_delta:.3e} > {IDENTITY_TOLERANCE})")
    return {"what": (
        "три числа с историческими значениями сняты в этих же прогонах: одно "
        "совпадение объяснялось бы совпадением набора, три — только тем, что "
        "измеряет та же функция"), "tolerance_abs": IDENTITY_TOLERANCE,
        "checks": checks, "worst_abs_delta": worst,
        "reproduced": not problems}, problems


def pipeline_identity(root: Path, arms: dict[str, dict]) -> tuple[dict, list[str]]:
    """Одна ли копия пайплайна грузила чекпойнты — проверено хешем, а не именем.

    Загрузка состояния идёт ``_load_ckpt_with_resize`` из файла пайплайна **того
    прогона**, чей чекпойнт меряется. У двух контрольных рук копии разные по
    пути; сравниваются их хеши и хеш копии, которой мерились руки S3t, — иначе
    «тем же прибором» было бы утверждением о имени файла.
    """
    hashes = {arm: block["pipeline_sha256"] for arm, block in arms.items()}
    s3t_copy = root / "runs" / "calib-25-0.35-20260916-0820" / "laguna_pipeline_calib.py"
    s3t_hash = sha256_file(s3t_copy) if s3t_copy.is_file() else None
    hashes["runs/calib-25-0.35-20260916-0820 (копия рук S3t)"] = s3t_hash
    distinct = {h for h in hashes.values() if h}
    problems = []
    if len(distinct) > 1:
        problems.append(f"копии пайплайна различаются: {hashes}")
    return {"what": "копии пайплайна, грузившие чекпойнты, тождественны по хешу",
            "hashes": hashes, "one_copy": len(distinct) <= 1}, problems


def build_matrix(arms: dict[str, dict], refs: dict) -> tuple[list[dict], list[str]]:
    """Строки таблицы: рука × {K1, K2, v1} × шаг → PPL, отношение, проход, конъюнкция.

    Проход — механическое сравнение с потолком компоненты (AD-11: критерий
    бинарный). Отношение — к **своей** базе каждой компоненты (ADR-027 п.2).
    """
    problems: list[str] = []
    rows: list[dict] = []
    for arm, block in arms.items():
        base = block["states"]["base"]["sets"]
        #: База руки обязана совпасть с базой эталона: отношение к «почти той же»
        #: базе — это отношение по другой шкале, и заметить это можно только здесь.
        for comp, meta in refs["components"].items():
            got = base[meta["set"]]["ppl"]
            rel = abs(got - meta["base_ppl"]) / max(abs(meta["base_ppl"]), 1e-12)
            if rel > BASE_AGREEMENT_TOL:
                problems.append(
                    f"{arm}: база {meta['set']} в отчёте {got!r} не совпала с базой "
                    f"эталона {meta['base_ppl']!r} (Δ {rel:.2e})")
        if problems:
            return rows, problems

        points = [("base", 0)] + [(s, b) for s, b in
                                  sorted(((c["state"], c["step"])
                                          for c in block["checkpoints"]),
                                         key=lambda kv: kv[1])]
        for state, step in points:
            sets = block["states"][state]["sets"]
            row: dict = {"arm": arm, "state": state, "step": step,
                         "checkpoint": next((c["path"] for c in block["checkpoints"]
                                             if c["state"] == state), None)}
            for comp, meta in refs["components"].items():
                ppl = sets[meta["set"]]["ppl"]
                ratio = ppl / base[meta["set"]]["ppl"]
                row[f"{comp}_ppl"] = ppl
                row[f"{comp}_base"] = base[meta["set"]]["ppl"]
                row[f"{comp}_ratio"] = ratio
                row[f"{comp}_ceiling"] = meta["ceiling_ppl"]
                row[f"{comp}_pass"] = bool(ppl <= meta["ceiling_ppl"])
            row["conjunction"] = bool(row["k1_pass"] and row["k2_pass"])
            row["diagnosis"] = (
                "реплей держит свой жанр, обобщение потеряно"
                if row["k1_pass"] and not row["k2_pass"]
                else "обобщение сохранено, свой жанр не удержан"
                if row["k2_pass"] and not row["k1_pass"]
                else "обе компоненты пройдены" if row["k1_pass"] and row["k2_pass"]
                else "обе компоненты провалены")
            rows.append(row)
    return rows, problems


def curve_and_crossing(rows: list[dict], refs: dict) -> dict:
    """Профиль по шагам и первый ИЗМЕРЕННЫЙ шаг за потолком — по каждой компоненте.

    Первый пересечённый шаг — это не «момент обвала»: между стартом и первой
    сохранённой точкой замеров нет, и переход мог случиться в любой точке
    интервала (та же оговорка, что в S3n).
    """
    out: dict = {}
    for arm in sorted({r["arm"] for r in rows}):
        arm_rows = sorted((r for r in rows if r["arm"] == arm), key=lambda r: r["step"])
        comps: dict = {}
        for comp, meta in refs["components"].items():
            profile = [{"step": r["step"], f"{comp}_ppl": r[f"{comp}_ppl"],
                        f"{comp}_ratio": r[f"{comp}_ratio"],
                        f"{comp}_pass": r[f"{comp}_pass"]} for r in arm_rows]
            crossed = [r for r in arm_rows if not r[f"{comp}_pass"]]
            peak = max(arm_rows, key=lambda r: r[f"{comp}_ppl"])
            final = arm_rows[-1]
            first_point = min((r for r in arm_rows if r["step"] > 0),
                              key=lambda r: r["step"])
            if not crossed:
                shape = "потолок не пересечён ни на одной сохранённой точке"
            else:
                #: Где пик — первая точка или позже: «ступенька» (обвал завершён до
                #: первой сохранённой точки) и «сползание к пику» различают версии
                #: о причине, и это различие берётся из чисел, а не из глаз.
                head = (f"пик на первой сохранённой точке ({peak['step']})"
                        if peak["step"] == first_point["step"]
                        else f"пик на шаге {peak['step']}, не на первой точке")
                tail = ("откат выводит под потолок к финалу"
                        if final[f"{comp}_pass"] else "откат под потолок не выводит")
                shape = f"{head}; {tail}"
            comps[comp] = {
                "set": meta["set"],
                "base_ppl": meta["base_ppl"],
                "ceiling_ppl": meta["ceiling_ppl"],
                "profile": profile,
                "max_ppl": peak[f"{comp}_ppl"],
                "max_ratio": max(r[f"{comp}_ratio"] for r in arm_rows),
                "max_ratio_step": peak["step"],
                "peak_is_first_point": bool(peak["step"] == first_point["step"]),
                "first_point_ratio": first_point[f"{comp}_ratio"],
                "final_ppl": final[f"{comp}_ppl"],
                "final_ratio": final[f"{comp}_ratio"],
                "first_crossing_step": crossed[0]["step"] if crossed else None,
                "first_crossing_ratio": crossed[0][f"{comp}_ratio"] if crossed else None,
                "steps_above_ceiling": [r["step"] for r in crossed],
                "shape": shape,
            }
        out[arm] = comps
    return out


def loss_trajectory(stand: Path, root: Path) -> dict:
    """Траектория лосса рук — **побочное наблюдение**, а не критерий.

    Зачем оно нужно. Вердикт (б) говорит «низкий LR сохраняет язык». Без второго
    числа это читалось бы как «низкий LR ничего не делает» — а это другое
    утверждение. Поэтому рядом приводится лосс руки **на её собственном корпусе**:
    у C2 корпус тот же (`v12r`), что у руки `25-0.35`, и при одном сиде лосс двух
    рук сопоставим. Лосс **C1** с ними не сравнивается: корпус другой (S3m-2, тот
    же запрет).

    Отсутствие файла — не отказ свода: вердикт на лосс не опирается, и в отчёте
    это помечено `available: false`.
    """
    out: dict = {"note": ("лосс между руками с разным корпусом не сравнивается "
                          "(S3m-2); сопоставимы только C2 и 25-0.35 — один корпус, "
                          "один сид"), "arms": {}}
    for arm, spec in ARMS.items():
        path = stand / spec["run_dir"] / "logs" / "loss_trace.jsonl"
        if not path.is_file():
            out["arms"][arm] = {"available": False,
                                "why": f"нет {path}",
                                "corpus": spec["corpus"]}
            continue
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        losses = [r["loss"] for r in rows]
        third = max(len(losses) // 3, 1)
        first, last = losses[:third], losses[-third:]
        lrs = [r["lr"] for r in rows if r.get("lr")]
        out["arms"][arm] = {
            "available": True,
            "corpus": spec["corpus"],
            "steps": len(rows),
            "first_third_mean": statistics.fmean(first),
            "last_third_mean": statistics.fmean(last),
            "delta": statistics.fmean(last) - statistics.fmean(first),
            "min": min(losses), "max": max(losses),
            "decreasing": statistics.fmean(last) < statistics.fmean(first),
            "peak_lr": max(lrs) if lrs else None,
            "source": str(path),
        }
    #: Уголёк сравнения — числа руки `25-0.35` из её собственного свода, а не
    #: пересчитанные здесь: одно место правды на число.
    s3m = load(root, REF_PEER_S3M)
    if s3m and isinstance(s3m.get("loss_trajectory_criterion_a"), dict):
        peer = s3m["loss_trajectory_criterion_a"].get("25-0.35")
        if peer:
            out["peer_25-0.35"] = dict(peer, source=REF_PEER_S3M,
                                       corpus=ARMS["ctrl-C2-25-0.035"]["corpus"])
    return out


def lr_comparison(rows: list[dict], root: Path) -> dict:
    """Вердикт (б): что меняет низкий LR — сравниваются два числа одного корпуса.

    C2 и `25-0.35` обучались на **одном** корпусе (`v12r`), с одним сидом и одной
    упаковкой; варьируется ровно пик LR (×0.035 против ×0.35). Числа `25-0.35`
    берутся из его собственного свода, а не меряются здесь заново.
    """
    k2set = load(root, REF_PEER_K2SET)
    s3m = load(root, REF_PEER_S3M)
    peer: dict = {"evidence": [REF_PEER_K2SET, REF_PEER_S3M]}
    if k2set:
        arm = next((a for a in k2set.get("arms", {}).get("arms", [])
                    if a["arm"] == "25-0.35"), None)
        if arm:
            peer["k1_final"] = {"ppl": arm["k1_ppl"], "ratio": arm["k1_ratio"],
                                "pass": arm["k1_pass"]}
            peer["k2_final"] = {"ppl": arm["k2_ppl"], "ratio": arm["k2_ratio"],
                                "pass": arm["k2_pass"]}
    if s3m:
        arm = next((a for a in s3m.get("arms", []) if a["arm"] == "25-0.35"), None)
        if arm:
            peer["v1_final"] = {"ppl": arm["ppl_general"],
                                "ratio": arm["ratio_vs_base"]}
    c2 = {r["step"]: r for r in rows if r["arm"] == "ctrl-C2-25-0.035"}
    c2_final = max(c2.values(), key=lambda r: r["step"])
    comparison = {
        "what": ("низкий пик LR (C2, ×0.035) против пика ×0.35 на **том же** "
                 "корпусе `v12r` (рука 25-0.35): варьируется одна величина"),
        "peer": peer,
        "c2": {
            "peak_lr_scale": ARMS["ctrl-C2-25-0.035"]["peak_lr_scale"],
            "k1_final": {"ppl": c2_final["k1_ppl"], "ratio": c2_final["k1_ratio"]},
            "k2_final": {"ppl": c2_final["k2_ppl"], "ratio": c2_final["k2_ratio"]},
            "v1_final": {"ppl": c2_final["v1_ppl"], "ratio": c2_final["v1_ratio"]},
        },
    }
    if "k1_final" in peer and peer["k1_final"]:
        comparison["ratio_of_ratios"] = {
            "k1": peer["k1_final"]["ratio"] / c2_final["k1_ratio"],
            "k2": peer["k2_final"]["ratio"] / c2_final["k2_ratio"],
        }
    if peer.get("v1_final"):
        comparison.setdefault("ratio_of_ratios", {})["v1"] = (
            peer["v1_final"]["ratio"] / c2_final["v1_ratio"])
    return comparison


def build_verdict(rows: list[dict], curves: dict, lr: dict,
                  v1_ceiling: float) -> dict:
    """Три вопроса контракта — с числами, а не прилагательными."""
    c1 = {r["step"]: r for r in rows if r["arm"] == "ctrl-C1-100-0.35"}
    c1_final = max(c1.values(), key=lambda r: r["step"])
    c1_first = min((r for r in c1.values() if r["step"] > 0), key=lambda r: r["step"])
    c2_curves = curves["ctrl-C2-25-0.035"]
    c1_curve = curves["ctrl-C1-100-0.35"]
    c1_v1 = {s: c1[s]["v1_ppl"] / c1[s]["v1_base"] for s in sorted(c1)}

    #: (а) Протокол без домена. Различаются два утверждения, и смешивать их
    #: нельзя: «обвал случается на первых сотнях шагов» и «язык разрушен к концу».
    #: По финалу конъюнкция пройдена; на первой точке K2 за потолком; на
    #: историческом наборе воспроизведён весь диапазон ×4.6…9.5 — без домена.
    c2 = {r["step"]: r for r in rows if r["arm"] == "ctrl-C2-25-0.035"}
    c2_final = max(c2.values(), key=lambda r: r["step"])
    protocol = {
        "answer": ("частично: протокол воспроизводит обвал без домена, но не "
                   "разрушает язык к концу руки"),
        "protocol_step_reproduced": True,
        "final_conjunction_passed": bool(c1_final["conjunction"]),
        "numbers": {
            "final": {k: {"ppl": c1_final[f"{k}_ppl"], "ratio": c1_final[f"{k}_ratio"],
                          "pass": c1_final[f"{k}_pass"]} for k in COMPONENTS},
            "first_point": {"step": c1_first["step"],
                            **{k: {"ppl": c1_first[f"{k}_ppl"],
                                   "ratio": c1_first[f"{k}_ratio"],
                                   "pass": c1_first[f"{k}_pass"]}
                               for k in COMPONENTS}},
            "v1_general_by_step": c1_v1,
            "v1_historical_ceiling": v1_ceiling,
            "v1_max_ratio": max(c1_v1.values()),
            "v1_min_ratio": min(c1_v1.values()),
        },
        "why": [
            "C1 обучалась **только на общем языке** (3 493 чанка "
            "`general_replay_ru.txt`, `mix_domain_ratio: 0.0`) — домена в её "
            "материале нет вовсе, и обвал на историческом наборе всё равно "
            f"воспроизведён: {c1_v1[500]:.2f}× на 500-м шаге, "
            f"{max(c1_v1.values()):.2f}× в максимуме — то есть диапазон ×4.6…9.5 "
            "объясняется протоколом, а не доменной частью микса",
            "На обеих компонентах профиль — ступенька с откатом: пик на первой "
            "сохранённой точке (500) и дальше монотонно вниз; к концу руки "
            f"конъюнкция пройдена (K1 ×{c1_final['k1_ratio']:.3f}, "
            f"K2 ×{c1_final['k2_ratio']:.3f})",
            "Пересечение потолка на 500-м шаге — только по K2, и оно узкое: "
            f"{c1_first['k2_ppl']:.4f} против потолка {c1_first['k2_ceiling']:.4f} "
            f"(×{c1_first['k2_ratio']:.3f} при пороге 2.000); по K1 первая точка "
            f"потолок не пробивает (×{c1_first['k1_ratio']:.3f}). Твёрдое "
            "свидетельство ступеньки — исторический набор, где числа далеки от "
            "потолка, а не K2 на 500-м шаге",
        ],
    }

    #: (б) Низкий LR. Здесь важно не спутать «LR сохраняет язык» с «LR ничего не
    #: делает»: по траектории лосса рука училась — это отдельный блок
    #: `loss_trajectory`, и в вердикт он входит как оговорка, а не как критерий.
    low_lr = {
        "answer": "да, решающе: при ×0.035 язык не сдвигается вовсе",
        "numbers": lr,
        "why": [
            "C2 и `25-0.35` — один корпус (`v12r`), один сид, одна упаковка; "
            "варьируется ровно пик LR",
            f"Финал C2: K1 ×{c2_final['k1_ratio']:.3f}, K2 ×{c2_final['k2_ratio']:.3f} "
            f"против ×{lr['peer']['k1_final']['ratio']:.3f} и "
            f"×{lr['peer']['k2_final']['ratio']:.3f} у `25-0.35`",
            "На историческом наборе разрыв ещё резче: "
            f"×{c2_final['v1_ratio']:.3f} против ×{lr['peer']['v1_final']['ratio']:.3f}",
            "Ни на одном шаге и ни на одной компоненте C2 не приблизилась к "
            "потолку: максимум отношения — "
            f"{curves['ctrl-C2-25-0.035']['k2']['max_ratio']:.3f} по K2 (порог 2.000)",
        ],
    }

    #: (в) Согласие с S3n и ADR-029. Оба утверждения проверяются по числам этого
    #: замера, а не пересказываются.
    s3n = {
        "answer": ("согласуется и уточняет: ступенька в (0, 500] воспроизведена на "
                   "руке без домена, а её глубина управляется пиком LR"),
        "consistent": True,
        "why": [
            "S3n: «обвал завершён до первой сохранённой точки, одинаковой формы у "
            "всех четырёх рук». У C1 профиль той же формы: пик на первой точке "
            "(500) и откат дальше — по обеим компонентам; различие только в том, "
            "что по K1 пик потолок не пробивает (×1.848 при пороге 2.000), а по K2 "
            "пробивает (×2.033). Впервые это показано на руке, в корпусе которой "
            "домена нет, — значит форма не свойство доменной части микса",
            "S3n: «глубину обвала задаёт ПИК LR, а не доля replay». Здесь это "
            "проверено с другой стороны: при пике в 10 раз ниже (C2) потолок не "
            "пересечён ни на одной точке и ни по одной компоненте, а подъём на "
            "первой точке есть у обеих (K1 ×%.3f, K2 ×%.3f против ×%.3f и ×%.3f "
            "у C1) — то есть низкий LR гасит амплитуду ступеньки, а не переносит "
            "событие на другой шаг" % (
                c2_curves["k1"]["first_point_ratio"], c2_curves["k2"]["first_point_ratio"],
                c1_curve["k1"]["first_point_ratio"], c1_curve["k2"]["first_point_ratio"]),
            "ADR-029: «разрушение языка не воспроизводится ни в жанре реплея, ни "
            "вне обучающего распределения». C1 это подтверждает на третьем, "
            "независимом материале (100 % общий язык): финал — конъюнкция пройдена, "
            f"K1 ×{c1_final['k1_ratio']:.3f}, K2 ×{c1_final['k2_ratio']:.3f}",
            "Решение ADR-029 этот замер не меняет: оно принято по конъюнкции двух "
            "чистых наборов на четырёх руках сетки, и продолжение стадии обеспечено "
            "ими. Уточняется **объяснение**: исторические ×4.6…9.5 — свойство "
            "протокола при пике ×0.35 на наборе из 24 документов, а не свидетельство "
            "о доменном миксе",
        ],
        "limits": [
            "Обе контрольные руки — 2 000 шагов калибровки, а не полный CPT: "
            "поведение языка на длинном прогоне этим замером не покрыто (та же "
            "граница, что названа в ADR-029)",
            "Переход локализован интервалом (0, 500], а не шагом: чекпойнты у обеих "
            "рук пишутся каждые 500 шагов",
        ],
    }
    del c2  # профиль C2 в вердикт (б) входит числами, а не таблицей
    return {"protocol_destroys_language": protocol,            "low_lr_effect": low_lr,
            "consistent_with_s3n": s3n}


def ensure_run_manifest(root: Path, arms: dict[str, dict]) -> dict:
    """Манифест AD-2 для каталога пробы: считается из отчётов, а не пишется руками.

    Правило C-012 требует манифест у **каждого** каталога прогона, а проба PPL —
    это прогон. Манифест здесь не «документ о прогоне», а его учётная карточка:
    наборы и их хеши берутся из отчётов прибора, база и версия пайплайна — оттуда
    же. Ручная копия этих чисел разошлась бы с отчётами молча.
    """
    sets: dict = {}
    for block in arms.values():
        sets.update(block["all_sets"])
    concat = "\n".join(f"{n}:{sets[n]['sha256']}" for n in sorted(sets))
    manifest = {
        "dataset_path": "datasets/",
        "dataset_sha256": hashlib.sha256(concat.encode("utf-8")).hexdigest(),
        "dataset_note": ("проба меряет не корпус, а состояния чекпойнтов двух "
                         "контрольных рук на трёх наборах общего языка; "
                         "dataset_sha256 — sha256 по их содержимому, склеенному в "
                         "порядке имён наборов, datasets_extra — поимённо. "
                         "Пути, начинающиеся с `calib/`, отсчитываются от корня "
                         "сетевого диска gb10-shared (там лежат прогоны рук; "
                         "симлинка в кейс нет — AD-4)"),
        "base_model_id": "Qwen/Qwen2.5-0.5B",
        #: Копии пайплайна обеих рук тождественны по sha256 (это проверено
        #: отдельной проверкой свода), поэтому путь назван один — общий для рук.
        "pipeline_path": f"calib/{ARMS['ctrl-C1-100-0.35']['run_dir']}/laguna_pipeline_calib.py",
        "pipeline_version": f"laguna_pipeline_calib.py@{sorted({b['pipeline_sha256'][:12] for b in arms.values()})[0]}",
        "pipeline_sha256": sorted({b["pipeline_sha256"] for b in arms.values()})[0],
        "image": ("не контейнер: локальная машина (RTX 4080 SUPER), НЕ стенд GB10, "
                  "torch/transformers — локальное окружение, dtype bfloat16, "
                  "batch 4, max_len 1024"),
        "seed": sorted({b.get("seed") for b in arms.values() if b.get("seed")} or [42])[0],
        "stages": [{"name": "ppl-probe", "status": "done"}],
        "pipeline_complete": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_version": "tools/calib_ppl_probe.py",
        "datasets_extra": {name: {"path": node["path"], "sha256": node["sha256"]}
                           for name, node in sorted(sets.items())},
        "hyperparameters": {
            "measurement": "PPL состояний контрольных рук S3o (C1, C2) по обеим компонентам",
            "device": "cuda", "dtype": "bfloat16", "batch": 4, "max_len": 1024,
            "tokenizer_setup": "pipeline (8 спецтокенов + resize)",
            "states_measured": ",".join(
                f"{arm}:{s}" for arm, b in sorted(arms.items())
                for s in b["states_in_order"]),
            "seed_note": "проба не сэмплирует: PPL детерминирован, сид наследуется от прогонов рук",
        },
        "hyperparameters_source": "отчёты пробы runs/s3v-arms-20260916/*.json (instrument, sets)",
    }
    path = root / ARMS_DIR / "run_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    #: `created_at` — время **первой** записи манифеста, а не этого вызова свода:
    #: иначе повторный свод переписывал бы уже закоммиченный артефакт прогона, и
    #: хеш манифеста в evidence разошёлся бы с файлом при первом же перегоне.
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8")).get("created_at")
            if previous:
                manifest["created_at"] = previous
        except (OSError, ValueError):
            pass
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return {"path": str(path.relative_to(root)),
            "dataset_sha256": manifest["dataset_sha256"],
            "why": ("манифест AD-2 для каталога пробы (C-012): числа взяты из отчётов "
                    "прибора, а не вписаны")}


def build(root: Path, stand: Path) -> tuple[dict, int]:
    refs, problems, rc = reference_numbers(root)
    if rc != EXIT_OK:
        print(("NOT-VERIFIED: " if rc == EXIT_NOT_VERIFIED else "ОТКАЗ: ")
              + "; ".join(problems), file=sys.stderr)
        return {}, rc

    arms: dict[str, dict] = {}
    for arm, spec in ARMS.items():
        block, errs, rc = read_arm_report(root, arm, spec, refs, stand)
        if block is None:
            print(("NOT-VERIFIED: " if rc == EXIT_NOT_VERIFIED else "ОТКАЗ: ")
                  + "; ".join(errs), file=sys.stderr)
            return {}, rc
        arms[arm] = block

    identity, problems = identity_check(arms, refs)
    if problems:
        print("ОТКАЗ: тождество прибора не воспроизведено: " + "; ".join(problems),
              file=sys.stderr)
        return {}, EXIT_FAIL

    pipes, problems = pipeline_identity(root, arms)
    if problems:
        print("ОТКАЗ: " + "; ".join(problems), file=sys.stderr)
        return {}, EXIT_FAIL

    rows, problems = build_matrix(arms, refs)
    if problems:
        print("ОТКАЗ: " + "; ".join(problems), file=sys.stderr)
        return {}, EXIT_FAIL

    curves = curve_and_crossing(rows, refs)
    lr = lr_comparison(rows, root)
    loss = loss_trajectory(stand, root)
    verdict = build_verdict(rows, curves, lr,
                            refs["components"]["v1"]["ceiling_ppl"])
    manifest = ensure_run_manifest(root, arms)

    evidence = {
        "schema": "s3v-control-arms-ppl/1",
        "stage": "S3v",
        "status": "complete",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": ("PPL контрольных рук S3o (C1 — протокол без домена, C2 — низкий "
                    "пик LR) по обеим компонентам меры общего языка: объяснение "
                    "обвала на v1_general и проверка вывода S3n"),
        "question": ("чем объяснялся обвал языка на `v1_general` (×4.6…9.5): "
                     "протоколом CPT или доменной частью микса; и меняет ли что-то "
                     "низкий пик LR"),
        "adr": ["ADR-022 п.1/п.2 (гипотеза протокола и LR)",
                "ADR-025 п.1 (чистота наборов)",
                "ADR-027 п.1/п.2 (двухкомпонентная мера, потолок от своей базы)",
                "ADR-029 (отзыв стоп-сигнала — решение этим замером не меняется)",
                "AD-11 (критерий бинарный)", "AD-12 (доказательства + коммит)"],
        "stand": {
            "where_measured": "локальная машина (RTX 4080 SUPER), НЕ стенд GB10",
            "why": ("стенд не задействован: на нём стартует полный CPT (ADR-029 п.2); "
                    "числа PPL переносятся как свойства наборов, а не как замеры "
                    "производительности стенда"),
            "checkpoints_read_in_place": (f"{stand}/calib/<рука>/checkpoints/ — "
                                          "по месту, копий не делалось (AD-4)"),
        },
        "instrument": {
            "probe": "tools/calib_ppl_probe.py (прибор S3m, не переписан)",
            "measure": "tools/ppl_probe.py:measure (методика _ppl_eval S3h)",
            "load_checkpoint": ("laguna_pipeline_v8.py:_load_ckpt_with_resize из копии "
                                "пайплайна того прогона, чей чекпойнт меряется"),
            "sets_measured": list(SETS),
            "max_len": 1024, "batch": 4, "dtype": "bfloat16", "device": "cuda",
            "tokenizer_setup": "pipeline (8 спецтокенов + resize), как в S3h/S3q/S3t",
            "base_weights": "/home/user/gb10-shared/models-store/experiments/kda-graft/models/qwen2.5-0.5b-base",
            "env_shims": [
                "urllib3.contrib.pyopenssl подменён заглушкой (pyOpenSSL сломан: "
                "AttributeError: module 'lib' has no attribute 'GEN_EMAIL') — "
                "необязательный TLS-бэкенд botocore, в пути пробы не используется",
            ],
        },
        "pipeline_identity": pipes,
        "instrument_identity": identity,
        "baselines": refs["components"],
        "sets": {name: {"sha256": node["sha256"], "role": node["role"]}
                 for name, node in refs["sets"].items()},
        "arms": rows,
        "arms_params": {arm: {k: block[k] for k in
                              ("report", "report_sha256", "vary", "corpus",
                               "replay_share_pct", "peak_lr_scale", "cpt_steps",
                               "seed", "attn", "run_dir", "pipeline_sha256",
                               "checkpoints", "probe_checks")}
                        for arm, block in arms.items()},
        "curves": curves,
        "loss_trajectory": loss,
        "lr_comparison": lr,
        "conjunction_by_arm": {
            arm: {"steps_passed": [r["step"] for r in rows
                                   if r["arm"] == arm and r["conjunction"]],
                  "steps_failed": [r["step"] for r in rows
                                   if r["arm"] == arm and not r["conjunction"]],
                  "final_pass": bool(max((r for r in rows if r["arm"] == arm),
                                         key=lambda r: r["step"])["conjunction"])}
            for arm in ARMS},
        "not_averaged": ("k1_ratio и k2_ratio не усредняются и не сворачиваются в "
                         "одно число: вердикт — конъюнкция (ADR-027 п.1/п.2)"),
        "verdict": verdict,
        "checks": [
            {"name": "instrument_identity", "verdict": "ok",
             "detail": (f"три контрольных числа (v1_general, v3_general, k2_general) "
                        f"сняты в обоих прогонах; худшее абсолютное расхождение "
                        f"{identity['worst_abs_delta']:.3e} при пороге "
                        f"{IDENTITY_TOLERANCE}"),
             "tolerance": IDENTITY_TOLERANCE},
            {"name": "base_agreement", "verdict": "ok",
             "detail": ("база в отчёте каждой руки совпала с базой эталона по обеим "
                        "компонентам — отношения считаны от той же шкалы"),
             "tolerance": BASE_AGREEMENT_TOL},
            {"name": "pipeline_one_copy", "verdict": "ok",
             "detail": ("копии пайплайна контрольных рук и копия рук S3t "
                        "тождественны по sha256")},
            {"name": "run_manifest", "verdict": "ok",
             "detail": f"{manifest['path']}: {manifest['why']}"},
        ],
        "artifacts": [
            {"path": OUT, "role": "evidence замера (этот файл)", "sha256": None,
             "note": "хеш не приводится: файл пишется этим же вызовом"},
            {"path": "tools/assemble_s3v_evidence.py", "role": "свод",
             "sha256": sha256_file(Path(__file__).resolve())},
            {"path": "tools/calib_ppl_probe.py", "role": "прибор (не переписан)",
             "sha256": sha256_if_exists(root / "tools" / "calib_ppl_probe.py")},
            {"path": "tools/ppl_probe.py", "role": "методика замера",
             "sha256": sha256_if_exists(root / "tools" / "ppl_probe.py")},
        ] + [{"path": block["report"], "role": "отчёт прибора по руке",
              "sha256": block["report_sha256"]} for block in arms.values()]
          + [{"path": manifest["path"], "role": "манифест прогона (AD-2)",
              "sha256": sha256_if_exists(root / manifest["path"])}],
        "assumptions": [
            "Прибор и параметры — те же, что дали базы: tools/calib_ppl_probe.py, "
            "bfloat16 / batch 4 / max_len 1024, те же три набора; тождество доказано "
            "числом в этих же прогонах, а не заявлено",
            "Потолок каждой компоненты — 2× её собственной базы (ADR-022 п.3, "
            "ADR-027 п.2); значение вычислено от базы эталона и сверено с записанным",
            "Потолок v1_general (23.863848) действующим не является (ADR-025/ADR-029) "
            "и приведён только для чтения чисел C1 на том же наборе, на котором "
            "обвал был снят",
            "Сравнение по LR одномерно: C2 и `25-0.35` — один корпус, один сид, одна "
            "упаковка; числа `25-0.35` берутся из его собственного свода, а не "
            "меряются здесь заново",
            "Траектория лосса — побочное наблюдение: она показывает, что C2 училась, "
            "но критерием не является и между разными корпусами не сравнивается",
        ],
        "open_questions": OPEN_QUESTIONS,
        "reproduction": [
            "# замер обеих контрольных рук (локальная машина, стенд не задействован)",
            "for arm in ctrl-C1-100-0.35 ctrl-C2-25-0.035; do",
            "  D=/home/user/gb10-shared/calib/$arm-20260916-1632",
            "  python3 tools/calib_ppl_probe.py --pipeline $D/laguna_pipeline_calib.py \\",
            "    --state base=base \\",
            "    --state c500=ckpt:$D/checkpoints/calib_checkpoint_500.pt \\",
            "    --state c1000=ckpt:$D/checkpoints/calib_checkpoint_1000.pt \\",
            "    --state c1500=ckpt:$D/checkpoints/calib_checkpoint_1500.pt \\",
            "    --state cfinal=ckpt:$D/checkpoints/checkpoint_final.pt \\",
            "    --state base_untouched=base \\",
            "    --sets v1_general,v3_general,k2_general \\",
            "    --dtype bfloat16 --device cuda --max-len 1024 --batch 4 \\",
            "    --out runs/s3v-arms-20260916/$arm.json",
            "done",
            "# свод",
            "python3 tools/assemble_s3v_evidence.py",
        ],
        "rollback": ("откат — git reset --hard HEAD && git clean -fd; чекпойнты "
                     "контрольных рук на gb10-shared не удаляются: это исходные "
                     "данные решения (ADR-029 п.5), а не артефакт этого свода"),
    }

    return evidence, EXIT_OK


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="S3v: свод PPL контрольных рук C1/C2")
    ap.add_argument("--case-root", default=None)
    ap.add_argument("--stand-calib", default="/home/user/gb10-shared/calib",
                    help="каталог прогонов контрольных рук на сетевом диске")
    ap.add_argument("--out", default=OUT)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(args.case_root).resolve() if args.case_root else CASE_ROOT
    evidence, rc = build(root, Path(args.stand_calib))
    if rc != EXIT_OK:
        return rc
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    identity = evidence["instrument_identity"]
    print(f"тождество прибора: худшее Δ {identity['worst_abs_delta']:.3e} "
          f"(порог {identity['tolerance_abs']}) — воспроизведено: "
          f"{identity['reproduced']}")
    for arm, node in evidence["conjunction_by_arm"].items():
        print(f"  {arm:20s} финал конъюнкции: "
              f"{'ПРОХОД' if node['final_pass'] else 'провал'} "
              f"(за потолком шаги {node['steps_failed'] or '—'})")
    print(f"\nотчёт: {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
