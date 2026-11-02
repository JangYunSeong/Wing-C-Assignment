"""④ 대조 → 명세 초안 → 사람 승인(interrupt) → 확정.

승인은 approval 노드의 interrupt() 로만 받는다 (불변식 9). 자동 승인 경로 없음.
TODO(spec-generation): draft_spec 는 prompting + llm 으로 연결.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from task_spec.schemas import ApprovalDecision, Comparison, RunState, TaskSpec

ApprovalRoute = Literal["finalize", "draft_spec", "__end__"]


async def compare(state: RunState) -> dict[str, Any]:
    posting = state.posting_profile.competency_ids if state.posting_profile else []
    return {"comparison": Comparison(posting_only=list(posting))}


async def draft_spec(state: RunState) -> dict[str, Any]:
    """공통 과제는 공고 기준만 (불변식 3). revise 로 돌아오면 revision 을 올린다."""
    revision = state.spec.revision + 1 if state.spec is not None else 1
    spec = TaskSpec(
        run_id=state.run_id,
        posting_id=state.posting_id,
        ontology_version=state.ontology_version,
        revision=revision,
    )
    return {"spec": spec, "approval": None}


async def approval(state: RunState) -> Command[ApprovalRoute]:
    """사람 승인. interrupt() 이전에 부작용을 두지 않는다 (resume 시 노드가 처음부터 다시 돈다).

    잘못된 결정이 들어오면 사유를 담아 다시 interrupt 한다.
    """
    if state.spec is None:
        raise RuntimeError("approval: spec 이 없습니다")
    payload: dict[str, Any] = {
        "run_id": state.run_id,
        "spec": state.spec.model_dump(mode="json"),
        "provenance": [p.model_dump(mode="json", by_alias=True) for p in state.provenance],
        "error": None,
    }
    while True:
        raw = interrupt(payload)
        try:
            decision = ApprovalDecision.model_validate(raw)
            break
        except ValidationError as exc:
            payload = {**payload, "error": _summarize(exc)}

    goto: dict[str, ApprovalRoute] = {
        "approved": "finalize",
        "revise": "draft_spec",
        "rejected": END,
    }
    return Command(update={"approval": decision}, goto=goto[decision.status])


async def finalize(state: RunState) -> dict[str, Any]:
    if state.spec is None or state.approval is None or state.approval.status != "approved":
        raise RuntimeError("finalize: 승인되지 않은 명세는 확정할 수 없습니다")
    return {"spec": state.spec.model_copy(update={"approval": state.approval})}


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'decision'}: {e['msg']}" for e in exc.errors()
    )
