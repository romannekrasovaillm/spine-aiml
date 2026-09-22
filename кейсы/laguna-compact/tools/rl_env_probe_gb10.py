#!/usr/bin/env python3
"""Проба предусловий RL **на стенде GB10** (ADR-049 п.10): изоляция, детерминизм, цена, G2/G3.

**Зачем отдельный прибор, если есть `grounded_runner.py`.** Тот прогон снят на
локальной 4080 и по решению ADR-049 п.10 на стенд **не переносится**: другой хост,
другой docker (нативный, а не snap-конфайнмент), другая сеть, ARM, 20 ядер. Здесь
те же пять задач скелета прогоняются внутри контейнера стадии на `gb10-fast`, и
вместе с ними проверяется то, чего локальный прогон не покрывал:

1. **шаг RL как единица цены.** Локальный замер давал «цену среды на rollout», но не
   отвечал на вопрос «сколько стоит шаг». Здесь измеряются слагаемые **шага** и
   пересчитывается сам шаг на базе `evidence/s3-rl-probe.json` (123.87 с, 71.4 %
   генерация, 8 rollout'ов на шаг);
2. **лимит шагов.** G4 требует лимита шагов на rollout; локальный раннер его не
   проверял вовсе. Здесь бюджет шагов задаётся и проверяется **фактом состояния**:
   один и тот же эталонный скрипт при бюджете 0 даёт награду 0, при бюджете 1 — 1;
3. **таймаут как он есть.** Клиентский `subprocess`-таймаут `docker exec` не убивает
   процесс внутри контейнера — это проверяется отдельно (`process_survived_timeout`)
   и названо ограничением механизма, а не спрятано;
4. **закрытость сетевого профиля.** Локальный раннер для задачи с моком поднимал
   контейнер в дефолтной `bridge`-сети. Проба показывает, что такой профиль **не
   закрыт** (контейнер достаёт до интернета), и проверяет альтернативу — сеть
   `--internal` с моком на её шлюзе.

Сверх этого — усиленные пробы изоляции (сброс capabilities, лимиты pid/памяти/tmpfs
проверяются попыткой их нарушить, отсутствие docker-сокета) и состязательные пробы
G3: подмена вердикта изнутри песочницы (`forge`) и зашитое число вместо программы
(`cheat` — только для задачи с удержанным входом).

**Чего инструмент не делает.** Он не запускает модель: `run` — положительный
контроль (эталонное действие задачи), `no-action`/`empty-action`/`forge`/`cheat` —
пробы контура награды. Доля успехов скелета ничего не говорит о трудности задач для
политики (полоса p* по ADR-049 п.12 измеряется на реальных rollout).

**Границы постановки.** Скелет берётся из ветви `arch/laguna-control-arms` и
монтируется в пробу как есть (`--set-dir`); инструмент его только читает и сверяет
`sha256_full` с карточкой набора. Контейнеры и сети создаются с префиксом пробы
(`rlprobe-`) и снимаются по точному имени в `finally` — широких `pkill` нет.

Коды возврата::

    0 — все пробы дали ожидаемое, отчёт записан
    1 — красное: проба дала не то (задача не заземлена, среда не пропускает действия
        или изоляция не подтверждена там, где она обязана быть)
    2 — NOT-VERIFIED/blocked: нет docker, образа или набора; проба не состоялась

Пример::

    python3 tools/rl_env_probe_gb10.py --set-dir data/grounded-skeleton \
        --out evidence/rl-env-probe-gb10.json
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.dont_write_bytecode = True  # импорт мока не должен добавлять .pyc в набор
PROBE_TAG = "rlprobe"  # префикс имён контейнеров, сетей и каталогов пробы
IMAGE_DEFAULT = "alpine:3.20"
# База сравнения — измеренный шаг текстового RL (evidence/s3-rl-probe.json).
BASE_STEP_S = 123.87
GEN_SHARE = 0.714
ROLLOUTS_PER_STEP = 8  # tasks_per_step 4 × group_size 2 в том же замере
BASE_MODES = ("run", "no-action", "empty-action")
EXPECTED_SCORE = {"run": 1, "no-action": 0, "empty-action": 0, "forge": 0, "cheat": 0}
# Допущения аудита, которые проба НЕ измеряет (берутся как есть, ADR-049 п.10).
AUDIT = {
    "source": "evidence/grounding-audit.json → cost_estimate.scenarios.container_per_rollout",
    "actions_per_rollout": [2, 15],
    "generation_growth_s": [17.69, 35.38],
    "t_sandbox_per_rollout_s": [0.8, 3.0],
    "t_action_s": [0.1, 0.25],
    "ratio_to_base": [1.207, 1.722],
}


# ─── утилиты ─────────────────────────────────────────────────────────────────


# Подпроцессы (мок, верификаторы) импортируют модули набора: без этого они писали бы
# в набор `__pycache__`, и тождество редакции (sha256_full) менялось бы прогоном.
CHILD_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def sh(cmd: list[str], timeout: int = 300, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env if env is not None else CHILD_ENV)


def sha256_file(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree_digest(root: pathlib.Path) -> str:
    """Полный sha256 дерева: пути, типы, содержимое (симлинки — как ссылки)."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            h.update(f"L {rel} -> {os.readlink(p)}\n".encode())
        elif p.is_dir():
            h.update(f"D {rel}\n".encode())
        else:
            h.update(f"F {rel} {sha256_file(p)}\n".encode())
    return h.hexdigest()


def tree_paths(root: pathlib.Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def find_symlinks(root: pathlib.Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_symlink()]


def wipe_contents(d: pathlib.Path) -> None:
    """Очищает каталог, не удаляя его: он служит точкой bind-mount контейнера."""
    for child in d.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_contents(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target, follow_symlinks=False)


def mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def rnd(x: float | None, n: int = 4) -> float | None:
    return None if x is None else round(x, n)


def set_digest(set_dir: pathlib.Path) -> tuple[str, dict[str, str]]:
    """Канонический sha256 набора: карта {путь: sha256 файла}, `card.json` не входит.

    Тем же правилом считается `sha256_full` в карточке набора — расхождение
    означает, что проба шла не на той редакции скелета (ADR-012).
    """
    files = {
        p.relative_to(set_dir).as_posix(): sha256_file(p)
        for p in sorted(set_dir.rglob("*"))
        if p.is_file() and p.name != "card.json"
    }
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), files


def set_drift(set_dir: pathlib.Path, files: dict[str, str], card: dict) -> dict:
    """Расхождение набора с его карточкой: чем именно проба отличается от прежней.

    Карточка набора — носитель тождества редакции (AD-2): если дерево с ней
    разошлось, доказательство прежнего прогона относится к другой редакции, и это
    надо назвать. Семантику задач расхождение может и не менять (документация, кэш
    байткода) — решает архитектор, поэтому здесь перечисляются имена файлов.
    """
    ref = card.get("files") or {}
    absent = sorted(set(ref) - set(files))
    extra = sorted(set(files) - set(ref))
    changed = sorted(k for k in set(ref) & set(files) if ref[k] != files[k])
    return {
        "card_sha256_full": card.get("sha256_full"),
        "actual_sha256_full": None,  # заполняется вызывающим
        "matches": None,
        "files_in_card_missing_on_disk": absent,
        "files_on_disk_not_in_card": extra,
        "files_with_changed_hash": changed,
        "affects_task_semantics": bool(
            [k for k in absent + extra + changed if k.endswith(("task.json", "verify.py", "solution.sh", "mock_server.py"))]
        ),
    }


def docker_ok() -> tuple[bool, str]:
    try:
        r = sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"docker недоступен: {exc}"
    if r.returncode != 0:
        return False, f"docker вернул {r.returncode}: {r.stderr.strip()[:200]}"
    return True, r.stdout.strip()


def ensure_image(image: str) -> tuple[bool, str]:
    r = sh(["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"], timeout=60)
    if r.returncode == 0 and r.stdout.strip():
        return True, r.stdout.strip()
    r = sh(["docker", "pull", image], timeout=600)
    if r.returncode != 0:
        return False, f"образ {image} недоступен и не скачался: {r.stderr.strip()[:200]}"
    return ensure_image(image)


def free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# ─── песочница ───────────────────────────────────────────────────────────────


