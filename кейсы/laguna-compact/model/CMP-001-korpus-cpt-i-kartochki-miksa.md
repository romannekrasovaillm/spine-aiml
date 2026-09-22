---
id: CMP-001
type: cmp
title: "Корпус CPT ревизии и карточки микса"
status: adopted
code_roots: ["cpt_corpus_v12r.txt", "datasets/cpt_corpus_v12r50.txt", "data/corpus-card.json", "data/corpus-card-v12r50.json", "data/mix-v12r50-build.json", "data/mix-ctrl100-build.json", "data/tok"]
implements: [AD-3, AD-4, AD-7, AD-8]
depends_on: []
verified_by: [C-008, C-010, C-011, C-016]
---

- **Назначение**: предмет обучения стадий ревизии — замороженный CPT-корпус `v12r` (ветка `qwen25`, 9776 чанков × 8192, домен 75.0 % / replay 25.0 %, seed шафла 42) и его ревизия `v12r50` (50 % replay, ADR-047); вместе с претокенизированными кэшами и карточками состава.
- **Где живёт**: сами тексты корпуса — на сетевом диске `/home/user/gb10-shared` (`cpt_corpus_v12r.txt`), в кейсе только симлинки (`cpt_corpus_v12r.txt`, `datasets/` — AD-4); кэши — `data/tok/`; состав и хеши — `data/corpus-card.json`, `data/corpus-card-v12r50.json`, `data/mix-v12r50-build.json`, `data/mix-ctrl100-build.json`.
- **Чем проверяется**: C-010 (карточка корпуса существует), C-016 (`license_status: internal_only`), C-011 (в кейсе только симлинки, копий нет), C-008 (токен-пространство кэша корпуса — 8 токенов в один id и вхождения 6 токенов формата v12).
- **Связь**: AD-7 (состав — версионируемый конфиг с карточкой и полным `sha256`; изменение — новым ADR), AD-4 (данные только симлинками на сетевой диск), AD-8 (внутренний лицензионный статус доменной части), AD-3 (кэш корпуса — носитель контракта спецтокенов). Решения: ADR-003, ADR-004, ADR-047.
- **Границы**: контрольный корпус C1 (100 % общий язык, `data/mix-ctrl100-build.json`) входит сюда же как контрольная рука того же контура; eval-наборы — отдельный компонент (CMP-002).
