---
id: CMP-002
type: cmp
title: "Eval-наборы измерительного контура"
status: adopted
code_roots: ["datasets/eval_ood_clean.jsonl", "datasets/eval_ood_oxalpha.jsonl", "datasets/domain_eval_v2.txt", "datasets/domain_eval_v3.txt", "datasets/general_eval_v2.txt", "datasets/general_eval_v3.txt", "datasets/general_eval_k2.txt", "domain_eval_v2.txt", "general_eval_v2.txt", "data/gen-eval-v2-card.json", "data/gen-eval-v3-card.json", "data/gen-eval-k2-card.json"]
implements: [AD-7, AD-4]
depends_on: []
verified_by: [C-009, C-020]
---

- **Назначение**: измерительная сторона контура — наборы, на которых снимается ось и PPL стадий: OOD-набор ревизии `eval_ood_clean.jsonl` (192 задачи), общий язык (`general_eval_v2.txt`, `general_eval_v3.txt`, компонента K2 `general_eval_k2.txt`) и домен (`domain_eval_v2.txt`, `domain_eval_v3.txt` — из другого корпуса, ADR-044).
- **Где живёт**: файлы наборов — на сетевом диске `/home/user/gb10-shared/datasets` (в кейсе симлинки `datasets/`, `domain_eval_v2.txt`, `general_eval_v2.txt` — AD-4); карточки наборов с хешами и правилами пересборки — `data/gen-eval-v2-card.json`, `data/gen-eval-v3-card.json`, `data/gen-eval-k2-card.json`.
- **Чем проверяется**: C-009 (эталон не встречается в промпте своей задачи, пересечение eval ↔ SFT/RL по нормализованному тексту пусто), C-020 (в evidence PPL есть `leak_check` либо ссылка на каноническую проверку утечки набора в обучающие корпуса).
- **Связь**: AD-7 (состав eval-набора фиксируется конфигом и карточкой; аудит утечек механический), AD-4 (наборы не копируются в кейс). Решения: ADR-003 (заморозка набора ревизии), ADR-025 (чистота измерительного набора и потолок PPL), ADR-027 (двухкомпонентная мера), ADR-044 (домен-набор v3).
- **Границы**: `eval_ood_oxalpha` (102/3000 задач с эталоном в промпте) в ревизии не берётся — решением ADR-003 до пересборки с аудитом; сам файл остаётся на месте как исторический.
