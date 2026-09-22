#!/usr/bin/env python3
"""C-014 — отчёт точки решения: фаза, в которой правило молчит, и полнота по схеме.

**Класс дефекта, ради которого инструмент заведён.** Правило C-014 было
`each_file_must_contain` по glob `evidence/result-report*.json`. Пустой набор файлов
по glob — находка движка (`docs/control.md`), а отчёт точки решения S4 создаётся
**только по факту** замера: `S4-PROTOCOL.md` §5 прямо запрещает писать его заранее
(«иначе правило C-014 станет ложно зелёным»). Значит до точки S4 правило было
красным **по построению** — не потому, что что-то нарушено, а потому что фаза не
наступила. Класс разобран трижды (ADR-023 п.12); лечится он **фазой**, а не
ослаблением смысла: у правила, знающего фазу, три состояния, и красное — только у
двух из них.

**Три состояния (и только они дают вердикт).**

1. `PENDING-EVIDENCE` — отчёта нет, и **точка решения не пройдена**: признаков
   исполненной RL-стадии в контуре нет. Это фаза, а не долг: вердикт `warn` с
   названной причиной, гейт не краснеет (`rc=0` в режиме `guard`; сам PENDING —
   предмет режима `pending`).
2. `MISSING-REPORT` — отчёта нет, а **точка пройдена**: в контуре есть RL-чекпойнт
   или журнал RL-метрик, то есть замер, по которому отчёт обязан быть собран. Это
   уже долг, а не фаза: вердикт `error`, гейт краснеет.
3. `INCOMPLETE` / `COMPLETE` — отчёт есть. Полнота проверяется **конъюнкцией**
   обязательных полей схемы `docs/specs/result-report.schema.json`: неполный отчёт
   краснеет **поимённо** (какие именно поля не выведены), полный — зелёный.

**Почему конъюнкция, а не `pattern`.** Прежнее правило проверяло
`"axis"|"judge_mean"|…` — дизъюнкцию подстрок: отчёт с одним словом «axis» и ничем
более был для него полным. Обязательные поля берутся **из схемы** рекурсивно
(каждый `required` на каждом уровне, вложенность — через точку: `axis.metric`,
`rl_health.entropy`), и каждое проверяется **наличием**; значения-константы схемы
(`axis.metric == "judge_mean"`) проверяются по `const`. Список полей поэтому нельзя
расширить или сузить правкой прибора: он и есть схема.

**Смысл правила сохранён.** Ось (`axis.metric` = `judge_mean`, `axis.rl/sft/delta/
exceeds_noise`), статистика по сидам (`std`, `k_of_k`, `single_seed`), число попыток
(`n_attempts`), анти-метрики дегенерации (`rl_health.entropy`, `clip_frac`,
`zero_reward_share`, `hit_timeout`) и вердикт (`verdict`) — всё это обязательные поля
схемы. Номер правила не менялся, имя и severity — тоже (ADR-046 п.8).

**Границы.** Прибор читает отчёт, а не судит решение: содержательная верность чисел
(направление оси, различие с шумом) — предмет `S4-PROTOCOL.md` §3 и человека.
Замера он не делает и отчёт не создаёт: созданный заранее отчёт был бы ложной
зелёнкой (ADR-023 п.12, §5 протокола).

Коды возврата::

    0 — зелёное для выбранного режима: отчёт есть и полон; либо отчёта нет и точка
        не пройдена (режим guard — фаза называется в выводе, гейт не краснеет)
    1 — красное: отчёта нет, а точка пройдена; отчёт есть, но неполон (поля
        перечислены поимённо); в режиме `pending` — само состояние PENDING
    2 — NOT-VERIFIED: нет входа (каталог кейса не читается)

Запуск::

    python3 tools/check_result_report.py                 # правило C-014 (guard)
    python3 tools/check_result_report.py --mode pending  # предмет режима — PENDING
    python3 tools/check_result_report.py --json          # машинный вердикт
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Каноническое имя отчёта: `S4-PROTOCOL.md` §5 и описание схемы называют один и тот
#: же файл. Дефисного варианта (`result-report-*.json`) не бывает — именно на нём
#: прежнее правило и промахнулось (S3bd).
REPORT_REL = "evidence/result-report.json"
SCHEMA_REL = "docs/specs/result-report.schema.json"

#: Признаки ПРОЙДЕННОЙ точки решения: артефакты исполненной стадии RL — те самые,
#: что `S4-PROTOCOL.md` §1/§6.1 называет входами отчёта. Найденный называется
#: поимённо: «точка пройдена» без имени файла было бы таким же молчанием, как
#: зелёный без отчёта.
#:
#: **Чего здесь нет и почему.** Проба цены шага (`tools/run_rl_probe.py`) пишет
#: `rl_metrics.json` **в корень** каталога прогона (копия из контейнера, ADR-010) —
#: это другой предмет: разведка цены, у которой «сходимость не критерий» (ADR-010
#: п.3), и точку решения S4 она не проходит. Признак в корне поэтому не берётся, а
#: берётся в `logs/` — там его пишет **сама стадия** (`Path(args.log_dir)/`
#: "rl_metrics.json", `laguna_pipeline_v8.py`). Иначе `runs/rl-probe-*/` от 14.09.2026
#: (проба, а не стадия) держала бы правило красным **по построению** — ровно тот
#: класс, который это правило и чинит.
S4_SIGN_GLOBS = (
    ("*/checkpoints/rl_checkpoint_final.pt", "RL-чекпойнт стадии (вход замера §6.1)"),
    ("*/logs/rl_metrics.json", "журнал RL-метрик стадии"),
    ("*/logs/eval_results.json", "eval-артефакт после RL (S4-PROTOCOL §1)"),
)


def required_paths(schema: dict, node: dict | None = None, prefix: str = "",
                   defs: dict | None = None) -> list[str]:
    """Обязательные поля схемы, рекурсивно: `a.b.c` на каждый `required`.

    Берутся из схемы, а не из списка в коде: список в коде разошёлся бы со схемой
    на первой правке формата отчёта, и правило проверяло бы вчерашний отчёт.
    """
    node = schema if node is None else node
    defs = (schema.get("$defs") or {}) if defs is None else defs
    if "$ref" in node:
        ref = str(node["$ref"]).split("/")[-1]
        return required_paths(schema, defs.get(ref, {}), prefix, defs)
    out: list[str] = []
    for name in node.get("required", []) or []:
        path = f"{prefix}{name}"
        out.append(path)
        child = (node.get("properties") or {}).get(name) or {}
        out.extend(required_paths(schema, child, f"{path}.", defs))
    return out


def const_paths(schema: dict, node: dict | None = None, prefix: str = "",
                defs: dict | None = None) -> list[tuple[str, object]]:
    """Поля схемы с зафиксированным значением (`const`) — их значения тоже обязаны сойтись.

    Ради `axis.metric == "judge_mean"`: отчёт, чья ось названа другой метрикой, —
    отчёт о другой оси, и «поле есть» здесь не то же самое, что «ось та».
    """
    node = schema if node is None else node
    defs = (schema.get("$defs") or {}) if defs is None else defs
    if "$ref" in node:
        ref = str(node["$ref"]).split("/")[-1]
        return const_paths(schema, defs.get(ref, {}), prefix, defs)
    out: list[tuple[str, object]] = []
    if "const" in node and prefix:
        out.append((prefix.rstrip("."), node["const"]))
    for name, child in (node.get("properties") or {}).items():
        out.extend(const_paths(schema, child, f"{prefix}{name}.", defs))
    return out


def dig(obj: object, path: str) -> tuple[bool, object]:
    """Значение по пути `a.b.c`; первый элемент — найдено ли поле."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def s4_signs(root: Path) -> list[str]:
    """Признаки пройденной точки решения, поимённо: «<какой файл> — <что это>»."""
    runs = root / "runs"
    found: list[str] = []
    if not runs.is_dir():
        return found
    for pattern, what in S4_SIGN_GLOBS:
        for p in sorted(runs.glob(pattern)):
            if p.is_file():
                found.append(f"{p.relative_to(root)} — {what}")
    return found


