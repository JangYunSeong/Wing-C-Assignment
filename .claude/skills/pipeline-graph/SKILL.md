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
  └─ extract_claims           ② LLM. 주장마다 인용이 속한 ⟦칸⟧(field)을 기록 · 1,500자 넘는 문서는 칸 밖 경계에서 처음부터 나눠 병렬 · 출력 잘림이면 반씩 다시 (nodes/claims.extract_parts, 호출 상한 2×조각+1 (9~25)·동시 3, 일부 조각 실패는 claims_partial)
  └─ assign_roles             ② 칸 → 역할 규칙 판정 (LLM 아님, nodes/roles.py)
  ├─ retrieve_t1              ③ 주장 → 역량 후보 (패턴+BM25(카드·예문) RRF, 범위 soft, 공고 가산점 → LLM 0~2개 선택·uncovered)
  └─ retrieve_t2              ③ 이력서↔자기소개서 연결·어긋남 — [보류] 자기소개서를 쓰지 않아 동작하지 않음
  └─ evidence                 ③ 역량 → NCS 수행준거·KSA / ESCO 그래프 조인 (결정적)
  └─ verify_overlap           ④ LLM. 공고 역량 → 주장 전체 확인 (top-down, 공고 1건 호출 1번) — nodes/verify_overlap.py
  └─ compare                  ④ 선택 경로 + 확인 경로 → 겹침(출처) / 관련 경험 / 공고에만 / 지원자에만 / uncovered
                                 + 검증 후보 주장 점수 순 (결정적, nodes/compare.py)
  └─ draft_spec               ④ 공통 과제(공고 기준) + 경험 검증 과제(claims_to_verify)
  └─ approval                 ④ interrupt() — 사람 승인/반려/수정 요청
       ├─ approved  → finalize → END
       ├─ revise    → draft_spec (반려 사유를 State 에 넣고 재생성. 겹침 낮추기가 있으면 대조를 다시 계산)
       └─ rejected  → END
