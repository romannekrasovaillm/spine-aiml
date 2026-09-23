#!/usr/bin/env bash
# fork_series_chain.sh — серийный драйвер форк-семантики сида (Н-7, ADR-057).
#
# Зачем отдельный драйвер, если есть `pilot_chain.sh`. Цепочка исполняет **стадии одной
# единицы прогона** (CPT → SFT → eval), но не знает, что единиц в серии несколько и что
# у них общий вход: цикл по сидам стоял бы снаружи всех стадий (так устроен `run_seeded_ladder.sh`,
# и это ровно то, что снято решением `ADR-056` п.2). Этот драйвер идёт по единицам серии
# **последовательно**: базовые стадии модели один раз → `k` RL-рук, у каждой свой каталог
# и свой сид. Форк виден структурой, а не договорённостью.
#
# Форма — по прецеденту `tools/mix_lr_grid_chain.sh` (коды 0/1/2, команда единицы читается
# из её каталога, инкрементальный прогресс, прерывание раннера серии безразлично); предмет
# другой, поэтому и файл другой: у `mix_lr_grid_chain.sh` единица — калибровочный прогон,
# у серии форка — две разные единицы (база модели и RL-рука), а вход рук объявлен планом.
#
# Что читается и что проверяется (инварианты спеки `docs/specs/LADDER-FORK-CARRIER.md`):
#
#   * I4 — только последовательно (AD-5): драйвер запускает ровно одну единицу за раз и
#     берёт замок серии (`var/series.pid`). Живой замок = вторая серия не стартует (код 2),
#     а не «встаёт в очередь»: две нагрузки на GB10 запрещены, очередь их не отменяет.
#   * I5 — стражи не обходятся: страж AD-9 обязан быть **назван** в записанной строке
#     (проверяет валидатор плана: нет `check_resource_owner.sh` — код 2), а `--guard <путь>`
#     позволяет вызвать стража и перед единицей: отказ стража → единица `blocked`,
#     серия останавливается (код 1), следующая не стартует.
#   * I6 — исполняется записанное: `chain_command.txt` из каталога единицы сверяется с планом
#     по sha256; расхождение — код 2. Драйвер не досочиняет строку и не «помнит» её.
#   * I2 — общий вход всех рук модели: перед каждой рукой проверяется, что объявленный
#     SFT-чекпойнт существует и его sha256 совпадает с планом, и что в каталоге руки
#     `checkpoints/sft_checkpoint_final.pt` — **симлинк** на этот же файл (копии весов
#     запрещены, AD-4), а не другой файл. Не тот вход — рука не стартует (код 2).
#   * I7 — отказ не размножается: единица, не дошедшая до `done`, останавливает серию (код 1);
#     следующие не запускаются.
#   * I8 — план до старта: `--print-plan` печатает состав серии и стенда не требует.
#   * финализация (дельты 2–3, ADR-057 «Поправки и решения» п.1, поправка 5): гейт AD-2
#     читает ОДИН корень (`python3 tools/check_run_manifest.py --runs runs/`) и ОДИН уровень
#     вложенности (`tools/check_run_manifest.py:187` — `runs.iterdir()`), а серия живёт на
#     сетевом диске. Поэтому отработавшая единица в `runs/` **не переезжает**, а выставляется
#     симлинком внутрь `$SHARED` **плоско**: `<link-root>/<имя единицы>` — промежуточный
#     каталог серии дал бы на корне находку «каталог серии без манифеста» (гигиена симлинков
#     C-011/AD-4 ссылку внутрь сетевого диска разрешает). Связь единицы с серией живёт в
#     плане (`fork_plan.json`), а не в пути. Копий артефактов не создаётся (AD-4), манифест
#     единицы гейт читает через симлинк.
#     Условие шага: единица дошла до `done` **и** её `run_manifest.json` существует —
#     каталог без манифеста гейт честно пометил бы находкой, поэтому выставлять его нельзя.
#     `series-id` **читается из плана** (имя каталога серии), а не угадывается по имени
#     флага: план — носитель того, что исполняется (ADR-023 п.10).
#   * снятие регистра (`--unlink-from-runs`, дельта 3). Ссылка живёт, пока жив каталог серии
#     на `$SHARED`: перенос или удаление серии обязано снимать ссылки ПЕРВЫМ (владелец
#     операции — оператор серии, ADR-057 §2.5), иначе битая ссылка красит гигиену симлинков
#     (C-011/C-032). Снимается только СВОЯ ссылка — та, что указывает внутрь каталога ЭТОЙ
#     серии; ссылка на чужой путь названа отказом (код 2) и не удаляется. Шаг идемпотентен
#     (нет ссылки — успех), стадии не исполняет и прогресс серии не трогает.
#
# Чего драйвер НЕ делает (границы названы, чтобы не выглядели обещанием):
#
#   * не исполняет стадии сам — стадии исполняет цепочка кейса (`tools/pilot_chain.sh`)
#     по записанной строке; драйвер стадий не знает и в них не вмешивается;
#   * не вызывает `docker` и не закрывает контейнеры: прежний драйвер сетки ждал закрытия
#     контейнера прошлой руки через `docker ps`, здесь этого нет — закрытие контейнера
#     принадлежит цепочке (она снимает остаток своего контейнера перед стартом стадии и
#     пишет статус `done` только по завершении). При таймауте драйвер останавливает цепочку
#     и **называет** владельцу, что контейнер стадии мог остаться живым;
#   * не решает вопросы площадки (сон GPU, тепло, хозяин ресурса сверх AD-9) — это области
#     `tools/chain_sleep_guard.sh` и `tools/guard_cuda_run.sh`;
#   * не копирует артефакты единицы в дерево кейса и не переписывает уже стоящий симлинк:
#     выставление — симлинк или отказ с названной причиной (копия весов — дефект AD-4);
#   * не убирает за чужой рукой: снятие регистра (`--unlink-from-runs`) снимает только
#     ссылки, указывающие внутрь каталога СВОЕЙ серии; чужая цель названа отказом, а не
#     удалена, и целиком чужие записи регистра шаг не трогает.
#
# Прогресс инкрементальный: строка в `status.tsv` на каждый переход и строка в `series.log`
# на каждый новый чекпойнт единицы — прогон не должен выглядеть тишиной.
#
# Коды возврата::
#
#     0 — все единицы серии дошли до `done` и выставлены в `--link-into-runs`
#     1 — единица не `done` (в `status.tsv` и `series.log` — какая и почему); следующие
#         не запускаются (I7); отказ стража помечается `blocked` (I5)
#     2 — NOT-VERIFIED: нет плана/каталога единицы/`chain_command.txt`, расхождение записи
#         и плана (I6), не тот или необъявленный общий SFT-вход (I2), серия уже идёт (I4);
#         финализация: некуда выставлять (нет каталога `--link-into-runs`, `series-id` не
#         читается из плана, каталог серии вне объявленного сетевого диска), единица `done`
#         без `run_manifest.json`, существующий симлинк ведёт на другой путь;
#         снятие регистра: нет каталога `--unlink-from-runs`, каталог серии не разрешается
#         в путь, ссылка ведёт не внутрь своей серии или не является симлинком
#
# Запуск (руками или из tmux на стенде; стенд драйвер сам не занимает)::
#
#     bash tools/fork_series_chain.sh --plan <series-dir>/fork_plan.json
#     bash tools/fork_series_chain.sh --series-dir <series-dir> --print-plan
#     bash tools/fork_series_chain.sh --series-dir <series-dir> --arms qwen25-05b-rl-s1337
#     bash tools/fork_series_chain.sh --plan <plan> --guard tools/check_resource_owner.sh
#     bash tools/fork_series_chain.sh --plan <plan> --link-into-runs runs/
#     bash tools/fork_series_chain.sh --plan <plan> --unlink-from-runs runs/

