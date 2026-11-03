"""경험 검증 확장 문항 LLM 입출력 (prompts/verify_task) [제안: 2026-11-03].

공통 과제 안에 붙는 지원자별 문항. 입력의 지원자 텍스트는 가명화본에서 다시 뽑은 주장만 (PseudonymizedText, 불변식 2).
공통 과제는 제목·상황(지원자에게 보이는 부분)만 넣는다 — 함정·정답은 넣지 않는다.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.domain import BehavioralAnchors, Observable
from task_spec.schemas.pseudo_text import PseudoTextField


class VerifyTaskInput(Schema):
    sub: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    common_task: str = Field(min_length=1)  # 공통 과제 제목 + 상황 (지원자에게 보이는 부분)
    claim: PseudoTextField  # "행동: … / 문장: …" — 가명화본에서 뽑은 주장
    competencies: str = Field(min_length=1)  # 이 주장의 역량 (공고 요구 수준 포함)
    ncs_criteria: str = Field(min_length=1)  # "[요소id#번호] 능력단위 > 요소: 수행준거"
    difficulty: str = Field(min_length=1)
    time_limit_min: int = Field(ge=10, le=240)
    tech_env: str = Field(min_length=1)
    revision_note: str


class VerifyTaskOutput(Schema):
    title: str = Field(min_length=1, max_length=120)
    scenario: str = Field(
        min_length=1, max_length=2000
    )  # 공통 과제 상황을 이어 쓰는 확장 문항 본문
    deliverables: list[str] = Field(min_length=1, max_length=4)
    observables: list[Observable] = Field(min_length=1, max_length=6)
    behavioral_anchors: BehavioralAnchors
    follow_up_questions: list[str] = Field(min_length=2, max_length=3)
    tech_used: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("deliverables", "follow_up_questions", "tech_used", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v
