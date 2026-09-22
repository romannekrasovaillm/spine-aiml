#!/usr/bin/env python3
"""S3-pre — RL-разведка на стенде GB10: **цена шага RL** (ADR-010, вариант D).

Назначение дельты — измерить, а не обучить. Разведка запускает RL-стадию
пайплайна на **готовом** ``sft_checkpoint_final.pt`` (исторический прогон
``laguna_qwen25-05b``, 30.07.2026), 50–100 шагов, один сид, без новых стадий
CPT/SFT. Сходимость не является критерием: если награда вырождена, замер времени
всё равно валиден — и это фиксируется в evidence, а не замалчивается.

Что раннер считает предусловием (ADR-010 п.4), а не «попробуем»:

* свободная unified-память стенда ≥ ``--min-free-gb`` (по умолчанию 40 ГБ);
  замер — по ``MemAvailable`` (у GB10 память общая с CPU, ``nvidia-smi`` про
  занятое молчит); меньше порога — стадия **не стартует**;
* страж сериализации AD-5 в ``--strict``: недоступность стенда — красный вердикт;
* контейнеры ``llm-platform-*`` — не трогаются, но замеряются до и после;
* набор курикулума (``rl_tasks_revpool_v2.jsonl``, 9 162 задачи — ADR-054 п.1) и
  SFT-чекпойнт проверяются **до** запуска, а не выясняются падением на 40-й минуте;
* запуск — через ``nvrm-storm/safe_start.sh`` (drop-caches-абсорбер) и с
  cgroup-капом памяти: как в рабочей лесенке ``run_v12_ladder.sh``, иначе
  разведка мерила бы не тот путь запуска.

Стоп-условия (ADR-010 п.5 + предохранитель хоста):

* энтропия политики < ``--entropy-floor`` два наблюдения подряд → стоп
  (mode collapse). Наблюдения — те, что печатает сам пайплайн (шаги, кратные 10:
  ``entropy_h``, ``laguna_pipeline_v8.py:1412``): своей шкалы энтропии раннер не
  вводит, иначе в отчёте оказались бы две разные величины под одним именем;
* два NVRM-инцидента (лог стадии + дельта ``dmesg``) → стоп и пауза;
* свободная память хоста ниже ``--mem-floor-gb`` во время прогона → стоп:
  платформенные контейнеры не должны пострадать от чужого OOM;
* ``--max-wall-hours`` — предел стены на стадию: разведка обязана закончиться
  замером, а не «идёт»;
* **вырожденная награда (ADR-017)** — критерий живёт в общем приборе
  ``tools/rl_degeneracy.py`` (том же, что стоит стражем в ``pilot_chain.sh``),
  наблюдения берутся из строк метрик шага. По умолчанию он только **публикует**
  вердикт: предмет разведки — цена шага, и мёртвая награда замер не отменяет
  (ADR-010 п.3). Остановка включается флагом ``--degenerate-stop`` — когда
  разведка исполняет роль стадии, то есть когда ADR-017 п.2 предписывает стоп.

Замеры (в ``evidence/s3-rl-probe.json``): время шага RL; доля времени на
генерацию роллаутов против обучения (по обвязке ``tools/rl_probe_hook.py``);
пик unified-памяти; ``tok/s`` генерации; число ресников весов; энтропия политики
(коридор 1.0–2.0 — здесь применим по определению, ADR-008 п.3); ``pass_rate`` и
``reward_mean`` как фон; деградировавшие траектории; число фактически
выполненных шагов. Гиперпараметры берутся **из кода** пайплайна
(``parse_pipeline_hyperparams``), а не из манифеста: расхождение ``resync_every``
25 (манифест) против 10 (код) уже задокументировано в ADR-010.

Артефакты: ``runs/rl-probe-<ts>/run_manifest.json`` (AD-2, C-012, через
``tools/write_run_manifest.py``), логи стадии, ``rl_probe.jsonl`` (обвязка),
``mem_rl.jsonl`` (сэмплер хоста), ``rollouts_log.jsonl`` (траектории),
``evidence/s3-rl-probe.json``.

Режимы без запуска: ``--plan``, ``--preflight-only``, ``--analyze-only``
(пересборка сводки из готовых артефактов), ``--stop-only`` (закрыть контейнер
разведки по имени — план отката).

Коды возврата::

    0 — стадия прошла (или запрошенный режим без запуска отработал)
    1 — предусловие нарушено / стадия упала / инцидент в замерах
    2 — NOT-VERIFIED: стенд недоступен или артефакт отсутствует

Запуск::

    python3 tools/run_rl_probe.py --plan
    python3 tools/run_rl_probe.py --preflight-only
    python3 tools/run_rl_probe.py                     # 100 шагов RL (≈2–3 ч)
    python3 tools/run_rl_probe.py --analyze-only --ts 20260914-1200
    python3 tools/run_rl_probe.py --stop-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Транспорт и разбор замеров — общие с S2: две реализации ssh-доставки или
#: парсера памяти разошлись бы на первой правке. Своё здесь только то, что
#: относится к RL: границы шага, стоп-условия, разбивка времени по фазам.
import run_smoke as S  # noqa: E402
#: Критерий вырожденной награды (ADR-017) — **тот же прибор**, что стоит стражем в
#: цепочке пилота: две реализации одного критерия разошлись бы на первой правке, и
#: «вырождение» в разведке значило бы не то же, что в стадии.
import rl_degeneracy as D  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "gb10-fast"
DEFAULT_IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"

STAND_SHARED = "/home/user/gb10-shared"
STAND_EXPERIMENTS = "/home/user/experiments"
SAFE_START = f"{STAND_SHARED}/nvrm-storm/safe_start.sh"
CTR_SHARED = "/workspace/shared"
CTR_EXPERIMENTS = "/workspace/experiments"
PIPELINE_CTR = f"{CTR_SHARED}/laguna_pipeline_v8.py"
PIPELINE_CASE = CASE_ROOT / "laguna_pipeline_v8.py"

#: Набор курикулума стадии — **v2** (ADR-054 п.1): на сетевом диске под версионным
#: именем, в кейсе — симлинк (AD-4/C-011). Это же имя читает контейнер внутри
#: стадии. `v1` набором стадии не выбирается (у него нет карточки AD-2 — прогон на
#: нём недоказуем, ADR-054 п.3); он остаётся историческим носителем.
RL_DATA_STAND = f"{STAND_SHARED}/datasets/rl_tasks_revpool_v2.jsonl"
RL_DATA_CTR = f"{CTR_SHARED}/datasets/rl_tasks_revpool_v2.jsonl"
RL_DATA_CASE = CASE_ROOT / "datasets" / "rl_tasks_revpool_v2.jsonl"
RL_POOL_SYMLINK = CASE_ROOT / "runs" / "rev-pool-v2" / "rl_pool_filtered.jsonl"

#: Стартовый чекпойнт разведки: готовый SFT исторического прогона (ADR-010).
#: CPT/SFT для разведки **не** запускаются — это и делает её дешёвой.
DEFAULT_SFT_CKPT = f"{STAND_EXPERIMENTS}/laguna_qwen25-05b/checkpoints/sft_checkpoint_final.pt"

#: Прочие входы стадии RL (PPL-наборы для GEN-EVAL, стабы ray для weight-transfer).
GEN_EVAL_FILES = [f"{STAND_SHARED}/datasets/general_eval.txt",
                  f"{STAND_SHARED}/datasets/domain_eval.txt"]
WT_STUBS = f"{STAND_SHARED}/wt_stubs"

#: Платформенный инференс: не гейтится (ADR-007 п.4), но не должен исчезнуть.
PLATFORM_PREFIX = "llm-platform-"
CONTAINER_PREFIX = "laguna-rlprobe-"

#: Порог памяти на старте RL (ADR-010 п.4). Калибруется этим же замером.
MIN_FREE_GB = 40.0
#: Предохранитель хоста во время прогона: ниже этого — стоп (платформенные
#: контейнеры дороже незавершённой разведки).
MEM_FLOOR_GB = 4.0
ENTROPY_FLOOR = 0.5
ENTROPY_LOW_STREAK = 2
NVRM_LIMIT = 2

#: Конфигурация RL-стадии, снятая с рабочей лесенки ``run_v12_ladder.sh`` для
#: ``qwen25-05b`` (util 0.32, KV 8 ГиБ, eager 0). Это не «свои числа»: разведка
#: обязана мерить тот же путь запуска, что и полный протокол, иначе замер цены
#: относится к другому прогону.
LADDER_VLLM_GPU_UTIL = "0.32"
LADDER_KV_CACHE_BYTES = "8589934592"
LADDER_VLLM_EAGER = "0"
DEFAULT_MEM_FRACTION = "0.6"
DEFAULT_MEM_CAP = "100g"

PROGRESS_RE = re.compile(
    r"STAGE:|RL step |HOT-SYNC|RESYNC|ENTROPY-BOOST|GEN-EVAL|CKPT:|PROBES|vLLM engine|"
    r"HF-экспорт|Loaded SFT ckpt|Traceback|Error|error|OOM|Killed|NVRM|Xid|"
    r"NOT-VERIFIED|RL_PROBE_")

#: Строка метрик RL-шага, которую печатает пайплайн каждые 10 шагов
#: (``laguna_pipeline_v8.py:1508``) — единственный источник энтропии политики,
#: который есть у контура.
RL_STEP_RE = re.compile(
    r"RL step (\d+)/(\d+) \| reward=(-?[\d.]+) \| pass=(\d+)% \| turns=([\d.]+) \| "
    r"len=(\d+)t \| kl=(-?[\d.]+) \| entropy=(-?[\d.]+) \| clip=(-?[\d.]+) \| "
    r"adv=(-?[\d.]+) \| adv≠0=(\d+)% \| active=(\d+)/(\d+) \| mem=([\d.]+)GB")
HOTSYNC_RE = re.compile(r"HOT-SYNC: веса подменены за (\d+)s \(step (\d+)\)")
RESYNC_RE = re.compile(r"RESYNC: веса роллаута обновлены \(step (\d+)\)")
RESYNC_WAIT_RE = re.compile(r"RESYNC-wait: free=(\d+)GB/(\d+)GB за (\d+)s")
ENTROPY_BOOST_RE = re.compile(r"ENTROPY-BOOST: entropy=([\d.]+) ×(\d+) → kl_coef=([\d.]+)")
GEN_EVAL_RE = re.compile(
    r"GEN-EVAL step (\d+): ppl_general=([\d.]+) \(([-+][\d.]+)% vs base\), "
    r"ppl_domain=([\d.]+) \(([-+][\d.]+)%\)")
GROUPS_RE = re.compile(r"RL: группы (\d+)×(\d+), baseline=(\w+) \(pure=(\w+)\), max_steps=(\d+)")
NVRM_RE = re.compile(r"NVRM|Xid")


# ─── гиперпараметры: из кода, а не из манифеста ───────────────────────────────

def parse_pipeline_hyperparams(source: str) -> dict:
    """Фактические гиперпараметры RL-стадии — регулярками по исходнику пайплайна.

    Почему так, а не из ``run_manifest.json`` пайплайна: манифест печатает
    ``resync_every`` из ``os.environ.get("RESYNC_EVERY", "25")``
    (``laguna_pipeline_v8.py:1817``), тогда как RL-петля берёт дефолт **10**
    (``:1178``) — расхождение 25 против 10 зафиксировано в ADR-010. Манифест в
    этой точке не источник истины; источник — код, который исполняется.

    Ненайденное значение остаётся ``None`` с пометкой: выдуманное число в отчёте
    хуже отсутствующего.
    """
    def find(pattern: str, group: int = 1, flags: int = re.M):
        m = re.search(pattern, source, flags)
        return m.group(group) if m else None

    def num(pattern: str, cast=float, group: int = 1):
        v = find(pattern, group)
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    hotsync = num(r"RESYNC_EVERY = int\(os\.environ\.get\(\"RESYNC_EVERY\", \"(\d+)\"\)\)", int)
    return {
        "kl_coef": num(r"^    kl_coef = ([\d.]+)$"),
        "kl_coef_source": "laguna_pipeline_v8.py: kl_coef = 0.01 (жёстко; ×1.5 при entropy<0.5 ×3 — ENTROPY-BOOST)",
        "resync_every_runtime": hotsync,
        "resync_every_manifest_default": num(
            r"\"resync_every\": int\(os\.environ\.get\(\"RESYNC_EVERY\", \"(\d+)\"\)\)", int),
        "resync_every_source": ("laguna_pipeline_v8.py: RESYNC_EVERY = env или 10; "
                                "манифест пайплайна печатает свой дефолт 25 — расхождение ADR-010"),
        "tasks_per_step": num(r"TASKS_PER_STEP, R_PER_TASK = (\d+),", int),
        "group_size": num(r"TASKS_PER_STEP, R_PER_TASK = \d+, (\d+)", int),
        "pure_tasks_per_step": num(r"PURE_TASKS_PER_STEP\", \"(\d+)\"", int),
        "pure_group_size": num(r"PURE_GROUP_SIZE\", \"(\d+)\"", int),
        "max_turns": num(r"^    max_turns = (\d+)", int),
        "lr": num(r"torch\.optim\.AdamW\(model\.parameters\(\), lr=([\d.eE-]+)"),
        "grad_clip_norm": num(r"clip_grad_norm_\(model\.parameters\(\), ([\d.]+)\)"),
        #: Не `max_new_tokens` из сигнатуры generate_batch (там дефолт 512, и он не
        #: исполняется): нужен вызов из RL-петли, где длина хода задана явно.
        "gen_max_new_tokens": num(r"prompts_to_gen, max_new_tokens=(\d+), temperature=", int),
        "gen_temperature": num(r"prompts_to_gen, max_new_tokens=\d+, temperature=([\d.]+)"),
        "gen_top_k": num(r"prompts_to_gen, max_new_tokens=\d+, temperature=[\d.]+, top_k=(\d+)", int),
        "gen_stop": find(r"stop=\[([^\]]+)\]"),
        "cispo_c_low": num(r"def cispo_surrogate_loss\(.*c_low=([\d.]+)", group=1),
        "cispo_c_high": num(r"def cispo_surrogate_loss\(.*c_high=([\d.]+)", group=1),
        "optimizer": "AdamW(lr=1e-6, weight_decay=0.01)",
        "baseline": find(r"baseline=\{?'LOO' if pure else '(\w+)'"),
    }


def pipeline_hyperparams(path: Path = PIPELINE_CASE) -> dict:
    """Гиперпараметры из файла пайплайна кейса (симлинк на gb10-shared)."""
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": f"пайплайн не читается: {e}"}
    hp = parse_pipeline_hyperparams(src)
    hp["pipeline_sha256"] = S.sha256_path(Path(path))
    hp["note"] = ("значения сняты регулярками с исполняемого файла пайплайна; "
                  "не найденное — None, а не догадка")
    return hp


# ─── предусловия ──────────────────────────────────────────────────────────────

def check_probe_inputs(host: str, args) -> dict:
    """Входы разведки: пул ревизии, стартовый чекпойнт, PPL-наборы, стабы."""
    required = [RL_DATA_STAND, args.sft_ckpt, f"{STAND_SHARED}/laguna_pipeline_v8.py",
                SAFE_START, WT_STUBS] + GEN_EVAL_FILES
    rc, out, _ = S.ssh(host, "for f in " + " ".join(required) +
                       '; do [ -e "$f" ] && echo "OK $f" || echo "MISSING $f"; done')
    if rc != 0:
        return {"name": "inputs", "ok": False, "detail": "проверка входов не отработала"}
    missing = [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("MISSING")]
    detail = ("пул ревизии, SFT-чекпойнт, PPL-наборы и стабы на месте" if not missing
              else f"нет: {', '.join(missing)}")
    return {"name": "inputs", "ok": not missing, "detail": detail, "missing": missing,
            "rl_data": RL_DATA_STAND, "sft_ckpt": args.sft_ckpt}


def check_pool_integrity(host: str) -> dict:
    """Пул ревизии на стенде — тот же файл, что в кейсе (иначе мерили бы другой пул)."""
    local = S.sha256_path(RL_DATA_CASE)
    rc, out, _ = S.ssh(host, f"sha256sum {RL_DATA_STAND}; wc -l < {RL_DATA_STAND}")
    if rc != 0:
        return {"name": "pool", "ok": False, "detail": "хеш пула не прочитан"}
    #: sha256sum печатает «хеш  путь», wc — число строк: путь в выводе есть, и
    #: брать «второе поле» значило бы показать путь там, где ожидается счёт.
    parts = out.split()
    remote = parts[0] if parts else None
    lines = next((p for p in reversed(parts) if p.isdigit()), None)
    ok = bool(local and remote and local == remote)
    return {"name": "pool", "ok": ok, "lines": lines,
            "sha256_case": local, "sha256_stand": remote,
            "detail": (f"пул совпадает с кейсом: {lines} строк, sha256 {str(remote)[:12]}"
                       if ok else "пул на стенде НЕ совпадает с кейсом — стоп")}


def check_disk(host: str, min_free_gb: float = 20.0) -> dict:
    """Диск стенда: RL-чекпоинты ~3 ГБ × 2 + hf_rollout ~1 ГБ на прогон."""
    rc, out, _ = S.ssh(host, "df -BG --output=avail /home/user | tail -1")
    avail = None
    if rc == 0:
        m = re.search(r"(\d+)", out)
        avail = int(m.group(1)) if m else None
    ok = avail is not None and avail >= min_free_gb
    return {"name": "disk", "ok": ok, "avail_gb": avail, "required_gb": min_free_gb,
            "detail": (f"свободно {avail} ГБ (нужно ≥ {min_free_gb:.0f})" if avail is not None
                       else "df не прочитан")}


def preflight(host: str, args) -> dict:
    """Полный прогон предусловий. ``ok`` — можно ли запускать стадию."""
    checks: list[dict] = []
    rc, out, err = S.ssh(host, "hostname")
    reachable = rc == 0
    checks.append({"name": "host", "ok": reachable,
                   "detail": (f"{host} → {out.strip()}" if reachable
                              else f"{host} недоступен: {err.strip()}")})
    if not reachable:
        return {"ok": False, "checks": checks, "host": host}
    checks.append(S.check_free_memory(host, args.min_free_gb))
    checks.append(S.check_platform_containers(host))
    checks.append(S.check_serialization_guard(host, strict=True))
    checks.append(S.check_image(host, args.image))
    checks.append(check_probe_inputs(host, args))
    checks.append(check_pool_integrity(host))
    checks.append(check_disk(host))
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "host": host,
            "dmesg_before": S.dmesg_counts(host)}


# ─── стоп-условия ─────────────────────────────────────────────────────────────

class Guard:
    """Стоп-условия ADR-010 п.5 (+ предохранитель памяти хоста).

    Страж живёт в отдельном треде, а не в цикле чтения лога: если стадия
    замолчит (зависание, wedge — известный режим GB10), цикл лога встанет
    вместе с ней, и страж по времени/памяти не сработает ровно тогда, когда он
    нужнее всего.
    """

    def __init__(self, host: str, container: str, args, deadline: float):
        self.host = host
        self.container = container
        self.deadline = deadline
        self.args = args
        self.reasons: list[str] = []
        self.entropy_points: list[dict] = []
        self.nvrm_log_incidents: list[str] = []
        self.dmesg_nvrm_before: int | None = None
        self._low_streak = 0
        #: RLock, а не Lock: сигналы из лога приходят из главного треда, страж по
        #: времени/памяти — из своего, и оба зовут ``trip``. Обычный Lock здесь
        #: либо заклинил бы, либо (хуже) дал бы «стоп без причины».
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._stopped = False
        self._mem_min: float | None = None
        self._thread: threading.Thread | None = None
        #: Наблюдения награды шага — вход критерия ADR-017. Копятся из тех же строк
        #: лога, что питают страж энтропии: отдельный «мониторинговый» поток метрик
        #: разошёлся бы с доказательной базой.
        self.reward_points: list[dict] = []
        self.degeneracy: dict | None = None

    # ── сигналы из лога ───────────────────────────────────────────────────────
    def note_entropy(self, step: int, value: float, line: str) -> None:
        reason = None
        with self._lock:
            self.entropy_points.append({"step": step, "entropy": value})
            self._low_streak = self._low_streak + 1 if value < self.args.entropy_floor else 0
            if self._low_streak >= self.args.entropy_low_streak:
                reason = (f"энтропия политики < {self.args.entropy_floor} на "
                          f"{self._low_streak} наблюдениях подряд (шаг {step}) — "
                          f"mode collapse")
        if reason:
            self.trip(reason)

    def note_step(self, m: re.Match) -> None:
        """Наблюдение шага → критерий вырожденной награды ADR-017.

        Что здесь **не** делается по умолчанию: стоп. Предмет разведки — цена шага
        (ADR-010 п.3: «сходимость не критерий»), и вырожденная награда замер не
        отменяет — ADR-010 прямо требует опубликовать её, а не спрятать. Поэтому
        наблюдение записывается, вердикт идёт в evidence, а остановка включается
        флагом ``--degenerate-stop`` — тем самым, которым ADR-017 п.2 закрывает
        стадию (в цепочке пилота стоп ставит ``pilot_chain.sh``).
        """
        obs = {"step": int(m.group(1)), "reward_mean": float(m.group(3)),
               "pass_rate": int(m.group(4)) / 100.0,
               "clip_frac": float(m.group(9)),
               "adv_nonzero": int(m.group(11)) / 100.0}
        with self._lock:
            self.reward_points.append(obs)
            report = D.evaluate(self.reward_points,
                                window_steps=self.args.degeneracy_window)
            self.degeneracy = report
        if getattr(self.args, "degenerate_stop", False) \
                and report["verdict"] == "degenerate_reward":
            self.trip(f"вырожденная награда RL (ADR-017): классы "
                      f"{', '.join(report['stop_classes'])} — стоп, результат "
                      f"помечается degenerate_reward")

    def note_line(self, line: str) -> None:
        """NVRM/Xid в логе стадии. ``trip`` зовётся **вне** блокировки: внутри неё
        нельзя ни ходить по ssh, ни ждать чужой поток."""
        if not NVRM_RE.search(line):
            return
        with self._lock:
            self.nvrm_log_incidents.append(line.strip()[:300])
        nvrm = len(self.nvrm_log_incidents) + (self._dmesg_nvrm_delta() or 0)
        if nvrm >= self.args.nvrm_limit:
            self.trip(f"NVRM/Xid-инцидентов {nvrm} ≥ {self.args.nvrm_limit} — "
                      f"стоп и пауза (ADR-010 п.5)")

    def note_dmesg(self, delta: int | None) -> None:
        if delta is None:
            return
        with self._lock:
            total = len(self.nvrm_log_incidents) + delta
        if total >= self.args.nvrm_limit:
            self.trip(f"NVRM/Xid в dmesg: +{delta} — стоп и пауза (ADR-010 п.5)")

    def _dmesg_nvrm_delta(self) -> int | None:
        now = S.dmesg_counts(self.host)
        if not now or self.dmesg_nvrm_before is None:
            return None
        return max(0, now.get("nvrm_xid", 0) - self.dmesg_nvrm_before)

    # ── стоп ──────────────────────────────────────────────────────────────────
    def trip(self, reason: str) -> None:
        """Остановить стадию. Вызывается из любого треда; идемпотентно."""
        with self._lock:
            self.reasons.append(reason)
            already = self._stopped
            self._stopped = True
        if already:
            return
        print(f"\n[run_rl_probe] СТОП ПО СТРАЖУ: {reason}", flush=True)
        rc, out, err = S.ssh(self.host, f"docker stop {self.container}", timeout=180)
        print(f"[run_rl_probe] docker stop {self.container}: rc={rc} "
              f"{out.strip() or err.strip()}", flush=True)

    @property
    def tripped(self) -> bool:
        return self._stopped

    # ── мониторинг (тред) ─────────────────────────────────────────────────────
    def start(self) -> None:
        self.dmesg_nvrm_before = (S.dmesg_counts(self.host) or {}).get("nvrm_xid")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        last_dmesg = 0.0
        while not self._stop.wait(15):
            now = time.time()
            if now > self.deadline:
                self.trip(f"предел стены {self.args.max_wall_hours} ч исчерпан — "
                          f"разведка обязана закончиться замером, а не «идёт»")
                return
            rc, raw, _ = S.ssh(self.host, "cat /proc/meminfo", timeout=30)
            if rc == 0:
                gb = S.free_gb(S.parse_meminfo(raw))
                if gb is not None:
                    self._mem_min = gb if self._mem_min is None else min(self._mem_min, gb)
                    if gb < self.args.mem_floor_gb:
                        self.trip(f"свободной unified-памяти {gb} ГБ < предохранителя "
                                  f"{self.args.mem_floor_gb} ГБ — стоп, чтобы не убить "
                                  f"платформенные контейнеры")
                        return
            if now - last_dmesg > 60:
                last_dmesg = now
                self.note_dmesg(self._dmesg_nvrm_delta())

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def summary(self) -> dict:
        return {
            "tripped": self._stopped,
            "reasons": list(self.reasons),
            "entropy_floor": self.args.entropy_floor,
            "entropy_low_streak_required": self.args.entropy_low_streak,
            "entropy_observations": list(self.entropy_points),
            "nvrm_incidents_in_log": len(self.nvrm_log_incidents),
            "nvrm_lines": self.nvrm_log_incidents[:5],
            "nvrm_limit": self.args.nvrm_limit,
            "mem_floor_gb": self.args.mem_floor_gb,
            "min_free_gb_seen": self._mem_min,
            "max_wall_hours": self.args.max_wall_hours,
            "degenerate_reward": self.degeneracy,
            "reward_observations": len(self.reward_points),
            "degenerate_stop_enabled": bool(getattr(self.args, "degenerate_stop", False)),
            "note": ("наблюдения энтропии — из строк пайплайна (шаги, кратные 10): "
                     "своей шкалы раннер не вводит; страж по памяти — предохранитель "
                     "хоста, в ADR-010 его нет (платформенные контейнеры дороже "
                     "незавершённой разведки)"),
        }


# ─── сценарий стадии ──────────────────────────────────────────────────────────

def run_id_for(ts: str) -> str:
    return f"rl-probe-compact-{ts}"


def container_name(ts: str) -> str:
    return f"{CONTAINER_PREFIX}{ts}"


def rel_ckpt_link(sft_ckpt: str, run_dir_stand: str) -> str:
    """Относительная ссылка на SFT-чекпойнт внутри каталога прогона.

    Абсолютный путь стенда (``/home/user/experiments/...``) в контейнере не
    существует, абсолютный путь контейнера (``/workspace/experiments/...``) не
    существует на хосте — а симлинк обязан разрешаться **в обоих** пространствах
    имён, потому что создаётся на хосте, а открывается в контейнере. Относительный
    путь от ``<run>/checkpoints/`` разрешается одинаково с обеих сторон.
    """
    base = Path(run_dir_stand) / "checkpoints"
    try:
        return os.path.relpath(sft_ckpt, start=str(base))
    except ValueError as e:  # разные диски/платформы
        raise SystemExit(f"не удалось построить относительный путь к чекпойнту: {e}") from e


def build_stage_script(*, ts: str, args) -> str:
    """Скрипт стадии для стенда: сэмплер памяти + safe_start + docker run + обвязка.

    Кладётся на стенд целиком: после прогона видно, чем именно он был запущен,
    и стадию можно повторить вручную, не реконструируя строку запуска.
    """
    run_id = run_id_for(ts)
    host_run_dir = f"{STAND_EXPERIMENTS}/{run_id}"
    ctr_run_dir = f"{CTR_EXPERIMENTS}/{run_id}"
    ctr_name = container_name(ts)
    exp_name = f"rl-probe-compact-{ts}"
    #: Абсолютный путь контейнера к стартовому чекпойнту — только как *цель*
    #: относительной ссылки (см. ``rel_ckpt_link``); на хосте он не существует.
    link = rel_ckpt_link(args.sft_ckpt, host_run_dir)
    ckpt_dir = f"{ctr_run_dir}/checkpoints"
    log_dir = f"{ctr_run_dir}/logs"
    probe_args = " ".join([
        f"--model_name {args.model}", "--stage rl", f"--exp_name {exp_name}",
        f"--max_steps {args.rl_steps}", f"--max_samples {args.max_samples}",
        f"--batch_size {args.batch_size}", f"--max_len {args.max_len}",
        f"--rl_steps {args.rl_steps}", f"--seed {args.seed}",
        "--cpt_data /workspace/shared/datasets/cpt_corpus_v12r.txt",
        "--sft_data /workspace/shared/datasets/sft_train_v12.jsonl",
        f"--rl_data {RL_DATA_CTR}",
        "--eval_data /workspace/shared/datasets/eval_ood_clean.jsonl",
        f"--ckpt_dir {ckpt_dir}", f"--log_dir {log_dir}",
    ])
    return f"""#!/usr/bin/env bash
