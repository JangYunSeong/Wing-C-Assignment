"""테스트용 백엔드. 네트워크 없이 고정 응답을 순서대로 돌려준다.

응답 키: (prompt_id, 입력 해시) — 같은 입력에 여러 응답을 넣으면 재시도 순서대로 소비한다.
키가 없으면 default 큐를 쓴다.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from typing import Any

from task_spec.llm.backend import Completion, TransientLLMError
from task_spec.prompting import Message


def input_key(messages: list[Message]) -> str:
    return hashlib.sha256("\n".join(m.content for m in messages).encode()).hexdigest()[:16]


class FakeBackend:
    def __init__(self) -> None:
        self._queues: dict[str, deque[Completion | Exception]] = defaultdict(deque)
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (model, params) — 본문은 남기지 않는다

    def push(self, *items: Completion | Exception, key: str = "default") -> FakeBackend:
        self._queues[key].extend(items)
        return self

    async def complete(
        self, model: str, messages: list[Message], params: dict[str, Any]
    ) -> Completion:
        self.calls.append((model, dict(params)))
        k = input_key(messages)
        q = self._queues[k] if self._queues.get(k) else self._queues["default"]
        if not q:
            raise AssertionError("FakeBackend: 준비된 응답이 없습니다")
        item = q.popleft()
        if isinstance(item, Exception):
            raise item
        return item


__all__ = ["FakeBackend", "TransientLLMError", "input_key"]
