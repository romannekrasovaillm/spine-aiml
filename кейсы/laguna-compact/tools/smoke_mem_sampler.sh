#!/usr/bin/env bash
# smoke_mem_sampler.sh — сэмплер unified-памяти стенда GB10 во время стадии смоука.
#
# Зачем снаружи контейнера, а не изнутри: у GB10 память **unified** (121 ГБ на
# CPU+GPU), `nvidia-smi --query-gpu=memory.used` отдаёт N/A, а `docker stats`
# внутри контейнера видит только свой cgroup. Давление на память, из-за которого
# AD-5 и вводит бюджет (≥30 ГБ свободных, ADR-007), видно только с хоста —
# поэтому замер идёт с хоста, рядом со стадией.
#
# Пишет JSONL: одна строка на сэмпл, значения в килобайтах как в /proc/meminfo,
# плюс память контейнеров на момент сэмпла (docker stats — имя=расход).
#
# Ограничение по времени обязательно: сэмплер не должен пережить стадию и
# остаться сиротой, если раннер упал (--duration секунд, по умолчанию 1800).
#
# Запуск: bash smoke_mem_sampler.sh <out.jsonl> [interval_s] [duration_s]

set -uo pipefail

OUT="${1:?укажи выходной файл}"
INT="${2:-5}"
DUR="${3:-1800}"

: > "$OUT"
END=$(( $(date +%s) + DUR ))

while :; do
  #: docker stats берём до /proc/meminfo: сам он памяти почти не ест, но идёт ~1 с,
  #: и лучше пусть сдвиг будет в сторону «память измерена после контейнеров».
  ctr="$(docker stats --no-stream --format '{{.Name}}={{.MemUsage}}' 2>/dev/null \
         | tr '\n' ';' | sed 's/;$//')"
  awk -v ts="$(date +%s)" -v ctr="$ctr" '
    /^MemTotal:/     { t = $2 }
    /^MemAvailable:/ { a = $2 }
    /^MemFree:/      { f = $2 }
    /^Cached:/       { c = $2 }
    END {
      printf "{\"ts\":%s,\"mem_total_kb\":%d,\"mem_available_kb\":%d,\"mem_free_kb\":%d,\"cached_kb\":%d,\"containers\":\"%s\"}\n", ts, t, a, f, c, ctr
    }
  ' /proc/meminfo >> "$OUT"

  [ "$(date +%s)" -ge "$END" ] && break
  sleep "$INT"
done
