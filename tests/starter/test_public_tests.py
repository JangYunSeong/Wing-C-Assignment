"""테스트 먼저(TDD) — 공개 테스트(gen_public)·저장 확인·공개 게이트 (2026-11-08 사용자 결정). 가상 데이터만."""

from __future__ import annotations

from task_spec.llm import FakeBackend
from task_spec.nodes.starter.generate import gen_public, public_input, starter_input
from task_spec.schemas import VerifyReport
from task_spec.starter.contract import SpecView
from task_spec.starter.hidden_helpers import (
    public_rows,
    render_contract_case,
    render_persistence_check,
)
from task_spec.starter.repair import assign
from task_spec.starter.static_check import Overlay, static_check
from tests.starter.test_contract import BACKEND_SEED
from tests.starter.test_generate import _files, _gen, _inputs
from tests.starter.test_repair import GOOD_ACC, GOOD_NAIVE, MISSED, OVERLAYS, PLAN2, _run, _t

PUB = "src/test/java/com/example/service/publictests"
CASE = """package com.example.service.publictests;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

@DisplayName("pt1 변경 성공 시 이력 1건 · pt2 조회가 최신순")
class OrderHistoryPublicTest extends PublicCase {
    @Test
    void pt1() throws Exception {
        var r = getOrdersIdHistory(PUBLIC_ORDER_READY[0]);
    }
}
"""


def test_public_helper_has_public_rows_but_no_hidden_ranges_or_peak() -> None:
    b = _inputs()
    rows = public_rows(b.contract, b.plan)
    assert rows and all(len(v) <= 3 for v in rows.values())
    hidden_ids = {
        i
        for r in __import__("task_spec.starter.seed_rows", fromlist=["allocate_rows"])
        .allocate_rows(b.contract, b.plan)
        .ranges
        for i in range(r.first, r.last + 1)
    }
    assert all(i > 10 and i not in hidden_ids for ids in rows.values() for i in ids)
    src = render_contract_case(b.contract, b.plan, b.template.manifest, audience="public").content  # type: ignore[union-attr]
    assert "package com.example.service.publictests;" in src and "abstract class PublicCase" in src
    assert "protected static final long[] PUBLIC_ORDER" in src
    assert (
        "HC1_FIRST" not in src
        and "peak(" not in src
        and "TABLE_" not in src
        and "동시 요청" not in src
    )
    assert "getOrdersIdHistory" in src  # 계약 API 함수는 같다


def test_persistence_check_targets_new_empty_entities() -> None:
    b = _inputs()
    f = render_persistence_check(b.contract, b.template.manifest)
    assert f is not None and f.path == f"{PUB}/PersistenceCheckTest.java"
    assert 'new String[] {"OrderHistory"}' in f.content and "extends PublicCase" in f.content
    assert "getPersistenceUnitUtil().getIdentifier(row)" in f.content
    # 외래 키는 이 확인의 대상이 아니다 — 저장해 보는 동안만 참조 검사를 끄고(finally 에서 켬), 저장한 줄은 되돌린다
    # (2026-11-25 재시도 중복 알파: 임시 값이 시작 코드 FK 를 어겨 시작 코드를 다시 만들었고, 다시 만든 시작 코드가 FK 를 지웠다)
    body = f.content
    off = body.index("SET REFERENTIAL_INTEGRITY FALSE")
    on = body.index("SET REFERENTIAL_INTEGRITY TRUE")
    assert off < body.index("em.persist(row)") < body.index("finally") < on
    assert "status.setRollbackOnly()" in body and "executeWithoutResult" not in body
    # 처음 0행인 엔티티가 없으면 만들지 않는다
    seeded = b.contract.model_copy(
        update={
            "seed": [
                s.model_copy(update={"rows": 20, "groups": s.groups or []}) for s in b.contract.seed
            ]
        }
    )
    assert render_persistence_check(seeded, b.template.manifest) is None or "OrderHistory" not in (
        render_persistence_check(seeded, b.template.manifest).content  # type: ignore[union-attr]
    )


