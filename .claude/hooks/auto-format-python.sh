#!/usr/bin/env bash
# PostToolUse hook: Python 파일 자동 포매팅 (ruff)

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [[ "$FILE_PATH" != *.py ]]; then
  exit 0
fi

# ruff가 설치되어 있으면 포매팅 + lint fix 적용
if command -v ruff &> /dev/null; then
  ruff format "$FILE_PATH" 2>/dev/null || true
  ruff check --fix "$FILE_PATH" 2>/dev/null || true
fi

exit 0
