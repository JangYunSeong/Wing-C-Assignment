"""계약 필드 타입 해석 — 앱 계약 타입과 계약 적합성 스모크 예시 값이 같은 해석을 쓴다. 가상 데이터만."""

from __future__ import annotations

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
    assert parse("(a)|(b)").kind == "union"
