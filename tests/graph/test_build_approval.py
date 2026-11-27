"""빌드 승인 그래프 (#130) — 메모리 체크포인터 + 메모리 저장소. 가상 데이터만.

승인은 interrupt 로만, 표시마다 판단이 있어야 approved, 결과가 바뀐 뒤 옛 thread 의 resume 은 적용되지 않는다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from task_spec.graph import make_checkpointer
from task_spec.graph.build_approval import build_approval_graph, outcome_key, thread_id
from task_spec.schemas.build_record import README_PATH, BuildRecord, StarterFile
from task_spec.schemas.package import NOT_A_TRAP, ReviewDecision, flag_id
from task_spec.starter.store import BuildRuleError, InMemoryBuildStore
from tests.starter.test_build_store import AT, FLAG, H_NEW, H_OLD, _artifact, _outcome

CID = "ct-0123456789ab"
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


async def _review_build(
    store: InMemoryBuildStore, tree: str = H_OLD, flags: list[str] | None = None
) -> BuildRecord:
    b = await store.new_build(CID, "onto-test", AT)
    await store.set_status(b.build_id, "generating")
    await store.put_readme(StarterFile.of(b.build_id, "readme", README_PATH, "# 가상 README"))
    outcome = _outcome(tree)
    if flags is not None:
        outcome = outcome.model_copy(update={"review_flags": flags})
    await store.save_outcome(b.build_id, outcome)
    await store.set_status(b.build_id, "verifying")
    await store.set_status(b.build_id, "review")
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(b.build_id, role, tree))  # type: ignore[arg-type]
    got = await store.get(b.build_id)
    assert got is not None
    return got


@pytest.fixture
async def graph_store() -> AsyncIterator[tuple[CompiledStateGraph, InMemoryBuildStore]]:
    store = InMemoryBuildStore()
    async with make_checkpointer(kind="memory") as cp:
        yield build_approval_graph(store, cp), store


def _cfg(b: BuildRecord) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id(b)}}


async def _start(graph: CompiledStateGraph, b: BuildRecord) -> dict[str, Any]:
    await graph.ainvoke({"build_id": b.build_id, "outcome_key": outcome_key(b)}, _cfg(b))
    return await _pending(graph, b)


async def _pending(graph: CompiledStateGraph, b: BuildRecord) -> dict[str, Any]:
    snap = await graph.aget_state(_cfg(b))
    for task in snap.tasks:
        for itr in task.interrupts:
            return dict(itr.value)
    return {}


def _decision(status: str = "approved", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": status,
        "reviewer": "가상승인자",
        "at": NOW.isoformat(),
        "decisions": [
            ReviewDecision(
                flag_id=flag_id(FLAG), flag=FLAG, verdict="confirmed", reviewer="가상승인자"
            ).model_dump(mode="json")
        ]
        if status == "approved"
        else [],
        "comment": None if status == "approved" else "가상 반려 사유",
    }
    return {**base, **over}


async def test_approval_waits_at_interrupt_with_the_flags(graph_store: Any) -> None:
    graph, store = graph_store
    b = await _review_build(store)
    payload = await _start(graph, b)
    assert payload["build_id"] == b.build_id and payload["error"] is None
    assert payload["flags"] == [{"flag_id": flag_id(FLAG), "text": FLAG}]
    now = await store.get(b.build_id)
    assert now is not None and now.status == "review"  # 시작만으로는 아무것도 바뀌지 않는다


async def test_approve_records_reviewer_and_decisions(graph_store: Any) -> None:
    graph, store = graph_store
    b = await _review_build(store)
    await _start(graph, b)
    out = await graph.ainvoke(Command(resume=_decision()), _cfg(b))
    assert out["result"] == "approved"
    done = await store.get(b.build_id)
    assert done is not None and (done.status, done.approved_by) == ("approved", "가상승인자")
    assert [d.verdict for d in done.review_decisions] == ["confirmed"]


async def test_missing_decision_or_bad_value_asks_again(graph_store: Any) -> None:
    graph, store = graph_store
    b = await _review_build(store)
    await _start(graph, b)
    await graph.ainvoke(Command(resume=_decision(decisions=[])), _cfg(b))
    assert "빠진 표시 1건" in (await _pending(graph, b))["error"]
    await graph.ainvoke(Command(resume={"status": "approved"}), _cfg(b))
    assert "reviewer" in (await _pending(graph, b))["error"]
    await graph.ainvoke(Command(resume=_decision("rejected", comment="")), _cfg(b))
    assert "반려에는 사유" in (await _pending(graph, b))["error"]
    now = await store.get(b.build_id)
    assert now is not None and now.status == "review"
    out = await graph.ainvoke(Command(resume=_decision()), _cfg(b))  # 고쳐서 다시 — 승인
    assert out["result"] == "approved"


async def test_a_not_a_trap_flag_is_approved_only_as_accepted_risk(graph_store: Any) -> None:
    """#341: 겹침·무력화 판정을 받아들인 빌드의 '함정 아님' 표시는 '알고 받아들임' 으로만 승인 — 확인(문제 없음)·고침이면 이유를
    보이고 다시 기다린다 (과제가 AI 첫 답과 구별되지 않는 것은 고칠 수도, 문제가 없는 것도 아니다). 다른 표시는 예전처럼 아무 판단이나."""
    graph, store = graph_store
    trap = f"{NOT_A_TRAP}구별 불가(무력화) — 가상 판정 · 승인 조건: '알고 받아들임'"
    b = await _review_build(store, flags=[FLAG, trap])
    await _start(graph, b)

    def judged(verdict: str) -> dict[str, Any]:
        return _decision(
            decisions=[
                ReviewDecision(
                    flag_id=flag_id(f), flag=f, verdict=v, reviewer="가상승인자"
                ).model_dump(mode="json")
                for f, v in ((FLAG, "confirmed"), (trap, verdict))
            ]
        )

    for verdict in ("confirmed", "fixed"):
        await graph.ainvoke(Command(resume=judged(verdict)), _cfg(b))
        error = (await _pending(graph, b))["error"]
        assert "'함정 아님' 표시 1건" in error and "accepted_risk" in error
    now = await store.get(b.build_id)
    assert now is not None and now.status == "review"
    out = await graph.ainvoke(Command(resume=judged("accepted_risk")), _cfg(b))
    assert out["result"] == "approved"
    done = await store.get(b.build_id)
    assert done is not None and [d.verdict for d in done.review_decisions] == [
        "confirmed",
        "accepted_risk",
    ]


async def test_two_decisions_on_a_not_a_trap_flag_must_both_accept_the_risk() -> None:
    """같은 표시에 판단이 둘 이상이면 모두 '알고 받아들임' 이어야 — 순서로 승인이 갈리지 않는다 (#341 적대적 검토 5)."""
    trap = f"{NOT_A_TRAP}구별 불가(무력화) — 가상 판정 · 승인 조건: '알고 받아들임'"
    for order in (("accepted_risk", "confirmed"), ("confirmed", "accepted_risk")):
        store = InMemoryBuildStore()
        b = await _review_build(store, flags=[FLAG, trap])
        decisions = [
            ReviewDecision.of(FLAG, "confirmed", "가상승인자"),
            *(ReviewDecision.of(trap, v, "가상승인자") for v in order),  # type: ignore[arg-type]
        ]
        with pytest.raises(BuildRuleError, match="'함정 아님' 표시 1건"):
            await store.approve(b.build_id, "가상승인자", NOW, decisions)


async def test_reject_fails_the_build_with_the_reason(graph_store: Any) -> None:
    graph, store = graph_store
    b = await _review_build(store)
    await _start(graph, b)
    out = await graph.ainvoke(Command(resume=_decision("rejected")), _cfg(b))
    assert out["result"] == "rejected"
    done = await store.get(b.build_id)
    assert done is not None and done.status == "failed"
    assert done.error == "승인자 반려 (가상승인자): 가상 반려 사유"


async def test_store_rule_violation_returns_to_the_interrupt(graph_store: Any) -> None:
    """README 가 없는 빌드처럼 저장소가 승인을 막으면 그 사유를 보이고 다시 기다린다 (thread 가 끝나지 않는다)."""
    graph, store = graph_store
    b = await store.new_build(CID, "onto-test", AT)
    await store.set_status(b.build_id, "generating")
    await store.save_outcome(b.build_id, _outcome(H_OLD))
    await store.set_status(b.build_id, "verifying")
    b = await store.set_status(b.build_id, "review")
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(b.build_id, role, H_OLD))  # type: ignore[arg-type]
    await _start(graph, b)
    await graph.ainvoke(Command(resume=_decision()), _cfg(b))
    assert "README" in (await _pending(graph, b))["error"]
    now = await store.get(b.build_id)
    assert now is not None and now.status == "review"


async def test_old_thread_cannot_approve_a_changed_outcome(graph_store: Any) -> None:
    """승인을 기다리는 사이 수리(reopen)로 결과가 바뀌면 옛 thread 의 resume 은 적용되지 않는다 — 새 결과는 새 thread."""
    graph, store = graph_store
    b = await _review_build(store)
    await _start(graph, b)
    await store.reopen(b.build_id)
    await store.save_outcome(b.build_id, _outcome(H_NEW))
    await store.set_status(b.build_id, "verifying")
    await store.set_status(b.build_id, "review")
    for role in ("candidate", "grader"):
        await store.put_artifact(_artifact(b.build_id, role, H_NEW))  # type: ignore[arg-type]
    out = await graph.ainvoke(Command(resume=_decision()), _cfg(b))  # 옛 thread
    assert out["result"] == "stale"
    now = await store.get(b.build_id)
    assert now is not None and now.status == "review" and now.approved_by is None
    assert thread_id(now) != thread_id(b)  # 새 결과는 새 thread 로 승인한다
