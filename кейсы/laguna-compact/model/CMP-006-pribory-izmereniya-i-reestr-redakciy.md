---
id: CMP-006
type: cmp
title: "Приборы измерения и реестр их редакций"
status: adopted
code_roots: ["tools/ppl_probe.py", "tools/calib_ppl_probe.py", "tools/flex_ppl_probe.py", "tools/ppl_probe_k2.py", "tools/ppl_probe_v3.py", "tools/full_cpt_probe.py", "tools/passrate_probe.py", "tools/probe_language_split.py", "tools/probe_pool_v2_leak.py", "tools/probe_pool_v2_reachability.py", "tools/probe_control.py", "tools/probe_chain_projection.py", "tools/validate_verifiers.py", "tools/measure_corpus_residual.py", "tools/s3n_ppl_curve.py", "tools/calib_ppl_arms.py", "docs/specs/INSTRUMENT-VERSIONS.md", "evidence/instrument-hash-audit.json"]
implements: [AD-3, AD-7, AD-10]
depends_on: []
verified_by: [C-020, C-025]
---

- **Назначение**: чем в контуре получают числа — приборы замера PPL (`tools/ppl_probe.py` с методикой `_ppl_eval`, `tools/calib_ppl_probe.py`, `tools/ppl_probe_k2.py`, `tools/ppl_probe_v3.py`, `tools/full_cpt_probe.py`), проб поведения и языка (`tools/probe_language_split.py`, `tools/probe_control.py`), достижимости и лейка пула (`tools/probe_pool_v2_reachability.py`, `tools/probe_pool_v2_leak.py`), pass-rate (`tools/passrate_probe.py`) и валидации верификаторов (`tools/validate_verifiers.py`). Число без прибора, которым оно снято, в контуре не читается: у прибора есть редакция и хеш.
- **Где живёт**: код приборов — `tools/*.py` кейса; реестр «прибор ↔ редакция ↔ sha256» — `docs/specs/INSTRUMENT-VERSIONS.md` (носитель объявленного хеша), снимок аудита — `evidence/instrument-hash-audit.json`. Часть приборов объявляет хеш сама (`tool_sha256 = sha256_file(__file__)` внутри своего отчёта) и в реестр не подмешивается — граница названа в шапке реестра.
- **Чем проверяется**: C-025 (реестр сходится со снимком, деревом и git-историей; замороженные цитаты не переписаны), C-020 (`leak_check` в evidence PPL — чистота набора, которым прибор мерил).
- **Связь**: AD-3 (прибор использует тот же контракт токен-пространства и чат-формата, что обучение), AD-7 (измерение отделено от конфига набора), AD-10 (прибор — не страж и не раннер: граница по имени и по роли в гейте). Решения: ADR-023 п.10 (реестр редакций), ADR-027/ADR-025 (методика и чистота измерения), ADR-015 (измерение вне стенда).
