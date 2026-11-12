"""공고 단위 공통 과제 재사용 (InMemoryCommonStore) — 같은 공고 지원자는 같은 공통 과제. 가상 데이터만."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from task_spec.nodes.spec import (
    FLAG_REUSED_DRAFT,
    SKELETON_REF,
    CommonTaskMissing,
    approval_summary,
    make_draft_spec,
    make_finalize,
)
from task_spec.schemas import (
    ApprovalDecision,
    Comparison,
    RunState,
    VerifyTarget,
)
from task_spec.spec.store import CommonKey, InMemoryCommonStore
from tests.spec.test_common import COMPS, _ev, _profile


def _state(run_id: str, **over: Any) -> RunState:
    ev = [_ev(c.competency_id) for c in COMPS if c.competency_id != "comp:noev"]
    return RunState(
        run_id=run_id,
        ontology_version="onto-v2026.11.02",
        posting_id="p",
        posting_profile=_profile(COMPS),
        evidence=ev,
        **over,
    )


def _decision(status: str, comment: str | None = None) -> ApprovalDecision:
    return ApprovalDecision(
        status=status,  # type: ignore[arg-type]
        reviewer="reviewer-fake",
        comment=comment,
        at=datetime(2026, 11, 3, tzinfo=UTC),
    )


async def test_second_applicant_reuses_same_common_task() -> None:
    store = InMemoryCommonStore()
    draft = make_draft_spec(None, store=store)
    a = (await draft(_state("run-a")))["spec"]
    b = (await draft(_state("run-b")))["spec"]
    assert len(store.rows) == 1
    assert a.common_tasks[0].task_id == b.common_tasks[0].task_id
    assert a.common_tasks[0].task_id.startswith("ct-")
    assert (a.common_refs[0].reused, b.common_refs[0].reused) == (False, True)
    # 미승인 초안 재사용은 승인자에게 표시 (store 에는 표시 없이 저장)
    assert FLAG_REUSED_DRAFT in b.common_tasks[0].review_flags
    assert FLAG_REUSED_DRAFT not in a.common_tasks[0].review_flags
    assert b.common_refs[0].prompt_ref == SKELETON_REF


async def test_approve_then_revise_creates_new_revision_and_supersedes() -> None:
    store = InMemoryCommonStore()
    draft = make_draft_spec(None, store=store)
    finalize = make_finalize(store)
    key = CommonKey("p", "onto-v2026.11.02", SKELETON_REF)

    s1 = (await draft(_state("run-a")))["spec"]
    await finalize(_state("run-a", spec=s1, approval=_decision("approved")))
    r1 = await store.reusable(key)
    assert r1 is not None and r1.status == "approved" and r1.revision == 1

    # 다른 지원자가 revise → 재사용하지 않고 새 판(r2, 초안)
    s2 = (
        await draft(_state("run-b", spec=s1, approval=_decision("revise", "자료를 줄여 주세요")))
    )["spec"]
    assert s2.common_refs[0].reused is False and s2.common_refs[0].revision == 2
    still = await store.reusable(key)
    assert still is not None and still.revision == 1  # 승인된 판이 여전히 우선

    await finalize(_state("run-b", spec=s2, approval=_decision("approved")))
    now = await store.reusable(key)
    assert now is not None and now.revision == 2 and now.status == "approved"
    assert [r["status"] for r in store.rows] == ["superseded", "approved"]


async def test_without_store_keeps_run_scoped_task_id() -> None:
    spec = (await make_draft_spec(None)(_state("run-a")))["spec"]
    assert spec.common_tasks[0].task_id == "run-a-r1-common-1" and spec.common_refs == []


async def test_approval_summary_has_tasks_common_and_verify_claims() -> None:
    store = InMemoryCommonStore()
    spec = (await make_draft_spec(None, store=store)(_state("run-a")))["spec"]
    comp = Comparison(
        claims_to_verify=[VerifyTarget(claim_id="c1", competency_ids=["comp:a"], score=4.5)]
    )
    summary = approval_summary(_state("run-a", spec=spec, comparison=comp))
    assert summary.tasks[0].track == "common"
    assert summary.common[0].revision == 1
    assert summary.total_time_min == 60
    assert summary.comparison is not None
    assert summary.comparison.claims_to_verify[0].claim_id == "c1"
    # 인증 전: 주장 인용(quote)은 요약 모델에 필드 자체가 없다
    assert "quote" not in type(summary.comparison.claims_to_verify[0]).model_fields


async def test_finalize_refuses_when_common_task_is_gone() -> None:
    """명세가 가리키는 공통 과제 판이 지워졌으면 (운영 DB 정리 등) KeyError 대신 사유를 담은 오류, 다른 판은 승인하지 않는다."""
    store = InMemoryCommonStore()
    spec = (await make_draft_spec(None, store=store)(_state("run-a")))["spec"]
    gone = spec.common_refs[0].common_id
    assert await store.missing([gone, "ct-nope"]) == ["ct-nope"]
    store.rows.clear()
    with pytest.raises(CommonTaskMissing) as e:
        await make_finalize(store)(_state("run-a", spec=spec, approval=_decision("approved")))
    assert e.value.common_ids == [gone] and gone in str(e.value)
