"""계약의 응답 모양(response_shapes, 테이블 아님)과 딸린 정보 검사(related_entities) — 2026-11-13. 가상 데이터만, LLM 없음.

근거: 2026-11-12 행마다 조회 알파 — 계약에 '응답에만 있는 모양'을 적을 자리가 없어, 고객 이름·상품 수를 주문 테이블 칸·시드 값으로 복사하거나
(칸만 읽으면 숨은 요구가 사라짐) 응답 모양을 빈 테이블(order_list_item)로 만들었다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.schemas import ContractResponse, GeneratedFile, ResponseShape, TaskContract
from task_spec.spec.incidents import incident_by_id, load_incidents
from task_spec.starter.contract import contract_fields
from task_spec.starter.hidden_helpers import render_contract_case
from task_spec.starter.render import contract_markdown
from task_spec.starter.static_check import Overlay, static_check
from task_spec.starter.ts_helpers import app_type_names, render_ts_contract
from tests.starter.test_contract import _check, _contract, _has
from tests.starter.test_frontend_template import FE
from tests.starter.test_frontend_template import _contract as _ui_contract
from tests.starter.test_static_check import JAVA, PLAN, _task

ROOT = Path(__file__).resolve().parents[2]
_INC = incident_by_id(
    load_incidents(ROOT / "incidents" / "public_v1"), "pub:sw.backend:list-query-per-row"
)
assert _INC is not None and _INC.feature is not None
NPLUS1 = _INC.feature


def _f(name: str, typ: str = "string", constraints: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "type": typ,
        "refs": ["r1"],
        **({"constraints": constraints} if constraints else {}),
    }


def _list_contract(*, copied: bool = False, shape: bool = True) -> dict[str, Any]:
    """주문(→ 고객 외래 키) · 고객 · 주문 상품(→ 주문). shape 면 목록 응답이 응답 모양 v1 OrderSummary(고객 이름·상품 수),
    copied 면 고객 이름·상품 수를 주문 칸·시드 값으로 복사 (2026-11-12 첫 알파 계약 모양)."""
    order = [_f("id", "integer"), _f("customerId", "integer"), _f("createdAt", "datetime")]
    values: dict[str, Any] = {"customerId": 1}
    if copied:
        order += [_f("customerName"), _f("itemCount", "integer")]
        values |= {"customerName": "가상고객", "itemCount": 2}
    c = _contract()
    c["endpoints"][1] |= {"path": "/orders", "summary": "주문 목록"}
    c["endpoints"][1]["responses"][0] = {
        "status": 200,
        "description": "목록",
        "returns": "v1[]" if shape else "m1[]",
    }
    c["entities"] = [
        {
            "entity_id": "m1",
            "name": "Order",
            "fields": order,
            "foreign_keys": ["customerId → Customer"],
        },
        {"entity_id": "m2", "name": "Customer", "fields": [_f("id", "integer"), _f("name")]},
        {
            "entity_id": "m3",
            "name": "OrderItem",
            "fields": [_f("id", "integer"), _f("orderId", "integer")],
            "foreign_keys": ["orderId → Order"],
        },
    ]
    if shape:
        c["response_shapes"] = [
            {
                "shape_id": "v1",
                "name": "OrderSummary",
                "fields": [
                    _f("id", "integer"),
                    _f("customerName", constraints="Customer.name — 주문의 customerId 로"),
                    _f("itemCount", "integer", "그 주문의 OrderItem 수"),
                ],
            }
        ]
    c["seed"] = [
        {"entity_id": "m1", "rows": 20, "groups": [{"first": 1, "last": 20, "values": values}]},
        {
            "entity_id": "m2",
            "rows": 2,
            "groups": [{"first": 1, "last": 2, "values": {"name": "가상고객"}}],
        },
        {"entity_id": "m3", "rows": 40, "groups": [{"first": 1, "last": 40, "values": {}}]},
    ]
    c["covers"] = {"r1": ["e1", "m1", "m2", "m3"], "r2": ["e2", "m1"]}
    return c


def test_shapes_are_not_tables_and_use_their_own_ids() -> None:
    c = TaskContract.model_validate(_list_contract())
    assert [s.shape_id for s in c.response_shapes] == ["v1"]
    assert [m.entity_id for m in c.data_shapes()] == ["m1", "m2", "m3", "v1"]
    assert ContractResponse(status=200, description="x", returns="v1[]").returns == "v1[]"
    with pytest.raises(ValidationError):  # s 는 화면 id
        ResponseShape(shape_id="s1", name="X", fields=[_f("a")])


def test_contract_checks_returns_names_and_unused_shapes() -> None:
    clean = _check(_list_contract()).problems
    assert not _has(clean, "응답 returns") and not _has(clean, "응답 모양")
    bad = _list_contract()
    bad["endpoints"][1]["responses"][0]["returns"] = "v2[]"
    assert _has(
        _check(bad).problems, "응답 returns 가 계약에 없는 엔티티·응답 모양을 가리킴", "v2[]"
    )
    clash = _list_contract()
    clash["response_shapes"][0]["name"] = "Order"
    assert _has(_check(clash).problems, "응답 모양 이름이 테이블 엔티티와 같음", "Order")
    unused = _list_contract()
    unused["endpoints"][1]["responses"][0]["returns"] = "m1[]"
    assert _has(_check(unused).problems, "어느 응답도 가리키지 않는 응답 모양", "v1 OrderSummary")
    # covers 에 응답 모양 id 를 넣어도 '계약에 없는 id' 가 아니다
    covered = _list_contract()
    covered["covers"]["r2"].append("v1")
    assert not _has(_check(covered).problems, "계약에 없는 id")


def test_shape_fields_are_public_names_and_readme_shows_them_as_not_tables() -> None:
    c = TaskContract.model_validate(_list_contract())
    assert {f.name for _, f in contract_fields(c)} >= {"customerName", "itemCount"}
    md = contract_markdown(c, tables=JAVA.manifest.naming.table)
    assert "→ `OrderSummary` 목록" in md
    assert "### 응답 모양" in md and "테이블이 아니다" in md
    assert "#### `OrderSummary`" in md and "그 주문의 OrderItem 수" in md
    assert "OrderSummary` — 테이블" not in md  # 테이블 이름이 붙지 않는다


def test_hidden_helper_reads_shape_lists_as_lists() -> None:
    c = TaskContract.model_validate(_list_contract())
    f = render_contract_case(c, PLAN, JAVA.manifest)
    assert f is not None
    assert (
        "protected List<Map<String, Object>> getOrdersBody(HttpResponse<String> response)"
        in f.content
    )
    assert "TABLE_ORDER_SUMMARY" not in f.content  # 테이블 상수는 테이블 엔티티만


def test_starter_making_a_table_for_a_shape_is_sent_back() -> None:
    c = TaskContract.model_validate(_list_contract())
    schema = (
        "CREATE TABLE orders (id BIGINT PRIMARY KEY, customer_id BIGINT, created_at TIMESTAMP);\n"
        "CREATE TABLE customer (id BIGINT PRIMARY KEY, name VARCHAR(50));\n"
        "CREATE TABLE order_item (id BIGINT PRIMARY KEY, order_id BIGINT);\n"
        "CREATE TABLE order_summary (id BIGINT PRIMARY KEY, customer_name VARCHAR(50));\n"
    )
    o = Overlay(
        kind="starter", files=(GeneratedFile(path="src/main/resources/schema.sql", content=schema),)
    )
    from task_spec.starter.contract import SpecView

    res = static_check([o], template=JAVA, plan=PLAN, spec=SpecView.from_task(_task()), contract=c)
    assert any(
        "order_summary" in m and "계약 테이블 밖" in m for m in res.for_target("gen_starter")
    )


def test_typescript_types_include_shapes() -> None:
    c = _ui_contract().model_dump(mode="json")
    c["endpoints"][0]["responses"][0] = {"status": 200, "description": "추천", "returns": "v1[]"}
    c["response_shapes"] = [
        {"shape_id": "v1", "name": "Suggestion", "fields": [_f("name"), _f("score", "integer")]}
    ]
    contract = TaskContract.model_validate(c)
    assert "Suggestion" in app_type_names(contract)
    ts = render_ts_contract(contract, FE.manifest, "hidden", None)
    assert ts is not None and "export interface Suggestion {" in ts.content


def test_related_info_must_come_from_other_entities() -> None:
    """딸린 정보(고객 이름·상품 수)가 시드 있는 테이블 칸으로 복사되면 칸만 읽어도 된다 — 재료 related_entities 가 요구하면 재요청."""
    assert any(n.kind == "related_entities" for n in NPLUS1.contract_needs)
    copied = _check(_list_contract(copied=True), seed=NPLUS1).problems
    assert _has(
        copied, "칸으로 복사돼 있음", "Order.customerName", "Order.itemCount", "response_shapes"
    )
    clean = _check(_list_contract(), seed=NPLUS1).problems
    assert not _has(clean, "칸으로 복사돼 있음") and not _has(clean, "딸린 정보를 읽을 다른 엔티티")
    no_fk = _list_contract()
    for m in no_fk["entities"]:
        m["foreign_keys"] = []
    assert _has(_check(no_fk, seed=NPLUS1).problems, "딸린 정보를 읽을 다른 엔티티")
    # 재료에 선언이 없으면 묻지 않는다
    assert not _has(_check(_list_contract(copied=True)).problems, "칸으로 복사돼 있음")


def test_copied_fields_are_matched_by_words_not_one_spelling() -> None:
    """#180: camel 결합(customerName)·`…Count` 꼴만 봐 snake·앞에 붙은 개수 낱말(customer_name·item_count·numItems)은 지나갔다."""
    from task_spec.schemas.build import Entity
    from task_spec.starter.contract import _copied_fields

    customer = Entity.model_validate(
        {"entity_id": "m2", "name": "Customer", "fields": [_f("id", "integer"), _f("name")]}
    )
    item = Entity.model_validate(
        {
            "entity_id": "m3",
            "name": "OrderItem",
            "fields": [_f("id", "integer"), _f("qty", "integer")],
        }
    )
    names = ["customerName", "customer_name", "item_count", "numItems", "totalItems", "itemCount"]
    order = Entity.model_validate(
        {
            "entity_id": "m1",
            "name": "Order",
            "fields": [_f("id", "integer"), _f("customerId", "integer"), _f("note")]
            + [_f(n, "integer") for n in names],
            "foreign_keys": ["customerId → Customer"],
        }
    )
    assert _copied_fields(order, customer) == ["customerName", "customer_name"]
    assert _copied_fields(order, item) == ["item_count", "numItems", "totalItems", "itemCount"]
