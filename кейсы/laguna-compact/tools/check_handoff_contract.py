#!/usr/bin/env python3
"""C-021 (proposal) — поведенческий страж AD-12 / ADR-023: отчёт прогона несёт JSON-контракт.

Механический сторож правила «дельта принимается только при JSON-контракте
результата» (ADR-023 Decision 1/5, спайн AD-12 «Дельта считается выполненной
только при доказательствах» — условие 4). Класс сбоя, который он ловит: харнесс
завершился с кодом 0, но исполнитель не выдал
машиночитаемый статус — например, ушёл в фоновое ожидание («ответ пришлёт
watcher») и закрыл прогон без контракта (hr-43, hr-55). Код возврата харнесса
при этом ничего не доказывает (ADR-023 «Ключевой вывод»).

Страж читает лог прогона (``hr-*.log`` харнесса или голый stdout) и заново
извлекает и валидирует JSON-контракт результата — тот же алгоритм, что у
``parse_result_contract`` в харнессе (fenced `` ```json ``-блоки с конца +
голый JSON-объект в хвосте 4 КБ). Вердикт стража обязан совпадать с вердиктом
харнесса в заголовке лога; расхождение — сигнал ошибки самого стража.

Три исхода на файл:

* **valid**   — контракт найден и валиден (``status`` ∈ complete|partial|blocked);
* **missing** — контракта нет (в stdout нет ``"status"`` вовсе);
* **invalid** — блок со ``status`` есть, но это не валидный JSON или схема
  нарушена (``status`` вне перечисления, список не массивом, ``status`` не строка).

Сводный код возврата::

    0 — все проверенные логи несут валидный контракт;
    1 — хотя бы один лог без контракта или с невалидным контрактом (НАРУШЕНИЕ);
    2 — NOT-VERIFIED: вход не задан / файл не найден / нечитаем — не зелёный.

Запуск::

    python3 tools/check_handoff_contract.py <лог> [<лог>...]
    python3 tools/check_handoff_contract.py --log-dir ~/.arch-ml/reports/harness --latest 1
    python3 tools/check_handoff_contract.py --log-dir DIR --glob "hr-*.log" --latest 30

Позиционные аргументы — пути к файлам (с раскрытием ``~`` и glob-паттернов).
``--log-dir`` — каталог с логами харнесса: берутся файлы по ``--glob`` (default
``hr-*.log``), отсортированные от свежих к старым, и ограничиваются ``--latest``
(без него — все). Логи харнесса живут вне кейса (``~/.arch-ml/reports/harness/``),
поэтому путь в правило CONSTRAINTS подводится архитектором (переменная окружения
или аргумент) — см. docs/specs/HANDOFF-CONTRACT-PROPOSAL.md, сам скрипт жёстких
путей не содержит.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Допустимые значения поля `status` (схема TASK.md, спайн AD-12).
ALLOWED_STATUS = ("complete", "partial", "blocked")

#: Поля-списки контракта; если присутствуют — обязаны быть массивами.
LIST_FIELDS = ("assumptions", "open_questions", "conflicts_with_prior_decisions")

_STDOUT_MARK = re.compile(r"^--- stdout ---[ \t]*$", re.MULTILINE)
_STDERR_MARK = re.compile(r"^--- stderr ---[ \t]*$", re.MULTILINE)


class NotVerified(Exception):
    """Входа нет или он нечитаем — судить не о чём."""


def stdout_region(text: str) -> str:
    """Вырезает stdout из лога харнесса (между ``--- stdout ---`` и ``--- stderr ---``).

    Если маркеров нет — файл считается голым stdout, ищется весь текст целиком.
    Выделение области нужно, чтобы шапка лога и stderr (чужой JSON, ошибки
    обвязки) не были приняты за контракт.
    """
    m = _STDOUT_MARK.search(text)
    if m is None:
        return text
    start = m.end()
    tail = text[start:]
    m2 = _STDERR_MARK.search(tail)
    if m2 is not None:
        return tail[: m2.start()]
    return tail


def _validate(obj: object) -> tuple[str | None, str | None]:
    """Проверяет разобранный контракт по схеме; возвращает (status, ошибка)."""
    if not isinstance(obj, dict):
        return None, "контракт не является JSON-объектом"
    status = obj.get("status")
    if not isinstance(status, str):
        return None, "поле `status` отсутствует или не строка"
    status_norm = status.strip().lower()
    if status_norm not in ALLOWED_STATUS:
        return None, f"status={status!r} вне complete|partial|blocked"
    for key in LIST_FIELDS:
        value = obj.get(key)
        if value is not None and not isinstance(value, list):
            return None, f"поле `{key}` не массив"
    return status_norm, None


def find_contract(stdout: str) -> tuple[str, str | None]:
    """Зеркало ``parse_result_contract`` харнесса.

    Возвращает ``(kind, detail)``: ``kind`` ∈ valid|invalid|missing,
    ``detail`` — статус (valid) или причина (invalid).
    """
    # 1. fenced ```json-блоки, сканируемые с конца (контракт обязан идти последним).
    blocks: list[str] = []
    rest = stdout
    fence = "```json"
    while True:
        i = rest.find(fence)
        if i < 0:
            break
        after = rest[i + len(fence):]
        j = after.find("```")
        if j < 0:
            break
        blocks.append(after[:j].strip())
        rest = after[j + 3:]
    invalid_reason: str | None = None
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
        except Exception as exc:  # noqa: BLE001 — здесь важен факт, не тип
            # Блок со status, который не парсится, — это Invalid, а не промах.
            if '"status"' in block:
                invalid_reason = f"невалидный JSON в ```json-блоке со status: {exc}"
            continue
        if isinstance(obj, dict) and "status" in obj:
            status, err = _validate(obj)
            if err is not None:
                return "invalid", err
            return "valid", status
    # 2. Голый JSON в хвосте (fence уронен): перебор `{`-позиций последних 4 КБ.
    tail = stdout[-4096:]
    for i in _rbrace_positions(tail):
        cand = tail[i:]
        if '"status"' not in cand:
            continue
        try:
            obj = json.loads(cand)
        except Exception:  # noqa: BLE001 — следующий кандидат
            continue
        if isinstance(obj, dict) and "status" in obj:
            status, err = _validate(obj)
            if err is not None:
                return "invalid", err
            return "valid", status
    if invalid_reason is not None:
        return "invalid", invalid_reason
    return "missing", None


def _rbrace_positions(text: str, limit: int = 8) -> list[int]:
    """Позиции ``{`` в тексте, от конца к началу (не более ``limit``)."""
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    return positions[::-1][:limit]


def _first_words(text: str, limit: int = 90) -> str:
    """Короткая выжимка stdout для диагноза «контракта нет»."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line[:limit] + ("…" if len(line) > limit else "")


def check_log(path: Path) -> dict:
    """Проверяет один лог; возвращает словарь вердикта."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise NotVerified(f"{path}: не читается ({exc})") from exc
    region = stdout_region(text)
    kind, detail = find_contract(region)
    hint = ""
    if kind == "missing":
        hint = _first_words(region)
    return {
        "path": str(path),
        "kind": kind,
        "detail": detail,
        "hint": hint,
        "size": len(text),
    }


def select_logs(args: argparse.Namespace) -> list[Path]:
    """Собирает список файлов из позиционных аргументов и/или --log-dir."""
    files: list[Path] = []
    seen: set[str] = set()
    for raw in args.logs or []:
        raw_exp = os.path.expanduser(raw)
        if _has_glob(raw_exp):
            matches = [Path(p) for p in glob.glob(raw_exp) if Path(p).is_file()]
            if not matches:
                raise NotVerified(f"glob '{raw}' не дал ни одного файла")
            for path in matches:
                if str(path) not in seen:
                    seen.add(str(path))
                    files.append(path)
        else:
            path = Path(raw_exp)
            if not path.is_file():
                raise NotVerified(f"{path}: файла нет")
            if str(path) not in seen:
                seen.add(str(path))
                files.append(path)
    if args.log_dir:
        d = Path(os.path.expanduser(args.log_dir))
        if not d.is_dir():
            raise NotVerified(f"каталог логов недоступен: {args.log_dir}")
        pattern = args.glob or "hr-*.log"
        found = sorted(
            (p for p in d.glob(pattern) if p.is_file()),
            key=lambda p: (p.stat().st_mtime, p.name),
            reverse=True,
        )
        if not found:
            raise NotVerified(f"в каталоге {args.log_dir} нет файлов по '{pattern}'")
        if args.latest is not None:
            found = found[: args.latest]
        for p in found:
            if str(p) not in seen:
                seen.add(str(p))
                files.append(p)
    if not files:
        raise NotVerified("не задано ни одного лога (пусто после раскрытия путей)")
    return files


def _has_glob(path: str) -> bool:
    return any(ch in path for ch in "*?[")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="check_handoff_contract.py",
        description="Страж JSON-контракта результата прогона (AD-12 / ADR-023).",
    )
    parser.add_argument("logs", nargs="*", help="лог(и) прогона; поддерживает ~ и glob")
    parser.add_argument("--log-dir", help="каталог с логами харнесса (вне кейса)")
    parser.add_argument("--glob", default="hr-*.log", help="glob внутри --log-dir")
    parser.add_argument("--latest", type=int, help="проверять только N свежайших логов")
    args = parser.parse_args(argv)

    try:
        files = select_logs(args)
    except NotVerified as exc:
        print(f"NOT-VERIFIED: {exc}")
        return EXIT_NOT_VERIFIED

    n_valid = 0
    failures: list[str] = []
    not_verified: list[str] = []
    for path in files:
        try:
            res = check_log(path)
        except NotVerified as exc:
            not_verified.append(str(exc))
            print(f"  nv      {path}  ({exc})")
            continue
        if res["kind"] == "valid":
            n_valid += 1
            print(f"  ok      {path}  status={res['detail']}")
        elif res["kind"] == "missing":
            failures.append(f"{path}: контракт не найден")
            print(f"  FAIL    {path}  контракт не найден (stdout: «{res['hint']}»)")
        else:  # invalid
            failures.append(f"{path}: контракт невалиден ({res['detail']})")
            print(f"  FAIL    {path}  контракт невалиден: {res['detail']}")

    print()
    print(f"итог: {n_valid}/{len(files)} с валидным контрактом; "
          f"нарушений {len(failures)}; not-verified {len(not_verified)}")
    if failures:
        print("FAIL: есть прогон без валидного JSON-контракта (AD-12 / ADR-023)")
        return EXIT_FAIL
    if not_verified:
        print("NOT-VERIFIED: часть входов нечитаема — не зелёный")
        return EXIT_NOT_VERIFIED
    print("HANDOFF CONTRACT OK: все логи несут валидный JSON-контракт")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
