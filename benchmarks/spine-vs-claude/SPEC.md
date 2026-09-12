# SPEC: A/B-бенчмарк «Spine vs Claude Code» (H0.1)

Статус: проект. Автор: harness. Дата фиксации: см. `prereg.lock.json`.

## 1. Зачем (какое утверждение проверяем)

Рецензия §3.1: **реализуемость ≠ эффект**. То, что харнесс `arch-ml` собирается,
запускается и выдаёт артефакты, не доказывает, что он даёт эффект на выходе.
Гипотеза:

> **H0.1.** На одинаковом корпусе и одинаковом механическом гейте рука
> `spine-arch` (агентный цикл Spine + MCP) даёт не худший `mech_score`, чем
> `claude-mcp` (Claude Code headless с тем же корпусом и тем же MCP), и строго
> лучший, чем `claude-plain` (голый Claude Code без корпуса и инструментов).

Проверяем **пре-регистрированным** дизайном: входы заморожены по sha256 ДО
первой генерации, гейт — один и тот же детерминированный для всех рук, отчёт
публикуется как есть (включая нули и падения).

Сопутствующие гипотезы:

- **H0.2 (подотчётность).** Руки с корпусом закрывают белое пятно (а):
  `DECISION.md` содержит именного аппрувера и `spec: sha256:<хэш>`, равный
  замороженному хэшу спеки. Доля `accountability_ok` выше у `spine-*`.
- **H0.3 (независимый eval-бар).** Ни одна рука не переписывает
  `ACCEPTANCE.md` (белое пятно (б): приёмочные тесты фиксирует человек ДО
  генерации). Метрика `eval_bar_ok` = `acceptance_untouched ∧ coverage_ok`,
  где `coverage_ok` — механическая сверка, что каждая `AC-<nn>` из
  замороженного `ACCEPTANCE.md` адресно покрыта строкой `<AC-id> -> <ADR-id>`
  в `TRACE.md` (ответ на рецензию §3.5: дословных цитат нет — есть адресный
  след).

## 2. Домен задачи

Задачи — архитектурные решения уровня AI/ML-исследователя (домен форка):
разделение train/serve, происхождение экспериментов, квоты GPU, миграция
реестра моделей. Проверяется не «красивый текст», а механически наблюдаемые
свойства пакета решений: инварианты spine, адресный след на приёмку,
подотчётность, предлагаемые fitness-правила.

## 3. Структура каталога

```
benchmarks/spine-vs-claude/
  SPEC.md                  # этот документ (спецификация; в задаче не участвует)
  README.md                # краткая навигация
  PREREGISTRATION.md       # замороженный дизайн: гипотезы, матрица, метрики, стоп-правило
  prereg.lock.json         # машинный слепок sha256 всех входов (пишет freeze.py)
  tasks/<TASK-ID>/         # 4 задачи; файлы ниже неизменяемы после freeze.py
    TASK.md                # постановка для испытуемого (входит в prompt)
    CONTEXT.md             # корпус предметной области (одинаков для всех рук)
    SPEC.md                # версия спеки; её sha256 цитируется в DECISION.md
    ACCEPTANCE.md          # независимый eval-бар: приёмочные тесты (пишет ЧЕЛОВЕК)
    CONSTRAINTS.yaml       # ГЕЙТ этой задачи: fitness-правила, одинаковы для всех рук
  runners/
    svclib.py              # общая библиотека (пути, env, схема записи)
    freeze.py              # prereg.lock.json + печать таблицы хэшей
    prepare_cells.py       # разворачивание cells/ по матрице
    run_matrix.py          # прогон рук (arch-ml run -q | claude -p [--mcp-config])
    gate.py                # ЕДИНЫЙ детерминированный гейт -> results.jsonl
    analyze.py             # results.jsonl -> summary.json (bootstrap CI)
    compare_runs.py        # регрессионный гейт между прогонами
    mcp.arch.json.tpl      # шаблон MCP-конфига для руки claude-mcp
    run_spine.sh           # тонкая обёртка: одна ячейка Spine
    run_claude.sh          # тонкая обёртка: одна ячейка Claude Code
  pack/                    # spine-пакет: каркас spine + шаблон журнала (руки с корпусом)
    ARCHITECTURE-SPINE.md
    DECISION.template.md
  runs/<RUN-ID>/           # создаётся прогоном (в git не попадает)
    cells/<CELL>/          #   ячейка: prompt.txt, work/, answer.md, meta.json,
                           #           gate.json, record.json
    results.jsonl          #   одна строка JSON на ячейку (контракт §8)
    summary.json           #   агрегат analyze.py (bootstrap CI)
    logs/generations.jsonl #   сырые события прогона
    deviations.md          #   отклонения от протокола (если были)
```

