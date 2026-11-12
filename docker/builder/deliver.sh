#!/usr/bin/env bash
# builder 새 이미지 보내기 (docs/deploy.md 2-10) — **배포 사용자**로, 레포 루트에서. task-builder 가 자동으로 업그레이드한다.
#
#   docker/builder/deliver.sh            # 지금 커밋으로 이미지 빌드 → 검사 → 받는 폴더에 넣고 결과(last-upgrade.json)를 기다림
#   docker/builder/deliver.sh --setup    # 처음 한 번: 받는 폴더 setup/ 에 설치 파일을 넣는다 (task-builder 가 install-upgrader.sh 실행)
set -euo pipefail
cd "$(dirname "$0")/../.."
INBOX="${TASK_BUILDER_INBOX:-/srv/task-spec/builder-inbox}"
[[ -d "$INBOX" && -w "$INBOX" ]] || { echo "받는 폴더가 없거나 쓸 수 없습니다: $INBOX — docs/deploy.md 2-10" >&2; exit 1; }

if [[ "${1:-}" == --setup ]]; then
  mkdir -p "$INBOX/setup"
  install -m 644 docker/builder/upgrade.sh docker/builder/install-upgrader.sh docker/builder/run.sh \
    docker/builder/systemd/task-builder-upgrade.path docker/builder/systemd/task-builder-upgrade.service "$INBOX/setup/"
  echo "task-builder 사용자로:  bash $INBOX/setup/install-upgrader.sh   (sh 는 안 됨 — bash 스크립트)"
  exit 0
fi

[[ -z "$(git status --porcelain -- src prompts incidents templates docker/builder pyproject.toml uv.lock)" ]] \
  || { echo "커밋 안 된 변경이 있습니다 — 이미지가 커밋과 달라집니다" >&2; exit 1; }
VER="${TASK_BUILDER_VERSION:-$(date +%Y%m%d)-$(git rev-parse --short HEAD)}"
podman build -q -f docker/builder/Dockerfile -t "task-builder:${VER}" . >/dev/null
# 이미지에 넣으면 안 되는 파일 (build_release.sh 와 같은 검사)
podman run --rm --network none --entrypoint sh "task-builder:${VER}" -c \
  'test ! -e /app/.env && test ! -e /app/data && test ! -e /app/tests && test ! -e /app/eval && test -z "$(find /app/templates -name .gradle -o -name node_modules)"' \
  || { echo "builder 이미지에 넣으면 안 되는 파일이 있습니다" >&2; exit 1; }

# 체크섬 파일을 마지막에 쓴다 — path 유닛은 그 파일이 생길 때 돌므로 반쯤 쓴 tar 를 읽지 않는다
podman save -o "$INBOX/.task-builder-$VER.tar.part" "task-builder:${VER}"
mv -f "$INBOX/.task-builder-$VER.tar.part" "$INBOX/task-builder-$VER.tar"
install -m 644 docker/builder/run.sh "$INBOX/run-$VER.sh"
(cd "$INBOX" && sha256sum "task-builder-$VER.tar" "run-$VER.sh" >".sum.part" && mv -f .sum.part "task-builder-$VER.sha256")
echo "보냄: task-builder:$VER — task-builder 의 업그레이드를 기다립니다 (지금 작업이 있으면 끝난 뒤)"

for _ in $(seq 1 120); do  # 최대 20분
  sleep 10
  if [[ -f "$INBOX/last-upgrade.json" ]] && grep -q "\"version\": \"$VER\"" "$INBOX/last-upgrade.json"; then
    cat "$INBOX/last-upgrade.json"
    grep -q '"result": "ok"' "$INBOX/last-upgrade.json" && exit 0 || exit 1
  fi
done
echo "20분 안에 결과가 없습니다 — task-builder 의 'systemctl --user status task-builder-upgrade.path' 를 확인하세요" >&2
exit 1
