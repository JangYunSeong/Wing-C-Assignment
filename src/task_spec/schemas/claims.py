"""주장 추출 작업의 LLM 입출력 (prompts/extract_claims 의 input_schema / output_schema).

LLM 출력에 역할(role)은 없다 — 역할은 칸 위치로 규칙 판정한다 (CLAUDE.md 불변식 6).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema
from task_spec.schemas.pseudo_text import PseudoTextField


class ClaimExtractionInput(Schema):
    """프롬프트 템플릿 변수: {{doc_kind}}, {{text}}."""

    doc_kind: Literal["resume", "cover_letter"]
    text: PseudoTextField


class ExtractedClaim(Schema):
    """행동 단위 주장 1건 (context.md §4.6: 행동, 대상·기술, 결과·수치, 기간, 원문 문장)."""

    action: str = Field(min_length=1, max_length=300, description="지원자가 한 행동 (동사구)")
    target: str | None = Field(default=None, max_length=300, description="행동의 대상·기술")
    result: str | None = Field(default=None, max_length=300, description="결과·수치")
    period: str | None = Field(default=None, max_length=100, description="기간 (원문 표기 그대로)")
    quote: str = Field(
        min_length=1, max_length=1000, description="근거 원문 문장 — 입력에 글자 그대로 있어야 함"
    )


class ClaimExtractionOutput(Schema):
    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=100)
