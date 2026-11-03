#!/usr/bin/env bash
# task-api 컨테이너 기동 (deployment 스킬). DB(task_db) 와 같은 podman 네트워크(task-net)에 붙는다.
#
# 사용:  TASK_API_IMAGE=task-api:<버전> TASK_API_ENV=/etc/task-spec/env docker/api/run.sh
#   TASK_API_BIND  기본 127.0.0.1:8001 — 내부망 다른 PC 에서 접속하려면 <서버IP>:8001 (인증 전 — 접근 대상 제한 필수)
#                  none = 호스트에 포트를 열지 않음 (nginx 앞단 구성 — docker/nginx/run.sh 가 task-net 으로 접속)
set -euo pipefail

NAME="${TASK_API_NAME:-task-api}"
IMAGE="${TASK_API_IMAGE:?TASK_API_IMAGE=task-api:<버전> 을 지정하세요}"
ENV_FILE="${TASK_API_ENV:-/etc/task-spec/env}"
BIND="${TASK_API_BIND:-127.0.0.1:8001}"
NET="${TASK_NET:-task-net}"

[[ -f "$ENV_FILE" ]] || { echo "env 파일 없음: $ENV_FILE (docker/api/env.example 참고, 권한 600)" >&2; exit 1; }
perm=$(stat -c %a "$ENV_FILE")
[[ "$perm" == 600 || "$perm" == 400 ]] || { echo "env 파일 권한이 $perm — chmod 600 $ENV_FILE" >&2; exit 1; }

podman network exists "$NET" || podman network create "$NET" >/dev/null

if podman container exists "$NAME"; then
  echo "이미 존재: $NAME — 업그레이드는 'podman rm -f $NAME' 후 새 TASK_API_IMAGE 로 다시 실행 (데이터는 볼륨에 남음)" >&2
  exit 1
fi

publish=(-p "${BIND}:8000")
[[ "$BIND" == none ]] && publish=()

podman run -d --name "$NAME" \
  --network "$NET" \
  --env-file "$ENV_FILE" \
  -v task_data:/data \
  "${publish[@]}" \
  --restart unless-stopped \
  --health-cmd "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)\"" \
  --health-interval 30s --health-start-period 60s \
  "$IMAGE"
if [[ "$BIND" == none ]]; then
  echo "기동: $NAME ($IMAGE) — 호스트 포트 없음, nginx(task-net) 경유"
else
  echo "기동: $NAME ($IMAGE) → http://${BIND}/healthz"
fi
