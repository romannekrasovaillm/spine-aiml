#!/usr/bin/env bash
# AD-9 (pending evidence) — «хозяин ресурса объявлен, а не выясняется гонкой».
#
# Страж перед тренировочной стадией ревизии: проверяет не «здоровье стенда», а
# **право стартовать**. Инцидент 14.09.2026: освобождённый под ревизию стенд через
# несколько часов снова занял конкурирующий прогон 3B-SFT — саморемонтирующийся
# сторож лесенки вернул себя в crontab и через `restart_v12.sh` возобновил прогон.
# Свободной памяти осталось 49 ГБ, то есть меньше порога SFT (≥50 ГБ, ADR-008):
# ревизия физически не могла стартовать, и выяснилось это не из вердикта, а из
# замера. Этот страж превращает такой факт в вердикт **до** старта стадии.
#
# Три проверки (все — только чтение):
#
#   1. **Хозяин объявлен и пауза полная.** Достаточно одного из двух:
#      (а) на стенде есть флаг паузы `~/gb10-shared/REVISION-WINDOW.lock`
#          (машинный признак паузы, ADR-012 п.3) **и контур лесенки его читает**:
#          в `restart_v12.sh` есть проверка файла-флага. Сенсор читает текст
#          скрипта (`--restart-script`), потому что флаг без поддержки — это
#          декларация, а не защита: `@reboot`-строка зовёт `restart_v12.sh`
#          напрямую, и скрипт, не знающий флага, всё равно добьёт контейнеры и
#          поднимет лесенку (AD-9, уточнение 14.09.2026); либо
#      (б) сторож-скрипт лесенки **неисполняем** (`chmod -x`, шаг «в» ADR-012 п.2)
#          И в crontab нет активных строк сторожа семейства лесенки
#          (`laguna_watchdog*`, включая переименованные версии) — ни названного
#          скрипта, ни его соседей по семейству. Оба условия вместе, а не любое:
#          правка крона без `chmod -x` бесполезна — сторож сам вернёт себя за
#          ≤15 минут (измерено), а `chmod -x` без правки крона оставляет запуск
#          по расписанию (он упадёт, но факт остаётся фактом).
#   1a. **Активный `@reboot`-автозапуск лесенки — блокирующий критерий**, а не
#      предупреждение (уточнение AD-9 по факту пробы S3a, где страж печатал
#      предупреждение). Проверяется **до** обеих ветвей (а)/(б) и снимается ровно
#      одним способом: флагом паузы, **поддержанным скриптом** — `@reboot`-строка
#      зовёт `restart_v12.sh`, а тот на флаге останавливается, не трогая
#      контейнеры (факт S3c: прогон при флаге не убил контейнеры и не поднял
#      лесенку). Ни права сторожа, ни «строки крона нет» `@reboot`-строку не
#      отменяют: она живёт своей жизнью. Без поддержки в скрипте флаг не
#      отменяет и её — один ребут возвращает конкурирующий прогон и убивает
#      стадию ревизии.
#   2. **Бюджет памяти.** Свободной unified-памяти ≥ порога стадии:
#      `--stage cpt|sft|rl` → 35/50/49 ГБ (ADR-008 п.2, ADR-011 п.2). Замер — по
#      `MemAvailable` из `/proc/meminfo`: у GB10 память общая с CPU, и
#      `nvidia-smi` про занятое не говорит ничего.
#   3. **Нагрузок не больше одной.** Опознание нагрузки по `--exp_name` (правило
#      AD-5, см. `tools/check_gb10_serialization.sh`): один прогон стадии — это
#      дерево из шести процессов, и подсчёт «по процессам» объявил бы одну
#      нагрузку нарушением. Если задан `--exp-name`, единственная нагрузка обязана
#      быть **своей**: чужая нагрузка на стенде — это конкурирующий прогон, а не
#      «одна из».
#
# СТРОГО ЧТЕНИЕ. Флаг паузы **только читается**: создавать (и снимать) его —
# решение владельца, деструктив на стенде — R4+ (AD-9, ADR-012 п.5). Страж не
# пишет файлов, не правит crontab, не останавливает процессы и контейнеры. Страж,
# который «чинит» хозяина ресурса, сам становится инцидентом.
#
# Исходы:
#   0 — можно стартовать: печатает вердикт и причину допуска (какая из ветвей
#       объявления хозяина сработала, запас памяти, число нагрузок);
#   2 — нельзя: печатает причину отказа по каждой проваленной проверке
#       (включая активный `@reboot`-автозапуск лесенки). Сюда же попадает
#       недоступность стенда и непрочитанный crontab: на гейте «не смогли
#       проверить» обязано быть красным, а не «пропущено» (ADR-007 п.5) — иначе
#       вердикт получает тот, кто ничего не доказал.
#   0 + NOT-VERIFIED — только при явном `--unreachable-not-verified`: стенд
#       недоступен, проверка не состоялась. Это профиль fitness-правила C-018
#       (правило исполняется и вне стенда, где «стенд недоступен» — не дефект
#       архитектуры, а отсутствие сенсора). Печатается словом NOT-VERIFIED и в
#       человеческом, и в машинном вердикте; сенсорные отказы (стенд есть,
#       данных нет) остаются exit 2 и в этом профиле. **Предусловие тренировочной
#       стадии обязано звать страж без этого флага** — стадия, стартующая на
#       стенде, которого не видно, не должна получать зелёный вердикт.
#
# Что страж НЕ проверяет (названо явно, чтобы «зелёно» не читалось шире факта):
#   · намерения людей: объявленный хозяин и фактическая дисциплина — разные вещи;
#   · автозапуск лесенки помимо крона (`systemd`-юниты, `.bashrc`, ручной старт) —
#     проверяется crontab, и это граница сенсора;
#   · **порядок ветвей внутри `restart_v12.sh`**: сенсор поддержки ищет в тексте
#     проверку файла-флага (`-f … REVISION-WINDOW.lock`), но не доказывает, что она
#     стоит до `docker kill`. Это доказывается прогоном (S3c: запуск при флаге не
#     тронул контейнеры и не поднял лесенку — лог `v12_restart.log`), а не чтением;
#     страж называет границу, чтобы «поддержан» не читалось как «проверено
#     исполнением».
#
# Источник стенда, порядок: `--host H` → `$GB10_HOST` → локальный GB10
# (nvidia-smi) → список кандидатов `--hosts "H1 H2"`/`$GB10_HOSTS` → по умолчанию
# `gb10-fast`, затем `gb10` (как в страже AD-5: `gb10` из рабочей сети недоступен,
# рабочий алиас — `gb10-fast`).
#
# Запуск: bash tools/check_resource_owner.sh --stage sft [--host HOST] [--exp-name N]
#         [--lock PATH] [--watchdog PATH] [--restart-script PATH]
#         [--unreachable-not-verified] [--json]

