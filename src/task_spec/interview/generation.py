"""인터뷰 과제 생성 실행의 계약 (#280) — 서비스가 보는 실행기 프로토콜·진행·thread 규칙.

구현은 api/interview_generation.py (RunLauncher + 체크포인터, 명세 실행과 같은 방식). 인터뷰 모듈이 API 계층을 import 하지
않게 계약만 여기 둔다. 워커 1개 전제 — '이 프로세스에서 도는 중'이 곧 '생성 중'이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from task_spec.schemas import GenerateStep

GenerationStatus = Literal["running", "failed", "interrupted", "done", "stale"]


class GenerationBusy(RuntimeError):
    """같은 thread 가 이 프로세스에서 도는 중이거나, 읽은 뒤 다른 요청이 먼저 띄웠다."""


class GenerationNotResumable(RuntimeError):
    """이어서 할 단계가 없다 — 끝났거나 시작한 적이 없는 thread."""


@dataclass(frozen=True)
class GenerationProgress:
    status: GenerationStatus
    # running: 지금 단계, failed·interrupted: 이어서 할 단계(실패·중단한 단계), done·stale: None
    step: GenerateStep | None


def thread_id(interview_id: str, revision: int) -> str:
    """생성마다 하나 — revision 은 생성 표시를 저장한 인터뷰 판본."""
    return f"interview-gen-{interview_id}-r{revision}"


class GenerationRunner(Protocol):
    def is_active(self, thread_id: str) -> bool: ...

    def start(self, interview_id: str, thread_id: str) -> None:
        """새 thread 로 처음부터 — 즉시 반환. 같은 thread 가 도는 중이면 GenerationBusy."""
        ...

    async def resume(self, thread_id: str) -> None:
        """실패·중단한 단계부터 — 즉시 반환. 도는 중이면 GenerationBusy, 이을 단계가 없으면 GenerationNotResumable."""
        ...

    async def progress(self, thread_id: str) -> GenerationProgress | None:
        """없는 thread 면 None."""
        ...