def verdict(root: Path, schema: dict) -> dict:
    """Вердикт по трём состояниям. `severity` — уровень находки, `rc` — код выхода."""
    report = root / REPORT_REL
    if report.is_file():
        try:
            doc = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"state": "UNREADABLE", "severity": "error", "rc": EXIT_FAIL,
                    "why": f"{REPORT_REL} есть, но не разбирается как JSON: {e}",
                    "missing": [], "signs": []}
        if not isinstance(doc, dict):
            return {"state": "UNREADABLE", "severity": "error", "rc": EXIT_FAIL,
                    "why": f"{REPORT_REL}: ожидается объект JSON, получено "
                           f"{type(doc).__name__}", "missing": [], "signs": []}
        missing = [p for p in required_paths(schema) if not dig(doc, p)[0]]
        bad_const = []
        for path, want in const_paths(schema):
            ok, got = dig(doc, path)
            if ok and got != want:
                bad_const.append(f"{path}={got!r}, схема требует {want!r}")
        if missing or bad_const:
            return {"state": "INCOMPLETE", "severity": "error", "rc": EXIT_FAIL,
                    "why": f"{REPORT_REL}: не выведено полей {len(missing)}; правилу "
                           f"C-014 нечего проверять конъюнкцией — отчёт полным не является",
                    "missing": missing, "const_mismatch": bad_const, "signs": []}
        return {"state": "COMPLETE", "severity": "ok", "rc": EXIT_OK,
                "why": f"{REPORT_REL}: обязательные поля схемы выведены "
                       f"({len(required_paths(schema))}), значения-константы сошлись",
                "missing": [], "signs": []}

    signs = s4_signs(root)
    if signs:
        return {"state": "MISSING-REPORT", "severity": "error", "rc": EXIT_FAIL,
                "why": f"отчёта {REPORT_REL} нет, а точка решения ПРОЙДЕНА: "
                       f"{'; '.join(signs)} — по этому замеру отчёт обязан быть собран "
                       f"(это долг, а не фаза)",
                "missing": [], "signs": signs}
    return {"state": "PENDING-EVIDENCE", "severity": "warn", "rc": EXIT_OK,
            "why": f"отчёта {REPORT_REL} нет, потому что точка решения не пройдена: "
                   f"признаков исполненной RL-стадии в runs/ нет. Создавать отчёт "
                   f"заранее запрещено (S4-PROTOCOL §5) — правило ждёт фазу, а не "
                   f"нарушение",
            "missing": [], "signs": []}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Отчёт точки решения: фаза + полнота по схеме (C-014)")
    ap.add_argument("--root", default=".", help="корень кейса")
    ap.add_argument("--mode", choices=("guard", "pending"), default="guard",
                    help="guard (правило C-014): красное на долг и неполноту, зелёное "
                         "на PENDING; pending: предмет режима — сам PENDING")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    root = Path(a.root)
    if not root.is_dir():
        print(f"NOT-VERIFIED: {root} не каталог", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    schema_path = root / SCHEMA_REL
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"NOT-VERIFIED: схема отчёта не прочитана ({schema_path}): {e}",
              file=sys.stderr)
        return EXIT_NOT_VERIFIED

    v = verdict(root, schema)
    if a.mode == "pending":
        # Режим PENDING: его предмет — само состояние «отчёта нет, потому что фаза не
        # наступила». Долг и неполнота — не его предмет (за них отвечает guard-режим
        # того же прибора), поэтому здесь они зелёные.
        rc = EXIT_FAIL if v["state"] == "PENDING-EVIDENCE" else EXIT_OK
        msg = (f"{v['why']} (режим pending: фаза названа, красное здесь было бы ложным)"
               if rc == EXIT_FAIL else
               f"{v['state']}: состояние PENDING снято — предмет режима pending не наступил")
        out = {"mode": a.mode, "rc": rc, "state": v["state"],
               "severity": "warn" if rc == EXIT_FAIL else "ok", "message": msg}
    else:
        rc = v["rc"]
        out = {"mode": a.mode, "rc": rc, "state": v["state"], "severity": v["severity"],
               "finding_severity": v["severity"] if rc == EXIT_FAIL else None,
               "missing": v.get("missing") or [],
               "const_mismatch": v.get("const_mismatch") or [],
               "signs": v["signs"], "message": v["why"],
               "schema": SCHEMA_REL, "report": REPORT_REL}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(out["message"])
        if out.get("missing"):
            print(f"  не выведено поимённо: {', '.join(out['missing'])}")
        if out.get("const_mismatch"):
            for m in out["const_mismatch"]:
                print(f"  расхождение с константой схемы: {m}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