set -uo pipefail

PLAN=""
SERIES_DIR=""
ARMS=""
PRINT_PLAN=0
ARM_TIMEOUT=0              # 0 — без таймаута: цена базы и руки различается на порядок (~100 ч против ~18 ч)
POLL_SEC=30
GUARD=""
LINK_ROOT=""               # пусто — <корень кейса>/runs (разрешается ниже: корень считается от файла)
LINK_ROOT_GIVEN=0
UNLINK_ROOT=""             # каталог РЕГИСТРА для снятия ссылок (--unlink-from-runs) — режим, не фаза серии
UNLINK_GIVEN=0
#: Корень кейса — от самого файла драйвера, а не от текущего каталога вызова: `runs/` кейса
#: обязан быть тем же, из какого гейт C-012 читает манифесты, а не тем, откуда запустили.
CASE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLANNER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/build_fork_plan.py"

usage() {
  sed -n '2,94p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "флаги: (--plan <fork_plan.json> | --series-dir <каталог>) [--arms <имена через запятую>]"
  echo "       [--print-plan] [--arm-timeout <с>] [--poll <с>] [--guard <путь>]"
  echo "       [--link-into-runs <каталог, по умолчанию <корень кейса>/runs>]"
  echo "       [--unlink-from-runs <каталог регистра, по умолчанию <корень кейса>/runs>]"
  echo "       [--planner <путь к build_fork_plan.py>]"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --plan) PLAN="${2:-}"; shift 2 ;;
    --series-dir) SERIES_DIR="${2:-}"; shift 2 ;;
    --arms) ARMS="${2:-}"; shift 2 ;;
    --print-plan) PRINT_PLAN=1; shift ;;
    --arm-timeout) ARM_TIMEOUT="${2:-}"; shift 2 ;;
    --poll) POLL_SEC="${2:-}"; shift 2 ;;
    --guard) GUARD="${2:-}"; shift 2 ;;
    #: Сдвиг с проверкой длины: флаг последним словом (без значения) обязан отказать (см.
    #: ниже), а не крутить разбор на месте — `shift 2` при одном аргументе не сдвигает.
    --link-into-runs) LINK_ROOT="${2:-}"; LINK_ROOT_GIVEN=1; [ $# -ge 2 ] && shift 2 || shift ;;
    --unlink-from-runs) UNLINK_ROOT="${2:-}"; UNLINK_GIVEN=1; [ $# -ge 2 ] && shift 2 || shift ;;
    --planner) PLANNER="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "неизвестный флаг: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$PLAN" ] && [ -z "$SERIES_DIR" ]; then
  echo "NOT-VERIFIED: нужен --plan <fork_plan.json> или --series-dir <каталог>" >&2
  exit 2
