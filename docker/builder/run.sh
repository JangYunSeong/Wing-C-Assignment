#!/usr/bin/env bash
# task-builder 컨테이너 기동 (#129, docs/deploy.md 2-8). builder 전용 호스트 사용자로 실행한다 — 그 사용자의 rootless podman 저장소·소켓만 쓴다.
#
# 사용:  TASK_BUILDER_IMAGE=task-builder:<버전> docker/builder/run.sh
#   TASK_BUILDER_ENV   기본 docker/builder/.env (이 스크립트 옆, gitignore·권한 600) — docker/builder/env.example 참고
#
# 경로 규칙: 이 컨테이너는 호스트 podman 을 소켓으로 부른다 → `podman run -v`·`-w` 경로가 호스트 기준으로 해석된다.
#   그래서 작업 폴더(BUILD_WORK_DIR)·도구 폴더(BUILD_TOOLS_DIR)·묶음 폴더(IMAGES_DIR)는 호스트와 **같은 절대 경로**로 붙인다.
# 네트워크: DB 는 호스트 루프백(127.0.0.1:5434, task_db 가 공개한 포트), LLM 은 인터넷 → --network host.
set -euo pipefail

NAME="${TASK_BUILDER_NAME:-task-builder}"
IMAGE="${TASK_BUILDER_IMAGE:?TASK_BUILDER_IMAGE=task-builder:<버전> 을 지정하세요}"
ENV_FILE="${TASK_BUILDER_ENV:-$(cd "$(dirname "$0")" && pwd)/.env}"

[[ -f "$ENV_FILE" ]] || { echo "env 파일 없음: $ENV_FILE (docker/builder/env.example 참고, 권한 600)" >&2; exit 1; }
perm=$(stat -c %a "$ENV_FILE")
[[ "$perm" == 600 || "$perm" == 400 ]] || { echo "env 파일 권한이 $perm — chmod 600 $ENV_FILE" >&2; exit 1; }

# podman --env-file 은 따옴표를 벗기지 않는다 — 그대로 읽어 절대 경로만 받는다
val() { sed -nE "s#^$1=(.*)\$#\\1#p" "$ENV_FILE" | tail -1; }
mounts=()
for key in BUILD_WORK_DIR BUILD_TOOLS_DIR IMAGES_DIR; do
  dir=$(val "$key")
  [[ "$dir" == /* ]] || { echo "$key 는 따옴표 없는 호스트 절대 경로여야 합니다: '$dir' ($ENV_FILE)" >&2; exit 1; }
  [[ -d "$dir" && -w "$dir" ]] || { echo "$key 폴더가 없거나 쓸 수 없습니다: $dir — docs/deploy.md 2-8" >&2; exit 1; }
  mounts+=(-v "${dir}:${dir}")
done
# DB 는 builder 전용 역할로 (표 주인 task 가 아님 — 체크포인트·지원자 파생물에 권한 없음)
db_user=$(sed -nE 's#^DATABASE_URL=[a-z+]+://([^:@/]+).*#\1#p' "$ENV_FILE" | tail -1)
[[ "$db_user" != task ]] || { echo "DATABASE_URL 이 표 주인(task) 계정입니다 — builder 전용 역할(task_builder)로 (docs/deploy.md 2-8)" >&2; exit 1; }

[[ "$(podman info --format '{{.Host.Security.Rootless}}')" == true ]] \
  || { echo "rootless podman 이 아닙니다 — builder 전용 사용자로 실행하세요 (root 금지)" >&2; exit 1; }
sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
[[ -S "$sock" ]] || { echo "podman 소켓 없음: $sock — systemctl --user enable --now podman.socket" >&2; exit 1; }

if podman container exists "$NAME"; then
  echo "이미 존재: $NAME — 업그레이드는 'podman stop $NAME && podman rm $NAME' 후 새 TASK_BUILDER_IMAGE 로 다시 실행" >&2
  exit 1
fi

# 정지: SIGTERM 을 받으면 지금 작업 뒤 멈춘다. 그 전에 시간이 다 되면 강제 종료 — lease 가 지나 다음 기동 때 다시 대기열로
podman run -d --name "$NAME" \
  --network host \
  --env-file "$ENV_FILE" \
  -v "${sock}:/run/podman/podman.sock" \
  "${mounts[@]}" \
  --stop-timeout 60 \
  --restart unless-stopped \
  "$IMAGE"
echo "기동: $NAME ($IMAGE) — podman logs -f $NAME"
