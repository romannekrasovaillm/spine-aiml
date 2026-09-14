# Скиллы лесенки Laguna на GB10

Набор из пяти скиллов для TUI-агентов (Claude Code, OpenClaw, Hermes Agent,
CodeWhale), выведенных из отчёта «Интерпретация эксперимента
v12_qwen25-15b_s42» (13.09.2026). Каждый скилл — каталог с `SKILL.md`
(фронтматтер name/description + инструкции), при необходимости
`references/` и `scripts/` (только стандартная библиотека Python 3).

| Скилл | Когда | Скрипты |
|---|---|---|
| `laguna-ladder-run` | запуск, статус, падения и резюмы прогона на GB10 | `run_status.py` |
| `ood-stage-eval` | сравнение BASE/CPT/SFT/RL на OOD: slug, судья, МакНемар, примеры | `judge_health.py`, `stage_compare.py`, `extract_examples.py` |
| `cpt-template-check` | диагностика template domination / language drift после CPT | `template_dominance.py` |
| `rl-health-check` | здоровье GRPO: энтропия, KL, clip, ppl_general, бюджет | `rl_health.py` |
| `ladder-interpretation-report` | написание интерпретационного отчёта по прогону | шаблон в `references/` |

Типовой маршрут после закрытия прогона:
`laguna-ladder-run` (полнота артефактов) → `ood-stage-eval` →
`cpt-template-check` → `rl-health-check` → `ladder-interpretation-report`.

## Установка

- **Claude Code**: скопировать каталоги в `~/.claude/skills/` (глобально)
  или `.claude/skills/` проекта; либо импортировать `.skill`-файлы из `dist/`.
- **OpenClaw / Hermes Agent / CodeWhale**: положить каталоги в директорию
  скиллов агента (см. его конфиг, обычно `skills/` рядом с workspace);
  агент читает `SKILL.md` по фронтматтеру.

Скрипты угадывают имена полей в `eval_results_*.json` и `rollouts_log.jsonl`;
если схема другая — у каждого есть `--inspect` и `--field ключ=имя`.
