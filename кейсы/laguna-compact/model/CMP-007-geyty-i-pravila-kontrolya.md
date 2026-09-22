---
id: CMP-007
type: cmp
title: "Гейты и правила контроля"
status: adopted
code_roots: ["CONSTRAINTS.yaml", "tools/check_special_tokens.py", "tools/check_eval_leakage.py", "tools/check_eval_set_purity.py", "tools/check_sft_rl_overlap.py", "tools/check_sft_agentic_share.py", "tools/check_measurement_overlap.py", "tools/check_symlink_hygiene.sh", "tools/check_run_manifest.py", "tools/check_resource_owner.sh", "tools/check_gb10_serialization.sh", "tools/check_instrument_versions.py", "tools/check_handoff_contract.py", "tools/check_judge_isolation.py", "tools/check_grounded_pool.py", "tools/tests/run_tool_tests.sh"]
implements: [AD-5, AD-9, AD-10]
depends_on: []
verified_by: [C-018, C-019]
---

- **Назначение**: механическая часть контроля — правила `CONSTRAINTS.yaml` (C-001…C-020, C-023…C-027) и стражи `tools/check_*`, которые они вызывают: токен-пространство, утечки eval и чистота набора, пересечение пулов, гигиена симлинков, манифест прогона, хозяин и сериализация стенда, реестр редакций приборов, JSON-контракт дельты, изоляция судьи, страж заземления (C-026/C-027, ADR-049 п.5/п.11). Каждое правило несёт владельца смысла, тип (`kind`) и evidence-путь.
- **Где живёт**: `CONSTRAINTS.yaml` в корне кейса (реестр правил), стражи — `tools/check_*.py|sh`, регрессия инструментов — `tools/tests/run_tool_tests.sh` (тест гейтом не является — ADR-023 п.10).
- **Чем проверяется**: C-019 (правило не вызывает раннеры и билдеры — граница стража и раннера держится механически), C-018 (страж хозяина стенда отвечает CAN-START: флаг паузы читается контуром лесенки, память по стадии, ≤1 нагрузки). Сами стражи прогоняются `fitness_check`/`control check`; живое состояние гейта читается прогоном, а не текстом README.
- **Связь**: AD-10 (граница стража и раннера проходит по имени и проверяется), AD-9 (хозяин ресурса объявлен, а не выясняется гонкой), AD-5 (сериализация нагрузок на GB10). Решения: ADR-019 (каталог `tools/`), ADR-023 (практика handoff), ADR-012/ADR-008 (арбитраж и бюджет ресурса).
- **Границы**: правила, снятые с учёта или предложенные (`C-021` — предложение AD-12, `C-022` — страж agentic-доли), в реестр не внесены; их стражи лежат в `tools/`, но гейтом не вызываются.
