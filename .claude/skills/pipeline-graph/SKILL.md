---
name: pipeline-graph
description: P1 과제 명세 파이프라인(①공고 프로필 → ②입력·가명화·주장 추출 → ③retrieve·평가 근거 → ④대조·명세·사람 승인)의 LangGraph 그래프 설계 SSOT. State 스키마, 노드 경계, 승인 interrupt 위치, 재시도·에러 정책, 체크포인터를 다룬다. src/task_spec/graph/ 나 nodes/ 를 만들거나 고칠 때, 노드를 추가·분리할 때, 파이프라인 흐름·State 필드를 바꿀 때 사용. LangGraph API 자체는 langgraph-fundamentals / langgraph-persistence / langgraph-human-in-the-loop 스킬을 함께 본다.
---

# pipeline-graph — P1 LangGraph 설계

API 사용법은 vendored 스킬(`langgraph-fundamentals`, `langgraph-persistence`, `langgraph-human-in-the-loop`)이 담당한다.
이 스킬은 **이 프로젝트의 그래프 모양과 규칙**만 정한다.

## 그래프

```
START
  └─ posting_profile          ① 온톨로지 조회. LLM 없음. 공고 없으면 END(posting_not_found)
  └─ ingest                   ② 문서별 텍스트 추출(PDF 텍스트층 → 스캔본만 OCR) + 칸 분리
  └─ pseudonymize             ② 치환 + 누락 검사. 실패 시 END(error) — 다음 노드로 절대 넘기지 않음
  └─ extract_claims           ② LLM. 주장마다 인용이 속한 ⟦칸⟧(field)을 기록
  └─ assign_roles             ② 칸 → 역할 규칙 판정 (LLM 아님, nodes/roles.py)
  ├─ retrieve_t1              ③ 주장 → 역량 후보 (패턴+BM25(카드·예문) RRF, 범위 soft, 공고 가산점 → LLM 0~2개 선택·uncovered)
  └─ retrieve_t2              ③ 이력서↔자기소개서 연결·어긋남 (문서가 둘 다 있을 때만)
  └─ evidence                 ③ 역량 → NCS 수행준거·KSA / ESCO 그래프 조인 (결정적)
  └─ compare                  ④ 겹침 / 공고에만 / 지원자에만(직무 연계 강·중·약) / uncovered + 검증 후보 주장 점수 순 (결정적, nodes/compare.py)
  └─ draft_spec               ④ 공통 과제(공고 기준) + 경험 검증 과제(claims_to_verify)
  └─ approval                 ④ interrupt() — 사람 승인/반려/수정 요청
       ├─ approved  → finalize → END
       ├─ revise    → draft_spec (반려 사유를 State 에 넣고 재생성)
       └─ rejected  → END
```

- `retrieve_t1` 과 `retrieve_t2` 는 서로 독립 → 병렬 분기 후 `evidence` 에서 합류.
- 지원자 문서 없이 공고만 들어오면 ② 와 ③ retrieve 를 건너뛰고 **evidence(공고 역량의 평가 근거)** → 공통 과제 (조건부 엣지).

## State

```python
from __future__ import annotations
from typing import Annotated, Literal
from operator import add
from pydantic import BaseModel

class RunState(BaseModel):
    run_id: str
    ontology_version: str                 # 실행 시작 때 고정, 이후 불변
    posting_id: str
    documents: list[DocumentRef] = []     # 경로·종류(resume|cover_letter)·해시. 원문 텍스트는 State 에 넣지 않는다
    posting_profile: PostingProfile | None = None
    pseudo_docs: list[PseudoDocRef] = []  # 가명화본 저장 위치 + 검사 결과. 매핑 테이블은 State 밖(data/ 암호화 저장)
    claims: list[Claim] = []              # 교체 — assign_roles 가 역할을 채운 목록으로 바꾼다
    candidates: list[ClaimCandidates] = []
    links: list[CrossDocLink] = []
    evidence: list[CompetencyEvidence] = []
    comparison: Comparison | None = None
    spec: TaskSpec | None = None
    approval: ApprovalDecision | None = None
    provenance: Annotated[list[Provenance], add] = []   # LLM 호출마다 1건 (prompt-registry)
    errors: Annotated[list[NodeError], add] = []
```

규칙
- **원문·가명화 매핑은 State 에 넣지 않는다.** 체크포인터가 State 를 영속화하므로 State = 디스크.
  State 에는 `data/` 안의 참조(경로·해시)만 둔다.
- fan-out 결과를 모으는 필드만 reducer(`add`)를 쓴다.
- 모든 스키마는 `src/task_spec/schemas/` 의 Pydantic 모델. 노드는 dict 를 반환하되 키는 State 필드명.

## 역할 규칙 [확정: 2026-11-02]

| 주장이 속한 칸 | 역할 |
|---|---|
| 과정 · 담당업무 | 참여 (participate) |
| 기여한 점 | 주도 (lead) |
| 기여한 점·담당업무 + 설계 표현(설계·아키텍처·구조 수립/정의) | 설계 (design) |
| 문제 정의 · 성과 · 사용 기술 · 칸 밖(자기소개서 산문 등) | 판정 안 함 (None) → 명세에서 '역할 확인 필요' |

