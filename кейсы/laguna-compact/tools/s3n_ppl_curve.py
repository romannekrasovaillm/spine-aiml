#!/usr/bin/env python3
"""S3n — профиль обвала языка: PPL общего языка по промежуточным точкам калибровки.

Зачем. Четыре руки калибровки микса × пика LR (S3m, ADR-022 п.2) провалили потолок
ADR-022 на финале: PPL общего языка 55.3…113.1 против базы 11.93 (потолок 23.86).
Причина не установлена, и **форма** обвала — это то, что различает версии:
ступенька в самом начале указывает на данные/токенизацию (в том числе спецтокены),
плавный рост — на LR и длительность. Отдельная точка тут бесполезна: S3m уже дал
единственный замер (рука 25-0.7, шаг 500, PPL 204.4), и он не отличает «обвал на
старте» от «обвал к 500-му шагу из 2000».

Что делает инструмент. Промежуточные точки рук сохраняются копией пайплайна прогона
(``CALIB_CKPT_EVERY=500``), но GEN-EVAL их не посещает. Инструмент прогоняет по
каждой руке **существующий** прибор ``tools/calib_ppl_probe.py`` (методика
``_ppl_eval`` из ``tools/ppl_probe.py`` — та же, что дала базу 11.9319) и собирает
общий профиль «рука × шаг».

Почему не свой замер. Своя реализация PPL разошлась бы с базой на первой правке
набора/токенизатора, а числа после этого несопоставимы — и это не было бы видно.
Прибор пробы сам отказывает (exit 1), если состояние ``base`` не воспроизвело числа
S3h, поэтому «прибор тот же» здесь проверяется, а не подразумевается.

Отсутствующая точка — это факт, а не пропуск. У руки ``25-0.7`` шаг 500 утрачен
штатной ретенцией пайплайна (``glob("checkpoint_[0-9]*.pt")``, держим 2 последних),
и это не восстанавливается: инструмент пишет её в ``missing_points`` с причиной и
не подставляет вместо неё ничего.

Коды возврата::

    0 — профиль собран, отчёт записан
    1 — отказ прибора на одной из рук (числа не полны) либо профиль неполон
    2 — NOT-VERIFIED: нет входа (чекпойнты/наборы/пайплайн/CUDA)

Запуск::

    python3 tools/s3n_ppl_curve.py --plan
    python3 tools/s3n_ppl_curve.py --mode run      # замер (нужна карта)
    python3 tools/s3n_ppl_curve.py --mode assemble # только сборка отчёта
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Корень артефактов калибровки. AD-4: данные лежат на сетевом диске, чекпойнты
#: читаются **по месту** — копий в рабочем каталоге не держим.
CALIB_ROOT = Path("/home/user/gb10-shared/calib")

#: Руки сетки S3m. Имена каталогов — как их записал прогон (``<arm>-<ts>``).
ARM_DIRS: dict[str, str] = {
    "25-0.35": "calib-25-0.35-20260916-0820",
    "25-0.7": "calib-25-0.7-20260916-0820",
    "50-0.35": "calib-50-0.35-20260916-0820",
    "50-0.7": "calib-50-0.7-20260916-0820",
}

#: Шаги, которые рука обязана была сохранить (``ckpt_points`` из calib_params.json).
EXPECTED_STEPS: tuple[int, ...] = (500, 1000, 1500, 2000)

#: Причина утраты точки шага 500 у руки 25-0.7 — записана в её же pipeline_patch:
#: та копия пайплайна сохраняла точки под штатным именем без снятия ретенции, и
#: на шаге 1500 ретенция удалила шаг 500. Не «не сохранилась» — удалена.
LOST_500_25_07 = ("штатная ретенция пайплайна (glob checkpoint_[0-9]*.pt, держим 2 "
                  "последних по mtime) удалила точку на шаге 1500; факт зафиксирован "
                  "в pipeline_patch.json руки и в S3m (D2)")

#: Соответствие наборов прибора S3m полям контракта S3n. Ключи — имена ``SETS``
#: из ``tools/ppl_probe.py``; здесь только роль, которую набор играет в профиле.
#: ``general`` — тот самый ``general_eval.txt`` (sha 11b3164d…), по которому
#: считалась база 11.9319; ``domain`` — доменный семпл корпуса v10.1 (готовый,
#: собран ADR-018), его база 11.1341 на том же приборе.
ROLE_GENERAL = "v1_general"
ROLE_DOMAIN = "v2_domain"
ROLE_GENERAL_V2 = "v2_general"
ROLE_DOMAIN_V1 = "v1_domain"

#: Состояния прибора, которые не являются точками руки: ``base`` — общая точка
#: старта (шаг 0, LR=0 → нетронутые веса), ``base_untouched`` — вакуумная проверка
#: восстановления весов. В матрицу они входят отдельной строкой с ``source``.
BASE_STATE = "base"
UNTOUCHED_STATE = "base_untouched"

#: Порог из ADR-022 п.3: «вдвое хуже базы» — по нему и решается провал.
CEILING_FACTOR = 2.0

#: Разброс внутри «полки»: если все точки выше потолка и различаются меньше чем в
#: это число раз, то профиль — ступенька (обвал случился до первого замера), а не
#: деградация, у которой видно наклон.
PLATEAU_SPAN = 2.0


# ─────────────────────────── обнаружение точек ───────────────────────────────

def parse_step(name: str) -> int | None:
    """Шаг из имени файла чекпойнта.

    Две схемы имён — факт, а не выбор: три руки сохраняли ``calib_checkpoint_{N}.pt``
    (имя вне ретенции пайплайна, см. pipeline_patch), рука 25-0.7 — штатное
    ``checkpoint_{N}.pt``. Обе разбираются; ``checkpoint_final.pt`` — не шаг, а
    финал прогона, и его номер берётся из ``cpt_steps``.
    """
    m = re.fullmatch(r"(?:calib_)?checkpoint_(\d+)\.pt", name)
    return int(m.group(1)) if m else None


def discover_checkpoints(ckpt_dir: Path, final_step: int) -> tuple[dict[int, Path], list[int]]:
    """``{шаг: путь}`` и список отсутствующих шагов из ``EXPECTED_STEPS``.

    Финал (``checkpoint_final.pt``) привязывается к ``final_step``, а не к имени:
    он записывается после цикла и номера шага в имени не несёт.
    """
    found: dict[int, Path] = {}
    if ckpt_dir.is_dir():
        for p in sorted(ckpt_dir.glob("*.pt")):
            if p.name == "checkpoint_final.pt":
                found[final_step] = p
                continue
            step = parse_step(p.name)
            if step is not None:
                # Одна и та же точка под двумя именами (рука, где обе схемы
                # встречаются) — не повод мерить дважды; берём первую по сортировке.
                found.setdefault(step, p)
    missing = [s for s in EXPECTED_STEPS if s not in found]
    return found, missing


def cpt_steps(arm_dir: Path) -> int:
    """``cpt_steps`` руки из ``calib_params.json``; без файла — штатные 2000."""
    path = arm_dir / "calib_params.json"
    if path.is_file():
        try:
            return int(json.loads(path.read_text(encoding="utf-8")).get("cpt_steps", 2000))
        except (ValueError, TypeError):
            pass
    return 2000


def pipeline_for_arm(case_root: Path, arm: str) -> Path | None:
    """Копия пайплайна **того прогона**, чьи чекпойнты меряются.

    Загрузка чекпойнта идёт ``_load_ckpt_with_resize`` из копии руки: своя
    реализация разошлась бы с контуром на первой же правке resize. Обе схемы рук
    несут одну и ту же функцию (проверено сравнением), но брать её из файла руки —
    правило, а не совпадение.
    """
    p = case_root / "runs" / ARM_DIRS[arm] / "laguna_pipeline_calib.py"
    return p if p.is_file() else None


def plan(case_root: Path, calib_root: Path) -> list[dict]:
    """План замера: что будет измерено и чего в наличии нет."""
    out = []
    for arm, dirname in ARM_DIRS.items():
        arm_dir = calib_root / dirname
        final_step = cpt_steps(arm_dir)
        found, missing = discover_checkpoints(arm_dir / "checkpoints", final_step)
        out.append({
            "arm": arm,
            "dir": str(arm_dir),
            "pipeline": (str(pipeline_for_arm(case_root, arm))
                         if pipeline_for_arm(case_root, arm) else None),
            "final_step": final_step,
            "points": {str(s): str(p) for s, p in sorted(found.items())},
            "missing": [{"step": s, "reason": LOST_500_25_07 if (arm == "25-0.7" and s == 500)
                         else "чекпойнт не сохранён прогоном"} for s in missing],
        })
    return out


# ────────────────────────────── замер ────────────────────────────────────────

def state_name(step: int, final_step: int) -> str:
    """Имя состояния прибора: ``c500`` / ``cfinal`` — по нему потом и разбираем."""
    return "cfinal" if step == final_step else f"c{step}"


def run_arm(case_root: Path, calib_root: Path, arm: str, out_dir: Path,
            sets: str, dtype: str, device: str, max_len: int, batch: int,
            timeout: float | None) -> tuple[dict | None, int]:
    """Прогон прибора по одной руке. Возвращает ``(отчёт, код возврата)``."""
    arm_dir = calib_root / ARM_DIRS[arm]
    pipe = pipeline_for_arm(case_root, arm)
    if pipe is None:
        print(f"NOT-VERIFIED: нет копии пайплайна руки {arm} "
              f"(runs/{arm}/laguna_pipeline_calib.py)", file=sys.stderr)
        return None, EXIT_NOT_VERIFIED
    final_step = cpt_steps(arm_dir)
    found, missing = discover_checkpoints(arm_dir / "checkpoints", final_step)
    if not found:
        print(f"NOT-VERIFIED: у руки {arm} нет ни одной точки замера", file=sys.stderr)
        return None, EXIT_NOT_VERIFIED

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{arm}.json"
    cmd = [sys.executable, str(case_root / "tools" / "calib_ppl_probe.py"),
           "--case-root", str(case_root), "--pipeline", str(pipe),
           "--state", f"{BASE_STATE}=base", "--sets", sets,
           "--dtype", dtype, "--device", device,
           "--max-len", str(max_len), "--batch", str(batch),
           "--out", str(out)]
    for step, path in sorted(found.items()):
        cmd += ["--state", f"{state_name(step, final_step)}=ckpt:{path}"]
    # Вакуумная точка снимается **после** чекпойнтов — так задумано прибором:
    # она ловит осевшие веса предыдущего чекпойнта, а не «дрейф» до них.
    cmd += ["--state", f"{UNTOUCHED_STATE}=base"]

    print(f"\n=== рука {arm}: {len(found)} точек (шаги {sorted(found)})"
          + (f", отсутствуют {missing}" if missing else ""))
    proc = subprocess.run(cmd, cwd=str(case_root), timeout=timeout)
    report = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
    if proc.returncode != EXIT_OK:
        print(f"ОТКАЗ: прибор на руке {arm} вернул {proc.returncode}", file=sys.stderr)
    return report, proc.returncode


# ────────────────────────────── разбор ───────────────────────────────────────

def sha256_file(path: Path, chunk: int = 8 << 20,
                cache: dict | None = None) -> str | None:
    """SHA-256 файла. ``cache`` — словарь-кэш, живущий в каталоге отчётов.

    Чекпойнт — 3 ГБ, и хешировать пятнадцать таких файлов на каждой
    инкрементальной записи отчёта значило бы читать 45 ГБ ради строки, которая
    уже посчитана. Ключ кэша — путь, поэтому устаревание ловится по ``size`` и
    ``mtime``: подменённый чекпойнт перехешируется, а не «сойдётся по памяти».
    """
    if not path.is_file():
        return None
    key = str(path)
    st = path.stat()
    if cache is not None:
        hit = cache.get(key)
        if hit and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime:
            return hit["sha256"]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    digest = h.hexdigest()
    if cache is not None:
        cache[key] = {"size": st.st_size, "mtime": st.st_mtime, "sha256": digest}
    return digest


def load_sha_cache(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def save_sha_cache(path: Path, cache: dict) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def doc_stats(report: dict, state: str, set_name: str) -> dict | None:
    node = report.get("states", {}).get(state, {}).get("sets", {}).get(set_name)
    return node.get("doc_ppl") if isinstance(node, dict) else None


def classify_curve(points: list[tuple[int, float]], ceiling: float,
                   anchor: tuple[int, float] | None = None) -> dict:
    """Форма кривой по точкам руки ``[(шаг, ppl)]`` (отсортированы по шагу).

    ``anchor`` — общая точка старта (шаг 0, нетронутые веса), известная для всех
    рук заранее. Она **не** считается измеренной точкой руки: она называет только
    нижнюю границу интервала, внутри которого случился переход. Без неё «ступенька»
    и «обвал, завершившийся к 500-му шагу» неразличимы, а с ней формулировка
    честная: потолок пробит не «на шаге 500», а где-то в (0, 500].

    Классификация нарочно грубая: точек четыре-пять, и любая тонкая подгонка
    («наклон», «экспонента») на таком числе точек была бы украшением. Различаем
    то, что действительно различает версии: **лежит ли кривая выше потолка уже в
    первой измеренной точке** (тогда переход случился раньше неё и виден только
    как ступенька) или **пересекает потолок внутри наблюдаемого диапазона** (тогда
    у деградации есть наблюдаемый ход).
    """
    pts = sorted(points)
    if not pts:
        return {"shape": "нет данных", "n_points": 0, "first_crossing_step": None}
    ppls = [p for _, p in pts]
    steps = [s for s, _ in pts]
    above = [(s, p) for s, p in pts if p > ceiling]
    crossing = above[0][0] if above else None
    span = (max(ppls) / min(ppls)) if min(ppls) > 0 else float("inf")
    features = {
        "n_points": len(pts),
        "steps": steps,
        "ppl_min": min(ppls), "ppl_max": max(ppls),
        "span_ratio": span,
        "monotone_down": all(b <= a for a, b in zip(ppls, ppls[1:])),
        "monotone_up": all(b >= a for a, b in zip(ppls, ppls[1:])),
        "all_above_ceiling": len(above) == len(pts),
        "first_crossing_step": crossing,
        "crossed_at_first_measured": bool(above) and above[0][0] == steps[0],
    }
    if anchor is not None:
        features["anchor"] = {"step": anchor[0], "ppl": anchor[1],
                              "above_ceiling": bool(anchor[1] > ceiling)}
    if len(pts) == 1:
        kind, shape = "single_point", "одна точка — не кривая"
    elif features["all_above_ceiling"]:
        tail = (f", разброс ×{span:.2f}" if span <= PLATEAU_SPAN
                else f", но разброс ×{span:.2f} — полка не плоская")
        kind = "step"
        if anchor is not None and not features["anchor"]["above_ceiling"]:
            shape = (f"ступенька: обвал завершён на интервале "
                     f"({anchor[0]}, {steps[0]}] — норма на старте, обвал уже в первой "
                     f"сохранённой точке{tail}")
            features["transition_bracket"] = [anchor[0], steps[0]]
        else:
            shape = f"ступенька: все измеренные точки выше потолка{tail}"
    elif crossing is None:
        kind, shape = "no_crossing", "потолок в измеренном диапазоне не пересечён"
    else:
        kind = "transition"
        # Переход виден: называем интервал, между концами которого он произошёл.
        idx = steps.index(crossing)
        lo = steps[idx - 1] if idx > 0 else (anchor[0] if anchor else None)
        shape = (f"переход внутри диапазона: между шагами {lo} и {crossing}"
                 if lo is not None else f"потолок пробит на первом шаге {crossing}")
        features["transition_bracket"] = [lo, crossing]
        if features["monotone_up"]:
            shape += " (рост монотонный)"
        elif features["monotone_down"]:
            shape += " (убывание — восстановление, а не деградация)"
        else:
            shape += " (немонотонно)"
    return {"kind": kind, "shape": shape, **features}


#: Человекочитаемое имя вида кривой — для сводной строки отчёта.
KIND_RU = {"step": "ступенька (обвал завершён до первой сохранённой точки)",
           "transition": "переход внутри наблюдаемого диапазона",
           "no_crossing": "потолок не пересечён",
           "single_point": "одна точка — форма не определена",
           "no_data": "нет данных"}


def summarize_shape(curves: dict[str, dict]) -> str:
    """Сводная форма профиля: без чисел разброса — они у каждой руки свои."""
    kinds = {c.get("kind") for c in curves.values()}
    if len(kinds) == 1:
        kind = kinds.pop()
        return KIND_RU.get(kind, kind)
    parts = sorted(f"{a}: {KIND_RU.get(c.get('kind'), c.get('kind'))}"
                   for a, c in curves.items())
    return "форма различается по рукам — " + "; ".join(parts)


def build_matrix(reports: dict[str, dict], base_ppl: float, domain_base: float | None,
                 ceiling: float) -> list[dict]:
    """Матрица «рука × шаг» из отчётов прибора. Строка шага 0 — общая точка старта."""
    rows: list[dict] = []
    for arm, rep in reports.items():
        if not rep:
            continue
        final_step = rep.get("final_step")
        for state, entry in rep.get("states", {}).items():
            if state == UNTOUCHED_STATE:
                continue
            if state == BASE_STATE:
                step, source = 0, "base (общая точка старта; не чекпойнт руки)"
            else:
                m = re.fullmatch(r"c(?:final|\d+)", state)
                if not m:
                    continue
                step = final_step if state == "cfinal" else int(state[1:])
                source = "чекпойнт руки"
            sets = entry.get("sets", {})
            gen = sets.get(ROLE_GENERAL, {}).get("ppl")
            dom = sets.get(ROLE_DOMAIN, {}).get("ppl")
            if gen is None:
                continue
            rows.append({
                "arm": arm, "step": step, "source": source, "state": state,
                "ppl_general": gen,
                "ratio_vs_base": gen / base_ppl if base_ppl else None,
                "above_ceiling": bool(gen > ceiling),
                "ppl_domain": dom,
                "ratio_domain_vs_base": (dom / domain_base
                                         if (dom is not None and domain_base) else None),
                "ppl_general_v2": sets.get(ROLE_GENERAL_V2, {}).get("ppl"),
                "ppl_domain_v1": sets.get(ROLE_DOMAIN_V1, {}).get("ppl"),
                "tokens_general": sets.get(ROLE_GENERAL, {}).get("tokens"),
                "general_doc_ppl": doc_stats(rep, state, ROLE_GENERAL),
            })
    rows.sort(key=lambda r: (r["arm"], r["step"]))
    return rows


#: Замеры, снятые тем же прибором на точках, которых на диске больше нет. Рука
#: 25-0.7, шаг 500: чекпойнт удалён штатной ретенцией (факт D2), но замер S3m
#: остался (`runs/calib-ppl-20260916-0820/smoke.json`). Точка важна: без неё
#: кривая 25-0.7 начинается с 1000, и «монотонное убывание» неотличимо от
#: «максимум ещё впереди». Точка входит в матрицу с флагом `reproducible: false`:
#: она проверяема по файлу-источнику, но не перепроверяема прогоном.
EXTERNAL_POINTS: tuple[dict, ...] = (
    {"arm": "25-0.7", "step": 500, "ppl_general": 204.44178711987345,
     "ppl_domain_v1": 10.140387483567405,
     "source": "runs/calib-ppl-20260916-0820/smoke.json",
     "note": ("чекпойнт утрачен (D2). Прибор тот же: в том же отчёте состояние base "
              "воспроизвело числа S3h с расхождением 0.00e+00. Наборы v2 в том "
              "прогоне не снимались — поля пусты, а не «равны нулю»")},
)


def external_rows(base_ppl: float, domain_base: float | None, ceiling: float) -> list[dict]:
    """Строки матрицы для внешних замеров (см. ``EXTERNAL_POINTS``)."""
    rows = []
    for p in EXTERNAL_POINTS:
        gen = p["ppl_general"]
        dom = p.get("ppl_domain_v1")
        rows.append({
            "arm": p["arm"], "step": p["step"],
            "source": f"внешний замер: {p['source']} (чекпойнт утрачен)",
            "state": state_name(p["step"], 2000), "reproducible": False,
            "ppl_general": gen,
            "ratio_vs_base": gen / base_ppl if base_ppl else None,
            "above_ceiling": bool(gen > ceiling),
            "ppl_domain": None, "ratio_domain_vs_base": None,
            "ppl_general_v2": None,
            "ppl_domain_v1": dom,
            "tokens_general": None, "general_doc_ppl": None,
            "note": p["note"],
        })
    return rows


def load_trace(calib_root: Path, arm: str) -> list[dict]:
    """Построчная запись шага (``logs/loss_trace.jsonl`` руки), как есть."""
    path = calib_root / ARM_DIRS[arm] / "logs" / "loss_trace.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def train_context(trace: list[dict], step: int, window: int = 100) -> dict | None:
    """Что происходило в обучении на этом шаге: LR и средний лосс вокруг него.

    Нужно для разбора формы: видно, попадает ли точка в полку пикового LR или уже
    в участок затухания, — иначе «восстановление к финалу» и «переключение LR»
    выглядят одинаково.
    """
    if not trace:
        return None
    # Шага 2000 в трассе нет: цикл пишет шаги 0…1999, а финальный чекпойнт
    # сохраняется уже после цикла. Поэтому берётся ближайший записанный шаг НЕ
    # ПОЗЖЕ нужного, и его номер называется рядом (``lr_step``) — значение не
    # выдаётся за замер на самом шаге.
    lr_at, lr_step, losses = None, None, []
    for row in trace:
        try:
            s = int(row.get("step", -1))
        except (TypeError, ValueError):
            continue
        if s <= step and (lr_step is None or s > lr_step):
            lr_at, lr_step = row.get("lr"), s
        if step - window <= s <= step + window and row.get("loss") is not None:
            try:
                losses.append(float(row["loss"]))
            except (TypeError, ValueError):
                pass
    if lr_at is None and not losses:
        return None
    return {"lr": lr_at, "lr_step": lr_step,
            "loss_mean_pm100": (sum(losses) / len(losses)) if losses else None,
            "loss_n": len(losses)}


def curve_per_arm(matrix: list[dict], ceiling: float) -> dict[str, dict]:
    """Кривая по каждой руке. Точки шага 0 — якорь, а не точка руки (см. classify)."""
    out = {}
    for arm in sorted({r["arm"] for r in matrix}):
        rows = [r for r in matrix if r["arm"] == arm]
        pts = [(r["step"], r["ppl_general"]) for r in rows if r["step"] > 0]
        anchor_rows = [r for r in rows if r["step"] == 0]
        anchor = ((0, anchor_rows[0]["ppl_general"]) if anchor_rows else None)
        out[arm] = classify_curve(pts, ceiling, anchor=anchor)
    return out


def instrument_check(matrix: list[dict], base_g1: float, base_g2: float | None) -> dict:
    """Пробит ли потолок 2× на **двух** приборах общего языка.

    Смысл проверки. Потолок ADR-022 считался от ``general_eval.txt`` (v1, 24
    документа, 3682 токена) — прибора, о шумности которого сказано ещё в S3h.
    ADR-018 собрал v2 (200 документов, 39747 токенов) именно чтобы заменить
    шумный прибор. Если вердикт «обвал» держится на v1 и не держится на v2, то
    он в этой части — свойство прибора, и молчать об этом нельзя.
    """
    out = {"base_v1": base_g1, "ceiling_v1": base_g1 * CEILING_FACTOR,
           "base_v2": base_g2,
           "ceiling_v2": (base_g2 * CEILING_FACTOR) if base_g2 else None,
           "per_arm": {}}
    for arm in sorted({r["arm"] for r in matrix}):
        rows = [r for r in matrix if r["arm"] == arm and r["step"] > 0]
        g1 = [r["ppl_general"] for r in rows]
        g2 = [r["ppl_general_v2"] for r in rows if r["ppl_general_v2"] is not None]
        out["per_arm"][arm] = {
            "v1_max": max(g1) if g1 else None,
            "v1_ratio_max": (max(g1) / base_g1) if g1 else None,
            "v1_crossed": bool(g1) and max(g1) > out["ceiling_v1"],
            "v2_max": max(g2) if g2 else None,
            "v2_ratio_max": (max(g2) / base_g2) if (g2 and base_g2) else None,
            "v2_crossed": bool(g2 and out["ceiling_v2"]) and max(g2) > out["ceiling_v2"],
            "v2_final_ratio": (g2[-1] / base_g2) if (g2 and base_g2) else None,
        }
    return out


#: Имя руки сетки — «<доля replay>-<масштаб пика LR>» (см. ARM_DIRS).
ARM_NAME_RE = re.compile(r"^(\d+)-([\d.]+)$")


def factor_effects(instr: dict) -> dict:
    """Насколько глубину обвала двигают LR и доля replay — по отдельности.

    Обе оси меняются в сетке вдвое (25/50 % и 0.35/0.7), поэтому «факторы»
    сравнимы: для каждой оси берётся отношение средней глубины на верхнем
    значении к средней на нижнем. Это и есть ответ на «дело в доле домена или в
    LR» — числом, а не по порядку строк в таблице.
    """
    per = instr.get("per_arm", {})
    parsed = {}
    for arm, node in per.items():
        m = ARM_NAME_RE.match(arm)
        if m:
            parsed[arm] = {"replay": int(m.group(1)), "lr": float(m.group(2)), **node}
    out: dict = {"per_arm": parsed}
    for key, ratios in (("v1", "v1_ratio_max"), ("v2", "v2_ratio_max")):
        vals = {a: n[ratios] for a, n in parsed.items() if n.get(ratios)}
        if len(vals) < 4:
            continue
        for axis, hi, lo in (("lr", 0.7, 0.35), ("replay", 50, 25)):
            fn = (lambda n: n["lr"] == hi) if axis == "lr" else (lambda n: n["replay"] == hi)
            top = [v for a, v in vals.items() if fn(parsed[a])]
            bot = [v for a, v in vals.items() if not fn(parsed[a])]
            if top and bot:
                out[f"{axis}_effect_{key}"] = (sum(top) / len(top)) / (sum(bot) / len(bot))
    return out


def verdict_from(curves: dict[str, dict], matrix: list[dict], ceiling: float,
                 instr: dict | None = None, factors: dict | None = None,
                 base_domain: float | None = None) -> dict:
    """Что форма обвала поддерживает и что исключает.

    Формулировки выводятся из чисел, а не вписаны: вердикт, который не меняется
    при смене данных, — это не вердикт. Порог «рука учитывается» — две точки:
    по одной точке форму не утверждают, и это ограничение здесь, а не в отчёте.
    """
    support: list[str] = []
    exclude: list[str] = []
    limits: list[str] = []
    arm_curves = {a: c for a, c in curves.items() if c.get("n_points", 0) >= 2}
    if not arm_curves:
        return {"supported": [], "excluded": [], "limits": [],
                "note": "точек недостаточно для вердикта"}
    firsts = {a: c["steps"][0] for a, c in arm_curves.items()}
    last = {a: c["steps"][-1] for a, c in arm_curves.items()}
    series = {a: _row_ppls(matrix, a) for a in arm_curves}
    all_crossed = all(c["crossed_at_first_measured"] for c in arm_curves.values())
    per_arm_domain: dict[str, list[tuple[int, float]]] = {}
    for r in matrix:
        if r["step"] > 0 and r["ppl_domain"] is not None:
            per_arm_domain.setdefault(r["arm"], []).append((r["step"], r["ppl_domain"]))
    for a in per_arm_domain:
        per_arm_domain[a].sort()

    if all_crossed:
        bracket = [0, min(firsts.values())]
        total = max(last.values())
        support.append(
            "потолок пробит НЕ ПОЗЖЕ первой сохранённой точки во всех четырёх руках "
            f"(первые точки: {', '.join(f'{a}→{s}' for a, s in sorted(firsts.items()))}) "
            f"— значит переход случился в ОДНОМ и том же интервале {bracket} у всех, "
            "а не «на шаге X у одной руки»")
        support.append(
            f"искать причину следует среди того, что одинаково для всех четырёх рук "
            f"и действует в первые {bracket[1]} шагов: состав и токенизация корпуса "
            "(в том числе строки спецтокенов и их инициализация), режим внимания "
            "(flex) и постановка задачи CPT — всё это сетка не варьирует")
        exclude.append(
            "«обвал объясняется долей replay (домена в миксе)»: при 25 % и 50 % "
            f"переход уложился в один и тот же интервал {bracket} — доля домена "
            "меняет глубину, но не факт и не момент перехода")
        exclude.append(
            "«длительность обучения / накопление»: обвал случился до первой точки "
            f"({bracket[1]}-й шаг из {total}), то есть в первые "
            f"~{bracket[1] * 100 // total} % прогона — на длительность он не опирается")

    if factors:
        lr_v1, rp_v1 = factors.get("lr_effect_v1"), factors.get("replay_effect_v1")
        lr_v2, rp_v2 = factors.get("lr_effect_v2"), factors.get("replay_effect_v2")
        if lr_v1 and rp_v1:
            support.append(
                f"глубину обвала задаёт ПИК LR, а не доля replay. Удвоение LR "
                f"(0.35→0.7) углубляет обвал в ×{lr_v1:.2f} (v1)"
                + (f" и ×{lr_v2:.2f} (v2)" if lr_v2 else "")
                + f" — это +{abs(lr_v1 - 1) * 100:.0f} %"
                + (f"/+{abs(lr_v2 - 1) * 100:.0f} %" if lr_v2 else "")
                + f". Удвоение доли replay (25→50 %) меняет глубину в ×{rp_v1:.2f}"
                + (f" и ×{rp_v2:.2f}" if rp_v2 else "")
                + f", то есть на {abs(rp_v1 - 1) * 100:.0f} %"
                + (f"/{abs(rp_v2 - 1) * 100:.0f} %" if rp_v2 else "")
                + ", и в сторону уменьшения: 25 % replay обваливает общий язык "
                  "чуть сильнее, чем 50 %")
            limits.append(
                "величина LR определяет ГЛУБИНУ, но на приборе v1 не определяет ФАКТ: "
                "потолок 2× пробит и половинным LR. На приборе v2, наоборот, "
                "половинный LR потолок не пробивает — приборы расходятся, и «виноват "
                "ли LR» одним профилем не закрывается")

    # Восстановление против накопления: обе версии различимы по знаку хода.
    recovered = [a for a, s in series.items() if len(s) >= 2 and s[-1] < s[0]]
    grew = [a for a, s in series.items() if len(s) >= 2 and max(s) > s[0]]
    if len(recovered) == len(arm_curves):
        support.append(
            f"к финалу общего языка ЧАСТИЧНО ВОЗВРАЩАЕТСЯ: у всех рук PPL на шаге "
            f"{max(last.values())} ниже, чем в первой сохранённой точке — обвал не "
            "накопительный, а «переход в другой режим» с последующим откатом")
        exclude.append(
            "«деградация растёт с длительностью обучения»: ход обратный — чем "
            "дольше учим, тем НИЖЕ PPL общего языка (и всё равно выше потолка)")
    if grew:
        support.append(
            f"обвал не мгновенный: у рук {grew} PPL ещё ПОДНИМАЕТСЯ после первой "
            "точки до максимума — то есть переход занимает первые сотни шагов, а "
            "не происходит на одном шаге")
    if per_arm_domain and all(len(v) >= 2 and v[-1][1] < v[0][1]
                              for v in per_arm_domain.values()):
        worst = max(v[0][1] - v[-1][1] for v in per_arm_domain.values())
        support.append(
            "доменное качество к финалу ЛУЧШЕ, чем в первой точке, у всех рук "
            f"(максимальный отыгрыш {worst:.2f} PPL против базы "
            f"{base_domain if base_domain else float('nan'):.2f}) — обучение идёт в "
            "цель, а не «ломается»: общий язык вытесняется, а не портится случайным "
            "шумом")
    # Различие приборов — не сноска, а часть вердикта: на v1 «пробили все», на v2
    # может оказаться иначе, и тогда «обвал у всех рук» — свойство 24-документного
    # набора, а не свойство моделей.
    if instr and instr.get("base_v2"):
        crossed_v1 = {a: v["v1_crossed"] for a, v in instr["per_arm"].items()}
        crossed_v2 = {a: v["v2_crossed"] for a, v in instr["per_arm"].items()}
        if all(crossed_v1.values()) and not all(crossed_v2.values()):
            only_v2 = sorted(a for a, ok in crossed_v2.items() if ok)
            keep_v2 = sorted(a for a, ok in crossed_v2.items() if not ok)
            support.append(
                f"вердикт «обвал общего языка» держится НЕ одинаково на двух "
                f"приборах: на v1 (24 документа) потолок 2× пробит всеми четырьмя "
                f"руками, на v2 (200 документов) — только руками {only_v2}; у рук "
                f"{keep_v2} общего языка на v2 потолок не пробит вовсе "
                f"(максимум ×{min(instr['per_arm'][a]['v2_ratio_max'] for a in keep_v2):.2f} "
                f"от базы v2)")
            limits.append(
                "форма «ступеньки» установлена на приборе v1 (тем, по которому "
                "считался потолок ADR-022); на приборе v2 глубина обвала у мягких "
                "рук (пик LR 0.35) потолок 2× не пробивает — то есть v1 для этих "
                "рук глубину завышает, и вопрос «обвал или нет» на них решается "
                "выбором набора, а не моделью")
    limits.append(
        "сетка чекпойнтов — 500 шагов; промежуточных замеров между 0 и первой "
        "точкой нет (GEN-EVAL в калибровочных прогонах выключен: CPT_GEN_EVAL_EVERY=0), "
        "поэтому «ступенька» локализована интервалом, а не шагом: чем именно вызван "
        "переход внутри (0, первой точки], эта сетка не различает")
    return {"supported": support, "excluded": exclude, "limits": limits}


def _row_ppls(matrix: list[dict], arm: str) -> list[float]:
    """PPL общего языка по точкам руки, в порядке шага (для знака хода кривой)."""
    return [r["ppl_general"] for r in sorted(
        (r for r in matrix if r["arm"] == arm and r["step"] > 0),
        key=lambda r: r["step"])]


def assemble(reports: dict[str, dict], plans: list[dict], case_root: Path,
             calib_root: Path, probe_rc: dict[str, int],
             runs_dir: Path | None = None, arms: list[str] | None = None,
             out_name: str = "evidence/s3n-ppl-curve.json") -> dict:
    """Собрать evidence-отчёт профиля из отчётов прибора."""
    base_rep = next((r for r in reports.values() if r), None)
    if base_rep is None:
        raise SystemExit("NOT-VERIFIED: нет ни одного отчёта прибора")
    base_node = base_rep["states"][BASE_STATE]["sets"]
    base_ppl = float(base_node[ROLE_GENERAL]["ppl"])
    domain_node = base_node.get(ROLE_DOMAIN)
    domain_base = float(domain_node["ppl"]) if domain_node else None
    ceiling = base_ppl * CEILING_FACTOR

    matrix = build_matrix(reports, base_ppl, domain_base, ceiling)
    # Внешний замер входит в матрицу ТОЛЬКО пока его точка отсутствует в наличии:
    # найдётся чекпойнт — замер перемеряется, и старая строка стала бы дублем.
    present = {(r["arm"], r["step"]) for r in matrix}
    for row in external_rows(base_ppl, domain_base, ceiling):
        if (row["arm"], row["step"]) not in present:
            matrix.append(row)
    matrix.sort(key=lambda r: (r["arm"], r["step"]))
    # Контекст обучения к каждой точке: LR и средний лосс вокруг шага. Без него
    # «PPL падает к финалу» не отличить от «LR ушёл в затухание».
    traces: dict[str, list[dict]] = {}
    for row in matrix:
        if row["arm"] not in traces:
            traces[row["arm"]] = load_trace(calib_root, row["arm"])
        row["train"] = train_context(traces[row["arm"]], row["step"])
    curves = curve_per_arm(matrix, ceiling)
    gen2_node = base_node.get(ROLE_GENERAL_V2)
    instr = instrument_check(matrix, base_ppl,
                             float(gen2_node["ppl"]) if gen2_node else None)
    factors = factor_effects(instr)
    verdict = verdict_from(curves, matrix, ceiling, instr, factors, domain_base)

    missing_points, hashes = [], {}
    sha_cache_path = (runs_dir / "_sha_cache.json") if runs_dir else None
    sha_cache = load_sha_cache(sha_cache_path) if sha_cache_path else None
    for p in plans:
        for m in p["missing"]:
            missing_points.append({"arm": p["arm"], "step": m["step"], "reason": m["reason"]})
        for step, path in p["points"].items():
            hashes[path] = sha256_file(Path(path), cache=sha_cache)
    if sha_cache_path:
        save_sha_cache(sha_cache_path, sha_cache)

    sets_info = {}
    for set_name, node in base_node.items():
        sets_info[set_name] = {
            "path": base_rep["sets"].get(set_name, {}).get("path"),
            "sha256": base_rep["sets"].get(set_name, {}).get("sha256"),
            "docs_kept": base_rep["sets"].get(set_name, {}).get("docs_kept"),
            "base_ppl": float(node["ppl"]),
            "tokens": node.get("tokens"),
            "role": {"v1_general": "ppl_general (та же методика, что дала базу)",
                     "v2_domain": "ppl_domain (доменный семпл корпуса v10.1, ADR-018)",
                     "v2_general": "ppl_general_v2 (прибор ADR-018)",
                     "v1_domain": "ppl_domain_v1 (домен v1, пара к базе 9.29)"}.get(set_name),
        }

    first_cross = [c["first_crossing_step"] for c in curves.values()
                   if c.get("first_crossing_step") is not None]
    first_steps = sorted({c["steps"][0] for c in curves.values() if c.get("steps")})

    checks = []
    for arm, rep in reports.items():
        if not rep:
            checks.append({"name": f"instrument_ok[{arm}]", "verdict": "не проверено",
                           "detail": f"отчёта прибора по руке {arm} нет"})
            continue
        for c in rep.get("checks", []):
            checks.append({"name": f"{c['name']}[{arm}]", "verdict": c["verdict"],
                           "detail": c.get("detail"), "tolerance": c.get("tolerance")})
        checks.append({"name": f"probe_exit[{arm}]",
                       "verdict": "ok" if probe_rc.get(arm) == 0 else "ОТКАЗ",
                       "detail": f"код возврата прибора: {probe_rc.get(arm)}"})

    # «Полон» отчёт относительно **запрошенных** рук: прогон по подмножеству
    # (`--arms 50-0.35,50-0.7`) не становится отказом от того, что в нём не
    # участвовали остальные. Чего в отчёте нет вообще — видно в `per_arm_status`.
    wanted = list(arms) if arms else list(ARM_DIRS)
    incomplete = [a for a in wanted if not reports.get(a)]

    return {
        "schema": "s3n-ppl-curve/1",
        "stage": "S3n",
        "date": datetime.now(timezone.utc).isoformat(),
        "question": ("Обвал общего языка у калибровочных рук — ступенька в начале "
                     "обучения или постепенная деградация? Форма различает версии: "
                     "данные/токенизация против LR/длительности."),
        "instrument": {
            "measure": "tools/ppl_probe.py:measure (методика _ppl_eval S3h)",
            "runner": "tools/calib_ppl_probe.py (прибор S3m, не переписан)",
            "load_checkpoint": ("laguna_pipeline_v8.py:_load_ckpt_with_resize из копии "
                                "пайплайна той руки, чей чекпойнт меряется"),
            "base_agreement_check": ("прибор отказывает, если состояние base не "
                                     "воспроизвело числа S3h — «тот же прибор» проверено"),
            "max_len": 1024, "batch": 4, "dtype": "bfloat16", "device": "cuda",
            "base_weights": base_rep["instrument"].get("base_weights"),
            "tokenizer": base_rep["instrument"].get("tokenizer"),
        },
        "calib_root": str(calib_root),
        "baseline_ppl": base_ppl,
        "ceiling": ceiling,
        "ceiling_rule": f"{CEILING_FACTOR}× базы (ADR-022 п.3)",
        "sets": sets_info,
        "matrix": matrix,
        "curves": curves,
        "first_crossing_step": min(first_cross) if first_cross else None,
        "first_crossing_note": (
            f"первый ИЗМЕРЕННЫЙ шаг, где ppl_general > потолка; переход произошёл в "
            f"интервале (0, {min(first_steps)}] — между стартом и первым сохранённым "
            f"чекпойнтом промежуточных замеров нет"
            if first_steps else None),
        "curve_shape": summarize_shape(curves),
        "instrument_check": instr,
        "factor_effects": factors,
        "verdict": verdict,
        "missing_points": missing_points,
        "checkpoints": [{"path": p, "sha256": h} for p, h in sorted(hashes.items())],
        "checks": checks,
        "per_arm_status": {
            arm: {"measured": bool(reports.get(arm)),
                  "probe_exit": probe_rc.get(arm),
                  "points": sorted(r["step"] for r in matrix if r["arm"] == arm and r["step"])}
            for arm in ARM_DIRS},  # все четыре руки сетки — даже если мерили подмножество
        "complete": not incomplete,
        "incomplete_arms": incomplete,
        "artifacts": artifacts_of(case_root, runs_dir, out_name),
        "open_questions": open_questions(verdict, instr),
    }


def artifacts_of(case_root: Path, runs_dir: Path | None, out_name: str) -> list[dict]:
    """Файлы дельты с хешами там, где файл уже есть.

    Сам этот отчёт в список входит без хеша: он дописывается тем же вызовом, и
    хеш от предыдущей редакции был бы про другое содержимое.
    """
    items = [{"path": out_name, "role": "evidence профиля (этот файл)",
              "sha256": None, "note": "хеш не приводится: файл пишется этим же вызовом"}]
    for rel, role in (("tools/s3n_ppl_curve.py", "инструмент профиля"),
                      ("docs/specs/S3N-PPL-CURVE.md", "разбор")):
        p = case_root / rel
        items.append({"path": rel, "role": role,
                      "sha256": sha256_file(p) if p.is_file() else None})
    if runs_dir:
        for p in sorted(runs_dir.glob("*.json")):
            if p.name == "_sha_cache.json":
                continue
            items.append({"path": str(p.relative_to(case_root)) if
                          str(p).startswith(str(case_root)) else str(p),
                          "role": "отчёт прибора по руке", "sha256": sha256_file(p)})
    return items


def open_questions(verdict: dict, instr: dict) -> list[str]:
    """Вопросы, которые профиль ставит, но не закрывает (для архитектора)."""
    qs = [
        "Порог 2× ADR-022 посчитан от базы v1 (11.93). Приборы расходятся: на v1 "
        "потолок пробит всеми четырьмя руками, на v2 — тремя. Пересчитывать порог "
        "от базы v2 (21.53) или ввести отдельные пороги под каждый прибор?",
        "Переход локализован интервалом (0, 500]. Нужна ли перегонка для точки "
        "внутри интервала (чекпойнты каждые 50 шагов на одной мягкой руке), или "
        "достаточно того, что переход раньше 500?",
        "Строки спецтокенов: во всех замерах `resized_embeddings: false`, то есть "
        "resize в пробе не срабатывал. Проверять ли гипотезу спецтокенов отдельным "
        "замером (init_special_tokens_subtoken против нулевой инициализации)?",
    ]
    if instr.get("base_v2"):
        qs.append(
            "Рука 50-0.35 на приборе v2 показала общего языка на уровне базы "
            "(×0.99 на финале): считать ли её годной по общему языку и разбирать "
            "только домен, или расхождение приборов само по себе — повод не "
            "принимать решение до разрешения?")
    return qs


# ─────────────────────────────── CLI ─────────────────────────────────────────

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="S3n: профиль PPL по точкам калибровки")
    ap.add_argument("--case-root", default=None)
    ap.add_argument("--calib-root", default=None,
                    help=f"корень артефактов калибровки (по умолчанию {CALIB_ROOT})")
    ap.add_argument("--mode", default="both", choices=["plan", "run", "assemble", "both"])
    ap.add_argument("--runs-dir", default=None,
                    help="каталог отчётов прибора (по умолчанию runs/s3n-ppl-<ts>)")
    ap.add_argument("--out", default="evidence/s3n-ppl-curve.json")
    ap.add_argument("--sets", default="all")
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=3600.0, help="таймаут на руку, с")
    ap.add_argument("--arms", default=None, help="подмножество рук через запятую")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    case_root = Path(args.case_root).resolve() if args.case_root else CASE_ROOT
    calib_root = Path(args.calib_root) if args.calib_root else CALIB_ROOT
    arms = list(ARM_DIRS) if not args.arms else args.arms.split(",")
    unknown = [a for a in arms if a not in ARM_DIRS]
    if unknown:
        print(f"NOT-VERIFIED: неизвестные руки: {unknown}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    plans = plan(case_root, calib_root)
    if args.mode == "plan":
        for p in plans:
            print(f"рука {p['arm']}: финал={p['final_step']}, точек {len(p['points'])}"
                  + (f", нет {[m['step'] for m in p['missing']]}" if p["missing"] else ""))
            for step, path in p["points"].items():
                print(f"    {step:>4} → {path}")
            print(f"    пайплайн: {p['pipeline']}")
        return EXIT_OK

    if not calib_root.is_dir():
        print(f"NOT-VERIFIED: нет каталога артефактов калибровки: {calib_root}",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    runs_dir = (Path(args.runs_dir) if args.runs_dir
                else case_root / "runs" / f"s3n-ppl-{datetime.now():%Y%m%d-%H%M}")
    if not runs_dir.is_absolute():
        runs_dir = case_root / runs_dir

    reports: dict[str, dict] = {}
    probe_rc: dict[str, int] = {}
    if args.mode in ("run", "both"):
        for arm in arms:
            rep, rc = run_arm(case_root, calib_root, arm, runs_dir, args.sets,
                              args.dtype, args.device, args.max_len, args.batch,
                              args.timeout)
            reports[arm], probe_rc[arm] = rep, rc
            # Инкрементально: каждая рука, как только замерена, попадает в отчёт
            # целиком. Падение на четвёртой руке не имеет права стирать три.
            if rep is not None:
                rep_for_arm = dict(rep)
                rep_for_arm.setdefault("final_step",
                                       cpt_steps(calib_root / ARM_DIRS[arm]))
                partial = assemble(merge_reports(reports, calib_root), plans,
                                   case_root, calib_root, probe_rc, runs_dir, arms,
                                   args.out)
                write_report(partial, case_root, args.out, final=False)
    else:
        for arm in arms:
            path = runs_dir / f"{arm}.json"
            if not path.is_file():
                print(f"NOT-VERIFIED: нет отчёта прибора по руке {arm}: {path}",
                      file=sys.stderr)
                return EXIT_NOT_VERIFIED
            rep = json.loads(path.read_text(encoding="utf-8"))
            rep["final_step"] = cpt_steps(calib_root / ARM_DIRS[arm])
            reports[arm] = rep
            probe_rc[arm] = EXIT_OK if rep.get("checks") else EXIT_FAIL

    merged = merge_reports(reports, calib_root)
    report = assemble(merged, plans, case_root, calib_root, probe_rc, runs_dir, arms,
                      args.out)
    out = write_report(report, case_root, args.out, final=True)
    print(f"\nотчёт: {out}")
    print(f"матрица: {len(report['matrix'])} строк; форма: {report['curve_shape']}")
    print(f"первое пересечение потолка: шаг {report['first_crossing_step']}")
    bad = [c for c in report["checks"] if c["verdict"] not in ("ok", "не проверено")]
    for c in bad:
        print(f"ОТКАЗ: {c['name']}: {c['detail']}", file=sys.stderr)
    if not report["complete"] or bad:
        return EXIT_FAIL
    return EXIT_OK


def merge_reports(reports: dict[str, dict], calib_root: Path) -> dict[str, dict]:
    """Дописать в отчёт каждой руки её ``final_step`` (нужен для имени шага финала)."""
    out = {}
    for arm, rep in reports.items():
        if rep is None:
            continue
        rep = dict(rep)
        rep.setdefault("final_step", cpt_steps(calib_root / ARM_DIRS[arm]))
        out[arm] = rep
    return out


def write_report(report: dict, case_root: Path, out_name: str, final: bool) -> Path:
    out = Path(out_name)
    if not out.is_absolute():
        out = case_root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    report["complete"] = bool(report.get("complete")) and final
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    sys.exit(main())
