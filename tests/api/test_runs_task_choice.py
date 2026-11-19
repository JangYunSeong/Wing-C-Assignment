"""실행 만들 때 담당자가 문제를 고른다 [확정: 2026-11-20 사용자 — 같은 공고에 문제가 여럿이면] — GET /postings/{id}/tasks ·
POST /runs 의 common_id. ASGI 앱 + memory 체크포인터 + InMemoryCommonStore. 가상 데이터만.
공고 묶음(이어진 공고)은 공고 저장소가 있어야 해서 tests/postings/test_postings_pg.py 에서 본다."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from task_spec.nodes.spec import FLAG_CHOSEN_COMMON
from task_spec.spec.store import CommonKey, InMemoryCommonStore, StoredCommon
from tests.api.conftest import FAKE_FILE, reviewer_decision, wait_status
from tests.api.test_runs_interview import ONTO, POSTING, _interview, api  # noqa: F401 — 픽스처
from tests.spec.test_interview_common import _task


async def _generated(
    store: InMemoryCommonStore, title: str, *, approve: bool = True
) -> StoredCommon:
    """공고 문구로 만든 공통 과제 (인터뷰 과제 아님)."""
    key = CommonKey(POSTING, ONTO, f"draft_feature_task@11+가상#{title}")
    row = await store.save_draft(key, _task(title), [])
    if approve:
        await store.approve(row.id, "task-reviewer-fake", datetime(2026, 11, 20, tzinfo=UTC))
    return row


async def _run(client: httpx.AsyncClient, **data: str) -> httpx.Response:
    return await client.post(
        "/runs", data={"posting_id": POSTING, **data}, files=[("resumes", FAKE_FILE)]
    )


async def test_tasks_lists_every_approved_problem_of_the_posting(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    iv = await _interview(store, "iv-1")
    gen = await _generated(store, "가상 목록 조회 과제")
    await _generated(store, "가상 초안 과제", approve=False)  # 승인 전은 고를 수 없다
    tasks = (await client.get(f"/postings/{POSTING}/tasks")).json()
    assert [t["common_id"] for t in tasks] == [gen.id, iv.id]  # 최근 승인 순
    assert [t["interview"] for t in tasks] == [False, True]
    assert tasks[0]["posting_id"] == POSTING and tasks[0]["title"] == "가상 목록 조회 과제"


async def test_run_uses_the_chosen_problem_over_the_interview_task(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    iv = await _interview(store, "iv-1")
    gen = await _generated(store, "가상 목록 조회 과제")
    # 고르지 않으면 지금처럼 공고의 인터뷰 과제
    plain = await _run(client)
    view = await wait_status(client, plain.json()["run_id"], "awaiting_approval")
    assert [t["task_id"] for t in view["spec"]["common_tasks"]] == [iv.id]
    # 고르면 그 문제
    resp = await _run(client, common_id=gen.id)
    assert resp.status_code == 202, resp.text
    view = await wait_status(client, resp.json()["run_id"], "awaiting_approval")
    assert [t["task_id"] for t in view["spec"]["common_tasks"]] == [gen.id]
    assert FLAG_CHOSEN_COMMON + gen.id in view["spec"]["review_flags"]
    approved = await client.post(
        f"/runs/{view['run_id']}/approve", json=reviewer_decision("approved")
    )
    assert approved.status_code == 202, approved.text
    await wait_status(client, view["run_id"], "done")
    # 고른 문제는 이미 승인된 과제 — 지원자 명세 승인이 다시 승인해 원래 승인 기록을 덮지 않는다
    row = next(r for r in store.rows if r["id"] == gen.id)
    assert (row["status"], row["approved_by"]) == ("approved", "task-reviewer-fake")
    assert (await client.get(f"/runs/{view['run_id']}")).json()["common_id"] == gen.id


async def test_problem_that_is_not_choosable_is_422(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    draft = await _generated(store, "가상 초안 과제", approve=False)
    data_dir = client._transport.app.state.settings.data_dir  # type: ignore[attr-defined]
    for bad in ("ct-000000000000", draft.id):
        resp = await _run(client, common_id=bad)
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "common_task_not_choosable"
    assert not (data_dir / "raw").exists() or not any(
        (data_dir / "raw").iterdir()
    )  # 지원서를 남기지 않는다


async def test_chosen_problem_runs_even_if_the_interview_task_is_on_another_version(
    api: tuple[httpx.AsyncClient, InMemoryCommonStore],  # noqa: F811
) -> None:
    client, store = api
    await _interview(store, "iv-old", version="onto-v2026.10.01")
    assert (await _run(client)).status_code == 409  # 고르지 않으면 지금처럼 멈춘다
    gen = await _generated(store, "가상 목록 조회 과제")
    resp = await _run(client, common_id=gen.id)
    assert resp.status_code == 202, resp.text
    view = await wait_status(client, resp.json()["run_id"], "awaiting_approval")
    assert [t["task_id"] for t in view["spec"]["common_tasks"]] == [gen.id]
