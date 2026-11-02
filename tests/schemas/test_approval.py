from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from task_spec.schemas import ApprovalDecision

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def test_approved_ok() -> None:
    d = ApprovalDecision(status="approved", reviewer="reviewer-fake", at=NOW)
    assert d.status == "approved"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "auto_approved", "reviewer": "r", "at": NOW},  # 없는 상태
        {"status": "approved", "reviewer": "", "at": NOW},  # 승인자 없음
        {"status": "approved", "reviewer": "   ", "at": NOW},  # 공백 승인자
        {"status": "revise", "reviewer": "r", "at": NOW},  # revise 인데 사유 없음
        {"status": "approved", "reviewer": "r", "at": datetime(2026, 11, 2)},  # 시간대 없음
        {"status": "approved", "reviewer": "r", "at": NOW, "extra": 1},  # 모르는 필드
    ],
)
def test_invalid_decisions_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        ApprovalDecision(**kwargs)
