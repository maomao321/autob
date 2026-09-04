#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "请使用 root 运行：sudo bash $0" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_SOURCE="${SCRIPT_DIR}/autoquant.service"

if [[ ! -f "${UNIT_SOURCE}" ]]; then
    echo "缺少 systemd 单元文件：${UNIT_SOURCE}" >&2
    exit 1
fi

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
fi
if [[ "${PLATFORM_ID:-}" != "platform:el9" && "${VERSION_ID:-}" != 9* ]]; then
    echo "警告：此脚本面向 CentOS Stream 9，当前系统为 ${PRETTY_NAME:-未知系统}。" >&2
fi

dnf install -y python3.11 python3.11-pip curl

if ! id autoquant >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /home/autoquant --shell /bin/bash autoquant
fi

install -d -m 0755 -o autoquant -g autoquant /opt/autoquant
install -d -m 0755 -o autoquant -g autoquant /opt/autoquant/releases
install -d -m 0700 -o autoquant -g autoquant /home/autoquant/.autoquant
install -d -m 0750 -o root -g autoquant /etc/autoquant

if [[ ! -e /etc/autoquant/autoquant.env ]]; then
    install -m 0640 -o root -g autoquant /dev/null /etc/autoquant/autoquant.env
fi

install -m 0644 -o root -g root "${UNIT_SOURCE}" /etc/systemd/system/autoquant.service

cat >/etc/sudoers.d/autoquant-deploy <<'EOF'
autoquant ALL=(root) NOPASSWD: /usr/bin/systemctl restart autoquant.service
EOF
chmod 0440 /etc/sudoers.d/autoquant-deploy
visudo -cf /etc/sudoers.d/autoquant-deploy

systemctl daemon-reload
systemctl enable autoquant.service

echo "服务器初始化完成。配置 autoquant 用户的 SSH 公钥和 /etc/autoquant/autoquant.env 后即可首次部署。"
