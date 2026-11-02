#!/usr/bin/env bash
# PreToolUse:Edit|Write — 기존 프롬프트 버전(prompts/<task>/vN.md) 수정 차단 (prompt-registry §1)
# 새 vN 파일 생성과 CURRENT 수정은 허용.

set -euo pipefail
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[[ "$FILE_PATH" =~ /prompts/[^/]+/v[0-9]+\.md$ ]] || exit 0

if [[ -e "$FILE_PATH" ]]; then
  echo "차단: $FILE_PATH 는 이미 있는 프롬프트 버전입니다. 기존 버전은 고치지 않고 새 vN+1.md 를 추가하세요 (prompt-registry)." >&2
  exit 2
fi
exit 0
