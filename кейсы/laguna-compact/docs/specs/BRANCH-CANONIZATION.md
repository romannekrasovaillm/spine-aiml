# BRANCH-CANONIZATION.md — карта расхождений между ветвями кейса и план сведения

- Date: 2026-09-16 (раздел 8 добавлен 2026-09-17)
- Статус: **ВЫПОЛНЕНО (S3z, 17.09.2026)** — ветка канона `arch/laguna-canon`, журнал
  `evidence/s3z-canonization.json`. Владелец разрешил сведение; план ниже сохранён
  как есть (аудит не переписывается, ADR-023 п.9)
- Ветка аудита: `arch/laguna-fleet-2` (точка сведения)
- Основание: **ADR-028** пп. 1–3 (факт первичен; два поля вместо `arch_base_version`;
  нумерация ADR) и п. 8 (расхождения чинятся **при сведении**, чужие ветви не правятся)
- Числа и карта: `evidence/fleet-branch-divergence.json` (машиночитаемо, тот же прогон)

## 0. Инварианты этого аудита

| Инвариант | Как соблюдён |
|---|---|
| Чужие ветви не менять | Только чтение: `git ls-tree`, `git cat-file`, `git show`, `git log --all`. Ни одного `checkout`, `merge`, `commit` в чужой ветке |
| GPU/стенд не занимать | Стенд GB10 занят полным CPT (`tmux s3u-full-cpt-20260916-1933`). Аудит не запускал ни одной стадии, ни одного контейнера |
| Копий данных не делать | Ничего не копировалось; тяжёлые `.npy` не читались, сверка шла по blob-хешам git |
| Правки — только в своей ветке | Созданы ровно два артефакта: этот файл и `evidence/fleet-branch-divergence.json` |

## 1. Что именно сравнивалось

Восемь ветвей несут каталог кейса `кейсы/laguna-compact/`: `arch/laguna-control-arms`,
`arch/laguna-eval-set`, `arch/laguna-mix-lr-calibration`, `arch/laguna-protocol-diag`,
`arch/laguna-fleet-analysis`, `arch/laguna-replay50`, `arch/laguna-passrate-probe`,
`arch/laguna-fleet-2`. Читалось по tip-коммитам:

| Ветвь | Tip | Файлов в каталоге кейса |
|---|---|---|
| `arch/laguna-control-arms` | `e7d2d89` | 295 |
| `arch/laguna-eval-set` | `949a5b9` | 259 |
| `arch/laguna-mix-lr-calibration` | `17945e5` | 220 |
| `arch/laguna-protocol-diag` | `6d2aa3c` | 248 |
| `arch/laguna-fleet-analysis` | `0ec9cf4` | 236 |
| `arch/laguna-replay50` | `c6e9374` | 214 |
| `arch/laguna-passrate-probe` | `e1b07a2` | 142 |
| `arch/laguna-fleet-2` | `47f921f` | 301 |

Ветви читались по ссылкам refs/heads на момент аудита (tip зафиксирован выше). Две ветви — arch/laguna-mix-lr-calibration и arch/laguna-protocol-diag — получили по одному новому коммиту ВО ВРЕМЯ аудита (17945e5, 6d2aa3c); оба коммита добавляют только новые файлы (docs/specs/SFT-STAGE-PLAN.md, evidence/fleet-sft-prep.json, docs/specs/DISK-PLAN.md, evidence/fleet-disk-audit.json) и ни одного файла из карты расхождений не трогают, поэтому карта от них не зависит.

Файл считается **разошедшимся**, если множество его blob-хешей
среди ветвей, где он присутствует, имеет мощность больше единицы (симлинки и
`datasets/`, `data/tok/`, `*.npy` из сравнения исключены — AD-4).

| Показатель | Число |
|---|---|
| Файлов в объединении по восьми ветвям | **385** |
| Тождественны во всех восьми ветвях | 124 |
| Тождественны там, где присутствуют (есть не везде) | 154 |
| **Разошлись содержательно** | **27** |
| Есть только в одной ветви (уникальные) | 80 |

Читать это надо так: **основная масса кейса едина** (278 файлов из 385 тождественны),
расхождений ровно 27 имён — они и перечислены ниже. 80 файлов уникальны для одной
ветви: это результаты её дельт (evidence-файлы, каталоги прогонов, инструменты),
они не «расхождение», а вклад ветви. Два таких файла появились уже во время аудита
(коммиты соседних ветвей 17945e5 и 6d2aa3c) — карта от них не зависит, они новые.

### 1.1 Классификация — пять классов вместо трёх

Запрошенные три класса уточнены до пяти: два дополнительных нужны, чтобы «разные
редакции одного решения» и «одно имя — два разных артефакта» не сваливались в «разные
решения» — цена сведения у них разная.

| Класс | Что это | Цена сведения |
|---|---|---|
| `stale_snapshot` | устаревший снимок той же линии; одна сторона строго новее | низкая (fast-forward по содержанию) |
| `additive_annotation` | одно решение + дополнения, сделанные в разных ветвях; нужны обе стороны | средняя (объединение, даёт конфликт в diff) |
| `field_value_conflict` | **разные значения одного поля** (запрошенный класс) | средняя (пересборка из носителя факта) |
| `different_decision` | разные решения по существу | высокая — **в этом наборе не встретился ни разу** |
| `artifact_name_conflict` | одно имя несут несовместимые артефакты | низкая, но требует решения о имени |
| *(справочно)* `identical_copy` | идемпотентная копия (запрошенный класс) | нулевая — в список расхождений не попадает по построению |

Распределение по классам (это и есть ответ на вопрос «сколько чего»):

| Класс | Файлов |
|---|---|
| устаревший снимок (`stale_snapshot`) | 12 |
| дополнения по ветвям (`additive_annotation`) | 8 |
| разные значения поля (`field_value_conflict`) | 5 |
| конфликт имени артефакта (`artifact_name_conflict`) | 2 |
| **Итого** | **27** |

Отсюда главный вывод, который видно только из таблицы: **класс «разные решения» не
реализовался ни на одном файле.** Из 27 расхождений 12 — отставание одной ветви,
8 — дополнения, сделанные в разных ветвях, 5 — разные значения поля
в манифестах прогонов и 2 — конфликт имени артефакта. То есть сведение ветвей
laguna-compact — задача **не арбитража между несовместимыми курсами**, а аккуратного
объединения с починкой двух классов дефектов (значения манифестов и два имени).

## 2. Карта расхождений: 27 имён

Сокращения ветвей: `mix-lr` = `arch/laguna-mix-lr-calibration`. Столбец «вариантов» —
число различных blob-хешей; в скобках — какие ветви какой вариант несут.

