"""OpenAI 호환 백엔드 (OpenAI API · OpenCode). langchain-openai ChatOpenAI 를 쓴다.

- SDK 자체 재시도는 끈다(max_retries=0). 재시도 정책은 client 한 곳.
- 키는 SecretStr 로 받아 여기서만 꺼낸다. 로그에 키·메시지 본문을 남기지 않는다.
"""

from __future__ import annotations

from typing import Any

import openai
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from task_spec.llm.backend import Completion, TransientLLMError, Usage
from task_spec.prompting import Message

_TRANSIENT = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _to_lc(messages: list[Message]) -> list[BaseMessage]:
    return [
        SystemMessage(m.content) if m.role == "system" else HumanMessage(m.content)
        for m in messages
    ]


def _usage(msg: AIMessage) -> Usage:
    um = msg.usage_metadata or {}
    details = um.get("output_token_details") or {}
    return Usage(
        input=int(um.get("input_tokens", 0) or 0),
        output=int(um.get("output_tokens", 0) or 0),
        reasoning=int(details.get("reasoning", 0) or 0),
    )


class OpenAICompatBackend:
    def __init__(
        self, *, api_key: SecretStr, base_url: str | None = None, timeout: float = 120.0
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url or None
        self._timeout = timeout

    async def complete(
        self, model: str, messages: list[Message], params: dict[str, Any]
    ) -> Completion:
        chat = ChatOpenAI(
            model=model,
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout,
            max_retries=0,
            **params,
        )
        try:
            msg = await chat.ainvoke(_to_lc(messages))
        except _TRANSIENT as exc:
            raise TransientLLMError(type(exc).__name__) from exc
        assert isinstance(msg, AIMessage)
        content = msg.content if isinstance(msg.content, str) else ""
        meta = msg.response_metadata or {}
        return Completion(
            content=content,
            usage=_usage(msg),
            finish_reason=meta.get("finish_reason"),
            model=meta.get("model_name"),
        )