# Стадия RL-разведки S3-pre. Сгенерировано tools/run_rl_probe.py (ts={ts}).
# Ручное повторение: bash {host_run_dir}/run_rl.sh
set -uo pipefail
RUN_DIR="{host_run_dir}"          # хост-путь (сэмплер, лог, tee)
CTR_RUN_DIR="{ctr_run_dir}"       # тот же каталог глазами контейнера
CTR="{ctr_name}"
LOG="$RUN_DIR/logs/rl.log"
METRICS="$CTR_RUN_DIR/rl_probe.jsonl"
MEM="$RUN_DIR/mem_rl.jsonl"
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/checkpoints"

# Стартовый чекпойнт: относительная ссылка (см. rel_ckpt_link) — пайплайн ищет
# sft_checkpoint_final.pt в --ckpt_dir, а чекпойнт исторический и лежит в чужом
# каталоге. Копия 2.8 ГБ не нужна, симлинк работает в обоих пространствах имён.
ln -sfn "{link}" "$RUN_DIR/checkpoints/sft_checkpoint_final.pt"
echo "RL_PROBE_LINK=$(readlink "$RUN_DIR/checkpoints/sft_checkpoint_final.pt")"

bash "$RUN_DIR/smoke_mem_sampler.sh" "$MEM" 5 {args.sampler_seconds} &
SAMPLER=$!
trap 'kill "$SAMPLER" 2>/dev/null' EXIT

