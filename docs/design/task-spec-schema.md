# 과제 명세(task_spec) 스키마

> **이 문서가 과제 명세 스키마의 원본(SSOT)이다** [확정: 2026-11-11 사용자 — 원본을 온톨로지 레포 `context.md` §6 에서 P1 으로 옮김].
> 코드 구현은 `src/task_spec/schemas/domain.py`(TaskSpec·TaskItem 등)·`approval.py`(ApprovalDecision) — 칸을 넣거나 뜻을 바꿀 때는
> **이 문서와 코드를 같은 PR 에서** 고친다.
> 근거: 온톨로지 레포 `context.md` §6 개정안(2026-11-11, P1 main `f7b770f` 와 칸 단위 대조 — 명세 칸 11개·과제 칸 35개 일치,
> 승인 칸 5개 등 누락 5건 보충). 태그·날짜는 코드 주석과 설계 문서(`feature-task-backend.md`)에 적힌 것을 옮겼다.
> 결정 표기는 온톨로지 레포와 같다 — `[확정]` / `[제안]` / `[미정]`.

## 1. 경계

- **P1 이 정한다:** 명세의 구조·칸·검증 규칙 (이 문서). 명세를 만들고(파이프라인)·저장하고(`task_db`)·승인받는(API·interrupt) 곳이 모두 P1 이다.
- **온톨로지 레포가 준다:** 버전 고정 산출물(`onto-vYYYY.MM.DD`) — 역량·NCS(능력단위·요소·수행준거)·ESCO. 명세의
  `target_competencies`·`ncs_unit_ids`·`ncs_criterion_ids`·`esco_uri` 는 그 산출물의 id 를 쓴다. 모든 명세에 `ontology_version` 을 적는다
  (CLAUDE.md 불변식 4). 이 id 체계가 바뀌면 온톨로지 레포 쪽 결정을 따른다.
- **이 문서 밖 (P1 빌드용, 과제 코드를 만들 때만):** 숨은 계획 `HiddenPlan`·공개 계약 `TaskContract`·재료 `FeatureSeed`
  (`schemas/build.py`·`schemas/incident.py`, `docs/design/spec-to-code.md`). 지원자·승인자에게 보이는 명세가 아니라 생성기 입력이다.
- 명세에는 과제 내용뿐 아니라 채점 기준(Evidence Model)이 반드시 들어간다 [R10][R11].

## 2. 스키마

