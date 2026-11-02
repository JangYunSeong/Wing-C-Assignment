"""백그라운드 실행기 (fastapi-service "실행 방식").

초기 구현은 `asyncio.create_task` + 프로세스 내 레지스트리. 작업 큐로 교체할 때는 이 클래스만 바꾼다 [미정].
레지스트리는 "지금 이 프로세스에서 도는 중인가" 만 안다 — 실행 상태의 SSOT 는 체크포인터.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


class RunLauncher:
    def __init__(self, graph: CompiledStateGraph) -> None:
        self._graph = graph
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._failures: dict[str, str] = {}  # run_id → 예외 타입 이름 (메시지는 저장하지 않는다)

    def is_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def failure(self, run_id: str) -> str | None:
        return self._failures.get(run_id)

    def launch(self, run_id: str, graph_input: Any) -> None:
        """그래프 입력(시작 dict 또는 Command(resume))으로 실행을 띄우고 즉시 반환한다."""
        if self.is_active(run_id):
            raise RuntimeError("run is already active")
        self._failures.pop(run_id, None)
        self._tasks[run_id] = asyncio.create_task(self._run(run_id, graph_input))

    async def _run(self, run_id: str, graph_input: Any) -> None:
        config = {"configurable": {"thread_id": run_id}}
        try:
            await self._graph.ainvoke(graph_input, config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures[run_id] = type(exc).__name__
            logger.error("run failed run_id=%s exc=%s", run_id, type(exc).__name__)

    async def shutdown(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
