#!/usr/bin/env bash
# PreToolUse hook: Python 코딩 규칙 강제
# - from __future__ import annotations 필수
# - 파일명 snake_case 필수

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Python 파일이 아니면 패스
if [[ "$FILE_PATH" != *.py ]]; then
  exit 0
fi

# __init__.py는 빈 파일일 수 있으므로 제외
BASENAME=$(basename "$FILE_PATH" .py)
if [[ "$BASENAME" == "__init__" ]]; then
  exit 0
fi

# snake_case 파일명 체크 (대문자 포함 시 위반)
if [[ "$BASENAME" =~ [A-Z] ]]; then
  echo "Python 파일명은 snake_case여야 합니다: $BASENAME (CLAUDE.md 규칙)" >&2
  exit 2
fi

# Write 도구로 새 파일 작성 시 __future__ import 확인
CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
if [[ -n "$CONTENT" ]]; then
  if ! echo "$CONTENT" | grep -q "from __future__ import annotations"; then
    echo "Python 파일에 'from __future__ import annotations'가 필요합니다 (CLAUDE.md 규칙)" >&2
    exit 2
  fi
fi

exit 0
