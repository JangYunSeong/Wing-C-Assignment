"""백그라운드 실행기 (fastapi-service "실행 방식").

초기 구현은 `asyncio.create_task` + 프로세스 내 레지스트리. 작업 큐로 교체할 때는 이 클래스만 바꾼다 [미정].
레지스트리는 "지금 이 프로세스에서 도는 중인가" 만 안다 — 실행 상태의 SSOT 는 체크포인터.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
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

    async def delete(
        self, run_id: str, *, after: int, before: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        """멈춘 실행을 지운다 — before(지원자 파일 같은 멱등 정리) 다음 체크포인트 thread. 도는 중이거나 상태를 읽은 뒤 다른 요청이
        띄웠으면 RunBusy. before 가 실패하면 thread 를 남긴다 — 실행이 보여 다시 지울 수 있다 (개인정보 점검 M2).

        지우는 동안 이 실행의 자리를 차지해(is_active) 이어서 하기·승인이 띄우지 못한다. 띄운 횟수를 시작과 끝에 올려, 지우기 전이나
        지우는 중에 상태를 읽은 요청도 launch(after=…) 에서 거부된다 (L1). 확인과 자리 차지 사이에 await 가 없다 (launch 와 같음).
        """
        if self.is_active(run_id) or self.launches(run_id) != after:
            raise RunBusy("run is already active")
        saver = self._graph.checkpointer
        if not isinstance(saver, BaseCheckpointSaver):
            raise RuntimeError("checkpointer is not configured")
        self._launches[run_id] = after + 1

        async def work() -> None:
            if before is not None:
                await before()
            await saver.adelete_thread(run_id)

        task = asyncio.create_task(work())
        self._tasks[run_id] = task
        try:
            await task
        finally:
            self._tasks.pop(run_id, None)
            self._launches[run_id] = self.launches(run_id) + 1
        self._failures.pop(
            run_id, None
        )  # 다 지운 뒤에만 — 정리가 실패하면 실패 판정(failed)을 그대로 둔다
        logger.info("run deleted run_id=%s", run_id)

    async def wait(self, run_id: str) -> None:
        """이 프로세스에서 띄운 실행이 끝날 때까지 (테스트·종료 정리용). 실패는 _run 이 이미 기록했다."""
        task = self._tasks.get(run_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def shutdown(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks.clear()


ThreadStatus = Literal["running", "failed", "interrupted", "finished"]


def thread_status(
    snapshot: Any, launcher: RunLauncher, run_id: str
) -> tuple[ThreadStatus, str | None] | None:
    """이 실행기가 띄운 thread 의 진행과 그 노드 — 없는 thread 면 None. 노드는 도는 중이면 지금 노드, 실패·중단이면 이어서 할 노드.

    워커 1개 전제(api/app.py): 체크포인트가 중간(next 가 있음)인데 이 프로세스에서 돌지 않으면 재시작·배포로 끊긴 것 = interrupted
    (#280 — 명세 실행은 api/run_view 가 따로 가른다). 실패 기록은 이 프로세스의 것뿐이라 재시작 뒤엔 interrupted.
    interrupt() 로 기다리는 그래프(승인)는 next 가 있어도 끊긴 게 아니다 — 부르는 쪽이 먼저 가른다.
    """
    values: dict[str, Any] = snapshot.values or {}
    node = snapshot.next[0] if snapshot.next else None
    if launcher.is_active(run_id):
        return "running", node
    if not values and not snapshot.next:
        return None
    if snapshot.next:
        return ("failed" if launcher.failure(run_id) is not None else "interrupted"), node
    return "finished", None
