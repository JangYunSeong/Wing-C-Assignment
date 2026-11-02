"""모델 백엔드 경계. 재시도·검증은 client 가 하고, 백엔드는 한 번 호출만 한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from task_spec.prompting import Message


class TransientLLMError(Exception):
    """재시도할 만한 오류 (타임아웃·연결·429·5xx)."""


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    reasoning: int = 0


@dataclass(frozen=True)
class Completion:
    content: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    model: str | None = None


class ChatBackend(Protocol):
    async def complete(
        self, model: str, messages: list[Message], params: dict[str, Any]
    ) -> Completion: ...
