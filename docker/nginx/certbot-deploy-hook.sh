#!/usr/bin/env bash
# certbot deploy hook — 새 인증서를 nginx 인증서 디렉토리로 복사하고 rootless nginx 를 reload (deployment 스킬 "nginx").
#
# certbot 이 root 로 실행한다. 레포 파일을 그대로 쓰지 말고 root 소유로 복사해서 등록한다 (사용자가 고칠 수 있는 파일을 root 가 실행하지 않게):
#   sudo install -o root -g root -m 755 docker/nginx/certbot-deploy-hook.sh /etc/letsencrypt/renewal-hooks/deploy/task-spec.sh
# 설정 (환경 변수, 기본값):
#   TASK_CERTS_DIR=/etc/task-spec/certs   TASK_USER=yunseong   TASK_NGINX_NAME=task-nginx
# certbot 이 넘기는 RENEWED_LINEAGE (= /etc/letsencrypt/live/<cert-name>) 를 쓴다.
set -euo pipefail

CERTS="${TASK_CERTS_DIR:-/etc/task-spec/certs}"
OWNER="${TASK_USER:-yunseong}"
NAME="${TASK_NGINX_NAME:-task-nginx}"
SRC="${RENEWED_LINEAGE:?certbot deploy hook 으로만 실행 (RENEWED_LINEAGE 없음)}"
CERT_NAME="${TASK_CERT_NAME:-task-spec}"

[[ "$(basename "$SRC")" == "$CERT_NAME" ]] || exit 0

install -d -o "$OWNER" -g "$OWNER" -m 700 "$CERTS"
# 심볼릭 링크(live → archive)를 따라 실제 파일을 복사한다 — 컨테이너 안에서는 링크가 깨진다
install -o "$OWNER" -g "$OWNER" -m 644 "$SRC/fullchain.pem" "$CERTS/fullchain.pem"
install -o "$OWNER" -g "$OWNER" -m 600 "$SRC/privkey.pem" "$CERTS/privkey.pem"

# rootless podman 은 그 사용자의 런타임 디렉토리로 접근한다. nginx 가 없거나 HTTP 모드면 reload 만 실패하고 넘어간다
uid="$(id -u "$OWNER")"
if runuser -u "$OWNER" -- env XDG_RUNTIME_DIR="/run/user/$uid" podman exec "$NAME" nginx -s reload; then
  echo "task-spec: 인증서 갱신 → $NAME reload"
else
  echo "task-spec: 인증서 복사함, $NAME reload 실패 (컨테이너 없음?) — TLS 모드로 띄울 때 적용됨" >&2
fi
