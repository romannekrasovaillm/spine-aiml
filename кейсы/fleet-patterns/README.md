# Кейс 010 — `fleet-patterns`: движок оркестрации флотов по паттернам

> **Что показывает кейс.** Не веер, а **граф**: один инструмент исполняет
> разные топологии передачи архитектуры в работу кодовым агентам — конвейер,
> веер с интегратором, турнир, пару «реализатор + ревьювер», canary,
> произвольный DAG. Паттерн разворачивается в узлы и рёбра, узлы идут волнами
> по топологическим уровням, каждый результат проходит механический гейт, а
> интеграция в main остаётся за владельцем.
>
> **For English readers:** a pattern-driven fleet engine — the pattern is sugar
> over one DAG; nodes run in topological waves through a bounded process pool,
> every node result passes deterministic gates, and merging into main stays
> behind the owner gate (ADR-042).

- **Кейс механический**: воспроизводится голым бинарём без LLM, сети и ключей
  (кодовый харнесс — shell-скрипт с JSON-контрактом).
- **Живой прогон**: 2026-09-12, бинарь этой сборки; все выводы в `evidence/` —
  дословные.
- **Не путать с кейсом 005** ([`fleet-of-ten`](../fleet-of-ten/)): там десять
  реальных Claude Code в одной топологии (веер). Здесь — движок топологий.

## Что проверяется

| Проверка | Где видно |
|---|---|
| План компилируется: паттерн → граф, топологические волны | `evidence/plan-mapreduce.txt` |
| Веер исполняется: worktree-на-узел, гейт контракта, exit 0 | `evidence/run-fanout.txt` |
| Resume: завершённые узлы не перезапускаются | `evidence/run-fanout.txt` (второй блок) |
| Отказ гейта останавливает масштабирование, зависимые пропущены, exit 1 | `evidence/pipeline-gate-fail.txt` |
| Вердикт ревьювера NOT-READY блокирует интеграцию, exit 1 | `evidence/review-not-ready.txt` |
| Все восемь паттернов проходят `plan validate` | `tests/fleet_patterns.rs` |
| **Живой прогон на реальном харнессе** (kimi-code): контракт complete + fitness 8/8 | `evidence/live-kimi-fanout.txt` |
| Зависимый узел видит код зависимости (ревьювер/интегратор не слепы) | `tests/fleet_patterns.rs::dependent_node_sees_dependency_work_in_its_worktree` |
| **Живой review_pair**: NOT-READY заблокировал, находки проверены | `evidence/live-review-pair.txt` |
| **Замыкание контура**: находка → правило → PASS/FAIL механически | `evidence/live-rules-upgrade.txt` |

## Как повторить

Нужен только собранный бинарь (`cargo build --release`), git и POSIX-shell.

```bash
# 1) Герметичная среда: git-репозиторий + фейковый харнесс + config с путями внутри каталога
R=/tmp/fleet-patterns && rm -rf $R && mkdir -p $R/bin $R/state $R/repo
cat > $R/bin/fake-harness <<'EOF'
#!/bin/sh
cat > /dev/null
echo 'работа сделана'
echo '```json'
echo '{"status":"complete","assumptions":["готово"],"open_questions":[],"conflicts_with_prior_decisions":[]}'
echo '```'
EOF
chmod +x $R/bin/fake-harness
cd $R/repo && git init -q -b main . && git config user.email t@t \
  && git config user.name t && echo x > README.md && git add -A && git commit -qm base

cat > $R/config.toml <<EOF
[paths]
state_dir = "$R/state"
reports_dir = "$R/reports"
sessions_dir = "$R/sessions"
assets_dir = "$R/assets"
[harnesses.fake]
binary = "$R/bin/fake-harness"
prompt_mode = "stdin"
timeout_secs = 60
idle_timeout_secs = 30
auto_commit = true
EOF

