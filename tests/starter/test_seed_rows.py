"""계약 시드 검사와 숨은 케이스 행 배정 (starter/seed_rows.py) — 가상 데이터만."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.auth_rule import fit_auth
from task_spec.starter.hidden_helpers import row_use_consts
from task_spec.starter.seed_rows import (
    PUBLIC_PER_GROUP,
    RESERVED_ROWS,
    allocate_rows,
    case_rows_problems,
    fit_seed,
    peak_reasons,
    public_free,
    public_per_group,
    row_use_problems,
    seed_markdown,
    seed_problems,
    unique_fields,
)
from task_spec.starter.templates import load_template
from tests.starter.test_auth_issued import _session
from tests.starter.test_contract import _check, _contract, _manifest, _plan
from tests.starter.test_templates import JAVA


def _seed(
    rows: int, *groups: tuple[int, int, dict[str, Any]], entity: str = "m1"
) -> dict[str, Any]:
    return {
        "entity_id": entity,
        "rows": rows,
        "groups": [{"first": a, "last": b, "values": v} for a, b, v in groups],
    }


def _with_seed(*seeds: dict[str, Any]) -> TaskContract:
    c = _contract()
    c["seed"] = list(seeds)
    return TaskContract.model_validate(c)


def _cases(*cases: tuple[str, list[dict[str, Any]]]) -> HiddenPlan:
    """버스트 행이 없는 케이스는 동시 요청이 필요 없는 사유로 — 기본 케이스의 pool_acquire_timeout 을 그대로 두면 '동시 요청 사유인데
    버스트 행 없음'(case_rows_problems)에 걸린다."""
    base = _plan()["cases"][0]
    return HiddenPlan.model_validate(
        _plan(
            harness={"pool_size": 8, "concurrency": 32, "bursts": 3, "query_delay_ms": 20},
            cases=[
                base
                | {
                    "case_id": cid,
                    "ac_id": f"h{n}",
                    "data": data,
                    "intended_reason": base["intended_reason"]
                    if any(d.get("burst") for d in data)
                    else "record_mismatch",
                }
                for n, (cid, data) in enumerate(cases, 1)
            ],
        )
    )


def _rows(n: int, burst: bool = False, entity: str = "m1", **initial: Any) -> dict[str, Any]:
    return {"entity_id": entity, "rows": n, "initial": initial, "burst": burst}


TWO_GROUPS = _seed(200, (1, 150, {"status": "READY"}), (151, 200, {"status": "SHIPPING"}))


def _has(problems: list[str], *needles: str) -> bool:
    return any(all(n in p for n in needles) for p in problems)


def test_allocation_takes_big_ids_first_in_case_order() -> None:
    plan = _cases(
        ("hc1", [_rows(96, burst=True, status="READY")]),
        ("hc2", [_rows(2, status="READY")]),
        ("hc3", [_rows(5, status="SHIPPING")]),
        ("hc4", [_rows(1)]),  # 조건 없음 → 가장 큰 id 쪽 그룹
    )
    alloc = allocate_rows(_with_seed(TWO_GROUPS), plan)
    assert not alloc.problems
    got = {r.case_id: (r.first, r.last) for r in alloc.ranges}
    assert got == {"hc1": (55, 150), "hc2": (53, 54), "hc3": (196, 200), "hc4": (195, 195)}
    assert alloc.for_case("hc1")[0].burst and alloc.for_case("hc1")[0].rows == 96


def test_allocation_keeps_small_ids_for_candidates() -> None:
    contract = _with_seed(_seed(100, (1, 100, {"status": "READY"})))
    ok = allocate_rows(contract, _cases(("hc1", [_rows(100 - RESERVED_ROWS, status="READY")])))
    assert not ok.problems and ok.ranges[0].first == RESERVED_ROWS + 1
    short = allocate_rows(contract, _cases(("hc1", [_rows(96, burst=True, status="READY")])))
    assert _has(short.problems, "hc1", "96개", "남은 맞는 행 90개", f"작은 id {RESERVED_ROWS}행")
    # 처음 값이 맞는 그룹이 없음
    none = allocate_rows(contract, _cases(("hc1", [_rows(1, status="DELIVERED")])))
    assert _has(none.problems, "hc1", "status=DELIVERED", "남은 맞는 행 0개")


def test_case_rows_need_a_seeded_entity() -> None:
    contract = _with_seed(TWO_GROUPS, {"entity_id": "m2", "rows": 0})
    alloc = allocate_rows(contract, _cases(("hc1", [_rows(1, entity="m2")])))
    assert _has(alloc.problems, "hc1", "OrderHistory", "시드 행이 없음")