class Sandbox:
    """Один контейнер на батч задач одного сетевого профиля (ADR-049 п.10).

    Отличие от локального раннера: `--rm` и имя с префиксом пробы, учёт шагов
    (каждый `exec` — шаг среды), бюджет шагов и измерение времени снятия.
    """

    def __init__(self, image: str, workdir: pathlib.Path, profile: str, name: str,
                 network: str = "", mock_url: str = "", max_steps: int = 4) -> None:
        self.image = image
        self.workdir = workdir
        self.profile = profile          # none | bridge | internal
        self.name = name
        self.network = network
        self.mock_url = mock_url
        self.max_steps = max_steps
        self.start_s = 0.0
        self.stop_s = 0.0
        self.probes: dict = {}
        self.steps_used = 0
        self.steps_refused = 0
        self._started = False

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workdir, 0o777)
        cmd = [
            "docker", "run", "-d", "--rm", "--name", self.name,
            "--read-only", "--tmpfs", "/tmp:rw,size=16m",
            "--user", "1000:1000",
            "--memory", "256m", "--pids-limit", "256",
            "-v", f"{self.workdir}:/work", "-w", "/work",
        ]
        if self.profile == "none":
            cmd += ["--network", "none"]
        else:
            cmd += ["--network", self.network, "--add-host", "host.docker.internal:host-gateway"]
            if self.mock_url:
                cmd += ["-e", f"MOCK_BASE={self.mock_url}"]
        cmd += [self.image, "sleep", "7200"]
        t0 = time.perf_counter()
        r = sh(cmd, timeout=180)
        self.start_s = time.perf_counter() - t0
        if r.returncode != 0:
            raise RuntimeError(f"контейнер не поднялся: {r.stderr.strip()[:300]}")
        self._started = True

    def exec(self, command: str, timeout: int = 120, count_step: bool = True) -> dict:
        """Шаг среды: один вызов инструмента внутри песочницы.

        Бюджет шагов — свойство СРЕДЫ (это и проверяется): сверх него шаг не
        исполняется вовсе, поэтому последствие видно в состоянии, а не в тексте.
        """
        if count_step and self.steps_used >= self.max_steps:
            self.steps_refused += 1
            return {"refused": True, "returncode": None, "stdout": "", "stderr": "",
                    "reason": f"бюджет шагов исчерпан ({self.max_steps})", "elapsed_s": 0.0}
        if count_step:
            self.steps_used += 1
        cmd = ["docker", "exec", "-w", "/work"]
        if self.mock_url:
            cmd += ["-e", f"MOCK_BASE={self.mock_url}"]
        cmd += [self.name, "sh", "-c", command]
        t0 = time.perf_counter()
        try:
            r = sh(cmd, timeout=timeout)
            return {"refused": False, "returncode": r.returncode, "stdout": r.stdout,
                    "stderr": r.stderr[-400:], "elapsed_s": round(time.perf_counter() - t0, 4)}
        except subprocess.TimeoutExpired:
            return {"refused": False, "returncode": None, "stdout": "", "stderr": "",
                    "timed_out": True, "elapsed_s": round(time.perf_counter() - t0, 4)}

    def reset_steps(self) -> None:
        self.steps_used = 0
        self.steps_refused = 0

    def stop(self) -> None:
        if self._started:
            t0 = time.perf_counter()
            sh(["docker", "stop", "-t", "1", self.name], timeout=90)
            self.stop_s = round(time.perf_counter() - t0, 4)
            self._started = False

    # ── пробы изоляции: G4 доказывается фактами, а не флагами запуска ──
    def probe(self, journal_path: pathlib.Path | None = None,
              set_dir: pathlib.Path | None = None,
              holdout_dir: pathlib.Path | None = None,
              host_paths: list[str] | None = None) -> dict:
        e = lambda c, t=30: self.exec(c, timeout=t, count_step=False)  # noqa: E731 — проба, не шаг задачи
        p: dict = {}
        p["uid"] = e("id -u").get("stdout", "").strip()
        p["nonroot"] = p["uid"] not in ("", "0")
        p["rootfs_readonly"] = e("touch /probe-write 2>/dev/null && echo WRITABLE || echo READONLY").get("stdout", "").strip().endswith("READONLY")
        p["bind_mount_writable"] = e("touch /work/.probe-write && echo OK || echo FAIL").get("stdout", "").strip().endswith("OK")
        e("rm -f /work/.probe-write")
        # Сброс capabilities: с нулевым CapEff пространства имён внутри песочницы
        # недоступны так же, как на хосте — это и есть причина контейнерного пути.
        cap = e("grep CapEff /proc/self/status").get("stdout", "").strip()
        p["cap_eff"] = cap.split()[-1] if cap else ""
        p["cap_eff_zero"] = p["cap_eff"] in ("0000000000000000", "0", "")
        p["docker_socket_absent"] = e("test ! -e /var/run/docker.sock && echo ABSENT || echo PRESENT").get("stdout", "").strip().endswith("ABSENT")
        p["memory_max_bytes"] = e("cat /sys/fs/cgroup/memory.max 2>/dev/null").get("stdout", "").strip()
        p["pids_max"] = e("cat /sys/fs/cgroup/pids.max 2>/dev/null").get("stdout", "").strip()
        p["tmpfs_probe"] = self._tmpfs_probe()
        hidden = [
            e(f"test ! -e {path} && echo HIDDEN || echo VISIBLE").get("stdout", "").strip().endswith("HIDDEN")
            for path in (host_paths or [])
        ]
        p["host_paths_probed"] = list(host_paths or [])
        p["host_paths_hidden"] = all(hidden) if hidden else None
        if journal_path is not None:
            p["journal_invisible"] = e(f"test ! -e {journal_path} && echo ABSENT || echo PRESENT").get("stdout", "").strip().endswith("ABSENT")
        if set_dir is not None:
            p["verifier_files_invisible"] = e(f"test ! -e {set_dir} && echo ABSENT || echo PRESENT").get("stdout", "").strip().endswith("ABSENT")
        if holdout_dir is not None:
            p["holdout_invisible"] = e(f"test ! -e {holdout_dir} && echo ABSENT || echo PRESENT").get("stdout", "").strip().endswith("ABSENT")
        p.update(self._network_probe())
        self.probes = p
        return p

    def _tmpfs_probe(self) -> dict:
        """Лимит tmpfs проверяется записью сверх него, а не чтением опции запуска."""
        # Вывод берётся целиком: сообщение об отказе и итог `dd` — разные строки, и
        # `tail -1` оставил бы только итог, то есть «лимит не сработал» на сработавшем.
        r = self.exec("dd if=/dev/zero of=/tmp/fill bs=1M count=32 2>&1; rm -f /tmp/fill",
                      timeout=60, count_step=False)
        out = (r.get("stdout", "") + r.get("stderr", "")).lower()
        refused = "no space" in out or "left on device" in out or "error" in out
        return {"wrote_32m_into_16m_tmpfs": not refused, "limit_enforced": refused,
                "observed": (r.get("stdout", "") + r.get("stderr", "")).strip()[-240:]}

    def _network_probe(self) -> dict:
        """Сетевой профиль: чем именно ограничен доступ из песочницы."""
        e = lambda c, t=30: self.exec(c, timeout=t, count_step=False)  # noqa: E731
        p: dict = {"profile": self.profile}
        p["default_route"] = e("cat /proc/net/route | awk 'NR>1 && $2==\"00000000\" {print $1}'").get("stdout", "").strip()
        p["interfaces"] = [ln.split(":")[0].strip() for ln in e("cat /proc/net/dev").get("stdout", "").splitlines()[2:]]
        p["external_unreachable"] = "rc=0" not in e("wget -q -T 3 -O - http://1.1.1.1/ >/dev/null 2>&1; echo rc=$?").get("stdout", "")
        p["dns_unresolvable"] = "RESOLVES" not in e("nslookup example.com >/dev/null 2>&1 && echo RESOLVES || echo BLOCKED").get("stdout", "")
        if self.profile == "none":
            p["network_closed"] = p["external_unreachable"]
            return p
        if self.mock_url:
            r = e(f"wget -q -T 3 -O - {self.mock_url}/v1/health && echo MOCK_OK || echo MOCK_FAIL")
            p["mock_reachable"] = "MOCK_OK" in r.get("stdout", "")
        # Прочие сервисы хоста на шлюзе сети: их доступность — цена профиля.
        gw = self.network_gateway(self.network) if self.network else "172.17.0.1"
        ports = {}
        for port in (22, 80, 443, 3080, 8080):
            probe = e(f"(echo > /dev/tcp/{gw}/{port}) >/dev/null 2>&1 && echo OPEN || echo CLOSED")
            ports[str(port)] = "OPEN" if "OPEN" in probe.get("stdout", "") else "CLOSED"
        p["gateway"] = gw
        p["host_service_ports"] = ports
        p["other_host_services_reachable"] = [k for k, v in ports.items() if v == "OPEN"]
        p["network_closed"] = bool(p["external_unreachable"] and p["dns_unresolvable"]
                                   and not p["other_host_services_reachable"])
        return p

    @staticmethod
    def network_gateway(network: str) -> str:
        if not network:
            return "172.17.0.1"
        r = sh(["docker", "network", "inspect", network,
                "--format", "{{(index .IPAM.Config 0).Gateway}}"], timeout=30)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "172.17.0.1"

    def isolation_ok(self) -> tuple[bool, list[str]]:
        """Изоляция подтверждена, только если каждая обязательная проба дала ожидаемое."""
        want = {
            "nonroot": True,
            "rootfs_readonly": True,
            "bind_mount_writable": True,
            "cap_eff_zero": True,
            "docker_socket_absent": True,
        }
        if self.probes.get("host_paths_hidden") is not None:
            want["host_paths_hidden"] = True
        want["network_closed" if self.profile == "none" else "mock_reachable"] = True
        bad = [k for k, v in want.items() if self.probes.get(k) is not v]
        return (not bad), bad


# ─── мок ─────────────────────────────────────────────────────────────────────


