"""② 역할 판정(규칙) + 주장 추출(LLM).

TODO(claim-extraction): extract_claims 는 prompting.assemble() + llm.call() 로 연결.
지금은 LLM 을 부르지 않는 스텁이다.
"""

from __future__ import annotations

from typing import Any

from task_spec.schemas import RunState


async def assign_roles(state: RunState) -> dict[str, Any]:
    """칸 위치로 역할을 규칙 판정한다 (불변식 6). TODO: 칸 정보가 생기면 구현."""
    return {}


async def extract_claims(state: RunState) -> dict[str, Any]:
    return {"claims": []}