@pytest.mark.parametrize(
    ("seed", "needle"),
    [
        (_seed(200, (1, 100, {}), (102, 200, {})), "id 101~101 이 어느 범위에도 없음"),
        (_seed(200, (1, 120, {}), (100, 200, {})), "범위 100~200 가 앞 범위와 겹침"),
        (_seed(100, (1, 120, {})), "rows=100 를 넘음"),
        (_seed(200, (1, 150, {})), "id 151~200 이 어느 범위에도 없음"),
        (_seed(0, (1, 5, {})), "0행인데 groups"),
        ({"entity_id": "m1", "rows": 50}, "groups 가 없음"),
        (_seed(10, (1, 10, {}), entity="m9"), "m9: 엔티티가 계약에 없음"),
        (_seed(10, (1, 10, {"colour": "red"})), "Order 에 없는 필드 colour"),
        (_seed(10, (1, 10, {"id": 3})), "id 는 값으로 정하지 않는다"),
    ],
)
def test_seed_rule_problems(seed: dict[str, Any], needle: str) -> None:
    assert _has(seed_problems(_with_seed(seed)), needle), seed_problems(_with_seed(seed))


def test_seed_values_respect_enumerated_fields() -> None:
    history = _seed(3, (1, 3, {"result": "MAYBE"}), entity="m2")  # type enum(SUCCESS|FAILURE)
    assert _has(
        seed_problems(_with_seed(TWO_GROUPS, history)), "OrderHistory.result=MAYBE", "SUCCESS"
    )
    c = _contract()
    c["entities"][0]["fields"][1]["constraints"] = "'READY', 'SHIPPING', 'DELIVERED' 중 하나"
    c["seed"] = [_seed(10, (1, 10, {"status": "LOST"}))]
    assert _has(seed_problems(TaskContract.model_validate(c)), "Order.status=LOST", "DELIVERED")
    c["seed"] = [_seed(10, (1, 10, {"status": "READY"}))]
    assert not seed_problems(TaskContract.model_validate(c))


def test_lookup_key_fixed_for_many_rows() -> None:
    # 경로 변수로 한 행을 찾는데 그룹이 그 필드를 모든 행에 같은 값으로 — 여러 건 조회 오류 (alpha-cap1)
    c = _contract()
    c["entities"][0]["fields"] += [
        {"name": "shopId", "type": "integer", "refs": ["r1"]},
        {"name": "orderNo", "type": "integer", "refs": ["r1"]},
    ]
    c["endpoints"][0]["path"] = "/shops/{shopId}/orders/{orderNo}/status"
    c["seed"] = [_seed(200, (1, 200, {"status": "READY", "shopId": 7, "orderNo": 1}))]
    got = seed_problems(TaskContract.model_validate(c))
    assert _has(got, "범위 1~200", "shopId, orderNo", "orderNo 는 values 에서 뺀다"), got
    assert not _has(got, "목록을 돌려주는")  # 변경(PATCH)은 목록 API 가 아니다
    # 하나라도 행마다 다르면(코드 기본값), 한 행 그룹이면, id 로 찾으면 괜찮다
    for seed in (
        _seed(200, (1, 200, {"status": "READY", "shopId": 7})),
        _seed(1, (1, 1, {"shopId": 7, "orderNo": 1})),
    ):
        c["seed"] = [seed]
        assert not seed_problems(TaskContract.model_validate(c))
    c["endpoints"][0]["path"] = "/shops/{shopId}/orders/{id}/status"
    c["seed"] = [_seed(200, (1, 200, {"shopId": 7, "orderNo": 1}))]
    assert not seed_problems(TaskContract.model_validate(c))


def test_two_rules_for_one_entity() -> None:
    assert _has(seed_problems(_with_seed(TWO_GROUPS, TWO_GROUPS)), "둘 이상")


def test_burst_case_needs_concurrency_times_bursts_rows() -> None:
    java = load_template(JAVA).manifest
    contract = _with_seed(TWO_GROUPS)
    plan = _cases(("hc1", [_rows(32, burst=True, status="READY")]))
    assert _has(
        case_rows_problems(contract, plan, java),
        "hc1",
        "버스트 행 32개",
        "concurrency(32) × bursts(3) = 96",
    )
    plan = _cases(("hc1", [_rows(96, burst=True, status="READY")]))
    assert not case_rows_problems(contract, plan, java)
    # 버스트가 아닌 케이스는 곱을 요구하지 않는다
    plan = _cases(("hc1", [_rows(2, status="READY")]))
    assert not case_rows_problems(contract, plan, java)


def test_case_initial_fields_are_checked() -> None:
    plan = _cases(("hc1", [_rows(2, colour="red")]))
    assert _has(
        case_rows_problems(_with_seed(TWO_GROUPS), plan, _manifest()), "hc1", "없는 필드 colour"
    )


def test_check_contract_reports_seed_and_allocation_problems() -> None:
    c = _contract()
    c["seed"] = [_seed(50, (1, 40, {"status": "READY"}))]
    res = _check(c)
    assert _has(res.problems, "id 41~50 이 어느 범위에도 없음")
    assert _has(res.problems, "hc1", "이어진 범위로 나눌 수 없음")
    assert not res.spec_gaps


def test_manifest_burst_rows_must_name_int_params() -> None:
    data = _manifest().model_dump() | {"burst_rows": ["concurrency", "nope"]}
    with pytest.raises(ValidationError, match="burst_rows 의 nope"):
        TemplateManifest.model_validate(data)