set -uo pipefail

#: LC_ALL=C — иначе awk печатает десятичную запятую (78,6) в машинном вердикте,
#: а `--json` перестаёт быть JSON. Вердикт читает и человек, и раннер стадии.
export LC_ALL=C

STAGE=""
HOST="${GB10_HOST:-}"
HOSTS_SPEC="${GB10_HOSTS:-}"
TIMEOUT=8
EXP_NAME=""
LOCK="${GB10_REVISION_LOCK:-/home/user/gb10-shared/REVISION-WINDOW.lock}"
WATCHDOG="${GB10_LADDER_WATCHDOG:-/home/user/gb10-shared/laguna_watchdog_v12.sh}"
#: Скрипт, который зовёт `@reboot`-строка (и сторож). Флаг паузы действителен, только
#: если этот скрипт его читает (AD-9): иначе ребут всё равно вернёт прогон.
RESTART_SCRIPT="${GB10_LADDER_RESTART:-/home/user/gb10-shared/restart_v12.sh}"
UNREACHABLE_NV="${GB10_OWNER_UNREACHABLE_NV:-0}"
JSON=0

DEFAULT_HOSTS=(gb10-fast gb10)

usage() { sed -n '2,90p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --stage)    STAGE="${2:?--stage требует значение}"; shift 2 ;;
    --host)     HOST="${2:?--host требует значение}"; shift 2 ;;
    --hosts)    HOSTS_SPEC="${2:?--hosts требует непустой список}"; shift 2 ;;
    --timeout)  TIMEOUT="${2:?--timeout требует секунды}"; shift 2 ;;
    --exp-name) EXP_NAME="${2:?--exp-name требует значение}"; shift 2 ;;
    --unreachable-not-verified) UNREACHABLE_NV=1; shift ;;
    --lock)     LOCK="${2:?--lock требует путь}"; shift 2 ;;
    --watchdog) WATCHDOG="${2:?--watchdog требует путь}"; shift 2 ;;
    --restart-script) RESTART_SCRIPT="${2:?--restart-script требует путь}"; shift 2 ;;
    --json)     JSON=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "неизвестный флаг: $1" >&2; exit 2 ;;
  esac
done

#: Порог свободной unified-памяти по стадии (ADR-008 п.2: CPT 35 / SFT 50;
#: ADR-011 п.2: RL 49 вместо 40 из ADR-010 — порог стоит на замере).
stage_threshold() {
  case "$1" in
    cpt) echo 35 ;;
    sft) echo 50 ;;
    rl)  echo 49 ;;
    *)   echo "" ;;
  esac
}
STAGE_RU=""
case "$STAGE" in
  cpt) STAGE_RU="CPT" ;;
  sft) STAGE_RU="SFT" ;;
  rl)  STAGE_RU="RL" ;;
esac

if [ -z "$STAGE" ]; then
  echo "не указана стадия: --stage cpt|sft|rl (порог памяти стадии — ADR-008/ADR-011)" >&2
  echo >&2
  usage >&2
  exit 2
fi
MIN_FREE_GB="$(stage_threshold "$STAGE")"
if [ -z "$MIN_FREE_GB" ]; then
  echo "неизвестная стадия: $STAGE (ожидается cpt|sft|rl)" >&2
  exit 2
