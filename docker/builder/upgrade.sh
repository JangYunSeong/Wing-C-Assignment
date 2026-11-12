#!/usr/bin/env bash
# builder 자동 업그레이드 (docs/deploy.md 2-10) — task-builder 사용자로 돈다. systemd path 유닛(task-builder-upgrade.path)이
# 받는 폴더에 task-builder-<버전>.sha256 이 생기면 부른다. 배포 사용자는 docker/builder/deliver.sh 로 이미지·run.sh·체크섬을 넣기만 한다.
#
# 순서: 받은 파일을 작업 폴더로 옮김(실패해도 같은 파일로 다시 돌지 않게) → 체크섬 → podman load → 지금 builder 정지(작업 끝날 때까지,
# 컨테이너 --stop-timeout) → 새 run.sh 로 기동 → 'builder 시작' 로그 확인 → 실패면 이전 이미지·run.sh 로 되돌림.
# 결과는 받는 폴더의 last-upgrade.json (배포 사용자가 읽는다). 설정 파일(~/builder-settings, 비밀값)은 건드리지 않는다.
set -uo pipefail

INBOX="${TASK_BUILDER_INBOX:-/srv/task-spec/builder-inbox}"
BIN="${TASK_BUILDER_BIN:-$HOME/builder-bin}"
ENV_FILE="${TASK_BUILDER_ENV:-$HOME/builder-settings}"
NAME="${TASK_BUILDER_NAME:-task-builder}"
REPO="${TASK_BUILDER_REPO:-task-builder}"  # 이미지 이름 (시험할 때만 바꾼다)
STATUS="$INBOX/last-upgrade.json"

mkdir -p "$BIN"
exec 9>"$BIN/.upgrade.lock"
flock -n 9 || exit 0  # 이미 도는 중

status() {  # 결과 버전 메시지 — 메시지는 이 스크립트가 만든 짧은 문장만 (원문·비밀값 없음)
  local msg=${3//\"/\'}
  printf '{"result": "%s", "version": "%s", "at": "%s", "message": "%s"}\n' "$1" "$2" "$(date -Is)" "$msg" >"$STATUS.tmp" \
    && mv -f "$STATUS.tmp" "$STATUS"
  echo "업그레이드 $1 ($2): $3"
}

started() {  # 기동 뒤 'builder 시작' 로그가 나오고 계속 떠 있으면 성공 (최대 60초)
  for _ in $(seq 1 30); do
    sleep 2
    [[ "$(podman inspect "$NAME" --format '{{.State.Running}}' 2>/dev/null)" == true ]] || continue
    podman logs "$NAME" 2>&1 | grep -q "builder 시작" && return 0
  done
  return 1
}

launch() {  # 이미지 run.sh
  podman rm -f "$NAME" >/dev/null 2>&1
  # 잠금(9번)을 닫고 넘긴다 — podman run 이 띄운 conmon 이 물려받으면 잠금이 계속 잡혀 다음 업그레이드가 돌지 않는다
  TASK_BUILDER_ENV="$ENV_FILE" TASK_BUILDER_IMAGE="$1" "$2" 9>&- && started
}

for sum in "$INBOX"/task-builder-*.sha256; do
  [[ -e "$sum" ]] || continue
  ver=$(basename "$sum" .sha256)
  ver=${ver#task-builder-}
  work=$(mktemp -d "$BIN/up.XXXXXX")  # 받는 폴더와 같은 디스크 — 수백 MB 를 복사하지 않고 옮긴다
  mv -f "$sum" "$work/" && mv -f "$INBOX/task-builder-$ver.tar" "$INBOX/run-$ver.sh" "$work/" 2>/dev/null
  if ! (cd "$work" && sha256sum -c --quiet "task-builder-$ver.sha256"); then
    status failed "$ver" "체크섬이 맞지 않거나 파일이 빠짐 — 다시 보내세요"
    rm -rf "$work"
    continue
  fi
  image=$(podman load -i "$work/task-builder-$ver.tar" 2>&1 | sed -n 's/^Loaded image: //p' | tail -1)
  if [[ "$image" != *"$REPO:$ver" ]]; then
    status failed "$ver" "이미지 불러오기 실패 (태그 ${image:-없음})"
    rm -rf "$work"
    continue
  fi
  prev_image=$(podman inspect "$NAME" --format '{{.ImageName}}' 2>/dev/null || true)
  [[ -n "$prev_image" ]] || prev_image=$(podman ps -a --filter "name=^${NAME}\$" --format '{{.Image}}' 2>/dev/null | head -1)
  install -m 755 "$work/run-$ver.sh" "$BIN/run.sh.new"
  podman stop "$NAME" >/dev/null 2>&1 9>&-  # SIGTERM → 지금 작업 뒤 멈춤 (--stop-timeout 뒤 강제, lease 가 지나 다시 대기열로)
  if launch "$image" "$BIN/run.sh.new"; then
    [[ -f "$BIN/run.sh" ]] && mv -f "$BIN/run.sh" "$BIN/run.sh.prev"
    mv -f "$BIN/run.sh.new" "$BIN/run.sh"
    # 이미지는 지금 것과 바로 앞 것만 남긴다
    podman images --format '{{.Repository}}:{{.Tag}}' | grep -E "(^|/)$REPO:" \
      | grep -vxF -e "$image" -e "${prev_image:-none}" | xargs -r podman rmi >/dev/null 2>&1
    status ok "$ver" "기동 확인 (이전 ${prev_image:-없음})"
  elif [[ -n "$prev_image" && -f "$BIN/run.sh" ]] && launch "$prev_image" "$BIN/run.sh"; then
    rm -f "$BIN/run.sh.new"
    status failed "$ver" "새 이미지 기동 실패 — 이전 이미지 $prev_image 로 되돌림 (podman logs 확인)"
  else
    status failed "$ver" "기동 실패, 되돌리기도 실패 — builder 가 멈춰 있음 (task-builder 사용자로 podman logs 확인)"
  fi
  rm -rf "$work"
done
exit 0  # 받은 파일이 없을 때도 성공 (path 유닛이 다시 불러도 실패로 남지 않게)
