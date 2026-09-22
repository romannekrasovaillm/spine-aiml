#!/usr/bin/env bash
# S3m — сетка целиком: руки идут ОДНОЙ tmux-сессией на стенде, последовательно.
#
# Зачем отдельный драйвер, если есть `run_mix_lr_calib.py --launch`. `--launch`
# поднимает **одну** руку и возвращает управление: чтобы пошла следующая, раннер
# должен дожить до следующего вызова. 16.09 он не дожил — смерть раннера после
# первой руки оставила сетку на 1 из 4 (инцидент hr-40: процессов и контейнеров
# не осталось, GPU освободилась, сделано 1 из 4). Этот скрипт идёт по рукам сам:
# он живёт в tmux на стенде и прерывание раннера ему безразлично.
#
# Последовательность, а не параллель, обязательна: AD-5 — одна тренировочная
# нагрузка на GB10 (две руки сразу делили бы память и мерили друг друга).
#
# Что читается. У каждой руки уже есть каталог прогона на сетевом диске с
# `chain_command.txt` — ровно та строка, которой рука запускается вручную
# (`run_mix_lr_calib.py --launch` пишет её же). Драйвер ничего не досочиняет:
# исполняется то, что лежит в каталоге, а не то, что помнит драйвер.
#
# Прогресс инкрементальный (требование дельты: прогон не должен выглядеть
# тишиной): строка в `status.tsv` на каждый переход и строка в `grid.log` на
# каждый новый чекпойнт или шаг траектории руки.
#
# Коды возврата::
#
#     0 — все руки дошли до `chain.status = done`
#     1 — рука завершилась не `done` (в логе и status.tsv — какая и почему);
#         следующие руки не запускаются: непонятный отказ первой нельзя
#         размножать на остальные
#     2 — NOT-VERIFIED: нет входа (нет каталога руки, нет `chain_command.txt`)
#
# Префикс каталогов рук и имя каталога серии — параметры, а не константы: у сетки
# S3m это `calib-<рука>-<ts>` и `grid-<ts>`, у серии контрольных рук S3o —
# `ctrl-<рука>-<ts>` и `ctrl-grid-<ts>`. Драйвер не угадывает их по именам рук:
# он читает `chain_command.txt` из каталога, который назвал раннер.
#
# Запуск (на стенде, руками или из `run_mix_lr_calib.py --chain`)::
#
#     bash mix_lr_grid_chain.sh --ts 20260916-0820 --arms 25-0.35 50-0.7 50-0.35
#     bash mix_lr_grid_chain.sh --ts 20260916-0820 --arms C1-100-0.35 C2-25-0.035 \
#          --run-prefix ctrl --grid-name ctrl-grid-20260916-0820
#     bash mix_lr_grid_chain.sh --ts 20260916-0820 --arms 25-0.35 --print-plan

set -u

SHARED="${SHARED:-/home/user/gb10-shared}"
TS=""
ARMS=""
RUN_PREFIX="calib"
GRID_NAME=""
ARM_TIMEOUT=$((6 * 3600))
POLL_SEC=30
PRINT_PLAN=0

usage() {
  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "флаги: --ts <метка> --arms <список> [--shared <каталог>] [--arm-timeout <с>]"
  echo "       [--run-prefix <calib|ctrl>] [--grid-name <имя каталога серии>]"
  echo "       [--poll <с>] [--print-plan]"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ts) TS="${2:-}"; shift 2 ;;
    --arms) ARMS="${2:-}"; shift 2 ;;
    --shared) SHARED="${2:-}"; shift 2 ;;
    --run-prefix) RUN_PREFIX="${2:-}"; shift 2 ;;
    --grid-name) GRID_NAME="${2:-}"; shift 2 ;;
    --arm-timeout) ARM_TIMEOUT="${2:-}"; shift 2 ;;
    --poll) POLL_SEC="${2:-}"; shift 2 ;;
    --print-plan) PRINT_PLAN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "неизвестный флаг: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$TS" ] || [ -z "$ARMS" ]; then
  echo "NOT-VERIFIED: нужны --ts и --arms (список рук через пробел)" >&2
  exit 2
fi

# Руки принимаются и через пробел, и через запятую: строка уходит в tmux, где
# пробелы легко теряются при подстановке, а запятая — нет.
ARMS="${ARMS//,/ }"

# Имя каталога серии по умолчанию выводится из префикса: `calib` → `grid-<ts>`
# (так назывались серии S3m), `ctrl` → `ctrl-grid-<ts>`. Явный --grid-name сильнее
# вывода: раннер и драйвер обязаны смотреть в ОДИН каталог, а не в два похожих.
if [ -z "$GRID_NAME" ]; then
  if [ "$RUN_PREFIX" = "calib" ]; then GRID_NAME="grid-$TS"; else GRID_NAME="$RUN_PREFIX-grid-$TS"; fi
fi

GRID="$SHARED/calib/$GRID_NAME"
STATUS="$GRID/status.tsv"
LOG="$GRID/grid.log"

