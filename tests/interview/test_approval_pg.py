"""PgApprovalCommitter — 인터뷰 판본·회사 사례·공통 과제를 한 트랜잭션으로 (#266 D2b). task_db_test (`-m pg`), 가상 데이터만.

성공하면 셋 다 바뀌고, 교체 충돌·판본 바뀜이면 아무것도 바뀌지 않는다.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from task_spec.interview.approval import ApprovalCommit, PgApprovalCommitter, interview_ref
from task_spec.interview.store import PgInterviewStore
from task_spec.schemas import PublicIncident
from task_spec.schemas.interview_state import InterviewState
from task_spec.spec.company_incidents import CompanyIncidentStore
from task_spec.spec.store import CommonKey, CommonTaskStore, InterviewTaskConflict
from tests.interview.test_store_pg import _state
from tests.interview.test_task import PROFILE, fake_task, known_incident
from tests.postings.test_postings_pg import _Urls

pytestmark = pytest.mark.pg
VERSION = "onto-test"
AT = datetime(2026, 11, 19, tzinfo=UTC)


@dataclass
class Stores:
    interviews: PgInterviewStore
    common: CommonTaskStore
    incidents: CompanyIncidentStore
    committer: PgApprovalCommitter
    posting_id: str
    incident_ids: list[str]

    def case(self) -> PublicIncident:
        inc = known_incident().model_copy(
            update={"incident_id": f"co:sw.backend:{uuid.uuid4().hex[:12]}"}
        )
        self.incident_ids.append(inc.incident_id)
        return inc


@pytest.fixture
async def stores() -> AsyncIterator[Stores]:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    url = u.get_secret_value()
    interviews = await PgInterviewStore.open(url)
    common = await CommonTaskStore.open(url)
    incidents = await CompanyIncidentStore.open(url)
    posting_id = f"prov-{uuid.uuid4().hex[:12]}"
    found = Stores(
        interviews, common, incidents, PgApprovalCommitter(interviews.pool), posting_id, []
    )
    try:
        yield found
    finally:
        async with interviews.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.common_task WHERE posting_id = %s", (posting_id,)
            )
            await conn.execute(
                "DELETE FROM public.company_incident WHERE id = ANY(%s)", (found.incident_ids,)
            )
            await conn.execute(
                "DELETE FROM public.recruiter_interview WHERE state->>'posting_id' = %s",
                (posting_id,),
            )
        await incidents.close()
        await common.close()
        await interviews.close()


async def interview(s: Stores) -> tuple[str, list[str]]:
    """과제 검토 단계의 인터뷰 하나와 그 초안 판 2개."""
    iid = f"int-{uuid.uuid4().hex}"
    state = _state().model_copy(update={"stage": "task_review", "posting_id": s.posting_id})
    await s.interviews.create(iid, state)
    key = CommonKey(s.posting_id, VERSION, interview_ref(iid))
    ids = [
        (await s.common.save_draft(key, fake_task(PROFILE, "t", scenario=f"가상 {n}"), [])).id
        for n in (1, 2)
    ]
    return iid, ids


def commit(
    iid: str,
    revision: int,
    state: InterviewState,
    incident: PublicIncident,
    common_id: str,
    *,
    replaces: str | None = None,
) -> ApprovalCommit:
    return ApprovalCommit(
        interview_id=iid,
        revision=revision,
        state=state.model_copy(update={"stage": "approved"}),
        company_id="가상회사",
        incident=incident,
        common_id=common_id,
        reviewer="가상 승인자",
        at=AT,
        replaces=replaces,
    )


async def test_commit_is_all_or_nothing(stores: Stores) -> None:
    s = stores
    first, (old_draft, first_task) = await interview(s)
    row = await s.interviews.get(first)
    assert row is not None
    inc1 = s.case()
    c1 = commit(first, row.revision, row.state, inc1, first_task)
    assert await s.committer.commit(c1) is True
    after = await s.interviews.get(first)
    assert (
        after is not None and after.state.stage == "approved" and after.revision == row.revision + 1
    )
    rec = await s.incidents.get(inc1.incident_id)
    assert rec is not None and rec.status == "pending" and rec.source["interview_id"] == first
    approved = await s.common.interview_task(s.posting_id, VERSION)
    assert approved is not None and approved.id == first_task and approved.approved_at is not None
    assert await s.common.missing([old_draft]) == [old_draft]  # 같은 인터뷰의 다른 초안은 지움

    # 교체를 명시하지 않은 두 번째 승인 — 아무것도 바뀌지 않는다
    second, (_, second_task) = await interview(s)
    row2 = await s.interviews.get(second)
    assert row2 is not None
    inc2 = s.case()
    with pytest.raises(InterviewTaskConflict) as err:
        await s.committer.commit(commit(second, row2.revision, row2.state, inc2, second_task))
    assert err.value.current == first_task
    unchanged = await s.interviews.get(second)
    assert unchanged is not None and unchanged.revision == row2.revision
    assert unchanged.state.stage == "task_review"
    assert await s.incidents.get(inc2.incident_id) is None
    assert (await s.common.interview_task(s.posting_id, VERSION)).id == first_task  # type: ignore[union-attr]
    assert await s.common.missing([second_task]) == []

    # 판본이 바뀐 승인 — False, 아무것도 바뀌지 않는다
    stale = commit(second, row2.revision + 5, row2.state, inc2, second_task, replaces=first_task)
    assert await s.committer.commit(stale) is False
    assert await s.incidents.get(inc2.incident_id) is None

    # 교체 명시 — 이전 과제는 superseded
    assert await s.committer.commit(
        commit(second, row2.revision, row2.state, inc2, second_task, replaces=first_task)
    )
    now = await s.common.interview_task(s.posting_id, VERSION)
    assert now is not None and now.id == second_task


async def test_a_duplicate_case_id_rolls_back_the_interview_too(stores: Stores) -> None:
    s = stores
    iid, (_, task) = await interview(s)
    row = await s.interviews.get(iid)
    assert row is not None
    inc = s.case()
    await s.incidents.submit("가상회사", inc, source={"kind": "manual"}, created_by="x")
    with pytest.raises(ValueError):
        await s.committer.commit(commit(iid, row.revision, row.state, inc, task))
    after = await s.interviews.get(iid)
    assert (
        after is not None and after.revision == row.revision and after.state.stage == "task_review"
    )
    assert await s.common.interview_task(s.posting_id, VERSION) is None
    assert await s.common.missing([task]) == []
