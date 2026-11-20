"""채용담당자 인터뷰 과제 생성 — 생성 그래프의 State (#280).

명세 생성 과정과 같은 실행 방식: 그래프를 백그라운드로 돌리고(RunLauncher) 노드마다 체크포인트를 남기며, 화면은 현재 노드를 본다.
State 에 인터뷰·과제 본문을 싣지 않는다 (승인 그래프와 같음 — 체크포인트에 사본을 두지 않는다). 단계 산출물(재료 사례·과제 초안)은
서비스가 인터뷰 상태에 저장하고 노드는 id 만 넘긴다 — 끊긴 단계부터 이을 때도 거기서 읽는다.
thread 는 생성마다 하나 (`graph/interview_generate.thread_id`) — 인터뷰의 생성 표시가 이 thread 가 아니면 낡은 thread 라 멈춘다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from task_spec.schemas.interview_approval import INTERVIEW_ID

GenerateStep = Literal["seed", "feature", "request", "slots", "finish"]
# 노드 순서 — 재료 정리 → 기능형 초안 → 요청 글 → 확장 슬롯 → 질문·저장. 화면의 단계 목록도 이 순서
GENERATE_STEPS: tuple[GenerateStep, ...] = ("seed", "feature", "request", "slots", "finish")
InterviewGenerateResult = Literal["done", "stale"]


class InterviewGenerateState(BaseModel):
    """인터뷰 과제 생성 thread 하나의 State — thread 는 생성마다 하나."""

    model_config = ConfigDict(extra="forbid")

    interview_id: str = Field(pattern=INTERVIEW_ID)
    thread_id: str = Field(min_length=1, max_length=200)
    # done = 저장까지 끝남, stale = 인터뷰의 생성 표시가 다른 thread
    result: InterviewGenerateResult | None = None
