"""채용담당자 인터뷰 과제 승인 그래프 (#266 D2b, 결정 2 [확정: 2026-11-19 사용자]) — 빌드 승인·회사 사례 공유 그래프를 본뜬다.

    interview_approval(interrupt) ─ approved → finalize_interview → END
                                  └ rejected → reject_interview   → END
    finalize_interview ─ 교체 충돌·검사 실패 → interview_approval (사유를 붙여 다시 기다림)

- 결정 1 로 이 승인이 그 공고 지원자 전원의 공통 과제를 정한다 — 지원자 명세 승인과 같은 수준으로 불변식 9 를 따른다.
- thread 는 인터뷰 판본마다 하나 — `thread_id(interview_id, revision)`. 판본이 바뀌면 thread 가 달라져 옛 화면에서 누른 결정이
  새 판본에 적용되지 않는다. resume 하면 노드가 처음부터 다시 돌며 지금 판본·단계를 보고, 다르면 stale 로 끝낸다.
- interrupt() 앞에는 부작용을 두지 않는다 (저장소 읽기·검사만). 대기 내용에 인터뷰·과제 본문을 싣지 않는다.
- 승인할 수 없는 사유(problems)가 있으면 승인은 사유를 붙여 다시 기다리고, 반려는 받는다. 다시 기다릴 때는 노드를 새로 돌린다
  (한 노드 안에서 interrupt 를 되풀이하지 않는다 — resume 값 재생으로 옛 결정이 적용되지 않게).
- 확정·반려는 서비스가 인터뷰 잠금 안에서 한다 (interview/service.py — 확정은 세 표를 한 트랜잭션으로, 커밋 뒤 대응표 삭제).
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from task_spec.interview.approval import ApprovalOutcome
from task_spec.schemas import (
    InterviewApprovalDecision,
    InterviewApprovalResult,
    InterviewApprovalState,
)

Route = Literal["interview_approval", "finalize_interview", "reject_interview", "__end__"]
FinalizeRoute = Literal["interview_approval", "__end__"]
_END: Literal["__end__"] = "__end__"  # langgraph END (str 로만 알려져 있어 타입을 좁힌다)


class ApprovalService(Protocol):
    async def approval_context(self, interview_id: str, revision: int) -> dict[str, Any] | None: ...

    async def finalize_approval(
        self, interview_id: str, revision: int, decision: InterviewApprovalDecision
    ) -> ApprovalOutcome: ...

    async def reject_approval(
        self, interview_id: str, revision: int, decision: InterviewApprovalDecision
    ) -> InterviewApprovalResult: ...


def thread_id(interview_id: str, revision: int) -> str:
    return f"interview-{interview_id}-r{revision}"


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'decision'}: {e['msg']}" for e in exc.errors()
    )[:300]


def interview_approval_graph(
    service: ApprovalService, checkpointer: BaseCheckpointSaver
) -> CompiledStateGraph:
    async def interview_approval(state: InterviewApprovalState) -> Command[Route]:
        context = await service.approval_context(state.interview_id, state.revision)
        if context is None:
            return Command(update={"result": "stale"}, goto=_END)  # 판본·단계가 바뀜 — 낡은 thread
        raw = interrupt({**context, "error": state.error})
        # 받을 수 없는 결정은 이 노드를 새로 돌려 다시 기다린다 — 한 노드 안에서 interrupt 를 되풀이하면 resume 이 값 목록을
        # 처음부터 다시 재생해, 승인 불가 사유로 막혔던 '승인'이 사유가 풀린 뒤 다음 결정(예: 반려) 대신 적용된다 (code review C1)
        try:
            decision = InterviewApprovalDecision.model_validate(raw)
        except ValidationError as exc:
            return Command(
                update={"decision": None, "error": _summarize(exc)}, goto="interview_approval"
            )
        problems = [str(p) for p in context.get("problems", [])]
        if decision.status == "approved" and problems:
            return Command(
                update={
                    "decision": None,
                    "error": "승인할 수 없습니다: " + "; ".join(problems[:3]),
                },
                goto="interview_approval",
            )
        goto: Route = "finalize_interview" if decision.status == "approved" else "reject_interview"
        return Command(update={"decision": decision, "error": None}, goto=goto)

    async def finalize_interview(state: InterviewApprovalState) -> Command[FinalizeRoute]:
        d = state.decision
        assert d is not None
        outcome = await service.finalize_approval(state.interview_id, state.revision, d)
        if outcome.result is None:  # 교체 충돌·검사 실패 — 사유를 붙여 다시 기다린다
            return Command(
                update={"decision": None, "error": outcome.error}, goto="interview_approval"
            )
        return Command(update={"result": outcome.result, "common_id": outcome.common_id}, goto=_END)

    async def reject_interview(state: InterviewApprovalState) -> dict[str, Any]:
        d = state.decision
        assert d is not None
        return {"result": await service.reject_approval(state.interview_id, state.revision, d)}

    g = StateGraph(InterviewApprovalState)
    g.add_node("interview_approval", interview_approval)
    g.add_node("finalize_interview", finalize_interview)
    g.add_node("reject_interview", reject_interview)
    g.add_edge(START, "interview_approval")
    g.add_edge("reject_interview", END)
    return g.compile(checkpointer=checkpointer)