```yaml
task_spec:
  spec_id: ...
  run_id: ...                          # P1 실행 id (LangGraph thread_id)
  posting_id: ...
  ontology_version: ...                # 버전 고정 온톨로지 산출물
  revision: 1                          # 명세 판 번호 (1 이상)
  common_tasks: [task, ...]            # 공고 공통 과제 — 공고 기준만으로 만든다, 같은 공고 지원자 모두 같음
  verification_tasks: [task, ...]      # 지원자별 경험 검증 과제 (claims_to_verify 에서)
  common_refs:                         # 공통 과제가 공고 단위 저장소(spec/store.py)의 어느 판인지 (비교 가능성 추적)
    - {common_id, revision, status: draft|approved|superseded, reused: bool, prompt_ref}
  slot_assignments:                    # 검증할 주장 → 확장 슬롯 배정 (규칙, 승인자가 바꿀 수 있음)
    - {claim_id, competency_ids, slot_id|null, basis, link_text, follow_up_questions, review_flags, provenance_id}
  review_flags: [...]                  # 승인자가 꼭 볼 것
  approval:                            # 사람 승인 — LangGraph interrupt() 로만 받는다 (불변식 9)
    status: approved|revise|rejected
    reviewer: ...                      # 필수
    comment: ...                       # revise 면 필수
    revise_target: common|verification|all          # 기본 all
    reassign: {claim_id: slot_id|null}               # 슬롯 배정 바꾸기 — revise 일 때만, 규칙보다 우선
    downgrade_overlap: [competency_id]               # 겹침 낮추기 — revise·대상 verification|all 일 때만
    restore_overlap: [competency_id]                 # 낮춘 겹침 되돌리기 — 같은 조건
    promote_overlap: [competency_id]                 # 선택 경로만 고른 역량을 겹침으로 올리기 — 같은 조건
    task_kind: incident|feature|null                 # 공통 과제 유형 바꾸기 — revise·대상 common|all 일 때만
    at: <시간대가 있는 시각>
    # 같은 역량을 낮추기·되돌리기·올리기에 함께 넣을 수 없다. 세 목록은 각 60개까지.

task:                                  # common_tasks·verification_tasks·slots 의 한 항목
  task_id: ...
  track: common | claim_verification | extension_slot
  task_kind: incident | feature        # 기능형은 공통 과제(track=common)에만 — 슬롯·경험 검증 과제는 incident
  parent_task_id: ...                  # 경험 검증 문항이 이어 쓰는 공통 과제
  slots: [task, ...]                   # 확장 슬롯 (track=extension_slot), 최대 6 — '공통 과제에만'은 스키마가 강제하지 않음
  slot_id: ...                         # 지원자별 확장 문항이 가리키는 슬롯
  link_text: "..."                     # 지원자에게 보이는 연결 문구 (검사는 생성 단계 spec/questions.py: 수치·지원서 원문·정답 개념 없음)

  # Student model: 무엇을 측정하나
  target_competencies:                 # 온톨로지 역량 단위 (§1 경계)
    - competency_id: comp:...
      label: ...
      level: 수행|활용|이해|언급         # 공고 요구 수준
      esco_uri: ...                    # ESCO 대표가 있을 때만 (broad·공백이면 생략)
      ncs_unit_ids: [...]              # 셀(직군 × 업종) 안 능력단위
      ncs_criterion_ids: [...]         # 과제 근거로 쓴 수행준거 (요소 id#번호)
  ai_axes: [delegation, communication, verification, responsibility]   # 기본 넷 모두
  claims_to_verify: [...]              # claim_verification 트랙일 때만

  # Task model: 어떤 조건에서 측정하나
  tech_env: {languages, frameworks, basis}
  difficulty_target: high|medium|low
  time_limit_min: 10~240
  constraints: [no_pii, ...]
  title: ...
  scenario: ...
  deliverables: [...]
  invariant: "<유지할 심층 구조>"
  variable: [domain_surface, numbers, ...]   # domain_surface 는 포지션의 업종에서 온다
  traps:                               # 경험자만 걸리는 함정
    - {trap_id, description, why_ai_fails}
  artifacts:                           # 요청 시 공개 자료 (지원자에게는 목록만, 요청한 것만 내용 공개)
    - {artifact_id, title, content, role: key|support|distractor}   # role 은 승인자·채점용
  max_requests: 1~12
  source_incident: critical_incident_id      # 부서 결정적 사건 — 수집 전에는 비어 있음

  # 기능 개발형(task_kind=feature) 전용 — incident 에서는 비어 있어야 한다 (스키마 검증)
  requirements: [{req_id: r#, text}]          # 공개 요구 — '무엇을'만, 최대 6
  acceptance_public: [{ac_id, text}]          # 공개 수용 기준, 최대 6
  acceptance_hidden: [{ac_id: h#, trap_id, text}]   # 숨김 수용 기준 (채점용, 숨은 요구 하나에 연결), 최대 8
  clarifications:                             # 물으면 공개할 추가 요구·제약, 최대 6
    - {clarification_id, topic, answer, answer_hint, reveals: [trap_id], match_terms}
  max_clarifications: 1~6
  accepted_designs: [{design_id, summary, conditions, trap_ids}]   # 정답 설계 — 숨은 요구마다 2개 이상(생성 단계), 최대 5
  debug_stage:                                # 2단계: 1단계 제출 뒤 '배포 후 운영 증상' → 진단·수정
    {reveal_after: stage1_submitted, time_limit_min: 5~120, scenario, trap_ids, artifacts, max_requests, deliverables, observables}

  # Evidence model: 무엇을 보면 점수를 주나
  behavioral_anchors:                  # 수준별 행동 기준 — 각 기준에 근거 수행준거 id 필수 (1개 이상)
    level_1: {behavior, criterion_ids}
    level_3: {behavior, criterion_ids}
    level_5: {behavior, criterion_ids}
  observables:                         # 로그에서 관찰할 것
    - {kind: first_mention_of, value: "<핵심 원인 개념>"}
    - {kind: catches_trap, value: trap_id}
    - {kind: requests_artifact, value: artifact_id}
    - {kind: asks_clarification, value: clarification_id}   # 기능형은 clarifications 안에서만
  follow_up_questions: [...]           # 면접 확인 질문 (확장 문항)

  # 운영
  review_flags: [...]
  provenance_id: ...                   # 프롬프트 id@버전+해시, 모델·설정, ontology_version (불변식 8)
```