def test_seed_markdown() -> None:
    contract = _with_seed(TWO_GROUPS)
    line = seed_markdown(contract.seed[0], "Order")
    assert (
        line
        == "- `Order` 200행 (id 1~200) — id 1~150: `status`=READY · id 151~200: `status`=SHIPPING"
        " — 테스트에서 바꿔도 되는 행은 id 1~10"  # 채점 테스트가 큰 id 를 쓴다 (2026-11-07 v2 스모크: 시작 코드 테스트가 id 22)
    )
    blank = _with_seed(
        _seed(5, (1, 5, {})), {"entity_id": "m2", "rows": 0, "rules": ["요청마다 쌓임"]}
    )
    assert seed_markdown(blank.seed[0], "Order") == "- `Order` 5행 (id 1~5)"
    assert (
        seed_markdown(blank.seed[1], "OrderHistory")
        == "- `OrderHistory` 0행 (처음엔 비어 있음) — 요청마다 쌓임"
    )


def test_seed_markdown_states_values_code_fills_for_ungrouped_fields() -> None:
    # 2026-11-10 gen-item: 시드 줄에 없던 playerId(실제 'playerId-1')를 시작 코드 테스트가 'player-1' 로 짐작 — 기존 테스트 5개 실패
    from task_spec.schemas.build import ContractField, Entity

    contract = _with_seed(TWO_GROUPS)
    order = Entity(
        entity_id="m1",
        name="Order",
        existing=True,
        fields=[
            ContractField(name="id", type="integer", refs=["r1"]),
            ContractField(name="status", type="enum(READY|SHIPPING)", refs=["r1"]),
            ContractField(name="ownerId", type="string", refs=["r1"]),
            ContractField(name="qty", type="integer", refs=["r1"]),
        ],
    )
    line = seed_markdown(contract.seed[0], "Order", order)
    assert (
        "그 밖의 필드는 행마다: `ownerId`='ownerId-<id>' (예: id 1 → 'ownerId-1'), `qty`=그 행의 id"
        in line
    )
    assert (
        "`status`=READY" in line and "그 밖의 필드는 행마다: `status`" not in line
    )  # 그룹이 정한 필드는 그대로
    # 날짜·시각 칸은 행마다 다르고 id 순서와 무관하다고 적는다 (2026-11-13 — 예전엔 '고정 시각' 하나)
    timed = order.model_copy(
        update={
            "fields": [
                *order.fields,
                ContractField(name="createdAt", type="string", constraints="ISO 8601", refs=["r1"]),
            ]
        }
    )
    line = seed_markdown(contract.seed[0], "Order", timed)
    assert "`createdAt`=행마다 다른 값, id 순서와 무관 (예: id 1 → '2026-01-05T04:07:00')" in line


def test_shortage_states_the_exact_rows_needed() -> None:
    """2026-11-07 v3 스모크: rows=106(96+10)로 hc2 를 빠뜨렸고 "늘린다"만으론 재요청도 틀렸다 → 필요한 수를 적는다."""
    contract = _with_seed(_seed(106, (1, 106, {"status": "READY"})))
    plan = _cases(
        ("hc1", [_rows(96, burst=True, status="READY")]), ("hc2", [_rows(1, status="READY")])
    )
    problems = allocate_rows(contract, plan).problems
    assert _has(problems, "Order 시드는 rows ≥ 107", "지금 106", "hc1 96 + hc2 1 = 97")
    assert _has(problems, "status=READY 97행")


def test_fit_seed_fixes_counts_the_llm_got_wrong() -> None:
    """2026-11-07 v3 스모크: 버스트 행을 concurrency 만큼만, 시드는 케이스 합을 빠뜨림, 빈 기록 엔티티에 data."""
    java = load_template(JAVA).manifest
    contract = _with_seed(
        _seed(64, (1, 10, {"status": "READY"}), (11, 64, {"status": "READY"})),
        {"entity_id": "m2", "rows": 0},
    )
    plan = _cases(
        ("hc1", [_rows(32, burst=True, status="READY"), _rows(5, entity="m2")]),
        ("hc2", [_rows(12, status="READY")]),
    )
    fitted, fplan, notes = fit_seed(contract, plan, java)
    assert _has(notes, "hc1", "버스트 행 32 → 96", "concurrency × bursts")
    assert _has(notes, "hc1", "처음에 비어 있는 OrderHistory 의 data 를 뺌")
    assert _has(notes, "Order 시드 64 → ", "status=READY")
    alloc = allocate_rows(fitted, fplan)
    assert not alloc.problems and not seed_problems(fitted)
    assert {r.case_id: r.rows for r in alloc.ranges} == {"hc1": 96, "hc2": 12}
    # 끝 범위와 처음 값이 같으면 범위를 늘린다 (새 범위를 덧붙이지 않음)
    assert len(fitted.seed[0].groups) == 2 and fitted.seed[0].groups[-1].last == fitted.seed[0].rows
    assert not case_rows_problems(fitted, fplan, java)