fi

#: Шаблоны тренировочных нагрузок контура — те же, что в страже AD-5
#: (`tools/check_gb10_serialization.sh`). Держать их в согласии обязывает не
#: комментарий, а тест: `tools/tests/run_tool_tests.sh` §13 подаёт обоим стражaм
#: одну и ту же раскладку процессов и сверяет вердикты.
LOAD_RE='laguna_pipeline|run_v[0-9]+_ladder|train_(cpt|sft|rl)|torchrun|deepspeed|accelerate launch|python[0-9.]* +[^ ]*(cpt|sft|_rl)'
SELF_RE='check_resource_owner|check_gb10_serialization|ps -eo|nvidia-smi|tmux list-panes|crontab -l'

#: Автозапуск автономного контура лесенки в кроне (шаг «г» паузы, ADR-012 п.2).
#: Активная такая строка — **блокирующий критерий** (AD-9, уточнение по факту
#: пробы S3a): один ребут возвращает конкурирующий прогон, потому что
#: `restart_v12.sh` зовётся напрямую и флага паузы не читает. Снятие строк —
#: действие владельца (R4+), страж её только читает и называет.
LADDER_AUTOSTART_RE='laguna_watchdog|restart_v[0-9]+|run_v[0-9]+_ladder|_ladder\.sh'
#: Семейство сторожей лесенки. Блокирует не только строка названного сторожа, но и
#: строка любого `laguna_watchdog*`: сторож переименовывается от версии к версии
#: (v10 → v12), и «строки нет» про конкретное имя пропустило бы живого сторожа
#: соседней версии — то есть ровно тот возврат прогона, от которого AD-9 защищает.
WATCHDOG_FAMILY_RE='laguna_watchdog'

#: Группировка совпавших процессов по нагрузке (то же правило, что в страже AD-5).
LOAD_ID_AWK='
function expname(s,    i, n, a) {
  n = split(s, a, " ")
  for (i = 1; i < n; i++) if (a[i] == "--exp_name") return a[i + 1]
  return ""
}
NF == 0 { next }
{
  n++
  pid[n] = $1; par[$1] = $2; txt[n] = $0
  e[pid[n]] = expname($0)
  if (e[pid[n]] != "") has_exp = 1
}
END {
  if (has_exp) {
    for (i = 1; i <= n; i++) {
      p = pid[i]
      if (e[p] == "" || (e[p] in seen)) continue
      seen[e[p]] = 1; m++; order[m] = e[p]
    }
    print m
    print "exp_name"
    for (j = 1; j <= n; j++) {
      p = pid[j]
      tag = (e[p] != "") ? e[p] : "-"
      printf "    [%s] %s\n", tag, txt[j]
    }
  } else {
    for (i = 1; i <= n; i++) {
      p = pid[i]; cur = par[p]; hop = 0; root = 1
      while (cur != "" && cur != "0" && cur != "1" && hop < 128) {
        if (cur in par) { root = 0; break }
        cur = par[cur]; hop++
      }
      if (root) { m++; order[m] = p }
    }
    print m
    print "process-roots"
    for (j = 1; j <= n; j++) {
      p = pid[j]
      tag = "-"
      for (i = 1; i <= m; i++) if (order[i] == p) tag = "root"
      printf "    [%s] %s\n", tag, txt[j]
    }
  }
}
'

MODE=""; TARGET=""
declare -A PROBE_ERR=()
TRIED=()

nvidia_gpu_is_gb10() {
  command -v nvidia-smi >/dev/null 2>&1 || return 1
  nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null \
    | grep -qiE 'GB10|Spark|Grace *Blackwell'
}

probe() {
  local err rc
  err="$(ssh -o BatchMode=yes -o ConnectTimeout="$TIMEOUT" "$1" true </dev/null 2>&1 >/dev/null)"
  rc=$?
  #: CR снимается здесь: ssh печатает часть ошибок с CRLF, и одинокий \r делает
  #: машинный вердикт невалидным JSON (тест ловит это как «Unterminated string»).
  PROBE_ERR["$1"]="$(printf '%s' "$err" | tail -n1 | tr -d '\r')"
  return "$rc"
}

split_hosts() {
  printf '%s' "$1" | tr ',;' '  ' | tr -s ' \t' '\n' | sed '/^$/d'
}

#: json-строка одного сенсора: значение в кавычках, управляющие символы (CR/LF)
#: заменены пробелами. Вердикты собираются из чисел и имён, но в них попадают
#: сообщения ssh — а ssh печатает часть ошибок с CRLF, и одинокий \r ломает JSON.
jstr() { printf '"%s"' "$(printf '%s' "$1" | tr '\n\r' '  ' | sed 's/"/\\"/g')"; }

if [ -n "$HOST" ]; then
  MODE="ssh"; TARGET="$HOST"; TRIED=("$HOST")
  probe "$TARGET" || { MODE=""; }
