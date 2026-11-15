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
    c["endpoints"][0]["headers"] = [
        {"name": HEADER, "type": "string", "per_request": False, "refs": ["r1"]}
    ]
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
    # #181: `<…>` 꼴만이 아니라 값 글자로 읽지 못한 대입 전부를 진단에
    for shown in ("{통과 값}", "[관리자 id]", "(통과하는 값)", "관리자로"):
        [msg] = header_problems(_with_header(f"{HEADER}={shown} PATCH /orders/1/status → 200"))
        assert f"pt1 의 {shown}" in msg, (shown, msg)


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
        {
            "name": "X-Request-Id",
            "type": "string",
            "constraints": "UUID v4 형식",
            "per_request": True,
            "refs": ["r1"],
        }
    ]
    contract = TaskContract.model_validate(c)
    assert per_request(contract, "X-Request-Id")
    [h] = header_values(contract)
    assert h.source == PER_REQUEST and h.ok is None and h.denied is None
    assert not header_problems(contract)
    c["endpoints"][0]["headers"] = [
        {"name": "X-Actor", "type": "string", "per_request": False, "refs": ["r1"]}
    ]
    assert not per_request(TaskContract.model_validate(c), "X-Actor")
    # #181: 이름·제약 낱말이 아니라 칸으로 — 사전 밖 이름(X-Nonce)도 칸이 true 면 요청마다, 이름이 X-Request-Id 여도 false 면 상수
    c["endpoints"][0]["headers"] = [
        {
            "name": "X-Nonce",
            "type": "string",
            "constraints": "고유·중복 불가",
            "per_request": True,
            "refs": ["r1"],
        }
    ]
    [h] = header_values(TaskContract.model_validate(c))
    assert h.source == PER_REQUEST
    c["endpoints"][0]["headers"] = [
        {"name": "X-Request-Id", "type": "string", "per_request": False, "refs": ["r1"]}
    ]
    # per_request 는 칸만 읽는다 — 이름이 요청 번호인데 false 인 칸은 그 앞의 계약 맞춤(fit_per_request)이 고친다
    assert not per_request(TaskContract.model_validate(c), "X-Request-Id")


def test_request_key_name_or_constraint_overrides_a_false_field() -> None:
    """2026-11-15 알파 pilot-retry-ae030: 계약이 X-Request-Id 에 false 를 적어 도우미가 상수 하나 — 세 번 충전이 같은 번호라
    번호로 중복을 막은 허용 구현이 공개 테스트에서 떨어졌다. 이름·제약이 요청 하나를 가리키면 코드가 true 로 맞춘다."""
    from task_spec.starter.header_values import PER_REQUEST, fit_per_request

    def fitted(*headers: dict[str, Any]) -> tuple[TaskContract, list[str]]:
        c = _contract()
        c["endpoints"][0]["headers"] = [{"type": "string", "refs": ["r1"], **h} for h in headers]
        return fit_per_request(TaskContract.model_validate(c))

    contract, notes = fitted(
        {
            "name": "X-Request-Id",
            "constraints": "UUID v4 형식의 요청 고유 식별자",
            "per_request": False,
        }
    )
    [h] = header_values(contract)
    assert h.source == PER_REQUEST
    assert notes == [
        "헤더 X-Request-Id 의 per_request 를 true 로 (계약은 false) — 이름·제약이 요청 하나를 가리킴: "
        "도우미가 요청마다 새 값을 준다"
    ]
    # 이름은 사전 밖이어도 제약이 요청 번호·멱등 키를 말하면 — 비어 있던 칸도 채운다 (재요청하지 않음)
    contract, notes = fitted({"name": "X-Key", "constraints": "충전 멱등 키", "per_request": None})
    assert header_values(contract)[0].source == PER_REQUEST and "비어 있음" in notes[0]
    assert not header_problems(contract)
    # 요청 하나를 가리키지 않는 헤더는 칸 그대로 — 요청자(X-Requester-Id 는 request-id 가 아님)·'고유 식별자'·uuid 인 회원 id
    for h in (
        {"name": "X-Requester-Id", "per_request": False},
        {"name": "X-Member-Id", "constraints": "회원 고유 식별자 (UUID)", "per_request": False},
    ):
        contract, notes = fitted(h)
        assert notes == [] and header_values(contract)[0].source != PER_REQUEST
    # 권한 칸의 헤더는 권한 규칙이 값을 정한다
    c = _contract()
    c["endpoints"][0]["headers"] = [
        {"name": "X-Request-Id", "type": "string", "per_request": False, "refs": ["r1"]}
    ]
    c["endpoints"][0]["responses"].append({"status": 403, "description": "권한 없음"})
    c["endpoints"][0]["auth"] = {"header": "X-Request-Id", "kind": "other", "denied_status": 403}
    assert fit_per_request(TaskContract.model_validate(c))[1] == []


def test_helper_gives_fresh_request_key_and_a_retry_overload() -> None:
    c = _contract()
    c["endpoints"][0]["headers"] = [
        {
            "name": "X-Request-Id",
            "type": "string",
            "constraints": "UUID v4 형식",
            "per_request": True,
            "refs": ["r1"],
        }
    ]
    helper = render_contract_case(
        TaskContract.model_validate(c), HiddenPlan.model_validate(_plan()), JAVA.manifest
    )
    assert helper is not None
    body = helper.content
    assert "return okHeaders(newRequestId());" in body
    assert "protected static Map<String, String> okHeaders(String key)" in body
    assert 'Map.of("X-Request-Id", key)' in body and "HEADER_X_REQUEST_ID_OK" not in body


def test_literal_request_key_in_tests_is_re_requested() -> None:
    """2026-11-12 백엔드 중복 처리: hc1·hc2 가 요청 번호를 글자('req-seq-001')로 써 서로의 기록을 돌려받았다 — 글자 그대로든
    변수에 담았든 다시 묻는다. 도우미로 만든 값(newRequestId())·계약이 형식 오류 400 을 정한 경우는 둔다."""
    from task_spec.schemas import GeneratedFile
    from task_spec.starter.static_check import _literal_headers

    c = _contract()
    c["endpoints"][0]["headers"] = [
        {
            "name": "X-Request-Id",
            "type": "string",
            "constraints": "UUID v4 형식",
            "per_request": True,
            "refs": ["r1"],
        }
    ]
    contract = TaskContract.model_validate(c)

    def probs(body: str, ct: TaskContract = contract) -> list[str]:
        return _literal_headers(
            [GeneratedFile(path="cases/Hc1Test.java", content=body)], ct, "숨은 테스트"
        )

    assert probs('var r = post(okHeaders("req-seq-001"), body);')  # 글자 그대로
    assert probs(
        'String reqId = "req-seq-001";\nvar r = post(okHeaders(reqId), body);'
    )  # 변수에 담은 글자
    assert probs('Map.of("X-Request-Id", "abc-1")')  # 헤더 이름 꼴
    assert probs('headers = {"X-Request-Id": "abc-1"}')  # Python·TS 꼴
    assert "newRequestId()" in probs('okHeaders("x1")')[0]
    assert not probs(
        "String key = newRequestId();\nvar r = post(okHeaders(key), body);\npost(okHeaders(key), body);"
    )
    assert not probs("var r = post(okHeaders(), body);")
    # 계약이 그 헤더의 형식 오류를 400 으로 정했으면 거절 시험 값은 둔다
    c["endpoints"][0]["responses"].append(
        {"status": 400, "description": "X-Request-Id 형식이 UUID 가 아님"}
    )
    assert not probs('okHeaders("not-a-uuid")', TaskContract.model_validate(c))