def test_fit_seed_appends_a_group_for_other_initial_values_and_small_seeds() -> None:
    java = load_template(JAVA).manifest
    contract = _with_seed(_seed(4, (1, 4, {"status": "READY"})))
    plan = _cases(("hc1", [_rows(3, status="SHIPPING")]))
    no_public = java.model_copy(update={"public_helper": None})
    fitted, fplan, notes = fit_seed(contract, plan, no_public)
    s = fitted.seed[0]
    # 시드가 지원자 몫보다 작으면 그 위로 — id 5~13 은 SHIPPING (11~13 을 hc1 이 씀)
    assert s.rows == RESERVED_ROWS + 3 and s.groups[-1].values == {"status": "SHIPPING"}
    assert [(r.first, r.last) for r in allocate_rows(fitted, fplan).ranges] == [(11, 13)]
    assert len(notes) == 1


def test_fit_seed_leaves_public_rows_per_initial_value() -> None:
    """2026-11-08 상류 평가: 숨은 케이스가 11~172 를 모두 가져가 공개 몫 상수가 없었고 공개 테스트가 없는 이름을 짐작했다 (2/2)."""
    java = load_template(JAVA).manifest
    full = _with_seed(_seed(106, (1, 106, {"status": "READY"})))
    plan = _cases(("hc1", [_rows(96, burst=True, status="READY")]))
    assert [ids for _, _, ids in public_free(full, plan)] == [[]]  # 11~106 을 숨은 케이스가 다 씀
    fitted, fplan, notes = fit_seed(full, plan, java)
    assert _has(notes, "Order 시드 106 → 109행", "공개 테스트 몫 3행")
    assert len(fitted.seed[0].groups) == 1  # 끝 범위와 처음 값이 같으면 늘린다
    free = public_free(fitted, fplan)
    assert free == [("m1", {"status": "READY"}, [11, 12, 13])]
    hidden = {i for r in allocate_rows(fitted, fplan).ranges for i in range(r.first, r.last + 1)}
    assert not hidden & set(free[0][2]) and not seed_problems(fitted)
    # 처음 값마다 따로 — 작은 시드(1~4 READY)에 다른 값(SHIPPING) 숨은 케이스
    small = _with_seed(_seed(4, (1, 4, {"status": "READY"})))
    fitted, fplan, _ = fit_seed(small, _cases(("hc1", [_rows(3, status="SHIPPING")])), java)
    assert sorted((v["status"], len(ids)) for _, v, ids in public_free(fitted, fplan)) == [
        ("READY", 3),
        ("SHIPPING", 3),
    ]
    assert not allocate_rows(fitted, fplan).problems and not seed_problems(fitted)
    # 공개 테스트가 없는 템플릿은 그대로
    assert fit_seed(full, plan, java.model_copy(update={"public_helper": None}))[2] == []


def test_fit_seed_public_rows_ignore_key_order_of_group_values() -> None:
    """2026-11-13 #164 평가(좌석 알파 v26): 경계 그룹(남은 1·0·50)의 values 칸 순서가 공통 값(status)을 앞에 두는 덧붙임 그룹과 달라
    같은 처음 값을 다른 그룹으로 세었다 — 같은 그룹을 5번 덧붙이고, 남은 0·50 그룹에는 공개 몫 상수가 하나도 생기지 않았다."""
    java = load_template(JAVA).manifest
    contract = _with_seed(
        _seed(
            61,
            (1, 5, {"remainingSeats": 1, "totalSeats": 50, "status": "OPEN"}),
            (6, 10, {"remainingSeats": 0, "totalSeats": 30, "status": "OPEN"}),
            (11, 61, {"remainingSeats": 50, "totalSeats": 50, "status": "OPEN"}),
        )
    )
    plan = _cases(("hc1", [_rows(51, burst=True, remainingSeats=50, totalSeats=50, status="OPEN")]))
    fitted, fplan, notes = fit_seed(contract, plan, java)
    free = public_free(fitted, fplan)
    # 처음 값마다 한 번 — 칸 순서가 달라도 같은 처음 값이면 같은 그룹
    assert sorted((v["remainingSeats"], len(ids)) for _, v, ids in free) == [
        (0, 3),
        (1, 3),
        (50, 3),
    ]
    assert len([n for n in notes if "공개 테스트 몫" in n]) == 3
    assert not allocate_rows(fitted, fplan).problems


def test_fit_seed_adds_groups_for_rows_public_tests_use() -> None:
    """#164: 공개 테스트가 쓰겠다고 한 행(PublicTest.rows — 경계 값·걸러질 행)의 처음 값이 시드에 없으면 시드 끝에 그룹을 덧붙이고,
    그 그룹에도 공개 몫 상수가 생긴다. 이미 있는 처음 값(칸 일부만 적어도)은 그대로."""
    java = load_template(JAVA).manifest
    c = _with_seed(_seed(20, (1, 20, {"status": "READY"}))).model_dump()
    c["public_tests"][0]["rows"] = [
        {"entity_id": "m1", "values": {"status": "SHIPPING"}},
        {"entity_id": "m1", "values": {"status": "READY"}},
    ]
    contract = TaskContract.model_validate(c)
    plan = _cases(("hc1", [_rows(3, status="READY")]))
    fitted, fplan, notes = fit_seed(contract, plan, java)
    assert _has(notes, "공개 테스트 pt1 가 쓰는 행", "status=SHIPPING")
    assert not _has(notes, "공개 테스트 pt1 가 쓰는 행", "status=READY")
    free = {v["status"]: len(ids) for _, v, ids in public_free(fitted, fplan)}
    assert free == {"READY": 3, "SHIPPING": 3}
    assert not seed_problems(fitted) and not allocate_rows(fitted, fplan).problems
    names = row_use_consts(fitted, fitted.public_tests[0].rows[0])
    assert names == ["PUBLIC_ORDER_SHIPPING"]


