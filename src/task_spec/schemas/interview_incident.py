"""사례 인터뷰의 구조화 입출력. 내려받는 사례 자체는 PublicIncident 그대로다.

Wing-C-Interview 포크(9ef2f50)에서 옮김 (#262). 출력 모델은 프롬프트 본문에 렌더되므로 포크와 같게 둔다.
입력의 글 칸은 포크의 일반 str 대신 PseudoTextField — 담당자 말·공고·대화는 가명화 모듈이 만든 글만 받는다
(불변식 2, 결정 4 [확정: 2026-11-19 사용자]). 입력 모델은 검증에만 쓰이고 프롬프트에 렌더되지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema
from task_spec.schemas.incident import PublicIncident
from task_spec.schemas.pseudo_text import PseudoTextField

IncidentFact = Literal[
    "symptom", "root_cause", "accepted_fixes", "naive_fix", "why_naive_fails", "conditions"
]


class IncidentInterviewInput(Schema):
    sub: str
    context_json: PseudoTextField
    conversation_json: PseudoTextField


class InterviewFeatureSeedInput(Schema):
    """확인된 사례·기준 답안 → 직군 템플릿의 기능형 재료 (#266 D2a). interview 는 가명화한 사례 블록, template_json 은 템플릿 메타(코드 값)."""

    interview: PseudoTextField
    template_json: str


class IncidentInterviewOutput(Schema):
    incident: PublicIncident | None = None
    conditions: str = ""
    evidence: dict[IncidentFact, list[str]] = Field(default_factory=dict)
    missing_fields: list[IncidentFact] = Field(default_factory=list)
    question: str = Field(default="", max_length=1000)
