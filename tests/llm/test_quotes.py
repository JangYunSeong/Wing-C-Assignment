from __future__ import annotations

import pytest

from task_spec.llm import verify_quote

SOURCE = "⟦담당업무⟧ [ORG_1] 에서  데이터 파이프라인을\n설계했다. ⟦성과⟧ 처리 시간 30% 단축."


@pytest.mark.parametrize(
    ("quote", "ok"),
    [
        ("데이터 파이프라인을 설계했다.", True),  # 줄바꿈·공백 정규화
        ("⟦담당업무⟧ [ORG_1] 에서 데이터 파이프라인을 설계했다.", True),  # 칸 표시까지 인용
        ("[ORG_1] 에서 데이터 파이프라인을 설계했다.", True),  # 가명화 토큰은 원문 일부
        ("처리 시간 30% 단축.", True),
        ("처리 시간 40% 단축.", False),  # 수치 변경
        ("데이터 파이프라인을 구축했다.", False),  # 바꿔 쓰기
        ("", False),
        ("⟦담당업무⟧", False),  # 표시만
    ],
)
def test_verify_quote(quote: str, ok: bool) -> None:
    assert verify_quote(quote, SOURCE) is ok


def test_quote_survives_line_break_inside_a_word() -> None:
    """PDF·복사본은 단어 중간에서 줄이 끊긴다 — 모델이 붙여 인용해도 통과 (2026-11-04, 한 이력서 주장 9개 중 5개 폐기)."""
    src = "⟦기여한 점⟧ 크롤링 및 전처리, 데이터베\n이스 및 서비스 아키텍쳐 설계, 로깅 시스템 구\n현"
    assert verify_quote("데이터베이스 및 서비스 아키텍쳐 설계", src)
    assert verify_quote("로깅 시스템 구현", src)
    assert not verify_quote(
        "데이터베이스 및 서비스 아키텍처 설계", src
    )  # 글자가 다르면 여전히 실패
