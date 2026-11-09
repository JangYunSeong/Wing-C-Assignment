"""계약 시드 검사와 숨은 케이스 행 배정 (starter/seed_rows.py) — 가상 데이터만."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.schemas import HiddenPlan, TaskContract
from task_spec.schemas.template import TemplateManifest
from task_spec.starter.seed_rows import (
    RESERVED_ROWS,
    allocate_rows,
    case_rows_problems,
    fit_seed,
    public_free,
    seed_markdown,
    seed_problems,
)
from task_spec.starter.templates import load_template
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
    base = _plan()["cases"][0]
    return HiddenPlan.model_validate(
        _plan(
            harness={"pool_size": 8, "concurrency": 32, "bursts": 3, "query_delay_ms": 20},
            cases=[
                base | {"case_id": cid, "ac_id": f"h{n}", "data": data}
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
    built = GeneratedFile(path="src/test/java/X.java", content='String no = "order-" + HC1_FIRST;')
    used = GeneratedFile(path="src/test/java/Y.java", content="String no = orderNoOf(HC1_FIRST);")
    assert _hand_built_keys([built], contract, "숨은 테스트") and not _hand_built_keys(
        [used], contract, "숨은 테스트"
    )
