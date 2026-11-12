#!/usr/bin/env bash
# builder 자동 업그레이드 설치 (docs/deploy.md 2-10) — task-builder 사용자로 **처음 한 번**. 배포 사용자가 deliver.sh --setup 으로
# 받는 폴더의 setup/ 에 넣어 둔 파일을 쓴다:  sh /srv/task-spec/builder-inbox/setup/install-upgrader.sh
set -euo pipefail
SRC=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/builder-bin"
UNITS="$HOME/.config/systemd/user"
[[ "$(id -un)" != root ]] || { echo "root 금지 — task-builder 사용자로" >&2; exit 1; }
mkdir -p "$BIN" "$UNITS"
install -m 755 "$SRC/upgrade.sh" "$BIN/upgrade.sh"
[[ -f "$BIN/run.sh" ]] || install -m 755 "$SRC/run.sh" "$BIN/run.sh"  # 지금 떠 있는 builder 를 띄운 run.sh (되돌리기용)
install -m 644 "$SRC/task-builder-upgrade.path" "$SRC/task-builder-upgrade.service" "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now task-builder-upgrade.path
systemctl --user is-active task-builder-upgrade.path
echo "설치됨 — 이제 배포 사용자가 deliver.sh 로 보내면 자동으로 업그레이드합니다"
