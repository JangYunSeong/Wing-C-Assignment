# 과제 명세(TaskSpec) 스키마

> 상태: 2026-11-11. 결정 표기는 온톨로지 레포와 같다 — `[확정]` / `[제안]` / `[미정]`.  
> 이력: 온톨로지 레포 `context.md` §6 "과제 명세(spec) 스키마 초안 [제안]"(2026-10-28)을 P1 구현(`schemas/domain.py`·`approval.py`, `docs/design/feature-task-backend.md`)에 맞춰 개정하고(2026-11-11, P1 세션 대조: 명세 칸 11·과제 칸 35·기능형 7·`debug_stage` 8 일치, 지적 5건 반영), 원본을 P1으로 옮겼다 [확정: 2026-11-11 사용자].

명세 스키마의 **원본은 P1**이다 — 이 문서와 `src/task_spec/schemas/domain.py` [확정: 2026-11-11 사용자]. 칸을 더하거나 뜻을 바꾸는 결정은 이 문서에서 하고 코드와 함께 고친다.  
온톨로지 레포(`JangYunSeong/job-competency-ontology`) `context.md` §6은 연결만 적는다: 온톨로지가 주는 것은 버전 고정 산출물(역량·NCS·ESCO)이고, 명세의 `target_competencies`·`ncs_*`·`esco_uri`는 그 id를 쓰며, 명세는 `ontology_version`을 적는다.  
과제 코드를 만들려고 정한 **빌드용 스키마**(숨은 계획 `HiddenPlan`, 공개 계약 `TaskContract`, 재료 `FeatureSeed`)는 이 문서 밖이며 `schemas/build.py`·`schemas/incident.py`에서 정한다.

명세에는 **과제 내용뿐 아니라 채점 기준(Evidence Model)이 반드시 포함**되어야 한다 (온톨로지 `context.md` 참고문헌 R10·R11 — ECD).

