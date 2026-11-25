"""함정 틀 (#318, docs/design/trap-frame.md) — 명세를 쓰기 전에 이 공고에서 숨길 것·공개할 것·공개 쪽 금지·허용 설계 대조를 정한다.

재료(`FeatureSeed`)는 '무슨 사고인가'를 담고, 틀은 '이 공고에서 무엇을 숨기고 무엇을 공개하나'를 담는다. 틀이 정한 것을 재료에 덮어쓴
사본(틀 적용 재료)을 코드가 만들어 명세 초안·계약 단계가 원 재료 대신 쓴다 (interview/trap_frame.apply_frame). 틀 적용 재료와 틀은
그 공고의 공통 과제 저장 행에 함께 보관한다 [확정: 2026-11-25 사용자 — D1 (나)]. 출력 모델(TrapFrame)은 프롬프트 본문에 렌더된다.

문서 칸 이름과의 대응: `public_forbidden` = `forbidden_terms`(낱말) + `forbidden_storage`(저장 엔티티 단위).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema
from task_spec.schemas.incident import FeatureSeed
from task_spec.schemas.pseudo_text import PseudoTextField

TrapKind = Literal["design", "behavior"]


class TrapFrameInput(Schema):
    """material: 가명화한 재료 블록 JSON (재료 칸 + 업무 요구·계약 필요 요소 + 재료 분석 결과 + 보강 문답).
    template_json: 템플릿 실패 사유·조절값. kinds: 고를 수 있는 틀 종류 (코드가 템플릿을 보고 정한다 — D3)."""

    material: PseudoTextField
    template_json: str
    kinds: str


class ForbiddenStorage(Schema):
    entity: str = Field(
        min_length=1,
        max_length=60,
        description="공개 계약에 엔티티(테이블)로 두면 안 되는 것 — 계약에 쓸 엔티티 이름 그대로, 영문 PascalCase 단수 (예: Session, "
        "RequestUsage). 계약 검사가 엔티티 이름으로 대조한다",
    )
    why: str = Field(
        min_length=1,
        max_length=200,
        description="그것을 공개 계약의 엔티티로 두면 흔한 구현으로 이끄는 이유",
    )


class DesignCheck(Schema):
    design: str = Field(
        min_length=1,
        max_length=300,
        description="허용 설계 — 재료 accepted_designs 의 한 항목을 앞부분 그대로 (다른 항목과 구별되게)",
    )
    holds: Literal["yes", "no", "partial"] = Field(
        description="그 설계로 재료의 업무 요구(business_need)를 모두 지킬 수 있는가 — yes·no·partial"
    )
    requirement: str = Field(
        default="",
        max_length=200,
        description="지키지 못하는 업무 요구 — holds 가 yes 면 빈 글",
    )
    why: str = Field(min_length=1, max_length=300, description="판정 이유 (기전으로)")


class TrapFrame(Schema):
    kind: TrapKind = Field(
        description="틀 종류 — 입력 kinds 에 있는 것만. design: 설계 조건을 숨긴다 / behavior: 관찰 가능한 동작을 숨긴다"
    )
    hidden_requirement: str = Field(
        min_length=1,
        max_length=400,
        description="이 공고의 숨은 요구 — design 이면 재료의 숨은 요구를 글자 그대로 두고, 틀이 공개 쪽 저장 방식을 막았을 때만 그 설계 "
        "조건 한 문장을 덧붙인다. behavior 면 관찰 가능한 동작(특정 상태 코드 등)",
    )
    public_rules: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="공개할 것 — design 이면 지원자가 설계할 때 볼 운영 조건(풀 크기·지연 가능성·피크 동시 요청 등), behavior 면 "
        "오류 응답 규칙(특정 상태 코드). 숨은 요구 자체·해법·흔한 구현의 기전은 쓰지 않는다",
    )
    forbidden_terms: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="공개 쪽(공개 요구·시나리오·자료·사실·계약)에 쓰면 흔한 구현의 기전이나 해법을 드러내는 낱말 — 업무 요구에 이미 "
        "나온 말(예: 세션)은 넣지 않는다",
    )
    forbidden_storage: list[ForbiddenStorage] = Field(
        default_factory=list,
        max_length=3,
        description="공개 계약에 엔티티로 두면 흔한 구현의 저장 방식으로 이끄는 것 — 없으면 빈 목록",
    )
    stateful_target_forbidden: bool = Field(
        default=False,
        description="재료 contract_needs 의 stateful_target(상태를 가진 대상 엔티티)이 흔한 구현의 저장 방식인가 — 요청이 만드는 것을 "
        "미리 채운 테이블로 바꾸게 되면 true. stateful_target 이 없으면 false",
    )
    stateful_target_why: str = Field(
        default="",
        max_length=300,
        description="stateful_target_forbidden 의 근거 — true 면 반드시 적는다",
    )
    design_matrix: list[DesignCheck] = Field(
        default_factory=list,
        max_length=6,
        description="허용 설계(accepted_designs)마다 업무 요구를 지키는지",
    )
    reason: str = Field(
        min_length=1,
        max_length=60,
        description="흔한 구현이 채점에서 떨어질 실패 사유 — failure_reasons 의 code 하나",
    )
    evidence: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="그 사유가 성립하는 근거로 재료에 있는 기전 (예: 요청마다 DB 연결을 쥐는 시간) — 재료에 없으면 쓰지 않는다",
    )
    gaps: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="그 사유의 근거로 재료에 빠진 정보 — 없으면 빈 목록",
    )
    trigger: str = Field(
        default="",
        max_length=400,
        description="behavior 일 때만 — 바뀐 운영 조건 (design 이면 빈 글)",
    )
    why_naive_breaks: str = Field(
        default="",
        max_length=400,
        description="behavior 일 때만 — 그 조건에서 흔한 구현이 깨지는 이유 (design 이면 빈 글)",
    )


class FramedMaterial(Schema):
    """틀 적용 재료와 틀 — 그 공고의 공통 과제 저장 행(`common_task.framed`)에 보관하고, 빌드 계약 단계가 있으면 원 재료 대신 쓴다
    (builder `incident_for`). 금지 저장 엔티티(검사 4)도 여기서 읽는다."""

    seed: FeatureSeed
    frame: TrapFrame
