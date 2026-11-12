"""빌드(시작 코드) 승인 — 빌드 단위 승인 그래프의 State 와 resume 값 (#130).

명세 승인(실행 그래프, thread = run_id)과 따로다. builder 가 review 까지 만든 빌드를 사람이 검토 표시마다 판단하고 승인·반려한다.
승인은 그래프 interrupt 로만 받는다 (불변식 9) — API 는 thread 를 시작하고 resume 만 한다.
묶음 파일을 approved 로 다시 쓰고 평가 키트를 만드는 일은 builder 가 한다 [확정: 2026-11-12 사용자, #130 ③ A].
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.package import BUILD_ID, ReviewDecision

BuildApprovalResult = Literal["approved", "rejected", "stale"]


class BuildApprovalDecision(Schema):
    """build_approval 노드의 interrupt() 에 resume 으로 들어오는 값."""

    status: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)  # TODO(auth): 인증 전까지 요청 본문에서
    at: datetime
    decisions: list[ReviewDecision] = Field(
        default_factory=list
    )  # 검토 표시마다 판단 (승인에 필요)
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _check(self) -> BuildApprovalDecision:
        if self.at.tzinfo is None:
            raise ValueError("at 은 시간대가 있는 시각이어야 한다")
        if self.status == "rejected" and not (self.comment or "").strip():
            raise ValueError("반려에는 사유(comment)가 필요하다")
        return self


class BuildApprovalState(BaseModel):
    """빌드 승인 thread 하나의 State — thread 는 (빌드, 결과)마다 하나 (`graph/build_approval.thread_id`)."""

    model_config = ConfigDict(extra="forbid")

    build_id: str = Field(pattern=BUILD_ID)
    outcome_key: str = Field(
        pattern=r"^[0-9a-f]{12}$"
    )  # 이 thread 가 승인하는 결과 (tree·grader 해시)
    decision: BuildApprovalDecision | None = None
    result: BuildApprovalResult | None = None
    error: str | None = None  # 다시 interrupt 할 때 승인자에게 보일 사유
