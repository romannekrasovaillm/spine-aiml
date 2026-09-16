#!/usr/bin/env python3
"""S2 / ADR-007 п.1 — сборка **пула ревизии** RL без задач, заученных в SFT.

Решение ADR-007: восемь RL-задач, чьи промпты дословно присутствуют в
``sft_train_v12.jsonl``, из пула ревизии **исключаются**. Причина не в дефекте
сборки пулов (RL вправе дожимать те же промпты), а в оси AD-1: SFT собран из
дистилляций учителя, то есть содержит *ответы* на эти промпты, и награда на них
может быть получена «по памяти», а не решением. Сравнение RL vs SFT теряет смысл
ровно на них.

Что инструмент делает и чего не делает:

* читает **только** ``datasets/rl_tasks_oxalpha.jsonl`` и
  ``datasets/sft_train_v12.jsonl`` (AD-7: источники заморожены, ни один байт в
  них не пишется);
* пишет только в ``runs/rev-pool/``: сам пул, отчёт об исключениях и маркер
  «каталог не является прогоном» (см. ниже);
* **не** трогает корпус, SFT и eval-набор — фильтруется исключительно пул
  ревизии, и это записано в ADR-007 как требование к пулу, а не к данным.

Критерий исключения — механический: нормализованный текст промпта RL-задачи
совпадает с пользовательским текстом хотя бы одного примера SFT. Нормализация и
сам замер берутся из ``check_sft_rl_overlap.py`` / ``check_eval_leakage.py`` (одна
реализация нормы на все стражи; иначе «0 совпадений» и «8 исключений» будут
считать разное). Исключаются задачи по **номеру строки** RL-пула: у задач
``rl_tasks_oxalpha`` нет поля-идентификатора, и позиция — единственный
однозначный ключ.

Сохранение строк. Оставшиеся задачи пишутся **дословно исходными строками**
файла-источника, а не пересборкой JSON: пересборка меняет порядок/формат ключей и
превращает «отфильтровали пул» в «переписали пул». Провенанс (хеши, критерий,
список исключённых с причинами) живёт в отдельном ``exclusions.json``.

Маркер ``NOT_A_RUN``. Каталог ``runs/rev-pool/`` лежит рядом с каталогами
прогонов по требованию правила C-017, но прогоном не является: манифест AD-2 у
него был бы фикцией. Чтобы страж C-012 не считал пул прогоном без манифеста,
инструмент кладёт в каталог файл ``NOT_A_RUN`` с причиной — явное утверждение
вместо молчаливого исключения по имени каталога.

``--expect-excluded N`` — кросс-проверка против зафиксированного в ADR-007 числа
(8 задач). Расхождение — сигнал: либо пересечение пулов изменилось, либо сломан
критерий. Молча «перемерить» и продолжить здесь нельзя: исключение задач из пула
ревизии — это то, что определяет ось сравнения. ``--no-expect`` отключает сверку
осознанно (например, при смене пула новым ADR).

Коды возврата::

    0 — пул записан (или уже существует с тем же содержимым)
    1 — отказ: пул существует и отличается (нужен --force) ИЛИ расхождение с
        ожидаемым числом исключённых
    2 — NOT-VERIFIED: вход отсутствует/нечитаем

Запуск::

    python3 tools/build_rev_pool.py                      # симлинки кейса, сверка с ADR-007
    python3 tools/build_rev_pool.py --json               # машинный отчёт
    python3 tools/build_rev_pool.py --no-expect          # без сверки числа
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_eval_leakage import NotVerified  # noqa: E402
from check_sft_rl_overlap import (  # noqa: E402
    EXIT_NOT_VERIFIED,
    EXIT_OK,
    rl_prompts,
    scan_sft,
)

EXIT_REFUSE = 1

DEFAULT_SFT = "datasets/sft_train_v12.jsonl"
DEFAULT_RL = "datasets/rl_tasks_oxalpha.jsonl"
DEFAULT_OUT = "runs/rev-pool/rl_pool_filtered.jsonl"
DEFAULT_REPORT = "runs/rev-pool/exclusions.json"

#: Число исключаемых задач, зафиксированное ADR-007 п.1 (замер S1: 39 пар / 8 задач).
ADR007_EXPECTED_EXCLUDED = 8

#: Маркер «каталог не является прогоном» — читается стражем C-012
#: (``tools/check_run_manifest.py``). Первая строка файла — причина.
NOT_A_RUN_MARKER = "NOT_A_RUN"

EXCLUDED_REASON = ("prompt_verbatim_in_sft_train_v12: нормализованный промпт "
                   "встречается в примерах SFT v12 — награда на такой задаче может "
                   "быть получена по памяти, а не решением (ADR-007 п.1)")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def build(sft_path: Path, rl_path: Path, expect_excluded: int | None) -> dict:
    """Замер пересечения и разбор исключаемых задач (без записи на диск)."""
    index, n_rl = rl_prompts(rl_path, None)
    #: ``max_matches=0`` — перечислить ВСЕ пары: по ним строится причина исключения
    #: поимённо (сколько примеров SFT «знают» ответ на эту задачу), а не только счёт.
    res = scan_sft(sft_path, index, None, 0)
    excluded_lines = set(res["matched_rl_lines"])

    by_line: dict[int, list[dict]] = {}
    for m in res["matches"]:
        by_line.setdefault(m["rl_line"], []).append(m)

    excluded = []
    for line_no in sorted(excluded_lines):
        pairs = by_line.get(line_no, [])
        excluded.append({
            "rl_line": line_no,
            "rl_id": pairs[0]["rl_id"] if pairs else f"line:{line_no}",
            "rl_task_type": pairs[0]["rl_task_type"] if pairs else None,
            "normalized_sha1": pairs[0]["normalized_sha1"] if pairs else None,
            "prompt_preview": pairs[0]["prompt_preview"] if pairs else None,
            "sft_examples_matched": len({p["sft_line"] for p in pairs}),
            "sft_lines": sorted({p["sft_line"] for p in pairs}),
            "reason": EXCLUDED_REASON,
        })
    by_type: dict[str, int] = {}
    for e in excluded:
        by_type[str(e["rl_task_type"])] = by_type.get(str(e["rl_task_type"]), 0) + 1
    return {
        "rl_tasks_total": n_rl,
        "rl_unique_prompts": len(index),
        "sft_examples": res["sft_examples"],
        "sft_matched_prompts": res["sft_matched_prompts"],
        "sft_rl_pairs": res["sft_rl_pairs"],
        "excluded_count": len(excluded),
        "retained_count": n_rl - len(excluded),
        "excluded_by_task_type": by_type,
        "excluded": excluded,
        "expect_excluded": expect_excluded,
        "expect_matched": bool(expect_excluded is not None
                               and len(excluded) == expect_excluded),
        "matched_rl_ids": res["matched_rl_ids"],
    }


def write_pool(rl_path: Path, out_path: Path, excluded_lines: set[int]) -> dict:
    """Пишет пул ревизии: оставшиеся строки — дословно как в источнике."""
    kept, dropped = 0, 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    ends_with_newline = True
    with rl_path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
        for i, line in enumerate(src, start=1):
            if not line.strip():
                continue
            if i in excluded_lines:
                dropped += 1
                continue
            dst.write(line)
            kept += 1
            ends_with_newline = line.endswith("\n")
        if not ends_with_newline and kept:
            dst.write("\n")
    tmp.replace(out_path)
    return {"written_lines": kept, "dropped_lines": dropped,
            "sha256": sha256_file(out_path), "path": str(out_path)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="S2/ADR-007: собрать пул ревизии RL без задач, промпты которых "
                    "дословно присутствуют в SFT v12 (источники не меняются).")
    ap.add_argument("--sft", default=DEFAULT_SFT, help=f"SFT-пул (.jsonl), по умолчанию {DEFAULT_SFT}")
    ap.add_argument("--rl", default=DEFAULT_RL, help=f"RL-пул (.jsonl), по умолчанию {DEFAULT_RL}")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"пул ревизии (по умолчанию {DEFAULT_OUT})")
    ap.add_argument("--report", default=DEFAULT_REPORT,
                    help=f"отчёт об исключениях (по умолчанию {DEFAULT_REPORT})")
    ap.add_argument("--expect-excluded", type=int, default=ADR007_EXPECTED_EXCLUDED,
                    help=f"кросс-проверка числа исключённых задач (по умолчанию "
                         f"{ADR007_EXPECTED_EXCLUDED} — ADR-007 п.1)")
    ap.add_argument("--no-expect", action="store_true",
                    help="не сверять число исключённых с ADR-007 (осознанный отказ от сверки)")
    ap.add_argument("--force", action="store_true", help="перезаписать существующий пул")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    args = ap.parse_args(argv)

    sft_path, rl_path = Path(args.sft), Path(args.rl)
    out_path, report_path = Path(args.out), Path(args.report)
    for p, what in ((sft_path, "--sft"), (rl_path, "--rl")):
        if not p.is_file():
            print(f"NOT-VERIFIED: {what}: файл не найден: {p}", file=sys.stderr)
            return EXIT_NOT_VERIFIED

    expect = None if args.no_expect else args.expect_excluded
    try:
        info = build(sft_path, rl_path, expect)
    except NotVerified as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    except OSError as e:
        print(f"NOT-VERIFIED: {e}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    excluded_lines = {e["rl_line"] for e in info["excluded"]}
    payload = {
        "stage": "S2",
        "decision": "ADR-007 п.1: RL-задачи, чьи промпты дословно присутствуют в "
                    "SFT v12, исключаются из пула ревизии",
        "sources": {
            "rl": args.rl, "rl_sha256": sha256_file(rl_path),
            "sft": args.sft, "sft_sha256": sha256_file(sft_path),
        },
        "pool": args.out,
        "criterion": ("нормализованный текст промпта RL-задачи совпадает с "
                      "пользовательским текстом (role=user) хотя бы одного примера SFT"),
        "normalization": "lower + схлопнутые пробелы (norm из tools/check_eval_leakage.py)",
        "id_scheme": "rl_id = line:<N> (у задач oxalpha нет поля-идентификатора)",
        "counts": {
            "rl_tasks_total": info["rl_tasks_total"],
            "excluded": info["excluded_count"],
            "retained": info["retained_count"],
            "sft_examples": info["sft_examples"],
            "sft_matched_prompts": info["sft_matched_prompts"],
            "sft_rl_pairs": info["sft_rl_pairs"],
            "excluded_by_task_type": info["excluded_by_task_type"],
        },
        "cross_check": {
            "expected_excluded": expect,
            "actual_excluded": info["excluded_count"],
            "matched": info["expect_matched"],
            "source": "ADR-007 п.1 (замер S1: 39 пар / 8 задач)",
        },
        "excluded": info["excluded"],
        "data_sources_unchanged": True,
        "note": ("источники (корпус, SFT, eval) не изменяются: отбор живёт на уровне "
                 "пула ревизии (AD-7 — заморозка)"),
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK if info["expect_matched"] else EXIT_REFUSE

    print("== S2 / ADR-007: пул ревизии RL без заученных в SFT задач ==")
    print(f"rl:  {rl_path}  задач: {info['rl_tasks_total']}")
    print(f"sft: {sft_path}  примеров: {info['sft_examples']}")
    print()
    print(f"исключается: {info['excluded_count']} задач "
          f"({', '.join(f'{k}: {v}' for k, v in sorted(info['excluded_by_task_type'].items()))}), "
          f"остаётся: {info['retained_count']}")
    print(f"пар sft↔rl под исключение: {info['sft_rl_pairs']} "
          f"(промптов SFT затронуто: {info['sft_matched_prompts']})")
    print()
    for e in info["excluded"]:
        print(f"  - rl {e['rl_id']} [{e['rl_task_type']}] sha1 {e['normalized_sha1']} "
              f"— {e['sft_examples_matched']} примеров SFT")
        print(f"      {e['prompt_preview']}")

    if expect is not None and not info["expect_matched"]:
        # Пул ревизии не собирается: набор исключённых задач определяет ось
        # сравнения RL vs SFT (AD-1), и молча принять изменившийся набор нельзя.
        print()
        print(f"РАСХОЖДЕНИЕ: ADR-007 фиксирует {expect}, замер даёт "
              f"{info['excluded_count']} — сигнал: критерий или пул изменились. "
              f"Пул ревизии в этом состоянии не собирается (--no-expect — только "
              f"осознанным решением).")
        return EXIT_REFUSE

    if out_path.is_file() and not args.force:
        same = out_path.read_text(encoding="utf-8") == _pool_text(rl_path, excluded_lines)
        if same:
            print()
            print(f"пул уже актуален, не перезаписываю: {out_path}")
            _write_report(report_path, payload)
            _write_not_a_run(out_path.parent)
            print(f"отчёт об исключениях: {report_path}")
            return EXIT_OK
        print()
        print(f"ОТКАЗ: {out_path} существует и отличается от нового — не перезатираю; "
              f"сверь пул или передай --force", file=sys.stderr)
        return EXIT_REFUSE

    res = write_pool(rl_path, out_path, excluded_lines)
    payload["pool_sha256"] = res["sha256"]
    payload["pool_lines"] = res["written_lines"]
    _write_report(report_path, payload)
    _write_not_a_run(out_path.parent)

    print()
    print(f"пул ревизии: {out_path} — строк {res['written_lines']}, "
          f"sha256 {res['sha256'][:12]}…")
    print(f"отчёт об исключениях: {report_path}")
    print(f"маркер: {out_path.parent / NOT_A_RUN_MARKER} (каталог не является прогоном)")
    print()
    print(f"BUILD_REV_POOL OK: {info['retained_count']} задач в пуле ревизии, "
          f"{info['excluded_count']} исключено, пересечение с SFT — 0")
    return EXIT_OK


def _pool_text(rl_path: Path, excluded_lines: set[int]) -> str:
    """Текст пула, который получился бы сейчас — база проверки идемпотентности."""
    parts = []
    with rl_path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if not line.strip() or i in excluded_lines:
                continue
            parts.append(line)
    text = "".join(parts)
    if parts and not text.endswith("\n"):
        text += "\n"
    return text


def _write_report(report_path: Path, payload: dict) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(report_path)


def _write_not_a_run(directory: Path) -> None:
    """Маркер C-012: каталог пула — не каталог прогона (манифест AD-2 был бы фикцией)."""
    marker = directory / NOT_A_RUN_MARKER
    marker.write_text(
        "Каталог пула ревизии, не прогон: здесь лежит отобранный пул данных\n"
        "(rl_pool_filtered.jsonl, ADR-007 п.1), а не результаты стадии.\n"
        "Манифест AD-2 (run_manifest.json) к нему неприменим — прогона нет.\n"
        "Читается стражем C-012 (tools/check_run_manifest.py).\n",
        encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
