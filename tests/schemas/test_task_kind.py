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
    Fact,
    HiddenAcceptance,
    LinkedAcceptance,
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
            acceptance_public=[LinkedAcceptance(ac_id="p1", text="성공한 변경 1건 → 기록 1건")],
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
    with pytest.raises(ValidationError, match="request"):  # 문제 분석 칸도 기능형 전용
        TaskItem(**_base(request="기록을 남겨 주세요"))


def _fact(**over: Any) -> Fact:
    return Fact(
        **{
            "fact_id": "f1",
            "covers": ["r1", "p1"],
            "topic": "기록을 남기는 때",
            "ask_when": "언제 기록을 남기는지 물을 때",
            "answer": "상태 변경 요청마다 한 건씩 남겨 주세요.",
            **over,
        }
    )


def test_analysis_fields_load_and_old_specs_stay_valid() -> None:
    """문제 분석 칸(요청 글·사실·req_ids) — 있는 명세와 없는 예전 명세가 둘 다 읽힌다 (analysis-stage.md §6)."""
    t = TaskItem(
        **_base(
            task_kind="feature",
            requirements=[Requirement(req_id="r1", text="상태 변경마다 기록을 남긴다")],
            acceptance_public=[
                LinkedAcceptance(ac_id="p1", text="성공한 변경 1건 → 기록 1건", req_ids=["r1"])
            ],
            background="가상 회사의 예약 서비스 — 운영팀이 상태 변경을 추적하지 못한다.",
            request="예약 상태가 언제 바뀌었는지 알 수 있게 해 주세요.",
            request_mentions=["r1"],
            facts=[_fact()],
        )
    )
    again = TaskItem.model_validate(t.model_dump(mode="json"))
    assert again.facts[0].covers == ["r1", "p1"] and again.acceptance_public[0].req_ids == ["r1"]
    old = again.model_dump(mode="json")
    for f in ("background", "request", "request_mentions", "facts"):
        old.pop(f)
    old["acceptance_public"][0].pop("req_ids")  # 칸이 생기기 전에 저장된 명세
    loaded = TaskItem.model_validate(old)
    assert (loaded.request, loaded.background, loaded.facts) == (None, None, [])
    assert loaded.acceptance_public[0].req_ids == []


def test_analysis_id_patterns() -> None:
    with pytest.raises(ValidationError):
        _fact(fact_id="c1")  # 사실은 f 번호
    with pytest.raises(ValidationError):
        _fact(covers=["h1"])  # 숨김 기준·함정은 덮지 않는다 (r#·p# 만)
    with pytest.raises(ValidationError):
        _fact(covers=[])
    with pytest.raises(ValidationError):
        LinkedAcceptance(ac_id="p1", text="x", req_ids=["p1"])


def test_draft_acceptance_schema_has_no_req_ids() -> None:
    """생성기 출력의 공개 수용 기준에는 req_ids 가 없다 — 출력 스키마가 프롬프트에 들어가므로 예전 판 프롬프트가 바뀌지 않게."""
    assert "req_ids" not in PublicAcceptance.model_json_schema()["properties"]
    assert "req_ids" in LinkedAcceptance.model_json_schema()["properties"]


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
