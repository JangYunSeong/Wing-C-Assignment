"""공개 사례 (부서 결정적 사건 대용) — incidents/public_v1/<직군>.json. 지원자 데이터 아님."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema

NeedKind = Literal["stateful_target", "transition_rules", "empty_record_entity"]


class ContractNeed(Schema):
    """이 숨은 요구를 시험하려면 공개 계약에 꼭 있어야 하는 요소 — 계약을 기계적으로 대조한다 (starter/contract._needs).

    2026-11-07~08 알파 평가: 계약이 숨은 요구에 필요한 것을 빠뜨렸다 — 상태를 바꿀 대상 엔티티가 없고(감사 기록에 시드), 상태 전이 규칙이
    없어 숨은 테스트와 시작 코드가 서로 다른 전이를 짐작. 숨은 요구는 재료마다 다르므로 템플릿이 아니라 재료에 선언한다.
    - stateful_target: 상태를 가진 대상 엔티티 — 이미 있음(existing), 시드 행이 있고, 숨은 케이스 data 가 그 엔티티
    - transition_rules: 그 대상의 상태 필드(또는 상태를 바꾸는 요청 필드)에 허용 전이 규칙(A→B)이 적혀 있음
    - empty_record_entity: 요청마다 쌓이는 기록 엔티티 — 새로 만듦(existing 아님), 처음 0행
    """

    kind: NeedKind
    note: str = ""  # 프롬프트·재요청에 붙는 설명 (재료의 말로)


class FeatureSeed(Schema):
    """공개 사례 → 기능 개발형(1단계 구현 → 2단계 디버깅) 공통 과제 재료 [2026-11-06 사용자 결정].

    "어떤 기능을 만들 때 흔한 구현이 이 사고를 내는가"를 사람이 미리 적어 둔 것. 생성(draft_feature_task v2)은
    숨은 요구·허용 설계·2단계 원인을 여기서 그대로 쓰고, LLM 은 공고 업종에 맞춘 겉모습(상황 문장·공개 요구·자료 본문)만 쓴다.
    """

    business_need: str = Field(
        min_length=1
    )  # 만들 기능과 업무 요구 — 사고·증상·원인 언급 없이 (1단계 상황 재료)
    naive_impl: str = Field(min_length=1)  # 공개 요구만 보고 짜는 흔한 구현 (AI 첫 답)
    hidden_requirement: str = Field(min_length=1)  # 이 회사 운영 조건에서 지켜야 할 숨은 요구
    trigger: str = Field(min_length=1)  # 흔한 구현을 깨는 운영 조건 — 1단계 자료에서 보여야 함
    why_naive_breaks: str = Field(min_length=1)  # 그 조건에서 흔한 구현이 깨지는 이유
    discoverable_by: list[str] = Field(min_length=1)  # 1단계에서 조건이 보이는 자료 종류
    accepted_designs: list[str] = Field(
        min_length=2
    )  # 설계 단계 정답 — accepted_fixes 형식 (`[설계] … — 조건: … — 함정: …`)
    stage2_symptom: str = Field(min_length=1)  # 흔한 구현을 배포한 뒤 겉 증상 (원인 개념 없이)
    # 2단계에서 원인을 가르는 원시 자료 종류 — 첫 항목이 핵심 증거 (1단계 자료에 같은 종류가 있으면 재요청)
    stage2_evidence: list[str] = Field(min_length=1)
    stage2_cause_terms: list[str] = Field(
        min_length=1
    )  # 2단계에서 먼저 말할 원인 개념 — 지원자 화면 금지
    # 1단계 지원자 화면(제목·상황·공개 요구·수용 기준·추가 요구 답·자료 제목)과 2단계 상황에 나오면 숨은 요구가 새는 말
    leak_terms: list[str] = Field(default_factory=list)
    # 운영 조건(trigger)을 드러내는 말과 흔한 바꿔 쓰기 [2026-11-06 사용자] — 1단계 제목·상황·공개 요구·공개 수용 기준에
    # 나오면 재요청. 자료 본문·구체 답(answer)에는 써도 된다 (단서는 거기서 보여야 함). 프롬프트에는 넣지 않는다 (검사 전용).
    # business_need 에 나온 말은 공개된 업무 요구라 검사에서 뺀다.
    trigger_terms: list[str] = Field(default_factory=list)
    task_shape: Literal["code", "analysis"] = "code"
    starter: str | None = None  # 검증된 시작 코드 예제 (data/feature_starters/<id>) — 없으면 명세만
    reviewed: bool = False  # 사람이 검토했는지 — false 면 명세 review_flags 에 남긴다
    contract_needs: list[ContractNeed] = Field(default_factory=list)  # 계약에 꼭 있어야 할 요소


class PublicIncident(Schema):
    incident_id: str = Field(pattern=r"^pub:[a-z_.]+:[a-z0-9-]+$")
    sub: str
    title: str
    org: str
    url: str
    published: str
    source_lang: Literal["ko", "en"]
    symptom: str
    naive_fix: str
    root_cause: str
    why_naive_fails: str
    key_insight: str
    deep_structure: str
    surface: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)
    verified: bool
    notes: str = ""
    hypotheses: list[str] = Field(
        default_factory=list
    )  # 지원자가 세우고 기각할 가설 — 기각 근거 자료
    avoid_terms: list[str] = Field(
        default_factory=list
    )  # 회사·제품·사건 이름 — 과제 어디에도 쓰지 않음
    hidden_terms: list[str] = Field(
        default_factory=list
    )  # 원인 기술 용어 — 지원자에게 보이는 곳(제목·상황·제출물)에만 쓰지 않음, 함정 설명엔 허용
    accepted_fixes: list[str] = Field(
        default_factory=list
    )  # 정답으로 인정할 조치 — 함정·오답으로 쓰지 않음 (흔한 처방과 겉이 같아도 원인 근거가 있으면 정답)
    source_literals: list[str] = Field(
        default_factory=list
    )  # 원문의 오류 문구·지표 값 — 생성 입력에 넣지 않고, 과제에 2개 이상 함께 나오면 복사로 본다
    task_pattern: Literal["qa_mutation"] | None = None
    feature: FeatureSeed | None = (
        None  # 기능 개발형 재료 (없으면 그 직군은 기능형 기본값 대상이 아님)
    )
