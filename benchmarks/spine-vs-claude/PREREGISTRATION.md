# Пререгистрация: spine-vs-claude (H0.1)

Дизайн зафиксирован **до** первой генерации. Все входы — задачи, корпус,
приёмка, гейт — заморожены по sha256 (`prereg.lock.json`, пишет
`runners/freeze.py`). Отчёт публикуется как есть.

## 0. Слепок входов

Файл `prereg.lock.json` (машиночитаемо) и таблица ниже (заполняет
`freeze.py`) фиксируют побайтовый хэш каждого входа. Прогон, чьи хэши не
совпали со слепком, `analyze.py` помечает `prereg_mismatch` и не агрегирует.

<!-- HASH-TABLE-START -->
| Вход | sha256 |
|---|---|
| harness | `arch-ml 0.1.4` |
| `SPEC.md` | `0bf140c311841de6…` |
| `pack/ARCHITECTURE-SPINE.md` | `6db56a5eff8bcd47…` |
| `pack/DECISION.template.md` | `96daf37bf10a4066…` |
| `tasks/experiment-lineage/ACCEPTANCE.md` | `abbe18f65434cd5c…` |
| `tasks/experiment-lineage/CONSTRAINTS.yaml` | `35006cbd54caf6c1…` |
| `tasks/experiment-lineage/CONTEXT.md` | `a84a15cbd44ad6ee…` |
| `tasks/experiment-lineage/SPEC.md` | `2344eb6d3e58782a…` |
| `tasks/experiment-lineage/TASK.md` | `87afac78bb6cdb06…` |
| `tasks/gpu-quota-policy/ACCEPTANCE.md` | `3cbed34bda8c1370…` |
| `tasks/gpu-quota-policy/CONSTRAINTS.yaml` | `22f141620c9c585b…` |
| `tasks/gpu-quota-policy/CONTEXT.md` | `b9d1ab91151c30ba…` |
| `tasks/gpu-quota-policy/SPEC.md` | `b7f5c71b926db9f1…` |
| `tasks/gpu-quota-policy/TASK.md` | `73f440ee0524cba9…` |
| `tasks/ml-serving-split/ACCEPTANCE.md` | `5f0faf3f2970c2e8…` |
| `tasks/ml-serving-split/CONSTRAINTS.yaml` | `dbdd6395cd20a2f7…` |
| `tasks/ml-serving-split/CONTEXT.md` | `085ba86c47d903e4…` |
| `tasks/ml-serving-split/SPEC.md` | `c2777ddb8c7099c4…` |
| `tasks/ml-serving-split/TASK.md` | `b5cc6381c7139e1e…` |
| `tasks/model-registry-migration/ACCEPTANCE.md` | `180292621f59da1d…` |
| `tasks/model-registry-migration/CONSTRAINTS.yaml` | `968b47788135f1c3…` |
| `tasks/model-registry-migration/CONTEXT.md` | `f6afa9aa0d185aa6…` |
| `tasks/model-registry-migration/SPEC.md` | `c829be8343e10dec…` |
| `tasks/model-registry-migration/TASK.md` | `17a47c52ba877ee8…` |
<!-- HASH-TABLE-END -->

## 1. Вопрос и гипотезы

**Вопрос.** Даёт ли агентный харнесс `arch-ml` измеримый эффект на выходе —
при прочих равных (та же модель, тот же корпус, тот же детерминированный
гейт) — против Claude Code headless?

- **H0.1** — `spine-arch` не хуже `claude-mcp` (разность средних
  `mech_score` в пределах −5) и строго лучше `claude-plain` (нижняя граница
  95% CI разности > 0).
- **H0.2 (подотчётность)** — доля `accountability_ok` в руках `spine-*`
  выше, чем в `claude-*` без MCP.
- **H0.3 (независимый eval-бар)** — доля `eval_bar_ok` (`acceptance_untouched
  ∧ coverage_ok`) выше в руках с корпусом; регресс — любая рука, изменившая
  `ACCEPTANCE.md`.

