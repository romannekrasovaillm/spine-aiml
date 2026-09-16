#!/usr/bin/env python3
"""S3i — валидация верификаторов сред E1–E7 на эталонных ответах (ADR-021 п.2).

Вопрос пробы: **поддаются ли верификаторы сред бинаризации** — существует ли
порог, при котором проверка принимает собственный эталон и отвергает заведомо
неверные ответы. Это условие приёмки переработки верификаторов (ADR-021 п.2:
«детерминированная проверка не должна давать ложных нулей; если даёт — среда
исключается целиком, а не подкручивается»).

Проба **ничего не переписывает**: среды (`~/library/rl_envs/`) только читаются.
Импорт модулей верификаторов идёт с запретом байткода (``sys.dont_write_bytecode``
до первого импорта), поэтому ``__pycache__`` в каталоге сред не появляется, а
sha256 и mtime файлов сверяются до и после замера (``source.read_only``).

Метод — один и тот же для всех сред::

    позитив   score(verify(gold, gold))   — эталон против самого себя
    негативы  (а) эталон другой задачи
              (б) обрезанный эталон (первые 20 %)
              (в) эталон с перемешанными строками
              (г) пустой ответ
    зазор     min(позитив) − max(негатив);  зазор > 0 → порог существует

**Проекция score.** Верификаторы возвращают две несовместимые формы: `float`
(`v_define`, `v_extract`, `v_contrast`, `v_relate`, `v_plan`) и
`{gate, components, evidence}` (`common.py::verify_*`). Приведение — явное и
одно на все среды: `float` берётся как есть; словарь даёт `0.0`, если
`gate=False`, иначе **среднее по `components`**. Иначе среда с `gate` выглядела
бы «идеальной» (словарь не сравнивается с числом) и замер был бы несопоставим.

**Проекция эталона.** У сред разный носитель gold: `gold_card` (E1–E3, E7),
`gold_concepts` (E4), `gold_relation` (E5, словарь), `gold_diff` (E6). Эталонный
ответ — это текст, который задача считает правильным; где он не текст (E4, E5),
он **проецируется** (склейка карточек / слаги пары), и проекция названа в
отчёте (`reference_projection`). Проекция — консервативная: она даёт позитиву
максимум, какой носитель gold позволяет, поэтому «ложный ноль» в замере —
свойство верификатора, а не проекции.

**Негатив (в)** не применим к однострочному эталону (перемешивание строк —
тождественная операция): такие случаи помечаются ``applicable: false`` с
причиной и в зазор не входят. Это отмечено в отчёте, а не спрятано.

Коды возврата::

    0 — замер состоялся, и все измеренные среды прошли порог (binary-safe);
        либо замер состоялся при ``--probe-only`` (вердикт без гейта)
    1 — замер состоялся, но есть среды без порога (сигнал архитектору) ИЛИ
        каталог сред изменился во время замера (нарушена read-only граница)
    2 — NOT-VERIFIED: каталог сред/задачи/верификатор отсутствуют или нечитаемы

Запуск::

    python3 tools/validate_verifiers.py --plan            # что и как будет измерено
    python3 tools/validate_verifiers.py                   # замер + evidence + пороги
    python3 tools/validate_verifiers.py --no-evidence     # замер без записи файлов
    python3 tools/validate_verifiers.py --json            # машинный отчёт в stdout
    python3 tools/validate_verifiers.py --envs-root X --n 20 --seed 7 --json
"""

from __future__ import annotations

#: Запрет байткода — до импорта модулей сред: каталог `~/library/rl_envs/`
#: читается, но не должен обрастать `__pycache__` от пробы.
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable, NamedTuple  # noqa: E402

EXIT_OK, EXIT_SIGNAL, EXIT_NOT_VERIFIED = 0, 1, 2

DEFAULT_ENVS_ROOT = "~/library/rl_envs"
DEFAULT_EVIDENCE = "evidence/verifier-validation.json"
DEFAULT_THRESHOLDS = "data/verifier-thresholds.json"

#: Порог бинаризации ищется внутри зазора (max негатива, min позитива]:
#: берётся середина зазора, округлённая вниз к шагу 0.05 — максимальный отступ
#: от обеих границ. Шаг канонический: порог должен быть числом, которое человек
#: назовёт вслух («0.8»), а не 0.8167.
THRESHOLD_STEP = 0.05

NEGATIVE_KINDS = ("other_task", "truncated_20pct", "shuffled_lines", "empty")
NEGATIVE_TITLES = {
    "other_task": "эталон другой задачи",
    "truncated_20pct": "обрезанный эталон (первые 20 %)",
    "shuffled_lines": "перемешанные строки эталона",
    "empty": "пустой ответ",
}


# ============================================================
# Описание сред: как позвать верификатор и что считать эталоном
# ============================================================
class EnvSpec(NamedTuple):
    """Среда: файл задач, вход верификатора и проекция эталона.

    ``call`` — единственное место, где знание о сигнатуре верификатора
    (порядок аргументов, второй gold у E7) не выводится из реестра: в
    ``envs.yaml`` его нет.

    ``diagnosis`` — прочтение замера автором пробы: **механизм**, из-за которого
    негатив проходит (какая именно строка верификатора его пропускает). Числа
    рядом (`negatives`), диагноз — их объяснение; он не заменяет замер, а
    избавляет архитектора от повторного чтения кода.
    """

    key: str
    tasks_file: str
    verifier_file: str
    entry: str
    gold_fields: tuple[str, ...]
    reference: Callable[[dict], str]
    reference_projection: str
    call: Callable[[Any, dict, str], Any]
    notes: tuple[str, ...] = ()
    diagnosis: str = ""


