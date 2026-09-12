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
    "theseus-plain": "Theseus",
    "theseus-arch": "Theseus + AGENTS.md архитектора",
    "claude-plain": "Claude Code",
    "claude-arch": "Claude Code + CLAUDE.md",
    "raw-llm": "Модель без харнесса (raw)",
}
MODEL_RU = {"dsf": "DeepSeek V4 Flash", "glm": "GLM-5.3 Flash",
            "glm53": "GLM-5.3", "dsp": "DeepSeek V4 Pro",
            "default": "дефолт харнесса"}
COND_ORDER = ["spine-arch", "spine-min", "theseus-plain", "theseus-arch",
              "claude-plain", "claude-arch", "raw-llm",
              "dsh-plain", "codewhale-plain", "hermes-plain",
              "openclaw-plain", "kimi-plain"]


def cond_ru(c):
    return COND_RU.get(c, c)


def load():
    with open(RUNS / "summary.json", encoding="utf-8") as f:
        summary = json.load(f)
    records = [json.loads(x) for x in
               open(RUNS / "results.jsonl", encoding="utf-8")]
    return summary, records


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


def chart_effects(summary):
    eff = {**summary.get("effects", {}), **summary.get("effects_by_model", {})}
    eff = {k: v for k, v in eff.items() if v}
    if not eff:
        return None
    fig, ax = plt.subplots(figsize=(10, 0.6 * len(eff) + 2))
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
    ax.set_yticklabels(keys, fontsize=9)
    ax.set_xlabel("Разность средних итогов, баллы (95% CI, bootstrap)")
    ax.set_title("Эффекты: разница качества между условиями")
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

def interpret(summary):
    out = []
    eff = summary.get("effects", {})
    ebm = summary.get("effects_by_model", {})

    def sig(v):
        return v and (v["ci_lo"] > 0 or v["ci_hi"] < 0)

    e1 = eff.get("spine-arch - theseus-plain")
    e2 = eff.get("spine-arch - claude-plain")
    e3 = eff.get("theseus-arch - theseus-plain")
    e4 = eff.get("claude-arch - claude-plain")
    e5 = eff.get("spine-arch - spine-min")

    if e1 and e2:
        if sig(e1) and sig(e2) and e1["diff"] > 0 and e2["diff"] > 0:
            out.append(f"H1 подтверждена: Spine со спайн-пакетом превосходит "
                       f"кодовые агенты без кастомизации на {e1['diff']:+.1f} "
                       f"балла против Theseus и на {e2['diff']:+.1f} против "
                       f"Claude Code; оба доверительных интервала не "
                       f"пересекают ноль.")
        else:
            out.append(f"H1 на текущих данных не подтверждена однозначно: "
                       f"разности против Theseus ({e1['diff']:+.1f} "
                       f"[{e1['ci_lo']:+.1f}; {e1['ci_hi']:+.1f}]) и Claude "
                       f"Code ({e2['diff']:+.1f} [{e2['ci_lo']:+.1f}; "
                       f"{e2['ci_hi']:+.1f}]) либо малы, либо их интервалы "
                       f"пересекают ноль.")
    if e5:
        out.append(f"Вклад спайн-контекста отдельно от харнесса (spine-arch "
                   f"против spine-min): {e5['diff']:+.1f} балла "
                   f"[{e5['ci_lo']:+.1f}; {e5['ci_hi']:+.1f}] — "
                   + ("различие статистически значимо."
                      if sig(e5) else "различие значимо не доказано."))
    if e3 or e4:
        parts = []
        if e3:
            parts.append(f"Theseus {e3['diff']:+.1f} "
                         f"[{e3['ci_lo']:+.1f}; {e3['ci_hi']:+.1f}]")
        if e4:
            parts.append(f"Claude Code {e4['diff']:+.1f} "
                         f"[{e4['ci_lo']:+.1f}; {e4['ci_hi']:+.1f}]")
        out.append("H2 (кастомизация кодовых агентов под архитекторов): "
                   + "; ".join(parts) + ".")
    ds = ebm.get("spine-arch - theseus-plain | dsf")
    gl = ebm.get("spine-arch - theseus-plain | glm")
    if ds and gl:
        dep = abs(ds["diff"] - gl["diff"]) > 5
        out.append(f"H3 (модель-зависимость эффекта спайна): разность "
                   f"spine-arch против theseus-plain на DeepSeek V4 Flash "
                   f"{ds['diff']:+.1f}, на GLM-5.3 Flash {gl['diff']:+.1f} — "
                   + ("эффект заметно зависит от модели."
                      if dep else "эффект устойчив между моделями."))
    elif ds:
        out.append("H3: данных по glm пока нет (деградация канала Z.AI); "
                   f"на dsf эффект спайна {ds['diff']:+.1f} "
                   f"[{ds['ci_lo']:+.1f}; {ds['ci_hi']:+.1f}].")
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


