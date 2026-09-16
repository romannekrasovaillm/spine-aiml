#!/usr/bin/env python3
"""S2 — смоук-прогон контура на стенде GB10: CPT 20–50 шагов, SFT 20–30 шагов.

Назначение дельты — **доказать, что контур сходится**, а не получить результат:
одна модель (Qwen2.5-0.5B), один сид, короткие стадии на реальном миксе. RL в
смоук не входит (стадия не запускается вовсе).

Что раннер считает предусловием (ADR-007), а не «попробуем»:

* свободная unified-память стенда ≥ ``--min-free-gb`` (по умолчанию 30 ГБ) —
  замер по ``MemAvailable`` из ``/proc/meminfo`` (у GB10 память общая с CPU, и
  ``nvidia-smi`` про занятое не говорит ничего);
* страж сериализации AD-5 запущен в режиме ``--strict``: недоступность стенда —
  красный вердикт, а не «пропущено» (ADR-007 п.5);
* контейнеры ``llm-platform-*`` (платформенный инференс, ~28 ГБ) — **не трогаем**:
  они не гейтятся, но замеряются до и после, и их исчезновение — ошибка раннера,
  а не успех смоука;
* стадии идут через ``nvrm-storm/safe_start.sh`` (drop-caches-absorbер) — как в
  рабочем раннере контура ``run_v12_ladder.sh``, иначе смоук проверял бы не тот
  путь запуска.

Что раннер измеряет (а не оценивает): ``tok/s`` (Σтокенов / Σвремени обучающих
шагов), время шага, ``loss`` по шагам, токенную ``entropy`` (методика —
``tools/smoke_probe.py``), пик unified-памяти хоста по сэмплам и пик памяти
процесса обучения по данным torch, инциденты OOM/NVRM.

Артефакты:

* ``runs/smoke-<ts>/run_manifest.json`` — пиннинг AD-2 (C-012), через
  ``tools/write_run_manifest.py`` (одна реализация манифеста на весь кейс);
* ``runs/smoke-<ts>/logs/<stage>.log``, ``probe_<stage>.jsonl``,
  ``mem_<stage>.jsonl``, ``pipeline_run_manifest.json`` — сырые замеры;
* ``evidence/s2-smoke.json`` — сводка замеров и **явный список непроверенного**.

Режимы без запуска: ``--plan`` печатает команды и ничего не делает,
``--preflight-only`` делает только проверки предусловий, ``--analyze-only``
пересобирает сводку и evidence из уже лежащих артефактов (после ручного прогона
или падения раннера), ``--stop-only`` закрывает контейнеры смоука по имени (план
отката: стадия завершается до чекпойнта, затем контейнер закрывается).

Коды возврата::

    0 — смоук прошёл (или запрошенный режим без запуска отработал)
    1 — предусловие нарушено / стадия упала / инцидент в замерах
    2 — NOT-VERIFIED: стенд недоступен или артефакт отсутствует

Запуск::

    python3 tools/run_smoke.py --plan                  # что и как будет запущено
    python3 tools/run_smoke.py --preflight-only        # только предусловия
    python3 tools/run_smoke.py                         # CPT + SFT (≈10 минут)
    python3 tools/run_smoke.py --analyze-only --ts 20260914-1200
    python3 tools/run_smoke.py --stop-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Хост стенда: `gb10` из рабочей сети не резолвится (факт 14.09.2026), рабочий
#: алиас — `gb10-fast`. Раннер намеренно не перебирает кандидатов молча: смоук,
#: уехавший на другой хост, — это другой смоук.
DEFAULT_HOST = "gb10-fast"
DEFAULT_IMAGE = "nvcr.io/nvidia/pytorch:26.07-py3-vllm"

#: Пути стенда (хост → контейнер). Родительский контур read-only; пишем только в
#: ~/experiments/<run_id>/ (рабочая область прогонов контура).
STAND_SHARED = "/home/user/gb10-shared"
STAND_EXPERIMENTS = "/home/user/experiments"
SAFE_START = f"{STAND_SHARED}/nvrm-storm/safe_start.sh"
CTR_SHARED = "/workspace/shared"
CTR_EXPERIMENTS = "/workspace/experiments"
PIPELINE_CTR = f"{CTR_SHARED}/laguna_pipeline_v8.py"

#: Датасеты контура (симлинки кейса ведут в тот же gb10-shared).
CPT_DATA = f"{CTR_SHARED}/datasets/cpt_corpus_v12r.txt"
SFT_DATA = f"{CTR_SHARED}/datasets/sft_train_v12.jsonl"
RL_DATA = f"{CTR_SHARED}/datasets/rl_tasks_oxalpha.jsonl"
EVAL_DATA = f"{CTR_SHARED}/datasets/eval_ood_clean.jsonl"
#: Преток-кэши: пайплайн ищет их сам по stem файла данных (и по тегу токенайзера).
CPT_TOK_CACHE = "datasets/tok/cpt_corpus_v12r_8192_qwen25.npy"
CPT_TOK_POS = "datasets/tok/cpt_corpus_v12r_8192_qwen25_pos.npy"
SFT_TOK_CACHE = "datasets/tok/sft_train_v12_8192_qwen25.npz"
PRETOKENIZER = f"{CTR_SHARED}/pretokenize_v9.py"

#: Платформенный инференс: не гейтится (ADR-007 п.4), но не должен исчезнуть.
PLATFORM_PREFIX = "llm-platform-"

MIN_FREE_GB = 30

#: Строки прогресса, которые печатаются в консоль (полный лог — в файле).
PROGRESS_RE = re.compile(
    r"STAGE:|^\S+ \[INFO\] (CPT|SFT|RL|EVAL) |CKPT:|PROBES|GEN-EVAL|resume|RESUME|"
    r"Traceback|Error|error|OOM|Killed|NOT-VERIFIED|SMOKE_")


# ─── транспорт ────────────────────────────────────────────────────────────────

def ssh(host: str, command: str, timeout: int | None = 60) -> tuple[int, str, str]:
    """Один ssh-вызов. ``BatchMode`` — без интерактивных подсказок пароля."""
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", f"таймаут ssh ({timeout} с)"
    except OSError as e:
        return 125, "", f"ssh недоступен: {e}"
    return p.returncode, p.stdout, p.stderr


def ssh_put(host: str, remote_path: str, content: bytes) -> tuple[int, str]:
    """Кладёт файл на стенд через stdin (без scp и без временных файлов)."""
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host,
             f"mkdir -p $(dirname {remote_path}) && cat > {remote_path}"],
            input=content, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 125, str(e)
    return p.returncode, p.stderr.decode("utf-8", "replace")


def ssh_get(host: str, remote_path: str, timeout: int = 120) -> tuple[int, bytes, str]:
    """Забирает файл со стенда через stdout."""
    try:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host, f"cat -- {remote_path}"],
            capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 125, b"", str(e)
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")


def sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


# ─── предусловия ──────────────────────────────────────────────────────────────

def parse_meminfo(text: str) -> dict:
    """``MemTotal``/``MemAvailable`` (кБ) из /proc/meminfo."""
    out = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        if key in ("MemTotal", "MemAvailable", "MemFree", "Cached"):
            m = re.search(r"(\d+)", parts[1])
            if m:
                out[key] = int(m.group(1))
    return out


def free_gb(mem: dict) -> float | None:
    avail = mem.get("MemAvailable")
    return None if avail is None else round(avail / (1024 * 1024), 2)


def check_free_memory(host: str, min_free_gb: float) -> dict:
    """Предусловие ADR-007: свободной unified-памяти ≥ порога, иначе — стоп."""
    rc, raw, err = ssh(host, "cat /proc/meminfo")
    if rc != 0:
        return {"name": "memory", "ok": False, "detail": f"стенд не ответил: {err.strip()}"}
    mem = parse_meminfo(raw)
    total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
    if avail is None:
        return {"name": "memory", "ok": False, "detail": "MemAvailable не прочитан"}
    gb = free_gb(mem)
    rc2, free_out, _ = ssh(host, "free -g")
    ok = gb >= min_free_gb
    return {"name": "memory", "ok": ok,
            "detail": (f"доступно {gb} ГБ из {round((total or 0) / 1048576, 1)} ГБ "
                       f"(порог {min_free_gb}); free -g: "
                       + " / ".join(free_out.splitlines()[1].split()[1:4]) if free_out else ""),
            "mem_available_gb": gb,
            "threshold_gb": min_free_gb,
            "free_g": free_out}


def containers(host: str) -> dict[str, str]:
    rc, out, _ = ssh(host, "docker ps --format '{{.Names}} {{.Image}} {{.Status}}'")
    if rc != 0:
        return {}
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if parts:
            result[parts[0]] = line
    return result


def check_platform_containers(host: str) -> dict:
    """llm-platform-* на месте — их раннер не трогает (ADR-007 п.4)."""
    now = containers(host)
    if not now:
        return {"name": "containers", "ok": False, "detail": "docker ps недоступен"}
    platform = {k: v for k, v in now.items() if k.startswith(PLATFORM_PREFIX)}
    smoke = sorted(k for k in now if k.startswith("laguna-smoke-"))
    if not platform:
        return {"name": "containers", "ok": True,
                "detail": (f"llm-platform-* не найдены (в гейт не входят, ADR-007 п.4); "
                           f"всего контейнеров: {len(now)}"),
                "platform": {}, "containers": now, "smoke_running": smoke}
    return {"name": "containers", "ok": True,
            "detail": (f"llm-platform-* живы: {', '.join(sorted(platform))}"
                       + (f"; остались контейнеры смоука: {smoke}" if smoke else "")),
            "platform": platform, "containers": now, "smoke_running": smoke}


def check_serialization_guard(host: str, strict: bool = True) -> dict:
    """Страж AD-5 в гейтовом профиле: exit 2 (недоступность) — красный."""
    script = CASE_ROOT / "tools" / "check_gb10_serialization.sh"
    cmd = ["bash", str(script), "--host", host] + (["--strict"] if strict else [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"name": "serialization", "ok": False, "detail": f"страж не запустился: {e}"}
    ok = p.returncode == 0
    return {"name": "serialization", "ok": ok, "strict": strict,
            "exit": p.returncode,
            "detail": (("AD-5 OK: одновременных нагрузок нет" if ok else
                        f"страж AD-5 красный (exit {p.returncode})")),
            "output": p.stdout.strip().splitlines()[-8:]}


def check_image(host: str, image: str) -> dict:
    rc, out, _ = ssh(host, f"docker images -q {image}")
    ok = rc == 0 and bool(out.strip())
    return {"name": "image", "ok": ok,
            "detail": f"{image}: {'есть' if ok else 'НЕ найден на стенде'}"}


def check_data(host: str, attn: str) -> dict:
    """Входы стадий: **читаются преток-кэши**, не исходные тексты.

    Тонкость контура, которую проверяем явно: ``CPTDataset``/``SFTDataset``
    открывают не файл данных, а кэш рядом с ним — ``<parent>/tok/<stem>_<len>_<tag>.npy``.
    Поэтому ``--cpt_data`` задаёт *имя* датасета (и путь для разрешения кэша), а
    сам ``datasets/cpt_corpus_v12r.txt`` на стенде может и не существовать: так и
    есть (файл лежит в корне ``gb10-shared``), и рабочий раннер контура передаёт
    именно несуществующий ``datasets/...`` — иначе кэш не найдётся. Проверять
    «файла данных нет → стоп» здесь значило бы краснеть на здоровом стенде.
    """
    caches = [CPT_TOK_CACHE, SFT_TOK_CACHE] + ([CPT_TOK_POS] if attn == "flex" else [])
    required = [f"{STAND_SHARED}/{c}" for c in caches] + \
               [f"{STAND_SHARED}/laguna_pipeline_v8.py",
                f"{STAND_SHARED}/datasets/sft_train_v12.jsonl",
                f"{STAND_SHARED}/nvrm-storm/safe_start.sh"]
    nominal = [f"{STAND_SHARED}/datasets/cpt_corpus_v12r.txt"]
    rc, out, _ = ssh(host, "for f in " + " ".join(required + nominal) +
                     '; do [ -e "$f" ] && echo "OK $f" || echo "MISSING $f"; done')
    lines = out.splitlines()
    missing = [ln.split(" ", 1)[1] for ln in lines if ln.startswith("MISSING")]
    missing_required = [m for m in missing if m not in nominal]
    notes = []
    if any(m in nominal for m in missing):
        notes.append("имя CPT-датасета (`datasets/cpt_corpus_v12r.txt`) на стенде "
                     "отсутствует — это ожидаемо: читается преток-кэш, а путь нужен "
                     "только для его разрешения и для lineage-хеша (как в run_v12_ladder.sh)")
    if missing_required:
        notes.append(f"кэш/вход отсутствует — пересборка средствами контура: "
                     f"python3 {PRETOKENIZER} (SFT) или build_cpt_v12r_mix.py (CPT), "
                     f"см. run_v12_ladder.sh")
    detail = ("кэши и входы на месте" if not missing_required
              else f"нет: {', '.join(missing_required)}")
    if notes:
        detail += "; " + "; ".join(notes)
    return {"name": "data", "ok": not missing_required, "detail": detail,
            "missing": missing_required, "notes": notes, "caches": caches}


def dmesg_counts(host: str) -> dict:
    """Счётчики NVRM/Xid и OOM в dmesg (дельта за прогон — в замерах).

    ``grep -c`` возвращает 1 при нуле совпадений — на этом «нет инцидентов»
    превращалось бы в «счётчик не прочитан» (``None``), а это разные вещи:
    первое — доказательство, второе — пробел. Поэтому выход команды
    нормализуется явным ``exit 0``.
    """
    rc, out, _ = ssh(host, "sudo -n dmesg 2>/dev/null | grep -icE 'NVRM|Xid'; "
                           "sudo -n dmesg 2>/dev/null | grep -icE 'Out of memory|oom-kill|oom_reaper'; "
                           "exit 0", timeout=60)
    vals = [int(x) for x in out.split() if x.strip().isdigit()]
    if rc != 0 or len(vals) < 2:
        return {}
    return {"nvrm_xid": vals[0], "oom": vals[1]}


def preflight(host: str, args) -> dict:
    """Полный прогон предусловий. ``ok`` — можно ли запускать стадии."""
    checks: list[dict] = []
    rc, out, err = ssh(host, "hostname")
    reachable = rc == 0
    checks.append({"name": "host", "ok": reachable,
                   "detail": (f"{host} → {out.strip()}" if reachable
                              else f"{host} недоступен: {err.strip()}")})
    if not reachable:
        return {"ok": False, "checks": checks, "host": host}
    checks.append(check_free_memory(host, args.min_free_gb))
    checks.append(check_platform_containers(host))
    checks.append(check_serialization_guard(host, strict=True))
    checks.append(check_image(host, args.image))
    checks.append(check_data(host, args.attn))
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "host": host,
            "dmesg_before": dmesg_counts(host)}


# ─── разбор замеров (чистые функции — тестируются без стенда) ──────────────────

def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def rel_or_abs(path: Path) -> str:
    """Путь от корня кейса, если он внутри; иначе абсолютный (--runs-dir вне кейса)."""
    try:
        return str(Path(path).relative_to(CASE_ROOT))
    except ValueError:
        return str(path)


def parse_probe(path: Path) -> dict:
    """JSONL обвязки → {start, steps, end}."""
    rows = read_jsonl(path)
    start = next((r for r in rows if r.get("event") == "start"), {})
    end = next((r for r in rows if r.get("event") == "end"), {})
    steps = [r for r in rows if r.get("event") == "step"]
    return {"start": start, "steps": steps, "end": end}


def slope(xs: list[float], ys: list[float]) -> float | None:
    """МНК-наклон (единицы y на шаг); None, если точек мало."""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def summarize_steps(steps: list[dict], end: dict, wall_seconds: float | None) -> dict:
    """Замеры стадии: tok/s, время шага, loss (первые/последние, тренд), entropy.

    Тренд loss считается двумя способами, и оба печатаются: ``loss_first``/
    ``loss_last`` — средние по крайним десяткам шагов (устойчиво к шуму одного
    шага), ``loss_slope`` — МНК-наклон по всей стадии. На разогреве WSD loss на
    коротком окне может расти (домен-сдвиг) — тогда это видно как положительный
    наклон при падающем хвосте, и вердикт не подменяется одним числом.

    Время шага и ``tok/s`` считаются по **интервалам между началами обучающих
    forward'ов** (``dt_step``), и только по тем интервалам, внутри которых не
    было eval-проходов (``skipped_before == 0``): backward и optimizer живут вне
    ``model(...)``, а GEN-EVAL/probes вклиниваются между шагами и без фильтра
    раздули бы время шага в разы. Сколько интервалов реально пошло в замер —
    в ``step_intervals_used``, время eval — в ``eval_seconds``.
    """
    if not steps:
        return {"steps": 0, "note": "обвязка не записала ни одного обучающего шага"}
    losses = [s["loss"] for s in steps]
    fwd = [s.get("dt") for s in steps if s.get("dt") is not None]
    tokens = [s.get("tokens", 0) for s in steps]
    k = max(1, len(steps) // 10)
    total_tokens = sum(tokens)
    usable = [s for s in steps
              if s.get("dt_step") is not None and not s.get("skipped_before")]
    step_secs = [s["dt_step"] for s in usable]
    step_tokens = sum(s.get("tokens", 0) for s in usable)
    entropies = [s["entropy"] for s in steps if s.get("entropy") is not None]
    first_k, last_k = losses[:k], losses[-k:]
    out = {
        "steps": len(steps),
        "tokens_total": total_tokens,
        "train_seconds": round(sum(step_secs), 2),
        "step_intervals_used": len(step_secs),
        "step_intervals_skipped_eval": len([s for s in steps
                                            if s.get("skipped_before")]),
        "wall_seconds": None if wall_seconds is None else round(wall_seconds, 1),
        "tok_per_s": round(step_tokens / sum(step_secs), 1) if step_secs else None,
        "step_seconds_mean": round(sum(step_secs) / len(step_secs), 3) if step_secs else None,
        "step_seconds_min": round(min(step_secs), 3) if step_secs else None,
        "step_seconds_max": round(max(step_secs), 3) if step_secs else None,
        "forward_seconds_mean": round(sum(fwd) / len(fwd), 4) if fwd else None,
        "loss_first": round(sum(first_k) / len(first_k), 4),
        "loss_last": round(sum(last_k) / len(last_k), 4),
        "loss_first_step": round(losses[0], 4),
        "loss_last_step": round(losses[-1], 4),
        "loss_min": round(min(losses), 4),
        "loss_max": round(max(losses), 4),
        "loss_argmin_step": int(losses.index(min(losses))),
        "loss_slope_per_step": (None if slope(list(range(len(losses))), losses) is None
                                else round(slope(list(range(len(losses))), losses), 6)),
        "loss_decreasing_head_to_tail": bool(last_k) and (sum(last_k) / len(last_k)) < (sum(first_k) / len(first_k)),
        "loss_decreasing_after_peak": losses[-1] < max(losses),
        "tokens_per_step": sorted(set(tokens)),
    }
    if entropies:
        out["entropy"] = {
            "points": len(entropies),
            "mean": round(sum(entropies) / len(entropies), 4),
            "first": round(entropies[0], 4),
            "last": round(entropies[-1], 4),
            "min": round(min(entropies), 4),
            "max": round(max(entropies), 4),
            "corridor": [1.0, 2.0],
            "in_corridor": 1.0 <= sum(entropies) / len(entropies) <= 2.0,
        }
    else:
        out["entropy"] = {"points": 0}
    if end:
        out["skipped_forwards"] = end.get("skipped_forwards")
        out["eval_seconds"] = end.get("skipped_seconds")
        out["probe_seconds"] = end.get("probe_seconds")
        if "torch_peak_allocated_bytes" in end:
            out["torch_peak_allocated_gb"] = round(end["torch_peak_allocated_bytes"] / 2**30, 2)
            out["torch_peak_reserved_gb"] = round(end.get("torch_peak_reserved_bytes", 0) / 2**30, 2)
        if end.get("error"):
            out["error"] = end["error"]
    return out


def parse_mem_samples(path: Path, container_hint: str = "") -> dict:
    """Пик unified-памяти хоста и (вторично) память контейнера смоука.

    Основная величина — хост: у GB10 память общая с CPU, и только ``MemAvailable``
    показывает, сколько её реально осталось. ``docker stats`` даёт память,
    *учтённую cgroup* контейнера, а CUDA-аллокации unified-памяти могут в неё не
    попадать — поэтому расхождение с хостом не «шум», а свойство платформы, и оно
    называется явно, а не сглаживается.
    """
    rows = read_jsonl(path)
    if not rows:
        return {"samples": 0, "note": "сэмплов памяти нет"}
    used = [r["mem_total_kb"] - r["mem_available_kb"] for r in rows
            if "mem_total_kb" in r and "mem_available_kb" in r]
    total_gb = round(rows[0].get("mem_total_kb", 0) / 1048576, 1)
    out = {
        "samples": len(rows),
        "mem_total_gb": total_gb,
        "peak_used_gb": round(max(used) / 1048576, 2) if used else None,
        "min_available_gb": round(min(r.get("mem_available_kb", 0) for r in rows) / 1048576, 2),
        "used_first_gb": round(used[0] / 1048576, 2) if used else None,
        "used_last_gb": round(used[-1] / 1048576, 2) if used else None,
    }
    if container_hint:
        peak = 0.0
        for r in rows:
            for item in str(r.get("containers", "")).split(";"):
                name, _, usage = item.partition("=")
                if container_hint in name:
                    peak = max(peak, parse_docker_mem(usage))
        out["container_peak_gb"] = round(peak, 2) if peak else None
        out["container_mem_source"] = ("docker stats (память cgroup; CUDA-аллокации "
                                       "unified могут не учитываться)")
        host_delta = (max(used) - used[0]) / 1048576 if used else 0.0
        if peak > total_gb:
            out["container_peak_note"] = ("память контейнера по cgroup больше всей памяти "
                                          "стенда — величина недостоверна, опираться на "
                                          "пик по хосту")
        elif host_delta > 1 and peak < 0.5 * host_delta:
            out["container_peak_note"] = (
                f"память контейнера по cgroup ({round(peak, 2)} ГБ) много меньше прироста "
                f"по хосту ({round(host_delta, 2)} ГБ) — CUDA-аллокации unified-памяти "
                f"в cgroup не учитываются; опираться на пик по хосту")
    return out


#: Коэффициенты к гибибайтам. Единицы docker stats — десятичные/двоичные пары
#: (`GB`/`GiB`, `MB`/`MiB`), и разница в 1024× здесь не косметическая: неверный
#: коэффициент даёт «пик памяти контейнера» в сотни терабайт.
_MEM_UNITS = {
    "B": 1 / 2 ** 30, "KIB": 1 / 2 ** 20, "MIB": 1 / 2 ** 10, "GIB": 1.0, "TIB": 2 ** 10,
    "KB": 1e3 / 2 ** 30, "MB": 1e6 / 2 ** 30, "GB": 1e9 / 2 ** 30, "TB": 1e12 / 2 ** 30,
}


def parse_docker_mem(text: str) -> float:
    """``'12.3GiB / 121GiB'`` → 12.3 (ГиБ); ``'675.4MiB / 121GiB'`` → 0.66."""
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?i?B)", text or "")
    if not m:
        return 0.0
    return float(m.group(1)) * _MEM_UNITS.get(m.group(2).upper(), 1.0)


INCIDENT_RE = re.compile(
    r"CUDA out of memory|OutOfMemoryError|torch\.cuda\.OutOfMemory|"
    r"NV_ERR_NO_MEMORY|NVRM|Xid|oom-kill|Out of memory|Killed|rc=137|exit code 137")


def scan_incidents(log_text: str) -> list[str]:
    """Инциденты OOM/NVRM в логе стадии (поимённо, а не счётчиком)."""
    hits = []
    for line in log_text.splitlines():
        if INCIDENT_RE.search(line):
            hits.append(line.strip()[:300])
    return hits[:20]


# ─── сценарий стадии ──────────────────────────────────────────────────────────

def build_stage_script(*, ts: str, run_id: str, stage: str, exp_name: str, args) -> str:
    """Скрипт стадии для стенда: сэмплер памяти + safe_start + docker run + обвязка.

    Скрипт кладётся на стенд целиком: его видно после прогона, и стадию можно
    повторить вручную — без реконструкции строки запуска из логов раннера.
    """
    is_cpt = stage == "cpt"
    steps = args.cpt_steps if is_cpt else args.sft_steps
    batch = args.cpt_batch if is_cpt else args.sft_batch
    max_samples = args.cpt_max_samples if is_cpt else args.sft_max_samples
    ctr_name = f"laguna-smoke-{ts}-{stage}"
    #: Хост-путь каталога прогона (сэмплер, лог, tee) и он же — глазами контейнера
    #: (обвязка, метрики): один каталог на диске, два имени. Разделены явно, потому
    #: что `python3 "$RUN_DIR/..."` внутри `docker run` ищет хост-путь в контейнере,
    #: где его нет.
    host_run_dir = f"{STAND_EXPERIMENTS}/{run_id}"
    ctr_run_dir = f"{CTR_EXPERIMENTS}/{run_id}"
    #: Логи и чекпойнты обеих стадий живут в одном каталоге прогона: SFT обязан
    #: подхватить `checkpoint_final.pt` CPT (иначе он молча учится с базовой
    #: модели — пайплайн не падает, если чекпойнта нет, поэтому путь общий явно).
    ckpt_dir = f"{ctr_run_dir}/checkpoints"
    log_dir = f"{ctr_run_dir}/logs"
    env_attn = f'-e LAGUNA_ATTN={args.attn} -e FLEX_COMPILE=0'
    probe_args = " ".join([
        f"--model_name {args.model}", f"--stage {stage}", f"--exp_name {exp_name}",
        f"--max_steps {steps}", f"--max_samples {max_samples}", f"--batch_size {batch}",
        f"--max_len {args.max_len}", f"--seed {args.seed}",
        f"--peak_lr_scale {args.peak_lr_scale}",
        f"--cpt_data {CPT_DATA}", f"--sft_data {SFT_DATA}",
        f"--rl_data {RL_DATA}", f"--eval_data {EVAL_DATA}",
        f"--ckpt_dir {ckpt_dir}", f"--log_dir {log_dir}",
    ])
    return f"""#!/usr/bin/env bash
