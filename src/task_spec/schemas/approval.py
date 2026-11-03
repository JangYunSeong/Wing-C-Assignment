"""사람 승인 결정 (pipeline-graph 스킬 "승인", context.md §6 approval 필드)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from task_spec.schemas.base import Schema

ApprovalStatus = Literal["approved", "revise", "rejected"]


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
    at: AwareDatetime

    @model_validator(mode="after")
    def _revise_needs_comment(self) -> ApprovalDecision:
        if self.status == "revise" and not (self.comment and self.comment.strip()):
            raise ValueError("revise 결정에는 수정 사유(comment)가 필요합니다")
        if not self.reviewer.strip():
            raise ValueError("reviewer 가 비어 있습니다")
        return self
