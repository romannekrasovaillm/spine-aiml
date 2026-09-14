#!/usr/bin/env bash
# Тонкая обёртка для вызова из харнесса / CI / fitness-правил.
#   hypothesis-hook.sh intent "<текст>" [project_dir]
#   hypothesis-hook.sh pre-handoff <project_dir> [handoff_dir]
#   hypothesis-hook.sh post-accept <handoff_dir> <project_name>
#   hypothesis-hook.sh refresh
#   hypothesis-hook.sh check <handoff_dir>
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
ev="${1:-}"; shift || true
case "$ev" in
  intent)      exec "$PY" "$HERE/hypothesis_hook.py" intent --text "${1:?текст намерения}" ${2:+--project "$2"} ;;
  pre-handoff) exec "$PY" "$HERE/hypothesis_hook.py" pre-handoff --project "${1:?project_dir}" ${2:+--handoff "$2"} ${HYPOTHESIS_ANNOTATE:+--annotate} ;;
  post-accept) exec "$PY" "$HERE/hypothesis_hook.py" post-accept --handoff "${1:?handoff_dir}" --project-name "${2:?project_name}" ;;
  refresh)     exec "$PY" "$HERE/hypothesis_hook.py" refresh ;;
  check)       exec "$PY" "$HERE/hypothesis_hook.py" check --handoff "${1:?handoff_dir}" ;;
  *) echo "usage: $0 {intent|pre-handoff|post-accept|refresh|check} ..." >&2; exit 2 ;;
esac
