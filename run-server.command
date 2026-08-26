#!/usr/bin/env bash
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

source "$PROJECT_ROOT/packaging/macos_runtime.sh"
autoquant_prepare_runtime \
    "$PROJECT_ROOT" \
    "import autoquant_backend, autoquant_shared, websocket" || exit 1

exec "$AUTOQUANT_VENV_PYTHON" -m autoquant_backend "$@"
