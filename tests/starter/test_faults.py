"""숨은 케이스의 실패 지정 — 순번이 아니라 요청 본문 내용으로 고른다 (2026-11-11 재측정 오프라인). 가상 데이터만."""

from __future__ import annotations

from typing import Any

from task_spec.nodes.starter.generate import fault_text, plan_text
from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.schemas.build import Fault
from tests.starter.test_contract import BACKEND_SEED, _check, _contract, _has, _plan


def _with(case: dict[str, Any]) -> dict[str, Any]:
    plan = _plan()
    plan["cases"] = [{**plan["cases"][0], **case}]
    return plan


def test_fault_must_name_a_contract_api_and_its_request_fields() -> None:
    res = _check(plan=_with({"faults": [{"endpoint": "e9", "status": 503}]}))
    assert _has(res.problems, "실패 지정 API e9 가 계약에 없음")
    res = _check(plan=_with({"faults": [{"endpoint": "e1", "when": {"nope": "x"}, "status": 503}]}))
    assert _has(res.problems, "요청 본문에 없는 칸으로 고름: nope", "toStatus")
    res = _check(
        plan=_with({"faults": [{"endpoint": "e1", "when": {"toStatus": "SHIPPED"}, "status": 503}]})
    )
    assert not _has(res.problems, "실패 지정")


def test_item_material_needs_content_faults_and_no_ordinal_scenario() -> None:
    """재료가 fault_by=item 이면 케이스마다 내용으로 고른 실패가 있어야 하고, '처음 2건 503' 같은 순번은 다시 묻는다."""
    seed = BACKEND_SEED.model_copy(update={"fault_by": "item"})
    res = _check(plan=_with({"scenario": "재연결 직후 처음 2건 503"}), seed=seed)
    assert _has(res.problems, "hc1 에 내용으로 고른 실패 지정(faults.when)이 없음")
    assert _has(res.problems, "실패를 순번으로 정함('처음 2건')")
    ok = _with(
        {
            "scenario": "첫 재연결에서 SHIPPED 로 바꾸는 요청의 첫 전송만 503",
            "faults": [{"endpoint": "e1", "when": {"toStatus": "SHIPPED"}, "status": 503}],
        }
    )
    assert not _has(_check(plan=ok, seed=seed).problems, "실패")
    # 재료가 정하지 않으면 순번 글도 지금처럼 둔다
    assert not _has(_check(plan=_with({"scenario": "처음 2건 503"})).problems, "순번")


def test_hidden_test_input_says_which_request_fails_by_content() -> None:
    plan = HiddenPlan.model_validate(
        _with({"faults": [{"endpoint": "e1", "when": {"toStatus": "SHIPPED"}, "status": 503}]})
    )
    text = plan_text(plan, TaskContract.model_validate(_contract()))
    assert (
        '실패 지정: PATCH /orders/{id}/status 에서 본문 toStatus="SHIPPED" 인 요청의 처음 1번 → 503 응답'
        in text
    )
    assert "몇 번째 요청인지로 정하지 않는다" in text
    assert "연결 실패" in fault_text(Fault(endpoint="e2", times=2))
    assert "모든 요청의 처음 2번" in fault_text(Fault(endpoint="e2", times=2))


def test_faults_are_dropped_unless_the_material_uses_them() -> None:
    """출력 스키마가 프롬프트 본문에 들어가 이전 판에도 faults 가 보인다 — 재료가 쓰지 않으면 코드가 빼서 다른 재료는 전과 같다."""
    from task_spec.nodes.starter.plan_contract import fit_faults

    plan = HiddenPlan.model_validate(
        _with({"faults": [{"endpoint": "e1", "when": {"toStatus": "SHIPPED"}, "status": 503}]})
    )
    kept, notes = fit_faults(plan, BACKEND_SEED.model_copy(update={"fault_by": "item"}))
    assert kept.cases[0].faults and notes == []
    dropped, notes = fit_faults(plan, BACKEND_SEED)
    assert dropped.cases[0].faults == [] and "실패 지정 뺌 (hc1)" in notes[0]
    assert fit_faults(dropped, None) == (dropped, [])
