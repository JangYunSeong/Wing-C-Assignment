"""손 시범 (평가 전용, docs/design/analysis-stage.md §9.1) — 진행자 쪽 고치기에 쓰는 동의어 목록 절차 (`expand_match_terms`).

입력은 공고로 만든 공통 과제의 주제·매칭 낱말뿐 (지원자 데이터 없음). 답 문장은 넣지 않는다.
"""

from __future__ import annotations

from pydantic import Field

from task_spec.schemas.base import Schema


class ExpandTermsInput(Schema):
    items: str = Field(min_length=1)  # "[f1] 주제: … / 지금 낱말: a, b" 줄 묶음


class ExpandedTerms(Schema):
    item_id: str = Field(min_length=1)
    terms: list[str] = Field(default_factory=list, max_length=8)


class ExpandTermsOutput(Schema):
    items: list[ExpandedTerms] = Field(default_factory=list)
