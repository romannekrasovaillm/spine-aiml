#!/usr/bin/env python3
"""Страж ADR-023 п.10 «у числа прибора один носитель» — реестр редакций приборов.

Дефект, ради которого страж заведён (назван при сведении, дельта S3z-2):
``docs/specs/EVAL-SETS.md`` объявлял ``sha256(tools/ppl_probe.py) = 5a2c3f72…``,
файл в дереве нёс ``931a6ff0…`` — потому что S3ao аддитивно дописал в ``SETS``
набор ``v3_domain``, а реестр за собой не обновил. Тот же старый хеш цитировали
ещё 16 артефактов, включая замороженные ``runs/*/state.json``. Правка цитат
запрещена (ADR-028 п.4): история не переписывается — значит расхождение лечится
не подгонкой чисел, а **одним носителем соответствия «прибор ↔ редакция ↔ sha256»**.

Носитель — ``docs/specs/INSTRUMENT-VERSIONS.md``. ``evidence/instrument-hash-audit.json``
— снимок аудита на дату (числа в нём пересобираемы из сырья: git-истории файлов и
дерева, — поэтому снимок не является независимым источником истины, ADR-023 п.13).

**Приборов несколько** (дельта ``followup-instruments``: реестр ведёт не один
файл, а N). Раздел прибора в реестре начинается заголовком уровня 2–4, называющим
путь прибора в бэктиках (``### 2.1 `tools/ppl_probe.py` …``); строки редакций
принадлежат прибору того раздела, в котором стоят. Список ожидаемых приборов —
константа ``INSTRUMENTS``; расхождение её состава с разделами реестра — находка
(иначе раздел с опечаткой в пути жил бы непроверенным).

Что проверяется механически — по каждому прибору отдельно:

  (а) каждая строка реестра сходится с фактом: ``sha256(git show <коммит>:<прибор>)``
      равна объявленной (реестр не мог «уехать» от истории);
  (б) ровно одна редакция помечена актуальной, и её sha256 равна sha256 файла в дереве;
  (в) набор редакций в реестре совпадает с набором, выведенным из истории файла:
      редакция, живущая в git и не названная в реестре, — нарушение (иначе следующая
      аддитивная правка снова родит расхождение);
  (г) снимок ``evidence/instrument-hash-audit.json`` не разошёлся с реестром —
      ни по составу приборов, ни по составу редакций, ни по дереву;
  (д) ``docs/specs/EVAL-SETS.md`` ссылается на реестр (у объявленного хеша прибора
      не должно остаться второго независимого носителя);
  (е) **исторические цитаты не переписаны**: каждая цитата из снимка по-прежнему
      присутствует в названном файле (ловит переписывание замороженного артефакта);
  (ж) ни одна цитата хеша прибора не осталась без редакции: 64-шечные хеши на
      строках про приборы обязаны принадлежать реестру. Хеш, которому нет
      редакции в истории, называется находкой, а не подгоняется (правило дельты);
  (з) **замороженные числа сверены с носителем и с цитатой** (раздел прибора,
      таблица ``| F-N | … |``): число сходится со своим носителем (поле отчёта или
      константа прибора), а названная цитата присутствует в названном файле и несёт
      ОКРУГЛЕНИЕ этого числа при своей точности. Класс дефекта тот же, что у хешей
      («объявление отстало от носителя»), но носитель — измеренная величина, а не
      хеш; цитата в README, живущая отдельно от реестра, — находка.

Коды возврата::

    0 — реестр, снимок, дерево и история согласованы
    1 — нарушение: поимённый список в stdout
    2 — NOT-VERIFIED: нет реестра/снимка/спеки либо git недоступен

Запуск::

    python3 tools/check_instrument_versions.py                 # проверка
    python3 tools/check_instrument_versions.py --emit          # пересобрать снимок
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Приборы — предмет реестра, в порядке разделов. Родственные приборы
#: (``flex_ppl_probe.py``, ``calib_ppl_probe.py``, ``ppl_probe_k2.py``,
#: ``probe_language_split.py``) ведут собственные хеши и сюда не подмешиваются: их
#: отчёт объявляет хеш прибора сам (``tool_sha256 = sha256_file(__file__)`` внутри
#: артефакта, который прибор и производит), поэтому носитель едет вместе с числом.
INSTRUMENTS = (
    "tools/ppl_probe.py",
    "tools/probe_pool_v2_reachability.py",
    "tools/grounded_runner.py",
)

REGISTRY = "docs/specs/INSTRUMENT-VERSIONS.md"
AUDIT = "evidence/instrument-hash-audit.json"
SPEC = "docs/specs/EVAL-SETS.md"

#: Ссылка на прибор — по ней хеш и приписывается прибору. У ``ppl_probe.py``
#: исторически три формы (имя файла и два поля-спутника), у второго прибора —
#: имя файла: формы разные, наборы не пересекаются, поэтому приборы не «крадут»
#: цитаты друг у друга.
INSTRUMENT_REFS = {
    "tools/ppl_probe.py": re.compile(
        # ``(?<![\w.])`` намеренно: внутри ``flex_ppl_probe.py`` и
        # ``calib_ppl_probe.py`` такой подстроки нет (другой файл), а
        # ``ppl_probe_k2.py`` не содержит ``ppl_probe.py`` вовсе.
        r"(?<![\w.])ppl_probe\.py|probe_revision|ppl_probe_sha256"),
    "tools/probe_pool_v2_reachability.py": re.compile(
        r"(?<![\w.])probe_pool_v2_reachability\.py"),
    #: Третий прибор цитируется **числами**, а не хешем (§2.3.3 реестра), поэтому
    #: его форма нужна для полноты картины: ссылка на путь прибора рядом с
    #: 64-шечным хешем означала бы цитату, которой у него быть не должно.
    "tools/grounded_runner.py": re.compile(
        r"(?<![\w.])grounded_runner\.py"),
}

#: Раздел прибора: заголовок уровня 2–4, называющий путь прибора в бэктиках.
SECTION = re.compile(r"^#{2,4}\s+[^\n]*?`(?P<path>[^`]+\.py)`")
#: Заголовок уровня 2 закрывает раздел: дальше идут разделы документа, не приборов.
LEVEL2 = re.compile(r"^##\s")
#: Строка реестра: ``| P-1 | `sha256` | `коммит` | … |`` — читаются первые три ячейки.
#: Идентификаторы редакций уникальны в пределах прибора (``P-1…P-4`` у PPL-пробы,
#: ``R-1`` у достижимости) — в цитате редакция называется парой «прибор + id».
ROW = re.compile(
    r"^\|\s*(?P<id>[A-Z]{1,3}-\d+)\s*\|\s*`(?P<sha256>[0-9a-f]{64})`\s*"
    r"\|\s*`(?P<commit>[0-9a-f]{7,40})`\s*\|"
)
#: Строка, объявляющая актуальную редакцию: ``| **P-4** | … | **актуальная** |``.
ACTUAL = re.compile(r"\*\*актуальная\*\*")

#: Строка таблицы замороженных чисел прибора (``| F-1 | … |``): величина, носитель,
#: значение, цитаты. Строкой редакции такая строка не прикидывается — ``ROW``
#: требует 64-шечный хеш во ВТОРОЙ ячейке, а здесь она несёт имя величины, — зато
#: идентификаторы различимы по префиксу (``F-`` против ``P-``/``R-``/``G-``).
FROZEN_ROW = re.compile(
    r"^\|\s*(?P<id>F-\d+)\s*\|\s*(?P<what>[^|]*?)\s*\|\s*(?P<carrier>[^|]*?)\s*\|"
    r"\s*(?P<value>[^|]*?)\s*\|\s*(?P<cites>[^|]*?)\s*\|\s*$"
)
#: Ссылка одной формы на оба случая: `` `файл` → `поле` `` у носителя и
#: `` `файл` → `фраза` `` у цитаты. Разделитель ячеек — ``;``.
REF = re.compile(r"`(?P<file>[^`]+)`\s*→\s*`(?P<target>[^`]+)`")
#: Число в цитате: целое или с десятичной точкой (разделитель — точка: оба README
#: пишут десятичную точку, и запятая здесь была бы другой записью, а не той же).
CITE_NUMBER = re.compile(r"\d+(?:\.\d+)?")

HX = re.compile(r"\b[0-9a-f]{64}\b")
#: Упоминание любого другого файла — граница атрибуции: хеш относится к прибору,
#: только если ближайшая вверх ссылка на файл — сам прибор (структуры вида
#: ``"ppl_probe.py": {"path": …, "sha256": …}`` и проза «Прибор: …», следующая строкой).
#: Расширено с ``*.py`` на артефакты вообще намеренно: в блоке ``instrument``
#: рядом с прибором лежат ``sha256`` данных и эталонов (``…-baseline-v1v2.json``),
#: и без этой границы их хеши приписывались бы прибору по ссылке строкой выше.
OTHER_FILE = re.compile(
    r"[\w./-]+\.(?:py|json|jsonl|txt|npy|yaml|yml|md|pt|sh|log|toml)\b"
)
#: Насколько вверх искать ссылку на файл: в JSON-записи прибора между ключом и
#: хешем стоит ``path``/``source``/``role`` — три строки, взят запас.
LOOKBACK = 6

SCAN_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".txt", ".sh", ".py"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "target"}
MAX_FILE_BYTES = 8 * 1024 * 1024


class Problem(Exception):
    """Нарушение, которое печатается и приводит к exit 1."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: Корень кейса, от которого стартуют git-вызовы. Ставится в ``main`` до первой
