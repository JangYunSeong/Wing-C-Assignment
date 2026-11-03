"""④ 명세 초안 (대조는 nodes/compare.py) → 사람 승인(interrupt) → 확정.

승인은 approval 노드의 interrupt() 로만 받는다 (불변식 9). 자동 승인 경로 없음.
공통 과제: 결정적 골격(spec/common.py) → 서술(spec/draft.py, draft_task LLM). 입력은 공고·온톨로지만.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langgraph.graph import END
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from task_spec.llm import LLMClient
from task_spec.ontology.relatedness import Relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.prompting import PromptRegistry
from task_spec.schemas import ApprovalDecision, RunState, TaskItem, TaskSpec
from task_spec.spec.build import build_common
from task_spec.spec.draft import apply, draft_task, safe_reviewer_note

ApprovalRoute = Literal["finalize", "draft_spec", "__end__"]


FLAG_NO_DRAFTER = "draft_task 미연결 — 결정적 골격만 (제목·상황·행동 기준 없음)"


def make_draft_spec(
    ontology: OntologyRepository | None,
    rel: Relatedness | None = None,
    llm: LLMClient | None = None,
    prompts: PromptRegistry | None = None,
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    """공통 과제 = 결정적 골격 + 서술(LLM). 공고·온톨로지만 쓴다 — 지원자 문서·주장을 읽지 않는다 (불변식 3).

    revise 로 돌아오면 revision 을 올리고 승인자 수정 요청을 서술 입력에 넣는다. 경험 검증 과제는 PR-4.
    """

    async def draft_spec(state: RunState) -> dict[str, Any]:
        revision = state.spec.revision + 1 if state.spec is not None else 1
        spec_id = f"{state.run_id}-r{revision}"
        common: list[TaskItem] = []
        flags: list[str] = []
        update: dict[str, Any] = {}
        p = state.posting_profile
        if p is not None:
            built = await build_common(
                p,
                state.evidence,
                task_id=f"{spec_id}-common-1",
                ontology_version=state.ontology_version,
                ontology=ontology,
                rel=rel,
            )
            flags = built.flags
            task = built.task
            if task is not None:
                if built.ctx is None or llm is None or prompts is None:
                    task = task.model_copy(
                        update={"review_flags": [*task.review_flags, FLAG_NO_DRAFTER]}
                    )
                else:
                    revise = (
                        state.approval
                        if state.approval and state.approval.status == "revise"
                        else None
                    )
                    note, note_flags = safe_reviewer_note(revise.comment if revise else None)
                    res = await draft_task(built.ctx, llm=llm, prompts=prompts, reviewer_note=note)
                    res.flags[:0] = note_flags
                    task = apply(task, res)
                    update = {"provenance": res.provenance, "errors": res.errors}
                common.append(task)
        spec = TaskSpec(
            spec_id=spec_id,
            run_id=state.run_id,
            posting_id=state.posting_id,
            ontology_version=state.ontology_version,
            revision=revision,
            common_tasks=common,
            review_flags=flags,
        )
        return {"spec": spec, "approval": None, **update}

    return draft_spec


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