```

- `retrieve_t1` 과 `retrieve_t2` 는 서로 독립 → 병렬 분기 후 `evidence` 에서 합류.
- 지원자 문서 없이 공고만 들어오면 ② 와 ③ retrieve 를 건너뛰고 **evidence(공고 역량의 평가 근거)** → 공통 과제 (조건부 엣지).
  verify_overlap 은 주장이 없으면 아무것도 하지 않는다 (`overlap_check=None`).

## 대조: 두 경로 합치기 [확정: 2026-11-04 사용자, 선택만 일치 제외 2026-11-06 사용자]

- **선택 경로 (L0)**: retrieve_t1 — 주장 → 검색 상위 10 → 주장별 0~2개. 공고와 무관한 프로필·온톨로지 공백(uncovered)용.
  공고 역량은 검색 가산점만 (불변식 5 — 이 경로는 그대로).
- **확인 경로 (TD)**: verify_overlap — 공고 역량마다 그 이력서의 **주장 전체**(가명화본 주장 텍스트만, 불변식 2·3)를 보고
  action / related / mention / none + 근거 주장 id. 프롬프트에 '공고 역량' 이라는 사실은 알리지 않는다.
  - 답 검증: 목록 밖 id 버림 · 중복은 앞의 것 · 근거 주장 없는 판정은 none.
  - 다시 묻기 (공고 역량 40개 묶음 하나당 첫 호출 + 최대 2번): 답에서 **빠진 역량만** 한 번에 다시 묻고,
    묶음 호출이 **예외로 실패**(빈 응답·추론 토큰 소진·잘림·스키마 등)하면 그 묶음을 **반씩 나눠** 다시 묻는다 [2026-11-05 G안 —
    긴 이력서(주장 50개+)에서 첫 묶음이 통째로 실패한 사례]. 그래도 빠지거나 다시 묻기가 실패한 역량은 `status=partial`
    (그 역량은 '확인 안 됨' — 선택 경로만). **모든 호출이 실패했을 때만** `status=failed` → 선택 경로만 (예전 동작).
  - 판정 이유(LLM 자유 텍스트)는 State 에 남기지 않는다. provenance 는 호출마다.
- **합치는 규칙** (compare, 결정적) — 공고 역량 하나는 overlap / related / posting_only 중 하나에만:

| 확인 경로 | 선택 경로가 고름 | 결과 | 출처 (`overlap_source`) |
|---|---|---|---|
| action | 예 | 겹침 | `both` — 두 경로 일치 |
| action | 아니오 | 겹침 | `verify` — 공고 역량 확인 (근거 주장 표시) |
| related | 예 | 관련 경험 (겹침 아님), `select_only` 표시 | — |
| mention·none | 예 | 공고에만, `select_only` 표시 | — |
| related | 아니오 | 관련 경험 (겹침 아님) | — |
| mention·none | 아니오 | 공고에만 | — |
| 확인 안 함 (예전 실행·실패·빠짐) | 예 / 아니오 | 겹침 / 공고에만 | 없음 |
| 승인자가 내림 (`downgrade_overlap`) | — | 관련 경험 (`downgraded`) | — |
| 승인자가 올림 (`promote_overlap`, `select_only` 만) | 예 | 겹침 (`promoted`) | `promoted` — 승인자가 올림 |

- **선택만 일치(select_only)는 겹침이 아니다** [확정: 2026-11-06 사용자]. 근거: v2 e2e (25쌍 × 2회, 최종 정답)에서
  선택만 일치 출처의 action 정밀도 0.14–0.2. 빼고 다시 계산하면 운영 겹침 정밀도 0.733 → 0.827 · 0.719 → 0.830,
  재현율 0.901 → 0.878 · 0.939 → 0.893. 승인 화면에 "선택만 일치(확인 경로 근거 없음)" 표시 — 확인 필요.
- 출처 `select` 는 예전 규칙(2026-11-06 이전 실행)의 자동 겹침만 뜻한다 — 새 계산은 만들지 않는다. 예전 체크포인트를 열면
  승인 화면에 "선택만 일치(예전 실행) — 확인 필요"(위험색)로 보이고, 승인자가 **겹침 낮추기**로 내릴 수 있다 (테스트로 확인).
  revise 로 대조를 다시 계산하면 그 실행 전체가 새 규칙으로 자리 잡는다.

- 역할: 근거 주장의 칸 규칙 역할(assign_roles, 불변식 6) 중 가장 높은 것 — 확인 경로 근거가 있으면 그것, 없으면 선택 경로 근거.
- 검증 후보(claims_to_verify): 주장의 역량 = 선택 경로 역량 + 그 주장이 확인 경로 **action 근거**인 공고 역량 (승인자가 내린 것 제외).
  공고 주·보조 가산점·슬롯 배정·공통 과제 겹침 감점이 이 합집합으로 계산된다. 확인 경로로만 이어진 역량은 사유에 `(확인)`.
  선택만 일치 역량도 주장 → 역량 연결은 그대로라 그 주장은 검증 후보로 남는다 (확인 질문 대상) — 사유에 `선택만 일치(확인 경로 근거 없음): …`.
  확인 경로 근거가 된 uncovered 주장은 uncovered 에서 뺀다.
- 겹침 낮추기: 승인자가 `revise` + `downgrade_overlap: [공고 역량 id]` (revise_target 은 verification·all — 경험 검증을 다시 만들어야 하므로).
  draft_spec 이 대조를 다시 계산해 State 에 쓰고 낮춘 역량은 누적된다 (다음 revise 에서 다시 보내지 않아도 유지). 겹침이 아닌 id 는 무시 + `downgrade_ignored`.
  되돌리기 `restore_overlap` (낮춘 역량만, 아니면 `restore_ignored`).
- 겹침으로 올리기 [2026-11-06]: `revise` + `promote_overlap: [공고 역량 id]` — 같은 제약 (revise_target verification·all,
  최대 60개, id 1~120자, 낮추기·되돌리기 목록과 겹치면 거부). `select_only` 인 역량만 받고 그 밖은 `promote_ignored`.
  올린 역량은 `Comparison.promoted` 에 누적, 출처 `promoted`, 검증 후보 사유는 '승인자가 올림'(선택만 일치 문구 대신).
  `downgrade_overlap` 으로 내리면 올림만 취소 (원래 자리로, `downgraded` 에는 안 넣음) — 다시 올릴 수 있다.
  낮춘 역량을 올리라고 보내면 `promote_ignored` + "되돌리기(restore)를 먼저 쓰세요" (같은 결정에 같은 id 는 두 목록에 못 넣음).
  이미 올린 역량을 다시 올리라고 보내면 조용히 무시. 세 가지 모두 승인 interrupt resume 으로만 받는다 (불변식 9).
- 공통 과제는 이 결과로 바꾸지 않는다 (불변식 3).

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
    overlap_check: OverlapCheckResult | None = None   # ④ 확인 경로. None = 하지 않음 (예전 체크포인트도 읽힘)
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
| 기여한 점·담당업무 + 원문 인용에서 설계 표현(설계·아키텍처·구조 수립/정의) | 설계 (design) |
| 문제 정의 · 성과 · 사용 기술 · 칸 밖(자기소개서 산문 등) | 판정 안 함 (None) → 명세에서 '역할 확인 필요' |

주장마다 `field`(칸)·`role_basis`(근거)를 남긴다. 칸은 다음 칸 표시·빈 줄·절 제목·기간 머리말에서 끝난다.
설계 표현은 원문 대조를 거친 인용(`quote`)에서만 찾고, LLM 이 쓴 행동 요약(`action`)은 보지 않는다
[확정: 2026-11-18 사용자 #239] — 요약을 보면 같은 인용이 요약 문구에 따라 설계(검증 후보 가중치 4배)가 됐다.

## 과제 기술 환경 [확정: 2026-11-02 공통 과제 / 제안: 경험 검증 과제 — PR-4 에서 enables 데이터로 확정]

| 과제 트랙 | 기술 환경(언어·프레임워크) |
|---|---|
| 공통 과제 | **공고 언어만** — 지원자 문서와 무관 (불변식 3, 같은 공고 지원자 간 비교 가능성) |
| 경험 검증 과제 (`claims_to_verify`) | 지원자 `사용 기술` 칸 ∩ 공고 언어가 있으면 **공고 언어**, 없으면 지원자 역량의 **프레임워크**(온톨로지 `enables`: 도구 → 역량) |

입력: ingest 의 `사용 기술` 칸(가명화본) + ① 공고 프로필의 언어·도구. 판정은 규칙(온톨로지 도구 별칭 대조), LLM 아님.
draft_spec 은 선택한 기술 환경과 근거(겹친 언어 / 사용한 enables 관계)를 명세에 남긴다.

## 공통 과제 골격 [확정: 2026-11-02] (`spec/common.py`, 결정적)

- 역량: 공고 **주** 역량 중 수행준거가 있는 셀 범위 NCS 단위가 있는 것 2~3개. 순서 = 요구 수준(수행>활용>이해) → 담당업무 우선 → 공고 문장 수.
  1위와 근접·기반 관계인 역량을 먼저 묶는다. 부족하면 보조 역량으로 채우고 `review_flags`.
- 난이도: 최고 요구 수준 수행 → high, 활용 → medium, 그 밖 low. 제한 시간 기본 60분.
- 근거: 단위별 번갈아 수행준거 최대 12개·KSA 10개 (`ontology/queries.py criteria_detail`), id = `요소id#번호`.
- 함정·심층 구조: 부서 결정적 사건(`source_incident`)이 모일 때까지 LLM 제안 + 승인자 검토 (`review_flags` 고정 문구).
- 명세 LLM 판정 모델(평가용): gpt-4.1 (비교 대상 밖).

