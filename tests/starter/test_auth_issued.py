"""권한 칸 issued — 로그인 같은 다른 API 가 응답으로 발급한 값을 헤더로 받는 규칙 (가상 데이터).

운영 빌드 ct-f6087a7ba679-b1 (2026-11-24): 세션 유지 과제의 계약이 로그인 401 에 auth 칸을, X-Session-Id 에 통과 값 예시를
요구받아 세 번 모두 계약 단계에서 실패했다 — 발급받는 값은 role·fixed·other 어느 것으로도 적을 수 없었다.
"""

from __future__ import annotations

from typing import Any

from task_spec.schemas import GeneratedFile, HiddenPlan, TaskContract
from task_spec.starter.auth_rule import auth_problems, fit_auth, requester_rows
from task_spec.starter.header_values import ISSUED, header_problems, header_values
from task_spec.starter.hidden_helpers import render_contract_case
from task_spec.starter.seed_rows import seed_problems
from task_spec.starter.static_check import Overlay
from tests.starter.test_contract import _contract, _plan, _spec
from tests.starter.test_static_check import CASES, JAVA

HEADER = "X-Session-Id"
ISSUED_RULE = {
    "kind": "issued",
    "header": HEADER,
    "denied_status": 401,
    "entity": "m3",
    "issued_by": "e3",
    "issued_field": "sessionId",
}


def _f(name: str, typ: str = "string") -> dict[str, Any]:
    return {"name": name, "type": typ, "refs": ["r1"]}


def _session(auth: dict[str, Any] | None = ISSUED_RULE, **login: Any) -> dict[str, Any]:
    """주문 상태 변경(e1)이 로그인(e3)으로 받은 세션을 X-Session-Id 로 받는 계약."""
    c = _contract()
    e = c["endpoints"][0]
    e["headers"] = [{"name": HEADER, "type": "string", "refs": ["r1"]}]
    e["responses"].append({"status": 401, "description": "세션 없음·유효하지 않음"})
    e["auth"] = auth
    c["endpoints"].append(
        {
            "endpoint_id": "e3",
            "method": "POST",
            "path": "/api/auth/login",
            "summary": "로그인 — 세션 발급",
            "request": [_f("username"), _f("password")],
            "responses": [
                {"status": 200, "description": "세션 발급", "body": [_f("sessionId")]},
                {"status": 401, "description": "자격 증명 불일치"},
            ],
            "refs": ["r1"],
            **login,
        }
    )
    c["entities"].append(
        {
            "entity_id": "m3",
            "name": "Member",
            "fields": [
                _f("id", "integer"),
                _f("username"),
                _f("password"),
                _f("nickname"),
            ],
        }
    )
    c["seed"].append(
        {
            "entity_id": "m3",
            "rows": 5,
            "groups": [{"first": 1, "last": 5, "values": {"nickname": "가상회원"}}],
        }
    )
    return c


def _fit(c: dict[str, Any]) -> TaskContract:
    return fit_auth(TaskContract.model_validate(c))[0]


def test_issued_rule_adds_a_login_row_and_passes_both_checks() -> None:
    """운영 실패의 두 사유 — 로그인 401 의 auth 칸 · 세션 헤더의 통과 값 예시 — 가 issued 칸 하나로 풀린다."""
    fitted, notes = fit_auth(TaskContract.model_validate(_session()))
    rule = fitted.endpoints[0].auth
    assert rule is not None and rule.rows == [6]
    assert (rule.ok_value, rule.denied_value) == (None, "not-issued-session")
    member = next(s for s in fitted.seed if s.entity_id == "m3")
    assert member.rows == 6
    assert member.groups[-1].model_dump() == {
        "first": 6,
        "last": 6,
        "values": {"username": "username-6", "password": "password-6"},
    }
    assert notes and "id 6" in notes[0] and "POST /api/auth/login" in notes[0]
    assert requester_rows(fitted) == {"m3": {6}}  # 숨은 케이스·공개 몫 배정에서 뺀다
    assert not auth_problems(fitted) and not header_problems(fitted) and not seed_problems(fitted)
    [h] = header_values(fitted)
    assert (h.name, h.ok, h.denied, h.source) == (HEADER, None, "not-issued-session", ISSUED)
    said = fitted.endpoints[0].headers[0].constraints or ""
    assert "POST /api/auth/login 성공 응답의 sessionId 값을 그대로 넣는다" in said
    assert "없거나 발급하지 않은 값이면 401" in said and "시드 값은 평문" in said
    assert fit_auth(fitted)[0] == fitted  # 두 번 불러도 같다