def _extract_gold_text(task: dict) -> str:
    """Gold E4: в банке нет `gold_card`, есть `gold_concepts[].card`.

    Склейка карточек — ближайший аналог «карточки», который верификатор ждёт
    вторым аргументом.
    """
    concepts = task.get("gold_concepts") or []
    cards = [c.get("card", "") for c in concepts if isinstance(c, dict)]
    return "\n\n".join(c for c in cards if c)


def _relate_ref(task: dict) -> str:
    """Эталон E5: пара слагов — текстовый минимум, который верификатор проверяет."""
    rel = task.get("gold_relation") or {}
    return f"{rel.get('from', '')} {rel.get('to', '')}".strip()


ENVS: tuple[EnvSpec, ...] = (
    EnvSpec(
        key="E1_define", tasks_file="define.jsonl", verifier_file="verifiers/v_define.py",
        entry="verify_define", gold_fields=("gold_card",),
        reference=lambda t: t["gold_card"], reference_projection="gold_card (как есть)",
        call=lambda m, t, a: m.verify_define(a, t["gold_card"]),
        diagnosis=("доля пересечения множеств слов: перемешивание строк множество слов не "
                   "меняет, поэтому перемешанный эталон набирает ровно столько же, "
                   "сколько сам эталон. Порядок и структура ответа не различаются вовсе"),
    ),
    EnvSpec(
        key="E2_formula", tasks_file="formula.jsonl", verifier_file="verifiers/common.py",
        entry="verify_formula", gold_fields=("gold_card",),
        reference=lambda t: t["gold_card"], reference_projection="gold_card (как есть)",
        call=lambda m, t, a: m.verify_formula(a, t["gold_card"]),
        diagnosis=("пересечение множеств LaTeX-формул + наличие раздела обозначений: обе "
                   "компоненты инвариантны к порядку строк и к обрезке (формулы остаются "
                   "в тексте). Полная шкала недостижима: эталон набирает 0.4/0.65/0.9 в "
                   "зависимости от того, есть ли в карточке раздел «Обозначения»"),
    ),
    EnvSpec(
        key="E3_classify", tasks_file="classify.jsonl", verifier_file="verifiers/common.py",
        entry="verify_classify", gold_fields=("gold_card",),
        reference=lambda t: t["gold_card"], reference_projection="gold_card (как есть)",
        call=lambda m, t, a: m.verify_classify(a, t["gold_card"]),
        diagnosis=("поиск подстрок type/level/formality в ответе. Все три поля лежат в "
                   "YAML-frontmatter в начале карточки, поэтому обрезка до 20 % и "
                   "перемешивание строк дают 1.0; чужая карточка даёт 1.0, когда совпали "
                   "type и level (formality «A» находится как подстрока почти в любом "
                   "непустом тексте)"),
    ),
    EnvSpec(
        key="E4_extract", tasks_file="extract.jsonl", verifier_file="verifiers/v_extract.py",
        entry="verify_extract", gold_fields=("gold_concepts",),
        reference=_extract_gold_text,
        reference_projection="склейка gold_concepts[].card (в банке нет gold_card)",
        call=lambda m, t, a: m.verify_extract(a, _extract_gold_text(t)),
        notes=("сигнатура верификатора ждёт gold_card, банк несёт gold_concepts — "
               "поля не совпадают",
               "«## Сигнал введения» есть в 15 из 45 задач, и в промпте задачи "
               "не спрашивается (0 вхождений)"),
        diagnosis=("ветка «сигнал введения» срабатывает по эталону, а не по ответу: "
                   "проверка «любое из первых 5 слов цитаты встречается в ответе» "
                   "пропускает почти любой текст — среди этих слов есть «the», а "
                   "ненулевой ответ засчитывается; score зависит от эталона, а не от "
                   "ответа (в 41 задаче из 45 три негатива дали ровно score эталона), "
                   "пустой ответ получает 0.5 «частичного кредита»"),
    ),
    EnvSpec(
        key="E5_relate", tasks_file="relate.jsonl", verifier_file="verifiers/v_relate.py",
        entry="verify_relate", gold_fields=("gold_relation",),
        reference=_relate_ref,
        reference_projection="слаги gold_relation.from + .to (gold — словарь, не текст)",
        call=lambda m, t, a: m.verify_relate(a, t["gold_relation"]),
        notes=("верификатор читает ключи source/target, банк несёт from/to",),
        diagnosis=("ключи не совпадают: rel.get('source') и rel.get('target') пусты, а "
                   "пустая строка — подстрока любого текста, поэтому верификатор "
                   "возвращает 1.0 на любом ответе, включая пустой: проверка константна "
                   "(не различает даже награду за ничего)"),
    ),
    EnvSpec(
        key="E6_contrast", tasks_file="contrast.jsonl", verifier_file="verifiers/v_contrast.py",
        entry="verify_contrast", gold_fields=("gold_diff",),
        reference=lambda t: t["gold_diff"], reference_projection="gold_diff (как есть)",
        call=lambda m, t, a: m.verify_contrast(a, t["gold_diff"]),
        diagnosis=("всего два значения: 0.7 при слове «отличие»/«difference» в ответе и "
                   "0.3 иначе. 36 эталонов из 100 слова «отличие» не содержат (позитив "
                   "0.3 = негатив пустого ответа), а обрезка 20 % оставляет «В отличие…» "
                   "— негатив равен позитиву"),
    ),
    EnvSpec(
        key="E7_repair", tasks_file="repair.jsonl", verifier_file="verifiers/common.py",
        entry="verify_repair", gold_fields=("gold_card", "corrupted_card"),
        reference=lambda t: t["gold_card"], reference_projection="gold_card (как есть)",
        call=lambda m, t, a: m.verify_repair(a, t["corrupted_card"], t["gold_card"]),
        diagnosis=("сверка YAML-frontmatter эталона с frontmatter ответа; если frontmatter "
                   "не разобран, значения вылавливаются регуляркой из любого места текста "
                   "— поэтому обрезка до 20 % (frontmatter в начале карточки) и "
                   "перемешивание строк дают 1.0. Ломает только чужая карточка (0.36–0.78), "
                   "и то не всегда"),
    ),
)