| Файл | Класс | Вар. | Размеры (Б) | Кто несёт | Канон (предложение) |
|---|---|---|---|---|---|
| `tools/tests/run_tool_tests.sh` | дополнения по ветвям | 7 | 304085/354833/297507/287150/275273/211304/309043 | control-arms; eval-set; mix-lr; protocol-diag; fleet-analysis/replay50; passrate-probe; fleet-2 | `UNION(SECTIONS)` |
| `README.md` | устаревший снимок | 6 | 24636/35810/32141/24486/23420/13806 | control-arms/fleet-2; eval-set; mix-lr; protocol-diag; fleet-analysis/replay50; passrate-probe | `MERGE(2e70b76e8af6c806077c6318ffa2f6aeaeaf04fe base + разделы ветвей)` |
| `runs/calib-25-0.35-20260916-0820/run_manifest.json` | разные значения поля | 4 | 1507/1659/1662/1507 | control-arms/fleet-2; eval-set/mix-lr; protocol-diag; fleet-analysis | `PENDING-OWNER` |
| `runs/calib-25-0.7-20260916-0820/run_manifest.json` | разные значения поля | 4 | 1505/1657/1660/1505 | control-arms/fleet-2; eval-set/mix-lr; protocol-diag; fleet-analysis | `PENDING-OWNER` |
| `runs/calib-50-0.35-20260916-0820/run_manifest.json` | разные значения поля | 4 | 1511/1663/1511/1511 | control-arms/fleet-2; eval-set/mix-lr; protocol-diag; fleet-analysis | `PENDING-OWNER` |
| `runs/calib-50-0.7-20260916-0820/run_manifest.json` | разные значения поля | 4 | 1509/1661/1509/1509 | control-arms/fleet-2; eval-set/mix-lr; protocol-diag; fleet-analysis | `PENDING-OWNER` |
| `docs/adr/ADR-021-sreda-revizii-v2-podklyuchenie-sred-e1-e8-i-env-types-s-filtrom-po-verificiruemosti.md` | дополнения по ветвям | 3 | 26561/30684/18831 | control-arms/eval-set/protocol-diag/fleet-analysis/replay50/fleet-2; mix-lr; passrate-probe | `MERGE(bddfdcacceb05a1cf8637bc98dbd82336d4ef8dc + ce1ede069703c158972b9c7f172e443eabd349b6)` |
| `runs/replay50-collapse-20260916/run_manifest.json` | разные значения поля | 3 | 3686/3488/3812 | control-arms/fleet-2; protocol-diag; fleet-analysis | `PENDING-OWNER` |
| `tools/README.md` | устаревший снимок | 3 | 59062/60616/51028 | control-arms/protocol-diag/fleet-analysis/replay50/fleet-2; eval-set/mix-lr; passrate-probe | `MERGE(afc4b640d7b35b3e9c848993025825d6e24bdc3d + 55787250017860a521deaf7a1ec877ce07ddfc71)` |
| `ARCHITECTURE-SPINE.md` | устаревший снимок | 2 | 23653/20836 | control-arms/eval-set/mix-lr/protocol-diag/fleet-analysis/replay50/fleet-2; passrate-probe | `09ea27e1efb5` |
| `CONSTRAINTS.yaml` | устаревший снимок | 2 | 13106/14876 | control-arms/eval-set/protocol-diag/fleet-analysis/replay50/passrate-probe/fleet-2; mix-lr | `4891d221c46e` |
| `data/rev-envs-v2-card.json` | устаревший снимок | 2 | 13906/41426 | control-arms/eval-set/protocol-diag/fleet-analysis/replay50/passrate-probe/fleet-2; mix-lr | `67829db26492` |
| `docs/adr/ADR-015-promezhutochnye-chek-poynty-pilota-gen-eval-kak-ranniy-signal-degradacii.md` | устаревший снимок | 2 | 14215/11561 | control-arms/eval-set/mix-lr/protocol-diag/fleet-analysis/replay50/fleet-2; passrate-probe | `5fd74b942872` |
| `docs/adr/ADR-018-rasshirenie-naborov-gen-eval-novye-fayly-v2-s-filtrom-peresecheniya-s-obucheniem.md` | дополнения по ветвям | 2 | 15194/16933 | control-arms/eval-set/protocol-diag/fleet-analysis/replay50/passrate-probe/fleet-2; mix-lr | `MERGE(2df62edce2f7cd6f5e3ff8f372a3add2c011a6fc + 4c09a830406373533dc474a4a379fd0102336f5a)` |
| `docs/adr/ADR-022-perezapusk-cpt-snachala-diagnostika-prichiny-forgettinga-flex-maska-protiv-miksa-i-lr.md` | дополнения по ветвям | 2 | 10015/11670 | control-arms/mix-lr/protocol-diag/fleet-analysis/replay50/fleet-2; eval-set | `e32a9d0df14c` |
| `docs/adr/ADR-023-praktika-handoff-chek-list-zavershyonnosti-razmer-delty-zapret-fonovyh-ozhidaniy.md` | дополнения по ветвям | 2 | 10827/16368 | control-arms/eval-set/protocol-diag/fleet-analysis/replay50/fleet-2; mix-lr | `ac67f793a343` |
| `docs/adr/ADR-024-sostav-cpt-miksa-v12r50-dolya-replay-50-kak-kalibrovochnyy-miks-s3m.md` | дополнения по ветвям | 2 | 10522/11184 | control-arms/eval-set/protocol-diag/fleet-analysis/replay50/fleet-2; mix-lr | `9d078c2dbf9f` |
| `evidence/s3m-ppl-arms.json` | дополнения по ветвям | 2 | 35697/35755 | eval-set; mix-lr | `b081cc6fca94` |
| `evidence/s3s-hygiene.json` | конфликт имени артефакта | 2 | 12543/25742 | control-arms/fleet-2; fleet-analysis | `SPLIT(c1b71108965edf4160f3e6dd4a726be949f27ced ; c26463c7932e05297c1ccbac6fb52b2ca7e67068)` |
| `runs/calib-ppl-20260916-0820/NOT_A_RUN` | дополнения по ветвям | 2 | 781/787 | control-arms/protocol-diag/fleet-2; fleet-analysis | `99c7d828007f` |
| `runs/replay50-collapse-20260916/compare_text.json` | конфликт имени артефакта | 2 | 16309/145 | control-arms/protocol-diag/fleet-analysis/fleet-2; replay50 | `b4dba60af549` |
| `runs/rev-pool-v2/exclusions.json` | устаревший снимок | 2 | 48843/114605 | control-arms/eval-set/protocol-diag/fleet-analysis/replay50/passrate-probe/fleet-2; mix-lr | `74fa04a2ebd9` |
| `tools/mix_lr_grid_chain.sh` | устаревший снимок | 2 | 10507/8762 | control-arms/fleet-2; eval-set/mix-lr/protocol-diag/fleet-analysis/replay50 | `e945eddd8485` |
| `tools/pilot_chain.sh` | устаревший снимок | 2 | 42320/39724 | control-arms/eval-set/mix-lr/protocol-diag/fleet-analysis/replay50/fleet-2; passrate-probe | `d7cd13ec98e3` |
| `tools/ppl_probe.py` | устаревший снимок | 2 | 83623/82355 | control-arms/eval-set/fleet-2; mix-lr/protocol-diag/fleet-analysis/replay50 | `8ec80ebaa4bc` |
| `tools/run_mix_lr_calib.py` | устаревший снимок | 2 | 148565/116314 | control-arms/fleet-2; eval-set/mix-lr/protocol-diag/fleet-analysis/replay50 | `96a8b3532748` |
| `tools/write_run_manifest.py` | устаревший снимок | 2 | 15043/14616 | control-arms/fleet-2; eval-set/mix-lr/protocol-diag/fleet-analysis/replay50/passrate-probe | `58bdce0fcfb1` |

Ниже — то, что в таблицу не влезает: причина и канон по каждому имени.

### `ARCHITECTURE-SPINE.md`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `09ea27e1efb59ca701a84ded8a4b71151bb38a81`
- **Почему:** passrate-probe несёт снимок спайна без раздела AD-12 (7 строк); остальные семь ветвей совпадают побайтово.

### `CONSTRAINTS.yaml`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `4891d221c46e06b04fc3145ce9230b576ba25a0f`
- **Почему:** mix-lr-calibration несёт 20 правил (C-001…C-020), семь остальных — 19 (без C-020 «leak_check в evidence PPL», ADR-025 п.2). Вариант mix-lr — надмножество; файлы различны, набор правил строго вложен.

### `README.md`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 6
- **Канон:** `MERGE(2e70b76e8af6c806077c6318ffa2f6aeaeaf04fe base + разделы ветвей)`
- **Почему:** Шесть редакций одной линии: каждая ветвь дописала свои стадии в карту evidence и в список инструментов. passrate-probe — самый старый снимок (счётчик «19 ADR, 19 правил, 19 инструментов, 7 evidence»). Канон — сведение: база fleet-2/control-arms + разделы ветвей.

### `data/rev-envs-v2-card.json`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `67829db26492e602c44cd17bda53fff3f3729b6a`
- **Почему:** mix-lr-calibration несёт пересобранную карточку с полем prompt_leak (E5: 6 исключённых из 216, E6: 30 из 700, chain_projection по 500 задач); в остальных семи — 13.9 КБ без этих полей. Вариант mix-lr — надмножество факта.

### `docs/adr/ADR-015-promezhutochnye-chek-poynty-pilota-gen-eval-kak-ranniy-signal-degradacii.md`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `5fd74b9428721fe4c70e0b8d3d48c4c45f57b09d`
- **Почему:** Версия в passrate-probe (11.5 КБ) — ранняя редакция; семь ветвей совпадают (14.2 КБ). Выбор направления не проверялся содержательно, только по ветвлению.