def test_login_denial_needs_no_auth_only_when_an_issued_rule_points_at_it() -> None:
    # issued 가 없으면 로그인의 401 도 칸을 요구하되, 헤더 없는 엔드포인트에는 issued 로 적는 길을 알린다
    probs = auth_problems(TaskContract.model_validate(_session(auth=None)))
    login = [p for p in probs if p.startswith("POST /api/auth/login auth")]
    assert login and '"issued_by": "e3"' in login[0]
    assert any(p.startswith("PATCH /orders/{id}/status auth") for p in probs)
    # 헤더가 있는 엔드포인트의 403 에는 issued 안내를 붙이지 않는다
    patch = next(p for p in probs if p.startswith("PATCH"))
    assert "issued" not in patch


def test_issued_rule_problems() -> None:
    def probs(rule: dict[str, Any], **login: Any) -> list[str]:
        return auth_problems(_fit(_session({**ISSUED_RULE, **rule}, **login)))

    assert any("issued_by e9 가 계약 엔드포인트에 없음" in p for p in probs({"issued_by": "e9"}))
    assert any("자기 자신" in p for p in probs({"issued_by": "e1"}))
    assert any("issued_field token 가" in p for p in probs({"issued_field": "token"}))
    assert any("issued_field(" in p for p in probs({"issued_field": None}))
    # 자격 증명 칸 이름을 가진 엔티티가 없으면 로그인 행을 만들 수 없다
    bad = probs({}, request=[_f("loginId"), _f("password")])
    assert any("요청 칸 loginId, password 를 모두 가진 엔티티가 없음" in p for p in bad)
    # 날짜·시각 칸은 행으로 값을 정할 수 없다
    c = _session()
    c["entities"][-1]["fields"].append(_f("birthDate", "date"))
    c["endpoints"][2]["request"].append(_f("birthDate", "date"))
    assert any("날짜·시각·외래 키" in p for p in auth_problems(_fit(c)))
    # 자격 증명은 본문으로 — 경로 변수 있는 GET 은 받지 않는다
    assert any("POST·PUT·PATCH" in p for p in probs({}, method="GET", path="/login/{username}"))
    # 성공 응답 본문 대신 returns 로 적어도 칸을 찾는다
    c = _session()
    c["endpoints"][2]["responses"][0] = {"status": 200, "description": "세션", "returns": "v1"}
    c["response_shapes"] = [
        {"shape_id": "v1", "name": "LoginResult", "fields": [_f("sessionId"), _f("expiresAt")]}
    ]
    assert not auth_problems(_fit(c))


def test_issued_rules_must_agree_across_endpoints() -> None:
    c = _session()
    c["endpoints"][1]["headers"] = [{"name": HEADER, "type": "string", "refs": ["r1"]}]
    c["endpoints"][1]["responses"].append({"status": 401, "description": "세션 없음"})
    c["endpoints"][1]["auth"] = {**ISSUED_RULE, "issued_field": "token"}
    c["endpoints"][2]["responses"][0]["body"].append(_f("token"))
    assert any("issued 칸이 서로 다름" in p for p in auth_problems(_fit(c)))
    c["endpoints"][1]["auth"] = ISSUED_RULE  # 같으면 로그인 행은 하나
    fitted = _fit(c)
    assert not auth_problems(fitted)
    assert fitted.endpoints[0].auth == fitted.endpoints[1].auth
    assert next(s for s in fitted.seed if s.entity_id == "m3").rows == 6


