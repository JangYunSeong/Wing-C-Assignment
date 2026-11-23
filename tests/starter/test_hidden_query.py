"""숨은·공개 테스트 도우미의 쿼리 매개변수 오버로드, 그 정적 검사, 허용 구현 실패 배정 (2026-11-13). 가상 데이터만, LLM 없음.

근거: 2026-11-12 행마다 조회 알파 — 도우미가 경로 변수만 받아 숨은 테스트가 `send("GET", "/orders?size=100", …)` 를 손으로 쓰다
기본 크기 호출과 섞었다 (시간을 재는 요청 안에서 목록을 두 번, 기본 50건 목록에 100건 기대). 허용 구현까지 떨어졌는데 수리 배정은
흔한 구현이 의도한 사유로 떨어졌다는 이유로 허용 구현 탓으로 돌렸다.
"""

from __future__ import annotations

from typing import Any

from task_spec.schemas import TaskContract
from task_spec.starter.contract import SpecView
from task_spec.starter.hidden_helpers import render_contract_case
from task_spec.starter.repair import assign
from task_spec.starter.static_check import static_check
from tests.starter.test_contract import BACKEND_SEED, _contract
from tests.starter.test_repair import (
    GOOD_ACC,
    GOOD_NAIVE,
    MISSED,
    OVERLAYS,
    PLAN2,
    _report,
    _run,
    _t,
)
from tests.starter.test_static_check import HC1, JAVA, PLAN, _f, _overlays, _task

CASES = "src/test/java/com/example/service/grading/cases"
PUBLIC = "src/test/java/com/example/service/publictests"


def _query_contract(*names: str) -> TaskContract:
    """e2 GET /orders/{id}/history 에 쿼리 매개변수 (계약 순서 그대로)."""
    c: dict[str, Any] = _contract()
    c["endpoints"][1]["query"] = [{"name": n, "type": "int", "refs": ["r2"]} for n in names]
    return TaskContract.model_validate(c)


def test_query_overload_in_contract_order() -> None:
    contract = _query_contract("size", "after")
    for audience in ("hidden", "public"):
        f = render_contract_case(contract, PLAN, JAVA.manifest, audience)  # type: ignore[arg-type]
        assert f is not None
        src = f.content
        # 경로 변수만 받는 함수는 그대로, 쿼리 칸을 계약 순서로 받는 오버로드가 더해진다
        assert (
            "protected HttpResponse<String> getOrdersIdHistory(Object id) throws Exception {" in src
        )
        assert (
            "protected HttpResponse<String> getOrdersIdHistory(Object id, Object size, Object after) throws Exception {"
            in src
        )
        assert 'get("/orders/" + id + "/history" + queryOf("size", size, "after", after))' in src
        assert "private static String queryOf(Object... nameValues)" in src
        assert (
            "import java.net.URLEncoder;" in src
            and "import java.nio.charset.StandardCharsets;" in src
        )
    # 쿼리 칸이 없는 계약은 예전 그대로
    plain = render_contract_case(TaskContract.model_validate(_contract()), PLAN, JAVA.manifest)
    assert (
        plain is not None and "queryOf" not in plain.content and "URLEncoder" not in plain.content
    )


def test_query_param_names_avoid_keywords_and_path_variables() -> None:
    f = render_contract_case(_query_contract("id", "class"), PLAN, JAVA.manifest)
    assert f is not None
    assert "getOrdersIdHistory(Object id, Object qId, Object qClass)" in f.content
    assert 'queryOf("id", qId, "class", qClass)' in f.content  # 보내는 이름은 계약 칸 그대로


def _hidden_msgs(case_src: str, contract: TaskContract) -> list[str]:
    res = static_check(
        _overlays(hidden=[_f(f"{CASES}/OrderStatusBurstTest.java", case_src)]),
        template=JAVA,
        plan=PLAN,
        spec=SpecView.from_task(_task()),
        contract=contract,
        seed=BACKEND_SEED,
    )
    return res.for_target("gen_hidden")


def _with_burst(call: str) -> str:
    return HC1.replace(
        "Burst.fire(n, i -> 200, i -> 200, REQUEST_TIMEOUT)",
        f"Burst.fire(n, i -> 200, {call}, REQUEST_TIMEOUT)",
    )


