"""공통 과제 유형(task_kind)과 기능형 필드 — 하위 호환·형식 검사. 가상 데이터만."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from task_spec.schemas import (
    AcceptedDesign,
    ApprovalDecision,
    Artifact,
    Clarification,
    DebugStage,
    HiddenAcceptance,
    PublicAcceptance,
    Requirement,
    TargetCompetency,
    TaskItem,
    TechEnv,
)


def _base(**over: Any) -> dict[str, Any]:
    return {
        "task_id": "t1",
        "track": "common",
        "target_competencies": [TargetCompetency(competency_id="comp:a", label="A")],
        "tech_env": TechEnv(languages=["Java"], basis="공고 언어"),
        "difficulty_target": "medium",
        "time_limit_min": 60,
        **over,
    }


def _debug() -> DebugStage:
    return DebugStage(
        time_limit_min=20,
        scenario="배포 다음 날 오후 마감 시간대에 상태 변경 요청 일부가 오래 걸리다 실패했다는 보고가 들어왔다.",
        trap_ids=["t1"],
        artifacts=[Artifact(artifact_id="a7", title="API 응답 지표", content="p99 …", role="key")],
        max_requests=2,
        deliverables=["원인 진단과 수정 (이미 대응했다면 그 근거)"],
    )


def test_old_stored_task_without_kind_loads_as_incident() -> None:
    dumped = TaskItem(**_base()).model_dump(mode="json")
    for f in (
        "task_kind",
        "requirements",
        "acceptance_public",
        "acceptance_hidden",
        "clarifications",
        "max_clarifications",
        "accepted_designs",
        "debug_stage",
    ):
        dumped.pop(f)
    assert TaskItem.model_validate(dumped).task_kind == "incident"


def test_feature_task_with_two_stages_validates() -> None:
    t = TaskItem(
        **_base(
            task_kind="feature",
            requirements=[Requirement(req_id="r1", text="상태 변경마다 기록을 남긴다")],
            acceptance_public=[PublicAcceptance(ac_id="p1", text="성공한 변경 1건 → 기록 1건")],
            acceptance_hidden=[
                HiddenAcceptance(ac_id="h1", trap_id="t1", text="동시 32건에서 모두 기한 안")
            ],
            clarifications=[
                Clarification(
                    clarification_id="c1",
                    topic="피크 요청량",
                    answer="마감 직전 수십 건 동시",
                    reveals=["t1"],
                )
            ],
            max_clarifications=2,
            accepted_designs=[
                AcceptedDesign(
                    design_id="d1", summary="같은 트랜잭션", conditions="…", trap_ids=["t1"]
                ),
                AcceptedDesign(
                    design_id="d2", summary="끝난 뒤 별도 기록", conditions="…", trap_ids=["t1"]
                ),
            ],
            debug_stage=_debug(),
        )
    )
    assert t.task_kind == "feature" and t.debug_stage is not None
    assert t.debug_stage.reveal_after == "stage1_submitted"


def test_feature_fields_rejected_on_incident() -> None:
    with pytest.raises(ValidationError, match="task_kind=feature 일 때만"):
        TaskItem(**_base(requirements=[Requirement(req_id="r1", text="x")]))
    with pytest.raises(ValidationError, match="debug_stage"):
        TaskItem(**_base(debug_stage=_debug()))


def test_feature_only_on_common_track() -> None:
    with pytest.raises(ValidationError, match="track=common"):
        TaskItem(**_base(track="extension_slot", task_kind="feature"))


def test_id_patterns() -> None:
    with pytest.raises(ValidationError):
        Requirement(req_id="req1", text="x")
    with pytest.raises(ValidationError):
        HiddenAcceptance(ac_id="p1", trap_id="t1", text="x")  # 숨김은 h 번호


def _decision(**over: Any) -> ApprovalDecision:
    return ApprovalDecision(
        **{
            "status": "revise",
            "reviewer": "reviewer-fake",
            "comment": "기능 개발형으로 바꿔 주세요",
            "at": datetime(2026, 11, 6, tzinfo=UTC),
            **over,
        }
    )


def test_approval_task_kind_only_on_common_revise() -> None:
    assert _decision(task_kind="feature").task_kind == "feature"
    assert _decision(task_kind="incident", revise_target="common").task_kind == "incident"
    with pytest.raises(ValidationError, match="revise 결정에서만"):
        _decision(status="approved", comment=None, task_kind="feature")
    with pytest.raises(ValidationError, match="revise_target"):
        _decision(task_kind="feature", revise_target="verification")
