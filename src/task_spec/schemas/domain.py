"""도메인 모델 (공고 프로필·주장·후보·대조·근거·비교·명세).

명세(TaskSpec) 스키마의 SSOT 는 `docs/design/task-spec-schema.md` — 칸을 넣거나 뜻을 바꿀 때는 그 문서와 이 파일을 같은 PR 에서 고친다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from task_spec.schemas.approval import ApprovalDecision, TaskKind
from task_spec.schemas.base import Schema

Role = Literal["participate", "lead", "design"]
# 공고 역량을 이력서가 얼마나 보여주는가 (judge_overlap 정답 판정·verify_overlap 운영 판정 공통)
OverlapLabel = Literal["action", "related", "mention", "none"]
# 겹침의 출처 [확정: 2026-11-04 사용자, 2026-11-06 변경]: both = 두 경로 일치(선택 ∩ 확인) / verify = 공고 역량 확인(top-down 만)
# / promoted = 승인자가 겹침으로 올린 선택만 일치 / select = 예전 규칙(2026-11-06 이전 실행)의 자동 겹침(선택만 일치) —
# 새 코드는 select 를 만들지 않는다 (예전 체크포인트 읽기용). 예전 select 겹침은 승인자가 겹침 낮추기로 내릴 수 있다.
OverlapSource = Literal["both", "verify", "promoted", "select"]


PostingSection = Literal["duties", "required", "preferred"]


class PostingCompetency(Schema):
    """공고 역량 1건. 주/보조 [확정: 2026-11-02]: 담당업무·자격요건에 나오면 주, 우대에만 나오면 보조."""

    competency_id: str = Field(min_length=1)
    label: str
    sections: list[PostingSection] = Field(default_factory=list)
    primary: bool
    levels: list[str] = Field(default_factory=list)  # 수행·활용·이해·언급
    line_ids: list[str] = Field(default_factory=list)  # 근거 공고 문장 (posting_line.id)


class PostingTool(Schema):
    tool_id: str = Field(min_length=1)
    label: str
    kind: str | None = None  # language · framework · platform · tool …
    family_id: str | None = None
    sections: list[PostingSection] = Field(default_factory=list)


class PostingProfile(Schema):
    """① 공고 → 온톨로지 역량 프로필 (결정적, ontology/queries.py).

    source: ontology = 온톨로지에 있는 포지션(검수됨) / provisional = 새 공고 임시 분석(미검수, 경로 2).
    """

    posting_id: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)
    source: Literal["ontology", "provisional"] = "ontology"
    company: str | None = None
    position_name: str | None = None
    sub: str | None = None  # 직군
    secondary_sub: str | None = None
    industry_id: str | None = None
    in_pilot: bool = False
    competencies: list[PostingCompetency] = Field(default_factory=list)
    tools: list[PostingTool] = Field(default_factory=list)

    @property
    def cell_id(self) -> str | None:
        return f"{self.sub}|{self.industry_id}" if self.sub and self.industry_id else None

    @property
    def competency_ids(self) -> list[str]:
        return [c.competency_id for c in self.competencies]

    @property
    def languages(self) -> list[str]:
        return [t.label for t in self.tools if t.kind == "language"]


class Claim(Schema):
    """② 가명화된 문서에서 뽑은 주장 1건. 텍스트 필드는 모두 가명화본 기준이다.

    role 은 assign_roles 가 칸 위치 규칙으로 채운다 (불변식 6). LLM 은 role 을 정하지 않는다.
    """

    claim_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    target: str | None = None
    result: str | None = None
    period: str | None = None
    quote: str = Field(min_length=1)
    field: str | None = None  # 인용이 속한 ⟦칸⟧ (FieldLabel). 칸 밖이면 None
    role: Role | None = None
    role_basis: str | None = None  # 판정 근거: "field:기여한 점", "+design_term", "no_field"
    provenance_id: str | None = None


class CandidateRef(Schema):
    """T1 검색 후보 1건 (결정적 검색 결과). ranks = 채널별 순위 (pattern·card·example)."""

    competency_id: str = Field(min_length=1)
    label: str
    score: float
    ranks: dict[str, int] = Field(default_factory=dict)
    in_scope: bool = True  # 지원 공고 직군 범위 안 (밖이면 점수 절반)
    posting: bool = False  # 공고 역량 (가산점을 받음)


class ClaimCandidates(Schema):
    """③ T1: 주장 → 역량 후보 상위 k (검색) → LLM 이 0~2개 선택 또는 uncovered.

    competency_ids 는 candidates 안에서만 고른다. 고른 게 없고 uncovered 면 온톨로지 공백 후보.
    """

    claim_id: str = Field(min_length=1)
    candidates: list[CandidateRef] = Field(default_factory=list)
    competency_ids: list[str] = Field(default_factory=list, max_length=2)
    uncovered: bool = False
    reason: str | None = None
    provenance_id: str | None = None


class CrossDocLink(Schema):
    """③ T2: 이력서 ↔ 자기소개서 연결·어긋남.

    TODO(retrieve_t2): 어긋남 유형(기간·역할·수치) 필드 확정.
    """

    resume_claim_id: str = Field(min_length=1)
    cover_letter_claim_id: str = Field(min_length=1)
    relation: Literal["consistent", "mismatch"]


class NcsUnitRef(Schema):
    unit_cl_cd: str
    subd_code: str | None = None
    name: str
    level: int | None = None
    # 셀 허용(primary) / 보조 직군 허용(secondary) / 셀에 NCS 목록이 없어 직군 단위로 대체(sub_fallback, 덜 정밀)
    scope: Literal["primary", "secondary", "sub_fallback"]
    rel: str | None = None
    n_elements: int = 0
    n_criteria: int = 0  # 수행준거 수 — 본문은 명세 생성 때 조회
    n_ksa: int = 0


class NcsCriterion(Schema):
    """NCS 수행준거 1건 (명세 근거). criterion_id = '요소id#번호'."""

    criterion_id: str
    unit_cl_cd: str
    unit_name: str
    element_name: str
    text: str


class NcsKsa(Schema):
    ksa_id: str  # '요소id#종류번호'
    unit_cl_cd: str
    kind: str  # 지식·기술·태도
    text: str


class EscoRef(Schema):
    uri: str
    label: str | None = None
    rel: str | None = None


class CompetencyEvidence(Schema):
    """③ 역량 → 셀 범위 NCS 능력단위(수행준거·KSA 건수) / 대표 ESCO (결정적).

    셀 밖 NCS 는 넣지 않는다 (온톨로지 v_position_competency_ncs 와 같은 규칙).
    """

    competency_id: str = Field(min_length=1)
    ncs_units: list[NcsUnitRef] = Field(default_factory=list)
    esco: list[EscoRef] = Field(default_factory=list)


class CompetencyMatch(Schema):
    """④ 대조 항목 1건: 역량 + 공고 여부 + 근거 주장 id."""

    competency_id: str = Field(min_length=1)
    label: str
    in_posting: bool
    primary: bool | None = None  # 공고 역량일 때만: 주(담당업무·자격요건) / 보조(우대)
    claim_ids: list[str] = Field(default_factory=list)  # 선택 경로(주장 → 역량)의 근거 주장
    # 공고 밖 역량일 때만: 직무 연계 강·중·약 (ontology/relatedness.py). None = 판정 데이터 없음
    relatedness: Literal["strong", "medium", "weak"] | None = None
    relatedness_reason: str | None = None
    # 공고 역량일 때만 — top-down 확인(verify_overlap). check_label None = 확인 안 함(예전 실행·실패·빠짐)
    check_label: OverlapLabel | None = None
    check_claim_ids: list[str] = Field(default_factory=list)  # 확인 경로의 근거 주장
    overlap_source: OverlapSource | None = None  # 겹침일 때만
    role: Role | None = None  # 근거 주장의 칸 규칙 역할 중 가장 높은 것 (불변식 6)
    downgraded: bool = False  # 승인자가 겹침 → 관련 경험으로 내림
    # 선택 경로만 고르고 확인 경로는 action 이 아님 (확인 related → 관련 경험, none·mention → 공고에만) [2026-11-06]
    select_only: bool = False
    promoted: bool = False  # 승인자가 select_only 역량을 겹침으로 올림 (출처 promoted)


class VerifyTarget(Schema):
    """경험 검증 과제 후보 주장 (compare 의 결정적 점수 순)."""

    claim_id: str = Field(min_length=1)
    competency_ids: list[str] = Field(default_factory=list)
    role: Role | None = None
    score: float
    reasons: list[str] = Field(default_factory=list)


class Comparison(Schema):
    """④ 공고 역량 vs 지원자 주장 (nodes/compare.py, 결정적 — top-down 확인 결과를 규칙으로 합친다).

    공고 역량은 overlap / related / posting_only 중 하나에만 들어간다.
    check_status None = top-down 확인을 하지 않은 실행 (예전 체크포인트 포함) — 겹침은 선택 경로만.
    """

    overlap: list[CompetencyMatch] = Field(default_factory=list)
    related: list[CompetencyMatch] = Field(default_factory=list)  # 관련 경험 (겹침 아님)
    posting_only: list[CompetencyMatch] = Field(default_factory=list)
    applicant_only: list[CompetencyMatch] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)  # 맞는 역량이 없는 주장 id
    claims_to_verify: list[VerifyTarget] = Field(default_factory=list)
    check_status: Literal["ok", "partial", "failed"] | None = None
    downgraded: list[str] = Field(default_factory=list)  # 승인자가 관련 경험으로 내린 공고 역량 id
    promoted: list[str] = Field(
        default_factory=list
    )  # 승인자가 겹침으로 올린 select_only 공고 역량 id


AiAxis = Literal["delegation", "communication", "verification", "responsibility"]
AI_AXES: tuple[AiAxis, ...] = ("delegation", "communication", "verification", "responsibility")
Track = Literal["common", "claim_verification", "extension_slot"]
Difficulty = Literal["high", "medium", "low"]


class TargetCompetency(Schema):
    """측정 대상 역량 1건 (task-spec-schema.md target_competencies). 결정적으로 채운다."""

    competency_id: str = Field(min_length=1)
    label: str
    level: str | None = None  # 공고 요구 수준 (수행·활용·이해·언급)
    esco_uri: str | None = None  # 대표 ESCO 가 있을 때만
    ncs_unit_ids: list[str] = Field(default_factory=list)  # 셀 범위 능력단위
    ncs_criterion_ids: list[str] = Field(
        default_factory=list
    )  # 과제 근거로 쓴 수행준거 (요소id#번호)


class Anchor(Schema):
    """수준별 행동 기준 1개 — 근거 수행준거 id 가 반드시 있다."""

    behavior: str = Field(min_length=1)
    criterion_ids: list[str] = Field(min_length=1)


class BehavioralAnchors(Schema):
    level_1: Anchor
    level_3: Anchor
    level_5: Anchor


class Trap(Schema):
    """경험자만 걸리는 함정 (context.md §4.3). 부서 사례가 없으면 LLM 제안 — 승인자 검토 필요."""

    trap_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    why_ai_fails: str = Field(min_length=1)  # 일반적인 AI 답이 그럴듯하지만 틀리는 이유


ARTIFACT_MAX = 3000
ARTIFACT_TRIMMED = "… (자료가 길어 코드가 줄 단위로 줄임)"


class Artifact(Schema):
    """요청 시 공개하는 자료 1건 (자료 점진 공개) [제안: 2026-11-03 — AI 단독 풀이 테스트에서 '자료 전부 제공' 과제가 풀림].

    지원자에게는 처음에 title(목록)만 보이고, 요청한 것만 content 가 공개된다. role 은 승인자·채점용 (지원자에게 숨김).
    """

    artifact_id: str = Field(pattern=r"^a\d{1,2}$")
    title: str = Field(min_length=1, max_length=80)  # 목록에 보이는 이름 — 원인을 드러내지 않음
    content: str = Field(min_length=1, max_length=ARTIFACT_MAX)
    role: Literal["key", "support", "distractor"]

    @model_validator(mode="before")
    @classmethod
    def _missing_role_is_support(cls, data: object) -> object:
        """role 이 빠진 LLM 답은 support 로 받는다 — 하나만 빠져도 답 전체가 스키마 위반으로 버려지고 재요청 기회까지 날아갔다
        (2026-11-11 명세 알파, 하루 5번). 출력 스키마에는 그대로 필수로 보인다. key·distractor 가 모자라면 자료 목록 검사가 다시 묻는다."""
        if isinstance(data, dict) and data.get("role") in (None, ""):
            return {**data, "role": "support"}
        return data

    @field_validator("content", mode="before")
    @classmethod
    def _fit_length(cls, v: object) -> object:
        """상한을 넘으면 줄 단위로 잘라 맞추고 표시를 남긴다 (명세 생성이 승인 화면에 '사람 확인'으로 올린다).
        2026-11-10 갱신 손실 재료: 명세 생성이 요청 기록·대조표 자료를 3000자 넘게 써 스키마 위반이 네 번 반복 —
        로그·표는 줄을 줄여도 의미가 남는다 (사용자 결정: 코드가 줄이고 사람 확인)."""
        if not isinstance(v, str) or len(v) <= ARTIFACT_MAX:
            return v
        room = ARTIFACT_MAX - len(ARTIFACT_TRIMMED) - 1
        cut = v.rfind("\n", 0, room)
        return v[: cut if cut > room // 2 else room].rstrip() + "\n" + ARTIFACT_TRIMMED


class Observable(Schema):
    """로그에서 관찰할 것 (first_mention_of 개념 / catches_trap 함정 id / requests_artifact 자료 id /
    asks_clarification 추가 요구 id — 기능형에서 숨은 요구에 닿는 질문)."""

    kind: Literal["first_mention_of", "catches_trap", "requests_artifact", "asks_clarification"]
    value: str = Field(min_length=1)


# ── 기능 개발형(feature) 공통 과제 [확정: 2026-11-06 사용자 — 1단계 구현 → 2단계 디버깅] ──
# 설계: docs/design/feature-task-backend.md. id 사이 참조(trap_id 등)는 생성 검증(spec/feature.py)이 본다 —
# 스키마는 형식만 (재요청 후 고쳐 쓴 초안도 저장·승인 화면에 올라가야 하므로).


class Requirement(Schema):
    """공개 기능 요구 1건 (지원자에게 보임). '무엇을'만 — 구현 방법·숨은 요구를 쓰지 않는다."""

    req_id: str = Field(pattern=r"^r\d{1,2}$")
    text: str = Field(min_length=1, max_length=300)


class PublicAcceptance(Schema):
    """공개 수용 기준 1건 (지원자에게 보임) — 관찰 가능한 문장."""

    ac_id: str = Field(pattern=r"^p\d{1,2}$")
    text: str = Field(min_length=1, max_length=300)


class HiddenFault(Schema):
    """숨김 수용 기준의 실패 지정 — 채점 때 하네스가 어느 요청을 실패시키는지, **순번이 아니라 항목 내용으로** (업무 말).
    [확정: 2026-11-11 사용자] 명세에 두고 사람이 승인한다 — 과제 코드 단계(plan_contract)는 이것을 계약 API·요청 칸에 옮기기만 하고
    코드가 같은 값인지 대조한다. 2026-11-11 재측정 오프라인: '처음 2건 503' 같은 순번 지정이 하나씩 보내는 허용 설계와 한꺼번에 보내는
    구현에서 다르게 소비돼 판정이 갈렸다."""

    operation: str = Field(
        min_length=1, max_length=80
    )  # 실패시킬 동작 — 공개 요구의 말로 (예: 점검 메모 저장)
    item: str = Field(min_length=1, max_length=80)  # 실패할 항목을 가리키는 내용 값 (예: 메모B)
    times: int = Field(default=1, ge=1, le=3)  # 그 항목의 처음 몇 번
    status: int | None = Field(default=None, ge=400, le=599)  # 오류 응답 상태 — 없으면 연결 실패

    @field_validator("status", mode="before")
    @classmethod
    def _no_status_is_network_failure(cls, v: object) -> object:
        """400 미만·빈 값(0·"")은 '연결 실패'(None)로 받는다 — LLM 이 연결 실패를 0 으로 적어 스키마 위반으로 재시도를 날렸다
        (2026-11-11 오프라인 명세 v8)."""
        if v in (None, "", 0) or (isinstance(v, int) and not isinstance(v, bool) and v < 400):
            return None
        return v


class HiddenAcceptance(Schema):
    """숨김 수용 기준 1건 (채점용) — 숨은 요구(trap) 하나에 연결."""

    ac_id: str = Field(pattern=r"^h\d{1,2}$")
    trap_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=400)
    # 테스트가 만들 항목 (실패 지정이 있을 때 2개 이상, 서로 다른 짧은 이름 — 예: 메모A·메모B·메모C). 실패 지정의 item 은 이 중 하나이고
    # 실패하지 않는 항목이 하나 이상 있어야 '그 항목만 실패'가 성립한다. 2026-11-11 재배정 명세(오프라인): item 을 '대기 중인 점검 메모'처럼
    # 항목 전체로 적어 사실상 '모든 메모의 처음 2번 실패'가 됐다.
    items: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        default_factory=list, max_length=8
    )
    faults: list[HiddenFault] = Field(default_factory=list, max_length=4)  # 실패 지정 (비면 없음)


class Clarification(Schema):
    """지원자가 물으면 공개할 추가 요구·제약 1건. topic·match_terms 는 질문 매칭용(승인자·진행자만).

    두 층 답 [확정: 2026-11-06 사용자]: 진행자(응시 플랫폼)는 질문에 match_terms 중 하나라도 있으면 answer(구체),
    없으면 answer_hint(모호)를 준다 — 둘 다 승인 대상, 보낼 때 문장을 바꾸지 않는다 (고르는 것만, 결정적).
    answer_hint 가 없는 예전 판은 늘 answer 를 준다.
    """

    clarification_id: str = Field(pattern=r"^c\d{1,2}$")
    topic: str = Field(min_length=1, max_length=120)
    answer: str = Field(min_length=1, max_length=600)  # 구체적인 답 — 정확히 짚은 질문에
    reveals: list[str] = Field(default_factory=list)  # 이 답이 단서가 되는 trap_id
    answer_hint: str | None = Field(default=None, max_length=400)  # 모호한 답 — 일반 질문에
    match_terms: list[str] = Field(default_factory=list, max_length=8)  # 구체 답을 줄 질문 낱말


class AcceptedDesign(Schema):
    """숨은 요구를 만족하는 설계 1건 — 한 가지 정답으로 감점하지 않게 2개 이상 둔다."""

    design_id: str = Field(pattern=r"^d\d{1,2}$")
    summary: str = Field(min_length=1, max_length=300)
    conditions: str = Field(min_length=1, max_length=600)  # 성립 조건·흔한 함정
    trap_ids: list[str] = Field(min_length=1)


class DebugStage(Schema):
    """기능형 2단계 — 1단계(구현) 제출 뒤 공개하는 '배포 후 운영 증상' + 자료 → 진단·수정.

    증상은 숨은 요구(trap_ids)를 놓친 구현에서 생기는 것. 1단계에서 이미 대응한 지원자는
    '이 증상이 자기 구현에서 생기지 않는 근거'를 내면 정답으로 본다 [확정: 2026-11-06 사용자].
    자료 id 는 1단계 자료와 겹치지 않는다 (같은 a 번호 공간).
    """

    reveal_after: Literal["stage1_submitted"] = "stage1_submitted"
    time_limit_min: int = Field(ge=5, le=120)
    scenario: str = Field(min_length=1, max_length=2000)  # 겉 증상만 — 원인 개념을 쓰지 않는다
    trap_ids: list[str] = Field(min_length=1)
    artifacts: list[Artifact] = Field(default_factory=list)
    max_requests: int | None = Field(default=None, ge=1, le=12)
    deliverables: list[str] = Field(min_length=1, max_length=4)
    observables: list[Observable] = Field(default_factory=list)


FEATURE_FIELDS = (
    "requirements",
    "acceptance_public",
    "acceptance_hidden",
    "clarifications",
    "max_clarifications",
    "accepted_designs",
    "debug_stage",
)


class TechEnv(Schema):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    basis: str  # 근거: "공고 언어" / "지원자 사용 기술 ∩ 공고 언어" / "enables: …"


class TaskItem(Schema):
    """명세 안의 과제 1건 (task-spec-schema.md). 결정적 필드 + LLM 필드(draft_task) + 검토 표시."""

    task_id: str = Field(min_length=1)
    track: Track
    # 공통 과제 유형 — 필드가 없는 예전 판은 incident 로 읽는다 (하위 호환). 슬롯·경험 검증 과제는 incident
    task_kind: TaskKind = "incident"
    target_competencies: list[TargetCompetency] = Field(min_length=1)
    ai_axes: list[AiAxis] = Field(default_factory=lambda: list(AI_AXES))
    claims_to_verify: list[str] = Field(default_factory=list)  # claim_verification 트랙일 때만
    # 경험 검증 확장 문항이 붙는 공통 과제 (같은 상황을 이어 씀) [제안: 2026-11-03 — context §3 두 트랙을 한 과제로 냄]
    parent_task_id: str | None = None
    # 확장 슬롯 [확정: 2026-11-03] — 공통 과제(track=common)에만. 공고당 한 번 생성·승인, 그 공고 지원자 모두 같음.
    # 슬롯도 TaskItem(track=extension_slot, task_id=s1…) — 지원자별 확장 문항은 slot_id 로 가리킨다 (PR-B)
    slots: list[TaskItem] = Field(default_factory=list, max_length=6)
    slot_id: str | None = None
    # 지원자에게 보이는 연결 문구 (확장 문항) — "지원서에 적은 '…' 경험과 관련됩니다". 수치·회사·가명 토큰 없음
    link_text: str | None = None
    tech_env: TechEnv
    difficulty_target: Difficulty
    time_limit_min: int = Field(ge=10, le=240)
    constraints: list[str] = Field(default_factory=lambda: ["no_pii"])
    # LLM 이 채우는 것 (draft_task). 결정적 단계만 끝난 초안이면 None
    title: str | None = None
    scenario: str | None = None
    deliverables: list[str] = Field(default_factory=list)
    invariant: str | None = None  # 유지할 심층 구조
    variable: list[str] = Field(default_factory=list)  # 바꿀 표면 (domain_surface, numbers …)
    traps: list[Trap] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(
        default_factory=list
    )  # 요청 시 공개 자료 (비면 자료가 scenario 안에 다 있음)
    max_requests: int | None = Field(default=None, ge=1, le=12)  # 지원자가 요청할 수 있는 자료 수
    observables: list[Observable] = Field(default_factory=list)
    behavioral_anchors: BehavioralAnchors | None = None
    follow_up_questions: list[str] = Field(
        default_factory=list
    )  # 면접 확인 질문 (확장 문항) [제안]
    source_incident: str | None = None  # 부서 결정적 사건 id — 수집 전에는 None
    # 기능형(feature) 전용 — LLM(draft_feature_task)이 채운다. incident 에서는 비어 있어야 한다
    requirements: list[Requirement] = Field(default_factory=list, max_length=6)
    acceptance_public: list[PublicAcceptance] = Field(default_factory=list, max_length=6)
    acceptance_hidden: list[HiddenAcceptance] = Field(default_factory=list, max_length=8)
    clarifications: list[Clarification] = Field(default_factory=list, max_length=6)
    max_clarifications: int | None = Field(default=None, ge=1, le=6)
    accepted_designs: list[AcceptedDesign] = Field(default_factory=list, max_length=5)
    debug_stage: DebugStage | None = None
    review_flags: list[str] = Field(default_factory=list)  # 승인자가 꼭 볼 것
    provenance_id: str | None = None

    @model_validator(mode="after")
    def _feature_fields_only_for_feature(self) -> TaskItem:
        if self.task_kind != "feature":
            used = [f for f in FEATURE_FIELDS if getattr(self, f) not in (None, [])]
            if used:
                raise ValueError(f"기능형 전용 필드는 task_kind=feature 일 때만: {', '.join(used)}")
        if self.task_kind == "feature" and self.track != "common":
            raise ValueError("task_kind=feature 는 공통 과제(track=common)에만")
        return self


class CommonRef(Schema):
    """공통 과제가 공고 단위 저장소의 어느 판인지 (spec/store.py). 같은 공고 지원자 비교 가능성 추적용."""

    common_id: str = Field(min_length=1)  # = 그 공통 과제의 task_id
    revision: int = Field(ge=1)
    status: Literal["draft", "approved", "superseded"]  # 이 명세를 만들 때의 상태
    reused: bool  # True = 이전 실행이 만든 판을 재사용 (LLM 호출 없음)
    prompt_ref: str  # draft_task@N 또는 skeleton


class SlotAssignment(Schema):
    """검증할 주장 1건 → 확장 슬롯 배정 (규칙) [확정: 2026-11-03 사용자]. 슬롯이 없으면 면접 질문만."""

    claim_id: str = Field(min_length=1)
    competency_ids: list[str] = Field(default_factory=list)
    slot_id: str | None = None  # 공통 과제 slots 의 task_id (s1…). None = 맞는 슬롯 없음
    basis: str  # "역량 같음: comp:x" / "근접·기반 관계: comp:x ↔ comp:y" / "승인자 지정" / "맞는 슬롯 없음"
    link_text: str | None = None
    follow_up_questions: list[str] = Field(default_factory=list)  # 면접관용 (지원자마다 LLM)
    review_flags: list[str] = Field(default_factory=list)
    provenance_id: str | None = None


class TaskSpec(Schema):
    """④ 과제 명세 초안/확정본 (task-spec-schema.md).

    공통 과제는 공고 기준만으로 만든다 (불변식 3). 경험 검증 과제는 compare 의 claims_to_verify 에서.
    """

    spec_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    posting_id: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    common_tasks: list[TaskItem] = Field(default_factory=list)
    verification_tasks: list[TaskItem] = Field(default_factory=list)
    common_refs: list[CommonRef] = Field(default_factory=list)
    slot_assignments: list[SlotAssignment] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)
    approval: ApprovalDecision | None = None  # finalize 가 승인 결정을 붙인다