def test_login_entity_is_derived_from_the_credential_fields() -> None:
    """2026-11-24 v32 회귀: 재료가 세션 엔티티를 요구하자 entity 를 Session 으로 적어(로그인 칸은 User 에) 계약이 실패했다 —
    계약만 보고 정할 수 있는 값은 묻지 않는다. 발급 API 요청 칸을 모두 가진 엔티티가 하나면 그것, 여럿일 때만 entity 로 고른다."""
    session_entity = {
        "entity_id": "m4",
        "name": "Session",
        "fields": [_f("id", "integer"), _f("memberId", "integer"), _f("expiresAt", "datetime")],
    }
    for entity in (None, "m4"):
        c = _session({**ISSUED_RULE, "entity": entity})
        c["entities"].append(session_entity)
        c["seed"].append({"entity_id": "m4", "rows": 0, "groups": []})
        fitted, notes = fit_auth(TaskContract.model_validate(c))
        rule = fitted.endpoints[0].auth
        assert rule is not None and (rule.entity, rule.rows) == ("m3", [6])
        assert any("로그인 엔티티를 Member 로" in n for n in notes)
        assert ("Session 대신" in notes[0]) == (entity == "m4")
        assert not auth_problems(fitted)
    # 같은 칸을 가진 엔티티가 여럿이면 고르지 않는다 — entity 로 고르게 묻는다
    c = _session({**ISSUED_RULE, "entity": None})
    c["entities"].append({**c["entities"][-1], "entity_id": "m4", "name": "Admin"})
    c["seed"].append({**c["seed"][-1], "entity_id": "m4"})
    fitted = _fit(c)
    assert fitted.endpoints[0].auth is not None and fitted.endpoints[0].auth.rows == []
    assert any("가진 엔티티가 여럿 (m3 Member, m4 Admin)" in p for p in auth_problems(fitted))
    c["endpoints"][0]["auth"]["entity"] = "m4"
    assert not auth_problems(_fit(c))


def test_prefix_and_email_credentials() -> None:
    c = _session({**ISSUED_RULE, "prefix": "Bearer "}, request=[_f("email"), _f("password")])
    c["entities"][-1]["fields"].append(_f("email", "email"))
    fitted = _fit(c)
    rule = fitted.endpoints[0].auth
    assert rule is not None and rule.denied_value == "Bearer not-issued-session"
    group = next(s for s in fitted.seed if s.entity_id == "m3").groups[-1]
    # 형식이 email 인 칸은 이메일 모양 — 로그인 요청 검증(@Email 등)에 막히지 않게 (이름이 아니라 계약이 선언한 형식으로)
    assert group.values == {"email": "user6@example.com", "password": "password-6"}
    assert "앞에 'Bearer ' 를 붙여" in (fitted.endpoints[0].headers[0].constraints or "")


def test_helper_logs_in_with_the_login_row_for_a_session() -> None:
    fitted = _fit(_session())
    helper = render_contract_case(fitted, HiddenPlan.model_validate(_plan()), JAVA.manifest)
    assert helper is not None
    body = helper.content
    assert "protected static final long SESSION_USER_ID = 6L;" in body
    # 자격 증명은 행의 함수 — 시드 SQL 과 같은 규칙 (그 행의 그룹 값, 없으면 '<칸>-<행>' 20자 안)
    assert "protected static String loginBody(long row) {" in body
    assert "protected static String loginBody(long row, String field, Object value) {" in body
    assert 'body.put("username", credentialUsername(row));' in body
    assert 'if (row >= 6 && row <= 6) return "username-6";' in body
    assert (
        'String s = "-" + row, n = "password"; '
        "return n.substring(0, Math.min(n.length(), Math.max(0, 20 - s.length()))) + s;"
    ) in body  # seed_sql.text_default — 넘치면 칸 이름 쪽을 잘라 행 번호를 지킨다
    assert "HttpResponse<String> r = postApiAuthLogin(Map.of(), loginBody(row));" in body
    assert "protected String newSession(long row) {" in body
    assert "return newSession(SESSION_USER_ID);" in body
    assert 'jsonObject(r.body()).get("sessionId")' in body
    assert 'return "" + v;' in body
    # 세션은 고정 값이 없다 — 통과 상수 없이 로그인하는 okHeaders(), 미리 받은 세션으로 sessionHeaders(session)
    assert 'HEADER_X_SESSION_ID_DENIED = "not-issued-session"' in body
    assert "HEADER_X_SESSION_ID_OK" not in body
    assert "protected Map<String, String> okHeaders() {" in body
    assert "protected static Map<String, String> okHeaders()" not in body
    assert 'return Map.of("X-Session-Id", session);' in body
    assert "peak" in body  # 동시 요청 전에 받아 두라는 안내


