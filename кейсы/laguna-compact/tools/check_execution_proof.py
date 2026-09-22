#!/usr/bin/env python3
"""Страж «Доказательства исполнения»: решение обязано назвать, ЧЕМ доказано,
что оно дошло до потребителя, — а не только что артефакт создан.

**Класс дефекта (S3av, ADR-042 → ADR-051).** Решение ADR-042 было верным и
исполненным: стадия остановлена, дефектные данные исправлены в новый файл, набор
верифицирован, старый сохранён. Не сделано (и не требовалось) другое — **доказать,
что перезапущенная стадия прочла именно исправленный файл**. Проверка стояла на
**артефакте** («файл создан», «манифест есть»), а не на **потребителе** («его
прочитали именно так»). Инвариант AD-2 называл класс словами («иначе сверка
манифестов выглядит как подмена данных»), но манифест фиксировал **объявленный**
набор, факт прочтения не фиксировал никто — и правило было зелёным.

**Что проверяется.** Каждый ADR, датированный `--since` (по умолчанию 2026-09-21)
и позже, обязан нести раздел «Доказательство исполнения», отвечающий ровно на один
вопрос: *чем доказывается, что решение дошло до потребителя*. Раздел принимается,
если в нём назван

* **носитель** — поле манифеста, строка лога, `sha`/хеш прочитанного файла, путь
  артефакта или число (счётчик), — то есть то, что можно открыть и прочитать, и
* **потребитель** — что носитель возник у ЧИТАЮЩЕЙ стороны (`прочитан`, `загружен`,
  `использован`, `получен`, `совпал`), а не у пишущей («артефакт создан»).

Раздел с заглушкой («будет проверено», «планируется», `TODO`) не принимается:
обещание проверки — не проверка.

**Границы — что страж НЕ проверяет.** Он не судит **достаточность** доказательства:
носитель назван и относится к потребителю — этого довольно для механического
зелёного. Полноту и убедительность смотрит ревьюер (ADR-024), и замены ему здесь
нет. Страж ловит класс «обещано вместо доказанного» и «артефакт вместо потребителя»,
а не «доказательство слабое».

**Раздел не всегда применим.** Решение, не меняющее ни вход, ни артефакт-потребитель,
ни контракт, не имеет потребителя, до которого надо «дойти». Такой ADR обязан нести
раздел с явным `Не применимо:` и причиной — это названное исключение (печатается
в сводке), а не молчаливый пропуск. Форма раздела обязательна для всех новых ADR:
механика не берётся судить, «меняет ли решение вход».

**История не переписывается** (ADR-023 п.9, ADR-028 п.4). ADR до `--since`
печатаются как `legacy-not-checked` со своими именами: молчаливого исключения нет,
но и находки по ним не выставляются — иначе повторяется класс «тест, красный by
construction» (ADR-023 п.12). Задним числом раздел в исторические ADR не
дописывается: пометка-урок — ссылкой (ADR-042 → ADR-051), а не правкой решения.

Коды возврата::

    0 — все ADR после --since предъявили носителя и потребителя (или таких нет)
    1 — нарушение: поимённый список в stdout
    2 — NOT-VERIFIED: нет входа (нет каталога ADR или в нём нет ни одного ADR)

Запуск::

    python3 tools/check_execution_proof.py                 # гейт кейса
    python3 tools/check_execution_proof.py --json          # машинный вердикт
    python3 tools/check_execution_proof.py --since 2026-09-21 --adr-dir docs/adr
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

SECTION = "Доказательство исполнения"
#: Раздел — заголовок 2-го или 3-го уровня: у ADR кейса секции решений `##`,
#: подсекции `###`. Заголовок другого уровня разделом не считается.
SECTION_RE = re.compile(rf"^\s{{0,3}}#{{2,3}}\s+{SECTION}\s*$", re.M | re.I)
#: Следующий заголовок того же или более высокого уровня — конец тела раздела.
NEXT_HEADING_RE = re.compile(r"^\s{0,3}#{1,3}\s+\S", re.M)

FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(\n|$)", re.S)
DATE_RE = re.compile(r'^date:\s*"?(\d{4}-\d{2}-\d{2})"?', re.M)
ID_RE = re.compile(r'^id:\s*"?([^"\n]+)"?', re.M)

#: Раздел-пустышка: обещание проверки вместо проверки.
STUB_RE = re.compile(
    r"будет\s+(?:провер|добав|введ|реализован|сдел|оформлен)|"
    r"планиру|предстоит|позже|в\s+дальнейшем|\bTODO\b|\bTBD\b|"
    r"не\s+проверял|пока\s+не\s+провер",
    re.I)
#: Явная неприменимость — названное исключение, а не находка.
NOT_APPLICABLE_RE = re.compile(r"не\s+применим", re.I)

#: Носители доказательства: что можно открыть и прочитать. Имя носителя печатается
#: в отчёте — по нему видно, ЧЕМ именно доказательство принято (иначе страж сам
#: становится чёрным ящиком).
#:
#: Хеш — ПАРА «слово + дайджест в той же строке»: одно слово без значения («хеш в
#: манифесте») носителем не является, а голый hex — тем более (номера прогонов вроде
#: `sft-20260919-0810` состоят из цифр и подходят под `[0-9a-f]{8}`; тождества они не
#: доказывают). Поэтому близость обязательна, а в отчёт идёт сам дайджест.
HASH_PAIR_RE = re.compile(
    r"(?:(?:sha256|sha512|sha|хеш\w*|хэш\w*|hash|дайджест|коммит\w*|commit)"
    r"[^\n]{0,40}?([0-9a-f]{8,64})"
    r"|([0-9a-f]{8,64})[^\n]{0,40}?(?:sha256|sha512|sha|хеш\w*|хэш\w*|hash|дайджест|"
    r"коммит\w*|commit))", re.I)
CARRIERS: list[tuple[str, re.Pattern[str]]] = [
    ("путь артефакта",
     re.compile(r"(?<![\w/])(?:tools|evidence|docs|model|runs|data|datasets|scripts)"
                r"/[\w./*\-]+(?::\d+)?"
                # Имя файла в кавычках-бэктиках тоже носитель: его открывают и
                # читают. Без этого законный раздел «носитель — `ARCHITECTURE-SPINE.md`,
                # раздел AD-2» краснел бы, а ложное красное — тот самый класс,
                # который кейс запрещает (ADR-023 п.12).
                r"|`[\w.\-]+\.(?:md|json|jsonl|ya?ml|py|sh|txt|npz|csv|log|toml)"
                r"(?::\d+)?`")),
    ("поле-носитель", re.compile(r"`[A-Za-z_][\w]*(?:\.[\w]+)+`|"
                                 r"(?<![\w`])[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*){2,}(?![\w`])")),
    ("sha/хеш + дайджест", HASH_PAIR_RE),
    ("число/счётчик",
     re.compile(r"число\s+(?:сэмплов|примеров|строк|шагов)|сэмплов|примеров\b|"
                r"строк\w*\s+лога|numpy\.load|\bshape\b|\brows_read\b", re.I)),
]
#: Потребитель: носитель возник у ЧИТАЮЩЕЙ стороны. «Создан» и «проверен» здесь
#: намеренно нет — это пишущая сторона («артефакт создан») либо оценка самого
#: артефакта, а не свидетельство того, что его прочли.
WITNESS_RE = re.compile(
    r"проч\w*|чита\w*|загруж\w*|потреб\w*|использ\w*|примен\w*|дошл\w*|получ\w*|"
    r"совпал\w*|сверк\w*|сравн\w*|слича\w*|\bread\b|\bconsum\w*|\bload\w*|\bapplied\b",
    re.I)

MIN_BODY = 200          #: символов в теле раздела: короче — не ответ, а отписка
MIN_NOT_APPLICABLE = 80  #: символов причины у «Не применимо»


class Finding:
    def __init__(self, adr: str, rule: str, message: str, severity: str = "error"):
        self.adr, self.rule, self.message, self.severity = adr, rule, message, severity

    def as_dict(self) -> dict:
        return {"adr": self.adr, "rule": self.rule, "severity": self.severity,
                "message": self.message}


def front_matter(text: str) -> str:
    m = FM_RE.match(text)
    return m.group(1) if m else ""


def adr_date(text: str) -> date | None:
    m = DATE_RE.search(front_matter(text))
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def adr_id(text: str, path: Path) -> str:
    m = ID_RE.search(front_matter(text))
    return m.group(1).strip() if m else path.stem.split("-")[0]


def section_body(text: str) -> str | None:
    """Тело раздела «Доказательство исполнения» или None, если раздела нет."""
    m = SECTION_RE.search(text)
    if not m:
        return None
    rest = text[m.end():]
    nxt = NEXT_HEADING_RE.search(rest)
    return rest[:nxt.start()] if nxt else rest


def first_hit(pattern: re.Pattern[str], text: str) -> str | None:
    """Первое совпадение: у шаблонов с группой показывается группа (у пары
    «хеш + дайджест» — сам дайджест, а не слово перед ним)."""
    m = pattern.search(text)
    if not m:
        return None
    return (m.group(m.lastindex) if m.lastindex else m.group(0)).strip()


def check_adr(path: Path, since: date, findings: list[Finding],
              notes: list[str], checked: list[dict]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    name = adr_id(text, path)
    d = adr_date(text)
    if d is None:
        # ADR без даты не датируется — и не может быть ни «новым», ни «историей».
        # Молчание о нём было бы пропуском, поэтому он назван находкой: дата в
        # шапке ADR обязательна (шаблон кейса), и её отсутствие — дефект шапки.
        findings.append(Finding(name, "date-missing",
                                f"{path.name}: в шапке ADR нет даты (date: YYYY-MM-DD) — "
                                f"принадлежность к правилу не определяется"))
        return
    if d < since:
        notes.append(f"{name}: ADR от {d.isoformat()} до {since.isoformat()} — "
                     f"legacy-not-checked (историю не переписываем, ADR-023 п.9)")
        return

    body = section_body(text)
    if body is None:
        findings.append(Finding(
            name, "no-proof-section",
            f"{path.name}: решение от {d.isoformat()} не несёт раздел "
            f"«## {SECTION}» — не сказано, ЧЕМ доказано, что решение дошло до "
            f"потребителя (не «артефакт создан», а «его прочитали именно так»)"))
        return

    stripped = body.strip()
    if NOT_APPLICABLE_RE.search(stripped):
        if len(stripped) < MIN_NOT_APPLICABLE:
            findings.append(Finding(
                name, "not-applicable-unnamed",
                f"раздел объявляет неприменимость, но не называет причину "
                f"({len(stripped)} символов; нужно ≥ {MIN_NOT_APPLICABLE}): "
                f"не названо, что именно решение не меняет"))
        else:
            notes.append(f"{name}: раздел назван неприменимым с причиной — "
                         f"исключение по существу, не находка")
            checked.append({"adr": name, "date": d.isoformat(), "verdict": "not-applicable"})
        return

    stub = first_hit(STUB_RE, stripped)
    if stub:
        findings.append(Finding(
            name, "proof-stub",
            f"раздел обещает проверку вместо проверки («{stub}»): обещание "
            f"носителем доказательства не является"))
        return

    if len(stripped) < MIN_BODY:
        findings.append(Finding(
            name, "proof-too-short",
            f"раздел не отвечает на вопрос: {len(stripped)} символов при "
            f"минимуме {MIN_BODY}"))
        return

    carriers = [f"{label}: «{hit}»" for label, pat in CARRIERS
                if (hit := first_hit(pat, stripped))]
    if not carriers:
        findings.append(Finding(
            name, "no-carrier",
            "в разделе не назван носитель доказательства (поле манифеста, строка "
            "лога, sha прочитанного файла с дайджестом, путь артефакта или число) — "
            "проверять нечем"))
        return

    witness = first_hit(WITNESS_RE, stripped)
    if not witness:
        findings.append(Finding(
            name, "no-consumer-witness",
            "раздел называет носитель, но не называет ПОТРЕБИТЕЛЯ: не сказано, что "
            "носитель возник у читающей стороны (прочитан/загружен/использован/сверен). "
            "«Артефакт создан» — это пишущая сторона, и именно её свидетельства "
            "не хватило в ADR-042"))
        return

    checked.append({"adr": name, "date": d.isoformat(), "verdict": "proved",
                    "carriers": carriers, "witness": witness})


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Страж «Доказательства исполнения» у новых ADR (ADR-051)")
    ap.add_argument("--adr-dir", default="docs/adr", help="каталог ADR кейса")
    ap.add_argument("--since", default="2026-09-21",
                    help="дата, с которой правило действует (ADR раньше — legacy)")
    ap.add_argument("--json", action="store_true", help="машинный вердикт")
    a = ap.parse_args()

    try:
        since = date.fromisoformat(a.since)
    except ValueError:
        print(f"--since не дата: {a.since}", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    adr_dir = Path(a.adr_dir)
    if not adr_dir.is_dir():
        print(f"NOT-VERIFIED: нет каталога ADR: {adr_dir}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    paths = sorted(adr_dir.glob("*.md"))
    if not paths:
        print(f"NOT-VERIFIED: в {adr_dir} нет ни одного ADR", file=sys.stderr)
        return EXIT_NOT_VERIFIED

    findings: list[Finding] = []
    notes: list[str] = []
    checked: list[dict] = []
    legacy = 0
    for path in paths:
        before = len(notes)
        check_adr(path, since, findings, notes, checked)
        legacy += any("legacy-not-checked" in n for n in notes[before:])

    passed = not findings
    out = {
        "tool": "check_execution_proof",
        "passed": passed,
        "since": since.isoformat(),
        "adrs_total": len(paths),
        "adrs_checked": len(checked),
        "adrs_legacy": legacy,
        "findings": [f.as_dict() for f in findings],
        "checked": checked,
        "notes": notes,
        "summary": (
            f"доказательство исполнения предъявлено: раздел есть, носитель и "
            f"потребитель названы ({len(checked)} ADR после {since.isoformat()}; "
            f"исторических не проверялось: {legacy})" if passed else
            f"нарушений: {len(findings)} (раздел «{SECTION}» обязателен для ADR "
            f"от {since.isoformat()})"),
        "boundary": ("страж требует носителя и потребителя, но НЕ судит "
                     "достаточность доказательства — это смотрит ревьюер (ADR-024)"),
    }
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for n in notes:
            print(f"  · {n}")
        for c in checked:
            if c["verdict"] == "proved":
                print(f"  ok {c['adr']}: носитель — {', '.join(c['carriers'])}; "
                      f"потребитель — «{c['witness']}»")
        for f in findings:
            print(f"  [{f.severity}] {f.adr}: {f.rule} — {f.message}")
        print(out["summary"])
    return EXIT_OK if passed else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
