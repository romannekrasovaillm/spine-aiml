#!/usr/bin/env python3
"""S3ay: ранний гейт полной SFT-стадии — проба точки 500, сравнение, остановка.

**Зачем ранний гейт.** Полная стадия идёт ≈40 часов. Срыв длины виден уже на 500-м
шаге (S3ap: 31/104 = 29.8 % усечений против 4.8 % у CPT-базы), то есть за ~35 минут
пробы можно узнать то, что иначе выяснится на вторые сутки. Критерий объявлен
**до** запуска (`early_gate.json` в каталоге прогона, пишет `launch_full_stage.py`):
критерий, подобранный после чтения чисел, не критерий.

**Почему сторож живёт на стороне аналитика, а не на стенде.** Остановка 40-часового
прогона — вмешательство, и оно обязано идти **штатным** путём (`tools/stop_stage_run.py`,
ADR-016: маркер `var/STOPPED` → `docker stop -t 120` → статус `stopped`, а не
`failed`). Этот инструмент живёт там, где до стенда есть ssh, и зовёт штатный
инструмент как есть. Каталог прогона виден по NFS (`/home/user/gb10-shared`), так
что ожидание точки и разбор отчётов не требуют ни одной команды на стенде.

**Что он делает по шагам** (каждый переход пишется в `early_gate_state.json` — по
нему видно, что сторож жив и на чём стоит):

1. **Ждёт точку.** `logs/loss_trace.jsonl` — последний шаг ≥ 500, и на месте
   `checkpoints/sft_probe_500.pt`. Ожидание по ФАКТУ шага, а не по наличию файла:
   файл появляется атомарной заменой раньше, чем обновляется траектория, и проба,
   стартовавшая по файлу, могла бы прочитать недописанное состояние.
2. **Ждёт место на хосте.** Проба идёт ПРИ ЖИВОЙ стадии, поэтому обязана влезть,
   не отняв память у обучения. Порог объявлен (`--min-host-free-gb`), источник —
   `/proc/meminfo` (`MemAvailable`), проба дополнительно сама отказывает при
   нехватке памяти устройства (`--min-free-gb`).
3. **Снимает пробу** — тем же прибором, набором, бюджетом и на той же площадке, что
   и рука сравнения. Другой прибор или другая площадка сделали бы разность чисел
   разностью условий (пробы не инвариантны к устройству — измерено 57/104).
4. **Сравнивает** с первой доступной опорой из объявленного списка.
5. **Вердикт.** Провал критерия — остановка стадии штатным инструментом. Провал
   САМОГО замера — `not_verified`, стадия НЕ останавливается: отсутствие числа не
   есть плохое число.

**Почему память читается из `/proc/meminfo`, а не из `free`.** 21.09.2026 сторож
полной стадии `sft-v13-2e6-20260921-1748` висел в `wait_memory` с
`available_gb: None`, хотя триггер (шаг ≥ 500 и точка `sft_probe_500.pt`) сработал:
`free -m | awk '/Mem:/{print $7}'` на стенде не находил строку, потому что `free`
печатает по локали (`Память:`), `int('')` давал `None` — и предохранителя не было,
при том что выглядел он работающим. Тот же урок уже был пройден в
`start_full_stage.py` (и в страже сна, где локаль пинну́та `LC_ALL=C`); здесь он не
был применён. Теперь основной источник — `/proc/meminfo` (английский всегда,
`MemAvailable` — ровно та величина, которой пользуется страж AD-9), запасной —
`free` с принудительной локалью C. Недоступность ОБОИХ источников называется
вслух (`memory_unreadable` → `memory_guard_not_armed`) и завершает сторожа
NOT-VERIFIED: «память не читается» и «памяти нет» — разные вещи, и вторая из них
не повод ждать три часа.

**Почему прибор доставляется вместе с его модулями.** `probe_language_split.py`
импортирует `probe_control` и `ppl_probe` на уровне файла. Каталог, куда положили
один прибор (как это сделала цепочка пробы руки S3ax), даёт не замер, а
`ModuleNotFoundError` за две секунды — то есть не «not_verified», а отсутствие
вердикта при видимости работы. Наличие модулей проверяется ДО контейнера
(`probe_tool_incomplete`), а не после.

Коды возврата::

    0 — вердикт вынесен (продолжать или остановлено)
    2 — NOT-VERIFIED: замер не снят (нет точки, нет места/память не читается, проба
        упала, прибор поставлен без модулей) — стадия жива
    3 — остановка потребовалась, но не удалась (см. вердикт; стадия продолжает идти)

Запуск (отсоединённо, с анализаторской машины)::

    setsid nohup python3 tools/early_gate_watch.py \
        --run-dir /home/user/gb10-shared/sft-v13-2e6-20260921-1805 \
        >> /home/user/gb10-shared/sft-v13-2e6-20260921-1805/early_gate_watch.log 2>&1 &
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STAND = "gb10-fast"
CASE_ROOT = Path(__file__).resolve().parent.parent
IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"
HARDCACHE = "/home/user/.cache/huggingface"
SHARED = "/home/user/gb10-shared"
CTR_SHARED = "/workspace/shared"
#: Прибор, пиннутый хешем (тот же, что в решающих отчётах S3ap/S3aq/S3aw/S3ax).
PROBE_TOOL_SHA = "99dafa8d9551caaa57b770d4cd321da6f0c09b28deb1885500034c9d62eeb276"
#: Модули, которые прибор импортирует на уровне файла. Оба обязаны лежать рядом с
#: ним: без них проба падает за секунды, и гейт не выносит вердикт вовсе.
PROBE_MODULES = ("probe_control.py", "ppl_probe.py")
#: Чтение свободной памяти хоста. Основной источник — `/proc/meminfo`: он английский
#: на любой локали, а `MemAvailable` это ровно та величина, которой пользуется страж
#: AD-9 (`check_resource_owner.sh`) и стартер (`start_full_stage.py`). Запасной —
#: `free` с ПРИНУДИТЕЛЬНОЙ локалью C: без неё разбор зависел бы от машины.
MEMINFO_CMD = "awk '/^MemAvailable:/{print $2}' /proc/meminfo"
MEMINFO_FALLBACK_CMD = "LC_ALL=C free -m | LC_ALL=C awk '/^Mem:/{print $7}'"
#: Множитель кБ → ГиБ для каждого источника (оба печатают килобайты).
MEM_SOURCES = (("proc_meminfo", MEMINFO_CMD, 1048576.0),
               ("free_locale_c", MEMINFO_FALLBACK_CMD, 1024.0))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{now()}] {msg}", flush=True)


def ssh(cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def host_path(p) -> str:
    """Путь контейнера → хостовый: контейнер видит общий диск под /workspace/shared."""
    s = str(p)
    return SHARED + s[len(CTR_SHARED):] if s.startswith(CTR_SHARED) else s


def sha256_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def last_step(run_dir: Path) -> int | None:
    lt = run_dir / "logs" / "loss_trace.jsonl"
    if not lt.is_file():
        return None
    try:
        lines = lt.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return int(json.loads(line)["step"])
        except Exception:
            continue
    return None


# ── память хоста ────────────────────────────────────────────────────────────
def first_int(text: str) -> int | None:
    """Первое число из вывода команды. `None` — числа нет (пусто, ошибка, текст)."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.isdigit():
            return int(s)
    return None


