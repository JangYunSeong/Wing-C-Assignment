#!/usr/bin/env bash
# PreToolUse:Edit|Write — 가명화 이전 노드가 LLM/임베딩을 부르면 차단, 그 밖의 LLM 코드엔 리마인드
# (CLAUDE.md 불변식 2, pseudonymization 스킬)

set -euo pipefail
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[[ "$FILE_PATH" == *.py ]] || exit 0
CONTENT=$(echo "$INPUT" | jq -r '(.tool_input.content // "") + "\n" + (.tool_input.new_string // "")')

LLM_RE='(task_spec\.llm|from \.\.?llm|init_chat_model|ChatOpenAI|ChatOllama|langchain_openai|langchain_ollama|openai|Embeddings|embed_(query|documents))'

# 가명화 이전 단계 모듈: LLM/임베딩 사용 금지
if [[ "$FILE_PATH" =~ /nodes/(ingest|posting_profile|pseudonymize)(/|\.py) ]]; then
  if echo "$CONTENT" | grep -Eq "$LLM_RE"; then
    echo "차단: $FILE_PATH 는 가명화 이전 단계입니다. LLM·임베딩을 부를 수 없습니다 (CLAUDE.md 불변식 2)." >&2
    exit 2
  fi
  exit 0
fi

# 프롬프트 문자열 하드코딩 경고 (불변식 7)
if echo "$CONTENT" | grep -Eq '("|'"'"')(system|user)("|'"'"')\s*,\s*("|'"'"')[^"'"'"']{80,}'; then
  echo "경고: 코드에 긴 프롬프트 문자열이 보입니다. prompts/<task>/vN.md 로 옮기세요 (prompt-registry)." >&2
fi
exit 0