def test_fit_seed_row_uses_on_a_seed_smaller_than_reserved_rows() -> None:
    """리뷰 2026-11-13: 시드가 작은 id 몫(1~10)보다 작으면 덧붙인 그룹은 그 위까지 덮고, 공개 몫은 작은 id 밖에서 나온다."""
    java = load_template(JAVA).manifest
    c = _with_seed(_seed(4, (1, 4, {"status": "READY"}))).model_dump()
    c["public_tests"][0]["rows"] = [{"entity_id": "m1", "values": {"status": "SHIPPING"}}]
    fitted, fplan, _ = fit_seed(
        TaskContract.model_validate(c), _cases(("hc1", [_rows(2, status="READY")])), java
    )
    groups = [(g.first, g.last, g.values["status"]) for g in fitted.seed[0].groups]
    assert [g[2] for g in groups].count("SHIPPING") == 1
    assert all(a[1] + 1 == b[0] for a, b in pairwise(groups))  # 빈틈·겹침 없음
    free = {v["status"]: ids for _, v, ids in public_free(fitted, fplan)}
    assert len(free["SHIPPING"]) == 3 and min(free["SHIPPING"]) > RESERVED_ROWS
    assert not seed_problems(fitted) and not allocate_rows(fitted, fplan).problems


def test_row_use_problems_point_at_unseedable_rows() -> None:
    c = _with_seed(_seed(20, (1, 20, {"status": "READY"}))).model_dump()
    c["public_tests"][0]["rows"] = [
        {"entity_id": "m9", "values": {"status": "READY"}},
        {"entity_id": "m1", "values": {"color": "RED"}},
    ]
    c["public_tests"][1]["rows"] = [{"entity_id": "m2", "values": {"note": "x"}}]
    problems = row_use_problems(TaskContract.model_validate(c))
    assert _has(problems, "pt1", "없는 엔티티 m9")
    assert _has(problems, "pt1", "없는 칸", "color")
    assert _has(problems, "pt2", "처음에 비어", "요청으로 만드는 데이터")


def test_fit_seed_leaves_good_answers_and_unseeded_entities_alone() -> None:
    java = load_template(JAVA).manifest
    contract = _with_seed(TWO_GROUPS)
    plan = _cases(("hc1", [_rows(96, burst=True, status="READY")]))
    assert fit_seed(contract, plan, java) == (contract, plan, [])
    # 시드 규칙이 없는 엔티티는 무엇으로 채울지 몰라 고치지 않는다 → 검사 문제로 남음
    plan = _cases(("hc1", [_rows(2, entity="m2")]))
    fitted, fplan, notes = fit_seed(contract, plan, java)
    assert not notes and _has(
        allocate_rows(fitted, fplan).problems, "OrderHistory 의 시드 행이 없음"
    )


def test_zero_row_data_is_dropped_not_rejected() -> None:
    """2026-11-07 v6 스모크: LLM 이 빈 기록 엔티티를 data rows 0 으로 적어 응답 전체가 스키마에서 거부됐다 → 받고 코드가 뺀다."""
    java = load_template(JAVA).manifest
    contract = _with_seed(TWO_GROUPS, {"entity_id": "m2", "rows": 0})
    plan = _cases(("hc1", [_rows(96, burst=True, status="READY"), _rows(0, entity="m2")]))
    assert not allocate_rows(contract, plan).problems
    fitted, fplan, notes = fit_seed(contract, plan, java)
    assert _has(notes, "hc1", "rows 0 을 뺌") and [d.entity_id for d in fplan.cases[0].data] == [
        "m1"
    ]


def _scoped(groups: list[tuple[int, int, dict[str, Any]]], rows: int) -> TaskContract:
    """한 매장(shopId=7) 주문을 경로 /shops/{shopId}/orders/{orderNo} 로 찾는 계약 — orderNo 는 글자, 시드가 정하지 않음."""
    c = _contract()
    c["entities"][0]["fields"] += [
        {"name": "shopId", "type": "integer", "refs": ["r1"]},
        {"name": "orderNo", "type": "string", "refs": ["r1"]},
    ]
    c["endpoints"][0]["path"] = "/shops/{shopId}/orders/{orderNo}/status"
    c["seed"] = [_seed(rows, *groups)]
    return TaskContract.model_validate(c)


