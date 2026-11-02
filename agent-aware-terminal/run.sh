#!/usr/bin/env bash
# Agent-Aware Terminal — native launch (GTK + VTE, no web anything).
#   export MODEL_API_KEY="LLM|..."   # Meta Model API key (optional)
#   ./run.sh                         # a normal terminal window opens
set -euo pipefail
cd "$(dirname "$0")"
# Load API keys from keys.sh (git-ignored). Real environment
# variables win, matching load_keys_file() in the app. Values
# are never printed — only the variable names.
KEYS_FILE="${AAT_KEYS_FILE:-./keys.sh}"
if [ -f "$KEYS_FILE" ]; then
  _names="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Z0-9_]+)=.*/\2/p' "$KEYS_FILE")"
  declare -A _pre=()
  while IFS= read -r _k; do
    [ -n "$_k" ] && [ -n "${!_k+set}" ] && _pre["$_k"]="${!_k}"
  done <<< "$_names"
  set -a
  # shellcheck disable=SC1090
  . "$KEYS_FILE" || echo "run.sh: warning: could not source $KEYS_FILE" >&2
  set +a
  for _k in "${!_pre[@]}"; do export "$_k=${_pre[$_k]}"; done
  # shellcheck disable=SC2086
  echo "run.sh: keys loaded from $KEYS_FILE ($(echo $_names | tr '\n' ' '))" >&2
  unset _names _pre _k
fi
# gi (PyGObject) is a system package built for the system Python
# (e.g. 3.12 on Ubuntu 24.04). A conda/micromamba python3 first in
# PATH cannot import it, so pick an interpreter that actually can.
pick_python() {
  local cand
  for cand in ".venv/bin/python" "/usr/bin/python3" "python3"; do
    if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c "import gi" >/dev/null 2>&1; then
        printf '%s\n' "$cand"
        return 0
      fi
    fi
  done
  return 1
}
if ! PY="$(pick_python)"; then
  echo "run.sh: no Python with 'gi' (PyGObject) found." >&2
  echo "Install: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91" >&2
  echo "Or create the project env: /usr/bin/python3 -m venv --system-site-packages .venv" >&2
  exit 1
fi
exec "$PY" desktop/native_terminal.py "$@"