def host_memory_gb(ssh_fn=None) -> tuple[float | None, dict]:
    """Свободная память хоста в ГиБ и запись о том, ЧЕМ она прочитана.

    Возвращает `(None, evidence)` — если не прочитал ни один объявленный источник.
    Это не «памяти нет»: отсутствие числа не имеет права выглядеть как число.
    """
    ssh_fn = ssh_fn or ssh
    ev: dict = {"sources": [], "threshold_cmd": MEMINFO_CMD}
    for name, cmd, per_gb in MEM_SOURCES:
        try:
            rc, out, err = ssh_fn(cmd, 60)
        except Exception as e:                       # ssh не поднялся/таймаут
            ev["sources"].append({"source": name, "cmd": cmd, "rc": None,
                                  "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        kb = first_int(out)
        row = {"source": name, "cmd": cmd, "rc": rc, "raw": (out or "")[:200],
               "stderr": (err or "")[:200] if rc != 0 else "", "kb": kb}
        if kb is not None:
            row["gb"] = round(kb / per_gb, 2)
            ev["sources"].append(row)
            ev.update({"available_gb": row["gb"], "source": name})
            return row["gb"], ev
        ev["sources"].append(row)
    ev["available_gb"] = None
    ev["source"] = None
    ev["why"] = ("свободная память хоста не прочитана ни одним из объявленных "
                 "источников (/proc/meminfo, free с локалью C) — предохранитель "
                 "НЕ вооружён")
    return None, ev


def mem_beat_kw(ev: dict) -> dict:
    """Запись о памяти без полей, которые переход называет сам.

    `available_gb`/`source` есть всегда (в том числе `None`), и подстановка их
    рядом с явным аргументом — `TypeError: got multiple values`. Первая редакция
    этой правки на этом и упала: тест поймал до стенда.
    """
    return {k: v for k, v in ev.items() if k not in ("available_gb", "source")}


def probe_tool_path(stage_dir: Path) -> tuple[Path | None, list[str], list[str]]:
    """Где прибор и есть ли рядом его модули.

    Возвращает `(путь, чего не хватает, где искали)`. Прибор доставляется двумя
    раскладками: `tools/probe_language_split.py` (как в пакете сравнения) и плоской
    в старой сборке. Берётся первая раскладка, где прибор И оба его модуля на месте:
    прибор без модулей — это не замер, а ModuleNotFoundError.
    """
    tried, incomplete = [], []
    for cand in (stage_dir / "tools" / "probe_language_split.py",
                 stage_dir / "probe_language_split.py"):
        tried.append(str(cand))
        if not cand.is_file():
            continue
        missing = [m for m in PROBE_MODULES if not (cand.parent / m).is_file()]
        if missing:
            incomplete.append(f"{cand}: нет {', '.join(missing)}")
            continue
        return cand, [], tried
    return None, incomplete or ["прибора нет ни в одной раскладке"], tried


def metric(report: dict, tag: str, path: list[str]):
    node = report.get("states", {}).get(tag)
    if node is None:
        return None
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def load_report(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ── арифметика вердикта (чистая функция: проверяется без стенда и без пробы) ──
def compare_metrics(gate: dict, tag: str, report: dict, ref: dict) -> dict:
    """Сравнение отчёта стадии с опорой по объявленным метрикам.

    Отделено от `Watch`, чтобы арифметику можно было прогнать на синтетике ДО
    запуска: критерий, который проверен только на живом 40-часовом прогоне,
    проверен слишком поздно.
    """
    rows, worst = [], None
    for m in gate["metrics"]:
        got = metric(report, tag, m["path"])
        want = metric(ref["report"], ref["states_key"], m["path"])
        row = {"key": m["key"], "name": m["name"], "role": m["role"],
               "stage": got, "reference": want}
        if isinstance(got, (int, float)) and isinstance(want, (int, float)):
            d = round(float(got) - float(want), 4)
            row.update({"delta": d, "mdd": gate["mdd"],
                        "worse_significantly": d >= gate["mdd"],
                        "warn": gate["warn_delta"] <= d < gate["mdd"]})
            if row["worse_significantly"] and (worst is None or d > worst["delta"]):
                worst = row
        rows.append(row)
    if worst is not None:
        return {"verdict": "fail", "why": f"{worst['name']}: {worst['stage']} против "
                f"{worst['reference']} у опоры «{ref['tag']}» (Δ = +{worst['delta']}, "
                f"допуск {gate['mdd']})", "metrics": rows, "reference_used": ref["tag"]}
    warned = [r for r in rows if r.get("warn")]
    return {"verdict": "continue",
            "why": ("усечения и незакрытые блоки не хуже опоры за объявленным допуском"
                    if not warned else
                    "ухудшение есть, но внутри допуска — стадия продолжается, "
                    "ухудшение названо"),
            "metrics": rows, "reference_used": ref["tag"],
            "warnings": [r["key"] for r in warned]}


def pick_reference(gate: dict, n_expected: int) -> dict | None:
    """Первая доступная опора объявленного списка: целиком (n = n_expected)."""
    for cand in gate["reference"]:
        r = load_report(Path(cand["path"]))
        #: Состояние внутри отчёта ищется по `states_key`, а не по метке опоры:
        #: метка — наше имя опоры, ключ — имя, которым её снимал прибор.
        key = cand.get("states_key", cand["tag"])
        if r and metric(r, key, ["aggregate", "n"]) == n_expected:
            return {"tag": cand["tag"], "states_key": key, "path": cand["path"],
                    "why": cand["why"], "report": r}
    return None


#: Поля, по которым объявление прогона сверяется с фактом стадии (C-030).
IDENTITY_FIELDS = (
    ("jsonl_path", "sft_jsonl", ("jsonl", "path")),
    ("jsonl_sha256", "sft_jsonl_sha256", ("jsonl", "sha256")),
    ("jsonl_samples", "sft_examples", ("jsonl", "samples")),
    ("tensor_path", "tok_cache", ("tensor", "path")),
    ("tensor_sha256", "tok_cache_sha256", ("tensor", "sha256")),
    ("tensor_samples", "sft_examples", ("tensor", "samples")),
)


def identity_rows(declared: dict, got: dict) -> tuple[list[dict], list[str]]:
    """Объявленное против прочитанного: строки сверки и список расхождений.

    Чистая функция по той же причине, что и арифметика вердикта: сценарии
    идентичности входа (честный / подмена набора / усечённый хеш / нет блока)
    обязаны прогоняться до запуска, а не выясняться на 40-м часу обучения.
    """
    rows, bad = [], []
    for name, decl_key, path in IDENTITY_FIELDS:
        want = declared.get(decl_key)
        have = got
        for k in path:
            have = have.get(k) if isinstance(have, dict) else None
        if want is None:
            continue
        #: Пути объявлены относительно общего диска, факт пишется абсолютным.
        same = (str(have) == str(want) or str(have).endswith(str(want))
                or str(want).endswith(str(have)))
        rows.append({"field": name, "declared": want, "actual": have, "match": same})
        if not same:
            bad.append(name)
    for kind in ("jsonl", "tensor"):
        node = got.get(kind) if isinstance(got.get(kind), dict) else {}
        if node.get("hash_scope") != "full":
            bad.append(f"{kind}_hash_scope")
            rows.append({"field": f"{kind}_hash_scope", "declared": "full",
                         "actual": node.get("hash_scope"), "match": False})
    return rows, bad


class Watch:
    def __init__(self, run_dir: Path, a) -> None:
        self.run_dir = run_dir
        self.a = a
        self.state_path = run_dir / "early_gate_state.json"
        self.gate = json.loads((run_dir / "early_gate.json").read_text(encoding="utf-8"))
        self.state: dict = {"tool": "tools/early_gate_watch.py", "run_dir": str(run_dir),
                            "started_at": now(), "phase": "init", "events": []}

    def beat(self, phase: str, **kw) -> None:
        self.state.update({"phase": phase, "at": now(), **kw})
        self.state["events"].append({"phase": phase, "at": now(), **kw})
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        log(f"{phase}: {kw if kw else ''}")

    # ── 0. доказательство входа (C-030) ─────────────────────────────────────
    def verify_input(self) -> dict:
        """Объявленный набор против ФАКТИЧЕСКИ прочитанного — до 40 часов обучения.

        Страж C-030 (ADR-051) сверяет две записи, сделанные независимо: объявление
        прогона (`full_sft_params.json`) и факт стадии (блок `sft_input` манифеста
        `checkpoints/run_manifest.json`, который пишет сам пайплайн до первого шага).
        Здесь тот же вопрос задаётся РАНЬШЕ и с правом остановки: если стадия
        прочитала не тот набор, 40 часов учили бы не то, и узнавать об этом из
        отчёта стража через двое суток поздно.

        Провал — остановка стадии штатным инструментом. Недостижимость манифеста
        провалом не считается: стадия может ещё не дойти до записи (её пишет
        пайплайн после загрузки набора), поэтому ожидание с дедлайном, а не отказ.
        """
        params_f = self.run_dir / "full_sft_params.json"
        declared = json.loads(params_f.read_text(encoding="utf-8")).get("data", {})
        man_f = self.run_dir / "checkpoints" / "run_manifest.json"
        deadline = time.time() + self.a.wait_input_hours * 3600
        while time.time() < deadline:
            st = last_step(self.run_dir)
            if man_f.is_file():
                try:
                    man = json.loads(man_f.read_text(encoding="utf-8"))
                except Exception:
                    man = {}
                if isinstance(man.get("sft_input"), dict):
                    break
            self.beat("wait_input_manifest", step=st, manifest_exists=man_f.is_file())
            time.sleep(self.a.poll_secs)
        else:
            self.beat("input_manifest_timeout", waited_hours=self.a.wait_input_hours)
            return {"verdict": "not_verified",
                    "why": f"манифест стадии не появился за {self.a.wait_input_hours} ч — "
                           "вход не доказан и не опровергнут"}

        got = man["sft_input"]
        j, t = got.get("jsonl", {}), got.get("tensor", {})
        rows, bad = identity_rows(declared, got)
        res = {"verdict": "ok" if not bad else "mismatch", "fields": rows,
               "mismatched": bad, "manifest": str(man_f),
               "why": ("объявленный набор совпал с фактически прочитанным"
                       if not bad else
                       "стадия читает НЕ тот набор, который объявлен: " + ", ".join(bad))}
        self.beat("input_identity", verdict=res["verdict"], mismatched=bad,
                  jsonl_sha256=j.get("sha256"), tensor_sha256=t.get("sha256"),
                  samples=j.get("samples"))
        return res

    # ── 1. точка ────────────────────────────────────────────────────────────
    def wait_checkpoint(self) -> Path | None:
        step = self.gate["when"]["step"]
        name = Path(self.gate["when"]["artifact"]).name
        deadline = time.time() + self.a.wait_ckpt_hours * 3600
        while time.time() < deadline:
            st = last_step(self.run_dir)
            ck = self.run_dir / "checkpoints" / name
            if st is not None and st >= step and ck.is_file():
                self.beat("checkpoint_ready", step=st, checkpoint=str(ck),
                          bytes=ck.stat().st_size)
                return ck
            self.beat("wait_checkpoint", step=st, checkpoint_exists=ck.is_file())
            time.sleep(self.a.poll_secs)
        self.beat("checkpoint_timeout", waited_hours=self.a.wait_ckpt_hours)
        return None

    # ── 2. место на хосте ───────────────────────────────────────────────────
    def wait_memory(self) -> str:
        """Ждёт свободной памяти хоста: `ok` / `short` / `unreadable`.

        Три исхода вместо прежних двух, и это не косметика. Раньше «память не
        читается» и «память ниже порога» были одним и тем же (`None`), и первое
        уходило в цикл до таймаута: предохранитель, которого нет, выглядел
        работающим. Теперь нечитаемость называется вслух и после
        `--mem-unreadable-polls` подряд завершает сторожа — ждать дальше значило бы
        выдать молчание за проверку.
        """
        deadline = time.time() + self.a.wait_mem_hours * 3600
        misses = 0
        while time.time() < deadline:
            gb, ev = host_memory_gb()
            if gb is None:
                misses += 1
                self.beat("memory_unreadable", misses=misses,
                          limit=self.a.mem_unreadable_polls, **mem_beat_kw(ev))
                if misses >= self.a.mem_unreadable_polls:
                    self.beat("memory_guard_not_armed", misses=misses, **mem_beat_kw(ev))
                    return "unreadable"
                time.sleep(self.a.poll_secs)
                continue
            misses = 0
            if gb >= self.a.min_host_free_gb:
                self.beat("memory_ok", available_gb=round(gb, 1), source=ev.get("source"),
                          threshold_gb=self.a.min_host_free_gb, **mem_beat_kw(ev))
                return "ok"
            self.beat("wait_memory", available_gb=round(gb, 1), source=ev.get("source"),
                      threshold_gb=self.a.min_host_free_gb, **mem_beat_kw(ev))
            time.sleep(self.a.poll_secs)
        self.beat("memory_timeout", waited_hours=self.a.wait_mem_hours)
        return "short"

    # ── 3. проба ────────────────────────────────────────────────────────────
    def probe(self, ckpt: Path) -> dict | None:
        #: Прибор и страж — из САМОГО каталога прогона: проба снимается тем кодом,
        #: чей хеш записан в ship.json прогона, а не тем, что лежит в рабочей копии
        #: аналитика (её могли править после старта — и тогда число мерил бы другой
        #: прибор, чем записано). Раскладка и модули прибора проверяются ДО
        #: контейнера: прибор без `probe_control`/`ppl_probe` падает за секунды.
        stage_dir = self.run_dir
        tool, missing, tried = probe_tool_path(stage_dir)
        if tool is None:
            self.beat("probe_tool_missing", searched=tried, missing=missing,
                      why="прибор доставлен без модулей, которые он импортирует на "
                          "уровне файла, — проба упала бы ModuleNotFoundError, и "
                          "вердикта не было бы вовсе")
            return None
        #: Тождество прибора: числа сравнимы только при том же приборе, которым снята
        #: опора. Хеш берётся из ОБЪЯВЛЕНИЯ гейта (`early_gate.json`), а не из
        #: константы: объявление писалось до прогона, константу можно поправить потом.
        want_sha = self.gate.get("probe", {}).get("tool_sha256", PROBE_TOOL_SHA)
        have_sha = sha256_file(tool)
        if have_sha != want_sha:
            self.beat("probe_tool_sha_mismatch", tool=str(tool), declared=want_sha,
                      actual=have_sha,
                      why="доставленный прибор не тот, которым снята опора, — числа "
                          "были бы разностью приборов, а не состояний")
            return None
        tag = self.a.tag
        out = self.run_dir / f"report_{tag}.json"
        ctr = f"laguna-{self.run_dir.name}-earlygate"
        #: Проба идёт ПОД ТЕМ ЖЕ стражем сна, что и стадия: 35 минут — тоже прогон
        #: на устройстве, и сон в них убил бы контекст CUDA (Xid 31). `--device-check
        #: warn` объявлен потому, что устройство намеренно ЗАНЯТО стадией: «устройство
        #: свободно» здесь не критерий, а его отсутствие — не повод не мерить.
        inner = (
            f"docker rm -f {shlex.quote(ctr)} >/dev/null 2>&1 || true; "
            f"docker run --rm --name {shlex.quote(ctr)} --gpus all --ipc=host "
            f"--memory=48g --memory-swap=48g "
            f"-v {SHARED}:{SHARED} -v {HARDCACHE}:{HARDCACHE} -w {stage_dir} {IMAGE} "
            f"python3 {tool} --prompts {self.gate['probe']['prompts']} "
            f"--max-new-tokens {self.gate['probe']['max_new_tokens']} "
            f"--stop-at-turn-end --batch-size {self.gate['probe']['batch_size']} --sha "
            f"--min-free-gb {self.a.min_device_free_gb} "
            f"--ckpt {tag}={ckpt} --out {out}")
        guarded = (f"bash {stage_dir}/guard/guard_cuda_run.sh "
                   f"--out {self.run_dir}/guard_early_gate.json --device-check warn "
                   f"--why {shlex.quote('S3ay: ранний гейт — проба точки ' + str(self.gate['when']['step']))} "
                   f"-- bash -c {shlex.quote(inner)}")
        self.beat("probe_start", container=ctr, report=str(out), tool=str(tool),
                  tool_sha256=have_sha[:12], guard_wrapped=True,
                  tool_sha_pinned=want_sha[:12])
        t0 = time.time()
        rc, got, err = ssh(guarded, timeout=self.a.probe_timeout_mins * 60)
        wall = round(time.time() - t0, 1)
        rep = load_report(out)
        n = metric(rep, tag, ["aggregate", "n"]) if rep else None
        ok = rc == 0 and n == self.gate["probe"]["n_probes"]
        self.beat("probe_done", rc=rc, wall_seconds=wall,
                  report_exists=out.is_file(), n_probes=n, ok=ok,
                  stderr_tail=err[-400:] if not ok else "")
        return rep if ok else None

    # ── 4–5. сравнение и вердикт ────────────────────────────────────────────
    def compare(self, report: dict) -> dict:
        ref = pick_reference(self.gate, self.gate["probe"]["n_probes"])
        if ref is None:
            return {"verdict": "not_verified",
                    "why": "ни одна объявленная опора не читается целиком "
                           f"(n = {self.gate['probe']['n_probes']}) — сравнивать не с чем",
                    "metrics": [],
                    "reference_missing": [c["tag"] for c in self.gate["reference"]]}
        return compare_metrics(self.gate, self.a.tag, report, ref)

    def stop(self, why: str) -> dict:
        """Остановка ШТАТНЫМ инструментом (ADR-016), а не своим docker stop."""
        tool = self.run_dir / "stop_stage_run.py"
        cmd = [sys.executable, str(tool), "--run", str(self.run_dir), "--do", "stop",
               "--reason", f"early-gate S3ay: {why}", "--timeout", "120"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        try:
            res = json.loads(p.stdout)
        except Exception:
            res = {"stdout_tail": p.stdout[-1500:], "stderr_tail": p.stderr[-1500:]}
        return {"rc": p.returncode, "tool": "stop_stage_run.py", "cmd": cmd, **res}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="каталог прогона на общем диске")
    ap.add_argument("--tag", default=None, help="метка состояния в отчёте прибора")
    ap.add_argument("--poll-secs", type=int, default=60)
    ap.add_argument("--wait-input-hours", type=float, default=2.0,
                    help="сколько ждать блок sft_input манифеста стадии (вход C-030)")
    ap.add_argument("--wait-ckpt-hours", type=float, default=12.0)
    ap.add_argument("--wait-mem-hours", type=float, default=3.0)
    ap.add_argument("--mem-unreadable-polls", type=int, default=3,
                    help="сколько РАЗ ПОДРЯД память хоста обязана не прочитаться, "
                         "чтобы сторож назвал отказ: нечитаемость не повод ждать три часа")
    ap.add_argument("--probe-timeout-mins", type=float, default=180.0)
    ap.add_argument("--min-host-free-gb", type=float, default=25.0,
                    help="порог свободной памяти ХОСТА перед пробой: стадия держит бюджет, "
                         "проба обязана влезть, не отняв его")
    ap.add_argument("--min-device-free-gb", type=float, default=6.0,
                    help="порог свободной памяти УСТРОЙСТВА (флаг прибора --min-free-gb, AD-5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="проверить объявление, опоры, прибор и ЧТЕНИЕ ПАМЯТИ, ничего "
                         "не снимая и не останавливая")
    ap.add_argument("--check-memory", action="store_true",
                    help="только чтение памяти хоста тем же кодом, что у сторожа: "
                         "предполётная проверка предохранителя на площадке")
    a = ap.parse_args()

    run_dir = Path(a.run_dir)
    if not (run_dir / "early_gate.json").is_file():
        print(f"нет объявления раннего гейта: {run_dir / 'early_gate.json'}", file=sys.stderr)
        return 2
    gate = json.loads((run_dir / "early_gate.json").read_text(encoding="utf-8"))
    a.tag = a.tag or f"stage_{Path(run_dir).name.replace('sft-', '')}_s{gate['when']['step']}"

    if a.check_memory:
        #: Состояние предполётной проверки — отдельным файлом: живой сторож начинает
        #: `early_gate_state.json` заново, и предполётное число не должно выглядеть
        #: его событием. Лог — общий (`early_gate_watch.log`): число из него и
        #: приводится как доказательство, что предохранитель читает память на площадке.
        w = Watch(run_dir, a)
        w.state_path = run_dir / "early_gate_memory_preflight_state.json"
        w.beat("memory_preflight_start", mode="check-memory", threshold_gb=a.min_host_free_gb)
        gb, ev = host_memory_gb()
        ok = gb is not None and gb >= a.min_host_free_gb
        w.beat("memory_preflight", available_gb=(round(gb, 1) if gb is not None else None),
               threshold_gb=a.min_host_free_gb, ok=ok, **mem_beat_kw(ev))
        out = run_dir / "early_gate_memory_preflight.json"
        out.write_text(json.dumps({"at": now(), "run_dir": str(run_dir),
                                   "threshold_gb": a.min_host_free_gb, "ok": ok, **ev},
                                  ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(json.dumps({"memory_preflight": {"available_gb": (round(gb, 1) if gb is not None
                                                               else None),
                                             "threshold_gb": a.min_host_free_gb,
                                             "ok": ok, "source": ev.get("source"),
                                             "file": str(out)}},
                       ensure_ascii=False))
        return 0 if ok else 2

    if a.dry_run:
        refs = []
        for c in gate["reference"]:
            r = load_report(Path(c["path"]))
            refs.append({"tag": c["tag"], "states_key": c.get("states_key", c["tag"]),
                         "path": c["path"], "readable": r is not None,
                         "n": (metric(r, c.get("states_key", c["tag"]),
                                      ["aggregate", "n"]) if r else None)})
        tool, missing, tried = probe_tool_path(run_dir)
        gb, mem_ev = host_memory_gb()
        print(json.dumps({"dry_run": True, "run_dir": str(run_dir), "tag": a.tag,
                          "step": gate["when"]["step"], "mdd": gate["mdd"],
                          "metrics": [m["key"] for m in gate["metrics"]],
                          "references": refs,
                          "probe_tool": {"path": (str(tool) if tool else None),
                                         "searched": tried, "incomplete": missing},
                          "host_memory": {"available_gb": gb, "source": mem_ev.get("source"),
                                          "threshold_gb": a.min_host_free_gb,
                                          "ok": gb is not None and gb >= a.min_host_free_gb,
                                          "sources": mem_ev.get("sources", [])},
                          "tool_sha_expected": PROBE_TOOL_SHA},
                         ensure_ascii=False, indent=2))
        return 0

    w = Watch(run_dir, a)
    w.beat("start", mode="live", tag=a.tag)

    # ── 0. Вход: стадия читает объявленный набор, или её останавливают ───────
    inp = w.verify_input()
    (run_dir / "input_identity_verdict.json").write_text(
        json.dumps({"at": now(), "run_dir": str(run_dir), "declared_before_run": True,
                    **inp}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if inp["verdict"] == "mismatch":
        stop = w.stop(f"вход стадии не совпал с объявленным ({', '.join(inp['mismatched'])}) "
                      "— 40 часов учили бы не тот набор")
        v = {"at": now(), "run_dir": str(run_dir), "tag": a.tag,
             "verdict": "fail", "stopped": stop.get("verdict") == "STOPPED",
             "why": inp["why"], "input_identity": inp, "stop": stop, "metrics": []}
        (run_dir / "early_gate_verdict.json").write_text(
            json.dumps(v, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        w.beat("verdict", verdict="fail", why=v["why"], stage="вход")
        log(json.dumps({"verdict": "fail", "stage": "вход", "why": v["why"],
                        "stopped": v["stopped"]}, ensure_ascii=False))
        return 0 if v["stopped"] else 3

    ckpt = w.wait_checkpoint()
    if ckpt is None:
        #: Точка не появилась — тоже НАЗЫВАЕТСЯ файлом вердикта, а не только кодом
        #: возврата: «сторож вышел» и «сторож ничего не сказал» не должны выглядеть
        #: одинаково для того, кто читает каталог прогона через сутки.
        verdict = {"at": now(), "run_dir": str(run_dir), "tag": a.tag,
                   "step": gate["when"]["step"], "verdict": "not_verified",
                   "why": f"точка шага {gate['when']['step']} не появилась за "
                          f"{a.wait_ckpt_hours} ч (последний шаг в loss_trace — "
                          f"{last_step(run_dir)}) — проба не снята; стадия не "
                          "останавливается",
                   "metrics": [], "reference_candidates":
                   [c["tag"] for c in gate["reference"]]}
        (run_dir / "early_gate_verdict.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        w.beat("verdict", verdict="not_verified", stage="точка", why=verdict["why"])
        log(json.dumps({"verdict": "not_verified", "stage": "точка",
                        "why": verdict["why"]}, ensure_ascii=False))
        return 2
    mem = w.wait_memory()
    if mem != "ok":
        #: Явный отказ вместо молчания: предохранитель, который не сработал, обязан
        #: быть назван — иначе «сторож отработал» и «сторож простоял» неразличимы.
        verdict = {"at": now(), "run_dir": str(run_dir), "tag": a.tag,
                   "step": gate["when"]["step"], "verdict": "not_verified",
                   "memory_guard": ("not_armed" if mem == "unreadable"
                                    else "armed_but_below_threshold"),
                   "why": ("память хоста не читается ни из /proc/meminfo, ни из free с "
                           "локалью C — предохранитель не вооружён, проба не снята; "
                           "стадия не останавливается"
                           if mem == "unreadable" else
                           f"свободной памяти хоста не дождались за {a.wait_mem_hours} ч "
                           f"(порог {a.min_host_free_gb} ГиБ) — проба не снята; "
                           "стадия не останавливается"),
                   "metrics": [], "reference_candidates":
                   [c["tag"] for c in gate["reference"]]}
        (run_dir / "early_gate_verdict.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        w.beat("verdict", verdict="not_verified", stage="память", why=verdict["why"],
               memory_guard=verdict["memory_guard"])
        log(json.dumps({"verdict": "not_verified", "stage": "память",
                        "memory_guard": verdict["memory_guard"], "why": verdict["why"]},
                       ensure_ascii=False))
        return 2
    rep = w.probe(ckpt)
    if rep is None:
        verdict = {"verdict": "not_verified",
                   "why": "проба не снята (см. probe_done в early_gate_state.json) — "
                          "стадия не останавливается: отсутствие числа не есть плохое число",
                   "metrics": []}
    else:
        verdict = w.compare(rep)
    verdict.update({"at": now(), "run_dir": str(run_dir), "tag": a.tag,
                    "step": gate["when"]["step"], "declared_before_run":
                    gate.get("declared_before_run", False),
                    "reference_candidates": [c["tag"] for c in gate["reference"]]})
    if verdict["verdict"] == "fail":
        verdict["stop"] = w.stop(verdict["why"])
        verdict["stopped"] = verdict["stop"].get("verdict") == "STOPPED"
    (run_dir / "early_gate_verdict.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    w.beat("verdict", verdict=verdict["verdict"], why=verdict["why"])
    log(json.dumps({"verdict": verdict["verdict"], "why": verdict["why"],
                    "reference_used": verdict.get("reference_used"),
                    "verdict_file": str(run_dir / "early_gate_verdict.json")},
                   ensure_ascii=False))
    if verdict["verdict"] == "fail" and not verdict.get("stopped"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