class MockServer:
    """HTTP-мок на хосте: журнал пишет сервер, в контейнер не монтируется."""

    def __init__(self, set_dir: pathlib.Path, run_dir: pathlib.Path, seed: int, host: str) -> None:
        self.set_dir = set_dir
        self.host = host
        self.seed = seed
        run_dir.mkdir(parents=True, exist_ok=True)
        self.journal = run_dir / "mock-journal.jsonl"
        self.ready = run_dir / "mock-ready"
        self.module = set_dir / "mock" / "mock_server.py"
        self.port = free_port("127.0.0.1")
        self.url = f"http://{host}:{self.port}"
        # Токен ротации журнала знает только прибор: в контейнер он не передаётся,
        # поэтому админ-путь мока агенту недоступен.
        self.admin_token = secrets.token_hex(16)
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.journal.touch()
        for _ in range(3):
            self.proc = subprocess.Popen(
                [sys.executable, str(self.module), "--host", self.host, "--port", str(self.port),
                 "--seed", str(self.seed), "--journal", str(self.journal),
                 "--ready-file", str(self.ready), "--admin-token", self.admin_token],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(80):
                if self.ready.is_file():
                    return
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
            if self.proc.poll() is None:
                self.stop()
            try:
                self.port = free_port(self.host)
            except OSError:
                self.port = free_port("127.0.0.1")
            self.url = f"http://{self.host}:{self.port}"
        raise RuntimeError("мок не поднялся")

    @staticmethod
    def _opener():
        import urllib.request

        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def query(self, path: str, timeout: int = 10) -> str:
        # С хоста мок доступен по тому же адресу, что и из песочницы (шлюз сети).
        with self._opener().open(f"http://{self.host}:{self.port}{path}", timeout=timeout) as resp:
            return resp.read().decode("utf-8")

    def rotate(self) -> None:
        """Откат состояния среды: журнал обнуляется СЕРВЕРОМ между прогонами."""
        import urllib.request

        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/__admin/rotate", method="POST",
            headers={"X-Admin-Token": self.admin_token},
        )
        with self._opener().open(req, timeout=10) as resp:
            resp.read()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


# ─── задачи ──────────────────────────────────────────────────────────────────


class Task:
    def __init__(self, path: pathlib.Path) -> None:
        self.dir = path
        self.spec = json.loads((path / "task.json").read_text(encoding="utf-8"))
        self.id: str = self.spec["id"]
        self.seed = path / self.spec["seed_dir"]
        self.solution = path / self.spec["solution"]
        self.verifier = path / self.spec["verifier"]
        holdout = self.spec.get("holdout_dir")
        self.holdout = (path / holdout) if holdout else None
        self.network: str = self.spec.get("network", "none")
        self.timeout_s: int = int(self.spec.get("timeout_s", 60))


def load_tasks(set_dir: pathlib.Path, only: list[str] | None) -> list[Task]:
    tasks = [
        Task(d) for d in sorted((set_dir / "tasks").iterdir())
        if d.is_dir() and (d / "task.json").is_file()
    ]
    if only:
        tasks = [t for t in tasks if t.id in set(only)]
    return tasks


def modes_for(task: Task) -> tuple[str, ...]:
    """Набор проб задачи: базовые + состязательные (cheat — только с удержанным входом)."""
    return BASE_MODES + ("forge",) + (("cheat",) if task.holdout is not None else ())


# ─── верификация ─────────────────────────────────────────────────────────────


def run_verifier(task: Task, snapshot: pathlib.Path, args, mock: "MockServer | None",
                 scratch: pathlib.Path) -> dict:
    cmd = [
        sys.executable, str(task.verifier),
        "--snapshot", str(snapshot),
        "--task", str(task.dir / "task.json"),
        "--seed", str(task.seed),
        "--image", args.image,
        "--scratch", str(scratch),
    ]
    if task.holdout is not None:
        cmd += ["--holdout", str(task.holdout)]
    # Аргументы мока получает только задача, которая с ним работает: остальным
    # верификаторам они незнакомы (и не должны быть — иначе мок стал бы общим
    # каналом наблюдения для незаземлённых задач).
    if mock is not None and task.network != "none":
        cmd += ["--journal", str(mock.journal), "--mock-module", str(mock.module),
                "--mock-seed", str(mock.seed)]
    t0 = time.perf_counter()
    limit = max(180, task.timeout_s * 4)
    try:
        r = sh(cmd, timeout=limit)
    except subprocess.TimeoutExpired:
        return {"score": None, "reason": f"верификатор не уложился в {limit} с",
                "reads": [], "verify_s": round(time.perf_counter() - t0, 4)}
    dt = round(time.perf_counter() - t0, 4)
    if r.returncode != 0:
        tail = [ln for ln in r.stderr.strip().splitlines() if ln.strip()]
        return {"score": None, "reason": f"верификатор упал ({r.returncode}): {tail[-1][:300] if tail else ''}",
                "reads": [], "verify_s": dt}
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001 — нечитаемый ответ верификатора это отказ
        return {"score": None, "reason": f"ответ верификатора не разобран: {exc}", "reads": [], "verify_s": dt}
    out["verify_s"] = dt
    return out


def empty_snapshot(run_dir: pathlib.Path, name: str) -> pathlib.Path:
    d = run_dir / "empty" / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


# ─── состязательные действия ─────────────────────────────────────────────────


def visible_total(task: Task) -> int:
    total = 0
    for csv in sorted((task.seed / "data").glob("*.csv")):
        for n, line in enumerate(csv.read_text(encoding="utf-8").splitlines()):
            if n and line.strip():
                total += int(line.split(",")[1])
    return total


def cheat_action(task: Task) -> str:
    """Состязательное действие «зашитое число»: ответ верен на ВИДИМЫХ данных.

    Приём execution-grounded-benchmark-gen: эталон строится исполнением, поэтому
    программа, которая знает ответ, но не умеет его считать, обязана провалиться
    на удержанном входе. Здесь — перезапись агрегатора на константу.
    """
    total = visible_total(task)
    return (
        "cat > bin/aggregate.sh <<'CHEAT'\n"
        "#!/bin/sh\n"
        "mkdir -p out\n"
        f"echo {total} > out/aggregate.txt\n"
        "CHEAT\n"
        f"mkdir -p out && echo {total} > out/aggregate.txt"
    )


def forge_action(expected_public: str) -> str:
    """Подмена вердикта изнутри песочницы: forged-файлы на стороне проверки.

    Пишем в /work то, что выглядит как источник верификатора (его скрипт,
    «ожидаемое» значение, журнал мока) и текстовый ответ с точным ожидаемым
    значением. Файл-результат задачи НЕ пишем: подделка проверки не должна
    подменять собой работу. Вердикт обязан остаться нулевым.
    """
    payload = expected_public.replace("'", "'\\''")
    return (
        "mkdir -p out\n"
        f"printf '%s\\n' '{payload}' > ANSWER.txt\n"
        f"printf '%s\\n' '{payload}' > expected.json\n"
        "printf '%s\\n' '{\"score\": 1, \"reason\": \"forged\"}' > verify.json\n"
        "printf '%s\\n' '{\"accepted\": true, \"seq\": 1, \"body\": {\"items_count\": 10}}' > mock-journal.jsonl\n"
        "printf '%s\\n' '{\"score\": 1}' > out/verify.py\n"
    )


# ─── один rollout ────────────────────────────────────────────────────────────


