#!/usr/bin/env bash
# task-nginx 컨테이너 기동 — TLS + Basic 인증 리버스 프록시 (deployment 스킬 "nginx").
#
# 사용: TASK_NGINX_CERTS=/etc/task-spec/certs TASK_NGINX_AUTH=/etc/task-spec/htpasswd docker/nginx/run.sh
#   TASK_NGINX_CERTS  fullchain.pem·privkey.pem 이 있는 디렉토리 (권한 700, privkey 600)
#   TASK_NGINX_AUTH   htpasswd 파일 (htpasswd -B -c <파일> <계정>) — 필수
#   TASK_NGINX_ALLOW  (선택) IP 허용 목록 파일 ('allow 203.0.113.0/24;' 줄들 + 'deny all;')
#   TASK_NGINX_HTTPS / TASK_NGINX_HTTP  공개 포트 (기본 443 / 80)
set -euo pipefail
cd "$(dirname "$0")"

NAME="${TASK_NGINX_NAME:-task-nginx}"
IMAGE="${TASK_NGINX_IMAGE:-docker.io/library/nginx:1.27-alpine}"
NET="${TASK_NET:-task-net}"
CERTS="${TASK_NGINX_CERTS:?TASK_NGINX_CERTS (인증서 디렉토리) 를 지정하세요}"
AUTH="${TASK_NGINX_AUTH:?TASK_NGINX_AUTH (htpasswd) 를 지정하세요 — 앱에 인증이 없어 필수}"
ALLOW="${TASK_NGINX_ALLOW:-}"
HTTPS="${TASK_NGINX_HTTPS:-443}"
HTTP="${TASK_NGINX_HTTP:-80}"

for f in "$CERTS/fullchain.pem" "$CERTS/privkey.pem" "$AUTH"; do
  [[ -s "$f" ]] || { echo "없음 또는 빈 파일: $f" >&2; exit 1; }
done
[[ "$(stat -c %a "$CERTS/privkey.pem")" =~ ^[46]00$ ]] || { echo "privkey.pem 권한을 600 으로 (chmod 600)" >&2; exit 1; }

# IP 허용 목록이 없으면 빈 파일 (모두 허용 — Basic 인증만)
allow_mount=()
if [[ -n "$ALLOW" ]]; then
  [[ -s "$ALLOW" ]] || { echo "TASK_NGINX_ALLOW 파일이 비었습니다: $ALLOW" >&2; exit 1; }
  allow_mount=(-v "$ALLOW:/etc/nginx/conf.d/allow.conf:ro,Z")
else
  empty="$(mktemp)"; allow_mount=(-v "$empty:/etc/nginx/conf.d/allow.conf:ro,Z")
fi

podman network exists "$NET" || podman network create "$NET" >/dev/null
if podman container exists "$NAME"; then
  echo "이미 존재: $NAME — 설정 변경 후에는 'podman rm -f $NAME' 하고 다시 실행" >&2
  exit 1
fi

podman run -d --name "$NAME" \
  --network "$NET" \
  -v "$PWD/nginx.conf:/etc/nginx/nginx.conf:ro,Z" \
  -v "$CERTS:/etc/nginx/certs:ro,Z" \
  -v "$AUTH:/etc/nginx/auth/htpasswd:ro,Z" \
  "${allow_mount[@]}" \
  -p "${HTTPS}:443" -p "${HTTP}:80" \
  --restart unless-stopped \
  --health-cmd "wget -q --no-check-certificate -O /dev/null https://127.0.0.1/healthz" --health-interval 30s \
  "$IMAGE"
echo "기동: $NAME → https://<서버 주소>:${HTTPS}/healthz"
