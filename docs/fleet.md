# Флот прогонов: изоляция worktree и гейт мерджа владельца

Мотив — сравнительный разбор «Харнесс vs Anthropic SDLC» (AI-native SDLC
playbook Anthropic): у Anthropic «агент не аппрувит свой код и не имеет пути
в main» — enforced платформой, а не дисциплиной промптов. Здесь то же
закрывается конфигурацией поверх существующей worktree-фабрики
(`src/worktree.rs`).

## Два переключателя: `[fleet]`

```toml
[fleet]
require_worktree = true    # прогоны харнессов — только в изолированном worktree
merge_gate = "owner"       # мерж результата — только с подтверждением владельца
```

Оба дефолта выключены/совместимы с прежним поведением: `require_worktree =
false` (прогон в рабочем дереве как раньше), `merge_gate = "owner"` (гейт
включён для `arch fleet merge`; прямые `arch worktree accept` им не
затрагиваются).

## Enforcement: нет пути в main (`require_worktree`)

Когда `require_worktree = true`, прогон кодового харнесса — инструмент
`harness_run` и CLI `arch harness-run` — обязан идти в изолированном git
worktree:

1. Создаётся worktree фабрики: ветка `arch/<харнесс>-<timestamp>` (run-id,
   напр. `claude-code-20260825143000`), каталог — вне репозитория
   (`~/.arch-ml/worktrees/<repo-slug>/<run-id>`).
2. Handoff-пакет `.arch-handoff/` по контракту в git не коммитится, поэтому
   копируется в worktree автоматически — исполнитель получает TASK.md и
   MANIFEST.json как обычно.
3. Харнесс работает с `cwd` = worktree; авто-коммит хвоста (если включён)
   фиксирует работу в ветке worktree. Основное дерево не изменяется ни на
   байт.
4. Каталог не git-репозиторий — понятная ошибка ДО запуска харнесса.

Сводка прогона сообщает run-id и дальнейшие шаги; merge — только через гейт
владельца (см. ниже), отклонение — `arch worktree drop <run-id>`.

## Гейт мерджа: `arch fleet merge <run-id>`

Интеграция результата прогона в основную ветку — решение владельца:

```bash
arch fleet merge claude-code-20260825143000                 # review: сводка, мерж ОТКЛОНЁН (exit 1)
arch fleet merge claude-code-20260825143000 --owner-approve # влить (merge --no-ff + уборка worktree)
```

Без `--owner-approve` (при `merge_gate = "owner"`; неизвестные значения
трактуются как `owner` — безопасная интерпретация) печатается сводка для
решения человеком:

- коммиты ветки и `git diff --stat` против основного дерева;
- состояние worktree (незакоммиченные изменения подсвечиваются — merge их
  не подхватит);
- статус контракта результата (`status=complete|partial|blocked`) из лога
  прогона `reports/harness/*.log`, если лог с этим run-id найден (evidence).

С `--owner-approve` выполняется accept-семантика worktree-фабрики: merge
`--no-ff` в текущую ветку основного дерева, удаление worktree и ветки.
Отказ при незакоммиченных изменениях — как у `arch worktree accept`.
`merge_gate = "none"` отключает гейт (мерж без флага).

Связанное: `arch fleet audit` — SSOT-аудит дублей/дрейфа копий спайна
(см. README и `кейсы/fleet-spine-drift`); для каждого worktree показывает
размер на диске (`du -sh`, недоступен — «—») и возраст последнего коммита
(`git log -1`, не-git — «—»), а для worktree старше 30 дней — рекомендацию
prune (`git worktree remove`); `arch worktree …` — ручная
фабрика worktree (`docs/slash_commands.md`, `/worktree`).

## Планы флота: паттерны оркестрации (ADR-042)

Веер — не единственная топология передачи архитектуры в работу. Команда
`arch-ml fleet plan|run|resume` исполняет **план флота**: паттерн оркестрации
(`fanout`, `pipeline`, `map_reduce`, `tournament`, `review_pair`, `ralph`,
`walking_skeleton`, `dag`) разворачивается в граф узлов, узлы идут волнами по
топологическим уровням, конкурентность ограничена пулом (`max_parallel`),
результат каждого узла проходит механические гейты, а интеграция в main
остаётся за владельцем (`fleet merge --owner-approve`).

- каталог паттернов, схема плана, гейты и resume — [fleet_patterns.md](fleet_patterns.md);
- `arch-ml fleet plan propose --repo .` — черновик плана по
  `.arch-handoff/MANIFEST.json` (паттерн по маршруту значимости);
- `arch-ml fleet plan validate <plan>` — механический гейт для CI;
- журнал прогона — `state/fleet/<run-id>.jsonl`, рядом снимок плана
  (`<run-id>.plan.toml`), поэтому `fleet resume` самодостаточен.