def rollout(task: Task, mode: str, sandbox: Sandbox, run_dir: pathlib.Path, args,
            mock: "MockServer | None", prev_paths: set[str],
            timeout_override: int | None = None, step_budget: int | None = None) -> dict:
    """Прогон задачи: откат состояния → шаги действия (или их отсутствие) → верификация."""
    rd = run_dir / "rollouts" / task.id / mode
    shutil.rmtree(rd, ignore_errors=True)
    (rd / "snapshot").mkdir(parents=True)
    transcript = rd / "transcript.txt"
    scratch = run_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    sandbox.reset_steps()
    saved_budget = None
    if step_budget is not None:
        saved_budget, sandbox.max_steps = sandbox.max_steps, step_budget

    rec: dict = {"mode": mode, "task": task.id, "tool": task.spec["tool"], "network": task.network,
                 "profile": sandbox.profile, "container": sandbox.name,
                 "container_start_s": sandbox.start_s, "step_budget": sandbox.max_steps}

    # 1. Откат состояния: каталог-точка монтирования остаётся, содержимое — из seed.
    t0 = time.perf_counter()
    wipe_contents(sandbox.workdir)
    copy_contents(task.seed, sandbox.workdir)
    rec["wipe_seed_s"] = round(time.perf_counter() - t0, 4)
    rec["seed_digest"] = tree_digest(sandbox.workdir)
    rec["pristine_digest"] = tree_digest(task.seed)
    rec["seed_matches_pristine"] = rec["seed_digest"] == rec["pristine_digest"]
    seed_paths = tree_paths(task.seed)
    rec["residue_from_previous"] = sorted(
        p for p in prev_paths if p not in seed_paths and (sandbox.workdir / p).exists()
    )
    # Откат состояния среды целиком: у задачи с моком состояние живёт не только в
    # /work, но и в журнале сервера — он тоже возвращается к исходному.
    if mock is not None and task.network != "none":
        mock.rotate()
        rec["journal_rotated"] = True

    # Ожидаемое значение спрашивается у верификатора ДО действия: оно нужно, чтобы
    # состязательный текст пробы называл точное значение, а не «что-то похожее».
    pre = run_verifier(task, empty_snapshot(run_dir, task.id), args, mock, scratch)
    expected_public = str(pre.get("expected_public", ""))

    # 2. Шаги действия (или их отсутствие).
    t0 = time.perf_counter()
    step_records: list[dict] = []
    tmo = timeout_override or task.timeout_s
    if mode == "run":
        transcript.write_text("(действие выполнено инструментами; текстового ответа нет)\n", encoding="utf-8")
        body = task.solution.read_text(encoding="utf-8")
        step_records.append(sandbox.exec("sh -s <<'GS_SOLUTION_EOF'\n" + body + "\nGS_SOLUTION_EOF", timeout=tmo))
    elif mode == "cheat":
        transcript.write_text(f"Готово. {expected_public}\n", encoding="utf-8")
        step_records.append(sandbox.exec(cheat_action(task), timeout=tmo))
    elif mode == "forge":
        transcript.write_text(f"Готово. {expected_public}\n", encoding="utf-8")
        step_records.append(sandbox.exec(forge_action(expected_public), timeout=tmo))
    elif mode == "empty-action":
        transcript.write_text(f"Готово. {expected_public}\n", encoding="utf-8")
        step_records.append(sandbox.exec("true", timeout=tmo))
    elif mode == "timeout":
        # Шаг заведомо длиннее таймаута задачи: среда обязана его прервать.
        transcript.write_text("(действие превысило таймаут)\n", encoding="utf-8")
        step_records.append(sandbox.exec("mkdir -p out; sleep 120; echo LATE > out/late.txt", timeout=tmo))
    elif mode == "no-action":
        # Состязательный текстовый ответ: называет ТОЧНОЕ ожидаемое значение словами
        # и не делает ни одного вызова. Проба проверяет не «нет файла», а «вердикт не
        # зависит от текста» — иначе она была бы вакуумной.
        transcript.write_text(f"Готово. {expected_public}\n", encoding="utf-8")
    else:
        raise ValueError(mode)
    rec["action_s"] = round(time.perf_counter() - t0, 4)
    rec["steps_used"] = sandbox.steps_used
    rec["steps_refused"] = sandbox.steps_refused
    rec["step_records"] = [
        {k: v for k, v in s.items() if k in ("returncode", "refused", "timed_out", "elapsed_s", "reason")}
        for s in step_records
    ]
    rec["action_exit_code"] = step_records[0].get("returncode") if step_records else None
    rec["action_timed_out"] = any(s.get("timed_out") for s in step_records)
    rec["action_refused_by_step_budget"] = any(s.get("refused") for s in step_records)
    err = next((s.get("stderr", "") for s in step_records if s.get("stderr")), "")
    if err:
        rec["action_stderr_tail"] = err[-300:]
    text = transcript.read_text(encoding="utf-8")
    rec["transcript_sha256"] = sha256_file(transcript)
    rec["expected_public"] = expected_public
    rec["text_answer_carried_expected"] = bool(expected_public) and expected_public in text

    # 3. Снимок состояния (снаружи контейнера; симлинки — попытка выхода).
    t0 = time.perf_counter()
    copy_contents(sandbox.workdir, rd / "snapshot")
    rec["snapshot_s"] = round(time.perf_counter() - t0, 4)
    links = find_symlinks(rd / "snapshot")
    rec["symlink_escape"] = links
    rec["state_digest"] = tree_digest(rd / "snapshot")
    rec["new_paths"] = sorted(tree_paths(rd / "snapshot") - seed_paths)

    # 4. Верификация: дважды (детерминизм) и третий раз с удалённым транскриптом —
    #    доказательство, что текст ответа в вердикт не входит.
    first = run_verifier(task, rd / "snapshot", args, mock, scratch)
    rec["verify_s"] = first.get("verify_s")
    second = run_verifier(task, rd / "snapshot", args, mock, scratch)
    saved = transcript.read_bytes()
    transcript.unlink()
    without_text = run_verifier(task, rd / "snapshot", args, mock, scratch)
    transcript.write_bytes(saved)

    rec["score"] = first.get("score")
    rec["reason"] = first.get("reason")
    rec["reads"] = first.get("reads", [])
    rec["detail"] = first.get("detail", {})
    rec["verifier_deterministic"] = first.get("score") is not None and first.get("score") == second.get("score")
    rec["score_without_transcript"] = without_text.get("score")
    rec["verifier_ignores_text"] = first.get("score") is not None and first.get("score") == without_text.get("score")
    rec["total_s"] = round(rec["wipe_seed_s"] + rec["action_s"] + rec["snapshot_s"]
                           + (rec["verify_s"] or 0) + (second.get("verify_s") or 0)
                           + (without_text.get("verify_s") or 0), 4)
    if saved_budget is not None:
        sandbox.max_steps = saved_budget
    return rec


# ─── батч ────────────────────────────────────────────────────────────────────


def container_limits_probe(image: str, name: str) -> dict:
    """Пробы лимитов в одноразовых контейнерах: лимит надо НАРУШИТЬ, а не прочитать.

    Читать `pids.max` из cgroup мало — это объявление. Здесь лимит проверяется
    попыткой его превысить: форк сверх pids-limit и рост памяти сверх memory.
    """
    out: dict = {}
    # Форк-петля идёт в подоболочке: она упирается в pids-limit и печатает отказ,
    # а родитель остаётся жив и считает процессы ПРИМОГОННО (glob + `set --` — это
    # builtins busybox, им форк не нужен; внешние `ls | grep` в лимите не работают).
    r = sh(["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,size=16m", "--user", "1000:1000",
            "--memory", "256m", "--memory-swap", "256m", "--pids-limit", "32",
            image, "sh", "-c",
            "( i=0; while [ $i -lt 500 ]; do sleep 30 & i=$((i+1)); done ) 2>/tmp/forkerr; "
            "set -- /proc/[0-9]*; echo procs=$#; echo forkerr=$(head -c 60 /tmp/forkerr)"],
           timeout=120)
    m = re.search(r"procs=([0-9]+)", r.stdout)
    out["pids_limit_declared"] = 32
    out["pids_limit_observed"] = r.stdout.strip()[:200]
    out["pids_processes_at_limit"] = int(m.group(1)) if m else None
    out["pids_fork_errors"] = "can't fork" in r.stdout
    out["pids_limit_enforced"] = bool(m and int(m.group(1)) <= 33 and out["pids_fork_errors"])
    # Память: удвоение строки быстро упирается в 256 МиБ, и процесс убивает ядро.
    # Код возврата снимается у самого awk (`echo rc=$?`), а не у конвейера.
    r = sh(["docker", "run", "--rm", "--name", name + "-mem", "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,size=16m", "--user", "1000:1000",
            "--memory", "256m", "--memory-swap", "256m", "--pids-limit", "64",
            image, "sh", "-c",
            "awk 'BEGIN{ a=\"x\"; while (length(a) < 400000000) a = a a; print length(a) }' 2>&1; "
            "echo awk_rc=$?"], timeout=180)
    tail = r.stdout.strip()
    rc = re.search(r"awk_rc=([0-9]+)", tail)
    out["memory_limit_declared_bytes"] = 256 * 1024 * 1024
    out["memory_limit_observed"] = tail[-200:]
    out["memory_awk_rc"] = int(rc.group(1)) if rc else None
    out["memory_limit_enforced"] = bool(rc and int(rc.group(1)) in (137, 139))
    return out


def lifecycle_probe(image: str, name: str, workdir: pathlib.Path) -> dict:
    """Снятие контейнера стоит почти целиком grace-периода `docker stop`.

    Проверяется двумя одноразовыми контейнерами с теми же флагами: `-t 1` (как в
    батче) и `-t 0` (сразу SIGKILL). Это цена, которую платит каждый батч, то есть
    на шаг RL она ложится целиком — знать её стоит точно, а не «плюс-минус секунда».
    """
    out: dict = {}
    for grace in (1, 0):
        tag = f"{name}-t{grace}"
        work = workdir / f"lifecycle-t{grace}"
        work.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        r = sh(["docker", "run", "-d", "--rm", "--name", tag, "--network", "none",
                "--read-only", "--tmpfs", "/tmp:rw,size=16m", "--user", "1000:1000",
                "--memory", "256m", "--pids-limit", "256",
                "-v", f"{work}:/work", "-w", "/work", image, "sleep", "7200"], timeout=120)
        start = time.perf_counter() - t0
        if r.returncode != 0:
            out[f"t{grace}"] = {"error": r.stderr.strip()[:200]}
            continue
        t0 = time.perf_counter()
        sh(["docker", "stop", "-t", str(grace), tag], timeout=90)
        stop = time.perf_counter() - t0
        sh(["docker", "rm", "-f", tag], timeout=60)  # на случай, если --rm не сработал
        out[f"t{grace}"] = {"start_s": round(start, 4), "stop_s": round(stop, 4)}
    out["reading"] = (
        "снятие батч-контейнера почти целиком состоит из grace-периода `docker stop`: "
        + (f"-t 1 стоит {out.get('t1', {}).get('stop_s')} с, -t 0 — {out.get('t0', {}).get('stop_s')} с"
           if out.get("t1", {}).get("stop_s") and out.get("t0", {}).get("stop_s") else "замер неполон")
        + "; цена ложится на каждый батч, то есть на шаг RL целиком"
    )
    return out


def exec_cost_probe(sandbox: Sandbox, n: int = 20) -> dict:
    """Цена одного шага среды (`docker exec`) и его распараллеливание по батчу."""
    serial = []
    for _ in range(n):
        r = sandbox.exec("true", timeout=30, count_step=False)
        serial.append(r["elapsed_s"])
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: sandbox.exec("true", timeout=30, count_step=False), range(8)))
    parallel8 = round(time.perf_counter() - t0, 4)
    return {
        "n": n,
        "exec_overhead_s": mean(serial),
        "exec_overhead_min_s": min(serial) if serial else None,
        "exec_overhead_max_s": max(serial) if serial else None,
        "samples": serial,
        "exec_parallel8_wall_s": parallel8,
        "exec_parallel8_per_step_s": rnd(parallel8 / 8),
        "parallelism_note": "8 шагов, запущенных одновременно в ОДНОМ контейнере; "
                            "перекрытие шагов РАЗНЫХ rollout'ов не измерялось",
    }


