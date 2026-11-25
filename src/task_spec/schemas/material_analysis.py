"""재료 입구 분석 (#301 PR 3) — 회사 재료(인터뷰)가 빌드 하네스에서 재현되는지 승인 전에 짚는다.

2026-11-24 세션 유지 알파: 흔한 구현(요청마다 세션 테이블 조회·갱신)이 Hikari 기본 대기에서 숨은 케이스를 통과했다 — 재료가 재현되는지
아무도 빌드 전에 보지 않아 과제 승인까지 가고 빌드 마지막 ③ 에서야 드러났다. 분석은 승인을 막지 않고 과제 검토 표시로만 알린다
(LLM 오판으로 담당자 진행이 멈추지 않게). 출력 모델은 프롬프트 본문에 렌더된다 — 칸·설명을 바꾸면 출력 스키마 골든이 깨진다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from task_spec.schemas.base import Schema
from task_spec.schemas.pseudo_text import PseudoTextField


class AnalyzeMaterialInput(Schema):
    """material: 가명화한 재료 블록 (흔한 구현·깨지는 이유·허용 설계·사유·조절값·메모), template_json: 템플릿 사유·조절값 (코드 값)."""

    material: PseudoTextField
    template_json: str


class MaterialAnalysis(Schema):
    mechanism: str = Field(
        min_length=1,
        max_length=300,
        description="흔한 구현이 운영 조건에서 깨지는 기전 한 줄 — 요청 하나가 무엇을 얼마나 쥐고, 무엇이 바닥나거나 쌓이는지",
    )
    check: str = Field(
        default="",
        max_length=800,
        description="흔한 구현과 허용 설계가 갈리는 조건을 직접 따진 것 — 요청 하나가 쥐는 자원·시간, 동시에 몰리면 어떻게 되는지",
    )
    reason: str = Field(
        min_length=1,
        max_length=60,
        description="그 기전이 채점에서 보일 실패 사유 — failure_reasons 의 code 하나",
    )
    split_params: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="흔한 구현은 떨어지고 허용 설계는 통과하게 가르는 조절값 — harness_params 의 name (필요 없으면 빈 목록)",
    )
    reproducible: Literal["yes", "no", "unsure"] = Field(
        description="yes=템플릿 조절값으로 흔한 구현만 떨어뜨릴 수 있음 / no=어떤 값으로도 흔한 구현과 허용 설계가 함께 통과하거나 "
        "함께 떨어짐(하네스가 그 조건을 만들 수 없음) / unsure=재료 정보로 가릴 수 없음"
    )
    gaps: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="판정을 가르는 데 재료에 빠진 정보 (예: 요청 하나의 DB 문장 수) — 없으면 빈 목록",
    )
