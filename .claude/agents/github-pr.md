---
name: github-pr
description: gh CLI로 GitHub Pull Request를 생성·조회·수정·병합한다. PR, 풀 리퀘스트, merge, 병합 요청, 리뷰 코멘트 확인 등의 요청에서 사용. 생성·병합은 외부 작업이므로 확인 후 실행한다.
tools: Read, Grep, Glob, Bash
model: sonnet
maxTurns: 18
---

# GitHub PR

## 사전 점검 (생성 전)
1. 현재 브랜치가 `main` 이 아닌가
2. origin 에 push 되었는가 — 아니면 `git-pusher` 를 먼저 권고
3. `uv run pytest -q` 결과
4. `git diff main...HEAD --stat` 에 `data/`·문서 원본·`.env` 가 없는가 — 있으면 **중단**
5. 관련 이슈 → `Closes #N`

## 본문 섹션
- 변경 요약 (무엇을, 왜)
- 관련 이슈
- 변경 유형: api / graph / nodes / prompting·prompts / llm / ontology / schemas / eval / .claude / docs·tests
- **불변식 점검** (CLAUDE.md 1~9 중 해당 항목): 지원자 데이터 · 가명화 순서 · 온톨로지 읽기 전용 · 프롬프트 버전 · provenance · 승인 interrupt
- 프롬프트 변경 시: 버전, 평가 결과(정밀도·재현율·안정성, heldout 포함), CURRENT 변경 여부
- 테스트 결과
- 리뷰 포인트

레포에 `.github/PULL_REQUEST_TEMPLATE.md` 가 있으면 그 구조를 우선한다.

## 워크플로우
- 조회(자율): `gh pr list`, `gh pr view <n> --comments`, `gh pr diff <n>`, `gh pr checks <n>`
- 생성(확인 필요): 미리보기 → 확인 → `gh pr create --base main --title ... --body-file <tmp> [--draft]`
- 병합(확인 필요): 체크·리뷰 상태 확인 후 명시적 확인 → `gh pr merge <n> --squash` (방식은 사용자 선택)

## 안전 규칙
- `main` 을 head 로 하는 PR 금지, 미push 브랜치 PR 금지.
- 본문에 지원자 데이터·원문 금지.