def test_hand_built_query_string_is_sent_back_to_the_overload() -> None:
    contract = _query_contract("size", "after")
    hand = _with_burst(
        'i -> send("GET", "/orders/" + HC1_FIRST + "/history?size=100", Map.of(), null).statusCode()'
    )
    msgs = [m for m in _hidden_msgs(hand, contract) if "쿼리 문자열을 손으로 씀" in m]
    assert len(msgs) == 1 and "getOrdersIdHistory(id, size, after)" in msgs[0]
    ok = _with_burst("i -> getOrdersIdHistory(HC1_FIRST, 100, null).statusCode()")
    assert not [m for m in _hidden_msgs(ok, contract) if "쿼리 문자열" in m]
    # 계약에 쿼리 칸이 없는 API 에 붙이면 계약에 없는 칸이라고 알린다
    plain = [
        m
        for m in _hidden_msgs(hand, TaskContract.model_validate(_contract()))
        if "쿼리 문자열" in m
    ]
    assert len(plain) == 1 and "계약에 없는 칸을 보내지 않는다" in plain[0]


def test_timed_request_sends_one_request() -> None:
    contract = _query_contract("size", "after")
    two = _with_burst(
        "i -> { getOrdersIdHistory(HC1_FIRST); return getOrdersIdHistory(HC1_FIRST, 100, null).statusCode(); }"
    )
    msgs = [m for m in _hidden_msgs(two, contract) if "시간을 재는" in m]
    assert len(msgs) == 1 and "2번" in msgs[0] and "getOrdersIdHistory" in msgs[0]
    # 하나만 보내면 통과, 본문 읽기(…Body)는 요청이 아니다
    one = _with_burst("i -> getOrdersIdHistoryBody(getOrdersIdHistory(HC1_FIRST + i)).size()")
    assert not [m for m in _hidden_msgs(one, contract) if "시간을 재는" in m]
    # 분기로 요청마다 하나만 보내는 것은 둔다
    branch = _with_burst(
        "i -> i % 2 == 0 ? getOrdersIdHistory(HC1_FIRST).statusCode() : getOrdersIdHistory(HC1_FIRST, 5, null).statusCode()"
    )
    assert not [m for m in _hidden_msgs(branch, contract) if "시간을 재는" in m]


def test_public_tests_get_the_query_check_too() -> None:
    from task_spec.starter.static_check import _hand_built_query

    contract = _query_contract("size", "after")
    test = _f(f"{PUBLIC}/Pt1Test.java", 'var r = get("/orders/3/history?size=5");')
    msgs = _hand_built_query([test], contract, JAVA.manifest.hidden_rules.http_calls, "공개 테스트")
    assert len(msgs) == 1 and msgs[0].startswith("공개 테스트 ")


def test_accepted_failing_an_untagged_assertion_suspects_the_test() -> None:
    """흔한 구현은 의도한 사유로 떨어지는데 허용 구현은 사유 없는 단언에서 떨어짐 → 숨은 요구가 아니라 테스트의 다른 조건 의심."""
    naive = _run("naive-all", "ref_naive", *GOOD_NAIVE)
    untagged = _run(
        "d1",
        "ref_accepted",
        GOOD_ACC[0],
        _t("hidden", "hc2", "ASSERT", None, "expected size 100 but was 50"),
    )
    plan = assign(_report(naive, untagged), PLAN2, OVERLAYS, MISSED, {"gen_accepted:d1": 1})
    assert [a.key for a in plan.actions] == ["gen_hidden"] and not plan.unresolved
    # 그 한 번을 썼으면 사람 확인 — '설계를 못 따름'이라고 단정하지 않는다
    plan = assign(
        _report(naive, untagged), PLAN2, OVERLAYS, MISSED, {"gen_accepted:d1": 1, "gen_hidden": 1}
    )
    assert (
        not plan.actions
        and plan.unresolved
        and not any("설계를 준수하지 못함" in u for u in plan.unresolved)
    )
    # 사유가 붙은 단언(숨은 요구를 재는 단언)에서 떨어지면 예전처럼 허용 구현 탓
    tagged = _run(
        "d1",
        "ref_accepted",
        GOOD_ACC[0],
        _t("hidden", "hc2", "ASSERT", "record_missing", "기록 없음"),
    )
    plan = assign(_report(naive, tagged), PLAN2, OVERLAYS, MISSED, {"gen_accepted:d1": 1})
    assert not plan.actions and any("설계를 준수하지 못함" in u for u in plan.unresolved)
