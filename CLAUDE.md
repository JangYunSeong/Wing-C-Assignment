# P1 과제 명세 생성 (make_task) — CLAUDE.md

> 항상 로드되는 **규칙·불변식** SSOT. 구현 사연·파일 열거는 넣지 않는다.
> 설계 출처: 온톨로지 레포 `JangYunSeong/job-competency-ontology` 이슈 #59(레포 분리 가이드라인)
> · #59 댓글(프롬프트 관리) · `context.md` §2·§4.6·§6·§7.

| 찾는 것 | 위치 |
|---|---|
| 파이프라인 그래프·State·노드 경계 | `.claude/skills/pipeline-graph/SKILL.md` |
| API 레이어 규칙 | `.claude/skills/fastapi-service/SKILL.md` |
| 프롬프트 버전·조립·provenance | `.claude/skills/prompt-registry/SKILL.md` |
| LLM 호출 공통 규칙 | `.claude/skills/llm-client/SKILL.md` |
| 온톨로지 산출물 읽기 | `.claude/skills/ontology-artifact/SKILL.md` |
| 가명화 | `.claude/skills/pseudonymization/SKILL.md` |
| 스킬·에이전트·훅 전량 | `docs/claude-config-catalog.md` |

## 목적

이력서·자기소개서 + 공고 + 온톨로지 → **과제 명세(task_spec)** → **사람 승인**.
명세 스키마는 온톨로지 레포 `context.md` §6 이 SSOT 다. 승인 없이 명세가 확정되는 경로는 만들지 않는다
(AI 기본법 고영향 AI — 사람이 최종 판단, `context.md` §7).

## 스택

Python 3.11+ · **FastAPI**(인터페이스) · **LangGraph**(파이프라인) · **PostgreSQL 16 + pgvector**
(`task_db` 컨테이너, `127.0.0.1:5434`, DB `task_db`·테스트 `task_db_test` — `docker/db/README.md`)
· LLM: **OpenAI API · OpenCode**(OpenAI 호환) — 키는 `.env` · uv(패키지) · pytest · ruff.

## 아키텍처

```
FastAPI (src/task_spec/api)          POST /runs · GET /runs/{id} · POST /runs/{id}/approve · SSE
   │  thread_id = run_id
   ▼
LangGraph StateGraph (src/task_spec/graph) + AsyncPostgresSaver
   ① posting_profile   온톨로지 조회 (결정적)
   ② ingest → pseudonymize → extract_claims        ← 가명화 이전에 LLM/임베딩 호출 금지
   ③ retrieve_t1 (역량 카드) · retrieve_t2 (이력서↔자기소개서) · evidence (NCS/ESCO 그래프 조인)
   ④ compare → draft_spec → approval(interrupt) → finalize
   │
   ├─ prompting/   prompts/<task>/vN.md 조립 + provenance
   ├─ llm/         모델 호출 래퍼 (재시도·추론 설정·비용 기록)
   └─ ontology/    버전 고정 산출물 읽기 전용 접근
```

## 불변식 (어기면 리뷰에서 Critical)

1. **지원자 데이터는 git 에 넣지 않는다.** 원본·가명화본·주장·대조·명세 시험 결과 전부 `data/`(gitignore).
   테스트 픽스처는 가상 데이터만.
2. **가명화 → 그다음에 LLM·임베딩.** 가명화되지 않은 텍스트가 `llm/` 나 임베딩 클라이언트로 가는 경로는 없다.
   외부 API(OpenAI, OpenCode) 사용 [확정: 2026-11-02] — **가명화를 통과한 입력만** 보낸다. 가명화 단계 자체(NER 등)는 외부 호출 금지.
3. **지원서는 주장 추출에만 쓴다.** 난이도·공통 과제를 지원자 문서로 바꾸지 않는다 (같은 공고 지원자 간 비교 가능성).
4. **온톨로지는 읽기만 한다.** 버전 고정 산출물(`onto-vYYYY.MM.DD`)만 읽고, 온톨로지 레포 경로를 import 하지 않는다.
   모든 명세에 `ontology_version` 을 적는다.
5. **판정은 규칙·재정렬·LLM·사람.** 벡터 유사도는 후보 재현율 보강용이지 판정이 아니다.
   공고 역량은 retrieve 에서 **가산점만** (후보를 공고 역량으로 제한하지 않는다) [확정: 2026-11-02 — `context.md` §4.6 "제한"보다 우선].
6. **역할(참여/주도/설계)은 칸 위치로 규칙 판정**한다. LLM 에 맡기지 않는다.
7. **프롬프트는 코드에 문자열로 박지 않는다.** `prompts/<task>/vN.md` + `CURRENT`. 기존 vN 은 수정 금지(새 버전 추가).
   `CURRENT` 변경은 평가 통과 후에만.
8. **모든 LLM 산출물에 provenance**: `prompt_id@version+hash`, 모델·설정, few-shot 예시 id, `ontology_version`.
9. **승인은 LangGraph `interrupt()`** 로만 받는다. API 는 resume 만 한다.

## 디렉토리 (목표)

```
src/task_spec/{api,graph,nodes,prompting,llm,ontology,schemas}/
prompts/<task>/vN.md, CURRENT
eval/            채점 스크립트 (데이터는 git 밖)
tests/           가상 데이터만
data/            git 밖 — raw/ pseudonymized/ runs/ ontology/<version>/
```

## 코딩 규칙

- `from __future__ import annotations`, 타입 힌트 필수, 파일명 snake_case (훅이 강제).
- 데이터 경계는 Pydantic 모델(`schemas/`). 노드 간 dict 남용 금지.
- 비동기 기본(FastAPI·LangGraph 노드 `async def`). 블로킹 I/O(OCR·SQLite)는 `asyncio.to_thread`.
- 로그에 원문·가명화 매핑을 남기지 않는다 (id·해시·길이만).
- 결정 기록 표기: `[확정]` / `[제안]` / `[미정]` (온톨로지 레포와 동일).

## 작업 규칙

- main 직접 커밋 금지. 커밋은 `git-committer`, push 는 `git-pusher`(확인 후).
- 이슈·PR 은 GitHub(`gh`) — `github-issue`, `github-pr` 에이전트.
- 코드 변경 후 `uv run pytest -q` (단위). Postgres 통합 테스트는 `-m pg` (`TEST_DATABASE_URL` 필요).
  실제 LLM API 스모크는 `-m llm` — 비용이 들므로 프롬프트·llm 변경 때만, 가상 텍스트로.
- 레포: `JangYunSeong/p1-task-spec` (private).
