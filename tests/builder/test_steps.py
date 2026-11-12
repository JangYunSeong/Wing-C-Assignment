"""builder 실제 단계의 실패 사유 — 명세의 숨은 요구·테스트 문구를 사유에 넣지 않는다. LLM·podman 없이."""

from __future__ import annotations

from task_spec.builder.steps import _kinds


def test_reason_keeps_only_the_kind_of_problem() -> None:
    """2026-11-12 스모크: 계약 실패 사유에 숨은 요구 문장('날짜별 입력 폴더: …')이 그대로 들어갔다."""
    problems = [
        "숨은 요구에 필요한 것이 계약에 없음 — 가상 요구: 가상 숨은 요구 본문 SECRET",
        "시드 행 수가 부족: 가상 엔티티 SECRET",
        "숨은 요구에 필요한 것이 계약에 없음 — 다른 가상 요구",
    ]
    got = _kinds(problems)
    assert got == "숨은 요구에 필요한 것이 계약에 없음 | 시드 행 수가 부족"
    assert "SECRET" not in got