elif [ -n "$HOSTS_SPEC" ]; then
  for h in $(split_hosts "$HOSTS_SPEC"); do
    TRIED+=("$h")
    if probe "$h"; then MODE="ssh"; TARGET="$h"; break; fi
  done
elif nvidia_gpu_is_gb10; then
  MODE="local"; TARGET="$(hostname)"
else
  for h in "${DEFAULT_HOSTS[@]}"; do
    TRIED+=("$h")
    if probe "$h"; then MODE="ssh"; TARGET="$h"; break; fi
  done
fi

#: Недоступность стенда — это «нельзя стартовать» и «недоказуемо» одновременно:
#: стартовать на стенде, которого не видно, всё равно нельзя, а на гейте
#: «не смогли проверить» обязано быть красным (ADR-007 п.5). Профиль
#: `--unreachable-not-verified` — исключение ровно для fitness-правила C-018,
#: которое исполняется и вне стенда; сенсоры при нём не «зелёные», а `null`.
if [ -z "$MODE" ]; then
  if [ "$UNREACHABLE_NV" = "1" ]; then
    if [ "$JSON" = "1" ]; then
      printf '{"tool": "check_resource_owner.sh", "stage": %s, "unreachable_not_verified": true, ' "$(jstr "$STAGE")"
      printf '"host": null, "mode": null, "threshold_gb": %s, ' "$MIN_FREE_GB"
      printf '"owner_ok": null, "memory_ok": null, "loads_ok": null, '
      printf '"verdict": "NOT-VERIFIED", "exit": 0, '
      printf '"reason": "стенд недоступен — проверка AD-9 не состоялась (профиль --unreachable-not-verified)", '
      printf '"hosts_tried": ['
      first=1
      for h in "${TRIED[@]:-}"; do
        [ -n "$h" ] || continue
        [ "$first" = "1" ] || printf ', '
        printf '{"host": %s, "error": %s}' "$(jstr "$h")" "$(jstr "${PROBE_ERR[$h]:-нет ответа}")"
        first=0
      done
      printf ']}\n'
    else
      echo "NOT-VERIFIED: стенд недоступен — проверка AD-9 не состоялась"
      for h in "${TRIED[@]:-}"; do
        [ -n "$h" ] || continue
        echo "  · $h: ${PROBE_ERR[$h]:-нет ответа}"
      done
      echo
      echo "ВЕРДИКТ: NOT-VERIFIED (exit 0, профиль --unreachable-not-verified) — хозяин"
      echo "ресурса не доказан, память не измерена, нагрузки не сосчитаны. Это не «чисто»:"
      echo "предусловие тренировочной стадии обязано звать страж без этого флага."
    fi
    exit 0
  fi
  if [ "$JSON" = "1" ]; then
    printf '{"tool": "check_resource_owner.sh", "stage": %s, "unreachable_not_verified": false, ' "$(jstr "$STAGE")"
    printf '"owner_ok": false, '
    printf '"verdict": "CANNOT-START", "exit": 2, "reason": "стенд недоступен — хозяин ресурса не доказан", '
    printf '"hosts_tried": ['
    first=1
    for h in "${TRIED[@]:-}"; do
      [ -n "$h" ] || continue
      [ "$first" = "1" ] || printf ', '
      printf '{"host": %s, "error": %s}' "$(jstr "$h")" "$(jstr "${PROBE_ERR[$h]:-нет ответа}")"
      first=0
    done
    printf ']}\n'
  else
    echo "NOT-VERIFIED: стенд недоступен — проверка AD-9 не состоялась"
    for h in "${TRIED[@]:-}"; do
      [ -n "$h" ] || continue
      echo "  · $h: ${PROBE_ERR[$h]:-нет ответа}"
    done
    echo
    echo "ВЕРДИКТ: СТАРТОВАТЬ НЕЛЬЗЯ — хозяин ресурса не доказан, память не измерена,"
    echo "нагрузки не сосчитаны. Недоказуемое не равно чистому (ADR-007 п.5)."
  fi
  exit 2
fi

run_remote() {
  if [ "$MODE" = "local" ]; then
    bash -c "$1"
  else
    ssh -o BatchMode=yes -o ConnectTimeout="$TIMEOUT" "$TARGET" "$1"
  fi
}

reasons_ok=()
reasons_fail=()
warnings=()

echo "== AD-9: хозяин ресурса GB10 =="
echo "стенд: $TARGET ($MODE)"
echo "стадия: $STAGE (порог свободной unified-памяти $MIN_FREE_GB ГБ, ADR-008/ADR-011)"
echo

# ── проверка 1: хозяин объявлен ───────────────────────────────────────────────
LOCK_PRESENT=0
WATCHDOG_EXEC="unknown"
CRON_READABLE=0
CRON_ACTIVE=""
CRON_DISABLED=0
CRON_WATCHDOG=0
CRON_WATCHDOG_LINES=""
CRON_AUTOSTART=""

if run_remote "[ -f '$LOCK' ] && echo yes || echo no" 2>/dev/null | grep -q '^yes$'; then
  LOCK_PRESENT=1
