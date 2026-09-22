#!/usr/bin/env python3
"""S3x — сторож промежуточных замеров полного CPT (K1, K2 и домен; ADR-015/ADR-027/ADR-031).

Что и зачем. ADR-015 требует промежуточных точек как **раннего сигнала
деградации**, а ADR-027 объявляет меру двухкомпонентной. Значит на каждой точке
замера (каждые 500 шагов, имя `calib_checkpoint_<N>.pt`) язык обязан быть измерен
по **обеим** компонентам — иначе к моменту финала будет не «сигнал», а одно число
в конце, то есть ровно тот режим отказа, ради которого ADR-015 и вводился.
ADR-031 п.4 добавляет третью компоненту — **домен** (`domain_eval_v2.txt`,
200 документов; правило чтения «× базы < 1 = домен выучен», `domain_eval.txt` из
5 документов не усредняется с ним), и она меряется на тех же точках: решение о
конфигурации принималось по домену и языку **вместе**, и видеть их порознь на
точках — то же требование, что для K1 и K2.

Правило точек вынесено в код (ADR-030). Раньше тревога по тренду была
человеческим чтением журнала; теперь она считается на каждом круге и пишется в
`trend.json` (`--trend` считает её же по требованию, без сторожа):
тревога — точка выше потолка и следующая не лучше; остановка стадии — рост на
трёх точках подряд выше потолка; контроль первой четверти — к шагу 2500
конъюнкция K1 ∧ K2 и домен обязаны быть в потолке.

Где считается. На **локальной машине** (RTX 4080), а не на стенде: стенд занят
обучением (AD-5 — одна нагрузка за раз), и замер в его контейнере спорил бы с
прогоном за память. 4080 для замеров PPL и не занимается обучением — это
разделение названо в TASK S3u.

Чем считается. Прибором `tools/calib_ppl_probe.py` — он берёт методику
`tools/ppl_probe.py:measure` (та же функция, что дала 11.9319 для v1 и 7.5047 для
K1) и загружает чекпойнт функцией `_load_ckpt_with_resize` **из копии пайплайна
самого прогона**. Ни методика, ни загрузка не переписываются: своя реализация
разошлась бы с контуром на первой правке. Состав состояний — как в замерах S3t:
`base` + измеряемая точка + `base_untouched` (вакуумная точка: если веса после
загрузки чекпойнта не возвращаются к базовым, это отказ, а не «дрейф»).

**Прибор и наборы — с сетевого диска.** Прибор доставляется в каталог пробы на
`gb10-shared` (та же практика, что у `--probe` сетки: `/workspace/shared/calib/
<ppl>/calib_ppl_probe.py` рядом с эталоном S3h), наборы читаются оттуда же через
симлинк `datasets/` — копий данных не делается вовсе (AD-4). Отчёты пишутся на
сетевой диск и зеркалятся в кейс (`runs/full-cpt-ppl-<ts>/`) — это малые JSON.

Проверки, встроенные в прогон (не отчёт, а отказ):

* **базовые числа K1/K2 обязаны воспроизвестись** — 7.504686 (K1) и 6.159936
  (K2) из ADR-029. Не совпало — числа точек не записываются: сравнение шло бы
  другим прибором или другим набором;
* **`base_untouched` против `base`** — собственная проверка прибора
  (`checks[base_restored]`), её вердикт переносится в журнал сторожа.

Поведение на локальной карте. 4080 — общий прибор: на нём меряют и другие дельты
контура. Сторож не толкается: перед замером он проверяет свободную память карты и
**ждёт**, а провал замера (в т.ч. OOM) не съедает точку — она остаётся в очереди и
повторяется на следующем круге. Точка считается снятой только по записанному
отчёту с вердиктом `ok`.

Коды возврата::

    0 — все точки сняты (отчёты на месте, вердикты ok)
    1 — отказ: хотя бы одна точка снята с расхождением прибора
    2 — NOT-VERIFIED: нет входа (каталог прогона/прибор/наборы/CUDA)

Запуск::

    python3 tools/full_cpt_probe.py --ts <ts> --plan
    python3 tools/full_cpt_probe.py --ts <ts> --once      # один круг по недостающим точкам
    setsid nohup python3 tools/full_cpt_probe.py --ts <ts> --watch \\
        >> runs/full-cpt-ppl-<ts>/watch.log 2>&1 < /dev/null &
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CASE_ROOT / "tools"))

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

SHARED = "/home/user/gb10-shared"
SESSION_PREFIX = "full-cpt"
PIPELINE_COPY_NAME = "laguna_pipeline_calib.py"
#: Имена чекпойнтов-точек и финального артефакта стадии. У CPT это
#: `calib_checkpoint_<step>.pt` / `checkpoint_final.pt`, у SFT —
#: `sft_probe_<step>.pt` / `sft_checkpoint_final.pt` (S3aa). Умолчания — CPT:
#: поведение сторожа для CPT не меняется ни на число, ни на имя файла.
CKPT_PATTERN = "calib_checkpoint_{step}.pt"
FINAL_CKPT = "checkpoint_final.pt"


#: Прибор: доставляется в каталог пробы на сетевом диске вместе с эталоном S3h —
#: ровно тот состав, что у пробы сетки (`run_mix_lr_calib.py --probe`).
INSTRUMENT = ["tools/calib_ppl_probe.py", "tools/ppl_probe.py"]
S3H_NAME = "evidence/ppl-baseline-v1v2.json"

#: Компоненты меры (ADR-027 + ADR-031 п.4). Все три — в одном прогоне: иначе
#: конъюнкция и домен сравнивали бы разные шкалы (ADR-029/ADR-031 требуют их на
#: каждой точке).
SETS = ["v1_general", "v3_general", "k2_general", "v2_domain"]
COMPONENTS = {"K1": "v3_general", "K2": "k2_general", "DOMAIN": "v2_domain"}

#: Базовые числа компонент — из ADR-029 (язык) и ADR-031 п.4 (домен: решающий
#: набор `domain_eval_v2.txt`, 200 документов). Это не «ожидание», а проверка
#: тождества прибора: базовые веса обязаны дать ровно эти числа, иначе точка снята
#: не тем прибором. Числа домена совпадают с эталоном S3h (`ppl.v2_domain`)
#: до 1e-9 — эталон и здесь остаётся эталоном, а не пересказом.
BASELINE = {"v3_general": 7.50468637420902, "k2_general": 6.1599356842437585,
            "v1_general": 11.931923888434497, "v2_domain": 11.134115855539092}
BASELINE_TOL = 1e-3  # относительное расхождение

#: Потолки и правило точек (ADR-022 п.3б, ADR-030, ADR-031 п.4).
#:
#: * `CEILING_SCALE` — потолок компонент языка: 2× своей базы «к концу стадии»
#:   (ADR-022 п.3б, подтверждён ADR-030 п.1). Тот же множитель — полоса
#:   сопоставимости конфигураций по домену (ADR-031 п.4).
#: * `DOMAIN_LEARNED` — домен выучен при `× базы < 1` (ADR-031 п.4). Именно это,
#:   а не 2× базы, считается «в потолке» для домена: 2× для домена — это ширина
#:   полосы сравнения конфигураций, а не признак выученности.
#: * `FIRST_QUARTER_FRACTION` — контроль первой четверти (ADR-030 п.5): к концу
#:   первых 25 % шагов конъюнкция обязана быть в потолке (для 9776 шагов — шаг 2444,
#:   первая точка на/после него — 2500).
CEILING_SCALE = 2.0
DOMAIN_LEARNED = 1.0
FIRST_QUARTER_FRACTION = 0.25
LANG_COMPONENTS = ("K1", "K2")

#: Сколько памяти карты обязано быть свободно перед замером: модель (~1 ГБ bf16) +
#: логиты на батч 4 × 1024 × 151936 словарь (~2.5 ГБ) + контекст CUDA. Порог выше
#: суммы намеренно: замер идёт рядом с чужими процессами, и «влезло в притирку» —
#: это OOM на середине пробы, а не экономия.
DEVICE_FREE_BYTES_MIN = 5 * 1024 ** 3

#: Окружение замера: тот же ключ, что у пробы сетки в контейнере
#: (`run_mix_lr_calib.py --probe`) — против фрагментации при чужом соседе на карте.
PROBE_ENV = {"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
             "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_dir(ts: str) -> Path:
    return Path(f"{SHARED}/{SESSION_PREFIX}-{ts}")


def probe_dir(ts: str) -> Path:
    return Path(f"{SHARED}/{SESSION_PREFIX}-ppl-{ts}")


def case_probe_dir(ts: str) -> Path:
    return CASE_ROOT / "runs" / f"{SESSION_PREFIX}-ppl-{ts}"


def points(steps: int, every: int) -> list[dict]:
    """Точки замера: каждые `every` шагов + финальный артефакт стадии.

    Имена файлов — из `CKPT_PATTERN`/`FINAL_CKPT`: у каждой стадии своя ретенция,
    и точек под штатным именем чекпойнта стадии не бывает (их съедает keep_last).
    """
    out = [{"name": f"s{s}", "step": s,
            "ckpt": "checkpoints/" + CKPT_PATTERN.format(step=s)}
           for s in range(every, steps, every)]
    out.append({"name": f"s{steps}", "step": steps, "ckpt": f"checkpoints/{FINAL_CKPT}",
                "decisive": True})
    return out


def gpu_free_bytes() -> int | None:
    """Свободная память локальной карты. `None` — карты/`nvidia-smi` нет."""
    try:
        p = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    total = used = 0
    for line in p.stdout.strip().splitlines():
        t, u = (int(x) for x in line.split(",")[:2])
        total += t
        used += u
    return (total - used) * 1024 ** 2


def ensure_probe_dir(ts: str) -> dict:
    """Каталог пробы на сетевом диске: прибор, эталон S3h, симлинк на наборы.

    Копий данных не делается (AD-4): `datasets` — симлинк на единственный
    экземпляр на сетевом диске, и проба читает его оттуда же.
    """
    d = probe_dir(ts)
    d.mkdir(parents=True, exist_ok=True)
    shipped = {}
    for rel in INSTRUMENT:
        src = CASE_ROOT / rel
        dst = d / Path(rel).name
        data = src.read_bytes()
        if not dst.is_file() or dst.read_bytes() != data:
            dst.write_bytes(data)
        shipped[Path(rel).name] = {"source": rel, "sha256": _sha(data)}
    s3h_dst = d / Path(S3H_NAME).name
    s3h_src = CASE_ROOT / S3H_NAME
    data = s3h_src.read_bytes()
    if not s3h_dst.is_file() or s3h_dst.read_bytes() != data:
        s3h_dst.write_bytes(data)
    shipped[s3h_dst.name] = {"source": S3H_NAME, "sha256": _sha(data)}
    link = d / "datasets"
    #: Проверка именно на симлинк, а не на существование цели: у висящего симлинка
    #: `exists()` ложно, и повторный вызов пытался бы создать его заново (факт
    #: теста на фикстуре, где цель симлинка намеренно не существует).
    if not link.is_symlink() and not link.exists():
        link.symlink_to(f"{SHARED}/datasets")
    return {"dir": str(d), "instrument": shipped, "datasets_symlink": str(link),
            "datasets_resolves_to": str(link.resolve()) if link.exists() else None}


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def probe_point(ts: str, pt: dict, state: dict, timeout: int, dry: bool = False) -> dict:
    """Снять одну точку: `base` + точка + `base_untouched` на K1/K2 (и v1 опорно)."""
    d = probe_dir(ts)
    rd = run_dir(ts)
    ckpt = rd / pt["ckpt"]
    out = d / f"{pt['name']}.json"
    inner = [
        sys.executable, str(d / "calib_ppl_probe.py"),
        "--case-root", str(d),
        "--pipeline", str(rd / PIPELINE_COPY_NAME),
        "--s3h", str(d / Path(S3H_NAME).name),
        "--sets", ",".join(SETS),
        "--state", "base=base",
        "--state", f"{pt['name']}=ckpt:{ckpt}",
        "--state", "base_untouched=base",
        "--out", str(out),
    ]
    if dry:
        return {"dry": True, "argv": inner, "ckpt": str(ckpt), "exists": ckpt.is_file()}
    env = dict(os.environ)
    env.update(PROBE_ENV)
    p = subprocess.run(inner, capture_output=True, text=True, check=False, timeout=timeout,
                       cwd=str(d), env=env)
    rec = {"argv": inner, "rc": p.returncode, "out_tail": p.stdout.strip().splitlines()[-8:],
           "err_tail": p.stderr.strip().splitlines()[-4:], "report": str(out),
           "report_written": out.is_file()}
    if out.is_file():
        rec["verdict"] = judge_report(json.loads(out.read_text(encoding="utf-8")))
    return rec


def judge_report(rep: dict) -> dict:
    """Вердикт сторожа по отчёту прибора: тождество прибора + обе компоненты.

    Здесь не считается «прошла ли стадия» (это ADR-029, по обеим компонентам и
    потолку): здесь проверяется, что числа вообще можно сравнивать — базовые
    веса дали ожидаемые K1/K2, а `base_untouched` вернулся к `base`.
    """
    base = rep.get("states", {}).get("base", {}).get("sets", {})
    mism = []
    for name, want in BASELINE.items():
        got = base.get(name, {}).get("ppl")
        if got is None:
            continue
        rel = abs(got - want) / max(abs(want), 1e-12)
        if rel > BASELINE_TOL:
            mism.append({"set": name, "got": got, "want": want, "rel": rel})
    checks = {c["name"]: c["verdict"] for c in rep.get("checks", [])}
    ratios = {}
    for pt_name, node in rep.get("states", {}).items():
        if pt_name == "base":
            continue
        for comp, set_name in COMPONENTS.items():
            got = node.get("sets", {}).get(set_name, {}).get("ppl")
            b = base.get(set_name, {}).get("ppl")
            if got is not None and b:
                ratios[f"{comp}@{pt_name}"] = round(got / b, 4)
    return {"baseline_ok": not mism, "baseline_mismatch": mism,
            "base_restored": checks.get("base_restored"),
            "base_vs_s3h": checks.get("base_vs_s3h"),
            "ratios": ratios, "complete": rep.get("complete")}


def load_log(ts: str) -> dict:
    p = case_probe_dir(ts) / "state.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"ts": ts, "points": {}}


#: Маркер «каталог не является прогоном» (та же конвенция, что у `runs/calib-ppl-*`):
#: страж C-012 требует манифест AD-2 от каждого каталога в `runs/`, а у каталога
#: приборных отчётов прогона нет — весов, стадий и сида здесь не лежит. Маркер
#: ставится сторожем, а не руками: иначе гейт краснеет на первом же запуске, и
#: «красный по недосмотру» indistinguishable от «красный по делу».
#: Текст берётся от стадии (`--session-prefix`): маркер, называющий SFT-каталог
#: каталогом CPT, — это ложное утверждение в файле, который читает страж.
NOT_A_RUN_TEXT = (
    "каталог промежуточных замеров языка {stage} (журнал сторожа "
    "tools/full_cpt_probe.py + зеркала отчётов прибора), не прогон обучения.\n"
    "Содержимое: state.json (журнал точек) и <точка>.json — зеркала отчётов прибора "
    "tools/calib_ppl_probe.py с сетевого диска. Ни весов, ни стадий, ни чекпойнтов "
    "здесь нет: измеряются состояния чекпойнтов прогона runs/{prefix}-<ts>/.\n"
    "Манифест AD-2 (run_manifest.json) неприменим — прогона здесь нет; манифест "
    "прогона лежит в runs/{prefix}-<ts>/run_manifest.json.\n"
    "Читается стражем C-012 (tools/check_run_manifest.py).\n")


def not_a_run_text() -> str:
    #: Название стадии — как её называет решение (ADR-032/ADR-033), а не префикс каталога.
    stage = {"full-cpt": "полного CPT", "sft": "SFT"}.get(SESSION_PREFIX, SESSION_PREFIX)
    return NOT_A_RUN_TEXT.format(stage=stage, prefix=SESSION_PREFIX)


def save_log(ts: str, state: dict) -> None:
    d = case_probe_dir(ts)
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "NOT_A_RUN"
    if not marker.is_file():
        marker.write_text(not_a_run_text(), encoding="utf-8")
    state["updated_at"] = now_iso()
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")


def mirror(ts: str, pt: dict) -> None:
    """Зеркалить отчёт в кейс: малый JSON едет в git, данные — нет (AD-4)."""
    src = probe_dir(ts) / f"{pt['name']}.json"
    dst = case_probe_dir(ts) / f"{pt['name']}.json"
    if src.is_file():
        dst.write_bytes(src.read_bytes())


def cycle(ts: str, args, state: dict) -> str:
    """Один круг: снять все точки, чьи чекпойнты уже есть и отчёт ещё не снят."""
    rd = run_dir(ts)
    pts = points(args.steps, args.every)
    pending = [p for p in pts if state["points"].get(p["name"], {}).get("verdict", {}).get(
        "baseline_ok") is not True]
    #: Бюджет попыток на точку: без него точка с честным расхождением прибора
    #: крутилась бы до предела жизни сторожа и топила бы в шуме остальные. Провал
    #: остаётся видимым (`attempts`, `verdict`), а не «тихо добитым».
    exhausted = [p for p in pending
                 if state["points"].get(p["name"], {}).get("attempts", 0) >= args.max_attempts]
    ready = [p for p in pending
             if (rd / p["ckpt"]).is_file() and p not in exhausted]
    if not pending:
        print(f"[{now_iso()}] все {len(pts)} точек сняты", flush=True)
        return "done"
    if exhausted and len(exhausted) == len(pending):
        print(f"[{now_iso()}] точки с исчерпанным бюджетом попыток: "
              f"{[p['name'] for p in exhausted]} — дальше не повторяем", flush=True)
        return "done"
    if not ready:
        have = [p["name"] for p in pending if (rd / p["ckpt"]).is_file()]
        print(f"[{now_iso()}] ждём чекпойнтов: снято {len(pts) - len(pending)}/{len(pts)}, "
              f"готовы к замеру {have or '—'}", flush=True)
        return "waiting"
    free = gpu_free_bytes()
    if free is not None and free < DEVICE_FREE_BYTES_MIN:
        print(f"[{now_iso()}] 4080 занята: свободно {free / 1024 ** 3:.1f} ГБ "
              f"< {DEVICE_FREE_BYTES_MIN / 1024 ** 3:.1f} ГБ — ждём (общий прибор, AD-5)", flush=True)
        return "waiting-gpu"
    pt = ready[0]
    attempts = state["points"].get(pt["name"], {}).get("attempts", 0) + 1
    print(f"[{now_iso()}] замер {pt['name']} (шаг {pt['step']}, попытка {attempts}): "
          f"{pt['ckpt']}; свободно на карте {(free or 0) / 1024 ** 3:.1f} ГБ", flush=True)
    rec = probe_point(ts, pt, state, args.timeout)
    ok = rec.get("verdict", {}).get("baseline_ok") is True
    state["points"][pt["name"]] = {
        "step": pt["step"], "ckpt": pt["ckpt"], "measured_at": now_iso(),
        "attempts": attempts,
        "rc": rec.get("rc"), "verdict": rec.get("verdict", {}),
        "out_tail": rec.get("out_tail", []), "err_tail": rec.get("err_tail", []),
        "report": rec.get("report"), "probe_dir": str(probe_dir(ts)),
    }
    mirror(ts, pt)
    print(f"[{now_iso()}] {pt['name']}: {'ok' if ok else 'ОТКАЗ/повтор'} "
          f"{json.dumps(rec.get('verdict', {}).get('ratios', {}), ensure_ascii=False)}", flush=True)
    save_log(ts, state)
    report_trend(ts, state, args)
    return "measured"


def report_trend(ts: str, state: dict, args) -> dict:
    """Пересчитать тренд ADR-030 и записать его рядом с журналом (`trend.json`).

    Правило точек считается **на каждом круге**, а не читается человеком в журнале:
    ADR-030 называет слабым местом именно «тревогу, которую можно не заметить».
    Журнал тренда — малый JSON в кейсе, он же попадает в git.
    """
    t = trend(state, args.steps)
    d = case_probe_dir(ts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "trend.json").write_text(json.dumps(t, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    line = (f"[{now_iso()}] тренд ADR-030 по {t['checked']} точкам(е): {t['verdict']}; "
            f"первая четверть: {t['first_quarter']['verdict']} "
            f"(точка {t['first_quarter']['point']})")
    print(line, flush=True)
    for a in t["alarms"]:
        print(f"[{now_iso()}] ТРЕВОГА ADR-030: {a['at']} (шаг {a['step']}) — {a['why']}; "
              f"следующая {a['next']['point']}: K1 ×{a['next']['K1']}, K2 ×{a['next']['K2']}",
              flush=True)
    if t["stop_condition"]:
        s = t["stop_condition"]
        print(f"[{now_iso()}] ОСТАНОВКА ПО ADR-030 п.4: {s['points']} — рост на трёх точках "
              f"подряд выше потолка; стадия останавливается по протоколу ADR-016 "
              f"(tools/run_full_cpt.py --stop --ts {ts})", flush=True)
    return t


def do_trend(args) -> int:
    """Тренд по требованию — без сторожа и без карты (только чтение журнала)."""
    state = load_log(args.ts)
    if not state.get("points"):
        print(f"NOT-VERIFIED: в журнале нет снятых точек "
              f"({case_probe_dir(args.ts) / 'state.json'})", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    t = trend(state, args.steps)
    d = case_probe_dir(args.ts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "trend.json").write_text(json.dumps(t, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
    print(f"тренд ADR-030 (ts {args.ts}): {t['verdict']}; точек {t['checked']}")
    cb = t["ceiling"]["base"]
    print(f"  потолки: K1/K2 — {t['ceiling']['K1']:g}× базы "
          f"(K1 база {cb.get('K1')}, K2 база {cb.get('K2')}); домен — выучен при "
          f"< {t['ceiling']['DOMAIN']:g}× базы ({cb.get('DOMAIN')})")
    lang_word = {True: "в потолке", False: "ВЫШЕ ПОТОЛКА", None: "не измерено"}
    dom_word = {True: "выучен", False: "НЕ выучен", None: "не измерен"}
    for r in t["rows"]:
        print(f"  {r['point']:6s} шаг {r['step']:5d}  K1 ×{r['K1']}  K2 ×{r['K2']}  "
              f"домен ×{r['DOMAIN']}  "
              f"[{lang_word[r['lang_within']]}; домен {dom_word[r['domain_learned']]}]")
    for a in t["alarms"]:
        print(f"  ТРЕВОГА: {a['at']} — {a['why']}")
    if t["stop_condition"]:
        print(f"  ОСТАНОВКА (ADR-030 п.4): {t['stop_condition']['points']}")
    fq = t["first_quarter"]
    print(f"  первая четверть (шаг ≥ {fq['step_required']}): точка {fq['point']} — "
          f"{fq['verdict']}" + (f" ({fq['why']})" if fq.get("why") else ""))
    print(f"тренд записан: {d / 'trend.json'}")
    return EXIT_FAIL if (t["stop_condition"] or t["alarms"]
                         or fq["verdict"] == "НАРУШЕН") else EXIT_OK


# ─────────────────── правило точек (ADR-030 + ADR-031 п.4) ───────────────────

def trend(state: dict, steps: int) -> dict:
    """Тренд по точкам: тревога, остановка, контроль первой четверти (ADR-030).

    Считается по **записанным** числам точек (журнал сторожа), а не по ожиданиям:
    компоненты языка сравниваются со своим потолком `CEILING_SCALE × базы`
    (ADR-022 п.3б), домен — с признаком выученности `× базы < 1` (ADR-031 п.4).

    Правила (ADR-030):
    * **тревога** (фиксируется, стадия не останавливается) — точка выше потолка
      **и** следующая точка не лучше предыдущей (тренд не вниз);
    * **остановка** — рост на трёх последовательных точках (каждая хуже
      предыдущей, все выше потолка): форма настоящей деградации, а не ступеньки;
    * **первая четверть** — к концу первых 25 % шагов конъюнкция K1 ∧ K2 обязана
      быть в потолке; для домена «в потолке» прочитано как «выучен» (× базы < 1).

    Читается буквально, чтобы правило не подменялось на ходу:
    «в потолке» = каждая компонента языка не хуже `CEILING_SCALE × своей базы`;
    «не лучше» = хотя бы по одной компоненте хуже (тренд не вниз), а не «хуже по
    обеим» — тревога обязана срабатывать раньше, чем деградация станет очевидной.
    """
    pts = state.get("points", {})
    base = state.get("baseline", BASELINE)
    rows, alarms = [], []
    order = sorted(pts, key=lambda n: pts[n].get("step", 0))
    for name in order:
        r = pts[name].get("verdict", {}).get("ratios", {})
        row = {"point": name, "step": pts[name].get("step")}
        for comp in ("K1", "K2", "DOMAIN"):
            v = r.get(f"{comp}@{name}")
            limit = DOMAIN_LEARNED if comp == "DOMAIN" else CEILING_SCALE
            row[comp] = v
            row[f"{comp}_limit"] = limit
            #: `within` = «в потолке» (не хуже своего предела). Для домена предел —
            #: 1× базы (выучен), для языка — 2× базы (ADR-022 п.3б).
            row[f"{comp}_within"] = None if v is None else v <= limit
        #: Три состояния, а не два: «в потолке», «выше потолка» и «не измерено».
        #: Слить последние два в одно значит выдать отсутствие числа за деградацию
        #: (и наоборот) — ошибка, которая стоила бы ложной остановки стадии.
        row["lang_measured"] = all(row[c] is not None for c in LANG_COMPONENTS)
        row["lang_within"] = (all(row[f"{c}_within"] for c in LANG_COMPONENTS)
                              if row["lang_measured"] else None)
        row["domain_learned"] = row["DOMAIN_within"]
        rows.append(row)

    #: Тревога и остановка — по компонентам ЯЗЫКА: ADR-030 п.3-4 сформулирован для
    #: конъюнкции K1 ∧ K2. Домен в них не участвует — правило для роста домена
    #: числом не названо (вынесено в open_questions отчёта).
    for i in range(len(rows)):
        cur = rows[i]
        if cur["lang_within"] is not False:   # тревога — только для точки ВЫШЕ потолка
            continue                          # (True — в потолке, None — не измерено)
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt is None:
            continue
        if nxt["K1"] is None or nxt["K2"] is None:
            continue
        if nxt["K1"] > cur["K1"] or nxt["K2"] > cur["K2"]:   # «не лучше» = хуже хотя бы где-то
            alarms.append({"kind": "тревога", "at": cur["point"], "step": cur["step"],
                           "why": "точка выше потолка, следующая не лучше предыдущей",
                           "ratios": {c: cur[c] for c in LANG_COMPONENTS},
                           "next": {"point": nxt["point"], "step": nxt["step"],
                                    "K1": nxt["K1"], "K2": nxt["K2"]}})
    stop = None
    for i in range(len(rows) - 2):
        a, b, c = rows[i], rows[i + 1], rows[i + 2]
        if not all(x["lang_within"] is False for x in (a, b, c)):
            continue
        if any(x["K1"] is None or x["K2"] is None for x in (a, b, c)):
            continue
        if (b["K1"] > a["K1"] and c["K1"] > b["K1"]
                and b["K2"] > a["K2"] and c["K2"] > b["K2"]):
            stop = {"kind": "остановка", "points": [a["point"], b["point"], c["point"]],
                    "steps": [a["step"], b["step"], c["step"]],
                    "ratios": {"K1": [a["K1"], b["K1"], c["K1"]],
                               "K2": [a["K2"], b["K2"], c["K2"]]},
                    "rule": "ADR-030 п.4: рост на трёх точках подряд выше потолка — "
                            "остановка стадии по протоколу ADR-016"}
    quarter_step = FIRST_QUARTER_FRACTION * steps
    q_rows = [r for r in rows if r["step"] >= quarter_step]
    fq = {"step_required": round(quarter_step),
          "point": q_rows[0]["point"] if q_rows else None,
          "step": q_rows[0]["step"] if q_rows else None,
          "lang_within": (q_rows[0]["lang_within"] if q_rows else None),
          "domain_learned": (q_rows[0]["domain_learned"] if q_rows else None)}
    if not q_rows:
        fq.update({"verdict": "не проверено",
                   "why": f"нет точки на шаге ≥ {round(quarter_step)}"})
    elif fq["lang_within"] is None:
        fq.update({"verdict": "не проверено",
                   "why": f"на точке {fq['point']} конъюнкция не измерена"})
    elif fq["lang_within"] is False:
        fq.update({"verdict": "НАРУШЕН",
                   "why": "к концу первой четверти конъюнкция не в потолке — ADR-030 п.5: "
                          "это не ступенька, стадия останавливается по протоколу ADR-016"})
    elif fq["domain_learned"] is None:
        fq.update({"verdict": "не проверено",
                   "why": f"на точке {fq['point']} домен не измерен"})
    elif fq["domain_learned"] is False:
        fq.update({"verdict": "НАРУШЕН",
                   "why": "к концу первой четверти домен не выучен (× базы ≥ 1)"})
    else:
        fq.update({"verdict": "в потолке", "why": None})
    return {"schema": "adr030-trend/1", "date": now_iso(), "steps": steps,
            "ceiling": {"K1": CEILING_SCALE, "K2": CEILING_SCALE, "DOMAIN": DOMAIN_LEARNED,
                        "base": {comp: base.get(set_name)
                                 for comp, set_name in COMPONENTS.items()},
                        "note": "K1/K2 — потолок 2× базы (ADR-022 п.3б); домен — «выучен» "
                                "при × базы < 1 (ADR-031 п.4)"},
            "rows": rows, "alarms": alarms, "stop_condition": stop, "first_quarter": fq,
            "verdict": ("СТОП" if stop else ("ТРЕВОГА" if alarms else "норма")),
            "checked": len(rows)}


def do_plan(args) -> int:
    ts = args.ts
    rd = run_dir(ts)
    print("план промежуточных замеров (S3x, ADR-015 + ADR-027 + ADR-031 п.4):")
    print(f"  каталог прогона:   {rd} ({'есть' if rd.is_dir() else 'НЕТ'})")
    print(f"  каталог пробы:     {probe_dir(ts)} (прибор и наборы — с сетевого диска)")
    print(f"  зеркало в кейс:    {case_probe_dir(ts)}")
    print(f"  прибор:            tools/calib_ppl_probe.py (методика tools/ppl_probe.py:measure), "
          f"sha256={_sha((CASE_ROOT / 'tools/ppl_probe.py').read_bytes())[:12]}…")
    print("  компоненты:        K1 = v3_general, K2 = k2_general, домен = v2_domain "
          "(+ v1_general опорно, тождество прибора)")
    print("  правило точек:     ADR-030: тревога — точка выше потолка и следующая не лучше; "
          "остановка — рост на трёх точках выше потолка; первая четверть — конъюнкция "
          "K1 ∧ K2 и домен в потолке (домен «выучен» = × базы < 1, ADR-031 п.4)")
    print("  базовые числа:     " + ", ".join(f"{k} {v:.6f}" for k, v in BASELINE.items()))
    print(f"  точки:             каждые {args.every} шагов → {len(points(args.steps, args.every))} точек")
    print("  состояния в точке: base + точка + base_untouched (вакуумная точка)")
    print("  где считается:     локальная карта (4080); при занятости — ожидание, не толкотня")
    for pt in points(args.steps, args.every):
        p = rd / pt["ckpt"]
        print(f"    {pt['name']:6s} шаг {pt['step']:5d}  {'есть' if p.is_file() else '—':4s} {pt['ckpt']}")
    return EXIT_OK if rd.is_dir() else EXIT_NOT_VERIFIED


def main(argv=None) -> int:
    global SHARED, SESSION_PREFIX, PIPELINE_COPY_NAME, CKPT_PATTERN, FINAL_CKPT
    global DEVICE_FREE_BYTES_MIN
    ap = argparse.ArgumentParser(
        description="S3x: промежуточные замеры K1/K2/домена полного CPT")
    ap.add_argument("--ts", required=True)
    #: Корень сетевого диска — параметр, а не константа: на фикстуре тестов сторож
    #: обязан проверяться без стенда и без данных (копий не делается, AD-4).
    ap.add_argument("--shared", default=SHARED)
    #: Стадия. У CPT и SFT разные имена точек (`calib_checkpoint_*` против
    #: `sft_probe_*`) и разные финальные артефакты; прибор при этом один и тот же
    #: (`calib_ppl_probe.py` + методика `ppl_probe.py:measure`), и это и есть
    #: тождество замера между стадиями. Умолчания — CPT.
    ap.add_argument("--session-prefix", default=SESSION_PREFIX,
                    help="префикс каталогов прогона/пробы (full-cpt | sft)")
    ap.add_argument("--pipeline-copy", default=PIPELINE_COPY_NAME,
                    help="имя копии пайплайна в каталоге прогона")
    ap.add_argument("--ckpt-pattern", default=CKPT_PATTERN,
                    help="шаблон имени чекпойнта-точки, напр. 'sft_probe_{step}.pt'")
    ap.add_argument("--final-ckpt", default=FINAL_CKPT,
                    help="имя финального артефакта стадии")
    ap.add_argument("--steps", type=int, default=9776)
    ap.add_argument("--every", type=int, default=500)
    ap.add_argument("--timeout", type=int, default=2400, help="таймаут одного замера, с")
    ap.add_argument("--poll", type=int, default=120, help="пауза между кругами, с")
    ap.add_argument("--max-hours", type=float, default=11.0, help="предел жизни сторожа")
    ap.add_argument("--max-attempts", type=int, default=5,
                    help="бюджет попыток на точку (провал остаётся видимым, а не тонет в повторах)")
    #: Порог свободной памяти карты перед замером. По умолчанию 5 ГБ (как было), но
    #: его приходится поднимать, когда на той же карте идёт ВТОРАЯ проба: порог ниже
    #: фактической потребности превращает ожидание в отказ, а отказ съедает бюджет
    #: попыток точки (наблюдено 17.09.2026: проба agentic держала 9.3 ГБ из 16, прибор
    #: влез в 6.0 ГБ и умер на «Tried to allocate 38.00 MiB»).
    ap.add_argument("--min-free-gb", type=float, default=DEVICE_FREE_BYTES_MIN / 1024 ** 3,
                    help="порог свободной памяти карты, ГБ: ниже — ЖДЁМ, не тратя попытку")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_true")
    g.add_argument("--once", action="store_true")
    g.add_argument("--watch", action="store_true")
    g.add_argument("--trend", action="store_true",
                   help="правило точек ADR-030 по журналу: тревога/остановка/первая четверть")
    args = ap.parse_args(argv)
    SHARED = args.shared
    SESSION_PREFIX = args.session_prefix
    PIPELINE_COPY_NAME = args.pipeline_copy
    CKPT_PATTERN = args.ckpt_pattern
    FINAL_CKPT = args.final_ckpt
    DEVICE_FREE_BYTES_MIN = int(args.min_free_gb * 1024 ** 3)

    if args.plan:
        return do_plan(args)
    if args.trend:
        #: Тренд — чтение журнала, а не замер: ни карты, ни каталога прогона не требует.
        return do_trend(args)
    if not run_dir(args.ts).is_dir():
        print(f"NOT-VERIFIED: нет каталога прогона {run_dir(args.ts)}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if (CASE_ROOT / "tools" / "calib_ppl_probe.py").is_file() is False:
        print("NOT-VERIFIED: нет прибора tools/calib_ppl_probe.py", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    if gpu_free_bytes() is None and not args.once:
        print("NOT-VERIFIED: карты не видно (nvidia-smi) — замер PPL нечем сделать",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    info = ensure_probe_dir(args.ts)
    state = load_log(args.ts)
    state["probe_dir"] = info
    state["instrument_sha256"] = info["instrument"]
    state["sets"] = SETS
    state["components"] = COMPONENTS
    state["baseline"] = BASELINE
    state["steps"] = args.steps
    state["every_steps"] = args.every
    save_log(args.ts, state)
    print(f"каталог пробы: {info['dir']}; наборы: {info['datasets_resolves_to']}", flush=True)

    t0 = time.time()
    while True:
        verdict = cycle(args.ts, args, state)
        if verdict in ("waiting", "waiting-gpu"):
            #: На ожидании тренд не меняется, но правило первой четверти и вердикт
            #: в журнале обязаны быть свежими: их читает человек, а не пересчитывает.
            report_trend(args.ts, state, args)
        if verdict == "done":
            bad = [n for n, v in state["points"].items()
                   if v.get("verdict", {}).get("baseline_ok") is not True]
            return EXIT_FAIL if bad else EXIT_OK
        if args.once:
            return EXIT_OK
        if time.time() - t0 > args.max_hours * 3600:
            print(f"[{now_iso()}] предел жизни сторожа ({args.max_hours} ч) — выход", flush=True)
            return EXIT_OK
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
