#!/usr/bin/env bash
# 给 WSL 中的 Docker Engine 配置本机代理（7897）与 Hub 镜像源。
# 需要 sudo。官方 Hub 直连超时、代理可用时使用。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROXY_SRC="$ROOT/docker/http-proxy.conf"
MIRROR_SRC="$ROOT/docker/daemon.mirrors.json"
DROP_IN_DIR="/etc/systemd/system/docker.service.d"
DAEMON_JSON="/etc/docker/daemon.json"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请用 sudo 运行：$0" >&2
  exit 1
fi

install -d -m 0755 "$DROP_IN_DIR"
install -d -m 0755 /etc/docker
install -m 0644 "$PROXY_SRC" "$DROP_IN_DIR/http-proxy.conf"
install -m 0644 "$MIRROR_SRC" "$DAEMON_JSON"

systemctl daemon-reload
systemctl restart docker
systemctl show docker --property=Environment --no-pager
docker info -f '{{json .RegistryConfig.Mirrors}}'
echo "已写入代理 http://127.0.0.1:7897 与 registry-mirrors，docker 已重启。"
