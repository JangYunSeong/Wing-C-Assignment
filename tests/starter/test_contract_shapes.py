"""HTTP + DB 행 밖의 계약 — 종료 코드·입력 환경(fixtures/setup)·seed_rows (2026-11-07 직군별 종이 단계). 가상 데이터만."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.nodes.starter.generate import plan_text
from task_spec.nodes.starter.plan_contract import seed_rules_text
from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.starter.render import contract_markdown
from tests.starter.test_contract import _check, _contract, _manifest, _plan


def _cli_contract(**over: Any) -> dict[str, Any]:
    c = _contract()
    c["interface_kind"] = "cli"
    for e in c["endpoints"]:
        e["method"] = None
        e["path"] = e["path"].strip("/").replace("{id}", "id").replace("/", "-")
        for r in e["responses"]:
            r["status"] = 0 if r["status"] == 200 else 1
    c["seed"] = []
    c.update(over)
    return c


def test_cli_exit_codes_and_http_status_ranges() -> None:
    TaskContract.model_validate(
        _cli_contract()
    )  # 종료 코드 0·1 — 2026-11-07: ≥100 만 받아 응답 전체가 거부됐다
    bad = _cli_contract()
    bad["endpoints"][0]["responses"][0]["status"] = 300
    with pytest.raises(ValidationError, match="종료 코드는 0~255"):
        TaskContract.model_validate(bad)
    http = _contract()
    http["endpoints"][0]["responses"][0]["status"] = 0
    with pytest.raises(ValidationError, match="HTTP 상태 코드는 100~599"):
        TaskContract.model_validate(http)


FIXTURES = [
    {
        "fixture_id": "f1",
        "kind": "db",
        "summary": "메타데이터 DB — 데이터베이스 둘에 같은 이름 테이블",
        "content": "analytics.orders (order_id, user_id)\nmarketing.orders (order_id, campaign_id)",
        "refs": ["r1"],
    }
]


def _no_rows_manifest():  # type: ignore[no-untyped-def]
    return _manifest(interfaces=["http", "cli"], seed_rows=False)


def test_non_row_templates_use_fixtures_instead_of_seed() -> None:
    plan = _plan()
    plan["cases"][0]["data"] = []
    plan["cases"][0]["setup"] = ["marketing DB 에 같은 이름 테이블 추가"]
    ok = _check(_cli_contract(fixtures=FIXTURES), plan, manifest=_no_rows_manifest())
    assert not [p for p in ok.problems if "시드" in p or "data" in p or "입력 환경" in p], (
        ok.problems
    )
    # 시드·행 배정을 쓰면 다시 요청
    seeded = _cli_contract(
        fixtures=FIXTURES,
        seed=[{"entity_id": "m1", "rows": 20, "groups": [{"first": 1, "last": 20, "values": {}}]}],
    )
    res = _check(seeded, _plan(), manifest=_no_rows_manifest())
    assert any("DB 행 시드를 쓰지 않는다" in p for p in res.problems)
    assert any("hc1 의 data 를 비운다" in p for p in res.problems)


def test_fixture_refs_ids_and_leaks_are_checked() -> None:
    fx = [dict(FIXTURES[0], refs=["r9"]), dict(FIXTURES[0])]
    res = _check(
        _cli_contract(fixtures=fx),
        _plan() | {"cases": [dict(_plan()["cases"][0], data=[])]},
        manifest=_no_rows_manifest(),
    )
    assert any("입력 환경 f1 의 refs 에 명세에 없는 id: r9" in p for p in res.problems)
    assert any("입력 환경 id" in p and "겹침" in p for p in res.problems)
    leak = [dict(FIXTURES[0], content="커넥션 풀 8개로 시작")]
    res = _check(
        _cli_contract(fixtures=leak),
        _plan() | {"cases": [dict(_plan()["cases"][0], data=[])]},
        manifest=_no_rows_manifest(),
    )
    assert any("숨은 요구" in p and "풀" in p for p in res.problems), res.problems


def test_fixtures_and_setup_are_shown_where_they_belong() -> None:
    contract = TaskContract.model_validate(_cli_contract(fixtures=FIXTURES))
    md = contract_markdown(contract)
    assert (
        "### 입력 환경" in md and "- `f1` (db) 메타데이터 DB" in md and "  marketing.orders" in md
    )
    plan = _plan()
    plan["cases"][0]["setup"] = ["sales DB 에 같은 이름 테이블 추가"]
    text = plan_text(HiddenPlan.model_validate(plan), contract)
    assert "  준비 (하네스가 입력 환경을 바꿈): sales DB 에 같은 이름 테이블 추가" in text
    assert "sales DB" not in md  # setup 은 숨김 — 계약에 안 감


def test_seed_rules_text_follows_the_template() -> None:
    assert "seed 와 숨은 케이스 data 는 비운다" in seed_rules_text(_no_rows_manifest())
    assert "작은 id" in seed_rules_text(_manifest())


SCREEN = {
    "screen_id": "s1",
    "route": "/orders",
    "summary": "주문 상태를 바꾸고 이력을 본다",
    "elements": [
        {
            "test_id": "status-select",
            "role": "input",
            "description": "바꿀 상태",
            "action": "상태를 고르면 바로 변경 요청",
            "refs": ["r1"],
        },
        {
            "test_id": "history-list",
            "role": "list",
            "description": "변경 이력 (최신순)",
            "refs": ["r2"],
        },
    ],
    "on_load": "GET /orders/{id}/history 로 이력을 읽어 보여 줌",
    "storage": [
        {"name": "drafts", "kind": "indexeddb", "shape": "{id, status, updatedAt}", "refs": ["r1"]}
    ],
    "refs": ["r1", "r2"],
}


def _ui_contract(**over: Any) -> dict[str, Any]:
    c = _contract()
    c["interface_kind"] = "ui"
    c["seed"] = []
    c["screens"] = [SCREEN]
    c["covers"] = {"r1": ["e1", "m1", "m2", "s1"], "r2": ["e2", "m2", "s1"]}
    c["background"] = "없음"
    c.update(over)
    return c


def _ui_plan() -> dict[str, Any]:
    plan = _plan()
    plan["cases"][0]["data"] = []
    plan["cases"][0]["setup"] = ["같은 출처 탭 4개를 연다"]
    return plan


def test_ui_contract_needs_screens_and_only_ui_has_them() -> None:
    TaskContract.model_validate(_ui_contract())
    with pytest.raises(ValidationError, match="screens 가 하나 이상"):
        TaskContract.model_validate(_ui_contract(screens=[]))
    with pytest.raises(ValidationError, match="screens 를 쓰지 않음"):
        TaskContract.model_validate(_contract() | {"screens": [SCREEN]})


def test_ui_contract_checks() -> None:
    m = _manifest(interfaces=["ui"], seed_rows=False)
    ok = _check(_ui_contract(), _ui_plan(), manifest=m)
    assert not [p for p in ok.problems if "화면" in p or "test id" in p or "covers" in p], (
        ok.problems
    )
    bad_screen = dict(
        SCREEN,
        elements=[
            {
                "test_id": "Status_Select",
                "role": "input",
                "description": "바꿀 상태",
                "refs": ["r9"],
            },
            {
                "test_id": "Status_Select",
                "role": "list",
                "description": "커넥션 풀 상태",
                "refs": ["r2"],
            },
        ],
    )
    res = _check(_ui_contract(screens=[bad_screen]), _ui_plan(), manifest=m)
    assert any("test id 가 템플릿 규칙과 다름: Status_Select" in p for p in res.problems)
    # 조작하는 요소(input)에 조작 방법이 없음 — 2026-11-09 React 알파: 저장 시점을 테스트·구현이 다르게 짐작
    assert any("조작 방법(action)이 없음: Status_Select" in p for p in res.problems)
    assert any("test id" in p and "겹침" in p for p in res.problems)
    assert any("요소 Status_Select 의 refs 에 명세에 없는 id: r9" in p for p in res.problems)
    assert any("숨은 요구" in p and "풀" in p for p in res.problems)  # 화면 문구도 누출 검사
    # 화면 진입·배경 요청을 정하지 않으면 재요청 — 2026-11-09 React 알파: 테스트는 '들어오면 GET', 구현은 로컬만 읽음
    vague = _check(
        _ui_contract(
            screens=[{k: v for k, v in SCREEN.items() if k != "on_load"}], background=None
        ),
        _ui_plan(),
        manifest=m,
    )
    assert any("on_load)이 없음: /orders" in p for p in vague.problems)
    assert any("background)이 없음" in p for p in vague.problems)
    assert not [p for p in ok.problems if "on_load" in p or "background" in p]
    loose = _check(
        _ui_contract(covers={"r1": ["e1", "m1", "m2"], "r2": ["e2", "m2"]}), _ui_plan(), manifest=m
    )
    assert any("연결되지 않은 계약 요소: s1" in p for p in loose.problems)


def test_ui_contract_markdown() -> None:
    md = contract_markdown(TaskContract.model_validate(_ui_contract()))
    assert "### 화면 `/orders`" in md and "| `status-select` | input | 바꿀 상태 |" in md
    assert "브라우저 저장소: `drafts` (indexeddb)" in md
    assert "화면에 들어오면: GET /orders/{id}/history" in md
    assert "### 화면 조작 없이 나가는 요청\n\n없음" in md


def test_lenient_rules_from_the_v7_paper_run() -> None:
    """2026-11-07 v7 종이 단계: 너무 엄격했던 규칙 — covers 가 입력 환경을 가리킴, covers 키에 p*, 화면마다 같은 부품."""
    plan = _plan()
    plan["cases"][0]["data"] = []
    m = _no_rows_manifest()
    c = _cli_contract(fixtures=FIXTURES)
    c["covers"] = {"r1": ["e1", "m1", "m2", "f1"], "r2": ["e2", "m2"], "p1": ["e1"]}
    res = _check(c, plan, manifest=m)
    assert not [p for p in res.problems if "covers" in p], res.problems
    c["covers"]["r2"] = ["e2", "m2", "f9"]
    assert any(
        "covers[r2] 에 계약에 없는 id: f9" in p for p in _check(c, plan, manifest=m).problems
    )
    # 같은 test id 가 화면 둘에 있는 것은 된다, 한 화면 안에서 겹치면 안 된다
    second = dict(SCREEN, screen_id="s2", route="/orders/history")
    ui = _ui_contract(screens=[SCREEN, second])
    ui["covers"]["r2"] = ["e2", "m2", "s1", "s2"]
    mu = _manifest(interfaces=["ui"], seed_rows=False)
    assert not [p for p in _check(ui, _ui_plan(), manifest=mu).problems if "test id" in p]
    twice = dict(SCREEN, elements=[SCREEN["elements"][0], SCREEN["elements"][0]])
    assert any(
        "화면 s1 의 test id" in p and "겹침" in p
        for p in _check(_ui_contract(screens=[twice]), _ui_plan(), manifest=mu).problems
    )
