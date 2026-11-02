#!/usr/bin/env bash
# task_db 컨테이너 빌드·기동. 사용: TASK_DB_PASSWORD=<pw> docker/db/run.sh
# 포트: 호스트 5432(로컬 PostgreSQL)·5433(sql_generator pg_log) 회피 → 5434
set -euo pipefail
cd "$(dirname "$0")"

: "${TASK_DB_PASSWORD:?TASK_DB_PASSWORD 를 지정하세요}"
NAME="${TASK_DB_NAME:-task_db}"
PORT="${TASK_DB_PORT:-5434}"
IMAGE="task-db:1"

podman build -q -f Dockerfile -t "$IMAGE" . >/dev/null

if podman container exists "$NAME"; then
  echo "이미 존재: $NAME (podman start $NAME 로 기동, 재생성은 podman rm 후 다시 실행)" >&2
  exit 1
fi

podman run -d --name "$NAME" \
  -e POSTGRES_USER=task \
  -e POSTGRES_PASSWORD="$TASK_DB_PASSWORD" \
  -e POSTGRES_DB=task_db \
  -v task_db_data:/var/lib/postgresql/data \
  -p "127.0.0.1:${PORT}:5432" \
  --restart unless-stopped \
  --health-cmd "pg_isready -U task -d task_db" --health-interval 10s \
  "$IMAGE"
