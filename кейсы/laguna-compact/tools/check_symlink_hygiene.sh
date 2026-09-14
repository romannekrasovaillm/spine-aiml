#!/usr/bin/env bash
# C-011 — поведенческий страж AD-4 «данные и веса живут только на сетевом диске —
# в контуре симлинки».
#
# Проверяет три вещи в дереве кейса:
#   (а) файлы данных > LIMIT МБ — только симлинки, и ведут они в $SHARED
#       (/home/user/gb10-shared); настоящий файл такого размера — копия;
#   (б) любой симлинк дерева разрешается внутрь $SHARED: симлинк наружу или
#       в локальный каталог — тот же класс дефекта (дрейф версий данных);
#   (в) в индексе git (`git ls-files`) нет блобов > LIMIT МБ — инцидент
#       f7deadc: копии sft_train_v12.jsonl (486 МБ ×3) попали в индекс.
#
# Битые симлинки — нарушение: молчаливо исчезнувший вход дороже красного гейта.
#
# Коды возврата:
#   0 — нарушений нет (печатается список симлинков с размерами)
#   1 — нарушение: поимённый список в stdout
#   2 — NOT-VERIFIED: корень не существует / git недоступен там, где нужен
#
# Запуск: bash tools/check_symlink_hygiene.sh [ROOT] [--shared DIR] [--limit-mb N]

set -uo pipefail

ROOT="."
SHARED="/home/user/gb10-shared"
LIMIT_MB=50

while [ $# -gt 0 ]; do
  case "$1" in
    --shared)    SHARED="${2:?--shared требует каталог}"; shift 2 ;;
    --limit-mb)  LIMIT_MB="${2:?--limit-mb требует число}"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) echo "неизвестный флаг: $1" >&2; exit 2 ;;
    *)  ROOT="$1"; shift ;;
  esac
done

if [ ! -d "$ROOT" ]; then
  echo "NOT-VERIFIED: корень не найден: $ROOT" >&2
  exit 2
fi

# Канонический $SHARED — сравнение по реальному пути, не по строке.
SHARED_REAL="$(readlink -f "$SHARED")"
if [ -z "$SHARED_REAL" ] || [ ! -d "$SHARED_REAL" ]; then
  echo "NOT-VERIFIED: сетевой диск недоступен: $SHARED" >&2
  exit 2
fi

violations=0
symlinks=()

echo "== C-011 / AD-4: гигиена данных и весов =="
echo "root:   $ROOT"
echo "shared: $SHARED_REAL"
echo "порог:  > ${LIMIT_MB} МБ"
echo

# ── (а) настоящие файлы > порога = копии ─────────────────────────────────────
while IFS= read -r -d '' f; do
  size_mb=$(( $(stat -c %s "$f") / 1024 / 1024 ))
  echo "НАРУШЕНИЕ (копия данных в дереве): $f — ${size_mb} МБ (> ${LIMIT_MB} МБ)"
  echo "  перенеси данные в $SHARED_REAL и оставь симлинк"
  violations=$((violations + 1))
done < <(find "$ROOT" -name .git -prune -o -type f -size "+${LIMIT_MB}M" -print0 2>/dev/null)

# ── (б) симлинки: цель обязана лежать в $SHARED ──────────────────────────────
while IFS= read -r -d '' l; do
  target="$(readlink -f "$l" 2>/dev/null)"
  if [ -z "$target" ] || [ ! -e "$target" ]; then
    echo "НАРУШЕНИЕ (битый симлинк): $l -> $(readlink "$l")"
    violations=$((violations + 1))
    continue
  fi
  case "$target" in
    "$SHARED_REAL"/*) ;;
    *)
      echo "НАРУШЕНИЕ (симлинк вне сетевого диска): $l -> $target"
      violations=$((violations + 1))
      continue ;;
  esac
  if [ -d "$target" ]; then
    size="каталог"
  else
    size="$(numfmt --to=iec --suffix=B "$(stat -c %s "$target")" 2>/dev/null || stat -c %s "$target")"
  fi
  symlinks+=("$(printf '  %-44s -> %s (%s)' "$l" "$target" "$size")")
done < <(find "$ROOT" -name .git -prune -o -type l -print0 2>/dev/null)

# ── (в) индекс git: блобы > порога ───────────────────────────────────────────
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  # mode 120000 — симлинк: в индексе лежит строка цели, а не данные (проверены выше).
  shas="$(git -C "$ROOT" ls-files -s | awk '$1 != "120000" {print $2}' | sort -u)"
  if [ -n "$shas" ]; then
    while read -r sha size; do
      [ -z "${sha:-}" ] && continue
      if [ "${size:-0}" -gt $(( LIMIT_MB * 1024 * 1024 )) ]; then
        paths="$(git -C "$ROOT" ls-files -s | awk -v s="$sha" '$2 == s {print "      " $4}')"
        echo "НАРУШЕНИЕ (блоб в git-индексе): $sha — $(( size / 1024 / 1024 )) МБ"
        printf '%s\n' "$paths"
        violations=$((violations + 1))
      fi
    done < <(printf '%s\n' "$shas" | git -C "$ROOT" cat-file --batch-check='%(objectname) %(objectsize)')
  fi
else
  echo "ПРЕДУПРЕЖДЕНИЕ: $ROOT вне git-репозитория — проверка индекса (в) пропущена"
fi

echo
if [ ${#symlinks[@]} -gt 0 ]; then
  echo "СИМЛИНКИ (${#symlinks[@]}):"
  printf '%s\n' "${symlinks[@]}"
else
  echo "СИМЛИНКОВ НЕТ"
fi
echo
if [ "$violations" -gt 0 ]; then
  echo "FAIL: нарушений AD-4 — $violations"
  exit 1
fi
echo "SYMLINK HYGIENE OK: копий данных в дереве кейса нет"
exit 0
