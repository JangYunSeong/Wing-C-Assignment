"""평가 전용 대조 정답 판정(judge_overlap) 입출력 — eval/retrieve/run_overlap.py. 운영 경로에서 쓰지 않는다."""

from __future__ import annotations

from pydantic import Field

from task_spec.schemas.base import Schema
from task_spec.schemas.domain import OverlapLabel
from task_spec.schemas.pseudo_text import PseudoTextField


class JudgeOverlapInput(Schema):
    resume: PseudoTextField  # 가명화본 이력서 전체 (불변식 2)
    competencies: str = Field(min_length=1)  # "comp:id — 이름" 줄 목록 (공고 역량)


class OverlapVerdict(Schema):
    competency_id: str = Field(min_length=1)
    label: OverlapLabel = Field(
        description="action=이 역량을 직접 보여주는 행동 / related=가까운 역량의 행동으로 일부만 / "
        "mention=기술·도구 이름만 나오고 이 역량의 행동은 없음 / none=없음"
    )
    evidence: str = Field(
        default="", max_length=120, description="근거가 된 이력서 표현 (none 이면 빈 문자열)"
    )


class JudgeOverlapOutput(Schema):
    items: list[OverlapVerdict] = Field(min_length=1, max_length=80)