```yaml
task_spec:
  spec_id: ...
  run_id: ...                          # P1 실행 id
  posting_id: ...
  ontology_version: ...                # 버전 고정 온톨로지 산출물
  revision: 1                          # 명세 판 번호
  common_tasks: [task, ...]            # 공고 공통 과제 — 공고 기준만으로 만든다, 같은 공고 지원자 모두 같음
  verification_tasks: [task, ...]      # 지원자별 경험 검증 과제 (claims_to_verify에서)
  common_refs:                         # 공통 과제가 공고 단위 저장소의 어느 판인지 (비교 가능성 추적)
    - {common_id, revision, status: draft|approved|superseded, reused: bool, prompt_ref}
  slot_assignments:                    # 검증할 주장 → 확장 슬롯 배정 (규칙, 승인자가 바꿀 수 있음)
    - {claim_id, competency_ids, slot_id|null, basis, link_text, follow_up_questions, review_flags, provenance_id}
  review_flags: [...]                  # 승인자가 꼭 볼 것
  approval:                            # 사람 승인 (LangGraph interrupt로만). 승인 전에는 비어 있다
    status: approved|revise|rejected
    reviewer: ...
    at: <시간대가 있는 시각>
    comment: ...                       # revise면 필수
    revise_target: common|verification|all
    # 아래는 모두 status=revise일 때만
    reassign: {claim_id: slot_id|null}       # 슬롯 배정 바꾸기 — 규칙보다 우선, revise_target이 verification·all일 때만 반영 (common이면 배정 유지 + reassign_ignored 기록)
    downgrade_overlap: [competency_id]       # 겹침 → 관련 경험으로 낮추기 (경험 검증 다시 만듦)
    restore_overlap: [competency_id]         # 낮춘 겹침 되돌리기
    promote_overlap: [competency_id]         # 선택 경로만 고른(select_only) 공고 역량을 겹침으로 올리기
    task_kind: incident|feature              # 공통 과제 유형 바꾸기 — revise_target이 common·all일 때만
    # 겹침 조정 3개는 revise_target이 verification·all일 때만, 같은 역량을 둘 이상에 넣을 수 없음

task:                                  # common_tasks·verification_tasks·slots의 한 항목
  task_id: ...
  track: common | claim_verification | extension_slot
  task_kind: incident | feature        # 공통 과제 유형. feature는 track=common에만 (슬롯·경험 검증 과제는 incident)
  parent_task_id: ...                  # 경험 검증 문항이 이어 쓰는 공통 과제
  slots: [task, ...]                   # 확장 슬롯 (최대 6, 각 항목 track=extension_slot). 공통 과제에만 둔다 [확정] — 스키마 검증은 아직 없음 [제안]
  slot_id: ...                         # 지원자별 확장 문항이 가리키는 슬롯
  link_text: "..."                     # 지원자에게 보이는 연결 문구 — 숫자·가명화 토큰·원문 베끼기·과제 정답 개념 없음 (생성 단계 검사)

  # Student model: 무엇을 측정하나
  target_competencies:                 # 온톨로지 역량 단위 (온톨로지 context.md §5.5)
    - competency_id: comp:...
      label: ...
      level: 수행|활용|이해|언급         # 공고 요구 수준
      esco_uri: ...                    # ESCO 대표가 있을 때만 (broad·공백이면 생략)
      ncs_unit_ids: [...]              # 셀(직군 × 업종) 안 능력단위
      ncs_criterion_ids: [...]         # 과제 근거로 쓴 수행준거 (요소 id#번호)
  ai_axes: [delegation, communication, verification, responsibility]
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
  variable: [domain_surface, numbers, ...]   # domain_surface는 포지션의 업종에서 온다 (온톨로지 context.md §4.4)
  traps:                               # 경험자만 걸리는 함정 (온톨로지 context.md §4.3)
    - {trap_id, description, why_ai_fails}
  artifacts:                           # 요청 시 공개 자료 (지원자에게는 목록만, 요청한 것만 내용 공개)
    - {artifact_id, title, content, role: key|support|distractor}   # role은 승인자·채점용 — LLM 이 빠뜨리면 support 로 받는다 (key·distractor 수는 검사가 본다)
  max_requests: 1~12 | null                 # 기능형은 2026-11-16 부터 null — 한도 없음 (analysis-stage.md §4.4). 예전 판·이슈형은 숫자
  source_incident: critical_incident_id      # 부서 결정적 사건 — 수집 전에는 비어 있음

  # 기능 개발형(task_kind=feature) 전용 — incident에서는 비어 있어야 한다
  requirements: [{req_id: r#, text}]         # 공개 요구 — '무엇을'만, 최대 6
  acceptance_public: [{ac_id, text, req_ids: [r#]}]   # 공개 수용 기준, 최대 6. req_ids = 확인하는 공식 요구 (예전 판은 비어 있음).
                                             # draft_feature_task v11 이 p# 와 함께 쓰고(출력 AcceptanceDraftV6), 그 전 판 명세는 문제 분석 생성기가 채운다.
                                             # v5 이하 생성기 출력 모델은 req_ids 없는 판(PublicAcceptance) — 출력 스키마가 프롬프트에 들어가므로
  acceptance_hidden:                          # 숨김 수용 기준 (채점용, 숨은 요구 하나에 연결), 최대 8
    - ac_id: h#
      trap_id: ...
      text: ...
      items: ["<테스트가 만들 항목>", ...]      # 실패 지정이 있을 때 2개 이상, 서로 다른 짧은 이름 (최대 8)
      faults:                                 # 실패 지정 — 순번이 아니라 항목 내용으로, 최대 4 (비면 없음)
        - {operation: "<실패시킬 동작, 공개 요구의 말>", item: "<items 중 하나>", times: 1~3, status: 400~599|null(연결 실패)}
                                              # 실패하지 않는 항목이 items 에 하나 이상 남아야 한다
  clarifications:                            # 물으면 공개할 추가 요구·제약, 최대 6
    - {clarification_id, topic, answer, answer_hint, reveals: [trap_id], match_terms, ask_when}
                                             # ask_when = 구체 답을 줄 질문 문장 (TaskClarification) — 문제 분석 생성기 v3 가 사실과 함께 써
                                             # 두 항목의 조건을 나눈다. 없으면(예전 판) 진행자가 match_terms 를 예시 표현으로 본다
  max_clarifications: 1~6 | null           # 기능형은 2026-11-16 부터 null (한도 없음)
  accepted_designs: [{design_id, summary, conditions, trap_ids}]   # 정답 설계 — 숨은 요구마다 2개 이상(생성 단계 검사), 최대 5
  debug_stage:                               # 2단계: 1단계 제출 뒤 '배포 후 운영 증상' → 진단·수정
    {reveal_after: stage1_submitted, time_limit_min: 5~120, scenario, trap_ids, artifacts, max_requests(null — 한도 없음), deliverables, observables}
  # 문제 분석 판 (analysis-stage.md §4·§6) — 요청 글로 시작해 요구를 진행자에게 물어 확인한다. 예전 판은 비어 있다.
  # id 사이 참조는 spec/analysis.py 가 본다: covers 는 있는 r#·p# 만, 모든 r#·p# 는 사실 하나 이상이 덮는다, req_ids·request_mentions 는 있는 r# 만,
  # p# 마다 req_ids 하나 이상, r# 마다 가리키는 p# 하나 이상, 요청 글과 사실은 함께, 배경 글 필수
  background: "<배경 글>"                    # README 상황 절 자리 — 회사·서비스·누가 무엇 때문에 곤란한가만, 공식 요구 없이 (1500자 이하).
                                             # 지금 상황 글(scenario)은 요구를 나열하므로 문제 분석 판은 이 글을 보인다 [확정: 2026-11-16 사용자]
  request: "<요청 글>"                       # README 요구 사항 절 자리 (1000자 이하)
  request_mentions: [r#]                     # 배경 글·요청 글이 이미 말한 공식 요구 → 채점에서 "보이는 것으로 앎"
  facts:                                     # 요청자 지식 — 물으면 주는 답 (승인 대상), 최대 12
    - {fact_id: f#, covers: [r#|p#], topic, ask_when: "<구체 답 조건 문장>", answer, answer_hint, match_terms}
                                             # 진행자 보기로는 covers·match_terms 를 빼고 넘긴다 (match_terms 는 명세 안 비교용)

  # Evidence model: 무엇을 보면 점수를 주나
  behavioral_anchors:                  # 수준별 행동 기준 — 각 기준에 근거 수행준거 id 필수
    level_1: {behavior, criterion_ids}
    level_3: {behavior, criterion_ids}
    level_5: {behavior, criterion_ids}
  observables:                         # 로그에서 관찰할 것
    - {kind: first_mention_of, value: "<핵심 원인 개념>"}
    - {kind: catches_trap, value: trap_id}
    - {kind: requests_artifact, value: artifact_id}
    - {kind: asks_clarification, value: clarification_id}
  follow_up_questions: [...]           # 면접 확인 질문 (확장 문항)

  # 운영
  review_flags: [...]
  provenance_id: ...                   # 프롬프트 id@버전+해시, 모델·설정, ontology_version
```

