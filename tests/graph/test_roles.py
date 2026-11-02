"""역할 규칙 판정 (불변식 6). 데이터는 가상."""

from __future__ import annotations

import pytest

from task_spec.nodes.roles import decide_role, field_of_lines, locate_field
from task_spec.schemas import Claim

TEXT = """가상 데이터 — 실존 인물 아님
경력
[ORG_1] 데이터 엔지니어 2023.01 ~ 2024.12
⟦문제 정의⟧ 주문 로그 집계가 하루 6시간 걸렸다.
⟦과정⟧ Airflow DAG 를 재구성했다.
Spark 집계를 파티션 단위로 나눴다.
⟦기여한 점⟧ 집계 시간을 6시간에서 40분으로 줄였다.
신규 파이프라인 아키텍처를 정의했다.
⟦담당업무⟧ 데이터 파이프라인 운영
⟦성과⟧ 월 비용 20% 절감
⟦사용 기술⟧ Airflow, Spark

자기소개
저는 팀 프로젝트를 이끌었습니다.
"""


def _claim(quote: str, action: str = "행동") -> Claim:
    return Claim(
        claim_id="c1", doc_id="d1", action=action, quote=quote, field=locate_field(TEXT, quote)
    )


@pytest.mark.parametrize(
    ("quote", "field", "role", "basis"),
    [
        ("Airflow DAG 를 재구성했다.", "과정", "participate", "field:과정"),
        (
            "Spark 집계를 파티션 단위로 나눴다.",
            "과정",
            "participate",
            "field:과정",
        ),  # 칸 다음 줄도 같은 칸
        ("집계 시간을 6시간에서 40분으로 줄였다.", "기여한 점", "lead", "field:기여한 점"),
        (
            "신규 파이프라인 아키텍처를 정의했다.",
            "기여한 점",
            "design",
            "field:기여한 점+design_term",
        ),
        ("데이터 파이프라인 운영", "담당업무", "participate", "field:담당업무"),
        ("주문 로그 집계가 하루 6시간 걸렸다.", "문제 정의", None, "field:문제 정의"),
        ("월 비용 20% 절감", "성과", None, "field:성과"),
        ("Airflow, Spark", "사용 기술", None, "field:사용 기술"),
        (
            "저는 팀 프로젝트를 이끌었습니다.",
            None,
            None,
            "no_field",
        ),  # 칸 밖 — '이끌었다' 여도 판정 안 함
        (
            "⟦과정⟧ Airflow DAG 를 재구성했다.",
            "과정",
            "participate",
            "field:과정",
        ),  # 모델이 칸 표시까지 인용
    ],
)
def test_role_table(quote: str, field: str | None, role: str | None, basis: str) -> None:
    c = _claim(quote)
    assert c.field == field
    assert decide_role(c) == (role, basis)


def test_design_term_ignored_outside_design_fields() -> None:
    text = "⟦과정⟧ 스키마를 설계했다."
    c = Claim(
        claim_id="c",
        doc_id="d",
        action="스키마 설계",
        quote="스키마를 설계했다.",
        field=locate_field(text, "스키마를 설계했다."),
    )
    assert decide_role(c) == ("participate", "field:과정")  # 과정 칸은 설계 표현이 있어도 참여


def test_field_ends_at_header_and_section() -> None:
    fields = field_of_lines("⟦과정⟧ a\nb\n가상회사 2022.01 ~ 2023.01\nc\n학력\nd")
    assert fields == ["과정", "과정", None, None, None, None]