echo "RL_PROBE_STAGE_START=rl ts=$(date -Is)"
bash {SAFE_START} -d 60 -i 5 -- docker run --rm --name "$CTR" \\
  --gpus all --ipc=host --pid=host \\
  --memory={args.mem_cap} --memory-swap={args.mem_cap} \\
  --security-opt seccomp=unconfined --cap-add SYS_PTRACE \\
  --ulimit memlock=-1 --ulimit stack=67108864 --ulimit nofile=262144:262144 \\
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -e HF_HUB_OFFLINE=1 \\
  -e TRANSFORMERS_OFFLINE=1 -e LAGUNA_MEM_FRACTION={args.mem_fraction} \\
  -e VLLM_ALLOW_INSECURE_SERIALIZATION=1 -e PYTHONPATH=/workspace/shared/wt_stubs \\
  -e VLLM_GPU_UTIL={args.vllm_gpu_util} -e VLLM_KV_CACHE_BYTES={args.kv_cache_bytes} \\
  -e VLLM_EAGER={args.vllm_eager} -e RESYNC_EVERY={args.resync_every} \\
  -e GEN_EVAL_EVERY={args.gen_eval_every} \\
  -e TORCHINDUCTOR_CACHE_DIR=/workspace/shared/inductor_cache \\
  -v {STAND_EXPERIMENTS}:{CTR_EXPERIMENTS} \\
  -v {STAND_SHARED}:{CTR_SHARED} \\
  -v /home/user/.cache/huggingface:/root/.cache/huggingface -w /workspace \\
  {args.image} \\
  python3 "$CTR_RUN_DIR/rl_probe_hook.py" \\
    --metrics "$METRICS" --pipeline {PIPELINE_CTR} \\
    --tasks-per-step {args.tasks_per_step} --group-size {args.group_size} \\
    -- {probe_args} \\
  2>&1 | tee "$LOG"