#: Среды реестра, которые проба не измеряет (нет задач) — попадают в отчёт
#: как `skipped`, чтобы «семь из восьми» было видно, а не выглядело «все».
SKIPPED_ENVS = (("E8_plan_experiment", "tasks/plan_experiment.jsonl", "verifiers/v_plan.py"),)


# ============================================================
# Загрузка сред (только чтение)
# ============================================================
class NotVerified(Exception):
    """Вход отсутствует/нечитаем — это не зелёный результат и не красный."""


def load_module(path: Path) -> Any:
    """Импорт верификатора по пути (без байткода — каталог сред только читается)."""
    if not path.is_file():
        raise NotVerified(f"верификатор не найден: {path}")
    spec = importlib.util.spec_from_file_location(f"_verifier_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise NotVerified(f"верификатор не импортируется: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 — чужой модуль: причину надо показать целиком
        raise NotVerified(f"{path}: импорт упал ({type(e).__name__}: {e})") from e
    return module


def read_tasks(path: Path) -> list[dict]:
    """Задачи среды: по одной JSON-записи в строке; пустые строки пропускаются."""
    if not path.is_file():
        raise NotVerified(f"файл задач не найден: {path}")
    out: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise NotVerified(f"{path}:{i}: строка не разбирается как JSON ({e})") from e
            if not isinstance(obj, dict):
                raise NotVerified(f"{path}:{i}: запись не объект")
            obj.setdefault("task_id", f"line:{i}")
            out.append(obj)
    return out


def snapshot(root: Path, files: list[Path]) -> dict[str, dict]:
    """Слепок файлов среды: sha256 + mtime — до и после замера.

    Хешируются и служебные файлы каталога (`__pycache__/*.pyc`), иначе запись
    байткода от импорта осталась бы незамеченной.
    """
    snap: dict[str, dict] = {}
    for p in files:
        try:
            st = p.stat()
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
        snap[str(p.relative_to(root))] = {"sha256": digest, "mtime_ns": st.st_mtime_ns,
                                          "size": st.st_size}
    return snap


def tracked_files(root: Path) -> list[Path]:
    """Что обязано остаться неизменным: верификаторы, реестр и файлы задач проб."""
    out: list[Path] = []
    verifiers = root / "verifiers"
    if verifiers.is_dir():
        out.extend(sorted(p for p in verifiers.rglob("*") if p.is_file()))
    for name in ("envs.yaml",):
        p = root / name
        if p.is_file():
            out.append(p)
    tasks = root / "tasks"
    for spec in ENVS:
        p = tasks / spec.tasks_file
        if p.is_file():
            out.append(p)
    return out


def registry_task_counts(root: Path) -> dict[str, Any]:
    """`task_count` из `envs.yaml` — сверка реестра с фактом (ADR-021 п.5).

    Разбор нарочно простой (regex по строкам): в кейсе нет зависимости от YAML, а
    нужное поле лежит одной строкой под ключом среды. Отсутствие реестра — не
    ошибка: сверка тогда просто не делается.
    """
    path = root / "envs.yaml"
    if not path.is_file():
        return {"available": False, "reason": f"нет {path}"}
    counts: dict[str, int] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^  ([A-Za-z0-9_]+):\s*$", line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"^\s+task_count:\s*(\d+)\s*$", line)
        if m and current:
            counts[current] = int(m.group(1))
    return {"available": True, "task_count": counts}


def registry_diff(root: Path, measured: list[dict], skipped: list[dict]) -> dict[str, Any]:
    """Реестр против факта: где `task_count` расходится с числом строк в банке.

    Это не гейт (реестр — документация, а не данные), а проверка утверждения
    ADR-021 п.5 «реестр устарел»: расхождение видно поимённо.
    """
    reg = registry_task_counts(root)
    if not reg.get("available"):
        return reg
    counts = reg["task_count"]
    rows = []
    for e in measured:
        got, want = e["pool_size"], counts.get(e["env"])
        #: Отсутствие среды в реестре — не расхождение числа: это отдельный
        #: факт (`absent`), иначе «реестр врёт» смешалось бы с «реестр молчит».
        rows.append({"env": e["env"], "registry": want, "actual": got,
                     "matches": None if want is None else want == got})
    for s in skipped:
        key = s["env"]
        want, got = counts.get(key), 0
        rows.append({"env": key, "registry": want, "actual": got,
                     "matches": None if want is None else want == got})
    return {"available": True, "task_count": counts, "rows": rows,
            "stale": [r["env"] for r in rows if r["matches"] is False],
            "absent": [r["env"] for r in rows if r["matches"] is None]}


# ============================================================
# Формирование негативов (детерминированно)
# ============================================================
def negative_answers(spec: EnvSpec, task: dict, pool: list[dict],
                     ref: str) -> dict[str, dict]:
    """Негативы к эталону задачи: (а) чужая, (б) обрезанная, (в) перемешанная, (г) пустая.

    Детерминизм: и выбор «другой задачи», и перемешивание идут от
    `seed|среда|вид|task_id`, поэтому подвыборка задач не влияет на негатив —
    тот же seed даёт тот же набор ответов при любом N.
    """
    out: dict[str, dict] = {}
    task_id = str(task.get("task_id"))

    # (а) эталон другой задачи. Берётся первый в детерминированном порядке
    # перебора, чей эталон отличается от нашего: среда, где у двух задач
    # совпал gold, не должна давать «негатив», равный позитиву.
    rng = random.Random(f"{spec.key}|other|{task_id}")
    start = rng.randrange(len(pool))
    other_ref, other_id = None, None
    for step in range(len(pool)):
        cand = pool[(start + step) % len(pool)]
        if str(cand.get("task_id")) == task_id:
            continue
        try:
            cand_ref = spec.reference(cand)
        except (KeyError, TypeError):
            continue
        if cand_ref and cand_ref != ref:
            other_ref, other_id = cand_ref, str(cand.get("task_id"))
            break
    if other_ref is None:
        out["other_task"] = {"applicable": False,
                             "reason": "нет другой задачи с иным эталоном"}
    else:
        out["other_task"] = {"applicable": True, "answer": other_ref,
                             "source_task_id": other_id}

    # (б) обрезанный эталон — первые 20 %.
    cut = max(1, int(len(ref) * 0.2))
    out["truncated_20pct"] = {"applicable": True, "answer": ref[:cut],
                              "cut_chars": cut, "ref_chars": len(ref)}

    # (в) перемешанные строки. Однострочный эталон перемешать нельзя — операция
    # тождественная, и «негатив» совпал бы с позитивом по построению.
    lines = ref.split("\n")
    if len(lines) < 2:
        out["shuffled_lines"] = {"applicable": False,
                                 "reason": "эталон однострочный — перемешивание строк тождественно"}
    else:
        rng = random.Random(f"{spec.key}|shuffle|{task_id}")
        shuffled = list(lines)
        rng.shuffle(shuffled)
        out["shuffled_lines"] = {"applicable": True, "answer": "\n".join(shuffled),
                                 "lines": len(lines)}

    # (г) пустой ответ.
    out["empty"] = {"applicable": True, "answer": ""}
    return out


# ============================================================
# Замер
# ============================================================
def score_of(raw: Any) -> float:
    """Скалярная проекция ответа верификатора (одна на все среды).

    `float` — как есть. Словарь `{gate, components}` — 0.0 при `gate=False`,
    иначе среднее `components`. Правило названо в отчёте (`method.score_projection`).
    """
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        if raw.get("gate") is False:
            return 0.0
        comp = raw.get("components") or {}
        vals = [float(v) for v in comp.values() if isinstance(v, (int, float))]
        return sum(vals) / len(vals) if vals else 0.0
    raise TypeError(f"верификатор вернул {type(raw).__name__}, а не score")


def measure_call(spec: EnvSpec, module: Any, task: dict, answer: str) -> tuple[float | None, str | None]:
    """Один вызов верификатора: score либо причина отказа (исключение)."""
    try:
        return score_of(spec.call(module, task, answer)), None
    except Exception as e:  # noqa: BLE001 — чужой верификатор: нужен тип и текст
        return None, f"{type(e).__name__}: {e}"


def dist(values: list[float]) -> dict:
    """Распределение score: min/median/max/mean, доли нулей и «не максимума»."""
    if not values:
        return {"n": 0}
    uniq = sorted({round(v, 4) for v in values})
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "median": round(statistics.median(values), 4),
        "max": round(max(values), 4),
        "mean": round(statistics.fmean(values), 4),
        #: Доля ответов с ненулевым score — «ложные срабатывания» негатива.
        "share_gt_0": round(sum(1 for v in values if v > 0) / len(values), 4),
        #: Доля ответов ниже максимума шкалы — «ложные нули» позитива.
        "share_lt_1": round(sum(1 for v in values if v < 1.0) / len(values), 4),
        "distinct_values": uniq[:12],
        "distinct_truncated": len(uniq) > 12,
    }