fi

wd_perm="$(run_remote "if [ -e '$WATCHDOG' ]; then [ -x '$WATCHDOG' ] && echo exec || echo noexec; else echo missing; fi" 2>/dev/null)"
case "$wd_perm" in
  exec)    WATCHDOG_EXEC=1 ;;
  noexec)  WATCHDOG_EXEC=0 ;;
  missing) WATCHDOG_EXEC=-1 ;;
esac

#: stderr crontab нужен, чтобы отличить «строк нет» (`no crontab for …`, это не
#: отказ чтения) от «не прочитан» (недоказуемо → красный вердикт).
CRON_ERR_FILE="$(mktemp)"
trap 'rm -f "$CRON_ERR_FILE"' EXIT
cron_out="$(run_remote "crontab -l" 2>"$CRON_ERR_FILE")"
cron_rc=$?
cron_err="$(cat "$CRON_ERR_FILE" 2>/dev/null)"
if [ "$cron_rc" -eq 0 ]; then
  CRON_READABLE=1
  CRON_ACTIVE="$(printf '%s\n' "$cron_out" | grep -vE '^[[:space:]]*(#|$)' || true)"
  CRON_DISABLED="$(printf '%s' "$cron_out" | grep -cE '^[[:space:]]*#' || true)"
elif printf '%s' "$cron_err" | grep -qi 'no crontab'; then
  # «нет crontab у пользователя» — это отсутствие строк, а не отказ чтения.
  CRON_READABLE=1
fi
CRON_AUTOSTART_N=0
if [ "$CRON_READABLE" = "1" ]; then
  wd_base="$(basename "$WATCHDOG")"
  CRON_WATCHDOG_LINES="$(printf '%s\n' "$CRON_ACTIVE" \
    | grep -E -- "$wd_base|$WATCHDOG_FAMILY_RE" || true)"
  CRON_WATCHDOG="$(printf '%s' "$CRON_WATCHDOG_LINES" | grep -c . || true)"
  #: Строки сторожа — отдельный критерий (проверка 1б) и названы им же: здесь они
  #: не дублируются, иначе один факт читался бы как два разных.
  CRON_AUTOSTART="$(printf '%s\n' "$CRON_ACTIVE" \
    | grep -E "$LADDER_AUTOSTART_RE" \
    | grep -vxFf <(printf '%s\n' "$CRON_WATCHDOG_LINES") || true)"
  CRON_AUTOSTART_N="$(printf '%s' "$CRON_AUTOSTART" | grep -c . || true)"
fi
#: Активные строки автозапуска одной строкой — для вердикта и JSON (перенос строки
#: внутри элемента массива сломал бы построчный вывод).
CRON_AUTOSTART_ONE="$(printf '%s' "$CRON_AUTOSTART" | tr '\n' ';' | cut -c1-200)"

#: Поддержка флага контуром лесенки (AD-9): ищем в тексте `restart_v12.sh` проверку
#: файла-флага. Без неё флаг — декларация: `@reboot`-строка зовёт скрипт напрямую, и
#: он всё равно добьёт контейнеры и поднимет лесенку. Сенсор читает текст (граница
#: сенсора: порядок ветвей внутри скрипта он не доказывает — это доказывает прогон).
FLAG_SUPPORTED=0
FLAG_SUPPORT_ERR=""
_rs="$(run_remote "if [ -r '$RESTART_SCRIPT' ]; then grep -Eq -- '-f.*REVISION-WINDOW\.lock' '$RESTART_SCRIPT' && echo yes || echo no; else echo unreadable; fi" 2>/dev/null)"
case "$_rs" in
  yes) FLAG_SUPPORTED=1 ;;
  no)  FLAG_SUPPORT_ERR="$(basename "$RESTART_SCRIPT") не читает REVISION-WINDOW.lock" ;;
  *)   FLAG_SUPPORT_ERR="текст $(basename "$RESTART_SCRIPT") не прочитан — поддержка флага недоказуема" ;;
esac

#: Порядок ветвей — от «недоказуемо» к «доказано»: непрочитанный crontab делает
#: недоказуемой и ветвь «строк нет», поэтому он первый. Активный `@reboot` идёт
#: **до** флага паузы, но снимается флагом, **поддержанным скриптом**: `@reboot`-строка
#: зовёт `restart_v12.sh`, и поддержанный флаг останавливает его на входе — ребут
#: перестаёт возвращать прогон (уточнение AD-9, факт S3c). Флаг без поддержки не
#: снимает ничего: и сам он не защита, и `@reboot`-строка остаётся блокирующей.
AUTOSTART_DEFUSED=0
if [ "$LOCK_PRESENT" = "1" ] && [ "$FLAG_SUPPORTED" = "1" ]; then
  AUTOSTART_DEFUSED=1