fi

#: Флаг назван, а каталога нет — это отказ, а не «возьмём умолчание»: молчаливая подмена
#: каталога выставления увела бы симлинки не туда, куда просил оператор.
if [ "$LINK_ROOT_GIVEN" = "1" ] && [ -z "$LINK_ROOT" ]; then
  echo "NOT-VERIFIED: флаг --link-into-runs назван без каталога" >&2
  exit 2
fi
if [ "$UNLINK_GIVEN" = "1" ] && [ -z "$UNLINK_ROOT" ]; then
  echo "NOT-VERIFIED: флаг --unlink-from-runs назван без каталога" >&2
  exit 2
fi
#: Выставление и снятие — разные шаги с противоположным действием на регистр. Названы оба —
#: отказ, а не «последний победил»: молчаливое предпочтение одного из них читалось бы как
#: исполненное намерение оператора, которого он не выражал.
if [ "$LINK_ROOT_GIVEN" = "1" ] && [ "$UNLINK_GIVEN" = "1" ]; then
  echo "NOT-VERIFIED: --link-into-runs и --unlink-from-runs — разные шаги (выставление и снятие); назови один" >&2
  exit 2
fi
#: Печать состава и снятие регистра — тоже разные намерения, и «напечатал и заодно снял» не то,
#: что просил оператор. Проверка стоит здесь, а не в режиме снятия: иначе печать плана (она
#: идёт первой) вернула бы 0, и отказ до снятия было бы уже некому предъявить.
if [ "$PRINT_PLAN" = "1" ] && [ "$UNLINK_GIVEN" = "1" ]; then
  echo "NOT-VERIFIED: --print-plan и --unlink-from-runs вместе не исполняются: назови один шаг" >&2
  exit 2
fi
[ -n "$LINK_ROOT" ] || LINK_ROOT="$CASE_ROOT/runs"
[ -n "$UNLINK_ROOT" ] || UNLINK_ROOT="$CASE_ROOT/runs"

# Руки принимаются и через пробел, и через запятую (строка легко теряет пробелы при
# подстановке в tmux — прецедент mix_lr_grid_chain.sh).
ARMS="${ARMS//,/ }"

not_verified() { echo "NOT-VERIFIED: $*" >&2; exit 2; }

# ── план: каталог серии, единицы и их записанные строки ────────────────────────
# Читает план и печатает плоский список единиц: по строке на единицу, поля через табуляцию —
# `kind  dir  exp_base  seed  chain_command_sha256  shared_path  shared_sha  link_target`.
# Разбор плана живёт в планировщике (одно место на формат), а не здесь: копия разошлась бы
# с ним на первой правке (ADR-023 п.10).
units_of_plan() {
  python3 - "$1" <<'PY'
import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
units = []
for model in plan.get("models") or []:
    units.append(("base", model.get("base_dir"), model.get("base_exp_base"),
                  model.get("base_seed"), model.get("chain_command_sha256"),
                  "", "", ""))
for arm in plan.get("arms") or []:
    sf = arm.get("shared_sft") or {}
    units.append(("arm", arm.get("dir"), arm.get("exp_base"), arm.get("seed"),
                  arm.get("chain_command_sha256"), sf.get("path") or "",
                  sf.get("sha256") or "", sf.get("link_target") or ""))
for u in units:
    print("\t".join(str(x) if x is not None else "" for x in u))
PY
}

plan_file() {
  if [ -n "$PLAN" ]; then
    printf '%s' "$PLAN"
  else
    printf '%s/fork_plan.json' "${SERIES_DIR%/}"
  fi
}

#: Тождество серии: `series-id` и корень сетевого диска — ОБА из плана. Реестр ссылок плоский
#: (`<link-root>/<имя единицы>`, ADR-057 поправка 5), поэтому серию в реестре называет ПЛАН, а
#: не путь: `series-id` — имя каталога серии (`series_dir` в плане), и без него не назвать,
#: чьи ссылки снимает шаг `--unlink-from-runs` (угадывание по имени флага `--series-dir`
#: разошлось бы с планом на первой же правке, ADR-023 п.10). Пустое имя (`/`, `.`, `..`) — не
#: тождество, и драйвер обязан это назвать, а не молчать о безымянной серии.
plan_identity() {
  python3 - "$1" <<'PY'
import json, pathlib, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
name = pathlib.PurePath(str(plan.get("series_dir") or "")).name
#: Две строки, а не два поля через табуляцию: пустое `series-id` при полях через
#: табуляцию `read` съел бы как ведущий разделитель, и пустое имя подменилось бы путём
#: сетевого диска — «не читается» выглядело бы как «читается».
print(name if name not in ("", ".", "..") else "")
print(plan.get("shared") or "")
PY
}

PLAN_FILE="$(plan_file)"
[ -f "$PLAN_FILE" ] || not_verified "нет плана серии: $PLAN_FILE (собирается tools/build_fork_plan.py)"

