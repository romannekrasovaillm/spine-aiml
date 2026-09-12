#!/usr/bin/env bash
# smoke.sh — по одной дешёвой мини-генерации на каждое условие/харнесс.
# Ответы кладутся в /tmp/pvbench_smoke/, ключи не печатаются.
# Код возврата: 0 — все зелёные, 1 — есть сбои.
set -u
OUT=/tmp/pvbench_smoke
WORK=$OUT/work
mkdir -p "$OUT" "$WORK"
PROMPT='Ответь одним словом: готов'
OK=(); FAIL=()

run_smoke() { # $1=имя, $2=таймаут, $3...=команда
  local name=$1 tmo=$2; shift 2
  local dir="$WORK/$name"; mkdir -p "$dir"
  echo "=== $name ==="
  ( cd "$dir" && timeout "$tmo" "$@" >"$OUT/$name.out" 2>"$OUT/$name.err" )
  local rc=$?
  if [ $rc -eq 0 ] && [ -s "$OUT/$name.out" ]; then
    OK+=("$name"); echo "  OK ($(wc -c <"$OUT/$name.out") байт): $(head -c 120 "$OUT/$name.out" | tr '\n' ' ')"
  else
    FAIL+=("$name"); echo "  FAIL rc=$rc"; tail -c 300 "$OUT/$name.err" | sed 's/^/  stderr: /'
  fi
}

raw_smoke() { # $1=имя, $2=url, $3=модель, $4=env-ключ
  local name=$1 url=$2 model=$3 keyenv=$4
  echo "=== $name (raw POST $model) ==="
  if python3 - "$url" "$model" "$keyenv" >"$OUT/$name.out" 2>"$OUT/$name.err" <<'PYEOF'
import json, os, sys, urllib.request
url, model, keyenv = sys.argv[1], sys.argv[2], sys.argv[3]
key = os.environ.get(keyenv)
if not key:
    print(f"нет env {keyenv}", file=sys.stderr); sys.exit(1)
payload = {"model": model, "messages": [{"role": "user", "content": "Ответь одним словом: готов"}],
           "temperature": 0.7, "max_tokens": 2048}
req = urllib.request.Request(url, data=json.dumps(payload).encode(),
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as r:
    body = json.loads(r.read())
msg = body["choices"][0]["message"]
print(msg.get("content") or "", end="")
print(f"\n[echo={body.get('model')}]", file=sys.stderr)
PYEOF
  then
    if [ -s "$OUT/$name.out" ]; then
      OK+=("$name"); echo "  OK: $(head -c 120 "$OUT/$name.out" | tr '\n' ' ')"
    else
      FAIL+=("$name"); echo "  FAIL: пустой content"; tail -c 300 "$OUT/$name.err" | sed 's/^/  stderr: /'
    fi
  else FAIL+=("$name"); echo "  FAIL"; tail -c 300 "$OUT/$name.err" | sed 's/^/  stderr: /'
  fi
}

# --- 6 основных условий (по харнессу/каналу) ---
run_smoke spine-arch-dsf 300 arch-be run -q --model deepseek --timeout 280 "$PROMPT"
run_smoke spine-arch-glm 300 arch-be run -q --model glm-5.3-flash --timeout 280 "$PROMPT"
run_smoke theseus-dsf 300 theseus -m deepseek-flash --yolo -p "$PROMPT"
run_smoke theseus-glm 300 theseus -m glm-5.3-flash --yolo -p "$PROMPT"
run_smoke claude-dsf 300 claude -p "$PROMPT" --model deepseek-flash --output-format text --dangerously-skip-permissions
raw_smoke raw-dsf https://api.deepseek.com/v1/chat/completions deepseek-chat DEEPSEEK_API_KEY
raw_smoke raw-glm http://127.0.0.1:8787/v1/chat/completions glm-5.3-flash ZHIPU_API_KEY

# --- расширенный свип харнессов ---
run_smoke dsh-plain 300 dsh --profile headless "$PROMPT"
run_smoke codewhale-plain 300 codewhale exec "$PROMPT"
run_smoke hermes-plain 300 hermes -z "$PROMPT"
run_smoke openclaw-plain 660 openclaw agent --local -m "$PROMPT" --json --session-key pvbench-smoke
run_smoke kimi-plain 300 "${KIMI_BIN:-kimi}" -p "$PROMPT"

echo
echo "=== ИТОГ ==="
echo "OK   (${#OK[@]}): ${OK[*]:-}"
echo "FAIL (${#FAIL[@]}): ${FAIL[*]:-}"
[ ${#FAIL[@]} -eq 0 ]
