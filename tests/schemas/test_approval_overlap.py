"""승인 결정의 겹침 낮추기·되돌리기 입력 검사 (가상 값)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.schemas import ApprovalDecision


def _d(**kw: Any) -> dict[str, Any]:
    return {
        "status": "revise",
        "reviewer": "reviewer-fake",
        "comment": "가상 사유",
        "revise_target": "verification",
        "at": "2026-11-04T09:00:00+09:00",
        **kw,
    }


def test_downgrade_and_restore_are_revise_only() -> None:
    assert ApprovalDecision.model_validate(_d(restore_overlap=["comp:a"])).restore_overlap == [
        "comp:a"
    ]
    for field in ("downgrade_overlap", "restore_overlap"):
        with pytest.raises(ValidationError, match="revise 결정에서만"):
            ApprovalDecision.model_validate(
                _d(status="approved", comment=None, **{field: ["comp:a"]})
            )
        with pytest.raises(ValidationError, match="revise_target"):
            ApprovalDecision.model_validate(_d(revise_target="common", **{field: ["comp:a"]}))


def test_ids_are_bounded_and_not_both_ways() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecision.model_validate(_d(downgrade_overlap=["x" * 121]))
    with pytest.raises(ValidationError):
        ApprovalDecision.model_validate(_d(restore_overlap=[""]))
    with pytest.raises(ValidationError, match="함께"):
        ApprovalDecision.model_validate(
            _d(downgrade_overlap=["comp:a"], restore_overlap=["comp:a"])
        )
    assert ApprovalDecision.model_validate(_d(downgrade_overlap=["x" * 120])).downgrade_overlap