async def test_gen_public_keeps_code_written_helpers_and_checks_cases() -> None:
    b = _inputs()
    inp = public_input(b.task, b.contract, b.plan, b.template)
    assert "abstract class PublicCase" in inp.harness_api and "pt1 (p1)" in inp.public_tests
    assert "hc1" not in inp.public_tests and "pool" not in inp.harness_api.lower()
    # 허용 설계는 숨은 요구의 해법 — 숨은 테스트 입력(v8~)에만, 공개 테스트 입력에는 없다
    dumped = inp.model_dump_json()
    assert b.task.accepted_designs and all(d.summary not in dumped for d in b.task.accepted_designs)
    handwritten = (f"{PUB}/PublicCase.java", "abstract class PublicCase {}")
    fake = FakeBackend().push(_files((f"{PUB}/OrderHistoryPublicTest.java", CASE), handwritten))
    res = await _gen(gen_public, fake, b)
    assert res.ok and res.overlay is not None and res.overlay.kind == "public_test"
    paths = {f.path: f.content for f in res.overlay.files}
    assert "PUBLIC_ORDER" in paths[f"{PUB}/PublicCase.java"]  # 손으로 쓴 것은 버리고 렌더본
    assert f"{PUB}/PersistenceCheckTest.java" in paths


def test_public_static_checks() -> None:
    b = _inputs()

    def msgs(text: str) -> list[str]:
        o = Overlay(kind="public_test", files=(_files_one(text),))
        return static_check(
            [o],
            template=b.template,
            plan=b.plan,
            spec=SpecView.from_task(b.task),
            contract=b.contract,
            seed=BACKEND_SEED,
        ).for_target("gen_public")

    assert not msgs(CASE)
    assert any("pt2" in m for m in msgs(CASE.replace(" · pt2 조회가 최신순", "")))
    assert any(
        "상속하지 않음" in m
        for m in msgs(CASE.replace("extends PublicCase", "extends GraderSupport"))
    )
    burst = CASE.replace(
        "var r =", "var o = Burst.rounds(1, 1, i -> 200, i -> 200, null);\n        var r ="
    )
    assert any("동시 요청·오류 주입" in m for m in msgs(burst))


def _files_one(text: str):  # type: ignore[no-untyped-def]
    from task_spec.schemas import GeneratedFile

    return GeneratedFile(path=f"{PUB}/OrderHistoryPublicTest.java", content=text)


def test_starter_input_shows_public_tests() -> None:
    b = _inputs()
    pub = Overlay(kind="public_test", files=(_files_one(CASE),))
    assert (
        "OrderHistoryPublicTest"
        in starter_input(b.task, b.contract, b.template, public=pub).public_tests
    )
    assert starter_input(b.task, b.contract, b.template).public_tests == "(없음)"


def _p(name: str, result: str = "PASS", msg: str = "") -> dict:  # type: ignore[type-arg]
    return _t("public", name, result, None, msg)


def test_starter_public_gate() -> None:
    """2026-11-08 알파 4케이스: 새 엔티티 id 생성 전략 없음 — 저장 확인이 시작 코드 단계에서 잡는다."""
    refs = (_run("naive-all", "ref_naive", *GOOD_NAIVE), _run("d1", "ref_accepted", *GOOD_ACC))
    bad = _run(
        "starter",
        "starter",
        _p("PersistenceCheckTest", "HARNESS_ERR", "Identifier of entity must be manually assigned"),
        _p("OrderHistoryPublicTest", "ASSERT"),
    )
    plan = assign(VerifyReport(runs=[bad, *refs]), PLAN2, OVERLAYS, MISSED)
    assert [a.generator for a in plan.actions] == [
        "gen_starter"
    ] and "저장할 수 없음" in plan.actions[0].reason
    solved = _run("starter", "starter", _p("PersistenceCheckTest"), _p("OrderHistoryPublicTest"))
    plan = assign(VerifyReport(runs=[solved, *refs]), PLAN2, OVERLAYS, MISSED)
    assert [a.generator for a in plan.actions] == [
        "gen_starter"
    ] and "새 기능을 이미" in plan.actions[0].reason
    ok = _run(
        "starter", "starter", _p("PersistenceCheckTest"), _p("OrderHistoryPublicTest", "ASSERT")
    )
    assert not [
        a
        for a in assign(VerifyReport(runs=[ok, *refs]), PLAN2, OVERLAYS, MISSED).actions
        if a.generator == "gen_starter"
    ]