### `docs/adr/ADR-018-rasshirenie-naborov-gen-eval-novye-fayly-v2-s-filtrom-peresecheniya-s-obucheniem.md`

- **Класс:** дополнения по ветвям (`additive_annotation`)
- **Вариантов:** 2
- **Канон:** `MERGE(2df62edce2f7cd6f5e3ff8f372a3add2c011a6fc + 4c09a830406373533dc474a4a379fd0102336f5a)`
- **Почему:** mix-lr-calibration несёт дополнения (16.9 КБ против 15.2 КБ в семи): ADR-025 вырос из этого решения. Канон — сведение, не выбор одной стороны.

### `docs/adr/ADR-021-sreda-revizii-v2-podklyuchenie-sred-e1-e8-i-env-types-s-filtrom-po-verificiruemosti.md`

- **Класс:** дополнения по ветвям (`additive_annotation`)
- **Вариантов:** 3
- **Канон:** `MERGE(bddfdcacceb05a1cf8637bc98dbd82336d4ef8dc + ce1ede069703c158972b9c7f172e443eabd349b6)`
- **Почему:** Три редакции: passrate-probe — ранняя (18.8 КБ), шесть ветвей — базовая (26.6 КБ), mix-lr-calibration — с дополнениями (30.7 КБ). Канон — база + дополнения mix-lr.

### `docs/adr/ADR-022-perezapusk-cpt-snachala-diagnostika-prichiny-forgettinga-flex-maska-protiv-miksa-i-lr.md`

- **Класс:** дополнения по ветвям (`additive_annotation`)
- **Вариантов:** 2
- **Канон:** `e32a9d0df14c0518a0d23c834e787d16050c1b0f`
- **Почему:** eval-set добавил к шапке баннер «ОТОЗВАНО ПО ОСНОВАНИЮ — ADR-029» (+11 строк); тело решения совпадает. Канон — редакция с баннером: без неё ADR-029 не виден из ADR-022.

### `docs/adr/ADR-023-praktika-handoff-chek-list-zavershyonnosti-razmer-delty-zapret-fonovyh-ozhidaniy.md`

- **Класс:** дополнения по ветвям (`additive_annotation`)
- **Вариантов:** 2
- **Канон:** `ac67f793a34314f620d345f525db6eb14479bac0`
- **Почему:** mix-lr-calibration добавил пункты 8–11 (+20 строк: self-check своего коммита, аудиторский след не переписывается, один носитель чисел, долгоживущий процесс = handoff). Канон — редакция с пунктами 8–11.

### `docs/adr/ADR-024-sostav-cpt-miksa-v12r50-dolya-replay-50-kak-kalibrovochnyy-miks-s3m.md`

- **Класс:** дополнения по ветвям (`additive_annotation`)
- **Вариантов:** 2
- **Канон:** `9d078c2dbf9fdaffc75798f6c2c320034f54e9e1`
- **Почему:** mix-lr-calibration меняет ссылку на носитель чисел: канонический — evidence/s3m-ppl-arms.json, а evidence/mix-lr-calibration.json «не создан и не будет» (ADR-023 п.10б). Канон — редакция mix-lr.

### `evidence/s3m-ppl-arms.json`

- **Класс:** дополнения по ветвям (`additive_annotation`)
- **Вариантов:** 2
- **Канон:** `b081cc6fca94a37c5378e15a9a7f79dd21ec46bf`
- **Почему:** mix-lr-calibration добавляет leak_check_ref → evidence/calib-protocol-audit.json (ADR-025 п.2 / C-020). Канон — редакция mix-lr.

### `evidence/s3s-hygiene.json`

- **Класс:** конфликт имени артефакта (`artifact_name_conflict`)
- **Вариантов:** 2
- **Канон:** `SPLIT(c1b71108965edf4160f3e6dd4a726be949f27ced ; c26463c7932e05297c1ccbac6fb52b2ca7e67068)`
- **Почему:** ОДНО ИМЯ — ДВА РАЗНЫХ УЗЛА: control-arms/fleet-2 несут узел S3s-2 (12.5 КБ, ветка arch/laguna-control-arms), fleet-analysis несёт узел S3s-1 (25.7 КБ, ветка arch/laguna-fleet-analysis). Это не редакции одного доказательства, а два разных доказательства. Канон — развести по именам (s3s1-hygiene.json / s3s2-hygiene.json) либо оставить одно с полем node и слить как массив узлов.

### `runs/calib-25-0.35-20260916-0820/run_manifest.json`

- **Класс:** разные значения поля (`field_value_conflict`)
- **Вариантов:** 4
- **Канон:** `PENDING-OWNER`
- **Почему:** Четыре редакции одного манифеста одного физического прогона; расходятся arch_base_version, pipeline_path, created_at, набор hyperparameters и hyperparameters_source. Носители факта (calib_params.json, stages.tsv) во всех ветвях тождественны — см. canon_fields.

### `runs/calib-25-0.7-20260916-0820/run_manifest.json`

- **Класс:** разные значения поля (`field_value_conflict`)
- **Вариантов:** 4
- **Канон:** `PENDING-OWNER`
- **Почему:** То же, рука 25-0.7.

### `runs/calib-50-0.35-20260916-0820/run_manifest.json`

- **Класс:** разные значения поля (`field_value_conflict`)
- **Вариантов:** 4
- **Канон:** `PENDING-OWNER`
- **Почему:** То же, рука 50-0.35.

### `runs/calib-50-0.7-20260916-0820/run_manifest.json`

- **Класс:** разные значения поля (`field_value_conflict`)
- **Вариантов:** 4
- **Канон:** `PENDING-OWNER`
- **Почему:** То же, рука 50-0.7.

### `runs/calib-ppl-20260916-0820/NOT_A_RUN`

- **Класс:** дополнения по ветвям (`additive_annotation`)
- **Вариантов:** 2
- **Канон:** `99c7d828007fb5bd4c8b49ee8a2d582cddfe5e3e`
- **Почему:** fleet-analysis меняет одну строку: ссылка на «тот же класс каталога» указывает на runs/calib-audit-20260916/ вместо runs/s3n-ppl-20260916/. Смысл маркера тождествен; канон — любая, предпочтительна ссылка на каталог, существующий в сводимой ветке.

### `runs/replay50-collapse-20260916/compare_text.json`

- **Класс:** конфликт имени артефакта (`artifact_name_conflict`)
- **Вариантов:** 2
- **Канон:** `b4dba60af5499087d7fc370694b1096aa979fe41`
- **Почему:** ОДНО ИМЯ — РАЗНЫЕ ТИПЫ АРТЕФАКТА: в ветке replay50 под этим именем лежит stdout-лог детокенизации (145 Б, не JSON), в четырёх других — JSON-отчёт схемы corpus-compare-text/1 (16.3 КБ). Дефект: имя .json занято логом. Канон — JSON-отчёт; лог переносится под .log.

### `runs/replay50-collapse-20260916/run_manifest.json`

- **Класс:** разные значения поля (`field_value_conflict`)
- **Вариантов:** 3
- **Канон:** `PENDING-OWNER`
- **Почему:** Три редакции: arch_base_version a0ed9c9/342eb9e/2f85a12 и разные notes[2] (S3s-2 / S3s-1 / S3r описывают свой способ переноса корпуса).

### `runs/rev-pool-v2/exclusions.json`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `74fa04a2ebd96aa4440cdaf63d20282a9fb8ef94`
- **Почему:** mix-lr-calibration несёт S3f-fix-2 с разбором цепочек (114.6 КБ, +840/-125 строк); семь ветвей — S3f (48.8 КБ). Вариант mix-lr — продолжение той же линии.

### `tools/README.md`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 3
- **Канон:** `MERGE(afc4b640d7b35b3e9c848993025825d6e24bdc3d + 55787250017860a521deaf7a1ec877ce07ddfc71)`
- **Почему:** Три редакции: passrate-probe — ранняя (51.0 КБ), control-arms/protocol-diag/fleet-analysis/replay50/fleet-2 — база (59.1 КБ), eval-set/mix-lr — база + раздел S3m-2 (+14 строк). Канон — сведение.

### `tools/mix_lr_grid_chain.sh`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `e945eddd84856d8e10b7e592ad221f1ced6ac05a`
- **Почему:** control-arms/fleet-2 несут обобщённый драйвер (--run-prefix / --grid-name, серия контрольных рук S3o, 10.5 КБ); пять ветвей — версия S3m с жёсткими calib-* (8.8 КБ). Обобщённая версия — надмножество.