def test_appended_groups_keep_the_scope_every_group_fixes() -> None:
    # 2026-11-09 v16-be1: 덧붙인 그룹에 guildId 가 빠져 그 행들의 guild_id 가 행 번호 → 숨은 테스트 요청이 모두 대상 없음
    java = load_template(JAVA).manifest.model_copy(update={"public_helper": None})
    contract = _scoped([(1, 20, {"shopId": 7, "status": "READY"})], 20)
    plan = _cases(("hc1", [_rows(30, status="READY")]))
    fitted, fplan, _ = fit_seed(contract, plan, java)
    assert all(g.values.get("shopId") == 7 for g in fitted.seed[0].groups)
    assert not seed_problems(fitted)
    # 모델이 쓴 그룹 사이에 범위 값이 빈 그룹이 있으면 재요청
    gap = _scoped([(1, 10, {"shopId": 7, "status": "READY"}), (11, 20, {"status": "READY"})], 20)
    assert _has(seed_problems(gap), "경로 변수 shopId", "11~20 는 비움")


def test_helpers_give_path_key_values_and_hand_built_keys_are_flagged() -> None:
    # 2026-11-09 v16-be2: 시드 'memberId-191', 테스트 "member-" + HC2_FIRST → 모든 구현에서 대상 없음
    from task_spec.schemas import GeneratedFile
    from task_spec.starter.hidden_helpers import key_fields, render_contract_case
    from task_spec.starter.static_check import _hand_built_keys

    java = load_template(JAVA).manifest
    contract = _scoped([(1, 200, {"shopId": 7, "status": "READY"})], 200)
    plan = _cases(("hc1", [_rows(20, status="READY")]))
    assert key_fields(contract) == [("orderNo", "orderNo-", False)]
    helper = render_contract_case(contract, plan, java)
    assert helper is not None and "protected static String orderNoOf(long row)" in helper.content
    assert '"orderNo-" + row' in helper.content  # seed_sql.default_value 와 같은 형식
    from task_spec.starter.hidden_helpers import api_functions

    fn = api_functions(contract)[0][1]

    def flagged(body: str) -> bool:
        return bool(
            _hand_built_keys(
                [GeneratedFile(path="src/test/java/X.java", content=body)], contract, "숨은 테스트"
            )
        )

    # #178: 모양("order-" +)이 아니라 자리(경로 변수 orderNo 인자)로 본다 — 붙임표 없는 이름·String.format·변수 거쳐 넘김도 잡는다
    assert flagged(f'String no = "order-" + HC1_FIRST;\n{fn}(7, no);')
    assert flagged(f'{fn}(7, "order" + HC1_FIRST);')
    assert flagged(f'{fn}(7, String.format("order-%d", HC1_FIRST));')
    assert not flagged(f"{fn}(7, orderNoOf(HC1_FIRST));")
    assert not flagged(f"String no = orderNoOf(HC1_FIRST);\n{fn}(7, no);")
    # 숫자 경로 변수(shopId) 자리의 글자는 키 값 짐작이 아니다 · 호출에 넘기지 않은 글자는 보지 않는다
    assert not flagged(f'{fn}("7", orderNoOf(HC1_FIRST));')
    assert not flagged('String label = "order-" + HC1_FIRST;')


def test_list_endpoint_with_a_shared_path_key_is_fine() -> None:
    """목록 API 는 같은 경로 변수 값이 여러 행에 있는 것이 정상 — 2026-11-11 N+1 알파: GET /projects/{projectId}/board/posts(m1[]) 를
    '한 행을 찾는 API' 로 짚어 계약을 다시 요청했다. 한 행을 돌려주는 API 는 지금처럼 잡는다."""
    c = _contract()
    c["entities"][0]["fields"] += [{"name": "shopId", "type": "integer", "refs": ["r1"]}]
    c["endpoints"][1]["path"] = "/shops/{shopId}/orders"
    c["endpoints"][0]["path"] = "/orders/{id}/status"
    c["seed"] = [_seed(200, (1, 200, {"status": "READY", "shopId": 7}))]
    ok = next(r for r in c["endpoints"][1]["responses"] if 200 <= r["status"] < 300)
    ok.update({"returns": "m1[]", "body": []})
    assert not _has(seed_problems(TaskContract.model_validate(c)), "한 행을 찾는")
    ok.update({"returns": None, "body": [{"name": "items", "type": "m1[]", "refs": ["r1"]}]})
    assert not _has(seed_problems(TaskContract.model_validate(c)), "한 행을 찾는")  # 감싼 목록
    ok.update({"returns": "m1", "body": []})
    assert _has(seed_problems(TaskContract.model_validate(c)), "한 행을 찾는", "shopId")
    # GET 인데 응답을 한 건으로 적었으면 — 안내 하나에 두 고침 (목록이면 returns "m1[]" 로, 한 행 조회면 값을 뺀다)
    one = seed_problems(TaskContract.model_validate(c))
    assert _has(one, 'returns "m1[]" (Order 목록)', "shopId 는 values 에서 뺀다")
    # 다른 것의 목록(멤버 한 행을 찾아 감사 기록을 돌려줌)도 경로로 한 행을 가리키므로 계속 잡는다 — 이 검사가 생긴 경우 (alpha-cap1·trap2).
    # 판정·안내는 같다 (#180: 사례마다 안내 갈래를 두던 것을 하나로)
    ok.update({"returns": None, "body": [{"name": "content", "type": "array", "refs": ["r1"]}]})
    got = seed_problems(TaskContract.model_validate(c))
    assert _has(got, "Order 한 행을 가리키는데", "shopId") and [
        m for m in got if "한 행을 가리키는데" in m
    ] == [m for m in one if "한 행을 가리키는데" in m]


