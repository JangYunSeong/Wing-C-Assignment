"""인터뷰 과제 생성 실행기 (#280) — 명세 실행과 같은 방식(RunLauncher + 체크포인터)으로 생성 그래프를 백그라운드로 돌린다.

명세 실행의 run_view.py 처럼 체크포인트와 이 프로세스의 실행기를 함께 보고 진행을 판정한다 (launcher.thread_status).
서비스(interview/service.py)는 이 클래스를 interview/generation.GenerationRunner 프로토콜로만 안다 — 인터뷰 모듈이 API 계층을
import 하지 않게.
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from task_spec.api.launcher import RunBusy, RunLauncher, thread_status
from task_spec.interview.generation import (
    GenerationBusy,
    GenerationNotResumable,
    GenerationProgress,
)
from task_spec.schemas import GENERATE_STEPS, GenerateStep, InterviewGenerateState


def _step(node: str | None) -> GenerateStep:
    """체크포인트 노드 → 단계. 첫 체크포인트 전(입력만 받음)이면 첫 단계."""
    for step in GENERATE_STEPS:
        if node == step:
            return step
    return GENERATE_STEPS[0]


class InterviewGenerationRunner:
    def __init__(self, graph: CompiledStateGraph) -> None:
        self._graph = graph
        self._launcher = RunLauncher(graph)

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

    async def wait(self, thread_id: str) -> None:
        """이 프로세스에서 띄운 생성이 끝날 때까지 (테스트용)."""
        await self._launcher.wait(thread_id)

    async def shutdown(self) -> None:
        await self._launcher.shutdown()