def propose_threshold(negative_max: float | None, positive_min: float | None) -> tuple[float | None, float | None]:
    """Порог внутри зазора: середина, округлённая вниз к шагу 0.05.

    Возвращает `(порог, зазор)`. Порога нет, если зазор не положителен: равные
    границы (max негатива == min позитива) не разделяются никаким порогом —
    это и есть вердикт «не поддаётся бинаризации».
    """
    if negative_max is None or positive_min is None:
        return None, None
    gap = round(positive_min - negative_max, 4)
    if gap <= 0:
        return None, gap
    mid = (negative_max + positive_min) / 2
    thr = math.floor(mid / THRESHOLD_STEP) * THRESHOLD_STEP
    if not (negative_max < thr <= positive_min):  # округление съело зазор
        thr = round(mid, 3)
    return round(thr, 4), gap


def measure_env(env_root: Path, spec: EnvSpec, n: int, seed: int) -> dict:
    """Замер одной среды: позитив + четыре негатива на N задачах."""
    tasks_path = env_root / "tasks" / spec.tasks_file
    module = load_module(env_root / spec.verifier_file)
    verifier_path = env_root / spec.verifier_file
    verifier_sha = hashlib.sha256(verifier_path.read_bytes()).hexdigest()
    pool = read_tasks(tasks_path)
    if not pool:
        raise NotVerified(f"{tasks_path}: ни одной задачи")

    order = list(range(len(pool)))
    random.Random(f"{seed}|{spec.key}|sample").shuffle(order)
    chosen = sorted(order[:min(n, len(pool))])

    positives: list[float] = []
    neg_values: dict[str, list[float]] = {k: [] for k in NEGATIVE_KINDS}
    neg_skipped: dict[str, int] = {k: 0 for k in NEGATIVE_KINDS}
    errors: list[dict] = []
    skipped_tasks = 0

    for idx in chosen:
        task = pool[idx]
        try:
            ref = spec.reference(task)
        except (KeyError, TypeError) as e:
            skipped_tasks += 1
            errors.append({"task_id": str(task.get("task_id")), "where": "reference",
                           "error": f"{type(e).__name__}: {e}"})
            continue
        if not ref:
            skipped_tasks += 1
            errors.append({"task_id": str(task.get("task_id")), "where": "reference",
                           "error": "пустой эталон"})
            continue

        val, err = measure_call(spec, module, task, ref)
        if err:
            errors.append({"task_id": str(task.get("task_id")), "where": "positive", "error": err})
        else:
            positives.append(val)

        for kind, neg in negative_answers(spec, task, pool, ref).items():
            if not neg["applicable"]:
                neg_skipped[kind] += 1
                continue
            val, err = measure_call(spec, module, task, neg["answer"])
            if err:
                errors.append({"task_id": str(task.get("task_id")), "where": kind, "error": err})
                continue
            neg_values[kind].append(val)

    pos = dist(positives)
    negatives: dict[str, dict] = {}
    for kind in NEGATIVE_KINDS:
        entry = dist(neg_values[kind])
        entry["title"] = NEGATIVE_TITLES[kind]
        entry["applicable"] = bool(neg_values[kind]) or neg_skipped[kind] == 0
        entry["not_applicable"] = neg_skipped[kind]
        if not neg_values[kind] and neg_skipped[kind]:
            entry["reason"] = ("эталон однострочный — перемешивание строк тождественно"
                               if kind == "shuffled_lines"
                               else "нет другой задачи с иным эталоном")
        negatives[kind] = entry

    applicable_max = {k: v["max"] for k, v in negatives.items() if "max" in v}
    negative_max = max(applicable_max.values()) if applicable_max else None
    breaking = sorted(k for k, v in applicable_max.items() if v == negative_max)
    positive_min = pos.get("min")
    threshold, gap = propose_threshold(negative_max, positive_min)
    separable = threshold is not None
    verdict = "binary-safe" if separable else "unsafe"

    reason = _reason(verdict, breaking, applicable_max, pos, threshold)
    return {
        "env": spec.key,
        "tasks_file": f"tasks/{spec.tasks_file}",
        "verifier": f"{spec.verifier_file}::{spec.entry}",
        "verifier_sha256": verifier_sha,
        "gold_fields": list(spec.gold_fields),
        "reference_projection": spec.reference_projection,
        "notes": list(spec.notes),
        "diagnosis": spec.diagnosis,
        "pool_size": len(pool),
        "n": pos.get("n", 0),
        "skipped_tasks": skipped_tasks,
        #: Поля-контракта (имена из отчёта харнесса) — на верхнем уровне,
        #: подробности — рядом, чтобы числа читались без раскопок.
        "positive_min": pos.get("min"),
        "positive_median": pos.get("median"),
        "positive_max": pos.get("max"),
        "false_negatives_share": pos.get("share_lt_1"),
        "negative_max": negative_max,
        "proposed_threshold": threshold,
        "verdict": verdict,
        "positive": pos,
        "negatives": negatives,
        "gap": gap,
        "breaking_negative": breaking,
        "scale_max_reachable": pos.get("max") == 1.0,
        "adr_strict_no_false_negatives": pos.get("share_lt_1") == 0.0,
        "errors": errors[:10],
        "errors_total": len(errors),
        "reason": reason,
    }


