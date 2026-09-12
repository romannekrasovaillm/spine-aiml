# Кейс 007 — fleet-spine-drift (дрейф спайна во флоте worktree)

> Training case produced with the Spine domain harness. Synthetic and
> educational — not a production design. Механический кейс: LLM не
> используется, воспроизводится голым бинарём `arch-be`.

Учебный кейс по мотивам разбора реального кейса агентной разработки: флот из
15 git-worktree, каждый несёт ПОЛНУЮ копию архитектурного спайна; из 1322
файлов документации ~90% — точные дубли, SSOT нарушен, копии измеримо
дрейфуют (87/88/89 файлов на worktree). Здесь та же болезнь в миниатюре —
на выдуманной платформе инференса из трёх компонентов (gateway, router,
registry), каждый в своём «worktree» `fleet/wt-{a,b,c}/` с полной копией
спайна.

## Связь с моделью «5.2 + дельта-протокол»

Рекомендация разбора — слоистая модель **5.2**: спайн живёт в ОДНОЙ копии
в корне (SSOT), компонент несёт lean-дельту (~3 файла: задача, контракты
SPEC.md, ссылки на спайн), а изменения спайна идут только дельтами
`changes/<id>` (ADDED/MODIFIED/REMOVED → ревью → применение → архив, по
OpenSpec). Кейс демонстрирует инструментарий Spine для этой модели:

- `arch-be fleet audit` — SSOT-аудит флота: считает точные дубли, файлы-ядро и
  дрейф копий; дрейф — exit code 1 (гейт для CI).
- `arch-be delta guard` — CI-запрет прямых правок спайна мимо дельты: изменённый
  защищённый файл обязан упоминаться в активной дельте `changes/*/DELTA.md`.
- SPEC.md в handoff-пакете (`handoff_create`) — верифицируемые контракты
  интерфейсов вместо прозаического ARCHITECTURE.md компонента.

## Анатомия кейса

```
fleet/
  wt-a/                        worktree компонента gateway
    ARCHITECTURE-SPINE.md      копия спайна (идентична во всех трёх)
    CONSTRAINTS.yaml           копия fitness-правил (идентична в wt-a/wt-b)
    model/adr/ADR-001.md       копия ADR (идентична во всех трёх)
    model/components/gateway.md  уникальный файл компонента (не спайн)
  wt-b/                        то же для router (router.md — уникальный)
  wt-c/                        то же для registry, НО:
    CONSTRAINTS.yaml           НАМЕРЕННЫЙ ДРЕЙФ: severity error → warn
                               у router-no-static-routes мимо дельт
expected/
  audit.txt                    эталонный вывод `arch-be fleet audit --stable` (exit 1):
                               флаг `--stable` убирает волатильные колонки
                               (размер на диске, возраст последнего коммита) —
                               эталон воспроизводится побайтово из кейса
```

`fleet/*` — просто каталоги, без вложенных `.git`: кейс переносим, а дрейф
считается по содержимому, а не по истории. Уникальные файлы компонентов
названы различно (`gateway.md`/`router.md`/`registry.md`), чтобы единственным
расхождением был намеренный дрейф `CONSTRAINTS.yaml`.

## Воспроизведение: аудит флота

Из корня репозитория Spine:

```bash
arch-be fleet audit кейсы/fleet-spine-drift/fleet/wt-* --stable
```

Флаг `--stable` даёт воспроизводимый вывод (без волатильных колонок размера и
возраста коммита) — именно он снят в эталон `expected/audit.txt`; без флага
вывод шире (для живой диагностики).

Ожидаемый вывод (эталон — `expected/audit.txt`, снят реальным запуском):

- 12 файлов в трёх корнях, точные дубли — 8 (66.7%): каждая копия спайна
  дублируется по флоту;
- ядро (файлы во всех worktree) — 3: спайн, ADR, CONSTRAINTS;
- дрейф ровно одного файла: `CONSTRAINTS.yaml`, канон — majority-версия
  (wt-a/wt-b), отступник — wt-c (ослабил severity мимо дельта-протокола);
- **exit code 1** — гейт для CI.

Сужение сканирования (только модель, без корневых файлов):

```bash
arch-be fleet audit кейсы/fleet-spine-drift/fleet/wt-* --include 'model/**'
# дрейфа нет: CONSTRAINTS.yaml вне выборки — демонстрация флага --include
```

Порог дублей как второй независимый гейт: `--fail-on-dupes 50` даст exit 1
уже на 66.7% дублей, даже без дрейфа. Worktree можно не перечислять руками:
`arch-be fleet audit --repo <путь>` возьмёт их из `git worktree list`.

## Воспроизведение: гейт прямых правок спайна (`arch-be delta guard`)

Демо на временном репозитории (файлы кейса не трогаем):

```bash
D=$(mktemp -d) && cd "$D"
git init -q
mkdir -p model/adr
printf '# ADR-003\n' > model/adr/ADR-003.md
printf '# Spine\n' > ARCHITECTURE-SPINE.md
git add . && git -c user.name=t -c user.email=t@t commit -qm init

# 1) Прямая правка model/ без дельты → FAIL, exit 1:
echo "v2" >> model/adr/ADR-003.md
arch-be delta guard        # [error] model/adr/ADR-003.md — не упоминается
                        # ни в одной активной дельте → Итог: FAIL (exit 1)

# 2) Оформляем правку дельтой → PASS, exit 0:
arch-be delta new tighten-adr-003
printf '\nПравка model/adr/ADR-003.md — таймауты.\n' >> changes/tighten-adr-003/DELTA.md
arch-be delta guard        # [ok] … покрыт активной дельтой 'tighten-adr-003' → PASS
```

Замечания:

- По умолчанию защищены `model/`, `ARCHITECTURE-SPINE.md`, `CONSTRAINTS.yaml`;
  повторяемый `--protect <префикс>` заменяет дефолт целиком.
- База diff по умолчанию `HEAD` (staged+unstaged рабочего дерева); в CI —
  `arch-be delta guard --base origin/main...HEAD` (трёхточечную форму разбирает
  сам git). Новые untracked-файлы `git diff` не видит — это известная
  граница дефолтной базы.
- Архивные дельты (`changes/archive/`) покрытием НЕ засчитываются: влитая
  истина не освобождает от протокола для новых правок.

## Чему учит кейс

- **Полные копии спайна во флоте дрейфуют измеримо**: аудит превращает
  «кажется, разошлось» в числа и поимённого отступника — почва для перехода
  на модель 5.2 (одна копия + lean-дельты).
- **Дрейф — это exit code**, а не заметка в вики: `fleet audit` и
  `delta guard` гейтят CI механически.
- **Протокол важнее памяти**: правка спайна легальна только через дельту —
  гейт проверяет след протокола, а не намерения исполнителя.