def run_batch(tasks: list[Task], profile: str, args, run_dir: pathlib.Path,
              mock: "MockServer | None", pass_idx: int, reps: int,
              network: str = "", mock_url: str = "") -> dict:
    """Один батч задач в одном контейнере: пробы изоляции → rollout'ы → снятие."""
    name = f"{PROBE_TAG}-{run_dir.name}-p{profile}{pass_idx}"
    sb = Sandbox(args.image, run_dir / f"work-{profile}-{pass_idx}", profile, name,
                 network=network, mock_url=mock_url, max_steps=args.max_steps)
    t_start = time.perf_counter()
    sb.start()
    sb.probe(journal_path=(mock.journal if (mock and profile != "none") else None),
             set_dir=pathlib.Path(args.set_dir).resolve(),
             holdout_dir=next((t.holdout for t in tasks if t.holdout), None),
             host_paths=[str(pathlib.Path.home()), str(pathlib.Path(args.set_dir).resolve())])
    isolated, bad = sb.isolation_ok()
    info: dict = {
        "profile": profile, "container": name, "pass": pass_idx, "reps": reps,
        "container_start_s": round(sb.start_s, 4),
        "isolation_probes": sb.probes,
        "isolation_ok": isolated,
        "isolation_failed_probes": bad,
        "network": {"profile": profile, "docker_network": network, "mock_url": mock_url},
        "mock_seed": mock.seed if mock else None,
    }
    if not isolated:
        info["aborted"] = f"изоляция не подтверждена ({', '.join(bad)}) — прогон батча остановлен"
        sb.stop()
        info["container_stop_s"] = sb.stop_s
        return info

    prev_paths: set[str] = set()
    results: list[dict] = []
    t_rollouts = time.perf_counter()
    for t in tasks:
        for mode in modes_for(t):
            for rep in range(reps):
                rec = rollout(t, mode, sb, run_dir, args, mock, prev_paths)
                rec["rep"] = rep
                prev_paths |= set(rec["new_paths"])
                results.append(rec)
    info["rollouts_s"] = round(time.perf_counter() - t_rollouts, 4)
    info["rollouts"] = results
    info["exec_cost"] = exec_cost_probe(sb)
    info["batch_wall_s"] = round(time.perf_counter() - t_start, 4)
    sb.stop()
    info["container_stop_s"] = sb.stop_s
    return info


def step_limit_probes(task: Task, args, run_dir: pathlib.Path) -> dict:
    """Лимит шагов: один и тот же эталонный скрипт при разном бюджете.

    Проверяется не «счётчик вырос», а последствие: при бюджете 0 работа не
    выполнена и награда нулевая, при бюджете 1 — выполнена и награда единичная.
    Это и есть требование G4 «лимит шагов применяется».
    """
    out: dict = {"task": task.id, "budget_probe": {}}
    name = f"{PROBE_TAG}-{run_dir.name}-steplimit"
    sb = Sandbox(args.image, run_dir / "work-steplimit", "none", name, max_steps=0)
    sb.start()
    try:
        for budget in (0, 1, 2):
            rec = rollout(task, "run", sb, run_dir / "steplimit", args, None, set(),
                          step_budget=budget)
            out["budget_probe"][str(budget)] = {
                "steps_used": rec["steps_used"],
                "steps_refused": rec["steps_refused"],
                "action_refused_by_step_budget": rec["action_refused_by_step_budget"],
                "seed_matches_pristine": rec["seed_matches_pristine"],
                "state_changed": rec["state_digest"] != rec["seed_digest"],
                "score": rec["score"],
                "reason": rec["reason"],
            }
    finally:
        sb.stop()
    got = {b: v["score"] for b, v in out["budget_probe"].items()}
    out["binds_on_state"] = got.get("0") == 0 and got.get("1") == 1
    out["reading"] = (
        "бюджет 0: эталонное действие не исполнено (состояние не изменилось, награда 0); "
        "бюджет 1: исполнено, награда 1 — лимит шагов управляет наградой через состояние"
        if out["binds_on_state"] else "лимит шагов НЕ управляет наградой — проба красная"
    )
    return out


def timeout_probes(task: Task, args, run_dir: pathlib.Path) -> dict:
    """Таймаут: применяется ли он и что происходит с процессом внутри контейнера."""
    out: dict = {"task": task.id, "timeout_s": 3}
    name = f"{PROBE_TAG}-{run_dir.name}-timeout"
    sb = Sandbox(args.image, run_dir / "work-timeout", "none", name, max_steps=4)
    sb.start()
    try:
        rec = rollout(task, "timeout", sb, run_dir / "timeout", args, None, set(),
                      timeout_override=3)
        out["rollout"] = {k: rec[k] for k in ("action_s", "action_timed_out", "steps_used",
                                              "state_digest", "seed_digest", "score", "reason")}
        out["timeout_applied"] = bool(rec["action_timed_out"]) and rec["action_s"] < 30
        out["state_unchanged"] = rec["state_digest"] == rec["seed_digest"]
        out["partial_state_left_by_killed_step"] = not out["state_unchanged"]
        # Клиентский таймаут `docker exec` не убивает процесс внутри: это надо назвать.
        # Считается именно `sleep 120` из прерванного шага — главный процесс контейнера
        # тоже `sleep` (7200) и в наивном счёте дал бы ложное «выжил».
        left = sb.exec("for p in /proc/[0-9]*; do [ \"$(cat $p/comm 2>/dev/null)\" = sleep ] && "
                       "tr '\\0' ' ' < $p/cmdline 2>/dev/null | grep -q '120' && echo FOUND; done | wc -l",
                       timeout=30, count_step=False)
        out["sleep_processes_after_timeout"] = int((left.get("stdout") or "0").strip() or 0)
        out["process_survived_timeout"] = out["sleep_processes_after_timeout"] > 0
        alive = sb.exec("echo ALIVE", timeout=30, count_step=False)
        out["container_usable_after_timeout"] = "ALIVE" in (alive.get("stdout") or "")
        if out["process_survived_timeout"]:
            sb.exec("for p in /proc/[0-9]*; do [ \"$(cat $p/comm 2>/dev/null)\" = sleep ] && "
                    "tr '\\0' ' ' < $p/cmdline 2>/dev/null | grep -q '120' && kill -9 ${p#/proc/}; done",
                    timeout=30, count_step=False)
        out["reading"] = (
            "таймаут применяется (шаг прерван, награда читается из состояния); "
            + ("состояние НЕ вернулось к исходному: прерванный шаг оставил частичный след"
               if out["partial_state_left_by_killed_step"] else "состояние не изменилось")
            + "; "
            + ("процесс внутри контейнера переживает клиентский таймаут — среда обязана "
               "добивать его сама (kill по pid внутри контейнера), иначе шаги копятся"
               if out["process_survived_timeout"] else
               "процесс внутри контейнера снят вместе с шагом")
        )
    finally:
        sb.stop()
    return out


def network_profile_comparison(args, run_dir: pathlib.Path) -> dict:
    """Чем закрытый профиль отличается от дефолтного `bridge`.

    Дефолтная bridge-сеть выпускает контейнер в интернет (проверяется фактом);
    `--internal` — нет, а мок остаётся доступен через шлюз сети, потому что мок
    bind'ится именно на адрес шлюза.
    """
    out: dict = {"probed": ["external http://1.1.1.1/", "dns example.com",
                            "host services on gateway", "interfaces"]}
    internal_net = f"{PROBE_TAG}-cmp-{run_dir.name}"
    sh(["docker", "network", "rm", internal_net], timeout=60)
    created = sh(["docker", "network", "create", "--internal", internal_net], timeout=60).returncode == 0
    try:
        for profile, net in (("bridge", "bridge"), ("internal", internal_net)):
            if not net:
                continue
            name = f"{PROBE_TAG}-{run_dir.name}-cmp-{profile}"
            sb = Sandbox(args.image, run_dir / f"netcmp-{profile}", profile, name,
                         network=net, mock_url="", max_steps=0)
            sb.start()
            try:
                p = sb._network_probe()  # noqa: SLF001 — проба профиля, не задача
                out[profile] = {"docker_network": net, "gateway": p.get("gateway"),
                                "external_unreachable": p.get("external_unreachable"),
                                "dns_unresolvable": p.get("dns_unresolvable"),
                                "interfaces": p.get("interfaces"),
                                "host_service_ports": p.get("host_service_ports"),
                                "network_closed": p.get("network_closed")}
            finally:
                sb.stop()
    finally:
        if created:
            sh(["docker", "network", "rm", internal_net], timeout=60)
    br = out.get("bridge", {})
    intn = out.get("internal", {})
    out["reading"] = (
        "дефолтная bridge-сеть НЕ закрыта: egress в интернет открыт "
        f"(external_unreachable={br.get('external_unreachable')}) — для задачи с моком это лишний "
        "канал наблюдения и действий; профиль `--internal` закрывает egress "
        f"(external_unreachable={intn.get('external_unreachable')}, "
        f"dns_unresolvable={intn.get('dns_unresolvable')}) и оставляет доступным мок на шлюзе своей сети"
    )
    return out


# ─── разбор ──────────────────────────────────────────────────────────────────


READS_TEXT_PREFIXES = ("text:", "transcript:")