if [ ! -f "$PLANNER" ]; then
  not_verified "нет валидатора плана: $PLANNER (--planner)"
fi
# Проверка плана — до всего остального: I2/I3/I5/I6 обязаны быть сняты до запуска нагрузки,
# а не выясняться на середине серии.
if ! python3 "$PLANNER" --check-plan "$PLAN_FILE"; then
  not_verified "план $PLAN_FILE не прошёл проверку (см. находки выше)"
fi

#: Каталог серии берётся из плана (единицы лежат в нём), а не из имени флага: --plan и
#: --series-dir обязаны смотреть в одно место, иначе драйвер писал бы прогресс мимо серии.
SERIES_DIR="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["series_dir"])' "$PLAN_FILE")"
IDENT="$(plan_identity "$PLAN_FILE")" || not_verified "план $PLAN_FILE не разобран (series-id)"
SERIES_ID="$(printf '%s\n' "$IDENT" | sed -n 1p)"
SHARED_DIR="$(printf '%s\n' "$IDENT" | sed -n 2p)"
STATUS="$SERIES_DIR/status.tsv"
LOG="$SERIES_DIR/series.log"
LOCK="$SERIES_DIR/var/series.pid"

UNITS="$(units_of_plan "$PLAN_FILE")" || not_verified "план $PLAN_FILE не разобран"
#: Полный состав плана — до подмножества `--arms`: реестр ссылок снимается по ВСЕМ единицам
#: серии (`--unlink-from-runs`). Подмножество здесь означало бы «ссылки остальных остаются
#: висеть» — а шаг и существует ради того, чтобы висячих ссылок не осталось.
UNITS_ALL="$UNITS"

#: Подмножество рук (--arms) — режим добора: базовые единицы в нём не исполняются, вход рук
#: обязан быть уже посчитан базой (иначе проверка общего входа откажет, код 2).
SELECTED=""
if [ -n "$ARMS" ]; then
  SELECTED="$(printf '%s\n' "$UNITS" | awk -F'\t' -v want="$ARMS" '
    BEGIN { n = split(want, w, " "); for (i = 1; i <= n; i++) if (w[i] != "") keep[w[i]] = 1 }
    $1 == "arm" && ($2 in keep) { print }
  ')"
  for arm in $ARMS; do
    printf '%s\n' "$SELECTED" | awk -F'\t' -v a="$arm" '$2 == a { found = 1 } END { exit !found }' \
      || not_verified "руки $arm нет в плане $PLAN_FILE"
  done
  UNITS="$SELECTED"
fi

units_count() { printf '%s\n' "$UNITS" | grep -c . ; }

if [ "$PRINT_PLAN" = "1" ]; then
  echo "план серии: $PLAN_FILE"
  echo "  каталог серии: $SERIES_DIR; таймаут единицы: $([ "$ARM_TIMEOUT" = "0" ] && echo 'без таймаута' || echo "${ARM_TIMEOUT} с"); опрос ${POLL_SEC} с"
  echo "  стоп-условия стадий — внутри цепочки (tools/pilot_chain.sh), здесь их нет"
  echo "  выставление отработавших единиц: ${LINK_ROOT}/<имя единицы> (плоско: промежуточного каталога серии нет) → симлинк внутрь ${SHARED_DIR:-<нет shared>} (после done и манифеста)"
  echo "  снятие регистра перед переносом/удалением серии ${SERIES_ID:-<series-id не читается из плана>}: $0 --series-dir $SERIES_DIR --unlink-from-runs ${UNLINK_ROOT}"
  echo "  единиц: $(units_count) (базовых: $(printf '%s\n' "$UNITS" | awk -F'\t' '$1=="base"' | grep -c .), рук: $(printf '%s\n' "$UNITS" | awk -F'\t' '$1=="arm"' | grep -c .))"
  printf '%s\n' "$UNITS" | while IFS=$'\t' read -r kind dir exp seed sha shared shared_sha link; do
    [ -n "${dir:-}" ] || continue
    echo "  · $kind $dir (exp-base $exp, сид $seed)"
    if [ "$kind" = "arm" ]; then
      echo "      общий вход: $shared (sha256 ${shared_sha:0:12}…) → $dir/${link##*/}"
    fi
    if [ -f "$SERIES_DIR/$dir/chain_command.txt" ]; then
      echo "      строка: $(head -c 160 "$SERIES_DIR/$dir/chain_command.txt")…"
    else
      echo "      строка: НЕТ $SERIES_DIR/$dir/chain_command.txt (единица не подготовлена)"
    fi
  done
  exit 0
fi

# ── шаг снятия регистра ссылок (`--unlink-from-runs`, дельта 3, ADR-057 §2.5) ──
# Это РЕЖИМ, а не фаза серии: шаг не исполняет стадии, не берёт замок серии, не пишет её
# прогресс и не требует стенда. Он снимает из регистра (`<link-root>/<имя единицы>`) ссылки,
# поставленные выставлением, — и только СВОИ: те, что указывают внутрь каталога ЭТОЙ серии.
# Зачем: ссылка живёт, пока жив каталог серии на сетевом диске; перенос или удаление серии
# обязано снимать регистр ПЕРВЫМ (владелец операции — оператор серии), иначе битая ссылка
# красит гигиену симлинков (C-011/C-032). Чужая цель — отказ с названной причиной, не удаление.
unote() { printf '[%s] %s\n' "$(date -Is)" "$*"; }

