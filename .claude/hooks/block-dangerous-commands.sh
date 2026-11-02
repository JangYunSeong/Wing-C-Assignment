#!/usr/bin/env bash
# PreToolUse:Bash — 파괴적 명령 차단
# - rm -rf / (루트), data/ 통째 삭제
# - git push --force (명시 요청 시 사용자가 직접), git clean -fdx (data/ 날아감)

set -euo pipefail
INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[[ -z "$CMD" ]] && exit 0

if echo "$CMD" | grep -Eq 'rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+)*/(\s|$|\*)'; then
  echo "위험: 루트 디렉토리 삭제 명령이 차단되었습니다." >&2; exit 2
fi
if echo "$CMD" | grep -Eq 'rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(\./)?data/?(\s|$)'; then
  echo "위험: data/ 전체 삭제는 차단됩니다(지원자 원본·체크포인트). 하위 경로를 지정하세요." >&2; exit 2
fi
if echo "$CMD" | grep -Eq 'git\s+clean\s+-[a-zA-Z]*x'; then
  echo "위험: git clean -x 는 gitignore 된 data/ 까지 지웁니다. 차단합니다." >&2; exit 2
fi
if echo "$CMD" | grep -Eq 'git\s+push\s.*(--force(\s|$)|-f(\s|$))'; then
  echo "위험: force push 는 차단됩니다(git-pusher 규칙: 명시 요청 시 --force-with-lease 만)." >&2; exit 2
fi
exit 0