### `tools/pilot_chain.sh`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `d7cd13ec98e363414ae93c35cca952b13b1a71e2`
- **Почему:** passrate-probe несёт снимок от 14.09 (39.7 КБ); семь ветвей — 42.3 КБ. Направление — база семи.

### `tools/ppl_probe.py`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `8ec80ebaa4bc08c15b49f399822ac396ddc378c4`
- **Почему:** ФАКТ ПЕРВИЧЕН (ADR-028 п.1): вариант control-arms/eval-set/fleet-2 (83.6 КБ) имеет sha256 5a2c3f72…abf72, который объявлен в docs/specs/EVAL-SETS.md и процитирован в 5 evidence-файлах (s3t-baseline-k2, s3t-k2-set, s3v-control-arms-ppl, s3w-domain-by-arms, s3u-full-cpt). Вариант mix-lr/protocol-diag/fleet-analysis/replay50 (82.4 КБ, sha256 73c6b3a8…) не процитирован нигде и не содержит наборов v3_general/k2_general. Канон — вариант с объявленным хешем.

### `tools/run_mix_lr_calib.py`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `96a8b353274842559227392bec4a0c23783307c8`
- **Почему:** control-arms/fleet-2 несут обобщённый раннер (148.6 КБ, поддержка серии контрольных рук, +16 функций относительно 5 ветвей); пять ветвей — версия S3m (116.3 КБ). Обобщённая версия — надмножество.

### `tools/tests/run_tool_tests.sh`

- **Класс:** дополнения по ветвям (`additive_annotation`)
- **Вариантов:** 7
- **Канон:** `UNION(SECTIONS)`
- **Почему:** СЕМЬ редакций одной линии — каждая ветвь дописала свои секции поверх общей базы (18 общих секций). Уникальные секции: eval-set — 16–24 (S3q/S3t/S3v/S3w), protocol-diag — 17 (s3n_ppl_curve), control-arms/fleet-2 — 17–19 (S3o, S3u, check_handoff_contract), mix-lr — без своих, passrate-probe — 16 (passrate_probe.py). Ни одна ветвь не несёт всех секций. Канон — объединение секций по номерам инструментов, не выбор одной стороны.

### `tools/write_run_manifest.py`

- **Класс:** устаревший снимок (`stale_snapshot`)
- **Вариантов:** 2
- **Канон:** `58bdce0fcfb1ef802989d35dca601f613d8810bf`
- **Почему:** control-arms/fleet-2 добавили статус `running` в KNOWN_STATUS (+1/−4, коммит a0ed9c9 S3o) — «каталог прогона существует с первой минуты». Шесть ветвей — редакция S3-pre. Важно: замороженные копии в runs/<прогон>/write_run_manifest.py обязаны остаться прежними (sha256 ba3f889… прибит в calib_params.json прогонов).

## 3. `run_manifest.json`: единственный класс «разные значения одного поля»

Четыре калибровочные руки несут по **четыре** редакции манифеста каждая. Ключевое
наблюдение, которое решает вопрос:

> **Носители факта тождественны во всех восьми ветвях. Расходится только производный
> артефакт.**

| Носитель факта прогона | Blob | Во скольких ветвях одинаков |
|---|---|---|
| `runs/calib-25-0.35-20260916-0820/calib_params.json` | `6eaef0c` | 8 из 8 |
| `runs/calib-25-0.7-20260916-0820/calib_params.json` | `f24a4b6` | 8 из 8 |
| `runs/calib-50-0.35-20260916-0820/calib_params.json` | `07357f4` | 8 из 8 |
| `runs/calib-50-0.7-20260916-0820/calib_params.json` | `7c212ed` | 8 из 8 |
| `runs/<рука>/stages.tsv` | `736581c / 22c25ba / 2b4ee4f / c7c89eb` | 8 из 8 |
| `runs/<рука>/write_run_manifest.py` | `c6e670e3, sha256 ba3f889abe288767…` | 8 из 8 |

Четыре физические руки — ОДИН прогон каждая: носители факта тождественны во всех ветвях (calib_params.json created_at 06:50:35 для 25-0.35). Расходится только производный артефакт — run_manifest.json, пересобранный в четырёх дельтах независимо.

Кто пересобрал манифест (все четыре — независимо, каждый из своего источника):

| Ветвь(и) | Коммит | Дельта | Источник значений |
|---|---|---|---|
| fleet-2/control-arms | `a3dee03` | S3s-2 | `calib_params.json` прогона |
| fleet-analysis | `0ec9cf4` | S3s-1 | `calib_params.json` прогона |
| protocol-diag | `282d903` | S3r + 0150d6e | `calib_params.json` прогона |
| eval-set/mix-lr | `bd05ed4` | S3m-2 | пересказ `run_pilot.py → pilot_chain.sh` |

Поле-в-поле (на примере руки `calib-25-0.35-20260916-0820`):

| Поле | fleet-2 / control-arms | fleet-analysis | protocol-diag | eval-set / mix-lr | Канон |
|---|---|---|---|---|---|
| `arch_base_version` | `a0ed9c9` | `342eb9e` | `2f85a12` | `null` | УДАЛИТЬ (ADR-028 п.2) |
| `pipeline_path` | `runs/calib-25-0.35-…/laguna_pipeline_calib.py` | `runs/calib-25-0.35-…/laguna_pipeline_calib.py` | `runs/calib-25-0.35-…/laguna_pipeline_calib.py` | `calib/calib-25-0.35-…/…` | runs/calib-<рука>-<ts>/laguna_pipeline_calib.py |
| `created_at` | `14:11:52` | `14:11:12` | `14:02:20` | `08:14:19` | время записи канонического манифеста |
| `hyperparameters.corpus` | `v12r` | `v12r` | `v12r` | `v12r` | v12r для 25-рук, v12r50 для 50-рук — СОВПАДАЕТ ВО ВСЕХ ВЕТВЯХ |
| `hyperparameters.corpus_value_note` | — | — | есть | — | СОХРАНИТЬ примечание |
| `hyperparameters.pipeline_base_sha256` | — | — | — | есть | объединить (фактическое значение запуска) |
| `hyperparameters.rl_steps / eval_items / stages_file` | — | — | — | `500` / `192` / `stages.tsv` | объединить (фактические значения запуска) |
| `hyperparameters.arch_base_version` | — | — | — | `""` (пустая строка) | УДАЛИТЬ (дубль удаляемого поля) |
| `hyperparameters_source` | `calib_params.json прогона (фактический запуск цепочки)` | `calib_params.json прогона (фактический запуск цепочки)` | `calib_params.json прогона (фактический запуск цепочки)` | `фактические значения запуска цепочки (run_pilot.py → pilot_chain.sh)` | "calib_params.json прогона (фактический запуск цепочки)" |

**Что здесь важно и неочевидно.** Дефект, описанный в ADR-028 п.1 — «`corpus: v12r50` у
25 %-рук» — **на момент аудита уже исправлен во всех восьми ветвях**: все дают
`corpus=v12r` для 25-рук при `dataset_path=datasets/tok/cpt_corpus_v12r_8192_qwen25.npy`.
Чинить по этому полю нечего; единственное отличие — в `protocol-diag` рядом лежит
поясняющее поле `corpus_value_note`, и его стоит сохранить при сведении. Это
положительный результат: правило ADR-028 п.1 сработало и отражено в факте.

**Что чинится механически** (ADR-028 п.2): поле `arch_base_version` удаляется из новых
манифестов и заменяется парой `run_started_at_commit` / `manifest_written_at_commit`.
Первый для этих рук неизвестен и записывается `null` («не выдумывать», ADR-028 п.2);
второй — коммит, в котором манифест записан при сведении.

**Про `pipeline_path`.** Значение `calib/calib-25-0.35-…` из ветвей eval-set/mix-lr
указывает в никуда: каталога `calib/` в кейсе нет (проверено). Факт — `runs/…`, он же
совпадает с фактическим расположением копии пайплайна в каталоге прогона.

## 4. ADR-номера 025–031: таблица и вопрос перенумерации

### 4.1 Кто какой номер несёт

