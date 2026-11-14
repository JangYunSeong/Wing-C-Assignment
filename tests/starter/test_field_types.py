"""계약 필드 타입 해석 — 앱 계약 타입과 계약 적합성 스모크 예시 값이 같은 해석을 쓴다. 가상 데이터만."""

from __future__ import annotations

import re

import pytest

from task_spec.schemas.build import ContractField, TaskContract
from task_spec.starter.conformance import _Examples
from task_spec.starter.field_types import parse
from task_spec.starter.ts_helpers import ts_type
from tests.starter.test_contract import _contract

ENT = {"Sensor": "Sensor", "m2": "Sensor"}


@pytest.mark.parametrize(
    ("t", "ts"),
    [
        ("number[]", "number[]"),
        ("(number|null)[]", "(number | null)[]"),  # 2026-11-11 재측정 토큰 갱신
        ("(Sensor|null)[]", "(Sensor | null)[]"),
        ("array<Sensor>", "Sensor[]"),
        ("list<m2>", "Sensor[]"),
        ("number | null", "number | null"),
        ("enum(A|B)", '"A" | "B"'),
        ("'A' | 'B'", '"A" | "B"'),
        ("LocalDateTime", "string"),
        ("object", "Record<string, unknown>"),
        ("Unknown$Thing!", "unknown"),
        ("string | string[]", "string | string[]"),  # 맨 바깥 | 를 [] 보다 먼저
        ("Sensor | number[]", "Sensor | number[]"),
        ("str", "string"),
        ("phone", "unknown"),  # 엔티티가 아닌 이름
    ],
)
def test_one_parse_for_app_types(t: str, ts: str) -> None:
    assert ts_type(t, ENT) == ts


def test_smoke_example_follows_the_same_parse() -> None:
    """스모크가 `(number|null)[]` 칸에 글자를 넣어 계약대로 .map 한 구현이 모두 깨졌다 — 같은 해석이면 배열, object 는 {}."""
    ex = _Examples(TaskContract.model_validate(_contract()))

    def value(t: str) -> object:
        return ex.value(ContractField(name="x", type=t, required=True, refs=["r1"]))

    assert value("(number|null)[]") == [1.5]
    assert value("number | null") == 1.5
    assert value("object") == {}
    assert value("array") == [] and value("Nope[]") == []
    # 엔티티가 아닌 이름은 참조가 아니다 — {} 가 아니라 글자 (이전 해석과 같게)
    assert value("phone") == "가상 x" and value("Money") == "가상 x"
    assert value("str") == "가상 x"
    assert value("string | string[]") == "가상 x"
    assert parse("enum(A|B)").kind == "enum" and parse("array<A|B>").kind == "list"
    assert parse("(a)|(b)").kind == "union"


def test_entity_with_spaces_is_found_the_same_way() -> None:
    """엔티티 이름 'Daily Sales' 를 필드 타입 DailySales 로 쓰면 앱 타입은 엔티티, 스모크 예시는 글자로 갈렸다 — 같은 기준으로 찾는다."""
    c = _contract()
    c["entities"][0]["name"] = "Daily Sales"
    contract = TaskContract.model_validate(c)
    ents = {e.name.replace(" ", ""): e.name.replace(" ", "") for e in contract.entities}
    assert ts_type("DailySales", ents) == "DailySales"
    f = ContractField(name="x", type="DailySales", required=True, refs=["r1"])
    assert isinstance(_Examples(contract).value(f), dict)


# ── #172: 시드 SQL·권한 규칙·시드 행 검사·Python 도우미·화면 칸·정적 검사가 같은 해석을 쓴다 ──────────

_SEED_KIND = {"integer": int, "number": int, "boolean": bool}


@pytest.mark.parametrize(
    ("t", "kind"),
    [
        ("date-time", "datetime"),  # 시드 SQL 은 날짜로 읽었다
        ("localdate", "date"),  # 판정 없음 → 'x-3' 이었다
        ("smallint", "integer"),
        ("bigdecimal", "number"),
        ("datetime", "datetime"),
        ("instant", "datetime"),
        ("localtime", "time"),
        ("int32", "integer"),
        ("real", "number"),
        ("decimal(10,2)", "number"),  # 크기가 붙은 SQL 형식
        ("varchar(255)", "string"),
        ("long[]", "list"),  # 시드 SQL 은 앞머리 long 으로 정수 3 을 넣었다
        ("array<long>", "list"),
        ("enum('A'|'B')", "enum"),
        ("enum (A|B)", "enum"),
        ("'A' | 'B'", "enum"),
        ("boolean", "boolean"),
    ],
)
def test_seed_sql_reads_types_like_field_types(t: str, kind: str) -> None:
    from task_spec.starter.seed_sql import default_value, time_kind

    assert parse(t).kind == kind
    f = ContractField(name="x", type=t, refs=["r1"])
    got = default_value(f, 3)
    if kind in ("datetime", "date", "time"):
        assert time_kind(f) == kind
        shape = {
            "datetime": r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d",
            "date": r"\d{4}-\d\d-\d\d",
            "time": r"\d\d:\d\d:\d\d",
        }
        assert re.fullmatch(shape[kind], str(got)), got
    elif kind == "enum":
        assert got == "A"  # 따옴표 없이
    elif kind in _SEED_KIND:
        assert type(got) is _SEED_KIND[kind] and time_kind(f) is None
    else:
        assert got == "x-3" and time_kind(f) is None


