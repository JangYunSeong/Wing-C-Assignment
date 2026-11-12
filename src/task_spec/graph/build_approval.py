"""빌드(시작 코드) 승인 그래프 (#130) — 실행 그래프(thread = run_id, 지원자 단위)와 따로 둔 빌드 단위 그래프.

    build_approval(interrupt) ─ approved → finalize_build → END
                              └ rejected → reject_build   → END

- thread 는 (빌드, 결과)마다 하나 — `thread_id(build)`. 수리(reopen)로 결과가 바뀌면 thread 가 달라져, 옛 검토 표시를 보고 한
  승인이 새 결과에 적용되지 않는다. resume 하면 노드가 처음부터 다시 도므로 그때 저장소의 지금 결과를 다시 보고, 다르면 stale 로 끝낸다.
- 승인은 interrupt 로만 (불변식 9). 잘못된 값·판단이 빠진 검토 표시·저장소 규칙 위반이면 사유를 붙여 다시 interrupt 한다.
- finalize_build 는 DB 에 승인만 기록한다 (`store.approve` — review·묶음이 지금 결과와 맞는지·README·표시마다 판단을 검사).
  묶음 파일을 approved 로 다시 쓰고 평가 키트를 만드는 일은 builder 가 한다 [확정: 2026-11-12 사용자, ③ A].
- interrupt() 앞에는 부작용을 두지 않는다 (저장소 읽기만).
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from task_spec.schemas.build_approval import BuildApprovalDecision, BuildApprovalState
from task_spec.schemas.build_record import BuildRecord
from task_spec.schemas.package import flag_id
from task_spec.starter.store import BuildRepo, BuildRuleError

Route = Literal["finalize_build", "reject_build", "__end__"]
FinalizeRoute = Literal["build_approval", "__end__"]
_END: Literal["__end__"] = "__end__"  # langgraph END (str 로만 알려져 있어 타입을 좁힌다)


def outcome_key(build: BuildRecord) -> str:
    """승인할 결과의 열쇠 — 시작 코드(tree)·채점 내용(grader) 해시. 결과가 없으면 빈 문자열."""
    o = build.outcome
    if o is None:
        return ""
    return hashlib.sha256(f"{o.tree_sha256}:{o.grader_sha256}".encode()).hexdigest()[:12]


def thread_id(build: BuildRecord) -> str:
    return f"build-{build.build_id}-{outcome_key(build)}"


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'decision'}: {e['msg']}" for e in exc.errors()
    )[:300]


def build_approval_graph(store: BuildRepo, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    async def build_approval(state: BuildApprovalState) -> Command[Route]:
        build = await store.get(state.build_id)
        if build is None or build.status != "review" or outcome_key(build) != state.outcome_key:
            return Command(
                update={"result": "stale"}, goto=_END
            )  # 이미 승인·반려·수리됨 — 이 thread 는 낡았다
        assert build.outcome is not None
        flags = list(build.outcome.review_flags)
        want = {flag_id(f) for f in flags}
        payload: dict[str, Any] = {
            "build_id": build.build_id,
            "outcome_key": state.outcome_key,
            "flags": [{"flag_id": flag_id(f), "text": f} for f in flags],
            "error": state.error,
        }
        while True:
            raw = interrupt(payload)
            try:
                decision = BuildApprovalDecision.model_validate(raw)
            except ValidationError as exc:
                payload = {**payload, "error": _summarize(exc)}
                continue
            got = {d.flag_id for d in decision.decisions}
            if decision.status == "approved" and got != want:
                missing, extra = len(want - got), len(got - want)
                payload = {
                    **payload,
                    "error": f"검토 표시 판단이 맞지 않는다 — 빠진 표시 {missing}건, 없는 표시 {extra}건",
                }
                continue
            break
        goto: Route = "finalize_build" if decision.status == "approved" else "reject_build"
        return Command(update={"decision": decision, "error": None}, goto=goto)

    async def finalize_build(state: BuildApprovalState) -> Command[FinalizeRoute]:
        d = state.decision
        assert d is not None
        try:
            await store.approve(state.build_id, d.reviewer, d.at, d.decisions)
        except (
            BuildRuleError
        ) as e:  # 묶음이 결과와 다름·README 없음 등 — 승인자에게 보이고 다시 기다린다
            return Command(update={"decision": None, "error": str(e)[:300]}, goto="build_approval")
        return Command(update={"result": "approved"}, goto=_END)

    async def reject_build(state: BuildApprovalState) -> dict[str, Any]:
        d = state.decision
        assert d is not None
        reason = f"승인자 반려 ({d.reviewer}): {' '.join((d.comment or '').split())}"
        try:
            await store.fail(state.build_id, reason)
        except BuildRuleError:
            return {"result": "stale"}
        return {"result": "rejected"}

    g = StateGraph(BuildApprovalState)
    g.add_node("build_approval", build_approval)
    g.add_node("finalize_build", finalize_build)
    g.add_node("reject_build", reject_build)
    g.add_edge(START, "build_approval")
    g.add_edge("reject_build", END)
    return g.compile(checkpointer=checkpointer)
