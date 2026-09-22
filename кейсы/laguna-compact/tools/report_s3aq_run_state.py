#!/usr/bin/env python3
"""S3aq — состояние прогона: что в нём есть, чего нет и почему правило не применено.

**Зачем отдельный инструмент.** Свод (`tools/assemble_s3aq_evidence.py`) при
отсутствии решающего отчёта отказывается работать — и это правильно: свод без
входа был бы пересказом. Но тогда «почему свода нет» остаётся только в голове
того, кто смотрел в каталог, а это ровно тот класс знания, который в этом кейсе
обязан быть артефактом. Инструмент называет состояние **из файлов**: какие
артефакты цепочки есть (с размером и sha256), каких нет, сколько проб дошло до
журнала прибора, и применимо ли объявленное правило.

**Чего инструмент не делает.** Он **не выносит вердикта**: `rule_zone` берётся из
свода (`assemble_s3aq_evidence.rule_zone`, правило ADR-045 п.2 — единственный
источник порогов) и при отсутствии числа равен `not_measured`. Сузить или сдвинуть
зону здесь нечем: пороги — константы свода.

**Контекст (S3aq).** Дельта должна была дать решающее число — обрывы блоков у
свежей точки v13 на бюджете 8192 — и по нему решить, «усечение» это или
«деградация». Прогон 20.09.2026 оборван (журнал прибора кончается на 88-й пробе
CPT-финала, решающего отчёта `format_wide_8192.json` нет), поэтому состояние
фиксируется как измеренное частично, а не как вердикт.

**Фаза замера берётся из факта процесса, а не из наличия файлов** (ADR-023 п.12).
Отсутствие отчёта у **живого** прогона и отсутствие отчёта у **мёртвого** — разные
факты, и различать их обязан прибор, а не читатель. Поэтому в записи есть блок
`run_phase`:

* `active` — процесс добора жив (`pid` из sidecar `resume_confirmed.json`), отчёта
  ещё нет: замер идёт, и «свод против каталога» **не сверяется строго** — журнал
  дописывается по построению, и красное здесь было бы ложным (проверка, красная
  by construction, сигналом не является);
* `finished` — отчёт записан **или** процесс мёртв: сверка строгая;
* `unknown` — ни отчёта, ни следа процесса: фаза не определена и названа таковой;
  строгой сверки нет, потому что «мёртв» не доказано, а выдавать неизвестное за
  расхождение — тот же ложный красный с другой стороны.

Liveness — только чтение: `/proc/<pid>`; сигналы процессу не посылаются.

**Долговечные поля здесь не живут** (ADR-023 п.13). Запись пересобирается целиком из
сырья прогона, поэтому всё, что нельзя восстановить из сырья, лежит sidecar-файлом
рядом (`resume_confirmed.json`: `pid`/`pgid`/`sid`, команды добора, подтверждение
старта). Свод на него **ссылается** (`resume_sidecar`), а не хранит его содержимое:
пересборка затрёт вложенный блок, и знание о прогоне исчезнет вместе с ним.

Коды возврата::

    0 — решающий отчёт на месте: правило применимо, свод может считаться
    2 — решающего отчёта нет: NOT-VERIFIED (правило применить нечем)
    3 — каталога прогона нет
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE = Path(__file__).resolve().parent.parent

EXIT_APPLICABLE, EXIT_NOT_VERIFIED, EXIT_NO_RUN = 0, 2, 3

RUN = "runs/s3aq-budget-8192-20260920"
#: Имена файлов внутри каталога прогона: путь собирается от каталога, а не от корня
#: кейса, — иначе инструмент нельзя прогнать на фикстуре (а непроверенный страж
#: в этом кейсе считается непроверенным).
DECISIVE = "format_wide_8192.json"          # без него правило ADR-045 п.2 не применить
PAIR_CONTROL = "format_wide_4096_pair.json"  # парный контроль «то же состояние, два бюджета»
PROBE_LOG = "format_wide_8192.log"
PIN_CHECK = "pin_checkpoints.json"
#: Долговечные поля прогона: sidecar рядом с пересобираемым сводом (ADR-023 п.13).
SIDECAR = "resume_confirmed.json"
PID_FILE = "resume_probe.pid"
#: Имена блоков, которых в пересобираемом своде быть не может: их пересборка затрёт,
#: и долговечный факт исчезнет. Проверяется механически на выходе `build()`.
DURABLE_BLOCK_KEYS = ("resume_confirmed",)
#: Поля, которые обязаны жить в sidecar (ADR-023 п.13): из сырья прогона они не
#: восстанавливаются — ни из журнала, ни из отчётов. Список один на инструмент и на
#: тест: правило «долговечное — в sidecar» проверяется механически, а не глазами.
DURABLE_FIELDS_FOR_STATE = ("pid", "pgid", "sid", "started_at", "launcher",
                            "cmd_8192", "cmd_4096")
#: Нормативная база 4096 (S3ap): числа того же прибора в том же режиме. Живёт в кейсе,
#: а не в каталоге прогона: это чужая дельта, и её отчёт читается по пути.
BASELINE_4096 = "runs/s3ap-format-monitor-20260919/format_wide_standard.json"
BASELINE_EVIDENCE = "evidence/s3ap-format-monitor.json"

#: Минимум проб на состояние — критерий приёмки дельты (n ≥ 104).
N_REQUIRED = 104

#: Состояния, перечисленные цепочкой для бюджета 8192. Точки 500/5000/9000 удалены
#: ретенцией прогона SFT (PROBE_KEEP_LAST) до замера — «мерить нечего», и это
#: фиксируется как unavailable, а не как «не мерили».
SKIPPED_STATES = {
    "sft_v13_0500": "чекпойнт шага 500 удалён ретенцией прогона SFT до замера",
    "sft_v13_5000": "чекпойнт шага 5000 удалён ретенцией прогона SFT до замера",
    "sft_v13_9000": "чекпойнт шага 9000 удалён ретенцией прогона SFT до замера",
}

#: Артефакты цепочки: имя файла в каталоге прогона → (роль, признак «решающий»).
ARTIFACTS = {
    "device_check.json": ("устройство: настоящая аллокация CUDA (AD-5: стенд не занят)", False),
    "pin_checkpoints.json": ("пиннинг чекпойнтов: sha256 копий против источников", False),
    "identity_core_cfinal.json": ("тождество прибора: ядро 5 промптов против S3ai", False),
    "identity_core_cfinal.log": ("журнал гейта тождества прибора", False),
    "chain.sh": ("цепочка замера (объявленный протокол)", False),
    "chain.log": ("журнал цепочки", False),
    "finalize.sh": ("финализация: свод и гейт до/после", False),
    "run_manifest.json": ("манифест прогона (AD-2)", False),
    "format_wide_8192.log": ("журнал прибора на бюджете 8192 (частичный)", False),
    "format_wide_8192.json": ("РЕШАЮЩИЙ отчёт прибора: вход правила ADR-045 п.2", True),
    "format_wide_4096_pair.json": ("парный контроль: тот же чекпойнт на 4096", True),
    "probe_record.json": ("запись прогона: состояния, бюджеты, коды возврата", False),
}

#: Журнал прибора: строка пробы — `[состояние/режим] тег: … tok=… stop=… | 'сниппет'`.
LOG_LINE = re.compile(r"^\s*\[(?P<state>[^/\]]+)/(?P<mode>[^\]]+)\]\s+\w+:.*?"
                      r"tok=(?P<tok>\d+)\s+stop=(?P<stop>\S+)")
#: Сниппет генерации в журнале: последнее поле строки, в кавычках, 70 знаков.
LOG_SNIPPET = re.compile(r"\|\s*(?P<snippet>.*)$")


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def load(p: Path) -> dict | None:
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


def rule_module():
    """Свод как источник правила: пороги берутся оттуда, а не переписываются здесь."""
    spec = importlib.util.spec_from_file_location(
        "s3aq_assembler", CASE / "tools" / "assemble_s3aq_evidence.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pct(values: list[int], q: float) -> int | None:
    """Перцентиль по ближайшему рангу — **та же арифметика, что у прибора**.

    Копия `probe_language_split._pct` (ceil(q·n), а не интерполяция и не round):
    число, названное здесь, обязано совпасть с числом прибора на тех же данных,
    иначе «медиана из журнала» и «медиана из отчёта» разошлись бы на ровном месте.
    """
    if not values:
        return None
    s = sorted(values)
    return s[max(1, math.ceil(q * len(s))) - 1]


def read_log(path: Path) -> dict:
    """Разобрать журнал прибора: сколько проб какого состояния, чем кончились, длины."""
    out: dict = {"path": str(path), "exists": path.is_file(),
                 "lines": 0, "states_seen": {}, "stop_breakdown": {},
                 "tokens": {}, "snippets": []}
    if not path.is_file():
        return out
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out["lines"] = len(lines)
    toks: list[int] = []
    for ln in lines:
        m = LOG_LINE.search(ln)
        if not m:
            continue
        state = m.group("state")
        out["states_seen"][state] = out["states_seen"].get(state, 0) + 1
        out["stop_breakdown"][m.group("stop")] = \
            out["stop_breakdown"].get(m.group("stop"), 0) + 1
        toks.append(int(m.group("tok")))
        if state == "cfinal":
            s = LOG_SNIPPET.search(ln)
            if s:
                out["snippets"].append(s.group("snippet").strip())
    if toks:
        #: Медиана — ближайшим рангом, как у прибора: интерполяция дала бы 1072.5 там,
        #: где прибор печатает целое, и «то же число» перестало бы быть тем же.
        out["tokens"] = {
            "n": len(toks), "median": pct(toks, 0.5), "p90": pct(toks, 0.9),
            "max": max(toks), "at_or_over_budget": sum(1 for t in toks if t >= 8192),
        }
    for state, seen in out["states_seen"].items():
        out["states_seen"][state] = {"probes": seen, "required": N_REQUIRED,
                                     "complete": seen >= N_REQUIRED}
    return out


def paired_prefix_check(log: dict, baseline_path: Path) -> dict | None:
    """Совпадают ли пробы 8192 с базой 4096 на том же состоянии.

    Сравнение честное ровно настолько, насколько позволяет журнал: в нём лежит
    **сниппет** генерации (70 знаков), а не ход целиком. Поэтому проверяется
    префикс, и вывод формулируется как «путь тот же», а не «числа те же»:
    померить по журналу обрывы блоков нельзя — для этого нужен отчёт прибора.
    """
    base = load(baseline_path)
    if not base or not log.get("snippets"):
        return None
    probes = (base.get("states", {}).get("cfinal") or {}).get("probes") or []
    n = min(len(probes), len(log["snippets"]))
    matched = 0
    for i in range(n):
        snip = log["snippets"][i]
        if len(snip) >= 2 and snip[0] == snip[-1] and snip[0] in "'\"":
            body = snip[1:-1].replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')
        else:
            body = snip
        if probes[i]["response"].startswith(body):
            matched += 1
    toks_base = [p["n_new_tokens"] for p in probes[:n]]
    return {
        "baseline": str(baseline_path),
        "baseline_budget": (base.get("protocol") or {}).get("max_new_tokens"),
        "compared": n, "matched": matched,
        "baseline_median_first_n": pct(toks_base, 0.5) if toks_base else None,
        "reading": ("префиксы совпали — прогон 8192 идёт тем же детерминированным путём, "
                    "что база 4096; различия бюджета в этой части журнала не видно"
                    if n and matched == n else
                    "префиксы разошлись — числа бюджетов несопоставимы, разбирать до выводов"),
    }


def proc_alive(pid: int) -> bool:
    """Жив ли процесс с этим pid. Только чтение `/proc` — сигналы не посылаются.

    `os.kill(pid, 0)` тоже ответил бы на вопрос, но он **посылает сигнал**: на живом
    прогоне, который трогать нельзя, проверка обязана быть читающей, а не спрашивающей.
    """
    return isinstance(pid, int) and pid > 1 and Path(f"/proc/{pid}").is_dir()


def cmdline_of(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip() or None


def liveness(run: Path) -> dict:
    """Жив ли замер: pid из sidecar, запасным фактом — pid-файл сессии добора.

    `pid` из sidecar — объявленный (ADR-023 п.13: долговечные поля живут там), pid-файл
    остаётся запасным источником, если sidecar утрачен: без него фаза стала бы
    «неизвестной» ровно там, где факт был записан.
    """
    side = load(run / SIDECAR)
    pid, source = None, None
    if isinstance(side, dict) and isinstance(side.get("pid"), int):
        pid, source = side["pid"], SIDECAR
    else:
        pf = run / PID_FILE
        if pf.is_file():
            txt = pf.read_text(encoding="utf-8", errors="replace").strip()
            if txt.isdigit():
                pid, source = int(txt), PID_FILE
    alive = proc_alive(pid) if pid else False
    return {
        "known": pid is not None, "pid": pid, "pid_source": source, "alive": alive,
        "checked_by": "чтение /proc/<pid> (сигналы не посылаются)",
        "cmdline": cmdline_of(pid) if alive else None,
        "session_id": (side or {}).get("sid") if isinstance(side, dict) else None,
        "started_at": (side or {}).get("started_at") if isinstance(side, dict) else None,
    }


def run_phase(decisive_exists: bool, live: dict) -> dict:
    """Фаза замера — по факту процесса и факту отчёта, а не по отсутствию файла.

    Правило ADR-023 п.12: строгая сверка «свод против каталога прогона» обязательна
    только когда процесс мёртв **или** отчёт записан. Пока процесс жив и отчёта нет,
    журнал дописывается по построению, и требовать равенства значило бы держать
    красный тест, который красен ровно потому, что замер идёт.
    """
    if decisive_exists:
        return {
            "phase": "finished", "strict_comparison_required": True,
            "why": ("решающий отчёт записан — замер закрыт, журнал закрыт вместе с ним"
                    + ("; процесс ещё жив (пишет другой файл — парный контроль)"
                       if live.get("alive") else "")),
            "adr": "ADR-023 п.12",
        }
    if live.get("alive"):
        return {
            "phase": "active", "strict_comparison_required": False,
            "why": (f"процесс замера жив (pid {live.get('pid')} из {live.get('pid_source')}), "
                    "решающего отчёта ещё нет: журнал дописывается по построению — "
                    "сверка «свод против каталога» строгой быть не может"),
            "adr": "ADR-023 п.12",
        }
    if live.get("known"):
        return {
            "phase": "finished", "strict_comparison_required": True,
            "why": (f"процесс замера мёртв (pid {live.get('pid')} из {live.get('pid_source')}), "
                    "отчёта нет: замер завершился — сверка строгая"),
            "adr": "ADR-023 п.12",
        }
    return {
        "phase": "unknown", "strict_comparison_required": False,
        "why": ("ни отчёта, ни следа процесса (нет pid ни в sidecar, ни в pid-файле): фаза "
                "не определена — «мёртв» не доказано, а неизвестное выдавать за "
                "расхождение значило бы получить красное из отсутствия данных"),
        "adr": "ADR-023 п.12",
    }


def resume_sidecar(run: Path) -> dict:
    """Sidecar долговечных полей: **ссылка** на файл, а не его содержимое (ADR-023 п.13)."""
    p = run / SIDECAR
    doc = load(p) or {}
    durable = [k for k in (DURABLE_FIELDS_FOR_STATE + ("launcher_sha256", "eta_hours"))
               if k in doc]
    return {
        "path": str(p), "exists": p.is_file(),
        "sha256": sha256_file(p) if p.is_file() else None,
        "durable_fields": durable,
        "why": ("пересобираемый свод не хранит долговечные поля: пересборка их затрёт "
                "(ADR-023 п.13) — они лежат в sidecar, здесь только ссылка"),
        "rebuild_warning": ("даже если блок долговечных полей вписан в run_state.json "
                            "руками, ближайшая пересборка его удалит — источник истины "
                            "ровно один: файл sidecar"),
    }


def baseline_reference() -> dict | None:
    """Числа базы 4096 по состояниям (S3ap) — рядом, а не вместо решающего числа."""
    d = load(Path(BASELINE_EVIDENCE))
    if not d:
        return None
    #: Только те поля, что база действительно несёт: `None` вместо отсутствующего
    #: поля читался бы как «измерено и не определено», а это разные вещи.
    rows = {s["state"]: {k: s[k] for k in
                         ("n", "unclosed_think", "tool_call", "answer_coverage",
                          "truncated", "median_len", "cyr_think", "cyr_answer")
                         if k in s}
            for s in d.get("states", [])}
    return {"source": BASELINE_EVIDENCE, "budget": 4096, "states": rows}


def build(run: Path) -> dict:
    rm = rule_module()
    artifacts = {}
    for name, (role, decisive) in ARTIFACTS.items():
        p = run / name
        artifacts[name] = {"role": role, "decisive": decisive, "exists": p.is_file(),
                           "bytes": p.stat().st_size if p.is_file() else None,
                           "sha256": sha256_file(p) if p.is_file() else None}
    decisive = run / DECISIVE
    pair = run / PAIR_CONTROL
    log = read_log(run / PROBE_LOG)
    pin = load(run / PIN_CHECK) or {}
    pinned = list((pin.get("checkpoints") or {}).keys())
    fresh = [t for t in pinned if t != "cfinal"]
    expected = ["cfinal"] + sorted(fresh)
    zone, mechanism = rm.rule_zone(None)
    missing_states = [s for s in expected if s not in log["states_seen"]]
    paired = paired_prefix_check(log, Path(BASELINE_4096))

    state = "rules_applicable" if decisive.is_file() else "measurement_incomplete"
    live = liveness(run)
    phase = run_phase(decisive.is_file(), live)
    doc = {
        "schema": "s3aq-run-state/1",
        "tool": "tools/report_s3aq_run_state.py",
        "stage": "S3aq",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run),
        "status": state,
        "question": ("обрывы блоков у свежей точки v13 при бюджете 8192: усечение выросших "
                     "рассуждений (бюджет) или деградация обучения (модель) — ADR-045 п.2"),
        #: Фаза — то, что делает сверку «свод против каталога» осмысленной (ADR-023 п.12):
        #: у живого замера журнал дописывается, и равенство не есть свойство свода.
        "run_phase": {**phase, "liveness": live},
        "resume_sidecar": resume_sidecar(run),
        "artifacts": artifacts,
        "decisive_input": {
            "path": str(decisive), "exists": decisive.is_file(),
            "role": "отчёт прибора широкого замера на 8192 — единственный вход правила",
            "pair_control": {"path": str(pair), "exists": pair.is_file()},
        },
        "probe_log": log,
        "expected_states": expected,
        "states_missing_from_log": missing_states,
        "unavailable_states": SKIPPED_STATES,
        "checkpoints_pinned": pinned,
        "baseline_4096_reference": baseline_reference(),
        "budget_prefix_check": paired,
        #: Правило ADR-045 п.5 в приборе УЖЕ есть (`aggregate.stop.natural`), и свод
        #: применяет его к обоим бюджетам — вводить заново не нужно и вредно (правка
        #: прибора сломала бы тождество с базой). Здесь оно неприменимо по третьей
        #: причине: незакрытый `<think>` считается по полному ходу, а в артефактах
        #: прогона полных ходов нет — отчёт прибора не записан, журнал несёт сниппет
        #: в 70 знаков. Поэтому `value: null` — «не определено», а не «ноль».
        "unclosed_rule_applied": {
            "value": None,
            "rule": ("ADR-045 п.5: незакрытый <think> — дефект только среди ходов, "
                     "завершившихся естественно; для усечённых показатель не определён"),
            "implemented_in": ("tools/probe_language_split.py → aggregate.stop.natural "
                               "(вводить заново не нужно)"),
            "why_not_computed": ("нужен полный ход, а его в артефактах прогона нет: "
                                 "отчёт прибора не записан; журнал несёт сниппет 70 знаков, "
                                 "по нему обрыв блока не отличается от закрытого блока"),
        },
        "rule": {
            "source": "ADR-045 п.2 (объявлено до замера); пороги — константы свода",
            "cpt_level_max": rm.CPT_LEVEL_MAX,
            "degradation_min": rm.DEGRADATION_MIN,
            "applicable": decisive.is_file(),
            "applied": False,
            "rule_zone": zone,
            "mechanism": mechanism,
            "why_not_applied": ("решающее число (обрывы у v13 при 8192) не измерено: "
                                "отчёта прибора нет — применять правило нечем"
                                if not decisive.is_file() else
                                "зона по числу считается сводом "
                                "(tools/assemble_s3aq_evidence.py): здесь числа нет "
                                "по построению — этот инструмент вердикта не выносит"),
        },
        "recommend_stop_stage": False,
        "verdict": None,
        "open_questions": [
            "добирать ли решающее число на пиннутых чекпойнтах (вопрос владельца: замер — часы GPU)",
            "мерить ли заодно свежую точку как отдельное состояние — это уже другой шаг обучения",
            "считать ли частичный журнал CPT-финала доказательством чего-либо: обрывы по нему не считаются",
        ],
    }
    #: Как добор выглядит — только когда добирать есть что, и только по пиннутым
    #: копиям: путь «свежая точка» (правило устоявшегося файла) сейчас выбрал бы
    #: чекпойнт другого шага, то есть измерил бы другое состояние.
    if not decisive.is_file():
        pin_paths = {tag: (rec or {}).get("pinned")
                     for tag, rec in (pin.get("checkpoints") or {}).items()}
        ckpt_args = " ".join(f"--ckpt {t}={pin_paths[t]}" for t in expected if pin_paths.get(t))
        doc["resume"] = {
            "why": ("цепочка оборвана на пробе CPT-финала: решающие состояния "
                    f"{sorted(fresh)} не мерялись вовсе"),
            "command": (f"python3 tools/probe_language_split.py --prompts wide "
                        f"--max-new-tokens 8192 --stop-at-turn-end --batch-size 8 --sha "
                        f"{ckpt_args} --out {run}/{DECISIVE}"),
            "pair_control": (f"затем тот же чекпойнт на 4096 (шаг 2 цепочки) → "
                             f"{run}/{PAIR_CONTROL}: без него сравнение бюджетов "
                             f"опиралось бы на чужие сутки"),
            "detach": ("запускать отделённо от сессии агента (setsid/nohup): оборванный "
                       "прогон не оставил ни ошибки, ни строки о причине — признак "
                       "смерти процесса вместе с родителем, а не отказа прибора"),
        }
    #: Механический страж ADR-023 п.13: долговечный блок не может жить в пересобираемом
    #: своде. Проверка стоит на выходе `build()`, а не в тесте, — иначе правило
    #: держалось бы на том, что кто-то не забыл его проверить.
    leaked = [k for k in DURABLE_BLOCK_KEYS if k in doc]
    if leaked:
        raise AssertionError(
            "пересобираемый свод не хранит долговечные поля (ADR-023 п.13): "
            f"в запись попал блок {leaked}; долговечная копия — sidecar {SIDECAR} рядом")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser(description="Состояние прогона S3aq (без вердикта)")
    ap.add_argument("--run", default=RUN, help="каталог прогона")
    ap.add_argument("--out", default=None, help="куда записать свод (по умолчанию <run>/run_state.json)")
    args = ap.parse_args()

    run = Path(args.run)
    if not run.is_dir():
        note(f"нет каталога прогона {run}")
        return EXIT_NO_RUN
    doc = build(run)
    out = Path(args.out) if args.out else run / "run_state.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    seen = {s: v["probes"] for s, v in doc["probe_log"]["states_seen"].items()}
    note(f"состояние прогона: {doc['status']} → {out}")
    note(f"  пробы в журнале: {seen or '—'} (норма {N_REQUIRED} на состояние)")
    note(f"  решающий отчёт {doc['decisive_input']['path']}: "
         f"{'есть' if doc['decisive_input']['exists'] else 'НЕТ'}")
    note(f"  фаза замера: {doc['run_phase']['phase']} "
         f"(строгая сверка: {'да' if doc['run_phase']['strict_comparison_required'] else 'нет'}) — "
         f"{doc['run_phase']['why']}")
    note(f"  правило ADR-045 п.2: не применено (зона {doc['rule']['rule_zone']})")
    return EXIT_APPLICABLE if doc["decisive_input"]["exists"] else EXIT_NOT_VERIFIED


if __name__ == "__main__":
    sys.exit(main())