def test_helper_session_with_a_per_request_key_and_prefix() -> None:
    c = _session({**ISSUED_RULE, "prefix": "Bearer "})
    c["endpoints"][0]["headers"].append(
        {"name": "X-Request-Id", "type": "string", "per_request": True, "refs": ["r1"]}
    )
    helper = render_contract_case(_fit(c), HiddenPlan.model_validate(_plan()), JAVA.manifest)
    assert helper is not None
    body = helper.content
    assert 'return "Bearer " + v;' in body
    assert "return sessionHeaders(newSession(), newRequestId());" in body
    assert "protected Map<String, String> okHeaders(String key) {" in body
    assert 'return Map.of("X-Session-Id", session, "X-Request-Id", key);' in body


def test_ui_contract_keeps_its_issued_rule_and_header_text() -> None:
    """화면 과제는 권한 검사·도우미가 칸을 쓰지 않는다 — 칸에서 쓴 문장으로 헤더 설명(README)을 덮지 않는다
    (2026-11-24 v32 회귀: 토큰 갱신 화면 과제에서 갱신 API 도 발급하는데 '로그인 응답의 값' 으로 덮였다)."""
    c = TaskContract.model_validate(_session())
    header = (
        c.endpoints[0]
        .headers[0]
        .model_copy(update={"constraints": "로그인·갱신 응답의 accessToken"})
    )
    endpoints = [c.endpoints[0].model_copy(update={"headers": [header]}), *c.endpoints[1:]]
    ui = c.model_copy(update={"interface_kind": "ui", "endpoints": endpoints})
    fitted, notes = fit_auth(ui, seed_rows=False)
    assert fitted == ui and notes == []


def test_credentials_follow_the_seed_sql_values() -> None:
    """도우미의 행별 자격 증명이 시드 SQL 이 넣는 값과 같다 — 그룹 값·정수·enum·글자 기본값."""
    from task_spec.starter.auth_rule import credentials
    from task_spec.starter.seed_sql import default_value

    c = _session(request=[_f("username"), _f("pin", "integer"), _f("tier", "enum(GOLD|SILVER)")])
    c["entities"][-1]["fields"] += [_f("pin", "integer"), _f("tier", "enum(GOLD|SILVER)")]
    c["seed"][-1]["groups"] = [
        {"first": 1, "last": 3, "values": {"nickname": "가상회원", "username": "same"}},
        {"first": 4, "last": 5, "values": {"nickname": "가상회원"}},
    ]
    fitted = _fit(c)
    rule = fitted.endpoints[0].auth
    assert rule is not None
    creds = credentials(fitted, rule)
    assert creds is not None
    member = next(m for m in fitted.entities if m.entity_id == "m3")
    seed = next(s for s in fitted.seed if s.entity_id == "m3")
    for row in range(1, seed.rows + 1):
        group = next(g for g in seed.groups if g.first <= row <= g.last).values
        for cr in creds:
            f = next(x for x in member.fields if x.name == cr.name)
            want = group.get(cr.name, default_value(f, row))
            got = next((v for a, b, v in cr.groups if a <= row <= b), None)
            if got is None:
                got = {"row": row, "fixed": cr.fixed}.get(cr.default, f"{cr.name}-{row}"[:20])
            assert got == want, (row, cr.name)