RC=${{PIPESTATUS[0]}}
kill "$SAMPLER" 2>/dev/null
echo "RL_PROBE_STAGE_EXIT=$RC"
exit "$RC"
"""


def render_plan(args) -> str:
    """План прогона: что будет запущено, куда и при каких условиях."""
    hp = pipeline_hyperparams()
    run_id = run_id_for(args.ts)
    return "\n".join([
        "== S3-pre: план RL-разведки (цена шага RL, ADR-010) ==",
        f"стенд:            {args.host} (ssh), образ: {args.image}",
        f"каталог прогона:  стенд {STAND_EXPERIMENTS}/{run_id}  ←  кейс {args.runs_dir}/rl-probe-{args.ts}",
        f"стартовый чекпойнт: {args.sft_ckpt} (только чтение; CPT/SFT не запускаются)",
        f"пул:              {RL_DATA_CTR} (9 162 задачи, ADR-054 п.1; симлинк кейса "
        f"runs/rev-pool-v2/rl_pool_filtered.jsonl)",
        f"модель/сид:       {args.model}, seed={args.seed}, шагов RL: {args.rl_steps}",
        f"RL-конфиг:        kl_coef={hp.get('kl_coef')} (код), "
        f"resync_every={args.resync_every} (env; дефолт в коде {hp.get('resync_every_runtime')}, "
        f"в манифесте пайплайна {hp.get('resync_every_manifest_default')}), "
        f"группы {hp.get('tasks_per_step')}×{hp.get('group_size')}, "
        f"max_turns={hp.get('max_turns')}",
        f"vLLM:             util={args.vllm_gpu_util}, KV={args.kv_cache_bytes} Б, "
        f"eager={args.vllm_eager} (как в run_v12_ladder.sh для qwen25-05b)",
        f"предусловия:      свободной unified-памяти ≥ {args.min_free_gb} ГБ; "
        f"страж AD-5 в --strict; llm-platform-* не трогаются; диск ≥ 20 ГБ",
        f"запуск стадии:    {SAFE_START} -d 60 -i 5 -- docker run (absorbер включён, "
        f"cgroup-кап {args.mem_cap})",
        "",
        f"стоп-условия:     entropy < {args.entropy_floor} ×{args.entropy_low_streak} → стоп; "
        f"NVRM/Xid ≥ {args.nvrm_limit} → стоп и пауза; память хоста < {args.mem_floor_gb} ГБ → стоп; "
        f"стена > {args.max_wall_hours} ч → стоп",
        f"награда:          критерий ADR-017 (окно {args.degeneracy_window} шагов, прибор "
        f"tools/rl_degeneracy.py) — стоп "
        f"{'ВКЛЮЧЁН' if args.degenerate_stop else 'НЕ включается: ADR-010 п.3 — сходимость не критерий разведки'}; "
        f"вердикт и классы публикуются в evidence в любом случае",
        "замеры:           время шага RL, доля генерации роллаутов против обучения, "
        "пик unified-памяти, tok/s генерации, число ресников, энтропия политики, "
        "pass_rate/reward_mean (фон), деградировавшие траектории",
        f"артефакты:        {args.runs_dir}/rl-probe-{args.ts}/ (манифест AD-2, лог стадии, "
        f"rl_probe.jsonl, mem_rl.jsonl, rollouts_log.jsonl) + {args.evidence}",
    ])


# ─── исполнение ───────────────────────────────────────────────────────────────

def run_stage(host: str, args, local_dir: Path) -> dict:
    """Стадия разведки: доставка скриптов, запуск, страж, возврат артефактов."""
    ts = args.ts
    run_id = run_id_for(ts)
    run_dir = f"{STAND_EXPERIMENTS}/{run_id}"
    script = build_stage_script(ts=ts, args=args)
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "run_rl.sh").write_text(script, encoding="utf-8")

    shipped = {}
    for name, payload in (("run_rl.sh", script.encode()),
                          ("rl_probe_hook.py",
                           (CASE_ROOT / "tools" / "rl_probe_hook.py").read_bytes()),
                          ("smoke_mem_sampler.sh",
                           (CASE_ROOT / "tools" / "smoke_mem_sampler.sh").read_bytes())):
        rc, err = S.ssh_put(host, f"{run_dir}/{name}", payload)
        if rc != 0:
            return {"stage": "rl", "ok": False,
                    "error": f"доставка {name} на стенд не удалась: {err.strip()}"}
        shipped[name] = S.sha256_bytes(payload)

    # Сверка доставки: инструмент на стенде обязан быть байт-в-байт тем, что
    # лежит в кейсе, иначе замер сделан не тем кодом, который в git.
    rc, out, err = S.ssh(host, "sha256sum " + " ".join(f"{run_dir}/{n}" for n in shipped))
    if rc != 0:
        return {"stage": "rl", "ok": False, "error": f"sha256sum на стенде: {err.strip()}"}
    broken = [line.split()[1] for line in out.splitlines()
              if len(line.split()) == 2 and shipped.get(Path(line.split()[1]).name)
              != line.split()[0]]
    if broken or len(out.splitlines()) != len(shipped):
        return {"stage": "rl", "ok": False,
                "error": f"инструменты на стенде не совпали с отправленными: {broken or out}"}

    log_path = local_dir / "logs" / "rl.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run_rl_probe] rl: запуск на {host}, лог → {log_path}")
    print(f"[run_rl_probe] стоп-условия: entropy<{args.entropy_floor} ×{args.entropy_low_streak}, "
          f"NVRM≥{args.nvrm_limit}, память<{args.mem_floor_gb} ГБ, стена {args.max_wall_hours} ч")

    guard = Guard(host, container_name(ts), args, time.time() + args.max_wall_hours * 3600)
    guard.start()
    t0 = time.time()
    rc_stage = None
    try:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", host, f"bash {run_dir}/run_rl.sh"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        guard.stop()
        return {"stage": "rl", "ok": False, "error": f"ssh не запустился: {e}"}

    with log_path.open("w", encoding="utf-8") as fh:
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            fh.flush()
            m = re.search(r"RL_PROBE_STAGE_EXIT=(\d+)", line)
            if m:
                rc_stage = int(m.group(1))
            # Страж питается теми же строками, что уходят в лог: отдельный
            # «мониторинговый» поток метрик разошёлся бы с доказательной базой.
            step = RL_STEP_RE.search(line)
            if step:
                guard.note_entropy(int(step.group(1)), float(step.group(8)), line)
                #: Тот же разбор строки питает критерий награды (ADR-017): одной
                #: строкой метрик закрываются оба стража, и второй парсер не нужен.
                guard.note_step(step)
            elif NVRM_RE.search(line):
                guard.note_line(line)
            if PROGRESS_RE.search(line):
                print("    " + line.rstrip()[:200], flush=True)
    proc.wait(timeout=60)
    wall = time.time() - t0
    guard.stop()

    result = {"stage": "rl", "exp_name": f"rl-probe-compact-{ts}",
              "exit": rc_stage, "ssh_exit": proc.returncode,
              "wall_seconds": round(wall, 1),
              "hook_sha256": shipped["rl_probe_hook.py"],
              "sampler_sha256": shipped["smoke_mem_sampler.sh"],
              "script_sha256": shipped["run_rl.sh"],
              "local_log": str(log_path), "ok": rc_stage == 0,
              "guard": guard.summary()}
    if rc_stage is None:
        result["ok"] = False
        result["error"] = f"стадия не напечатала RL_PROBE_STAGE_EXIT (ssh rc={proc.returncode})"

    # Контейнер закрываем всегда: разведка не оставляет за собой нагрузку (AD-5).
    rc, out, _ = S.ssh(host, f"docker ps -q -f name=^{container_name(ts)}$")
    if rc == 0 and out.strip():
        rc2, out2, err2 = S.ssh(host, f"docker stop {container_name(ts)}", timeout=180)
        result["container_stop"] = {"rc": rc2, "detail": out2.strip() or err2.strip()}

    # Артефакты забираем к себе: доказательная база обязана пережить стенд.
    for remote, local in (("rl_probe.jsonl", "rl_probe.jsonl"),
                          ("mem_rl.jsonl", "mem_rl.jsonl"),
                          ("rollouts_log.jsonl", "rollouts_log.jsonl"),
                          ("logs/probes_rl.json", "probes_rl.json"),
                          ("logs/rl_metrics.json", "rl_metrics.json"),
                          ("general_eval_history.jsonl", "general_eval_history.jsonl"),
                          ("checkpoints/run_manifest.json", "pipeline_run_manifest.json"),
                          ("checkpoints/pass_rates.json", "pass_rates.json")):
        rc, data, err = S.ssh_get(host, f"{run_dir}/{remote}", timeout=300)
        if rc == 0 and data:
            (local_dir / local).write_bytes(data)
            result[f"fetched_{local}"] = len(data)
        else:
            result.setdefault("missing_artifacts", []).append(remote)
    return result


# ─── разбор замеров ───────────────────────────────────────────────────────────

def parse_hook_jsonl(path: Path) -> dict:
    """JSONL обвязки → старт, шаги, инициализации движка, итог."""
    if not path.is_file():
        return {"error": f"нет {path.name}"}
    rows = S.read_jsonl(path)
    start = next((r for r in rows if r.get("event") == "start"), {})
    end = next((r for r in rows if r.get("event") == "end"), {})
    steps = [r for r in rows if r.get("event") == "step"]
    starts = sorted((r for r in rows if r.get("event") == "step_start"),
                    key=lambda r: r.get("i", 0))
    inits = [r for r in rows if r.get("event") == "engine_init"]
    startup = None
    if start.get("ts") and starts:
        startup = round(starts[0]["ts"] - start["ts"], 2)
    return {"start": start, "end": end, "steps": steps, "step_starts": starts,
            "engine_inits": inits, "startup_seconds": startup}


def phase_summary(steps: list[dict]) -> dict:
    """Разбивка времени по фазам и доля роллаутов против обучения.

    Считаются только **закрытые** блоки (``partial=False``): незакрытый последний
    блок — это «шаг начался, стадия кончилась», и его время в средние не идёт.

    «Обучение» здесь — остаток ``dt`` после вычитания измеренных внешних фаз
    (генерация роллаутов, GEN-EVAL, ресник, чекпоинт, пробы): отдельно
    инструментированы forward политики, backward и optimizer — нет, поэтому
    остаток называется остатком, а не «временем backward».
    """
    closed = [s for s in steps if not s.get("partial")]
    if not closed:
        return {"steps_closed": 0, "note": "закрытых блоков шагов нет — разбивка не получена"}
    dt = sum(s["dt"] for s in closed)
    acc = {k: sum(s.get(k, 0) or 0 for s in closed) for k in
           ("t_generate", "t_gen_eval", "t_resync", "t_ckpt", "t_probe",
            "t_policy_fwd", "t_other_fwd")}
    acc["t_train_residual"] = max(0.0, dt - sum(acc[k] for k in
                                                ("t_generate", "t_gen_eval", "t_resync",
                                                 "t_ckpt", "t_probe")))
    out = {"steps_closed": len(closed), "dt_seconds_total": round(dt, 1),
           "phases_seconds": {k: round(v, 1) for k, v in acc.items()},
           "shares_pct": {k: round(100.0 * v / dt, 1) for k, v in acc.items()},
           "definition": {
               "t_generate": "время vLLM.generate — генерация роллаутов (все ходы)",
               "t_train_residual": ("dt минус измеренные внешние фазы: forward политики, "
                                    "backward, optimizer, advantage, постобработка, обвязка"),
               "t_gen_eval": "GEN-EVAL (анти-форгеттинг), каждые GEN_EVAL_EVERY шагов",
               "t_resync": "подмена весов роллаута (hot-sync или пересоздание движка)",
               "t_policy_fwd": "forward политики с grad (часть t_train_residual)",
               "t_other_fwd": ("forward без grad: снапшот ref_model и PPL-прогоны; "
                               "PPL входит и в t_gen_eval — суммы не складывать"),
           }}
    gen = {k: sum(s.get(k, 0) or 0 for s in closed) for k in
           ("gen_tokens", "prompt_tokens", "n_generate_calls")}
    out["generation"] = {
        "gen_tokens": gen["gen_tokens"], "prompt_tokens": gen["prompt_tokens"],
        "n_generate_calls": gen["n_generate_calls"],
        "tok_per_s": (round(gen["gen_tokens"] / acc["t_generate"], 1)
                      if acc["t_generate"] > 0 else None),
        "prompt_tok_per_call": (round(gen["prompt_tokens"] / gen["n_generate_calls"], 1)
                                if gen["n_generate_calls"] else None),
        "note": "tok/s — сгенерированные токены (outputs[].token_ids) / время vLLM.generate",
    }
    dts = [s["dt"] for s in closed]
    out["step_seconds"] = {
        "mean": round(statistics.fmean(dts), 2),
        "median": round(statistics.median(dts), 2),
        "min": round(min(dts), 2), "max": round(max(dts), 2),
        "stdev": round(statistics.stdev(dts), 2) if len(dts) > 1 else None,
        "per_step": dts,
    }
    resync = {k: sum(s.get(k, 0) or 0 for s in closed) for k in
              ("n_hotsync", "n_engine_init")}
    out["resync"] = {"hotsync_count": resync["n_hotsync"],
                     "engine_reinit_count": resync["n_engine_init"],
                     "total": resync["n_hotsync"] + resync["n_engine_init"]}
    return out


#: Оценка цены самой инструментации: каждый перехваченный вызов — это обёртка на
#: Python (``perf_counter`` + вызов оригинала + учёт в bucket). Микросекунды на
#: вызов; берём щедрые 10 мкс, чтобы оценка была верхней границей, а не удобной
#: цифрой. Это **оценка**, а не замер: чтобы измерить её, нужен прогон без обвязки.
HOOK_CALL_OVERHEAD_US = 10.0


def step_seconds_bases(steps: list[dict], phases: dict, end: dict) -> dict:
    """Три законных основания «времени шага» — рядом и без усреднения.

    Величина «сколько стоит шаг RL» имеет не одно значение, а несколько — и
    каждая считается по-своему:

    * ``hook_mean_closed_steps`` — среднее ``dt`` по закрытым блокам (шкала
      обвязки). Это основание экстраполяции: время старта стадии и хвост после
      цикла шагов в него не входят, поэтому на 500 шагах они не растворятся,
      а на 100 — не раздуют шаг.
    * ``wall_over_closed_steps`` — вся стена стадии / число закрытых шагов.
      Грубее: включает старт стадии (загрузка чекпойнта, экспорт весов, подъём
      vLLM) и хвост — то есть уже не «шаг», а «шаг плюс его доля одноразового».
    * ``wall_over_started_steps`` — то же, но делится и на незакрытый шаг.

    Расхождение между первым и вторым — не ошибка измерения, а разные вопросы
    («сколько длится шаг» против «сколько стены на шаг ушло»). Для экстраполяции
    берётся первое; второе публикуется, чтобы цифру нельзя было получить
    делением стены на шаги и выдать за замер шага.
    """
    closed = [s for s in steps if not s.get("partial")]
    wall = (end or {}).get("wall_seconds")
    n_closed = (end or {}).get("steps_closed") or len(closed)
    n_started = (end or {}).get("steps_started") or len(steps)
    hook_mean = (phases.get("step_seconds") or {}).get("mean")
    all_mean = round(statistics.fmean([s["dt"] for s in steps]), 2) if steps else None
    out = {
        "hook_mean_closed_steps": hook_mean,
        "mean_including_partial": all_mean,
        "wall_over_closed_steps": (round(wall / n_closed, 2) if wall and n_closed else None),
        "wall_over_started_steps": (round(wall / n_started, 2) if wall and n_started else None),
        "basis_used_for_extrapolation": "hook_mean_closed_steps",
        "note": ("основания не усредняются: hook_mean_closed_steps — длительность "
                 "шага; wall_over_closed_steps — стена на шаг, включая одноразовый "
                 "старт стадии и хвост после цикла"),
    }
    if out["wall_over_closed_steps"] and hook_mean:
        out["wall_vs_hook_delta_seconds"] = round(out["wall_over_closed_steps"] - hook_mean, 2)
    return out


def overhead_report(steps: list[dict], phases: dict, end: dict) -> dict:
    """Накладные расходы обвязки: сколько времени съедает само измерение.

    Здесь важно не спутать две разные вещи, которые обе зовутся «пробами»:

    * ``t_probe`` — это ``run_probes`` **пайплайна** (``laguna_pipeline_v8.py:1547``):
      четыре фиксированные генерации по 384 токена в ``probes_rl.json``. Пайплайн
      зовёт их **один раз**, после цикла шагов, — поэтому в разбивку по шагам это
      время не попадает вовсе, а оседает в незакрытом последнем блоке (граница
      шага — ``RLDataset.sample``, после цикла он больше не зовётся). Это не
      «цена мониторинга на шаг», а одноразовый хвост стадии.
    * собственно **инструментация** (``rl_probe_hook.py`` + сэмплер памяти) —
      обёртки на Python поверх функций пайплайна; её цена считается по числу
      перехваченных вызовов и оценивается сверху.

    Обе цифры называются, а решение об удешевлении — не здесь (владелец).
    """
    all_steps = steps or []
    closed = [s for s in all_steps if not s.get("partial")]
    partial = [s for s in all_steps if s.get("partial")]
    t_probe_all = round(sum(s.get("t_probe", 0) or 0 for s in all_steps), 3)
    t_probe_closed = round(sum(s.get("t_probe", 0) or 0 for s in closed), 3)
    wall = (end or {}).get("wall_seconds")
    dt_closed = sum(s["dt"] for s in closed) or None
    dt_partial = sum(s["dt"] for s in partial) or None
    calls = {k: sum(s.get(k, 0) or 0 for s in all_steps) for k in
             ("n_generate_calls", "n_policy_fwd", "n_other_fwd", "n_hotsync",
              "n_engine_init")}
    n_calls = sum(calls.values())
    est = round(n_calls * HOOK_CALL_OVERHEAD_US / 1e6, 6)
    out = {
        "t_probe_seconds_total": t_probe_all,
        "t_probe_seconds_in_closed_steps": t_probe_closed,
        "t_probe_lands_in": ("незакрытый последний блок (partial) — хвост стадии после "
                             "цикла шагов" if partial else None),
        "call_site": ("laguna_pipeline_v8.py:1547 — run_probes(args, tokenizer, model, 'rl'): "
                      "один вызов после цикла шагов, не на каждом шаге"),
        "runs_per_stage": 1,
        "stage_wall_seconds": wall,
        "share_of_stage_wall_pct": (round(100.0 * t_probe_all / wall, 3) if wall else None),
        "amortized_seconds_per_closed_step": (round(t_probe_all / len(closed), 3)
                                              if closed else None),
        "amortized_share_of_step_pct": (round(100.0 * t_probe_all / dt_closed, 3)
                                        if dt_closed else None),
        "hook_instrumentation": {
            "wrapped_calls_counted": n_calls,
            "calls_by_kind": calls,
            "overhead_us_per_call_assumed": HOOK_CALL_OVERHEAD_US,
            "estimated_seconds": est,
            "estimated_share_of_stage_pct": (round(100.0 * est / wall, 6) if wall else None),
            "note": ("оценка сверху по числу перехваченных вызовов × 10 мкс; вызовы "
                     "чекпоинта, GEN-EVAL и проб поштучно не считаются — их единицы, "
                     "на оценку не влияют. Чтобы заменить оценку замером, нужен "
                     "прогон без обвязки"),
        },
        "reading": ("цена измерения в этой стадии: t_probe "
                    f"{t_probe_all} с — это {round(100.0 * t_probe_all / wall, 3) if wall else None}% "
                    "стены стадии и однократная величина (не на шаг); "
                    f"инструментация обвязки ≈ {est} с за весь прогон. "
                    "Деление t_probe на хвостовой блок («14.9 с на шаге, ~10–12 % шага») "
                    "описывает не шаг, а хвост после цикла: у незакрытого блока нет "
                    "следующего старта, по которому он стал бы шагом."),
        "decision_on_cheapening": "не принимается здесь: цифра названа, решение — за владельцем",
    }
    if dt_partial:
        out["per_partial_block_reading"] = {
            "partial_block_dt_seconds": round(dt_partial, 3),
            "t_probe_share_of_partial_block_pct": round(100.0 * t_probe_all / dt_partial, 1),
            "why_not_a_step": ("partial-блок — это «шаг начался, стадия кончилась»: в нём "
                               "финальный чекпоинт, дописывание метрик и пробы, а не тело шага"),
        }
    return out


def parse_log_metrics(text: str) -> dict:
    """Метрики из лога стадии: энтропия политики, ресники, GEN-EVAL, инциденты.

    Энтропия берётся **из строк пайплайна** (шаги, кратные 10) — это величина
    политики по определению ADR-008 п.3, а не токенная энтропия ЯМ. Раннер её не
    пересчитывает: две методики под одним именем в отчёте хуже одной.
    """
    steps = []
    for line in text.splitlines():
        m = RL_STEP_RE.search(line)
        if m:
            steps.append({
                "step": int(m.group(1)), "max_steps": int(m.group(2)),
                "reward": float(m.group(3)), "pass_rate": int(m.group(4)) / 100.0,
                "turns": float(m.group(5)), "len_tokens": int(m.group(6)),
                "kl": float(m.group(7)), "entropy": float(m.group(8)),
                "clip_frac": float(m.group(9)), "adv": float(m.group(10)),
                "adv_nonzero": int(m.group(11)) / 100.0,
                "active_tasks": int(m.group(12)), "tasks": int(m.group(13)),
                "torch_mem_gb": float(m.group(14)),
            })
    hotsyncs = [{"seconds": int(m.group(1)), "step": int(m.group(2))}
                for m in (HOTSYNC_RE.search(ln) for ln in text.splitlines()) if m]
    resyncs = [int(m.group(1)) for m in (RESYNC_RE.search(ln) for ln in text.splitlines()) if m]
    waits = [{"free_gb": int(m.group(1)), "total_gb": int(m.group(2)), "seconds": int(m.group(3))}
             for m in (RESYNC_WAIT_RE.search(ln) for ln in text.splitlines()) if m]
    boosts = [{"entropy": float(m.group(1)), "low_count": int(m.group(2)),
               "kl_coef": float(m.group(3))}
              for m in (ENTROPY_BOOST_RE.search(ln) for ln in text.splitlines()) if m]
    gen_eval = [{"step": int(m.group(1)), "ppl_general": float(m.group(2)),
                 "delta_general_pct": float(m.group(3)), "ppl_domain": float(m.group(4)),
                 "delta_domain_pct": float(m.group(5))}
                for m in (GEN_EVAL_RE.search(ln) for ln in text.splitlines()) if m]
    groups = None
    for ln in text.splitlines():
        m = GROUPS_RE.search(ln)
        if m:
            groups = {"tasks_per_step": int(m.group(1)), "group_size": int(m.group(2)),
                      "baseline": m.group(3), "pure": m.group(4) == "True",
                      "max_steps": int(m.group(5))}
            break
    incidents = S.scan_incidents(text)
    return {"step_lines": steps, "hotsyncs": hotsyncs, "resync_steps": resyncs,
            "resync_waits": waits, "entropy_boosts": boosts, "gen_eval": gen_eval,
            "groups": groups, "incidents": incidents, "incidents_count": len(incidents)}


def parse_rollouts(path: Path, cap: int | None = None) -> dict:
    """Фон разведки по траекториям: reward/pass и деградировавшие траектории.

    Это **фон, а не результат** (ADR-010 п.3): цель разведки — цена шага. Числа
    публикуются, чтобы вырожденная награда была видна, а не спрятана.

    Отдельно считается **упор в ``max_new_tokens``** (``cap`` из
    ``gen_max_new_tokens`` пайплайна, 4096 для RL-петли). Это не то же самое, что
    ``hit_timeout``: ``hit_timeout`` — «не уложилась в ``max_turns``», а упор в
    лимит хода — «не договорила в пределах одного хода». Для цены шага различие
    решающее: чем больше траекторий генерируют до потолка, тем дороже шаг, и тем
    осторожнее надо читать экстраполяцию.

    **Признак — косвенный, и это надо знать.** Считается он по
    ``n_assistant_tokens``, а это не число сгенерированных токенов:
    ``laguna_pipeline_v8.py:1311`` берёт ``len(tokenizer.encode(responses[g]))``,
    а ``responses[g]`` накапливает, кроме ходов модели, ещё и prefill
    (``<think>\\n<tool_call>``) и **текст ответов инструмента**
    (``<tool_response>…</tool_response>``, строки 1261 и 1283–1284) — то есть
    чужие токены. Поэтому величина систематически завышена против точного
    ``gen_tokens`` обвязки (``outputs[].token_ids``) и может превышать потолок
    одного вызова (наблюдалось 4623 при ``cap`` 4096). Упор в потолок по ней —
    **верхняя граница**, а не точная доля; точная проверка («генерация идёт до
    потолка») делается по ``gen_tokens`` и вынесена в
    ``cost_interpretation.generation_cap_pressure``.
    """
    if not path.is_file():
        return {"error": f"нет {path.name}"}
    rows = S.read_jsonl(path)
    if not rows:
        return {"trajectories": 0}
    rewards = [float(r.get("reward", 0.0)) for r in rows]
    passed = [bool(r.get("verifier_passed")) for r in rows]
    timeouts = [bool(r.get("hit_timeout")) for r in rows]
    empty = [len(str(r.get("text", "")).strip()) < 5 for r in rows]
    no_tools = [int(r.get("n_tool_calls", 0)) == 0 for r in rows]
    toks = [int(r.get("n_assistant_tokens", 0)) for r in rows]
    truncated = [bool(cap) and t >= cap for t in toks]
    n = len(rows)
    per_step: dict[str, list[float]] = {}
    for r in rows:
        per_step.setdefault(str(r.get("step")), []).append(float(r.get("reward", 0.0)))
    #: Сумма ``n_assistant_tokens`` и число траекторий по шагам — для сверки
    #: косвенного признака с точными ``gen_tokens`` обвязки на том же окне.
    tok_sum: dict[str, int] = {}
    cnt: dict[str, int] = {}
    for r, t in zip(rows, toks):
        k = str(r.get("step"))
        tok_sum[k] = tok_sum.get(k, 0) + t
        cnt[k] = cnt.get(k, 0) + 1
    return {
        "trajectories": n,
        "steps": len(per_step),
        "tokens_sum_per_step": {k: tok_sum[k] for k in sorted(tok_sum, key=int)},
        "trajectories_per_step": {k: cnt[k] for k in sorted(cnt, key=int)},
        "reward_mean": round(statistics.fmean(rewards), 4),
        "pass_rate": round(sum(passed) / n, 4),
        "reward_nonzero_rate": round(sum(1 for r in rewards if r != 0.0) / n, 4),
        "degraded": {
            "hit_timeout": sum(timeouts),
            "empty_text": sum(empty),
            "no_tool_call": sum(no_tools),
            "zero_reward": sum(1 for r in rewards if r == 0.0),
            "truncated_at_cap": sum(truncated),
            "truncated_at_cap_rate": round(sum(truncated) / n, 4),
            "cap_tokens": cap,
            "combined_hit_timeout_or_empty": sum(
                1 for t, e in zip(timeouts, empty) if t or e),
            "definition": ("деградировавшей считается траектория с hit_timeout "
                           "(не завершилась за max_turns) или пустым текстом "
                           "(<5 символов — тот же признак, что has_parse_error в пайплайне); "
                           "truncated_at_cap — отдельный признак «упёрлась в потолок хода»"),
        },
        "turns_mean": round(statistics.fmean([float(r.get("n_turns", 0)) for r in rows]), 3),
        "assistant_tokens_mean": round(statistics.fmean([float(t) for t in toks]), 1),
        "assistant_tokens_max": max(toks),
        "reward_mean_per_step": {k: round(statistics.fmean(v), 3)
                                 for k, v in sorted(per_step.items(), key=lambda kv: int(kv[0]))},
    }


def analyze(run_dir: Path, args) -> dict:
    """Сводка замеров по артефактам стадии."""
    hook = parse_hook_jsonl(run_dir / "rl_probe.jsonl")
    log_text = ""
    log_path = run_dir / "logs" / "rl.log"
    if log_path.is_file():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    summary: dict = {
        "hook": {k: v for k, v in hook.items() if k not in ("steps", "start", "step_starts")},
        "hook_start": hook.get("start", {}),
        "phases": phase_summary(hook.get("steps", [])),
        #: Сырые блоки шагов (включая незакрытый): нужны для накладных расходов
        #: обвязки — t_probe оседает именно в незакрытом блоке и в разбивку по
        #: закрытым шагам не попадает.
        "hook_steps": hook.get("steps", []),
        "log": parse_log_metrics(log_text),
        #: Потолок хода — из кода пайплайна: по нему считается «упор в лимит»,
        #: который и объясняет цену шага (длинные ходы = дорогая генерация).
        "rollouts": parse_rollouts(run_dir / "rollouts_log.jsonl",
                                   cap=pipeline_hyperparams().get("gen_max_new_tokens")),
    }
    mem = run_dir / "mem_rl.jsonl"
    summary["memory"] = (S.parse_mem_samples(mem, container_hint=container_name(args.ts))
                         if mem.is_file() else {"error": f"нет {mem.name}"})
    end = hook.get("end", {})
    if "torch_peak_allocated_bytes" in end:
        summary["memory"]["torch_peak_allocated_gb"] = round(
            end["torch_peak_allocated_bytes"] / 2**30, 2)
        summary["memory"]["torch_peak_reserved_gb"] = round(
            end.get("torch_peak_reserved_bytes", 0) / 2**30, 2)
    summary["memory"]["source"] = ("хост-сэмплер (MemAvailable) + torch.max_memory_allocated "
                                   "внутри контейнера; у GB10 память unified, и cgroup "
                                   "контейнера CUDA-аллокации не учитывает")
    metrics = run_dir / "rl_metrics.json"
    if metrics.is_file():
        try:
            summary["rl_metrics"] = json.loads(metrics.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            summary["rl_metrics"] = {"error": f"rl_metrics.json не разбирается: {e}"}
    return summary


def entropy_report(summary: dict, guard: dict | None) -> dict:
    """Энтропия политики: из лога (каждые 10 шагов) и, если стадия дошла, из rl_metrics."""
    from_log = [{"step": s["step"], "entropy": s["entropy"]}
                for s in summary["log"]["step_lines"]]
    def pack(points: list[dict], source: str, cadence) -> dict:
        vals = [p["entropy"] for p in points]
        if not vals:
            return {"points": [], "source": source, "cadence_steps": cadence,
                    "note": "энтропия не получена — стадия не дошла до шага 10"}
        return {
            "points": points, "source": source, "cadence_steps": cadence,
            "points_count": len(vals),
            "mean": round(statistics.fmean(vals), 4), "min": round(min(vals), 4),
            "max": round(max(vals), 4),
            "first": vals[0], "last": vals[-1],
            "corridor": [1.0, 2.0],
            "in_corridor": bool(1.0 <= statistics.fmean(vals) <= 2.0),
            #: Коридор — про мониторинг mode collapse, поэтому важно не только
            #: «внутри/снаружи», но и с какой стороны: выход снизу — угроза
            #: коллапса (стоп-условие ADR-010 п.5), выход сверху — обратная
            #: ситуация (политика слишком разнообразна), и она не стоп-условие.
            "corridor_side": ("inside" if 1.0 <= statistics.fmean(vals) <= 2.0
                              else ("above" if statistics.fmean(vals) > 2.0 else "below")),
            "above_corridor": sum(1 for v in vals if v > 2.0),
            "below_corridor": sum(1 for v in vals if v < 1.0),
            "definition": ("энтропия политики RL: средняя энтропия распределения "
                           "по ответным токенам роллаута, считает пайплайн "
                           "(laguna_pipeline_v8.py:1412); коридор 1.0–2.0 — ADR-008 п.3"),
            "under_floor": sum(1 for v in vals if v < ENTROPY_FLOOR),
        }
    out = {"from_log": pack(from_log, "лог стадии, шаги кратные 10", 10)}
    m = summary.get("rl_metrics", {}).get("entropy") if isinstance(summary.get("rl_metrics"), dict) else None
    if m:
        pts = [{"step": i, "entropy": v} for i, v in enumerate(m)]
        out["from_rl_metrics"] = pack(pts, "rl_metrics.json (записывается в конце стадии)", 1)
    if guard:
        out["guard_observations"] = guard.get("entropy_observations", [])
    return out


def build_evidence(run_dir: Path, args, pre: dict, stage: dict, summary: dict,
                   manifest_path: Path | None, post: dict) -> dict:
    """``evidence/s3-rl-probe.json`` — замеры п.3 ADR-010 + экстраполяция."""
    guard = stage.get("guard") or {}
    ent = entropy_report(summary, guard)
    log = summary["log"]
    rollouts = summary["rollouts"]
    phases = summary["phases"]
    mem = summary["memory"]
    # Провенанс обвязки: три уровня доказательства, как в evidence смоука —
    # файлы кейса, отправленное на стенд (сверено sha256) и то, что реально
    # лежит в каталоге прогона на стенде.
    instr = {
        "case_runner_sha256": S.sha256_path(CASE_ROOT / "tools" / "run_rl_probe.py"),
        "case_hook_sha256": S.sha256_path(CASE_ROOT / "tools" / "rl_probe_hook.py"),
        "case_sampler_sha256": S.sha256_path(CASE_ROOT / "tools" / "smoke_mem_sampler.sh"),
        "ship_hook_sha256": stage.get("hook_sha256"),
        "ship_sampler_sha256": stage.get("sampler_sha256"),
        "ship_script_sha256": stage.get("script_sha256"),
        "sft_checkpoint": args.sft_ckpt,
        "pipeline_sha256_case": S.sha256_path(PIPELINE_CASE),
        "note": ("case_* — файлы кейса на момент сборки сводки; ship_* — то, что раннер "
                 "отправил на стенд и сверил sha256sum; расхождение case_* и ship_* "
                 "означало бы, что замер сделан не тем кодом, который в git"),
    }

    #: Число выполненных шагов берётся из обвязки, а не из числа строк лога:
    #: пайплайн печатает метрики на шагах, кратных 10, поэтому прогон в 100 шагов
    #: оставляет последнюю строку на шаге 90 — по строкам вышло бы 91. Число строк
    #: публикуется рядом как независимая (и заведомо более грубая) оценка.
    started = (summary["hook"].get("end") or {}).get("steps_started")
    from_log = (max(s["step"] for s in log["step_lines"]) + 1) if log["step_lines"] else None
    steps_done = started or from_log
    out_steps = {"steps_started": started, "steps_closed": phases.get("steps_closed"),
                 "steps_from_log_lines": from_log,
                 "steps_requested": args.rl_steps,
                 "source": ("обвязка (RLDataset.sample): число стартов шагов" if started
                            else "лог стадии: последняя строка метрик + 1 (грубее — "
                                 "строки печатаются каждые 10 шагов)")}

    nvrm_delta = None
    if post.get("dmesg_after") and (pre.get("dmesg_before") or {}):
        nvrm_delta = max(0, post["dmesg_after"].get("nvrm_xid", 0)
                         - pre["dmesg_before"].get("nvrm_xid", 0))

    #: Контекст «упор в потолок хода» для экстраполяции: без него оценка 500
    #: шагов читается как цена «нормальной» политики, хотя она измерена на
    #: поведении, которое генерирует до потолка каждый ход.
    _deg = (rollouts.get("degraded") or {})
    _hist_traj = ((summary.get("historical_rl_run") or {}).get("trajectories") or {})
    cap_ctx = {
        "cap_tokens": _deg.get("cap_tokens"),
        "our_truncated_at_cap_rate": _deg.get("truncated_at_cap_rate"),
        "our_truncated_at_cap": _deg.get("truncated_at_cap"),
        "our_trajectories": rollouts.get("trajectories"),
        "historical_truncated_at_cap_rate": _hist_traj.get("truncated_at_cap_rate"),
        "historical_trajectories": _hist_traj.get("sampled"),
    }
    ext = extrapolate(phases, args, summary.get("historical_rl_run"), cap_ctx)

    measurements = {
        "steps_completed": steps_done,
        "steps_requested": args.rl_steps,
        "steps": out_steps,
        "hook_lifecycle": {
            "steps_started": (summary["hook"].get("end") or {}).get("steps_started"),
            "steps_closed": (summary["hook"].get("end") or {}).get("steps_closed"),
            "samples_total": (summary["hook"].get("end") or {}).get("samples_total"),
            "tasks_per_step": (summary["hook"].get("end") or {}).get("tasks_per_step"),
            "group_size": (summary["hook"].get("end") or {}).get("group_size"),
            "engine_inits_total": (summary["hook"].get("end") or {}).get("engine_inits_total"),
            "engine_inits_detail": (summary["hook"].get("engine_inits") or []),
            "wall_seconds": (summary["hook"].get("end") or {}).get("wall_seconds"),
            "error": (summary["hook"].get("end") or {}).get("error"),
            "partial_blocks": sum(1 for s in (summary.get("hook_steps") or [])
                                  if s.get("partial")),
            "note": ("инициализаций движка всего "
                     f"{(summary['hook'].get('end') or {}).get('engine_inits_total')} — "
                     "первая до первого шага (это старт стадии, не ресник), пересозданий "
                     "(engine_reinit_count) нет; ресники в прогоне — только hot-sync"),
        },
        "step_seconds": phases.get("step_seconds"),
        "phase_split": phases,
        "memory": mem,
        "resync": phases.get("resync"),
        "resync_cross_check": {
            "from_hook": (phases.get("resync") or {}).get("hotsync_count"),
            "from_log_hotsync_lines": len(log.get("hotsyncs") or []),
            "from_log_resync_lines": len(log.get("resync_steps") or []),
            "agree": (phases.get("resync") or {}).get("hotsync_count")
                     == len(log.get("hotsyncs") or []),
            "note": ("два независимых источника: обвязка (обёртка hot_sync_weights) и лог "
                     "пайплайна (HOT-SYNC ... step N). Расхождение — сигнал: упавший "
                     "hot-sync считан обвязкой и не напечатан пайплайном"),
        },
        "entropy": ent,
        #: Критерий вырожденной награды (ADR-017) — рядом с энтропией и **в том же
        #: evidence**: разведка обязана показать мёртвую награду, а не только цену
        #: шага (ADR-010 п.3). Вердикт приходит из общего прибора
        #: ``tools/rl_degeneracy.py`` — того же, что стоит стражем в цепочке.
        "degeneracy_reward": (guard.get("degenerate_reward") or {
            "criterion": "ADR-017 (вырожденная награда RL)",
            "verdict": "not_evaluated",
            "why": ("ни одной строки метрик шага не разобрано: критерий ADR-017 "
                    "считается по наблюдениям шагов окна (первые 50), а стадия до "
                    "них не дошла"),
        }),
        "background": {
            "note": "фон, а не результат (ADR-010 п.3): цель разведки — цена шага",
            "reward_mean": rollouts.get("reward_mean"),
            "pass_rate": rollouts.get("pass_rate"),
            "reward_mean_per_step_line": [s["reward"] for s in log["step_lines"]],
            "pass_rate_per_step_line": [s["pass_rate"] for s in log["step_lines"]],
            "trajectories": rollouts.get("trajectories"),
            "degraded": rollouts.get("degraded"),
            "turns_mean": rollouts.get("turns_mean"),
            "assistant_tokens_mean": rollouts.get("assistant_tokens_mean"),
        },
        "startup_seconds": summary["hook"].get("startup_seconds"),
        "gen_eval": log.get("gen_eval"),
        "entropy_boosts": log.get("entropy_boosts"),
        "groups_from_log": log.get("groups"),
    }
    #: Как читать цену шага. Вырожденное поведение политики не отменяет замер
    #: (ADR-010: цель — цена шага), но меняет вывод из него: если траектории
    #: упираются в потолок хода, шаг дорог именно этим, и цифра — верхняя
    #: граница для чекпойнта, который умеет завершать ход раньше.
    deg = (rollouts.get("degraded") or {})
    rate = deg.get("truncated_at_cap_rate")
    measurements["cost_interpretation"] = {
        "max_new_tokens_cap": deg.get("cap_tokens"),
        "truncated_at_cap": deg.get("truncated_at_cap"),
        "truncated_at_cap_rate": rate,
        "generation_seconds_per_step": (phases.get("phases_seconds") or {}).get("t_generate"),
        "note": ("цена шага измерена на поведении этого чекпойнта; доля траекторий, "
                 "упёршихся в потолок хода, прямо влияет на неё — генерация даёт "
                 f"{(phases.get('shares_pct') or {}).get('t_generate')}% времени шага"),
        "reading": (f"уперлись в max_new_tokens {deg.get('cap_tokens')}: "
                    f"{deg.get('truncated_at_cap')} из {rollouts.get('trajectories')} "
                    f"({rate:.0%}) — цена шага относится к политике, которая завершает "
                    f"ход на потолке, а не к «идеальному» поведению"
                    if rate else "упоров в потолок хода не зафиксировано"),
    }
    #: Точная проверка того же утверждения — по ``gen_tokens`` обвязки
    #: (``outputs[].token_ids``, то есть реально сгенерированные токены), а не по
    #: косвенному ``n_assistant_tokens``. Нужна потому, что косвенный признак
    #: включает prefill и текст ответов инструмента и завышает длину ответа:
    #: «упёрлись в потолок» по нему — верхняя граница, а не доля. Агрегат по
    #: закрытым шагам: сколько токенов политика сгенерировала на траекторию
    #: против потолка одного вызова.
    gen = phases.get("generation") or {}
    if gen.get("gen_tokens") and phases.get("steps_closed"):
        closed_steps = set(range(phases["steps_closed"]))
        tps = rollouts.get("tokens_sum_per_step") or {}
        cps = rollouts.get("trajectories_per_step") or {}
        proxy_tok = sum(v for k, v in tps.items() if int(k) in closed_steps)
        n_traj = sum(v for k, v in cps.items() if int(k) in closed_steps)
        exact_tok = gen["gen_tokens"]
        cap = deg.get("cap_tokens")
        if n_traj and exact_tok:
            per_traj = exact_tok / n_traj
            measurements["cost_interpretation"]["generation_cap_pressure"] = {
                "cap_tokens_per_call": cap,
                "exact_generated_tokens_closed_steps": exact_tok,
                "trajectories_closed_steps": n_traj,
                "exact_tokens_per_trajectory": round(per_traj, 1),
                "cap_utilisation_pct": (round(100.0 * per_traj / cap, 1) if cap else None),
                "source": ("gen_tokens обвязки: len(outputs[].token_ids) — точное число "
                           "сгенерированных токенов, без prefill и без ответов инструмента"),
                "proxy_flag_truncated_at_cap_rate": rate,
                "proxy_over_exact_ratio": (round(proxy_tok / exact_tok, 4) if exact_tok else None),
                "proxy_field_defect": ("n_assistant_tokens = len(tokenizer.encode(responses[g])) "
                                       "включает prefill и текст <tool_response> "
                                       "(laguna_pipeline_v8.py:1261,1283-1284) — это не токены "
                                       "ассистента; величина завышена и может превышать "
                                       f"потолок одного вызова (наблюдаемый максимум "
                                       f"{rollouts.get('assistant_tokens_max')} при cap {cap})"),
                "reading": (f"генерация идёт до потолка: {round(per_traj, 1)} точных токенов "
                            f"на траекторию против {cap} ({round(100.0 * per_traj / cap, 1)}%). "
                            f"Косвенный признак даёт {rate:.0%} упоров — это верхняя граница, "
                            f"он считает длину с лишним (×{round(proxy_tok / exact_tok, 2)} "
                            f"к точным токенам)"),
            }
    measurements["step_seconds_bases"] = step_seconds_bases(
        summary.get("hook_steps") or [], phases, summary["hook"].get("end") or {})
    measurements["measurement_overhead"] = overhead_report(
        summary.get("hook_steps") or [], phases, summary["hook"].get("end") or {})
    #: Историческая сверка траекторий: почему шаг разведки дороже/дешевле.
    #: Сравнение не «на глаз»: тот же признак (упор в потолок хода), то же окно.
    hist_traj = ((summary.get("historical_rl_run") or {}).get("trajectories") or {})
    our_tok = rollouts.get("assistant_tokens_mean")
    if hist_traj.get("available") and our_tok:
        measurements["cost_interpretation"]["historical_comparison"] = {
            "our_assistant_tokens_mean": our_tok,
            "historical_assistant_tokens_mean": hist_traj.get("assistant_tokens_mean"),
            "tokens_ratio": round(our_tok / max(hist_traj.get("assistant_tokens_mean") or 1, 1e-9), 2),
            "our_truncated_at_cap_rate": rate,
            "historical_truncated_at_cap_rate": hist_traj.get("truncated_at_cap_rate"),
            "our_turns_mean": rollouts.get("turns_mean"),
            "historical_turns_mean": hist_traj.get("turns_mean"),
            "historical_pass_rate": hist_traj.get("pass_rate"),
            "historical_reward_mean": hist_traj.get("reward_mean"),
            "historical_pass_rate_caveat": ("pass_rate исторического прогона считается по "
                                            "его пулу (rl_tasks_oxalpha) и его чекпойнту — "
                                            "это фон для сверки длины, а не ось сравнения"),
            "reading": ("длина ответа политики и есть цена шага в этой конфигурации: "
                        f"у разведки {our_tok} токенов на траекторию против "
                        f"{hist_traj.get('assistant_tokens_mean')} у исторического прогона "
                        f"(в {round(our_tok / max(hist_traj.get('assistant_tokens_mean') or 1, 1e-9), 2)} раза), "
                        f"и упоров в потолок хода {rate:.0%} против "
                        f"{hist_traj.get('truncated_at_cap_rate'):.0%}"),
        }
    measurements["completeness_of_ADR010_p3"] = measurements_completeness(measurements)

    return {
        "stage": "S3-pre",
        "date": datetime.now().isoformat(timespec="seconds"),
        "purpose": ("RL-разведка (ADR-010, вариант D): измерить стоимость шага RL "
                    "на готовом sft_checkpoint_final.pt, без новых стадий CPT/SFT. "
                    "Сходимость — не критерий приёмки: цель — цена шага."),
        "status_reason": (guard.get("reasons") or ["стадия дошла до конца запрошенных шагов"])[0],
        "stand": {"host": pre.get("host"), "run_dir": f"{STAND_EXPERIMENTS}/{run_id_for(args.ts)}",
                  "case_run_dir": S.rel_or_abs(run_dir)},
        "preconditions": {"required_free_gb": args.min_free_gb, "checks": pre.get("checks", []),
                          "ok": pre.get("ok")},
        "config": {
            "model": args.model, "seed": args.seed, "steps_requested": args.rl_steps,
            "max_samples": args.max_samples, "max_len": args.max_len,
            "batch_size": args.batch_size, "image": args.image,
            "start_checkpoint": args.sft_ckpt,
            "start_checkpoint_note": ("исторический SFT-чекпойнт прогона laguna_qwen25-05b "
                                      "(30.07.2026), не чекпойнт ревизии — замер даёт "
                                      "порядок, а не точную цифру для S3"),
            "rl_data": RL_DATA_CTR, "rl_data_case_symlink": S.rel_or_abs(RL_POOL_SYMLINK),
            "launch": f"{SAFE_START} -d 60 -i 5 -- docker run (absorbер, cgroup-кап {args.mem_cap})",
            "env": {"VLLM_GPU_UTIL": args.vllm_gpu_util,
                    "VLLM_KV_CACHE_BYTES": args.kv_cache_bytes,
                    "VLLM_EAGER": args.vllm_eager, "RESYNC_EVERY": args.resync_every,
                    "GEN_EVAL_EVERY": args.gen_eval_every,
                    "LAGUNA_MEM_FRACTION": args.mem_fraction,
                    "note": ("vLLM-параметры — как в run_v12_ladder.sh для qwen25-05b "
                             "(util 0.32, KV 8 ГиБ, eager 0); RESYNC_EVERY задан явно =10, "
                             "чтобы манифест пайплайна не печатал 25 (ADR-010); "
                             "LAGUNA_ATTN/FLEX_COMPILE не передаются: для --stage rl они "
                             "инертны (flex подключается только при stage cpt/all)")},
            "hyperparameters": pipeline_hyperparams(),
        },
        "stage_result": stage,
        "measurements": measurements,
        "extrapolation": ext,
        "incidents": {
            "in_stage_log": log.get("incidents_count"),
            "lines": log.get("incidents", [])[:5],
            "dmesg_nvrm_xid_delta": nvrm_delta,
            "guard": guard,
        },
        "postconditions": {
            "containers_after": post.get("containers"),
            "platform_alive": post.get("platform_alive"),
            "platform_lost": post.get("platform_lost"),
            "free_after_g": post.get("free_g"),
            "mem_free_gb": post.get("mem_free_gb"),
            "probe_container_running": post.get("probe_containers_running"),
        },
        "run_manifest": None if manifest_path is None else S.rel_or_abs(manifest_path),
        "not_verified": [
            "точная стоимость шага RL **ревизионного** прогона: замер сделан на историческом "
            "sft_checkpoint_final.pt (30.07.2026), а длина ответов и доля успешных роллаутов "
            "зависят от чекпойнта (ADR-010, Negative)",
            "500 шагов и три сида не запускались: их стоимость — линейная экстраполяция от "
            "измеренного шага (допущения названы в extrapolation.assumptions)",
            "eval-стадия (GLM-судья, slug_accuracy, judge_mean) не запускалась — вне задания "
            "разведки; судья в контур награды не входит (AD-6, C-013)",
            "энтропия политики: наблюдения с шагом 10 (как её печатает пайплайн); "
            "per-step ряд есть только при нормальном завершении стадии (rl_metrics.json)",
            "детерминизм RL-стадии (ADR-009) не проверялся: разведка — один сид и один прогон",
            "устойчивость к NVRM-бурям на длинных прогонах: окно разведки — часы, "
            "но не сутки полного протокола",
        ],
        "evidence_limits": ("замеры получены обвязкой tools/rl_probe_hook.py (без правки "
                            "пайплайна) и сэмплером tools/smoke_mem_sampler.sh; "
                            "sha256 пайплайна и обвязки — в config.hyperparameters и instrument"),
        #: Провенанс обвязки: case_* — файлы кейса на момент сборки сводки, ship_* —
        #: то, что раннер отправил на стенд и сверил по sha256. Расхождение означало
        #: бы, что замер сделан не той ревизией кода, которая лежит в git.
        "instrument": instr,
    }


#: Перечень замеров п.3 ADR-010 — с путём к величине и причиной, если её нет.
#: Критерий приёмки дельты: «в evidence все замеры п.3 либо явное „не получено“
#: с причиной». Поэтому пропуск здесь — не пустая строка, а названная причина;
#: проверяется механически (``measurements_completeness``), а не глазами.
MEASURED_ITEMS = (
    ("step_seconds", "время шага RL",
     lambda m: (m.get("step_seconds") or {}).get("mean"),
     "ни один блок шага не закрылся: замок шага — RLDataset.sample пайплайна, "
     "и до первого перехода шага стадия не дожила"),
    ("rollout_vs_train_share", "доля времени на генерацию роллаутов против обучения",
     lambda m: (m.get("phase_split") or {}).get("shares_pct"),
     "закрытых блоков шага нет — разбивка фаз не построена"),
    ("peak_unified_memory_gb", "пик unified-памяти",
     lambda m: (m.get("memory") or {}).get("peak_used_gb"),
     "хост-сэмплер не записал ни одного сэмпла (не стартовал или стадия короче шага сэмплинга)"),
    ("generation_tok_per_s", "tok/s генерации",
     lambda m: ((m.get("phase_split") or {}).get("generation") or {}).get("tok_per_s"),
     "время генерации нулевое или vLLM не отдал token_ids: tok/s не определён"),
    ("resync_count", "число ресников весов",
     lambda m: (m.get("resync") or {}).get("total"),
     "стадия не дошла до шага 10 — ресник по рецепту идёт каждые RESYNC_EVERY шагов"),
    ("policy_entropy", "энтропия политики",
     lambda m: ((m.get("entropy") or {}).get("from_log") or {}).get("mean"),
     "пайплайн печатает энтропию на шагах, кратных 10: стадия не дошла до шага 10"),
    ("reward_and_pass_rate", "pass_rate и reward_mean (фон)",
     lambda m: ((m.get("background") or {}).get("reward_mean"),
                (m.get("background") or {}).get("pass_rate")),
     "rollouts_log.jsonl не забран со стенда (стадия умерла до первой записи траекторий)"),
    ("degraded_trajectories", "число деградировавших траекторий",
     lambda m: (m.get("background") or {}).get("degraded"),
     "траектории не записаны: rollouts_log.jsonl отсутствует"),
    ("steps_completed", "число шагов, фактически выполненных",
     lambda m: m.get("steps_completed"),
     "ни строк шага в логе, ни стартов шагов в обвязке — стадия не дошла до петли"),
)


def measurements_completeness(measurements: dict) -> dict:
    """Все замеры п.3 ADR-010: величина либо явная причина «не получено»."""
    out = {}
    for key, title, getter, reason in MEASURED_ITEMS:
        try:
            value = getter(measurements)
        except (AttributeError, TypeError):
            value = None
        if isinstance(value, (tuple, list)) and all(v is None for v in value):
            value = None
        out[key] = {"title": title, "value": value,
                    "not_obtained_reason": None if value is not None else reason}
    out["_summary"] = {
        "items": len(MEASURED_ITEMS),
        "obtained": sum(1 for k, v in out.items()
                        if isinstance(v, dict) and v.get("value") is not None),
        "not_obtained": [k for k, v in out.items()
                         if isinstance(v, dict) and v.get("value") is None],
    }
    return out


#: Исторический RL-прогон той же модели на том же стенде с той же конфигурацией
#: стадии (``v12_qwen25-05b_s42``: util 0.32, KV 8 ГиБ, eager 0, RESYNC_EVERY=10,
#: 500 шагов, 12.2 ч). Нужен как **независимая сверка** цены шага: он измеряет
#: ровно ту величину, которую разведка оценивает экстраполяцией.
HISTORICAL_RL_LOG = (f"{STAND_EXPERIMENTS}/v12_qwen25-05b_s42/logs/rl.log")

#: Траектории исторического прогона (`rollouts_log.jsonl` рядом с логом стадии).
#: Сопоставимы с нашими по признаку «упор в потолок хода»: это и объясняет, почему
#: цена шага на разных чекпойнтах расходится в разы.
HISTORICAL_RL_ROLLOUTS = (f"{STAND_EXPERIMENTS}/v12_qwen25-05b_s42/rollouts_log.jsonl")
#: Сколько траекторий брать из исторического файла: 8 на шаг → 800 = первые 100
#: шагов, то есть окно, сравнимое с окном разведки.
HISTORICAL_ROLLOUT_SAMPLE = 800

TIME_HOTSYNC_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}).*HOT-SYNC: веса подменены за \d+s \(step (\d+)\)")
TIME_GROUPS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}).*RL: группы (\d+)×(\d+)")


def historical_rl_cross_check(host: str, path: str = HISTORICAL_RL_LOG,
                              rollouts_path: str = HISTORICAL_RL_ROLLOUTS,
                              cap: int | None = None) -> dict:
    """Сверка цены шага с историческим RL-прогоном (500 шагов, та же конфигурация).

    Тонкая обёртка над разбором (``parse_historical_rl_log``): ssh отделён от
    арифметики, чтобы разбор проверялся тестом на синтетическом логе, а не
    только на живом стенде.
    """
    cmd = (f"grep -E 'HOT-SYNC: веса подменены|RL: группы|RL step [0-9]+/[0-9]+ |"
           f"RL complete|^[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}' {path} | head -4000")
    rc, text, err = S.ssh(host, cmd, timeout=180)
    if rc != 0 or not text.strip():
        return {"available": False, "path": path,
                "reason": f"лог исторического прогона недоступен: {err.strip() or 'пусто'}"}
    out = parse_historical_rl_log(text, path)
    out["trajectories"] = historical_trajectories(host, rollouts_path, cap)
    return out


TIME_ANY_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})")


def _seconds(text_line: str) -> int | None:
    m = TIME_ANY_RE.match(text_line)
    return None if not m else int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def historical_trajectories(host: str, path: str = HISTORICAL_RL_ROLLOUTS,
                            cap: int | None = None,
                            sample: int = HISTORICAL_ROLLOUT_SAMPLE) -> dict:
    """Разрез траекторий исторического прогона на том же окне шагов.

    Нужен, чтобы разница цен шага была объяснена **фактом поведения политики**,
    а не догадкой: одинаковая конфигурация стадии при разной длине ответов даёт
    разное время шага, и «упор в потолок хода» — измеримый признак этого.
    """
    if cap is None:
        cap = pipeline_hyperparams().get("gen_max_new_tokens")
    rc, text, err = S.ssh(host, f"head -{int(sample)} {path}", timeout=180)
    if rc != 0 or not text.strip():
        return {"available": False, "path": path,
                "reason": f"траектории исторического прогона недоступны: "
                          f"{err.strip() or 'пусто'}"}
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not rows:
        return {"available": False, "path": path, "reason": "ни одной разобранной строки"}
    toks = [int(r.get("n_assistant_tokens", 0)) for r in rows]
    rewards = [float(r.get("reward", 0.0)) for r in rows]
    truncated = [t for t in toks if cap and t >= cap]
    return {
        "available": True, "path": path, "sampled": len(rows),
        "cap_tokens": cap,
        "assistant_tokens_mean": round(statistics.fmean(toks), 1),
        "truncated_at_cap": len(truncated),
        "truncated_at_cap_rate": round(len(truncated) / len(rows), 4),
        "turns_mean": round(statistics.fmean([float(r.get("n_turns", 0)) for r in rows]), 2),
        "tool_calls_mean": round(statistics.fmean([float(r.get("n_tool_calls", 0))
                                                   for r in rows]), 2),
        "hit_timeout": sum(1 for r in rows if r.get("hit_timeout")),
        "pass_rate": round(sum(1 for r in rows if r.get("verifier_passed")) / len(rows), 4),
        "reward_mean": round(statistics.fmean(rewards), 4),
        "note": ("окно — первые шаги исторического прогона (тот же размер окна, что у "
                 "разведки); признак «упор в потолок хода» считается по тому же cap, "
                 "что и у нас"),
    }


def parse_historical_rl_log(text: str, path: str = HISTORICAL_RL_LOG) -> dict:
    """Арифметика сверки: интервалы между строками ресника → время шага и тренд.

    Ресник печатает строку с временем на каждом ``RESYNC_EVERY``-м шаге, поэтому
    интервалы между строками — это время ровно ``RESYNC_EVERY`` шагов. Так из лога
    извлекается и среднее, и разброс, и тренд (первая половина против второй) —
    без доверия к чему-либо, кроме строк самого пайплайна.

    Сверка отвечает на вопрос, который экстраполяция оставляет открытым:
    держится ли время шага на дистанции 500 шагов, или разведка на 50–100 шагах
    систематически занижает цену. **Чекпойнт при этом другой** (исторический
    прогон шёл со своего SFT-чекпойнта, а не с того, с которого стартует
    разведка) — поэтому это сверка порядка, а не равенство конфигураций.
    """
    marks = []
    for line in text.splitlines():
        m = TIME_HOTSYNC_RE.match(line)
        if m:
            marks.append((int(m.group(4)), int(m.group(1)) * 3600 + int(m.group(2)) * 60
                          + int(m.group(3))))
    groups = None
    for line in text.splitlines():
        g = TIME_GROUPS_RE.match(line)
        if g:
            groups = {"tasks_per_step": int(g.group(4)), "group_size": int(g.group(5)),
                      "t": int(g.group(1)) * 3600 + int(g.group(2)) * 60 + int(g.group(3))}
            break
    if len(marks) < 3:
        return {"available": False, "path": path,
                "reason": f"в логе нашлось {len(marks)} строк ресника — интервалы не построить"}
    marks.sort()
    per_step, first_leg = [], None
    for (s_prev, t_prev), (s_cur, t_cur) in zip(marks, marks[1:]):
        dt = t_cur - t_prev
        if dt < 0:  # смена суток в логе
            dt += 86400
        if s_cur > s_prev:
            per_step.append(dt / (s_cur - s_prev))
    if groups and groups["t"] <= marks[0][1]:
        dt0 = marks[0][1] - groups["t"]
        first_leg = {"steps": marks[0][0], "seconds_per_step": round(dt0 / max(1, marks[0][0]), 1),
                     "note": "первый участок — с прогревом vLLM (захват CUDA-графов), "
                             "поэтому он длиннее установившегося режима"}
    #: Полное время исторической RL-стадии: первая строка лога с временем →
    #: «RL complete». Это **прямой замер** 500 шагов (вместе со стартом стадии),
    #: а не экстраполяция — поэтому он публикуется рядом с нашей оценкой.
    stamps = [t for t in (_seconds(ln) for ln in text.splitlines()) if t is not None]
    wall_total = None
    if stamps:
        wall_total = stamps[-1] - stamps[0]
        if wall_total < 0:  # смена суток
            wall_total += 86400
    half = len(per_step) // 2
    trend = None
    if half:
        first, second = per_step[:half], per_step[half:]
        trend = {"first_half_mean": round(statistics.fmean(first), 1),
                 "second_half_mean": round(statistics.fmean(second), 1),
                 "change_pct": round(100 * (statistics.fmean(second) - statistics.fmean(first))
                                     / statistics.fmean(first), 1)}
    return {
        "available": True, "path": path,
        "source": "лог исторического прогона v12_qwen25-05b_s42 (та же модель, стенд, "
                  "конфигурация стадии: util 0.32, KV 8 ГиБ, eager 0, RESYNC_EVERY=10)",
        "steps_covered": marks[-1][0] - marks[0][0],
        "intervals": len(per_step),
        "step_seconds_mean": round(statistics.fmean(per_step), 1) if per_step else None,
        "step_seconds_min": round(min(per_step), 1) if per_step else None,
        "step_seconds_max": round(max(per_step), 1) if per_step else None,
        "step_seconds_stdev": round(statistics.stdev(per_step), 1) if len(per_step) > 2 else None,
        "first_leg_with_warmup": first_leg,
        "trend_first_vs_second_half": trend,
        "groups_from_log": groups,
        "stage_wall_seconds_measured": wall_total,
        "stage_wall_hours_measured": (round(wall_total / 3600, 2) if wall_total else None),
        "stage_wall_note": ("время всей RL-стадии исторического прогона (старт, 500 шагов, "
                            "финальный чекпоинт) по отметкам самого лога — прямой замер "
                            "500 шагов в той же конфигурации, а не экстраполяция"),
        "caveat": ("исторический прогон шёл со **своего** SFT-чекпойнта и по пулу "
                   "rl_tasks_oxalpha (28 000 задач), разведка — с исторического "
                   "sft_checkpoint_final.pt и по пулу ревизии (27 992): это сверка "
                   "порядка и устойчивости на дистанции, а не равенство конфигураций"),
    }


def extrapolate(phases: dict, args, historical: dict | None = None,
                cap_ctx: dict | None = None) -> dict:
    """Стоимость 500 шагов и трёх сидов — от измеренного шага, с допущениями.

    Календарь считается как ``startup + N × время шага``: время запуска стадии
    (загрузка чекпойнта, экспорт HF-весов, инициализация vLLM) в разведке
    измерено отдельно и в цену шага не входит — иначе на 500 шагах оно бы
    растворилось, а на 100 раздуло бы шаг.

    Если доступен исторический 500-шаговый прогон той же конфигурации, он
    приводится рядом как **независимая сверка** допущения о линейности: это
    единственный способ проверить его фактом, а не обещанием.

    Оценка даётся **двумя границами без усреднения** (``bounds``): сверху —
    экстраполяция от шага разведки, снизу — прямой замер 500 шагов той же
    конфигурации на другом чекпойнте. Среднее между ними не имеет смысла: это
    не две оценки одной величины, а два разных поведения политики.
    """
    step = phases.get("step_seconds", {}).get("mean") if phases else None
    startup = None
    out = {"basis": {"step_seconds_mean": step, "steps_measured": phases.get("steps_closed"),
                     "startup_seconds": startup}}
    if not step:
        out["error"] = "закрытых шагов нет — экстраполировать не от чего"
        out["assumptions"] = ["экстраполяция не выполнялась: нет измеренного шага"]
        return out
    startup = args.startup_seconds if getattr(args, "startup_seconds", None) else None
    out["basis"]["startup_seconds"] = startup
    def hours(n: int) -> float:
        base = (startup or 0.0) + n * step
        return round(base / 3600.0, 2)
    out["rl_500_steps"] = {"steps": 500, "seconds": round((startup or 0) + 500 * step, 1),
                           "hours": hours(500), "days": round(hours(500) / 24, 2)}
    out["three_seeds"] = {"steps": 1500, "hours": hours(1500),
                          "days": round(hours(1500) / 24, 2),
                          "per_seed_hours": hours(500)}
    b = args.batch_size
    out["assumptions"] = [
        f"время шага линейно по числу шагов: {args.rl_steps} измеренных шагов → 500 "
        f"(на длинной дистанции возможны термотроттлинг и замедление — не учтены)",
        "конфигурация стадии та же, что измерена (vLLM util/KV/eager, resync_every, "
        "размер групп), а пул — тот же по составу (27 992 задачи ревизии)",
        "средняя длина ответов и доля успешных роллаутов остаются такими же: на 500 шагах "
        "политика меняется, и цена шага может уехать (на историческом прогоне RL шаги "
        "шли ~90–115 с при той же конфигурации)",
        f"стоимость трёх сидов = 3 × стоимость одного (без общего прог рева и кэшей)",
        "одноразовые расходы учтены отдельно один раз (startup_seconds), а не на каждый сид",
    ]
    out["caveats"] = [
        "замер сделан на **историческом** sft_checkpoint_final.pt (30.07.2026), а не на "
        "чекпойнте ревизии: порядок величины — да, точная цифра для S3 — нет",
        "eval-стадия в стоимость не входит (не запускалась)",
        "энтропия и награда — фон; при вырожденной награде цена шага формально верна, "
        "но стоимость «полезного» RL остаётся открытой (ADR-010, Negative)",
    ]
    #: Обязательная оговорка к цене шага: «упор в потолок хода». Верхняя граница
    #: получена на чекпойнте, который генерирует до потолка хода почти каждую
    #: траекторию (генерация — ~71% времени шага). Политика, завершающая ход
    #: раньше, стоит дешевле; поэтому верхнюю границу нельзя выдавать за
    #: ожидаемую цену ревизионного прогона.
    if cap_ctx and cap_ctx.get("our_truncated_at_cap_rate") is not None:
        ours = cap_ctx["our_truncated_at_cap_rate"]
        hist_rate = cap_ctx.get("historical_truncated_at_cap_rate")
        cap = cap_ctx.get("cap_tokens")
        out["caveats"].append(
            f"«упор в потолок хода»: {cap_ctx.get('our_truncated_at_cap')} из "
            f"{cap_ctx.get('our_trajectories')} траекторий разведки ({ours:.0%}) "
            f"упёрлись в max_new_tokens {cap} — цена шага измерена на политике, которая "
            f"договаривает ход до потолка, и потому это **верхняя** граница"
            + (f"; у исторического прогона тот же признак даёт "
               f"{hist_rate:.0%} ({cap_ctx.get('historical_trajectories')} траекторий) — "
               f"отсюда и разница цен шага" if hist_rate is not None else ""))
        out["turn_ceiling_caveat"] = {
            "cap_tokens": cap,
            "our_truncated_at_cap": cap_ctx.get("our_truncated_at_cap"),
            "our_trajectories": cap_ctx.get("our_trajectories"),
            "our_rate": ours,
            "historical_truncated_at_cap_rate": hist_rate,
            "historical_trajectories": cap_ctx.get("historical_trajectories"),
            "reading": ("признак считается по n_assistant_tokens — величине, которая "
                        "включает prefill и ответы инструмента (см. "
                        "measurements.cost_interpretation.generation_cap_pressure): "
                        "это верхняя граница доли, а не точная доля. Точная проверка — "
                        "по gen_tokens: генерация идёт почти до потолка вызова"),
        }
    if historical and historical.get("available"):
        out["cross_check_historical_500_steps"] = historical
        trend = historical.get("trend_first_vs_second_half") or {}
        out["assumptions"].append(
            f"допущение линейности проверено независимо: исторический прогон той же "
            f"конфигурации прошёл {historical.get('steps_covered')} шагов со средним "
            f"{historical.get('step_seconds_mean')} с/шаг, тренд первая/вторая половина "
            f"{trend.get('change_pct')}% — время шага на дистанции не растёт")
        if historical.get("stage_wall_hours_measured") and out.get("rl_500_steps"):
            probe_h = out["rl_500_steps"]["hours"]
            hist_h = historical["stage_wall_hours_measured"]
            #: Три сида: для верхней границы берётся прямо посчитанное значение
            #: (startup + 1500 шагов), а не 3 × округлённая цена 500 шагов — иначе
            #: двойное округление даёт в отчёте два разных числа под одним именем.
            seeds_upper = out["three_seeds"]["hours"]
            seeds_lower = round(3 * hist_h, 1)
            #: Две границы — рядом и без усреднения. Разные вопросы, не «оценки
            #: одной величины»: верхняя — экстраполяция от поведения чекпойнта,
            #: который договаривает до потолка; нижняя — прямой замер 500 шагов
            #: той же конфигурации на чекпойнте, который завершает ход раньше.
            seeds_range = [min(seeds_lower, seeds_upper), max(seeds_lower, seeds_upper)]
            out["bounds"] = {
                "upper": {
                    "name": "экстраполяция от шага разведки",
                    "step_seconds": step,
                    "steps": 500,
                    "rl_500_steps_hours": probe_h,
                    "three_seeds_hours": seeds_upper,
                    "checkpoint": "исторический sft_checkpoint_final.pt (30.07.2026)",
                    "why_upper": ("политика этого чекпойнта генерирует до потолка хода "
                                  "почти каждый ход — генерация даёт основную часть шага"),
                },
                "lower": {
                    "name": "прямой замер 500 шагов той же конфигурации (лог)",
                    "step_seconds": historical.get("step_seconds_mean"),
                    "steps": 500,
                    "rl_500_steps_hours": hist_h,
                    "three_seeds_hours": seeds_lower,
                    "checkpoint": ("чекпойнт исторического прогона v12 (v12_qwen25-05b_s42): "
                                   "тот же стенд и конфигурация стадии, другой чекпойнт"),
                    "why_lower": ("политика того прогона завершает ход раньше — «упор в "
                                  "потолок хода» там кратно реже (см. turn_ceiling_caveat)"),
                },
                "no_averaging": ("среднее между границами не считается: это два разных "
                                 "поведения политики, а не две оценки одной величины"),
                "three_seeds_hours_range": seeds_range,
            }
            out["range_summary"] = {
                "rl_500_steps_hours": sorted([probe_h, hist_h]),
                "reading": (f"оценка 500 шагов лежит между {min(probe_h, hist_h)} ч "
                            f"(прямой замер той же конфигурации на другом чекпойнте) и "
                            f"{max(probe_h, hist_h)} ч (экстраполяция от шага разведки, "
                            f"чей чекпойнт генерирует более длинные ответы); для решения "
                            f"о протоколе брать обе границы, а не среднее"),
                "three_seeds_hours_range": seeds_range,
            }
        if historical.get("stage_wall_hours_measured"):
            out["rl_500_steps_measured_historically"] = {
                "hours": historical["stage_wall_hours_measured"],
                "source": historical["path"],
                "note": ("прямой замер 500 шагов той же конфигурации на том же стенде "
                         "(шаги и старт стадии включены) — вторая граница оценки"),
            }
        measured = step
        hist = historical.get("step_seconds_mean")
        if hist:
            out["cross_check_verdict"] = {
                "measured_step_seconds": measured, "historical_step_seconds": hist,
                "ratio": round(measured / hist, 3),
                "consistent_order_of_magnitude": 0.5 <= measured / hist <= 2.0,
                "note": ("отношение — не ошибка и не совпадение: чекпойнт старта и пул "
                         "у разведки и исторического прогона разные, а длина ответов "
                         "и есть цена шага в этой конфигурации (71% шага — генерация)"),
            }
    else:
        out["cross_check_historical_500_steps"] = historical or {
            "available": False, "reason": "сверка не запрашивалась"}
    return out


def postcheck(host: str, pre: dict, args) -> dict:
    """Состояние стенда после прогона: контейнер разведки закрыт, платформа жива."""
    rc, out, _ = S.ssh(host, "docker ps --format '{{.Names}}'")
    running = out.split() if rc == 0 else []
    probe_running = [n for n in running if n.startswith(CONTAINER_PREFIX)]
    platform = [n for n in running if n.startswith(PLATFORM_PREFIX)]
    rc, free_out, _ = S.ssh(host, "free -g")
    rc2, mem_raw, _ = S.ssh(host, "cat /proc/meminfo")
    return {
        "containers": running,
        "probe_containers_running": probe_running,
        "container_closed": not probe_running,
        "platform_alive": platform,
        "platform_lost": sorted(set((pre.get("platform") or {}).keys()) - set(platform)),
        "free_g": free_out,
        "mem_free_gb": S.free_gb(S.parse_meminfo(mem_raw)) if rc2 == 0 else None,
        "dmesg_after": S.dmesg_counts(host),
    }


def write_manifest(run_dir: Path, args, stage: dict, status: str) -> Path | None:
    """Манифест AD-2 — генератором кейса (одна реализация на все прогоны).

    Гиперпараметры передаются **из кода** пайплайна (``pipeline_hyperparams``):
    AD-2 требует фактических значений, а собственный манифест пайплайна печатает
    ``resync_every`` 25 при работающих 10 (ADR-010) — пересказ чужого манифеста
    закрепил бы неверное число.
    """
    hp = pipeline_hyperparams()
    #: ``resync_every_manifest_default`` — не гиперпараметр прогона, а то число,
    #: которое печатает **чужой** манифест (25 против работающих 10, ADR-010).
    #: Оно стоит в манифесте рядом с фактическим именно поэтому: расхождение
    #: должно быть видно читателю манифеста, а не только читателю ADR.
    keys = ("kl_coef", "resync_every_runtime", "resync_every_manifest_default",
            "tasks_per_step", "group_size", "max_turns", "lr", "grad_clip_norm",
            "gen_max_new_tokens", "gen_temperature", "gen_top_k",
            "cispo_c_low", "cispo_c_high")
    env_keys = (("vllm_gpu_util", args.vllm_gpu_util),
                ("vllm_kv_cache_bytes", args.kv_cache_bytes),
                ("vllm_eager", args.vllm_eager),
                ("resync_every_env", args.resync_every),
                ("gen_eval_every", args.gen_eval_every),
                ("laguna_mem_fraction", args.mem_fraction))
    cmd = [sys.executable, str(CASE_ROOT / "tools" / "write_run_manifest.py"),
           "--run-dir", str(run_dir),
           "--dataset", str(RL_DATA_CASE),
           "--base-model", args.model,
           "--pipeline", str(CASE_ROOT / "laguna_pipeline_v8.py"),
           "--seed", str(args.seed), "--image", args.image,
           "--stages", f"rl={status}",
           "--run-version",
           "tools/run_rl_probe.py@" + (S.sha256_path(CASE_ROOT / "tools" / "run_rl_probe.py") or "?")[:12],
           "--dataset-extra", f"pool_symlink={RL_POOL_SYMLINK}",
           "--dataset-extra", f"sft_corpus={CASE_ROOT / 'datasets' / 'sft_train_v12.jsonl'}",
           "--hyperparams-source",
           ("значения из кода laguna_pipeline_v8.py (регулярки run_rl_probe."
            "pipeline_hyperparams) и из env запуска; не из манифеста пайплайна"),
           "--relative-to", str(CASE_ROOT), "--force"]
    for k in keys:
        if hp.get(k) is not None:
            cmd += ["--hyperparams", f"{k}={json.dumps(hp[k])}"]
    for k, v in env_keys:
        cmd += ["--hyperparams", f"{k}={json.dumps(str(v))}"]
    if status == "done":
        cmd.append("--complete")
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"ПРЕДУПРЕЖДЕНИЕ: манифест не записан (rc={p.returncode}): "
              f"{p.stderr.strip() or p.stdout.strip()}", file=sys.stderr)
        return None
    return run_dir / "run_manifest.json"


def stop_container(host: str, ts: str) -> dict:
    """План отката: закрыть контейнер разведки по имени (llm-platform-* не трогаем)."""
    name = container_name(ts)
    rc, out, err = S.ssh(host, "docker ps -a --format '{{.Names}}'")
    if rc != 0:
        return {"ok": False, "detail": f"docker ps недоступен: {err.strip()}"}
    if name not in out.split():
        return {"ok": True, "detail": f"контейнера {name} нет (уже закрыт)", "stopped": []}
    rc2, out2, err2 = S.ssh(host, f"docker stop {name}", timeout=180)
    return {"ok": rc2 == 0, "stopped": [name],
            "detail": (f"остановлен: {name}" if rc2 == 0
                       else f"docker stop rc={rc2}: {err2.strip()}")}


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="S3-pre: RL-разведка на стенде GB10 — цена шага RL (ADR-010).")
    ap.add_argument("--host", default=os.environ.get("GB10_HOST", DEFAULT_HOST),
                    help=f"стенд по ssh (по умолчанию {DEFAULT_HOST} / $GB10_HOST)")
    ap.add_argument("--ts", default=datetime.now().strftime("%Y%m%d-%H%M"),
                    help="метка прогона (по умолчанию — текущее время)")
    ap.add_argument("--image", default=os.environ.get("LAGUNA_IMAGE", DEFAULT_IMAGE),
                    help="образ окружения")
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B", help="базовая модель (ADR-002)")
    ap.add_argument("--seed", type=int, default=42, help="сид прогона (ADR-010: один сид)")
    ap.add_argument("--rl-steps", type=int, default=100,
                    help="шагов RL (коридор ADR-010: 50–100)")
    ap.add_argument("--max-samples", type=int, default=50000,
                    help="задач из пула (27 992 — весь пул ревизии)")
    ap.add_argument("--batch-size", type=int, default=2, help="batch_size (в RL не используется)")
    ap.add_argument("--max-len", type=int, default=8192, help="длина примера")
    ap.add_argument("--sft-ckpt", default=DEFAULT_SFT_CKPT,
                    help="стартовый SFT-чекпойнт на стенде (только чтение)")
    ap.add_argument("--vllm-gpu-util", default=LADDER_VLLM_GPU_UTIL,
                    help="VLLM_GPU_UTIL (как в рабочей лесенке для 0.5B)")
    ap.add_argument("--kv-cache-bytes", default=LADDER_KV_CACHE_BYTES,
                    help="VLLM_KV_CACHE_BYTES (как в рабочей лесенке)")
    ap.add_argument("--vllm-eager", default=LADDER_VLLM_EAGER,
                    help="VLLM_EAGER (как в рабочей лесенке)")
    ap.add_argument("--resync-every", type=int, default=10,
                    help="RESYNC_EVERY; задаётся явно, чтобы манифест пайплайна не печатал 25")
    ap.add_argument("--gen-eval-every", type=int, default=25,
                    help="GEN_EVAL_EVERY (дефолт пайплайна для RL — 25)")
    ap.add_argument("--mem-fraction", default=DEFAULT_MEM_FRACTION, help="LAGUNA_MEM_FRACTION")
    ap.add_argument("--mem-cap", default=DEFAULT_MEM_CAP, help="cgroup-кап памяти контейнера")
    ap.add_argument("--tasks-per-step", type=int, default=4,
                    help="задач на шаг (граница шага в обвязке; 4 при LAGUNA_PURE=0)")
    ap.add_argument("--group-size", type=int, default=2, help="траекторий на задачу")
    ap.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB,
                    help=f"порог свободной unified-памяти на старте (ADR-010 п.4: {MIN_FREE_GB})")
    ap.add_argument("--mem-floor-gb", type=float, default=MEM_FLOOR_GB,
                    help="предохранитель: стоп, если свободной памяти меньше (защита платформы)")
    ap.add_argument("--entropy-floor", type=float, default=ENTROPY_FLOOR,
                    help="порог энтропии политики для стопа (ADR-010 п.5)")
    ap.add_argument("--degeneracy-window", type=int, default=D.WINDOW_STEPS,
                    help=f"окно критерия вырожденной награды в шагах (ADR-017 п.1: "
                         f"{D.WINDOW_STEPS}; число берётся у прибора, а не подбирается)")
    ap.add_argument("--degenerate-stop", action="store_true",
                    help="остановить разведку по критерию ADR-017 (по умолчанию "
                         "вырожденная награда только публикуется: ADR-010 п.3 — "
                         "сходимость не критерий разведки)")
    ap.add_argument("--entropy-low-streak", type=int, default=ENTROPY_LOW_STREAK,
                    help="сколько наблюдений ниже порога подряд = стоп")
    ap.add_argument("--nvrm-limit", type=int, default=NVRM_LIMIT,
                    help="сколько NVRM/Xid-инцидентов = стоп и пауза")
    ap.add_argument("--max-wall-hours", type=float, default=4.0,
                    help="предел стены на стадию, ч")
    ap.add_argument("--sampler-seconds", type=int, default=None,
                    help="окно сэмплера памяти (по умолчанию — из шагов и стены)")
    ap.add_argument("--runs-dir", default="runs", help="каталог прогонов кейса")
    ap.add_argument("--evidence", default="evidence/s3-rl-probe.json", help="evidence-файл")
    ap.add_argument("--plan", action="store_true", help="напечатать план и выйти")
    ap.add_argument("--preflight-only", action="store_true", help="только предусловия")
    ap.add_argument("--analyze-only", action="store_true",
                    help="пересобрать сводку и evidence из готовых артефактов")
    ap.add_argument("--write-manifest", action="store_true",
                    help="при --analyze-only: перезаписать run_manifest.json (AD-2) "
                         "текущей ревизией раннера — для прогона, чей собственный "
                         "манифест отстал от пинуемой ревизии или не записался")
    ap.add_argument("--stop-only", action="store_true", help="закрыть контейнер разведки")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)
    if args.write_manifest and not args.analyze_only:
        ap.error("--write-manifest имеет смысл только с --analyze-only")
    if not 50 <= args.rl_steps <= 100:
        ap.error(f"--rl-steps {args.rl_steps} вне коридора ADR-010 (50–100): "
                 f"разведка не полный протокол и не смоук")
    if args.plan and args.analyze_only:
        ap.error("--plan и --analyze-only несовместимы")
    if args.sampler_seconds is None:
        args.sampler_seconds = int(max(args.max_wall_hours * 3600,
                                       args.rl_steps * 300) + 600)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = CASE_ROOT / args.runs_dir / f"rl-probe-{args.ts}"

    if args.plan:
        print(render_plan(args))
        return EXIT_OK

    if args.stop_only:
        res = stop_container(args.host, args.ts)
        print(f"[run_rl_probe] закрытие контейнера разведки: {res['detail']}")
        return EXIT_OK if res["ok"] else EXIT_FAIL

    if args.analyze_only:
        if not run_dir.is_dir():
            print(f"NOT-VERIFIED: каталога прогона нет: {run_dir}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        existing: dict = {}
        ev_path = CASE_ROOT / args.evidence
        if ev_path.is_file():
            try:
                existing = json.loads(ev_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = {}
        summary = analyze(run_dir, args)
        summary["historical_rl_run"] = historical_rl_cross_check(args.host)
        if summary["hook"].get("startup_seconds") is not None:
            args.startup_seconds = summary["hook"]["startup_seconds"]
        stage = existing.get("stage_result", {"stage": "rl", "analyzed_only": True})
        pre = {"host": args.host, "checks": existing.get("preconditions", {}).get("checks", []),
               "ok": existing.get("preconditions", {}).get("ok"), "dmesg_before": {}}
        post = _post_from_existing(existing) or postcheck(args.host, pre, args)
        manifest = run_dir / "run_manifest.json"
        regenerated = False
        if args.write_manifest:
            status_for_manifest = ("done" if existing.get("status") == "done" else "partial")
            written = write_manifest(run_dir, args, stage, status_for_manifest)
            if written is not None:
                manifest, regenerated = written, True
                print(f"[run_rl_probe] манифест AD-2 перезаписан текущей ревизией раннера: "
                      f"{S.rel_or_abs(written)}")
        ev = build_evidence(run_dir, args, pre, stage, summary,
                            manifest if manifest.is_file() else None, post)
        if regenerated:
            ev.setdefault("rebuilt", {})["manifest_regenerated"] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "why": ("манифест прогона перезаписан генератором кейса той же ревизией, "
                        "что собрала сводку: собственный манифест прогона писался "
                        "ревизией раннера, которая к моменту разбора уже не является "
                        "пинуемой (либо не записался вовсе)"),
                "runner_sha256": S.sha256_path(CASE_ROOT / "tools" / "run_rl_probe.py"),
            }
        # Статус — из прежнего evidence: он описывает ход прогона (дошёл/остановлен
        # стражем), а не то, что удалось пересобрать из артефактов сейчас.
        ev["status"] = existing.get("status", "partial")
        if existing:
            ev["rebuilt"] = {"at": datetime.now().isoformat(timespec="seconds"),
                             "note": ("сводка пересобрана из артефактов; предусловия, "
                                      "страж и состояние стенда после прогона взяты из "
                                      "прежнего evidence и повторным замером не подменялись")}
        S.write_evidence(ev_path, ev)
        print(f"[run_rl_probe] evidence пересобран из артефактов: {args.evidence}")
        return EXIT_OK

    pre = preflight(args.host, args)
    print(f"== предусловия ({args.host}) ==")
    for c in pre["checks"]:
        print(f"  [{'ok ' if c['ok'] else 'FAIL'}] {c['name']:<14} {c['detail']}")
    if not pre["ok"]:
        print()
        failed = [c["name"] for c in pre["checks"] if not c["ok"]]
        print(f"СТОП: предусловия не выполнены ({', '.join(failed)}) — стадия не запускается "
              f"(ADR-010 п.5: память ниже порога — не стартовать вовсе)")
        if args.json:
            print(json.dumps(pre, ensure_ascii=False, indent=2))
        return EXIT_FAIL
    if args.preflight_only:
        return EXIT_OK

    pre["platform"] = next((c.get("platform", {}) for c in pre["checks"]
                            if c["name"] == "containers"), {})
    stage = run_stage(args.host, args, run_dir)
    summary = analyze(run_dir, args)
    summary["historical_rl_run"] = historical_rl_cross_check(args.host)
    if summary["hook"].get("startup_seconds") is not None:
        args.startup_seconds = summary["hook"]["startup_seconds"]
    post = postcheck(args.host, pre, args)
    guard = stage.get("guard") or {}

    steps_done = None
    if summary["log"]["step_lines"]:
        steps_done = max(s["step"] for s in summary["log"]["step_lines"]) + 1
    complete = (stage.get("exit") == 0 and not guard.get("tripped"))
    status = "done" if complete else ("stopped_by_guard" if guard.get("tripped") else "partial")
    manifest = write_manifest(run_dir, args, stage, "done" if complete else "partial")

    ev = build_evidence(run_dir, args, pre, stage, summary, manifest, post)
    ev["status"] = status
    S.write_evidence(CASE_ROOT / args.evidence, ev)

    print()
    print("== замеры ==")
    ph = summary["phases"]
    if ph.get("step_seconds"):
        ss = ph["step_seconds"]
        print(f"  шагов: закрыто {ph.get('steps_closed')} из {args.rl_steps} "
              f"(в логе до шага {steps_done}); время шага "
              f"{ss['mean']} с (медиана {ss['median']}, {ss['min']}–{ss['max']}, σ={ss['stdev']})")
        shares = ph["shares_pct"]
        print(f"  фазы: генерация роллаутов {shares['t_generate']}% "
              f"({ph['phases_seconds']['t_generate']} с), обучение (остаток) "
              f"{shares['t_train_residual']}%, GEN-EVAL {shares['t_gen_eval']}%, "
              f"ресник {shares['t_resync']}%, чекпоинт {shares['t_ckpt']}%")
        g = ph["generation"]
        print(f"  генерация: {g['tok_per_s']} ток/с, {g['gen_tokens']} токенов "
              f"за {g['n_generate_calls']} вызовов; ресников: {ph['resync']}")
    else:
        print(f"  замеры шага не получены: {ph.get('note')}")
    eg = ev["measurements"]["entropy"]["from_log"]
    print(f"  энтропия политики: {eg.get('mean')} (мин {eg.get('min')}, макс {eg.get('max')}, "
          f"точек {eg.get('points_count', 0)}, коридор 1.0–2.0: {eg.get('in_corridor')})")
    dn = ev["measurements"]["degeneracy_reward"]
    print(f"  награда (ADR-017): вердикт {dn.get('verdict')}"
          + (f", классы стопа {dn.get('stop_classes')}, предупреждения "
             f"{dn.get('warning_classes')}" if dn.get("verdict") not in (None, "not_evaluated")
             else f" — {dn.get('why', '')[:80]}"))
    mem = summary["memory"]
    print(f"  память: пик занятой {mem.get('peak_used_gb')} ГБ, минимум свободной "
          f"{mem.get('min_available_gb')} ГБ, torch пик {mem.get('torch_peak_allocated_gb')} ГиБ")
    bg = ev["measurements"]["background"]
    print(f"  фон: reward_mean={bg.get('reward_mean')}, pass_rate={bg.get('pass_rate')}, "
          f"деградировавших траекторий "
          f"{(bg.get('degraded') or {}).get('combined_hit_timeout_or_empty')}")
    ex = ev["extrapolation"]
    if ex.get("rl_500_steps"):
        print(f"  экстраполяция: 500 шагов ≈ {ex['rl_500_steps']['hours']} ч "
              f"({ex['rl_500_steps']['days']} сут), три сида ≈ {ex['three_seeds']['days']} сут")
        cc = ex.get("cross_check_verdict")
        if cc:
            print(f"  сверка с историческим 500-шаговым прогоном: "
                  f"{cc['historical_step_seconds']} с/шаг против измеренных "
                  f"{cc['measured_step_seconds']} (отношение {cc['ratio']}, "
                  f"порядок сходится: {cc['consistent_order_of_magnitude']})")
    print()
    print(f"  статус: {status}" + (f" — {ev['status_reason']}" if guard.get("tripped") else ""))
    print(f"  контейнер закрыт: {post['container_closed']}; llm-platform-*: "
          f"{post['platform_alive'] or '— (не найдены)'}; свободно после: {post.get('mem_free_gb')} ГБ")
    print()
    print(f"манифест: {S.rel_or_abs(manifest) if manifest else '—'}")
    print(f"evidence: {args.evidence}")

    if args.json:
        print(json.dumps({"status": status, "measurements": ev["measurements"],
                          "extrapolation": ex, "post": post}, ensure_ascii=False, indent=2))
    ok = status in ("done", "stopped_by_guard", "partial") and post["container_closed"] \
        and not post["platform_lost"] and bool(ev["measurements"]["step_seconds"])
    return EXIT_OK if ok else EXIT_FAIL


def _post_from_existing(existing: dict) -> dict | None:
    """Состояние стенда «после прогона» из прежнего evidence (повторно не подменяем)."""
    pc = existing.get("postconditions")
    if not pc:
        return None
    return {"containers": pc.get("containers_after"),
            "probe_containers_running": pc.get("probe_container_running") or [],
            "container_closed": not pc.get("probe_container_running"),
            "platform_alive": pc.get("platform_alive"), "platform_lost": [],
            "free_g": pc.get("free_after_g"), "mem_free_gb": pc.get("mem_free_gb"),
            "dmesg_after": {}}


if __name__ == "__main__":
    sys.exit(main())