## 3. 결정

| 항목 | 상태 | 출처 |
|---|---|---|
| 명세 스키마의 원본은 P1 (이 문서 + `schemas/domain.py`). 온톨로지 레포 `context.md` §6 은 경계만 | [확정: 2026-11-11 사용자] | 명세를 만들고·저장하고·승인하는 곳이 P1 |
| 명세 = 공통 과제 목록 + 경험 검증 과제 목록 (한 명세에 여러 과제) | [제안] | TaskSpec 구현. 공통 과제는 공고 기준만으로 (불변식 3) |
| 확장 슬롯 (extension_slot, 공통 과제에만, 공고당 한 번 생성·승인) | [확정: 2026-11-03] | TaskItem.slots — '공통 과제에만'은 스키마 검증 없음 (강제하려면 P1 에 검증 추가) |
| 주장 → 슬롯 배정 규칙 (slot_assignments, 맞는 슬롯 없으면 면접 질문만) | [확정: 2026-11-03 사용자] | SlotAssignment |
| 경험 검증 문항이 공통 과제를 이어 씀 (parent_task_id) | [제안: 2026-11-03] | 두 트랙을 한 과제로 |
| 자료 점진 공개 (artifacts, max_requests) | [제안: 2026-11-03] | AI 단독 풀이 테스트에서 '자료 전부 제공' 과제가 풀림 |
| 공통 과제 유형 incident·feature 둘 다 유지, 기능형 = 구현 → 디버깅 두 단계, 첫 대상 sw.backend | [확정: 2026-11-06 사용자] | `docs/design/feature-task-backend.md` |
| 기능형은 공통 과제(track=common)에만 | [확정: 2026-11-06 사용자] | TaskItem 검증 규칙 |
| 추가 요구 두 층 답 (answer 구체 / answer_hint 모호, match_terms 로 고름) | [확정: 2026-11-06 사용자] | Clarification |
| 디버깅 단계: 1단계에서 이미 대응했으면 '증상이 생기지 않는 근거'도 정답 | [확정: 2026-11-06 사용자] | DebugStage |
| 행동 기준마다 근거 수행준거 id 필수, ncs_criterion_ids | [제안] | NCS 연결 단위(능력단위·요소·수행준거)는 온톨로지 쪽 열린 질문과 함께 정한다 |
| revise 는 대상(공통/검증/전체)과 슬롯 재배정을 함께 받음 | [제안] | ApprovalDecision |
| 승인자가 겹침 낮추기·되돌리기 (downgrade_overlap·restore_overlap) | [확정: 2026-11-04] | CLAUDE.md 불변식 5 — 겹침 = LLM 판정 + 규칙 합침 + 사람 |
| 승인자가 선택 경로만 고른 역량을 겹침으로 올리기 (promote_overlap) | [제안: 2026-11-06] | ApprovalDecision 주석 |
| revise 에서 공통 과제 유형 바꾸기 (task_kind) — 공통 과제가 새 판이 된다 | [제안: 2026-11-06] | ApprovalDecision 주석 |
| link_text: 수치·지원서 원문·정답 개념 없음 | [제안] | 생성 단계 검사 `spec/questions.py` (스키마 검증 아님) |

## 4. 바꾸는 법

1. 이 문서의 §2·§3 과 `schemas/domain.py`(·`approval.py`)를 **같은 PR** 에서 고친다. 태그는 `[확정]`/`[제안]`/`[미정]`.
2. 저장된 명세(`task_db`)와 맞지 않게 되는 변경(칸 이름·형식 바꿈·필수로 바꿈)은 저장소 읽기 호환을 함께 본다 (`spec/store.py`).
3. 온톨로지 산출물 id 를 쓰는 칸(target_competencies 등)의 뜻을 바꿀 때는 온톨로지 레포에 알린다 (그쪽 id 체계를 따른다).