## 공통 과제 유형 [확정: 2026-11-06] (`spec/kind.py`, `nodes/spec.py choose_kind`)

- `task_kind`: incident(이슈 진단형) / feature(기능 개발형 — 1단계 구현 40분 → 1단계 제출 뒤 2단계 디버깅 20분, `debug_stage`).
- 우선순위: 승인자 수정 요청의 `task_kind`(revise, 대상 common·all) → 그 공고에서 가장 최근 승인된 판의 유형(`store.approved_kind`)
  → 기본값 규칙(직군 데이터 파일 `resources/task_kind/<sub>.json`, 주 역량 중 진단 > 만드는 일 때만 incident, 동률 feature, 파일 없는 직군 incident).
- 기본값 규칙의 feature 는 설정 `feature_task_default`(기본 false, 평가 뒤 켬)가 켜져 있고 `draft_feature_task` 프롬프트가 있을 때만 —
  아니면 incident + 명세 flag. 승인자 지정·승인된 판이 feature 면 늘 feature (프롬프트·LLM 이 없으면 골격만 + 과제 flag).
- 기능형 서술: `spec/feature.py draft_feature_task` (이슈형 `draft_task` 루프 재사용, 검사 `check_feature`·고쳐 쓰기·자료 섞기만 다름).
  확장 슬롯은 `draft_slot` 그대로 — 지원자에게 보이는 부분에 공개 요구·공개 수용 기준을 넣고, 언급 금지 개념에 2단계 원인 개념까지 넣는다.
