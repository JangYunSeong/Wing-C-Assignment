"""요청당 DB 사용 세기 (#339) — 템플릿 Java v7 의 사유 db_per_request, 하네스 세기 API, 계약 도우미 sessionPathStatements.
가상 데이터만 (계약은 tests.starter 의 가상 주문·회원 계약)."""

from __future__ import annotations

from typing import Any

from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.starter.contract import _count_target
from task_spec.starter.hidden_helpers import (
    count_endpoint,
    count_helper,
    count_targets,
    render_contract_case,
)
from task_spec.starter.tables import contract_tables
from task_spec.starter.templates import check_template, load_template
from tests.starter.test_auth_issued import HEADER, ISSUED_RULE, _f, _fit, _session
from tests.starter.test_contract import _plan

JAVA = "backend/java-spring-boot3-jpa"


def _with_get(
    c: dict[str, Any],
    path: str,
    returns: str,
    eid: str = "e4",
    *,
    session: bool = True,
    query: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """로그인 세션(X-Session-Id)으로 부르는 가상 조회 API — session=False 면 세션 없이 부르는 공개 조회."""
    e: dict[str, Any] = {
        "endpoint_id": eid,
        "method": "GET",
        "path": path,
        "summary": "가상 조회",
        "responses": [{"status": 200, "description": "가상 조회", "returns": returns}],
        "refs": ["r1"],
    }
    if session:
        e["headers"] = [{"name": HEADER, "type": "string", "refs": ["r1"]}]
        e["responses"].append({"status": 401, "description": "세션 없음·유효하지 않음"})
        e["auth"] = dict(ISSUED_RULE)
    if query:
        e["query"] = query
    c["endpoints"].append(e)
    return c


def _with_entity(
    c: dict[str, Any],
    eid: str,
    name: str,
    foreign_keys: list[str] | None = None,
    *,
    relations: list[str] | None = None,
    extra: tuple[str, ...] = (),
) -> dict[str, Any]:
    c["entities"].append(
        {
            "entity_id": eid,
            "name": name,
            "fields": [_f("id", "integer"), _f("memo"), *(_f(x) for x in extra)],
            "foreign_keys": foreign_keys or [],
            "relations": relations or [],
        }
    )
    c["seed"].append({"entity_id": eid, "rows": 0})
    return c


def _shipments() -> dict[str, Any]:
    """주문을 외래 키로 가리키는 배송(m4)과 그 목록 조회(e4)."""
    return _with_get(
        _with_entity(_session(), "m4", "Shipment", ["orderId → Order"]), "/shipments", "m4[]"
    )


def _helper(c: dict[str, Any], version: int) -> str:
    fitted = _fit(c)
    rendered = render_contract_case(
        fitted, HiddenPlan.model_validate(_plan()), load_template(JAVA, version).manifest
    )
    assert rendered is not None
    return rendered.content


def _picked(c: dict[str, Any]) -> tuple[str, list[str]] | None:
    fitted = _fit(c)
    found = count_endpoint(
        fitted, contract_tables(fitted, load_template(JAVA, 7).manifest.naming.table)
    )
    return None if found is None else (found[0].endpoint_id, found[2])


def _bean_methods(src: str) -> list[str]:
    lines = src.splitlines()
    return [lines[i + 1].strip() for i, x in enumerate(lines) if x.strip() == "@Bean"]


def test_v7_declares_the_count_reason_without_calibration() -> None:
    """대기 시간 보정은 세션 재료에서 좁은 띠에서만 성립했다 (보정 7회 중 창 1회) — 요청당 DB 사용은 SQL 문 수로 잰다. tunes 가 없어
    보정하지 않고, 사유는 세기 도우미를 써야 하며(requires) 그 단언에만 붙는다(assert_on). v6 은 그대로 둔다."""
    v7 = load_template(JAVA, 7)
    assert check_template(v7) == [] and v7.manifest.version == 7
    r = next(x for x in v7.manifest.failure_reasons if x.code == "db_per_request")
    assert r.tunes == [] and r.feasible == []
    assert r.requires == [r"sessionPathStatements\("] and r.assert_on == [
        r"sessionPathStatements\("
    ]
    harness = v7.root / "hidden/harness"
    jdbc = (harness / "GraderJdbcHarness.java").read_text("utf-8")
    assert "static List<String> drainAndStop(ApplicationContext context)" in jdbc
    assert (
        "public static long statementsNotTouching(List<String> statements, String... tables)"
        in jdbc
    )
    assert "protected List<String> countStatements(Callable<?> body)" in (
        harness / "GraderSupport.java"
    ).read_text("utf-8")
    assert "sessionPathStatements" in (v7.root / "hidden/HARNESS.md").read_text("utf-8")
    selftest = (v7.root / "hidden/selftest/HarnessSelfTest.java").read_text("utf-8")
    assert "세기는_측정_구간의_sql_문을_모으고_비동기_작업까지_기다린다" in selftest
    v6 = load_template(JAVA, 6)
    # 세기 메서드를 @Bean 과 감싸기 빈 사이에 끼우면 DataSource 를 못 감싼다 (1단계 e2e: 여덟 대상 모두 하네스 오류) — 빈은 v6 그대로
    assert _bean_methods(jdbc) == _bean_methods(
        (v6.root / "hidden/harness/GraderJdbcHarness.java").read_text("utf-8")
    )
    assert "db_per_request" not in v6.manifest.reason_codes
    assert "countStatements" not in (v6.root / "hidden/harness/GraderSupport.java").read_text(
        "utf-8"
    )


def test_the_helper_counts_on_a_business_api_chosen_by_code() -> None:
    """부를 API·뺄 표는 LLM 이 아니라 코드가 계약에서 고른다 — 세션으로 부르는, 경로 변수 없는 GET 이 돌려주는 엔티티의 표."""
    body = _helper(_with_get(_session(), "/orders", "m1[]"), 7)
    assert "protected long sessionPathStatements(String session, int n) throws Exception {" in body
    assert "                HttpResponse<String> response = getOrders(headers);" in body
    assert "protected HttpResponse<String> getOrders(Map<String, String> headers)" in body
    # 주문을 가리키는 주문 이력(관계 글)도 뺀다 — 역방향 연관을 매핑해 직렬화하면 읽는다
    assert (
        "return GraderJdbcHarness.statementsNotTouching(sql, TABLE_ORDER, TABLE_ORDER_HISTORY);"
        in body
    )
    assert "List<String> sql = countStatements(() -> {" in body
    # 세기의 전제 — 세션으로 부른 업무 API 가 성공해야 한다 (빠진 헤더로 400 이면 DB 0 번으로 통과하지 않게)
    assert "if (response.statusCode() / 100 != 2) {" in body
    assert 'throw new IllegalStateException("세기 전제 불성립 — GET /orders 응답 "' in body


def test_an_api_returning_the_login_entity_is_never_counted() -> None:
    """로그인(자격 증명) 엔티티를 돌려주는 API 를 고르면 '메모리 세션 + 요청마다 사용자 조회' 함정 변형의 조회가 업무 표 조회로 빠져
    살아남는다 — 그런 API 밖에 없으면 도우미를 만들지 않는다 (계약 검사 _count_target 이 다시 요청한다)."""
    c = _with_get(_session(), "/members/me", "m3")
    assert "sessionPathStatements" not in _helper(c, 7)
    both = _with_get(_with_get(_session(), "/members/me", "m3"), "/orders", "m1[]", "e5")
    assert _picked(both) == ("e5", ["m1", "m2"])


def test_an_entity_pointing_at_the_login_entity_is_never_counted() -> None:
    """외래 키로 로그인 엔티티를 가리키는 엔티티(2026-11-26 알파 fix337-1 의 WorkOrder → Operator) — 시작 코드가 @ManyToOne 으로
    매핑하면 업무 API 가 로그인 표를 정상으로 읽어, 함정 변형의 요청마다 사용자 조회와 표로 가를 수 없다."""
    c = _with_entity(_session(), "m4", "Note", ["memberId → Member"])
    assert _picked(_with_get(c, "/notes", "m4[]")) is None


def test_referenced_tables_are_left_out_and_self_contained_apis_come_first() -> None:
    """계약이 밝힌 참조(외래 키·관계·칸 타입)의 표는 세기에서 뺀다 — 2026-11-26 알파 frame-r2-3 의 주문 목록은 @OneToMany 품목을
    지연 로딩해 요청마다 품목 SELECT 가 났다. 참조 없는 API 가 있으면 그것부터."""
    assert _picked(_shipments()) == ("e4", ["m4", "m1"])
    assert "return GraderJdbcHarness.statementsNotTouching(sql, TABLE_SHIPMENT, TABLE_ORDER);" in (
        _helper(_shipments(), 7)
    )
    assert _picked(_with_get(_shipments(), "/orders", "m1[]", "e5")) == ("e5", ["m1", "m2", "m4"])


def test_relations_written_in_korean_still_count_as_references() -> None:
    """관계 글은 한국어 문장이 흔하다 — 파이썬 \\b 는 한글도 낱말 문자로 보아 'Order를' 의 Order 를 놓쳤다 (검토 지적). 놓치면 연관 표의
    SELECT 가 세기에 들어가 허용 구현이 떨어진다."""
    c = _with_entity(_session(), "m4", "Shipment", relations=["Shipment는 Order를 참조한다"])
    assert _picked(_with_get(c, "/shipments", "m4[]")) == ("e4", ["m4", "m1"])


def test_public_session_store_and_required_query_apis_are_skipped() -> None:
    """세션 없이 부르는 공개 조회는 세션 확인을 지나지 않는다 (도우미 함수도 headers 를 받지 않는다) · 세션 저장소로 보이는 엔티티는
    그 표를 빼면 흔한 구현의 세션 조회가 빠진다 · 필수 쿼리 매개변수를 빼고 부르면 400 이라 세션 확인 전에 끝날 수 있다."""
    assert _picked(_with_get(_session(), "/orders", "m1[]", session=False)) is None
    store = _with_entity(_session(), "m4", "LoginSession")
    assert _picked(_with_get(store, "/sessions", "m4[]")) is None
    q = [{"name": "status", "type": "string", "refs": ["r1"]}]
    assert _picked(_with_get(_session(), "/orders", "m1[]", query=q)) is None
    optional = [{"name": "status", "type": "string", "required": False, "refs": ["r1"]}]
    assert _picked(_with_get(_session(), "/orders", "m1[]", query=optional)) == ("e4", ["m1", "m2"])


def test_a_session_store_found_by_structure_is_never_left_out() -> None:
    """이름이 세션처럼 보이지 않아도 발급 값(sessionId)을 칸으로 가진, 어느 API 도 돌려주지 않는 엔티티는 세션 저장소다 (issued_stores) —
    그 표를 빼면 흔한 구현의 세션 조회가 세기에서 빠진다. 그것을 가리키는 업무 엔티티는 고르지 않고, 업무 엔티티를 가리켜도 빼지 않는다."""
    device = _with_entity(_session(), "m4", "Device", ["orderId → Order"], extra=("sessionId",))
    assert _picked(_with_get(device, "/orders", "m1[]")) == ("e4", ["m1", "m2"])
    ticket = _with_entity(
        _with_entity(_session(), "m4", "Device", extra=("sessionId",)),
        "m5",
        "Ticket",
        ["deviceId → Device"],
    )
    assert _picked(_with_get(ticket, "/tickets", "m5[]")) is None


def test_the_contract_check_asks_for_a_countable_api() -> None:
    """케이스가 db_per_request 를 쓰는데 고를 API 가 없으면 계약을 다시 요청한다 — 그대로 두면 부를 함수가 없어 채점 코드가 컴파일되지
    않는다. 고를 API 가 있거나, 케이스가 이 사유를 쓰지 않거나, 템플릿에 이 사유가 없으면 말하지 않는다."""
    raw = _plan()
    raw["cases"][0]["intended_reason"] = "db_per_request"
    counting = HiddenPlan.model_validate(raw)
    v7, v6 = load_template(JAVA, 7).manifest, load_template(JAVA, 6).manifest
    none = _fit(_with_get(_session(), "/members/me", "m3"))
    problems = _count_target(none, counting, v7)
    assert len(problems) == 1 and "db_per_request" in problems[0] and "GET" in problems[0]
    assert count_targets(none) == []
    assert _count_target(_fit(_with_get(_session(), "/orders", "m1[]")), counting, v7) == []
    assert _count_target(none, HiddenPlan.model_validate(_plan()), v7) == []
    assert _count_target(none, counting, v6) == []
    # 부를 API 는 있는데 로그인 세션 도우미를 만들 수 없음(로그인 행 전) — 도우미와 같은 판단이라 '다른 까닭'으로 다시 요청한다
    unfitted = TaskContract.model_validate(_with_get(_session(), "/orders", "m1[]"))
    assert count_targets(unfitted) and count_helper(unfitted, v7) is None
    (why,) = _count_target(unfitted, counting, v7)
    assert "로그인 세션 도우미" in why


def test_the_reason_rule_follows_a_variable_holding_the_count() -> None:
    """숨은 테스트까지 간 알파 두 회차(sql339-1·3) 모두 첫 답이 `long n = sessionPathStatements(…); assertThat(n).as(Reasons.as(…))` 였고
    한 회차는 재요청에도 같은 꼴이라 숨은 테스트 생성이 실패했다. 변수를 같은 파일에서 세기 결과로 대입했으면 그 단언이다 — 응답·다른 값에
    붙인 사유는 그대로 막는다."""
    import re

    from task_spec.schemas import GeneratedFile
    from task_spec.starter.static_check import _reason_rules

    m = load_template(JAVA, 7).manifest
    raw = _plan()
    raw["cases"][0]["intended_reason"] = "db_per_request"
    plan = HiddenPlan.model_validate(raw)
    cid = plan.cases[0].case_id

    def probs(body: str) -> list[str]:
        case = GeneratedFile(
            path="src/test/java/com/example/service/grading/cases/DbPerRequestTest.java",
            content=f'@DisplayName("{cid} 세션 확인이 요청마다 DB 를 쓰지 않음")\nclass DbPerRequestTest extends ContractCase {{\n'
            f"    @Test void t() throws Exception {{\n        String session = newSession();\n        int n = 20;\n{body}\n    }}\n}}",
        )
        rules = m.hidden_rules
        return _reason_rules(
            [case],
            m,
            plan,
            re.compile(rules.case_id_pattern),
            [re.compile(p) for p in rules.reason_patterns],
        )

    tag = '.as(Reasons.as("db_per_request", "요청 %d건 동안 업무 표 밖 SQL 문 수", n))'
    assert (
        probs(
            f"        long extra = sessionPathStatements(session, n);\n        assertThat(extra){tag}.isLessThan(n);"
        )
        == []
    )
    assert probs(f"        assertThat(sessionPathStatements(session, n)){tag}.isLessThan(n);") == []
    on_response = (
        "        long extra = sessionPathStatements(session, n);\n"
        "        HttpResponse<String> resp = getOrders(sessionHeaders(session));\n"
        f"        assertThat(resp.statusCode()){tag}.isEqualTo(200);"
    )
    other = f"        long extra = sessionPathStatements(session, n);\n        long fixed = 5L;\n        assertThat(fixed){tag}.isLessThan(n);"
    for body in (on_response, other):
        (why,) = probs(body)
        assert "그 사유를 보는 단언이 아닌 곳에 붙임" in why


def test_the_reason_rule_takes_the_nearest_assignment_only() -> None:
    """다른 메서드가 같은 이름(n)에 세기 결과를 담았어도, 이 단언 바로 앞의 대입이 다른 값이면 막는다 — 파일 전체에서 대입을 찾으면
    인정됐다 (#339 검토). 하네스는 표 이름을 표 자리에서만 대조한다 — 자체 점검이 칼럼 이름이 겹치는 꼴을 본다."""
    import re

    from task_spec.schemas import GeneratedFile
    from task_spec.starter.static_check import _reason_rules

    m = load_template(JAVA, 7).manifest
    raw = _plan()
    raw["cases"][0]["intended_reason"] = "db_per_request"
    plan = HiddenPlan.model_validate(raw)
    cid = plan.cases[0].case_id
    tag = '.as(Reasons.as("db_per_request", "업무 표 밖 SQL 문 수"))'
    content = (
        f'@DisplayName("{cid} 세션 확인이 요청마다 DB 를 쓰지 않음")\nclass DbPerRequestTest extends ContractCase {{\n'
        "    @Test void a() throws Exception { long n = sessionPathStatements(newSession(), 20); }\n"
        f"    @Test void b() throws Exception {{ long n = 5L; assertThat(n){tag}.isLessThan(20); }}\n}}"
    )
    case = GeneratedFile(
        path="src/test/java/com/example/service/grading/cases/DbPerRequestTest.java",
        content=content,
    )
    rules = m.hidden_rules
    (why,) = _reason_rules(
        [case],
        m,
        plan,
        re.compile(rules.case_id_pattern),
        [re.compile(p) for p in rules.reason_patterns],
    )
    assert "그 사유를 보는 단언이 아닌 곳에 붙임" in why
    selftest = (load_template(JAVA, 7).root / "hidden/selftest/HarnessSelfTest.java").read_text(
        "utf-8"
    )
    assert "표_이름은_표_자리에서만_대조한다" in selftest


def test_no_helper_before_v7_or_without_a_session() -> None:
    assert "sessionPathStatements" not in _helper(_with_get(_session(), "/orders", "m1[]"), 6)
    plain = _with_get(_session(auth=None), "/orders", "m1[]", session=False)
    assert "sessionPathStatements" not in _helper(plain, 7)
