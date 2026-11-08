"""상태 규칙 칸(Entity.state) — 옛 화살표 글 옮기기·검사·문장·도우미 상수 (starter/state_rule.py). 가상 데이터만."""

from __future__ import annotations

from typing import Any

from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.starter.contract import check_contract
from task_spec.starter.hidden_helpers import render_contract_case
from task_spec.starter.seed_rows import seed_problems
from task_spec.starter.state_rule import fit_state, next_states, sentence, state_problems
from task_spec.starter.static_check import unknown_helper_names
from task_spec.starter.templates import load_template
from tests.starter.test_contract import BACKEND_SEED, _contract, _manifest, _plan, _spec
from tests.starter.test_templates import JAVA

RULE = {
    "field": "status",
    "values": ["READY", "SHIPPED", "DELIVERED"],
    "transitions": ["READY→SHIPPED", "SHIPPED→DELIVERED"],
    "rejected_status": 409,
}


def _with_state(rule: dict[str, Any] | None = RULE, arrows: bool = False) -> TaskContract:
    c = _contract()
    if not arrows:
        c["endpoints"][0]["responses"][1]["description"] = "허용되지 않는 상태 변경"
    c["entities"][0]["state"] = rule
    c["entities"][0]["fields"][1]["constraints"] = "1~32자"
    return TaskContract.model_validate(c)


def _has(msgs: list[str], *needles: str) -> bool:
    return any(all(n in m for n in needles) for m in msgs)


def _status(contract: TaskContract) -> str:
    return next(f.constraints or "" for f in contract.entities[0].fields if f.name == "status")


def test_fit_state_writes_the_constraints_sentence_from_the_slot() -> None:
    fitted, notes = fit_state(_with_state())
    assert notes == []  # 칸이 이미 있으면 옮긴 것 없음
    assert _status(fitted) == (
        '상태 값: "READY", "SHIPPED", "DELIVERED". 허용 전이: READY→SHIPPED, SHIPPED→DELIVERED — 그 밖은 409 (1~32자)'
    )
    assert fit_state(fitted) == (fitted, [])  # 두 번 불러도 같다
    assert not seed_problems(fitted)  # 따옴표 값 목록 — 시드 값 검사가 읽는다


def test_legacy_arrow_text_is_moved_into_the_slot() -> None:
    """plan_contract v9 이하 — 응답 설명에 화살표 전이만 적은 답도 칸으로 (거절 코드는 상태를 말하는 4xx 응답)."""
    legacy = _with_state(None, arrows=True)
    fitted, notes = fit_state(legacy)
    rule = fitted.entities[0].state
    assert rule is not None and rule.pairs() == [("READY", "SHIPPED"), ("SHIPPED", "DELIVERED")]
    assert rule.rejected_status == 409 and rule.values == ["READY", "SHIPPED", "DELIVERED"]
    assert _has(notes, "Order.status", "state 칸으로 옮김")
    assert fitted.entities[1].state is None  # 기록 엔티티는 건드리지 않음
    # 칸도 화살표도 없으면 그대로
    assert fit_state(_with_state(None)) == (_with_state(None), [])


def test_needs_ask_for_the_slot_when_transitions_are_missing() -> None:
    plan = HiddenPlan.model_validate(_plan())

    def needs(c: TaskContract) -> list[str]:
        return check_contract(c, plan, _spec(), _manifest(), BACKEND_SEED).problems

    assert _has(needs(_with_state(None)), "상태 전이 규칙", "state 칸")
    assert not _has(needs(_with_state()), "상태 전이 규칙")
    assert not _has(
        needs(_with_state(None, arrows=True)), "상태 전이 규칙"
    )  # 옮길 수 있으면 있는 것으로


def test_state_problems_cross_check_contract_seed_and_cases() -> None:
    plan = HiddenPlan.model_validate(_plan())
    assert state_problems(_with_state(), plan) == []
    bad = {
        "field": "state",
        "values": ["READY", "SHIPPED"],
        "transitions": ["READY→SHIPPED", "SHIPPED→LOST"],
        "rejected_status": 422,
    }
    msgs = state_problems(_with_state(bad), plan)
    assert _has(msgs, "field state 가 Order 필드에 없음")
    msgs = state_problems(_with_state({**bad, "field": "status"}), plan)
    assert _has(msgs, "SHIPPED→LOST", "values 에 없음")
    assert _has(msgs, "거절 코드 422", "응답에도 없음")
    # 숨은 케이스 처음 값에서 나갈 허용 전이가 없음 (READY 에서 못 나감)
    stuck = {**RULE, "transitions": ["SHIPPED→DELIVERED"]}
    assert _has(
        state_problems(_with_state(stuck), plan), "hc1", "status=READY", "나갈 허용 전이가 없음"
    )
    # 시드 처음 값이 값 목록 밖
    other = {**RULE, "values": ["NEW", "SHIPPED", "DELIVERED"], "transitions": ["NEW→SHIPPED"]}
    msgs = state_problems(_with_state(other), plan)
    assert _has(msgs, "시드 처음 값 status=READY") and _has(msgs, "hc1", "values 에 없음")


def test_helpers_carry_next_states_and_names_are_checked() -> None:
    contract, _ = fit_state(_with_state())
    assert list(next_states(contract)) == [("Order", "READY", "SHIPPED", "DELIVERED")]
    plan = HiddenPlan.model_validate(_plan())
    manifest = load_template(JAVA).manifest
    for audience in ("hidden", "public"):
        helper = render_contract_case(contract, plan, manifest, audience=audience)  # type: ignore[arg-type]
        assert helper is not None
        assert 'protected static final String NEXT_OK_ORDER_READY = "SHIPPED";' in helper.content
        assert 'protected static final String NEXT_BAD_ORDER_READY = "DELIVERED";' in helper.content
        assert sentence(contract.entities[0].state) in helper.content  # type: ignore[arg-type]
    from task_spec.schemas import GeneratedFile

    case = GeneratedFile(
        path="src/test/java/com/example/service/grading/cases/X.java",
        content="class X extends ContractCase { void a() { send(NEXT_OK_ORDER_SHIPPED); } }",
    )
    msgs = unknown_helper_names([case], helper, "숨은 테스트")
    assert _has(msgs, "NEXT_OK_ORDER_SHIPPED")
    # 칸이 없으면 상수 없음
    assert "NEXT_OK_" not in render_contract_case(_with_state(None), plan, manifest).content  # type: ignore[union-attr]