fi
OWNER_BRANCH=""
owner_ok=0
if [ "$CRON_READABLE" != "1" ]; then
  OWNER_BRANCH="cron-unreadable"
  owner_detail="crontab не прочитан — ветвь «строки крона нет» недоказуема"
  reasons_fail+=("хозяин не доказан: crontab не прочитан (${cron_err:-причина не названа})")
elif [ "$CRON_AUTOSTART_N" != "0" ] && [ "$AUTOSTART_DEFUSED" != "1" ]; then
  OWNER_BRANCH="autostart-blocked"
  owner_detail="в кроне $CRON_AUTOSTART_N активных строк автозапуска автономного контура — один ребут возвращает конкурирующий прогон (ADR-012 п.2г)"
  reasons_fail+=("хозяин не доказан: активный @reboot-автозапуск лесенки — $CRON_AUTOSTART_ONE")
  #: Флаг есть, но не поддержан: это отдельная причина, а не тот же факт — иначе
  #: «положите флаг» читалось бы как достаточное действие.
  if [ "$LOCK_PRESENT" = "1" ]; then
    reasons_fail+=("хозяин не доказан: флаг паузы $(basename "$LOCK") есть, но $FLAG_SUPPORT_ERR — флаг без поддержки не защита (AD-9)")
  fi
elif [ "$LOCK_PRESENT" = "1" ] && [ "$FLAG_SUPPORTED" = "1" ]; then
  OWNER_BRANCH="lock-supported"
  owner_ok=1
  reasons_ok+=("хозяин объявлен флагом паузы $(basename "$LOCK"), и контур лесенки его читает ($(basename "$RESTART_SCRIPT"))")
  if [ "$CRON_AUTOSTART_N" != "0" ]; then
    owner_detail="флаг паузы $(basename "$LOCK") на месте и поддержан $(basename "$RESTART_SCRIPT"): активная @reboot-строка автозапуска обезврежена — скрипт остановится на флаге, не тронув контейнеры"
  else
    owner_detail="флаг паузы $(basename "$LOCK") на месте и поддержан $(basename "$RESTART_SCRIPT") — контур лесенки не может вернуть прогон"
  fi
elif [ "$LOCK_PRESENT" = "1" ]; then
  OWNER_BRANCH="lock-unsupported"
  owner_detail="флаг паузы $(basename "$LOCK") есть, но $FLAG_SUPPORT_ERR — флаг без поддержки контуром лесенки (AD-9)"
  reasons_fail+=("хозяин не доказан: флаг паузы $(basename "$LOCK") есть, но $FLAG_SUPPORT_ERR")
elif [ "$WATCHDOG_EXEC" = "1" ]; then
  OWNER_BRANCH="watchdog-executable"
  owner_detail="сторож исполняем ($(basename "$WATCHDOG")) — пауза не выполнена (нужен chmod -x, ADR-012 п.2в)"
  reasons_fail+=("хозяин не доказан: сторож исполняем и флага паузы нет")
elif [ "$CRON_WATCHDOG" != "0" ]; then
  OWNER_BRANCH="watchdog-cron-line"
  owner_detail="сторож неисполняем, но его активная строка в кроне есть ($CRON_WATCHDOG шт.) — пауза неполная (ADR-012 п.2г)"
  reasons_fail+=("хозяин не доказан: строка сторожа активна в crontab")
elif [ "$WATCHDOG_EXEC" = "-1" ]; then
  #: Отсутствие файла и снятые права — разные состояния: первое держит паузу
  #: «потому что скрипта нет», второе — выполнением шага «в» ADR-012. Названы
  #: по-разному, потому что и восстановление у них разное.
  OWNER_BRANCH="watchdog-missing"
  owner_ok=1
  owner_detail="флага паузы нет; сторожа $(basename "$WATCHDOG") на стенде нет вовсе и его строки в кроне нет — пауза держится отсутствием файла, а не снятыми правами"
  reasons_ok+=("автономный контур не может вернуть прогон: сторожа нет и его строки в кроне нет")
else
  OWNER_BRANCH="watchdog-noexec"
  owner_ok=1
  owner_detail="флага паузы нет, но пауза выполнена связкой: сторож неисполняем ($(basename "$WATCHDOG")) и его активной строки в кроне нет"
  reasons_ok+=("хозяин объявлен связкой «сторож неисполняем + нет строки крона» (ADR-012 п.3)")
fi

#: Сторож не найден вовсе — пауза «выполнена» от того, что скрипта нет: это надо
#: назвать, а не выдать за тот же факт (отсутствие файла и снятые права — разные
#: состояния, и второе обратимо одной командой).
if [ "$WATCHDOG_EXEC" = "-1" ] && [ "$LOCK_PRESENT" != "1" ]; then
  warnings+=("сторожа $(basename "$WATCHDOG") на стенде нет вовсе — пауза держится отсутствием файла, а не снятыми правами: восстановление скрипта владельцем вернёт автозапуск")
fi

