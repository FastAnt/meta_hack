#!/usr/bin/env bash
# Agent-Aware Terminal — native launch (GTK + VTE, no web anything).
#   export MODEL_API_KEY="LLM|..."   # Meta Model API key (optional)
#   ./run.sh                         # a normal terminal window opens
set -euo pipefail
cd "$(dirname "$0")"
exec python3 desktop/native_terminal.py "$@"
