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


class PostingProfile(Schema):
    """① 공고 → 온톨로지 역량 프로필.

    TODO(ontology-artifact): 역량 가중치·NCS/ESCO 매핑 등 필드 확정.
    """

    posting_id: str = Field(min_length=1)
    ontology_version: str = Field(min_length=1)
    competency_ids: list[str] = Field(default_factory=list)


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
    role: Role | None = None
    provenance_id: str | None = None


class ClaimCandidates(Schema):
    """③ T1: 주장 → 역량 후보 (LLM 이 0~2개 선택).

    TODO(retrieve_t1): 점수·선택 근거 필드 확정.
    """

    claim_id: str = Field(min_length=1)
    competency_ids: list[str] = Field(default_factory=list, max_length=2)
    provenance_id: str | None = None


class CrossDocLink(Schema):
    """③ T2: 이력서 ↔ 자기소개서 연결·어긋남.

    TODO(retrieve_t2): 어긋남 유형(기간·역할·수치) 필드 확정.
    """

    resume_claim_id: str = Field(min_length=1)
    cover_letter_claim_id: str = Field(min_length=1)
    relation: Literal["consistent", "mismatch"]


class CompetencyEvidence(Schema):
    """③ 역량 → NCS 수행준거·KSA / ESCO 그래프 조인 결과 (결정적).

    TODO(evidence): KSA 구분·ESCO 관계 필드 확정.
    """

    competency_id: str = Field(min_length=1)
    ncs_criteria_ids: list[str] = Field(default_factory=list)
    esco_ids: list[str] = Field(default_factory=list)


class Comparison(Schema):
    """④ 공고 역량 vs 지원자 역량.

    TODO(compare): 항목별 근거 주장 id 연결.
    """

    overlap: list[str] = Field(default_factory=list)
    posting_only: list[str] = Field(default_factory=list)
    applicant_only: list[str] = Field(default_factory=list)
    uncovered: list[str] = Field(default_factory=list)


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
