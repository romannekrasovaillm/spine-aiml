# Cron и баш-пайпы

Принцип харнесса: **задача — это markdown; исполнитель — LLM с ядерными
инструментами (баш-пайпы!); тайминг — cron**. Реализация — `src/cron.rs`;
CLI — `arch-ml cron` (`src/main.rs`).

## Расписание `cron.toml`

Путь — `cron.file` (дефолт `~/.arch-ml/cron.toml`; образец —
`cron.example.toml`, `arch-ml init` копирует его и md-инструкции):

```toml
[[job]]
name = "kb-digest"                    # уникальное имя (по нему arch-ml cron run)
schedule = "30 9 * * *"               # 5 полей: минута час день-месяца месяц день-недели
task_md = "~/.arch-ml/cron/kb_digest.md"   # md-инструкция исполнителю
model = "deepseek"                    # опционально (иначе default_model)
out = "~/.arch-ml/reports/cron"  # опционально (иначе <reports_dir>/cron)
```

Нюанс: тильда в `task_md`/`out` **не раскрывается автоматически** — после
`arch-ml init` замените на абсолютные пути (в `cron.toml` самого харнесса
тильда в `cron.file` раскрывается, внутри job-полей — нет).

## Команды

```bash
arch-ml cron list                # задачи: имя, расписание, файл инструкции
arch-ml cron run kb-digest       # запустить задачу по имени сейчас
arch-ml cron tick                # прогнать дюжные задачи (для системного cron)
```

### `tick` и системный crontab

Харнесс не держит собственный демон: периодичность — системный cron,
вызывающий `arch-ml cron tick`:

```cron
*/15 * * * * arch-ml cron tick >> ~/.arch-ml/reports/cron/tick.log 2>&1
```

- Метка последнего тика — `~/.arch-ml/cron-last-tick` (RFC 3339);
  при первом запуске — «сейчас минус 24 часа».
- Задача дюжная, если ближайшее срабатывание её выражения после метки
  `last` не позже `now` (`cron-parser`); дюжные выполняются последовательно;
  метка перезаписывается после тика.
- Битое cron-выражение (не 5 полей, ошибка разбора) — задача пропускается
  с предупреждением в лог, тик не рвётся. Первая ошибка прогона задачи
  прерывает тик; уже записанные отчёты остаются.

## Прогон задачи и headless JSON-статус

`run_job`: читает `task_md` → локальный цикл «модель ↔ инструменты»
(полный реестр: bash, файлы, kb, web, контроль…; лимит 16 итераций) →
markdown-отчёт `<out>/<name>-<yyyymmdd-HHMMSS>.md`.

Системный промпт исполнителя требует: отчёт в markdown, **последняя строка
— JSON-статус**:

```json
{"status": "complete|partial|blocked", "summary": "…"}
```

Статус извлекается из последней непустой строки (`extract_status`);
не распарсилась — `unknown`. Шапка отчёта: задача, расписание, модель,
дата, длительность, статус. Это тот же headless-контракт, что и у
кодовых харнессов (`docs/harness_integrations.md`): отчёт читается и
человеком, и скриптом (`tail -1 report.md | jq .status`).

### Goal-режим и cron: двухчастотный сторож

Goal-петля (`/goal` в TUI, `arch-ml run --goal "…"` в headless) — тот же
транспорт «user-сообщение как задание», но от контроль-петли сессии, а не от
планировщика: после каждого терна цель переинжектится в хвост контекста, пока
механическая проверка критерия не даст exit 0. Коды выхода headless:
`0` complete, `3` blocked, `6` paused (`docs/headless.md`).

```bash
arch-ml run -q --goal "у 4 моделей есть eval_results.json || файлы на месте || test -f a.json" > run.md
case $? in
  0) echo "цель достигнута";;
  3) echo "заблокирована — причина в журнале сессии";;
  6) echo "пауза — продолжить: /goal resume";;
esac
```

Двухчастотный сторож: гол даёт ЧАСТЫЕ мелкие тики (после каждого терна),
cron — РЕДКИЕ независимые протокольные проверки того же прогона
(`goal-protocol-check` в `cron.example.toml`, закомментирован: включает
`examples/cron/goal_protocol.md`, который раскладывает `arch-ml init`).
Пауза гол-петли не останавливает крон, и наоборот.

### Примеры инструкций (`examples/cron/`)

- **`kb_digest.md`** — дайджест новых материалов базы знаний за сутки:
  `find … -mtime -1` через инструмент `bash`, чтение свежих файлов,
  тематическая группировка, индекс `reports/cron/INDEX.md`; пустые дни
  честно помечаются. Статусы: `partial` — часть каталогов недоступна.
- **`spec_drift.md`** — дрейф-чек спецификаций (гейт A5 по расписанию):
  свежесть `docs/specs/*.md` против свежих коммитов кода; расхождения —
  только со свидетельствами (цитата спеки + файл:строка/хеш коммита);
  классификация `DRIFT`/`STALE-SPEC`/`OK`; прогон `arch-ml control spine`.

Пишите свои задачи по тому же образцу: роль дежурного агента, входные
параметры, нумерованные шаги с конкретными командами, формат отчёта,
JSON-статус с правилами выбора `partial`/`blocked`.

## Баш-пайпы

Харнесс дружит с Unix-пайпами — всё headless читает stdin и пишет stdout:

```bash
# Headless-агент на файле: промпт — весь stdin (`-` или просто пайп)
cat docs/specs/payment.md | arch-ml run -
cat spec.md | arch-ml run --model deepseek-pro --no-stream

# Рендер диаграммы в файл/дальше по пайпу
arch-ml mermaid examples/mermaid/flow.mmd > art.txt
cat seq.mmd | arch-ml mermaid - | less

# Контроль в CI: exit 1 при FAIL ломает пайплайн
arch-ml control check . || exit 1
arch-ml control spine docs/ARCHITECTURE-SPINE.md
arch-ml control sensors docs/specs

# Отбор отчётов крона по статусу
for f in ~/.arch-ml/reports/cron/*.md; do
  tail -1 "$f" | grep -q '"status": *"blocked"' && echo "BLOCKED: $f"
done
```

Детали: `arch-ml run` без аргумента читает stdin, только если stdin — не TTY
(в терминале без пайпа вернёт ошибку «нет промпта»); `--no-stream` печатает
только финальный ответ (удобно в скриптах). В стриминг-режиме stdout несёт
только текст ответа, а прогресс (вызовы инструментов `▶ tool: …` / `✓ …`,
заметки) уходит в stderr — пайп остаётся чистым.

Бюджеты на прогон (страховка CI/cron; превышение таймаута — причина в
stderr и exit 1):

```bash
arch-ml run -q --timeout 600 --max-turns 24 "…" > answer.md
```