Правила: задачи, `CONSTRAINTS.yaml` и `ACCEPTANCE.md` — **неизменяемы** после
`freeze.py`. Правка любого из них = новый прогон с новым `RUN-ID`.

## 4. Руки (arms) и матрица

Руки образуют 2×2 «агентный цикл × корпус» плюс контрольная:

| Рука | Цикл | Корпус (spine-пакет) | MCP-инструменты |
|---|---|---|---|
| `spine-arch` | Spine `arch-ml run -q` | да | нативно (`arch-ml mcp serve`) |
| `spine-min` | Spine `arch-ml run -q` | нет | нативно |
| `claude-arch` | `claude -p` | да | нет |
| `claude-mcp` | `claude -p --mcp-config` | да | `arch-ml mcp serve` |
| `claude-plain` | `claude -p` | нет | нет |

Ключевые контрасты:

- **H0.1 основной:** `spine-arch` − `claude-mcp` (одинаковы корпус и
  механические инструменты; различается только агентный цикл).
- **H0.1 контроль:** `spine-arch` − `claude-plain` (полный эффект харнесса).
- **Разложение:** (`spine-arch` − `spine-min`) = вклад корпуса;
  (`claude-mcp` − `claude-arch`) = вклад MCP-инструментов.

Модели: `deepseek-flash` (основная), `glm-5.3-flash` (вторая, если доступна
через прокси). Одна и та же строка модели в обеих руках. Повторов: `REPS = 3`.

Объём: 4 задачи × 5 рук × 2 модели × 3 повтора = **120 ячеек**.

## 5. Корпус: что видит рука

Две разные вещи, их важно не смешивать:

- **Постановка (prompt.txt) — одинакова у ВСЕХ рук.** `prepare_cells.py`
  склеивает `TASK.md + CONTEXT.md + SPEC.md + ACCEPTANCE.md` в `prompt.txt`;
  это единственный текст задачи, и он побайтово один и тот же для всех ячеек
  одной задачи. Так «знание задачи» не является переменной.
- **Рабочий каталог `work/` — переменная.** База у всех одна: дерево
  репозитория-заготовки (`docs/`, `docs/adr/`, `model/`, пустые каталоги под
  артефакты). Сверх базы spine-пакет (`pack/ARCHITECTURE-SPINE.md` — каркас
  инвариантов, `pack/DECISION.template.md` — шаблон журнала) добавляется
  только рукам с корпусом: `spine-arch`, `claude-arch`, `claude-mcp` (§4).

Каркас `pack/ARCHITECTURE-SPINE.md` не должен проходить гейт сам по себе:
примеры заголовков внутри него закомментированы и отступлены, чтобы
`(?m)^#{2,3}\s*AD-\d+` не срабатывал на нетронутой заготовке.

`ACCEPTANCE.md` кладётся в `work/` **только для чтения** и защищён хэшем:
`gate.py` сверяет его sha256 с замороженным и фиксирует `acceptance_untouched`.

Каждый файл, посеянный в `work/`, заносится в `meta.json:seeded` (относительный
путь → sha256). Ячейка со всеми неизменёнными и недописанными файлами
распознаётся как `skipped_no_code` и получает `mech_score = 0` — «ничего не
делать» не даёт зелёного гейта.

## 6. Гейт (ОДИН, детерминированный, для всех рук)

Единая команда на ячейку:

```
arch-ml control check <RUN>/cells/<CELL>/work \
    --constraints tasks/<TASK>/CONSTRAINTS.yaml --json
```

Один и тот же `CONSTRAINTS.yaml` применяется ко всем рукам и повторам. LLM в
гейте не участвует. `gate.py` добавляет к отчёту fitness-контроля ровно три
детерминированные проверки, невыразимые правилами `RuleKind`:

1. **`spec_binding`** — sha256 строки `spec: sha256:<hex>` в `DECISION.md`
   равен замороженному хэшу `tasks/<TASK>/SPEC.md`.