JAVA_TERMS = (
    __import__("task_spec.starter.templates", fromlist=["load_template"])
    .load_template("backend/java-spring-boot3-jpa")
    .manifest.repair_terms
)


def test_public_failures_are_assigned_by_comparison_and_untrusted_refs_skip_hidden_gates() -> None:
    starter = _run("starter", "starter", _p("PersistenceCheckTest"), _p("T", "ASSERT"))
    fail = _p("T", "ASSERT", "expected 200 but was 400")
    # 흔한 구현만 실패 → 흔한 구현 (그 구현의 숨은 결과는 믿지 않는다 — 흔한 구현이 숨은 케이스를 '통과'해도 ③ 배정 없음)
    naive = _run("naive-all", "ref_naive", fail, _t("hidden", "hc1"), _t("hidden", "hc2"))
    d1 = _run("d1", "ref_accepted", _p("T"), *GOOD_ACC)
    plan = assign(VerifyReport(runs=[starter, naive, d1]), PLAN2, OVERLAYS, MISSED)
    assert sorted(a.key for a in plan.actions) == ["gen_naive:naive-all"]
    # 이미 다시 만든 흔한 구현이 또 그 공개 테스트만 실패 → 누설 의심 (gen_public)
    plan = assign(
        VerifyReport(runs=[starter, naive, d1]), PLAN2, OVERLAYS, MISSED, {"gen_naive:naive-all": 1}
    )
    assert [a.generator for a in plan.actions] == ["gen_public"]
    # 모든 구현이 같은 서버 오류 → 공유 코드(시작 코드)
    srv = _p(
        "T",
        "HARNESS_ERR",
        "ServletException: JpaSystemException: Identifier must be manually assigned",
    )
    plan = assign(
        VerifyReport(
            runs=[
                starter,
                _run("naive-all", "ref_naive", srv, *GOOD_NAIVE),
                _run("d1", "ref_accepted", srv, *GOOD_ACC),
            ]
        ),
        PLAN2,
        OVERLAYS,
        MISSED,
        terms=JAVA_TERMS,  # 서버 쪽 예외의 꼴은 매니페스트 repair_terms.server_side (#176)
    )
    assert [a.generator for a in plan.actions] == ["gen_starter"]
    # 그 꼴을 선언하지 않은 템플릿(데이터·프론트)은 서버 쪽으로 보지 않는다 — 같은 메시지면 테스트 의심
    plan = assign(
        VerifyReport(
            runs=[
                starter,
                _run("naive-all", "ref_naive", srv, *GOOD_NAIVE),
                _run("d1", "ref_accepted", srv, *GOOD_ACC),
            ]
        ),
        PLAN2,
        OVERLAYS,
        MISSED,
    )
    assert [a.generator for a in plan.actions] == ["gen_public"]
    # 모든 구현이 같은 단언 실패 → 테스트 의심
    plan = assign(
        VerifyReport(
            runs=[
                starter,
                _run("naive-all", "ref_naive", fail, *GOOD_NAIVE),
                _run("d1", "ref_accepted", fail, *GOOD_ACC),
            ]
        ),
        PLAN2,
        OVERLAYS,
        MISSED,
    )
    assert [a.generator for a in plan.actions] == ["gen_public"]
