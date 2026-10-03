#!/usr/bin/env python3
"""S3ah — сторож терминологии как **приёмка учителя**, а не способ выбора между учителями.

Роль сторожа изменилась вместе с решением владельца (ADR-038, обновление
17.09.2026): учитель один — `qwen3.8-27b`, выбирать больше не между кем. Но
принимать его вслепую нельзя: ровно тот класс дефектов, на котором отпал
локальный 8B (неверный технический референт), числа языка и длины **не видят** —
у «автосуммирующей генерации» доля кириллицы отличная. Сторож нужен, чтобы
эталонный учитель не искажал доменные термины.

**Что сторож ловит механически.** Идентификаторы и обозначения, которые обязаны
дойти без изменений: slug-и концептов (`mr_molar_refractivity`), ЛАТИНСКИЕ
аббревиатуры (GRPO, PPO, KL, SFT, PPL, GAE), camelCase-имена, содержимое
``code-span``. Термин источника, которого нет в переводе, — расхождение; термин
перевода, которого нет в источнике, — выдуманный идентификатор. Оба класса
считаются, а не описываются словами.

**Чего сторож НЕ ловит — и это сказано вслух.** Он не видит смысловой подмены
обычных технических слов: «авторегрессионная» → «автосуммирующая»,
«slug-и» → «слоги» — идентификаторов там нет, и регулярка молчит. Именно этот
класс поймало **чтение** (S3ag), и именно он развёл учителей. Поэтому вердикт
сторожа всегда идёт рядом с границей: сеть на регресс идентификаторов, не замена
чтению.

Режимы::

    check  — прогнать проверку по фрагментам пилота и записать JSON
    report — то же, но с явным вердиктом и списком расхождений для чтения

Коды возврата::

    0 — проверка выполнена (расхождений может быть сколько угодно: это замер)
    2 — NOT-VERIFIED: нет входа (пилот, источник, список фрагментов)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from russian_think_pilot import (  # noqa: E402
    load_source_records, segment_assistant, sha256_text,
)

CASE_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_NOT_VERIFIED = 0, 2

GB10_SHARED = Path("/home/user/gb10-shared")
DEFAULT_SFT = GB10_SHARED / "datasets/sft_train_v12.jsonl"

#: Slug концепта: минимум одно подчёркивание, только строчные латинские.
RE_SLUG = re.compile(r"(?<![\w`])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w])")
#: ЛАТИНСКАЯ аббревиатура: GRPO, PPO, KL, SFT, PPL, GAE, RLHF.
RE_ABBR = re.compile(r"(?<![\w])[A-Z][A-Z0-9]{1,7}(?![\w])")
#: camelCase / PascalCase имена функций и полей.
RE_CAMEL = re.compile(r"(?<![\w])[a-z]+(?:[A-Z][a-z0-9]*)+(?![\w])")
#: Содержимое code-span: до 60 символов без перевода строки.
RE_CODE = re.compile(r"`([^`\n]{1,60})`")

#: Английские служебные слова, случайно набранные капсом. Без этого списка
#: «IT», «IS», «OK» попадали бы в термины и раздували расхождения шумом.
NOISE = {"OK", "IT", "IS", "IF", "THE", "AND", "OR", "AS", "AT", "IN", "ON", "TO",
         "BY", "OF", "NO", "SO", "DO", "BE", "WE", "HE", "AN", "AM", "PM", "VS",
         "ETC", "EG", "IE", "TBD", "TODO", "NULL", "NONE", "TRUE", "FALSE"}


def note(msg: str) -> None:
    print(msg, file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def filter_emphasis(text: str, abbrs: list) -> tuple:
    """Отделить аббревиатуры от слов, набранных капсом для эмфазы.

    Заглавное NOT в «you should NOT call the tool» — не обозначение, а ударение;
    в переводе оно законно становится строчным «не», и сторож, считающий его
    термином, даёт ложное расхождение (поймано на строке 864 пилота).
    Признак механический и не требует словаря: если то же слово встречается в
    ТОМ ЖЕ фрагменте строчными буквами — это то же слово, а не идентификатор.
    """
    lower_words = set(re.findall(r"\b[a-z]{2,}\b", text or ""))
    keep, dropped = [], []
    for a in abbrs:
        (dropped if a.lower() in lower_words else keep).append(a)
    return sorted(set(keep)), sorted(set(dropped))


def extract_terms(text: str, filter_text: str = "") -> dict:
    """Термины, обязанные дойти без изменений, по классам."""
    code = [c.strip() for c in RE_CODE.findall(text or "")]
    code = [c for c in code if re.search(r"[A-Za-z]", c)]
    abbr, emphasis = filter_emphasis(filter_text or text, RE_ABBR.findall(text or ""))
    return {
        "slugs": sorted(set(RE_SLUG.findall(text or ""))),
        "abbr": [a for a in abbr if a not in NOISE],
        "camel": sorted(set(RE_CAMEL.findall(text or ""))),
        "code": sorted(set(code)),
        "emphasis_filtered": [a for a in emphasis if a not in NOISE],
    }


def all_terms(terms: dict) -> list:
    return [t for k in ("slugs", "abbr", "camel", "code") for t in terms[k]]


def is_absent(term: str, translated: str) -> bool:
    """Термин отсутствует в переводе буквально.

    Проверка нарочно буквальная: учителю велено оставлять идентификаторы как
    есть, поэтому «нет строки» — расхождение, а не «может, перефразировал».
    """
    return term not in (translated or "")


def pair_fragments(src_rec: dict, out_rec: dict, chars_in: int) -> tuple | None:
    """Найти фрагмент источника и его перевод по длине сегмента.

    Пилот S3ag адресует прочитанные фрагменты полем `chars_in` (длина сегмента
    источника). Ищем сегмент ровно этой длины — адресация по номеру сегмента
    зависела бы от того, как прибор нумерует «other»-сегменты.
    """
    src_asst = [m for m in src_rec["messages"] if m.get("role") == "assistant"]
    out_asst = [m for m in out_rec["messages"] if m.get("role") == "assistant"]
    if not src_asst or not out_asst:
        return None
    s_segs = segment_assistant(src_asst[0]["content"])
    o_segs = segment_assistant(out_asst[0]["content"])
    for i, (kind, text) in enumerate(s_segs):
        if kind != "think":
            continue
        # `chars_in` пилота — длина сегмента как он лежит в наборе; допуск в
        # несколько символов покрывает краевые пробелы, но не «похожесть».
        if abs(len(text) - chars_in) > 4:
            continue
        if i >= len(o_segs) or o_segs[i][0] != "think":
            continue
        return text, o_segs[i][1]
    return None


def check_pair(source: str, translated: str) -> dict:
    src_terms = extract_terms(source)
    # Фильтр эмфазы у перевода берётся из ИСТОЧНИКА: слово, законно ставшее
    # строчным в русском тексте, не должно выглядеть выдуманной аббревиатурой.
    out_terms = extract_terms(translated, filter_text=source)
    src_all, out_all = all_terms(src_terms), all_terms(out_terms)
    missing = [t for t in src_all if is_absent(t, translated)]
    invented = [t for t in out_all if t not in (source or "")]
    by_class = {}
    for cls in ("slugs", "abbr", "camel", "code"):
        by_class[cls] = {
            "n_source": len(src_terms[cls]),
            "missing": [t for t in src_terms[cls] if is_absent(t, translated)],
            "invented": [t for t in out_terms[cls] if t not in (source or "")],
        }
    return {
        "terms_source": len(src_all),
        "missing": missing,
        "invented": invented,
        "emphasis_filtered": src_terms["emphasis_filtered"],
        "by_class": by_class,
        "ok": not missing and not invented,
    }


def check_pilot(sft: Path, pilot: Path, items: list, label: str) -> tuple:
    """Прогнать сторожа по пилоту одного учителя. Возвращает (результаты, сводка)."""
    linenos = sorted({it["source_line"] for it in items})
    recs = {r["lineno"]: json.loads(r["line"])
            for r in load_source_records(sft, 0, linenos=linenos)}
    with pilot.open(encoding="utf-8") as f:
        pilot_recs = {json.loads(l)["s3af"]["source_line"]: json.loads(l)
                      for l in f if l.strip()}

    results = []
    for it in items:
        line = it["source_line"]
        src_rec, out_rec = recs.get(line), pilot_recs.get(line)
        if not src_rec or not out_rec:
            results.append({"source_line": line, "error": "нет записи в источнике или пилоте"})
            continue
        pair = pair_fragments(src_rec, out_rec, it["chars_in"])
        if not pair:
            results.append({"source_line": line, "chars_in": it["chars_in"],
                            "error": "фрагмент не найден по длине сегмента"})
            continue
        source, translated = pair
        row = check_pair(source, translated)
        row.update({
            "source_line": line,
            "chars_in": len(source),
            "read_verdict_s3ag": it.get("verdict"),
            "read_why_s3ag": it.get("why"),
            "sha_source": sha256_text(source)[:16],
            "sha_translated": sha256_text(translated)[:16],
        })
        results.append(row)
        note(f"[{label}] строка {line}: терминов {row['terms_source']}, "
             f"отсутствуют {len(row['missing'])}, выдуманы {len(row['invented'])}")

    ok_rows = [r for r in results if "error" not in r]
    summary = {
        "n_fragments": len(ok_rows),
        "n_errors": len(results) - len(ok_rows),
        "terms_source_total": sum(r["terms_source"] for r in ok_rows),
        "missing_total": sum(len(r["missing"]) for r in ok_rows),
        "invented_total": sum(len(r["invented"]) for r in ok_rows),
        "fragments_with_divergence": sum(1 for r in ok_rows if not r["ok"]),
        "divergences": [{"source_line": r["source_line"], "missing": r["missing"],
                         "invented": r["invented"]} for r in ok_rows if not r["ok"]],
        "read_verdicts_s3ag": {},
    }
    for r in ok_rows:
        v = r["read_verdict_s3ag"]
        summary["read_verdicts_s3ag"][v] = summary["read_verdicts_s3ag"].get(v, 0) + 1
    return results, summary


def do_check(args) -> int:
    sft = Path(args.sft_jsonl)
    pilot = Path(args.pilot)
    if not sft.exists() or not pilot.exists():
        note(f"NOT-VERIFIED: нет входа (sft={sft.exists()}, pilot={pilot.exists()})")
        return EXIT_NOT_VERIFIED

    items = []
    teacher = None
    if args.s3ag and Path(args.s3ag).exists():
        s3ag = json.loads(Path(args.s3ag).read_text(encoding="utf-8"))
        teacher = args.teacher or s3ag["price"]["compared_teacher"]
        items = [it for it in s3ag["spotcheck"]["items"] if it["teacher"] == teacher]
    if not items:
        note("NOT-VERIFIED: в отчёте S3ag нет прочитанных фрагментов для этого учителя")
        return EXIT_NOT_VERIFIED

    results, summary = check_pilot(sft, pilot, items, teacher or "учитель")

    # Контроль: тот же сторож на пилоте учителя, у которого ЧТЕНИЕ нашло
    # смысловые искажения. Если сторож молчит и там, он меряет не тот класс —
    # и это доказывается, а не декларируется. Учитель-контроль ничего не
    # генерирует: читается уже снятый пилот.
    control = None
    if args.control_pilot and Path(args.control_pilot).exists():
        c_teacher = args.control_teacher or "control"
        c_items = [it for it in s3ag.get("spotcheck", {}).get("items", [])
                   if it["teacher"] == c_teacher]
        if c_items:
            c_res, c_sum = check_pilot(sft, Path(args.control_pilot), c_items, c_teacher)
            control = {"teacher": c_teacher, "pilot": args.control_pilot,
                       "fragments": c_res, "summary": c_sum}
        else:
            control = {"teacher": c_teacher,
                       "error": "в S3ag нет прочитанных фрагментов для учителя-контроля"}

    doc = {
        "schema": "s3ah-terminology-27b/1",
        "stage": "S3ah",
        "date": now_iso(),
        "status": "complete",
        "purpose": ("приёмка эталонного учителя 27B сторожем терминологии: есть ли у него "
                    "искажения доменных терминов, которых не заметило чтение S3ag"),
        "role": ("сторож нужен как ПРИЁМКА 27B (учитель один по ADR-038), а не как способ "
                 "выбора между учителями — выбирать больше не между кем"),
        "teacher": teacher,
        "scope": ("те же фрагменты, что читались в S3ag (5 на учителя); адресация — "
                  "полем chars_in, а не номером сегмента"),
        "method": ("буквальная проверка присутствия терминов источника в переводе: slug-и, "
                   "латинские аббревиатуры, camelCase, содержимое code-span; плюс обратная "
                   "проверка — термины перевода, которых нет в источнике (выдуманные)"),
        "covers": ["slug-и концептов", "латинские аббревиатуры (GRPO/PPO/KL/SFT/PPL)",
                   "camelCase-имена", "содержимое code-span"],
        "does_not_cover": [
            "смысловую подмену обычных технических слов (авторегрессионная → автосуммирующая, "
            "slug-и → слоги): идентификаторов там нет, регулярка молчит — этот класс ловит "
            "только чтение",
            "сокращение или упрощение рассуждения при целых терминах",
        ],
        "noise_stoplist": sorted(NOISE),
        "fragments": results,
        "summary": summary,
        "control": control,
    }
    doc["verdict"] = None
    if args.verdict and Path(args.verdict).exists():
        doc["verdict"] = json.loads(Path(args.verdict).read_text(encoding="utf-8"))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        note(f"записано: {args.out}")
    else:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="S3ah: сторож терминологии как приёмка учителя")
    ap.add_argument("--sft-jsonl", default=str(DEFAULT_SFT))
    ap.add_argument("--pilot", default="runs/s3af-27b-pilot-20260917-1026/pilot.jsonl")
    ap.add_argument("--s3ag", default="evidence/s3ag-teacher-comparison.json")
    ap.add_argument("--teacher", default="")
    ap.add_argument("--control-pilot", default="",
                    help="пилот второго учителя — контроль сторожа, а не выбор учителя")
    ap.add_argument("--control-teacher", default="",
                    help="имя учителя-контроля в отчёте S3ag (например qwen3:8b)")
    ap.add_argument("--out", default="")
    ap.add_argument("--verdict", default="")
    args = ap.parse_args()
    return do_check(args)


if __name__ == "__main__":
    sys.exit(main())
