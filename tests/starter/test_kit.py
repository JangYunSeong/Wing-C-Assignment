"""평가 키트 과정 평가 기준 (starter/kit.py) — 가상 명세만."""

from __future__ import annotations

from task_spec.schemas import TargetCompetency, TaskItem, TechEnv
from task_spec.schemas.domain import (
    Artifact,
    Clarification,
    DebugStage,
    Observable,
    Trap,
)
from task_spec.starter.kit import kit_stages


def _task(**over: object) -> TaskItem:
    base: dict[str, object] = {
        "task_id": "ct-000000000001",
        "track": "common",
        "task_kind": "feature",
        "target_competencies": [TargetCompetency(competency_id="comp:a", label="가상 역량")],
        "tech_env": TechEnv(basis="공고 언어"),
        "difficulty_target": "medium",
        "time_limit_min": 60,
        "traps": [Trap(trap_id="t1", description="가상 함정", why_ai_fails="가상 이유")],
        "artifacts": [
            Artifact(artifact_id="a1", title="가상 지표", content="CONTENT-1", role="key"),
            Artifact(artifact_id="a2", title="가상 문서", content="CONTENT-2", role="distractor"),
        ],
        "max_requests": 1,
        "observables": [
            Observable(kind="first_mention_of", value="가상 개념"),
            Observable(kind="requests_artifact", value="a1"),
            Observable(kind="asks_clarification", value="c1"),
        ],
        "clarifications": [
            Clarification(
                clarification_id="c1",
                topic="가상 주제",
                answer="ANSWER-1",
                match_terms=["가상"],
                reveals=["t1"],
            )
        ],
        "debug_stage": DebugStage(
            time_limit_min=20,
            scenario="가상 증상",
            trap_ids=["t1"],
            artifacts=[
                Artifact(artifact_id="a8", title="가상 로그", content="CONTENT-8", role="key"),
                Artifact(
                    artifact_id="a9", title="가상 설정", content="CONTENT-9", role="distractor"
                ),
            ],
            max_requests=2,
            deliverables=["원인 메모"],
            observables=[
                Observable(kind="first_mention_of", value="가상 원인"),
                Observable(kind="requests_artifact", value="a8"),
            ],
        ),
    }
    return TaskItem.model_validate({**base, **over})


def test_both_stages_carry_roles_observables_and_traps() -> None:
    s1, s2 = kit_stages(_task())
    assert s1 is not None and s2 is not None
    assert s1.trap_ids == ["t1"] and s1.max_requests == 1
    assert [(m.artifact_id, m.role) for m in s1.materials] == [("a1", "key"), ("a2", "distractor")]
    assert [(c.clarification_id, c.reveals) for c in s1.clarifications] == [("c1", ["t1"])]
    assert [o.kind for o in s1.observables] == [
        "first_mention_of",
        "requests_artifact",
        "asks_clarification",
    ]
    assert s2.trap_ids == ["t1"] and s2.max_requests == 2 and s2.clarifications == []
    assert [(m.artifact_id, m.role) for m in s2.materials] == [("a8", "key"), ("a9", "distractor")]
    assert [o.value for o in s2.observables] == ["가상 원인", "a8"]


def test_kit_has_no_material_bodies_or_answers() -> None:
    """자료 본문·답 문장은 진행자 쪽에만 — 키트에 넣지 않는다."""
    s1, s2 = kit_stages(_task())
    dumped = (s1.model_dump_json() if s1 else "") + (s2.model_dump_json() if s2 else "")
    assert "CONTENT-" not in dumped and "ANSWER-" not in dumped


def test_stage1_carries_question_limit() -> None:
    s1, _ = kit_stages(_task(max_clarifications=1))
    assert s1 is not None and s1.max_clarifications == 1


def test_stage1_kept_when_only_traps() -> None:
    """함정만 있고 관찰 항목·자료·질문이 없어도 1단계(함정 목록)는 남는다."""
    s1, _ = kit_stages(
        _task(artifacts=[], observables=[], clarifications=[], max_requests=None, debug_stage=None)
    )
    assert s1 is not None and s1.trap_ids == ["t1"] and s1.materials == []


def test_missing_stages_are_none() -> None:
    s1, s2 = kit_stages(
        _task(
            traps=[],
            artifacts=[],
            observables=[],
            clarifications=[],
            max_requests=None,
            debug_stage=None,
        )
    )
    assert s1 is None and s2 is None
