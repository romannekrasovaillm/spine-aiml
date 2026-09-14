# Подключение hypothesis-router к Spine

## 0. Установка
```bash
cp -r hypothesis-router <spine>/aiml/plugins/hypothesis-router
export HYPOTHESES_DIR=~/hypotheses          # теплица карточек
<spine>/aiml/plugins/hypothesis-router/hooks/hypothesis-hook.sh refresh
```
Первая карточка — `skills/hypothesis-card/references/example-laguna.md`
→ `~/hypotheses/laguna-gb10-ladder/HYPOTHESIS.md`.

## 1. Без правки ядра (работает сразу)
- В `library/fitness/` положить `hypothesis-routing.yaml` **в схеме движка**
  (`constraints:` + `command_succeeds`), а не в формате плагина: движок
  вложенный `check:` и плейсхолдеры не понимает, а из манифеста читает
  только `name/version/description/keywords/hooks`. Правило
  `hypothesis_handoff_attached` покажет, что pre-handoff не выполнялся.
  Готовая копия — `aiml/library/fitness/hypothesis-routing.yaml` этого
  репозитория; в самом плагине файла правил нет намеренно (один источник).
- В сценарий `handoff_create` (скрипт/алиас, которым вы его запускаете)
  добавить строку `hypothesis-hook.sh pre-handoff <repo>` перед сборкой.
- В команду принятия worktree — `hypothesis-hook.sh post-accept
  <repo>/.arch-handoff <имя проекта>`.
- Слэш-команда `/hypotheses` — для ручного подъёма из TUI.

## 2. С правкой ядра (по ADR-draft)
Три события хуков в `harness.rs` / точке accept, контракт: процесс,
аргументы позиционные, stdout — одна строка для TUI, exit 0/2/3.
Регистрация — из `plugin.json` → `hooks`. Ядро не зависит от плагина
(AD-1), хук без LLM (AD-2).

## 3. Что читает детерминированный слой
`$HYPOTHESES_DIR/routing.json`:
```json
{"min_score": 2, "rows": [{"trigger": "rollouts_log.jsonl", "kind": "files",
  "weight": 3, "card": "laguna-gb10-ladder", "state": "latent",
  "skills": ["laguna-ladder-run", "ood-stage-eval"]}]}
```
Правило подъёма: сумма весов совпавших триггеров карточки ≥ min_score.
Можно перенести в `detect_diff_triggers`-подобный сенсор: появление файла
по маске из routing.json — само по себе триггер значимости `hypothesis_hit`.

## 4. Контракт HYPOTHESES.json в handoff-пакете
```json
{"generated_at": "...", "min_score": 2, "max_cards": 8, "dedupe": 0.5,
 "hits": [{"card": "...", "title": "...", "state": "latent", "score": 9, "norm": 0.75,
           "hits": {"files": [...], "keys": [...], "deps": [...], "words": [...]},
           "skills": [...], "plugins": [...], "promote_when": "..."}],
 "suppressed": [{"card": "...", "suppressed_by": "...", "jaccard": 0.7778}]}
```
`hits` — только поднятые карточки: не больше `HYPOTHESES_MAX_CARDS`
(по умолчанию 8 для pre-handoff, 5 для intent) и без почти-дублей по
совпавшим триггерам (`HYPOTHESIS_DEDUPE`, по умолчанию 0.5). Подавленные
не выбрасываются: пара и коэффициент Jaccard — в `suppressed`.
`norm` — балл к потолку карточки (`3*|files| + 3*|keys| + 2*|deps| + 1*|words|`),
чтобы сравнивать карточки с разным числом триггеров. Поля `card`, `state`,
`score`, `skills` на месте — ядро читает их как раньше, лишние поля игнорирует.

MANIFEST.json получает ключ `hypotheses` (card/state/score/norm/skills) и
`skills` — объединение скиллов **только поднятых** карточек (скиллы прошлых,
более широких прогонов в пакет не переносятся). TASK.md — только с
`HYPOTHESIS_ANNOTATE=1`, блок ≤ 400 символов (бюджет epic-context).
