"""사람 승인 결정 (pipeline-graph 스킬 "승인", docs/design/task-spec-schema.md approval 필드)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from task_spec.schemas.base import Schema

ApprovalStatus = Literal["approved", "revise", "rejected"]
CompetencyRef = Annotated[str, Field(min_length=1, max_length=120)]  # 승인자 입력 — 공고 역량 id
# 공통 과제 유형 [확정: 2026-11-06 사용자] — incident = 이슈 진단·해결형, feature = 기능 개발형(1단계 구현 → 2단계 디버깅).
# domain.py 가 이 모듈을 import 하므로 순환을 피해 여기에 둔다.
TaskKind = Literal["incident", "feature"]


class ApprovalDecision(Schema):
    """approval 노드의 interrupt() 에 resume 으로 들어오는 값.

    - reviewer 필수 (승인자 식별). 인증 전까지는 API 요청 본문에서 받는다 (fastapi-service).
    - at 은 시간대가 있는 시각만 받는다.
    - revise 는 재생성 지시가 필요하므로 comment 필수.
    """

    status: ApprovalStatus
    reviewer: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=4000)
    # revise 대상: common = 공고 공통 과제(새 판) / verification = 이 지원자의 확장 문항만 (공통 과제는 재사용) / all
    revise_target: Literal["common", "verification", "all"] = "all"
    # 슬롯 배정 바꾸기 (revise 일 때만): {주장 id: 슬롯 id 또는 null(슬롯 없이 면접 질문만)} — 규칙보다 우선
    reassign: dict[str, str | None] = Field(default_factory=dict)
    # 겹침 낮추기 (revise 일 때만): 공고 역량 id — 겹침을 관련 경험으로 내리고 경험 검증을 다시 만든다 [2026-11-04]
    downgrade_overlap: list[CompetencyRef] = Field(default_factory=list, max_length=60)
    # 낮춘 겹침 되돌리기 (revise 일 때만): 낮춘 역량 id — 다시 대조 규칙대로
    restore_overlap: list[CompetencyRef] = Field(default_factory=list, max_length=60)
    # 겹침으로 올리기 (revise 일 때만): 선택 경로만 고른(select_only) 공고 역량 id [2026-11-06]
    promote_overlap: list[CompetencyRef] = Field(default_factory=list, max_length=60)
    # 공통 과제 유형 바꾸기 (revise 일 때만, 공통 과제를 다시 만들 때만) — 그 공고의 공통 과제가 새 판이 된다 [2026-11-06]
    task_kind: TaskKind | None = None
    at: AwareDatetime

    @model_validator(mode="after")
    def _revise_needs_comment(self) -> ApprovalDecision:
        if self.status == "revise" and not (self.comment and self.comment.strip()):
            raise ValueError("revise 결정에는 수정 사유(comment)가 필요합니다")
        if not self.reviewer.strip():
            raise ValueError("reviewer 가 비어 있습니다")
        if self.reassign and self.status != "revise":
            raise ValueError("reassign(슬롯 배정 바꾸기)은 revise 결정에서만 쓸 수 있습니다")
        if self.task_kind is not None and self.status != "revise":
            raise ValueError("task_kind(과제 유형 바꾸기)는 revise 결정에서만 쓸 수 있습니다")
        if self.task_kind is not None and self.revise_target == "verification":
            raise ValueError(
                "task_kind(과제 유형 바꾸기)는 공통 과제를 다시 만들어야 합니다 — revise_target 을 common 또는 all 로"
            )
        for name, ids in (
            ("downgrade_overlap(겹침 낮추기)", self.downgrade_overlap),
            ("restore_overlap(낮춘 겹침 되돌리기)", self.restore_overlap),
            ("promote_overlap(겹침으로 올리기)", self.promote_overlap),
        ):
            if ids and self.status != "revise":
                raise ValueError(f"{name}은 revise 결정에서만 쓸 수 있습니다")
            if ids and self.revise_target == "common":
                raise ValueError(
                    f"{name}은 경험 검증을 다시 만들어야 합니다 — revise_target 을 verification 또는 all 로"
                )
        lists = (self.downgrade_overlap, self.restore_overlap, self.promote_overlap)
        if any(set(a) & set(b) for i, a in enumerate(lists) for b in lists[i + 1 :]):
            raise ValueError("같은 역량을 낮추기·되돌리기·올리기에 함께 넣을 수 없습니다")
        return self