# 2) Паттерны: validate всех восьми фикстур и просмотр графа
B=/path/to/arch-ml
for f in tests/fixtures/fleet/*.plan.toml; do $B fleet plan validate $f; done
$B fleet plan show tests/fixtures/fleet/map_reduce.plan.toml
$B fleet plan show tests/fixtures/fleet/pipeline.plan.toml --mermaid

# 3) Прогон веера (evidence/fanout.plan.toml) и resume
$B --config $R/config.toml fleet run --repo $R/repo --plan evidence/fanout.plan.toml
$B --config $R/config.toml fleet resume <run-id> --repo $R/repo

# 4) Отказ гейта и блокировка зависимых
$B --config $R/config.toml fleet run --repo $R/repo --plan evidence/pipeline-fail.plan.toml; echo "exit=$?"

# 5) Пара «реализатор + ревьювер» (ревьювер с вердиктом NOT-READY)
$B --config $R/config.toml fleet run --repo $R/repo --plan evidence/review-pair.plan.toml; echo "exit=$?"
```

Пункт 5 требует фейкового харнесса с состязательным ответом: на формулировку
ревьювера («Ты НЕ проектировал…») он печатает
`{"verdict":"NOT-READY","findings":[…]}` вместо контракта — так проверяется,
что LLM-вердикт блокирует интеграцию (гейт `review`).

## Живой прогон (2026-09-12)

Помимо механических тестов, движок прогнан на **реальном кодовом харнессе**
(`kimi-code`, песочница `~/experiments/fleet-live-20260912`):

- пакет `.arch-handoff/` сгенерирован `arch-ml handoff` из спайна AD-1/AD-2,
  CONSTRAINTS переписаны под инварианты (8 правил: file_exists, must_contain
  на `Decimal`/`ROUND_HALF_UP`, must_not_contain на `print`/сеть,
  `command_succeeds` с pytest);
- `fleet run --plan live-smoke-kimi.plan.toml` — узел в worktree, 97.3 с,
  exit 0, контракт `complete`; гейты: `contract` ok и `fitness`
  **«Правил: 8, нарушений: 0»**; агент сам закоммитил работу (контракт
  «Финализация»);
- дословный вывод и код агента — `evidence/live-kimi-fanout.txt`.

### review_pair на живом агенте: ревьювер нашёл то, что гейт пропустил

Второй прогон — `live-review.plan.toml` (реализатор + состязательный
ревьювер, оба узла на `kimi-code`). Реализатор отработал 116 с, гейты
`contract` + `fitness` — зелёные; движок влил его ветку в дерево ревьювера
(событие `dependency:impl → ok`), ревьювер отработал 128 с и вынес
**NOT-READY** — узел заблокирован, прогон завершился exit 1.

Пять находок ревьювера проверены вручную, все подтвердились
(`evidence/live-review-pair.txt`):

| # | Находка | Проверка |
|---|---|---|
| F1 | Тесты «половина копейки вверх» **не отличают** Decimal+HALF_UP от запрещённой float-реализации | `round(0.005,2)=0.01`, `round(0.025,2)=0.03` — float-версия проходит оба ассерта; дискриминирующий случай `calc_fee(10.0,125.0)`: HALF_UP 0.13 против float 0.12 — в наборе отсутствует |
| F2 | fitness-правила пакета **слабее, чем декларируют** | `must_contain "Decimal"` проходит на неиспользуемом импорте; `import (urllib\|http\|…)` обходится формой `from urllib.request import …`; правила «ровно одна публичная функция» нет вовсе |
| F3 | `nan` молча возвращается, `inf` бросает необработанный `InvalidOperation`; SPEC — незаполненный шаблон, а контракт вернул `complete` без `open_questions` | воспроизведено |
| F4 | Нет теста отрицательных сумм (возвраты/сторно): Decimal HALF_UP даёт −0.13, float −0.12 | воспроизведено |
| F5 | Конфликт контракта (float на границе) с AD-2 (точные деньги) не поднят как `open_question` | по тексту TASK/SPEC |

**Замыкание контура** (находка → машинное правило, без LLM и без денег):
CONSTRAINTS ужесточены до v2 (`evidence/live-constraints-v2.yaml` —
`quantize(` вместо импорта, запрет `\bround(` и `from … import`-форм сети,
обязательный дискриминирующий тест тай-брейка). Проверка
`control check` (`evidence/live-rules-upgrade.txt`):

- код агента (Decimal + quantize + HALF_UP) — **PASS, 10 правил, 0 нарушений**;
- отрицательный контроль (наивный `round(amount*bps/10000, 2)` с неиспользуемым
  импортом `Decimal` и без теста тай-брейка) — **FAIL, 3 error**:
  `ad2_quantize_used`, `ad2_no_builtin_round`, `tests_discriminate_tie`.

Это и есть тезис контура: прозаическая находка ревьювера превращается в
машинное правило, которое ловит нарушение **детерминированно** — на следующем
прогоне гейт уже не пропустит то, что пропустил.

**Честная граница прогона:** Claude Code в этом сеансе запустить не удалось —
классификатор разрешений среды дважды отклонил команду, поднимающую
`claude -p --dangerously-skip-permissions` (флаг из конфига адаптера). Это
ограничение оператора, а не движка: `fleet run` с узлом на другом настроенном
харнессе прошёл сразу. Claude Code можно прогнать тем же планом, заменив
`agent` (или убрав его — роутер возьмёт первый харнесс конфига).

## Анатомия

```
README.md            этот файл
evidence/
  fanout.plan.toml         план веера (2 узла, worktree, гейт contract)
  pipeline-fail.plan.toml  конвейер с падающим гейтом стадии A
  review-pair.plan.toml    реализатор + автоузлы ревьювера
  run-fanout.txt           дословный вывод: прогон + resume
  pipeline-gate-fail.txt   дословный вывод: остановка и пропуск зависимого
  review-not-ready.txt     дословный вывод: NOT-READY → exit 1
  plan-mapreduce.txt       дословный вывод: граф с достроенным интегратором
```

Фикстуры всех паттернов — `tests/fixtures/fleet/*.plan.toml`; автотесты
движка — `tests/fleet_patterns.rs` (12 тестов, офлайн).

## Чему учит кейс

- **Топология — это данные, а не код**: восемь паттернов живут в одном
  компиляторе и одном исполнителе; новый паттерн — правило развёртки, а не
  второй оркестратор.
- **Детерминированный судья снимает спор о вкусе**: турнир выбирает первый по
  id узел, прошедший гейты; вердикт LLM-ревьювера только блокирует.
- **Найдена живая гонка**: журнал, писавший строку `writeln!` (payload и `\n`
  двумя вызовами), склеивал записи конкурентных узлов — на десяти узлах это
  почти не видно, на сотнях ломает resume. Починено одним `write_all`,
  регрессия зафиксирована тестом.
- **Resume — свойство журнала, а не удачи**: детерминированное имя worktree
  делает повторный прогон идемпотентным; без worktree-изоляции движок честно
  отказывается возобновлять прогон без `--force-rerun`.

## Честные границы

- Фейковый харнесс — механическая проверка движка; живой прогон на реальных
  Claude Code — отдельная задача (по кейсу 005 такая топология уже доказана).
- Движок не декомпозирует пакет на узлы: узлы пишет архитектор.
- Мерж в main в кейсе не показан: он всегда за владельцем
  (`fleet merge --owner-approve`), см. `docs/fleet.md`.
