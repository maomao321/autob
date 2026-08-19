#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ENVIRONMENT="$PROJECT_ROOT/.packaging/build-venv-macos"
BUILD_PYTHON="$BUILD_ENVIRONMENT/bin/python"
DIST_PATH="$PROJECT_ROOT/dist/macos"
WORK_PATH="$PROJECT_ROOT/build/macos"

if [[ ! -x "$BUILD_PYTHON" ]]; then
    if [[ -n "${PYTHON_EXE:-}" ]]; then
        BOOTSTRAP_PYTHON="$PYTHON_EXE"
    elif command -v python3 >/dev/null 2>&1; then
        BOOTSTRAP_PYTHON="$(command -v python3)"
    else
        echo "[AutoQuant] 未找到 Python 3.10 或更高版本。" >&2
        echo "请从 https://www.python.org/downloads/macos/ 安装后重试。" >&2
        exit 1
    fi

    echo "创建隔离的 macOS 构建环境..."
    "$BOOTSTRAP_PYTHON" -m venv "$BUILD_ENVIRONMENT"
fi

cd "$PROJECT_ROOT"
"$BUILD_PYTHON" -m pip install --upgrade pip
"$BUILD_PYTHON" -m pip install -e . 'pyinstaller>=6.10,<7'
"$BUILD_PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$DIST_PATH" \
    --workpath "$WORK_PATH" \
    AutoQuant.spec

echo "构建完成: $DIST_PATH/AutoQuant.app"
