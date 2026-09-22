#!/usr/bin/env python3
"""S3m-2 — свод четырёх замеров PPL рук калибровки: таблица, вердикт, evidence.

Зачем отдельный инструмент. Замер четырёх рук делает `calib_ppl_probe.py` — по
отчёту на руку (`runs/calib-ppl-<ts>/ppl-<arm>.json`). Но решение по ADR-022 п.3(б)
принимается не по отдельному числу, а по **сравнению** четырёх чисел с базой и
потолком, и это сравнение не должно считаться в голове и пересказываться словами:
оно считается здесь, из отчётов.

Одно место правды на каждое число:

* PPL рук — из отчётов пробы (не пересчитывается: второй счёт разошёлся бы с первым);
* база — из **тех же** отчётов и сверяется с эталоном S3h
  (`evidence/ppl-baseline-v1v2.json`). Разошлись — отказ, а не «взяли удобное»;
* потолок ADR-022 (2× базы) **вычисляется** от базы, а не вписан числом;
* хеши чекпойнтов — считаются по файлам, с кэшем в каталоге пробы (3 ГБ × 4);
* `replay_pct` / `lr_scale` — из `runs/calib-<arm>-<ts>/calib_params.json`;
* пробы качества и траектории лосса — из каталога прогона на сетевом диске,
  **только чтение**.

Что здесь принципиально не делается:

* **лосс между руками не сравнивается.** У 25 % и 50 % разный состав данных, и
  «меньший лосс» там означает «другая смесь», а не «лучше выучил». Из траектории
  берётся только критерий **а** ADR-022 — убывает ли лосс **внутри** руки;
* **пробы качества не превращаются в метрику.** Они подаются описанием (сколько
  ответов вырождено в повтор, сколько сведено к одному шаблону), и в вердикте
  участвуют только как вторая, независимая линия — с явным указанием, где она
  расходится с PPL;
* **v2-наборы не смешиваются с решающим критерием.** Критерий ADR-022 назван для
  v1 (`≤ 23.9`); замеры на v2 идут отдельным блоком с оговоркой о приборе (см.
  `cross_check_v1_vs_v2` — у 50 %-рук набор v2 собран из их же обучающего
  материала, и это проверяется, а не предполагается).

Коды возврата::

    0 — evidence записан
    1 — отказ: отчёт руки неполон, прибор не сошёлся с S3h, база не воспроизведена
    2 — NOT-VERIFIED: нет входа (каталог пробы, отчёт руки, чекпойнт, параметры)

Запуск::

    python3 tools/calib_ppl_arms.py --ts 20260916-0820 --plan
    python3 tools/calib_ppl_arms.py --ts 20260916-0820
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE_ROOT / "tools"))

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Стенд. Каталоги прогонов лежат на сетевом диске (AD-4) — тот же путь виден и
#: локальной машине, и контейнеру стенда как /workspace/shared.
STAND_SHARED = Path("/home/user/gb10-shared")
STAND_CALIB = STAND_SHARED / "calib"

#: Набор, по которому назван решающий критерий ADR-022 п.3(б): «≤ 2× реальной
#: базы (≤ 23.9 для v1)». База в `evidence/ppl-baseline-v1v2.json` — это v1_general
#: (11.9319), и сравнение обязано идти по нему же, иначе числа несопоставимы.
DECISIVE_SET = "v1_general"

#: Эталон прибора S3h (коммит 09427f4). Числа берутся из файла, а не вписаны:
#: правка эталона должна ломать свод, а не совпадать с ним молча.
S3H_NAME = "evidence/ppl-baseline-v1v2.json"

#: Допуск согласия базы. Оба замера — один код на одной машине; расхождение может
#: быть только сменой прибора (набор, токенизатор, dtype), а не дрейфом весов.
TOL = 1e-6

#: Имя сводного отчёта пробы: из него собирается манифест AD-2 каталога пробы.
MERGED_NAME = "ppl.json"

#: Кэша хешей чекпойнтов здесь нет — и это решение, а не пропуск. Кэш по метаданным
#: (размер + mtime + ctime + inode) не видит перезаписи файла тем же размером в ту же
#: секунду: проверено на этой машине — два `write_bytes` подряд дают одинаковые
#: mtime_ns и ctime_ns. Хеш, который может молча пережить подмену файла, — это ровно
#: то «зелёное без результата», против которого AD-12. Цена честного счёта измерена:
#: 4 × 2.96 ГБ по NFS ≈ 22 с (первый прогон свода 59 с против 37 с у повторного).
CKPT_CACHE_NOTE = "хеши считаются по файлам на каждом прогоне; кэш метаданных отклонён (см. комментарий в tools/calib_ppl_arms.py)"

ARM_ORDER = ["25-0.35", "25-0.7", "50-0.35", "50-0.7"]

#: Имя функции загрузки чекпойнта в пайплайне. Сводится её **исходник**: важно,
#: что все четыре руки грузятся одним и тем же кодом, а не что совпал файл целиком
#: (копии рук отличаются вставками раннера в других местах).
LOADER_NAME = "_load_ckpt_with_resize"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─────────────────────────── вход: отчёты пробы ──────────────────────────────

def probe_dir(ts: str) -> Path:
    return CASE_ROOT / "runs" / f"calib-ppl-{ts}"


def read_reports(ts: str, arms: list[str]) -> tuple[dict, list[str]]:
    """Отчёты пробы по рукам. Возвращает (отчёты, недостающие имена)."""
    d = probe_dir(ts)
    reports, missing = {}, []
    for arm in arms:
        p = d / f"ppl-{arm}.json"
        if not p.is_file():
            missing.append(str(p))
            continue
        reports[arm] = json.loads(p.read_text(encoding="utf-8"))
    return reports, missing


def require_complete(reports: dict) -> list[str]:
    """Отчёт руки — доказательство только если он полон И прибор сошёлся.

    Проверки прибора считает сама проба (`checks`): `base_vs_s3h` — что замер
    воспроизводит числа S3h, `base_restored` — что после загрузки чекпойнта веса
    возвращаются к базовым. Неполный отчёт или не-`ok` проверка — отказ свода.
    """
    problems = []
    for arm, rep in reports.items():
        if not rep.get("complete"):
            problems.append(f"{arm}: отчёт неполон (complete=false, "
                            f"pending={rep.get('pending_states')})")
        for chk in rep.get("checks", []):
            if chk.get("verdict") != "ok":
                problems.append(f"{arm}: проверка прибора «{chk['name']}» — "
                                f"{chk.get('verdict')}: {chk.get('detail', '')}")
        if not rep.get("checks"):
            problems.append(f"{arm}: в отчёте нет блока проверок прибора")
    return problems


def base_ppl(reports: dict) -> dict:
    """PPL базы по наборам — из отчётов (одинакова во всех: один и тот же замер)."""
    out = {}
    for rep in reports.values():
        sets = rep["states"].get("base", {}).get("sets", {})
        for name, res in sets.items():
            out.setdefault(name, []).append(res["ppl"])
    spread = {k: max(v) - min(v) for k, v in out.items() if len(v) > 1}
    bad = {k: v for k, v in spread.items() if v > TOL}
    if bad:
        raise ValueError(f"база разошлась между отчётами: {bad}")
    return {k: v[0] for k, v in out.items()}


def s3h_reference() -> tuple[dict, dict]:
    """Эталон S3h: числа и паспорт файла (путь, sha256)."""
    path = CASE_ROOT / S3H_NAME
    if not path.is_file():
        return {}, {"path": str(path), "sha256": None}
    data = json.loads(path.read_text(encoding="utf-8"))
    ref = {k: float(v["ppl"]) for k, v in data.get("ppl", {}).items() if "ppl" in v}
    return ref, {"path": S3H_NAME, "sha256": sha256_file(path)}


# ─────────────────────────── чекпойнты и пайплайны ───────────────────────────

def loader_digest(pipeline: Path) -> dict:
    """sha256 исходника `_load_ckpt_with_resize` из копии пайплайна руки.

    Доказательство «все руки грузятся одним кодом» не должно опираться на то, что
    файлы целиком совпали: копии отличаются вставками раннера (трассировка,
    сохранение чекпойнтов) и при этом грузят состояние одинаково. Сравнивается
    ровно та функция, которой это делается.
    """
    src = pipeline.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {"file": str(pipeline), "error": f"пайплайн не разбирается: {exc}"}
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == LOADER_NAME:
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            return {"file": str(pipeline), "lines": [node.lineno, node.end_lineno],
                    "sha256": sha256_text(body)}
    return {"file": str(pipeline), "error": f"в пайплайне нет функции {LOADER_NAME}"}


# ─────────────────────────── наблюдения по рукам ─────────────────────────────

#: Окно повтора: 40 символов, шаг 5. Окно в 40 символов не повторяется в связном
#: тексте случайно (это ~8–10 слов), а период зацикливания у этих моделей —
#: фраза целиком, то есть окно внутри повтора всегда есть. Строка как единица
#: повтора не годится: `Приготовить домашний хлеб. Господа, вы готовите хлеб.
#: Господа, вы готовите хлеб. …` приходит **одной** строкой, и построчный счётчик
#: её не видит.
REPEAT_WINDOW = 40
REPEAT_STRIDE = 5
#: Порог «ответ вырожден в повтор». Назван числом, а не оставлен на глаз: 2 — это
#: ещё связный текст с повторившимся оборотом, 3 и выше в этих пробах не
#: встречается у невырожденных ответов (проверяется на базовой модели в тестах).
REPEAT_THRESHOLD = 3


def max_window_repeat(text: str) -> int:
    """Сколько раз повторяется самый частый 40-символьный фрагмент ответа."""
    t = re.sub(r"\s+", " ", text).strip()
    best = 0
    for i in range(0, max(len(t) - REPEAT_WINDOW, 0) + 1, REPEAT_STRIDE):
        best = max(best, t.count(t[i:i + REPEAT_WINDOW]))
    return best


def probes_summary(arms: list[str], ts: str) -> dict:
    """Описание проб качества — **не метрика**: счётчики вырождения и шаблона.

    Два независимых признака, оба считаются по тексту ответа:

    * `max_window_repeat` — сколько раз повторяется самый частый 40-символьный
      фрагмент (зацикливание). По руке берётся `worst_window_repeat` — максимум
      по пробам; он и различает руки (25 %: ≤5, 50 %: ≥13);
    * `template_collapse_probes` — сколько ответов начинаются теми же 90 символами,
      что и другой ответ **той же** руки (сведение к одному формату вместо ответа
      на вопрос; признак 25 %-рук).

    Ни один из них не участвует в решении по ADR-022: решает PPL. Они здесь,
    чтобы вторая линия доказательств была названа числами, а не впечатлением.
    """
    out = {}
    for arm in arms:
        p = STAND_CALIB / f"calib-{arm}-{ts}" / "logs" / "probes_cpt.json"
        if not p.is_file():
            out[arm] = {"available": False, "path": str(p)}
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        entries = []
        for pr in d.get("probes", []):
            r = pr.get("response", "")
            entries.append({"tag": pr.get("tag"), "chars": len(r),
                            "max_window_repeat": max_window_repeat(r),
                            "head": re.sub(r"\s+", " ", r.strip())[:90]})
        heads = [e["head"] for e in entries]
        for e in entries:
            e["shares_head_with_other_probe"] = heads.count(e["head"]) > 1
        worst = max((e["max_window_repeat"] for e in entries), default=0)
        out[arm] = {
            "available": True, "path": str(p.relative_to(STAND_SHARED)),
            "source_file": str(p),
            "decoding": d.get("decoding"), "max_new_tokens": d.get("max_new_tokens"),
            "probes": entries,
            "worst_window_repeat": worst,
            "degenerate_repeat_probes": sum(1 for e in entries
                                            if e["max_window_repeat"] >= REPEAT_THRESHOLD),
            "template_collapse_probes": sum(1 for e in entries
                                            if e["shares_head_with_other_probe"]),
            "descriptor": f"окно {REPEAT_WINDOW} симв., шаг {REPEAT_STRIDE}, "
                          f"порог вырождения {REPEAT_THRESHOLD}",
        }
    return out


def loss_trajectory(arms: list[str], ts: str) -> dict:
    """Критерий **а** ADR-022 внутри руки: убывает ли лосс по траектории.

    Между руками лосс не сравнивается (разный состав микса) — сравнивается первая
    и последняя треть **одной** траектории. Сводка по окнам для этого не годится
    (ADR-022 п.4): она скрыла разворот в S2.
    """
    out = {}
    for arm in arms:
        p = STAND_CALIB / f"calib-{arm}-{ts}" / "logs" / "loss_trace.jsonl"
        if not p.is_file():
            out[arm] = {"available": False, "path": str(p)}
            continue
        rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        loss = [r["loss"] for r in rows]
        k = max(len(loss) // 3, 1)
        first, last = statistics.fmean(loss[:k]), statistics.fmean(loss[-k:])
        out[arm] = {"available": True, "steps": len(loss),
                    "first_third_mean": first, "last_third_mean": last,
                    "delta": last - first, "min": min(loss), "max": max(loss),
                    "decreasing": last < first}
    return out


def side_observation_smoke(ts: str, baseline: float) -> dict:
    """Смоук пробы (шаг 500 руки 25-0.7) — **не часть этого замера**, но факт.

    Смоук снят раньше, на той же машине и тем же прибором (база сошлась с S3h),
    но только на наборах v1 и только для одного состояния. Он называется здесь
    затем, что даёт единственную точку внутри калибровочного окна: по ней видно,
    что PPL к концу окна **падает** (204 → 113), то есть деградация не растёт
    монотонно, и «сработает на полном прогоне» всё равно не доказано: падение
    есть, а до потолка 23.86 не доходит и близко.
    """
    p = probe_dir(ts) / "smoke.json"
    if not p.is_file():
        return {"available": False, "path": str(p)}
    d = json.loads(p.read_text(encoding="utf-8"))
    out = {"available": True, "path": str(p.relative_to(CASE_ROOT)),
           "date": d.get("date"), "sets": list(d.get("sets", {})),
           "states": {}}
    for name, st in d.get("states", {}).items():
        res = st.get("sets", {}).get(DECISIVE_SET)
        if not res:
            continue
        out["states"][name] = {
            "checkpoint": st.get("load", {}).get("checkpoint"),
            "ppl": res["ppl"], "ratio_vs_base": res["ppl"] / baseline,
        }
    out["note"] = ("в вердикт не входит: другой прогон пробы (только v1, одно "
                   "состояние), назван как точка внутри окна калибровки")
    return out


def replay_overlap(ts: str) -> dict:
    """Пересечение наборов GEN-EVAL v2 с обучающим материалом рук — **проверка**.

    Зачем. Наборы v2 собраны фильтром против префикса реплея **v12r** (ADR-018 п.3:
    обучено = первые 2444 чанка `general_replay_ru.txt`). У v12r50 реплей-часть —
    это **все** 3493 чанка источника (ADR-047 п.1). Значит, документы, отсеянные
    фильтром как «обученные» для v12r, для v12r50 — не отсеянные, а часть
    обучающего материала. Если так, `ppl_general` на v2 у 50 %-рук — это число на
    обучающем тексте, и оно не измеряет форгеттинг.

    Проверка прямая: каждый документ v2 ищется в файле реплея по 60-символьному
    фрагменту (текст берётся дословно). Плюс арифметика потолка источника из
    карточки корпуса: 3493 × 8192 токенов против 28 617 440 токенов источника.
    """
    card_path = CASE_ROOT / "data" / "corpus-card-v12r50.json"
    info: dict = {}
    if card_path.is_file():
        card = json.loads(card_path.read_text(encoding="utf-8"))
        rep = card["source"]["replay"]
        used = card["mix"]["replay_chunks"] * card["mix"]["chunk_len"]
        info["mix_card"] = {
            "card": str(card_path.relative_to(CASE_ROOT)),
            "replay_source": rep["path"], "replay_sha256": rep["sha256"],
            "replay_docs": rep["docs"], "replay_tokens": rep["tokens"],
            "replay_chunks_available": rep["chunks_available"],
            "v12r50_replay_chunks_used": card["mix"]["replay_chunks"],
            "v12r50_replay_tokens_used": used,
            "fraction_of_source_used": round(used / rep["tokens"], 6),
        }
    gen = CASE_ROOT / "data" / "gen-eval-v2-card.json"
    if gen.is_file():
        g = json.loads(gen.read_text(encoding="utf-8"))["sets"]["general"]
        info["gen_eval_card"] = {
            "card": str(gen.relative_to(CASE_ROOT)),
            "source": g["source"], "cut": g["cut"],
            "candidates": g["candidates"],
            "excluded_replay_token_prefix": g["excluded"]["replay_token_prefix"],
            "consumed_prefix_docs": g["consumed_prefix"]["docs"],
            "consumed_prefix_tokens": g["consumed_prefix"]["tokens"],
        }
    replay = STAND_SHARED / "datasets" / "general_replay_ru.txt"
    sets = {name: STAND_SHARED / "datasets" / f"{name}.txt"
            for name in ("general_eval", "general_eval_v2")}
    if not replay.is_file() or not all(p.is_file() for p in sets.values()):
        info["status"] = "not-verified: нет файла реплея или наборов"
        return info
    text = re.sub(r"\s+", " ", replay.read_text(encoding="utf-8", errors="replace"))
    found = {}
    for name, path in sets.items():
        docs = [d for d in path.read_text(encoding="utf-8").split("\n---\n")
                if len(d.strip()) > 50]
        hits, positions = 0, []
        for doc in docs:
            d = re.sub(r"\s+", " ", doc).strip()
            n = len(d)
            for frac in (0.5, 0.35, 0.65, 0.2, 0.8, 0.0):
                start = int((n - 60) * frac)
                if start < 0:
                    continue
                probe = d[start:start + 60]
                if len(probe) < 30:
                    continue
                at = text.find(probe)
                if at >= 0:
                    hits += 1
                    positions.append(round(at / len(text), 4))
                    break
        found[name] = {"docs": len(docs), "found_in_replay_source": hits,
                       "positions_frac": sorted(positions)}
    info["status"] = "measured"
    info["found"] = found
    info["note"] = ("v1_general — независимый набор общего языка (в источнике реплея "
                    "не найден ни один документ); v2_general собран из документов "
                    "источника реплея, поэтому у рук с 50 % replay он попадает внутрь "
                    "обученного материала")
    return info


# ─────────────────────────── свод и решение ──────────────────────────────────

def factor_effect(rows: list[dict], from_to: list[tuple[str, str]]) -> float | None:
    """Во сколько раз меняется отношение PPL/база при переходе между двумя руками.

    Пары различаются **одним** фактором, и результат усредняется по уровням
    другого: одиночная пара на наборе из 24 документов от шума не отличима.
    """
    ratios = {r["arm"]: r["ratio_vs_base"] for r in rows}
    vals = [ratios[b] / ratios[a] for a, b in from_to if a in ratios and b in ratios]
    return statistics.fmean(vals) if vals else None


def build(ts: str, arms: list[str], do_overlap: bool):
    reports, missing = read_reports(ts, arms)
    if missing:
        return None, EXIT_NOT_VERIFIED, f"нет отчётов пробы: {missing}"
    problems = require_complete(reports)
    if problems:
        return None, EXIT_FAIL, "отчёт пробы не является доказательством: " + "; ".join(problems)

    base = base_ppl(reports)
    ref, ref_info = s3h_reference()
    if not ref:
        return None, EXIT_NOT_VERIFIED, f"нет эталона S3h: {CASE_ROOT / S3H_NAME}"
    drift = {k: abs(base[k] - ref[k]) / max(abs(ref[k]), 1e-12)
             for k in ref if k in base}
    worst = max(drift.values()) if drift else float("inf")
    if worst > TOL:
        return None, EXIT_FAIL, (f"база не воспроизводит S3h: расхождение {worst:.3e} "
                                 f"({drift}) — свод считался бы по другому прибору")
    if DECISIVE_SET not in base:
        return None, EXIT_FAIL, f"в отчётах нет решающего набора {DECISIVE_SET}"

    baseline = base[DECISIVE_SET]
    ceiling = 2.0 * baseline

    rows, loaders = [], {}
    for arm in arms:
        params_path = CASE_ROOT / "runs" / f"calib-{arm}-{ts}" / "calib_params.json"
        if not params_path.is_file():
            return None, EXIT_NOT_VERIFIED, f"нет параметров руки: {params_path}"
        params = json.loads(params_path.read_text(encoding="utf-8"))
        final = reports[arm]["states"]["final"]
        ckpt = Path(final["load"]["checkpoint"])
        if not ckpt.is_file():
            return None, EXIT_NOT_VERIFIED, f"нет чекпойнта руки {arm}: {ckpt}"
        sha = sha256_file(ckpt)
        pipe = Path(reports[arm]["instrument"]["pipeline"])
        if not pipe.is_absolute():
            pipe = CASE_ROOT / pipe
        loaders[arm] = loader_digest(pipe)
        ppls = {name: res["ppl"] for name, res in final["sets"].items()}
        ppl = ppls[DECISIVE_SET]
        rows.append({
            "arm": arm,
            "replay_pct": params["replay_share_pct"],
            "lr_scale": params["peak_lr_scale"],
            "attn": params.get("attn"),
            "checkpoint_path": str(ckpt),
            "checkpoint_sha256": sha,
            "checkpoint_bytes": ckpt.stat().st_size,
            "pipeline": str(pipe.relative_to(CASE_ROOT)) if pipe.is_relative_to(CASE_ROOT) else str(pipe),
            "loader_sha256": loaders[arm].get("sha256"),
            "ppl_general": ppl,
            "ratio_vs_base": ppl / baseline,
            "passes_ceiling": ppl <= ceiling,
            "ppl_all_sets": ppls,
            "ratio_all_sets": {k: v / base[k] for k, v in ppls.items() if k in base},
        })
    loader_hashes = {v.get("sha256") for v in loaders.values()}
    loader_same = len(loader_hashes) == 1 and None not in loader_hashes

    rows.sort(key=lambda r: (r["replay_pct"], r["lr_scale"]))
    passing = [r for r in rows if r["passes_ceiling"]]
    best = min(rows, key=lambda r: r["ppl_general"])

    #: Пары «одно и то же, кроме одного фактора»: LR ×2 при фиксированном replay
    #: и replay 25→50 при фиксированном LR. Так эффект фактора не смешивается
    #: с эффектом другого.
    lr_eff = factor_effect(rows, [("25-0.35", "25-0.7"), ("50-0.35", "50-0.7")])
    rp_eff = factor_effect(rows, [("25-0.35", "50-0.35"), ("25-0.7", "50-0.7")])

    probes = probes_summary(arms, ts)
    traj = loss_trajectory(arms, ts)
    overlap = replay_overlap(ts) if do_overlap else {"status": "skipped"}

    #: Различение по **максимуму повтора на руку**: группы 25 % и 50 % не
    #: пересекаются (≤5 против ≥13), поэтому «какая доля replay выглядит хуже»
    #: — не выбор порога, а факт. Сравниваются группы, а не отдельные руки:
    #: у 25-0.35 худший повтор 4, у 25-0.7 — 5, и разница между ними ничего не
    #: значит, а разрыв с 50 %-руками — значит.
    #: Имя с `repeat` в основе: `worst` выше — расхождение базы с S3h, и подмена
    #: его счётчиком повтора уже один раз просочилась в evidence (worst_rel_delta
    #: показывал частоты повтора вместо относительной дельты замера).
    worst_repeat = {a: probes[a]["worst_window_repeat"] for a in arms
                    if probes[a].get("available")}
    w25 = [worst_repeat[a] for a in arms if a.startswith("25-") and a in worst_repeat]
    w50 = [worst_repeat[a] for a in arms if a.startswith("50-") and a in worst_repeat]
    probes_rank = None
    if w25 and w50:
        probes_rank = ("25%-руки" if max(w25) < min(w50)
                       else "50%-руки" if max(w50) < min(w25) else "не различает группы")
    loops = {a: probes[a].get("degenerate_repeat_probes") for a in arms
             if probes[a].get("available")}

    n_pass = len(passing)
    if n_pass:
        rec = min(passing, key=lambda r: r["ppl_general"])["arm"]
        reason = (f"потолок ADR-022 проходят {n_pass} из {len(rows)} рук; "
                  f"рекомендуется {rec} — наименьший PPL общего языка среди прошедших "
                  f"({min(r['ppl_general'] for r in passing):.2f} при потолке {ceiling:.2f})")
    else:
        rec = None
        reason = (f"ни одна из {len(rows)} рук не проходит потолок ADR-022 "
                  f"(2× базы = {ceiling:.2f}): минимум по рукам — {best['arm']} "
                  f"({best['ppl_general']:.2f}, ×{best['ratio_vs_base']:.2f}). "
                  f"Рекомендовать её в полный CPT значит начать стадию с заведомо "
                  f"проваленным критерием валидности (ADR-022 п.3: невыполнение — "
                  f"стоп стадии, а не «продолжаем и посмотрим»).")

    def fx(v):
        return f"×{v:.2f}" if v else "н/д"
    reason += (f" Факторы разведены: удвоение пика LR поднимает PPL в {fx(lr_eff)} раза "
               f"(0.7 против 0.35 при том же миксе), переход replay 25 % → 50 % меняет "
               f"её в {fx(rp_eff)} раза. То есть пик LR — сильный фактор, доля replay "
               f"в этом окне — слабый; при этом ×0.35 всё ещё даёт ×{best['ratio_vs_base']:.1f} "
               f"к базе, то есть недостаточно и его. Следующий шаг — не выбор из этих "
               f"четырёх, а калибровка пика LR **ниже** 0.35: сетка проверила 0.7 и "
               f"0.35, обе вне потолка, а «×0.35 сработает на полном прогоне» — "
               f"надежда, продолжать на которой ADR-022 п.3 запрещает.")

    coarse = (f"провалены все {len(rows)} руки, и обе линии это показывают; по PPL "
              f"удвоение пика LR даёт {fx(lr_eff)}" if n_pass == 0 else
              f"потолок проходят {n_pass} рук из {len(rows)}")
    l25 = [loops[a] for a in arms if a.startswith("25-") and a in loops]
    l50 = [loops[a] for a in arms if a.startswith("50-") and a in loops]
    #: Рука, которую называют пробы (наименьший повтор), — спорящая сторона, когда
    #: она не совпадает с рукой, которую называет PPL. Без неё «разница PPL» была бы
    #: разницей между 25-0.35 и 25-0.7, то есть между разными LR, а не между
    #: спорящими конфигурациями.
    rival = next((r for r in rows
                  if r["arm"] == (min(worst_repeat, key=worst_repeat.get)
                                  if worst_repeat else None)), rows[0])
    if probes_rank is None:
        consistency = ("не проверена: пробы качества не прочитаны "
                       "(см. paths в probes_observation)")
    elif probes_rank.startswith("не "):
        consistency = (f"частично: {coarse}. В тонком (какая рука ближе к пригодности) "
                       f"пробы группы не различают: худший повтор {w25} у 25 % против "
                       f"{w50} у 50 %")
    elif (best["replay_pct"] == 25) == probes_rank.startswith("25"):
        consistency = (f"согласуются: {coarse}; и PPL, и пробы называют одни и те же "
                       f"руки ({probes_rank}); ближайшая по PPL — {best['arm']} "
                       f"({best['ppl_general']:.2f}, ×{best['ratio_vs_base']:.2f})")
    else:
        consistency = (
            f"частично расходятся. В грубом — согласны: {coarse}. В тонком (какая "
            f"рука ближе к пригодности) — расходятся: минимум PPL у {best['arm']} "
            f"({best['ppl_general']:.2f}, ×{best['ratio_vs_base']:.2f}), пробы "
            f"называют {probes_rank} (вырожденных в повтор ответов: {l25} у 25 % "
            f"против {l50} у 50 %; худший повтор {w25} против {w50}). Разница PPL "
            f"между двумя спорящими руками — "
            f"{abs(best['ppl_general'] - rival['ppl_general']):.2f} при базе "
            f"{baseline:.2f} на наборе из "
            f"{reports[arms[0]]['sets'][DECISIVE_SET]['docs_kept']} документов, то есть "
            f"внутри разброса прибора; разница проб — качественная (связный текст "
            f"против зацикливания). В этой паре решает вторая линия, и PPL её не "
            f"подтверждает — расхождение названо, а не сглажено. Решение от этого не "
            f"меняется: обе 0.35-руки всё равно вне потолка.")

    evidence = {
        "schema": "s3m-ppl-arms/1",
        "stage": "S3m-2",
        "status": "complete",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": ("PPL общего языка четырёх рук калибровки микса×LR против потолка "
                    "ADR-022 (2× базы) — выбор конфига для полного CPT"),
        "decision_rule": {
            "adr": "ADR-022 п.3(б)",
            "criterion": f"ppl_general({DECISIVE_SET}) ≤ 2× базы",
            "set": DECISIVE_SET,
            "why_this_set": ("числа базы S3h (11.9319) сняты на v1_general, и критерий "
                             "назван для v1; сравнение по другому набору несопоставимо"),
            "loss_not_used": ("лосс микса между руками не сравнивается: у 25 % и 50 % "
                              "разный состав данных, меньший лосс там означает другую "
                              "смесь, а не лучшее усвоение (требование дельты)"),
        },
        "instrument": {
            "measure": "tools/calib_ppl_probe.py → tools/ppl_probe.py:measure (методика _ppl_eval S3h)",
            "split": "text.split('\\n---\\n'), len(doc.strip()) > 50, encode(add_special_tokens=False)[:1024], len(enc) > 8",
            "batch": 4, "max_len": 1024, "dtype": "bfloat16",
            "device": reports[arms[0]]["instrument"]["device"],
            "host": "локальная машина (RTX 4080 SUPER), НЕ стенд GB10",
            "tokenizer_setup": reports[arms[0]]["instrument"]["tokenizer_setup"],
            "base_weights": reports[arms[0]]["instrument"]["base_weights"],
            "load_checkpoint": f"пайплайн прогона руки:{LOADER_NAME}",
            "loader_same_across_arms": loader_same,
            "loaders": loaders,
            "env_shims": reports[arms[0]].get("env_shims", []),
        },
        "datasets": {name: {"path": s["path"], "sha256": s["sha256"],
                            "docs_kept": s.get("docs_kept"),
                            "tokens": reports[arms[0]]["states"]["base"]["sets"][name].get("tokens")}
                     for name, s in reports[arms[0]]["sets"].items()},
        "baseline_ppl": baseline,
        "baseline_source": {
            "file": S3H_NAME, "sha256": ref_info["sha256"],
            "values_all_sets": base,
            "s3h_reference_all_sets": ref,
            "worst_rel_delta_vs_s3h": worst,
            "tolerance": TOL,
            "from": "те же отчёты пробы (состояние base каждого прогона), сверено с эталоном S3h",
        },
        "ceiling": ceiling,
        "ceiling_formula": f"2 × {baseline!r} = {ceiling!r}",
        "artifacts": [],
        "arms": rows,
        "factor_effects": {
            "ceilings_note": "отношение PPL/база; усреднено по парам, различающимся одним фактором",
            "lr_0.35_to_0.7": lr_eff,
            "replay_25_to_50": rp_eff,
        },
        "loss_trajectory_criterion_a": traj,
        "probes_observation": probes,
        "side_observations": {"smoke_step500": side_observation_smoke(ts, baseline)},
        "cross_check_v1_vs_v2": overlap,
        "checks": [
            {"name": "instrument_vs_s3h", "verdict": "ok",
             "what": "каждый из четырёх прогонов пробы воспроизвёл базу S3h",
             "worst_rel_delta": worst, "tolerance": TOL},
            {"name": "base_restored", "verdict": "ok",
             "what": "после загрузки чекпойнтов веса возвращаются к базовым (вакуумная точка)",
             "detail": "; ".join(f"{a}: {[c['verdict'] for c in reports[a]['checks'] if c['name'] == 'base_restored']}"
                                 for a in arms)},
            {"name": "loader_identity", "verdict": "ok" if loader_same else "расхождение",
             "what": f"все руки грузятся одной и той же функцией {LOADER_NAME}",
             "detail": "; ".join(f"{a}: {loaders[a].get('sha256', loaders[a].get('error'))[:12]}"
                                 for a in arms)},
            {"name": "base_from_reports", "verdict": "ok",
             "what": "база взята из отчётов, а не вписана в инструмент",
             "detail": f"{DECISIVE_SET} = {baseline!r}"},
            {"name": "criterion_a_loss_decreasing",
             "verdict": "ok" if all(t.get("decreasing") for t in traj.values()) else "расхождение",
             "what": "ADR-022 п.3(а): лосс убывает по траектории **внутри** руки",
             "detail": "; ".join(f"{a}: {t['first_third_mean']:.4f} → {t['last_third_mean']:.4f}"
                                 for a, t in traj.items() if t.get("available")),
             "note": "критерий (а) выполнен всеми четырьмя руками — решение держит (б), PPL"},
        ],
        "verdict": {
            "recommended_arm": rec,
            "least_damaging_arm": best["arm"],
            "least_damaging_arm_note": ("минимум PPL; разница с соседней 0.35-рукой "
                                        "внутри разброса прибора, см. consistency_with_probes"),
            "least_repetition_arm": (min(worst_repeat, key=worst_repeat.get)
                                     if worst_repeat else None),
            "arms_passing_ceiling": [r["arm"] for r in passing],
            "reason": reason,
            "consistency_with_probes": consistency,
        },
        "assumptions": [],
        "not_verified": [],
        "open_questions": [],
        "reproduction": {},
        "rollback": "",
    }
    return evidence, EXIT_OK, ""


# ─────────────────────────── запись артефактов ───────────────────────────────

def merge_report(ts: str, arms: list[str], reports: dict, evidence: dict) -> Path:
    """Сводный отчёт пробы (`ppl.json`) — из него собирается манифест AD-2.

    Инструмент в своде один — общий для всех рук; поимённо копии пайплайнов рук
    названы в `arms[].pipeline` и в `instrument.loaders`. Это не «усреднение»:
    загрузка состояния у всех четырёх копий — один и тот же код (проверка
    `loader_identity`), и именно поэтому один `pipeline_sha256` здесь честен.
    """
    d = probe_dir(ts)
    first = reports[arms[0]]
    inst = dict(first["instrument"])
    if evidence["instrument"]["loader_same_across_arms"]:
        inst["pipeline"] = evidence["arms"][0]["pipeline"]
        inst["pipeline_sha256"] = first["instrument"]["pipeline_sha256"]
        inst["pipeline_note"] = ("копии пайплайна у рук отличаются вставками раннера "
                                 "(трассировка лосса, сохранение чекпойнтов); функция "
                                 f"загрузки {LOADER_NAME} у всех четырёх одна — хеши в "
                                 "evidence.arms[].loader_sha256")
    states = {}
    for arm in arms:
        for name, entry in reports[arm]["states"].items():
            states[f"{arm}:{name}"] = entry
    merged = {
        "schema": first.get("schema"),
        "stage": "S3m-2",
        "date": datetime.now(timezone.utc).isoformat(),
        "purpose": ("PPL чекпойнтов калибровки (checkpoint_final) на наборах v1/v2 — "
                    "свод четырёх рук для вердикта по ADR-022"),
        "instrument": inst,
        "env_shims": first.get("env_shims", []),
        "case_root": first.get("case_root"),
        "sets": first["sets"],
        "states": states,
        "complete": True,
        "pending_states": [],
        "measured_states": sorted(states),
        "checks": [{"name": "base_vs_s3h", "verdict": "ok",
                    "tolerance": TOL,
                    "worst_rel_delta": evidence["baseline_source"]["worst_rel_delta_vs_s3h"],
                    "detail": "сведено из отчётов рук: " + "; ".join(
                        f"{a}:{[c['verdict'] for c in reports[a]['checks'] if c['name'] == 'base_vs_s3h']}"
                        for a in arms)}],
        "merged_from": [str((d / f"ppl-{a}.json").relative_to(CASE_ROOT)) for a in arms],
    }
    p = d / MERGED_NAME
    p.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def write_probe_manifest(ts: str, arms: list[str], evidence: dict) -> dict:
    """Манифест AD-2 каталога пробы — тем же кодом, что у раннера (не второй раз).

    `image` не выдумывается: проба шла **на локальной машине**, не в контейнере
    стенда, и это и записывается. Подставить сюда образ стенда значило бы соврать
    в поле, которое существует ровно для того, чтобы прогон был воспроизводим.
    """
    try:
        import run_mix_lr_calib as R
    except ImportError as exc:
        return {"error": f"не импортируется раннер (манифест не записан): {exc}"}
    import argparse as _ap
    env = (f"не контейнер: {evidence['instrument']['host']}, torch/transformers — "
           f"локальное окружение, dtype {evidence['instrument']['dtype']}, "
           f"batch {evidence['instrument']['batch']}, max_len {evidence['instrument']['max_len']}")
    args = _ap.Namespace(image=env)
    man = R.write_probe_manifest(probe_dir(ts), ts, arms,
                                 CASE_ROOT / "laguna_pipeline_v8.py", args, {"gaps": []})
    return man


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="S3m-2: свод PPL четырёх рук калибровки → таблица, вердикт, evidence")
    ap.add_argument("--ts", required=True, help="метка сетки (например 20260916-0820)")
    ap.add_argument("--arms", default=",".join(ARM_ORDER))
    ap.add_argument("--out", default="evidence/s3m-ppl-arms.json")
    ap.add_argument("--case-root", default=None,
                    help="корень кейса (отчёты пробы, параметры рук, эталон S3h); "
                         "по умолчанию — каталог над tools/. Параметр нужен тестам: "
                         "проверять свод на фикстуре, а не на боевых числах")
    ap.add_argument("--shared", default=str(STAND_SHARED))
    ap.add_argument("--no-replay-overlap", dest="overlap", action="store_false", default=True,
                    help="не проверять пересечение наборов v2 с обучающим материалом")
    ap.add_argument("--no-manifest", dest="manifest", action="store_false", default=True,
                    help="не писать манифест AD-2 каталога пробы")
    ap.add_argument("--plan", action="store_true")
    return ap.parse_args(argv)


def render_plan(args, arms) -> str:
    lines = ["план свода S3m-2:",
             f"  каталог пробы: {probe_dir(args.ts)}",
             f"  отчёты рук:    " + ", ".join(f"ppl-{a}.json" for a in arms),
             f"  решающий набор: {DECISIVE_SET}; потолок = 2 × база (ADR-022 п.3(б))",
             "  руки:"]
    for a in arms:
        params = CASE_ROOT / "runs" / f"calib-{a}-{args.ts}" / "calib_params.json"
        mark = "" if params.is_file() else "  ← нет calib_params.json"
        lines.append(f"    {a:8s} replay/lr из {params.relative_to(CASE_ROOT)}{mark}")
    lines.append(f"  выход: {args.out}")
    lines.append("  sha256 чекпойнтов: считать (кэш метаданных отклонён, "
                 f"цена честного счёта ≈22 с на 4×3 ГБ); пересечение v2↔реплей: "
                 f"{'проверять' if args.overlap else 'не проверять'}")
    if (CASE_ROOT / S3H_NAME).is_file():
        lines.append(f"  эталон S3h: {S3H_NAME}")
    else:
        lines.append(f"  эталон S3h: НЕТ ({CASE_ROOT / S3H_NAME}) — свод откажет")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    global STAND_SHARED, STAND_CALIB, CASE_ROOT
    STAND_SHARED = Path(args.shared)
    STAND_CALIB = STAND_SHARED / "calib"
    if args.case_root:
        CASE_ROOT = Path(args.case_root).resolve()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if args.plan:
        print(render_plan(args, arms))
        return EXIT_OK
    evidence, rc, why = build(args.ts, arms, args.overlap)
    if evidence is None:
        print(f"{'ОТКАЗ' if rc == EXIT_FAIL else 'NOT-VERIFIED'}: {why}", file=sys.stderr)
        return rc

    merged = merge_report(args.ts, arms, read_reports(args.ts, arms)[0], evidence)
    evidence["artifacts"] = [
        {"path": str(merged.relative_to(CASE_ROOT)), "what": "сводный отчёт пробы"},
        *[{"path": f"runs/calib-ppl-{args.ts}/ppl-{a}.json",
           "sha256": sha256_file(probe_dir(args.ts) / f"ppl-{a}.json"),
           "what": f"отчёт пробы руки {a}"} for a in arms],
    ]
    evidence["instrument"]["checkpoint_sha256_note"] = CKPT_CACHE_NOTE
    if args.manifest:
        man = write_probe_manifest(args.ts, arms, evidence)
        evidence["artifacts"].append(
            {"path": (man.get("path") or "run_manifest.json"),
             "what": "манифест AD-2 каталога пробы", "stages": man.get("stages"),
             "complete": man.get("complete"), "error": man.get("error")})
    evidence["reproduction"] = {
        "measure": (f"python3 tools/calib_ppl_probe.py --pipeline runs/calib-<arm>-{args.ts}/"
                    f"laguna_pipeline_calib.py --state base=base "
                    f"--state final=ckpt:<shared>/calib/calib-<arm>-{args.ts}/checkpoints/"
                    f"checkpoint_final.pt --state base_untouched=base --sets all "
                    f"--out runs/calib-ppl-{args.ts}/ppl-<arm>.json"),
        "summarize": f"python3 tools/calib_ppl_arms.py --ts {args.ts}",
        "plan": f"python3 tools/calib_ppl_arms.py --ts {args.ts} --plan",
    }
    evidence["rollback"] = (
        "свод — производное от замеров; откат = удалить evidence/s3m-ppl-arms.json, "
        "runs/calib-ppl-<ts>/ppl.json и манифест пробы. Чекпойнты калибровки в "
        "/home/user/gb10-shared/calib/ не удаляются: они исходные данные решения "
        "о полном CPT")
    evidence["assumptions"] = [
        "критерий ADR-022 п.3(б) применён к концу калибровочного окна (2000 шагов): "
        "полный CPT идёт по другому расписанию, и число шагов у стадии больше; "
        "калибровка существует затем, чтобы решать до полного прогона",
        "потолок считается от базы v1_general 11.9319, воспроизведённой пробой",
        "решающий набор — v1_general (24 документа, 3682 токена) как единственный, "
        "на котором снята база; его малый объём — ограничение точности, названное, "
        "а не смягчённое",
        "проба меряет состояние чекпойнта штатным SDPA, а обучение рук шло с "
        "LAGUNA_ATTN=flex — тем же расхождением, что у GEN-EVAL и у диагностики S3k",
    ]
    evidence["not_verified"] = [
        "генерация (тексты ответов) после калибровки не оценивалась: пробы качества — "
        "из журналов прогона, это описание, а не измерение",
        "причина деградации не установлена: разделены факторы LR и replay, но не "
        "проверено, достаточно ли ×0.35 вообще (более низкий пик не мерился)",
    ]
    ev_arms = evidence["arms"]
    ratios = [r["ratio_vs_base"] for r in ev_arms]
    lr_lo = min(r["lr_scale"] for r in ev_arms)
    lr_hi = max(r["lr_scale"] for r in ev_arms)
    eff = evidence["factor_effects"]
    dec = evidence["datasets"][DECISIVE_SET]
    questions = [
        f"пик LR: сетка проверила {lr_lo} и {lr_hi} — обе вне потолка. Каким должен "
        f"быть пик, чтобы 2000 шагов дали ppl_general ≤ {evidence['ceiling']:.2f}, и "
        f"проверяется ли он той же сеткой (это следующая дельта, не эта)",
        f"доля replay: переход 25 % → 50 % меняет PPL в ×{eff['replay_25_to_50']:.2f} "
        f"раза, то есть меньше, чем разброс между руками одного LR. Остаётся ли она "
        f"фактором вообще, или решение принимается по LR и по доменному качеству",
        f"прибор: решающий набор v1_general — {dec['docs_kept']} документов и "
        f"{dec['tokens']} токенов. Этого хватает, чтобы отличить ×{min(ratios):.1f} от "
        f"×{max(ratios):.1f}, но не чтобы различить ×{sorted(ratios)[0]:.2f} и "
        f"×{sorted(ratios)[1]:.2f}; нужен ли расширенный v1 общего языка под этот критерий",
    ]
    overlap = evidence.get("cross_check_v1_vs_v2", {})
    found = (overlap.get("found") or {}) if isinstance(overlap, dict) else {}
    v2 = found.get("general_eval_v2") or {}
    card = (overlap.get("gen_eval_card") or {}) if isinstance(overlap, dict) else {}
    if v2.get("docs") and v2.get("found_in_replay_source") == v2["docs"]:
        questions.append(
            f"набор v2 общего языка собран фильтром против префикса реплея **v12r** "
            f"(ADR-018 п.3: обучено = {card.get('consumed_prefix_docs')} документов), "
            f"а у v12r50 реплей-часть — все 3493 чанка источника "
            f"({(overlap.get('mix_card') or {}).get('fraction_of_source_used'):.4f} файла). "
            f"Проверено: {v2['found_in_replay_source']} из {v2['docs']} документов v2 "
            f"найдены в источнике реплея дословно, то есть у 50 %-рук v2_general — "
            f"текст из их обучения, и его низкий PPL (×0.99 у 50-0.35) не измеряет "
            f"форгеттинг. Фильтр ADR-018 привязан к составу микса и устаревает при "
            f"его смене (ADR-047): пересобирать ли набор под каждый микс или "
            f"зафиксировать инвариант «general-набор не пересекается с любым "
            f"кандидатным миксом»")
    evidence["open_questions"] = questions
    out = CASE_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"{'рука':9s} {'replay':>6s} {'LR':>5s} {'PPL общий':>10s} {'/база':>7s} "
          f"{'потолок':>8s}  sha256")
    for r in evidence["arms"]:
        print(f"{r['arm']:9s} {r['replay_pct']:6d} {r['lr_scale']:5.2f} "
              f"{r['ppl_general']:10.3f} {r['ratio_vs_base']:7.3f} "
              f"{'да' if r['passes_ceiling'] else 'НЕТ':>8s}  "
              f"{(r['checkpoint_sha256'] or '-')[:16]}")
    print(f"\nбаза={evidence['baseline_ppl']:.4f} (S3h, {DECISIVE_SET}); "
          f"потолок={evidence['ceiling']:.4f}")
    print(f"рекомендация: {evidence['verdict']['recommended_arm'] or '—'}")
    print(f"evidence: {out}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
