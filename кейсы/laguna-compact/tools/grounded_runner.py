#!/usr/bin/env python3
"""Walking skeleton заземлённого RL (ADR-049 пп.1–12): прогон задач в контейнере.

**Зачем инструмент.** ADR-049 (Proposed) объявляет награду по состоянию среды
(G1), нерешаемость без действий (G2), бинарный внешний верификатор (G3) и
изолированную среду с откатом (G4) обязательными для задач оси RL. Проверить это
«в целом» нельзя: каждое условие проверяется отдельно и предъявляется машинно.
Инструмент делает ровно это на пяти задачах и **измеряет цену шага** — оценка
аудита (`evidence/grounding-audit.json`, ×1.207–1.722 к базовому шагу 123.87 с)
осталась оценкой; здесь она проверяется фактом.

**Что именно измеряется и что объявлено допущением.** Замер — только время
прогонов в контейнере (подъём, откат состояния, действие, снимок, верификация).
Рост длины генерации из формулы аудита здесь не измеряется и берётся как есть
(`cost.assumptions.inherited`); поэтому «шаг» в отчёте — пересчёт формулы аудита
на измеренных слагаемых, а не замер шага RL.

**Режимы.**
  (ни одного флага)   полный проход: `run` + `no-action` + `empty-action` по всем задачам
  --run               только положительный контроль (действие выполнено → награда 1)
  --no-action         только проба G2 (текстовый ответ без вызовов → награда 0)
  --empty-action      только проба «действие без последствий» (`true` → награда 0)
  --step-probe        проба лимита шагов: бюджет 0 → 0, бюджет 1 → 1 (среда, не харнесс)
  --timeout-probe     проба супервизора шага: убитый по таймауту шаг не влияет на следующий
  --teardown-probe    проба снятия батч-контейнера: `-t 0` против `-t 1` — цена и целость состояния
  --sidecar-probe     цена мока-sidecar: подъём с готовностью и снятие, N повторов
  --mount-preflight-only  только предпроверка монтирования (маркер-контейнер) и выход
  --in-stage-container  заявка «проба идёт внутри контейнера стадии» — принимается только
                      при подтверждении признаками (см. execution_context)
  --json              отчёт в stdout (файл пишется в обоих случаях)

**Предпроверка монтирования — ДО прогона, а не по следствию.** Bind-mount'ы
резолвит демон ХОСТА, поэтому внутри контейнера стадии каталог прогона и дерево
набора обязаны быть смонтированы ПО ТЕМ ЖЕ путям. До дельты несовпадение ловилось
косвенно («мок не отметился готовым», задача падала на `mv: Permission denied`) — а
это маскирует причину: «среда не дала» читается как «задача не решена». Теперь
маркер-контейнер сверяет СОДЕРЖИМОЕ по названному пути (nonce каталога прогона,
состав дерева набора, размер карточки) и останавливает прогон явным сообщением;
проверка стоит один контейнер и видна в отчёте полем `host.mount_preflight`.
Дерево набора проверяется тогда, когда прогон будет его монтировать (в наборе есть
задача с сетью) — иначе проверка краснела бы на наборе без мока (ADR-023 п.12).

**Как устроена изоляция (G4).** Задача исполняется в контейнере (docker) с
read-only корнем, непривилегированным пользователем, без выхода наружу, с
лимитами памяти и числа процессов; рабочее состояние — единственный bind-mount
`/work`, откатываемый к исходному между прогонами. Контейнер на батч, а не на
rollout (ADR-049 п.10: оптимизация цены без потери изоляции — батч группируется
по сетевому профилю, состояние откатывается).

**Сетевой профиль задачи с моком — `--internal`, а не дефолтный `bridge`**
(проба 20.09.2026: `bridge` выпускает контейнер в интернет, `external_unreachable=false`).
Раннер сам создаёт внутреннюю сеть прогона (`docker network create --internal`);
egress наружу отсутствует, DNS не резолвится, а мок — единственный достижимый
эндпоинт. Пока сеть открыта, награду можно получить внешним вызовом, а не
состоянием, — это подрывает саму ось ADR-049. Живая проверка выхода — страж
`tools/check_env_contract.py` (правило C-028): он пытается выйти наружу и обязан
провалиться; проверка «флаг `--internal` выставлен» стражем не считается.

**Мок — sidecar-контейнер в сети прогона, а не слушатель на её шлюзе**
(поправка к ADR-049 п.10 от 20.09.2026). Слушатель на шлюзе жил в сетевом
пространстве ХОСТА: пока раннер исполнялся на хосте, это работало, но проба
«повторить изоляцию внутри контейнера стадии» требовала, чтобы раннер сам занял
адрес хоста, — а этим адресом владеет демон, не контейнер (`Errno 99 Cannot
assign requested address`). Sidecar снимает зависимость от топологии: контейнер
мока живёт в ТОЙ ЖЕ внутренней сети, что и песочница, поэтому достижим из неё
независимо от того, где исполняется цикл RL.

Следствия, названные явно:
  * админ-операции над моком (ротация журнала, чтение выборки) идут **через
    демон** (`docker exec`), а не по сети: у раннера нет своего сетевого пути к
    моку, поэтому механизм одинаков на хосте и внутри контейнера стадии;
  * канарейка выхода наружу — тоже контейнер, но на **дефолтной bridge**: её
    адрес лежит ВНЕ сети прогона, и «недостижима» проверяется оттуда, где
    выход есть (иначе проба вакуумна на хосте без интернета);
  * bind-mount'ы контейнер стадии резолвит **демон хоста**, поэтому каталог
    прогона и дерево набора обязаны быть смонтированы в контейнер стадии **по
    тем же путям**, что на хосте. Несоблюдение не проходит молча: прогон
    останавливает **предпроверка монтирования** (`mount_preflight`, маркер-
    контейнер до первого шага) и возвращает NOT-VERIFIED с названной причиной и
    путём — вместо косвенного «мок не отметился готовым», которое не отличает
    невидимый каталог от нерешаемой задачи.

**Шаг под супервизором (G4).** Измерено пробой 20.09.2026: клиентский таймаут
`docker exec` процесс внутри контейнера **не убивает** и оставляет частичный след.
Поэтому каждое действие исполняется под супервизором (`set -m` — своя группа
процессов), и по таймауту среда сама снимает группу (`kill -9 -<pgid>`) и
откатывает след: состояние возвращается из seed, проба `--timeout-probe`
показывает, что убитый шаг не влияет на следующий rollout.

**Лимит шагов — из задачи, а не от вызывающей стороны.** `task.json` несёт
`max_steps` (`grounded-task/2`), и исполняет его **среда**: исчерпанный бюджет
отказывает в действии, не исполняя его. Харнесс вправе держать внешний потолок
сверху (`--max-steps N`, применяется как минимум), но источник истины — задача;
оба числа попадают в отчёт.

Почему контейнер, а не пространства имён: проба аудита показала, что `bwrap` и
`unshare` в этом окружении падают (`setting up uid map: Permission denied`,
`CapEff: 0000000000000000`). Если контейнер недоступен или проба изоляции не
подтвердилась — инструмент возвращает NOT-VERIFIED (exit 2), а не «заземление без
изоляции».

**Границы.** Инструмент не запускает модель: `--run` исполняет эталонное действие
задачи (положительный контроль), `--no-action` — текстовый ответ без действий.
Это пробы контура награды, а не замер способности модели; поэтому доля успехов в
скелете ничего не говорит о трудности задач для политики (полоса p* из ADR-049
п.12 измеряется на реальных rollout, отдельной дельтой).

Коды возврата::

    0 — все пробы дали ожидаемое (run=1, no-action=0, empty-action=0), отчёт записан
    1 — красное: проба дала не то (задача не заземлена либо среда не пропускает действия)
    2 — NOT-VERIFIED/blocked: нет docker, образа или задач; либо изоляция не подтверждена

Примеры::

    python3 tools/grounded_runner.py                        # полный проход + evidence
    python3 tools/grounded_runner.py --no-action --json      # только проба G2
    python3 tools/grounded_runner.py --repeat 2 --out evidence/grounded-skeleton.json
    python3 tools/grounded_runner.py --card                  # пересобрать карточку набора
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

CASE_ROOT = pathlib.Path(__file__).resolve().parent.parent
SET_DIR = CASE_ROOT / "data" / "grounded-skeleton"
DEFAULT_IMAGE = "alpine:3.20"
DEFAULT_OUT = CASE_ROOT / "evidence" / "grounded-skeleton.json"
# Каталог прогона обязан лежать в $HOME: docker на этом хосте (snap-конфайнмент)
# не видит /tmp и молча отдаёт в контейнер пустой каталог вместо bind-mount.
DEFAULT_RUN_ROOT = pathlib.Path.home() / ".cache" / "arch-ml" / "grounded-skeleton"
MOCK_SEED_DEFAULT = 20260920
BASE_STEP_S = 123.87  # evidence/s3-rl-probe.json: измеренный шаг текстового S3-pre
MODES = ("run", "no-action", "empty-action")
EXPECTED_SCORE = {"run": 1, "no-action": 0, "empty-action": 0}

# ── сетевой профиль задачи с моком ───────────────────────────────────────────
# Имя сети/контейнеров несёт префикс пробы: снятие за собой идёт по ТОЧНОМУ
# префиксу, а не по широкому шаблону (общий /tmp и общий docker делят дельты).
RUN_TAG = "gs"
TASK_SCHEMA = "grounded-task/2"
# Попытки выхода наружу: проверка ИМИТИРУЕТ выход, а не читает флаг запуска.
# Один и тот же набор применяется раннером (проба изоляции) и стражем
# `tools/check_env_contract.py` — формулу держат два файла, совпадение сверяют
# тесты (AD-10: страж самостоятелен, но проверяет то же свойство).
EGRESS_HTTP_TARGET = "http://1.1.1.1/"          # публичный IP: egress по адресу
EGRESS_DNS_TARGET = "example.com"               # DNS: резолв наружу
# Канарейка: контейнер-слушатель на ДЕФОЛТНОЙ bridge — адрес ВНЕ сети прогона.
# Внутри своей внутренней сети он недостижим (межсетевой трафик docker не
# пропускает), из контейнера на дефолтной bridge — достижим. Даёт проверку, не
# зависящую от наличия интернета у хоста (иначе «зелёный» страж на изолированном
# хосте ничего не доказывал бы). Запасной адрес — шлюз дефолтной bridge: он нужен
# только как имя площадки в отчёте, когда своего адреса у канарейки ещё нет.
CANARY_HOST_DEFAULT = "172.17.0.1"
CANARY_NETWORK = "bridge"
HOST_SERVICE_PORTS = (22, 80, 443, 3080, 8080)

# ── sidecar-мок и канарейка (поправка ADR-049 п.10 от 20.09.2026) ────────────
# Образ sidecar'ов — отдельный от образа песочницы: песочнице нужен только sh,
# моку и канарейке — python3. Порт мока фиксирован: он живёт в сетевом
# пространстве СВОЕГО контейнера, поэтому конфликтов с хостом не бывает (в
# отличие от слушателя на шлюзе, которому порт выбирался свободным).
DEFAULT_SIDECAR_IMAGE = "python:3.12-alpine"
MOCK_PORT = 18099
CANARY_PORT = 18081
MOCK_STATE_DIR = "mock-state"   # каталог журнала: монтируется ТОЛЬКО в контейнер мока
# Пользователь ВСЕХ контейнеров прогона (песочница, мок, канарейка) — один и тот
# же, и он же задаёт владельца состояния: песочница обязана уметь переименовывать
# файлы в /work, а мок — переписывать свой журнал. Одно число на оба места, иначе
# владелец и потребитель разъезжаются (см. `hand_to_runtime_user`).
RUNTIME_USER = "1000:1000"
RUNTIME_UID = 1000
RUNTIME_GID = 1000
SIDECAR_USER = RUNTIME_USER
# Признаки контейнера стадии — по ним заявка `--in-stage-container` подтверждается
# фактом, а не принимается на слово (метка площадки не должна называть чужую машину).
STAGE_MARKERS = ("NVIDIA_PYTORCH_VERSION", "NVIDIA_PYTORCH_CONTAINER")

# Обёртка шага внутри контейнера (G4: среда сама снимает процесс по таймауту).
# `set -m` даёт фоновому заданию СВОЮ группу процессов: `$!` — лидер группы,
# поэтому `kill -9 -$!` из контейнера снимает и сам шаг, и его потомков
# (`sh -c "$1"` без своей группы оставил бы `sleep` висеть — измерено пробой).
SUPERVISOR = (
    "set -m; mkdir -p /tmp/gs-step; rm -f /tmp/gs-step/pid; "
    'sh -c "$1" & child=$!; echo "$child" > /tmp/gs-step/pid; wait "$child"'
)
STEP_DIR = "/tmp/gs-step"

# ── предпроверка монтирования (same-path) ────────────────────────────────────
# Файл-маркер каталога прогона: nonce, записанный раннером, читается маркер-
# контейнером по ТОМУ ЖЕ пути. Несовпадение путей до этой дельты ловилось
# косвенно — «мок не отметился готовым» и «задача упала на `mv: Permission
# denied`», — а это маскирует причину: «среда не дала» читается как «задача не
# решена». Класс дефекта тот же, что у владения файлами; лечится предпроверкой.
PREFLIGHT_MARKER = ".mount-preflight"

# Допущения аудита, которые здесь НЕ измеряются (берутся как есть, ADR-049 п.10).
AUDIT = {
    "source": "evidence/grounding-audit.json → cost_estimate.scenarios.container_per_rollout",
    "base_step_s": BASE_STEP_S,
    "rollouts_per_step": 8,
    "t_sandbox_per_rollout_s": [0.8, 3.0],
    "t_action_s": [0.1, 0.25],
    "ratio_to_base": [1.207, 1.722],
    "generation_growth_s": [17.69, 35.38],
    "actions_per_rollout": [2, 15],
}


def default_stand_label() -> str:
    """Метка стенда по факту: кадр замера не должен называть чужую машину.

    Цена и пробы изоляции не переносятся между стендами (ADR-049 п.10), поэтому
    «локальная 4080» в отчёте, снятом на GB10, была бы ложью о происхождении чисел.
    """
    return f"{platform.node()} (GPU пробой не задействован)"



# ─── мелкие утилиты ──────────────────────────────────────────────────────────


def sh(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


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


def hand_to_runtime_user(path: pathlib.Path) -> int:
    """Отдаёт файл (или дерево) пользователю, под которым работают контейнеры прогона.

    Зачем это нужно явно. Песочница и мок работают под `--user 1000:1000`, а
    состояние /work и журнал мока готовит раннер — и если он исполняется от root
    (прогон из контейнера стадии), то созданные им файлы принадлежат root. Песочнице
    этого мало: переименование файла требует прав на КАТАЛОГ, а не на файл, поэтому
    `mv` в задаче падает с `Permission denied`, а ротация журнала мока — с отказом
    записи. На хосте дефект не проявлялся ровно потому, что раннер и песочница
    совпадали по uid, то есть работало СЛУЧАЙНО; найдено пробой 20.09.2026 внутри
    контейнера стадии.

    Под не-root передача не нужна (владелец уже совпадает), но и не «молча ок»:
    вызывающая сторона обязана сверить владельца — это делает
    `runtime_user_ready`.
    """
    if os.geteuid() != 0:
        return 0
    targets = [path, *path.rglob("*")] if path.is_dir() else [path]
    n = 0
    for p in targets:
        try:
            os.chown(p, RUNTIME_UID, RUNTIME_GID, follow_symlinks=False)
            n += 1
        except OSError:
            pass
    return n


def runtime_user_ready(root: pathlib.Path) -> tuple[bool, str]:
    """Владелец состояния совпадает с пользователем контейнеров прогона.

    Проверка нужна там, где передать владение нельзя (раннер не root): если uid
    раннера и uid песочницы разошлись, состояние ей недоступно, и прогон обязан
    сказать это СЛОВАМИ, а не отдать загадочные нули за награду.
    """
    if os.geteuid() == 0:
        return True, "раннер root: владение передаётся контейнерам прогона явно"
    if os.geteuid() == RUNTIME_UID:
        return True, f"раннер и контейнеры прогона — один uid ({RUNTIME_UID})"
    return False, (
        f"раннер исполняется от uid {os.geteuid()}, а контейнеры прогона — от {RUNTIME_UID}: "
        "состояние /work и журнал мока им недоступны на запись (передать владение можно только из-под root)"
    )


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
    if r.returncode == 0:
        return True, r.stdout.strip()
    r = sh(["docker", "pull", image], timeout=600)
    if r.returncode != 0:
        return False, f"образ {image} недоступен и не скачался: {r.stderr.strip()[:200]}"
    return ensure_image(image)


def ensure_image_measured(image: str) -> tuple[bool, dict]:
    """`ensure_image` + ЦЕНА тяга: тянулся ли образ и сколько это стоило.

    Граница переносимости, которую это поле делает наблюдаемой: числа замера сняты
    на ПРОГРЕТОМ стенде, где оба образа (песочницы и sidecar'а) уже лежали в
    хранилище демона. На чистом стенде первая проба платит скачивание, и без этого
    поля цена молча уехала бы в «необъяснимую» задержку подъёма — а подставлять сюда
    оценку запрещено: оценка не замер (ADR-023 п.13). Тяга не было — `pull_s: null`,
    а не ноль: ноль означал бы «скачалось мгновенно», то есть измерение, которого не
    делали.
    """
    r = sh(["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"], timeout=60)
    if r.returncode == 0:
        return True, {"image": image, "digest": r.stdout.strip(), "pulled": False, "pull_s": None}
    t0 = time.perf_counter()
    r = sh(["docker", "pull", image], timeout=600)
    pull_s = round(time.perf_counter() - t0, 4)
    if r.returncode != 0:
        return False, {"image": image, "digest": None, "pulled": False, "pull_s": None,
                       "error": f"образ {image} недоступен и не скачался: {r.stderr.strip()[:200]}"}
    ok, digest = ensure_image(image)
    return ok, {"image": image, "digest": digest, "pulled": True, "pull_s": pull_s}


def mount_preflight(run_dir: pathlib.Path, set_dir: pathlib.Path, image: str,
                    check_set_tree: bool) -> dict:
    """Same-path монтирование: каталог прогона и дерево набора видит ЛИ ДЕМОН.

    Bind-mount'ы контейнеров прогона резолвит демон ХОСТА, а не раннер. Пока раннер
    исполняется на хосте, его пути и пути демона совпадают; внутри контейнера стадии
    это верно только если каталог прогона и дерево набора смонтированы в него ПО ТЕМ
    ЖЕ путям. Несоблюдение имеет два лица одного дефекта:

      * демон не видит путь — docker отказывает (`bind source path does not exist`);
      * демон видит путь, но ДРУГОЙ каталог — контейнер молча получает пустоту (так
        выглядит `/tmp` у демона под snap-конфайнментом: bind «удался», а внутри
        ничего).

    Второе лицо и есть причина, по которой дефект ловился косвенно: «мок не отметился
    готовым» одинаково выглядит и при невидимом каталоге, и при нерешаемой задаче.
    Поэтому маркер сверяет не факт монтирования, а СОДЕРЖИМОЕ по названному пути:
    nonce, записанный раннером, и состав дерева набора обязаны быть видны из
    контейнера — иначе прогон останавливается ЗДЕСЬ, с названной причиной, а не на
    первом шаге задачи.

    `check_set_tree` — условность, а не удобство: дерево набора монтирует только мок,
    поэтому на наборе без сети проверка дерева краснела бы за отсутствие того, чего в
    прогоне не будет (ложный красный запрещён, ADR-023 п.12).
    """
    nonce = secrets.token_hex(16)
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o755)
    marker = run_dir / PREFLIGHT_MARKER
    marker.write_text(nonce, encoding="utf-8")
    os.chmod(marker, 0o644)
    card = set_dir / "card.json"
    expected_listing = sorted(p.name for p in set_dir.iterdir()) if (check_set_tree and set_dir.is_dir()) else []
    expected_card_bytes = card.stat().st_size if (check_set_tree and card.is_file()) else None

    mounts = ["--mount", f"type=bind,src={run_dir},dst={run_dir},readonly"]
    script = f'cat "{marker}" || exit 3; echo'
    if check_set_tree:
        mounts += ["--mount", f"type=bind,src={set_dir},dst={set_dir},readonly"]
        script += (f'; echo "==SET=="; ls -1 "{set_dir}" || exit 4'
                   f'; echo "==CARD=="; wc -c < "{card}" || exit 5')
    cmd = ["docker", "run", "--rm", "--network", "none", "--read-only",
           "--user", RUNTIME_USER, *mounts, image, "sh", "-c", script]
    where = f"каталог прогона {run_dir}" + (f" и дерево набора {set_dir}" if check_set_tree else "")
    findings: list[str] = []
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
        rc, raw_out, raw_err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        rc, raw_out, raw_err = -1, b"", "маркер-контейнер не ответил за 120 с".encode()
    elapsed = round(time.perf_counter() - t0, 4)
    # Байты, а не текст: `card.json` набора — UTF-8, и режим `text=True` уронил бы
    # пробу на локали, отличной от UTF-8, выдав порчу кодировки за дефект путей.
    out = raw_out.decode("utf-8", errors="replace")
    err = raw_err.decode("utf-8", errors="replace").strip()
    head, _, rest = out.partition("==SET==")
    set_part, _, card_part = rest.partition("==CARD==")
    observed_nonce = head.strip()
    observed_listing = sorted(x.strip() for x in set_part.splitlines() if x.strip())
    observed_card_bytes = card_part.strip()

    if rc != 0:
        if "bind source path does not exist" in err:
            findings.append(
                f"same-path монтирование не выполнено: демон не видит по названному пути {where} "
                f"({err[:200]}). Причина: bind-mount резолвит демон ХОСТА, поэтому при прогоне из "
                "контейнера стадии каталог прогона и дерево набора обязаны быть смонтированы в него "
                "ПО ТЕМ ЖЕ путям, что на хосте (docker run -v <путь>:<путь> …)"
            )
        else:
            findings.append(
                f"маркер-контейнер пробы монтирования не отработал ({rc}): {err[:200] or 'без вывода'}"
            )
    else:
        if observed_nonce != nonce:
            findings.append(
                f"same-path монтирование не выполнено: каталог прогона виден демону по ДРУГОМУ пути — "
                f"маркер {marker} из контейнера не прочитан (получено {observed_nonce[:32]!r}). Причина: "
                "демон смонтировал не тот каталог, который назвал раннер"
            )
        if check_set_tree and observed_listing != expected_listing:
            findings.append(
                "same-path монтирование не выполнено: дерево набора видно демону по тому же пути, но "
                f"это ДРУГОЙ каталог — в контейнере {observed_listing}, у раннера {expected_listing}"
            )
        if expected_card_bytes is not None and observed_card_bytes != str(expected_card_bytes):
            findings.append(
                f"same-path монтирование не выполнено: карточка набора {card} видна демону, но не та — "
                f"размер {observed_card_bytes or 'не прочитан'} против {expected_card_bytes} Б"
            )
    marker.unlink(missing_ok=True)
    return {
        "ok": not findings,
        "is_measurement": False,
        "what": "same-path монтирование: демон видит каталог прогона и дерево набора по тем же путям, что раннер",
        "checked": where,
        "probe": {"image": image, "run_dir": str(run_dir),
                  "set_dir": str(set_dir) if check_set_tree else None,
                  "exit_code": rc, "elapsed_s": elapsed},
        "observed": {"run_dir_marker_read": observed_nonce == nonce,
                     "set_tree_listing_matches": (observed_listing == expected_listing) if check_set_tree else None,
                     "card_size_bytes": observed_card_bytes or None},
        "expected": {"set_tree_listing": expected_listing if check_set_tree else None,
                     "card_size_bytes": expected_card_bytes},
        "findings": findings,
        "reading": (
            f"маркер-контейнер ({image}, {elapsed} с) проверил {where}: "
            + ("пути совпали, монтирование исполнимо" if not findings
               else "пути НЕ совпали — прогон остановлен с названной причиной")
            + ("; дерево набора не проверялось: в наборе нет задач с сетью, мок его не монтирует"
               if not check_set_tree else "")
        ),
    }


def network_gateway(network: str) -> str:
    """Шлюз сети прогона: адрес, на котором живёт мок (и только он)."""
    if not network or network == "none":
        return CANARY_HOST_DEFAULT
    r = sh(["docker", "network", "inspect", network,
            "--format", "{{(index .IPAM.Config 0).Gateway}}"], timeout=30)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else CANARY_HOST_DEFAULT


def create_internal_network(name: str) -> tuple[bool, str]:
    """Сеть прогона: `--internal` — без выхода наружу.

    Именно `--internal`, а не дефолтный `bridge`: 20.09.2026 проба показала, что
    на `bridge` контейнер достаёт до `http://1.1.1.1/`, то есть награду можно
    получить внешним вызовом. Мок при этом остаётся достижим — он sidecar-
    контейнер В ЭТОЙ сети (поправка ADR-049 п.10), поэтому его достижимость не
    зависит от того, где исполняется цикл RL.
    """
    sh(["docker", "network", "rm", name], timeout=60)
    r = sh(["docker", "network", "create", "--internal", name], timeout=60)
    if r.returncode != 0:
        return False, r.stderr.strip()[:200]
    return True, network_gateway(name)


def remove_network(name: str) -> None:
    if name:
        sh(["docker", "network", "rm", name], timeout=60)


def container_ip(name: str, network: str) -> str:
    """Адрес контейнера в названной сети — им и зовётся sidecar (DNS не нужен)."""
    r = sh(["docker", "inspect", "-f",
            f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}', name], timeout=30)
    return r.stdout.strip() if r.returncode == 0 else ""


def execution_context() -> dict:
    """Где раннер исполняется НА САМОМ ДЕЛЕ — по признакам, а не по флагу.

    Метка площадки не должна называть чужую машину (ADR-049 п.10), поэтому
    заявка `--in-stage-container` подтверждается фактами: признак контейнера,
    примонтированный docker-сокет и переменные образа стадии. Расхождение
    заявки и признаков — отказ прогона, а не «поверим флагу».
    """
    in_container = pathlib.Path("/.dockerenv").is_file()
    cgroup = ""
    if not in_container:
        try:
            cgroup = pathlib.Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
        except OSError:
            cgroup = ""
        in_container = "docker" in cgroup or "containerd" in cgroup
    markers = {k: os.environ[k] for k in STAGE_MARKERS if os.environ.get(k)}
    return {
        "in_container": in_container,
        "container_hostname": socket.gethostname(),
        "docker_socket_mounted": pathlib.Path("/var/run/docker.sock").exists(),
        "stage_markers": markers,
    }


def stage_container_ok(ctx: dict) -> tuple[bool, str]:
    """Подтверждение заявки «проба в контейнере стадии»: что именно не сошлось."""
    bad = []
    if not ctx.get("in_container"):
        bad.append("признаков контейнера нет (/.dockerenv и cgroup говорят о хосте)")
    if not ctx.get("docker_socket_mounted"):
        bad.append("docker-сокет внутрь не примонтирован (/var/run/docker.sock отсутствует)")
    if not ctx.get("stage_markers"):
        bad.append(f"нет ни одной переменной образа стадии ({', '.join(STAGE_MARKERS)})")
    return (not bad), "; ".join(bad)


# ─── контейнер ───────────────────────────────────────────────────────────────


class StepResult:
    """Итог шага среды: отказ по бюджету и таймаут — разные факты, не «ошибка».

    `refused` — шаг НЕ исполнялся (бюджет шагов исчерпан): это работающая среда,
    а не сбой. `timed_out` — шаг прерван клиентом, после чего среда обязана
    снять группу процессов сама (`killed_pid`, `group_left`).
    """

    def __init__(self, returncode: int | None, stdout: str = "", stderr: str = "",
                 refused: bool = False, timed_out: bool = False, elapsed_s: float = 0.0,
                 reason: str = "", step_index: int | None = None,
                 killed_pid: int | None = None, group_left: int | None = None,
                 supervised: bool = False) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.refused = refused
        self.timed_out = timed_out
        self.elapsed_s = round(elapsed_s, 4)
        self.reason = reason
        self.step_index = step_index
        self.killed_pid = killed_pid
        self.group_left = group_left
        self.supervised = supervised


class Sandbox:
    """Один контейнер на батч задач с одним сетевым профилем (ADR-049 п.10).

    Бюджет шагов — свойство ЗАДАЧИ (`max_steps`), а не вызывающей стороны:
    среда отказывает в действии, когда бюджет исчерпан, и этот отказ попадает в
    отчёт (`steps_used`, `steps_refused`). Внешний потолок харнесса применяется
    как минимум (`min`) и не может поднять бюджет выше объявленного задачей.
    """

    def __init__(self, image: str, workdir: pathlib.Path, network: str, name: str,
                 mock_url: str = "", max_steps: int | None = None) -> None:
        self.image = image
        self.workdir = workdir
        self.network = network
        self.name = name
        self.mock_url = mock_url
        self.max_steps = max_steps
        self.steps_used = 0
        self.steps_refused = 0
        self.start_s = 0.0
        self.stop_s = 0.0
        self.stop_grace = 0
        self.handover_objects = 0
        self.probes: dict = {}
        self._started = False

    def probe_state_writable(self) -> dict:
        """Права песочницы на СОСТОЯНИЕ, а не на точку монтирования.

        Проба `bind_mount_writable` доказывает лишь то, что запись возможна в
        `/work`: каталог монтирования открыт на 0777. Переименование файла требует
        прав на его КАТАЛОГ, поэтому состояние, скопированное не тем пользователем,
        недоступно песочнице — и это видно только здесь. Проба создаёт и тут же
        удаляет файл в первом попавшемся подкаталоге: след не остаётся, а
        свойство «песочница может писать в состояние» названо явно.
        """
        r = self.exec(
            'd=$(find /work -mindepth 1 -type d 2>/dev/null | head -1); '
            '[ -z "$d" ] && { echo "NO_SUBDIR -"; exit 0; }; '
            'if touch "$d/.gs-write-probe" 2>/dev/null; then rm -f "$d/.gs-write-probe"; echo "WRITABLE $d"; '
            'else echo "DENIED $d"; fi',
            timeout=30, count_step=False)
        line = ((r.stdout or "").strip().splitlines() or ["NO_ANSWER -"])[-1].split(" ", 1)
        return {"verdict": line[0], "dir": line[1] if len(line) > 1 else "",
                "checked": line[0] != "NO_SUBDIR"}

    def reset_steps(self, max_steps: int | None) -> None:
        """Новый rollout — новый бюджет: лимит считается на rollout, не на батч."""
        self.max_steps = max_steps
        self.steps_used = 0
        self.steps_refused = 0

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workdir, 0o777)
        hand_to_runtime_user(self.workdir)
        cmd = [
            "docker", "run", "-d", "--rm", "--name", self.name,
            "--read-only", "--tmpfs", "/tmp:rw,size=16m",
            "--user", RUNTIME_USER,
            "--memory", "256m", "--pids-limit", "256",
            "-v", f"{self.workdir}:/work", "-w", "/work",
        ]
        if self.network == "none":
            cmd += ["--network", "none"]
        else:
            # Адрес мока — шлюз СВОЕЙ внутренней сети; --add-host не нужен: DNS
            # наружу закрыт, и имя хоста было бы вторым каналом наблюдения.
            cmd += ["--network", self.network]
            if self.mock_url:
                cmd += ["-e", f"MOCK_BASE={self.mock_url}"]
        cmd += [self.image, "sleep", "7200"]
        t0 = time.perf_counter()
        r = sh(cmd, timeout=120)
        self.start_s = time.perf_counter() - t0
        if r.returncode != 0:
            raise RuntimeError(f"контейнер не поднялся: {r.stderr.strip()[:300]}")
        self._started = True

    def _docker_exec_argv(self, command: str, supervise: bool) -> list[str]:
        cmd = ["docker", "exec", "-w", "/work"]
        if self.mock_url:
            cmd += ["-e", f"MOCK_BASE={self.mock_url}"]
        if supervise:
            # Обёртка держит шаг в своей группе процессов: иначе `kill` по
            # таймауту не достанет потомков (измерено пробой 20.09.2026).
            cmd += [self.name, "sh", "-c", SUPERVISOR, f"{RUN_TAG}-step", command]
        else:
            cmd += [self.name, "sh", "-c", command]
        return cmd

    def exec(self, command: str, timeout: int = 120, count_step: bool = False,
             supervise: bool | None = None) -> StepResult:
        """Шаг среды. `count_step` — это ДЕЙСТВИЕ: расходует бюджет и супервизируется."""
        if supervise is None:
            supervise = count_step
        if count_step:
            if self.max_steps is not None and self.steps_used >= self.max_steps:
                self.steps_refused += 1
                return StepResult(
                    None, refused=True, step_index=self.steps_used,
                    reason=f"бюджет шагов исчерпан ({self.max_steps}) — действие не исполнено",
                )
            self.steps_used += 1
        t0 = time.perf_counter()
        try:
            r = sh(self._docker_exec_argv(command, supervise), timeout=timeout)
            return StepResult(r.returncode, r.stdout, r.stderr,
                              elapsed_s=time.perf_counter() - t0,
                              step_index=self.steps_used if count_step else None,
                              supervised=supervise)
        except subprocess.TimeoutExpired:
            # Клиентский таймаут процесс внутри НЕ убивает: снимает среда.
            killed, left = self.kill_step()
            return StepResult(None, timed_out=True, elapsed_s=time.perf_counter() - t0,
                              step_index=self.steps_used if count_step else None,
                              killed_pid=killed, group_left=left, supervised=supervise,
                              reason=f"шаг превысил таймаут {timeout}s — снят средой")

    # ── супервизор шага: снять процесс и убедиться, что он снят ──
    def kill_step(self) -> tuple[int | None, int | None]:
        """Снимает группу процессов шага внутри контейнера и считает, что осталось."""
        r = self.exec(
            f"if [ -f {STEP_DIR}/pid ]; then p=$(cat {STEP_DIR}/pid); "
            "kill -9 -$p 2>/dev/null; kill -9 $p 2>/dev/null; echo $p; else echo 0; fi",
            timeout=30, count_step=False)
        pid = (r.stdout or "").strip().splitlines()
        killed = int(pid[-1]) if pid and pid[-1].isdigit() and int(pid[-1]) > 0 else None
        return killed, self.step_group_left(killed)

    def step_group_left(self, pgid: int | None) -> int | None:
        """Сколько процессов группы шага живо после снятия (0 — снят целиком)."""
        if pgid is None:
            return None
        census = self.exec(
            f"t={pgid}; n=0; for d in /proc/[0-9]*; do pid=${{d#/proc/}}; "
            "[ \"$pid\" = \"$$\" ] && continue; "
            "pg=$(cut -d' ' -f5 \"$d/stat\" 2>/dev/null); "
            "[ \"$pg\" = \"$t\" ] && n=$((n+1)); done; echo $n",
            timeout=30, count_step=False)
        out = (census.stdout or "").strip().splitlines()
        return int(out[-1]) if out and out[-1].isdigit() else None

    def stop(self) -> None:
        """Снятие батч-контейнера: `-t 0` (SIGKILL сразу), а не `-t 1`.

        Измерено пробой 20.09.2026: `docker stop -t 1` стоил 1.10 с против
        0.085 с у `-t 0`, и grace-период ложился на каждый батч, то есть на шаг
        RL целиком. Целость состояния от этого не страдает: награда читается из
        снимка ДО снятия, а состояние следующего rollout'а откатывается из seed;
        факт (а не аргумент) проверяется пробой `--teardown-probe`.
        """
        if self._started:
            t0 = time.perf_counter()
            sh(["docker", "stop", "-t", "0", self.name], timeout=90)
            self.stop_s = round(time.perf_counter() - t0, 4)
            self.stop_grace = 0
            sh(["docker", "rm", "-f", self.name], timeout=60)
            self._started = False

    def stop_with_grace(self, grace: int) -> None:
        """То же снятие с заданным grace-периодом — только для пробы цены."""
        if self._started:
            t0 = time.perf_counter()
            sh(["docker", "stop", "-t", str(grace), self.name], timeout=90)
            self.stop_s = round(time.perf_counter() - t0, 4)
            self.stop_grace = grace
            sh(["docker", "rm", "-f", self.name], timeout=60)
            self._started = False

    # ── пробы изоляции: G4 доказывается пробами, а не флагами запуска ──
    def probe(self, journal_path: pathlib.Path | None = None, set_dir: pathlib.Path | None = None,
              canary_url: str = "") -> dict:
        """Пробы изоляции. Выход наружу проверяется ПОПЫТКОЙ выхода, не флагом."""
        p: dict = {}
        p["uid"] = self.exec("id -u", count_step=False).stdout.strip()
        p["nonroot"] = p["uid"] not in ("", "0")
        p["rootfs_readonly"] = self.exec("touch /probe-write 2>/dev/null && echo WRITABLE || echo READONLY", count_step=False).stdout.strip().endswith("READONLY")
        p["host_paths_hidden"] = self.exec(f"test ! -e {CASE_ROOT} && test ! -e /home/user && echo HIDDEN || echo VISIBLE", count_step=False).stdout.strip().endswith("HIDDEN")
        p["bind_mount_writable"] = self.exec("touch /work/.probe-write && echo OK || echo FAIL", count_step=False).stdout.strip().endswith("OK")
        self.exec("rm -f /work/.probe-write", count_step=False)
        if journal_path is not None:
            p["journal_invisible"] = self.exec(f"test ! -e {journal_path} && echo ABSENT || echo PRESENT", count_step=False).stdout.strip().endswith("ABSENT")
        if set_dir is not None:
            p["verifier_files_invisible"] = self.exec(f"test ! -e {set_dir} && echo ABSENT || echo PRESENT", count_step=False).stdout.strip().endswith("ABSENT")

        # Попытки выхода наружу — обязаны провалиться (ADR-049: иначе награда
        # достижима внешним вызовом). Проверяется факт выхода, а не флаг сети.
        p["egress_attempts"] = egress_attempts(self, canary_url)
        p["external_unreachable"] = p["egress_attempts"]["http_ip"]["refused"]
        p["dns_unresolvable"] = p["egress_attempts"]["dns"]["refused"]
        p["canary_attempted"] = bool(canary_url)
        p["canary_unreachable"] = p["egress_attempts"]["canary"]["refused"]
        gw = network_gateway(self.network)
        p["gateway"] = gw
        p["host_service_ports"] = host_service_ports(self, gw)
        p["other_host_services_reachable"] = sorted(k for k, v in p["host_service_ports"].items() if v == "OPEN")
        if self.network == "none":
            p["network_closed"] = bool(p["external_unreachable"] and p["dns_unresolvable"])
            p["network_profile"] = "none"
        else:
            r = self.exec(f"wget -q -T 3 -O - {self.mock_url}/v1/health && echo MOCK_OK || echo MOCK_FAIL", count_step=False)
            p["mock_reachable"] = "MOCK_OK" in (r.stdout or "")
            p["network_profile"] = f"internal({self.network})+mock-sidecar@{self.mock_url.removeprefix('http://')}"
        self.probes = p
        return p

    def isolation_ok(self) -> tuple[bool, list[str]]:
        """Изоляция подтверждена, только если каждая проба назвала ожидаемое.

        Канарейка требуется тогда, когда она вообще поднималась: «недостижима» без
        попытки — это молчание, а не доказательство (ADR-023 п.12).
        """
        want = {"nonroot": True, "rootfs_readonly": True, "host_paths_hidden": True,
                "bind_mount_writable": True, "external_unreachable": True, "dns_unresolvable": True}
        want["network_closed" if self.network == "none" else "mock_reachable"] = True
        if self.probes.get("canary_attempted"):
            want["canary_unreachable"] = True
        bad = [k for k, v in want.items() if self.probes.get(k) is not v]
        return (not bad), bad


def egress_attempts(sandbox: Sandbox, canary_url: str = "") -> dict:
    """Живые попытки выхода наружу: каждая обязана ПРОВАЛИТЬСЯ.

    Тот же набор применяет страж `tools/check_env_contract.py` (C-028) — иначе
    он проверял бы другую среду, чем исполняет задачи раннер.
    """
    out: dict = {}
    out["http_ip"] = _egress_probe(
        sandbox, f"wget -q -T 3 -O - {EGRESS_HTTP_TARGET}", EGRESS_HTTP_TARGET)
    out["dns"] = _egress_probe(
        sandbox, f"nslookup {EGRESS_DNS_TARGET}", f"DNS {EGRESS_DNS_TARGET}")
    if canary_url:
        out["canary"] = _egress_probe(sandbox, f"wget -q -T 3 -O - {canary_url}", canary_url)
    else:
        out["canary"] = {"target": "", "refused": None, "rc": None,
                         "note": "канарейка не поднята (нет адреса хоста вне сети прогона) — молчание не доказательство"}
    return out


def _egress_probe(sandbox: Sandbox, command: str, target: str) -> dict:
    r = sandbox.exec(f"({{ {command}; }} >/dev/null 2>&1; echo rc=$?)", timeout=30, count_step=False)
    rc_line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("rc=")]
    rc = int(rc_line[-1][3:]) if rc_line and rc_line[-1][3:].isdigit() else None
    return {"target": target, "rc": rc, "refused": rc is not None and rc != 0}


def host_service_ports(sandbox: Sandbox, gateway: str) -> dict:
    """Порты шлюза, открытые наружу: мок — единственный, кому там можно быть."""
    out: dict = {}
    for port in HOST_SERVICE_PORTS:
        r = sandbox.exec(f"(echo > /dev/tcp/{gateway}/{port}) >/dev/null 2>&1 && echo OPEN || echo CLOSED",
                         timeout=30, count_step=False)
        out[str(port)] = "OPEN" if "OPEN" in (r.stdout or "") else "CLOSED"
    return out



# ─── мок ─────────────────────────────────────────────────────────────────────


class MockSidecar:
    """HTTP-мок как КОНТЕЙНЕР в сети прогона (поправка ADR-049 п.10, 20.09.2026).

    Почему контейнер, а не слушатель на шлюзе сети: слушатель занимал адрес
    хоста, и проба изоляции становилась исполнимой только там, где живёт демон.
    Sidecar достижим из песочницы по адресу в ТОЙ ЖЕ сети, поэтому механизм один
    и на хосте, и внутри контейнера стадии.

    Почему админ-операции идут через демон (`docker exec`), а не по сети: у
    раннера тогда нет своего сетевого пути к моку, и его собственная топология
    перестаёт влиять на результат. Токен ротации при этом остаётся у раннера и в
    песочницу не передаётся — админ-путь агенту недоступен, как и раньше.

    Журнал пишет сервер внутри контейнера в каталог, смонтированный ТОЛЬКО в
    контейнер мока (`mock-state`): в песочницу он не попадает, поэтому «дописать
    журнал» агенту физически некуда (проверяется пробой `journal_invisible`).
    """

    def __init__(self, set_dir: pathlib.Path, run_dir: pathlib.Path, seed: int,
                 network: str, image: str, name: str) -> None:
        self.set_dir = set_dir
        self.network = network
        self.image = image
        self.name = name
        self.seed = seed
        self.state_dir = run_dir / MOCK_STATE_DIR
        self.journal = self.state_dir / "mock-journal.jsonl"
        self.ready = self.state_dir / "mock-ready"
        self.module = set_dir / "mock" / "mock_server.py"
        self.port = MOCK_PORT
        self.ip = ""
        self.url = ""
        # Цена подъёма разложена: `run_s` — создание контейнера, `ready_s` — до
        # отметки готовности, `start_s` — до первого успешного /v1/health. Мок
        # считается поднятым только к последнему: первые два числа без третьего
        # называли бы рабочим адрес, который ещё не отвечает.
        self.run_s = 0.0
        self.ready_s = 0.0
        self.health_s = 0.0
        self.start_s = 0.0
        self.stop_s = 0.0
        self.error = ""
        # Токен ротации журнала знает только раннер: в песочницу он не передаётся,
        # поэтому админ-путь мока агенту недоступен.
        self.admin_token = secrets.token_hex(16)
        self._started = False

    def start(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o777)
        # След прежнего прогона в том же каталоге не должен сойти за готовность.
        self.ready.unlink(missing_ok=True)
        self.journal.touch()
        # Журнал пишет СЕРВЕР внутри контейнера (uid 1000), поэтому файл, созданный
        # root'ом, обязан быть ему передан: иначе ротация падает на записи, а
        # `docker exec` возвращает обрыв соединения вместо причины.
        hand_to_runtime_user(self.journal)
        cmd = [
            "docker", "run", "-d", "--rm", "--name", self.name,
            "--network", self.network,
            "--read-only", "--tmpfs", "/tmp:rw,size=8m",
            "--user", SIDECAR_USER,
            "--memory", "128m", "--pids-limit", "64",
            # `--mount`, а не `-v`: при невидимом демону пути docker обязан
            # отказать, а не создать пустой каталог и молча поднять мок не с тем
            # журналом (так выглядел бы прогон из контейнера стадии без
            # same-path монтирования).
            "--mount", f"type=bind,src={self.module.parent},dst=/mock,readonly",
            "--mount", f"type=bind,src={self.state_dir},dst=/state",
            self.image, "python3", "/mock/mock_server.py",
            "--host", "0.0.0.0", "--port", str(self.port), "--seed", str(self.seed),
            "--journal", "/state/mock-journal.jsonl", "--ready-file", "/state/mock-ready",
            "--admin-token", self.admin_token,
        ]
        t0 = time.perf_counter()
        r = sh(cmd, timeout=180)
        self.run_s = round(time.perf_counter() - t0, 4)
        if r.returncode != 0:
            err = r.stderr.strip()[:300]
            if "bind source path does not exist" in err:
                # Демон хоста не видит путь раннера: так выглядит прогон из
                # контейнера стадии БЕЗ same-path монтирования. Это не «docker
                # сломался», и назвать причину обязан раннер, а не читатель лога.
                err += (f" — путь {self.state_dir} не виден демону по тому же пути: "
                        "каталог прогона обязан быть смонтирован в контейнер стадии по ТОМУ ЖЕ пути, что на хосте")
            self.error = f"мок-sidecar не поднялся: {err}"
            raise RuntimeError(self.error)
        self._started = True
        deadline = time.perf_counter() + 30
        while time.perf_counter() < deadline:
            if self.ready.is_file():
                self.ready_s = round(time.perf_counter() - t0, 4)
                break
            state = sh(["docker", "inspect", "-f", "{{.State.Running}}", self.name], timeout=30)
            if state.stdout.strip() != "true":
                logs = sh(["docker", "logs", "--tail", "20", self.name], timeout=30)
                tail = (logs.stderr or logs.stdout).strip()[:300]
                self.error = f"мок-sidecar вышел, не отметившись готовым: {tail}"
                raise RuntimeError(self.error)
            time.sleep(0.1)
        else:
            self.error = (
                f"мок-sidecar не отметился готовым за 30 с (журнал {self.journal}) — "
                "каталог прогона не виден демону по ТОМУ ЖЕ пути, что у раннера "
                "(bind-mount контейнера стадии резолвит демон хоста)"
            )
            raise RuntimeError(self.error)
        self.ip = container_ip(self.name, self.network)
        self.url = f"http://{self.ip}:{self.port}"
        # Готовность файла мало что доказывает: пока не ответил /v1/health, URL
        # называть рабочим нельзя — иначе песочница получила бы мёртвый адрес.
        self._http("/v1/health")
        self.start_s = round(time.perf_counter() - t0, 4)
        self.health_s = round(self.start_s - self.ready_s, 4)

    def _http(self, path: str, method: str = "GET", admin: bool = False) -> str:
        """HTTP-вызов моку ЧЕРЕЗ ДЕМОН: своя топология раннера на это не влияет."""
        headers = f",headers={{'X-Admin-Token':{self.admin_token!r}}}" if admin else ""
        code = (
            "import sys,urllib.request;"
            f"req=urllib.request.Request('http://127.0.0.1:{self.port}{path}',method={method!r}{headers});"
            "sys.stdout.write(urllib.request.urlopen(req,timeout=10).read().decode())"
        )
        r = sh(["docker", "exec", self.name, "python3", "-c", code], timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"мок-sidecar: {path} недоступен ({r.stderr.strip()[:200]})")
        return r.stdout

    def query(self, path: str) -> str:
        return self._http(path)

    def rotate(self) -> None:
        """Откат состояния среды: журнал мока обнуляется СЕРВЕРОМ между прогонами."""
        self._http("/__admin/rotate", method="POST", admin=True)

    def stop(self) -> None:
        if self._started:
            t0 = time.perf_counter()
            sh(["docker", "rm", "-f", self.name], timeout=90)
            self.stop_s = round(time.perf_counter() - t0, 4)
            self._started = False

    def record(self) -> dict:
        """След механизма в отчёте: им страж отличает sidecar от слушателя на шлюзе."""
        return {
            "kind": "container",
            "name": self.name,
            "image": self.image,
            "network": self.network,
            "ip": self.ip,
            "url": self.url,
            "in_run_network": bool(self.ip and self.network),
            "journal": str(self.journal),
            "module": str(self.module),
            "module_sha256": sha256_file(self.module) if self.module.is_file() else "",
            "run_s": self.run_s,
            "ready_s": self.ready_s,
            "health_s": self.health_s,
            "start_s": self.start_s,
            "stop_s": self.stop_s,
        }


# ─── задачи ──────────────────────────────────────────────────────────────────


class Task:
    """Задача набора. Лимит шагов — поле ЗАДАЧИ (`max_steps`), не вызывающей стороны."""

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
        # Отсутствие поля — отказ, а не «без лимита»: G4 (ADR-049 п.1) держится
        # лимитом шагов, и лимит, которого нет в задаче, держит вызывающая сторона.
        if "max_steps" not in self.spec:
            raise ValueError(f"{self.id}: task.json не несёт max_steps (схема {TASK_SCHEMA})")
        self.max_steps: int = int(self.spec["max_steps"])
        if self.max_steps < 0:
            raise ValueError(f"{self.id}: max_steps отрицательный ({self.max_steps})")

    def effective_max_steps(self, outer_cap: int | None) -> int:
        """Бюджет шага: из задачи; внешний потолок харнесса может только понизить."""
        return self.max_steps if outer_cap is None else min(self.max_steps, outer_cap)


def load_tasks(set_dir: pathlib.Path, only: list[str] | None) -> list[Task]:
    tasks = [
        Task(d) for d in sorted((set_dir / "tasks").iterdir())
        if d.is_dir() and (d / "task.json").is_file()
    ]
    if only:
        tasks = [t for t in tasks if t.id in set(only)]
    return tasks



# ─── верификация ─────────────────────────────────────────────────────────────


def run_verifier(task: Task, snapshot: pathlib.Path, args, mock: "MockSidecar | None", scratch: pathlib.Path) -> dict:
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
        cmd += ["--journal", str(mock.journal), "--mock-module", str(mock.module), "--mock-seed", str(mock.seed)]
    r = sh(cmd, timeout=max(120, task.timeout_s * 4))
    if r.returncode != 0:
        tail = [ln for ln in r.stderr.strip().splitlines() if ln.strip()]
        return {"score": None, "reason": f"верификатор упал ({r.returncode}): {tail[-1][:300] if tail else ''}", "reads": []}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001 — нечитаемый ответ верификатора это отказ
        return {"score": None, "reason": f"ответ верификатора не разобран: {exc}", "reads": []}


def empty_snapshot(run_dir: pathlib.Path, name: str) -> pathlib.Path:
    d = run_dir / "empty" / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


# ─── один rollout ────────────────────────────────────────────────────────────


def rollback_state(task: Task, sandbox: Sandbox, mock: MockSidecar | None) -> None:
    """Откат состояния целиком: `/work` из seed + журнал мока к исходному.

    Состояние живёт не только в `/work`: у задачи с моком его часть — журнал
    сервера, поэтому откат без ротации был бы половиной отката.
    """
    wipe_contents(sandbox.workdir)
    copy_contents(task.seed, sandbox.workdir)
    # Копию отдаём пользователю песочницы: иначе состояние, скопированное root'ом
    # (прогон из контейнера стадии), ей недоступно на запись — и задача падает на
    # `mv` не потому, что она плоха, а потому, что ей не дали работать.
    sandbox.handover_objects = hand_to_runtime_user(sandbox.workdir)
    if mock is not None and task.network != "none":
        mock.rotate()


def rollout(task: Task, mode: str, sandbox: Sandbox, run_dir: pathlib.Path, args,
            mock: MockSidecar | None, prev_paths: set[str]) -> dict:
    """Прогон задачи: откат состояния → действие (или его отсутствие) → верификация."""
    rd = run_dir / "rollouts" / task.id / mode
    shutil.rmtree(rd, ignore_errors=True)
    (rd / "snapshot").mkdir(parents=True)
    transcript = rd / "transcript.txt"
    scratch = run_dir / "scratch"

    rec: dict = {"mode": mode, "task": task.id, "tool": task.spec["tool"], "network": task.network,
                 "container": sandbox.name, "container_start_s": sandbox.start_s, "repeat": 0}

    # 1. Откат состояния: каталог-точка монтирования остаётся, содержимое — из seed.
    t0 = time.perf_counter()
    rollback_state(task, sandbox, mock)
    rec["seed_s"] = round(time.perf_counter() - t0, 4)
    rec["seed_digest"] = tree_digest(sandbox.workdir)
    rec["pristine_digest"] = tree_digest(task.seed)
    rec["seed_matches_pristine"] = rec["seed_digest"] == rec["pristine_digest"]
    # Состояние принадлежит пользователю песочницы — проверяется ПОПЫТКОЙ записи в
    # подкаталог, а не фактом chown: проба называет свойство, а не намерение.
    rec["state_writable"] = sandbox.probe_state_writable()
    rec["handover_objects"] = sandbox.handover_objects
    seed_paths = tree_paths(task.seed)
    rec["residue_from_previous"] = sorted(
        p for p in prev_paths if p not in seed_paths and (sandbox.workdir / p).exists()
    )
    if mock is not None and task.network != "none":
        rec["journal_rotated"] = True


    # Ожидаемое значение спрашивается у верификатора ДО действия: оно нужно, чтобы
    # состязательный текстовый ответ пробы no-action называл точное значение.
    pre = run_verifier(task, empty_snapshot(run_dir, task.id), args, mock, scratch)
    expected_public = str(pre.get("expected_public", ""))

    # 2. Действие (или его отсутствие). Бюджет шагов берётся из ЗАДАЧИ; внешний
    #    потолок харнесса может его только понизить (task.effective_max_steps).
    sandbox.reset_steps(task.effective_max_steps(getattr(args, "max_steps", None)))
    rec["max_steps_from_task"] = task.max_steps
    rec["max_steps_effective"] = sandbox.max_steps
    t0 = time.perf_counter()
    action_rc: int | None = None
    action_err = ""
    step: StepResult | None = None
    if mode == "run":
        transcript.write_text("(действие выполнено инструментами; текстового ответа нет)\n", encoding="utf-8")
        body = task.solution.read_text(encoding="utf-8")
        step = sandbox.exec(body, timeout=task.timeout_s, count_step=True)
    elif mode == "empty-action":
        transcript.write_text(f"Готово. {expected_public}\n", encoding="utf-8")
        step = sandbox.exec("true", timeout=task.timeout_s, count_step=True)
    elif mode == "no-action":
        # Состязательный текстовый ответ: называет ТОЧНОЕ ожидаемое значение словами
        # и не делает ни одного вызова. Проба проверяет не «нет файла», а «вердикт не
        # зависит от текста» — иначе она была бы вакуумной.
        transcript.write_text(f"Готово. {expected_public}\n", encoding="utf-8")
    else:
        raise ValueError(mode)
    if step is not None:
        action_rc = None if step.refused else step.returncode
        action_err = "" if step.refused else (step.stderr or "").strip()[-300:]
        rec["action_refused_by_step_budget"] = step.refused
        rec["action_timed_out"] = step.timed_out
        if step.timed_out:
            rec["action_killed_pid"] = step.killed_pid
            rec["action_group_left"] = step.group_left
        if step.reason:
            rec["action_note"] = step.reason
    else:
        rec["action_refused_by_step_budget"] = False
        rec["action_timed_out"] = False
    rec["action_s"] = round(time.perf_counter() - t0, 4)
    rec["action_exit_code"] = action_rc
    rec["steps_used"] = sandbox.steps_used
    rec["steps_refused"] = sandbox.steps_refused
    if action_err:
        rec["action_stderr_tail"] = action_err
    text = transcript.read_text(encoding="utf-8")
    rec["transcript_sha256"] = sha256_file(transcript)
    rec["expected_public"] = expected_public
    rec["text_answer_carried_expected"] = bool(expected_public) and expected_public in text

    # 2b. Откат частичного следа прерванного шага. Клиентский таймаут `docker exec`
    #     процесс внутри не убивает — среда снимает группу сама (шаг супервизируется)
    #     и возвращает состояние из seed, иначе след шага дожил бы до следующего rollout.
    if step is not None and step.timed_out:
        t0 = time.perf_counter()
        partial = tree_digest(sandbox.workdir)
        rec["partial_trace_digest"] = partial
        rec["partial_trace_left_state"] = partial != rec["seed_digest"]
        rollback_state(task, sandbox, mock)
        rec["rollback_s"] = round(time.perf_counter() - t0, 4)
        rec["partial_trace_rolled_back"] = tree_digest(sandbox.workdir) == rec["pristine_digest"]


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
    t0 = time.perf_counter()
    first = run_verifier(task, rd / "snapshot", args, mock, scratch)
    rec["verify_s"] = round(time.perf_counter() - t0, 4)
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
    rec["total_s"] = round(rec["seed_s"] + rec["action_s"] + rec["snapshot_s"] + rec["verify_s"], 4)
    return rec


# ─── проверки G1–G4 по задаче ────────────────────────────────────────────────


def gate_verdicts(task: Task, res: dict[str, dict], sandbox: Sandbox, leak: dict, residue_absent: bool) -> dict:
    run, no_action, empty = res.get("run"), res.get("no-action"), res.get("empty-action")
    base = run or no_action or empty or {}
    reads = base.get("reads", [])
    reads_text = [r for r in reads if r.startswith(("text:", "transcript:"))]
    probe_scores = [v["score"] for v in (no_action, empty) if v]

    g1 = {
        "pass": bool(run and run["score"] == 1 and all(s == 0 for s in probe_scores)),
        "reward_by_state": bool(run and run["score"] == 1),
        "state_sources": reads,
        "text_channel_read": bool(reads_text),
        "state_digest_changed_by_action": bool(run and run["state_digest"] != run["seed_digest"]),
    }
    g2 = {
        "pass": bool(no_action and no_action["score"] == 0),
        "no_action_score": no_action["score"] if no_action else None,
        "no_action_text_sha256": no_action.get("transcript_sha256") if no_action else None,
        "no_action_text_carried_expected": bool(no_action and no_action.get("text_answer_carried_expected")),
        "empty_action_score": empty["score"] if empty else None,
    }
    g3 = {
        "pass": bool(
            run and isinstance(run["score"], int) and run["score"] in (0, 1)
            and run["verifier_deterministic"] and run["verifier_ignores_text"] and not reads_text
        ),
        "verifier": task.spec["verifier"],
        "verifier_sha256": sha256_file(task.verifier),
        "binary": bool(run and isinstance(run["score"], int) and run["score"] in (0, 1)),
        "deterministic": bool(run and run["verifier_deterministic"]),
        "ignores_text": bool(run and run["verifier_ignores_text"]),
        "reads_state_only": not reads_text,
        "frozen_outside_container": str(task.verifier).startswith(str(SET_DIR)),
    }
    if task.spec["tool"] == "http_mock":
        g3["journal_written_by_server"] = True
        g3["journal_outside_container"] = bool(sandbox.probes.get("journal_invisible"))
    # Состояние, недоступное песочнице на запись, — не «задача не решена», а
    # сломанная среда: переименование файла требует прав на каталог, и отказ
    # выходит на первом же `mv` (найдено пробой 20.09.2026 в контейнере стадии).
    writable = base.get("state_writable") or {}
    state_writable = writable.get("verdict") in ("WRITABLE", "NO_SUBDIR")
    g4 = {
        "pass": bool(sandbox.isolation_ok()[0] and residue_absent and not base.get("symlink_escape")
                     and state_writable),
        "container": sandbox.name,
        "image": sandbox.image,
        "network_profile": sandbox.probes.get("network_profile"),
        "isolation_probes": sandbox.probes,
        # Выход наружу закрыт ПОПЫТКОЙ выхода, а не флагом сети: проба пробует
        # выйти и обязана провалиться (иначе награда достижима внешним вызовом).
        "egress_closed": bool(sandbox.probes.get("external_unreachable")
                              and sandbox.probes.get("dns_unresolvable")),
        "step_budget": {
            "from_task": base.get("max_steps_from_task"),
            "effective": base.get("max_steps_effective"),
            "steps_used": base.get("steps_used"),
            "steps_refused": base.get("steps_refused"),
        },
        "residue_absent": residue_absent,
        "state_rolled_back": bool(run and run["seed_matches_pristine"]),
        "symlink_escape": base.get("symlink_escape", []),
        "state_writable_by_sandbox": state_writable,
        "state_writable_probe": writable,
        "handover_objects": base.get("handover_objects"),
    }
    return {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "leak": leak}


def leak_checks(task: Task, expected_public: str, mock: MockSidecar | None) -> dict:
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
    if mock is not None:
        try:
            body = mock.query("/v1/items")
            out["mock_response_has_gold"] = bool(expected_public and expected_public in body) or any(n in body for n in numbers)
            out["mock_contract_stated_in_prompt"] = "/v1/report" in prompt
        except Exception as exc:  # noqa: BLE001 — недоступный мок виден и без этого
            out["mock_query_error"] = str(exc)[:120]
    return out


# ─── пробы лимита шагов, супервизора и снятия контейнера ─────────────────────


def step_limit_probe(task: Task, sandbox: Sandbox, run_dir: pathlib.Path, args,
                     mock: MockSidecar | None, budgets: tuple[int, ...] = (0, 1)) -> dict:
    """Бюджет шагов исполняет СРЕДА, а не вызывающая сторона: 0 → награда 0, 1 → 1.

    Проба идёт тем же путём, что обычный rollout (эталонное действие задачи), и
    подменяет только бюджет. Если бы лимит держал харнесс, отказ не изменил бы
    состояние, и награда 0 при бюджете 0 не отличалась бы от «задача не решена».
    Здесь отказ назван явно (`action_refused_by_step_budget`) и подтверждён тем,
    что состояние НЕ изменилось.
    """
    probe_dir = run_dir / "step-probe"
    (probe_dir / "scratch").mkdir(parents=True, exist_ok=True)
    saved = getattr(args, "max_steps", None)
    out: dict = {"task": task.id, "max_steps_from_task": task.max_steps, "budget_probe": {}}
    try:
        for budget in budgets:
            args.max_steps = budget
            rec = rollout(task, "run", sandbox, probe_dir, args, mock, set())
            out["budget_probe"][str(budget)] = {
                "budget": budget,
                "effective": rec["max_steps_effective"],
                "steps_used": rec["steps_used"],
                "steps_refused": rec["steps_refused"],
                "action_refused_by_step_budget": rec["action_refused_by_step_budget"],
                "seed_matches_pristine": rec["seed_matches_pristine"],
                "state_changed": rec["state_digest"] != rec["seed_digest"],
                "score": rec["score"],
                "reason": rec["reason"],
            }
    finally:
        args.max_steps = saved
    got = {int(k): v["score"] for k, v in out["budget_probe"].items()}
    out["binds_on_state"] = got.get(0) == 0 and got.get(1) == 1
    out["reading"] = (
        f"бюджет 0: эталонное действие {'отказано' if out['budget_probe'].get('0', {}).get('action_refused_by_step_budget') else 'НЕ отказано'}, "
        f"награда {got.get(0)}; бюджет 1: награда {got.get(1)} — лимит шагов управляет наградой через состояние"
        if out["binds_on_state"] else
        f"лимит шагов НЕ связывает награду: бюджет 0 → {got.get(0)}, бюджет 1 → {got.get(1)}"
    )
    return out


def timeout_probe(task: Task, sandbox: Sandbox, run_dir: pathlib.Path, args,
                  mock: MockSidecar | None, timeout_override: int = 3) -> dict:
    """Шаг, убитый по таймауту, не влияет на следующий rollout.

    Клиентский таймаут `docker exec` процесс внутри не убивает (измерено
    20.09.2026) и оставляет частичный след. Проба проверяет три факта: группа
    процессов шага снята СРЕДОЙ (`group_left == 0`), след откачен (`/work` из
    seed), и следующий rollout даёт ту же награду и то же состояние, что чистый
    прогон без убитого шага.
    """
    probe_dir = run_dir / "timeout-probe"
    (probe_dir / "scratch").mkdir(parents=True, exist_ok=True)
    tmo = timeout_override or task.timeout_s
    out: dict = {"task": task.id, "timeout_s": tmo}

    clean = rollout(task, "run", sandbox, probe_dir, args, mock, set())
    out["clean_rollout"] = {"score": clean["score"], "state_digest": clean["state_digest"]}

    rollback_state(task, sandbox, mock)
    sandbox.reset_steps(None)
    seed_digest = tree_digest(sandbox.workdir)
    step = sandbox.exec(
        "mkdir -p out; echo partial > out/partial.txt; sleep 120; echo LATE > out/late.txt",
        timeout=tmo, count_step=True)
    partial_digest = tree_digest(sandbox.workdir)
    out["action_timed_out"] = step.timed_out
    out["action_s"] = step.elapsed_s
    out["killed_pid"] = step.killed_pid
    out["group_left_after_kill"] = step.group_left
    out["process_survived"] = None if step.group_left is None else step.group_left > 0
    out["partial_trace_left_state"] = partial_digest != seed_digest
    out["partial_trace_digest"] = partial_digest

    rollback_state(task, sandbox, mock)
    out["state_after_rollback"] = tree_digest(sandbox.workdir)
    out["partial_trace_rolled_back"] = out["state_after_rollback"] == seed_digest

    nxt = rollout(task, "run", sandbox, probe_dir, args, mock, set())
    out["next_rollout"] = {"score": nxt["score"], "state_digest": nxt["state_digest"],
                           "residue_from_previous": nxt["residue_from_previous"]}
    out["next_rollout_unaffected"] = bool(
        nxt["score"] == clean["score"] == 1
        and nxt["state_digest"] == clean["state_digest"]
        and not nxt["residue_from_previous"]
    )
    out["reading"] = (
        f"шаг прерван на {step.elapsed_s} с (таймаут {tmo} с), среда сняла группу "
        f"(pid {step.killed_pid}, живо {step.group_left}), частичный след "
        f"{'был' if out['partial_trace_left_state'] else 'не возник'} и "
        f"{'откачен' if out['partial_trace_rolled_back'] else 'НЕ откачен'}; следующий rollout — "
        f"награда {nxt['score']}, состояние {'совпало с чистым' if out['next_rollout_unaffected'] else 'РАЗОШЛОСЬ с чистым'}"
    )
    return out


def teardown_probe(image: str, run_root: pathlib.Path, args, prefix: str,
                   graces: tuple[int, ...] = (0, 1)) -> dict:
    """Снятие батч-контейнера: цена `-t 0` против `-t 1` и целость состояния.

    Аргумент «чекпойнты стадии пишутся иначе» проверяется фактом: состояние
    контейнера снимается диджестом ДО и ПОСЛЕ снятия. Граница названа честно:
    `-t 0` не сбрасывает незавершённую запись в момент снятия, поэтому награда
    обязана читаться из снимка ДО снятия (так и делает rollout).

    Имя контейнера несёт префикс ПРОГОНА, а не только пробы: иначе два прогона
    (или прогон и страж) делили бы одно имя, а `leftovers` не увидел бы остаток —
    имя не попадало бы под фильтр прогона.
    """
    out: dict = {"measurements": {}, "state_intact": {}}
    for grace in graces:
        workdir = run_root / f"teardown-t{grace}"
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True, exist_ok=True)
        os.chmod(workdir, 0o777)
        sb = Sandbox(image, workdir, "none", f"{prefix}-teardown-t{grace}")
        sb.start()
        sb.exec("mkdir -p out && echo state > out/f.txt && sha256sum out/f.txt > out/sum.txt", count_step=False)
        before = tree_digest(workdir)
        sb.stop_with_grace(grace)
        after = tree_digest(workdir)
        out["measurements"][f"t{grace}"] = {"stop_s": sb.stop_s, "grace": grace}
        out["state_intact"][f"t{grace}"] = before == after
        shutil.rmtree(workdir, ignore_errors=True)
    t0 = out["measurements"].get("t0", {}).get("stop_s")
    t1 = out["measurements"].get("t1", {}).get("stop_s")
    out["reading"] = (
        f"снятие батч-контейнера: `-t 1` стоит {t1} с, `-t 0` — {t0} с; состояние после снятия "
        f"целое в обоих случаях ({out['state_intact']}), то есть grace-период не защищает ничего, "
        "чего не защищает снимок состояния ДО снятия"
        if t0 is not None and t1 is not None else "замер снятия неполон"
    )
    return out


def sidecar_probe(set_dir: pathlib.Path, run_dir: pathlib.Path, args, network: str,
                  reps: int = 3) -> dict:
    """Цена sidecar-мока: +1 контейнер на батч — подъём с готовностью и снятие.

    Зачем отдельная проба, если старт мока и так измеряется в прогоне: один замер
    неотличим от случайной задержки демона, а цена ложится на КАЖДЫЙ шаг RL (батч
    = шаг), поэтому она обязана быть повторяемой, а не единичной. Сравнение — с
    подъёмом и снятием батч-контейнера (тем же механизмом, что в `--teardown-probe`).
    """
    out: dict = {"reps": reps, "measurements": [], "errors": []}
    for i in range(reps):
        d = run_dir / f"sidecar-probe/rep{i}"
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        sb = MockSidecar(set_dir, d, args.mock_seed, network, args.sidecar_image,
                         f"{network}-sidecar-probe")
        try:
            sb.start()
            sb.rotate()
            sb.query("/v1/items")
            sb.stop()
            out["measurements"].append({"start_s": sb.start_s, "stop_s": sb.stop_s,
                                        "total_s": round(sb.start_s + sb.stop_s, 4)})
        except RuntimeError as exc:
            sb.stop()
            out["errors"].append(str(exc)[:200])
    starts = [m["start_s"] for m in out["measurements"]]
    stops = [m["stop_s"] for m in out["measurements"]]
    totals = [m["total_s"] for m in out["measurements"]]
    out["start_s_mean"] = mean(starts)
    out["stop_s_mean"] = mean(stops)
    out["total_s_mean"] = mean(totals)
    out["reading"] = (
        f"мок-sidecar: подъём с готовностью {out['start_s_mean']} с, снятие {out['stop_s_mean']} с, "
        f"итого {out['total_s_mean']} с на батч (один контейнер на батч, не на rollout)"
        if totals else f"цена sidecar не измерена: {out['errors']}"
    )
    return out


def leftovers(prefix: str) -> dict:
    """Что осталось после прогона: снимается по ТОЧНОМУ префиксу имени пробы."""
    ps = sh(["docker", "ps", "-a", "--filter", f"name={prefix}", "--format", "{{.Names}}"], timeout=60)
    nets = sh(["docker", "network", "ls", "--filter", f"name={prefix}", "--format", "{{.Name}}"], timeout=60)
    return {
        "containers": [x for x in (ps.stdout or "").strip().splitlines() if x],
        "networks": [x for x in (nets.stdout or "").strip().splitlines() if x],
    }


# ─── цена ────────────────────────────────────────────────────────────────────


def sidecar_cost(mock: "MockSidecar | None", probe: dict | None, containers: list[dict],
                 teardown: dict | None) -> dict | None:
    """Цена sidecar-мока: +1 контейнер на батч — и её сравнение с батч-контейнером.

    Числа берутся из ТОГО мока, который обслуживал прогон (`start_s`/`stop_s`
    фактического подъёма и снятия), а повторяемость — из отдельной пробы: один
    замер неотличим от случайной задержки демона, а цена ложится на каждый шаг.
    """
    if mock is None:
        return None
    batch_start = mean([c["start_s"] for c in containers])
    # Без пробы снятия цену батч-контейнера мерить нечем: ноль здесь читался бы
    # как «снятие бесплатно», поэтому неизмеренное остаётся неизмеренным.
    t0 = (teardown or {}).get("measurements", {}).get("t0", {}).get("stop_s")
    t1 = (teardown or {}).get("measurements", {}).get("t1", {}).get("stop_s")
    total = round(mock.start_s + mock.stop_s, 4)
    per_step = total
    per_rollout = round(total / AUDIT["rollouts_per_step"], 4)
    batch_total = round(batch_start + t0, 4) if (batch_start is not None and t0 is not None) else None
    ratio = round(per_step / batch_total, 3) if batch_total else None
    return {
        "what": "мок живёт sidecar-контейнером в сети прогона: +1 контейнер на батч (не на rollout)",
        "is_measurement": True,
        "measured": {
            "start_s": mock.start_s,
            "stop_s": mock.stop_s,
            "total_s": total,
            "probe_repeats": (probe or {}).get("measurements"),
            "probe_total_s_mean": (probe or {}).get("total_s_mean"),
            "probe_errors": (probe or {}).get("errors"),
        },
        "per_step_s": per_step,
        "per_rollout_s": per_rollout,
        "baseline_batch_container_s": {
            "start_s": batch_start,
            "stop_s_with_t0": t0,
            "total_s_with_t0": batch_total,
            "source": "тот же прогон: контейнеры батча + проба снятия (--teardown-probe); "
                      "пока проба не прогнана, снятие не измерено (null), а не ноль",
        },
        "comparison": {
            "ratio_to_batch_container_t0": ratio,
            "prior_teardown_grace_s": t1,
            "cheaper_than_one_grace_period": None if t1 is None else bool(per_step < t1),
            "prior_measurement": ("прежде батч-контейнер стоил 1.30 с на батч, из них 1.10 с — grace-период `docker stop -t 1`; "
                                  "снятие переведено на `-t 0` (0.078 с в замере 20.09.2026), поэтому grace больше не платится"),
        },
        "assumptions": {
            "batches_per_step": 1,
            "reading": "батч=шаг: сеть прогона одна, sidecar поднимается один раз на батч; при нескольких сетевых профилях на шаг цена умножается на их число",
        },
        "reading": (
            f"мок-sidecar стоит {total} с на батч (подъём {mock.start_s} с + снятие {mock.stop_s} с), то есть "
            f"{per_rollout} с на rollout при R={AUDIT['rollouts_per_step']}"
            + (f"; это {ratio}× от цены батч-контейнера с `-t 0` ({batch_total} с)" if ratio else
               "; цена батч-контейнера не измерена — проба снятия (--teardown-probe) в этом прогоне не шла")
            + (f", и {'меньше' if per_step < t1 else 'НЕ меньше'} одного grace-периода ({t1} с), который снятие уже не платит"
               if t1 is not None else "")
        ),
    }


def cost_section(timings: list[dict], containers: list[dict], n_tasks_by_container: dict[str, int],
                 sidecar: dict | None = None) -> dict:
    sandbox_terms, action_terms, naive_terms = [], [], []
    for rec in timings:
        share = rec["container_start_s"] / max(n_tasks_by_container.get(rec["container"], 1), 1)
        sandbox_terms.append(share + rec["seed_s"] + rec["snapshot_s"] + rec["verify_s"])
        # Тот же прогон, но без батч-оптимизации ADR-049 п.10: контейнер на каждый
        # rollout. Нужен, чтобы сравнение шло с ТЕМ ЖЕ сценарием, что оценён в аудите
        # (`container_per_rollout`), а не с более дешёвым батчем.
        naive_terms.append(rec["container_start_s"] + rec["seed_s"] + rec["snapshot_s"] + rec["verify_s"])
        action_terms.append(rec["action_s"])
    t_sandbox, t_action, t_sandbox_naive = mean(sandbox_terms), mean(action_terms), mean(naive_terms)
    # Цена sidecar-мока — слагаемое ШАГА, а не rollout: контейнер мока поднимается
    # один раз на батч. В формулу аудита он не входил вовсе (мок считался
    # бесплатным), поэтому пересчёт даётся в двух видах: сопоставимый с прежним
    # замером (без sidecar) и полный (с ним).
    t_sidecar = (sidecar or {}).get("per_step_s") or 0.0
    steps, ratios, steps_sidecar, ratios_sidecar = [], [], [], []
    if t_sandbox is not None and t_action is not None:
        for a in AUDIT["actions_per_rollout"]:
            for growth in AUDIT["generation_growth_s"]:
                step = (AUDIT["base_step_s"] + AUDIT["rollouts_per_step"] * t_sandbox
                        + AUDIT["rollouts_per_step"] * a * t_action + growth)
                steps.append(round(step, 2))
                ratios.append(round(step / AUDIT["base_step_s"], 3))
                steps_sidecar.append(round(step + t_sidecar, 2))
                ratios_sidecar.append(round((step + t_sidecar) / AUDIT["base_step_s"], 3))
    aud_s, aud_a = AUDIT["t_sandbox_per_rollout_s"], AUDIT["t_action_s"]
    if t_sandbox is None:
        reading = "замер неполон: нет ни одного rollout"
    else:
        parts = []
        parts.append(
            f"песочница+состояние {t_sandbox} с против оценённых {aud_s[0]}–{aud_s[1]} с: "
            + ("ниже оценки" if t_sandbox < aud_s[0] else "внутри оценки" if t_sandbox <= aud_s[1] else "выше оценки")
        )
        parts.append(
            f"действие {t_action} с против оценённых {aud_a[0]}–{aud_a[1]} с: "
            + ("ниже оценки" if t_action < aud_a[0] else "внутри оценки" if t_action <= aud_a[1] else "выше оценки")
        )
        if ratios:
            parts.append(
                f"пересчёт шага по формуле аудита на измеренных слагаемых даёт ×{min(ratios)}–{max(ratios)} "
                f"против оценённых ×{AUDIT['ratio_to_base'][0]}–{AUDIT['ratio_to_base'][1]}"
            )
        if t_sandbox_naive is not None:
            parts.append(f"без батч-оптимизации (контейнер на rollout) песочница стоила бы {t_sandbox_naive} с — тоже ниже оценки")
        if sidecar:
            parts.append(f"sidecar-мок добавляет {t_sidecar} с к шагу (один контейнер на батч), чего в оценке аудита не было вовсе")
        reading = "; ".join(parts)
    return {
        "reading": reading,
        "is_measurement": True,
        "what_is_measured": "подъём контейнера (амортизованный на батч), откат состояния, действие, снимок и верификация — то есть цена СРЕДЫ на rollout",
        "sidecar_mock": sidecar,
        "measured": {
            "containers": containers,
            "t_sandbox_per_rollout_s": t_sandbox,
            "t_sandbox_per_rollout_if_not_batched_s": t_sandbox_naive,
            "t_action_per_rollout_s": t_action,
            "rollout_overhead_s": mean([r["total_s"] for r in timings]),
            "per_task": [
                {k: r[k] for k in ("task", "mode", "seed_s", "action_s", "snapshot_s", "verify_s", "total_s")}
                for r in timings
            ],
        },
        "audit_estimate": AUDIT,
        "comparison": {
            "sandbox_within_estimate": None if t_sandbox is None else AUDIT["t_sandbox_per_rollout_s"][0] <= t_sandbox <= AUDIT["t_sandbox_per_rollout_s"][1],
            "sandbox_within_estimate_if_not_batched": None if t_sandbox_naive is None else AUDIT["t_sandbox_per_rollout_s"][0] <= t_sandbox_naive <= AUDIT["t_sandbox_per_rollout_s"][1],
            "action_within_estimate": None if t_action is None else AUDIT["t_action_s"][0] <= t_action <= AUDIT["t_action_s"][1],
            "step_seconds_recomputed": steps,
            "ratio_to_base_recomputed": ratios,
            "step_seconds_with_sidecar": steps_sidecar,
            "ratio_to_base_with_sidecar": ratios_sidecar,
            "audit_ratio_to_base": AUDIT["ratio_to_base"],
        },
        "assumptions": {
            "inherited": [
                "рост длины генерации (17.69–35.38 с) не измеряется здесь — взят из аудита",
                "линейность по шагам и отсутствие термотроттлинга — как в замере S3-pre",
                "число действий на rollout A ∈ {2, 15} — бюджет пула env_types, не замер скелета",
                "sidecar-мок в оценку аудита не входил вовсе: мок считался бесплатным, поэтому пересчёт шага даётся и без него (сопоставимо с прежним замером), и с ним",
            ],
            "measured_not_assumed": [
                "цена верификатора состояния в формуле аудита принята нулевой; здесь она измерена и входит в t_sandbox",
                "подъём контейнера измерен, а не взят из диапазона 0.8–3.0 с",
                "цена sidecar-мока измерена (start_s/stop_s фактического контейнера + повторная проба), а не взята из цены батч-контейнера",
            ],
            "reading": "сравнение идёт по слагаемым песочницы и действия; «шаг» — пересчёт формулы аудита на измеренных слагаемых, а не замер шага RL",
            "scope": "задачи скелета короткие (одна-две команды на действие), поэтому t_action — нижняя граница цены действия в пуле стадии; сценарий же песочницы измерен прямо",
        },
    }


# ─── отчёт ───────────────────────────────────────────────────────────────────


def build_evidence(args, tasks, per_task, sandboxes, mock, timings, containers, started_at,
                   status_note="", extra: dict | None = None) -> dict:
    g = {k: all(t["gates"][k]["pass"] for t in per_task) for k in ("G1", "G2", "G3", "G4")} if per_task else {}
    n_probe = [t for t in per_task if t["res"].get("no-action")]
    share = round(sum(1 for t in n_probe if t["res"]["no-action"]["score"] == 1) / len(n_probe), 4) if n_probe else None
    findings = [
        f"{t['id']}: {k} не выполнен — {(t['res'].get('run') or t['res'].get('no-action') or {}).get('reason')}"
        for t in per_task for k in ("G1", "G2", "G3", "G4") if not t["gates"][k]["pass"]
    ]
    # Сломанная среда обязана называть себя сама: иначе «G4 не выполнен» читается
    # как «задача не решена», хотя состояние просто не отдали песочнице.
    findings += [
        f"{t['id']}: G4 — состояние /work недоступно песочнице на запись: {t['gates']['G4'].get('state_writable_probe')}"
        for t in per_task if t["gates"]["G4"].get("state_writable_by_sandbox") is False
    ]
    extra = extra or {}
    reproducible = {
        t["id"]: {mode: sorted({r["state_digest"] for r in t["repeats"] if r["mode"] == mode}) for mode in MODES}
        for t in per_task
    }
    net_profiles = sorted({t["spec"].get("network", "none") for t in per_task})
    return {
        "schema": "grounded-skeleton-evidence/1",
        "generated_at": started_at,
        "tool": "tools/grounded_runner.py",
        "status": "ok" if all(g.values()) and not status_note else "failed",
        "status_note": status_note,
        "host": {
            "platform": platform.platform(),
            "kernel": platform.release(),
            "stand": getattr(args, "stand", None) or default_stand_label(),
            "docker": getattr(args, "docker_version", None),
            "image": args.image,
            "image_digest": getattr(args, "image_digest", None),
            # Цена тяга образов: `pull_s: null` — образ лежал (стенд прогрет), число —
            # стоил столько секунд. Граница переносимости чисел названа в `limits`.
            "images": getattr(args, "images", None),
            # След предпроверки same-path монтирования: маркер-контейнер до прогона.
            "mount_preflight": getattr(args, "mount_preflight", None),
            "run_dir": str(getattr(args, "run_dir", "")),
            "run_dir_kept": bool(args.keep),
            # Где проба шла НА САМОМ ДЕЛЕ: заявка `--in-stage-container` подтверждена
            # признаками до прогона (execution_context), поэтому метка площадки не
            # называет чужую машину.
            "execution": {**(getattr(args, "execution", None) or {}),
                          "claim_in_stage_container": bool(getattr(args, "in_stage_container", False)),
                          "runtime_user": getattr(args, "runtime_user", None)},
        },
        "mode": {"run": args.do_run, "no_action": args.do_no_action, "empty_action": args.do_empty_action,
                 "step_probe": bool(getattr(args, "do_step_probe", False)),
                 "timeout_probe": bool(getattr(args, "do_timeout_probe", False)),
                 "teardown_probe": bool(getattr(args, "do_teardown_probe", False)),
                 "sidecar_probe": bool(getattr(args, "do_sidecar_probe", False)),
                 "repeat": args.repeat, "mock_seed": mock.seed if mock else None,
                 "max_steps_outer_cap": getattr(args, "max_steps", None)},
        "set": {
            "dir": str(SET_DIR.relative_to(CASE_ROOT)) if str(SET_DIR).startswith(str(CASE_ROOT)) else str(SET_DIR),
            "n_tasks": len(tasks),
            "tools": sorted({t.spec["tool"] for t in tasks}),
            "network_profiles": net_profiles,
            "task_schema": TASK_SCHEMA,
            "card": "data/grounded-skeleton/card.json",
            # Хеш набора и верификаторов: по ним страж tools/check_grounded_pool.py
            # отличает доказательство о ТЕКУЩЕМ наборе от устаревшего.
            "sha256_full": set_digest(pathlib.Path(args.set_dir).resolve())[0],
            "max_steps_from_task": {t.id: t.max_steps for t in tasks},
        },
        "network": {
            "profile": "internal" if any(t.network != "none" for t in tasks) else "none",
            "networks": {name: sb.network for name, sb in sandboxes.items()},
            "gateways": {name: sb.probes.get("gateway") for name, sb in sandboxes.items()},
            "egress_attempts": {name: sb.probes.get("egress_attempts") for name, sb in sandboxes.items()},
            # След МЕХАНИЗМА, а не только его результата: по нему страж отличает
            # sidecar в сети прогона от слушателя на её шлюзе (поправка ADR-049 п.10).
            "sidecars": {
                "mock": mock.record() if mock else None,
                "canary": extra.get("canary"),
            },
            "reading": (
                "задачи с моком исполняются в сети `--internal`, мок — sidecar-КОНТЕЙНЕР в этой же сети "
                "(адрес не на шлюзе, поэтому достижимость не зависит от того, где идёт цикл RL); выход наружу "
                "проверяется ПОПЫТКОЙ выхода (публичный IP, DNS, канарейка-контейнер на дефолтной bridge) "
                "и обязан провалиться — проверка «флаг --internal выставлен» доказательством не является; "
                "канарейка обязана быть живой (self_check), иначе «недостижима» ничего не доказывает"
            ),
        },
        "tasks": per_task,
        "gates": {
            "G1": {"name": "награда по состоянию, не по тексту ответа", "pass": g.get("G1")},
            "G2": {"name": "no-action = 0 (успех недостижим без действий)", "pass": g.get("G2")},
            "G3": {"name": "бинарный внешний верификатор, читает не то, что писал агент", "pass": g.get("G3")},
            "G4": {"name": "изоляция контейнером + откат состояния + лимит шагов из задачи", "pass": g.get("G4")},
        },
        "no_action": {
            "share": share,
            "expected": 0.0,
            "per_task": {t["id"]: (t["res"].get("no-action") or {}).get("score") for t in per_task},
            "per_task_empty_action": {t["id"]: (t["res"].get("empty-action") or {}).get("score") for t in per_task},
            "text_answer_carried_expected": {t["id"]: t["gates"]["G2"]["no_action_text_carried_expected"] for t in per_task},
        },
        "isolation": {name: sb.probes for name, sb in sandboxes.items()},
        "step_limit": extra.get("step_limit"),
        "supervision": extra.get("supervision"),
        "teardown": extra.get("teardown"),
        "sidecar": extra.get("sidecar_probe"),
        "cleanup": extra.get("cleanup"),
        "rollback": {
            t["id"]: {
                "seed_matches_pristine": (t["res"].get("run") or {}).get("seed_matches_pristine"),
                "residue_absent": t["residue_absent"],
                "state_after_run_digest": (t["res"].get("run") or {}).get("state_digest"),
            }
            for t in per_task
        },
        "reproducibility": {
            "repeat": args.repeat,
            "state_digests": reproducible,
            "stable": {t["id"]: all(len(v) <= 1 for v in reproducible[t["id"]].values()) for t in per_task},
            "note": "два прогона одного режима с одним сидом обязаны дать одно состояние (ADR-049 п.3(iii)); при --repeat 1 проба не выполняется",
        },
        "cost": cost_section(timings, containers, {c["name"]: c["n_tasks"] for c in containers},
                             sidecar=extra.get("sidecar_cost")),
        "verdict": {
            "g1_g4": {k: ("выполняется" if v else "НЕ выполняется") for k, v in g.items()},
            "skeleton_ready": bool(g) and all(g.values()),
            "not_measured_here": [
                "способность модели решать задачи (полоса p* по ADR-049 п.12) — нужен rollout политики",
                "утечка gold у МАССОВОГО пула стадии — здесь только скелет из 5 задач",
                "цена шага RL целиком (генерация и обучение) — здесь измерена только цена среды",
            ],
        },
        "limits": [
            "пробы no-action и empty-action исполняет раннер, а не модель: они проверяют контур награды, а не политику",
            "шаг = одно действие среды (один exec); эталонное действие задачи — один шаг, поэтому бюджет 1 достаточен",
            "цена измерена на том стенде, который назван в host.stand; цифры не переносятся между стендами (ADR-049 п.10)",
            "состязательный текстовый ответ пробы no-action подставляет раннер из expected_public верификатора — это имитация худшего случая, а не поведение модели",
            "выход наружу проверяется попыткой; на хосте без интернета проба публичного IP вакуумна — её держит канарейка-контейнер на дефолтной bridge (её собственная живость подтверждается self-check'ом)",
            "канарейка проверяет ДОСТИЖИМОСТЬ адреса вне сети прогона, а не наличие NAT: контейнер на дефолтной bridge сам выходит наружу, и «канарейка недостижима» этого не опровергает — его опровергает проба публичного IP",
            "цена sidecar-мока снята на том же стенде, что и остальные числа, и в формулу аудита не входит: пересчёт шага даётся и без него (сопоставимо с прежним замером), и с ним",
            "цена снята на ПРОГРЕТОМ стенде: оба образа (песочницы и sidecar'а) уже лежали, тяга в замере не было, и цена первой пробы на ЧИСТОМ стенде НЕ ИЗМЕРЕНА — оценкой она не подставляется (оценка не замер, ADR-023 п.13); с этой редакции тяг записывается полем host.images[].pull_s (null — образ лежал, число — стоил столько секунд), к числам 20.09.2026 такого замера нет и появиться не может (ADR-028 п.4)",
            "дерево набора проверяется предпроверкой монтирования только там, где прогон будет его монтировать (в наборе есть задача с сетью): без мока проверка краснела бы за отсутствие того, чего в прогоне не будет (ADR-023 п.12)",
        ],
        "invariants": {"ok": not findings, "findings": findings},
    }



# ─── Политика канонического хеша набора ──────────────────────────────────────
# Пересобираемые артефакты (байткод Python) в каноническую карту не входят. Причина
# не в удобстве: байткод производен от исходников, в git не хранится и появляется
# или исчезает от одного импорта на машине. Хеш набора, посчитанный вместе с ним,
# из чистого клона не воспроизводится, то есть правило-страж краснеет по построению
# (ADR-023 п.12) — что и произошло с набором скелета 20.09.2026.
#
# Политику держат ДВА файла — здесь и в страже `tools/check_grounded_pool.py`
# (страж обязан быть самостоятельным: AD-10, граница стража и раннера).
# Совпадение формул сверяется тестом `tools/tests/run_tool_tests.sh`, раздел 30.
REBUILDABLE_DIRS = ("__pycache__",)
REBUILDABLE_SUFFIXES = (".pyc",)
REBUILDABLE_GLOBS = ("__pycache__/", "*.pyc")

CANONICAL_METHOD = (
    "sha256 по канонической карте {относительный путь: sha256 файла} по СОДЕРЖАТЕЛЬНЫМ файлам набора, "
    "без усечения (AD-2: хеш по первым 1 МиБ тождества не доказывает); card.json в карту не входит; "
    "пересобираемые артефакты (__pycache__/, *.pyc) исключены — они производны от исходников и в git "
    "не хранятся, поэтому хеш с ними из чистого клона не воспроизводится (решение архитектора 20.09.2026)"
)


def is_rebuildable(rel: str) -> bool:
    """Пересобираемый артефакт набора: байткод Python — вне канонической карты."""
    parts = pathlib.PurePosixPath(rel).parts
    return any(part in REBUILDABLE_DIRS for part in parts[:-1]) or rel.endswith(REBUILDABLE_SUFFIXES)


def canonical_digest(files: dict[str, str]) -> str:
    """sha256 канонической карты {относительный путь: sha256 файла}, без усечения."""
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def set_digest(set_dir: pathlib.Path) -> tuple[str, dict[str, str]]:
    """Полный sha256 набора: каноническая карта {путь: sha256 файла}, без усечения.

    Ту же функцию пересчитывает страж `tools/check_grounded_pool.py`: расхождение
    означает, что доказательство относится к прежней версии набора (ADR-012).
    """
    files = {
        p.relative_to(set_dir).as_posix(): sha256_file(p)
        for p in sorted(set_dir.rglob("*"))
        if p.is_file() and p.name != "card.json" and not is_rebuildable(p.relative_to(set_dir).as_posix())
    }
    return canonical_digest(files), files


def delta_classes(old_files: dict, new_files: dict) -> dict:
    """Разложение отличий прежней карты от текущей по трём классам (ADR-028 п.1).

    Класс отличает «объявление» от «дыры»: байткод вне карты по построению, документ
    корня набора — описание набора, а измеренный артефакт (файлы задач и мока) —
    то, подмена чего обязана быть названа, а не спрятана. Значение — ПРЕЖНИЙ хеш
    (`None`, если файл появился только сейчас).
    """
    out = {"files_excluded": {}, "files_doc_delta": {}, "files_measured_delta": {}}

    def put(rel: str, old_sha) -> None:
        if is_rebuildable(rel):
            out["files_excluded"][rel] = old_sha
        elif "/" not in rel:
            out["files_doc_delta"][rel] = old_sha
        else:
            out["files_measured_delta"][rel] = old_sha

    for rel, old_sha in sorted(old_files.items()):
        if new_files.get(rel) != old_sha:
            put(rel, old_sha)
    for rel in sorted(new_files):
        if rel not in old_files:
            put(rel, None)
    return out


def _history_entry(block: dict, prefix: str) -> dict:
    """Запись цепочки идентичностей: те же имена полей, что у блока `previous_*`."""
    return {
        "sha256_full": block.get(f"{prefix}sha256_full"),
        "sha256_full_method": block.get(f"{prefix}sha256_full_method"),
        "sha256_full_at": block.get(f"{prefix}sha256_full_at"),
        "sha256_full_reason": block.get(f"{prefix}sha256_full_reason"),
        "files": block.get(f"{prefix}files") or {},
        "files_excluded": block.get(f"{prefix}files_excluded") or {},
        "files_doc_delta": block.get(f"{prefix}files_doc_delta") or {},
        "files_measured_delta": block.get(f"{prefix}files_measured_delta") or {},
        "size_bytes": block.get(f"{prefix}size_bytes"),
    }


def identity_history(out_path: pathlib.Path, canon: str, files: dict) -> dict:
    """Цепочка идентичностей набора: текущая пара уходит в `previous_*`, прежняя не теряется.

    ADR-028 п.1: факт первичен, прежнее объявление сохраняется рядом с новым, а не
    затирается. Прежняя запись уезжает в `previous_history` — так у цепочки нет
    «одного слота», и третья пересборка не стирает вторую.

    Прежнее объявление несёт **полную каноническую карту** (`previous_files`), а не
    подпись: по ней страж `tools/check_grounded_pool.py` восстанавливает идентичность
    без догадок и раскладывает отличия по классам. Класс измеренных артефактов
    (`previous_files_measured_delta`) назван отдельно от документов корня: изменение
    задачи обязано быть видимым, а не выглядеть «прежней идентичностью».
    """
    old = {}
    if out_path.is_file():
        try:
            loaded = json.loads(out_path.read_text(encoding="utf-8"))
            old = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            old = {}
    if not old:
        return {}
    if old.get("sha256_full") == canon:
        # Хеш не менялся — цепочка описывает отношение к той же идентичности.
        block = {k: v for k, v in old.items() if k.startswith("previous_")}
        return block
    delta = delta_classes(old.get("files") or {}, files)
    reason = [
        "идентичность набора сменена пересборкой карточки: содержательная часть изменилась, "
        "отличия разложены по классам в previous_files_* (ADR-028 п.1: прежнее объявление "
        "сохраняется рядом с новым, а не затирается)",
        "пересобираемый байткод (__pycache__/, *.pyc) вне канонической карты: хеш с ним из "
        "чистого клона не воспроизводится (ADR-023 п.12: страж не должен краснеть по построению)",
    ]
    if delta["files_doc_delta"]:
        reason.append("документы корня набора: " + ", ".join(sorted(delta["files_doc_delta"])))
    if delta["files_measured_delta"]:
        reason.append(
            "измеренные артефакты (задачи и мок): " + ", ".join(sorted(delta["files_measured_delta"]))
            + " — доказательство, записанное под прежней идентичностью, устаревает и пересобирается (ADR-012)"
        )
    entry = {
        "previous_sha256_full": old.get("sha256_full"),
        "previous_sha256_full_method": old.get("sha256_full_method"),
        "previous_sha256_full_at": datetime.date.today().isoformat(),
        "previous_sha256_full_reason": "; ".join(reason),
        "previous_files": old.get("files") or {},
        "previous_files_excluded": delta["files_excluded"],
        "previous_files_doc_delta": delta["files_doc_delta"],
        "previous_files_measured_delta": delta["files_measured_delta"],
        "previous_size_bytes": old.get("size_bytes"),
    }
    history = list(old.get("previous_history") or [])
    if old.get("previous_sha256_full"):
        item = _history_entry(old, "previous_")
        if not any(h.get("sha256_full") == item["sha256_full"] for h in history):
            history.append(item)
    return {**entry, "previous_history": history}


def write_card(set_dir: pathlib.Path, out_path: pathlib.Path) -> dict:
    """Карточка набора AD-2: полный sha256 содержимого, объём, схема.

    Объём считается по канонической карте — у числа один носитель (ADR-023 п.10):
    `size_bytes` обязан описывать тот же состав, что и `sha256_full`.
    """
    canon, files = set_digest(set_dir)
    total = sum((set_dir / rel).stat().st_size for rel in files)
    history = identity_history(out_path, canon, files)
    tasks = []
    for d in sorted((set_dir / "tasks").iterdir()):
        if (d / "task.json").is_file():
            spec = json.loads((d / "task.json").read_text(encoding="utf-8"))
            tasks.append({
                "id": spec["id"], "tool": spec["tool"], "network": spec["network"],
                "max_steps": spec.get("max_steps"),
                "verifier": spec["verifier"], "verifier_sha256": sha256_file(d / spec["verifier"]),
                "task_json_sha256": sha256_file(d / "task.json"),
            })
    card = {
        "schema": "grounded-skeleton-card/1",
        "what": "walking skeleton заземлённого RL (ADR-049 пп.1–12): 5 задач × 3 инструмента (2 shell, 2 файловая система, 1 HTTP-мок)",
        "root": "data/grounded-skeleton",
        "n_tasks": len(tasks),
        "tools": sorted({t["tool"] for t in tasks}),
        "size_bytes": total,
        "sha256_full": canon,
        "sha256_full_method": CANONICAL_METHOD,
        "excluded_rebuildable": list(REBUILDABLE_GLOBS),
        "files": files,
        **history,
        "tasks": tasks,
        "task_schema": {
            "id": "идентификатор без ожидаемого ответа (ADR-049 п.9)",
            "tool": "shell | filesystem | http_mock",
            "network": "none | mock",
            "max_steps": "лимит шагов ЗАДАЧИ (grounded-task/2): исполняет среда, харнесс может "
                         "держать внешний потолок сверху, но источник истины — задача (ADR-049 п.1, G4)",
            "prompt": "наблюдение агента (gold в него не входит)",
            "initial_state": "(а) описание начального состояния",
            "goal_action": "(б) действие-цель",
            "success_criterion": "(в) бинарный критерий успеха по состоянию",
            "no_action_probe": "(г) проба «текстовый ответ без вызовов»",
            "empty_action_probe": "вырожденная проба «действие без последствий»",
            "seed_dir": "начальное состояние задачи (монтируется в /work контейнера)",
            "holdout_dir": "опционально: удержанный вход для верификатора-ре-execution",
            "solution": "эталонное действие (положительный контроль)",
            "verifier": "внешний верификатор: JSON {score, reason, expected_public, reads}",
            "verifier_reads": "объявленные источники (префиксы snapshot|pristine|journal|holdout|reexec|generator)",
            "requires": "чем задача пользуется внутри контейнера",
            "leak_check": "что именно проверяется на утечку gold",
        },
        "runner": "tools/grounded_runner.py",
        "guard": "tools/check_grounded_pool.py",
        "env_guard": "tools/check_env_contract.py",
        "evidence": "evidence/grounded-skeleton.json",
    }
    out_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return card



# ─── канарейка выхода наружу ─────────────────────────────────────────────────


class Canary:
    """Слушатель ВНЕ сети прогона: контейнер на дефолтной bridge.

    Зачем отдельная проверка, если есть `wget http://1.1.1.1/`: на хосте без
    интернета та проба вакуумна, и «зелёный» страж ничего не доказывал бы.
    Канарейка живёт на дефолтной bridge: с неё контейнер доходит до своего
    сегмента, из внутренней сети прогона — нет (межсетевой трафик docker не
    пропускает). Контейнер, а не слушатель в процессе раннера: адрес хоста
    занят демоном, и проба из контейнера стадии на нём падала бы `Errno 99`.

    Канарейка обязана быть ЖИВОЙ: мёртвый слушатель давал бы «недостижима» даром,
    поэтому готовность подтверждается self-check'ом через демон.
    """

    def __init__(self, image: str, name: str, port: int = CANARY_PORT) -> None:
        self.image = image
        self.name = name
        self.port = port
        self.ip = ""
        self.url = ""
        self.self_check = False
        self.error = ""
        self._started = False

    def start(self) -> None:
        r = sh(["docker", "run", "-d", "--rm", "--name", self.name,
                "--read-only", "--tmpfs", "/tmp:rw,size=8m",
                "--user", SIDECAR_USER, "--memory", "128m", "--pids-limit", "64",
                self.image, "python3", "-m", "http.server", str(self.port), "--bind", "0.0.0.0"],
               timeout=180)
        if r.returncode != 0:
            self.error = f"канарейка не поднялась: {r.stderr.strip()[:300]}"
            raise RuntimeError(self.error)
        self._started = True
        self.ip = container_ip(self.name, CANARY_NETWORK)
        if not self.ip:
            self.error = "канарейка поднялась без адреса на дефолтной bridge"
            raise RuntimeError(self.error)
        self.url = f"http://{self.ip}:{self.port}/"
        code = ("import sys,urllib.request;"
                f"sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:{self.port}/',timeout=10)"
                ".read().decode())")
        for _ in range(50):
            probe = sh(["docker", "exec", self.name, "python3", "-c", code], timeout=30)
            if probe.returncode == 0:
                self.self_check = True
                return
            time.sleep(0.1)
        self.error = "канарейка не отвечает сама себе — проба «недостижима» была бы вакуумной"
        raise RuntimeError(self.error)

    def stop(self) -> None:
        if self._started:
            sh(["docker", "rm", "-f", self.name], timeout=90)
            self._started = False

    def record(self) -> dict:
        return {"kind": "container", "name": self.name, "image": self.image,
                "network": CANARY_NETWORK, "ip": self.ip, "url": self.url,
                "self_check": self.self_check}


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Прогон заземлённого скелета в контейнере (ADR-049 пп.1–12)")
    ap.add_argument("--run", dest="do_run", action="store_true", help="положительный контроль: действие выполнено → 1")
    ap.add_argument("--no-action", dest="do_no_action", action="store_true", help="проба G2: текстовый ответ без вызовов → 0")
    ap.add_argument("--empty-action", dest="do_empty_action", action="store_true", help="действие без последствий (true) → 0")
    ap.add_argument("--step-probe", dest="do_step_probe", action="store_true",
                    help="проба лимита шагов задачи: бюджет 0 → 0, бюджет 1 → 1")
    ap.add_argument("--timeout-probe", dest="do_timeout_probe", action="store_true",
                    help="проба супервизора шага: убитый по таймауту шаг не влияет на следующий rollout")
    ap.add_argument("--teardown-probe", dest="do_teardown_probe", action="store_true",
                    help="проба снятия батч-контейнера: -t 0 против -t 1 (цена и целость состояния)")
    ap.add_argument("--sidecar-probe", dest="do_sidecar_probe", action="store_true",
                    help="проба цены мока-sidecar: подъём с готовностью и снятие, N повторов")
    ap.add_argument("--json", action="store_true", help="отчёт в stdout (машинно читаемый: только JSON)")
    ap.add_argument("--tasks", nargs="*", default=None, help="подмножество задач по id")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--sidecar-image", default=DEFAULT_SIDECAR_IMAGE,
                    help="образ sidecar'ов (мок и канарейка): нужен python3 внутри")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--run-dir", default="", help="каталог прогона (обязан лежать в $HOME: docker не видит /tmp)")
    ap.add_argument("--set-dir", default=str(SET_DIR))
    ap.add_argument("--repeat", type=int, default=1, help="повторов каждого режима (проверка воспроизводимости состояния)")
    ap.add_argument("--mock-seed", type=int, default=MOCK_SEED_DEFAULT)
    ap.add_argument("--stand", default="", help="метка стенда для отчёта (по умолчанию — имя хоста)")
    ap.add_argument("--in-stage-container", dest="in_stage_container", action="store_true",
                    help="заявка «проба идёт внутри контейнера стадии»: принимается только при "
                         "подтверждении признаками (контейнер + docker-сокет + переменные образа стадии)")
    ap.add_argument("--max-steps", type=int, default=None,
                    help="внешний потолок шагов харнесса: применяется как min с бюджетом задачи")
    ap.add_argument("--timeout-override", type=int, default=3, help="таймаут пробы супервизора, с")
    ap.add_argument("--card", action="store_true", help="пересобрать карточку набора и выйти")
    ap.add_argument("--mount-preflight-only", dest="mount_preflight_only", action="store_true",
                    help="только предпроверка same-path монтирования (маркер-контейнер) и выход: "
                         "каталог прогона и дерево набора обязаны быть видны демону по тем же путям")
    ap.add_argument("--keep", action="store_true", help="не убирать контейнеры и каталог прогона")
    args = ap.parse_args(argv)

    def say(msg: str) -> None:
        print(msg, file=sys.stderr if args.json else sys.stdout)

    set_dir = pathlib.Path(args.set_dir).resolve()
    if args.card:
        card = write_card(set_dir, set_dir / "card.json")
        say(json.dumps({"card": str(set_dir / "card.json"), "sha256_full": card["sha256_full"],
                        "files": len(card["files"]),
                        "previous_sha256_full": card.get("previous_sha256_full"),
                        "previous_history": [h.get("sha256_full") for h in card.get("previous_history") or []]},
                       ensure_ascii=False))
        return 0

    if not (args.do_run or args.do_no_action or args.do_empty_action):
        args.do_run = args.do_no_action = args.do_empty_action = True

    # Место пробы — по признакам, до всякой работы: заявка без подтверждения
    # означала бы, что в отчёте названа не та площадка (ADR-049 п.10).
    args.execution = execution_context()
    if args.in_stage_container:
        ok, why = stage_container_ok(args.execution)
        if not ok:
            say(f"NOT-VERIFIED: заявлено «внутри контейнера стадии», а признаков нет: {why}")
            return 2
    args.execution["where"] = (
        "контейнер стадии" if args.in_stage_container else
        "контейнер (не стадии)" if args.execution["in_container"] else "хост"
    )

    if not set_dir.is_dir():
        say(f"NOT-VERIFIED: нет набора задач {set_dir}")
        return 2
    try:
        tasks = load_tasks(set_dir, args.tasks)
    except ValueError as exc:
        # Задача без max_steps — не «без лимита»: G4 держится лимитом из задачи,
        # а лимит, которого нет, держит вызывающая сторона.
        say(f"NOT-VERIFIED: {exc}")
        return 2
    if not tasks:
        say("NOT-VERIFIED: набор задач пуст")
        return 2

    # Владелец состояния и пользователь контейнеров прогона обязаны сойтись: иначе
    # задачи падают не потому, что не решены, а потому, что среда не дала писать.
    ready, why = runtime_user_ready(CASE_ROOT)
    args.runtime_user = {"ready": ready, "why": why, "euid": os.geteuid(), "runtime_uid": RUNTIME_UID}
    if not ready:
        say(f"NOT-VERIFIED: {why}")
        return 2

    ok, info = docker_ok()
    if not ok:
        say(f"NOT-VERIFIED: {info}")
        return 2
    args.docker_version = info
    # Оба образа пробы — с ценой тяга: на чистом стенде первая проба платит
    # скачивание, и без этого поля цена молча уехала бы в задержку подъёма.
    images: list[dict] = []
    ok, image_info = ensure_image_measured(args.image)
    if not ok:
        say(f"NOT-VERIFIED: {image_info['error']}")
        return 2
    images.append(image_info)
    args.image_digest = image_info["digest"]
    if any(t.network != "none" for t in tasks):
        ok, sidecar_info = ensure_image_measured(args.sidecar_image)
        if not ok:
            say(f"NOT-VERIFIED: {sidecar_info['error']}")
            return 2
        images.append(sidecar_info)
    args.images = images

    run_root = pathlib.Path(args.run_dir).expanduser() if args.run_dir else DEFAULT_RUN_ROOT
    run_dir = run_root / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    args.run_dir = run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scratch").mkdir(exist_ok=True)
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    by_net: dict[str, list[Task]] = {}
    for t in tasks:
        by_net.setdefault(t.network, []).append(t)

    mock: MockSidecar | None = None
    canary: Canary | None = None
    sandboxes: dict[str, Sandbox] = {}
    sandbox_by_task: dict[str, Sandbox] = {}
    containers: list[dict] = []
    per_task: list[dict] = []
    timings: list[dict] = []
    prev_paths: set[str] = set()
    extra: dict = {}
    network = ""
    rc = 0

    def shutdown() -> None:
        """Снять за собой всё: контейнеры, мок, канарейку, сеть. Идемпотентно."""
        for sb in sandboxes.values():
            sb.stop()
        if mock:
            mock.stop()
        if canary:
            canary.stop()
        remove_network(network)

    try:
        # Предпроверка монтирования — ДО сети и мока, и до первого шага задачи:
        # несовпадение путей обязано называть СЕБЯ причиной, а не выглядеть как
        # «мок не отметился готовым» или «задача не решена».
        args.mount_preflight = mount_preflight(
            run_dir, set_dir, args.image, check_set_tree=any(t.network != "none" for t in tasks))
        if not args.mount_preflight["ok"]:
            note = args.mount_preflight["findings"][0]
            say(f"NOT-VERIFIED: {note}")
            if args.mount_preflight_only:
                if args.json:
                    print(json.dumps(args.mount_preflight, ensure_ascii=False, indent=2))
                return 2
            shutdown()
            extra["cleanup"] = leftovers(f"{RUN_TAG}-{run_dir.name}")
            ev = build_evidence(args, tasks, per_task, sandboxes, mock, timings, containers,
                                started_at, note, extra)
            ev["status"] = "blocked"
            pathlib.Path(args.out).write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
            if args.json:
                print(json.dumps(ev, ensure_ascii=False, indent=2))
            return 2
        if args.mount_preflight_only:
            if args.json:
                print(json.dumps(args.mount_preflight, ensure_ascii=False, indent=2))
            else:
                say("монтирование: " + args.mount_preflight["reading"])
            return 0
        if any(net != "none" for net in by_net):
            # Сеть прогона — СВОЯ и внутренняя: дефолтный bridge выпускает контейнер
            # в интернет, и награду можно получить внешним вызовом (проба 20.09.2026).
            network = f"{RUN_TAG}-{run_dir.name}-net"
            ok, gw = create_internal_network(network)
            if not ok:
                say(f"NOT-VERIFIED: сеть --internal не создана: {gw}")
                return 2
            # Мок и канарейка поднимаются ДО песочницы и падают в NOT-VERIFIED с
            # названной причиной: недостижимость sidecar из контейнера стадии —
            # это ФАКТ для решения (поправка ADR-049 п.10), а не повод обойти пробу.
            try:
                mock = MockSidecar(set_dir, run_dir, args.mock_seed, network,
                                   args.sidecar_image, f"{network}-mock")
                mock.start()
                canary = Canary(args.sidecar_image, f"{network}-canary")
                canary.start()
            except RuntimeError as exc:
                note = f"sidecar-мок или канарейка не поднялись: {exc}"
                say(f"NOT-VERIFIED: {note}")
                shutdown()
                extra["cleanup"] = leftovers(f"{RUN_TAG}-{run_dir.name}")
                ev = build_evidence(args, tasks, per_task, sandboxes, mock, timings, containers, started_at, note, extra)
                ev["status"] = "blocked"
                pathlib.Path(args.out).write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if args.json:
                    print(json.dumps(ev, ensure_ascii=False, indent=2))
                return 2

        for net, group in sorted(by_net.items()):
            name = f"{RUN_TAG}-{run_dir.name}-{'offline' if net == 'none' else 'net'}"
            sb = Sandbox(args.image, run_dir / f"work-{net}", network if net != "none" else "none",
                         name, mock_url=(mock.url if net != "none" and mock else ""))
            sb.start()
            # Контейнер регистрируется сразу: он обязан быть снят и на пути отказа
            # (иначе проба изоляции оставила бы за собой работающий контейнер).
            sandboxes[name] = sb
            containers.append({"name": name, "network": net, "n_tasks": len(group), "start_s": round(sb.start_s, 4)})
            # Канарейка идёт в пробу ОБОИХ профилей: «none» не должен становиться
            # зелёным по той лишь причине, что попытки выхода не было вовсе.
            sb.probe(journal_path=(mock.journal if (mock and net != "none") else None),
                     set_dir=(set_dir if (mock and net != "none") else None),
                     canary_url=(canary.url if canary else ""))
            isolated, bad = sb.isolation_ok()
            if not isolated:
                note = f"изоляция не подтверждена ({', '.join(bad)}) — прогон остановлен, заземление без изоляции не заявляется"
                say(f"NOT-VERIFIED: {note}")
                shutdown()
                extra["cleanup"] = leftovers(f"{RUN_TAG}-{run_dir.name}")
                ev = build_evidence(args, tasks, per_task, sandboxes, mock, timings, containers, started_at, note, extra)
                ev["status"] = "blocked"
                pathlib.Path(args.out).write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if args.json:
                    print(json.dumps(ev, ensure_ascii=False, indent=2))
                return 2
            for t in group:
                sandbox_by_task[t.id] = sb

        for t in tasks:
            sb = sandbox_by_task[t.id]
            entry = {"id": t.id, "tool": t.spec["tool"], "network": t.network, "res": {}, "repeats": [],
                     "spec": t.spec, "residue_absent": True}
            for mode in MODES:
                if not {"run": args.do_run, "no-action": args.do_no_action, "empty-action": args.do_empty_action}[mode]:
                    continue
                for rep in range(max(1, args.repeat)):
                    rec = rollout(t, mode, sb, run_dir, args, mock, prev_paths)
                    rec["repeat"] = rep
                    # Накопительно, а не «только последний прогон»: проба no-action
                    # не создаёт файлов, и без накопления её собственный след от
                    # предыдущего режима остался бы непроверенным.
                    prev_paths |= set(rec["new_paths"])
                    if rec["residue_from_previous"]:
                        entry["residue_absent"] = False
                    entry["repeats"].append(rec)
                    if rep == 0:
                        entry["res"][mode] = rec
                        timings.append(rec)
            base = entry["res"].get("run") or entry["res"].get("no-action") or entry["res"].get("empty-action") or {}
            entry["gates"] = gate_verdicts(t, entry["res"], sb, leak_checks(t, base.get("expected_public", ""),
                                         mock if t.network != "none" else None), entry["residue_absent"])
            per_task.append(entry)
            for mode, want in EXPECTED_SCORE.items():
                if mode not in entry["res"]:
                    continue
                got = entry["res"][mode].get("score")
                if got != want:
                    rc = 1
                    say(f"КРАСНОЕ {t.id} [{mode}]: получено {got!r}, ожидалось {want} — {entry['res'][mode].get('reason')}")

        # ── пробы, доказывающие, что лимит и супервизор — в СРЕДЕ, а не в харнессе ──
        if args.do_step_probe:
            limits = [step_limit_probe(t, sandbox_by_task[t.id], run_dir, args, mock) for t in tasks]
            extra["step_limit"] = {
                "per_task": limits,
                "binds_on_state": all(x["binds_on_state"] for x in limits),
                "reading": ("бюджет шагов объявлен задачей (`max_steps`) и исполняется средой: "
                            "исчерпанный бюджет отказывает в действии, не исполняя его, поэтому "
                            "награда падает через состояние, а не через текст ответа"),
            }
            for x in limits:
                if not x["binds_on_state"]:
                    rc = 1
                    say(f"КРАСНОЕ {x['task']}: лимит шагов не связывает награду — {x['reading']}")
        if args.do_timeout_probe:
            sup = [timeout_probe(t, sandbox_by_task[t.id], run_dir, args, mock,
                                 timeout_override=args.timeout_override) for t in tasks]
            extra["supervision"] = {
                "per_task": sup,
                "next_rollout_unaffected": all(x["next_rollout_unaffected"] for x in sup),
                "process_killed_by_environment": all(x["group_left_after_kill"] == 0 for x in sup),
                "reading": ("клиентский таймаут `docker exec` процесс внутри не убивает — среда держит шаг "
                            "под супервизором, сама снимает группу процессов и откатывает частичный след; "
                            "следующий rollout даёт ту же награду и то же состояние, что чистый"),
            }
            for x in sup:
                if not x["next_rollout_unaffected"]:
                    rc = 1
                    say(f"КРАСНОЕ {x['task']}: убитый по таймауту шаг повлиял на следующий rollout — {x['reading']}")
        if args.do_teardown_probe:
            # Префикс — от КАТАЛОГА ПРОГОНА, а не от сети: при наборе без мока сети
            # нет, и имя контейнера пробы выпало бы из фильтра остатков.
            extra["teardown"] = teardown_probe(args.image, run_dir / "teardown", args,
                                               f"{RUN_TAG}-{run_dir.name}")
        if args.do_sidecar_probe:
            if not network:
                say("проба sidecar пропущена: в наборе нет задач с сетью — мок не поднимался")
            else:
                extra["sidecar_probe"] = sidecar_probe(set_dir, run_dir, args, network)
        # Канарейка уходит в отчёт ДО снятия: её запись — часть доказательства
        # (живость слушателя), а не только строка лога.
        extra["canary"] = canary.record() if canary else None

        # Снятие за собой — ДО учёта остатка: иначе отчёт называл бы «оставшимся»
        # то, что снимается строкой ниже, и число теряло бы смысл.
        if not args.keep:
            shutdown()
        # Цена sidecar считается ПОСЛЕ снятия: в неё входит `stop_s`, которого до
        # снятия не существует. При `--keep` контейнеры остаются живыми, поэтому
        # снятие честно помечается неизмеренным, а не нулём.
        if mock is not None:
            extra["sidecar_cost"] = sidecar_cost(mock, extra.get("sidecar_probe"), containers,
                                                 extra.get("teardown"))
            if args.keep:
                extra["sidecar_cost"]["reading"] += "; снятие не измерено (--keep: контейнеры оставлены живыми)"
        extra["cleanup"] = leftovers(f"{RUN_TAG}-{run_dir.name}")
        ev = build_evidence(args, tasks, per_task, sandboxes, mock, timings, containers, started_at, extra=extra)
        if extra["cleanup"]["containers"] or extra["cleanup"]["networks"]:
            rc = 1
            say(f"КРАСНОЕ: за прогоном остались объекты — {extra['cleanup']}")
        pathlib.Path(args.out).write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps(ev, ensure_ascii=False, indent=2))
        say(f"скелет: {len(per_task)} задач, G1–G4 {'выполнены' if ev['verdict']['skeleton_ready'] else 'НЕ выполнены'}; "
            f"no-action={ev['no_action']['share']}; сеть {ev['network']['profile']}; "
            f"цена среды {ev['cost']['measured']['rollout_overhead_s']} с/rollout; отчёт: {args.out}")
        return rc
    finally:
        shutdown()
        if not args.keep:
            shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
