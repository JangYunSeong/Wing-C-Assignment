"""공통 과제 서술 작업의 LLM 입출력 (prompts/draft_task 의 input_schema / output_schema).

입력은 공고·온톨로지 텍스트만 — 지원자 문서·주장 필드가 없다 (불변식 3). PseudonymizedText 불필요.
출력은 TaskItem 의 LLM 부분만. 역량·수준·기술 환경·난이도·시간은 결정적 골격이 정하고 LLM 은 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.domain import Artifact, BehavioralAnchors, Observable, Trap


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


class DraftTaskOutputV3(DraftTaskOutput):
    """v3: 자료 점진 공개 — scenario 는 증상·목록만, 자료 본문은 artifacts 에."""

    artifacts: list[Artifact] = Field(min_length=6, max_length=12)
    max_requests: int = Field(ge=2, le=6)


class DraftSlotInput(DraftTaskInput):
    """확장 슬롯 1개 (prompts/draft_slot). 공통 과제의 지원자에게 보이는 부분과 언급 금지 개념을 더한다."""

    common_task: str  # 공통 과제 제목·상황·자료 목록 (지원자가 이미 본 것)
    forbidden: str  # 슬롯 본문에 쓰면 안 되는 공통 과제의 핵심 개념 (공통 과제 정답 노출 방지)


class DraftSlotOutput(DraftTaskOutputV3):
    """v3 형식과 같고 규모만 작다 (30분)."""

    deliverables: list[str] = Field(min_length=1, max_length=3)
    traps: list[Trap] = Field(min_length=1, max_length=2)
    observables: list[Observable] = Field(min_length=1, max_length=6)
    artifacts: list[Artifact] = Field(min_length=4, max_length=8)
    max_requests: int = Field(ge=1, le=3)


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


class JudgeTrapsInput(Schema):
    """평가 전용 함정 사실 판정(judge_trap_facts) 입력 — 숨김 부분까지 다 보인 과제 + 원 사례 기준."""

    task: str = Field(min_length=1)  # 과제 JSON (상황·제출물·자료 본문·역할·함정·행동 기준)
    reference: str = Field(
        min_length=1
    )  # 원 사례 원인·흔한 처방이 틀리는 이유·인정할 조치, 없으면 "(없음)"


class TrapVerdict(Schema):
    trap_id: str = Field(min_length=1)
    check: str = Field(
        default="",
        max_length=800,
        description="문제가 생기는 조건·사라지는 최소 조건을 직접 계산한 것",
    )
    verdict: Literal["sound", "unsound", "unclear"] = Field(
        description="sound=어떤 근거 있는 형태로도 그 처방은 문제를 못 푼다(함정 맞음) / "
        "unsound=근거 있는 형태로는 그 처방이 통한다(함정이 정답을 감점) / unclear=자료로 가릴 수 없음"
    )
    reason: str = Field(min_length=1, max_length=600)


class JudgeTrapsOutput(Schema):
    traps: list[TrapVerdict] = Field(min_length=1, max_length=6)
    correct_but_penalized: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="실제로 문제를 해결하는데 함정·수준 1 기준대로면 감점될 답 (틀린 답은 적지 않음)",
    )
    inconsistencies: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="상황·자료·행동 기준 사이 수치·사실이 안 맞는 곳",
    )
