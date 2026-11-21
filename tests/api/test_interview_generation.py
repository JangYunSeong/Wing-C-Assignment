"""인터뷰 과제 생성 실행기 (#280) — 도는 중 단계·실패·재시작 뒤 중단 판정·이어서 하기. InMemorySaver, 가상 데이터만."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from task_spec.api.interview_generation import InterviewGenerationRunner
from task_spec.graph import make_checkpointer
from task_spec.graph.interview_generate import interview_generate_graph, thread_id
from task_spec.interview.errors import OutputRejected
from task_spec.interview.generation import (
    GenerationBusy,
    GenerationNotResumable,
    GenerationProgress,
)
from task_spec.schemas import GENERATE_STEPS, GenerateStep

INTERVIEW = "int-" + "0" * 31 + "2"
THREAD = thread_id(INTERVIEW, 5)


@dataclass
class GatedService:
    """단계마다 들어온 것을 알리고, 문(gate)이 있으면 열릴 때까지 기다린다."""

    gate: dict[str, asyncio.Event] = field(default_factory=dict)
    fail_at: dict[str, Exception] = field(default_factory=dict)  # 단계 → 한 번 던질 예외
    calls: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    entered: dict[str, asyncio.Event] = field(
        default_factory=lambda: {s: asyncio.Event() for s in GENERATE_STEPS}
    )

    async def generation_step(self, interview_id: str, thread: str, step: GenerateStep) -> bool:
        self.calls.append(step)
        self.entered[step].set()
        if (gate := self.gate.get(step)) is not None:
            await gate.wait()
        if (exc := self.fail_at.pop(step, None)) is not None:
            raise exc
        return True

    async def generation_failed(
        self, interview_id: str, thread: str, step: GenerateStep, exc: Exception
    ) -> None:
        self.failed.append(step)


@pytest.fixture
async def checkpointer() -> AsyncIterator[object]:
    async with make_checkpointer(kind="memory") as cp:
        yield cp


def _runner(service: GatedService, cp: object) -> InterviewGenerationRunner:
    return InterviewGenerationRunner(interview_generate_graph(service, cp))  # type: ignore[arg-type]


async def test_progress_follows_the_running_step_then_done(checkpointer: object) -> None:
    gate = asyncio.Event()
    service = GatedService(gate={"feature": gate})
    runner = _runner(service, checkpointer)
    runner.start(INTERVIEW, THREAD)
    await service.entered["feature"].wait()
    assert await runner.progress(THREAD) == GenerationProgress("running", "feature")
    gate.set()
    await runner.wait(THREAD)
    assert await runner.progress(THREAD) is None  # 끝까지 간 thread 는 지운다 (#280 I2)


async def test_failed_step_shows_failed_then_resumes_from_it(checkpointer: object) -> None:
    service = GatedService(fail_at={"request": OutputRejected("가상 형식 오류")})
    runner = _runner(service, checkpointer)
    runner.start(INTERVIEW, THREAD)
    await runner.wait(THREAD)
    assert await runner.progress(THREAD) == GenerationProgress("failed", "request")
    assert service.failed == ["request"]
    await runner.resume(THREAD)
    await runner.wait(THREAD)
    assert await runner.progress(THREAD) is None  # 이어서 끝까지 갔으면 지운다
    assert service.calls == ["seed", "feature", "request", "request", "slots", "finish"]


async def test_restart_shows_interrupted_and_resumes_without_repeating_steps(
    checkpointer: object,
) -> None:
    # 열리지 않는 문 — 요청 글을 쓰는 중에 서버가 꺼진다
    service = GatedService(gate={"request": asyncio.Event()})
    before = _runner(service, checkpointer)
    before.start(INTERVIEW, THREAD)
    await service.entered["request"].wait()
    await before.shutdown()  # 재시작·배포 (도는 작업 취소)
    service.gate.clear()
    after = _runner(service, checkpointer)  # 새 프로세스 — 같은 체크포인터, 새 실행기
    assert await after.progress(THREAD) == GenerationProgress("interrupted", "request")
    await after.resume(THREAD)
    await after.wait(THREAD)
    assert await after.progress(THREAD) is None
    assert service.calls == ["seed", "feature", "request", "request", "slots", "finish"]


async def test_second_start_or_resume_while_running_is_busy(checkpointer: object) -> None:
    gate = asyncio.Event()
    service = GatedService(gate={"seed": gate})
    runner = _runner(service, checkpointer)
    runner.start(INTERVIEW, THREAD)
    await service.entered["seed"].wait()
    with pytest.raises(GenerationBusy):
        runner.start(INTERVIEW, THREAD)
    with pytest.raises(GenerationBusy):
        await runner.resume(THREAD)
    gate.set()
    await runner.wait(THREAD)
    assert service.calls == list(GENERATE_STEPS)  # 한 번만 돌았다


async def test_resume_needs_an_unfinished_thread(checkpointer: object) -> None:
    service = GatedService()
    runner = _runner(service, checkpointer)
    assert await runner.progress(THREAD) is None  # 시작한 적 없는 thread
    with pytest.raises(GenerationNotResumable):
        await runner.resume(THREAD)
    runner.start(INTERVIEW, THREAD)
    await runner.wait(THREAD)
    with pytest.raises(GenerationNotResumable):  # 끝난 thread
        await runner.resume(THREAD)


async def test_failed_thread_is_kept_for_resume_until_forgotten(checkpointer: object) -> None:
    """실패·중단한 thread 는 이어서 하기에 필요해 두고, 서비스가 그 생성을 버릴 때 forget 으로 지운다 (#280 I2)."""
    service = GatedService(fail_at={"slots": OutputRejected("가상")})
    runner = _runner(service, checkpointer)
    runner.start(INTERVIEW, THREAD)
    await runner.wait(THREAD)
    assert await runner.progress(THREAD) == GenerationProgress("failed", "slots")
    await runner.forget(THREAD)
    assert await runner.progress(THREAD) is None


async def test_forget_leaves_a_running_thread(checkpointer: object) -> None:
    gate = asyncio.Event()
    service = GatedService(gate={"feature": gate})
    runner = _runner(service, checkpointer)
    runner.start(INTERVIEW, THREAD)
    await service.entered["feature"].wait()
    await runner.forget(THREAD)  # 도는 중 — 두고 간다
    assert await runner.progress(THREAD) == GenerationProgress("running", "feature")
    gate.set()
    await runner.wait(THREAD)
    assert await runner.progress(THREAD) is None and service.calls == list(GENERATE_STEPS)
