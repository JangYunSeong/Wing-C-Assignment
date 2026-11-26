"""조절값 성립 검사 — 템플릿이 사유마다 선언한 식으로 계산. 가상 값만."""

from __future__ import annotations

import pytest

from task_spec.schemas.build import HiddenPlan
from task_spec.starter.feasibility import (
    evaluate,
    feasibility_problems,
    fit_feasible_rows,
    harness_env,
    parse,
)
from task_spec.starter.templates import load_template

JAVA = load_template("backend/java-spring-boot3-jpa").manifest


@pytest.mark.parametrize(
    "bad", ["__import__('os')", "a.b > 1", "open('x')", "'x' == 'x'", "[1][0] > 0", "f(a=1)"]
)
def test_only_arithmetic_comparisons_and_max_min_are_allowed(bad: str) -> None:
    with pytest.raises((ValueError, SyntaxError)):
        parse(bad)
    assert evaluate("max(30, a * 2 / 1000 + 5) >= 30 and not b < 1", {"a": 0, "b": 2})


def test_defaults_follow_the_harness() -> None:
    """계획에 없으면 하네스가 실제로 쓰는 값 — 쿼리 지연 20 (v4 부터, HARNESS.md 대체값), 요청 한도 max(30, 연결 대기 × 2 + 5) (GraderSupport.requestTimeout)."""
    assert harness_env({}, JAVA)["query_delay_ms"] == 20
    assert harness_env({}, JAVA)["request_timeout_s"] == 30
    assert harness_env({"connection_timeout_ms": 20000}, JAVA)["request_timeout_s"] == 45
    assert harness_env({"request_timeout_s": 2}, JAVA)["request_timeout_s"] == 2


def _plan(harness: dict[str, object], reason: str, rows: int = 200) -> HiddenPlan:
    return HiddenPlan.model_validate(
        {
            "harness": harness,
            "cases": [
                {
                    "case_id": "hc1",
                    "ac_id": "h1",
                    "trap_id": "t1",
                    "scenario": "가상 목록 조회",
                    "expect": "한도 안에 응답",
                    "intended_reason": reason,
                    "data": [{"entity_id": "m1", "rows": rows}],
                }
            ],
        }
    )


def test_list_query_per_row_must_be_able_to_time_out() -> None:
    """2026-11-10 nplus1-d: 200행 × 20ms = 4초 < 한도 30초 — 흔한 구현이 떨어질 수 없었다."""
    probs = feasibility_problems(_plan({"query_delay_ms": 20}, "request_timeout"), JAVA)
    assert probs and "rows=200" in probs[0] and "request_timeout_s=30" in probs[0]
    assert feasibility_problems(_plan({"query_delay_ms": 0}, "request_timeout"), JAVA)  # 지연 없음
    ok = _plan({"query_delay_ms": 11, "request_timeout_s": 1}, "request_timeout")  # 2.2초 ≥ 1초
    assert feasibility_problems(ok, JAVA) == []


def test_case_rows_are_raised_until_the_reason_can_hold() -> None:
    """2026-11-25 목록 조회 알파: 행 50 × 지연 12ms < 한도 2초를 계약 재요청 3회 안에 못 맞춰 실패 — 행 수는 운영 값이 아니라 케이스 안에서
    정하는 값이라 코드가 템플릿 식으로 계산해 늘린다. 상한(2000행) 안에서 못 맞추면 그대로 두어 검사가 다시 묻는다."""
    plan = _plan({"query_delay_ms": 12, "request_timeout_s": 2}, "request_timeout", rows=50)
    fixed, notes = fit_feasible_rows(plan, JAVA)
    assert fixed.cases[0].data[0].rows == 167 and feasibility_problems(fixed, JAVA) == []
    assert "행 50 → 167" in notes[0]
    ok = _plan({"query_delay_ms": 11, "request_timeout_s": 1}, "request_timeout")
    assert fit_feasible_rows(ok, JAVA) == (ok, [])
    far = _plan({"query_delay_ms": 1, "request_timeout_s": 30}, "request_timeout", rows=50)
    assert fit_feasible_rows(far, JAVA) == (far, [])
    # 행 수를 쓰지 않는 조건(풀 고갈)은 건드리지 않는다
    pool = _plan({"pool_size": 20, "concurrency": 10}, "pool_acquire_timeout")
    assert fit_feasible_rows(pool, JAVA) == (pool, [])


def test_pool_exhaustion_needs_concurrency_at_least_the_pool() -> None:
    """동시 20·풀 20 에서도 흔한 구현(커넥션을 쥔 채 하나 더)이 떨어졌다 (2026-11-10 gen-item2) — 같으면 성립."""
    plan = _plan({"pool_size": 20, "concurrency": 20}, "pool_acquire_timeout")
    assert feasibility_problems(plan, JAVA) == []
    plan = _plan({"pool_size": 50, "concurrency": 30}, "pool_acquire_timeout")
    assert "pool_size=50" in feasibility_problems(plan, JAVA)[0]


def test_reasons_without_conditions_and_unknown_values_are_not_judged() -> None:
    assert feasibility_problems(_plan({}, "record_missing"), JAVA) == []
    assert (
        feasibility_problems(_plan({"query_delay_ms": 20}, "request_timeout", rows=0), JAVA) == []
    )