def _overlay(kind: str, path: str, content: str) -> Overlay:
    return Overlay(kind=kind, files=(GeneratedFile(path=path, content=content),))  # type: ignore[arg-type]


def test_hand_built_login_body_in_tests_is_reasked() -> None:
    """2026-11-24 세션 알파: 공개 4개·숨은 1개 테스트가 자격 증명을 "user-"+id·"pass-"+id 로 지어 모든 구현에서 401.
    계약이 선언한 발급 API 요청 칸 이름이 모두 글자로 나오면 손으로 만든 본문이다 — 낱말 목록이 아니라 계약의 칸 이름."""
    from task_spec.starter.static_check import _hand_built_login

    fitted = _fit(_session())

    def probs(code: str) -> list[str]:
        return _hand_built_login(
            [GeneratedFile(path=f"{CASES}/Hc1Test.java", content=code)], fitted, "숨은 테스트"
        )

    assert probs(
        'String body = "{\\"username\\": \\"user-" + id + "\\", \\"password\\": \\"pass-" + id + "\\"}";'
    )
    assert probs('var body = Map.of("username", "u", "password", "p");')  # 만드는 방식과 상관없이
    assert "loginBody(row)" in probs('Map.of("username", "u", "password", "p")')[0]
    assert not probs("var r = postApiAuthLogin(Map.of(), loginBody(HC1_FIRST + i));")
    assert not probs('var r = postApiAuthLogin(Map.of(), loginBody(row, "password", "wrong"));')
    assert not probs(
        'assertThat(body.get("username")).isEqualTo("username-6");'
    )  # 응답 단언의 칸 하나


def test_starter_tests_do_not_call_apis_locked_behind_a_new_login() -> None:
    """2026-11-24 세션 알파: 시작 코드의 기존 기능 테스트가 기존 API 를 X-Session-Id: dummy-session 으로 불러 200 을 단언 — 시작 코드는
    통과, 참조 구현 3개는 모두 401 이라 '계약 모호 의심'으로 빌드가 멈췄다. 로그인(발급 API)이 새것이면 그 값이 필요한 기존 API 는
    시작 코드가 부를 수 없다 (계약에서 도출)."""
    from task_spec.starter.static_check import _starter_issued_calls

    c = _session()
    c["endpoints"][0]["existing"] = True  # 주문 상태 변경은 기존 API, 로그인(e3)은 새 API
    fitted = _fit(c)
    spec = _spec()

    def probs(code: str) -> list[str]:
        o = _overlay("starter", "src/test/java/com/example/service/ExistingApiTest.java", code)
        return [p.message for p in _starter_issued_calls(o, fitted, spec)]

    http_client = 'HttpRequest.newBuilder().uri(URI.create("http://localhost:" + port + "/orders/" + id + "/status"))'
    assert probs(http_client + '.header("X-Session-Id", "dummy-session")')
    assert probs(http_client)  # 헤더 없이 불러도 — 구현하면 401
    got = probs('mockMvc.perform(patch("/orders/1/status").header("X-Session-Id", "x"))')
    assert got and "PATCH /orders/{id}/status" in got[0] and "POST /api/auth/login" in got[0]
    assert not probs('mockMvc.perform(get("/orders/1/history"))')  # 세션이 필요 없는 기존 API
    # 로그인이 기존 API 면 시작 코드도 로그인할 수 있다 — 보지 않는다
    c["endpoints"][2]["existing"] = True
    o = _overlay("starter", "src/test/java/com/example/service/ExistingApiTest.java", http_client)
    assert not _starter_issued_calls(o, _fit(c), spec)