def gates_for_task(task: Task, by_mode: dict[str, dict], isolation_ok: bool,
                   residue_absent: bool) -> dict:
    run, no_action, empty = by_mode.get("run"), by_mode.get("no-action"), by_mode.get("empty-action")
    base = run or no_action or empty or {}
    reads = base.get("reads", [])
    reads_text = [r for r in reads if r.startswith(READS_TEXT_PREFIXES)]
    forge, cheat = by_mode.get("forge"), by_mode.get("cheat")
    g1 = {
        "pass": bool(run and run["score"] == 1 and all(v["score"] == 0 for v in (no_action, empty) if v)),
        "reward_by_state": bool(run and run["score"] == 1),
        "state_sources": reads,
        "text_channel_read": bool(reads_text),
        "state_digest_changed_by_action": bool(run and run["state_digest"] != run["seed_digest"]),
    }
    g2 = {
        "pass": bool(no_action and no_action["score"] == 0),
        "no_action_score": no_action["score"] if no_action else None,
        "no_action_text_carried_expected": bool(no_action and no_action.get("text_answer_carried_expected")),
        "empty_action_score": empty["score"] if empty else None,
        "no_action_state_unchanged": bool(no_action and no_action["state_digest"] == no_action["seed_digest"]),
    }
    g3 = {
        "pass": bool(
            run and isinstance(run["score"], int) and run["score"] in (0, 1)
            and run["verifier_deterministic"] and run["verifier_ignores_text"] and not reads_text
            and forge and forge["score"] == 0
        ),
        "verifier": task.spec["verifier"],
        "verifier_sha256": sha256_file(task.verifier),
        "binary": bool(run and isinstance(run["score"], int) and run["score"] in (0, 1)),
        "deterministic": bool(run and run["verifier_deterministic"]),
        "ignores_text": bool(run and run["verifier_ignores_text"]),
        "reads_state_only": not reads_text,
        "forge_score": forge["score"] if forge else None,
        "forge_rejected": bool(forge and forge["score"] == 0),
    }
    if cheat is not None:
        g3["cheat_score"] = cheat["score"]
        g3["cheat_rejected"] = cheat["score"] == 0
        g3["pass"] = g3["pass"] and cheat["score"] == 0
    if task.spec["tool"] == "http_mock":
        g3["journal_written_by_server"] = True
    g4 = {
        "pass": bool(isolation_ok and residue_absent and not base.get("symlink_escape")),
        "isolation_ok": isolation_ok,
        "residue_absent": residue_absent,
        "state_rolled_back": bool(run and run["seed_matches_pristine"]),
        "symlink_escape": base.get("symlink_escape", []),
    }
    return {"G1": g1, "G2": g2, "G3": g3, "G4": g4}


def leak_checks(task: Task, expected_public: str, mock_module: pathlib.Path | None,
                mock_seed: int | None) -> dict:
    prompt = task.spec["prompt"]
    norm = lambda s: re.sub(r"\s+", " ", s.lower())  # noqa: E731
    numbers = re.findall(r"\d{3,}", expected_public)
    in_prompt = bool(expected_public) and (
        norm(expected_public) in norm(prompt) or any(n in prompt for n in numbers)
    )
    gold_in_seed = any(
        expected_public and expected_public.encode() in p.read_bytes()
        for p in task.seed.rglob("*") if p.is_file()
    )
    out = {
        "gold_in_prompt": in_prompt,
        "gold_in_id": bool(expected_public) and (expected_public in task.id or any(n in task.id for n in numbers)),
        "gold_in_seed": gold_in_seed,
    }
    if mock_module is not None and mock_seed is not None and task.network != "none":
        # Генератор спрашивается напрямую, без живого мока: проверка утечки не должна
        # зависеть от того, поднят ли сервер в этот момент.
        import importlib.util

        spec = importlib.util.spec_from_file_location("gs_mock_probe", mock_module)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        items = mod.gen_items(mock_seed)
        body = json.dumps(items, ensure_ascii=False)
        out["mock_response_has_gold"] = any(n in body for n in numbers) if numbers else False
        out["mock_contract_stated_in_prompt"] = "/v1/report" in prompt
    return out


def cost_section(rollouts: list[dict], batches: list[dict], exec_cost: dict | None) -> dict:
    """Цена шага RL: измеренные слагаемые среды + пересчёт шага на базе S3-pre.

    Разложение на слагаемые ровно то, которое масштабируется:

        rollout(A) = состояние(wipe+seed) + снимок + верификация + A × шаг среды
        шаг RL     = container_start + container_stop + 8 × rollout(A) + база S3-pre

    `A` — число действий на rollout (бюджет пула, не замер скелета), поэтому шаг
    считается для обоих значений аудита {2, 15}. Батч-контейнер амортизируется на
    шаг целиком (решение ADR-049 п.10: контейнер на батч, а не на rollout).
    """
    t_step = exec_cost.get("exec_overhead_s") if exec_cost else None
    per_rollout, fixed_terms = [], []
    for r in rollouts:
        if r["mode"] not in BASE_MODES:
            continue
        fixed = r["wipe_seed_s"] + r["snapshot_s"] + (r["verify_s"] or 0)
        per_rollout.append({"task": r["task"], "mode": r["mode"],
                            "state_and_verify_s": round(fixed, 4),
                            "wipe_seed_s": r["wipe_seed_s"], "action_s": r["action_s"],
                            "snapshot_s": r["snapshot_s"], "verify_s": r["verify_s"],
                            "env_s_at_A1": round(fixed + r["action_s"], 4),
                            "total_s": r["total_s"]})
        fixed_terms.append(fixed)
    t_fixed = mean(fixed_terms)
    t_action_measured = mean([r["action_s"] for r in per_rollout])
    t_env_a1 = mean([p["env_s_at_A1"] for p in per_rollout])
    starts = [b["container_start_s"] for b in batches if "container_start_s" in b]
    stops = [b["container_stop_s"] for b in batches if "container_stop_s" in b]
    batch_walls = [b["batch_wall_s"] for b in batches if "batch_wall_s" in b]
    t_lifecycle = None
    if starts and stops:
        t_lifecycle = round(mean(starts) + mean(stops), 4)

    step: dict = {}
    if t_fixed is not None and t_step is not None:
        gen = BASE_STEP_S * GEN_SHARE
        for a in AUDIT["actions_per_rollout"]:
            env_rollout = t_fixed + a * t_step
            env_step = ROLLOUTS_PER_STEP * env_rollout + (t_lifecycle or 0.0)
            step[f"A{a}"] = {
                "env_per_rollout_s": round(env_rollout, 4),
                "env_per_step_s": round(env_step, 4),
                "env_share_pct": round(100 * env_step / (BASE_STEP_S + env_step), 3),
                "step_s_additive": round(BASE_STEP_S + env_step, 2),
                "ratio_additive": round((BASE_STEP_S + env_step) / BASE_STEP_S, 3),
                "step_s_additive_with_generation_growth": [
                    round(BASE_STEP_S + env_step + g, 2) for g in AUDIT["generation_growth_s"]],
                "ratio_additive_with_generation_growth": [
                    round((BASE_STEP_S + env_step + g) / BASE_STEP_S, 3)
                    for g in AUDIT["generation_growth_s"]],
                "step_s_overlap_bound": round(BASE_STEP_S + max(0.0, env_step - gen), 2),
                "ratio_overlap_bound": round((BASE_STEP_S + max(0.0, env_step - gen)) / BASE_STEP_S, 3),
            }
        step["bounds"] = {
            "why": f"среда {round(ROLLOUTS_PER_STEP * (t_fixed + 2 * t_step) + (t_lifecycle or 0), 1)} с/шаг (A=2) "
                   f"против генерации {round(gen, 1)} с/шаг: "
                   + ("среда НЕ прячется за генерацией — обе границы совпадают"
                      if ROLLOUTS_PER_STEP * (t_fixed + 2 * t_step) + (t_lifecycle or 0) > gen else
                      "среда меньше генерации, поэтому при перекрытии прирост может быть скрыт"),
            "additive_reading": "среда прибавляется к базовому шагу целиком (пессимистичная граница)",
            "overlap_reading": "шаги среды идут между вызовами vLLM; при перекрытии с генерацией "
                               "прирост ограничен разностью, а не суммой",
        }
    return {
        "is_measurement": True,
        "what_is_measured": "подъём/снятие контейнера, откат состояния (wipe+seed), шаг среды (docker exec), "
                            "действие, снимок, верификация — и полное время батча",
        "measured": {
            "t_step_env_s": t_step,
            "t_env_per_rollout_at_A1_s": t_env_a1,
            "t_state_and_verify_per_rollout_s": t_fixed,
            "t_action_measured_s": t_action_measured,
            "t_container_lifecycle_s": t_lifecycle,
            "container_start_s": {"mean": mean(starts), "samples": starts},
            "container_stop_s": {"mean": mean(stops), "samples": stops},
            "batch_wall_s": batch_walls,
            "exec_cost": exec_cost,
            "per_rollout": per_rollout,
            "rollouts_per_step": ROLLOUTS_PER_STEP,
        },
        "step": step,
        "base": {"step_s": BASE_STEP_S, "generation_share": GEN_SHARE,
                 "source": "evidence/s3-rl-probe.json (S3-pre, 99 закрытых шагов)"},
        "audit_estimate": AUDIT,
        "comparison": {
            "local_4080_state_and_verify_s": 0.134,
            "local_4080_action_s": 0.035,
            "stand_vs_local_state_and_verify": None if t_fixed is None else round(t_fixed / 0.134, 2),
            "stand_vs_local_step": None if t_step is None else round(t_step / 0.035, 2),
            "stand_action_within_audit_range": None if t_action_measured is None else (
                AUDIT["t_action_s"][0] <= t_action_measured <= AUDIT["t_action_s"][1]),
        },
        "assumptions": {
            "inherited": [
                "рост длины генерации 17.69–35.38 с — из аудита, здесь не измеряется",
                "число действий на rollout A ∈ {2, 15} — бюджет пула, не замер скелета",
                "линейность по шагам и отсутствие термотроттлинга — как в S3-pre",
            ],
            "measured_not_assumed": [
                "цена шага среды (docker exec) измерена — в локальном прогоне она была принята нулевой",
                "подъём и снятие контейнера измерены, а не взяты диапазоном 0.8–3.0 с",
                "цена верификатора состояния измерена и входит в t_env",
            ],
            "scope": "задачи скелета короткие (одно действие на rollout), поэтому измеренное действие — "
                     "нижняя граница цены действия; сценарий песочницы и шага измерен прямо",
            "additivity": "среда считается аддитивной к базовому шагу — пессимистичная граница; "
                          "перекрытие шагов среды с генерацией не измерялось",
            "batch_per_step": "батч-контейнер амортизируется на один шаг RL (8 rollout'ов); при батче "
                              "другого размера слагаемое жизненного цикла делится иначе — обе части "
                              "приведены отдельно, чтобы пересчитать",
        },
    }


