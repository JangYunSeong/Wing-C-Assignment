"""숨은 테스트 도우미 ContractCase 를 계약에서 만든다 (2026-11-08 — 숨은 테스트가 id·응답 모양·Duration 을 손으로 쓰다 어긋남). 가상 데이터만."""

from __future__ import annotations

import re

from task_spec.llm import FakeBackend
from task_spec.nodes.starter.generate import gen_hidden, hidden_input
from task_spec.starter.contract import SpecView
from task_spec.starter.hidden_helpers import _inject_lines, method_name, render_contract_case
from task_spec.starter.seed_rows import allocate_rows
from task_spec.starter.static_check import Overlay, static_check
from tests.starter.test_contract import BACKEND_SEED
from tests.starter.test_generate import CASES, _files, _gen, _inputs
from tests.starter.test_static_check import HC1, JAVA, PLAN, _f, _overlays, _task

HELPER = "src/test/java/com/example/service/grading/cases/ContractCase.java"


def test_helper_has_ranges_tables_api_bodies_peak_and_num() -> None:
    b = _inputs()
    f = render_contract_case(b.contract, b.plan, JAVA.manifest)
    assert f is not None and f.path == HELPER
    src = f.content
    assert "abstract class ContractCase extends GraderSupport {" in src
    r = allocate_rows(b.contract, b.plan).ranges[0]
    assert f"protected static final long HC1_FIRST = {r.first}L;" in src
    assert f"protected static final int HC1_ROWS = {r.rows};" in src
    assert 'protected static final String TABLE_ORDER = "orders";' in src  # 예약어라 복수형
    # 계약 API — 헤더가 있는 PATCH 는 send, 목록 returns 는 List 로 읽는 Body 함수
    assert 'send("PATCH", "/orders/" + id + "/status", headers, jsonBody)' in src
    assert "List<Map<String, Object>> getOrdersIdHistoryBody(HttpResponse<String> response)" in src
    assert 'get("/orders/" + id + "/history")' in src
    # 운영 피크는 조절값·시간 한도를 넣어 둔다 (Duration 을 손으로 감싸지 않게)
    assert (
        'Burst.rounds(GraderParams.intValue("concurrency", 32), GraderParams.intValue("bursts",'
        in src
    )
    assert "REQUEST_TIMEOUT);" in src and "Duration.of" not in src
    assert "protected static long num(Object value)" in src


def test_method_names_follow_method_and_path() -> None:
    b = _inputs()
    names = [method_name(e) for e in b.contract.endpoints]
    assert names == ["patchOrdersIdStatus", "getOrdersIdHistory"]


async def test_gen_hidden_gets_helper_from_code_and_sees_its_source() -> None:
    b = _inputs()
    assert (
        "abstract class ContractCase"
        in hidden_input(b.task, b.contract, b.plan, b.template).harness_api
    )
    handwritten = (HELPER, "abstract class ContractCase {}")
    fake = FakeBackend().push(_files((f"{CASES}/OrderStatusBurstTest.java", HC1), handwritten))
    res = await _gen(gen_hidden, fake, b)
    assert res.ok and res.overlay is not None
    helper = [f for f in res.overlay.files if f.path == HELPER]
    assert (
        len(helper) == 1 and "HC1_FIRST" in helper[0].content
    )  # 손으로 쓴 것은 버리고 계약 렌더본


def test_cases_must_extend_the_helper_not_grader_support() -> None:
    direct = HC1.replace("extends ContractCase", "extends GraderSupport")

    def msgs(text: str, extra=()):  # type: ignore[no-untyped-def]
        o = _overlays(hidden=[_f(f"{CASES}/OrderStatusBurstTest.java", text), *extra])
        res = static_check(
            o,
            template=JAVA,
            plan=PLAN,
            spec=SpecView.from_task(_task()),
            contract=_inputs().contract,
            seed=BACKEND_SEED,
        )
        return res.for_target("gen_hidden")

    assert any("GraderSupport 를 직접 상속" in m for m in msgs(direct))
    assert not [m for m in msgs(HC1) if "상속" in m or "도우미" in m]
    # 도우미를 손으로 바꾸면 잡는다
    assert any(
        "계약 도우미" in m for m in msgs(HC1, [_f(HELPER, "abstract class ContractCase {}")])
    )
    assert Overlay  # 형식 확인용 import 사용


def _select_rx(table: str) -> re.Pattern[str]:
    """도우미가 쓰는 select 주입 정규식(Java 원문) — 문자열 이음을 풀어 같은 뜻의 파이썬 정규식으로 (\\s·\\b·.*? 는 두 언어가 같다)."""
    src = "\n".join(_inject_lines())
    found = re.search(
        r'Pattern\.compile\("(\^[^"]*select[^"]*)" \+ Pattern\.quote\(table\) \+ "([^"]*)"', src
    )
    assert found is not None
    head, tail = (g.replace("\\\\", "\\") for g in found.groups())
    return re.compile(head + re.escape(table) + tail, re.IGNORECASE | re.DOTALL)


def test_select_statement_failure_reaches_the_table_after_the_column_list() -> None:
    """#308 팩트체크 1·2차: sqlOn 이 select 뒤 열 목록을 건너뛰지 못해 injectStatementFailure("select", TABLE_…) 가 조용히 아무것도
    주입하지 못했다 — Hibernate 는 select 와 from 사이에 열 목록을 둔다. from·join 에 그 테이블이 오는 읽기만 실패시킨다."""
    rx = _select_rx("session")
    hit = [
        "select s1_0.id,s1_0.expires_at,s1_0.user_id from session s1_0 where s1_0.id=?",
        "SELECT S1_0.ID FROM SESSION S1_0",
        "select u1_0.id from users u1_0 join session s1_0 on s1_0.user_id=u1_0.id",
        "select s1_0.id\nfrom session s1_0",
    ]
    miss = [
        "select s1_0.id from session_log s1_0",
        "select s1_0.id from sessions s1_0",
        "update session set expires_at=? where id=?",
        "insert into session (id) values (?)",
    ]
    assert all(rx.search(sql) for sql in hit), [s for s in hit if not rx.search(s)]
    assert not any(rx.search(sql) for sql in miss), [s for s in miss if rx.search(s)]
