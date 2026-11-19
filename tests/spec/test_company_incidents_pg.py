"""CompanyIncidentStore · CommonTaskStore 의 회사 사례 조회 — task_db_test (`uv run pytest -m pg`). 가상 사례만."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from task_spec.schemas import TaskItem
from task_spec.spec.company_incidents import CompanyIncidentStore
from task_spec.spec.store import CommonKey, CommonTaskStore
from tests.postings.test_postings_pg import _Urls
from tests.spec.test_company_incidents import _company_incident

pytestmark = pytest.mark.pg
AT = datetime(2026, 11, 19, tzinfo=UTC)


def _task(source: str | None) -> TaskItem:
    return TaskItem.model_validate(
        {
            "task_id": "tmp",
            "track": "common",
            "target_competencies": [{"competency_id": "comp:a", "label": "가상 역량"}],
            "tech_env": {"basis": "공고 언어"},
            "difficulty_target": "medium",
            "time_limit_min": 60,
            "source_incident": source,
        }
    )


async def test_company_incident_store_roundtrip() -> None:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    store = await CompanyIncidentStore.open(u.get_secret_value())
    company = f"가상회사-{uuid.uuid4().hex[:6]}"
    suffix = uuid.uuid4().hex[:12]
    try:
        rec = await store.submit(
            company,
            _company_incident(suffix),
            source={"kind": "manual"},
            created_by="담당자-가상",
        )
        with pytest.raises(ValueError, match="이미"):
            await store.submit(
                company, _company_incident(suffix), source={}, created_by="담당자-가상"
            )
        got = await store.get(rec.incident_id)
        assert got is not None and got.incident == rec.incident and got.status == "pending"
        assert await store.shared(company, "sw.backend") == []
        assert not await store.decide(rec.incident_id, "0" * 64, "shared", "r", AT, None)
        assert await store.decide(rec.incident_id, rec.content_sha256, "shared", "r", AT, "좋음")
        assert not await store.decide(rec.incident_id, rec.content_sha256, "rejected", "r", AT, "x")
        shared = await store.shared(company, "sw.backend")
        assert [s.incident_id for s in shared] == [rec.incident_id]
        assert shared[0].decision_comment == "좋음" and shared[0].source == {"kind": "manual"}
        assert [r.incident_id for r in await store.find(company_id=company)] == [rec.incident_id]
        assert await store.find(company_id=company, status="pending") == []
        assert await store.retire(rec.incident_id, "r", AT, "밖으로 샘")
        assert not await store.retire(rec.incident_id, "r", AT, "두 번")
        assert await store.shared(company, "sw.backend") == []
    finally:
        async with store._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.company_incident WHERE company_id = %s", (company,)
            )
        await store.close()


async def test_common_task_incident_usage_and_posting_incidents() -> None:
    u = _Urls().test_database_url
    if u is None:
        pytest.skip("TEST_DATABASE_URL 없음")
    common = await CommonTaskStore.open(u.get_secret_value())
    tag = uuid.uuid4().hex[:8]
    a, b, c = (f"test-{tag}-{n}" for n in "abc")
    co = f"co:sw.backend:{tag}0000"
    try:
        for posting in (a, b):
            row = await common.save_draft(
                CommonKey(posting, "onto-test", "draft_task@3"), _task(co), []
            )
            await common.approve(row.id, "r", AT)
        await common.save_draft(
            CommonKey(c, "onto-test", "draft_task@3"), _task(co), []
        )  # 초안 — 세지 않음
        assert await common.incident_usage([co], exclude_posting=a) == {co: 1}
        assert await common.incident_usage([co], exclude_posting="") == {co: 2}
        assert await common.incident_usage([], exclude_posting="") == {}

        # 공고의 사례 순서 — 승인된 것(최근 승인 순) → 초안만 → 사례 없는 과제 "" (in-memory 와 같은 순서)
        await common.save_draft(CommonKey(a, "onto-test", "skeleton"), _task(None), [])
        assert await common.posting_incidents(a) == [co, ""]
        co2 = f"co:sw.backend:{tag}0001"
        row = await common.save_draft(CommonKey(a, "onto-test", "draft_task@4"), _task(co2), [])
        await common.approve(row.id, "r", datetime(2026, 11, 20, tzinfo=UTC))
        await common.save_draft(
            CommonKey(a, "onto-test", "draft_task@5"), _task(co), []
        )  # co 의 새 초안
        assert await common.posting_incidents(a) == [
            co2,
            co,
            "",
        ]  # 새 초안이 co 를 앞으로 올리지 않는다
        assert await common.posting_incidents(c) == [co]
        assert await common.posting_incidents(f"test-{tag}-none") == []
    finally:
        async with common._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM public.common_task WHERE posting_id = ANY(%s)", ([a, b, c],)
            )
        await common.close()
