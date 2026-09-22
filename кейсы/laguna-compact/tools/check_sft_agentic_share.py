#!/usr/bin/env python3
"""C-022 (предложено S3ab) — страж agentic-доли SFT-стадии (критерий ADR-033 п.2).

Дыра, которую правило закрывает (G4 плана стадии). ADR-033 п.2 сделал (в)
третьим компонентом критерия стадии — **agentic-поведение**, и назвал его
двумя условиями. Но у критерия не было прибора-стража: долю самостоятельных
вызовов инструмента мерили руками один раз (hr-68), и ничто не мешало сдать
стадию, не измерив её вовсе либо измерив несопоставимо.

**Исправленная редакция (ADR-033 п.2, вставка 17.09.2026).** Посылка «на
CPT-чекпойнте самостоятельных вызовов ноль» опровергнута замером: на входном
чекпойнте SFT самостоятельный вызов делают **158 из 350 траекторий = 45.1 %**
(v2 — 72/200 = 36.0 %, v1 — 86/150 = 57.3 %), без форсирования. Точка отсчёта —
**45.1 %**, а не ноль; критерий читается как два условия:

* **(в1)** доля самостоятельных вызовов **не падает** относительно 45.1 %
  (регрессия агентного поведения запрещена);
* **(в2)** **pass-rate на том же наборе растёт** относительно базы
  (поведение становится результативнее, а не только сохраняется).

**Что «самостоятельный вызов» значит здесь.** Только отчёт, снятый прибором
``tools/passrate_probe.py`` в конфигурации ``--no-toolcall-force --no-hint``:
при ``toolcall-force`` первый вызов вкладывает сам харнесс, при ``hint``
подставляется открывающий тег ``<tool_call>``, и модель лишь дописывает JSON.
В этих двух конфигурациях число описывает харнесс, а не модель — на смене флагов
и споткнулась посылка ADR-033. Страж обязан отказать на несопоставимом отчёте:
это **отказ** (exit 1), а не «нет данных».

**Почему сравниваются подвыборки, а не только сводка.** База снята на двух
пулах; если после стадии измерен только один, сводные доли несопоставимы по
построению (разный состав задач) — тогда сравнение идёт **по пулам**,
у которых база есть, а сводное помечается несопоставимым. Молчаливое сравнение
сводок разных подвыборок — тот же класс ошибки, что «поверили в коллинеарное
сравнение» (ADR-026 п.2).

База берётся из замороженной цитаты ADR-033 п.2; если задан ``--base-report``,
она **повторно выводится из артефакта** и сверяется с цитатой (расхождение —
отказ). Иначе «база» поехала бы вместе с прибором, и критерий проверял бы сам себя.

Коды возврата::

    0 — PASS: критерий (в) в исправленной редакции выполнен (все сравнимые
        единицы прошли и хотя бы одна сравнимая единица была)
    1 — FAIL: регрессия доли, падение/отсутствие роста pass-rate либо
        несопоставимый отчёт (формат отчёта, флаги прибора, подмена пула)
    2 — NOT-VERIFIED: нет данных — отчёт отсутствует/нечитаем, в нём нет
        ни одного пула, или нет ни одной сравнимой с базой единицы

Запуск::

    python3 tools/check_sft_agentic_share.py \\
        --report evidence/s3aa-agentic-sft.json \\
        --base-report evidence/s3aa-agentic-cpt.json \\
        --expect-pool-sha256 "v2=e678eb680d5abba0825f81d3e84cda3f680a51850f683fa6f1a3aa22de91d4da,v1=06b95b2f3b62d2f8b24a458ca49ff13232b9fc7a00c5390e4354d6b2956a5eb3"

    # агрегаты из сырых записей прибора (без GPU), затем страж по ним:
    python3 tools/passrate_probe.py --summarize runs/passrate-agentic-sft-*/v2/tasks.jsonl \\
        --evidence /tmp/after.json

JSON-контракт (``--json``): ``{"verdict": "PASS|FAIL|NOT-VERIFIED", "checks": [...],
"report": {...}, "base": {...}, "reason", "inconclusive", "tolerance_pp"}``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

CASE_ROOT = Path(__file__).resolve().parent.parent

#: Замороженная база ADR-033 п.2 (вставка 17.09.2026 по замеру hr-68): артефакт
#: `evidence/s3aa-agentic-cpt.json`, прогон `runs/passrate-agentic-cpt-20260917-0153/`,
#: прибор `tools/passrate_probe.py --no-toolcall-force --no-hint`. Числа — цитата
#: решения, а не вычисление: страж сверяет с ними базу, выведенную из артефакта.
BASE_FROZEN = {
    "source": "ADR-033 п.2 (замер hr-68), evidence/s3aa-agentic-cpt.json",
    "pools": {
        "v2": {"n": 200, "n_tool_call": 72, "share": 0.36, "n_pass": 11, "pass_rate": 0.055},
        "v1": {"n": 150, "n_tool_call": 86, "share": 0.5733, "n_pass": 42, "pass_rate": 0.28},
    },
    "combined": {"n": 350, "n_tool_call": 158, "share": 0.4514,
                 "n_pass": 53, "pass_rate": 0.1514},
}

#: Конфигурация прибора, в которой вызов инструмента самостоятелен (``--no-hint``).
#: Всё остальное — отказ: число описывает харнесс, а не модель.
#:
#: Две формы одной и той же строки: операционно прибор подставляет реальный
#: перевод строки (``passrate_probe.py:669``), а в отчёт пишет **экранированную**
#: запись ``"<think>\\n"`` (``passrate_probe.py:1769``) — 9 символов, где последние
#: два это обратный слэш и ``n``. Проверены обе: сравнивать с одной и отвергать
#: вторую значило бы отказывать на верном отчёте из-за формы записи.
REQUIRED_PREFILL = ("<think>\\n", "<think>\n")

#: Допуск на расхождение доли, выведенной из артефакта базы, с цитатой ADR.
BASE_REDERIVE_TOL = 0.01

#: Сколько знаков печатать в долях.
ND = 4


def note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def n_from_share(n: int, share: float) -> int:
    """Счёт вызовов из доли отчёта.

    Прибор пишет долю, округлённую до 4 знаков, счёта не пишет; обратный переход
    даёт ±0.5 задачи. Ошибка меньше шага решения (шаг доли базы при n=350 —
    0.29 %), но она названа, а не спрятана.
    """
    return int(round(n * share))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class NotVerified(Exception):
    """Вход непригоден для суждения (нет данных)."""


class Refuse(Exception):
    """Вход пригоден, но несопоставим с базой (отказ, не «нет данных»)."""


def check_protocol(proto: dict | None, label: str, where: str) -> tuple:
    """Конфигурация прибора: от неё зависит смысл числа.

    Возвращает ключ сравнимости ``(toolcall_force, prefill, checkpoint_sha256)``.
    """
    if not proto:
        raise Refuse(f"{label}: в отчёте нет блока 'protocol' ({where}) — конфигурацию "
                     f"прибора (toolcall_force/hint) проверить нечем, а от неё зависит "
                     f"смысл числа (ADR-033 п.2, вставка 17.09.2026)")
    if proto.get("toolcall_force"):
        raise Refuse(f"{label}: отчёт снят с toolcall_force=true ({where}) — первый вызов "
                     f"вкладывает харнесс, самостоятельность вызова не измерена")
    prefill = proto.get("prefill")
    if prefill not in REQUIRED_PREFILL:
        raise Refuse(f"{label}: prefill={prefill!r} вместо {REQUIRED_PREFILL[0]!r} ({where}) — "
                     f"подсказка открывающего тега (--hint) делает вызов "
                     f"полуфорсированным, число несопоставимо с базой")
    return (bool(proto.get("toolcall_force")), prefill, proto.get("checkpoint_sha256"))


def units_from_report(rep: dict, label: str,
                      expect_sha: dict[str, str]) -> tuple[dict, dict, list[str]]:
    """Сравнимые единицы отчёта прибора: по пулам + сводная.

    Единица — ``{"name", "n", "n_tool_call", "share", "n_pass", "pass_rate"}``.
    """
    pools = rep.get("pools")
    if not isinstance(pools, dict) or not pools:
        raise NotVerified(f"{label}: в отчёте нет блока 'pools' — "
                          f"доли вызовов и pass-rate взять не из чего")

    # Смысл числа зависит от конфигурации прибора, поэтому она проверяется первой —
    # и не только сводная: пересборка из сырых записей (`--summarize`) кладёт
    # протокол в каждый пул, и расхождение между пулами означало бы, что подвыборки
    # сняты по-разному (или на разных весах), а сводка по ним — среднее из разного.
    key = check_protocol(rep.get("protocol"), label, "на уровне отчёта")

    units: dict[str, dict] = {}
    notes: list[str] = []
    for name in sorted(pools):
        p = pools[name] or {}
        ov = p.get("overall") or {}
        if not ov.get("n"):
            notes.append(f"{label}: пул '{name}' без блока overall/n — пропущен")
            continue
        sha = p.get("sha256")
        if name in expect_sha and sha != expect_sha[name]:
            raise Refuse(f"{label}: пул '{name}' имеет sha256 {sha}, ожидался "
                         f"{expect_sha[name]} — измерен другой пул, числа несопоставимы")
        if isinstance(p.get("protocol"), dict):
            pkey = check_protocol(p["protocol"], label, f"пул '{name}'")
            if pkey[:2] != key[:2]:
                raise Refuse(f"{label}: пул '{name}' снят в другой конфигурации прибора "
                             f"(toolcall_force={pkey[0]}, prefill={pkey[1]!r}) против "
                             f"остального отчёта (toolcall_force={key[0]}, prefill={key[1]!r}) — "
                             f"это разные измерения, а не подвыборки одного")
            if pkey[2] and key[2] and pkey[2] != key[2]:
                raise Refuse(f"{label}: пул '{name}' снят на чекпойнте {pkey[2]}, а отчёт — "
                             f"на {key[2]} — сравнение моделей вместо сравнения подвыборок "
                             f"(ошибка постановки, ADR-006/S3j-2)")
        share = ov.get("tool_call_share")
        if share is None:
            notes.append(f"{label}: пул '{name}' без tool_call_share — пропущен")
            continue
        units[name] = {
            "name": name, "n": ov["n"], "n_tool_call": n_from_share(ov["n"], share),
            "share": share, "n_pass": ov.get("n_pass"),
            "pass_rate": ov.get("pass_rate"), "pool_sha256": sha,
        }

    if not units:
        raise NotVerified(f"{label}: ни в одном пуле нет overall с n и tool_call_share")

    #: Сводная единица — арифметика по измеренным пулам. Она сравнима с базой
    #: только при ТОМ ЖЕ составе подвыборок: у другого набора задач своя доля.
    n = sum(u["n"] for u in units.values())
    k = sum(u["n_tool_call"] for u in units.values())
    npass = sum(u["n_pass"] or 0 for u in units.values())
    sample = sorted(units)
    units["combined"] = {
        "name": "combined", "n": n, "n_tool_call": k, "sample": sample,
        "share": round(k / n, ND) if n else None,
        "n_pass": npass,
        "pass_rate": round(npass / n, ND) if n else None,
        "pool_sha256": None,
    }
    meta = {"pools": sample, "n": n,
            "note": "счёт вызовов выведен из доли отчёта (round(n*share), ±0.5 задачи)"}
    return units, meta, notes


def base_units(base_report: str | None) -> tuple[dict, dict, list[str]]:
    """База: замороженная цитата ADR-033 или повторно выведенная из артефакта."""
    frozen = {k: dict(v) for k, v in BASE_FROZEN["pools"].items()}
    frozen["combined"] = dict(BASE_FROZEN["combined"])
    if not base_report:
        return frozen, {"source": BASE_FROZEN["source"], "derived": False}, []

    p = Path(base_report)
    if not p.is_file():
        raise NotVerified(f"базовый отчёт не найден: {p}")
    units, _meta, notes = units_from_report(read_json(p), str(p), {})
    derived, mismatches = {}, []
    for name, fz in frozen.items():
        if name not in units:
            mismatches.append(f"{name}: в артефакте нет единицы")
            continue
        u = units[name]
        derived[name] = {k: u[k] for k in ("n", "n_tool_call", "share", "pass_rate")}
        if u["n"] != fz["n"] or abs(u["share"] - fz["share"]) > BASE_REDERIVE_TOL:
            mismatches.append(
                f"{name}: артефакт даёт {u['n_tool_call']}/{u['n']} = {u['share']}, "
                f"цитата ADR-033 — {fz['n_tool_call']}/{fz['n']} = {fz['share']}")
    if mismatches:
        raise Refuse("база, выведенная из артефакта, разошлась с цитатой ADR-033: "
                     + "; ".join(mismatches))
    return units, {"source": f"{BASE_FROZEN['source']}; выведена из {p}",
                   "derived": True, "checks": derived}, notes


def compare(after: dict, base: dict, *, tolerance_pp: float) -> list[dict]:
    """Проверки (в1) и (в2) для одной сравнимой единицы."""
    tol = tolerance_pp / 100.0
    checks = []
    d_share = after["share"] - base["share"]
    checks.append({
        "rule": "в1: доля самостоятельных вызовов не падает",
        "unit": after["name"],
        "value": after["share"], "threshold": round(base["share"] - tol, ND),
        "delta": round(d_share, ND),
        "passed": bool(after["share"] >= base["share"] - tol),
        "reading": (f"{after['n_tool_call']}/{after['n']} = {after['share']:.4f} "
                    f"против базы {base['n_tool_call']}/{base['n']} = {base['share']:.4f} "
                    f"(Δ {d_share:+.4f}"
                    + (f", допуск {tolerance_pp:g} п.п." if tolerance_pp else "") + ")"),
    })
    if after["pass_rate"] is not None and base.get("pass_rate") is not None:
        dp = after["pass_rate"] - base["pass_rate"]
        checks.append({
            "rule": "в2: pass-rate растёт",
            "unit": after["name"],
            "value": after["pass_rate"], "threshold": base["pass_rate"],
            "delta": round(dp, ND),
            "passed": bool(after["pass_rate"] > base["pass_rate"]),
            "reading": (f"{after['n_pass']}/{after['n']} = {after['pass_rate']:.4f} "
                        f"против базы {base['n_pass']}/{base['n']} = {base['pass_rate']:.4f} "
                        f"(Δ {dp:+.4f})"),
        })
    else:
        checks.append({
            "rule": "в2: pass-rate растёт", "unit": after["name"],
            "value": after["pass_rate"], "threshold": base.get("pass_rate"),
            "delta": None, "passed": False,
            "reading": "pass-rate отсутствует в отчёте или в базе — рост не доказан",
        })
    return checks


def evaluate(args) -> tuple[dict, int]:
    rep_path = Path(args.report)
    if not rep_path.is_file():
        return {"verdict": "NOT-VERIFIED",
                "reason": f"нет отчёта прогона: {rep_path}"}, EXIT_NOT_VERIFIED

    expect_sha = {}
    for item in (args.expect_pool_sha256 or "").split(","):
        if item.strip():
            name, _, sha = item.partition("=")
            expect_sha[name.strip()] = sha.strip()

    try:
        after_units, after_meta, notes = units_from_report(
            read_json(rep_path), str(rep_path), expect_sha)
        base, base_meta, base_notes = base_units(args.base_report)
        notes += base_notes
    except NotVerified as exc:
        return {"verdict": "NOT-VERIFIED", "reason": str(exc)}, EXIT_NOT_VERIFIED
    except Refuse as exc:
        return {"verdict": "FAIL", "reason": str(exc)}, EXIT_FAIL

    # Сравнимые единицы: только те, у которых есть база. Сводная — лишь при
    # совпадении состава подвыборок (иначе у неё другая доля по построению).
    base_pools = sorted(p for p in base if p != "combined")
    after_pools = after_meta["pools"]
    checks: list[dict] = []
    inconclusive: list[str] = []
    for name in after_pools:
        if name not in base:
            inconclusive.append(f"пул '{name}': базы нет — сравнение не делается "
                                f"(база снята на {', '.join(base_pools)})")
            continue
        checks += compare(after_units[name], base[name], tolerance_pp=args.tolerance_pp)
    if after_pools == base_pools:
        checks += compare(after_units["combined"], base["combined"],
                          tolerance_pp=args.tolerance_pp)
    else:
        inconclusive.append(
            "сводная: состав подвыборок другой (после: " + ", ".join(after_pools)
            + "; база: " + ", ".join(base_pools) + ") — сводные доли несопоставимы "
            "по построению, решение принимается по пулам")

    if not checks:
        return {"verdict": "NOT-VERIFIED",
                "reason": "нет ни одной сравнимой с базой единицы",
                "inconclusive": inconclusive}, EXIT_NOT_VERIFIED

    failed = [c for c in checks if not c["passed"]]
    out = {
        "verdict": "PASS" if not failed else "FAIL",
        "criterion": "ADR-033 п.2 (в) в исправленной редакции 17.09.2026: "
                     "база 45.1 % (158/350), (в1) доля не падает, (в2) pass-rate растёт",
        "report": {"path": str(rep_path), "pools": after_pools,
                   "n": after_meta["n"],
                   "combined_share": after_units["combined"]["share"]},
        "base": base_meta,
        "checks": checks,
        "reason": ("|".join(f"[{c['unit']}] {c['rule']}: {c['reading']}"
                            for c in failed) if failed else
                   f"все {len(checks)} проверок выполнены"),
        "inconclusive": inconclusive,
        "tolerance_pp": args.tolerance_pp,
    }
    if notes:
        out["notes"] = notes
    return out, (EXIT_OK if not failed else EXIT_FAIL)


def report_text(res: dict) -> str:
    lines = [f"{res['verdict']}: критерий (в) ADR-033 п.2 (agentic-доля)"
             + (f" — {res['criterion']}" if res.get("criterion") else "")]
    if res.get("report"):
        r = res["report"]
        lines.append(f"  отчёт: {r['path']} | пулы: {', '.join(r['pools'])} | "
                     f"задач: {r['n']} | сводная доля вызовов: {r['combined_share']}")
    if res.get("base"):
        b = res["base"]
        lines.append(f"  база: {b['source']}"
                     + (" (выведена из артефакта и сверена с цитатой)"
                        if b.get("derived") else " (замороженная цитата)"))
    for c in res.get("checks", []):
        lines.append(f"  [{'ok' if c['passed'] else 'FAIL'}] {c['unit']} | {c['rule']}: "
                     f"{c['reading']}")
    for inc in res.get("inconclusive", []):
        lines.append(f"  [--] {inc}")
    lines.append(f"  причина: {res.get('reason', '')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", required=True,
                    help="отчёт прогона: evidence-JSON прибора tools/passrate_probe.py, "
                         "снятый БЕЗ --toolcall-force и БЕЗ --hint")
    ap.add_argument("--base-report", default=None,
                    help="артефакт базового замера (умолчание — цитата ADR-033 п.2: "
                         "evidence/s3aa-agentic-cpt.json). Если задан, база выводится "
                         "из него и сверяется с цитатой; расхождение — отказ")
    ap.add_argument("--tolerance-pp", type=float, default=0.0,
                    help="допуск на падение доли (в1), п.п. По умолчанию 0: ADR-033 "
                         "говорит «не падает». Любое ненулевое значение — отступление "
                         "от критерия, и оно печатается в отчёте")
    ap.add_argument("--expect-pool-sha256", default="",
                    help='ожидаемые хеши пулов, "v2=<sha>,v1=<sha>" — защита от подмены '
                         'набора между базовым замером и замером после стадии')
    ap.add_argument("--json", default=None, help="куда записать машинный результат")
    args = ap.parse_args(argv)

    res, code = evaluate(args)
    print(report_text(res))
    if args.json:
        Path(args.json).write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