| Номер | Ветвь(и) | Тема | Статус |
|---|---|---|---|
| **ADR-025** | mix-lr | Чистота измерительного набора: general-набор не пересекается ни с одним кандидатным миксом (гейт перед применением потолка PPL) | Proposed |
| **ADR-026** | mix-lr | Одномерность калибровочной манипуляции и неразличённый режим внимания: пересмотр выводов S3m | Proposed |
| **ADR-027** | eval-set, fleet-2 | Мера общего языка двухкомпонентна: набор в жанре реплея и набор вне обучающего распределения | Proposed |
| **ADR-028** | protocol-diag | Канон манифеста прогона и политика истории git: факт первичен, история не переписывается | Proposed |
| **ADR-029** | eval-set | Отзыв стоп-сигнала CPT: двухкомпонентная мера не подтверждает разрушение общего языка, стадия продолжается на замороженном миксе | Proposed |
| **ADR-030** | control-arms, fleet-2 | Промежуточные точки CPT: тревога по тренду, остановка по концу стадии или по росту на трёх точках | Proposed |
| **ADR-031** | control-arms, fleet-2 | Пик LR полного CPT: ×0.035 вместо ×0.35 — домен сопоставим, язык на порядок лучше | Proposed |

### 4.2 Висячие зависимости: ни одна ветвь не несёт полного набора

Это самое важное для сведения. У **каждой** из четырёх ветвей есть ссылки на решения,
которых в ней нет:

| Ветвь | Ссылается на отсутствующие ADR | Кто ссылается |
|---|---|---|
| control-arms | ADR-027 | ADR-030, ADR-031 |
| control-arms | ADR-029 | ADR-030, ADR-031 |
| eval-set | ADR-025 | ADR-027, ADR-029 |
| eval-set | ADR-026 | ADR-027, ADR-029 |
| fleet-2 | ADR-025 | ADR-027 |
| fleet-2 | ADR-026 | ADR-027 |
| fleet-2 | ADR-029 | ADR-030, ADR-031 |
| protocol-diag | ADR-025 | ADR-028 |

НИ ОДНА ветвь не несёт полного набора решений: у каждой есть ссылки на ADR, которых в ней нет. При сведении union по docs/adr/ закрывает все ссылки — проверено: объединение 8 ветвей даёт ровно ADR-001…ADR-031 без пропусков и без дублей номеров.

Практический вывод: **`fleet-2` сейчас несёт ADR-030/031, которые зависят от ADR-027 и
ADR-029, а ADR-029 в ней нет** — как и ADR-025/026, от которых зависит ADR-027.
Решения приняты и действуют, а их оснований в дереве нет. Это не косметика: без
ADR-029 нельзя прочитать, **почему** CPT продолжен, а без ADR-025/026 — **почему**
изменилась мера общего языка. Перенос оснований обязан идти первым шагом.

### 4.3 Перенумерация: не требуется

**Вердикт: ПЕРЕНОМЕРАЦИЯ НЕ ТРЕБУЕТСЯ.**

- **case_namespace.** ADR-025…ADR-031 заняты по одному разу каждый; файлового дубля номера нет ни в одном дереве и ни в одной точке истории (проверено полным обходом git log --all по путям docs/adr/ADR-025* и ADR-026* — за всю историю существует ровно один ADR-025 и ровно один ADR-026 в пространстве имён кейса).
- **union.** Объединение docs/adr/ по восьми ветвям = ADR-001…ADR-031, 31 файл, без пропусков номеров.
- **premise_check.** Пример ADR-028 п.3 («ADR-025 существует в двух вариантах: чистота измерительного набора и канон манифеста») в репозитории НЕ ВОСПРОИЗВОДИТСЯ: решение о каноне манифеста закоммичено сразу как ADR-028 (коммит 105f1ee), а ADR-025 за всю историю существует в единственном варианте. Правило п.3 не отменяется, но его обоснование следует привести к факту при сведении.
- **real_overlap.** Реальное перекрытие номеров — МЕЖДУ ПРОСТРАНСТВАМИ ИМЁН репозитория: корневой docs/adr/ (продукт arch-be, 45 файлов, максимум ADR-047) и кейсовый кейсы/laguna-compact/docs/adr/ (31 файл) обе используют номера 001…031+. Файлового конфликта нет (разные каталоги), но правило «номер = максимум по всем ветвям +1» без указания области неисполнимо.

**Предложение.** Уточнить ADR-028 п.3 при сведении: (а) нумерация глобальна ВНУТРИ области (кейс / корневой продукт), максимум считается по всем ветвям этой области; (б) пример-обоснование заменить на воспроизводимый факт — перекрытие кейсового и корневого пространств имён; (в) счётчик «следующий свободный номер кейса» = ADR-032.

**Движений номеров нет.** Ни один ADR не «уезжает» на другой номер: перенумерация задним числом запрещена ADR-028 п.3 и здесь не нужна.

## 5. План сведения — пошагово, без выполнения

Ветка сведения: arch/laguna-fleet-2 (точка сведения). Каждый шаг ниже выполняется владельцем на ветке сведения; этот аудит ничего не менял.

**Шаг 1. Зафиксировать область нумерации ADR (ADR-028 п.3): кейс — своё пространство, максимум 031 → следующий ADR-032.**

- Зачем: Без этого шага любой следующий номер рискует столкнуться с корневым docs/adr/.
- Риск: низкий

**Шаг 2. Перенести отсутствующие решения: ADR-025, ADR-026 из mix-lr-calibration, ADR-028 из protocol-diag, ADR-029 из eval-set. ADR-027/030/031 уже в fleet-2.**

- Зачем: Закрывает все висячие зависимости: ADR-027 зависит от 025/026, ADR-030/031 — от 027 и 029.
- Риск: низкий — файлы вносятся как есть, номера не меняются

**Шаг 3. Свести tools/ppl_probe.py к варианту с объявленным хешем 5a2c3f72…; проверить, что sha256 файла после сведения совпадает с объявленным в EVAL-SETS.md и в 5 evidence-файлах.**

- Зачем: ADR-028 п.1: факт первичен. Обратный выбор обесценил бы 5 доказательств, ссылающихся на хеш.
- Риск: средний — нужна сверка, что вариант mix-lr не несёт нужных правок помимо SETS

**Шаг 4. Свести tools/run_mix_lr_calib.py, tools/mix_lr_grid_chain.sh, tools/write_run_manifest.py к обобщённым редакциям (fleet-2/control-arms).**

- Зачем: Это надмножества: контрольные руки S3o и обобщённый драйвер серий не существуют в редакциях пяти ветвей.
- Риск: низкий — направление проверено по diff (добавление флагов, а не иная логика)

**Шаг 5. Свести tools/tests/run_tool_tests.sh объединением секций, а не выбором стороны.**

- Зачем: Ни одна из семи редакций не несёт всех секций: уникальные наборы у eval-set (16–24), protocol-diag (17), fleet-2/control-arms (17–19), passrate-probe (16). Выбор любой стороны молча удалит проверки четырёх приборов.
- Риск: высокий — самая дорогая операция сведения; делать по секциям с прогоном после каждой

**Шаг 6. Пересобрать восемь run_manifest.json калибровочных рук и replay50-collapse по канону ADR-028: удалить arch_base_version, добавить run_started_at_commit=null и manifest_written_at_commit=<коммит сведения>, привести pipeline_path к runs/…, hyperparameters — к объединению без arch_base_version, сохранить corpus_value_note.**

- Зачем: Это единственный класс, где расходятся ЗНАЧЕНИЯ одного поля, и единственный, который ADR-028 п.2 предписывает чинить механически. Носители факта (calib_params.json, stages.tsv, замороженный write_run_manifest.py) во всех ветвях тождественны — пересборка воспроизводима.
- Риск: средний — перезапись поля, которое читает страж C-012; прогнать check_run_manifest.py после

**Шаг 7. Развести evidence/s3s-hygiene.json: два узла (S3s-1 и S3s-2) под одним именем — либо два файла, либо один с массивом узлов.**

- Зачем: Это не редакции одного доказательства, а два разных; слияние «по одной стороне» потеряет доказательство одной из дельт.
- Риск: низкий — файлы не читаются стражами по имени

