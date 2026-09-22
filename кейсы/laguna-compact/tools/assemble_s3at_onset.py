#!/usr/bin/env python3
"""assemble_s3at_onset.py — S3at: кривая зарождения петель по всему измеримому диапазону.

**Вопрос дельты.** Тренд S3as дал **плато**: на 32 600 петель 6/104 — ровно столько
же, сколько на 21 500 (изменение за 11 100 шагов неразличимо). Значит «доучивать»
этим замером не подтверждается. Но остаётся неизвестным, **когда** петли родились:

* родились **сразу** (уже к первым сотням шагов) — причина в формате/данных, и
  «режим обучения» как гипотеза этим не подтверждается;
* родились **позже** — тогда названо окно, и причина — в том, что в этом окне
  меняется (темп, расписание, накопление), а не в наборе как таковом.

От этого зависит выбор плана: доучивать остановленный прогон или переделывать
режим. Поэтому кривая строится по **всему измеримому** диапазону, а не по его
последней трети.

**Что склеивается и чем.** Ничего не перемеряется: новых проб не снималось, GPU не
занимался (ни локальная RTX 4080, ни стенд GB10). Три источника уже снятых чисел:

1. **Ранние точки того же прогона** — `runs/s3ap-format-monitor-20260919/
   format_wide_standard.json`: состояния `cfinal` (вход стадии), `sft_v13_0500`,
   `sft_v13_5000`, `sft_v13_9000` — **одним процессом** прибора, набор `wide`,
   бюджет 4096;
2. **Поздние точки** — восемь отчётов `runs/sft-loop-trend-20260921/
   report_sft_v13_*.json` (21 500…32 600), тем же прибором и тем же протоколом;
3. **Якорь через границу устройств** — парный контроль S3aq
   (`format_wide_4096_pair.json`, 4080): на нём тот же чекпойнт 21 500 снят **вне**
   прогона тренда, то есть он даёт второй замер одной точки на другом устройстве.

Доли считаются **вызовом** `tools/analyze_loop_trend.py` (тот же инструмент и та же
метрика, что у тренда) — не переписанной рядом арифметикой: «петля» — диагноз,
который в двух местах не должен считаться по-разному. Блоки по словам берутся из
свода прибора происхождения (`analyze_loop_origin.py`), снятого на тех же 12
состояниях: `runs/s3at-loop-onset-20260921/loop_origin.json`.

**Правила кривой объявлены ДО чтения чисел** (без них «зарождение» доопределялось бы
по месту, то есть подгонялось бы под ответ):

1. **Присутствие ≠ превышение базы.** «Петли есть на шаге S» — это k(S) ≥ 1, и
   только это. У базы (`cfinal`) своя ненулевая доля (1/104), поэтому «на 500 петли
   есть» само по себе не значит «на 500 их больше, чем было до SFT».
2. **«Уровень выше базы» — двухдолевой тест против базы, p < 0.05** (двусторонне).
3. **Окно зарождения** — между последней точкой, НЕ отличимой от базы, и первой,
   отличимой от неё. Нет такой точки — окно не определено при этом n, и это
   утверждение о разрешении замера, а не «петель нет».
4. **Разрешение называется рядом.** Для каждой пары печатается MDD (минимальная
   разница долей, различимая с мощностью 0.8 при α = 0.05, n = 104 на точку).
   Значимая по p, но меньшая MDD разница помечается «на грани разрешения»: p
   говорит про эту выборку, MDD — про то, что замер ловит вообще.
5. **Пары через границу устройств помечаются** и в вывод о зарождении не идут:
   числа разных устройств расходятся не только шагом (см. `device_bridge`).
6. **Блоки по словам — ДРУГОЕ определение, чем доля петель прибора.** Доля петель —
   это `metrics.looped` прибора (одна 4-грамма повторена ≥ 8 раз); блок — ≥ 8 слов,
   повторённых ≥ 2 раз, с любым периодом, то есть в него попадают и длинные
   перечисления. Совпадение определений проверяется числом (`metric_definition_check`),
   а не предполагается: на 500 они расходятся принципиально.

Коды возврата::

    0 — свод собран
    1 — отказ: вход противоречит контракту (точки сняты разными приборами или на
        разных наборах промптов — склейка мерила бы разницу приборов)
    2 — NOT-VERIFIED: мерить нечего (нет ранних отчётов, нет свода блоков)

Запуск::

    python3 tools/analyze_loop_origin.py \\
        --report runs/s3ap-format-monitor-20260919/format_wide_standard.json \\
        --report runs/sft-loop-trend-20260921/report_sft_v13_21500.json ... \\
        --dataset datasets/sft_train_v13_fixed.jsonl \\
        --out runs/s3at-loop-onset-20260921/loop_origin.json

    python3 tools/assemble_s3at_onset.py --out evidence/sft-loop-onset.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE / "tools"))

#: Арифметика значимости и чтение входов — **импортом**, не копией: доля, MDD и
#: двухдолевой тест обязаны считаться там же, где их считает тренд, иначе
#: «склейка кривой» склеила бы два разных определения доли.
import analyze_loop_trend as T  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

RUN = "runs/s3at-loop-onset-20260921"
EARLY_REPORT = "runs/s3ap-format-monitor-20260919/format_wide_standard.json"
LATE_DIR = "runs/sft-loop-trend-20260921"
LATE_STEPS = (21500, 23000, 24500, 26000, 27500, 29000, 30500, 32600)
ORIGIN = RUN + "/loop_origin.json"
TREND_JSON = RUN + "/loop_trend.json"
EVIDENCE = "evidence/sft-loop-onset.json"

#: Якорь через границу устройств: парный контроль S3aq (тот же чекпойнт 21 500,
#: тот же протокол 4096), снятый НЕ прогоном тренда. Отчёт лежит в ветке
#: `arch/laguna-control-arms` — читается ссылкой `git:`, а не копией (ADR-023 п.9).
#: sha256 объявлен здесь и сверяется: подмена входа обязана быть отказом, а не
#: тихой подменой чисел.
ANCHOR = ("git:arch/laguna-control-arms:кейсы/laguna-compact/runs/"
          "s3aq-budget-8192-20260920/format_wide_4096_pair.json")
ANCHOR_SHA256 = "b3479e75463565a05a2de72e6d1f09d2b7c36c94f8ff974405080ad4060703c9"
ANCHOR_SHA_SOURCE = ("evidence/s3aq-budget-8192.json → artifacts.report_4096_pair.sha256 "
                     "(плюс sha256 файла в ветке)")

#: Состояния раннего отчёта и шаг, которым они входят в кривую. `cfinal` — вход
#: стадии (`checkpoint_final.pt`, `loaded_as`), то есть состояние шага 0: это не
#: догадка, а запись прибора о прогоне (см. `inventory.files_without_step_in_name`
#: у `analyze_loop_trend.py`). Инструмент тренда такие состояния исключает
#: (в метке нет шага) — здесь они идут отдельным якорем, а не точкой тренда.
EARLY_STATES = {
    "cfinal": 0,
    "sft_v13_0500": 500,
    "sft_v13_5000": 5000,
    "sft_v13_9000": 9000,
}

#: Три дефектные метрики — те же имена, что в `DEFECT_METRICS` тренда (импортом).
DEFECT_METRICS = T.DEFECT_METRICS

#: Наборы, на которых называется зарождение и (отдельно) позднее плато.
#: Ранний набор — ОДИН процесс прибора на ОДНОМ устройстве: в нём нет ни разницы
#: устройств, ни разницы сессий, и именно поэтому он отвечает на вопрос «когда».
ONSET_SET = ("cfinal", "sft_v13_0500", "sft_v13_5000", "sft_v13_9000")
ONSET_SET_NOTE = ("четыре состояния одного вызова прибора (19.09.2026, локальная "
                  "RTX 4080): ни разницы устройств, ни разницы сессий внутри набора")
LATE_SET = tuple(LATE_STEPS)
LATE_SET_NOTE = ("восемь точек одного вызова прибора на стенде GB10 (21.09.2026) — "
                 "внутри набора та же машина и та же сессия")

#: Устройство и сессия каждой группы отчётов. Объявлено здесь потому, что разница
#: устройств — не деталь провенанса, а измеренная величина: см. `device_bridge`.
#: Источник у каждой строки назван файлом, а не памятью.
DEVICE_GROUPS = {
    "early-4080": {
        "reports": [EARLY_REPORT],
        "device": "NVIDIA GeForce RTX 4080 SUPER (локальная машина)",
        "session": "19.09.2026, один вызов прибора на 4 состояния",
        "source": "runs/s3ap-format-monitor-20260919/device_check.json (device_name) "
                  "+ chain.sh (шаг 2: «один широкий прогон на все состояния»)",
    },
    "late-gb10": {
        "reports": [f"{LATE_DIR}/report_sft_v13_{s}.json" for s in LATE_STEPS],
        "device": "стенд GB10, контейнер nvcr.io/nvidia/pytorch:26.07-py3-vllm",
        "session": "21.09.2026, восемь точек по 500 шагов (кроме 32 600 — там "
                   "ретенция оставила только checkpoint_*)",
        "source": f"{LATE_DIR}/chain.sh (IMG, STAGE) + log_sft_v13_21500.log "
                  "(баннер NVIDIA Release 26.07; CUDA 13.3 / драйвер 580.173.02)",
    },
    "anchor-4080": {
        "reports": [ANCHOR],
        "device": "NVIDIA GeForce RTX 4080 SUPER (локальная машина)",
        "session": "20.09.2026, парный контроль S3aq (тот же чекпойнт, бюджет 4096)",
        "source": "git:" + ANCHOR.split(":", 2)[1] + ":" + ANCHOR.split(":", 2)[2]
                  .rsplit("/", 1)[0] + "/device_check.json (device_name)",
    },
}

#: Признак среды, проверяемый МАШИННО, а не по объявлению: путь кэша токенизатора
#: в `model_provenance`. На хосте он начинается с `/home/user/`, в контейнере
#: стенда — с `/root/`: два прогона в разных средах обязаны различаться этим полем.
RUNTIME_MARKER = {"host_prefix": "/home/user/", "container_prefix": "/root/"}


def runtime_of(tokenizer_path: str | None) -> str:
    """Среда прогона по корню кэша токенизатора: `host` / `container` / `?`.

    Признак выбран потому, что он МАШИННЫЙ: путь кэша — часть отчёта прибора, и он
    различается у прогона на хосте (локальная 4080) и в контейнере стенда, тогда как
    поле `device` у обоих просто «cuda».
    """
    p = tokenizer_path or ""
    if p.startswith(RUNTIME_MARKER["host_prefix"]):
        return "host"
    if p.startswith(RUNTIME_MARKER["container_prefix"]):
        return "container"
    return "?"

#: Порог значимости — тот же, что у тренда (правило 3 тренда: p < 0.05 двусторонне).
ALPHA = 0.05

#: Шаг сетки точек прогона: им измеряется «сколько шагов не покрыто замером».
GRID = 500


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str | None:
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_raw(spec: str) -> str | None:
    """Текст входа: файл дерева или `git:<ref>:<путь>` (отчёты живут в ветвях)."""
    if spec.startswith("git:"):
        ref, _, rel = spec[4:].partition(":")
        try:
            out = subprocess.run(["git", "show", f"{ref}:{rel}"], cwd=str(CASE),
                                 capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            note(f"git show не удался для {spec}: {e.stderr.decode()[:200]}")
            return None
        return out.stdout.decode("utf-8")
    p = Path(spec)
    if not p.is_file():
        note(f"нет файла {spec}")
        return None
    return p.read_text(encoding="utf-8")


def input_hash(spec: str) -> dict:
    """Вход с носителем: путь, байты и sha256 — по файлу дерева или по ветке.

    Хеш считается по СОДЕРЖИМОМУ, а не по пути: отчёт в ветке и его копия в дереве
    обязаны давать одно число, иначе «тот же вход» держалось бы на имени.
    """
    raw = read_raw(spec)
    if raw is None:
        return {"path": spec, "exists": False}
    return {"path": spec, "exists": True, "bytes": len(raw.encode("utf-8")),
            "sha256": sha256_text(raw)}


def read_json(spec: str):
    raw = read_raw(spec)
    if raw is None:
        return None, None
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as e:
        note(f"не разобран JSON {spec}: {e}")
        return None, raw


# ── точки замеров ────────────────────────────────────────────────────────────

def measurements(reports: list[tuple[str, dict]]) -> list[dict]:
    """По каждому состоянию каждого отчёта — векторы 0/1 по пробам и провенанс.

    Векторы считаются `metric_vector` тренда (импортом): единица наблюдения —
    проба, а не блок (правило 1 тренда). Знаменатель незакрытых `<think>` —
    естественно завершённые ходы (ADR-045 п.5) и приходит оттуда же.
    """
    out = []
    for spec, rep in reports:
        proto = rep.get("protocol") or {}
        for tag, st in (rep.get("states") or {}).items():
            probes = st.get("probes") or []
            if not probes:
                note(f"состояние {tag} в {spec}: проб нет — пропущено")
                continue
            step = T.step_of(tag)
            rec = {
                "report": spec,
                "tag": tag,
                "step": step if step is not None else EARLY_STATES.get(tag),
                "step_basis": ("шаг из метки состояния" if step is not None else
                               "вход стадии (cfinal = checkpoint_final.pt, loaded_as)"),
                "checkpoint": st.get("checkpoint"),
                "checkpoint_sha256": st.get("checkpoint_sha256"),
                "n_probes": len(probes),
                "group": group_of(spec),
                "vectors": {k: T.metric_vector(probes, k) for k in DEFECT_METRICS},
                "runtime_marker": ((rep.get("model_provenance") or {})
                                   .get("tokenizer", "")),
                "runtime": runtime_of((rep.get("model_provenance") or {})
                                      .get("tokenizer")),
                "decoding": (proto.get("decoding_protocol") or {}),
                "instrument_sha256": rep.get("tool_sha256"),
                "prompts_digest": proto.get("prompts_digest"),
                "prompts_set": proto.get("prompts_set"),
                "max_new_tokens": proto.get("max_new_tokens"),
                "batch_size": proto.get("batch_size"),
                "stop_at_turn_end": proto.get("stop_at_turn_end"),
            }
            rec["shares"] = {
                k: {
                    "k": sum(v), "n": len(v),
                    "share": (sum(v) / len(v)) if v else None,
                    "ci95": T.wilson(sum(v), len(v)),
                }
                for k, v in rec["vectors"].items()
            }
            rec["looped_instances"] = [
                {"tag": p.get("tag"), "system": p.get("system"),
                 "max4gram_rep": (p.get("metrics") or {}).get("max4gram_rep"),
                 "stop_reason": p.get("stop_reason")}
                for p in probes if (p.get("metrics") or {}).get("looped")
            ]
            #: Сырые пробы остаются при записи: ими сверяется один и тот же
            #: чекпойнт, снятый дважды (граница устройств и повторный замер).
            rec["_probes"] = probes
            out.append(rec)
    out.sort(key=lambda r: (r["step"] if r["step"] is not None else -1, r["tag"]))
    return out


def group_of(spec: str) -> str:
    for name, g in DEVICE_GROUPS.items():
        if spec in g["reports"]:
            return name
    return "?"


def device_of(spec: str) -> str:
    """Устройство группы — строкой, чтобы «та же машина» сравнивалось машиной.

    Имя группы («early-4080» против «anchor-4080») для этого не годится: группы
    разные, а машина одна, и сравнение имён выдало бы повторный замер того же
    чекпойнта за замер на другом устройстве.
    """
    return (DEVICE_GROUPS.get(group_of(spec)) or {}).get("device", "?")


def comparability(recs: list[dict]) -> dict:
    """Сопоставимость склейки: один прибор, один набор промптов, один протокол.

    Расхождение прибора или набора промптов — **отказ** (код 1): кривая из разных
    приборов мерила бы разницу приборов, а не шага. Расхождение бюджета/пакета —
    не отказ, а названный факт: в этом замере бюджеты совпадают, и это проверяется.
    """
    def uniq(field):
        return sorted({str(r[field]) for r in recs})

    inst, dig, sets = uniq("instrument_sha256"), uniq("prompts_digest"), uniq("prompts_set")
    budget, batch = uniq("max_new_tokens"), uniq("batch_size")
    stop = sorted({str(r["stop_at_turn_end"]) for r in recs})
    nogr = sorted({str(r["decoding"].get("no_repeat_ngram")) for r in recs})
    standard = sorted({str(r["decoding"].get("standard")) for r in recs})
    allowed = sorted({str(r["decoding"].get("allowed_for_conclusions")) for r in recs})
    verdict = "comparable" if (len(inst) == 1 and len(dig) == 1 and len(sets) == 1) \
        else "not_comparable"
    return {
        "instrument_sha256": inst,
        "prompts_digest": dig,
        "prompts_set": sets,
        "max_new_tokens": budget,
        "batch_size": batch,
        "stop_at_turn_end": stop,
        "no_repeat_ngram": nogr,
        "decoding_standard": standard,
        "allowed_for_conclusions": allowed,
        "n_probes_per_state": sorted({r["n_probes"] for r in recs}),
        #: Машиночитаемый признак среды: до «/.cache» путь токенизатора принадлежит
        #: ХОСТУ прогона (/home/user) или КОНТЕЙНЕРУ стенда (/root). Это проверка
        #: объявления «ранняя часть — 4080, поздняя — GB10», а не доверие к тексту.
        "runtime_markers": sorted({(r["runtime_marker"] or "").rsplit("/.cache", 1)[0]
                                   or "?" for r in recs}),
        "runtime_marker_reading": ("корень кэша токенизатора по отчётам: хост-прогон "
                                   "даёт /home/user, контейнер стенда — /root; "
                                   "расхождение корней и есть граница сред"),
        "runtime_classes": sorted({r.get("runtime", "?") for r in recs}),
        "verdict": verdict,
        "reading": ("один прибор, один набор промптов и один протокол — доли точек "
                    "сопоставимы между собой" if verdict == "comparable" else
                    "прибор или набор промптов разошлись: склейка недействительна"),
        "n_states": len(recs),
    }


def device_bridge(anchor_rep: dict, late_rec: dict) -> dict:
    """Сколько стоит граница устройств — числом, а не оговоркой.

    Берётся ОДИН чекпойнт, снятый дважды: в прогоне тренда (GB10) и парным
    контролем S3aq (4080). Если sha весов совпадают, а ответы — нет, то разница
    подписывается устройством/средой, и её размер становится известен. Без этого
    числа склейка «ранняя часть на 4080 + поздняя на GB10» выдавала бы смену
    устройства за смену шага.
    """
    pa = next((st.get("probes") or [] for st in (anchor_rep.get("states") or {}).values()
               if st.get("checkpoint_sha256") == late_rec["checkpoint_sha256"]), None)
    if not pa:
        return {"available": False,
                "why": "в парном контроле нет состояния с тем же sha чекпойнта"}
    #: Пробы сопоставляются порядком: оба отчёта сняты на одном наборе `wide`
    #: (дайджест сверен выше), и порядок промптов в наборе — часть прибора.
    pb = late_rec["_probes"]
    same_bytes = sum(1 for p, q in zip(pa, pb) if p.get("response") == q.get("response"))
    flips = {}
    for name, getter in (
            ("looped", lambda p: bool((p.get("metrics") or {}).get("looped"))),
            ("hit_limit", lambda p: bool(p.get("hit_limit"))),
            ("stop_reason", lambda p: p.get("stop_reason")),
            ("n_new_tokens", lambda p: p.get("n_new_tokens"))):
        flips[name] = sum(1 for p, q in zip(pa, pb) if getter(p) != getter(q))
    return {
        "available": True,
        "checkpoint_sha256": late_rec["checkpoint_sha256"],
        "step": late_rec["step"],
        "n": min(len(pa), len(pb)),
        "byte_identical_responses": same_bytes,
        "flips": flips,
        "flips_share": {k: v / max(1, min(len(pa), len(pb))) for k, v in flips.items()},
        "reading": ("один чекпойнт, два устройства: ответы совпадают побайтово не "
                    "полностью, а флаги петель расходятся — прибор детерминирован "
                    "на устройстве, но НЕ инвариантен к устройству/среде"),
        "consequence": ("пары через границу устройств в вывод о зарождении не идут "
                        "(правило 5): их разница содержит этот размах"),
    }


def same_device_reproducibility(recs: list[dict]) -> dict:
    """Повторный замер того же чекпойнта на ТОМ ЖЕ устройстве — база сравнения.

    Без неё «4 пробы разошлись» читалось бы как свойство замера вообще. Здесь
    видно, что это свойство именно границы устройств: на одном устройстве (пусть
    и в другой день) ответы совпадают побайтово.
    """
    by_sha: dict[str, list[dict]] = {}
    for r in recs:
        if r.get("checkpoint_sha256"):
            by_sha.setdefault(r["checkpoint_sha256"], []).append(r)
    out = []
    for sha, group in sorted(by_sha.items()):
        if len(group) < 2:
            continue
        a, b = group[0], group[1]
        pa, pb = a["_probes"], b["_probes"]
        out.append({
            "checkpoint_sha256": sha,
            "step": a["step"],
            "n": min(len(pa), len(pb)),
            "same_device": device_of(a["report"]) == device_of(b["report"]),
            "reports": [a["report"], b["report"]],
            "byte_identical_responses": sum(1 for p, q in zip(pa, pb)
                                            if p.get("response") == q.get("response")),
            "flips_looped": sum(1 for p, q in zip(pa, pb)
                                if bool((p.get("metrics") or {}).get("looped"))
                                != bool((q.get("metrics") or {}).get("looped"))),
        })
    return {"pairs": out,
            "reading": ("повторный замер того же чекпойнта тем же прибором: "
                        "совпадение побайтово означает, что сессия и день "
                        "результат не двигают")}


# ── зарождение ───────────────────────────────────────────────────────────────

def pairwise(recs: list[dict], metric: str) -> list[dict]:
    """Полная матрица пар по объявленному набору точек — без выбора «удобных».

    Матрица полная намеренно: выбор пар под ответ («сравним 5000 с 9000») и есть
    подгонка. Пары через границу устройств присутствуют, но помечены.
    """
    out = []
    for i, a in enumerate(recs):
        for b in recs[i + 1:]:
            va, vb = a["vectors"][metric], b["vectors"][metric]
            t = T.two_prop_test(sum(va), len(va), sum(vb), len(vb))
            m = T.mdd((sum(va) + sum(vb)) / (len(va) + len(vb)), min(len(va), len(vb)))
            out.append({
                "from": {"tag": a["tag"], "step": a["step"], "k": sum(va), "n": len(va)},
                "to": {"tag": b["tag"], "step": b["step"], "k": sum(vb), "n": len(vb)},
                "delta": t.get("delta"), "p": t.get("p"), "z": t.get("z"),
                "mdd": m,
                "significant": (t.get("p") is not None and t["p"] < ALPHA),
                "beyond_mdd": (t.get("delta") is not None and abs(t["delta"]) > m),
                "same_device": device_of(a["report"]) == device_of(b["report"]),
                "same_session": a["report"] == b["report"],
            })
    return out


def onset_for(recs: list[dict], metric: str, base_tag: str) -> dict:
    """Зарождение по объявленному правилу: присутствие, превышение базы, окно.

    Правила 1–4 шапки применяются механически; ничего не доопределяется по месту.
    """
    base = next(r for r in recs if r["tag"] == base_tag)
    base_k, base_n = base["shares"][metric]["k"], base["shares"][metric]["n"]
    steps = []
    for r in recs:
        k, n = r["shares"][metric]["k"], r["shares"][metric]["n"]
        t = T.two_prop_test(base_k, base_n, k, n)
        m = T.mdd((base_k + k) / (base_n + n), min(base_n, n))
        sig = (t.get("p") is not None and t["p"] < ALPHA)
        delta = t.get("delta")
        steps.append({
            "tag": r["tag"], "step": r["step"], "k": k, "n": n,
            "share": r["shares"][metric]["share"],
            "present": k >= 1,
            "vs_base_p": t.get("p"), "vs_base_delta": delta,
            "vs_base_significant": sig,
            #: Значимость двусторонняя, а «зарождение» — направленное: значимое
            #: ПАДЕНИЕ ниже базы зарождением не является. Без этого разделения
            #: дефект, который на ранних шагах НИЖЕ базы (незакрытые <think>),
            #: объявлялся бы выросшим — поймано фикстурой, а не глазами.
            "above_base": bool(sig and delta is not None and delta > 0),
            "below_base": bool(sig and delta is not None and delta < 0),
            "vs_base_beyond_mdd": (delta is not None and abs(delta) > m),
            "vs_base_mdd": m,
        })
    measured = [s for s in steps if s["tag"] != base_tag]
    first = measured[0] if measured else None
    above = next((s for s in measured if s["above_base"]), None)
    prev = None
    if above is not None:
        idx = measured.index(above)
        prev = measured[idx - 1] if idx > 0 else base
    at_edge = bool(above is not None and not above["vs_base_beyond_mdd"])
    if above is None:
        window = None
        window_note = (f"ни одна точка набора не отличается значимо от {base_tag}: "
                       "окно зарождения этим n не разрешается — это не «дефекта не "
                       "появляется», а «замер не различает»")
    else:
        window = {"lo_step": prev["step"], "hi_step": above["step"],
                  "lo_tag": prev["tag"], "hi_tag": above["tag"],
                  "span_steps": above["step"] - prev["step"],
                  "edge_of_resolution": at_edge,
                  "note": ("границы окна — соседние ИЗМЕРЕННЫЕ точки; между ними "
                           "замеров нет, и точная точка зарождения внутри окна "
                           "этим замером не локализуется")}
        window_note = (f"первая точка выше базы — {above['tag']} "
                       f"(шаг {above['step']}, p = {above['vs_base_p']:.3f}"
                       + (", НА ГРАНИ РАЗРЕШЕНИЯ: разница "
                          f"{above['vs_base_delta']:+.4f} меньше MDD "
                          f"{above['vs_base_mdd']:.4f}" if at_edge else "") + ")")
    return {
        "metric": metric,
        "base": {"tag": base_tag, "k": base_k, "n": base_n,
                 "share": base["shares"][metric]["share"]},
        "by_point": steps,
        "present_at_first_measured": bool(first and first["present"]),
        "first_measured": ({"tag": first["tag"], "step": first["step"],
                            "k": first["k"], "n": first["n"],
                            "share": first["share"], "present": first["present"],
                            "vs_base_p": first["vs_base_p"],
                            "vs_base_significant": first["vs_base_significant"],
                            "above_base": first["above_base"],
                            "below_base": first["below_base"],
                            "vs_base_delta": first["vs_base_delta"],
                            "vs_base_mdd": first["vs_base_mdd"]}
                           if first else None),
        "above_base_at_first_measured": bool(first and first["above_base"]),
        "below_base_at_first_measured": bool(first and first["below_base"]),
        "window": window,
        "window_note": window_note,
        "reading": ("присутствие и превышение базы — разные утверждения (правило 1): "
                    "k ≥ 1 отвечает «есть ли», p < 0.05 — «больше ли, чем было до SFT»"),
    }


# ── блоки по словам ──────────────────────────────────────────────────────────

def blocks_census(origin: dict | None) -> dict:
    """Блоки по словам из свода происхождения — ДРУГОЕ определение, чем доля петель.

    Числа берутся как есть (пер-состояние), плюс разрез «тривиальные/содержательные»
    по признаку прибора (`trivial_text`): блок из знаков пунктуации находится в
    любом тексте и о деградации модели не говорит — тот же разрез, что в S3ar.
    """
    if not origin:
        return {"available": False, "why": "свод происхождения не прочитан"}
    by_state = ((origin.get("blocks") or {}).get("by_state") or {})
    distinct = ((origin.get("blocks") or {}).get("distinct_list") or [])
    rows = []
    for rep, states in by_state.items():
        for tag, v in states.items():
            #: `distinct_list` называет отчёт ИМЕНЕМ ФАЙЛА, а `by_state` — путём:
            #: сравнивать их строками значило бы не найти ни одного блока и молча
            #: получить нули в разрезе (поймано сверкой с уже снятым S3ar, а не
            #: глазами: нули выглядели правдоподобно).
            own = [b for b in distinct if b["state"] == tag
                   and Path(b["report"]).name == Path(rep).name]
            content = [b for b in own if not b.get("trivial_text")]
            rows.append({
                "report": rep, "tag": tag,
                "blocks": v.get("blocks"), "blocks_strong": v.get("blocks_strong"),
                "distinct": v.get("distinct_blocks"),
                "distinct_trivial": len(own) - len(content),
                "distinct_content": len(content),
                "control_units": v.get("control_units"),
                "instrument_loop_share": v.get("instrument_loop_share"),
                "median_tokens": v.get("median_tokens"),
            })
    rows.sort(key=lambda r: (r["tag"]))
    return {
        "available": True,
        "by_state": rows,
        "definition": ("блок = ≥ 8 слов, повторённых в ходе ≥ 2 раз, период любой "
                       "(mild); ярус strong — ≥ 16 слов ИЛИ ≥ 3 повторов ИЛИ период "
                       "≤ 4 (константы прибора происхождения)"),
        "total_mild": (origin.get("blocks") or {}).get("total_mild"),
        "distinct_mild": (origin.get("blocks") or {}).get("distinct_mild"),
        "cpt_axis": ("ось CPT не измерялась: `--cpt` прибору не передавался (вопрос "
                     "не «откуда», а «когда»), и в артефакте она названа неизмеренной"),
    }


def metric_definition_check(recs: list[dict], census: dict) -> dict:
    """Совпадают ли определения: доля петель прибора и число блоков по словам.

    Проверка — числом, а не рассуждением: берётся точка, где расхождение видно
    (доля мала, а блоков много), и названы обе величины. Если определения совпадали
    бы, блоки росли бы вместе с долей; расхождение означает, что «число блоков» —
    описательная величина другого определения, и складывать её с долей нельзя.
    """
    if not census.get("available"):
        return {"available": False, "why": census.get("why")}
    rows = []
    for r in recs:
        c = next((x for x in census["by_state"] if x["tag"] == r["tag"]), None)
        if not c:
            continue
        k = r["shares"]["instrument_looped_share"]["k"]
        rows.append({
            "tag": r["tag"], "step": r["step"],
            "instrument_looped_k": k,
            "instrument_looped_share": r["shares"]["instrument_looped_share"]["share"],
            "blocks_mild": c["blocks"], "blocks_content_distinct": c["distinct_content"],
            "blocks_per_looped_probe": (c["blocks"] / k) if k else None,
        })
    #: Самый резкий контраст — объявленное правило, а не «на глаз»: максимум
    #: отношения «блоков на одну зацикленную пробу». Точка с сотнями блоков и
    #: единицами зацикленных проб и есть место, где два определения расходятся.
    sharpest = max(rows, key=lambda x: x["blocks_per_looped_probe"] or 0) if rows else None
    diverges = bool(rows and any(
        (x["blocks_mild"] or 0) > 100 and (x["instrument_looped_k"] or 0) <= 3
        for x in rows))
    return {
        "available": True,
        "by_point": rows,
        "instrument_metric": ("`metrics.looped` прибора: одна 4-грамма слов повторена "
                              "в генерации ≥ 8 раз (LOOP_MAX4GRAM_REP, S3ak/S3al)"),
        "block_metric": census["definition"],
        "diverges": diverges,
        "divergence_rule": ("расхождение называется, если есть точка, где блоков > 100 "
                            "при зацикленных пробах ≤ 3: плотный счёт блоков при "
                            "нулевой доле петель означает, что величина блока меряет "
                            "не то же самое"),
        "sharpest_contrast": sharpest,
        "verdict": ("определения РАСХОДЯТСЯ: доля петель склеивается с трендом "
                    "(та же метрика прибора), число блоков — описательная величина "
                    "другого определения и читать её как долю петель нельзя"
                    if diverges else
                    "точки расхождения не найдено: на этом наборе обе величины "
                    "ведут себя согласованно, но определения всё равно разные"),
    }


# ── сборка артефакта ─────────────────────────────────────────────────────────

def run_trend(report_specs: list[str], origin_spec: str, ckpt_dir: str | None,
              lo: int, hi: int) -> tuple[dict | None, list[str]]:
    """Доли и тренд — ВЫЗОВОМ инструмента тренда, с сохранением его выхода.

    Не переписанной рядом арифметикой: если бы доля считалась здесь по-своему,
    «та же метрика» держалось бы на честном слове, а не на вызове.
    """
    cmd = [sys.executable, "tools/analyze_loop_trend.py"]
    for spec in report_specs:
        cmd += ["--report", spec]
    if origin_spec and Path(origin_spec).is_file():
        cmd += ["--origin", origin_spec]
    if ckpt_dir:
        cmd += ["--ckpt-dir", ckpt_dir, "--range-lo", str(lo), "--range-hi", str(hi)]
    cmd += ["--no-write"]
    r = subprocess.run(cmd, cwd=str(CASE), capture_output=True, text=True)
    if r.returncode != EXIT_OK:
        note(f"инструмент тренда отказал (код {r.returncode}): {r.stderr[-400:]}")
        return None, cmd, r.stderr
    #: Диагностику инструмента не глотаем: его собственная сверка «сводка отчёта
    #: против проб» — часть проверки определений, и молчание о расхождении читалось
    #: бы как «определения совпали».
    for line in r.stderr.splitlines():
        if line.strip():
            note(f"[тренд] {line.strip()}")
    try:
        return json.loads(r.stdout), cmd, r.stderr
    except json.JSONDecodeError as e:
        note(f"выход инструмента тренда не разобран: {e}")
        return None, cmd, r.stderr


def build(args) -> tuple[dict, int]:
    specs = [EARLY_REPORT] + [f"{LATE_DIR}/report_sft_v13_{s}.json" for s in LATE_STEPS]
    reports = []
    for spec in specs:
        rep, raw = read_json(spec)
        if rep is None:
            note(f"NOT-VERIFIED: не читается отчёт {spec}")
            return {}, EXIT_NOT_VERIFIED
        reports.append((spec, rep))

    recs = measurements(reports)

    comp = comparability(recs)
    if comp["verdict"] != "comparable":
        note("ОТКАЗ: прибор или набор промптов у точек разные — склейка мерила бы "
             "разницу приборов, а не обучения")
        return {}, EXIT_FAIL

    origin, _ = read_json(args.origin) if args.origin else (None, None)
    census = blocks_census(origin)

    anchor_raw = read_raw(ANCHOR)
    if anchor_raw is None:
        note("NOT-VERIFIED: парный контроль S3aq не читается")
        return {}, EXIT_NOT_VERIFIED
    anchor_sha = sha256_text(anchor_raw)
    if anchor_sha != ANCHOR_SHA256:
        note(f"ОТКАЗ: sha256 парного контроля {anchor_sha} ≠ объявленного "
             f"{ANCHOR_SHA256} — это другой вход")
        return {}, EXIT_FAIL
    anchor, _ = read_json(ANCHOR)
    anchor_reports = reports + [(ANCHOR, anchor)]
    anchor_recs = measurements([(ANCHOR, anchor)])

    late_21500 = next(r for r in recs
                      if r["step"] == 21500 and r["report"].startswith(LATE_DIR))
    bridge = device_bridge(anchor, late_21500)

    #: Мерить «когда родились» можно только внутри одного устройства и одной сессии:
    #: набор объявлен константами ONSET_SET / LATE_SET.
    early_recs = [r for r in recs if r["report"] == EARLY_REPORT
                  and r["tag"] in ONSET_SET]
    early_recs.sort(key=lambda r: r["step"])
    late_recs = [r for r in recs if r["report"].startswith(LATE_DIR)]
    late_recs.sort(key=lambda r: r["step"])

    onset = {m: onset_for(early_recs, m, "cfinal") for m in DEFECT_METRICS}
    late = {m: onset_for(late_recs, m, "sft_v13_21500") for m in DEFECT_METRICS}
    #: Матрица — ПОЛНАЯ, вместе с парами через границу устройств: они помечены
    #: (`device_crossed`), а не выброшены, иначе «выбор пар» решал бы ответ.
    matrix = {m: pairwise(recs, m) for m in DEFECT_METRICS}
    defs = metric_definition_check(recs, census)

    trend_json, trend_cmd, trend_notes = run_trend(
        specs, args.origin if args.origin else "",
        args.ckpt_dir, args.grid_lo, args.grid_hi)
    if trend_json is None:
        note("NOT-VERIFIED: тренд по склейке не собран — кривой нет")
        return {}, EXIT_NOT_VERIFIED

    #: Покрытие: что ретенция сняла с диска и чем это восполнено. Числа — из
    #: переписи инструмента тренда, а не из памяти о прогоне.
    inv = trend_json.get("inventory") or {}
    cov = inv.get("coverage") or {}
    lost = (cov.get("probe_points_below_first") or {})
    measured_steps = sorted({r["step"] for r in recs if r["step"]})
    early_steps = sorted({r["step"] for r in early_recs})
    gap_lo, gap_hi = 9000, 21500
    gap_points = [s for s in range(gap_lo + GRID, gap_hi, GRID)
                  if s not in set(measured_steps)]

    curve = []
    blocks_by_tag = {b["tag"]: b for b in (census.get("by_state") or [])}
    for r in sorted(recs, key=lambda x: x["step"]):
        if r["report"] == ANCHOR:
            continue                       # якорь: тот же шаг, что у точки GB10
        blk = blocks_by_tag.get(r["tag"]) or {}
        curve.append({
            "step": r["step"], "tag": r["tag"], "group": group_of(r["report"]),
            "runtime": r["runtime"], "report": r["report"],
            "checkpoint_sha256": r["checkpoint_sha256"],
            "in_trend": r["tag"] in [f"sft_v13_{s}" for s in LATE_STEPS]
                        + ["sft_v13_0500", "sft_v13_5000", "sft_v13_9000"],
            "step_basis": r["step_basis"],
            "metrics": r["shares"],
            #: Блоки по словам — рядом, но отдельным полем: это ДРУГОЕ определение
            #: (см. metric_definition_check), и складывать их с долей нельзя.
            "blocks": {"mild": blk.get("blocks"), "strong": blk.get("blocks_strong"),
                       "distinct": blk.get("distinct"),
                       "distinct_content": blk.get("distinct_content"),
                       "definition": "≥ 8 слов × ≥ 2 раза, период любой"},
            "looped_instances": r["looped_instances"],
        })

    base_reading = {
        "loops_at_500": onset["instrument_looped_share"]["first_measured"],
        "truncations_at_500": onset["truncated_share"]["first_measured"],
        "unclosed_think_at_500": onset["unclosed_think_natural"]["first_measured"],
    }

    inputs = {
        "reports": [input_hash(sp) for sp in specs],
        "origin_census": input_hash(args.origin) if args.origin else None,
        "anchor": {"path": ANCHOR, "exists": True, "bytes": len(anchor_raw.encode("utf-8")),
                   "sha256": anchor_sha, "sha256_source": ANCHOR_SHA_SOURCE},
        "device_check": [
            input_hash("runs/s3ap-format-monitor-20260919/device_check.json"),
            input_hash(ANCHOR.rsplit("/", 1)[0] + "/device_check.json"),
        ],
        "run_record": [input_hash(LATE_DIR + "/chain.sh")],
        "origin_census_tool": "tools/analyze_loop_origin.py",
        "curve_tool": "tools/analyze_loop_trend.py",
    }

    #: Пара «первая измеренная точка кривой против последней» — выбрана ПРАВИЛОМ,
    #: а не под ответ: она отвечает, отличается ли ранняя доля от уровня плато.
    last_rec = max(recs, key=lambda r: r["step"])
    first_step = (onset["instrument_looped_share"].get("first_measured") or {}).get("step")
    edge_pairs = None
    if first_step is not None:
        edge_pairs = next((p for p in matrix["instrument_looped_share"]
                           if p["from"]["step"] == first_step
                           and p["to"]["step"] == last_rec["step"]), None)

    status = "complete" if census.get("available") else "partial"
    data = {
        "schema": "sft-loop-onset/1",
        "tool": "tools/assemble_s3at_onset.py",
        "tool_sha256": sha256_file(Path(__file__)),
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": status,
        "question": ("петли SFT-состояния присутствуют уже к 500 шагам (тогда причина "
                     "не в длительности обучения) или появляются позже (тогда окно "
                     "называется числом) — по всему измеримому диапазону прогона"),
        "command": ("python3 tools/assemble_s3at_onset.py --origin " + ORIGIN +
                    " --ckpt-dir <чекпойнты стадии> --out " + EVIDENCE),
        "trend_command": " ".join(trend_cmd),
        "rules": {
            "presence_vs_base": ("«петли есть» = k ≥ 1; «уровень выше базы» = "
                                 "двухдолевой тест против cfinal, p < 0.05 — это "
                                 "разные утверждения, и первое без второго не сигнал"),
            "window": ("окно зарождения — между последней точкой НЕ выше базы и "
                       "первой выше базы; точная точка внутри окна не локализуется"),
            "resolution": ("рядом печатается MDD (мощность 0.8, α = 0.05, n = 104); "
                           "значимая, но меньшая MDD разница помечается «на грани»"),
            "device_crossing": ("пары через границу устройств называются и в вывод о "
                                "зарождении не идут; цена границы измерена на общем "
                                "чекпойнте (device_bridge)"),
            "block_metric": ("число блоков по словам — другое определение, чем доля "
                             "петель прибора; совпадение проверяется, а не "
                             "предполагается (metric_definition_check)"),
            "unit": "наблюдение — проба (0/1), не блок (то же правило, что у тренда)",
        },
        "inputs": inputs,
        "comparability": comp,
        "device_groups": DEVICE_GROUPS,
        "sets": {
            "onset": {"steps": [r["step"] for r in early_recs], "note": ONSET_SET_NOTE},
            "late": {"steps": [r["step"] for r in late_recs], "note": LATE_SET_NOTE},
        },
        "curve": curve,
        "anchor": {
            "role": ("общий чекпойнт 21 500, снятый вне прогона тренда — даёт и "
                     "второй замер точки, и цену границы устройств"),
            "report": ANCHOR,
            "sha256": anchor_sha,
            "sha256_source": ANCHOR_SHA_SOURCE,
            "points": [{k: v for k, v in r.items()
                        if k not in ("vectors", "_probes")} for r in anchor_recs],
        },
        "base_reading": base_reading,
        "onset": onset,
        "late": late,
        "pairwise": matrix,
        "device_bridge": bridge,
        "same_device_reproducibility": same_device_reproducibility(recs + anchor_recs),
        "blocks_census": census,
        "metric_definition_check": defs,
        "coverage": {
            "measured_steps": measured_steps,
            "early_measured_steps": early_steps,
            "gap": {
                "from_step": gap_lo, "to_step": gap_hi,
                "span_steps": gap_hi - gap_lo,
                "unmeasured_grid_points": gap_points,
                "n_unmeasured_grid_points": len(gap_points),
                "reason": ("точки 500…20 500 сняты ретенцией стадии "
                           "(PROBE_KEEP_LAST=24) до прогона тренда; 500/5000/9000 "
                           "восстановлены из отчёта 19.09 (числа и sha весов), "
                           "остальные замером не покрыты и восстановлению не "
                           "подлежат — чекпойнтов нет"),
                "retention_points_lost": lost.get("count"),
                "retention_range": [lost.get("from"), lost.get("to")],
                "disk_census": {
                    "probe_points_present": inv.get("probe_points_present"),
                    "files_without_step_in_name": inv.get("files_without_step_in_name"),
                },
                "named_not_smoothed": ("разрыв назван, а не сглажен: линия между "
                                       "9000 и 21 500 на графике — интерполяция "
                                       "через незамеренный участок"),
            },
        },
        "curve_trend": {"tool": trend_json.get("tool"),
                        "notes": [l for l in (trend_notes or "").splitlines() if l.strip()],
                        "tool_sha256": trend_json.get("tool_sha256"),
                        "points": trend_json.get("points"),
                        "trend": trend_json.get("trend"),
                        "answer": trend_json.get("answer"),
                        "states_excluded": trend_json.get("states_excluded"),
                        "note": ("выход `tools/analyze_loop_trend.py` на склеенном "
                                 "наборе 12 состояний — доли, вердикты и MDD "
                                 "посчитаны ИМ, не переписаны здесь"),
                        "range_reading": (
                            "тренд на склейке отвечает на вопрос «меняется ли доля "
                            "вдоль ВСЕГО измеримого диапазона» и потому включает рост "
                            "раннего окна (5000 → 9000); вердикт «плато» замера S3as "
                            "относится к отрезку 21500…32600 и с ним не спорит — это "
                            "разные диапазоны одного ряда. Обратно: «растёт» здесь не "
                            "означает «растёт и дальше» — изменение за весь диапазон "
                            f"({(trend_json.get('trend', {}).get('instrument_looped_share', {}).get('change_over_range') or {}).get('point_estimate', float('nan')):+.4f}) "
                            "меньше MDD "
                            f"({(trend_json.get('trend', {}).get('instrument_looped_share', {}).get('mdd_two_points_n104') or {}).get('value', float('nan')):.4f}), "
                            "то есть направление видно, а размер — нет")},
        "answer": onset_answer(onset, late, defs, bridge, gap_lo, gap_hi,
                               edge_pair=edge_pairs),
        "limits": [
            "n = 104 пробы на точку: разница долей меньше MDD (0.19 при p ≈ 0.5, "
            "0.075 при p ≈ 0.04) этим замером не различается в принципе",
            "Ранняя часть снята 19.09 на локальной RTX 4080, поздняя — 21.09 на "
            "стенде GB10: склейка пересекает границу устройств, и её цена измерена "
            "(device_bridge), а не объявлена нулевой",
            "Сегмент 9000…21 500 не покрыт замером: чекпойнты сняты ретенцией, "
            "восстановить нечем",
            "Причинность не измеряется: кривая говорит «доля меняется вместе с "
            "шагом», а не «шаг это вызывает»",
            "Точка внутри окна не локализуется: между соседними измеренными точками "
            "замеров нет",
            "Числа блоков по словам (blocks_census) — ОПИСАТЕЛЬНЫ и другого "
            "определения; единица наблюдения в тестах — проба",
            "Замер стоит на инференсе сохранённых весов, а не на обучении: ни лосса, "
            "ни порядка данных, ни эпох здесь нет",
        ],
        "open_questions": [
            "Вернётся ли дефект позже: прогон остановлен (ADR-050), данных за "
            "32 600 нет",
            "Какой рычаг режима снимет петли: замер стоит внутри одного режима, "
            "сравнения режимов в нём нет",
            "Точная точка зарождения внутри окна: нужна сетка чаще 500 шагов на "
            "участке окна",
        ],
    }
    return data, EXIT_OK


def onset_answer(onset: dict, late: dict, defs: dict, bridge: dict,
                 gap_lo: int, gap_hi: int, edge_pair: dict | None = None) -> dict:
    """Ответ на вопрос замера — выведенный из чисел, а не написанный рядом с ними.

    Каждая ветка обязана назвать довод, который она снимает или даёт. Ветка
    «на 500 петли есть, но не выше базы» — не «петли есть» и не «петель нет»:
    она говорит, что ранняя часть кривой лежит в пределах, которые база уже
    занимает, и что появления «с нуля» в первых сотнях шагов не видно.
    """
    loop = onset["instrument_looped_share"]
    trunc = onset["truncated_share"]
    uncl = onset["unclosed_think_natural"]
    first = loop["first_measured"]
    win = loop["window"]

    if first is None:
        loops_shape = "no_data"
    elif first["above_base"]:
        loops_shape = "already_above_base"
    elif first["present"]:
        loops_shape = "present_not_above_base"
    else:
        loops_shape = "absent_at_first_measured"

    if loops_shape == "present_not_above_base":
        loops_text = (
            f"Петли на 500 шагах ЕСТЬ: {first['k']}/{first['n']} проб "
            f"({first['share']:.4f}), но доля НЕ отличается от базы "
            f"(p = {first['vs_base_p']:.3f}; база — "
            f"{loop['base']['k']}/{loop['base']['n']}). Устойчивое превышение базы "
            + (f"появляется к {win['hi_tag']} (шаг {win['hi_step']}, p = "
               f"{loop['by_point'][-1]['vs_base_p']:.3f}), то есть окно "
               f"({win['lo_step']}, {win['hi_step']}]"
               if win else "не появляется ни на одной точке набора")
            + (f". От последней измеренной точки кривой (шаг "
               f"{edge_pair['to']['step']}) доля на 500 тоже не отличима: "
               f"{first['k']}/{first['n']} против {edge_pair['to']['k']}/"
               f"{edge_pair['to']['n']}, p = {edge_pair['p']:.3f}"
               + (" — пара через границу устройств, и её разница включает цену "
                  "границы, а не только шаг" if not edge_pair["same_device"] else "")
               if edge_pair else "")
            + ". Читать это надо так: в первых сотнях шагов доля стоит на уровне базы, "
              "но при n = 104 замер не различает ни «выросла», ни «не было» — "
              "отличимой от базы оказывается одна точка (9000), и потому окно названо "
              "промежутком между соседними ИЗМЕРЕННЫМИ точками, а не точкой. "
              "«Причина не в длительности» отсюда НЕ следует: доля на 500 не отличима "
              "и от уровня плато, то есть на 500 замер уже допускает то значение, "
              "которое держится до 32 600"
            + f" (поздняя часть: {late['instrument_looped_share']['window_note']})")
    elif loops_shape == "already_above_base":
        loops_text = (f"Петли на 500 шагах выше базы: {first['k']}/{first['n']} против "
                      f"{loop['base']['k']}/{loop['base']['n']} (p = "
                      f"{first['vs_base_p']:.3f}) — гипотеза «режим обучения» этим "
                      "не подтверждается: дефект виден уже в начале стадии")
    elif loops_shape == "absent_at_first_measured":
        loops_text = "Петель на 500 шагах нет ни в одной пробе"
    else:
        loops_text = "Ранних точек нет — вопрос о зарождении этим сводом не решается"

    def _shape(metric: dict) -> str:
        f = metric["first_measured"]
        if f is None:
            return "нет ранней точки"
        if f["above_base"]:
            return (f"выше базы уже на {f['step']} ({f['k']}/{f['n']}, p = "
                    f"{f['vs_base_p']:.3f})")
        if f["below_base"]:
            return (f"НИЖЕ базы на {f['step']} ({f['k']}/{f['n']} против "
                    f"{metric['base']['k']}/{metric['base']['n']}, p = "
                    f"{f['vs_base_p']:.3f}) — в первые шаги дефект не вырос, а упал")
        if f["present"]:
            return (f"присутствует ({f['k']}/{f['n']}), но не отличимо от базы (p = "
                    f"{f['vs_base_p']:.3f})")
        return "отсутствует"

    dev = bridge.get("flips", {}).get("looped")
    return {
        "loops_present_at_500": bool(first and first["present"]),
        "loops_above_base_at_500": bool(first and first["vs_base_significant"]),
        "loops_at_500": first,
        "loops_base": loop["base"],
        "loops_onset_window": win,
        "loops_verdict": loops_shape,
        "loops_text": loops_text,
        "truncations_at_500": {"verdict": _shape(trunc), "point": trunc["first_measured"]},
        "unclosed_think_at_500": {"verdict": _shape(uncl), "point": uncl["first_measured"]},
        "defects_differ_in_onset": {
            "truncations": _shape(trunc),
            "unclosed_think": _shape(uncl),
            "reading": ("начала у трёх дефектов разные, и это видно числами: усечения "
                        "встают на первой же точке, незакрытые <think> (среди "
                        "естественно завершённых) на ранних шагах НИЖЕ базы, петли — "
                        "на уровне базы; «одна причина на все три» этим сводом не "
                        "подтверждается"),
        },
        "metric_definitions_diverge": defs.get("diverges"),
        "metric_definitions_note": (
            ("число блоков по словам и доля петель прибора — разные определения, и на "
             "ранних точках они расходятся: "
             + (f"{defs['sharpest_contrast']['tag']} — "
                f"{defs['sharpest_contrast']['blocks_mild']} блоков при "
                f"{defs['sharpest_contrast']['instrument_looped_k']}/104 зацикленных "
                "пробах" if defs.get("sharpest_contrast") else "точка контраста не названа")
             + "; ответ о зарождении даётся по доле петель, число блоков — описательно")
            if defs.get("diverges") else
            "расхождения на этом наборе не найдено, но определения разные"),
        "implication_for_choice": (
            "«Доучивать»: этим сводом не подтверждается. Уровень дефекта набирается "
            f"в окне ({win['lo_step']}, {win['hi_step']}] и держится до 32 600 "
            "неизменным (тренд S3as — плато), то есть продолжение того же режима не "
            "обещает самоустранения. «Менять режим»: довод НЕ усиливается, а "
            "смещается — раз петли появляются не с первых шагов, а в окне, спор идёт "
            "о том, что в этом окне меняется (темп, расписание, накопление), а не о "
            "том, что набор «отравлен с нуля». Сравнения режимов в этом замере нет — "
            "он стоит внутри одного режима."
            if win else
            "Выбор плана этим сводом не решается: окно зарождения при n = 104 не "
            "разрешено, а «нет окна» здесь означает «замер не различает»."),
        "device_boundary_cost": (
            ("цена границы устройств измерена на общем чекпойнте 21 500: "
             f"{bridge.get('byte_identical_responses')}/{bridge.get('n')} ответов "
             f"совпали побайтово, флаг петель разошёлся на {dev} пробах "
             f"({(bridge.get('flips_share') or {}).get('looped', 0):.4f}) — поэтому "
             "пары через границу в вывод о зарождении не идут")
            if bridge.get("available") else "границу устройств измерить не удалось"),
        "gap_named": (f"сегмент {gap_lo}…{gap_hi} замером не покрыт (чекпойнты сняты "
                      "ретенцией): кривая между этими точками — интерполяция, а не "
                      "измерение"),
    }


def selftest() -> int:
    """Фикстуры правил: без них «зарождение» — это то, что инструмент сказал."""
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'ok ' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    def rec(tag, step, rep, looped_k, n=104, trunc_k=0, uncl_k=0, uncl_n=None):
        vec_loop = [1] * looped_k + [0] * (n - looped_k)
        vec_trunc = [1] * trunc_k + [0] * (n - trunc_k)
        m = uncl_n if uncl_n is not None else n
        vec_uncl = [1] * uncl_k + [0] * (m - uncl_k)
        vectors = {"instrument_looped_share": vec_loop, "truncated_share": vec_trunc,
                   "unclosed_think_natural": vec_uncl}
        return {"report": rep, "tag": tag, "step": step, "n_probes": n,
                "checkpoint_sha256": None, "_probes": [],
                "vectors": vectors,
                "shares": {k: {"k": sum(v), "n": len(v), "share": sum(v) / len(v),
                               "ci95": T.wilson(sum(v), len(v))}
                           for k, v in vectors.items()}}

    early = "runs/s3ap-format-monitor-20260919/format_wide_standard.json"
    late = "runs/sft-loop-trend-20260921/report_sft_v13_21500.json"
    DEVICE_GROUPS["__fixture_early"] = {"reports": [early]}
    DEVICE_GROUPS["__fixture_late"] = {"reports": [late]}

    # (а) «есть» и «выше базы» — разные утверждения (правило 1). Числа взяты из
    #     настоящей ранней части: 2/104 против базы 1/104 неразличимы.
    rs = [rec("cfinal", 0, early, 1, trunc_k=5, uncl_k=33, uncl_n=99),
          rec("sft_v13_0500", 500, early, 2, trunc_k=31, uncl_k=13, uncl_n=73),
          rec("sft_v13_5000", 5000, early, 1, trunc_k=46, uncl_k=30, uncl_n=58),
          rec("sft_v13_9000", 9000, early, 7, trunc_k=55, uncl_k=34, uncl_n=49)]
    early_onset = {m: onset_for(rs, m, "cfinal") for m in DEFECT_METRICS}
    o = early_onset["instrument_looped_share"]
    check("зарождение: на 500 петли ЕСТЬ (k ≥ 1)", o["present_at_first_measured"] is True)
    check("зарождение: но НЕ выше базы (p ≥ 0.05)",
          o["above_base_at_first_measured"] is False)
    check("зарождение: окно — (5000, 9000]",
          o["window"] and (o["window"]["lo_step"], o["window"]["hi_step"]) == (5000, 9000))
    check("зарождение: окно помечено «на грани разрешения» (Δ < MDD)",
          o["window"]["edge_of_resolution"] is True)
    check("зарождение: усечения выше базы УЖЕ на 500",
          early_onset["truncated_share"]["above_base_at_first_measured"] is True)
    uncl_o = early_onset["unclosed_think_natural"]
    check("зарождение: незакрытые на 500 НЕ выше базы",
          uncl_o["above_base_at_first_measured"] is False)
    check("зарождение: значимое ПАДЕНИЕ ниже базы не выдаётся за зарождение",
          uncl_o["below_base_at_first_measured"] is True)

    # (б) Кривая без роста: окно не выдумывается, и это называется разрешением,
    #     а не «петель не появляется».
    flat = [rec("cfinal", 0, early, 1), rec("sft_v13_0500", 500, early, 2),
            rec("sft_v13_5000", 5000, early, 1), rec("sft_v13_9000", 9000, early, 2)]
    of = onset_for(flat, "instrument_looped_share", "cfinal")
    check("зарождение: без роста окно не выдумывается", of["window"] is None)
    check("зарождение: отсутствие окна названо разрешением, а не «петель нет»",
          "не различает" in of["window_note"])

    # (в) Полная матрица пар: и «удобные», и неудобные; пары через границу
    #     устройств помечены, а не выброшены.
    mixed = [rec("cfinal", 0, early, 1), rec("sft_v13_0500", 500, early, 2),
             rec("sft_v13_21500", 21500, late, 6), rec("sft_v13_32600", 32600, late, 6)]
    mat = pairwise(mixed, "instrument_looped_share")
    check("пары: матрица полная (n(n-1)/2)", len(mat) == 6)
    cross = [p for p in mat if not p["same_device"]]
    check("пары: пересекающие устройства названы, а не выброшены", len(cross) > 0)
    check("пары: 500 против 21 500 помечена как межмашинная",
          any(p["from"]["step"] == 500 and p["to"]["step"] == 21500
              and not p["same_device"] for p in mat))
    check("пары: внутри сессии помечены same_session",
          all(p["same_session"] for p in mat if p["from"]["step"] in (21500,)
              and p["to"]["step"] in (32600,)))

    # (г) Границы устройств: повторный замер того же чекпойнта на том же
    #     устройстве обязан читаться как совпадение, а не как расхождение.
    rp = same_device_reproducibility([
        {**rec("cfinal", 0, early, 1), "checkpoint_sha256": "aa"},
        {**rec("cfinal", 0, early, 1), "checkpoint_sha256": "aa"},
        {**rec("cfinal", 0, late, 1), "checkpoint_sha256": "aa"}])
    check("повтор: пара по одному sha найдена",
          len(rp["pairs"]) == 1 and rp["pairs"][0]["same_device"] is True)

    # (д) Определения метрик: расхождение «блоки против доли» обязано быть
    #     замечено — на 500 блоков много, а доля на уровне базы.
    census = {"available": True, "by_state": [
        {"tag": "sft_v13_0500", "blocks": 399, "distinct_content": 72},
        {"tag": "sft_v13_9000", "blocks": 191, "distinct_content": 54}],
        "definition": "блок = ≥ 8 слов × ≥ 2 раза"}
    d = metric_definition_check(
        [rec("sft_v13_0500", 500, early, 2), rec("sft_v13_9000", 9000, early, 7)], census)
    check("определения: расхождение «блоки против доли» замечено", d["diverges"] is True)
    d2 = metric_definition_check(
        [rec("sft_v13_9000", 9000, early, 7)], {**census, "by_state": [
            {"tag": "sft_v13_9000", "blocks": 191, "distinct_content": 54}]})
    check("определения: без расхождения вердикт не выдумывает расхождение",
          d2["diverges"] is False)

    # (е) Ответ выводится из чисел: ветка «есть, но не выше базы» не выдаётся ни за
    #     «петель нет», ни за «уже выше базы».
    lateo = {m: onset_for([rec("sft_v13_21500", 21500, late, 6),
                           rec("sft_v13_32600", 32600, late, 6)], m, "sft_v13_21500")
             for m in DEFECT_METRICS}
    ans = onset_answer(early_onset, lateo, {"diverges": True},
                       {"available": True, "n": 104,
                                                      "byte_identical_responses": 57,
                                                      "flips": {"looped": 4},
                                                      "flips_share": {"looped": 4 / 104}},
                       9000, 21500)
    check("ответ: петли на 500 названы ЕСТЬ", ans["loops_present_at_500"] is True)
    check("ответ: превышение базы на 500 названо НЕТ",
          ans["loops_above_base_at_500"] is False)
    check("ответ: окно названо числом",
          ans["loops_onset_window"]["hi_step"] == 9000)
    check("ответ: расхождение определений названо", ans["metric_definitions_diverge"] is True)
    check("ответ: цена границы устройств названа числом",
          "4/104" in ans["device_boundary_cost"] or "4 " in ans["device_boundary_cost"])
    check("ответ: разрыв назван, а не сглажен", "интерполяция" in ans["gap_named"])

    # (ж) Отказ на разных приборах и на разных наборах промптов: склейка из разных
    #     приборов мерила бы прибор, а не обучение.
    r1 = rec("sft_v13_0500", 500, early, 2)
    r1.update({"instrument_sha256": "aaa", "prompts_digest": "d1", "prompts_set": "wide",
               "max_new_tokens": 4096, "batch_size": 8, "stop_at_turn_end": True,
               "decoding": {}, "runtime_marker": "/home/user/.cache"})
    r2 = rec("sft_v13_9000", 9000, early, 7)
    r2.update({"instrument_sha256": "bbb", "prompts_digest": "d1", "prompts_set": "wide",
               "max_new_tokens": 4096, "batch_size": 8, "stop_at_turn_end": True,
               "decoding": {}, "runtime_marker": "/root/.cache"})
    check("сопоставимость: разные приборы — not_comparable",
          comparability([r1, r2])["verdict"] == "not_comparable")
    r3 = dict(r2, instrument_sha256="aaa", prompts_digest="d2")
    check("сопоставимость: разные наборы промптов — not_comparable",
          comparability([r1, r3])["verdict"] == "not_comparable")
    r4 = dict(r2, instrument_sha256="aaa", prompts_digest="d1")
    check("сопоставимость: один прибор и набор — comparable",
          comparability([r1, r4])["verdict"] == "comparable")

    print("selftest:", "OK" if ok else "FAIL")
    return EXIT_OK if ok else EXIT_FAIL


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origin", default=ORIGIN, help="свод блоков прибора происхождения")
    ap.add_argument("--ckpt-dir", default=None,
                    help="каталог чекпойнтов стадии для переписи диска (называет потерю)")
    ap.add_argument("--grid-lo", type=int, default=500)
    ap.add_argument("--grid-hi", type=int, default=32600)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    data, code = build(args)
    if code != EXIT_OK:
        return code
    if args.out and not args.no_write:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
        note(f"артефакт записан: {args.out}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
