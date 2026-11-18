"""builder 실제 단계의 실패 사유 — 명세의 숨은 요구·테스트 문구를 사유에 넣지 않는다. LLM·podman 없이."""

from __future__ import annotations

from pathlib import Path

from task_spec.builder.steps import (
    FAILED_OVERLAYS,
    _counted,
    _kind,
    contract_failure,
    keep_overlays,
)
from task_spec.nodes.starter.plan_contract import PlanResult
from task_spec.schemas import GeneratedFile
from task_spec.starter.static_check import Overlay


def test_reason_keeps_only_the_kind_of_problem() -> None:
    """2026-11-12 스모크: 계약 실패 사유에 숨은 요구 문장('날짜별 입력 폴더: …')이 그대로 들어갔다."""
    problems = [
        "숨은 요구에 필요한 것이 계약에 없음 — 가상 요구: 가상 숨은 요구 본문 SECRET",
        "시드 행 수가 부족: 가상 엔티티 SECRET",
        "숨은 요구에 필요한 것이 계약에 없음 — 다른 가상 요구",
    ]
    got = _counted([_kind(p) for p in problems])
    assert got == "숨은 요구에 필요한 것이 계약에 없음 ×2 | 시드 행 수가 부족"
    assert "SECRET" not in got


def test_harness_problem_kind_stops_before_value_and_quote() -> None:
    """2026-11-15: HiddenPlan 조절값 문장은 머리에 값과 자료 인용이 온다 — 조절값 이름까지만."""
    item = "HiddenPlan request_timeout_s=1 이 근거 인용 '가상 자료 문장 SECRET: 500ms' 에서 재료 규칙으로 만든 값 4 과 다름"
    assert _kind(item) == "HiddenPlan request_timeout_s"
    assert (
        _kind("명세 자료에 운영 값 query_delay_ms(15, 16)이 있으니 넣는다")
        == "명세 자료에 운영 값 query_delay_ms"
    )


def test_contract_failure_lists_call_errors_gaps_and_problems_together() -> None:
    """2026-11-15 운영: 세 번 중 한 번 빈 응답이면 첫 비어 있지 않은 목록(fail_reasons)만 실려 검사 문제가 가려졌다."""
    res = PlanResult(
        fail_reasons=[
            "EmptyResponse(finish_reason=stop): plan_contract@31: 빈 응답 (finish_reason=stop, 재시도 없음)"
        ],
        problems=[
            "HiddenPlan request_timeout_s=1 이 근거 값 글자 '500ms' 를 규칙대로 바꾼 값 2 와 다름",
            "HiddenPlan query_delay_ms 의 근거 값 글자 'SECRET' 이 근거 인용 안에 없음",
        ],
    )
    got = contract_failure(res)
    assert got == (
        "호출 실패 EmptyResponse(finish_reason=stop) · "
        "검사 문제 HiddenPlan request_timeout_s | HiddenPlan query_delay_ms 의 근거 값 글자"
    )
    assert "SECRET" not in got and "500ms" not in got
    gaps = PlanResult(
        spec_gaps=[
            "명세 1단계 자료·추가 요구 구체 답에 운영 수치 pool_size 이 없음 — 명세 수정 필요"
        ]
    )
    assert (
        contract_failure(gaps)
        == "명세 결함 명세 1단계 자료·추가 요구 구체 답에 운영 수치 pool_size 이 없음"
    )
    assert contract_failure(PlanResult()) == "사유 없음"


def test_counted_keeps_the_first_kinds_and_says_how_many_more() -> None:
    assert _counted(["a", "b", "a", "c", "d"], n=2) == "a ×2 | b 외 2종"


def test_failed_build_keeps_the_last_overlays_inside_the_work_dir(tmp_path: Path) -> None:
    """2026-11-18 ct-10eb910705dd-b2: 수리 루프가 실패한 빌드는 검증 로그만 남아 허용 구현·숨은 테스트를 볼 수 없었다 —
    마지막 생성물을 작업 폴더 overlays/<키>/ 에 (알파 실행 폴더와 같은 모양)."""
    overlays = {
        "hidden": Overlay(
            kind="hidden",
            files=(GeneratedFile(path="src/__hidden__/hc1.test.tsx", content="가상 hc1"),),
        ),
        "d1": Overlay(
            kind="ref_accepted",
            label="d1",
            files=(GeneratedFile(path="src/App.tsx", content="가상 d1"),),
        ),
    }
    root = tmp_path / "work" / FAILED_OVERLAYS
    assert keep_overlays(overlays, root) == ["hidden", "d1"]
    assert (root / "hidden/src/__hidden__/hc1.test.tsx").read_text("utf-8") == "가상 hc1"
    assert (root / "d1/src/App.tsx").read_text("utf-8") == "가상 d1"
