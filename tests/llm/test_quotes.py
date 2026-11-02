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
