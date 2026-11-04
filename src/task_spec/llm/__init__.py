"""LLM 호출 공통 래퍼 (llm-client 스킬). 노드는 `LLMClient.call()` 만 쓴다."""

from __future__ import annotations

from task_spec.llm.backend import ChatBackend, Completion, TransientLLMError, Usage
from task_spec.llm.client import LLMClient, LLMResult, SchemaViolation
from task_spec.llm.fake import FakeBackend
from task_spec.llm.quotes import compact_for_quote, normalize_for_quote, quote_in, verify_quote

__all__ = [
    "ChatBackend",
    "Completion",
    "FakeBackend",
    "LLMClient",
    "LLMResult",
    "SchemaViolation",
    "TransientLLMError",
    "Usage",
    "compact_for_quote",
    "normalize_for_quote",
    "quote_in",
    "verify_quote",
]
