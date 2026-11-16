"""문제 분석 판 명세의 참조 검사 (spec/analysis.py, analysis-stage.md §4.2·§6) — 가상 명세만."""

from __future__ import annotations

from typing import Any

from task_spec.schemas import (
    Fact,
    LinkedAcceptance,
    Requirement,
    TargetCompetency,
    TaskItem,
    TechEnv,
)
from task_spec.spec.analysis import check_analysis


def _fact(fid: str, covers: list[str]) -> Fact:
    return Fact(
        fact_id=fid,
        covers=covers,
        topic="가상 주제",
        ask_when="가상 조건을 물을 때",
        answer="가상 답",
    )


def _task(**over: Any) -> TaskItem:
    base: dict[str, Any] = {
        "task_id": "ct-000000000001",
        "track": "common",
        "task_kind": "feature",
        "target_competencies": [TargetCompetency(competency_id="comp:a", label="가상 역량")],
        "tech_env": TechEnv(basis="공고 언어"),
        "difficulty_target": "medium",
        "time_limit_min": 60,
        "requirements": [
            Requirement(req_id="r1", text="가상 요구 1"),
            Requirement(req_id="r2", text="가상 요구 2"),
        ],
        "acceptance_public": [
            LinkedAcceptance(ac_id="p1", text="가상 기준 1", req_ids=["r1"]),
            LinkedAcceptance(ac_id="p2", text="가상 기준 2", req_ids=["r2"]),
        ],
        "request": "가상 요청 글",
        "request_mentions": ["r1"],
        "facts": [_fact("f1", ["r1", "p1"]), _fact("f2", ["r2", "p2"])],
    }
    return TaskItem.model_validate({**base, **over})


def test_complete_analysis_spec_passes() -> None:
    assert check_analysis(_task()) == []


def test_old_spec_without_request_or_facts_passes() -> None:
    assert check_analysis(_task(request=None, request_mentions=[], facts=[])) == []


def test_request_and_facts_come_together() -> None:
    assert "사실이 없음" in check_analysis(_task(facts=[]))[0]
    assert "요청 글이 없음" in check_analysis(_task(request=None))[0]


def test_every_requirement_and_criterion_needs_a_covering_fact() -> None:
    """물어서 알 수 없는 요구로 채점하면 안 된다 (§4.2)."""
    problems = check_analysis(_task(facts=[_fact("f1", ["r1", "p1"])]))
    assert any("덮는 사실이 없는" in p and "r2" in p and "p2" in p for p in problems)


def test_references_must_resolve() -> None:
    problems = check_analysis(
        _task(
            facts=[_fact("f1", ["r1", "p1", "r9"]), _fact("f1", ["r2", "p2"])],
            request_mentions=["r7"],
        )
    )
    assert any("f1 의 covers 가 없는" in p and "r9" in p for p in problems)
    assert any("사실 id 가 겹침: f1" in p for p in problems)
    assert any("request_mentions 가 없는 요구" in p and "r7" in p for p in problems)


def test_acceptance_links_to_requirements() -> None:
    problems = check_analysis(
        _task(
            acceptance_public=[
                LinkedAcceptance(ac_id="p1", text="가상 기준 1", req_ids=[]),
                LinkedAcceptance(ac_id="p2", text="가상 기준 2", req_ids=["r5"]),
            ]
        )
    )
    assert any("p1 에 req_ids 가 없음" in p for p in problems)
    assert any("p2 의 req_ids 가 없는 요구" in p and "r5" in p for p in problems)
    assert any(
        "가리키는 공개 수용 기준이 없는 요구" in p and "r1" in p and "r2" in p for p in problems
    )
