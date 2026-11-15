#!/usr/bin/env bash
# 운영 서버 한 번에 배포 (docs/deploy.md 2-13) — **배포 사용자**로, 레포 루트에서. API 를 지금 커밋으로 다시 띄우고 builder 새 이미지를 보낸다.
# 비밀번호·sudo 없음: API 는 배포 사용자의 컨테이너, builder 는 받는 폴더에 넣으면 task-builder 가 스스로 업그레이드한다 (2-10).
#
#   docker/deploy_all.sh                 # API + builder
#   docker/deploy_all.sh --api-only      # API 만
#   docker/deploy_all.sh --builder-only  # builder 만 (= docker/builder/deliver.sh)
#
#   TASK_API_ENV  API env 파일 (기본 docker/api/.env — 작업 폴더가 따로면 메인 폴더의 것을 지정)
#
# 순서: 커밋 안 된 변경 검사 → API 이미지 빌드 → 지금 API 이미지를 기억 → 교체 → /healthz 가 ok 가 아니면 이전 이미지로 되돌림
#       → builder 이미지 보내기(deliver.sh, last-upgrade.json 이 같은 버전 ok 가 될 때까지 기다림).
# DB 스키마는 API 가 기동 때 만든다 — 열을 더하는 변경이면 builder 도 같이 바꿔야 한다 (지금 builder 가 예전 연결의 준비문으로 실패, 2026-11-15).
set -euo pipefail
cd "$(dirname "$0")/.."

api=1 builder=1
case "${1:-}" in
  --api-only) builder=0 ;;
  --builder-only) api=0 ;;
  "") ;;
  *) echo "사용: $0 [--api-only|--builder-only]" >&2; exit 2 ;;
esac

[[ -z "$(git status --porcelain -- src prompts incidents templates docker pyproject.toml uv.lock)" ]] \
  || { echo "커밋 안 된 변경이 있습니다 — 이미지가 커밋과 달라집니다" >&2; exit 1; }
VER="$(date +%Y%m%d)-$(git rev-parse --short HEAD)"
echo "배포 판: $VER ($(git log -1 --format=%s | cut -c1-80))"

if (( api )); then
  NAME="${TASK_API_NAME:-task-api}"
  echo "① API 이미지 빌드 — task-api:$VER"
  podman build -q -f docker/api/Dockerfile -t "task-api:$VER" . >/dev/null
  prev=$(podman inspect "$NAME" --format '{{.ImageName}}' 2>/dev/null || true)
  echo "② API 교체 (이전 ${prev:-없음})"
  podman rm -f -t 30 "$NAME" >/dev/null 2>&1 || true
  healthy() {
    local s=""
    for _ in $(seq 1 40); do
      s=$(curl -s "http://${TASK_API_BIND:-127.0.0.1:8001}/healthz" || true)
      [[ "$s" == *'"status":"ok"'* ]] && return 0
      sleep 3
    done
    echo "헬스체크 실패: ${s:-응답 없음}" >&2
    return 1
  }
  if TASK_API_IMAGE="localhost/task-api:$VER" docker/api/run.sh && healthy; then
    echo "   API ok — task-api:$VER"
  else
    if [[ -n "$prev" ]]; then
      echo "   새 API 가 뜨지 않음 — 이전 이미지 $prev 로 되돌림" >&2
      podman rm -f -t 10 "$NAME" >/dev/null 2>&1 || true
      TASK_API_IMAGE="$prev" docker/api/run.sh && healthy && echo "   되돌림 ok — $prev" >&2
    fi
    exit 1
  fi
fi

if (( builder )); then
  echo "③ builder 이미지 보내기 — task-builder:$VER (지금 작업이 있으면 끝난 뒤 교체)"
  TASK_BUILDER_VERSION="$VER" docker/builder/deliver.sh
fi
echo "배포 끝 — $VER"
