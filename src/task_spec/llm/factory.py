"""설정 → LLMClient. 키는 설정(SecretStr) 에서만 꺼낸다. API 의 Settings 와 builder 의 BuilderSettings(#129) 가 같이 쓴다."""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr

from task_spec.llm.client import LLMClient
from task_spec.llm.openai_compat import OpenAICompatBackend

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


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
    backends = {
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
    # 돌연변이 생성기(gen_variant)는 Gemini [2026-11-15 사용자] — 키가 있을 때만 (없으면 gemini: 모델 호출이 '모르는 공급자'로 실패)
    gemini_key = getattr(settings, "gemini_api_key", None)
    if gemini_key is not None and gemini_key.get_secret_value():
        backends["gemini"] = OpenAICompatBackend(
            api_key=gemini_key,
            base_url=getattr(settings, "gemini_base_url", GEMINI_BASE_URL),
            timeout=settings.llm_timeout_seconds,
        )
    return LLMClient(backends)
