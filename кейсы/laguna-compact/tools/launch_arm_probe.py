#!/usr/bin/env python3
"""S3ay-fix: сборка пакета пробы короткой руки на стенде — опора раннего гейта.

**Зачем инструмент, а не `cp` руками.** Первый пакет (`v13-arm-probe-20260921-1905`)
собирался руками, и в его `tools/` лёг ОДИН прибор — без `probe_control.py` и
`ppl_probe.py`, которые он импортирует на уровне файла. Обе пробы упали за две
секунды с `ModuleNotFoundError`; отчётов не появилось вовсе, а `CHAIN_DONE` был
записан (`done new=1 prev=1`) и выглядел готовностью опоры — на него смотрел стартер
полной стадии. Два урока стали требованиями к этому инструменту:

1. **Прибор доставляется вместе с модулями, и это проверяется по хешу** — до запуска,
   а не после. Обвязка сна доставляется копиями того же стража, что у прогонов
   сравнения (у серии состояний одна обвязка, а не три похожих), чекпойнты сверяются
   с источником.
2. **`CHAIN_DONE` несёт коды возврата.** Цепочка (`chain.sh`) выходит ненулём, если
   проба не снята; пакет без отчётов опорой не считается.

Чего инструмент НЕ делает: не трогает чужие каталоги прогонов (чекпойнты
**копируются**, источники только читаются), не правит остановленные артефакты, не
занимает локальную 4080 — проба идёт на стенде.

Запуск::

    python3 tools/launch_arm_probe.py --ts 20260921-1710 --arms v13 --do build
    python3 tools/launch_arm_probe.py --ts 20260921-1710 --do launch
    python3 tools/launch_arm_probe.py --ts 20260921-1710 --do facts
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
SHARED = Path("/home/user/gb10-shared")
STAND = "gb10-fast"
PKG_PREFIX = "v13-arm-probe2-"

#: Прибор и его модули. Три файла, а не один: `probe_language_split` импортирует
#: `probe_control` (промпты, метрики вырождения) и `ppl_probe` — на уровне файла.
PROBE_TOOLS = ("probe_language_split.py", "probe_control.py", "ppl_probe.py")
#: Прибор, пиннутый хешем (тот же, что в решающих отчётах S3ap/S3aq/S3aw/S3ax).
#: Хеш принадлежит `probe_language_split.py`, а не модулю PPL-пробы, который едет
#: рядом в списке выше: у PPL-пробы своя запись в реестре редакций, и цитатой её
#: этот хеш не является. Имя пиннутого прибора названо прямо, потому что реестр
#: (`C-025`) относит хеш к прибору по ближайшей вверх ссылке на файл — без этой
#: строки ближайшей оказывалась строка списка модулей, и хеш прибора, законно не
#: входящего в реестр, читался цитатой чужой редакции, которой нет.
PROBE_TOOL_SHA = "99dafa8d9551caaa57b770d4cd321da6f0c09b28deb1885500034c9d62eeb276"
#: Обвязка сна — копиями того же стража, что у прогона сравнения и у полной стадии.
GUARD_SRC = SHARED / "sft-lr2e6-20260921-1453" / "guard"
GUARD_FILES = ("guard_cuda_run.sh", "check_gpu_sleep_guard.py")
#: Цепочка: тот же файл, что лежит в `tools/` кейса, — доставляется как `chain.sh`.
CHAIN_SRC = "tools/probe_v13_arm.sh"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def ssh(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def pkg_dir(ts: str) -> Path:
    return SHARED / f"{PKG_PREFIX}{ts}"


def build(ts: str, arms: str) -> dict:
    """Собрать пакет. Ничего не запускать: запуск — отдельным вызовом."""
    stage = pkg_dir(ts)
    (stage / "tools").mkdir(parents=True, exist_ok=True)
    (stage / "guard").mkdir(parents=True, exist_ok=True)
    shipped: dict = {}

    def ship(src: Path, dst: Path) -> dict:
        body = src.read_bytes()
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(body)
        if dst.read_bytes() != body:                       # NFS: сверка после записи
            raise SystemExit(f"ОТКАЗ: доставка не сошлась байт-в-байт: {dst}")
        if dst.suffix in (".sh", ".py"):
            dst.chmod(0o755)
        shipped[dst.name] = {"source": str(src), "delivered": str(dst),
                             "sha256": sha256_bytes(body)}
        return shipped[dst.name]

    # 1. Прибор вместе с модулями — в ту же раскладку `tools/`, что у пакета
    #    сравнения: `probe_control` ищет `ppl_probe` рядом с собой.
    for name in PROBE_TOOLS:
        src = CASE_ROOT / "tools" / name
        if not src.is_file():
            raise SystemExit(f"ОТКАЗ: нет модуля прибора {src}")
        ship(src, stage / "tools" / name)
    if shipped["probe_language_split.py"]["sha256"] != PROBE_TOOL_SHA:
        raise SystemExit("ОТКАЗ: прибор не совпал с пиннутым хешем — числа были бы "
                         "разностью приборов, а не состояний")

    # 2. Цепочка и обвязка сна.
    ship(CASE_ROOT / CHAIN_SRC, stage / "chain.sh")
    for name in GUARD_FILES:
        src = GUARD_SRC / name
        if not src.is_file():
            raise SystemExit(f"ОТКАЗ: нет стража сна {src} — обвязка без него не ставится")
        ship(src, stage / "guard" / name)

    # 3. Обёртка запуска: `ARMS` доводится до цепочки через окружение (обвязка
    #    передаёт окружение дальше, поэтому переменная видна внутри chain.sh).
    run_probe = (f"#!/usr/bin/env bash\n"
                 f"# S3ay-fix: проба руки — под стражем сна/устройства.\n"
                 f"# Устройство освобождает chain.sh (ждёт по факту процесса контейнера\n"
                 f"# стадии): страж обязан прочитать устройство ДО нагрузки, а до неё оно\n"
                 f"# занято стадией (AD-5).\n"
                 f"set -u\n"
                 f'STAGE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"\n'
                 f'ARMS={shlex.quote(arms)} exec bash "$STAGE/guard/guard_cuda_run.sh" \\\n'
                 f'  --out "$STAGE/guard.json" \\\n'
                 f'  --why "S3ay-fix: опора раннего гейта — рука v13+LR2e-6 (arms={arms}), '
                 f'wide/4096" \\\n'
                 f'  -- bash "$STAGE/chain.sh"\n')
    (stage / "run_probe.sh").write_text(run_probe, encoding="utf-8")
    (stage / "run_probe.sh").chmod(0o755)
    shipped["run_probe.sh"] = {"source": "(сгенерировано)",
                               "delivered": str(stage / "run_probe.sh"),
                               "sha256": sha256_file(stage / "run_probe.sh")}

    # 4. Команды запуска — на диск, а не в голову оператора.
    chain_cmd = f"bash {stage}/run_probe.sh"
    (stage / "chain_command.txt").write_text(chain_cmd + "\n", encoding="utf-8")
    launch_cmd = (f"ssh {STAND} 'tmux kill-session -t {PKG_PREFIX}{ts} 2>/dev/null; "
                  f"tmux new-session -d -s {PKG_PREFIX}{ts} \"setsid nohup {chain_cmd} "
                  f">> {stage}/chain.log 2>&1 < /dev/null\"'")
    (stage / "launch_command.txt").write_text(launch_cmd + "\n", encoding="utf-8")

    rec = {"tool": "tools/launch_arm_probe.py", "at": now(), "stage": str(stage),
           "arms": arms, "shipped": shipped, "chain_command": chain_cmd,
           "why": ("опора раннего гейта: проба короткой руки на той же площадке; сборка "
                   "инструментом, потому что собранная руками не дала ни одного отчёта, "
                   "а признаки готовности при этом появились"),
           "not_touched": ["источники чекпойнтов (копируются, читаются только)",
                           "остановленный каталог полной стадии",
                           "наборы, тензоры, карточки (AD-7)"]}
    (stage / "ship.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8")
    return rec


def launch(ts: str) -> dict:
    stage = pkg_dir(ts)
    cmd = (stage / "chain_command.txt").read_text(encoding="utf-8").strip()
    name = f"{PKG_PREFIX}{ts}"
    inner = (f"tmux kill-session -t {name} 2>/dev/null; "
             f"tmux new-session -d -s {name} \"setsid nohup {cmd} "
             f">> {stage}/chain.log 2>&1 < /dev/null\"")
    rc, out, err = ssh(inner, timeout=120)
    res = {"rc": rc, "stdout": out, "stderr": err, "launched_command": cmd,
           "stage": str(stage)}
    if rc == 0 or out == "":
        rc2, ses, _ = ssh(f"tmux ls 2>/dev/null | grep -c '^{name}:' || true", 60)
        res["tmux_session_present"] = ses.strip() not in ("", "0")
    return res


def facts(ts: str) -> dict:
    stage = pkg_dir(ts)
    f = stage / "CHAIN_DONE"
    out: dict = {"stage": str(stage), "chain_done": f.is_file(),
                 "reports": sorted(p.name for p in stage.glob("report_*.json")),
                 "checkpoints": sorted(p.name for p in (stage / "ckpts").glob("*.pt"))}
    if f.is_file():
        out["chain_done_content"] = f.read_text(encoding="utf-8").strip()
    log = stage / "chain.log"
    if log.is_file():
        out["chain_log_tail"] = log.read_text(encoding="utf-8", errors="replace")[-1200:]
    rc, ps, _ = ssh("docker ps --format '{{.Names}}' | grep v13arm2 || true", 60)
    out["probe_containers_running"] = [c for c in ps.splitlines() if c.strip()]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ts", required=True, help=f"метка; каталог — {PKG_PREFIX}<ts>")
    ap.add_argument("--arms", default="v13", choices=["v13", "both"],
                    help="v13 — только рука новой стадии (прямое сравнение); "
                         "both — ещё и повтор руки сравнения")
    ap.add_argument("--do", required=True, choices=["build", "launch", "facts"])
    a = ap.parse_args()
    if a.do == "build":
        rec = build(a.ts, a.arms)
        print(json.dumps({"stage": rec["stage"], "arms": rec["arms"],
                          "shipped": {k: v["sha256"][:12] for k, v in rec["shipped"].items()},
                          "chain_command": rec["chain_command"]},
                         ensure_ascii=False, indent=2))
    elif a.do == "launch":
        print(json.dumps(launch(a.ts), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(facts(a.ts), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