# План печатается без обращения к стенду и без записи: по нему видно, какие руки
# и какой строкой пойдут, — до того, как что-то запущено (практика кейса:
# `--print-plan` у сборщика микса).
if [ "$PRINT_PLAN" = "1" ]; then
  echo "план серии рук: метка $TS, префикс каталогов рук «$RUN_PREFIX»"
  echo "  каталог серии: $GRID"
  echo "  таймаут руки:  ${ARM_TIMEOUT} с; опрос ${POLL_SEC} с (стоп-условия самой цепочки — в pilot_chain.sh)"
  for arm in $ARMS; do
    run="$RUN_PREFIX-$arm-$TS"
    dir="$SHARED/calib/$run"
    if [ -f "$dir/chain_command.txt" ]; then
      echo "  $arm: $run → $(head -c 120 "$dir/chain_command.txt")..."
    else
      echo "  $arm: $run → НЕТ chain_command.txt (рука не подготовлена)"
    fi
  done
  exit 0
fi

mkdir -p "$GRID"
say() { printf '[%s] %s\n' "$(date -Is)" "$*" >> "$LOG"; }
row() { printf '%s\t%s\t%s\t%s\n' "$(date -Is)" "$1" "$2" "$3" >> "$STATUS"; }
# `tee` в лог не нужен: stdout драйвера сам уходит в grid.log через tmux-обёртку
# запуска; но при ручном запуске его видно на терминале, поэтому пишем и туда.

say "== серия рук, метка $TS: префикс $RUN_PREFIX, руки $ARMS =="
say "стенд: $(hostname); каталог серии: $GRID"
overall="done"

for arm in $ARMS; do
  run="$RUN_PREFIX-$arm-$TS"
  dir="$SHARED/calib/$run"
  if [ ! -d "$dir" ]; then
    say "NOT-VERIFIED: нет каталога руки $arm ($dir)"
    row "$arm" "missing" "-" "$run"
    overall="not_verified"
    break
  fi
  if [ ! -f "$dir/chain_command.txt" ]; then
    say "NOT-VERIFIED: у руки $arm нет chain_command.txt — запускать нечего"
    row "$arm" "missing" "-" "$run"
    overall="not_verified"
    break
  fi

  # Предыдущая рука обязана быть закрыта: контейнер прошлой руки, оставшийся
  # живым, — это вторая нагрузка на GB10, то есть нарушение AD-5, а не «мелочь».
  waited=0
  while :; do
    #: Имя контейнера руки начинается с `laguna-<префикс>-`: у контрольных рук это
    #: `laguna-ctrl-*`, и чужой контейнер здесь считался бы «не закрывшимся».
    left="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c "^laguna-$RUN_PREFIX-" || true)"
    [ "$left" = "0" ] && break
    if [ "$waited" -ge 1800 ]; then
      say "ОТКАЗ: контейнеры прошлой руки не закрылись за 30 мин ($left шт.) — сетка остановлена"
      row "$arm" "previous_container_alive" "$waited" "$run"
      overall="failed"
      break 2
    fi
    say "ждём закрытия контейнеров прошлой руки: $left шт., ${waited} с"
    sleep "$POLL_SEC"
    waited=$((waited + POLL_SEC))
  done

  cmd="$(cat "$dir/chain_command.txt")"
  rm -f "$dir/var/chain.status"
  say "СТАРТ руки $arm: $run"
  say "  строка запуска: $cmd"
  row "$arm" "start" "-" "$run"
  t0="$(date +%s)"
  bash -c "$cmd" >> "$dir/logs/chain.log" 2>&1 &
  chain_pid=$!

  last=""
  while :; do
    st="$(cat "$dir/var/chain.status" 2>/dev/null || echo none)"
    case "$st" in
      done|failed|precondition_failed|paused) break ;;
    esac
    ck="$(ls "$dir/checkpoints" 2>/dev/null | grep -c 'checkpoint' || true)"
    step="$(tail -1 "$dir/logs/loss_trace.jsonl" 2>/dev/null \
            | sed -n 's/.*"step": *\([0-9]*\).*/\1/p')"
    now="$ck/$step"
    if [ "$now" != "$last" ]; then
      say "  $arm: чекпойнтов $ck, последний шаг траектории ${step:-—}"
      last="$now"
    fi
    elapsed=$(( $(date +%s) - t0 ))
    if [ "$elapsed" -gt "$ARM_TIMEOUT" ]; then
      say "ТАЙМАУТ руки $arm (>${ARM_TIMEOUT} с): закрываю контейнер и останавливаю сетку"
      docker rm -f "laguna-$RUN_PREFIX-$TS-$arm-cpt" >/dev/null 2>&1 || true
      kill "$chain_pid" 2>/dev/null || true
      row "$arm" "timeout" "$elapsed" "$run"
      overall="failed"
      break 2
    fi
    sleep "$POLL_SEC"
  done
  wait "$chain_pid" 2>/dev/null
  elapsed=$(( $(date +%s) - t0 ))
  st="$(cat "$dir/var/chain.status" 2>/dev/null || echo none)"
  say "ИТОГ руки $arm: $st за ${elapsed} с"
  row "$arm" "$st" "$elapsed" "$run"
  if [ "$st" != "done" ]; then
    say "серия остановлена на руке $arm: статус $st (следующие руки не запускаются)"
    overall="failed"
    break
  fi
done

echo "$overall" > "$GRID/status"
say "== серия завершена: $overall =="

case "$overall" in
  done) exit 0 ;;
  not_verified) exit 2 ;;
  *) exit 1 ;;
esac
