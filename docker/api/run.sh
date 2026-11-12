#!/usr/bin/env bash
# task-api 컨테이너 기동 (deployment 스킬). DB(task_db) 와 같은 podman 네트워크(task-net)에 붙는다.
#
# 사용:  TASK_API_IMAGE=task-api:<버전> docker/api/run.sh
#   TASK_API_ENV   기본 docker/api/.env (이 스크립트 옆, gitignore·권한 600) — 다른 파일을 쓸 때만 지정
#   TASK_API_BIND  기본 127.0.0.1:8001 — 내부망 다른 PC 에서 접속하려면 <서버IP>:8001 (인증 전 — 접근 대상 제한 필수)
#                  none = 호스트에 포트를 열지 않음 (nginx 앞단 구성 — docker/nginx/run.sh 가 task-net 으로 접속)
#
# 과제 전달 묶음 (#115 2-a): env 파일에 IMAGES_DIR=<호스트 절대 경로> 가 있으면 그 폴더를 **같은 경로로 읽기 전용** 마운트한다.
#   묶음은 호스트 사용자(적재·builder)가 소유자 전용(0600/0700)으로 쓴다 → rootless 에서는 --userns=keep-id 로 컨테이너의 app(10001)을
#   호스트 사용자로 맞춰 읽는다. 이때 task_data 볼륨의 주인도 호스트 사용자여야 한다 — 아니면 기동을 거부한다 (docs/deploy.md 2-7).
set -euo pipefail

NAME="${TASK_API_NAME:-task-api}"
IMAGE="${TASK_API_IMAGE:?TASK_API_IMAGE=task-api:<버전> 을 지정하세요}"
ENV_FILE="${TASK_API_ENV:-$(cd "$(dirname "$0")" && pwd)/.env}"
BIND="${TASK_API_BIND:-127.0.0.1:8001}"
NET="${TASK_NET:-task-net}"

[[ -f "$ENV_FILE" ]] || { echo "env 파일 없음: $ENV_FILE (docker/api/env.example 참고, 권한 600)" >&2; exit 1; }
perm=$(stat -c %a "$ENV_FILE")
[[ "$perm" == 600 || "$perm" == 400 ]] || { echo "env 파일 권한이 $perm — chmod 600 $ENV_FILE" >&2; exit 1; }
# 컨테이너 안의 localhost 는 컨테이너 자신 — DB 는 task-net 의 task_db:5432 (호스트 포트 5434 아님). 값은 출력하지 않는다
db_host=$(sed -nE 's#^DATABASE_URL=[^@]*@([^:/?]+).*#\1#p' "$ENV_FILE" | tail -1)
[[ "$db_host" != localhost && "$db_host" != 127.0.0.1 ]] \
  || { echo "DATABASE_URL 호스트가 $db_host — 컨테이너에서는 task_db:5432 로 적으세요 ($ENV_FILE)" >&2; exit 1; }

# 과제 전달 묶음 폴더 (선택). 값은 경로라 출력해도 된다 (비밀 아님)
# podman --env-file 은 따옴표를 벗기지 않는다 — 컨테이너가 받는 값과 같게 그대로 읽고, 절대 경로가 아니면 거부
images_dir=$(sed -nE 's#^IMAGES_DIR=(.*)$#\1#p' "$ENV_FILE" | tail -1)
images=()
if [[ -n "$images_dir" ]]; then
  [[ "$images_dir" == /* ]] || { echo "IMAGES_DIR 는 따옴표 없는 호스트 절대 경로여야 합니다: $images_dir ($ENV_FILE)" >&2; exit 1; }
  [[ -d "$images_dir" ]] || { echo "IMAGES_DIR 폴더 없음: $images_dir — docs/deploy.md 2-7 로 먼저 만드세요" >&2; exit 1; }
  images=(-v "${images_dir}:${images_dir}:ro")
fi

# SFTP 전달 (#116, 선택): SFTP_DIR(키·known_hosts 폴더)를 같은 경로로 읽기 전용 마운트.
# SFTP_TARGETS__<이름>__KEY_PATH·KNOWN_HOSTS 는 전부 그 아래여야 한다 (값은 경로라 출력해도 된다 — 호스트·계정은 출력하지 않음)
sftp_dir=$(sed -nE 's#^SFTP_DIR=(.*)$#\1#p' "$ENV_FILE" | tail -1)
sftp=()
sftp_paths=$(sed -nE 's#^SFTP_TARGETS__[A-Za-z0-9_]+__(KEY_PATH|KNOWN_HOSTS)=(.*)$#\2#p' "$ENV_FILE")
if [[ -n "$sftp_dir" ]]; then
  [[ "$sftp_dir" == /* && -d "$sftp_dir" ]] || { echo "SFTP_DIR 폴더 없음(호스트 절대 경로): $sftp_dir — docs/deploy.md 2-11" >&2; exit 1; }
  sperm=$(stat -c %a "$sftp_dir")
  [[ "$sperm" == 700 || "$sperm" == 500 ]] || { echo "SFTP_DIR 권한이 $sperm — chmod 700 $sftp_dir" >&2; exit 1; }
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    [[ "$p" == "$sftp_dir"/* && -f "$p" ]] || { echo "SFTP 키·known_hosts 가 SFTP_DIR 아래 파일이 아닙니다: $p" >&2; exit 1; }
  done <<< "$sftp_paths"
  sftp=(-v "${sftp_dir}:${sftp_dir}:ro")
elif [[ -n "$sftp_paths" ]]; then
  echo "SFTP_TARGETS__* 가 있는데 SFTP_DIR 가 없습니다 — 키 폴더를 지정하세요 (docs/deploy.md 2-11)" >&2; exit 1
fi

# rootless 면 컨테이너 app(10001) = 호스트 사용자. rootful(개발 서버 root)에서는 keep-id 를 쓸 수 없어 기존 매핑 그대로
userns=()
if [[ "$(podman info --format '{{.Host.Security.Rootless}}')" == true ]]; then
  userns=(--userns=keep-id:uid=10001,gid=10001)
  if podman volume exists task_data; then
    vol=$(podman volume inspect task_data --format '{{.Mountpoint}}')
    owner=$(stat -c %u "$vol")
    [[ "$owner" == "$(id -u)" ]] || {
      echo "task_data 볼륨 주인이 호스트 uid $owner — keep-id 로는 app 이 /data 를 못 읽습니다." >&2
      echo "  한 번만: podman rm -f $NAME (있으면) 후  podman unshare chown -R 0:0 '$vol'   (docs/deploy.md 2-7)" >&2
      exit 1
    }
  fi
elif [[ -n "$images_dir" ]]; then
  echo "주의: rootful podman — keep-id 없이 띄웁니다. 묶음 파일이 컨테이너 app(10001)에게 읽히는지 따로 확인하세요" >&2
fi

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
  "${userns[@]}" \
  -v task_data:/data \
  "${images[@]}" \
  "${sftp[@]}" \
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