@pytest.mark.parametrize(
    ("t", "py"),
    [
        ("instant", "str"),  # 자체 형식표는 Any 로 읽었다
        ("localdatetime", "str"),
        ("numeric", "float"),
        ("smallint", "int"),
        ("enum('A'|'B')", "Literal['A', 'B']"),  # 따옴표째 값이었다
        ("'A' | 'B'", "Literal['A', 'B']"),
        ("long[]", "list[int]"),
        ("array<long>", "list[int]"),
        ("string | null", "str | None"),
        ("Order[]", "list[Order]"),
        ("phone", "Any"),
    ],
)
def test_python_helper_types_follow_field_types(t: str, py: str) -> None:
    from task_spec.starter.py_helpers import py_type

    assert py_type(t, {"Order": "Order"}) == py


@pytest.mark.parametrize(
    "role_type", ["enum('ADMIN'|'CLERK')", "enum (ADMIN|CLERK)", "'ADMIN' | 'CLERK'"]
)
def test_auth_rule_and_seed_checks_read_enum_values_without_quotes(role_type: str) -> None:
    """권한 규칙은 `enum('A'|'B')` 를 따옴표째, `enum (A|B)`·`'A' | 'B'` 는 못 읽어 거절용 요청자 행이 틀어지거나
    시드 검사가 정상 값을 '허용 값 밖'으로 재요청했다 (#172 팩트체크)."""
    from task_spec.starter.auth_rule import auth_problems, fit_auth
    from task_spec.starter.seed_rows import seed_problems
    from tests.starter.test_auth_rule import _shop

    c = _shop()
    c["entities"][-1]["fields"][3]["type"] = role_type
    fitted, _ = fit_auth(TaskContract.model_validate(c))
    staff = next(s for s in fitted.seed if s.entity_id == "m3")
    assert [g.values["role"] for g in staff.groups[-2:]] == ["ADMIN", "CLERK"]
    assert not seed_problems(fitted) and not auth_problems(fitted)


def test_form_fields_and_raw_number_check_use_the_same_parse() -> None:
    from task_spec.schemas.build import GeneratedFile, UiElement
    from task_spec.starter.contract import _field_element
    from task_spec.starter.static_check import _raw_number_asserts

    form = UiElement(test_id="order-form", role="form", description="주문 폼", refs=["r1"])
    pick = _field_element(form, ContractField(name="grade", type="enum('A'|'B')", refs=["r1"]))
    assert (pick.role, pick.options) == ("combobox", ["A", "B"])  # 따옴표째 선택지였다
    num = _field_element(form, ContractField(name="qty", type="bigint", refs=["r1"]))
    assert num.role == "input"  # 글자 칸이었다
    c = _contract()
    fields = c["entities"][0]["fields"]
    fields += [
        {"name": "qty", "type": "smallint", "refs": ["r1"]},  # 정수인데 빠졌다
        {"name": "tags", "type": "long[]", "refs": ["r1"]},  # 목록인데 들어갔다
    ]
    pattern = r'assertThat\(\s*\w+(?:\.\w+\([^()"]*\))*\.get\(\s*"(\w+)"\s*\)\s*\)'
    case = GeneratedFile(
        path="src/test/java/X.java",
        content='assertThat(row.get("qty")).isEqualTo(3L);\nassertThat(row.get("tags")).hasSize(2);\n',
    )
    [msg] = _raw_number_asserts([case], [pattern], TaskContract.model_validate(c))
    assert '"qty"' in msg and '"tags"' not in msg


def test_java_helper_reads_the_body_of_a_201_only_endpoint() -> None:
    """Java 도우미만 200 을 성공 응답으로 봐서 201 만 선언한 엔드포인트에 본문 함수가 없었다 (#172)."""
    from task_spec.schemas import HiddenPlan
    from task_spec.starter.hidden_helpers import render_contract_case
    from tests.starter.test_render import _contract_with_list
    from tests.starter.test_static_check import JAVA, PLAN

    c = _contract_with_list().model_dump()
    [ok] = [r for r in c["endpoints"][1]["responses"] if r["status"] == 200]
    ok["status"] = 201
    f = render_contract_case(
        TaskContract.model_validate(c), HiddenPlan.model_validate(PLAN.model_dump()), JAVA.manifest
    )
    assert f is not None and "getOrdersIdHistoryBody(HttpResponse<String> response)" in f.content