def build_docx(summary, records, charts, interim):
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
        f"Прогнозо ячеек: {n_cells}; оценено судьёй: {n_judged}; "
        f"сбоев/нет ответа: {n_fail}. Бенчмарк: 24 архитектурные задачи по "
        f"документации Platform V (СберТech) — от проектирования HA/DR-слоёв "
        f"данных до комплаенс-маппинга 719-П/683-П/851-П. Каждый ответ "
        f"оценён независимым LLM-судьёй по рубрикам с цитатами-"
        f"доказательствами плюс детерминированным слоем проверок.")
    for line in interpret(summary):
        doc.add_paragraph(line, style="List Bullet")

    doc.add_heading("2. Методология", 1)
    doc.add_paragraph(
        "Матрица: 24 задачи × условия (Spine со спайн-пакетом и "
        "fitness-гейтом; Spine без спайна; Theseus и Claude Code — без "
        "кастомизации и с кастомизацией архитектора AGENTS.md/CLAUDE.md; "
        "голая модель raw) × модели (DeepSeek V4 Flash/Pro, GLM-5.3/"
        "5.3 Flash) × повторы. Дизайн, гипотезы H1–H3 и хэши входов "
        "зафиксированы в пререгистрации до прогона "
        "(benchmarks/platformv-arch-bench/PREREGISTRATION.md, монорепо "
        "Spine). Судья анонимизирован: не знает ни условия, ни модели "
        "ответа. Итог задачи: total = 100·Σ(wᵢ·sᵢ)/(4·Σwᵢ); любой hard-fail "
        "ограничивает итог 39 баллами.")

    doc.add_heading("3. Покрытие прогона", 1)
    if interim:
        doc.add_paragraph(
            "Отчёт промежуточный: включены только завершённые ячейки. "
            "Канал GLM (Z.AI) деградировал в период прогона — ячейки glm "
            "частично отсутствуют и будут догнаны повторным прогоном; "
            "выводы по ним делать рано.")
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

    doc.add_heading("4.2. Эффекты (bootstrap 95% CI)", 2)
    eff = {**summary.get("effects", {}), **summary.get("effects_by_model", {})}
    rows = [(k, f"{v['diff']:+.1f}", f"[{v['ci_lo']:+.1f}; {v['ci_hi']:+.1f}]",
             f"{v['n_a']} vs {v['n_b']}") for k, v in eff.items() if v]
    add_table(doc, ("Сравнение", "Разность", "95% CI", "n"), rows)

    doc.add_heading("5. Интерпретация", 1)
    for line in interpret(summary):
        doc.add_paragraph(line)
    doc.add_paragraph(
        "Интерпретации являются интерпретациями: балл судьи измеряет "
        "соответствие рубрикам, а не абсолютную архитектурную правильность. "
        "Детерминированный слой (полнота артефактов, regex-детекторы "
        "hard-fail) не меряет семантику; LLM-судья может иметь смещения, "
        "свойственные модели судьи.")

    doc.add_heading("6. Ограничения", 1)
    for line in [
        "Промежуточный срез: покрытие ячеек неполное (см. §3); итоговые "
        "выводы — только по полной матрице.",
        "Судья glm-5.3 не участвовал как решатель в основной матрице "
        "(решатели — flash/pro-тиры), что снижает конфаунд «родного судьи»; "
        "тем не менее смещение судьи к своему семейству не исключено.",
        "Два повтора на ячейку ограничивают точность оценки дисперсии; "
        "bootstrap CI отражает неопределённость среднего, а не разброс "
        "отдельных ответов.",
        "Деградация Z.AI в окне прогона могла сместить выборку glm-ячеек "
        "(выжившие ячейки — из «здоровых» окон upstream).",
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
    DIAG.mkdir(parents=True, exist_ok=True)
    models_present = {k.split("|")[1]
                      for k in summary["total_by_condition_model"]}
    interim = not {"dsf", "glm"}.issubset(models_present) or \
        len(summary["failed_or_missing"]) > 0
    charts = [p for p in (chart_total_by_condition(summary),
                          chart_effects(summary),
                          chart_task_heatmap(summary, records),
                          chart_completeness_hf(summary),
                          chart_time(summary)) if p]
    path = build_docx(summary, records, charts, interim)
    print(f"отчёт: {path}")
    print(f"диаграмм: {len(charts)} в {DIAG}")
    print(f"interim: {interim}")


if __name__ == "__main__":
    main()
