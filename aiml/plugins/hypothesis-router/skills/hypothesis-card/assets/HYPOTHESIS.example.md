---
id: laguna-gb10-ladder
title: Лесенка маленьких моделей на GB10 (CPT → SFT → RL)
state: latent
created: 2026-09-13
last_touched: 2026-09-13
source: "Техотчёт лабы: Интерпретация эксперимента v12_qwen25-15b_s42, 13.09.2026"
source_path: ~/reports/Отчёт_v12_15b_s42_CPT-SFT-RL_OOD_интерпретация_13-09-2026.docx
owner: me
triggers:
  files: ["rollouts_log.jsonl", "eval_results_*.json", "*_checkpoint_*.pt", "*_ladder_status.log"]
  keys: ["LAGUNA_MEM_FRACTION", "vllm_gpu_memory_utilization", "max_len"]
  deps: ["vllm", "flex-attn", "trl"]
  words: ["GB10", "DGX Spark", "UMA", "CPT", "SFT", "GRPO", "лесенка", "Laguna", "Qwen2.5-1.5B", "Qwen2.5-3B"]
skills: [laguna-ladder-run, ood-stage-eval, cpt-template-check, rl-health-check, ladder-interpretation-report]
plugins: [laguna-gb10-skills]
projects: []
related: []
promote_when: "есть доступ к боксу GB10/Spark и задача дообучить модель ≤3B под узкий домен с tool-call'ами"
decay_months: 6
---

## Намерение
Уметь прогонять и интерпретировать полный цикл дообучения маленькой модели
(CPT → SFT → RL) на одной UMA-машине, зная, где рецепт ломается.

## Почему это может пригодиться
Локальные модели под доменные харнессы (поиск по концептам, tool-call'ы)
дешевле API, но требуют дисциплины прогона: падения — норма, судья
флапает, CPT может обвалить сильную базу. В библиотеке нет ничего про
GB10 и про патологию template domination.

## Что сделало бы это проектом
Появление задачи «своя модель для доменного харнесса» + доступ к Spark.
Тогда первый шаг — eval_base для 3B и аблация дозы CPT.

## Что выведено
- laguna-ladder-run — запуск/статус/восстановление прогона
- ood-stage-eval — сравнение стадий с МакНемаром и проверкой судьи
- cpt-template-check — input attribution, leak rate, перекос корпуса
- rl-health-check — пороги энтропии/KL/ppl_general, профиль RL
- ladder-interpretation-report — шаблон отчёта

## Открытые вопросы
- Вылечит ли SFT обвал 3B после CPT (в отчёте прогон 3B ещё шёл)?
- Схемы eval_results_*.json / rollouts_log.jsonl — скрипты угадывают поля.
- Переносится ли рецепт на не-Qwen базы?

## Журнал
- 2026-09-13 — заведена из отчёта v12; выведено 5 скиллов, архив laguna-gb10-skills.zip.
