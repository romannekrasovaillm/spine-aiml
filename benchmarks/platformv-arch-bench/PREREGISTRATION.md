# PREREGISTRATION.md — фиксация дизайна ДО массовых генераций

Дата фиксации: 2026-09-09T21:10 (+03:00). После этой точки задачи, спайн-пакеты,
кастомизация и раннеры не меняются; результаты публикуются какими выйдут.
Методологический образец: `experiments/openspec-vs-spine/`.

## Зафиксированные входы

sha256 всех входов — в `logs/prereg_hashes.txt` (49 файлов):
- `tasks/*/{TASK,CONTEXT,RUBRICS}.md` — 24 файла, 8 задач;
- `spine/*/{ARCHITECTURE-SPINE.md,CONSTRAINTS.yaml}` — 16 файлов (генерат
  prepare_cells.py из TASK.md §3 + RUBRICS.md §2 + mechanical_checks);
- `customization/architect.md`;
- `runners/{prepare_cells,run_matrix,mech_score,judge,analyze,pvlib}.py`,
  `runners/smoke.sh`, `runners/mech_overrides.yaml`.

## Дизайн матрицы

**Основной массив: 8 задач × условия × 2 повтора (r1, r2) = 160 ячеек.**
- Модель `dsf` = deepseek-flash — 6 условий: `spine-arch`, `theseus-plain`,
  `theseus-arch`, `claude-plain`, `claude-arch`, `raw-llm`.
- Модель `glm` = glm-5.3-flash — 4 условия: `spine-arch`, `theseus-plain`,
  `theseus-arch`, `raw-llm`.

**Расширенный свип: 2 задачи (PGL-ARCH-001, CRX-ARCH-001) × 5 харнессов
(dsh, codewhale, hermes, openclaw, kimi) × 1 повтор = 10 ячеек**, условие
`<harness>-plain`, модель — дефолт харнесса.

Итого 170 ячеек: `cells/<TASK>__<COND>__<MODEL>__r<N>/` с `prompt.txt`,
`work/`, после прогона — `answer.md`, `meta.json`, `mech.json`, `judge*.json`.

Во всех условиях промпт = инлайн: инструкция-обёртка («Ты — ведущий
архитектор решений в ДКА банка… Ответ — только итоговый документ на русском»)
+ TASK.md + CONTEXT.md целиком. В `work/` агентных условий — копии
TASK.md/CONTEXT.md. Условие `spine-arch` добавляет в промпт строку про
ARCHITECTURE-SPINE.md/CONSTRAINTS.yaml и кладёт спайн-пакет в `work/`;
`*-arch` условия кладут `customization/architect.md` как AGENTS.md
(theseus-arch) / CLAUDE.md (claude-arch); `*-plain` — без кастомизаций.

### Условия — команды (проверены smoke 2026-09-09, 12/12 зелёный)
- `spine-arch`: `arch-be run -q --model {deepseek|glm-5.3-flash} --timeout 1100 "$(cat prompt.txt)"`, cwd=work/.
- `theseus-*`: `theseus -m {deepseek-flash|glm-5.3-flash} --yolo -p "$(cat prompt.txt)"`, cwd=work/.
- `claude-*`: `claude -p "$(cat prompt.txt)" --model deepseek-flash --output-format text --dangerously-skip-permissions`, cwd=work/.
- `raw-llm`: одиночный POST, dsf → api.deepseek.com (env DEEPSEEK_API_KEY,
  API-алиас `deepseek-chat` = deepseek-flash), glm → llm-proxy :8787
  (env ZHIPU_API_KEY), temperature 0.7, max_tokens 16000.
- Свип: `dsh --profile headless`, `codewhale exec`, `hermes -z`,
  `openclaw agent --local -m … --json --session-key pvbench-<TASK>`,
  `kimi -p` (~/.kimi-code/bin/kimi). Все из work/.

## Гипотезы

- **H1:** `spine-arch` > кодовые агенты plain (`theseus-plain`, `claude-plain`)
  по judge total.
- **H2:** кастомизация архитектора улучшает кодовых агентов
  (`*-arch` > `*-plain`), но не догоняет `spine-arch`.
- **H3:** эффект модель-зависим (различия dsf vs glm в величине эффектов).

## Метрики

- **Первичная:** judge total 0–100 (формула рубрик
  `100·Σw·s/(4·Σw)`, HF-cap `min(total,39)`).
- **Вторичные:** completeness (механические deliverables), HF-rate,
  evidence_unverified, время/объём генерации.

## Судья и отклонения от RUBRICS.md

- Модель судьи: **glm-5.3** (полная) через llm-proxy :8787 — независима от
  решателей-флэшей. temperature 0.
- **k=2** независимых прогона (рубрики требуют ensemble_size 3 — отклонение,
  принято по стоимости/времени), агрегация — среднее по критериям
  (рубрики: median_per_criterion — при k=2 среднее вместо медианы).
- **Один вызов на все критерии** (рубрики: schema §6 с checks[] агентных
  проверок — checks не выполняются, судья без инструментов).
- Ответы обезличены: судья получает TASK+CONTEXT+RUBRICS+answer, без имён
  ячеек/условий/харнессов.
- **max_tokens судьи 32768 вместо 8000** (отклонение от ТЗ): у glm-5.3
  thinking не отключается, reasoning съедает бюджет 8000 целиком
  (замерено: finish=length, пустой content, ~28–30К символов reasoning,
  3/3 пустых ответа при 8000). При 32768 вердикт возвращается стабильно.
