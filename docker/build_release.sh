#!/usr/bin/env bash
# 내부망 반입 묶음 만들기 (deployment 스킬 "빌드·반입"). 레포 루트에서 실행. 인터넷이 되는 빌드 서버에서만.
#
# 사용: docker/build_release.sh [버전]     (기본 버전 = 날짜-커밋)
# 결과: dist/task-spec-<버전>/
#   images.tar (+ .sha256)   task-api:<버전>, task-db:1   → 서버에서 podman load -i images.tar
#   docker/                  run 스크립트·env 예시·DB 초기화 스크립트
# DB 데이터(덤프)는 별도 — data/exports/ 의 덤프를 함께 전달 (지원자 파생물 포함 여부 확인 후).
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -z "$(git status --porcelain -- src prompts incidents pyproject.toml uv.lock docker)" ]] \
  || { echo "커밋되지 않은 변경이 있습니다 (src·prompts·incidents·docker) — 커밋된 상태로만 묶습니다" >&2; exit 1; }

VER="${1:-$(date +%Y%m%d)-$(git rev-parse --short HEAD)}"
OUT="dist/task-spec-${VER}"
mkdir -p "$OUT"

podman build -f docker/api/Dockerfile -t "task-api:${VER}" .
podman build -q -f docker/db/Dockerfile -t task-db:1 docker/db >/dev/null

# 이미지에 비밀값·지원자 데이터가 없는지 확인
podman run --rm --entrypoint sh "task-api:${VER}" -c \
  'test ! -e /app/.env && test ! -e /app/data && test -z "$(ls -A /data)" && test ! -e /app/tests' \
  || { echo "이미지에 넣으면 안 되는 파일이 있습니다" >&2; exit 1; }

podman save -o "$OUT/images.tar" "task-api:${VER}" task-db:1
( cd "$OUT" && sha256sum images.tar > images.tar.sha256 )
mkdir -p "$OUT/docker"
cp -r docker/api docker/db "$OUT/docker/"
printf 'task-api:%s\ntask-db:1\ncommit %s\n' "$VER" "$(git rev-parse HEAD)" > "$OUT/VERSION"
echo "→ $OUT ($(du -sh "$OUT/images.tar" | cut -f1))"
