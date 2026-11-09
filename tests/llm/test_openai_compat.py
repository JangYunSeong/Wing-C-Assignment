"""OpenCode 백엔드는 출력 상한을 max_tokens 이름 그대로 보낸다 — max_completion_tokens 면 추론 예산이 듣지 않는다 (2026-11-09)."""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from task_spec.llm.openai_compat import OpenAICompatBackend

PARAMS = {"temperature": 0.2, "max_tokens": 12000, "reasoning_effort": "low"}


def _payload(backend: OpenAICompatBackend) -> dict[str, object]:
    chat = ChatOpenAI(
        model="qwen3.8-flash",
        api_key=SecretStr("x"),
        base_url="http://localhost",
        max_retries=0,
        **backend._kwargs(PARAMS),
    )
    return {
        k: v
        for k, v in chat._get_request_payload([HumanMessage("가상")]).items()
        if k != "messages"
    }


def test_opencode_sends_max_tokens_not_max_completion_tokens() -> None:
    p = _payload(
        OpenAICompatBackend(
            api_key=SecretStr("x"), base_url="http://localhost", max_tokens_field=True
        )
    )
    assert "max_completion_tokens" not in p and p["extra_body"] == {"max_tokens": 12000}
    assert p["reasoning_effort"] == "low"


def test_openai_keeps_langchain_default() -> None:
    p = _payload(OpenAICompatBackend(api_key=SecretStr("x")))
    assert p["max_completion_tokens"] == 12000 and "extra_body" not in p
