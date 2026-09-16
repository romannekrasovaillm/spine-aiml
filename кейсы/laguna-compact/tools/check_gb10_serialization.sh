#!/usr/bin/env bash
# AD-5 (pending evidence) — «на GB10 одна GPU-нагрузка за раз».
#
# Смоук-страж ресурсного инварианта до появления behavioural-правила: проверяет,
# что на стенде GB10 нет двух одновременных тренировочных нагрузок и что
# tmux-панели стадий не пересекаются во времени.
#
# СТРОГО ЧТЕНИЕ. Скрипт не убивает, не приостанавливает и не трогает процессы:
# только `ps`, `nvidia-smi --query-compute-apps` и `tmux list-panes`. Страж,
# который «чинит» занятость ресурса, сам становится инцидентом.
#
# Четыре исхода:
#   0 — OK (нагрузок ≤ 1) ИЛИ стенд недоступен в обычном режиме
#       («gb10 not reachable — check skipped» + строка NOT-VERIFIED)
#   1 — НАРУШЕНИЕ: подтверждённые параллельные нагрузки стадий
#   2 — NOT-VERIFIED: стенд доступен, но ни один сенсор не дал данных,
#       ЛИБО (--strict) стенд недоступен вовсе
#
# --strict (ADR-007 п.5) — гейтовый профиль: на гейтах A4/A5 недоступность стенда
# обязана быть красным вердиктом, а не «пропущено». Иначе «не смогли проверить»
# молча превращается в «проверено и чисто», и AD-5 держит [PENDING-EVIDENCE]
# бесконечно. В обычном режиме поведение прежнее (exit 0), но вердикт печатается
# как NOT-VERIFIED: «недоступен» — это «не доказано», а не «OK».
#
# Источник стенда, порядок: `--host H` → `$GB10_HOST` → локальный GB10
# (nvidia-smi) → список кандидатов `--hosts "H1 H2"`/`$GB10_HOSTS` → по умолчанию
# `gb10-fast`, затем `gb10`. Локальная проверка идёт перед ssh: в контуре Лагуны
# GB10 — это и есть рабочая машина, ssh туда не нужен. Кандидатов несколько,
# потому что алиасы неравноценны: факт 14.09.2026 — `gb10` (192.168.101.50) из
# рабочей сети недоступен, рабочий `gb10-fast` (10.55.0.2, прямой 10GbE).
# Берётся первый ответивший; если не ответил никто — в обычном режиме exit 0
# («стенд недоступен» не равно «инвариант нарушен»), в --strict — exit 2.
#
# Нагрузка опознаётся по имени эксперимента (`--exp_name`), а не по процессу:
# один прогон стадии — это дерево `tmux → bash -c → run_v12_ladder.sh →
# safe_start.sh → docker run → python laguna_pipeline_v8.py`, и подсчёт «по
# процессам» объявил бы ОДНУ нагрузку шестью (ложное нарушение AD-5). Если
# `--exp_name` в командных строках нет вовсе, нагрузкой считается корень
# совпавшего дерева (у него нет совпавшего предка). Точность правила названа
# честно: два одновременных прогона с ОДНИМ `--exp_name` не различаются.
#
# Запуск: bash tools/check_gb10_serialization.sh [--host HOST] [--hosts "H1 H2"] [--timeout S] [--strict]

set -uo pipefail

HOST="${GB10_HOST:-}"
HOSTS_SPEC="${GB10_HOSTS:-}"
TIMEOUT=8
STRICT="${GB10_STRICT:-0}"

#: Кандидаты по умолчанию — первый ответивший (см. шапку).
DEFAULT_HOSTS=(gb10-fast gb10)

while [ $# -gt 0 ]; do
  case "$1" in
    --host)    HOST="${2:?--host требует значение}"; shift 2 ;;
    --hosts)   HOSTS_SPEC="${2:?--hosts требует непустой список}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout требует секунды}"; shift 2 ;;
    --strict)  STRICT=1; shift ;;
    -h|--help) sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "неизвестный флаг: $1" >&2; exit 2 ;;
  esac
done