## 결정

| 항목                                                                                   | 상태                     | 출처                                                                                       |
| ------------------------------------------------------------------------------------ | ---------------------- | ---------------------------------------------------------------------------------------- |
| 명세 = 공통 과제 목록 + 경험 검증 과제 목록 (한 명세에 여러 과제)                                            | [제안]                 | P1 `TaskSpec` 구현. 공통 과제는 공고 기준만으로 (온톨로지 context.md §4.6, CLAUDE.md 불변식 3)                                       |
| 확장 슬롯 (`extension_slot`, 공통 과제에만, 공고당 한 번 생성·승인)                                     | [확정: 2026-11-03]     | P1 `TaskItem.slots`                                                                      |
| 주장 → 슬롯 배정 규칙 (`slot_assignments`, 맞는 슬롯 없으면 면접 질문만)                                 | [확정: 2026-11-03 사용자] | P1 `SlotAssignment`                                                                      |
| 경험 검증 문항이 공통 과제를 이어 씀 (`parent_task_id`)                                             | [제안: 2026-11-03]     | 온톨로지 context.md §3 두 트랙을 한 과제로                                                                           |
| 자료 점진 공개 (`artifacts`, `max_requests`)                                               | [제안: 2026-11-03]     | AI 단독 풀이 테스트에서 '자료 전부 제공' 과제가 풀림 (온톨로지 context.md §4.3)                                                  |
| 공통 과제 유형 `incident`·`feature` 둘 다 유지, 기능형 = 구현 → 디버깅 두 단계, 첫 대상 `sw.backend`         | [확정: 2026-11-06 사용자] | P1 `docs/design/feature-task-backend.md`                                                 |
| 추가 요구 두 층 답 (`answer` 구체 / `answer_hint` 모호, `match_terms`로 고름)                      | [확정: 2026-11-06 사용자] | P1 `Clarification`                                                                       |
| 디버깅 단계: 1단계에서 이미 대응했으면 '증상이 생기지 않는 근거'도 정답                                           | [확정: 2026-11-06 사용자] | P1 `DebugStage`                                                                          |
| 행동 기준마다 근거 수행준거 id 필수, `ncs_criterion_ids`                                           | [제안]                 | P1 구현. NCS 연결 단위(능력단위·요소·수행준거)는 온톨로지 context.md §10 열린 질문과 함께 정한다 — 이전 초안의 `ncs_element_ids` [미정]을 대신함 |
| `revise`는 대상(공통/검증/전체)과 슬롯 재배정을 함께 받음                                                | [제안]                 | P1 `ApprovalDecision`                                                                    |
| 승인자가 겹침을 낮추기·되돌리기 (`downgrade_overlap`·`restore_overlap`) — 겹침 = LLM 판정 + 규칙 합침 + 사람 | [확정: 2026-11-04]     | CLAUDE.md 불변식 5, `ApprovalDecision`                                                             |
| 선택 경로만 고른 공고 역량을 승인자가 겹침으로 올리기 (`promote_overlap`)                                   | [제안: 2026-11-06]     | P1 `ApprovalDecision` 주석 (태그 없이 날짜만)                                                     |
| revise에서 공통 과제 유형 바꾸기 (`approval.task_kind`, 대상 common·all)                          | [제안: 2026-11-06]     | P1 `ApprovalDecision` 주석 (태그 없이 날짜만)                                                     |
| 기능형(`task_kind=feature`)은 공통 과제(`track=common`)에만, 기능형 전용 칸은 incident에서 비어 있어야 함     | [확정: 2026-11-06 사용자] | P1 `TaskItem` 검증 규칙                                                                      |
| 확장 슬롯(`slots`)이 공통 과제(`track=common`)에만 있도록 스키마 검증 추가 | [제안] | 슬롯을 공통 과제에 둔다는 결정은 위 [확정: 2026-11-03] 행. 지금 P1 스키마는 최대 6개만 강제한다 — 이 행은 그 검증을 더하자는 제안
| 숨김 수용 기준의 실패 지정(`acceptance_hidden[].faults`) — 순번(처음 N건)이 아니라 항목 내용으로 정해 명세에 두고 사람이 승인한다. 과제 코드 단계는 계약 API·요청 칸에 옮기기만 하고 코드가 같은 값인지 대조 | [확정: 2026-11-11 사용자] | 재측정 오프라인: 순번 지정이 보내는 방식(하나씩/한꺼번에)마다 다르게 소비됨. 재료 `FeatureSeed.fault_by="item"` 이면 필수 |
| 실패 지정이 있는 숨김 수용 기준은 테스트가 만들 항목(`items`, 2개 이상)을 나열하고, 실패 항목은 그중 하나 — 실패하지 않는 항목이 남아야 한다 | [확정: 2026-11-11 사용자] | 재배정 명세(오프라인): item 을 '대기 중인 점검 메모'(항목 전체)로 적어 '그 항목만 실패'가 성립하지 않음 |
| 자료 `role` 이 빠진 LLM 답은 support 로 받는다 — 답 전체를 버리지 않고, 핵심·방해 자료 수는 자료 목록 검사가 판정 | [제안] | 명세 알파: 자료 하나의 role 누락으로 답 전체가 스키마 위반, 하루 5번 재요청 기회를 날림 |
| 연결 문구(`link_text`)에 숫자·가명화 토큰·원문 베끼기·정답 개념 없음                                        | [제안]                 | P1 생성 단계 검사 `spec/questions.py` (스키마 검증 아님), 실패 시 1회 재요청 후 기본 문구                         |
| 기능형의 자료·질문 한도 없음 — 새로 만드는 기능형은 `max_requests`·`max_clarifications`·`debug_stage.max_requests` 가 null, 상황 글에 한도 안내를 붙이지 않고 README 는 "제한이 없다"고 적음. 생성기 출력의 한도 칸은 예전 판 출력 스키마라 남기되 명세에 넣지 않음 | [확정: 2026-11-16 사용자] | `analysis-stage.md` §4.4 — 한도는 정보를 막지 못하고 묻는 형식만 벌함. 상한은 제한 시간 |
| 문제 분석 판 칸 — `background`·`request`·`request_mentions`·`facts`(`ask_when` 포함)·`acceptance_public[].req_ids`. 기능형 전용, 예전 판은 비어 있음. 참조 검사는 `spec/analysis.py` | [확정: 2026-11-16 사용자] | `analysis-stage.md` §4·§6 (스키마 PR, §12-3). 생성기는 다음 단계 |
| 공개 수용 기준 연결(`req_ids`)은 p# 를 쓰는 기능형 생성기(v11)가 함께 쓰고 "모든 r# 에 p# 하나 이상"을 재요청 사유로 검사. 추가 요구에 질문 문장 `clarifications[].ask_when`(명세 모델 `TaskClarification`) — 문제 분석 생성기 v3 가 씀 | [확정: 2026-11-16 사용자] | `analysis-stage.md` §5.1 — 흘림 판정·v2 평가 뒤 원인 수정 (r# 에 p# 없음, 사실 질문에 c# 가 열림) |


