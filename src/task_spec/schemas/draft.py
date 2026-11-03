"""공통 과제 서술 작업의 LLM 입출력 (prompts/draft_task 의 input_schema / output_schema).

입력은 공고·온톨로지 텍스트만 — 지원자 문서·주장 필드가 없다 (불변식 3). PseudonymizedText 불필요.
출력은 TaskItem 의 LLM 부분만. 역량·수준·기술 환경·난이도·시간은 결정적 골격이 정하고 LLM 은 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.domain import BehavioralAnchors, Observable, Trap


class DraftTaskInput(Schema):
    """프롬프트 템플릿 변수. 모두 공고·온톨로지에서 나온 텍스트."""

    sub: str = Field(min_length=1)  # "id: 라벨"
    industry: str = Field(min_length=1)  # 업종 라벨 — 표면(domain_surface) 근거
    difficulty: str = Field(min_length=1)  # high / medium / low (+ 설명)
    time_limit_min: int = Field(ge=10, le=240)
    tech_env: str = Field(min_length=1)  # 쓸 수 있는 언어·프레임워크 (공고 언어만)
    competencies: str = Field(min_length=1)  # "- comp:… 라벨 (공고 요구 수준: 수행)" 줄 묶음
    ncs_criteria: str = Field(min_length=1)  # "[요소id#번호] 능력단위 > 요소: 수행준거" 줄 묶음
    ksa: str  # "- 지식: …" 줄 묶음 (없으면 "(없음)")
    revision_note: str  # 재작성 사유 — 검증 실패 내용·승인자 수정 요청 (없으면 "(없음)")


class DraftTaskInputV2(DraftTaskInput):
    """v2: 공개 사례(원리·증상·원인·함정·표면)를 더한다. 조직명·제목·URL 은 넣지 않는다."""

    incident: str  # 사례 블록 (없으면 "(없음)")


class DraftTaskOutput(Schema):
    title: str = Field(min_length=1, max_length=120)
    scenario: str = Field(min_length=1, max_length=3000)
    deliverables: list[str] = Field(min_length=1, max_length=6)
    invariant: str = Field(min_length=1, max_length=600, description="유지할 심층 구조(원리)")
    variable: list[str] = Field(
        min_length=1,
        max_length=6,
        description="지원자·회차마다 바꿀 표면 (domain_surface, numbers …)",
    )
    traps: list[Trap] = Field(min_length=1, max_length=3)
    observables: list[Observable] = Field(min_length=1, max_length=8)
    behavioral_anchors: BehavioralAnchors
    tech_used: list[str] = Field(
        default_factory=list, max_length=8, description="과제가 요구하는 언어·프레임워크"
    )

    @field_validator("deliverables", "variable", "tech_used", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v


class JudgeDraftInput(Schema):
    """평가 전용 판정 프롬프트(judge_draft_task) 입력 — 공고·온톨로지 조건 + 이름을 가린 초안 여러 개."""

    conditions: str = Field(min_length=1)
    drafts: str = Field(min_length=1)  # "[A]\n{json}\n\n[B]\n{json}" — 순서는 공고마다 섞는다


Score = Annotated[int, Field(ge=1, le=5)]


class JudgedDraft(Schema):
    ref: str = Field(min_length=1, max_length=4)
    alignment: Score = Field(description="측정 역량·수행준거와 과제가 맞는가")
    trap_quality: Score = Field(description="함정이 실제로 AI 의 흔한 답을 틀리게 만드는가")
    anchors: Score = Field(description="수준별 행동 기준이 관찰 가능하고 수준 차이가 분명한가")
    feasibility: Score = Field(description="제한 시간·난이도·주어진 자료로 풀 수 있는가")
    ai_axes: Score = Field(description="AI 활용 4축(위임·소통·검증·책임)이 드러나는가")
    issues: list[str] = Field(default_factory=list, max_length=5)


class JudgeDraftOutput(Schema):
    items: list[JudgedDraft] = Field(min_length=1, max_length=8)
    ranking: list[str] = Field(min_length=1, max_length=8, description="좋은 순서의 ref")