# Стадия смоука S2: {stage}. Сгенерировано tools/run_smoke.py (ts={ts}).
# Ручное повторение: bash {host_run_dir}/run_{stage}.sh
set -uo pipefail
RUN_DIR="{host_run_dir}"          # хост-путь (сэмплер памяти, лог, tee)
CTR_RUN_DIR="{ctr_run_dir}"       # тот же каталог глазами контейнера
STAGE="{stage}"
CTR="{ctr_name}"
LOG="$RUN_DIR/logs/{stage}.log"
METRICS="$CTR_RUN_DIR/probe_{stage}.jsonl"
MEM="$RUN_DIR/mem_{stage}.jsonl"
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/checkpoints"

bash "$RUN_DIR/smoke_mem_sampler.sh" "$MEM" 5 {args.stage_timeout} &
SAMPLER=$!
trap 'kill "$SAMPLER" 2>/dev/null' EXIT

echo "SMOKE_STAGE_START=$STAGE ts=$(date -Is)"
bash {SAFE_START} -d 60 -i 5 -- docker run --rm --name "$CTR" \\
  --gpus all --ipc=host --pid=host \\
  --memory=100g --memory-swap=100g \\
  --security-opt seccomp=unconfined --cap-add SYS_PTRACE \\
  --ulimit memlock=-1 --ulimit stack=67108864 --ulimit nofile=262144:262144 \\
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -e HF_HUB_OFFLINE=1 \\
  -e TRANSFORMERS_OFFLINE=1 -e LAGUNA_MEM_FRACTION={args.mem_fraction} \\
  -e VLLM_ALLOW_INSECURE_SERIALIZATION=1 -e PYTHONPATH=/workspace/shared/wt_stubs \\
  -e CPT_GEN_EVAL_EVERY={args.gen_eval_every} \\
  {env_attn} \\
  -v {STAND_EXPERIMENTS}:{CTR_EXPERIMENTS} \\
  -v {STAND_SHARED}:{CTR_SHARED} \\
  -v /home/user/.cache/huggingface:/root/.cache/huggingface -w /workspace \\
  {args.image} \\
  python3 "$CTR_RUN_DIR/smoke_probe.py" \\
    --metrics "$METRICS" --stage "$STAGE" --exp-name "{exp_name}" \\
    --entropy {args.entropy} --entropy-every {args.entropy_every} \\
    --entropy-pos-stride {args.entropy_pos_stride} \\
    --pipeline {PIPELINE_CTR} \\
    -- {probe_args} \\
  2>&1 | tee "$LOG"