#: команды: ``git -C <корень кейса>`` делает вызовы не зависящими от текущего
#: каталога, а это существенно — у git две разные конвенции пути: ``-- <путь>``
#: (pathspec) разрешается от текущего каталога, а ``<рев>:<путь>`` — от корня
#: дерева. Смешать их значит молча получить пустую историю.
CASE: Path = Path(".")


def run_git(args: list[str]) -> subprocess.CompletedProcess:
    """Единственная точка вызова git: всегда ``-C <корень кейса>``.

    Через неё проходят и текстовые, и байтовые чтения: вызов в обход (просто
    ``subprocess.run(["git", …])``) ушёл бы в репозиторий по текущему каталогу —
    и, например, ``cat-file`` вернул бы пустоту, то есть хеш пустой строки.
    """
    proc = subprocess.run(
        ["git", "-C", str(CASE), *args], capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise Problem(
            f"git {' '.join(args)}: "
            f"{proc.stderr.decode(errors='replace').strip() or 'код ' + str(proc.returncode)}"
        )
    return proc


def git(*args: str) -> str:
    return run_git(list(args)).stdout.decode(errors="replace")


def git_bytes(*args: str) -> bytes:
    return run_git(list(args)).stdout


def instrument_in_repo(instrument: str) -> str:
    """Путь прибора от корня репозитория (``<рев>:<путь>`` ждёт именно его)."""
    top = Path(git("rev-parse", "--show-toplevel").strip())
    rel = os.path.relpath(CASE, top)
    return instrument if rel == "." else f"{rel}/{instrument}"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_registry(text: str) -> tuple[dict[str, list[dict]], list[str], dict[str, list[dict]], list[str]]:
    """Разделы реестра: путь прибора → строки редакций и строки чисел.

    Возвращает ``(sections, unattributed, frozen, frozen_unattributed)``.
    ``unattributed`` — строки редакций, ``frozen_unattributed`` — строки чисел,
    стоящие до первого раздела прибора: строка без прибора не проверяется ничем,
    поэтому называется находкой, а не пропускается молча.
    """
    sections: dict[str, list[dict]] = {}
    unattributed: list[str] = []
    frozen: dict[str, list[dict]] = {}
    frozen_unattributed: list[str] = []
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if LEVEL2.match(line):
            current = None  # раздел документа верхнего уровня закрывает раздел прибора
        heading = SECTION.match(line)
        if heading:
            current = heading.group("path")
            sections.setdefault(current, [])
            continue
        row = ROW.match(line)
        if row:
            if current is None:
                unattributed.append(f"{row.group('id')} ({row.group('sha256')[:8]}…)")
                continue
            sections[current].append(
                {
                    "id": row.group("id"),
                    "sha256": row.group("sha256"),
                    "commit": row.group("commit"),
                    "actual": bool(ACTUAL.search(line)),
                }
            )
            continue
        number = FROZEN_ROW.match(line)
        if not number:
            continue
        if current is None:
            frozen_unattributed.append(f"{number.group('id')} ({number.group('what')})")
            continue
        frozen.setdefault(current, []).append(
            {
                "id": number.group("id"),
                "what": number.group("what"),
                "value": unquote(number.group("value")),
                "carriers": [m.groupdict() for m in REF.finditer(number.group("carrier"))],
                "citations": [m.groupdict() for m in REF.finditer(number.group("cites"))],
            }
        )
    return sections, unattributed, frozen, frozen_unattributed


def unquote(cell: str) -> str:
    """Значение из ячейки: `` `0.7729` `` → ``0.7729`` (обратные кавычки — вёрстка)."""
    cell = cell.strip()
    if cell.startswith("`") and cell.endswith("`") and len(cell) >= 2:
        return cell[1:-1]
    return cell


def json_at(doc, field: str):
    """Значение по dotted-пути с индексами (``a.b[0].c``)."""
    current = doc
    for part in re.finditer(r"\.?([A-Za-z0-9_]+)|\[(\d+)\]", field):
        key, index = part.group(1), part.group(2)
        current = current[int(index)] if index is not None else current[key]
    return current


def constant_at(text: str, name: str):
    """Значение константы прибора: ``ИМЯ = <литерал>`` на верхнем уровне файла."""
    m = re.search(rf"^{re.escape(name)}\s*=\s*(?P<raw>.+?)\s*(?:#.*)?$", text, re.M)
    if not m:
        return None
    raw = m.group("raw").strip()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip("\"'")


def carrier_value(file: str, field: str):
    """Значение за носителем: поле отчёта (JSON) или константа прибора (``.py``).

    Недоступный носитель — находка, а не исключение: одно битое число не должно
    скрывать остальные (тот же принцип, что у проверки «е»).
    """
    path = CASE / file
    if not path.is_file():
        raise LookupError(f"носитель {file} не найден")
    if path.suffix == ".py":
        value = constant_at(read_text(path), field)
        if value is None:
            raise LookupError(f"константа {field} не найдена в {file}")
        return value
    try:
        return json_at(json.loads(read_text(path)), field)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LookupError(f"поле {field} не читается из {file}: {exc}") from exc


def same_value(declared: str, actual) -> bool:
    if isinstance(actual, bool):
        return declared.strip().lower() == str(actual).lower()
    if isinstance(actual, (int, float)):
        try:
            return abs(float(declared) - float(actual)) <= 1e-9
        except ValueError:
            return False
    return declared == str(actual)


def citation_carries(value, phrase: str) -> str | None:
    """Каким числом фраза несёт значение (``None`` — не несёт).

    Точность записи не фиксирована: ``0.77`` и ``0.773`` — одно число (0.7729) при
    разной точности, и оба README правы. Проверяется, что записанное в фразе число
    есть ОКРУГЛЕНИЕ носителя при своей точности (допуск — полшага младшего
    разряда), поэтому «1.77» для 0.7729 — находка, а «0.77» — нет. Число берётся
    целым токеном: «0.77» внутри «0.7729» цитатой не считается.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for m in CITE_NUMBER.finditer(phrase):
            token = m.group(0)
            before = phrase[m.start() - 1] if m.start() else ""
            after = phrase[m.end()] if m.end() < len(phrase) else ""
            if before.isdigit() or before == "." or after.isdigit() or after == ".":
                continue
            digits = len(token.split(".")[1]) if "." in token else 0
            if abs(float(value) - float(token)) <= 0.5 * 10 ** (-digits) + 1e-9:
                return token
        return None
    return str(value) if str(value) in phrase else None


def history_revisions(in_repo: str) -> dict[str, dict]:
    """Редакции прибора, выведенные из git: sha256 блоба → {коммиты, даты, размер}.

    Читается вся история файла (``--all``): редакция, живущая только в боковой
    ветви, — такой же факт, как и в основной, и реестр обязан её называть.
    """
    # ``:(top)`` — pathspec от корня дерева: без него путь кейса ищется от текущего
    # каталога и история выходит пустой (та же ловушка двух конвенций, что в git()).
    out = git("log", "--all", "--format=%H%x09%ad%x09%s", "--date=short",
              "--", f":(top){in_repo}")
    revs: dict[str, dict] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        commit, date, subject = (line.split("\t", 2) + ["", ""])[:3]
        blob = git("rev-parse", f"{commit}:{in_repo}").strip()
        data = git_bytes("cat-file", "blob", blob)
        digest = sha256_bytes(data)
        entry = revs.setdefault(
            digest, {"sha256": digest, "size_bytes": len(data), "commits": []}
        )
        entry["commits"].append({"commit": commit, "date": date, "subject": subject})
    for entry in revs.values():
        entry["commits"].sort(key=lambda c: (c["date"], c["commit"]))
        entry["first_commit"] = entry["commits"][0]["commit"]
        entry["date"] = entry["commits"][0]["date"]
    return revs


def blob_at(commit: str, in_repo: str) -> str:
    return git("show", f"{commit}:{in_repo}")


def sets_of(commit: str, in_repo: str) -> list[str]:
    """Ключи реестра наборов ``SETS`` в указанной редакции прибора (если он там есть)."""
    m = re.search(r"^SETS: dict\[str, str\] = \{(.*?)^\}", blob_at(commit, in_repo), re.S | re.M)
    return re.findall(r'^\s*"([^"]+)":', m.group(1), re.M) if m else []


def measure_digest(commit: str, in_repo: str) -> str:
    """sha256 тела ``measure`` — доказательство, что числа сопоставимы между редакциями."""
    m = re.search(r"^def measure\(.*?(?=^def |\Z)", blob_at(commit, in_repo), re.S | re.M)
    return sha256_bytes(m.group(0).encode()) if m else ""


def branch_tips(in_repo: str) -> dict[str, str]:
    """sha256 прибора на вершине каждой ветви, где файл есть (факт сведения ветвей)."""
    tips: dict[str, str] = {}
    for ref in git("branch", "-a", "--format=%(refname:short)").splitlines():
        ref = ref.strip()
        if not ref:
            continue
        proc = subprocess.run(
            ["git", "-C", str(CASE), "show", f"{ref}:{in_repo}"],
            capture_output=True, check=False,
        )
        if proc.returncode == 0:
            tips[ref] = sha256_bytes(proc.stdout)
    return tips


def attribute(lines: list[str], index: int) -> str | None:
    """Какому прибору принадлежит хеш на строке ``index`` (``None`` — ничьему).

    Строка хеша и до ``LOOKBACK`` строк вверх: первая же ссылка на файл решает.
    Ссылка на прибор реестра — этот прибор; ссылка на любой другой файл — «ничей»
    (так хеши соседнего прибора ``calib_ppl_probe.py`` и эталона
    ``…-baseline-v1v2.json``, лежащие в том же блоке ``instrument``, в реестр не
    подмешиваются). Ни одной ссылки на файл — «ничей»: цитата без имени прибора
    неотличима от случайного хеша данных.
    """
    for offset in range(0, LOOKBACK + 1):
        position = index - offset
        if position < 0:
            break
        line = lines[position]
        for instrument in INSTRUMENTS:
            if INSTRUMENT_REFS[instrument].search(line):
                return instrument
        if OTHER_FILE.search(line):
            return None
    return None


def scan_citations(case_root: Path) -> list[dict]:
    """Все цитаты хешей, отнесённые к приборам реестра: прибор, файл, строка, хеш.

    Сам носитель (`REGISTRY`) в область скана не входит: его таблицы редакций и
    есть реестр, а не цитаты реестра — иначе каждый объявленный хеш считался бы
    «цитатой самого себя», и счёт цитат зависел бы от вёрстки документа. Хеш
    строки реестра проверяется иначе и строже — сходимостью с историей (проверка «а»).

    Снимок (`AUDIT`) исключён по той же причине: он **пересказывает** реестр
    (ADR-023 п.13 — генерируемый артефакт не носитель долговечного факта), и его
    `tree_sha256` с `revisions[].sha256` — те же числа реестра, а не цитаты. Без
    этого исключения снимок цитировал бы сам себя, а `--emit` переставал бы быть
    идемпотентным: скан идёт по прежней редакции файла, запись — уже другая.
    """
    found: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(case_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or path.suffix not in SCAN_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = read_text(path)
            except OSError:
                continue
            rel = path.relative_to(case_root).as_posix()
            if rel in (REGISTRY, AUDIT):
                continue
            lines = text.splitlines()
            for index, line in enumerate(lines):
                for m in HX.finditer(line):
                    instrument = attribute(lines, index)
                    if instrument:
                        found.append(
                            {"instrument": instrument, "file": rel, "line": index + 1,
                             "sha256": m.group(0)}
                        )
    return found


def instrument_audit(instrument: str, rows: list[dict], frozen: list[dict]) -> dict:
    """Раздел снимка по одному прибору: редакции, вершины ветвей, цитаты, числа."""
    in_repo = instrument_in_repo(instrument)
    revs = history_revisions(in_repo)
    revisions = []
    for row in rows:
        digest = row["sha256"]
        known = revs.get(digest)
        revisions.append(
            {
                "id": row["id"],
                "sha256": digest,
                "status": "актуальная" if row["actual"] else "историческая",
                "registry_commit": row["commit"],
                "size_bytes": known["size_bytes"] if known else None,
                "commits": known["commits"] if known else [],
                "date": known["date"] if known else None,
                "sets": sets_of(row["commit"], in_repo) if known else [],
                "measure_sha256": measure_digest(row["commit"], in_repo) if known else "",
            }
        )
    return {
        "instrument": instrument,
        "tree_sha256": sha256_bytes((CASE / instrument).read_bytes()),
        "actual_revision": next((r["id"] for r in rows if r["actual"]), None),
        "revisions": revisions,
        # Замороженные числа прибора (у приборов, которые цитируются хешем, — пусто):
        # снимок их пересказывает, носитель остаётся реестром (ADR-023 п.13).
        "frozen_numbers": frozen,
        "branch_tips": branch_tips(in_repo),
    }


def build_audit() -> dict:
    sections, unattributed, frozen, _frozen_unattributed = parse_registry(read_text(CASE / REGISTRY))
    instruments = [instrument_audit(instrument, sections.get(instrument, []), frozen.get(instrument, []))
                   for instrument in INSTRUMENTS]
    citations = scan_citations(CASE)
    by_instrument: dict[str, dict[str, str]] = {
        entry["instrument"]: {r["sha256"]: r["id"] for r in entry["revisions"]}
        for entry in instruments
    }
    for cite in citations:
        cite["revision"] = by_instrument.get(cite["instrument"], {}).get(cite["sha256"])
    for entry in instruments:
        own = [c for c in citations if c["instrument"] == entry["instrument"]]
        entry["citations"] = own
        entry["citations_total"] = len(own)
        entry["citations_files"] = sorted({c["file"] for c in own})
        entry["uncited_revisions"] = [
            r["id"] for r in entry["revisions"]
            if not any(c["sha256"] == r["sha256"] for c in own)
        ]
    return {
        "schema": "instrument-hash-audit/2",
        "date": "2026-09-20",
        "delta": "grounded-instrument-registry",
        "carrier": REGISTRY,
        "snapshot_note": (
            "Снимок аудита на дату. Носитель соответствия «прибор ↔ редакция ↔ число» — "
            f"{REGISTRY}; все числа снимка пересобираемы из git-истории приборов, "
            "дерева и носителей замороженных чисел (ADR-023 п.13), расхождение с "
            "реестром ловит tools/check_instrument_versions.py. Разделы приборов — "
            "в порядке константы INSTRUMENTS стража."
        ),
        "instruments": instruments,
        "unattributed_rows": unattributed,
        "totals": {
            "instruments": len(instruments),
            "citations": len(citations),
            "citations_files": len({c["file"] for c in citations}),
        },
    }


def verify() -> tuple[list[str], list[str]]:
    """Возвращает (нарушения, замечания-факты). Замечания не красят гейт."""
    problems: list[str] = []
    notes: list[str] = []

    for rel in (REGISTRY, AUDIT, SPEC):
        if not (CASE / rel).exists():
            raise FileNotFoundError(rel)
    for instrument in INSTRUMENTS:
        if not (CASE / instrument).exists():
            raise FileNotFoundError(instrument)

    sections, unattributed, frozen, frozen_unattributed = parse_registry(read_text(CASE / REGISTRY))
    if not sections:
        problems.append(
            f"{REGISTRY}: ни одного раздела прибора (формат заголовка "
            "`### <номер> `tools/<прибор>.py` …` не найден)")
    for row in unattributed:
        problems.append(
            f"реестр: строка редакции {row} стоит вне раздела прибора — "
            "не проверяется ничем (нужен заголовок раздела с путём прибора)"
        )
    for row in frozen_unattributed:
        problems.append(
            f"реестр: строка замороженного числа {row} стоит вне раздела прибора — "
            "не проверяется ничем (нужен заголовок раздела с путём прибора)"
        )

    declared_instruments = set(sections)
    expected = set(INSTRUMENTS)
    for instrument in sorted(declared_instruments - expected):
        problems.append(
            f"реестр: раздел прибора {instrument} не назван в INSTRUMENTS стража — "
            "прибор не проверяется (правка константы — тем же коммитом)"
        )
    for instrument in sorted(expected - declared_instruments):
        problems.append(
            f"реестр: прибор {instrument} объявлен в INSTRUMENTS, но раздела с "
            "таблицей редакций нет — реестр неполон"
        )

    audit = json.loads(read_text(CASE / AUDIT))
    snapshot = {e.get("instrument"): e for e in audit.get("instruments", [])}
    if set(snapshot) != expected:
        problems.append(
            "снимок аудита: состав приборов разошёлся с INSTRUMENTS — "
            f"только в снимке {sorted(set(snapshot) - expected)}, "
            f"только в константе {sorted(expected - set(snapshot))} (пересобрать: --emit)"
        )

    seen_ids: dict[str, str] = {}
    for instrument in INSTRUMENTS:
        if instrument not in declared_instruments:
            continue  # уже названо находкой «раздела с таблицей редакций нет»
        rows = sections.get(instrument, [])
        prefix = f"{instrument}: "
        if not rows:
            problems.append(
                f"{prefix}раздел прибора есть, а строк редакций в нём нет "
                "(формат `| P-N | `sha` | `commit` |` не найден)")
            continue
        for row in rows:
            if row["id"] in seen_ids and seen_ids[row["id"]] != instrument:
                problems.append(
                    f"{prefix}идентификатор редакции {row['id']} уже занят прибором "
                    f"{seen_ids[row['id']]} — цитата станет неоднозначной"
                )
            seen_ids[row["id"]] = instrument

        # (а) реестр против истории
        in_repo = instrument_in_repo(instrument)
        revs = history_revisions(in_repo)
        for row in rows:
            if row["sha256"] not in revs:
                problems.append(
                    f"{prefix}редакция {row['id']} ({row['sha256'][:8]}…) не найдена в "
                    f"истории {instrument} — либо хеш выдуман, либо правка прибора "
                    "прошла без реестра"
                )
                continue
            if row["commit"] not in {c["commit"] for c in revs[row["sha256"]]["commits"]}:
                problems.append(
                    f"{prefix}коммит {row['commit'][:8]} редакции {row['id']} не содержит "
                    f"{instrument} с объявленным хешем {row['sha256'][:8]}…"
                )

        # (б) ровно одна актуальная, и она — дерево
        actual = [r for r in rows if r["actual"]]
        tree = sha256_bytes((CASE / instrument).read_bytes())
        if len(actual) != 1:
            problems.append(
                f"{prefix}актуальных редакций {len(actual)}, ожидалась одна "
                f"({[r['id'] for r in actual]})"
            )
        elif actual[0]["sha256"] != tree:
            problems.append(
                f"{prefix}дерево: sha256({instrument}) = {tree[:8]}… не совпал с "
                f"актуальной редакцией {actual[0]['id']} = {actual[0]['sha256'][:8]}…"
            )

        # (в) реестр покрывает историю целиком
        declared = {r["sha256"] for r in rows}
        for digest in sorted(set(revs) - declared):
            problems.append(
                f"{prefix}история: редакция {digest[:8]}… ({revs[digest]['date']}, "
                f"{revs[digest]['first_commit'][:8]}) живёт в git, но не названа в реестре"
            )

        # (г) снимок против реестра
        entry = snapshot.get(instrument)
        if entry is None:
            continue
        snap_revs = {r["sha256"] for r in entry.get("revisions", [])}
        if snap_revs != declared:
            problems.append(
                f"{prefix}снимок аудита разошёлся с реестром: "
                f"только в снимке {sorted(x[:8] for x in snap_revs - declared)}, "
                f"только в реестре {sorted(x[:8] for x in declared - snap_revs)} "
                "(пересобрать: --emit)"
            )
        if entry.get("tree_sha256") != tree:
            problems.append(
                f"{prefix}снимок аудита: tree_sha256 {str(entry.get('tree_sha256'))[:8]}… "
                f"не равен дереву {tree[:8]}… (пересобрать: --emit)"
            )
        snap_numbers = {n["id"]: n["value"] for n in entry.get("frozen_numbers", [])}
        reg_numbers = {n["id"]: n["value"] for n in frozen.get(instrument, [])}
        if snap_numbers != reg_numbers:
            problems.append(
                f"{prefix}снимок аудита разошёлся с реестром по замороженным числам: "
                f"только в снимке {sorted(set(snap_numbers) - set(reg_numbers))}, "
                f"только в реестре {sorted(set(reg_numbers) - set(snap_numbers))}, "
                f"расхождение значений "
                f"{sorted(i for i in set(snap_numbers) & set(reg_numbers) if snap_numbers[i] != reg_numbers[i])} "
                "(пересобрать: --emit)"
            )

        # (е) исторические цитаты не переписаны
        for cite in entry.get("citations", []):
            path = CASE / cite["file"]
            if not path.exists():
                problems.append(
                    f"{prefix}цитата: файл {cite['file']} исчез "
                    f"(цитата была на строке {cite['line']})"
                )
                continue
            # Хеш ищется целым токеном, а не подстрокой: ``0<хеш>`` содержит исходный
            # хеш как подстроку, и проверка «вхождением» пропустила бы подмену —
            # цитата перестала бы быть цитатой, оставшись «похожей».
            if not re.search(rf"(?<![0-9a-f]){re.escape(cite['sha256'])}(?![0-9a-f])",
                             read_text(path)):
                problems.append(
                    f"{prefix}цитата: {cite['file']}:{cite['line']} больше не несёт "
                    f"{cite['sha256'][:8]}… — замороженный артефакт переписан (ADR-028 п.4)"
                )

        uncited = [r["id"] for r in rows
                   if not any(c["sha256"] == r["sha256"] for c in entry.get("citations", []))]
        if uncited:
            notes.append(f"{instrument}: редакции без цитат в артефактах: {', '.join(uncited)}")

    # (з) замороженные числа: носитель и цитата обязаны сойтись ОБА. Число, у
    #     которого носитель уехал, — тот же дефект, что «объявление отстало от
    #     файла» у хешей; цитата, переставшая нести число, — второй носитель,
    #     живущий отдельно от реестра (ADR-023 п.10б).
    for instrument, rows in frozen.items():
        for row in rows:
            prefix = f"{instrument}: число {row['id']} ({row['what']}): "
            if not row["carriers"]:
                problems.append(f"{prefix}не назван носитель (нужна ссылка `файл` → `поле`)")
            if not row["citations"]:
                problems.append(
                    f"{prefix}не названа ни одна цитата — число заморожено без места, "
                    "где оно читается (тогда проверять нечего)"
                )
            values = []
            for ref in row["carriers"]:
                try:
                    value = carrier_value(ref["file"], ref["target"])
                except LookupError as exc:
                    problems.append(f"{prefix}{exc}")
                    continue
                values.append(value)
                if not same_value(row["value"], value):
                    problems.append(
                        f"{prefix}носитель {ref['file']} → {ref['target']} даёт {value!r}, "
                        f"а реестр объявляет {row['value']!r} — число уехало от носителя"
                    )
            if len({str(v) for v in values}) > 1:
                problems.append(
                    f"{prefix}носители расходятся между собой: "
                    + ", ".join(f"{r['file']} → {r['target']} = {v!r}"
                                for r, v in zip(row["carriers"], values))
                )
            for cite in row["citations"]:
                path = CASE / cite["file"]
                if not path.is_file():
                    problems.append(f"{prefix}файл цитаты {cite['file']} исчез")
                    continue
                if cite["target"] not in read_text(path):
                    problems.append(
                        f"{prefix}цитата {cite['file']} больше не несёт фразу «{cite['target']}» — "
                        "цитата разошлась с реестром (правь реестр тем же коммитом или верни цитату)"
                    )
                    continue
                if not values:
                    continue
                if citation_carries(values[0], cite["target"]) is None:
                    problems.append(
                        f"{prefix}фраза «{cite['target']}» в {cite['file']} не несёт числа "
                        f"{row['value']} — цитата ссылается на другое число"
                    )

    # (д) у объявленного хеша прибора не осталось второго независимого носителя
    spec_text = read_text(CASE / SPEC)
    if Path(REGISTRY).name not in spec_text:
        problems.append(
            f"{SPEC}: нет ссылки на {Path(REGISTRY).name} — объявленный хеш прибора "
            "остался бы вторым независимым носителем (ADR-023 п.10б)"
        )

    # (ж) цитат без редакции не осталось
    known = {r["sha256"]: instrument
             for instrument in INSTRUMENTS for r in sections.get(instrument, [])}
    for cite in scan_citations(CASE):
        if cite["sha256"] not in known:
            problems.append(
                f"цитата без редакции: {cite['file']}:{cite['line']} цитирует "
                f"{cite['sha256'][:8]}… как {cite['instrument']} — такой редакции нет в "
                "истории прибора (факт называется, а не подгоняется)"
            )
        elif known[cite["sha256"]] != cite["instrument"]:
            problems.append(
                f"цитата под чужим прибором: {cite['file']}:{cite['line']} несёт хеш "
                f"редакции прибора {known[cite['sha256']]}, а отнесена к {cite['instrument']}"
            )
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Страж ADR-023 п.10: реестр редакций приборов кейса"
    )
    parser.add_argument("--case-root", default=".", help="корень кейса (по умолчанию текущий)")
    parser.add_argument("--emit", action="store_true", help="пересобрать снимок аудита")
    parser.add_argument("--stdout", action="store_true", help="печатать снимок, не писать файл")
    args = parser.parse_args(argv)

    global CASE
    case_root = Path(args.case_root).resolve()
    if not case_root.is_dir():
        print(f"NOT-VERIFIED: корень кейса не найден: {case_root}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    CASE = case_root

    try:
        git("rev-parse", "--show-toplevel")
    except Problem as exc:
        print(f"NOT-VERIFIED: git недоступен ({exc}) — история приборов не читается", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    try:
        if args.emit or args.stdout:
            audit = build_audit()
            blob = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
            if args.stdout:
                print(blob, end="")
            else:
                (CASE / AUDIT).write_text(blob, encoding="utf-8")
                per = ", ".join(
                    f"{e['instrument']}: {len(e['revisions'])} ред., {e['citations_total']} цитат"
                    for e in audit["instruments"])
                print(f"снимок пересобран: {AUDIT} ({per})")
            return EXIT_OK
        problems, notes = verify()
    except FileNotFoundError as exc:
        print(f"NOT-VERIFIED: нет входа: {exc}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    except Problem as exc:
        print(f"NOT-VERIFIED: {exc}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    for note in notes:
        print(f"  замечание: {note}")
    if problems:
        print(f"НАРУШЕНИЙ: {len(problems)}")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_FAIL
    print(f"реестр приборов согласован: {REGISTRY} ↔ {AUDIT} ↔ дерево ↔ история "
          f"({len(INSTRUMENTS)} прибора)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
