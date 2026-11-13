#!/usr/bin/env bash
# task-nginx 컨테이너 기동 — TLS + Basic 인증 리버스 프록시 (deployment 스킬 "nginx").
#
# 사용: TASK_NGINX_CERTS=/etc/task-spec/certs TASK_NGINX_AUTH=/etc/task-spec/htpasswd docker/nginx/run.sh
#   TASK_NGINX_MODE     tls(기본) | http — http 는 인증서 받기 전 임시 단계 (nginx.http.conf, 평문 — 실제 지원서 금지)
#   TASK_NGINX_NETWORK  host(기본) | bridge
#     host   : 호스트 네트워크에서 80·443 을 직접 받는다 → 접속자 실제 IP 로 요청 제한·허용 목록이 동작.
#              API 는 task-api 의 로컬 포트로 (TASK_NGINX_UPSTREAM, 기본 127.0.0.1:8001 — task-api 를 TASK_API_BIND 기본값으로)
#     bridge : task-net 에서 task-api:8000 으로. rootless 포트 전달이 접속자 IP 를 내부 주소로 바꿔 IP 별 제한이 안 됨
#   TASK_NGINX_CERTS    fullchain.pem·privkey.pem 이 있는 디렉토리 (권한 700, privkey 600) — tls 모드만
#   TASK_NGINX_AUTH     htpasswd 파일 (htpasswd -B -C 12) — 필수
#   TASK_NGINX_ACME     (선택) certbot --webroot 경로 (기본 /etc/task-spec/acme) — 80 의 확인 경로로 냄
#   TASK_NGINX_ALLOW    (선택) IP 허용 목록 파일 ('allow 203.0.113.0/24;' 줄들 + 'deny all;')
#   TASK_NGINX_CORS     (선택) CORS 허용 origin 목록 (기본 docker/nginx/.allow_list, gitignore) — 한 줄에 origin 하나
#                       ('http://203.0.113.5:3000', 끝 '/' 없이, '#' 주석). 없으면 CORS 헤더를 내지 않는다. tls 모드만
#   TASK_NGINX_STATE    (선택) run.sh 가 만드는 설정 조각 위치 (기본 /etc/task-spec/nginx) — 재부팅 후 재시작에도 남아야 해서 /tmp 금지
#   TASK_NGINX_HTTPS / TASK_NGINX_HTTP  공개 포트 (기본 443 / 80) — bridge 만. host 는 설정 파일의 80·443
# rootless podman 으로 띄운다. 80·443 은 net.ipv4.ip_unprivileged_port_start=80 필요.
set -euo pipefail
cd "$(dirname "$0")"

NAME="${TASK_NGINX_NAME:-task-nginx}"
IMAGE="${TASK_NGINX_IMAGE:-docker.io/library/nginx:1.27-alpine}"
NET="${TASK_NET:-task-net}"
MODE="${TASK_NGINX_MODE:-tls}"
NETWORK="${TASK_NGINX_NETWORK:-host}"
AUTH="${TASK_NGINX_AUTH:?TASK_NGINX_AUTH (htpasswd) 를 지정하세요 — 앱에 인증이 없어 필수}"
ACME="${TASK_NGINX_ACME:-/etc/task-spec/acme}"
ALLOW="${TASK_NGINX_ALLOW:-}"
CORS="${TASK_NGINX_CORS:-$PWD/.allow_list}"
STATE="${TASK_NGINX_STATE:-/etc/task-spec/nginx}"
HTTPS="${TASK_NGINX_HTTPS:-443}"
HTTP="${TASK_NGINX_HTTP:-80}"

[[ -s "$AUTH" ]] || { echo "없음 또는 빈 파일: $AUTH" >&2; exit 1; }
# htpasswd 는 요청마다 worker(컨테이너 nginx, gid 101)가 읽는다 → 그룹만 컨테이너 nginx 로, 권한 640 (다른 사용자는 못 읽음)
podman unshare chgrp 101 "$AUTH" && chmod 640 "$AUTH"
[[ -d "$ACME" ]] || { echo "ACME webroot 디렉토리가 없습니다: $ACME (install -d -m 755)" >&2; exit 1; }
install -d -m 755 "$STATE"