RC=${{PIPESTATUS[0]}}
kill "$SAMPLER" 2>/dev/null
echo "SMOKE_STAGE_EXIT=$RC"
exit "$RC"
"""


def render_plan(args, run_id: str, host: str) -> str:
    """План прогона: что будет запущено, куда и при каких предусловиях."""
    dirs = f"{STAND_EXPERIMENTS}/{run_id}"
    lines = [
        "== S2: план смоук-прогона (CPT + SFT, один сид) ==",
        f"стенд:            {host} (ssh), образ: {args.image}",
        f"каталог прогона:  стенд {dirs}  ←  кейс {args.runs_dir}/smoke-{args.ts}",
        f"модель/сид:       {args.model}, seed={args.seed}, max_len={args.max_len}",
        f"предусловия:      свободной unified-памяти ≥ {args.min_free_gb} ГБ; "
        f"страж AD-5 в --strict; llm-platform-* не трогаются",
        f"запуск стадии:    {SAFE_START} -d 60 -i 5 -- docker run ... (absorbер включён)",
        "",
        f"CPT: steps={args.cpt_steps} batch={args.cpt_batch} "
        f"max_samples={args.cpt_max_samples} attn={args.attn} — данные {CPT_DATA}",
        f"SFT: steps={args.sft_steps} batch={args.sft_batch} "
        f"max_samples={args.sft_max_samples} — данные {SFT_DATA} + чекпойнт CPT",
        "RL:  НЕ запускается (смоук ограничен CPT+SFT)",
        "",
        "замеры: tok/s по обучающим шагам, время шага, loss по шагам, токенная "
        "entropy, пик unified-памяти хоста, пик памяти torch, инциденты OOM/NVRM",
        f"артефакты: {args.runs_dir}/smoke-{args.ts}/ (манифест AD-2, логи, "
        f"probe_*.jsonl, mem_*.jsonl) + {args.evidence}",
    ]
    return "\n".join(lines)


# ─── исполнение ───────────────────────────────────────────────────────────────

def run_stage(host: str, args, run_id: str, stage: str, local_dir: Path) -> dict:
    """Одна стадия: доставка скриптов, запуск, поток лога, возврат артефактов."""
    exp_name = f"smoke-compact-{stage}-{args.ts}"
    run_dir = f"{STAND_EXPERIMENTS}/{run_id}"
    script = build_stage_script(ts=args.ts, run_id=run_id, stage=stage,
                                exp_name=exp_name, args=args)
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / f"run_{stage}.sh").write_text(script, encoding="utf-8")

    shipped = {}
    for name, payload in ((f"run_{stage}.sh", script.encode()),
                          ("smoke_probe.py", (CASE_ROOT / "tools" / "smoke_probe.py").read_bytes()),
                          ("smoke_mem_sampler.sh",
                           (CASE_ROOT / "tools" / "smoke_mem_sampler.sh").read_bytes())):
        rc, err = ssh_put(host, f"{run_dir}/{name}", payload)
        if rc != 0:
            return {"stage": stage, "ok": False,
                    "error": f"доставка {name} на стенд не удалась: {err.strip()}"}
        shipped[name] = sha256_bytes(payload)
    # Проверка доставки: инструмент на стенде должен быть байт-в-байт тем, что мы
    # отправили. Иначе замер сделан не тем кодом, который лежит в git, — и это
    # выясняется до стадии, а не после расхождения цифр.
    rc, out, err = ssh(host, "sha256sum " + " ".join(f"{run_dir}/{n}" for n in shipped))
    if rc != 0:
        return {"stage": stage, "ok": False, "error": f"sha256sum на стенде: {err.strip()}"}
    broken = [line.split()[1] for line in out.splitlines()
              if len(line.split()) == 2 and shipped.get(Path(line.split()[1]).name)
              != line.split()[0]]
    if broken or len(out.splitlines()) != len(shipped):
        return {"stage": stage, "ok": False,
                "error": f"инструменты на стенде не совпали с отправленными: {broken or out}"}

    log_path = local_dir / "logs" / f"{stage}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run_smoke] {stage}: запуск на {host}, лог → {log_path}")
    t0 = time.time()
    rc_stage = None
    try:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", host, f"bash {run_dir}/run_{stage}.sh"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        return {"stage": stage, "ok": False, "error": f"ssh не запустился: {e}"}

    with log_path.open("w", encoding="utf-8") as fh:
        assert proc.stdout is not None
        for line in proc.stdout:
            fh.write(line)
            fh.flush()
            m = re.search(r"SMOKE_STAGE_EXIT=(\d+)", line)
            if m:
                rc_stage = int(m.group(1))
            if PROGRESS_RE.search(line):
                print("    " + line.rstrip()[:200])
    proc.wait(timeout=60)
    wall = time.time() - t0

    result = {"stage": stage, "exp_name": exp_name, "exit": rc_stage,
              "ssh_exit": proc.returncode, "wall_seconds": round(wall, 1),
              "probe_sha256": shipped["smoke_probe.py"],
              "sampler_sha256": shipped["smoke_mem_sampler.sh"],
              "script_sha256": shipped[f"run_{stage}.sh"],
              "local_log": str(log_path), "ok": rc_stage == 0}
    if rc_stage is None:
        result["ok"] = False
        result["error"] = f"стадия не напечатала SMOKE_STAGE_EXIT (ssh rc={proc.returncode})"

    # Артефакты стадии: сырые замеры забираем к себе — доказательная база должна
    # пережить и стенд, и `docker stop`.
    for remote, local in ((f"probe_{stage}.jsonl", "probe_{}.jsonl".format(stage)),
                          (f"mem_{stage}.jsonl", "mem_{}.jsonl".format(stage)),
                          (f"logs/probes_{stage}.json", f"probes_{stage}.json"),
                          ("general_eval_history.jsonl", "general_eval_history.jsonl"),
                          ("checkpoints/run_manifest.json", "pipeline_run_manifest.json")):
        rc, data, err = ssh_get(host, f"{run_dir}/{remote}")
        if rc == 0 and data:
            (local_dir / local).write_bytes(data)
            result[f"fetched_{local}"] = len(data)
        else:
            result.setdefault("missing_artifacts", []).append(remote)
    return result


def analyze(run_dir: Path, args, stages: list[str]) -> dict:
    """Сводка замеров по артефактам стадий."""
    summary: dict = {"stages": {}}
    hist = run_dir / "general_eval_history.jsonl"
    if hist.is_file():
        summary["general_eval_history"] = read_jsonl(hist)
    for stage in stages:
        entry: dict = {}
        probe = run_dir / f"probe_{stage}.jsonl"
        log = run_dir / "logs" / f"{stage}.log"
        mem = run_dir / f"mem_{stage}.jsonl"
        if probe.is_file():
            parsed = parse_probe(probe)
            entry["probe_start"] = parsed["start"]
            #: Замеры стадии лежат в evidence плоско (``measurements.cpt.steps``),
            #: а не вложенным ``measurements.cpt.measurements``: читателю сводки
            #: нужны числа, а не структура внутренних вызовов.
            entry.update(summarize_steps(parsed["steps"], parsed["end"], None))
        else:
            entry["error"] = f"нет {probe.name}"
        if mem.is_file():
            entry["memory"] = parse_mem_samples(mem, container_hint=f"laguna-smoke-{args.ts}-{stage}")
        else:
            entry["memory"] = {"error": f"нет {mem.name}"}
        if log.is_file():
            text = log.read_text(encoding="utf-8", errors="replace")
            incidents = scan_incidents(text)
            entry["incidents"] = incidents
            entry["incidents_count"] = len(incidents)
        else:
            entry["incidents"] = []
            entry["incidents_count"] = 0
        summary["stages"][stage] = entry
    return summary


def instrument_pins(host: str | None, run_id: str,
                    stage_results: dict | None = None) -> dict:
    """Хеши инструментов, которыми получены и собраны замеры (провенанс обвязки).

    Три уровня доказательства, по возрастанию силы:

    * ``case_*`` — файлы кейса на момент сборки сводки;
    * ``ship_*`` — то, что раннер отправил на стенд и сверил ``sha256sum``
      (заполняется при обычном прогоне, а не при пересборке сводки);
    * ``stand_*`` — хеши файлов **в каталоге прогона на стенде**, откуда стадия их
      и запускала. Совпадение ``stand_*`` с ``case_*`` доказывает, что замер
      сделан тем же кодом, что лежит в кейсе, — даже когда сводку пересобирали
      позже. Расхождение обязано быть видно, а не растворено в тексте.
    """
    case_probe = sha256_path(CASE_ROOT / "tools" / "smoke_probe.py")
    case_sampler = sha256_path(CASE_ROOT / "tools" / "smoke_mem_sampler.sh")
    pins = {"case_probe_sha256": case_probe, "case_sampler_sha256": case_sampler,
            "case_runner_sha256": sha256_path(CASE_ROOT / "tools" / "run_smoke.py"),
            "note": ("case_* — файлы кейса на момент сборки сводки (--analyze-only "
                     "пересобирает после правок раннера, поэтому хеш раннера относится "
                     "к сборке отчёта); stand_* — файлы в каталоге прогона на стенде")}
    run_dir = f"{STAND_EXPERIMENTS}/{run_id}"
    if host:
        rc, out, _ = ssh(host, f"sha256sum {run_dir}/smoke_probe.py {run_dir}/smoke_mem_sampler.sh",
                         timeout=60)
        stand = {}
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    stand[Path(parts[1]).name] = parts[0]
        pins["stand_probe_sha256"] = stand.get("smoke_probe.py")
        pins["stand_sampler_sha256"] = stand.get("smoke_mem_sampler.sh")
        pins["stand_available"] = bool(stand)
        pins["stand_matches_case"] = bool(
            stand and stand.get("smoke_probe.py") == case_probe
            and stand.get("smoke_mem_sampler.sh") == case_sampler)
    for stage, res in (stage_results or {}).items():
        if res.get("probe_sha256"):
            pins[f"ship_{stage}_probe_sha256"] = res["probe_sha256"]
            pins[f"ship_{stage}_sampler_sha256"] = res.get("sampler_sha256")
    return pins


def sha256_path(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def acceptance_verdict(summary: dict, incidents: int, post: dict) -> dict:
    """Механическая сверка с критериями приёмки смоука (SPEC §5.4).

    Критерий объявлен до прогона, поэтому и вердикт по нему считается кодом, а не
    подбирается в тексте отчёта. Про энтропию: пайплайн её не считает, ко ридор
    SPEC не говорит, какой именно энтропии он про 1.0–2.0 (величина политики RL
    и токенная энтропия языковой модели — разные шкалы). Поэтому здесь
    публикуется и вердикт по букве коридора, и сама величина с названной
    методикой: расхождение обязано быть видно, а не сглажено формулировкой.
    """
    per_stage = {}
    for stage, m in summary["stages"].items():
        ent = m.get("entropy") or {}
        per_stage[stage] = {
            "steps": m.get("steps"),
            "loss_first": m.get("loss_first"),
            "loss_last": m.get("loss_last"),
            "loss_decreasing_head_to_tail": m.get("loss_decreasing_head_to_tail"),
            "loss_slope_per_step": m.get("loss_slope_per_step"),
            "entropy_mean": ent.get("mean"),
            "entropy_points": ent.get("points"),
            "entropy_corridor": ent.get("corridor"),
            "entropy_in_corridor": ent.get("in_corridor"),
        }
    return {
        "criteria": ("SPEC §5.4: loss убывает, entropy в коридоре 1.0–2.0, "
                     "нет OOM/NVRM, бюджет шага не превышен"),
        "per_stage": per_stage,
        "no_incidents_in_logs_and_dmesg": incidents,
        "container_closed": post.get("container_closed"),
        "platform_alive_after": not post.get("platform_lost"),
        "entropy_note": ("энтропия измерена обвязкой как средняя токенная энтропия "
                         "распределения модели на супервизируемых позициях; SPEC не "
                         "называет, какой энтропии принадлежит коридор 1.0–2.0"),
    }


def build_evidence(run_dir: Path, args, pre: dict, stage_results: dict,
                   summary: dict, manifest_path: Path | None, post: dict) -> dict:
    """``evidence/s2-smoke.json`` — сводка замеров + явный список непроверенного."""
    dmesg_after = post.get("dmesg_after") or {}
    before = pre.get("dmesg_before") or {}
    nvrm_delta = None
    oom_delta = None
    if dmesg_after and before:
        nvrm_delta = max(0, dmesg_after.get("nvrm_xid", 0) - before.get("nvrm_xid", 0))
        oom_delta = max(0, dmesg_after.get("oom", 0) - before.get("oom", 0))
    incidents = sum(v.get("incidents_count", 0) for v in summary["stages"].values())
    return {
        "acceptance": acceptance_verdict(summary, incidents, post),
        "stage": "S2",
        "date": datetime.now().isoformat(timespec="seconds"),
        "purpose": ("доказательство, что контур сходится: CPT 20–50 шагов + SFT 20–30 "
                    "шагов на реальном миксе, один сид, Qwen2.5-0.5B"),
        "stand": {"host": pre.get("host"), "run_dir": f"{STAND_EXPERIMENTS}/{args.run_id}",
                  "case_run_dir": rel_or_abs(run_dir)},
        "preconditions": {"required_free_gb": args.min_free_gb,
                          "checks": pre.get("checks", []),
                          "ok": pre.get("ok")},
        "instrument": instrument_pins(pre.get("host"), args.run_id, stage_results),
        "config": {
            "model": args.model, "seed": args.seed, "max_len": args.max_len,
            "image": args.image, "attn": args.attn,
            "cpt": {"steps": args.cpt_steps, "batch": args.cpt_batch,
                    "max_samples": args.cpt_max_samples, "data": CPT_DATA,
                    "tok_cache": CPT_TOK_CACHE},
            "sft": {"steps": args.sft_steps, "batch": args.sft_batch,
                    "max_samples": args.sft_max_samples, "data": SFT_DATA,
                    "tok_cache": SFT_TOK_CACHE},
            "rl": "не запускалась",
            "launch": f"{SAFE_START} -d 60 -i 5 -- docker run (absorbер включён)",
            "instrumentation": {"probe": "tools/smoke_probe.py",
                                "mem_sampler": "tools/smoke_mem_sampler.sh",
                                "entropy": f"{args.entropy}, подвыборка позиций с шагом "
                                           f"{args.entropy_pos_stride}, блок {args.entropy_block}"},
        },
        "stage_results": stage_results,
        "measurements": summary["stages"],
        "forgetting_ppl": summary.get("general_eval_history", []),
        "incidents": {
            "per_stage_from_logs": {s: summary["stages"][s].get("incidents_count", 0)
                                    for s in summary["stages"]},
            "dmesg_nvrm_xid_delta": nvrm_delta,
            "dmesg_oom_delta": oom_delta,
            "dmesg_note": ("dmesg — кольцевой буфер: при перезаписи дельта занижается; "
                           "основное доказательство — лог стадии и код возврата"),
        },
        "postconditions": {
            "containers_after": post.get("containers"),
            "platform_alive": post.get("platform_alive"),
            "free_after_g": post.get("free_g"),
            "container_closed": post.get("container_closed"),
        },
        "run_manifest": None if manifest_path is None else rel_or_abs(manifest_path),
        "not_verified": [
            "RL-стадия и всё, что с ней связано: reward, pass-rate, judge_mean — "
            "смоук ограничен CPT+SFT по заданию дельты; AD-1 (ось успеха) смоуком не закрывается",
            "eval-стадия и метрики judge_mean/slug_accuracy: в смоуке не запускались "
            "(нужны GLM-судья и полный набор; не входят в перечень замеров дельты)",
            "три сида и полный набор SFT/RL: смоук — один сид и урезанные max_samples",
            "устойчивость к NVRM-бурям на длинных прогонах: окно смоука минуты, "
            "а не часы; отсутствие инцидентов здесь не переносится на S3",
            "энтропия: пайплайн её не логирует, замер сделан обвязкой "
            "(средняя токенная энтропия на супервизируемых позициях по подвыборке) — "
            "это методика смоука, а не величина, которую считает контур",
            "контроль «loss убывает» на коротком окне CPT ограничен разогревом WSD "
            "и домен-сдвигом: вердикт считается по крайним десяткам шагов и наклону, "
            "оба числа в замерах",
        ],
        "evidence_limits": ("замеры получены обвязкой tools/smoke_probe.py и сэмплером "
                            "tools/smoke_mem_sampler.sh; пайплайн не менялся, "
                            "sha256 пайплайна — в run_manifest.json"),
    }


def rebuild_evidence(evidence_path: Path, run_dir: Path, args, summary: dict,
                     manifest_path: Path | None, post: dict) -> dict:
    """Пересборка сводки из артефактов **без потери уже записанного**.

    Обновляются только выводимые из артефактов поля (замеры, инциденты стадий,
    вердикт по критериям, PPL-история). ``preconditions``, ``stage_results``,
    ``postconditions`` и ``config`` берутся из существующего evidence: состояние
    стенда **до и после** прогона повторным замером не восстанавливается, и
    подменять его текущим состоянием значило бы переписать историю. Если файла
    ещё нет (смоук прогнали вручную), собирается скелет, а пропуски названы явно.
    """
    existing: dict = {}
    if evidence_path.is_file():
        try:
            loaded = json.loads(evidence_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError as e:
            print(f"NOT-VERIFIED: {evidence_path} не разбирается как JSON ({e})",
                  file=sys.stderr)
            raise SystemExit(EXIT_NOT_VERIFIED) from e
    pre = {"host": args.host,
           "checks": existing.get("preconditions", {}).get("checks", []),
           "ok": existing.get("preconditions", {}).get("ok"),
           "dmesg_before": {}}
    stage_results = existing.get("stage_results",
                                 {s: {"stage": s, "analyzed_only": True}
                                  for s in summary["stages"]})
    post = dict(post)
    if existing.get("postconditions"):
        # Состояние «после прогона» восстанавливается только из прежнего отчёта:
        # замер сейчас — это «сейчас», а не «сразу после стадии».
        pc = existing["postconditions"]
        post = {"containers": pc.get("containers_after"),
                "container_closed": pc.get("container_closed"),
                "platform_alive": pc.get("platform_alive"), "platform_lost": [],
                "free_g": pc.get("free_after_g"), "mem_free_gb": None,
                "smoke_containers_running": [], "dmesg_after": {}}
    ev = build_evidence(run_dir, args, pre, stage_results, summary, manifest_path, post)
    old_inc = existing.get("incidents", {})
    for key in ("dmesg_nvrm_xid_delta", "dmesg_oom_delta"):
        if key in old_inc:
            ev["incidents"][key] = old_inc[key]
    if existing:
        ev["rebuilt"] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "note": ("сводка пересобрана из артефактов; предусловия, результаты стадий "
                     "и состояние стенда после прогона взяты из прежнего evidence и "
                     "повторным замером не подменялись"),
        }
    else:
        ev["not_verified"].append(
            "предусловия и состояние стенда после прогона не записаны: evidence собран "
            "из артефактов без запуска (--analyze-only)")
    return ev


def stop_containers(host: str, ts: str, stages: list[str]) -> dict:
    """План отката: закрыть контейнеры смоука по имени (llm-platform-* не трогаем)."""
    names = [f"laguna-smoke-{ts}-{s}" for s in stages]
    rc, out, err = ssh(host, "docker ps -a --format '{{.Names}}'")
    if rc != 0:
        return {"ok": False, "detail": f"docker ps недоступен: {err.strip()}"}
    present = [n for n in names if n in out.split()]
    if not present:
        return {"ok": True, "detail": "контейнеров смоука нет (уже закрыты)", "stopped": []}
    rc, out2, err2 = ssh(host, "docker stop " + " ".join(present), timeout=180)
    return {"ok": rc == 0, "stopped": present,
            "detail": (f"остановлены: {', '.join(present)}" if rc == 0
                       else f"docker stop rc={rc}: {err2.strip()}")}


def postcheck(host: str, ts: str, pre: dict, stages: list[str]) -> dict:
    """Состояние стенда после прогона: контейнер закрыт, платформа жива, память свободна."""
    rc, out, _ = ssh(host, "docker ps --format '{{.Names}}'")
    running = out.split() if rc == 0 else []
    smoke_running = [n for n in running if n.startswith("laguna-smoke-")]
    platform = [n for n in running if n.startswith(PLATFORM_PREFIX)]
    rc, free_out, _ = ssh(host, "free -g")
    rc2, mem_raw, _ = ssh(host, "cat /proc/meminfo")
    return {
        "containers": running,
        "smoke_containers_running": smoke_running,
        "container_closed": not smoke_running,
        "platform_alive": platform,
        "platform_lost": sorted(set((pre.get("platform") or {}).keys()) - set(platform)),
        "free_g": free_out,
        "mem_free_gb": free_gb(parse_meminfo(mem_raw)) if rc2 == 0 else None,
        "dmesg_after": dmesg_counts(host),
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="S2: смоук-прогон CPT+SFT на стенде GB10 (ADR-007: бюджет памяти, "
                    "--strict, safe_start).")
    ap.add_argument("--host", default=os.environ.get("GB10_HOST", DEFAULT_HOST),
                    help=f"стенд по ssh (по умолчанию {DEFAULT_HOST} / $GB10_HOST)")
    ap.add_argument("--ts", default=datetime.now().strftime("%Y%m%d-%H%M"),
                    help="метка прогона (по умолчанию — текущее время)")
    ap.add_argument("--image", default=os.environ.get("LAGUNA_IMAGE", DEFAULT_IMAGE),
                    help="образ окружения (в манифест и docker run)")
    ap.add_argument("--model", default="Qwen/Qwen2.5-0.5B", help="базовая модель (AD-002)")
    ap.add_argument("--seed", type=int, default=42, help="сид прогона")
    ap.add_argument("--max-len", type=int, default=8192, help="длина чанка/примера")
    ap.add_argument("--cpt-steps", type=int, default=50, help="шагов CPT (20–50)")
    ap.add_argument("--sft-steps", type=int, default=30, help="шагов SFT (20–30)")
    ap.add_argument("--cpt-batch", type=int, default=1, help="батч CPT (flex eager — 1)")
    ap.add_argument("--sft-batch", type=int, default=2, help="батч SFT")
    ap.add_argument("--cpt-max-samples", type=int, default=9776, help="чанков CPT из кэша")
    ap.add_argument("--sft-max-samples", type=int, default=2000,
                    help="примеров SFT из кэша (смоук не равен полному прогону)")
    ap.add_argument("--peak-lr-scale", type=float, default=0.7,
                    help="множитель пикового LR WSD (как в run_v12_ladder.sh)")
    ap.add_argument("--attn", default="flex", choices=["flex", "none"],
                    help="LAGUNA_ATTN: flex — путь рабочего раннера; none — пакованный базлайн")
    ap.add_argument("--mem-fraction", default="0.6", help="LAGUNA_MEM_FRACTION (кап torch)")
    ap.add_argument("--gen-eval-every", type=int, default=10,
                    help="CPT_GEN_EVAL_EVERY: PPL общего/доменного языка (анти-форгеттинг); "
                         "10 даёт на 50 шагах 4 точки, 0 — выключить")
    ap.add_argument("--entropy", default="on", choices=["on", "off"], help="замер энтропии")
    ap.add_argument("--entropy-every", type=int, default=1, help="энтропия каждый N-й шаг")
    ap.add_argument("--entropy-pos-stride", type=int, default=16,
                    help="шаг подвыборки позиций для энтропии")
    ap.add_argument("--entropy-block", type=int, default=64,
                    help="строк логитов на блок при расчёте энтропии")
    ap.add_argument("--min-free-gb", type=float, default=MIN_FREE_GB,
                    help=f"порог свободной unified-памяти (ADR-007: {MIN_FREE_GB} ГБ)")
    ap.add_argument("--stage-timeout", type=int, default=1800,
                    help="предел стадии и окно сэмплера памяти, с")
    ap.add_argument("--runs-dir", default="runs", help="каталог прогонов кейса")
    ap.add_argument("--evidence", default="evidence/s2-smoke.json", help="evidence-файл")
    ap.add_argument("--plan", action="store_true", help="напечатать план и выйти")
    ap.add_argument("--preflight-only", action="store_true",
                    help="только предусловия, стадии не запускать")
    ap.add_argument("--analyze-only", action="store_true",
                    help="пересобрать сводку и evidence из готовых артефактов")
    ap.add_argument("--stop-only", action="store_true",
                    help="закрыть контейнеры смоука по имени (план отката)")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)
    args.run_id = f"smoke-compact-{args.ts}"
    if args.plan and args.analyze_only:
        ap.error("--plan и --analyze-only несовместимы")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = CASE_ROOT / args.runs_dir / f"smoke-{args.ts}"
    stages = ["cpt", "sft"]

    if args.plan:
        print(render_plan(args, args.run_id, args.host))
        return EXIT_OK

    if args.stop_only:
        res = stop_containers(args.host, args.ts, stages)
        print(f"[run_smoke] закрытие контейнеров смоука: {res['detail']}")
        return EXIT_OK if res["ok"] else EXIT_FAIL

    if args.analyze_only:
        if not run_dir.is_dir():
            print(f"NOT-VERIFIED: каталога прогона нет: {run_dir}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        summary = analyze(run_dir, args, stages)
        post = postcheck(args.host, args.ts, {}, stages)
        manifest = run_dir / "run_manifest.json"
        ev = rebuild_evidence(CASE_ROOT / args.evidence, run_dir, args, summary,
                              manifest if manifest.is_file() else None, post)
        write_evidence(CASE_ROOT / args.evidence, ev)
        print(f"[run_smoke] evidence пересобран из артефактов: {args.evidence}")
        return EXIT_OK

    pre = preflight(args.host, args)
    print(f"== предусловия ({args.host}) ==")
    for c in pre["checks"]:
        print(f"  [{'ok ' if c['ok'] else 'FAIL'}] {c['name']:<14} {c['detail']}")
    if not pre["ok"]:
        print()
        failed = [c["name"] for c in pre["checks"] if not c["ok"]]
        print(f"СТОП: предусловия не выполнены ({', '.join(failed)}) — стадии не запускаются")
        if args.json:
            print(json.dumps(pre, ensure_ascii=False, indent=2))
        return EXIT_FAIL
    if args.preflight_only:
        return EXIT_OK

    pre["platform"] = next((c.get("platform", {}) for c in pre["checks"]
                            if c["name"] == "containers"), {})
    stage_results = {}
    for stage in stages:
        res = run_stage(args.host, args, args.run_id, stage, run_dir)
        stage_results[stage] = res
        if not res["ok"]:
            reason = res.get("error") or f"код стадии {res.get('exit')}"
            print(f"[run_smoke] стадия {stage} НЕ завершилась успешно: {reason}")
            summary = analyze(run_dir, args, stages)
            post = postcheck(args.host, args.ts, pre, stages)
            post["dmesg_after"] = post.get("dmesg_after") or {}
            write_evidence(CASE_ROOT / args.evidence,
                           build_evidence(run_dir, args, pre, stage_results, summary, None, post))
            return EXIT_FAIL

    summary = analyze(run_dir, args, stages)
    post = postcheck(args.host, args.ts, pre, stages)
    post["dmesg_after"] = post.get("dmesg_after") or {}

    manifest = write_manifest(run_dir, args, stages)
    ev = build_evidence(run_dir, args, pre, stage_results, summary,
                        manifest, post)
    write_evidence(CASE_ROOT / args.evidence, ev)

    incidents = sum(v.get("incidents_count", 0) for v in summary["stages"].values())
    oom_delta, nvrm_delta = ev["incidents"]["dmesg_oom_delta"], ev["incidents"]["dmesg_nvrm_xid_delta"]
    print()
    print("== замеры ==")
    for stage, m in summary["stages"].items():
        print(f"  {stage}: steps={m.get('steps')} tok/s={m.get('tok_per_s')} "
              f"step={m.get('step_seconds_mean')}s loss {m.get('loss_first')}→"
              f"{m.get('loss_last')} entropy={m.get('entropy', {}).get('mean')}")
        mem = m.get("memory", {})
        print(f"      память: пик занятой {mem.get('peak_used_gb')} ГБ, "
              f"минимум свободной {mem.get('min_available_gb')} ГБ, "
              f"контейнер {mem.get('container_peak_gb')} ГБ")
    print()
    print(f"  контейнер закрыт: {post['container_closed']}; "
          f"llm-platform-* живы: {post['platform_alive'] or '— (не найдены)'}; "
          f"свободно после прогона: {post.get('mem_free_gb')} ГБ")
    print(f"  инциденты: лог {incidents}, dmesg NVRM/Xid Δ={nvrm_delta}, OOM Δ={oom_delta}")
    print()
    print(f"манифест: {rel_or_abs(manifest) if manifest else '—'}")
    print(f"evidence: {args.evidence}")

    # Стадия без записанных обучающих шагов — не «зелёная с малыми числами»:
    # это «замер не состоялся» (обвязка не увидела ни одного обучающего forward).
    empty = [s for s, v in summary["stages"].items() if not v.get("steps")]
    if empty:
        print(f"ВНИМАНИЕ: нет ни одного обучающего шага в замерах стадий: {', '.join(empty)} "
              f"— замер не состоялся")
    ok = (incidents == 0 and post["container_closed"] and not post["platform_lost"]
          and (oom_delta in (0, None)) and (nvrm_delta in (0, None)) and not empty
          and all(s.get("ok") for s in stage_results.values()))
    if args.json:
        print(json.dumps({"ok": ok, "measurements": summary, "post": post,
                          "incidents": ev["incidents"]}, ensure_ascii=False, indent=2))
    return EXIT_OK if ok else EXIT_FAIL


def write_manifest(run_dir: Path, args, stages: list[str]) -> Path | None:
    """Манифест AD-2 — генератором кейса (одна реализация на все прогоны)."""
    cmd = [sys.executable, str(CASE_ROOT / "tools" / "write_run_manifest.py"),
           "--run-dir", str(run_dir),
           "--dataset", str(CASE_ROOT / CPT_TOK_CACHE),
           "--base-model", args.model,
           "--pipeline", str(CASE_ROOT / "laguna_pipeline_v8.py"),
           "--seed", str(args.seed), "--image", args.image,
           "--stages", ",".join(f"{s}=done" for s in stages),
           "--complete",
           #: run_version — раннер прогона (SPEC §1a: имя файла + хеш, как у
           #: pipeline_version). Локальный раннер смоука — tools/run_smoke.py;
           #: safe_start.sh и образ — обёртки запуска, они названы отдельно.
           "--run-version",
           "tools/run_smoke.py@" + (sha256_path(CASE_ROOT / "tools" / "run_smoke.py") or "?")[:12],
           "--dataset-extra", f"sft_tok_cache={CASE_ROOT / SFT_TOK_CACHE}",
           "--relative-to", str(CASE_ROOT), "--force"]
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        print(f"ПРЕДУПРЕЖДЕНИЕ: манифест не записан (rc={p.returncode}): "
              f"{p.stderr.strip() or p.stdout.strip()}", file=sys.stderr)
        return None
    return run_dir / "run_manifest.json"


def write_evidence(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    sys.exit(main())
