"""PgInterviewStore — task_db_test (`uv run pytest -m pg`). 가상 인터뷰만 (#262)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from task_spec.interview.store import PgInterviewStore
from task_spec.schemas.interview_state import ChatMessage, InterviewState, PostingContext
from tests.postings.test_postings_pg import _Urls

pytestmark = pytest.mark.pg


def _state() -> InterviewState:
    return InterviewState(
        stage="incident_interview",
        company="가상회사",
        title="가상 백엔드",
        posting_id="prov-0123456789ab",
        posting_context=PostingContext(
            posting_id="prov-0123456789ab", sub="sw.backend", ontology_version="onto-test"
        ),
        incident_sub="sw.backend",
        incident_messages=[ChatMessage(role="assistant", text="가상 질문")],
    )


async def test_interview_store_revision_drafts_and_retention() -> None:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await PgInterviewStore.open(u.get_secret_value())
    iid = f"int-{uuid.uuid4().hex}"
    cutoff = datetime.now(UTC) - timedelta(days=1)
    try:
        row = await store.create(iid, _state())
        assert row.revision == 1 and row.ui_drafts == {} and row.draft_revision == 0
        # 임시 저장 — draft_revision 으로 충돌 검사
        drafts = await store.save_drafts(
            iid, 0, {"3": {"values": {"m": "가상"}}, "4": {"values": {}}}, cutoff=cutoff
        )
        assert drafts is not None and drafts.draft_revision == 1 and drafts.drafts_started_at
        assert await store.save_drafts(iid, 0, {"3": {"values": {}}}, cutoff=cutoff) is None
        # 상태 저장 — 판본이 맞을 때만, 제출한 단계의 임시 저장을 지운다
        state = row.state.model_copy(deep=True)
        state.stage = "trap_interview"
        saved = await store.save(iid, 1, state, clear_steps=["3"])
        assert saved is not None and saved.revision == 2 and saved.state.stage == "trap_interview"
        assert list(saved.ui_drafts) == ["4"] and saved.draft_revision == 2
        assert await store.save(iid, 1, state) is None
        cleared = await store.save(iid, 2, state, clear_steps=["4"])
        assert cleared is not None and cleared.ui_drafts == {} and cleared.drafts_started_at is None
        # 보관 기간 — 오래된 임시 저장, 방치된 인터뷰
        await store.save_drafts(
            iid, cleared.draft_revision, {"5": {"values": {"m": "가상"}}}, cutoff=cutoff
        )
        future = datetime.now(UTC) + timedelta(days=1)
        assert await store.purge_drafts(future) >= 1
        purged = await store.get(iid)
        assert purged is not None and purged.ui_drafts == {}
        assert iid in await store.stale(future)
        closed = purged.state.model_copy(deep=True)
        closed.stage = "discarded"
        assert await store.save(iid, purged.revision, closed, clear_all_drafts=True) is not None
        assert iid not in await store.stale(future)
        assert (
            await store.save_drafts(
                iid, purged.draft_revision + 1, {"3": {"values": {}}}, cutoff=cutoff
            )
            is None
        )
    finally:
        async with store._pool.connection() as conn:
            await conn.execute("DELETE FROM public.recruiter_interview WHERE id = %s", (iid,))
        await store.close()
