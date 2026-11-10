"""요청 헤더 값 (starter/header_values.py) — 도우미 상수·계약 검사·정적 검사. 가상 데이터만.

2026-11-08 알파: 숨은 테스트가 계약에 없는 요청자(`grader`)로 요청해 모든 요청이 403 에서 끝났다.
"""

from __future__ import annotations

from typing import Any

from task_spec.schemas import GeneratedFile, HiddenPlan, TaskContract
from task_spec.starter.header_values import header_problems, header_values
from task_spec.starter.hidden_helpers import render_contract_case
from task_spec.starter.static_check import Overlay, static_check
from tests.starter.test_contract import _contract, _plan, _spec
from tests.starter.test_static_check import CASES, JAVA

HEADER = "X-Requester-Id"


def _with_header(*checks: str, deny: bool = True) -> TaskContract:
    c: dict[str, Any] = _contract()
    c["endpoints"][0]["headers"] = [{"name": HEADER, "type": "string", "refs": ["r1"]}]
    if deny:
        c["endpoints"][0]["responses"].append({"status": 403, "description": "권한 없음"})
    for t, text in zip(c["public_tests"], checks, strict=False):
        t["checks"] = text
    return TaskContract.model_validate(c)


GIVEN = (
    f"PATCH /orders/1/status ({HEADER}=admin, toStatus=SHIPPED) → 200 후 이력 1건",
    f"{HEADER}: guest 로 PATCH /orders/2/status → 403, 이력에 FAILURE",
)


def test_values_come_from_public_test_examples() -> None:
    [h] = header_values(_with_header(*GIVEN))
    assert (h.name, h.ok, h.denied) == (HEADER, "admin", "guest")
    # 값 뒤 괄호 설명(역할)은 값이 아니다, 상태는 그 헤더를 받는 엔드포인트가 선언한 것만 (경로 숫자 150 은 상태가 아님)
    [h] = header_values(
        _with_header(
            f"{HEADER}: 11(SUB_MASTER)로 PATCH /orders/150/status → 200",
            f"{HEADER}=1 로 PATCH /orders/2/status → 403",
        )
    )
    assert (h.ok, h.denied) == ("11", "1")
    # 같은 값이 통과·거절 양쪽이면 버린다
    [h] = header_values(_with_header(f"{HEADER}=a → 200", f"{HEADER}=a → 403"))
    assert (h.ok, h.denied) == (None, None)


def test_no_example_means_no_invented_value_unless_any_value_is_accepted() -> None:
    [h] = header_values(_with_header("권한 없는 요청자로 PATCH 시 403", "조회가 최신순"))
    assert h.ok is None and h.denied is None  # 지어내지 않는다
    assert header_problems(_with_header("권한 없는 요청자로 PATCH 시 403")) and not header_problems(
        _with_header(*GIVEN)
    )
    [h] = header_values(
        _with_header("변경 성공", deny=False)
    )  # 거절 응답이 없는 헤더 — 아무 값이나 받음
    assert (
        h.ok == "grader"
        and h.denied is None
        and not header_problems(_with_header("변경 성공", deny=False))
    )


def test_reask_names_placeholders_and_asks_for_real_values() -> None:
    # 재요청 문구가 `<통과하는 값>` 꼴이면 답도 자리표시자로 온다 (alpha-cap2)
    c = _with_header(f"{HEADER}=<MASTER 등급 멤버의 memberId>로 PATCH /orders/1/status → 200")
    [msg] = header_problems(c)
    assert f"`{HEADER}=1 → 200`" in msg and "<통과하는 값>" not in msg and "시드 groups" in msg
    assert "pt1 의 <MASTER 등급 멤버의 memberId>" in msg
    [plain] = header_problems(_with_header("권한 없는 요청자로 PATCH 시 403"))
    assert "자리표시자" not in plain


def test_helpers_carry_the_constants() -> None:
    plan = HiddenPlan.model_validate(_plan())
    for audience in ("hidden", "public"):
        helper = render_contract_case(_with_header(*GIVEN), plan, JAVA.manifest, audience=audience)  # type: ignore[arg-type]
        assert helper is not None
        assert 'HEADER_X_REQUESTER_ID_OK = "admin"' in helper.content
        assert 'HEADER_X_REQUESTER_ID_DENIED = "guest"' in helper.content
        assert 'Map.of("X-Requester-Id", HEADER_X_REQUESTER_ID_OK)' in helper.content


def test_hidden_test_with_an_invented_header_value_is_re_requested() -> None:
    contract = _with_header(*GIVEN)
    plan = HiddenPlan.model_validate(_plan())
    helper = render_contract_case(contract, plan, JAVA.manifest)
    assert helper is not None

    def case(header_value: str) -> Overlay:
        body = (
            "package com.example.service.grading.cases;\n\nimport java.util.Map;\n\n"
            "class Hc1Test extends ContractCase {\n    void hc1() throws Exception {\n"
            f'        patchOrdersIdStatus(HC1_FIRST, Map.of("{HEADER}", {header_value}), "{{}}");\n'
            "    }\n}\n"
        )
        return Overlay(
            kind="hidden", files=(GeneratedFile(path=f"{CASES}/Hc1Test.java", content=body),)
        )

    def problems(o: Overlay) -> list[str]:
        return [
            p.message
            for p in static_check(
                [o], template=JAVA, plan=plan, spec=_spec(), contract=contract
            ).problems
            if "헤더" in p.message
        ]

    assert any(
        '"grader"' in m and "HEADER_X_REQUESTER_ID_OK" in m for m in problems(case('"grader"'))
    )
    assert not problems(case("HEADER_X_REQUESTER_ID_OK"))
    assert not problems(case('"guest"'))  # 계약 값 그대로면 막지 않는다


def test_request_key_header_gets_a_new_value_per_request() -> None:
    # 2026-11-10 retrydup-c: 요청 번호를 상수 하나로 줘 모든 테스트가 같은 번호 — 명세대로 막은 허용 구현이 떨어졌다
    from task_spec.starter.header_values import PER_REQUEST, header_values, per_request

    c = _contract()
    c["endpoints"][0]["headers"] = [
        {"name": "X-Request-Id", "type": "string", "constraints": "UUID v4 형식", "refs": ["r1"]}
    ]
    contract = TaskContract.model_validate(c)
    assert per_request(contract, "X-Request-Id")
    [h] = header_values(contract)
    assert h.source == PER_REQUEST and h.ok is None and h.denied is None
    assert not header_problems(contract)
    c["endpoints"][0]["headers"] = [{"name": "X-Actor", "type": "string", "refs": ["r1"]}]
    assert not per_request(TaskContract.model_validate(c), "X-Actor")


def test_helper_gives_fresh_request_key_and_a_retry_overload() -> None:
    c = _contract()
    c["endpoints"][0]["headers"] = [
        {"name": "X-Request-Id", "type": "string", "constraints": "UUID v4 형식", "refs": ["r1"]}
    ]
    helper = render_contract_case(
        TaskContract.model_validate(c), HiddenPlan.model_validate(_plan()), JAVA.manifest
    )
    assert helper is not None
    body = helper.content
    assert "return okHeaders(newRequestId());" in body
    assert "protected static Map<String, String> okHeaders(String key)" in body
    assert 'Map.of("X-Request-Id", key)' in body and "HEADER_X_REQUEST_ID_OK" not in body