- 유형이 다르면 저장 키 `prompt_ref` 가 달라 다른 판이다. 예전 판(유형 없음)은 incident. 설계: `docs/design/feature-task-backend.md`.

## 공통 과제 서술 [확정: 2026-11-03] (`spec/draft.py`, `prompts/draft_task`)

- 골격(`spec/build.py build_common`) 위에 제목·상황·제출물·심층 구조·표면·함정·관찰 행동·행동 기준(1·3·5)만 LLM 이 쓴다.
  입력은 직군·업종 라벨, 난이도, 시간, 기술 환경, 역량, 수행준거·KSA 본문 — **지원자 필드 없음** (불변식 3).
- 자동 검증: 행동 기준 인용 id ⊆ 입력 수행준거, 역량마다 1개 이상 인용, `catches_trap` ∈ trap_id,
  `tech_used`·본문 언어 ⊆ 기술 환경. 어긋나면 사유를 넣어 1회 재요청 → 그래도 어긋나면 잘못된 값만 걷어내고 **다시 검사**해
  `review_flags` 를 둘로 — 고쳐 쓰기가 푼 문제는 "코드가 고침"(`FLAG_DRAFT_REPAIRED`), 남은 문제만 "자동 검증 실패"(`FLAG_DRAFT_INVALID`)
  [2026-11-15 — 고친 명세를 지우기 전 문제로 실패로 셌다].
  LLM 실패 → 골격만 + `review_flags`.
- revise: 승인자 `comment` 를 `revision_note` 로 넣는다 (아래 탐지에 걸리면 보내지 않고 `review_flags`).
- 모델: `opencode:qwen3.8-flash` [확정: 2026-11-03 — test 순위 1.17/4, gpt-5.5 의 1/40 비용]. 근거 `prompts/draft_task/PROMOTIONS.md`.
- 모델 비교: `eval/spec/run_draft.py` (시범 공고 직군별 1건, 12건 dev/test, 판정 `judge_draft_task` gpt-4.1 이 이름 가린 초안 비교 채점·순위).
- 승인자 수정 요청은 가명화와 같은 로컬 탐지기(이름·학교·회사·연락처)에 하나라도 걸리거나 1000자를 넘으면 보내지 않는다.

## 공개 사례로 함정·원리 근거 [확정: 2026-11-03] (`incidents/public_v1/`, `spec/incidents.py`)

- 부서 결정적 사건이 모이기 전까지 직군마다 공개 사례 1건(사후 분석·기술 블로그·논문 요약, 출처 URL)을 쓴다. sw.game 보류.
- draft_task v2 입력 `incident` 에는 원리·증상·원인·흔한 처방·단서·표면만 — 조직명·제목·URL·원문 수치는 넣지 않는다.
- 자동 검증에 `avoid_terms`(원 사례 회사·제품·사건 이름) 노출 검사를 더한다. 명세 `source_incident` = 사례 id,
  `review_flags` 는 "부서 사례 없음" 대신 "공개 사례 기반 — 부서 사례 아님".
- `accepted_fixes`(정답으로 인정할 조치)도 입력에 넣는다 — 원 사례의 실제 조치(예: 근거 있는 풀 크기 산정)를 함정으로 적어
  맞는 답이 감점되는 것을 막는다. `notes`(큐레이터 메모)는 넣지 않는다. 형식 `[복구|재발 방지|진단] 조치 — 조건: … — 함정: …`
  (`incidents/public_v1/README.md`), `naive_fix` 는 정답형과 겹치지 않게 "근거 없이 / ~만" 으로 한정한다.
- 사례 문구·수치 복사 검사(`incident_copied`): 원문 표지(증상의 키=값·세 자리 이상 수 + `source_literals`) 2개 이상,
  증상·원인의 따옴표 문구, 증상·원인과 20자 이상 같은 글이 과제(자료 본문 포함)에 있으면 재요청 — 검색하면 원 사례·정답이 나온다.
  원문의 오류 문구·지표 값은 사례마다 `source_literals` 로 옮기고, 생성 입력 필드(증상·원인·단서·원리·표면)는 관계·일반식으로 요약한다
  (입력에 있으면 재요청해도 베낌, sw.backend).
