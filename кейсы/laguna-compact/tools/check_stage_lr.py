#!/usr/bin/env python3
"""Страж темпа стадии: объявленный LR против того, который исполняет копия пайплайна.

**Класс дефекта — тот же, что у C-030, но по другой оси.** C-030 ловит расхождение
«объявленный набор против прочитанного». Здесь ловится расхождение «объявленный
темп против исполняемого»: `full_sft_params.json` — это **декларация** прогона
(её читает человек и по ней сравнивают прогоны), а темп задаётся **строкой в коде**
замороженной копии `laguna_pipeline_sft.py`. Пока копию собирает патч, а объявление
пишет раннер, эти два числа могут разойтись молча — и прогон на 40 часов будет
описан темпом, которым он не учился. Ровно этот дефект (декларация есть, она
конфорна, и она про другое) зафиксирован в S3av по набору.

**Что сверяется, и почему именно это.**

1. **Пик.** Из копии берётся выражение `peak_lr = 1e-5 * S` (или одиночная
   `peak_lr = 1e-5` при штатном темпе) и вычисляется фактический пик. Он обязан
   совпасть с `config.lr` объявления с точностью 1e-12 (числа здесь
   представимы двоично: 1e-5 × 0.2, и допуск нужен только от арифметики).
2. **Множитель.** `config.peak_lr_scale` объявления обязан совпасть с `S` копии.
   Без этой сверки пик мог бы сойтись при разных множителях (например, «0.2 от
   1e-5» объявлено, а в коде зашито `2e-6` литералом) — то есть число сошлось бы,
   а способ его получения разошёлся.
3. **Форма расписания не тронута.** `CosineAnnealingLR(T_max=args.max_steps,
   eta_min=2e-7)` и `warmup_steps = min(100, args.max_steps // 10)` обязаны
   присутствовать в копии дословно. Это не формальность: смысл понижения темпа
   («одна переменная — масштаб») держится ровно на том, что форма штатная. Правка
   формы превратила бы руку в две переменные, и объявление об этом молчало бы.
4. **Ожидание (необязательно).** `--expect-peak` позволяет стартеру объявить
   темп ДО запуска: расхождение — отказ стартовать 40-часовую стадию.

Коды возврата::

    0 — объявление сходится с копией (и с --expect-peak, если задан)
    1 — расхождение: поимённый список в stdout
    2 — NOT-VERIFIED: нет входа (нет копии пайплайна или объявления) — сверять нечего

Запуск::

    python3 tools/check_stage_lr.py --run-dir /home/user/gb10-shared/sft-...
    python3 tools/check_stage_lr.py --run-dir ... --expect-peak 2e-6 --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_NOT_VERIFIED = 0, 1, 2

#: Штатный пик SFT — константа базы (`run_sft`), а не «текущее значение»: множитель
#: определён именно относительно неё, и если база сменит пик, страж обязан это
#: заметить, а не подстроиться.
PEAK_BASE = 1e-5
#: Финал косинуса и правило warmup — часть ФОРМЫ, которую понижение темпа не трогает.
ETA_MIN = 2e-7
SCHEDULE_RE = re.compile(
    r"CosineAnnealingLR\(\s*optimizer,\s*T_max=args\.max_steps,\s*eta_min=2e-7\s*\)")
WARMUP_RE = re.compile(r"warmup_steps = min\(100, args\.max_steps // 10\)")
#: Выражение пика в копии: со множителем (S3ax) или без него (штатный прогон).
PEAK_RE = re.compile(r"^    peak_lr = 1e-5(?: \* _SFT_PEAK_LR_SCALE)?$", re.M)
SCALE_RE = re.compile(r"^    _SFT_PEAK_LR_SCALE = ([0-9.eE+-]+)$", re.M)


class Finding:
    def __init__(self, rule: str, message: str):
        self.rule, self.message = rule, message

    def as_dict(self) -> dict:
        return {"rule": self.rule, "message": self.message, "severity": "error"}


def check(run_dir: Path, expect_peak: float | None) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    facts: dict = {"run_dir": str(run_dir)}

    pipe = run_dir / "laguna_pipeline_sft.py"
    params_f = run_dir / "full_sft_params.json"
    missing = [str(p) for p in (pipe, params_f) if not p.is_file()]
    if missing:
        return [], {"not_verified": "нет входа", "missing": missing}

    text = pipe.read_text(encoding="utf-8")
    params = json.loads(params_f.read_text(encoding="utf-8"))
    cfg = params.get("config", {})

    # ── факт: что исполняет копия ────────────────────────────────────────────
    peaks = PEAK_RE.findall(text)
    if len(peaks) != 1:
        return [Finding("lr_peak_expr",
                        f"в копии пайплайна строк с `peak_lr = 1e-5…` найдено "
                        f"{len(peaks)}, а обязана быть ровно одна")], facts
    scale_m = SCALE_RE.findall(text)
    if len(scale_m) > 1:
        return [Finding("lr_scale_expr",
                        f"множителей _SFT_PEAK_LR_SCALE найдено {len(scale_m)}, "
                        "обязан быть не более одного")], facts
    actual_scale = float(scale_m[0]) if scale_m else 1.0
    if scale_m and "_SFT_PEAK_LR_SCALE" not in peaks[0]:
        findings.append(Finding(
            "lr_scale_unused",
            "множитель _SFT_PEAK_LR_SCALE объявлен, но строка пика его не "
            "использует: темп не изменился бы, а объявление называло бы новый"))
    actual_peak = PEAK_BASE * (actual_scale if "_SFT_PEAK_LR_SCALE" in peaks[0] else 1.0)

    facts["pipeline_sha256"] = hashlib.sha256(pipe.read_bytes()).hexdigest()
    facts["actual"] = {"peak_lr": actual_peak, "peak_lr_scale": actual_scale,
                       "eta_min_present": bool(SCHEDULE_RE.search(text)),
                       "warmup_unchanged": bool(WARMUP_RE.search(text)),
                       "schedule_T_max": "args.max_steps" if SCHEDULE_RE.search(text) else None}

    # ── форма расписания: её понижение темпа обязано НЕ трогать ──────────────
    if not SCHEDULE_RE.search(text):
        findings.append(Finding(
            "lr_schedule_shape",
            "в копии нет дословного CosineAnnealingLR(optimizer, T_max=args.max_steps, "
            "eta_min=2e-7): форма расписания изменена или переписана — тогда рука "
            "отличается от контроля не только масштабом, и объявление об этом молчит"))
    if not WARMUP_RE.search(text):
        findings.append(Finding(
            "lr_warmup_shape",
            "в копии нет дословного `warmup_steps = min(100, args.max_steps // 10)`: "
            "warmup изменён — это вторая переменная, а не понижение темпа"))

    # ── декларация против факта ──────────────────────────────────────────────
    declared_peak = cfg.get("lr")
    declared_scale = cfg.get("peak_lr_scale")
    declared_min = cfg.get("lr_min")
    facts["declared"] = {"peak_lr": declared_peak, "peak_lr_scale": declared_scale,
                         "lr_min": declared_min, "schedule": cfg.get("schedule"),
                         "warmup_steps": cfg.get("warmup_steps")}

    if declared_peak is None:
        findings.append(Finding("lr_declared_missing",
                                "объявление не называет `config.lr` — сверять нечего"))
    elif abs(float(declared_peak) - actual_peak) > 1e-12:
        findings.append(Finding(
            "lr_declared_vs_actual",
            f"объявленный пик {declared_peak!r} не равен исполняемому {actual_peak!r} "
            f"(копия: 1e-5 × {actual_scale:g}) — прогон был бы описан темпом, "
            "которым он не учится"))

    if declared_scale is None:
        findings.append(Finding(
            "lr_scale_declared_missing",
            "объявление не называет `config.peak_lr_scale` — способ получения пика "
            "не объявлен"))
    elif abs(float(declared_scale) - actual_scale) > 1e-12:
        findings.append(Finding(
            "lr_scale_declared_vs_actual",
            f"объявленный множитель {declared_scale!r} не равен множителю копии "
            f"{actual_scale!r}"))

    if declared_min is not None and abs(float(declared_min) - ETA_MIN) > 1e-15:
        findings.append(Finding(
            "lr_min_changed",
            f"объявленный финал {declared_min!r} не равен штатному 2e-7: понижение "
            "темпа не должно менять финал расписания"))

    # ── ожидание стартера (объявлено ДО запуска) ─────────────────────────────
    if expect_peak is not None and abs(float(expect_peak) - actual_peak) > 1e-12:
        findings.append(Finding(
            "lr_expect_peak",
            f"стартер ожидал пик {float(expect_peak)!r}, копия исполняет "
            f"{actual_peak!r}"))
    return findings, facts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="каталог прогона стадии")
    ap.add_argument("--expect-peak", type=float, default=None,
                    help="объявленный ДО запуска пик LR (расхождение — отказ)")
    ap.add_argument("--json", action="store_true", help="машинный вердикт")
    a = ap.parse_args()

    run_dir = Path(a.run_dir)
    if not run_dir.is_dir():
        print(f"каталога прогона нет: {run_dir}", file=sys.stderr)
        return EXIT_NOT_VERIFIED
    findings, facts = check(run_dir, a.expect_peak)
    if "not_verified" in facts:
        out = {"passed": None, "verdict": "NOT-VERIFIED", "tool": "check_stage_lr.py",
               **facts, "findings": [f.as_dict() for f in findings]}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return EXIT_NOT_VERIFIED

    passed = not findings
    out = {"passed": passed, "verdict": "OK" if passed else "FAIL",
           "tool": "check_stage_lr.py", **facts,
           "findings": [f.as_dict() for f in findings]}
    print(json.dumps(out, ensure_ascii=False, indent=2) if a.json else
          _human(out))
    return EXIT_OK if passed else EXIT_FAIL


def _human(out: dict) -> str:
    a, d = out["actual"], out["declared"]
    lines = [
        f"страж темпа: {out['verdict']}",
        f"  копия:      пик {a['peak_lr']:.4e} = 1e-5 × {a['peak_lr_scale']:g}; "
        f"финал 2e-7 {a['eta_min_present']}; warmup штатный {a['warmup_unchanged']}",
        f"  объявление: пик {d['peak_lr']!r}; множитель {d['peak_lr_scale']!r}; "
        f"финал {d['lr_min']!r}; warmup {d['warmup_steps']!r}",
    ]
    for f in out["findings"]:
        lines.append(f"  [error] {f['rule']}: {f['message']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
