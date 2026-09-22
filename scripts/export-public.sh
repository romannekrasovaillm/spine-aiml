#!/usr/bin/env bash
# export-public.sh — собрать санитизированный снапшот публичной редакции
# Spine AI/ML Edition (github.com/romannekrasovaillm/spine-aiml).
#
# Использование:
#   scripts/export-public.sh <целевой-каталог> [ref]     # ref по умолчанию HEAD
#
# Что делает:
#   1. Экспортирует tracked-файлы ревизии (git archive) во временный каталог.
#   2. Исключает закрытые зоны: banking/, experiments/, LICENSE.banking,
#      ARCHITECTURE-SPINE-BE.md, кейсы/laguna-compact/runs/,
#      кейсы/laguna-compact/.arch-fleet/.
#   3. Санитизирует персональные пути: /home/user → /home/user,
#      /home/user → /home/user, голый "/home/.../" → "/home/.../"
#      (текстовые файлы и цели симлинков).
#   4. CONSTRAINTS.yaml: снимает правила закрытой зоны banking/ (список ниже)
#      вместе с их комментариями и вставляет NB-комментарий — иначе
#      dogfood-гейт публичной редакции красный по несуществующим путям.
#   5. Проверяет результат: ни одного /home/user|/home/user, ни одного
#      исключённого пути.
#   6. rsync --delete в целевой каталог (обычно — чистый клон публичного репо).
#
# Коммит и пуш в публичный репозиторий делает оператор — скрипт только
# собирает дерево.
set -euo pipefail

die() { echo "export-public: $*" >&2; exit 1; }

DEST="${1:-}"
REF="${2:-HEAD}"
[ -n "$DEST" ] || die "нужен целевой каталог: scripts/export-public.sh <dest> [ref]"
mkdir -p "$DEST"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Ревизия резолвится в репозитории-источнике (тот, где лежит этот скрипт),
# а не в cwd вызова — иначе при запуске из клона публичного репо archive
# упадёт («не tar-архив»), т.к. там такого коммита нет.
SRC_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "== git archive $REF (из $SRC_REPO)"
git -C "$SRC_REPO" archive "$REF" | tar -x -C "$TMP"

echo "== исключение закрытых зон"
rm -rf "$TMP/banking" "$TMP/experiments" \
       "$TMP/LICENSE.banking" "$TMP/ARCHITECTURE-SPINE-BE.md" \
       "$TMP/кейсы/laguna-compact/runs" "$TMP/кейсы/laguna-compact/.arch-fleet"

echo "== санитизация персональных путей (текст + симлинки)"
python3 - "$TMP" <<'PY_SANITIZE'
import os, sys

root = sys.argv[1]
REPL = [
    ("/home/user", "/home/user"),
    ("/home/user", "/home/user"),
    # голый "/home/.../" в кавычках (grep-инструкции в планах флота) — в плейсхолдер
    ('"/home/.../"', '"/home/.../"'),
    # unicode-многоточие → ASCII-плейсхолдер (конвенция публичной редакции)
    ("/home/\u2026", "/home/..."),
]
MAX_TEXT = 50 * 1024 * 1024  # бинарные/огромные файлы пропускаем

def sanitize_text(path):
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192] or len(data) > MAX_TEXT:
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    out = text
    for old, new in REPL:
        out = out.replace(old, new)
    if out != text:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out)

def fix_symlink(p):
    target = os.readlink(p)
    new_target = target
    for old, new in REPL:
        new_target = new_target.replace(old, new)
    if new_target != target:
        os.unlink(p)
        os.symlink(new_target, p)

for dirpath, dirnames, filenames in os.walk(root):
    # симлинки на КАТАЛОГИ os.walk кладёт в dirnames — обрабатываем и их,
    # но не спускаемся внутрь (содержимое — вне репозитория)
    for name in list(dirnames):
        p = os.path.join(dirpath, name)
        if os.path.islink(p):
            fix_symlink(p)
            dirnames.remove(name)
    for name in filenames:
        p = os.path.join(dirpath, name)
        if os.path.islink(p):
            fix_symlink(p)
        else:
            sanitize_text(p)
PY_SANITIZE

echo "== CONSTRAINTS.yaml: снятие правил закрытой зоны"
python3 - "$TMP/CONSTRAINTS.yaml" <<'PY_CONSTRAINTS'
import sys

path = sys.argv[1]
DROP_IDS = {
    "BE-01a", "BE-01b", "BE-02c", "BE-03", "BE-04c", "BE-05", "BE-07",
    "BE-08", "BE-09", "BE-10", "BE-11", "BE-12", "BE-13", "BE-15",
    "BE-16a", "BE-16b", "BE-17a", "BE-17b", "BE-18", "BE-20f", "BE-21",
}
NB = """
  # NB: правила закрытой зоны banking/ (и ADF) при публикации ядра сняты —
  # эта зона в публичный снапшот не входит (см. README).
"""

lines = open(path, encoding="utf-8").read().split("\n")

# Модель: преамбула + юниты [комментарии] + ["  - id: X", ...тело].
# Комментарий прикреплён к СЛЕДУЮЩЕМу правилу: снятое правило уносит свои
# комментарии с собой, комментарии сохранённых правил остаются.
out, pending, units = [], [], []
for line in lines:
    if line.startswith("constraints:"):
        out.append(line)
        out.extend(NB.strip("\n").split("\n"))
        continue
    if line.startswith("  - id: "):
        units.append([pending, [line]])
        pending = []
        continue
    if units:
        units[-1][1].append(line)
    else:
        pending.append(line)

# Хвост шапки файла (после constraints: до первого правила)
out.extend(pending)

for comments, body in units:
    rule_id = body[0].split(":", 1)[1].strip()
    if rule_id in DROP_IDS:
        continue
    out.extend(comments)
    out.extend(body)

open(path, "w", encoding="utf-8").write("\n".join(out))
PY_CONSTRAINTS

echo "== проверка результата"
bad=0
if grep -rIl "/home/user\|/home/user" "$TMP" 2>/dev/null | grep -q .; then
  echo "FAIL: остались персональные пути:" >&2
  grep -rIl "/home/user\|/home/user" "$TMP" | head >&2
  bad=1
fi
for p in banking experiments LICENSE.banking ARCHITECTURE-SPINE-BE.md \
         "кейсы/laguna-compact/runs" "кейсы/laguna-compact/.arch-fleet"; do
  [ -e "$TMP/$p" ] && { echo "FAIL: не исключено: $p" >&2; bad=1; }
done
if find "$TMP" -type l \( -lname "*/home/user*" -o -lname "*/home/user*" \) | grep -q .; then
  echo "FAIL: симлинки с персональными путями" >&2
  bad=1
fi
[ "$bad" = 0 ] || die "снапшот не прошёл проверки"

echo "== перенос в $DEST"
# .git целевого клона не трогаем никогда
rsync -a --delete --exclude='/.git' "$TMP"/ "$DEST"/
echo "export-public: готово → $DEST"
echo "Дальше: cd <клон публичного репо> && git add -A && git commit"
