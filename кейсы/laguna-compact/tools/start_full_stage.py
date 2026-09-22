#!/usr/bin/env python3
"""S3ay: стартер полной SFT-стадии — ждёт освобождения стенда и запускает цепочку.

**Зачем стартер, а не запуск руками.** Полная стадия занимает GB10 на ≈40 часов, и
AD-5 запрещает на ней вторую нагрузку. В момент сборки каталога стенд занят короткой
рукой темпа (`sft-v13lr2e6-*`, 500 шагов) и её пробой (`v13-arm-probe-*`), которые
идут одна за другой. Ждать их «на глазок» и запускать вручную — значит либо
столкнуться с рукой (две нагрузки делят память и меряют друг друга), либо
пропустить окно. Стартер ждёт по ФАКТАМ и запускает сам.

**Условие «стенд свободен» — три части, и каждая проверяется отдельно.**

1. **Опора раннего гейта готова.** Цепочка пробы руки дописала `CHAIN_DONE`. Одного
   «нет контейнеров» мало: между выходом контейнера руки и стартом её пробы есть
   окно, в котором стенд выглядит свободным, — и запуск в это окно дал бы ровно ту
   конкуренцию, от которой защищает AD-5. Готовый `CHAIN_DONE` закрывает окно и
   заодно гарантирует, что раннему гейту будет с чем сравнивать.
2. **Никакой чужой нагрузки.** `docker ps` не содержит ничего, кроме платформенных
   `llm-platform-*` (они живут на стенде постоянно и стадии не мешают).
3. **Память в бюджете стадии.** Свободной unified-памяти ≥ порога SFT (50 ГБ,
   ADR-008/ADR-011). Тот же порог проверит и страж AD-9 внутри цепочки — здесь он
   проверяется ДО запуска, чтобы не стартовать в отказ.

Плюс два предусловия, без которых запускать 40 часов нельзя вообще: каталог прогона
собран, и **страж темпа** (`check_stage_lr.py`) подтверждает, что объявленный пик
совпадает с исполняемым копией пайплайна.

**Отсоединённость.** Стартер сам живёт под `setsid nohup` (его запускает оператор
отсоединённо) и запускает цепочку на стенде через `tmux + setsid nohup`: цепочка
переживает и агента, и сессию, и обрыв ssh. Ожидание синхронно — гарантированная
гибель дельты (ADR-023 п.11): дельта закрывается фактом «стартер работает, pid N»,
а не ожиданием.

Коды возврата::

    0 — цепочка запущена (и сторож раннего гейта поднят)
    2 — NOT-STARTED: стенд не освободился за отведённое время, или не выполнено
        предусловие (каталог/темп) — стадия не запускалась
    3 — цепочка запущена, но сторож гейта не поднялся (см. вывод)

Запуск::

    setsid nohup python3 tools/start_full_stage.py --ts v13-2e6-20260921-1805 \
        --log /tmp/s3ay-starter.log > /dev/null 2>&1 &
    python3 tools/start_full_stage.py --ts v13-2e6-20260921-1805 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CASE_ROOT = Path(__file__).resolve().parent.parent
SHARED = Path("/home/user/gb10-shared")
STAND = "gb10-fast"
#: Порог свободной памяти стадии SFT (ADR-008/ADR-011) — тот же, что у стража AD-9.
SFT_MIN_FREE_GB = 50.0
#: Объявленный пик полной стадии — предусловие старта (проверяется стражем темпа).
EXPECT_PEAK_LR = 2e-6
#: Платформенные контейнеры стенда: они там живут постоянно и в счёт нагрузок AD-5
#: не идут (иначе «стенд свободен» не наступал бы никогда).
PLATFORM_PREFIX = "llm-platform-"
#: Цепочка пробы короткой руки: её `CHAIN_DONE` — условие готовности опоры гейта.
ARM_PROBE_DIR = SHARED / "v13-arm-probe2-20260921-1710"


def probe_chain_state() -> dict:
    """Готова ли опора раннего гейта — по СОДЕРЖИМОМУ `CHAIN_DONE`, а не по факту файла.

    Цепочка пробы руки S3ax (`v13-arm-probe-20260921-1905`) дописала
    `done new=1 prev=1`: обе пробы упали (`ModuleNotFoundError`, прибор лёг без
    `probe_control`), отчётов не появилось вовсе. Файл при этом был — и стартер
    счёл бы опору готовой, а гейт на 500-м шаге остался бы без опоры. Поэтому
    «готово» читается как `new=0 prev=0`; всё остальное называется вслух и
    опорой не считается (гейт тогда берёт опору из объявленного списка).
    """
    f = ARM_PROBE_DIR / "CHAIN_DONE"
    st: dict = {"dir": str(ARM_PROBE_DIR), "dir_exists": ARM_PROBE_DIR.is_dir(),
                "chain_done": f.is_file(), "content": None, "ok": False, "why": None}
    if not st["dir_exists"]:
        st["why"] = "каталога пакета пробы нет — опора берётся из объявленного списка"
        return st
    if not st["chain_done"]:
        st["why"] = "CHAIN_DONE нет — проба ещё идёт"
        return st
    try:
        st["content"] = f.read_text(encoding="utf-8").strip()
    except OSError as e:
        st["why"] = f"CHAIN_DONE не прочитан: {e}"
        return st
    codes = re.findall(r"=(-?\d+)", st["content"])
    st["return_codes"] = codes
    if len(codes) >= 2 and all(c == "0" for c in codes):
        st["ok"] = True
        st["why"] = "обе пробы цепочки завершились нулём — опора снята"
    else:
        st["why"] = ("CHAIN_DONE записан, но пробы НЕ завершились нулём "
                     f"(«{st['content']}») — опоры нет, отчётов не появится")
    return st


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{now()}] {msg}", flush=True)


def ssh(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", STAND, cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def containers() -> list[str]:
    rc, out, _ = ssh("docker ps --format '{{.Names}}'", 60)
    return [c for c in out.splitlines() if c.strip()] if rc == 0 else []


def foreign_loads() -> list[str]:
    return [c for c in containers() if not c.startswith(PLATFORM_PREFIX)]


def free_memory_gb() -> float | None:
    """Свободная память — из `/proc/meminfo`, а не из `free`.

    `free` на стенде печатает по локали («Память:»), и разбор по слову `Mem:`
    молча давал пустой ответ: порог «памяти ≥ 50 ГБ» не выполнялся бы Никогда, и
    стартер не запустил бы стадию вовсе — при том что выглядел бы работающим.
    `/proc/meminfo` — английский всегда, и `MemAvailable` это ровно та величина,
    которой пользуется страж AD-9.
    """
    rc, out, _ = ssh("awk '/^MemAvailable:/{print $2}' /proc/meminfo", 60)
    try:
        return int(out.splitlines()[0]) / 1048576.0      # kB → ГиБ
    except Exception:
        return None


def guard_lr(run_dir: Path) -> dict:
    p = subprocess.run([sys.executable, str(CASE_ROOT / "tools/check_stage_lr.py"),
                        "--run-dir", str(run_dir), "--expect-peak", repr(EXPECT_PEAK_LR),
                        "--json"], capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"passed": None, "verdict": "NOT-VERIFIED",
                "stdout_tail": p.stdout[-800:], "stderr_tail": p.stderr[-800:]}


class Starter:
    def __init__(self, run_dir: Path, a) -> None:
        self.run_dir = run_dir
        self.a = a
        self.state_path = run_dir / "starter_state.json"
        self.state: dict = {"tool": "tools/start_full_stage.py", "run_dir": str(run_dir),
                            "started_at": now(), "phase": "init", "events": []}

    def beat(self, phase: str, **kw) -> None:
        self.state.update({"phase": phase, "at": now(), **kw})
        self.state["events"].append({"phase": phase, "at": now(), **kw})
        try:
            self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
        except OSError as e:
            log(f"состояние не записано ({e}) — продолжаю: запись состояния не предусловие")
        log(f"{phase}: {kw if kw else ''}")

    def free_now(self) -> tuple[bool, dict]:
        probe = probe_chain_state()
        facts = {"foreign_loads": foreign_loads(),
                 "probe_chain": probe,
                 "probe_chain_done": probe["chain_done"],
                 "probe_chain_dir_exists": probe["dir_exists"],
                 "memory_gb": free_memory_gb()}
        #: Опора готова, если цепочка пробы дописала `CHAIN_DONE` УСПЕШНО (`new=0
        #: prev=0`). Файла нет, а каталога нет вовсе — это НАЗЫВАЕТСЯ и не блокирует
        #: (иначе стартер стал бы заложником чужого прогона, которого может и не
        #: быть): тогда опора гейта берётся из списка объявленных кандидатов — в
        #: вердикте будет видно, какая. А вот записанный ПРОВАЛ пробы ожидание не
        #: снимает: опоры не будет, и запускать полную стадию «в никуда» нельзя.
        ref_ok = probe["ok"] or not facts["probe_chain_dir_exists"]
        ok = (not facts["foreign_loads"] and ref_ok
              and facts["memory_gb"] is not None and facts["memory_gb"] >= SFT_MIN_FREE_GB)
        return ok, facts

    def wait_free(self) -> bool:
        stable = 0
        deadline = time.time() + self.a.wait_hours * 3600
        while time.time() < deadline:
            ok, facts = self.free_now()
            stable = stable + 1 if ok else 0
            self.beat("wait_free", stable_polls=stable, need=self.a.stable_polls, **facts)
            if stable >= self.a.stable_polls:
                return True
            time.sleep(self.a.poll_secs)
        self.beat("wait_timeout", waited_hours=self.a.wait_hours)
        return False

    def launch(self) -> dict:
        cmd = (self.run_dir / "chain_command.txt").read_text(encoding="utf-8").strip()
        inner = (f"tmux kill-session -t {self.run_dir.name} 2>/dev/null; "
                 f"tmux new-session -d -s {self.run_dir.name} \"setsid nohup {cmd} "
                 f">> {self.run_dir}/chain.log 2>&1 < /dev/null\"")
        rc, out, err = ssh(inner, timeout=120)
        res = {"rc": rc, "stdout": out, "stderr": err}
        if rc == 0 or out == "":
            # факт появления сессии — отдельной проверкой, а не по коду ssh
            rc2, ses, _ = ssh(f"tmux ls 2>/dev/null | grep -c '^{self.run_dir.name}:' || true", 60)
            res["tmux_session_present"] = ses.strip() not in ("", "0")
        return res

    def start_watch(self) -> dict:
        watch = self.run_dir / "early_gate_watch.py"
        logf = self.run_dir / "early_gate_watch.log"
        cmd = (f"setsid nohup {sys.executable} {watch} --run-dir {self.run_dir} "
               f">> {logf} 2>&1 < /dev/null & echo $!")
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        pid = p.stdout.strip()
        alive = False
        if pid.isdigit():
            time.sleep(3)
            alive = subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0
        return {"pid": pid, "alive": alive, "log": str(logf), "tool": str(watch)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ts", required=True, help="метка прогона; каталог — sft-<ts>")
    ap.add_argument("--wait-hours", type=float, default=8.0,
                    help="сколько ждать освобождения стенда; после — NOT-STARTED, "
                         "а не запуск в неизвестный момент")
    ap.add_argument("--stable-polls", type=int, default=3,
                    help="сколько проверок подряд стенд обязан быть свободен (окно "
                         "между контейнерами руки и её пробы)")
    ap.add_argument("--poll-secs", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true",
                    help="проверить предусловия и печатать вердикт, НЕ занимая стенд")
    a = ap.parse_args()

    run_dir = SHARED / f"sft-{a.ts}"
    st = Starter(run_dir, a)

    # ── предусловия, которые не зависят от занятости стенда ─────────────────
    missing = [str(p.name) for p in (run_dir / "chain_command.txt",
                                     run_dir / "early_gate.json",
                                     run_dir / "early_gate_watch.py",
                                     run_dir / "stop_stage_run.py") if not p.is_file()]
    if missing:
        print(json.dumps({"verdict": "NOT-STARTED", "why": "каталог прогона собран не "
                          "полностью — запускать нечего", "run_dir": str(run_dir),
                          "missing": missing}, ensure_ascii=False, indent=2))
        return 2

    lr = guard_lr(run_dir)
    if lr.get("passed") is not True:
        print(json.dumps({"verdict": "NOT-STARTED",
                          "why": "страж темпа не подтвердил объявленный пик — 40-часовой "
                                 "прогон не стартует на неподтверждённом темпе",
                          "stage_lr": lr}, ensure_ascii=False, indent=2))
        return 2

    ok, facts = st.free_now()
    if a.dry_run:
        print(json.dumps({"dry_run": True, "verdict": "WOULD-START" if ok else "WOULD-WAIT",
                          "run_dir": str(run_dir), "expect_peak_lr": EXPECT_PEAK_LR,
                          "stage_lr_guard": {k: lr.get(k) for k in
                                             ("passed", "verdict", "pipeline_sha256")},
                          "stand": facts,
                          "arm_probe_chain": str(ARM_PROBE_DIR),
                          "chain_command": str(run_dir / "chain_command.txt"),
                          "would_launch": (run_dir / "chain_command.txt").read_text().strip()[:400],
                          "watchdog": "tools/early_gate_watch.py (поднимется после запуска)"},
                         ensure_ascii=False, indent=2))
        return 0

    st.beat("preconditions_ok", expect_peak_lr=EXPECT_PEAK_LR,
            stage_lr_verdict=lr.get("verdict"),
            pipeline_sha256=lr.get("pipeline_sha256"))
    if not st.wait_free():
        print(json.dumps({"verdict": "NOT-STARTED",
                          "why": f"стенд не освободился за {a.wait_hours} ч",
                          "run_dir": str(run_dir), "last": st.state.get("events", [])[-1:]},
                         ensure_ascii=False, indent=2))
        return 2

    st.beat("stand_free", **st.free_now()[1])
    launched = st.launch()
    st.beat("launched", **launched)
    if not launched.get("tmux_session_present"):
        print(json.dumps({"verdict": "NOT-STARTED", "why": "сессии tmux на стенде не "
                          "появилось — цепочка не пошла", "launch": launched},
                         ensure_ascii=False, indent=2))
        return 2
    watch = st.start_watch()
    st.beat("watchdog", **watch)
    print(json.dumps({"verdict": "STARTED", "run_dir": str(run_dir),
                      "expect_peak_lr": EXPECT_PEAK_LR, "launch": launched,
                      "watchdog": watch, "starter_pid": os.getpid(),
                      "starter_state": str(st.state_path)},
                     ensure_ascii=False, indent=2))
    return 0 if watch.get("alive") else 3


if __name__ == "__main__":
    raise SystemExit(main())
