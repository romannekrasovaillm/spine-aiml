---
name: check-spine-aiml-docs
description: Справка по Spine AI/ML Edition (бинарь arch-ml) — доменному агент-харнессу AI/ML-исследователя. Используй этот навык, когда нужно ответить на вопрос о самом харнессе: какие доменные инструменты есть (concept_search, govern, accept, evolve, distill, skill_distill), какие fitness-правила (address_trace, skill_contract, playbook_graduation, managed_hash_current), какова модельная матрица, как устроен мост к библиотеке Ариадны. Отвечай по документации репозитория, не по памяти.
---

# Spine AI/ML Edition — справка по харнессу

Форк банковской линии Spine под домен AI/ML-исследователя (происхождение —
`NOTICE.md`). Бинарь `arch-ml`,
crate `arch-harness` (не переименован), изолированные пути `~/.arch-ml` /
`~/.config/arch-ml`.

## Доменные инструменты (AI/ML-редакция)

- `concept_search` — типизированный поиск по базе концептов Ариадны (186K):
  `op=resolve|lookup|neighbors|search|stats`; фасеты type/level/formality/family.
- `govern` — формальная подотчётность (H1.1): именной аппрувер + журнал решений.
- `accept` — независимый eval-бар (H1.2): человеко-заданные приёмочные тесты ДО генерации.
- `evolve` — guarded self-evolution (H2.2): propose/commit изменений доменного слоя.
- `distill` / `skill_distill` — дистилляция статья→digest→playbook→SKILL (md-vs-skill гейт).

## Fitness-правила домена (`CONSTRAINTS.yaml`)

- `address_trace` (ML-TRACE-1) — адресный след дистиллята резолвится.
- `skill_contract` (ML-SKILL-1) — структурный контракт SKILL.md.
- `playbook_graduation` (ML-PLAYBOOK-1) — правило трёх повторений.
- `managed_hash_current` (ML-MANAGED-1) — целостность managed-блоков.

## Модельная матрица

Внешние API (DeepSeek V4, GLM-5.2, Kimi K3) + локальные open-weights за
OpenAI-совместимым шлюзом + доменная модель концептов (Ариадна, локальная 4B).

## Мост библиотеки

`~/library` (Ариадна): `concept_search` поверх `index/concept_index.json`,
`knowledge.dirs` → distillate/pedagogy/concepts, доменные плагины `aiml/plugins/ml-*`.
