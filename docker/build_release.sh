#!/usr/bin/env bash
# 내부망 반입 묶음 만들기 (deployment 스킬 "빌드·반입"). 레포 루트에서 실행. 인터넷이 되는 빌드 서버에서만.
#
# 사용: docker/build_release.sh [버전]     (기본 버전 = 날짜-커밋)
# 결과: dist/task-spec-<버전>/
#   images.tar (+ .sha256)   task-api:<버전>, task-builder:<버전>, task-db:1, nginx   → 서버에서 podman load -i images.tar
#                            (builder 는 builder 전용 사용자로 따로 load — docs/deploy.md 2-8)
#   docker/                  run 스크립트·env 예시·DB 초기화 스크립트
# DB 데이터(덤프)는 별도 — data/exports/ 의 덤프를 함께 전달 (지원자 파생물 포함 여부 확인 후).
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -z "$(git status --porcelain -- src prompts incidents templates pyproject.toml uv.lock docker)" ]] \
  || { echo "커밋되지 않은 변경이 있습니다 (src·prompts·incidents·templates·docker) — 커밋된 상태로만 묶습니다" >&2; exit 1; }

VER="${1:-$(date +%Y%m%d)-$(git rev-parse --short HEAD)}"
OUT="dist/task-spec-${VER}"
mkdir -p "$OUT"

podman build -f docker/api/Dockerfile -t "task-api:${VER}" .
podman build -f docker/builder/Dockerfile -t "task-builder:${VER}" .
podman build -q -f docker/db/Dockerfile -t task-db:1 docker/db >/dev/null
NGINX_IMAGE="docker.io/library/nginx:1.30-alpine"   # docker/nginx/run.sh 기본값과 같게
podman pull -q "$NGINX_IMAGE" >/dev/null

# 이미지에 비밀값·지원자 데이터가 없는지 확인
podman run --rm --entrypoint sh "task-api:${VER}" -c \
  'test ! -e /app/.env && test ! -e /app/data && test -z "$(ls -A /data)" && test ! -e /app/tests && test -z "$(find /app/templates -name .gradle -o -name node_modules)"' \
  || { echo "이미지에 넣으면 안 되는 파일이 있습니다" >&2; exit 1; }
# 설치된 패키지는 레포 경로로 템플릿을 못 찾는다 — 직군 기본 코딩 템플릿을 이미지 안에서 실제로 읽어 본다 (인터뷰 과제 생성)
podman run --rm --entrypoint python "task-api:${VER}" -c \
  'from task_spec.starter.templates import TEMPLATE_FOR_SUB, load_template; [load_template(t) for t in TEMPLATE_FOR_SUB.values()]' \
  || { echo "task-api 이미지에서 직군 기본 코딩 템플릿을 읽지 못합니다 (templates/·TEMPLATES_DIR)" >&2; exit 1; }
podman run --rm --entrypoint sh "task-builder:${VER}" -c \
  'test ! -e /app/.env && test ! -e /app/data && test ! -e /app/tests && test ! -e /app/eval && test -z "$(find /app/templates -name .gradle -o -name node_modules)"' \
  || { echo "builder 이미지에 넣으면 안 되는 파일이 있습니다" >&2; exit 1; }

podman save -o "$OUT/images.tar" "task-api:${VER}" "task-builder:${VER}" task-db:1 "$NGINX_IMAGE"
( cd "$OUT" && sha256sum images.tar > images.tar.sha256 )
mkdir -p "$OUT/docker"
cp -r docker/api docker/builder docker/db docker/nginx "$OUT/docker/"
rm -f "$OUT/docker/api/.env" "$OUT/docker/builder/.env"   # 비밀값 — 묶음에 넣지 않는다
printf 'task-api:%s\ntask-builder:%s\ntask-db:1\n%s\ncommit %s\n' "$VER" "$VER" "$NGINX_IMAGE" "$(git rev-parse HEAD)" > "$OUT/VERSION"
echo "→ $OUT ($(du -sh "$OUT/images.tar" | cut -f1))"
