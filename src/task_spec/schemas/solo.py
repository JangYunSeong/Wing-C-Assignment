"""AI 단독 풀이 테스트 (평가 전용) — 강한 모델이 지원자에게 보이는 과제만 보고 혼자 풀 때 함정에 빠지는가 (context.md §4.3).

입력은 생성된 과제 텍스트뿐 (지원자 데이터 없음).
"""

from __future__ import annotations

from pydantic import Field

from task_spec.schemas.base import Schema


class SolveTaskInput(Schema):
    task: str = Field(min_length=1)  # 지원자에게 보이는 부분 (제목·상황·제출물)
    time_limit_min: int = Field(ge=10, le=240)
    tech_env: str = Field(min_length=1)


class SolveSelectInput(Schema):
    """자료 점진 공개 과제: 목록을 보고 요청할 자료를 고른다."""

    task: str = Field(min_length=1)  # 제목·상황·제출물
    menu: str = Field(min_length=1)  # "- a1: 제목" 목록
    max_requests: int = Field(ge=1, le=12)


class SolveSelectOutput(Schema):
    artifact_ids: list[str] = Field(min_length=1, max_length=12)
    reason: str = Field(default="", max_length=1500)


class SolveTaskOutput(Schema):
    submission: str = Field(min_length=1, max_length=20000)


class GradeSoloInput(Schema):
    task: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    traps: str  # "- t1: 함정 설명 / AI 가 틀리는 이유" (확장 문항은 "(없음)")
    key_concepts: str  # first_mention_of 개념 목록
    anchors: str  # 수준 1·3·5 행동 기준


class TrapVerdict(Schema):
    trap_id: str
    caught: bool = Field(description="답이 함정을 알아채고 흔한 처방을 피했는가")
    evidence: str = Field(default="", max_length=300)


class ConceptVerdict(Schema):
    concept: str
    mentioned: bool = Field(description="답이 그 핵심 개념(또는 같은 뜻)을 스스로 짚었는가")


class GradeSoloOutput(Schema):
    traps: list[TrapVerdict] = Field(default_factory=list, max_length=5)
    concepts: list[ConceptVerdict] = Field(default_factory=list, max_length=8)
    level: int = Field(ge=1, le=5, description="행동 기준에 비춘 수준 1~5")
    rationale: str = Field(default="", max_length=600)
