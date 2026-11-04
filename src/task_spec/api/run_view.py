"""체크포인트 스냅샷 → `RunView`. 별도 상태 저장소 없이 `graph.aget_state()` 에서만 계산한다."""

from __future__ import annotations

from typing import Any

from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from task_spec.api.launcher import RunLauncher
from task_spec.api.schemas import RunView
from task_spec.schemas import ApprovalSummary, RunState, TaskSpec
from task_spec.spec.summary import approval_summary


def _pending_interrupt(snapshot: Any) -> dict[str, Any] | None:
    for task in snapshot.tasks:
        for itr in task.interrupts:
            if isinstance(itr.value, dict):
                return itr.value
    return None


async def load_run_view(
    graph: CompiledStateGraph, launcher: RunLauncher, run_id: str
) -> RunView | None:
    """없는 run 이면 None."""
    snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
    values: dict[str, Any] = snapshot.values or {}
    active = launcher.is_active(run_id)

    if not values:
        # 첫 체크포인트 전이라도 이 프로세스가 막 띄운 실행이면 존재한다.
        return RunView(run_id=run_id, status="running") if active else None

    current = snapshot.next[0] if snapshot.next else None
    base: dict[str, Any] = {
        "run_id": run_id,
        "current_node": current,
        "ontology_version": values.get("ontology_version"),
        "errors": list(values.get("errors") or []),
    }
    spec: TaskSpec | None = values.get("spec")

    # 이 프로세스에서 도는 중이면 스냅샷이 중간 상태일 수 있다 (resume 직후 등).
    if active:
        return RunView(**base, status="running", revision=spec.revision if spec else None)

    pending = _pending_interrupt(snapshot)
    if pending is not None:
        draft = TaskSpec.model_validate(pending["spec"]) if pending.get("spec") else spec
        return RunView(
            **base,
            status="awaiting_approval",
            revision=draft.revision if draft else None,
            spec=draft,
            approval_error=pending.get("error"),
            approval_summary=(
                ApprovalSummary.model_validate(pending["summary"])
                if pending.get("summary")
                else None
            ),
        )

    if launcher.failure(run_id) is not None:
        return RunView(**base, status="failed")

    if snapshot.next:
        # 프로세스 재시작 등으로 이어 받을 주체가 없는 중간 상태 — [미정] 복구 정책.
        return RunView(**base, status="running", revision=spec.revision if spec else None)

    approval = values.get("approval")
    revision = spec.revision if spec else None
    if approval is not None and approval.status == "rejected":
        return RunView(
            **base, status="rejected", revision=revision, approval_summary=_summary(values)
        )
    if approval is not None and approval.status == "approved" and spec and spec.approval:
        return RunView(**base, status="done", revision=revision, approval_summary=_summary(values))
    return RunView(**base, status="failed", revision=revision)


def _summary(values: dict[str, Any]) -> ApprovalSummary | None:
    """승인이 끝난 실행도 승인 대기 때와 같은 요약(대조·배정)을 — 저장된 State 에서 다시 계산한다."""
    if values.get("spec") is None:
        return None
    try:
        return approval_summary(RunState.model_validate(values))
    except ValidationError:
        return None  # 예전 형식 체크포인트 — 요약 없이 상태만
