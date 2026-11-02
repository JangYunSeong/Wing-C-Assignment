"""④ 명세 초안 (대조는 nodes/compare.py) → 사람 승인(interrupt) → 확정.

승인은 approval 노드의 interrupt() 로만 받는다 (불변식 9). 자동 승인 경로 없음.
TODO(spec-generation): draft_spec 는 prompting + llm 으로 연결.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langgraph.graph import END
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from task_spec.ontology.queries import criteria_detail
from task_spec.ontology.relatedness import Relatedness
from task_spec.ontology.repository import OntologyRepository
from task_spec.schemas import ApprovalDecision, RunState, TaskItem, TaskSpec
from task_spec.spec.common import (
    FLAG_NO_INCIDENT,
    TIME_LIMIT_MIN,
    common_tech_env,
    difficulty,
    plan_common,
    targets,
)

ApprovalRoute = Literal["finalize", "draft_spec", "__end__"]


FLAG_NO_DRAFTER = "draft_task 미연결 — 결정적 골격만 (제목·상황·행동 기준 없음)"


def make_draft_spec(
    ontology: OntologyRepository | None, rel: Relatedness | None = None
) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
    """공통 과제 골격(결정적). 공고·온톨로지만 쓴다 — 지원자 문서·주장을 읽지 않는다 (불변식 3).

    revise 로 돌아오면 revision 을 올린다. 경험 검증 과제는 PR-4.
    """

    async def draft_spec(state: RunState) -> dict[str, Any]:
        revision = state.spec.revision + 1 if state.spec is not None else 1
        spec_id = f"{state.run_id}-r{revision}"
        common: list[TaskItem] = []
        flags: list[str] = []
        p = state.posting_profile
        if p is not None:
            plan = plan_common(p, state.evidence, rel)
            flags = list(plan.flags)
            if plan.competencies:
                tcs = targets(plan, state.evidence)
                if ontology is not None:
                    filled = []
                    for t in tcs:
                        crit, _ = await criteria_detail(ontology, t.ncs_unit_ids)
                        ids = [c.criterion_id for c in crit]
                        filled.append(t.model_copy(update={"ncs_criterion_ids": ids}))
                    tcs = filled
                env, env_flags = common_tech_env(p)
                common.append(
                    TaskItem(
                        task_id=f"{spec_id}-common-1",
                        track="common",
                        target_competencies=tcs,
                        tech_env=env,
                        difficulty_target=difficulty(plan),  # type: ignore[arg-type]
                        time_limit_min=TIME_LIMIT_MIN,
                        review_flags=[FLAG_NO_INCIDENT, FLAG_NO_DRAFTER, *env_flags],
                    )
                )
        spec = TaskSpec(
            spec_id=spec_id,
            run_id=state.run_id,
            posting_id=state.posting_id,
            ontology_version=state.ontology_version,
            revision=revision,
            common_tasks=common,
            review_flags=flags,
        )
        return {"spec": spec, "approval": None}

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