**Шаг 8. Развести runs/replay50-collapse-20260916/compare_text.json: JSON-отчёт оставить под именем .json, stdout-лог ветки replay50 перенести под .log.**

- Зачем: Под одним именем лежат разные типы артефакта; сейчас ветка replay50 несёт под .json не-JSON.
- Риск: низкий

**Шаг 9. Свести CONSTRAINTS.yaml к редакции с C-020 (20 правил) и проверить C-020 на evidence-файлах после шага 6.**

- Зачем: C-020 (leak_check в evidence PPL) ссылается на поле, которое добавляет mix-lr; без него гейт C-020 красный.
- Риск: низкий

**Шаг 10. Свести data/rev-envs-v2-card.json и runs/rev-pool-v2/exclusions.json к редакциям mix-lr-calibration (надмножества с prompt_leak и S3f-fix-2).**

- Зачем: Правка карточки пула v2 (35a96e9) существует только в mix-lr-calibration — отсюда красный тест S3f в двух других ветках.
- Риск: низкий — надмножества

**Шаг 11. Свести ADR-018/021/022/023/024/015 и README.md/tools/README.md/ARCHITECTURE-SPINE.md объединением дополнений на базовой редакции; для ADR-022 сохранить баннер «ОТОЗВАНО — ADR-029».**

- Зачем: Дополнения сделаны в разных ветвях и каждое несёт решение; выбор одной стороны теряет пункты 8–11 ADR-023 и ссылку на канонический носитель чисел.
- Риск: средний — ручная работа с текстом, источник новых расхождений

**Шаг 12. После сведения прогнать полный набор гейтов и тестов ветви-приёмника; зафиксировать факт сведения в README (карта evidence) и в отдельном evidence-файле.**

- Зачем: ADR-023 п.8: дельта обязана проверить СВОЙ результат механически, а не заявить его.
- Риск: низкий

Порядок шагов не произволен: шаг 1 (область нумерации) открывает шаг 2 (перенос
оснований), шаг 2 открывает шаги 3–11 (сведение содержания), шаг 12 закрывает
дельту. Обратный порядок даст сведение без оснований.