def test_list_of_response_shape_with_a_shared_path_key_is_fine() -> None:
    """응답 모양(조인한 칸이 붙은 목록 항목)의 목록도 목록 API — 2026-11-13 송금 내역(ct-4fb4cffa89b4-b1): plan_contract v24 대로
    GET /shops/{shopId}/orders → v1[] 로 적은 계약을 '한 행을 찾는 API' 로 짚어 다시 요청해도 못 고쳤다. 한 건 응답은 지금처럼 잡는다."""
    c = _contract()
    c["entities"][0]["fields"] += [{"name": "shopId", "type": "integer", "refs": ["r1"]}]
    c["endpoints"][1]["path"] = "/shops/{shopId}/orders"
    c["endpoints"][0]["path"] = "/orders/{id}/status"
    c["seed"] = [_seed(200, (1, 200, {"status": "READY", "shopId": 7}))]
    c["response_shapes"] = [
        {
            "shape_id": "v1",
            "name": "OrderSummary",
            "fields": [
                {"name": "id", "type": "integer", "refs": ["r1"]},
                {"name": "shopName", "type": "string", "refs": ["r1"], "constraints": "Shop.name"},
            ],
        }
    ]
    ok = next(r for r in c["endpoints"][1]["responses"] if 200 <= r["status"] < 300)
    ok.update({"returns": "v1[]", "body": []})
    assert not _has(seed_problems(TaskContract.model_validate(c)), "한 행을 찾는")
    ok.update({"returns": None, "body": [{"name": "items", "type": "v1[]", "refs": ["r1"]}]})
    assert not _has(seed_problems(TaskContract.model_validate(c)), "한 행을 찾는")  # 감싼 목록
    ok.update({"returns": "v1", "body": []})  # 한 건 — 경로 변수로 한 행을 찾는다
    assert _has(seed_problems(TaskContract.model_validate(c)), "한 행을 찾는", "shopId")


def test_one_answer_to_what_a_response_lists() -> None:
    """#179: '성공 응답이 무엇의 목록인가'를 세 함수가 따로 답했다 — response_list_target 하나로."""
    from task_spec.starter.seed_rows import response_list_target

    c = _contract()
    ent = c["entities"][0]
    c["response_shapes"] = [{"shape_id": "v1", "name": "OrderRow", "fields": ent["fields"][:1]}]
    e = c["endpoints"][1]
    e["responses"][0] |= {"returns": f"{ent['entity_id']}[]", "body": []}
    contract = TaskContract.model_validate(c)
    assert response_list_target(contract.endpoints[1], contract) == ("entity", ent["entity_id"])
    e["responses"][0] |= {"returns": "v1[]"}
    contract = TaskContract.model_validate(c)
    assert response_list_target(contract.endpoints[1], contract) == ("shape", "v1")
    e["responses"][0] |= {
        "returns": None,
        "body": [{"name": "tags", "type": "string[]", "refs": ["r1"]}],
    }
    contract = TaskContract.model_validate(c)
    assert response_list_target(contract.endpoints[1], contract) == ("other", "string[]")
    e["responses"][0] |= {"body": [{"name": "id", "type": "integer", "refs": ["r1"]}]}
    contract = TaskContract.model_validate(c)
    assert response_list_target(contract.endpoints[1], contract) is None


# ── 고유 칸 (#308 버그 A) · 동시 요청 사유의 버스트 행 (#308 버그 B) ─────────────


def _login_contract(**login_row: Any) -> TaskContract:
    """로그인(issued) 엔티티 Member(m3 — username·password·nickname)에 공개 테스트 pt1 이 로그인할 행을 쓰는 계약 (가상).
    운영 세션 과제(#308)의 모양 — 공개 테스트가 쓰는 로그인 행을 코드가 시드 끝에 덧붙인다."""
    c = fit_auth(TaskContract.model_validate(_session()))[0].model_dump()
    c["public_tests"][0]["rows"] = [{"entity_id": "m3", "values": login_row}]
    return TaskContract.model_validate(c)


