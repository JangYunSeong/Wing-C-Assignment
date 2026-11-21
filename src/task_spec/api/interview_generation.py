"""인터뷰 과제 생성 실행기 (#280) — 명세 실행과 같은 방식(RunLauncher + 체크포인터)으로 생성 그래프를 백그라운드로 돌린다.

명세 실행의 run_view.py 처럼 체크포인트와 이 프로세스의 실행기를 함께 보고 진행을 판정한다 (launcher.thread_status).
서비스(interview/service.py)는 이 클래스를 interview/generation.GenerationRunner 프로토콜로만 안다 — 인터뷰 모듈이 API 계층을
import 하지 않게.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from task_spec.api.launcher import RunBusy, RunLauncher, thread_status
from task_spec.interview.generation import (
    GenerationBusy,
    GenerationNotResumable,
    GenerationProgress,
)
from task_spec.schemas import GENERATE_STEPS, GenerateStep, InterviewGenerateState

logger = logging.getLogger(__name__)


def _step(node: str | None) -> GenerateStep:
    """체크포인트 노드 → 단계. 첫 체크포인트 전(입력만 받음)이면 첫 단계."""
    for step in GENERATE_STEPS:
        if node == step:
            return step
    return GENERATE_STEPS[0]


class InterviewGenerationRunner:
    """끝까지 간 생성(done·stale)의 thread 는 끝나자마자 지운다 (#280 I2 — 생성마다 쌓였다). 실패·중단한 thread 는 이어서 하기에
    필요해 두고, 서비스가 그 생성을 버릴 때(다시 만들기·역량 바꾸기·승인·폐기) forget 으로 지운다."""

    def __init__(self, graph: CompiledStateGraph) -> None:
        self._graph = graph
        self._launcher = RunLauncher(graph)
        self._watchers: dict[str, asyncio.Task[None]] = {}

    @staticmethod
    def _config(thread_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id}}

    def is_active(self, thread_id: str) -> bool:
        return self._launcher.is_active(thread_id)

    def start(self, interview_id: str, thread_id: str) -> None:
        start = InterviewGenerateState(interview_id=interview_id, thread_id=thread_id)
        try:
            self._launcher.launch(thread_id, start.model_dump())
        except RunBusy:
            raise GenerationBusy("generation is already active") from None
        self._watch(thread_id)

    async def resume(self, thread_id: str) -> None:
        """읽은 뒤 다른 요청이 먼저 띄웠으면 GenerationBusy (launch(after=…), 명세 실행 승인과 같음)."""
        seen = self._launcher.launches(thread_id)
        if self._launcher.is_active(thread_id):
            raise GenerationBusy("generation is already active")
        snapshot = await self._graph.aget_state(self._config(thread_id))
        if not snapshot.next:
            raise GenerationNotResumable("이어서 할 단계가 없습니다")
        try:
            self._launcher.launch(thread_id, None, after=seen)
        except RunBusy:
            raise GenerationBusy("generation is already active") from None
        self._watch(thread_id)

    async def progress(self, thread_id: str) -> GenerationProgress | None:
        snapshot = await self._graph.aget_state(self._config(thread_id))
        found = thread_status(snapshot, self._launcher, thread_id)
        if found is None:
            return None
        status, node = found
        if status == "finished":
            result = (snapshot.values or {}).get("result")
            return GenerationProgress(status="stale" if result == "stale" else "done", step=None)
        return GenerationProgress(status=status, step=_step(node))

    async def forget(self, thread_id: str) -> None:
        if self._launcher.is_active(thread_id):
            return  # 도는 중 — 끝나면 지켜보는 태스크가 지운다
        saver = self._graph.checkpointer
        if isinstance(saver, BaseCheckpointSaver):
            await saver.adelete_thread(thread_id)
            logger.info("interview generation thread forgotten thread=%s", thread_id)

    def _watch(self, thread_id: str) -> None:
        self._watchers[thread_id] = asyncio.create_task(self._forget_when_finished(thread_id))

    async def _forget_when_finished(self, thread_id: str) -> None:
        """끝까지 갔으면(체크포인트에 다음 노드가 없음 — done·stale) 지운다. 실패·중단이면 이어서 하기에 두고 간다."""
        await self._launcher.wait(thread_id)
        try:
            snapshot = await self._graph.aget_state(self._config(thread_id))
            if not snapshot.next:
                await self.forget(thread_id)
        except Exception as exc:  # noqa: BLE001 — 지우지 못해도 생성은 끝났다 (다음에 버릴 때 다시 지운다)
            logger.warning(
                "interview generation thread not forgotten thread=%s type=%s",
                thread_id,
                type(exc).__name__,
            )

    async def wait(self, thread_id: str) -> None:
        """이 프로세스에서 띄운 생성이 끝날 때까지 — 끝난 thread 지우기까지 (테스트용)."""
        await self._launcher.wait(thread_id)
        watcher = self._watchers.get(thread_id)
        if watcher is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def shutdown(self) -> None:
        # 지켜보는 태스크를 먼저 멈춘다 — 실행을 끊은 뒤 그 thread 를 지우지 않게 (끊긴 생성은 다음 기동 때 이어서 한다)
        for watcher in self._watchers.values():
            watcher.cancel()
        for watcher in self._watchers.values():
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        self._watchers.clear()
        await self._launcher.shutdown()
