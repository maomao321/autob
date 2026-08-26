#!/usr/bin/env bash

# Shared runtime bootstrap for the macOS .command launchers. Finder does not
# reliably load the user's interactive shell startup files, so resolve pyenv
# directly instead of relying on its shims being present in PATH.

autoquant_show_error() {
    local message="$1"
    echo "[AutoQuant] $message" >&2
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display alert \"AutoQuant\" message \"$message\" as critical"
    fi
}

autoquant_resolve_python() {
    if [[ -n "${PYTHON_EXE:-}" ]]; then
        printf '%s\n' "$PYTHON_EXE"
        return
    fi

    local pyenv_executable=""
    local candidate
    for candidate in \
        "$(command -v pyenv 2>/dev/null || true)" \
        "${PYENV_ROOT:-$HOME/.pyenv}/bin/pyenv" \
        "/opt/homebrew/bin/pyenv" \
        "/usr/local/bin/pyenv"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            pyenv_executable="$candidate"
            break
        fi
    done

    if [[ -n "$pyenv_executable" ]]; then
        "$pyenv_executable" which python 2>/dev/null
        return
    fi

    command -v python3 2>/dev/null || true
}

autoquant_python_identity() {
    "$1" -c 'import os, sys; print(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))'
}

autoquant_python_is_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1
}

autoquant_prepare_runtime() {
    local project_root="$1"
    local import_check="$2"
    local bootstrap_python
    local venv_python="$project_root/.venv/bin/python"

    bootstrap_python="$(autoquant_resolve_python)"
    if [[ -z "$bootstrap_python" || ! -x "$bootstrap_python" ]]; then
        autoquant_show_error "未找到可用的 Python。请先用 pyenv 安装并选择 Python 3.10 或更高版本。"
        return 1
    fi
    if ! autoquant_python_is_supported "$bootstrap_python"; then
        local detected_version
        detected_version="$("$bootstrap_python" --version 2>&1)"
        autoquant_show_error "当前 pyenv Python 版本过低（${detected_version}）。请执行 pyenv local <3.10以上版本> 后重试。"
        return 1
    fi

    local rebuild_reason=""
    if [[ ! -x "$venv_python" ]]; then
        rebuild_reason="项目虚拟环境不存在"
    elif ! autoquant_python_is_supported "$venv_python"; then
        rebuild_reason="项目虚拟环境的 Python 版本低于 3.10"
    else
        local bootstrap_identity
        local venv_identity
        bootstrap_identity="$(autoquant_python_identity "$bootstrap_python")"
        venv_identity="$(autoquant_python_identity "$venv_python")"
        if [[ "$bootstrap_identity" != "$venv_identity" ]]; then
            rebuild_reason="项目虚拟环境不是由当前 pyenv Python 创建的"
        fi
    fi

    if [[ -n "$rebuild_reason" ]]; then
        echo "[AutoQuant] ${rebuild_reason}，正在使用 ${bootstrap_python} 重建..."
        if [[ -d "$project_root/.venv" ]]; then
            local backup_path="$project_root/.venv.backup-$(date +%Y%m%d-%H%M%S)"
            if ! mv "$project_root/.venv" "$backup_path"; then
                autoquant_show_error "无法备份旧的 .venv。"
                return 1
            fi
            echo "[AutoQuant] 旧环境已保留在 $backup_path"
        fi
        if ! "$bootstrap_python" -m venv "$project_root/.venv"; then
            autoquant_show_error "创建 .venv 失败，请检查 pyenv Python 是否包含 venv 模块。"
            return 1
        fi
    fi

    if ! "$venv_python" -c "$import_check" >/dev/null 2>&1; then
        echo "[AutoQuant] 正在向 .venv 安装源码运行依赖..."
        if ! "$venv_python" -m pip install --upgrade pip setuptools wheel || \
           ! "$venv_python" -m pip install -e "$project_root"; then
            autoquant_show_error "依赖安装失败，请检查网络连接和上方的 pip 错误。"
            return 1
        fi
    fi

    AUTOQUANT_VENV_PYTHON="$venv_python"
    export AUTOQUANT_VENV_PYTHON
    echo "[AutoQuant] 使用 $("$AUTOQUANT_VENV_PYTHON" --version 2>&1)"
}
