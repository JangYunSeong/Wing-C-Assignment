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


class FacilitatorChooseInput(Schema):
    """손 시범 진행자 LLM (응시 플랫폼 흉내, `facilitator_choose`) — 규칙 글·요청 글·항목·질문. 지원자 데이터 없음 (AI 풀이의 질문)."""

    rule: str = Field(min_length=1)  # 답 규칙 글 (지금 ANSWER_RULE 또는 새 ANSWER_RULE_ASK)
    request: str = Field(min_length=1)  # 지원자가 받은 요청 글
    items: str = Field(min_length=1)  # 항목 줄 묶음 (주제·구체 조건·답)
    question: str = Field(min_length=1)


class AskedItem(Schema):
    item_id: str = Field(min_length=1)
    concrete: bool  # 규칙 글의 구체 답 조건에 해당 (아니면 주제만)


class FacilitatorChooseOutput(Schema):
    """질문이 묻는 항목 — 비면 어느 항목도 묻지 않음. 답 문장은 쓰지 않는다 (프로그램이 승인된 문장을 그대로 낸다)."""

    asked: list[AskedItem] = Field(default_factory=list, max_length=8)
