"""회사 사례 공유 승인 그래프 [확정: 2026-11-19 사용자] — interrupt 로만 승인 (불변식 9). InMemorySaver, 가상 데이터만."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from task_spec.graph import make_checkpointer
from task_spec.graph.incident_share import incident_share_graph, thread_id
from task_spec.schemas import IncidentShareState
from task_spec.spec.company_incidents import CompanyIncident, InMemoryCompanyIncidents
from tests.spec.test_company_incidents import _company_incident

AT = datetime(2026, 11, 19, tzinfo=UTC).isoformat()


def _detect(text: str) -> list[str]:
    return ["PERSON"] if "가상 담당자" in text else []


@pytest.fixture
async def setup() -> AsyncIterator[tuple[CompiledStateGraph, InMemoryCompanyIncidents]]:
    store = InMemoryCompanyIncidents()
    async with make_checkpointer(kind="memory") as cp:
        yield incident_share_graph(store, cp, detect=_detect), store


async def _pending(store: InMemoryCompanyIncidents) -> CompanyIncident:
    inc = _company_incident("aaaa00000001").model_copy(update={"symptom": "가상 담당자가 본 증상"})
    return await store.submit("가상회사", inc, source={"kind": "manual"}, created_by="x")


async def _start(graph: CompiledStateGraph, rec: CompanyIncident) -> tuple[RunnableConfig, Any]:
    cfg: RunnableConfig = {"configurable": {"thread_id": thread_id(rec)}}
    state = IncidentShareState(incident_id=rec.incident_id, content_sha256=rec.content_sha256)
    await graph.ainvoke(state.model_dump(), cfg)
    snap = await graph.aget_state(cfg)
    return cfg, snap.tasks[0].interrupts[0].value


def _decision(status: str, comment: str | None = None) -> dict[str, Any]:
    return {"status": status, "reviewer": "승인자-가상", "at": AT, "comment": comment}


async def test_approve_shares_and_payload_has_no_incident_text(
    setup: tuple[CompiledStateGraph, InMemoryCompanyIncidents],
) -> None:
    graph, store = setup
    rec = await _pending(store)
    cfg, payload = await _start(graph, rec)
    assert payload["flags"] == ["symptom: 사람 이름으로 보이는 표현 1곳"]
    assert "가상 담당자" not in json.dumps(
        payload, ensure_ascii=False
    )  # 체크포인트에 본문 사본 없음
    out = await graph.ainvoke(Command(resume=_decision("approved")), cfg)
    assert out["result"] == "shared"
    got = await store.get(rec.incident_id)
    assert got is not None and (got.status, got.decided_by) == ("shared", "승인자-가상")


async def test_reject_needs_reason_then_rejects(
    setup: tuple[CompiledStateGraph, InMemoryCompanyIncidents],
) -> None:
    graph, store = setup
    rec = await _pending(store)
    cfg, _ = await _start(graph, rec)
    await graph.ainvoke(Command(resume=_decision("rejected")), cfg)  # 사유 없음 → 다시 기다림
    snap = await graph.aget_state(cfg)
    assert "comment" in snap.tasks[0].interrupts[0].value["error"]
    assert (await store.get(rec.incident_id)).status == "pending"  # type: ignore[union-attr]
    out = await graph.ainvoke(Command(resume=_decision("rejected", "직원 이름이 남아 있음")), cfg)
    assert out["result"] == "rejected"
    got = await store.get(rec.incident_id)
    assert got is not None and (got.status, got.decision_comment) == (
        "rejected",
        "직원 이름이 남아 있음",
    )


async def test_retired_while_waiting_is_stale(
    setup: tuple[CompiledStateGraph, InMemoryCompanyIncidents],
) -> None:
    graph, store = setup
    rec = await _pending(store)
    cfg, _ = await _start(graph, rec)
    assert await store.retire(rec.incident_id, "r", datetime.now(UTC), "등록 취소")
    out = await graph.ainvoke(Command(resume=_decision("approved")), cfg)
    assert out["result"] == "stale"
    assert (await store.get(rec.incident_id)).status == "retired"  # type: ignore[union-attr]