case "$MODE" in
  tls)
    CERTS="${TASK_NGINX_CERTS:?TASK_NGINX_CERTS (인증서 디렉토리) 를 지정하세요}"
    for f in "$CERTS/fullchain.pem" "$CERTS/privkey.pem"; do
      [[ -s "$f" ]] || { echo "없음 또는 빈 파일: $f" >&2; exit 1; }
    done
    [[ "$(stat -c %a "$CERTS/privkey.pem")" =~ ^[46]00$ ]] || { echo "privkey.pem 권한을 600 으로 (chmod 600)" >&2; exit 1; }
    conf=nginx.conf
    # CORS 허용 origin → nginx map 줄. 형식이 아닌 줄은 거부한다 (설정 파일에 그대로 들어가므로)
    cors_conf="$STATE/cors_origins.conf"; : > "$cors_conf"
    if [[ -f "$CORS" ]]; then
      while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"; line="${line//[[:space:]]/}"
        [[ -z "$line" ]] && continue
        [[ "$line" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] \
          || { echo "$CORS: origin 형식이 아닙니다 (예 http://203.0.113.5:3000, 끝 '/' 없이): $line" >&2; exit 1; }
        printf '"%s" $http_origin;\n' "$line" >> "$cors_conf"
      done < "$CORS"
    fi
    mode_args=(-v "$CERTS:/etc/nginx/certs:ro,Z"
               -v "$cors_conf:/etc/nginx/conf.d/cors_origins.conf:ro,Z"
               -v "$PWD/cors_headers.conf:/etc/nginx/cors_headers.conf:ro,Z")
    ports=(-p "${HTTPS}:443" -p "${HTTP}:80")
    health="wget -q --no-check-certificate -O /dev/null https://127.0.0.1/healthz"
    url="https://<서버 주소>/healthz"
    ;;
  http)
    echo "주의: HTTP 모드 — 비밀번호·업로드가 평문으로 오갑니다. 실제 지원서는 올리지 마세요." >&2
    conf=nginx.http.conf
    mode_args=()
    ports=(-p "${HTTP}:80")
    health="wget -q -O /dev/null http://127.0.0.1/healthz"
    url="http://<서버 주소>/healthz"
    ;;
  *) echo "TASK_NGINX_MODE 는 tls 또는 http: $MODE" >&2; exit 1 ;;
esac

case "$NETWORK" in
  host)
    UPSTREAM="${TASK_NGINX_UPSTREAM:-127.0.0.1:8001}"
    curl -sf -m 5 -o /dev/null "http://$UPSTREAM/healthz" \
      || { echo "API 가 $UPSTREAM 에서 응답하지 않습니다 — task-api 를 로컬 포트로 띄우세요 (TASK_API_BIND 기본값)" >&2; exit 1; }
    net_args=(--network host)
    ;;
  bridge)
    UPSTREAM="${TASK_NGINX_UPSTREAM:-task-api:8000}"
    podman network exists "$NET" || podman network create "$NET" >/dev/null
    net_args=(--network "$NET" "${ports[@]}")
    ;;
  *) echo "TASK_NGINX_NETWORK 는 host 또는 bridge: $NETWORK" >&2; exit 1 ;;
esac

printf 'upstream task_api {\n    server %s;\n    keepalive 16;\n}\n' "$UPSTREAM" > "$STATE/upstream.conf"

# IP 허용 목록이 없으면 빈 파일 (모두 허용 — Basic 인증만)
if [[ -n "$ALLOW" ]]; then
  [[ -s "$ALLOW" ]] || { echo "TASK_NGINX_ALLOW 파일이 비었습니다: $ALLOW" >&2; exit 1; }
  allow_src="$ALLOW"
else
  allow_src="$STATE/allow-empty.conf"; : > "$allow_src"
fi

if podman container exists "$NAME"; then
  echo "이미 존재: $NAME — 설정 변경 후에는 'podman rm -f $NAME' 하고 다시 실행" >&2
  exit 1
fi

podman run -d --name "$NAME" \
  "${net_args[@]}" \
  -v "$PWD/$conf:/etc/nginx/nginx.conf:ro,Z" \
  -v "$STATE/upstream.conf:/etc/nginx/conf.d/upstream.conf:ro,Z" \
  -v "$allow_src:/etc/nginx/conf.d/allow.conf:ro,Z" \
  -v "$AUTH:/etc/nginx/auth/htpasswd:ro,Z" \
  -v "$ACME:/var/www/acme:ro" \
  "${mode_args[@]}" \
  --restart unless-stopped \
  --health-cmd "$health" --health-interval 30s \
  "$IMAGE"
echo "기동: $NAME ($MODE, $NETWORK → $UPSTREAM) → $url"
[[ "$MODE" == tls ]] && echo "CORS 허용 origin: $(grep -c . "$cors_conf" || true)개 ($CORS)"
