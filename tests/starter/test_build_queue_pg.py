"""빌드 큐 (#128) — task_db_test 에서 동시성까지 (`uv run pytest -m pg`). 가상 과제만.

- 생성 요청을 동시에 여러 번 보내도 진행 중 빌드는 하나 (공통 과제 행 잠금 + 부분 유일 색인)
- 두 builder 가 동시에 가져가도 한 빌드는 한 번만 (FOR UPDATE SKIP LOCKED)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from task_spec.spec.store import CommonKey, CommonTaskStore
from task_spec.starter.store import (
    LEASE_EXPIRED,
    SUPERSEDED,
    BuildRuleError,
    PgBuildStore,
)
from tests.postings.test_postings_pg import _Urls
from tests.spec.test_store_pg import _task

pytestmark = pytest.mark.pg

T0 = datetime(2026, 11, 12, tzinfo=UTC)


@dataclass
class Env:
    common: CommonTaskStore
    store: PgBuildStore
    posting: str

    async def feature_task(self, approve: bool = True, prompt: int = 1) -> str:
        """가상 공통 과제 한 판 — 기능형으로 바꾸고 (선택) 승인. 같은 prompt 면 같은 키(새 판이 이전 판을 대체)."""
        key = CommonKey(self.posting, "onto-test", f"draft_task@{prompt}")
        row = await self.common.save_draft(key, _task(), [])
        async with self.store._pool.connection() as conn:
            await conn.execute(
                "UPDATE public.common_task SET task = jsonb_set(task, '{task_kind}', '\"feature\"') "
                "WHERE id = %s",
                (row.id,),
            )
        if approve:
            await self.common.approve(row.id, "가상승인자", T0)
        return row.id


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    url = u.get_secret_value()
    common = await CommonTaskStore.open(url)
    store = await PgBuildStore.open(url)
    posting = f"test-{uuid.uuid4().hex[:8]}"
    try:
        yield Env(common, store, posting)
    finally:
        async with store._pool.connection() as conn:
            ids = (
                "SELECT b.id FROM public.common_task_build b JOIN public.common_task c "
                "ON c.id = b.common_id WHERE c.posting_id = %s"
            )
            await conn.execute(
                f"DELETE FROM public.starter_file WHERE build_id IN ({ids})", (posting,)
            )
            await conn.execute(
                f"DELETE FROM public.common_task_build WHERE id IN ({ids})", (posting,)
            )
            await conn.execute("DELETE FROM public.common_task WHERE posting_id = %s", (posting,))
        await store.close()
        await common.close()


async def _queue_only(env: Env, *common_ids: str) -> None:
    """시험 DB 에 남은 다른 요청이 끼지 않게 — 이 테스트의 공통 과제 밖 pending 요청은 요청 표시를 지운다 (가져가지 않게)."""
    async with env.store._pool.connection() as conn:
        await conn.execute(
            "UPDATE public.common_task_build SET requested_at = NULL "
            "WHERE status = 'pending' AND requested_at IS NOT NULL AND NOT (common_id = ANY(%s))",
            (list(common_ids),),
        )


async def test_request_rules_and_idempotency(env: Env) -> None:
    draft = await env.feature_task(approve=False)
    with pytest.raises(BuildRuleError, match="승인된 공통 과제만"):
        await env.store.request_build(draft, "가상승인자")
    with pytest.raises(KeyError):
        await env.store.request_build("ct-000000000000", "가상승인자")
    cid = await env.feature_task()
    results = await asyncio.gather(*(env.store.request_build(cid, f"u{i}") for i in range(5)))
    assert sum(created for _, created in results) == 1
    assert len({b.build_id for b, _ in results}) == 1
    (b,) = await env.store.builds(cid)
    assert b.status == "pending" and b.requested_by is not None and b.requested_at is not None


async def test_concurrent_claims_take_each_build_once(env: Env) -> None:
    cids = [await env.feature_task(prompt=n) for n in (1, 2, 3)]  # 키가 달라 셋 다 approved
    for c in cids:
        await env.store.request_build(c, "가상승인자")
    await _queue_only(env, *cids)
    got = await asyncio.gather(*(env.store.claim(f"w{i}", 60, T0) for i in range(5)))
    taken = [b.build_id for b in got if b is not None]
    assert sorted(taken) == sorted(f"{c}-b1" for c in cids)  # 셋 다 한 번씩, 남은 둘은 None
    assert all(b.status == "generating" and b.attempt == 1 for b in got if b is not None)


async def test_superseded_request_fails_on_claim(env: Env) -> None:
    old = await env.feature_task()
    await env.store.request_build(old, "가상승인자")
    new = await env.feature_task()  # 같은 공고 새 판 승인 → old 는 superseded
    await _queue_only(env, old)
    assert await env.store.claim("w1", 60, T0) is None
    stale = await env.store.get(f"{old}-b1")
    assert stale is not None and (stale.status, stale.error) == ("failed", SUPERSEDED)
    b, created = await env.store.request_build(new, "가상승인자")
    assert created and b.common_id == new


async def test_lease_heartbeat_stage_and_recovery(env: Env) -> None:
    cid = await env.feature_task()
    await env.store.request_build(cid, "가상승인자")
    await _queue_only(env, cid)
    b = await env.store.claim("w1", 60, T0)
    assert b is not None and b.lease_until == T0 + timedelta(seconds=60)
    assert await env.store.heartbeat(b.build_id, "w1", 60, T0 + timedelta(seconds=30))
    assert not await env.store.heartbeat(b.build_id, "w2", 60, T0)
    s = await env.store.record_stage(b.build_id, "contract", 0.25)
    assert (s.stage, s.cost_usd) == ("contract", 0.25)
    # lease 가 지나면 한 번은 다시 대기열로, 두 번째는 실패
    (back,) = [
        x for x in await env.store.recover_expired(T0 + timedelta(seconds=91)) if x.common_id == cid
    ]
    assert (back.status, back.error, back.worker) == ("pending", LEASE_EXPIRED, None)
    again = await env.store.claim("w2", 60, T0 + timedelta(seconds=100))
    assert again is not None and again.attempt == 2
    (dead,) = [
        x
        for x in await env.store.recover_expired(T0 + timedelta(seconds=200))
        if x.common_id == cid
    ]
    assert (dead.status, dead.error) == ("failed", LEASE_EXPIRED) and dead.finished_at is not None


async def test_builder_role_can_run_the_queue_but_not_ddl_or_checkpoints(env: Env) -> None:
    """builder 전용 DB 역할(docker/builder/db-role.sql) — 큐·빌드 표는 쓰고, DDL·체크포인트(지원자 파생물)는 못 한다 (#129).
    시험 DB 에 임시 역할로 같은 SQL 을 적용해 본다 (역할은 클러스터 단위라 시험 뒤 지운다)."""
    import secrets
    from pathlib import Path

    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from task_spec.schemas.build_record import README_PATH, StarterFile

    u = _Urls().test_database_url
    assert u is not None
    admin_url = u.get_secret_value()
    role = f"tb_{uuid.uuid4().hex[:8]}"
    pw = secrets.token_hex(12)
    sql = Path("docker/builder/db-role.sql").read_text("utf-8").replace("task_builder", role)
    async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as a:
        await a.execute(sql)  # type: ignore[arg-type]
        await a.execute(f"ALTER ROLE {role} PASSWORD '{pw}'")  # type: ignore[arg-type]
    url = make_conninfo(**{**conninfo_to_dict(admin_url), "user": role, "password": pw})
    try:
        cid = await env.feature_task()
        await env.store.request_build(cid, "가상승인자")  # 생성 요청은 API(표 주인)
        await _queue_only(env, cid)
        with pytest.raises(Exception, match="(?i)owner|permission"):
            await PgBuildStore.open(url)  # DDL — 표 주인이 아니라 못 한다
        store = await PgBuildStore.open(url, ddl=False)
        try:
            b = await store.claim("w-role", 60, T0)
            assert b is not None and b.common_id == cid
            assert (
                await store.task(cid) is not None and (await store.task_labels([cid]))[cid].revision
            )
            await store.record_stage(b.build_id, "build", 0.01)
            await store.put_files(
                b.build_id, [StarterFile.of(b.build_id, "readme", README_PATH, "# 가상")]
            )
            failed = await store.fail(b.build_id, "가상 실패")
            assert failed.status == "failed"
        finally:
            await store.close()
        async with await psycopg.AsyncConnection.connect(url) as c:
            for table in (
                "public.checkpoints",
                "public.checkpoint_writes",
                "public.provisional_posting",
            ):
                cur = await c.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
                row = await cur.fetchone()
                if row and row[0]:  # 표가 있으면 읽기가 막혀야 한다
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        await c.execute(f"SELECT 1 FROM {table} LIMIT 1")  # type: ignore[arg-type]
                    await c.rollback()
    finally:
        async with await psycopg.AsyncConnection.connect(admin_url, autocommit=True) as a:
            await a.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER")  # type: ignore[arg-type]
            await a.execute(f"DROP OWNED BY {role}")  # type: ignore[arg-type]
            await a.execute(f"DROP ROLE {role}")  # type: ignore[arg-type]
