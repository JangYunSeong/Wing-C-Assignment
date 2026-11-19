"""채용담당자 인터뷰 과제 승인 — 승인 그래프의 State 와 resume 값 (#266 D2b, 결정 2 [확정: 2026-11-19 사용자]).

승인은 그래프 interrupt 로만 받는다 (불변식 9) — API 는 thread 를 시작하고 resume 만 한다.
thread 는 인터뷰 판본마다 하나 (`graph/interview_approval.thread_id`) — 판본이 바뀌면 옛 화면의 결정은 적용되지 않는다.
State 에 인터뷰·과제 본문을 싣지 않는다 (체크포인트에 사본을 두지 않는다). 결정의 comment 는 API 가 인터뷰 대응표로
가명화한 글이다 — 반려 사유는 다음 과제 생성의 수정 요청(review_note)으로 LLM 에 간다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from task_spec.schemas.base import Schema

InterviewApprovalResult = Literal["approved", "rejected", "stale"]
INTERVIEW_ID = r"^int-[0-9a-f]{32}$"
COMMON_ID = r"^ct-[0-9a-f]{12}$"


class InterviewApprovalDecision(Schema):
    """interview_approval 노드의 interrupt() 에 resume 으로 들어오는 값."""

    status: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)  # TODO(auth): 인증 전까지 요청 본문에서
    at: datetime
    comment: str | None = Field(default=None, max_length=2000)  # 가명화한 글 — 반려면 필수
    # 화면이 보여 준 '지금 승인된 인터뷰 과제' — 그 공고에 이미 있으면 이 id 를 짚어야 바꾼다 (결정 1)
    replaces: str | None = Field(default=None, pattern=COMMON_ID)

    @model_validator(mode="after")
    def _check(self) -> InterviewApprovalDecision:
        if self.at.tzinfo is None:
            raise ValueError("at 은 시간대가 있는 시각이어야 한다")
        if not self.reviewer.strip():
            raise ValueError("reviewer 가 비어 있다")
        if self.status == "rejected" and not (self.comment or "").strip():
            raise ValueError("반려에는 사유(comment)가 필요하다")
        return self


class InterviewApprovalState(BaseModel):
    """인터뷰 승인 thread 하나의 State — thread 는 (인터뷰, 판본)마다 하나."""

    model_config = ConfigDict(extra="forbid")

    interview_id: str = Field(pattern=INTERVIEW_ID)
    revision: int = Field(ge=1)  # 이 thread 가 승인하는 인터뷰 판본
    decision: InterviewApprovalDecision | None = None
    result: InterviewApprovalResult | None = None
    common_id: str | None = None  # 승인한 공통 과제 (결과)
    error: str | None = None  # 다시 interrupt 할 때 승인자에게 보일 사유