**Errata (20.09.2026, дельта `followup-instruments`) — к шагу 3 и к блоку
`### tools/ppl_probe.py` §2.** Утверждение «вариант … с sha256 `73c6b3a8…` **не
процитирован нигде**» верно **только про цитаты**: ни один артефакт не объявляет
этот хеш хешем прибора своего замера (сплошной скан — `evidence/instrument-hash-audit.json`,
поле `instruments[].uncited_revisions`). Как утверждение о **дереве** оно не верно:
`73c6b3a8…` — это редакция **P-1** прибора `tools/ppl_probe.py`, и она стоит на
вершинах ветвей: пять ветвей кейса на дату снимка hr-11 (`arch/laguna-mix-lr-calibration`,
`arch/laguna-protocol-diag`, `arch/laguna-fleet-analysis`, `arch/laguna-ladder-view`,
`arch/laguna-replay50`) плюс два служебных ref'а (`backup/pre-filterrepo-20260917`,
`origin/main`) — **семь вершин**. Состав ветвей подвижен (принятые ветви флота
удаляются: `arch/laguna-ladder-view` принята 20.09.2026, `aaf6c25`; на дату этой
дельты P-1 держат четыре ветви кейса и те же два служебных ref'а), поэтому число
берётся замером, а не по памяти. Текст §2 и шага 3 не переписывается (ADR-028
п.4) — факт назван здесь и в реестре приборов
(`docs/specs/INSTRUMENT-VERSIONS.md` §2.1.3, таблица вершин).

### 5.1 Чего план НЕ делает

- Перенумерация ADR — не требуется (дублей номеров в кейсе нет).
- Перезапись git-истории для удаления 456 524 032 Б (два блоба по 228 262 016 Б, коммит 342eb9e) — запрещена ADR-028 п.4.
- Правка чужих ветвей: расхождения чинятся только на ветке сведения (ADR-028 п.8).
- Запуск GPU/стенда: полный CPT идёт автономно (tmux s3u-full-cpt-20260916-1933), аудит его не трогал.

## 6. Расхождения с прежними решениями

- ADR-028 п.3: пример-обоснование («ADR-025 в двух вариантах») не воспроизводится в репозитории; правило сохраняется, обоснование требует правки при сведении.
- ADR-028 п.1: описанный дефект corpus=v12r50 у 25-рук на момент аудита исправлен во всех восьми ветвях — расхождение по этому полю отсутствует, чинить нечего.
- ADR-023 п.10б: mix-lr-calibration объявляет evidence/s3m-ppl-arms.json каноническим носителем чисел S3m и снимает evidence/mix-lr-calibration.json; при сведении имя в README и ADR-024 обязано указывать на фактический носитель.

Фиксируются как факт, а не как повод не выполнять план: ни одно из этих расхождений не
отменяет правила ADR-028 — они требуют приведения **обоснований** к факту при сведении.

## 7. Что дальше

Сведение выполняет владелец: этот аудит сознательно оставлен read-only (ADR-028 п.8 —
«расхождения, живущие в одной ветке, чинятся при сведении, а не в чужих ветках»).
Машиночитаемая карта — `evidence/fleet-branch-divergence.json`; суммы в этом файле и в
нём получены одним прогоном и обязаны совпадать.

## 8. Сведение выполнено (S3z, 17.09.2026)

**Статус: ВЫПОЛНЕНО.** Ветка канона — `arch/laguna-canon`, основание — tip
`arch/laguna-control-arms` (`53f934a`, ADR-030/031 и CPT-контур). Журнал сведения
(ветка → merge-коммит → конфликты → решения → гейты) — `evidence/s3z-canonization.json`;
здесь — только то, что меняет чтение плана выше.

**Влито 10 ветвей из 12 заявленных.** `arch/laguna-ppl-probe` и
`arch/laguna-verifiers` в репозитории не существуют (ни локально, ни в refs) —
вопрос к владельцу, см. `open_questions` журнала. Порядок — по возрастанию риска,
как в плане; после каждого merge прогонялись `check_symlink_hygiene.sh` и
`check_run_manifest.py --runs runs/`.

**Что разошлось с этим планом по факту:**

| Пункт плана | Факт сведения |
|---|---|
| §2: 27 разошедшихся имён по восьми ветвям | **38 имён** по одиннадцати: план не знал ветвей `flex-check`, `pool-v2-fix`, `sft-prep` (и их файлов: `docs/specs/SFT-STAGE-PLAN.md`, `evidence/fleet-sft-prep.json`, `evidence/s1-data-audit.json`, `tools/build_rev_envs.py`, `tools/check_eval_leakage.py`, `tools/full_cpt_probe.py`, `tools/run_full_cpt.py`, `runs/full-cpt-*`, ADR-030) |
| §2: `run_tool_tests.sh` — 7 редакций | **10 редакций**, 36 секций; канон — объединение секций, 552 уникальные `expect_`-строки. Три пары утверждений взаимоисключающие (сообщение драйвера сетки/серии, старый тест карточки пула, конфигурация S3u ADR-029 против ADR-031) — оставлена редакция, согласованная с каноном приборов |
| §2: у `s3s-hygiene.json` два варианта | подтверждено; канон — **один документ схемы `s3s-hygiene/2` с массивом узлов** `S3s-1`/`S3s-2`, оба узла без правок (требование задания) |
| §2: `compare_text.json` — JSON + лог | подтверждено; JSON остался под `.json`, stdout-лог ветви replay50 — под `compare_text.log` |
| §5 шаг 2: перенести ADR-025/026/028/029 | выполнено: union `docs/adr/` даёт **ADR-001…ADR-031** без пропусков и дублей, все висячие зависимости закрыты |
| §5 шаг 3: `ppl_probe.py` к хешу `5a2c3f72…` | выполнено; sha256 файла в каноне совпал с объявленным в `docs/specs/EVAL-SETS.md` |
| §5 шаг 6: пересобрать манифесты | выполнено для 5 имён; `write_run_manifest.py` расширен полями ADR-028 п.2 (`run_started_at_commit` = `null`, `manifest_written_at_commit`) и флагом `--note`; `arch_base_version` из новых манифестов исключён |
| §6: дефект `corpus: v12r50` у 25-рук | подтверждено: чинить нечего, `corpus` совпадал во всех ветвях; примечание `corpus_value_note` protocol-diag сохранено |
| §6: `mix-lr-calibration.json` назван носителем | исправлено в README: носитель чисел S3m — `evidence/s3m-ppl-arms.json` (ADR-023 п.10б); строка «не создан» внесена в карту evidence |

**Починки, которых план не предвидел** (вскрыты самим сведением — гейт покраснел
на сводимом дереве, а не на ветвях):

1. `runs/passrate-probe-20260916-0740/` — каталог без манифеста AD-2 в ветви
   passrate-probe; получил маркер `NOT_A_RUN` (это вход прибора, не прогон).
2. `runs/calib-grid-20260916-0820/` и `runs/calib-ppl-20260916-0820/` — маркер
   `NOT_A_RUN` и `run_manifest.json` одновременно, после слияния двух независимых
   починок C-012 из разных ветвей; канон — маркер.
3. `runs/s3t-arms-20260916/` — каталог замера без манифеста; манифест записан
   `tools/write_run_manifest.py` по образцу однотипных `s3q/s3v/s3w-arms`.

**Что осталось несведённым** (и почему): ревизия `c0a1480` (S3f-fix-2) — единственный
производитель фактического пула v2 (9162) и канонической карточки S3p, но её код не
входит ни в одну из сводимых ветвей (только `backup/pre-filterrepo-20260917`).
Полный перечень — `open_questions` в `evidence/s3z-canonization.json`.


---

## 8. Дополнение S3z-2 (20.09.2026): что этот план не покрывал

Карта писалась по **восьми ветвям** `arch/laguna-*` и по tip-ам 16.09.2026. Сведение
канона с живой линией `arch/laguna-control-arms` (S3z-2, ADR-046) нашло три класса
расхождений, которых в карте нет по построению. Ни один пункт карты не отменяется —
дополнение фиксирует границы её области.

1. **Дубль номера ADR на границе с `main`.** Карта проверила только ветви `arch/*`
   и потому заключила «перенумерация не требуется» (§4.3). Конфликт 024 лежал на
   границе «`main` ↔ канон»: там «Процедура приёмки дельт», здесь — «состав микса
   v12r50». Разрешён по ADR-028 п.3 с тайбрейком владельца: 024 закреплён за
   процедурой, микс переномерован в **ADR-047** (ADR-046 пп.1–4), ссылки правлены.
   Правило ADR-028 п.3 расширено на `main` — аудит без него не считается выполненным.
2. **Новый конфликт в `CONSTRAINTS.yaml`** (класса в карте не было): ветка выдала
   локальным счётом правила **C-020/C-021** под карточку обучающего набора SFT, а эти
   номера в каноне уже заняты (`C-020` — `leak_check` ADR-025 п.2; `C-021` —
   предложение AD-12; `C-022` зарезервирован за стражем agentic-доли, ADR-035 п.5).
   Разведено как конфликт имён: правила ветки уехали на **C-023/C-024**, тексты не
   менялись (ADR-046 п.2 — номер выдаёт канон).

   > Тот же класс дефекта, что и дубль 024, и та же причина: номер выдавался локальным
   > счётом каталога в каждом worktree. Карта искала дубли только среди ADR.
3. **Конфликтов сверх 27 имён стало больше: 4** (`CONSTRAINTS.yaml`,
   `docs/specs/SFT-STAGE-PLAN.md`, `tools/README.md`, `tools/passrate_probe.py`).
   Причина: `control-arms` после аудита продолжил линию — дописал секции приборов,
   добавил файлы, которых в карте нет (SFT-STAGE-PLAN.md появился в двух ветвях
   одновременно, passrate_probe.py — add/add). Разрешение — по классам карты:
   дополнения объединением (`additive_annotation`), надмножество — по более новой
   редакции (`stale_snapshot`).

**Что из карты выполнено иначе, чем в столбце «Канон (предложение)».** Ровно одно:
`evidence/s3s-hygiene.json` — карта предлагала `SPLIT(...)`, S3z свёл два узла в один
документ с массивом (`s3s-hygiene/2`). S3z-2 привёл к столбцу карты: узлы разведены
по именам — `evidence/s3s1-hygiene.json` (S3s-1) и `evidence/s3s2-hygiene.json`
(S3s-2), содержимое — исходные документы узлов байт-в-байт.

---

## 8. Сведение 22.09.2026: четыре ветви, каноническая нумерация правил, страж C-032

**Статус: ВЫПОЛНЕНО.** Сведены четыре ветви, накопившие незабранную работу; акт —
`evidence/acceptance-2026-09-22-consolidation.json`. Этот раздел дополняет карту
там, где она снова оказалась неполной: карта (и её дополнение S3z-2) искала дубли
номеров **ADR** и нашла один класс — дубль номера **правила** она назвала в §8.2,
но **механизма** не предложила. Ниже — что изменилось в чтении плана.

| Ветвь | Что несла | Конфликты | Канонический номер правила |
|---|---|---|---|
| `arch/proof-of-execution` | обязательный раздел «Доказательство исполнения» у новых ADR, `tools/check_execution_proof.py` | `CONSTRAINTS.yaml` (add/add в конце), `run_tool_tests.sh` (add/add в конце) | **C-031** (перенумеровано из C-029 коммитом `51ca0a5` ДО сведения) |
| `arch/sft-decode-diagnosis` | диагностика декодирования, evidence пригодности rollout'ов, обвязка запрета сна | `CONSTRAINTS.yaml` (ветвь форкнута до C-026…C-028), `README.md`, `model/AD-9`, `run_tool_tests.sh` | **C-029** (страж сна, AD-9 вторая грань) |
| `arch/sft-loop-origin` | профиль петель S3ar/S3at, три evidence, три прибора | `README.md`, `run_tool_tests.sh` | новых номеров нет |
| `arch/rl-env-probe` | проба предусловий RL на стенде GB10 (ADR-049 п.10) | `README.md`, `run_tool_tests.sh` (секция вставлена в середину файла) | новых номеров нет |
| `arch/sft-length-policy` | — | — | **не сведена: впереди `main` ноль коммитов** (вершина — предок канона) |

**Разрешение конфликтов по классам — то же, что в §1.1 карты, плюс один новый.**

1. `CONSTRAINTS.yaml` — **объединение реестров**. Ветвь, форкнутая до появления
   C-026…C-028, несла устаревшую копию реестра: из неё взят **только её собственный
   блок правила**, а не весь файл (иначе сведение откатило бы правила канона).
   Тексты правил при перенумерации не менялись — проверено сличением полей
   (не строк-комментариев: комментарий принадлежит разделу, а не правилу).
2. `tools/tests/run_tool_tests.sh` — **объединение секций**; секции ветвей
   переномерованы вслед за секциями канона (34–41). Строк тестов добавлено
   **109 + 967 + 246 + 83**; ни одна секция не потеряна, блок `rl-env-probe` сверен
   построчно.
3. `model/*` — **объединение сущностей**, но не механическое: у `model/AD-9`
   `verified_by` — объединение (`[C-018, C-029]`), а `affects` — **редакция канона**
   (пустой список). Редакция ветви вернула бы дефект, уже исправленный `22517ca`
   (ссылки на правила в `affects` — дубль `verified_by`). Класс назван: не всякое
   расхождение чинится объединением — поле, чей дефект канон уже правил, объединению
   не подлежит.
4. `README.md` — объединение; **исключение одно**: строка «состав правил» приведена
   к факту (`C-001…C-020 и C-023…C-032`). Это не текст правила, а описание текущего
   состояния гейта; объединение двух устаревших редакций дало бы заведомо неверную.

**Новый класс — страж номеров правил (правило C-032).** Дефект «номер выдан
локальным счётом каталога» воспроизводился трижды и каждый раз ловился человеком
(ADR-024; C-020/C-021 → C-023/C-024; C-029 → C-031). Карта назвала класс, но
оставила его дисциплине — а дисциплина здесь не работает по построению: ветвь не
видит чужих номеров до сведения. Теперь класс закрыт механически:
`tools/check_rule_numbers.py` берёт у каждой **незалитой** ветви номера, которые
она **выдала сама** (её номера минус номера точки ветвления), и краснеет при
пересечении с каноном; отдельно краснеет номер, выданный **двумя** незалитыми
ветвями (ровно то, что случилось с C-029), и дубль внутри файла правил. Слитые
ветви пропускаются — иначе страж краснел бы на собственной истории
(ADR-023 п.12). Зубы: секция 41 тестов (синтетический репозиторий, обе стороны) и
показ на настоящем кейсе временной ссылкой.

**Что эта работа не чинила и почему** (названо, а не умолчано):

* **C-029 против `runs/sft-loop-trend-20260921`** — новое красное, созданное парой
  шагов сведения (правило пришло одной ветвью, каталог прогона — другой). Причина
  измерена: порог прибора — начало суток 21.09, а правило вступило в силу в
  14:22 MSK; прогон начат в 14:10 MSK, расписки у него быть не могло. Правка порога —
  решение владельца правила, а не исполнителя сведения.
* **`model/AD-13`**: `verified_by` в кавычках не ловится шаблоном гейта — дефект
  унаследован от `98eb548`, к сведению не относится.
* **C-014/AD-1**: артефакт `evidence/result-report-*.json` ждёт стадии A5.
* **C-018**: живой сенсор стенда, мерцает по занятости стадии (это свойство
  задокументировано в `README.md` с 20.09.2026).

## 8. Вторая волна сведения 22.09.2026 (S3bb): две оставшиеся ветви, ADR-053 и гигиена канона

**Статус: ВЫПОЛНЕНО.** Сведены две ветви, оставшиеся после первой волны; акт —
`evidence/acceptance-2026-09-22-consolidation.json`, раздел `wave2`. Этот раздел —
по образцу предыдущего §8: он называет то, что карта снова не покрывала, а не только
факт слияния.

| Ветвь | Что несла | Конфликты | Канонический номер |
|---|---|---|---|
| `arch/ladder-full-plan` (+1 коммит `7f114d8`) | ADR-052 (полная лесенка возвращается из `Deferred`), `docs/specs/LADDER-FULL-PLAN.md` (382 строки), оговорка области у AD-1, запись в `WORK-BACKLOG.md` | **нет** — автослияние (правки в конце разных блоков `ARCHITECTURE-SPINE.md`, остальное — новые файлы) | ADR-052 (номер был выдан ветвью; линия 001…053 без пропусков, ADR-053 лежит выше) |
| `arch/sft-v13-arm` (+2 коммита `5abbfb0`, `2baa36b`) | S3ay-fix: предохранитель памяти не зависит от локали (`tools/early_gate_watch.py` + обвязка), инструменты S3ax/S3ay-arm, `tools/launch_full_stage.py`, `start_full_stage.py`, `patch_pipeline_sft.py`, `evidence/sft-v13-full-launch.json` | **нет** механически (ветвь дописывала файл тестов в конец) — но **номер секции** тестов совпал с каноном: у ветви и у канона была «34», см. ниже | новых номеров правил нет (`check_rule_numbers.py`: «выдала —») |

**Классы разрешений — те же, что в §1.1 карты, плюс два новых.**

1. **Номер секции тестов после слияния аддитивных блоков.** Обе стороны дописали
   тесты в конец `tools/tests/run_tool_tests.sh` (канон — секции 34–41, ветвь —
   свою «34»). Механического конфликта git не увидел, но два блока с одним номером
   читались бы как один: секция ветви переномерована **34 → 42** (прецедент первой
   волны: «секции ветвей переномерованы вслед за секциями канона»). Ни одна строка
   тестов не потеряна — блок ветви (424 строки) целиком лежит перед итоговой
   строкой `итого: PASS=…`.
2. **Красное, найденное на приёмке ветви, а не созданное ею намеренно.** Приёмка
   обязана не только слить, но и назвать, что ветвь принесла красного. Нашлись два
   класса, оба починены (и оба названы, а не замаскированы):

   * **Гонка в тесте ветви** (`секция 34 → 42`, «источник назван, число совпало с
     `/proc/meminfo` этой машины»). Тест сравнивал `available_gb` из отчёта прибора
     с **повторно прочитанным** `/proc/meminfo` на равенство с точностью до 0.01 ГБ.
     `MemAvailable` — живая величина: на машине, которую делят параллельные дельты
     флота, между записью отчёта и чтением тестом она уезжает на 0.01 ГБ за секунды
     (измерено на приёмке: 37.78 против 37.79). Тест проходил на площадке ветви по
     случайности; здесь упал. Проверка переведена на **допуск в 1 ГБ** с названной
     причиной: неверный источник, `None` или число не из `meminfo` расходятся с ней
     на гигабайты, а точное равенство живой величины проверкой не является.
   * **Ложная атрибуция цитаты хеша** (`tools/launch_arm_probe.py`). Реестр
     редакций (`C-025`) относит 64-шечный хеш к прибору по **ближайшей вверх ссылке
     на файл**; в ветви константа `PROBE_TOOL_SHA` (хеш `99dafa8d…` — пиннутый
     прибор `probe_language_split.py`, ADR-041) стояла сразу под списком модулей
     `PROBE_TOOLS`, где назван и `ppl_probe` — прибор **реестра**. Хеш читался
     цитатой чужой редакции, которой в истории прибора нет, и `C-025` краснел.
     Исправлено именем пиннутого прибора в комментарии над константой: смысл не
     менялся ни на строку, изменилась только различимость имени.

**ADR-053 — порог применимости правила: момент, а не начало суток.** Красное
`C-029` против `runs/sft-loop-trend-20260921`, названное первой волной, закрыто
решением архитектора (ADR-053), а не ослаблением правила:

* порог прибора `ENACTED_AT` = `2026-09-21T11:22:35+00:00` — **момент** коммита
  `ed87e8b` (14:22:35 MSK), которым правило вошло в ветвь; суточная гранулярность
  (`00:00:00`) была источником дефекта;
* формулировка правила `C-029` приведена к точной («после момента вступления
  правила в силу — 2026-09-21 14:22:35 MSK»); остальной текст правила не тронут;
* прогоны старше порога печатаются **поимённо** как `legacy-not-checked`
  («старше правила, защита сна НЕ доказана») и находкой не выставляются — то же
  третье состояние, что у `C-030`/`C-031` (ADR-023 п.12);
* задним числом не дописано ничего: ни расписка, ни артефакт прогона (ADR-023 п.9,
  ADR-028 п.4) — изменено только то, **к какому прошлому** прибор применяет требование;
* изменение прибора внесено в реестр редакций (`INSTRUMENT-VERSIONS.md` §6, строка
  `B-1`) с прямой оговоркой: **числа прибора до и после правки несравнимы** — до неё
  в область правила попадали все прогоны суток 21.09, после — только начатые с
  14:22:35 MSK;
* зубы проверены мутантами: тот же вход с порогом `00:00:00` краснеет, с порогом —
  моментом — зелёный; константа проверяется тестом (`ENACTED_AT` и её разбор), чтобы
  молчаливый откат к началу суток не прошёл.

**Что этот раздел не делал и почему** (названо, а не умолчано):

* **Текст правила `C-029` вне части о пороге не тронут** — дословно, включая
  evidence о механизме, площадках и обвязке (ADR-053 п.4 ограничивает правку именно
  частью о пороге применимости).
* **ADR-052 и ADR-053 получили обязательный раздел «Доказательство исполнения»**
  (правило `C-031`, канон от 22.09.2026): оба решения датированы 21–22.09.2026, то
  есть попадают в область правила, и без раздела гейт кейса краснел на **обоих**
  (ADR-053 — с момента своего коммита `221842d`, ADR-052 — с момента приёмки ветви).
  Разделы написаны по образцу ADR-051: назван носитель (константа, файл тестов,
  строка отчёта гейта, запись реестра) и **читающая** сторона; границы названы
  прямо — доказательство относится к объявлению решения, а не к его исполнению.
* **Веса, наборы, пулы, объявленный LR, бюджет шагов не менялись** — задача
  сведения их не касается.

**Обновление к §8 первой волны.** Оговорка у `rl-env-probe-gb10` в `README.md`
(«в канон не сведено — ADR-049 ещё Proposed в другой ветви») **устарела**: ветвь
`arch/laguna-control-arms` сведена ещё в первой волне, ADR-049 — **Accepted**
(коммит `62e44c2`, 21.09.2026). Формулировка исправлена на факт; сам факт приёмки
не переписывался.