- Верификация цитат: evidence ≥ 40 символов fuzzy-проверяется на вхождение
  в answer.md (нормализация пробелов/регистра); доля неверифицированных —
  `evidence_unverified` в judge.json.
- Идемпотентность: judge_k.json не перезаписываются; ячейка с k<2
  дожимается при перезапуске.

## Параметры прогона

- Таймаут ячейки 1200с (raw-llm — 600с), 2 ретрая (3 попытки), пауза 20/40с.
- Параллелизм: глобальный 6 потоков; семафоры: deepseek-канал ≤5,
  glm-канал (llm-proxy) ≤4, claude-прокси ≤2, свип ≤2.
- Пропуск ячейки, если answer.md ≥ 500 байт; ответ < 500 байт — сбой.
- Ответ raw-llm с finish_reason=length считается сбоем (не подмешивать
  обрезанные ответы в судейство).
- Извлечение ответов: stdout для arch-be (-q)/claude (text)/dsh/codewhale/
  hermes; theseus — последнее assistant-сообщение новейшей сессии
  work/.theseus/session-*.json (fallback: ANSI-strip stdout); openclaw —
  JSON payloads[].text; kimi — stdout минус служебный префикс «• ».

## Правило остановки

Прогон завершён, когда у всех 170 ячеек есть answer.md (или зафиксирован
сбой после 3 попыток) и judge.json с k=2 (или зафиксирован сбой судьи).
Ячейки со сбоем не подменяются и учитываются в summary отдельно.

## Отклонения от ТЗ и замечания (зафиксированы до прогона)

1. **PGL-ARCH-001 не содержит блока mechanical_checks** в RUBRICS.md
   (единственная из 8). Составлен override `runners/mech_overrides.yaml`
   (паттерны D1–D10 по шаблону §5 её TASK.md, детекторы HF-01/03/04/05
   в стиле остальных задач, HF-05 — derived от deliverables).
2. Формат mechanical_checks у авторов различается: кавычки ('/"),
   многострочные паттерны с ``` внутри (RDS D2), placeholder
   `__derived_from_deliverables__` (RDS HF-05, PGL override), id вида
   HF-03a/b/c (DTM). Парсер pvlib.py построчный, устойчив к обоим стилям;
   все паттерны всех 8 задач компилируются (проверено).
3. max_tokens судьи 32768 вместо 8000 — см. раздел «Судья».
4. CONSTRAINTS.yaml: типа max_words в arch-be control нет — проверка лимита
   слов выражена через `command_succeeds` (wc -w ≤ 4000).
5. raw-llm dsf использует API-алиас `deepseek-chat` (это и есть deepseek-flash;
   echo фиксируется в meta).
6. Факт среды: upstream Z.AI (open.bigmodel.cn через llm-proxy) деградирует
   окнами (HTTP 503 connection_error на больших запросах при живых мелких) —
   ретраи судьи с паузами 30/60/90с; незавершённые k=2 дожимаются перезапуском.

---

## Дополнение v2 (2026-09-09, до прогона v2)

Зафиксировано ДО запуска матрицы v2:

1. **Источник истины перенесён** из рабочего каталога эксперимента в
   монорепозиторий Spine: `benchmarks/platformv-arch-bench/`. Рабочий каталог
   `~/experiments/0909-platformv-arch-benchmark/` — зона выполнения (runs).
2. **Матрица v2** (на задачу): dsf × {spine-arch, spine-min, theseus-plain,
   theseus-arch, claude-plain, claude-arch, raw-llm} × 2; glm × {spine-arch,
   spine-min, theseus-plain, theseus-arch, raw-llm} × 2; glm53 (glm-5.3) ×
   {spine-arch, theseus-plain, raw-llm} × 1; dsp (deepseek-v4-pro) ×
   {spine-arch, claude-plain, raw-llm} × 1. Плюс свип 5 харнессов × 2 задачи.
3. **Новое условие `spine-min`**: arch-be без спайн-пакета (чистый эффект
   харнесса Spine против эффекта спайн-контекста; пара spine-arch vs
   spine-min добавлена в EFFECTS analyze.py).
4. **Задачи: 24** (8 исходных + 16 новых по продуктам/сквозным сценариям).
   На момент фиксации 8 задач не завершены (нет RUBRICS.md — прервано
   лимитом квоты оркестратора): DTM-RT-001, PGL-ARCH-002, PGL-ARCH-003,
   PGL-OLAP-001, PGL-SEC-001, RDS-LOCK-001, SEC-ARCH-001, SYN-ACL-001.
   Они входят в прогон после завершения и хэшируются отдельным дополнением.
5. **Фильтры запуска** (env): PVBENCH_TASKS / PVBENCH_CONDITIONS /
   PVBENCH_MODELS / PVBENCH_REPS / PVBENCH_NO_SWEEP / PVBENCH_JUDGE_K /
   PVBENCH_RUNS — для регрессионных поднаборов и переиспользования ячеек.
6. **Регрессионный гейт**: scripts/run_regression.sh + compare_runs.py;
   порог регрессии — падение среднего total spine-arch > 5 баллов.
7. **Судья v2**: число прогонов k задаётся PVBENCH_JUDGE_K (умолч. 2);
   при k=1 стабильность оценивается на подвыборке повторным судейством.
8. Хэши v2: см. prereg_hashes_v2.txt (корень каталога бенчмарка) (вычисляются после заморозки
   задач и раннеров v2, перед стартом массового прогона v2).
