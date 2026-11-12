"""전달 묶음의 평가 키트 — 적재한 묶음의 응시용·채점용 설명과 같은 빌드로, EvalKit 스키마를 통과하게 만든다 (2026-11-12). 가상 데이터만."""

from __future__ import annotations

from eval.starter.split_delivery import build_kit
from task_spec.schemas.domain import AcceptedDesign
from task_spec.schemas.package import CandidateDescriptor, EvalKit, GraderDescriptor
from tests.schemas.test_package import _candidate, _grader
from tests.starter.test_kit import _task


def test_kit_from_stored_descriptors_passes_the_schema() -> None:
    """예전 키트는 build_id·grader_sha256 이 없고 숨김 수용 기준에 스키마 밖 칸(items·faults)이 있어 평가 쪽 스키마가 거부했다."""
    task = _task(
        acceptance_hidden=[
            {
                "ac_id": "h1",
                "trap_id": "t1",
                "text": "가상 숨김 기준",
                "items": ["메모A", "메모B"],
                "faults": [{"operation": "가상 전송", "item": "메모B", "times": 1, "status": 500}],
            }
        ],
        accepted_designs=[
            AcceptedDesign(
                design_id="d1", summary="가상 설계", conditions="가상 조건", trap_ids=["t1"]
            )
        ],
        review_flags=["명세 표시"],
    )
    mock = {"mock": True, "approval": "none"}  # 지금 묶음은 목 판 — 승인 전
    cand = CandidateDescriptor.model_validate(_candidate(**mock))
    grad = GraderDescriptor.model_validate(_grader(**mock))
    t = {
        "grader": {
            "cases": [{"id": "hc1", "kind": "hidden", "trap_id": "t1", "intended_reason": "gap"}]
        },
        "verification": {
            "starter-only": {
                "summary": {
                    "hidden_passed": 0,
                    "hidden_total": 1,
                    "public_passed": 0,
                    "public_total": 1,
                },
                "cases": [{"id": "hc1", "kind": "hidden", "passed": False, "reason": "gap"}],
            }
        },
    }
    kit = build_kit(
        t,
        task.model_dump(mode="json"),
        cand,
        grad,
        {"gap": "가상 사유"},
        ["빌드 표시", "명세 표시"],
    )
    EvalKit.model_validate(kit.model_dump(mode="json"))  # 쓴 것을 다시 읽어도 통과
    assert kit.build_id == cand.build_id and kit.grader_sha256 == grad.grader_sha256
    assert kit.images.candidate == cand.image.image_id and kit.images.grader == grad.image.image_id
    dumped = kit.model_dump(mode="json")
    assert set(dumped["acceptance_hidden"][0]) == {
        "ac_id",
        "trap_id",
        "text",
    }  # items·faults 는 채점 이미지 안
    assert kit.review_flags == [
        "명세 표시",
        "빌드 표시",
    ]  # 명세 표시 + 빌드 단계 표시 (같은 글은 한 번)
    assert kit.stage1 is not None and kit.stage2 is not None
    assert kit.verification["starter-only"].cases[0].reason == "gap"


def test_approved_kit_needs_reviewer_decisions_for_flags() -> None:
    """승인된(목 아닌) 묶음은 검토 표시마다 승인자 판단이 있어야 한다 — 판단 없이 키트를 만들면 쓰기 전에 멈춘다 (EvalKit 규칙)."""
    import pytest
    from pydantic import ValidationError

    cand = CandidateDescriptor.model_validate(_candidate())
    grad = GraderDescriptor.model_validate(_grader())
    task = _task(
        acceptance_hidden=[{"ac_id": "h1", "trap_id": "t1", "text": "가상 숨김 기준"}],
        accepted_designs=[
            AcceptedDesign(
                design_id="d1", summary="가상 설계", conditions="가상 조건", trap_ids=["t1"]
            )
        ],
    )
    t = {
        "grader": {
            "cases": [{"id": "hc1", "kind": "hidden", "trap_id": "t1", "intended_reason": "gap"}]
        },
        "verification": {
            "starter-only": {
                "summary": {
                    "hidden_passed": 0,
                    "hidden_total": 1,
                    "public_passed": 0,
                    "public_total": 1,
                }
            }
        },
    }
    with pytest.raises(ValidationError, match="승인자 판단"):
        build_kit(t, task.model_dump(mode="json"), cand, grad, {"gap": "가상 사유"}, ["빌드 표시"])