#: Остаточная щель, которую флаг не закрывает: сторож лесенки гасит контейнер сам
#: (`docker kill`), не спрашивая флага, и только потом зовёт `restart_v12.sh`. Пауза
#: держится неисполняемостью сторожа и отсутствием его строки в кроне; если это не
#: так, флаг спасает от `@reboot`, но не от сторожа — назвать, а не умолчать.
if [ "$LOCK_PRESENT" = "1" ] && { [ "$WATCHDOG_EXEC" = "1" ] || [ "$CRON_WATCHDOG" != "0" ]; }; then
  warnings+=("флаг паузы не останавливает сторожа $(basename "$WATCHDOG"): тот гасит контейнер сам и лишь потом зовёт $(basename "$RESTART_SCRIPT") — пауза держится неисполняемостью сторожа и отсутствием его строки в кроне")
fi

# ── проверка 2: свободная unified-память ──────────────────────────────────────
mem_raw="$(run_remote "cat /proc/meminfo" 2>/dev/null)"
MEM_TOTAL_GB=""; MEM_AVAIL_GB=""; mem_ok=0
if [ -n "$mem_raw" ]; then
  total_kb="$(printf '%s\n' "$mem_raw" | awk '/^MemTotal:/{print $2}')"
  avail_kb="$(printf '%s\n' "$mem_raw" | awk '/^MemAvailable:/{print $2}')"
  if [ -n "$avail_kb" ]; then
    MEM_TOTAL_GB="$(awk -v k="$total_kb" 'BEGIN{printf "%.1f", k/1048576}')"
    MEM_AVAIL_GB="$(awk -v k="$avail_kb" 'BEGIN{printf "%.1f", k/1048576}')"
    if awk -v a="$MEM_AVAIL_GB" -v t="$MIN_FREE_GB" 'BEGIN{exit !(a >= t)}'; then
      mem_ok=1
      reasons_ok+=("память: доступно $MEM_AVAIL_GB ГБ ≥ порога $MIN_FREE_GB ГБ")
    else
      reasons_fail+=("память: доступно $MEM_AVAIL_GB ГБ < порога $MIN_FREE_GB ГБ (ADR-008/ADR-011)")
    fi
  fi
fi
if [ -z "$MEM_AVAIL_GB" ]; then
  reasons_fail+=("память: MemAvailable не прочитан — недоказуемо")
fi

# ── проверка 3: нагрузок не больше одной ──────────────────────────────────────
ps_out="$(run_remote "ps -eo pid,ppid,etime,args --no-headers" 2>/dev/null)"
N_LOADS=""; LOAD_MODE=""; LOAD_TAGS=""; loads_ok=0
if [ -n "$ps_out" ]; then
  loads="$(printf '%s\n' "$ps_out" | grep -E "$LOAD_RE" | grep -vE "$SELF_RE" || true)"
  n_procs="$(printf '%s' "$loads" | grep -c . || true)"
  if [ "$n_procs" -gt 0 ]; then
    report="$(printf '%s\n' "$loads" | awk "$LOAD_ID_AWK")"
    N_LOADS="$(printf '%s\n' "$report" | sed -n '1p')"
    LOAD_MODE="$(printf '%s\n' "$report" | sed -n '2p')"
    LOAD_TAGS="$(printf '%s\n' "$report" | sed -n '3,$p' | grep -oE '\[[^]]+\]' | tr -d '[]' | tr '\n' ' ')"
  else
    N_LOADS=0; LOAD_MODE="exp_name"
  fi
  if [ "$N_LOADS" -le 1 ]; then
    if [ "$N_LOADS" = "1" ] && [ -n "$EXP_NAME" ]; then
      if printf '%s' "$LOAD_TAGS" | tr ' ' '\n' | grep -qx -- "$EXP_NAME"; then
        loads_ok=1
        reasons_ok+=("нагрузка 1 — это своя стадия $EXP_NAME")
      else
        reasons_fail+=("на стенде идёт чужая нагрузка (${LOAD_TAGS:-без --exp_name}), а не $EXP_NAME")
      fi
    else
      loads_ok=1
      reasons_ok+=("тренировочных нагрузок $N_LOADS ≤ 1")
    fi
  else
    reasons_fail+=("на стенде $N_LOADS тренировочных нагрузок одновременно (AD-5: одна за раз)")
  fi
else
  reasons_fail+=("ps недоступен — число нагрузок не сосчитано")
fi

# ── вердикт ───────────────────────────────────────────────────────────────────
echo "[$( [ "$owner_ok" = 1 ] && echo 'ok  ' || echo 'FAIL')] хозяин    $owner_detail"
#: Сенсор поддержки печатается только при выставленном флаге: без флага он бы
#: выглядел как ещё один критерий допуска, хотя это свойство скрипта, а не паузы.
if [ "$LOCK_PRESENT" = "1" ]; then
  echo "[$( [ "$FLAG_SUPPORTED" = 1 ] && echo 'ok  ' || echo 'FAIL')] поддержка флаг паузы читает $(basename "$RESTART_SCRIPT"): $( [ "$FLAG_SUPPORTED" = 1 ] && echo 'да' || echo "нет — $FLAG_SUPPORT_ERR")"
