"""공개 사례 (부서 결정적 사건 대용) — incidents/public_v1/<직군>.json. 지원자 데이터 아님."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from task_spec.schemas.base import Schema

NeedKind = Literal[
    "stateful_target",
    "transition_rules",
    "empty_record_entity",
    "mutation_endpoint",
    "result_screen",
    "dated_input_folders",
]


class ContractNeed(Schema):
    """이 숨은 요구를 시험하려면 공개 계약에 꼭 있어야 하는 요소 — 계약을 기계적으로 대조한다 (starter/contract._needs).

    2026-11-07~08 알파 평가: 계약이 숨은 요구에 필요한 것을 빠뜨렸다 — 상태를 바꿀 대상 엔티티가 없고(감사 기록에 시드), 상태 전이 규칙이
    없어 숨은 테스트와 시작 코드가 서로 다른 전이를 짐작. 숨은 요구는 재료마다 다르므로 템플릿이 아니라 재료에 선언한다.
    - stateful_target: 상태를 가진 대상 엔티티 — 이미 있음(existing), 시드 행이 있고, 숨은 케이스 data 가 그 엔티티
    - transition_rules: 그 대상의 상태 필드(또는 상태를 바꾸는 요청 필드)에 허용 전이 규칙(A→B)이 적혀 있음
    - empty_record_entity: 요청마다 쌓이는 기록 엔티티 — 새로 만듦(existing 아님), 처음 0행
    - mutation_endpoint: 화면이 부르는 바꾸는 API (POST·PUT·PATCH·DELETE) — 프론트엔드 재료 (2026-11-09: 중복 제출·낙관적 갱신)
    - result_screen: 그 결과를 보여 주는 화면 요소 (계약 screens) — 바뀐 값이 화면에 보여야 판정할 수 있다
    - dated_input_folders: 입력이 날짜별 폴더(예 <raw>/YYYY-MM-DD/*.jsonl)로 쌓이고 명령은 그 상위 폴더와 날짜를 옵션으로 받는다 — 데이터 배치 재료 (2026-11-09)
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
    # 허용 설계의 장치가 코드에 드러나는 표지 (정규식) — 흔한 구현에는 없어야 한다. 흔한 구현이 숨은 케이스를 통과하고 이 표지가 있으면
    # 흔한 구현을 그 장치를 짚어 다시 만들고, 다시 만든 뒤에도 그렇면 "AI 흔한 구현이 기본으로 정답을 쓴다"는 재료 판정 (repair ③).
    # 2026-11-10 변별 실패 분석: 응답 순서 흔한 구현이 매 회차 React 정리 패턴(let cancelled …)을 써 — 허용 설계 그 자체.
    # 생성 단계 정적 검사로는 쓰지 않는다 (저장 코드 검증에서 흔한 구현이 제대로 떨어진 회차에도 걸린 경우가 있었다).
    fix_markers: list[str] = Field(default_factory=list)
    task_shape: Literal["code", "analysis"] = "code"

    @field_validator("fix_markers")
    @classmethod
    def _markers_compile(cls, v: list[str]) -> list[str]:
        for rx in v:
            re.compile(rx)
        return v

    starter: str | None = None  # 검증된 시작 코드 예제 (data/feature_starters/<id>) — 없으면 명세만
    reviewed: bool = False  # 사람이 검토했는지 — false 면 명세 review_flags 에 남긴다
    contract_needs: list[ContractNeed] = Field(default_factory=list)  # 계약에 꼭 있어야 할 요소
    # 이 숨은 요구를 어긴 흔한 구현이 실패할 때의 사유 코드 (템플릿 failure_reasons 중) — 비면 템플릿 목록 전체.
    # 2026-11-09 React 상류 평가: 계약 계획이 템플릿 사유 목록에서 숨은 요구(응답 경쟁)와 무관한 duplicate_request 를 골라
    # 올바른 구현도 떨어지는 케이스(같은 검색어를 두 번 입력하면 요청 1번)를 만들었다. 숨은 요구는 재료마다 다르므로 재료에 선언한다.
    reasons: list[str] = Field(default_factory=list)
    # 이 숨은 요구를 재는 데 꼭 필요한 하네스 조절값 (템플릿 harness_params 이름) — 템플릿의 required 와 합친다.
    # 2026-11-10 백엔드 재료 3건: 템플릿이 pool_size·concurrency 를 늘 필수로 둬 풀과 무관한 재료(목록 행마다 조회·재시도 중복)
    # 계약이 '명세 자료에 운영 수치 pool_size 없음' 으로 막혔다 — 무엇이 필수인지는 숨은 요구가 정한다.
    # None = 템플릿 required 그대로 (예전과 같음), [] = 필수 없음.
    harness: list[str] | None = None
    # 그 조절값을 자료의 어느 운영 조건에서 읽는지 (계약 입력의 조절값 설명에 붙는다) — 2026-11-10 목록 재료: 템플릿 설명
    # '채점 클라이언트의 요청 하나 시간 한도' 를 자료의 '응답 시간 목표' 와 잇지 못해 세 번 1초·2초·30초로 흔들렸다.
    harness_notes: dict[str, str] = Field(default_factory=dict)


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
    # 이 사례의 기능형 재료(feature)를 숨은 테스트로 시험할 수 있는 템플릿 (templates/<id>). 직군 기본 사례(<직군>.json)가 아닌 사례는 적어야 한다.
    # 2026-11-09 사용자 결정: 직군마다 여러 건 허용 — 기능형 과제는 직군 템플릿이 시험할 수 있는 사례를, 디버깅형은 기본 사례를 쓴다
    # (프론트엔드 기본 사례 '여러 탭이 같은 로컬 DB 에 씀'은 실제 브라우저가 필요해 jsdom 템플릿으로 시험할 수 없다).
    templates: list[str] = Field(default_factory=list)
    feature: FeatureSeed | None = (
        None  # 기능 개발형 재료 (없으면 그 직군은 기능형 기본값 대상이 아님)
    )
