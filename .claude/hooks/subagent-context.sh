#!/usr/bin/env bash
# SubagentStart hook: 에이전트 유형별 프로젝트 규칙 주입 (규칙 SSOT 는 CLAUDE.md — 여기는 요약만)

read -r hook_input
agent_type=$(echo "$hook_input" | jq -r '.agent_type // empty')
[[ -z "$agent_type" ]] && exit 0

PRIV="지원자 데이터는 data/(git 밖)에만, 테스트는 가상 데이터만. 가명화 이전에 LLM·임베딩 호출 금지. 원문·가명화 매핑을 State·로그·응답에 넣지 않는다."
CODE="Python 3.11+, from __future__ import annotations, 타입 힌트, snake_case 파일명, 경계는 Pydantic(schemas/), 노드·라우터 async."

case "$agent_type" in
  graph-architect)
    context="[프로젝트 규칙] pipeline-graph 스킬이 그래프 SSOT. 승인은 approval 노드의 interrupt()만, 자동 승인 경로 금지. LLM 호출은 prompting.assemble()+llm.call()만, provenance 반환 필수. ${PRIV} ${CODE}" ;;
  api-implementor)
    context="[프로젝트 규칙] fastapi-service 스킬이 API 계약 SSOT. 라우터는 그래프만 호출(노드 직접 호출 금지). 상태는 체크포인터에서 계산. 실행은 백그라운드(RunLauncher). ${PRIV} ${CODE}" ;;
  privacy-auditor)
    context="[점검 기준] CLAUDE.md 불변식 1·2·3·8 과 pseudonymization 스킬. ${PRIV} 외부 API(OpenAI·OpenCode)는 PseudonymizedText 만 허용, 가명화 단계 내 외부 호출은 Critical. 체크포인터는 Postgres(State=지원자 파생물)." ;;
  ontology-reference)
    context="[프로젝트 규칙] ontology-artifact 스킬 절차. sqlite 는 mode=ro 로만 연다. 컬럼명은 PRAGMA table_info 로 확인 후 질의. NCS 는 셀(직군×업종) 범위 안에서만." ;;
  code-reviewer)
    context="[프로젝트 규칙] CLAUDE.md 불변식 9개 기준. ${PRIV} ${CODE}" ;;
  test-writer)
    context="[테스트 규칙] pytest + pytest-asyncio, tests/ 에 소스 구조 대응. 가상 데이터만(실존 인물·학교·회사 금지). LLM 은 FakeLLM(실 API 호출 금지), 단위는 InMemorySaver, Postgres 통합은 -m pg + TEST_DATABASE_URL(운영 DB 금지). 네거티브 테스트 필수(가명화 누락→중단, 승인 대기 아님→409 등). ${CODE}" ;;
  git-committer)
    context="[커밋 규칙] Conventional Commit(한국어): '<type>(<scope>): 제목' 50자·명령형·마침표 없음 + 본문(72자) + 푸터. type=feat|fix|chore|docs|refactor|test, scope=api|graph|nodes|prompting|prompts|llm|ontology|schemas|eval|claude|docs|tests. 'Closes #N' 또는 'Related to #N'. main 직접 커밋 금지. data/·문서 원본·.env 스테이징 금지. push 하지 않음." ;;
  git-pusher)
    context="[push 규칙] push 전 요약(브랜치·커밋 수·원격) 제시 후 사용자 확인. main 직접 push 금지. force push 금지(명시 요청 시 --force-with-lease 만). --no-verify 금지." ;;
  github-issue|github-pr)
    context="[GitHub] gh CLI. 조회는 자율, 생성·수정·종료·병합은 미리보기 + 사용자 확인. 본문에 지원자 데이터·원문 인용 금지(가상 예시만). 온톨로지 레포(JangYunSeong/job-competency-ontology) 이슈는 -R 로 명시해 참조." ;;
  *) exit 0 ;;
esac

jq -n --arg ctx "$context" '{"hookSpecificOutput":{"hookEventName":"SubagentStart","additionalContext":$ctx}}'
exit 0
