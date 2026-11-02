"""프롬프트 버전 관리·조립 (prompt-registry 스킬)."""

from __future__ import annotations

from task_spec.prompting.assemble import AssembledPrompt, Message, assemble
from task_spec.prompting.context import (
    CriteriaProvider,
    EmptyCriteria,
    EmptyFewshot,
    FewshotContext,
    FewshotProvider,
)
from task_spec.prompting.registry import PromptError, PromptRegistry, PromptTemplate

__all__ = [
    "AssembledPrompt",
    "CriteriaProvider",
    "EmptyCriteria",
    "EmptyFewshot",
    "FewshotContext",
    "FewshotProvider",
    "Message",
    "PromptError",
    "PromptRegistry",
    "PromptTemplate",
    "assemble",
]