# Шаблоны тренировочных нагрузок контура. Осознанно узкие: широкий шаблон
# («python») даёт ложные срабатывания на чужих процессах и обесценивает гейт.
LOAD_RE='laguna_pipeline|run_v[0-9]+_ladder|train_(cpt|sft|rl)|torchrun|deepspeed|accelerate launch|python[0-9.]* +[^ ]*(cpt|sft|_rl)'
#: Шаблон самого стража и его транспорта — чтобы не считать себя нагрузкой.
SELF_RE='check_gb10_serialization|ps -eo|nvidia-smi|tmux list-panes'

#: Группировка совпавших процессов по нагрузке: строка 1 — число нагрузок,
#: строка 2 — режим опознания, далее — процессы с меткой нагрузки.
#: Единственное место, где «процесс» превращается в «нагрузку»; правило описано
#: в шапке. Печатается в отчёт целиком, чтобы вердикт можно было перепроверить.
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

# probe <host> — доступность ssh-хоста. Только чтение: BatchMode (без пароля),
# короткий таймаут, команда `true`; в known_hosts не пишем.
probe() {
  local err rc
  # </dev/null: ssh не должен наследовать stdin — в BatchMode это исключает любые
  # интерактивные подсказки, а если вызов снова окажется внутри `while read`, ssh
  # не съест остаток списка кандидатов.
  err="$(ssh -o BatchMode=yes -o ConnectTimeout="$TIMEOUT" "$1" true </dev/null 2>&1 >/dev/null)"
  rc=$?
  #: Последняя строка stderr — честная причина «недоступен» в отчёте.
  PROBE_ERR["$1"]="$(printf '%s' "$err" | tail -n1)"
  return "$rc"
}

split_hosts() {
  printf '%s' "$1" | tr ',;' '  ' | tr -s ' \t' '\n' | sed '/^$/d'
}

skip() {
  local h
  if [ "$STRICT" = "1" ]; then
    echo "NOT-VERIFIED: стенд недоступен — проверка AD-5 не состоялась"
    for h in "${TRIED[@]:-}"; do
      [ -n "$h" ] || continue
      echo "  · $h: ${PROBE_ERR[$h]:-нет ответа}"
    done
    echo
    echo "FAIL(strict): недоказуемое не равно чистому — на гейте недоступность"
    echo "стенда даёт красный вердикт (ADR-007 п.5), а не «пропущено»."
    exit 2
  fi
  echo "gb10 not reachable — check skipped"
  echo "NOT-VERIFIED: недоступность стенда — не доказательство AD-5; на гейте"
  echo "запускать с --strict (тогда это красный вердикт, exit 2)."
  for h in "${TRIED[@]:-}"; do
    [ -n "$h" ] || continue
    echo "  · $h: ${PROBE_ERR[$h]:-нет ответа}"
  done
  exit 0
}

if [ -n "$HOST" ]; then
  MODE="ssh"; TARGET="$HOST"; TRIED=("$HOST")
  probe "$TARGET" || skip
elif [ -n "$HOSTS_SPEC" ]; then
  # Явный список кандидатов перебивает локальное определение: так проверка
  # воспроизводима и на машине, которая сама является стендом.
  # Разбор — for по подстановке, а не `while read` из процесса: имена хостов не
  # содержат пробелов, а `while read < <(... sed ...)` гоняется с буферизацией
  # sed и теряет хвост списка.
  for h in $(split_hosts "$HOSTS_SPEC"); do
    TRIED+=("$h")
    if probe "$h"; then MODE="ssh"; TARGET="$h"; break; fi
  done
  [ -n "$MODE" ] || skip
elif nvidia_gpu_is_gb10; then
  MODE="local"; TARGET="$(hostname)"
else
  for h in "${DEFAULT_HOSTS[@]}"; do
    TRIED+=("$h")
    if probe "$h"; then MODE="ssh"; TARGET="$h"; break; fi
  done
  [ -n "$MODE" ] || skip
fi

# run_remote <команда> — исполнение только на чтение.
run_remote() {
  if [ "$MODE" = "local" ]; then
    bash -c "$1"
  else
    ssh -o BatchMode=yes -o ConnectTimeout="$TIMEOUT" "$TARGET" "$1"
  fi
}

sensored=0
violations=0
notes=()

