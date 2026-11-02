#!/usr/bin/env bash
# PreToolUse:Bash — 지원자 데이터가 git 에 들어가는 것 차단 (CLAUDE.md 불변식 1)
# - git add 로 data/ 경로나 -f(강제, gitignore 무시) 추가
# - git add -A / . 실행 시 data/ 가 gitignore 되어 있지 않으면 차단

set -euo pipefail
INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[[ -z "$CMD" ]] && exit 0
echo "$CMD" | grep -Eq '(^|[;&|[:space:]])git\s+(add|commit\s+-a|stage)' || exit 0

if echo "$CMD" | grep -Eq 'git\s+add(\s+\S+)*\s+(\./)?data(/|\s|$)'; then
  echo "차단: data/ 는 지원자 원본·가명화본·결과 위치라 커밋할 수 없습니다." >&2; exit 2
fi
if echo "$CMD" | grep -Eq 'git\s+add\s+(.*\s)?(-f|--force)(\s|$)'; then
  echo "차단: git add -f 는 .gitignore(지원자 데이터 보호)를 우회합니다." >&2; exit 2
fi
if echo "$CMD" | grep -Eiq 'git\s+add\s+.*\.(pdf|docx|hwp|hwpx)(\s|$)'; then
  echo "차단: 문서 원본(pdf/docx/hwp)은 커밋하지 않습니다. 테스트 픽스처는 가상 텍스트로 만드세요." >&2; exit 2
fi

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0
if git rev-parse --git-dir >/dev/null 2>&1 && [[ -d data ]]; then
  if ! git check-ignore -q data/ 2>/dev/null; then
    echo "차단: data/ 가 .gitignore 되어 있지 않습니다. 먼저 .gitignore 에 data/ 를 추가하세요." >&2; exit 2
  fi
fi
exit 0
