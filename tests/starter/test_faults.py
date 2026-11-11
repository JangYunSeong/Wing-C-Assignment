"""숨김 수용 기준의 실패 지정 — 명세가 항목 내용으로 정하고(사람 승인), 계약 단계는 API·요청 칸에 옮기기만 한다. 가상 데이터만."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any, cast

from task_spec.nodes.starter.generate import fault_text, plan_text
from task_spec.nodes.starter.plan_contract import fit_faults
from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.schemas.build import Fault
from task_spec.schemas.domain import HiddenAcceptance, HiddenFault
from task_spec.spec.feature import check_hidden_faults
from tests.starter.test_contract import BACKEND_SEED, _check, _contract, _has, _plan, _spec

APPROVED = HiddenFault(operation="주문 상태 변경", item="SHIPPED", times=1, status=503)
MAPPED = {"endpoint": "e1", "when": {"toStatus": "SHIPPED"}, "status": 503}


def _with(case: dict[str, Any]) -> dict[str, Any]:
    plan = _plan()
    plan["cases"] = [{**plan["cases"][0], **case}]
    return plan


def test_spec_check_needs_content_faults_for_item_materials() -> None:
    item = BACKEND_SEED.model_copy(update={"fault_by": "item"})
    bare = HiddenAcceptance(
        ac_id="h1", trap_id="t1", text="재연결 직후 처음 2건 503 이어도 한 번씩 저장"
    )
    out = cast(Any, SimpleNamespace(acceptance_hidden=[bare]))
    probs = check_hidden_faults(out, item)
    assert any("h1 에 실패 지정(faults)이 없음" in p for p in probs)
    assert any("순번으로 정함('처음 2건')" in p for p in probs)
    ok = bare.model_copy(
        update={"text": "SHIPPED 변경의 첫 전송이 실패해도 한 번씩 저장", "faults": [APPROVED]}
    )
    assert check_hidden_faults(cast(Any, SimpleNamespace(acceptance_hidden=[ok])), item) == []
    assert check_hidden_faults(out, BACKEND_SEED) == []  # 재료가 쓰지 않으면 보지 않는다


def test_fault_must_name_a_contract_api_and_its_request_fields() -> None:
    res = _check(plan=_with({"faults": [{"endpoint": "e9", "status": 503}]}))
    assert _has(res.problems, "실패 지정 API e9 가 계약에 없음")
    res = _check(plan=_with({"faults": [{"endpoint": "e1", "when": {"nope": "x"}, "status": 503}]}))
    assert _has(res.problems, "요청 본문에 없는 칸으로 고름: nope", "toStatus")


def test_contract_must_carry_the_approved_fault_with_the_same_values() -> None:
    spec = dataclasses.replace(_spec(), hidden_faults={"h1": (APPROVED,)})
    res = _check(plan=_with({"scenario": "처음 2건 503"}), spec=spec)
    assert _has(
        res.problems, "hc1 가 숨김 수용 기준 h1 의 승인된 실패 지정을 옮기지 않음", "'SHIPPED'"
    )
    assert _has(res.problems, "순번으로 정함('처음 2건')")
    wrong = {**MAPPED, "status": 500}
    assert _has(_check(plan=_with({"faults": [wrong]}), spec=spec).problems, "옮기지 않음")
    ok = _with({"scenario": "SHIPPED 변경의 첫 전송만 503", "faults": [MAPPED]})
    assert not _has(_check(plan=ok, spec=spec).problems, "실패")
    # 승인된 지정이 없으면 순번 글도 지금처럼 둔다
    assert not _has(_check(plan=_with({"scenario": "처음 2건 503"})).problems, "순번")


def test_faults_are_dropped_unless_the_spec_approved_them() -> None:
    plan = HiddenPlan.model_validate(_with({"faults": [MAPPED]}))
    kept, notes = fit_faults(plan, dataclasses.replace(_spec(), hidden_faults={"h1": (APPROVED,)}))
    assert kept.cases[0].faults and notes == []
    dropped, notes = fit_faults(plan, _spec())
    assert dropped.cases[0].faults == [] and "실패 지정 뺌 (hc1)" in notes[0]


def test_hidden_test_input_says_which_request_fails_by_content() -> None:
    plan = HiddenPlan.model_validate(_with({"faults": [MAPPED]}))
    text = plan_text(plan, TaskContract.model_validate(_contract()))
    assert (
        '실패 지정: PATCH /orders/{id}/status 에서 본문 toStatus="SHIPPED" 인 요청의 처음 1번 → 503 응답'
        in text
    )
    assert "몇 번째 요청인지로 정하지 않는다" in text
    assert "모든 요청의 처음 2번 → 연결 실패" in fault_text(Fault(endpoint="e2", times=2))


def test_inputs_carry_the_fault_rule_and_the_approved_faults() -> None:
    """명세 재료 글에 '실패 지정' 줄(재료가 켰을 때만), 계약 입력의 숨김 수용 기준 줄에 승인된 실패 지정."""
    import json
    from pathlib import Path

    from task_spec.nodes.starter.plan_contract import hidden_ac_line
    from task_spec.schemas.incident import PublicIncident
    from task_spec.spec.feature import seed_block

    raw = json.loads(
        (
            Path(__file__).parents[2] / "incidents/public_v1/sw.frontend.offline-replay.json"
        ).read_text("utf-8")
    )
    inc = PublicIncident.model_validate(raw)
    assert "실패 지정 (숨김 수용 기준마다 필수)" in seed_block(inc)
    off = inc.model_copy(update={"feature": inc.feature.model_copy(update={"fault_by": None})})  # type: ignore[union-attr]
    assert "실패 지정" not in seed_block(off)
    h = HiddenAcceptance(ac_id="h1", trap_id="t1", text="가상 기준", faults=[APPROVED])
    assert hidden_ac_line(h).splitlines()[1] == (
        '  실패 지정 (승인됨 — 이 기준의 숨은 케이스 faults 로 옮긴다): 주문 상태 변경 — 항목 "SHIPPED" 처음 1번 → 503 응답'
    )
