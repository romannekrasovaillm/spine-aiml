"""Общая библиотека бенчмарка spine-vs-claude.

Только стандартная библиотека: скрипты бенчмарка не тянут внешних
зависимостей (YAML-библиотеки в том числе), поэтому `CONSTRAINTS.yaml`
разбирается построчным сканированием — бенчмарку нужны лишь идентификаторы,
имена и severity правил, а не полная YAML-семантика.

Содержит: пути каталога, константы матрицы, хэширование, имена ячеек,
чтение/запись JSON и JSONL, снимок пререгистрации.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# --- Пути -------------------------------------------------------------------

RUNNERS_DIR = Path(__file__).resolve().parent
ROOT = RUNNERS_DIR.parent
REPO_ROOT = ROOT.parent.parent

TASKS_DIR = ROOT / "tasks"
PACK_DIR = ROOT / "pack"
LOCK_PATH = ROOT / "prereg.lock.json"


def cells_dir(run_id: str) -> Path:
    """Каталог ячеек прогона."""
    return runs_dir() / run_id / "cells"


def runs_dir() -> Path:
    """Каталог прогонов (переопределяется `SVC_RUNS`)."""
    return Path(os.environ.get("SVC_RUNS") or (ROOT / "runs"))


def run_dir(run_id: str) -> Path:
    """Каталог одного прогона."""
    return runs_dir() / run_id


# --- Матрица ----------------------------------------------------------------

TASKS: tuple[str, ...] = (
    "ml-serving-split",
    "experiment-lineage",
    "gpu-quota-policy",
    "model-registry-migration",
)

ARMS: tuple[str, ...] = (
    "spine-arch",
    "spine-min",
    "claude-arch",
    "claude-mcp",
    "claude-plain",
)

MODELS: tuple[str, ...] = ("deepseek-flash", "glm-5.3-flash")

REPS: int = 3

#: Руки, которых касается `claude` CLI.
CLAUDE_ARMS = frozenset({"claude-arch", "claude-mcp", "claude-plain"})

#: Руки Spine (headless `arch-ml run -q`).
SPINE_ARMS = frozenset({"spine-arch", "spine-min"})

#: Руки с корпусом (spine-пакет кладётся в `work/`).
CORPUS_ARMS = frozenset({"spine-arch", "claude-arch", "claude-mcp"})

#: Руки, которым нужен сгенерированный MCP-конфиг.
MCP_ARMS = frozenset({"claude-mcp"})

#: Файлы постановки: одинаковы для всех рук, склеиваются в `prompt.txt`.
PROMPT_FILES: tuple[str, ...] = ("TASK.md", "CONTEXT.md", "SPEC.md", "ACCEPTANCE.md")

#: Файлы spine-пакета (руки с корпусом), лежат в `pack/`.
PACK_FILES: tuple[str, ...] = ("ARCHITECTURE-SPINE.md", "DECISION.template.md")

#: Минимальная длина ответа руки, ниже — считается сбоем генерации.
MIN_ANSWER_BYTES: int = 500

#: Таймаут одной ячейки (секунды).
ARM_TIMEOUT_SECS: int = int(os.environ.get("SVC_ARM_TIMEOUT") or "1100")

#: Общий лимит прогона (секунды); стоп-правило §7 пререгистрации.
MAX_RUN_SECS: int = int(os.environ.get("SVC_MAX_SECS") or str(6 * 3600))

# --- Регулярные выражения ---------------------------------------------------

AC_ID_RE = re.compile(r"AC-\d{2}")
TRACE_PAIR_RE = re.compile(r"(AC-\d{2})\s*->\s*(ADR-\d{3})")
ADR_NAME_RE = re.compile(r"ADR-(\d{3})")
ADR_HEADING_RE = re.compile(r"(?m)^#\s*ADR-(\d{3})")
SPINE_AD_RE = re.compile(r"(?m)^#{2,3}\s*AD-\d+")
SPEC_HASH_RE = re.compile(r"spec:\s*sha256:([0-9a-f]{64})")
APPROVER_RE = re.compile(
    r"(?i)(?:approver|аппрувер)\s*:\s*"
    r"[A-ZА-ЯЁ][a-zа-яё\-]+(?:\s+[A-ZА-ЯЁ][a-zа-яё\-]+){1,}"
)  # ФИО: ≥2 слов с заглавных; «укажите ФИО»/«claude»/«TODO» не проходят
RULE_ID_RE = re.compile(r"\s*-\s*id:\s*(\S+)")
RULE_NAME_RE = re.compile(r"\s*name:\s*(\S+)")
RULE_SEVERITY_RE = re.compile(r"\s*severity:\s*(\S+)")


# --- Утилиты ----------------------------------------------------------------


def now_iso() -> str:
    """Текущая метка времени UTC в формате ISO-8601 (секунды)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    """sha256 байтовой строки в hex."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    """sha256 файла в hex; `None`, если файл не читается."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def read_text(path: Path) -> str:
    """Текст файла; пустая строка, если файл не читается."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def write_json(path: Path, obj: Any) -> None:
    """Записать JSON с отступом, создав каталоги."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Any | None:
    """Прочитать JSON; `None`, если файла нет или он битый."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    """Записать JSONL (по строке на запись, без форматирования)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict) -> None:
    """Дописать одну запись в JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    """Прочитать JSONL, пропуская нечитаемые строки."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def resolve_bin(env_var: str, default: str, candidates: Iterable[Path]) -> str:
    """Разрешить внешний бинарь: env → PATH → известные сборки репозитория.

    Эксперимент должен уметь запуститься без правки кода: путь к `arch-ml`
    задаётся `SVC_ARCH`, `claude` — `SVC_CLAUDE`.
    """
    override = os.environ.get(env_var)
    if override:
        return override
    found = shutil.which(default)
    if found:
        return found
    for cand in candidates:
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return default


