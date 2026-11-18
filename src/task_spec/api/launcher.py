"""백그라운드 실행기 (fastapi-service "실행 방식").

초기 구현은 `asyncio.create_task` + 프로세스 내 레지스트리. 작업 큐로 교체할 때는 이 클래스만 바꾼다 [미정].
레지스트리는 "지금 이 프로세스에서 도는 중인가" 만 안다 — 실행 상태의 SSOT 는 체크포인터.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


class RunBusy(RuntimeError):
    """이 실행이 이 프로세스에서 도는 중이거나, 부르는 쪽이 상태를 읽은 뒤 다른 요청이 띄웠다 (#247)."""


class RunLauncher:
    def __init__(self, graph: CompiledStateGraph) -> None:
        self._graph = graph
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._failures: dict[str, str] = {}  # run_id → 예외 타입 이름 (메시지는 저장하지 않는다)
        self._launches: dict[str, int] = {}  # run_id → 이 프로세스에서 띄운 횟수

    def is_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def failure(self, run_id: str) -> str | None:
        return self._failures.get(run_id)

    def launches(self, run_id: str) -> int:
        """이 프로세스에서 이 실행을 띄운 횟수. 상태를 읽기 전에 받아 두고 launch(after=…) 로 넘긴다."""
        return self._launches.get(run_id, 0)

    def launch(self, run_id: str, graph_input: Any, *, after: int | None = None) -> None:
        """그래프 입력(시작 dict 또는 Command(resume))으로 실행을 띄우고 즉시 반환한다.

        도는 중이면 RunBusy. after(부르는 쪽이 상태를 읽기 전에 본 launches) 가 있으면 그 사이 다른 요청이
        띄웠을 때도 RunBusy — 읽은 상태가 낡았다 (같은 실행에 승인 결정이 겹칠 때, 먼저 들어온 결정이
        이미 끝났어도 #247). 확인과 띄우기 사이에 await 가 없어 같은 이벤트 루프 안에서는 한 요청만 띄운다.
        """
        if self.is_active(run_id) or (after is not None and self.launches(run_id) != after):
            raise RunBusy("run is already active")
        self._launches[run_id] = self.launches(run_id) + 1
        self._failures.pop(run_id, None)
        self._tasks[run_id] = asyncio.create_task(self._run(run_id, graph_input))

    async def _run(self, run_id: str, graph_input: Any) -> None:
        """노드가 끝날 때마다 이름·걸린 시간만 로그에 남긴다 (State 값·원문은 남기지 않음)."""
        config = {"configurable": {"thread_id": run_id}}
        t0 = last = time.monotonic()
        logger.info("run start run_id=%s", run_id)
        try:
            async for update in self._graph.astream(graph_input, config, stream_mode="updates"):
                now = time.monotonic()
                for node in update:
                    if node == "__interrupt__":
                        logger.info(
                            "run waiting approval run_id=%s total_ms=%d", run_id, (now - t0) * 1000
                        )
                    else:
                        logger.info(
                            "node done run_id=%s node=%s ms=%d", run_id, node, (now - last) * 1000
                        )
                last = now
            logger.info("run stop run_id=%s total_ms=%d", run_id, (time.monotonic() - t0) * 1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures[run_id] = type(exc).__name__
            logger.error(
                "run failed run_id=%s exc=%s total_ms=%d",
                run_id,
                type(exc).__name__,
                (time.monotonic() - t0) * 1000,
            )

    async def shutdown(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()
