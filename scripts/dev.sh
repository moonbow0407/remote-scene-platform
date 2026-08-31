#!/usr/bin/env bash
# 日常联调：只起基础设施容器，在本机跑 API / Dispatcher / Geo Worker。
# Ctrl-C 结束本机进程，容器保持运行。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ALL=0
usage() {
  cat <<'EOF'
用法: ./scripts/dev.sh [--all]

  默认启动 API、Outbox Dispatcher、Celery Geo Worker。
  --all  额外启动 Scheduler、Recovery、Cleanup（监测计划与生命周期联调）。

首次先: cp .env.example .env && uv sync --all-groups
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all) ALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已从 .env.example 复制 .env"
fi

if grep -qE '@(db|rabbitmq|minio|titiler)[:/]' .env; then
  echo "当前 .env 仍使用容器内主机名（db/minio/rabbitmq）。" >&2
  echo "请对照 .env.example 改成 127.0.0.1 映射口；Compose 全栈会在 compose.yaml 里覆盖。" >&2
  exit 1
fi

# 丢掉当前 shell 里可能残留的容器主机名，改用 .env 本机地址
set -a
# shellcheck disable=SC1091
source .env
set +a

port_busy() {
  local port="$1"
  python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
s.settimeout(0.3)
try:
    s.connect(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
else:
    raise SystemExit(0)
finally:
    s.close()
PY
}

if port_busy 8000; then
  echo "127.0.0.1:8000 已被占用。先停掉已有的 uvicorn，或不要并行跑本脚本。" >&2
  exit 1
fi

echo "启动基础设施（db / minio / rabbitmq / titiler）…"
# titiler 没有 healthcheck，不能放进 --wait，否则会一直等
docker compose up -d --wait db minio rabbitmq
docker compose up -d titiler
docker compose up minio-init

mkdir -p "${APP_WORKER_TMP_DIR:-/tmp/remote-scene-worker}"
echo "执行数据库迁移…"
uv run alembic upgrade head

pids=()
cleanup() {
  trap - INT TERM EXIT
  echo
  echo "正在停止本机进程（基础设施容器继续运行）…"
  if ((${#pids[@]})); then
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi
}
trap cleanup INT TERM EXIT

run() {
  local tag="$1"
  shift
  "$@" > >(sed -u "s/^/[${tag}] /") 2> >(sed -u "s/^/[${tag}] /" >&2) &
  pids+=($!)
}

export PYTHONUNBUFFERED=1

run api uv run uvicorn app.api.app:create_app --factory --host 127.0.0.1 --port 8000 --reload
run dispatcher uv run python -m app.dispatcher.main
run worker uv run celery -A app.worker.celery_app:celery worker -Q geo --concurrency "${APP_WORKER_CONCURRENCY:-2}" --loglevel INFO

if [[ "$ALL" -eq 1 ]]; then
  run scheduler uv run python -m app.scheduler.main
  run recovery uv run python -m app.recovery.main
  run cleanup uv run python -m app.cleanup.main
fi

cat <<EOF
本机进程已启动。
  API     http://127.0.0.1:8000
  就绪    curl -s http://127.0.0.1:8000/api/v1/readyz
  MinIO   http://127.0.0.1:9001
Ctrl-C 结束本机进程。
EOF

# 任一子进程退出则收掉其余，避免 API 挂了 Dispatcher 还在空转
wait -n "${pids[@]}"
exit_code=$?
cleanup
exit "$exit_code"
