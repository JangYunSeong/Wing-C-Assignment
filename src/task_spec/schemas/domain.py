"""도메인 모델 (공고 프로필·주장·후보·대조·근거·비교·명세).

TODO(spec): 아직 내용이 정해지지 않았다. 골격 단계라 최소 필드만 둔다.
명세(TaskSpec) 스키마의 SSOT 는 온톨로지 레포 `context.md` §6 — 확정되면 그쪽에 맞춘다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.approval import ApprovalDecision
from task_spec.schemas.base import Schema

Role = Literal["participate", "lead", "design"]


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
    claim_ids: list[str] = Field(default_factory=list)
    # 공고 밖 역량일 때만: 직무 연계 강·중·약 (ontology/relatedness.py). None = 판정 데이터 없음
    relatedness: Literal["strong", "medium", "weak"] | None = None
    relatedness_reason: str | None = None


class VerifyTarget(Schema):
    """경험 검증 과제 후보 주장 (compare 의 결정적 점수 순)."""

    claim_id: str = Field(min_length=1)
    competency_ids: list[str] = Field(default_factory=list)
    role: Role | None = None
    score: float
    reasons: list[str] = Field(default_factory=list)


class Comparison(Schema):
    """④ 공고 역량 vs 지원자 주장에서 고른 역량 (nodes/compare.py, 결정적)."""

    overlap: list[CompetencyMatch] = Field(default_factory=list)
    posting_only: list[CompetencyMatch] = Field(default_factory=list)
    applicant_only: list[CompetencyMatch] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)  # 맞는 역량이 없는 주장 id
    claims_to_verify: list[VerifyTarget] = Field(default_factory=list)


class TaskItem(Schema):
    """명세 안의 과제 1건.

    TODO(spec): context.md §6 기준으로 필드 확정 (난이도·평가 기준 등).
    """

    task_id: str = Field(min_length=1)
    competency_ids: list[str] = Field(default_factory=list)
    provenance_id: str | None = None


class TaskSpec(Schema):
    """④ 과제 명세 초안/확정본.

    공통 과제는 공고 기준만으로 만든다 (불변식 3). claims_to_verify 는 경험 검증 과제 대상 주장 id.
    TODO(spec): context.md §6 스키마로 교체.
    """

    run_id: str = Field(min_length=1)
    posting_id: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    common_tasks: list[TaskItem] = Field(default_factory=list)
    verification_tasks: list[TaskItem] = Field(default_factory=list)
    claims_to_verify: list[str] = Field(default_factory=list)
    approval: ApprovalDecision | None = None  # finalize 가 승인 결정을 붙인다