def _reason(verdict: str, breaking: list[str], applicable_max: dict[str, float],
            pos: dict, threshold: float | None) -> str:
    """Человеческая причина вердикта: какой негатив ломает разделимость."""
    if verdict == "binary-safe":
        return (f"зазор положителен: эталон отделяется от всех негативов порогом "
                f"{threshold} (max негатива {max(applicable_max.values())}, "
                f"min позитива {pos.get('min')})")
    if not applicable_max:
        return "ни один негатив не удалось построить — замер неполон"
    kinds = ", ".join(f"{k}={applicable_max[k]}" for k in breaking)
    top = applicable_max[breaking[0]]
    extra = []
    if pos.get("max") is not None and pos["max"] < 1.0:
        extra.append(f"максимум шкалы недостижим: лучший позитив {pos['max']}")
    if pos.get("share_lt_1"):
        extra.append(f"доля позитивов < 1.0: {pos['share_lt_1']}")
    tail = ("; " + "; ".join(extra)) if extra else ""
    return (f"нет зазора: максимум негатива {top} равен или выше минимума позитива "
            f"{pos.get('min')} (ломает: {kinds}){tail}")


# ============================================================
# Запись результатов
# ============================================================
#: Поля, которые меняются от прогона к прогону и не описывают замер: в отпечаток
#: замера они не входят, иначе идемпотентность («тот же seed → тот же файл»)
#: ломалась бы на одной отметке времени.
VOLATILE_KEYS = ("generated_at", "fingerprint", "reproduced")


