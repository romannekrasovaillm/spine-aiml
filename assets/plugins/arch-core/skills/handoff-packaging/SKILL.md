---
name: handoff-packaging
description: Передача архитектуры кодовым агентам (Claude Code, Qwen Code, OpenClaw, Hermes, Theseus, CodeWhale) через handoff-пакет: компиляция epic-context 800–1500 токенов по смыслу, инварианты целиком, headless JSON-контракт результата complete/partial/blocked, контроль после прогона fitness-функциями и рубрикой. Используй этот навык ВСЕГДА при постановке задачи кодовому агенту/харнессу, при распараллеливании работ, при разборе «агент сделал не то».
---

# Handoff-пакеты

Агенты забывают, файлы — нет. Передача архитектуры в реализацию — через пакет файлов `.arch-handoff/`, а не через устную постановку (инструмент `handoff_create`, CLI `arch-ml handoff`).

## Состав пакета

- `TASK.md` — задача + контракт результата (см. ниже).
- `ARCHITECTURE.md` — epic-context: компиляция 800–1500 токенов **по смыслу, не по источнику**: spine-блоки и ADR-инварианты целиком, спеки — заголовки и ключевые секции. Без дословного копирования всего дерева.
- `CONSTRAINTS.yaml` — fitness-правила (навык `fitness-functions`); пользовательские правки не затираются при повторной генерации.
- `RUBRIC.yaml` — якорная рубрика приёмки.
- `adr/` — копии затронутых ADR. `MANIFEST.json` — мета (дата, источники, оценка токенов).

## Headless-контракт результата

Финальный ответ кодового агента ОБЯЗАН завершаться JSON:

```json
{"status": "complete|partial|blocked",
 "assumptions": ["..."],
 "open_questions": ["..."],
 "conflicts_with_prior_decisions": ["ADR-3: выбрал иной retry"]}
```

`conflicts_with_prior_decisions` — сигнал эскалации к архитектору, не молчаливое отклонение.

## Контур контроля (после прогона)

1. `arch-ml harness-run <harness> --repo <path>` — прогон (таймаут из конфига).
2. `arch-ml control check <repo>` — fitness functions по CONSTRAINTS.yaml (гейт A4; exit 1 при FAIL — в CI).
3. `arch-ml rubric run handoff_quality <итог.md>` — оценка результата рубрикой.
4. Инциденты/отклонения → новые сенсоры и правила (цикл обучения org/team/project).

## Антипаттерны

- Постановка «всю спеку в промпт» — деградация контекста (context rot), бюджет окна <50%.
- Handoff без инвариантов — агенты выбирают несовместимые решения.
- Приёмка без fitness-прогона — «на вид нормально».
