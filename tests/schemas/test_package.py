"""전달 묶음 파일 형식 (candidate.json · grader.json · kit.json) — 가상 값만."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.schemas.package import CandidateDescriptor, EvalKit, GraderDescriptor, flag_id

CID = "ct-0123456789ab"
BID = f"{CID}-b1"
H = "a" * 64
IMG = "sha256:" + "b" * 64


def _head(**over: Any) -> dict[str, Any]:
    return {
        "mock": False,
        "approval": "approved",
        "common_id": CID,
        "revision": 2,
        "build_id": BID,
        "template_ref": "backend/java-spring-boot3-jpa@v1",
        "ontology_version": "onto-v2026.11.01",
        "tree_sha256": H,
        "title": "가상 과제",
        **over,
    }


def _image(role: str) -> dict[str, Any]:
    return {
        "file": f"{BID}-{role}.tar.zst",
        "sha256": H,
        "size_bytes": 10,
        "image_id": IMG,
        "name": f"localhost/task/{BID}-{role}",
    }


def _candidate(**over: Any) -> dict[str, Any]:
    return {
        **_head(**over),
        "role": "candidate",
        "image": _image("candidate"),
        "load": f"podman load -i {BID}-candidate.tar.zst",
        "workspace": "/workspace",
        "run": f"podman run -it --name <응시자> localhost/task/{BID}-candidate bash",
        "commands": {"test": "./gradlew --offline test"},
    }


def _grader(**over: Any) -> dict[str, Any]:
    img = f"localhost/task/{BID}-grader"
    return {
        **_head(),
        "role": "grader",
        "image": _image("grader"),
        "load": f"podman load -i {BID}-grader.tar.zst",
        "grader_sha256": H,
        "grade": f"podman run --rm --network none -v <제출물>:/submission:ro,z -v <결과>:/out:z {img} grade",
        "grade_without_selinux_relabel": (
            f"podman run --rm --network none --security-opt label=disable "
            f"-v <제출물>:/submission:ro -v <결과>:/out {img} grade"
        ),
        "timeout_s": 1200,
        "result": {
            "fields": ["common_id", "revision", "setup", "cases", "summary"],
            "case_fields": [
                "id",
                "trap_id",
                "kind",
                "passed",
                "reason",
                "intended_reason",
                "message",
            ],
        },
        "case_ids": ["hc1", "pt1"],
        "starter_expected": {
            "hidden_passed": 0,
            "hidden_total": 1,
            "public_passed": 0,
            "public_total": 1,
        },
        **over,
    }


def _kit(**over: Any) -> dict[str, Any]:
    return {
        **_head(),
        "role": "eval_kit",
        "images": {"candidate": IMG, "grader": IMG},
        "grader_sha256": H,
        "cases": [
            {
                "id": "hc1",
                "trap_id": "t1",
                "intended_reason": "fake_reason",
                "reason_description": "가상 사유 설명",
                "hidden_requirement": "가상 숨은 요구",
                "competency_ids": ["comp:a"],
            }
        ],
        "acceptance_hidden": [{"ac_id": "h1", "trap_id": "t1", "text": "가상 수용 기준"}],
        "accepted_designs": [
            {
                "design_id": "d1",
                "summary": "가상 설계",
                "conditions": "가상 조건",
                "trap_ids": ["t1"],
            }
        ],
        "target_competencies": [{"competency_id": "comp:a", "label": "가상 역량"}],
        "verification": {
            "starter-only": {
                "hidden_passed": 0,
                "hidden_total": 1,
                "public_passed": 0,
                "public_total": 1,
            }
        },
        "review_flags": ["가상 표시"],
        "review_decisions": [
            {
                "flag_id": flag_id("가상 표시"),
                "flag": "가상 표시",
                "verdict": "confirmed",
                "reviewer": "reviewer-fake",
            }
        ],
        **over,
    }


def test_valid_descriptors() -> None:
    CandidateDescriptor.model_validate(_candidate())
    GraderDescriptor.model_validate(_grader())
    EvalKit.model_validate(_kit())


def test_build_id_must_belong_to_common_id() -> None:
    with pytest.raises(ValidationError, match="빌드가 아니다"):
        CandidateDescriptor.model_validate(_candidate(build_id="ct-ffffffffffff-b1"))


@pytest.mark.parametrize(
    "bad", ["../x", "ct-0123456789ab", "ct-0123456789ab-b0", "CT-0123456789AB-b1"]
)
def test_build_id_pattern_is_narrow(bad: str) -> None:
    with pytest.raises(ValidationError):
        CandidateDescriptor.model_validate(_candidate(build_id=bad))


def test_real_package_needs_approval_and_pinned_ontology() -> None:
    with pytest.raises(ValidationError, match="approval=approved"):
        CandidateDescriptor.model_validate(_candidate(approval="none"))
    with pytest.raises(ValidationError, match=r"\+dirty"):
        CandidateDescriptor.model_validate(_candidate(ontology_version="onto@abc+dirty"))
    # 목은 둘 다 허용
    CandidateDescriptor.model_validate(
        _candidate(mock=True, approval="none", ontology_version="onto@abc+dirty")
    )


def test_grader_commands_must_be_offline_and_have_selinux_fallback() -> None:
    g = _grader()
    with pytest.raises(ValidationError, match="network none"):
        GraderDescriptor.model_validate({**g, "grade": g["grade"].replace("--network none ", "")})
    with pytest.raises(ValidationError, match="label=disable"):
        GraderDescriptor.model_validate({**g, "grade_without_selinux_relabel": g["grade"]})


def test_grader_has_no_trap_fields() -> None:
    assert not {"trap_id", "intended_reason", "cases"} & set(GraderDescriptor.model_fields)
    assert not {"trap_id", "intended_reason", "cases"} & set(CandidateDescriptor.model_fields)


def test_verify_summary_passed_within_total() -> None:
    with pytest.raises(ValidationError, match="전체보다"):
        GraderDescriptor.model_validate(
            _grader(
                starter_expected={
                    "hidden_passed": 2,
                    "hidden_total": 1,
                    "public_passed": 0,
                    "public_total": 1,
                }
            )
        )


def test_kit_consistency() -> None:
    with pytest.raises(ValidationError, match="acceptance_hidden"):
        EvalKit.model_validate(
            _kit(acceptance_hidden=[{"ac_id": "h1", "trap_id": "t9", "text": "x"}])
        )
    case = _kit()["cases"][0]
    with pytest.raises(ValidationError, match="대상 역량에 없다"):
        EvalKit.model_validate(_kit(cases=[{**case, "competency_ids": ["comp:zzz"]}]))
    with pytest.raises(ValidationError, match="starter-only"):
        EvalKit.model_validate(_kit(verification={}))


def test_kit_needs_reviewer_decision_per_flag_unless_mock() -> None:
    with pytest.raises(ValidationError, match="승인자 판단"):
        EvalKit.model_validate(_kit(review_decisions=[]))
    EvalKit.model_validate(_kit(review_decisions=[], mock=True, approval="none"))