echo "== AD-5: сериализация нагрузок на GB10 =="
echo "стенд: $TARGET ($MODE)"
[ "$STRICT" = "1" ] && echo "режим: --strict (недоступность стенда — красный вердикт)"
echo

# ── сенсор 1: тренировочные нагрузки ─────────────────────────────────────────
ps_out="$(run_remote "ps -eo pid,ppid,etime,args --no-headers" 2>/dev/null)"
if [ -n "$ps_out" ]; then
  sensored=$((sensored + 1))
  loads="$(printf '%s\n' "$ps_out" | grep -E "$LOAD_RE" | grep -vE "$SELF_RE" || true)"
  n_procs="$(printf '%s' "$loads" | grep -c . || true)"
  if [ "$n_procs" -gt 0 ]; then
    report="$(printf '%s\n' "$loads" | awk "$LOAD_ID_AWK")"
    n_loads="$(printf '%s\n' "$report" | sed -n '1p')"
    id_mode="$(printf '%s\n' "$report" | sed -n '2p')"
    echo "тренировочных нагрузок: $n_loads (процессов в их деревьях: $n_procs, опознание: $id_mode)"
    printf '%s\n' "$report" | sed -n '3,$p'
  else
    n_loads=0
    echo "тренировочных нагрузок: 0"
  fi
  if [ "$n_loads" -gt 1 ]; then
    echo "НАРУШЕНИЕ: одновременно $n_loads тренировочных нагрузок (AD-5: одна нагрузка за раз)"
    violations=$((violations + 1))
  fi
else
  notes+=("ps недоступен — сенсор процессов пропущен")
fi

# ── сенсор 2: считающие CUDA-процессы ────────────────────────────────────────
smi="$(run_remote "nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader" 2>/dev/null)"
if [ -n "$smi" ]; then
  sensored=$((sensored + 1))
  n_apps="$(printf '%s' "$smi" | grep -c . || true)"
  echo "CUDA-процессов: $n_apps"
  printf '%s\n' "$smi" | sed 's/^/    /'
  if [ "$n_apps" -gt 1 ]; then
    echo "ВНИМАНИЕ: на GPU больше одного процесса — сверь с сенсором процессов"
    notes+=("CUDA-процессов $n_apps: параллельная нагрузка возможна, но не подтверждена")
  fi
else
  notes+=("nvidia-smi недоступен — сенсор GPU пропущен")
fi

# ── сенсор 3: tmux-панели стадий ─────────────────────────────────────────────
tmux_out="$(run_remote "tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_start_command}'" 2>/dev/null)"
if [ -n "$tmux_out" ]; then
  sensored=$((sensored + 1))
  stage_panes="$(printf '%s\n' "$tmux_out" | grep -E "$LOAD_RE" || true)"
  n_panes="$(printf '%s' "$stage_panes" | grep -c . || true)"
  n_sessions="$(printf '%s\n' "$stage_panes" | awk '{print $1}' | cut -d: -f1 | sort -u | grep -c . || true)"
  echo "tmux-панелей со стадиями: $n_panes (сессий: $n_sessions)"
  if [ "$n_panes" -gt 0 ]; then printf '%s\n' "$stage_panes" | sed 's/^/    /'; fi
  if [ "$n_panes" -gt 1 ] || [ "$n_sessions" -gt 1 ]; then
    echo "НАРУШЕНИЕ: стадии идут в нескольких tmux-панелях/сессиях одновременно (AD-5)"
    violations=$((violations + 1))
  fi
else
  notes+=("tmux недоступен/нет сервера сессий — сенсор tmux пропущен")
fi

echo
if [ ${#notes[@]} -gt 0 ]; then
  for n in "${notes[@]}"; do echo "  · $n"; done
  echo
fi

if [ "$violations" -gt 0 ]; then
  echo "FAIL: нарушений AD-5 — $violations"
  exit 1
fi
if [ "$sensored" -eq 0 ]; then
  echo "NOT-VERIFIED: стенд доступен, но ни один сенсор не дал данных"
  exit 2
fi
echo "GB10 SERIALIZATION OK: одновременных нагрузок не обнаружено (сенсоров: $sensored)"
exit 0
