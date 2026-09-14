#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Адаптеры: сухой прогон headless-контракта.
#
# Зачем. `control check` и argparse-ошибки ловят только грубую поломку флага
# (код 2). Они НЕ ловят главный дефект класса «объявлено ≠ работает»: адаптер
# настроен так, что запускается ИНТЕРАКТИВНЫЙ режим/TUI — процесс не падает,
# а висит до таймаута, и прогон молча стоит часов. Этот скрипт проверяет
# ровно это: каждый адаптер получает пустую задачу и обязан САМ завершиться
# в неинтерактивном режиме.
#
# Объявленный контракт (цитаты справок CLI) зафиксирован в
# docs/harness_integrations.md; здесь он проверяется прогоном.
#
# ВНИМАНИЕ: прогон ТРАТИТ квоты/токены провайдеров и требует залогиненных CLI.
# Поэтому он НЕ включён в CI и запускается руками.
#
# Примеры:
#   scripts/adapters-headless-check.sh --list            # только показать команды
#   scripts/adapters-headless-check.sh                   # прогнать все адаптеры
#   scripts/adapters-headless-check.sh --only codewhale --timeout 90
#   scripts/adapters-headless-check.sh --config ~/.config/arch-harness/config.toml
# -----------------------------------------------------------------------------
set -u

CONFIG="${HOME}/.config/arch-ml/config.toml"
ONLY=""
TIMEOUT=120
OUT=""
KEEP=0
LIST_ONLY=0
TASK="Ответь одним словом: OK"

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --config)  CONFIG="${2:-}"; shift 2 ;;
    --only)    ONLY="${2:-}"; shift 2 ;;
    --timeout) TIMEOUT="${2:-}"; shift 2 ;;
    --out)     OUT="${2:-}"; shift 2 ;;
    --keep)    KEEP=1; shift ;;
    --list)    LIST_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -r "$CONFIG" ] || { echo "конфиг не читается: $CONFIG" >&2; exit 2; }
case "$TIMEOUT" in ''|*[!0-9]*) echo "--timeout: нужно целое число секунд" >&2; exit 2 ;; esac

# Адаптеры берём ИЗ КОНФИГА (список не хардкодим): name<TAB>binary<TAB>mode<TAB>args-json
ADAPTERS=$(python3 - "$CONFIG" <<'PY'
import json, sys, tomllib
with open(sys.argv[1], 'rb') as fh:
    cfg = tomllib.load(fh)
for name, h in sorted(cfg.get('harnesses', {}).items()):
    print('\t'.join([
        name,
        str(h.get('binary', '')),
        str(h.get('prompt_mode', 'flag')),
        json.dumps(h.get('args', []), ensure_ascii=False),
    ]))
PY
) || { echo "не удалось разобрать конфиг: $CONFIG" >&2; exit 2; }

if [ -z "$ADAPTERS" ]; then
  echo "в конфиге нет секций [harnesses.*]: $CONFIG" >&2
  exit 2
fi

# Разбор args в argv для конкретного режима подачи задачи.
build_argv() { # $1 = args-json, $2 = mode
  python3 - "$1" "$2" "$TASK" <<'PY'
import json, sys
args, mode, task = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
if mode == 'flag':
    out, replaced = [], False
    for a in args:
        if '{prompt}' in a:
            out.append(a.replace('{prompt}', task)); replaced = True
        else:
            out.append(a)
    if not replaced:                      # плейсхолдера нет — задача в конец
        out.append(task)
else:                                      # stdin / positional
    out = list(args)
for a in out:
    print(a)
PY
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/adapters-headless-check.XXXXXX") || exit 2
cleanup() { [ "$KEEP" -eq 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT
[ -n "$OUT" ] || OUT="$WORK/report"
mkdir -p "$OUT"

pass=0; fail=0; skip=0

printf 'Конфиг: %s\nЗадача: %s\nТаймаут: %ss   Логи: %s\n\n' "$CONFIG" "$TASK" "$TIMEOUT" "$OUT"
printf '%-14s %-5s %-7s %-9s %-6s %s\n' "АДАПТЕР" "RC" "ВРЕМЯ" "STDOUT" "ВЕРДИКТ" "ДИАГНОЗ"
printf '%s\n' "------------------------------------------------------------------------------"

while IFS=$'\t' read -r name binary mode args_json; do
  [ -n "$name" ] || continue
  [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue

  if ! command -v "$binary" >/dev/null 2>&1; then
    printf '%-14s %-5s %-7s %-9s %-6s %s\n' "$name" "-" "-" "-" "SKIP" "бинарник '$binary' не в PATH"
    skip=$((skip + 1)); continue
  fi

  mapfile -t ARGV < <(build_argv "$args_json" "$mode")
  dir="$WORK/$name"; mkdir -p "$dir"

  if [ "$LIST_ONLY" -eq 1 ]; then
    if [ "$mode" = "stdin" ]; then
      printf '%-14s stdin ← задача | %s %s\n' "$name" "$binary" "${ARGV[*]:-}"
    else
      printf '%-14s %s %s </dev/null\n' "$name" "$binary" "${ARGV[*]:-}"
    fi
    continue
  fi

  start=$(date +%s)
  if [ "$mode" = "stdin" ]; then
    ( cd "$dir" && printf '%s' "$TASK" | timeout "$TIMEOUT" "$binary" "${ARGV[@]}" ) >"$dir/out.txt" 2>"$dir/err.txt"
  else
    ( cd "$dir" && timeout "$TIMEOUT" "$binary" "${ARGV[@]}" </dev/null ) >"$dir/out.txt" 2>"$dir/err.txt"
  fi
  rc=$?
  dur=$(( $(date +%s) - start ))
  bytes=$(wc -c <"$dir/out.txt" | tr -d ' ')

  verdict="FAIL"; diag="rc=$rc"
  if [ "$rc" -eq 0 ] && [ "$bytes" -gt 0 ]; then
    verdict="PASS"; diag="ответ получен"
  elif [ "$rc" -eq 124 ]; then
    diag="таймаут ${TIMEOUT}s — вероятно интерактив/TUI (ждёт ввода) или нет ответа"
  elif [ "$rc" -eq 0 ]; then
    diag="rc=0, но stdout пуст — проверьте контракт вывода"
  else
    diag=$(tr '\n' ' ' <"$dir/err.txt" | cut -c1-90)
    [ -n "$diag" ] || diag="rc=$rc, stderr пуст"
  fi

  [ "$verdict" = "PASS" ] && pass=$((pass + 1)) || fail=$((fail + 1))
  printf '%-14s %-5s %-7s %-9s %-6s %s\n' "$name" "$rc" "${dur}s" "$bytes" "$verdict" "$diag"
  cp -f "$dir/out.txt" "$OUT/$name.out" 2>/dev/null || true
  cp -f "$dir/err.txt" "$OUT/$name.err" 2>/dev/null || true
done < <(printf '%s\n' "$ADAPTERS")

if [ "$LIST_ONLY" -eq 1 ]; then
  printf '\n(--list: ничего не запускалось)\n'
  exit 0
fi

printf '\nИтог: PASS %s, FAIL %s, SKIP %s\n' "$pass" "$fail" "$skip"
printf 'Полные stdout/stderr: %s\n' "$OUT"
if [ "$fail" -gt 0 ]; then
  printf '\nFAIL — это и есть дефект «объявлено ≠ работает»: адаптер не завершился\n'
  printf 'в неинтерактивном режиме. Сверьте его args со справкой CLI и с таблицей\n'
  printf 'объявленных контрактов в docs/harness_integrations.md.\n'
  exit 1
fi
exit 0
