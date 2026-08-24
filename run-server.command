#!/bin/zsh
set -e
SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

if ! .venv/bin/python -c "import autoquant_backend, autoquant_shared, websocket" >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e .
fi

exec .venv/bin/python -m autoquant_backend "$@"