def test_starter_generator_is_told_which_existing_apis_are_locked() -> None:
    """2026-11-24 운영 빌드 ct-f6087a7ba679-b2: 시작 코드 검사만 잠긴 기존 API 를 알고 생성기는 몰라, '기존 API 로 기존 기능 테스트를
    하나 이상'(gen_starter)과 '이 API 는 부르지 않는다'(검사)가 부딪쳐 시작 코드 생성이 재시도 한도에서 끝났다 (기존 API 가 모두 잠김).
    생성기 입력의 기존 API 줄에 같은 판정(auth_rule.locked_existing)으로 표시한다 — 줄은 그대로 둔다 (구현은 해야 한다)."""
    from task_spec.nodes.starter.generate import existing_apis
    from tests.starter.test_plan_contract import _task

    c = _session()
    for e in c["endpoints"][:2]:  # 주문 상태 변경·이력 조회 둘 다 기존 API, 이력 조회도 세션이 필요
        e["existing"] = True
    c["endpoints"][1]["headers"] = [{"name": HEADER, "type": "string", "refs": ["r1"]}]
    c["endpoints"][1]["responses"].append({"status": 401, "description": "세션 없음"})
    c["endpoints"][1]["auth"] = ISSUED_RULE
    fitted = _fit(c)
    lines = existing_apis(_task(), fitted)
    assert len(lines) == 2  # 잠긴 API 도 목록에 남는다
    assert all(
        "기존 기능 테스트에서 부르지 않는다" in x and "POST /api/auth/login" in x for x in lines
    )
    assert all("시드·저장소 수준으로 확인" in x for x in lines)
    # 로그인이 기존 API 면 시작 코드도 로그인할 수 있다 — 표시하지 않는다
    c["endpoints"][2]["existing"] = True
    assert not any("부르지 않는다" in x for x in existing_apis(_task(), _fit(c)))


def _with_store(returns: str | None = None) -> tuple[TaskContract, HiddenPlan]:
    """운영 b6 모양 (가상) — 세션(m4, 로그인이 발급하는 sessionId 를 담음)이 시드 10행의 기존 테이블이고, 공개 몫·숨은 버스트가 그 행을 쓴다."""
    from tests.starter.test_seed_rows import _cases, _rows

    c = _session()
    c["entities"].append(
        {
            "entity_id": "m4",
            "name": "Session",
            "existing": True,
            "fields": [
                _f("id", "integer"),
                _f("sessionId"),
                _f("memberId", "integer"),
                _f("expiresAt"),
            ],
            "foreign_keys": ["memberId → Member"],
        }
    )
    c["seed"].append(
        {
            "entity_id": "m4",
            "rows": 10,
            "groups": [{"first": 1, "last": 10, "values": {"memberId": 1}}],
        }
    )
    c["public_tests"][0]["rows"] = [{"entity_id": "m4", "values": {"memberId": 1}}]
    c["covers"] = {k: [*v, "m4"] for k, v in c.get("covers", {}).items()}
    if returns is not None:
        c["endpoints"].append(
            {
                "endpoint_id": "e4",
                "method": "GET",
                "path": "/api/sessions",
                "summary": "내 세션 목록",
                "responses": [{"status": 200, "description": "세션 목록", "returns": returns}],
                "refs": ["r1"],
            }
        )
    # nickname 은 회원에도 있는 이름 — 저장소 칸 값은 이름이 같아도 옮기지 않는다 (운영 b6: 세션 userId ↔ 회원 기본 키 userId)
    plan = _cases(("hc1", [_rows(30, burst=True, entity="m4", memberId=1, nickname="x")]))
    return TaskContract.model_validate(c), plan


