"""채용담당자 인터뷰 과제 생성 그래프 (#280) — 명세 생성 과정과 같은 실행 방식으로 과제 만들기를 백그라운드로 돌린다.

    seed(재료 정리) → feature(기능형 초안) → request(요청 글) → slots(확장 슬롯) → finish(질문·저장) → END

- 노드는 서비스의 단계 함수만 부른다. 입력을 모으고 생성기 단계를 돌려 산출물을 인터뷰 상태에 저장하는 일은 서비스가
  인터뷰 잠금 안에서 한다 (interview/service.py). State 에는 id 만 (schemas/interview_generate.py).
- 낡은 thread: 서비스가 False 를 돌려주면(인터뷰의 생성 표시가 이 thread 가 아님 — 다시 만들기·역량 바꾸기·폐기)
  result=stale 로 끝낸다.
- 단계가 실패하면 사유를 인터뷰 상태에 고정 문구로 남기고(generation_failed) 예외를 다시 던진다. 체크포인트의 다음 노드가
  실패한 단계로 남아, 같은 thread 를 입력 없이 다시 돌리면 그 단계부터 잇는다 (끝난 단계는 다시 부르지 않는다).
- thread 는 생성마다 하나 — `thread_id(interview_id, revision)` (생성 표시를 저장한 인터뷰 판본).
"""

from __future__ import annotations

import logging
from typing import Literal, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from task_spec.interview.generation import thread_id
from task_spec.schemas import GENERATE_STEPS, GenerateStep, InterviewGenerateState

__all__ = ["GenerateService", "interview_generate_graph", "thread_id"]

logger = logging.getLogger(__name__)

Route = Literal["seed", "feature", "request", "slots", "finish", "__end__"]
_END: Literal["__end__"] = "__end__"  # langgraph END (str 로만 알려져 있어 타입을 좁힌다)


class GenerateService(Protocol):
    async def generation_step(self, interview_id: str, thread_id: str, step: GenerateStep) -> bool:
        """단계 하나를 돌려 산출물을 저장한다. 생성 표시가 이 thread 가 아니면 아무것도 하지 않고 False."""
        ...

    async def generation_failed(
        self, interview_id: str, thread_id: str, step: GenerateStep, exc: Exception
    ) -> None:
        """실패 사유를 고정 문구로 인터뷰 상태에 남긴다 (예외 메시지를 그대로 싣지 않는다)."""
        ...


class _StepNode(Protocol):
    async def __call__(self, state: InterviewGenerateState) -> Command[Route]: ...


def interview_generate_graph(
    service: GenerateService, checkpointer: BaseCheckpointSaver
) -> CompiledStateGraph:
    def step_node(step: GenerateStep, after: Route) -> _StepNode:
        async def run(state: InterviewGenerateState) -> Command[Route]:
            try:
                current = await service.generation_step(state.interview_id, state.thread_id, step)
            except Exception as exc:
                try:
                    await service.generation_failed(state.interview_id, state.thread_id, step, exc)
                except Exception as record_exc:  # 사유를 못 남겨도 원래 실패를 가리지 않는다
                    logger.error(
                        "interview_generate failure not recorded step=%s exc=%s",
                        step,
                        type(record_exc).__name__,
                    )
                raise
            if not current:
                return Command(update={"result": "stale"}, goto=_END)
            if after == _END:
                return Command(update={"result": "done"}, goto=_END)
            return Command(goto=after)

        return run

    g = StateGraph(InterviewGenerateState)
    nexts: list[Route] = [*GENERATE_STEPS[1:], _END]
    for step, after in zip(GENERATE_STEPS, nexts, strict=True):
        g.add_node(step, step_node(step, after))
    g.add_edge(START, GENERATE_STEPS[0])
    return g.compile(checkpointer=checkpointer)
