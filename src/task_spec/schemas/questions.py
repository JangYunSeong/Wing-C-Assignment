"""면접 확인 질문 LLM 입출력 (prompts/slot_questions) [확정: 2026-11-03].

지원자의 검증 주장(가명화본, 불변식 2) + 배정된 슬롯(또는 없음) → 연결 문구 + 면접 질문 2~3개.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.pseudo_text import PseudoTextField


class SlotQuestionsInput(Schema):
    claim: PseudoTextField  # "행동: … / 문장: …" — 가명화본에서 다시 뽑은 주장
    competencies: str = Field(min_length=1)
    slot: str = Field(min_length=1)  # 배정된 슬롯의 제목·상황, 없으면 "(슬롯 없음 — 면접 질문만)"
    forbidden: str  # 연결 문구에 쓰면 안 되는 과제 정답 개념
    revision_note: str  # 재요청 사유 (없으면 "(없음)")


class SlotQuestionsOutput(Schema):
    link_text: str = Field(
        min_length=1, max_length=80, description="주장의 행동만 짧게 (수치·회사 없음)"
    )
    follow_up_questions: list[str] = Field(min_length=2, max_length=3)

    @field_validator("follow_up_questions", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v
