---
id: CMP-012
type: cmp
title: "Пайплайн стадий и раннеры цепочки"
status: adopted
code_roots: ["laguna_pipeline_v8.py", "tools/pilot_chain.sh", "tools/run_pilot.py", "tools/run_full_cpt.py", "tools/run_mix_lr_calib.py", "tools/mix_lr_grid_chain.sh", "tools/launch_sft_stage.py", "tools/patch_pipeline_sft.py", "tools/run_smoke.py", "tools/run_rl_probe.py", "tools/run_det_probe.py", "tools/run_flex_check.py", "tools/run_mask_smoke.py", "tools/stop_stage_run.py", "tools/smoke_mem_sampler.sh"]
implements: [AD-2, AD-3, AD-6, AD-10, AD-12]
depends_on: []
verified_by: [C-013]
---

- **Назначение**: исполнительная часть контура — пайплайн `laguna_pipeline_v8.py` (стадии CPT / SFT / RL / eval, чат-формат и маскирование, запись манифеста, eval-путь с судьёй) и раннеры, которые стадии запускают: цепочка `pilot_chain.sh`, сетка калибровки `mix_lr_grid_chain.sh` (`run_mix_lr_calib.py`), полный CPT `run_full_cpt.py`, SFT-стадия `launch_sft_stage.py`, пробы и штатная остановка `stop_stage_run.py`.
- **Где живёт**: пайплайн — на сетевом диске `/home/user/gb10-shared/laguna_pipeline_v8.py` (в кейсе симлинк — AD-4); каждая стадия работает своей копией, снятой в её каталог прогона (`runs/<id>/laguna_pipeline_calib.py` + `pipeline_patch.json`), поэтому «чем именно обучали» называет манифест, а не текущий файл по ссылке. Раннеры — `tools/` кейса.
- **Чем проверяется**: C-013 (судья вызывается только из eval-пути пайплайна). Косвенно — C-012 (манифест прогона: обе версии, пайплайна и прогона) и C-008 (контракт токен-пространства, который пайплайн реализует). Прямого правила на раннеры нет намеренно: AD-10 запрещает гейту ссылаться на них, обратное направление (раннер вызывает стража перед стадией) разрешено и используется.
- **Связь**: AD-2 (манифест пишет пайплайн), AD-3 (токенизация, маскирование и чат-формат — один контракт у пайплайна, харнесса и eval), AD-6 (изоляция судьи от награды проходит по коду пайплайна), AD-10 (граница стража и раннера: раннер исполняется человеком или харнессом, гейтом не вызывается), AD-12 (финальный коммит и JSON-контракт дельты, запустившей стадию). Решения: ADR-019 (каталог `tools/`), ADR-032 (запуск SFT-стадии), ADR-016 (штатная остановка), ADR-036/ADR-037 (маскирование `<think>`), ADR-041 (штатный режим декодирования).