def test_unique_fields_are_marked_ones_and_login_identifiers() -> None:
    """고유 칸 = 제약 글이 고유를 말하는 칸 + 로그인 엔티티의 식별 칸. 비밀번호는 여러 행이 같아도 된다."""
    c = _login_contract(username="user-1", password="pass-1")
    assert unique_fields(c, "m3") == {"username"}
    assert unique_fields(c, "m1") == frozenset()
    assert public_per_group(c, "m3", {"username": "user-1", "password": "pass-1"}) == 1
    assert public_per_group(c, "m3", {"password": "pass-1"}) == PUBLIC_PER_GROUP
    d = c.model_dump()
    fields = d["entities"][0]["fields"]
    fields.append(
        {"name": "orderNo", "type": "string", "constraints": "주문 번호 — 고유", "refs": ["r1"]}
    )
    fields.append({"name": "memo", "type": "string", "constraints": "중복 허용", "refs": ["r1"]})
    fields.append({"name": "code", "type": "string", "constraints": "UNIQUE", "refs": ["r1"]})
    assert unique_fields(TaskContract.model_validate(d), "m1") == {"orderNo", "code"}


def test_public_rows_for_a_login_are_one_row() -> None:
    """#308 알파 1·3: 공개 테스트 pt1 이 로그인할 행(username user-1·password pass-1)을 3행 덧붙여, 'username 으로 찾기' 구현이 500 으로
    떨어지고(알파 1) USERS(USERNAME) 고유 인덱스 위반으로 시작 코드가 기동하지 못했다(알파 3). 고유 칸이 든 처음 값은 1행만 — 작은 id
    쪽이면 그 행이 공개 몫이다."""
    java = load_template(JAVA).manifest
    contract = _login_contract(username="user-1", password="pass-1")
    fitted, fplan, notes = fit_seed(contract, _cases(("hc1", [_rows(2, status="READY")])), java)
    member = next(s for s in fitted.seed if s.entity_id == "m3")
    login = [g for g in member.groups if g.values.get("username") == "user-1"]
    assert [(g.first, g.last) for g in login] == [(7, 7)]  # fit_auth 의 로그인 행 6 다음 한 행
    assert not seed_problems(fitted)
    assert _has(notes, "공개 테스트 pt1", "고유 칸(username)이라 1행")
    free = {(e, v.get("username")): ids for e, v, ids in public_free(fitted, fplan)}
    assert free[("m3", "user-1")] == [7]  # 작은 id(10행) 안이어도 그 행이 공개 몫
    # 공개 몫을 채우는 단계(_fit_public)도 고유 칸 그룹은 덧붙이지 않는다 — 같은 username 의 행이 둘이 되지 않는다
    names = [
        g.values["username"]
        for g in member.groups
        if "username" in g.values
        for _ in range(g.first, g.last + 1)
    ]
    assert len(names) == len(set(names))


def test_llm_groups_and_hidden_data_must_not_repeat_a_unique_value() -> None:
    """LLM 이 시드 범위나 숨은 케이스 처음 값에 고유 칸을 같은 값으로 여러 행 두어도 같은 실패가 난다 — 계약 단계에서 재요청."""
    d = _login_contract(username="user-1", password="pass-1").model_dump()
    member = next(s for s in d["seed"] if s["entity_id"] == "m3")
    member["groups"][0]["values"]["username"] = "same"  # m3 범위 1~5 가 모두 username=same
    contract = TaskContract.model_validate(d)
    assert _has(seed_problems(contract), "시드 m3 범위 1~5", "고유 칸 username", "5행")
    plan = _cases(("hc1", [_rows(3, entity="m3", username="x")]))
    problems = case_rows_problems(contract, plan, load_template(JAVA).manifest)
    assert _has(problems, "숨은 케이스 hc1 data", "고유 칸 username", "3행")


def test_peak_reasons_need_burst_rows() -> None:
    """#308 알파 1: pool_acquire_timeout 케이스 data 가 모두 burst=false 라 hc1 이 Burst.fire(1, …) 한 건이 됐고, 풀이 고갈될 수 없으니
    흔한 구현이 매 회차 통과했다. 성립 조건이 동시 요청 수(burst_rows)를 쓰는 사유는 버스트 행이 있어야 한다 — 계약 단계에서 재요청."""
    java = load_template(JAVA).manifest
    assert peak_reasons(java) == {"pool_acquire_timeout": ["concurrency"]}
    assert peak_reasons(load_template("data/python-duckdb").manifest) == {}
    contract = _with_seed(TWO_GROUPS)
    base = _plan()["cases"][0]

    def plan(reason: str, *data: dict[str, Any]) -> HiddenPlan:
        case = base | {
            "case_id": "hc1",
            "ac_id": "h1",
            "intended_reason": reason,
            "data": list(data),
        }
        harness = {"pool_size": 8, "concurrency": 32, "bursts": 3, "query_delay_ms": 20}
        return HiddenPlan.model_validate(_plan(harness=harness, cases=[case]))

    single = plan("pool_acquire_timeout", _rows(2, status="READY"))
    assert _has(
        case_rows_problems(contract, single, java),
        "hc1",
        "pool_acquire_timeout 는 동시 요청으로만 재현",
        "버스트 행이 없음",
        "concurrency",
    )
    burst = plan("pool_acquire_timeout", _rows(96, burst=True, status="READY"))
    assert not case_rows_problems(contract, burst, java)
    assert not case_rows_problems(contract, plan("request_timeout", _rows(2, status="READY")), java)
