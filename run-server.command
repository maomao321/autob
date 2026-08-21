#!/bin/zsh
set -e
SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -e .
fi

exec .venv/bin/python -m autoquant.server "$@"
