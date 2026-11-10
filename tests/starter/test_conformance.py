"""계약 적합성 스모크 — 계약 모양 예시 응답·화면별 검사 생성, 수리 게이트 ⓪ 의 탓 정하기. 가상 데이터만."""

from __future__ import annotations

import json
import re

import pytest

from task_spec.schemas import TargetRun, TaskContract, VerifyReport
from task_spec.starter.conformance import (
    CONFORM_CONFIG,
    CONFORM_TEST,
    _Examples,
    render_conformance,
    type_kind,
)
from task_spec.starter.repair import assign
from task_spec.starter.templates import load_template
from tests.starter.test_frontend_template import FE, _contract
from tests.starter.test_repair import GOOD_ACC, GOOD_NAIVE, MISSED, OVERLAYS, PLAN2, _run, _t


@pytest.mark.parametrize(
    ("typ", "kind"),
    [
        ("string", "string"),
        ("VARCHAR", "string"),
        ("bigint", "integer"),
        ("Long", "integer"),
        ("timestamp", "datetime"),
        ("LocalDateTime", "datetime"),
        ("BigDecimal", "number"),
        ("enum(READY|SHIPPED)", "enum"),
        ("m2[]", "list"),
        ("array<Notification>", "list"),
        ("m1", "ref"),
    ],
)
def test_free_form_contract_types_are_grouped_by_meaning(typ: str, kind: str) -> None:
    """저장 계약의 필드 타입은 생성기마다 다르게 쓴다 (2026-11-10 집계) — 예시 값을 만들 때만 뜻으로 묶는다."""
    assert type_kind(typ) == kind


def _list_contract() -> TaskContract:
    """응답이 엔티티 배열인 계약 — 2026-11-10 fefix e99cbba0 와 같은 모양."""
    d = _contract().model_dump(mode="json")
    d["entities"][0]["fields"] = [
        {"name": "id", "type": "integer", "refs": ["r1"]},
        {"name": "name", "type": "string", "refs": ["r1"]},
        {"name": "status", "type": "enum(ON|OFF)", "refs": ["r1"]},
    ]
    d["endpoints"][0]["responses"] = [
        {"status": 200, "description": "추천", "returns": "m1[]"},
        {
            "status": 500,
            "description": "오류",
            "body": [{"name": "message", "type": "string", "refs": ["r1"]}],
        },
    ]
    return TaskContract.model_validate(d)


def test_example_response_has_the_contract_shape() -> None:
    c = _list_contract()
    ex = _Examples(c)
    body = ex.response(c.endpoints[0].responses[0])
    assert isinstance(body, list) and body[0]["id"] == 1 and body[0]["status"] == "ON"


def test_smoke_routes_every_endpoint_with_its_success_example_and_types_inputs() -> None:
    files = {f.path: f.content for f in render_conformance(_list_contract(), FE.manifest)}
    assert set(files) == {CONFORM_TEST, CONFORM_CONFIG}
    test = files[CONFORM_TEST]
    m = re.search(r"const ROUTES: .*? = (\[.*\]);", test)
    assert m is not None
    routes = json.loads(m.group(1))
    assert routes == [
        ["GET", "/api/products/suggest", 200, [{"id": 1, "name": "가상 name", "status": "ON"}]]
    ]
    assert (
        'renderApp("/search")' in test and '["search-input"]' in test
    )  # combobox 검색 입력란도 글을 넣는다
    assert CONFORM_TEST.startswith("__conform__/")  # tsconfig(src)·지원자 설정(src/**)·채점 설정 밖
    assert "__conform__/**/*.test.{ts,tsx}" in files[CONFORM_CONFIG]
    assert "from '../src/__grading__/harness/grader'" in test


def test_not_applied_to_java_templates() -> None:
    java = load_template("backend/java-spring-boot3-jpa")
    assert render_conformance(_list_contract(), java.manifest) == []


def _conform(
    name: str = "s1 /search — 계약 모양 응답에 화면이 깨지지 않는다", ok: bool = True
) -> dict[str, object]:
    return _t(
        "conform",
        name,
        "PASS" if ok else "HARNESS_ERR",
        None,
        "" if ok else "앱 오류: Cannot read properties of undefined",
    )


def _assign(*runs: TargetRun, starter_ok: bool = True):  # type: ignore[no-untyped-def]
    starter = _run("starter", "starter", _conform(ok=starter_ok))
    return assign(VerifyReport(runs=[starter, *runs]), PLAN2, OVERLAYS, MISSED, {})


def test_gate0_starter_broken_by_contract_shape_goes_to_starter() -> None:
    plan = _assign(_run("d1", "ref_accepted", *GOOD_ACC, _conform()), starter_ok=False)
    assert [(a.generator, a.gate) for a in plan.actions] == [("gen_starter", "⓪")]
    assert "계약의 모양" in plan.actions[0].note and "Cannot read" in plan.actions[0].note


def test_gate0_all_implementations_broken_the_same_way_is_shared_code() -> None:
    plan = _assign(
        _run("naive-all", "ref_naive", *GOOD_NAIVE, _conform(ok=False)),
        _run("d1", "ref_accepted", *GOOD_ACC, _conform(ok=False)),
    )
    assert [(a.generator, a.gate) for a in plan.actions] == [("gen_starter", "⓪")]
    assert "공유 코드" in plan.actions[0].reason


def test_gate0_some_implementations_broken_regenerates_them_only() -> None:
    """깨진 구현만 다시 — 이번 회차 다른 게이트는 보지 않는다 (남은 구현끼리 비교하면 '모두 실패'를 잘못 읽는다)."""
    broken = _run(
        "d1", "ref_accepted", _t("hidden", "hc1", "ASSERT", None, "x"), _conform(ok=False)
    )
    plan = _assign(
        _run("naive-all", "ref_naive", *GOOD_NAIVE, _conform()),
        broken,
        _run("d2", "ref_accepted", *GOOD_ACC, _conform()),
    )
    assert [(a.generator, a.label, a.gate) for a in plan.actions] == [("gen_accepted", "d1", "⓪")]
    # 남은 구현 하나가 공개 테스트에서 떨어져도 이번 회차에는 공개 테스트를 의심하지 않는다 (2026-11-10 e99cbba0 재검증)
    lone = _run(
        "d2",
        "ref_accepted",
        *GOOD_ACC,
        _t("public", "pt1", "HARNESS_ERR", None, "요청이 오지 않음"),
        _conform(),
    )
    plan = _assign(_run("naive-all", "ref_naive", *GOOD_NAIVE, _conform(ok=False)), lone)
    assert [a.generator for a in plan.actions] == ["gen_naive"]


def test_gate0_silent_when_everything_conforms() -> None:
    plan = _assign(
        _run("naive-all", "ref_naive", *GOOD_NAIVE, _conform()),
        _run("d1", "ref_accepted", *GOOD_ACC, _conform()),
    )
    assert plan.passed