## 바꾸는 법

1. 이 문서와 `schemas/domain.py`(·`approval.py`)를 **같은 PR** 에서 고친다. 태그는 `[확정]`/`[제안]`/`[미정]`.
2. 저장된 명세(`task_db`)와 맞지 않게 되는 변경(칸 이름·형식 바꿈, 필수로 바꿈)은 저장소 읽기 호환을 함께 본다 (`spec/store.py`).
3. 온톨로지 산출물 id 를 쓰는 칸(`target_competencies`·`ncs_*`·`esco_uri`)의 뜻을 바꿀 때는 온톨로지 레포에 알린다 (그쪽 id 체계를 따른다).

---

## 온톨로지 `context.md` §6 초안(2026-10-28) 대비 바뀐 점

- **구조**: 명세 = 과제 1개(평평한 필드) → 명세 안에 공통 과제·경험 검증 과제 목록 + 공통 과제 판 참조 + 슬롯 배정.
- **새 필드**: `run_id`, `revision`, `track: extension_slot`, `task_kind`, `parent_task_id`, `slots`, `slot_id`, `link_text`, `tech_env`, `time_limit_min`, `title`, `scenario`, `deliverables`, `artifacts`, `max_requests`, `follow_up_questions`, 기능형 7개(`requirements`·`acceptance_public`·`acceptance_hidden`·`clarifications`·`max_clarifications`·`accepted_designs`·`debug_stage`), `review_flags`, `provenance_id`.
- **형식이 바뀐 필드**: `target_competencies`(+`label`·`level`, `ncs_element_ids` → `ncs_criterion_ids`), `traps`(id·설명·AI가 틀리는 이유), `observables`(`{kind, value}`, 종류 4개), `behavioral_anchors`(수준마다 근거 수행준거), `approval`(`pending` → `revise`, +`at`·`revise_target`·`reassign`·`downgrade_overlap`·`restore_overlap`·`promote_overlap`·`task_kind`).
- **이 절 밖으로 명시**: `HiddenPlan`·`TaskContract`·`FeatureSeed`(P1 빌드용).

