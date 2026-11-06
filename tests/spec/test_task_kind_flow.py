"""공통 과제 유형이 draft_spec → 승인 → 다음 지원자로 이어지는 흐름 (InMemoryCommonStore, LLM 없음). 가상 데이터만."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from task_spec.nodes.spec import (
    FLAG_FEATURE_DEFERRED,
    FLAG_FEATURE_SKELETON,
    FLAG_NO_DRAFTER,
    SKELETON_REF,
    make_draft_spec,
    make_finalize,
)
from task_spec.schemas import ApprovalDecision, PostingCompetency, PostingProfile, RunState
from task_spec.spec.store import CommonKey, InMemoryCommonStore
from tests.spec.test_common import _ev

ONTO = "onto-v2026.11.02"
BACKEND = [  # 만드는 역량 2 · 진단 역량 1 → 기본값 feature
    PostingCompetency(
        competency_id="comp:sw.rest_api",
        label="REST API",
        sections=["duties"],
        primary=True,
        levels=["수행"],
    ),
    PostingCompetency(
        competency_id="comp:sw.spring",
        label="Spring",
        sections=["required"],
        primary=True,
        levels=["활용"],
    ),
    PostingCompetency(
        competency_id="comp:infra.incident",
        label="장애 대응",
        sections=["duties"],
        primary=True,
        levels=["활용"],
    ),
]


def _state(run_id: str, **over: Any) -> RunState:
    profile = PostingProfile(
        posting_id="p-backend",
        ontology_version=ONTO,
        sub="sw.backend",
        industry_id="ind.x",
        competencies=BACKEND,
    )
    return RunState(
        run_id=run_id,
        ontology_version=ONTO,
        posting_id="p-backend",
        posting_profile=profile,
        evidence=[_ev(c.competency_id) for c in BACKEND],
        **over,
    )


def _decision(status: str, **over: Any) -> ApprovalDecision:
    return ApprovalDecision(
        status=status,  # type: ignore[arg-type]
        reviewer="reviewer-fake",
        at=datetime(2026, 11, 6, tzinfo=UTC),
        **over,
    )


async def test_feature_default_falls_back_to_incident_until_feature_drafter_exists() -> None:
    spec = (await make_draft_spec(None, store=InMemoryCommonStore())(_state("run-a")))["spec"]
    task = spec.common_tasks[0]
    assert task.task_kind == "incident"
    assert FLAG_NO_DRAFTER in task.review_flags
    deferred = [f for f in spec.review_flags if f.startswith(FLAG_FEATURE_DEFERRED)]
    assert deferred and "만드는 역량 2개" in deferred[0]
    assert spec.common_refs[0].prompt_ref == SKELETON_REF


async def test_reviewer_switches_to_feature_then_next_applicant_follows_approved_kind() -> None:
    store = InMemoryCommonStore()
    draft, finalize = make_draft_spec(None, store=store), make_finalize(store)
    s1 = (await draft(_state("run-a")))["spec"]

    revise = _decision("revise", comment="기능 개발형으로 바꿔 주세요", task_kind="feature")
    s2 = (await draft(_state("run-a", spec=s1, approval=revise)))["spec"]
    feat = s2.common_tasks[0]
    assert feat.task_kind == "feature"
    assert FLAG_FEATURE_SKELETON in feat.review_flags and FLAG_NO_DRAFTER not in feat.review_flags
    assert s2.common_refs[0].prompt_ref == f"{SKELETON_REF}+feature"  # 유형이 다르면 다른 판
    assert not [f for f in s2.review_flags if f.startswith(FLAG_FEATURE_DEFERRED)]

    await finalize(_state("run-a", spec=s2, approval=_decision("approved")))
    assert await store.approved_kind("p-backend", ONTO) == "feature"

    # 다음 지원자: 승인된 기능형 판을 재사용 (기본값 규칙·서술 유무와 무관)
    s3 = (await draft(_state("run-b")))["spec"]
    assert s3.common_tasks[0].task_kind == "feature"
    assert s3.common_refs[0].reused is True
    assert s3.common_tasks[0].task_id == feat.task_id


async def test_approved_incident_keeps_posting_on_incident() -> None:
    store = InMemoryCommonStore()
    draft, finalize = make_draft_spec(None, store=store), make_finalize(store)
    s1 = (await draft(_state("run-a")))["spec"]
    await finalize(_state("run-a", spec=s1, approval=_decision("approved")))
    assert await store.approved_kind("p-backend", ONTO) == "incident"
    s2 = (await draft(_state("run-b")))["spec"]
    assert s2.common_tasks[0].task_kind == "incident"
    # 승인된 유형을 따를 때는 '기능형 미루기' 표시를 붙이지 않는다
    assert not [f for f in s2.review_flags if f.startswith(FLAG_FEATURE_DEFERRED)]


async def test_approved_kind_is_latest_approval_per_posting() -> None:
    store = InMemoryCommonStore()
    assert await store.approved_kind("p-backend", ONTO) is None
    draft, finalize = make_draft_spec(None, store=store), make_finalize(store)
    s1 = (await draft(_state("run-a")))["spec"]
    await finalize(_state("run-a", spec=s1, approval=_decision("approved")))
    revise = _decision("revise", comment="기능 개발형으로", task_kind="feature")
    s2 = (await draft(_state("run-b", spec=s1, approval=revise)))["spec"]
    later = _decision("approved").model_copy(update={"at": datetime(2026, 11, 7, tzinfo=UTC)})
    await finalize(_state("run-b", spec=s2, approval=later))
    assert await store.approved_kind("p-backend", ONTO) == "feature"
    # 두 유형의 판은 키가 달라 서로 superseded 하지 않는다
    statuses = {r["key"].prompt_ref: r["status"] for r in store.rows}
    assert statuses == {SKELETON_REF: "approved", f"{SKELETON_REF}+feature": "approved"}
    assert await store.reusable(CommonKey("p-backend", ONTO, f"{SKELETON_REF}+feature"))


async def test_kind_survives_later_revise_without_task_kind() -> None:
    store = InMemoryCommonStore()
    draft = make_draft_spec(None, store=store)
    s1 = (await draft(_state("run-a")))["spec"]
    to_feature = _decision("revise", comment="기능 개발형으로", task_kind="feature")
    s2 = (await draft(_state("run-a", spec=s1, approval=to_feature)))["spec"]
    # 유형 없이 문구만 고치는 수정 요청 → 기능형 유지 (기본값 이슈형으로 돌아가지 않음)
    wording = _decision("revise", comment="상황 설명을 줄여 주세요", revise_target="common")
    s3 = (await draft(_state("run-a", spec=s2, approval=wording)))["spec"]
    assert s3.common_tasks[0].task_kind == "feature"
    # 확장 문항만 다시 → 공통 과제는 같은 기능형 판을 재사용
    verify_only = _decision("revise", comment="확장 문항만", revise_target="verification")
    s4 = (await draft(_state("run-a", spec=s3, approval=verify_only)))["spec"]
    assert s4.common_tasks[0].task_kind == "feature"
    assert s4.common_refs[0].reused is True
    assert s4.common_tasks[0].task_id == s3.common_tasks[0].task_id
