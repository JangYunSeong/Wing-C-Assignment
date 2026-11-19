"""새 공고(경로 2) 스키마 — 절 분리·직군/업종 추천 LLM 입출력과 API 경계.

공고 원문은 지원자 개인정보가 아니다 (PseudonymizedText 불필요, 외부 LLM 사용 가능).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from task_spec.schemas.base import Schema

LineSection = Literal["duties", "required", "preferred", "other"]
# 공고의 신입·경력 (postings/career.py) [확정: 2026-11-19 사용자 — 두 값]. 출처: 사람이 정함 / 공고 문구로 추정
CareerLevel = Literal["신입", "경력"]
CareerSource = Literal["confirmed", "posting_text"]


class PostingLineIn(Schema):
    text: str = Field(min_length=1)
    section: LineSection


class SegmentPostingInput(Schema):
    """프롬프트 segment_posting 변수: {{text}}"""

    text: str = Field(min_length=1, max_length=40000)


class SegmentPostingOutput(Schema):
    lines: list[PostingLineIn] = Field(default_factory=list, max_length=400)


class SuggestFieldInput(Schema):
    """프롬프트 suggest_field 변수."""

    company: str
    title: str
    text: str = Field(max_length=40000)
    subs: str  # "id: 라벨" 목록
    industries: str


class SuggestFieldOutput(Schema):
    sub_ids: list[str] = Field(default_factory=list, max_length=3)
    industry_id: str | None = None  # v1: 하나만
    industry_ids: list[str] = Field(
        default_factory=list, max_length=3
    )  # v2: 가능성 높은 순 최대 3개
    reason: str = Field(default="", max_length=500)

    @field_validator("sub_ids", "industry_ids", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        """모델이 '모름'을 목록 안 null 로 쓰는 경우가 있다 (2026-11-02 dev) — 빼고 받는다."""
        return [x for x in v if x is not None] if isinstance(v, list) else v


class Candidate(Schema):
    id: str
    label: str
    score: float = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)


class AnalyzedLine(Schema):
    """매처 결과 1줄 (임시 공고 저장 단위)."""

    line_id: str
    section: LineSection
    text: str
    kind: Literal["comp", "non", "unmatched"]
    comps: list[str] = Field(default_factory=list)
    level: str | None = None
    non_type: str | None = None
    tools: list[str] = Field(default_factory=list)
