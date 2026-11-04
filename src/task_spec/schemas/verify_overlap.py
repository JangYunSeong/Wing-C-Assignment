"""④ 공고 역량 → 주장 확인(top-down, prompts/verify_overlap) 입출력과 State 기록. [확정: 2026-11-04 사용자]

선택 경로(주장 → 검색 상위 10 → 주장별 0~2개)만으로는 겹침 재현율이 검색 상한에 묶인다.
공고 역량마다 그 이력서의 주장 전체를 보고 판정해 대조(compare)가 규칙으로 합친다 (nodes/verify_overlap.py).
claims 는 가명화본에서 나온 주장만 (PseudonymizedText — 불변식 2·3). competencies 는 온톨로지 텍스트만.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from task_spec.schemas.base import Schema
from task_spec.schemas.domain import OverlapLabel
from task_spec.schemas.pseudo_text import PseudoTextField


class VerifyOverlapInput(Schema):
    """프롬프트 템플릿 변수: {{competencies}}, {{claims}}."""

    competencies: str = Field(min_length=1)  # "- comp:id 라벨 — 설명" 줄 목록 (온톨로지 텍스트)
    claims: PseudoTextField  # "[c1] 행동: … / 문장: …" 줄 묶음


class VerifyVerdict(Schema):
    competency_id: str = Field(min_length=1)
    evidence_claims: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="근거 주장 표시 (c1, c2 …). action·related·mention 이면 1개 이상, none 이면 []",
    )
    reason: str | None = Field(default=None, max_length=300, description="근거 표현과 판단 한 문장")
    label: OverlapLabel = Field(
        description="action=이 역량을 직접 보여주는 행동 / related=가까운 역량의 행동으로 일부만 / "
        "mention=기술·도구 이름만 나오고 이 역량의 행동은 없음 / none=없음"
    )

    @field_validator("evidence_claims", mode="before")
    @classmethod
    def _drop_nulls(cls, v: object) -> object:
        return [x for x in v if x] if isinstance(v, list) else v


class VerifyOverlapOutput(Schema):
    """프롬프트에 그려지는 출력 스키마 (평가 때와 같게 고정 — 바꾸면 LLM 이 보는 지시가 달라진다)."""

    items: list[VerifyVerdict] = Field(min_length=1, max_length=60)


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class VerifyParsedVerdict(_Loose):
    """응답 파싱용 — 길이 한도 없이 받고 nodes/verify_overlap.normalize 가 자르고 정리한다.

    한도 하나(근거 7개, 이유 301자 …) 때문에 호출 전체가 실패하지 않게. label 도 문자열로 받아
    네 값이 아니면 그 역량만 버린다 (빠진 역량으로 다시 묻는다). reason 은 읽지 않는다.
    """

    competency_id: str = ""
    evidence_claims: list[str] = Field(default_factory=list)
    label: str = ""

    @field_validator("competency_id", "label", mode="before")
    @classmethod
    def _text(cls, v: object) -> object:
        return "" if v is None else str(v)

    @field_validator("evidence_claims", mode="before")
    @classmethod
    def _refs(cls, v: object) -> object:
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v if x] if isinstance(v, list) else []


class VerifyParsedOutput(_Loose):
    items: list[VerifyParsedVerdict] = Field(default_factory=list)

    @field_validator("items", mode="before")
    @classmethod
    def _objects(cls, v: object) -> object:
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


class OverlapCheck(Schema):
    """공고 역량 1건의 확인 결과 (검증 후). 판정 이유(LLM 자유 텍스트)는 State 에 남기지 않는다."""

    competency_id: str = Field(min_length=1)
    label: OverlapLabel
    claim_ids: list[str] = Field(default_factory=list)  # 근거 주장 id (none 이면 [])
    provenance_id: str | None = None


class OverlapCheckResult(Schema):
    """verify_overlap 노드 결과. ok = 공고 역량 전부 답 / partial = 다시 물어도 빠진 역량 있음 / failed = 호출 실패."""

    status: Literal["ok", "partial", "failed"]
    items: list[OverlapCheck] = Field(default_factory=list)
