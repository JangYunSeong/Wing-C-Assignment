"""Settings → LLMClient. 키는 Settings(SecretStr) 에서만 꺼낸다."""

from __future__ import annotations

from task_spec.config import Settings
from task_spec.llm.client import LLMClient
from task_spec.llm.openai_compat import OpenAICompatBackend


def build_llm_client(settings: Settings) -> LLMClient:
    return LLMClient(
        {
            "openai": OpenAICompatBackend(api_key=settings.openai_api_key),
            "opencode": OpenAICompatBackend(
                api_key=settings.opencode_api_key, base_url=settings.opencode_base_url
            ),
        }
    )
