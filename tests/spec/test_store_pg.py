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
