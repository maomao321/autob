#!/usr/bin/env bash
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

if [[ -d "dist/macos/AutoQuant.app" ]]; then
    open "dist/macos/AutoQuant.app"
    exit $?
fi

if [[ -d "dist/AutoQuant.app" ]]; then
    open "dist/AutoQuant.app"
    exit $?
fi

if [[ -x ".venv/bin/python" ]]; then
    exec ".venv/bin/python" -m autoquant
fi

if command -v python3 >/dev/null 2>&1; then
    exec python3 -m autoquant
fi

MESSAGE="未找到 AutoQuant.app 或 Python 3。请先运行 bash packaging/build_macos.sh。"
echo "[AutoQuant] $MESSAGE" >&2
if command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"AutoQuant\" message \"$MESSAGE\" as critical"
fi
exit 1
