"""회사 결정적 사건 공유 승인 — 사례 단위 승인 그래프의 State 와 resume 값 [확정: 2026-11-19 사용자].

채용담당자 인터뷰 등으로 모은 회사 사례는 등록(pending)만으로는 과제 생성에 쓰지 않는다. 사람이 공유를 승인한 사례(shared)만
**같은 회사** 공고의 공통 과제에 쓴다 (spec/company_incidents.py). 승인은 그래프 interrupt 로만 받는다 (불변식 9) —
API 는 thread 를 시작하고 resume 만 한다 (graph/incident_share.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from task_spec.schemas.base import Schema

IncidentShareResult = Literal["shared", "rejected", "stale"]


class IncidentShareDecision(Schema):
    """incident_share 노드의 interrupt() 에 resume 으로 들어오는 값."""

    status: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)  # TODO(auth): 인증 전까지 요청 본문에서
    at: datetime
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _check(self) -> IncidentShareDecision:
        if self.at.tzinfo is None:
            raise ValueError("at 은 시간대가 있는 시각이어야 한다")
        if not self.reviewer.strip():
            raise ValueError("reviewer 가 비어 있다")
        if self.status == "rejected" and not (self.comment or "").strip():
            raise ValueError("반려에는 사유(comment)가 필요하다")
        return self


class IncidentShareState(BaseModel):
    """공유 승인 thread 하나의 State — thread 는 (사례, 내용)마다 하나 (`graph/incident_share.thread_id`).

    사례 본문은 싣지 않는다 — 체크포인트에 회사 사례 사본을 두지 않는다 (화면은 GET /incidents/{id} 로 본다)."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(pattern=r"^co:[a-z_.]+:[a-z0-9-]+$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # 이 thread 가 승인하는 내용
    decision: IncidentShareDecision | None = None
    result: IncidentShareResult | None = None
    error: str | None = None  # 다시 interrupt 할 때 승인자에게 보일 사유