fi
if [ "$mem_ok" = 1 ]; then
  echo "[ok  ] память    доступно $MEM_AVAIL_GB ГБ из $MEM_TOTAL_GB ГБ (порог $MIN_FREE_GB)"
else
  echo "[FAIL] память    ${MEM_AVAIL_GB:-не прочитана} ГБ против порога $MIN_FREE_GB"
fi
if [ "$loads_ok" = 1 ]; then
  echo "[ok  ] нагрузки  тренировочных нагрузок: $N_LOADS (опознание: $LOAD_MODE)"
elif [ -n "$N_LOADS" ]; then
  echo "[FAIL] нагрузки  тренировочных нагрузок: $N_LOADS (опознание: $LOAD_MODE)"
else
  echo "[FAIL] нагрузки  число нагрузок не сосчитано"
fi
if [ "${#warnings[@]}" -gt 0 ]; then
  echo
  for w in "${warnings[@]}"; do echo "  · ПРЕДУПРЕЖДЕНИЕ: $w"; done
fi
echo

verdict_ok=1
[ "$owner_ok" = 1 ] && [ "$mem_ok" = 1 ] && [ "$loads_ok" = 1 ] || verdict_ok=0

if [ "$verdict_ok" = 1 ]; then
  reason="$(printf '%s; ' "${reasons_ok[@]}")"
  reason="${reason%; }"
  echo "ВЕРДИКТ: МОЖНО СТАРТОВАТЬ — $reason"
  echo "AD-9 OK: хозяин ресурса объявлен, бюджет стадии $STAGE_RU соблюдён."
else
  echo "ВЕРДИКТ: СТАРТОВАТЬ НЕЛЬЗЯ — причина:"
  for r in "${reasons_fail[@]}"; do echo "  · $r"; done
  if [ "${#reasons_fail[@]}" -eq 0 ]; then echo "  · причина не названа (ошибка стража)"; fi
fi

if [ "$JSON" = "1" ]; then
  printf '{"tool": "check_resource_owner.sh", "host": %s, "mode": %s, "stage": %s, ' \
    "$(jstr "$TARGET")" "$(jstr "$MODE")" "$(jstr "$STAGE")"
  printf '"unreachable_not_verified": false, "owner_branch": %s, ' "$(jstr "$OWNER_BRANCH")"
  printf '"threshold_gb": %s, "mem_available_gb": %s, "mem_total_gb": %s, ' \
    "$MIN_FREE_GB" "${MEM_AVAIL_GB:-null}" "${MEM_TOTAL_GB:-null}"
  printf '"lock_present": %s, "watchdog_executable": %s, "cron_readable": %s, ' \
    "$( [ "$LOCK_PRESENT" = 1 ] && echo true || echo false)" \
    "$( [ "$WATCHDOG_EXEC" = 1 ] && echo true || echo false)" \
    "$( [ "$CRON_READABLE" = 1 ] && echo true || echo false)"
  printf '"flag_supported": %s, "restart_script": %s, "flag_support_error": %s, ' \
    "$( [ "$FLAG_SUPPORTED" = 1 ] && echo true || echo false)" \
    "$(jstr "$RESTART_SCRIPT")" \
    "$(jstr "$FLAG_SUPPORT_ERR")"
  printf '"cron_watchdog_lines": %s, "cron_disabled_lines": %s, ' \
    "${CRON_WATCHDOG:-0}" "${CRON_DISABLED:-0}"
  printf '"cron_autostart_lines": %s, "cron_autostart_blocking": %s, "cron_autostart_defused": %s, "cron_autostart": %s, ' \
    "${CRON_AUTOSTART_N:-0}" \
    "$( [ "${CRON_AUTOSTART_N:-0}" != "0" ] && [ "$AUTOSTART_DEFUSED" != "1" ] && echo true || echo false)" \
    "$( [ "$AUTOSTART_DEFUSED" = "1" ] && echo true || echo false)" \
    "$(jstr "$CRON_AUTOSTART_ONE")"
  printf '"n_loads": %s, "load_tags": %s, ' "${N_LOADS:-null}" "$(jstr "$LOAD_TAGS")"
  printf '"owner_ok": %s, "memory_ok": %s, "loads_ok": %s, ' \
    "$( [ "$owner_ok" = 1 ] && echo true || echo false)" \
    "$( [ "$mem_ok" = 1 ] && echo true || echo false)" \
    "$( [ "$loads_ok" = 1 ] && echo true || echo false)"
  printf '"warnings": ['
  first=1
  for w in "${warnings[@]:-}"; do
    [ -n "$w" ] || continue
    [ "$first" = "1" ] || printf ', '
    printf '%s' "$(jstr "$w")"; first=0
  done
  printf '], "verdict": %s, "exit": %s}\n' \
    "$( [ "$verdict_ok" = 1 ] && echo '"CAN-START"' || echo '"CANNOT-START"')" \
    "$( [ "$verdict_ok" = 1 ] && echo 0 || echo 2)"
fi

[ "$verdict_ok" = 1 ] || exit 2
exit 0
