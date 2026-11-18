"""PgBuildStore — task_db_test 에서 같은 수명 주기 시나리오 (`uv run pytest -m pg`). 가상 과제만."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from task_spec.spec.store import CommonKey, CommonTaskStore
from task_spec.starter.store import PgBuildStore
from tests.postings.test_postings_pg import _Urls
from tests.spec.test_store_pg import _task
from tests.starter.test_build_store import run_failure_record, run_lifecycle

pytestmark = pytest.mark.pg


async def test_lifecycle_pg() -> None:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    url = u.get_secret_value()
    common = await CommonTaskStore.open(url)
    store = await PgBuildStore.open(url)
    key = CommonKey(f"test-{uuid.uuid4().hex[:8]}", "onto-test", "draft_task@1")
    row = await common.save_draft(key, _task(), [])
    tag = f"t-{uuid.uuid4().hex[:8]}"  # 전달 대상 이름 — 실행마다 따로
    try:
        await run_lifecycle(store, row.id, tag)
        # 최근 빌드 — limit=None 이면 전부 (LIMIT NULL). 테스트 DB 는 다른 빌드와 같이 쓴다
        mine = {b.build_id for b in await store.builds(row.id)}
        assert mine and mine <= {b.build_id for b in await store.recent(None)}
        assert len(await store.recent(1)) == 1
    finally:
        async with store._pool.connection() as conn:
            ids = "SELECT id FROM public.common_task_build WHERE common_id = %s"
            await conn.execute(
                f"DELETE FROM public.artifact_delivery WHERE build_id IN ({ids})", (row.id,)
            )
            await conn.execute(
                f"DELETE FROM public.build_artifact WHERE build_id IN ({ids})", (row.id,)
            )
            await conn.execute(
                f"DELETE FROM public.starter_file WHERE build_id IN ({ids})", (row.id,)
            )
            await conn.execute(
                "DELETE FROM public.common_task_build WHERE common_id = %s", (row.id,)
            )
            await conn.execute(
                "DELETE FROM public.delivery_target WHERE name LIKE %s", (f"{tag}-%",)
            )
            await conn.execute(
                "DELETE FROM public.common_task WHERE posting_id = %s", (key.posting_id,)
            )
        await store.close()
        await common.close()


@pytest.fixture
async def pg_common() -> AsyncIterator[tuple[PgBuildStore, str, str]]:
    """(표 주인 저장소, 이 실행만의 가상 공통 과제 id, 전달 대상 이름표) — 끝나면 test_lifecycle_pg 와 같이 지운다."""
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    url = u.get_secret_value()
    common = await CommonTaskStore.open(url)
    store = await PgBuildStore.open(url)
    key = CommonKey(f"test-{uuid.uuid4().hex[:8]}", "onto-test", "draft_task@1")
    row = await common.save_draft(key, _task(), [])
    tag = f"t-{uuid.uuid4().hex[:8]}"
    try:
        yield store, row.id, tag
    finally:
        async with store._pool.connection() as conn:
            ids = "SELECT id FROM public.common_task_build WHERE common_id = %s"
            await conn.execute(
                f"DELETE FROM public.artifact_delivery WHERE build_id IN ({ids})", (row.id,)
            )
            await conn.execute(
                f"DELETE FROM public.build_artifact WHERE build_id IN ({ids})", (row.id,)
            )
            await conn.execute(
                f"DELETE FROM public.starter_file WHERE build_id IN ({ids})", (row.id,)
            )
            await conn.execute(
                "DELETE FROM public.common_task_build WHERE common_id = %s", (row.id,)
            )
            await conn.execute(
                "DELETE FROM public.delivery_target WHERE name LIKE %s", (f"{tag}-%",)
            )
            await conn.execute(
                "DELETE FROM public.common_task WHERE posting_id = %s", (key.posting_id,)
            )
        await store.close()
        await common.close()


async def test_failure_record_pg(pg_common: tuple[PgBuildStore, str, str]) -> None:
    """#250 — 메모리 구현과 같은 계약 (run_failure_record)."""
    store, common_id, _ = pg_common
    await run_failure_record(store, common_id)
