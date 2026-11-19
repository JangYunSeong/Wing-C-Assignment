"""체크포인트 스냅샷 → `RunView`. 별도 상태 저장소 없이 `graph.aget_state()` 에서만 계산한다."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import ValidationError

from task_spec.api.launcher import RunLauncher
from task_spec.api.schemas import RunListItem, RunView
from task_spec.postings.career import PostingCareer
from task_spec.postings.company import PostingCompany, company_id
from task_spec.schemas import ApprovalSummary, RunState, TaskSpec
from task_spec.spec.summary import approval_summary

# POST /runs 가 만드는 run_id (uuid4 hex) — 같은 체크포인터의 다른 thread(빌드 승인 `build-…`)와 가른다
RUN_ID = re.compile(r"[0-9a-f]{32}")


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
    return run_view(snapshot, launcher, run_id)


CompanyResolver = Callable[[set[str]], Awaitable[dict[str, PostingCompany]]]
CareerResolver = Callable[[set[str]], Awaitable[dict[str, PostingCareer]]]


async def list_runs(
    graph: CompiledStateGraph,
    launcher: RunLauncher,
    *,
    limit: int | None = None,
    statuses: frozenset[str] | None = None,
    companies: CompanyResolver | None = None,
    company: str | None = None,
    careers: CareerResolver | None = None,
) -> list[RunListItem]:
    """최근에 시작한 실행부터 — limit 이 없으면 전부. 상태·회사(id 또는 표기 — 같은 규칙으로 맞춤)를 고르면 그것만.
    시작 시각·공고 = thread 의 첫 체크포인트(source=input)와 그 그래프 입력 — 회사로 먼저 걸러 상태를 읽는 실행을 줄인다."""
    saver = graph.checkpointer
    if not isinstance(saver, BaseCheckpointSaver):
        return []
    # 목록을 다 받은 뒤 실행마다 상태를 읽는다 — 목록을 도는 동안 같은 저장소를 다시 부르지 않게
    starts: list[tuple[str, str, str | None]] = []
    async for t in saver.alist(None, filter={"source": "input"}):
        run_id = t.config["configurable"]["thread_id"]
        if not RUN_ID.fullmatch(run_id):
            continue
        start = (t.checkpoint.get("channel_values") or {}).get("__start__")
        posting = start.get("posting_id") if isinstance(start, dict) else None
        starts.append((run_id, t.checkpoint["ts"], posting))
    starts.sort(key=lambda x: x[1], reverse=True)
    found = await companies({p for _, _, p in starts if p}) if companies else {}
    wanted = company_id(company) if company else None
    if company:
        starts = [s for s in starts if (c := found.get(s[2] or "")) and c.company_id == wanted]
    levels = await careers({p for _, _, p in starts if p}) if careers else {}
    out: list[RunListItem] = []
    for run_id, started, posting in starts:
        c = found.get(posting) if posting else None
        lv = levels.get(posting) if posting else None
        snapshot = await graph.aget_state({"configurable": {"thread_id": run_id}})
        view = run_view(snapshot, launcher, run_id, detail=False)
        if view is None or (statuses and view.status not in statuses):
            continue
        out.append(
            RunListItem(
                run_id=run_id,
                status=view.status,
                current_node=view.current_node,
                revision=view.revision,
                posting_id=posting or (snapshot.values or {}).get("posting_id"),
                company_id=c.company_id if c else None,
                company=c.name if c else None,
                company_known=c.known if c else None,
                career_level=lv.level if lv else None,
                career_level_source=lv.source if lv else None,
                started_at=datetime.fromisoformat(started),
                updated_at=datetime.fromisoformat(snapshot.created_at)
                if snapshot.created_at
                else None,
            )
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def run_view(
    snapshot: Any, launcher: RunLauncher, run_id: str, *, detail: bool = True
) -> RunView | None:
    """없는 run 이면 None. detail=False 면 명세 초안·승인 요약 없이 상태만 (실행 목록 — 실행마다 요약을 다시 계산하지 않게)."""
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
        "common_id": values.get("common_id"),
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
            spec=draft if detail else None,
            approval_error=pending.get("error"),
            approval_summary=(
                ApprovalSummary.model_validate(pending["summary"])
                if detail and pending.get("summary")
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
    summary = _summary(values) if detail else None
    if approval is not None and approval.status == "rejected":
        return RunView(**base, status="rejected", revision=revision, approval_summary=summary)
    if approval is not None and approval.status == "approved" and spec and spec.approval:
        return RunView(**base, status="done", revision=revision, approval_summary=summary)
    return RunView(**base, status="failed", revision=revision)


def _summary(values: dict[str, Any]) -> ApprovalSummary | None:
    """승인이 끝난 실행도 승인 대기 때와 같은 요약(대조·배정)을 — 저장된 State 에서 다시 계산한다."""
    if values.get("spec") is None:
        return None
    try:
        return approval_summary(RunState.model_validate(values))
    except ValidationError:
        return None  # 예전 형식 체크포인트 — 요약 없이 상태만
