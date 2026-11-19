"""채용담당자 인터뷰로 승인된 공통 과제를 그 공고 지원자 실행이 그대로 쓴다 [확정: 2026-11-19 사용자]. 가상 데이터만.

① 공고(같은 온톨로지 판)마다 승인된 인터뷰 과제는 하나 — 바꾸려면 교체를 명시한다 (approve_interview(replaces=…)).
② 다른 온톨로지 판에만 있으면 그 공고 실행을 ① 공고 프로필에서 멈춘다 — 공개 사례 과제로 바꿔 만들지 않는다.
③ 지원자 명세 승인은 인터뷰 과제를 다시 승인하지 않는다 (승인자 기록·교체 유지) — 지금 과제인지만 확인한다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from task_spec.llm import Completion, FakeBackend
from task_spec.nodes.posting_profile import make_posting_profile
from task_spec.nodes.pseudonymize import mint_pseudonymized
from task_spec.nodes.spec import (
    FLAG_INTERVIEW_COMMON,
    FLAG_INTERVIEW_SWITCHED,
    CommonTaskNotCurrent,
    make_draft_spec,
    make_finalize,
)
from task_spec.schemas import (
    ApprovalDecision,
    Claim,
    Comparison,
    Provenance,
    RunState,
    TargetCompetency,
    TaskItem,
    TechEnv,
    VerifyTarget,
)
from task_spec.spec import questions as q_mod
from task_spec.spec.questions import FLAG_UNAPPROVED_SLOT
from task_spec.spec.store import (
    CommonKey,
    InMemoryCommonStore,
    InterviewTaskConflict,
    StoredCommon,
    interview_lock,
    replaced_common,
)
from tests.spec.test_assign import PROMPTS, QUOTE, SLOTS, _q
from tests.spec.test_draft import _client
from tests.spec.test_store import _state

ONTO = "onto-v2026.11.02"  # tests.spec.test_store._state 의 판
RECRUITER_AT = datetime(2026, 11, 19, 9, tzinfo=UTC)


def _task(title: str) -> TaskItem:
    return TaskItem(
        task_id="tmp",
        track="common",
        task_kind="feature",
        target_competencies=[TargetCompetency(competency_id="comp:a", label="가상 역량")],
        tech_env=TechEnv(basis="공고 언어"),
        difficulty_target="medium",
        time_limit_min=60,
        title=title,
    )


def _prov(n: int) -> Provenance:
    return Provenance(
        provenance_id=f"prov-iv-{n}",
        prompt="draft_feature_task@2",
        prompt_sha256="0" * 64,
        model="opencode:fake",
        ontology_version=ONTO,
    )


async def _interview(
    store: InMemoryCommonStore,
    interview_id: str,
    *,
    version: str = ONTO,
    replaces: str | None = None,
    n: int = 1,
) -> StoredCommon:
    """인터뷰 하나가 만든 과제를 저장하고 채용담당자가 승인한다 (같은 인터뷰를 다시 부르면 그 인터뷰의 다음 판)."""
    key = CommonKey("p", version, f"interview:{interview_id}")
    row = await store.save_draft(key, _task(f"가상 인터뷰 과제 {interview_id}"), [_prov(n)])
    await store.approve_interview(row.id, "recruiter-fake", RECRUITER_AT, replaces=replaces)
    got = await store.interview_task("p", version)
    assert got is not None and got.id == row.id
    return got


def _decision(status: str, **over: Any) -> ApprovalDecision:
    return ApprovalDecision(
        status=status,  # type: ignore[arg-type]
        reviewer="reviewer-fake",
        at=datetime(2026, 11, 20, tzinfo=UTC),
        **over,
    )


def _row(store: InMemoryCommonStore, common_id: str) -> dict[str, Any]:
    return next(r for r in store.rows if r["id"] == common_id)


# ── 저장소: 공고마다 하나, 교체는 명시 ─────────────────────────────────────────────


async def test_second_interview_needs_explicit_replacement() -> None:
    store = InMemoryCommonStore()
    first = await _interview(store, "iv-1")
    second = await store.save_draft(CommonKey("p", ONTO, "interview:iv-2"), _task("둘째"), [])

    with pytest.raises(InterviewTaskConflict) as e:
        await store.approve_interview(second.id, "recruiter-2", RECRUITER_AT)
    assert (e.value.current, e.value.replaces) == (first.id, None)
    with pytest.raises(InterviewTaskConflict):  # 지금 과제가 아닌 것을 짚으면 (그 사이 다른 승인)
        await store.approve_interview(second.id, "recruiter-2", RECRUITER_AT, replaces="ct-old")
    assert _row(store, second.id)["status"] == "draft"

    await store.approve_interview(second.id, "recruiter-2", RECRUITER_AT, replaces=first.id)
    assert (_row(store, first.id)["status"], _row(store, second.id)["status"]) == (
        "superseded",
        "approved",
    )
    got = await store.interview_task("p", ONTO)
    assert got is not None and got.id == second.id and got.prompt_ref == "interview:iv-2"


async def test_new_revision_of_same_interview_is_also_a_replacement() -> None:
    """같은 인터뷰의 고친 판도 모집 중 과제가 바뀌는 것 — 교체를 명시해야 한다."""
    store = InMemoryCommonStore()
    r1 = await _interview(store, "iv-1")
    r2 = await store.save_draft(CommonKey("p", ONTO, "interview:iv-1"), _task("고친 판"), [])
    assert r2.revision == 2
    with pytest.raises(InterviewTaskConflict):
        await store.approve_interview(r2.id, "recruiter-fake", RECRUITER_AT)
    await store.approve_interview(r2.id, "recruiter-fake", RECRUITER_AT, replaces=r1.id)
    assert _row(store, r1.id)["status"] == "superseded"


async def test_reapproving_current_interview_task_keeps_its_record() -> None:
    store = InMemoryCommonStore()
    iv = await _interview(store, "iv-1")
    await store.approve_interview(iv.id, "someone-else", datetime(2026, 12, 1, tzinfo=UTC))
    row = _row(store, iv.id)
    assert (row["approved_by"], row["approved_at"]) == ("recruiter-fake", RECRUITER_AT)


async def test_plain_approve_refuses_interview_task_and_vice_versa() -> None:
    store = InMemoryCommonStore()
    iv = await _interview(store, "iv-1")
    with pytest.raises(ValueError, match="approve_interview"):
        await store.approve(iv.id, "reviewer-fake", RECRUITER_AT)
    p1 = await store.save_draft(CommonKey("p", ONTO, "draft_task@3"), _task("P1 과제"), [])
    with pytest.raises(ValueError, match="인터뷰 과제가 아닙니다"):
        await store.approve_interview(p1.id, "recruiter-fake", RECRUITER_AT)
    with pytest.raises(KeyError):
        await store.approve_interview("ct-nope", "recruiter-fake", RECRUITER_AT)


async def test_interview_tasks_are_per_ontology_version() -> None:
    store = InMemoryCommonStore()
    assert await store.interview_versions("p") == []
    old = await _interview(store, "iv-1", version="onto-v2026.10.01")
    assert await store.interview_task("p", ONTO) is None
    new = await _interview(
        store, "iv-1", version=ONTO
    )  # 새 판에서 다시 승인 — 다른 판 과제는 교체가 아니다
    assert await store.interview_versions("p") == ["onto-v2026.10.01", ONTO]
    assert _row(store, old.id)["status"] == "approved"
    assert (await store.interview_task("p", ONTO)).id == new.id  # type: ignore[union-attr]


async def test_replaced_interview_task_is_not_reapproved() -> None:
    """교체된 과제를 다시 승인하면 예전 판이 되살아나는 일이 저장소에서 막힌다 — 되돌리려면 새 판으로 저장해 승인."""
    store = InMemoryCommonStore()
    first = await _interview(store, "iv-1")
    second = await _interview(store, "iv-2", replaces=first.id)
    with pytest.raises(ValueError, match="교체된 인터뷰 과제"):
        await store.approve_interview(first.id, "recruiter-fake", RECRUITER_AT, replaces=second.id)
    assert _row(store, second.id)["status"] == "approved"


async def test_interview_task_id_is_aligned_with_store_id_on_read() -> None:
    """인터뷰 계층이 과제 id 를 저장 id 와 다르게 썼어도 읽을 때 맞춘다 (CommonRef.common_id = task_id, 슬롯 parent)."""
    store = InMemoryCommonStore()
    iv = await _interview(store, "iv-1")
    row = _row(store, iv.id)
    odd = _task("가상 인터뷰 과제").model_copy(update={"task_id": "iv-1-task", "slots": SLOTS[:1]})
    row["task"] = odd
    got = await store.interview_task("p", ONTO)
    assert got is not None and got.task.task_id == iv.id
    assert [x.parent_task_id for x in got.task.slots] == [iv.id]

    draft = make_draft_spec(None, store=store)
    s1 = (await draft(_state("run-a")))["spec"]
    revise = _decision("revise", comment="면접 질문을 더 구체적으로", revise_target="verification")
    out = await draft(_state("run-a", spec=s1, approval=revise))
    assert out["spec"].common_tasks[0].task_id == iv.id
    assert not any(f.startswith(FLAG_INTERVIEW_SWITCHED) for f in out["spec"].review_flags)


# ── 지원자 실행: 만들지 않고 그대로 쓴다 ─────────────────────────────────────────


async def test_applicant_run_uses_approved_interview_task() -> None:
    store = InMemoryCommonStore()
    iv = await _interview(store, "iv-1")
    out = await make_draft_spec(None, store=store)(_state("run-a"))
    spec = out["spec"]
    assert [t.task_id for t in spec.common_tasks] == [iv.id]
    assert (
        spec.common_tasks[0].task_kind == "feature" and spec.common_tasks[0].title == iv.task.title
    )
    ref = spec.common_refs[0]
    assert (ref.common_id, ref.revision, ref.status, ref.reused, ref.prompt_ref) == (
        iv.id,
        1,
        "approved",
        True,
        "interview:iv-1",
    )
    assert FLAG_INTERVIEW_COMMON in spec.review_flags
    assert len(store.rows) == 1  # P1 과제를 만들지 않았다
    assert [p.provenance_id for p in out["provenance"]] == ["prov-iv-1"]


async def test_interview_task_wins_over_p1_approved_task() -> None:
    """P1 이 만든 이슈형 과제가 승인돼 있어도, 인터뷰 과제가 승인되면 다음 지원자는 인터뷰 과제(기능형)를 받는다."""
    store = InMemoryCommonStore()
    draft, finalize = make_draft_spec(None, store=store), make_finalize(store)
    s1 = (await draft(_state("run-a")))["spec"]
    await finalize(_state("run-a", spec=s1, approval=_decision("approved")))
    assert await store.approved_kind("p", ONTO) == "incident"

    iv = await _interview(store, "iv-1")
    s2 = (await draft(_state("run-b")))["spec"]
    assert s2.common_tasks[0].task_id == iv.id and s2.common_tasks[0].task_kind == "feature"


async def test_interview_task_of_other_version_is_not_used_by_draft() -> None:
    store = InMemoryCommonStore()
    await _interview(store, "iv-1", version="onto-v2026.10.01")
    spec = (await make_draft_spec(None, store=store)(_state("run-a")))["spec"]
    assert not spec.common_refs[0].prompt_ref.startswith("interview:")


async def test_verification_revise_keeps_same_interview_task() -> None:
    store = InMemoryCommonStore()
    iv = await _interview(store, "iv-1")
    draft = make_draft_spec(None, store=store)
    s1 = (await draft(_state("run-a")))["spec"]
    revise = _decision("revise", comment="면접 질문을 더 구체적으로", revise_target="verification")
    out = await draft(_state("run-a", spec=s1, approval=revise))
    assert (
        out["spec"].common_tasks == s1.common_tasks and out["spec"].common_tasks[0].task_id == iv.id
    )
    assert not out.get("provenance")  # 같은 과제 — provenance 는 첫 초안 때 이미 State 에 있다
    assert FLAG_INTERVIEW_SWITCHED not in " ".join(out["spec"].review_flags)


async def test_common_revise_is_recorded_not_applied() -> None:
    """API 가 409 로 먼저 막지만 노드에 들어와도 인터뷰 과제를 고치거나 유형을 바꾸지 않고 기록만 남긴다."""
    store = InMemoryCommonStore()
    iv = await _interview(store, "iv-1")
    draft = make_draft_spec(None, store=store)
    s1 = (await draft(_state("run-a")))["spec"]
    revise = _decision(
        "revise", comment="상황을 줄여 주세요", revise_target="all", task_kind="incident"
    )
    out = await draft(_state("run-a", spec=s1, approval=revise))
    assert out["spec"].common_tasks[0].task_id == iv.id
    assert out["spec"].common_tasks[0].task_kind == "feature"
    assert [e.code for e in out["errors"]] == ["interview_common_locked"]
    assert out["errors"][0].ref_id == iv.id and "유형 바꾸기" in out["errors"][0].message
    assert len(store.rows) == 1


async def test_verification_revise_picks_up_replaced_interview_task() -> None:
    store = InMemoryCommonStore()
    first = await _interview(store, "iv-1")
    draft = make_draft_spec(None, store=store)
    s1 = (await draft(_state("run-a")))["spec"]
    second = await _interview(store, "iv-2", replaces=first.id, n=2)

    revise = _decision("revise", comment="인터뷰 과제 교체 반영", revise_target="verification")
    out = await draft(_state("run-a", spec=s1, approval=revise))
    assert out["spec"].common_tasks[0].task_id == second.id
    assert FLAG_INTERVIEW_SWITCHED + first.id in out["spec"].review_flags
    assert [p.provenance_id for p in out["provenance"]] == ["prov-iv-2"]


async def test_interview_path_builds_experience_verification_with_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """두 쪽을 붙이는 이유 — 인터뷰 과제의 슬롯에 지원자 주장을 배정하고 면접 질문을 만든다 (P1 골격 서술 입력 없이)."""

    async def texts(state: RunState, ids: list[str]) -> tuple[dict[str, Any], list[Any]]:
        return {i: mint_pseudonymized(QUOTE) for i in ids}, []

    monkeypatch.setattr(q_mod, "_claim_texts", texts)
    store = InMemoryCommonStore()
    key = CommonKey("p", ONTO, "interview:iv-1")
    row = await store.save_draft(
        key, _task("가상 인터뷰 과제").model_copy(update={"slots": SLOTS}), [_prov(1)]
    )
    await store.approve_interview(row.id, "recruiter-fake", RECRUITER_AT)

    fake = FakeBackend().push(Completion(json.dumps(_q(), ensure_ascii=False)))
    draft = make_draft_spec(None, llm=_client(fake), prompts=PROMPTS, store=store)
    claims = [Claim(claim_id="c1", doc_id="d1", action="가상 행동", quote=QUOTE)]
    comparison = Comparison(
        claims_to_verify=[VerifyTarget(claim_id="c1", competency_ids=["comp:go"], score=6)]
    )
    out = await draft(_state("run-a", claims=claims, comparison=comparison))
    spec = out["spec"]
    assert [(a.claim_id, a.slot_id) for a in spec.slot_assignments] == [("c1", "s1")]
    assert [(v.slot_id, v.parent_task_id) for v in spec.verification_tasks] == [("s1", row.id)]
    assert spec.verification_tasks[0].follow_up_questions
    assert FLAG_UNAPPROVED_SLOT not in spec.verification_tasks[0].review_flags  # 승인된 과제의 슬롯
    assert len(fake.calls) == 1  # 면접 질문만 — 공통 과제 서술·슬롯 생성 호출 없음
    assert [p.provenance_id for p in out["provenance"]][0] == "prov-iv-1"
    assert len(out["provenance"]) == 2


# ── 확정: 다시 승인하지 않고, 지금 과제인지만 ──────────────────────────────────────


async def test_finalize_keeps_recruiter_approval_record() -> None:
    store = InMemoryCommonStore()
    iv = await _interview(store, "iv-1")
    draft, finalize = make_draft_spec(None, store=store), make_finalize(store)
    for run in ("run-a", "run-b"):
        spec = (await draft(_state(run)))["spec"]
        out = await finalize(_state(run, spec=spec, approval=_decision("approved")))
        assert out["spec"].approval.reviewer == "reviewer-fake"  # 지원자 명세 승인 기록은 명세에
    row = _row(store, iv.id)
    assert (row["status"], row["approved_by"], row["approved_at"]) == (
        "approved",
        "recruiter-fake",
        RECRUITER_AT,
    )


async def test_finalize_refuses_spec_whose_interview_task_was_replaced() -> None:
    """교체된 과제를 가리키는 명세를 확정하면 예전에는 그 판이 다시 approved 로 되살아났다 (store.approve)."""
    store = InMemoryCommonStore()
    first = await _interview(store, "iv-1")
    spec = (await make_draft_spec(None, store=store)(_state("run-a")))["spec"]
    second = await _interview(store, "iv-2", replaces=first.id)

    assert await replaced_common(store, spec) == [first.id]
    with pytest.raises(CommonTaskNotCurrent) as e:
        await make_finalize(store)(_state("run-a", spec=spec, approval=_decision("approved")))
    assert e.value.common_ids == [first.id]
    assert (_row(store, first.id)["status"], _row(store, second.id)["status"]) == (
        "superseded",
        "approved",
    )


async def test_finalize_refuses_p1_spec_made_before_interview_approval() -> None:
    store = InMemoryCommonStore()
    spec = (await make_draft_spec(None, store=store)(_state("run-a")))["spec"]
    p1 = spec.common_refs[0].common_id
    await _interview(store, "iv-1")
    with pytest.raises(CommonTaskNotCurrent):
        await make_finalize(store)(_state("run-a", spec=spec, approval=_decision("approved")))
    assert _row(store, p1)["status"] == "draft"  # 이 공고가 이제 쓰지 않는 과제를 승인하지 않았다


async def test_interview_lock_only_when_spec_has_common_task() -> None:
    store = InMemoryCommonStore()
    spec = (await make_draft_spec(None, store=store)(_state("run-a")))["spec"]
    assert await interview_lock(store, spec) is None  # 인터뷰 과제 없음 — P1 수정 요청 그대로
    iv = await _interview(store, "iv-1")
    assert await interview_lock(store, spec) == iv.id  # 다시 만들어도 인터뷰 과제를 쓴다
    empty = spec.model_copy(update={"common_refs": [], "common_tasks": []})
    assert await interview_lock(store, empty) is None


# ── ① 공고 프로필: 다른 판에만 있으면 멈춘다 ──────────────────────────────────────


async def test_posting_profile_stops_when_interview_task_is_on_other_version_only() -> None:
    store = InMemoryCommonStore()
    node = make_posting_profile(None, common=store)
    state = RunState(run_id="run-a", ontology_version=ONTO, posting_id="p")
    assert (await node(state)).get("posting_profile") is not None  # 인터뷰 과제 없음

    await _interview(store, "iv-1", version="onto-v2026.10.01")
    out = await node(state)
    assert out.get("posting_profile") is None
    assert [(e.code, e.ref_id) for e in out["errors"]] == [("interview_task_version_mismatch", "p")]
    assert "onto-v2026.10.01" in out["errors"][0].message

    await _interview(store, "iv-1", version=ONTO)  # 이 판에서 다시 승인하면 진행
    assert (await node(state)).get("posting_profile") is not None
