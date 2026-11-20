"""인터뷰 과제 생성 그래프 (#280) — 단계 순서·실패한 단계부터 잇기·낡은 thread·체크포인트에 id 만. InMemorySaver, 가상 데이터만."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from task_spec.graph import make_checkpointer
from task_spec.graph.interview_generate import interview_generate_graph, thread_id
from task_spec.interview.errors import OutputRejected
from task_spec.schemas import GENERATE_STEPS, GenerateStep, InterviewGenerateState

INTERVIEW = "int-" + "0" * 31 + "1"


@dataclass
class FakeService:
    fail_at: dict[str, Exception] = field(default_factory=dict)  # 단계 → 한 번 던질 예외
    stale_at: str | None = None
    record_fails: bool = False  # generation_failed 자체가 실패
    calls: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    async def generation_step(self, interview_id: str, thread: str, step: GenerateStep) -> bool:
        assert interview_id == INTERVIEW and thread == thread_id(INTERVIEW, 3)
        self.calls.append(step)
        if step == self.stale_at:
            return False
        if (exc := self.fail_at.pop(step, None)) is not None:
            raise exc
        return True

    async def generation_failed(
        self, interview_id: str, thread: str, step: GenerateStep, exc: Exception
    ) -> None:
        if self.record_fails:
            raise RuntimeError("DB 연결 끊김 (가상)")
        self.failed.append((step, type(exc).__name__))


@pytest.fixture
async def checkpointer() -> AsyncIterator[object]:
    async with make_checkpointer(kind="memory") as cp:
        yield cp


def _setup(service: FakeService, cp: object) -> tuple[CompiledStateGraph, RunnableConfig, dict]:
    graph = interview_generate_graph(service, cp)  # type: ignore[arg-type]
    thread = thread_id(INTERVIEW, 3)
    cfg: RunnableConfig = {"configurable": {"thread_id": thread}}
    return graph, cfg, InterviewGenerateState(interview_id=INTERVIEW, thread_id=thread).model_dump()


async def test_runs_every_step_in_order_and_keeps_only_ids(checkpointer: object) -> None:
    service = FakeService()
    graph, cfg, start = _setup(service, checkpointer)
    out = await graph.ainvoke(start, cfg)
    assert service.calls == list(GENERATE_STEPS) and out["result"] == "done"
    snap = await graph.aget_state(cfg)
    assert snap.next == ()
    # 체크포인트에 인터뷰·과제 본문이 없다 — 단계 산출물은 서비스가 인터뷰 상태에 저장한다
    assert set(snap.values) <= {"interview_id", "thread_id", "result"}


async def test_failed_step_records_reason_and_resumes_from_that_step(checkpointer: object) -> None:
    service = FakeService(fail_at={"request": OutputRejected("가상 형식 오류")})
    graph, cfg, start = _setup(service, checkpointer)
    with pytest.raises(OutputRejected):
        await graph.ainvoke(start, cfg)
    assert service.failed == [("request", "OutputRejected")]
    assert (await graph.aget_state(cfg)).next == ("request",)  # 화면의 현재 단계·이어서 할 곳
    out = await graph.ainvoke(None, cfg)  # 같은 thread 를 입력 없이 — 실패한 단계부터
    assert out["result"] == "done"
    assert service.calls == ["seed", "feature", "request", "request", "slots", "finish"]


async def test_stale_thread_stops_without_running_later_steps(checkpointer: object) -> None:
    service = FakeService(stale_at="feature")
    graph, cfg, start = _setup(service, checkpointer)
    out = await graph.ainvoke(start, cfg)
    assert out["result"] == "stale" and service.calls == ["seed", "feature"]
    assert (await graph.aget_state(cfg)).next == ()


async def test_unrecorded_failure_still_raises_the_original_error(checkpointer: object) -> None:
    service = FakeService(fail_at={"feature": OutputRejected("가상")}, record_fails=True)
    graph, cfg, start = _setup(service, checkpointer)
    with pytest.raises(OutputRejected):
        await graph.ainvoke(start, cfg)
    assert (await graph.aget_state(cfg)).next == ("feature",)


def test_thread_is_per_generation() -> None:
    assert thread_id(INTERVIEW, 3) != thread_id(INTERVIEW, 4)
    assert thread_id(INTERVIEW, 3).startswith("interview-gen-")