#: Своя ли ссылка: указывает ли она внутрь каталога ЭТОЙ серии. Разрешаем цель, а если цель
#: не разрешается (единица или сама серия уже снята — битая ссылка) — сравниваем ЗАПИСАННЫЙ
#: путь: драйвер ставит ссылку абсолютным путём (`ln -s <readlink -f единицы>`), поэтому
#: `readlink` отвечает тем же тождеством и без разрешения. Относительная цель разрешается от
#: каталога самой ссылки — иначе она сравнивалась бы с текущим каталогом вызова.
is_own_link() {  # link series_real
  local link="$1" series_real="$2" raw cand
  raw="$(readlink "$link" 2>/dev/null || true)"
  for cand in "$(readlink -f "$link" 2>/dev/null || true)" "$raw"; do
    [ -n "$cand" ] || continue
    case "$cand" in
      "$series_real"|"$series_real"/*) return 0 ;;
    esac
  done
  case "$raw" in
    ""|/*) ;;
    *) cand="$(readlink -f "$(dirname "$link")/$raw" 2>/dev/null || true)"
       [ -n "$cand" ] || return 1
       case "$cand" in
         "$series_real"|"$series_real"/*) return 0 ;;
       esac ;;
  esac
  return 1
}

unlink_from_runs() {  # <корень регистра> <series_real>
  local root="$1" series_real="$2" name link removed=0 absent=0 refused=0
  while IFS=$'\t' read -r kind dir exp seed sha shared shared_sha target; do
    [ -n "${dir:-}" ] || continue
    name="$(basename "${dir%/}")"
    link="$root/$name"
    #: Не ссылка и не файл — снимать нечего; это успех, а не «тишина»: шаг идемпотентен.
    if [ ! -L "$link" ] && [ ! -e "$link" ]; then
      unote "   $name: ссылки нет — снимать нечего (идемпотентно)"
      absent=$((absent + 1)); continue
    fi
    #: Обычный файл/каталог под именем единицы — это НЕ утверждение драйвера о пути единицы.
    #: Удалить его значило бы «снять» чужое: отказ, а не удаление.
    if [ ! -L "$link" ]; then
      unote "ОТКАЗ: $link — не симлинк: это не запись регистра о единице $name, не удаляю"
      refused=$((refused + 1)); continue
    fi
    if ! is_own_link "$link" "$series_real"; then
      unote "ОТКАЗ: $link ведёт не внутрь каталога серии ($(readlink "$link") ∉ $series_real) — чужая цель не удаляется (коллизия имён разбирается человеком)"
      refused=$((refused + 1)); continue
    fi
    if ! rm -f "$link"; then
      unote "ОТКАЗ: не удалось снять $link"
      refused=$((refused + 1)); continue
    fi
    unote "снята своя ссылка: $link → $(readlink "$link")"
    removed=$((removed + 1))
  done <<< "$UNITS_ALL"
  unote "итог снятия: снято $removed, отсутствовало $absent, отказов $refused (единиц в плане: $(printf '%s\n' "$UNITS_ALL" | grep -c .))"
  [ "$refused" -eq 0 ] || return 2
  return 0
}

if [ "$UNLINK_GIVEN" = "1" ]; then
  #: Нет каталога регистра — отказ, а не молчаливый успех: опечатка в пути («runs/» вместо
  #: «../runs/») выглядела бы как «снято», после чего серию удалили бы вместе со ссылками.
  [ -d "$UNLINK_ROOT" ] || not_verified "нет каталога регистра: $UNLINK_ROOT (--unlink-from-runs) — снимать негде; каталог драйвер не создаёт"
  SERIES_REAL="$(readlink -f "$SERIES_DIR" 2>/dev/null || true)"
  [ -n "$SERIES_REAL" ] && [ -d "$SERIES_REAL" ] || not_verified "каталог серии не разрешается в путь: $SERIES_DIR — чьи ссылки снимать, не определить (регистр снимается ПЕРВЫМ, до переноса/удаления серии; ADR-057 §2.5)"
  unote "== снятие регистра ссылок серии ${SERIES_ID:-<имя не читается>}: $UNLINK_ROOT (единиц в плане: $(printf '%s\n' "$UNITS_ALL" | grep -c .)) =="
  unlink_from_runs "$UNLINK_ROOT" "$SERIES_REAL"
  unlink_rc=$?
  if [ "$unlink_rc" != "0" ]; then
    unote "NOT-VERIFIED: регистр снят не полностью (см. отказы выше) — чужие цели не тронуты"
    exit 2
  fi
  exit 0
fi

# ── стоп-условия финализации: куда и чем выставляются отработавшие единицы ─────
# Проверяются ДО записи прогресса и до старта нагрузки: серия идёт часами, и выяснить после
# первой отработавшей единицы, что выставлять её некуда, значило бы потерять и время, и
# доказательство. Всё, что здесь отказывает, — код 2 (NOT-VERIFIED) с названной причиной.
if [ -z "$SERIES_ID" ]; then
  not_verified "series-id не читается из плана $PLAN_FILE: имя каталога серии пусто (series_dir=$SERIES_DIR) — серия не названа: в плоском реестре ссылок её не отличить от чужой, а снятие (--unlink-from-runs) не назвало бы владельца"
fi
if [ -z "$SHARED_DIR" ]; then
  not_verified "в плане $PLAN_FILE не назван сетевой диск (shared) — цель симлинка неизвестна"
fi
[ -d "$SHARED_DIR" ] || not_verified "сетевой диск из плана недоступен: $SHARED_DIR"
[ -d "$LINK_ROOT" ] || not_verified "нет каталога выставления: $LINK_ROOT (--link-into-runs) — симлинки ставить некуда, а создавать его драйвер не вправе"
SHARED_REAL="$(readlink -f "$SHARED_DIR")"
SERIES_REAL="$(readlink -f "$SERIES_DIR")"
case "$SERIES_REAL" in
  "$SHARED_REAL"/*) ;;
  *) not_verified "каталог серии $SERIES_DIR вне объявленного сетевого диска $SHARED_REAL: симлинк в $LINK_ROOT вёл бы наружу (AD-4/C-011)" ;;
esac

mkdir -p "$SERIES_DIR/var"
say() { printf '[%s] %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }
row() { printf '%s\t%s\t%s\t%s\t%s\n' "$(date -Is)" "$1" "$2" "${3:-0}" "${4:-}" >> "$STATUS"; }

# ── I4: замок серии ───────────────────────────────────────────────────────────
# Живой замок = серия уже идёт. Ждать нельзя: две нагрузки на GB10 (AD-5) — это не очередь,
# а запрещённое состояние. Владелец замка назван, чтобы решение было за человеком.
if [ -f "$LOCK" ]; then
  holder="$(cat "$LOCK" 2>/dev/null || echo '')"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    not_verified "серия уже идёт: $LOCK держит процесс $holder (AD-5: вторая нагрузка не запускается — ни сразу, ни в очередь)"
  fi
  say "снят замок мёртвого процесса ${holder:-?}: серия не идёт"
fi
printf '%s\n' "$$" > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

say "== серия форк-семантики сида: $(units_count) единиц =="
say "план: $PLAN_FILE; каталог серии: $SERIES_DIR; стенд: $(hostname)"
say "стоп-условия стадий — внутри цепочки; драйвер единицы не исполняет, он исполняет записанное"

# ── подготовка общего входа руки (I2 + AD-4) ──────────────────────────────────
# Вход RL-стадии — `checkpoints/sft_checkpoint_final.pt` в каталоге прогона
# (`laguna_pipeline_v8.py`: `sft_ckpt = Path(args.ckpt_dir)/"sft_checkpoint_final.pt"`):
# флага «взять чекпойнт по пути» у пайплайна нет. Поэтому общий SFT-чекпойнт базы появляется
# в каталоге руки **симлинком** (копия весов запрещена, AD-4), а перед этим сверяется, что
# он тот самый: путь и sha256 объявлены планом, факт — замер на месте (ADR-028 п.1).
prepare_shared_input() {  # dir link_target declared_path declared_sha
  local dir="$1" target="$2" declared="$3" sha="$4"
  local link="$dir/checkpoints/sft_checkpoint_final.pt"
  [ -n "$declared" ] || { say "ОТКАЗ I2: у руки не объявлен SFT-вход"; return 2; }
  [ -f "$declared" ] || { say "ОТКАЗ I2: объявленного SFT-входа нет на месте: $declared"; return 2; }
  local actual
  actual="$(sha256sum "$declared" | awk '{print $1}')"
  [ "$actual" = "$sha" ] || {
    say "ОТКАЗ I2: объявленный SFT-вход изменился: план ${sha:0:12}…, факт ${actual:0:12}… ($declared)"
    return 2
  }
  mkdir -p "$dir/checkpoints"
  if [ -L "$link" ]; then
    local now; now="$(readlink -f "$link" 2>/dev/null || true)"
    [ "$now" = "$(readlink -f "$declared")" ] || {
      say "ОТКАЗ I2: $link ведёт не на объявленный вход ($now ≠ $declared)"; return 2; }
    return 0
  fi
  if [ -e "$link" ]; then
    say "ОТКАЗ AD-4: $link — обычный файл (копия весов), а вход обязан быть симлинком на общий чекпойнт"
    return 2
  fi
  ln -s "$target" "$link" || { say "ОТКАЗ: не удалось поставить симлинк $link → $target"; return 2; }
  say "общий вход руки на месте: $link → $target (sha256 ${sha:0:12}…)"
  return 0
}

# ── финализация единицы: выставление в runs/ симлинком (дельты 2–3, ADR-057 п.1) ──
# Единица не переезжает с сетевого диска (тяжёлые артефакты остаются на месте, AD-4) — она
# выставляется симлинком, и гейт AD-2 читает её манифест через этот симлинк. Копий нет,
# перезаписи чужого симлинка нет: выставление — либо симлинк, либо отказ с названной причиной.
# Выставка ПЛОСКАЯ (`<link-root>/<имя единицы>`), без промежуточного каталога серии: гейт AD-2
# читает один уровень (`tools/check_run_manifest.py:187` — `runs.iterdir()`), поэтому каталог
# серии на корне читался бы как прогон без манифеста (ADR-057 «Поправки и решения», поправка 5).
# Связь единицы с серией живёт в плане (`fork_plan.json`), а не в пути.
exhibit_unit() {  # unit_dir
  local dir="$1" name link manifest target
  name="$(basename "${dir%/}")"
  link="$LINK_ROOT/$name"
  manifest="$dir/run_manifest.json"
  target="$(readlink -f "$dir" 2>/dev/null || true)"
  [ -n "$target" ] || { say "ОТКАЗ: каталог единицы не разрешается в путь: $dir"; return 2; }
  case "$target" in
    "$SHARED_REAL"/*) ;;
    *) say "ОТКАЗ AD-4: каталог единицы $dir вне сетевого диска ($target) — симлинк в $LINK_ROOT вёл бы наружу (C-011)"; return 2 ;;
  esac
  #: Порядок важен: выставление — ПОСЛЕ манифеста. Каталог без манифеста гейт AD-2 честно
  #: пометит находкой, поэтому «выставить, а манифест подоспеет» — это подставить гейт кейса.
  if [ ! -f "$manifest" ]; then
    say "ОТКАЗ AD-2: единица $name дошла до done, но её манифеста нет ($manifest): выставлять нечего — гейт пометил бы каталог находкой. Симлинк не ставится"
    return 2
  fi
  if [ -L "$link" ]; then
    local now; now="$(readlink -f "$link" 2>/dev/null || true)"
    if [ "$now" = "$target" ]; then
      say "единица уже выставлена: $link → $now (повтор без правки)"
      return 0
    fi
    say "ОТКАЗ: $link уже указывает на другой путь (${now:-битая цель: $(readlink "$link")}) ≠ $target — чужое утверждение о пути не перезаписываю"
    return 2
  fi
  if [ -e "$link" ]; then
    say "ОТКАЗ: $link существует и это не симлинк — не перезаписываю"
    return 2
  fi
  #: Каталога под выставление драйвер не создаёт: корень назван оператором и обязан
  #: существовать (проверено выше), а промежуточный каталог серии плоской выставке не нужен.
  ln -s "$target" "$link" || { say "ОТКАЗ: не удалось поставить симлинк $link → $target"; return 2; }
  say "единица выставлена плоско: $link → $target (манифест гейт читает через симлинк)"
  return 0
}

# ── I6: записанное сверяется с планом ДО старта нагрузки ──────────────────────
# Подмена `chain_command.txt` (или расхождение плана с записью) обязана быть видна до того,
# как что-то запущено: иначе серия потратила бы часы и остановилась на середине.
check_record() {  # dir expected_sha
  local dir="$1" expected="$2" unit_dir="$SERIES_DIR/$1"
  [ -d "$unit_dir" ] || { say "NOT-VERIFIED: нет каталога единицы $dir ($unit_dir)"; return 2; }
  [ -f "$unit_dir/chain_command.txt" ] || {
    say "NOT-VERIFIED: у единицы $dir нет chain_command.txt — исполнять нечего"; return 2; }
  local recorded
  recorded="$(sha256sum "$unit_dir/chain_command.txt" | awk '{print $1}')"
  [ "$recorded" = "$expected" ] || {
    say "NOT-VERIFIED: chain_command.txt единицы $dir расходится с планом (${recorded:0:12}… ≠ ${expected:0:12}…)"
    return 2
  }
  return 0
}

echo "── предполётная сверка записей (I6) ──────────────────────────────────────"
preflight_ok=1
while IFS=$'\t' read -r kind dir exp seed sha shared shared_sha link; do
  [ -n "${dir:-}" ] || continue
  if ! check_record "$dir" "$sha"; then
    row "$dir" "chain_command_mismatch" 0 "$SERIES_DIR/$dir"
    preflight_ok=0
  fi
done <<< "$UNITS"
if [ "$preflight_ok" != "1" ]; then
  echo "not_verified" > "$SERIES_DIR/status"
  say "== серия не начата: записанное не сходится с планом (I6) =="
  exit 2
fi
say "записи сходятся с планом: единиц $(units_count)"

overall="done"
stop=0
#: `while` с here-string, а не с конвейером: конвейер увёл бы цикл в подоболочку, и
#: `overall`/`stop` (итог серии и её остановка) не дожили бы до конца скрипта.
while IFS=$'\t' read -r kind dir exp seed sha shared shared_sha link; do
  [ -n "${dir:-}" ] || continue
  unit_dir="$SERIES_DIR/$dir"
  if [ "$stop" = "1" ]; then break; fi

  #: Запись сверяется ещё раз прямо перед стартом: серия живёт часами, и правку записи
  #: в это время драйвер обязан заметить (I6), а не исполнить изменившееся.
  if ! check_record "$dir" "$sha"; then
    row "$dir" "chain_command_mismatch" 0 "$unit_dir"
    overall="not_verified"; stop=1; continue
  fi

  if [ "$kind" = "arm" ]; then
    if ! prepare_shared_input "$unit_dir" "$link" "$shared" "$shared_sha"; then
      say "серия остановлена: рука $dir не получила объявленный общий вход (I2)"
      row "$dir" "shared_input_refused" 0 "$unit_dir"
      overall="not_verified"; stop=1; continue
    fi
  fi

  #: I5: страж AD-9 — перед единицей (если объявлен) и/или перед каждой стадией внутри
  #: цепочки (она вызывает его безусловно; страж назван в записанной строке, и это проверено
  #: валидатором плана). Отказ стража — `blocked` и стоп серии, а не «попробуем следующую».
  if [ -n "$GUARD" ]; then
    say "страж AD-9 перед единицей $dir: $GUARD"
    if ! bash "$GUARD" --stage "$( [ "$kind" = "arm" ] && echo rl || echo cpt )" --exp-name "$exp"; then
      say "ОТКАЗ СТРАЖА: единица $dir помечена blocked, серия остановлена (I5)"
      row "$dir" "blocked" 0 "$unit_dir"
      overall="failed"; stop=1; continue
    fi
  fi

  cmd="$(cat "$unit_dir/chain_command.txt")"
  rm -f "$unit_dir/var/chain.status"
  timed_out=0
  say "СТАРТ единицы $dir ($kind, exp-base $exp, сид $seed)"
  say "  строка запуска: $cmd"
  row "$dir" "start" 0 "$unit_dir"
  t0="$(date +%s)"
  bash -c "$cmd" >> "$unit_dir/logs/chain.log" 2>&1 &
  chain_pid=$!

  last=""
  while :; do
    st="$(cat "$unit_dir/var/chain.status" 2>/dev/null || echo none)"
    case "$st" in
      done|failed|precondition_failed|paused|stopped|partial|checked|blocked) break ;;
    esac
    ck="$(ls "$unit_dir/checkpoints" 2>/dev/null | grep -c 'checkpoint' || true)"
    step="$(tail -1 "$unit_dir/logs/loss_trace.jsonl" 2>/dev/null \
            | sed -n 's/.*"step": *\([0-9]*\).*/\1/p')"
    now="$ck/$step"
    if [ "$now" != "$last" ]; then
      say "  $dir: объектов checkpoint* в каталоге $ck (вход руки — тоже), последний шаг траектории ${step:-—}"
      last="$now"
    fi
    elapsed=$(( $(date +%s) - t0 ))
    if [ "$ARM_TIMEOUT" -gt 0 ] && [ "$elapsed" -gt "$ARM_TIMEOUT" ]; then
      say "ТАЙМАУТ единицы $dir (>${ARM_TIMEOUT} с): останавливаю цепочку и останавливаю серию"
      say "  контейнер стадии драйвер не закрывает (docker вне его границ) — если он жив, его закрывает владелец: tools/stop_stage_run.py"
      kill "$chain_pid" 2>/dev/null || true
      row "$dir" "timeout" "$elapsed" "$unit_dir"
      overall="failed"; stop=1; timed_out=1
      break
    fi
    sleep "$POLL_SEC"
  done
  wait "$chain_pid" 2>/dev/null
  elapsed=$(( $(date +%s) - t0 ))
  if [ "$timed_out" = "1" ]; then
    #: Строка `timeout` уже записана; «none» после убийства цепочки — это не статус единицы,
    #: а её отсутствие, и как итог оно читалось бы как «стадия ничего не сказала».
    say "ИТОГ единицы $dir: timeout за ${elapsed} с (строка timeout в status.tsv выше)"
    continue
  fi
  st="$(cat "$unit_dir/var/chain.status" 2>/dev/null || echo none)"
  #: Отказ стража цепочка записывает как `precondition_failed`, а стадию — как `blocked`
  #: (`var/status/<стадия>`). Различаем их, чтобы отчёт называл причину: «страж отказал»
  #: и «стадия упала» — разные события (I5).
  if [ "$st" = "precondition_failed" ] && grep -qsl '^blocked$' "$unit_dir"/var/status/* 2>/dev/null; then
    st="blocked"
  fi
  say "ИТОГ единицы $dir: $st за ${elapsed} с"
  row "$dir" "$st" "$elapsed" "$unit_dir"
  if [ "$st" != "done" ]; then
    say "серия остановлена на единице $dir: статус $st (следующие не запускаются, I7)"
    overall="failed"; stop=1
  elif ! exhibit_unit "$unit_dir"; then
    #: Единица отработала, но не выставлена: доказательство не появилось там, где его читает
    #: гейт. Это NOT-VERIFIED (не «failed»): стадия не падала, а носитель её не предъявил.
    row "$dir" "not_exhibited" "$elapsed" "$unit_dir"
    say "серия остановлена: единица $dir отработала, но не выставлена в $LINK_ROOT (финализация, ADR-057 п.1)"
    overall="not_verified"; stop=1
  fi
done <<< "$UNITS"

echo "$overall" > "$SERIES_DIR/status"
say "== серия завершена: $overall =="

case "$overall" in
  done) exit 0 ;;
  not_verified) exit 2 ;;
  *) exit 1 ;;
esac