def gate_summary(per_task: list[dict], ev: dict) -> dict:
    g = {k: all(t["gates"][k]["pass"] for t in per_task) for k in ("G1", "G2", "G3", "G4")} if per_task else {}
    return {
        "G1": {"name": "награда по состоянию, не по тексту ответа", "pass": g.get("G1")},
        "G2": {"name": "no-action = 0 (успех недостижим без действий)", "pass": g.get("G2")},
        "G3": {"name": "бинарный внешний верификатор, читает не то, что писал агент", "pass": g.get("G3")},
        "G4": {"name": "изоляция контейнером + откат состояния + лимиты", "pass": g.get("G4")},
        "container_limits": ev.get("container_limits"),
        "container_lifecycle": ev.get("container_lifecycle"),
        "step_limit_binds": ev.get("step_limit", {}).get("binds_on_state"),
        "timeout_applied": ev.get("timeout", {}).get("timeout_applied"),
    }


def share_of(rollouts: list[dict], mode: str) -> float | None:
    xs = [r["score"] for r in rollouts if r["mode"] == mode]
    return round(sum(1 for s in xs if s == 1) / len(xs), 4) if xs else None


def determinism_section(rollouts: list[dict], batches: list[dict]) -> dict:
    """Один сид и один набор действий → одно состояние; что меняется при смене сида."""
    seed_of_batch = {b["container"]: b.get("mock_seed") for b in batches}
    groups: dict[tuple, list[dict]] = {}
    for r in rollouts:
        key = (r["task"], r["mode"], seed_of_batch.get(r["container"]))
        groups.setdefault(key, []).append({
            "container": r["container"], "rep": r.get("rep"), "seed": seed_of_batch.get(r["container"]),
            "state_digest": r["state_digest"], "score": r["score"],
            "expected_public": r.get("expected_public"),
        })
    same_seed, unstable = {}, {}
    for (task, mode, seed), runs in groups.items():
        digests = {r["state_digest"] for r in runs}
        key = f"{task}[{mode}]"
        entry = {"seed": seed, "runs": runs, "distinct_state_digests": len(digests)}
        if len(runs) > 1:
            (same_seed if len(digests) == 1 else unstable)[key] = entry
        elif key not in same_seed:
            same_seed[key] = entry
    mock_runs = [r for r in rollouts if r["network"] != "none"]
    by_seed: dict[int, set[str]] = {}
    per_mode: dict[str, dict[str, str]] = {}
    for r in mock_runs:
        seed = seed_of_batch.get(r["container"]) or 0
        by_seed.setdefault(seed, set()).add(r["state_digest"])
        per_mode.setdefault(f"{r['task']}[{r['mode']}]", {})[str(seed)] = r["state_digest"]
    return {
        "same_seed_same_state": all(v["distinct_state_digests"] == 1 for v in same_seed.values()),
        "same_seed_groups": same_seed,
        "unstable_with_same_seed": unstable,
        "changes_with_seed": {
            "tasks": sorted({r["task"] for r in mock_runs}),
            "state_digests_per_seed": {str(k): sorted(v) for k, v in by_seed.items()},
            "per_mode_digests": per_mode,
            "modes_differing_between_seeds": sorted(k for k, v in per_mode.items() if len(set(v.values())) > 1),
            "modes_equal_between_seeds": sorted(k for k, v in per_mode.items() if len(set(v.values())) == 1),
            "distinct_states_across_seeds": len({d for v in by_seed.values() for d in v}),
            "note": "у задачи с моком сид задаёт выборку генератора: смена сида меняет и ожидаемое "
                    "значение, и состояние после того же действия; у задач без сети сид ни на что не влияет",
        },
        "does_not_change": [
            "состояние задач без сети (shell/filesystem) от сида мока не зависит",
            "вердикт верификатора на одном и том же снимке (два вызова дают один балл)",
            "хэш текста состязательного ответа при одном и том же expected_public",
        ],
        "varies_regardless_of_seed": [
            "времена: подъём контейнера, шаг среды, верификация (в отчёте — распределение, не одно число)",
        ],
    }


