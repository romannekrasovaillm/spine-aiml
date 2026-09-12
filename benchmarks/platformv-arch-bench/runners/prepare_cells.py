#!/usr/bin/env python3
"""prepare_cells.py — создаёт все ячейки, work/, prompt.txt, спайн-пакеты.

Матрица (ЗАФИКСИРОВАНА, см. PREREGISTRATION.md):
  основной массив: 8 задач × условия × 2 повтора (r1, r2):
    dsf (deepseek-flash): spine-arch, theseus-plain, theseus-arch,
                             claude-plain, claude-arch, raw-llm
    glm (glm-5.3-flash):     spine-arch, theseus-plain, theseus-arch, raw-llm
  расширенный свип: 2 задачи (PGL-ARCH-001, CRX-ARCH-001) × 5 харнессов
    (dsh, codewhale, hermes, openclaw, kimi) × 1 повтор, cond=<harness>-plain.

Ячейка: cells/<TASK>__<COND>__<MODEL>__r<N>/{prompt.txt, work/, ...}.
Идемпотентно: существующие файлы не перезаписываются.
Только stdlib.
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pvlib

BASE = pvlib.BASE
CELLS = pvlib.CELLS_DIR
SPINE = BASE / "spine"
CUSTOM = BASE / "customization" / "architect.md"

MAIN_CONDITIONS = {
    "dsf":   ["spine-arch", "spine-min", "theseus-plain", "theseus-arch",
              "claude-plain", "claude-arch", "raw-llm"],
    "glm":   ["spine-arch", "spine-min", "theseus-plain", "theseus-arch",
              "raw-llm"],
    "glm53": ["spine-arch", "theseus-plain", "raw-llm"],
    "dsp":   ["spine-arch", "claude-plain", "raw-llm"],
}
REPS = {"dsf": 2, "glm": 2, "glm53": 1, "dsp": 1}
SWEEP_TASKS = ["PGL-ARCH-001", "CRX-ARCH-001"]
SWEEP_HARNESSES = ["dsh", "codewhale", "hermes", "openclaw", "kimi"]

WRAPPER = (
    "Ты — ведущий архитектор решений в ДКА банка. Прочитай TASK.md и "
    "CONTEXT.md ниже и подготовь архитектурную записку строго по требуемой "
    "структуре. Ответ — только итоговый документ на русском.\n\n"
)
SPINE_LINE = (
    "В рабочем каталоге есть ARCHITECTURE-SPINE.md и CONSTRAINTS.yaml — "
    "соблюдай их; перед сдачей проверь ответ гейтом "
    "`arch-be control check . --constraints CONSTRAINTS.yaml` если доступен.\n\n"
)


def gen_spine_pack(task):
    """Генерирует spine/<TASK>/{ARCHITECTURE-SPINE.md, CONSTRAINTS.yaml}."""
    files = pvlib.load_task_files(task)
    product = ""
    for line in files["RUBRICS"].splitlines():
        if line.startswith("product:"):
            product = line.split(":", 1)[1].strip()
            break
    dels = pvlib.extract_deliverables(files["TASK"])
    hfs = pvlib.extract_hard_fails(files["RUBRICS"])
    mech, mech_src = pvlib.load_mech(task)

    lines = [
        f"# ARCHITECTURE-SPINE — {task}",
        "",
        f"Продукт: {product}",
        "",
        "## Инварианты (нарушать нельзя)",
        "",
        "**I-1. Обязательные артефакты.** Ответ ОБЯЗАН содержать все разделы "
        f"D1–D{len(dels)} с заголовками, в порядке из TASK.md:",
        "",
    ]
    lines += [f"- **{did}** — {dtext}" for did, dtext in dels]
    lines += [
        "",
        "**I-2. Фактология.** Единственный источник фактов о продукте — "
        "CONTEXT.md §1 (`[DOC]`). Всё за пределами §1 — только как допущение "
        "с явной пометкой «Допущение: …» или как вопрос к вендору с планом "
        "проверки. Проверка: каждое утверждение о возможностях продукта "
        "сверено с §1.",
        "",
        "**I-3. Наименования.** Используются только официальные наименования "
        "продуктов и компонентов Platform V из CONTEXT.md, дословно. "
        "Вымышленные или переименованные компоненты запрещены. "
        "Проверка: каждое имя компонента встречается в CONTEXT.md §1.",
        "",
        "**I-4. Трассировка.** Каждое требование NFR/SEC/INF из TASK.md "
        "покрыто решением в разделе трассировки (статус: выполнено / "
        "с оговоркой / требует проверки у вендора). Проверка: все ID "
        "NFR-xx, SEC-xx, INF-xx присутствуют в таблице трассировки.",
        "",
        "**I-5. Числа.** Голословные числа запрещены: каждая ключевая цифра "
        "выведена расчётом из исходных данных или подтверждена ссылкой на "
        "§1. Проверка: у каждой метрики есть расчёт или ссылка.",
        "",
        "**I-6. Hard-fail табу (нарушение ограничивает итог 39 баллами):**",
        "",
    ]
    lines += [f"- **{hid}**: {htext}" for hid, htext in hfs]
    lines += [
        "",
        f"Лимит объёма: ≤ {mech['max_words'] or 4000} слов; резюме ≤ 15 строк.",
        "",
    ]
    spine_md = "\n".join(lines)

    # CONSTRAINTS.yaml (формат arch-be control): must_contain по deliverables
    cy = [
        f"# CONSTRAINTS.yaml — fitness-правила для {task} (генерируется prepare_cells.py).",
        f"# Источник: mechanical_checks из RUBRICS.md ({mech_src}).",
        "# Проверка: arch-be control check . --constraints CONSTRAINTS.yaml",
        "constraints:",
    ]
    for i, e in enumerate(mech["deliverables"], 1):
        pat = e["pattern"].replace("\\", "\\\\").replace('"', '\\"')
        cy += [
            f"  - id: SP-{task}-{i:02d}",
            f"    name: deliverable-{e['id'].lower()}",
            "    type: must_contain",
            '    glob: "answer.md"',
            f'    pattern: "{pat}"',
            "    severity: high",
            "",
        ]
    if mech.get("max_words"):
        # отдельного типа max_words в arch-be control нет — через command_succeeds
        cy += [
            f"  - id: SP-{task}-99",
            "    name: max-words",
            "    type: command_succeeds",
            f'    command: "bash -c \\"test $(wc -w < answer.md 2>/dev/null || echo 999999) -le {mech["max_words"]}\\""',
            "    severity: medium",
            "",
        ]
    return spine_md, "\n".join(cy)


def build_prompt(task, cond):
    files = pvlib.load_task_files(task)
    p = WRAPPER
    if cond == "spine-arch":
        p += SPINE_LINE
    p += ("# TASK.md\n\n" + files["TASK"].strip() + "\n\n"
          "# CONTEXT.md\n\n" + files["CONTEXT"].strip() + "\n")
    return p


def prepare_cell(task, cond, model, rep):
    name = f"{task}__{cond}__{model}__r{rep}"
    cell = CELLS / name
    work = cell / "work"
    work.mkdir(parents=True, exist_ok=True)
    prompt_p = cell / "prompt.txt"
    if not prompt_p.is_file():
        prompt_p.write_text(build_prompt(task, cond), encoding="utf-8")
    # копии TASK/CONTEXT в work/ для агентных условий (не для raw-llm)
    if cond != "raw-llm":
        for f in ("TASK.md", "CONTEXT.md"):
            dst = work / f
            if not dst.is_file():
                shutil.copy2(pvlib.TASKS_DIR / task / f, dst)
    if cond == "spine-arch":
        for f in ("ARCHITECTURE-SPINE.md", "CONSTRAINTS.yaml"):
            dst = work / f
            if not dst.is_file():
                shutil.copy2(SPINE / task / f, dst)
    elif cond == "theseus-arch":
        dst = work / "AGENTS.md"
        if not dst.is_file():
            shutil.copy2(CUSTOM, dst)
    elif cond == "claude-arch":
        dst = work / "CLAUDE.md"
        if not dst.is_file():
            shutil.copy2(CUSTOM, dst)
    return name


def main():
    # Фильтры через env: подмножества для регрессионных/быстрых прогонов.
    tasks_filter = set(os.environ.get("PVBENCH_TASKS", "").split()) or None
    conds_filter = set(os.environ.get("PVBENCH_CONDITIONS", "").split()) or None
    models_filter = set(os.environ.get("PVBENCH_MODELS", "").split()) or None
    reps_override = os.environ.get("PVBENCH_REPS")
    no_sweep = os.environ.get("PVBENCH_NO_SWEEP") == "1"

    created_spine = 0
    tasks = []
    for task in pvlib.list_tasks():
        if not (pvlib.TASKS_DIR / task / "RUBRICS.md").is_file():
            print(f"ПРОПУСК {task}: неполный комплект (нет RUBRICS.md)")
            continue
        if tasks_filter and task not in tasks_filter:
            continue
        tasks.append(task)
    for task in tasks:
        sd = SPINE / task
        sd.mkdir(parents=True, exist_ok=True)
        spine_md, constraints = gen_spine_pack(task)
        for fname, content in (("ARCHITECTURE-SPINE.md", spine_md),
                               ("CONSTRAINTS.yaml", constraints)):
            p = sd / fname
            if not p.is_file():
                p.write_text(content, encoding="utf-8")
                created_spine += 1

    cells = []
    for task in tasks:
        for model, conds in MAIN_CONDITIONS.items():
            if models_filter and model not in models_filter:
                continue
            for cond in conds:
                if conds_filter and cond not in conds_filter:
                    continue
                reps = int(reps_override) if reps_override else REPS[model]
                for rep in range(1, reps + 1):
                    cells.append(prepare_cell(task, cond, model, rep))
    if not no_sweep:
        for task in SWEEP_TASKS:
            if tasks_filter and task not in tasks_filter:
                continue
            for harness in SWEEP_HARNESSES:
                cells.append(prepare_cell(task, f"{harness}-plain", "default", 1))

    print(f"задач в матрице: {len(tasks)}")
    print(f"спайн-пакетов создано файлов: {created_spine}")
    print(f"ячеек подготовлено: {len(cells)}")


if __name__ == "__main__":
    main()
