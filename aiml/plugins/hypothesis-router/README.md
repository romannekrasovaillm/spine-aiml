# hypothesis-router — плагин Spine

Память о намерении с жизненным циклом + роутинг по фактам проекта.

```
plugin.json                      манифест: скилл, хуки, fitness, команда, env
hooks/hypothesis-hook.sh         intent | pre-handoff | post-accept | refresh | check
hooks/hypothesis_hook.py         реализация (stdlib, без LLM)
                                  (правила — в aiml/library/fitness/hypothesis-routing.yaml,
                                   схема движка Spine; в плагине не дублируются)
commands/hypotheses.md           слэш-команда /hypotheses
skills/hypothesis-card/          скилл карточек (SKILL.md, схема, пример, скрипты)
tests/                           pytest: отбор подъёма (norm, top-N, диверсификация)
docs/routing-topn.delta.md       дельта отбора: norm, --max-cards, --dedupe
docs/ADR-draft-*.md              ADR-черновик для evolve propose
docs/spine-integration.md        подключение с правкой ядра и без
```
Быстрая проверка: `hooks/hypothesis-hook.sh intent "поднять RL для 3B на Spark" ~/repo`.
Тесты: `python3 -m pytest aiml/plugins/hypothesis-router/tests/ -q`.
