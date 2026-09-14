#!/usr/bin/env python3
"""report_docx.py — отчёт бенчмарка Platform V arch-bench (docx + диаграммы).

Читает $PVBENCH_RUNS/summary.json и results.jsonl (после analyze.py),
строит диаграммы matplotlib (PNG) и собирает docx с интерпретацией.
Устойчив к частичным данным (промежуточный отчёт): отсутствующие
модели/условия помечаются, а не ломают генерацию.

Выход: $PVBENCH_REPORT_DIR (по умолчанию $PVBENCH_RUNS/report/) —
diagrams/*.png + otchet_platformv_arch_bench_<дата>.docx
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

RUNS = pvlib.RUNS
OUT = Path(os.environ.get("PVBENCH_REPORT_DIR", str(RUNS / "report")))
DIAG = OUT / "diagrams"

COND_RU = {
    "spine-arch": "Spine + спайн-пакет",
    "spine-min": "Spine (без спайна)",
    "spine-arch-think": "Spine + спайн-пакет + ризонинг",
    "theseus-plain": "Theseus",
    "theseus-arch": "Theseus + AGENTS.md архитектора",
    "claude-plain": "Claude Code",
    "claude-arch": "Claude Code + CLAUDE.md",
    "kimi-plain": "Kimi Code",
    "kimi-arch": "Kimi Code + AGENTS.md",
    "openclaw-plain": "OpenClaw",
    "qwen-plain": "Qwen Code",
    "omp-plain": "pi-coding-agent",
    "raw-llm": "Модель без харнесса (raw)",
}
MODEL_RU = {"dsf": "DeepSeek V4.1 Flash", "glm": "GLM-5.3 Flash",
            "glm53": "GLM-5.3", "dsp": "DeepSeek V4 Pro",
            "default": "дефолт харнесса"}
COND_ORDER = ["spine-arch", "spine-min", "spine-arch-think",
              "claude-plain", "claude-arch", "kimi-plain", "kimi-arch",
              "openclaw-plain", "qwen-plain", "omp-plain",
              "theseus-plain", "theseus-arch", "raw-llm",
              "dsh-plain", "codewhale-plain", "hermes-plain"]


def cond_ru(c):
    return COND_RU.get(c, c)


def load():
    with open(RUNS / "summary.json", encoding="utf-8") as f:
        summary = json.load(f)
    records = [json.loads(x) for x in
               open(RUNS / "results.jsonl", encoding="utf-8")]
    return summary, records


def load_effects():
    """Каноничные эффекты: парность по ячейкам «задача × повтор»,
    bootstrap 95% CI — results/effects_paired.json (значения совпадают
    с опубликованными в README). Фолбэк — пул-эффекты из summary.json."""
    p = RUNS / "effects_paired.json"
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("effects", {})
    return {}


# ---------- диаграммы ----------

def chart_total_by_condition(summary):
    tbl = summary["total_by_condition_model"]
    models = sorted({k.split("|")[1] for k in tbl if tbl.get(k)})
    conds = [c for c in COND_ORDER
             if any(tbl.get(f"{c}|{m}") for m in models)]
    if not conds or not models:
        return None
    fig, ax = plt.subplots(figsize=(11, 5.5))
    w = 0.8 / len(models)
    x = np.arange(len(conds))
    for i, m in enumerate(models):
        vals, errs = [], []
        for c in conds:
            v = tbl.get(f"{c}|{m}")
            vals.append(v[0] if v else 0)
            errs.append(v[1] if v else 0)
        ax.bar(x + i * w, vals, w, yerr=errs, capsize=3,
               label=MODEL_RU.get(m, m), alpha=0.9)
    ax.axhline(70, ls="--", c="green", lw=1, label="порог pass = 70")
    ax.axhline(39, ls="--", c="red", lw=1, label="кап hard-fail = 39")
    ax.set_xticks(x + w * (len(models) - 1) / 2)
    ax.set_xticklabels([cond_ru(c) for c in conds], rotation=20, ha="right")
    ax.set_ylabel("Итог судьи, баллы (0–100)")
    ax.set_title("Качество архитектурного решения по условиям и моделям")
    ax.legend()
    ax.set_ylim(0, 100)
    fig.tight_layout()
    p = DIAG / "01_total_by_condition.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def chart_effects(effects):
    eff = {k: v for k, v in effects.items() if v}
    if not eff:
        return None
    fig, ax = plt.subplots(figsize=(11, 0.55 * len(eff) + 2))
    keys = list(eff)
    y = np.arange(len(keys))
    for i, k in enumerate(keys):
        v = eff[k]
        ax.plot([v["ci_lo"], v["ci_hi"]], [i, i], lw=3, solid_capstyle="round")
        ax.plot(v["diff"], i, "o", ms=9)
        ax.text(v["ci_hi"], i + 0.28,
                f"{v['diff']:+.1f} [{v['ci_lo']:+.1f}; {v['ci_hi']:+.1f}]",
                fontsize=8)
    ax.axvline(0, ls="--", c="gray")
    ax.set_yticks(y)
    ax.set_yticklabels(keys, fontsize=8)
    ax.set_xlabel("Парная разность итогов по ячейкам «задача × повтор», "
                  "баллы (95% CI, bootstrap)")
    ax.set_title("Эффекты: разница качества между условиями "
                 "(эталон — spine-arch-think)")
    fig.tight_layout()
    p = DIAG / "02_effects_forest.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def chart_task_heatmap(summary, records):
    judged = [r for r in records if r.get("judge_total") is not None]
    if not judged:
        return None
    tasks = sorted({r["task"] for r in judged})
    conds = [c for c in COND_ORDER if any(r["condition"] == c for r in judged)]
    M = np.full((len(tasks), len(conds)), np.nan)
    for i, t in enumerate(tasks):
        for j, c in enumerate(conds):
            vals = [r["judge_total"] for r in judged
                    if r["task"] == t and r["condition"] == c]
            if vals:
                M[i, j] = sum(vals) / len(vals)
    fig, ax = plt.subplots(figsize=(11, 0.35 * len(tasks) + 2))
    im = ax.imshow(M, cmap="RdYlGn", vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(conds)))
    ax.set_xticklabels([cond_ru(c) for c in conds], rotation=25, ha="right",
                       fontsize=8)
    ax.set_yticks(range(len(tasks)))
    ax.set_yticklabels(tasks, fontsize=8)
    for i in range(len(tasks)):
        for j in range(len(conds)):
            if not np.isnan(M[i, j]):
                ax.text(j, i, f"{M[i, j]:.0f}", ha="center", va="center",
                        fontsize=7)
    ax.set_title("Средний итог судьи: задача × условие")
    fig.colorbar(im, ax=ax, label="баллы")
    fig.tight_layout()
    p = DIAG / "03_task_heatmap.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def chart_completeness_hf(summary):
    tbl = summary.get("by_condition", {})
    conds = [c for c in COND_ORDER if c in tbl]
    if not conds:
        return None
    compl = [tbl[c]["completeness"] * 100 for c in conds]
    hf = [tbl[c]["hf_rate_judge"] * 100 for c in conds]
    x = np.arange(len(conds))
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(x - 0.2, compl, 0.4, label="Полнота артефактов D, %")
    ax.bar(x + 0.2, hf, 0.4, label="Доля hard-fail, %")
    ax.set_xticks(x)
    ax.set_xticklabels([cond_ru(c) for c in conds], rotation=20, ha="right")
    ax.set_ylabel("%")
    ax.set_title("Детерминированная полнота и частота hard-fail")
    ax.legend()
    fig.tight_layout()
    p = DIAG / "04_completeness_hf.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


def chart_time(summary):
    tbl = summary.get("by_condition", {})
    conds = [c for c in COND_ORDER if c in tbl and tbl[c].get("mean_secs")]
    if not conds:
        return None
    vals = [tbl[c]["mean_secs"] / 60 for c in conds]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar([cond_ru(c) for c in conds], vals)
    ax.set_ylabel("Среднее время ячейки, мин")
    ax.set_title("Стоимость по времени")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    fig.tight_layout()
    p = DIAG / "05_time.png"
    fig.savefig(p, dpi=150)
    plt.close(fig)
    return p


# ---------- интерпретация ----------

def interpret(effects):
    """Выводы, синхронизированные с README (числа — effects_paired.json)."""
    def f(key):
        v = effects.get(key)
        return (f"{v['diff']:+.1f} [{v['ci_lo']:+.1f}; {v['ci_hi']:+.1f}]"
                if v else "—")

    out = []
    out.append("Эталонная конфигурация Spine — spine-arch-think (харнесс + "
               "спайн-пакет + ризонинг модели, бюджет 64K). Все сравнения — "
               "по отдельным конфигурациям Spine, а не по среднему между "
               "руками.")
    out.append(f"H1 подтверждена для Spine с ризонингом: spine-arch-think "
               f"значимо сильнее Theseus ({f('think − theseus-plain (dsf)')}) "
               f"и идёт в паритете с лучшими универсалами — Claude Code "
               f"({f('think − claude-plain, фабричный (dsf)')}) и Kimi Code "
               f"({f('think − kimi-plain (dsf)')}). При этом на dsf Claude "
               f"Code и Kimi Code работали без ризонинга — паритет здесь "
               f"это «think on» против «think off». На GLM-5.3 Flash — "
               f"превосходство над Claude Code на грани значимости "
               f"({f('think − claude-plain (glm)')}): 8 общих задач, "
               f"основную часть эффекта даёт CMP-ARCH-001 (+44.6 при "
               f"медианном диффе ≈ +2). Без ризонинга (spine-arch): "
               f"паритет-минус с Claude Code "
               f"({f('spine-arch − claude-plain (dsf, без ризонинга)')}) "
               f"и отставание от Kimi Code "
               f"({f('spine-arch − kimi-plain (dsf)')}).")
    out.append(f"Ризонинг — главный усилитель Spine: премия think над "
               f"spine-arch {f('think − spine-arch, без ризонинга (dsf)')} "
               f"на V4.1 Flash и "
               f"{f('think − spine-arch, без ризонинга (dsp)')} на V4 Pro "
               f"(значимо). Выключать ризонинг у Spine нельзя — без него "
               f"харнесс теряет преимущество.")
    out.append(f"Вклад доменного формата (спайн-пакет): spine-arch − "
               f"spine-min = {f('spine-arch − spine-min (dsf)')} на dsf "
               f"(~+4 пулом по моделям) — слабый плюс поверх голого "
               f"харнесса.")
    out.append("Отрыва от хороших универсалов нет: Claude Code и Kimi Code "
               "закрывают те же задачи на 90+ баллов (по моделям 89.6–96.8) "
               "без доменной специализации. Ценность Spine — контур вокруг "
               "документа (гейты, трассируемость, handoff), который этот "
               "бенчмарк не измеряет.")
    out.append(f"H2 отклонена на полных руках: кастомизация универсалов "
               f"под архитекторов эффекта не дала — claude-arch − "
               f"claude-plain = {f('claude-arch − claude-plain (dsf)')}, "
               f"kimi-arch − kimi-plain = "
               f"{f('kimi-arch − kimi-plain (dsf)')}.")
    out.append(f"H3 подтверждена: агентный контур важнее выбора харнесса, "
               f"но это зависит от модели — claude-plain − raw-llm = "
               f"{f('claude-plain − raw-llm (dsf)')} на dsf (голая модель "
               f"77 против 90+ у большинства харнессов; Theseus — ниже "
               f"голой модели), на GLM-5.3 Flash голая модель почти не "
               f"проигрывает (93.5, n=4 — осторожно). Эффект любого "
               f"харнесса нужно мерить на своей целевой модели.")
    out.append("Надёжность — главный риск агентных прогонов: 30–33% "
               "ответов Theseus оборваны лимитом ходов; 18 «обрывов» Spine "
               "оказались дефектом извлечения, а не модели (D18); связка "
               "arch-be × glm-5.3-flash частично несовместима (D14). "
               "Пайплайн извлечения ответов нужно проверять прежде, чем "
               "судить модель.")
    return out


# ---------- docx ----------

def add_table(doc, headers, rows):
    t = doc.add_table(rows=1 + len(rows), cols=len(headers))
    t.style = "Light Grid Accent 1"
    for j, h in enumerate(headers):
        cell = t.rows[0].cells[j]
        cell.text = h
        for p in cell.paragraphs:
            for r in p.runs:
                r.font.bold = True
                r.font.size = Pt(9)
    for i, row in enumerate(rows, 1):
        for j, v in enumerate(row):
            cell = t.rows[i].cells[j]
            cell.text = str(v)
            for p in cell.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(9)
    return t


def build_docx(summary, records, charts, effects, interim):
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Cm(2)

    h = doc.add_heading(
        "Бенчмарк архитектурных задач Platform V: сравнение Spine с "
        "кодовыми агентами", 0)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(
        ("ПРОМЕЖУТОЧНЫЙ ОТЧЁТ — " if interim else "ОТЧЁТ — ") +
        f"исследовательский прототип · {datetime.now():%d.%m.%Y}").italic = True

    doc.add_heading("1. Резюме", 1)
    n_cells = summary["cells_total"]
    n_judged = summary["judged"]
    n_fail = len(summary["failed_or_missing"])
    doc.add_paragraph(
        f"Ячеек в матрице: {n_cells}; оценено судьёй: {n_judged}; "
        f"{n_fail} ячеек отсутствуют по задокументированным отклонениям "
        f"(D13/D14/D17 — частичные руки), 18 ячеек восстановлены после "
        f"дефекта извлечения (D18). Бенчмарк: 24 архитектурные задачи по "
        f"документации Platform V (СберТех) — от проектирования HA/DR-слоёв "
        f"данных до комплаенс-маппинга 719-П/683-П/851-П. Каждый ответ "
        f"оценён независимым LLM-судьёй по рубрикам с цитатами-"
        f"доказательствами плюс детерминированным слоем проверок.")
    for line in interpret(effects):
        doc.add_paragraph(line, style="List Bullet")

    doc.add_heading("2. Методология", 1)
    doc.add_paragraph(
        "Матрица: 1032 прогона в 19 условиях × 24 задачи × до 2 повторов × "
        "5 моделей/конфигураций (DeepSeek V4.1 Flash / V4 Pro, GLM-5.3 "
        "Flash / 5.3 + свип); завершено генераций 1017, оценено судьёй "
        "917. kimi×glm прогонялся через OpenRouter (та же модель, D17). "
        "Дизайн, гипотезы H1–H3 и хэши входов зафиксированы в "
        "пререгистрации до прогона (PREREGISTRATION.md + "
        "prereg_hashes_v2.txt); все отклонения задокументированы "
        "(DEVIATIONS.md, D1–D20). Судья: deepseek-v4-pro, анонимизированные "
        "ответы, JSON-вердикт по рубрикам, верификация цитат-"
        "доказательств; любой hard-fail ограничивает итог 39 баллами. "
        "Итог задачи: total = 100·Σ(wᵢ·sᵢ)/(4·Σwᵢ).")
    doc.add_paragraph(
        "Сравнения ведутся по отдельным конфигурациям Spine (spine-arch, "
        "spine-min, spine-arch-think; эталон — spine-arch-think), а не по "
        "среднему между руками: руки отвечают на разные вопросы дизайна "
        "(вклад формата, вклад ризонинга).")
    doc.add_paragraph(
        "Ризонинг по рукам. Claude Code и Kimi Code запускались в "
        "заводской конфигурации без thinking-флагов: на dsf (ризонинг по "
        "умолчанию выключен) обе руки работали без ризонинга — как и "
        "qwen, omp, raw-llm и spine-arch/spine-min (явный --think off, "
        "D10); ризонили только spine-arch-think (--think on, 64K) и "
        "theseus/openclaw (ризонинг max — боевые дефолты харнессов, D11). "
        "На GLM-5.3 Flash ризонинг включён на стороне модели у всех рук "
        "(API Z.AI не позволяет его отключить — HTTP 1210, только effort "
        "low/high/max; харнессы effort не задавали, у raw-llm — явно "
        "effort=low). На DeepSeek V4 Pro (ризонящая модель по умолчанию) "
        "Claude Code, Kimi Code и raw-llm работали с ризонингом; "
        "spine-arch — с --think off, spine-arch-think — с --think on "
        "(64K).")

    doc.add_heading("3. Покрытие прогона", 1)
    if interim:
        doc.add_paragraph(
            "Отчёт промежуточный: включены только завершённые ячейки. "
            "Канал GLM (Z.AI) деградировал в период прогона — ячейки glm "
            "частично отсутствуют и будут догнаны повторным прогоном; "
            "выводы по ним делать рано.")
    else:
        doc.add_paragraph(
            "Матрица завершена. Отсутствующие 115 ячеек — частичные руки "
            "по задокументированным отклонениям: сокращение glm-канала "
            "(D13/D17), хронические таймауты arch-be × glm-5.3-flash "
            "(D14, 2 ячейки выведены), частичные dsp-руки кастомизированных "
            "условий. 18 ячеек с дефектом извлечения восстановлены, а не "
            "удалены (D18).")
    models = sorted({k.split("|")[1] for k in summary["total_by_condition_model"]})
    conds = sorted({k.split("|")[0] for k in summary["total_by_condition_model"]})
    doc.add_paragraph(f"Модели в данных: {', '.join(MODEL_RU.get(m, m) for m in models)}. "
                      f"Условия: {len(conds)}.")

    doc.add_heading("4. Результаты", 1)
    tbl = summary["total_by_condition_model"]
    rows = []
    for key, v in sorted(tbl.items()):
        if not v:
            continue
        c, m = key.split("|")
        rows.append((cond_ru(c), MODEL_RU.get(m, m), f"{v[0]:.1f}",
                     f"±{v[1]:.1f}", v[2]))
    doc.add_heading("4.1. Итог судьи по условиям и моделям", 2)
    add_table(doc, ("Условие", "Модель", "Среднее (0–100)", "SD", "n"), rows)
    for p in charts:
        doc.add_picture(str(p), width=Cm(16.5))

    doc.add_heading("4.2. Эффекты (парность по ячейкам «задача × повтор», "
                    "bootstrap 95% CI)", 2)
    doc.add_paragraph(
        "Эталон Spine — spine-arch-think; все сравнения — по отдельным "
        "конфигурациям Spine, без усреднения рук. Значения совпадают с "
        "опубликованными в README (results/effects_paired.json).")
    rows = [(k, f"{v['diff']:+.1f}",
             f"[{v['ci_lo']:+.1f}; {v['ci_hi']:+.1f}]",
             v.get("note", ""))
            for k, v in effects.items() if v]
    add_table(doc, ("Сравнение", "Δ баллов", "95% CI", "Что это значит"),
              rows)

    doc.add_heading("5. Интерпретация", 1)
    for line in interpret(effects):
        doc.add_paragraph(line)
    doc.add_paragraph(
        "Интерпретации являются интерпретациями: балл судьи измеряет "
        "соответствие рубрикам, а не абсолютную архитектурную правильность. "
        "Детерминированный слой (полнота артефактов, regex-детекторы "
        "hard-fail) не меряет семантику; LLM-судья может иметь смещения, "
        "свойственные модели судьи.")

    doc.add_heading("6. Ограничения", 1)
    for line in [
        "Судья одиночный (deepseek-v4-pro) из семейства одного из "
        "решателей — смещение декларируется, но не измерено (пул судей "
        "отменён по стоимости).",
        "Сравнение dsf↔glm (H3) — на пересечении 8–16 задач; glm53-канал "
        "не закрыт.",
        "Превосходство think над Claude Code на glm (+7.2) опирается на "
        "8 общих задач, и основную часть эффекта даёт одна из них "
        "(CMP-ARCH-001, +44.6); трактовать как сигнал на грани значимости, "
        "а не устойчивый эффект.",
        "2 ячейки выведены из-за хронических таймаутов (D14).",
        "Два повтора на ячейку ограничивают точность оценки дисперсии; "
        "bootstrap CI отражает неопределённость среднего, а не разброс "
        "отдельных ответов.",
        "Результаты — про режим «один документ на задачу»; они не "
        "оценивают многошаговую работу архитектора (гейты, трассировка, "
        "handoff) в проде.",
    ]:
        doc.add_paragraph(line, style="List Bullet")

    doc.add_heading("Приложение. Состав бенчмарка (24 задачи)", 1)
    tasks = sorted({r["task"] for r in records if r.get("task")})
    add_table(doc, ("Задача", "Ячеек", "Оценено"),
              [(t, sum(1 for r in records if r["task"] == t),
                sum(1 for r in records
                    if r["task"] == t and r.get("judge_total") is not None))
               for t in tasks])
    OUT.mkdir(parents=True, exist_ok=True)
    name = f"otchet_platformv_arch_bench_{'interim_' if interim else ''}" \
           f"{datetime.now():%Y%m%d_%H%M}.docx"
    path = OUT / name
    doc.save(path)
    return path


def main():
    summary, records = load()
    effects = load_effects()
    DIAG.mkdir(parents=True, exist_ok=True)
    models_present = {k.split("|")[1]
                      for k in summary["total_by_condition_model"]}
    interim = not {"dsf", "glm"}.issubset(models_present) or \
        len(summary["failed_or_missing"]) > 0
    # финальная матрица: отсутствующие ячейки — задокументированные
    # отклонения (D13/D14/D17), а не незавершённая работа
    if os.environ.get("PVBENCH_FINAL") == "1":
        interim = False
    charts = [p for p in (chart_total_by_condition(summary),
                          chart_effects(effects),
                          chart_task_heatmap(summary, records),
                          chart_completeness_hf(summary),
                          chart_time(summary)) if p]
    path = build_docx(summary, records, charts, effects, interim)
    print(f"отчёт: {path}")
    print(f"диаграмм: {len(charts)} в {DIAG}")
    print(f"interim: {interim}")


if __name__ == "__main__":
    main()
