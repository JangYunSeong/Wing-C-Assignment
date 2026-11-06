"""CommonTaskStore — task_db_test 에서 저장·재사용·승인·대체 (`uv run pytest -m pg`). 가상 과제만."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from task_spec.schemas import TargetCompetency, TaskItem, TechEnv
from task_spec.spec.store import CommonKey, CommonTaskStore
from tests.postings.test_postings_pg import _Urls

pytestmark = pytest.mark.pg


def _task() -> TaskItem:
    return TaskItem(
        task_id="tmp",
        track="common",
        target_competencies=[TargetCompetency(competency_id="comp:a", label="가상 역량")],
        tech_env=TechEnv(basis="공고 언어"),
        difficulty_target="medium",
        time_limit_min=60,
        title="가상 과제",
    )


async def test_store_roundtrip_reuse_approve_supersede() -> None:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await CommonTaskStore.open(u.get_secret_value())
    key = CommonKey(f"test-{uuid.uuid4().hex[:8]}", "onto-test", "draft_task@1")
    try:
        assert await store.reusable(key) is None
        r1 = await store.save_draft(key, _task(), [])
        assert r1.revision == 1 and r1.task.task_id == r1.id and r1.status == "draft"
        got = await store.reusable(key)
        assert got is not None and got.id == r1.id and got.task.title == "가상 과제"

        at = datetime(2026, 11, 3, tzinfo=UTC)
        await store.approve(r1.id, "reviewer-fake", at)
        r2 = await store.save_draft(key, _task(), [])
        assert r2.revision == 2
        got = await store.reusable(key)
        assert got is not None and got.id == r1.id and got.status == "approved"  # 승인 판 우선

        await store.approve(r2.id, "reviewer-fake", at)
        got = await store.reusable(key)
        assert got is not None and got.id == r2.id and got.approved_by == "reviewer-fake"
    finally:
        async with store._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.common_task WHERE posting_id = %s", (key.posting_id,)
            )
        await store.close()


async def test_store_approved_kind_follows_latest_approval() -> None:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await CommonTaskStore.open(u.get_secret_value())
    posting = f"test-{uuid.uuid4().hex[:8]}"
    k_inc = CommonKey(posting, "onto-test", "draft_task@3")
    k_feat = CommonKey(posting, "onto-test", "skeleton+feature")
    try:
        assert await store.approved_kind(posting, "onto-test") is None
        inc = await store.save_draft(k_inc, _task(), [])
        assert await store.approved_kind(posting, "onto-test") is None  # 초안은 세지 않음
        await store.approve(inc.id, "reviewer-fake", datetime(2026, 11, 6, tzinfo=UTC))
        assert await store.approved_kind(posting, "onto-test") == "incident"

        feat = await store.save_draft(
            k_feat, _task().model_copy(update={"task_kind": "feature"}), []
        )
        await store.approve(feat.id, "reviewer-fake", datetime(2026, 11, 7, tzinfo=UTC))
        assert await store.approved_kind(posting, "onto-test") == "feature"
        assert await store.approved_kind(posting, "onto-other") is None
    finally:
        async with store._pool.connection() as conn:
            await conn.execute("DELETE FROM public.common_task WHERE posting_id = %s", (posting,))
        await store.close()