2. **`acceptance_untouched`** — sha256 `work/ACCEPTANCE.md` равен замороженному.
3. **`trace_coverage`** — множество `AC-<nn>` из `ACCEPTANCE.md` ⊆ множество
   адресов в левой части строк `TRACE.md` (`<AC-id> -> <ADR-id>`), и каждый
   `<ADR-id>` из правой части существует в `work/docs/adr/` либо в spine.

Вердикт ячейки. **Единица счёта** — `rule` из `CONSTRAINTS.yaml` и каждая из
трёх семантических проверок выше:

```
units_total = rules_total + 3            # rules_total = число правил в CONSTRAINTS.yaml
units_green = rules_green + Σ(eval_bar/spec_binding проверки)   # 0..3
mech_score  = 100 · units_green / units_total
```

`rules_total` у задач различается: `model-registry-migration` — 12 правил
(`units_total = 15`), остальные три — 11 (`units_total = 14`). Знаменатель
зафиксирован **внутри** задачи и одинаков у всех рук и повторов — именно это
нужно парному контрасту §4. Разные знаменатели между задачами не выравниваются
искусственно: «добить» пак до 12-го правила означало бы вписать правило ради
числа, а не ради инварианта. При агрегации `mech_score` усредняется уже
безразмерным (в процентах), поэтому вес правила внутри задачи на итог не
влияет — влияет только вес задачи, и он одинаков для всех рук.

Пять булевых `deliverables` (`spine`, `adr`, `decision`, `trace`,
`acceptance_evidence`) **не входят в знаменатель**: существование каждого
файла-результата уже покрыто правилом `must_contain` (отсутствующий файл даёт
находку `error`), поэтому отдельная плата за него была бы двойным счётом. Они
фиксируются как диагностика для отчёта.

- `hard_fail` — есть находка `severity: critical` ЛИБО провалена любая из трёх
  проверок выше (кроме `gate = "skipped_no_code"`, где `mech_score = 0`);
- при `hard_fail` `mech_score` зажимается до `min(mech_score, 39)`;
- `mech_pass` — `mech_score ≥ 70` и нет `hard_fail`;
- `ok` — `mech_score ≥ 85` и нет `hard_fail`.

Порог 70/85 и зажим 39 — та же шкала, что в `platformv-arch-bench`
(совместимость отчётов).

Ячейка без единого файла-результата (`docs/` пуст) → `gate = "skipped_no_code"`,
она **не** выбрасывается: входит в знаменатель как провал.

## 7. Раннер

`run_matrix.py` идемпотентен: ячейка с готовым `answer.md` и `meta.json`
пропускается. Команды рук (cwd = `work/`, промпт лежит на уровень выше):

```bash
# spine-*
arch-ml run -q --model {model} --timeout 1100 "$(cat ../prompt.txt)"

# claude-plain / claude-arch
claude -p "$(cat ../prompt.txt)" --model {model} \
       --output-format text --dangerously-skip-permissions

# claude-mcp
claude -p "$(cat ../prompt.txt)" --model {model} \
       --output-format text --dangerously-skip-permissions \
       --mcp-config "$PWD/.mcp.arch.json"
```

Промпт передаётся содержимым файла (не путём), поэтому все руки получают
байт-идентичный текст. `runners/run_spine.sh` / `runners/run_claude.sh` —
ручные обёртки тех же команд для одной ячейки; матрицу гоняет `run_matrix.py`
(он вызывает бинари напрямую, без обёрток). stdout → `answer.md`, stderr →
`errors.txt`. Ненулевой код или ответ короче `MIN_ANSWER_BYTES = 500`
→ `meta.json:generation.error`, ячейка остаётся (провал учитывается гейтом).

## 8. Контракт результата

`runs/<RUN-ID>/results.jsonl` — одна строка JSON на ячейку (UTF-8, без
форматирования):

