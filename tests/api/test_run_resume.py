"""명세 실행 — 재시작 뒤 중단 판정(interrupted)·노드 오류(failed)·이어서 하기 (#280 E). 작은 가상 그래프 + InMemorySaver."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import httpx
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from task_spec.api.app import create_app
from task_spec.api.launcher import RunLauncher
from task_spec.api.run_view import _task_error
from task_spec.graph import make_checkpointer
from tests.api.conftest import make_settings


class _State(TypedDict, total=False):
    run_id: str


@dataclass
class _Nodes:
    """a → b → c. b 는 문(gate)이 있으면 기다리고, fail_b 가 있으면 한 번 실패한다. c 는 wait_approval 이면 interrupt."""

    gate: asyncio.Event | None = None
    fail_b: Exception | None = None
    wait_approval: bool = False
    calls: list[str] = field(default_factory=list)
    entered_b: asyncio.Event = field(default_factory=asyncio.Event)

    def graph(self, checkpointer: Any) -> CompiledStateGraph:
        async def a(state: _State) -> dict[str, Any]:
            self.calls.append("a")
            return {}

        async def b(state: _State) -> dict[str, Any]:
            self.calls.append("b")
            self.entered_b.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_b is not None:
                exc, self.fail_b = self.fail_b, None
                raise exc
            return {}

        async def c(state: _State) -> dict[str, Any]:
            self.calls.append("c")
            if self.wait_approval:
                interrupt({"spec": None})
            return {}

        g = StateGraph(_State)
        g.add_node("a", a)
        g.add_node("b", b)
        g.add_node("c", c)
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", END)
        return g.compile(checkpointer=checkpointer)


@pytest.fixture
async def checkpointer() -> AsyncIterator[Any]:
    async with make_checkpointer(kind="memory") as cp:
        yield cp


async def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _app(tmp_path: Path, graph: CompiledStateGraph) -> Any:
    return create_app(settings=make_settings(tmp_path), graph=graph)


async def test_restart_shows_interrupted_and_resume_continues_from_the_cut_node(
    tmp_path: Path, checkpointer: Any
) -> None:
    nodes = _Nodes(gate=asyncio.Event())
    graph = nodes.graph(checkpointer)
    app = _app(tmp_path, graph)
    run_id = uuid.uuid4().hex
    app.state.launcher.launch(run_id, {"run_id": run_id})
    await nodes.entered_b.wait()
    await app.state.launcher.shutdown()  # 재시작·배포 — b 를 도는 중에 끊긴다
    app.state.launcher = RunLauncher(graph)  # 새 프로세스
    async with await _client(app) as client:
        view = (await client.get(f"/runs/{run_id}")).json()
        assert view["status"] == "interrupted" and view["current_node"] == "b"
        assert view["failure"] is None
        running = (await client.get("/runs", params={"status": "running"})).json()
        assert run_id not in [r["run_id"] for r in running]  # 끊긴 실행이 '진행 중'에 섞이지 않는다
        assert nodes.gate is not None
        nodes.gate.set()
        resumed = await client.post(f"/runs/{run_id}/resume")
        assert resumed.status_code == 202 and resumed.json()["status"] == "running"
        await app.state.launcher.wait(run_id)
    assert (await graph.aget_state({"configurable": {"thread_id": run_id}})).next == ()
    assert nodes.calls == ["a", "b", "b", "c"]  # 끝난 a 는 다시 하지 않고 끊긴 b 부터


async def test_node_error_stays_failed_after_restart_and_resume_retries_that_node(
    tmp_path: Path, checkpointer: Any
) -> None:
    nodes = _Nodes(fail_b=RuntimeError("가상 입력 조각이 든 메시지"))
    graph = nodes.graph(checkpointer)
    app = _app(tmp_path, graph)
    run_id = uuid.uuid4().hex
    app.state.launcher.launch(run_id, {"run_id": run_id})
    await app.state.launcher.wait(run_id)
    async with await _client(app) as client:
        before = (await client.get(f"/runs/{run_id}")).json()
        assert (before["status"], before["current_node"], before["failure"]) == (
            "failed",
            "b",
            "RuntimeError",
        )
        # 재시작 — 이 프로세스의 실패 기록이 없어도 체크포인트의 노드 오류로 안다
        app.state.launcher = RunLauncher(graph)
        after = (await client.get(f"/runs/{run_id}")).json()
        assert (after["status"], after["failure"]) == ("failed", "RuntimeError")
        assert "가상 입력 조각" not in str(after)  # 오류 메시지는 내지 않는다
        assert (await client.post(f"/runs/{run_id}/resume")).status_code == 202
        await app.state.launcher.wait(run_id)
    assert nodes.calls == ["a", "b", "b", "c"]


async def test_resume_needs_an_interrupted_or_failed_run(tmp_path: Path, checkpointer: Any) -> None:
    nodes = _Nodes(gate=asyncio.Event(), wait_approval=True)
    graph = nodes.graph(checkpointer)
    app = _app(tmp_path, graph)
    run_id = uuid.uuid4().hex
    async with await _client(app) as client:
        assert (await client.post(f"/runs/{uuid.uuid4().hex}/resume")).status_code == 404
        app.state.launcher.launch(run_id, {"run_id": run_id})
        await nodes.entered_b.wait()
        busy = await client.post(f"/runs/{run_id}/resume")  # 도는 중
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "run_not_resumable"
        assert nodes.gate is not None
        nodes.gate.set()
        await app.state.launcher.wait(run_id)
        app.state.launcher = RunLauncher(graph)  # 재시작해도 승인 대기는 끊긴 게 아니다
        waiting = (await client.get(f"/runs/{run_id}")).json()
        assert waiting["status"] == "awaiting_approval"
        assert (await client.post(f"/runs/{run_id}/resume")).status_code == 409


def test_failure_kind_is_read_from_old_repr_errors_too() -> None:
    """예전 체크포인트는 노드 오류를 repr 글로 남겼다 (2df170e2: "KeyError('ct-0683f8a1b8d6')") — 이름만 읽는다."""
    old = SimpleNamespace(tasks=(SimpleNamespace(error="KeyError('ct-0683f8a1b8d6')"),))
    assert _task_error(old) == "KeyError"
    new = SimpleNamespace(tasks=(SimpleNamespace(error=ValueError("가상")),))
    assert _task_error(new) == "ValueError"
    assert _task_error(SimpleNamespace(tasks=(SimpleNamespace(error=None),))) is None
