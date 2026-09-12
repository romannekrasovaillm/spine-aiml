# Управление: автономия, доказательства, метрики, дельты

Четыре механизма губернанса из обзоров `_24_августа` (`docs/SOURCE_BRIEF.md`: AI-Disrupt PDLC,
OpenSpec), реализованные в харнессе детерминированным слоем.

## R-уровни автономности (policy)

Автономия калибруется **риском действия** (обратимость, blast radius), а не брендом модели.
Каждый вызов инструмента проходит проверку в `ToolRegistry::dispatch`:

| Класс риска | Примеры | R0–R1 | R2 (дефолт) | R4 | R5 |
|---|---|---|---|---|---|
| ReadOnly | read_file, grep, `cat`, `ls`, kb/web/skill-search | ALLOW | ALLOW | ALLOW | ALLOW |
| Mutating | write_file, edit_file, `cargo test`, `git commit` | confirm | ALLOW | ALLOW | ALLOW |
| Destructive | `rm -rf`, `git push --force`, `kubectl delete` | deny | **DENY** | confirm | ALLOW |

```toml
[policy]
autonomy = "R2"
```

- `arch-ml policy` — текущий уровень; `arch-ml policy --check "rm -rf /"` — класс риска и вердикт.
- Отказы журналируются в сессионном JSONL — это материал для детектора approval theater
  и аудита («кто и что пытался»).
- RequireConfirm в неинтерактивном режиме = отказ с текстом эскалации (модель корректно
  останавливается и просит человека — проверено живым прогоном).

## Evidence Bundle — условие выпуска

Аудиторский след собирается ДО релиза, а не после. Профиль полноты — по маршруту
значимости (`arch-ml control score`):

- **Fast** (5): problem, spec_or_delta, risk_level, acceptance, rollback.
- **Standard** (+2): adr_or_pattern, validation, fitness_report.
- **Critical** (+5): spine, decision_a3 (choice/rationale/rejected/expiry),
  walking_skeleton, adversarial_review, rollback_rehearsal (репетиция отката
  на гейте A4 — `.arch-handoff/REHEARSAL.json`, см. `docs/control.md`).

```bash
arch-ml evidence pack <dir> --route critical   # EVIDENCE.yaml + хэши артефактов
arch-ml evidence verify <dir>                  # полнота + целостность; exit 1 при FAIL
```

`verify` ловит подмену артефакта после упаковки (хэши не сошлись) — «спека,
дописанная задним числом» больше не проходит. Артефакты ищутся по каноническим
именам (SPEC.md, docs/adr/, DECISION.md, reports/fitness.md…).

## Метрики харнесса

`arch-ml metrics` — из локальных журналов сессий (`sessions/*.jsonl`) и отчётов (`reports/`):

- сессии/сообщения/вызовы инструментов, доля ошибок инструментов (first-pass proxy);
- реальные токены из записей `usage` журнала (агент пишет их по итогам каждого
  ответа LLM из `stream_options.include_usage`: модель, prompt, completion);
  для сессий без usage — грубая оценка chars/4, в выводе она помечена как fallback;
- рубричные отчёты: число и средний взвешенный балл;
- бенчмарки: pass rate; крон-отчёты.

Cost per validated outcome считается в деньгах ТОЛЬКО при заданных тарифах
(`price_in_per_1m`/`price_out_per_1m` в `[models.<name>]`, цена за 1M токенов
в валюте пользователя) — выдуманного курса нет: без тарифов метрика показывает
токены на результат (реальные usage либо оценку chars/4, с пометкой источника).

`arch-ml metrics --cost-report` — смета по реальным журналам: таблица по моделям
(сессий, prompt, completion, стоимость при заданном тарифе), топ-10 самых дорогих
сессий (по токенам), итоговая строка. Без usage-записей выводит понятное
сообщение: данные появятся после сессий с usage, пока — оценка chars/4.

Рост доли ошибок инструментов или падение среднего балла рубрик — красные флаги
процесса (см. SOURCE_BRIEF «Красные флаги при внедрении»).

## Дельта-спеки (state machine OpenSpec)

Для brownfield-потока (Fast/Standard): изменение = дельта относительно живой истины.

```bash
arch-ml delta new payment-timeout        # каркас changes/payment-timeout/DELTA.md
arch-ml delta validate payment-timeout   # секции ADDED/MODIFIED/REMOVED, EARS, заглушки
arch-ml delta list                       # предложенные vs архивные
arch-ml delta archive payment-timeout    # после apply: валидация + перенос в archive/
```

Имена дельт не переиспользуются; архивация без валидации невозможна (error-находки
блокируют). Critical Path дельтой не закрывается — там полный Solutioning
(скилл `significance-routing`).
