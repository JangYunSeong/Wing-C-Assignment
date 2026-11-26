"""CommonTaskStore — task_db_test 에서 저장·재사용·승인·대체 (`uv run pytest -m pg`). 가상 과제만."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from task_spec.schemas import TargetCompetency, TaskItem, TechEnv
from task_spec.spec.store import (
    CommonKey,
    CommonTaskStore,
    InterviewTaskConflict,
    approve_interview_task,
)
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
        assert await store.missing([r1.id, "ct-nope"]) == ["ct-nope"]
        assert await store.missing([]) == []
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


async def test_store_interview_task_one_per_posting_and_explicit_replacement() -> None:
    """인터뷰 과제 [확정: 2026-11-19 사용자] — 공고(같은 판)마다 승인된 것 하나(부분 유일 색인), 교체는 replaces 로 명시,
    지원자 명세 승인용 approve() 는 인터뷰 과제를 거절, 판마다 따로."""
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await CommonTaskStore.open(u.get_secret_value())
    posting = f"test-{uuid.uuid4().hex[:8]}"
    at = datetime(2026, 11, 19, tzinfo=UTC)
    try:
        assert await store.interview_task(posting, "onto-test") is None
        assert await store.interview_versions(posting) == []
        a = await store.save_draft(CommonKey(posting, "onto-test", "interview:iv-a"), _task(), [])
        b = await store.save_draft(CommonKey(posting, "onto-test", "interview:iv-b"), _task(), [])
        with pytest.raises(ValueError, match="approve_interview"):
            await store.approve(a.id, "reviewer-fake", at)

        await store.approve_interview(a.id, "recruiter-fake", at)
        got = await store.interview_task(posting, "onto-test")
        assert got is not None and got.id == a.id and got.prompt_ref == "interview:iv-a"
        assert got.approved_by == "recruiter-fake"
        with pytest.raises(InterviewTaskConflict):
            await store.approve_interview(b.id, "recruiter-2", at)
        with pytest.raises(InterviewTaskConflict):
            await store.approve_interview(b.id, "recruiter-2", at, replaces="ct-nope")
        await store.approve_interview(a.id, "someone-else", at)  # 이미 지금 과제 — 기록 유지
        kept = await store.interview_task(posting, "onto-test")
        assert kept is not None and kept.approved_by == "recruiter-fake"

        await store.approve_interview(b.id, "recruiter-2", at, replaces=a.id)
        got = await store.interview_task(posting, "onto-test")
        assert got is not None and got.id == b.id

        # 색인이 막는다 — 앱 경로를 거치지 않고 둘째를 승인하려 해도
        async with store._pool.connection() as conn:
            with pytest.raises(Exception) as e:
                await conn.execute(
                    "UPDATE public.common_task SET status = 'approved' WHERE id = %s", (a.id,)
                )
            assert type(e.value).__name__ == "UniqueViolation"

        other = await store.save_draft(
            CommonKey(posting, "onto-old", "interview:iv-a"), _task(), []
        )
        await store.approve_interview(other.id, "recruiter-fake", at)  # 다른 판은 교체가 아니다
        assert await store.interview_versions(posting) == ["onto-old", "onto-test"]
        p1 = await store.save_draft(CommonKey(posting, "onto-test", "draft_task@3"), _task(), [])
        with pytest.raises(ValueError, match="인터뷰 과제가 아닙니다"):
            await store.approve_interview(p1.id, "recruiter-fake", at)
    finally:
        async with store._pool.connection() as conn:
            await conn.execute("DELETE FROM public.common_task WHERE posting_id = %s", (posting,))
        await store.close()


async def test_store_concurrent_first_interview_approvals_one_wins() -> None:
    """같은 공고에 인터뷰 과제 둘을 동시에 처음 승인 — 둘 다 '지금 과제 없음'을 보지만 색인이 하나만 남기고,
    진 쪽은 DB 오류가 아니라 이긴 과제를 알려 주는 InterviewTaskConflict. savepoint 라 호출하는 쪽 트랜잭션은 그대로 쓸 수 있다."""
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await CommonTaskStore.open(u.get_secret_value())
    posting = f"test-{uuid.uuid4().hex[:8]}"
    at = datetime(2026, 11, 19, tzinfo=UTC)
    try:
        a = await store.save_draft(CommonKey(posting, "onto-test", "interview:iv-a"), _task(), [])
        b = await store.save_draft(CommonKey(posting, "onto-test", "interview:iv-b"), _task(), [])
        async with store._pool.connection() as c1, store._pool.connection() as c2:
            async with c1.transaction():
                await approve_interview_task(c1, a.id, "recruiter-a", at)
                late = asyncio.create_task(_approve_catching(c2, b.id, None))
                await asyncio.sleep(0.3)  # 둘째가 색인 검사에서 첫째의 커밋을 기다리는 중
                assert not late.done()
            err = await asyncio.wait_for(late, 5)
        assert err is not None and (err.current, err.replaces) == (a.id, None)
        got = await store.interview_task(posting, "onto-test")
        assert got is not None and got.id == a.id
    finally:
        async with store._pool.connection() as conn:
            await conn.execute("DELETE FROM public.common_task WHERE posting_id = %s", (posting,))
        await store.close()


async def test_store_concurrent_replacements_late_one_names_the_winner() -> None:
    """같은 과제 A 를 두 사람이 동시에 교체(replaces=A) — 늦은 쪽은 잠금을 기다린 뒤 A 가 빠진 것만 보지만, 알릴 때는 지금 과제(이긴
    쪽)를 다시 읽어 알려 준다. 교체된 A 는 다시 승인하지 않는다."""
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await CommonTaskStore.open(u.get_secret_value())
    posting = f"test-{uuid.uuid4().hex[:8]}"
    at = datetime(2026, 11, 19, tzinfo=UTC)
    try:
        ids = []
        for name in ("iv-a", "iv-b", "iv-c"):
            row = await store.save_draft(
                CommonKey(posting, "onto-test", f"interview:{name}"), _task(), []
            )
            ids.append(row.id)
        a, b, c = ids
        await store.approve_interview(a, "recruiter-a", at)
        async with store._pool.connection() as c1, store._pool.connection() as c2:
            async with c1.transaction():
                await approve_interview_task(c1, b, "recruiter-b", at, replaces=a)
                late = asyncio.create_task(_approve_catching(c2, c, a))
                await asyncio.sleep(0.3)  # 둘째가 A 의 행 잠금을 기다리는 중
                assert not late.done()
            err = await asyncio.wait_for(late, 5)
        assert err is not None and (err.current, err.replaces) == (b, a)
        assert b in str(err)
        got = await store.interview_task(posting, "onto-test")
        assert got is not None and got.id == b
        with pytest.raises(ValueError, match="교체된 인터뷰 과제"):
            await store.approve_interview(a, "recruiter-a", at, replaces=b)
    finally:
        async with store._pool.connection() as conn:
            await conn.execute("DELETE FROM public.common_task WHERE posting_id = %s", (posting,))
        await store.close()


async def _approve_catching(
    conn: Any, common_id: str, replaces: str | None
) -> InterviewTaskConflict | None:
    """호출하는 쪽 트랜잭션 안에서 승인 — 충돌을 잡은 뒤에도 그 트랜잭션으로 계속 질의할 수 있어야 한다."""
    at = datetime(2026, 11, 19, tzinfo=UTC)
    async with conn.transaction():
        try:
            await approve_interview_task(conn, common_id, "recruiter-late", at, replaces=replaces)
        except InterviewTaskConflict as e:
            cur = await conn.execute("SELECT 1")
            assert await cur.fetchone() == (1,)
            return e
    return None


async def test_store_keeps_the_framed_material_with_the_draft() -> None:
    """#318 D1: 틀 적용 재료와 틀은 공통 과제 행의 framed 칸에 — 빌더 저장소도 같은 칸을 읽는다. 틀이 없으면 NULL."""
    from task_spec.schemas import FeatureSeed, FramedMaterial, TrapFrame
    from task_spec.starter.store import PgBuildStore
    from tests.interview.test_task import feature_seed_output

    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await CommonTaskStore.open(u.get_secret_value())
    key = CommonKey(f"test-{uuid.uuid4().hex[:8]}", "onto-test", "interview:int-test")
    framed = FramedMaterial(
        seed=FeatureSeed.model_validate_json(feature_seed_output()),
        frame=TrapFrame.model_validate(
            {
                "kind": "design",
                "hidden_requirement": "가상 숨은 요구",
                "forbidden_storage": [{"entity": "Session", "why": "가상 이유"}],
                "reason": "pool_acquire_timeout",
            }
        ),
    )
    try:
        r = await store.save_draft(key, _task(), [], framed=framed)
        plain = await store.save_draft(key, _task(), [])
        assert await store.framed(r.id) == framed and await store.framed(plain.id) is None
        builds = await PgBuildStore.open(u.get_secret_value(), ddl=False)
        try:
            assert await builds.framed(r.id) == framed and await builds.framed(plain.id) is None
        finally:
            await builds.close()
    finally:
        async with store._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.common_task WHERE posting_id = %s", (key.posting_id,)
            )
        await store.close()