def fingerprint(payload: dict) -> str:
    """sha256 детерминированной части отчёта (без отметки времени и провенанса)."""
    body = {k: v for k, v in payload.items() if k not in VOLATILE_KEYS}
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def write_evidence(path: Path, payload: dict) -> str:
    """Запись evidence. Повторный прогон с тем же замером файл не пачкает.

    Иначе каждый запуск пробы (и теста) менял бы только `generated_at`, и
    «перемерил» выглядело бы как «изменилось» — git-шум вместо провенанса.
    Payload правится на месте (в том числе `reproduced`), чтобы машинный отчёт в
    stdout и файл описывали одно состояние.
    """
    payload["fingerprint"] = fingerprint(payload)
    payload["reproduced"] = False
    if path.is_file():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old = None
        if isinstance(old, dict) and old.get("fingerprint") == payload["fingerprint"]:
            payload["generated_at"] = old.get("generated_at")
            payload["reproduced"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def write_thresholds(path: Path, payload: dict, measured: list[dict], meta: dict) -> str:
    """`data/verifier-thresholds.json` — предложение порогов, не конфиг пайплайна.

    Порог без положительного зазора не выдумывается: у среды без зазора в файле
    стоит `null` и причина, иначе «предложение» читалось бы как разрешение
    бинаризовать среду, которая бинаризации не поддаётся.
    """
    doc = {
        "status": "proposal",
        "not_a_pipeline_config": True,
        "caveat": (f"порог валидирован на {meta['n_per_env']} задачах на среду "
                   f"(seed {meta['seed']}), на домене концептов; это предложение "
                   f"по замеру, а не конфиг пайплайна — в награду не подключается "
                   f"до решения владельца (ADR-021 п.1: кредит остаётся бинарным)"),
        "validated_on": {"n_per_env": meta["n_per_env"], "seed": meta["seed"],
                         "domain": "карточки концептов (~/library/rl_envs/tasks)",
                         "evidence": str(payload.get("evidence_path", DEFAULT_EVIDENCE))},
        "generated_by": "tools/validate_verifiers.py",
        "thresholds": {e["env"]: e["proposed_threshold"] for e in measured},
        "excluded": [
            {"env": e["env"], "reason": e["reason"],
             "negative_max": e["negative_max"], "positive_min": e["positive_min"]}
            for e in measured if e["verdict"] != "binary-safe"
        ],
        "skipped": payload.get("skipped", []),
        "rule": ("порог = середина зазора (max негатива, min позитива], округлённая "
                 "вниз к шагу 0.05; зазор ≤ 0 → порога нет и среда исключается целиком "
                 "(ADR-021 п.2)"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


# ============================================================
# CLI
# ============================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="S3i: валидация верификаторов сред E1–E7 на эталонных ответах "
                    "(позитив = эталон против себя; негативы = чужая задача, "
                    "обрезка 20 %, перемешанные строки, пустой ответ).")
    ap.add_argument("--envs-root", default=DEFAULT_ENVS_ROOT,
                    help=f"каталог сред (только чтение), по умолчанию {DEFAULT_ENVS_ROOT}")
    ap.add_argument("--n", type=int, default=200, help="задач на среду (по умолчанию 200)")
    ap.add_argument("--seed", type=int, default=42, help="сид отбора задач и негативов")
    ap.add_argument("--env", action="append", default=None, metavar="KEY",
                    help="измерять только эту среду (можно повторять)")
    ap.add_argument("--evidence", default=DEFAULT_EVIDENCE, help="куда писать evidence")
    ap.add_argument("--thresholds", default=DEFAULT_THRESHOLDS,
                    help="куда писать предложение порогов")
    ap.add_argument("--no-evidence", action="store_true",
                    help="не писать файлы (только stdout)")
    ap.add_argument("--plan", action="store_true", help="что будет измерено, без замера")
    ap.add_argument("--probe-only", action="store_true",
                    help="вердикт без гейта: exit 0 даже при средах без порога")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.n <= 0:
        print("NOT-VERIFIED: --n должен быть положительным", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    env_root = Path(os.path.expanduser(args.envs_root))
    specs = [s for s in ENVS if not args.env or s.key in args.env]
    if args.env:
        unknown = sorted(set(args.env) - {s.key for s in ENVS})
        if unknown:
            print(f"NOT-VERIFIED: неизвестные среды: {', '.join(unknown)}", file=sys.stderr)
            return EXIT_NOT_VERIFIED

    if args.plan:
        print("== S3i: валидация верификаторов сред (ADR-021 п.2) — план ==")
        print(f"каталог сред: {env_root} (только чтение)")
        print(f"задач на среду: {args.n}, сид: {args.seed}")
        print()
        print(f"{'среда':<18} {'задачи':<16} {'верификатор':<34} эталон")
        for s in specs:
            print(f"{s.key:<18} {s.tasks_file:<16} "
                  f"{s.verifier_file + '::' + s.entry:<34} {s.reference_projection}")
        print()
        print("не измеряются (нет задач): " +
              ", ".join(f"{k} ({f})" for k, f, _v in SKIPPED_ENVS))
        print("позитив: score(verify(gold, gold)); негативы: чужая задача, обрезка 20 %, "
              "перемешанные строки, пустой ответ")
        print("вердикт: binary-safe ⇔ min(позитив) > max(негатив) — существует порог")
        return EXIT_OK

    if not env_root.is_dir():
        print(f"NOT-VERIFIED: каталог сред не найден: {env_root}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    before = snapshot(env_root, tracked_files(env_root))
    measured: list[dict] = []
    skipped: list[dict] = []
    for spec in specs:
        try:
            measured.append(measure_env(env_root, spec, args.n, args.seed))
        except NotVerified as e:
            print(f"NOT-VERIFIED: {e}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
    for key, tasks_file, verifier in SKIPPED_ENVS:
        path = env_root / tasks_file
        try:
            n_tasks = len(read_tasks(path))
        except NotVerified:
            n_tasks = 0
        if n_tasks == 0:
            skipped.append({"env": key, "tasks_file": tasks_file, "verifier": verifier,
                            "reason": f"0 задач в {tasks_file}"})
    after = snapshot(env_root, tracked_files(env_root))

    changed = sorted(set(before) ^ set(after)) + sorted(
        k for k in set(before) & set(after) if before[k] != after[k])
    read_only = {"unchanged": not changed, "files": len(before),
                 "changed": changed[:20], "changed_total": len(changed),
                 "boundary": "каталог сред только читается: sha256+mtime до и после замера"}

    reg = registry_diff(env_root, measured, skipped)
    safe = [e["env"] for e in measured if e["verdict"] == "binary-safe"]
    unsafe = [e["env"] for e in measured if e["verdict"] != "binary-safe"]
    # Условие ADR-021 п.2 сформулировано про «ложные нули», а не про зазор: среда,
    # чей эталон не набирает полную шкалу, отсеивается и при положительном зазоре —
    # порог тогда отделял бы не «верно/неверно», а «эталон/остальное».
    fn_fail = [e["env"] for e in measured if not e["adr_strict_no_false_negatives"]]
    excluded = [
        {"env": e["env"], "reason": e["reason"], "positive_min": e["positive_min"],
         "negative_max": e["negative_max"], "breaking_negative": e["breaking_negative"],
         "false_negatives_share": e["false_negatives_share"],
         "scale_max_reachable": e["scale_max_reachable"]}
        for e in measured if e["verdict"] != "binary-safe"
    ]
    notes = [{"env": e["env"], "note": n} for e in measured for n in e["notes"]]

    payload: dict[str, Any] = {
        "probe": "S3i — валидация верификаторов сред на эталонных ответах",
        "adr": "ADR-021 п.2 (условие приёмки переработки: валидация на эталонных ответах)",
        "question": ("поддаются ли верификаторы сред E1–E7 бинаризации: существует ли "
                     "порог, при котором проверка принимает эталон и отвергает "
                     "заведомо неверные ответы"),
        "generated_at": _now(),
        "method": {
            "positive": "score(verify(gold, gold)) — эталон против самого себя",
            "negatives": {k: NEGATIVE_TITLES[k] for k in NEGATIVE_KINDS},
            "score_projection": ("float как есть; словарь {gate, components} → 0.0 при "
                                 "gate=false, иначе среднее components"),
            "reference_projection": {e["env"]: e["reference_projection"] for e in measured},
            "threshold_rule": ("середина зазора (max негатива, min позитива], округлённая "
                               "вниз к шагу 0.05"),
            "verdict_rule": "binary-safe ⇔ min(позитив) > max(негатив), зазор строго > 0",
            "n_per_env": args.n,
            "seed": args.seed,
            "determinism": ("отбор задач (тасуется сид-порядок, берутся первые N) и "
                            "негативы (сид из seed|среда|вид|task_id) воспроизводимы: "
                            "тот же seed → те же ответы"),
        },
        "source": {
            "envs_root": str(env_root),
            "read_only": read_only,
            "verifiers_sha256": {e["env"]: e["verifier_sha256"] for e in measured},
        },
        "registry": reg,
        "summary": {
            "measured": len(measured), "binary_safe": len(safe), "unsafe": len(unsafe),
            "unsafe_envs": unsafe, "skipped": [s["env"] for s in skipped],
        },
        "adr_021_p2": {
            "condition": ("детерминированная проверка не должна давать ложных нулей; "
                          "если даёт — среда исключается целиком, а не подкручивается"),
            "false_negatives_by_env": {e["env"]: e["false_negatives_share"]
                                       for e in measured},
            "envs_failing_no_false_negatives": fn_fail,
            "envs_failing_separation": unsafe,
            "excluded_as_written": sorted(set(fn_fail) | set(unsafe)),
        },
        "assumptions": [
            "эталонный ответ = носитель gold задачи; где gold не текст (E4 — список "
            "карточек, E5 — словарь), он спроецирован в текст, и проекция названа "
            "в reference_projection",
            "позитив — эталон против самого себя: это верхняя граница того, что "
            "верификатор вообще способен выдать на правильном ответе",
            "негатив (в) не строится для односстрочного эталона (перемешивание строк "
            "тождественно) — такие случаи помечены не применимыми, а не «нулём»",
            "score словарных верификаторов приведён к скаляру (0.0 при gate=false, "
            "иначе среднее components) — без приведения словарь несравним с float",
            "замер — про текущее состояние верификаторов; ни верификаторы, ни envs.yaml "
            "не правились (каталог сред только читается)",
        ],
        "verifiers": {e["env"]: e for e in measured},
        "excluded": excluded,
        "skipped": skipped,
        "notes": notes,
        "checks": {
            "sources_unchanged": read_only["unchanged"],
            "errors_total": sum(e["errors_total"] for e in measured),
            "all_envs_measured": len(measured) == len(specs),
        },
        "verdict_text": _verdict_text(measured, skipped),
    }

    written = None
    if not args.no_evidence:
        payload["evidence_path"] = args.evidence
        written = write_evidence(Path(args.evidence), payload)
        payload["evidence_written"] = written
        payload["thresholds_written"] = write_thresholds(
            Path(args.thresholds), payload, measured,
            {"n_per_env": args.n, "seed": args.seed})

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_report(measured, skipped, read_only, written, args)

    if not read_only["unchanged"]:
        print("VALIDATE VERIFIERS: каталог сред изменился во время замера — "
              "граница read-only нарушена", file=sys.stderr)
        return EXIT_SIGNAL
    if unsafe and not args.probe_only:
        return EXIT_SIGNAL
    return EXIT_OK


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _verdict_text(measured: list[dict], skipped: list[dict]) -> str:
    safe = [e["env"] for e in measured if e["verdict"] == "binary-safe"]
    unsafe = [e["env"] for e in measured if e["verdict"] != "binary-safe"]
    fn_fail = [e["env"] for e in measured if not e["adr_strict_no_false_negatives"]]
    tail = (f"; условие ADR-021 п.2 («без ложных нулей») не проходит у "
            f"{', '.join(fn_fail)}" if fn_fail else
            "; эталон набирает полную шкалу во всех средах")
    if not unsafe:
        return (f"все {len(measured)} измеренных сред разделяются порогом "
                f"(binary-safe: {', '.join(safe)}){tail}")
    if not safe:
        return (f"ни одна из {len(measured)} измеренных сред не разделяется порогом в "
                f"текущем виде: {', '.join(unsafe)} — бинаризация требует переработки "
                f"верификатора, а не подбора порога (ADR-021 п.2){tail}")
    return (f"разделяются порогом: {', '.join(safe)}; не разделяются: "
            f"{', '.join(unsafe)}{tail}")


def _print_report(measured: list[dict], skipped: list[dict], read_only: dict,
                  written: str | None, args: argparse.Namespace) -> None:
    print("== S3i / ADR-021 п.2: валидация верификаторов сред на эталонных ответах ==")
    print(f"каталог сред: {os.path.expanduser(args.envs_root)} (только чтение)")
    print(f"задач на среду: {args.n}, сид: {args.seed}")
    print()
    print(f"{'среда':<14} {'n':>4} {'позитив min/med/max':<22} "
          f"{'негатив max':>11} {'порог':>6}  вердикт")
    for e in measured:
        p = e["positive"]
        thr = e["proposed_threshold"]
        pos_txt = f"{p.get('min')}/{p.get('median')}/{p.get('max')}"
        print(f"{e['env']:<14} {e['n']:>4} {pos_txt:<22} "
              f"{str(e['negative_max']):>11} {('—' if thr is None else thr):>6}  {e['verdict']}")
    print()
    print("негативы по видам (max score, доля > 0):")
    for e in measured:
        parts = []
        for k in NEGATIVE_KINDS:
            n = e["negatives"][k]
            if "max" in n:
                parts.append(f"{k} {n['max']} ({n['share_gt_0']})")
            else:
                parts.append(f"{k} n/a")
        print(f"  {e['env']:<14} " + "; ".join(parts))
    print()
    for e in measured:
        if e["verdict"] != "binary-safe":
            print(f"  - {e['env']}: {e['reason']}")
    print()
    print("почему негатив проходит (прочтение кода рядом с числами выше):")
    for e in measured:
        print(f"  {e['env']}: {e['diagnosis']}")
    if skipped:
        print()
        print("не измерялись: " + ", ".join(f"{s['env']} ({s['reason']})" for s in skipped))
    if read_only["changed"]:
        print()
        print(f"ВНИМАНИЕ: изменены файлы сред: {', '.join(read_only['changed'])}")
    print()
    print(f"read-only: файлов под слепком {read_only['files']}, "
          f"изменено {read_only['changed_total']}")
    if written:
        print(f"evidence: {written}")
        print(f"пороги:   {args.thresholds} (предложение, не конфиг пайплайна)")
    safe = [e["env"] for e in measured if e["verdict"] == "binary-safe"]
    unsafe = [e["env"] for e in measured if e["verdict"] != "binary-safe"]
    print()
    if not unsafe:
        print(f"VERIFIERS OK: все среды разделяются порогом ({len(safe)})")
    else:
        print(f"VERIFIERS: без порога {len(unsafe)} из {len(measured)} "
              f"({', '.join(unsafe)}) — это сигнал архитектору (ADR-021 п.2: "
              f"среда исключается целиком, а не подкручивается)")


if __name__ == "__main__":
    sys.exit(main())
