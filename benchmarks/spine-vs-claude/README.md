# spine-vs-claude

A/B-бенчмарк H0.1: даёт ли `arch-ml` эффект на выходе против голого агента
(Claude Code headless) на одном корпусе и одном детерминированном гейте.

- Дизайн и вся спецификация: [`SPEC.md`](SPEC.md)
- Замороженный дизайн и гипотезы: [`PREREGISTRATION.md`](PREREGISTRATION.md)
- Задачи: `tasks/<TASK-ID>/{TASK,CONTEXT,SPEC,ACCEPTANCE}.md` + `CONSTRAINTS.yaml`
- Корпус руки с корпусом: `pack/{ARCHITECTURE-SPINE.md,DECISION.template.md}`
  (первый — дистиллят-образец, второй — болванка журнала решений)
- Гейт: один на все руки — `arch-ml control check <cell>/work --constraints ... --json`

## Порядок прогона (выполняет человек, нужны ключи)

```bash
cd benchmarks/spine-vs-claude
export SVC_RUN=2026-09-11T10-00-00        # один RUN-ID на весь прогон
python3 runners/freeze.py --write         # 1. заморозить входы -> prereg.lock.json
python3 runners/prepare_cells.py "$SVC_RUN"   # 2. развернуть ячейки по матрице
python3 runners/run_matrix.py             # 3. генерации (Spine | Claude Code)
python3 runners/gate.py                   # 4. единый детерминированный гейт
python3 runners/analyze.py                # 5. агрегация -> summary.json
```

Ячейки живут в `runs/<RUN-ID>/cells/<TASK>__<ARM>__<MODEL>__r<REP>/`; внутри —
`prompt.txt`, `work/` (то, что правит рука), `answer.md`, `meta.json`,
`gate.json`, `record.json`. `results.jsonl` и `summary.json` — на уровне прогона.

Полезные сужения (отладка, не для зачётного прогона):

```bash
python3 runners/prepare_cells.py "$SVC_RUN" --reps 1 --models deepseek-flash
python3 runners/run_matrix.py --dry-run        # напечатать команды, ничего не запускать
python3 runners/run_matrix.py --only-task gpu-quota-policy --only-arm spine-arch
python3 runners/gate.py "$SVC_RUN" --force     # агрегировать даже при prereg_mismatch
```

Регрессия между прогонами:

```bash
python3 runners/compare_runs.py runs/<BASE> runs/<CUR> --threshold 5
```

Ручной прогон одной ячейки (те же команды, что у матрицы):

```bash
runners/run_spine.sh  runs/<RUN>/cells/<CELL>
runners/run_claude.sh runs/<RUN>/cells/<CELL>     # MCP подключится сам, если .mcp.arch.json на месте
```

## Оговорки

- `arch-ml control check --json` печатает отчёт в stdout и возвращает **1**,
  когда репозиторий гейт не прошёл. Это не ошибка: `gate.py` разбирает stdout
  и не смотрит на код возврата (код сохраняется в `gate.json:returncode`).
- Ячейка без правок (`work/` совпадает с посевом из `meta.json:seeded`) даёт
  `gate: "skipped_no_code"` и `mech_score = 0` — «ничего не сломал» не засчитывается.
- Любая правка `tasks/**`, `pack/**`, `SPEC.md` или `PREREGISTRATION.md` после
  `freeze.py` инвалидирует прогон: `analyze.py` пометит его `prereg_mismatch`.

Зависимости: Python 3.11+ stdlib, `arch-ml` в `PATH` (или `SVC_ARCH`),
`claude` для руки Claude Code. Новых зависимостей нет ни у харнесса, ни у
скриптов.
