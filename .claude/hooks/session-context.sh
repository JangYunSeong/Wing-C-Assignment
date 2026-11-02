#!/usr/bin/env bash
# SessionStart hook: 세션 시작 시 "지금 이 순간" 의 상태만 알린다.
#
# 규칙·아키텍처는 CLAUDE.md 가 SSOT 이고 이미 컨텍스트에 올라간다 — 여기서 되풀이하면
# 두 곳이 갈라진다(실제로 갈라져서 이 훅이 낡은 상태를 단언하고 있었다). 그래서 이 훅은
# 정적 문서가 알 수 없는 것만 출력한다: 지금 어느 브랜치이고, 작업 중인 변경이 있는지.

set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  exit 0
fi

branch=$(git branch --show-current 2>/dev/null)
head=$(git log -1 --format='%h %s' 2>/dev/null | cut -c1-72)
dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')

printf '브랜치: %s | HEAD: %s | 미커밋 변경: %s개' \
  "${branch:-(detached)}" "${head:-없음}" "$dirty"

if [[ "${branch:-}" == "main" ]]; then
  printf ' — main 에 직접 커밋하지 말고 작업 브랜치를 먼저 딸 것'
fi

if [[ "$dirty" != "0" ]]; then
  printf '\n미커밋 파일:\n%s' "$(git status --short 2>/dev/null | head -10)"
fi

printf '\n규칙·아키텍처는 CLAUDE.md 참조(SSOT).\n'
exit 0
