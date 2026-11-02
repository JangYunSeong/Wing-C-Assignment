"""그래프 단위 테스트 (InMemorySaver, LLM 없음)."""

from __future__ import annotations

from langgraph.types import Command

from tests.conftest import config_for, decision, failing_pseudonymizer, fake_doc, run_input


def _interrupted(result: dict) -> bool:
    return bool(result.get("__interrupt__"))


async def test_reaches_approval_interrupt(memory_graph) -> None:
    inp = run_input(docs=[fake_doc("d1", "resume"), fake_doc("d2", "cover_letter")])
    result = await memory_graph.ainvoke(inp, config_for(inp))
    assert _interrupted(result)
    payload = result["__interrupt__"][0].value
    assert payload["spec"]["revision"] == 1
    assert payload["spec"]["ontology_version"] == "onto-v2026.11.02"
    state = await memory_graph.aget_state(config_for(inp))
    assert state.next == ("approval",)


async def test_approved_finalizes_with_decision(memory_graph) -> None:
    inp = run_input(docs=[fake_doc("d1", "resume")])
    cfg = config_for(inp)
    await memory_graph.ainvoke(inp, cfg)
    result = await memory_graph.ainvoke(Command(resume=decision("approved")), cfg)
    assert not _interrupted(result)
    assert result["spec"].approval.status == "approved"
    assert result["spec"].approval.reviewer == "reviewer-fake"


async def test_rejected_ends_without_finalize(memory_graph) -> None:
    inp = run_input(docs=[fake_doc("d1", "resume")])
    cfg = config_for(inp)
    await memory_graph.ainvoke(inp, cfg)
    result = await memory_graph.ainvoke(Command(resume=decision("rejected")), cfg)
    assert result["approval"].status == "rejected"
    assert result["spec"].approval is None  # 확정되지 않음
    assert (await memory_graph.aget_state(cfg)).next == ()


async def test_revise_regenerates_and_asks_again(memory_graph) -> None:
    inp = run_input(docs=[fake_doc("d1", "resume")])
    cfg = config_for(inp)
    await memory_graph.ainvoke(inp, cfg)
    result = await memory_graph.ainvoke(
        Command(resume=decision("revise", "과제 범위를 좁혀 주세요")), cfg
    )
    assert _interrupted(result)
    assert result["__interrupt__"][0].value["spec"]["revision"] == 2
    final = await memory_graph.ainvoke(Command(resume=decision("approved")), cfg)
    assert final["spec"].revision == 2
    assert final["spec"].approval.status == "approved"


async def test_invalid_decision_reinterrupts_with_error(memory_graph) -> None:
    inp = run_input(docs=[fake_doc("d1", "resume")])
    cfg = config_for(inp)
    await memory_graph.ainvoke(inp, cfg)
    bad = {**decision("approved"), "reviewer": ""}
    result = await memory_graph.ainvoke(Command(resume=bad), cfg)
    assert _interrupted(result)
    assert "reviewer" in result["__interrupt__"][0].value["error"]
    final = await memory_graph.ainvoke(Command(resume=decision("approved")), cfg)
    assert final["spec"].approval.status == "approved"


async def test_pseudonymization_failure_stops_pipeline(graph_with) -> None:
    graph = await graph_with(failing_pseudonymizer)
    inp = run_input(docs=[fake_doc("d1", "resume")])
    cfg = config_for(inp)
    result = await graph.ainvoke(inp, cfg)
    assert not _interrupted(result)
    assert result.get("spec") is None  # 명세 단계까지 가지 않음
    assert result.get("claims") == []
    assert [e.code for e in result["errors"]] == ["residual_pii"]
    history = [s.next async for s in graph.aget_state_history(cfg)]
    visited = {n for nxt in history for n in nxt}
    assert "assign_roles" not in visited and "extract_claims" not in visited


async def test_no_documents_goes_straight_to_common_tasks(memory_graph) -> None:
    inp = run_input(docs=[])
    cfg = config_for(inp)
    result = await memory_graph.ainvoke(inp, cfg)
    assert _interrupted(result)
    history = [s.next async for s in memory_graph.aget_state_history(cfg)]
    visited = {n for nxt in history for n in nxt}
    assert "ingest" not in visited and "pseudonymize" not in visited
    assert "compare" in visited


async def test_unknown_input_field_never_enters_state(memory_graph) -> None:
    """입력 스키마에 없는 키(예: 원문)는 State·체크포인트에 들어가지 않는다."""
    inp = {**run_input(), "raw_text": "가상 원문 — State 에 들어가면 안 됨"}
    cfg = config_for(inp)
    await memory_graph.ainvoke(inp, cfg)
    snapshot = await memory_graph.aget_state(cfg)
    assert "raw_text" not in snapshot.values
    async for s in memory_graph.aget_state_history(cfg):
        assert "raw_text" not in s.values
