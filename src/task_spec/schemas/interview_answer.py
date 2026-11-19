"""성공 조건 인터뷰 → 담당자가 확인할 기준 답안.

Wing-C-Interview 포크(9ef2f50)에서 옮김 (#262). 출력 모델은 프롬프트 본문에 렌더되므로 클래스 이름·상속·필드·제약을
포크와 같게 둔다. 입력의 글 칸은 PseudoTextField (가명화 모듈이 만든 글만 — 불변식 2, 결정 4).
지원자 질문 답변 범위(TargetedAnswerDraft*)는 D2a(#266)에서 옮겼다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema
from task_spec.schemas.pseudo_text import PseudoTextField

SuccessField = Literal[
    "reproduction", "expected_result", "invariants", "verification", "alternatives"
]


class InterviewAnswerInput(Schema):
    incident_json: PseudoTextField
    success_json: PseudoTextField
    trap_json: PseudoTextField


class InterviewAnswerGroundedInput(InterviewAnswerInput):
    sources_json: PseudoTextField


class InterviewAnswerOutput(Schema):
    answer_text: str = Field(default="", max_length=12000)
    trap_answer_text: str = Field(default="", max_length=12000)
    evidence: list[str] = Field(default_factory=list)
    missing_fields: list[SuccessField] = Field(default_factory=list)
    question: str = Field(default="", max_length=1000)
    questions: list[str] = Field(default_factory=list, max_length=4)


AnswerCheckStatus = Literal["pass", "fail", "needs_information"]
AnswerCheckTopic = Literal[
    "grounding",
    "causality",
    "correct_solution",
    "alternatives",
    "trap_discrimination",
    "verification",
]


class AnswerClaimCheck(Schema):
    text: str = Field(min_length=1, max_length=3000)
    is_trap: bool
    status: AnswerCheckStatus
    evidence: list[str] = Field(default_factory=list, max_length=6)
    reason: str = Field(min_length=1, max_length=1500)
    source_ids: list[str] = Field(default_factory=list, max_length=6)


class AnswerRuleCheck(Schema):
    topic: AnswerCheckTopic
    status: AnswerCheckStatus
    reason: str = Field(min_length=1, max_length=1500)


class AnswerProbeResult(Schema):
    probe_id: str
    verdict: Literal["accept", "reject", "needs_information"]
    reason: str = Field(min_length=1, max_length=1500)


class InterviewAnswerCheckInput(Schema):
    sources_json: PseudoTextField
    incident_json: PseudoTextField
    probes_json: PseudoTextField


class InterviewAnswerCheckOutput(Schema):
    claims: list[AnswerClaimCheck] = Field(min_length=1, max_length=24)
    checks: list[AnswerRuleCheck] = Field(min_length=6, max_length=6)
    probes: list[AnswerProbeResult] = Field(min_length=1, max_length=2)
    questions: list[str] = Field(default_factory=list, max_length=4)


class AnswerValidationRecord(InterviewAnswerCheckOutput):
    incident_sha256: str
    sources_sha256: str
    passed: bool
    issues: list[str] = Field(default_factory=list)
    provenance: dict
    sources: list[dict[str, str]]


class InterviewAnswerGroundedOutput(InterviewAnswerOutput):
    source_ids: list[str] = Field(default_factory=list, max_length=12)


class TargetedAnswerDraftInput(Schema):
    """지원자 질문에 답할 범위 초안 (#266 D2a, 포크 그대로) — 질문은 담당자 글(가명화), 나머지는 과제에서 나온 글."""

    question: PseudoTextField
    hidden_condition: PseudoTextField
    task_json: PseudoTextField


class TargetedAnswerDraftOutput(Schema):
    answer: str = Field(min_length=1, max_length=4000)
