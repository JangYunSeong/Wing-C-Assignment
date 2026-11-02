"""역량 선택 작업의 LLM 입출력 (prompts/select_competency 의 input_schema / output_schema).

claims 는 가명화본에서 나온 주장 텍스트만 (PseudonymizedText). candidates 는 온톨로지 텍스트만 (지원자 정보 없음).
"""

from __future__ import annotations

from pydantic import Field, field_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.pseudo_text import PseudoTextField


class SelectCompetencyInput(Schema):
    """프롬프트 템플릿 변수: {{sub}}, {{claims}}, {{candidates}}."""

    sub: str = Field(min_length=1)  # 지원 공고 직군 (id: 라벨)
    claims: PseudoTextField  # "[c1] 행동: … / 문장: …" 줄 묶음
    candidates: str = Field(min_length=1)  # "[c1]\n- comp:… 라벨 — 설명" 묶음 (온톨로지 텍스트)


class SelectedItem(Schema):
    ref: str = Field(min_length=1, max_length=20, description="주장 표시 (c1, c2 …)")
    competency_ids: list[str] = Field(
        default_factory=list, max_length=2, description="그 주장의 후보 목록에서 고른 역량 id 0~2개"
    )
    uncovered: bool = Field(
        default=False, description="후보 중 맞는 역량이 없고 직무 역량을 보여주는 주장이면 true"
    )
    reason: str | None = Field(default=None, max_length=300)

    @field_validator("competency_ids", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v


class SelectCompetencyOutput(Schema):
    items: list[SelectedItem] = Field(default_factory=list, max_length=50)