```json
{
  "run": "2026-09-11T10-00-00",
  "cell": "ml-serving-split__spine-arch__deepseek-flash__r1",
  "task": "ml-serving-split",
  "arm": "spine-arch",
  "model": "deepseek-flash",
  "rep": 1,
  "secs": 412.3,
  "exit_code": 0,
  "bytes": 8123,
  "gen_error": null,
  "gate": "ok",
  "passed": false,
  "mech_score": 73.3,
  "hard_fail": false,
  "errors_total": 5,
  "warns_total": 1,
  "rules_total": 12,
  "rules_green": 9,
  "units_total": 15,
  "units_green": 11,
  "per_rule_errors": {"deliverable-spine": 1, "trace-addressed": 3},
  "per_rule_warns": {"migration-phases": 1},
  "deliverables": {
    "spine": true, "adr": true, "decision": true,
    "trace": false, "acceptance_evidence": false
  },
  "accountability_ok": true,
  "spec_binding": true,
  "eval_bar": {"acceptance_untouched": true, "trace_coverage": false,
               "coverage_ok": false},
  "trace": {"acs_total": 6, "acs_covered": 4, "acs_orphan": ["AC-05", "AC-06"],
            "adr_unresolved": ["ADR-002"]},
  "outcome": {"found_by_gate": 5, "missed_by_gate": 2, "time_to_green_secs": null},
  "judge": null,
  "produced_files": ["DECISION.md", "docs/adr/ADR-001-registry.md", "TRACE.md"]
}
```

Инварианты записи:

- `mech_score ∈ [0, 100]`; `hard_fail ⇒ mech_score ≤ 39`;
- `units_total = rules_total + 3` (число единиц счёта детерминировано и не
  зависит от руки — знаменатель гейта один и тот же для всех ячеек **одной
  задачи**; `rules_total` у задач разный, см. §6);
- `gate ∈ {"ok","skipped_no_code","gate_error"}`; при `gate != "ok"` поля
  правил пусты, `mech_score = 0`;
- `found_by_gate` — число находок error, `missed_by_gate` — число адресных
  ссылок `TRACE.md`, не подтверждённых ни файлом, ни правилом (пустой след);
- `produced_files` — файлы в `work/`, отличающиеся от посева `meta.json:seeded`
  (или отсутствовавшие там). Непустой список — условие `gate = "ok"`; он же
  аудиторский след того, что рука действительно писала;
- `judge` — необязательный вторичный слой (LLM-судья по рубрике), `null` в
  основном прогоне: H0.1 проверяется механически.

`done` фиксируется отдельно: `runs/<RUN-ID>/meta.json` (`{run, git_rev,
prereg_lock_sha256, arch_ml_version, started_at, finished_at, cells_total,
cells_done}`) и `runs/<RUN-ID>/logs/generations.jsonl` (сырые события прогона).

## 9. Агрегация и стоп-правило

`analyze.py` → `runs/<RUN-ID>/summary.json`:

- `by_arm`: `{n, mech_pass_rate, mech_score_mean, accountability_rate,
  eval_bar_rate, mean_secs, hard_fail_rate}` + диагностика
  `{skipped_no_code_rate, gen_error_rate}`;
- `by_arm_model`, `by_task`;
- `effects`: парные разности средних `mech_score` (bootstrap 95% CI,
  10000 ресэмплов, seed 42) для контрастов §4;
- `primary`: `H0.1` — вердикт `supported | rejected | inconclusive` по CI.

Правило принятия (фиксируется ДО прогона):

- `H0.1 supported` ⟺ нижняя граница CI разности `spine-arch − claude-plain`
  > 0 **и** нижняя граница CI разности `spine-arch − claude-mcp` ≥ −5
  (не хуже контрольной руки с тем же корпусом и MCP);
- `inconclusive` — CI накрывает 0;
- `rejected` — верхняя граница CI разности с `claude-plain` ≤ 0.

Регрессионный гейт: `compare_runs.py <BASELINE> <CURRENT> --threshold 5` —
exit 1, если средний `mech_score` руки `spine-arch` упал более чем на 5 пунктов.

## 10. Ограничения и честность отчёта

- Прогон требует ключей (`DEEPSEEK_API_KEY`, прокси `claude`), поэтому в
  репозиторий попадают **только харнесс и скрипты**; `runs/` и `cells/` — не
  в git. Первый прогон выполняет человек, скрипты печатают точную команду.
- Дизайн замораживается `freeze.py` ДО генераций; любая правка `tasks/**`
  инвалидирует `prereg.lock.json` (проверяется в `analyze.py`).
- Результат публикуется как есть, включая отрицательный: провалившиеся и
  `skipped_no_code` ячейки остаются в отчёте.
- Внешний фрейминг: EU AI Act не является предметом бенчмарка. Подотчётность
  (H0.2) и независимый eval-бар (H0.3) измеряются как доменные свойства
  артефакта — именной аппрувер, журнал решений, привязка к версии спеки,
  механическая сверка покрытия приёмки. Комплаенс-чеклистов в гейте нет.
