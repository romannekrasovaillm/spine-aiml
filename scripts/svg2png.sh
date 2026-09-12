#!/usr/bin/env bash
# svg2png.sh — растеризация SVG-скриншотов TUI в PNG для README/кейсов.
#
# SVG — источник истины (генерируется из кода: `ARCH_GEN_SHOTS=1 cargo test gen_`);
# PNG — производные для рендеринга в GitHub/документах. Конвертер — headless
# Chrome (на стендах нет rsvg-convert): обёртка-HTML с точным размером кадра,
# без лёттербокса и полей. Цвета и сетка ячеек сохраняются 1:1.
#
# Использование:
#   scripts/svg2png.sh <файл.svg | каталог> [...]
# Примеры:
#   scripts/svg2png.sh docs/screenshots                 # все SVG каталога
#   scripts/svg2png.sh docs/screenshots/01-splash.svg   # один файл
#
# Требования: google-chrome (chromium) в PATH.
set -euo pipefail

die() { echo "svg2png: $*" >&2; exit 1; }

CHROME="$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)"
[ -n "$CHROME" ] || die "не найден google-chrome/chromium в PATH"

# rasterize <svg> <png>
rasterize() {
    local svg="$1" png="$2"
    local w h html
    w="$(grep -o 'width="[0-9]*"' "$svg" | head -1 | grep -o '[0-9]*')" || true
    h="$(grep -o 'height="[0-9]*"' "$svg" | head -1 | grep -o '[0-9]*')" || true
    [ -n "${w:-}" ] && [ -n "${h:-}" ] || die "не прочитаны width/height: $svg"
    html="$(mktemp --suffix=.html)"
    cat > "$html" <<HTML
<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;padding:0;background:#1a1b26}img{display:block;width:${w}px;height:${h}px}</style></head>
<body><img src="file://$(realpath "$svg")"></body></html>
HTML
    "$CHROME" --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
        --screenshot="$png" --window-size="$w,$h" "file://$html" >/dev/null 2>&1
    rm -f "$html"
    [ -s "$png" ] || die "пустой результат: $png"
    echo "svg2png: $svg → $png (${w}×${h})"
}

collect() {
    local target="$1"
    if [ -d "$target" ]; then
        find "$target" -maxdepth 1 -name '*.svg' -type f | sort
    elif [ -f "$target" ] && [[ "$target" == *.svg ]]; then
        echo "$target"
    else
        die "не svg-файл и не каталог: $target"
    fi
}

[ $# -ge 1 ] || die "нужен хотя бы один аргумент: <файл.svg | каталог>"

n=0
for target in "$@"; do
    while IFS= read -r svg; do
        rasterize "$svg" "${svg%.svg}.png"
        n=$((n + 1))
    done < <(collect "$target")
done
echo "svg2png: готово, файлов: $n"
