"""설정 → LLMClient. 키는 설정(SecretStr) 에서만 꺼낸다. API 의 Settings 와 builder 의 BuilderSettings(#129) 가 같이 쓴다."""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr

from task_spec.llm.client import LLMClient
from task_spec.llm.openai_compat import OpenAICompatBackend


class LLMSettings(Protocol):
    @property
    def openai_api_key(self) -> SecretStr: ...
    @property
    def opencode_api_key(self) -> SecretStr: ...
    @property
    def opencode_base_url(self) -> str: ...
    @property
    def llm_timeout_seconds(self) -> float: ...


def build_llm_client(settings: LLMSettings) -> LLMClient:
    return LLMClient(
        {
            "openai": OpenAICompatBackend(
                api_key=settings.openai_api_key, timeout=settings.llm_timeout_seconds
            ),
            "opencode": OpenAICompatBackend(
                api_key=settings.opencode_api_key,
                base_url=settings.opencode_base_url,
                timeout=settings.llm_timeout_seconds,
                max_tokens_field=True,  # 추론 예산이 듣게 (openai_compat 머리말)
            ),
        }
    )
