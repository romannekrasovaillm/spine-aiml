#!/usr/bin/env python3
"""C-008 — поведенческий страж AD-3 «токенизационное пространство — единый контракт».

Проверяет три независимых вещи и только при их совпадении печатает
``SPECIAL_TOKENS OK``:

1. **Токен-пространство.** Все 8 спецтокенов контура разрешаются токенизатором
   в ОДИН id (не в последовательность сабтокенов) и id различны. Это прямой
   страж дефекта 17.08: ``pretokenize.py`` не вызывал ``add_special_tokens`` →
   ``<tool_response>`` оставался сабтокенами → ``TOOL_RESP_IDS`` молча не
   маскировал → модель училась генерировать вывод инструмента (``AGENTS.md`` §10).
   Проверка повторяет sanity-assert из ``pretokenize_v9.py``.

2. **Вхождения в кэше.** Каждый спецтокен встречается в претокен-кэше ненулевое
   число раз (AD-3: «любой кэш, попадающий в обучение, обязан содержать
   ненулевые вхождения всех спецтокенов»).

3. **Путь претокенизации.** В скриптах контура (``build_cpt_v12r_mix.py``,
   ``build_cpt_v12r_pos.py``, ``build_cpt_v12r_docs.py``, ``pretokenize*.py``)
   есть вызов ``add_special_tokens(...)`` с тем же списком ``SPECIAL_TOKENS``.
   Резервные копии (``*.bak*``) — не путь исполнения и в гейт не входят,
   но перечисляются в отчёте.

Политика вхождений (``--occurrences``) — трёхуровневая, по ADR-004:

* уровень 1 — **токен-пространство** (проверяется всегда, независимо от флага):
  все 8 токенов разрешаются в один id;
* уровень 2 — **вхождения 6 токенов формата v12** (``<think>``, ``</think>``,
  ``<tool_call>``, ``</tool_call>``, ``<tool_response>``, ``</tool_response>``):
  гейт по умолчанию (``chat-format``), потому что именно эти токены несёт
  обучающий формат;
* уровень 3 — **неиспользуемые теги** (``<reasoning>``, ``</reasoning>``):
  вхождений не требуем. Их нет в текстах корпуса (0 вхождений во всех 42 кэшах
  контура), и дополнять корпус ими запрещено — это фабрикация данных в обход
  заморозки ADR-003. Отсутствие — не дефект, значения печатаются как справка.

``all8`` (буквальная формулировка прежней редакции C-008) остаётся доступным
**явным флагом** для разовых проверок, но дефолтом не является: гейт, красный
на замороженном корпусе по причине, которую нельзя устранить иначе как правкой
данных, обесценивает сигнал.

Выбранная политика всегда печатается в отчёте.

Коды возврата::

    0 — SPECIAL_TOKENS OK
    1 — FAIL: перечисленные проверки не прошли
    2 — NOT-VERIFIED: нет входа (кэш/токенизатор не найдены) — не зелёный

Запуск::

    python3 tools/check_special_tokens.py [--dataset PATH] [--tokenizer PATH_OR_ID]
                                          [--contour DIR] [--occurrences all8|chat-format]
                                          [--json]

``--dataset`` допускает отсутствие значения: правило C-008 передаёт
``--dataset ${LAGUNA_TOK_CACHE}``, и при невыставленной переменной bash
убирает слово — скрипт обязан перейти к автоопределению кэша.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

#: Контракт AD-3 — ровно этот список и ровно в этом порядке (порядок задаёт id).
SPECIAL_TOKENS = ["<think>", "</think>", "<tool_call>", "</tool_call>",
                  "<tool_response>", "</tool_response>", "<reasoning>", "</reasoning>"]

#: Токены чат-формата v12 (SFT/RL/eval); последние два — legacy-теги.
CHAT_FORMAT_TOKENS = SPECIAL_TOKENS[:6]

#: Теги модели по тегу кэша — копия ``TOKENIZERS`` из ``build_cpt_v12r_mix.py``.
TOKENIZERS = {"qwen25": "Qwen/Qwen2.5-0.5B",
              "qwen3": "Qwen/Qwen3-0.6B",
              "qwen35": "Qwen/Qwen3.5-0.8B-Base"}

#: Скрипты претокенизации контура (glob относительно ``--contour``).
PRETOK_GLOBS = ["build_cpt_v12r_mix.py", "build_cpt_v12r_pos.py",
                "build_cpt_v12r_docs.py", "pretokenize*.py"]

#: Резервные копии — не путь исполнения.
BACKUP_RE = re.compile(r"\.bak|~$|\.orig$")

#: Кэш ревизии: замороженный микс v12r под целевое семейство (ADR-002).
PREFERRED_CACHES = ["cpt_corpus_v12r_8192_qwen25.npy",
                    "sft_train_v12_8192_qwen25.npz"]

DEFAULT_CONTOUR = "/home/user/gb10-shared"

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2


class NotVerified(Exception):
    """Недостаточно входа для суждения — это не «зелено»."""


# ── разрешение входа ──────────────────────────────────────────────────────────

def _unexpanded(value: str | None) -> bool:
    """``${VAR}``/``$VAR`` как литерал (bash не подставил переменную) — не путь."""
    return bool(value) and (value.startswith("$") or "${" in value)


def resolve_dataset(arg: str | None, contour: Path) -> tuple[Path, str]:
    """Кэш для аудита: явный аргумент → переменная окружения → предпочтение → первый."""
    if arg and not _unexpanded(arg):
        p = Path(arg)
        if not p.is_file():
            raise NotVerified(f"--dataset: файл не найден: {p}")
        return p, "argument --dataset"
    env = os.environ.get("LAGUNA_TOK_CACHE")
    if env and not _unexpanded(env):
        p = Path(env)
        if not p.is_file():
            raise NotVerified(f"LAGUNA_TOK_CACHE: файл не найден: {p}")
        return p, "env LAGUNA_TOK_CACHE"
    tok_dir = contour / "datasets" / "tok"
    if not tok_dir.is_dir():
        raise NotVerified(f"нет каталога кэшей: {tok_dir}")
    caches = sorted([p for p in tok_dir.iterdir() if p.suffix in (".npy", ".npz")])
    for name in PREFERRED_CACHES:
        p = tok_dir / name
        if p.is_file():
            return p, f"preference list ({name})"
    if not caches:
        raise NotVerified(f"в {tok_dir} нет *.npy/*.npz")
    return caches[0], f"first cache in {tok_dir.name}/ ({caches[0].name})"


def resolve_tokenizer(arg: str | None, dataset: Path, contour: Path) -> tuple[object, str]:
    """Загружает токенизатор: явный путь/id → тег кэша → HF-кэш → контур."""
    try:
        from tokenizers import Tokenizer
    except ImportError as e:  # pragma: no cover — зависимость среды
        raise NotVerified(f"библиотека tokenizers недоступна: {e}") from e

    cands: list[tuple[str, str]] = []
    if arg:
        cands.append((arg, "argument --tokenizer"))
    tag = next((t for t in TOKENIZERS if f"_{t}" in dataset.name), None)
    if tag:
        cands.append((TOKENIZERS[tag], f"tag _{tag} в имени кэша"))
    cands.append(("Qwen/Qwen2.5-0.5B", "целевое семейство ADR-002"))

    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    for ref, why in cands:
        p = Path(ref)
        if p.is_file():
            return Tokenizer.from_file(str(p)), f"{why}: {p}"
        if (p / "tokenizer.json").is_file():
            return Tokenizer.from_file(str(p / "tokenizer.json")), f"{why}: {p}"
        snap = hub / ("models--" + ref.replace("/", "--")) / "snapshots"
        if snap.is_dir():
            for tj in sorted(snap.glob("*/tokenizer.json")):
                return Tokenizer.from_file(str(tj)), f"{why}: {tj}"
        local = contour / "models" / Path(ref).name
        if (local / "tokenizer.json").is_file():
            return Tokenizer.from_file(str(local / "tokenizer.json")), f"{why}: {local}"
    raise NotVerified("токенизатор не найден ни в HF-кэше, ни в контуре; "
                      "передай --tokenizer PATH/tokenizer.json")


# ── проверки ─────────────────────────────────────────────────────────────────

def check_token_space(tk) -> tuple[dict[str, int], list[str]]:
    """Регистрирует контракт в токенизаторе и проверяет, что каждый токен — один id.

    ``add_special_tokens`` — тот же вызов, что в ``build_cpt_v12r_mix.py`` /
    ``pretokenize_v9.py``: без него половина контракта (`<think>`,
    `<tool_response>`, `<reasoning>`) в токенизаторе не существует, а
    `<tool_call>`/`</tool_call>` есть уже в базовом Qwen2.5.
    """
    try:
        added = tk.add_special_tokens(SPECIAL_TOKENS)
    except Exception as e:  # pragma: no cover — несовместимая версия tokenizers
        return {}, [f"add_special_tokens не сработал: {e}"]
    ids: dict[str, int] = {}
    problems: list[str] = []
    if added != len(SPECIAL_TOKENS):
        problems.append(f"add_special_tokens зарегистрировал {added} из "
                        f"{len(SPECIAL_TOKENS)} токенов контракта")
    for t in SPECIAL_TOKENS:
        tid = tk.token_to_id(t)
        if tid is None:
            problems.append(f"{t}: нет в токенизаторе (token_to_id → None)")
            continue
        n = len(tk.encode(t, add_special_tokens=False).ids)
        if n != 1:
            problems.append(f"{t}: не одиночный токен — {n} сабтокенов (id {tid}) "
                            f"[класс дефекта 17.08]")
            continue
        ids[t] = tid
    if len(set(ids.values())) != len(ids):
        problems.append("id спецтокенов не уникальны: "
                        f"{sorted(ids.items(), key=lambda kv: kv[1])}")
    return ids, problems


def token_keys(files: list[str]) -> list[str]:
    """Ключи .npz, в которых лежат токены, а не маски/длины.

    Контур использует три раскладки: ``input_ids``/``attention_mask`` (SFT),
    ``tokens_flat``/``doc_lens`` (docs-кэши), одиночный массив (.npy). Маски и
    длины в скан не берём — это не токены, и совпадение с id спецтокена в них
    было бы ложным сигналом.
    """
    for key in ("input_ids",):
        if key in files:
            return [key]
    for marker in ("token", "id"):
        hits = [k for k in files if marker in k.lower()]
        if hits:
            return hits
    skip = {"attention_mask", "doc_lens", "lens", "lengths", "loss_mask"}
    rest = [k for k in files if k.lower() not in skip]
    return rest or list(files)


def _arrays(path: Path):
    """Итерирует int-массивы кэша: .npy → один массив, .npz → ключи с токенами."""
    import numpy as np
    d = np.load(str(path), mmap_mode="r")
    if hasattr(d, "files"):
        return d, [(k, d[k]) for k in token_keys(list(d.files))]
    return d, [("arr", d)]


def count_occurrences(path: Path, ids: dict[str, int], max_tokens: int | None) -> dict[str, int]:
    """Считает вхождения id по всему кэшу (mmap, чанками — файл не читается в RAM)."""
    import numpy as np
    if not ids:
        return {}
    lo, hi = min(ids.values()), max(ids.values())
    counts = {t: 0 for t in ids}
    obj, arrays = _arrays(path)
    for _name, arr in arrays:
        step = 4_000_000
        seen = 0
        for i in range(0, arr.shape[0], step):
            chunk = np.asarray(arr[i:i + step]).ravel()
            if max_tokens is not None:
                chunk = chunk[: max(0, max_tokens - seen)]
            seen += chunk.size
            sel = chunk[(chunk >= lo) & (chunk <= hi)]
            if sel.size:
                bc = np.bincount(sel.astype(np.int64) - lo, minlength=hi - lo + 1)
                for t, tid in ids.items():
                    counts[t] += int(bc[tid - lo])
            if max_tokens is not None and seen >= max_tokens:
                break
    return counts


def check_pretokenize_scripts(contour: Path) -> tuple[list[Path], list[str], list[Path]]:
    """Ищет ``add_special_tokens(...SPECIAL_TOKENS)`` на пути претокенизации."""
    import ast
    scripts: list[Path] = []
    for g in PRETOK_GLOBS:
        scripts.extend(sorted(p for p in contour.glob(g) if not BACKUP_RE.search(p.name)))
    backups = sorted(p for g in PRETOK_GLOBS for p in contour.glob(g) if BACKUP_RE.search(p.name))
    problems: list[str] = []
    for p in scripts:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            problems.append(f"{p.name}: не разобран как Python ({e})")
            continue
        ok = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "add_special_tokens":
                continue
            src = ast.dump(node)
            if "SPECIAL_TOKENS" in src:
                ok = True
                break
        if not ok:
            problems.append(f"{p.name}: нет вызова add_special_tokens(...SPECIAL_TOKENS) "
                            f"[класс дефекта 17.08]")
    if not scripts:
        problems.append(f"в {contour} не найдено ни одного скрипта претокенизации "
                        f"({', '.join(PRETOK_GLOBS)})")
    return scripts, problems, backups


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="C-008: спецтокены AD-3 — токен-пространство, вхождения в кэше, "
                    "add_special_tokens на пути претокенизации.")
    ap.add_argument("--dataset", nargs="?", const=None, default=None,
                    help="претокен-кэш (.npy/.npz); без значения — автоопределение")
    ap.add_argument("--tokenizer", default=None, help="путь к tokenizer.json или HF-id")
    ap.add_argument("--contour", default=os.environ.get("LAGUNA_CONTOUR", DEFAULT_CONTOUR),
                    help=f"корень контура «Лагуна» (по умолчанию {DEFAULT_CONTOUR})")
    ap.add_argument("--occurrences", choices=("all8", "chat-format"), default="chat-format",
                    help="политика гейта по вхождениям: chat-format (ADR-004, "
                         "дефолт) — 6 токенов формата v12; all8 — буквальные 8 "
                         "токенов, явный флаг для разовых проверок")
    ap.add_argument("--max-tokens", type=int, default=None,
                    help="ограничить скан первыми N токенами (диагностика)")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    ap.add_argument("--contour-only", action="store_true",
                    help="проверить только путь претокенизации (без кэша)")
    args = ap.parse_args(argv)

    contour = Path(args.contour)
    report: dict = {"check": "C-008", "occurrences_policy": args.occurrences,
                    "contour": str(contour), "problems": [], "info": []}
    try:
        scripts, static_problems, backups = check_pretokenize_scripts(contour)
    except OSError as e:
        print(f"NOT-VERIFIED: контур недоступен: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    report["pretokenize_scripts"] = [p.name for p in scripts]
    report["backup_scripts_skipped"] = [p.name for p in backups]
    report["problems"].extend(static_problems)

    ids: dict[str, int] = {}
    counts: dict[str, int] = {}
    empty: list[str] = []
    dataset = None

    if not args.contour_only:
        try:
            dataset, why_ds = resolve_dataset(args.dataset, contour)
            tk, why_tk = resolve_tokenizer(args.tokenizer, dataset, contour)
        except NotVerified as e:
            report["problems"].append(f"NOT-VERIFIED: {e}")
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(f"NOT-VERIFIED: {e}", file=sys.stderr)
            return EXIT_NOT_VERIFIED
        report["dataset"] = str(dataset)
        report["dataset_resolved_by"] = why_ds
        report["tokenizer"] = why_tk

        ids, space_problems = check_token_space(tk)
        report["problems"].extend(space_problems)
        try:
            counts = count_occurrences(dataset, ids, args.max_tokens)
        except (OSError, ValueError) as e:
            report["problems"].append(f"NOT-VERIFIED: не читается кэш {dataset}: {e}")
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            return EXIT_NOT_VERIFIED
        gated = SPECIAL_TOKENS if args.occurrences == "all8" else CHAT_FORMAT_TOKENS
        empty = [t for t in gated if counts.get(t, 0) == 0]
        report["token_table"] = [{"token": t, "id": ids.get(t), "occurrences": counts.get(t),
                                  "gated": t in gated} for t in SPECIAL_TOKENS]
        for t in empty:
            extra = ""
            if args.occurrences == "chat-format" and t not in CHAT_FORMAT_TOKENS:
                extra = " (вне гейта chat-format)"
            report["problems"].append(f"{t} (id {ids.get(t)}): 0 вхождений в кэше{extra}")
        if args.occurrences == "chat-format":
            for t in SPECIAL_TOKENS:
                if t not in CHAT_FORMAT_TOKENS and counts.get(t, 0) == 0:
                    report["info"].append(f"{t}: 0 вхождений — вне гейта chat-format")

    ok = not report["problems"]
    report["ok"] = ok

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_OK if ok else EXIT_FAIL

    print("== C-008 / AD-3: токенизационное пространство ==")
    if dataset:
        print(f"dataset:   {dataset}   [{report['dataset_resolved_by']}]")
        print(f"tokenizer: {report['tokenizer']}")
    print(f"policy:    occurrences={args.occurrences}"
          f"{' (ограниченный скан)' if args.max_tokens else ''}")
    print()
    if ids:
        print(f"{'токен':<18}{'id':>8}  {'вхождений':>12}  гейт")
        for t in SPECIAL_TOKENS:
            gated = "да" if (t in SPECIAL_TOKENS if args.occurrences == "all8"
                             else t in CHAT_FORMAT_TOKENS) else "—"
            print(f"{t:<18}{ids.get(t, -1):>8}  {counts.get(t, 0):>12}  {gated}")
        print()
    print(f"скрипты претокенизации: {', '.join(report['pretokenize_scripts']) or '—'}")
    if backups:
        print(f"резервные копии вне гейта: {', '.join(report['backup_scripts_skipped'])}")
    print()
    if report["problems"]:
        print(f"FAIL ({len(report['problems'])}):")
        for p in report["problems"]:
            print(f"  - {p}")
        legacy = [t for t in (empty or []) if t not in CHAT_FORMAT_TOKENS]
        if legacy and args.occurrences == "all8":
            print()
            print(f"  подсказка: провалились только legacy-теги {legacy} — их нет в тексте")
            print("  корпуса (0 вхождений в источнике), а не в токенизаторе. Токен-пространство")
            print("  цело: все 8 разрешаются в один id. Это разовая проверка all8; гейт C-008")
            print("  работает по политике chat-format (ADR-004): 6 токенов формата v12 → exit 0.")
        return EXIT_FAIL
    print("SPECIAL_TOKENS OK")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
