"""공개 사례 (부서 결정적 사건 대용) — incidents/public_v1/<직군>.json. 지원자 데이터 아님."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema


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