def test_the_issued_value_store_is_left_to_the_candidate() -> None:
    """운영 b6 (2026-11-25): 계약이 세션을 시드 323행의 기존 테이블로 만들어, 규칙 글('id 6~10 은 만료')을 믿은 시작 코드 테스트와
    세션 행 공개 몫으로 로그인한 공개 테스트가 떨어졌다. 발급 값의 저장소는 지원자 설계 몫 — 공개 계약에서 빼고, 숨은 케이스가
    쓰던 행은 로그인 엔티티 행으로 옮긴다 (버스트는 그대로 — 요청마다 다른 회원으로 로그인)."""
    from task_spec.starter.auth_rule import fit_issued_store, issued_stores

    contract, plan = _with_store()
    assert issued_stores(contract) == ["m4"]
    fitted, fplan, notes = fit_issued_store(contract, plan)
    assert [m.entity_id for m in fitted.entities] == ["m1", "m2", "m3"]
    assert all(s.entity_id != "m4" for s in fitted.seed)
    assert all(u.entity_id != "m4" for t in fitted.public_tests for u in t.rows)
    assert all("m4" not in v for v in fitted.covers.values())
    data = fplan.cases[0].data[0]
    assert (data.entity_id, data.rows, data.burst, dict(data.initial)) == ("m3", 30, True, {})
    assert "Session 엔티티를 공개 계약에서 뺌" in notes[0] and "sessionId" in notes[0]
    assert "숨은 케이스 hc1 의 그 행을 로그인 엔티티 Member 행으로" in notes[0]


def test_an_entity_the_api_returns_is_not_an_issued_store() -> None:
    """API 가 그 엔티티를 돌려주면(내 세션 목록 등) 공개 동작이라 저장소가 아니다 — 그대로 둔다. 발급 규칙이 없는 계약도 그대로."""
    from task_spec.starter.auth_rule import fit_issued_store, issued_stores

    contract, plan = _with_store(returns="m4[]")
    assert issued_stores(contract) == []
    assert fit_issued_store(contract, plan) == (contract, plan, [])
    plain = TaskContract.model_validate(_contract())
    assert issued_stores(plain) == []


def test_issued_store_contract_passes_the_contract_stage() -> None:
    """계약 단계 전체(fit_output → check_contract)를 거쳐도 세션은 다시 생기지 않고, 옮긴 버스트 행으로 회원 시드가 늘며 문제가 없다."""
    from task_spec.nodes.starter.plan_contract import fit_output
    from task_spec.schemas import PlanContractOutput
    from task_spec.starter.contract import check_contract
    from task_spec.starter.templates import load_template
    from tests.starter.test_seed_rows import _spec
    from tests.starter.test_templates import JAVA

    java = load_template(JAVA).manifest
    contract, plan = _with_store()
    out, notes = fit_output(PlanContractOutput(contract=contract, hidden_plan=plan), java, _spec())
    assert all(m.name != "Session" for m in out.contract.entities)
    member = next(s for s in out.contract.seed if s.entity_id == "m3")
    assert member.rows >= 30
    assert any("Session 엔티티를 공개 계약에서 뺌" in n for n in notes)
    problems = check_contract(out.contract, out.hidden_plan, _spec(), java).problems
    assert not [p for p in problems if "m4" in p or "Session" in p]


