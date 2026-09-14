#!/bin/sh
# rustc-wrapper.sh — тонкий прозрачный посредник между cargo и sccache.
#
# Зачем: параллельный флот собирает ветки в отдельных ворктри. Хочется делить
# кэш компиляции между ними, но sccache установлен не на каждой машине.
# Прописать `rustc-wrapper = "sccache"` напрямую нельзя: там, где бинаря нет,
# любая сборка начнёт падать ещё до вызова rustc. Эта обёртка включает sccache
# только когда он реально доступен, иначе ведёт себя как отсутствие обёртки.
#
# Протокол cargo (RUSTC_WRAPPER): обёртка вызывается как
#     <wrapper> <путь-к-rustc> <аргументы rustc...>
# поэтому первый аргумент — уже готовый путь к компилятору.
#
# Включение: ЯВНО, переменной окружения RUSTC_WRAPPER (рекомендуется абсолютный
# путь) — например
#     export RUSTC_WRAPPER="$PWD/scripts/rustc-wrapper.sh"
# В репозитории СОЗНАТЕЛЬНО нет .cargo/config.toml с rustc-wrapper: авто-подхват
# из репозитория не работает для фабричных ворктри (они лежат вне дерева), а
# оставленный конфиг создаёт ловушку — .cargo/ git игнорирует, а эта обёртка
# untracked, поэтому `git clean -fd` удаляет обёртку, оставляя конфиг со ссылкой
# на неё, и ЛЮБАЯ команда cargo в репозитории падает. Подробности и настройка для
# исполнителей флота: docs/build_cache.md.
#
# Поведение:
#   * sccache доступен (SCCACHE_PATH или PATH) -> exec sccache "$@";
#   * иначе                                    -> exec "$@" (passthrough в rustc).
#
# Свойства: ничего не пишет на диск; подменяет процесс через exec — коды
# возврата и сигналы проходят без искажений и без лишнего процесса-посредника
# (нулевой оверхед в режиме passthrough). От cwd зависит только резолв
# относительного SCCACHE_PATH (см. ниже); поиск rustc/sccache — через PATH.
#
# SCCACHE_PATH — путь к бинарю sccache (аналог переменной самого sccache).
# Если SCCACHE_PATH не задан, поиск идёт по PATH; при пустом PATH sccache
# просто не найдётся и сборка пойдёт напрямую в rustc.

set -eu

sccache_bin=""

# 1) Явный SCCACHE_PATH имеет приоритет (относительный путь считаем от cwd).
if [ -n "${SCCACHE_PATH:-}" ]; then
    case "$SCCACHE_PATH" in
        /*) candidate="$SCCACHE_PATH" ;;
        ./*) candidate="$PWD/${SCCACHE_PATH#./}" ;;
        *) candidate="$PWD/$SCCACHE_PATH" ;;
    esac
    if [ -x "$candidate" ]; then
        sccache_bin="$candidate"
    fi
fi

# 2) Иначе — sccache из PATH (при пустом PATH не найдётся; это ожидаемо).
if [ -z "$sccache_bin" ] && command -v sccache >/dev/null 2>&1; then
    sccache_bin="$(command -v sccache)"
fi

# 3) sccache есть — отдаём ему эстафету. sccache сам вызовет компилятор,
#    переданный первым аргументом (тот же протокол rustc-wrapper).
if [ -n "$sccache_bin" ]; then
    exec "$sccache_bin" "$@"
fi

# 4) sccache нет — полный passthrough. Пустой вызов (ручной запуск без
#    аргументов) сюда не приходит от cargo, но обработаем и его.
if [ "$#" -eq 0 ]; then
    if [ -n "${RUSTC:-}" ] && [ -x "$RUSTC" ]; then
        exec "$RUSTC"
    fi
    if [ -n "${CARGO_HOME:-}" ] && [ -x "$CARGO_HOME/bin/rustc" ]; then
        exec "$CARGO_HOME/bin/rustc"
    fi
    if [ -n "${HOME:-}" ] && [ -x "$HOME/.cargo/bin/rustc" ]; then
        exec "$HOME/.cargo/bin/rustc"
    fi
    for fallback in /usr/local/bin/rustc /usr/bin/rustc; do
        if [ -x "$fallback" ]; then
            exec "$fallback"
        fi
    done
fi

exec "$@"