- `avoid_terms` 는 자료 본문까지, 단어 경계로 찾는다 (부분 문자열이면 'ExP' 가 experiment 에 걸림).
- 공통 과제 저장 키 `prompt_ref` 에 사례 지문(`incident_ref` = 사례 id#내용 해시)을 붙인다 — 사례를 고치면 새 판.
- 과제 형태 `qa_mutation`(sw.qa): 요구사항 + 작은 모듈 + 커버리지 높고 단언 약한 테스트 → 채점은 비공개로 심은 결함 검출 수.
  실행 채점(돌연변이 실행·커버리지 측정)은 응시 플랫폼 몫 [미정].

## 지원서 사용 범위 — 이력서만 [확정: 2026-11-03 사용자]

- 검증할 주장은 **이력서에서만** 뽑는다. `USE_COVER_LETTERS=false`(기본): API 는 자기소개서를 저장하지 않고 응답 `ignored` 로 알리고,
  `ingest` 도 처리하지 않는다(`cover_letter_unused` 기록). 다시 쓰려면 설정만 켠다.
- 근거 (1단계 분석): 역할은 칸 규칙으로만 정하는데(불변식 6) 자기소개서에는 칸이 없어 역할 미판정 → 검증 점수에서 구조적으로 불리,
  같은 경험의 중복 서술이 많아 짝 짓기(T2)가 필요해짐. 이력서만 쓰면 T2 가 필요 없고 처리하는 개인정보도 준다.
- 잃는 것: 이력서가 짧은 신입·전환자는 검증 후보가 거의 없을 수 있다 — 공통 과제만으로 평가 (context.md §3).
- **T2(retrieve_t2) [보류]**: 노드와 compare 의 "어긋남 +2" 규칙은 남겨 두지만 동작하지 않는다. 자기소개서를 다시 쓸 때 재검토.
- 같은 분석에서 확인한 공백 (T2 와 무관): 주장 내용(행동·대상·결과·기간·문서·칸)이 명세·API 어디에도 나가지 않아
  승인자·면접관이 무엇을 검증하는지 모른다 — 인증과 묶어 "확인 대상 주장 요약"을 노출하는 작업이 필요 [미정].

## 공통 과제 v3 — 자료 점진 공개 + 과정 채점 [확정: 2026-11-03] (`prompts/draft_task/v3`, `spec/draft.py`)

- 근거: AI 단독 풀이 테스트(`eval/spec/run_solo.py`) — 자료를 다 준 v2 는 gpt-5.5 가 12개 중 11개를 풀고 모델 실력 간 점수 차가 없었다.
- 명세 `artifacts`(8~12개, key·support·distractor — 역할은 지원자에게 숨김) + `max_requests`(핵심 수 + 1, 자료 수의 3분의 1 이하).
  scenario 는 배경·겉 증상만. 지원자는 제목 목록을 보고 골라서 요청한 자료만 받는다.
- 자료는 **원시 로그·지표·설정·코드만** — 결론이 보이는 요약표·대조표·O/X·판정 문구 금지. 핵심 자료는 두 자료를 맞춰 봐야 원인이 드러나게.
- 자동 검사: 핵심 ≥1·방해 ≥3, 요청 한도, 제목의 결론 암시 단어(대조·비교·차이·불일치·vs·누락·원인·요약·결론), 핵심 자료 본문의 판정 문구, 핵심·보조 자료의 판정·요약 줄(`Pattern:`·`Conclusion:`·`결론:` 등 이름표 + 네 단어 이상 문장 — 설정 키 `pattern: "^…"` 는 제외), 핵심 자료가 상황에 미리 실림.
- 생성 뒤 `shuffle_artifacts` 로 순서를 섞고 번호를 다시 매긴다 (생성 모델이 핵심을 앞에 둔다). 본문·함정·행동 기준 속 자료 번호도 함께 바꾼다.
- 관찰 행동 `requests_artifact`(핵심 자료 요청), 행동 기준은 과정 중심 (가설 → 자료 선택 → AI 보다 먼저 핵심 개념). 제출물에 "자료 요청 순서와 이유".
- 한계: 가장 강한 모델은 v3 도 푼다 — 최종 답 채점으로는 못 막는다. 과정 채점·"AI 없는 첫 단계"는 응시 플랫폼 설계 [미정].

## 확장 슬롯 [확정: 2026-11-03 사용자] (`spec/slots.py`, `prompts/draft_slot`) — PR-A

- 공통 과제 위에서 이어지는 "추가로 구현·분석할 부분". **공고당 한 번** 공통 과제와 함께 만들어 저장·승인 (`common_task`) — 그 공고 지원자 모두 같은 본문.
  지원자마다 다른 것은 어떤 슬롯을 배정받는지와 면접 질문뿐 (배정·질문은 PR-B).
- 슬롯 = `TaskItem(track=extension_slot, task_id=s1…)`, 공통 과제 `slots` 에. 30분, v3 형식(원시 자료 4~8개·요청 한도 1~3·함정 1~2·과정 중심 행동 기준).
- 대상 고르기 (결정적): 근거(수행준거 있는 셀 NCS) 있는 공고 역량 중 **공통 과제가 안 다룬 역량 먼저**, 그다음 공통 과제 역량. 최대 4.
- 생성: 공통 과제와 같은 루프(`draft_task(task="draft_slot")`) — 같은 검증 + **공통 과제 정답 노출 검사**(공통 과제 first_mention_of·원 사례 원인 용어가 슬롯 본문에 나오면 재요청). 슬롯마다 동시에.
  실패한 슬롯은 빼고 공통 과제 review_flags. 입력에는 공통 과제의 보이는 부분(제목·상황·자료 이름)만 — 자료 본문·함정 제외.
- 저장 키 프롬프트 = `draft_task@N+draft_slot@M` (어느 쪽이 바뀌어도 새 판). 저장 시 슬롯 `parent_task_id` 를 공통 과제 저장 id 로.
- 승인 요약 `slots`.

## 경험 검증 = 슬롯 배정 + 면접 질문 [확정: 2026-11-03 사용자] (`spec/assign.py`, `spec/questions.py`, `prompts/slot_questions`) — PR-B

- 채점은 두 트랙 그대로: 공통 과제 본체 = 점수 비교 / 확장 슬롯 = 확인됨·의심됨 + 면접 질문 (비교 점수 제외).
- 지원자마다: 검증할 주장 고르기(`pick_targets` — 점수 순, 공통 과제 역량 −1.5, 역량 안 겹치게 최대 2) →
  **슬롯 배정(규칙)**: 승인자 지정(`reassign`) > 같은 역량 슬롯 > 근접·기반 관계 슬롯 > 없음. 한 슬롯은 한 주장. 근거(`basis`) 기록.
  → **면접 질문(LLM)**: 연결 문구(행동 요약, 지원자에게 보임) + 면접 확인 질문 2~3개. 입력은 가명화본에서 다시 뽑은 주장만.
  검사: 원문 베끼기(가명화 토큰·인용 수치·12자 겹침), 연결 문구 수치·과제 정답 개념. 1회 재요청.
- 명세 `slot_assignments`(모든 고른 주장 — 슬롯 없으면 면접 질문만) + `verification_tasks`(슬롯이 배정된 주장만:
  `slot_id`·`parent_task_id`·`link_text`·`follow_up_questions`, 시간·역량은 슬롯 것). 승인 전 슬롯이면 review_flags.
- revise: `revise_target` = common(공통 과제·슬롯 새 판, 배정 유지) / verification(명세의 공통 과제·슬롯 그대로 — 저장소에서 판을 다시 고르지 않음 #234,
  배정·질문만 다시 + `reassign` 반영) / all.
  `reassign {주장 id: 슬롯 id | null}` 은 revise 에서만.
- 지원자별 확장 문항 서술(`verify_task`)은 폐기 — 서면 문항이 경험을 가리지 못함 (AI 단독 풀이 테스트 22/22 수준 3 이상).

## 공고 단위 공통 과제 재사용 [확정: 2026-11-03] (`spec/store.py`, task_db `public.common_task`)

- 같은 공고 지원자는 같은 공통 과제를 받는다 (context.md §3). 키 = (posting_id, ontology_version, draft_task@N | skeleton).
- draft_spec: revise 가 아니면 저장소에서 **승인된 최신 판 → 없으면 최신 초안**을 재사용 (LLM 호출 없음, 저장된 provenance 를 State 에 싣는다).
  없으면 생성 후 초안으로 저장. task_id = 저장 id(`ct-…`). 명세 `common_refs` 에 판 번호·재사용 여부를 남긴다.
- revise(common·all): 재사용하지 않고 승인자 수정 요청으로 새 판(revision+1) 초안을 만든다.
  revise(verification): 저장소를 보지 않고 명세가 가리키는 판을 그대로 둔다 (LLM 호출 없음, `common_refs` 의 `reused`=true) —
  다시 고르면 그 사이 승인된 판·다른 실행의 새 판·배포로 바뀐 프롬프트의 새 판으로 바뀌었다 (#234).
- finalize: 그 판을 approved 로, 같은 키의 이전 approved 판은 superseded 로 (이미 그 판을 받은 지원자의 명세는 그대로).
- 승인 payload: `summary`(`schemas/summary.py ApprovalSummary` — 과제별 review_flags·시간 합계·공통 과제 판·대조 요약·검증 후보 claim_id·점수·사유) + `spec` + `provenance`.
  대조 요약의 `posting_checks` = 공고 역량마다 대조 결과·출처·확인 판정·근거 주장 id·역할·낮춤 여부 (`check_status` 함께).
  API `RunView.approval_summary` 로 노출. **주장 인용·판정 이유는 인증 전까지 넣지 않는다** — 근거는 주장 id 만 [제안: 인증 후 가명화 인용 추가].
- 저장소에는 지원자 실행과 연결되는 값(run_id 등)을 두지 않는다. 미승인 초안을 재사용하면 `review_flags` 로 표시.

## 노드 규칙

- 노드 = `async def node(state: RunState) -> dict`. 하나의 노드는 하나의 단계만.
- LLM 을 부르는 노드는 `prompting.assemble()` + `llm.call()` 만 쓴다 — 직접 모델 클라이언트 생성 금지.
  반환에 `provenance` 를 반드시 포함.
- `pseudonymize` 이전 노드(`posting_profile`, `ingest`)는 LLM·임베딩을 import 하지 않는다.
- 결정적 노드(`posting_profile`, `assign_roles`, `evidence`, `compare`)는 같은 입력 → 같은 출력. 테스트로 고정.

## 승인 (interrupt)

> 빌드(시작 코드) 승인은 실행 그래프가 아니라 **빌드 승인 그래프**(`graph/build_approval.py`, #130)다 — 같은 체크포인터,
> thread 는 (빌드, 결과)마다 `build-<build_id>-<결과 키>`. 결과가 바뀌면(수리) 옛 thread 의 resume 은 stale 로 끝난다.
> `finalize_build` 는 DB 승인만, 묶음 approved 다시 쓰기·평가 키트는 builder [확정: 2026-11-12 사용자].

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
- 승인 결정에는 `reviewer`, `at`, `comment` 를 남긴다 (명세 `approval` 필드, `docs/design/task-spec-schema.md`).
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
from psycopg_pool import AsyncConnectionPool

@asynccontextmanager
async def make_checkpointer(settings):
    if settings.checkpointer == "memory":
        yield InMemorySaver(); return
    pool = AsyncConnectionPool(url, open=False, check=AsyncConnectionPool.check_connection,
                               kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    await pool.open(wait=True)
    try:
        cp = AsyncPostgresSaver(pool)
        await cp.setup()          # 체크포인트 테이블 생성 (멱등)
        yield cp
    finally:
        await pool.close()
```

- 패키지: `langgraph-checkpoint-postgres`, `psycopg[binary,pool]`.
- **연결 하나(`from_conn_string`) 금지, 연결 풀** [확정: 2026-11-05]. SSE 연결이 끊기면 sse-starlette 가 anyio 취소 범위로
  조회를 끊고, psycopg 정리 대기까지 취소돼 연결이 `another command is already in progress` 로 남는다 — 연결 하나면
  재시작 전까지 실행 조회·헬스체크가 전부 실패한다(운영 2026-11-05). 풀은 그런 연결을 돌려받을 때 버린다.
  회귀 테스트: `tests/graph/test_postgres.py::test_cancelled_query_does_not_break_later_requests` (`-m pg`).
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