def test_the_starter_may_keep_its_own_issued_value_table() -> None:
    """#322 알파 (2026-11-25): 계약에서 세션을 빼자 시작 코드가 기존 API 의 세션 확인에 쓸 session·user_session 테이블을 만들었고, 시작 코드
    테이블 검사('계약 테이블 밖')에 두 번 막혀 생성 실패. 그 검사의 목적은 '숨은 테스트가 그 이름으로 오류를 주입한다'인데 발급 값의 저장소는
    계약 밖이고 테스트는 HTTP 로만 판정한다 — 발급 값 칸을 가진 테이블은 검사에서 뺀다. 다른 계약 밖 테이블은 그대로 막는다."""
    from task_spec.starter.static_check import _starter_tables
    from task_spec.starter.tables import tables_with_fields
    from task_spec.starter.templates import load_template
    from tests.starter.test_templates import JAVA

    t = load_template(JAVA)
    store = "CREATE TABLE user_session (\n  id BIGINT PRIMARY KEY,\n  session_id VARCHAR(64) NOT NULL,\n  member_id BIGINT\n);"
    audit = "CREATE TABLE audit_log (\n  id BIGINT PRIMARY KEY,\n  memo VARCHAR(64)\n);"
    entity = (
        '@Entity\n@Table(name = "login_session")\npublic class LoginSession {\n'
        "    @Id private Long id;\n    private String sessionId;\n}\n"
    )

    def files(*sql: str) -> tuple[GeneratedFile, ...]:
        return (
            GeneratedFile(path="src/main/resources/schema.sql", content="\n".join(sql)),
            GeneratedFile(
                path="src/main/java/com/example/service/domain/LoginSession.java", content=entity
            ),
        )

    assert tables_with_fields(files(store, audit), ["sessionId"]) == {
        "user_session",
        "login_session",
    }
    issued = _fit(_session())

    def problems(contract: TaskContract, *sql: str) -> list[str]:
        return [
            p.message
            for p in _starter_tables(Overlay(kind="starter", files=files(*sql)), t, contract)
        ]

    assert problems(issued, store) == []
    got = problems(issued, store, audit)
    assert len(got) == 1 and "audit_log" in got[0] and "user_session" not in got[0]
    # 발급 규칙이 없는 계약에서는 세션 테이블도 계약 밖이다
    plain = TaskContract.model_validate(_contract())
    assert "user_session" in problems(plain, store)[0]


def test_a_trap_frame_forbidden_store_is_not_excused_by_the_issued_value_exception() -> None:
    """설계 검사 #332 T-F2: 발급 값 예외(#322)는 함정 틀이 금지한 저장 엔티티를 통과시키지 않는다. 예외 전 코드에서 돈 알파(r2 3/3)는
    '계약 테이블 밖'이 막았지만, 예외가 들어온 뒤에는 token·session_id 열을 가진 세션 테이블이 그대로 시작 코드로 나갔다."""
    from task_spec.schemas import ForbiddenStorage
    from task_spec.starter.static_check import _starter_forbidden_storage, _starter_tables
    from task_spec.starter.templates import load_template
    from tests.starter.test_templates import JAVA

    t = load_template(JAVA)
    store = "CREATE TABLE user_session (\n  id BIGINT PRIMARY KEY,\n  session_id VARCHAR(64) NOT NULL,\n  member_id BIGINT\n);"
    starter = Overlay(
        kind="starter",
        files=(GeneratedFile(path="src/main/resources/schema.sql", content=store),),
    )
    session = ForbiddenStorage(entity="Session", why="가상 이유")
    issued = _fit(_session())
    assert [p.message for p in _starter_tables(starter, t, issued)] == []  # 틀이 없으면 예외 그대로
    assert [
        p.message for p in _starter_tables(starter, t, issued, [session])
    ] == []  # 금지 테이블은 전용 검사 몫
    got = [p.message for p in _starter_forbidden_storage(starter, [session])]
    assert len(got) == 1 and "user_session" in got[0] and "함정 틀" in got[0]


def test_the_public_helper_has_no_operating_condition_hint() -> None:
    """설계 검사 #332 B-F11: 공개 도우미(공개 테스트 — 지원자에게 줄 수 있음)의 고정 주석에는 운영 조건 낱말·기전('동시 요청(peak)…
    서버 자원')을 쓰지 않는다. 숨은 도우미는 숨은 케이스를 쓰는 생성기에 필요한 안내를 그대로 둔다."""
    contract = _fit(_session())
    plan = HiddenPlan.model_validate(_plan())
    public = render_contract_case(contract, plan, JAVA.manifest, audience="public")
    hidden = render_contract_case(contract, plan, JAVA.manifest)
    assert public is not None and hidden is not None
    assert (
        "newSession" in public.content
        and "동시 요청" not in public.content
        and "서버 자원" not in public.content
    )
    assert "동시 요청(peak)" in hidden.content