# ─── главный проход ──────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Проба предусловий RL на стенде GB10 (ADR-049 п.10)")
    ap.add_argument("--set-dir", default="data/grounded-skeleton")
    ap.add_argument("--set-revision", default="", help="редакция набора (ветка@коммит), для шапки отчёта")
    ap.add_argument("--out", default="evidence/rl-env-probe-gb10.json")
    ap.add_argument("--run-dir", default="", help="каталог прогона (в $HOME)")
    ap.add_argument("--image", default=IMAGE_DEFAULT)
    ap.add_argument("--passes", type=int, default=2, help="проходов батча (2 = проба воспроизводимости)")
    ap.add_argument("--reps", type=int, default=2, help="повторов каждого режима внутри прохода")
    ap.add_argument("--max-steps", type=int, default=4, help="бюджет шагов среды на rollout")
    ap.add_argument("--mock-seed", type=int, default=20260920)
    ap.add_argument("--seed-shift", type=int, default=1, help="сдвиг сида мока между проходами")
    ap.add_argument("--tasks", nargs="*", default=None)
    ap.add_argument("--keep", action="store_true", help="не убирать каталог прогона")
    ap.add_argument("--json", action="store_true", help="отчёт в stdout (только JSON)")
    ap.add_argument("--skip-batches", action="store_true", help="только окружение и ценовые пробы")
    args = ap.parse_args(argv)

    set_dir = pathlib.Path(args.set_dir).resolve()
    run_root = pathlib.Path(args.run_dir).expanduser() if args.run_dir else (pathlib.Path.home() / f"{PROBE_TAG}-runs")
    run_dir = run_root / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scratch").mkdir(exist_ok=True)
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    say = lambda m: print(m, file=sys.stderr if args.json else sys.stdout)  # noqa: E731

    ev: dict = {
        "schema": "rl-env-probe-gb10/1",
        "generated_at": started_at,
        "tool": "tools/rl_env_probe_gb10.py",
        "purpose": "проба предусловий RL на стенде (ADR-049 п.10): механизм изоляции, детерминизм, цена шага, G2/G3",
        "host": {
            "platform": platform.platform(), "kernel": platform.release(),
            "machine": platform.machine(),
            "stand": "gb10-fast (контейнер стадии; GPU пробой не задействован)",
            "cpu_count": os.cpu_count(), "run_dir": str(run_dir),
        },
        "status": "blocked",
        "status_note": "",
        "limits": [
            "пробы no-action/empty-action/forge/cheat исполняет прибор, а не модель: проверяется контур награды, а не политика",
            "лимит шагов реализован прибором (аналог шлюза инструментов харнесса) — в task.json скелета поля шагов нет",
            "цена шага RL — пересчёт на измеренных слагаемых: рост длины генерации взят из аудита как допущение",
            "перекрытие шагов среды с генерацией не измерялось: среда считается аддитивной",
            "проба не проверяет способность модели решать задачи (полоса p* по ADR-049 п.12 — на реальных rollout)",
        ],
    }
    network_name = ""
    mock: MockServer | None = None
    batches: list[dict] = []

    def write() -> None:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps(ev, ensure_ascii=False, indent=2))

    try:
        ok, info = docker_ok()
        ev["host"]["docker"] = info
        if not ok:
            ev["status_note"] = f"NOT-VERIFIED: {info}"
            write()
            return 2
        ok, digest = ensure_image(args.image)
        if not ok:
            ev["status_note"] = f"NOT-VERIFIED: {digest}"
            write()
            return 2
        ev["host"]["image"] = args.image
        ev["host"]["image_digest"] = digest
        if not set_dir.is_dir():
            ev["status_note"] = f"NOT-VERIFIED: нет набора задач {set_dir}"
            write()
            return 2

        # Набор: сверка с карточкой обязательна — иначе проба относится к другой редакции.
        canon, files = set_digest(set_dir)
        card_path = set_dir / "card.json"
        card = json.loads(card_path.read_text(encoding="utf-8")) if card_path.is_file() else {}
        drift = set_drift(set_dir, files, card)
        drift["actual_sha256_full"] = canon
        drift["matches"] = drift["card_sha256_full"] == canon
        ev["set"] = {
            "dir": str(set_dir), "n_files": len(files), "sha256_full": canon,
            "card_sha256_full": card.get("sha256_full"), "card_matches": drift["matches"],
            "drift": drift,
            "source": "ветвь arch/laguna-control-arms, data/grounded-skeleton (монтируется как есть)",
            "source_revision": args.set_revision or None,
            "local_runner": "tools/grounded_runner.py (прогон на 4080 — на стенд не переносится, ADR-049 п.10)",
        }
        tasks = load_tasks(set_dir, args.tasks)
        if not tasks:
            ev["status_note"] = "NOT-VERIFIED: набор задач пуст"
            write()
            return 2
        ev["set"]["n_tasks"] = len(tasks)
        ev["set"]["tools"] = sorted({t.spec["tool"] for t in tasks})
        ev["set"]["task_fields"] = {t.id: sorted(t.spec.keys()) for t in tasks}
        ev["set"]["max_steps_declared_in_task_json"] = {
            t.id: ("max_steps" in t.spec or "step_limit" in t.spec) for t in tasks}
        ev["set"]["timeout_declared_s"] = {t.id: t.timeout_s for t in tasks}

        profiles: dict[str, list[Task]] = {}
        for t in tasks:
            profiles.setdefault("none" if t.network == "none" else "mock", []).append(t)

        # ── батч offline (сеть закрыта профилем `none`), проходы с одним и тем же сидом ──
        if not args.skip_batches:
            for pass_idx in range(1, max(1, args.passes) + 1):
                b = run_batch(profiles["none"], "none", args, run_dir, None, pass_idx, args.reps)
                batches.append(b)
                say(f"батч none, проход {pass_idx}: изоляция={b['isolation_ok']}, "
                    f"rollout'ов={len(b.get('rollouts', []))}, стена={b.get('batch_wall_s')} с")

        # ── батч с моком: сеть `--internal`, мок на её шлюзе ──
        if "mock" in profiles and not args.skip_batches:
            # Имя сети начинается с имени прогона, чтобы снятие по точному префиксу
            # (`rlprobe-<run-id>`) ловило и её: иначе сеть переживает прогон.
            network_name = f"{PROBE_TAG}-{run_dir.name}-net"
            sh(["docker", "network", "rm", network_name], timeout=60)
            if sh(["docker", "network", "create", "--internal", network_name], timeout=60).returncode != 0:
                network_name = ""
            gw = Sandbox.network_gateway(network_name)
            for pass_idx in range(1, max(1, args.passes) + 1):
                seed = args.mock_seed + (pass_idx - 1) * args.seed_shift
                mock = MockServer(set_dir, run_dir / f"pass{pass_idx}", seed, gw)
                mock.start()
                b = run_batch(profiles["mock"], "mock", args, run_dir, mock, pass_idx, args.reps,
                              network=network_name, mock_url=mock.url)
                mock.stop()
                mock = None
                batches.append(b)
                say(f"батч mock, проход {pass_idx} (сид {seed}): изоляция={b['isolation_ok']}, "
                    f"rollout'ов={len(b.get('rollouts', []))}")

        if "mock" in profiles and not args.skip_batches:
            ev["network_profiles"] = network_profile_comparison(args, run_dir)

        # ── лимиты контейнера, лимит шагов, таймаут ──
        ev["container_limits"] = container_limits_probe(args.image, f"{PROBE_TAG}-{run_dir.name}-limits")
        ev["container_lifecycle"] = lifecycle_probe(args.image, f"{PROBE_TAG}-{run_dir.name}-life",
                                                    run_dir)
        plain_task = next((t for t in tasks if t.network == "none"), tasks[0])
        ev["step_limit"] = step_limit_probes(plain_task, args, run_dir)
        ev["timeout"] = timeout_probes(plain_task, args, run_dir)

        exec_cost = next((b["exec_cost"] for b in batches if b.get("exec_cost")), None)
        all_rollouts = [r for b in batches for r in b.get("rollouts", [])]
        ev["batches"] = [{k: v for k, v in b.items() if k != "rollouts"} for b in batches]
        ev["cost"] = cost_section(all_rollouts, batches, exec_cost)

        mock_module = set_dir / "mock" / "mock_server.py"
        per_task = []
        for t in tasks:
            mine = [r for r in all_rollouts if r["task"] == t.id]
            by_mode: dict[str, dict] = {}
            for r in mine:
                by_mode.setdefault(r["mode"], r)
            iso_ok = all(b.get("isolation_ok") for b in batches)
            residue_absent = all(not r["residue_from_previous"] for r in mine)
            gates = gates_for_task(t, by_mode, iso_ok, residue_absent)
            seed_seen = next((b.get("mock_seed") for b in batches if b.get("mock_seed")), None)
            gates["leak"] = leak_checks(t, by_mode.get("run", {}).get("expected_public", ""),
                                        mock_module, seed_seen)
            per_task.append({
                "id": t.id, "tool": t.spec["tool"], "network": t.network, "spec": t.spec,
                "verifier_sha256": sha256_file(t.verifier),
                "res": by_mode,
                "repeat_summary": [{"mode": r["mode"], "rep": r.get("rep"), "pass_batch": r["container"],
                                    "score": r["score"], "state_digest": r["state_digest"],
                                    "seed_matches_pristine": r["seed_matches_pristine"],
                                    "residue_from_previous": r["residue_from_previous"]} for r in mine],
                "gates": gates,
                "residue_absent": residue_absent,
                "state_rolled_back": all(r["seed_matches_pristine"] for r in mine),
                "steps_within_budget": all(r["steps_refused"] == 0 for r in mine),
            })
        ev["tasks"] = per_task
        ev["gates"] = gate_summary(per_task, ev)
        ev["rollouts_count"] = len(all_rollouts)
        ev["no_action"] = {
            "share": share_of(all_rollouts, "no-action"),
            "expected": 0.0,
            "per_task": {r["task"]: r["score"] for r in all_rollouts if r["mode"] == "no-action"},
            "empty_action_per_task": {r["task"]: r["score"] for r in all_rollouts if r["mode"] == "empty-action"},
            "forge_per_task": {r["task"]: r["score"] for r in all_rollouts if r["mode"] == "forge"},
            "cheat_per_task": {r["task"]: r["score"] for r in all_rollouts if r["mode"] == "cheat"},
            "text_answer_carried_expected": {r["task"]: r["text_answer_carried_expected"]
                                             for r in all_rollouts if r["mode"] == "no-action"},
        }
        ev["determinism"] = determinism_section(all_rollouts, batches)

        red = [f"{r['task']}[{r['mode']}]={r['score']!r} (ожидалось {EXPECTED_SCORE[r['mode']]})"
               for r in all_rollouts if r["score"] != EXPECTED_SCORE.get(r["mode"])]
        isolation_bad = [b["container"] for b in batches if not b.get("isolation_ok")]
        ev["verdict"] = {
            "isolation_works": not isolation_bad,
            **{f"G{i}": ev["gates"][f"G{i}"]["pass"] for i in (1, 2, 3, 4)},
            "red_probes": red,
            "isolation_failed_containers": isolation_bad,
            "not_measured_here": [
                "способность модели решать задачи (полоса p* по ADR-049 п.12) — нужен rollout политики",
                "утечка gold у МАССОВОГО пула (9162 задачи) — здесь только скелет из 5 задач",
                "цена обучения RL на заземлённом пуле (шаг пересчитан на измеренных слагаемых, не замерен целиком)",
            ],
        }
        ev["status"] = "ok" if (not red and not isolation_bad) else "failed"
        if red:
            ev["status_note"] = "красные пробы награды: " + "; ".join(red[:5])
        elif isolation_bad:
            ev["status_note"] = "изоляция не подтверждена: " + ", ".join(isolation_bad)
        write()
        say(f"проба: изоляция={'работает' if not isolation_bad else 'НЕ подтверждена'}, "
            f"G1–G4={ {i: ev['gates'][f'G{i}']['pass'] for i in (1,2,3,4)} }, "
            f"цена среды {json.dumps(ev['cost']['step'].get('A2', {}), ensure_ascii=False)[:180]}, "
            f"отчёт: {args.out}")
        return 0 if ev["status"] == "ok" else 1
    finally:
        if mock:
            mock.stop()
        cleanup(run_dir)
        if not args.keep:
            shutil.rmtree(run_dir, ignore_errors=True)


def cleanup(run_dir: pathlib.Path) -> None:
    """Снятие только своих объектов — по точному имени прогона, без широких шаблонов.

    Фильтр включает run-id (`rlprobe-<дата-время>`), а не только префикс пробы:
    параллельный прогон того же прибора другим агентом под фильтр не попадает.
    """
    exact = f"^{PROBE_TAG}-{run_dir.name}"
    r = sh(["docker", "ps", "-aq", "--filter", f"name={exact}"], timeout=60)
    for cid in [c for c in r.stdout.split() if c]:
        sh(["docker", "rm", "-f", cid], timeout=60)
    r = sh(["docker", "network", "ls", "--filter", f"name={exact}", "--format", "{{.Name}}"], timeout=60)
    for net in [n for n in r.stdout.split() if n]:
        sh(["docker", "network", "rm", net], timeout=60)


if __name__ == "__main__":
    raise SystemExit(main())
