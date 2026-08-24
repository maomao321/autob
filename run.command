#!/usr/bin/env bash
set -u

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

show_error() {
    local message="$1"
    echo "[AutoQuant] $message" >&2
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display alert \"AutoQuant\" message \"$message\" as critical"
    fi
}

if [[ ! -x ".venv/bin/python" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        show_error "未找到 Python 3。请安装 Python 3.10 或更高版本，然后重新运行此脚本。"
        exit 1
    fi
    echo "[AutoQuant] 正在创建项目虚拟环境..."
    if ! python3 -m venv ".venv"; then
        show_error "创建 .venv 失败，请确认 Python 包含 venv 模块。"
        exit 1
    fi
fi

if ! ".venv/bin/python" -c "import autoquant_frontend, autoquant_shared, PySide6, openpyxl" >/dev/null 2>&1; then
    echo "[AutoQuant] 正在向 .venv 安装源码运行依赖..."
    if ! ".venv/bin/python" -m pip install -e .; then
        show_error "依赖安装失败，请检查网络连接和上方的 pip 错误。"
        exit 1
    fi
fi

exec ".venv/bin/python" -m autoquant_frontend
