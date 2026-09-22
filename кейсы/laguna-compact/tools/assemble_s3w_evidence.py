#!/usr/bin/env python3
"""S3w — домен-метрика по всем рукам: цена LR для домена против языка (свод).

Какой вопрос закрывается. Замер языка (S3v) показал: обвал на историческом наборе
объясняется **протоколом** CPT, а его глубина — **пиком LR**, и при пике ×0.035
(C2) язык не сдвигается вовсе (K1 ×1.005, K2 ×1.053 против ×1.520 и ×1.745 у руки
`25-0.35`). Но обе метрики решения — домен и язык — до сих пор не снимались
**в одном прогоне на одних и тех же состояниях**: язык измерен, домен нет. Без
домена выбор между «продолжать полный CPT на ×0.35» и «перезапустить на ×0.035»
нельзя даже назвать обменом: неизвестна вторая половина цены.

Чем закрывается. Тем же прибором (`tools/calib_ppl_probe.py`, методика
`_ppl_eval`) на **всех сохранившихся точках** шести рук — четырёх калибровочных
(`calib-{25,50}-{0.35,0.7}`) и двух контрольных (`ctrl-C1-100-0.35`,
`ctrl-C2-25-0.035`) — плюс нетронутая база, снимаются **обе** метрики:

* домен — `v2_domain` (`datasets/domain_eval_v2.txt`, 200 документов, 125 322
  токена) как решающий набор и `v1_domain` (`datasets/domain_eval.txt`, 5
  документов) как историческая непрерывность с `ppl_domain` GEN-EVAL (S3n);
* язык — `v3_general` (K1) и `k2_general` (K2) — те же компоненты, что в S3q/S3t/S3v.

Почему домен меряется **двумя** наборами, а решает один. `v1_domain` — это 5
документов общего шаблона: ADR-015 назвал прибор слабым прямо по этой причине, и
ADR-018 п.1 собрал v2 именно чтобы её снять. Поэтому решающий набор — `v2_domain`
(×33 по токенам, ×40 по документам), а `v1_domain` приводится рядом как
историческая шкала: он отвечает на вопрос «то же ли это число, что видел S3n», и
без него разрыв с прежними замерами читался бы как смена прибора.

Почему прибор не переписан. Замер сделан тем же `tools/calib_ppl_probe.py` на
тех же `bfloat16 / batch 4 / max_len 1024`. Тождество доказывается **числом в том
же прогоне**, и не одним: базовое состояние каждой руки обязано воспроизвести все
пять исторических эталонов в пределах ±1e-4 — `v1_general` 11.931923888 (S3h),
`v1_domain` 9.290193903 (S3h), `v2_domain` 11.134115856 (S3h), `v3_general`
7.504686374 (S3q), `k2_general` 6.159936 (S3t). Дополнительно числа чекпойнтов
сверяются с **чужими** отчётами того же прибора: домен финала калибровочных рук —
с S3m, язык — с S3t/S3q, язык по шагам контрольных рук — с S3v. Не сошлось —
отказ (код 1), а не пометка: тогда «тот же прибор» не доказано, и сравнивать
числа нечем.

Чего этот свод НЕ делает. Он не решает за владельца и не объявляет победителя:
вердикт — **бинарный ответ** на поставленный вопрос («домен при ×0.035 против
×0.35 на том же миксе: сопоставимо / хуже / лучше») плюс названная числом цена
второй метрики. Полоса сопоставимости ×2 — не выдуманная шкала, а уже принятая в
контуре (ADR-022 п.3: потолок деградации = 2× базы); другого числа контур не
называл, и брать новое значило бы подгонять порог под ответ.

Компоненты K1 и K2 не усредняются и не сворачиваются в одно число (ADR-027 п.2) —
в таблице они стоят столбцами, а вердикт по языку остаётся конъюнкцией.

Коды возврата::

    0 — evidence записан, тождество прибора доказано, вердикт посчитан
    1 — отказ: отчёт руки неполон, набор подменён, рука перепутана, база руки не
        совпала с эталоном, тождество прибора не воспроизведено, у доменного
        набора есть дословное пересечение с обучающим корпусом
    2 — NOT-VERIFIED: нет входа (отчёт руки, эталон, гейт чистоты)

Запуск::

    python3 tools/assemble_s3w_evidence.py
    python3 tools/assemble_s3w_evidence.py --case-root <корень> --stand-calib <диск>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Каталог с отчётами прибора (по отчёту на руку) и файл свода.
ARMS_DIR = "runs/s3w-domain-20260916"
OUT = "evidence/s3w-domain-by-arms.json"

#: Эталоны. Базы, потолки и хеши наборов берутся **из файлов**, а не вписаны
#: здесь: правка эталона обязана ломать свод, а не молча с ним совпадать.
REF_V1V2 = "evidence/ppl-baseline-v1v2.json"   # S3h: v1_general, v1_domain, v2_domain
REF_K1 = "evidence/s3q-baseline-v3.json"       # S3q: база компоненты K1
REF_K2 = "evidence/s3t-baseline-k2.json"       # S3t: база компоненты K2
#: Чужие отчёты того же прибора — не источник чисел, а их сверка.
REF_PEER_DOMAIN = "evidence/s3m-ppl-arms.json"        # S3m: домен финала калибровочных рук
REF_PEER_K2SET = "evidence/s3t-k2-set.json"           # S3t: язык финала калибровочных рук
REF_PEER_S3V = "evidence/s3v-control-arms-ppl.json"   # S3v: язык по шагам контрольных рук

#: Гейт чистоты доменных наборов (ADR-025 п.1/п.2). Оба обязательны: домен — та
#: метрика, ради которой замер и делается, и набор под ней обязан быть проверен.
PURITY = {
    "v2_domain": "evidence/s3w-purity-v2domain.json",
    "v1_domain": "evidence/s3w-purity-v1domain.json",
}

#: Наборы прогона и их роли. Домен — решающая метрика этого замера, язык —
#: вторая метрика решения (её числа снимаются в том же прогоне, чтобы обе
#: половины цены были с одной шкалы).
SETS: dict[str, str] = {
    "v2_domain": "решающая домен-метрика: 200 документов вне обучающего корпуса",
    "v1_domain": "историческая домен-шкала: тот же набор, что `ppl_domain` GEN-EVAL (S3n), 5 документов",
    "v1_general": "контрольное число прибора (S3h), не решающее",
    "v3_general": "компонента K1 меры общего языка (ADR-027 п.1)",
    "k2_general": "компонента K2 меры общего языка (ADR-027 п.1)",
}

#: Решающий и исторический доменные наборы. Разделены явно: решает один, второй
#: отвечает за непрерывность с прежними замерами.
DOMAIN_PRIMARY = "v2_domain"
DOMAIN_ALT = "v1_domain"

#: Компоненты меры общего языка (ADR-027 п.1) — они же столбцы языка в таблице.
COMPONENTS = ("v3_general", "k2_general")

#: Соответствие «имя состояния прибора → шаг». `cfinal` — это `cpt_steps` руки, и
#: он берётся из её собственного `calib_params.json`, а не предполагается.
STATE_STEPS: dict[str, int | None] = {"c500": 500, "c1000": 1000, "c1500": 1500,
                                      "cfinal": None}

#: Какие состояния обязана нести каждая рука. У `calib-25-0.7` точки 500 нет —
#: и это не умолчание, а названная дырка в данных (см. `gap`): первая по времени
#: рука сетки шла по старой ревизии патча, где номерной чекпойнт писался под
#: именем `checkpoint_{step}.pt` и попадал под ретенцию пайплайна. Пропуск
#: объявлен здесь, чтобы «нет файла» не превратилось в «нет строки».
ARMS: dict[str, dict] = {
    "calib-25-0.35": {
        "run_dir": "calib-25-0.35-20260916-0820",
        "kind": "calib",
        "vary": "референс по домену для C2: тот же корпус и сид, пик LR×0.35",
        "replay_share_pct": 25,
        "peak_lr_scale": 0.35,
        "corpus": "v12r (75/25 — тот же кэш, что у C2)",
        "pipeline": "runs/calib-25-0.35-20260916-0820/laguna_pipeline_calib.py",
        "steps": (500, 1000, 1500, 2000),
        "gap": None,
    },
    "calib-25-0.7": {
        "run_dir": "calib-25-0.7-20260916-0820",
        "kind": "calib",
        "vary": "удвоенный пик LR (×0.7) при 25 % replay",
        "replay_share_pct": 25,
        "peak_lr_scale": 0.7,
        "corpus": "v12r (75/25)",
        "pipeline": "runs/calib-25-0.7-20260916-0820/laguna_pipeline_calib.py",
        "steps": (1000, 1500, 2000),
        "gap": ("чекпойнт шага 500 отсутствует: рука шла первой (08:20) по ревизии "
                "патча `numbered_ckpt_every`, где номерной снимок писался как "
                "`checkpoint_500.pt` и был удалён ретенцией пайплайна; последующие "
                "руки пишут `calib_checkpoint_{step}.pt` вне ретенции"),
    },
    "calib-50-0.35": {
        "run_dir": "calib-50-0.35-20260916-0820",
        "kind": "calib",
        "vary": "50 % replay вместо 25 % при пике LR×0.35",
        "replay_share_pct": 50,
        "peak_lr_scale": 0.35,
        "corpus": "v12r50 (50/50, собран S3m)",
        "pipeline": "runs/calib-50-0.35-20260916-0820/laguna_pipeline_calib.py",
        "steps": (500, 1000, 1500, 2000),
        "gap": None,
    },
    "calib-50-0.7": {
        "run_dir": "calib-50-0.7-20260916-0820",
        "kind": "calib",
        "vary": "оба фактора сразу: 50 % replay и пик LR×0.7",
        "replay_share_pct": 50,
        "peak_lr_scale": 0.7,
        "corpus": "v12r50 (50/50, собран S3m)",
        "pipeline": "runs/calib-50-0.7-20260916-0820/laguna_pipeline_calib.py",
        "steps": (500, 1000, 1500, 2000),
        "gap": None,
    },
    "ctrl-C1-100-0.35": {
        "run_dir": "ctrl-C1-100-0.35-20260916-1632",
        "kind": "ctrl",
        "vary": "протокол без домена: 100 % общий язык, пик LR×0.35",
        "replay_share_pct": 100,
        "peak_lr_scale": 0.35,
        "corpus": "ctrl100 (0/100 — домена в обучении нет вовсе)",
        "pipeline": "calib/ctrl-C1-100-0.35-20260916-1632/laguna_pipeline_calib.py",
        "steps": (500, 1000, 1500, 2000),
        "gap": None,
    },
    "ctrl-C2-25-0.035": {
        "run_dir": "ctrl-C2-25-0.035-20260916-1632",
        "kind": "ctrl",
        "vary": "низкий пик LR (×0.035) при том же миксе v12r, что у `calib-25-0.35`",
        "replay_share_pct": 25,
        "peak_lr_scale": 0.035,
        "corpus": "v12r (75/25 — тот же кэш, что у 25-0.35)",
        "pipeline": "calib/ctrl-C2-25-0.035-20260916-1632/laguna_pipeline_calib.py",
        "steps": (500, 1000, 1500, 2000),
        "gap": None,
    },
}

#: Пара, в которой варьируется **только** пик LR: один корпус (`v12r`), один сид,
#: одна упаковка. Вопрос «цена низкого LR для домена» решается на ней, и только
#: на ней: у остальных рук меняется ещё и микс.
LR_PAIR = ("ctrl-C2-25-0.035", "calib-25-0.35")

#: Префикс, которым **этот** свод отличает калибровочные руки от контрольных.
#: Своды S3m и S3t зовут те же руки без него (`25-0.35`): без перевода имени
#: сверка с чужим отчётом молча выродилась бы в ноль сравнений — а это ровно тот
#: случай, когда «проверено» и «не проверено» выглядят одинаково.
CALIB_PREFIX = "calib-"


def peer_name(arm: str) -> str:
    """Имя руки в чужом своде (S3m/S3t зовут калибровочные руки без префикса)."""
    return arm[len(CALIB_PREFIX):] if arm.startswith(CALIB_PREFIX) else arm

#: Порог тождества прибора из контракта дельты — ``±1e-4``, абсолютный. Один
#: порог на все пять контрольных чисел: все они одного порядка (6.16…11.93).
IDENTITY_TOLERANCE = 1e-4

#: Допуск согласия базы руки с базой эталона. Оба числа снимает один и тот же код
#: на одной машине; расхождение может быть только сменой прибора, и тогда
#: отношение руки считалось бы от другой базы — это отказ.
BASE_AGREEMENT_TOL = 1e-6

#: Правило потолка языка (ADR-022 п.3) — множитель, а не значение.
CEILING_FACTOR = 2.0

#: Полоса сопоставимости домена между руками. Это **не** новое число: контур уже
#: пользуется шкалой ×2 для деградации (ADR-022 п.3), и брать под тот же вопрос
#: другую шкалу значило бы подгонять порог под ответ.
COMPARABILITY_FACTOR = 2.0

#: Обстановка прогона, названная явно. Первый пункт — общий для всех проб кейса;
#: второй — про руку C1 и **только** про неё: карта 16 ГБ делится с чужой пробой
#: (проба полного CPT другого контура, пик ~14 ГБ), и её первая попытка упала
#: `CUDA out of memory` на шаге 1000. Замер от этого не «похожий», а тот же:
#: параметры прибора не менялись, а тождество доказано числами — у C1 сошлись те же
#: пять эталонов (Δ 0.0) и все её числа по шагам совпали с S3v (Δ 0.0).
ENV_SHIMS = [
    "urllib3.contrib.pyopenssl подменён заглушкой (pyOpenSSL сломан: "
    "AttributeError: module 'lib' has no attribute 'GEN_EMAIL') — "
    "необязательный TLS-бэкенд botocore, в пути пробы не используется",
    ("C1 (только эта рука): `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` и "
     "повторный запуск пробы — при первом проходе карта 16 ГБ была занята чужой "
     "пробой полного CPT (tools/full_cpt_probe.py --watch, пик ~14 ГБ) и замер упал "
     "CUDA OOM на шаге 1000. Прибор не менялся (те же batch 4 / max_len 1024 / "
     "bfloat16 / наборы): смена расписания попыток числу не передаётся, а тождество "
     "доказано в самом отчёте — база сошлась с пятью эталонами, а c500…cfinal — с "
     "отчётом S3v (расхождение 0.0)"),
]

#: Полоса «тренд стоит». 5 % — уже принятый в контуре допуск совпадения замера
#: (`ppl_probe.py:REPRO_TOLERANCE_PCT`); новых чисел не вводится.
TREND_BAND = 0.05

#: Физический смысл отношений домена: база — нетронутые веса, поэтому
#: отношение **меньше** единицы означает «домен выучен», больше — «домен испорчен».
DOMAIN_RATIO_READING = ("отношение < 1 = домен выучен (PPL ниже базовой), "
                        "> 1 = домен испорчен обучением")

OPEN_QUESTIONS = [
    "У `calib-25-0.7` нет чекпойнта шага 500 — данные утеряны ретенцией пайплайна "
    "до правки патча (см. `arms_params['calib-25-0.7'].gap`). Восстановить точку "
    "нельзя: это снимок весов, а не пересчитываемое число. Профиль этой руки "
    "начинается с шага 1000, и «ступенька в (0, 500]» на ней не проверяется — "
    "вопрос тот же, что оставлен открытым в S3n и S3v",
    "Ни у одной руки нет точек внутри (0, 500]: чекпойнты пишутся каждые 500 "
    "шагов. Максимум обвала домена локализован интервалом, а не шагом",
    "У доменных наборов нулевое **документное** пересечение с обоими обучающими "
    "корпусами (гейт), но ненулевое по 12-граммам: 1733 окна из 49962 (3.47 %) у "
    "`v2_domain`, 78 из 1006 (7.75 %) у `v1_domain` — целиком за счёт доменного "
    "корпуса. Примеры совпавших окон — формульные обороты карточек ('group "
    "relative policy optimization (grpo)', 'swiglu', 'the kitti qa dataset'), то "
    "есть тот самый общий шаблон, который карточка `gen-eval-v2-card.json` "
    "называет не-гейтом. Гейт ADR-025 п.1 сформулирован для наборов `general_eval*` "
    "и на доменный набор формально не распространяется; распространять ли его "
    "(и с какой оговоркой) — вопрос к архитектору, а не решение этого свода",
    "PPL по домену меряет **обобщение внутри домена**, а не «домен вне "
    "распределения»: карточки набора того же семейства и шаблона, что обучающие "
    "(ограничение названо в `gen-eval-v2-card.json → limits`). Общие шаблонные "
    "обороты снижают PPL независимо от забывания, поэтому абсолютный уровень "
    "домена оптимистичен — но отношение к базе считается на том же наборе, и "
    "сдвиг сокращается",
    "Домен-метрика не имеет потолка: ADR-022 п.3 задаёт потолок деградации языка, "
    "а у домена порог — сама база (отношение 1.0). Считать ли «домен выучен» "
    "требованием отношения < 1 или допускать полосу — вопрос к владельцу; здесь "
    "ответ дан на обоих прочтениях (см. `verdict.reading_of_domain_ratio`)",
    "Полномасштабный CPT на стенде идёт на миксе `v12r` (25 % replay, ADR-029 п.2) "
    "при пике LR×0.35 — ровно конфигурация руки `calib-25-0.35`. Решение «перейти "
    "на ×0.035» относится к этой же конфигурации, поэтому пара C2 ↔ `25-0.35` "
    "переносится на него без поправок; но перенос — допущение о переносимости "
    "профиля с 2 000 шагов на полный бюджет, а не измеренный факт",
    "Каталог `runs/s3t-arms-20260916/` не несёт `run_manifest.json` (правило C-012 "
    "/ AD-2 красное) — дефект внесён дельтой S3t (коммит 5b408ab) и этим сводом не "
    "правится: чужой каталог прогона задним числом не переписывается",
    "Тесты инструментов зелёные по всем разделам, включая раздел S3w, кроме ОДНОГО "
    "давнего красного вне этой дельты: `tools/tests/run_tool_tests.sh` сверяет "
    "`datasets/rl_tasks_revpool_v2.jsonl` с карточкой `data/rev-envs-v2-card.json`, "
    "а пул на сетевом диске пересобран (9 162 строки, sha256 e678eb68… против "
    "9 197 и a5d6496a… в карточке, коммит S3f 35f933d). Артефакт живёт вне git "
    "(AD-4, общий сетевой диск) и пересобран не этим замером; карточку задним "
    "числом не правим, дефект называем. На вердикт S3w он не влияет: домен и язык "
    "меряются на своих наборах, пул ревизии в них не входит",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_if_exists(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def load(root: Path, rel: str) -> dict | None:
    path = root / rel
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def reference_numbers(root: Path) -> tuple[dict, list[str], int]:
    """Базы, потолки, хеши наборов и чужие числа сверки — из эталонных evidence.

    Возвращает `(числа, проблемы, код)`. Нет эталона — NOT-VERIFIED (входа нет,
    свод собирать не из чего); эталон есть, но противоречив — отказ.
    """
    missing: list[str] = []
    out: dict = {"sets": {}, "components": {}, "domain": {}}

    ref_v1v2 = load(root, REF_V1V2)
    ref_k1 = load(root, REF_K1)
    ref_k2 = load(root, REF_K2)
    peer_domain = load(root, REF_PEER_DOMAIN)
    peer_k2set = load(root, REF_PEER_K2SET)
    peer_s3v = load(root, REF_PEER_S3V)
    for name, data in (("S3h (v1,v1_domain,v2_domain)", ref_v1v2),
                       ("S3q (база K1)", ref_k1), ("S3t (база K2)", ref_k2),
                       ("S3m (домен рук)", peer_domain),
                       ("S3t (язык рук)", peer_k2set), ("S3v (язык по шагам)", peer_s3v)):
        if data is None:
            missing.append(f"нет эталона {name}")
    if missing:
        return out, missing, EXIT_NOT_VERIFIED

    def set_sha(data: dict, set_name: str) -> str | None:
        node = (data.get("datasets") or {}).get(set_name)
        return node.get("sha256") if isinstance(node, dict) else None

    #: Домен: базы и хеши — из S3h (там оба набора сняты тем же прибором, что и
    #: здесь). Роль набора («решающий» / «исторический») названа, а не выведена:
    #: по ней свод решает, какие числа идут в вердикт, а какие — в справку.
    for set_name, role in ((DOMAIN_PRIMARY, "решающий"), (DOMAIN_ALT, "исторический")):
        node = ref_v1v2["ppl"].get(set_name)
        if not isinstance(node, dict) or "ppl" not in node:
            return out, [f"в {REF_V1V2} нет базы набора {set_name}"], EXIT_NOT_VERIFIED
        out["domain"][set_name] = {
            "set": set_name,
            "role": role,
            "base_ppl": float(node["ppl"]),
            "docs": node.get("docs"),
            "tokens": node.get("tokens"),
            "evidence": REF_V1V2,
            "evidence_stage": "S3h",
            "set_sha256": set_sha(ref_v1v2, set_name),
            "ratio_reading": DOMAIN_RATIO_READING,
        }
        out["sets"][set_name] = {"sha256": set_sha(ref_v1v2, set_name),
                                 "role": SETS[set_name]}

    #: Язык: база и потолок каждой компоненты — от **её собственной** базы
    #: (ADR-027 п.2), значение потолка вычисляется и сверяется с записанным.
    comps = (
        ("v3_general", ref_k1, REF_K1, "S3q", ref_k1.get("baseline", {}).get("new_ceiling"), "K1"),
        ("k2_general", ref_k2, REF_K2, "S3t", ref_k2.get("component", {}).get("ceiling"), "K2"),
    )
    for set_name, data, rel, stage, recorded, comp in comps:
        node = data["ppl"].get(set_name)
        if not isinstance(node, dict) or "ppl" not in node:
            return out, [f"в {rel} нет базы набора {set_name}"], EXIT_NOT_VERIFIED
        base = float(node["ppl"])
        ceiling = CEILING_FACTOR * base
        entry = {
            "component": comp, "set": set_name, "base_ppl": base,
            "ceiling_ppl": ceiling, "ceiling_rule": f"{CEILING_FACTOR}× базы (ADR-022 п.3)",
            "ceiling_recorded_in_evidence": recorded,
            "evidence": rel, "evidence_stage": stage,
            "evidence_sha256": sha256_file(root / rel),
            "set_sha256": set_sha(data, set_name),
        }
        if recorded is not None:
            rel_delta = abs(ceiling - float(recorded)) / max(abs(float(recorded)), 1e-12)
            entry["ceiling_agrees_with_recorded"] = bool(rel_delta <= BASE_AGREEMENT_TOL)
            if not entry["ceiling_agrees_with_recorded"]:
                return out, [f"{comp}: вычисленный потолок {ceiling!r} не совпал с "
                             f"записанным {recorded!r} (Δ {rel_delta:.2e})"], EXIT_FAIL
        out["components"][comp] = entry
        out["sets"][set_name] = {"sha256": set_sha(data, set_name), "role": SETS[set_name]}

    #: Контрольное число прибора (v1_general) — исторический эталон S3h.
    out["sets"]["v1_general"] = {"sha256": set_sha(ref_v1v2, "v1_general"),
                                 "role": SETS["v1_general"]}

    #: Исторические эталоны по именам: с чем именно сверяется базовое состояние.
    out["historical"] = {
        "source": {"v1_general": REF_V1V2, "v1_domain": REF_V1V2, "v2_domain": REF_V1V2,
                   "v3_general": REF_K1, "k2_general": REF_K2},
        "stage": {"v1_general": "S3h", "v1_domain": "S3h", "v2_domain": "S3h",
                  "v3_general": "S3q", "k2_general": "S3t"},
        "ppl": {
            "v1_general": float(ref_v1v2["ppl"]["v1_general"]["ppl"]),
            "v1_domain": float(ref_v1v2["ppl"]["v1_domain"]["ppl"]),
            "v2_domain": float(ref_v1v2["ppl"]["v2_domain"]["ppl"]),
            "v3_general": float(ref_k1["ppl"]["v3_general"]["ppl"]),
            "k2_general": float(ref_k2["ppl"]["k2_general"]["ppl"]),
        },
    }
    out["peers"] = {
        "domain_final": {a["arm"]: {k: a.get(k) for k in ("ppl_all_sets",)}
                         for a in peer_domain["arms"]},
        "language_final": {a["arm"]: {k: a.get(k) for k in ("k1_ppl", "k2_ppl", "conjunction")}
                           for a in peer_k2set["arms"]["arms"]},
        "language_by_step": {f"{a['arm']}:{a['step']}": {"v1_general": a.get("v1_ppl"),
                                                         "v3_general": a.get("k1_ppl"),
                                                         "k2_general": a.get("k2_ppl")}
                             for a in peer_s3v["arms"] if a.get("step")},
    }
    return out, [], EXIT_OK


def read_purity(root: Path, set_name: str) -> tuple[dict | None, list[str], int]:
    """Гейт чистоты доменного набора: дословное пересечение — отказ, остальное — справка.

    Документное совпадение означает, что набор измеряет обучающий текст, и тогда
    отношение к базе перестаёт быть свойством модели (ADR-025 п.1) — это отказ.
    Ненулевые 12-граммы у доменного набора ожидаемы и гейтом не являются
    (карточка `gen-eval-v2-card.json → limits`): карточки набора и обучения —
    одного шаблона. Это **число**, а не суждение: примеры совпавших окон
    приводятся рядом.
    """
    rel = PURITY[set_name]
    if not (root / rel).is_file():
        return None, [f"{set_name}: нет отчёта гейта чистоты {rel}"], EXIT_NOT_VERIFIED
    try:
        data = load(root, rel)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, [f"{set_name}: отчёт гейта {rel} не читается "
                      f"({type(exc).__name__})"], EXIT_FAIL
    sets = (data.get("set") or {})
    corpora = data.get("corpora") or {}
    #: Пустой блок корпусов означал бы «пересечения нет» без единой проверки —
    #: ноль из отсутствия данных неотличим от нуля по существу. Это отказ.
    if not corpora:
        return None, [f"{set_name}: в отчёте гейта {rel} нет ни одного корпуса — "
                      f"чистота не проверена"], EXIT_FAIL
    doc_overlap = sum(int((corpora.get(r) or {}).get("overlap_docs") or 0)
                      for r in corpora)
    ngram_overlap = sum(int((corpora.get(r) or {}).get("overlap_ngram_windows") or 0)
                        for r in corpora)
    examples: list[str] = []
    for r in sorted(corpora):
        for w in (corpora[r].get("window_examples") or [])[:5]:
            if w not in examples:
                examples.append(w)
    block = {
        "set": data.get("set", {}).get("path"),
        "sha256": sets.get("sha256"),
        "docs": sets.get("docs"),
        "set_windows": (data.get("instrument") or {}).get("set_windows"),
        "corpora": {r: {"path": corpora[r].get("path"), "sha256": corpora[r].get("sha256"),
                        "overlap_docs": corpora[r].get("overlap_docs"),
                        "overlap_ngram_windows": corpora[r].get("overlap_ngram_windows")}
                    for r in sorted(corpora)},
        "overlap_docs_total": doc_overlap,
        "overlap_ngram_total": ngram_overlap,
        "ngram_share": (round(ngram_overlap / (data.get("instrument") or {})
                              .get("set_windows", 1), 5)
                        if (data.get("instrument") or {}).get("set_windows") else None),
        "window_examples": examples[:5],
        "tool_verdict": data.get("verdict"),
        "verdict": "clean" if doc_overlap == 0 else "leak",
        "gate_note": ("гейт ADR-025 п.1 сформулирован для наборов `general_eval*`; "
                      "на доменный набор он здесь применён как проверка той "
                      "компоненты, которая делает число свойством модели "
                      "(дословный документ), а 12-граммы оставлены справкой — "
                      "карточка набора объявляет их не-гейтом"),
        "report": rel,
        "report_sha256": sha256_file(root / rel),
    }
    if doc_overlap > 0:
        return None, [f"{set_name}: {doc_overlap} документов набора дословно найдены в "
                      f"обучающем корпусе — числа набора не являются свойством модели "
                      f"(ADR-025 п.1)"], EXIT_FAIL
    return block, [], EXIT_OK


def read_arm_report(root: Path, arm: str, spec: dict, refs: dict,
                    stand: Path) -> tuple[dict | None, list[str], int]:
    """Отчёт руки + параметры прогона.

    Нет файла — NOT-VERIFIED (входа нет). Есть, но проба мерила не те наборы,
    рука в таблице перепутана или не хватает объявленной точки — **отказ**: числа
    есть, и именно поэтому их нельзя молчаливо сравнить с чужими.
    """
    path = root / ARMS_DIR / f"{arm}.json"
    if not path.is_file():
        return None, [f"{arm}: нет отчёта прибора {path.relative_to(root)}"], EXIT_NOT_VERIFIED
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        #: Проба пишет отчёт инкрементально (состояние за состоянием): файл,
        #: прочитанный во время записи, оборван. Это отказ с названной причиной,
        #: а не трассировка: молча достроить по обрывку нельзя.
        return None, [f"{arm}: отчёт прибора не читается ({type(exc).__name__}: {exc}) — "
                      f"проба ещё пишет или файл повреждён"], EXIT_FAIL
    problems: list[str] = []

    #: Наборы: прибор обязан был мерить те же файлы, что эталоны. Подмена набора
    #: сделала бы числа несопоставимыми по построению, а не «немного другими».
    for set_name, want in refs["sets"].items():
        node = rep["sets"].get(set_name)
        if node is None:
            problems.append(f"{arm}: проба не мерила {set_name}")
            continue
        if want["sha256"] not in (None, node["sha256"]):
            problems.append(f"{arm}: {set_name} подменён (sha256 {node['sha256'][:12]} ≠ "
                            f"эталонного)")
    if problems:
        return None, problems, EXIT_FAIL

    #: Параметры руки из её собственного прогона. Перепутать руки в таблице нельзя:
    #: у рук разный микс, и «25 % replay» на v12r50 означало бы другой опыт.
    params_path = stand / spec["run_dir"] / "calib_params.json"
    params = json.loads(params_path.read_text(encoding="utf-8")) if params_path.is_file() else {}
    for key, want in (("replay_share_pct", spec["replay_share_pct"]),
                      ("peak_lr_scale", spec["peak_lr_scale"])):
        got = params.get(key)
        if got is not None and float(got) != float(want):
            problems.append(f"{arm}: {key} в прогоне {got!r} ≠ ожидаемого {want!r} — "
                            f"рука в таблице перепутана")

    #: Объявленные точки: каждая обязана быть в отчёте, и каждая точка отчёта —
    #: быть объявленной. Лишняя точка означает, что свод читает не тот прогон.
    state_of = {500: "c500", 1000: "c1000", 1500: "c1500", 2000: "cfinal"}
    want_states = {state_of[s] for s in spec["steps"]}
    cpt_steps = params.get("cpt_steps")
    for state in sorted(want_states):
        if state not in rep["states"]:
            problems.append(f"{arm}: в отчёте нет состояния {state} (шаг объявлен)")
    got_ckpt_states = {n for n, v in rep["states"].items()
                       if (v.get("load") or {}).get("kind") == "ckpt"}
    extra = sorted(got_ckpt_states - want_states)
    if extra:
        problems.append(f"{arm}: в отчёте лишние состояния {extra} — свод читает не тот прогон")
    missed = sorted(want_states - got_ckpt_states)
    if missed and not spec["gap"]:
        problems.append(f"{arm}: нет состояний {missed}, и пропуск в таблице не назван")
    #: Финальная точка — это `cpt_steps` руки, а не «понятно, что 2000».
    if "cfinal" in want_states and not cpt_steps:
        problems.append(f"{arm}: шаг финального состояния неизвестен — нет cpt_steps в "
                        f"calib_params.json прогона")
    #: База обязана быть измерена до чекпойнтов и восстановлена после (вакуумная
    #: точка): иначе числа чекпойнтов снимались бы с осевших весов.
    for state in ("base", "base_untouched"):
        if state not in rep["states"]:
            problems.append(f"{arm}: проба не сняла {state} — восстановление весов "
                            f"не проверено")
    if "base" in rep["states"] and "base_untouched" in rep["states"]:
        #: Проба сама отказывает при расхождении этих состояний, но свод проверяет
        #: это и по числам: «отчёт получен» и «отчёт непротиворечив» — не одно и то же.
        for set_name, node in rep["states"]["base"]["sets"].items():
            got = (rep["states"]["base_untouched"]["sets"].get(set_name) or {}).get("ppl")
            want = node.get("ppl")
            if got is None or want is None:
                continue
            if abs(got - want) > BASE_AGREEMENT_TOL:
                problems.append(f"{arm}: base_untouched разошлась с base на {set_name} "
                                f"({got!r} против {want!r}) — веса не восстановлены")
    #: Отчёт, помеченный незавершённым, не является отчётом: проба пишет его
    #: инкрементально, и «все нужные состояния на месте» ещё не значит «проба дошла
    #: до конца и сверила себя».
    if rep.get("complete") is not True:
        problems.append(f"{arm}: проба не завершена (complete={rep.get('complete')!r}) — "
                        f"отчёт неполон")
    for check in rep.get("checks", []):
        if check.get("verdict") != "ok":
            problems.append(f"{arm}: собственная проверка пробы «{check.get('name')}» "
                            f"дала «{check.get('verdict')}»")
    if problems:
        return None, problems, EXIT_FAIL

    #: Подпись входного состояния: путь, хеш и объём чекпойнта. Хеш считается по
    #: файлу на сетевом диске (AD-4 — читаем по месту, копий не держим); нет файла
    #: (свод на синтетической фикстуре) — честная пометка «не проверено», вердикт
    #: на хеш не опирается.
    steps = {state_of[s]: s for s in spec["steps"]}
    checkpoints = []
    for state, step in sorted(steps.items(), key=lambda kv: kv[1]):
        load_info = rep["states"][state].get("load") or {}
        ck = load_info.get("checkpoint")
        node = {"state": state, "step": step, "path": ck, "sha256": None, "bytes": None,
                "resized_embeddings": load_info.get("resized_embeddings")}
        if ck and Path(ck).is_file():
            node["bytes"] = Path(ck).stat().st_size
            node["sha256"] = sha256_file(Path(ck))
        checkpoints.append(node)

    block = {
        "arm": arm,
        "kind": spec["kind"],
        "report": str(path.relative_to(root)),
        "report_sha256": sha256_file(path),
        "vary": spec["vary"],
        "corpus": spec["corpus"],
        "gap": spec["gap"],
        "replay_share_pct": params.get("replay_share_pct", spec["replay_share_pct"]),
        "peak_lr_scale": params.get("peak_lr_scale", spec["peak_lr_scale"]),
        "cpt_steps": cpt_steps,
        "seed": params.get("seed"),
        "attn": params.get("attn"),
        "run_dir": str(stand / spec["run_dir"]),
        "pipeline": spec["pipeline"],
        "pipeline_sha256": rep["instrument"]["pipeline_sha256"],
        "loader": rep["instrument"]["load_checkpoint"],
        "checkpoints": checkpoints,
        "states": rep["states"],
        "states_in_order": list(rep["states"]),
        "all_sets": rep["sets"],
        "probe_checks": rep.get("checks", []),
    }
    return block, [], EXIT_OK


def identity_check(arms: dict[str, dict], refs: dict) -> tuple[dict, list[str]]:
    """Тождество прибора: базовое состояние каждой руки против пяти эталонов.

    Проверка не «на слово»: числа берутся из файлов-эталонов, а не вписаны сюда.
    Сходятся все пять эталонов у **каждой** руки — прибор тот же и шкала та же;
    расхождение хотя бы одного — отказ, потому что тогда сравнивать нечем.
    """
    checks: dict[str, dict] = {}
    problems: list[str] = []
    worst = 0.0
    for arm, block in arms.items():
        for set_name, want in refs["historical"]["ppl"].items():
            got = (block["states"]["base"]["sets"].get(set_name) or {}).get("ppl")
            if got is None:
                problems.append(f"{arm}: состояние base не мерило {set_name}")
                continue
            delta = abs(got - want)
            worst = max(worst, delta)
            key = f"{arm}:{set_name}"
            checks[key] = {
                "set": set_name,
                "ppl_measured": got,
                "ppl_historical": want,
                "historical_evidence": refs["historical"]["source"][set_name],
                "historical_stage": refs["historical"]["stage"][set_name],
                "abs_delta": delta,
                "within_tolerance": bool(delta <= IDENTITY_TOLERANCE),
            }
            if delta > IDENTITY_TOLERANCE:
                problems.append(f"{arm}:{set_name}: база {got!r} разошлась с исторической "
                                f"{want!r} (Δ {delta:.3e} > {IDENTITY_TOLERANCE})")
    out = {
        "what": ("пять исторических чисел сняты в этих же прогонах у каждой руки: одно "
                 "совпадение объяснялось бы совпадением набора, пять — только тем, что "
                 "измеряет та же функция"),
        "tolerance_abs": IDENTITY_TOLERANCE,
        "checks": checks,
        "worst_abs_delta": worst,
        "reproduced": not problems,
    }
    return out, problems


def cross_run_reproduction(arms: dict[str, dict], refs: dict) -> tuple[dict, list[str], int]:
    """Чужие числа того же прибора воспроизведены — сверка, а не второй источник.

    Домен финала калибровочных рук сверяется с S3m, язык — с S3t/S3q, язык по
    шагам контрольных рук — с S3v. Это проверяет **две** вещи сразу: что прогон
    детерминирован между запусками и что свод читает те же состояния, о которых
    говорят прежние дельты. Эталона нет — NOT-VERIFIED, расхождение — отказ.
    """
    out: dict = {"checks": {}, "worst_abs_delta": 0.0}
    problems: list[str] = []
    worst = 0.0
    #: Сколько сравнений **ожидается**: каждая рука, о которой чужой свод что-то
    #: говорит, обязана быть сверена. Считается заранее, чтобы неполнота сверки
    #: была отказом, а не тишиной.
    expected = 0

    def cmp(key: str, got, want, what: str) -> None:
        nonlocal worst
        if got is None or want is None:
            problems.append(f"{key}: сверка не состоялась — нет числа с одной из сторон "
                            f"(своё {got!r}, чужое {want!r})")
            return
        delta = abs(float(got) - float(want))
        worst = max(worst, delta)
        out["checks"][key] = {"what": what, "ppl_measured": got, "ppl_peer": want,
                              "abs_delta": delta,
                              "within_tolerance": bool(delta <= IDENTITY_TOLERANCE)}
        if delta > IDENTITY_TOLERANCE:
            problems.append(f"{key}: {got!r} против чужого {want!r} (Δ {delta:.3e})")

    for arm, block in arms.items():
        pn = peer_name(arm)
        if pn in refs["peers"]["domain_final"]:
            node = refs["peers"]["domain_final"][pn].get("ppl_all_sets") or {}
            final = block["states"].get("cfinal", {}).get("sets", {})
            for set_name in (DOMAIN_PRIMARY, DOMAIN_ALT):
                if set_name in node:
                    expected += 1
                    cmp(f"{arm}:{set_name}", (final.get(set_name) or {}).get("ppl"),
                        node[set_name], "домен финала калибровочной руки — сверка с S3m")
        if pn in refs["peers"]["language_final"]:
            node = refs["peers"]["language_final"][pn]
            final = block["states"].get("cfinal", {}).get("sets", {})
            for set_name, key in (("v3_general", "k1_ppl"), ("k2_general", "k2_ppl")):
                expected += 1
                cmp(f"{arm}:{set_name}", (final.get(set_name) or {}).get("ppl"), node.get(key),
                    "язык финала калибровочной руки — сверка с S3t")
        for state, step in STATE_STEPS.items():
            if state not in block["states"]:
                continue
            step_num = int(block["cpt_steps"] or 0) if step is None else step
            node = refs["peers"]["language_by_step"].get(f"{pn}:{step_num}")
            if not node:
                continue
            for set_name in ("v1_general", "v3_general", "k2_general"):
                expected += 1
                cmp(f"{arm}:{state}:{set_name}",
                    (block["states"][state]["sets"].get(set_name) or {}).get("ppl"),
                    node.get(set_name), "язык по шагам контрольной руки — сверка с S3v")

    #: Неполнота — отказ. Именно здесь ловится расхождение имён: если бы имена не
    #: переводились, сверка не дала бы ни одного сравнения и молча «прошла».
    if len(out["checks"]) < expected:
        problems.append(f"сверка с чужими сводами неполна: сравнений "
                        f"{len(out['checks'])} из {expected}")

    out["expected_checks"] = expected
    out["worst_abs_delta"] = worst
    out["ok"] = not problems
    out["why"] = ("сверяются чужие отчёты того же прибора: домен — S3m, язык — S3t/S3q, "
                  "язык по шагам — S3v; расхождение означало бы, что прогон не "
                  "детерминирован или свод читает другие состояния")
    return out, problems, (EXIT_FAIL if problems else EXIT_OK)


def trend_profile(rows: list[dict], key: str) -> dict:
    """Профиль набора по шагам руки: первая/последняя точка, экстремумы и тренд.

    Тренд — по краям профиля (первая → последняя), а не по максимуму: «ступенька»
    внутри интервала видна в экстремумах, и подменять ею направление нельзя.
    """
    pts = [(r["step"], r[key]) for r in sorted(rows, key=lambda r: r["step"])
           if r.get(key) is not None]
    if len(pts) < 2:
        return {"points": len(pts), "trend": "неизвестен"}
    first_step, first = pts[0]
    last_step, last = pts[-1]
    rel = (last / first - 1.0) if first else None
    if rel is None:
        trend = "неизвестен"
    elif abs(rel) <= TREND_BAND:
        trend = "стоит"
    elif rel < 0:
        trend = "падает"
    else:
        trend = "растёт"
    peak_step, peak = max(pts, key=lambda p: p[1])
    low_step, low = min(pts, key=lambda p: p[1])
    return {
        "points": len(pts),
        "first": {"step": first_step, "ppl": first},
        "last": {"step": last_step, "ppl": last},
        "rel_change": rel,
        "trend": trend,
        "trend_band": TREND_BAND,
        "max": {"step": peak_step, "ppl": peak},
        "min": {"step": low_step, "ppl": low},
    }


def build_matrix(arms: dict[str, dict], refs: dict) -> tuple[list[dict], list[str]]:
    """Матрица «рука × шаг»: домен (два набора) рядом с языком (две компоненты).

    Обе метрики снимаются в **одном** прогоне на одном состоянии — иначе «домен ↓
    на X, язык ↑ на Y» складывалось бы из двух приборов, и цена считалась бы по
    разным шкалам.
    """
    rows: list[dict] = []
    problems: list[str] = []
    k1 = refs["components"]["K1"]
    k2 = refs["components"]["K2"]
    dom = refs["domain"]

    #: Порядок точек: вакуумная база (шаг 0) и затем чекпойнты руки. База входит в
    #: таблицу строкой, а не только как делитель: без неё «отношение» нечем
    #: прочитать глазами, и отношение 1.0 пришлось бы принимать на веру.
    for arm, block in arms.items():
        for state in ("base", "c500", "c1000", "c1500", "cfinal"):
            if state not in block["states"]:
                continue
            state_sets = block["states"][state]["sets"]
            step = STATE_STEPS.get(state)
            step_num = 0 if state == "base" else (
                int(block["cpt_steps"]) if step is None else int(step))
            row: dict = {
                "arm": arm,
                "kind": block["kind"],
                "state": state,
                "step": step_num,
                "is_base": state == "base",
                #: Факторы руки — в каждой строке, а не только в шапке: строка
                #: таблицы должна читаться без оглядки на другой блок.
                "replay_share_pct": block["replay_share_pct"],
                "peak_lr_scale": block["peak_lr_scale"],
                "corpus": block["corpus"],
                "checkpoint": (block["states"][state].get("load") or {}).get("checkpoint"),
                "language_source": "этот прогон: v3_general (K1), k2_general (K2)",
            }

            for set_name, meta in dom.items():
                ppl = (state_sets.get(set_name) or {}).get("ppl")
                row[f"{set_name}_ppl"] = ppl
                row[f"{set_name}_base"] = meta["base_ppl"]
                row[f"{set_name}_ratio"] = (ppl / meta["base_ppl"]) if ppl else None
                #: На вакуумной базе «выучен/не выучен» не определено: отношение
                #: там равно 1.0 по построению, и `false` читалось бы как «домен
                #: испорчен» вместо «точка отсчёта».
                row[f"{set_name}_learned"] = (
                    None if state == "base"
                    else (bool(ppl < meta["base_ppl"]) if ppl is not None else None))
            for comp, node in (("k1", k1), ("k2", k2)):
                ppl = (state_sets.get(node["set"]) or {}).get("ppl")
                row[f"{comp}_ppl"] = ppl
                row[f"{comp}_base"] = node["base_ppl"]
                row[f"{comp}_ceiling"] = node["ceiling_ppl"]
                row[f"{comp}_ratio"] = (ppl / node["base_ppl"]) if ppl else None
                row[f"{comp}_pass"] = (bool(ppl <= node["ceiling_ppl"])
                                       if ppl is not None else None)
            row["conjunction"] = (bool(row["k1_pass"] and row["k2_pass"])
                                  if row["k1_pass"] is not None else None)
            #: Парная сводка «домен против языка» на одной точке: домен по решающему
            #: набору, язык — обеими компонентами, без усреднения (ADR-027 п.2).
            row["pairing"] = {
                "domain_ratio": row[f"{DOMAIN_PRIMARY}_ratio"],
                "domain_change_pct": (round(100 * (row[f"{DOMAIN_PRIMARY}_ratio"] - 1), 3)
                                      if row[f"{DOMAIN_PRIMARY}_ratio"] is not None else None),
                "language_k1_ratio": row["k1_ratio"],
                "language_k2_ratio": row["k2_ratio"],
                "language_change_pct": {
                    "k1": (round(100 * (row["k1_ratio"] - 1), 3)
                           if row["k1_ratio"] is not None else None),
                    "k2": (round(100 * (row["k2_ratio"] - 1), 3)
                           if row["k2_ratio"] is not None else None)},
                "reading": ("домен: <0 % — выучен, >0 % — испорчен; язык: чем больше "
                            "+ %, тем сильнее разрушен (потолок каждой компоненты — "
                            "2× её базы)"),
            }
            rows.append(row)
    if not rows:
        problems.append("в отчётах нет ни одной измеренной точки")
    return rows, problems


def curves(rows: list[dict]) -> dict:
    """Профиль по каждой руке — отдельно по домену и по каждой компоненте языка.

    База (шаг 0) в профиль не входит: тренд «падает/стоит/растёт» читается по
    траектории обучения, а не по разнице с нетронутыми весами — разница с базой
    уже стоит отдельным столбцом (`ratio`).
    """
    out: dict = {}
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm and not r["is_base"]]
        if not arm_rows:
            continue
        out[arm] = {
            "domain_deciding": trend_profile(arm_rows, f"{DOMAIN_PRIMARY}_ppl"),
            "domain_historical": trend_profile(arm_rows, f"{DOMAIN_ALT}_ppl"),
            "k1": trend_profile(arm_rows, "k1_ppl"),
            "k2": trend_profile(arm_rows, "k2_ppl"),
            "steps": [r["step"] for r in sorted(arm_rows, key=lambda r: r["step"])],
        }
    return out


def lr_pair_comparison(rows: list[dict], refs: dict) -> dict:
    """Пара, где варьируется **только** пик LR: C2 (×0.035) против `25-0.35` (×0.35).

    Один корпус (`v12r`), один сид, одна упаковка, одна копия пайплайна. Цена
    низкого LR считается по обеим метрикам сразу: домен — отношением отношений,
    язык — тем же отношением по каждой компоненте отдельно.
    """
    low_arm, ref_arm = LR_PAIR
    #: Шаг 0 (нетронутая база) в сравнение не берётся: там отношения обеих рук
    #: равны 1.0 по построению, и строка «0» была бы шумом, а не точкой обмена.
    low = {r["step"]: r for r in rows if r["arm"] == low_arm and not r["is_base"]}
    ref = {r["step"]: r for r in rows if r["arm"] == ref_arm and not r["is_base"]}
    steps = sorted(set(low) & set(ref))
    per_step = []
    for step in steps:
        entry = {"step": step}
        for label, key in (("domain", f"{DOMAIN_PRIMARY}_ratio"),
                           ("domain_historical", f"{DOMAIN_ALT}_ratio"),
                           ("k1", "k1_ratio"), ("k2", "k2_ratio")):
            a, b = low[step].get(key), ref[step].get(key)
            entry[label] = {"low_lr": a, "lr_035": b,
                            "ratio_of_ratios": (a / b) if (a and b) else None}
        per_step.append(entry)
    final = per_step[-1] if per_step else {}
    out = {
        "pair": {"low_lr_arm": low_arm, "reference_arm": ref_arm},
        "what": ("один корпус `v12r` (75/25), один сид, одна упаковка, тождественные "
                 "копии пайплайна — варьируется ровно пик LR (×0.035 против ×0.35)"),
        "per_step": per_step,
        "final": final,
        "reading": ("`ratio_of_ratios` — отношение «низкий LR : ×0.35» по каждому "
                    "столбцу. По домену > 1 означает, что при низком LR домен взят "
                    "**хуже** (PPL ближе к базовой); по языку < 1 означает, что язык "
                    "разрушен **слабее**. Направления чтения противоположны, поэтому "
                    "в вердикте язык перевёрнут в `language_preserved_better_by` — "
                    "там > 1 всегда «лучше для решения»"),
    }
    return out


def build_verdict(rows: list[dict], curves_block: dict, pair: dict,
                  refs: dict) -> dict:
    """Вердикт по пункту 3 задания: выучивает ли LR×0.035 домен.

    Критерий бинарный и назван **до** чисел (AD-11), чтобы ответ не подгонялся под
    результат:

    * D1 «домен выучен» — на решающем наборе отношение к базе < 1.0 в финале;
    * D2 «сопоставимо с ×0.35» — отношение низкого LR не хуже, чем у референса
      `25-0.35`, более чем в ×2 (полоса — уже принятая в контуре шкала ADR-022 п.3).

    Ответ: «хуже» / «сопоставимо» / «лучше» — по сравнению с референсом, плюс
    выучен ли домен вовсе. Обе метрики приводятся рядом: вердикт по языку без
    домена (и наоборот) не выносится.
    """
    low_arm, ref_arm = LR_PAIR
    dom = refs["domain"][DOMAIN_PRIMARY]

    def final_row(arm: str) -> dict | None:
        rs = [r for r in rows if r["arm"] == arm]
        return max(rs, key=lambda r: r["step"]) if rs else None

    low_final, ref_final = final_row(low_arm), final_row(ref_arm)
    if low_final is None or ref_final is None:
        return {"answer": None, "why": ["нет финальных точек пары — вердикт не считается"]}

    r_low = low_final[f"{DOMAIN_PRIMARY}_ratio"]
    r_ref = ref_final[f"{DOMAIN_PRIMARY}_ratio"]
    if r_low is None or r_ref is None:
        return {"answer": None, "why": ["домен пары не измерен — вердикт не считается"]}
    ratio_of_ratios = r_low / r_ref

    learned_low = bool(r_low < 1.0)
    learned_ref = bool(r_ref < 1.0)
    if ratio_of_ratios > COMPARABILITY_FACTOR:
        answer = "хуже"
    elif ratio_of_ratios < 1.0 / COMPARABILITY_FACTOR:
        answer = "лучше"
    else:
        answer = "сопоставимо"
    comparable = answer != "хуже"

    basis = {
        "domain_deciding_set": DOMAIN_PRIMARY,
        "base_ppl": dom["base_ppl"],
        "low_lr": {"arm": low_arm, "peak_lr_scale": low_final["peak_lr_scale"],
                   "ppl": low_final[f"{DOMAIN_PRIMARY}_ppl"], "ratio": r_low,
                   "learned": learned_low},
        "reference": {"arm": ref_arm, "peak_lr_scale": ref_final["peak_lr_scale"],
                      "ppl": ref_final[f"{DOMAIN_PRIMARY}_ppl"], "ratio": r_ref,
                      "learned": learned_ref},
        "ratio_of_ratios": ratio_of_ratios,
        "comparability_factor": COMPARABILITY_FACTOR,
        "comparability_basis": ("полоса ×2 — та же шкала, которой контур уже "
                                "пользуется для деградации (ADR-022 п.3); новое "
                                "число под этот вопрос не вводится"),
        "language_at_same_step": {
            "low_lr": {"k1_ratio": low_final["k1_ratio"], "k2_ratio": low_final["k2_ratio"],
                       "conjunction": low_final["conjunction"]},
            "reference": {"k1_ratio": ref_final["k1_ratio"],
                          "k2_ratio": ref_final["k2_ratio"],
                          "conjunction": ref_final["conjunction"]},
            "ratio_of_ratios": {"k1": (low_final["k1_ratio"] / ref_final["k1_ratio"]
                                       if low_final["k1_ratio"] and ref_final["k1_ratio"]
                                       else None),
                                "k2": (low_final["k2_ratio"] / ref_final["k2_ratio"]
                                       if low_final["k2_ratio"] and ref_final["k2_ratio"]
                                       else None)},
        },
    }

    why = [
        f"D1 «домен выучен»: у {low_arm} отношение к базе {r_low:.4f} на "
        f"`{DOMAIN_PRIMARY}` — "
        + ("выучен" if learned_low else "домен **не** выучен (PPL не ниже базовой)"),
        f"D2 «сопоставимо»: отношение отношений {ratio_of_ratios:.4f} при полосе "
        f"×{COMPARABILITY_FACTOR} — {answer}",
        f"Обе метрики на одном шаге: язык у {low_arm} — K1 ×{low_final['k1_ratio']:.3f}, "
        f"K2 ×{low_final['k2_ratio']:.3f} против ×{ref_final['k1_ratio']:.3f} и "
        f"×{ref_final['k2_ratio']:.3f} у {ref_arm}; компоненты не усредняются "
        f"(ADR-027 п.2)",
    ]

    #: Названное число цены: во сколько раз меньше домена взято и во сколько раз
    #: чище сохранён язык. Это и есть «цена LR» — обе половины, а не одна.
    #: Язык перевёрнут (`×0.35 : низкий`), чтобы «> 1» означало «лучше» в обеих
    #: половинах: иначе два числа одного блока читались бы в разные стороны.
    lang_low = basis["language_at_same_step"]["low_lr"]
    lang_ref = basis["language_at_same_step"]["reference"]
    preserved = {comp: (lang_ref[f"{comp}_ratio"] / lang_low[f"{comp}_ratio"]
                        if lang_low[f"{comp}_ratio"] and lang_ref[f"{comp}_ratio"] else None)
                 for comp in ("k1", "k2")}
    price = {
        "domain_taken_less_by": ratio_of_ratios,
        "language_preserved_better_by": preserved,
        "reading": ("первое число > 1 = низкий LR взял меньше домена; второе > 1 = "
                    "низкий LR сохранил язык лучше. Обмен описан обеими половинами; "
                    "компоненты языка не усредняются (ADR-027 п.2)"),
    }

    if comparable:
        decision = ("низкий LR берёт домен в пределах полосы "
                    f"×{COMPARABILITY_FACTOR} от ×0.35 (отношение отношений "
                    f"{ratio_of_ratios:.3f}) — снижение пика LR от домена не "
                    "отказывает, и вопрос «полный CPT на ×0.035» получает вторую "
                    "половину цены, а не только первую")
    else:
        decision = ("снижение пика LR до ×0.035 берёт домен хуже, чем ×0.35, более "
                    f"чем в ×{COMPARABILITY_FACTOR} (отношение отношений "
                    f"{ratio_of_ratios:.3f}) — это **обмен**, а не бесплатное "
                    "улучшение: цена названа обеими половинами (домен — "
                    "`domain_taken_less_by`, язык — `language_preserved_better_by`), "
                    "и выбор между «продолжать на ×0.35» и «перезапустить на "
                    "×0.035» остаётся за владельцем: свод называет цену, а не "
                    "выбирает")

    return {
        "answer": answer,
        "question": ("выучивает ли LR×0.035 (C2) домен — сопоставимо / хуже / лучше, "
                     "чем LR×0.35 при том же миксе (рука 25-0.35)"),
        "criterion": {
            "D1_domain_learned": "отношение PPL к базе < 1.0 в финале на решающем наборе",
            "D2_comparable": (f"отношение отношений ≤ ×{COMPARABILITY_FACTOR} "
                              f"(полоса ADR-022 п.3)"),
            "binary": "да, AD-11: ответ — одно из трёх слов, без средних",
        },
        "low_lr_domain_comparable": comparable,
        "domain_learned_by_low_lr": learned_low,
        "numbers": basis,
        "domain_trend_by_arm": {arm: node["domain_deciding"]["trend"]
                                for arm, node in sorted(curves_block.items())},
        "pair_by_step": pair["per_step"],
        "price_of_low_lr": price,
        "basis_for_decision": decision,
        "why": why,
        "reading_of_domain_ratio": {
            "strict": ("если «домен выучен» читать строго — отношение < 1: "
                       + ("выполнено" if learned_low else "не выполнено")),
            "with_base_band": ("если допускать полосу до базы: отношение ≤ 1.0 — "
                               + ("выполнено" if r_low <= 1.0 else "не выполнено")),
            "note": ("порог домена контур не задавал: ADR-022 п.3 задаёт потолок "
                     "деградации **языка**, а у домена опора — сама база"),
        },
        "not_averaged": ("K1 и K2 не усредняются; домен и язык не сводятся к одному "
                         "числу — вердикт по стадии остаётся конъюнкцией, а этот "
                         "замер называет вторую половину её цены"),
        "reading_notes": [
            "`curves[*].trend` — движение **внутри руки** (первая сохранённая точка → "
            "последняя); `arms[*].*_ratio` — отношение к **нетронутой базе**. Это "
            "разные вопросы, и они расходятся там, где важно: у руки C1 (в обучении "
            "домена нет) домен от своего пика падает, но отношение к базе остаётся "
            "больше единицы — то есть порча домена не откатывается, а продолжается "
            "с более низкого уровня",
            "`arms[*].pairing` сводит обе метрики на **одной** точке одного состояния: "
            "домен по решающему набору, язык — обеими компонентами без усреднения",
        ],
    }


def ensure_run_manifest(root: Path, arms: dict[str, dict]) -> dict:
    """Манифест AD-2 для каталога пробы: считается из отчётов, а не пишется руками."""
    sets: dict = {}
    for block in arms.values():
        sets.update(block["all_sets"])
    concat = "\n".join(f"{n}:{sets[n]['sha256']}" for n in sorted(sets))
    pipes = sorted({b["pipeline_sha256"] for b in arms.values()})
    #: Общий хеш — тот, что несут большинство рук; полный список рядом. Поле
    #: `pipeline_sha256` в манифесте одно, и подставлять в него «None» значило бы
    #: отчитываться пустотой там, где ответ есть.
    shared_pipe = max(pipes, key=lambda p: sum(1 for b in arms.values()
                                               if b["pipeline_sha256"] == p))
    manifest = {
        "dataset_path": "datasets/",
        "dataset_sha256": hashlib.sha256(concat.encode("utf-8")).hexdigest(),
        "dataset_note": ("проба меряет не корпус, а состояния чекпойнтов шести рук на "
                         "двух доменных и трёх наборах общего языка; dataset_sha256 — "
                         "sha256 по их содержимому, склеенному в порядке имён наборов, "
                         "datasets_extra — поимённо. Пути, начинающиеся с `calib/`, "
                         "отсчитываются от корня сетевого диска gb10-shared (там лежат "
                         "прогоны рук; симлинка в кейс нет — AD-4)"),
        "base_model_id": "Qwen/Qwen2.5-0.5B",
        "pipeline_path": ARMS["calib-25-0.35"]["pipeline"],
        "pipeline_version": ("laguna_pipeline_calib.py@" + ",".join(p[:12] for p in pipes)),
        "pipeline_sha256": shared_pipe,
        "pipeline_sha256_all": pipes,
        "pipeline_note": ("копии пайплайна тождественны у пяти рук и отличаются у "
                          "`calib-25-0.7` (ee8341d0…): та рука шла по более ранней "
                          "ревизии патча снимков; сам загрузчик чекпойнтов в обеих "
                          "ревизиях один и тот же (`_load_ckpt_with_resize`)"),
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
            "measurement": ("PPL состояний шести рук S3m/S3o: домен (v1_domain, "
                            "v2_domain) и обе компоненты общего языка (K1, K2) в одном "
                            "прогоне"),
            "device": "cuda", "dtype": "bfloat16", "batch": 4, "max_len": 1024,
            "tokenizer_setup": "pipeline (8 спецтокенов + resize)",
            "states_measured": ",".join(
                f"{arm}:{s}" for arm, b in sorted(arms.items())
                for s in b["states_in_order"]),
            "seed_note": ("проба не сэмплирует: PPL детерминирован, сид наследуется от "
                          "прогонов рук"),
        },
        "hyperparameters_source": (f"отчёты пробы {ARMS_DIR}/*.json "
                                   f"(instrument, sets)"),
    }
    path = root / ARMS_DIR / "run_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return {"path": str(path.relative_to(root)),
            "dataset_sha256": manifest["dataset_sha256"],
            "why": ("манифест AD-2 для каталога пробы (C-012): числа взяты из отчётов "
                    "прибора, а не вписаны")}


def domain_set_choice(refs: dict, purity: dict) -> dict:
    """Какой доменный набор решает и почему — с числами, а не с мотивировкой.

    Решает `v2_domain`: ×40 документов и ×48 токенов к `v1_domain`. Исторический
    `v1_domain` приводится рядом, потому что именно на нём снят `ppl_domain`
    GEN-EVAL (S3n): без него разрыв с прежними дельтами читался бы как смена
    прибора.
    """
    primary = refs["domain"][DOMAIN_PRIMARY]
    alt = refs["domain"][DOMAIN_ALT]
    return {
        "chosen": DOMAIN_PRIMARY,
        "chosen_path": purity[DOMAIN_PRIMARY]["set"] if DOMAIN_PRIMARY in purity else None,
        "chosen_sha256": primary["set_sha256"],
        "chosen_base_ppl": primary["base_ppl"],
        "chosen_docs": primary["docs"],
        "chosen_tokens": primary["tokens"],
        "why_chosen": [
            "различимость: 200 документов / 125 322 токена против 5 / 2 604 — "
            "×48 по токенам; ADR-015 назвал прибор `ppl_domain` слабым прямо по "
            "числу документов, а ADR-018 п.1 собрал v2, чтобы это снять",
            "роль: на 5 документах одного шаблона решение принималось бы внутри "
            "разброса прибора — ровно та ошибка, из-за которой v1 снят с решающей "
            "роли в мере общего языка (ADR-027 п.3)",
            "источник в контуре: набор собран из `cpt_corpus_full.txt` с отсевом "
            "документов, найденных в обучающем корпусе (карточка "
            "`data/gen-eval-v2-card.json`), и его база снята тем же прибором "
            "(`evidence/ppl-baseline-v1v2.json`, S3h)",
        ],
        "historical_alt": {
            "set": DOMAIN_ALT,
            "path": purity[DOMAIN_ALT]["set"] if DOMAIN_ALT in purity else None,
            "sha256": alt["set_sha256"],
            "base_ppl": alt["base_ppl"],
            "docs": alt["docs"],
            "tokens": alt["tokens"],
            "why_kept": ("непрерывность с S3n: `ppl_domain` GEN-EVAL считался именно по "
                         "этому набору, и расхождение с прежними дельтами без него "
                         "читалось бы как смена прибора"),
            "why_not_deciding": ("5 документов общего шаблона: ADR-015 п.1 — прибор "
                                 "слабый, ADR-018 п.1 — причина сборки v2"),
        },
        "both_sets_measured": True,
        "note": ("числа двух наборов не усредняются: решает один, второй отвечает за "
                 "непрерывность"),
    }


def build(root: Path, stand: Path, out: Path | None = None) -> tuple[dict, int]:
    refs, problems, rc = reference_numbers(root)
    if rc != EXIT_OK:
        print(("NOT-VERIFIED: " if rc == EXIT_NOT_VERIFIED else "ОТКАЗ: ")
              + "; ".join(problems), file=sys.stderr)
        return {}, rc

    purity: dict = {}
    for set_name in (DOMAIN_PRIMARY, DOMAIN_ALT):
        block, errs, rc = read_purity(root, set_name)
        if block is None:
            print(("NOT-VERIFIED: " if rc == EXIT_NOT_VERIFIED else "ОТКАЗ: ")
                  + "; ".join(errs), file=sys.stderr)
            return {}, rc
        purity[set_name] = block

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

    cross, problems, rc = cross_run_reproduction(arms, refs)
    if rc != EXIT_OK:
        print("ОТКАЗ: чужие числа того же прибора не воспроизведены: "
              + "; ".join(problems), file=sys.stderr)
        return {}, rc

    rows, problems = build_matrix(arms, refs)
    if problems:
        print("ОТКАЗ: " + "; ".join(problems), file=sys.stderr)
        return {}, EXIT_FAIL

    curves_block = curves(rows)
    pair = lr_pair_comparison(rows, refs)
    verdict = build_verdict(rows, curves_block, pair, refs)
    #: Вердикт без ответа — это не «свод собран»: пара рук, на которой решается
    #: вопрос, обязана иметь общие измеренные точки, иначе вопрос остаётся без
    #: ответа, а evidence выглядел бы полным.
    if not verdict.get("answer") or not pair.get("per_step"):
        print("ОТКАЗ: вердикт не посчитан: " + "; ".join(verdict.get("why", []) or
                                                         ["нет общих точек пары по LR"]),
              file=sys.stderr)
        return {}, EXIT_FAIL
    manifest = ensure_run_manifest(root, arms)
    pipes = sorted({b["pipeline_sha256"] for b in arms.values()})

    evidence = {
        "schema": "s3w-domain-by-arms/1",
        "stage": "S3w",
        "status": "complete",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": ("домен-метрика по всем рукам: цена низкого пика LR для домена "
                    "против цены по языку — вторая половина основания решения о "
                    "полном CPT (ADR-029 п.2)"),
        "question": ("выучивает ли LR×0.035 (C2) домен так же, как LR×0.35 при том же "
                     "миксе, и что это значит для выбора между «продолжать на ×0.35» "
                     "и «перезапустить на ×0.035»"),
        "adr": ["ADR-022 п.1/п.2/п.3 (LR и потолок деградации — шкала ×2)",
                "ADR-025 п.1/п.2/п.5 (чистота измерительного набора)",
                "ADR-027 п.1/п.2 (двухкомпонентная мера языка; компоненты не усредняются)",
                "ADR-029 п.2 (полный CPT продолжается на v12r при LR×0.35)",
                "ADR-015 п.1 / ADR-018 п.1 (слабость доменного набора из 5 документов)",
                "AD-11 (критерий бинарный)", "AD-12 (доказательства + коммит)"],
        "stand": {
            "where_measured": "локальная машина (RTX 4080 SUPER), НЕ стенд GB10",
            "why": ("стенд не задействован: на нём идёт полный CPT (ADR-029 п.2) — ни "
                    "остановить, ни занять его этот замер не имеет права; числа PPL "
                    "переносятся как свойства наборов и состояний, а не как замеры "
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
            "language_components": list(COMPONENTS),
            "max_len": 1024, "batch": 4, "dtype": "bfloat16", "device": "cuda",
            "tokenizer_setup": "pipeline (8 спецтокенов + resize), как в S3h/S3q/S3t/S3v",
            "base_weights": ("/home/user/gb10-shared/models-store/experiments/"
                             "kda-graft/models/qwen2.5-0.5b-base"),
            "env_shims": ENV_SHIMS,
        },
        "domain_set": domain_set_choice(refs, purity),
        "purity": purity,
        "domain_baselines": refs["domain"],
        "language_baselines": refs["components"],
        "sets": {name: {"sha256": node["sha256"], "role": node["role"]}
                 for name, node in refs["sets"].items()},
        "pipeline_identity": {
            "hashes": {arm: b["pipeline_sha256"] for arm, b in arms.items()},
            "distinct": pipes,
            "five_of_six_identical": len(pipes) == 2,
            "why": ("пять рук грузили тождественные копии пайплайна; `calib-25-0.7` — "
                    "более раннюю ревизию патча снимков (ee8341d0…), где отличается "
                    "только имя номерного чекпойнта; загрузчик `_load_ckpt_with_resize` "
                    "в обеих ревизиях один и тот же — это и делает числа сопоставимыми"),
        },
        "instrument_identity": identity,
        "cross_run_reproduction": cross,
        "arms": rows,
        "arms_params": {arm: {k: b[k] for k in
                              ("report", "report_sha256", "kind", "vary", "corpus", "gap",
                               "replay_share_pct", "peak_lr_scale", "cpt_steps", "seed",
                               "attn", "run_dir", "pipeline", "pipeline_sha256", "loader",
                               "checkpoints", "probe_checks")}
                        for arm, b in arms.items()},
        "curves": curves_block,
        "lr_pair_comparison": pair,
        "removed_checkpoint": {
            "arm": "calib-25-0.7",
            "missing_step": 500,
            "why": ARMS["calib-25-0.7"]["gap"],
            "consequence": ("профиль руки начинается с шага 1000; ступенька в (0, 500] "
                            "на ней не проверяется. Восстановить точку нельзя — это "
                            "снимок весов, а не пересчитываемое число"),
            "not_hidden": "пропуск объявлен в arms_params и в этом блоке, а не пропущен молча",
        },
        "verdict": verdict,
        "checks": [
            {"name": "instrument_identity", "verdict": "ok", "tolerance": IDENTITY_TOLERANCE,
             "detail": (f"пять исторических чисел (v1_general, v1_domain, v2_domain, "
                        f"v3_general, k2_general) воспроизведены базовым состоянием "
                        f"каждой из {len(arms)} рук; худшее абсолютное расхождение "
                        f"{identity['worst_abs_delta']:.3e}")},
            {"name": "cross_run_reproduction", "verdict": "ok",
             "tolerance": IDENTITY_TOLERANCE,
             "detail": (f"числа чекпойнтов совпали с чужими отчётами того же прибора "
                        f"(домен — S3m, язык — S3t/S3q, язык по шагам — S3v); худшее "
                        f"расхождение {cross['worst_abs_delta']:.3e}")},
            {"name": "base_agreement", "verdict": "ok", "tolerance": BASE_AGREEMENT_TOL,
             "detail": ("база в отчёте каждой руки совпала с базой эталона по всем пяти "
                        "наборам — отношения считаны от одной шкалы")},
            {"name": "domain_set_purity", "verdict": "ok",
             "detail": ("дословное пересечение доменных наборов с обучающими корпусами "
                        "нулевое (0 документов); ненулевые 12-граммы названы числом и "
                        "объяснены общим шаблоном карточек — см. purity и open_questions")},
            {"name": "run_manifest", "verdict": "ok",
             "detail": f"{manifest['path']}: {manifest['why']}"},
        ],
        "artifacts": [
            {"path": OUT, "role": "evidence замера (этот файл)", "sha256": None,
             "note": "хеш не приводится: файл пишется этим же вызовом"},
            {"path": "tools/assemble_s3w_evidence.py", "role": "свод",
             "sha256": sha256_file(Path(__file__).resolve())},
            {"path": "tools/calib_ppl_probe.py", "role": "прибор (не переписан)",
             "sha256": sha256_if_exists(root / "tools" / "calib_ppl_probe.py")},
            {"path": "tools/ppl_probe.py", "role": "методика замера",
             "sha256": sha256_if_exists(root / "tools" / "ppl_probe.py")},
            {"path": "tools/check_eval_set_purity.py", "role": "гейт чистоты доменных наборов",
             "sha256": sha256_if_exists(root / "tools" / "check_eval_set_purity.py")},
        ] + [{"path": block["report"], "role": "отчёт прибора по руке",
              "sha256": block["report_sha256"]} for block in arms.values()]
          + [{"path": node["report"], "role": f"гейт чистоты {name}",
              "sha256": node["report_sha256"]} for name, node in purity.items()]
          + [{"path": manifest["path"], "role": "манифест прогона (AD-2)",
              "sha256": sha256_if_exists(root / manifest["path"])}],
        "assumptions": [
            "Прибор и параметры — те же, что дали базы: tools/calib_ppl_probe.py, "
            "bfloat16 / batch 4 / max_len 1024, те же наборы; тождество доказано числом "
            "в этих же прогонах (пять эталонов), а не заявлено",
            "Решающий доменный набор — `v2_domain` (200 документов); `v1_domain` "
            "приводится как историческая шкала и решающим не является",
            "Отношение домена считается к базовой модели на **том же** наборе: сдвиг "
            "набора сокращается, абсолютный уровень оптимистичен из-за общего шаблона "
            "карточек (ограничение названо в карточке набора)",
            "Полоса сопоставимости ×2 взята из действующей шкалы контура (ADR-022 п.3), "
            "а не подобрана под ответ",
            "Полоса «тренд стоит» — 5 %, уже принятый в контуре допуск совпадения "
            "замера (`ppl_probe.py:REPRO_TOLERANCE_PCT`)",
            "Язык считается в этом же прогоне теми же наборами, что в S3q/S3t/S3v, и "
            "дополнительно сверяется с их отчётами — числа K1/K2 здесь не новый "
            "источник, а воспроизведение",
            "Домен и язык не сводятся к одному числу: вердикт стадии остаётся "
            "конъюнкцией K1 и K2 (ADR-027 п.2), а домен — вторая половина её цены",
        ],
        "open_questions": OPEN_QUESTIONS,
        "reproduction": [
            "# замер всех шести рук (локальная машина, стенд не задействован)",
            "SETS=v1_general,v1_domain,v2_domain,v3_general,k2_general",
            "for arm in calib-25-0.35 calib-25-0.7 calib-50-0.35 calib-50-0.7; do",
            "  D=/home/user/gb10-shared/calib/$arm-20260916-0820",
            "  P=runs/$arm-20260916-0820/laguna_pipeline_calib.py",
            "  python3 tools/calib_ppl_probe.py --pipeline $P \\",
            "    --state base=base \\",
            "    --state c500=ckpt:$D/checkpoints/calib_checkpoint_500.pt \\",
            "    --state c1000=ckpt:$D/checkpoints/calib_checkpoint_1000.pt \\",
            "    --state c1500=ckpt:$D/checkpoints/calib_checkpoint_1500.pt \\",
            "    --state cfinal=ckpt:$D/checkpoints/checkpoint_final.pt \\",
            "    --state base_untouched=base --sets $SETS \\",
            "    --dtype bfloat16 --device cuda --max-len 1024 --batch 4 \\",
            "    --out runs/s3w-domain-20260916/$arm.json",
            "done",
            "# у `calib-25-0.7` нет точки 500 (ретенция старой ревизии патча) и другие",
            "# имена файлов: checkpoint_1000.pt / checkpoint_1500.pt / checkpoint_final.pt",
            "# контрольные руки — то же, но пайплайн берётся из прогона на сетевом диске:",
            "#   --pipeline /home/user/gb10-shared/calib/ctrl-<рука>-20260916-1632/laguna_pipeline_calib.py",
            "#   --state c500=ckpt:$D/checkpoints/calib_checkpoint_500.pt   (и далее как выше)",
            "# гейт чистоты доменных наборов (обязателен до применения шкалы)",
            "python3 tools/check_eval_set_purity.py --set datasets/domain_eval_v2.txt \\",
            "        --report evidence/s3w-purity-v2domain.json",
            "python3 tools/check_eval_set_purity.py --set datasets/domain_eval.txt \\",
            "        --report evidence/s3w-purity-v1domain.json",
            "# свод",
            "python3 tools/assemble_s3w_evidence.py",
        ],
        "rollback": ("откат — git reset --hard HEAD && git clean -fd; чекпойнты рук на "
                     "gb10-shared не удаляются: это исходные данные решения, а не "
                     "артефакт этого свода. Стенд не задействован — полный CPT на нём "
                     "не останавливался и не трогался"),
    }

    out_path = out if out else root / OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"тождество прибора: худшее расхождение {identity['worst_abs_delta']:.3e} "
          f"(порог {IDENTITY_TOLERANCE})")
    print(f"сверка с чужими отчётами: худшее расхождение {cross['worst_abs_delta']:.3e}")
    print(f"вердикт: {verdict.get('answer')} — {verdict.get('basis_for_decision', '')[:120]}")
    print(f"отчёт: {out_path}")
    return evidence, EXIT_OK


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="S3w: свод домен-метрики по всем рукам (домен против языка)")
    ap.add_argument("--case-root", default=None,
                    help="корень кейса (отчёты, эталоны, наборы); по умолчанию — над tools/")
    ap.add_argument("--stand-calib", default="/home/user/gb10-shared/calib",
                    help="каталог прогонов рук на сетевом диске")
    ap.add_argument("--out", default=None, help=f"куда записать evidence (по умолчанию {OUT})")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    root = Path(args.case_root).resolve() if args.case_root else CASE_ROOT
    #: `--out` — путь как задан (относительный считается от текущего каталога),
    #: иначе — файл evidence в корне кейса. Тот же порядок, что в сводах S3q…S3v.
    out = Path(args.out) if args.out else None
    _, rc = build(root, Path(args.stand_calib), out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
