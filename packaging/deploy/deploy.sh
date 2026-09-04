#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${AUTOQUANT_APP_ROOT:-/opt/autoquant}"
SERVICE_NAME="autoquant.service"
HEALTH_URL="${AUTOQUANT_HEALTH_URL:-http://127.0.0.1:8765/health}"
ARCHIVE=""
RELEASE_ID=""
PREVIOUS_RELEASE=""
SWITCHED=0
CREATED_RELEASE=0

usage() {
    echo "用法: $0 --archive <release.tar.gz> --release <git-sha>" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --archive)
            ARCHIVE="${2:-}"
            shift 2
            ;;
        --release)
            RELEASE_ID="${2:-}"
            shift 2
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

if [[ ! "${RELEASE_ID}" =~ ^[0-9a-f]{7,64}$ ]]; then
    echo "非法 release id：${RELEASE_ID}" >&2
    exit 2
fi
if [[ ! -f "${ARCHIVE}" ]]; then
    echo "发布包不存在：${ARCHIVE}" >&2
    exit 2
fi
if [[ "$(id -un)" != "autoquant" ]]; then
    echo "部署脚本必须由 autoquant 用户执行。" >&2
    exit 2
fi

RELEASES_DIR="${APP_ROOT}/releases"
RELEASE_DIR="${RELEASES_DIR}/${RELEASE_ID}"
CURRENT_LINK="${APP_ROOT}/current"
LOCK_FILE="${APP_ROOT}/deploy.lock"

mkdir -p "${RELEASES_DIR}"
exec 9>"${LOCK_FILE}"
flock -n 9 || { echo "已有部署任务正在运行。" >&2; exit 1; }

if [[ -L "${CURRENT_LINK}" ]]; then
    PREVIOUS_RELEASE="$(readlink -f "${CURRENT_LINK}")"
fi

rollback() {
    local exit_code=$?
    trap - ERR
    set +e
    if [[ ${SWITCHED} -eq 1 && -n "${PREVIOUS_RELEASE}" && -d "${PREVIOUS_RELEASE}" ]]; then
        echo "部署失败，回滚到 $(basename "${PREVIOUS_RELEASE}")。" >&2
        ln -sfn "releases/$(basename "${PREVIOUS_RELEASE}")" "${CURRENT_LINK}.rollback"
        mv -Tf "${CURRENT_LINK}.rollback" "${CURRENT_LINK}"
        sudo -n systemctl restart "${SERVICE_NAME}" || true
    fi
    if [[ ${CREATED_RELEASE} -eq 1 && ! -e "${RELEASE_DIR}/.ready" ]]; then
        rm -rf -- "${RELEASE_DIR}"
    fi
    exit "${exit_code}"
}
trap rollback ERR

if [[ -d "${RELEASE_DIR}" && ! -e "${RELEASE_DIR}/.ready" ]]; then
    rm -rf -- "${RELEASE_DIR}"
fi
if [[ ! -d "${RELEASE_DIR}" ]]; then
    mkdir "${RELEASE_DIR}"
    CREATED_RELEASE=1
    tar -xzf "${ARCHIVE}" -C "${RELEASE_DIR}"
    python3.11 -m venv "${RELEASE_DIR}/.venv"
    "${RELEASE_DIR}/.venv/bin/python" -m pip install \
        --disable-pip-version-check "${RELEASE_DIR}"
    "${RELEASE_DIR}/.venv/bin/python" -c \
        "import autoquant_backend, autoquant_shared, websocket"
    touch "${RELEASE_DIR}/.ready"
fi

"${RELEASE_DIR}/.venv/bin/python" -c \
    "import autoquant_backend, autoquant_shared, websocket"

ln -sfn "releases/${RELEASE_ID}" "${CURRENT_LINK}.next"
mv -Tf "${CURRENT_LINK}.next" "${CURRENT_LINK}"
SWITCHED=1

sudo -n systemctl restart "${SERVICE_NAME}"

healthy=0
for _attempt in {1..30}; do
    if curl --fail --silent --show-error --max-time 2 "${HEALTH_URL}" >/dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
if [[ ${healthy} -ne 1 ]]; then
    echo "健康检查失败：${HEALTH_URL}" >&2
    false
fi

trap - ERR
SWITCHED=0
echo "部署成功：${RELEASE_ID}"
