#!/usr/bin/env bash
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

if [[ -x ".venv/bin/python" ]]; then
    exec ".venv/bin/python" -m autoquant
fi

if command -v python3 >/dev/null 2>&1; then
    exec python3 -m autoquant
fi

MESSAGE="未找到 Python 3。请安装 Python 3.10 或更高版本，并运行 python3 -m pip install -e .。"
echo "[AutoQuant] $MESSAGE" >&2
if command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"AutoQuant\" message \"$MESSAGE\" as critical"
fi
exit 1
