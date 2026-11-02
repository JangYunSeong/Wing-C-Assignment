#!/usr/bin/env bash
# PreToolUse:Bash — .env 시크릿의 셸 열람/유출 차단
# 앱은 pydantic-settings 로 .env 를 인프로세스 로드한다. 셸에서 읽을 이유가 없다.
# .env.example/.sample/.template/.dist 는 예외. 메타데이터 조회(ls/stat/test)는 허용.

set -euo pipefail
INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[[ -z "$CMD" ]] && exit 0

if echo "$CMD" | grep -iEq '\.env\.(example|sample|template|dist)' \
   && ! echo "$CMD" | grep -iEq '\.env([[:space:]]|$|[/"'\''`])'; then
  exit 0
fi

block() {
  echo "차단: .env 시크릿은 셸에서 열람/소싱/복사할 수 없습니다($1)." >&2
  echo "앱은 Settings(pydantic-settings)로 .env 를 직접 로드합니다." >&2
  exit 2
}

READ_TOOLS='cat|tac|nl|less|more|head|tail|grep|egrep|fgrep|rg|ag|awk|sed|cut|paste|sort|uniq|rev|od|xxd|hexdump|strings|bat|view|vi|vim|nano|emacs|cp|mv|scp|rsync|tee|base64|gzip|gpg|xargs|read|python|python3|perl|ruby|node'
if echo "$CMD" | grep -iEq "(^|[|;&[:space:](])(${READ_TOOLS})([[:space:]]|$)[^|;&]*\.env([[:space:]]|$|[/\"'\`])"; then
  block "read/copy"
fi
if echo "$CMD" | grep -iEq "(^|[|;&[:space:](])(source|\.)[[:space:]]+([^|;&]*/)?\.env([[:space:]]|$|[/\"'\`])"; then
  block "source"
fi
if echo "$CMD" | grep -Eq "<[[:space:]]*([^|;&]*/)?\.env([[:space:]]|$|[/\"'\`])"; then
  block "redirect"
fi
# API 키 계열 환경변수 덤프
if echo "$CMD" | grep -iEq "(^|[|;&[:space:]])(printenv|env|export|declare|set)([[:space:]]).*(API_KEY|_TOKEN|_SECRET)|echo[^|;&]*\\\$\{?[A-Z_]*(API_KEY|_TOKEN|_SECRET)"; then
  block "env-dump"
fi
exit 0