주장마다 `field`(칸)·`role_basis`(근거)를 남긴다. 칸은 다음 칸 표시·빈 줄·절 제목·기간 머리말에서 끝난다.

## 과제 기술 환경 [제안: 2026-11-02 — draft_spec 착수 시 온톨로지 enables 데이터를 보고 확정]

| 과제 트랙 | 기술 환경(언어·프레임워크) |
|---|---|
| 공통 과제 | **공고 언어만** — 지원자 문서와 무관 (불변식 3, 같은 공고 지원자 간 비교 가능성) |
| 경험 검증 과제 (`claims_to_verify`) | 지원자 `사용 기술` 칸 ∩ 공고 언어가 있으면 **공고 언어**, 없으면 지원자 역량의 **프레임워크**(온톨로지 `enables`: 도구 → 역량) |

입력: ingest 의 `사용 기술` 칸(가명화본) + ① 공고 프로필의 언어·도구. 판정은 규칙(온톨로지 도구 별칭 대조), LLM 아님.
draft_spec 은 선택한 기술 환경과 근거(겹친 언어 / 사용한 enables 관계)를 명세에 남긴다.

## 노드 규칙

- 노드 = `async def node(state: RunState) -> dict`. 하나의 노드는 하나의 단계만.
- LLM 을 부르는 노드는 `prompting.assemble()` + `llm.call()` 만 쓴다 — 직접 모델 클라이언트 생성 금지.
  반환에 `provenance` 를 반드시 포함.
- `pseudonymize` 이전 노드(`posting_profile`, `ingest`)는 LLM·임베딩을 import 하지 않는다.
- 결정적 노드(`posting_profile`, `assign_roles`, `evidence`)는 같은 입력 → 같은 출력. 테스트로 고정.

## 승인 (interrupt)

```python
from langgraph.types import interrupt, Command

async def approval(state: RunState) -> Command:
    decision = interrupt({                 # 반드시 노드 첫 줄. resume 시 노드가 처음부터 다시 돈다
        "run_id": state.run_id,
        "spec": state.spec.model_dump(mode="json"),
        "provenance": [p.model_dump(mode="json") for p in state.provenance],
    })
    d = ApprovalDecision.model_validate(decision)
    goto = {"approved": "finalize", "revise": "draft_spec", "rejected": "__end__"}[d.status]
    return Command(update={"approval": d}, goto=goto)
```

- interrupt 이전에 부작용(저장·외부 호출)을 두지 않는다.
- 승인 결정에는 `reviewer`, `at`, `comment` 를 남긴다 (명세 `approval` 필드, `context.md` §6).
- 자동 승인 경로·타임아웃 승인은 만들지 않는다.

## 체크포인터

| 환경 | 체크포인터 |
|---|---|
| 단위 테스트 | `InMemorySaver` |
| 통합 테스트 | `AsyncPostgresSaver` — `task_db_test` (`TEST_DATABASE_URL`), pytest 마커 `pg` |
| 로컬·운영 | `AsyncPostgresSaver` — `task_db` (`DATABASE_URL`) [확정: 2026-11-02] |

DB 는 `task_db` 컨테이너(`docker/db/`, PostgreSQL 16 + pgvector, `127.0.0.1:5434`, 사용자 `task`).

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

@asynccontextmanager
async def make_checkpointer(settings):
    if settings.checkpointer == "memory":
        yield InMemorySaver(); return
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as cp:
        await cp.setup()          # 체크포인트 테이블 생성 (멱등)
        yield cp
```

- 패키지: `langgraph-checkpoint-postgres`, `psycopg[binary,pool]`.
- `thread_id = run_id`. 체크포인트 테이블은 State 를 담으므로 **지원자 파생물** — 원문·매핑을 State 에 넣지 않는 규칙이 여기서 중요해진다.
- 통합 테스트는 실행마다 고유 `thread_id`, 끝나면 해당 thread 삭제(`adelete_thread`). 운영 DB URL 로 테스트를 돌리지 않는다
  (`TEST_DATABASE_URL` 이 `DATABASE_URL` 과 같으면 conftest 가 거부).

## 에러·재시도

| 종류 | 처리 |
|---|---|
| LLM 일시 오류·빈 응답 | `llm-client` 가 재시도 (노드는 모름) |
| 출력 스키마 위반 | 노드에서 1회 재요청 후 `errors` 기록, 해당 주장만 skip |
| 원문 인용 대조 실패 | 그 주장 폐기 + `errors` 기록 (재요청하지 않음) |
| 가명화 누락 검사 실패 | **즉시 END** — 다음 단계 진행 금지 |
| 온톨로지 산출물 버전 불일치 | 실행 시작 전 거부 |

LangGraph `RetryPolicy` 는 I/O 노드(ingest, evidence)에만 건다. LLM 재시도와 이중으로 걸지 않는다.

## 테스트

- 노드 단위: 가상 State → 반환 dict 검사.
- 그래프 단위: `InMemorySaver` + 가짜 LLM(고정 응답)으로 START → approval interrupt → `Command(resume=...)` → END.
- 승인 3갈래(approved/revise/rejected) 각각 테스트.
