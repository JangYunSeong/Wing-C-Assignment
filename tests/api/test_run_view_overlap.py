"""실행 조회(run_view)·승인 요약이 공고 역량 확인 전후 State 를 모두 읽는지. 가상 데이터만."""

from __future__ import annotations

from typing import Any

from task_spec.api.run_view import _summary
from task_spec.graph import make_serializer
from task_spec.schemas import (
    Comparison,
    CompetencyMatch,
    OverlapCheck,
    OverlapCheckResult,
    PostingCompetency,
    PostingProfile,
    RunState,
    TaskSpec,
)


def _profile() -> dict[str, Any]:
    return PostingProfile(
        posting_id="p-fake",
        ontology_version="v-test",
        competencies=[
            PostingCompetency(competency_id="comp:a", label="가상 A", primary=True),
            PostingCompetency(competency_id="comp:b", label="가상 B", primary=False),
        ],
    ).model_dump()


def _spec() -> dict[str, Any]:
    return TaskSpec(
        spec_id="r1-r1", run_id="r1", posting_id="p-fake", ontology_version="v-test"
    ).model_dump()


def test_old_checkpoint_without_check_fields_still_summarizes() -> None:
    # 확인 경로 이전 형식: overlap_check 없음, 대조 항목에 새 필드 없음
    values = {
        "run_id": "r1",
        "ontology_version": "v-test",
        "posting_id": "p-fake",
        "posting_profile": _profile(),
        "comparison": {
            "overlap": [
                {
                    "competency_id": "comp:a",
                    "label": "가상 A",
                    "in_posting": True,
                    "claim_ids": ["d1-c1"],
                }
            ],
            "posting_only": [{"competency_id": "comp:b", "label": "가상 B", "in_posting": True}],
        },
        "spec": _spec(),
    }
    s = _summary(values)
    assert s is not None and s.comparison is not None
    c = s.comparison
    assert c.check_status is None and c.related == []
    assert [
        (r.competency_id, r.status, r.source, r.check_label, r.claim_ids) for r in c.posting_checks
    ] == [
        ("comp:a", "overlap", None, None, ["d1-c1"]),
        ("comp:b", "posting_only", None, None, []),
    ]


def test_new_state_round_trips_through_checkpoint_serializer() -> None:
    state = RunState(
        run_id="r1",
        ontology_version="v-test",
        posting_id="p-fake",
        overlap_check=OverlapCheckResult(
            status="ok",
            items=[OverlapCheck(competency_id="comp:a", label="action", claim_ids=["d1-c1"])],
        ),
        comparison=Comparison(
            overlap=[
                CompetencyMatch(
                    competency_id="comp:a",
                    label="가상 A",
                    in_posting=True,
                    check_label="action",
                    check_claim_ids=["d1-c1"],
                    overlap_source="verify",
                    role="lead",
                )
            ],
            check_status="ok",
            downgraded=["comp:b"],
        ),
    )
    serde = make_serializer()
    back = serde.loads_typed(serde.dumps_typed(state.model_dump()))
    again = RunState.model_validate(back)
    assert again.overlap_check == state.overlap_check and again.comparison == state.comparison
    obj = serde.loads_typed(serde.dumps_typed(state.overlap_check))
    assert isinstance(obj, OverlapCheckResult) and obj == state.overlap_check


def test_old_json_validates_without_new_fields() -> None:
    """확인 경로 이전 체크포인트 JSON — 새 필드가 하나도 없는 형태."""
    from task_spec.schemas import ApprovalDecision

    old_cmp = {
        "overlap": [
            {
                "competency_id": "comp:a",
                "label": "가상 A",
                "in_posting": True,
                "primary": True,
                "claim_ids": ["d1-c1"],
                "relatedness": None,
                "relatedness_reason": None,
            }
        ],
        "posting_only": [],
        "applicant_only": [],
        "uncovered": [],
        "claims_to_verify": [
            {
                "claim_id": "d1-c1",
                "competency_ids": ["comp:a"],
                "role": "lead",
                "score": 4.5,
                "reasons": [],
            }
        ],
    }
    old_decision = {
        "status": "revise",
        "reviewer": "reviewer-fake",
        "comment": "가상 수정 요청",
        "revise_target": "all",
        "reassign": {},
        "at": "2026-11-03T09:00:00+09:00",
    }
    cmp = Comparison.model_validate(old_cmp)
    assert cmp.related == [] and cmp.check_status is None and cmp.downgraded == []
    m = cmp.overlap[0]
    assert (m.check_label, m.check_claim_ids, m.overlap_source, m.role, m.downgraded) == (
        None,
        [],
        None,
        None,
        False,
    )
    d = ApprovalDecision.model_validate(old_decision)
    assert d.downgrade_overlap == [] and d.restore_overlap == []
    st = RunState.model_validate(
        {
            "run_id": "r1",
            "ontology_version": "v-test",
            "posting_id": "p-fake",
            "posting_profile": _profile(),
            "comparison": old_cmp,
            "approval": old_decision,
            "spec": _spec(),
        }
    )
    assert st.overlap_check is None and st.comparison == cmp