def arch_bin() -> str:
    """Путь к бинарю харнесса."""
    return resolve_bin(
        "SVC_ARCH",
        "arch-ml",
        (REPO_ROOT / "target" / "debug" / "arch-ml", REPO_ROOT / "target" / "release" / "arch-ml"),
    )


def claude_bin() -> str:
    """Путь к CLI Claude Code."""
    return resolve_bin("SVC_CLAUDE", "claude", ())


def arch_version() -> str:
    """Версия харнесса (`arch-ml --version`) или строка сбоя."""
    try:
        proc = subprocess.run(
            [arch_bin(), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return (proc.stdout or proc.stderr).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


# --- Ячейки -----------------------------------------------------------------

CELL_SEP = "__"


def cell_name(task: str, arm: str, model: str, rep: int) -> str:
    """Имя ячейки: `<TASK>__<ARM>__<MODEL>__r<N>`."""
    return CELL_SEP.join((task, arm, model, f"r{rep}"))


def parse_cell(cell: str) -> tuple[str, str, str, int]:
    """Разобрать имя ячейки обратно в `(task, arm, model, rep)`."""
    task, arm, model, rep = cell.split(CELL_SEP)
    return task, arm, model, int(rep.lstrip("r"))


def matrix(
    tasks: Iterable[str] = TASKS,
    arms: Iterable[str] = ARMS,
    models: Iterable[str] = MODELS,
    reps: int = REPS,
) -> list[dict[str, Any]]:
    """Матрица ячеек в детерминированном порядке."""
    out: list[dict[str, Any]] = []
    for task in tasks:
        for arm in arms:
            for model in models:
                for rep in range(1, reps + 1):
                    out.append(
                        {
                            "task": task,
                            "arm": arm,
                            "model": model,
                            "rep": rep,
                            "cell": cell_name(task, arm, model, rep),
                        }
                    )
    return out


# --- Задачи -----------------------------------------------------------------


def task_dir(task: str) -> Path:
    """Каталог задачи."""
    return TASKS_DIR / task


def constraints_path(task: str) -> Path:
    """Путь к гейту задачи."""
    return task_dir(task) / "CONSTRAINTS.yaml"


def task_rules(task: str) -> list[dict[str, str]]:
    """Правила `CONSTRAINTS.yaml` задачи: `id`, `name`, `severity`.

    Построчное сканирование вместо YAML-парсера: бенчмарку нужны только
    идентификаторы и severity, а внешних зависимостей он не тянет.
    """
    rules: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in read_text(constraints_path(task)).splitlines():
        m = RULE_ID_RE.match(line)
        if m:
            current = {"id": m.group(1).strip("\"'"), "name": "", "severity": "error"}
            rules.append(current)
            continue
        if current is None:
            continue
        m = RULE_NAME_RE.match(line)
        if m:
            current["name"] = m.group(1).strip("\"'")
            continue
        m = RULE_SEVERITY_RE.match(line)
        if m:
            current["severity"] = m.group(1).strip("\"'")
    for rule in rules:
        if not rule["name"]:
            rule["name"] = rule["id"]
    return rules


def task_ac_ids(task: str) -> list[str]:
    """Идентификаторы `AC-NN` из замороженного `ACCEPTANCE.md` (по порядку)."""
    seen: list[str] = []
    for ac in AC_ID_RE.findall(read_text(task_dir(task) / "ACCEPTANCE.md")):
        if ac not in seen:
            seen.append(ac)
    return seen


def task_spec_sha256(task: str) -> str:
    """sha256 файла `SPEC.md` задачи — то, что обязано попасть в `DECISION.md`."""
    return sha256_file(task_dir(task) / "SPEC.md") or ""


def build_prompt(task: str) -> str:
    """Текст постановки: TASK + CONTEXT + SPEC + ACCEPTANCE (одинаков для рук)."""
    parts: list[str] = [
        "# Постановка",
        "",
        f"Задача: `{task}`.",
        "",
        "Ниже — постановка, контекст предметной области, спецификация и",
        "приёмочные тесты. Артефакты результата кладите в текущий каталог.",
        "",
    ]
    for name in PROMPT_FILES:
        parts.append(f"\n---\n\n## Файл: {name}\n")
        parts.append(read_text(task_dir(task) / name).rstrip())
    return "\n".join(parts).rstrip() + "\n"


# --- Пререгистрация ---------------------------------------------------------


def frozen_inputs() -> list[Path]:
    """Все файлы, замораживаемые пререгистрацией (детерминированный порядок)."""
    files: list[Path] = [ROOT / "SPEC.md", ROOT / "PREREGISTRATION.md"]
    for task in TASKS:
        for name in (*PROMPT_FILES, "CONSTRAINTS.yaml"):
            files.append(task_dir(task) / name)
    for name in PACK_FILES:
        files.append(PACK_DIR / name)
    return sorted({p for p in files})


def compute_lock() -> dict[str, Any]:
    """Слепок sha256 всех замороженных входов."""
    hashes: dict[str, str] = {}
    for path in frozen_inputs():
        rel = str(path.relative_to(ROOT))
        hashes[rel] = sha256_file(path) or "MISSING"
    return {
        "schema": 1,
        "arch_ml_version": arch_version(),
        "hashes": hashes,
    }


def load_lock() -> dict[str, Any] | None:
    """Прочитать `prereg.lock.json`."""
    return read_json(LOCK_PATH)


def lock_mismatches(current: dict[str, Any], frozen: dict[str, Any]) -> list[str]:
    """Расхождения между текущими входами и замороженным слепком."""
    cur = current.get("hashes", {})
    ref = frozen.get("hashes", {})
    diff: list[str] = []
    for rel in sorted(set(cur) | set(ref)):
        if cur.get(rel) != ref.get(rel):
            diff.append(rel)
    return diff
