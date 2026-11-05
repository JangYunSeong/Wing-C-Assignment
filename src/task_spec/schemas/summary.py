"""승인자가 먼저 볼 요약 (approval interrupt payload.summary, API RunView.approval_summary).

필드를 모델로 고정한다 — 지원자 유래 자유 텍스트가 슬그머니 들어오지 않게 (privacy-auditor W2).
주장 인용(quote)은 인증이 붙기 전까지 넣지 않는다 (W1) [제안: 인증 후 가명화 인용 추가].
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema
from task_spec.schemas.domain import (
    CommonRef,
    Difficulty,
    OverlapLabel,
    OverlapSource,
    Role,
    Track,
)


class TaskBrief(Schema):
    task_id: str
    track: Track
    title: str | None = None
    competencies: list[str] = Field(default_factory=list)  # 온톨로지 역량 라벨
    difficulty: Difficulty
    time_limit_min: int
    n_traps: int
    has_anchors: bool
    review_flags: list[str] = Field(default_factory=list)  # 고정 문구·온톨로지 값


class OutsideBrief(Schema):
    label: str  # 온톨로지 역량 라벨
    relatedness: Literal["strong", "medium", "weak"] | None = None


class VerifyBrief(Schema):
    claim_id: str
    score: float
    reasons: list[str] = Field(
        default_factory=list
    )  # compare 규칙 문구 + 온톨로지 라벨 + 역할 근거 코드


class PostingCheckBrief(Schema):
    """공고 역량 1건의 대조 결과. 근거는 주장 id 만 — 인용·판정 이유는 인증 전까지 넣지 않는다 (W1·W2)."""

    competency_id: str
    label: str  # 온톨로지 역량 라벨
    primary: bool | None = None
    status: Literal["overlap", "related", "posting_only"]
    source: OverlapSource | None = None  # 겹침일 때: both·verify·select (None = 확인 안 함)
    check_label: OverlapLabel | None = None  # top-down 확인 판정 (None = 확인 안 함)
    claim_ids: list[str] = Field(default_factory=list)  # 근거 주장 id (확인 경로 우선)
    role: Role | None = None  # 근거 주장의 칸 규칙 역할
    downgraded: bool = False  # 승인자가 관련 경험으로 내림
    select_only: bool = (
        False  # 선택 경로만 (확인 경로 근거 없음) — 겹침 아님, 확인 필요 [2026-11-06]
    )
    promoted: bool = False  # 승인자가 겹침으로 올림


class ComparisonBrief(Schema):
    overlap: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)  # 관련 경험 (겹침 아님)
    posting_only: list[str] = Field(default_factory=list)
    applicant_only: list[OutsideBrief] = Field(default_factory=list)
    uncovered_claims: int = 0
    claims_to_verify: list[VerifyBrief] = Field(default_factory=list)
    # top-down 확인: ok·partial·failed, None = 하지 않음 (예전 실행 포함)
    check_status: Literal["ok", "partial", "failed"] | None = None
    posting_checks: list[PostingCheckBrief] = Field(default_factory=list)  # 공고 역량 순서


class AssignmentBrief(Schema):
    claim_id: str
    slot_id: str | None = None  # None = 맞는 슬롯 없음 (면접 질문만)
    basis: str  # 배정 근거 (규칙 문구·온톨로지 id)
    n_questions: int = 0
    review_flags: list[str] = Field(default_factory=list)


class ApprovalSummary(Schema):
    review_flags: list[str] = Field(default_factory=list)
    tasks: list[TaskBrief] = Field(default_factory=list)
    # 공통 과제의 확장 슬롯 (공고 단위 — 승인하면 공통 과제와 함께 승인됨)
    slots: list[TaskBrief] = Field(default_factory=list)
    # 지원자별 슬롯 배정 — 바꾸려면 revise + reassign {주장 id: 슬롯 id | null}
    assignments: list[AssignmentBrief] = Field(default_factory=list)
    common: list[CommonRef] = Field(default_factory=list)
    total_time_min: int = 0
    comparison: ComparisonBrief | None = None
