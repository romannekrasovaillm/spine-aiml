# NOTICE — происхождение кодовой базы

Этот репозиторий — **Spine AI/ML Edition**: локальный форк **Spine Banking
Edition** (`~/spine-bank`) под новый доменный харнесс
AI/ML-исследователя.

Цепочка происхождения:

1. **Spine** (crate `arch-harness`, бинарь `arch`) — MIT, Copyright (c) 2026
   Roman Nekrasov. Публичный архив: github.com/romannekrasovaillm/spine.
2. **Spine Banking Edition** — продуктовый форк Spine (точка форка `b2088cf0`,
   2026-08-21, ADR-010), бинарь `arch-be`. Ядро MIT (`LICENSE`), зона
   `banking/` proprietary (`LICENSE.banking`).
3. **Spine AI/ML Edition** (этот репозиторий) — локальный форк Spine Banking
   Edition.

- **Источник форка:** `~/spine-bank`
- **Точка форка:** commit `b928684` («fix(benchmarks): raw dsp напрямую на
  api.deepseek.com …»), 2026-09-11
- **Стратегия форка:** чистая локальная копия — история git не наследуется,
  переносы улучшений из Spine BE — патчами осознанно (по образцу AD-BE3 /
  ADR-010). Якорь отката — baseline-коммит этой копии.
- **Лицензии:** ядро остаётся MIT (`LICENSE`). Зона `banking/` унаследована из
  Spine BE как **эталон толстого домена** (proprietary, `LICENSE.banking`) и
  подлежит замене на `aiml/` по мере адаптации. Новый доменный слой `aiml/` —
  лицензия фиксируется при наполнении (по умолчанию MIT-ядро, пока нет
  решения об open-core для AI/ML-надстройки).

Право на перелицензирование: Roman Nekrasov — единственный держатель копирайта
апстрима и форков.
