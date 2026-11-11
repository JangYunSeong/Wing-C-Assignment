"""공통 과제 서술 작업의 LLM 입출력 (prompts/draft_task 의 input_schema / output_schema).

입력은 공고·온톨로지 텍스트만 — 지원자 문서·주장 필드가 없다 (불변식 3). PseudonymizedText 불필요.
출력은 TaskItem 의 LLM 부분만. 역량·수준·기술 환경·난이도·시간은 결정적 골격이 정하고 LLM 은 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.domain import (
    AcceptedDesign,
    Artifact,
    BehavioralAnchors,
    Clarification,
    HiddenAcceptance,
    Observable,
    PublicAcceptance,
    Requirement,
    Trap,
)


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


class DraftFeatureInput(DraftTaskInputV2):
    """기능 개발형 공통 과제 (prompts/draft_feature_task) 입력 — 이슈형 v2 입력 + 단계별 시간.

    공고·온톨로지·공개 사례만 (불변식 3). 참고 사례는 2단계 '배포 후 증상'과 숨은 요구의 원리 근거로 쓴다.
    """

    build_min: int = Field(ge=10, le=180)  # 1단계 구현
    debug_min: int = Field(ge=5, le=60)  # 2단계 디버깅 (1단계 제출 뒤 공개)


class DebugStageDraft(Schema):
    """2단계 디버깅 (LLM 이 쓰는 부분). 공개 시점·시간은 결정적으로 붙인다 (spec/feature.py apply)."""

    scenario: str = Field(min_length=1, max_length=2000)  # 배포 후 겉 증상 — 원인 개념 없이
    trap_ids: list[str] = Field(min_length=1, max_length=3)
    artifacts: list[Artifact] = Field(min_length=3, max_length=6)
    max_requests: int = Field(ge=1, le=3)
    deliverables: list[str] = Field(min_length=1, max_length=3)
    observables: list[Observable] = Field(min_length=1, max_length=6)

    @field_validator("deliverables", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v


class DraftFeatureOutput(Schema):
    """기능 개발형 출력 — 이슈형 v3 와 겹치는 필드는 이름·의미가 같다 (승인 화면·평가 도구·검사 재사용)."""

    title: str = Field(min_length=1, max_length=120)  # 기능 이름 — 구현 방법·숨은 요구 없이
    scenario: str = Field(min_length=1, max_length=3000)  # 업무 맥락·사용자 요구
    requirements: list[Requirement] = Field(min_length=2, max_length=6)
    acceptance_public: list[PublicAcceptance] = Field(min_length=2, max_length=6)
    deliverables: list[str] = Field(min_length=2, max_length=4)
    artifacts: list[Artifact] = Field(min_length=5, max_length=8)  # 1단계 자료 (요청 시 공개)
    max_requests: int = Field(ge=1, le=4)
    clarifications: list[Clarification] = Field(min_length=2, max_length=5)
    max_clarifications: int = Field(ge=1, le=4)
    traps: list[Trap] = Field(min_length=1, max_length=3)  # 숨은 요구
    acceptance_hidden: list[HiddenAcceptance] = Field(min_length=1, max_length=6)
    accepted_designs: list[AcceptedDesign] = Field(min_length=2, max_length=4)
    debug_stage: DebugStageDraft
    invariant: str = Field(min_length=1, max_length=600)
    variable: list[str] = Field(min_length=1, max_length=6)
    observables: list[Observable] = Field(min_length=1, max_length=10)  # 1단계 관찰 행동
    behavioral_anchors: BehavioralAnchors
    tech_used: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("deliverables", "variable", "tech_used", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v


class DraftFeatureInputV2(DraftTaskInput):
    """기능형 v2 입력 — 공개 사례 원문 대신 사람이 적어 둔 기능형 재료(incidents/*.json feature) 블록.

    숨은 요구·허용 설계·2단계 원인은 재료 그대로 쓰고(코드가 채움), LLM 은 공고 업종에 맞춘 겉모습만 쓴다.
    """

    build_min: int = Field(ge=10, le=180)
    debug_min: int = Field(ge=5, le=60)
    feature_seed: str = Field(
        min_length=1
    )  # spec/feature.py seed_block — 원 사례 조직명·제목·URL·원문 수치 없음


class DraftFeatureOutputV2(Schema):
    """기능형 v2 출력 — 숨은 요구(traps)·허용 설계는 없다 (재료에서 코드가 채움, spec/feature.py fill_from_seed).
    숨은 요구 id 는 t1 하나 (재료의 숨은 요구)."""

    title: str = Field(min_length=1, max_length=120)
    scenario: str = Field(min_length=1, max_length=3000)
    requirements: list[Requirement] = Field(min_length=2, max_length=6)
    acceptance_public: list[PublicAcceptance] = Field(min_length=2, max_length=6)
    deliverables: list[str] = Field(min_length=2, max_length=4)
    artifacts: list[Artifact] = Field(min_length=5, max_length=8)
    max_requests: int = Field(ge=1, le=4)
    clarifications: list[Clarification] = Field(min_length=2, max_length=5)
    max_clarifications: int = Field(ge=1, le=4)
    acceptance_hidden: list[HiddenAcceptance] = Field(min_length=1, max_length=4)
    debug_stage: DebugStageDraft
    invariant: str = Field(min_length=1, max_length=600)
    variable: list[str] = Field(min_length=1, max_length=6)
    observables: list[Observable] = Field(min_length=1, max_length=10)
    behavioral_anchors: BehavioralAnchors
    tech_used: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("deliverables", "variable", "tech_used", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v


class ClarificationDraft(Schema):
    """v3·v4 추가 요구 — 두 층 답 필수. 질문 한도는 코드가 정한다 (v4)."""

    clarification_id: str = Field(pattern=r"^c\d{1,2}$")
    topic: str = Field(min_length=1, max_length=120)
    match_terms: list[str] = Field(
        min_length=1,
        max_length=5,
        description="이 주제를 정확히 짚은 질문에 들어 있을 낱말·짧은 구",
    )
    answer_hint: str = Field(
        min_length=1,
        max_length=400,
        description="모호한 답 — 사실이 있다는 것만, 수치·이름·결론 없이",
    )
    answer: str = Field(
        min_length=1, max_length=600, description="구체 답 — 관찰 사실과 수치, 결론·해법 없이"
    )
    reveals: list[str] = Field(
        default_factory=list, description='숨은 요구 단서면 ["t1"], 아니면 []'
    )


class DebugStageDraftV4(Schema):
    """v4 2단계 — 숨은 요구 연결·요청 한도·관찰 행동·공통 산출물 문구는 코드가 붙인다."""

    scenario: str = Field(min_length=1, max_length=2000, description="배포 뒤 겉 증상만, 원인 없이")
    artifacts: list[Artifact] = Field(min_length=3, max_length=6)
    deliverables: list[str] = Field(min_length=1, max_length=2)


class DraftFeatureOutputV4(Schema):
    """기능형 v4 출력 — 계산할 수 있는 칸(자료 번호·요청 한도·질문 한도·관찰 행동·2단계 연결)은 빼고 코드가 채운다
    (spec/feature.py fill_from_seed_v4). 숨은 요구·허용 설계도 재료에서 코드가 채운다."""

    title: str = Field(min_length=1, max_length=120, description="기능 이름 — 해법·원인 없이")
    scenario: str = Field(
        min_length=1,
        max_length=3000,
        description="업무 맥락과 사용자 요구 — 장애·원인 없이, 자료 안내 문장 없이",
    )
    requirements: list[Requirement] = Field(min_length=2, max_length=6)
    acceptance_public: list[PublicAcceptance] = Field(min_length=2, max_length=6)
    deliverables: list[str] = Field(min_length=2, max_length=4)
    artifacts: list[Artifact] = Field(min_length=5, max_length=8, description="1단계 자료")
    clarifications: list[ClarificationDraft] = Field(min_length=2, max_length=5)
    acceptance_hidden: list[HiddenAcceptance] = Field(min_length=1, max_length=4)
    debug_stage: DebugStageDraftV4
    concepts: list[str] = Field(
        min_length=1,
        max_length=3,
        description="지원자가 AI 보다 먼저 말해야 할 설계 결정 개념 (짧은 명사구)",
    )
    invariant: str = Field(min_length=1, max_length=600)
    variable: list[str] = Field(min_length=1, max_length=6)
    behavioral_anchors: BehavioralAnchors
    tech_used: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("deliverables", "variable", "tech_used", "concepts", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v


class ClarificationDraftV5(Schema):
    """v5 추가 요구 — 단서(reveals 있음)는 모호한 답 대신 단서 자료(1단계 자료 제목)를 지목하고, 모호한 답 문장은
    코드가 만든다 ("'<제목>' 자료에서 확인할 수 있습니다.", spec/feature.py fill_from_seed_v5) [2026-11-06 사용자 (B)].
    단서가 아닌 추가 요구는 모호한 답을 쓴다 (사실이 드러나도 해가 없음)."""

    clarification_id: str = Field(pattern=r"^c\d{1,2}$")
    topic: str = Field(min_length=1, max_length=120)
    match_terms: list[str] = Field(
        min_length=1,
        max_length=6,
        description="이 주제를 정확히 짚은 질문에 들어 있을 말 — 공개 글에 없는 말, 두 단어 이상, 동의어·영문 표기도",
    )
    answer: str = Field(
        min_length=1, max_length=600, description="구체 답 — 관찰 사실과 수치, 결론·해법 없이"
    )
    reveals: list[str] = Field(
        default_factory=list, description='숨은 요구 단서면 ["t1"], 아니면 []'
    )
    hint_source: str | None = Field(
        default=None,
        max_length=120,
        description="단서면 필수 — 이 사실이 보이는 1단계 자료의 title 을 글자 그대로",
    )
    answer_hint: str | None = Field(
        default=None,
        max_length=400,
        description="단서가 아니면 필수 — 모호한 답 (수치·이름 없이)",
    )

    @model_validator(mode="after")
    def _tier(self) -> ClarificationDraftV5:
        if self.reveals and not (self.hint_source or "").strip():
            raise ValueError("단서 추가 요구(reveals 있음)는 hint_source(1단계 자료 title)가 필요")
        if not self.reveals and not (self.answer_hint or "").strip():
            raise ValueError("단서가 아닌 추가 요구는 answer_hint 가 필요")
        return self


class DraftFeatureOutputV5(DraftFeatureOutputV4):
    """기능형 v5 출력 — v4 + 단서 추가 요구는 단서 자료를 지목 (모호한 답은 코드가 만듦)."""

    clarifications: list[ClarificationDraftV5] = Field(min_length=2, max_length=5)  # type: ignore[assignment]


class DraftFeatureOutputV3(DraftFeatureOutputV2):
    """기능형 v3 출력 — v2 + 추가 요구의 두 층 답 (prompts/draft_feature_task/v3.md)."""

    clarifications: list[ClarificationDraft] = Field(min_length=2, max_length=5)  # type: ignore[assignment]


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


class FieldFixInput(Schema):
    """명세 칸 부분 수리 (prompts/fix_feature_fields) — 검사에 걸린 칸의 글만 고친다 (spec/patch.py). 공고·재료 글만 — 지원자 문서 없음."""

    fields: str = Field(min_length=1)  # "[경로] 글" 줄 묶음 — 고칠 칸들
    rules: str = Field(min_length=1)  # "- 칸들: 빼야 할 말 — 이유" 줄 묶음


class FixedField(Schema):
    path: str = Field(min_length=1, max_length=80, description="입력의 [경로] 그대로")
    text: str = Field(
        min_length=1, max_length=2000, description="고쳐 쓴 글 — 빼야 할 말과 같은 뜻의 말 없이"
    )


class FieldFixOutput(Schema):
    fields: list[FixedField] = Field(min_length=1, max_length=20)
