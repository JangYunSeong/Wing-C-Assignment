#!/usr/bin/env bash
# SubagentStop hook: 서브에이전트 결과 품질 체크
read -r hook_input
agent_type=$(echo "$hook_input" | jq -r '.agent_type // empty')
last_message=$(echo "$hook_input" | jq -r '.last_assistant_message // empty')
[[ -z "$agent_type" || -z "$last_message" ]] && exit 0

if [[ "$agent_type" == "test-writer" ]]; then
  if echo "$last_message" | grep -q "def test_" && \
     ! echo "$last_message" | grep -qi "invalid\|error\|negative\|fail\|raises\|exception\|거부\|차단"; then
    jq -n '{"decision":"block","reason":"[품질 체크] 네거티브 테스트가 없습니다. 잘못된 입력·가명화 누락·승인 상태 오류 같은 실패 경로도 작성하세요."}'
    exit 0
  fi
fi
exit 0