Гипотезы сформулированы до прогона; отрицательный результат публикуется.

## 2. Матрица

| Фактор | Значения |
|---|---|
| Задача | 4: `ml-serving-split`, `experiment-lineage`, `gpu-quota-policy`, `model-registry-migration` |
| Рука | `spine-arch`, `spine-min`, `claude-plain`, `claude-arch`, `claude-mcp` |
| Модель | `deepseek-flash`, `glm-5.3-flash` |
| Повтор | 3 (`r1..r3`) |

Итого **120 ячеек** `cells/<TASK>__<ARM>__<MODEL>__r<N>/`.

Ячейка = `prompt.txt` (фиксированный текст: TASK + CONTEXT + SPEC + приёмка),
`work/` (рабочий каталог руки), `answer.md` (stdout руки), `meta.json`,
`gate.json` (отчёт гейта), `record.json` (строка `results.jsonl`).

## 3. Команды рук

Зафиксированы дословно; cwd = `work/`.

```bash
# spine-arch | spine-min
arch-ml run -q --model {model} --timeout 1100 "$(cat prompt.txt)"

# claude-plain | claude-arch
claude -p "$(cat prompt.txt)" --model {model} --output-format text --dangerously-skip-permissions

# claude-mcp  (+ work/.mcp.arch.json из runners/mcp.arch.json.tpl)
claude -p "$(cat prompt.txt)" --model {model} --output-format text \
       --dangerously-skip-permissions --mcp-config work/.mcp.arch.json
```

## 4. Гейт

Один детерминированный гейт на все ячейки:

```bash
arch-ml control check <RUN>/cells/<CELL>/work \
    --constraints tasks/<TASK>/CONSTRAINTS.yaml --json
```

плюс три детерминированные проверки в `gate.py` (`spec_binding`,
`acceptance_untouched`, `trace_coverage`). LLM-судья в основной прогон не
входит (`judge = null`). Шкала: `mech_score ∈ [0,100]`, зажим до 39 при
`hard_fail`, `mech_pass ≥ 70`, `ok ≥ 85`.

## 5. Метрики

| Метрика | Уровень | Смысл |
|---|---|---|
| `mech_score` | первичная | доля зелёных правил гейта, 0..100 |
| `mech_pass` | первичная | `mech_score ≥ 70` и нет hard-fail |
| `accountability_ok` | вторичная (H0.2) | именной аппрувер + привязка к хэшу спеки |
| `eval_bar_ok` | вторичная (H0.3) | приёмка не тронута ∧ покрытие адресного следа полное |
| `hard_fail_rate` | вторичная | доля ячеек с критической находкой |
| `time_to_green_secs` | вторичная | время руки до первого зелёного гейта (если измерено рукой) |
| `found_by_gate` / `missed_by_gate` | вторичная | обнаружено гейтом / пропущено гейтом |

Ноль отличий считается за отсутствие эффекта, а не за успех.

## 6. Агрегация и статистика

`analyze.py`: средние по руке, руке×модели, задаче; парные разности средних
`mech_score` с bootstrap 95% CI (10000 ресэмплов, seed 42). Основной контраст
— `spine-arch − claude-plain`; контрольный — `spine-arch − claude-mcp`.
Вердикт H0.1 — по правилу §9 `SPEC.md`.

## 7. Стоп-правило

- Прогон останавливается, когда все 120 ячеек завершены (успех или сбой
  генерации) либо превышен лимит `SVC_MAX_SECS` (дефолт 6 ч).
- Незавершённые ячейки не досочиняются: они входят как `skipped_no_code`.
- Никакого «добора» повторов ради значимости: число повторов фиксировано (3).

## 8. Отклонения от протокола

Фиксируются в `runs/<RUN-ID>/deviations.md` (например, недоступность модели
`glm-5.3-flash` → прогон по одной модели; тогда в `summary.json`
`deviations: [...]`, а H0.1 считается только по `deepseek-flash`).
